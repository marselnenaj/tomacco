//! Android app performance read from `am` and `dumpsys`: startup time, with
//! each measurement as a report whose flat `metrics` profile.compare reads.
use crate::{core::*, devices, toolchain as tc};
use serde_json::{Map, Value, json};
use std::time::{Duration, Instant};

fn bundle(id: &str) -> Result<&str> {
    identifier(
        id,
        "application ID",
        Some(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+"),
    )
}

/// The standard output of a shell command on the device.
fn shell_out(device: &str, args: &[&str], timeout: u64) -> Result<String> {
    let result = tc::run(&devices::shell(device, &argv(args))?, timeout, None, true)?;
    Ok(s(&result, "stdout").to_string())
}

/// Waits, ending early when the call is cancelled.
fn pause(duration: Duration) -> Result<()> {
    let end = Instant::now() + duration;
    while Instant::now() < end {
        check_cancelled()?;
        std::thread::sleep(Duration::from_millis(50).min(end - Instant::now()));
    }
    Ok(())
}

/// A value rounded to three decimals, so reports stay readable.
fn round(value: f64) -> f64 {
    (value * 1000.0).round() / 1000.0
}

/// The nearest-rank percentile of sorted values.
fn percentile(sorted: &[f64], p: f64) -> f64 {
    let rank = ((p / 100.0) * sorted.len() as f64).ceil().max(1.0) as usize;
    sorted[rank.min(sorted.len()) - 1]
}

/// Count, minimum, maximum, mean, median and 90th percentile of values.
pub fn summary(values: &[f64]) -> Value {
    if values.is_empty() {
        return json!({"count":0});
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(f64::total_cmp);
    let count = sorted.len();
    let median = if count % 2 == 1 {
        sorted[count / 2]
    } else {
        (sorted[count / 2 - 1] + sorted[count / 2]) / 2.0
    };
    json!({"count":count,"min":sorted[0],"max":sorted[count-1],
           "mean":round(sorted.iter().sum::<f64>()/count as f64),"median":median,"p90":percentile(&sorted,90.0)})
}

/// Writes a report to `report` when given, and returns it.
fn finish(a: &Value, report: Value) -> Result {
    if !s(a, "report").is_empty() {
        atomic_json(&output_path(s(a, "report"))?, &report)?;
    }
    Ok(report)
}

fn report(kind: &str, device: &str, id: &str, metrics: Map<String, Value>) -> Value {
    json!({"version":1,"kind":kind,"bundle_id":id,"device":device,"created":now(),"metrics":metrics})
}

pub fn handle(op: &str, a: &Value) -> Result {
    match op {
        "profile.startup" => startup(a),
        _ => Err(Error::new("operation_not_found", op)),
    }
}

/// One `am start -W` answer.
#[derive(Debug, PartialEq)]
pub struct Launch {
    pub state: String,
    pub activity: String,
    pub total_ms: f64,
    pub wait_ms: Option<f64>,
}

/// Reads `am start -W` output: Status, LaunchState (Android 10 and newer),
/// Activity, TotalTime and WaitTime.
pub fn parse_launch(output: &str) -> Result<Launch> {
    let field = |name: &str| {
        output.lines().find_map(|line| {
            line.trim()
                .strip_prefix(name)
                .and_then(|rest| rest.strip_prefix(':'))
                .map(|value| value.trim().to_string())
        })
    };
    if let Some(error) = output
        .lines()
        .find(|line| line.trim_start().starts_with("Error"))
    {
        return Err(Error::new("launch_failed", error.trim()).details(json!({"output":output})));
    }
    match field("Status").as_deref() {
        Some("ok") | None => (),
        Some(status) => {
            return Err(Error::new(
                "launch_failed",
                format!("am start answered Status: {status}"),
            )
            .details(json!({"output":output})));
        }
    }
    let total_ms = field("TotalTime")
        .and_then(|v| v.parse::<f64>().ok())
        .ok_or_else(|| {
            Error::new(
                "launch_unmeasured",
                "am start -W reported no TotalTime for this launch",
            )
            .details(json!({"output":output}))
        })?;
    Ok(Launch {
        state: field("LaunchState").unwrap_or_default(),
        activity: field("Activity").unwrap_or_default(),
        total_ms,
        wait_ms: field("WaitTime").and_then(|v| v.parse::<f64>().ok()),
    })
}

/// The component to start: `activity` as `package/class`, `.Class` or a
/// class name in the app, or the app's launcher activity.
fn component(device: &str, id: &str, activity: &str) -> Result<String> {
    if activity.is_empty() {
        return devices::launcher_component(device, id);
    }
    let class = r"[A-Za-z0-9_.$]+";
    if activity.contains('/') {
        identifier(activity, "activity", Some(&format!("{class}/{class}")))?;
        return Ok(activity.to_string());
    }
    identifier(activity, "activity", Some(class))?;
    Ok(format!("{id}/{activity}"))
}

/// Metric names of a startup report: startup_<mode>_<statistic>_ms.
fn startup_metrics(mode: &str, total: &Value) -> Map<String, Value> {
    let mut metrics = Map::new();
    for statistic in ["median", "p90", "min", "max", "mean"] {
        metrics.insert(
            format!("startup_{mode}_{statistic}_ms"),
            total[statistic].clone(),
        );
    }
    metrics
}

/// Launches the app `runs` times in a cold, warm or hot state and reports
/// `am start -W` TotalTime and WaitTime. Cold stops the app first; warm
/// keeps the process and recreates the activity (`--activity-clear-task`);
/// hot brings the existing activity back from the home screen.
pub fn startup(a: &Value) -> Result {
    let device = identifier(s(a, "device"), "device", None)?;
    let id = bundle(s(a, "bundle_id"))?;
    let mode = match s(a, "mode") {
        "" => "cold",
        mode => mode,
    };
    let runs = a["runs"].as_u64().unwrap_or(5).clamp(1, 30);
    let component = component(device, id, s(a, "activity"))?;
    let start = |extra: &[&str]| -> Result<Launch> {
        let mut args = vec!["am", "start", "-W"];
        args.extend(extra);
        args.extend(["-n", component.as_str()]);
        parse_launch(&shell_out(device, &args, 60)?)
    };
    if mode != "cold" {
        // A warm or hot launch needs the process, and hot the activity.
        start(&[])?;
        pause(Duration::from_millis(1000))?;
    }
    let mut launches = Vec::new();
    for _ in 0..runs {
        check_cancelled()?;
        let launch = match mode {
            "cold" => {
                shell_out(device, &["am", "force-stop", id], 30)?;
                start(&[])?
            }
            "warm" => start(&["--activity-clear-task"])?,
            _ => {
                shell_out(device, &["input", "keyevent", "KEYCODE_HOME"], 30)?;
                pause(Duration::from_millis(1000))?;
                start(&[])?
            }
        };
        launches.push(launch);
        pause(Duration::from_millis(500))?;
    }
    startup_report(a, device, id, &component, mode, &launches)
}

fn startup_report(
    a: &Value,
    device: &str,
    id: &str,
    component: &str,
    mode: &str,
    launches: &[Launch],
) -> Result {
    let totals: Vec<f64> = launches.iter().map(|l| l.total_ms).collect();
    let waits: Vec<f64> = launches.iter().filter_map(|l| l.wait_ms).collect();
    let total = summary(&totals);
    // Older Android names no launch state; only a named, different one counts.
    let mismatched = launches
        .iter()
        .filter(|l| !l.state.is_empty() && !l.state.eq_ignore_ascii_case(mode))
        .count();
    let mut value = report("startup", device, id, startup_metrics(mode, &total));
    value["mode"] = json!(mode);
    value["component"] = json!(component);
    value["total_ms"] = total;
    value["wait_ms"] = summary(&waits);
    value["mismatched_runs"] = json!(mismatched);
    value["runs"] = json!(
        launches
            .iter()
            .enumerate()
            .map(|(index, l)| json!({"run":index+1,"launch_state":l.state,"activity":l.activity,"total_ms":l.total_ms,"wait_ms":l.wait_ms}))
            .collect::<Vec<_>>()
    );
    finish(a, value)
}

#[cfg(test)]
mod tests {
    use super::*;

    // Recorded from an API 36 x86_64 emulator, Settings app.
    const COLD: &str = "Starting: Intent { cmp=com.android.settings/.Settings }\nStatus: ok\nLaunchState: COLD\nActivity: com.android.settings/.homepage.SettingsHomepageActivity\nTotalTime: 779\nWaitTime: 780\nComplete\n";
    const HOT: &str = "Starting: Intent { cmp=com.android.settings/.Settings }\nWarning: Activity not started, its current task has been brought to the front\nStatus: ok\nLaunchState: HOT\nActivity: com.android.settings/.homepage.SettingsHomepageActivity\nTotalTime: 206\nWaitTime: 215\nComplete\n";

    #[test]
    fn launch_output_gives_state_and_times() {
        let cold = parse_launch(COLD).unwrap();
        assert_eq!(
            cold,
            Launch {
                state: "COLD".into(),
                activity: "com.android.settings/.homepage.SettingsHomepageActivity".into(),
                total_ms: 779.0,
                wait_ms: Some(780.0)
            }
        );
        assert_eq!(parse_launch(HOT).unwrap().state, "HOT");
        let missing = "Starting: Intent { cmp=dev.example/.Main }\nError type 3\nError: Activity class {dev.example/dev.example.Main} does not exist.\n";
        assert_eq!(parse_launch(missing).unwrap_err().code, "launch_failed");
        let timeout = "Status: timeout\nLaunchState: UNKNOWN (0)\nActivity: dev.example/.Main\nWaitTime: 10010\nComplete\n";
        assert_eq!(parse_launch(timeout).unwrap_err().code, "launch_failed");
        assert_eq!(
            parse_launch("Status: ok\nComplete\n").unwrap_err().code,
            "launch_unmeasured"
        );
    }

    #[test]
    fn summary_gives_median_and_nearest_rank_p90() {
        let value = summary(&[300.0, 100.0, 200.0, 400.0]);
        assert_eq!(value["median"], 250.0);
        assert_eq!(value["p90"], 400.0);
        assert_eq!(
            (value["min"].clone(), value["max"].clone()),
            (json!(100.0), json!(400.0))
        );
        assert_eq!(value["mean"], 250.0);
        let ten: Vec<f64> = (1..=10).map(f64::from).collect();
        assert_eq!(summary(&ten)["p90"], 9.0);
        assert_eq!(summary(&[5.0])["p90"], 5.0);
        assert_eq!(summary(&[])["count"], 0);
    }

    #[test]
    fn startup_report_names_metrics_by_mode_and_counts_other_states() {
        let launches = vec![
            parse_launch(COLD).unwrap(),
            parse_launch(&COLD.replace("779", "721")).unwrap(),
            parse_launch(HOT).unwrap(),
        ];
        let value = startup_report(
            &json!({}),
            "emulator-5554",
            "com.android.settings",
            "com.android.settings/.Settings",
            "cold",
            &launches,
        )
        .unwrap();
        assert_eq!(value["metrics"]["startup_cold_median_ms"], 721.0);
        assert_eq!(value["metrics"]["startup_cold_p90_ms"], 779.0);
        assert_eq!(value["metrics"]["startup_cold_min_ms"], 206.0);
        assert_eq!(value["mismatched_runs"], 1);
        assert_eq!(value["runs"][2]["launch_state"], "HOT");
        assert_eq!(value["wait_ms"]["count"], 3);
    }

    #[test]
    fn an_explicit_activity_is_qualified_with_the_package() {
        assert_eq!(
            component("emulator-5554", "dev.example", ".Main").unwrap(),
            "dev.example/.Main"
        );
        assert_eq!(
            component("emulator-5554", "dev.example", "dev.other/.Main").unwrap(),
            "dev.other/.Main"
        );
        assert!(component("emulator-5554", "dev.example", "a;rm -rf /").is_err());
    }
}
