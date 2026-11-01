//! Offline build and installation of owned programs in a virtual iOS guest.
use super::*;

pub fn run(a: &Value, install: bool) -> Result {
    let admission = Lock::open(&home()?.join("locks/ios-guest-tools-admission.lock"))?;
    admission.acquire(false)?;
    let mut arguments = a.clone();
    let operation = if install {
        "ios.emulator.prepare_guest_tools"
    } else {
        "ios.emulator.build_guest_tools"
    };
    let script = if install {
        "prepare_ios_guest_tools.py"
    } else {
        "build_ios_guest_tools.py"
    };
    let report = if install {
        "guest-tools-install.json"
    } else {
        "guest-tools.json"
    };
    let mut fields = vec!["output", "trustcache"];
    if install {
        fields.extend([
            "disk",
            "auxiliary_disk",
            "sep_storage",
            "tools",
            "device_tree",
            "support",
            "helper_engine",
            "helper_kernel",
            "bios",
        ]);
    }
    for name in &fields {
        let directory = ["output", "tools", "support", "bios"].contains(name);
        arguments[*name] = json!(path(s(a, name), directory, *name != "output")?);
    }
    if install && a.get("previous_tools").is_some() {
        arguments["previous_tools"] = json!(path(s(a, "previous_tools"), true, true)?);
        fields.push("previous_tools");
    }
    if install && a.get("previous_trustcache").is_some() {
        if a.get("previous_tools").is_none() {
            return Err(Error::new(
                "invalid_guest_tools_request",
                "A previous trust cache requires the corresponding previous tools",
            ));
        }
        arguments["previous_trustcache"] = json!(path(s(a, "previous_trustcache"), false, true)?);
        fields.push("previous_trustcache");
    }
    if install && a.get("graphics").is_some() {
        arguments["graphics"] = json!(path(s(a, "graphics"), true, true)?);
        fields.push("graphics");
    }
    if install && a.get("previous_graphics").is_some() {
        if a.get("graphics").is_none() {
            return Err(Error::new(
                "invalid_guest_tools_request",
                "Previous graphics requires the new graphics payload",
            ));
        }
        arguments["previous_graphics"] = json!(path(s(a, "previous_graphics"), true, true)?);
        fields.push("previous_graphics");
    }
    if install
        && a["qemu_img"]
            .as_str()
            .is_some_and(|value| !value.is_empty())
    {
        arguments["qemu_img"] = json!(path(s(a, "qemu_img"), false, true)?);
    }
    let mut application_argv = Vec::new();
    if !install {
        for name in ["clang", "linker", "ldid"] {
            arguments[name] = json!(path(&toolchain::find(s(a, name))?, false, true)?);
            fields.push(name);
        }
        fields.push("minimum_ios");
        // Developer app bundles to stage, each with an optional entitlements
        // plist; the build inspects, signs and admits them.
        if let Some(rows) = a.get("applications") {
            let mut normalized = Vec::new();
            for row in rows.as_array().cloned().unwrap_or_default() {
                let bundle = path(s(&row, "bundle"), true, true)?;
                let entitlements = match row["entitlements"].as_str() {
                    Some(file) if !file.is_empty() => Some(path(file, false, true)?),
                    _ => None,
                };
                application_argv.extend([
                    "--application".to_string(),
                    bundle.to_string_lossy().into_owned(),
                    "--application-entitlements".to_string(),
                    entitlements
                        .as_ref()
                        .map(|file| file.to_string_lossy().into_owned())
                        .unwrap_or_default(),
                ]);
                normalized.push(json!({"bundle":bundle,"entitlements":entitlements}));
            }
            if !normalized.is_empty() && !b(&arguments, "development_signing") {
                return Err(Error::new(
                    "invalid_argument",
                    "Staging app bundles requires development_signing",
                ));
            }
            arguments["applications"] = json!(normalized);
        }
    } else {
        fields.push("accel");
    }
    let output = PathBuf::from(s(&arguments, "output"));
    // Both operations share this reservation: a build and an installation must
    // never compete to populate the same destination.
    let exclusive = format!("ios-guest-tools:{}", output.display());
    let operation_key = json!({"operation":operation,"arguments":arguments});
    let describe = |mut job: Value| {
        job["report_directory"] = json!(output);
        job["guest_tools_report"] = json!(output.join(report));
        job
    };
    if let Some(mut active) = jobs::active_exclusive(&exclusive)? {
        if !b(&active, "cancelling") && active["plan"]["operation_key"] == operation_key {
            active["reused"] = json!(true);
            return Ok(describe(active));
        }
        return Err(Error::new(
            "ios_prepare_busy",
            "This guest-tools output has an active job",
        )
        .details(json!({"job":active})));
    }
    if install && b(&arguments, "resume") && (output.is_symlink() || !output.is_dir()) {
        return Err(Error::new(
            "invalid_guest_tools_request",
            "Resume requires an existing preparation directory",
        ));
    }
    if (output.exists() || output.is_symlink()) && !(install && b(&arguments, "resume")) {
        return Err(Error::new(
            "output_exists",
            "Choose a new guest-tools output directory",
        ));
    }
    let mut argv = vec![
        toolchain::find("python3")?,
        research::script(script)?.to_string_lossy().into_owned(),
    ];
    for name in fields {
        argv.extend([
            format!("--{}", name.replace('_', "-")),
            s(&arguments, name).into(),
        ]);
    }
    let timeout = if install {
        if b(&arguments, "resume") {
            argv.push("--resume".into());
        }
        if b(&arguments, "development_device") {
            argv.push("--development-device".into());
        }
        if b(&arguments, "root_overlay") {
            if !text_set(&arguments, "qemu_img") {
                return Err(Error::new(
                    "invalid_argument",
                    "root_overlay needs qemu_img, the qemu-img that makes the overlay",
                ));
            }
            argv.push("--root-overlay".into());
        }
        if text_set(&arguments, "qemu_img") {
            argv.extend(["--qemu-img".into(), s(&arguments, "qemu_img").into()]);
        }
        argv.extend(["--seconds".into(), n(&arguments, "seconds").to_string()]);
        n(&arguments, "seconds") + 15
    } else {
        if b(&arguments, "application_probe") {
            argv.push("--application-probe".into());
        }
        if b(&arguments, "agent") {
            argv.push("--agent".into());
        }
        if b(&arguments, "graphics_query_access") {
            if !b(&arguments, "agent") {
                return Err(Error::new(
                    "invalid_guest_tools_request",
                    "Graphics query access requires the owned agent",
                ));
            }
            argv.push("--graphics-query-access".into());
        }
        if b(&arguments, "development_signing") {
            if !b(&arguments, "agent") || !b(&arguments, "application_probe") {
                return Err(Error::new(
                    "invalid_argument",
                    "Development signing requires agent and application_probe",
                ));
            }
            argv.push("--development-signing".into());
        }
        if b(&arguments, "quiet_services") {
            argv.push("--quiet-services".into());
        }
        argv.extend(application_argv);
        900
    };
    fs::create_dir_all(output.parent().unwrap())?;
    let job = jobs::submit(
        if install {
            "Install native iOS guest tools into a private copy"
        } else {
            "Build native iOS guest tools on Linux"
        },
        json!({"cwd":home()?, "timeout":timeout, "exclusive":exclusive,
            "single_active":true, "operation_key":operation_key, "steps":[{"argv":argv}],
            "always_artifacts":if install { vec![output.join(report), output.join("helper/helper.log")] }
                else { vec![output.join(report)] }}),
    )?;
    Ok(describe(job))
}

fn text_set(value: &Value, key: &str) -> bool {
    value[key].as_str().is_some_and(|text| !text.is_empty())
}
