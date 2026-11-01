//! Native Perfetto captures, attributed metrics and explicit regression budgets.
use crate::{core::*, devices, jobs, toolchain as tc};
use serde_json::{Value, json};
use std::fs;
fn quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}
pub fn handle(op: &str, a: &Value) -> Result {
    match op {
        "profile.capture" => capture(a),
        "profile.analyze" => analyze(a),
        "profile.compare" => compare(a),
        _ => crate::perf::handle(op, a),
    }
}
fn capture(a: &Value) -> Result {
    let device = s(a, "device");
    let app = s(a, "bundle_id");
    identifier(
        app,
        "application ID",
        Some(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+"),
    )?;
    let target = output_path(s(a, "path"))?;
    fs::create_dir_all(target.parent().unwrap())?;
    let config = format!(
        r#"buffers {{ size_kb: {} fill_policy: RING_BUFFER }}
    duration_ms: {}
    data_sources {{ config {{ name: "linux.ftrace" ftrace_config {{
      ftrace_events: "sched/sched_switch" ftrace_events: "sched/sched_waking"
      ftrace_events: "power/cpu_frequency" ftrace_events: "power/cpu_idle"
      atrace_categories: "gfx" atrace_categories: "view" atrace_categories: "wm"
      atrace_categories: "am" atrace_categories: "input" atrace_categories: "binder_driver"
      atrace_apps: "{}" }} }} }}
    data_sources {{ config {{ name: "linux.process_stats" process_stats_config {{ scan_all_processes_on_start: true proc_stats_poll_ms: 250 }} }} }}
    data_sources {{ config {{ name: "linux.sys_stats" sys_stats_config {{ meminfo_period_ms: 1000 stat_period_ms: 1000 stat_counters: STAT_CPU_TIMES }} }} }}
    data_sources {{ config {{ name: "android.surfaceflinger.frametimeline" }} }}
    data_sources {{ config {{ name: "android.packages_list" }} }}
"#,
        n(a, "buffer_mb") * 1024,
        n(a, "seconds") * 1000,
        app
    );
    let capture_id = uuid::Uuid::new_v4().simple().to_string();
    let remote = format!("/data/misc/perfetto-configs/tomacco-{capture_id}.pbtxt");
    let remote_trace = format!("/data/misc/perfetto-traces/tomacco-{capture_id}.pftrace");
    let cmd = devices::shell(
        device,
        &argv(&["perfetto", "--txt", "-c", &remote, "-o", &remote_trace]),
    )?;
    let config_path = target.with_extension(format!("{capture_id}.config.pbtxt"));
    atomic_write(&config_path, config.as_bytes())?;
    let mut push = devices::adb(device)?;
    push.extend([
        "push".into(),
        config_path.to_string_lossy().into_owned(),
        remote.clone(),
    ]);
    let readable = devices::shell(device, &argv(&["chmod", "644", &remote]))?;
    let mut download = devices::adb(device)?;
    download.extend(argv(&["exec-out", "cat", &remote_trace]));
    let cleanup = devices::shell(device, &argv(&["rm", "-f", &remote, &remote_trace]))?;
    jobs::submit(
        &format!("Perfetto · {app}"),
        json!({"steps":[{"argv":push},{"argv":readable},{"argv":cmd},{"argv":download,"output":target,"minimum_bytes":128,"magic_hex":"0a"}],"cleanup":[cleanup],"cwd":home()?,"env":tc::environment()?,"timeout":n(a,"seconds")+60,"exclusive":format!("perfetto:{device}"),"always_artifacts":[config_path]}),
    )
}
fn csv(text: &str) -> Result<Vec<Vec<String>>> {
    let mut rows = Vec::new();
    let mut row = Vec::new();
    let mut value = String::new();
    let mut chars = text.chars().peekable();
    let mut quoted = false;
    while let Some(ch) = chars.next() {
        match ch {
            '"' if quoted && chars.peek() == Some(&'"') => {
                chars.next();
                value.push('"');
            }
            '"' => quoted = !quoted,
            ',' if !quoted => {
                row.push(std::mem::take(&mut value));
            }
            '\n' if !quoted => {
                row.push(std::mem::take(&mut value));
                rows.push(std::mem::take(&mut row));
            }
            '\r' if !quoted => (),
            _ => value.push(ch),
        }
    }
    if quoted {
        return Err(Error::new(
            "trace_output",
            "Unterminated trace processor CSV value",
        ));
    }
    if !value.is_empty() || !row.is_empty() {
        row.push(value);
        rows.push(row);
    }
    Ok(rows)
}
fn analyze(a: &Value) -> Result {
    let trace = path(s(a, "path"), false, true)?;
    let app = s(a, "bundle_id");
    identifier(
        app,
        "application ID",
        Some(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+"),
    )?;
    let condition = format!(
        "(p.name={} OR p.name GLOB {})",
        quote(app),
        quote(&format!("{app}:*"))
    );
    // All sections share one parse of the trace. Missing measurements stay NULL.
    let sql = format!(
        r#"SELECT 'duration_ms' metric, (end_ts-start_ts)/1e6 value FROM trace_bounds
UNION ALL SELECT 'cpu_ms', SUM(s.dur)/1e6 FROM sched s JOIN thread t USING(utid) JOIN process p USING(upid) WHERE s.dur>0 AND {condition}
UNION ALL SELECT 'rss_peak_bytes', MAX(value) FROM counter c JOIN process_counter_track ct ON c.track_id=ct.id JOIN process p USING(upid) WHERE ct.name='mem.rss' AND {condition}
UNION ALL SELECT 'frame_count', COUNT(*) FROM actual_frame_timeline_slice f JOIN process p USING(upid) WHERE {condition}
UNION ALL SELECT 'frame_avg_ms', AVG(f.dur)/1e6 FROM actual_frame_timeline_slice f JOIN process p USING(upid) WHERE f.dur>0 AND {condition}
UNION ALL SELECT 'frame_max_ms', MAX(f.dur)/1e6 FROM actual_frame_timeline_slice f JOIN process p USING(upid) WHERE f.dur>0 AND {condition}
UNION ALL SELECT 'jank_frames', SUM(CASE WHEN f.jank_type NOT IN ('None','') THEN 1 ELSE 0 END) FROM actual_frame_timeline_slice f JOIN process p USING(upid) WHERE {condition};"#
    );
    let output = tc::run(
        &[
            tc::find("trace_processor")?,
            "query".into(),
            trace.to_string_lossy().into_owned(),
            sql,
        ],
        120,
        None,
        true,
    )?;
    let rows = csv(s(&output, "stdout"))?;
    let mut metrics = json!({});
    for row in rows.iter().skip(1) {
        if row.len() != 2 {
            continue;
        }
        metrics[&row[0]] = row[1]
            .parse::<f64>()
            .ok()
            .map(|v| json!(v))
            .unwrap_or(Value::Null);
    }
    let diagnostics_sql = format!(
        "SELECT p.name, t.name thread, SUM(s.dur)/1e6 cpu_ms FROM sched s JOIN thread t USING(utid) JOIN process p USING(upid) WHERE s.dur>0 AND {condition} GROUP BY t.utid ORDER BY cpu_ms DESC LIMIT 50;"
    );
    let threads = tc::run(
        &[
            tc::find("trace_processor")?,
            "query".into(),
            trace.to_string_lossy().into_owned(),
            diagnostics_sql,
        ],
        120,
        None,
        true,
    )?;
    let report = json!({"version":1,"bundle_id":app,"trace":artifact(&trace,0.0)?,"metrics":metrics,"threads_csv":s(&threads,"stdout"),"diagnostics":s(&output,"stderr"),"created":now()});
    if !s(a, "report").is_empty() {
        atomic_json(&output_path(s(a, "report"))?, &report)?;
    }
    Ok(report)
}
fn compare(a: &Value) -> Result {
    let baseline = read_json(&path(s(a, "baseline"), false, true)?, None)?;
    let current = read_json(&path(s(a, "current"), false, true)?, None)?;
    let value = compare_reports(&baseline, &current, &a["budgets"])?;
    if !s(a, "report").is_empty() {
        atomic_json(&output_path(s(a, "report"))?, &value)?;
    }
    Ok(value)
}
/// The change of every metric from a baseline report to a current one, and
/// the verdict of the budgets on it. Without budgets nothing can fail.
fn compare_reports(baseline: &Value, current: &Value, budgets: &Value) -> Result {
    if baseline["bundle_id"] != current["bundle_id"] {
        return Err(Error::new(
            "profile_mismatch",
            "Compare reports for the same application",
        ));
    }
    if baseline["kind"] != current["kind"] {
        return Err(Error::new(
            "profile_mismatch",
            "Compare reports of the same kind of measurement",
        ));
    }
    let empty = serde_json::Map::new();
    let budgets = match budgets {
        Value::Null => &empty,
        value => value
            .as_object()
            .filter(|budgets| !budgets.is_empty())
            .ok_or_else(|| {
                Error::new("invalid_argument", "Provide at least one regression budget")
            })?,
    };
    let increase = |old: f64, new: f64| {
        if old == 0.0 {
            if new == 0.0 { 0.0 } else { f64::INFINITY }
        } else {
            (new - old) / old.abs() * 100.0
        }
    };
    let finite = |value: f64| {
        if value.is_finite() {
            json!(value)
        } else {
            Value::Null
        }
    };
    let mut names: Vec<&String> = Vec::new();
    for report in [baseline, current] {
        for name in report["metrics"]
            .as_object()
            .into_iter()
            .flatten()
            .map(|m| m.0)
        {
            if !names.contains(&name) {
                names.push(name);
            }
        }
    }
    let changes: Vec<Value> = names
        .iter()
        .map(|metric| {
            let old = baseline["metrics"][metric.as_str()].as_f64();
            let new = current["metrics"][metric.as_str()].as_f64();
            match (old, new) {
                (Some(old), Some(new)) => json!({"metric":metric,"baseline":old,"current":new,
                    "delta":new-old,"delta_percent":finite(increase(old,new))}),
                _ => json!({"metric":metric,"baseline":old,"current":new,"delta":null,"delta_percent":null}),
            }
        })
        .collect();
    let mut results = Vec::new();
    let mut passed = true;
    for (metric, budget) in budgets {
        let old = baseline["metrics"][metric].as_f64();
        let new = current["metrics"][metric].as_f64();
        let result = match (old, new) {
            (Some(old), Some(new)) => {
                let max_absolute = budget["maximum"].as_f64().unwrap_or(f64::INFINITY);
                let max_regression = budget["increase_percent"].as_f64().unwrap_or(f64::INFINITY);
                let increase = increase(old, new);
                let ok = new <= max_absolute && increase <= max_regression;
                passed &= ok;
                json!({"metric":metric,"baseline":old,"current":new,"increase_percent":finite(increase),"passed":ok})
            }
            _ => {
                passed = false;
                json!({"metric":metric,"passed":false,"reason":"measurement_unavailable"})
            }
        };
        results.push(result);
    }
    Ok(
        json!({"passed":passed,"bundle_id":current["bundle_id"],"kind":current["kind"],"results":results,"changes":changes}),
    )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn csv_handles_quotes_nulls_and_newlines() {
        assert_eq!(
            csv("\"metric\",\"value\"\n\"a,\"\"b\",3\n").unwrap()[1],
            vec!["a,\"b", "3"]
        );
        assert!(csv("\"unfinished").is_err());
    }
    #[test]
    fn a_comparison_lists_every_change_and_gates_only_on_budgets() {
        let baseline = json!({"bundle_id":"dev.example","kind":"startup","metrics":{"startup_cold_median_ms":400.0,"startup_cold_p90_ms":500.0,"frames_total":0.0,"gone":1.0}});
        let current = json!({"bundle_id":"dev.example","kind":"startup","metrics":{"startup_cold_median_ms":460.0,"startup_cold_p90_ms":450.0,"frames_total":3.0,"new":2.0}});
        let value = compare_reports(&baseline, &current, &Value::Null).unwrap();
        assert_eq!(value["passed"], true);
        assert_eq!(value["results"], json!([]));
        let changes = value["changes"].as_array().unwrap();
        let names: Vec<&str> = changes.iter().map(|c| s(c, "metric")).collect();
        assert_eq!(
            names,
            [
                "startup_cold_median_ms",
                "startup_cold_p90_ms",
                "frames_total",
                "gone",
                "new"
            ]
        );
        assert_eq!(
            (
                changes[0]["delta"].clone(),
                changes[0]["delta_percent"].clone()
            ),
            (json!(60.0), json!(15.0))
        );
        assert_eq!(changes[1]["delta_percent"], -10.0);
        assert!(
            changes[2]["delta_percent"].is_null(),
            "an increase from zero has no percentage"
        );
        assert!(changes[3]["current"].is_null() && changes[3]["delta"].is_null());
        let budgets = json!({"startup_cold_median_ms":{"increase_percent":10},"startup_cold_p90_ms":{"maximum":480}});
        let value = compare_reports(&baseline, &current, &budgets).unwrap();
        assert_eq!(value["passed"], false);
        assert_eq!(value["results"][0]["passed"], false);
        assert_eq!(value["results"][1]["passed"], true);
        assert_eq!(value["changes"].as_array().unwrap().len(), 5);
        let other = json!({"bundle_id":"dev.example","kind":"memory","metrics":{}});
        assert_eq!(
            compare_reports(&baseline, &other, &Value::Null)
                .unwrap_err()
                .code,
            "profile_mismatch"
        );
        assert_eq!(
            compare_reports(&baseline, &current, &json!({}))
                .unwrap_err()
                .code,
            "invalid_argument"
        );
    }
}
