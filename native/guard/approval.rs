//! Asks the person at the Tomacco desktop before a destructive call from an
//! MCP agent runs, when `agent_policy.approval.destructive` is on.
//!
//! The question goes over the desktop's preview socket as `approve`, with the
//! operation, the client's name and the arguments, secrets masked. The
//! desktop answers `{"approved": true}` or `{"approved": false, "reason": ...}`.
//! No answer within the timeout, or any answer that is not a yes, is a no.
use crate::core::*;
use serde_json::{Value, json};
use std::{
    io::{BufRead, BufReader, Read, Write},
    os::unix::{fs::FileTypeExt, fs::MetadataExt, net::UnixStream},
    path::Path,
    time::Duration,
};

#[derive(Debug, PartialEq)]
pub enum Answer {
    Approved,
    Denied(String),
    /// No desktop listens on the session.
    NoDesktop,
}

/// The socket of a desktop session, as preview.rs finds it.
pub fn socket(session: &str) -> Result<std::path::PathBuf> {
    identifier(session, "session", Some("[A-Za-z0-9_-]{1,40}"))?;
    Ok(crate::preview::socket_directory()?.join(format!("preview-{session}.sock")))
}

/// Asks on the socket at `path` and waits up to `timeout` for the answer.
pub fn ask(path: &Path, question: &Value, timeout: Duration) -> Answer {
    let Ok(meta) = std::fs::symlink_metadata(path) else {
        return Answer::NoDesktop;
    };
    // SAFETY: getuid has no preconditions.
    if !meta.file_type().is_socket() || meta.uid() != unsafe { libc::getuid() } {
        return Answer::Denied("the desktop socket is not this user's".into());
    }
    let Ok(mut stream) = UnixStream::connect(path) else {
        return Answer::NoDesktop;
    };
    let request = json!({"id":1,"method":"approve","params":question});
    if stream
        .set_write_timeout(Some(Duration::from_secs(5)))
        .and_then(|()| writeln!(stream, "{request}"))
        .is_err()
    {
        return Answer::Denied("the desktop did not take the question".into());
    }
    // A little longer than the desktop's own countdown, which answers no.
    let _ = stream.set_read_timeout(Some(timeout + Duration::from_secs(3)));
    let mut line = Vec::new();
    let read = BufReader::new(stream.take(64 * 1024)).read_until(b'\n', &mut line);
    if read.is_err() || line.last() != Some(&b'\n') {
        return Answer::Denied("no answer in time".into());
    }
    let Ok(reply) = serde_json::from_slice::<Value>(&line) else {
        return Answer::Denied("the desktop's answer was not JSON".into());
    };
    if let Some(error) = reply.get("error") {
        // A desktop from before approval refuses the unknown request.
        return Answer::Denied(format!(
            "the desktop could not ask: {}",
            error["message"].as_str().unwrap_or("error")
        ));
    }
    if reply["result"]["approved"] == true {
        Answer::Approved
    } else {
        Answer::Denied(
            reply["result"]["reason"]
                .as_str()
                .unwrap_or("denied")
                .to_string(),
        )
    }
}

/// The question for a call: what the desktop shows.
pub fn question(operation: &str, client: &str, arguments: &Value, timeout: u64) -> Value {
    let title = crate::api::operation(operation)
        .map(|op| s(op, "title").to_string())
        .unwrap_or_default();
    json!({"operation":operation,"title":title,"client":client,"channel":"mcp",
           "arguments":super::audit::arguments(arguments),"timeout_seconds":timeout})
}

pub fn denied(operation: &str, reason: &str) -> Error {
    Error::new(
        "approval_denied",
        format!("The person at the Tomacco desktop did not approve {operation}: {reason}"),
    )
    .remedy("Do not repeat the call on your own. Tell the person what you wanted to do and why, and let them run it or approve it when you ask again")
    .details(json!({"operation":operation,"reason":reason}))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::net::UnixListener;
    /// A desktop that answers one question with `reply`, or never.
    fn desktop(path: &Path, reply: Option<Value>) -> std::thread::JoinHandle<Value> {
        let listener = UnixListener::bind(path).unwrap();
        std::thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            let request: Value = serde_json::from_str(&line).unwrap();
            match reply {
                Some(reply) => writeln!(&stream, "{reply}").unwrap(),
                None => std::thread::sleep(Duration::from_millis(3500)),
            }
            request
        })
    }
    #[test]
    fn a_yes_a_no_silence_and_no_desktop() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("preview-main.sock");
        let q = question(
            "files.write",
            "agent",
            &json!({"path":"/p","token":"abc123secret"}),
            30,
        );
        assert_eq!(q["title"], "Save source file");
        assert_eq!(q["arguments"]["token"], crate::redact::MASK);
        assert_eq!(ask(&path, &q, Duration::from_secs(1)), Answer::NoDesktop);

        let served = desktop(&path, Some(json!({"id":1,"result":{"approved":true}})));
        assert_eq!(ask(&path, &q, Duration::from_secs(5)), Answer::Approved);
        let request = served.join().unwrap();
        assert_eq!(request["method"], "approve");
        assert_eq!(request["params"]["operation"], "files.write");
        std::fs::remove_file(&path).unwrap();

        let served = desktop(
            &path,
            Some(json!({"id":1,"result":{"approved":false,"reason":"timeout"}})),
        );
        assert_eq!(
            ask(&path, &q, Duration::from_secs(5)),
            Answer::Denied("timeout".into())
        );
        served.join().unwrap();
        std::fs::remove_file(&path).unwrap();

        let served = desktop(
            &path,
            Some(
                json!({"id":1,"error":{"code":"preview_error","message":"Unknown preview operation: approve"}}),
            ),
        );
        assert!(
            matches!(ask(&path, &q, Duration::from_secs(5)), Answer::Denied(r) if r.contains("Unknown preview operation"))
        );
        served.join().unwrap();
        std::fs::remove_file(&path).unwrap();

        // Silence: the read times out (timeout + 3 s) and that is a no.
        let served = desktop(&path, None);
        let started = std::time::Instant::now();
        assert_eq!(
            ask(&path, &q, Duration::from_millis(0)),
            Answer::Denied("no answer in time".into())
        );
        assert!(started.elapsed() < Duration::from_secs(5));
        served.join().unwrap();
        std::fs::remove_file(&path).unwrap();

        // A socket nobody listens on any more is no desktop.
        drop(UnixListener::bind(&path).unwrap());
        assert_eq!(ask(&path, &q, Duration::from_secs(1)), Answer::NoDesktop);
        let error = denied("files.write", "timeout");
        assert_eq!(error.code, "approval_denied");
    }
}
