//! Optional Android instrumentation for lossless native text replacement.
use crate::{api, core::*, devices, jobs, projects, toolchain as tc};
use base64::{Engine as _, engine::general_purpose::STANDARD};
use serde_json::{Value, json};
use std::fs;
const PACKAGE: &str = "io.github.marselnenaj.tomacco.automation";
const COMPONENT: &str = "io.github.marselnenaj.tomacco.automation/.AgentInstrumentation";
const JAVA: &str = include_str!("../assets/templates/AgentInstrumentation.java");
pub fn setup(a: &Value) -> Result {
    let device = identifier(s(a, "device"), "device", None)?;
    let directory = home()?
        .join("android-automation")
        .join(&digest(JAVA.as_bytes())[..16]);
    let lock = Lock::open(&home()?.join("locks/automation-setup.lock"))?;
    lock.acquire(false)?;
    if !directory.exists() {
        projects::create(
            &json!({"path":directory,"name":"Automation","organization":"io.github.marselnenaj.tomacco","kind":"android"}),
        )?;
    }
    let source = directory.join("app/src/main/java/io/github/marselnenaj/tomacco/automation");
    // This project is owned by Tomacco, not an opened user project.
    atomic_write(&source.join("AgentInstrumentation.java"), JAVA.as_bytes())?;
    let activity = source.join("MainActivity.java");
    if activity.exists() {
        fs::remove_file(activity)?;
    }
    atomic_write(&directory.join("app/src/main/AndroidManifest.xml"),br#"<manifest xmlns:android="http://schemas.android.com/apk/res/android"><application android:label="Tomacco Automation" android:allowBackup="false"/><instrumentation android:name=".AgentInstrumentation" android:targetPackage="io.github.marselnenaj.tomacco.automation" android:functionalTest="true"/></manifest>"#)?;
    let args = api::arguments(
        "project.plan",
        &json!({"path":directory,"platform":"android","action":"build"}),
    )?;
    let mut spec = projects::plan(&args)?;
    let apk = directory.join("app/build/outputs/apk/debug/app-debug.apk");
    let mut install = devices::adb(device)?;
    install.extend(argv(&["install", "-r", &apk.to_string_lossy()]));
    spec["steps"]
        .as_array_mut()
        .unwrap()
        .push(json!({"argv":install}));
    spec["steps"].as_array_mut().unwrap().push(json!({"argv":[crate::core::own_executable()?,"call","testing.instrumentation",json!({"device":device}).to_string()]}));
    spec["timeout"] = json!(1800);
    spec["exclusive"] = json!("android-automation-build");
    jobs::submit("Build and install Android UI automation", spec)
}
pub fn available(device: &str) -> Result<bool> {
    let result = tc::run(
        &devices::shell(device, &argv(&["pm", "path", PACKAGE]))?,
        15,
        None,
        false,
    )?;
    Ok(s(&result, "stdout")
        .lines()
        .any(|line| line.starts_with("package:")))
}
pub fn invoke(a: &Value) -> Result {
    let device = s(a, "device");
    let action = if a.get("selector").is_some() {
        "fill"
    } else {
        "ping"
    };
    let mut args = argv(&["am", "instrument", "-w", "-e", "action", action]);
    if action == "fill" {
        args.extend(argv(&[
            "-e",
            "selector",
            &STANDARD.encode(a["selector"].to_string()),
            "-e",
            "text",
            &STANDARD.encode(s(a, "text")),
        ]));
    }
    args.push(COMPONENT.into());
    let result = tc::run(&devices::shell(device, &args)?, 30, None, true)?;
    let encoded = s(&result, "stdout").lines().find_map(|line| {
        line.trim()
            .strip_prefix("INSTRUMENTATION_RESULT: tomacco_result=")
    });
    let Some(encoded) = encoded else {
        return Err(Error::new(
            "instrumentation_unavailable",
            "Android UI automation is not ready",
        )
        .remedy("Run testing.setup for the selected device, wait for success, then retry")
        .details(result));
    };
    let bytes = STANDARD
        .decode(encoded)
        .map_err(|_| Error::new("instrumentation_protocol", "Invalid Android response"))?;
    let value: Value = serde_json::from_slice(&bytes)?;
    if value["protocol"] != 1 {
        return Err(Error::new(
            "instrumentation_protocol",
            "Update automation with testing.setup",
        ));
    }
    if value["ok"] != true {
        return Err(Error::new("element_input_failed", s(&value, "error")));
    }
    Ok(value)
}
