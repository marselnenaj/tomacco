//! One-step start and stop of a configured virtual iPhone.
//!
//! The profile directory holds the static inputs (`profile.json`) and the
//! current run (`state.json`). The orchestration itself is a durable job that
//! only calls public operations, so the desktop, the CLI and agents observe
//! one shared state.
use super::*;

const SCRIPT: &str = "ios_virtual_iphone.py";
const EXCLUSIVE: &str = "ios-virtual-iphone";
const REMEDY_NO_PROFILE: &str = "Write a profile with tools/ios_virtual_profile.py, then call ios.virtual.configure with its directory";
/// The owned diagnostic app every development-signing guest-tools build carries
/// (tools/build_ios_research_bootstrap.py application_info).
const RESEARCH_APPLICATION: &str = "io.github.marselnenaj.tomacco.research-app";

fn location() -> Result<PathBuf> {
    Ok(home()?.join("ios-virtual.json"))
}

fn text(value: &Value, key: &str) -> bool {
    value[key].as_str().is_some_and(|text| !text.is_empty())
}

/// The developer apps a profile names (`applications`: objects with the
/// host path of an `.app` bundle), or none. The start installs them from the
/// guest-tools build that carries them; the list itself names only paths.
fn applications(profile: &Value) -> Result<Vec<Value>> {
    match profile.get("applications") {
        None | Some(Value::Null) => Ok(Vec::new()),
        Some(Value::Array(rows))
            if rows
                .iter()
                .all(|row| row.is_object() && text(row, "bundle")) =>
        {
            Ok(rows.clone())
        }
        _ => Err(Error::new(
            "ios_virtual_profile_invalid",
            "profile.json: applications must be a list of {bundle: path to an .app directory}",
        )),
    }
}

/// Reads a bundle the way the start does (tools/ios_application_bundle.py),
/// so a bundle that cannot be deployed is refused when it is added, with
/// the reason, instead of at the next start.
pub(crate) fn inspect_application(bundle: &Path, entitlements: Option<&Path>) -> Result {
    let mut argv = vec![
        toolchain::find("python3")?,
        research::script("ios_application_bundle.py")?
            .to_string_lossy()
            .into_owned(),
        "inspect".into(),
        bundle.to_string_lossy().into_owned(),
    ];
    if let Some(file) = entitlements {
        argv.extend(["--entitlements".into(), file.to_string_lossy().into_owned()]);
    }
    let done = toolchain::run(&argv, 120, None, false)?;
    let reply: Value = serde_json::from_str(s(&done, "stdout").trim()).unwrap_or(Value::Null);
    if reply["ok"] != true {
        let message = reply["error"]
            .as_str()
            .map(str::to_string)
            .unwrap_or_else(|| {
                format!(
                    "The bundle inspector failed: {}",
                    s(&done, "stderr")
                        .chars()
                        .rev()
                        .take(400)
                        .collect::<String>()
                        .chars()
                        .rev()
                        .collect::<String>()
                )
            });
        return Err(
            Error::new("invalid_application_bundle", message).details(json!({"bundle":bundle}))
        );
    }
    Ok(reply)
}

/// Reads and checks both records. Nothing is repaired or defaulted here: a
/// profile that cannot be trusted must not start a guest.
pub(crate) fn records(directory: &Path) -> Result<(Value, Value)> {
    let invalid = |message: &str| {
        Error::new("ios_virtual_profile_invalid", message).details(json!({"directory":directory}))
    };
    if directory.is_symlink() || !directory.is_dir() {
        return Err(invalid(
            "The virtual iOS device profile must be a real directory",
        ));
    }
    let limit = |name: &str| -> Result<Value> {
        let file = directory.join(name);
        if file.is_symlink() || !file.is_file() || fs::metadata(&file)?.len() > 1024 * 1024 {
            return Err(invalid(
                "profile.json and state.json must be regular files of at most 1 MiB",
            ));
        }
        read_json(&file, None)
    };
    let profile = limit("profile.json")?;
    let state = limit("state.json")?;
    if profile["format"] != 1
        || !profile["prepare"].is_object()
        || !profile["boot"].is_object()
        || !["graphics", "engine", "application", "installs"]
            .iter()
            .all(|key| text(&profile, key))
    {
        return Err(invalid(
            "profile.json is not a format 1 virtual iOS device profile",
        ));
    }
    applications(&profile)?;
    if !state["run"].is_u64()
        || !state["closed"].is_boolean()
        || !text(&state, "install")
        || !text(&state, "tools")
    {
        return Err(invalid(
            "state.json does not describe a virtual iOS device run",
        ));
    }
    Ok((profile, state))
}

pub(crate) fn directory() -> Result<PathBuf> {
    let record = read_json(&location()?, Some(json!({})))?;
    if !text(&record, "directory") {
        return Err(Error::new(
            "ios_virtual_unconfigured",
            "No virtual iOS device profile is configured",
        )
        .remedy(REMEDY_NO_PROFILE));
    }
    Ok(PathBuf::from(s(&record, "directory")))
}

/// The device model the next start describes the phone as. Empty: the
/// profile's own description, a phone with a Home button.
fn model() -> Result<String> {
    Ok(s(&read_json(&location()?, Some(json!({})))?, "model").to_string())
}

/// The panel of a run as `WIDTHxHEIGHT@HZ:WIDTH_MMxHEIGHT_MM`. The guest
/// job's own request says what the engine was given (`paravirtual::start`
/// puts a model's panel there), so a profile or catalogue edited after the
/// boot cannot change the answer; a request without a panel left the engine
/// its default, which is unknown here (null). Without the guest job: the
/// model's own panel, or for the Home-button phone the profile's display
/// profile, which a model start leaves aside (`boot_request` in the script).
/// Null for a model this control plane does not know.
fn running_panel(profile: &Value, model: &str, guest: Option<&Value>) -> Value {
    if let Some(key) = guest
        .map(|job| &job["plan"]["operation_key"])
        .filter(|key| key["operation"] == "ios.emulator.paravirtual")
    {
        return key["arguments"]["display_profile"]
            .as_str()
            .map_or(Value::Null, Value::from);
    }
    if model.is_empty() {
        return profile["boot"]["display_profile"].clone();
    }
    paravirtual::device_models()
        .into_iter()
        .find(|known| known["id"] == model)
        .map(|known| known["panel"].clone())
        .unwrap_or(Value::Null)
}

pub fn configure(a: &Value) -> Result {
    let mut record = read_json(&location()?, Some(json!({})))?;
    if a.get("directory").is_some() {
        let directory = path(s(a, "directory"), true, true)?;
        records(&directory)?;
        if text(&record, "directory") && record["directory"] != json!(directory) {
            refuse_switch(Path::new(s(&record, "directory")))?;
        }
        record["directory"] = json!(directory);
        record["profiles"] = json!(remembered(&record, Some(&directory)));
    } else if !text(&record, "directory") {
        return Err(Error::new(
            "ios_virtual_unconfigured",
            "No virtual iOS device profile is configured",
        )
        .remedy(REMEDY_NO_PROFILE));
    }
    if let Some(model) = a.get("model") {
        let id = model.as_str().unwrap_or_default();
        if !id.is_empty() && !paravirtual::device_models().iter().any(|m| m["id"] == id) {
            return Err(Error::new(
                "invalid_device_model",
                "This is not a device model the virtual iOS device can be described as",
            ));
        }
        record["model"] = json!(id);
    }
    // The profile's own apps and the app the start opens live in
    // profile.json; only these fields of it are changed, atomically.
    if [
        "add_application",
        "remove_application",
        "application",
        "clang",
        "start_disks",
        "stall_seconds",
    ]
    .iter()
    .any(|key| a.get(key).is_some())
    {
        let directory = PathBuf::from(s(&record, "directory"));
        let (mut profile, _state) = records(&directory)?;
        let mut listed = applications(&profile)?;
        let mut removed = Vec::new();
        if a.get("add_application").is_some() {
            let bundle = path(s(a, "add_application"), true, true)?;
            let entitlements = if text(a, "add_entitlements") {
                Some(path(s(a, "add_entitlements"), false, true)?)
            } else {
                None
            };
            let inspected = inspect_application(&bundle, entitlements.as_deref())?;
            let id = s(&inspected, "bundle_id").to_string();
            if id == RESEARCH_APPLICATION {
                return Err(Error::new(
                    "invalid_application_bundle",
                    "This bundle uses the owned diagnostic app's bundle identifier; choose another",
                )
                .details(json!({"bundle":bundle,"bundle_id":id})));
            }
            if let Some(other) = listed
                .iter()
                .find(|row| row["bundle_id"] == id && row["bundle"] != json!(bundle))
            {
                return Err(Error::new(
                    "ios_virtual_application_conflict",
                    format!("Another listed bundle already uses the bundle identifier {id}"),
                )
                .details(json!({"bundle":other["bundle"],"bundle_id":id})));
            }
            let mut entry = json!({"bundle":bundle,"bundle_id":id,"name":inspected["name"],
                "display_name":inspected["display_name"]});
            if let Some(file) = entitlements {
                entry["entitlements"] = json!(file);
            }
            listed.retain(|row| row["bundle"] != json!(bundle));
            listed.push(entry);
        }
        if a.get("remove_application").is_some() {
            let target = s(a, "remove_application");
            let before = listed.len();
            listed.retain(|row| {
                let hit = row["bundle"] == target || row["bundle_id"] == target;
                if hit {
                    removed.push(row["bundle_id"].clone());
                }
                !hit
            });
            if listed.len() == before {
                return Err(Error::new(
                    "ios_virtual_application_unknown",
                    "No listed app bundle has this path or bundle identifier",
                )
                .details(json!({"applications":listed})));
            }
        }
        if a.get("application").is_some() {
            let id = s(a, "application");
            if id != RESEARCH_APPLICATION && !listed.iter().any(|row| row["bundle_id"] == id) {
                return Err(Error::new(
                    "invalid_application",
                    format!("{id} is not among the apps a start stages: {RESEARCH_APPLICATION} and the listed bundles"),
                )
                .details(json!({"applications":listed})));
            }
            profile["application"] = json!(id);
        } else if removed.contains(&profile["application"]) {
            // The app the start opened is gone; back to the owned diagnostic.
            profile["application"] = json!(RESEARCH_APPLICATION);
        }
        if a.get("clang").is_some() {
            // The clang 22 the start rebuilds guest tools with when they do
            // not carry a listed app; empty: found on PATH or in the record.
            let clang = s(a, "clang");
            if clang.is_empty() {
                if let Some(toolchain) = profile["toolchain"].as_object_mut() {
                    toolchain.remove("clang");
                }
            } else {
                let file = path(clang, false, true)?;
                let executable = fs::metadata(&file)
                    .map(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
                    .unwrap_or(false);
                if !executable {
                    return Err(Error::new(
                        "invalid_toolchain",
                        "clang must name an executable file (a clang 22 release)",
                    )
                    .details(json!({"clang":file})));
                }
                if !profile["toolchain"].is_object() {
                    profile["toolchain"] = json!({});
                }
                profile["toolchain"]["clang"] = json!(file);
            }
        }
        if let Some(mode) = a.get("start_disks") {
            // How the next start makes its root disk: a copy of the base's,
            // or a qcow2 overlay on it (disk_overlay).
            let mode = mode.as_str().unwrap_or_default();
            if !["copy", "overlay"].contains(&mode) {
                return Err(Error::new(
                    "invalid_argument",
                    "start_disks must be copy or overlay",
                ));
            }
            profile["start_disks"] = json!(mode);
        }
        if let Some(seconds) = a.get("stall_seconds") {
            // How long a running phone may show no sign of life before the
            // status calls it stalled (liveness).
            match seconds.as_u64() {
                Some(seconds) if liveness::STALL_SECONDS_RANGE.contains(&seconds) => {
                    profile["stall_seconds"] = json!(seconds);
                }
                _ => {
                    return Err(Error::new(
                        "invalid_argument",
                        "stall_seconds must be a whole number of seconds from 30 to 86400",
                    ));
                }
            }
        }
        profile["applications"] = json!(listed);
        atomic_json(&directory.join("profile.json"), &profile)?;
    }
    atomic_json(&location()?, &record)?;
    let mut reply = status(&json!({}))?;
    // The fast path: the app just added goes onto the running phone at once.
    if a["deploy"] == true {
        if !text(a, "add_application") {
            return Err(Error::new(
                "invalid_application",
                "deploy goes with add_application",
            ));
        }
        reply["deploy_job"] = deploy(&json!({"bundle":a["add_application"],
            "entitlements":a.get("add_entitlements").cloned().unwrap_or(json!("")),
            "open_app":a["deploy_open_app"].as_bool().unwrap_or(true)}))?;
    }
    Ok(reply)
}

/// The most profile directories `ios.virtual.profiles` remembers.
const PROFILES_KEPT: usize = 32;

/// The profile directories configured so far, newest last, with `added`
/// moved to the end; the current one is always among them.
fn remembered(record: &Value, added: Option<&Path>) -> Vec<PathBuf> {
    let mut list: Vec<PathBuf> = record["profiles"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|item| item.as_str().filter(|p| !p.is_empty()).map(PathBuf::from))
        .collect();
    if text(record, "directory") {
        list.push(PathBuf::from(s(record, "directory")));
    }
    if let Some(added) = added {
        list.retain(|item| item != added);
        list.push(added.to_path_buf());
    }
    let mut seen = Vec::new();
    for item in list.into_iter().rev() {
        if !seen.contains(&item) {
            seen.push(item);
        }
    }
    seen.truncate(PROFILES_KEPT);
    seen.reverse();
    seen
}

/// Another profile is chosen only while the current one is idle: no start
/// or stop running and no guest of it alive, so no run loses its profile.
fn refuse_switch(current: &Path) -> Result<()> {
    if let Some(job) = jobs::active_exclusive(EXCLUSIVE)? {
        return Err(Error::new(
            "ios_virtual_busy",
            "A start or stop is in progress; choose another profile when it has ended",
        )
        .details(json!({"job":job})));
    }
    if let Ok((_, state)) = records(current) {
        if guest_is_running(&state) {
            return Err(Error::new(
                "ios_virtual_running",
                "The phone of the current profile is running; stop it before choosing another profile",
            )
            .remedy("Call ios.virtual.stop, then ios.virtual.configure with the other directory")
            .details(json!({"directory":current,"guest_job":state["guest_job"]})));
        }
    }
    Ok(())
}

/// The profile directories configured so far, each with whether it can be
/// used and what it holds. Reads files only.
pub fn profiles(_a: &Value) -> Result {
    let record = read_json(&location()?, Some(json!({})))?;
    let current = text(&record, "directory").then(|| PathBuf::from(s(&record, "directory")));
    let busy = jobs::active_exclusive(EXCLUSIVE)?.is_some();
    let rows: Vec<Value> = remembered(&record, None)
        .into_iter()
        .rev()
        .map(|directory| {
            let selected = current.as_ref() == Some(&directory);
            match records(&directory) {
                Ok((profile, state)) => {
                    json!({"directory":directory,"current":selected,"valid":true,
                    "error":null,"run":state["run"],"phase":state["phase"],"closed":state["closed"],
                    "running":guest_is_running(&state),"application":profile["application"],
                    "installs":profile["installs"],
                    "start_disks":profile["start_disks"].as_str().unwrap_or("copy"),
                    "saved_states":states::saved(&directory).map(|m| m.len()).ok()})
                }
                Err(error) => json!({"directory":directory,"current":selected,"valid":false,
                    "error":{"code":error.code,"message":error.message},"running":false}),
            }
        })
        .collect();
    let running = rows
        .iter()
        .any(|row| row["current"] == true && row["running"] == true);
    Ok(json!({"current":current,"profiles":rows,"busy":busy,
        "can_switch":!busy && !running}))
}

/// The outcome of the start's time-zone step as the state records it
/// (`apply_time_zone` in the script): the host zone it offered, `applied`,
/// `skipped` with its `reason`, or the guest's error name, and for an
/// applied zone the link before and after and whether it changed. Null for
/// a state from before the step (runs up to 323) and for a record of
/// another shape; nothing is guessed from it.
fn time_zone(state: &Value) -> Value {
    let record = &state["time_zone"];
    if !record.is_object() || !text(record, "outcome") {
        return Value::Null;
    }
    let string = |key: &str| record[key].as_str().map_or(Value::Null, Value::from);
    json!({
        "zone":string("zone"),"outcome":string("outcome"),
        "previous_link":string("previous_link"),"link":string("link"),
        "changed":record["changed"].as_bool().map_or(Value::Null, Value::from),
        "reason":string("reason"),
    })
}

/// The developer apps installed on the current run's data volume, bundle
/// identifier to the cdhash of the installed executable (`install_applications`
/// in the script; a verified base hands its record on). Empty for a state
/// without the record, so a listed app that no start has installed yet
/// reads as not installed.
fn installed_applications(state: &Value) -> Value {
    match &state["applications"] {
        Value::Object(rows) => Value::Object(
            rows.iter()
                .filter(|(_, hash)| hash.is_string())
                .map(|(id, hash)| (id.clone(), hash.clone()))
                .collect(),
        ),
        _ => json!({}),
    }
}

/// What each listed bundle's Info.plist says now (tools/ios_ipa_inspect.py
/// `info`), by host path; empty when there is nothing to read or the reader
/// cannot run, so every bundle then reads as unknown rather than absent.
fn bundle_infos(bundles: &[String]) -> std::collections::HashMap<String, Value> {
    let mut found = std::collections::HashMap::new();
    if bundles.is_empty() {
        return found;
    }
    let reply = (|| -> Result {
        let mut argv = vec![
            toolchain::find("python3")?,
            "-I".into(),
            research::script("ios_ipa_inspect.py")?
                .to_string_lossy()
                .into_owned(),
            "info".into(),
        ];
        argv.extend(bundles.iter().cloned());
        let done = toolchain::run(&argv, 60, None, false)?;
        Ok(serde_json::from_str(s(&done, "stdout").trim()).unwrap_or(Value::Null))
    })()
    .unwrap_or(Value::Null);
    for row in reply["applications"].as_array().into_iter().flatten() {
        found.insert(s(row, "path").to_string(), row.clone());
    }
    found
}

/// The executable name of an app the profile lists, by bundle identifier
/// (any case), read from its bundle's Info.plist on the host.
pub(crate) fn listed_executable(bundle_id: &str) -> Result<String> {
    let (profile, _) = records(&directory()?)?;
    let bundles: Vec<String> = applications(&profile)?
        .iter()
        .map(|row| s(row, "bundle").to_string())
        .collect();
    executable_of(bundle_id, &bundles, &bundle_infos(&bundles))
}

fn executable_of(
    bundle_id: &str,
    bundles: &[String],
    infos: &std::collections::HashMap<String, Value>,
) -> Result<String> {
    bundles
        .iter()
        .filter_map(|bundle| infos.get(bundle))
        .find(|info| s(info, "bundle_id").eq_ignore_ascii_case(bundle_id))
        .and_then(|info| info["executable"].as_str().filter(|e| !e.is_empty()))
        .map(str::to_string)
        .ok_or_else(|| {
            Error::new(
                "ios_virtual_application_unknown",
                format!("The profile lists no readable app {bundle_id}"),
            )
            .remedy("Deploy it with ios.virtual.deploy or add it with ios.virtual.configure add_application; ios.virtual.apps lists the apps")
        })
}

/// One row per app the phone knows of: the owned diagnostic, each app the
/// profile lists (with the version its bundle on the host has now) and any
/// app installed on the current run's volume that the profile no longer
/// lists. `installed` is the run's record (a start's install or a hot
/// deploy); it names the cdhash installed, not whether the bundle changed
/// since, which the next start finds out itself.
fn application_rows(
    profile: &Value,
    state: &Value,
    infos: &std::collections::HashMap<String, Value>,
) -> Vec<Value> {
    let installed = installed_applications(state);
    let opens = profile["application"].as_str().unwrap_or_default();
    let mut rows = vec![
        json!({"bundle_id":RESEARCH_APPLICATION,"owned_diagnostic":true,
        "listed":false,"bundle":null,"name":null,"display_name":null,"version":null,"build":null,
        "minimum_os":null,"bundle_readable":null,"bundle_error":null,
        // Every guest-tools build carries it; the run's record does not list it.
        "installed":null,"installed_cdhash":null,"opens_at_start":opens == RESEARCH_APPLICATION}),
    ];
    let listed = profile["applications"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    for row in &listed {
        let id = s(row, "bundle_id");
        let info = infos.get(s(row, "bundle"));
        let field = |key: &str| {
            info.filter(|info| info["ok"] == true)
                .map_or(Value::Null, |info| info[key].clone())
        };
        let readable = info.map_or(Value::Null, |info| json!(info["ok"] == true));
        let error = info
            .filter(|info| info["ok"] != true)
            .map_or(Value::Null, |info| info["error"].clone());
        // The bundle on the host may now carry another identifier than the
        // one the profile recorded when it was added.
        let changed_id = field("bundle_id")
            .as_str()
            .is_some_and(|now| !id.is_empty() && now != id);
        rows.push(json!({"bundle_id":row["bundle_id"],"owned_diagnostic":false,"listed":true,
            "bundle":row["bundle"],"name":row["name"],"display_name":row["display_name"],
            "version":field("version"),"build":field("build"),"minimum_os":field("minimum_os"),
            "bundle_readable":readable,"bundle_error":error,
            "bundle_id_changed":changed_id,
            "entitlements":row.get("entitlements").cloned().unwrap_or(Value::Null),
            "installed":installed.get(id).is_some(),"installed_cdhash":installed.get(id).cloned().unwrap_or(Value::Null),
            "opens_at_start":!id.is_empty() && opens == id}));
    }
    if let Value::Object(map) = &installed {
        for (id, hash) in map {
            if listed.iter().any(|row| row["bundle_id"] == *id) {
                continue;
            }
            rows.push(
                json!({"bundle_id":id,"owned_diagnostic":false,"listed":false,
                "bundle":null,"name":null,"display_name":null,"version":null,"build":null,
                "minimum_os":null,"bundle_readable":null,"bundle_error":null,
                "installed":true,"installed_cdhash":hash,"opens_at_start":opens == id}),
            );
        }
    }
    rows
}

/// The apps of the configured phone in one list: what the profile lists,
/// each bundle's version on the host, and what the current run has
/// installed. Reads the profile, the state and the bundles' Info.plist only.
pub fn apps(_a: &Value) -> Result {
    let directory = directory()?;
    let (profile, state) = records(&directory)?;
    let bundles: Vec<String> = applications(&profile)?
        .iter()
        .map(|row| s(row, "bundle").to_string())
        .collect();
    let rows = application_rows(&profile, &state, &bundle_infos(&bundles));
    Ok(
        json!({"directory":directory,"run":state["run"],"guest_running":guest_is_running(&state),
        "application":profile["application"],"applications":rows,
        "applications_error":state.get("applications_error").filter(|v| v.is_string()).cloned().unwrap_or(Value::Null)}),
    )
}

/// The disks a start copies from its base, in the order it copies them
/// (`prepare` in tools/prepare_ios_guest_tools.py).
use super::disk_overlay;
pub mod doctor;
mod host_load;
mod liveness;
pub mod states;

/// The disks a start copies besides the root disk, which is a copy, an
/// overlay or a flattened overlay (`disk_overlay`).
const COPIED_DISKS: [&str; 2] = ["auxiliary.raw", "sep.raw"];

/// How far the `preparing` phase's disk copy has come: the bytes of the new
/// copy's disk files against those of the base it is copied from, and the
/// preparation's own step (`copy-storage`, `install-preboot`, ...) and disk
/// from its record. A streamed copy grows as it is written; a reflinked one
/// is complete at once. Null outside `preparing`, before the attempt is
/// recorded and when the base's disks cannot be read.
fn copy_progress(state: &Value) -> Value {
    if state["phase"] != "preparing" {
        return Value::Null;
    }
    let (Some(output), Some(base)) = (
        state["attempt"]["install"].as_str(),
        state["verified"]["install"].as_str(),
    ) else {
        return Value::Null;
    };
    let size = |path: PathBuf| {
        fs::symlink_metadata(path)
            .ok()
            .filter(|meta| meta.is_file())
            .map(|meta| meta.len())
    };
    let (base, output) = (Path::new(base), Path::new(output));
    // An overlay is complete once it exists; a copy or a flattened disk
    // grows to the base's bytes as a raw disk.
    let Some(root) = disk_overlay::root_bytes(base) else {
        return Value::Null;
    };
    let (mut done, mut total) = (0u64, root);
    done += if size(output.join(disk_overlay::ROOT_OVERLAY)).is_some() {
        root
    } else {
        size(output.join(disk_overlay::ROOT_RAW))
            .unwrap_or(0)
            .min(root)
    };
    for name in COPIED_DISKS {
        let Some(source) = size(base.join(name)) else {
            return Value::Null;
        };
        total += source;
        done += size(output.join(name)).unwrap_or(0).min(source);
    }
    let record = Path::new(output).join("guest-tools-install.json");
    let record = if fs::symlink_metadata(&record).is_ok_and(|meta| meta.is_file()) {
        read_json(&record, None).unwrap_or(Value::Null)
    } else {
        Value::Null
    };
    let text = |key: &str| record[key].as_str().map_or(Value::Null, Value::from);
    let percent = if total == 0 {
        0.0
    } else {
        (done as f64 * 1000.0 / total as f64).floor() / 10.0
    };
    json!({"bytes_done":done,"bytes_total":total,"percent":percent,
        "step":text("phase"),"disk":text("copying_disk")})
}

/// What a full disk copy needs beyond the disks themselves, as the copy
/// checks for each disk (`COPY_HEADROOM` in tools/prepare_ios_userland.py).
const COPY_HEADROOM: u64 = 1 << 30;

/// Whether a disk copy may be deleted by the start: only a real directory
/// the start created in the profile's `installs` (`Phone.owned`).
fn owned_copy(installs: &Path, copy: &Path) -> bool {
    let parent = copy.parent().and_then(|parent| parent.canonicalize().ok());
    parent.is_some()
        && parent == installs.canonicalize().ok()
        && copy
            .file_name()
            .is_some_and(|name| name.to_string_lossy().starts_with("guest-install"))
        && fs::symlink_metadata(copy).is_ok_and(|meta| meta.is_dir())
}

/// The copies a start deletes before it makes its own: the disk copy of an
/// attempt that never booted (`discard_attempt`) and every recorded copy but
/// the verified run's and the newest (`prune(keep=1)`), as far as they are
/// the start's own.
fn copies_a_start_removes(
    profile: &Value,
    state: &Value,
    pins: &[(PathBuf, String)],
) -> Vec<PathBuf> {
    let installs = PathBuf::from(s(profile, "installs"));
    let created: Vec<PathBuf> = state["created"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|path| path.as_str().map(PathBuf::from))
        .collect();
    let verified = PathBuf::from(s(&state["verified"], "install"));
    // What the start keeps (prune(keep=1)) and what those read as overlays.
    let mut keeps = vec![verified.clone()];
    keeps.extend(created.last().cloned());
    keeps.extend(pins.iter().map(|(path, _)| path.clone()));
    let kept = disk_overlay::with_backing(&keeps);
    let mut removed: Vec<PathBuf> = created
        .iter()
        .take(created.len().saturating_sub(1))
        .filter(|path| **path != verified && !disk_overlay::among(&kept, path))
        .cloned()
        .collect();
    if let Some(attempt) = state["attempt"]["install"].as_str().map(PathBuf::from) {
        removed.push(attempt);
    }
    removed.sort();
    removed.dedup();
    removed.retain(|path| owned_copy(&installs, path));
    removed
}

/// The bytes the regular files directly inside a directory occupy on disk.
fn allocated_bytes(directory: &Path) -> u64 {
    fs::read_dir(directory)
        .into_iter()
        .flatten()
        .flatten()
        .filter_map(|entry| fs::symlink_metadata(entry.path()).ok())
        .filter(|meta| meta.is_file())
        .map(|meta| meta.blocks() * 512)
        .sum()
}

/// Whether the filesystem of `installs` can clone `source` instead of
/// copying it (FICLONE), as the copy tries first. The probe clones the
/// base's smallest disk into a temporary file there and removes it again.
fn clones_into(source: &Path, installs: &Path) -> bool {
    use std::os::fd::AsRawFd;
    let Ok(input) = File::open(source) else {
        return false;
    };
    let Ok(output) = tempfile::Builder::new()
        .prefix(".tomacco-clone-probe")
        .tempfile_in(installs)
    else {
        return false;
    };
    unsafe { libc::ioctl(output.as_file().as_raw_fd(), 0x40049409, input.as_raw_fd()) == 0 }
}

/// Free bytes for an unprivileged writer on the filesystem of `path`.
pub(super) fn free_bytes(path: &Path) -> Option<u64> {
    use std::os::unix::ffi::OsStrExt;
    let name = std::ffi::CString::new(path.as_os_str().as_bytes()).ok()?;
    let mut space: libc::statvfs = unsafe { std::mem::zeroed() };
    (unsafe { libc::statvfs(name.as_ptr(), &mut space) } == 0)
        .then(|| space.f_bavail as u64 * space.f_frsize as u64)
}

/// Whether a filesystem of this kind can clone files at all (btrfs, XFS,
/// bcachefs, OCFS2), read from its superblock magic without writing.
fn may_clone(path: &Path) -> bool {
    use std::os::unix::ffi::OsStrExt;
    let Ok(name) = std::ffi::CString::new(path.as_os_str().as_bytes()) else {
        return false;
    };
    let mut info: libc::statfs = unsafe { std::mem::zeroed() };
    if unsafe { libc::statfs(name.as_ptr(), &mut info) } != 0 {
        return false;
    }
    [0x9123_683E_i64, 0x5846_5342, 0xCA45_1A4E, 0x7461_636F].contains(&(info.f_type as i64))
}

/// Whether the copy fits: a full copy needs the base's disks and the
/// headroom, which the free space and what the start deletes first must
/// cover. Only when they do not is `clones` asked whether the filesystem
/// shares blocks instead (None: not known), and a clone needs the headroom
/// alone. The answer is None when it depends on a clone that is not known.
fn disk_verdict(
    disks: u64,
    free: u64,
    reclaimable: u64,
    clones: impl FnOnce() -> Option<bool>,
) -> (u64, Option<bool>, Option<bool>) {
    let full = disks + COPY_HEADROOM;
    if free.saturating_add(reclaimable) >= full {
        return (full, None, Some(true));
    }
    match clones() {
        Some(true) => (COPY_HEADROOM, Some(true), Some(free >= COPY_HEADROOM)),
        Some(false) => (full, Some(false), Some(false)),
        None => (full, None, None),
    }
}

/// The space the next start's disk copy needs in the profile's `installs`
/// and whether it is there. With `probe`, a short filesystem is asked to
/// clone the base's smallest disk (the start); without, nothing is written
/// and a filesystem that might clone leaves the answer open (the status).
/// Null when the base's disks or the filesystem cannot be read; the start
/// itself then says why.
fn start_disk(profile: &Value, state: &Value, pins: &[(PathBuf, String)], probe: bool) -> Value {
    let base = match state["verified"]["install"].as_str() {
        Some(base) => PathBuf::from(base),
        None if state["closed"] == true && text(state, "install") => {
            PathBuf::from(s(state, "install"))
        }
        None => return Value::Null,
    };
    let installs = PathBuf::from(s(profile, "installs"));
    // An overlay needs only the small disks' copies; a copy or a flattened
    // overlay the whole root disk as well.
    let (method, _) = disk_overlay::start_method(profile, &base);
    let Some(root) = disk_overlay::root_bytes(&base) else {
        return Value::Null;
    };
    let mut disks = if method == "overlay" { 0 } else { root };
    for name in COPIED_DISKS {
        match fs::metadata(base.join(name)) {
            Ok(meta) if meta.is_file() => disks += meta.len(),
            _ => return Value::Null,
        }
    }
    let Some(free) = free_bytes(&installs) else {
        return Value::Null;
    };
    let reclaimable = copies_a_start_removes(profile, state, pins)
        .iter()
        .map(|copy| allocated_bytes(copy))
        .sum::<u64>();
    let (needed, reflink, enough) = disk_verdict(disks, free, reclaimable, || {
        if method == "flatten" {
            // qemu-img convert writes a new file; it never clones.
            Some(false)
        } else if probe {
            Some(clones_into(&base.join("sep.raw"), &installs))
        } else if may_clone(&installs) {
            None
        } else {
            Some(false)
        }
    });
    json!({"installs":installs,"needed_bytes":needed,"free_bytes":free,
        "reclaimable_bytes":reclaimable,"reflink":reflink,"enough":enough,"method":method})
}

/// What a cleanup of the profile's `installs` would do: the disk copies the
/// start recorded as its own (`created`) that are neither the verified base,
/// the current run nor a start's attempt, and the reason each other copy is
/// kept. A `guest-install*` directory the state does not record is listed
/// as unrecorded and never removed: it may belong to another profile.
fn cleanup_plan(
    profile: &Value,
    state: &Value,
    pins: &[(PathBuf, String)],
) -> (Vec<PathBuf>, Vec<Value>, Vec<PathBuf>) {
    let installs = PathBuf::from(s(profile, "installs"));
    let mut protected: Vec<(PathBuf, String)> = Vec::new();
    if let Some(base) = state["verified"]["install"].as_str() {
        protected.push((PathBuf::from(base), "verified-base".into()));
    }
    if text(state, "install") {
        protected.push((PathBuf::from(s(state, "install")), "current-run".into()));
    }
    if let Some(attempt) = state["attempt"]["install"].as_str() {
        protected.push((PathBuf::from(attempt), "start-attempt".into()));
    }
    for (path, name) in pins {
        protected.push((path.clone(), format!("saved-state:{name}")));
    }
    let created: Vec<PathBuf> = state["created"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|path| path.as_str().map(PathBuf::from))
        .collect();
    // A copy an overlay among them reads is kept for as long as they are.
    let roots: Vec<PathBuf> = protected.iter().map(|(path, _)| path.clone()).collect();
    for (path, _) in protected.clone() {
        for backing in disk_overlay::backing_installs(&path) {
            if !roots.contains(&backing) {
                protected.push((backing, "overlay-backing".into()));
            }
        }
    }
    let shielded = disk_overlay::with_backing(&roots);
    let mut removable = Vec::new();
    let mut kept: Vec<Value> = Vec::new();
    for (path, reason) in &protected {
        if fs::symlink_metadata(path).is_ok() && !kept.iter().any(|row| row["path"] == json!(path))
        {
            kept.push(json!({"path":path,"reason":reason}));
        }
    }
    for path in &created {
        if !protected.iter().any(|(kept, _)| kept == path)
            && !disk_overlay::among(&shielded, path)
            && owned_copy(&installs, path)
        {
            removable.push(path.clone());
        }
    }
    let mut unrecorded: Vec<PathBuf> = fs::read_dir(&installs)
        .into_iter()
        .flatten()
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| {
            owned_copy(&installs, path)
                && !created.contains(path)
                && !protected.iter().any(|(kept, _)| kept == path)
                && !disk_overlay::among(&shielded, path)
        })
        .collect();
    removable.sort();
    removable.dedup();
    unrecorded.sort();
    (removable, kept, unrecorded)
}

/// Removes one disk copy the way the start does (`remove_install` in the
/// script): every regular file in it is opened and locked first, so a copy
/// whose disks a running guest holds is refused, and the directory must
/// still be the same one when it goes. Err names why it was kept.
fn remove_copy(installs: &Path, copy: &Path) -> std::result::Result<(), String> {
    use std::os::fd::AsRawFd;
    use std::os::unix::fs::OpenOptionsExt;
    if !owned_copy(installs, copy) {
        return Err("not a disk copy Tomacco made in installs".into());
    }
    let directory = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW)
        .open(copy)
        .map_err(|error| error.to_string())?;
    let identity = directory.metadata().map_err(|error| error.to_string())?;
    let mut held = Vec::new();
    let entries = fs::read_dir(copy).map_err(|error| error.to_string())?;
    for entry in entries.flatten() {
        let name = entry.path();
        let Ok(meta) = fs::symlink_metadata(&name) else {
            continue;
        };
        if !meta.is_file() {
            continue;
        }
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(&name)
            .map_err(|error| error.to_string())?;
        let opened = file.metadata().map_err(|error| error.to_string())?;
        if (opened.dev(), opened.ino()) != (meta.dev(), meta.ino()) {
            return Err(format!("{} changed during cleanup", name.display()));
        }
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err("in use by a running guest; stop it first".into());
        }
        held.push(file);
    }
    let now = fs::symlink_metadata(copy).map_err(|error| error.to_string())?;
    if (now.dev(), now.ino()) != (identity.dev(), identity.ino()) {
        return Err("the directory changed during cleanup".into());
    }
    fs::remove_dir_all(copy).map_err(|error| error.to_string())?;
    drop(held);
    Ok(())
}

/// Removes older disk copies from the profile's `installs`, or with
/// `dry_run` (the default) only says what would go and how many bytes that
/// frees. Only copies the start recorded as its own are touched; the
/// verified base, the current run and a start's attempt are always kept.
pub fn cleanup(a: &Value) -> Result {
    let directory = directory()?;
    let (profile, state) = records(&directory)?;
    let dry_run = a["dry_run"].as_bool().unwrap_or(true);
    let busy = jobs::active_exclusive(EXCLUSIVE)?;
    if busy.is_some() && !dry_run {
        return Err(Error::new(
            "ios_virtual_busy",
            "A start or stop is in progress; clean up when it has ended",
        )
        .details(json!({"job":busy})));
    }
    let installs = PathBuf::from(s(&profile, "installs"));
    let (removable, kept, unrecorded) = cleanup_plan(&profile, &state, &states::pinned(&directory));
    let mut copies = Vec::new();
    let (mut reclaimable, mut freed) = (0u64, 0u64);
    for copy in &removable {
        let bytes = allocated_bytes(copy);
        reclaimable += bytes;
        let mut row = json!({"path":copy,"bytes":bytes,"removed":false,"reason":null});
        if !dry_run {
            match remove_copy(&installs, copy) {
                Ok(()) => {
                    freed += bytes;
                    row["removed"] = json!(true);
                }
                Err(reason) => row["reason"] = json!(reason),
            }
        }
        copies.push(row);
    }
    Ok(
        json!({"dry_run":dry_run,"installs":installs,"copies":copies,
        "reclaimable_bytes":reclaimable,"freed_bytes":freed,"kept":kept,
        "unrecorded":unrecorded.iter().map(|path| json!({"path":path,"bytes":allocated_bytes(path)})).collect::<Vec<_>>(),
        "busy":busy.is_some()}),
    )
}

fn gib(bytes: u64) -> String {
    format!("{:.1} GiB", bytes as f64 / (1u64 << 30) as f64)
}

/// A start that would run out of space part way through its disk copy is
/// refused before anything is deleted or copied, with what to do about it.
fn refuse_short_disk(disk: &Value) -> Result<()> {
    if disk["enough"] != false {
        return Ok(());
    }
    let (needed, free, reclaimable) = (
        n(disk, "needed_bytes"),
        n(disk, "free_bytes"),
        n(disk, "reclaimable_bytes"),
    );
    let installs = s(disk, "installs");
    let short = needed.saturating_sub(free.saturating_add(reclaimable));
    Err(Error::new(
        "ios_virtual_disk_space",
        format!(
            "The start copies {} of disks into {installs}, but that filesystem has {} free{}",
            gib(needed),
            gib(free),
            if reclaimable > 0 {
                format!(" and the start would delete {} of older copies", gib(reclaimable))
            } else {
                String::new()
            }
        ),
    )
    .remedy(format!(
        "Free at least {} on the filesystem of {installs}, or point the profile's installs at a filesystem with room; \
         on btrfs or XFS with reflink the copy shares blocks with its base. Tomacco deletes only the copies it made there",
        gib(short)
    ))
    .details(disk.clone()))
}

/// When each phase of the current start or stop began (`phase_log`, which
/// `Phone.save` in the script keeps from the start's `preparing` on) and how
/// long each took; the current phase's time runs on until the next. Entries
/// of another shape are left out, and a log whose last phase is not the
/// state's phase (a state written before the log existed) gives no elapsed
/// time for the phase.
fn phase_times(state: &Value, now: f64) -> Value {
    let log: Vec<(&str, f64)> = state["phase_log"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|row| Some((row["phase"].as_str()?, row["started"].as_f64()?)))
        .filter(|(_, started)| started.is_finite())
        .collect();
    let round = |seconds: f64| (seconds.max(0.0) * 10.0).round() / 10.0;
    let phases: Vec<Value> = log
        .iter()
        .enumerate()
        .map(|(index, (phase, started))| {
            let until = log.get(index + 1).map_or(now, |next| next.1);
            json!({"phase":phase,"started":started,"seconds":round(until - started)})
        })
        .collect();
    let current = log
        .last()
        .filter(|(phase, _)| state["phase"] == *phase)
        .map(|(_, started)| *started);
    json!({"phase_started":current,
        "phase_elapsed_seconds":current.map(|started| round(now - started)),
        "phases":phases})
}

/// The plain-word verdict the script recorded when the last run finished
/// (`verdict`, tools/ios_run_verdict.py), or null for a state from before it
/// or a record of another shape.
fn last_run(state: &Value) -> Value {
    let verdict = &state["verdict"];
    if verdict["verified"].is_boolean()
        && verdict["summary"].is_string()
        && verdict["reasons"]
            .as_array()
            .is_some_and(|reasons| reasons.iter().all(Value::is_string))
        && (verdict["remedy"].is_string() || verdict["remedy"].is_null())
    {
        json!({"verified":verdict["verified"],"outcome":verdict["outcome"].as_str(),
            "summary":verdict["summary"],"reasons":verdict["reasons"],"remedy":verdict["remedy"]})
    } else {
        Value::Null
    }
}

/// What to call next when a start or stop ended halfway, from the state the
/// script left (tools/ios_virtual_iphone.py) and whether its guest runs. A
/// start adopts a running guest instead of booting another: it makes no disk
/// copy and goes on from the guest-service check, so a start that failed
/// after the boot resumes with `ios.virtual.start`. Null when nothing is left
/// halfway: the phone is ready, off, or its last run is described by
/// `last_run`.
fn next_step(state: &Value, running: bool, ended: bool, busy: Option<&Value>) -> Value {
    let step = |action: &str, call: &str, arguments: Value, resumes: bool, why: &str| json!({"action":action,"call":call,"arguments":arguments,"resumes":resumes,"why":why});
    if let Some(job) = busy {
        return step(
            "wait",
            "job.wait",
            json!({"job_id":job["id"]}),
            false,
            "A start or stop is in progress; follow its job.",
        );
    }
    let phase = s(state, "phase");
    if running {
        return match phase {
            "launch-uncertain" => step(
                "resume",
                "ios.virtual.start",
                json!({"open_app":false}),
                true,
                "The guest runs, but whether iOS opened the app is unknown, and the launch is never sent twice                  on its own. A start with open_app false adopts the guest and finishes without it; open the app                  yourself afterwards.",
            ),
            "awaiting-unlock" => step(
                "unlock_then_resume",
                "ios.virtual.start",
                json!({}),
                true,
                "The lock screen has a passcode. Unlock it in the phone window, then start again: the start                  adopts the running guest and opens the app.",
            ),
            "stopping" => step(
                "stop",
                "ios.virtual.stop",
                json!({}),
                false,
                "A stop ended before the guest did. Stop again; a guest that answers is asked to save first.",
            ),
            "preparing" | "booting" | "installing" | "unlocking" | "failed" => step(
                "resume",
                "ios.virtual.start",
                json!({}),
                true,
                "The last start ended while its guest kept running. Start again: it adopts that guest without                  a new disk copy and goes on from the guest-service check.",
            ),
            _ => Value::Null,
        };
    }
    if ended {
        return step(
            "record_end",
            "ios.virtual.stop",
            json!({}),
            false,
            "The guest has ended on its own. A stop records whether it saved first, and last_run then says how              it ended; a start does the same before it begins from the last verified run.",
        );
    }
    if phase == "failed" && state["closed"] == true {
        return step(
            "start_again",
            "ios.virtual.start",
            json!({}),
            false,
            "The last start failed before its guest ran, and its disk copy was removed. A new start begins              again from the last verified run; error says what went wrong.",
        );
    }
    Value::Null
}

fn application_process_observation(profile: &Value, state: &Value, running: bool) -> Value {
    let launch = &state["launch_receipt"];
    if running
        && state["guest_job"].as_str().is_some_and(|id| !id.is_empty())
        && launch["state"] == "accepted"
        && launch["job_id"] == state["guest_job"]
        && launch["application"] == profile["application"]
        && launch["process_observation"].is_object()
    {
        launch["process_observation"].clone()
    } else {
        Value::Null
    }
}

pub fn status(a: &Value) -> Result {
    let directory = match directory() {
        Ok(directory) => directory,
        Err(error) if error.code == "ios_virtual_unconfigured" => {
            return Ok(json!({"configured":false,"experimental":true}));
        }
        Err(error) => return Err(error),
    };
    let (profile, state) = records(&directory)?;
    // The guest job belongs to whichever data directory started it. A job
    // this control plane does not know is reported as unknown, not as stopped.
    // A definitive "no such job" (another data directory, a reset job store)
    // is that unknown state; a store that cannot be read is an error.
    let mut guest_known = true;
    let guest = match &state["guest_job"] {
        Value::Null => None,
        Value::String(id) if id.is_empty() => None,
        Value::String(id) => {
            identifier(id, "job ID", Some("[0-9a-f]{32}")).map_err(|_| {
                Error::new(
                    "ios_virtual_profile_invalid",
                    "The virtual iOS device guest job must be a job ID or null",
                )
            })?;
            match jobs::get(id) {
                Err(error) if error.code == "job_not_found" => {
                    guest_known = false;
                    None
                }
                Err(error) => {
                    return Err(Error::new(
                        "ios_virtual_guest_state_unknown",
                        "The virtual iOS device job could not be observed; its state is unknown",
                    )
                    .details(json!({"job_id":id,"cause":error.value()})));
                }
                Ok(job) => {
                    let job_state = s(&job, "state");
                    if !["queued", "running"].contains(&job_state)
                        && !jobs::TERMINAL.contains(&job_state)
                    {
                        return Err(Error::new(
                            "ios_virtual_guest_state_unknown",
                            "The virtual iOS device job has an unknown lifecycle state",
                        )
                        .details(json!({"job_id":id,"state":job_state})));
                    }
                    Some(job)
                }
            }
        }
        _ => {
            return Err(Error::new(
                "ios_virtual_profile_invalid",
                "The virtual iOS device guest job must be a job ID or null",
            ));
        }
    };
    if guest.is_none() && guest_known && state["closed"] == false {
        return Err(Error::new(
            "ios_virtual_guest_state_unknown",
            "An open virtual iOS device run has no recorded guest job",
        ));
    }
    let guest_state = guest.as_ref().map(|job| s(job, "state").to_string());
    let running = guest_state
        .as_deref()
        .is_some_and(|state| !jobs::TERMINAL.contains(&state));
    // The guest has ended on its own (its session limit, a crash) and nobody
    // has looked at how it ended yet. Whether it saved first is in its report.
    let ended = state["closed"] == false && guest_state.is_some() && !running;
    // Only exact evidence counts as saved: exit code 0, complete hashes and a
    // flush the stop confirmed. A report that cannot be read, or whose fields
    // have another shape, proves nothing and leaves the status readable.
    let mut report_readable = true;
    let mut gpu_memory_stop = false;
    let saved = ended
        && match research::report(&json!({"job_id":state["guest_job"]})) {
            Ok(reply) => {
                gpu_memory_stop = reply["report"]["vram_guard_stop"].is_object();
                reply["report"]["exit_code"].as_i64() == Some(0)
                    && reply["report"]["storage_hashes_complete"].as_bool() == Some(true)
                    && reply["stop"]["guest_storage_flushed"].as_bool() == Some(true)
            }
            Err(_) => {
                report_readable = false;
                false
            }
        };
    let busy = jobs::active_exclusive(EXCLUSIVE)?.map(|job| {
        json!({"id":job["id"],"label":job["label"],"state":job["state"],
            "action":job["plan"]["operation_key"]["operation"]})
    });
    let application_process = application_process_observation(&profile, &state, running);
    let times = phase_times(&state, now());
    Ok(json!({
        "configured":true,"experimental":true,"directory":directory,
        "run":state["run"],"phase":state["phase"],"closed":state["closed"],
        // When the phase began, how long it has run, each phase of this start
        // or stop with its seconds, and the disk copy's progress while preparing.
        "phase_started":times["phase_started"],"phase_elapsed_seconds":times["phase_elapsed_seconds"],
        "phases":times["phases"],"copy_progress":copy_progress(&state),
        // Whether the next start's disk copy fits in the profile's installs,
        // and whether it copies the root disk or lays an overlay on it.
        "start_disk":start_disk(&profile,&state,&states::pinned(&directory),false),
        "start_disks":profile["start_disks"].as_str().unwrap_or("copy"),
        "error":state["error"],"updated":state["updated"],
        "guest_job":state["guest_job"],"guest_state":guest_state,"guest_state_known":guest_known,
        "guest_report_readable":report_readable,
        "guest_running":running,
        "application":profile["application"],
        // The developer apps the profile names (installed at the start from
        // the guest-tools build that carries them); the diagnostic is implicit.
        "applications":profile.get("applications").cloned().unwrap_or(json!([])),
        "application_launch_accepted":state["application_launch_accepted"],
        "application_process":application_process,
        "application_scene_verified":state["application_scene_verified"].as_bool().unwrap_or(false),
        // Whether the start saw anything but black on the panel (null: not looked).
        "display_visible":state.get("display_visible").cloned().unwrap_or(Value::Null),
        // The start's time-zone step (null before the first start that had it)
        // and the apps installed on this run's volume, by bundle identifier.
        "time_zone":time_zone(&state),
        "installed_applications":installed_applications(&state),
        // Why the last start installed nothing (the adapter did not activate,
        // installd refused); null when the staged apps installed or none exist.
        "applications_error":state.get("applications_error").filter(|v| v.is_string()).cloned().unwrap_or(Value::Null),
        // Whether this boot has a network, and why not if it was asked for.
        "guest_network":state.get("guest_network").filter(|v| v.is_object()).cloned().unwrap_or(json!({"state":"off"})),
        "busy":busy,
        // What the next start will be, what the current run is, and the choice.
        "model":model()?,"running_model":state.get("model").cloned().unwrap_or(json!("")),
        "running_panel":running_panel(&profile,s(&state,"model"),guest.as_ref()),
        "models":paravirtual::device_models().iter().map(|model| json!({"id":model["id"],
            "name":model["name"],"panel":model["panel"],"source":model["source"]})).collect::<Vec<_>>(),
        "guest_ended":ended,"guest_saved":saved,"guest_gpu_memory_stop":gpu_memory_stop,
        // How the last finished run ended, in plain words: verified, outcome,
        // summary, the reasons it was not verified and a remedy.
        "last_run":last_run(&state),
        // What to call next when a start or stop ended halfway: resume the
        // running guest instead of a new start, or record how a guest ended.
        "next":next_step(&state,running,ended,busy.as_ref()),
        // The graphics card's memory against the guard's limit, and the
        // host's I/O and memory pressure, on every call.
        "host":host_load::host(guest.as_ref()),
        // Whether the running phone still shows signs of life, what it last
        // showed, and the typed calls that recover a stalled one.
        "liveness":match guest.as_ref() {
            Some(job) if running => liveness::observe(job,&profile,s(&state,"phase"),a["probe"] == true),
            _ => Value::Null,
        },
    }))
}

fn submit(operation: &str, action: &str, label: &str, timeout: u64, a: &Value) -> Result {
    submit_with(operation, action, label, timeout, a, Vec::new())
}

fn submit_with(
    operation: &str,
    action: &str,
    label: &str,
    timeout: u64,
    a: &Value,
    arguments: Vec<String>,
) -> Result {
    let directory = directory()?;
    let (_, state) = records(&directory)?;
    let key = json!({"operation":operation,"arguments":{"directory":directory,
        "open_app":a["open_app"].as_bool().unwrap_or(true),"extra":arguments}});
    if let Some(mut active) = jobs::active_exclusive(EXCLUSIVE)? {
        if !b(&active, "cancelling") && active["plan"]["operation_key"] == key {
            active["reused"] = json!(true);
            return Ok(active);
        }
        return Err(Error::new(
            "ios_virtual_busy",
            "The virtual iOS device is already being started or stopped",
        )
        .details(json!({"job":active})));
    }
    let mut argv = vec![
        toolchain::find("python3")?,
        research::script(SCRIPT)?.to_string_lossy().into_owned(),
        action.to_string(),
        "--cli".into(),
        crate::core::own_executable()?
            .to_string_lossy()
            .into_owned(),
        "--profile".into(),
        directory.to_string_lossy().into_owned(),
    ];
    if action != "stop" && a["open_app"] == false {
        argv.push("--no-app".into());
    }
    argv.extend(arguments);
    if action == "start" {
        let model = model()?;
        if !model.is_empty() {
            // Such a device has no Home button; it is unlocked with a swipe.
            argv.extend(["--model".into(), model, "--unlock".into(), "swipe".into()]);
        }
    } else if action != "stop" && !s(&state, "model").is_empty() {
        // A deploy or an unlock leaves the lock screen of the running phone,
        // which is unlocked as the model it was started as.
        argv.extend(["--unlock".into(), "swipe".into()]);
    }
    jobs::submit(
        label,
        json!({"cwd":home()?,"timeout":timeout,"exclusive":EXCLUSIVE,"single_active":true,
            "operation_key":key,"steps":[{"argv":argv}],
            "always_artifacts":[directory.join("state.json")]}),
    )
}

pub fn start(a: &Value) -> Result {
    // A start that adopts a running guest, or repeats an active start, makes
    // no copy; any other start is refused here when its copy cannot fit.
    let directory = directory()?;
    let (profile, state) = records(&directory)?;
    let boots = jobs::active_exclusive(EXCLUSIVE)?.is_none() && !guest_is_running(&state);
    if boots {
        refuse_short_disk(&start_disk(
            &profile,
            &state,
            &states::pinned(&directory),
            true,
        ))?;
    }
    // Preparation, a boot of many minutes and the unlock each have their own
    // bound inside the script; this is the outer limit of the whole job.
    let mut job = submit(
        "ios.virtual.start",
        "start",
        "Start virtual iOS device",
        4800,
        a,
    )?;
    // A start that boots a guest says first when the card is already short
    // of memory or the host under pressure; it is not refused for either.
    if boots {
        let host = host_load::host(None);
        job["warnings"] = json!(host_load::warnings(&host["gpu"], &host["pressure"]));
    }
    Ok(job)
}

/// One step from a built bundle to the app on the running phone: inspected
/// here, so a bundle that cannot go is refused at once, and then a job
/// (`Phone.deploy` in the script). No reboot; the profile lists the app after.
pub fn deploy(a: &Value) -> Result {
    let bundle = path(s(a, "bundle"), true, true)?;
    let entitlements = if text(a, "entitlements") {
        Some(path(s(a, "entitlements"), false, true)?)
    } else {
        None
    };
    let inspected = inspect_application(&bundle, entitlements.as_deref())?;
    if inspected["bundle_id"] == RESEARCH_APPLICATION {
        return Err(Error::new(
            "invalid_application_bundle",
            "This bundle uses the owned diagnostic app's bundle identifier; choose another",
        ));
    }
    let directory = directory()?;
    let (_, state) = records(&directory)?;
    if !guest_is_running(&state) {
        return Err(Error::new(
            "ios_virtual_not_running",
            "The phone is not running; add the app with ios.virtual.configure and start the phone",
        ));
    }
    submit_with(
        "ios.virtual.deploy",
        "deploy",
        "Install app on virtual iOS device",
        1800,
        a,
        deploy_arguments(&bundle, entitlements.as_deref(), a["force"] == true),
    )
}

/// The deploy script's own arguments: the bundle, the entitlements to
/// request, and `--force` to install an unchanged app again.
fn deploy_arguments(bundle: &Path, entitlements: Option<&Path>, force: bool) -> Vec<String> {
    let mut arguments = vec![
        "--bundle".to_string(),
        bundle.to_string_lossy().into_owned(),
    ];
    if let Some(file) = entitlements {
        arguments.extend([
            "--entitlements".to_string(),
            file.to_string_lossy().into_owned(),
        ]);
    }
    if force {
        arguments.push("--force".to_string());
    }
    arguments
}

/// A debugger's way to a process of the running phone: a job holding a port on
/// the host's loopback for one lldb session (tools/ios_debug_bridge.py). Its
/// log's first line names the port.
pub fn debug(a: &Value) -> Result {
    let target = s(a, "target");
    if !guest_control_debug_target(target) {
        return Err(Error::new(
            "invalid_debug_target",
            "target must be a PID or a process name: 1 to 64 ASCII letters, digits, '.', '_' or '-'",
        ));
    }
    let directory = directory()?;
    let (_, state) = records(&directory)?;
    if !guest_is_running(&state) {
        return Err(Error::new(
            "ios_virtual_not_running",
            "The phone is not running",
        ));
    }
    let job = s(&state, "guest_job").to_string();
    let plan = jobs::get(&job)?;
    let request = read_json(
        &PathBuf::from(s(&plan["plan"], "cwd")).join("paravirtual/request.json"),
        None,
    )?;
    let socket = s(&request["guest_control"], "socket").to_string();
    if socket.is_empty() {
        return Err(Error::new(
            "ios_guest_control_unavailable",
            "This run has no guest channel",
        ));
    }
    let seconds = a["seconds"].as_u64().unwrap_or(3600);
    if !(10..=86400).contains(&seconds) {
        return Err(Error::new(
            "invalid_debug_session",
            "seconds must be 10 to 86400",
        ));
    }
    let port = a["port"].as_u64().unwrap_or(0);
    if port > 65535 {
        return Err(Error::new(
            "invalid_debug_session",
            "port must be a TCP port or 0",
        ));
    }
    let argv = vec![
        toolchain::find("python3")?,
        research::script("ios_debug_bridge.py")?
            .to_string_lossy()
            .into_owned(),
        "--cli".into(),
        std::env::current_exe()?.to_string_lossy().into_owned(),
        "--job".into(),
        job,
        "--socket".into(),
        socket,
        "--target".into(),
        target.to_string(),
        "--port".into(),
        port.to_string(),
        "--seconds".into(),
        seconds.to_string(),
    ];
    jobs::submit(
        "Debugger for virtual iOS device",
        json!({"cwd":home()?,"timeout":seconds + 300,"exclusive":"ios-virtual-debug","single_active":true,
            "operation_key":{"operation":"ios.virtual.debug","arguments":{"target":target}},
            "steps":[{"argv":argv}]}),
    )
}

fn guest_control_debug_target(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
        && !value.starts_with(['.', '-'])
}

/// The guest job of the running phone and the model it was started as
/// (empty: the Home-button phone), or `ios_virtual_not_running`.
pub(crate) fn running_guest() -> Result<(String, String)> {
    let (_, state) = records(&directory()?)?;
    if !guest_is_running(&state) {
        return Err(Error::new(
            "ios_virtual_not_running",
            "The phone is not running; start it with ios.virtual.start",
        ));
    }
    Ok((
        s(&state, "guest_job").to_string(),
        s(&state, "model").to_string(),
    ))
}

/// The guest job of the configured phone's current or last run, running or
/// not, or `ios_virtual_not_running` when no run has one.
pub(super) fn guest_job() -> Result<String> {
    let (_, state) = records(&directory()?)?;
    match state["guest_job"].as_str() {
        Some(id) if !id.is_empty() => {
            identifier(id, "job ID", Some("[0-9a-f]{32}"))?;
            Ok(id.to_string())
        }
        _ => Err(Error::new(
            "ios_virtual_not_running",
            "The phone has no run with a guest job; start it with ios.virtual.start",
        )),
    }
}

/// Whether the state's guest job is still on its way or running.
fn guest_is_running(state: &Value) -> bool {
    state["guest_job"].as_str().is_some_and(|id| {
        jobs::get(id).is_ok_and(|job| !jobs::TERMINAL.contains(&s(&job, "state")))
    })
}

/// Leaves the running phone's lock screen as a start does (`Phone.ensure_unlocked`
/// in the script), as a short job: `screen` in its result is `unlocked`, or
/// `not-checked` when the guest tools cannot report the screen. A passcode is
/// never entered.
pub fn unlock(a: &Value) -> Result {
    let (_, state) = records(&directory()?)?;
    if !guest_is_running(&state) {
        return Err(Error::new(
            "ios_virtual_not_running",
            "The phone is not running; start it with ios.virtual.start",
        ));
    }
    submit(
        "ios.virtual.unlock",
        "unlock",
        "Unlock virtual iOS device",
        900,
        a,
    )
}

/// What a screen_state reply says about the lock screen.
#[derive(Debug, PartialEq)]
pub(crate) enum LockScreen {
    Unlocked,
    /// Locked without a passcode: the unlock path can leave it.
    Locked,
    Passcode,
    /// The guest could not say (older guest tools, a busy or slow guest).
    Unknown(String),
}

pub(crate) fn lock_screen(reply: &Result) -> LockScreen {
    let response = match reply {
        Ok(reply) => &reply["response"],
        Err(error) => return LockScreen::Unknown(error.code.clone()),
    };
    if response["ok"] != true || response["screen_state_known"] != true {
        return LockScreen::Unknown(
            response["error"]
                .as_str()
                .unwrap_or("screen_state_unknown")
                .to_string(),
        );
    }
    match (
        response["screen_locked"].as_bool(),
        response["passcode_enabled"].as_bool(),
    ) {
        (Some(false), _) => LockScreen::Unlocked,
        (Some(true), Some(false)) => LockScreen::Locked,
        (Some(true), Some(true)) => LockScreen::Passcode,
        _ => LockScreen::Unknown("screen_state_unknown".into()),
    }
}

fn locked(message: &str, details: Value) -> Error {
    Error::new("ios_virtual_locked", message)
        .remedy("Unlock the phone in its window (Home, or a swipe up on a model without one), then try again")
        .details(details)
}

/// Makes sure the running phone is past its lock screen before something
/// that needs the screen: asks the guest once, and when it is locked without
/// a passcode runs the unlock job and waits for it. A guest that cannot say
/// is not held up (`not-checked`); a passcode or a screen that stays locked is
/// `ios_virtual_locked`.
pub(crate) fn ensure_unlocked() -> Result {
    let (job, _) = running_guest()?;
    let reply =
        guest_control::call(&json!({"job_id":job,"operation":"screen_state","timeout_seconds":30}));
    match lock_screen(&reply) {
        LockScreen::Unlocked => Ok(json!({"screen":"unlocked","unlock_job":null})),
        LockScreen::Unknown(reason) => Ok(json!({"screen":"not-checked","reason":reason})),
        LockScreen::Passcode => Err(locked(
            "The virtual iPhone's lock screen has a passcode, which Tomacco does not enter",
            json!({"job_id":job}),
        )),
        LockScreen::Locked => {
            let submitted = unlock(&json!({}))?;
            let id = s(&submitted, "id").to_string();
            let ended = jobs::wait(&id, 900)?;
            if s(&ended, "state") == "succeeded" {
                Ok(json!({"screen":"unlocked","unlock_job":id}))
            } else {
                Err(locked(
                    "The virtual iPhone stayed on its lock screen",
                    json!({"job_id":job,"unlock_job":ended}),
                ))
            }
        }
    }
}

pub fn stop(a: &Value) -> Result {
    // Stopping wins over a start that is still running. The start is
    // cancelled; its guest is an independent job, which the stop then shuts
    // down, without saving if its service has not come up yet.
    if let Some(active) = jobs::active_exclusive(EXCLUSIVE)? {
        if active["plan"]["operation_key"]["operation"] == "ios.virtual.start" {
            let id = s(&active, "id");
            jobs::cancel(id)?;
            let ended = jobs::wait(id, 30)?;
            if !jobs::TERMINAL.contains(&s(&ended, "state")) {
                return Err(Error::new(
                    "ios_virtual_busy",
                    "The running start did not end in time; try stopping again",
                )
                .details(json!({"job":ended})));
            }
        }
    }
    submit(
        "ios.virtual.stop",
        "stop",
        "Stop virtual iOS device",
        900,
        a,
    )
}

#[cfg(test)]
mod debug_target_tests {
    use super::*;

    #[test]
    fn a_listed_app_names_its_executable_and_an_unknown_one_is_refused() {
        let bundles = vec!["/a/Demo.app".to_string(), "/a/Gone.app".to_string()];
        let mut infos = std::collections::HashMap::new();
        infos.insert(
            "/a/Demo.app".to_string(),
            json!({"bundle_id":"dev.example.Demo","executable":"DemoExe"}),
        );
        assert_eq!(
            executable_of("dev.example.demo", &bundles, &infos).unwrap(),
            "DemoExe"
        );
        assert_eq!(
            executable_of("dev.example.Gone", &bundles, &infos)
                .unwrap_err()
                .code,
            "ios_virtual_application_unknown"
        );
    }

    #[test]
    fn force_reaches_the_deploy_script_only_when_asked() {
        let bundle = Path::new("/apps/Demo.app");
        assert_eq!(
            deploy_arguments(bundle, None, false),
            ["--bundle", "/apps/Demo.app"]
        );
        assert_eq!(
            deploy_arguments(bundle, Some(Path::new("/apps/e.plist")), true),
            [
                "--bundle",
                "/apps/Demo.app",
                "--entitlements",
                "/apps/e.plist",
                "--force"
            ]
        );
    }

    #[test]
    fn a_debug_target_never_reads_as_an_option() {
        assert!(guest_control_debug_target("HelloTomacco"));
        assert!(guest_control_debug_target("551"));
        for refused in ["", "-x", ".x", "a b", "a/b", &"x".repeat(65)] {
            assert!(!guest_control_debug_target(refused), "{refused}");
        }
    }
}

#[cfg(test)]
mod running_panel_tests {
    use super::*;

    #[test]
    fn a_model_run_has_the_models_panel_not_the_profiles() {
        // A profile can name the Home-button phone's panel while the guest
        // runs as the iPhone 16; the status says what runs.
        let profile = json!({"boot":{"display_profile":"1170x2532@60:65x141"}});
        assert_eq!(
            running_panel(&profile, "16", None),
            json!("1179x2556@60:65x141")
        );
        assert_eq!(
            running_panel(&profile, "18pro", None),
            json!("1206x2622@60:66x143")
        );
        assert_eq!(
            running_panel(&profile, "", None),
            json!("1170x2532@60:65x141")
        );
        assert_eq!(running_panel(&profile, "no-such-model", None), Value::Null);
        assert_eq!(running_panel(&json!({"boot":{}}), "", None), Value::Null);
    }

    #[test]
    fn the_guest_jobs_request_is_the_panel_that_was_booted() {
        // The profile was edited after the boot: the status still reports
        // what the engine was given, not what the next start would give it.
        let edited = json!({"boot":{"display_profile":"1206x2622@60:66x143"}});
        let guest = |arguments: Value| {
            json!({"plan":{"operation_key":{"operation":"ios.emulator.paravirtual",
                "arguments":arguments}}})
        };
        let booted = guest(json!({"display_profile":"1170x2532@60:65x141"}));
        assert_eq!(
            running_panel(&edited, "", Some(&booted)),
            json!("1170x2532@60:65x141")
        );
        // A model boot carries its panel in the request as well.
        let model = guest(json!({"device_model":"16","display_profile":"1179x2556@60:65x141"}));
        assert_eq!(
            running_panel(&edited, "16", Some(&model)),
            json!("1179x2556@60:65x141")
        );
        // A boot without a panel ran on the engine's default, unknown here.
        assert_eq!(
            running_panel(&edited, "", Some(&guest(json!({})))),
            Value::Null
        );
        // A job of another kind says nothing about the panel.
        let other = json!({"plan":{"operation_key":{"operation":"ios.emulator.research"}}});
        assert_eq!(
            running_panel(&edited, "", Some(&other)),
            json!("1206x2622@60:66x143")
        );
    }
}

#[cfg(test)]
mod status_record_tests {
    use super::*;

    #[test]
    fn the_time_zone_step_is_reported_as_the_start_recorded_it() {
        // The guest's worker stayed busy, so the zone was left alone.
        let busy = json!({"time_zone":{"zone":"Europe/Vienna","outcome":"guest_operation_busy"}});
        assert_eq!(
            time_zone(&busy),
            json!({"zone":"Europe/Vienna","outcome":"guest_operation_busy",
                "previous_link":null,"link":null,"changed":null,"reason":null})
        );
        let applied = json!({"time_zone":{"zone":"Europe/Vienna","outcome":"applied",
            "previous_link":"/usr/share/zoneinfo/America/Los_Angeles",
            "link":"/usr/share/zoneinfo/Europe/Vienna","changed":true}});
        assert_eq!(
            time_zone(&applied),
            json!({"zone":"Europe/Vienna","outcome":"applied",
                "previous_link":"/usr/share/zoneinfo/America/Los_Angeles",
                "link":"/usr/share/zoneinfo/Europe/Vienna","changed":true,"reason":null})
        );
        let skipped =
            json!({"time_zone":{"zone":null,"outcome":"skipped","reason":"host_zone_unknown"}});
        assert_eq!(
            time_zone(&skipped),
            json!({"zone":null,"outcome":"skipped","previous_link":null,"link":null,
                "changed":null,"reason":"host_zone_unknown"})
        );
        // A state from before the step, or a record of another shape, reports nothing.
        assert!(time_zone(&json!({})).is_null());
        assert!(time_zone(&json!({"time_zone":null})).is_null());
        assert!(time_zone(&json!({"time_zone":"Europe/Vienna"})).is_null());
        assert!(time_zone(&json!({"time_zone":{"zone":"Europe/Vienna"}})).is_null());
        assert!(time_zone(&json!({"time_zone":{"zone":"Europe/Vienna","outcome":7}})).is_null());
    }

    #[test]
    fn the_last_runs_verdict_is_reported_as_recorded_and_nothing_else() {
        let verdict = json!({"verified":false,"outcome":"emulator-error",
            "summary":"The emulator exited with exit code 134.",
            "reasons":["The emulator exited with exit code 134, not 0."],
            "remedy":"The next start uses the last verified run.","extra":1});
        let reported = last_run(&json!({"verdict":verdict}));
        assert_eq!(reported["verified"], false);
        assert_eq!(reported["outcome"], "emulator-error");
        assert_eq!(
            reported["reasons"][0],
            "The emulator exited with exit code 134, not 0."
        );
        assert!(reported.get("extra").is_none());
        // A verified run has no remedy; a missing outcome stays null.
        let clean = last_run(
            &json!({"verdict":{"verified":true,"summary":"Fine.","reasons":[],"remedy":null}}),
        );
        assert_eq!(
            (clean["verified"].clone(), clean["outcome"].clone()),
            (json!(true), Value::Null)
        );
        // States from before verdicts, and records of another shape.
        assert!(last_run(&json!({})).is_null());
        assert!(last_run(&json!({"verdict":"verified"})).is_null());
        assert!(
            last_run(&json!({"verdict":{"verified":"yes","summary":"x","reasons":[]}})).is_null()
        );
        assert!(
            last_run(&json!({"verdict":{"verified":true,"summary":"x","reasons":[7]}})).is_null()
        );
    }

    #[test]
    fn installed_apps_are_the_runs_record_by_bundle_identifier() {
        let state = json!({"applications":{"io.example.first":"00".repeat(20),
            "io.example.broken":7}});
        assert_eq!(
            installed_applications(&state),
            json!({"io.example.first":"00".repeat(20)})
        );
        // A refused staged app and a state from before app deployment both
        // have nothing installed.
        assert_eq!(
            installed_applications(&json!({"applications":{}})),
            json!({})
        );
        assert_eq!(installed_applications(&json!({})), json!({}));
        assert_eq!(
            installed_applications(&json!({"applications":["io.example.first"]})),
            json!({})
        );
    }
}

#[cfg(test)]
mod cleanup_tests {
    use super::*;

    fn copy(installs: &Path, name: &str) -> PathBuf {
        let path = installs.join(name);
        fs::create_dir_all(&path).unwrap();
        fs::write(path.join("system.raw"), vec![1u8; 8192]).unwrap();
        path
    }

    #[test]
    fn only_recorded_copies_other_than_base_run_and_attempt_are_removable() {
        let root = tempfile::tempdir().unwrap();
        let installs = root.path().join("installs");
        let (old, base, run, attempt, stray) = (
            copy(&installs, "guest-install-1"),
            copy(&installs, "guest-install-2"),
            copy(&installs, "guest-install-3"),
            copy(&installs, "guest-install-4"),
            copy(&installs, "guest-install-9"),
        );
        let other = copy(root.path(), "guest-install-elsewhere");
        let first = copy(root.path(), "first-install");
        let profile = json!({"installs":installs});
        let state = json!({"install":run,"verified":{"install":base},"attempt":{"install":attempt},
            "created":[old,base,run,attempt,other,installs.join("guest-install-gone")]});
        let (removable, kept, unrecorded) = cleanup_plan(&profile, &state, &[]);
        assert_eq!(removable, vec![old.clone()]);
        let reasons: Vec<_> = kept.iter().map(|row| row["reason"].clone()).collect();
        assert_eq!(
            reasons,
            vec![
                json!("verified-base"),
                json!("current-run"),
                json!("start-attempt")
            ]
        );
        assert_eq!(unrecorded, vec![stray.clone()]);
        // The first install outside installs is never even considered.
        let state = json!({"install":first,"verified":{"install":first},"created":[]});
        let (removable, kept, unrecorded) = cleanup_plan(&profile, &state, &[]);
        assert!(removable.is_empty());
        assert_eq!(kept, vec![json!({"path":first,"reason":"verified-base"})]);
        assert_eq!(unrecorded.len(), 5);
    }

    #[test]
    fn a_copy_whose_disk_is_locked_is_kept_and_a_free_one_goes() {
        let root = tempfile::tempdir().unwrap();
        let installs = root.path().join("installs");
        let free = copy(&installs, "guest-install-1");
        let busy = copy(&installs, "guest-install-2");
        let held = File::open(busy.join("system.raw")).unwrap();
        use std::os::fd::AsRawFd;
        assert_eq!(
            unsafe { libc::flock(held.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) },
            0
        );
        let refused = remove_copy(&installs, &busy).unwrap_err();
        assert!(refused.contains("in use"), "{refused}");
        assert!(busy.join("system.raw").exists());
        remove_copy(&installs, &free).unwrap();
        assert!(!free.exists());
        // Never a directory outside installs, or one not named as a copy.
        let outside = copy(root.path(), "guest-install-3");
        assert!(remove_copy(&installs, &outside).is_err());
        let named = copy(&installs, "base");
        assert!(remove_copy(&installs, &named).is_err());
        assert!(outside.exists() && named.exists());
        // A link named like a copy is not followed.
        let link = installs.join("guest-install-link");
        std::os::unix::fs::symlink(&named, &link).unwrap();
        assert!(remove_copy(&installs, &link).is_err());
        assert!(named.join("system.raw").exists());
    }
}

#[cfg(test)]
mod application_rows_tests {
    use super::*;
    use std::collections::HashMap;

    #[test]
    fn listed_installed_and_orphaned_apps_are_one_list() {
        let profile = json!({"application":"dev.example.Two","applications":[
            {"bundle":"/apps/One.app","bundle_id":"dev.example.One","name":"One.app","display_name":"One"},
            {"bundle":"/apps/Two.app","bundle_id":"dev.example.Two","name":"Two.app","display_name":"Two",
             "entitlements":"/apps/two.plist"},
            {"bundle":"/apps/Gone.app","bundle_id":"dev.example.Gone","name":"Gone.app","display_name":"Gone"}]});
        let state = json!({"applications":{"dev.example.One":"aa".repeat(20),
            "dev.example.Removed":"bb".repeat(20)}});
        let mut infos = HashMap::new();
        infos.insert(
            "/apps/One.app".to_string(),
            json!({"path":"/apps/One.app","ok":true,"bundle_id":"dev.example.One",
                "version":"1.2","build":"7","minimum_os":"17.0"}),
        );
        infos.insert(
            "/apps/Two.app".to_string(),
            json!({"path":"/apps/Two.app","ok":true,"bundle_id":"dev.example.Renamed",
                "version":"2.0","build":"1","minimum_os":null}),
        );
        infos.insert(
            "/apps/Gone.app".to_string(),
            json!({"path":"/apps/Gone.app","ok":false,"error":"/apps/Gone.app does not exist"}),
        );
        let rows = application_rows(&profile, &state, &infos);
        assert_eq!(rows.len(), 5);
        assert_eq!(rows[0]["bundle_id"], RESEARCH_APPLICATION);
        assert_eq!(rows[0]["owned_diagnostic"], true);
        assert_eq!(rows[0]["opens_at_start"], false);
        let one = &rows[1];
        assert_eq!(
            (&one["version"], &one["build"], &one["minimum_os"]),
            (&json!("1.2"), &json!("7"), &json!("17.0"))
        );
        assert_eq!(one["installed"], true);
        assert_eq!(one["installed_cdhash"], "aa".repeat(20));
        assert_eq!(one["bundle_readable"], true);
        assert_eq!(one["bundle_id_changed"], false);
        let two = &rows[2];
        assert_eq!(two["installed"], false);
        assert_eq!(two["opens_at_start"], true);
        assert_eq!(two["bundle_id_changed"], true);
        assert_eq!(two["entitlements"], "/apps/two.plist");
        let gone = &rows[3];
        assert_eq!(gone["bundle_readable"], false);
        assert_eq!(gone["bundle_error"], "/apps/Gone.app does not exist");
        assert_eq!(gone["version"], Value::Null);
        let removed = &rows[4];
        assert_eq!(removed["bundle_id"], "dev.example.Removed");
        assert_eq!(removed["listed"], false);
        assert_eq!(removed["installed"], true);
    }

    #[test]
    fn a_bundle_the_reader_did_not_answer_for_is_unknown_not_absent() {
        let profile = json!({"application":RESEARCH_APPLICATION,"applications":[
            {"bundle":"/apps/One.app","bundle_id":"dev.example.One"}]});
        let rows = application_rows(&profile, &json!({}), &HashMap::new());
        assert_eq!(rows[0]["opens_at_start"], true);
        assert_eq!(rows[1]["bundle_readable"], Value::Null);
        assert_eq!(rows[1]["version"], Value::Null);
        assert_eq!(rows[1]["installed"], false);
        assert!(bundle_infos(&[]).is_empty());
    }
}

#[cfg(test)]
mod process_observation_tests {
    use super::*;

    #[test]
    fn accepted_observation_is_reported_with_its_unchanged_limits() {
        let profile = json!({"application":"dev.app"});
        let observation = json!({"pid":551,"worker_pid":550,
            "process_lifetime_bound":false,"snapshot_atomic_with_handle":false});
        let state = json!({"guest_job":"current","launch_receipt":{
            "state":"accepted","job_id":"current","application":"dev.app",
            "process_observation":observation}});
        assert_eq!(
            application_process_observation(&profile, &state, true),
            observation
        );
        assert!(application_process_observation(&profile, &state, false).is_null());
    }

    #[test]
    fn foreign_ended_or_nonaccepted_context_never_reports_an_app_process() {
        let profile = json!({"application":"dev.app"});
        let original = json!({"guest_job":"current","launch_receipt":{
            "state":"accepted","job_id":"current","application":"dev.app",
            "process_observation":{"pid":551}}});
        for (field, value) in [
            ("job_id", json!("older")),
            ("application", json!("other.app")),
            ("state", json!("submitting")),
            ("state", json!("unknown")),
            ("process_observation", json!("not-a-record")),
        ] {
            let mut state = original.clone();
            state["launch_receipt"][field] = value;
            assert!(application_process_observation(&profile, &state, true).is_null());
        }
        for id in [Value::Null, json!(""), json!(false)] {
            let mut state = original.clone();
            state["guest_job"] = id.clone();
            state["launch_receipt"]["job_id"] = id;
            assert!(application_process_observation(&profile, &state, true).is_null());
        }
    }
}

#[cfg(test)]
mod lock_screen_tests {
    use super::*;

    fn reply(response: Value) -> Result {
        Ok(json!({"job_id":"0".repeat(32),"request_id":"a".repeat(32),"response":response}))
    }

    #[test]
    fn a_screen_state_reply_decides_whether_to_unlock() {
        let known = |locked: bool, passcode: bool| {
            reply(
                json!({"ok":true,"operation":"screen_state","screen_state_known":true,
                "screen_locked":locked,"passcode_enabled":passcode}),
            )
        };
        assert_eq!(lock_screen(&known(false, true)), LockScreen::Unlocked);
        assert_eq!(lock_screen(&known(true, false)), LockScreen::Locked);
        assert_eq!(lock_screen(&known(true, true)), LockScreen::Passcode);
        // Older guest tools, an unknown screen, a busy or silent guest: not judged.
        assert_eq!(
            lock_screen(&reply(
                json!({"ok":false,"error":"screen_query_api_unavailable"})
            )),
            LockScreen::Unknown("screen_query_api_unavailable".into())
        );
        assert_eq!(
            lock_screen(&reply(
                json!({"ok":true,"screen_state_known":false,"screen_locked":true})
            )),
            LockScreen::Unknown("screen_state_unknown".into())
        );
        assert_eq!(
            lock_screen(&reply(
                json!({"ok":true,"screen_state_known":true,"screen_locked":true})
            )),
            LockScreen::Unknown("screen_state_unknown".into())
        );
        assert_eq!(
            lock_screen(&Err(Error::new("ios_guest_timeout", "late"))),
            LockScreen::Unknown("ios_guest_timeout".into())
        );
        let error = locked("stayed locked", json!({}));
        assert_eq!(error.code, "ios_virtual_locked");
        assert!(error.remedy.contains("Unlock the phone in its window"));
    }
}

#[cfg(test)]
mod next_step_tests {
    use super::*;

    fn action(state: Value, running: bool, ended: bool) -> Value {
        next_step(&state, running, ended, None)["action"].clone()
    }

    #[test]
    fn a_start_that_ended_while_its_guest_runs_is_resumed_not_repeated() {
        for phase in ["preparing", "booting", "installing", "unlocking", "failed"] {
            let state = json!({"phase":phase,"closed":false,"error":"iOS refused the launch"});
            let step = next_step(&state, true, false, None);
            assert_eq!(step["action"], "resume", "{phase}");
            assert_eq!(step["call"], "ios.virtual.start");
            assert_eq!(step["arguments"], json!({}));
            assert_eq!(step["resumes"], true);
        }
        // An unknown launch is never sent again on its own.
        let step = next_step(&json!({"phase":"launch-uncertain"}), true, false, None);
        assert_eq!(
            (step["action"].clone(), step["arguments"].clone()),
            (json!("resume"), json!({"open_app":false}))
        );
        assert_eq!(
            action(json!({"phase":"awaiting-unlock"}), true, false),
            "unlock_then_resume"
        );
        assert_eq!(action(json!({"phase":"stopping"}), true, false), "stop");
    }

    #[test]
    fn a_ready_or_stopped_phone_has_nothing_left_halfway() {
        for phase in ["launch-accepted", "service-ready"] {
            assert!(next_step(&json!({"phase":phase}), true, false, None).is_null());
        }
        for phase in ["stopped", "unverified", "configured"] {
            assert!(next_step(&json!({"phase":phase,"closed":true}), false, false, None).is_null());
        }
    }

    #[test]
    fn an_ended_guest_a_failed_preparation_and_a_running_job() {
        assert_eq!(
            action(
                json!({"phase":"launch-accepted","closed":false}),
                false,
                true
            ),
            "record_end"
        );
        let step = next_step(&json!({"phase":"failed","closed":true}), false, false, None);
        assert_eq!(
            (step["action"].clone(), step["resumes"].clone()),
            (json!("start_again"), json!(false))
        );
        // A start or stop in progress comes first.
        let busy = json!({"id":"a".repeat(32),"state":"running"});
        let step = next_step(&json!({"phase":"failed"}), true, false, Some(&busy));
        assert_eq!(step["action"], "wait");
        assert_eq!(step["arguments"]["job_id"], busy["id"]);
    }
}

#[cfg(test)]
mod start_progress_tests {
    use super::*;

    #[test]
    fn each_phase_has_its_start_and_seconds_and_the_current_one_runs_on() {
        let state = json!({"phase":"booting","phase_log":[
            {"phase":"preparing","started":100.0},{"phase":"booting","started":160.5}]});
        assert_eq!(
            phase_times(&state, 200.0),
            json!({"phase_started":160.5,"phase_elapsed_seconds":39.5,"phases":[
                {"phase":"preparing","started":100.0,"seconds":60.5},
                {"phase":"booting","started":160.5,"seconds":39.5}]})
        );
        // A clock that went back never gives negative seconds.
        assert_eq!(phase_times(&state, 150.0)["phase_elapsed_seconds"], 0.0);
    }

    #[test]
    fn a_state_without_a_usable_log_reports_no_phase_time() {
        let none = json!({"phase_started":null,"phase_elapsed_seconds":null,"phases":[]});
        assert_eq!(phase_times(&json!({"phase":"stopped"}), 9.0), none);
        assert_eq!(
            phase_times(&json!({"phase":"stopped","phase_log":"x"}), 9.0),
            none
        );
        // Malformed rows are left out; a log that ends in another phase than
        // the state's gives the phases but no elapsed time.
        let state = json!({"phase":"failed","phase_log":[{"phase":"preparing","started":1.0},
            {"phase":7,"started":2.0},{"phase":"booting"},{"phase":"booting","started":"3"}]});
        assert_eq!(
            phase_times(&state, 11.0),
            json!({"phase_started":null,"phase_elapsed_seconds":null,"phases":[
                {"phase":"preparing","started":1.0,"seconds":10.0}]})
        );
    }

    #[test]
    fn the_copy_reports_its_bytes_against_the_base_while_preparing() {
        let root = tempfile::tempdir().unwrap();
        let (base, output) = (root.path().join("base"), root.path().join("guest-install2"));
        fs::create_dir_all(&base).unwrap();
        fs::create_dir_all(&output).unwrap();
        for (name, bytes) in [
            ("system.raw", 6000),
            ("auxiliary.raw", 3000),
            ("sep.raw", 1000),
        ] {
            fs::write(base.join(name), vec![0u8; bytes]).unwrap();
        }
        let state = json!({"phase":"preparing","verified":{"install":base},
            "attempt":{"run":2,"install":output}});
        // Nothing written yet, and no record: the preparation has not begun copying.
        assert_eq!(
            copy_progress(&state),
            json!({"bytes_done":0,"bytes_total":10000,"percent":0.0,"step":null,"disk":null})
        );
        fs::write(output.join("system.raw"), vec![0u8; 6000]).unwrap();
        fs::write(output.join("auxiliary.raw"), vec![0u8; 1234]).unwrap();
        fs::write(
            output.join("guest-tools-install.json"),
            r#"{"phase":"copy-storage","copying_disk":"auxiliary.raw"}"#,
        )
        .unwrap();
        assert_eq!(
            copy_progress(&state),
            json!({"bytes_done":7234,"bytes_total":10000,"percent":72.3,
                "step":"copy-storage","disk":"auxiliary.raw"})
        );
        // Other phases, a missing attempt and an unreadable base report nothing.
        let mut booting = state.clone();
        booting["phase"] = json!("booting");
        assert!(copy_progress(&booting).is_null());
        let mut unrecorded = state.clone();
        unrecorded["attempt"] = Value::Null;
        assert!(copy_progress(&unrecorded).is_null());
        fs::remove_file(base.join("sep.raw")).unwrap();
        assert!(copy_progress(&state).is_null());
    }
}

#[cfg(test)]
mod start_disk_tests {
    use super::*;

    const GIB: u64 = 1 << 30;

    #[test]
    fn a_full_copy_needs_its_disks_and_headroom_in_free_or_deleted_space() {
        let never = || panic!("no clone probe while a full copy fits");
        assert_eq!(
            disk_verdict(32 * GIB, 40 * GIB, 0, never),
            (33 * GIB, None, Some(true))
        );
        assert_eq!(
            disk_verdict(32 * GIB, 20 * GIB, 13 * GIB, never),
            (33 * GIB, None, Some(true))
        );
        assert_eq!(
            disk_verdict(32 * GIB, 20 * GIB, 0, || Some(false)),
            (33 * GIB, Some(false), Some(false))
        );
        // A filesystem that clones needs the headroom alone.
        assert_eq!(
            disk_verdict(32 * GIB, 2 * GIB, 0, || Some(true)),
            (GIB, Some(true), Some(true))
        );
        assert_eq!(
            disk_verdict(32 * GIB, GIB / 2, 0, || Some(true)),
            (GIB, Some(true), Some(false))
        );
        // One that might clone, not asked: the answer stays open.
        assert_eq!(
            disk_verdict(32 * GIB, 2 * GIB, 0, || None),
            (33 * GIB, None, None)
        );
    }

    #[test]
    fn the_refusal_names_the_space_and_the_remedy() {
        let disk = json!({"installs":"/data/installs","needed_bytes":33 * GIB,
            "free_bytes":20 * GIB,"reclaimable_bytes":4 * GIB,"reflink":false,"enough":false});
        let error = refuse_short_disk(&disk).unwrap_err();
        assert_eq!(error.code, "ios_virtual_disk_space");
        let value = error.value();
        let message = s(&value, "message");
        assert!(message.contains("33.0 GiB"), "{message}");
        assert!(message.contains("20.0 GiB free"), "{message}");
        assert!(message.contains("4.0 GiB of older copies"), "{message}");
        let remedy = s(&value, "remedy");
        assert!(remedy.starts_with("Free at least 9.0 GiB"), "{remedy}");
        assert!(remedy.contains("reflink"), "{remedy}");
        assert_eq!(value["details"], disk);
        // Enough space, or nothing known, never refuses.
        assert!(refuse_short_disk(&json!({"enough":true})).is_ok());
        assert!(refuse_short_disk(&Value::Null).is_ok());
    }

    #[test]
    fn only_the_starts_own_older_copies_count_as_space_it_frees() {
        let root = tempfile::tempdir().unwrap();
        let installs = root.path().join("installs");
        for name in [
            "guest-install5",
            "guest-install6",
            "guest-install7",
            "guest-install8",
        ] {
            fs::create_dir_all(installs.join(name)).unwrap();
            fs::write(installs.join(name).join("system.raw"), vec![1u8; 8192]).unwrap();
        }
        let elsewhere = root.path().join("elsewhere/guest-install4");
        fs::create_dir_all(&elsewhere).unwrap();
        fs::create_dir_all(installs.join("kept")).unwrap();
        let profile = json!({"installs":installs});
        let state = json!({
            "verified":{"install":installs.join("guest-install6")},
            "created":[elsewhere, installs.join("guest-install5"), installs.join("guest-install6"),
                installs.join("kept"), installs.join("guest-install7")],
            "attempt":{"install":installs.join("guest-install8")}});
        // Not the verified run, not the newest copy, not a copy outside
        // installs or of another name; the unbooted attempt goes.
        assert_eq!(
            copies_a_start_removes(&profile, &state, &[]),
            vec![
                installs.join("guest-install5"),
                installs.join("guest-install8")
            ]
        );
        assert!(allocated_bytes(&installs.join("guest-install5")) >= 8192);
        assert_eq!(allocated_bytes(&root.path().join("missing")), 0);
    }

    #[test]
    fn the_start_disk_reads_the_bases_disks_and_the_installs_filesystem() {
        let root = tempfile::tempdir().unwrap();
        let (base, installs) = (root.path().join("base"), root.path().join("installs"));
        fs::create_dir_all(&base).unwrap();
        fs::create_dir_all(&installs).unwrap();
        for name in ["system.raw", "auxiliary.raw", "sep.raw"] {
            fs::write(base.join(name), vec![0u8; 4096]).unwrap();
        }
        let profile = json!({"installs":installs});
        let state = json!({"closed":true,"install":base});
        for probe in [true, false] {
            let disk = start_disk(&profile, &state, &[], probe);
            assert_eq!(disk["needed_bytes"], 3 * 4096 + COPY_HEADROOM);
            if disk["free_bytes"].as_u64().unwrap() >= 3 * 4096 + COPY_HEADROOM {
                assert_eq!(disk["enough"], true);
            }
            // Neither reading leaves anything in installs.
            assert_eq!(fs::read_dir(&installs).unwrap().count(), 0);
        }
        // An open run without a verified base, or a base without its disks,
        // says nothing.
        assert!(
            start_disk(
                &profile,
                &json!({"closed":false,"install":base}),
                &[],
                false
            )
            .is_null()
        );
        fs::remove_file(base.join("sep.raw")).unwrap();
        assert!(start_disk(&profile, &state, &[], true).is_null());
    }

    #[test]
    fn an_overlay_start_needs_room_for_the_small_disks_only() {
        let root = tempfile::tempdir().unwrap();
        let (base, installs, helper) = (
            root.path().join("installs/guest-install1"),
            root.path().join("installs"),
            root.path().join("helper"),
        );
        fs::create_dir_all(&base).unwrap();
        fs::create_dir_all(&helper).unwrap();
        fs::write(base.join("system.raw"), vec![0u8; 64 * 4096]).unwrap();
        for name in COPIED_DISKS {
            fs::write(base.join(name), vec![0u8; 4096]).unwrap();
        }
        fs::write(helper.join("qemu-img"), "#!/bin/sh\n").unwrap();
        fs::set_permissions(helper.join("qemu-img"), fs::Permissions::from_mode(0o755)).unwrap();
        let profile = json!({"installs":installs,"start_disks":"overlay",
            "prepare":{"helper_engine":helper.join("qemu-system-x86_64")}});
        let state = json!({"closed":true,"install":base});
        let disk = start_disk(&profile, &state, &[], false);
        assert_eq!(disk["method"], "overlay");
        assert_eq!(disk["needed_bytes"], 2 * 4096 + COPY_HEADROOM);
        // A copy of an overlay base is a flattened raw disk of its virtual size.
        let next = installs.join("guest-install2");
        fs::create_dir_all(&next).unwrap();
        super::disk_overlay::tests::overlay(
            &next.join("system.qcow2"),
            64 * 4096,
            &base.join("system.raw"),
        );
        for name in COPIED_DISKS {
            fs::write(next.join(name), vec![0u8; 4096]).unwrap();
        }
        let copies = json!({"installs":installs,"prepare":{"helper_engine":helper.join("qemu-system-x86_64")}});
        let disk = start_disk(&copies, &json!({"closed":true,"install":next}), &[], true);
        assert_eq!(disk["method"], "flatten");
        assert_eq!(disk["needed_bytes"], 66 * 4096 + COPY_HEADROOM);
    }

    #[test]
    fn copies_an_overlay_reads_are_never_removed() {
        let root = tempfile::tempdir().unwrap();
        let installs = root.path().join("installs");
        let names = ["guest-install5", "guest-install6", "guest-install7"];
        for name in names {
            fs::create_dir_all(installs.join(name)).unwrap();
        }
        fs::write(installs.join("guest-install5/system.raw"), vec![0u8; 4096]).unwrap();
        super::disk_overlay::tests::overlay(
            &installs.join("guest-install6/system.qcow2"),
            4096,
            &installs.join("guest-install5/system.raw"),
        );
        fs::write(installs.join("guest-install7/system.raw"), vec![0u8; 4096]).unwrap();
        let profile = json!({"installs":installs});
        let created: Vec<PathBuf> = names.iter().map(|name| installs.join(name)).collect();
        // The verified run is an overlay on 5: the start keeps 5 as well.
        let state =
            json!({"verified":{"install":created[1]},"install":created[2],"created":created});
        assert!(copies_a_start_removes(&profile, &state, &[]).is_empty());
        let (removable, kept, _) = cleanup_plan(&profile, &state, &[]);
        assert!(removable.is_empty());
        assert!(kept.contains(&json!({"path":created[0],"reason":"overlay-backing"})));
        // Once nothing kept reads it, it goes like any older copy.
        let state =
            json!({"verified":{"install":created[2]},"install":created[2],"created":created});
        assert_eq!(
            copies_a_start_removes(&profile, &state, &[]),
            created[..2].to_vec()
        );
    }
}

#[cfg(test)]
mod profile_list_tests {
    use super::*;

    #[test]
    fn the_list_keeps_each_directory_once_newest_last_and_bounded() {
        let record = json!({"directory":"/b","profiles":["/a","/b","/a",""]});
        assert_eq!(
            remembered(&record, None),
            vec![PathBuf::from("/a"), PathBuf::from("/b")]
        );
        assert_eq!(
            remembered(&record, Some(Path::new("/a"))),
            vec![PathBuf::from("/b"), PathBuf::from("/a")]
        );
        let many: Vec<String> = (0..40).map(|index| format!("/p{index}")).collect();
        let list = remembered(&json!({"profiles":many}), Some(Path::new("/new")));
        assert_eq!(list.len(), PROFILES_KEPT);
        assert_eq!(list.last(), Some(&PathBuf::from("/new")));
        assert!(!list.contains(&PathBuf::from("/p0")));
        assert!(remembered(&json!({}), None).is_empty());
    }
}
