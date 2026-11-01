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
    if baseline["bundle_id"] != current["bundle_id"] {
        return Err(Error::new(
            "profile_mismatch",
            "Compare reports for the same application",
        ));
    }
    let budgets = a["budgets"]
        .as_object()
        .ok_or_else(|| Error::new("invalid_argument", "Provide metric budgets"))?;
    if budgets.is_empty() {
        return Err(Error::new(
            "invalid_argument",
            "Provide at least one regression budget",
        ));
    }
    let mut results = Vec::new();
    let mut passed = true;
    for (metric, budget) in budgets {
        let old = baseline["metrics"][metric].as_f64();
        let new = current["metrics"][metric].as_f64();
        let result = match (old, new) {
            (Some(old), Some(new)) => {
                let max_absolute = budget["maximum"].as_f64().unwrap_or(f64::INFINITY);
                let max_regression = budget["increase_percent"].as_f64().unwrap_or(f64::INFINITY);
                let increase = if old == 0.0 {
                    if new == 0.0 { 0.0 } else { f64::INFINITY }
                } else {
                    (new - old) / old.abs() * 100.0
                };
                let ok = new <= max_absolute && increase <= max_regression;
                passed &= ok;
                json!({"metric":metric,"baseline":old,"current":new,"increase_percent":if increase.is_finite(){json!(increase)}else{Value::Null},"passed":ok})
            }
            _ => {
                passed = false;
                json!({"metric":metric,"passed":false,"reason":"measurement_unavailable"})
            }
        };
        results.push(result);
    }
    let value = json!({"passed":passed,"bundle_id":current["bundle_id"],"results":results});
    if !s(a, "report").is_empty() {
        atomic_json(&output_path(s(a, "report"))?, &value)?;
    }
    Ok(value)
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
}
