//! Mac-free Swift/iPhone setup. Credentials stay in xtool's interactive login.
use crate::{core::*, devices, jobs, projects, toolchain as tc};
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::process::CommandExt,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
};

const WDA_VERSION: &str = "16.13.6";
const WDA_SHA256: &str = "152fb7feedaa2b33a9fd9717d8ad62cfebfde820cc391f378e200e9e77361c6f";
const WDA_URL: &str = "https://github.com/appium/WebDriverAgent/releases/download/v16.13.6/WebDriverAgentRunner-Runner.zip";

/// xtool dev run builds, signs and installs, but does not launch the app.
/// Read the packed ID and resolve xtool's signing prefix from the device inventory.
pub fn launch_installed(a: &Value) -> Result {
    let root = path(s(a, "path"), true, true)?;
    let device = identifier(s(a, "device"), "iPhone UDID", Some("[A-Fa-f0-9-]{16,64}"))?;
    let mut bundle = s(a, "bundle_id").to_owned();
    if bundle.is_empty() {
        let products = glob_paths(&format!(
            "{}/xtool/*.app/Info.plist",
            glob::Pattern::escape(&root.to_string_lossy())
        ))?;
        if products.len() != 1 {
            return Err(Error::new(
                "application_id_required",
                "Select the installed app with project.run bundle_id; the xtool output contains no unique app",
            ));
        }
        let info = products[0].canonicalize()?;
        if !info.starts_with(&root) || fs::metadata(&info)?.len() > 1024 * 1024 {
            return Err(Error::new(
                "invalid_artifact",
                "The app manifest is outside the project or too large",
            ));
        }
        let xml = fs::read_to_string(info)?;
        let doc = roxmltree::Document::parse_with_options(
            &xml,
            roxmltree::ParsingOptions {
                allow_dtd: true,
                ..Default::default()
            },
        )
        .map_err(|e| {
            Error::new(
                "invalid_artifact",
                format!("Cannot read xtool's XML app manifest: {e}"),
            )
        })?;
        bundle = doc
            .root_element()
            .first_element_child()
            .into_iter()
            .flat_map(|n| n.children())
            .find(|n| n.has_tag_name("key") && n.text() == Some("CFBundleIdentifier"))
            .and_then(|n| n.next_siblings().skip(1).find(|n| n.is_element()))
            .filter(|n| n.has_tag_name("string"))
            .and_then(|n| n.text())
            .unwrap_or("")
            .to_owned();
    }
    identifier(
        &bundle,
        "application ID",
        Some(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+"),
    )?;
    let apps = handle("ios.apps", &json!({"device":device}))?;
    let prefix = regex::Regex::new(r"^XTL-[A-Z0-9]+\.").unwrap();
    let candidates: Vec<_> = apps["apps"]
        .as_array()
        .ok_or_else(|| Error::new("invalid_response", "go-ios returned no application array"))?
        .iter()
        .map(|v| s(v, "CFBundleIdentifier"))
        .filter(|id| *id == bundle || prefix.replace(id, "") == bundle)
        .collect();
    if candidates.len() != 1 {
        return Err(Error::new("application_id_ambiguous", "The installed application could not be uniquely identified; inspect ios.apps and pass its exact bundle ID to project.run").details(json!({"bundle_id":bundle,"matches":candidates})));
    }
    let installed = candidates[0];
    identifier(
        installed,
        "installed application ID",
        Some(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+"),
    )?;
    let output = tc::run(
        &argv(&[
            &tc::find("go-ios")?,
            "launch",
            installed,
            &format!("--udid={device}"),
        ]),
        60,
        None,
        true,
    )?;
    Ok(
        json!({"device":device,"bundle_id":installed,"launched":true,"mac_required":false,"output":output}),
    )
}

/// A go-ios crash-report pattern: a glob over report names, never a way up.
fn crash_pattern(value: &str) -> Result<&str> {
    let pattern = if value.is_empty() { "*" } else { value };
    if pattern.len() > 128
        || pattern.contains("..")
        || pattern.starts_with(['/', '-'])
        || !pattern
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._-*?/".contains(&c))
    {
        return Err(Error::new(
            "invalid_argument",
            "pattern is a report name glob of up to 128 ASCII letters, digits, '.', '_', '-', '*', '?' or '/', without '..'",
        ));
    }
    Ok(pattern)
}

/// The report names `ios crash ls` printed: its JSON line `{"files":[...]}`,
/// or else one name per line.
fn crash_listing(stdout: &str) -> Vec<String> {
    for line in stdout.lines().rev() {
        if let Ok(value) = serde_json::from_str::<Value>(line.trim()) {
            if let Some(files) = value["files"].as_array() {
                return files
                    .iter()
                    .filter_map(|file| file.as_str().map(str::to_string))
                    .collect();
            }
        }
    }
    stdout
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty() && !line.starts_with('{'))
        .map(str::to_string)
        .collect()
}

/// Every regular file under a directory, by path, with its size and mtime;
/// links are not followed.
fn files_under(directory: &Path) -> std::collections::BTreeMap<PathBuf, (u64, i64)> {
    use std::os::unix::fs::MetadataExt;
    let mut found = std::collections::BTreeMap::new();
    let mut pending = vec![directory.to_path_buf()];
    while let Some(folder) = pending.pop() {
        for entry in fs::read_dir(&folder).into_iter().flatten().flatten() {
            let Ok(meta) = fs::symlink_metadata(entry.path()) else {
                continue;
            };
            if meta.is_dir() {
                pending.push(entry.path());
            } else if meta.is_file() {
                found.insert(entry.path(), (meta.len(), meta.mtime()));
            }
        }
    }
    found
}

/// The files a copy added or replaced in a directory, with size and SHA-256.
fn copied_reports(
    before: &std::collections::BTreeMap<PathBuf, (u64, i64)>,
    after: &std::collections::BTreeMap<PathBuf, (u64, i64)>,
) -> Vec<Value> {
    after
        .iter()
        .filter(|(path, stamp)| before.get(*path) != Some(*stamp))
        .map(|(path, (bytes, _))| {
            let sha256 = fs::read(path).ok().map(|data| digest(&data));
            json!({"name":path.file_name().map(|name| name.to_string_lossy().into_owned()),
                "path":path,"bytes":bytes,"sha256":sha256})
        })
        .collect()
}

/// Crash reports of a USB-connected iPhone through go-ios: the names that
/// match `pattern`, and with `directory` the files copied there.
fn crash_reports(a: &Value) -> Result {
    let device = identifier(s(a, "device"), "iPhone UDID", Some("[A-Fa-f0-9-]{16,64}"))?;
    let pattern = crash_pattern(s(a, "pattern"))?;
    let go_ios = tc::find("go-ios")?;
    let udid = format!("--udid={device}");
    let listed = tc::run(
        &argv(&[&go_ios, "crash", "ls", pattern, &udid]),
        60,
        None,
        true,
    )?;
    let names = crash_listing(s(&listed, "stdout"));
    let mut result =
        json!({"device":device,"pattern":pattern,"reports":names,"directory":null,"copied":[]});
    if s(a, "directory").is_empty() {
        return Ok(result);
    }
    let directory = path(s(a, "directory"), false, false)?;
    if directory.exists() && !directory.is_dir() {
        return Err(Error::new(
            "invalid_path",
            "directory names a file; name a directory for the reports",
        ));
    }
    private_dir(&directory)?;
    let before = files_under(&directory);
    tc::run(
        &argv(&[
            &go_ios,
            "crash",
            "cp",
            pattern,
            &directory.to_string_lossy(),
            &udid,
        ]),
        300,
        None,
        true,
    )?;
    result["directory"] = json!(directory);
    result["copied"] = json!(copied_reports(&before, &files_under(&directory)));
    Ok(result)
}

fn check(name: &str, ready: bool, detail: impl Into<String>, remedy: &str) -> Value {
    json!({"name":name,"ready":ready,"detail":detail.into(),"remedy":if ready{""}else{remedy}})
}
fn swift_identity(name: &str, executable: Option<PathBuf>) -> Value {
    let Some(executable) = executable else {
        return json!({"name":name,"ready":false,"error":"Not found on managed PATH"});
    };
    let resolved = executable.canonicalize().ok();
    let mut identity = json!({"name":name,"path":executable,"resolved":resolved,"ready":false});
    match tc::run(
        &argv(&[&executable.to_string_lossy(), "--version"]),
        10,
        None,
        false,
    ) {
        Ok(output) => {
            let text = format!("{}\n{}", s(&output, "stdout"), s(&output, "stderr"));
            let version = text.lines().find(|line| line.contains("Swift version "));
            identity["ready"] =
                json!(output["exit_code"] == 0 && resolved.is_some() && version.is_some());
            identity["exit_code"] = output["exit_code"].clone();
            identity["version"] =
                json!(version.map(|line| line.trim().chars().take(512).collect::<String>()));
            identity["output"] = json!(text.trim().chars().take(4096).collect::<String>());
        }
        Err(error) => identity["error"] = error.value(),
    }
    identity
}
fn swift_toolchain() -> Result {
    let config = tc::settings()?;
    let selected = config["executables"]["swift"]
        .as_str()
        .map(PathBuf::from)
        .or(tc::find_optional("swift")?.map(PathBuf::from));
    let resolved = selected.as_ref().and_then(|path| path.canonicalize().ok());
    let swift = swift_identity("swift", tc::managed_which("swift")?);
    let swiftc = swift_identity("swiftc", tc::managed_which("swiftc")?);
    let ready = b(&swift, "ready")
        && b(&swiftc, "ready")
        && resolved
            .as_ref()
            .is_some_and(|path| path == Path::new(s(&swift, "resolved")))
        && Path::new(s(&swift, "resolved")).parent() == Path::new(s(&swiftc, "resolved")).parent()
        && swift["version"] == swiftc["version"];
    Ok(
        json!({"ready":ready,"selected":selected,"resolved":resolved,"swift":swift,"swiftc":swiftc,"sdk_compatibility":"not_checked"}),
    )
}
pub fn handle(op: &str, a: &Value) -> Result {
    match op {
        "ios.doctor" => doctor(a),
        "ios.crash_reports" => crash_reports(a),
        "ios.apps" => {
            identifier(s(a, "device"), "iPhone UDID", Some("[A-Fa-f0-9-]{16,64}"))?;
            let value = tc::run(
                &argv(&[
                    &tc::find("go-ios")?,
                    "apps",
                    &format!("--udid={}", s(a, "device")),
                ]),
                30,
                None,
                true,
            )?;
            Ok(
                json!({"device":a["device"],"apps":serde_json::from_str::<Value>(s(&value,"stdout"))?}),
            )
        }
        "ios.prepare" => {
            let action = s(a, "action");
            if action == "login" {
                let terminal = tc::which("xdg-terminal-exec").ok_or_else(|| {
                    Error::new("terminal_missing", "Run xtool auth login in your terminal")
                })?;
                let mut command = Command::new(terminal);
                command
                    .args(["--", &tc::find("xtool")?, "auth", "login"])
                    .stdin(Stdio::null())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null());
                // The terminal owns the interactive secret prompts, never a job log.
                unsafe {
                    command.pre_exec(|| {
                        if libc::setsid() < 0 {
                            Err(std::io::Error::last_os_error())
                        } else {
                            Ok(())
                        }
                    });
                }
                let mut child = command.spawn()?;
                thread::spawn(move || {
                    let _ = child.wait();
                });
                return Ok(
                    json!({"terminal_opened":true,"authenticated":false,"next":"ios.doctor"}),
                );
            }
            let device = identifier(s(a, "device"), "iPhone UDID", Some("[A-Fa-f0-9-]{16,64}"))?;
            let args = match action {
                "pair" => argv(&[&tc::find("idevicepair")?, "-u", device, "pair"]),
                "developer-image" => argv(&[
                    &tc::find("go-ios")?,
                    "image",
                    "auto",
                    &format!("--udid={device}"),
                ]),
                "tunnel" => argv(&[
                    &tc::find("go-ios")?,
                    "tunnel",
                    "start",
                    "--userspace",
                    &format!("--udid={device}"),
                ]),
                _ => {
                    return Err(Error::new(
                        "invalid_argument",
                        "Unknown iPhone setup action",
                    ));
                }
            };
            jobs::submit(
                &format!("iPhone · {action}"),
                json!({"steps":[{"argv":args}],"cwd":home()?,"timeout":if action=="tunnel"{86400}else{300},"exclusive":format!("iphone-setup:{action}:{device}")}),
            )
        }
        "ios.wda_prepare" => {
            if !s(a, "device").is_empty() {
                identifier(s(a, "device"), "iPhone UDID", Some("[A-Fa-f0-9-]{16,64}"))?;
                tc::find("xtool")?;
            }
            tc::find("curl")?;
            if tc::which("unzip").is_none() {
                return Err(Error::new(
                    "tool_missing",
                    "Install unzip to extract the verified iPhone runner",
                ));
            }
            let dir = home()?
                .join("iphone-setup")
                .join(uuid::Uuid::new_v4().simple().to_string());
            private_dir(&dir)?;
            atomic_json(&dir.join("request.json"), a)?;
            jobs::submit(
                "Prepare iPhone automation on Linux",
                json!({"steps":[{"argv":[crate::core::own_executable()?,"_iphone_setup",dir.join("request.json")]}],"cwd":dir,"timeout":900,"always_artifacts":["report.json"],"metadata":{"version":WDA_VERSION,"mac_required":false}}),
            )
        }
        _ => Err(Error::new("operation_not_found", op)),
    }
}
fn doctor(a: &Value) -> Result {
    let mut checks = Vec::new();
    for (tool, remedy) in [
        (
            "swift",
            "Install the Swift toolchain matching your Apple SDK",
        ),
        ("xtool", "Install xtool for Linux"),
        ("go-ios", "Install go-ios for iPhone developer services"),
        ("iproxy", "Install libusbmuxd"),
        ("idevicepair", "Install libimobiledevice"),
    ] {
        let found = tc::find_optional(tool)?;
        checks.push(check(
            tool,
            found.is_some(),
            found.unwrap_or_default(),
            remedy,
        ));
    }
    let swift_toolchain = swift_toolchain()?;
    checks.push(check(
        "swift_toolchain",
        b(&swift_toolchain, "ready"),
        if b(&swift_toolchain, "ready") {
            "Managed PATH selects the chosen Swift toolchain for swift and swiftc"
        } else {
            "Managed PATH could not confirm the chosen Swift toolchain for swift and swiftc"
        },
        "Configure the Swift toolchain's swift executable and ensure its bin directory contains swift and swiftc",
    ));
    let mut sdk = false;
    let mut auth = false;
    if let Some(xtool) = tc::find_optional("xtool")? {
        if let Ok(value) = tc::run(&argv(&[&xtool, "sdk", "status"]), 20, None, false) {
            let text = s(&value, "stdout").trim();
            sdk = value["exit_code"] == 0
                && !text.is_empty()
                && !text.to_ascii_lowercase().contains("not installed");
            checks.push(check("apple_sdk",sdk,text,"Download Xcode.xip from Apple in your browser, then use ios.sdk_install. No Mac is required to extract it."));
        } else {
            checks.push(check(
                "apple_sdk",
                false,
                "SDK status could not be read",
                "Run xtool sdk status in a terminal",
            ));
        }
        if let Ok(value) = tc::run(&argv(&[&xtool, "auth", "status"]), 20, None, false) {
            // Auth status may include account/token fields. Never return or log it.
            auth = value["exit_code"] == 0 && s(&value, "stdout").starts_with("Logged in.");
        }
    } else {
        checks.push(check(
            "apple_sdk",
            false,
            "xtool is unavailable",
            "Install xtool, then import the Apple SDK",
        ));
    }
    checks.push(check(
        "apple_auth",
        auth,
        if auth { "Signed in" } else { "Not signed in" },
        "Use ios.prepare action=login; enter credentials only in the local terminal",
    ));
    let socket =
        std::env::var("USBMUXD_SOCKET_ADDRESS").unwrap_or_else(|_| "/var/run/usbmuxd".into());
    let usb = Path::new(&socket).exists() || socket.contains(':');
    checks.push(check(
        "usbmuxd",
        usb,
        &socket,
        "Connect and unlock the iPhone; ensure the distribution's usbmuxd service is available",
    ));
    let devices = devices::listing("ios")?;
    let connected = devices["devices"]
        .as_array()
        .unwrap()
        .iter()
        .any(|v| s(a, "device").is_empty() || v["id"] == a["device"]);
    checks.push(check(
        "iphone",
        connected,
        if connected {
            "Device discovered"
        } else {
            "No selected iPhone connected"
        },
        "Connect an iPhone/iPad by USB, unlock it and confirm Trust This Computer",
    ));
    let project = if s(a, "path").is_empty() {
        Value::Null
    } else {
        let p = projects::inspect(s(a, "path"))?;
        let supported = p["kinds"].as_array().unwrap().iter().any(|k| k == "xtool");
        checks.push(check("project",supported,s(&p,"name"),"Mac-free iOS compilation uses SwiftPM/xtool projects. For an Xcode project call project.import_xcode; for Flutter call ios.flutter_build; linux_toolkit says what those need."));
        p
    };
    Ok(
        json!({"mac_required":false,"route":"linux-swiftpm-physical-iphone","local_modern_ios_emulator":false,"sdk_ready":sdk,"signing_ready":auth,"device_ready":connected,"ready":checks.iter().all(|c|b(c,"ready")),"checks":checks,"swift_toolchain":swift_toolchain,"devices":devices,"project":project,"linux_toolkit":crate::apple_dev::status().unwrap_or_else(|error|json!({"installed":false,"error":error.value()})),"sdk_download":"https://developer.apple.com/download/all/?q=Xcode","runner":{"version":WDA_VERSION,"sha256":WDA_SHA256,"url":WDA_URL}}),
    )
}
pub fn worker(file: &str) -> Result<()> {
    let file = path(file, false, true)?;
    let dir = file.parent().unwrap();
    let a = read_json(&file, None)?;
    let result = (|| -> Result {
        // Kept between runs: an interrupted download continues, and a
        // verified one is reused. It is used only once it matches the pin.
        let archive =
            crate::download::directory()?.join(format!("WebDriverAgentRunner-{WDA_VERSION}.zip"));
        crate::download::fetch(&crate::download::Request {
            url: WDA_URL,
            destination: &archive,
            checksum: crate::download::Checksum::Sha256(WDA_SHA256.into()),
            max_bytes: 20 * 1024 * 1024,
            progress: false,
            allow_http: false,
            curl: &tc::find("curl")?,
        })?;
        let extracted = dir.join("runner");
        private_dir(&extracted)?;
        tc::run(
            &argv(&[
                &tc::which("unzip").unwrap().to_string_lossy(),
                "-q",
                &archive.to_string_lossy(),
                "-d",
                &extracted.to_string_lossy(),
            ]),
            30,
            None,
            true,
        )?;
        let apps = glob_paths(&format!(
            "{}/*.app",
            glob::Pattern::escape(&extracted.to_string_lossy())
        ))?;
        if apps.len() != 1 {
            return Err(Error::new(
                "invalid_artifact",
                "The pinned runner package does not contain exactly one app",
            ));
        }
        let mut installed = false;
        if !s(&a, "device").is_empty() {
            tc::run(
                &argv(&[
                    &tc::find("xtool")?,
                    "install",
                    "--udid",
                    s(&a, "device"),
                    &apps[0].to_string_lossy(),
                ]),
                600,
                None,
                true,
            )?;
            installed = true;
        }
        Ok(
            json!({"version":WDA_VERSION,"sha256":WDA_SHA256,"app":apps[0],"installed":installed,"device":a["device"],"runner_bundle_id":"com.facebook.WebDriverAgentRunner.xctrunner","mac_required":false,"note":"xtool may remap the bundle ID for your signing team; use ios.apps to select the installed runner identifier."}),
        )
    })();
    atomic_json(
        &dir.join("report.json"),
        &match &result {
            Ok(value) => json!({"passed":true,"result":value}),
            Err(error) => json!({"passed":false,"error":error.value()}),
        },
    )?;
    result.map(|_| ())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crash_patterns_are_globs_over_names_only() {
        assert_eq!(crash_pattern("").unwrap(), "*");
        for good in ["MyApp*", "MyApp-2026-10-31*.ips", "Retired/*", "?yApp*"] {
            assert_eq!(crash_pattern(good).unwrap(), good);
        }
        for bad in [
            "../x",
            "/etc/*",
            "-rf",
            "a b",
            "a;b",
            "ä*",
            &"a".repeat(129),
        ] {
            assert!(crash_pattern(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn the_listing_is_read_from_the_json_line_or_line_by_line() {
        let stdout = "time=\"x\" level=info msg=\"connecting\"\n{\"files\":[\"MyApp-2026-10-31-120000.ips\",\"Retired/MyApp-old.ips\"],\"length\":2}\n";
        assert_eq!(
            crash_listing(stdout),
            vec!["MyApp-2026-10-31-120000.ips", "Retired/MyApp-old.ips"]
        );
        assert_eq!(
            crash_listing("{\"files\":[],\"length\":0}"),
            Vec::<String>::new()
        );
        assert_eq!(crash_listing("a.ips\n\nb.ips\n"), vec!["a.ips", "b.ips"]);
    }

    #[test]
    fn copied_reports_are_the_new_or_changed_files_with_digests() {
        let root = tempfile::tempdir().unwrap();
        fs::write(root.path().join("old.ips"), b"old").unwrap();
        let before = files_under(root.path());
        fs::create_dir(root.path().join("Retired")).unwrap();
        fs::write(root.path().join("Retired/new.ips"), b"new").unwrap();
        std::os::unix::fs::symlink("/etc/hostname", root.path().join("link.ips")).unwrap();
        let rows = copied_reports(&before, &files_under(root.path()));
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["name"], "new.ips");
        assert_eq!(rows[0]["path"], json!(root.path().join("Retired/new.ips")));
        assert_eq!(rows[0]["bytes"], 3);
        assert_eq!(rows[0]["sha256"], digest(b"new"));
    }
}
