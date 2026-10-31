//! Typed Xcode, Simulator, Instruments and App Store operations on an SSH Mac.
use crate::{core::*, jobs, remote, toolchain as tc};
use serde_json::{Value, json};
use std::path::{Component, Path};
fn relative(value: &str) -> Result<&str> {
    let p = Path::new(value);
    if value.is_empty()
        || value.contains(['\n', '\0'])
        || p.is_absolute()
        || p.components().any(|c| matches!(c, Component::ParentDir))
    {
        return Err(Error::new(
            "invalid_path",
            "Use a non-empty path relative to the Mac workspace",
        ));
    }
    Ok(value)
}
fn command(config: &Value, args: &[String]) -> Result<Vec<String>> {
    remote::ssh(
        config,
        &format!(
            "cd -- {} && exec {}",
            shell_quote(s(config, "directory")),
            shell_join(args)
        ),
    )
}
fn rsync(config: &Value, source: &str, dest: &Path) -> Result<Vec<String>> {
    let rsync = tc::which("rsync").ok_or_else(|| Error::new("tool_missing", "Install rsync"))?;
    Ok(vec![
        rsync.to_string_lossy().into_owned(),
        "-a".into(),
        "--protect-args".into(),
        "-e".into(),
        "ssh -o BatchMode=yes -o ConnectTimeout=10".into(),
        format!(
            "{}:{}/{}",
            s(config, "host"),
            s(config, "directory"),
            relative(source)?
        ),
        dest.to_string_lossy().into_owned(),
    ])
}
fn sync(config: &Value, local: &str) -> Result<Vec<Value>> {
    let root = path(local, true, true)?;
    let rsync = tc::which("rsync").ok_or_else(|| Error::new("tool_missing", "Install rsync"))?;
    Ok(vec![
        json!({"argv":remote::ssh(config,&format!("mkdir -p -- {}",shell_quote(s(config,"directory"))))?}),
        json!({"argv":[rsync,"-az","--protect-args","--exclude=.git","--exclude=.env*","--exclude=.tooling","--exclude=.gradle","--exclude=node_modules","--exclude=build","--exclude=.dart_tool","--exclude=.tomacco","-e","ssh -o BatchMode=yes -o ConnectTimeout=10",format!("{}/",root.display()),format!("{}:{}/",s(config,"host"),s(config,"directory"))]}),
    ])
}
pub fn handle(op: &str, a: &Value) -> Result {
    let config = remote::workspace(s(a, "remote"))?;
    if op == "apple.devices" {
        let simulators = tc::run(
            &command(
                &config,
                &argv(&["xcrun", "simctl", "list", "devices", "available", "--json"]),
            )?,
            30,
            None,
            true,
        )?;
        let id = uuid::Uuid::new_v4().simple().to_string();
        let output = format!("/tmp/tomacco-devices-{id}.json");
        let shell = format!(
            "xcrun devicectl list devices --json-output {} >/dev/null && cat -- {}; result=$?; rm -f -- {}; exit $result",
            shell_quote(&output),
            shell_quote(&output),
            shell_quote(&output)
        );
        let devices = tc::run(&remote::ssh(&config, &shell)?, 30, None, false)?;
        return Ok(
            json!({"simulators":serde_json::from_str::<Value>(s(&simulators,"stdout"))?,"devices":if devices["exit_code"]==0{serde_json::from_str::<Value>(s(&devices,"stdout"))?}else{json!({"error":devices})}}),
        );
    }
    if op == "apple.project" {
        let project = relative(s(a, "project"))?;
        let kind = if project.ends_with(".xcworkspace") {
            "-workspace"
        } else if project.ends_with(".xcodeproj") {
            "-project"
        } else {
            return Err(Error::new(
                "invalid_project",
                "Select an .xcodeproj or .xcworkspace",
            ));
        };
        let result = tc::run(
            &command(
                &config,
                &argv(&["xcodebuild", kind, project, "-list", "-json"]),
            )?,
            60,
            None,
            true,
        )?;
        return serde_json::from_str(s(&result, "stdout")).map_err(Into::into);
    }
    let id = uuid::Uuid::new_v4().simple().to_string();
    let local = home()?.join("apple").join(&id);
    private_dir(&local)?;
    let output = format!(".tomacco/{id}");
    let mut steps = Vec::new();
    let mut cleanup = Vec::<Vec<String>>::new();
    let mut artifacts = Vec::<String>::new();
    let mut metadata = json!({"remote":s(a,"remote"),"remote_directory":s(&config,"directory"),"output":output,"local_directory":local});
    if !s(a, "path").is_empty() {
        steps.extend(sync(&config, s(a, "path"))?);
    }
    steps.push(json!({"argv":command(&config,&argv(&["mkdir","-p",&output]))?}));
    match op {
        "apple.build" | "apple.test" | "apple.archive" => {
            let project = relative(s(a, "project"))?;
            let kind = if project.ends_with(".xcworkspace") {
                "-workspace"
            } else if project.ends_with(".xcodeproj") {
                "-project"
            } else {
                return Err(Error::new(
                    "invalid_project",
                    "Select an Xcode project or workspace",
                ));
            };
            let mut args = argv(&[
                "xcodebuild",
                kind,
                project,
                "-scheme",
                s(a, "scheme"),
                "-configuration",
                if s(a, "configuration") == "release" {
                    "Release"
                } else {
                    "Debug"
                },
                "-derivedDataPath",
                &format!("{output}/DerivedData"),
            ]);
            if b(a, "allow_provisioning_updates") {
                args.push("-allowProvisioningUpdates".into());
            }
            let destination = s(a, "destination");
            if !destination.is_empty() {
                args.extend(argv(&["-destination", destination]));
            } else if op == "apple.archive" {
                args.extend(argv(&["-destination", "generic/platform=iOS"]));
            }
            if op == "apple.archive" {
                let archive = format!("{output}/App.xcarchive");
                metadata["archive"] = json!(archive);
                args.extend(argv(&["-archivePath", &archive, "archive"]));
            } else if op == "apple.test" {
                let result = format!("{output}/Tests.xcresult");
                let zip = format!("{output}/Tests.xcresult.zip");
                args.extend(argv(&["-resultBundlePath", &result, "test"]));
                for test in strings(&a["only_testing"]) {
                    args.push(format!("-only-testing:{test}"));
                }
                cleanup.push(command(
                    &config,
                    &argv(&[
                        "ditto",
                        "-c",
                        "-k",
                        "--sequesterRsrc",
                        "--keepParent",
                        &result,
                        &zip,
                    ]),
                )?);
                cleanup.push(rsync(&config, &zip, &local.join("Tests.xcresult.zip"))?);
                artifacts.push("Tests.xcresult.zip".into());
                metadata["result_bundle"] = json!(result);
            } else {
                args.push("build".into());
                metadata["products"] = json!(format!("{output}/DerivedData/Build/Products"));
            }
            if !s(a, "team_id").is_empty() {
                identifier(s(a, "team_id"), "Apple team ID", Some("[A-Z0-9]{10}"))?;
                args.push(format!("DEVELOPMENT_TEAM={}", s(a, "team_id")));
            }
            steps.push(json!({"argv":command(&config,&args)?}));
        }
        "apple.export" => {
            let archive = relative(s(a, "archive"))?;
            let options = path(s(a, "export_options"), false, true)?;
            let transfer =
                tc::which("rsync").ok_or_else(|| Error::new("tool_missing", "Install rsync"))?;
            let remote_options = format!("{output}/ExportOptions.plist");
            steps.push(json!({"argv":[transfer,"-a","--protect-args","-e","ssh -o BatchMode=yes -o ConnectTimeout=10",options,format!("{}:{}/{remote_options}",s(&config,"host"),s(&config,"directory"))]}));
            let export = format!("{output}/Export");
            let mut args = argv(&[
                "xcodebuild",
                "-exportArchive",
                "-archivePath",
                archive,
                "-exportOptionsPlist",
                &remote_options,
                "-exportPath",
                &export,
            ]);
            if b(a, "allow_provisioning_updates") {
                args.push("-allowProvisioningUpdates".into());
            }
            steps.push(json!({"argv":command(&config,&args)?}));
            let mut fetch = rsync(&config, &format!("{export}/"), &local)?;
            fetch.insert(1, "--include=*.ipa".into());
            fetch.insert(2, "--include=*.plist".into());
            fetch.insert(3, "--exclude=*".into());
            steps.push(json!({"argv":fetch}));
            artifacts.extend(["*.ipa".into(), "*.plist".into()]);
            metadata["export"] = json!(export);
        }
        "apple.simulator" => {
            let device = identifier(s(a, "device"), "Simulator UUID", Some("[A-Fa-f0-9-]{36}"))?;
            let action = s(a, "action");
            let mut args = argv(&["xcrun", "simctl"]);
            match action {
                "boot" => {
                    args.extend(argv(&["boot", device]));
                    steps.push(json!({"argv":command(&config,&args)?}));
                    args = argv(&["xcrun", "simctl", "bootstatus", device, "-b"]);
                }
                "shutdown" => args.extend(argv(&["shutdown", device])),
                "install" => args.extend(argv(&["install", device, relative(s(a, "app"))?])),
                "launch" | "terminate" => {
                    identifier(s(a, "bundle_id"), "application ID", None)?;
                    args.extend(argv(&[action, device, s(a, "bundle_id")]));
                }
                "openurl" => {
                    let url = s(a, "url");
                    if url.is_empty() || !url.contains(':') || url.contains(['\n', '\0']) {
                        return Err(Error::new(
                            "invalid_argument",
                            "openurl requires a URL with a scheme",
                        ));
                    }
                    args.extend(argv(&["openurl", device, url]));
                }
                "screenshot" => {
                    let file = format!("{output}/screen.png");
                    args.extend(argv(&["io", device, "screenshot", &file]));
                    cleanup.push(rsync(&config, &file, &local.join("screen.png"))?);
                    artifacts.push("screen.png".into());
                }
                _ => return Err(Error::new("invalid_argument", "Unknown Simulator action")),
            }
            steps.push(json!({"argv":command(&config,&args)?}));
        }
        "apple.device" => {
            let device = identifier(s(a, "device"), "device ID", None)?;
            let action = s(a, "action");
            let args = match action {
                "install" => argv(&[
                    "xcrun",
                    "devicectl",
                    "device",
                    "install",
                    "app",
                    "--device",
                    device,
                    relative(s(a, "app"))?,
                ]),
                "launch" => {
                    identifier(s(a, "bundle_id"), "application ID", None)?;
                    argv(&[
                        "xcrun",
                        "devicectl",
                        "device",
                        "process",
                        "launch",
                        "--device",
                        device,
                        "--terminate-existing",
                        s(a, "bundle_id"),
                    ])
                }
                "terminate" => {
                    if n(a, "pid") == 0 {
                        return Err(Error::new(
                            "invalid_argument",
                            "terminate requires a positive process ID",
                        ));
                    }
                    argv(&[
                        "xcrun",
                        "devicectl",
                        "device",
                        "process",
                        "terminate",
                        "--device",
                        device,
                        "--pid",
                        &n(a, "pid").to_string(),
                    ])
                }
                _ => return Err(Error::new("invalid_argument", "Unknown device action")),
            };
            steps.push(json!({"argv":command(&config,&args)?}));
        }
        "apple.profile" => {
            let trace = format!("{output}/Profile.trace");
            let zip = format!("{output}/Profile.trace.zip");
            let args = argv(&[
                "xcrun",
                "xctrace",
                "record",
                "--template",
                s(a, "template"),
                "--device",
                s(a, "device"),
                "--time-limit",
                &format!("{}s", n(a, "seconds")),
                "--output",
                &trace,
                "--attach",
                s(a, "process"),
            ]);
            steps.push(json!({"argv":command(&config,&args)?}));
            steps.push(json!({"argv":command(&config,&argv(&["xcrun","xctrace","export","--input",&trace,"--toc","--output",&format!("{output}/toc.xml")]))?}));
            steps.push(json!({"argv":command(&config,&argv(&["ditto","-c","-k","--keepParent",&trace,&zip]))?}));
            steps.push(json!({"argv":rsync(&config,&zip,&local.join("Profile.trace.zip"))?}));
            steps.push(
                json!({"argv":rsync(&config,&format!("{output}/toc.xml"),&local.join("toc.xml"))?}),
            );
            artifacts.extend(["Profile.trace.zip".into(), "toc.xml".into()]);
            metadata["trace"] = json!(trace);
        }
        "apple.store" => {
            let ipa = relative(s(a, "ipa"))?;
            identifier(
                s(a, "key_id"),
                "App Store Connect API key ID",
                Some("[A-Z0-9]{10}"),
            )?;
            identifier(
                s(a, "issuer"),
                "App Store Connect issuer",
                Some("[A-Fa-f0-9-]{36}"),
            )?;
            let key_directory = s(a, "key_directory");
            if !key_directory.starts_with('/') || key_directory.contains(['\0', '\n']) {
                return Err(Error::new(
                    "invalid_path",
                    "Select the absolute private-key directory on the Mac",
                ));
            }
            let args = argv(&[
                "env",
                &format!("API_PRIVATE_KEYS_DIR={key_directory}"),
                "xcrun",
                "altool",
                if s(a, "action") == "upload" {
                    "--upload-app"
                } else {
                    "--validate-app"
                },
                "-f",
                ipa,
                "-t",
                "ios",
                "--apiKey",
                s(a, "key_id"),
                "--apiIssuer",
                s(a, "issuer"),
                "--output-format",
                "json",
            ]);
            steps.push(json!({"argv":command(&config,&args)?}));
        }
        _ => return Err(Error::new("operation_not_found", op)),
    }
    atomic_json(&local.join("context.json"), &metadata)?;
    artifacts.push("context.json".into());
    let cleanup: Vec<_> = cleanup
        .into_iter()
        .map(|args| json!({"argv":args,"timeout":300}))
        .collect();
    jobs::submit(
        &format!("{} · {}", op, s(a, "remote")),
        json!({"steps":steps,"cleanup":cleanup,"cwd":local,"timeout":a["timeout"].as_u64().unwrap_or(3600),"exclusive":format!("mac:{}:{}",s(&config,"host"),s(&config,"directory")),"always_artifacts":artifacts,"metadata":metadata}),
    )
}
