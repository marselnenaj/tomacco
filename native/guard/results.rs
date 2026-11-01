//! Results too large for an MCP answer: saved owner-only under
//! DATA/results/ID.json and read back in pieces with `result.read`. Saved
//! results older than a week are removed when the next one is saved.
use crate::core::*;
use serde_json::{Value, json};
use std::{
    fs,
    io::{Read, Seek, SeekFrom},
    path::{Path, PathBuf},
    time::{Duration, SystemTime},
};

const KEEP: Duration = Duration::from_secs(7 * 24 * 3600);

fn directory() -> Result<PathBuf> {
    Ok(home()?.join("results"))
}

/// The value itself when its JSON fits in `limit` bytes; otherwise it is
/// saved and the answer is a `result_too_large` error that says where.
pub fn inline_or_saved(operation: &str, value: Value, limit: usize) -> Result {
    let text = value.to_string();
    if text.len() <= limit {
        return Ok(value);
    }
    let directory = directory()?;
    let id = save(&directory, &text)?;
    Err(Error::new(
        "result_too_large",
        format!(
            "{operation} ran; its result has {} bytes, more than the {} KiB an MCP answer may carry (agent_policy.limits.inline_result_kib). It was saved as {id}",
            text.len(),
            limit / 1024
        ),
    )
    .remedy(format!(
        "Do not call {operation} again for it: read the saved result in pieces with result.read {{\"result_id\":\"{id}\",\"offset\":0}}, passing next_offset back as offset, or narrow the call with a filter or limit"
    ))
    .details(json!({"result_id":id,"bytes":text.len(),"path":directory.join(format!("{id}.json"))})))
}

fn save(directory: &Path, text: &str) -> Result<String> {
    private_dir(directory)?;
    if let Ok(entries) = fs::read_dir(directory) {
        let now = SystemTime::now();
        for entry in entries.flatten() {
            let old = entry
                .metadata()
                .and_then(|m| m.modified())
                .is_ok_and(|t| now.duration_since(t).unwrap_or_default() > KEEP);
            if old {
                let _ = fs::remove_file(entry.path());
            }
        }
    }
    let id = uuid::Uuid::new_v4().simple().to_string();
    atomic_write(&directory.join(format!("{id}.json")), text.as_bytes())?;
    Ok(id)
}

/// `result.read`: up to `limit` bytes of a saved result from `offset`,
/// ending on a whole character.
pub fn read(a: &Value) -> Result {
    read_in(&directory()?, a)
}

fn read_in(directory: &Path, a: &Value) -> Result {
    let id = identifier(s(a, "result_id"), "result_id", Some("[a-f0-9]{32}"))?;
    let file = directory.join(format!("{id}.json"));
    let mut handle = fs::File::open(&file).map_err(|_| {
        Error::new("result_not_found", format!("No saved result {id}"))
            .remedy("Saved results are kept for a week; call the operation again")
    })?;
    let size = handle.metadata()?.len();
    let offset = a["offset"].as_u64().unwrap_or(0).min(size);
    let limit = a["limit"].as_u64().unwrap_or(256 * 1024);
    handle.seek(SeekFrom::Start(offset))?;
    let mut bytes = Vec::new();
    handle.take(limit).read_to_end(&mut bytes)?;
    // Stop before a character the piece would cut.
    let end = match std::str::from_utf8(&bytes) {
        Err(error) if error.error_len().is_none() && error.valid_up_to() > 0 => error.valid_up_to(),
        _ => bytes.len(),
    };
    let text = String::from_utf8_lossy(&bytes[..end]).into_owned();
    let next = offset + end as u64;
    Ok(
        json!({"result_id":id,"bytes":size,"offset":offset,"next_offset":next,"text":text,"eof":next >= size}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn a_large_result_is_saved_and_read_back_whole_in_pieces() {
        let temp = tempfile::tempdir().unwrap();
        let value = json!({"names":(0..200).map(|i| format!("é{i}")).collect::<Vec<_>>()});
        let text = value.to_string();
        let id = save(temp.path(), &text).unwrap();
        use std::os::unix::fs::PermissionsExt;
        let file = temp.path().join(format!("{id}.json"));
        assert_eq!(
            fs::metadata(&file).unwrap().permissions().mode() & 0o777,
            0o600
        );
        let mut whole = String::new();
        let mut offset = 0;
        loop {
            let piece = read_in(
                temp.path(),
                &json!({"result_id":id,"offset":offset,"limit":101}),
            )
            .unwrap();
            whole.push_str(s(&piece, "text"));
            offset = piece["next_offset"].as_u64().unwrap();
            if piece["eof"] == true {
                break;
            }
        }
        assert_eq!(whole, text);
        assert_eq!(
            read_in(temp.path(), &json!({"result_id":"0".repeat(32)}))
                .unwrap_err()
                .code,
            "result_not_found"
        );
        assert!(read_in(temp.path(), &json!({"result_id":"../x"})).is_err());
    }
    #[test]
    fn a_small_result_stays_inline() {
        let value = json!({"a":1});
        assert_eq!(
            inline_or_saved("doctor", value.clone(), 1024).unwrap(),
            value
        );
    }
}
