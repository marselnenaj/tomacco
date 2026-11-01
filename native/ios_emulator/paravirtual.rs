//! Agent-owned launch of the real PCC virtual platform, independent of app qualification.
use super::*;

const PATHS: &[&str] = &[
    "engine",
    "guest_control_backend",
    "guest_network_backend",
    "kernel",
    "sptm",
    "txm",
    "device_tree",
    "ramdisk",
    "trustcache",
    "boot_manifest",
    "system_volume_root_hash",
    "sep",
    "sep_manifest",
    "sep_storage",
    "auxiliary_disk",
    "root_disk",
    "writable_auxiliary_disk",
    "writable_root_disk",
];

/// The TCG profile report's name in the run's directory (the launcher places
/// a relative --tcg-profile path in its --output).
const TCG_PROFILE: &str = "tcg-profile.json";

pub fn start(a: &Value) -> Result {
    if a.get("host_cpu_policy").is_some()
        && !matches!(s(a, "host_cpu_policy"), "scheduler" | "performance")
    {
        return Err(Error::new(
            "invalid_host_cpu_policy",
            "host_cpu_policy must be scheduler or performance",
        ));
    }
    if let Some(size) = a.get("translation_cache_mib") {
        if !size.as_u64().is_some_and(|n| (1..=4096).contains(&n)) {
            return Err(Error::new(
                "invalid_translation_cache",
                "translation_cache_mib must be an integer between 1 and 4096; requests above 2048 require a range-independent engine chain capability",
            ));
        }
    }
    if let Some(profile) = a.get("display_profile") {
        validate_display_profile(profile.as_str().unwrap_or_default())?;
    }
    // The opt-in engine patches, CPU properties fixed at start (the
    // launcher's --bti, --jump-probe, --wfe-wait-us); omitted keys keep the
    // engine's own behaviour.
    if a.get("guest_network").is_some() && !matches!(s(a, "guest_network"), "off" | "nat") {
        return Err(Error::new(
            "invalid_guest_network",
            "guest_network must be off or nat",
        ));
    }
    if a.get("guest_memory_import").is_some()
        && !matches!(s(a, "guest_memory_import"), "shared" | "dma-buf")
    {
        return Err(Error::new(
            "invalid_engine_option",
            "guest_memory_import must be shared or dma-buf",
        ));
    }
    if a.get("guest_network_backend").is_some() && s(a, "guest_network") != "nat" {
        return Err(Error::new(
            "invalid_guest_network",
            "guest_network_backend is the passt of guest_network nat",
        ));
    }
    if a.get("bti").is_some() && !matches!(s(a, "bti"), "keep" | "off") {
        return Err(Error::new(
            "invalid_engine_option",
            "bti must be keep or off",
        ));
    }
    if a.get("jump_probe").is_some()
        && !matches!(s(a, "jump_probe"), "engine" | "generic" | "lean" | "inline")
    {
        return Err(Error::new(
            "invalid_engine_option",
            "jump_probe must be engine, generic, lean or inline",
        ));
    }
    if let Some(wait) = a.get("wfe_wait_us") {
        if !wait.as_u64().is_some_and(|n| n <= 100_000) {
            return Err(Error::new(
                "invalid_engine_option",
                "wfe_wait_us must be an integer between 0 and 100000 microseconds",
            ));
        }
    }
    // qemu-arm-target-page-16k: the engine's target page size (the launcher's
    // --target-page); omitted keeps the board's.
    if a.get("target_page").is_some() && !matches!(s(a, "target_page"), "engine" | "4k" | "16k") {
        return Err(Error::new(
            "invalid_engine_option",
            "target_page must be engine, 4k or 16k",
        ));
    }
    // Diagnostic: the launcher's TCG profile plugin, its report written in
    // the run's own directory (tcg-profile.json, rendered to tcg-profile.txt).
    if a.get("tcg_profile").is_some_and(|v| !v.is_boolean()) {
        return Err(Error::new(
            "invalid_tcg_profile",
            "tcg_profile must be true or false",
        ));
    }
    if let Some(seconds) = a.get("tcg_profile_interval") {
        if !b(a, "tcg_profile")
            || !seconds
                .as_f64()
                .is_some_and(|n| (1.0..=3600.0).contains(&n))
        {
            return Err(Error::new(
                "invalid_tcg_profile",
                "tcg_profile_interval must be a number of seconds between 1 and 3600, with tcg_profile=true",
            ));
        }
    }
    // A device model brings its own panel; a different explicit panel would
    // describe a device that does not exist.
    let model = match a.get("device_model") {
        Some(id) => Some(device_model(id.as_str().unwrap_or_default())?),
        None => None,
    };
    if let Some(model) = &model {
        if a.get("display_profile")
            .is_some_and(|panel| *panel != model["panel"])
        {
            return Err(Error::new(
                "conflicting_display_profile",
                "This device model has its own display; omit display_profile",
            )
            .details(json!({"panel":model["panel"]})));
        }
    }
    if s(a, "input_transport") == "usb-hid" && !b(a, "interactive") {
        return Err(Error::new(
            "invalid_interactive_guest",
            "USB HID input requires interactive=true",
        ));
    }
    let admission = Lock::open(&home()?.join("locks/ios-emulator-admission.lock"))?;
    admission.acquire(false)?;
    let mut key = a.clone();
    if let Some(model) = &model {
        key["display_profile"] = model["panel"].clone();
    }
    for name in PATHS {
        if a.get(*name).is_some() {
            key[*name] = json!(path(s(a, name), false, true)?);
        }
    }
    for kind in ["auxiliary", "root"] {
        if a.get(format!("{kind}_disk")).is_some()
            && a.get(format!("writable_{kind}_disk")).is_some()
        {
            return Err(Error::new(
                "conflicting_storage",
                "Select one access mode per virtual disk",
            ));
        }
    }
    if a.get("pcc_hostname").is_some()
        && (a.get("system_volume_root_hash").is_none()
            || (a.get("root_disk").is_none() && a.get("writable_root_disk").is_none())
            || s(a, "sep_boot_role") == "restore")
    {
        return Err(Error::new(
            "invalid_pcc_boot",
            "PCC configuration requires a normal sealed-system boot with a root disk",
        ));
    }
    let operation_key = json!({"operation":"ios.emulator.paravirtual","arguments":key});
    // Share admission with the other whole-system iOS jobs. Repeating the same
    // agent call reuses its live job instead of launching another VM.
    let exclusive = "tomacco-ios-kernel-probe";
    if let Some(mut active) = jobs::active_exclusive(exclusive)? {
        if !b(&active, "cancelling") && active["plan"]["operation_key"] == operation_key {
            active["reused"] = json!(true);
            active["report_directory"] = active["plan"]["cwd"].clone();
            return Ok(active);
        }
        return Err(
            Error::new("ios_emulator_busy", "An iOS emulator job is already active")
                .details(json!({"job":active})),
        );
    }
    let directory = home()?
        .join("ios-emulator")
        .join(uuid::Uuid::new_v4().simple().to_string());
    private_dir(&directory)?;
    let output = directory.join("paravirtual");
    let mut argv = vec![
        toolchain::find("python3")?,
        research::script("run_ios_paravirt.py")?
            .to_string_lossy()
            .into_owned(),
    ];
    for name in PATHS.iter().copied().chain([
        "boot_args",
        "sep_boot_role",
        "sep_handoff",
        "pcc_hostname",
        "clock_mode",
        "host_cpu_policy",
        "input_transport",
        "display_profile",
        "bti",
        "jump_probe",
        "target_page",
        "guest_network",
        "guest_memory_import",
    ]) {
        if key.get(name).is_some() {
            argv.extend([
                format!("--{}", name.replace('_', "-")),
                s(&key, name).to_string(),
            ]);
        }
    }
    for name in [
        "usb",
        "window",
        "interactive",
        "trace_exceptions",
        "trace_storage",
        "sep_measure_manifest",
    ] {
        if b(&key, name) {
            argv.push(format!("--{}", name.replace('_', "-")));
        }
    }
    if let Some(size) = key.get("translation_cache_mib") {
        argv.extend(["--translation-cache-mib".into(), size.to_string()]);
    }
    if let Some(wait) = key.get("wfe_wait_us") {
        argv.extend(["--wfe-wait-us".into(), wait.to_string()]);
    }
    if b(&key, "tcg_profile") {
        argv.extend(["--tcg-profile".into(), TCG_PROFILE.into()]);
        if let Some(seconds) = key.get("tcg_profile_interval") {
            argv.extend(["--tcg-profile-interval".into(), seconds.to_string()]);
        }
    }
    if let Some(model) = &model {
        argv.extend(["--device-model".into(), model["description"].to_string()]);
    }
    // A session someone works in saves and stops itself before its limit.
    if b(&key, "interactive") && key.get("guest_control_backend").is_some() {
        argv.extend([
            "--cli".into(),
            crate::core::own_executable()?
                .to_string_lossy()
                .into_owned(),
        ]);
    }
    argv.extend([
        "--seconds".into(),
        n(&key, "seconds").to_string(),
        "--output".into(),
        output.to_string_lossy().into_owned(),
    ]);
    let mut job = jobs::submit(
        "Run virtual iOS platform",
        json!({
            "cwd":directory, "steps":[{"argv":argv}],
            // Observation time excludes immutable-input and disk provenance reads.
            // The worker additionally bounds those and reaps the whole process group.
            "timeout":n(&key,"seconds")+900, "exclusive":exclusive, "single_active":true,
            "operation_key":operation_key,
            "always_artifacts":[output.join("request.json"),output.join("result.json"),
                                output.join("uart.log"),output.join("host.log"),output.join("debug.log"),
                                output.join("renderer/fail.log"),output.join("renderer/draw.log"),
                                output.join(TCG_PROFILE),output.join("tcg-profile.txt")]
        }),
    )?;
    job["report_directory"] = json!(directory);
    Ok(job)
}

/// The devices the virtual iPhone can be described as: every built-in iPhone
/// profile with a `virtual` entry. `description` is what the launcher applies
/// to the device tree; `panel` is that model's display.
pub fn device_models() -> Vec<Value> {
    crate::catalog::builtin()
        .as_array()
        .into_iter()
        .flatten()
        .filter(|profile| profile["platform"] == "ios" && profile["virtual"].is_object())
        .map(|profile| {
            let entry = &profile["virtual"];
            let mut description = json!({"variant":entry["variant"]});
            for (from, to) in [
                ("artworkSubtype", "artwork_subtype"),
                ("cornerRadius", "corner_radius"),
                ("islandBottom", "island_bottom"),
                ("largeFormat", "large_format"),
            ] {
                if let Some(value) = entry.get(from) {
                    // A bottom edge of zero stands for "no island or notch".
                    description[to] = if from == "islandBottom" && *value == 0 {
                        Value::Null
                    } else {
                        value.clone()
                    };
                }
            }
            json!({"id":profile["id"],"name":profile["name"],"panel":entry["panel"],
                "source":entry["source"],"home_button":false,"description":description})
        })
        .collect()
}

fn device_model(id: &str) -> Result<Value> {
    device_models()
        .into_iter()
        .find(|model| model["id"] == id)
        .ok_or_else(|| {
            Error::new(
                "invalid_device_model",
                "This is not a device model the virtual iOS device can be described as",
            )
            .details(json!({"models":device_models().iter().map(|m| m["id"].clone()).collect::<Vec<_>>()}))
        })
}

/// Keep malformed geometry out of the job queue. The graphics configuration
/// owner validates the same contract when a standalone engine reads it.
fn validate_display_profile(raw: &str) -> std::result::Result<(), Error> {
    let invalid = || {
        Error::new(
            "invalid_display_profile",
            "Use WIDTHxHEIGHT@HZ:WIDTH_MMxHEIGHT_MM; pixels 64..8192, Hz 1..120, mm 10..2000, with matching pixel and physical aspect ratios",
        )
    };
    let numbers: Vec<_> = raw.split(['x', '@', ':']).collect();
    if numbers.len() != 5
        || numbers
            .iter()
            .any(|n| n.is_empty() || n.len() > 5 || !n.bytes().all(|b| b.is_ascii_digit()))
    {
        return Err(invalid());
    }
    let n = numbers
        .iter()
        .map(|n| n.parse::<u64>().map_err(|_| invalid()))
        .collect::<std::result::Result<Vec<_>, _>>()?;
    // Splitting numbers alone would accept reordered delimiters.
    if raw
        != format!(
            "{}x{}@{}:{}x{}",
            numbers[0], numbers[1], numbers[2], numbers[3], numbers[4]
        )
        || !(64..=8192).contains(&n[0])
        || !(64..=8192).contains(&n[1])
        || !(1..=120).contains(&n[2])
        || !(10..=2000).contains(&n[3])
        || !(10..=2000).contains(&n[4])
        || (n[0] * n[4]).abs_diff(n[1] * n[3]) * 2 > n[0] + n[1]
    {
        return Err(invalid());
    }
    Ok(())
}
