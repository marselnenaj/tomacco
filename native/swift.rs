use crate::{core::*, jobs, toolchain as tc};
use serde_json::{Value, json};
use std::path::PathBuf;
fn selected(sdk: &str) -> Result<String> {
    let config = tc::settings()?;
    let selected = if sdk.is_empty() {
        config["swift_android_sdk"]
            .as_str()
            .unwrap_or("swift-6.4.0-RELEASE_android")
    } else {
        sdk
    };
    identifier(selected, "Swift SDK", None)?;
    Ok(selected.into())
}
fn configuration(sdk: &str) -> Result {
    let selected = selected(sdk)?;
    let value = tc::run(
        &argv(&[
            &tc::find("swift")?,
            "sdk",
            "configure",
            "--show-configuration",
            &selected,
            "aarch64-unknown-linux-android26",
        ]),
        15,
        None,
        true,
    )?;
    let mut properties = json!({});
    for line in s(&value, "stdout").lines() {
        if let Some((k, v)) = line.split_once(": ") {
            properties[k] = json!(v);
        }
    }
    let root = PathBuf::from(properties["sdkRootPath"].as_str().unwrap_or("/nonexistent"));
    Ok(
        json!({"sdk":selected,"sysroot":root,"configured":root.join("usr/include").is_dir(),"setup_script":root.parent().unwrap_or(&root).join("scripts/setup-android-sdk.sh")}),
    )
}
pub fn plan(a: &Value) -> Result {
    let root = path(s(a, "path"), true, true)?;
    if !root.join("Package.swift").is_file() {
        return Err(Error::new(
            "package_missing",
            format!("No Package.swift found in {}", root.display()),
        ));
    }
    let mut args = argv(&[
        &tc::find("swift")?,
        s(a, "action"),
        "--configuration",
        s(a, "configuration"),
    ]);
    if s(a, "platform") == "android" {
        if s(a, "action") == "test" {
            return Err(Error::new(
                "test_runtime_required",
                "Run portable Swift unit tests on Linux; Android behavior needs a device integration test",
            ));
        }
        if tc::android_ndk()?.is_none() {
            return Err(Error::new(
                "ndk_missing",
                "Configure Android NDK matching the selected Swift SDK",
            ));
        }
        let sdk = selected(s(a, "sdk"))?;
        if !b(&configuration(&sdk)?, "configured") {
            return Err(Error::new(
                "swift_sdk_unconfigured",
                "Swift SDK is not connected to an Android NDK",
            )
            .remedy("Run swift.android_configure after installing the matching NDK"));
        }
        args.extend(argv(&[
            "--swift-sdk",
            &sdk,
            "--triple",
            &format!(
                "{}-unknown-linux-android{}",
                s(a, "architecture"),
                n(a, "api_level")
            ),
            "--static-swift-stdlib",
        ]));
    }
    let workspace = if root
        .parent()
        .is_some_and(|p| crate::projects::config_path(p).is_file())
    {
        root.parent().unwrap().to_path_buf()
    } else {
        root.clone()
    };
    Ok(
        json!({"cwd":root,"steps":[{"argv":args}],"env":tc::environment()?,"exclusive":workspace,"artifacts":[".build/**/*.so",".build/**/*.a",".build/**/*.xctest"]}),
    )
}
pub fn handle(op: &str, a: &Value) -> Result {
    match op {
        "swift.plan" => plan(a),
        "swift.execute" => jobs::submit(
            &format!("Swift · {} · {}", s(a, "action"), s(a, "platform")),
            plan(a)?,
        ),
        "swift.status" => Ok(
            json!({"compiler":tc::run(&argv(&[&tc::find("swift")?,"--version"]),15,None,true)?,"android":configuration("").unwrap_or_else(|e|json!({"configured":false,"error":e.value()})),"sdks":tc::run(&argv(&[&tc::find("swift")?,"sdk","list"]),15,None,true)?,"android_ndk":tc::android_ndk()?.map(|p|p.to_string_lossy().into_owned()).unwrap_or_default(),"selected_android_sdk":selected("")?}),
        ),
        "swift.android_configure" => {
            if tc::android_ndk()?.is_none() {
                return Err(Error::new(
                    "ndk_missing",
                    "Install the Android NDK required by your Swift SDK",
                ));
            }
            let configuration = configuration(s(a, "sdk"))?;
            let script = path(s(&configuration, "setup_script"), false, true)?;
            jobs::submit(
                "Connect Swift SDK to Android NDK",
                json!({"steps":[{"argv":["bash",script]}],"cwd":script.parent(),"env":tc::environment()?}),
            )
        }
        "swift.sdk_install" => {
            let source = s(a, "source");
            let checksum = s(a, "checksum");
            let source = if source.starts_with("https://") {
                identifier(checksum, "SDK SHA-256 checksum", Some("[a-fA-F0-9]{64}"))?;
                source.to_string()
            } else {
                path(source, false, true)?.to_string_lossy().into_owned()
            };
            let mut args = argv(&[&tc::find("swift")?, "sdk", "install", &source]);
            if !checksum.is_empty() {
                identifier(checksum, "SDK SHA-256 checksum", Some("[a-fA-F0-9]{64}"))?;
                args.extend(argv(&["--checksum", checksum]));
            }
            jobs::submit(
                "Install Swift SDK",
                json!({"steps":[{"argv":args}],"cwd":home()?,"env":tc::environment()?,"timeout":3600}),
            )
        }
        _ => Err(Error::new("operation_not_found", op)),
    }
}
