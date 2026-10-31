//! An accessibility audit of an Android screen from its UI hierarchy.
//!
//! These are heuristics over what `uiautomator dump` reports, in the spirit
//! of Android's Accessibility Scanner, not a certificate: the dump has no font
//! sizes, contrast or focus order.
use crate::{core::*, devices, toolchain as tc};
use serde_json::{Value, json};
use std::fs;

/// The density `wm density` reports; an override wins over the physical one.
pub fn parse_density(text: &str) -> Option<f64> {
    let value = |label: &str| {
        text.lines().find_map(|line| {
            line.trim()
                .strip_prefix(label)
                .and_then(|rest| rest.trim().parse::<f64>().ok())
                .filter(|v| *v > 0.0)
        })
    };
    value("Override density:").or_else(|| value("Physical density:"))
}

fn rectangle(bounds: &str) -> Option<(i64, i64, i64, i64)> {
    let pattern = regex::Regex::new(r"^\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]$").unwrap();
    let c = pattern.captures(bounds)?;
    let v: Vec<i64> = (1..=4).filter_map(|i| c[i].parse().ok()).collect();
    (v.len() == 4).then(|| (v[0], v[1], v[2], v[3]))
}

pub struct Limits {
    pub density: f64,
    pub min_target_dp: f64,
    pub min_text_height_dp: f64,
    pub package: String,
}

fn has_label(node: roxmltree::Node) -> bool {
    node.descendants()
        .filter(|n| n.has_tag_name("node"))
        .any(|n| {
            !n.attribute("text").unwrap_or("").trim().is_empty()
                || !n.attribute("content-desc").unwrap_or("").trim().is_empty()
        })
}

/// The findings for one hierarchy, in document order, and how many nodes
/// were audited.
pub fn audit(xml: &str, limits: &Limits) -> Result<(Vec<Value>, usize)> {
    let tree = roxmltree::Document::parse(xml)
        .map_err(|e| Error::new("invalid_hierarchy", e.to_string()))?;
    let scale = 160.0 / limits.density;
    let mut findings = Vec::new();
    let mut audited = 0;
    for node in tree.descendants().filter(|n| n.has_tag_name("node")) {
        let attribute = |name: &str| node.attribute(name).unwrap_or("");
        if !limits.package.is_empty() && attribute("package") != limits.package {
            continue;
        }
        let Some((left, top, right, bottom)) = rectangle(attribute("bounds")) else {
            continue;
        };
        if right <= left || bottom <= top {
            continue;
        }
        audited += 1;
        let (width, height) = ((right - left) as f64 * scale, (bottom - top) as f64 * scale);
        let class = attribute("class");
        let actionable = attribute("clickable") == "true"
            || attribute("long-clickable") == "true"
            || attribute("checkable") == "true";
        let finding = |rule: &str, severity: &str, message: String| {
            json!({
                "rule":rule,"severity":severity,"message":message,
                "class":class,"resource_id":attribute("resource-id"),"text":attribute("text"),
                "description":attribute("content-desc"),"package":attribute("package"),
                "bounds":{"left":left,"top":top,"right":right,"bottom":bottom},
                "width_dp":(width * 10.0).round() / 10.0,"height_dp":(height * 10.0).round() / 10.0,
            })
        };
        // An element cut off at the edge of a scrolling list shows only part
        // of its size; its size is not judged.
        let clipped = node
            .ancestors()
            .skip(1)
            .find(|n| n.attribute("scrollable") == Some("true"))
            .and_then(|n| rectangle(n.attribute("bounds").unwrap_or("")))
            .is_some_and(|(l, t, r, b)| left <= l || top <= t || right >= r || bottom >= b);
        let image = class.ends_with("ImageView") || class.ends_with("ImageButton");
        if actionable && !has_label(node) {
            findings.push(finding(
                "missing_label",
                "error",
                if image {
                    "A clickable image has no content description; TalkBack cannot name it".into()
                } else {
                    "A clickable element has no text or content description, nor has anything inside it".into()
                },
            ));
        }
        if actionable
            && !clipped
            && attribute("enabled") != "false"
            && (width < limits.min_target_dp || height < limits.min_target_dp)
        {
            findings.push(finding(
                "small_target",
                "warning",
                format!(
                    "The touch target is {:.0}x{:.0} dp, under {:.0}x{:.0} dp",
                    width, height, limits.min_target_dp, limits.min_target_dp
                ),
            ));
        }
        let leaf = !node.children().any(|n| n.has_tag_name("node"));
        if leaf
            && !clipped
            && !attribute("text").trim().is_empty()
            && !class.ends_with("EditText")
            && height < limits.min_text_height_dp
        {
            findings.push(finding(
                "small_text",
                "warning",
                format!(
                    "The text is {:.0} dp high, under {:.0} dp: it is likely too small to read",
                    height, limits.min_text_height_dp
                ),
            ));
        }
    }
    Ok((findings, audited))
}

pub fn accessibility(a: &Value) -> Result {
    let device = s(a, "device");
    let hierarchy = s(a, "hierarchy");
    if device.is_empty() == hierarchy.is_empty() {
        return Err(Error::new(
            "invalid_argument",
            "Give either device or hierarchy",
        ));
    }
    let (xml, density) = if device.is_empty() {
        let file = path(hierarchy, false, true)?;
        if fs::metadata(&file)?.len() > 16 * 1024 * 1024 {
            return Err(Error::new(
                "invalid_hierarchy",
                "The hierarchy is larger than 16 MiB",
            ));
        }
        let density = a["density"].as_f64().ok_or_else(|| {
            Error::new(
                "invalid_argument",
                "A saved hierarchy needs the screen's density in dpi",
            )
            .remedy("Read it with adb shell wm density, or audit the device directly")
        })?;
        (fs::read_to_string(file)?, density)
    } else {
        let value = devices::handle("android.hierarchy", &json!({"device":device}))?;
        let density = match a["density"].as_f64() {
            Some(value) => value,
            None => {
                let output = tc::run(
                    &devices::shell(device, &argv(&["wm", "density"]))?,
                    30,
                    None,
                    true,
                )?;
                parse_density(s(&output, "stdout")).ok_or_else(|| {
                    Error::new(
                        "display_unavailable",
                        "The device did not report its density",
                    )
                })?
            }
        };
        (s(&value, "xml").to_string(), density)
    };
    let limits = Limits {
        density,
        min_target_dp: a["min_target_dp"].as_f64().unwrap_or(48.0),
        min_text_height_dp: a["min_text_height_dp"].as_f64().unwrap_or(14.0),
        package: s(a, "bundle_id").to_string(),
    };
    let (findings, audited) = audit(&xml, &limits)?;
    let count = |rule: &str| findings.iter().filter(|f| f["rule"] == rule).count();
    let counts = json!({"missing_label":count("missing_label"),"small_target":count("small_target"),"small_text":count("small_text")});
    let value = json!({
        "passed":findings.is_empty(),"device":device,"density":density,"audited_nodes":audited,
        "min_target_dp":limits.min_target_dp,"min_text_height_dp":limits.min_text_height_dp,
        "counts":counts,"findings":findings,
        "note":"Heuristics over the UI hierarchy: no contrast, font size or focus order. Text size is estimated from element height.",
    });
    if !s(a, "report").is_empty() {
        atomic_json(&output_path(s(a, "report"))?, &value)?;
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    const SCREEN: &str = r#"<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout" package="dev.example" clickable="false" bounds="[0,0][1080,2400]">
    <node index="0" text="" resource-id="dev.example:id/share" class="android.widget.ImageButton" package="dev.example" content-desc="" clickable="true" enabled="true" bounds="[0,0][168,168]"/>
    <node index="1" text="" resource-id="dev.example:id/close" class="android.widget.ImageView" package="dev.example" content-desc="Close" clickable="true" enabled="true" bounds="[900,0][990,90]"/>
    <node index="2" text="" class="android.widget.LinearLayout" package="dev.example" clickable="true" enabled="true" bounds="[0,300][1080,500]">
      <node index="0" text="Settings" class="android.widget.TextView" package="dev.example" clickable="false" bounds="[40,340][400,400]"/>
    </node>
    <node index="3" text="v1.0 build 7" class="android.widget.TextView" package="dev.example" clickable="false" bounds="[40,2300][400,2330]"/>
    <node index="4" text="" class="android.view.View" package="dev.example" clickable="true" enabled="false" bounds="[0,600][60,660]"/>
    <node index="5" text="" class="android.widget.EditText" package="dev.example" clickable="true" enabled="true" bounds="[0,700][1080,720]"/>
    <node index="6" text="" class="androidx.recyclerview.widget.RecyclerView" package="dev.example" scrollable="true" bounds="[0,800][1080,2000]">
      <node index="0" text="" class="android.widget.LinearLayout" package="dev.example" clickable="true" enabled="true" bounds="[0,1970][1080,2000]">
        <node index="0" text="Storage" resource-id="dev.example:id/cut" class="android.widget.TextView" package="dev.example" bounds="[40,1975][400,2000]"/>
      </node>
    </node>
  </node>
  <node index="1" text="12:00" class="android.widget.TextView" package="com.android.systemui" bounds="[0,0][100,20]"/>
</hierarchy>"#;

    fn limits(package: &str) -> Limits {
        Limits {
            density: 420.0,
            min_target_dp: 48.0,
            min_text_height_dp: 14.0,
            package: package.into(),
        }
    }
    fn rules(findings: &[Value]) -> Vec<(String, String)> {
        findings
            .iter()
            .map(|f| (s(f, "rule").to_string(), s(f, "resource_id").to_string()))
            .collect()
    }

    #[test]
    fn labels_targets_and_text_are_audited() {
        let (findings, audited) = audit(SCREEN, &limits("dev.example")).unwrap();
        assert_eq!(audited, 11);
        // The last row of the list is cut off by its edge: not judged.
        assert!(!findings.iter().any(|f| f["text"] == "Storage" || f["bounds"]["top"] == 1970));
        let found = rules(&findings);
        // 168 px at 420 dpi is 64 dp: big enough, but unnamed.
        assert!(found.contains(&("missing_label".into(), "dev.example:id/share".into())));
        assert!(!found.contains(&("small_target".into(), "dev.example:id/share".into())));
        // 90 px is 34 dp: named, but small.
        assert!(found.contains(&("small_target".into(), "dev.example:id/close".into())));
        assert!(!found.contains(&("missing_label".into(), "dev.example:id/close".into())));
        // The row is named by its text child; the version line is 11 dp high.
        let text: Vec<&Value> = findings
            .iter()
            .filter(|f| f["rule"] == "small_text")
            .collect();
        assert_eq!(text.len(), 1);
        assert_eq!(text[0]["text"], "v1.0 build 7");
        assert_eq!(text[0]["height_dp"], 11.4);
        // A disabled control is still unnamed, but its size is not a target.
        let disabled: Vec<&Value> = findings
            .iter()
            .filter(|f| f["class"] == "android.view.View")
            .collect();
        assert_eq!(disabled.len(), 1);
        assert_eq!(disabled[0]["rule"], "missing_label");
        // The status bar belongs to another package.
        assert!(findings.iter().all(|f| f["package"] == "dev.example"));
        let (everything, _) = audit(SCREEN, &limits("")).unwrap();
        assert!(everything.iter().any(|f| f["text"] == "12:00"));
        assert!(audit("<hierarchy", &limits("")).is_err());
    }

    #[test]
    fn density_prefers_the_override() {
        assert_eq!(parse_density("Physical density: 420\n"), Some(420.0));
        assert_eq!(
            parse_density("Physical density: 420\nOverride density: 560\n"),
            Some(560.0)
        );
        assert_eq!(parse_density("nothing"), None);
    }

    #[test]
    fn a_saved_hierarchy_needs_a_density_and_writes_a_report() {
        let root = tempfile::tempdir().unwrap();
        let file = root.path().join("screen.xml");
        fs::write(&file, SCREEN).unwrap();
        let missing = accessibility(&json!({"hierarchy":file})).unwrap_err();
        assert_eq!(missing.code, "invalid_argument");
        assert!(accessibility(&json!({})).is_err());
        let report = root.path().join("audit.json");
        let value = accessibility(
            &json!({"hierarchy":file,"density":420,"bundle_id":"dev.example","report":report}),
        )
        .unwrap();
        assert_eq!(value["passed"], false);
        assert_eq!(value["counts"]["small_text"], 1);
        assert_eq!(read_json(&report, None).unwrap()["counts"], value["counts"]);
    }
}
