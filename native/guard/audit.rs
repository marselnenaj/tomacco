//! The audit log: one JSON line for every call through `tomaccoctl call` or
//! the MCP server that may change something, and for every call a guard
//! refused, in DATA/audit/audit.jsonl (owner-only). It records who called
//! (the channel and the client's name from MCP initialize), the operation,
//! its arguments with recognised secrets masked and long texts shortened,
//! how it ended and when. Past 4 MiB the file moves to audit.1.jsonl and
//! older files move up; audit.4.jsonl is the oldest kept. `audit.list` reads
//! it, newest first.
use crate::{core::*, redact};
use serde_json::{Value, json};
use std::{
    fs,
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
};

const FILE: &str = "audit.jsonl";
const ROTATE_AT: u64 = 4 * 1024 * 1024;
const KEPT: usize = 4;
/// A string argument longer than this is recorded by its length and digest.
const LONG_TEXT: usize = 512;

pub fn directory() -> Result<PathBuf> {
    Ok(home()?.join("audit"))
}

/// Arguments as the log keeps them: secrets masked, and long strings and
/// every `text` (a saved file's contents, text typed on a device) replaced by
/// their length and SHA-256.
pub fn arguments(arguments: &Value) -> Value {
    fn summary(text: &str) -> Value {
        json!(format!(
            "[{} bytes, sha256 {}]",
            text.len(),
            digest(text.as_bytes())
        ))
    }
    fn shorten(value: &mut Value) {
        match value {
            Value::String(text) if text.len() > LONG_TEXT => *value = summary(text),
            Value::Array(items) => items.iter_mut().for_each(shorten),
            Value::Object(map) => {
                for (key, item) in map.iter_mut() {
                    match item {
                        Value::String(text) if key == "text" => *item = summary(text),
                        _ => shorten(item),
                    }
                }
            }
            _ => (),
        }
    }
    let mut kept = arguments.clone();
    redact::value(&mut kept);
    shorten(&mut kept);
    kept
}

/// Appends one entry under the log's lock, moving full files up first.
pub fn append(entry: &Value) -> Result<()> {
    append_in(&directory()?, entry, ROTATE_AT)
}

fn numbered(directory: &Path, index: usize) -> PathBuf {
    if index == 0 {
        directory.join(FILE)
    } else {
        directory.join(format!("audit.{index}.jsonl"))
    }
}

fn append_in(directory: &Path, entry: &Value, rotate_at: u64) -> Result<()> {
    private_dir(directory)?;
    let lock = Lock::open(&directory.join("audit.lock"))?;
    lock.acquire(false)?;
    let current = numbered(directory, 0);
    if fs::symlink_metadata(&current).is_ok_and(|m| m.len() >= rotate_at) {
        for index in (0..KEPT).rev() {
            let from = numbered(directory, index);
            if from.exists() {
                fs::rename(&from, numbered(directory, index + 1))?;
            }
        }
    }
    let mut line = entry.to_string();
    line.push('\n');
    private_file(&current, true)?.write_all(line.as_bytes())?;
    Ok(())
}

/// The files of the log, newest first.
fn files(directory: &Path) -> Vec<PathBuf> {
    (0..=KEPT)
        .map(|index| numbered(directory, index))
        .filter(|p| p.is_file())
        .collect()
}

/// Whether an entry passes `audit.list`'s filters.
fn wanted(entry: &Value, a: &Value) -> bool {
    let text = |key: &str| a[key].as_str().filter(|v| !v.is_empty());
    if let Some(prefix) = text("operation") {
        if !s(entry, "operation").starts_with(prefix) {
            return false;
        }
    }
    if text("channel").is_some_and(|c| c != s(entry, "channel")) {
        return false;
    }
    if text("client").is_some_and(|c| c != s(entry, "client")) {
        return false;
    }
    if a["failed"] == true && entry["ok"] == true {
        return false;
    }
    if let Some(since) = a["since"].as_f64() {
        if entry["time"].as_f64().unwrap_or(0.0) < since {
            return false;
        }
    }
    true
}

/// The lines of one file, newest first; reads at most its last 8 MiB.
fn lines_backwards(file: &Path) -> Result<Vec<String>> {
    let mut handle = fs::File::open(file)?;
    let size = handle.metadata()?.len();
    let start = size.saturating_sub(8 * 1024 * 1024);
    handle.seek(SeekFrom::Start(start))?;
    let mut bytes = Vec::new();
    handle.read_to_end(&mut bytes)?;
    let text = String::from_utf8_lossy(&bytes);
    let mut lines: Vec<String> = text.lines().map(str::to_string).collect();
    if start > 0 && !lines.is_empty() {
        // The first line was cut by the start of the read.
        lines.remove(0);
    }
    lines.reverse();
    Ok(lines)
}

/// `audit.list`: the newest entries that pass the filters.
pub fn list(a: &Value) -> Result {
    list_in(&directory()?, a)
}

fn list_in(directory: &Path, a: &Value) -> Result {
    let limit = a["limit"].as_u64().unwrap_or(50) as usize;
    let mut entries = Vec::new();
    let mut more = false;
    'files: for file in files(directory) {
        for line in lines_backwards(&file)? {
            let Ok(entry) = serde_json::from_str::<Value>(&line) else {
                continue;
            };
            if !wanted(&entry, a) {
                continue;
            }
            if entries.len() == limit {
                more = true;
                break 'files;
            }
            entries.push(entry);
        }
    }
    Ok(
        json!({"entries":entries,"more":more,"path":directory.join(FILE),"files":files(directory).len()}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    #[test]
    fn entries_are_owner_only_masked_shortened_and_rotated() {
        let temp = tempfile::tempdir().unwrap();
        let directory = temp.path().join("audit");
        let kept = arguments(
            &json!({"path":"/p","token":"s3cr3t","text":"x".repeat(2000),"steps":[{"password":"pw"}]}),
        );
        assert_eq!(kept["token"], redact::MASK);
        assert_eq!(kept["steps"][0]["password"], redact::MASK);
        assert!(
            kept["text"]
                .as_str()
                .unwrap()
                .starts_with("[2000 bytes, sha256 ")
        );
        assert_eq!(kept["path"], "/p");
        let typed = arguments(&json!({"action":"text","text":"pin 1234"}));
        assert!(
            typed["text"]
                .as_str()
                .unwrap()
                .starts_with("[8 bytes, sha256 ")
        );
        assert_eq!(typed["action"], "text");
        for i in 0..30 {
            let entry = json!({"time":i as f64,"channel":if i % 2 == 0 {"mcp"} else {"cli"},"client":"test",
                               "operation":if i % 3 == 0 {"files.write"} else {"project.run"},"ok":i % 5 != 0});
            append_in(&directory, &entry, 300).unwrap();
        }
        let current = directory.join(FILE);
        assert_eq!(
            fs::metadata(&directory).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(
            fs::metadata(&current).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert!(fs::metadata(&current).unwrap().len() < 300 + 200);
        assert_eq!(files(&directory).len(), KEPT + 1);
        assert!(!directory.join("audit.5.jsonl").exists());
        let all = list_in(&directory, &json!({"limit":1000})).unwrap();
        let times: Vec<f64> = all["entries"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e["time"].as_f64().unwrap())
            .collect();
        assert_eq!(times.first(), Some(&29.0));
        assert!(times.windows(2).all(|w| w[0] > w[1]), "{times:?}");
        assert_eq!(all["more"], false);
        let some = list_in(
            &directory,
            &json!({"limit":2,"operation":"files.","channel":"mcp","failed":true}),
        )
        .unwrap();
        let entries = some["entries"].as_array().unwrap();
        assert!(
            entries.iter().all(|e| e["operation"] == "files.write"
                && e["channel"] == "mcp"
                && e["ok"] == false)
        );
        let since = list_in(&directory, &json!({"limit":100,"since":27.0})).unwrap();
        assert_eq!(since["entries"].as_array().unwrap().len(), 3);
        let one = list_in(&directory, &json!({"limit":1})).unwrap();
        assert_eq!(one["more"], true);
        assert_eq!(
            list_in(&temp.path().join("none"), &json!({})).unwrap()["entries"],
            json!([])
        );
    }
}
