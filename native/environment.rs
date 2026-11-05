//! Named QA environments: versioned presets of the Android settings that
//! change how an app looks, reads and behaves in tests, applied to one
//! explicitly chosen device through a plan, read back, and put back from a
//! restore point.
//!
//! The flow is always `android.environment.plan` (reads the target, decides per
//! setting whether it is supported, verifiable and restorable, and remembers
//! who the target is) then `android.environment.apply` with that plan's ID. Apply
//! refuses when the device behind the serial, the preset or the settings it
//! read have changed since the plan, so a preset never lands on whatever
//! device happens to be connected. Before it changes anything it writes the
//! restore point; `android.environment.restore` writes back exactly what was read.
//!
//! TalkBack is the installed accessibility service whose class is a
//! TalkBackService; switching it keeps every other enabled service. This is
//! the screen reader's setting, not proof that a screen reader works with
//! the app: testing.accessibility audits a hierarchy statically.
use crate::{core::*, devices, toolchain as tc};
use serde_json::{Map, Value, json};
use std::{fs, path::PathBuf};

pub const FORMAT: &str = "tomacco.environment-preset";
pub const VERSION: u64 = 1;
/// Every setting a preset may name, in the order they are applied.
pub const FIELDS: [&str; 7] = [
    "dark_mode",
    "font_scale",
    "animations",
    "airplane_mode",
    "app_locale",
    "location",
    "talkback",
];
const ANIMATION_SCALES: [&str; 3] = [
    "window_animation_scale",
    "transition_animation_scale",
    "animator_duration_scale",
];
const ACCESSIBILITY_SERVICE: &str = "android.accessibilityservice.AccessibilityService";
const ENABLED_SERVICES: &str = "enabled_accessibility_services";
const PLAN_SECONDS: f64 = 15.0 * 60.0;
const ID: &str = "[a-z0-9][a-z0-9-]{0,62}";

/// How the module reaches one device. The real one runs adb; tests answer
/// from a fake device that remembers what was written.
pub trait Transport {
    fn serial(&self) -> &str;
    /// One `adb shell` command; its standard output.
    fn shell(&mut self, args: &[&str]) -> Result<String>;
    /// One emulator console command through `adb emu`.
    fn console(&mut self, args: &[&str]) -> Result<String>;
}

pub struct Adb(String);
impl Adb {
    pub fn new(serial: &str) -> Result<Self> {
        identifier(serial, "device ID", None)?;
        Ok(Self(serial.to_string()))
    }
}
impl Transport for Adb {
    fn serial(&self) -> &str {
        &self.0
    }
    fn shell(&mut self, args: &[&str]) -> Result<String> {
        let command = devices::shell(&self.0, &argv(args))?;
        let result = tc::run(&command, 30, None, true)?;
        Ok(s(&result, "stdout").to_string())
    }
    fn console(&mut self, args: &[&str]) -> Result<String> {
        devices::console(&self.0, args)
    }
}

// ---------------------------------------------------------------- storage

fn root() -> Result<PathBuf> {
    let path = match test_root() {
        Some(path) => path,
        None => home()?.join("environments"),
    };
    private_dir(&path)?;
    Ok(path)
}
fn lock() -> Result<Lock> {
    let lock = Lock::open(&root()?.join(".lock"))?;
    lock.acquire(false)?;
    Ok(lock)
}
fn preset_path(id: &str) -> Result<PathBuf> {
    Ok(root()?
        .join("presets")
        .join(format!("{}.json", preset_id(id)?)))
}
fn record_path(kind: &str, id: &str) -> Result<PathBuf> {
    identifier(id, &format!("{kind} ID"), Some("[0-9a-f]{32}"))?;
    Ok(root()?.join(kind).join(format!("{id}.json")))
}
fn preset_id(id: &str) -> Result<&str> {
    identifier(id, "preset ID", Some(ID))
}
fn new_id() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}
fn conflict(message: impl Into<String>) -> Error {
    Error::new("environment_conflict", message)
}
fn invalid(message: impl Into<String>) -> Error {
    Error::new("environment_invalid", message)
}

/// Checks a preset's settings and returns them in canonical form: known
/// fields only, each in range, language tags as Android takes them.
pub fn settings(value: &Value) -> Result<Map<String, Value>> {
    let map = value
        .as_object()
        .ok_or_else(|| invalid("settings must be an object"))?;
    let mut out = Map::new();
    for (key, v) in map {
        let ok = match key.as_str() {
            "dark_mode" => matches!(v.as_str(), Some("on" | "off" | "auto")),
            "font_scale" => v.as_f64().is_some_and(|f| (0.5..=2.0).contains(&f)),
            "animations" | "airplane_mode" | "talkback" => v.is_boolean(),
            "app_locale" => v.as_str().is_some_and(|t| locale_tags(t).is_ok()),
            "location" => {
                let lat = v["latitude"].as_f64();
                let lon = v["longitude"].as_f64();
                v.as_object().is_some_and(|o| o.len() == 2)
                    && lat.is_some_and(|l| (-90.0..=90.0).contains(&l))
                    && lon.is_some_and(|l| (-180.0..=180.0).contains(&l))
            }
            _ => {
                return Err(invalid(format!(
                    "Unknown setting {key}; a preset takes {}",
                    FIELDS.join(", ")
                )));
            }
        };
        if !ok {
            return Err(invalid(format!("Setting {key} has an invalid value: {v}")));
        }
        out.insert(key.clone(), v.clone());
    }
    if out.is_empty() {
        return Err(invalid("A preset names at least one setting"));
    }
    Ok(out)
}
/// Language tags separated by commas; empty follows the device language.
fn locale_tags(value: &str) -> Result<&str> {
    if value.is_empty() {
        return Ok(value);
    }
    identifier(
        value,
        "language tags",
        Some("[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*(,[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*)*"),
    )
}
fn name(value: &str) -> Result<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() || trimmed.chars().count() > 80 || trimmed.chars().any(char::is_control) {
        return Err(invalid("A preset name is 1 to 80 characters on one line"));
    }
    Ok(trimmed.to_string())
}
fn description(value: &str) -> Result<String> {
    if value.chars().count() > 500 {
        return Err(invalid("A preset description is at most 500 characters"));
    }
    Ok(value.trim().to_string())
}

/// A preset document, validated: the file format, export and import share it.
fn document(value: &Value) -> Result<Value> {
    if value["format"] != FORMAT {
        return Err(invalid(format!("Not a {FORMAT} document")));
    }
    if value["version"].as_u64() != Some(VERSION) {
        return Err(Error::new(
            "environment_version_unsupported",
            format!(
                "Preset version {} is not supported; this Tomacco reads version {VERSION}",
                value["version"]
            ),
        ));
    }
    let allowed = [
        "format",
        "version",
        "id",
        "name",
        "description",
        "revision",
        "updated",
        "settings",
    ];
    if let Some(key) = value
        .as_object()
        .and_then(|o| o.keys().find(|k| !allowed.contains(&k.as_str())))
    {
        return Err(invalid(format!("Unknown preset field {key}")));
    }
    Ok(json!({
        "format": FORMAT,
        "version": VERSION,
        "id": preset_id(s(value, "id"))?,
        "name": name(s(value, "name"))?,
        "description": description(s(value, "description"))?,
        "revision": value["revision"].as_u64().filter(|r| *r >= 1).unwrap_or(1),
        "updated": value["updated"].as_f64().unwrap_or_else(now),
        "settings": Value::Object(settings(&value["settings"])?),
    }))
}
fn load(id: &str) -> Result<Value> {
    let path = preset_path(id)?;
    if !path.exists() {
        return Err(Error::new(
            "environment_not_found",
            format!("No environment preset {id}"),
        )
        .remedy("List the saved presets with android.environment.preset.list"));
    }
    document(&read_json(&path, None)?)
}
fn list() -> Result<Value> {
    let dir = root()?.join("presets");
    let mut presets = Vec::new();
    let mut problems = Vec::new();
    if dir.exists() {
        let mut entries: Vec<_> = fs::read_dir(&dir)?.flatten().map(|e| e.path()).collect();
        entries.sort();
        for path in entries {
            if path.extension().and_then(|e| e.to_str()) != Some("json") {
                continue;
            }
            match read_json(&path, None).and_then(|v| document(&v)) {
                Ok(preset) => presets.push(preset),
                Err(error) => problems.push(json!({"path":path,"error":error.value()})),
            }
        }
    }
    Ok(json!({"presets":presets,"problems":problems,"fields":FIELDS}))
}
/// Creates a preset, or replaces one when `if_revision` names its current
/// revision. Writes never overwrite a revision the caller has not seen.
fn save(a: &Value) -> Result<Value> {
    let id = preset_id(s(a, "id"))?;
    let _lock = lock()?;
    let path = preset_path(id)?;
    let current = path.exists().then(|| load(id)).transpose()?;
    let revision = match (&current, a["if_revision"].as_u64()) {
        (None, None) => 1,
        (None, Some(_)) => {
            return Err(Error::new(
                "environment_not_found",
                format!("No environment preset {id}"),
            ));
        }
        (Some(c), None) => {
            return Err(conflict(format!(
                "Preset {id} exists at revision {}; pass if_revision to replace it",
                c["revision"]
            )));
        }
        (Some(c), Some(r)) if c["revision"].as_u64() != Some(r) => {
            return Err(conflict(format!(
                "Preset {id} is at revision {}, not {r}",
                c["revision"]
            ))
            .details(c.clone()));
        }
        (Some(_), Some(r)) => r + 1,
    };
    let preset = document(&json!({
        "format": FORMAT,
        "version": VERSION,
        "id": id,
        "name": s(a, "name"),
        "description": s(a, "description"),
        "revision": revision,
        "updated": now(),
        "settings": a["settings"],
    }))?;
    atomic_json(&path, &preset)?;
    Ok(json!({"preset":preset,"created":revision == 1}))
}
fn delete(a: &Value) -> Result<Value> {
    let id = preset_id(s(a, "id"))?;
    let _lock = lock()?;
    let current = load(id)?;
    if current["revision"].as_u64() != a["if_revision"].as_u64() {
        return Err(conflict(format!(
            "Preset {id} is at revision {}; delete names the revision it removes",
            current["revision"]
        )));
    }
    fs::remove_file(preset_path(id)?)?;
    Ok(json!({"deleted":id,"revision":current["revision"]}))
}
fn export(a: &Value) -> Result<Value> {
    let preset = load(s(a, "id"))?;
    let mut value = json!({"document":preset});
    let target = s(a, "path");
    if !target.is_empty() {
        let path = output_path(target)?;
        if path.exists() {
            return Err(conflict(format!("{} already exists", path.display()))
                .remedy("Choose a new file; an export never replaces one"));
        }
        atomic_json(&path, &preset)?;
        value["path"] = json!(path);
    }
    Ok(value)
}
/// Imports a document as a new preset, or over an existing one when
/// `if_revision` names its current revision. The stored revision continues
/// the local history, not the exporter's.
fn import(a: &Value) -> Result<Value> {
    let source = match (a.get("document"), s(a, "path")) {
        (Some(doc), "") => doc.clone(),
        (None, p) if !p.is_empty() => read_json(&path(p, false, true)?, None)?,
        _ => return Err(invalid("Pass either document or path")),
    };
    let doc = document(&source)?;
    let id = s(a, "id");
    let id = if id.is_empty() { s(&doc, "id") } else { id };
    let mut request = json!({"id":id,"name":doc["name"],"description":doc["description"],"settings":doc["settings"]});
    if let Some(r) = a.get("if_revision") {
        request["if_revision"] = r.clone();
    }
    let mut value = save(&request)?;
    value["imported_from_revision"] = doc["revision"].clone();
    Ok(value)
}

// ---------------------------------------------------------------- device

/// What a field read needs to know about the target.
pub struct Context {
    pub sdk: u64,
    pub emulator: bool,
    pub bundle_id: String,
}

/// `pkg/.Class` and `pkg/pkg.Class` name the same service.
pub fn component(value: &str) -> Option<String> {
    let (package, class) = value.trim().split_once('/')?;
    let ok = |v: &str| {
        !v.is_empty()
            && v.chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '$'))
    };
    if !ok(package) || !ok(class) || !package.contains('.') {
        return None;
    }
    let class = match class.strip_prefix('.') {
        Some(rest) => format!("{package}.{rest}"),
        None => class.to_string(),
    };
    Some(format!("{package}/{class}"))
}
/// The accessibility services `cmd package query-services --brief` lists,
/// in full form. The brief output has one `package/class` line per service.
pub fn installed_services(output: &str) -> Vec<String> {
    let mut services = Vec::new();
    for token in output.split_whitespace() {
        if let Some(c) = component(token) {
            if !services.contains(&c) {
                services.push(c);
            }
        }
    }
    services
}
/// The TalkBack service among the installed ones: Google's package first,
/// then any service class named TalkBackService (vendor builds ship their own).
pub fn talkback_service(installed: &[String]) -> Option<String> {
    let talkback = |c: &&String| c.ends_with(".TalkBackService");
    installed
        .iter()
        .filter(talkback)
        .find(|c| c.starts_with("com.google.android.marvin.talkback/"))
        .or_else(|| installed.iter().find(talkback))
        .cloned()
}
/// The enabled services as Android stores them: colon-separated, or `null`.
pub fn enabled_list(raw: &str) -> Vec<String> {
    raw.trim()
        .split(':')
        .map(str::trim)
        .filter(|v| !v.is_empty() && *v != "null")
        .map(String::from)
        .collect()
}
fn same_service(a: &str, b: &str) -> bool {
    match (component(a), component(b)) {
        (Some(x), Some(y)) => x == y,
        _ => a == b,
    }
}
/// The enabled list with TalkBack switched on or off. Every other service
/// keeps its place and spelling.
pub fn switch_talkback(raw: &str, service: &str, on: bool) -> Vec<String> {
    let mut list: Vec<String> = enabled_list(raw)
        .into_iter()
        .filter(|v| !same_service(v, service))
        .collect();
    if on {
        list.push(service.to_string());
    }
    list
}

fn trimmed(value: String) -> String {
    value.trim().to_string()
}
fn setting(t: &mut dyn Transport, namespace: &str, key: &str) -> Result<String> {
    Ok(trimmed(t.shell(&["settings", "get", namespace, key])?))
}
/// Writes a setting back as it was read; `null` was unset, so it is deleted.
fn put_raw(t: &mut dyn Transport, namespace: &str, key: &str, raw: &str) -> Result<()> {
    if raw == "null" {
        t.shell(&["settings", "delete", namespace, key])?;
    } else {
        t.shell(&["settings", "put", namespace, key, raw])?;
    }
    Ok(())
}
fn night(t: &mut dyn Transport) -> Result<String> {
    let output = t.shell(&["cmd", "uimode", "night"])?;
    output
        .lines()
        .find_map(|l| l.trim().strip_prefix("Night mode: "))
        .map(|v| v.trim().to_string())
        .ok_or_else(|| {
            Error::new(
                "setting_unsupported",
                "Android did not report its night mode",
            )
            .details(json!({"output":output}))
        })
}
/// The tags in `Locales for PACKAGE for user 0 are [de-DE,fr]`.
fn app_locales(output: &str) -> Option<String> {
    let line = output.lines().find(|l| l.starts_with("Locales for "))?;
    let list = line.rsplit_once(" are [")?.1.strip_suffix(']')?;
    Some(list.split(',').map(str::trim).collect::<Vec<_>>().join(","))
}
pub fn identity(t: &mut dyn Transport) -> Result<Value> {
    let mut value = json!({"serial": t.serial()});
    for (key, prop) in [
        ("serialno", "ro.serialno"),
        ("model", "ro.product.model"),
        ("fingerprint", "ro.build.fingerprint"),
        ("sdk", "ro.build.version.sdk"),
    ] {
        value[key] = json!(trimmed(t.shell(&["getprop", prop])?));
    }
    if value["sdk"].as_str().unwrap_or("").parse::<u64>().is_err() {
        return Err(Error::new(
            "environment_target_unreadable",
            format!("{} did not report its Android version", t.serial()),
        )
        .details(value));
    }
    Ok(value)
}
pub fn context(t: &mut dyn Transport, identity: &Value, bundle_id: &str) -> Context {
    Context {
        sdk: s(identity, "sdk").parse().unwrap_or(0),
        emulator: t.serial().starts_with("emulator-"),
        bundle_id: bundle_id.to_string(),
    }
}
fn unsupported(reason: &str) -> Value {
    json!({"supported":false,"reason":reason,"value":null,"raw":null,"verifiable":false,"restorable":false})
}
fn supported(value: Value, raw: Value, restorable: bool) -> Value {
    json!({"supported":true,"value":value,"raw":raw,"verifiable":true,"restorable":restorable})
}

/// Reads one field as it is now: the value in preset terms, the raw values
/// a restore writes back, and whether the device supports it at all.
pub fn read(t: &mut dyn Transport, field: &str, cx: &Context) -> Result<Value> {
    Ok(match field {
        "dark_mode" => match night(t) {
            Ok(raw) => {
                let value = match raw.as_str() {
                    "yes" => "on",
                    "no" => "off",
                    "auto" => "auto",
                    other => other,
                };
                let restorable = matches!(raw.as_str(), "yes" | "no" | "auto");
                supported(json!(value), json!(raw), restorable)
            }
            Err(_) => unsupported("This Android does not report its night mode to adb"),
        },
        "font_scale" => {
            let raw = setting(t, "system", "font_scale")?;
            supported(json!(raw.parse::<f64>().unwrap_or(1.0)), json!(raw), true)
        }
        "animations" => {
            let mut raw = json!({});
            let mut on = true;
            for key in ANIMATION_SCALES {
                let v = setting(t, "global", key)?;
                on &= v.parse::<f64>().unwrap_or(1.0) != 0.0;
                raw[key] = json!(v);
            }
            supported(json!(on), raw, true)
        }
        "airplane_mode" if cx.sdk < 30 => {
            unsupported("Switching airplane mode from adb needs Android 11 (API 30) or newer")
        }
        "airplane_mode" => {
            let raw = setting(t, "global", "airplane_mode_on")?;
            supported(json!(raw == "1"), json!(raw), true)
        }
        "app_locale" if cx.sdk < 33 => {
            unsupported("A language for one app needs Android 13 (API 33) or newer")
        }
        "app_locale" if cx.bundle_id.is_empty() => {
            unsupported("An app language needs the app: pass bundle_id")
        }
        "app_locale" => {
            let output = t.shell(&["cmd", "locale", "get-app-locales", &cx.bundle_id])?;
            match app_locales(&output) {
                Some(tags) => supported(json!(tags), json!(tags), true),
                None => unsupported("The app is not installed, so Android has no language for it"),
            }
        }
        "location" if !cx.emulator => unsupported(
            "Only an emulator takes a GPS fix from Tomacco; a phone would need a mock location app",
        ),
        "location" => {
            json!({"supported":true,"value":null,"raw":null,"verifiable":false,"restorable":false,
            "reason":"The emulator console sets a GPS fix but reports neither it nor the previous one back"})
        }
        "talkback" => {
            let services = installed_services(&t.shell(&[
                "cmd",
                "package",
                "query-services",
                "--brief",
                "-a",
                ACCESSIBILITY_SERVICE,
            ])?);
            let Some(service) = talkback_service(&services) else {
                let mut value = unsupported("TalkBack is not installed on this device");
                value["installed_services"] = json!(services);
                return Ok(value);
            };
            let enabled = setting(t, "secure", ENABLED_SERVICES)?;
            let switch = setting(t, "secure", "accessibility_enabled")?;
            let on = enabled_list(&enabled)
                .iter()
                .any(|v| same_service(v, &service));
            let mut value = supported(
                json!(on),
                json!({ENABLED_SERVICES: enabled, "accessibility_enabled": switch}),
                true,
            );
            value["service"] = json!(service);
            value["installed_services"] = json!(services);
            value["other_enabled_services"] = json!(
                enabled_list(&enabled)
                    .into_iter()
                    .filter(|v| !same_service(v, &service))
                    .collect::<Vec<_>>()
            );
            value
        }
        other => return Err(invalid(format!("Unknown setting {other}"))),
    })
}

/// Sets one field to the preset's value. `current` is this field's read.
pub fn write(
    t: &mut dyn Transport,
    field: &str,
    target: &Value,
    current: &Value,
    cx: &Context,
) -> Result<()> {
    match field {
        "dark_mode" => {
            let night = match target.as_str() {
                Some("on") => "yes",
                Some("off") => "no",
                _ => "auto",
            };
            t.shell(&["cmd", "uimode", "night", night])?;
        }
        "font_scale" => {
            let v = target.as_f64().unwrap_or(1.0).to_string();
            t.shell(&["settings", "put", "system", "font_scale", &v])?;
        }
        "animations" => {
            let v = if target == true { "1" } else { "0" };
            for key in ANIMATION_SCALES {
                t.shell(&["settings", "put", "global", key, v])?;
            }
        }
        "airplane_mode" => {
            let v = if target == true { "enable" } else { "disable" };
            t.shell(&["cmd", "connectivity", "airplane-mode", v])?;
        }
        "app_locale" => {
            let tags = target.as_str().unwrap_or("");
            let mut args = vec!["cmd", "locale", "set-app-locales", cx.bundle_id.as_str()];
            if !tags.is_empty() {
                args.extend(["--locales", tags]);
            }
            t.shell(&args)?;
        }
        "location" => {
            let lon = target["longitude"].to_string();
            let lat = target["latitude"].to_string();
            t.console(&["geo", "fix", &lon, &lat])?;
        }
        "talkback" => {
            let service = s(current, "service");
            let list = switch_talkback(
                s(&current["raw"], ENABLED_SERVICES),
                service,
                target == true,
            );
            if list.is_empty() {
                t.shell(&["settings", "delete", "secure", ENABLED_SERVICES])?;
            } else {
                t.shell(&[
                    "settings",
                    "put",
                    "secure",
                    ENABLED_SERVICES,
                    &list.join(":"),
                ])?;
            }
            let switch = if list.is_empty() { "0" } else { "1" };
            t.shell(&["settings", "put", "secure", "accessibility_enabled", switch])?;
        }
        other => return Err(invalid(format!("Unknown setting {other}"))),
    }
    Ok(())
}

/// Writes back one field's raw values from before the change.
pub fn restore_field(
    t: &mut dyn Transport,
    field: &str,
    before: &Value,
    cx: &Context,
) -> Result<()> {
    let raw = &before["raw"];
    match field {
        "dark_mode" => {
            t.shell(&["cmd", "uimode", "night", raw.as_str().unwrap_or("auto")])?;
        }
        "font_scale" => put_raw(t, "system", "font_scale", raw.as_str().unwrap_or("null"))?,
        "animations" => {
            for key in ANIMATION_SCALES {
                put_raw(t, "global", key, s(raw, key))?;
            }
        }
        "airplane_mode" => {
            let v = if raw == "1" { "enable" } else { "disable" };
            t.shell(&["cmd", "connectivity", "airplane-mode", v])?;
        }
        "app_locale" => write(t, field, raw, before, cx)?,
        "talkback" => {
            put_raw(t, "secure", ENABLED_SERVICES, s(raw, ENABLED_SERVICES))?;
            put_raw(
                t,
                "secure",
                "accessibility_enabled",
                s(raw, "accessibility_enabled"),
            )?;
        }
        other => {
            return Err(Error::new(
                "environment_not_restorable",
                format!("{other} has no previous value to restore"),
            ));
        }
    }
    Ok(())
}
/// Whether a read matches the preset's value.
pub fn matches(field: &str, read: &Value, target: &Value) -> bool {
    let value = &read["value"];
    match field {
        "font_scale" => {
            (value.as_f64().unwrap_or(0.0) - target.as_f64().unwrap_or(-1.0)).abs() < 0.001
        }
        "app_locale" => value
            .as_str()
            .zip(target.as_str())
            .is_some_and(|(v, t)| v.eq_ignore_ascii_case(t)),
        _ => value == target,
    }
}

// ---------------------------------------------------------------- flow

const NOT_A_SCREEN_READER_TEST: &str = "TalkBack here is the device setting and its read-back. It does not show that the app works with a screen reader; testing.accessibility audits a hierarchy statically.";

fn device_arg(a: &Value) -> Result<&str> {
    let device = s(a, "device");
    if device.is_empty() {
        return Err(Error::new(
            "device_required",
            "Name the device: a QA environment never applies to whatever device is connected",
        )
        .remedy("Pass the serial from device.list"));
    }
    identifier(device, "device ID", None)
}
fn bundle_arg(a: &Value) -> Result<&str> {
    let id = s(a, "bundle_id");
    if !id.is_empty() {
        identifier(
            id,
            "application ID",
            Some(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+"),
        )?;
    }
    Ok(id)
}

/// Every field as the device has it now, for the desktop and agents.
pub fn inspect_with(t: &mut dyn Transport, bundle_id: &str) -> Result<Value> {
    let id = identity(t)?;
    let cx = context(t, &id, bundle_id);
    let mut fields = json!({});
    for field in FIELDS {
        fields[field] = read(t, field, &cx)?;
    }
    Ok(
        json!({"device":t.serial(),"identity":id,"sdk":cx.sdk,"emulator":cx.emulator,
        "bundle_id":bundle_id,"fields":fields,"talkback_note":NOT_A_SCREEN_READER_TEST}),
    )
}

/// Reads the target for the preset's fields and records what apply will do.
pub fn plan_with(t: &mut dyn Transport, preset: &Value, bundle_id: &str) -> Result<Value> {
    let id = identity(t)?;
    let cx = context(t, &id, bundle_id);
    let mut entries = Vec::new();
    for field in FIELDS {
        let Some(target) = preset["settings"].get(field) else {
            continue;
        };
        let current = read(t, field, &cx)?;
        let supported = current["supported"] == true;
        let verifiable = current["verifiable"] == true;
        let change = supported && !(verifiable && matches(field, &current, target));
        entries.push(json!({"field":field,"target":target,"current":current,
            "action": if !supported { "unsupported" } else if change { "change" } else { "unchanged" }}));
    }
    let plan_id = new_id();
    let created = now();
    let plan = json!({"plan_id":plan_id,"created":created,"expires":created + PLAN_SECONDS,
        "preset":{"id":preset["id"],"name":preset["name"],"revision":preset["revision"]},
        "device":t.serial(),"identity":id,"bundle_id":bundle_id,"entries":entries,
        "unsupported":entries.iter().filter(|e| e["action"]=="unsupported").map(|e| e["field"].clone()).collect::<Vec<_>>(),
        "not_restorable":entries.iter().filter(|e| e["action"]=="change" && e["current"]["restorable"]!=true).map(|e| e["field"].clone()).collect::<Vec<_>>(),
        "applied":false});
    let _lock = lock()?;
    atomic_json(&record_path("plans", &plan_id)?, &plan)?;
    Ok(plan)
}

fn identity_changed(planned: &Value, actual: &Value) -> Option<Error> {
    (planned != actual).then(|| {
        Error::new(
            "environment_target_changed",
            format!(
                "{} is no longer the device the plan read",
                s(planned, "serial")
            ),
        )
        .remedy("Plan again for the device you mean; nothing was changed")
        .details(json!({"planned":planned,"actual":actual}))
    })
}

/// Applies a plan to the device it was made for, after writing the restore point.
pub fn apply_with(t: &mut dyn Transport, plan_id: &str, allow_partial: bool) -> Result<Value> {
    let plan_path = record_path("plans", plan_id)?;
    let guard = lock()?;
    if !plan_path.exists() {
        return Err(Error::new(
            "environment_not_found",
            format!("No plan {plan_id}"),
        ));
    }
    let mut plan = read_json(&plan_path, None)?;
    if s(&plan, "device") != t.serial() {
        return Err(Error::new(
            "environment_target_changed",
            format!(
                "Plan {plan_id} is for {}, not {}",
                s(&plan, "device"),
                t.serial()
            ),
        ));
    }
    if plan["applied"] == true {
        return Err(conflict(
            "This plan was applied already; plan again to apply the preset once more",
        ));
    }
    if plan["expires"].as_f64().unwrap_or(0.0) < now() {
        return Err(conflict("This plan is older than 15 minutes; plan again"));
    }
    let unsupported = plan["unsupported"].as_array().cloned().unwrap_or_default();
    if !unsupported.is_empty() && !allow_partial {
        return Err(Error::new(
            "environment_unsupported",
            format!(
                "The device does not support: {}",
                unsupported
                    .iter()
                    .filter_map(Value::as_str)
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
        )
        .remedy("Apply with allow_partial to set the rest, or choose a device that supports them")
        .details(json!({"unsupported":unsupported})));
    }
    let preset = load(s(&plan["preset"], "id"))?;
    if preset["revision"] != plan["preset"]["revision"] {
        return Err(conflict("The preset changed after the plan; plan again"));
    }
    let actual = identity(t)?;
    if let Some(error) = identity_changed(&plan["identity"], &actual) {
        return Err(error);
    }
    let cx = context(t, &actual, s(&plan, "bundle_id"));
    let entries = plan["entries"].as_array().cloned().unwrap_or_default();
    for entry in &entries {
        let field = s(entry, "field");
        let now_read = read(t, field, &cx)?;
        if now_read["raw"] != entry["current"]["raw"]
            || now_read["supported"] != entry["current"]["supported"]
        {
            return Err(conflict(format!(
                "{field} changed on the device after the plan; plan again"
            ))
            .details(json!({"field":field,"planned":entry["current"],"now":now_read})));
        }
    }
    plan["applied"] = json!(true);
    atomic_json(&plan_path, &plan)?;
    let restore_id = new_id();
    let restore_path = record_path("restores", &restore_id)?;
    let mut record = json!({"restore_id":restore_id,"plan_id":plan_id,"created":now(),"device":t.serial(),
        "identity":actual,"bundle_id":cx.bundle_id,"preset":plan["preset"],"state":"applying",
        "before":{},"applied":[]});
    for entry in &entries {
        if entry["action"] == "change" {
            record["before"][s(entry, "field")] = entry["current"].clone();
        }
    }
    atomic_json(&restore_path, &record)?;
    drop(guard);

    let mut applied = Vec::new();
    for entry in entries.iter().filter(|e| e["action"] == "change") {
        let field = s(entry, "field");
        if let Err(error) = write(t, field, &entry["target"], &entry["current"], &cx) {
            record["applied"] = json!(applied);
            record["state"] = json!("failed");
            record["error"] = error.value();
            atomic_json(&restore_path, &record)?;
            return Err(
                error.details(json!({"restore_id":restore_id,"applied":applied,"failed":field}))
            );
        }
        applied.push(field.to_string());
        record["applied"] = json!(applied);
        atomic_json(&restore_path, &record)?;
    }
    let mut results = Vec::new();
    let mut differences = Vec::new();
    for entry in &entries {
        let field = s(entry, "field");
        let action = s(entry, "action");
        if action == "unsupported" {
            results.push(
                json!({"field":field,"status":"unsupported","reason":entry["current"]["reason"]}),
            );
            continue;
        }
        let after = read(t, field, &cx)?;
        let status = if after["verifiable"] != true {
            "applied_unverified"
        } else if matches(field, &after, &entry["target"]) {
            if action == "change" {
                "applied"
            } else {
                "unchanged"
            }
        } else {
            differences.push(field.to_string());
            "not_applied"
        };
        results.push(
            json!({"field":field,"status":status,"target":entry["target"],"value":after["value"],
            "before":entry["current"]["value"],"restorable":entry["current"]["restorable"]}),
        );
    }
    record["state"] = json!(if differences.is_empty() {
        "applied"
    } else {
        "partially_applied"
    });
    record["results"] = json!(results);
    atomic_json(&restore_path, &record)?;
    let value = json!({"device":t.serial(),"preset":plan["preset"],"restore_id":restore_id,"results":results,
        "applied":applied,"talkback_note":NOT_A_SCREEN_READER_TEST});
    if !differences.is_empty() {
        return Err(Error::new(
            "setting_not_applied",
            format!("Android did not apply: {}", differences.join(", ")),
        )
        .remedy("android.environment.restore with the restore_id puts back what was changed")
        .details(value));
    }
    Ok(value)
}

/// Puts back what an apply changed, on the same device, and reads it back.
pub fn restore_with(t: &mut dyn Transport, restore_id: &str) -> Result<Value> {
    let path = record_path("restores", restore_id)?;
    let _lock = lock()?;
    if !path.exists() {
        return Err(Error::new(
            "environment_not_found",
            format!("No restore point {restore_id}"),
        ));
    }
    let mut record = read_json(&path, None)?;
    if s(&record, "device") != t.serial() {
        return Err(Error::new(
            "environment_target_changed",
            format!(
                "Restore point {restore_id} is for {}, not {}",
                s(&record, "device"),
                t.serial()
            ),
        ));
    }
    if record["state"] == "restored" {
        return Err(conflict("This restore point was used already").details(record));
    }
    let actual = identity(t)?;
    if let Some(error) = identity_changed(&record["identity"], &actual) {
        return Err(error);
    }
    let cx = context(t, &actual, s(&record, "bundle_id"));
    let mut results = Vec::new();
    let mut failed = Vec::new();
    for field in strings(&record["applied"]) {
        let before = record["before"][&field].clone();
        if before["restorable"] != true {
            results
                .push(json!({"field":field,"status":"not_restorable","reason":before["reason"]}));
            continue;
        }
        restore_field(t, &field, &before, &cx)?;
        let after = read(t, &field, &cx)?;
        let same = after["raw"] == before["raw"]
            || (field == "font_scale" && after["value"] == before["value"]);
        if !same {
            failed.push(field.clone());
        }
        results.push(
            json!({"field":field,"status": if same { "restored" } else { "not_restored" },
            "value":after["value"],"expected":before["value"]}),
        );
    }
    record["state"] = json!(if failed.is_empty() {
        "restored"
    } else {
        "restore_incomplete"
    });
    record["restored"] = json!(now());
    record["restore_results"] = json!(results);
    atomic_json(&path, &record)?;
    let value = json!({"device":t.serial(),"restore_id":restore_id,"results":results});
    if !failed.is_empty() {
        return Err(Error::new(
            "setting_not_applied",
            format!("Android did not restore: {}", failed.join(", ")),
        )
        .details(value));
    }
    Ok(value)
}

fn restores(a: &Value) -> Result<Value> {
    let dir = root()?.join("restores");
    let device = s(a, "device");
    let mut values = Vec::new();
    if dir.exists() {
        for entry in fs::read_dir(&dir)?.flatten() {
            if let Ok(v) = read_json(&entry.path(), None) {
                if device.is_empty() || s(&v, "device") == device {
                    values.push(json!({"restore_id":v["restore_id"],"device":v["device"],"identity":v["identity"],
                        "preset":v["preset"],"state":v["state"],"created":v["created"],"applied":v["applied"]}));
                }
            }
        }
    }
    values.sort_by(|x, y| {
        y["created"]
            .as_f64()
            .unwrap_or(0.0)
            .total_cmp(&x["created"].as_f64().unwrap_or(0.0))
    });
    values.truncate(50);
    Ok(json!({"restores":values}))
}

fn capabilities() -> Value {
    json!({"format":FORMAT,"version":VERSION,"fields":{
        "dark_mode":{"values":["on","off","auto"],"verifiable":true,"restorable":true},
        "font_scale":{"minimum":0.5,"maximum":2.0,"verifiable":true,"restorable":true},
        "animations":{"verifiable":true,"restorable":true},
        "airplane_mode":{"minimum_sdk":30,"verifiable":true,"restorable":true},
        "app_locale":{"minimum_sdk":33,"needs":"bundle_id","verifiable":true,"restorable":true},
        "location":{"emulator_only":true,"verifiable":false,"restorable":false},
        "talkback":{"needs":"an installed TalkBack service","keeps_other_services":true,"verifiable":true,"restorable":true,
            "note":NOT_A_SCREEN_READER_TEST}},
        "device_language":"not offered: a whole-device language change from adb is not reliable on user builds"})
}

pub fn handle(op: &str, a: &Value) -> Result {
    match op {
        "android.environment.capabilities" => Ok(capabilities()),
        "android.environment.preset.list" => list(),
        "android.environment.preset.get" => Ok(json!({"preset":load(s(a, "id"))?})),
        "android.environment.preset.save" => save(a),
        "android.environment.preset.delete" => delete(a),
        "android.environment.preset.export" => export(a),
        "android.environment.preset.import" => import(a),
        "android.environment.inspect" => {
            let bundle = bundle_arg(a)?;
            inspect_with(&mut Adb::new(device_arg(a)?)?, bundle)
        }
        "android.environment.plan" => {
            let bundle = bundle_arg(a)?;
            let preset = load(s(a, "preset"))?;
            if preset["settings"].get("app_locale").is_some() && bundle.is_empty() {
                return Err(invalid(
                    "This preset sets an app language; pass the app's bundle_id",
                ));
            }
            plan_with(&mut Adb::new(device_arg(a)?)?, &preset, bundle)
        }
        "android.environment.apply" => apply_with(
            &mut Adb::new(device_arg(a)?)?,
            s(a, "plan_id"),
            b(a, "allow_partial"),
        ),
        "android.environment.restore" => {
            restore_with(&mut Adb::new(device_arg(a)?)?, s(a, "restore_id"))
        }
        "android.environment.restores" => restores(a),
        _ => Err(Error::new("operation_not_found", op)),
    }
}

#[cfg(test)]
thread_local! {
    static TEST_ROOT: std::cell::RefCell<Option<PathBuf>> = const { std::cell::RefCell::new(None) };
}
#[cfg(test)]
fn test_root() -> Option<PathBuf> {
    TEST_ROOT.with(|r| r.borrow().clone())
}
#[cfg(not(test))]
fn test_root() -> Option<PathBuf> {
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    /// A device that remembers what was written, as adb would leave it.
    struct Fake {
        serial: String,
        props: BTreeMap<String, String>,
        settings: BTreeMap<String, String>,
        night: String,
        locale: Option<String>,
        services: String,
        calls: Vec<Vec<String>>,
        ignore_night: bool,
    }
    impl Fake {
        fn new(serial: &str, sdk: &str) -> Self {
            let props = [
                ("ro.serialno", serial),
                ("ro.product.model", "sdk_gphone64"),
                (
                    "ro.build.fingerprint",
                    "google/sdk/emu:15/X/1:user/release-keys",
                ),
                ("ro.build.version.sdk", sdk),
            ];
            Fake {
                serial: serial.into(),
                props: props.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect(),
                settings: BTreeMap::from([
                    ("global/airplane_mode_on".into(), "0".into()),
                    ("secure/enabled_accessibility_services".into(), "com.example.switch/.SwitchService".into()),
                    ("secure/accessibility_enabled".into(), "1".into()),
                ]),
                night: "no".into(),
                locale: Some(String::new()),
                services: "priority=0 preferredOrder=0 match=0x108000\n  com.example.switch/.SwitchService\npriority=0\n  com.google.android.marvin.talkback/com.google.android.marvin.talkback.TalkBackService\n".into(),
                calls: Vec::new(),
                ignore_night: false,
            }
        }
        fn writes(&self) -> Vec<String> {
            self.calls
                .iter()
                .map(|c| c.join(" "))
                .filter(|c| {
                    c.starts_with("settings put")
                        || c.starts_with("settings delete")
                        || c.contains("night ")
                        || c.contains("airplane-mode")
                        || c.contains("set-app-locales")
                        || c.starts_with("emu")
                })
                .collect()
        }
    }
    impl Transport for Fake {
        fn serial(&self) -> &str {
            &self.serial
        }
        fn shell(&mut self, args: &[&str]) -> Result<String> {
            self.calls.push(argv(args));
            Ok(match args {
                ["getprop", p] => self.props.get(*p).cloned().unwrap_or_default(),
                ["settings", "get", ns, k] => self
                    .settings
                    .get(&format!("{ns}/{k}"))
                    .cloned()
                    .unwrap_or("null".into()),
                ["settings", "put", ns, k, v] => {
                    self.settings.insert(format!("{ns}/{k}"), v.to_string());
                    String::new()
                }
                ["settings", "delete", ns, k] => {
                    self.settings.remove(&format!("{ns}/{k}"));
                    String::new()
                }
                ["cmd", "uimode", "night"] => format!("Night mode: {}", self.night),
                ["cmd", "uimode", "night", v] => {
                    if !self.ignore_night {
                        self.night = v.to_string();
                    }
                    format!("Night mode: {}", self.night)
                }
                ["cmd", "connectivity", "airplane-mode", v] => {
                    let on = if *v == "enable" { "1" } else { "0" };
                    self.settings
                        .insert("global/airplane_mode_on".into(), on.into());
                    String::new()
                }
                ["cmd", "locale", "get-app-locales", id] => match &self.locale {
                    Some(l) => format!("Locales for {id} for user 0 are [{l}]"),
                    None => format!("Unknown package {id} for userId 0"),
                },
                ["cmd", "locale", "set-app-locales", _, "--locales", l] => {
                    self.locale = Some(l.to_string());
                    String::new()
                }
                ["cmd", "locale", "set-app-locales", _] => {
                    self.locale = Some(String::new());
                    String::new()
                }
                ["cmd", "package", "query-services", ..] => self.services.clone(),
                other => panic!("unexpected shell {other:?}"),
            })
        }
        fn console(&mut self, args: &[&str]) -> Result<String> {
            let mut call = vec!["emu".to_string()];
            call.extend(argv(args));
            self.calls.push(call);
            Ok(String::new())
        }
    }

    fn isolated() -> tempfile::TempDir {
        let temp = tempfile::tempdir().unwrap();
        TEST_ROOT.with(|r| *r.borrow_mut() = Some(temp.path().to_path_buf()));
        temp
    }
    fn preset(settings: Value) -> Value {
        save(&json!({"id":"night-qa","name":"Night QA","settings":settings})).unwrap()["preset"]
            .clone()
    }

    #[test]
    fn talkback_is_found_switched_and_keeps_other_services() {
        let installed = installed_services(
            "priority=0\n  com.example.switch/.SwitchService\n  com.samsung.android.accessibility.talkback/com.samsung.android.marvin.talkback.TalkBackService\n  com.google.android.marvin.talkback/.TalkBackService\n",
        );
        assert_eq!(installed.len(), 3);
        assert_eq!(
            talkback_service(&installed).as_deref(),
            Some(
                "com.google.android.marvin.talkback/com.google.android.marvin.talkback.TalkBackService"
            )
        );
        assert_eq!(
            talkback_service(&installed[..2]).as_deref(),
            Some(
                "com.samsung.android.accessibility.talkback/com.samsung.android.marvin.talkback.TalkBackService"
            )
        );
        assert_eq!(talkback_service(&installed[..1]), None);
        let tb =
            "com.google.android.marvin.talkback/com.google.android.marvin.talkback.TalkBackService";
        let raw = "com.example.switch/.SwitchService:com.google.android.marvin.talkback/.TalkBackService:com.example.other/.A";
        assert_eq!(
            switch_talkback(raw, tb, false),
            ["com.example.switch/.SwitchService", "com.example.other/.A"]
        );
        assert_eq!(switch_talkback("null", tb, true), [tb]);
        assert_eq!(
            switch_talkback(raw, tb, true).len(),
            3,
            "no duplicate entry"
        );
        assert!(component("a/b; reboot").is_none());
        assert!(component("nodot/.X").is_none());
    }

    #[test]
    fn presets_are_validated_versioned_and_never_overwritten_unseen() {
        let _temp = isolated();
        for bad in [
            json!({}),
            json!({"font_scale":3}),
            json!({"dark_mode":"dim"}),
            json!({"system_locale":"de"}),
            json!({"app_locale":"de;reboot"}),
            json!({"location":{"latitude":91,"longitude":0}}),
        ] {
            let e = save(&json!({"id":"x","name":"X","settings":bad})).unwrap_err();
            assert_eq!(e.code, "environment_invalid", "{bad}");
        }
        assert_eq!(
            save(&json!({"id":"Bad ID","name":"X","settings":{"animations":false}}))
                .unwrap_err()
                .code,
            "invalid_argument"
        );
        let first = preset(json!({"dark_mode":"on","talkback":true}));
        assert_eq!(first["revision"], 1);
        assert_eq!(
            save(&json!({"id":"night-qa","name":"Again","settings":{"animations":false}}))
                .unwrap_err()
                .code,
            "environment_conflict"
        );
        assert_eq!(save(&json!({"id":"night-qa","name":"Old","if_revision":5,"settings":{"animations":false}})).unwrap_err().code, "environment_conflict");
        let second = save(&json!({"id":"night-qa","name":"Night QA 2","if_revision":1,"settings":{"animations":false}})).unwrap();
        assert_eq!(second["preset"]["revision"], 2);
        // Export, import under a new ID, and refuse a future format version.
        let doc = export(&json!({"id":"night-qa"})).unwrap()["document"].clone();
        let copy = import(&json!({"document":doc,"id":"copy"})).unwrap();
        assert_eq!(
            (
                copy["preset"]["revision"].clone(),
                copy["imported_from_revision"].clone()
            ),
            (json!(1), json!(2))
        );
        let mut future = doc.clone();
        future["version"] = json!(2);
        assert_eq!(
            import(&json!({"document":future})).unwrap_err().code,
            "environment_version_unsupported"
        );
        assert_eq!(list().unwrap()["presets"].as_array().unwrap().len(), 2);
        assert_eq!(
            delete(&json!({"id":"copy","if_revision":2}))
                .unwrap_err()
                .code,
            "environment_conflict"
        );
        delete(&json!({"id":"copy","if_revision":1})).unwrap();
        assert_eq!(load("copy").unwrap_err().code, "environment_not_found");
    }

    #[test]
    fn plan_apply_read_back_and_restore_exactly() {
        let _temp = isolated();
        let p = preset(
            json!({"dark_mode":"on","font_scale":1.3,"animations":false,"airplane_mode":true,
            "app_locale":"de-DE","location":{"latitude":48.2,"longitude":16.37},"talkback":true}),
        );
        let mut device = Fake::new("emulator-5554", "35");
        let plan = plan_with(&mut device, &p, "dev.test.app").unwrap();
        assert!(
            device.writes().is_empty(),
            "a plan only reads: {:?}",
            device.writes()
        );
        assert_eq!(plan["unsupported"], json!([]));
        assert_eq!(plan["not_restorable"], json!(["location"]));
        let id = s(&plan, "plan_id").to_string();
        // Another device under a different serial cannot take this plan.
        let mut other = Fake::new("emulator-5556", "35");
        assert_eq!(
            apply_with(&mut other, &id, false).unwrap_err().code,
            "environment_target_changed"
        );
        assert!(other.writes().is_empty());
        let applied = apply_with(&mut device, &id, false).unwrap();
        let status: BTreeMap<String, String> = applied["results"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| (s(r, "field").to_string(), s(r, "status").to_string()))
            .collect();
        assert_eq!(status["dark_mode"], "applied");
        assert_eq!(status["talkback"], "applied");
        assert_eq!(status["location"], "applied_unverified");
        assert_eq!(
            device.settings["secure/enabled_accessibility_services"],
            "com.example.switch/.SwitchService:com.google.android.marvin.talkback/com.google.android.marvin.talkback.TalkBackService"
        );
        assert!(
            device
                .writes()
                .contains(&"emu geo fix 16.37 48.2".to_string())
        );
        assert_eq!(
            apply_with(&mut device, &id, false).unwrap_err().code,
            "environment_conflict",
            "a plan applies once"
        );
        let restored = restore_with(&mut device, s(&applied, "restore_id")).unwrap();
        let status: BTreeMap<String, String> = restored["results"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| (s(r, "field").to_string(), s(r, "status").to_string()))
            .collect();
        assert_eq!(status["location"], "not_restorable");
        assert!(
            status
                .iter()
                .filter(|(k, _)| *k != "location")
                .all(|(_, v)| v == "restored"),
            "{status:?}"
        );
        assert_eq!(device.night, "no");
        assert!(
            !device.settings.contains_key("system/font_scale"),
            "unset stays unset"
        );
        assert_eq!(
            device.settings["secure/enabled_accessibility_services"],
            "com.example.switch/.SwitchService"
        );
        assert_eq!(device.settings["global/airplane_mode_on"], "0");
        assert_eq!(device.locale.as_deref(), Some(""));
        assert_eq!(
            restore_with(&mut device, s(&applied, "restore_id"))
                .unwrap_err()
                .code,
            "environment_conflict"
        );
    }

    #[test]
    fn unsupported_settings_are_reported_and_need_consent_to_skip() {
        let _temp = isolated();
        let p = preset(
            json!({"airplane_mode":true,"app_locale":"fr","location":{"latitude":1,"longitude":2},"talkback":true,"animations":false}),
        );
        let mut phone = Fake::new("R5CT123", "29");
        phone.services = "priority=0\n  com.example.switch/.SwitchService\n".into();
        let plan = plan_with(&mut phone, &p, "dev.test.app").unwrap();
        assert_eq!(
            plan["unsupported"],
            json!(["airplane_mode", "app_locale", "location", "talkback"])
        );
        let id = s(&plan, "plan_id").to_string();
        assert_eq!(
            apply_with(&mut phone, &id, false).unwrap_err().code,
            "environment_unsupported"
        );
        assert!(phone.writes().is_empty());
        let value = apply_with(&mut phone, &id, true).unwrap();
        assert_eq!(value["applied"], json!(["animations"]));
        assert!(
            phone.writes().iter().all(|w| w.contains("_scale ")),
            "{:?}",
            phone.writes()
        );
    }

    #[test]
    fn apply_refuses_a_changed_device_or_state_and_reports_ignored_values() {
        let _temp = isolated();
        let p = preset(json!({"dark_mode":"on"}));
        let mut device = Fake::new("emulator-5554", "35");
        let plan = plan_with(&mut device, &p, "").unwrap();
        // A reflashed or different device behind the same serial.
        device
            .props
            .insert("ro.build.fingerprint".into(), "other".into());
        assert_eq!(
            apply_with(&mut device, s(&plan, "plan_id"), false)
                .unwrap_err()
                .code,
            "environment_target_changed"
        );
        device.props.insert(
            "ro.build.fingerprint".into(),
            "google/sdk/emu:15/X/1:user/release-keys".into(),
        );
        device.night = "auto".into();
        assert_eq!(
            apply_with(&mut device, s(&plan, "plan_id"), false)
                .unwrap_err()
                .code,
            "environment_conflict"
        );
        assert!(device.writes().is_empty());
        let plan = plan_with(&mut device, &p, "").unwrap();
        device.ignore_night = true;
        let error = apply_with(&mut device, s(&plan, "plan_id"), false).unwrap_err();
        assert_eq!(error.code, "setting_not_applied");
        assert!(error.details.unwrap()["restore_id"].is_string());
    }
}
