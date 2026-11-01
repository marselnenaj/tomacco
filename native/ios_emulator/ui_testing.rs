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
    json!({"ui_hierarchy":false,"selectors":false,"run_coordinates":true,"run_selectors":false,"run_screens":true,"run_launch":true,"run_stop_clear":false,"visual_diff":true,"lint":false,"accessibility":false,"unicode_text":false})
}

/// The running phone as the scenario runner uses it. Tests replace it with recorded replies.
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
    fn a_screenshot_of_the_phone_can_be_compared() {
        // ios.virtual.screenshot's PNG is one testing.visual_diff reads.
        let image = crate::visual::decode(&png(3, 2, [1, 2, 3])).unwrap();
        assert_eq!((image.width, image.height), (3, 2));
        assert_eq!(&image.pixels[..4], &[1, 2, 3, 255]);
    }
}
