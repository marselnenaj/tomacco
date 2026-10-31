//! `diagnostics.bundle` and `tomaccoctl report`: one .tar.gz for a bug
//! report, written locally and sent nowhere.
//!
//! It holds the version, a host summary, doctor, the settings, and the
//! records, timelines and log tails of recent jobs that did not succeed.
//! Everything passes through the secret redaction first; job environments
//! keep only their variable names, and the home directory becomes `~`. The
//! redaction masks known shapes of secrets, so the person should still look
//! through the bundle before sharing it.
use crate::{core::*, jobs, redact};
use serde_json::{Value, json};
use std::{
    fs::{self, File},
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
};

const ENDED_BADLY: &[&str] = &["failed", "timed_out", "cancelled"];

/// What goes into a bundle, before it is scrubbed.
pub struct Contents {
    pub version: Value,
    pub host: Value,
    pub doctor: Value,
    pub settings: Value,
    /// Each job record with its events, and the tail of its log.
    pub jobs: Vec<(Value, String)>,
    /// Replaced by `~` everywhere.
    pub home: Option<String>,
}

fn read_text(path: &str) -> String {
    fs::read_to_string(path)
        .map(|text| text.trim().to_string())
        .unwrap_or_default()
}

fn host() -> Value {
    let os = read_text("/etc/os-release")
        .lines()
        .find_map(|line| line.strip_prefix("PRETTY_NAME="))
        .map(|name| name.trim_matches('"').to_string())
        .unwrap_or_default();
    let memory_kib = read_text("/proc/meminfo")
        .lines()
        .find_map(|line| line.strip_prefix("MemTotal:"))
        .and_then(|value| value.split_whitespace().next()?.parse::<u64>().ok());
    json!({
        "os": os,
        "kernel": read_text("/proc/sys/kernel/osrelease"),
        "architecture": std::env::consts::ARCH,
        "cpus": std::thread::available_parallelism().map(|n| n.get()).unwrap_or(0),
        "memory_bytes": memory_kib.map(|kib| kib * 1024),
        "kvm": Path::new("/dev/kvm").exists(),
        "session": std::env::var("XDG_SESSION_TYPE").unwrap_or_default(),
        "desktop": std::env::var("XDG_CURRENT_DESKTOP").unwrap_or_default(),
    })
}

/// The last `limit` bytes of a log, from the first whole line.
pub fn log_tail(path: &Path, limit: u64) -> String {
    let Ok(mut file) = File::open(path) else {
        return String::new();
    };
    let size = file.metadata().map(|m| m.len()).unwrap_or(0);
    let start = size.saturating_sub(limit);
    if file.seek(SeekFrom::Start(start)).is_err() {
        return String::new();
    }
    let mut bytes = Vec::new();
    let _ = file.take(limit).read_to_end(&mut bytes);
    let text = String::from_utf8_lossy(&bytes).into_owned();
    match (start > 0, text.find('\n')) {
        (true, Some(end)) => text[end + 1..].to_string(),
        _ => text,
    }
}

/// Environment values are dropped and only their names kept: an environment
/// can hold a secret under any name.
fn without_environments(value: &mut Value) {
    match value {
        Value::Object(map) => {
            for (key, item) in map.iter_mut() {
                if key == "env" && item.is_object() {
                    let names: Vec<String> = item.as_object().unwrap().keys().cloned().collect();
                    *item = json!({ "names": names });
                } else {
                    without_environments(item);
                }
            }
        }
        Value::Array(items) => items.iter_mut().for_each(without_environments),
        _ => (),
    }
}

fn scrub_text(text: &str, home: Option<&str>) -> String {
    let text = redact::text(text).into_owned();
    match home.filter(|home| home.len() > 1) {
        Some(home) => text.replace(home, "~"),
        None => text,
    }
}

fn scrub_value(mut value: Value, home: Option<&str>) -> Vec<u8> {
    without_environments(&mut value);
    redact::value(&mut value);
    let text = serde_json::to_string_pretty(&value).unwrap_or_default();
    scrub_text(&text, home).into_bytes()
}

const README: &str = "Tomacco diagnostics bundle\n\n\
This archive was written on this computer by `tomaccoctl report` (or the\n\
diagnostics.bundle operation). Nothing was sent anywhere.\n\n\
version.json   Tomacco version and executable\n\
host.json      Operating system, kernel, CPUs, memory, KVM, desktop session\n\
doctor.json    The doctor report: tool versions, paths and readiness\n\
settings.json  Configured paths and options\n\
jobs/          Recent jobs that failed, timed out or were cancelled: the\n\
               record with its timeline (ID.json) and the end of its log\n\
               (ID.log)\n\n\
Recognised secrets are masked as [redacted], job environments keep only\n\
the names of their variables, and your home directory is shown as ~.\n\
Masking recognises known shapes of secrets only: read the files before\n\
you share them.\n";

/// The files of a bundle, scrubbed, as (name, bytes).
pub fn files(contents: Contents) -> Vec<(String, Vec<u8>)> {
    let home = contents.home.as_deref();
    let mut files = vec![
        ("README.txt".to_string(), README.as_bytes().to_vec()),
        ("version.json".into(), scrub_value(contents.version, home)),
        ("host.json".into(), scrub_value(contents.host, home)),
        ("doctor.json".into(), scrub_value(contents.doctor, home)),
        ("settings.json".into(), scrub_value(contents.settings, home)),
    ];
    for (job, log) in contents.jobs {
        let id = s(&job, "id").to_string();
        if identifier(&id, "job ID", Some("[0-9a-f]{32}")).is_err() {
            continue;
        }
        files.push((format!("jobs/{id}.json"), scrub_value(job, home)));
        files.push((
            format!("jobs/{id}.log"),
            scrub_text(&log, home).into_bytes(),
        ));
    }
    files
}

/// Writes the files as an owner-only .tar.gz at `output`, replacing it only
/// once complete.
pub fn write(output: &Path, files: &[(String, Vec<u8>)]) -> Result<()> {
    let name = output
        .file_name()
        .ok_or_else(|| Error::new("invalid_path", "Select a file destination"))?;
    let mut partial_name = name.to_os_string();
    partial_name.push(".part");
    let partial = output.with_file_name(partial_name);
    let result = (|| -> Result<()> {
        let file = private_file(&partial, false)?;
        let encoder = flate2::write::GzEncoder::new(file, flate2::Compression::default());
        let mut archive = tar::Builder::new(encoder);
        let time = now() as u64;
        for (name, bytes) in files {
            let mut header = tar::Header::new_gnu();
            header.set_size(bytes.len() as u64);
            header.set_mode(0o600);
            header.set_mtime(time);
            header.set_entry_type(tar::EntryType::Regular);
            header.set_cksum();
            archive.append_data(&mut header, format!("tomacco-report/{name}"), &bytes[..])?;
        }
        let mut file = archive.into_inner()?.finish()?;
        file.flush()?;
        file.sync_all()?;
        fs::rename(&partial, output)?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&partial);
    }
    result
}

fn default_output() -> Result<PathBuf> {
    let directory = home()?.join("diagnostics");
    private_dir(&directory)?;
    Ok(directory.join(format!("tomacco-report-{}.tar.gz", now() as u64)))
}

pub fn bundle(a: &Value) -> Result {
    let output = if s(a, "path").is_empty() {
        default_output()?
    } else {
        output_path(s(a, "path"))?
    };
    let doctor = if a["doctor"].as_bool().unwrap_or(true) {
        crate::api::dispatch("doctor", &json!({}))
            .unwrap_or_else(|error| json!({"error":error.value()}))
    } else {
        json!({"skipped":true})
    };
    let settings =
        crate::toolchain::settings().unwrap_or_else(|error| json!({"error":error.value()}));
    let count = a["jobs"].as_u64().unwrap_or(10);
    let log_limit = a["log_kb"].as_u64().unwrap_or(64) * 1024;
    let mut recent = Vec::new();
    if count > 0 {
        let listed = jobs::list(&json!({"state":ENDED_BADLY,"limit":count}))?;
        for mut job in listed.as_array().cloned().unwrap_or_default() {
            let events = jobs::events(s(&job, "id"), 0, 1000)
                .map(|timeline| timeline["events"].clone())
                .unwrap_or(Value::Null);
            job["events"] = events;
            let log = log_tail(Path::new(s(&job, "log_path")), log_limit);
            recent.push((job, log));
        }
    }
    let job_ids: Vec<String> = recent
        .iter()
        .map(|(job, _)| s(job, "id").to_string())
        .collect();
    let anonymize = a["anonymize_home"].as_bool().unwrap_or(true);
    let files = files(Contents {
        version: json!({
            "product": crate::product::PRODUCT_NAME,
            "version": crate::update::current_version(),
            "executable": own_executable().ok(),
            "created": now(),
        }),
        host: host(),
        doctor,
        settings,
        jobs: recent,
        home: anonymize.then(|| user_home().to_string_lossy().into_owned()),
    });
    write(&output, &files)?;
    let bytes = fs::read(&output)?;
    Ok(json!({
        "path": output,
        "bytes": bytes.len(),
        "sha256": digest(&bytes),
        "files": files.iter().map(|(name, _)| name).collect::<Vec<_>>(),
        "jobs": job_ids,
        "sent": false,
        "note": "Written on this computer only. Secrets of known shapes are masked; read it before you share it.",
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn contents() -> Contents {
        Contents {
            version: json!({"version":"1.2.3","executable":"/home/ana/.local/lib/tomacco/tomaccoctl"}),
            host: json!({"os":"Example Linux"}),
            doctor: json!({"tools":[{"name":"adb","path":"/home/ana/Android/Sdk/platform-tools/adb"}]}),
            settings: json!({"android_sdk":"/home/ana/Android/Sdk","updates":{"token_file":"/home/ana/.config/token"},"api_key":"plain-secret"}),
            jobs: vec![(
                json!({"id":"a".repeat(32),"state":"failed",
                    "plan":{"steps":[{"argv":["curl","-H","Authorization: Bearer abcdefgh12345"]}],
                        "env":{"PATH":"/usr/bin","MY_SERVICE_CREDENTIAL":"hunter2hunter2"}},
                    "events":[{"kind":"state","value":"failed"}]}),
                "upload\nusing ghp_aaaaaaaaaaaaaaaaaaaaaaaa from /home/ana/app\n".to_string(),
            )],
            home: Some("/home/ana".into()),
        }
    }

    #[test]
    fn a_bundle_masks_secrets_drops_environment_values_and_hides_the_home_directory() {
        let files = files(contents());
        let names: Vec<&str> = files.iter().map(|(n, _)| n.as_str()).collect();
        let id = "a".repeat(32);
        assert_eq!(
            names,
            [
                "README.txt",
                "version.json",
                "host.json",
                "doctor.json",
                "settings.json",
                &format!("jobs/{id}.json"),
                &format!("jobs/{id}.log")
            ]
        );
        let all: String = files
            .iter()
            .map(|(_, bytes)| String::from_utf8_lossy(bytes).into_owned())
            .collect();
        for secret in [
            "plain-secret",
            "abcdefgh12345",
            "hunter2hunter2",
            "ghp_aaaa",
            "/home/ana",
        ] {
            assert!(!all.contains(secret), "{secret} leaked");
        }
        assert!(all.contains("~/Android/Sdk"));
        assert!(all.contains("MY_SERVICE_CREDENTIAL"), "variable names stay");
        assert!(all.contains("[redacted]"));
        assert!(all.contains("\"value\": \"failed\""), "timeline kept");
    }

    #[test]
    fn the_archive_is_owner_only_and_holds_every_file() {
        let root = tempfile::tempdir().unwrap();
        let output = root.path().join("report.tar.gz");
        let files = files(contents());
        write(&output, &files).unwrap();
        assert_eq!(
            fs::metadata(&output).unwrap().permissions().mode() & 0o077,
            0
        );
        assert!(!root.path().join("report.tar.gz.part").exists());
        let mut archive =
            tar::Archive::new(flate2::read::GzDecoder::new(File::open(&output).unwrap()));
        let mut read = Vec::new();
        for entry in archive.entries().unwrap() {
            let mut entry = entry.unwrap();
            let name = entry.path().unwrap().to_string_lossy().into_owned();
            let mut bytes = Vec::new();
            entry.read_to_end(&mut bytes).unwrap();
            read.push((
                name.strip_prefix("tomacco-report/").unwrap().to_string(),
                bytes,
            ));
        }
        assert_eq!(read, files);
    }

    #[test]
    fn a_log_tail_starts_at_a_whole_line() {
        let root = tempfile::tempdir().unwrap();
        let log = root.path().join("job.log");
        fs::write(&log, "first line\nsecond line\nthird\n").unwrap();
        assert_eq!(log_tail(&log, 1000), "first line\nsecond line\nthird\n");
        assert_eq!(log_tail(&log, 15), "third\n");
        assert_eq!(log_tail(&root.path().join("missing"), 10), "");
    }
}
