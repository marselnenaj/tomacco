//! screen.export: one screen of a device as a bundle a tool can read, a
//! `screen.png` and a `screen.json` that follows the versioned format in
//! docs/screen-export.md, and `screen.export_batch` for several devices into
//! one directory with an `index.json`. `screen.diff` reads two bundles too.
//!
//! The element tree comes from the UI hierarchy where the platform reports
//! one (Android) and from OCR lines where it does not (the virtual iPhone, a
//! USB iPhone), and the document says which, so nothing read off pixels is
//! taken for a control.
use crate::core::*;
use crate::screen::{self, Screen, Store};
use crate::screen_ocr::{self as ocr, Recognizer};
use crate::screen_tree::{self as tree, Node};
use crate::visual;
use serde_json::{Map, Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
};

pub const FORMAT: &str = "tomacco.screen";
pub const INDEX_FORMAT: &str = "tomacco.screen-index";
pub const VERSION: u64 = 1;
pub const SCREEN_JSON: &str = "screen.json";
pub const SCREEN_PNG: &str = "screen.png";
pub const MARKED_PNG: &str = "marked.png";

// ---- What a platform reports --------------------------------------------------

/// What a device reports about itself and its screen. A field the platform
/// does not report stays null (or `None`); nothing is guessed.
#[derive(Default, Clone, Debug)]
pub struct Facts {
    /// platform, id, serial or udid, virtual, model, manufacturer, os_name,
    /// os_version, sdk, form_factor.
    pub device: Value,
    pub density_dpi: Option<f64>,
    /// Pixels per dp (Android) or per point (iOS).
    pub scale: Option<f64>,
    /// Quarter turns from the natural orientation.
    pub rotation: Option<u64>,
    pub insets: Value,
    pub theme: Value,
    pub locale: Value,
    pub app: Value,
    pub warnings: Vec<String>,
}

/// A rectangle as an object with its four edges.
fn edges(rect: [i64; 4]) -> Value {
    json!({"left":rect[0],"top":rect[1],"right":rect[2],"bottom":rect[3]})
}

fn rounded(value: f64) -> f64 {
    (value * 100.0).round() / 100.0
}

/// The RFC 3339 UTC time of a Unix time in seconds.
pub fn utc(seconds: f64) -> String {
    let whole = seconds.floor() as i64;
    let rest = whole.rem_euclid(86_400);
    format!(
        "{}T{:02}:{:02}:{:02}Z",
        crate::signing_health::iso(whole),
        rest / 3600,
        rest % 3600 / 60,
        rest % 60
    )
}

// ---- Android -----------------------------------------------------------------------

/// Windows, focus and system bars from `dumpsys window`.
#[derive(Default, Debug, PartialEq)]
pub struct Window {
    pub rotation: Option<u64>,
    pub package: Option<String>,
    pub activity: Option<String>,
    /// Type name, frame (left, top, right, bottom) and whether it is visible.
    pub bars: Vec<(String, [i64; 4], bool)>,
    /// The cutout's insets (left, top, right, bottom) in pixels.
    pub cutout: Option<[i64; 4]>,
    pub cutout_boxes: Vec<[i64; 4]>,
}

fn numbers<const N: usize>(text: &str, pattern: &regex::Regex) -> Option<[i64; N]> {
    let c = pattern.captures(text)?;
    let mut out = [0i64; N];
    for (i, slot) in out.iter_mut().enumerate() {
        *slot = c.get(i + 1)?.as_str().parse().ok()?;
    }
    Some(out)
}

pub fn parse_window(dump: &str) -> Window {
    static BAR: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    static FOCUS: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    static ROTATION: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    static INSETS: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    static BOX: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let bar = BAR.get_or_init(|| {
        regex::Regex::new(r"InsetsSource[^\n]*?\btype=(\w+)[^\n]*?\bframe=\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\](?:[^\n]*?\bvisible=(true|false))?").unwrap()
    });
    let focus = FOCUS.get_or_init(|| {
        regex::Regex::new(
            r"(?m)^\s*(?:mCurrentFocus|mFocusedApp)=\S*\{[^}]*?\s([A-Za-z][\w.]*)/([\w.$]+)",
        )
        .unwrap()
    });
    let rotation = ROTATION.get_or_init(|| regex::Regex::new(r"\bmRotation=([0-3])\b").unwrap());
    let insets = INSETS.get_or_init(|| {
        regex::Regex::new(r"DisplayCutout\{insets=Rect\((-?\d+), (-?\d+) - (-?\d+), (-?\d+)\)")
            .unwrap()
    });
    let cutout_box = BOX.get_or_init(|| {
        regex::Regex::new(r"Bounds=\[|Rect\((-?\d+), (-?\d+) - (-?\d+), (-?\d+)\)").unwrap()
    });
    let mut window = Window {
        rotation: rotation
            .captures(dump)
            .and_then(|c| c[1].parse::<u64>().ok()),
        ..Window::default()
    };
    for c in bar.captures_iter(dump) {
        let number = |i: usize| c[i].parse::<i64>().unwrap_or(0);
        let name = match &c[1] {
            "statusBars" | "ITYPE_STATUS_BAR" => "status_bar",
            "navigationBars" | "ITYPE_NAVIGATION_BAR" => "navigation_bar",
            "displayCutout" | "ITYPE_DISPLAY_CUTOUT" => "cutout",
            _ => continue,
        };
        let frame = [number(2), number(3), number(4), number(5)];
        let visible = c.get(6).is_none_or(|v| v.as_str() == "true");
        if !window.bars.iter().any(|(n, f, _)| n == name && *f == frame) {
            window.bars.push((name.to_string(), frame, visible));
        }
    }
    window.cutout = numbers::<4>(dump, insets).filter(|r| r.iter().any(|v| *v != 0));
    // The cutout's bounding boxes: the rectangles in the `Bounds=[...]` list
    // of the first DisplayCutout line, except the all-zero ones.
    if let Some(line) = dump
        .lines()
        .find(|l| l.contains("DisplayCutout{") && l.contains("Bounds="))
    {
        let after = line.split("Bounds=").nth(1).unwrap_or("");
        for c in cutout_box.captures_iter(after) {
            if let (Some(a), Some(b), Some(d), Some(e)) = (c.get(1), c.get(2), c.get(3), c.get(4)) {
                let r = [a, b, d, e].map(|m| m.as_str().parse::<i64>().unwrap_or(0));
                if r[2] > r[0] && r[3] > r[1] {
                    window.cutout_boxes.push(r);
                }
            }
        }
    }
    if let Some(c) = focus.captures(dump) {
        window.package = Some(c[1].to_string());
        let activity = &c[2];
        window.activity = Some(if let Some(rest) = activity.strip_prefix('.') {
            format!("{}.{rest}", &c[1])
        } else {
            activity.to_string()
        });
    }
    window
}

/// The distance a system bar's frame covers from the edge it sits on, as
/// (left, top, right, bottom) insets of a `width` by `height` screen.
fn inset_of(frame: [i64; 4], width: i64, height: i64) -> [i64; 4] {
    let [l, t, r, b] = frame;
    let mut inset = [0; 4];
    if r - l >= b - t {
        if t <= 0 {
            inset[1] = b.max(0);
        } else if b >= height {
            inset[3] = (height - t).max(0);
        }
    } else if l <= 0 {
        inset[0] = r.max(0);
    } else if r >= width {
        inset[2] = (width - l).max(0);
    }
    inset
}

fn android_insets(window: &Window, size: (u32, u32), scale: Option<f64>) -> Value {
    let (width, height) = (i64::from(size.0), i64::from(size.1));
    let find = |name: &str| {
        window
            .bars
            .iter()
            .find(|(n, _, visible)| n == name && *visible)
            .map(|(_, f, _)| *f)
    };
    let status = find("status_bar");
    let navigation = find("navigation_bar");
    let mut safe = [0i64; 4];
    let mut merge = |inset: [i64; 4]| {
        for (slot, value) in safe.iter_mut().zip(inset) {
            *slot = (*slot).max(value);
        }
    };
    for frame in [status, navigation].into_iter().flatten() {
        merge(inset_of(frame, width, height));
    }
    if let Some(cutout) = window.cutout {
        merge(cutout);
    }
    let known = status.is_some() || navigation.is_some() || window.cutout.is_some();
    let sides = |v: [i64; 4], unit: f64| {
        if unit == 1.0 {
            return json!({"left":v[0],"top":v[1],"right":v[2],"bottom":v[3]});
        }
        json!({"left":rounded(v[0] as f64 / unit),"top":rounded(v[1] as f64 / unit),
            "right":rounded(v[2] as f64 / unit),"bottom":rounded(v[3] as f64 / unit)})
    };
    json!({"source":if known {json!("dumpsys window")} else {Value::Null},"estimated":false,
        "status_bar":status.map(edges),"navigation_bar":navigation.map(edges),
        "cutout":window.cutout_boxes.iter().map(|b| edges(*b)).collect::<Vec<_>>(),
        "safe_area_px":known.then(|| sides(safe, 1.0)),
        "safe_area_dp":scale.filter(|_| known).map(|s| sides(safe, s))})
}

fn properties(output: &str) -> std::collections::HashMap<String, String> {
    let pattern = regex::Regex::new(r"\[([^]]+)\]: \[([^]]*)\]").unwrap();
    pattern
        .captures_iter(output)
        .map(|c| (c[1].to_string(), c[2].to_string()))
        .collect()
}

/// The form factor Android's characteristics and the smallest width in dp
/// name: `tv`, `watch`, `tablet` from 600 dp, else `phone`.
fn form_factor(characteristics: &str, smallest_dp: Option<f64>) -> Value {
    let names: Vec<&str> = characteristics.split(',').map(str::trim).collect();
    if names.contains(&"tv") {
        json!("tv")
    } else if names.contains(&"watch") {
        json!("watch")
    } else if names.contains(&"tablet") || smallest_dp.is_some_and(|d| d >= 600.0) {
        json!("tablet")
    } else if smallest_dp.is_some() || names.contains(&"default") {
        json!("phone")
    } else {
        Value::Null
    }
}

/// Dark or light from `cmd uimode night` (`Night mode: yes`).
pub fn night_mode(output: &str) -> Option<&'static str> {
    let value = output.split(':').nth(1)?.trim().to_ascii_lowercase();
    match value.as_str() {
        "yes" => Some("dark"),
        "no" => Some("light"),
        _ => None,
    }
}

/// Dark or light from the display configuration in `dumpsys window`
/// (`... port night finger ...`): the mode in effect, which `auto` leaves open.
pub fn configured_night(dump: &str) -> Option<&'static str> {
    let line = dump.lines().find(|l| l.contains("overrideConfig={1.0"))?;
    let config = line.split_once("overrideConfig={")?.1.split('{').next()?;
    if !config.contains(" port ") && !config.contains(" land ") {
        return None;
    }
    Some(if config.split_whitespace().any(|w| w == "night") {
        "dark"
    } else {
        "light"
    })
}

pub fn android_facts(device: &str, size: (u32, u32)) -> Facts {
    let mut facts = Facts::default();
    let shell = |args: &[&str]| crate::perf::shell_out(device, args, 30);
    let mut warn = |what: &str, error: &Error| {
        facts
            .warnings
            .push(format!("{what} was not read: {}", error.message));
    };
    let props = match shell(&["getprop"]) {
        Ok(output) => properties(&output),
        Err(error) => {
            warn("The device properties", &error);
            Default::default()
        }
    };
    let prop = |key: &str| {
        props
            .get(key)
            .filter(|v| !v.is_empty())
            .map(|v| json!(v))
            .unwrap_or(Value::Null)
    };
    let density = shell(&["wm", "density"])
        .map(|o| {
            let (physical, over) = crate::android_device::densities(&o);
            over.as_f64().or(physical.as_f64())
        })
        .unwrap_or_else(|e| {
            warn("The display density", &e);
            None
        });
    facts.density_dpi = density.filter(|d| *d > 0.0);
    facts.scale = facts.density_dpi.map(|d| d / 160.0);
    let smallest_dp = facts.scale.map(|s| f64::from(size.0.min(size.1)) / s);
    let serial_is_emulator = device.starts_with("emulator-");
    facts.device = json!({"platform":"android","id":device,"serial":device,"udid":null,
        "virtual":serial_is_emulator || props.get("ro.kernel.qemu").is_some_and(|v| v == "1"),
        "model":prop("ro.product.model"),"manufacturer":prop("ro.product.manufacturer"),
        "os_name":"Android","os_version":prop("ro.build.version.release"),
        "sdk":props.get("ro.build.version.sdk").and_then(|v| v.parse::<u64>().ok()),
        "form_factor":form_factor(props.get("ro.build.characteristics").map_or("", |v| v), smallest_dp)});
    let locale = ["persist.sys.locale", "ro.product.locale"]
        .iter()
        .find_map(|k| props.get(*k).filter(|v| !v.is_empty()).map(|v| (k, v)));
    facts.locale = match locale {
        Some((key, tag)) => json!({"tag":tag.replace('_', "-"),"source":format!("getprop {key}")}),
        None => json!({"tag":null,"source":null}),
    };
    match shell(&["cmd", "uimode", "night"]) {
        Ok(output) => {
            facts.theme = match night_mode(&output) {
                Some(mode) => json!({"mode":mode,"source":"cmd uimode night"}),
                None => json!({"mode":null,"source":"cmd uimode night"}),
            };
        }
        Err(error) => {
            warn("The night mode", &error);
            facts.theme = json!({"mode":null,"source":null});
        }
    }
    match shell(&["dumpsys", "window"]) {
        Ok(output) => {
            let window = parse_window(&output);
            facts.rotation = window.rotation;
            facts.insets = android_insets(&window, size, facts.scale);
            facts.app = json!({"package":window.package,"activity":window.activity,
                "source":window.package.as_ref().map(|_| "dumpsys window")});
            if facts.theme["mode"].is_null() {
                if let Some(mode) = configured_night(&output) {
                    facts.theme = json!({"mode":mode,"source":"dumpsys window configuration"});
                }
            }
        }
        Err(error) => warn("The window state", &error),
    }
    facts
}

// ---- iPhone --------------------------------------------------------------------------

/// The catalogue's iPhone whose screen has these pixels, or the one with this id.
fn catalogue_phone(id: Option<&str>, size: (u32, u32)) -> Option<Value> {
    let catalogue = crate::catalog::builtin();
    let phones = catalogue
        .as_array()?
        .iter()
        .filter(|p| p["platform"] == "ios");
    let wanted = format!("{} × {}", size.0, size.1);
    phones
        .into_iter()
        .find(|p| match id {
            Some(id) => p["id"] == id,
            None => p["pixels"].as_str() == Some(wanted.as_str()),
        })
        .cloned()
}

fn phone_insets(phone: &Value, scale: f64, how: &str) -> Value {
    let side = |key: &str| phone["safePortrait"][key].as_f64().unwrap_or(0.0);
    let points = json!({"left":side("left"),"top":side("top"),"right":side("right"),"bottom":side("bottom")});
    let pixels = json!({"left":rounded(side("left") * scale),"top":rounded(side("top") * scale),
        "right":rounded(side("right") * scale),"bottom":rounded(side("bottom") * scale)});
    json!({"source":how,"estimated":true,"status_bar":null,"navigation_bar":null,"cutout":[],
        "safe_area_px":pixels,"safe_area_dp":points})
}

pub fn virtual_iphone_facts(size: (u32, u32)) -> Facts {
    let mut facts = Facts::default();
    let status = crate::api::dispatch("ios.virtual.status", &json!({}));
    let model = match &status {
        Ok(status) => status["running_model"]
            .as_str()
            .filter(|m| !m.is_empty())
            .map(String::from),
        Err(error) => {
            facts.warnings.push(format!(
                "The virtual iPhone's status was not read: {}",
                error.message
            ));
            None
        }
    };
    let phone = catalogue_phone(model.as_deref(), size);
    facts.device = json!({"platform":"ios","id":"virtual-iphone","serial":null,"udid":null,"virtual":true,
        "model":phone.as_ref().map_or(model.as_deref().map(|m| json!(m)).unwrap_or(Value::Null), |p| p["name"].clone()),
        "manufacturer":"Apple","os_name":"iOS","os_version":null,"sdk":null,"form_factor":"phone"});
    if let Some(phone) = phone {
        let scale = phone["dpr"].as_f64().unwrap_or(0.0);
        if scale > 0.0 {
            facts.scale = Some(scale);
            facts.density_dpi = Some(scale * 163.0);
            facts.insets = phone_insets(&phone, scale, "device catalogue (estimated)");
        }
        facts.rotation = Some(0);
    } else {
        facts.warnings.push(
            "The running model is not in the device catalogue, so there is no scale in points"
                .into(),
        );
    }
    facts.theme = json!({"mode":null,"source":null});
    facts.locale = json!({"tag":null,"source":null});
    facts.app = json!({"package":null,"activity":null,"source":null});
    facts
}

/// `Key: value` lines of `ideviceinfo`.
pub fn parse_ideviceinfo(output: &str) -> Map<String, Value> {
    output
        .lines()
        .filter_map(|l| l.split_once(": "))
        .map(|(k, v)| (k.trim().to_string(), json!(v.trim())))
        .collect()
}

/// A USB iPhone as a screen: a screenshot and what `ideviceinfo` says.
pub struct IPhone(pub String);

impl Screen for IPhone {
    fn capture(&mut self, path: &Path) -> Result<(u32, u32)> {
        let command = crate::iphone_device::screenshot_command(&self.0, path)?;
        crate::toolchain::run(&command, 60, None, true)?;
        screen::png_size(path)
    }
    fn hierarchy(&mut self) -> Result<Option<String>> {
        Ok(None)
    }
    fn facts(&mut self, size: (u32, u32)) -> Facts {
        let mut facts = Facts::default();
        let info = crate::toolchain::find("ideviceinfo").and_then(|tool| {
            crate::toolchain::run(&[tool, "-u".into(), self.0.clone()], 30, None, true)
        });
        let info = match info {
            Ok(result) => parse_ideviceinfo(s(&result, "stdout")),
            Err(error) => {
                facts
                    .warnings
                    .push(format!("ideviceinfo was not read: {}", error.message));
                Map::new()
            }
        };
        let field = |k: &str| info.get(k).cloned().unwrap_or(Value::Null);
        let phone = catalogue_phone(None, size);
        facts.device = json!({"platform":"ios","id":self.0,"serial":null,"udid":self.0,"virtual":false,
            "model":phone.as_ref().map_or(field("ProductType"), |p| p["name"].clone()),
            "manufacturer":"Apple","os_name":"iOS","os_version":field("ProductVersion"),"sdk":null,
            "form_factor":"phone"});
        if let Some(phone) = phone {
            let scale = phone["dpr"].as_f64().unwrap_or(0.0);
            if scale > 0.0 {
                facts.scale = Some(scale);
                facts.insets = phone_insets(&phone, scale, "device catalogue (estimated)");
            }
        } else {
            facts.warnings.push(
                "No catalogue phone has this screen size, so there is no scale in points".into(),
            );
        }
        facts.theme = json!({"mode":null,"source":null});
        facts.locale = json!({"tag":null,"source":null});
        facts.app = json!({"package":null,"activity":null,"source":null});
        facts
    }
    fn tap(&mut self, _x: i64, _y: i64, _hold_ms: Option<u64>) -> Result {
        Err(Error::new(
            "unsupported_device",
            "A USB iPhone can be read but not tapped through screen operations",
        ))
    }
}

// ---- The document -----------------------------------------------------------------------------

/// The role a person would give a UIAutomator element.
pub fn role(n: &Node) -> &'static str {
    let class = n.short_class();
    if n.editable() {
        "text_field"
    } else if class.ends_with("CheckBox") {
        "checkbox"
    } else if class.ends_with("RadioButton") {
        "radio"
    } else if class.ends_with("Switch")
        || class.ends_with("SwitchCompat")
        || class.ends_with("ToggleButton")
    {
        "switch"
    } else if class.ends_with("Button") {
        "button"
    } else if class.ends_with("ImageView") {
        "image"
    } else if class.ends_with("ProgressBar") || class.ends_with("SeekBar") {
        "progress"
    } else if class.ends_with("Spinner") {
        "dropdown"
    } else if class.ends_with("WebView") {
        "web_view"
    } else if class.ends_with("RecyclerView")
        || class.ends_with("ListView")
        || class.ends_with("GridView")
    {
        "list"
    } else if class.ends_with("ScrollView") || class.ends_with("ViewPager") {
        "scroll"
    } else if class.ends_with("TextView") {
        "text"
    } else if n.interactive() {
        "control"
    } else if class.ends_with("Layout") || class.ends_with("ViewGroup") || class == "View" {
        "container"
    } else {
        "view"
    }
}

fn box_value(rect: tree::Rect, unit: f64) -> Value {
    let v = |x: i64| rounded(x as f64 / unit);
    json!({"left":v(rect.0),"top":v(rect.1),"right":v(rect.2),"bottom":v(rect.3),
        "width":v(rect.2 - rect.0),"height":v(rect.3 - rect.1)})
}

fn bounds_value(rect: tree::Rect, scale: Option<f64>) -> Value {
    json!({"px":{"left":rect.0,"top":rect.1,"right":rect.2,"bottom":rect.3,
        "width":rect.2 - rect.0,"height":rect.3 - rect.1},
        "dp":scale.map(|s| box_value(rect, s))})
}

fn centre(rect: tree::Rect, size: (u32, u32)) -> Value {
    let (l, t) = (rect.0.max(0), rect.1.max(0));
    let (r, b) = (
        rect.2.min(i64::from(size.0).max(1)),
        rect.3.min(i64::from(size.1).max(1)),
    );
    json!({"x":(l + r) / 2,"y":(t + b) / 2})
}

fn element_id(index: usize) -> String {
    format!("e{index}")
}

/// Every node of the hierarchy as an element, parents before children, with
/// the mark numbers `screen.marks` gives the same hierarchy.
pub fn elements_from_nodes(
    nodes: &[Node],
    size: (u32, u32),
    scale: Option<f64>,
    max_marks: usize,
) -> (Vec<Value>, usize) {
    let numbers = tree::numbering(nodes, max_marks);
    let marked = tree::interactive(nodes).len();
    let elements = nodes
        .iter()
        .enumerate()
        .map(|(i, n)| {
            json!({"id":element_id(i),"parent":n.parent.map(element_id),"depth":n.depth,
                "source":"hierarchy","role":role(n),"class":n.class,"package":n.package,
                "resource_id":n.id,"text":n.text,"description":n.description,"label":tree::label(nodes, i),
                "bounds":bounds_value(n.rect, scale),"center":centre(n.rect, size),
                "enabled":n.enabled,"clickable":n.clickable,"long_clickable":n.long_clickable,
                "focusable":n.focusable,"focused":n.focused,"checkable":n.checkable,"checked":n.checked,
                "selected":n.selected,"scrollable":n.scrollable,"editable":n.editable(),"password":n.password,
                "confidence":null,"mark":numbers[i]})
        })
        .collect();
    (elements, marked)
}

/// OCR lines as elements: text and a box, and `null` for every state a
/// picture cannot tell.
pub fn elements_from_ocr(marks: &[Value], size: (u32, u32), scale: Option<f64>) -> Vec<Value> {
    marks
        .iter()
        .enumerate()
        .map(|(i, m)| {
            let b = &m["bounds"];
            let at = |k: &str| b[k].as_i64().unwrap_or(0);
            let rect = (at("left"), at("top"), at("right"), at("bottom"));
            json!({"id":format!("o{i}"),"parent":null,"depth":0,"source":"ocr","role":"text",
                "class":"","package":"","resource_id":"","text":m["text"],"description":"","label":m["label"],
                "bounds":bounds_value(rect, scale),"center":centre(rect, size),
                "enabled":null,"clickable":null,"long_clickable":null,"focusable":null,"focused":null,
                "checkable":null,"checked":null,"selected":null,"scrollable":null,"editable":null,"password":null,
                "confidence":m["confidence"],"mark":m["mark"]})
        })
        .collect()
}

/// What a capture is made of.
pub struct Capture {
    pub at: f64,
    pub device: String,
    pub size: (u32, u32),
    pub facts: Facts,
    /// The hierarchy's rotation attribute, where the platform has a hierarchy.
    pub xml_rotation: Option<u64>,
    pub elements: Vec<Value>,
    /// `hierarchy` or `ocr`; `none` when nothing could be read.
    pub elements_source: &'static str,
    /// `available`, `unavailable` (the platform reports none) or `skipped`.
    pub hierarchy: &'static str,
    pub max_marks: usize,
    pub marks_total: usize,
    pub screenshot_sha256: String,
    pub screenshot_bytes: usize,
    pub marked: bool,
    pub warnings: Vec<String>,
}

pub fn build_document(c: &Capture) -> Value {
    let facts = &c.facts;
    let (width, height) = c.size;
    let rotation = facts.rotation.or(c.xml_rotation);
    let dp = |px: u32| facts.scale.map(|s| rounded(f64::from(px) / s));
    let device = if facts.device.is_object() {
        facts.device.clone()
    } else {
        json!({"platform":null,"id":c.device,"serial":null,"udid":null,"virtual":null,"model":null,
            "manufacturer":null,"os_name":null,"os_version":null,"sdk":null,"form_factor":null})
    };
    let insets = if facts.insets.is_object() {
        facts.insets.clone()
    } else {
        json!({"source":null,"estimated":false,"status_bar":null,"navigation_bar":null,"cutout":[],
            "safe_area_px":null,"safe_area_dp":null})
    };
    let or_unknown = |v: &Value, keys: &[&str]| {
        if v.is_object() {
            v.clone()
        } else {
            Value::Object(
                keys.iter()
                    .map(|k| (k.to_string(), Value::Null))
                    .collect::<Map<_, _>>(),
            )
        }
    };
    json!({
        "format":FORMAT,"version":VERSION,"tomacco_version":env!("CARGO_PKG_VERSION"),
        "captured_at":utc(c.at),
        "device":device,
        "display":{"width_px":width,"height_px":height,"density_dpi":facts.density_dpi.map(|d| if d.fract() == 0.0 { json!(d as u64) } else { json!(rounded(d)) }),
            "scale":facts.scale,"width_dp":dp(width),"height_dp":dp(height),
            "unit":match facts.device["platform"].as_str() {Some("ios") => "pt", _ => "dp"},
            "orientation":if width > height {"landscape"} else {"portrait"},
            "rotation":rotation.map(|r| r * 90)},
        "insets":insets,
        "theme":or_unknown(&facts.theme, &["mode", "source"]),
        "locale":or_unknown(&facts.locale, &["tag", "source"]),
        "app":or_unknown(&facts.app, &["package", "activity", "source"]),
        "screenshot":{"file":SCREEN_PNG,"width":width,"height":height,"bytes":c.screenshot_bytes,
            "sha256":c.screenshot_sha256,"marked_file":c.marked.then_some(MARKED_PNG)},
        "hierarchy":c.hierarchy,
        "elements_source":c.elements_source,
        "marks":{"count":c.marks_total.min(c.max_marks),"limit":c.max_marks,"truncated":c.marks_total > c.max_marks},
        "elements":c.elements,
        "warnings":c.warnings,
    })
}

// ---- Writing a bundle --------------------------------------------------------------------------

/// A new directory, or an empty one.
fn fresh_directory(value: &str) -> Result<PathBuf> {
    let directory = path(value, false, false)?;
    if directory.exists() && (!directory.is_dir() || fs::read_dir(&directory)?.next().is_some()) {
        return Err(Error::new(
            "path_exists",
            "Choose an empty or new directory for the export",
        ));
    }
    Ok(directory)
}

fn source_argument(a: &Value) -> Result<&str> {
    let source = a["source"].as_str().unwrap_or("auto");
    if ["auto", "hierarchy", "ocr"].contains(&source) {
        Ok(source)
    } else {
        Err(Error::new(
            "invalid_argument",
            "source is auto, hierarchy or ocr",
        ))
    }
}

/// Takes one screen and writes `screen.png`, `screen.json` and, on request,
/// `marked.png` into `target`. Answers the summary `screen.export` returns.
pub fn write_bundle(
    a: &Value,
    device: &str,
    target: &Path,
    screen: &mut dyn Screen,
    recognizer: &mut dyn Recognizer,
    store: &Store,
) -> Result {
    let max_marks = screen::limit_argument(a, "max_marks", 200, 1, 500)?;
    let source = source_argument(a)?;
    let xml = screen::hierarchy_for(screen, source, "screen.export", device)?;
    let work = tempfile::tempdir()?;
    let raw = work.path().join(SCREEN_PNG);
    let size = screen.capture(&raw)?;
    let bytes = fs::read(&raw)?;
    let mut facts = screen.facts(size);
    let mut warnings = std::mem::take(&mut facts.warnings);
    let hierarchy_state = match (&xml, source) {
        (Some(_), _) => "available",
        (None, "ocr") => "skipped",
        (None, _) => "unavailable",
    };
    let (elements, source_used, marks, marks_total, xml_rotation, found) = match &xml {
        Some(xml) => {
            let nodes = tree::parse(xml)?;
            let (elements, total) = elements_from_nodes(&nodes, size, facts.scale, max_marks);
            let (found, _) = tree::marks(&nodes, max_marks);
            let rotation = regex::Regex::new(r#"<hierarchy[^>]*\brotation="([0-3])""#)
                .unwrap()
                .captures(xml)
                .and_then(|c| c[1].parse::<u64>().ok());
            (elements, "hierarchy", found.clone(), total, rotation, found)
        }
        None => {
            let reading = ocr::read(
                recognizer,
                &raw,
                screen::language(a)?,
                a["invert"].as_str().unwrap_or("auto"),
                a["min_confidence"].as_f64().unwrap_or(40.0),
                None,
            );
            match reading {
                Ok(reading) => {
                    let (found, _) = screen::ocr_marks(&reading.lines, max_marks);
                    let total = reading
                        .lines
                        .iter()
                        .filter(|l| l.text().chars().any(char::is_alphanumeric))
                        .count();
                    let elements = elements_from_ocr(&found, size, facts.scale);
                    (elements, "ocr", found.clone(), total, None, found)
                }
                Err(error) if error.code == "ocr_unavailable" => {
                    warnings.push(
                        "This platform reports no UI hierarchy and tesseract is not installed, so the screen has no elements".into(),
                    );
                    (Vec::new(), "none", Vec::new(), 0, None, Vec::new())
                }
                Err(error) => return Err(error),
            }
        }
    };
    let want_marked = b(a, "marked");
    let marked_bytes = if want_marked {
        let mut picture = visual::decode(&bytes)?;
        screen::draw_marks(&mut picture, &marks, 1.0);
        Some(visual::encode(&picture)?)
    } else {
        None
    };
    let capture = Capture {
        at: now(),
        device: device.to_string(),
        size,
        facts,
        xml_rotation,
        elements,
        elements_source: source_used,
        hierarchy: hierarchy_state,
        max_marks,
        marks_total,
        screenshot_sha256: digest(&bytes),
        screenshot_bytes: bytes.len(),
        marked: want_marked,
        warnings,
    };
    let document = build_document(&capture);
    fs::create_dir_all(target)?;
    atomic_write(&target.join(SCREEN_PNG), &bytes)?;
    if let Some(marked) = &marked_bytes {
        atomic_write(&target.join(MARKED_PNG), marked)?;
    }
    atomic_write(
        &target.join(SCREEN_JSON),
        serde_json::to_string_pretty(&document)?.as_bytes(),
    )?;
    // The marks are the ones screen.tap takes, as after screen.marks.
    let source_name = if source_used == "ocr" {
        "ocr"
    } else {
        "hierarchy"
    };
    if source_used != "none" {
        store.save(
            device,
            &json!({"device":device,"taken":now(),"source":source_name,"width":size.0,"height":size.1,"marks":found}),
            Some(&bytes),
        )?;
    }
    Ok(json!({"format":FORMAT,"version":VERSION,"directory":target,
        "screen_json":target.join(SCREEN_JSON),"screenshot":target.join(SCREEN_PNG),
        "marked_screenshot":marked_bytes.as_ref().map(|_| target.join(MARKED_PNG)),
        "screenshot_sha256":capture.screenshot_sha256,"width":size.0,"height":size.1,
        "device":document["device"],"display":document["display"],"theme":document["theme"],
        "locale":document["locale"],"app":document["app"],
        "hierarchy":hierarchy_state,"elements_source":source_used,
        "elements":document["elements"].as_array().map_or(0, Vec::len),
        "marks":document["marks"],"warnings":document["warnings"]}))
}

fn screen_for<T>(
    platform: &str,
    device: &str,
    action: impl FnOnce(&mut dyn Screen) -> Result<T>,
) -> Result<T> {
    if platform == "ios" && !crate::ios_emulator::ui_testing::is_virtual(device) {
        identifier(device, "device", None)?;
        return action(&mut IPhone(device.to_string()));
    }
    screen::with_screen(device, action)
}

fn platform_argument(a: &Value) -> Result<&str> {
    let platform = a["platform"].as_str().unwrap_or("android");
    if ["android", "ios"].contains(&platform) {
        Ok(platform)
    } else {
        Err(Error::new("invalid_argument", "platform is android or ios"))
    }
}

pub fn export(a: &Value) -> Result {
    let device = s(a, "device");
    let platform = platform_argument(a)?;
    let target = fresh_directory(s(a, "directory"))?;
    let store = Store::home()?;
    screen_for(platform, device, |screen| {
        write_bundle(a, device, &target, screen, &mut ocr::Tesseract, &store)
    })
}

fn slug(device: &str) -> String {
    device
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
        .collect()
}

/// Several devices into one directory: a bundle each in `NN-device/` and an
/// `index.json`. A device that fails is listed with its error and the others
/// go on.
pub fn batch(a: &Value, take: &mut dyn FnMut(&Value, &str, &Path) -> Result) -> Result {
    let devices = strings(&a["devices"]);
    if devices.is_empty() || devices.len() > 16 {
        return Err(Error::new(
            "invalid_argument",
            "devices lists 1 to 16 devices",
        ));
    }
    let unique: std::collections::HashSet<_> = devices.iter().collect();
    if unique.len() != devices.len() {
        return Err(Error::new("invalid_argument", "devices must be unique"));
    }
    let root = fresh_directory(s(a, "directory"))?;
    fs::create_dir_all(&root)?;
    let mut screens = Vec::new();
    for (i, device) in devices.iter().enumerate() {
        check_cancelled()?;
        let name = format!("{:02}-{}", i + 1, slug(device));
        match take(a, device, &root.join(&name)) {
            Ok(summary) => screens.push(json!({"device":device,"ok":true,"directory":name,
                "screen_json":format!("{name}/{SCREEN_JSON}"),"screenshot":format!("{name}/{SCREEN_PNG}"),
                "platform":summary["device"]["platform"],"model":summary["device"]["model"],
                "width_px":summary["width"],"height_px":summary["height"],
                "orientation":summary["display"]["orientation"],"theme":summary["theme"]["mode"],
                "locale":summary["locale"]["tag"],"app":summary["app"]["package"],
                "elements":summary["elements"],"elements_source":summary["elements_source"],
                "hierarchy":summary["hierarchy"],"warnings":summary["warnings"]})),
            Err(error) => screens.push(json!({"device":device,"ok":false,"error":error.value()})),
        }
    }
    let failed = screens.iter().filter(|s| s["ok"] != true).count();
    let index = json!({"format":INDEX_FORMAT,"version":VERSION,"tomacco_version":env!("CARGO_PKG_VERSION"),
        "captured_at":utc(now()),"count":screens.len(),"failed":failed,"screens":screens});
    atomic_write(
        &root.join("index.json"),
        serde_json::to_string_pretty(&index)?.as_bytes(),
    )?;
    Ok(
        json!({"format":INDEX_FORMAT,"version":VERSION,"directory":root,"index":root.join("index.json"),
        "count":index["count"],"failed":failed,"ok":failed == 0,"screens":index["screens"]}),
    )
}

pub fn export_batch(a: &Value) -> Result {
    let store = Store::home()?;
    batch(a, &mut |a, device, target| {
        screen::with_screen(device, |screen| {
            write_bundle(a, device, target, screen, &mut ocr::Tesseract, &store)
        })
    })
}

// ---- Reading bundles for screen.diff -------------------------------------------------

/// Whether a path names an export bundle: its directory or its screen.json.
pub fn is_bundle(value: &str) -> bool {
    let p = expand(value);
    p.is_dir() || p.file_name().is_some_and(|n| n == SCREEN_JSON)
}

pub struct Bundle {
    pub document: Value,
    pub picture: PathBuf,
}

pub fn load(value: &str) -> Result<Bundle> {
    let given = path(value, false, true)?;
    let directory = if given.is_dir() {
        given
    } else {
        given.parent().map(Path::to_path_buf).unwrap_or(given)
    };
    let document: Value = serde_json::from_str(&fs::read_to_string(directory.join(SCREEN_JSON))?)
        .map_err(|e| {
        Error::new(
            "invalid_export",
            format!("{SCREEN_JSON} is unreadable: {e}"),
        )
    })?;
    if document["format"] != FORMAT {
        return Err(Error::new(
            "invalid_export",
            format!("{value} is not a {FORMAT} export"),
        )
        .remedy("Give a directory made by screen.export, or its screen.json"));
    }
    if document["version"].as_u64().is_none_or(|v| v > VERSION) {
        return Err(Error::new(
            "unsupported_export_version",
            format!(
                "This export is version {}; this Tomacco reads versions up to {VERSION}",
                document["version"]
            ),
        )
        .remedy("Update Tomacco, or export the screen again"));
    }
    Ok(Bundle {
        picture: directory.join(s(&document["screenshot"], "file")),
        document,
    })
}

/// The hierarchy nodes of an export, rebuilt from its elements.
pub fn nodes_from(document: &Value) -> Vec<Node> {
    let list = document["elements"].as_array().cloned().unwrap_or_default();
    let index: std::collections::HashMap<&str, usize> = list
        .iter()
        .enumerate()
        .filter_map(|(i, e)| e["id"].as_str().map(|id| (id, i)))
        .collect();
    list.iter()
        .map(|e| {
            let px = &e["bounds"]["px"];
            let at = |k: &str| px[k].as_i64().unwrap_or(0);
            let flag = |k: &str| e[k].as_bool().unwrap_or(false);
            Node {
                depth: e["depth"].as_u64().unwrap_or(0) as usize,
                parent: e["parent"].as_str().and_then(|p| index.get(p).copied()),
                class: s(e, "class").to_string(),
                text: s(e, "text").to_string(),
                description: s(e, "description").to_string(),
                id: s(e, "resource_id").to_string(),
                package: s(e, "package").to_string(),
                rect: (at("left"), at("top"), at("right"), at("bottom")),
                clickable: flag("clickable"),
                long_clickable: flag("long_clickable"),
                checkable: flag("checkable"),
                checked: flag("checked"),
                enabled: e["enabled"].as_bool().unwrap_or(true),
                focused: flag("focused"),
                focusable: flag("focusable"),
                scrollable: flag("scrollable"),
                selected: flag("selected"),
                password: flag("password"),
            }
        })
        .collect()
}

fn pick<'a>(document: &'a Value, pointer: &str) -> &'a Value {
    document.pointer(pointer).unwrap_or(&Value::Null)
}

/// The fields of two exports that differ, other than the capture time.
pub fn metadata_changes(before: &Value, after: &Value) -> Vec<Value> {
    [
        "/device/id",
        "/device/model",
        "/device/os_version",
        "/display/width_px",
        "/display/height_px",
        "/display/density_dpi",
        "/display/orientation",
        "/display/rotation",
        "/theme/mode",
        "/locale/tag",
        "/app/package",
        "/app/activity",
    ]
    .iter()
    .filter(|p| pick(before, p) != pick(after, p))
    .map(|p| json!({"field":p.trim_start_matches('/').replace('/', "."),"from":pick(before, p),"to":pick(after, p)}))
    .collect()
}

pub fn diff_bundles(a: &Value) -> Result {
    let before = load(s(a, "before"))?;
    let after = match a["after"].as_str() {
        Some(after) if is_bundle(after) => load(after)?,
        Some(_) => {
            return Err(Error::new(
                "invalid_argument",
                "before is an export, so after must be one too",
            ));
        }
        None => {
            return Err(Error::new(
                "invalid_argument",
                "Give after: an export bundle is compared with another one, not with a device",
            ));
        }
    };
    let limit = screen::limit_argument(a, "max_regions", 12, 1, 100)?;
    let metadata = metadata_changes(&before.document, &after.document);
    let (first, second) = (
        s(&before.document, "elements_source"),
        s(&after.document, "elements_source"),
    );
    let elements = if first == second && first != "none" {
        let mut result = tree::compare(
            &nodes_from(&before.document),
            &nodes_from(&after.document),
            limit,
        );
        result["source"] = json!(first);
        result
    } else {
        json!({"kind":"unavailable","identical":null,
            "summary":format!("The elements were read differently ({first} and {second}), so they are not compared.")})
    };
    let one = visual::decode(&fs::read(&before.picture)?)?;
    let two = visual::decode(&fs::read(&after.picture)?)?;
    let tolerance = a
        .get("tolerance")
        .and_then(Value::as_u64)
        .map_or(24, |t| t.min(255) as u8);
    let pixels = if (one.width, one.height) != (two.width, two.height) {
        json!({"kind":"pixels","identical":false,"size_changed":true,
            "before_size":{"width":one.width,"height":one.height},"after_size":{"width":two.width,"height":two.height},
            "regions":[],"summary":format!("The screens differ in size ({}x{} and {}x{}).", one.width, one.height, two.width, two.height)})
    } else {
        screen::pixel_regions(&one, &two, tolerance, &a["ignore"], limit)
    };
    let identical =
        metadata.is_empty() && elements["identical"] == true && pixels["identical"] == true;
    let summary = format!(
        "{} field{} of the metadata changed; elements: {}; pixels: {}",
        metadata.len(),
        if metadata.len() == 1 { "" } else { "s" },
        elements["summary"].as_str().unwrap_or(""),
        pixels["summary"].as_str().unwrap_or("")
    );
    Ok(
        json!({"kind":"bundle","identical":identical,"summary":summary,
        "before_captured_at":before.document["captured_at"],"after_captured_at":after.document["captured_at"],
        "metadata":metadata,"elements":elements,"pixels":pixels}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::visual::Image;

    const LOGIN: &str = include_str!("../tests/fixtures/screen_login.xml");
    const FILLED: &str = include_str!("../tests/fixtures/screen_login_filled.xml");
    const WINDOW: &str = include_str!("../tests/fixtures/screen_export_window.txt");

    fn solid(width: u32, height: u32, shade: u8) -> Image {
        Image {
            width,
            height,
            pixels: [shade, shade, shade, 255].repeat((width * height) as usize),
        }
    }

    struct Fake {
        picture: Image,
        xml: Option<String>,
        facts: Facts,
    }
    impl Screen for Fake {
        fn capture(&mut self, path: &Path) -> Result<(u32, u32)> {
            fs::write(path, visual::encode(&self.picture)?)?;
            Ok((self.picture.width, self.picture.height))
        }
        fn hierarchy(&mut self) -> Result<Option<String>> {
            Ok(self.xml.clone())
        }
        fn facts(&mut self, _: (u32, u32)) -> Facts {
            self.facts.clone()
        }
        fn tap(&mut self, _: i64, _: i64, _: Option<u64>) -> Result {
            Ok(json!({}))
        }
    }

    struct Lines(&'static str);
    impl Recognizer for Lines {
        fn tsv(&mut self, _: &Path, _: &str) -> Result<String> {
            Ok(self.0.to_string())
        }
    }
    struct NoOcr;
    impl Recognizer for NoOcr {
        fn tsv(&mut self, _: &Path, _: &str) -> Result<String> {
            Err(Error::new("ocr_unavailable", "not in this test"))
        }
    }

    fn android_facts_fixture() -> Facts {
        let window = parse_window(WINDOW);
        Facts {
            device: json!({"platform":"android","id":"emulator-5554","serial":"emulator-5554","udid":null,
                "virtual":true,"model":"sdk_gphone64_x86_64","manufacturer":"Google","os_name":"Android",
                "os_version":"16","sdk":36,"form_factor":"phone"}),
            density_dpi: Some(420.0),
            scale: Some(2.625),
            rotation: window.rotation,
            insets: android_insets(&window, (1080, 2424), Some(2.625)),
            theme: json!({"mode":"light","source":"cmd uimode night"}),
            locale: json!({"tag":"en-US","source":"getprop persist.sys.locale"}),
            app: json!({"package":window.package,"activity":window.activity,"source":"dumpsys window"}),
            warnings: vec![],
        }
    }

    fn store() -> (tempfile::TempDir, Store) {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::at(dir.path().join("marks"));
        (dir, store)
    }

    fn export_login(dir: &Path) -> Value {
        let (_keep, store) = store();
        let mut screen = Fake {
            picture: solid(1080, 2400, 240),
            xml: Some(LOGIN.into()),
            facts: android_facts_fixture(),
        };
        write_bundle(
            &json!({"marked":true}),
            "emulator-5554",
            dir,
            &mut screen,
            &mut NoOcr,
            &store,
        )
        .unwrap()
    }

    fn fixed(mut document: Value) -> Value {
        document["captured_at"] = json!("2026-11-01T12:00:00Z");
        document["tomacco_version"] = json!("0.0.0");
        document
    }

    #[test]
    fn the_time_is_rfc_3339_utc() {
        assert_eq!(utc(0.0), "1970-01-01T00:00:00Z");
        assert_eq!(utc(1_793_534_400.4), "2026-11-01T12:00:00Z");
    }

    #[test]
    fn the_window_dump_gives_bars_focus_cutout_and_rotation() {
        let w = parse_window(WINDOW);
        assert_eq!(w.rotation, Some(0));
        assert_eq!(
            w.package.as_deref(),
            Some("com.google.android.apps.nexuslauncher")
        );
        assert_eq!(
            w.activity.as_deref(),
            Some("com.google.android.apps.nexuslauncher.NexusLauncherActivity")
        );
        assert!(
            w.bars
                .contains(&("status_bar".into(), [0, 0, 1080, 63], true))
        );
        assert!(
            w.bars
                .contains(&("navigation_bar".into(), [0, 2361, 1080, 2424], true))
        );
        assert_eq!(w.cutout, None);
        assert!(w.cutout_boxes.is_empty());
        let insets = android_insets(&w, (1080, 2424), Some(2.625));
        assert_eq!(insets["safe_area_px"]["top"], 63);
        assert_eq!(insets["safe_area_px"]["bottom"], 63);
        assert_eq!(insets["safe_area_dp"]["top"], 24.0);
        assert_eq!(insets["status_bar"]["bottom"], 63);
        assert_eq!(configured_night(WINDOW), Some("light"));
        assert_eq!(
            configured_night(&WINDOW.replace(" port finger", " port night finger")),
            Some("dark")
        );
    }

    #[test]
    fn a_cutout_widens_the_safe_area() {
        let dump = "mDisplayCutout=DisplayCutout{insets=Rect(0, 136 - 0, 0) waterfall=Insets{left=0, top=0, right=0, bottom=0} boundingRect={Bounds=[Rect(0, 0 - 0, 0), Rect(440, 0 - 640, 136), Rect(0, 0 - 0, 0), Rect(0, 0 - 0, 0)]} cutoutPathParserInfo={}}\n    mRotation=1 \n InsetsSource id=1 type=statusBars frame=[0,0][2400,40] visible=true flags=";
        let w = parse_window(dump);
        assert_eq!(w.cutout, Some([0, 136, 0, 0]));
        assert_eq!(w.cutout_boxes, vec![[440, 0, 640, 136]]);
        assert_eq!(w.rotation, Some(1));
        let insets = android_insets(&w, (2400, 1080), Some(2.0));
        assert_eq!(insets["safe_area_px"]["top"], 136);
        assert_eq!(insets["cutout"][0]["right"], 640);
    }

    #[test]
    fn a_bar_beside_the_screen_is_a_side_inset() {
        assert_eq!(inset_of([2316, 0, 2400, 1080], 2400, 1080), [0, 0, 84, 0]);
        assert_eq!(inset_of([0, 0, 84, 1080], 2400, 1080), [84, 0, 0, 0]);
    }

    #[test]
    fn night_mode_and_form_factor_are_read() {
        assert_eq!(night_mode("Night mode: yes\n"), Some("dark"));
        assert_eq!(night_mode("Night mode: no\n"), Some("light"));
        assert_eq!(night_mode("Night mode: auto\n"), None);
        assert_eq!(form_factor("default", Some(411.0)), "phone");
        assert_eq!(form_factor("default", Some(800.0)), "tablet");
        assert_eq!(form_factor("tv", Some(960.0)), "tv");
        assert_eq!(form_factor("", None), Value::Null);
    }

    #[test]
    fn elements_have_ids_parents_roles_dp_and_the_marks_numbers() {
        let nodes = tree::parse(LOGIN).unwrap();
        let (elements, total) = elements_from_nodes(&nodes, (1080, 2400), Some(2.625), 200);
        assert_eq!(elements.len(), nodes.len());
        assert_eq!(elements[0]["id"], "e0");
        assert_eq!(elements[0]["parent"], Value::Null);
        assert_eq!(elements[2]["parent"], "e1");
        let back = elements
            .iter()
            .find(|e| e["description"] == "Back")
            .unwrap();
        assert_eq!(back["role"], "button");
        assert_eq!(back["bounds"]["px"]["width"], 140);
        assert_eq!(back["bounds"]["dp"]["width"], 53.33);
        // The numbers are the ones screen.marks draws on the same hierarchy.
        let (marks, _) = tree::marks(&nodes, 200);
        assert_eq!(total, marks.len());
        for m in &marks {
            let found = elements.iter().find(|e| {
                e["bounds"]["px"]["left"] == m["bounds"]["left"]
                    && e["bounds"]["px"]["top"] == m["bounds"]["top"]
                    && e["mark"] == m["mark"]
                    && e["class"] == m["class"]
            });
            assert!(found.is_some(), "mark {} is on an element", m["mark"]);
        }
        let marked: Vec<u64> = elements.iter().filter_map(|e| e["mark"].as_u64()).collect();
        assert_eq!(marked, (1..=marks.len() as u64).collect::<Vec<_>>());
        let google = elements
            .iter()
            .find(|e| e["text"] == "Sign in with Google")
            .unwrap();
        assert_eq!(google["enabled"], false);
        assert_eq!(google["mark"], Value::Null);
    }

    #[test]
    fn a_limit_on_the_marks_keeps_the_same_numbers() {
        let nodes = tree::parse(LOGIN).unwrap();
        let (all, _) = elements_from_nodes(&nodes, (1080, 2400), None, 200);
        let (few, total) = elements_from_nodes(&nodes, (1080, 2400), None, 2);
        assert!(total > 2);
        for (a, f) in all.iter().zip(&few) {
            if f["mark"].is_u64() {
                assert_eq!(a["mark"], f["mark"]);
            }
        }
        assert_eq!(few.iter().filter(|e| e["mark"].is_u64()).count(), 2);
        assert_eq!(all[0]["bounds"]["dp"], Value::Null);
    }

    #[test]
    fn an_android_export_matches_its_recorded_bundle() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("bundle");
        let summary = export_login(&target);
        assert_eq!(summary["hierarchy"], "available");
        assert_eq!(summary["elements_source"], "hierarchy");
        assert!(target.join(SCREEN_PNG).exists() && target.join(MARKED_PNG).exists());
        let document = fixed(
            serde_json::from_str(&fs::read_to_string(target.join(SCREEN_JSON)).unwrap()).unwrap(),
        );
        assert_eq!(document["format"], FORMAT);
        assert_eq!(document["version"], 1);
        assert_eq!(document["screenshot"]["marked_file"], MARKED_PNG);
        assert_eq!(document["display"]["width_dp"], 411.43);
        assert_eq!(document["display"]["unit"], "dp");
        golden("screen_export_android.json", &document);
    }

    /// The recorded document, rewritten when TOMACCO_UPDATE_FIXTURES is set.
    fn golden(name: &str, document: &Value) {
        let file = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures/screen_export")
            .join(name);
        let text = serde_json::to_string_pretty(document).unwrap() + "\n";
        if std::env::var_os("TOMACCO_UPDATE_FIXTURES").is_some() {
            fs::create_dir_all(file.parent().unwrap()).unwrap();
            fs::write(&file, &text).unwrap();
        }
        assert_eq!(
            fs::read_to_string(&file).unwrap_or_default(),
            text,
            "{name} differs; run with TOMACCO_UPDATE_FIXTURES=1 to record it"
        );
    }

    const TSV: &str = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n5\t1\t1\t1\t1\t1\t100\t200\t300\t60\t95\tSettings\n5\t1\t1\t1\t2\t1\t100\t400\t200\t60\t90\tWi-Fi\n";

    #[test]
    fn a_screen_without_a_hierarchy_is_read_by_ocr_and_says_so() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("bundle");
        let (_keep, store) = store();
        let phone =
            catalogue_phone(None, (1206, 2622)).expect("a catalogue phone with this screen");
        let mut facts = Facts::default();
        facts.device = json!({"platform":"ios","id":"virtual-iphone","serial":null,"udid":null,"virtual":true,
            "model":phone["name"],"manufacturer":"Apple","os_name":"iOS","os_version":null,"sdk":null,"form_factor":"phone"});
        facts.scale = phone["dpr"].as_f64();
        facts.rotation = Some(0);
        facts.insets = phone_insets(&phone, facts.scale.unwrap(), "device catalogue (estimated)");
        let mut screen = Fake {
            picture: solid(1206, 2622, 20),
            xml: None,
            facts,
        };
        let summary = write_bundle(
            &json!({}),
            "virtual-iphone",
            &target,
            &mut screen,
            &mut Lines(TSV),
            &store,
        )
        .unwrap();
        assert_eq!(summary["hierarchy"], "unavailable");
        assert_eq!(summary["elements_source"], "ocr");
        assert!(!target.join(MARKED_PNG).exists());
        let document = fixed(
            serde_json::from_str(&fs::read_to_string(target.join(SCREEN_JSON)).unwrap()).unwrap(),
        );
        let first = &document["elements"][0];
        assert_eq!(first["source"], "ocr");
        assert_eq!(first["text"], "Settings");
        assert_eq!(first["clickable"], Value::Null);
        assert_eq!(first["mark"], 1);
        assert_eq!(first["bounds"]["dp"]["width"], 100.0);
        assert_eq!(document["display"]["unit"], "pt");
        assert_eq!(document["insets"]["estimated"], true);
        golden("screen_export_ocr.json", &document);
    }

    #[test]
    fn without_tesseract_the_export_still_works_and_warns() {
        let dir = tempfile::tempdir().unwrap();
        let (_keep, store) = store();
        let mut screen = Fake {
            picture: solid(100, 200, 0),
            xml: None,
            facts: Facts::default(),
        };
        let summary = write_bundle(
            &json!({}),
            "virtual-iphone",
            &dir.path().join("b"),
            &mut screen,
            &mut NoOcr,
            &store,
        )
        .unwrap();
        assert_eq!(summary["elements_source"], "none");
        assert_eq!(summary["elements"], 0);
        assert!(
            summary["warnings"][0]
                .as_str()
                .unwrap()
                .contains("tesseract")
        );
        assert_eq!(summary["device"]["platform"], Value::Null);
    }

    #[test]
    fn source_hierarchy_without_one_is_refused_and_ocr_skips_it() {
        let dir = tempfile::tempdir().unwrap();
        let (_keep, store) = store();
        let mut none = Fake {
            picture: solid(100, 200, 0),
            xml: None,
            facts: Facts::default(),
        };
        let error = write_bundle(
            &json!({"source":"hierarchy"}),
            "d",
            &dir.path().join("a"),
            &mut none,
            &mut NoOcr,
            &store,
        )
        .unwrap_err();
        assert_eq!(error.code, "ui_hierarchy_unavailable");
        let mut some = Fake {
            picture: solid(1080, 2400, 0),
            xml: Some(LOGIN.into()),
            facts: Facts::default(),
        };
        let summary = write_bundle(
            &json!({"source":"ocr"}),
            "d",
            &dir.path().join("b"),
            &mut some,
            &mut Lines(TSV),
            &store,
        )
        .unwrap();
        assert_eq!(summary["hierarchy"], "skipped");
        assert_eq!(summary["elements_source"], "ocr");
    }

    #[test]
    fn the_marks_of_an_export_can_be_tapped_after_it() {
        let dir = tempfile::tempdir().unwrap();
        let (_keep, store) = store();
        let mut screen = Fake {
            picture: solid(1080, 2400, 0),
            xml: Some(LOGIN.into()),
            facts: Facts::default(),
        };
        write_bundle(
            &json!({}),
            "emulator-5554",
            &dir.path().join("b"),
            &mut screen,
            &mut NoOcr,
            &store,
        )
        .unwrap();
        let (record, picture) = store.load("emulator-5554").unwrap();
        assert!(picture.is_some());
        assert_eq!(record["marks"][0]["mark"], 1);
    }

    #[test]
    fn an_export_directory_must_be_new_or_empty() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("x"), "1").unwrap();
        let error = fresh_directory(&dir.path().to_string_lossy()).unwrap_err();
        assert_eq!(error.code, "path_exists");
        assert!(fresh_directory(&dir.path().join("new").to_string_lossy()).is_ok());
    }

    #[test]
    fn a_batch_writes_a_bundle_per_device_and_an_index_even_when_one_fails() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("all");
        let (_keep, store) = store();
        let result = batch(
            &json!({"directory":root,"devices":["emulator-5554","emulator-5556"]}),
            &mut |a, device, target| {
                if device == "emulator-5556" {
                    return Err(Error::new("device_offline", "offline"));
                }
                let mut screen = Fake {
                    picture: solid(1080, 2400, 240),
                    xml: Some(LOGIN.into()),
                    facts: android_facts_fixture(),
                };
                write_bundle(a, device, target, &mut screen, &mut NoOcr, &store)
            },
        )
        .unwrap();
        assert_eq!(result["count"], 2);
        assert_eq!(result["failed"], 1);
        assert_eq!(result["ok"], false);
        let index: Value =
            serde_json::from_str(&fs::read_to_string(root.join("index.json")).unwrap()).unwrap();
        assert_eq!(index["format"], INDEX_FORMAT);
        assert_eq!(
            index["screens"][0]["screen_json"],
            "01-emulator-5554/screen.json"
        );
        assert_eq!(index["screens"][0]["model"], "sdk_gphone64_x86_64");
        assert_eq!(index["screens"][1]["error"]["code"], "device_offline");
        assert!(root.join("01-emulator-5554").join(SCREEN_PNG).exists());
        golden("screen_export_index.json", &fixed(index));
    }

    #[test]
    fn a_batch_needs_unique_devices() {
        let dir = tempfile::tempdir().unwrap();
        let error = batch(
            &json!({"directory":dir.path().join("x"),"devices":["a","a"]}),
            &mut |_, _, _| Ok(json!({})),
        )
        .unwrap_err();
        assert_eq!(error.code, "invalid_argument");
    }

    fn bundle_of(xml: &str, shade: u8, root: &Path, name: &str) -> PathBuf {
        let (_keep, store) = store();
        let mut screen = Fake {
            picture: solid(1080, 2400, shade),
            xml: Some(xml.into()),
            facts: android_facts_fixture(),
        };
        let target = root.join(name);
        write_bundle(
            &json!({}),
            "emulator-5554",
            &target,
            &mut screen,
            &mut NoOcr,
            &store,
        )
        .unwrap();
        target
    }

    #[test]
    fn two_bundles_of_one_screen_are_identical_and_a_change_is_described() {
        let dir = tempfile::tempdir().unwrap();
        let one = bundle_of(LOGIN, 240, dir.path(), "one");
        let two = bundle_of(LOGIN, 240, dir.path(), "two");
        let same = diff_bundles(&json!({"before":one,"after":two})).unwrap();
        assert_eq!(same["kind"], "bundle");
        assert_eq!(same["identical"], true, "{same}");
        let other = bundle_of(FILLED, 200, dir.path(), "other");
        let changed = diff_bundles(&json!({"before":one,"after":other.join(SCREEN_JSON)})).unwrap();
        assert_eq!(changed["identical"], false);
        assert_eq!(changed["elements"]["kind"], "hierarchy");
        assert!(
            !changed["elements"]["changed"]
                .as_array()
                .unwrap()
                .is_empty()
                || !changed["elements"]["added"].as_array().unwrap().is_empty()
        );
        assert_eq!(changed["pixels"]["identical"], false);
    }

    #[test]
    fn bundles_are_told_from_other_files_and_checked() {
        let dir = tempfile::tempdir().unwrap();
        let one = bundle_of(LOGIN, 240, dir.path(), "one");
        assert!(is_bundle(&one.to_string_lossy()));
        assert!(is_bundle(&one.join(SCREEN_JSON).to_string_lossy()));
        assert!(!is_bundle("/x/screen.png"));
        let bad = dir.path().join("bad");
        fs::create_dir_all(&bad).unwrap();
        fs::write(bad.join(SCREEN_JSON), r#"{"format":"other","version":1}"#).unwrap();
        assert_eq!(
            load(&bad.to_string_lossy()).err().unwrap().code,
            "invalid_export"
        );
        fs::write(
            bad.join(SCREEN_JSON),
            r#"{"format":"tomacco.screen","version":2}"#,
        )
        .unwrap();
        assert_eq!(
            load(&bad.to_string_lossy()).err().unwrap().code,
            "unsupported_export_version"
        );
        let error = diff_bundles(&json!({"before":one})).unwrap_err();
        assert_eq!(error.code, "invalid_argument");
    }

    #[test]
    fn ideviceinfo_lines_become_fields() {
        let info =
            parse_ideviceinfo("ProductType: iPhone17,3\nProductVersion: 26.1\nDeviceName: Phone\n");
        assert_eq!(info["ProductVersion"], "26.1");
        assert_eq!(info["ProductType"], "iPhone17,3");
    }
}
