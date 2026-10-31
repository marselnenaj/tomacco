//! Android device state for tests: display overrides.
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
}
