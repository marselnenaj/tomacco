//! Reproducible, offline import of original iPhone userland and PCC GPU assets.
use super::*;

pub fn prepare_cryptexes(a: &Value) -> Result {
    let research_inputs = ["kernel", "boot_manifest", "device_tree"];
    let research_count = research_inputs
        .iter()
        .filter(|name| a.get(**name).is_some())
        .count();
    if research_count != 0 && research_count != research_inputs.len() {
        return Err(Error::new(
            "incomplete_research_inputs",
            "Research graft preparation requires kernel, boot_manifest and device_tree together",
        ));
    }
    let graphics = a.get("gpu_trustcache").is_some();
    if graphics && research_count != research_inputs.len() {
        return Err(Error::new(
            "incomplete_research_inputs",
            "GPU preparation requires kernel, boot_manifest and device_tree together",
        ));
    }
    let installer_control = b(a, "installer_control");
    if installer_control && research_count != research_inputs.len() {
        return Err(Error::new(
            "incomplete_research_inputs",
            "Installer control requires kernel, boot_manifest and device_tree together",
        ));
    }
    let development = a.get("development_tools").is_some();
    if development && (!graphics || !installer_control) {
        return Err(Error::new(
            "incomplete_research_inputs",
            "Development mapping requires gpu_trustcache and installer_control",
        ));
    }
    let admission = Lock::open(&home()?.join("locks/ios-cryptex-admission.lock"))?;
    admission.acquire(false)?;
    let mut arguments = a.clone();
    for (name, directory) in [
        ("disk", false),
        ("userland", true),
        ("support", true),
        ("helper_engine", false),
        ("helper_kernel", false),
        ("bios", true),
        ("output", true),
    ] {
        arguments[name] = json!(path(s(a, name), directory, name != "output")?);
    }
    if research_count != 0 {
        for name in research_inputs {
            arguments[name] = json!(path(s(a, name), false, true)?);
        }
    }
    if graphics {
        arguments["gpu_trustcache"] = json!(path(s(a, "gpu_trustcache"), false, true)?);
    }
    if development {
        arguments["development_tools"] = json!(path(s(a, "development_tools"), true, true)?);
    }
    let output = PathBuf::from(s(&arguments, "output"));
    let exclusive = format!("ios-cryptex:{}", output.display());
    let operation_key = json!({"operation":"ios.emulator.prepare_cryptexes","arguments":arguments});
    let describe = |mut job: Value| {
        job["report_directory"] = json!(output);
        job["cryptex_report"] = json!(output.join("cryptex.json"));
        job
    };
    if let Some(mut active) = jobs::active_exclusive(&exclusive)? {
        if !b(&active, "cancelling") && active["plan"]["operation_key"] == operation_key {
            active["reused"] = json!(true);
            return Ok(describe(active));
        }
        return Err(Error::new(
            "ios_prepare_busy",
            "This Cryptex output already has an active job",
        )
        .details(json!({"job":active})));
    }
    if output.exists() || output.is_symlink() {
        return Err(Error::new(
            "output_exists",
            "Choose a new Cryptex output directory",
        ));
    }
    let mut argv = vec![
        toolchain::find("python3")?,
        research::script("prepare_ios_cryptex.py")?
            .to_string_lossy()
            .into_owned(),
    ];
    for name in [
        "disk",
        "userland",
        "support",
        "helper_engine",
        "helper_kernel",
        "bios",
        "output",
        "accel",
    ] {
        argv.extend([
            format!("--{}", name.replace('_', "-")),
            s(&arguments, name).to_string(),
        ]);
    }
    argv.extend(["--seconds".into(), n(&arguments, "seconds").to_string()]);
    if research_count != 0 {
        for name in research_inputs {
            argv.extend([
                format!("--{}", name.replace('_', "-")),
                s(&arguments, name).to_string(),
            ]);
        }
    }
    if graphics {
        argv.extend([
            "--gpu-trustcache".into(),
            s(&arguments, "gpu_trustcache").to_string(),
        ]);
    }
    if installer_control {
        argv.push("--installer-control".into());
    }
    if development {
        argv.extend([
            "--development-tools".into(),
            s(&arguments, "development_tools").to_string(),
        ]);
    }
    fs::create_dir_all(output.parent().unwrap())?;
    let job = jobs::submit(
        "Install iOS Cryptex files into private disk copy",
        json!({
            "cwd":home()?, "timeout":n(&arguments,"seconds")+15,
            "exclusive":exclusive, "single_active":true, "operation_key":operation_key,
            "steps":[{"argv":argv}], "always_artifacts":[output.join("cryptex.json"),
                output.join("helper/helper.log"), output.join("os-files.sha256"), output.join("app-files.sha256")]
        }),
    )?;
    Ok(describe(job))
}

pub fn build_gpu_plugin(a: &Value) -> Result {
    let admission = Lock::open(&home()?.join("locks/ios-gpu-plugin-admission.lock"))?;
    admission.acquire(false)?;
    let mut arguments = a.clone();
    for name in ["source", "output"] {
        arguments[name] = json!(path(s(a, name), true, name != "output")?);
    }
    for name in ["clang", "linker", "nm", "ldid"] {
        arguments[name] = json!(path(&toolchain::find(s(a, name))?, false, true)?);
    }
    let output = PathBuf::from(s(&arguments, "output"));
    let exclusive = format!("ios-gpu-plugin:{}", output.display());
    let operation_key = json!({"operation":"ios.emulator.build_gpu_plugin","arguments":arguments});
    let describe = |mut job: Value| {
        job["report_directory"] = json!(output);
        job["plugin_report"] = json!(output.join("plugin.json"));
        job
    };
    if let Some(mut active) = jobs::active_exclusive(&exclusive)? {
        if !b(&active, "cancelling") && active["plan"]["operation_key"] == operation_key {
            active["reused"] = json!(true);
            return Ok(describe(active));
        }
        return Err(Error::new(
            "ios_prepare_busy",
            "This GPU plugin output already has an active build",
        )
        .details(json!({"job":active})));
    }
    if output.exists() || output.is_symlink() {
        return Err(Error::new(
            "output_exists",
            "Choose a new GPU plugin output directory",
        ));
    }
    let mut argv = vec![
        toolchain::find("python3")?,
        research::script("build_ios_gpu_plugin.py")?
            .to_string_lossy()
            .into_owned(),
    ];
    for name in ["source", "output", "clang", "linker", "nm", "ldid"] {
        argv.extend([format!("--{name}"), s(&arguments, name).to_string()]);
    }
    fs::create_dir_all(output.parent().unwrap())?;
    let job = jobs::submit(
        "Build iOS GPU compiler plugin",
        json!({
            "cwd":home()?, "timeout":360, "exclusive":exclusive, "single_active":true,
            "operation_key":operation_key, "steps":[{"argv":argv}],
            "always_artifacts":[output.join("plugin.json"), output.join("build.log")]
        }),
    )?;
    Ok(describe(job))
}

pub fn prepare(a: &Value) -> Result {
    let admission = Lock::open(&home()?.join("locks/ios-userland-admission.lock"))?;
    admission.acquire(false)?;
    let mut arguments = a.clone();
    for (name, directory) in [
        ("firmware", true),
        ("pcc_firmware", true),
        ("pcc_system_image", false),
        ("support", true),
        ("helper_engine", false),
        ("helper_kernel", false),
        ("bios", true),
        ("output", true),
    ] {
        arguments[name] = json!(path(s(a, name), directory, name != "output")?);
    }
    if a.get("gpu_plugin_build").is_some() {
        arguments["gpu_plugin_build"] = json!(path(s(a, "gpu_plugin_build"), true, true)?);
    }
    if let Some(overrides) = arguments
        .get_mut("components")
        .and_then(Value::as_array_mut)
    {
        let mut names = std::collections::HashSet::new();
        for entry in overrides.iter_mut() {
            if !names.insert(s(entry, "component").to_string()) {
                return Err(Error::new(
                    "duplicate_component",
                    "Userland component overrides must be unique",
                ));
            }
            entry["path"] = json!(path(s(entry, "path"), false, true)?);
        }
        overrides.sort_by_key(|entry| s(entry, "component").to_string());
    }
    let output = PathBuf::from(s(&arguments, "output"));
    let exclusive = format!("ios-userland:{}", output.display());
    let operation_key = json!({"operation":"ios.emulator.prepare_userland","arguments":arguments});
    let describe = |mut job: Value| {
        job["report_directory"] = json!(output);
        job["userland"] = json!(output.join("userland.json"));
        job
    };
    if let Some(mut active) = jobs::active_exclusive(&exclusive)? {
        if !b(&active, "cancelling") && active["plan"]["operation_key"] == operation_key {
            active["reused"] = json!(true);
            return Ok(describe(active));
        }
        return Err(Error::new(
            "ios_prepare_busy",
            "This userland output already has an active import",
        )
        .details(json!({"job":active})));
    }
    if output.exists() || output.is_symlink() {
        return Err(Error::new(
            "output_exists",
            "Choose a new userland output directory",
        ));
    }
    let mut argv = vec![
        toolchain::find("python3")?,
        research::script("prepare_ios_userland.py")?
            .to_string_lossy()
            .into_owned(),
    ];
    for name in [
        "firmware",
        "pcc_firmware",
        "pcc_system_image",
        "support",
        "helper_engine",
        "helper_kernel",
        "bios",
        "output",
        "accel",
    ] {
        argv.extend([
            format!("--{}", name.replace('_', "-")),
            s(&arguments, name).to_string(),
        ]);
    }
    for name in ["identity", "pcc_identity", "seconds"] {
        argv.extend([
            format!("--{}", name.replace('_', "-")),
            n(&arguments, name).to_string(),
        ]);
    }
    if arguments.get("gpu_plugin_build").is_some() {
        argv.extend([
            "--gpu-plugin-build".into(),
            s(&arguments, "gpu_plugin_build").to_string(),
        ]);
    }
    for entry in arguments["components"].as_array().into_iter().flatten() {
        argv.extend([
            "--component".into(),
            format!("{}={}", s(entry, "component"), s(entry, "path")),
        ]);
    }
    fs::create_dir_all(output.parent().unwrap())?;
    let job = jobs::submit(
        "Import original iOS userland",
        json!({
            "cwd":home()?, "timeout":n(&arguments,"seconds")+15,
            "exclusive":exclusive, "single_active":true,
            "operation_key":operation_key, "steps":[{"argv":argv}],
            "always_artifacts":[output.join("userland.json"), output.join("gpu-extract/helper.log")]
        }),
    )?;
    Ok(describe(job))
}
