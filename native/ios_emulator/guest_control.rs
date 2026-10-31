//! Host-only requests to the owned native service through virtio-vsock.
use super::*;
use std::io::ErrorKind;
use std::os::unix::fs::FileTypeExt;

fn pending(deadline: Instant) -> Result<()> {
    check_cancelled()?;
    if Instant::now() >= deadline {
        return Err(Error::new(
            "ios_guest_timeout",
            "The native guest did not reply before the request deadline",
        ));
    }
    Ok(())
}

fn send(stream: &mut UnixStream, mut bytes: &[u8], deadline: Instant) -> Result<()> {
    while !bytes.is_empty() {
        pending(deadline)?;
        match stream.write(bytes) {
            Ok(0) => {
                return Err(Error::new(
                    "ios_guest_disconnected",
                    "The guest control stream closed while sending",
                ));
            }
            Ok(count) => bytes = &bytes[count..],
            Err(error)
                if matches!(
                    error.kind(),
                    ErrorKind::Interrupted | ErrorKind::WouldBlock | ErrorKind::TimedOut
                ) => {}
            Err(error) => return Err(error.into()),
        }
    }
    pending(deadline)
}

fn line(reader: &mut BufReader<UnixStream>, limit: usize, deadline: Instant) -> Result<String> {
    let mut bytes = Vec::new();
    loop {
        pending(deadline)?;
        let available = match reader.fill_buf() {
            Ok(bytes) => bytes,
            Err(error)
                if matches!(
                    error.kind(),
                    ErrorKind::Interrupted | ErrorKind::WouldBlock | ErrorKind::TimedOut
                ) =>
            {
                continue;
            }
            Err(error) => return Err(error.into()),
        };
        // A blocking read may finish just after the deadline. Its final
        // newline must not turn that late response into an on-time result.
        pending(deadline)?;
        if available.is_empty() {
            return Err(Error::new(
                "ios_guest_disconnected",
                "The guest closed the stream without a complete response",
            ));
        }
        let newline = available.iter().position(|byte| *byte == b'\n');
        let count = newline.map_or(available.len(), |index| index + 1);
        if count > limit - bytes.len() {
            return Err(Error::new(
                "invalid_guest_response",
                "Guest response exceeds the protocol limit",
            ));
        }
        bytes.extend_from_slice(&available[..count]);
        reader.consume(count);
        if newline.is_some() {
            break;
        }
    }
    String::from_utf8(bytes)
        .map_err(|_| Error::new("invalid_guest_response", "Guest response is not UTF-8"))
}

/// The launcher publishes the engine exit before its complete media hashes.
/// A still-running job in that interval has no live native guest transport.
/// Only the owned, bounded final receipt establishes this distinction; a
/// refused pre-launch result or a missing socket does not prove engine exit.
fn ended_emulator(output: &Path) -> Result<Option<Value>> {
    let path = output.join("result.json");
    let metadata = match fs::symlink_metadata(&path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    if !metadata.is_file()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.len() > 1024 * 1024
    {
        return Err(Error::new(
            "invalid_report_path",
            "Guest completion receipt must be an owned regular file of at most 1 MiB",
        ));
    }
    let result = read_json(&path, None)?;
    if result["format"] == 1
        && result["machine"] == "tomacco-vphone"
        && result["exit_code"].as_i64().is_some()
        && result["elapsed_seconds"]
            .as_f64()
            .is_some_and(|seconds| seconds.is_finite() && seconds >= 0.0)
    {
        Ok(Some(result))
    } else {
        Ok(None)
    }
}

pub fn call(a: &Value) -> Result {
    call_with_deadline(a, None)
}

/// Deadlines of one careful stop, in seconds: the status query, then the flush
/// asked for up to twice (the second time with the longest deadline). Together
/// with `control::LANE_WAIT` and `SERVICE_WAIT` they are the longest a stop
/// takes before the emulator is told to exit. A caller that waits for the stop
/// (`SAVED_STOP_SECONDS` in tools/run_ios_paravirt.py) must wait longer; the
/// tests of that launcher read these constants.
const STATUS_SECONDS: u64 = 15;
const FLUSH_SECONDS: [u64; 2] = [60, 120];
/// How long a stop waits in total for the service lane, which a concurrent
/// diagnostic read of another client may hold for a moment.
const SERVICE_WAIT: Duration = Duration::from_secs(10);

/// A status or flush request of a stop that finds the service lane taken.
/// The framework lane is already held by `reserve_idle`, so `ios_guest_busy`
/// from these requests can only be a collision on the separate service lane:
/// another client's status, log or cancel request, or a receipt lookup. That
/// is asked again for a short while, `waited` being spent across all of the
/// stop's requests. Worker state is checked separately; this helper only
/// retries service-lane contention and never submits a framework operation.
fn call_service(request: &Value, waited: &mut Duration) -> Result {
    call_service_until(request, waited, None)
}

fn call_service_until(request: &Value, waited: &mut Duration, deadline: Option<Instant>) -> Result {
    loop {
        match call_with_deadline(request, deadline) {
            Err(error) if error.code == "ios_guest_busy" && *waited < SERVICE_WAIT => {
                check_cancelled()?;
                if let Some(deadline) = deadline {
                    pending(deadline)?;
                }
                let started = Instant::now();
                thread::sleep(Duration::from_millis(50));
                *waited += started.elapsed();
            }
            other => return other,
        }
    }
}

/// A completed query can reply before the service reaps its child. Only a
/// fully identified read-only worker gets time to finish; mutations and
/// incomplete status remain an immediate refusal. Nothing is replayed.
fn readonly_worker_finishing(response: &Value) -> bool {
    response["ok"] == true
        && response["worker_running"] == true
        && response["worker_pid"]
            .as_u64()
            .is_some_and(|pid| pid > 0 && pid <= i32::MAX as u64)
        && response["worker_request_id"].as_str().is_some_and(|id| {
            id.len() == 32
                && id
                    .bytes()
                    .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        })
        && matches!(
            s(response, "worker_operation"),
            "inspect"
                | "lookup"
                | "logs"
                | "setup"
                | "screen_state"
                | "display_state"
                | "foreground_state"
                | "graphics_state"
                | "surface_state"
                | "crash_reports"
        )
}

/// Hold the framework lane from the idle observation through emulator exit.
/// A host timeout releases that lane before its guest worker necessarily ends.
/// The second value reports whether the guest confirmed a storage flush; older
/// guest tools without that capability are stopped as before.
pub fn reserve_idle(id: &str) -> Result<(Lock, bool)> {
    let lock = Lock::open(
        &home()?
            .join("locks")
            .join(format!("ios-guest-request-{id}.lock")),
    )?;
    if !lock.acquire(true)? {
        return Err(Error::new(
            "ios_guest_busy",
            "A native framework request is still in progress; the emulator was not stopped",
        ));
    }
    let mut waited = Duration::ZERO;
    // All status observations share the original status deadline. Keep the
    // framework reservation throughout and require a fresh native idle reply
    // before flush or quit. A different worker identity is never followed.
    let request = json!({"job_id":id,"operation":"status","timeout_seconds":STATUS_SECONDS});
    let deadline = Instant::now() + Duration::from_secs(STATUS_SECONDS);
    let mut status = call_service_until(&request, &mut waited, Some(deadline))?;
    let initial = status["response"].clone();
    while readonly_worker_finishing(&status["response"])
        && status["response"]["worker_pid"] == initial["worker_pid"]
        && status["response"]["worker_request_id"] == initial["worker_request_id"]
        && status["response"]["worker_operation"] == initial["worker_operation"]
    {
        check_cancelled()?;
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        thread::sleep(remaining.min(Duration::from_millis(200)));
        if Instant::now() >= deadline {
            break;
        }
        match call_service_until(&request, &mut waited, Some(deadline)) {
            Ok(next) => status = next,
            Err(error) if error.code == "ios_guest_timeout" => break,
            Err(error) => return Err(error),
        }
    }
    let response = &status["response"];
    if response["ok"].as_bool() != Some(true) || response["worker_running"].as_bool() != Some(false)
    {
        return Err(Error::new(
            "ios_guest_not_idle",
            "The native worker has not confirmed it is idle; the emulator was not stopped",
        )
        .details(json!({"job_id":id,"native_status":response})));
    }
    // A failed or missing flush is reported, not hidden; it must not make an
    // unresponsive guest impossible to stop. A loaded guest, however, can
    // need longer than one deadline to write everything out, and stopping it
    // unflushed loses its session: it is asked a second time, with the
    // longest deadline, before the stop goes ahead without the confirmation.
    let mut flushed = false;
    if response["storage_flush_supported"] == true {
        for seconds in FLUSH_SECONDS {
            let request = json!({"job_id":id,"operation":"flush","timeout_seconds":seconds});
            match call_service(&request, &mut waited) {
                Ok(reply) if reply["response"]["storage_flushed"] == true => {
                    flushed = true;
                    break;
                }
                // A client that withdrew the stop has not seen a flush fail:
                // the stop ends here instead of going ahead unconfirmed.
                Err(error) if error.code == "request_cancelled" => return Err(error),
                _ => {}
            }
        }
    }
    Ok((lock, flushed))
}

fn surface_ids_valid(value: &str) -> bool {
    if value.is_empty() || value.len() > 43 {
        return false;
    }
    let mut ids = [0u32; 4];
    let mut size = 0;
    for part in value.split(',') {
        if size == ids.len()
            || part.is_empty()
            || (part.len() > 1 && part.starts_with('0'))
            || !part.bytes().all(|byte| byte.is_ascii_digit())
        {
            return false;
        }
        let Ok(id) = part.parse::<u32>() else {
            return false;
        };
        if ids[..size].contains(&id) {
            return false;
        }
        ids[size] = id;
        size += 1;
    }
    size != 0
}

#[cfg(test)]
mod stop_status_tests {
    use super::*;

    #[test]
    fn only_identified_readonly_workers_can_be_observed_again() {
        let row = json!({"ok":true,"worker_running":true,"worker_pid":150,
            "worker_operation":"surface_state","worker_request_id":"0123456789abcdef0123456789abcdef"});
        assert!(readonly_worker_finishing(&row));
        let mut foreground = row.clone();
        foreground["worker_operation"] = json!("foreground_state");
        assert!(readonly_worker_finishing(&foreground));
        for (key, value) in [
            ("ok", json!(false)),
            ("worker_running", json!(false)),
            ("worker_pid", json!(0)),
            ("worker_pid", json!(-1)),
            ("worker_pid", json!(2147483648u64)),
            ("worker_pid", json!("150")),
            ("worker_request_id", json!(null)),
            (
                "worker_request_id",
                json!("ABCDEF0123456789abcdef0123456789"),
            ),
            (
                "worker_request_id",
                json!("0123456789abcdef0123456789abcdef0"),
            ),
            ("worker_operation", json!("install")),
            ("worker_operation", json!("open")),
            ("worker_operation", json!("display_refresh")),
            ("worker_operation", json!("unknown")),
        ] {
            let mut invalid = row.clone();
            invalid[key] = value;
            assert!(!readonly_worker_finishing(&invalid), "{invalid}");
        }
    }
}

#[cfg(test)]
mod surface_query_tests {
    use super::surface_ids_valid;

    #[test]
    fn surface_query_public_schema_accepts_operation() {
        let operations: serde_json::Value =
            serde_json::from_str(include_str!("../../assets/operations.json")).unwrap();
        let operation = operations
            .as_array()
            .unwrap()
            .iter()
            .find(|entry| entry["name"] == "ios.emulator.guest")
            .unwrap();
        assert!(
            operation["inputSchema"]["properties"]["operation"]["enum"]
                .as_array()
                .unwrap()
                .iter()
                .any(|value| value == "surface_state")
        );
    }

    #[test]
    fn surface_ids_validation() {
        for value in ["0", "7", "4294967295", "0,7,4294967295,12"] {
            assert!(surface_ids_valid(value), "{value}");
        }
        for value in [
            "",
            "-1",
            "+1",
            " 1",
            "1 ",
            "01",
            "1,,2",
            ",1",
            "1,",
            "1,1",
            "4294967296",
            "1,2,3,4,5",
            "1x",
            "١",
            "1\0",
            "9999999999999999999999999999999999999999999999",
        ] {
            assert!(!surface_ids_valid(value), "{value:?}");
        }
    }
}

#[cfg(test)]
mod persistence_probe_tests {
    use super::persistence_challenge_valid;

    #[test]
    fn only_bounded_create_read_classes_or_inode_arguments_are_accepted() {
        let challenge = "0123456789abcdef".repeat(4);
        for action in ["create", "read", "classes"] {
            assert!(persistence_challenge_valid(&format!(
                "{action}:{challenge}"
            )));
        }
        for inode in ["0", "191838", "1234567890123456789"] {
            assert!(persistence_challenge_valid(&format!("inode:{inode}")));
        }
        for value in [
            "inode:".to_string(),
            "inode:12345678901234567890".to_string(),
            "inode:19x".to_string(),
            "inode:-1".to_string(),
            format!("inode:{challenge}"),
            String::new(),
            challenge.clone(),
            format!("delete:{challenge}"),
            format!("class:{challenge}"),
            format!("read:/{challenge}"),
            format!("create:{}", "a".repeat(63)),
            format!("create:{}", "a".repeat(65)),
            format!("classes:{}", "a".repeat(63)),
            format!("read:{}", "A".repeat(64)),
            format!("read:{}", "g".repeat(64)),
            format!("read:{challenge}\0"),
            format!("read:{challenge}:x"),
        ] {
            assert!(!persistence_challenge_valid(&value), "{value:?}");
        }
    }

    #[test]
    fn public_schema_lists_the_persistence_probe() {
        let operations: serde_json::Value =
            serde_json::from_str(include_str!("../../assets/operations.json")).unwrap();
        let operation = operations
            .as_array()
            .unwrap()
            .iter()
            .find(|entry| entry["name"] == "ios.emulator.guest")
            .unwrap();
        assert!(
            operation["inputSchema"]["properties"]["operation"]["enum"]
                .as_array()
                .unwrap()
                .iter()
                .any(|value| value == "persistence_probe")
        );
    }
}

#[cfg(test)]
mod watchdog_scale_tests {
    use super::watchdog_scale_valid;

    #[test]
    fn only_original_or_a_bounded_whole_factor_is_accepted() {
        for value in ["original", "2", "8", "10", "30"] {
            assert!(watchdog_scale_valid(value), "{value:?}");
        }
        for value in [
            "", "0", "1", "02", "31", "100", "-4", "8.5", "+8", " 8", "8 ", "Original", "default",
        ] {
            assert!(!watchdog_scale_valid(value), "{value:?}");
        }
    }

    #[test]
    fn public_schema_lists_the_watchdog_scale() {
        let operations: serde_json::Value =
            serde_json::from_str(include_str!("../../assets/operations.json")).unwrap();
        let operation = operations
            .as_array()
            .unwrap()
            .iter()
            .find(|entry| entry["name"] == "ios.emulator.guest")
            .unwrap();
        assert!(
            operation["inputSchema"]["properties"]["operation"]["enum"]
                .as_array()
                .unwrap()
                .iter()
                .any(|value| value == "watchdog_scale")
        );
    }
}

#[cfg(test)]
mod icon_cache_tests {
    #[test]
    fn public_schema_lists_the_icon_cache_clear() {
        let operations: serde_json::Value =
            serde_json::from_str(include_str!("../../assets/operations.json")).unwrap();
        let operation = operations
            .as_array()
            .unwrap()
            .iter()
            .find(|entry| entry["name"] == "ios.emulator.guest")
            .unwrap();
        assert!(
            operation["inputSchema"]["properties"]["operation"]["enum"]
                .as_array()
                .unwrap()
                .iter()
                .any(|value| value == "icon_cache")
        );
    }
}

#[cfg(test)]
mod guest_request_shape_tests {
    use super::*;

    /// Whether `call_with_deadline` accepts a request's operation and
    /// argument. The job ID is checked only after them, so an accepted shape
    /// fails on this job ID (`invalid_argument`) before any lock or socket is
    /// touched, and a refused one on the request itself.
    fn accepted(operation: &str, argument: Option<&str>) -> bool {
        let mut request = json!({"job_id":"not-a-job","operation":operation});
        if let Some(argument) = argument {
            request["argument"] = json!(argument);
        }
        match call_with_deadline(&request, None) {
            Err(error) if error.code == "invalid_argument" => true,
            Err(error) if error.code == "invalid_guest_request" => false,
            Err(error) => panic!("unexpected error {}", error.code),
            Ok(_) => panic!("a request with this job ID cannot succeed"),
        }
    }

    #[test]
    fn crash_reports_take_any_report_name_prefix() {
        let longest = "a".repeat(255);
        for value in [
            "SpringBoard",
            "com.apple.MapKit.SnapshotService",
            "MyApp",
            "a",
            "x-y_z.1",
            "Settings-2026-10-28",
            longest.as_str(),
        ] {
            assert!(accepted("crash_reports", Some(value)), "{value:?}");
        }
        let too_long = "a".repeat(256);
        for value in [
            "",
            ".hidden",
            "-x",
            "a/b",
            "/SpringBoard",
            "a b",
            "ä",
            "a\0",
            "Spring\nBoard",
            too_long.as_str(),
        ] {
            assert!(!accepted("crash_reports", Some(value)), "{value:?}");
        }
        assert!(!accepted("crash_reports", None));
    }

    #[test]
    fn logs_include_the_quiet_record() {
        for value in ["quiet", "application", "battery", "buffer_texture"] {
            assert!(accepted("logs", Some(value)), "{value:?}");
        }
        for value in ["Quiet", "quiet ", "research-app", ""] {
            assert!(!accepted("logs", Some(value)), "{value:?}");
        }
        assert!(!accepted("logs", None));
    }

    #[test]
    fn time_zone_takes_a_zone_file_name() {
        let longest = "a".repeat(32);
        for value in [
            "UTC",
            "Europe/Vienna",
            "America/Argentina/Buenos_Aires",
            "Etc/GMT+1",
            "Etc/GMT-14",
            longest.as_str(),
        ] {
            assert!(accepted("time_zone", Some(value)), "{value:?}");
        }
        let too_long = "a".repeat(33);
        for value in [
            "",
            "/",
            "/Europe/Vienna",
            "Europe/",
            "Europe//Vienna",
            "A/B/C/D",
            "../zoneinfo",
            "Europe/Vienna.",
            "Europe Vienna",
            "Europe/Wien\0",
            "Europe/Zürich",
            too_long.as_str(),
        ] {
            assert!(!accepted("time_zone", Some(value)), "{value:?}");
        }
        assert!(!accepted("time_zone", None));
    }
}

#[cfg(test)]
mod host_channel_tests {
    use super::*;

    #[test]
    fn a_policy_entry_is_a_hash_and_an_identifier() {
        let hash = "0123456789abcdef0123456789abcdef01234567";
        assert!(policy_entry_valid(&format!("{hash}:dev.tomacco.hello")));
        for refused in [
            String::new(),
            "dev.tomacco.hello".to_string(),
            format!("{hash}:"),
            format!("{hash}:.hidden"),
            format!("{hash}:-flag"),
            format!("{hash}:a/b"),
            format!("{}:dev.tomacco.hello", &hash[1..]),
            format!("{}:dev.tomacco.hello", hash.to_uppercase()),
        ] {
            assert!(!policy_entry_valid(&refused), "{refused}");
        }
    }

    #[test]
    fn a_debug_target_is_a_pid_or_a_process_name() {
        for accepted in ["551", "HelloTomacco", "Tomacco-Sample_1.2"] {
            assert!(debug_target_valid(accepted));
        }
        for refused in ["", "-flag", ".hidden", "a b", "a/b", "a;b", &"x".repeat(65)] {
            assert!(!debug_target_valid(refused), "{refused}");
        }
    }

    #[test]
    fn host_channel_operations_recover_a_receipt_and_are_in_the_schema() {
        let operations: Value =
            serde_json::from_str(include_str!("../../assets/operations.json")).unwrap();
        let guest = operations
            .as_array()
            .unwrap()
            .iter()
            .find(|entry| entry["name"] == "ios.emulator.guest")
            .unwrap();
        let listed = guest["inputSchema"]["properties"]["operation"]["enum"]
            .as_array()
            .unwrap();
        for name in [
            "receive_bundle",
            "policy_add",
            "debug_server",
            "network_state",
            "hardware_probe",
            "gestalt",
            "developer_mode",
            "keyboard_layout",
        ] {
            assert!(receipt_operation(name), "{name}");
            assert!(listed.iter().any(|value| value == name), "{name}");
        }
    }
}

#[cfg(test)]
mod auto_lock_tests {
    use super::*;

    fn accepted(argument: Option<&str>) -> bool {
        let mut request = json!({"job_id":"not-a-job","operation":"auto_lock"});
        if let Some(argument) = argument {
            request["argument"] = json!(argument);
        }
        match call_with_deadline(&request, None) {
            Err(error) if error.code == "invalid_argument" => true,
            Err(error) if error.code == "invalid_guest_request" => false,
            Err(error) => panic!("unexpected error {}", error.code),
            Ok(_) => panic!("a request with this job ID cannot succeed"),
        }
    }

    #[test]
    fn auto_lock_takes_never_or_original() {
        for value in ["never", "original"] {
            assert!(accepted(Some(value)), "{value:?}");
        }
        for value in [
            "",
            "Never",
            "NEVER",
            "never ",
            "off",
            "0",
            "30",
            "default",
            "original\0",
        ] {
            assert!(!accepted(Some(value)), "{value:?}");
        }
        assert!(!accepted(None));
    }

    #[test]
    fn an_auto_lock_receipt_can_be_recovered() {
        assert!(receipt_operation("auto_lock"));
        assert!(!receipt_operation("autolock"));
    }

    #[test]
    fn public_schema_lists_the_auto_lock() {
        let operations: serde_json::Value =
            serde_json::from_str(include_str!("../../assets/operations.json")).unwrap();
        let operation = operations
            .as_array()
            .unwrap()
            .iter()
            .find(|entry| entry["name"] == "ios.emulator.guest")
            .unwrap();
        assert!(
            operation["inputSchema"]["properties"]["operation"]["enum"]
                .as_array()
                .unwrap()
                .iter()
                .any(|value| value == "auto_lock")
        );
    }
}

#[cfg(test)]
mod time_zone_tests {
    use super::{crash_report_filter_valid, receipt_operation, time_zone_valid};

    #[test]
    fn only_a_bounded_zone_file_name_is_accepted() {
        let longest = "A".repeat(32);
        for value in [
            "UTC",
            "Europe/Vienna",
            "America/Argentina/Buenos_Aires",
            "Etc/GMT+1",
            "GMT-0",
            longest.as_str(),
            &format!("{longest}/{longest}/{longest}"),
        ] {
            assert!(time_zone_valid(value), "{value:?}");
        }
        let too_long = "A".repeat(33);
        for value in [
            "",
            "/",
            "/UTC",
            "UTC/",
            "Europe//Vienna",
            "A/B/C/D",
            "..",
            "../zoneinfo",
            "Europe/Vienna.",
            "Europe Vienna",
            "Europe/Wien\0",
            "Europe/Zürich",
            "UTC\n",
            too_long.as_str(),
            &format!("Europe/{too_long}"),
        ] {
            assert!(!time_zone_valid(value), "{value:?}");
        }
    }

    #[test]
    fn a_crash_report_filter_is_a_bounded_name_never_a_path() {
        let longest = "a".repeat(255);
        for value in [
            "a",
            "SpringBoard",
            "com.apple.MapKit.SnapshotService",
            "My_App-1",
            "0",
        ] {
            assert!(crash_report_filter_valid(value), "{value:?}");
        }
        assert!(crash_report_filter_valid(&longest));
        for value in [
            "", ".", "..", ".hidden", "-x", "a/b", "/a", "a b", "ä", "a\0", "a\n",
        ] {
            assert!(!crash_report_filter_valid(value), "{value:?}");
        }
        assert!(!crash_report_filter_valid(&"a".repeat(256)));
    }

    #[test]
    fn a_time_zone_receipt_can_be_recovered() {
        for value in [
            "time_zone",
            "watchdog_scale",
            "icon_cache",
            "crash_reports",
            "open",
        ] {
            assert!(receipt_operation(value), "{value:?}");
        }
        for value in ["status", "flush", "cancel", "", "timezone", "Time_Zone"] {
            assert!(!receipt_operation(value), "{value:?}");
        }
    }

    #[test]
    fn public_schema_lists_the_time_zone() {
        let operations: serde_json::Value =
            serde_json::from_str(include_str!("../../assets/operations.json")).unwrap();
        let operation = operations
            .as_array()
            .unwrap()
            .iter()
            .find(|entry| entry["name"] == "ios.emulator.guest")
            .unwrap();
        assert!(
            operation["inputSchema"]["properties"]["operation"]["enum"]
                .as_array()
                .unwrap()
                .iter()
                .any(|value| value == "time_zone")
        );
    }
}

/// `original`, or a whole FrontBoard launch-watchdog factor from 2 to 30
/// without a leading zero; the guest service applies the same rule.
fn watchdog_scale_valid(value: &str) -> bool {
    value == "original"
        || (!value.starts_with('0')
            && value.len() <= 2
            && value.bytes().all(|c| c.is_ascii_digit())
            && value
                .parse::<u8>()
                .is_ok_and(|scale| (2..=30).contains(&scale)))
}

/// `create:`/`read:` own the Class C challenge; `classes:` surveys one file
/// per protection class A..D with the same challenge and `inode:<decimal>`
/// looks one file up by inode without opening it (both guest tools from 370;
/// older guest tools answer `invalid_request`). The guest applies the same
/// rule (`persistence_probe_argument` in guest-persistence-probe.h).
fn persistence_challenge_valid(value: &str) -> bool {
    value
        .split_once(':')
        .is_some_and(|(action, argument)| match action {
            "create" | "read" | "classes" => {
                argument.len() == 64
                    && argument
                        .bytes()
                        .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
            }
            "inode" => {
                (1..=19).contains(&argument.len()) && argument.bytes().all(|c| c.is_ascii_digit())
            }
            _ => false,
        })
}

/// The name a crash report file starts with (ReportCrash names a report
/// `<process>-<date>.ips`): 1 to 255 ASCII letters, digits, '.', '_' or
/// '-', not starting with '.' or '-', so never a path. The guest applies the
/// same rule (`crash_process_allowed` in guest-crash-reports.h); guest tools
/// before 368 answer `unsupported_crash_process` for all but seven fixed
/// names. The host projection of the reply (`crash_reports::project`)
/// accepts the same filters.
pub(super) fn crash_report_filter_valid(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 255
        && !value.starts_with('.')
        && !value.starts_with('-')
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
}

/// A zone file name under the guest's /var/db/timezone/zoneinfo: one to
/// three '/'-separated components of 1 to 32 ASCII letters, digits, '_',
/// '-' or '+' each (`UTC`, `Europe/Vienna`, `Etc/GMT+1`), no empty component
/// and no leading slash. The guest applies the same rule
/// (`time_zone_argument` in guest-time-zone.h).
fn time_zone_valid(value: &str) -> bool {
    let mut components = 0;
    value.split('/').all(|component| {
        components += 1;
        components <= 3
            && !component.is_empty()
            && component.len() <= 32
            && component
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"_-+".contains(&c))
    })
}

fn word_valid(value: &str, limit: usize, allowed: impl Fn(u8) -> bool) -> bool {
    !value.is_empty() && value.len() <= limit && value.bytes().all(allowed)
}

/// `policy_add_argument` in guest-host-channel.h.
fn policy_entry_valid(value: &str) -> bool {
    let Some((cdhash, identifier)) = value.split_once(':') else {
        return false;
    };
    cdhash.len() == 40
        && cdhash
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        && word_valid(identifier, 255, |c| {
            c.is_ascii_alphanumeric() || c == b'.' || c == b'-'
        })
        && !identifier.starts_with(['.', '-'])
}

/// `debug_target_valid` in guest-host-channel.h.
fn debug_target_valid(value: &str) -> bool {
    word_valid(value, 64, |c| {
        c.is_ascii_alphanumeric() || b"._-".contains(&c)
    }) && !value.starts_with(['.', '-'])
}

/// The operations whose worker writes a durable receipt that
/// `ios.emulator.guest_result` may recover: every framework worker except
/// the service's own status, flush, cancel and direct file logs.
fn receipt_operation(value: &str) -> bool {
    matches!(
        value,
        "install"
            | "register"
            | "open"
            | "inspect"
            | "lookup"
            | "logs"
            | "setup"
            | "setup_finish"
            | "restart_ui"
            | "effects"
            | "watchdog_scale"
            | "icon_cache"
            | "time_zone"
            | "auto_lock"
            | "screen_state"
            | "display_state"
            | "foreground_state"
            | "graphics_state"
            | "surface_state"
            | "crash_reports"
            | "persistence_probe"
            | "display_refresh"
            | "receive_bundle"
            | "policy_add"
            | "debug_server"
            | "network_state"
            | "hardware_probe"
            | "gestalt"
            | "developer_mode"
            | "keyboard_layout"
    )
}

fn call_with_deadline(a: &Value, shared_deadline: Option<Instant>) -> Result {
    if let Some(deadline) = shared_deadline {
        pending(deadline)?;
    }
    let id = s(a, "job_id");
    let operation = s(a, "operation");
    let argument = a.get("argument").and_then(Value::as_str);
    let bundle_id = a.get("bundle_id").and_then(Value::as_str);
    if a.get("bundle_id").is_some()
        && (operation != "install"
            || bundle_id.is_none_or(|value| {
                value.is_empty()
                    || value.len() > 255
                    || !value
                        .bytes()
                        .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
            }))
    {
        return Err(Error::new(
            "invalid_guest_request",
            "bundle_id must be an application identifier supplied with install",
        ));
    }
    if operation == "install"
        && argument.is_some_and(|path| path.ends_with(".ipa"))
        && bundle_id.is_none()
    {
        return Err(Error::new(
            "invalid_guest_request",
            "Installing a guest IPA requires its bundle_id; app bundles provide their own Info.plist",
        ));
    }
    let valid = match (operation, argument) {
        (
            "inspect" | "status" | "cancel" | "flush" | "setup" | "setup_finish" | "restart_ui"
            | "screen_state" | "display_state" | "foreground_state" | "display_refresh"
            | "graphics_state" | "receive_bundle" | "network_state" | "keyboard_layout",
            None,
        ) => true,
        (
            "logs",
            Some(
                "application" | "platform" | "control" | "operation" | "development" | "quiet"
                | "battery" | "debugserver" | "buffer_texture",
            ),
        ) => true,
        ("crash_reports", Some(value)) => crash_report_filter_valid(value),
        ("effects", Some("reduced" | "original")) => true,
        ("watchdog_scale", Some(value)) => watchdog_scale_valid(value),
        ("icon_cache", Some("clear")) => true,
        ("time_zone", Some(value)) => time_zone_valid(value),
        ("auto_lock", Some("never" | "original")) => true,
        ("policy_add", Some(value)) => policy_entry_valid(value),
        ("debug_server", Some(value)) => debug_target_valid(value),
        ("hardware_probe", Some(value)) => {
            word_valid(value, 96, |c| c.is_ascii_alphanumeric() || c == b'_')
        }
        ("gestalt", Some(value)) => {
            word_valid(value, 96, |c| c.is_ascii_alphanumeric() || c == b'-')
        }
        ("developer_mode", Some("status" | "enable")) => true,
        ("surface_state", Some(value)) => surface_ids_valid(value),
        ("persistence_probe", Some(value)) => persistence_challenge_valid(value),
        ("register", Some(value)) => {
            value.starts_with('/')
                && value.ends_with(".app")
                && value.len() <= 4096
                && !value.contains('\0')
        }
        ("install", Some(value)) => {
            value.starts_with('/')
                && (value.ends_with(".app") || value.ends_with(".ipa"))
                && value.len() <= 4096
                && !value.contains('\0')
        }
        ("open" | "lookup", Some(value)) => {
            !value.is_empty()
                && value.len() <= 255
                && value
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
        }
        _ => false,
    };
    if !valid {
        return Err(Error::new(
            "invalid_guest_request",
            "Use inspect/status/setup/screen_state/display_state/foreground_state/graphics_state, surface_state with up to four distinct comma-separated decimal native IOSurface IDs, persistence_probe create:<64 lowercase hex digits>, read:<same challenge>, classes:<challenge> or inode:<1 to 19 decimal digits>, crash_reports <report name prefix: 1 to 255 ASCII letters, digits, '.', '_' or '-', not starting with '.' or '-'>, display_refresh, setup_finish/restart_ui, effects reduced/original, watchdog_scale original or a whole factor 2..30, icon_cache clear, time_zone Area/City (one to three '/'-separated components of 1 to 32 ASCII letters, digits, '_', '-' or '+') and auto_lock never/original for an activated virtual guest, lookup/open bundle.identifier, logs application/platform/control/operation/development/quiet/buffer_texture, install /path/App.app-or.ipa, register /path/App.app, receive_bundle, policy_add <40 hex digits>:<bundle identifier>, debug_server <PID or process name>, network_state, hardware_probe <IOKit class>, gestalt <key>, developer_mode status/enable, keyboard_layout, or cancel",
        ));
    }
    identifier(id, "job ID", Some("[0-9a-f]{32}"))?;
    // The guest runs framework operations in one owned child. Its parent can
    // answer diagnostics and cancellation while that child still owns its
    // reply stream; do not make them wait for the framework request deadline.
    // Application logs resolve a real container through LaunchServices and
    // therefore share the framework worker's reservation.
    let service_request = matches!(operation, "status" | "cancel" | "flush")
        || (operation == "logs" && argument != Some("application"));
    let lock_kind = if service_request {
        "service"
    } else {
        "request"
    };
    let lock = Lock::open(
        &home()?
            .join("locks")
            .join(format!("ios-guest-{lock_kind}-{id}.lock")),
    )?;
    if !lock.acquire(true)? {
        return Err(Error::new(
            "ios_guest_busy",
            if service_request {
                "Another native guest diagnostic or cancellation request is in progress"
            } else {
                "Another native guest framework request is in progress"
            },
        ));
    }
    let job = jobs::get(id)?;
    let plan = &job["plan"];
    if s(&plan["operation_key"], "operation") != "ios.emulator.paravirtual"
        || !plan["operation_key"]["arguments"]["guest_control_backend"].is_string()
        || Path::new(plan["steps"][0]["argv"][1].as_str().unwrap_or(""))
            .file_name()
            .is_none_or(|name| name != "run_ios_paravirt.py")
    {
        return Err(Error::new(
            "ios_guest_control_unavailable",
            "Select an iOS job with guest_control_backend enabled",
        ));
    }
    if b(&job, "cancelling") || jobs::TERMINAL.contains(&s(&job, "state")) {
        return Err(Error::new(
            "ios_guest_stopped",
            "The virtual iOS job has stopped",
        ));
    }
    let root = home()?.join("ios-emulator").canonicalize()?;
    let directory = PathBuf::from(s(plan, "cwd")).canonicalize()?;
    let output = directory.join("paravirtual");
    let request = output.join("request.json");
    if directory.parent() != Some(root.as_path()) || output.is_symlink() || request.is_symlink() {
        return Err(Error::new(
            "invalid_report_path",
            "Guest request is outside the owned run",
        ));
    }
    if let Some(result) = ended_emulator(&output)? {
        return Err(Error::new(
            "ios_guest_stopped",
            "The virtual iOS device has stopped; final checks are still running",
        )
        .details(json!({"job_id":id,"emulator_exit_observed":true,
                       "exit_code":result["exit_code"],
                       "storage_hashes_complete":result["storage_hashes_complete"]})));
    }
    if !request.is_file() {
        return Err(Error::new(
            "ios_guest_starting",
            "The guest control transport has not started",
        ));
    }
    if fs::metadata(&request)?.len() > 1024 * 1024 {
        return Err(Error::new(
            "invalid_report_path",
            "Guest launch receipt exceeds 1 MiB",
        ));
    }
    let request = read_json(&request, None)?;
    let control = &request["guest_control"];
    let socket = PathBuf::from(s(control, "socket"));
    let parent = socket
        .parent()
        .ok_or_else(|| Error::new("invalid_control_socket", "Missing private socket directory"))?;
    let metadata = fs::symlink_metadata(parent)?;
    let owner = unsafe { libc::geteuid() };
    if s(control, "protocol") != "tomacco-vsock-json-v1"
        || control["port"] != 1339
        || socket.file_name().is_none_or(|name| name != "guest.sock")
        || !metadata.is_dir()
        || metadata.uid() != owner
        || metadata.mode() & 0o077 != 0
    {
        return Err(Error::new(
            "invalid_control_socket",
            "Guest transport must use its owner's private directory",
        ));
    }
    let metadata = fs::symlink_metadata(&socket).map_err(|_| {
        Error::new(
            "ios_guest_starting",
            "The guest transport socket is not ready",
        )
    })?;
    if !metadata.file_type().is_socket() || metadata.uid() != owner {
        return Err(Error::new(
            "invalid_control_socket",
            "Guest transport is not an owned Unix socket",
        ));
    }
    let stream = UnixStream::connect(&socket).map_err(|error| {
        Error::new(
            "ios_guest_starting",
            format!("Guest transport connection failed: {error}"),
        )
    })?;
    // Short individual waits permit cancellation. The absolute deadline also
    // bounds a peer that drips bytes forever without completing its response.
    stream.set_read_timeout(Some(Duration::from_millis(200)))?;
    stream.set_write_timeout(Some(Duration::from_millis(200)))?;
    let seconds = a
        .get("timeout_seconds")
        .and_then(Value::as_u64)
        .unwrap_or(30)
        .clamp(1, 120);
    let deadline = shared_deadline.unwrap_or_else(|| Instant::now() + Duration::from_secs(seconds));
    let mut reader = BufReader::new(stream);
    send(reader.get_mut(), b"CONNECT 1339\n", deadline)?;
    let handshake = line(&mut reader, 128, deadline).map_err(|error| {
        if error.code == "ios_guest_disconnected" {
            Error::new(
                "ios_guest_starting",
                "The native guest service has not accepted the connection",
            )
        } else {
            error
        }
    })?;
    if handshake != "OK 1339\n" {
        return Err(Error::new(
            "ios_guest_starting",
            "The native guest service did not accept the connection",
        ));
    }
    let mut body = json!({"operation":operation});
    // Read-only framework calls can also outlive a host deadline while iOS
    // initializes LaunchServices. Give their worker a recoverable identity.
    // Parent diagnostics stay outside this lane and never replace receipts.
    let request_id = (!service_request).then(|| uuid::Uuid::new_v4().simple().to_string());
    if let Some(request_id) = &request_id {
        body["request_id"] = json!(request_id);
    }
    if let Some(argument) = argument {
        body["argument"] = json!(argument);
    }
    if let Some(bundle_id) = bundle_id {
        body["bundle_id"] = json!(bundle_id);
    }
    let mut body = serde_json::to_vec(&body)?;
    body.push(b'\n');
    let unknown_outcome = |error: Error| {
        error.details(json!({
        "operation":operation, "guest_response_received":false, "request_id":request_id,
        "request_may_have_executed":true, "automatic_retry_safe":matches!(operation, "inspect" | "lookup" | "logs" | "status" | "setup" | "screen_state" | "display_state" | "foreground_state" | "graphics_state" | "surface_state" | "crash_reports" | "time_zone" | "auto_lock" | "network_state" | "hardware_probe" | "gestalt" | "developer_mode" | "keyboard_layout") || (operation == "persistence_probe" && argument.is_some_and(|value| value.starts_with("read:") || value.starts_with("classes:") || value.starts_with("inode:"))),
        "recovery":request_id.as_ref().map(|request_id| json!({
            "operation":"ios.emulator.guest_result", "arguments":{"job_id":id, "request_id":request_id}
        }))
    }))
    };
    send(reader.get_mut(), &body, deadline).map_err(unknown_outcome)?;
    let response = line(&mut reader, 1024 * 1024 + 128, deadline).map_err(unknown_outcome)?;
    let data = response
        .strip_prefix("TOMACCO_APP_CONTROL ")
        .ok_or_else(|| {
            unknown_outcome(Error::new(
                "invalid_guest_response",
                "Unknown native guest protocol",
            ))
        })?;
    let response: Value = serde_json::from_str(data).map_err(|_| {
        unknown_outcome(Error::new(
            "invalid_guest_response",
            "Native guest returned invalid JSON",
        ))
    })?;
    if response["format"] != 1
        || response["operation"] != operation
        || !response["ok"].is_boolean()
        || (response.get("request_id").is_some() && response["request_id"] != json!(request_id))
    {
        return Err(unknown_outcome(Error::new(
            "invalid_guest_response",
            "Native guest response does not match the request",
        )));
    }
    let mut result = json!({"job_id":id, "guest_response_received":true,
        "request_id":request_id, "response":response});
    if operation == "crash_reports" {
        result["crash_report_identities"] = super::crash_reports::project(&result["response"]);
    }
    Ok(result)
}

/// Progress is evidence about a stage, never evidence of completion. Keep a
/// malformed optional observation separate from the authoritative receipt.
fn installation_progress(worker: &Value, request_id: &str) -> (Value, Value) {
    if worker["installation_progress_supported"] != true {
        return (Value::Null, Value::Null);
    }
    let invalid = || (Value::Null, json!({"code":"invalid_guest_progress"}));
    if !worker["progress_read_error"].is_null() {
        return (
            Value::Null,
            json!({"code":"ios_guest_progress_unavailable"}),
        );
    }
    let row = &worker["installation_progress"];
    if row.is_null() {
        return (Value::Null, Value::Null);
    }
    let identity = s(row, "request_id");
    if row["format"] != 1
        || row["operation"] != "install"
        || identity.len() != 32
        || !identity
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        || row["sequence"].as_u64().is_none_or(|n| n == 0)
        || !matches!(
            s(row, "phase"),
            "preparing" | "installer-connect" | "staging-copy" | "staging-publish" | "installer"
        )
    {
        return invalid();
    }
    let mut progress = json!({
        "request_id":identity, "sequence":row["sequence"], "phase":row["phase"]
    });
    if let Some(status) = row.get("native_status") {
        if status
            .as_str()
            .is_none_or(|text| text.encode_utf16().count() > 128)
        {
            return invalid();
        }
        progress["native_status"] = status.clone();
    }
    if let Some(percent) = row.get("percent_complete") {
        if percent
            .as_f64()
            .is_none_or(|value| !value.is_finite() || !(0.0..=100.0).contains(&value))
        {
            return invalid();
        }
        progress["percent_complete"] = percent.clone();
    }
    if let Some(error) = row.get("native_progress_error") {
        if !matches!(
            error.as_str(),
            Some("invalid_percentage" | "invalid_payload")
        ) {
            return invalid();
        }
        progress["native_progress_error"] = error.clone();
    }
    if identity != request_id {
        return (Value::Null, Value::Null);
    }
    (progress, Value::Null)
}

/// Observe an existing framework request after its original host deadline.
/// No framework operation is submitted and no mutation is ever replayed.
pub fn result(a: &Value) -> Result {
    let id = s(a, "job_id");
    let request_id = s(a, "request_id");
    identifier(id, "job ID", Some("[0-9a-f]{32}"))?;
    identifier(request_id, "request ID", Some("[0-9a-f]{32}"))?;
    let seconds = a
        .get("timeout_seconds")
        .and_then(Value::as_u64)
        .unwrap_or(30)
        .clamp(1, 120);
    let deadline = Instant::now() + Duration::from_secs(seconds);
    let status = call_with_deadline(&json!({"job_id":id, "operation":"status"}), Some(deadline))?;
    let worker = &status["response"];
    if worker["ok"] != true || !worker["worker_running"].is_boolean() {
        return Err(Error::new(
            "invalid_guest_status",
            "The guest did not report its current worker state",
        ));
    }
    // Read the receipt after status: completion between these two reads wins
    // over an earlier running snapshot. An overwritten receipt stays unknown.
    let logs = call_with_deadline(
        &json!({"job_id":id, "operation":"logs", "argument":"operation"}),
        Some(deadline),
    )?;
    let log = &logs["response"];
    let mut receipt = Value::Null;
    if log["ok"] == true {
        let invalid = || {
            Error::new(
                "invalid_guest_receipt",
                "The guest operation receipt is incomplete or malformed",
            )
        };
        if log["truncated"] != false {
            return Err(invalid());
        }
        let row: Value = if let Some(receipt) = log.get("receipt") {
            if !receipt.is_object() {
                return Err(invalid());
            }
            receipt.clone()
        } else {
            let text = log["text"].as_str().ok_or_else(invalid)?;
            serde_json::from_str(text).map_err(|_| invalid())?
        };
        if row["format"] != 1
            || !receipt_operation(s(&row, "operation"))
            || !row["ok"].is_boolean()
            || row["request_id"].as_str().is_none_or(|value| {
                value.len() != 32
                    || !value
                        .bytes()
                        .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
            })
        {
            return Err(invalid());
        }
        if row["request_id"] == request_id {
            receipt = row;
        }
    } else if log["error"] != "log_unavailable" {
        return Err(Error::new(
            "ios_guest_receipt_unavailable",
            "The guest could not read its operation receipt",
        )
        .details(json!({"job_id":id, "request_id":request_id, "response":log})));
    }
    let interrupted = receipt["request_may_have_executed"] == true
        || matches!(
            s(&receipt, "error"),
            "guest_worker_timeout" | "guest_worker_cancelled"
        );
    let state = if !receipt.is_null() && !interrupted {
        "completed"
    } else if !receipt.is_null() {
        // Cancellation stops only our worker, not necessarily installd's work.
        "unknown"
    } else if worker["worker_running"] == true && worker["worker_request_id"] == request_id {
        "running"
    } else {
        "unknown"
    };
    let (progress, progress_error) = installation_progress(worker, request_id);
    let mut result = json!({
        "job_id":id, "request_id":request_id, "state":state,
        "outcome_known":state == "completed", "mutation_retry_safe":false,
        "ready_for_next_request":state == "completed"
            && worker["ok"] == true && worker["worker_running"] == false,
        "response":receipt, "worker":worker,
        "progress_supported":worker["installation_progress_supported"] == true,
        "progress":progress, "progress_error":progress_error,
        "receipt_error":if log["ok"] == false { log.clone() } else { Value::Null }
    });
    if s(&result["response"], "operation") == "crash_reports" {
        result["crash_report_identities"] = super::crash_reports::project(&result["response"]);
    }
    Ok(result)
}
