//! Android device state for tests: display overrides and notifications.
use crate::{core::*, devices, toolchain as tc};
use serde_json::{Value, json};

fn run(device: &str, args: &[&str]) -> Result {
    tc::run(&devices::shell(device, &argv(args))?, 30, None, true)
}

fn stdout(device: &str, args: &[&str]) -> Result<String> {
    Ok(s(&run(device, args)?, "stdout").replace('\r', ""))
}

const ORIENTATIONS: [&str; 4] = [
    "portrait",
    "landscape",
    "reverse-portrait",
    "reverse-landscape",
];

/// `Physical size: 1080x2400` and `Override size: 720x1600` from `wm size`.
fn sizes(output: &str) -> (Value, Value) {
    let pick = |prefix: &str| {
        output
            .lines()
            .find_map(|l| l.trim().strip_prefix(prefix))
            .and_then(|v| v.trim().split_once('x'))
            .and_then(|(w, h)| Some(json!([w.parse::<u64>().ok()?, h.parse::<u64>().ok()?])))
            .unwrap_or(Value::Null)
    };
    (pick("Physical size:"), pick("Override size:"))
}

/// `Physical density: 420` and `Override density: 320` from `wm density`.
fn densities(output: &str) -> (Value, Value) {
    let pick = |prefix: &str| {
        output
            .lines()
            .find_map(|l| l.trim().strip_prefix(prefix))
            .and_then(|v| v.trim().parse::<u64>().ok())
            .map(Value::from)
            .unwrap_or(Value::Null)
    };
    (pick("Physical density:"), pick("Override density:"))
}

/// The orientation named by Android's rotation settings.
fn orientation(accelerometer: &str, rotation: &str) -> &'static str {
    if accelerometer.trim() == "1" {
        return "auto";
    }
    rotation
        .trim()
        .parse::<usize>()
        .ok()
        .and_then(|r| ORIENTATIONS.get(r).copied())
        .unwrap_or("portrait")
}

fn display_state(device: &str) -> Result {
    let (physical_size, override_size) = sizes(&stdout(device, &["wm", "size"])?);
    let (physical_density, override_density) = densities(&stdout(device, &["wm", "density"])?);
    let accelerometer = stdout(
        device,
        &["settings", "get", "system", "accelerometer_rotation"],
    )?;
    let rotation = stdout(device, &["settings", "get", "system", "user_rotation"])?;
    Ok(json!({
        "device": device,
        "size": {"physical": physical_size, "override": override_size},
        "density": {"physical": physical_density, "override": override_density},
        "orientation": orientation(&accelerometer, &rotation),
    }))
}

/// Locks the orientation, overrides the display size or density, or resets
/// them, then reads all three back.
pub fn display(a: &Value) -> Result {
    let device = identifier(s(a, "device"), "device ID", None)?;
    let width = a.get("width").and_then(Value::as_u64);
    let height = a.get("height").and_then(Value::as_u64);
    if width.is_some() != height.is_some() {
        return Err(Error::new(
            "invalid_argument",
            "Pass width and height together",
        ));
    }
    let mut changed = Vec::new();
    if b(a, "reset") {
        run(device, &["wm", "size", "reset"])?;
        run(device, &["wm", "density", "reset"])?;
        run(
            device,
            &["settings", "put", "system", "accelerometer_rotation", "1"],
        )?;
        changed.push("reset");
    }
    if let (Some(w), Some(h)) = (width, height) {
        run(device, &["wm", "size", &format!("{w}x{h}")])?;
        changed.push("size");
    }
    if let Some(density) = a.get("density").and_then(Value::as_u64) {
        run(device, &["wm", "density", &density.to_string()])?;
        changed.push("density");
    }
    if let Some(wanted) = a.get("orientation").and_then(Value::as_str) {
        if wanted == "auto" {
            run(
                device,
                &["settings", "put", "system", "accelerometer_rotation", "1"],
            )?;
        } else {
            let rotation = ORIENTATIONS
                .iter()
                .position(|o| *o == wanted)
                .ok_or_else(|| Error::new("invalid_argument", wanted))?;
            run(
                device,
                &["settings", "put", "system", "accelerometer_rotation", "0"],
            )?;
            run(
                device,
                &[
                    "settings",
                    "put",
                    "system",
                    "user_rotation",
                    &rotation.to_string(),
                ],
            )?;
        }
        changed.push("orientation");
    }
    let mut value = display_state(device)?;
    value["changed"] = json!(changed);
    let mut differences = Vec::new();
    if let (Some(w), Some(h)) = (width, height) {
        if value["size"]["override"] != json!([w, h]) {
            differences.push("size");
        }
    }
    if let Some(density) = a.get("density").and_then(Value::as_u64) {
        if value["density"]["override"] != json!(density) {
            differences.push("density");
        }
    }
    if let Some(wanted) = a.get("orientation").and_then(Value::as_str) {
        if value["orientation"] != wanted {
            differences.push("orientation");
        }
    }
    if !differences.is_empty() {
        return Err(Error::new(
            "setting_not_applied",
            format!("Android did not apply: {}", differences.join(", ")),
        )
        .details(value));
    }
    Ok(value)
}

/// Notifications from `dumpsys notification --noredact`: one record per
/// `NotificationRecord(` line of its notification list, with the title and
/// texts from the record's extras.
fn notifications(output: &str) -> Vec<Value> {
    let header = regex::Regex::new(
        r"^NotificationRecord\(0x[0-9a-f]+: pkg=(\S+) user=UserHandle\{(-?\d+)\} id=(-?\d+) tag=(.*?) importance=(-?\d+) key=(.+?): Notification\((.*)\)\)$",
    )
    .unwrap();
    let extra = regex::Regex::new(r"^android\.(title|text|subText|bigText)=\w+ \((.*)\)$").unwrap();
    let mut found: Vec<Value> = Vec::new();
    let mut listing = false;
    for line in output.lines() {
        let trimmed = line.trim();
        if line.starts_with("  ") && !line.starts_with("   ") {
            listing = trimmed == "Notification List:";
            continue;
        }
        if !listing {
            continue;
        }
        if let Some(c) = header.captures(trimmed) {
            let notification = &c[7];
            let field = |name: &str| {
                notification
                    .split(' ')
                    .find_map(|part| part.strip_prefix(name))
                    .unwrap_or("")
                    .to_string()
            };
            let flags = field("flags=");
            let flags: Vec<_> = flags
                .split('|')
                .filter(|f| !f.is_empty() && *f != "0")
                .collect();
            found.push(json!({
                "key": &c[6],
                "package": &c[1],
                "user": c[2].parse::<i64>().unwrap_or(0),
                "id": c[3].parse::<i64>().unwrap_or(0),
                "tag": if &c[4] == "null" { Value::Null } else { json!(&c[4]) },
                "importance": c[5].parse::<i64>().unwrap_or(0),
                "channel": field("channel="),
                "flags": flags,
                "ongoing": flags.contains(&"ONGOING_EVENT") || flags.contains(&"FOREGROUND_SERVICE"),
            }));
        } else if let (Some(c), Some(last)) = (extra.captures(trimmed), found.last_mut()) {
            let key = match &c[1] {
                "subText" => "sub_text",
                "bigText" => "big_text",
                other => other,
            };
            if last.get(key).is_none() {
                last[key] = json!(&c[2]);
            }
        }
    }
    found
}

/// Lists notifications, opens or closes the notification shade, or snoozes
/// one notification. adb cannot cancel another app's notifications.
pub fn notification(a: &Value) -> Result {
    let device = identifier(s(a, "device"), "device ID", None)?;
    let id = s(a, "bundle_id");
    if !id.is_empty() {
        identifier(
            id,
            "application ID",
            Some(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+"),
        )?;
    }
    let action = s(a, "action");
    match action {
        "open" => {
            run(device, &["cmd", "statusbar", "expand-notifications"])?;
        }
        "close" => {
            run(device, &["cmd", "statusbar", "collapse"])?;
        }
        "snooze" => {
            let key = identifier(
                s(a, "key"),
                "notification key",
                Some(r"-?[0-9]+\|[^\x00-\x1f]{1,400}"),
            )?;
            let millis = (n(a, "seconds") * 1000).to_string();
            let output = stdout(
                device,
                &["cmd", "notification", "snooze", "--for", &millis, key],
            )?;
            if !output.contains("snoozing") {
                return Err(Error::new(
                    "notification_not_snoozed",
                    format!("Android did not snooze {key}"),
                )
                .details(json!({"output":output})));
            }
        }
        _ => (),
    }
    let mut list = notifications(&stdout(device, &["dumpsys", "notification", "--noredact"])?);
    if !id.is_empty() {
        list.retain(|v| v["package"] == id);
    }
    if action == "snooze" && list.iter().any(|v| v["key"] == s(a, "key")) {
        return Err(Error::new(
            "notification_not_snoozed",
            "The notification is still shown",
        ));
    }
    Ok(json!({"device":device,"action":action,"notifications":list}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn display_size_density_and_orientation_are_read() {
        assert_eq!(
            sizes("Physical size: 1080x2400\nOverride size: 720x1600\n"),
            (json!([1080, 2400]), json!([720, 1600]))
        );
        assert_eq!(
            sizes("Physical size: 1080x2400"),
            (json!([1080, 2400]), Value::Null)
        );
        assert_eq!(
            densities("Physical density: 420\r\nOverride density: 320"),
            (json!(420), json!(320))
        );
        assert_eq!(orientation("1\n", "3"), "auto");
        assert_eq!(orientation("0", "1\n"), "landscape");
        assert_eq!(orientation("0", "3"), "reverse-landscape");
        assert_eq!(orientation("0", "null"), "portrait");
    }

    #[test]
    fn notifications_are_read_from_the_notification_list_only() {
        let dump = "Current Notification Manager state:\n  Notification List:\n    NotificationRecord(0x01d08d09: pkg=com.android.shell user=UserHandle{0} id=2020 tag=tomacco-tag importance=3 key=0|com.android.shell|2020|tomacco-tag|2000: Notification(channel=shell_cmd shortcut=null contentView=null vibrate=null sound=null defaults=0 flags=0 color=0x00000000 vis=PRIVATE))\n      uid=2000 userId=0\n      notification=\n        extras={\n            android.title=String (Hello (there))\n            android.text=String (Body text)\n        }\n    NotificationRecord(0x02a82fca: pkg=android user=UserHandle{-1} id=19 tag=null importance=2 key=-1|android|19|null|1000: Notification(channel=DEVELOPER shortcut=null contentView=null vibrate=null sound=null tick defaults=0 flags=ONGOING_EVENT|CAN_COLORIZE|SILENT color=0x00000000 vis=PUBLIC))\n  Snoozed notifications:\n    NotificationRecord(0x0: pkg=x user=UserHandle{0} id=1 tag=null importance=1 key=0|x|1|null|1: Notification(channel=a flags=0))\n";
        let list = notifications(dump);
        assert_eq!(list.len(), 2);
        assert_eq!(list[0]["key"], "0|com.android.shell|2020|tomacco-tag|2000");
        assert_eq!(list[0]["tag"], "tomacco-tag");
        assert_eq!(list[0]["title"], "Hello (there)");
        assert_eq!(list[0]["text"], "Body text");
        assert_eq!(list[0]["channel"], "shell_cmd");
        assert_eq!(list[0]["ongoing"], false);
        assert_eq!(list[1]["package"], "android");
        assert_eq!(list[1]["user"], -1);
        assert_eq!(list[1]["tag"], Value::Null);
        assert_eq!(list[1]["ongoing"], true);
        assert_eq!(
            list[1]["flags"],
            json!(["ONGOING_EVENT", "CAN_COLORIZE", "SILENT"])
        );
    }
}
