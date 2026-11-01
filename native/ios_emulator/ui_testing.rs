//! UI tests on the running virtual iPhone, through the operations that
//! already reach it: `ios.emulator.control` for input and frames,
//! `ios.virtual.screenshot` for pictures and the guest service's
//! `crash_reports` for crashes.
//!
//! The guest tools report no UI hierarchy, so nothing here can find an
//! element by its label: scenario steps act at pixel coordinates of the
//! screenshot, and wait for or compare whole screens instead.
use super::*;
use crate::automation;

/// The device name testing.* operations take for the virtual iPhone.
pub const DEVICE: &str = "virtual-iphone";

pub fn is_virtual(device: &str) -> bool {
    device == DEVICE
}

/// The refusal of an operation that needs the UI hierarchy.
pub fn no_hierarchy(operation: &str) -> Error {
    Error::new(
        "ui_hierarchy_unavailable",
        format!(
            "{operation} needs a UI hierarchy, and the virtual iPhone's guest tools do not report one"
        ),
    )
    .remedy("Use testing.run with tap, swipe, text, key, wait_screen and assert_screen steps, or ios.emulator.control with coordinates")
    .details(json!({"device":DEVICE,"testing":capabilities()}))
}

/// What the virtual iPhone supports for UI tests.
pub fn capabilities() -> Value {
    json!({"ui_hierarchy":false,"selectors":false,"run_coordinates":true,"run_selectors":false,"run_screens":true,"run_launch":true,"run_stop_clear":false,"visual_diff":true,"lint":false,"accessibility":false,"unicode_text":false,"crashes":true,"run_crash_check":false})
}

/// The running phone as the scenario runner and testing.crashes use
/// it. Tests replace it with recorded replies.
pub trait Phone {
    /// The size of the screen in pixels, as screenshots have it.
    fn screen(&mut self) -> Result<(u64, u64)>;
    /// An `ios.emulator.control` call; the job is added.
    fn control(&mut self, a: Value) -> Result;
    /// `ios.virtual.screenshot` to a PNG at `path`.
    fn screenshot(&mut self, path: &Path) -> Result;
    /// An `ios.emulator.guest` call; the job is added.
    fn guest(&mut self, a: Value) -> Result;
}

/// The virtual iPhone that is running now.
#[derive(Default)]
pub struct LivePhone {
    job: Option<String>,
    screen: Option<(u64, u64)>,
}
impl LivePhone {
    fn job(&mut self) -> Result<String> {
        if let Some(job) = &self.job {
            return Ok(job.clone());
        }
        let (job, _) = virtual_phone::running_guest()?;
        self.job = Some(job.clone());
        Ok(job)
    }
}
impl Phone for LivePhone {
    fn screen(&mut self) -> Result<(u64, u64)> {
        if let Some(size) = self.screen {
            return Ok(size);
        }
        let job = self.job()?;
        // The frame file a viewer reads costs the guest nothing; without a
        // private runtime directory, a picture through the monitor says it.
        let size = match control::handle(&json!({"job_id":job,"action":"frame","transport":"file"}))
        {
            Ok(reply) => control::ppm_frame(Path::new(s(&reply["result"], "path"))),
            Err(_) => None,
        };
        let size = match size {
            Some(size) => size,
            None => {
                let reply = control::handle(&json!({"job_id":job,"action":"frame"}))?;
                screen_size(&reply["result"])?
            }
        };
        self.screen = Some(size);
        Ok(size)
    }
    fn control(&mut self, mut a: Value) -> Result {
        a["job_id"] = json!(self.job()?);
        control::handle(&a)
    }
    fn screenshot(&mut self, path: &Path) -> Result {
        virtual_screen::screenshot(&json!({"path":path}))
    }
    fn guest(&mut self, mut a: Value) -> Result {
        a["job_id"] = json!(self.job()?);
        guest_control::call(&a)
    }
}

fn screen_size(result: &Value) -> Result<(u64, u64)> {
    match (result["width"].as_u64(), result["height"].as_u64()) {
        (Some(width @ 1..=16384), Some(height @ 1..=16384)) => Ok((width, height)),
        _ => Err(Error::new(
            "invalid_capture",
            "The frame did not say the screen's size",
        )),
    }
}

/// A point in pixels of the screenshot as the 0..1 coordinates
/// ios.emulator.control takes: the middle of that pixel.
fn normalized(phone: &mut impl Phone, step: &Value, x: &str, y: &str) -> Result<(f64, f64)> {
    let (width, height) = phone.screen()?;
    let (px, py) = (n(step, x), n(step, y));
    if px >= width || py >= height {
        return Err(Error::new(
            "off_screen",
            format!("({px}, {py}) lies outside the {width}x{height} screen"),
        ));
    }
    Ok((
        (px as f64 + 0.5) / width as f64,
        (py as f64 + 0.5) / height as f64,
    ))
}

/// The QEMU key name for a scenario's key: QEMU names as they are, `home`
/// for the Home button, and the Android key codes that have an iPhone
/// counterpart.
fn key_name(key: &str) -> Result<&str> {
    let mapped = match key {
        "KEYCODE_HOME" => "home",
        "KEYCODE_ENTER" => "ret",
        "KEYCODE_DEL" => "backspace",
        "KEYCODE_FORWARD_DEL" => "delete",
        "KEYCODE_TAB" => "tab",
        "KEYCODE_ESCAPE" => "esc",
        "KEYCODE_SPACE" => "spc",
        "KEYCODE_DPAD_UP" => "up",
        "KEYCODE_DPAD_DOWN" => "down",
        "KEYCODE_DPAD_LEFT" => "left",
        "KEYCODE_DPAD_RIGHT" => "right",
        other if other.starts_with("KEYCODE_") || other.bytes().all(|c| c.is_ascii_digit()) => {
            return Err(Error::new(
                "invalid_key",
                format!("{other} has no key on the virtual iPhone; use a QEMU key name or home"),
            ));
        }
        other => other,
    };
    if mapped.is_empty()
        || mapped.len() > 32
        || !mapped
            .bytes()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'_')
    {
        return Err(Error::new(
            "invalid_key",
            "A key on the virtual iPhone is a QEMU key name such as ret, or home",
        ));
    }
    Ok(mapped)
}

/// How long a pointer contact may last in ios.emulator.control.
const CONTROL_DURATION: u64 = 2000;
/// The longest text one ios.emulator.control text call takes, in characters.
const TEXT_CHUNK: usize = 1024;

/// One scenario step on the phone. Screenshots go to `directory` with the
/// run's `tag` and the step's index in their name.
fn run_step(
    phone: &mut impl Phone,
    step: &Value,
    directory: &Path,
    tag: &str,
    index: usize,
) -> Result {
    let picture = directory.join(format!("{tag}-{index:03}.png"));
    let duration = |default: u64| {
        step["duration"]
            .as_u64()
            .unwrap_or(default)
            .clamp(1, CONTROL_DURATION)
    };
    match s(step, "action") {
        "tap" => {
            let (x, y) = normalized(phone, step, "x", "y")?;
            phone.control(json!({"action":"click","x":x,"y":y}))
        }
        "swipe" => {
            let (x, y) = normalized(phone, step, "x", "y")?;
            let (to_x, to_y) = normalized(phone, step, "end_x", "end_y")?;
            if (step["x"].clone(), step["y"].clone())
                == (step["end_x"].clone(), step["end_y"].clone())
            {
                // A swipe that stays put is a long press.
                phone.control(json!({"action":"click","x":x,"y":y,"duration_ms":duration(800)}))
            } else {
                phone.control(json!({"action":"drag","x":x,"y":y,"to_x":to_x,"to_y":to_y,
                    "duration_ms":duration(300)}))
            }
        }
        "text" => {
            let text: Vec<char> = s(step, "text").chars().collect();
            if text.is_empty() {
                return Err(Error::new(
                    "invalid_argument",
                    "A text step on the virtual iPhone needs at least one character",
                ));
            }
            let mut last = Value::Null;
            for chunk in text.chunks(TEXT_CHUNK) {
                last = phone
                    .control(json!({"action":"text","text":chunk.iter().collect::<String>()}))?;
            }
            Ok(last)
        }
        "key" => match key_name(s(step, "key"))? {
            "home" => phone.control(json!({"action":"home"})),
            key => phone.control(json!({"action":"key","keys":[key]})),
        },
        "pause" => {
            std::thread::sleep(Duration::from_millis(n(step, "duration")));
            Ok(json!({"paused_ms":n(step, "duration")}))
        }
        "screenshot" => phone.screenshot(&picture),
        "assert_screen" | "wait_screen" => automation::screen_step(
            step,
            &picture,
            &directory.join(format!("{tag}-{index:03}-diff.png")),
            |path| phone.screenshot(path).map(|_| ()),
        ),
        "launch" => {
            let reply = phone.guest(json!({"operation":"open","argument":s(step, "bundle_id"),
                "timeout_seconds":60}))?;
            if reply["response"]["ok"] == true {
                Ok(reply)
            } else {
                Err(
                    Error::new("app_action_failed", "The guest did not open the app")
                        .details(reply),
                )
            }
        }
        "stop" | "clear" => Err(Error::new(
            "unsupported_step",
            format!(
                "{} is not available on the virtual iPhone",
                s(step, "action")
            ),
        )),
        "wait" | "assert" | "click" | "long_click" | "fill" => {
            Err(no_hierarchy(&format!("A {} step", s(step, "action"))))
        }
        _ => Err(Error::new("invalid_step", "Unknown scenario action")),
    }
}

/// Runs one scenario on the phone, in the shape testing.run's report has
/// for an Android device. A failed step ends the scenario with a screenshot.
/// Crash reports are not read afterwards: their names follow the app's
/// executable, which a scenario does not name.
pub fn run_device(phone: &mut impl Phone, scenario: &Value, directory: &Path, tag: &str) -> Value {
    let started = Instant::now();
    let mut steps = Vec::new();
    let result = (|| -> Result<()> {
        for (index, step) in scenario["steps"]
            .as_array()
            .into_iter()
            .flatten()
            .enumerate()
        {
            let begin = Instant::now();
            let mut step = step.clone();
            if step.get("timeout").is_none() {
                step["timeout"] = json!(10);
            }
            let result = run_step(phone, &step, directory, tag, index);
            steps.push(
                json!({"index":index,"action":step["action"],"label":step["label"],
                "duration_seconds":begin.elapsed().as_secs_f64(),"passed":result.is_ok(),
                "result":match &result {Ok(v) => v.clone(), Err(e) => json!({"error":e.value()})}}),
            );
            if let Err(error) = result {
                let capture = phone.screenshot(&directory.join(format!("{tag}-failure.png")));
                steps.last_mut().unwrap()["failure_capture"] =
                    capture.unwrap_or_else(|e| json!({"error":e.value()}));
                return Err(error);
            }
        }
        Ok(())
    })();
    json!({"device":DEVICE,"passed":result.is_ok(),"duration_seconds":started.elapsed().as_secs_f64(),
        "steps":steps,"error":result.err().map(|e| e.value()),"crashes":[],
        "crashes_error":{"code":"crashes_not_checked",
            "message":"Crash reports of the virtual iPhone are not read after a scenario; call testing.crashes with the app's process name"}})
}

/// How many frames of the crashed thread an event keeps.
const STACK_FRAMES: usize = 40;

/// A time as crash reports write it, `2026-10-13 07:12:27.12 +0000`, in
/// seconds since 1970.
fn report_time(text: &str) -> Option<f64> {
    static PATTERN: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let pattern = PATTERN.get_or_init(|| {
        regex::Regex::new(
            r"^(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)(\.\d+)? ([+-])(\d\d)(\d\d)$",
        )
        .unwrap()
    });
    let c = pattern.captures(text)?;
    let number = |i: usize| c[i].parse::<i64>().ok();
    let (year, month, day) = (number(1)?, number(2)?, number(3)?);
    if !(1..=12).contains(&month) || !(1..=31).contains(&day) {
        return None;
    }
    // Days from 1970-01-01 to the civil date (proleptic Gregorian).
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let year_of_era = y - era * 400;
    let day_of_year = (153 * ((month + 9) % 12) + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    let days = era * 146_097 + day_of_era - 719_468;
    let fraction = c
        .get(7)
        .and_then(|m| m.as_str().parse::<f64>().ok())
        .unwrap_or(0.0);
    let offset = (number(9)? * 3600 + number(10)? * 60) * if &c[8] == "-" { -1 } else { 1 };
    Some(
        (days * 86_400 + number(4)? * 3600 + number(5)? * 60 + number(6)? - offset) as f64
            + fraction,
    )
}

/// One crash report's bytes (a JSON .ips: a header line, then the body) as
/// the event testing.crashes returns for Android: kind, time, package,
/// process, pid, thread, exception, message, signal and the crashed thread's
/// frames as `stack`. A report without an exception is not a crash.
pub fn crash_event(
    bytes: &[u8],
    name: &str,
    owner: &str,
) -> std::result::Result<Value, &'static str> {
    let newline = bytes
        .iter()
        .position(|byte| *byte == b'\n')
        .filter(|at| *at <= 4096)
        .ok_or("unsupported-report-format")?;
    let header: Value =
        serde_json::from_slice(&bytes[..newline]).map_err(|_| "unsupported-report-format")?;
    let body: Value =
        serde_json::from_slice(&bytes[newline + 1..]).map_err(|_| "unsupported-report-format")?;
    if !header.is_object() || !body.is_object() {
        return Err("unsupported-report-format");
    }
    let exception = body["exception"].as_object().ok_or("no-exception")?;
    let text = |value: &Value| value.as_str().unwrap_or_default().to_string();
    let threads = body["threads"].as_array().cloned().unwrap_or_default();
    let faulting = body["faultingThread"]
        .as_u64()
        .map(|index| index as usize)
        .filter(|index| *index < threads.len())
        .or_else(|| {
            threads
                .iter()
                .position(|thread| thread["triggered"] == true)
        });
    let images = body["usedImages"].as_array().cloned().unwrap_or_default();
    let (thread, stack) = match faulting {
        Some(index) => {
            let thread = &threads[index];
            let label = [&thread["name"], &thread["queue"]]
                .iter()
                .find_map(|value| value.as_str().filter(|text| !text.is_empty()))
                .map_or_else(|| format!("Thread {index}"), String::from);
            let frames: Vec<Value> = thread["frames"]
                .as_array()
                .into_iter()
                .flatten()
                .take(STACK_FRAMES)
                .enumerate()
                .map(|(number, frame)| {
                    let image = frame["imageIndex"]
                        .as_u64()
                        .and_then(|i| images.get(i as usize))
                        .and_then(|image| image["name"].as_str())
                        .unwrap_or("???");
                    match frame["symbol"].as_str() {
                        Some(symbol) => json!(format!(
                            "{number} {image} {symbol} + {}",
                            frame["symbolLocation"].as_u64().unwrap_or(0)
                        )),
                        None => json!(format!(
                            "{number} {image} +0x{:x}",
                            frame["imageOffset"].as_u64().unwrap_or(0)
                        )),
                    }
                })
                .collect();
            (json!(label), json!(frames))
        }
        None => (json!(""), json!([])),
    };
    // What says why: the abort messages, else the exception's subtype, else
    // the termination's indicator.
    let abort: Vec<String> = body["asi"]
        .as_object()
        .into_iter()
        .flat_map(|asi| asi.values())
        .flat_map(|messages| messages.as_array().cloned().unwrap_or_default())
        .filter_map(|message| message.as_str().map(String::from))
        .collect();
    let message = if !abort.is_empty() {
        abort.join(" ")
    } else if let Some(subtype) = exception.get("subtype").and_then(Value::as_str) {
        subtype.to_string()
    } else {
        text(&body["termination"]["indicator"])
    };
    let capture = text(&body["captureTime"]);
    let package = body["bundleInfo"]["CFBundleIdentifier"]
        .as_str()
        .or(header["bundleID"].as_str())
        .unwrap_or_default();
    Ok(json!({"kind":"crash",
        "time":report_time(&capture).map_or_else(|| json!(capture), |time| json!(time)),
        "package":package,"process":text(&body["procName"]),
        "pid":body["pid"].as_u64().filter(|pid| *pid > 0 && *pid <= i32::MAX as u64),
        "thread":thread,"exception":text(exception.get("type").unwrap_or(&Value::Null)),
        "signal":text(exception.get("signal").unwrap_or(&Value::Null)),
        "message":message,"termination":text(&body["termination"]["indicator"]),"stack":stack,
        "report":{"name":name,"owner":owner,"incident_id":header["incident_id"],"bug_type":header["bug_type"]}}))
}

/// The crash events in a guest's `crash_reports` reply, and the reports
/// that gave none with the reason.
fn crash_events(response: &Value) -> (Vec<Value>, Vec<Value>) {
    let mut events = Vec::new();
    let mut unparsed = Vec::new();
    for report in response["reports"].as_array().into_iter().flatten() {
        let name = s(report, "name");
        let owner = s(report, "owner");
        let bytes = report["content_base64"]
            .as_str()
            .and_then(|text| STANDARD.decode(text).ok());
        let outcome = match bytes {
            _ if report["content_complete"] != true => Err("incomplete-content"),
            Some(bytes) if Some(bytes.len() as u64) == report["content_bytes"].as_u64() => {
                crash_event(&bytes, name, owner)
            }
            _ => Err("content-size-mismatch"),
        };
        match outcome {
            Ok(event) => events.push(event),
            Err(reason) => unparsed.push(json!({"name":name,"owner":owner,"reason":reason})),
        }
    }
    (events, unparsed)
}

/// testing.crashes for the virtual iPhone: the crash reports whose names
/// start with `process`, as events. `bundle_id`, `since` and `job_id` select
/// as on Android, by the report's capture time, which is the guest's clock.
pub fn crashes(phone: &mut impl Phone, a: &Value) -> Result {
    let process = s(a, "process");
    if !guest_control::crash_report_filter_valid(process) {
        return Err(Error::new(
            "invalid_argument",
            "On the virtual iPhone, testing.crashes needs process: the app's executable name or another report name prefix",
        ));
    }
    let bundle_id = s(a, "bundle_id");
    let (since, until) = crate::crashes::window(a)?;
    let reply = phone.guest(json!({"operation":"crash_reports","argument":process,
        "timeout_seconds":60}))?;
    let response = &reply["response"];
    if response["ok"] != true {
        return Err(Error::new(
            "ios_guest_crash_reports_failed",
            "The guest could not read its crash reports",
        )
        .details(json!({"response":response})));
    }
    let (events, unparsed) = crash_events(response);
    let events: Vec<Value> = events
        .into_iter()
        .filter(|event| bundle_id.is_empty() || s(event, "package") == bundle_id)
        .filter(|event| match event["time"].as_f64() {
            Some(time) => time >= since.unwrap_or(0.0) && time <= until,
            None => true,
        })
        .collect();
    let value = json!({"device":DEVICE,"bundle_id":bundle_id,"process":process,"since":since,
        "until":if until.is_finite() {json!(until)} else {Value::Null},
        "crashed":!events.is_empty(),
        "counts":{"crash":events.len(),"anr":0,"native_crash":0},
        "events":events,"unparsed":unparsed,
        "collection_complete":response["collection_complete"] == true,
        "absence_proves_no_crash":false});
    if !s(a, "report").is_empty() {
        atomic_json(&output_path(s(a, "report"))?, &value)?;
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A phone that answers from recorded replies and remembers each call.
    #[derive(Default)]
    struct Recorded {
        calls: Vec<Value>,
        screen: (u64, u64),
        guest: Value,
        /// The PNG each screenshot writes, one after the other; the last
        /// one again when they run out.
        pictures: Vec<Vec<u8>>,
        fail_control: Option<&'static str>,
    }
    impl Phone for Recorded {
        fn screen(&mut self) -> Result<(u64, u64)> {
            Ok(self.screen)
        }
        fn control(&mut self, a: Value) -> Result {
            self.calls.push(a.clone());
            match self.fail_control {
                Some(code) if s(&a, "action") == code => {
                    Err(Error::new("ios_input_timeout", "recorded failure"))
                }
                _ => Ok(json!({"action":a["action"],"result":{"accepted_by_emulator":true}})),
            }
        }
        fn screenshot(&mut self, path: &Path) -> Result {
            self.calls.push(json!({"action":"screenshot","path":path}));
            let bytes = if self.pictures.len() > 1 {
                self.pictures.remove(0)
            } else {
                self.pictures.first().cloned().unwrap_or_default()
            };
            fs::write(path, &bytes)?;
            Ok(json!({"path":path}))
        }
        fn guest(&mut self, a: Value) -> Result {
            self.calls.push(a);
            Ok(self.guest.clone())
        }
    }

    fn png(width: usize, height: usize, rgb: [u8; 3]) -> Vec<u8> {
        virtual_screen::png(&rgb.repeat(width * height), width, height, 3).unwrap()
    }

    #[test]
    fn coordinate_steps_become_control_calls_at_the_pixels_middle() {
        let directory = tempfile::tempdir().unwrap();
        let mut phone = Recorded {
            screen: (1000, 2000),
            pictures: vec![png(2, 2, [0, 0, 0])],
            ..Default::default()
        };
        let scenario = json!({"name":"x","steps":[
            {"action":"tap","x":499,"y":999},
            {"action":"swipe","x":500,"y":1990,"end_x":500,"end_y":500,"duration":5000},
            {"action":"swipe","x":10,"y":10,"end_x":10,"end_y":10},
            {"action":"text","text":"me@example.com"},
            {"action":"key","key":"home"},
            {"action":"key","key":"KEYCODE_ENTER"},
            {"action":"key","key":"ret"},
            {"action":"pause","duration":1},
            {"action":"screenshot"}
        ]});
        automation::check_scenario(&scenario).unwrap();
        let result = run_device(&mut phone, &scenario, directory.path(), "t");
        assert_eq!(result["passed"], true, "{result}");
        assert_eq!(result["device"], DEVICE);
        assert_eq!(result["crashes_error"]["code"], "crashes_not_checked");
        let calls = &phone.calls;
        assert_eq!(calls[0], json!({"action":"click","x":0.4995,"y":0.49975}));
        assert_eq!(calls[1]["action"], "drag");
        assert_eq!(calls[1]["duration_ms"], 2000);
        assert_eq!(calls[1]["to_y"], 0.25025);
        // A swipe that stays put is a long press.
        assert_eq!(calls[2]["action"], "click");
        assert_eq!(calls[2]["duration_ms"], 800);
        assert_eq!(calls[3], json!({"action":"text","text":"me@example.com"}));
        assert_eq!(calls[4], json!({"action":"home"}));
        assert_eq!(calls[5], json!({"action":"key","keys":["ret"]}));
        assert_eq!(calls[6], json!({"action":"key","keys":["ret"]}));
        assert_eq!(calls[7]["action"], "screenshot");
        assert!(directory.path().join("t-008.png").is_file());
    }

    #[test]
    fn a_long_text_is_typed_in_pieces() {
        let directory = tempfile::tempdir().unwrap();
        let mut phone = Recorded {
            screen: (10, 10),
            ..Default::default()
        };
        let text = "ä".repeat(TEXT_CHUNK + 3);
        let scenario = json!({"name":"x","steps":[{"action":"text","text":text}]});
        assert_eq!(
            run_device(&mut phone, &scenario, directory.path(), "t")["passed"],
            true
        );
        assert_eq!(phone.calls.len(), 2);
        assert_eq!(phone.calls[1]["text"], "äää");
    }

    #[test]
    fn selector_steps_stop_and_off_screen_points_fail_with_a_screenshot() {
        let directory = tempfile::tempdir().unwrap();
        for (step, code) in [
            (
                json!({"action":"click","selector":{"text":"OK"}}),
                "ui_hierarchy_unavailable",
            ),
            (
                json!({"action":"assert","selector":{"text":"OK"}}),
                "ui_hierarchy_unavailable",
            ),
            (
                json!({"action":"stop","bundle_id":"dev.example.app"}),
                "unsupported_step",
            ),
            (json!({"action":"tap","x":100,"y":5}), "off_screen"),
            (json!({"action":"key","key":"KEYCODE_BACK"}), "invalid_key"),
            (json!({"action":"key","key":"Return Key"}), "invalid_key"),
        ] {
            let mut phone = Recorded {
                screen: (100, 200),
                pictures: vec![png(1, 1, [9, 9, 9])],
                ..Default::default()
            };
            let scenario = json!({"name":"x","steps":[{"action":"tap","x":1,"y":1},step,{"action":"tap","x":2,"y":2}]});
            let result = run_device(&mut phone, &scenario, directory.path(), "f");
            assert_eq!(result["passed"], false);
            assert_eq!(result["error"]["code"], code, "{result}");
            let steps = result["steps"].as_array().unwrap();
            // The step after the failed one does not run.
            assert_eq!(steps.len(), 2);
            assert_eq!(steps[1]["passed"], false);
            assert!(steps[1]["failure_capture"]["path"].is_string());
            if code == "ui_hierarchy_unavailable" {
                assert_eq!(result["error"]["details"]["testing"]["ui_hierarchy"], false);
            }
        }
        // An input the emulator refuses fails its step.
        let mut phone = Recorded {
            screen: (100, 200),
            fail_control: Some("click"),
            ..Default::default()
        };
        let scenario = json!({"name":"x","steps":[{"action":"tap","x":1,"y":1}]});
        let result = run_device(&mut phone, &scenario, directory.path(), "g");
        assert_eq!(result["error"]["code"], "ios_input_timeout");
    }

    #[test]
    fn launch_opens_the_app_through_the_guest() {
        let directory = tempfile::tempdir().unwrap();
        let scenario =
            json!({"name":"x","steps":[{"action":"launch","bundle_id":"dev.example.MyApp"}]});
        let mut phone = Recorded {
            guest: json!({"response":{"ok":true,"operation":"open","pid":412}}),
            ..Default::default()
        };
        assert_eq!(
            run_device(&mut phone, &scenario, directory.path(), "t")["passed"],
            true
        );
        assert_eq!(
            phone.calls[0],
            json!({"operation":"open","argument":"dev.example.MyApp","timeout_seconds":60})
        );
        let mut phone = Recorded {
            guest: json!({"response":{"ok":false,"error":"denied"}}),
            ..Default::default()
        };
        let result = run_device(&mut phone, &scenario, directory.path(), "u");
        assert_eq!(result["error"]["code"], "app_action_failed");
    }

    #[test]
    fn screen_steps_compare_screenshots_with_a_baseline() {
        let directory = tempfile::tempdir().unwrap();
        let baseline = directory.path().join("home.png");
        fs::write(&baseline, png(4, 4, [10, 20, 30])).unwrap();
        let step = |action: &str| json!({"name":"x","steps":[{"action":action,"baseline":baseline,"timeout":3,"tolerance":2}]});
        // Equal within the tolerance.
        let mut phone = Recorded {
            pictures: vec![png(4, 4, [11, 21, 31])],
            ..Default::default()
        };
        let result = run_device(&mut phone, &step("assert_screen"), directory.path(), "a");
        assert_eq!(result["passed"], true, "{result}");
        // Different: assert_screen fails at once and writes the difference.
        let mut phone = Recorded {
            pictures: vec![png(4, 4, [200, 20, 30])],
            ..Default::default()
        };
        let result = run_device(&mut phone, &step("assert_screen"), directory.path(), "b");
        assert_eq!(result["error"]["code"], "screen_mismatch");
        assert!(directory.path().join("b-000-diff.png").is_file());
        // wait_screen takes pictures until one matches.
        let mut phone = Recorded {
            pictures: vec![
                png(4, 4, [200, 0, 0]),
                png(4, 4, [200, 0, 0]),
                png(4, 4, [10, 20, 30]),
            ],
            ..Default::default()
        };
        let result = run_device(&mut phone, &step("wait_screen"), directory.path(), "c");
        assert_eq!(result["passed"], true, "{result}");
        assert_eq!(phone.calls.len(), 3);
        // A missing baseline says so.
        let mut phone = Recorded {
            pictures: vec![png(4, 4, [0, 0, 0])],
            ..Default::default()
        };
        let missing = json!({"name":"x","steps":[{"action":"assert_screen","baseline":directory.path().join("none.png")}]});
        let result = run_device(&mut phone, &missing, directory.path(), "d");
        assert_eq!(result["error"]["code"], "baseline_missing");
    }

    #[test]
    fn report_times_are_seconds_since_1970() {
        assert_eq!(report_time("1970-01-01 00:00:00 +0000"), Some(0.0));
        assert_eq!(
            report_time("2026-10-13 07:12:27.50 +0000"),
            Some(1_791_875_547.5)
        );
        assert_eq!(
            report_time("2026-10-13 09:12:27.50 +0200"),
            Some(1_791_875_547.5)
        );
        assert_eq!(
            report_time("2024-02-29 00:00:00 -0100"),
            Some(1_709_168_400.0)
        );
        assert_eq!(report_time("2026-13-01 00:00:00 +0000"), None);
        assert_eq!(report_time("yesterday"), None);
    }

    /// A crash report in the JSON .ips layout, written for this test.
    fn ips(process: &str, capture: &str) -> String {
        let header = json!({"app_name":process,"bug_type":"309","bundleID":format!("dev.example.{process}"),
            "incident_id":"00000000-0000-4000-8000-000000000001","name":process});
        let body = json!({"procName":process,"pid":412,"captureTime":capture,
            "bundleInfo":{"CFBundleIdentifier":format!("dev.example.{process}")},
            "exception":{"type":"EXC_CRASH","signal":"SIGABRT","codes":"0x0, 0x0"},
            "asi":{"libsystem_c.dylib":["abort() called"]},
            "termination":{"namespace":"SIGNAL","indicator":"Abort trap: 6"},
            "faultingThread":1,
            "threads":[{"frames":[]},{"queue":"com.apple.main-thread","triggered":true,"frames":[
                {"imageIndex":0,"symbol":"__pthread_kill","symbolLocation":8,"imageOffset":100},
                {"imageIndex":1,"imageOffset":4660},
                {"imageIndex":7,"imageOffset":1}]}],
            "usedImages":[{"name":"libsystem_kernel.dylib"},{"name":process}]});
        format!("{header}\n{body}")
    }

    fn reply(reports: &[(&str, String)]) -> Value {
        let reports: Vec<Value> = reports
            .iter()
            .map(|(name, text)| {
                json!({"name":name,"owner":"mobile","content_complete":true,
                "content_bytes":text.len(),"observed_file_bytes":text.len(),
                "content_base64":STANDARD.encode(text)})
            })
            .collect();
        json!({"response":{"ok":true,"operation":"crash_reports","process":"MyApp",
            "collection_complete":true,"reports":reports}})
    }

    #[test]
    fn a_crash_report_becomes_an_event_like_androids() {
        let event = crash_event(
            ips("MyApp", "2026-10-13 07:12:27.50 +0000").as_bytes(),
            "MyApp-2026-10-13-071227.ips",
            "mobile",
        )
        .unwrap();
        assert_eq!(event["kind"], "crash");
        assert_eq!(event["time"], 1_791_875_547.5);
        assert_eq!(event["package"], "dev.example.MyApp");
        assert_eq!(event["process"], "MyApp");
        assert_eq!(event["pid"], 412);
        assert_eq!(event["exception"], "EXC_CRASH");
        assert_eq!(event["signal"], "SIGABRT");
        assert_eq!(event["message"], "abort() called");
        assert_eq!(event["termination"], "Abort trap: 6");
        assert_eq!(event["thread"], "com.apple.main-thread");
        assert_eq!(
            event["stack"],
            json!([
                "0 libsystem_kernel.dylib __pthread_kill + 8",
                "1 MyApp +0x1234",
                "2 ??? +0x1"
            ])
        );
        assert_eq!(event["report"]["owner"], "mobile");
        // Without abort messages the exception's subtype says why.
        let mut body: Value =
            serde_json::from_str(ips("MyApp", "x").split_once('\n').unwrap().1).unwrap();
        body.as_object_mut().unwrap().remove("asi");
        body["exception"]["subtype"] = json!("KERN_INVALID_ADDRESS at 0x0");
        let text = format!("{{}}\n{body}");
        let event = crash_event(text.as_bytes(), "n", "root").unwrap();
        assert_eq!(event["message"], "KERN_INVALID_ADDRESS at 0x0");
        assert_eq!(event["time"], "x");
        // A report that is no crash, or no report at all.
        assert_eq!(
            crash_event(b"{}\n{\"procName\":\"x\"}", "n", "mobile"),
            Err("no-exception")
        );
        assert_eq!(
            crash_event(b"Process: x", "n", "mobile"),
            Err("unsupported-report-format")
        );
    }

    #[test]
    fn testing_crashes_reads_the_guests_reports_and_selects_by_app_and_time() {
        let mut partial = reply(&[("MyApp-b.ips", ips("MyApp", "2026-10-13 07:12:27 +0000"))]);
        partial["response"]["reports"][0]["content_complete"] = json!(false);
        let mut phone = Recorded {
            guest: reply(&[
                (
                    "MyApp-2026-10-13-071227.ips",
                    ips("MyApp", "2026-10-13 07:12:27 +0000"),
                ),
                (
                    "MyApp-2026-10-12-000000.ips",
                    ips("MyApp", "2026-10-12 00:00:00 +0000"),
                ),
                (
                    "MyAppHelper-2026-10-13.ips",
                    ips("MyAppHelper", "2026-10-13 08:00:00 +0000"),
                ),
                ("MyApp-broken.ips", "not a report".into()),
            ]),
            ..Default::default()
        };
        phone.guest["response"]["reports"]
            .as_array_mut()
            .unwrap()
            .push(partial["response"]["reports"][0].clone());
        let all = crashes(&mut phone, &json!({"device":DEVICE,"process":"MyApp"})).unwrap();
        assert_eq!(
            phone.calls[0],
            json!({"operation":"crash_reports","argument":"MyApp","timeout_seconds":60})
        );
        assert_eq!(all["counts"]["crash"], 3);
        assert_eq!(all["crashed"], true);
        assert_eq!(all["absence_proves_no_crash"], false);
        let reasons: Vec<&str> = all["unparsed"]
            .as_array()
            .unwrap()
            .iter()
            .map(|row| s(row, "reason"))
            .collect();
        assert_eq!(reasons, ["unsupported-report-format", "incomplete-content"]);
        let one = crashes(
            &mut phone,
            &json!({"device":DEVICE,"process":"MyApp","bundle_id":"dev.example.MyApp","since":1_791_800_000.0}),
        )
        .unwrap();
        assert_eq!(one["counts"]["crash"], 1);
        assert_eq!(
            one["events"][0]["report"]["name"],
            "MyApp-2026-10-13-071227.ips"
        );
        let directory = tempfile::tempdir().unwrap();
        let report = directory.path().join("crashes.json");
        crashes(
            &mut phone,
            &json!({"device":DEVICE,"process":"MyApp","report":report}),
        )
        .unwrap();
        assert_eq!(read_json(&report, None).unwrap()["counts"]["crash"], 3);
        // A process is required, and must be a report name prefix.
        for refused in [
            json!({"device":DEVICE}),
            json!({"device":DEVICE,"process":"../x"}),
        ] {
            assert_eq!(
                crashes(&mut phone, &refused).unwrap_err().code,
                "invalid_argument"
            );
        }
        phone.guest = json!({"response":{"ok":false}});
        assert_eq!(
            crashes(&mut phone, &json!({"device":DEVICE,"process":"MyApp"}))
                .unwrap_err()
                .code,
            "ios_guest_crash_reports_failed"
        );
    }

    #[test]
    fn a_screenshot_of_the_phone_can_be_compared() {
        // ios.virtual.screenshot's PNG is one testing.visual_diff reads.
        let image = crate::visual::decode(&png(3, 2, [1, 2, 3])).unwrap();
        assert_eq!((image.width, image.height), (3, 2));
        assert_eq!(&image.pixels[..4], &[1, 2, 3, 255]);
    }
}
