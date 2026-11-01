//! Simulated sensors of the Android Emulator through its console: any sensor
//! value, the device turned by gravity, a shake, proximity, light, a touch of
//! the fingerprint sensor and the posture of a foldable.
use crate::{core::*, devices, toolchain as tc};
use serde_json::{Value, json};
use std::{thread::sleep, time::Duration};

const GRAVITY: f64 = 9.80665;

fn console(device: &str, command: &[&str]) -> Result<String> {
    devices::console(device, command)
}

fn shell(device: &str, args: &[&str]) -> Result<String> {
    let result = tc::run(&devices::shell(device, &argv(args))?, 30, None, true)?;
    Ok(s(&result, "stdout").replace('\r', ""))
}

/// `name: enabled.` lines of `sensor status`.
fn statuses(output: &str) -> Vec<(String, bool)> {
    output
        .lines()
        .filter_map(|l| l.trim().strip_suffix('.')?.split_once(": "))
        .map(|(name, state)| (name.trim().to_string(), state.trim() == "enabled"))
        .collect()
}

/// The numbers of `acceleration = 0:9.77631:0.812349`.
fn values(output: &str) -> Vec<f64> {
    output
        .lines()
        .find_map(|l| l.split_once(" = "))
        .map(|(_, v)| v.split(':').filter_map(|x| x.trim().parse().ok()).collect())
        .unwrap_or_default()
}

fn get(device: &str, sensor: &str) -> Result<Vec<f64>> {
    Ok(values(&console(device, &["sensor", "get", sensor])?))
}

fn set(device: &str, sensor: &str, numbers: &[f64]) -> Result<Vec<f64>> {
    let joined = numbers
        .iter()
        .map(|v| format!("{v}"))
        .collect::<Vec<_>>()
        .join(":");
    console(device, &["sensor", "set", sensor, &joined])?;
    get(device, sensor)
}

/// The display rotation in quarter turns, from `dumpsys window displays`.
fn rotation(output: &str) -> Option<u64> {
    let pattern = regex::Regex::new(r"\bmRotation=([0-3])\b").unwrap();
    pattern.captures(output)?[1].parse().ok()
}

fn current_rotation(device: &str) -> Result<Option<u64>> {
    Ok(rotation(&shell(
        device,
        &["dumpsys", "window", "displays"],
    )?))
}

/// Gravity in the device's frame when it is held in this orientation, and the
/// rotation Android then gives the display.
fn gravity(orientation: &str) -> ([f64; 3], u64) {
    match orientation {
        "landscape" => ([GRAVITY, 0.0, 0.0], 1),
        "reverse-portrait" => ([0.0, -GRAVITY, 0.0], 2),
        "reverse-landscape" => ([-GRAVITY, 0.0, 0.0], 3),
        _ => ([0.0, GRAVITY, 0.0], 0),
    }
}

/// The console's posture numbers, as the emulator's foldable definitions use them.
fn posture_number(posture: &str) -> &'static str {
    match posture {
        "closed" => "1",
        "half-open" => "2",
        _ => "3",
    }
}

/// The name of the state `cmd device_state print-state` prints as a number.
fn device_state(output: &str) -> Value {
    match output.trim() {
        "1" => json!("closed"),
        "2" => json!("half-open"),
        "3" => json!("open"),
        "" => Value::Null,
        other => json!(other),
    }
}

fn pause(milliseconds: u64) {
    sleep(Duration::from_millis(milliseconds));
}

pub fn sensors(a: &Value) -> Result {
    let device = s(a, "device");
    if !device.starts_with("emulator-") {
        return Err(
            Error::new("not_emulator", "Simulated sensors need an emulator serial")
                .remedy("On a phone, move the phone"),
        );
    }
    let action = s(a, "action");
    match action {
        "status" => {
            let mut sensors = serde_json::Map::new();
            for (name, enabled) in statuses(&console(device, &["sensor", "status"])?) {
                let read = if enabled {
                    json!(get(device, &name)?)
                } else {
                    Value::Null
                };
                sensors.insert(name, json!({"enabled":enabled,"values":read}));
            }
            Ok(json!({"device":device,"action":action,"sensors":sensors}))
        }
        "set" => {
            let sensor = identifier(s(a, "sensor"), "sensor name", Some("[a-z][a-z0-9-]*"))?;
            let numbers: Vec<f64> = a["values"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_f64)
                .collect();
            if numbers.is_empty() {
                return Err(
                    Error::new("invalid_argument", "set needs sensor and values")
                        .remedy("Use action status to list the sensors and their values"),
                );
            }
            let read = set(device, sensor, &numbers)?;
            Ok(json!({"device":device,"action":action,"sensor":sensor,"values":read}))
        }
        "orientation" => {
            let wanted = s(a, "orientation");
            let (vector, expected) = gravity(wanted);
            let auto = shell(
                device,
                &["settings", "get", "system", "accelerometer_rotation"],
            )?;
            let read = set(device, "acceleration", &vector)?;
            // Android turns the display a moment after gravity changes.
            let mut now = current_rotation(device)?;
            for _ in 0..12 {
                if now == Some(expected) {
                    break;
                }
                pause(250);
                now = current_rotation(device)?;
            }
            let mut result = json!({"device":device,"action":action,"orientation":wanted,
                "acceleration":read,"rotation":now.map(|r| r*90),"applied":now==Some(expected),
                "auto_rotate":auto.trim()=="1"});
            if now != Some(expected) {
                result["note"] = json!(if auto.trim() != "1" {
                    "Auto-rotate is off; android.display orientation auto turns it on"
                } else {
                    "Android kept the rotation: the app or this device does not turn to it, as phones do for reverse-portrait"
                });
            }
            Ok(result)
        }
        "shake" => {
            let before = get(device, "acceleration")?;
            for step in 0..10 {
                let x = if step % 2 == 0 { 2.0 } else { -2.0 } * GRAVITY;
                console(
                    device,
                    &["sensor", "set", "acceleration", &format!("{x}:{GRAVITY}:0")],
                )?;
                pause(60);
            }
            let restore = if before.len() == 3 {
                before.clone()
            } else {
                vec![0.0, GRAVITY, 0.0]
            };
            let read = set(device, "acceleration", &restore)?;
            Ok(json!({"device":device,"action":action,"shakes":5,"acceleration":read}))
        }
        "proximity" => {
            // The emulator's proximity sensor reads 0 when something is near.
            let read = set(device, "proximity", &[if b(a, "near") { 0.0 } else { 1.0 }])?;
            Ok(
                json!({"device":device,"action":action,"near":read.first()==Some(&0.0),"values":read}),
            )
        }
        "light" => {
            let lux = a["lux"].as_f64().unwrap_or(0.0);
            let read = set(device, "light", &[lux])?;
            Ok(json!({"device":device,"action":action,"lux":read.first()}))
        }
        "fingerprint" => {
            let finger = n(a, "finger_id").to_string();
            console(device, &["finger", "touch", &finger])?;
            pause(300);
            console(device, &["finger", "remove"])?;
            Ok(json!({"device":device,"action":action,"finger_id":n(a,"finger_id")}))
        }
        _ => {
            let posture = s(a, "posture");
            let status = statuses(&console(device, &["sensor", "status"])?);
            if !status
                .iter()
                .any(|(name, on)| name == "hinge-angle0" && *on)
            {
                return Err(Error::new(
                    "not_foldable",
                    "This emulator has no hinge, so it has no posture",
                )
                .remedy("Create an AVD with a foldable profile such as pixel-11-pro-fold"));
            }
            match posture {
                "closed" => console(device, &["fold"])?,
                "open" => console(device, &["unfold"])?,
                _ => console(device, &["posture", posture_number(posture)])?,
            };
            pause(500);
            let angle = get(device, "hinge-angle0")?;
            // Android's device state is what apps see through Jetpack WindowManager.
            let state = shell(device, &["cmd", "device_state", "print-state"])?;
            Ok(json!({"device":device,"action":action,"posture":posture,
                "hinge_angle":angle.first(),"device_state":device_state(&state)}))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn console_answers_are_read() {
        let status = "acceleration: enabled.\nhinge-angle0: disabled.\nlight: enabled.\n";
        assert_eq!(
            statuses(status),
            [
                ("acceleration".to_string(), true),
                ("hinge-angle0".to_string(), false),
                ("light".to_string(), true)
            ]
        );
        assert_eq!(
            values("acceleration = 0:9.77631:0.812349\n"),
            [0.0, 9.77631, 0.812349]
        );
        assert!(values("KO: bad").is_empty());
        let window = "overrideConfig={ mRotation=ROTATION_0}\n    mRotation=1 mDeferredRotationPauseCount=0\n";
        assert_eq!(rotation(window), Some(1));
        assert_eq!(gravity("landscape").1, 1);
        assert_eq!(gravity("portrait").0, [0.0, GRAVITY, 0.0]);
        assert_eq!(posture_number("half-open"), "2");
        assert_eq!(device_state("2\n"), "half-open");
    }
}
