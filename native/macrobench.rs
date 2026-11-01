//! Android Macrobenchmark and Baseline Profile support. It finds a Gradle
//! build's benchmark modules without running Gradle, runs them on a device as
//! a job, reads their JSON output into the flat metrics that profile.compare
//! and profile.budget read, generates and installs Baseline Profiles, and
//! times an installed app's startup with and without its profile.
use crate::{core::*, gradle_model as gm, jobs, perf, profiling, projects, toolchain as tc};
use regex::Regex;
use serde_json::{Map, Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
};

/// Where a benchmark run leaves its JSON and traces, relative to a module.
const OUTPUT: &str = "build/outputs/connected_android_test_additional_output";
/// Where the jobs of this module save their reports, relative to the project.
const REPORTS: &str = "build/tomacco";
/// Instrumentation argument that lets benchmarks run on an emulator.
const SUPPRESS: &str =
    "-Pandroid.testInstrumentationRunnerArguments.androidx.benchmark.suppressErrors=EMULATOR";

pub fn handle(op: &str, a: &Value) -> Result {
    match op {
        "profile.macrobenchmark" => macrobenchmark(a),
        "profile.benchmark_results" => benchmark_results(a),
        "profile.baseline_profile" => baseline_profile(a),
        "profile.baseline_rules" => baseline_rules(a),
        "profile.baseline_install" => baseline_install(a),
        "profile.baseline_compare" => baseline_compare(a),
        _ => Err(Error::new("operation_not_found", op)),
    }
}

// ---------------------------------------------------------------------------
// Reading the build

fn module_label(gradle_path: &str) -> String {
    if gradle_path.is_empty() {
        ":".into()
    } else {
        format!(":{gradle_path}")
    }
}

/// Whether a script depends on a Macrobenchmark library, by coordinates or
/// through a catalog alias.
fn uses_library(script: &str, catalog: &gm::Catalog, prefix: &str) -> bool {
    if script.contains(prefix) {
        return true;
    }
    let alias = Regex::new(r"libs\.([A-Za-z][\w.]*)").unwrap();
    alias.captures_iter(script).any(|c| {
        catalog
            .libraries
            .iter()
            .any(|l| l.module.starts_with(prefix) && c[1].starts_with(&gm::accessor(&l.key)))
    })
}

/// The Kotlin and Java sources of a module that hold benchmarks or profile
/// generators.
fn benchmark_classes(module_dir: &Path) -> Vec<Value> {
    let package = Regex::new(r"(?m)^\s*package\s+([\w.]+)").unwrap();
    let class = Regex::new(r"(?m)\bclass\s+(\w+)").unwrap();
    let mut found = Vec::new();
    let mut pending = vec![module_dir.join("src")];
    let mut seen = 0;
    while let Some(directory) = pending.pop() {
        let Ok(entries) = fs::read_dir(&directory) else {
            continue;
        };
        let mut entries: Vec<_> = entries.filter_map(|e| e.ok()).collect();
        entries.sort_by_key(|e| e.path());
        for entry in entries {
            let file = entry.path();
            if file.is_dir() {
                pending.push(file);
                continue;
            }
            if !matches!(
                file.extension().and_then(|e| e.to_str()),
                Some("kt" | "java")
            ) {
                continue;
            }
            seen += 1;
            if seen > 2000 {
                return found;
            }
            let Some(text) = gm::read_text(&file) else {
                continue;
            };
            let kind = if text.contains("MacrobenchmarkRule") {
                "benchmark"
            } else if text.contains("BaselineProfileRule") {
                "baseline_profile_generator"
            } else {
                continue;
            };
            let Some(name) = class.captures(&text).map(|c| c[1].to_string()) else {
                continue;
            };
            let name = match package.captures(&text) {
                Some(p) => format!("{}.{name}", &p[1]),
                None => name,
            };
            found.push(json!({
                "class": name,
                "kind": kind,
                "tests": text.matches("@Test").count(),
                "file": file.strip_prefix(module_dir).unwrap_or(&file),
            }));
        }
    }
    found
}

/// What a baseline profile file holds: rules by kind and flag. A rule is
/// `[HSP]*Lpackage/Class;` for a class or `[HSP]*Lpackage/Class;->method(sig)ret`
/// for a method; H marks hot, S startup and P post-startup methods.
pub fn count_rules(text: &str) -> Value {
    let (mut rules, mut methods, mut classes) = (0u64, 0u64, 0u64);
    let (mut hot, mut startup, mut post, mut wildcards) = (0u64, 0u64, 0u64, 0u64);
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let rest = line.trim_start_matches(['H', 'S', 'P']);
        let flags = &line[..line.len() - rest.len()];
        if !rest.starts_with('L') && !rest.starts_with('[') {
            continue;
        }
        rules += 1;
        if rest.contains("->") {
            methods += 1;
        } else {
            classes += 1;
        }
        hot += flags.contains('H') as u64;
        startup += flags.contains('S') as u64;
        post += flags.contains('P') as u64;
        wildcards += rest.contains('*') as u64;
    }
    json!({"rules":rules,"methods":methods,"classes":classes,"hot":hot,"startup":startup,"post_startup":post,"wildcards":wildcards})
}

/// The profile files under a module: `src/*/baseline-prof.txt` and what the
/// Baseline Profile plugin writes to `src/*/generated/baselineProfiles/`.
fn profile_files(module_dir: &Path) -> Vec<Value> {
    let mut found = Vec::new();
    let Ok(sets) = fs::read_dir(module_dir.join("src")) else {
        return found;
    };
    let mut sets: Vec<_> = sets.filter_map(|e| e.ok()).map(|e| e.path()).collect();
    sets.sort();
    for set in sets {
        let name = set
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .to_string();
        for (relative, kind, generated) in [
            ("baseline-prof.txt", "baseline", false),
            (
                "generated/baselineProfiles/baseline-prof.txt",
                "baseline",
                true,
            ),
            (
                "generated/baselineProfiles/startup-prof.txt",
                "startup",
                true,
            ),
        ] {
            let file = set.join(relative);
            let Some(text) = gm::read_text(&file) else {
                continue;
            };
            let mut value = count_rules(&text);
            value["path"] = json!(file);
            value["kind"] = json!(kind);
            value["source_set"] = json!(name);
            value["generated"] = json!(generated);
            value["bytes"] = json!(text.len());
            value["modified"] = json!(
                fs::metadata(&file)
                    .and_then(|m| m.modified())
                    .ok()
                    .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                    .map_or(0.0, |d| d.as_secs_f64())
            );
            found.push(value);
        }
    }
    found
}

/// The Macrobenchmark and Baseline Profile parts of a Gradle build: modules
/// that run benchmarks on a device (`com.android.test` with
/// `androidx.benchmark:benchmark-macro`), modules that apply the Baseline
/// Profile plugin, and the profile files they hold. Null when there is none.
pub fn setup(dir: &Path) -> Value {
    let catalog = gm::catalog_file(dir)
        .and_then(|p| gm::read_text(&p))
        .map(|t| gm::parse_catalog(&t))
        .unwrap_or_default();
    let quoted = Regex::new(r#"["']([^"']+)["']"#).unwrap();
    let (mut benchmarks, mut plugin_modules, mut profiles) = (Vec::new(), Vec::new(), Vec::new());
    for (gradle_path, module_dir) in gm::modules_of(dir) {
        let Some(script) = gm::build_file(&module_dir).and_then(|p| gm::read_text(&p)) else {
            continue;
        };
        // A root build declares plugins for its modules with `apply false`.
        let script = gm::without_apply_false(&gm::strip_comments(&script));
        let plugin = gm::applies(
            &script,
            &catalog,
            &["androidx.baselineprofile"],
            &["androidxbaselineprofile", "baselineprofile"],
        );
        let test_module = gm::applies(&script, &catalog, &["com.android.test"], &["androidtest"]);
        let module = module_label(&gradle_path);
        if test_module
            && (uses_library(&script, &catalog, "androidx.benchmark:benchmark-macro") || plugin)
        {
            let build_types: Vec<String> = gm::blocks(&script, "buildTypes")
                .into_iter()
                .flat_map(gm::children)
                .map(|(name, _)| name)
                .collect();
            let variant = if plugin {
                "BenchmarkRelease".to_string()
            } else if build_types.iter().any(|t| t == "benchmark") {
                "Benchmark".to_string()
            } else {
                String::new()
            };
            let target = gm::property(&script, &["targetProjectPath"])
                .and_then(|v| quoted.captures(&v).map(|c| c[1].to_string()));
            benchmarks.push(json!({
                "module": module,
                "target_project": target,
                "baseline_profile_plugin": plugin,
                "macrobenchmark_library": uses_library(&script, &catalog, "androidx.benchmark:benchmark-macro"),
                "build_types": build_types,
                "variant": variant,
                "task": if variant.is_empty() { "connectedCheck".to_string() } else { format!("connected{variant}AndroidTest") },
                "classes": benchmark_classes(&module_dir),
            }));
        } else if plugin {
            let task_module = if gradle_path.is_empty() {
                String::new()
            } else {
                format!(":{gradle_path}")
            };
            plugin_modules.push(json!({
                "module": module,
                "generate_task": format!("{task_module}:generateBaselineProfile"),
                "profile_installer": uses_library(&script, &catalog, "androidx.profileinstaller:profileinstaller"),
            }));
        }
        for mut file in profile_files(&module_dir) {
            file["module"] = json!(module);
            profiles.push(file);
        }
    }
    if benchmarks.is_empty() && plugin_modules.is_empty() && profiles.is_empty() {
        return Value::Null;
    }
    json!({
        "benchmark_modules": benchmarks,
        "baseline_profile_modules": plugin_modules,
        "profiles": profiles,
        "read_from": "build files and sources, without running Gradle",
    })
}

// ---------------------------------------------------------------------------
// Benchmark results

fn snake(name: &str) -> String {
    let mut out = String::new();
    for (index, ch) in name.chars().enumerate() {
        if ch.is_ascii_uppercase() {
            if index > 0 {
                out.push('_');
            }
            out.push(ch.to_ascii_lowercase());
        } else if ch.is_ascii_alphanumeric() {
            out.push(ch);
        } else if !out.ends_with('_') {
            out.push('_');
        }
    }
    out.trim_matches('_').to_string()
}

fn finite(value: &Value) -> Value {
    match value.as_f64().filter(|v| v.is_finite()) {
        Some(v) => json!((v * 1000.0).round() / 1000.0),
        None => Value::Null,
    }
}

/// The metrics of one benchmark JSON file written by AndroidX Macrobenchmark,
/// flat as `<test>_<metric>_<statistic>`: `minimum`, `median` and `maximum`
/// for a metric measured once per iteration (`timeToInitialDisplayMs` gives
/// `time_to_initial_display_ms`), and `p50`, `p90`, `p95` and `p99` for a
/// sampled one such as `frameDurationCpuMs`. A test name shared by two classes
/// is prefixed with its class.
pub fn parse_benchmarks(document: &Value, directory: &Path) -> (Map<String, Value>, Vec<Value>) {
    let mut metrics = Map::new();
    let mut benchmarks = Vec::new();
    let list = document["benchmarks"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    for entry in &list {
        let name = s(entry, "name");
        let class = s(entry, "className");
        let simple = class.rsplit('.').next().unwrap_or(class);
        let shared = list.iter().filter(|e| s(e, "name") == name).count() > 1;
        let test = snake_test(&if shared {
            format!("{simple}_{name}")
        } else {
            name.to_string()
        });
        let mut measured = Map::new();
        for (metric, values) in entry["metrics"].as_object().into_iter().flatten() {
            let key = snake(metric);
            for statistic in ["minimum", "median", "maximum"] {
                metrics.insert(
                    format!("{test}_{key}_{statistic}"),
                    finite(&values[statistic]),
                );
            }
            measured.insert(
                metric.clone(),
                json!({"minimum":finite(&values["minimum"]),"median":finite(&values["median"]),"maximum":finite(&values["maximum"]),"runs":values["runs"]}),
            );
        }
        let mut sampled = Map::new();
        for (metric, values) in entry["sampledMetrics"].as_object().into_iter().flatten() {
            let key = snake(metric);
            for statistic in ["P50", "P90", "P95", "P99"] {
                metrics.insert(
                    format!("{test}_{key}_{}", statistic.to_lowercase()),
                    finite(&values[statistic]),
                );
            }
            sampled.insert(
                metric.clone(),
                json!({"p50":finite(&values["P50"]),"p90":finite(&values["P90"]),"p95":finite(&values["P95"]),"p99":finite(&values["P99"])}),
            );
        }
        let mut traces = Vec::new();
        for output in entry["profilerOutputs"].as_array().into_iter().flatten() {
            let file = directory.join(s(output, "filename"));
            if s(output, "filename").is_empty()
                || s(output, "filename").contains("..")
                || s(output, "filename").starts_with('/')
            {
                continue;
            }
            traces.push(json!({
                "type": output["type"],
                "label": output["label"],
                "path": file,
                "bytes": fs::metadata(&file).ok().map(|m| m.len()),
            }));
        }
        benchmarks.push(json!({
            "name": name,
            "class": class,
            "params": entry["params"],
            "metric": test,
            "metrics": measured,
            "sampled_metrics": sampled,
            "warmup_iterations": entry["warmupIterations"],
            "repeat_iterations": entry["repeatIterations"],
            "total_run_time_ms": entry["totalRunTimeNs"].as_f64().map(|n| (n / 1e6).round()),
            "traces": traces,
        }));
    }
    (metrics, benchmarks)
}

fn snake_test(name: &str) -> String {
    let cleaned: String = name
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '_' })
        .collect();
    cleaned.trim_matches('_').to_string()
}

/// Benchmark JSON files under the given module directories, newest first.
fn result_files(directories: &[PathBuf], since: f64) -> Result<Vec<PathBuf>> {
    let mut files = Vec::new();
    for directory in directories {
        let pattern = format!(
            "{}/{OUTPUT}/**/*benchmarkData.json",
            glob::Pattern::escape(&directory.to_string_lossy())
        );
        for file in glob_paths(&pattern)? {
            let modified = fs::metadata(&file)
                .and_then(|m| m.modified())
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map_or(0.0, |d| d.as_secs_f64());
            if modified >= since {
                files.push((modified, file));
            }
        }
    }
    files.sort_by(|x, y| y.0.total_cmp(&x.0));
    Ok(files.into_iter().map(|(_, f)| f).collect())
}

/// The application ID a benchmark module measures, from the build files.
fn target_application(project: &Value, target: &str) -> String {
    let apps = project["android"]["app_modules"].as_array();
    let target = target.trim_start_matches(':');
    let by_target = apps.and_then(|apps| {
        apps.iter()
            .find(|m| s(m, "module").trim_start_matches(':') == target)
    });
    by_target
        .and_then(|m| m["application_id"].as_str())
        .or_else(|| {
            project["android"]["application_ids"]
                .as_array()
                .filter(|ids| ids.len() == 1)
                .and_then(|ids| ids[0].as_str())
        })
        .unwrap_or_default()
        .to_string()
}

/// Reads the benchmark JSON a Macrobenchmark run wrote into the flat metrics
/// of a performance report.
pub fn benchmark_results(a: &Value) -> Result {
    let mut since = a["since"].as_f64().unwrap_or(0.0);
    if !s(a, "job_id").is_empty() {
        since = since.max(
            jobs::get(s(a, "job_id"))?["created"]
                .as_f64()
                .unwrap_or(0.0)
                - 2.0,
        );
    }
    let mut bundle_id = s(a, "bundle_id").to_string();
    let candidates = if !s(a, "file").is_empty() {
        vec![path(s(a, "file"), false, true)?]
    } else {
        if s(a, "path").is_empty() {
            return Err(Error::new(
                "invalid_argument",
                "Give the project path, or file, the benchmark JSON",
            ));
        }
        let project = projects::inspect(s(a, "path"))?;
        let dir = PathBuf::from(s(&project, "gradle_directory"));
        let modules = gm::modules_of(&dir);
        let wanted = s(a, "module").trim_start_matches(':');
        let directories: Vec<PathBuf> = modules
            .iter()
            .filter(|(name, _)| wanted.is_empty() || name == wanted)
            .map(|(_, d)| d.clone())
            .collect();
        if directories.is_empty() {
            return Err(Error::new(
                "benchmark_module_missing",
                format!("The build has no module :{wanted}"),
            )
            .remedy("Name a module from project.inspect's performance.benchmark_modules"));
        }
        if bundle_id.is_empty() {
            let setup = setup(&dir);
            let target = setup["benchmark_modules"]
                .as_array()
                .into_iter()
                .flatten()
                .find(|m| wanted.is_empty() || s(m, "module").trim_start_matches(':') == wanted)
                .map(|m| s(m, "target_project").to_string())
                .unwrap_or_default();
            bundle_id = target_application(&project, &target);
        }
        let files = result_files(&directories, since)?;
        if files.is_empty() {
            return Err(Error::new(
                "benchmark_results_missing",
                format!(
                    "No benchmark JSON under {OUTPUT} of {}{}",
                    s(&project, "path"),
                    if since > 0.0 { " from this run" } else { "" }
                ),
            )
            .remedy("Run profile.macrobenchmark first, then read its results with job_id"));
        }
        files
    };
    let file = &candidates[0];
    let document = read_json(file, None)?;
    if !document["benchmarks"].is_array() {
        return Err(Error::new(
            "invalid_report",
            format!("{} is not a Macrobenchmark result file", file.display()),
        ));
    }
    let directory = file.parent().unwrap_or(Path::new("."));
    let (metrics, mut benchmarks) = parse_benchmarks(&document, directory);
    if b(a, "analyze") {
        if bundle_id.is_empty() {
            return Err(Error::new(
                "invalid_argument",
                "analyze needs bundle_id: the build files name no single app",
            ));
        }
        for benchmark in &mut benchmarks {
            // The first iteration's trace stands for the benchmark.
            let Some(trace) = benchmark["traces"]
                .as_array()
                .and_then(|t| t.iter().find(|t| t["bytes"].is_u64()))
                .map(|t| s(t, "path").to_string())
            else {
                continue;
            };
            benchmark["trace_analysis"] =
                match profiling::analyze(&json!({"path":trace,"bundle_id":bundle_id})) {
                    Ok(value) => json!({"trace":trace,"metrics":value["metrics"]}),
                    Err(error) => json!({"trace":trace,"error":error.value()}),
                };
        }
    }
    let model = document["context"]["build"]["model"]
        .as_str()
        .unwrap_or_default();
    let device = if s(a, "device").is_empty() {
        model
    } else {
        s(a, "device")
    };
    let mut value = perf::report("macrobenchmark", device, &bundle_id, metrics);
    value["source"] = json!(file);
    value["other_files"] = json!(candidates[1..].to_vec());
    value["device_build"] = json!({
        "model": document["context"]["build"]["model"],
        "fingerprint": document["context"]["build"]["fingerprint"],
        "sdk": document["context"]["build"]["version"]["sdk"],
        "cpu_cores": document["context"]["cpuCoreCount"],
        "compilation_mode": document["context"]["compilationMode"],
    });
    value["benchmarks"] = json!(benchmarks);
    perf::finish(a, value)
}

// ---------------------------------------------------------------------------
// Jobs

struct Target {
    project: Value,
    directory: PathBuf,
    root: String,
    setup: Value,
}

fn target(a: &Value) -> Result<Target> {
    let project = projects::inspect(s(a, "path"))?;
    if project["gradle_directory"].is_null() {
        return Err(Error::new(
            "action_unsupported",
            "Macrobenchmark and Baseline Profiles are Gradle features; this project has no Gradle build",
        )
        .remedy("Open the Android project, or its android/ directory"));
    }
    let directory = PathBuf::from(s(&project, "gradle_directory"));
    let setup = setup(&directory);
    Ok(Target {
        root: s(&project, "path").to_string(),
        project,
        directory,
        setup,
    })
}

fn pick<'a>(list: &'a Value, wanted: &str, missing: Error) -> Result<&'a Value> {
    let modules = list.as_array().cloned().unwrap_or_default();
    let wanted = wanted.trim_start_matches(':');
    if !wanted.is_empty() {
        return list
            .as_array()
            .into_iter()
            .flatten()
            .find(|m| s(m, "module").trim_start_matches(':') == wanted)
            .ok_or_else(|| {
                missing.details(json!({"modules":modules.iter().map(|m| m["module"].clone()).collect::<Vec<_>>()}))
            });
    }
    match modules.len() {
        0 => Err(missing),
        1 => Ok(&list[0]),
        _ => Err(Error::new(
            "benchmark_module_ambiguous",
            "The build has several such modules; name one with module",
        )
        .details(
            json!({"modules":modules.iter().map(|m| m["module"].clone()).collect::<Vec<_>>()}),
        )),
    }
}

fn cap(value: &str) -> String {
    let mut chars = value.chars();
    chars
        .next()
        .map(|c| c.to_uppercase().collect::<String>() + chars.as_str())
        .unwrap_or_default()
}

fn device_env(a: &Value) -> Result<(String, Value)> {
    let device = s(a, "device");
    if device.is_empty() {
        return Err(
            Error::new("device_required", "Name the device the benchmark runs on")
                .remedy("Pass device, a serial from device.list"),
        );
    }
    identifier(device, "device identifier", None)?;
    let mut env = serde_json::to_value(tc::environment()?)?;
    env["ANDROID_SERIAL"] = json!(device);
    Ok((device.to_string(), env))
}

/// Gradle options that let benchmarks run on an emulator, unless refused.
fn emulator_options(a: &Value, device: &str) -> Vec<String> {
    let allow = a["allow_emulator"]
        .as_bool()
        .unwrap_or_else(|| device.starts_with("emulator-"));
    if allow {
        vec![SUPPRESS.to_string()]
    } else {
        Vec::new()
    }
}

/// Runs a benchmark module's tests on a device as a job and ends by reading
/// their JSON into a performance report.
pub fn macrobenchmark(a: &Value) -> Result {
    let t = target(a)?;
    let missing = Error::new(
        "benchmark_module_missing",
        "No Macrobenchmark module in this build",
    )
    .remedy("Add a com.android.test module that depends on androidx.benchmark:benchmark-macro-junit4, or create the app with project.create kind compose and performance:true");
    let module = pick(&t.setup["benchmark_modules"], s(a, "module"), missing)?;
    let (device, env) = device_env(a)?;
    let task = if s(a, "variant").is_empty() {
        s(module, "task").to_string()
    } else {
        identifier(s(a, "variant"), "variant", Some("[A-Za-z][A-Za-z0-9]*"))?;
        format!("connected{}AndroidTest", cap(s(a, "variant")))
    };
    let mut args = projects::gradle_command(&t.project)?;
    args.push("--console=plain".into());
    args.extend(emulator_options(a, &device));
    if !s(a, "class").is_empty() {
        identifier(s(a, "class"), "class", Some(r"[A-Za-z0-9_.$#]+"))?;
        args.push(format!(
            "-Pandroid.testInstrumentationRunnerArguments.class={}",
            s(a, "class")
        ));
    }
    args.push(format!("{}:{task}", s(module, "module")));
    let report = if s(a, "report").is_empty() {
        Path::new(&t.root).join(REPORTS).join("macrobenchmark.json")
    } else {
        output_path(s(a, "report"))?
    };
    let read =
        json!({"path":t.root,"module":s(module,"module"),"since":now() - 1.0,"report":report});
    let steps = json!([
        {"argv":args},
        {"argv":[own_executable()?,"call","profile.benchmark_results",read.to_string()]}
    ]);
    let artifacts = [
        format!("**/{OUTPUT}/**/*benchmarkData.json"),
        format!("**/{OUTPUT}/**/*.perfetto-trace"),
        "**/build/outputs/androidTest-results/**/*.xml".to_string(),
    ];
    let spec = json!({
        "project": t.project["path"], "cwd": t.directory, "steps": steps, "env": env,
        "artifacts": artifacts,
        "always_artifacts": ["build/outputs/androidTest-results/**/*.xml", report],
        "cleanup": [projects::test_report_cleanup(Path::new(&t.root), now() - 2.0)?],
        "exclusive": t.root, "single_active": true, "resource": "build",
        "timeout": a["timeout"].as_u64().unwrap_or(3600),
        "operation_key": {"operation":"profile.macrobenchmark","arguments":a,"project":t.directory},
    });
    jobs::submit(
        &format!(
            "{} · macrobenchmark · {}",
            s(&t.project, "name"),
            s(module, "module")
        ),
        spec,
    )
}

/// Generates a Baseline Profile with the Baseline Profile Gradle plugin on a
/// device and ends by listing the profile files and their rule counts.
pub fn baseline_profile(a: &Value) -> Result {
    let t = target(a)?;
    let missing = Error::new(
        "baseline_plugin_missing",
        "No module applies the androidx.baselineprofile plugin",
    )
    .remedy("Apply the androidx.baselineprofile plugin to the app module and add a producer module, or create the app with project.create kind compose and performance:true");
    let module = pick(
        &t.setup["baseline_profile_modules"],
        s(a, "module"),
        missing,
    )?;
    let (device, env) = device_env(a)?;
    let task = if s(a, "variant").is_empty() {
        "generateBaselineProfile".to_string()
    } else {
        identifier(s(a, "variant"), "variant", Some("[A-Za-z][A-Za-z0-9]*"))?;
        format!("generate{}BaselineProfile", cap(s(a, "variant")))
    };
    let mut args = projects::gradle_command(&t.project)?;
    args.push("--console=plain".into());
    args.extend(emulator_options(a, &device));
    args.push(format!("{}:{task}", s(module, "module")));
    let report = if s(a, "report").is_empty() {
        Path::new(&t.root)
            .join(REPORTS)
            .join("baseline-profile.json")
    } else {
        output_path(s(a, "report"))?
    };
    let read =
        json!({"path":t.root,"module":s(module,"module"),"since":now() - 1.0,"report":report});
    let spec = json!({
        "project": t.project["path"], "cwd": t.directory,
        "steps": [{"argv":args},{"argv":[own_executable()?,"call","profile.baseline_rules",read.to_string()]}],
        "env": env,
        "artifacts": ["**/src/*/generated/baselineProfiles/*.txt", format!("**/{OUTPUT}/**/*baseline-prof.txt")],
        "always_artifacts": [report],
        "exclusive": t.root, "single_active": true, "resource": "build",
        "timeout": a["timeout"].as_u64().unwrap_or(3600),
        "operation_key": {"operation":"profile.baseline_profile","arguments":a,"project":t.directory},
    });
    jobs::submit(
        &format!(
            "{} · baseline profile · {}",
            s(&t.project, "name"),
            s(module, "module")
        ),
        spec,
    )
}

// ---------------------------------------------------------------------------
// Profile files

/// The profile files of a project or one module, or of one file, with their
/// rule counts and where each would be used.
pub fn baseline_rules(a: &Value) -> Result {
    let since = a["since"].as_f64().unwrap_or(0.0);
    let mut files = Vec::new();
    let mut root = String::new();
    if !s(a, "file").is_empty() {
        let file = path(s(a, "file"), false, true)?;
        let text = gm::read_text(&file).ok_or_else(|| {
            Error::new(
                "baseline_profile_missing",
                format!("{} cannot be read", file.display()),
            )
        })?;
        let mut value = count_rules(&text);
        value["path"] = json!(file);
        value["bytes"] = json!(text.len());
        files.push(value);
    } else {
        let project = projects::inspect(s(a, "path"))?;
        let dir = PathBuf::from(s(&project, "gradle_directory"));
        root = s(&project, "path").to_string();
        let wanted = s(a, "module").trim_start_matches(':');
        for (name, directory) in gm::modules_of(&dir) {
            if !wanted.is_empty() && name != wanted {
                continue;
            }
            for mut file in profile_files(&directory) {
                file["module"] = json!(module_label(&name));
                file["fresh"] = json!(file["modified"].as_f64().unwrap_or(0.0) >= since);
                files.push(file);
            }
        }
    }
    if files.is_empty() {
        return Err(Error::new(
            "baseline_profile_missing",
            "No baseline profile file in this project",
        )
        .remedy(
            "Generate one with profile.baseline_profile, or write src/main/baseline-prof.txt",
        ));
    }
    let total = |key: &str| {
        files
            .iter()
            .map(|f| f[key].as_u64().unwrap_or(0))
            .sum::<u64>()
    };
    let value = json!({
        "version": 1,
        "kind": "baseline_rules",
        "project": root,
        "files": files,
        "totals": {"rules":total("rules"),"methods":total("methods"),"classes":total("classes")},
        "note": "AGP packages src/<variant>/baseline-prof.txt into the release build; the Baseline Profile plugin merges the profile it generated into src/<variant>/generated/baselineProfiles/ itself.",
        "created": now(),
    });
    perf::finish(a, value)
}

/// Copies a generated profile to `src/main/baseline-prof.txt` of an app
/// module, where AGP packages it without the plugin.
pub fn baseline_install(a: &Value) -> Result {
    let project = projects::inspect(s(a, "path"))?;
    let dir = PathBuf::from(s(&project, "gradle_directory"));
    let wanted = s(a, "module").trim_start_matches(':');
    let modules: Vec<(String, PathBuf)> = gm::modules_of(&dir)
        .into_iter()
        .filter(|(name, _)| {
            if wanted.is_empty() {
                project["android"]["app_modules"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .any(|m| s(m, "module").trim_start_matches(':') == name)
            } else {
                name == wanted
            }
        })
        .collect();
    let (name, module_dir) = match modules.as_slice() {
        [one] => one.clone(),
        [] => {
            return Err(Error::new(
                "benchmark_module_missing",
                "No such app module in this build",
            )
            .remedy("Name the app module with module (project.inspect android.app_modules)"));
        }
        _ => {
            return Err(Error::new(
                "benchmark_module_ambiguous",
                "The build has several app modules; name one with module",
            ));
        }
    };
    let source = if s(a, "source").is_empty() {
        profile_files(&module_dir)
            .into_iter()
            .filter(|f| f["generated"] == true && f["kind"] == "baseline")
            .max_by(|x, y| {
                x["modified"]
                    .as_f64()
                    .partial_cmp(&y["modified"].as_f64())
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .map(|f| PathBuf::from(s(&f, "path")))
            .ok_or_else(|| {
                Error::new(
                    "baseline_profile_missing",
                    "The module has no generated baseline profile",
                )
                .remedy("Generate one with profile.baseline_profile, or name a file with source")
            })?
    } else {
        path(s(a, "source"), false, true)?
    };
    let relative = if s(a, "destination").is_empty() {
        "src/main/baseline-prof.txt"
    } else {
        s(a, "destination")
    };
    if relative.starts_with('/')
        || relative.split('/').any(|p| p == ".." || p.is_empty())
        || !relative.ends_with(".txt")
    {
        return Err(Error::new(
            "invalid_argument",
            "destination is a .txt path inside the module, such as src/main/baseline-prof.txt",
        ));
    }
    let destination = module_dir.join(relative);
    let text = fs::read(&source)?;
    let rules = count_rules(&String::from_utf8_lossy(&text));
    if rules["rules"].as_u64().unwrap_or(0) == 0 {
        return Err(Error::new(
            "baseline_profile_missing",
            format!("{} holds no baseline profile rules", source.display()),
        ));
    }
    let existing = fs::read(&destination).ok();
    let unchanged = existing.as_deref() == Some(text.as_slice());
    if existing.is_some() && !unchanged && !b(a, "overwrite") {
        return Err(Error::new(
            "baseline_profile_exists",
            format!("{} exists and differs", destination.display()),
        )
        .remedy("Pass overwrite:true to replace it; the old file is kept beside it as .bak"));
    }
    let mut backup = Value::Null;
    if !unchanged {
        if existing.is_some() {
            let old = destination.with_extension("txt.bak");
            fs::copy(&destination, &old)?;
            backup = json!(old);
        }
        fs::create_dir_all(destination.parent().unwrap())?;
        atomic_write(&destination, &text)?;
    }
    Ok(json!({
        "module": module_label(&name),
        "source": source,
        "destination": destination,
        "installed": !unchanged,
        "unchanged": unchanged,
        "backup": backup,
        "rules": rules,
    }))
}

// ---------------------------------------------------------------------------
// Startup with and without the profile

/// The compiler filter the package manager reports for an app, if it names one.
pub fn compiler_filter(dexopt: &str) -> Option<String> {
    Regex::new(r"\[status=([\w-]+)\]")
        .unwrap()
        .captures(dexopt)
        .map(|c| c[1].to_string())
}

fn run_shell(device: &str, args: &[&str]) -> Result<String> {
    perf::shell_out(device, args, 120)
}

/// Whether the app's ProfileInstallReceiver reported success: result code 1
/// (installed) or 2 (already installed).
pub fn profile_install_result(output: &str) -> Option<i64> {
    Regex::new(r"result=(-?\d+)")
        .unwrap()
        .captures(output)
        .and_then(|c| c[1].parse().ok())
}

/// Times cold starts of an installed app twice: compiled with the `verify`
/// filter, which ignores the profile, then with the profile installed by
/// androidx.profileinstaller and compiled with `speed-profile`; and compares them.
pub fn baseline_compare(a: &Value) -> Result {
    let device = identifier(s(a, "device"), "device", None)?;
    let id = perf::bundle(s(a, "bundle_id"))?;
    let runs = a["runs"].as_u64().unwrap_or(5).clamp(1, 30);
    let startup = |label: &str| -> Result<Value> {
        perf::startup(&json!({"device":device,"bundle_id":id,"mode":"cold","runs":runs}))
            .map_err(|e| Error::new(&e.code, format!("{label}: {}", e.message)))
    };
    run_shell(device, &["am", "force-stop", id])?;
    let compile = |mode: &str| -> Result<()> {
        let output = run_shell(device, &["cmd", "package", "compile", "-f", "-m", mode, id])?;
        if !output.contains("Success") {
            return Err(Error::new(
                "compile_failed",
                format!("cmd package compile -m {mode} answered: {}", output.trim()),
            ));
        }
        Ok(())
    };
    compile("verify")?;
    let without_state = package_filter(device, id)?;
    let without = startup("without profile")?;
    let receiver = format!("{id}/androidx.profileinstaller.ProfileInstallReceiver");
    let installed = run_shell(
        device,
        &[
            "am",
            "broadcast",
            "-a",
            "androidx.profileinstaller.action.INSTALL_PROFILE",
            "-n",
            &receiver,
        ],
    )?;
    let code = profile_install_result(&installed);
    if !matches!(code, Some(1) | Some(2)) {
        return Err(Error::new(
            "profile_install_failed",
            format!(
                "The app did not install its baseline profile (receiver answered {})",
                code.map_or("nothing".to_string(), |c| c.to_string())
            ),
        )
        .details(json!({"output":installed}))
        .remedy("Install a release build that packages a baseline profile and depends on androidx.profileinstaller:profileinstaller"));
    }
    compile("speed-profile")?;
    let with_state = package_filter(device, id)?;
    let with = startup("with profile")?;
    let key = "startup_cold_median_ms";
    let before = without["metrics"][key].as_f64();
    let after = with["metrics"][key].as_f64();
    let mut metrics = Map::new();
    for (suffix, report) in [("without_profile", &without), ("with_profile", &with)] {
        for statistic in ["median", "p90", "min", "max", "mean"] {
            metrics.insert(
                format!("startup_cold_{statistic}_ms_{suffix}"),
                report["metrics"][format!("startup_cold_{statistic}_ms")].clone(),
            );
        }
    }
    if let (Some(before), Some(after)) = (before, after) {
        metrics.insert(
            "improvement_ms".into(),
            json!(((before - after) * 1000.0).round() / 1000.0),
        );
        if before != 0.0 {
            metrics.insert(
                "improvement_percent".into(),
                json!(((before - after) / before * 100.0 * 1000.0).round() / 1000.0),
            );
        }
    }
    let mut value = perf::report("baseline_compare", device, id, metrics);
    value["runs"] = json!(runs);
    value["profile_install_result"] = json!(code);
    value["without_profile"] = json!({"compiler_filter":without_state,"startup":without});
    value["with_profile"] = json!({"compiler_filter":with_state,"startup":with});
    value["comparison"] = profiling::compare_reports(&without, &with, &Value::Null)?;
    value["note"] = json!(
        "Both sets are cold starts timed by am start -W. The app was compiled with the verify filter for the first and with speed-profile after androidx.profileinstaller installed its profile for the second."
    );
    perf::finish(a, value)
}

/// The compiler filter `dumpsys package` reports for one package.
fn package_filter(device: &str, id: &str) -> Result<Option<String>> {
    let output = run_shell(device, &["dumpsys", "package", id])?;
    Ok(compiler_filter(&output))
}

#[cfg(test)]
mod tests {
    use super::*;

    const FIXTURE: &str =
        include_str!("../tests/fixtures/macrobenchmark/startup-benchmarkData.json");

    fn write(root: &Path, relative: &str, text: &str) {
        let file = root.join(relative);
        fs::create_dir_all(file.parent().unwrap()).unwrap();
        fs::write(file, text).unwrap();
    }

    #[test]
    fn recorded_benchmark_json_gives_flat_metrics_and_traces() {
        let document: Value = serde_json::from_str(FIXTURE).unwrap();
        let (metrics, benchmarks) = parse_benchmarks(&document, Path::new("/out"));
        assert_eq!(benchmarks.len(), 2);
        assert_eq!(
            metrics["startupCompilationNone_time_to_initial_display_ms_median"],
            json!(382.526)
        );
        assert_eq!(
            metrics["startupCompilationBaselineProfiles_time_to_initial_display_ms_minimum"],
            json!(373.514)
        );
        assert_eq!(metrics.len(), 6);
        let traces = benchmarks[0]["traces"].as_array().unwrap();
        assert_eq!(traces.len(), 5);
        assert!(
            s(&traces[0], "path")
                .starts_with("/out/StartupBenchmarks_startupCompilationBaselineProfiles_iter000")
        );
        assert_eq!(benchmarks[0]["repeat_iterations"], json!(5));
    }

    #[test]
    fn sampled_metrics_give_percentiles_and_shared_test_names_get_their_class() {
        let document = json!({"benchmarks":[
            {"name":"scroll","className":"a.ListBenchmark","metrics":{"frameOverrunMs":{"minimum":-3.5,"maximum":9.0,"median":-1.25,"runs":[]}},
             "sampledMetrics":{"frameDurationCpuMs":{"P50":4.0,"P90":9.5,"P95":12.0,"P99":30.25,"runs":[]}},"profilerOutputs":[
                {"type":"PerfettoTrace","label":"x","filename":"../../etc/passwd"}]},
            {"name":"scroll","className":"a.GridBenchmark","metrics":{},"sampledMetrics":{}}]});
        let (metrics, benchmarks) = parse_benchmarks(&document, Path::new("/out"));
        assert_eq!(
            metrics["ListBenchmark_scroll_frame_duration_cpu_ms_p90"],
            json!(9.5)
        );
        assert_eq!(
            metrics["ListBenchmark_scroll_frame_overrun_ms_minimum"],
            json!(-3.5)
        );
        assert_eq!(
            metrics["ListBenchmark_scroll_frame_duration_cpu_ms_p99"],
            json!(30.25)
        );
        // A trace file name that leaves the output directory is dropped.
        assert_eq!(benchmarks[0]["traces"], json!([]));
        assert_eq!(benchmarks[1]["metric"], json!("GridBenchmark_scroll"));
    }

    #[test]
    fn metric_names_become_snake_case() {
        assert_eq!(
            snake("timeToInitialDisplayMs"),
            "time_to_initial_display_ms"
        );
        assert_eq!(snake("frameDurationCpuMs"), "frame_duration_cpu_ms");
        assert_eq!(snake_test("scroll[mode=cold]"), "scroll_mode_cold");
    }

    #[test]
    fn profile_rules_are_counted_by_kind_and_flag() {
        let text = "# generated\nHSPLcom/example/Main;->onCreate(Landroid/os/Bundle;)V\nPLcom/example/Main;->helper()V\nLcom/example/Main;\nHSLcom/example/**;\n\nLcom/example/Other;->run()V\nnot a rule\n";
        let counts = count_rules(text);
        assert_eq!(counts["rules"], json!(5));
        assert_eq!(counts["methods"], json!(3));
        assert_eq!(counts["classes"], json!(2));
        assert_eq!(counts["hot"], json!(2));
        assert_eq!(counts["startup"], json!(2));
        assert_eq!(counts["post_startup"], json!(2));
        assert_eq!(counts["wildcards"], json!(1));
    }

    #[test]
    fn the_build_files_name_the_benchmark_module_and_the_plugin_consumer() {
        let root = tempfile::tempdir().unwrap();
        let r = root.path();
        write(
            r,
            "settings.gradle.kts",
            "include(\":app\")\ninclude(\":baselineprofile\")\n",
        );
        write(
            r,
            "gradle/libs.versions.toml",
            "[versions]\nbenchmark = \"1.5.0\"\n[libraries]\nandroidx-benchmark-macro-junit4 = { group = \"androidx.benchmark\", name = \"benchmark-macro-junit4\", version.ref = \"benchmark\" }\nandroidx-profileinstaller = { group = \"androidx.profileinstaller\", name = \"profileinstaller\", version = \"1.4.1\" }\n[plugins]\nandroid-test = { id = \"com.android.test\", version = \"9.1.1\" }\nandroid-application = { id = \"com.android.application\", version = \"9.1.1\" }\nandroidx-baselineprofile = { id = \"androidx.baselineprofile\", version = \"1.5.0\" }\n",
        );
        write(
            r,
            "app/build.gradle.kts",
            "plugins { alias(libs.plugins.android.application); alias(libs.plugins.androidx.baselineprofile) }\ndependencies { implementation(libs.androidx.profileinstaller); \"baselineProfile\"(project(\":baselineprofile\")) }\n",
        );
        write(
            r,
            "app/src/main/generated/baselineProfiles/baseline-prof.txt",
            "HSPLa/B;->c()V\nLa/B;\n",
        );
        write(
            r,
            "baselineprofile/build.gradle.kts",
            "plugins { alias(libs.plugins.android.test); alias(libs.plugins.androidx.baselineprofile) }\nandroid { targetProjectPath = \":app\" }\ndependencies { implementation(libs.androidx.benchmark.macro.junit4) }\n",
        );
        write(
            r,
            "baselineprofile/src/main/java/x/StartupBenchmarks.kt",
            "package x\nclass StartupBenchmarks {\n  @get:Rule val rule = MacrobenchmarkRule()\n  @Test fun a() {}\n  @Test fun b() {}\n}\n",
        );
        write(
            r,
            "baselineprofile/src/main/java/x/Generator.kt",
            "package x\nclass Generator { val rule = BaselineProfileRule()\n @Test fun g() {} }\n",
        );
        write(
            r,
            "build.gradle.kts",
            "plugins {\n    alias(libs.plugins.android.test) apply false\n    alias(libs.plugins.androidx.baselineprofile) apply false\n}\n",
        );
        let found = setup(r);
        assert_eq!(found["benchmark_modules"].as_array().unwrap().len(), 1);
        assert_eq!(
            found["baseline_profile_modules"].as_array().unwrap().len(),
            1
        );
        let module = &found["benchmark_modules"][0];
        assert_eq!(module["module"], json!(":baselineprofile"));
        assert_eq!(module["target_project"], json!(":app"));
        assert_eq!(
            module["task"],
            json!("connectedBenchmarkReleaseAndroidTest")
        );
        assert_eq!(module["macrobenchmark_library"], json!(true));
        let classes: Vec<_> = module["classes"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| {
                (
                    s(c, "class").to_string(),
                    s(c, "kind").to_string(),
                    c["tests"].as_u64().unwrap(),
                )
            })
            .collect();
        assert!(classes.contains(&("x.StartupBenchmarks".into(), "benchmark".into(), 2)));
        assert!(classes.contains(&("x.Generator".into(), "baseline_profile_generator".into(), 1)));
        let consumer = &found["baseline_profile_modules"][0];
        assert_eq!(
            consumer["generate_task"],
            json!(":app:generateBaselineProfile")
        );
        assert_eq!(consumer["profile_installer"], json!(true));
        assert_eq!(found["profiles"][0]["rules"], json!(2));
        assert_eq!(found["profiles"][0]["generated"], json!(true));
    }

    #[test]
    fn a_classic_benchmark_build_type_and_a_plain_app_are_told_apart() {
        let root = tempfile::tempdir().unwrap();
        let r = root.path();
        write(r, "settings.gradle", "include ':app', ':macrobenchmark'\n");
        write(
            r,
            "app/build.gradle",
            "plugins { id 'com.android.application' }\n",
        );
        write(
            r,
            "macrobenchmark/build.gradle",
            "plugins { id 'com.android.test' }\nandroid { targetProjectPath ':app'\n buildTypes { benchmark { debuggable true } } }\ndependencies { implementation 'androidx.benchmark:benchmark-macro-junit4:1.3.0' }\n",
        );
        let found = setup(r);
        let module = &found["benchmark_modules"][0];
        assert_eq!(module["task"], json!("connectedBenchmarkAndroidTest"));
        assert_eq!(module["baseline_profile_plugin"], json!(false));
        assert_eq!(module["target_project"], json!(":app"));
        assert_eq!(found["baseline_profile_modules"], json!([]));
        // A build without either is not reported.
        let plain = tempfile::tempdir().unwrap();
        write(plain.path(), "settings.gradle", "include ':app'\n");
        write(
            plain.path(),
            "app/build.gradle",
            "plugins { id 'com.android.application' }\n",
        );
        assert!(setup(plain.path()).is_null());
    }

    #[test]
    fn results_are_found_by_age_and_the_newest_file_wins() {
        let root = tempfile::tempdir().unwrap();
        let r = root.path();
        write(
            r,
            &format!("{OUTPUT}/benchmarkRelease/connected/pixel/x-benchmarkData.json"),
            FIXTURE,
        );
        let files = result_files(&[r.to_path_buf()], 0.0).unwrap();
        assert_eq!(files.len(), 1);
        assert!(
            result_files(&[r.to_path_buf()], now() + 100.0)
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn installing_a_profile_copies_it_and_keeps_a_changed_file_until_overwrite() {
        let root = tempfile::tempdir().unwrap();
        let r = root.path();
        write(r, "settings.gradle", "include ':app'\n");
        write(
            r,
            "app/build.gradle",
            "plugins { id 'com.android.application' }\nandroid { defaultConfig { applicationId 'a.b' } }\n",
        );
        write(
            r,
            "app/src/main/generated/baselineProfiles/baseline-prof.txt",
            "HSPLa/B;->c()V\n",
        );
        let call = |extra: Value| {
            let mut a = json!({"path":r});
            for (k, v) in extra.as_object().unwrap() {
                a[k] = v.clone();
            }
            baseline_install(&a)
        };
        let first = call(json!({})).unwrap();
        assert_eq!(first["installed"], json!(true));
        assert_eq!(first["rules"]["rules"], json!(1));
        assert_eq!(call(json!({})).unwrap()["unchanged"], json!(true));
        write(r, "app/src/main/baseline-prof.txt", "Lold/One;\n");
        assert_eq!(call(json!({})).unwrap_err().code, "baseline_profile_exists");
        let replaced = call(json!({"overwrite":true})).unwrap();
        assert_eq!(replaced["installed"], json!(true));
        assert_eq!(
            fs::read_to_string(r.join("app/src/main/baseline-prof.txt.bak")).unwrap(),
            "Lold/One;\n"
        );
        assert_eq!(
            call(json!({"destination":"../x.txt"})).unwrap_err().code,
            "invalid_argument"
        );
    }

    #[test]
    fn profile_install_and_dexopt_output_are_read() {
        assert_eq!(
            profile_install_result("Broadcasting: Intent\nBroadcast completed: result=1\n"),
            Some(1)
        );
        assert_eq!(
            profile_install_result("Broadcast completed: result=0"),
            Some(0)
        );
        assert_eq!(
            profile_install_result("Error: Activity class does not exist"),
            None
        );
        assert_eq!(
            compiler_filter(
                "Dexopt state:\n  [dev.example]\n    path: /data/app/base.apk\n      x86_64: [status=speed-profile] [reason=cmdline]\n"
            ),
            Some("speed-profile".into())
        );
        assert_eq!(compiler_filter("nothing"), None);
    }
}
