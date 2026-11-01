//! A USB-connected iPhone from Linux: its log, apps, state and screen
//! through go-ios and libimobiledevice. Tested with stand-ins for those tools.
use crate::{core::*, toolchain as tc};
use serde_json::{Value, json};
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Command, Stdio};

pub const UDID: &str = "[A-Fa-f0-9-]{16,64}";
const BUNDLE: &str = r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+";

pub fn udid(value: &str) -> Result<&str> {
    identifier(value, "iPhone UDID", Some(UDID))
}

/// The apps go-ios lists for a device: its JSON array of Info.plist dictionaries.
pub fn apps(device: &str, all: bool) -> Result<Vec<Value>> {
    let mut args = argv(&[&tc::find("go-ios")?, "apps", &format!("--udid={device}")]);
    if all {
        args.push("--all".into());
    }
    let output = tc::run(&args, 30, None, true)?;
    app_listing(s(&output, "stdout"))
}

/// go-ios prints the array alone or after log lines; the last line that is a
/// JSON array is the listing.
fn app_listing(stdout: &str) -> Result<Vec<Value>> {
    if let Ok(Value::Array(rows)) = serde_json::from_str::<Value>(stdout.trim()) {
        return Ok(rows);
    }
    stdout
        .lines()
        .rev()
        .find_map(|line| match serde_json::from_str::<Value>(line.trim()) {
            Ok(Value::Array(rows)) => Some(rows),
            _ => None,
        })
        .ok_or_else(|| Error::new("invalid_response", "go-ios returned no application array"))
}

/// The installed app a bundle id names: exactly that id, or the one xtool
/// renamed with its `XTL-<team>.` prefix.
pub fn installed<'a>(apps: &'a [Value], bundle: &str) -> Result<&'a Value> {
    let prefix = regex::Regex::new(r"^XTL-[A-Z0-9]+\.").unwrap();
    let exact: Vec<_> = apps
        .iter()
        .filter(|v| s(v, "CFBundleIdentifier") == bundle)
        .collect();
    let candidates: Vec<_> = if exact.is_empty() {
        apps.iter()
            .filter(|v| prefix.replace(s(v, "CFBundleIdentifier"), "") == bundle)
            .collect()
    } else {
        exact
    };
    match candidates.as_slice() {
        [one] => Ok(one),
        [] => Err(Error::new(
            "app_not_installed",
            format!("No app with bundle id {bundle} is installed on the iPhone"),
        )
        .remedy("List the installed apps with ios.apps and pass an exact bundle_id")),
        _ => Err(Error::new(
            "application_id_ambiguous",
            format!("Several installed apps match {bundle}"),
        )
        .remedy("Pass the exact bundle_id from ios.apps")
        .details(json!({"matches":candidates.iter().map(|v| s(v,"CFBundleIdentifier")).collect::<Vec<_>>()}))),
    }
}

/// The process name an app's log lines carry: its executable.
fn executable(app: &Value) -> Result<String> {
    let name = s(app, "CFBundleExecutable");
    if name.is_empty()
        || name.len() > 255
        || name.contains(['/', '\n', '\0'])
        || name.starts_with('-')
    {
        return Err(Error::new(
            "invalid_response",
            "go-ios did not name the app's executable",
        ));
    }
    Ok(name.to_string())
}

/// The command `device.logs` runs for an iPhone, and the process it follows.
/// libimobiledevice's idevicesyslog filters by process itself; without it,
/// go-ios syslog runs under Tomacco's filter.
pub fn syslog_command(device: &str, filter: &str, bundle: &str) -> Result<(Vec<String>, String)> {
    udid(device)?;
    if filter.len() > 200 || filter.contains(['\n', '\0']) {
        return Err(Error::new(
            "invalid_argument",
            "filter is up to 200 characters on one line",
        ));
    }
    let process = if bundle.is_empty() {
        String::new()
    } else {
        identifier(bundle, "application ID", Some(BUNDLE))?;
        executable(installed(&apps(device, false)?, bundle)?)?
    };
    if let Some(tool) = tc::find_optional("idevicesyslog")? {
        let mut args = argv(&[&tool, "-u", device]);
        if !process.is_empty() {
            args.extend(argv(&["-p", &process]));
        }
        if !filter.is_empty() {
            args.extend(argv(&["-m", filter]));
        }
        return Ok((args, process));
    }
    let request =
        json!({"go_ios":tc::find("go-ios")?,"device":device,"process":process,"filter":filter});
    Ok((
        vec![
            own_executable()?.to_string_lossy().into_owned(),
            "_iphone_syslog".into(),
            request.to_string(),
        ],
        process,
    ))
}

/// The process of a syslog line, `Oct 31 12:00:00 iPhone MyApp(UIKitCore)[123] <Notice>: ...`.
fn line_process(line: &str) -> Option<&str> {
    let mut words = line.split_whitespace();
    // Month, day, time and device name come first.
    let word = words.nth(4)?;
    let end = word.find(['(', '['])?;
    Some(&word[..end])
}

/// A go-ios syslog line as text: `{"msg":"..."}` without --nojson, a parsed
/// entry with `process` and `message`, or the raw line.
fn syslog_text(line: &str) -> (String, Option<String>) {
    if let Ok(value) = serde_json::from_str::<Value>(line) {
        if let Some(message) = value["msg"].as_str() {
            return (message.to_string(), None);
        }
        if let Some(process) = value["process"].as_str() {
            return (line.to_string(), Some(process.to_string()));
        }
    }
    (line.to_string(), None)
}

fn keep(line: &str, process: &str, filter: &str) -> Option<String> {
    let (text, named) = syslog_text(line.trim_end());
    if !process.is_empty() {
        let actual = named.as_deref().or_else(|| line_process(&text));
        if actual != Some(process) {
            return None;
        }
    }
    if !filter.is_empty() && !text.contains(filter) {
        return None;
    }
    Some(text)
}

/// `_iphone_syslog`: go-ios syslog, keeping the lines of one process and
/// those containing the filter, until go-ios ends or the job stops it.
pub fn syslog_worker(request: &str) -> Result<()> {
    let request: Value = serde_json::from_str(request)?;
    let mut child = Command::new(s(&request, "go_ios"))
        .args([
            "syslog",
            &format!("--udid={}", udid(s(&request, "device"))?),
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .spawn()?;
    let stdout = child.stdout.take().unwrap();
    let mut out = std::io::stdout().lock();
    for line in BufReader::new(stdout).lines() {
        let Ok(line) = line else { break };
        if let Some(text) = keep(&line, s(&request, "process"), s(&request, "filter")) {
            if writeln!(out, "{text}").and_then(|_| out.flush()).is_err() {
                break;
            }
        }
    }
    let status = child.wait()?;
    if !status.success() {
        return Err(Error::new(
            "tool_failed",
            format!("go-ios syslog ended with {status}"),
        ));
    }
    Ok(())
}

/// `ios.signing`: the provisioning profiles installed on the phone, in an
/// xtool project's built apps and in named files, with the signing
/// certificates they list and warnings. Read by tools/ios_ipa_inspect.py;
/// nothing is verified cryptographically and xtool's login is not read.
pub fn signing(a: &Value) -> Result {
    let device = s(a, "device");
    if !device.is_empty() {
        udid(device)?;
    }
    let mut sources: Vec<(PathBuf, &str)> = Vec::new();
    let mut apps = Vec::new();
    if !s(a, "path").is_empty() {
        let root = path(s(a, "path"), true, true)?;
        let pattern = format!(
            "{}/xtool/*.app",
            glob::Pattern::escape(&root.to_string_lossy())
        );
        for app in glob_paths(&pattern)? {
            let profile = app.join("embedded.mobileprovision");
            if profile.is_file() {
                sources.push((profile, "project"));
            }
            apps.push(app);
        }
    }
    for file in strings(&a["profiles"]) {
        let file = path(&file, false, true)?;
        if !file.is_file() {
            return Err(Error::new(
                "invalid_path",
                format!("{} is not a file", file.display()),
            ));
        }
        sources.push((file, "file"));
    }
    let mut notes = Vec::new();
    let copied = if device.is_empty() {
        None
    } else if let Some(tool) = tc::find_optional("ideviceprovision")? {
        let directory = home()?
            .join("iphone-profiles")
            .join(uuid::Uuid::new_v4().simple().to_string());
        private_dir(&directory)?;
        let copy = tc::run(
            &argv(&[&tool, "-u", device, "copy", &directory.to_string_lossy()]),
            60,
            None,
            true,
        );
        if let Err(error) = copy {
            let _ = fs::remove_dir_all(&directory);
            return Err(error);
        }
        let pattern = format!(
            "{}/*.mobileprovision",
            glob::Pattern::escape(&directory.to_string_lossy())
        );
        for file in glob_paths(&pattern)? {
            sources.push((file, "device"));
        }
        Some(directory)
    } else {
        notes.push("Install libimobiledevice's ideviceprovision to list the profiles installed on the iPhone".to_string());
        None
    };
    let result = (|| -> Result {
        if sources.is_empty() && apps.is_empty() && copied.is_none() {
            return Err(Error::new(
                "invalid_argument",
                "Give device, path or profiles to read",
            ));
        }
        let mut args = vec![
            tc::find("python3")?,
            "-I".into(),
            crate::ios_emulator::research::script("ios_ipa_inspect.py")?
                .to_string_lossy()
                .into_owned(),
            "profiles".into(),
        ];
        if !device.is_empty() {
            args.extend(argv(&["--device", device]));
        }
        for app in &apps {
            args.extend(["--app".into(), app.to_string_lossy().into_owned()]);
        }
        args.extend(
            sources
                .iter()
                .map(|(file, _)| file.to_string_lossy().into_owned()),
        );
        let done = tc::run(&args, 120, None, false)?;
        let mut reply: Value = serde_json::from_str(s(&done, "stdout").trim()).map_err(|_| {
            Error::new("tool_failed", "The profile reader gave no answer").details(json!({
                "stderr": s(&done, "stderr").chars().take(400).collect::<String>()
            }))
        })?;
        if let Some(rows) = reply["profiles"].as_array_mut() {
            for (row, (_, source)) in rows.iter_mut().zip(&sources) {
                row["source"] = json!(source);
                if *source == "device" {
                    // The copy is removed below; the profile is on the phone.
                    row["path"] = Value::Null;
                }
            }
        }
        if let Some(warnings) = reply["warnings"].as_array_mut() {
            warnings.extend(notes.iter().map(|note| json!(note)));
        }
        let object = reply.as_object_mut().unwrap();
        object.remove("ok");
        object.insert("device".into(), json!(device));
        Ok(reply)
    })();
    if let Some(directory) = copied {
        let _ = fs::remove_dir_all(directory);
    }
    result
}

/// Output and exit code of a tool whose failure is an answer, not an error.
fn answer(args: &[String]) -> (bool, String) {
    match tc::run(args, 20, None, false) {
        Ok(done) => (
            done["exit_code"] == 0,
            format!("{}\n{}", s(&done, "stdout"), s(&done, "stderr")),
        ),
        Err(error) => (false, error.message),
    }
}

/// What `idevicepair validate` says about the pairing: paired, waiting for
/// Trust, refused, locked or not paired.
fn pairing_state(ok: bool, text: &str) -> &'static str {
    let lower = text.to_lowercase();
    if lower.contains("passcode") {
        "locked"
    } else if lower.contains("trust dialog") && lower.contains("denied") {
        "denied"
    } else if lower.contains("trust dialog") {
        "trust_pending"
    } else if ok && lower.contains("success") {
        "paired"
    } else if lower.contains("not paired") || lower.contains("pairing record") {
        "not_paired"
    } else {
        "unknown"
    }
}

/// go-ios `image list`: a JSON line with a list of mounted image
/// signatures, empty when none is mounted.
fn image_state(ok: bool, text: &str) -> &'static str {
    if !ok {
        return "unknown";
    }
    for line in text.lines().rev() {
        if let Ok(Value::Object(fields)) = serde_json::from_str::<Value>(line.trim()) {
            if let Some(Value::Array(list)) = fields
                .iter()
                .find(|(_, value)| value.is_array())
                .map(|(_, v)| v)
            {
                return if list.is_empty() {
                    "not_mounted"
                } else {
                    "mounted"
                };
            }
            if fields.values().any(|v| {
                v.as_str()
                    .is_some_and(|m| m.to_lowercase().contains("none"))
            }) {
                return "not_mounted";
            }
        }
    }
    "unknown"
}

fn major(version: &str) -> Option<u32> {
    version.trim().split('.').next()?.parse().ok()
}

/// The plain next steps for a phone in this state, most urgent first.
fn next_steps(state: &Value) -> Vec<Value> {
    let step = |text: &str, operation: Value| json!({"step":text,"operation":operation});
    let mut steps = Vec::new();
    if !b(state, "connected") {
        steps.push(step(
            "Connect the iPhone with a USB cable and unlock it.",
            Value::Null,
        ));
        return steps;
    }
    match s(state, "pairing") {
        "locked" => steps.push(step("Unlock the iPhone with its passcode, then check again.", Value::Null)),
        "trust_pending" => steps.push(step("On the iPhone, tap Trust and enter the passcode.", Value::Null)),
        "denied" => steps.push(step(
            "The iPhone refused this computer. Unplug it, plug it in again, tap Trust and enter the passcode.",
            Value::Null,
        )),
        "not_paired" | "unknown" => steps.push(step(
            "Pair this computer with the iPhone, then tap Trust on the iPhone and enter the passcode.",
            json!({"operation":"ios.prepare","arguments":{"action":"pair","device":state["device"]}}),
        )),
        _ => (),
    }
    if s(state, "pairing") != "paired" {
        return steps;
    }
    if state["developer_mode"] == false {
        steps.push(step(
            "Turn on Developer Mode on the iPhone: Settings, Privacy & Security, Developer Mode. The iPhone restarts; unlock it and confirm Turn On. If the switch is missing, install a development build once (project.run) and look again.",
            Value::Null,
        ));
    }
    if s(state, "developer_image") == "not_mounted" {
        steps.push(step(
            "Mount the developer image.",
            json!({"operation":"ios.prepare","arguments":{"action":"developer-image","device":state["device"]}}),
        ));
    }
    if major(s(state, "ios_version")).is_some_and(|v| v >= 17) {
        steps.push(step(
            "On iOS 17 and newer, keep a tunnel running while you use developer services.",
            json!({"operation":"ios.prepare","arguments":{"action":"tunnel","device":state["device"]}}),
        ));
    }
    steps
}

/// `ios.device_status`: whether a USB iPhone is connected, paired and
/// trusted, unlocked, in Developer Mode and has the developer image, with
/// plain next steps. Only libimobiledevice's simple connection is used
/// before the phone is paired, so the check itself asks nothing of it.
pub fn status(a: &Value) -> Result {
    let listing = crate::devices::listing("ios")?;
    let ids: Vec<String> = listing["devices"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|d| s(d, "id").to_string())
        .collect();
    let device = match (s(a, "device"), ids.as_slice()) {
        ("", [one]) => one.clone(),
        ("", []) => String::new(),
        ("", _) => {
            return Err(Error::new(
                "device_required",
                "Several iPhones are connected; pass device",
            )
            .details(json!({"devices":ids})));
        }
        (given, _) => udid(given)?.to_string(),
    };
    let connected = !device.is_empty() && ids.contains(&device);
    let mut state = json!({"device":device,"connected":connected,"pairing":"unknown","trusted":false,
        "unlocked":null,"developer_mode":null,"developer_image":"unknown","ios_version":null,
        "name":null,"tunnel":"not_checked","diagnostics":listing["diagnostics"]});
    if connected {
        if let Some(info) = tc::find_optional("ideviceinfo")? {
            for (key, field) in [("ProductVersion", "ios_version"), ("DeviceName", "name")] {
                let (ok, text) = answer(&argv(&[&info, "-s", "-u", &device, "-k", key]));
                let value = text.lines().next().unwrap_or("").trim();
                if ok && !value.is_empty() {
                    state[field] = json!(value);
                }
            }
        }
        let pair = tc::find("idevicepair")?;
        let (listed_ok, listed) = answer(&argv(&[&pair, "list"]));
        let recorded = listed_ok && listed.lines().any(|line| line.trim() == device);
        state["pairing"] = json!(if recorded {
            let (ok, text) = answer(&argv(&[&pair, "-u", &device, "validate"]));
            pairing_state(ok, &text)
        } else {
            "not_paired"
        });
        let paired = s(&state, "pairing") == "paired";
        state["trusted"] = json!(paired);
        state["unlocked"] = match s(&state, "pairing") {
            "locked" => json!(false),
            "paired" => json!(true),
            _ => Value::Null,
        };
        if paired {
            if let Some(info) = tc::find_optional("ideviceinfo")? {
                let (ok, text) = answer(&argv(&[
                    &info,
                    "-u",
                    &device,
                    "-q",
                    "com.apple.security.mac.amfi",
                    "-k",
                    "DeveloperModeStatus",
                ]));
                state["developer_mode"] = match (ok, text.lines().next().unwrap_or("").trim()) {
                    (true, "true") => json!(true),
                    (true, "false") => json!(false),
                    // Before iOS 16 there is no Developer Mode to turn on.
                    _ if major(s(&state, "ios_version")).is_some_and(|v| v < 16) => {
                        json!("not_applicable")
                    }
                    _ => Value::Null,
                };
            }
            if let Some(go) = tc::find_optional("go-ios")? {
                let (ok, text) =
                    answer(&argv(&[&go, "image", "list", &format!("--udid={device}")]));
                state["developer_image"] = json!(image_state(ok, &text));
            }
        }
    }
    let steps = next_steps(&state);
    state["ready"] = json!(
        connected
            && s(&state, "pairing") == "paired"
            && (state["developer_mode"] == true || state["developer_mode"] == "not_applicable")
            && s(&state, "developer_image") == "mounted"
    );
    state["next_steps"] = json!(steps);
    Ok(state)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pairing_and_image_answers_are_read() {
        let device = "00008110-0011223344556677";
        assert_eq!(
            pairing_state(
                true,
                &format!("SUCCESS: Validated pairing with device {device}")
            ),
            "paired"
        );
        assert_eq!(
            pairing_state(
                false,
                &format!(
                    "ERROR: Could not validate with device {device} because a passcode is set. Please enter the passcode on the device and retry."
                )
            ),
            "locked"
        );
        assert_eq!(
            pairing_state(
                false,
                &format!(
                    "ERROR: Please accept the trust dialog on the screen of device {device}, then attempt to pair again."
                )
            ),
            "trust_pending"
        );
        assert_eq!(
            pairing_state(
                false,
                &format!("ERROR: Device {device} said that the user denied the trust dialog.")
            ),
            "denied"
        );
        assert_eq!(
            pairing_state(
                false,
                &format!("ERROR: Device {device} is not paired with this host")
            ),
            "not_paired"
        );
        assert_eq!(pairing_state(false, "something else"), "unknown");
        assert_eq!(
            image_state(true, "{\"msg\":\"x\"}\n{\"images\":[\"abc\"]}"),
            "mounted"
        );
        assert_eq!(image_state(true, "{\"images\":[]}"), "not_mounted");
        assert_eq!(
            image_state(true, "{\"msg\":\"none mounted\"}"),
            "not_mounted"
        );
        assert_eq!(image_state(false, "{\"images\":[\"abc\"]}"), "unknown");
        assert_eq!(image_state(true, "plain text"), "unknown");
    }

    #[test]
    fn next_steps_follow_the_first_thing_missing() {
        let device = "00008110-0011223344556677";
        let steps = next_steps(&json!({"device":device,"connected":false}));
        assert!(s(&steps[0], "step").contains("USB cable"));
        let steps = next_steps(&json!({"device":device,"connected":true,"pairing":"not_paired"}));
        assert_eq!(steps.len(), 1);
        assert_eq!(steps[0]["operation"]["arguments"]["action"], "pair");
        let steps = next_steps(&json!({"device":device,"connected":true,"pairing":"locked"}));
        assert!(s(&steps[0], "step").contains("Unlock"));
        let steps = next_steps(&json!({"device":device,"connected":true,"pairing":"paired",
            "developer_mode":false,"developer_image":"not_mounted","ios_version":"18.1"}));
        let texts: Vec<_> = steps.iter().map(|v| s(v, "step")).collect();
        assert!(texts[0].contains("Developer Mode"));
        assert_eq!(
            steps[1]["operation"]["arguments"]["action"],
            "developer-image"
        );
        assert_eq!(steps[2]["operation"]["arguments"]["action"], "tunnel");
        let ready = next_steps(&json!({"device":device,"connected":true,"pairing":"paired",
            "developer_mode":true,"developer_image":"mounted","ios_version":"16.7"}));
        assert!(ready.is_empty());
    }

    #[test]
    fn syslog_lines_are_kept_by_process_and_text() {
        let raw = "Oct 31 12:00:00 iPhone MyApp(UIKitCore)[123] <Notice>: tapped Save";
        assert_eq!(line_process(raw), Some("MyApp"));
        assert_eq!(
            line_process("Oct 31 12:00:01 iPhone SpringBoard[55] <Error>: x"),
            Some("SpringBoard")
        );
        assert_eq!(line_process("short line"), None);
        assert_eq!(keep(raw, "MyApp", ""), Some(raw.to_string()));
        assert_eq!(keep(raw, "MyApp", "Save"), Some(raw.to_string()));
        assert_eq!(keep(raw, "MyApp", "Load"), None);
        assert_eq!(keep(raw, "Other", ""), None);
        assert_eq!(keep(raw, "", ""), Some(raw.to_string()));
        let wrapped = json!({"msg":raw}).to_string();
        assert_eq!(keep(&wrapped, "MyApp", ""), Some(raw.to_string()));
        let parsed = r#"{"process":"MyApp","pid":123,"message":"hello"}"#;
        assert_eq!(keep(parsed, "MyApp", "hello"), Some(parsed.to_string()));
        assert_eq!(keep(parsed, "SpringBoard", ""), None);
    }

    #[test]
    fn an_installed_app_is_found_by_its_id_or_xtool_prefix() {
        let rows = app_listing("time=x level=info msg=y\n[{\"CFBundleIdentifier\":\"XTL-AB12.dev.example.app\",\"CFBundleExecutable\":\"Counter\"},{\"CFBundleIdentifier\":\"com.apple.Maps\",\"CFBundleExecutable\":\"Maps\"}]\n").unwrap();
        assert_eq!(rows.len(), 2);
        let app = installed(&rows, "dev.example.app").unwrap();
        assert_eq!(executable(app).unwrap(), "Counter");
        assert_eq!(
            s(
                installed(&rows, "com.apple.Maps").unwrap(),
                "CFBundleExecutable"
            ),
            "Maps"
        );
        assert_eq!(
            installed(&rows, "dev.example.other").unwrap_err().code,
            "app_not_installed"
        );
        let twice = [
            rows[0].clone(),
            json!({"CFBundleIdentifier":"XTL-CD34.dev.example.app"}),
        ];
        assert_eq!(
            installed(&twice, "dev.example.app").unwrap_err().code,
            "application_id_ambiguous"
        );
        assert!(executable(&json!({"CFBundleExecutable":"../x"})).is_err());
        assert!(app_listing("no listing").is_err());
    }
}
