//! Android crashes and ANRs, read from logcat as structured events.
use crate::{core::*, devices, jobs, toolchain as tc};
use serde_json::{Value, json};

/// How many log lines of one event are kept, and how many stack frames.
const EVENT_LINES: usize = 80;
const STACK_FRAMES: usize = 40;

struct Line<'a> {
    time: &'a str,
    pid: &'a str,
    tag: &'a str,
    message: &'a str,
    raw: &'a str,
}

fn parse_line(line: &str) -> Option<Line<'_>> {
    // threadtime, with an epoch or a month-day timestamp:
    // 1761937200.123  1234  1250 E AndroidRuntime: FATAL EXCEPTION: main
    // 10-31 20:15:01.123  1234  1250 E AndroidRuntime: FATAL EXCEPTION: main
    static PATTERN: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let pattern = PATTERN.get_or_init(|| {
        regex::Regex::new(
            r"^\s*(\d+\.\d+|\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\s+(?:\S+\s+)?(\d+)\s+(\d+)\s+[VDIWEFA]\s+([^:]*?)\s*: ?(.*)$",
        )
        .unwrap()
    });
    let c = pattern.captures(line)?;
    Some(Line {
        time: c.get(1)?.as_str(),
        pid: c.get(2)?.as_str(),
        tag: c.get(4)?.as_str(),
        message: c.get(5)?.as_str(),
        raw: line,
    })
}

fn time_value(time: &str) -> Value {
    match time.parse::<f64>() {
        Ok(seconds) => json!(seconds),
        Err(_) => json!(time),
    }
}

fn event(kind: &str, line: &Line) -> Value {
    json!({"kind":kind,"time":time_value(line.time),"package":"","process":"","pid":Value::Null,"lines":[line.raw]})
}

fn push_line(event: &mut Value, raw: &str) {
    if let Some(lines) = event["lines"].as_array_mut() {
        if lines.len() < EVENT_LINES {
            lines.push(json!(raw));
        }
    }
}

/// The package of a process name such as `dev.example:remote`.
fn package_of(process: &str) -> &str {
    process.split(':').next().unwrap_or(process)
}

/// Crash (`crash`), ANR (`anr`) and native crash (`native_crash`) events in
/// the order logcat printed them.
pub fn parse(log: &str) -> Vec<Value> {
    let mut events: Vec<Value> = Vec::new();
    // The open event of each kind, as (index in events, logging pid).
    let mut crash: Option<(usize, String)> = None;
    let mut anr: Option<(usize, String, usize)> = None;
    let mut native: Option<usize> = None;
    let signal = regex::Regex::new(
        r"Fatal signal (\d+) \((\w+)\).*?(?:in tid (\d+) \(([^)]*)\))?, pid (\d+) \(([^)]*)\)",
    )
    .unwrap();
    let tombstone = regex::Regex::new(r"pid: (\d+), tid: \d+, name: .*?>>> (\S+) <<<").unwrap();
    for raw in log.lines() {
        let Some(line) = parse_line(raw) else {
            continue;
        };
        let message = line.message.trim_end();
        if line.tag == "AndroidRuntime" {
            if let Some(thread) = message.strip_prefix("FATAL EXCEPTION: ") {
                let mut value = event("crash", &line);
                value["thread"] = json!(thread.trim());
                value["exception"] = json!("");
                value["message"] = json!("");
                value["stack"] = json!([]);
                value["caused_by"] = json!([]);
                value["pid"] = json!(line.pid.parse::<u64>().ok());
                events.push(value);
                crash = Some((events.len() - 1, line.pid.to_string()));
                continue;
            }
            if let Some((index, pid)) = &crash {
                if *pid == line.pid {
                    let value = &mut events[*index];
                    push_line(value, raw);
                    let text = message.trim();
                    if let Some(rest) = text.strip_prefix("Process: ") {
                        let (process, pid) = rest.split_once(", PID: ").unwrap_or((rest, ""));
                        value["process"] = json!(process);
                        value["package"] = json!(package_of(process));
                        if let Ok(pid) = pid.trim().parse::<u64>() {
                            value["pid"] = json!(pid);
                        }
                    } else if let Some(frame) = text.strip_prefix("at ") {
                        let stack = value["stack"].as_array_mut().unwrap();
                        if stack.len() < STACK_FRAMES {
                            stack.push(json!(frame));
                        }
                    } else if let Some(cause) = text.strip_prefix("Caused by: ") {
                        value["caused_by"]
                            .as_array_mut()
                            .unwrap()
                            .push(json!(cause));
                    } else if s(value, "exception").is_empty()
                        && !text.is_empty()
                        && !text.starts_with("...")
                    {
                        let (class, detail) = text.split_once(": ").unwrap_or((text, ""));
                        value["exception"] = json!(class);
                        value["message"] = json!(detail);
                    }
                }
            }
            continue;
        }
        if line.tag == "ActivityManager" {
            if let Some(rest) = message.strip_prefix("ANR in ") {
                let process = rest.split_whitespace().next().unwrap_or("");
                let mut value = event("anr", &line);
                value["process"] = json!(process);
                value["package"] = json!(package_of(process));
                value["component"] = json!(
                    rest.split_once('(')
                        .map(|(_, c)| c.trim_end_matches(')'))
                        .unwrap_or("")
                );
                value["reason"] = json!("");
                events.push(value);
                anr = Some((events.len() - 1, line.pid.to_string(), 0));
                continue;
            }
            if let Some((index, pid, seen)) = &mut anr {
                if *pid == line.pid && *seen < 12 {
                    *seen += 1;
                    let value = &mut events[*index];
                    push_line(value, raw);
                    let text = message.trim();
                    if let Some(found) = text.strip_prefix("PID: ") {
                        value["pid"] = json!(found.trim().parse::<u64>().ok());
                    } else if let Some(reason) = text.strip_prefix("Reason: ") {
                        value["reason"] = json!(reason);
                    }
                }
            }
            continue;
        }
        if let Some(c) = signal.captures(message) {
            let process = c.get(6).map_or("", |m| m.as_str());
            let mut value = event("native_crash", &line);
            value["signal"] = json!(c.get(2).map_or("", |m| m.as_str()));
            value["signal_number"] = json!(c[1].parse::<u64>().ok());
            value["thread"] = json!(c.get(4).map_or("", |m| m.as_str()));
            value["process"] = json!(process);
            value["package"] = json!(package_of(process));
            value["pid"] = json!(c[5].parse::<u64>().ok());
            value["message"] = json!("");
            events.push(value);
            native = Some(events.len() - 1);
            continue;
        }
        if line.tag == "DEBUG" || line.tag == "crash_dump64" || line.tag == "crash_dump32" {
            if let Some(index) = native {
                let value = &mut events[index];
                let text = message.trim();
                if let Some(c) = tombstone.captures(text) {
                    if value["pid"].as_u64() == c[1].parse::<u64>().ok() {
                        value["process"] = json!(&c[2]);
                        value["package"] = json!(package_of(&c[2]));
                    }
                }
                if let Some(abort) = text.strip_prefix("Abort message: ") {
                    value["message"] = json!(abort.trim_matches('\''));
                }
                if text.starts_with('#') && text.contains(" pc ") {
                    let stack = value
                        .as_object_mut()
                        .unwrap()
                        .entry("stack")
                        .or_insert(json!([]));
                    if stack.as_array().is_some_and(|s| s.len() < STACK_FRAMES) {
                        stack.as_array_mut().unwrap().push(json!(text));
                    }
                }
                push_line(value, raw);
            }
        }
    }
    events
}

/// Whether an event belongs to the app: its package, or one of its processes.
fn belongs(event: &Value, bundle_id: &str) -> bool {
    bundle_id.is_empty() || s(event, "package") == bundle_id
}

/// Events within [since, until] on the device clock, for one app or all.
pub fn select(events: Vec<Value>, bundle_id: &str, since: f64, until: f64) -> Vec<Value> {
    events
        .into_iter()
        .filter(|e| belongs(e, bundle_id))
        .filter(|e| match e["time"].as_f64() {
            Some(time) => time >= since && time <= until,
            None => true,
        })
        .collect()
}

/// The device's crash, system and main logs since `since` (epoch seconds), or
/// the last `lines` lines, with epoch timestamps.
pub fn read_log(device: &str, since: Option<f64>, lines: u64) -> Result<String> {
    let mut args = argv(&[
        "logcat",
        "-d",
        "-b",
        "crash,system,main",
        "-v",
        "threadtime",
        "-v",
        "epoch",
        "-t",
    ]);
    args.push(match since {
        Some(time) => format!("{time:.3}"),
        None => lines.to_string(),
    });
    let mut command = devices::adb(device)?;
    command.extend(args);
    let output = tc::run(&command, 60, None, true)?;
    Ok(s(&output, "stdout").to_string())
}

/// Events on a device since `since` (epoch seconds, device clock).
pub fn on_device(device: &str, bundle_id: &str, since: f64) -> Result<Vec<Value>> {
    let log = read_log(device, Some(since), 0)?;
    Ok(select(parse(&log), bundle_id, since, f64::INFINITY))
}

pub fn crashes(a: &Value) -> Result {
    let device = identifier(s(a, "device"), "device", None)?;
    let bundle_id = s(a, "bundle_id");
    if !bundle_id.is_empty() {
        identifier(
            bundle_id,
            "application ID",
            Some(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+"),
        )?;
    }
    let mut since = a["since"].as_f64();
    let mut until = f64::INFINITY;
    if !s(a, "job_id").is_empty() {
        let job = jobs::get(s(a, "job_id"))?;
        // The job's times are the computer's clock; an emulator keeps the
        // same, a phone usually within a second or two.
        since = Some(
            since
                .unwrap_or(0.0)
                .max(job["created"].as_f64().unwrap_or(0.0) - 2.0),
        );
        if jobs::TERMINAL.contains(&s(&job, "state")) {
            until = job["updated"].as_f64().unwrap_or(f64::INFINITY) + 2.0;
        }
    }
    let log = read_log(device, since, n(a, "lines"))?;
    let events = select(parse(&log), bundle_id, since.unwrap_or(0.0), until);
    let count = |kind: &str| events.iter().filter(|e| e["kind"] == kind).count();
    let value = json!({
        "device":device,"bundle_id":bundle_id,"since":since,"until":if until.is_finite(){json!(until)}else{Value::Null},
        "crashed":!events.is_empty(),
        "counts":{"crash":count("crash"),"anr":count("anr"),"native_crash":count("native_crash")},
        "events":events,
    });
    if !s(a, "report").is_empty() {
        atomic_json(&output_path(s(a, "report"))?, &value)?;
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    const LOG: &str = "\
--------- beginning of crash
1761937200.100  4321  4321 E AndroidRuntime: FATAL EXCEPTION: main
1761937200.101  4321  4321 E AndroidRuntime: Process: dev.example.app, PID: 4321
1761937200.102  4321  4321 E AndroidRuntime: java.lang.IllegalStateException: Counter overflow: 3
1761937200.103  4321  4321 E AndroidRuntime: \tat dev.example.app.MainActivity.add(MainActivity.java:42)
1761937200.104  4321  4321 E AndroidRuntime: \tat android.view.View.performClick(View.java:8000)
1761937200.105  1500  1600 I ActivityManager: Start proc 9999:other/u0a1
1761937200.106  4321  4321 E AndroidRuntime: Caused by: java.lang.ArithmeticException: divide by zero
1761937200.107  4321  4321 E AndroidRuntime: \t... 12 more
--------- beginning of system
1761937300.000   600   700 E ActivityManager: ANR in dev.example.app:sync (dev.example.app/.SyncService)
1761937300.001   600   700 E ActivityManager: PID: 5555
1761937300.002   600   700 E ActivityManager: Reason: executing service dev.example.app/.SyncService
1761937300.003   600   700 E ActivityManager: Load: 1.2 / 0.9 / 0.5
1761937400.000  7777  7790 F libc    : Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x0 in tid 7790 (RenderThread), pid 7777 (dev.other)
1761937400.100  7800  7800 F DEBUG   : pid: 7777, tid: 7790, name: RenderThread  >>> dev.other <<<
1761937400.101  7800  7800 F DEBUG   : Abort message: 'bad pointer'
1761937400.102  7800  7800 F DEBUG   :       #00 pc 000000000004f2a0  /system/lib64/libc.so (abort+160)
10-31 20:15:01.123  1234  1250 E AndroidRuntime: FATAL EXCEPTION: worker
10-31 20:15:01.124  1234  1250 E AndroidRuntime: Process: dev.legacy, PID: 1234
10-31 20:15:01.125  1234  1250 E AndroidRuntime: java.lang.NullPointerException
";

    #[test]
    fn crashes_anrs_and_native_crashes_are_parsed() {
        let events = parse(LOG);
        assert_eq!(events.len(), 4);
        let crash = &events[0];
        assert_eq!(crash["kind"], "crash");
        assert_eq!(crash["time"], 1761937200.1);
        assert_eq!(crash["package"], "dev.example.app");
        assert_eq!(crash["pid"], 4321);
        assert_eq!(crash["thread"], "main");
        assert_eq!(crash["exception"], "java.lang.IllegalStateException");
        assert_eq!(crash["message"], "Counter overflow: 3");
        assert_eq!(
            crash["stack"],
            json!([
                "dev.example.app.MainActivity.add(MainActivity.java:42)",
                "android.view.View.performClick(View.java:8000)"
            ])
        );
        assert_eq!(
            crash["caused_by"],
            json!(["java.lang.ArithmeticException: divide by zero"])
        );
        // Another process's line in between is not part of the crash.
        assert!(!crash["lines"].to_string().contains("Start proc"));
        let anr = &events[1];
        assert_eq!(anr["kind"], "anr");
        assert_eq!(anr["process"], "dev.example.app:sync");
        assert_eq!(anr["package"], "dev.example.app");
        assert_eq!(anr["pid"], 5555);
        assert_eq!(anr["component"], "dev.example.app/.SyncService");
        assert_eq!(
            anr["reason"],
            "executing service dev.example.app/.SyncService"
        );
        let native = &events[2];
        assert_eq!(native["kind"], "native_crash");
        assert_eq!(native["signal"], "SIGSEGV");
        assert_eq!(native["signal_number"], 11);
        assert_eq!(native["package"], "dev.other");
        assert_eq!(native["pid"], 7777);
        assert_eq!(native["thread"], "RenderThread");
        assert_eq!(native["message"], "bad pointer");
        assert_eq!(native["stack"].as_array().unwrap().len(), 1);
        let legacy = &events[3];
        assert_eq!(legacy["time"], "10-31 20:15:01.123");
        assert_eq!(legacy["exception"], "java.lang.NullPointerException");
        assert_eq!(legacy["message"], "");
    }

    #[test]
    fn events_are_selected_by_app_and_time() {
        let app = select(parse(LOG), "dev.example.app", 0.0, f64::INFINITY);
        assert_eq!(app.len(), 2);
        let later = select(parse(LOG), "", 1761937250.0, 1761937350.0);
        // The ANR, and the event whose time has no year, which is kept.
        assert_eq!(later.len(), 2);
        assert_eq!(later[0]["kind"], "anr");
        assert!(select(parse(LOG), "dev.example", 0.0, f64::INFINITY).is_empty());
        assert!(parse("no log here\n").is_empty());
    }
}
