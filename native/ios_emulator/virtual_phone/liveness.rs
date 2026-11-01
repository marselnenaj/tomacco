//! Whether a running virtual iPhone still shows signs of life, for
//! `ios.virtual.status`: the newest frame in its mailbox, the size of its
//! console log, and, when the status is asked to `probe`, whether the guest
//! service answers. Each sign is remembered with the time it last changed in
//! `liveness.json` in the guest job's run directory, so that a later status
//! can tell how long the phone has been silent.
//!
//! A still picture is normal: the renderer publishes only changed frames,
//! and an idle phone prints little. So silence alone is `quiet`; a phone is
//! `stalled` only when a probe found the guest service not answering and
//! neither it nor the screen has shown life for `stall_seconds`. Only a new
//! frame or an answered probe makes it `live`: the console keeps printing on
//! a phone whose display is black or frozen (seen live: console output while
//! the frame had not changed for 410 s), so console output alone is `quiet`
//! with `console_only`. Nothing here
//! acts on the phone: a stalled one gets a recovery list of typed calls (a
//! UI restart, then a verified stop) for a person or agent to make.
use super::*;

/// How long the phone may stay silent before it counts; profile.json's
/// `stall_seconds` (ios.virtual.configure) replaces it. The guest runs
/// several times slower than real time, so the default is generous.
pub(crate) const STALL_SECONDS: u64 = 300;
pub(crate) const STALL_SECONDS_RANGE: std::ops::RangeInclusive<u64> = 30..=86400;
const RECORD: &str = "liveness.json";

/// The configured silence limit of a profile.
pub(crate) fn stall_seconds(profile: &Value) -> u64 {
    profile["stall_seconds"]
        .as_u64()
        .filter(|seconds| STALL_SECONDS_RANGE.contains(seconds))
        .unwrap_or(STALL_SECONDS)
}

/// What one status call saw: the newest frame's number, the console log's
/// size, and a probe's outcome (Ok: the service answered; Err: its error
/// code), each None when not seen or not asked.
#[derive(Debug, Default)]
pub(crate) struct Seen {
    pub frame: Option<u64>,
    pub console: Option<u64>,
    pub probe: Option<std::result::Result<(), String>>,
}

/// The record with this call's observations: a sign whose value changed gets
/// this time; one seen for the first time also counts from now. Returns
/// whether anything is new to write.
pub(crate) fn update(record: &Value, seen: &Seen, now: f64) -> (Value, bool) {
    let mut next = if record["format"] == 1 {
        record.clone()
    } else {
        json!({"format":1,"since":now})
    };
    let mut changed = record["format"] != 1;
    for (key, value) in [("frame", seen.frame), ("console", seen.console)] {
        let Some(value) = value else { continue };
        if next[key]["value"].as_u64() != Some(value) {
            next[key] = json!({"value":value,"changed":now});
            changed = true;
        }
    }
    if let Some(probe) = &seen.probe {
        let answered = match probe {
            Ok(()) => json!(now),
            Err(_) => next["guest"]["answered"].clone(),
        };
        next["guest"] = json!({"answered":answered,"asked":now,
            "error":probe.as_ref().err()});
        changed = true;
    }
    (next, changed)
}

fn ago(at: Option<f64>, now: f64) -> Value {
    at.map_or(Value::Null, |at| {
        json!(((now - at).max(0.0) * 10.0).round() / 10.0)
    })
}

/// The status's `liveness` block from the record.
pub(crate) fn judge(record: &Value, now: f64, stall: u64, phase: &str, job: &str) -> Value {
    let at = |key: &str, field: &str| record[key][field].as_f64().filter(|t| t.is_finite());
    let since = record["since"].as_f64().unwrap_or(now);
    let frame = at("frame", "changed");
    let console = at("console", "changed");
    let answered = at("guest", "answered");
    let asked = at("guest", "asked");
    let error = record["guest"]["error"].as_str();
    let last_seen = json!({
        "frame":record["frame"]["value"].as_u64().map(|number| json!({"number":number,
            "changed_at":frame,"seconds_ago":ago(frame,now)})),
        "console":record["console"]["value"].as_u64().map(|bytes| json!({"bytes":bytes,
            "changed_at":console,"seconds_ago":ago(console,now)})),
        "guest":asked.map(|asked| json!({"answered_at":answered,"answered_seconds_ago":ago(answered,now),
            "asked_at":asked,"error":error})),
    });
    let limit = stall as f64;
    let silent = |times: &[Option<f64>]| {
        now - times
            .iter()
            .flatten()
            .fold(since, |newest, t| newest.max(*t))
            >= limit
    };
    let state = if matches!(phase, "preparing" | "booting") {
        "booting"
    } else if error.is_some() && silent(&[frame, answered]) {
        "stalled"
    } else if silent(&[frame, answered]) {
        "quiet"
    } else {
        "live"
    };
    let console_only = state == "quiet" && !silent(&[console]);
    let note = match state {
        "booting" => {
            "The guest service does not answer during the boot; silence is not judged yet."
        }
        "stalled" => {
            "The guest service did not answer the last probe, and neither it nor the screen has shown \
             life for stall_seconds. Nothing has been done to the phone; recovery lists the calls to make."
        }
        "quiet" if console_only => {
            "Only the console printed within stall_seconds: no new frame and no guest answer. A still \
             screen is normal, but so is console output on a black or frozen display; call \
             ios.virtual.status with probe true to ask the guest service."
        }
        "quiet" => {
            "Nothing new for stall_seconds: no new frame, no console output and no guest answer. A still \
             screen is normal; call ios.virtual.status with probe true to ask the guest service."
        }
        _ => "The phone showed a new frame or answered a probe within stall_seconds.",
    };
    let recovery = (state == "stalled").then(|| {
        json!([
            {"step":1,"action":"restart_ui","call":"ios.emulator.guest",
             "arguments":{"job_id":job,"operation":"restart_ui"},
             "why":"Restarts iOS's user interface (SpringBoard and backboardd) without ending the guest. \
                    It needs the guest service to answer; probe again afterwards."},
            {"step":2,"action":"stop","call":"ios.virtual.stop","arguments":{},
             "why":"Asks the guest to save first, four times twenty seconds apart, and only then ends it \
                    without saving; last_run then says what was kept. The next start uses the last verified run."},
        ])
    });
    json!({"state":state,"console_only":console_only,"stall_seconds":stall,"last_seen":last_seen,
        "note":note,"recovery":recovery})
}

/// Asks the guest service for its status, bounded to a few seconds. Ok when
/// it answered; Err with the error code when it did not. None when another
/// diagnostic held the service lane, which says nothing either way.
fn probe(job: &str) -> Option<std::result::Result<(), String>> {
    match guest_control::call(&json!({"job_id":job,"operation":"status","timeout_seconds":10})) {
        Ok(reply) if reply["response"]["ok"] == true => Some(Ok(())),
        Ok(_) => Some(Err("invalid_reply".into())),
        Err(error) if error.code == "ios_guest_busy" => None,
        Err(error) => Some(Err(error.code)),
    }
}

/// The `liveness` block of a running guest job, after recording what this
/// call saw. A record that cannot be written costs the status nothing.
pub(crate) fn observe(job: &Value, profile: &Value, phase: &str, ask: bool) -> Value {
    let id = s(job, "id");
    let run = PathBuf::from(s(&job["plan"], "cwd"));
    let output = run.join("paravirtual");
    let request = read_json(&output.join("request.json"), Some(Value::Null)).unwrap_or(Value::Null);
    let console = fs::symlink_metadata(output.join("uart.log"))
        .ok()
        .filter(|meta| meta.is_file())
        .map(|meta| meta.len());
    let seen = Seen {
        frame: control::mailbox_frame_number(&request),
        console,
        probe: if ask { probe(id) } else { None },
    };
    let file = run.join(RECORD);
    let previous = if file.is_symlink() {
        Value::Null
    } else {
        read_json(&file, Some(Value::Null)).unwrap_or(Value::Null)
    };
    let now = now();
    let (record, changed) = update(&previous, &seen, now);
    if changed && run.is_dir() && !file.is_symlink() {
        let temporary = run.join(format!(".{RECORD}.tmp"));
        let written = serde_json::to_vec(&record)
            .map_err(std::io::Error::other)
            .and_then(|bytes| fs::write(&temporary, bytes))
            .and_then(|()| fs::rename(&temporary, &file));
        if written.is_err() {
            let _ = fs::remove_file(&temporary);
        }
    }
    let mut verdict = judge(&record, now, stall_seconds(profile), phase, id);
    if ask {
        verdict["probed"] = json!(seen.probe.is_some());
    }
    verdict
}

#[cfg(test)]
mod tests {
    use super::*;

    const JOB: &str = "0123456789abcdef0123456789abcdef";

    fn seen(
        frame: Option<u64>,
        console: Option<u64>,
        probe: Option<std::result::Result<(), &str>>,
    ) -> Seen {
        Seen {
            frame,
            console,
            probe: probe.map(|probe| probe.map_err(str::to_string)),
        }
    }

    #[test]
    fn each_sign_counts_from_when_it_last_changed() {
        let (record, changed) = update(&Value::Null, &seen(Some(5), Some(100), None), 1000.0);
        assert!(changed);
        assert_eq!(record["since"], 1000.0);
        assert_eq!(record["frame"], json!({"value":5,"changed":1000.0}));
        // The same frame later changes nothing; more console output does.
        let (record, changed) = update(&record, &seen(Some(5), Some(100), None), 1100.0);
        assert!(!changed);
        let (record, changed) = update(&record, &seen(Some(5), Some(180), None), 1200.0);
        assert!(changed);
        assert_eq!(record["frame"]["changed"], 1000.0);
        assert_eq!(record["console"]["changed"], 1200.0);
        // A sign not seen this time keeps its last value.
        let (record, _) = update(&record, &seen(None, None, None), 1300.0);
        assert_eq!(record["frame"]["value"], 5);
        // A failed probe keeps the last answer's time.
        let (record, _) = update(&record, &seen(None, None, Some(Ok(()))), 1400.0);
        let (record, changed) = update(
            &record,
            &seen(None, None, Some(Err("ios_guest_timeout"))),
            1500.0,
        );
        assert!(changed);
        assert_eq!(
            record["guest"],
            json!({"answered":1400.0,"asked":1500.0,"error":"ios_guest_timeout"})
        );
    }

    #[test]
    fn a_still_screen_is_quiet_and_only_an_unanswered_probe_makes_a_stall() {
        let (record, _) = update(&Value::Null, &seen(Some(5), Some(100), None), 1000.0);
        let live = judge(&record, 1200.0, 300, "launch-accepted", JOB);
        assert_eq!(live["state"], "live");
        assert!(live["recovery"].is_null());
        assert_eq!(live["last_seen"]["frame"]["seconds_ago"], 200.0);
        assert!(live["last_seen"]["guest"].is_null());
        // Five minutes without a new frame, console output or answer.
        let quiet = judge(&record, 1300.0, 300, "launch-accepted", JOB);
        assert_eq!(quiet["state"], "quiet");
        assert!(quiet["recovery"].is_null());
        // A probe that the guest answers makes it live again.
        let (answered, _) = update(&record, &seen(None, None, Some(Ok(()))), 1300.0);
        assert_eq!(
            judge(&answered, 1310.0, 300, "launch-accepted", JOB)["state"],
            "live"
        );
        // One it does not answer, with no frame or answer for the limit, is a stall.
        let (silent, _) = update(
            &record,
            &seen(None, None, Some(Err("ios_guest_timeout"))),
            1300.0,
        );
        let stalled = judge(&silent, 1300.0, 300, "launch-accepted", JOB);
        assert_eq!(stalled["state"], "stalled");
        assert_eq!(stalled["last_seen"]["guest"]["error"], "ios_guest_timeout");
        assert_eq!(stalled["last_seen"]["frame"]["number"], 5);
        let recovery = stalled["recovery"].as_array().unwrap();
        assert_eq!(
            recovery
                .iter()
                .map(|step| s(step, "action"))
                .collect::<Vec<_>>(),
            ["restart_ui", "stop"]
        );
        assert_eq!(recovery[0]["arguments"]["job_id"], JOB);
        assert_eq!(recovery[1]["call"], "ios.virtual.stop");
        // A frame newer than the limit means the screen still moves: not a stall.
        let (moving, _) = update(&silent, &seen(Some(6), None, None), 1290.0);
        assert_eq!(
            judge(&moving, 1300.0, 300, "launch-accepted", JOB)["state"],
            "live"
        );
        // A shorter configured limit judges sooner.
        let (recent, _) = update(
            &Value::Null,
            &seen(Some(1), None, Some(Err("ios_guest_disconnected"))),
            1000.0,
        );
        assert_eq!(
            judge(&recent, 1030.0, 300, "service-ready", JOB)["state"],
            "live"
        );
        assert_eq!(
            judge(&recent, 1030.0, 30, "service-ready", JOB)["state"],
            "stalled"
        );
    }

    #[test]
    fn console_output_alone_does_not_make_a_frozen_screen_live() {
        // The live case: the frame stayed number 1 for 410 s while the
        // console kept printing on a black display.
        let (record, _) = update(&Value::Null, &seen(Some(1), Some(1000), None), 1000.0);
        let (record, _) = update(&record, &seen(Some(1), Some(1_096_578), None), 1410.0);
        let verdict = judge(&record, 1410.0, 300, "launch-accepted", JOB);
        assert_eq!(verdict["state"], "quiet");
        assert_eq!(verdict["console_only"], true);
        assert_eq!(verdict["last_seen"]["frame"]["seconds_ago"], 410.0);
        assert_eq!(verdict["last_seen"]["console"]["seconds_ago"], 0.0);
        assert!(
            verdict["note"]
                .as_str()
                .unwrap()
                .starts_with("Only the console")
        );
        assert!(verdict["recovery"].is_null());
        // A new frame makes it live again.
        let (moved, _) = update(&record, &seen(Some(2), None, None), 1420.0);
        let live = judge(&moved, 1420.0, 300, "launch-accepted", JOB);
        assert_eq!(live["state"], "live");
        assert_eq!(live["console_only"], false);
        // So does an answered probe, without any new frame.
        let (answered, _) = update(&record, &seen(None, None, Some(Ok(()))), 1420.0);
        assert_eq!(
            judge(&answered, 1420.0, 300, "launch-accepted", JOB)["state"],
            "live"
        );
        // Silence everywhere is quiet without console_only.
        let silent = judge(&record, 1800.0, 300, "launch-accepted", JOB);
        assert_eq!(silent["state"], "quiet");
        assert_eq!(silent["console_only"], false);
    }

    #[test]
    fn the_recorded_black_display_run_is_quiet_not_live() {
        // liveness.json of a live run whose display stayed black: one frame
        // early in the boot, then only console output.
        let record: Value = serde_json::from_str(include_str!(
            "../../../tests/fixtures/liveness-black-display.json"
        ))
        .unwrap();
        let console = record["console"]["changed"].as_f64().unwrap();
        let verdict = judge(&record, console, 300, "launch-accepted", JOB);
        assert_eq!(verdict["state"], "quiet");
        assert_eq!(verdict["console_only"], true);
        assert_eq!(verdict["last_seen"]["frame"]["number"], 1);
        assert_eq!(verdict["last_seen"]["frame"]["seconds_ago"], 418.2);
    }

    #[test]
    fn a_boot_is_never_judged_and_the_limit_comes_from_the_profile() {
        let (record, _) = update(
            &Value::Null,
            &seen(Some(0), None, Some(Err("ios_guest_starting"))),
            0.0,
        );
        for phase in ["preparing", "booting"] {
            assert_eq!(judge(&record, 9999.0, 300, phase, JOB)["state"], "booting");
        }
        assert_eq!(stall_seconds(&json!({})), STALL_SECONDS);
        assert_eq!(stall_seconds(&json!({"stall_seconds":60})), 60);
        for wrong in [json!(5), json!(100000), json!("60")] {
            assert_eq!(
                stall_seconds(&json!({"stall_seconds":wrong})),
                STALL_SECONDS
            );
        }
    }
}
