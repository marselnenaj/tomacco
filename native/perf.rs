//! Android app performance read from `am`, `dumpsys` and `/proc`: startup
//! time, frame rendering, memory, and CPU and battery use, each measurement a
//! report whose flat `metrics` profile.compare reads, and budgets over them.
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
        "profile.frames" => frames(a),
        "profile.memory" => memory(a),
        "profile.usage" => usage(a),
        "profile.budget" => budget(a),
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

/// The number a dumpsys value starts with, as in `12ms` or `15 (28.85%)`.
fn leading(value: &str) -> Option<f64> {
    let end = value
        .find(|c: char| !(c.is_ascii_digit() || c == '.'))
        .unwrap_or(value.len());
    value[..end].parse().ok()
}

/// `Slow UI thread` as `slow_ui_thread`, `ViewRootImpl` as `view_root_impl`.
fn snake(words: &str) -> String {
    let mut spaced = String::new();
    let mut previous = ' ';
    for c in words.chars() {
        if c.is_ascii_uppercase() && previous.is_ascii_lowercase() {
            spaced.push(' ');
        }
        spaced.push(c);
        previous = c;
    }
    spaced
        .split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|w| !w.is_empty())
        .map(str::to_ascii_lowercase)
        .collect::<Vec<_>>()
        .join("_")
}

/// The process sections of `dumpsys gfxinfo PACKAGE`: frames rendered, janky
/// frames, the frame time percentiles, the `Number ...` counters and the
/// frame time histogram as [milliseconds, frames] pairs.
pub fn parse_gfxinfo(text: &str) -> Vec<Value> {
    let header = regex::Regex::new(r"^\*\* Graphics info for pid (\d+) \[(.*)\] \*\*").unwrap();
    let mut processes: Vec<Value> = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        if let Some(c) = header.captures(line) {
            processes.push(json!({"pid":c[1].parse::<u64>().unwrap_or(0),"process":&c[2],"frames_total":null,
                                  "janky_frames":null,"percentiles_ms":{},"counters":{},"histogram":[]}));
            continue;
        }
        let (Some(process), Some((key, value))) = (processes.last_mut(), line.split_once(": "))
        else {
            continue;
        };
        match key {
            "Total frames rendered" => process["frames_total"] = json!(leading(value)),
            "Janky frames" => process["janky_frames"] = json!(leading(value)),
            "HISTOGRAM" => {
                process["histogram"] = json!(
                    value
                        .split_whitespace()
                        .filter_map(|bucket| {
                            let (ms, count) = bucket.split_once("ms=")?;
                            Some([ms.parse::<u64>().ok()?, count.parse::<u64>().ok()?])
                        })
                        .collect::<Vec<_>>()
                )
            }
            _ if key.ends_with("th percentile") && !key.contains("gpu") => {
                process["percentiles_ms"][key.trim_end_matches("th percentile")] =
                    json!(leading(value));
            }
            _ => {
                if let Some(name) = key.strip_prefix("Number ") {
                    process["counters"][snake(name)] = json!(leading(value));
                }
            }
        }
    }
    processes
}

/// Frame metrics over an app's processes. Slow frames took 17 ms or more and
/// frozen frames 700 ms or more, counted from the histogram; the percentiles
/// are those dumpsys gives for the app's main process.
pub fn frame_metrics(processes: &[Value], id: &str) -> Map<String, Value> {
    let rendering: Vec<&Value> = processes
        .iter()
        .filter(|p| p["frames_total"].is_number())
        .collect();
    let mut metrics = Map::new();
    let names = [
        "frames_total",
        "janky_frames",
        "jank_percent",
        "slow_frames",
        "frozen_frames",
        "frame_p50_ms",
        "frame_p90_ms",
        "frame_p95_ms",
        "frame_p99_ms",
    ];
    for name in names {
        metrics.insert(name.into(), Value::Null);
    }
    if rendering.is_empty() {
        return metrics;
    }
    let sum = |key: &str| {
        rendering
            .iter()
            .filter_map(|p| p[key].as_f64())
            .sum::<f64>()
    };
    let total = sum("frames_total");
    let janky = sum("janky_frames");
    let histogram = |minimum: u64| {
        rendering
            .iter()
            .flat_map(|p| p["histogram"].as_array().cloned().unwrap_or_default())
            .filter(|bucket| bucket[0].as_u64().unwrap_or(0) >= minimum)
            .filter_map(|bucket| bucket[1].as_f64())
            .sum::<f64>()
    };
    metrics.insert("frames_total".into(), json!(total));
    metrics.insert("janky_frames".into(), json!(janky));
    metrics.insert("slow_frames".into(), json!(histogram(17)));
    metrics.insert("frozen_frames".into(), json!(histogram(700)));
    if total > 0.0 {
        metrics.insert("jank_percent".into(), json!(round(janky / total * 100.0)));
        let main = rendering
            .iter()
            .find(|p| s(p, "process") == id)
            .unwrap_or(&rendering[0]);
        for p in ["50", "90", "95", "99"] {
            metrics.insert(format!("frame_p{p}_ms"), main["percentiles_ms"][p].clone());
        }
    }
    metrics
}

fn not_running(output: &str, id: &str) -> Result<()> {
    if output.contains("No process found") {
        return Err(
            Error::new("app_not_running", format!("{id} is not running"))
                .remedy("Launch the app first, for example with device.app"),
        );
    }
    Ok(())
}

/// Starts a new frame count for the app.
pub fn frames_reset(device: &str, id: &str) -> Result<()> {
    bundle(id)?;
    not_running(
        &shell_out(device, &["dumpsys", "gfxinfo", id, "reset"], 30)?,
        id,
    )
}

/// The frames the app rendered since the last reset, as a report.
pub fn frames_read(device: &str, id: &str) -> Result {
    bundle(id)?;
    let output = shell_out(device, &["dumpsys", "gfxinfo", id], 30)?;
    not_running(&output, id)?;
    let processes = parse_gfxinfo(&output);
    let mut value = report("frames", device, id, frame_metrics(&processes, id));
    value["processes"] = json!(processes);
    Ok(value)
}

/// Resets the app's frame statistics, waits `seconds` or runs a testing.run
/// scenario, and reads them.
pub fn frames(a: &Value) -> Result {
    let device = identifier(s(a, "device"), "device", None)?;
    let id = bundle(s(a, "bundle_id"))?;
    let scenario = if s(a, "scenario").is_empty() {
        None
    } else {
        let scenario = read_json(&path(s(a, "scenario"), false, true)?, None)?;
        crate::automation::check_scenario(&scenario)?;
        Some(scenario)
    };
    frames_reset(device, id)?;
    let started = Instant::now();
    if let Some(scenario) = &scenario {
        // Screenshots of a failed step go beside the report.
        let directory = if s(a, "report").is_empty() {
            home()?.join("profile")
        } else {
            output_path(s(a, "report"))?
                .parent()
                .map(std::path::Path::to_path_buf)
                .unwrap_or(home()?)
        };
        private_dir(&directory)?;
        let tag = format!(
            "frames-{}",
            &uuid::Uuid::new_v4().simple().to_string()[..12]
        );
        let result = crate::automation::run_device(device, scenario, &directory, &tag);
        if !b(&result, "passed") {
            return Err(Error::new(
                "scenario_failed",
                "The scenario failed, so its frames were not read",
            )
            .details(result));
        }
    } else {
        pause(Duration::from_secs(a["seconds"].as_u64().unwrap_or(5)))?;
    }
    let mut value = frames_read(device, id)?;
    value["seconds"] = json!(round(started.elapsed().as_secs_f64()));
    if let Some(scenario) = &scenario {
        value["scenario"] = scenario["name"].clone();
    }
    finish(a, value)
}

/// The process sections of `dumpsys meminfo PACKAGE`, in KB: the App Summary
/// (PSS and RSS per category and the totals), the size, allocated and free
/// parts of the native and Dalvik heaps, and the object counts.
pub fn parse_meminfo(text: &str) -> Vec<Value> {
    let header = regex::Regex::new(r"^\*\* MEMINFO in pid (\d+) \[(.*)\] \*\*").unwrap();
    let total = |name: &str| regex::Regex::new(&format!(r"{name}[^:]*:\s+(\d+)")).unwrap();
    let (total_pss, total_rss, total_swap) = (
        total(r"TOTAL PSS"),
        total(r"TOTAL RSS"),
        total(r"TOTAL SWAP"),
    );
    let pair = regex::Regex::new(r"([A-Za-z][A-Za-z ]*?):\s+(\d+)").unwrap();
    let digits = regex::Regex::new(r"\d+").unwrap();
    let mut processes: Vec<Value> = Vec::new();
    let mut section = "";
    // Where the Pss(KB) and Rss(KB) columns of the App Summary end.
    let mut columns: Vec<(usize, &str)> = Vec::new();
    for raw in text.lines() {
        let line = raw.trim();
        if let Some(c) = header.captures(line) {
            processes.push(json!({"pid":c[1].parse::<u64>().unwrap_or(0),"process":&c[2],"summary":{},
                                  "total_pss":null,"total_rss":null,"total_swap":null,"heaps":{},"objects":{}}));
            section = "table";
            continue;
        }
        let Some(process) = processes.last_mut() else {
            continue;
        };
        match line {
            "App Summary" => {
                section = "summary";
                continue;
            }
            "Objects" => {
                section = "objects";
                continue;
            }
            "SQL" | "DATABASES" | "Native Allocations" | "Dalvik Details" | "Asset Allocations" => {
                section = "";
                continue;
            }
            _ => (),
        }
        match section {
            "table" => {
                let heap = if line.starts_with("Native Heap") {
                    "native"
                } else if line.starts_with("Dalvik Heap") {
                    "dalvik"
                } else {
                    continue;
                };
                let numbers: Vec<u64> = line
                    .split_whitespace()
                    .filter_map(|w| w.parse().ok())
                    .collect();
                // The last three columns are Heap Size, Heap Alloc and Heap Free.
                if numbers.len() >= 7 {
                    let n = numbers.len();
                    process["heaps"][heap] =
                        json!({"size":numbers[n-3],"allocated":numbers[n-2],"free":numbers[n-1]});
                }
            }
            "summary" => {
                if raw.contains("Pss(KB)") {
                    columns = ["Pss(KB)", "Rss(KB)"]
                        .iter()
                        .filter_map(|h| raw.find(h).map(|i| (i + h.len(), *h)))
                        .map(|(end, h)| (end, if h.starts_with('P') { "pss" } else { "rss" }))
                        .collect();
                } else if line.starts_with("TOTAL") {
                    let read = |re: &regex::Regex| {
                        re.captures(line)
                            .and_then(|c| c[1].parse::<u64>().ok())
                            .map_or(Value::Null, |v| json!(v))
                    };
                    process["total_pss"] = read(&total_pss);
                    process["total_rss"] = read(&total_rss);
                    process["total_swap"] = read(&total_swap);
                    // Before Android 10 the summary total reads "TOTAL: n".
                    if process["total_pss"].is_null() {
                        process["total_pss"] = read(&total("TOTAL"));
                    }
                } else if let Some((name, values)) = raw.split_once(':') {
                    let name = snake(name);
                    let offset = raw.len() - values.len();
                    let mut entry = json!({});
                    for number in digits.find_iter(values) {
                        let end = offset + number.end();
                        if let Some((_, column)) =
                            columns.iter().min_by_key(|(c, _)| c.abs_diff(end))
                        {
                            entry[*column] = json!(number.as_str().parse::<u64>().unwrap_or(0));
                        }
                    }
                    if !name.is_empty() && !entry.as_object().unwrap().is_empty() {
                        process["summary"][name] = entry;
                    }
                }
            }
            "objects" => {
                for c in pair.captures_iter(line) {
                    process["objects"][snake(&c[1])] = json!(c[2].parse::<u64>().unwrap_or(0));
                }
            }
            _ => (),
        }
    }
    processes
}

/// Memory metrics in MB over an app's processes, and their activity and
/// view counts.
pub fn memory_metrics(processes: &[Value]) -> Map<String, Value> {
    let sum = |value: &dyn Fn(&Value) -> &Value| {
        let values: Vec<f64> = processes.iter().filter_map(|p| value(p).as_f64()).collect();
        (!values.is_empty()).then(|| values.iter().sum::<f64>())
    };
    let mb = |kb: Option<f64>| kb.map_or(Value::Null, |kb| json!(round(kb / 1024.0)));
    let mut metrics = Map::new();
    metrics.insert("memory_pss_mb".into(), mb(sum(&|p| &p["total_pss"])));
    metrics.insert("memory_rss_mb".into(), mb(sum(&|p| &p["total_rss"])));
    for part in ["java_heap", "native_heap", "code", "stack", "graphics"] {
        metrics.insert(
            format!("{part}_pss_mb"),
            mb(sum(&|p| &p["summary"][part]["pss"])),
        );
    }
    for object in ["activities", "views"] {
        metrics.insert(
            object.into(),
            sum(&|p| &p["objects"][object]).map_or(Value::Null, |v| json!(v)),
        );
    }
    metrics
}

/// Writes a heap dump of the app's main process to `target`, through a file
/// on the device that is removed afterwards.
fn heap_dump(device: &str, id: &str, target: &std::path::Path) -> Result {
    let remote = format!(
        "/data/local/tmp/tomacco-{}.hprof",
        uuid::Uuid::new_v4().simple()
    );
    let result = (|| -> Result {
        let output = shell_out(device, &["am", "dumpheap", id, &remote], 300)?;
        if output.contains("Exception") || output.lines().any(|l| l.starts_with("Error")) {
            return Err(
                Error::new("heap_dump_failed", output.trim().to_string()).remedy(
                    "Heap dumps need a debuggable app, or an emulator image without Google Play",
                ),
            );
        }
        let mut pull = devices::adb(device)?;
        pull.extend(argv(&["pull", &remote, &target.to_string_lossy()]));
        tc::run(&pull, 600, None, true)?;
        let mut magic = [0u8; 12];
        use std::io::Read;
        std::fs::File::open(target)?
            .read_exact(&mut magic)
            .map_err(|_| {
                Error::new(
                    "heap_dump_failed",
                    "The heap dump pulled from the device is empty",
                )
            })?;
        if &magic != b"JAVA PROFILE" {
            return Err(Error::new(
                "heap_dump_failed",
                "The file pulled from the device is not an HPROF heap dump",
            ));
        }
        artifact(target, 0.0)
    })();
    let _ = tc::run(
        &devices::shell(device, &argv(&["rm", "-f", &remote]))?,
        30,
        None,
        false,
    );
    result
}

/// The app's memory from `dumpsys meminfo`, and a heap dump when asked.
pub fn memory(a: &Value) -> Result {
    let device = identifier(s(a, "device"), "device", None)?;
    let id = bundle(s(a, "bundle_id"))?;
    let target = if s(a, "heap_dump").is_empty() {
        None
    } else {
        Some(output_path(s(a, "heap_dump"))?)
    };
    let output = shell_out(device, &["dumpsys", "meminfo", id], 60)?;
    not_running(&output, id)?;
    let processes = parse_meminfo(&output);
    if processes.is_empty() {
        return Err(Error::new(
            "meminfo_unreadable",
            "dumpsys meminfo named no process of the app",
        )
        .details(json!({"output":output})));
    }
    let mut value = report("memory", device, id, memory_metrics(&processes));
    value["processes"] = json!(processes);
    if let Some(target) = target {
        value["heap_dump"] = heap_dump(device, id, &target)?;
    }
    finish(a, value)
}

/// The app's processes in `ps -A -o PID,NAME`: the package and its
/// `package:name` processes.
pub fn app_pids(ps: &str, id: &str) -> Vec<(u64, String)> {
    ps.lines()
        .filter_map(|line| {
            let mut parts = line.split_whitespace();
            let pid = parts.next()?.parse::<u64>().ok()?;
            let name = parts.next()?;
            (name == id
                || name
                    .strip_prefix(id)
                    .is_some_and(|rest| rest.starts_with(':')))
            .then(|| (pid, name.to_string()))
        })
        .collect()
}

/// CPU time counters at one moment: clock ticks per second, the ticks of all
/// CPUs from /proc/stat, and user plus system ticks per app process.
#[derive(Debug, Default)]
pub struct CpuSample {
    pub clock: u64,
    pub total: u64,
    pub processes: std::collections::BTreeMap<u64, (String, u64)>,
}

/// Reads `getconf CLK_TCK`, the `cpu` line of /proc/stat and /proc/PID/stat
/// lines. A process's name comes from ps, as /proc truncates it.
pub fn parse_cpu_sample(output: &str, pids: &[(u64, String)]) -> CpuSample {
    let mut sample = CpuSample {
        clock: 100,
        ..Default::default()
    };
    for (index, line) in output.lines().enumerate() {
        let line = line.trim();
        if index == 0 {
            if let Ok(clock) = line.parse::<u64>() {
                sample.clock = clock.max(1);
                continue;
            }
        }
        if let Some(rest) = line.strip_prefix("cpu ") {
            // user nice system idle iowait irq softirq steal; guest time is
            // already part of user.
            sample.total = rest
                .split_whitespace()
                .take(8)
                .filter_map(|v| v.parse::<u64>().ok())
                .sum();
        } else if let Some((head, tail)) = line.rsplit_once(") ") {
            let Some(pid) = head.split_whitespace().next().and_then(|p| p.parse().ok()) else {
                continue;
            };
            let Some((_, name)) = pids.iter().find(|(p, _)| *p == pid) else {
                continue;
            };
            // After the name: state is field 3, utime 14 and stime 15.
            let fields: Vec<&str> = tail.split_whitespace().collect();
            let ticks = |i: usize| fields.get(i).and_then(|v| v.parse::<u64>().ok());
            if let (Some(user), Some(system)) = (ticks(11), ticks(12)) {
                sample.processes.insert(pid, (name.clone(), user + system));
            }
        }
    }
    sample
}

fn cpu_sample(device: &str, id: &str) -> Result<(CpuSample, Instant)> {
    let pids = app_pids(&shell_out(device, &["ps", "-A", "-o", "PID,NAME"], 30)?, id);
    let mut script = String::from("getconf CLK_TCK; head -n 1 /proc/stat");
    for (pid, _) in &pids {
        script.push_str(&format!("; cat /proc/{pid}/stat 2>/dev/null"));
    }
    script.push_str("; true");
    let output = shell_out(device, &["sh", "-c", &script], 30)?;
    Ok((parse_cpu_sample(&output, &pids), Instant::now()))
}

/// CPU use between two samples. A process that started in between counts
/// from zero; one that ended counts for nothing.
pub fn cpu_usage(before: &CpuSample, after: &CpuSample, seconds: f64) -> Value {
    let clock = after.clock as f64;
    let mut ticks = 0;
    let processes: Vec<Value> = after
        .processes
        .iter()
        .map(|(pid, (name, now))| {
            let earlier = before.processes.get(pid).map_or(0, |(_, t)| *t);
            let used = if earlier > *now { *now } else { now - earlier };
            ticks += used;
            json!({"pid":pid,"process":name,"cpu_ms":round(used as f64*1000.0/clock)})
        })
        .collect();
    let cpu_ms = ticks as f64 * 1000.0 / clock;
    let total = after.total.saturating_sub(before.total);
    json!({"cpu_ms":round(cpu_ms),
           "cpu_percent":if seconds>0.0 {json!(round(cpu_ms/(seconds*1000.0)*100.0))} else {Value::Null},
           "cpu_device_percent":if total>0 {json!(round(ticks as f64/total as f64*100.0))} else {Value::Null},
           "processes":processes})
}

/// The UID as batterystats prints it: `1000`, or `u0a191` for 10191.
pub fn battery_uid(uid: u64) -> String {
    let (user, app) = (uid / 100_000, uid % 100_000);
    if app < 10_000 {
        if user == 0 {
            app.to_string()
        } else {
            format!("u{user}s{app}")
        }
    } else {
        format!("u{user}a{}", app - 10_000)
    }
}

/// The UID's line in batterystats' "Estimated power use (mAh)": its total
/// and the parts on the line after it (cpu, sensors, wakelock and others).
pub fn parse_power(text: &str, label: &str) -> Option<(f64, Map<String, Value>)> {
    let prefix = format!("UID {label}: ");
    let mut lines = text.lines().map(str::trim);
    let total = lines.by_ref().find_map(|line| {
        line.strip_prefix(&prefix)
            .and_then(|rest| rest.split_whitespace().next())
            .and_then(|v| v.parse::<f64>().ok())
    })?;
    let part = regex::Regex::new(r"(?:^|\s)([A-Za-z_]+)=([0-9.eE+-]+)").unwrap();
    let mut parts = Map::new();
    if let Some(line) = lines
        .next()
        .filter(|l| !l.starts_with("UID ") && !l.starts_with('('))
    {
        for c in part.captures_iter(line) {
            if let Ok(value) = c[2].parse::<f64>() {
                parts.insert(c[1].to_string(), json!(value));
            }
        }
    }
    Some((total, parts))
}

/// Whether `dumpsys battery` names a connected charger, and the level.
pub fn parse_battery(text: &str) -> Value {
    let mut charging = false;
    let mut level = Value::Null;
    for line in text.lines().map(str::trim) {
        if let Some((key, value)) = line.split_once(": ") {
            if key.ends_with(" powered") && value == "true" {
                charging = true;
            } else if key == "level" {
                level = value.parse::<u64>().map_or(Value::Null, |v| json!(v));
            }
        }
    }
    json!({"charging":charging,"level":level})
}

/// mAh to a millionth, as batterystats prints them.
fn micro(value: f64) -> f64 {
    (value * 1e6).round() / 1e6
}

/// Battery use estimated by Android's power model over the interval: the
/// change of the UID's mAh since the last charge, part by part.
pub fn power_delta(
    before: Option<&(f64, Map<String, Value>)>,
    after: Option<&(f64, Map<String, Value>)>,
) -> Value {
    let Some((total, parts)) = after else {
        return json!({"power_mah":null,"parts_mah":{}});
    };
    let (old_total, old_parts) = before.map_or((0.0, None), |(t, p)| (*t, Some(p)));
    let mut delta = Map::new();
    for (name, value) in parts {
        let old = old_parts
            .and_then(|p| p.get(name).and_then(Value::as_f64))
            .unwrap_or(0.0);
        delta.insert(
            name.clone(),
            json!(micro((value.as_f64().unwrap_or(0.0) - old).max(0.0))),
        );
    }
    json!({"power_mah":micro((total-old_total).max(0.0)),"parts_mah":delta})
}

/// The app's CPU time from /proc and its estimated battery use from
/// batterystats over `seconds`.
pub fn usage(a: &Value) -> Result {
    let device = identifier(s(a, "device"), "device", None)?;
    let id = bundle(s(a, "bundle_id"))?;
    let seconds = a["seconds"].as_u64().unwrap_or(10).clamp(1, 600);
    let listing = shell_out(
        device,
        &["cmd", "package", "list", "packages", "-U", id],
        30,
    )?;
    let uid = devices::package_uid(&listing, id)
        .and_then(|uid| uid.parse::<u64>().ok())
        .ok_or_else(|| {
            Error::new("app_not_installed", format!("{id} is not installed"))
                .remedy("Install the app with device.install or project.run")
        })?;
    let label = battery_uid(uid);
    let power = || -> Result<Option<(f64, Map<String, Value>)>> {
        let text = shell_out(device, &["dumpsys", "batterystats", "--charged", id], 60)?;
        Ok(parse_power(&text, &label))
    };
    let power_before = power()?;
    let (before, started) = cpu_sample(device, id)?;
    pause(Duration::from_secs(seconds))?;
    let (after, ended) = cpu_sample(device, id)?;
    let power_after = power()?;
    if before.processes.is_empty() && after.processes.is_empty() {
        return Err(
            Error::new("app_not_running", format!("{id} is not running"))
                .remedy("Launch the app first, for example with device.app"),
        );
    }
    let elapsed = (ended - started).as_secs_f64();
    let cpu = cpu_usage(&before, &after, elapsed);
    let mut battery = power_delta(power_before.as_ref(), power_after.as_ref());
    let state = parse_battery(&shell_out(device, &["dumpsys", "battery"], 30)?);
    battery["uid"] = json!(uid);
    battery["charging"] = state["charging"].clone();
    battery["level"] = state["level"].clone();
    let mut metrics = Map::new();
    for key in ["cpu_percent", "cpu_device_percent", "cpu_ms"] {
        metrics.insert(key.into(), cpu[key].clone());
    }
    metrics.insert("power_mah".into(), battery["power_mah"].clone());
    let mut value = report("usage", device, id, metrics);
    value["seconds"] = json!(round(elapsed));
    value["processes"] = cpu["processes"].clone();
    value["battery"] = battery;
    finish(a, value)
}

/// A budget: per metric a maximum, a minimum or both.
pub fn budget_schema() -> Value {
    json!({"type":"object","minProperties":1,"maxProperties":100,"additionalProperties":{
        "type":"object","minProperties":1,"additionalProperties":false,
        "properties":{"maximum":{"type":"number"},"minimum":{"type":"number"}}}})
}

/// Checks metrics against a budget. A metric that is missing or null fails,
/// as an unmeasured value proves nothing.
pub fn check_budget(budget: &Value, metrics: &Map<String, Value>) -> (bool, Vec<Value>) {
    let mut passed = true;
    let mut results = Vec::new();
    for (metric, limit) in budget.as_object().into_iter().flatten() {
        let mut result =
            json!({"metric":metric,"maximum":limit["maximum"],"minimum":limit["minimum"]});
        match metrics.get(metric).and_then(Value::as_f64) {
            Some(value) => {
                let ok = limit["maximum"].as_f64().is_none_or(|max| value <= max)
                    && limit["minimum"].as_f64().is_none_or(|min| value >= min);
                result["value"] = json!(value);
                result["passed"] = json!(ok);
                passed &= ok;
            }
            None => {
                result["value"] = Value::Null;
                result["passed"] = json!(false);
                result["reason"] = json!("measurement_unavailable");
                passed = false;
            }
        }
        results.push(result);
    }
    (passed, results)
}

/// The error of a failed budget, naming the metrics over or under it.
pub fn budget_error(results: &[Value]) -> Error {
    let failed: Vec<&str> = results
        .iter()
        .filter(|r| !b(r, "passed"))
        .map(|r| s(r, "metric"))
        .collect();
    Error::new(
        "budget_exceeded",
        format!("Outside the performance budget: {}", failed.join(", ")),
    )
    .details(json!({"results":results}))
}

/// A budget given inline as `budgets` or as a JSON file at `budget`.
pub fn budget_argument(a: &Value) -> Result<Option<Value>> {
    let value = match (s(a, "budget").is_empty(), a.get("budgets")) {
        (true, None) => return Ok(None),
        (false, Some(_)) => {
            return Err(Error::new(
                "invalid_argument",
                "Give the budget either as a file (budget) or inline (budgets)",
            ));
        }
        (true, Some(inline)) => inline.clone(),
        (false, None) => read_json(&path(s(a, "budget"), false, true)?, None)?,
    };
    validate(&value, &budget_schema(), "budget")?;
    Ok(Some(value))
}

/// Checks saved reports of one app against a budget.
pub fn budget(a: &Value) -> Result {
    let limits = budget_argument(a)?
        .ok_or_else(|| Error::new("invalid_argument", "Give a budget file or budgets"))?;
    let mut metrics = Map::new();
    let mut sources = Map::new();
    let mut app = Value::Null;
    for file in strings(&a["reports"]) {
        let file = path(&file, false, true)?;
        let report = read_json(&file, None)?;
        let Some(values) = report["metrics"].as_object() else {
            return Err(Error::new(
                "invalid_report",
                format!("{} has no metrics", file.display()),
            ));
        };
        if !app.is_null() && report["bundle_id"] != app {
            return Err(Error::new(
                "profile_mismatch",
                "Check reports of the same application",
            ));
        }
        app = report["bundle_id"].clone();
        // A later report's value of a metric replaces an earlier one.
        for (metric, value) in values {
            metrics.insert(metric.clone(), value.clone());
            sources.insert(metric.clone(), json!(file));
        }
    }
    let (passed, mut results) = check_budget(&limits, &metrics);
    for result in &mut results {
        result["report"] = sources
            .get(s(result, "metric"))
            .cloned()
            .unwrap_or(Value::Null);
    }
    let value = json!({"passed":passed,"bundle_id":app,"results":results,"created":now()});
    finish(a, value)
}

/// One `measure` step of a testing.run scenario.
pub fn measure(device: &str, step: &Value) -> Result {
    let id = s(step, "bundle_id");
    let mut args = json!({"device":device,"bundle_id":id});
    for key in ["mode", "runs", "activity", "seconds"] {
        if let Some(value) = step.get(key) {
            args[key] = value.clone();
        }
    }
    match s(step, "measure") {
        "startup" => startup(&args),
        "frames_reset" => {
            frames_reset(device, id)?;
            Ok(json!({"kind":"frames_reset","bundle_id":id,"metrics":{}}))
        }
        "frames" => frames_read(device, id),
        "memory" => memory(&json!({"device":device,"bundle_id":id})),
        "usage" => usage(&args),
        other => Err(Error::new(
            "invalid_step",
            format!("Unknown measurement: {other}"),
        )),
    }
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
    // Recorded from an API 36 x86_64 emulator: Settings scrolled up and down.
    const GFXINFO: &str = "Applications Graphics Acceleration Info:\nUptime: 46410 Realtime: 46410\n\n** Graphics info for pid 3095 [com.android.settings] **\n\nStats since: 44928220974ns\nTotal frames rendered: 79\nJanky frames: 4 (5.06%)\nJanky frames (legacy): 51 (64.56%)\n50th percentile: 18ms\n90th percentile: 29ms\n95th percentile: 32ms\n99th percentile: 85ms\nNumber Missed Vsync: 0\nNumber High input latency: 146\nNumber Slow UI thread: 0\nNumber Slow bitmap uploads: 0\nNumber Slow issue draw commands: 4\nNumber Frame deadline missed: 4\nNumber Frame deadline missed (legacy): 4\nHISTOGRAM: 5ms=0 6ms=0 7ms=3 8ms=1 9ms=3 10ms=1 11ms=0 12ms=2 13ms=1 14ms=1 15ms=1 16ms=6 17ms=11 18ms=14 19ms=6 20ms=6 21ms=5 22ms=3 23ms=0 24ms=2 25ms=1 26ms=1 27ms=1 28ms=1 29ms=2 30ms=0 31ms=2 32ms=3 34ms=0 85ms=2 650ms=0 700ms=0 4950ms=0\n50th gpu percentile: 7ms\n90th gpu percentile: 22ms\nGPU HISTOGRAM: 1ms=0 5ms=9 4950ms=4\n\nPipeline=Skia (OpenGL)\nMemory policy:\n  Max surface area: 2617920\nTotal GPU memory usage:\n  22332776 bytes, 21.30 MB (19.22 MB is purgeable)\n";

    #[test]
    fn gfxinfo_gives_frames_jank_percentiles_and_histogram_counts() {
        let processes = parse_gfxinfo(GFXINFO);
        assert_eq!(processes.len(), 1);
        assert_eq!(processes[0]["pid"], 3095);
        assert_eq!(processes[0]["counters"]["high_input_latency"], 146.0);
        assert_eq!(
            processes[0]["counters"]["frame_deadline_missed_legacy"],
            4.0
        );
        let metrics = frame_metrics(&processes, "com.android.settings");
        assert_eq!(metrics["frames_total"], 79.0);
        assert_eq!(metrics["janky_frames"], 4.0);
        assert_eq!(metrics["jank_percent"], 5.063);
        assert_eq!(metrics["frame_p90_ms"], 29.0);
        assert_eq!(metrics["frame_p99_ms"], 85.0);
        // 17 ms and slower: 11+14+6+6+5+3+2+1+1+1+1+2+2+3+2.
        assert_eq!(metrics["slow_frames"], 60.0);
        assert_eq!(metrics["frozen_frames"], 0.0);
        let frozen = GFXINFO
            .replace("700ms=0", "700ms=2")
            .replace("4950ms=0\n", "4950ms=1\n");
        assert_eq!(
            frame_metrics(&parse_gfxinfo(&frozen), "com.android.settings")["frozen_frames"],
            3.0
        );
    }

    #[test]
    fn processes_add_up_and_a_process_without_frames_is_unavailable() {
        let two = format!(
            "{GFXINFO}\n** Graphics info for pid 4000 [com.android.settings:remote] **\nTotal frames rendered: 21\nJanky frames: 6 (28.57%)\n50th percentile: 40ms\n"
        );
        let metrics = frame_metrics(&parse_gfxinfo(&two), "com.android.settings");
        assert_eq!(
            (
                metrics["frames_total"].clone(),
                metrics["janky_frames"].clone()
            ),
            (json!(100.0), json!(10.0))
        );
        assert_eq!(metrics["jank_percent"], 10.0);
        assert_eq!(
            metrics["frame_p50_ms"], 18.0,
            "the main process's percentiles"
        );
        let idle = "** Graphics info for pid 1292 [com.android.phone] **\nGraphicBufferAllocator buffers:\nTotal imported by gralloc: 0KiB\n";
        let metrics = frame_metrics(&parse_gfxinfo(idle), "com.android.phone");
        assert!(metrics["frames_total"].is_null() && metrics["jank_percent"].is_null());
        assert!(not_running("No process found for: dev.example\n", "dev.example").is_err());
    }
    // Recorded from an API 36 x86_64 emulator, Settings app (shortened).
    const MEMINFO: &str = "Applications Memory Usage (in Kilobytes):\nUptime: 58688 Realtime: 58688\n\n** MEMINFO in pid 3095 [com.android.settings] **\n                   Pss  Private  Private     Swap      Rss     Heap     Heap     Heap\n                 Total    Dirty    Clean    Dirty    Total     Size    Alloc     Free\n                ------   ------   ------   ------   ------   ------   ------   ------\n  Native Heap    20060    20016        0        0    23720    34548    23594     7154\n  Dalvik Heap     9747     9648        0        0    16328    15105     7553     7552\n        Stack      788      788        0        0      796                           \n        TOTAL    62455    37076    16152        0   221696    49653    31147    14706\n \n App Summary\n                       Pss(KB)                        Rss(KB)\n                        ------                         ------\n           Java Heap:    11104                          48396\n         Native Heap:    20016                          23720\n                Code:    15968                         138324\n               Stack:      788                            796\n            Graphics:        0                              0\n       Private Other:     5352\n              System:     9227\n             Unknown:                                   10460\n \n           TOTAL PSS:    62455            TOTAL RSS:   221696      TOTAL SWAP (KB):        0\n \n Objects\n               Views:      164         ViewRootImpl:        1\n         AppContexts:        9           Activities:        1\n \n SQL\n         MEMORY_USED:      176\n";

    #[test]
    fn meminfo_gives_the_summary_heaps_and_objects() {
        let processes = parse_meminfo(MEMINFO);
        assert_eq!(processes.len(), 1);
        let p = &processes[0];
        assert_eq!(
            (
                p["total_pss"].clone(),
                p["total_rss"].clone(),
                p["total_swap"].clone()
            ),
            (json!(62455), json!(221696), json!(0))
        );
        assert_eq!(p["summary"]["java_heap"], json!({"pss":11104,"rss":48396}));
        assert_eq!(p["summary"]["private_other"], json!({"pss":5352}));
        assert_eq!(p["summary"]["unknown"], json!({"rss":10460}));
        assert_eq!(
            p["heaps"]["dalvik"],
            json!({"size":15105,"allocated":7553,"free":7552})
        );
        assert_eq!(p["objects"]["view_root_impl"], 1);
        assert_eq!(p["objects"]["activities"], 1);
        assert!(p["objects"].get("memory_used").is_none());
        let metrics = memory_metrics(&processes);
        assert_eq!(metrics["memory_pss_mb"], 60.991);
        assert_eq!(metrics["memory_rss_mb"], 216.5);
        assert_eq!(metrics["java_heap_pss_mb"], 10.844);
        assert_eq!(metrics["graphics_pss_mb"], 0.0);
        assert_eq!(metrics["views"], 164.0);
    }

    #[test]
    fn processes_add_up_and_the_old_total_line_is_read() {
        let old = "** MEMINFO in pid 7 [dev.example:sync] **\n App Summary\n                       Pss(KB)\n                        ------\n           Java Heap:     1024\n           TOTAL:     2048       TOTAL SWAP PSS:       12\n";
        let processes = parse_meminfo(&format!("{MEMINFO}{old}"));
        assert_eq!(processes[1]["total_pss"], 2048);
        assert_eq!(processes[1]["total_swap"], 12);
        assert!(processes[1]["total_rss"].is_null());
        let metrics = memory_metrics(&processes);
        assert_eq!(metrics["memory_pss_mb"], 62.991);
        assert_eq!(
            metrics["memory_rss_mb"], 216.5,
            "only processes that report RSS"
        );
        assert_eq!(metrics["java_heap_pss_mb"], 11.844);
        assert!(memory_metrics(&[])["memory_pss_mb"].is_null());
    }
    #[test]
    fn cpu_use_is_the_change_of_process_ticks_over_the_interval() {
        let ps = "  PID NAME\n 3095 com.android.settings\n 3100 com.android.settings:remote\n 3200 com.android.settings.intelligence\n";
        let pids = app_pids(ps, "com.android.settings");
        assert_eq!(
            pids,
            [
                (3095, "com.android.settings".to_string()),
                (3100, "com.android.settings:remote".to_string())
            ]
        );
        // Recorded from an API 36 x86_64 emulator; the name in /proc is cut short.
        let before = parse_cpu_sample(
            "100\ncpu  4840 3102 6545 11540 655 182 207 86 0 0\n3095 (ndroid.settings) S 493 493 0 0 -1 4194624 37887 0 116 0 194 182 0 0 10 -10 39 0 3716\n",
            &pids,
        );
        assert_eq!(before.total, 27157);
        assert_eq!(
            before.processes[&3095],
            ("com.android.settings".to_string(), 376)
        );
        let after = parse_cpu_sample(
            "100\ncpu  5040 3102 6645 12540 655 182 207 86 0 0\n3095 (ndroid.settings) S 493 493 0 0 -1 4194624 37887 0 116 0 244 207 0 0 10 -10 39 0 3716\n3100 (a (b) c) S 493 493 0 0 -1 0 0 0 0 0 10 5 0 0\n",
            &pids,
        );
        let usage = cpu_usage(&before, &after, 2.0);
        // 75 ticks of the app process and 15 of a new one, of 1300 in all.
        assert_eq!(usage["cpu_ms"], 900.0);
        assert_eq!(usage["cpu_percent"], 45.0);
        assert_eq!(usage["cpu_device_percent"], 6.923);
        assert_eq!(usage["processes"][1]["cpu_ms"], 150.0);
    }

    // Recorded from an API 36 x86_64 emulator (shortened).
    const BATTERYSTATS: &str = "  Estimated power use (mAh):\n    Capacity: 1000, Computed drain: 0, actual drain: 0\n    Global\n    cpu: 0.00121 apps: 0.00116 duration: 43s 643ms \n  UID u0a144: 0.0717 fg: 0.000425 (260ms) bg: 0.0665 (39s 689ms) fgs: 0.00319 (1s 941ms) cached: 0.00161 (978ms)\n      cpu=0.000184 cpu:fg=0.00000100 cpu:bg=0.000171 sensors=0.0716 sensors:fg=0.000424\n      (on battery, screen on) cpu=0.000184 sensors=0.0716\n  UID 1000: 0.000327 bg: 0.000327\n      cpu=0.000327 cpu:bg=0.000327\n";

    #[test]
    fn battery_use_is_read_per_uid_and_compared() {
        assert_eq!(battery_uid(1000), "1000");
        assert_eq!(battery_uid(10144), "u0a144");
        assert_eq!(battery_uid(1010144), "u10a144");
        let after = parse_power(BATTERYSTATS, "u0a144").unwrap();
        assert_eq!(after.0, 0.0717);
        assert_eq!(
            after.1,
            *json!({"cpu":0.000184,"sensors":0.0716})
                .as_object()
                .unwrap()
        );
        assert_eq!(parse_power(BATTERYSTATS, "1000").unwrap().0, 0.000327);
        assert!(parse_power(BATTERYSTATS, "u0a1").is_none());
        let before = parse_power(
            &BATTERYSTATS.replace("0.0717", "0.0517").replace(
                "cpu=0.000184 cpu:fg=0.00000100 cpu:bg=0.000171 sensors=0.0716",
                "sensors=0.0516",
            ),
            "u0a144",
        )
        .unwrap();
        let delta = power_delta(Some(&before), Some(&after));
        assert!((delta["power_mah"].as_f64().unwrap() - 0.02).abs() < 1e-9);
        assert!((delta["parts_mah"]["sensors"].as_f64().unwrap() - 0.02).abs() < 1e-9);
        assert_eq!(
            delta["parts_mah"]["cpu"], 0.000184,
            "a part new since the first read"
        );
        assert_eq!(power_delta(None, Some(&after))["power_mah"], 0.0717);
        assert!(power_delta(Some(&before), None)["power_mah"].is_null());
        let battery = parse_battery(
            "Current Battery Service state:\n  AC powered: true\n  USB powered: false\n  level: 100\n",
        );
        assert_eq!(battery, json!({"charging":true,"level":100}));
    }
    #[test]
    fn a_budget_checks_maximums_minimums_and_missing_values() {
        let metrics = json!({"startup_cold_median_ms":640.0,"jank_percent":7.5,"frames_total":90.0,"frame_p90_ms":null});
        let metrics = metrics.as_object().unwrap();
        let budget = json!({"startup_cold_median_ms":{"maximum":800},"jank_percent":{"maximum":5},
                            "frames_total":{"minimum":60,"maximum":1000},"frame_p90_ms":{"maximum":32},"memory_pss_mb":{"maximum":200}});
        validate(&budget, &budget_schema(), "budget").unwrap();
        let (passed, results) = check_budget(&budget, metrics);
        assert!(!passed);
        let verdict: Vec<(&str, bool)> = results
            .iter()
            .map(|r| (s(r, "metric"), b(r, "passed")))
            .collect();
        assert_eq!(
            verdict,
            [
                ("startup_cold_median_ms", true),
                ("jank_percent", false),
                ("frames_total", true),
                ("frame_p90_ms", false),
                ("memory_pss_mb", false)
            ]
        );
        assert_eq!(results[3]["reason"], "measurement_unavailable");
        assert_eq!(results[4]["reason"], "measurement_unavailable");
        let error = budget_error(&results);
        assert_eq!(error.code, "budget_exceeded");
        assert_eq!(
            error.message,
            "Outside the performance budget: jank_percent, frame_p90_ms, memory_pss_mb"
        );
        assert!(check_budget(&json!({"frames_total":{"minimum":60}}), metrics).0);
        assert!(validate(&json!({"jank_percent":5}), &budget_schema(), "budget").is_err());
        assert!(
            validate(
                &json!({"jank_percent":{"most":5}}),
                &budget_schema(),
                "budget"
            )
            .is_err()
        );
    }
}
