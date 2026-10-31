//! Bounded identities parsed from retained native report bytes. A parsed PID
//! describes those bytes; it does not bind a live process or its lifetime.
use super::guest_control::crash_report_filter_valid;
use super::*;
use base64::Engine as _;

const REPORT_LIMIT: usize = 6;
const CONTENT_LIMIT: usize = 256 * 1024;
const TOTAL_LIMIT: usize = 512 * 1024;

fn text_field(value: &Value, key: &str, limit: usize) -> Value {
    value[key]
        .as_str()
        .filter(|text| !text.is_empty() && text.len() <= limit && !text.contains('\0'))
        .map_or(Value::Null, |text| json!(text))
}

/// The identity in one report's bytes, read for the request's `filter` (a
/// report name prefix). The process is the report's own: the IPS header and
/// body must agree on it, the report file must be named after it
/// (ReportCrash names a report `<process>-<date>.ips`) and it must fit the
/// filter, so the process starts with the filter or the filter starts with
/// the process and '-' (a filter that reaches into the date).
fn identity(report: &Value, filter: &str, remaining: &mut usize) -> Value {
    let mut result = json!({"state":"unavailable", "pid":null,
        "process_lifetime_bound":false, "source":"serialized-native-report-bytes",
        "native_file_consistency_verified":report["file_consistency_verified"] == true});
    if report["content_complete"] != true {
        result["state"] = json!("incomplete-content");
        return result;
    }
    let Some(size) = report["content_bytes"].as_u64() else {
        result["state"] = json!("invalid-content-size");
        return result;
    };
    if size > CONTENT_LIMIT as u64 || size > *remaining as u64 {
        result["state"] = json!("content-limit");
        return result;
    }
    let Some(encoded) = report["content_base64"].as_str() else {
        result["state"] = json!("missing-content");
        return result;
    };
    // Check the encoded bound before allocating a decoded buffer. Charge every
    // attempted complete report, including a malformed one, against the total.
    *remaining -= size as usize;
    if encoded.len() != (size as usize).div_ceil(3) * 4 {
        result["state"] = json!("encoded-size-mismatch");
        return result;
    }
    let Ok(bytes) = STANDARD.decode(encoded) else {
        result["state"] = json!("invalid-base64");
        return result;
    };
    if bytes.len() != size as usize || report["observed_file_bytes"].as_u64() != Some(size) {
        result["state"] = json!("content-size-mismatch");
        return result;
    }
    let Some(newline) = bytes.iter().position(|byte| *byte == b'\n') else {
        result["state"] = json!("unsupported-report-format");
        return result;
    };
    if newline > 4096 {
        result["state"] = json!("header-limit");
        return result;
    }
    let (Ok(header), Ok(body)) = (
        serde_json::from_slice::<Value>(&bytes[..newline]),
        serde_json::from_slice::<Value>(&bytes[newline + 1..]),
    ) else {
        result["state"] = json!("unsupported-report-format");
        return result;
    };
    let pid = body["pid"]
        .as_u64()
        .filter(|pid| *pid > 0 && *pid <= i32::MAX as u64);
    if !header.is_object() || !body.is_object() || pid.is_none() {
        result["state"] = json!("invalid-identity");
        return result;
    }
    let Some(process) = body["procName"].as_str().filter(|process| {
        let own = format!("{process}-");
        crash_report_filter_valid(process)
            && header["app_name"].as_str() == Some(process)
            && report["name"]
                .as_str()
                .is_some_and(|name| name.starts_with(&own))
            && (process.starts_with(filter) || filter.starts_with(&own))
    }) else {
        result["state"] = json!("process-mismatch");
        return result;
    };
    result["state"] = json!("parsed");
    result["pid"] = json!(pid);
    result["process"] = json!(process);
    result["launch_time"] = text_field(&body, "procLaunch", 128);
    result["capture_time"] = text_field(&body, "captureTime", 128);
    result["incident_id"] = text_field(&header, "incident_id", 128);
    result
}

pub(super) fn project(response: &Value) -> Value {
    let mut result = json!({"state":"unavailable", "identities":[],
        "process_lifetime_bound":false, "absence_proves_no_crash":false});
    if response["ok"] != true {
        result["state"] = json!("native-query-failed");
        return result;
    }
    let Some(reports) = response["reports"].as_array() else {
        return result;
    };
    // The request's filter, as the guest echoes it: any report name prefix
    // (the request validation's rule), never a path.
    let Some(filter) = response["process"]
        .as_str()
        .filter(|name| crash_report_filter_valid(name))
    else {
        result["state"] = json!("invalid-process");
        return result;
    };
    if reports.len() > REPORT_LIMIT {
        result["state"] = json!("report-limit");
        return result;
    }
    let mut remaining = TOTAL_LIMIT;
    let identities: Vec<Value> = reports
        .iter()
        .enumerate()
        .map(|(index, report)| {
            let mut value = identity(report, filter, &mut remaining);
            value["report_index"] = json!(index);
            value
        })
        .collect();
    result["state"] = json!("projected");
    result["identities"] = json!(identities);
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    fn report(body: Value) -> Value {
        let bytes = format!(
            "{}\n{}",
            json!({"app_name":"Preferences","incident_id":"fixture"}),
            body
        );
        json!({"name":"Preferences-2026-10-13-071227.ips",
            "content_complete":true,"content_bytes":bytes.len(),
            "observed_file_bytes":bytes.len(),"content_base64":STANDARD.encode(bytes),
            "file_consistency_verified":false})
    }
    fn response(report: Value) -> Value {
        json!({"ok":true,"process":"Preferences","reports":[report]})
    }
    #[test]
    fn historical_pid_and_times_are_preserved_without_lifetime_claims() {
        let row = project(&response(report(
            json!({"pid":551,"procName":"Preferences",
            "procLaunch":"old launch","captureTime":"old capture"}),
        )));
        let identity = &row["identities"][0];
        assert_eq!(identity["state"], "parsed");
        assert_eq!(identity["pid"], 551);
        assert_eq!(identity["capture_time"], "old capture");
        assert_eq!(identity["process_lifetime_bound"], false);
        assert_eq!(identity["native_file_consistency_verified"], false);
        assert_eq!(row["absence_proves_no_crash"], false);
    }
    #[test]
    fn invalid_pid_or_process_never_produces_an_identity() {
        for pid in [
            json!(0),
            json!(-1),
            json!(true),
            json!(1.5),
            json!("551"),
            json!(2147483648u64),
        ] {
            let row = project(&response(report(
                json!({"pid":pid,"procName":"Preferences"}),
            )));
            assert_eq!(row["identities"][0]["state"], "invalid-identity");
        }
        let row = project(&response(report(
            json!({"pid":551,"procName":"OtherProcess"}),
        )));
        assert_eq!(row["identities"][0]["state"], "process-mismatch");
    }
    #[test]
    fn truncated_invalid_and_oversized_content_stays_unknown() {
        for (key, value, state) in [
            ("content_complete", json!(false), "incomplete-content"),
            ("content_bytes", json!(CONTENT_LIMIT + 1), "content-limit"),
            ("observed_file_bytes", json!(1), "content-size-mismatch"),
        ] {
            let mut item = report(json!({"pid":551,"procName":"Preferences"}));
            item[key] = value;
            assert_eq!(project(&response(item))["identities"][0]["state"], state);
        }
        let mut item = report(json!({"pid":551,"procName":"Preferences"}));
        item["content_base64"] = json!("!".repeat(item["content_base64"].as_str().unwrap().len()));
        assert_eq!(
            project(&response(item))["identities"][0]["state"],
            "invalid-base64"
        );
        let mut item = report(json!({"pid":551,"procName":"Preferences"}));
        item["content_bytes"] = json!(0);
        assert_eq!(
            project(&response(item))["identities"][0]["state"],
            "encoded-size-mismatch"
        );
    }
    #[test]
    fn chronod_and_the_maps_snapshot_service_are_read_like_the_others() {
        // Both are open guest-stability items: chronod aborts at boot and
        // the snapshot service when Maps opens.
        for process in ["chronod", "com.apple.MapKit.SnapshotService"] {
            let bytes = format!(
                "{}\n{}",
                json!({"app_name":process,"incident_id":"fixture"}),
                json!({"pid":88,"procName":process})
            );
            let report = json!({"name":format!("{process}-2026-10-28-000000.ips"),
                "content_complete":true,"content_bytes":bytes.len(),
                "observed_file_bytes":bytes.len(),"content_base64":STANDARD.encode(&bytes),
                "file_consistency_verified":false});
            let row = project(&json!({"ok":true,"process":process,"reports":[report]}));
            assert_eq!(row["identities"][0]["state"], "parsed", "{process}");
        }
        // Any report name prefix is a filter (guest tools from 368); only a
        // string that is no report name is refused.
        let row = project(&json!({"ok":true,"process":"launchd","reports":[]}));
        assert_eq!(row["state"], "projected");
        for filter in ["", ".launchd", "-x", "a/b", "launchd d", "ä"] {
            let row = project(&json!({"ok":true,"process":filter,"reports":[]}));
            assert_eq!(row["state"], "invalid-process", "{filter:?}");
        }
        let row = project(&json!({"ok":true,"process":"a".repeat(256),"reports":[]}));
        assert_eq!(row["state"], "invalid-process");
    }
    #[test]
    fn a_developers_app_and_a_prefix_project_the_reports_own_process() {
        // The filter is a report name prefix, so the identity's process is
        // the one the report's bytes and file name carry, not the filter.
        fn item(app_name: &str, proc_name: &str, name: &str) -> Value {
            let bytes = format!(
                "{}\n{}",
                json!({"app_name":app_name,"incident_id":"fixture"}),
                json!({"pid":353,"procName":proc_name})
            );
            json!({"name":name,"content_complete":true,"content_bytes":bytes.len(),
                "observed_file_bytes":bytes.len(),"content_base64":STANDARD.encode(&bytes),
                "file_consistency_verified":false})
        }
        let probe = "TomaccoProbe-2026-10-28-120000.ips";
        for filter in [
            "TomaccoProbe",
            "Tomacco",
            "T",
            "TomaccoProbe-",
            "TomaccoProbe-2026-10",
            "TomaccoProbe-2026-10-28-120000.ips",
        ] {
            let row = project(&json!({"ok":true,"process":filter,
                "reports":[item("TomaccoProbe", "TomaccoProbe", probe)]}));
            assert_eq!(row["state"], "projected", "{filter:?}");
            let identity = &row["identities"][0];
            assert_eq!(identity["state"], "parsed", "{filter:?}");
            assert_eq!(identity["process"], "TomaccoProbe", "{filter:?}");
            assert_eq!(identity["pid"], 353, "{filter:?}");
        }
        for (filter, app_name, proc_name, name) in [
            // Another process's report under a short filter.
            (
                "Tomacco",
                "Preferences",
                "Preferences",
                "Preferences-2026-10-13-071227.ips",
            ),
            // A file named after another process than its bytes say.
            ("P", "Preferences", "Preferences", probe),
            // Header and body disagree.
            ("TomaccoProbe", "Preferences", "TomaccoProbe", probe),
            // A filter longer than the process name that reaches no date.
            ("TomaccoProbeX", "TomaccoProbe", "TomaccoProbe", probe),
            // A process name that is no report name.
            ("Tomacco", "Tomacco Probe", "Tomacco Probe", probe),
        ] {
            let row = project(&json!({"ok":true,"process":filter,
                "reports":[item(app_name, proc_name, name)]}));
            let identity = &row["identities"][0];
            assert_eq!(identity["state"], "process-mismatch", "{filter:?} {name}");
            assert_eq!(identity["pid"], Value::Null, "{filter:?} {name}");
            assert_eq!(identity["process"], Value::Null, "{filter:?} {name}");
        }
        // A report without its file name supplies no identity either.
        let mut unnamed = item("TomaccoProbe", "TomaccoProbe", probe);
        unnamed["name"] = Value::Null;
        let row = project(&json!({"ok":true,"process":"TomaccoProbe","reports":[unnamed]}));
        assert_eq!(row["identities"][0]["state"], "process-mismatch");
    }
    #[test]
    fn too_many_reports_and_missing_native_query_are_explicit() {
        let mut row = response(report(json!({"pid":551,"procName":"Preferences"})));
        row["reports"] = json!(vec![row["reports"][0].clone(); REPORT_LIMIT + 1]);
        assert_eq!(project(&row)["state"], "report-limit");
        row["ok"] = json!(false);
        assert_eq!(project(&row)["state"], "native-query-failed");
    }

    #[test]
    fn unsupported_formats_and_trailing_bytes_cannot_supply_an_identity() {
        for bytes in [
            b"Process: Preferences [551]".to_vec(),
            b"{}\n{\"pid\":551,\"procName\":\"Preferences\"}garbage".to_vec(),
            [vec![b' '; 4097], b"\n{}".to_vec()].concat(),
        ] {
            let item = json!({"content_complete":true,"content_bytes":bytes.len(),
                "observed_file_bytes":bytes.len(),"content_base64":STANDARD.encode(&bytes)});
            let identity = &project(&response(item))["identities"][0];
            assert_ne!(identity["state"], "parsed");
            assert_eq!(identity["pid"], Value::Null);
        }
    }

    #[test]
    fn total_content_budget_also_counts_malformed_reports() {
        let bytes = vec![b' '; CONTENT_LIMIT];
        let item = json!({"content_complete":true,"content_bytes":bytes.len(),
            "observed_file_bytes":bytes.len(),"content_base64":STANDARD.encode(bytes)});
        let row = project(&json!({"ok":true,"process":"Preferences",
            "reports":[item.clone(),item.clone(),item]}));
        assert_eq!(row["identities"][0]["state"], "unsupported-report-format");
        assert_eq!(row["identities"][1]["state"], "unsupported-report-format");
        assert_eq!(row["identities"][2]["state"], "content-limit");
        assert_eq!(row["identities"][2]["pid"], Value::Null);
    }
}
