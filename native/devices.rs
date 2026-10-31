use crate::{catalog, core::*, jobs, toolchain as tc};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashSet},
    fs,
    path::PathBuf,
};

pub fn adb(device: &str) -> Result<Vec<String>> {
    identifier(device, "Android serial", None)?;
    Ok(vec![tc::find("adb")?, "-s".into(), device.into()])
}
pub fn shell(device: &str, args: &[String]) -> Result<Vec<String>> {
    let mut command = adb(device)?;
    command.extend(["shell".into(), shell_join(args)]);
    Ok(command)
}
fn sh(device: &str, args: &[&str]) -> Result<Vec<String>> {
    shell(device, &argv(args))
}
fn bundle(id: &str) -> Result<&str> {
    identifier(
        id,
        "application ID",
        Some(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+"),
    )
}
fn run(args: Vec<String>) -> Result {
    tc::run(&args, 30, None, true)
}
pub fn submit(label: &str, steps: Value, timeout: u64, env: Value) -> Result {
    let mut variables = serde_json::to_value(tc::environment()?)?;
    if let Some(values) = env.as_object() {
        for (k, v) in values {
            variables[k] = v.clone();
        }
    }
    jobs::submit(
        label,
        json!({"steps":steps,"cwd":home()?,"timeout":timeout,"env":variables}),
    )
}
pub fn listing(platform: &str) -> Result {
    let mut devices = Vec::new();
    let mut diagnostics = Vec::new();
    if platform == "all" || platform == "android" {
        let result = (|| run(vec![tc::find("adb")?, "devices".into(), "-l".into()]))();
        match result {
            Ok(result) => {
                for line in s(&result, "stdout").lines() {
                    if line.starts_with("List ") || line.starts_with('*') {
                        continue;
                    }
                    let parts: Vec<_> = line.split_whitespace().collect();
                    if parts.len() < 2 {
                        continue;
                    }
                    let mut details = json!({});
                    for part in &parts[2..] {
                        if let Some((k, v)) = part.split_once(':') {
                            details[k] = json!(v);
                        }
                    }
                    let mut entry = json!({"id":parts[0],"platform":"android","state":parts[1],"name":details["model"].as_str().unwrap_or(parts[0]).replace('_'," "),"details":details});
                    if parts[0].starts_with("emulator-") && parts[1] == "device" {
                        let mut command = adb(parts[0])?;
                        command.extend(argv(&["emu", "avd", "name"]));
                        if let Ok(reply) = tc::run(&command, 3, None, true) {
                            if let Some(name) = s(&reply, "stdout")
                                .lines()
                                .next()
                                .map(str::trim)
                                .filter(|v| avd_name(v).is_ok())
                            {
                                entry["avd"] = json!(name);
                                entry["kind"] = json!("emulator");
                                if let Ok(reply) = tc::run(
                                    &sh(parts[0], &["getprop", "sys.boot_completed"])?,
                                    3,
                                    None,
                                    true,
                                ) {
                                    entry["booted"] = json!(s(&reply, "stdout").trim() == "1");
                                }
                                if let Some(avd) =
                                    avds()?.into_iter().find(|v| s(v, "name") == name)
                                {
                                    entry["profile_id"] = avd["profile_id"].clone();
                                    entry["profile_source"] = avd["profile_source"].clone();
                                    entry["display"] = avd["display"].clone();
                                    entry["name"] = json!(
                                        format!("{} · {}", s(&avd, "profile_name"), name)
                                            .trim_start_matches(" · ")
                                    );
                                } else {
                                    entry["name"] = json!(name);
                                }
                            }
                        }
                    }
                    devices.push(entry);
                }
            }
            Err(e) => {
                let mut value = e.value();
                value["platform"] = json!("android");
                diagnostics.push(value);
            }
        }
    }
    if platform == "all" || platform == "ios" {
        let result = (|| -> Result<Vec<String>> {
            if let Some(tool) = tc::find_optional("idevice_id")? {
                let value = run(argv(&[&tool, "-l"]))?;
                let re = regex::Regex::new(r"^[A-Fa-f0-9-]{16,64}$").unwrap();
                Ok(s(&value, "stdout")
                    .lines()
                    .map(str::trim)
                    .filter(|l| re.is_match(l))
                    .map(String::from)
                    .collect())
            } else {
                let value = run(argv(&[&tc::find("xtool")?, "devices", "--no-wait"]))?;
                let re =
                    regex::Regex::new(r"\b(?:[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}|[0-9a-fA-F]{40})\b")
                        .unwrap();
                let mut seen = HashSet::new();
                Ok(re
                    .find_iter(s(&value, "stdout"))
                    .map(|m| m.as_str().to_string())
                    .filter(|s| seen.insert(s.clone()))
                    .collect())
            }
        })();
        match result {
            Ok(ids) => {
                for id in ids {
                    devices.push(json!({"id":id,"platform":"ios","state":"connected","name":"iPhone / iPad"}));
                }
            }
            Err(e) => {
                let mut value = e.value();
                value["platform"] = json!("ios");
                diagnostics.push(value);
            }
        }
    }
    Ok(json!({"devices":devices,"diagnostics":diagnostics}))
}
fn info(platform: &str, device: &str) -> Result {
    identifier(device, "device ID", None)?;
    if platform == "ios" {
        return run(argv(&[&tc::find("ideviceinfo")?, "-u", device]));
    }
    let output = run(sh(device, &["getprop"])?)?;
    let re = regex::Regex::new(r"\[([^]]+)\]: \[([^]]*)\]").unwrap();
    let mut props = json!({});
    for caps in re.captures_iter(s(&output, "stdout")) {
        props[&caps[1]] = json!(&caps[2]);
    }
    Ok(
        json!({"id":device,"properties":props,"display":run(sh(device,&["wm","size"])?)?["stdout"],"density":run(sh(device,&["wm","density"])?)?["stdout"]}),
    )
}
fn launch_command(device: &str, id: &str) -> Result<Vec<String>> {
    bundle(id)?;
    let value = run(sh(
        device,
        &[
            "cmd",
            "package",
            "resolve-activity",
            "--brief",
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.LAUNCHER",
            "-p",
            id,
        ],
    )?)?;
    let re = regex::Regex::new(&format!(r"^{}/[A-Za-z0-9_.$]+$", regex::escape(id))).unwrap();
    let component = s(&value, "stdout")
        .lines()
        .map(str::trim)
        .find(|line| re.is_match(line))
        .ok_or_else(|| {
            Error::new(
                "launcher_missing",
                format!("No launcher activity found for {id}"),
            )
            .remedy("Install the app or pass an explicit activity to device.app")
        })?;
    sh(device, &["am", "start", "-W", "-n", component])
}
fn sdk_level(device: &str) -> Result<u64> {
    let result = run(sh(device, &["getprop", "ro.build.version.sdk"])?)?;
    Ok(s(&result, "stdout").trim().parse().unwrap_or(0))
}
/// The app's user ID from `cmd package list packages -U`, matched exactly.
fn package_uid(listing: &str, id: &str) -> Option<String> {
    listing.lines().find_map(|line| {
        let mut parts = line.split_whitespace();
        let package = parts.next()?.strip_prefix("package:")?;
        let uid = parts.find_map(|part| part.strip_prefix("uid:"))?;
        (package == id && !uid.is_empty() && uid.bytes().all(|c| c.is_ascii_digit()))
            .then(|| uid.to_string())
    })
}
/// logcat arguments for one app. From Android 12 its user ID selects the app's
/// processes, across restarts and before launch; older logcat filters by the
/// PID of the running process.
fn app_log_filter(device: &str, id: &str) -> Result<Vec<String>> {
    bundle(id)?;
    if sdk_level(device)? >= 31 {
        let result = run(sh(
            device,
            &["cmd", "package", "list", "packages", "-U", id],
        )?)?;
        let uid = package_uid(s(&result, "stdout"), id).ok_or_else(|| {
            Error::new("app_not_installed", format!("{id} is not installed"))
                .remedy("Install the app with device.install or project.run")
        })?;
        return Ok(vec![format!("--uid={uid}")]);
    }
    let result = run(sh(device, &["pidof", "-s", id])?)?;
    let pid = s(&result, "stdout").trim();
    if pid.is_empty() || !pid.bytes().all(|c| c.is_ascii_digit()) {
        return Err(Error::new(
            "app_not_running",
            "Launch the app before requesting process logs",
        )
        .remedy("Below Android 12, logs follow the running process"));
    }
    Ok(argv(&["--pid", pid]))
}
fn screenshot(a: &Value) -> Result {
    let device = identifier(s(a, "device"), "device ID", None)?;
    let target = output_path(s(a, "path"))?;
    fs::create_dir_all(target.parent().unwrap())?;
    if s(a, "platform") == "ios" {
        submit(
            "iOS screenshot",
            json!([{"argv":[tc::find("idevicescreenshot")?,"-u",device,target],"artifacts":[target]}]),
            120,
            json!({}),
        )
    } else {
        let mut command = adb(device)?;
        command.extend(argv(&["exec-out", "screencap", "-p"]));
        submit(
            "Android screenshot",
            json!([{"argv":command,"output":target}]),
            120,
            json!({}),
        )
    }
}
fn avd_homes() -> Result<Vec<PathBuf>> {
    let mut values = vec![home()?.join("avds")];
    if let Some(v) = std::env::var_os("ANDROID_AVD_HOME") {
        values.push(v.into());
    }
    values.push(
        std::env::var_os("ANDROID_USER_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| user_home().join(".android"))
            .join("avd"),
    );
    values.push(user_home().join(".android/avd"));
    values.push(
        std::env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| user_home().join(".config"))
            .join(".android/avd"),
    );
    let mut seen = HashSet::new();
    values.retain(|p| seen.insert(p.clone()));
    Ok(values)
}
fn avd_name(name: &str) -> Result<&str> {
    identifier(name, "AVD name", Some("[A-Za-z0-9][A-Za-z0-9_-]{0,63}"))
}
fn ini_values(path: &std::path::Path) -> BTreeMap<String, String> {
    fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .filter_map(|line| {
            let line = line.trim();
            if line.starts_with('#') || line.starts_with(';') {
                return None;
            }
            line.split_once('=')
                .map(|(k, v)| (k.trim().into(), v.trim().into()))
        })
        .collect()
}
fn profile_pixels(profile: &Value) -> (u64, u64) {
    let physical: Vec<u64> = regex::Regex::new(r"\d+")
        .unwrap()
        .find_iter(s(profile, "pixels"))
        .filter_map(|m| m.as_str().parse().ok())
        .collect();
    if physical.len() == 2 {
        (physical[0], physical[1])
    } else {
        let dpr = profile["dpr"].as_f64().unwrap_or(1.0);
        (
            (profile["width"].as_f64().unwrap_or(0.0) * dpr).round() as u64,
            (profile["height"].as_f64().unwrap_or(0.0) * dpr).round() as u64,
        )
    }
}
fn avds() -> Result<Vec<Value>> {
    let profiles = catalog::profiles("android")?;
    let mut found = BTreeMap::new();
    for directory in avd_homes()? {
        for registry in glob_paths(&format!(
            "{}/*.ini",
            glob::Pattern::escape(&directory.to_string_lossy())
        ))? {
            let name = registry
                .file_stem()
                .unwrap_or_default()
                .to_string_lossy()
                .into_owned();
            if found.contains_key(&name) {
                continue;
            }
            let ini = ini_values(&registry);
            let path = ini
                .get("path")
                .map(PathBuf::from)
                .filter(|v| v.is_absolute())
                .unwrap_or_else(|| directory.join(format!("{name}.avd")));
            let config = ini_values(&path.join("config.ini"));
            let value = |key: &str| config.get(key).map(String::as_str).unwrap_or("");
            let width = value("hw.lcd.width").parse::<u64>().unwrap_or(0);
            let height = value("hw.lcd.height").parse::<u64>().unwrap_or(0);
            let density = value("hw.lcd.density").parse::<u64>().unwrap_or(0);
            let mut source = "assigned";
            let mut profile = profiles
                .as_array()
                .unwrap()
                .iter()
                .find(|p| s(p, "id") == value("tomacco.profile.id"));
            if profile.is_none() {
                // Legacy AVDs: infer only an unambiguous resolution AND density.
                source = "display-match";
                let matches: Vec<_> = profiles
                    .as_array()
                    .unwrap()
                    .iter()
                    .filter(|p| {
                        profile_pixels(p) == (width, height)
                            && (p["dpr"].as_f64().unwrap_or(0.0) * 160.0).round() as u64 == density
                    })
                    .collect();
                if matches.len() == 1 {
                    profile = Some(matches[0]);
                }
            }
            found.insert(name.clone(),json!({"name":name,"registry":registry,"path":path,"managed":directory==home()?.join("avds"),"profile_id":profile.map(|p|s(p,"id")),"profile_name":profile.map(|p|s(p,"name")).unwrap_or(""),"profile_source":if profile.is_some(){source}else{"unknown"},"display":{"width":width,"height":height,"density":density},"system_image":value("image.sysdir.1").trim_matches('/').replace('/',";"),"gpu":value("hw.gpu.mode"),"vulkan":value("tomacco.vulkan"),"memory_mb":avd_memory_mb(value("hw.ramSize")),"cores":value("hw.cpu.ncore").parse::<u64>().ok()}));
        }
    }
    Ok(found.into_values().collect())
}
fn installed_system_images() -> Result<Vec<String>> {
    let Some(sdk) = tc::android_sdk()? else {
        return Ok(Vec::new());
    };
    let mut images = Vec::new();
    for file in glob_paths(&format!(
        "{}/system-images/*/*/*/system.img",
        glob::Pattern::escape(&sdk.to_string_lossy())
    ))? {
        if let Some(parent) = file.parent().and_then(|p| p.strip_prefix(&sdk).ok()) {
            images.push(parent.to_string_lossy().replace('/', ";"));
        }
    }
    images.sort();
    images.dedup();
    Ok(images)
}
fn sdk_args() -> Result<Vec<String>> {
    Ok(tc::android_sdk()?
        .map(|p| vec![format!("--sdk_root={}", p.display())])
        .unwrap_or_default())
}
pub fn handle(op: &str, a: &Value) -> Result {
    let device = s(a, "device");
    let platform = s(a, "platform");
    let id = s(a, "bundle_id");
    match op {
        "device.list" => listing(platform),
        "device.info" => info(platform, device),
        "device.install" => {
            identifier(device, "device ID", None)?;
            let files = strings(&a["paths"])
                .iter()
                .map(|p| path(p, false, true))
                .collect::<Result<Vec<_>>>()?;
            let command = if platform == "ios" {
                if files.len() != 1
                    || files[0]
                        .extension()
                        .unwrap_or_default()
                        .to_string_lossy()
                        .to_lowercase()
                        != "ipa"
                {
                    return Err(Error::new(
                        "invalid_artifact",
                        "iOS installation requires one signed .ipa",
                    ));
                }
                vec![
                    tc::find("xtool")?,
                    "install".into(),
                    "--udid".into(),
                    device.into(),
                    files[0].to_string_lossy().into_owned(),
                ]
            } else if files.len() == 1
                && files[0]
                    .extension()
                    .is_some_and(|x| x.eq_ignore_ascii_case("aab"))
            {
                return crate::android_package::install_bundle(device, &files[0]);
            } else {
                if files.iter().any(|p| {
                    p.extension()
                        .unwrap_or_default()
                        .to_string_lossy()
                        .to_lowercase()
                        != "apk"
                }) {
                    return Err(Error::new(
                        "invalid_artifact",
                        "Android devices install APKs, split APKs or one .aab through bundletool",
                    ));
                }
                let mut command = adb(device)?;
                command.extend(argv(&[
                    if files.len() > 1 {
                        "install-multiple"
                    } else {
                        "install"
                    },
                    "-r",
                ]));
                command.extend(files.iter().map(|p| p.to_string_lossy().into_owned()));
                command
            };
            submit("Install app", json!([{"argv":command}]), 300, json!({}))
        }
        "device.app" => {
            identifier(device, "device ID", None)?;
            bundle(id)?;
            let action = s(a, "action");
            let activity = s(a, "activity");
            let command = if platform == "ios" {
                if !["launch", "uninstall"].contains(&action) {
                    return Err(Error::new(
                        "action_unsupported",
                        "xtool supports launch and uninstall; stopping requires a compatible device debugger",
                    ));
                }
                argv(&[&tc::find("xtool")?, action, "--udid", device, id])
            } else {
                match action {
                    "launch" => {
                        if activity.is_empty() {
                            launch_command(device, id)?
                        } else {
                            identifier(activity, "activity", Some(r"[A-Za-z0-9_.$/]+"))?;
                            sh(
                                device,
                                &["am", "start", "-W", "-n", &format!("{id}/{activity}")],
                            )?
                        }
                    }
                    "stop" => sh(device, &["am", "force-stop", id])?,
                    "uninstall" => {
                        let mut c = adb(device)?;
                        c.extend(argv(&["uninstall", id]));
                        c
                    }
                    "clear-data" => sh(device, &["pm", "clear", id])?,
                    _ => return Err(Error::new("invalid_argument", action)),
                }
            };
            submit(
                &format!("{action} {id}"),
                json!([{"argv":command}]),
                120,
                json!({}),
            )
        }
        "android.launch" => tc::run(&launch_command(device, id)?, 60, None, true),
        "device.screenshot" => screenshot(a),
        "device.capture" => {
            let job = screenshot(a)?;
            let result = jobs::wait(s(&job, "id"), 30)?;
            if s(&result, "state") != "succeeded" {
                return Err(Error::new(
                    "capture_incomplete",
                    "Device screenshot has not completed successfully",
                )
                .remedy("Inspect job.get and job.logs")
                .details(json!({"job_id":job["id"],"state":result["state"]})));
            }
            let mut artifact = result["result"]["artifacts"]
                .as_array()
                .and_then(|a| a.first())
                .cloned()
                .ok_or_else(|| {
                    Error::new(
                        "artifact_missing",
                        "Screenshot command produced no artifact",
                    )
                })?;
            artifact["job_id"] = job["id"].clone();
            artifact["platform"] = json!(platform);
            artifact["device"] = json!(device);
            Ok(artifact)
        }
        "device.logs" => {
            identifier(device, "device ID", None)?;
            let filter = s(a, "filter");
            let mut command = if platform == "ios" {
                let mut args = argv(&[&tc::find("idevicesyslog")?, "-u", device]);
                if !filter.is_empty() {
                    args.extend(argv(&["-m", filter]));
                }
                args
            } else {
                let mut args = adb(device)?;
                args.extend(argv(&["logcat", "-v", "threadtime"]));
                if !id.is_empty() {
                    args.extend(app_log_filter(device, id)?);
                }
                if !filter.is_empty() {
                    identifier(filter, "logcat filter", Some(r"[A-Za-z0-9_.*:-]+"))?;
                    args.push(filter.into());
                }
                args
            };
            submit(
                "Device logs",
                json!([{"argv":std::mem::take(&mut command),"duration":n(a,"duration")}]),
                n(a, "duration") + 10,
                json!({}),
            )
        }
        "android.input" => {
            let action = s(a, "action");
            let mut args = argv(&["input", action]);
            match action {
                "tap" => args.extend([n(a, "x").to_string(), n(a, "y").to_string()]),
                "swipe" => args
                    .extend(["x", "y", "end_x", "end_y", "duration"].map(|k| n(a, k).to_string())),
                "text" => {
                    let text = s(a, "text");
                    if !text.is_ascii() || text.contains("%s") {
                        // adb input text types ASCII only and reads %s as a space.
                        identifier(device, "Android serial", None)?;
                        return crate::runtime::android::paste(device, text);
                    }
                    args.push(text.replace(' ', "%s"));
                }
                "keyevent" => args.push(
                    identifier(
                        s(a, "key"),
                        "key code",
                        Some(r"KEYCODE_[A-Z0-9_]+|[0-9]{1,4}"),
                    )?
                    .into(),
                ),
                _ => return Err(Error::new("invalid_argument", action)),
            }
            run(shell(device, &args)?)
        }
        "android.hierarchy" => {
            let p = format!(
                "/data/local/tmp/tomacco-{}.xml",
                uuid::Uuid::new_v4().simple()
            );
            run(sh(device, &["uiautomator", "dump", &p])?)?;
            let value = run(sh(device, &["cat", &p])?);
            let _ = tc::run(&sh(device, &["rm", "-f", &p])?, 15, None, false);
            Ok(json!({"xml":value?["stdout"]}))
        }
        "android.record" => {
            let target = output_path(s(a, "path"))?;
            fs::create_dir_all(target.parent().unwrap())?;
            let remote = format!("/sdcard/tomacco-{}.mp4", uuid::Uuid::new_v4().simple());
            let mut pull = adb(device)?;
            pull.extend(argv(&["pull", &remote, &target.to_string_lossy()]));
            submit(
                "Record Android screen",
                json!([{"argv":sh(device,&["screenrecord","--time-limit",&n(a,"seconds").to_string(),&remote])?},{"argv":pull,"artifacts":[target]},{"argv":sh(device,&["rm","-f",&remote])?}]),
                n(a, "seconds") + 60,
                json!({}),
            )
        }
        "android.mirror" => submit(
            "Android live display",
            json!([{"argv":[tc::find("scrcpy")?,"--serial",identifier(device,"device",None)?,"--window-title","Tomacco · Android"]}]),
            86400,
            json!({}),
        ),
        "android.deep_link" => {
            let url = s(a, "url");
            if !regex::Regex::new(r"^[A-Za-z][A-Za-z0-9+.-]*://")
                .unwrap()
                .is_match(url)
            {
                return Err(Error::new(
                    "invalid_url",
                    "Use a complete deep link with a scheme",
                ));
            }
            run(sh(
                device,
                &[
                    "am",
                    "start",
                    "-W",
                    "-a",
                    "android.intent.action.VIEW",
                    "-d",
                    url,
                ],
            )?)
        }
        "android.performance" => {
            bundle(id)?;
            let args = match s(a, "report") {
                "memory" => argv(&["dumpsys", "meminfo", id]),
                "frames" => argv(&["dumpsys", "gfxinfo", id, "framestats"]),
                "battery" => argv(&["dumpsys", "batterystats", id]),
                _ => argv(&["dumpsys", "package", id]),
            };
            run(shell(device, &args)?)
        }
        "android.ports" => {
            let reverse = b(a, "reverse");
            let (left, right) = if reverse {
                (n(a, "device_port"), n(a, "local_port"))
            } else {
                (n(a, "local_port"), n(a, "device_port"))
            };
            let mut args = adb(device)?;
            args.push(if reverse { "reverse" } else { "forward" }.into());
            if b(a, "remove") {
                args.extend(["--remove".into(), format!("tcp:{left}")]);
            } else {
                args.extend([format!("tcp:{left}"), format!("tcp:{right}")]);
            }
            run(args)
        }
        "android.permission" => {
            bundle(id)?;
            identifier(s(a, "permission"), "permission", Some(r"[A-Za-z0-9_.]+"))?;
            run(sh(
                device,
                &[
                    "pm",
                    if b(a, "grant") { "grant" } else { "revoke" },
                    id,
                    s(a, "permission"),
                ],
            )?)
        }
        "android.connect" => {
            let address = identifier(
                s(a, "address"),
                "host:port",
                Some(r"[A-Za-z0-9.\[\]:_-]+:[0-9]{1,5}"),
            )?;
            let pairing = s(a, "pairing_code");
            let mut args = argv(&[
                &tc::find("adb")?,
                if pairing.is_empty() {
                    "connect"
                } else {
                    "pair"
                },
                address,
            ]);
            if !pairing.is_empty() {
                args.push(identifier(pairing, "pairing code", Some(r"[0-9]{6}"))?.into());
            }
            run(args)
        }
        "android.calibrate" => {
            let value = info("android", device)?;
            let re = regex::Regex::new(r"(\d+)x(\d+)").unwrap();
            let size = re
                .captures_iter(s(&value, "display"))
                .last()
                .ok_or_else(|| {
                    Error::new("display_unavailable", "Device did not report wm size")
                })?;
            let re = regex::Regex::new(r":\s*(\d+)").unwrap();
            let density = re
                .captures_iter(s(&value, "density"))
                .last()
                .ok_or_else(|| {
                    Error::new("display_unavailable", "Device did not report density")
                })?;
            let w = size[1].parse::<f64>().unwrap();
            let h = size[2].parse::<f64>().unwrap();
            let dpr = density[1].parse::<f64>().unwrap() / 160.0;
            let profile = json!({"id":s(a,"profile_id"),"name":s(a,"name"),"platform":"android","width":(w/dpr).round() as u64,"height":(h/dpr).round() as u64,"dpr":dpr,"radius":28,"bezel":8,"cutout":"punch","colors":["#30353d"],"colorNames":["Device"],"pixels":format!("{w} × {h}"),"confidence":"device-measured-display","notes":"Display size/density read from adb wm. Frame, camera and insets need visual calibration.","safePortrait":{"top":32,"right":0,"bottom":24,"left":0},"safeLandscape":{"top":0,"right":0,"bottom":24,"left":32}});
            catalog::check(&profile)?;
            let temp = tempfile::NamedTempFile::new_in(home()?)?;
            atomic_json(temp.path(), &profile)?;
            catalog::import(&json!({"path":temp.path()}))
        }
        "android.sdk_packages" => {
            let mut args = vec![tc::find("sdkmanager")?];
            args.extend(sdk_args()?);
            args.push("--list".into());
            tc::run(&args, 60, None, true)
        }
        "android.sdk_install" => {
            let mut args = vec![tc::find("sdkmanager")?];
            args.extend(sdk_args()?);
            args.push("--install".into());
            for package in strings(&a["packages"]) {
                identifier(&package, "SDK package", Some(r"[A-Za-z0-9_.;+-]+"))?;
                args.push(package);
            }
            let mut step = json!({"argv":args});
            if b(a, "accept_licenses") {
                step["input"] = json!("y\n".repeat(100));
            }
            submit(
                "Install Android SDK packages",
                json!([step]),
                3600,
                json!({}),
            )
        }
        "android.avds" => Ok(json!({"avds":avds()?,"system_images":installed_system_images()?})),
        "android.avd_create" => {
            let name = avd_name(s(a, "name"))?;
            let admission = Lock::open(
                &home()?
                    .join("locks")
                    .join(format!("avd-create-{}.lock", digest(name.as_bytes()))),
            )?;
            admission.acquire(false)?;
            identifier(
                s(a, "system_image"),
                "system image",
                Some(r"system-images;android-[0-9]+(?:\.[0-9]+)?;[a-z0-9_]+;[a-z0-9_]+"),
            )?;
            let profile = catalog::find(s(a, "profile_id"))?;
            if s(&profile, "platform") != "android" {
                return Err(Error::new("invalid_profile", "Select an Android profile"));
            }
            let target = home()?.join("avds").join(format!("{name}.avd"));
            if target.exists()
                || avd_homes()?
                    .iter()
                    .any(|p| p.join(format!("{name}.ini")).exists())
            {
                return Err(Error::new("avd_exists", "Choose a new AVD name"));
            }
            private_dir(&home()?.join("avds"))?;
            let mut env = serde_json::to_value(tc::environment()?)?;
            env["ANDROID_AVD_HOME"] = json!(home()?.join("avds"));
            jobs::submit(
                &format!("Create {name}"),
                json!({"steps":[{"argv":[tc::find("avdmanager")?,"create","avd","--name",name,"--package",s(a,"system_image"),"--path",target],"input":"no\n"},{"argv":[crate::core::own_executable()?,"call","android.avd_profile",json!({"name":name,"profile_id":s(a,"profile_id")}).to_string()]}],"timeout":120,"cwd":home()?,"env":env,"exclusive":format!("android-avd-create:{name}"),"single_active":true}),
            )
        }
        "android.avd_profile" => avd_profile(a),
        "android.avd_configure" => avd_configure(a),
        "android.avd_start" => {
            let name = avd_name(s(a, "name"))?;
            let admission = avd_settings_lock(name)?;
            admission.acquire(false)?;
            // Reuse an emulator even when it was started outside this process
            // or by an older Tomacco worker.
            let connected = listing("android")?;
            for device in connected["devices"].as_array().into_iter().flatten() {
                let serial = s(device, "id");
                if !serial.starts_with("emulator-") || s(device, "state") != "device" {
                    continue;
                }
                let mut command = adb(serial)?;
                command.extend(argv(&["emu", "avd", "name"]));
                if let Ok(reply) = tc::run(&command, 5, None, true) {
                    if s(&reply, "stdout")
                        .lines()
                        .next()
                        .is_some_and(|line| line.trim() == name)
                    {
                        return Ok(
                            json!({"reused":true,"name":name,"device":serial,"status":"running","settings_applied":false}),
                        );
                    }
                }
            }
            let directory = avd_homes()?
                .into_iter()
                .find(|p| p.join(format!("{name}.ini")).is_file())
                .ok_or_else(|| {
                    Error::new("avd_not_found", format!("Unknown AVD: {name}"))
                        .remedy("Use android.avds or android.avd_create")
                })?;
            let mut args = argv(&[&tc::find("emulator")?, "-avd", name, "-no-snapshot-save"]);
            let registry = ini_values(&directory.join(format!("{name}.ini")));
            let avd = registry
                .get("path")
                .map(PathBuf::from)
                .filter(|p| p.is_absolute())
                .unwrap_or_else(|| directory.join(format!("{name}.avd")));
            let config = ini_values(&avd.join("config.ini"));
            if b(a, "cold_boot") || config.get("tomacco.coldBoot").is_some_and(|v| v == "yes") {
                args.push("-no-snapshot-load".into());
            }
            let vulkan = if s(a, "vulkan") == "configured" {
                config
                    .get("tomacco.vulkan")
                    .map(String::as_str)
                    .unwrap_or("auto")
            } else {
                s(a, "vulkan")
            };
            if vulkan == "on" || vulkan == "off" {
                args.extend(argv(&[
                    "-feature",
                    if vulkan == "off" { "-Vulkan" } else { "Vulkan" },
                ]));
            }
            if b(a, "headless") {
                args.push("-no-window".into());
            }
            if !b(a, "audio") {
                args.push("-no-audio".into());
            }
            if b(a, "software") {
                args.extend(argv(&["-accel", "off", "-gpu", "swiftshader"]));
            } else if s(a, "gpu") != "configured" {
                args.extend(argv(&["-gpu", s(a, "gpu")]));
            }
            let mut env = serde_json::to_value(tc::environment()?)?;
            env["ANDROID_AVD_HOME"] = json!(directory);
            jobs::submit(
                &format!("Emulator {name}"),
                json!({"steps":[{"argv":args}],"cwd":home()?,"timeout":86400,"env":env,"exclusive":format!("android-avd:{name}"),"single_active":true}),
            )
        }
        "android.settings" => settings(a),
        "android.emulator" => emulator(a),
        "ios.sdk_install" => {
            let source = expand(s(a, "path"));
            let source = path(s(a, "path"), source.is_dir(), true)?;
            let mut args = vec![
                tc::find("xtool")?,
                "sdk".into(),
                "install".into(),
                source.to_string_lossy().into_owned(),
            ];
            if b(a, "slim") {
                args.push("--slim".into());
            }
            submit("Import Apple SDK", json!([{"argv":args}]), 7200, json!({}))
        }
        _ => Err(Error::new("operation_not_found", op)),
    }
}
/// Runs one emulator console command through `adb emu`. The console answers
/// `KO: reason` for a refused command while adb itself exits with success.
fn console(device: &str, command: &[&str]) -> Result<String> {
    let mut args = adb(device)?;
    args.push("emu".into());
    args.extend(argv(command));
    let result = run(args)?;
    let output = s(&result, "stdout").replace('\r', "");
    if let Some(reason) = output.lines().find_map(|l| l.trim().strip_prefix("KO")) {
        return Err(Error::new(
            "emulator_refused",
            format!(
                "The emulator refused {}: {}",
                command.join(" "),
                reason.trim_start_matches(':').trim()
            ),
        )
        .details(json!({"command":command,"output":output})));
    }
    Ok(output
        .lines()
        .filter(|l| l.trim() != "OK")
        .collect::<Vec<_>>()
        .join("\n"))
}
/// Snapshot tags from the console's `avd snapshot list` table.
fn snapshot_names(output: &str) -> Vec<String> {
    let mut rows = output.lines().skip_while(|l| {
        let mut columns = l.split_whitespace();
        !(columns.next() == Some("ID") && columns.next() == Some("TAG"))
    });
    rows.next();
    rows.filter_map(|l| l.split_whitespace().nth(1).map(String::from))
        .collect()
}
/// `key: value` lines of `power display` and `network status`.
fn console_fields(output: &str) -> Value {
    let mut fields = json!({});
    for line in output.lines() {
        if let Some((key, value)) = line.split_once(':') {
            let value = value.trim();
            if !value.is_empty() {
                fields[key.trim().replace(' ', "_")] = json!(value);
            }
        }
    }
    fields
}
fn emulator(a: &Value) -> Result {
    let device = s(a, "device");
    if !device.starts_with("emulator-") {
        return Err(Error::new(
            "not_emulator",
            "This operation requires an emulator serial",
        ));
    }
    let action = s(a, "action");
    let name = identifier(s(a, "name"), "snapshot name", Some("[A-Za-z0-9_-]+"))?;
    match action {
        "stop" => {
            console(device, &["kill"])?;
            Ok(json!({"device":device,"action":action}))
        }
        "snapshot-save" | "snapshot-load" | "snapshot-delete" => {
            let verb = match action {
                "snapshot-save" => "save",
                "snapshot-load" => "load",
                _ => "del",
            };
            console(device, &["avd", "snapshot", verb, name])?;
            Ok(json!({"device":device,"action":action,"name":name}))
        }
        "snapshot-list" => {
            let output = console(device, &["avd", "snapshot", "list"])?;
            Ok(json!({"device":device,"snapshots":snapshot_names(&output),"output":output}))
        }
        "battery" => {
            let level = n(a, "battery_level").to_string();
            let charging = b(a, "charging");
            console(device, &["power", "capacity", &level])?;
            console(
                device,
                &["power", "ac", if charging { "on" } else { "off" }],
            )?;
            console(
                device,
                &[
                    "power",
                    "status",
                    if charging { "charging" } else { "discharging" },
                ],
            )?;
            let state = console(device, &["power", "display"])?;
            Ok(json!({"device":device,"action":action,"battery":console_fields(&state)}))
        }
        "network" => {
            console(device, &["network", "speed", s(a, "network_speed")])?;
            // The console's own gprs latency preset applies UMTS values on the
            // tested emulator, so every preset goes as its range in milliseconds.
            let delay = match s(a, "network_delay") {
                "gprs" => "150:550",
                "edge" => "80:400",
                "umts" => "35:200",
                _ => "0:0",
            };
            console(device, &["network", "delay", delay])?;
            let state = console(device, &["network", "status"])?;
            Ok(
                json!({"device":device,"action":action,"speed":s(a,"network_speed"),"delay":s(a,"network_delay"),"network":console_fields(&state)}),
            )
        }
        _ => {
            console(
                device,
                &[
                    "geo",
                    "fix",
                    &a["longitude"].to_string(),
                    &a["latitude"].to_string(),
                ],
            )?;
            Ok(
                json!({"device":device,"action":action,"longitude":a["longitude"],"latitude":a["latitude"]}),
            )
        }
    }
}
const ANIMATION_SCALES: [&str; 3] = [
    "window_animation_scale",
    "transition_animation_scale",
    "animator_duration_scale",
];
/// Language tags separated by commas, as `cmd locale` takes them.
fn locale_tags(value: &str) -> Result<&str> {
    if value.is_empty() {
        return Ok(value);
    }
    identifier(
        value,
        "language tags",
        Some(r"[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*(,[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*)*"),
    )
}
/// The tags in `Locales for PACKAGE for user 0 are [de-DE,fr]`.
fn app_locales(output: &str) -> Option<String> {
    let line = output.lines().find(|l| l.starts_with("Locales for "))?;
    let list = line.rsplit_once(" are [")?.1.strip_suffix(']')?;
    Some(list.split(',').map(str::trim).collect::<Vec<_>>().join(","))
}
fn setting(device: &str, namespace: &str, key: &str) -> Result<String> {
    let result = run(sh(device, &["settings", "get", namespace, key])?)?;
    Ok(s(&result, "stdout").trim().to_string())
}
/// A scale setting as Android reads it; unset means the default of 1.
fn scale(value: &str) -> f64 {
    value.parse().unwrap_or(1.0)
}
/// Sets the settings that change how an app looks or moves in UI tests and
/// screenshots, then reads every one back. Arguments left out stay unchanged.
fn settings(a: &Value) -> Result {
    let device = identifier(s(a, "device"), "device ID", None)?;
    let locale = a.get("app_locale").and_then(Value::as_str);
    let id = s(a, "bundle_id");
    if locale.is_some() && id.is_empty() {
        return Err(Error::new(
            "invalid_argument",
            "app_locale applies to one app; pass its bundle_id",
        ));
    }
    if !id.is_empty() {
        bundle(id)?;
    }
    if let Some(tags) = locale {
        locale_tags(tags)?;
    }
    let sdk = sdk_level(device)?;
    if a.get("airplane_mode").is_some() && sdk < 30 {
        return Err(Error::new(
            "setting_unsupported",
            "Switching airplane mode from adb needs Android 11 (API 30) or newer",
        ));
    }
    if !id.is_empty() && sdk < 33 {
        return Err(Error::new(
            "setting_unsupported",
            "A language for one app needs Android 13 (API 33) or newer",
        )
        .remedy(
            "Older Android changes the language only for the whole device, in its Settings app",
        ));
    }
    let mut changed = Vec::new();
    if let Some(mode) = a.get("dark_mode").and_then(Value::as_str) {
        let night = match mode {
            "on" => "yes",
            "off" => "no",
            _ => "auto",
        };
        run(sh(device, &["cmd", "uimode", "night", night])?)?;
        changed.push("dark_mode");
    }
    if let Some(value) = a.get("font_scale").and_then(Value::as_f64) {
        run(sh(
            device,
            &[
                "settings",
                "put",
                "system",
                "font_scale",
                &value.to_string(),
            ],
        )?)?;
        changed.push("font_scale");
    }
    if let Some(on) = a.get("animations").and_then(Value::as_bool) {
        for key in ANIMATION_SCALES {
            run(sh(
                device,
                &["settings", "put", "global", key, if on { "1" } else { "0" }],
            )?)?;
        }
        changed.push("animations");
    }
    if let Some(on) = a.get("airplane_mode").and_then(Value::as_bool) {
        run(sh(
            device,
            &[
                "cmd",
                "connectivity",
                "airplane-mode",
                if on { "enable" } else { "disable" },
            ],
        )?)?;
        changed.push("airplane_mode");
    }
    if let Some(tags) = locale {
        let mut args = vec!["cmd", "locale", "set-app-locales", id];
        // Without --locales the app follows the device language again.
        if !tags.is_empty() {
            args.extend(["--locales", tags]);
        }
        run(sh(device, &args)?)?;
        changed.push("app_locale");
    }
    let night = run(sh(device, &["cmd", "uimode", "night"])?)?;
    let night = s(&night, "stdout")
        .lines()
        .find_map(|l| l.trim().strip_prefix("Night mode: "))
        .unwrap_or("")
        .trim();
    let mut animations = json!({});
    for key in ANIMATION_SCALES {
        animations[key] = json!(scale(&setting(device, "global", key)?));
    }
    let mut value = json!({
        "device": device,
        "sdk": sdk,
        "dark_mode": match night { "yes" => "on", "no" => "off", other => other },
        "font_scale": scale(&setting(device, "system", "font_scale")?),
        "animations": ANIMATION_SCALES.iter().all(|k| animations[k] != json!(0.0)),
        "animation_scales": animations,
        "airplane_mode": setting(device, "global", "airplane_mode_on")? == "1",
        "changed": changed,
    });
    if !id.is_empty() {
        let result = run(sh(device, &["cmd", "locale", "get-app-locales", id])?)?;
        let tags = app_locales(s(&result, "stdout")).ok_or_else(|| {
            Error::new(
                "app_not_installed",
                format!("Android has no language setting for {id}"),
            )
            .remedy("Install the app first")
            .details(result.clone())
        })?;
        value["bundle_id"] = json!(id);
        value["app_locale"] = json!(tags);
    }
    // Android accepts some values without applying them; report those.
    let mut differences = Vec::new();
    for key in ["dark_mode", "animations", "airplane_mode", "app_locale"] {
        if let Some(wanted) = a.get(key) {
            let actual = &value[key];
            let same = match (wanted.as_str(), actual.as_str()) {
                (Some(w), Some(v)) => w.eq_ignore_ascii_case(v),
                _ => wanted == actual,
            };
            if !same {
                differences.push(key);
            }
        }
    }
    if let Some(wanted) = a.get("font_scale").and_then(Value::as_f64) {
        if (value["font_scale"].as_f64().unwrap_or(0.0) - wanted).abs() > 0.001 {
            differences.push("font_scale");
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
fn avd_profile(a: &Value) -> Result {
    let name = avd_name(s(a, "name"))?;
    let admission = avd_settings_lock(name)?;
    admission.acquire(false)?;
    avd_not_starting(name)?;
    let profile = catalog::find(s(a, "profile_id"))?;
    if s(&profile, "platform") != "android" {
        return Err(Error::new("invalid_profile", "Select an Android profile"));
    }
    let target = home()?.join("avds").join(format!("{name}.avd/config.ini"));
    if !target.is_file() {
        return Err(Error::new(
            "avd_not_found",
            "Only Tomacco managed AVDs can be calibrated",
        ));
    }
    let _emulator_lock = avd_configuration_lock(target.parent().unwrap())?;
    let dpr = profile["dpr"].as_f64().unwrap();
    let (w, h) = profile_pixels(&profile);
    let values = BTreeMap::from([
        ("hw.lcd.width", w.to_string()),
        ("hw.lcd.height", h.to_string()),
        ("hw.lcd.density", (dpr * 160.0).round().to_string()),
        ("hw.keyboard", "yes".into()),
        ("showDeviceFrame", "no".into()),
        ("hw.mainKeys", "no".into()),
        ("hw.dPad", "no".into()),
        ("hw.trackBall", "no".into()),
        ("avd.id", name.into()),
        ("avd.name", name.into()),
        ("tomacco.profile.id", s(a, "profile_id").into()),
    ]);
    let content = fs::read_to_string(&target)?;
    let mut values = values;
    let configured = ini_values(&target);
    for (key, value) in [
        ("hw.gpu.enabled", "yes"),
        ("hw.gpu.mode", "auto"),
        ("hw.ramSize", "4096"),
    ] {
        if !configured.contains_key(key) {
            values.insert(key, value.into());
        }
    }
    let mut lines: Vec<_> = content
        .lines()
        .filter(|l| !values.contains_key(l.split('=').next().unwrap_or("").trim()))
        .map(String::from)
        .collect();
    lines.extend(values.iter().map(|(k, v)| format!("{k}={v}")));
    atomic_write(&target, (lines.join("\n") + "\n").as_bytes())?;
    atomic_write(
        &home()?.join("avds").join(format!("{name}.ini")),
        format!(
            "avd.ini.encoding=UTF-8\npath={}\n",
            target.parent().unwrap().display()
        )
        .as_bytes(),
    )?;
    Ok(
        json!({"avd":name,"profile":s(a,"profile_id"),"resolution":[w,h],"density":values["hw.lcd.density"]}),
    )
}

fn avd_memory_mb(value: &str) -> Option<u64> {
    let value = value.trim().to_ascii_uppercase();
    let (number, multiplier) = if let Some(number) = value.strip_suffix('G') {
        (number, 1024)
    } else {
        (value.strip_suffix('M').unwrap_or(&value), 1)
    };
    number.parse::<u64>().ok()?.checked_mul(multiplier)
}

fn avd_settings_lock(name: &str) -> Result<Lock> {
    Lock::open(
        &home()?
            .join("locks")
            .join(format!("avd-start-{}.lock", digest(name.as_bytes()))),
    )
}

fn avd_not_starting(name: &str) -> Result<()> {
    if let Some(job) = jobs::active_exclusive(&format!("android-avd:{name}"))? {
        return Err(Error::new(
            "avd_in_use",
            "Stop the emulator job before changing its hardware settings",
        )
        .details(json!({"job":job})));
    }
    Ok(())
}

fn avd_configure(a: &Value) -> Result {
    let name = avd_name(s(a, "name"))?;
    let admission = avd_settings_lock(name)?;
    admission.acquire(false)?;
    avd_not_starting(name)?;
    let selected = avds()?
        .into_iter()
        .find(|v| s(v, "name") == name)
        .ok_or_else(|| Error::new("avd_not_found", format!("Unknown AVD: {name}")))?;
    let directory = PathBuf::from(s(&selected, "path"));
    let target = directory.join("config.ini");
    let _emulator_lock = avd_configuration_lock(&directory)?;
    let mut values = BTreeMap::new();
    for (argument, key) in [
        ("gpu", "hw.gpu.mode"),
        ("vulkan", "tomacco.vulkan"),
        ("memory_mb", "hw.ramSize"),
        ("cores", "hw.cpu.ncore"),
    ] {
        if let Some(value) = a.get(argument) {
            values.insert(
                key,
                value
                    .as_str()
                    .map(String::from)
                    .unwrap_or_else(|| value.to_string()),
            );
        }
    }
    if values.is_empty() {
        return Err(Error::new(
            "invalid_argument",
            "Specify gpu, vulkan, memory_mb or cores",
        ));
    }
    if values.contains_key("hw.gpu.mode") {
        values.insert("hw.gpu.enabled", "yes".into());
    }
    // Snapshots contain the old GPU and RAM layout. Keep cold boots enabled
    // for explicitly configured hardware; never erase the data partition.
    values.insert("tomacco.coldBoot", "yes".into());
    let content = fs::read_to_string(&target)?;
    let backup = target.with_extension("ini.before-performance");
    if !backup.exists() {
        atomic_write(&backup, content.as_bytes())?;
    }
    let mut lines: Vec<_> = content
        .lines()
        .filter(|line| !values.contains_key(line.split('=').next().unwrap_or("").trim()))
        .map(String::from)
        .collect();
    lines.extend(values.iter().map(|(key, value)| format!("{key}={value}")));
    atomic_write(&target, (lines.join("\n") + "\n").as_bytes())?;
    Ok(
        json!({"name":name,"gpu":values.get("hw.gpu.mode").map(String::as_str).unwrap_or(s(&selected,"gpu")),"memory_mb":a.get("memory_mb").unwrap_or(&selected["memory_mb"]),"cores":a.get("cores").unwrap_or(&selected["cores"]),"vulkan":a.get("vulkan").unwrap_or(&selected["vulkan"]),"cold_boot":true,"backup":backup}),
    )
}

// Emulator multiinstance.lock is a flock, and remains on disk after shutdown.
// Hold the same exclusive lock throughout the update, including against an
// emulator started outside Tomacco. Older unknown lock formats stay
// protected rather than being deleted or mistaken for an idle device.
fn avd_configuration_lock(directory: &std::path::Path) -> Result<Lock> {
    let multi = directory.join("multiinstance.lock");
    let locks = glob_paths(&format!(
        "{}/*.lock",
        glob::Pattern::escape(&directory.to_string_lossy())
    ))?;
    let known = multi.is_file()
        && locks.iter().all(|p| {
            p.is_file()
                && (p == &multi || p.file_name().is_some_and(|n| n == "hardware-qemu.ini.lock"))
        });
    if !locks.is_empty() && !known {
        return Err(Error::new(
            "avd_in_use",
            "Stop the emulator before changing its hardware settings",
        )
        .remedy("Unrecognized emulator lock files are preserved"));
    }
    let lock = Lock::open(&multi)?;
    if !lock.acquire(true)? {
        return Err(Error::new(
            "avd_in_use",
            "Stop the emulator before changing its hardware settings",
        ));
    }
    Ok(lock)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn package_uid_matches_the_exact_package_only() {
        let listing =
            "package:dev.example.app.helper uid:10200\npackage:dev.example.app uid:10199\n";
        assert_eq!(
            package_uid(listing, "dev.example.app").as_deref(),
            Some("10199")
        );
        assert_eq!(package_uid(listing, "dev.example"), None);
        assert_eq!(
            package_uid("package:dev.example.app uid:x1", "dev.example.app"),
            None
        );
        assert_eq!(package_uid("", "dev.example.app"), None);
    }

    #[test]
    fn app_languages_are_validated_and_read_back() {
        assert!(locale_tags("de-DE,fr,zh-Hant-TW").is_ok());
        assert!(locale_tags("").is_ok());
        for bad in ["de DE", "de;reboot", "-de", "de,,fr", "d"] {
            assert!(locale_tags(bad).is_err(), "{bad}");
        }
        assert_eq!(
            app_locales("Locales for dev.example.app for user 0 are [de-DE,fr]\n").as_deref(),
            Some("de-DE,fr")
        );
        assert_eq!(
            app_locales("Locales for dev.example.app for user 0 are []").as_deref(),
            Some("")
        );
        assert_eq!(
            app_locales("Unknown package dev.example.app for userId 0"),
            None
        );
        assert_eq!(scale("null"), 1.0);
        assert_eq!(scale("0.0"), 0.0);
    }

    #[test]
    fn emulator_console_tables_and_fields_are_read() {
        let list = "List of snapshots present on all disks:\nID        TAG                 VM SIZE                DATE       VM CLOCK\n--        default_boot        102M 2026-10-31 19:40:01   00:02:10.512\n--        before-login        98M 2026-10-31 19:42:12   00:03:00.001";
        assert_eq!(snapshot_names(list), ["default_boot", "before-login"]);
        assert!(snapshot_names("There is no snapshot available.").is_empty());
        let power = "AC: online\nstatus: Charging\nhealth: Good\npresent: true\ncapacity: 42";
        let fields = console_fields(power);
        assert_eq!(fields["capacity"], "42");
        assert_eq!(fields["AC"], "online");
        let network = "Current network status:\n  download speed:   173000000 bits/s (21118.2 KB/s)\n  minimum latency:  35 ms";
        assert_eq!(console_fields(network)["minimum_latency"], "35 ms");
        assert!(
            console_fields(network)
                .get("Current_network_status")
                .is_none()
        );
    }
}
