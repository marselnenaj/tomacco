//! An offline stand-in device for tests and the benchmark runner: a state
//! machine of recorded frames and hierarchies driven by a scenario file.
//!
//! It exists so the agent control plane (sessions, the control socket,
//! `runtime.observe`, `runtime.actions`, `screen.*` on a session, stale-frame
//! rejection, rotation, focus timing) can be exercised and measured without
//! any hardware, USB or network. It is only reachable when
//! `TOMACCO_FIXTURE_RUNTIME` names the directory that holds the scenarios,
//! and a session on it is labelled `transport: "fixture"` everywhere, so
//! its timings are never mistaken for a device's.
//!
//! `runtime.start` with `device: "fixture:NAME"` reads
//! `$TOMACCO_FIXTURE_RUNTIME/NAME/scenario.json`:
//!
//! ```json
//! {"initial":"home","latency_ms":0,
//!  "states":{"home":{"frame":"home.png","hierarchy":"home.xml",
//!     "app":{"bundle_id":"com.example.app","name":"Example","home_screen":false},
//!     "keyboard":false,"focused":false,"orientation":"portrait",
//!     "taps":[{"left":0,"top":500,"right":1080,"bottom":600,"to":"field","after_ms":150}],
//!     "text_to":"typed","key_to":{"home":"home"},"rotate_to":{"landscape_left":"wide"},
//!     "swipe_to":"scrolled","pinch_to":"zoomed",
//!     "animated":{"frames":["a1.png","a2.png"],"interval_ms":100}}}}
//! ```
//!
//! A transition with `after_ms` becomes visible only after that time, so a
//! caller can tell an acknowledged action from a visible one. Text sent while
//! the state is not `focused` is counted as lost, as a phone would lose it.
//!
//! For the coordination tests a scenario may also set `"journal": true`,
//! which appends every input the fixture accepted to `journal.jsonl` beside
//! it (action, key, phase, worker PID; never text), so inputs can be counted
//! across sessions and processes; `"lose_reply_after"` (an object such as
//! `{"action":"key","key":"home"}`), which accepts a matching input and then
//! answers `runtime_timeout` as if its answer were lost; and
//! `"crash_after"`, which accepts a matching input and ends the worker
//! before it answers or records anything.
use super::*;

pub const PREFIX: &str = "fixture:";

/// Where the scenarios live, when the fixture runtime is enabled at all.
pub fn root() -> Option<PathBuf> {
    std::env::var_os("TOMACCO_FIXTURE_RUNTIME")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
}

pub fn requested(a: &Value) -> bool {
    s(a, "device").starts_with(PREFIX) && s(a, "remote").is_empty()
}

fn err(message: impl Into<String>) -> Error {
    Error::new("fixture_runtime", message)
}

pub fn validate(a: &Value) -> Result<()> {
    let name = s(a, "device").trim_start_matches(PREFIX);
    identifier(name, "fixture name", Some("[A-Za-z0-9][A-Za-z0-9_.-]{0,80}"))?;
    let root = root().ok_or_else(|| {
        err("The fixture runtime is off: TOMACCO_FIXTURE_RUNTIME names no scenario directory")
    })?;
    if !root.join(name).join("scenario.json").is_file() {
        return Err(err(format!("No scenario.json for fixture {name}")));
    }
    Ok(())
}

struct Frame {
    bytes: Vec<u8>,
    width: u32,
    height: u32,
}

pub struct Fixture {
    shared: Shared,
    directory: PathBuf,
    scenario: Value,
    state: String,
    pending: Option<(String, Instant)>,
    entered: Instant,
    latency: Duration,
    frames: std::collections::HashMap<String, Frame>,
    /// What the fixture saw, for tests: inputs, text that reached no focused
    /// field, taps that hit nothing.
    inputs: u64,
    lost_characters: u64,
    missed_taps: u64,
    captures: u64,
}

pub fn connect(a: &Value, shared: Shared, _dir: &Path) -> Result<Fixture> {
    validate(a)?;
    let name = s(a, "device").trim_start_matches(PREFIX).to_string();
    let directory = root().unwrap().join(&name);
    let scenario = read_json(&directory.join("scenario.json"), None)?;
    let initial = s(&scenario, "initial").to_string();
    if scenario["states"][&initial].is_null() {
        return Err(err("scenario.json names no initial state"));
    }
    let latency = Duration::from_millis(scenario["latency_ms"].as_u64().unwrap_or(0).min(10_000));
    let mut fixture = Fixture {
        shared: shared.clone(),
        directory,
        scenario,
        state: initial,
        pending: None,
        entered: Instant::now(),
        latency,
        frames: Default::default(),
        inputs: 0,
        lost_characters: 0,
        missed_taps: 0,
        captures: 0,
    };
    let (width, height) = fixture.size()?;
    shared.lock().unwrap().update(json!({"name":format!("Fixture {name}"),"width":width,"height":height,
        "codec":"none","transport":"fixture","evidence":"fixture","fixture":name}))?;
    Ok(fixture)
}

impl Fixture {
    fn current(&self) -> &Value {
        &self.scenario["states"][&self.state]
    }
    /// Applies a transition whose time has come.
    fn settle(&mut self) {
        if let Some((to, due)) = &self.pending {
            if Instant::now() >= *due {
                self.state = to.clone();
                self.pending = None;
                self.entered = Instant::now();
            }
        }
    }
    fn go(&mut self, to: &str, after_ms: u64) -> Result<()> {
        if self.scenario["states"][to].is_null() {
            return Err(err(format!("The scenario has no state {to}")));
        }
        if after_ms == 0 {
            self.state = to.to_string();
            self.pending = None;
            self.entered = Instant::now();
        } else {
            self.pending = Some((to.to_string(), Instant::now() + Duration::from_millis(after_ms)));
        }
        Ok(())
    }
    fn frame_name(&self) -> String {
        let state = self.current();
        if let Some(frames) = state["animated"]["frames"].as_array().filter(|f| !f.is_empty()) {
            // interval_ms 0 advances on every capture, so a poller always
            // sees a change; otherwise the frame follows the clock.
            let interval = state["animated"]["interval_ms"].as_u64().unwrap_or(100);
            let tick = if interval == 0 {
                self.captures
            } else {
                self.entered.elapsed().as_millis() as u64 / interval
            };
            let index = tick as usize % frames.len();
            return frames[index].as_str().unwrap_or("").to_string();
        }
        s(state, "frame").to_string()
    }
    fn frame(&mut self, name: &str) -> Result<&Frame> {
        if !self.frames.contains_key(name) {
            let file = self.directory.join(name);
            let bytes = fs::read(&file)
                .map_err(|e| err(format!("The frame {name} is unreadable: {e}")))?;
            let (width, height) = crate::screen::png_size(&file)?;
            self.frames.insert(
                name.to_string(),
                Frame {
                    bytes,
                    width,
                    height,
                },
            );
        }
        Ok(&self.frames[name])
    }
    fn size(&mut self) -> Result<(u32, u32)> {
        let name = self.frame_name();
        let frame = self.frame(&name)?;
        Ok((frame.width, frame.height))
    }
    fn pixel(&mut self, a: &Value) -> Result<(i64, i64)> {
        let (width, height) = self.size()?;
        Ok((
            (number(a, "x") * f64::from(width - 1)).round() as i64,
            (number(a, "y") * f64::from(height - 1)).round() as i64,
        ))
    }
    fn tap_at(&mut self, a: &Value) -> Result<()> {
        let (x, y) = self.pixel(a)?;
        let targets = self.current()["taps"].as_array().cloned().unwrap_or_default();
        for target in targets {
            let inside = x >= n(&target, "left") as i64
                && x < n(&target, "right") as i64
                && y >= n(&target, "top") as i64
                && y < n(&target, "bottom") as i64;
            if inside {
                return self.go(s(&target, "to"), n(&target, "after_ms"));
            }
        }
        self.missed_taps += 1;
        Ok(())
    }
    fn follow(&mut self, key: &str) -> Result<()> {
        let to = s(self.current(), key).to_string();
        if !to.is_empty() {
            self.go(&to, 0)?;
        }
        Ok(())
    }
    fn evidence(&self) -> Value {
        json!({"inputs":self.inputs,"captures":self.captures,"lost_characters":self.lost_characters,"missed_taps":self.missed_taps,"state":self.state})
    }
}

impl Fixture {
    /// Whether `a` matches every member of the scenario's knob `name`.
    fn knob(&self, name: &str, a: &Value) -> bool {
        self.scenario[name]
            .as_object()
            .is_some_and(|wanted| !wanted.is_empty() && wanted.iter().all(|(k, v)| &a[k] == v))
    }
    /// What happens after an input was accepted: the journal and the knobs.
    fn accepted(&self, a: &Value) -> Result<()> {
        if self.scenario["journal"] == true {
            use std::io::Write;
            let line = json!({"n":self.inputs,"action":a["action"],"key":a["key"],"phase":a["phase"],
                "pid":std::process::id(),"state":self.state});
            let mut file = fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(self.directory.join("journal.jsonl"))?;
            writeln!(file, "{line}")?;
            file.sync_all()?;
        }
        if self.knob("crash_after", a) {
            // Accepted, then gone: no answer and no record of it.
            std::process::exit(86);
        }
        if self.knob("lose_reply_after", a) {
            return Err(Error::new(
                "runtime_timeout",
                "The fixture accepted the input and withheld its answer",
            ));
        }
        Ok(())
    }
}

impl Device for Fixture {
    fn input(&mut self, a: &Value) -> Result {
        let result = (|| -> Result {
        thread::sleep(self.latency);
        self.settle();
        self.inputs += 1;
        match s(a, "action") {
            "tap" => {
                require(a, &["x", "y"])?;
                self.tap_at(a)?;
            }
            "touch" => {
                require(a, &["x", "y", "phase"])?;
                if s(a, "phase") == "up" {
                    self.tap_at(a)?;
                }
            }
            "swipe" => {
                require(a, &["x", "y", "x2", "y2"])?;
                if number(a, "x") == number(a, "x2") && number(a, "y") == number(a, "y2") {
                    // A press held in place is a long press, as the screen operations send one.
                    self.tap_at(a)?;
                } else {
                    self.follow("swipe_to")?;
                }
            }
            "text" | "fill" => {
                require(a, &["text"])?;
                if b(self.current(), "focused") || s(a, "action") == "fill" {
                    self.follow("text_to")?;
                } else {
                    self.lost_characters += s(a, "text").chars().count() as u64;
                }
            }
            "key" => {
                require(a, &["key"])?;
                let to = s(&self.current()["key_to"], s(a, "key")).to_string();
                if !to.is_empty() {
                    self.go(&to, 0)?;
                }
            }
            "rotate" => {
                let to = s(&self.current()["rotate_to"], s(a, "orientation")).to_string();
                if to.is_empty() {
                    return Err(Error::new(
                        "unsupported_input",
                        "This fixture state has no rotation",
                    ));
                }
                self.go(&to, 0)?;
                let (width, height) = self.size()?;
                self.shared
                    .lock()
                    .unwrap()
                    .update(json!({"width":width,"height":height}))?;
            }
            "pinch" => {
                require(a, &["x", "y", "scale"])?;
                self.follow("pinch_to")?;
            }
            "gesture" => {
                // Replayed with the trace's own timing, as a live transport would.
                let contacts = super::check_gesture(a)?;
                let longest = contacts
                    .iter()
                    .filter_map(|trace| trace.last().map(|p| p.2))
                    .max()
                    .unwrap_or(0);
                let started = Instant::now();
                thread::sleep(Duration::from_millis(longest));
                self.follow("swipe_to")?;
                return Ok(json!({"delivered":true,"mode":"live","contacts":contacts.len(),
                    "elapsed_ms":started.elapsed().as_millis() as u64,"fixture":self.evidence()}));
            }
            _ => {
                return Err(Error::new(
                    "unsupported_input",
                    "This action is not supported by the fixture runtime",
                ));
            }
        }
        Ok(json!({"delivered":true,"fixture":self.evidence()}))
        })();
        if result.is_ok() {
            self.accepted(a)?;
        }
        result
    }
    fn capture(&mut self, path: &Path) -> Result {
        thread::sleep(self.latency);
        self.settle();
        let started = now();
        let name = self.frame_name();
        self.captures += 1;
        let bytes = self.frame(&name)?.bytes.clone();
        publish_png(path, &bytes)?;
        artifact(path, started)
    }
    fn hierarchy(&mut self) -> Result {
        thread::sleep(self.latency);
        self.settle();
        let name = s(self.current(), "hierarchy").to_string();
        if name.is_empty() {
            return Ok(json!({"xml":"","available":false,"transport":"fixture"}));
        }
        let xml = fs::read_to_string(self.directory.join(&name))
            .map_err(|e| err(format!("The hierarchy {name} is unreadable: {e}")))?;
        Ok(json!({"xml":xml,"transport":"fixture"}))
    }
    fn geometry(&mut self) -> Result {
        self.settle();
        let (width, height) = self.size()?;
        let orientation = self.current()["orientation"]
            .as_str()
            .map(String::from)
            .unwrap_or_else(|| derived_orientation(width, height).to_string());
        Ok(json!({"width":width,"height":height,"orientation":orientation,"rotation":Value::Null,
            "scale":1.0,"source":"fixture scenario","coordinate_space":coordinate_space()}))
    }
    fn context(&mut self) -> Result {
        self.settle();
        let state = self.current().clone();
        let app = match state.get("app") {
            Some(app) if app.is_object() => json!({"available":true,"bundle_id":app["bundle_id"],"name":app["name"],
                "home_screen":app.get("home_screen").cloned().unwrap_or(Value::Null),"source":"fixture scenario"}),
            _ => unavailable("This fixture state names no app"),
        };
        let keyboard = match state.get("keyboard").and_then(Value::as_bool) {
            Some(visible) => json!({"available":true,"visible":visible,"source":"fixture scenario"}),
            None => unavailable("This fixture state says nothing about the keyboard"),
        };
        let focus = match state.get("focused").and_then(Value::as_bool) {
            Some(focused) => json!({"available":true,"focused":focused,"source":"fixture scenario"}),
            None => unavailable("This fixture state says nothing about focus"),
        };
        Ok(json!({"app":app,"keyboard":keyboard,"focus":focus,"fixture":self.evidence()}))
    }
    fn capabilities(&self) -> Value {
        json!({"transport":"fixture","evidence":"fixture","capture":true,"hierarchy":true,
            "live_touch":true,"atomic_trace":true,"max_contacts":2,"pinch":true,"fill":true,
            "rotate":true,"exact_scroll":true,"keys":["home","back","enter","backspace"],
            "note":"A scripted stand-in. Nothing it measures is a device's performance."})
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::visual::{self, Image};

    fn solid(width: u32, height: u32, shade: u8) -> Vec<u8> {
        visual::encode(&Image {
            width,
            height,
            pixels: [shade, shade, shade, 255].repeat((width * height) as usize),
        })
        .unwrap()
    }

    /// A scenario directory with a home screen, a field that takes focus
    /// 80 ms after the tap, and a landscape state.
    pub fn scenario(root: &Path) -> PathBuf {
        let dir = root.join("login");
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("home.png"), solid(360, 640, 250)).unwrap();
        fs::write(dir.join("field.png"), solid(360, 640, 200)).unwrap();
        fs::write(dir.join("typed.png"), solid(360, 640, 150)).unwrap();
        fs::write(dir.join("wide.png"), solid(640, 360, 250)).unwrap();
        fs::write(dir.join("home.xml"), "<hierarchy rotation=\"0\"><node class=\"android.widget.FrameLayout\" bounds=\"[0,0][360,640]\"><node class=\"android.widget.EditText\" resource-id=\"app:id/name\" text=\"\" bounds=\"[20,300][340,360]\" clickable=\"true\" focusable=\"true\" enabled=\"true\"/><node class=\"android.widget.Button\" text=\"Sign in\" bounds=\"[20,400][340,460]\" clickable=\"true\" enabled=\"true\"/></node></hierarchy>").unwrap();
        fs::write(dir.join("wide.xml"), "<hierarchy rotation=\"1\"><node class=\"android.widget.FrameLayout\" bounds=\"[0,0][640,360]\"><node class=\"android.widget.Button\" text=\"Sign in\" bounds=\"[200,200][440,260]\" clickable=\"true\" enabled=\"true\"/></node></hierarchy>").unwrap();
        let scenario = json!({"initial":"home","latency_ms":0,"states":{
            "home":{"frame":"home.png","hierarchy":"home.xml","app":{"bundle_id":"com.example.app","name":"Example","home_screen":false},
                "keyboard":false,"focused":false,
                "taps":[{"left":20,"top":300,"right":340,"bottom":360,"to":"field","after_ms":80}],
                "rotate_to":{"landscape_left":"wide"}},
            "field":{"frame":"field.png","hierarchy":"home.xml","app":{"bundle_id":"com.example.app","name":"Example","home_screen":false},
                "keyboard":true,"focused":true,"text_to":"typed","key_to":{"back":"home"}},
            "typed":{"frame":"typed.png","keyboard":true,"focused":true},
            "wide":{"frame":"wide.png","hierarchy":"wide.xml","orientation":"landscape","rotate_to":{"portrait":"home"}}}});
        atomic_json(&dir.join("scenario.json"), &scenario).unwrap();
        dir
    }

    fn hub() -> Shared {
        let dir = tempfile::tempdir().unwrap();
        let state_file = dir.path().join("state.json");
        std::mem::forget(dir);
        Arc::new(Mutex::new(Hub {
            state: json!({}),
            stopped: Arc::new(AtomicBool::new(false)),
            state_file,
            clients: Vec::new(),
            config: Vec::new(),
            pending_config: Vec::new(),
            gop: Vec::new(),
            cached_bytes: 0,
            receiving_config: false,
            cached_key: false,
            viewers: Arc::new(AtomicUsize::new(0)),
        }))
    }

    fn fixture(root: &Path) -> Fixture {
        let shared = hub();
        let scenario = read_json(&root.join("login/scenario.json"), None).unwrap();
        let mut fixture = Fixture {
            shared: shared.clone(),
            directory: root.join("login"),
            scenario,
            state: "home".into(),
            pending: None,
            entered: Instant::now(),
            latency: Duration::ZERO,
            frames: Default::default(),
            inputs: 0,
            lost_characters: 0,
            missed_taps: 0,
            captures: 0,
        };
        let (w, h) = fixture.size().unwrap();
        shared
            .lock()
            .unwrap()
            .update(json!({"width":w,"height":h}))
            .unwrap();
        fixture
    }

    #[test]
    fn text_before_focus_is_lost_and_focus_appears_after_the_tap_delay() {
        let root = tempfile::tempdir().unwrap();
        scenario(root.path());
        let mut device = fixture(root.path());
        assert_eq!(device.context().unwrap()["focus"]["focused"], false);
        device
            .input(&json!({"action":"tap","x":0.5,"y":0.5}))
            .unwrap();
        // Typed at once: the field is not focused yet, so the characters are lost.
        let reply = device
            .input(&json!({"action":"text","text":"ab"}))
            .unwrap();
        assert_eq!(reply["fixture"]["lost_characters"], 2);
        thread::sleep(Duration::from_millis(100));
        let context = device.context().unwrap();
        assert_eq!(context["focus"]["focused"], true);
        assert_eq!(context["keyboard"]["visible"], true);
        assert_eq!(context["app"]["bundle_id"], "com.example.app");
        device
            .input(&json!({"action":"text","text":"cd"}))
            .unwrap();
        assert_eq!(device.context().unwrap()["fixture"]["state"], "typed");
    }

    #[test]
    fn rotation_changes_the_frame_size_and_the_geometry() {
        let root = tempfile::tempdir().unwrap();
        scenario(root.path());
        let mut device = fixture(root.path());
        let before = device.geometry().unwrap();
        assert_eq!((before["width"].as_u64(), before["orientation"].as_str()), (Some(360), Some("portrait")));
        device
            .input(&json!({"action":"rotate","orientation":"landscape_left"}))
            .unwrap();
        let after = device.geometry().unwrap();
        assert_eq!((after["width"].as_u64(), after["orientation"].as_str()), (Some(640), Some("landscape")));
        let out = root.path().join("frame.png");
        device.capture(&out).unwrap();
        assert_eq!(crate::screen::png_size(&out).unwrap(), (640, 360));
        assert!(s(&device.hierarchy().unwrap(), "xml").contains("rotation=\"1\""));
        assert_eq!(device.capabilities()["evidence"], "fixture");
        assert!(
            device
                .input(&json!({"action":"rotate","orientation":"portrait_upside_down"}))
                .is_err()
        );
    }
}
