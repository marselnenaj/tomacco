//! Android device state for tests: display overrides, notifications, the
//! private files of debuggable apps, and one action on every device.
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
pub(crate) fn sizes(output: &str) -> (Value, Value) {
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
pub(crate) fn densities(output: &str) -> (Value, Value) {
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

/// `dumpsys battery`: level in percent, charge state, health, temperature and
/// which charger is connected. Null when Android reports no battery level.
fn battery(output: &str) -> Value {
    let fields: std::collections::BTreeMap<&str, &str> = output
        .lines()
        .filter_map(|l| l.trim().split_once(':'))
        .map(|(k, v)| (k.trim(), v.trim()))
        .collect();
    let number = |key: &str| fields.get(key).and_then(|v| v.parse::<i64>().ok());
    let Some(level) = number("level") else {
        return Value::Null;
    };
    let scale = number("scale").filter(|v| *v > 0).unwrap_or(100);
    let status = match number("status") {
        Some(2) => "charging",
        Some(3) => "discharging",
        Some(4) => "not-charging",
        Some(5) => "full",
        _ => "unknown",
    };
    let health = match number("health") {
        Some(2) => "good",
        Some(3) => "overheat",
        Some(4) => "dead",
        Some(5) => "over-voltage",
        Some(6) => "failure",
        Some(7) => "cold",
        _ => "unknown",
    };
    let plugged: Vec<&str> = [
        ("AC powered", "ac"),
        ("USB powered", "usb"),
        ("Wireless powered", "wireless"),
        ("Dock powered", "dock"),
    ]
    .iter()
    .filter(|(key, _)| fields.get(key) == Some(&"true"))
    .map(|(_, name)| *name)
    .collect();
    json!({
        "level": (level * 100 + scale / 2) / scale,
        "status": status,
        "health": health,
        "temperature_celsius": number("temperature").map(|t| t as f64 / 10.0),
        "plugged": plugged,
        "present": fields.get("present").map(|v| *v == "true"),
    })
}

/// The `df -k` line of one mount: total, used and free bytes.
fn storage(output: &str) -> Value {
    output
        .lines()
        .skip(1)
        .filter_map(|line| {
            let fields: Vec<_> = line.split_whitespace().collect();
            let numbers: Vec<u64> = fields
                .get(1..4)?
                .iter()
                .map(|v| v.parse().ok())
                .collect::<Option<_>>()?;
            Some(json!({
                "path": fields.last().copied().unwrap_or(""),
                "total_bytes": numbers[0] * 1024,
                "used_bytes": numbers[1] * 1024,
                "free_bytes": numbers[2] * 1024,
            }))
        })
        .last()
        .unwrap_or(Value::Null)
}

/// Seconds since boot, the first number of `/proc/uptime`.
fn uptime(output: &str) -> Value {
    output
        .split_whitespace()
        .next()
        .and_then(|v| v.parse::<f64>().ok())
        .map(|v| json!(v.floor() as u64))
        .unwrap_or(Value::Null)
}

/// What a device's state is at a glance, from its properties, `wm size`,
/// `wm density`, `dumpsys battery`, `df -k /data` and `/proc/uptime`. A part
/// the device does not report is null.
pub fn health(device: &str, properties: &Value, size: &str, density: &str) -> Value {
    let read = |args: &[&str]| stdout(device, args).unwrap_or_default();
    let property = |key: &str| {
        properties[key]
            .as_str()
            .filter(|v| !v.is_empty())
            .map(Value::from)
            .unwrap_or(Value::Null)
    };
    let (physical_size, override_size) = sizes(size);
    let (physical_density, override_density) = densities(density);
    let shown_size = if override_size.is_null() {
        physical_size.clone()
    } else {
        override_size.clone()
    };
    let shown_density = if override_density.is_null() {
        physical_density.clone()
    } else {
        override_density.clone()
    };
    let dp = match (shown_size.as_array(), shown_density.as_u64()) {
        (Some(pixels), Some(dpi)) if dpi > 0 => json!(
            pixels
                .iter()
                .map(|p| (p.as_u64().unwrap_or(0) as f64 * 160.0 / dpi as f64).round() as u64)
                .collect::<Vec<_>>()
        ),
        _ => Value::Null,
    };
    json!({
        "android": {
            "version": property("ro.build.version.release"),
            "sdk": properties["ro.build.version.sdk"].as_str().and_then(|v| v.parse::<u64>().ok()),
            "security_patch": property("ro.build.version.security_patch"),
            "build": property("ro.build.display.id"),
        },
        "model": property("ro.product.model"),
        "manufacturer": property("ro.product.manufacturer"),
        "battery": battery(&read(&["dumpsys", "battery"])),
        "storage": storage(&read(&["df", "-k", "/data"])),
        "screen": {
            "size": shown_size,
            "density": shown_density,
            "size_dp": dp,
            "physical_size": physical_size,
            "physical_density": physical_density,
        },
        "uptime_seconds": uptime(&read(&["cat", "/proc/uptime"])),
    })
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

/// A path inside an app's data directory, relative to it.
fn app_path(value: &str) -> Result<&str> {
    let value = if value.is_empty() { "." } else { value };
    if value.starts_with('/') || value.split('/').any(|part| part == "..") {
        return Err(Error::new(
            "invalid_path",
            "Give a path inside the app's data directory, relative to it",
        ));
    }
    identifier(value, "app path", Some(r"[A-Za-z0-9_.@+=,/ -]{1,400}"))
}

/// `SIZE MTIME PATH` lines of `stat -c '%s %Y %n'`.
fn file_list(output: &str) -> Vec<Value> {
    output
        .lines()
        .filter_map(|line| {
            let mut parts = line.splitn(3, ' ');
            let size = parts.next()?.parse::<u64>().ok()?;
            let modified = parts.next()?.parse::<u64>().ok()?;
            let name = parts.next()?;
            Some(json!({"path":name.strip_prefix("./").unwrap_or(name),"size":size,"modified":modified}))
        })
        .collect()
}

/// Lists or pulls files from a debuggable app's private data directory
/// through run-as, which Android allows only for debuggable builds.
pub fn app_files(a: &Value) -> Result {
    let device = identifier(s(a, "device"), "device ID", None)?;
    let id = identifier(
        s(a, "bundle_id"),
        "application ID",
        Some(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+"),
    )?;
    let remote = app_path(s(a, "path"))?;
    let output = if s(a, "action") == "pull" {
        let target = output_path(s(a, "output"))?;
        if target.extension().is_none_or(|x| x != "tar") {
            return Err(Error::new(
                "invalid_path",
                "The pulled files are written as one .tar archive; name the output FILE.tar",
            ));
        }
        Some(target)
    } else {
        None
    };
    // run-as is Android's own gate: debuggable builds of installed apps only.
    run(device, &["run-as", id, "true"]).map_err(|e| {
        Error::new(
            "app_not_debuggable",
            format!("Android refused run-as for {id}"),
        )
        .remedy("Install a debuggable build of the app, such as a debug build from project.run")
        .details(e.value())
    })?;
    run(device, &["run-as", id, "ls", "-d", remote]).map_err(|e| {
        Error::new(
            "path_not_found",
            format!("{remote} does not exist in the data directory of {id}"),
        )
        .details(e.value())
    })?;
    let Some(target) = output else {
        let listing = stdout(
            device,
            &[
                "run-as", id, "find", remote, "-type", "f", "-exec", "stat", "-c", "%s %Y %n",
                "{}", "+",
            ],
        )?;
        return Ok(
            json!({"device":device,"bundle_id":id,"path":remote,"files":file_list(&listing)}),
        );
    };
    std::fs::create_dir_all(target.parent().unwrap())?;
    // exec-out passes stderr into the archive and loses the exit status, so
    // the path was checked above and tar's messages are dropped.
    let mut command = devices::adb(device)?;
    command.extend([
        "exec-out".into(),
        format!(
            "run-as {} tar -cf - {} 2>/dev/null",
            shell_quote(id),
            shell_quote(remote)
        ),
    ]);
    devices::submit(
        &format!("Pull files of {id}"),
        json!([{"argv":command,"output":target,"minimum_bytes":1024}]),
        600,
        json!({}),
    )
}

/// Operations `android.each` may run on every device. Each takes `device`.
pub const EACH_OPERATIONS: [&str; 9] = [
    "device.install",
    "device.app",
    "android.launch",
    "android.permission",
    "android.settings",
    "android.display",
    "android.deep_link",
    "android.input",
    "android.notifications",
];

/// One step's arguments for one device.
fn for_device(operation: &str, arguments: &Value, device: &str) -> Value {
    let mut value = if arguments.is_object() {
        arguments.clone()
    } else {
        json!({})
    };
    value["device"] = json!(device);
    if operation.starts_with("device.") {
        value["platform"] = json!("android");
    }
    value
}

/// Runs the same steps on several Android devices as one job, all devices at
/// once, and reports every device's result in report.json.
pub fn each(a: &Value) -> Result {
    let steps = a["steps"].as_array().cloned().unwrap_or_default();
    let mut devices = strings(&a["devices"]);
    if devices.is_empty() {
        let listing = devices::listing("android")?;
        devices = listing["devices"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|d| s(d, "state") == "device")
            .map(|d| s(d, "id").to_string())
            .collect();
    }
    devices.sort();
    devices.dedup();
    if devices.is_empty() {
        return Err(Error::new("no_devices", "No Android device is connected")
            .remedy("Start an emulator with android.avd_start or connect a phone, then check device.list"));
    }
    for device in &devices {
        identifier(device, "Android serial", None)?;
    }
    for step in &steps {
        let operation = s(step, "operation");
        if !EACH_OPERATIONS.contains(&operation) {
            return Err(Error::new(
                "invalid_argument",
                format!("{operation} cannot run on every device"),
            ));
        }
        for key in ["device", "platform"] {
            if step["arguments"].get(key).is_some() {
                return Err(Error::new(
                    "invalid_argument",
                    format!("Leave {key} out of the step arguments; android.each sets it"),
                ));
            }
        }
        // Refuse a malformed step before anything runs on a device.
        crate::api::arguments(
            operation,
            &for_device(operation, &step["arguments"], &devices[0]),
        )?;
    }
    let directory = home()?
        .join("each")
        .join(uuid::Uuid::new_v4().simple().to_string());
    private_dir(&directory)?;
    let request = directory.join("request.json");
    atomic_json(
        &request,
        &json!({"devices":devices,"steps":steps,"report":directory.join("report.json")}),
    )?;
    crate::jobs::submit(
        &match devices.len() {
            1 => "Run on 1 Android device".to_string(),
            count => format!("Run on {count} Android devices"),
        },
        json!({"steps":[{"argv":[own_executable()?,"_android_each",request]}],"cwd":directory,"env":tc::environment()?,"timeout":n(a,"timeout"),"always_artifacts":["report.json"]}),
    )
}

/// One device's steps, in order, until one fails. A step that returns a job
/// is followed to its end.
fn each_device(device: &str, steps: &[Value]) -> Value {
    let mut results = Vec::new();
    let mut ok = true;
    for step in steps {
        let operation = s(step, "operation");
        let arguments = for_device(operation, &step["arguments"], device);
        let mut entry = json!({"operation":operation});
        match crate::api::dispatch(operation, &arguments) {
            Ok(value) if value.get("plan").is_some() && value["id"].is_string() => {
                let mut job = value;
                while !crate::jobs::TERMINAL.contains(&s(&job, "state")) {
                    match crate::jobs::wait(s(&job, "id"), 60) {
                        Ok(next) => job = next,
                        Err(error) => {
                            job["state"] = json!("failed");
                            entry["error"] = error.value();
                        }
                    }
                }
                entry["job_id"] = job["id"].clone();
                entry["state"] = job["state"].clone();
                entry["result"] = job["result"].clone();
                entry["ok"] = json!(s(&job, "state") == "succeeded");
            }
            Ok(value) => {
                entry["ok"] = json!(true);
                entry["result"] = value;
            }
            Err(error) => {
                entry["ok"] = json!(false);
                entry["error"] = error.value();
            }
        }
        ok = entry["ok"] == true;
        eprintln!(
            "[{device}] {operation}: {}",
            if ok { "succeeded" } else { "failed" }
        );
        results.push(entry);
        if !ok {
            break;
        }
    }
    json!({"device":device,"ok":ok,"steps":results})
}

/// The `_android_each` worker of an android.each job.
pub fn each_worker(request: &str) -> Result<()> {
    let spec = read_json(std::path::Path::new(request), None)?;
    let steps = spec["steps"].as_array().cloned().unwrap_or_default();
    let devices = strings(&spec["devices"]);
    let results: Vec<Value> = std::thread::scope(|scope| {
        let handles: Vec<_> = devices
            .iter()
            .map(|device| (device, scope.spawn(|| each_device(device, &steps))))
            .collect();
        handles
            .into_iter()
            .map(|(device, handle)| {
                handle.join().unwrap_or_else(|_| {
                    json!({"device":device,"ok":false,"steps":[],"error":"The device's worker stopped"})
                })
            })
            .collect()
    });
    let failed: Vec<_> = results
        .iter()
        .filter(|r| r["ok"] != true)
        .map(|r| s(r, "device").to_string())
        .collect();
    let report = json!({"ok":failed.is_empty(),"devices":results,"failed":failed});
    atomic_json(std::path::Path::new(s(&spec, "report")), &report)?;
    println!("{}", serde_json::to_string_pretty(&report)?);
    if !failed.is_empty() {
        return Err(Error::new(
            "device_failed",
            format!("Failed on {}", failed.join(", ")),
        ));
    }
    Ok(())
}

/// The width and height in a PNG's header.
fn png_size(bytes: &[u8]) -> Option<(u32, u32)> {
    (bytes.len() >= 24 && bytes.starts_with(b"\x89PNG\r\n\x1a\n") && &bytes[12..16] == b"IHDR")
        .then(|| {
            (
                u32::from_be_bytes(bytes[16..20].try_into().unwrap()),
                u32::from_be_bytes(bytes[20..24].try_into().unwrap()),
            )
        })
}

/// The screen as a PNG at once, without a job: for previews that refresh,
/// such as the desktop's device grid.
pub fn screen(a: &Value) -> Result {
    let device = identifier(s(a, "device"), "Android serial", None)?;
    let target = output_path(s(a, "path"))?;
    if !target
        .extension()
        .is_some_and(|x| x.eq_ignore_ascii_case("png"))
    {
        return Err(Error::new(
            "invalid_path",
            "The screen is saved as a PNG; name a file ending in .png",
        ));
    }
    crate::runtime::android::screencap(device, &target)?;
    let bytes = std::fs::read(&target)?;
    let (width, height) = png_size(&bytes).unwrap_or((0, 0));
    Ok(json!({"device":device,"path":target,"bytes":bytes.len(),"width":width,"height":height}))
}

/// The services `adb mdns services` lists: name, type and address.
fn mdns_services(output: &str) -> Vec<Value> {
    output
        .lines()
        .filter_map(|line| {
            let parts: Vec<&str> = line.split('\t').map(str::trim).collect();
            let [name, kind, address] = parts[..] else {
                return None;
            };
            (kind.starts_with("_adb") && address.contains(':'))
                .then(|| json!({"name":name,"type":kind.trim_end_matches('.'),"address":address}))
        })
        .collect()
}

/// `adb pair` answers with exit status 0 also when pairing failed.
fn paired(output: &str) -> Option<String> {
    let line = output
        .lines()
        .find(|l| l.trim_start().starts_with("Successfully paired"))?;
    Some(
        line.split("[guid=")
            .nth(1)
            .map(|g| g.trim_end_matches(']').trim().to_string())
            .unwrap_or_default(),
    )
}

fn mdns_listing() -> Result<Vec<Value>> {
    let reply = tc::run(
        &argv(&[&tc::find("adb")?, "mdns", "services"]),
        10,
        None,
        false,
    )?;
    if reply["exit_code"].as_i64().unwrap_or(0) != 0 {
        return Err(Error::new(
            "mdns_unavailable",
            format!(
                "adb cannot discover devices on the network: {}",
                format!("{} {}", s(&reply, "stdout"), s(&reply, "stderr")).trim()
            ),
        )
        .remedy(
            "Update the Android platform tools, or pair with the code through android.connect",
        ));
    }
    Ok(mdns_services(s(&reply, "stdout")))
}

/// A word of random letters and digits.
fn random_word(length: usize) -> String {
    const ALPHABET: &[u8] = b"abcdefghijkmnpqrstuvwxyz23456789";
    let mut out = String::new();
    while out.len() < length {
        for byte in uuid::Uuid::new_v4().into_bytes() {
            if out.len() < length {
                out.push(ALPHABET[byte as usize % ALPHABET.len()] as char);
            }
        }
    }
    out
}

/// Wireless debugging pairing by QR code (Android 11 and newer). `qr` makes
/// a service name and password and the code the phone scans; `wait` waits
/// for the phone to offer that service, pairs and connects; `services`
/// lists what adb finds on the network, for pairing by code.
pub fn pair(a: &Value) -> Result {
    match s(a, "action") {
        "services" => Ok(json!({"services":mdns_listing()?})),
        "wait" => pair_wait(a),
        _ => {
            let service = format!("tomacco-{}", random_word(8));
            let password = random_word(10);
            let text = format!("WIFI:T:ADB;S:{service};P:{password};;");
            let code = crate::qr::encode(&text).ok_or_else(|| {
                Error::new("qr_too_long", "The pairing text does not fit a QR code")
            })?;
            let mut value = json!({
                "service": service,
                "password": password,
                "qr_text": text,
                "qr": {"size": code.size, "rows": code.rows()},
                "svg": code.svg(),
                "next": "On the phone open Developer options > Wireless debugging > Pair device with QR code and scan it, then call android.pair with action wait, service and password",
            });
            if !s(a, "path").is_empty() {
                let target = output_path(s(a, "path"))?;
                let extension = target
                    .extension()
                    .map(|x| x.to_string_lossy().to_lowercase())
                    .unwrap_or_default();
                let bytes = match extension.as_str() {
                    "svg" => code.svg().into_bytes(),
                    "png" => {
                        let (grey, width) = code.pixels(8);
                        let rgb: Vec<u8> = grey.iter().flat_map(|&g| [g, g, g]).collect();
                        crate::ios_emulator::virtual_screen::png(&rgb, width, width, 3)?
                    }
                    _ => {
                        return Err(Error::new(
                            "invalid_path",
                            "Name a file ending in .png or .svg for the QR code",
                        ));
                    }
                };
                if let Some(parent) = target.parent() {
                    std::fs::create_dir_all(parent)?;
                }
                atomic_write(&target, &bytes)?;
                value["path"] = json!(target);
            }
            Ok(value)
        }
    }
}

fn pair_wait(a: &Value) -> Result {
    let service = identifier(
        s(a, "service"),
        "service name",
        Some("[A-Za-z0-9][A-Za-z0-9_-]{0,62}"),
    )?;
    let password = identifier(
        s(a, "password"),
        "pairing password",
        Some("[A-Za-z0-9]{6,32}"),
    )?;
    let deadline =
        std::time::Instant::now() + std::time::Duration::from_secs(n(a, "timeout").max(1));
    let adb = tc::find("adb")?;
    let pairing = loop {
        if let Some(found) = mdns_listing()?
            .into_iter()
            .find(|v| s(v, "name") == service && s(v, "type").starts_with("_adb-tls-pairing"))
        {
            break found;
        }
        if std::time::Instant::now() >= deadline {
            return Err(Error::new(
                "pairing_timeout",
                "The phone did not offer this pairing code in time",
            )
            .remedy("On the phone, on the same network, open Developer options > Wireless debugging > Pair device with QR code and scan the code; call wait again while it shows, or pair with the code through android.connect"));
        }
        check_cancelled()?;
        std::thread::sleep(std::time::Duration::from_millis(500));
    };
    let address = s(&pairing, "address").to_string();
    let reply = tc::run(&argv(&[&adb, "pair", &address, password]), 30, None, false)?;
    let output = format!("{}\n{}", s(&reply, "stdout"), s(&reply, "stderr"));
    let guid = paired(&output).ok_or_else(|| {
        Error::new(
            "pairing_failed",
            format!("adb could not pair: {}", output.trim()),
        )
        .remedy("Show a new QR code with android.pair and scan it again")
    })?;
    // The phone then offers its connection service; adb often connects by
    // itself, otherwise connect to the address it offers.
    let host = address
        .rsplit_once(':')
        .map(|(h, _)| h)
        .unwrap_or("")
        .to_string();
    let until = std::time::Instant::now() + std::time::Duration::from_secs(10);
    let mut device = Value::Null;
    while std::time::Instant::now() < until {
        let services = mdns_listing().unwrap_or_default();
        if let Some(connect) = services.iter().find(|v| {
            s(v, "type").starts_with("_adb-tls-connect")
                && ((!guid.is_empty() && s(v, "name") == guid)
                    || s(v, "address").rsplit_once(':').map(|(h, _)| h) == Some(host.as_str()))
        }) {
            let target = s(connect, "address").to_string();
            let reply = tc::run(&argv(&[&adb, "connect", &target]), 15, None, false)?;
            let text = format!("{} {}", s(&reply, "stdout"), s(&reply, "stderr")).to_lowercase();
            if text.contains("connected to") {
                device = json!(target);
                break;
            }
        }
        check_cancelled()?;
        std::thread::sleep(std::time::Duration::from_millis(500));
    }
    Ok(json!({
        "paired": true,
        "address": address,
        "guid": guid,
        "connected": !device.is_null(),
        "device": device,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_png_header_gives_the_size() {
        let mut png = b"\x89PNG\r\n\x1a\n\0\0\0\x0dIHDR".to_vec();
        png.extend(1080u32.to_be_bytes());
        png.extend(2400u32.to_be_bytes());
        assert_eq!(png_size(&png), Some((1080, 2400)));
        assert_eq!(png_size(b"not a png"), None);
    }

    #[test]
    fn mdns_services_and_pairing_replies_are_read() {
        let listing = "List of discovered mdns services\nadb-R5CT-AbCd\t_adb-tls-connect._tcp\t192.168.1.20:37099\ntomacco-abc\t_adb-tls-pairing._tcp.\t192.168.1.20:41235\nnoise line\n";
        let services = mdns_services(listing);
        assert_eq!(services.len(), 2);
        assert_eq!(
            services[1],
            json!({"name":"tomacco-abc","type":"_adb-tls-pairing._tcp","address":"192.168.1.20:41235"})
        );
        assert_eq!(
            paired("Successfully paired to 192.168.1.20:41235 [guid=adb-R5CT-AbCd]\n").as_deref(),
            Some("adb-R5CT-AbCd")
        );
        assert_eq!(
            paired("Failed: Wrong password or connection was dropped."),
            None
        );
        let word = random_word(10);
        assert_eq!(word.len(), 10);
        assert!(word.bytes().all(|c| c.is_ascii_alphanumeric()));
    }

    #[test]
    fn health_reads_battery_storage_and_uptime() {
        let dump = "Current Battery Service state:\n  AC powered: false\n  USB powered: true\n  Wireless powered: false\n  status: 2\n  health: 2\n  present: true\n  level: 87\n  scale: 100\n  temperature: 250\n";
        let value = battery(dump);
        assert_eq!(value["level"], 87);
        assert_eq!(value["status"], "charging");
        assert_eq!(value["health"], "good");
        assert_eq!(value["temperature_celsius"], 25.0);
        assert_eq!(value["plugged"], json!(["usb"]));
        assert_eq!(value["present"], true);
        assert_eq!(battery("level: 1\nscale: 2\nstatus: 5")["level"], 50);
        assert_eq!(battery("level: 1\nscale: 2\nstatus: 5")["status"], "full");
        assert!(battery("Can't find service: battery").is_null());
        let df = "Filesystem     1K-blocks    Used Available Use% Mounted on\n/dev/block/dm-5   6082636 2105092   3961160  35% /data\n";
        assert_eq!(
            storage(df),
            json!({"path":"/data","total_bytes":6082636u64*1024,"used_bytes":2105092u64*1024,"free_bytes":3961160u64*1024})
        );
        assert!(storage("df: /data: Permission denied").is_null());
        assert_eq!(uptime("12345.67 23456.78\n"), 12345);
        assert!(uptime("").is_null());
    }

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

    #[test]
    fn app_paths_stay_inside_the_data_directory_and_listings_are_read() {
        assert_eq!(app_path("").unwrap(), ".");
        assert_eq!(
            app_path("files/db name.sqlite").unwrap(),
            "files/db name.sqlite"
        );
        for bad in [
            "/data/data/x",
            "../other",
            "files/../../x",
            "a;b",
            "a$(x)",
            "a\nb",
        ] {
            assert!(app_path(bad).is_err(), "{bad}");
        }
        assert_eq!(
            file_list("3 1793474139 ./files/a b.txt\nnoise\n12 1 ./shared_prefs/p.xml\n"),
            vec![
                json!({"path":"files/a b.txt","size":3,"modified":1793474139}),
                json!({"path":"shared_prefs/p.xml","size":12,"modified":1})
            ]
        );
    }

    #[test]
    fn a_step_gets_the_device_and_device_operations_the_platform() {
        let value = for_device("device.install", &json!({"paths":["/a.apk"]}), "R5CT");
        assert_eq!(
            value,
            json!({"paths":["/a.apk"],"device":"R5CT","platform":"android"})
        );
        let value = for_device(
            "android.launch",
            &json!({"bundle_id":"dev.example.app"}),
            "emulator-5554",
        );
        assert_eq!(
            value,
            json!({"bundle_id":"dev.example.app","device":"emulator-5554"})
        );
        assert_eq!(
            for_device("android.input", &Value::Null, "x"),
            json!({"device":"x"})
        );
    }
}
