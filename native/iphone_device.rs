//! A USB-connected iPhone from Linux: its log, apps, state and screen
//! through go-ios and libimobiledevice. Tested with stand-ins for those tools.
use crate::{core::*, toolchain as tc};
use serde_json::{Value, json};
use std::io::{BufRead, BufReader, Write};
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

#[cfg(test)]
mod tests {
    use super::*;

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
