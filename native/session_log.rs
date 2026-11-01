//! An opt-in log of the operations a session calls, and their replay.
//!
//! With TOMACCO_SESSION_LOG set to an absolute file path, every operation
//! called through `tomaccoctl call` or the MCP server is appended to that
//! file as one JSON line: its name, its arguments with recognised secrets
//! masked, whether it worked, a short summary of the result and how long it
//! took. `tomaccoctl replay FILE` calls the operations that changed something
//! again, in order.
use crate::{api, core::*, redact};
use serde_json::{Value, json};
use std::io::Write;

pub const VARIABLE: &str = "TOMACCO_SESSION_LOG";

/// Calls an operation, and logs the call when a session log is set.
pub fn call(name: &str, arguments: &Value) -> Result {
    let file = std::env::var(VARIABLE).unwrap_or_default();
    if file.is_empty() {
        return crate::guard::dispatch(name, arguments);
    }
    let started = now();
    let clock = std::time::Instant::now();
    let result = crate::guard::dispatch(name, arguments);
    let entry = entry(
        name,
        arguments,
        &result,
        started,
        clock.elapsed().as_secs_f64(),
    );
    if let Err(error) = append(&file, &entry) {
        // Standard output carries the answer, and for MCP the protocol.
        eprintln!(
            "tomaccoctl: cannot write the session log {file}: {}",
            error.message
        );
    }
    result
}
fn append(file: &str, entry: &Value) -> Result<()> {
    if !std::path::Path::new(file).is_absolute() {
        return Err(Error::new(
            "invalid_path",
            format!("{VARIABLE} must be an absolute path"),
        ));
    }
    let target = output_path(file)?;
    let mut line = entry.to_string();
    line.push('\n');
    // One write of one line, appended: lines of concurrent calls do not mix.
    private_file(&target, true)?.write_all(line.as_bytes())?;
    Ok(())
}
/// Whether an operation changes something, by its schema; an unknown one
/// is treated as changing.
fn mutating(name: &str) -> bool {
    api::operation(name).map_or(true, |op| op["annotations"]["readOnlyHint"] != true)
}
fn entry(name: &str, arguments: &Value, result: &Result, started: f64, seconds: f64) -> Value {
    let mut arguments = arguments.clone();
    redact::value(&mut arguments);
    let summary = match result {
        Ok(value) => summary(value),
        Err(error) => {
            let mut error = json!({"code":error.code,"message":error.message});
            redact::value(&mut error);
            error
        }
    };
    json!({"time":started,"operation":name,"arguments":arguments,"mutating":mutating(name),
           "ok":result.is_ok(),"duration_seconds":(seconds * 1000.0).round() / 1000.0,
           (if result.is_ok() { "result" } else { "error" }):summary})
}
/// A few words of a result: a job's ID and state, an object's keys, an
/// array's length or a short value; never whole outputs.
fn summary(value: &Value) -> Value {
    match value {
        Value::Object(map) if map.contains_key("id") && map.contains_key("state") => {
            json!({"job_id":map["id"],"state":map["state"],"label":map.get("label")})
        }
        Value::Object(map) => json!({"keys":map.keys().take(20).collect::<Vec<_>>()}),
        Value::Array(items) => json!({"items":items.len()}),
        Value::String(text) if text.len() > 200 => json!({"length":text.len()}),
        other => {
            let mut other = other.clone();
            redact::value(&mut other);
            other
        }
    }
}

/// Why a logged call is not replayed, or None when it is.
fn skipped(entry: &Value) -> Option<&'static str> {
    let name = s(entry, "operation");
    if api::operation(name).is_err() {
        Some("unknown_operation")
    } else if !mutating(name) {
        Some("read_only")
    } else if entry["ok"] != true {
        Some("failed_originally")
    } else if name.starts_with("job.") {
        // A job ID of the original session means nothing now.
        Some("job_reference")
    } else if entry["arguments"].to_string().contains(redact::MASK) {
        Some("redacted")
    } else {
        None
    }
}
/// `tomaccoctl replay FILE [--dry-run]`: calls the logged operations that
/// changed something again, in order, waiting for each job to end, and stops
/// at the first that fails. A dry run only lists what would be called.
pub fn replay(text: &str, dry_run: bool, dispatch: impl Fn(&str, &Value) -> Result) -> Result {
    let mut calls = Vec::new();
    let mut ran = 0;
    for (number, line) in text.lines().enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        let entry: Value = serde_json::from_str(line).map_err(|e| {
            Error::new(
                "invalid_session_log",
                format!("Line {} is not JSON: {e}", number + 1),
            )
        })?;
        let mut call = json!({"line":number + 1,"operation":entry["operation"]});
        if let Some(reason) = skipped(&entry) {
            call["status"] = json!("skipped");
            call["reason"] = json!(reason);
            calls.push(call);
            continue;
        }
        let name = s(&entry, "operation");
        call["arguments"] = entry["arguments"].clone();
        if dry_run {
            call["status"] = json!("would_run");
            calls.push(call);
            continue;
        }
        ran += 1;
        let outcome =
            dispatch(name, &entry["arguments"]).and_then(|value| follow(&value, &dispatch));
        match outcome {
            Ok(value) => {
                call["status"] = json!("ran");
                call["result"] = summary(&value);
                calls.push(call);
            }
            Err(error) => {
                call["status"] = json!("failed");
                call["error"] = error.value();
                calls.push(call);
                return Err(Error::new(
                    "replay_failed",
                    format!("{name} from line {} failed: {}", number + 1, error.message),
                )
                .remedy("Fix the cause and replay the rest, or edit the log")
                .details(json!({"calls":calls,"ran":ran})));
            }
        }
    }
    let skipped = calls.iter().filter(|c| c["status"] == "skipped").count();
    Ok(json!({"dry_run":dry_run,"ran":ran,"skipped":skipped,"calls":calls}))
}
/// Waits for a job an operation started, so the next call finds its work done.
fn follow(value: &Value, dispatch: &impl Fn(&str, &Value) -> Result) -> Result {
    let id = s(value, "id");
    if id.len() != 32 || !value["state"].is_string() || !value["log_path"].is_string() {
        return Ok(value.clone());
    }
    loop {
        let job = dispatch("job.wait", &json!({"job_id":id,"seconds":60}))?;
        let state = s(&job, "state");
        if !crate::jobs::TERMINAL.contains(&state) {
            continue;
        }
        if state == "succeeded" {
            return Ok(job);
        }
        return Err(
            Error::new(&format!("job_{state}"), format!("Job {id} ended {state}"))
                .details(json!({"job":job})),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn a_logged_call_has_masked_arguments_and_a_short_result() {
        let job = json!({"id":"0123456789abcdef0123456789abcdef","state":"queued","label":"Build","log_path":"/x","plan":{"steps":[]}});
        let logged = entry(
            "project.execute",
            &json!({"path":"/p","token":"s3cr3t-value"}),
            &Ok(job),
            1.0,
            0.25,
        );
        assert_eq!(logged["arguments"]["token"], redact::MASK);
        assert_eq!(logged["arguments"]["path"], "/p");
        assert_eq!(logged["mutating"], true);
        assert_eq!(
            logged["result"],
            json!({"job_id":"0123456789abcdef0123456789abcdef","state":"queued","label":"Build"})
        );
        let read = entry("device.list", &json!({}), &Ok(json!([1, 2, 3])), 1.0, 0.1);
        assert_eq!(
            (&read["mutating"], &read["result"]),
            (&json!(false), &json!({"items":3}))
        );
        let failed = entry(
            "project.run",
            &json!({}),
            &Err(Error::new("workspace_busy", "busy")),
            1.0,
            0.1,
        );
        assert_eq!(
            (&failed["ok"], &failed["error"]["code"]),
            (&json!(false), &json!("workspace_busy"))
        );
    }
    #[test]
    fn replay_runs_the_changing_calls_in_order_and_stops_at_a_failure() {
        let line = |name: &str, arguments: Value, ok: bool| {
            entry(
                name,
                &arguments,
                &if ok {
                    Ok(json!({}))
                } else {
                    Err(Error::new("x", "x"))
                },
                1.0,
                0.1,
            )
            .to_string()
        };
        let log = [
            line("device.list", json!({}), true),
            line("settings.configure", json!({"android_sdk":"/sdk"}), true),
            line("project.run", json!({"path":"/p"}), false),
            line(
                "job.cancel",
                json!({"job_id":"0123456789abcdef0123456789abcdef"}),
                true,
            ),
            line(
                "remote.configure",
                json!({"password":"hunter2-secret"}),
                true,
            ),
            line(
                "project.execute",
                json!({"path":"/p","action":"build"}),
                true,
            ),
            String::new(),
        ]
        .join("\n");
        let calls = std::cell::RefCell::new(Vec::new());
        let job = json!({"id":"0123456789abcdef0123456789abcdef","state":"queued","log_path":"/x"});
        let waits = std::cell::Cell::new(0);
        let fake = |name: &str, arguments: &Value| -> Result {
            calls
                .borrow_mut()
                .push((name.to_string(), arguments.clone()));
            match name {
                "project.execute" => Ok(job.clone()),
                "job.wait" => {
                    waits.set(waits.get() + 1);
                    Ok(json!({"state":if waits.get() < 2 {"running"} else {"succeeded"}}))
                }
                _ => Ok(json!({"saved":true})),
            }
        };
        let plan = replay(&log, true, fake).unwrap();
        assert!(calls.borrow().is_empty(), "a dry run calls nothing");
        let statuses: Vec<(&str, &str)> = plan["calls"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| (s(c, "status"), s(c, "reason")))
            .collect();
        assert_eq!(
            statuses,
            [
                ("skipped", "read_only"),
                ("would_run", ""),
                ("skipped", "failed_originally"),
                ("skipped", "job_reference"),
                ("skipped", "redacted"),
                ("would_run", ""),
            ]
        );
        let done = replay(&log, false, fake).unwrap();
        assert_eq!((&done["ran"], &done["skipped"]), (&json!(2), &json!(4)));
        let names: Vec<String> = calls.borrow().iter().map(|(n, _)| n.clone()).collect();
        assert_eq!(
            names,
            [
                "settings.configure",
                "project.execute",
                "job.wait",
                "job.wait"
            ]
        );
        assert_eq!(calls.borrow()[0].1, json!({"android_sdk":"/sdk"}));
        // The first failure stops the replay.
        let error = replay(&log, false, |name: &str, _: &Value| {
            if name == "settings.configure" {
                Err(Error::new("invalid_path", "no SDK"))
            } else {
                panic!("nothing runs after a failure")
            }
        })
        .unwrap_err();
        assert_eq!(error.code, "replay_failed");
        assert_eq!(error.details.unwrap()["calls"][1]["status"], "failed");
        assert_eq!(
            replay("not json", true, fake).unwrap_err().code,
            "invalid_session_log"
        );
    }
}
