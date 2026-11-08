//! `runtime.actions`: an ordered batch of inputs and waits on one session,
//! with a result per step, a bounded deadline and explicit mutation
//! boundaries, so an agent types into a field only once it has the focus,
//! learns which steps were applied when one fails, and never replays them
//! blindly.
//!
//! Each step is sent through the session's control socket as `runtime.input`
//! would send it; waits and mark taps go through the same screen operations
//! an agent calls one by one. A step's status is `applied` (the transport
//! accepted it), `verified` (its `expect` postcondition was seen on the
//! screen, or a `fill` with `verify_value` read its exact value back),
//! `uncertain` (a verified fill sent text whose effect could not be
//! established; it is never sent again), `skipped` (after a failure) or
//! `failed`.
use super::*;
use crate::screen::{self, Session, Store};
use crate::screen_ocr::Tesseract;

/// Steps that change the device, as against waits and looks.
pub(crate) fn mutates(action: &str) -> bool {
    matches!(
        action,
        "tap" | "swipe" | "key" | "text" | "fill" | "pinch" | "rotate" | "gesture"
    )
}

fn invalid(message: impl Into<String>) -> Error {
    Error::new("invalid_argument", message)
}

/// Checks every step before anything is sent: a wrong step fails the
/// whole batch with nothing applied.
pub(crate) fn check(steps: &[Value]) -> Result<()> {
    for (i, step) in steps.iter().enumerate() {
        let action = s(step, "action");
        let needs: &[&str] = match action {
            "tap" if step.get("mark").is_some() => &[],
            "tap" => &["x", "y"],
            "swipe" => &["x", "y", "x2", "y2"],
            "key" => &["key"],
            "text" => &["text"],
            "fill" => &["x", "y", "text"],
            "pinch" => &["x", "y", "scale"],
            "rotate" => &["orientation"],
            "gesture" => &["contacts"],
            "wait" => &["ms"],
            "wait_text" | "wait_text_gone" => &["text"],
            "wait_stable" | "observe" => &[],
            other => return Err(invalid(format!("Step {i}: unknown action {other:?}"))),
        };
        for key in needs {
            if step.get(key).is_none() {
                return Err(invalid(format!("Step {i}: {action} needs {key}")));
            }
        }
        if action == "gesture" {
            super::check_gesture(step).map_err(|e| invalid(format!("Step {i}: {}", e.message)))?;
        }
        if action == "fill"
            || step.get("verify_value").is_some()
            || step.get("submit").is_some()
            || step.get("field_name").is_some()
        {
            // focus_timeout_ms is also a text step's; only a verified fill bounds it at 10 s.
            let mut fill = step.clone();
            if !b(step, "verify_value") {
                if let Some(fill) = fill.as_object_mut() {
                    fill.remove("focus_timeout_ms");
                }
            }
            super::field::check(&fill).map_err(|e| invalid(format!("Step {i}: {}", e.message)))?;
        }
        if let Some(expect) = step.get("expect") {
            if !expect.is_object()
                || (expect.get("text").is_none() && expect.get("text_gone").is_none())
            {
                return Err(invalid(format!("Step {i}: expect names text or text_gone")));
            }
        }
    }
    Ok(())
}

fn ms(value: &Value, key: &str, default: u64, max: u64) -> u64 {
    value[key].as_u64().unwrap_or(default).min(max)
}

/// The error a batch answers: what ran, what was applied, what was skipped.
fn stopped(error: Error, remedy: &str, done: &[Value], total: usize) -> Error {
    let applied: Vec<u64> = done
        .iter()
        .filter(|r| r["mutated"] == true && matches!(s(r, "status"), "applied" | "verified"))
        .map(|r| n(r, "index"))
        .collect();
    // Sent, with an effect nobody could establish: neither applied nor not.
    let uncertain: Vec<u64> = done
        .iter()
        .filter(|r| s(r, "status") == "uncertain")
        .map(|r| n(r, "index"))
        .collect();
    let skipped: Vec<usize> = (done.len()..total).collect();
    error.remedy(remedy).details(
        json!({"steps":done,"applied_steps":applied,"uncertain_steps":uncertain,"skipped":skipped,"stopped_at":done.len().saturating_sub(1)}),
    )
}

/// Waits until the session reports a focused field (or a shown keyboard
/// when focus itself is not reported), within `timeout`. Answers what was
/// seen last and whether focus could be observed at all.
fn wait_for_focus(id: &str, deadline: Instant, timeout: Duration) -> Result<Value> {
    let end = (Instant::now() + timeout).min(deadline);
    let mut polls = 0;
    loop {
        check_cancelled()?;
        let context = super::request(id, "context", &json!({}))?;
        polls += 1;
        let focus = &context["focus"];
        let keyboard = &context["keyboard"];
        let observable = focus["available"] == true || keyboard["available"] == true;
        let ready =
            focus["focused"] == true || (focus["available"] != true && keyboard["visible"] == true);
        if !observable {
            return Ok(
                json!({"observable":false,"focused":Value::Null,"polls":polls,"reason":focus["reason"]}),
            );
        }
        if ready {
            return Ok(
                json!({"observable":true,"focused":true,"polls":polls,"source":focus["source"]}),
            );
        }
        if Instant::now() >= end {
            return Ok(
                json!({"observable":true,"focused":false,"polls":polls,"source":focus["source"]}),
            );
        }
        thread::sleep(Duration::from_millis(80).min(end.saturating_duration_since(Instant::now())));
    }
}

/// Runs one step and answers its result value; an error is the step's failure.
fn run_step(id: &str, step: &Value, deadline: Instant, store: &Store) -> Result<(Value, Value)> {
    let action = s(step, "action");
    let remaining = deadline.saturating_duration_since(Instant::now());
    let input = |extra: Value| -> Value {
        let mut a = json!({"session":id,"action":action});
        for key in [
            "x",
            "y",
            "x2",
            "y2",
            "duration",
            "key",
            "text",
            "orientation",
            "scale",
            "contacts",
        ] {
            if let Some(v) = step.get(key) {
                a[key] = v.clone();
            }
        }
        // A verified fill's own arguments, validated by check(); its focus
        // wait never outlasts the batch.
        if action == "fill" && b(step, "verify_value") {
            a["verify_value"] = json!(true);
            for key in ["field_name", "submit"] {
                if let Some(v) = step.get(key) {
                    a[key] = v.clone();
                }
            }
            let wait = ms(step, "focus_timeout_ms", 2000, super::field::FOCUS_MAX_MS);
            a["focus_timeout_ms"] = json!(wait.min(remaining.as_millis() as u64));
        }
        if let Some(extra) = extra.as_object() {
            for (k, v) in extra {
                a[k] = v.clone();
            }
        }
        a
    };
    match action {
        "tap" if step.get("mark").is_some() => {
            let mut session = Session::open(id)?;
            let mut a = json!({"device":id,"mark":step["mark"]});
            if let Some(hold) = step.get("hold_ms") {
                a["duration"] = hold.clone();
            }
            if let Some(verify) = step.get("verify") {
                a["verify"] = verify.clone();
            }
            let result = screen::tap(&a, &mut session, store)?;
            Ok((result, json!({})))
        }
        "tap" => {
            let result = match step["hold_ms"].as_u64() {
                Some(hold) => super::request(
                    id,
                    "input",
                    &json!({"session":id,"action":"swipe","x":step["x"],"y":step["y"],"x2":step["x"],"y2":step["y"],
                        "duration":(hold as f64 / 1000.0).clamp(0.01, 5.0)}),
                )?,
                None => super::request(id, "input", &input(json!({})))?,
            };
            Ok((result, json!({})))
        }
        "swipe" | "key" | "fill" | "pinch" | "rotate" | "gesture" => {
            let op = if action == "gesture" {
                "gesture"
            } else {
                "input"
            };
            Ok((super::request(id, op, &input(json!({})))?, json!({})))
        }
        "text" => {
            let mode = step["focus"].as_str().unwrap_or("wait");
            let mut focus = json!({"mode":mode});
            if mode != "none" {
                let wait = Duration::from_millis(ms(step, "focus_timeout_ms", 2000, 60_000));
                let seen = wait_for_focus(id, deadline, wait)?;
                focus["seen"] = seen.clone();
                if seen["observable"] == false {
                    if mode == "require" {
                        return Err(Error::new(
                            "focus_unobservable",
                            "This transport does not report the input focus, and the step requires it",
                        )
                        .remedy("Use focus wait (types after a short wait when focus cannot be observed) or focus none, and check the result with expect or a fresh runtime.observe")
                        .details(seen));
                    }
                    // Nothing can be observed: a short settle instead of a blind
                    // send, and the result says the focus was not seen.
                    thread::sleep(
                        Duration::from_millis(ms(step, "settle_ms", 300, 5000)).min(remaining),
                    );
                    focus["status"] = json!("unobservable");
                } else if seen["focused"] != true {
                    return Err(Error::new(
                        "focus_timeout",
                        "No field took the focus within the wait, so nothing was typed",
                    )
                    .remedy("Tap the field first (a tap step before this one), wait longer with focus_timeout_ms, or look with runtime.observe")
                    .details(seen));
                } else {
                    focus["status"] = json!("focused");
                }
            } else {
                focus["status"] = json!("not_checked");
            }
            let result = super::request(id, "input", &input(json!({})))?;
            Ok((result, json!({"focus":focus})))
        }
        "wait" => {
            let pause = Duration::from_millis(ms(step, "ms", 0, 10_000)).min(remaining);
            thread::sleep(pause);
            Ok((json!({"waited_ms":pause.as_millis() as u64}), json!({})))
        }
        "wait_text" | "wait_text_gone" => {
            let mut session = Session::open(id)?;
            let timeout = ms(step, "timeout_ms", 5000, 300_000).min(remaining.as_millis() as u64);
            let mut a = json!({"device":id,"until":if action == "wait_text" {"text"} else {"text_gone"},"text":step["text"],
                "timeout":timeout.div_ceil(1000),"interval_ms":ms(step, "interval_ms", 200, 10_000).max(50)});
            for key in ["source", "fuzzy", "min_confidence", "language", "region"] {
                if let Some(v) = step.get(key) {
                    a[key] = v.clone();
                }
            }
            Ok((screen::wait(&a, &mut session, &mut Tesseract)?, json!({})))
        }
        "wait_stable" => {
            let mut session = Session::open(id)?;
            let timeout = ms(step, "timeout_ms", 5000, 300_000).min(remaining.as_millis() as u64);
            let mut a = json!({"device":id,"until":"stable","timeout":timeout.div_ceil(1000),
                "interval_ms":ms(step, "interval_ms", 200, 10_000).max(50),"stable_ms":ms(step, "stable_ms", 500, 60_000).max(100)});
            for key in ["threshold", "tolerance", "ignore"] {
                if let Some(v) = step.get(key) {
                    a[key] = v.clone();
                }
            }
            Ok((screen::wait(&a, &mut session, &mut Tesseract)?, json!({})))
        }
        "observe" => Ok((
            super::request(
                id,
                "observe",
                &json!({"session":id,"context":step.get("context").cloned().unwrap_or(json!(true))}),
            )?,
            json!({}),
        )),
        other => Err(invalid(format!("unknown action {other:?}"))),
    }
}

/// A step's postcondition: the text it expects on the screen (or gone)
/// within its timeout. Answers what the wait saw.
fn expect(id: &str, expect: &Value, deadline: Instant) -> Result {
    let mut session = Session::open(id)?;
    let remaining = deadline
        .saturating_duration_since(Instant::now())
        .as_millis() as u64;
    let timeout = ms(expect, "timeout_ms", 3000, 300_000).min(remaining);
    let (until, text) = match expect.get("text") {
        Some(text) => ("text", text.clone()),
        None => ("text_gone", expect["text_gone"].clone()),
    };
    let mut a = json!({"device":id,"until":until,"text":text,"timeout":timeout.div_ceil(1000),
        "interval_ms":ms(expect, "interval_ms", 200, 10_000).max(50)});
    for key in ["source", "fuzzy", "min_confidence", "language", "region"] {
        if let Some(v) = expect.get(key) {
            a[key] = v.clone();
        }
    }
    screen::wait(&a, &mut session, &mut Tesseract).map_err(|error| {
        Error::new(
            "postcondition_failed",
            format!("The step was applied, but its expected screen did not follow: {}", error.message),
        )
        .remedy("The action reached the device: do not send it again. Observe the screen (runtime.observe or screen.marks) and decide from what is there")
        .details(error.value())
    })
}

/// Refuses a batch planned on another geometry than the screen has now.
fn check_frame(id: &str, frame: &Value) -> Result<Value> {
    let geometry = super::request(id, "geometry", &json!({}))?;
    let mut mismatch = Vec::new();
    for key in ["width", "height"] {
        if let Some(expected) = frame[key].as_u64() {
            if geometry[key].as_u64() != Some(expected) {
                mismatch.push(key);
            }
        }
    }
    if let Some(expected) = frame["orientation"].as_str() {
        if geometry["orientation"]
            .as_str()
            .is_some_and(|o| o != expected)
        {
            mismatch.push("orientation");
        }
    }
    if !mismatch.is_empty() {
        return Err(Error::new(
            "frame_stale",
            format!(
                "The screen's {} changed since the frame this batch was planned on; nothing was sent",
                mismatch.join(" and ")
            ),
        )
        .remedy("Observe again with runtime.observe and plan the steps on the new frame")
        .details(json!({"expected":frame,"current":geometry})));
    }
    Ok(geometry)
}

pub fn run(a: &Value) -> Result {
    let id = s(a, "session");
    let steps = a["steps"].as_array().cloned().unwrap_or_default();
    if steps.is_empty() {
        return Err(invalid("steps lists at least one step"));
    }
    check(&steps)?;
    let state = super::handle("runtime.state", &json!({"session":id}))?;
    if s(&state, "status") != "connected" {
        return Err(Error::new(
            "runtime_unavailable",
            format!("Session {id} is {}", s(&state, "status")),
        )
        .remedy("Start the device again with runtime.start and use the session ID it answers"));
    }
    let started = Instant::now();
    let deadline = started + Duration::from_secs(a["timeout"].as_u64().unwrap_or(30).clamp(1, 300));
    let frame_checked = match a.get("frame").filter(|f| f.is_object()) {
        Some(frame) => Some(check_frame(id, frame)?),
        None => None,
    };
    let store = Store::home()?;
    let total = steps.len();
    let mut done: Vec<Value> = Vec::new();
    for (index, step) in steps.iter().enumerate() {
        let action = s(step, "action").to_string();
        let mutated = mutates(&action);
        if let Err(error) = check_cancelled() {
            return Err(stopped(
                Error::new(
                    "request_cancelled",
                    format!("{} before step {index}", error.message),
                ),
                "The steps listed as applied reached the device; the rest did not",
                &done,
                total,
            ));
        }
        if Instant::now() >= deadline {
            return Err(stopped(
                Error::new(
                    "actions_timeout",
                    format!("The batch's timeout ran out before step {index}"),
                ),
                "The steps listed as applied reached the device; the rest did not. Observe, then continue from the first skipped step",
                &done,
                total,
            ));
        }
        let at = Instant::now();
        let mut record =
            json!({"index":index,"action":action,"mutated":mutated,"started_at":now()});
        // The step's input is recorded in the request ledger as `ID/step-N`.
        crate::coordination::set_label(Some(format!("step-{index}")));
        let ran = run_step(id, step, deadline, &store);
        crate::coordination::set_label(None);
        match ran {
            Ok((result, extra)) => {
                // A verified fill read its exact value back from the editor.
                let field_verified = action == "fill" && result["verified"] == true;
                record["status"] = json!(if field_verified {
                    "verified"
                } else {
                    "applied"
                });
                if action == "fill" && b(step, "verify_value") {
                    record["field_verified"] = json!(field_verified);
                }
                record["result"] = result;
                if let Some(extra) = extra.as_object() {
                    for (k, v) in extra {
                        record[k] = v.clone();
                    }
                }
                record["duration_ms"] = json!(at.elapsed().as_millis() as u64);
                if let Some(wanted) = step.get("expect") {
                    match expect(id, wanted, deadline) {
                        Ok(seen) => {
                            record["status"] = json!("verified");
                            record["expect"] = seen;
                            record["verified_ms"] = json!(at.elapsed().as_millis() as u64);
                        }
                        Err(error) => {
                            record["status"] = json!("applied");
                            record["expect"] = json!({"met":false,"error":error.value()});
                            record["verified_ms"] = json!(at.elapsed().as_millis() as u64);
                            done.push(record);
                            return Err(stopped(
                                Error::new(
                                    "postcondition_failed",
                                    format!(
                                        "Step {index} ({}) was applied, but its expected screen did not follow",
                                        s(step, "action")
                                    ),
                                ),
                                "The step reached the device: do not send it again. Observe the screen and decide from what is there",
                                &done,
                                total,
                            ));
                        }
                    }
                }
                done.push(record);
            }
            Err(error) => {
                // A verified fill that sent text it could not confirm is
                // uncertain; one whose value was verified but whose Return
                // was not sent did reach the device, so it is applied (and
                // listed in applied_steps) though the batch stops on it.
                record["status"] = json!(match error.code.as_str() {
                    "field_uncertain" => "uncertain",
                    "field_submit_not_sent" => "applied",
                    _ => "failed",
                });
                record["duration_ms"] = json!(at.elapsed().as_millis() as u64);
                record["error"] = error.value();
                let code = error.code.clone();
                let message = format!(
                    "Step {index} ({}) failed: {}",
                    s(step, "action"),
                    error.message
                );
                done.push(record);
                // A step the transport refused did not change the device, unless
                // it says otherwise; the earlier steps did.
                // Refusals that say what to do keep their code; anything else
                // is a failed step.
                let kept = matches!(
                    code.as_str(),
                    "focus_timeout"
                        | "focus_unobservable"
                        | "mark_stale"
                        | "frame_stale"
                        | "request_cancelled"
                        | "fill_verification_unsupported"
                        | "field_tap_failed"
                        | "field_not_focused"
                        | "field_wrong_target"
                        | "field_unsupported"
                        | "field_stale"
                        | "field_unobservable"
                        | "field_uncertain"
                        | "field_submit_not_sent"
                );
                let remedy = if code == "field_uncertain" {
                    "The step listed in uncertain_steps sent text whose effect is unknown: never resend it. Observe the field and decide from what it holds; the steps listed as applied reached the device"
                } else {
                    "The steps listed as applied reached the device; do not resend them. Observe, fix the failed step and continue from it"
                };
                return Err(stopped(
                    if kept {
                        Error::new(&code, message)
                    } else {
                        Error::new("actions_step_failed", message)
                    },
                    remedy,
                    &done,
                    total,
                ));
            }
        }
    }
    let applied = done.iter().filter(|r| r["mutated"] == true).count();
    let verified = done.iter().filter(|r| r["status"] == "verified").count();
    let mut result = json!({"session":id,"steps":done,"count":total,"applied":applied,"verified":verified,
        "failed_index":Value::Null,"elapsed_ms":started.elapsed().as_millis() as u64,
        "frame_checked":frame_checked.is_some(),"geometry":frame_checked.unwrap_or(Value::Null),
        "evidence":state.get("evidence").cloned().unwrap_or(json!("device"))});
    if a["observe_after"] == json!(true) {
        result["observation"] = super::request(id, "observe", &json!({"session":id}))?;
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn steps_are_checked_before_anything_is_sent() {
        assert!(check(&[json!({"action":"tap","x":0.5,"y":0.5})]).is_ok());
        assert!(check(&[json!({"action":"tap","mark":3})]).is_ok());
        assert!(check(&[json!({"action":"tap","x":0.5})]).is_err());
        assert!(check(&[json!({"action":"text","text":"a","expect":{"text":"a"}})]).is_ok());
        assert!(check(&[json!({"action":"text","text":"a","expect":{"timeout_ms":5}})]).is_err());
        assert!(check(&[json!({"action":"dance"})]).is_err());
        assert!(check(&[json!({"action":"gesture","contacts":[{"points":[{"x":0.1,"y":0.1,"t_ms":9},{"x":0.2,"y":0.2,"t_ms":3}]}]})]).is_err());
        assert!(mutates("text") && !mutates("wait_text") && !mutates("observe"));
        let error = stopped(
            Error::new("actions_timeout", "late"),
            "go on",
            &[
                json!({"index":0,"status":"applied","mutated":true}),
                json!({"index":1,"status":"applied","mutated":false}),
                json!({"index":2,"status":"failed","mutated":true}),
            ],
            5,
        );
        let details = error.details.unwrap();
        assert_eq!(details["applied_steps"], json!([0]));
        assert_eq!(details["skipped"], json!([3, 4]));
        // An uncertain fill is listed apart: neither applied nor safe to resend.
        let error = stopped(
            Error::new("field_uncertain", "lost"),
            "look",
            &[
                json!({"index":0,"status":"verified","mutated":true}),
                json!({"index":1,"status":"uncertain","mutated":true}),
            ],
            3,
        );
        let details = error.details.unwrap();
        assert_eq!(details["applied_steps"], json!([0]));
        assert_eq!(details["uncertain_steps"], json!([1]));
        assert_eq!(details["skipped"], json!([2]));
        // A verified fill is checked like the rest before anything is sent.
        let fill =
            json!({"action":"fill","x":0.5,"y":0.5,"text":"a","verify_value":true,"submit":true});
        assert!(check(std::slice::from_ref(&fill)).is_ok());
        assert!(
            check(&[json!({"action":"fill","x":0.5,"y":0.5,"text":"a","submit":true})]).is_err()
        );
        assert!(check(&[json!({"action":"key","key":"enter","submit":true})]).is_err());
        assert!(check(&[json!({"action":"fill","x":0.5,"y":0.5,"text":"a","verify_value":true,"focus_timeout_ms":30000})]).is_err());
        // A plain fill and a text step keep their old focus bound.
        assert!(
            check(&[json!({"action":"fill","x":0.5,"y":0.5,"text":"a","focus_timeout_ms":30000})])
                .is_ok()
        );
        assert!(check(&[json!({"action":"text","text":"a","focus_timeout_ms":30000})]).is_ok());
    }
}
