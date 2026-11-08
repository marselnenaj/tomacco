//! `diagnostics.report_draft`: a bug report the person can read before it
//! leaves this computer. A directory with `issue.md` (title, environment,
//! steps, what was expected and what happened, excerpts of the failing
//! jobs' logs, the attachments with their digests), the attachments
//! themselves (the redacted bundle, the log tails) and
//! `redaction-receipt.json`, which counts what was masked: secrets of known
//! shapes, the home directory, device serials and UDIDs, and which absolute
//! paths remain so the person can look at them. Nothing is posted or
//! uploaded; `issue_url` carries only the title, never the body.
use crate::{core::*, diagnostics, jobs, redact};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};

const ENDED_BADLY: &[&str] = &["failed", "timed_out", "cancelled"];

/// Replaces device serials and UDIDs with stable placeholders: an iPhone
/// UDID (`00008110-0012...`, 25 characters, or 40 hex digits) and an
/// Android serial as `adb` prints it after `device.list`-style prefixes.
/// Emulator serials (`emulator-5554`) say nothing about a person and stay.
pub fn mask_device_ids(text: &str, table: &mut BTreeMap<String, String>) -> String {
    static PATTERNS: std::sync::OnceLock<Vec<regex::Regex>> = std::sync::OnceLock::new();
    let patterns = PATTERNS.get_or_init(|| {
        vec![
            regex::Regex::new(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}\b").unwrap(),
            regex::Regex::new(r"\b[0-9a-f]{40}\b").unwrap(),
            // Serials after the words adb -s, device:, serial:, udid:, or --udid.
            regex::Regex::new(r#"(?i)(adb -s |-s["', ]+|--udid[= ]|\b(?:device|serial|udid)["':= ]+)([A-Za-z0-9]{6,32})\b"#).unwrap(),
        ]
    });
    let mut out = text.to_string();
    for (i, pattern) in patterns.iter().enumerate() {
        out = pattern
            .replace_all(&out, |caps: &regex::Captures| {
                let (prefix, id) = if i == 2 {
                    (caps[1].to_string(), caps[2].to_string())
                } else {
                    (String::new(), caps[0].to_string())
                };
                if id.starts_with("emulator-")
                    || id.eq_ignore_ascii_case("airplay")
                    || id.chars().all(|c| c.is_ascii_digit()) && id.len() < 8
                {
                    return format!("{prefix}{id}");
                }
                let next = table.len() + 1;
                let placeholder = table
                    .entry(id.clone())
                    .or_insert_with(|| format!("[device-{next}]"))
                    .clone();
                format!("{prefix}{placeholder}")
            })
            .into_owned();
    }
    // An id seen once with a prefix is masked wherever else it appears bare.
    for (id, placeholder) in table.iter() {
        out = out.replace(id.as_str(), placeholder);
    }
    out
}

/// Absolute paths outside the (already replaced) home directory, for the receipt.
pub fn absolute_paths(text: &str) -> Vec<String> {
    static PATH: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let pattern = PATH.get_or_init(|| regex::Regex::new(r#"(?:^|[\s"'=:(\[])(/(?:home|Users|tmp|var|opt|mnt|media|run|srv)/[^\s"'),\]]{1,200})"#).unwrap());
    let mut found: Vec<String> = pattern
        .captures_iter(text)
        .map(|c| c[1].to_string())
        .collect();
    found.sort();
    found.dedup();
    found.truncate(50);
    found
}

fn scrub(
    text: &str,
    home: Option<&str>,
    table: &mut BTreeMap<String, String>,
    mask_devices: bool,
) -> (String, usize) {
    let masked = redact::text(text).into_owned();
    let secrets = masked
        .matches(redact::MASK)
        .count()
        .saturating_sub(text.matches(redact::MASK).count());
    let mut out = match home.filter(|h| h.len() > 1) {
        Some(home) => masked.replace(home, "~"),
        None => masked,
    };
    if mask_devices {
        out = mask_device_ids(&out, table);
    }
    (out, secrets)
}

fn text_argument(a: &Value, key: &str, limit: usize) -> Result<String> {
    let value = s(a, key).trim().to_string();
    if value.chars().count() > limit {
        return Err(Error::new(
            "invalid_argument",
            format!("{key} is at most {limit} characters"),
        ));
    }
    Ok(value)
}

fn fenced(text: &str) -> String {
    format!("```text\n{}\n```\n", text.trim_end())
}

pub fn draft(a: &Value) -> Result {
    let title = text_argument(a, "title", 120)?;
    if title.is_empty() {
        return Err(Error::new(
            "invalid_argument",
            "title says what went wrong, in one line",
        ));
    }
    let steps = text_argument(a, "steps", 4000)?;
    let expected = text_argument(a, "expected", 2000)?;
    let actual = text_argument(a, "actual", 2000)?;
    let anonymize = a["anonymize_home"].as_bool().unwrap_or(true);
    let mask_devices = a["device_ids"].as_str() != Some("keep");
    let count = a["jobs"].as_u64().unwrap_or(3).min(20);
    let log_limit = a["log_kb"].as_u64().unwrap_or(16).clamp(1, 1024) * 1024;
    let home = anonymize.then(|| user_home().to_string_lossy().into_owned());
    let directory = if s(a, "directory").is_empty() {
        let base = home_dir()?.join("diagnostics");
        private_dir(&base)?;
        base.join(format!("tomacco-report-draft-{}", now() as u64))
    } else {
        let given = path(s(a, "directory"), false, false)?;
        if given.exists() && (!given.is_dir() || fs::read_dir(&given)?.next().is_some()) {
            return Err(Error::new(
                "path_exists",
                "Choose an empty or new directory for the draft",
            ));
        }
        given
    };
    fs::create_dir_all(directory.join("attachments"))?;
    let mut table = BTreeMap::new();
    let mut secrets_masked = 0usize;
    let mut files: Vec<(String, Vec<u8>)> = Vec::new();
    let version = crate::update::current_version();
    let host = diagnostics::host();
    // The bundle, as diagnostics.bundle writes it, is the first attachment.
    let bundle = if a["bundle"].as_bool().unwrap_or(true) {
        let target = directory.join("attachments").join("tomacco-report.tar.gz");
        match diagnostics::bundle(
            &json!({"path":target,"jobs":count,"log_kb":log_limit / 1024,"anonymize_home":anonymize}),
        ) {
            Ok(written) => Some(written),
            Err(error) => Some(json!({"error":error.value()})),
        }
    } else {
        None
    };
    // Excerpts of the failing jobs, scrubbed again with the device ids masked.
    let mut tails = Vec::new();
    if count > 0 {
        if let Ok(listed) = jobs::list(&json!({"state":ENDED_BADLY,"limit":count})) {
            for job in listed.as_array().cloned().unwrap_or_default() {
                let tail = diagnostics::log_tail(Path::new(s(&job, "log_path")), log_limit);
                tails.push((job, tail));
            }
        }
    }
    // Ids are learnt over every text first, so one named with a prefix in
    // the steps is masked where it appears bare in a log.
    if mask_devices {
        let mut probe = format!(
            "{title}
{steps}
{expected}
{actual}
"
        );
        for (job, tail) in &tails {
            probe.push_str(s(job, "label"));
            probe.push('\n');
            probe.push_str(tail);
        }
        mask_device_ids(&probe, &mut table);
    }
    let mut excerpts = String::new();
    let mut job_list = Vec::new();
    {
        {
            for (job, tail) in tails {
                let id = s(&job, "id").to_string();
                let (clean, n) = scrub(&tail, home.as_deref(), &mut table, mask_devices);
                secrets_masked += n;
                let last: Vec<&str> = clean
                    .lines()
                    .rev()
                    .take(40)
                    .collect::<Vec<_>>()
                    .into_iter()
                    .rev()
                    .collect();
                let (label, n) = scrub(s(&job, "label"), home.as_deref(), &mut table, mask_devices);
                secrets_masked += n;
                excerpts.push_str(&format!(
                    "### {} ({}, `{}`)\n\n{}\n",
                    label,
                    s(&job, "state"),
                    &id[..8],
                    fenced(&last.join("\n"))
                ));
                files.push((
                    format!("attachments/job-{}.log", &id[..8]),
                    clean.into_bytes(),
                ));
                job_list.push(json!({"id":id,"label":label,"state":job["state"]}));
            }
        }
    }
    let (steps_clean, n1) = scrub(&steps, home.as_deref(), &mut table, mask_devices);
    let (expected_clean, n2) = scrub(&expected, home.as_deref(), &mut table, mask_devices);
    let (actual_clean, n3) = scrub(&actual, home.as_deref(), &mut table, mask_devices);
    let (title_clean, n4) = scrub(&title, home.as_deref(), &mut table, mask_devices);
    secrets_masked += n1 + n2 + n3 + n4;
    let mut issue = format!("# {title_clean}\n\n");
    issue.push_str("## Environment\n\n");
    issue.push_str(&format!("| | |\n| --- | --- |\n| Tomacco | {version} |\n| OS | {} |\n| Kernel | {} |\n| Architecture | {} |\n| CPUs | {} |\n| Memory | {} |\n| KVM | {} |\n| Session | {} {} |\n\n",
        s(&host, "os"), s(&host, "kernel"), s(&host, "architecture"), host["cpus"],
        host["memory_bytes"].as_u64().map_or("unknown".to_string(), |b| format!("{} GiB", b / (1024 * 1024 * 1024))),
        host["kvm"], s(&host, "session"), s(&host, "desktop")));
    issue.push_str("## Steps\n\n");
    issue.push_str(if steps_clean.is_empty() {
        "_(describe what you did)_\n"
    } else {
        &steps_clean
    });
    issue.push_str("\n\n## Expected\n\n");
    issue.push_str(if expected_clean.is_empty() {
        "_(what should have happened)_\n"
    } else {
        &expected_clean
    });
    issue.push_str("\n\n## What happened\n\n");
    issue.push_str(if actual_clean.is_empty() {
        "_(what happened instead)_\n"
    } else {
        &actual_clean
    });
    issue.push_str("\n\n");
    if !excerpts.is_empty() {
        issue.push_str("## Jobs that did not succeed\n\n");
        issue.push_str(&excerpts);
    }
    // Attachments and their digests, then the receipt.
    let mut attachments = Vec::new();
    if let Some(written) = &bundle {
        if written.get("error").is_none() {
            attachments.push(json!({"name":"attachments/tomacco-report.tar.gz","bytes":written["bytes"],"sha256":written["sha256"],"contents":written["files"]}));
        }
    }
    for (name, bytes) in &files {
        fs::write(directory.join(name), bytes)?;
        attachments.push(json!({"name":name,"bytes":bytes.len(),"sha256":digest(bytes)}));
    }
    issue.push_str("## Attachments\n\n");
    for attachment in &attachments {
        issue.push_str(&format!(
            "- `{}` ({} bytes, sha256 {})\n",
            s(attachment, "name"),
            attachment["bytes"],
            s(attachment, "sha256")
        ));
    }
    if attachments.is_empty() {
        issue.push_str("- none\n");
    }
    issue.push_str("\n_Written by Tomacco on this computer and sent nowhere. Secrets of known shapes are masked; read everything before you share it._\n");
    let paths = absolute_paths(&issue);
    let receipt = json!({
        "secrets_masked":secrets_masked,
        "home_replaced":anonymize,
        "device_ids_masked":table.len(),
        "device_id_placeholders":table.values().collect::<Vec<_>>(),
        "absolute_paths_remaining":paths,
        "files":attachments.iter().map(|a| a["name"].clone()).chain([json!("issue.md"), json!("draft.json"), json!("redaction-receipt.json")]).collect::<Vec<_>>(),
        "bundle":bundle.as_ref().map(|b| b.get("error").cloned().unwrap_or(json!("included"))),
        "sent":false,
        "note":"Masking recognises known shapes of secrets, your home directory and device serials or UDIDs. It cannot know what else is private: read issue.md and the attachments before sharing them."
    });
    atomic_write(&directory.join("issue.md"), issue.as_bytes())?;
    atomic_json(&directory.join("redaction-receipt.json"), &receipt)?;
    let issue_url = format!(
        "https://github.com/{}/{}/issues/new?title={}",
        crate::product::REPO_OWNER,
        crate::product::REPO_NAME,
        urlencode(&title_clean)
    );
    let draft = json!({"directory":directory,"issue_markdown":directory.join("issue.md"),"receipt":receipt,"attachments":attachments,
        "jobs":job_list,"version":version,"issue_url":issue_url,"sent":false,
        "next":"Read issue.md and the attachments. The issue_url opens a new issue with the title only; paste the body and attach the files yourself."});
    atomic_json(&directory.join("draft.json"), &draft)?;
    Ok(draft)
}

fn home_dir() -> Result<PathBuf> {
    home()
}

/// Percent-encodes a short title for a URL query: unreserved characters stay.
fn urlencode(text: &str) -> String {
    let mut out = String::new();
    for byte in text.bytes() {
        if byte.is_ascii_alphanumeric() || b"-_.~".contains(&byte) {
            out.push(byte as char);
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn device_ids_get_stable_placeholders_and_emulators_stay() {
        let mut table = BTreeMap::new();
        let text = "adb -s R5CT30ABCDE shell; udid 00008110-001A2B3C4D5E6F7A on emulator-5554; again R5CT30ABCDE and 0123456789abcdef0123456789abcdef01234567";
        let out = mask_device_ids(text, &mut table);
        assert_eq!(
            out,
            "adb -s [device-3] shell; udid [device-1] on emulator-5554; again [device-3] and [device-2]"
        );
        assert_eq!(table.len(), 3);
        assert_eq!(
            urlencode("Crash on sign-in: 50% fail"),
            "Crash%20on%20sign-in%3A%2050%25%20fail"
        );
        let paths = absolute_paths(
            "saw /tmp/build/app.apk and ~/x and /opt/android-sdk/platform-tools (ok)",
        );
        assert_eq!(
            paths,
            vec!["/opt/android-sdk/platform-tools", "/tmp/build/app.apk"]
        );
    }
    #[test]
    fn scrubbing_counts_secrets_and_replaces_the_home() {
        let mut table = BTreeMap::new();
        let (out, secrets) = scrub(
            "token ghp_0123456789abcdefABCDEF0123 in /home/ana/app with serial R5CT30ABCDE",
            Some("/home/ana"),
            &mut table,
            true,
        );
        assert_eq!(secrets, 1);
        assert!(
            out.contains("[redacted]") && out.contains("~/app") && out.contains("[device-1]"),
            "{out}"
        );
        let (kept, _) = scrub("serial R5CT30ABCDE", None, &mut BTreeMap::new(), false);
        assert!(kept.contains("R5CT30ABCDE"));
    }
}
