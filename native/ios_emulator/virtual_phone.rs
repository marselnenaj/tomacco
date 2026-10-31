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
fn records(directory: &Path) -> Result<(Value, Value)> {
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

fn directory() -> Result<PathBuf> {
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
        record["directory"] = json!(directory);
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

pub fn status(_a: &Value) -> Result {
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
    Ok(json!({
        "configured":true,"experimental":true,"directory":directory,
        "run":state["run"],"phase":state["phase"],"closed":state["closed"],
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
    records(&directory)?;
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
    }
    jobs::submit(
        label,
        json!({"cwd":home()?,"timeout":timeout,"exclusive":EXCLUSIVE,"single_active":true,
            "operation_key":key,"steps":[{"argv":argv}],
            "always_artifacts":[directory.join("state.json")]}),
    )
}

pub fn start(a: &Value) -> Result {
    // Preparation, a boot of many minutes and the unlock each have their own
    // bound inside the script; this is the outer limit of the whole job.
    submit(
        "ios.virtual.start",
        "start",
        "Start virtual iOS device",
        4800,
        a,
    )
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
    submit_with(
        "ios.virtual.deploy",
        "deploy",
        "Install app on virtual iOS device",
        1800,
        a,
        arguments,
    )
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

/// Whether the state's guest job is still on its way or running.
fn guest_is_running(state: &Value) -> bool {
    state["guest_job"].as_str().is_some_and(|id| {
        jobs::get(id).is_ok_and(|job| !jobs::TERMINAL.contains(&s(&job, "state")))
    })
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
