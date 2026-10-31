//! Agent admission for the original vphone restore service; no signing backend.
use super::*;

pub fn prepare(a: &Value) -> Result {
    let admission = Lock::open(&home()?.join("locks/ios-restore-prepare-admission.lock"))?;
    admission.acquire(false)?;
    let mut key = a.clone();
    for (name, directory) in [
        ("ramdisk", false),
        ("trustcache", false),
        ("support", true),
        ("helper_engine", false),
        ("helper_kernel", false),
        ("bios", true),
        ("output", true),
    ] {
        key[name] = json!(path(s(a, name), directory, name != "output")?);
    }
    if let Some(seal) = key.get_mut("research_seal") {
        for (name, directory) in [("firmware", true), ("manifest", false)] {
            seal[name] = json!(path(s(seal, name), directory, true)?);
        }
        if seal.get("userland").is_some() {
            seal["userland"] = json!(path(s(seal, "userland"), true, true)?);
        }
        if let Some(overrides) = seal.get_mut("overrides").and_then(Value::as_array_mut) {
            let mut names = std::collections::HashSet::new();
            for entry in overrides.iter_mut() {
                if !names.insert(s(entry, "component").to_string()) {
                    return Err(Error::new(
                        "duplicate_component",
                        "Research seal overrides must be unique",
                    ));
                }
                entry["path"] = json!(path(s(entry, "path"), false, true)?);
            }
            overrides.sort_by_key(|entry| s(entry, "component").to_string());
        }
    }
    let output = PathBuf::from(s(&key, "output"));
    let exclusive = format!("ios-restore-prepare:{}", output.display());
    let operation_key = json!({"operation":"ios.emulator.prepare_restore","arguments":key});
    if let Some(mut active) = jobs::active_exclusive(&exclusive)? {
        if !b(&active, "cancelling") && active["plan"]["operation_key"] == operation_key {
            active["reused"] = json!(true);
            return Ok(active);
        }
        return Err(Error::new(
            "ios_prepare_busy",
            "This restore output already has an active preparation",
        )
        .details(json!({"job":active})));
    }
    if output.exists() || output.is_symlink() {
        return Err(Error::new(
            "output_exists",
            "Choose a new restore preparation directory",
        ));
    }
    let mut argv = vec![
        toolchain::find("python3")?,
        research::script("prepare_ios_paravirt_restore.py")?
            .to_string_lossy()
            .into_owned(),
    ];
    for name in [
        "ramdisk",
        "trustcache",
        "support",
        "helper_engine",
        "helper_kernel",
        "bios",
        "output",
        "accel",
    ] {
        argv.extend([
            format!("--{}", name.replace('_', "-")),
            s(&key, name).to_string(),
        ]);
    }
    if let Some(seal) = key.get("research_seal") {
        for name in ["firmware", "manifest"] {
            argv.extend([format!("--research-seal-{name}"), s(seal, name).into()]);
        }
        if seal.get("userland").is_some() {
            argv.extend([
                "--research-seal-userland".into(),
                s(seal, "userland").into(),
            ]);
        }
        argv.extend([
            "--research-seal-identity".into(),
            n(seal, "identity").to_string(),
        ]);
        for entry in seal["overrides"].as_array().into_iter().flatten() {
            argv.extend([
                "--research-seal-override".into(),
                format!("{}={}", s(entry, "component"), s(entry, "path")),
            ]);
        }
    }
    fs::create_dir_all(output.parent().unwrap())?;
    jobs::submit(
        "Prepare virtual iOS restore runtime",
        json!({
            "cwd":home()?, "timeout":900, "exclusive":exclusive, "single_active":true,
            "operation_key":operation_key, "steps":[{"argv":argv}],
            "always_artifacts":[output.join("restore-runtime.json"), output.join("extract/helper.log"),
                                output.join("inject/helper.log")]
        }),
    )
}

pub fn start(a: &Value) -> Result {
    let admission = Lock::open(&home()?.join("locks/ios-restore-admission.lock"))?;
    admission.acquire(false)?;
    let mut key = a.clone();
    for name in ["bridge", "firmware", "manifest", "system_image"] {
        key[name] = json!(path(s(a, name), name == "firmware", true)?);
    }
    if a.get("userland").is_some() {
        key["userland"] = json!(path(s(a, "userland"), true, true)?);
    }
    let bridge_path = PathBuf::from(s(&key, "bridge"));
    if fs::metadata(&bridge_path)?.len() > 1024 * 1024 {
        return Err(Error::new(
            "invalid_restore_bridge",
            "Bridge receipt is too large",
        ));
    }
    let bridge = read_json(&bridge_path, None)?;
    if bridge["state"] != "ready"
        || bridge["local_only"] != true
        || bridge["physical_devices_exposed"] != false
    {
        return Err(Error::new(
            "invalid_restore_bridge",
            "Select a ready private vphone bridge; physical-device restore is not supported",
        ));
    }
    let request = path(s(&bridge, "request"), false, true)?;
    let exclusive = format!("ios-paravirt-restore:{}", request.display());
    if let Some(overrides) = key.get_mut("overrides").and_then(Value::as_array_mut) {
        let mut names = std::collections::HashSet::new();
        for entry in overrides.iter_mut() {
            if !names.insert(s(entry, "component").to_string()) {
                return Err(Error::new(
                    "duplicate_component",
                    "Firmware overrides must be unique",
                ));
            }
            entry["path"] = json!(path(s(entry, "path"), false, true)?);
        }
        overrides.sort_by_key(|entry| s(entry, "component").to_string());
    }
    let operation_key = json!({"operation":"ios.emulator.restore","arguments":key});
    if let Some(mut active) = jobs::active_exclusive(&exclusive)? {
        if !b(&active, "cancelling") && active["plan"]["operation_key"] == operation_key {
            active["reused"] = json!(true);
            active["report_directory"] = active["plan"]["cwd"].clone();
            return Ok(active);
        }
        return Err(Error::new(
            "ios_restore_busy",
            "This virtual guest already has an active restore",
        )
        .details(json!({"job":active})));
    }
    let mut argv = vec![
        toolchain::find("python3")?,
        research::script("ios_paravirt_restore.py")?
            .to_string_lossy()
            .into_owned(),
    ];
    for (name, flag) in [
        ("bridge", "bridge"),
        ("firmware", "firmware"),
        ("manifest", "manifest"),
        ("system_image", "system-image"),
    ] {
        argv.extend([format!("--{flag}"), s(&key, name).to_string()]);
    }
    argv.extend([
        "--identity".into(),
        n(&key, "identity").to_string(),
        "--seconds".into(),
        n(&key, "seconds").to_string(),
    ]);
    if key.get("userland").is_some() {
        argv.extend(["--userland".into(), s(&key, "userland").into()]);
    }
    for entry in key["overrides"].as_array().into_iter().flatten() {
        argv.extend([
            "--override".into(),
            format!("{}={}", s(entry, "component"), s(entry, "path")),
        ]);
    }
    let directory = home()?
        .join("ios-emulator")
        .join(uuid::Uuid::new_v4().simple().to_string());
    private_dir(&directory)?;
    let output = directory.join("restore");
    argv.extend(["--output".into(), output.to_string_lossy().into_owned()]);
    let mut job = jobs::submit(
        "Restore virtual iOS system",
        json!({
            "cwd":directory, "steps":[{"argv":argv,
                "artifacts":[output.join("restore.json"),output.join("events.jsonl")]}],
            "timeout":n(&key,"seconds")+15, "exclusive":exclusive, "single_active":true,
            "operation_key":operation_key
        }),
    )?;
    job["report_directory"] = json!(directory);
    Ok(job)
}
