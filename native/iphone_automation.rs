//! testing.run scenarios on a USB iPhone, through the WebDriverAgent session
//! `runtime.start` keeps. Selectors are read against WDA's XML source and
//! acted on with `runtime.input`. Tested against a stand-in for WDA only.
use crate::{core::*, devices, jobs, runtime};
use serde_json::{Value, json};
use std::{
    path::Path,
    thread,
    time::{Duration, Instant},
};

/// Selector properties an iPhone's accessibility tree has an answer for.
const UNSUPPORTED: [&str; 3] = ["clickable", "checked", "package"];
/// Scenario keys WDA can press; Android's KEYCODE names have no iOS match.
const KEYS: [&str; 5] = ["home", "enter", "backspace", "delete", "tab"];

/// What an iPhone scenario may not contain, checked before a run starts.
pub fn check_scenario(scenario: &Value) -> Result<()> {
    for (index, step) in scenario["steps"]
        .as_array()
        .into_iter()
        .flatten()
        .enumerate()
    {
        let refuse = |message: String| {
            Err(Error::new(
                "step_unsupported",
                format!("Step {index}: {message}"),
            ))
        };
        if let Some(key) = UNSUPPORTED
            .iter()
            .find(|key| step["selector"].get(**key).is_some())
        {
            return refuse(format!(
                "an iPhone selector has no {key}; use text, text_contains, resource_id (the accessibility identifier), description (the label), class, enabled or selected"
            ));
        }
        match s(step, "action") {
            "clear" => {
                return refuse(
                    "iOS cannot clear an app's data; uninstall and install it again".into(),
                );
            }
            "key" if !KEYS.contains(&s(step, "key")) => {
                return refuse(format!("an iPhone key is one of {}", KEYS.join(", ")));
            }
            _ => (),
        }
    }
    Ok(())
}

/// The connected `runtime.start` session of a USB iPhone.
pub fn session(device: &str) -> Result<String> {
    let sessions = runtime::handle("runtime.list", &json!({}))?;
    sessions
        .as_array()
        .into_iter()
        .flatten()
        .find(|v| {
            s(v, "platform") == "ios"
                && s(v, "device") == device
                && s(v, "status") == "connected"
                && s(v, "remote").is_empty()
        })
        .map(|v| s(v, "session").to_string())
        .ok_or_else(|| {
            Error::new(
                "runtime_required",
                format!("No connected iPhone session for {device}"),
            )
            .remedy("Start one with runtime.start platform=ios, device and runner_bundle_id, then run the scenario")
        })
}

/// WDA's XML source and the screen size in points, from its application element.
fn source(session: &str) -> Result<(String, f64, f64)> {
    let value = runtime::request(session, "hierarchy", &json!({"session":session}))?;
    let xml = value["xml"]
        .as_str()
        .ok_or_else(|| Error::new("invalid_hierarchy", "WebDriverAgent returned no XML source"))?
        .to_string();
    let (width, height) = {
        let tree = roxmltree::Document::parse(&xml)
            .map_err(|e| Error::new("invalid_hierarchy", e.to_string()))?;
        let root = tree
            .descendants()
            .find(|n| n.attribute("width").is_some() && n.attribute("height").is_some())
            .ok_or_else(|| Error::new("invalid_hierarchy", "The source has no screen size"))?;
        let size = |k| {
            root.attribute(k)
                .and_then(|v| v.parse::<f64>().ok())
                .unwrap_or(0.0)
        };
        (size("width"), size("height"))
    };
    if width <= 1.0 || height <= 1.0 {
        return Err(Error::new(
            "invalid_hierarchy",
            "The source has no usable screen size",
        ));
    }
    Ok((xml, width, height))
}

/// The visible elements of a WDA source that match a selector, each with its
/// attributes and a rect in points. text is the label, or the value of an
/// element without one; resource_id is the accessibility identifier (name);
/// description is the label; class is the element type, with or without
/// XCUIElementType.
pub fn select(xml: &str, selector: &Value) -> Result<Vec<Value>> {
    validate(selector, &crate::automation::selector_schema(), "selector")?;
    let fields = selector
        .as_object()
        .filter(|v| v.keys().any(|k| k != "index"))
        .ok_or_else(|| Error::new("invalid_selector", "Specify at least one semantic property"))?;
    if let Some(key) = UNSUPPORTED.iter().find(|k| fields.contains_key(**k)) {
        return Err(Error::new(
            "invalid_selector",
            format!("An iPhone selector has no {key}"),
        ));
    }
    let tree = roxmltree::Document::parse(xml)
        .map_err(|e| Error::new("invalid_hierarchy", e.to_string()))?;
    let mut matches = Vec::new();
    for node in tree.descendants().filter(|n| {
        n.is_element() && n.attribute("type").is_some() && n.attribute("visible") != Some("false")
    }) {
        let get = |k: &str| node.attribute(k).unwrap_or("");
        let text = if get("label").is_empty() {
            get("value")
        } else {
            get("label")
        };
        let ok = fields.iter().all(|(key, value)| {
            let wanted = value.as_str().unwrap_or("");
            match key.as_str() {
                "text" => text == wanted,
                "text_contains" => get("label").contains(wanted) || get("value").contains(wanted),
                "resource_id" => get("name") == wanted,
                "description" => get("label") == wanted,
                "class" => {
                    get("type") == wanted || get("type") == format!("XCUIElementType{wanted}")
                }
                "enabled" | "selected" => {
                    // WDA leaves selected out when it is false.
                    let actual = get(key) == "true";
                    value.as_bool() == Some(actual)
                }
                "index" => true,
                _ => false,
            }
        });
        if !ok {
            continue;
        }
        let mut entry = json!({});
        for attribute in node.attributes() {
            entry[attribute.name()] = json!(attribute.value());
        }
        let number = |k: &str| get(k).parse::<f64>().unwrap_or(0.0);
        let (x, y, w, h) = (number("x"), number("y"), number("width"), number("height"));
        entry["rect"] = json!({"left":x,"top":y,"right":x+w,"bottom":y+h,"x":x+w/2.0,"y":y+h/2.0});
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

/// Waits until the selector is present or absent, as testing.find does.
fn find(session: &str, step: &Value) -> Result {
    let timeout = step["timeout"].as_u64().unwrap_or(10);
    let expected = step["state"].as_str().unwrap_or("present");
    let deadline = Instant::now() + Duration::from_secs(timeout);
    loop {
        let (xml, width, height) = source(session)?;
        let found = select(&xml, &step["selector"])?;
        if (expected == "absent") == found.is_empty() {
            return Ok(json!({"matches":found,"state":expected,"width":width,"height":height}));
        }
        if Instant::now() >= deadline {
            return Err(Error::new(
                "element_timeout",
                format!("Element did not become {expected} within {timeout}s"),
            )
            .details(json!({"selector":step["selector"],"matches":found})));
        }
        thread::sleep(Duration::from_millis(200));
    }
}

/// A point in points as runtime.input's 0..1 coordinates.
fn normal(value: f64, size: f64) -> f64 {
    (value / (size - 1.0)).clamp(0.0, 1.0)
}

fn input(session: &str, mut args: Value) -> Result {
    args["session"] = json!(session);
    runtime::handle("runtime.input", &args)
}

/// click, long_click or fill on the one element a selector matches.
fn act(session: &str, step: &Value) -> Result {
    let mut query = step.clone();
    query["state"] = json!("present");
    let found = find(session, &query)?;
    let matches = found["matches"].as_array().unwrap();
    if matches.len() != 1 {
        return Err(Error::new(
            "ambiguous_selector",
            format!(
                "Selector matched {} elements; refine it or specify index",
                matches.len()
            ),
        )
        .details(found));
    }
    let element = &matches[0];
    if element["enabled"] == "false" {
        return Err(Error::new(
            "element_disabled",
            "Selected element is disabled",
        ));
    }
    let rect = &element["rect"];
    if rect["right"].as_f64() <= rect["left"].as_f64()
        || rect["bottom"].as_f64() <= rect["top"].as_f64()
    {
        return Err(Error::new(
            "invalid_bounds",
            "Selected element has empty bounds",
        ));
    }
    let x = normal(rect["x"].as_f64().unwrap_or(0.0), number(&found, "width"));
    let y = normal(rect["y"].as_f64().unwrap_or(0.0), number(&found, "height"));
    match s(step, "action") {
        "fill" => input(
            session,
            json!({"action":"fill","x":x,"y":y,"text":s(step,"text")}),
        )?,
        "long_click" => {
            input(
                session,
                json!({"action":"touch","phase":"down","x":x,"y":y}),
            )?;
            thread::sleep(Duration::from_millis(
                step["duration"].as_u64().unwrap_or(800),
            ));
            input(session, json!({"action":"touch","phase":"up","x":x,"y":y}))?
        }
        _ => input(session, json!({"action":"tap","x":x,"y":y}))?,
    };
    Ok(json!({"action":step["action"],"element":element}))
}

fn number(v: &Value, key: &str) -> f64 {
    v[key].as_f64().unwrap_or(0.0)
}

pub fn capture(session: &str, path: &Path) -> Result {
    runtime::handle("runtime.capture", &json!({"session":session,"path":path}))
}

/// One scenario step on an iPhone.
pub fn step(device: &str, session: &str, step: &Value, evidence: &Path) -> Result {
    match s(step, "action") {
        "wait" | "assert" => find(session, step),
        "click" | "long_click" | "fill" => act(session, step),
        "key" => input(session, json!({"action":"key","key":s(step,"key")})),
        "text" => input(session, json!({"action":"text","text":s(step,"text")})),
        "tap" | "swipe" => {
            let (_, width, height) = source(session)?;
            let mut args = json!({"action":step["action"],"x":normal(number(step,"x"),width),"y":normal(number(step,"y"),height)});
            if s(step, "action") == "swipe" {
                args["x2"] = json!(normal(number(step, "end_x"), width));
                args["y2"] = json!(normal(number(step, "end_y"), height));
                args["duration"] = json!(
                    (step["duration"].as_u64().unwrap_or(300) as f64 / 1000.0).clamp(0.01, 5.0)
                );
            }
            input(session, args)
        }
        "launch" | "stop" => {
            let job = devices::handle(
                "device.app",
                &json!({"platform":"ios","device":device,"bundle_id":s(step,"bundle_id"),"action":s(step,"action")}),
            )?;
            let done = jobs::wait(s(&job, "id"), 60)?;
            if s(&done, "state") == "succeeded" {
                Ok(done)
            } else {
                Err(Error::new("app_action_failed", "Device action failed").details(done))
            }
        }
        "screenshot" => capture(session, evidence),
        _ => Err(Error::new("invalid_step", "Unknown scenario action")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SOURCE: &str = r#"<?xml version="1.0" encoding="UTF-8"?><AppiumAUT><XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Counter" label="Counter" enabled="true" visible="true" x="0" y="0" width="390" height="844"><XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Count: 0" name="count" label="Count: 0" enabled="true" visible="true" x="20" y="100" width="200" height="30"/><XCUIElementTypeButton type="XCUIElementTypeButton" name="add" label="Add &amp; save" enabled="true" visible="true" x="20" y="200" width="100" height="44"/><XCUIElementTypeButton type="XCUIElementTypeButton" name="off" label="Later" enabled="false" visible="true" x="20" y="300" width="100" height="44"/><XCUIElementTypeTextField type="XCUIElementTypeTextField" name="who" value="Name" enabled="true" visible="true" x="20" y="400" width="300" height="40"/><XCUIElementTypeButton type="XCUIElementTypeButton" name="hidden" label="Add &amp; save" enabled="true" visible="false" x="0" y="900" width="10" height="10"/></XCUIElementTypeApplication></AppiumAUT>"#;

    #[test]
    fn wda_sources_are_matched_by_label_identifier_type_and_state() {
        let one = |selector: Value| select(SOURCE, &selector).unwrap();
        let add = one(json!({"text":"Add & save"}));
        assert_eq!(add.len(), 1, "the invisible twin is left out");
        assert_eq!(add[0]["rect"]["x"], 70.0);
        assert_eq!(add[0]["rect"]["y"], 222.0);
        assert_eq!(one(json!({"resource_id":"who"}))[0]["value"], "Name");
        assert_eq!(
            one(json!({"text":"Name"})).len(),
            1,
            "a field without a label matches by value"
        );
        assert_eq!(one(json!({"text_contains":"Count:"})).len(), 1);
        assert_eq!(one(json!({"class":"Button"})).len(), 2);
        assert_eq!(
            one(json!({"class":"XCUIElementTypeButton","enabled":false}))[0]["name"],
            "off"
        );
        assert_eq!(one(json!({"class":"Button","index":1}))[0]["name"], "off");
        assert_eq!(
            one(json!({"description":"Later","selected":false})).len(),
            1
        );
        assert!(select(SOURCE, &json!({"package":"x"})).is_err());
        assert!(select(SOURCE, &json!({"index":0})).is_err());
        assert_eq!(normal(389.0, 390.0), 1.0);
        assert_eq!(normal(-5.0, 390.0), 0.0);
    }

    #[test]
    fn iphone_scenarios_refuse_what_ios_cannot_do() {
        let scenario = |step: Value| json!({"name":"x","steps":[step]});
        check_scenario(&scenario(
            json!({"action":"click","selector":{"text":"Go"}}),
        ))
        .unwrap();
        check_scenario(&scenario(json!({"action":"key","key":"home"}))).unwrap();
        for step in [
            json!({"action":"clear","bundle_id":"dev.example.app"}),
            json!({"action":"key","key":"KEYCODE_BACK"}),
            json!({"action":"assert","selector":{"text":"Go","clickable":true}}),
        ] {
            let error = check_scenario(&scenario(step.clone())).unwrap_err();
            assert_eq!(error.code, "step_unsupported", "{step}");
        }
    }
}
