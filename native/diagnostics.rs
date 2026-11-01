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
    /// The virtual iOS device's summary and the tail of its guest's log.
    pub virtual_ios: Option<(Value, String)>,
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
virtual-ios.json  The virtual iOS device: this machine's readiness (CPU,\n\
               memory, disk, graphics card), the profile's settings, the\n\
               current status, the engine and guest-tools builds, and how the\n\
               last guest run ended\n\
virtual-ios/guest.log  The end of the last guest run's log\n\
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
    if let Some((summary, log)) = contents.virtual_ios {
        files.push(("virtual-ios.json".into(), scrub_value(summary, home)));
        if !log.is_empty() {
            files.push((
                "virtual-ios/guest.log".into(),
                scrub_text(&log, home).into_bytes(),
            ));
        }
    }
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

/// The settings of a profile that shape a run, without its app list.
fn profile_summary(profile: &Value) -> Value {
    let boot = &profile["boot"];
    let mut picked = serde_json::Map::new();
    for key in [
        "target_page",
        "bti",
        "translation_cache_mib",
        "display_profile",
        "interactive",
        "input_transport",
        "clock_mode",
        "host_cpu_policy",
        "guest_network",
        "boot_args",
        "seconds",
        "usb",
    ] {
        if !boot[key].is_null() {
            picked.insert(key.into(), boot[key].clone());
        }
    }
    json!({"engine":profile["engine"],"graphics":profile["graphics"],"tools":profile["tools"],
        "installs":profile["installs"],"application":profile["application"],
        "applications":profile["applications"].as_array().map_or(0, Vec::len),
        "accel":profile["prepare"]["accel"],"boot":picked,"toolchain":profile["toolchain"]})
}

/// Which engine build this is, from its build-manifest.json.
fn engine_summary(manifest: &Value) -> Value {
    if manifest.is_null() {
        return Value::Null;
    }
    let contract = &manifest["ordered_patch_contract"];
    let count = |family: &str| contract[family].as_array().map_or(0, Vec::len);
    json!({"engine_sha256":manifest["engine_sha256"],"source_commits":manifest["source_commits"],
        "cpu_optimization":manifest["cpu_optimization"],"hardening":manifest["hardening"],
        "experimental_patches":manifest["experimental_patches"],
        "experimental_graphics_patches":manifest["experimental_graphics_patches"],
        "patches":{"engine":count("engine"),"graphics":count("graphics"),"translator":count("translator")},
        "build_elapsed_seconds":manifest["build_elapsed_seconds"],"reproducibility":manifest["reproducibility"]})
}

/// Which guest-tools build this is, from its guest-tools.json.
fn tools_summary(report: &Value) -> Value {
    if report.is_null() {
        return Value::Null;
    }
    let programs: Vec<&str> = report["programs"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|program| program["name"].as_str().or(program["path"].as_str()))
        .collect();
    json!({"state":report["state"],"target":report["target"],"compiler":report["compiler"],
        "agent":report["agent"],"quiet_services":report["quiet_services"],
        "development_signing":!report["development_signing"].is_null(),
        "sources_sha256":digest(report["sources"].to_string().as_bytes()),
        "trustcache_sha256":report["trustcache"]["sha256"],"programs":programs,
        "applications":report["applications"].as_array().map_or(0, Vec::len)})
}

/// How a guest run ended, as run_ios_paravirt.py classified it.
fn run_summary(report: &Value) -> Value {
    let record = &report["report"];
    if record.is_null() {
        return json!({"job_state":report["job_state"],"report":null});
    }
    let evidence: serde_json::Map<String, Value> = record["guest_evidence"]
        .as_object()
        .into_iter()
        .flatten()
        .map(|(key, value)| {
            let shown = match value {
                Value::Array(items) => json!(items.len()),
                Value::Object(map) => json!(map.len()),
                other => other.clone(),
            };
            (key.clone(), shown)
        })
        .collect();
    let first = |key: &str, limit: usize| -> Value {
        json!(
            record[key].as_array().map(|items| items
                .iter()
                .take(limit)
                .cloned()
                .collect::<Vec<_>>())
        )
    };
    json!({"job_state":report["job_state"],"outcome":record["outcome"],"exit_code":record["exit_code"],
        "interrupted":record["interrupted"],"observation_error":record["observation_error"],
        "vram_guard_stop":record["vram_guard_stop"],"guest_evidence":evidence,
        "userspace_failures":first("userspace_failures", 16),"respawn_storms":first("respawn_storms", 16),
        "stop":report["stop"]})
}

/// Reads a small JSON receipt beside an artifact, or null.
fn receipt(path: &Path) -> Value {
    if path.is_file() && fs::metadata(path).is_ok_and(|meta| meta.len() <= 16 * 1024 * 1024) {
        read_json(path, None).unwrap_or(Value::Null)
    } else {
        Value::Null
    }
}

/// The virtual iOS device's part of the bundle: readiness, profile, status,
/// builds and the last run, and the tail of that run's log.
fn virtual_ios(log_limit: u64) -> (Value, String) {
    let host = crate::api::dispatch("ios.virtual.host_check", &json!({}))
        .unwrap_or_else(|error| json!({"error":error.value()}));
    let status = match crate::api::dispatch("ios.virtual.status", &json!({})) {
        Ok(status) => status,
        Err(error) => {
            return (
                json!({"host":host,"status_error":error.value()}),
                String::new(),
            );
        }
    };
    if status["configured"] != true {
        return (json!({"configured":false,"host":host}), String::new());
    }
    let profile = crate::ios_emulator::virtual_phone::directory()
        .and_then(|directory| crate::ios_emulator::virtual_phone::records(&directory))
        .map(|(profile, _)| profile)
        .unwrap_or(Value::Null);
    let engine = Path::new(s(&profile, "engine"));
    let manifest = [engine.parent(), engine.parent().and_then(Path::parent)]
        .into_iter()
        .flatten()
        .map(|directory| receipt(&directory.join("build-manifest.json")))
        .find(|record| record["engine_sha256"].is_string())
        .unwrap_or(Value::Null);
    let tools = receipt(&Path::new(s(&profile, "tools")).join("guest-tools.json"));
    let guest = s(&status, "guest_job").to_string();
    let (run, log) = if guest.is_empty() {
        (Value::Null, String::new())
    } else {
        let run = crate::api::dispatch("ios.emulator.report", &json!({"job_id":guest}))
            .map(|report| run_summary(&report))
            .unwrap_or_else(|error| json!({"error":error.value()}));
        let log = jobs::get(&guest)
            .map(|job| log_tail(Path::new(s(&job, "log_path")), log_limit))
            .unwrap_or_default();
        (run, log)
    };
    (
        json!({"configured":true,"host":host,"profile":profile_summary(&profile),"status":status,
            "engine":engine_summary(&manifest),"guest_tools":tools_summary(&tools),"last_run":run}),
        log,
    )
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
    let virtual_ios = a["virtual_ios"]
        .as_bool()
        .unwrap_or(true)
        .then(|| virtual_ios(log_limit));
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
        virtual_ios,
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
            virtual_ios: Some((
                json!({"configured":true,"profile":{"engine":"/home/ana/phone/engine"},
                    "status":{"error":"token ghp_bbbbbbbbbbbbbbbbbbbbbbbb rejected"}}),
                "boot\nkernel log from /home/ana/phone\n".to_string(),
            )),
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
                "virtual-ios.json",
                "virtual-ios/guest.log",
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
            "ghp_bbbb",
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
    fn the_virtual_ios_summaries_keep_what_explains_a_run() {
        let profile = json!({"engine":"/e/qemu","graphics":"/g","tools":"/t","installs":"/i","application":"app",
            "applications":[{"bundle":"/a.app"}],"prepare":{"accel":"kvm"},
            "boot":{"target_page":"16k","bti":"off","translation_cache_mib":2048,"kernel":"/k","sep":"/s"}});
        let summary = profile_summary(&profile);
        assert_eq!(summary["applications"], 1);
        assert_eq!(
            summary["boot"],
            json!({"target_page":"16k","bti":"off","translation_cache_mib":2048})
        );
        let manifest = json!({"engine_sha256":"ab","source_commits":{"sptm":"c"},"cpu_optimization":"native",
            "hardening":"qemu","ordered_patch_contract":{"engine":[{},{}],"graphics":[{}],"translator":[]},
            "toolchain":{"rustc":{"path":"/usr/bin/rustc"}}});
        let engine = engine_summary(&manifest);
        assert_eq!(
            engine["patches"],
            json!({"engine":2,"graphics":1,"translator":0})
        );
        assert_eq!(engine["engine_sha256"], "ab");
        assert!(engine.get("toolchain").is_none());
        assert_eq!(engine_summary(&Value::Null), Value::Null);
        let tools = tools_summary(&json!({"state":"built","target":"arm64-apple-ios27.0",
            "programs":[{"name":"tomacco-agent"},{"path":"usr/local/bin/x"}],"sources":{"a.c":"1"},
            "development_signing":{"policy":[]},"files":[{"path":"big"}]}));
        assert_eq!(
            tools["programs"],
            json!(["tomacco-agent", "usr/local/bin/x"])
        );
        assert_eq!(tools["development_signing"], true);
        assert!(tools.get("files").is_none());
        let run = run_summary(
            &json!({"job_state":"succeeded","report":{"outcome":"kernel-panic",
            "guest_evidence":{"kernel_panic":true,"device_errors":["a","b"],"bus_faults":[]},
            "userspace_failures":(0..40).map(|n| json!({"n":n})).collect::<Vec<_>>()}}),
        );
        assert_eq!(run["outcome"], "kernel-panic");
        assert_eq!(
            run["guest_evidence"],
            json!({"kernel_panic":true,"device_errors":2,"bus_faults":0})
        );
        assert_eq!(run["userspace_failures"].as_array().unwrap().len(), 16);
        assert_eq!(
            run_summary(&json!({"job_state":"queued","report":null}))["report"],
            Value::Null
        );
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
