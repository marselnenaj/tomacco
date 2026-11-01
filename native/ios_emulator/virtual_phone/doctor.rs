//! `ios.virtual.doctor`: one pass/warn/fail report on whether the configured
//! virtual iPhone can start and how it last ended, with a remedy per item.
//!
//! It joins what other parts already check: this machine (`host_check`), the
//! next start's disk copy (`start_disk`, as the start refuses it), the
//! graphics card and host pressure right now (`host_load`), and the profile's
//! own half from tools/ios_virtual_doctor.py: every input profile.json names,
//! whether the guest tools and the engine were built from this checkout's
//! sources, the saved states' disks and the last run's verdict. It reads only;
//! nothing is started, written or repaired.
use super::*;

/// How long the profile half may take: it reads the kernel and the payloads.
const PROFILE_SECONDS: u64 = 300;

fn row(check: &str, status: &str, found: Value, message: String, remedy: Option<String>) -> Value {
    json!({"check":check,"status":status,"found":found,"message":message,
        "remedy":if status == "pass" { Value::Null } else { json!(remedy) }})
}

/// The next start's disk copy, judged as the start judges it.
pub(crate) fn disk_row(disk: &Value) -> Value {
    if disk.is_null() {
        return row(
            "start_disk",
            "warn",
            Value::Null,
            "The base run's disks or the installs filesystem cannot be read, so the next start's disk copy cannot be sized."
                .into(),
            Some("Check that the verified base's disks exist (saved_states, profile_inputs) and that installs is reachable.".into()),
        );
    }
    match refuse_short_disk(disk) {
        Err(error) => row(
            "start_disk",
            "fail",
            disk.clone(),
            error.message.clone(),
            Some(error.remedy.clone()),
        ),
        Ok(()) if disk["enough"].is_null() => row(
            "start_disk",
            "warn",
            disk.clone(),
            format!(
                "The installs filesystem has {} free, less than a full copy needs; whether it can clone the base's disks is only found out by a start.",
                gib(n(disk, "free_bytes"))
            ),
            Some("A start probes the filesystem and refuses with ios_virtual_disk_space if the copy cannot fit; free space first to be sure.".into()),
        ),
        Ok(()) => row(
            "start_disk",
            "pass",
            disk.clone(),
            format!(
                "The next start's disk copy fits: it needs {} and {} is free.",
                gib(n(disk, "needed_bytes")),
                gib(n(disk, "free_bytes"))
            ),
            None,
        ),
    }
}

/// The graphics card and the host's pressure right now, as host_load sees them.
pub(crate) fn load_rows(gpu: &Value, pressure: &Value) -> Vec<Value> {
    let warnings = host_load::warnings(gpu, pressure);
    let message = |check: &str| {
        warnings
            .iter()
            .find(|warning| warning["check"] == check)
            .map(|warning| s(warning, "message").to_string())
    };
    let gpu_row = match (gpu.is_object(), message("gpu")) {
        (false, _) => row(
            "gpu_now",
            "warn",
            Value::Null,
            "No graphics card reports its memory here, so the graphics memory guard's headroom is unknown.".into(),
            Some("ios.virtual.host_check's graphics item says what is known about the card.".into()),
        ),
        (true, Some(text)) => row(
            "gpu_now",
            "warn",
            gpu.clone(),
            text,
            Some("Close other programs that use the graphics card before a start.".into()),
        ),
        (true, None) => row(
            "gpu_now",
            "pass",
            gpu.clone(),
            format!(
                "{} of the graphics card's {} is free, above the guard's limit of {}.",
                gib(n(gpu, "free_bytes")),
                gib(n(gpu, "total_bytes")),
                gib(n(gpu, "guard_limit_free_bytes"))
            ),
            None,
        ),
    };
    let pressure_row = match (pressure["observed"] == true, message("pressure")) {
        (_, Some(text)) => row(
            "pressure_now",
            "warn",
            pressure.clone(),
            text,
            Some("Wait until other disk- or memory-heavy work has finished.".into()),
        ),
        (false, None) => row(
            "pressure_now",
            "warn",
            pressure.clone(),
            "Host pressure cannot be read (no /proc/pressure); a start then admits work without it.".into(),
            Some("Use a kernel with PSI (CONFIG_PSI) to let starts wait out pressure.".into()),
        ),
        (true, None) => row(
            "pressure_now",
            "pass",
            pressure.clone(),
            "The host's I/O and memory pressure are below the limits a start waits for.".into(),
            None,
        ),
    };
    vec![gpu_row, pressure_row]
}

/// The rows tools/ios_virtual_doctor.py printed, or one row saying why there are none.
pub(crate) fn profile_rows(reply: std::result::Result<Value, String>) -> Vec<Value> {
    let failed = |message: String| {
        vec![row(
            "profile_inputs",
            "warn",
            Value::Null,
            message,
            Some("Run python3 tools/ios_virtual_doctor.py --profile PROFILE_DIRECTORY to see the error.".into()),
        )]
    };
    match reply {
        Ok(reply) if reply["ok"] == true => {
            let rows: Vec<Value> = reply["checks"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|row| {
                    row["check"].is_string()
                        && matches!(s(row, "status"), "pass" | "warn" | "fail")
                        && row["message"].is_string()
                })
                .cloned()
                .collect();
            if rows.is_empty() {
                failed("The profile check answered without any checks.".into())
            } else {
                rows
            }
        }
        Ok(reply) => failed(format!(
            "The profile check failed: {}",
            reply["error"].as_str().unwrap_or("no error given")
        )),
        Err(error) => failed(format!("The profile check could not run: {error}")),
    }
}

fn run_profile_check(directory: &Path) -> std::result::Result<Value, String> {
    let argv = vec![
        toolchain::find("python3").map_err(|error| error.message)?,
        research::script("ios_virtual_doctor.py")
            .map_err(|error| error.message)?
            .to_string_lossy()
            .into_owned(),
        "--profile".into(),
        directory.to_string_lossy().into_owned(),
    ];
    let done =
        toolchain::run(&argv, PROFILE_SECONDS, None, false).map_err(|error| error.message)?;
    serde_json::from_str(s(&done, "stdout").trim()).map_err(|_| {
        let stderr = s(&done, "stderr");
        let tail: String = stderr
            .chars()
            .rev()
            .take(300)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect();
        format!("it printed no answer ({tail})")
    })
}

/// The report from all rows.
pub(crate) fn report(checks: Vec<Value>, directory: Value) -> Value {
    let mut answer = host_check::summary(checks);
    answer["directory"] = directory;
    answer["guide"] = json!("docs/virtual-ios/troubleshooting.md");
    answer["note"] = json!(
        "A pass means each input and record checked out and the machine meets what the device was measured with; it does not prove the next start succeeds."
    );
    answer
}

pub fn doctor(_a: &Value) -> Result {
    let mut checks = Vec::new();
    let mut directory_value = Value::Null;
    match directory() {
        Err(error) if error.code == "ios_virtual_unconfigured" => checks.push(row(
            "profile",
            "fail",
            Value::Null,
            "No virtual iOS device profile is configured.".into(),
            Some(REMEDY_NO_PROFILE.into()),
        )),
        Err(error) => return Err(error),
        Ok(directory) => {
            directory_value = json!(directory);
            match records(&directory) {
                Err(error) => checks.push(row(
                    "profile",
                    "fail",
                    json!({"directory":directory,"error":error.value()}),
                    error.message.clone(),
                    Some("python3 tools/ios_virtual_profile.py --check lists what is missing; see docs/virtual-ios/setup.md".into()),
                )),
                Ok((profile, state)) => {
                    checks.push(row(
                        "profile",
                        "pass",
                        json!({"directory":directory}),
                        "profile.json and state.json are readable.".into(),
                        None,
                    ));
                    checks.extend(profile_rows(run_profile_check(&directory)));
                    checks.push(disk_row(&start_disk(
                        &profile,
                        &state,
                        &states::pinned(&directory),
                        false,
                    )));
                }
            }
        }
    }
    checks.extend(host_check::evaluate(&host_check::gather(&json!({}))?));
    let guest = virtual_phone_guest();
    let host = host_load::host(guest.as_ref());
    checks.extend(load_rows(&host["gpu"], &host["pressure"]));
    Ok(report(checks, directory_value))
}

/// The configured phone's guest job while it runs, for the guard's own view.
fn virtual_phone_guest() -> Option<Value> {
    let (_, state) = records(&directory().ok()?).ok()?;
    let job = jobs::get(state["guest_job"].as_str()?).ok()?;
    (!jobs::TERMINAL.contains(&s(&job, "state"))).then_some(job)
}

#[cfg(test)]
mod tests {
    use super::*;

    const G: u64 = 1 << 30;

    #[test]
    fn the_disk_row_judges_the_copy_as_the_start_does() {
        let fits = json!({"needed_bytes":33 * G,"free_bytes":400 * G,"reclaimable_bytes":0,
            "enough":true,"installs":"/phone/installs"});
        assert_eq!(disk_row(&fits)["status"], "pass");
        let short = json!({"needed_bytes":33 * G,"free_bytes":10 * G,"reclaimable_bytes":0,
            "enough":false,"installs":"/phone/installs"});
        let failed = disk_row(&short);
        assert_eq!(failed["status"], "fail");
        assert!(s(&failed, "message").starts_with("The start copies 33.0 GiB of disks"));
        assert!(s(&failed, "remedy").contains("Free at least 23.0 GiB"));
        let open = json!({"needed_bytes":33 * G,"free_bytes":10 * G,"enough":null});
        assert_eq!(disk_row(&open)["status"], "warn");
        assert_eq!(disk_row(&Value::Null)["status"], "warn");
    }

    #[test]
    fn the_card_and_pressure_now_are_rows_of_their_own() {
        let quiet = host_load::pressure(|_| {
            Some("full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n".into())
        });
        let card = |used: u64| {
            host_load::gpu(
                None,
                &[host_check::Card {
                    pci: "0000:03:00.0".into(),
                    driver: "amdgpu".into(),
                    vram_total: Some(16 * G),
                    vram_used: Some(used),
                }],
                0.0,
            )
        };
        let rows = load_rows(&card(G), &quiet);
        assert_eq!(
            rows.iter().map(|row| s(row, "status")).collect::<Vec<_>>(),
            ["pass", "pass"]
        );
        let rows = load_rows(&card(15 * G), &quiet);
        assert_eq!(
            (rows[0]["check"].clone(), rows[0]["status"].clone()),
            (json!("gpu_now"), json!("warn"))
        );
        let unknown = load_rows(&Value::Null, &host_load::pressure(|_| None));
        assert_eq!(
            unknown
                .iter()
                .map(|row| s(row, "status"))
                .collect::<Vec<_>>(),
            ["warn", "warn"]
        );
        let busy = host_load::pressure(|_| {
            Some("full avg10=90.00 avg60=90.00 avg300=0.00 total=0\n".into())
        });
        assert!(s(&load_rows(&card(G), &busy)[1], "message").contains("pressure"));
    }

    #[test]
    fn the_profile_half_is_taken_as_printed_or_said_to_be_missing() {
        let printed = json!({"ok":true,"checks":[
            {"check":"profile_inputs","status":"fail","found":{},"message":"1 of 18 inputs","remedy":"make it"},
            {"check":"bad","status":"maybe","message":"x"},
            {"check":"last_run","status":"pass","found":{},"message":"fine","remedy":null}]});
        let rows = profile_rows(Ok(printed));
        assert_eq!(
            rows.iter().map(|row| s(row, "check")).collect::<Vec<_>>(),
            ["profile_inputs", "last_run"]
        );
        for reply in [
            Ok(json!({"ok":false,"error":"profile.json: bad"})),
            Ok(json!({"ok":true,"checks":[]})),
            Err("python3 not found".to_string()),
        ] {
            let rows = profile_rows(reply);
            assert_eq!(rows.len(), 1);
            assert_eq!(
                (rows[0]["check"].clone(), rows[0]["status"].clone()),
                (json!("profile_inputs"), json!("warn"))
            );
        }
    }

    #[test]
    fn the_report_counts_and_points_at_troubleshooting() {
        let checks = vec![
            row("a", "pass", Value::Null, "ok".into(), None),
            row("b", "warn", Value::Null, "hm".into(), Some("do".into())),
            row("c", "fail", Value::Null, "no".into(), Some("fix".into())),
        ];
        let answer = report(checks, json!("/phone"));
        assert_eq!(
            (
                answer["status"].clone(),
                answer["ready"].clone(),
                answer["failed"].clone(),
                answer["warned"].clone()
            ),
            (json!("fail"), json!(false), json!(1), json!(1))
        );
        assert_eq!(answer["guide"], "docs/virtual-ios/troubleshooting.md");
        assert_eq!(answer["directory"], "/phone");
        assert!(answer["checks"][0]["remedy"].is_null());
    }
}
