//! profile.startup on the virtual iPhone: launches opened through the guest
//! service's `open` (FrontBoard), timed on frames read through
//! `ios.emulator.control`, with the guest's own operations to end the app
//! (`restart_ui`) or send it to the background (Home). The measuring itself is
//! ios_startup's, which a USB iPhone shares.
use super::{control, guest_control, ui_testing, virtual_phone};
use crate::core::*;
use crate::ios_startup::{self, Frame, Launcher, Opened, Opener, Settings, Signature};
use serde_json::{Value, json};
use std::path::Path;
use std::time::{Duration, Instant};

const NOTE: &str = "Measured on the virtual iPhone, whose guest runs on an emulated processor with a software renderer: \
launches take far longer than on an iPhone, and these times say nothing about one. Compare virtual-iPhone reports with \
each other only. Each time is bounded below by one frame read (resolution_ms). cold restarts iOS's interface (restart_ui), \
which on the phones tried left the app's process running: check launch_state and mismatched_runs, a cold run that says hot \
did not start a new process. System caches stay warm, so it is not a launch after a reboot.";

/// How long a guest request waits for an earlier one to end.
const BUSY_PATIENCE: Duration = Duration::from_secs(120);

/// A guest call, asked again while the guest or the host lane is busy with an
/// earlier request: that refusal comes before any work, so asking again is safe.
fn guest(job: &str, operation: &str, argument: Option<&str>, timeout: u64) -> Result {
    let mut request = json!({"job_id":job,"operation":operation,"timeout_seconds":timeout});
    if let Some(argument) = argument {
        request["argument"] = json!(argument);
    }
    let deadline = Instant::now() + BUSY_PATIENCE;
    loop {
        check_cancelled()?;
        let busy = match guest_control::call(&request) {
            Ok(reply) if s(&reply["response"], "error") == "guest_operation_busy" => reply,
            Err(error) if error.code == "ios_guest_busy" => json!({"error":error.code}),
            other => return other,
        };
        if Instant::now() >= deadline {
            return Err(Error::new(
                "ios_guest_busy",
                format!("The guest stayed busy with an earlier request instead of {operation}"),
            )
            .details(busy));
        }
        std::thread::sleep(Duration::from_secs(2));
    }
}

/// The open reply as a launch: accepted, and the app's process when FrontBoard
/// named it (`launch_process_pid`, guest tools with launch process observation).
pub fn opened(reply: &Value, completed_ms: f64) -> Result<Opened> {
    let response = &reply["response"];
    if response["ok"] != true || response["request_accepted"] != true {
        return Err(
            Error::new("app_action_failed", "The guest did not open the app")
                .remedy("Check that the app is installed with ios.virtual.deploy and opens with device.app or testing.run's launch step")
                .details(response.clone()),
        );
    }
    let pid = if response["launch_process_pid_known"] == true {
        response["launch_process_pid"].as_u64()
    } else {
        None
    };
    Ok(Opened {
        completed_ms,
        pid,
        reply: json!({"request_accepted":true,
            "launch_process_observation_state":response["launch_process_observation_state"],
            "launch_process_start_snapshot":response["launch_process_start_snapshot"]}),
    })
}

/// The picture a frame reply names: the PPM file of the file transport, or
/// the PNG of an inline frame.
fn picture(result: &Value) -> Result<Signature> {
    let unreadable = || Error::new("invalid_capture", "The frame could not be read");
    if let Some(file) = result["path"].as_str() {
        let data = std::fs::read(Path::new(file))?;
        return ios_startup::signature_ppm(&data).ok_or_else(unreadable);
    }
    let url = s(result, "data_url");
    let encoded = url
        .strip_prefix("data:image/png;base64,")
        .ok_or_else(unreadable)?;
    use base64::Engine;
    let data = base64::engine::general_purpose::STANDARD
        .decode(encoded)
        .map_err(|_| unreadable())?;
    let image = crate::visual::decode(&data)?;
    ios_startup::signature(
        &image.pixels,
        image.width as usize,
        image.height as usize,
        4,
    )
    .ok_or_else(unreadable)
}

struct VirtualIphone {
    job: String,
    bundle: String,
    base: Instant,
    /// The frame file last read: a reply naming it again is the same picture.
    last_path: Option<String>,
    inline: bool,
}

impl VirtualIphone {
    fn control(&self, action: &str) -> Result {
        control::handle(&json!({"job_id":self.job,"action":action}))
    }

    fn frontmost(&self) -> Result<Option<String>> {
        let reply = guest(&self.job, "foreground_state", None, 30)?;
        let response = &reply["response"];
        Ok((response["foreground_identifier_known"] == true)
            .then(|| s(response, "frontmost_bundle_id").to_string()))
    }
}

impl Launcher for VirtualIphone {
    fn now_ms(&self) -> f64 {
        self.base.elapsed().as_secs_f64() * 1000.0
    }

    fn frame(&mut self) -> Result<Frame> {
        if !self.inline {
            match control::handle(&json!({"job_id":self.job,"action":"frame","transport":"file"})) {
                Ok(reply) => {
                    let at_ms = self.now_ms();
                    let result = &reply["result"];
                    let path = s(result, "path").to_string();
                    if self.last_path.as_deref() == Some(path.as_str()) {
                        return Ok(Frame {
                            at_ms,
                            picture: None,
                            sample: true,
                        });
                    }
                    let picture = picture(result)?;
                    self.last_path = Some(path);
                    return Ok(Frame {
                        at_ms,
                        picture: Some(picture),
                        sample: true,
                    });
                }
                Err(error) if error.code == "ios_frame_file_unavailable" => self.inline = true,
                Err(error) => return Err(error),
            }
        }
        let reply = self.control("frame")?;
        let at_ms = self.now_ms();
        Ok(Frame {
            at_ms,
            picture: Some(picture(&reply["result"])?),
            sample: true,
        })
    }

    fn prepare(&mut self) -> Result<()> {
        // An open request is refused on the lock screen, which the phone
        // returns to after a few idle minutes. The guest answers "unknown"
        // now and then for a few seconds, also after the interface restarts.
        virtual_phone::ensure_unlocked_within(Duration::from_secs(30))?;
        Ok(())
    }

    fn terminate(&mut self) -> Result<Value> {
        let reply = guest(&self.job, "restart_ui", None, 120)?;
        let response = &reply["response"];
        if response["ok"] != true {
            return Err(Error::new(
                "app_action_failed",
                "The guest did not restart iOS's interface, which ends the app before a cold launch",
            )
            .details(response.clone()));
        }
        // The interface comes back on the lock screen, and cannot say so for
        // a while: wait for its answer instead of opening into a locked phone.
        virtual_phone::ensure_unlocked_within(Duration::from_secs(90))?;
        self.control("home")?;
        Ok(
            json!({"action":"restart_ui","previous_ui_pid":response["previous_pid"],"ui_pid":response["ui_pid"]}),
        )
    }

    fn background(&mut self) -> Result<Value> {
        self.control("home")?;
        let deadline = Instant::now() + Duration::from_secs(60);
        while self.frontmost()?.as_deref() == Some(self.bundle.as_str()) {
            if Instant::now() >= deadline {
                return Err(Error::new(
                    "app_action_failed",
                    "The app stayed in front after Home",
                ));
            }
            std::thread::sleep(Duration::from_secs(1));
        }
        Ok(json!({"action":"home"}))
    }

    fn opener(&mut self) -> Opener {
        let (job, bundle, base) = (self.job.clone(), self.bundle.clone(), self.base);
        Box::new(move || {
            let reply = guest(&job, "open", Some(&bundle), 120)?;
            opened(&reply, base.elapsed().as_secs_f64() * 1000.0)
        })
    }
}

/// profile.startup with device `virtual-iphone`.
pub fn measure(a: &Value) -> Result {
    let id = identifier(
        s(a, "bundle_id"),
        "application ID",
        Some(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+"),
    )?;
    if !s(a, "activity").is_empty() {
        return Err(Error::new(
            "invalid_argument",
            "activity is an Android argument; an iPhone app opens at its own first screen",
        ));
    }
    if !matches!(s(a, "platform"), "" | "ios") {
        return Err(Error::new(
            "invalid_argument",
            "The virtual iPhone is an iOS device; leave platform out or pass ios",
        ));
    }
    let settings = Settings {
        mode: ios_startup::mode(a)?,
        runs: a["runs"].as_u64().unwrap_or(5).clamp(1, 30),
        settle_ms: ios_startup::settle_ms(a),
        // Emulated launches are slow: minutes are not unusual for a first one.
        launch_timeout_ms: 300_000.0,
        quiet_timeout_ms: 120_000.0,
    };
    let (job, _) = virtual_phone::running_guest()?;
    let mut phone = VirtualIphone {
        job,
        bundle: id.to_string(),
        base: Instant::now(),
        last_path: None,
        inline: false,
    };
    let runs = ios_startup::measure(&mut phone, &settings)?;
    let value = ios_startup::report(
        ui_testing::DEVICE,
        "virtual-iphone",
        id,
        &settings,
        &runs,
        NOTE,
    );
    crate::perf::finish(a, value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_open_reply_names_the_process_only_when_frontboard_did() {
        // The fields guest-launch-process.h writes into an accepted open.
        let reply = json!({"response":{"ok":true,"operation":"open","request_accepted":true,
            "worker_pid":88,"pid_role":"request-worker","launch_process_pid_known":true,"launch_process_pid":412,
            "launch_process_observation_state":"pid-and-start-snapshot",
            "launch_process_start_snapshot":{"pid":412,"start_seconds":1793000000,"start_microseconds":5}}});
        let launch = opened(&reply, 812.5).unwrap();
        assert_eq!((launch.pid, launch.completed_ms), (Some(412), 812.5));
        assert_eq!(launch.reply["launch_process_start_snapshot"]["pid"], 412);
        // Older guest tools name only their worker, which is not the app.
        let older = json!({"response":{"ok":true,"request_accepted":true,"pid":88}});
        assert_eq!(opened(&older, 1.0).unwrap().pid, None);
        let refused = json!({"response":{"ok":false,"request_accepted":false,"error":"request_rejected_by_ios"}});
        let error = opened(&refused, 1.0).err().unwrap();
        assert_eq!(error.code, "app_action_failed");
        assert_eq!(
            error.details.as_ref().unwrap()["error"],
            "request_rejected_by_ios"
        );
    }

    #[test]
    fn a_frame_file_and_an_inline_frame_give_the_same_grid() {
        let directory = tempfile::tempdir().unwrap();
        let (width, height) = (64u32, 128u32);
        let mut ppm = format!("P6\n{width} {height}\n255\n").into_bytes();
        let mut rgba = Vec::new();
        for y in 0..height {
            for _ in 0..width {
                let grey = if y < 64 { 30 } else { 220 };
                ppm.extend_from_slice(&[grey, grey, grey]);
                rgba.extend_from_slice(&[grey, grey, grey, 255]);
            }
        }
        let file = directory.path().join("frame-7.m42.ppm");
        std::fs::write(&file, &ppm).unwrap();
        let from_file = picture(&json!({"path":file})).unwrap();
        let png = crate::visual::encode(&crate::visual::Image {
            width,
            height,
            pixels: rgba,
        })
        .unwrap();
        use base64::Engine;
        let url = format!(
            "data:image/png;base64,{}",
            base64::engine::general_purpose::STANDARD.encode(png)
        );
        assert_eq!(picture(&json!({"data_url":url})).unwrap(), from_file);
        assert_eq!((from_file[0], from_file[63 * 32]), (30, 220));
        assert!(picture(&json!({"data_url":"data:image/jpeg;base64,AAAA"})).is_err());
    }

    #[test]
    fn the_virtual_iphone_refuses_android_arguments_before_reaching_the_phone() {
        let error = measure(
            &json!({"device":"virtual-iphone","bundle_id":"dev.example.app","activity":".Main"}),
        )
        .err()
        .unwrap();
        assert_eq!(error.code, "invalid_argument");
        let error = measure(
            &json!({"device":"virtual-iphone","bundle_id":"dev.example.app","mode":"warm"}),
        )
        .err()
        .unwrap();
        assert_eq!(error.code, "invalid_argument");
        assert!(error.message.contains("cold or hot"));
    }
}
