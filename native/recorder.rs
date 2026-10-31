//! Recording a UI test: while a recording runs for an Android device, the
//! taps, swipes, text and keys sent to it through android.input,
//! runtime.input (the desktop's live display included) and testing.action
//! become the steps of a testing.run scenario. A tap becomes a click on the
//! element under it when the UI hierarchy names that element uniquely, and a
//! tap at coordinates otherwise.
use crate::{automation, core::*};
use serde_json::{Value, json};
use std::path::PathBuf;

/// testing.run takes at most this many steps.
const STEP_LIMIT: usize = 500;

/// Where recordings live: one file per device, and its lock beside it.
pub struct Store(PathBuf);
impl Store {
    pub fn home() -> Result<Self> {
        Ok(Store(home()?.join("recordings")))
    }
    fn file(&self, device: &str) -> PathBuf {
        self.0
            .join(format!("{}.json", &digest(device.as_bytes())[..16]))
    }
    fn lock(&self, device: &str) -> Result<Lock> {
        private_dir(&self.0)?;
        let lock = Lock::open(
            &self
                .0
                .join(format!("{}.lock", &digest(device.as_bytes())[..16])),
        )?;
        lock.acquire(false)?;
        Ok(lock)
    }
    /// Whether a recording runs for this device; one file test, so input
    /// that nobody records pays nothing more.
    pub fn active(&self, device: &str) -> bool {
        self.file(device).exists()
    }
    /// testing.record: start, status, stop (writing the scenario) or discard.
    pub fn handle(&self, a: &Value) -> Result {
        let device = identifier(s(a, "device"), "device", None)?;
        let path = self.file(device);
        let _lock = self.lock(device)?;
        let current = if path.exists() {
            Some(read_json(&path, None)?)
        } else {
            None
        };
        match s(a, "action") {
            "start" => {
                if let Some(current) = current {
                    return Err(Error::new(
                        "recording_active",
                        "A recording already runs for this device",
                    )
                    .remedy("Stop it with action stop, or throw it away with action discard")
                    .details(summary(&current)));
                }
                let name = match s(a, "name") {
                    "" => format!("Recorded on {device}"),
                    name => name.to_string(),
                };
                let value =
                    json!({"device":device,"name":name,"started":now(),"steps":[],"skipped":[]});
                atomic_json(&path, &value)?;
                Ok(summary(&value))
            }
            "status" => Ok(current
                .map(|value| summary(&value))
                .unwrap_or_else(|| json!({"recording":false,"device":device}))),
            "discard" => {
                if current.is_some() {
                    std::fs::remove_file(&path)?;
                }
                Ok(json!({"recording":false,"device":device,"discarded":current.is_some()}))
            }
            _ => {
                let current = current.ok_or_else(|| {
                    Error::new("recording_not_active", "No recording runs for this device")
                        .remedy("Start one with action start")
                })?;
                if s(a, "path").is_empty() {
                    return Err(Error::new(
                        "invalid_argument",
                        "stop requires path, the scenario file to write",
                    ));
                }
                let target = output_path(s(a, "path"))?;
                if target.exists() && !b(a, "overwrite") {
                    return Err(
                        Error::new("path_exists", format!("{} exists", target.display()))
                            .remedy("Choose another path, or pass overwrite:true"),
                    );
                }
                let steps = current["steps"].as_array().cloned().unwrap_or_default();
                if steps.is_empty() {
                    return Err(Error::new(
                        "recording_empty",
                        "Nothing was recorded; the recording keeps running",
                    )
                    .remedy("Send input to the device, or discard the recording"));
                }
                let scenario = json!({"name":current["name"],"steps":steps});
                atomic_json(&target, &scenario)?;
                std::fs::remove_file(&path)?;
                let mut result = summary(&current);
                result["recording"] = json!(false);
                result["path"] = json!(target);
                result["scenario"] = scenario;
                Ok(result)
            }
        }
    }
    /// Adds a step to the device's recording, or notes input a scenario
    /// cannot express (`Err(what)`), if a recording runs.
    pub fn append(&self, device: &str, step: std::result::Result<Value, &str>) -> Result<()> {
        if !self.active(device) {
            return Ok(());
        }
        let _lock = self.lock(device)?;
        let path = self.file(device);
        // Stopped in the meantime.
        if !path.exists() {
            return Ok(());
        }
        let mut value = read_json(&path, None)?;
        let step = match step {
            Ok(step) if value["steps"].as_array().map_or(0, Vec::len) < STEP_LIMIT => {
                value["steps"].as_array_mut().unwrap().push(step);
                None
            }
            Ok(_) => Some("step_limit"),
            Err(what) => Some(what),
        };
        if let Some(what) = step {
            let skipped = value["skipped"].as_array_mut().unwrap();
            if skipped.len() < 100 {
                skipped.push(json!(what));
            }
        }
        atomic_json(&path, &value)
    }
}
pub fn handle(a: &Value) -> Result {
    Store::home()?.handle(a)
}
fn summary(value: &Value) -> Value {
    let steps = value["steps"].as_array().map_or(0, Vec::len);
    let coordinates = value["steps"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|step| ["tap", "swipe"].contains(&s(step, "action")))
        .count();
    json!({"recording":true,"device":value["device"],"name":value["name"],"started":value["started"],
           "steps":steps,"coordinate_steps":coordinates,"skipped":value["skipped"]})
}

struct Node {
    depth: usize,
    rect: [i64; 4],
    clickable: bool,
    attributes: Vec<(String, String)>,
}
impl Node {
    fn get(&self, name: &str) -> &str {
        self.attributes
            .iter()
            .find(|(key, _)| key == name)
            .map_or("", |(_, value)| value)
    }
    fn contains(&self, x: i64, y: i64) -> bool {
        self.rect[0] <= x && x < self.rect[2] && self.rect[1] <= y && y < self.rect[3]
    }
    fn inside(&self, outer: &Node) -> bool {
        self.rect[0] >= outer.rect[0]
            && self.rect[1] >= outer.rect[1]
            && self.rect[2] <= outer.rect[2]
            && self.rect[3] <= outer.rect[3]
    }
}
fn nodes(xml: &str) -> Vec<Node> {
    let Ok(tree) = roxmltree::Document::parse(xml) else {
        return Vec::new();
    };
    let bounds = regex::Regex::new(r"^\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]$").unwrap();
    tree.descendants()
        .filter(|n| n.has_tag_name("node"))
        .filter_map(|n| {
            let c = bounds.captures(n.attribute("bounds")?)?;
            let rect = [1, 2, 3, 4].map(|i| c[i].parse::<i64>().unwrap_or(0));
            Some(Node {
                depth: n.ancestors().count(),
                rect,
                clickable: n.attribute("clickable") == Some("true")
                    || n.attribute("long-clickable") == Some("true"),
                attributes: n
                    .attributes()
                    .map(|a| (a.name().to_string(), a.value().to_string()))
                    .collect(),
            })
        })
        .collect()
}
/// The screen's size in pixels: the extent of the hierarchy's windows.
pub fn screen(xml: &str) -> Option<(i64, i64)> {
    let all = nodes(xml);
    let top = all.iter().map(|n| n.depth).min()?;
    let (width, height) = all
        .iter()
        .filter(|n| n.depth == top)
        .fold((0, 0), |(w, h), n| (w.max(n.rect[2]), h.max(n.rect[3])));
    (width > 0 && height > 0).then_some((width, height))
}
/// A selector that testing.action resolves to the element under (x, y): the
/// deepest clickable element there, or a labelled element inside it, named by
/// resource ID, description or text, with an index when the name alone is
/// shared. None when nothing there has a name.
pub fn selector_at(xml: &str, x: i64, y: i64) -> Option<Value> {
    let all = nodes(xml);
    let mut under: Vec<&Node> = all.iter().filter(|n| n.contains(x, y)).collect();
    under.sort_by_key(|n| std::cmp::Reverse(n.depth));
    let target = under
        .iter()
        .find(|n| n.clickable)
        .or(under.first())
        .copied()?;
    let mut indexed = None;
    for node in under.iter().filter(|n| n.inside(target)) {
        for keys in [
            &["resource_id"][..],
            &["description"],
            &["text"],
            &["resource_id", "text"],
            &["resource_id", "description"],
        ] {
            let mut selector = json!({});
            for key in keys {
                let attribute = match *key {
                    "resource_id" => "resource-id",
                    "description" => "content-desc",
                    other => other,
                };
                let value = node.get(attribute);
                if value.is_empty() {
                    selector = Value::Null;
                    break;
                }
                selector[*key] = json!(value);
            }
            if selector.is_null() {
                continue;
            }
            let Ok(matches) = automation::select(xml, &selector) else {
                continue;
            };
            if matches.len() == 1 {
                return Some(selector);
            }
            if indexed.is_none() {
                if let Some(index) = matches
                    .iter()
                    .position(|m| s(m, "bounds") == node.get("bounds"))
                {
                    selector["index"] = json!(index);
                    indexed = Some(selector);
                }
            }
        }
    }
    indexed
}
/// The step for a tap at pixel (x, y) on the screen `xml` describes.
pub fn tap_step(xml: Option<&str>, x: i64, y: i64, long: Option<u64>) -> Value {
    match (xml.and_then(|xml| selector_at(xml, x, y)), long) {
        (Some(selector), None) => json!({"action":"click","selector":selector}),
        (Some(selector), Some(_)) => json!({"action":"long_click","selector":selector}),
        (None, None) => json!({"action":"tap","x":x,"y":y}),
        (None, Some(duration)) => {
            json!({"action":"swipe","x":x,"y":y,"end_x":x,"end_y":y,"duration":duration})
        }
    }
}
/// Android key names of runtime.input, as testing.run's key step takes them.
pub fn keycode(key: &str) -> Option<&'static str> {
    Some(match key {
        "home" => "KEYCODE_HOME",
        "back" | "escape" => "KEYCODE_BACK",
        "up" => "KEYCODE_DPAD_UP",
        "down" => "KEYCODE_DPAD_DOWN",
        "left" => "KEYCODE_DPAD_LEFT",
        "right" => "KEYCODE_DPAD_RIGHT",
        "enter" => "KEYCODE_ENTER",
        "backspace" => "KEYCODE_DEL",
        "delete" => "KEYCODE_FORWARD_DEL",
        "tab" => "KEYCODE_TAB",
        "power" => "KEYCODE_POWER",
        "volume_up" => "KEYCODE_VOLUME_UP",
        "volume_down" => "KEYCODE_VOLUME_DOWN",
        "apps" => "KEYCODE_APP_SWITCH",
        _ => return None,
    })
}

type Step = std::result::Result<Value, &'static str>;

/// android.input, recorded when a recording runs: the hierarchy is read
/// before a tap, so the step names what was under the finger.
pub fn android_input(
    store: &Store,
    a: &Value,
    hierarchy: impl FnOnce() -> Option<String>,
    deliver: impl FnOnce(&Value) -> Result,
) -> Result {
    let device = s(a, "device");
    if !store.active(device) {
        return deliver(a);
    }
    let x = n(a, "x") as i64;
    let y = n(a, "y") as i64;
    let step: Step = match s(a, "action") {
        "tap" => Ok(tap_step(hierarchy().as_deref(), x, y, None)),
        "swipe" if (x, y) == (n(a, "end_x") as i64, n(a, "end_y") as i64) => Ok(tap_step(
            hierarchy().as_deref(),
            x,
            y,
            Some(n(a, "duration")),
        )),
        "swipe" => Ok(
            json!({"action":"swipe","x":x,"y":y,"end_x":a["end_x"],"end_y":a["end_y"],"duration":a["duration"]}),
        ),
        "text" => Ok(json!({"action":"text","text":a["text"]})),
        "keyevent" => Ok(json!({"action":"key","key":a["key"]})),
        _ => Err("unsupported"),
    };
    let result = deliver(a)?;
    store.append(device, step)?;
    Ok(result)
}
/// testing.action, recorded with the selector it was given.
pub fn testing_action(store: &Store, a: &Value, deliver: impl FnOnce(&Value) -> Result) -> Result {
    let device = s(a, "device");
    let result = deliver(a)?;
    let mut step = json!({"action":a["action"],"selector":a["selector"]});
    if s(a, "action") == "fill" {
        step["text"] = a["text"].clone();
    }
    store.append(device, Ok(step))?;
    Ok(result)
}
/// The screen of an Android device now, for android.input.
pub fn hierarchy(device: &str) -> Option<String> {
    crate::devices::handle("android.hierarchy", &json!({"device":device}))
        .ok()
        .map(|value| s(&value, "xml").to_string())
        .filter(|xml| !xml.is_empty())
}

/// runtime.input on an Android display session. Coordinates there are 0..1
/// of the screen, and the desktop sends a touch as down, moves and up; the
/// session's worker keeps the touch in progress here.
pub struct Live {
    store: Store,
    device: String,
    touch: Option<Touch>,
}
struct Touch {
    x: f64,
    y: f64,
    last: (f64, f64),
    started: std::time::Instant,
    xml: Option<String>,
}
impl Live {
    pub fn new(store: Store, device: &str) -> Self {
        Live {
            store,
            device: device.to_string(),
            touch: None,
        }
    }
    /// Called before the input is delivered, with the session's hierarchy
    /// reader, so that a tap is matched against the screen it lands on.
    /// Returns what to record once the input was delivered.
    pub fn before(
        &mut self,
        a: &Value,
        hierarchy: impl FnOnce() -> Option<String>,
    ) -> Option<Step> {
        if !self.store.active(&self.device) {
            self.touch = None;
            return None;
        }
        let x = a["x"].as_f64().unwrap_or(0.0);
        let y = a["y"].as_f64().unwrap_or(0.0);
        match s(a, "action") {
            "tap" => Some(normalized_tap(hierarchy().as_deref(), x, y, None)),
            "swipe" => Some(normalized_swipe(
                hierarchy().as_deref(),
                (x, y),
                (
                    a["x2"].as_f64().unwrap_or(0.0),
                    a["y2"].as_f64().unwrap_or(0.0),
                ),
                a["duration"].as_f64().unwrap_or(0.3),
            )),
            "touch" => match s(a, "phase") {
                "down" => {
                    self.touch = Some(Touch {
                        x,
                        y,
                        last: (x, y),
                        started: std::time::Instant::now(),
                        xml: hierarchy(),
                    });
                    None
                }
                "move" => {
                    if let Some(touch) = self.touch.as_mut() {
                        touch.last = (x, y);
                    }
                    None
                }
                "up" => self.touch.take().map(|touch| touch_step(&touch, (x, y))),
                _ => {
                    self.touch = None;
                    None
                }
            },
            "key" => Some(
                keycode(s(a, "key"))
                    .map(|key| json!({"action":"key","key":key}))
                    .ok_or("key"),
            ),
            "text" => Some(Ok(json!({"action":"text","text":a["text"]}))),
            "rotate" => Some(Err("rotate")),
            _ => Some(Err("unsupported")),
        }
    }
    pub fn after(&self, step: Option<Step>) {
        if let Some(step) = step {
            let _ = self.store.append(&self.device, step);
        }
    }
}
/// A touch that hardly moved is a tap, held for half a second a long click;
/// one that moved is a swipe.
fn touch_step(touch: &Touch, end: (f64, f64)) -> Step {
    let held = touch.started.elapsed().as_secs_f64();
    let moved = ((end.0 - touch.x).powi(2) + (end.1 - touch.y).powi(2)).sqrt();
    if moved < 0.02 {
        let long = (held >= 0.5).then_some(held);
        normalized_tap(touch.xml.as_deref(), touch.x, touch.y, long)
    } else {
        normalized_swipe(touch.xml.as_deref(), (touch.x, touch.y), end, held)
    }
}
/// Without a hierarchy the screen size is unknown, so a point in 0..1
/// cannot become pixels.
fn normalized_tap(xml: Option<&str>, x: f64, y: f64, long: Option<f64>) -> Step {
    let (width, height) = xml.and_then(screen).ok_or("no_hierarchy")?;
    let px = ((x * width as f64) as i64).clamp(0, width - 1);
    let py = ((y * height as f64) as i64).clamp(0, height - 1);
    Ok(tap_step(xml, px, py, long.map(milliseconds)))
}
fn normalized_swipe(xml: Option<&str>, from: (f64, f64), to: (f64, f64), seconds: f64) -> Step {
    let (width, height) = xml.and_then(screen).ok_or("no_hierarchy")?;
    let px = |v: f64, size: i64| ((v * size as f64) as i64).clamp(0, size - 1);
    Ok(
        json!({"action":"swipe","x":px(from.0,width),"y":px(from.1,height),"end_x":px(to.0,width),"end_y":px(to.1,height),"duration":milliseconds(seconds)}),
    )
}
fn milliseconds(seconds: f64) -> u64 {
    ((seconds * 1000.0).round() as u64).clamp(1, 10000)
}

#[cfg(test)]
mod tests {
    use super::*;
    const SCREEN: &str = r#"<hierarchy rotation="0">
<node class="android.widget.FrameLayout" package="dev.example" clickable="false" bounds="[0,0][1080,2400]">
  <node class="android.widget.LinearLayout" clickable="true" resource-id="" bounds="[0,200][1080,400]">
    <node class="android.widget.TextView" text="Settings" resource-id="" clickable="false" bounds="[40,250][400,350]"/>
  </node>
  <node class="android.widget.Button" text="Add" resource-id="dev.example:id/add" clickable="true" bounds="[0,500][540,600]"/>
  <node class="android.widget.Button" text="OK" resource-id="" clickable="true" bounds="[0,700][540,800]"/>
  <node class="android.widget.Button" text="OK" resource-id="" clickable="true" bounds="[540,700][1080,800]"/>
  <node class="android.view.View" text="" resource-id="" clickable="true" bounds="[0,900][1080,1000]"/>
</node>
</hierarchy>"#;

    #[test]
    fn a_tap_names_the_element_under_it_or_keeps_its_coordinates() {
        assert_eq!(screen(SCREEN), Some((1080, 2400)));
        // A unique resource ID wins over the text.
        assert_eq!(
            selector_at(SCREEN, 100, 550),
            Some(json!({"resource_id":"dev.example:id/add"}))
        );
        // An unnamed clickable row is named by the label inside it.
        assert_eq!(
            selector_at(SCREEN, 100, 300),
            Some(json!({"text":"Settings"}))
        );
        // A shared name gets the element's index.
        assert_eq!(
            selector_at(SCREEN, 800, 750),
            Some(json!({"text":"OK","index":1}))
        );
        // Nothing named there: coordinates.
        assert_eq!(selector_at(SCREEN, 100, 950), None);
        assert_eq!(
            tap_step(Some(SCREEN), 100, 950, None),
            json!({"action":"tap","x":100,"y":950})
        );
        assert_eq!(
            tap_step(Some(SCREEN), 100, 550, Some(800)),
            json!({"action":"long_click","selector":{"resource_id":"dev.example:id/add"}})
        );
        assert_eq!(
            tap_step(None, 5, 6, Some(700)),
            json!({"action":"swipe","x":5,"y":6,"end_x":5,"end_y":6,"duration":700})
        );
        assert!(selector_at("not xml", 1, 1).is_none());
    }
    #[test]
    fn normalized_touches_become_pixel_steps() {
        let touch = Touch {
            x: 0.1,
            y: 550.0 / 2400.0,
            last: (0.1, 550.0 / 2400.0),
            started: std::time::Instant::now(),
            xml: Some(SCREEN.into()),
        };
        assert_eq!(
            touch_step(&touch, (0.105, 550.0 / 2400.0)),
            Ok(json!({"action":"click","selector":{"resource_id":"dev.example:id/add"}}))
        );
        let swipe = touch_step(&touch, (0.1, 0.9)).unwrap();
        assert_eq!(swipe["action"], "swipe");
        assert_eq!((&swipe["x"], &swipe["end_y"]), (&json!(108), &json!(2160)));
        assert_eq!(normalized_tap(None, 0.5, 0.5, None), Err("no_hierarchy"));
        assert_eq!(keycode("back"), Some("KEYCODE_BACK"));
        assert_eq!(keycode("shake"), None);
    }
    #[test]
    fn a_display_session_records_its_touches_while_a_recording_runs() {
        let root = tempfile::tempdir().unwrap();
        let store = Store(root.path().join("recordings"));
        let mut live = Live::new(Store(root.path().join("recordings")), "emulator-5554");
        let screen = || Some(SCREEN.to_string());
        // Nothing is recorded, and no hierarchy read, without a recording.
        let down = json!({"action":"touch","phase":"down","x":0.1,"y":550.0/2400.0});
        assert!(
            live.before(&down, || panic!("no hierarchy is read"))
                .is_none()
        );
        store
            .handle(&json!({"device":"emulator-5554","action":"start"}))
            .unwrap();
        assert!(live.before(&down, screen).is_none());
        let step = live.before(
            &json!({"action":"touch","phase":"move","x":0.1,"y":0.23}),
            screen,
        );
        assert!(step.is_none());
        let step = live.before(
            &json!({"action":"touch","phase":"up","x":0.1,"y":0.23}),
            screen,
        );
        live.after(step);
        let step = live.before(&json!({"action":"key","key":"back"}), screen);
        live.after(step);
        let step = live.before(&json!({"action":"rotate"}), screen);
        live.after(step);
        let step = live.before(&json!({"action":"tap","x":0.5,"y":0.5}), || None);
        live.after(step);
        let status = store
            .handle(&json!({"device":"emulator-5554","action":"status"}))
            .unwrap();
        assert_eq!(status["steps"], 2);
        assert_eq!(status["skipped"], json!(["rotate", "no_hierarchy"]));
    }
    #[test]
    fn a_recording_starts_collects_steps_and_writes_a_scenario() {
        let root = tempfile::tempdir().unwrap();
        let store = Store(root.path().join("recordings"));
        let call = |a: Value| store.handle(&a);
        assert!(!store.active("emulator-5554"));
        let started =
            call(json!({"device":"emulator-5554","action":"start","name":"Login"})).unwrap();
        assert_eq!(
            (&started["recording"], &started["steps"]),
            (&json!(true), &json!(0))
        );
        assert_eq!(
            call(json!({"device":"emulator-5554","action":"start"}))
                .unwrap_err()
                .code,
            "recording_active"
        );
        let target = root.path().join("login.json");
        let stop = json!({"device":"emulator-5554","action":"stop","path":target});
        assert_eq!(call(stop.clone()).unwrap_err().code, "recording_empty");
        let delivered = std::cell::Cell::new(0);
        let deliver = |_: &Value| {
            delivered.set(delivered.get() + 1);
            Ok(json!({}))
        };
        let input = |a: Value| android_input(&store, &a, || Some(SCREEN.to_string()), deliver);
        input(json!({"device":"emulator-5554","action":"tap","x":100,"y":550})).unwrap();
        input(json!({"device":"emulator-5554","action":"tap","x":100,"y":950})).unwrap();
        input(json!({"device":"emulator-5554","action":"text","text":"me@example.com"})).unwrap();
        input(json!({"device":"emulator-5554","action":"keyevent","key":"KEYCODE_ENTER"})).unwrap();
        testing_action(
            &store,
            &json!({"device":"emulator-5554","action":"click","selector":{"text":"Sign in"}}),
            deliver,
        )
        .unwrap();
        // A failed delivery records nothing.
        assert!(
            android_input(
                &store,
                &json!({"device":"emulator-5554","action":"text","text":"x"}),
                || None,
                |_| Err(Error::new("tool_failed", "x"))
            )
            .is_err()
        );
        // Another device's input is not this recording's, and reads no hierarchy.
        android_input(
            &store,
            &json!({"device":"emulator-5556","action":"tap","x":1,"y":1}),
            || panic!("no hierarchy is read"),
            deliver,
        )
        .unwrap();
        assert_eq!(delivered.get(), 6);
        let status = call(json!({"device":"emulator-5554","action":"status"})).unwrap();
        assert_eq!(
            (&status["steps"], &status["coordinate_steps"]),
            (&json!(5), &json!(1))
        );
        let done = call(stop.clone()).unwrap();
        assert_eq!(done["recording"], false);
        let scenario = read_json(&target, None).unwrap();
        assert_eq!(scenario["name"], "Login");
        assert_eq!(
            scenario["steps"],
            json!([{"action":"click","selector":{"resource_id":"dev.example:id/add"}},
                   {"action":"tap","x":100,"y":950},
                   {"action":"text","text":"me@example.com"},{"action":"key","key":"KEYCODE_ENTER"},
                   {"action":"click","selector":{"text":"Sign in"}}])
        );
        assert!(!store.active("emulator-5554"));
        assert_eq!(call(stop).unwrap_err().code, "recording_not_active");
        // An existing file is kept unless overwrite says otherwise.
        call(json!({"device":"emulator-5554","action":"start"})).unwrap();
        store
            .append(
                "emulator-5554",
                Ok(json!({"action":"key","key":"KEYCODE_BACK"})),
            )
            .unwrap();
        let again = json!({"device":"emulator-5554","action":"stop","path":target});
        assert_eq!(call(again).unwrap_err().code, "path_exists");
        let discarded = call(json!({"device":"emulator-5554","action":"discard"})).unwrap();
        assert_eq!(discarded["discarded"], true);
        assert!(!store.active("emulator-5554"));
    }
}
