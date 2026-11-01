//! One self-contained HTML file per test run: a testing.run report
//! directory's scenarios, steps, screenshots, difference images, crashes
//! and timings, or the JUnit and Dart JSON results of a project's test job
//! with each test case and failure. Styles are inline and images are
//! embedded as data URIs, so the file opens anywhere without other files;
//! it has no scripts.
use crate::{core::*, test_history, test_results};
use base64::{Engine as _, engine::general_purpose::STANDARD};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
};

/// Images beyond these sizes are linked by path instead of embedded.
const IMAGE_LIMIT: u64 = 8 * 1024 * 1024;
const EMBED_LIMIT: u64 = 48 * 1024 * 1024;
/// Test cases listed in a results report.
const MAX_CASES: usize = 5000;

const STYLE: &str = "body{font:14px/1.45 system-ui,sans-serif;margin:24px;color:#1d1d1f;background:#fff}\
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}h3{font-size:15px;margin:16px 0 6px}\
.meta{color:#555;margin-bottom:16px}table{border-collapse:collapse;margin:8px 0;width:100%}\
th,td{border:1px solid #ddd;padding:4px 8px;text-align:left;vertical-align:top}th{background:#f4f4f6}\
.passed{color:#11772d;font-weight:600}.failed,.error{color:#b3261e;font-weight:600}.flaky{color:#9a6700;font-weight:600}\
.skipped,.not_run{color:#666}pre{background:#f6f6f8;padding:8px;overflow:auto;max-height:360px;white-space:pre-wrap}\
details{border:1px solid #ddd;border-radius:6px;padding:6px 10px;margin:8px 0}summary{cursor:pointer;font-weight:600}\
figure{display:inline-block;margin:6px 12px 6px 0;vertical-align:top}figure img{max-width:280px;max-height:560px;border:1px solid #ccc}\
figcaption{font-size:12px;color:#555}.totals span{margin-right:16px}@media (prefers-color-scheme:dark){\
body{background:#161618;color:#eee}th{background:#26262a}th,td,details{border-color:#3a3a3e}pre{background:#202024}\
.meta,figcaption{color:#aaa}}";

pub fn escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
}

/// Embeds images while the budget lasts.
struct Images {
    embedded: u64,
    count: usize,
    linked: usize,
}

impl Images {
    fn figure(&mut self, file: &Path, caption: &str) -> String {
        let size = fs::metadata(file).map(|m| m.len()).unwrap_or(0);
        let caption = format!(
            "{} · {}",
            escape(caption),
            escape(&file.file_name().unwrap_or_default().to_string_lossy())
        );
        if size > 0 && size <= IMAGE_LIMIT && self.embedded + size <= EMBED_LIMIT {
            if let Ok(bytes) = fs::read(file) {
                self.embedded += size;
                self.count += 1;
                return format!(
                    "<figure><img alt=\"{caption}\" src=\"data:image/png;base64,{}\"><figcaption>{caption}</figcaption></figure>",
                    STANDARD.encode(bytes)
                );
            }
        }
        self.linked += 1;
        format!(
            "<figure><figcaption>{caption}: <a href=\"file://{}\">{}</a> (not embedded)</figcaption></figure>",
            escape(&file.to_string_lossy()),
            escape(&file.to_string_lossy())
        )
    }
}

/// Every existing PNG a step's result names, in order of appearance.
fn pngs(value: &Value, found: &mut Vec<PathBuf>) {
    match value {
        Value::String(text) if text.ends_with(".png") => {
            let file = PathBuf::from(text);
            if file.is_absolute() && file.is_file() && !found.contains(&file) {
                found.push(file);
            }
        }
        Value::Array(items) => items.iter().for_each(|v| pngs(v, found)),
        Value::Object(map) => map.values().for_each(|v| pngs(v, found)),
        _ => (),
    }
}

fn caption_of(file: &Path, baseline: Option<&Path>) -> &'static str {
    let name = file.to_string_lossy();
    if Some(file) == baseline {
        "Baseline"
    } else if name.ends_with("-diff.png") {
        "Difference"
    } else if name.ends_with("-failure.png") {
        "At the failure"
    } else {
        "Screenshot"
    }
}

fn outcome_class(outcome: &str) -> &str {
    match outcome {
        "passed" | "failed" | "error" | "flaky" | "skipped" => outcome,
        _ => "not_run",
    }
}

fn seconds(value: &Value) -> String {
    value
        .as_f64()
        .map_or_else(|| "–".into(), |v| format!("{v:.2} s"))
}

fn page(title: &str, body: &str) -> String {
    format!(
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\
<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src data: file:; style-src 'unsafe-inline'\">\
<title>{}</title><style>{STYLE}</style></head><body>{body}<p class=\"meta\">Written by Tomacco {VERSION}.</p></body></html>\n",
        escape(title)
    )
}

fn time_text(epoch: f64) -> String {
    if epoch <= 0.0 {
        return String::new();
    }
    let seconds = epoch as i64;
    let of_day = seconds.rem_euclid(86_400);
    format!(
        "{} {:02}:{:02}:{:02} UTC",
        crate::signing_health::iso(seconds),
        of_day / 3600,
        of_day / 60 % 60,
        of_day % 60
    )
}

/// The HTML of a testing.run report directory.
fn run_html(directory: &Path) -> Result<(String, Value)> {
    let report = read_json(&directory.join("report.json"), None)?;
    let spec = read_json(&directory.join("run.json"), Some(json!({})))?;
    let results = report["results"].as_array().cloned().unwrap_or_default();
    let mut images = Images {
        embedded: 0,
        count: 0,
        linked: 0,
    };
    let passed = results.iter().filter(|r| b(r, "passed")).count();
    let flaky = results.iter().filter(|r| b(r, "flaky")).count();
    let total_time: f64 = results
        .iter()
        .filter_map(|r| r["duration_seconds"].as_f64())
        .sum();
    let crashes: usize = results
        .iter()
        .map(|r| r["crashes"].as_array().map_or(0, Vec::len))
        .sum();
    let name = s(&report, "name");
    let mut body = format!(
        "<h1>{}</h1><p class=\"meta\">testing.run report · {} · {}</p><p class=\"totals\"><span class=\"{}\">{}</span><span>{} of {} passed</span><span>{} flaky</span><span>{} crashes</span><span>{:.1} s on devices</span></p>",
        escape(if name.is_empty() { "UI test run" } else { name }),
        escape(&time_text(report["created"].as_f64().unwrap_or(0.0))),
        escape(&directory.to_string_lossy()),
        if b(&report, "passed") {
            "passed"
        } else {
            "failed"
        },
        if !b(&report, "complete") {
            "Not complete"
        } else if b(&report, "passed") {
            "Passed"
        } else {
            "Failed"
        },
        passed,
        results.len(),
        flaky,
        crashes,
        total_time
    );
    body.push_str("<h2>Scenarios</h2><table><tr><th>Scenario</th><th>Device</th><th>Outcome</th><th>Attempts</th><th>Time</th><th>Error</th></tr>");
    for result in &results {
        let outcome = if b(result, "flaky") {
            "flaky"
        } else if b(result, "passed") {
            "passed"
        } else {
            "failed"
        };
        body.push_str(&format!(
            "<tr><td>{}</td><td>{}</td><td class=\"{outcome}\">{outcome}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
            escape(s(result, "scenario")),
            escape(s(result, "device")),
            result["attempts"].as_array().map_or(1, Vec::len),
            seconds(&result["duration_seconds"]),
            escape(s(&result["error"], "message"))
        ));
    }
    body.push_str("</table>");
    for result in &results {
        let scenario = &spec["scenarios"][n(result, "scenario_index") as usize];
        let failed = !b(result, "passed");
        body.push_str(&format!(
            "<details{}><summary>{} on {} · <span class=\"{}\">{}</span> · {}</summary>",
            if failed || b(result, "flaky") {
                " open"
            } else {
                ""
            },
            escape(s(result, "scenario")),
            escape(s(result, "device")),
            if failed { "failed" } else { "passed" },
            if failed { "failed" } else { "passed" },
            seconds(&result["duration_seconds"])
        ));
        if result["error"].is_object() {
            body.push_str(&format!(
                "<p class=\"failed\">{}: {}</p>",
                escape(s(&result["error"], "code")),
                escape(s(&result["error"], "message"))
            ));
        }
        body.push_str("<table><tr><th>#</th><th>Step</th><th>Label</th><th>Time</th><th>Outcome</th><th>Detail</th></tr>");
        let mut figures = String::new();
        for step in result["steps"].as_array().into_iter().flatten() {
            let index = n(step, "index") as usize;
            let error = &step["result"]["error"];
            let outcome = if b(step, "passed") {
                "passed"
            } else {
                "failed"
            };
            let mut detail = escape(s(error, "message"));
            if let Some(fraction) = step["result"]["changed_fraction"]
                .as_f64()
                .or(error["details"]["changed_fraction"].as_f64())
            {
                detail.push_str(&format!(" changed {:.4} %", fraction * 100.0));
            }
            if b(&step["result"], "new_baseline") {
                detail.push_str("new screen, no baseline yet");
            }
            body.push_str(&format!(
                "<tr><td>{index}</td><td>{}</td><td>{}</td><td>{}</td><td class=\"{outcome}\">{outcome}</td><td>{detail}</td></tr>",
                escape(s(step, "action")),
                escape(s(step, "label")),
                seconds(&step["duration_seconds"])
            ));
            let mut files = Vec::new();
            pngs(&step["images"], &mut files);
            pngs(&step["result"], &mut files);
            pngs(&step["failure_capture"], &mut files);
            let baseline = scenario["steps"][index]["baseline"]
                .as_str()
                .map(PathBuf::from)
                .filter(|p| p.is_file());
            if let Some(baseline) = &baseline {
                if !files.contains(baseline) {
                    files.insert(0, baseline.clone());
                }
            }
            if !files.is_empty() {
                figures.push_str(&format!(
                    "<h3>Step {index}: {}</h3>",
                    escape(s(step, "action"))
                ));
                for file in files {
                    figures.push_str(&images.figure(&file, caption_of(&file, baseline.as_deref())));
                }
            }
        }
        body.push_str("</table>");
        body.push_str(&figures);
        if let Some(attempts) = result["attempts"].as_array().filter(|a| a.len() > 1) {
            body.push_str("<h3>Attempts</h3><table><tr><th>Attempt</th><th>Outcome</th><th>Failed step</th><th>Time</th><th>Error</th></tr>");
            for attempt in attempts {
                let outcome = if b(attempt, "passed") {
                    "passed"
                } else {
                    "failed"
                };
                body.push_str(&format!(
                    "<tr><td>{}</td><td class=\"{outcome}\">{outcome}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                    n(attempt, "attempt"),
                    escape(&attempt["failed_step"].to_string().replace("null", "")),
                    seconds(&attempt["duration_seconds"]),
                    escape(s(&attempt["error"], "message"))
                ));
            }
            body.push_str("</table>");
        }
        let crash_list = result["crashes"].as_array().cloned().unwrap_or_default();
        if !crash_list.is_empty() {
            body.push_str("<h3>Crashes</h3>");
            for crash in &crash_list {
                body.push_str(&format!(
                    "<p class=\"failed\">{} · {} · {}</p><pre>{}</pre>",
                    escape(s(crash, "kind")),
                    escape(s(crash, "package")),
                    escape(
                        &[
                            s(crash, "exception"),
                            s(crash, "message"),
                            s(crash, "reason"),
                            s(crash, "signal")
                        ]
                        .iter()
                        .filter(|t| !t.is_empty())
                        .cloned()
                        .collect::<Vec<_>>()
                        .join(": ")
                    ),
                    escape(&match &crash["stack"] {
                        Value::Array(lines) => lines
                            .iter()
                            .map(|l| l.as_str().map_or_else(|| l.to_string(), str::to_string))
                            .collect::<Vec<_>>()
                            .join("\n"),
                        Value::String(text) => text.clone(),
                        _ => String::new(),
                    })
                ));
            }
        } else if result["crashes_error"].is_object() {
            body.push_str(&format!(
                "<p class=\"meta\">Crashes not read: {}</p>",
                escape(s(&result["crashes_error"], "message"))
            ));
        }
        if result["metrics"].as_object().is_some_and(|m| !m.is_empty()) {
            body.push_str(&format!(
                "<h3>Metrics</h3><pre>{}</pre>",
                escape(&serde_json::to_string_pretty(&result["metrics"]).unwrap_or_default())
            ));
        }
        body.push_str("</details>");
    }
    let summary = json!({"kind":"testing.run","results":results.len(),"passed":passed,"flaky":flaky,
        "crashes":crashes,"images_embedded":images.count,"images_linked":images.linked});
    Ok((
        page(&format!("{name} · Tomacco test report"), &body),
        summary,
    ))
}

/// The HTML of the JUnit and Dart JSON results under `root` since `since`.
fn results_html(root: &Path, since: f64) -> Result<(String, Value)> {
    let files = test_results::recent_files(root, since)?;
    let mut summary = test_results::Summary::default();
    let mut cases = Vec::new();
    for file in &files {
        let text = String::from_utf8_lossy(&fs::read(file)?).into_owned();
        summary.add(file, &text);
        cases.extend(test_results::cases(&text));
    }
    if summary.suites.is_empty() {
        return Err(Error::new(
            "no_test_results",
            format!("No JUnit XML or Dart JSON test results under {}", root.display()),
        )
        .remedy("Run the tests first: project.execute with action test or instrumented-test, or testing.run"));
    }
    let value = summary.value();
    let totals = &value["totals"];
    let mut images = Images {
        embedded: 0,
        count: 0,
        linked: 0,
    };
    let name = root.file_name().unwrap_or_default().to_string_lossy();
    let mut body = format!(
        "<h1>{}</h1><p class=\"meta\">Test results · {}</p><p class=\"totals\"><span class=\"{}\">{}</span><span>{} tests</span><span>{} passed</span><span>{} failed</span><span>{} errors</span><span>{} skipped</span><span>{}</span></p>",
        escape(&name),
        escape(&root.to_string_lossy()),
        if b(&value, "passed") {
            "passed"
        } else {
            "failed"
        },
        if b(&value, "passed") {
            "Passed"
        } else {
            "Failed"
        },
        n(totals, "tests"),
        n(totals, "passed"),
        n(totals, "failed"),
        n(totals, "errors"),
        n(totals, "skipped"),
        seconds(&totals["time"])
    );
    body.push_str("<h2>Suites</h2><table><tr><th>Suite</th><th>Device</th><th>Tests</th><th>Failures</th><th>Errors</th><th>Skipped</th><th>Time</th></tr>");
    for suite in &summary.suites {
        let bad = n(suite, "failures") + n(suite, "errors") > 0;
        body.push_str(&format!(
            "<tr><td{}>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
            if bad { " class=\"failed\"" } else { "" },
            escape(s(suite, "name")),
            escape(s(suite, "device")),
            n(suite, "tests"),
            n(suite, "failures"),
            n(suite, "errors"),
            n(suite, "skipped"),
            seconds(&suite["time"])
        ));
    }
    body.push_str("</table>");
    if !summary.failures.is_empty() {
        body.push_str("<h2>Failures</h2>");
        for failure in &summary.failures {
            body.push_str(&format!(
                "<details open><summary class=\"failed\">{}{}{}</summary><p>{}</p><pre>{}</pre>",
                escape(s(failure, "class")),
                if s(failure, "class").is_empty() {
                    ""
                } else {
                    "."
                },
                escape(s(failure, "name")),
                escape(s(failure, "message")),
                escape(s(failure, "details"))
            ));
            let mut files = Vec::new();
            pngs(&failure["golden"]["images"], &mut files);
            for file in files {
                let caption = file
                    .file_stem()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .rsplit('_')
                    .next()
                    .unwrap_or("")
                    .to_string();
                body.push_str(&images.figure(&file, &caption));
            }
            body.push_str("</details>");
        }
    }
    cases.sort_by(|x, y| {
        let rank = |c: &Value| match s(c, "outcome") {
            "failed" | "error" => 0,
            "flaky" => 1,
            "skipped" => 3,
            _ => 2,
        };
        rank(x).cmp(&rank(y))
    });
    body.push_str(&format!(
        "<h2>Test cases</h2><table><tr><th>Class</th><th>Test</th><th>Device</th><th>Outcome</th><th>Time</th></tr>{}</table>",
        cases
            .iter()
            .take(MAX_CASES)
            .map(|c| format!(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td class=\"{}\">{}</td><td>{}</td></tr>",
                escape(s(c, "class")),
                escape(s(c, "name")),
                escape(s(c, "device")),
                outcome_class(s(c, "outcome")),
                escape(s(c, "outcome")),
                seconds(&c["time"])
            ))
            .collect::<String>()
    ));
    if cases.len() > MAX_CASES {
        body.push_str(&format!(
            "<p class=\"meta\">{} more test cases are not listed.</p>",
            cases.len() - MAX_CASES
        ));
    }
    let sources: BTreeSet<String> = files
        .iter()
        .map(|f| f.to_string_lossy().into_owned())
        .collect();
    body.push_str(&format!(
        "<details><summary>Result files ({})</summary><pre>{}</pre></details>",
        sources.len(),
        escape(&sources.into_iter().collect::<Vec<_>>().join("\n"))
    ));
    let out = json!({"kind":"results","totals":value["totals"],"passed":value["passed"],"cases":cases.len(),
        "files":files.len(),"images_embedded":images.count,"images_linked":images.linked});
    Ok((page(&format!("{name} · Tomacco test report"), &body), out))
}

/// Writes the report for `root` to `output` (or the default place) and
/// returns what it holds with the file as an artifact.
pub fn write(root: &Path, since: f64, output: Option<PathBuf>) -> Result {
    let run = root.is_dir() && test_history::is_run_directory(root);
    let (html, mut value) = if run {
        run_html(root)?
    } else {
        results_html(root, since)?
    };
    let target = match output {
        Some(target) => target,
        None if run => root.join("report.html"),
        None if root.is_dir() => root.join(crate::projects::TEST_REPORT_HTML),
        None => root.with_file_name("test-report.html"),
    };
    atomic_write(&target, html.as_bytes())?;
    value["html"] = artifact(&target, now())?;
    Ok(value)
}

pub fn report(a: &Value) -> Result {
    let root = path(s(a, "path"), false, true)?;
    let mut since = a["since"].as_f64().unwrap_or(0.0);
    if !s(a, "job_id").is_empty() {
        let job = crate::jobs::get(s(a, "job_id"))?;
        since = since.max(job["created"].as_f64().unwrap_or(0.0) - 2.0);
    }
    let output = if s(a, "output").is_empty() {
        None
    } else {
        Some(output_path(s(a, "output"))?)
    };
    write(&root, since, output)
}

/// The cleanup of a project's test job: its HTML report and its test
/// history entries. Without results, as after a build failure, it writes
/// nothing and succeeds.
pub fn job_worker(root: &str, since: &str) -> Result<()> {
    let root = path(root, true, true)?;
    let since = since.parse::<f64>().unwrap_or(0.0);
    if test_results::recent_files(&root, since)?.is_empty() {
        println!("No test results to report");
        return Ok(());
    }
    match write(&root, since, None) {
        Ok(value) => println!("Test report: {}", s(&value["html"], "path")),
        Err(error) if error.code == "no_test_results" => println!("No test results to report"),
        Err(error) => return Err(error),
    }
    let entries = test_history::result_entries(&root, since, &root.to_string_lossy())?;
    let store = test_history::store_path(&json!({}))?;
    let added = test_history::append(&store, entries)?;
    println!("Test history: {added} entries added to {}", store.display());
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The smallest valid PNG: one transparent pixel.
    pub fn png() -> Vec<u8> {
        crate::visual::encode(&crate::visual::Image {
            width: 1,
            height: 1,
            pixels: vec![0, 0, 0, 0],
        })
        .unwrap()
    }

    #[test]
    fn a_run_report_shows_scenarios_steps_images_crashes_and_timings() {
        let directory = tempfile::tempdir().unwrap();
        let dir = directory.path();
        for name in [
            "base.png",
            "abc-s000-001.png",
            "abc-s000-001-diff.png",
            "abc-s000-failure.png",
        ] {
            fs::write(dir.join(name), png()).unwrap();
        }
        let baseline = dir.join("base.png");
        atomic_json(&dir.join("run.json"), &json!({"scenarios":[{"name":"Login <home>","steps":[
            {"action":"launch","bundle_id":"dev.example"},{"action":"assert_screen","baseline":baseline}]}]})).unwrap();
        atomic_json(&dir.join("report.json"), &json!({"name":"login","created":1_800_000_000.0,"complete":true,"passed":false,"results":[
            {"scenario":"Login <home>","scenario_index":0,"device":"emulator-5554","passed":false,"duration_seconds":4.5,
             "error":{"code":"screen_mismatch","message":"The screen does not match the baseline"},
             "steps":[{"index":0,"action":"launch","duration_seconds":1.0,"passed":true,"result":{}},
                      {"index":1,"action":"assert_screen","duration_seconds":0.6,"passed":false,
                       "result":{"error":{"code":"screen_mismatch","message":"The screen does not match the baseline",
                                 "details":{"changed_fraction":0.0125,"diff":{"path":dir.join("abc-s000-001-diff.png")}}}},
                       "images":{"screenshot":dir.join("abc-s000-001.png"),"diff":dir.join("abc-s000-001-diff.png")},
                       "failure_capture":[{"path":dir.join("abc-s000-failure.png")}]}],
             "attempts":[{"attempt":1,"passed":false,"failed_step":1},{"attempt":2,"passed":false,"failed_step":1}],
             "crashes":[{"kind":"crash","package":"dev.example","exception":"java.lang.IllegalStateException","message":"boom","stack":["at A.b(A.kt:3)"]}]}]})).unwrap();
        let value = report(&json!({"path":dir})).unwrap();
        assert_eq!(value["kind"], "testing.run");
        assert_eq!(value["crashes"], 1);
        assert_eq!(value["images_embedded"], 4);
        let html = fs::read_to_string(dir.join("report.html")).unwrap();
        assert!(html.starts_with("<!DOCTYPE html>"));
        assert!(!html.contains("<script"));
        assert!(!html.contains("src=\"http"));
        assert!(html.contains("Login &lt;home&gt;"));
        assert!(html.contains("changed 1.2500 %"));
        assert!(html.contains("Baseline · base.png"));
        assert!(html.contains("Difference · abc-s000-001-diff.png"));
        assert!(html.contains("At the failure"));
        assert!(html.contains("java.lang.IllegalStateException: boom"));
        assert!(html.contains("<h3>Attempts</h3>"));
        assert!(html.contains("data:image/png;base64,"));
        assert_eq!(
            value["html"]["path"],
            json!(dir.join("report.html").canonicalize().unwrap())
        );
    }

    #[test]
    fn a_results_report_lists_suites_cases_failures_and_goldens() {
        let project = tempfile::tempdir().unwrap();
        let results = project
            .path()
            .join("app/build/test-results/testDebugUnitTest");
        fs::create_dir_all(&results).unwrap();
        fs::write(
            results.join("TEST-a.xml"),
            r#"<testsuite name="dev.example.CounterTest" tests="2" failures="1" time="0.3"><testcase classname="dev.example.CounterTest" name="adds" time="0.1"/><testcase classname="dev.example.CounterTest" name="overflows" time="0.2"><failure message="expected 3">at CounterTest.kt:20</failure></testcase></testsuite>"#,
        )
        .unwrap();
        let value = report(&json!({"path":project.path()})).unwrap();
        assert_eq!(value["kind"], "results");
        assert_eq!(value["cases"], 2);
        assert_eq!(value["passed"], false);
        let html =
            fs::read_to_string(project.path().join(crate::projects::TEST_REPORT_HTML)).unwrap();
        assert!(html.contains("dev.example.CounterTest"));
        assert!(html.contains("expected 3"));
        assert!(html.contains("CounterTest.kt:20"));
        // Failed cases come first.
        assert!(html.find(">overflows<").unwrap() < html.find(">adds<").unwrap());
        assert!(!html.contains("<script"));
        let output = project.path().join("elsewhere/r.html");
        report(&json!({"path":project.path(),"output":output})).unwrap();
        assert!(output.is_file());
        let empty = tempfile::tempdir().unwrap();
        assert_eq!(
            report(&json!({"path":empty.path()})).unwrap_err().code,
            "no_test_results"
        );
    }

    #[test]
    fn images_over_the_budget_are_linked_not_embedded() {
        let directory = tempfile::tempdir().unwrap();
        let file = directory.path().join("big.png");
        fs::write(&file, vec![0u8; (IMAGE_LIMIT + 1) as usize]).unwrap();
        let mut images = Images {
            embedded: 0,
            count: 0,
            linked: 0,
        };
        let html = images.figure(&file, "Screenshot");
        assert!(html.contains("not embedded"));
        assert_eq!(images.linked, 1);
        assert_eq!(escape("<a href='x'>&"), "&lt;a href=&#39;x&#39;&gt;&amp;");
    }
}
