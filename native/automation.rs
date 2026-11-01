//! Semantic native Android actions and durable, device-serialized scenario
//! runs, on Android devices and on the virtual iPhone; USB iPhone scenario
//! steps are in iphone_automation.
use crate::ios_emulator::ui_testing;
use crate::{core::*, devices, jobs, toolchain};
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
    thread,
    time::{Duration, Instant},
};

pub fn selector_schema() -> Value {
    json!({"type":"object","minProperties":1,"additionalProperties":false,"properties":{
    "text":{"type":"string"},"text_contains":{"type":"string"},"resource_id":{"type":"string"},"description":{"type":"string"},"class":{"type":"string"},"package":{"type":"string"},
    "clickable":{"type":"boolean"},"enabled":{"type":"boolean"},"checked":{"type":"boolean"},"selected":{"type":"boolean"},"index":{"type":"integer","minimum":0,"maximum":10000}}})
}
pub fn select(xml: &str, selector: &Value) -> Result<Vec<Value>> {
    validate(selector, &selector_schema(), "selector")?;
    if selector
        .as_object()
        .is_none_or(|v| v.is_empty() || (v.len() == 1 && v.contains_key("index")))
    {
        return Err(Error::new(
            "invalid_selector",
            "Specify at least one semantic property",
        ));
    }
    let tree = roxmltree::Document::parse(xml)
        .map_err(|e| Error::new("invalid_hierarchy", e.to_string()))?;
    let bounds = regex::Regex::new(r"^\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]$").unwrap();
    let mut matches = Vec::new();
    for node in tree.descendants().filter(|n| n.has_tag_name("node")) {
        let matches_selector = selector.as_object().unwrap().iter().all(|(key, value)| {
            let attribute = match key.as_str() {
                "resource_id" => "resource-id",
                "description" => "content-desc",
                "text_contains" => "text",
                "index" => return true,
                _ => key,
            };
            let actual = node.attribute(attribute).unwrap_or("");
            if key == "text_contains" {
                actual.contains(value.as_str().unwrap_or(""))
            } else if let Some(b) = value.as_bool() {
                actual == if b { "true" } else { "false" }
            } else {
                actual == value.as_str().unwrap_or("")
            }
        });
        if !matches_selector {
            continue;
        }
        let rectangle = node.attribute("bounds").unwrap_or("");
        let mut entry = json!({});
        for attr in node.attributes() {
            entry[attr.name()] = json!(attr.value());
        }
        if let Some(c) = bounds.captures(rectangle) {
            let values: Vec<i64> = (1..=4).map(|i| c[i].parse::<i64>().unwrap_or(0)).collect();
            entry["rect"] = json!({"left":values[0],"top":values[1],"right":values[2],"bottom":values[3],"x":(values[0]+values[2])/2,"y":(values[1]+values[3])/2});
        }
        matches.push(entry);
    }
    if let Some(index) = selector["index"].as_u64() {
        matches = matches
            .into_iter()
            .nth(index as usize)
            .into_iter()
            .collect();
    }
    Ok(matches)
}
fn nodes(device: &str, selector: &Value) -> Result<Vec<Value>> {
    let value = devices::handle("android.hierarchy", &json!({"device":device}))?;
    select(s(&value, "xml"), selector)
}
pub fn find(a: &Value) -> Result {
    let device = s(a, "device");
    identifier(device, "device", None)?;
    if ui_testing::is_virtual(device) {
        return Err(ui_testing::no_hierarchy("testing.find"));
    }
    let timeout = a["timeout"].as_u64().unwrap_or(10);
    let expected = a["state"].as_str().unwrap_or("present");
    let deadline = Instant::now() + Duration::from_secs(timeout);
    let mut transport_retries = 0;
    loop {
        let values = match nodes(device, &a["selector"]) {
            Ok(value) => value,
            Err(e) => {
                let transient = e.code == "tool_timeout"
                    || (e.code == "tool_failed"
                        && e.details.as_ref().is_some_and(|v| {
                            v.to_string().contains("device offline")
                                || v.to_string().contains("device not found")
                        }));
                if transient && transport_retries < 2 && Instant::now() < deadline {
                    transport_retries += 1;
                    thread::sleep(Duration::from_millis(200));
                    continue;
                }
                return Err(e);
            }
        };
        if (expected == "absent" && values.is_empty())
            || (expected == "present" && !values.is_empty())
        {
            return Ok(
                json!({"matches":values,"state":expected,"device":device,"transport_retries":transport_retries}),
            );
        }
        if Instant::now() >= deadline {
            return Err(Error::new(
                "element_timeout",
                format!("Element did not become {expected} within {timeout}s"),
            )
            .details(json!({"selector":a["selector"],"matches":values,"device":device})));
        }
        thread::sleep(Duration::from_millis(150));
    }
}
pub fn action(a: &Value) -> Result {
    if ui_testing::is_virtual(s(a, "device")) {
        return Err(ui_testing::no_hierarchy("testing.action"));
    }
    if s(a, "action") == "fill" && a.get("text").is_none() {
        return Err(Error::new(
            "invalid_argument",
            "fill requires text; use an empty string to clear the field",
        ));
    }
    let instrumented =
        s(a, "action") == "fill" && crate::instrumentation::available(s(a, "device"))?;
    if s(a, "action") == "fill"
        && !instrumented
        && (!s(a, "text").is_ascii() || s(a, "text").contains("%s"))
    {
        return Err(Error::new(
            "instrumentation_required",
            "Unicode input requires Tomacco's Android instrumentation",
        )
        .remedy("Run testing.setup for this device, wait for success, then retry"));
    }
    let mut query = a.clone();
    query["state"] = json!("present");
    let result = find(&query)?;
    let matches = result["matches"].as_array().unwrap();
    if matches.len() != 1 {
        return Err(Error::new(
            "ambiguous_selector",
            format!(
                "Selector matched {} elements; refine it or specify index",
                matches.len()
            ),
        )
        .details(result));
    }
    let node = &matches[0];
    if node["enabled"] == "false" {
        return Err(Error::new(
            "element_disabled",
            "Selected element is disabled",
        ));
    }
    let rectangle = &node["rect"];
    let x = rectangle["x"]
        .as_i64()
        .ok_or_else(|| Error::new("invalid_bounds", "Selected node has no usable bounds"))?;
    let y = rectangle["y"].as_i64().unwrap_or(-1);
    if x < 0
        || y < 0
        || rectangle["right"].as_i64() <= rectangle["left"].as_i64()
        || rectangle["bottom"].as_i64() <= rectangle["top"].as_i64()
    {
        return Err(Error::new(
            "invalid_bounds",
            "Selected element has empty or off-screen bounds",
        ));
    }
    let device = s(a, "device");
    let op = s(a, "action");
    if instrumented {
        return crate::instrumentation::invoke(a);
    } else if op == "long_click" {
        devices::handle(
            "android.input",
            &json!({"device":device,"action":"swipe","x":x,"y":y,"end_x":x,"end_y":y,"duration":a["duration"].as_u64().unwrap_or(800)}),
        )?;
    } else {
        devices::handle(
            "android.input",
            &json!({"device":device,"action":"tap","x":x,"y":y}),
        )?;
        if op == "fill" {
            let text = s(a, "text");
            // Android's documented key combinations select the existing editable value.
            toolchain::run(
                &devices::shell(device, &argv(&["input", "keycombination", "113", "29"]))?,
                15,
                None,
                true,
            )?;
            devices::handle(
                "android.input",
                &json!({"device":device,"action":"keyevent","key":"KEYCODE_DEL"}),
            )?;
            devices::handle(
                "android.input",
                &json!({"device":device,"action":"text","text":text}),
            )?;
        }
    }
    Ok(json!({"device":device,"action":op,"element":node}))
}
fn step_schema() -> Value {
    json!({"type":"object","additionalProperties":false,"required":["action"],"properties":{
    "action":{"type":"string","enum":["wait","assert","click","long_click","fill","key","launch","stop","clear","screenshot","tap","swipe","text","pause","assert_screen","wait_screen","measure"]},"selector":selector_schema(),
    "baseline":{"type":"string","minLength":1},"threshold":{"type":"number","minimum":0,"maximum":1},"tolerance":{"type":"integer","minimum":0,"maximum":255},
    "ignore":{"type":"array","maxItems":100,"items":{"type":"object","additionalProperties":false,"required":["x","y","width","height"],
        "properties":{"x":{"type":"integer","minimum":0},"y":{"type":"integer","minimum":0},"width":{"type":"integer","minimum":1},"height":{"type":"integer","minimum":1}}}},
    "measure":{"type":"string","enum":["startup","frames_reset","frames","memory","usage"]},"mode":{"type":"string","enum":["cold","warm","hot"]},"runs":{"type":"integer","minimum":1,"maximum":30},"seconds":{"type":"integer","minimum":1,"maximum":600},"activity":{"type":"string"},"budget":crate::perf::budget_schema(),
    "x":{"type":"integer","minimum":0,"maximum":16384},"y":{"type":"integer","minimum":0,"maximum":16384},"end_x":{"type":"integer","minimum":0,"maximum":16384},"end_y":{"type":"integer","minimum":0,"maximum":16384},"duration":{"type":"integer","minimum":1,"maximum":10000},"state":{"type":"string","enum":["present","absent"]},"text":{"type":"string"},"key":{"type":"string"},"bundle_id":{"type":"string"},"label":{"type":"string","maxLength":150},"timeout":{"type":"integer","minimum":0,"maximum":60}}})
}
/// A scenario's shape and each step's required fields.
pub fn check_scenario(scenario: &Value) -> Result<()> {
    validate(
        scenario,
        &json!({"type":"object","additionalProperties":false,"required":["name","steps"],"properties":{"name":{"type":"string","minLength":1,"maxLength":150},"retries":{"type":"integer","minimum":0,"maximum":5},"budget":crate::perf::budget_schema(),"steps":{"type":"array","minItems":1,"maxItems":500,"items":step_schema()}}}),
        "scenario",
    )?;
    for step in scenario["steps"].as_array().unwrap() {
        if s(step, "action") == "fill" && step.get("text").is_none() {
            return Err(Error::new("invalid_argument", "A fill step requires text"));
        }
        match s(step, "action") {
            "wait" | "assert" | "click" | "long_click" | "fill" => {
                validate(&step["selector"], &selector_schema(), "selector")?;
            }
            "launch" | "stop" | "clear" => {
                identifier(s(step, "bundle_id"), "application ID", None)?;
            }
            "measure" => {
                identifier(s(step, "bundle_id"), "application ID", None)?;
                if step.get("measure").is_none() {
                    return Err(Error::new(
                        "invalid_argument",
                        "A measure step requires measure",
                    ));
                }
            }
            "tap" | "swipe" => {
                let keys: &[&str] = if s(step, "action") == "tap" {
                    &["x", "y"]
                } else {
                    &["x", "y", "end_x", "end_y"]
                };
                if let Some(key) = keys.iter().find(|key| step.get(**key).is_none()) {
                    return Err(Error::new(
                        "invalid_argument",
                        format!("A {} step requires {key}", s(step, "action")),
                    ));
                }
            }
            "text" if step.get("text").is_none() => {
                return Err(Error::new("invalid_argument", "A text step requires text"));
            }
            "pause" if step.get("duration").is_none() => {
                return Err(Error::new(
                    "invalid_argument",
                    "A pause step requires duration",
                ));
            }
            "assert_screen" | "wait_screen" if !Path::new(s(step, "baseline")).is_absolute() => {
                // A relative baseline, or none, names a file in testing.run's
                // baselines directory; it must stay inside it.
                let relative = s(step, "baseline");
                if relative.starts_with('/')
                    || relative.split('/').any(|part| part == "..")
                    || (!relative.is_empty() && !relative.ends_with(".png"))
                {
                    return Err(Error::new(
                        "invalid_argument",
                        format!(
                            "A {} step's baseline is the absolute path of a PNG, or a .png path inside testing.run's baselines directory",
                            s(step, "action")
                        ),
                    ));
                }
            }
            _ => (),
        }
    }
    Ok(())
}
/// The scenarios at path: one file, or every .json file directly in a
/// directory, in name order.
fn scenarios(at: &Path) -> Result<Vec<Value>> {
    if !at.is_dir() {
        let scenario = read_json(at, None)?;
        check_scenario(&scenario)?;
        return Ok(vec![scenario]);
    }
    let mut files: Vec<_> = fs::read_dir(at)?
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|file| file.is_file() && file.extension().is_some_and(|e| e == "json"))
        .collect();
    files.sort();
    if files.is_empty() || files.len() > 200 {
        return Err(Error::new(
            "invalid_argument",
            format!(
                "A scenario directory holds 1 to 200 .json files; {} has {}",
                at.display(),
                files.len()
            ),
        ));
    }
    files
        .iter()
        .map(|file| {
            let scenario = read_json(file, None)?;
            check_scenario(&scenario).map_err(|error| {
                let message = format!("{}: {}", file.display(), error.message);
                Error::new(&error.code, message)
            })?;
            Ok(scenario)
        })
        .collect()
}
pub fn run(a: &Value) -> Result {
    let source = path(s(a, "path"), false, true)?;
    let scenarios = scenarios(&source)?;
    let name = if source.is_dir() {
        source
            .file_name()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_else(|| "scenarios".into())
    } else {
        s(&scenarios[0], "name").to_string()
    };
    let mut scenarios = scenarios;
    if let Some(budget) = crate::perf::budget_argument(a)? {
        // The run's budget applies to each scenario without its own.
        for scenario in &mut scenarios {
            if scenario.get("budget").is_none() {
                scenario["budget"] = budget.clone();
            }
        }
    }
    let devices = strings(&a["devices"]);
    let unique: std::collections::HashSet<_> = devices.iter().collect();
    if unique.len() != devices.len() {
        return Err(Error::new("invalid_argument", "Device IDs must be unique"));
    }
    let platform = a["platform"].as_str().unwrap_or("android");
    for device in &devices {
        identifier(device, "device", None)?;
    }
    if platform == "ios" {
        for scenario in &scenarios {
            crate::iphone_automation::check_scenario(scenario)?;
        }
        for device in &devices {
            crate::iphone_device::udid(device)?;
            crate::iphone_automation::session(device)?;
        }
    }
    resolve_baselines(&mut scenarios, a)?;
    let output = path(s(a, "directory"), false, false)?;
    if output.exists() && fs::read_dir(&output)?.next().is_some() {
        return Err(Error::new(
            "path_exists",
            "Choose an empty report directory",
        ));
    }
    private_dir(&output)?;
    let spec = json!({"name":name,"scenarios":scenarios,"devices":devices,"directory":output,"parallelism":a["parallelism"].as_u64().unwrap_or(2),"retries":a["retries"].as_u64().unwrap_or(0),"shard":b(a,"shard"),"platform":platform,"source":source,"baselines":a["baselines"]});
    atomic_json(&output.join("run.json"), &spec)?;
    jobs::submit(
        &format!("Native UI · {name}"),
        json!({"steps":[{"argv":[crate::core::own_executable()?,"_automation",output.join("run.json")],"artifacts":[output.join("report.json"),output.join("junit.xml")]}],"cwd":output,"timeout":a["timeout"].as_u64().unwrap_or(3600),"always_artifacts":["report.json","junit.xml","report.html","*.png"]}),
    )
}
/// The file name a screen step without a baseline gets in the baselines
/// directory: the scenario's name and the step's label, or its index.
fn baseline_name(scenario: &str, step: &Value, index: usize) -> String {
    let slug = |text: &str| {
        let mut out = String::new();
        for c in text.chars() {
            if c.is_ascii_alphanumeric() {
                out.push(c.to_ascii_lowercase());
            } else if !out.ends_with('-') {
                out.push('-');
            }
        }
        out.trim_matches('-').chars().take(80).collect::<String>()
    };
    let screen = match slug(s(step, "label")) {
        label if !label.is_empty() => label,
        _ => format!("step-{index:03}"),
    };
    let scenario = match slug(scenario) {
        name if !name.is_empty() => name,
        _ => "scenario".into(),
    };
    format!("{scenario}/{screen}.png")
}
/// Makes every screen step's baseline absolute: a relative one, or none,
/// is a file in the baselines directory. With record_baselines, steps whose
/// baseline is missing keep their screenshot and the scenario goes on.
fn resolve_baselines(scenarios: &mut [Value], a: &Value) -> Result<()> {
    let directory = if s(a, "baselines").is_empty() {
        None
    } else {
        Some(path(s(a, "baselines"), false, false)?)
    };
    for scenario in scenarios.iter_mut() {
        let name = s(scenario, "name").to_string();
        for (index, step) in scenario["steps"]
            .as_array_mut()
            .into_iter()
            .flatten()
            .enumerate()
        {
            if !["assert_screen", "wait_screen"].contains(&s(step, "action")) {
                continue;
            }
            if !Path::new(s(step, "baseline")).is_absolute() {
                let Some(directory) = &directory else {
                    return Err(Error::new(
                        "invalid_argument",
                        format!(
                            "Step {index} of {name} has {}; give testing.run a baselines directory, or the step an absolute baseline",
                            if s(step, "baseline").is_empty() {
                                "no baseline".to_string()
                            } else {
                                format!("the relative baseline {}", s(step, "baseline"))
                            }
                        ),
                    ));
                };
                let relative = match s(step, "baseline") {
                    "" => baseline_name(&name, step, index),
                    given => given.to_string(),
                };
                step["baseline"] = json!(directory.join(relative));
            }
            if b(a, "record_baselines") {
                step["record_baselines"] = json!(true);
            }
        }
    }
    Ok(())
}
/// The images of a step in the report directory: its screenshot and
/// difference image, and the baseline it was compared with.
pub(crate) fn step_images(directory: &Path, tag: &str, index: usize, step: &Value) -> Value {
    let mut images = serde_json::Map::new();
    for (key, file) in [
        (
            "screenshot",
            directory.join(format!("{tag}-{index:03}.png")),
        ),
        ("diff", directory.join(format!("{tag}-{index:03}-diff.png"))),
        ("baseline", PathBuf::from(s(step, "baseline"))),
    ] {
        if file.is_absolute() && file.is_file() {
            images.insert(key.into(), json!(file));
        }
    }
    Value::Object(images)
}
/// The error that ends a scenario whose screen steps found no baseline in
/// record_baselines mode, naming the screenshots kept for approval.
pub(crate) fn new_baselines(steps: &[Value]) -> Option<Error> {
    let new: Vec<Value> = steps
        .iter()
        .filter(|step| b(&step["result"], "new_baseline"))
        .map(|step| json!({"index":step["index"],"baseline":step["result"]["baseline"],"actual":step["result"]["actual"]}))
        .collect();
    (!new.is_empty()).then(|| {
        Error::new(
            "baselines_missing",
            format!("{} screens have no baseline yet", new.len()),
        )
        .remedy(
            "Look at the screenshots with testing.baselines review, then approve the right ones",
        )
        .details(json!({"screens":new}))
    })
}
/// testing.lint: checks a scenario against the screen now, without acting.
pub fn lint(a: &Value) -> Result {
    let device = s(a, "device");
    let hierarchy = s(a, "hierarchy");
    if device.is_empty() == hierarchy.is_empty() {
        return Err(Error::new(
            "invalid_argument",
            "Give either device or hierarchy",
        ));
    }
    let scenario = read_json(&path(s(a, "path"), false, true)?, None)?;
    if let Err(error) = check_scenario(&scenario) {
        return Ok(
            json!({"name":scenario["name"],"valid":false,"passed":false,"error":error.value(),"findings":[]}),
        );
    }
    let xml = if device.is_empty() {
        let file = path(hierarchy, false, true)?;
        if fs::metadata(&file)?.len() > 16 * 1024 * 1024 {
            return Err(Error::new(
                "invalid_hierarchy",
                "The hierarchy is larger than 16 MiB",
            ));
        }
        fs::read_to_string(file)?
    } else {
        identifier(device, "device", None)?;
        if ui_testing::is_virtual(device) {
            return Err(ui_testing::no_hierarchy("testing.lint"));
        }
        s(
            &devices::handle("android.hierarchy", &json!({"device":device}))?,
            "xml",
        )
        .to_string()
    };
    roxmltree::Document::parse(&xml).map_err(|e| Error::new("invalid_hierarchy", e.to_string()))?;
    let (steps, findings) = lint_steps(&scenario, &xml);
    let passed = !findings.iter().any(|f| f["severity"] == "error");
    Ok(
        json!({"name":scenario["name"],"valid":true,"passed":passed,"steps":steps,"findings":findings}),
    )
}
/// Each step's matches on one screen, and what would go wrong: a selector
/// that matches nothing here (a warning, as an earlier step may open the
/// screen it is on), several elements where one is acted on, a disabled
/// element, a point off the screen or a malformed key.
fn lint_steps(scenario: &Value, xml: &str) -> (Vec<Value>, Vec<Value>) {
    let size = crate::recorder::screen(xml);
    let key = regex::Regex::new(r"^(?:KEYCODE_[A-Z0-9_]+|[0-9]{1,4})$").unwrap();
    let mut steps = Vec::new();
    let mut findings = Vec::new();
    for (index, step) in scenario["steps"].as_array().unwrap().iter().enumerate() {
        let action = s(step, "action");
        let mut finding = |severity: &str, code: &str, message: String| {
            findings.push(json!({"step":index,"action":action,"label":step["label"],"severity":severity,"code":code,"message":message}));
        };
        let mut entry = json!({"index":index,"action":action});
        match action {
            "wait" | "assert" | "click" | "long_click" | "fill" => {
                let matches = select(xml, &step["selector"]).unwrap_or_default();
                entry["matches"] = json!(matches.len());
                let absent = s(step, "state") == "absent";
                let acts = ["click", "long_click", "fill"].contains(&action);
                match matches.len() {
                    0 if !absent => finding(
                        "warning",
                        "not_found",
                        "No element on this screen matches the selector".into(),
                    ),
                    0 => (),
                    _ if absent => finding(
                        "warning",
                        "present",
                        format!(
                            "{} element(s) match a selector expected absent",
                            matches.len()
                        ),
                    ),
                    1 => {
                        if acts && matches[0]["enabled"] == "false" {
                            finding("error", "disabled", "The element is disabled".into());
                        }
                    }
                    count => finding(
                        if acts { "error" } else { "warning" },
                        "ambiguous",
                        format!("{count} elements match; refine the selector or add index"),
                    ),
                }
                if matches.len() > 1 {
                    entry["candidates"] = json!(
                        matches
                            .iter()
                            .take(5)
                            .map(|m| json!({"text":m["text"],"resource_id":m["resource-id"],"description":m["content-desc"],"bounds":m["bounds"]}))
                            .collect::<Vec<_>>()
                    );
                }
            }
            "tap" | "swipe" => {
                if let Some((width, height)) = size {
                    let points: &[(&str, &str)] = if action == "tap" {
                        &[("x", "y")]
                    } else {
                        &[("x", "y"), ("end_x", "end_y")]
                    };
                    for (x, y) in points {
                        if n(step, x) as i64 >= width || n(step, y) as i64 >= height {
                            finding(
                                "error",
                                "off_screen",
                                format!(
                                    "({}, {}) lies outside the {width}x{height} screen",
                                    n(step, x),
                                    n(step, y)
                                ),
                            );
                        }
                    }
                }
            }
            "key" if !key.is_match(s(step, "key")) => finding(
                "error",
                "invalid_key",
                "A key is a KEYCODE_ name or a number".into(),
            ),
            _ => (),
        }
        steps.push(entry);
    }
    (steps, findings)
}
fn xml_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}
pub fn worker(file: &str) -> Result<()> {
    let spec = read_json(&path(file, false, true)?, None)?;
    let directory = path(s(&spec, "directory"), true, true)?;
    let devices = strings(&spec["devices"]);
    let scenarios = spec["scenarios"].as_array().cloned().unwrap_or_default();
    let several = scenarios.len() > 1;
    let name = s(&spec, "name");
    let parallel = n(&spec, "parallelism").clamp(1, 8) as usize;
    let shard = b(&spec, "shard");
    let platform = spec["platform"].as_str().unwrap_or("android");
    let partial = std::sync::Mutex::new(Vec::new());
    let results = schedule(
        &devices,
        scenarios.len(),
        shard,
        parallel,
        |device, index| {
            let scenario = &scenarios[index];
            let retries = scenario["retries"]
                .as_u64()
                .unwrap_or(n(&spec, "retries"))
                .min(5);
            let mut tag = digest(device.as_bytes())[..12].to_string();
            if several {
                tag = format!("{tag}-s{index:03}");
            }
            let mut result = with_retries(retries, |attempt| {
                let tag = if attempt == 1 {
                    tag.clone()
                } else {
                    format!("{tag}-a{attempt}")
                };
                run_device(device, scenario, &directory, &tag, platform)
            });
            result["scenario"] = scenario["name"].clone();
            result["scenario_index"] = json!(index);
            let mut done = partial.lock().unwrap();
            done.push(result.clone());
            let _ = atomic_json(
                &directory.join("report.json"),
                &json!({"name":name,"created":now(),"results":*done,"complete":false}),
            );
            result
        },
    );
    let passed = !results.is_empty() && results.iter().all(|r| b(r, "passed"));
    let flaky: Vec<Value> = results
        .iter()
        .filter(|r| b(r, "flaky"))
        .map(|r| json!({"scenario":r["scenario"],"device":r["device"]}))
        .collect();
    let names: Vec<&Value> = scenarios.iter().map(|s| &s["name"]).collect();
    atomic_write(
        &directory.join("junit.xml"),
        junit(name, &results).as_bytes(),
    )?;
    atomic_json(
        &directory.join("report.json"),
        &json!({"name":name,"created":now(),"passed":passed,"complete":true,"scenarios":names,"shard":shard,
                "retries":spec["retries"],"flaky":flaky,"results":results}),
    )?;
    after_run(&directory, &spec);
    if !passed {
        return Err(Error::new(
            "scenario_failed",
            "One or more device scenarios failed; inspect report.json and screenshots",
        ));
    }
    Ok(())
}
/// After a run: its HTML report and its test history entries. Neither
/// changes the run's outcome; a failure is logged.
fn after_run(directory: &Path, spec: &Value) {
    match crate::test_report::write(directory, 0.0, None) {
        Ok(value) => println!("HTML report: {}", s(&value["html"], "path")),
        Err(error) => eprintln!("HTML report not written: {}", error.message),
    }
    let suite = match s(spec, "source") {
        "" => s(spec, "name").to_string(),
        source => source.to_string(),
    };
    let recorded = read_json(&directory.join("report.json"), None).and_then(|report| {
        let store = crate::test_history::store_path(&json!({}))?;
        crate::test_history::append(
            &store,
            crate::test_history::run_entries(directory, &report, &suite),
        )
    });
    if let Err(error) = recorded {
        eprintln!("Test history not recorded: {}", error.message);
    }
}
/// Runs `count` scenarios on the devices, at most `parallel` devices at a
/// time. Without shard every device runs every scenario; with shard each
/// scenario runs once, on whichever of the first `parallel` devices is free
/// next. Results come in scenario order, then device order.
fn schedule(
    devices: &[String],
    count: usize,
    shard: bool,
    parallel: usize,
    run: impl Fn(&str, usize) -> Value + Sync,
) -> Vec<Value> {
    let failed = |device: &str, index: usize| json!({"device":device,"scenario_index":index,"passed":false,"error":{"code":"worker_panic","message":"Scenario worker failed"}});
    let mut results: Vec<(usize, usize, Value)> = Vec::new();
    if shard {
        let queue = std::sync::Mutex::new((0..count).collect::<std::collections::VecDeque<_>>());
        thread::scope(|scope| {
            let handles: Vec<_> = devices
                .iter()
                .take(parallel)
                .enumerate()
                .map(|(position, device)| {
                    let (queue, run) = (&queue, &run);
                    scope.spawn(move || {
                        let mut mine = Vec::new();
                        loop {
                            let Some(index) = queue.lock().unwrap().pop_front() else {
                                break;
                            };
                            let result =
                                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                                    run(device, index)
                                }))
                                .unwrap_or_else(|_| failed(device, index));
                            mine.push((index, position, result));
                        }
                        mine
                    })
                })
                .collect();
            for handle in handles {
                results.extend(handle.join().unwrap_or_default());
            }
        });
    } else {
        for (chunk_number, chunk) in devices.chunks(parallel).enumerate() {
            thread::scope(|scope| {
                let handles: Vec<_> = chunk
                    .iter()
                    .enumerate()
                    .map(|(offset, device)| {
                        let run = &run;
                        let position = chunk_number * parallel + offset;
                        scope.spawn(move || {
                            (0..count)
                                .map(|index| {
                                    let result = std::panic::catch_unwind(
                                        std::panic::AssertUnwindSafe(|| run(device, index)),
                                    )
                                    .unwrap_or_else(|_| failed(device, index));
                                    (index, position, result)
                                })
                                .collect::<Vec<_>>()
                        })
                    })
                    .collect();
                for handle in handles {
                    results.extend(handle.join().unwrap_or_default());
                }
            });
        }
    }
    results.sort_by_key(|(index, position, _)| (*index, *position));
    results.into_iter().map(|(_, _, result)| result).collect()
}
/// Runs a scenario until it passes or `retries` more attempts have failed.
/// The last attempt's result is the result; `attempts` lists every one, and
/// `flaky` marks a pass after a failure.
fn with_retries(retries: u64, mut attempt: impl FnMut(u64) -> Value) -> Value {
    let mut attempts = Vec::new();
    let mut number = 1;
    loop {
        let mut result = attempt(number);
        let failed_step = result["steps"]
            .as_array()
            .and_then(|steps| steps.iter().find(|step| !b(step, "passed")))
            .map(|step| step["index"].clone());
        attempts.push(json!({"attempt":number,"passed":result["passed"],"duration_seconds":result["duration_seconds"],
                             "error":result["error"],"failed_step":failed_step}));
        if b(&result, "passed") || number > retries {
            result["flaky"] = json!(b(&result, "passed") && number > 1);
            result["attempts"] = json!(attempts);
            return result;
        }
        number += 1;
    }
}
/// JUnit XML of a run: one test case per device and scenario. Failed attempts
/// before a pass are flakyFailure elements, before a final failure
/// rerunFailure elements, as Maven Surefire writes them.
fn junit(name: &str, results: &[Value]) -> String {
    let mut xml = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?><testsuite name=\"{}\" tests=\"{}\" failures=\"{}\">",
        xml_escape(name),
        results.len(),
        results.iter().filter(|r| !b(r, "passed")).count()
    );
    for result in results {
        let classname = match s(result, "scenario") {
            "" => String::new(),
            scenario => format!(" classname=\"{}\"", xml_escape(scenario)),
        };
        xml.push_str(&format!(
            "<testcase{classname} name=\"{}\" time=\"{}\">",
            xml_escape(s(result, "device")),
            result["duration_seconds"]
        ));
        let attempts = result["attempts"].as_array().cloned().unwrap_or_default();
        let earlier = attempts.len().saturating_sub(1);
        let element = if b(result, "passed") {
            "flakyFailure"
        } else {
            xml.push_str(&format!(
                "<failure message=\"Native UI scenario failed\">{}</failure>",
                xml_escape(&result.to_string())
            ));
            "rerunFailure"
        };
        for attempt in &attempts[..earlier] {
            xml.push_str(&format!(
                "<{element} message=\"{}\">{}</{element}>",
                xml_escape(s(&attempt["error"], "message")),
                xml_escape(&attempt.to_string())
            ));
        }
        xml.push_str("</testcase>");
    }
    xml.push_str("</testsuite>\n");
    xml
}
/// The apps a scenario launches, stops or clears: a crash of one of them
/// fails the run.
fn scenario_apps(scenario: &Value) -> Vec<String> {
    let mut apps: Vec<String> = scenario["steps"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|step| !s(step, "bundle_id").is_empty())
        .map(|step| s(step, "bundle_id").to_string())
        .collect();
    apps.sort();
    apps.dedup();
    apps
}
fn app_crash(events: &[Value], apps: &[String]) -> Option<Error> {
    let event = events
        .iter()
        .find(|e| apps.iter().any(|app| app == s(e, "package")))?;
    let what = if event["kind"] == "anr" {
        "stopped responding"
    } else {
        "crashed"
    };
    Some(
        Error::new(
            "app_crashed",
            format!("{} {what} during the scenario", s(event, "package")),
        )
        .details(event.clone()),
    )
}
/// The lock that serializes scenarios on one device.
fn device_lock(device: &str) -> Result<Lock> {
    let lock = Lock::open(
        &home()?
            .join("locks")
            .join(format!("automation-{}.lock", digest(device.as_bytes()))),
    )?;
    lock.acquire(false)?;
    Ok(lock)
}
/// A screen step: `assert_screen` takes one screenshot to `actual` and
/// compares it with the step's baseline as testing.visual_diff does;
/// `wait_screen` takes them until one matches or the step's timeout passes.
/// A failure writes the difference image to `diff`.
pub(crate) fn screen_step(
    step: &Value,
    actual: &Path,
    diff: &Path,
    mut capture: impl FnMut(&Path) -> Result<()>,
) -> Result {
    let wait = s(step, "action") == "wait_screen";
    let deadline = Instant::now() + Duration::from_secs(step["timeout"].as_u64().unwrap_or(10));
    let mut arguments = json!({"actual":actual,"baseline":step["baseline"],
        "threshold":step["threshold"].as_f64().unwrap_or(0.0),"tolerance":n(step, "tolerance")});
    // Regions left out, such as the status bar's clock, which changes every minute.
    if step["ignore"].is_array() {
        arguments["ignore"] = step["ignore"].clone();
    }
    let baseline = PathBuf::from(s(step, "baseline"));
    if !baseline.is_file() {
        capture(actual)?;
        if b(step, "record_baselines") {
            return Ok(
                json!({"passed":true,"new_baseline":true,"baseline":baseline,"actual":actual}),
            );
        }
        return Err(Error::new(
            "baseline_missing",
            format!("No baseline at {}", baseline.display()),
        )
        .remedy("Look at the screenshot; testing.baselines review lists it and approve keeps it as the baseline")
        .details(json!({"baseline":baseline,"actual":actual})));
    }
    loop {
        capture(actual)?;
        let last = !wait || Instant::now() >= deadline;
        if last {
            arguments["diff"] = json!(diff);
        }
        let result = crate::visual::visual_diff(&arguments)?;
        if b(&result, "passed") {
            return Ok(result);
        }
        if last {
            let (code, message) = if wait {
                (
                    "screen_timeout",
                    "The screen did not match the baseline in time",
                )
            } else {
                ("screen_mismatch", "The screen does not match the baseline")
            };
            return Err(Error::new(code, message).details(result));
        }
        thread::sleep(Duration::from_millis(500));
    }
}
pub fn run_device(
    device: &str,
    scenario: &Value,
    directory: &Path,
    tag: &str,
    platform: &str,
) -> Value {
    if ui_testing::is_virtual(device) {
        return match device_lock(device) {
            Ok(_lock) => ui_testing::run_device(
                &mut ui_testing::LivePhone::default(),
                scenario,
                directory,
                tag,
            ),
            Err(error) => {
                json!({"device":device,"passed":false,"duration_seconds":0.0,"steps":[],"error":error.value()})
            }
        };
    }
    let started = Instant::now();
    let iphone = platform == "ios";
    // The emulator shares this computer's clock; a second of margin.
    let since = now() - 1.0;
    let mut steps = Vec::new();
    // The metrics of the scenario's measure steps; a later one replaces an
    // earlier value.
    let mut metrics = serde_json::Map::new();
    let mut budget = Value::Null;
    let result = (|| -> Result<()> {
        let _lock = device_lock(device)?;
        let session = if iphone {
            crate::iphone_automation::session(device)?
        } else {
            String::new()
        };
        for (index, step) in scenario["steps"].as_array().unwrap().iter().enumerate() {
            let begin = Instant::now();
            let mut args = step.clone();
            args["device"] = json!(device);
            if args.get("timeout").is_none() {
                args["timeout"] = json!(10);
            }
            let evidence = directory.join(format!("{tag}-{index:03}.png"));
            let result = (|| -> Result {
                if iphone {
                    return crate::iphone_automation::step(device, &session, &args, &evidence);
                }
                match s(step, "action") {
                    "wait" | "assert" => find(&args),
                    "click" | "long_click" | "fill" => action(&args),
                    "key" => devices::handle(
                        "android.input",
                        &json!({"device":device,"action":"keyevent","key":s(step,"key")}),
                    ),
                    "tap" => devices::handle(
                        "android.input",
                        &json!({"device":device,"action":"tap","x":step["x"],"y":step["y"]}),
                    ),
                    "swipe" => devices::handle(
                        "android.input",
                        &json!({"device":device,"action":"swipe","x":step["x"],"y":step["y"],"end_x":step["end_x"],"end_y":step["end_y"],"duration":step["duration"].as_u64().unwrap_or(300)}),
                    ),
                    "text" => devices::handle(
                        "android.input",
                        &json!({"device":device,"action":"text","text":s(step,"text")}),
                    ),
                    "launch" | "stop" | "clear" => {
                        let job = devices::handle(
                            "device.app",
                            &json!({"platform":"android","device":device,"bundle_id":s(step,"bundle_id"),"action":if s(step,"action")=="clear"{"clear-data"}else{s(step,"action")}}),
                        )?;
                        let done = jobs::wait(s(&job, "id"), 60)?;
                        if s(&done, "state") == "succeeded" {
                            Ok(done)
                        } else {
                            Err(Error::new("app_action_failed", "Device action failed")
                                .details(done))
                        }
                    }
                    "screenshot" => {
                        screenshot(device, &directory.join(format!("{tag}-{index:03}.png")))
                    }
                    "pause" => {
                        thread::sleep(Duration::from_millis(n(step, "duration")));
                        Ok(json!({"paused_ms":n(step, "duration")}))
                    }
                    "assert_screen" | "wait_screen" => screen_step(
                        &args,
                        &directory.join(format!("{tag}-{index:03}.png")),
                        &directory.join(format!("{tag}-{index:03}-diff.png")),
                        |path| screenshot(device, path).map(|_| ()),
                    ),
                    "measure" => {
                        let mut report = crate::perf::measure(device, step)?;
                        let values = report["metrics"].as_object().cloned().unwrap_or_default();
                        metrics.extend(values.clone());
                        if step.get("budget").is_some() {
                            let (passed, results) =
                                crate::perf::check_budget(&step["budget"], &values);
                            if !passed {
                                return Err(crate::perf::budget_error(&results));
                            }
                            report["budget"] = json!(results);
                        }
                        Ok(report)
                    }
                    _ => Err(Error::new("invalid_step", "Unknown scenario action")),
                }
            })();
            let passed = result.is_ok();
            steps.push(json!({"index":index,"action":step["action"],"label":step["label"],"duration_seconds":begin.elapsed().as_secs_f64(),"passed":passed,"result":match &result{Ok(v)=>v.clone(),Err(e)=>json!({"error":e.value()})},"images":step_images(directory, tag, index, step)}));
            if let Err(error) = result {
                let failure = directory.join(format!("{tag}-failure.png"));
                let capture = if iphone {
                    crate::iphone_automation::capture(&session, &failure)
                } else {
                    screenshot(device, &failure)
                };
                steps.last_mut().unwrap()["failure_capture"] =
                    capture.unwrap_or_else(|e| json!({"error":e.value()}));
                return Err(error);
            }
        }
        if let Some(error) = new_baselines(&steps) {
            return Err(error);
        }
        if scenario.get("budget").is_some() {
            let (passed, results) = crate::perf::check_budget(&scenario["budget"], &metrics);
            budget = json!({"passed":passed,"results":results});
            if !passed {
                return Err(crate::perf::budget_error(&results));
            }
        }
        Ok(())
    })();
    let (crashes, crashes_error) = if iphone {
        (
            Vec::new(),
            json!({"code":"not_checked","message":"An iPhone run does not read crashes; ios.crash_reports lists them"}),
        )
    } else {
        match crate::crashes::on_device(device, "", since) {
            Ok(events) => (events, Value::Null),
            Err(error) => (Vec::new(), error.value()),
        }
    };
    let result = result.and_then(|()| match app_crash(&crashes, &scenario_apps(scenario)) {
        Some(error) => Err(error),
        None => Ok(()),
    });
    json!({"device":device,"passed":result.is_ok(),"duration_seconds":started.elapsed().as_secs_f64(),"steps":steps,"error":result.err().map(|e|e.value()),"crashes":crashes,"crashes_error":crashes_error,"metrics":metrics,"budget":budget})
}
fn screenshot(device: &str, path: &Path) -> Result {
    let mut command = devices::adb(device)?;
    command.extend(argv(&["exec-out", "screencap", "-p"]));
    let job = jobs::submit(
        "UI evidence",
        json!({"steps":[{"argv":command,"output":path}],"cwd":home()?,"timeout":30,"env":toolchain::environment()?}),
    )?;
    let done = jobs::wait(s(&job, "id"), 30)?;
    if s(&done, "state") != "succeeded" {
        return Err(Error::new("capture_failed", "Evidence capture failed").details(done));
    }
    Ok(done["result"]["artifacts"].clone())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn a_crash_of_a_scenario_app_fails_the_run() {
        let scenario = json!({"name":"x","steps":[{"action":"launch","bundle_id":"dev.example.app"},{"action":"click","selector":{"text":"Go"}},{"action":"stop","bundle_id":"dev.example.app"}]});
        let apps = scenario_apps(&scenario);
        assert_eq!(apps, ["dev.example.app"]);
        let other = json!({"kind":"crash","package":"com.android.systemui"});
        assert!(app_crash(std::slice::from_ref(&other), &apps).is_none());
        let anr = json!({"kind":"anr","package":"dev.example.app"});
        let error = app_crash(&[other, anr], &apps).unwrap();
        assert_eq!(error.code, "app_crashed");
        assert!(error.message.contains("stopped responding"));
    }
    #[test]
    fn coordinate_and_text_steps_need_their_fields() {
        let scenario = |step: Value| json!({"name":"x","steps":[step]});
        for step in [
            json!({"action":"tap","x":10,"y":20}),
            json!({"action":"swipe","x":10,"y":20,"end_x":10,"end_y":900,"duration":300}),
            json!({"action":"text","text":"hello"}),
        ] {
            check_scenario(&scenario(step)).unwrap();
        }
        for step in [
            json!({"action":"tap","x":10}),
            json!({"action":"swipe","x":10,"y":20,"end_x":10}),
            json!({"action":"text"}),
            json!({"action":"tap","x":-1,"y":20}),
        ] {
            assert!(check_scenario(&scenario(step.clone())).is_err(), "{step}");
        }
    }
    #[test]
    fn screen_and_pause_steps_need_their_fields() {
        let scenario = |step: Value| json!({"name":"x","steps":[step]});
        for step in [
            json!({"action":"pause","duration":500}),
            json!({"action":"assert_screen","baseline":"/absolute/home.png","threshold":0.01,"tolerance":8}),
            json!({"action":"wait_screen","baseline":"/absolute/home.png","timeout":30}),
            // Resolved in testing.run's baselines directory.
            json!({"action":"assert_screen"}),
            json!({"action":"wait_screen","baseline":"login/relative.png"}),
        ] {
            check_scenario(&scenario(step)).unwrap();
        }
        for step in [
            json!({"action":"pause"}),
            json!({"action":"wait_screen","baseline":"../relative.png"}),
            json!({"action":"wait_screen","baseline":"relative.txt"}),
            json!({"action":"assert_screen","baseline":"/a.png","threshold":2}),
            json!({"action":"assert_screen","baseline":"/a.png","tolerance":256}),
        ] {
            assert!(check_scenario(&scenario(step.clone())).is_err(), "{step}");
        }
    }
    #[test]
    fn screen_steps_resolve_in_the_baselines_directory_and_record_new_screens() {
        let root = tempfile::tempdir().unwrap();
        let base = root.path().join("baselines");
        let mut scenarios = vec![json!({"name":"Log in","steps":[
            {"action":"assert_screen","label":"Home screen"},
            {"action":"wait_screen","baseline":"shared/done.png"},
            {"action":"assert_screen"},
            {"action":"assert_screen","baseline":"/elsewhere/x.png"},
            {"action":"tap","x":1,"y":1}]})];
        let refused = resolve_baselines(&mut scenarios.clone(), &json!({})).unwrap_err();
        assert!(refused.message.contains("baselines directory"));
        resolve_baselines(
            &mut scenarios,
            &json!({"baselines":base,"record_baselines":true}),
        )
        .unwrap();
        let steps = &scenarios[0]["steps"];
        assert_eq!(
            steps[0]["baseline"],
            json!(base.join("log-in/home-screen.png"))
        );
        assert_eq!(steps[1]["baseline"], json!(base.join("shared/done.png")));
        assert_eq!(
            steps[2]["baseline"],
            json!(base.join("log-in/step-002.png"))
        );
        assert_eq!(steps[3]["baseline"], "/elsewhere/x.png");
        assert_eq!(steps[0]["record_baselines"], true);
        assert!(steps[4].get("record_baselines").is_none());
        // A missing baseline: recorded as a candidate, or refused.
        let actual = root.path().join("shot.png");
        let picture = crate::visual::encode(&crate::visual::Image {
            width: 1,
            height: 1,
            pixels: vec![1, 2, 3, 255],
        })
        .unwrap();
        let capture = |path: &Path| -> Result<()> {
            fs::write(path, &picture)?;
            Ok(())
        };
        let recorded =
            screen_step(&steps[0], &actual, &root.path().join("d.png"), capture).unwrap();
        assert_eq!(recorded["new_baseline"], true);
        assert!(actual.is_file());
        assert!(!base.join("log-in/home-screen.png").exists());
        let mut plain = steps[0].clone();
        plain["record_baselines"] = json!(false);
        let missing =
            screen_step(&plain, &actual, &root.path().join("d.png"), capture).unwrap_err();
        assert_eq!(missing.code, "baseline_missing");
        assert_eq!(missing.details.unwrap()["actual"], json!(actual));
        let report = [
            json!({"index":0,"result":recorded}),
            json!({"index":1,"result":{"passed":true}}),
        ];
        let error = new_baselines(&report).unwrap();
        assert_eq!(error.code, "baselines_missing");
        assert_eq!(error.details.unwrap()["screens"][0]["index"], 0);
        assert!(new_baselines(&report[1..]).is_none());
        // The step's images in the report directory.
        fs::write(root.path().join("tag-000.png"), &picture).unwrap();
        let images = step_images(root.path(), "tag", 0, &json!({"baseline":actual}));
        assert_eq!(
            images,
            json!({"screenshot":root.path().join("tag-000.png"),"baseline":actual})
        );
    }
    #[test]
    fn element_operations_on_the_virtual_iphone_say_it_has_no_hierarchy() {
        let selector = json!({"text":"OK"});
        let refused = [
            find(&json!({"device":"virtual-iphone","selector":selector})),
            action(&json!({"device":"virtual-iphone","selector":selector,"action":"click"})),
            crate::accessibility::accessibility(&json!({"device":"virtual-iphone"})),
        ];
        for result in refused {
            let error = result.unwrap_err();
            assert_eq!(error.code, "ui_hierarchy_unavailable");
            assert_eq!(error.details.unwrap()["testing"]["run_coordinates"], true);
        }
        let directory = tempfile::tempdir().unwrap();
        let file = directory.path().join("s.json");
        atomic_json(
            &file,
            &json!({"name":"x","steps":[{"action":"tap","x":1,"y":1}]}),
        )
        .unwrap();
        let error = lint(&json!({"path":file,"device":"virtual-iphone"})).unwrap_err();
        assert_eq!(error.code, "ui_hierarchy_unavailable");
        // On Android, process is refused rather than ignored.
        let error = crate::crashes::crashes(&json!({"device":"emulator-5554","process":"MyApp"}))
            .unwrap_err();
        assert_eq!(error.code, "invalid_argument");
    }
    #[test]
    fn measure_steps_and_budgets_are_checked() {
        let measure = json!({"action":"measure","measure":"startup","bundle_id":"dev.example.app","mode":"cold","runs":3,
                             "budget":{"startup_cold_median_ms":{"maximum":800}}});
        let mut scenario =
            json!({"name":"x","steps":[measure],"budget":{"jank_percent":{"maximum":5}}});
        check_scenario(&scenario).unwrap();
        for (key, bad) in [
            ("measure", json!("battery")),
            ("budget", json!({"jank_percent":5})),
            ("mode", json!("lukewarm")),
        ] {
            let mut broken = scenario.clone();
            broken["steps"][0][key] = bad;
            assert!(check_scenario(&broken).is_err(), "{key}");
        }
        scenario["steps"][0]
            .as_object_mut()
            .unwrap()
            .remove("measure");
        assert_eq!(
            check_scenario(&scenario).unwrap_err().message,
            "A measure step requires measure"
        );
        scenario["steps"][0]["measure"] = json!("memory");
        scenario["steps"][0]["bundle_id"] = json!("");
        assert!(check_scenario(&scenario).is_err());
    }
    #[test]
    fn lint_reports_missing_ambiguous_and_disabled_elements_and_bad_points() {
        let xml = r#"<hierarchy><node bounds="[0,0][1080,2400]"><node text="OK" enabled="true" bounds="[0,10][20,30]"/><node text="OK" enabled="true" bounds="[30,10][60,30]"/><node text="Save" enabled="false" bounds="[0,40][60,80]"/><node text="Home" enabled="true" bounds="[0,90][60,120]"/></node></hierarchy>"#;
        let scenario = json!({"name":"x","steps":[
            {"action":"assert","selector":{"text":"Home"}},
            {"action":"click","selector":{"text":"OK"}},
            {"action":"assert","selector":{"text":"OK"}},
            {"action":"click","selector":{"text":"OK","index":1}},
            {"action":"click","selector":{"text":"Save"}},
            {"action":"wait","selector":{"text":"Later"}},
            {"action":"wait","selector":{"text":"Later"},"state":"absent"},
            {"action":"assert","selector":{"text":"Home"},"state":"absent"},
            {"action":"tap","x":100,"y":2400},
            {"action":"key","key":"back"},
            {"action":"key","key":"KEYCODE_BACK"}
        ]});
        check_scenario(&scenario).unwrap();
        let (steps, findings) = lint_steps(&scenario, xml);
        assert_eq!(steps.len(), 11);
        assert_eq!(steps[1]["matches"], 2);
        assert_eq!(steps[1]["candidates"].as_array().unwrap().len(), 2);
        let found: Vec<(u64, &str, &str)> = findings
            .iter()
            .map(|f| (f["step"].as_u64().unwrap(), s(f, "code"), s(f, "severity")))
            .collect();
        assert_eq!(
            found,
            [
                (1, "ambiguous", "error"),
                (2, "ambiguous", "warning"),
                (4, "disabled", "error"),
                (5, "not_found", "warning"),
                (7, "present", "warning"),
                (8, "off_screen", "error"),
                (9, "invalid_key", "error"),
            ]
        );
    }
    #[test]
    fn a_scenario_is_retried_and_a_late_pass_is_flaky() {
        let outcome = |passes: &'static [bool]| {
            move |attempt: u64| {
                let passed = passes[attempt as usize - 1];
                json!({"device":"emulator-5554","passed":passed,"duration_seconds":1.0,
                       "steps":[{"index":0,"passed":true},{"index":1,"passed":passed}],
                       "error":if passed {Value::Null} else {json!({"code":"element_timeout","message":"Element did not become present"})}})
            }
        };
        let first = with_retries(2, outcome(&[true]));
        assert_eq!(
            (&first["flaky"], first["attempts"].as_array().unwrap().len()),
            (&json!(false), 1)
        );
        let late = with_retries(2, outcome(&[false, true]));
        assert_eq!(
            (&late["passed"], &late["flaky"]),
            (&json!(true), &json!(true))
        );
        assert_eq!(late["attempts"][0]["failed_step"], 1);
        assert_eq!(late["attempts"][1]["passed"], true);
        let never = with_retries(2, outcome(&[false, false, false]));
        assert_eq!(
            (&never["passed"], &never["flaky"]),
            (&json!(false), &json!(false))
        );
        assert_eq!(never["attempts"].as_array().unwrap().len(), 3);
        // Without retries a failure is final.
        assert_eq!(
            with_retries(0, outcome(&[false, true]))["attempts"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        let xml = junit("Login", &[first, late, never]);
        assert!(xml.contains("tests=\"3\" failures=\"1\""));
        assert_eq!(
            xml.matches("<flakyFailure message=\"Element did not become present\">")
                .count(),
            1
        );
        assert_eq!(xml.matches("<rerunFailure").count(), 2);
        assert_eq!(xml.matches("<failure ").count(), 1);
        roxmltree::Document::parse(&xml).unwrap();
    }
    #[test]
    fn shards_split_scenarios_across_devices_and_a_matrix_runs_all_on_each() {
        let devices: Vec<String> = ["a", "b", "c"].map(String::from).to_vec();
        let ran = std::sync::Mutex::new(Vec::new());
        let run = |device: &str, index: usize| {
            ran.lock().unwrap().push((device.to_string(), index));
            std::thread::sleep(std::time::Duration::from_millis(5));
            json!({"device":device,"scenario_index":index,"passed":true})
        };
        let results = schedule(&devices, 5, true, 2, run);
        assert_eq!(results.len(), 5);
        let order: Vec<u64> = results
            .iter()
            .map(|r| r["scenario_index"].as_u64().unwrap())
            .collect();
        assert_eq!(order, [0, 1, 2, 3, 4]);
        let mut ran_on: Vec<String> = ran.lock().unwrap().iter().map(|(d, _)| d.clone()).collect();
        ran_on.sort();
        ran_on.dedup();
        // Only the first `parallel` devices take work.
        assert_eq!(ran_on, ["a", "b"]);
        ran.lock().unwrap().clear();
        let results = schedule(&devices, 2, false, 2, run);
        let pairs: Vec<(String, u64)> = results
            .iter()
            .map(|r| {
                (
                    s(r, "device").to_string(),
                    r["scenario_index"].as_u64().unwrap(),
                )
            })
            .collect();
        let expected: Vec<(String, u64)> =
            [("a", 0), ("b", 0), ("c", 0), ("a", 1), ("b", 1), ("c", 1)]
                .map(|(d, i)| (d.to_string(), i))
                .to_vec();
        assert_eq!(pairs, expected);
        // A scenario whose worker panics fails; the others still run.
        let results = schedule(&devices[..1], 2, true, 1, |device: &str, index: usize| {
            assert!(index != 0, "boom");
            json!({"device":device,"scenario_index":index,"passed":true})
        });
        assert_eq!(results[0]["error"]["code"], "worker_panic");
        assert_eq!(results[1]["passed"], true);
    }
    #[test]
    fn selectors_handle_escaping_ambiguity_and_index() {
        let xml = r#"<hierarchy><node text="A &amp; B" resource-id="app:id/button" enabled="true" bounds="[0,10][20,30]"/><node text="A &amp; B" enabled="false" bounds="[30,10][60,30]"/></hierarchy>"#;
        assert_eq!(select(xml, &json!({"text":"A & B"})).unwrap().len(), 2);
        assert_eq!(
            select(xml, &json!({"text":"A & B","enabled":true})).unwrap()[0]["rect"]["x"],
            10
        );
        assert_eq!(
            select(xml, &json!({"text":"A & B","index":1})).unwrap()[0]["enabled"],
            "false"
        );
        assert!(select(xml, &json!({})).is_err());
        assert!(
            select(
                "<!DOCTYPE h [<!ENTITY x SYSTEM 'file:///etc/passwd'>]><h>&x;</h>",
                &json!({"text":"x"})
            )
            .is_err()
        );
    }
}
