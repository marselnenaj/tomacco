//! `workflow.run`: a short list of operations run in order in one call.
//! A step that starts a job is followed until the job ends (or its app is
//! up), and the first step that fails stops the rest. Destructive steps run
//! only with their own `confirm`, so one call cannot hide a deletion.
use crate::{api, core::*};
use serde_json::{Value, json};
use std::time::{Duration, Instant};

/// Operations a workflow does not run: itself, which would nest without end.
const REFUSED: &[&str] = &["workflow.run"];

pub fn run(a: &Value) -> Result {
    run_with(a, api::dispatch)
}

/// The steps in order with `dispatch` running each operation.
fn run_with(a: &Value, dispatch: impl Fn(&str, &Value) -> Result) -> Result {
    let steps = a["steps"].as_array().cloned().unwrap_or_default();
    check(&steps)?;
    let deadline = Instant::now() + Duration::from_secs(n(a, "timeout"));
    let mut done: Vec<Value> = Vec::new();
    for (index, step) in steps.iter().enumerate() {
        let operation = s(step, "operation");
        let started = Instant::now();
        let outcome = step_arguments(step, &done)
            .and_then(|arguments| dispatch(operation, &arguments))
            .and_then(|result| match job_id(&result) {
                Some(id) if step["wait"] != false => follow(&id, deadline, &dispatch),
                _ => Ok(result),
            });
        let mut record = json!({"step":index,"operation":operation});
        match outcome {
            Ok(result) => {
                let failed = job_id(&result).is_some()
                    && ["failed", "cancelled", "timed_out"].contains(&s(&result, "state"));
                record["ok"] = json!(!failed);
                record["result"] = result;
                record["seconds"] = json!(seconds(started));
                done.push(record);
                if failed {
                    let job = &done[index]["result"];
                    let id = s(job, "id");
                    return Err(stopped(
                        &steps,
                        index,
                        format!("its job {id} ended {}", s(job, "state")),
                        format!(
                            "Read the job's log with job.logs {{\"job_id\":\"{id}\"}}, fix the cause and run the workflow again"
                        ),
                        done,
                    ));
                }
            }
            Err(error) => {
                record["ok"] = json!(false);
                record["error"] = error.value();
                record["seconds"] = json!(seconds(started));
                done.push(record);
                let remedy = if error.remedy.is_empty() {
                    "Read the step's error under details.steps".to_string()
                } else {
                    error.remedy.clone()
                };
                return Err(stopped(&steps, index, error.message, remedy, done));
            }
        }
    }
    Ok(json!({"ok":true,"steps":done}))
}

fn seconds(started: Instant) -> f64 {
    (started.elapsed().as_secs_f64() * 10.0).round() / 10.0
}

/// The error a failed step ends the workflow with: every step that ran, and
/// the operations of the steps it skipped.
fn stopped(steps: &[Value], index: usize, why: String, remedy: String, done: Vec<Value>) -> Error {
    let operation = s(&steps[index], "operation");
    let skipped: Vec<&str> = steps[index + 1..]
        .iter()
        .map(|step| s(step, "operation"))
        .collect();
    Error::new(
        "workflow_step_failed",
        format!("Step {index} ({operation}) failed: {why}"),
    )
    .remedy(remedy)
    .details(json!({"failed_step":index,"steps":done,"skipped":skipped}))
}

/// Refuses the whole workflow before anything runs: an unknown or refused
/// operation, a destructive step without `confirm`, a reference to a step
/// that has not run by then, or arguments the operation would refuse.
fn check(steps: &[Value]) -> Result<()> {
    let mut unconfirmed = Vec::new();
    for (index, step) in steps.iter().enumerate() {
        let operation = s(step, "operation");
        if REFUSED.contains(&operation) {
            return Err(Error::new(
                "invalid_argument",
                format!("Step {index}: a workflow cannot run {operation}"),
            )
            .remedy("List that workflow's steps in this one instead"));
        }
        let schema = api::operation(operation).map_err(|error| {
            Error::new(
                "invalid_argument",
                format!("Step {index}: {}", error.message),
            )
            .remedy(error.remedy)
        })?;
        let arguments = step.get("arguments").cloned().unwrap_or(json!({}));
        let publishes = crate::mcp_consent::needs_yes(operation, &arguments);
        if (schema["annotations"]["destructiveHint"] == true || publishes)
            && step["confirm"] != true
        {
            unconfirmed.push(format!("{index} ({operation})"));
        }
        let mut references = Vec::new();
        collect_references(&arguments, &mut references);
        for from in &references {
            if *from >= index {
                return Err(Error::new(
                    "invalid_argument",
                    format!(
                        "Step {index} takes a value from step {from}, which has not run by then"
                    ),
                )
                .remedy("from_step names an earlier step, counting from 0"));
            }
        }
        if references.is_empty() {
            api::arguments(operation, &arguments).map_err(|error| {
                Error::new(
                    &error.code,
                    format!("Step {index} ({operation}): {}", error.message),
                )
                .remedy(error.remedy)
            })?;
        }
    }
    if !unconfirmed.is_empty() {
        return Err(Error::new(
            "workflow_needs_confirmation",
            format!(
                "Destructive and publishing steps need confirm:true: {}",
                unconfirmed.join(", ")
            ),
        )
        .remedy("Tell the person what those steps delete, overwrite or publish, and set confirm:true on each only after they agree"));
    }
    Ok(())
}

/// Whether a value is a reference to an earlier step's result:
/// `{"from_step": N, "pointer": "/json/pointer"}` and nothing else.
fn reference(value: &Value) -> Option<(usize, &str)> {
    let object = value.as_object()?;
    if object.len() != 2 {
        return None;
    }
    Some((
        object.get("from_step")?.as_u64()? as usize,
        object.get("pointer")?.as_str()?,
    ))
}
fn collect_references(value: &Value, found: &mut Vec<usize>) {
    if let Some((step, _)) = reference(value) {
        found.push(step);
        return;
    }
    match value {
        Value::Object(object) => object.values().for_each(|v| collect_references(v, found)),
        Value::Array(items) => items.iter().for_each(|v| collect_references(v, found)),
        _ => {}
    }
}
/// A step's arguments with every reference replaced by the value it points
/// at in that step's record: `/result/...` for its result, which for a
/// followed job is the finished job.
fn step_arguments(step: &Value, done: &[Value]) -> Result {
    fn fill(value: &Value, done: &[Value]) -> Result {
        if let Some((from, pointer)) = reference(value) {
            return done
                .get(from)
                .and_then(|record| record.pointer(pointer))
                .cloned()
                .ok_or_else(|| {
                    Error::new(
                        "invalid_argument",
                        format!("Step {from} has nothing at {pointer}"),
                    )
                    .remedy("Point into that step's record, such as /result/id or /result/artifacts/0/path")
                });
        }
        Ok(match value {
            Value::Object(object) => {
                let mut filled = serde_json::Map::new();
                for (key, inner) in object {
                    filled.insert(key.clone(), fill(inner, done)?);
                }
                Value::Object(filled)
            }
            Value::Array(items) => {
                Value::Array(items.iter().map(|v| fill(v, done)).collect::<Result<_>>()?)
            }
            other => other.clone(),
        })
    }
    fill(&step.get("arguments").cloned().unwrap_or(json!({})), done)
}

/// The ID of the job a result is, when it is one.
fn job_id(result: &Value) -> Option<String> {
    (result.get("plan").is_some() && result["id"].is_string()).then(|| s(result, "id").to_string())
}

/// Follows a job until it ends, or until its app is up (phase `ready`, as a
/// Flutter run that keeps its job is), or until the workflow's time is up.
fn follow(id: &str, deadline: Instant, dispatch: &impl Fn(&str, &Value) -> Result) -> Result {
    loop {
        let left = deadline.saturating_duration_since(Instant::now()).as_secs();
        let job = dispatch("job.wait", &json!({"job_id":id,"seconds":left.clamp(0, 1)}))?;
        let state = s(&job, "state");
        if ["succeeded", "failed", "cancelled", "timed_out"].contains(&state)
            || s(&job, "phase") == "ready"
        {
            return Ok(job);
        }
        if left == 0 {
            return Err(Error::new(
                "workflow_timed_out",
                format!("The workflow's time ran out while job {id} was {state}"),
            )
            .remedy(format!(
                "The job keeps running: follow it with job.wait {{\"job_id\":\"{id}\"}}, or stop it with job.cancel"
            ))
            .details(json!({ "job": job })));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    const JOB: &str = "0123456789abcdef0123456789abcdef";

    #[test]
    fn steps_run_in_order_and_a_job_is_followed_to_its_end() {
        let calls = RefCell::new(Vec::new());
        let waits = RefCell::new(0);
        let dispatch = |operation: &str, arguments: &Value| -> Result {
            calls
                .borrow_mut()
                .push((operation.to_string(), arguments.clone()));
            match operation {
                "project.execute" => Ok(json!({"id":JOB,"plan":{},"state":"queued"})),
                "job.wait" => {
                    *waits.borrow_mut() += 1;
                    let state = if *waits.borrow() < 3 {
                        "running"
                    } else {
                        "succeeded"
                    };
                    Ok(
                        json!({"id":JOB,"plan":{},"state":state,"artifacts":[{"path":"/out/app.apk"}]}),
                    )
                }
                "device.install" => Ok(json!({"installed":arguments["paths"]})),
                _ => Ok(json!({})),
            }
        };
        let done = run_with(
            &json!({"timeout":60,"steps":[
                {"operation":"project.execute","arguments":{"path":"/p","platform":"android"}},
                {"operation":"device.install","confirm":true,"arguments":{"platform":"android","device":"emulator-5554",
                    "paths":[{"from_step":0,"pointer":"/result/artifacts/0/path"}]}},
                {"operation":"device.list"},
            ]}),
            dispatch,
        )
        .unwrap();
        assert_eq!(done["ok"], true);
        let steps = done["steps"].as_array().unwrap();
        assert_eq!(steps.len(), 3);
        assert_eq!(steps[0]["result"]["state"], "succeeded");
        assert_eq!(steps[1]["result"]["installed"], json!(["/out/app.apk"]));
        assert_eq!(*waits.borrow(), 3);
        let names: Vec<String> = calls.borrow().iter().map(|(o, _)| o.clone()).collect();
        assert_eq!(
            names,
            [
                "project.execute",
                "job.wait",
                "job.wait",
                "job.wait",
                "device.install",
                "device.list"
            ]
        );
    }
    #[test]
    fn the_first_failure_stops_the_rest() {
        let ran = RefCell::new(Vec::new());
        let dispatch =
            |operation: &str, _: &Value| -> Result {
                ran.borrow_mut().push(operation.to_string());
                match operation {
                    "project.execute" => Ok(json!({"id":JOB,"plan":{},"state":"queued"})),
                    "job.wait" => Ok(json!({"id":JOB,"plan":{},"state":"failed","exit_code":1})),
                    "device.list" => Err(Error::new("tool_missing", "adb is missing")
                        .remedy("Install platform-tools")),
                    _ => Ok(json!({})),
                }
            };
        let build = json!({"operation":"project.execute","arguments":{"path":"/p"}});
        let list = json!({"operation":"device.list"});
        let error = run_with(&json!({"timeout":60,"steps":[build, list]}), dispatch).unwrap_err();
        assert_eq!(error.code, "workflow_step_failed");
        assert!(error.message.contains("Step 0 (project.execute)"));
        assert!(error.remedy.contains(JOB));
        let details = error.details.unwrap();
        assert_eq!(details["failed_step"], 0);
        assert_eq!(details["steps"][0]["ok"], false);
        assert_eq!(details["skipped"], json!(["device.list"]));
        assert_eq!(ran.borrow().as_slice(), ["project.execute", "job.wait"]);

        let error = run_with(&json!({"timeout":60,"steps":[list, build]}), dispatch).unwrap_err();
        assert_eq!(error.remedy, "Install platform-tools");
        assert_eq!(
            error.details.unwrap()["steps"][0]["error"]["code"],
            "tool_missing"
        );
        // A job not waited for counts as done when it starts.
        let started = json!({"operation":"project.execute","wait":false,"arguments":{"path":"/p"}});
        let done = run_with(&json!({"timeout":60,"steps":[started]}), dispatch).unwrap();
        assert_eq!(done["steps"][0]["result"]["state"], "queued");
    }
    #[test]
    fn nothing_runs_when_a_step_is_wrong_or_destructive_unconfirmed() {
        let dispatch = |_: &str, _: &Value| -> Result { panic!("nothing may run") };
        let refuse =
            |steps: Value| run_with(&json!({"timeout":60,"steps":steps}), dispatch).unwrap_err();
        let error = refuse(
            json!([{"operation":"device.list"},{"operation":"job.cancel","arguments":{"job_id":JOB}}]),
        );
        assert_eq!(error.code, "workflow_needs_confirmation");
        assert!(error.message.contains("1 (job.cancel)"));
        let commit = json!({"credentials":"/k.json","package":"dev.app","edit_id":"e1"});
        let error = refuse(json!([{"operation":"play.commit","arguments":commit}]));
        assert_eq!(error.code, "workflow_needs_confirmation");
        let error = refuse(json!([{"operation":"projct.run"}]));
        assert_eq!(error.code, "invalid_argument");
        assert!(error.remedy.contains("project.run"));
        assert_eq!(
            refuse(json!([{"operation":"workflow.run"}])).code,
            "invalid_argument"
        );
        let error = refuse(json!([{"operation":"project.inspect","arguments":{"pth":"/p"}}]));
        assert!(
            error
                .message
                .starts_with("Step 0 (project.inspect): Unknown argument")
        );
        let error = refuse(
            json!([{"operation":"project.inspect","arguments":{"path":{"from_step":0,"pointer":"/x"}}}]),
        );
        assert!(error.message.contains("has not run by then"));
    }
    #[test]
    fn a_missing_reference_fails_its_step_and_time_running_out_leaves_the_job() {
        let dispatch = |operation: &str, _: &Value| -> Result {
            match operation {
                "project.execute" => Ok(json!({"id":JOB,"plan":{},"state":"queued"})),
                "job.wait" => Ok(json!({"id":JOB,"plan":{},"state":"running"})),
                _ => Ok(json!({"a":1})),
            }
        };
        let steps = json!([{"operation":"device.list"},
            {"operation":"project.inspect","arguments":{"path":{"from_step":0,"pointer":"/result/b"}}}]);
        let error = run_with(&json!({"timeout":60,"steps":steps}), dispatch).unwrap_err();
        assert_eq!(
            error.details.unwrap()["steps"][1]["error"]["code"],
            "invalid_argument"
        );
        let error = run_with(
            &json!({"timeout":0,"steps":[{"operation":"project.execute","arguments":{"path":"/p"}}]}),
            dispatch,
        )
        .unwrap_err();
        assert_eq!(error.code, "workflow_step_failed");
        assert!(error.remedy.contains("job.cancel"));
        // An app that is up counts as done although its job keeps running.
        let up = |operation: &str, _: &Value| -> Result {
            match operation {
                "job.wait" => Ok(json!({"id":JOB,"plan":{},"state":"running","phase":"ready"})),
                _ => Ok(json!({"id":JOB,"plan":{},"state":"queued"})),
            }
        };
        let done = run_with(
            &json!({"timeout":60,"steps":[{"operation":"project.run","arguments":{"path":"/p","platform":"android","device":"emulator-5554"}}]}),
            up,
        )
        .unwrap();
        assert_eq!(done["steps"][0]["result"]["phase"], "ready");
    }
}
