//! JUnit XML results of a project's tests, and the JSON reporter output of
//! Dart and Flutter tests, read into one summary.
//!
//! Gradle writes them to build/test-results (unit tests) and
//! build/outputs/androidTest-results (instrumented tests); testing.run writes
//! junit.xml into its report directory; project.execute writes Flutter's
//! `--file-reporter json:` output to build/tomacco.
use crate::{core::*, jobs};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    time::UNIX_EPOCH,
};

const MAX_FAILURES: usize = 200;
const DETAIL_CHARACTERS: usize = 4000;

/// The XML result files under `root`, or `root` itself when it is a file.
pub fn files(root: &Path) -> Result<Vec<PathBuf>> {
    if root.is_file() {
        return Ok(vec![root.to_path_buf()]);
    }
    let base = glob::Pattern::escape(&root.to_string_lossy());
    let mut found = BTreeSet::new();
    for pattern in [
        "*.xml",
        "**/build/test-results/**/*.xml",
        "**/build/outputs/androidTest-results/**/*.xml",
        "build/tomacco/*.json",
    ] {
        for file in glob_paths(&format!("{base}/{pattern}"))? {
            let text = file.to_string_lossy();
            if !text.contains("/node_modules/") && !text.contains("/.gradle/") && file.is_file() {
                found.insert(file);
            }
        }
    }
    Ok(found.into_iter().collect())
}

fn number(node: roxmltree::Node, name: &str) -> f64 {
    node.attribute(name)
        .and_then(|v| v.replace(',', "").parse::<f64>().ok())
        .unwrap_or(0.0)
}

fn shorten(text: &str) -> String {
    let text = text.trim();
    if text.chars().count() <= DETAIL_CHARACTERS {
        text.to_string()
    } else {
        text.chars().take(DETAIL_CHARACTERS).collect::<String>() + "…"
    }
}

#[derive(Default)]
pub struct Summary {
    pub suites: Vec<Value>,
    pub failures: Vec<Value>,
    pub ignored: Vec<Value>,
    pub tests: u64,
    pub failed: u64,
    pub errors: u64,
    pub skipped: u64,
    pub time: f64,
    pub goldens: Vec<Value>,
}

/// The golden-file comparison a Flutter test failure reports, with the
/// images flutter_test wrote for it that exist: `masterImage`, `testImage`,
/// `isolatedDiff` and `maskedDiff`, in `failures/` beside the golden files.
pub fn golden(error: &str, test_file: &str) -> Option<Value> {
    let found = regex::Regex::new(r#"Golden "([^"]+)": ?([^\n]*)"#)
        .unwrap()
        .captures(error)?;
    let name = found[1].to_string();
    let directory = regex::Regex::new(r"Failure feedback can be found at (\S+)")
        .unwrap()
        .captures(error)
        .map(|c| PathBuf::from(c[1].trim_end_matches('.')))
        .or_else(|| {
            let file = test_file.strip_prefix("file://").unwrap_or(test_file);
            Path::new(file).parent().map(|p| p.join("failures"))
        });
    let stem = Path::new(&name)
        .file_stem()
        .unwrap_or_default()
        .to_string_lossy()
        .into_owned();
    let images: serde_json::Map<String, Value> = directory
        .iter()
        .flat_map(|dir| {
            let stem = &stem;
            ["masterImage", "testImage", "isolatedDiff", "maskedDiff"]
                .into_iter()
                .map(move |kind| (kind, dir.join(format!("{stem}_{kind}.png"))))
        })
        .filter(|(_, file)| file.is_file())
        .map(|(kind, file)| (kind.to_string(), json!(file)))
        .collect();
    Some(json!({
        "golden":name,"message":found[2].trim(),
        "failures_directory":directory,"images":images,
    }))
}

impl Summary {
    /// Adds one file. A file that is neither JUnit XML nor Dart's JSON
    /// reporter output is listed in `ignored`.
    pub fn add(&mut self, file: &Path, text: &str) {
        if text.trim_start().starts_with('{') {
            return self.add_dart_json(file, text);
        }
        let document = match roxmltree::Document::parse(text) {
            Ok(document) => document,
            Err(error) => {
                self.ignored
                    .push(json!({"file":file,"reason":format!("not XML: {error}")}));
                return;
            }
        };
        let root = document.root_element();
        let suites: Vec<roxmltree::Node> = match root.tag_name().name() {
            "testsuite" => vec![root],
            "testsuites" => root
                .descendants()
                .filter(|n| n.has_tag_name("testsuite"))
                .collect(),
            _ => {
                self.ignored
                    .push(json!({"file":file,"reason":"not JUnit XML"}));
                return;
            }
        };
        for suite in suites {
            let name = suite.attribute("name").unwrap_or("");
            let device = suite
                .descendants()
                .filter(|n| n.has_tag_name("property"))
                .find(|n| n.attribute("name") == Some("device"))
                .and_then(|n| n.attribute("value"))
                .unwrap_or("");
            let (mut tests, mut failed, mut errors, mut skipped) = (0u64, 0u64, 0u64, 0u64);
            for case in suite.children().filter(|n| n.has_tag_name("testcase")) {
                tests += 1;
                let problem = case
                    .children()
                    .find(|n| n.has_tag_name("failure") || n.has_tag_name("error"));
                if let Some(problem) = problem {
                    let kind = problem.tag_name().name();
                    if kind == "failure" {
                        failed += 1;
                    } else {
                        errors += 1;
                    }
                    if self.failures.len() < MAX_FAILURES {
                        self.failures.push(json!({
                            "suite":name,"class":case.attribute("classname").unwrap_or(""),
                            "name":case.attribute("name").unwrap_or(""),"kind":kind,
                            "type":problem.attribute("type").unwrap_or(""),
                            "message":shorten(problem.attribute("message").unwrap_or("")),
                            "details":shorten(problem.text().unwrap_or("")),
                            "device":device,"file":file,
                        }));
                    }
                } else if case.children().any(|n| n.has_tag_name("skipped")) {
                    skipped += 1;
                }
            }
            let time = number(suite, "time");
            self.tests += tests;
            self.failed += failed;
            self.errors += errors;
            self.skipped += skipped;
            self.time += time;
            self.suites.push(json!({
                "name":name,"file":file,"tests":tests,"failures":failed,"errors":errors,
                "skipped":skipped,"time":time,"timestamp":suite.attribute("timestamp").unwrap_or(""),
                "device":device,
            }));
        }
    }

    /// Adds the event stream of `dart test` or `flutter test` with the JSON
    /// reporter (`--machine`, `--reporter json` or `--file-reporter json:`):
    /// one suite per test file, hidden loading tests counted only when they
    /// fail, and golden-file failures listed with their diff images.
    pub fn add_dart_json(&mut self, file: &Path, text: &str) {
        use std::collections::BTreeMap;
        let mut suites: BTreeMap<u64, (String, [u64; 4], f64)> = BTreeMap::new();
        let mut tests: BTreeMap<u64, Value> = BTreeMap::new();
        let mut problems: BTreeMap<u64, Vec<Value>> = BTreeMap::new();
        let mut events = 0;
        for line in text.lines() {
            let Ok(event) = serde_json::from_str::<Value>(line.trim()) else {
                continue;
            };
            match s(&event, "type") {
                "start" => events += 1,
                "suite" => {
                    events += 1;
                    let suite = &event["suite"];
                    suites.insert(n(suite, "id"), (s(suite, "path").into(), [0; 4], 0.0));
                }
                "testStart" => {
                    let mut test = event["test"].clone();
                    test["started"] = event["time"].clone();
                    tests.insert(n(&event["test"], "id"), test);
                }
                "error" => problems.entry(n(&event, "testID")).or_default().push(event),
                "testDone" => {
                    let id = n(&event, "testID");
                    let Some(test) = tests.get(&id) else { continue };
                    let result = s(&event, "result");
                    if b(&event, "hidden") && result == "success" {
                        continue;
                    }
                    let entry = suites
                        .entry(n(test, "suiteID"))
                        .or_insert_with(|| (String::new(), [0; 4], 0.0));
                    let seconds =
                        (n(&event, "time").saturating_sub(n(test, "started"))) as f64 / 1000.0;
                    entry.1[0] += 1;
                    entry.2 += seconds;
                    if result == "success" {
                        if b(&event, "skipped") {
                            entry.1[3] += 1;
                        }
                        continue;
                    }
                    let kind = if result == "failure" {
                        "failure"
                    } else {
                        "error"
                    };
                    entry.1[if kind == "failure" { 1 } else { 2 }] += 1;
                    let errors = problems.get(&id).cloned().unwrap_or_default();
                    let message = errors
                        .first()
                        .map(|e| s(e, "error").to_string())
                        .unwrap_or_default();
                    let details = errors
                        .iter()
                        .map(|e| format!("{}\n{}", s(e, "error"), s(e, "stackTrace")))
                        .collect::<Vec<_>>()
                        .join("\n\n");
                    let suite_path = entry.0.clone();
                    let test_file = [s(test, "root_url"), s(test, "url"), &suite_path]
                        .into_iter()
                        .find(|u| !u.is_empty())
                        .unwrap_or_default()
                        .to_string();
                    let mut failure = json!({
                        "suite":suite_path,"class":"","name":s(test, "name"),"kind":kind,
                        "type":"","message":shorten(message.lines().next().unwrap_or("")),
                        "details":shorten(&details),"device":"","file":file,
                    });
                    let line = n(test, "root_line").max(n(test, "line"));
                    if line > 0 {
                        failure["line"] = json!(line);
                    }
                    if let Some(found) = errors
                        .iter()
                        .find_map(|e| golden(s(e, "error"), &test_file))
                    {
                        let mut found = found;
                        found["test"] = json!(s(test, "name"));
                        found["suite"] = json!(suite_path);
                        failure["golden"] = found.clone();
                        self.goldens.push(found);
                    }
                    if self.failures.len() < MAX_FAILURES {
                        self.failures.push(failure);
                    }
                }
                _ => (),
            }
        }
        if events == 0 {
            self.ignored
                .push(json!({"file":file,"reason":"not a Dart JSON test report"}));
            return;
        }
        for (name, [tests, failed, errors, skipped], time) in suites.into_values() {
            if tests == 0 {
                continue;
            }
            self.tests += tests;
            self.failed += failed;
            self.errors += errors;
            self.skipped += skipped;
            self.time += time;
            self.suites.push(json!({
                "name":name,"file":file,"tests":tests,"failures":failed,"errors":errors,
                "skipped":skipped,"time":(time * 1000.0).round() / 1000.0,"timestamp":"","device":"",
            }));
        }
    }

    pub fn value(&self) -> Value {
        json!({
            "passed":self.tests > 0 && self.failed == 0 && self.errors == 0,
            "totals":{
                "tests":self.tests,"passed":self.tests - self.failed - self.errors - self.skipped,
                "failed":self.failed,"errors":self.errors,"skipped":self.skipped,
                "time":(self.time * 1000.0).round() / 1000.0,
            },
            "suites":self.suites,"failures":self.failures,
            "failures_truncated":(self.failed + self.errors) as usize > self.failures.len(),
            "ignored":self.ignored,
            "golden_failures":self.goldens,
        })
    }
}

/// Each test case of a JUnit XML or Dart JSON reporter file, passed or not:
/// `class`, `name`, `outcome` (`passed`, `failed`, `error`, `skipped` or
/// `flaky` for a JUnit case with flakyFailure elements), `time`, `device`
/// and for a failure its `message`. Empty for anything else.
pub fn cases(text: &str) -> Vec<Value> {
    let mut found = Vec::new();
    if text.trim_start().starts_with('{') {
        let mut names = std::collections::BTreeMap::new();
        let mut errors: std::collections::BTreeMap<u64, String> = Default::default();
        for line in text.lines() {
            let Ok(event) = serde_json::from_str::<Value>(line.trim()) else {
                continue;
            };
            match s(&event, "type") {
                "testStart" => {
                    names.insert(
                        n(&event["test"], "id"),
                        (s(&event["test"], "name").to_string(), n(&event, "time")),
                    );
                }
                "error" => {
                    errors
                        .entry(n(&event, "testID"))
                        .or_insert_with(|| s(&event, "error").lines().next().unwrap_or("").into());
                }
                "testDone" => {
                    let id = n(&event, "testID");
                    let Some((name, started)) = names.get(&id) else {
                        continue;
                    };
                    if b(&event, "hidden") && s(&event, "result") == "success" {
                        continue;
                    }
                    let outcome = match s(&event, "result") {
                        "success" if b(&event, "skipped") => "skipped",
                        "success" => "passed",
                        "failure" => "failed",
                        _ => "error",
                    };
                    found.push(json!({"class":"","name":name,"outcome":outcome,
                        "time":n(&event, "time").saturating_sub(*started) as f64 / 1000.0,"device":"",
                        "message":shorten(errors.get(&id).map(String::as_str).unwrap_or(""))}));
                }
                _ => (),
            }
        }
        return found;
    }
    let Ok(document) = roxmltree::Document::parse(text) else {
        return found;
    };
    for suite in document
        .descendants()
        .filter(|n| n.has_tag_name("testsuite"))
    {
        let device = suite
            .descendants()
            .filter(|n| n.has_tag_name("property"))
            .find(|n| n.attribute("name") == Some("device"))
            .and_then(|n| n.attribute("value"))
            .unwrap_or("");
        for case in suite.children().filter(|n| n.has_tag_name("testcase")) {
            let problem = case
                .children()
                .find(|n| n.has_tag_name("failure") || n.has_tag_name("error"));
            let outcome = match problem {
                Some(p) if p.has_tag_name("failure") => "failed",
                Some(_) => "error",
                None if case.children().any(|n| n.has_tag_name("skipped")) => "skipped",
                None if case.children().any(|n| n.has_tag_name("flakyFailure")) => "flaky",
                None => "passed",
            };
            found.push(json!({
                "class":case.attribute("classname").unwrap_or(""),
                "name":case.attribute("name").unwrap_or(""),"outcome":outcome,
                "time":number(case, "time"),"device":device,
                "message":shorten(problem.and_then(|p| p.attribute("message")).unwrap_or("")),
            }));
        }
    }
    found
}

/// The result files under `root` written at or after `since`.
pub fn recent_files(root: &Path, since: f64) -> Result<Vec<PathBuf>> {
    Ok(files(root)?
        .into_iter()
        .filter(|file| modified(file) >= since)
        .filter(|file| fs::metadata(file).is_ok_and(|m| m.len() <= 64 * 1024 * 1024))
        .collect())
}

pub fn modified(file: &Path) -> f64 {
    fs::metadata(file)
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map_or(0.0, |d| d.as_secs_f64())
}

pub fn results(a: &Value) -> Result {
    let root = path(s(a, "path"), false, true)?;
    let mut since = a["since"].as_f64().unwrap_or(0.0);
    if !s(a, "job_id").is_empty() {
        let job = jobs::get(s(a, "job_id"))?;
        since = since.max(job["created"].as_f64().unwrap_or(0.0) - 2.0);
    }
    let mut summary = Summary::default();
    let (mut read, mut older) = (0, 0);
    for file in files(&root)? {
        if modified(&file) < since {
            older += 1;
            continue;
        }
        if fs::metadata(&file)?.len() > 64 * 1024 * 1024 {
            summary
                .ignored
                .push(json!({"file":file,"reason":"larger than 64 MiB"}));
            continue;
        }
        summary.add(&file, &String::from_utf8_lossy(&fs::read(&file)?));
        read += 1;
    }
    if summary.suites.is_empty() {
        return Err(Error::new(
            "no_test_results",
            if older > 0 {
                format!("{older} JUnit files under {} are older than the run", root.display())
            } else {
                format!("No JUnit XML or Dart JSON test results under {}", root.display())
            },
        )
        .remedy("Run the tests first: project.execute with action test or instrumented-test, or testing.run")
        .details(json!({"ignored":summary.ignored})));
    }
    let mut value = summary.value();
    value["path"] = json!(root);
    value["files"] = json!(read);
    value["older_files_skipped"] = json!(older);
    if !s(a, "report").is_empty() {
        atomic_json(&output_path(s(a, "report"))?, &value)?;
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    const UNIT: &str = r#"<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="dev.example.CounterTest" tests="3" skipped="1" failures="1" errors="0" timestamp="2026-10-31T10:00:00" time="0.25">
  <properties/>
  <testcase name="adds" classname="dev.example.CounterTest" time="0.1"/>
  <testcase name="overflows" classname="dev.example.CounterTest" time="0.15">
    <failure message="expected:&lt;3&gt; but was:&lt;4&gt;" type="java.lang.AssertionError">java.lang.AssertionError: expected:&lt;3&gt; but was:&lt;4&gt;
	at dev.example.CounterTest.overflows(CounterTest.kt:20)</failure>
  </testcase>
  <testcase name="later" classname="dev.example.CounterTest" time="0"><skipped/></testcase>
  <system-out><![CDATA[]]></system-out>
</testsuite>"#;
    const CONNECTED: &str = r#"<?xml version='1.0' encoding='UTF-8' ?>
<testsuites><testsuite name="dev.example.UiTest" tests="2" failures="0" errors="1" skipped="0" time="3.5" hostname="localhost">
  <properties><property name="device" value="Pixel_API_36(AVD) - 16" /></properties>
  <testcase name="opens" classname="dev.example.UiTest" time="1.5" />
  <testcase name="crashes" classname="dev.example.UiTest" time="2"><error message="Process crashed.">Test failed to run to completion.</error></testcase>
</testsuite></testsuites>"#;

    #[test]
    fn suites_failures_and_skips_are_summarised() {
        let mut summary = Summary::default();
        summary.add(Path::new("/p/TEST-dev.example.CounterTest.xml"), UNIT);
        summary.add(Path::new("/p/TEST-Pixel.xml"), CONNECTED);
        summary.add(Path::new("/p/lint-results.xml"), "<issues/>");
        summary.add(Path::new("/p/broken.xml"), "<testsuite");
        let value = summary.value();
        assert_eq!(value["passed"], false);
        assert_eq!(
            value["totals"],
            json!({"tests":5,"passed":2,"failed":1,"errors":1,"skipped":1,"time":3.75})
        );
        assert_eq!(value["suites"].as_array().unwrap().len(), 2);
        assert_eq!(value["suites"][1]["device"], "Pixel_API_36(AVD) - 16");
        let failures = value["failures"].as_array().unwrap();
        assert_eq!(failures[0]["name"], "overflows");
        assert_eq!(failures[0]["message"], "expected:<3> but was:<4>");
        assert!(s(&failures[0], "details").contains("CounterTest.kt:20"));
        assert_eq!(failures[1]["kind"], "error");
        assert_eq!(failures[1]["device"], "Pixel_API_36(AVD) - 16");
        assert_eq!(value["ignored"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn flutter_json_results_list_failures_and_golden_diffs() {
        let project = tempfile::tempdir().unwrap();
        let test_file = project.path().join("test/widget_test.dart");
        let failures = project.path().join("test/failures");
        fs::create_dir_all(&failures).unwrap();
        for kind in ["masterImage", "testImage", "isolatedDiff"] {
            fs::write(failures.join(format!("home_{kind}.png")), b"png").unwrap();
        }
        let golden_error = format!(
            "Golden \"goldens/home.png\": Pixel test failed, 2.50%, 1200px diff detected.\nFailure feedback can be found at {}",
            failures.display()
        );
        let events = [
            json!({"protocolVersion":"0.1.1","runnerVersion":"1.25.0","pid":1,"type":"start","time":0}),
            json!({"suite":{"id":0,"platform":"vm","path":test_file},"type":"suite","time":0}),
            json!({"test":{"id":1,"name":"loading test/widget_test.dart","suiteID":0,"groupIDs":[],"metadata":{"skip":false},"line":null,"column":null,"url":null},"type":"testStart","time":1}),
            json!({"testID":1,"result":"success","skipped":false,"hidden":true,"type":"testDone","time":400}),
            json!({"test":{"id":3,"name":"counter increments","suiteID":0,"groupIDs":[2],"metadata":{"skip":false},"line":12,"column":3,"url":format!("file://{}", test_file.display())},"type":"testStart","time":410}),
            json!({"testID":3,"result":"success","skipped":false,"hidden":false,"type":"testDone","time":600}),
            json!({"test":{"id":4,"name":"home matches golden","suiteID":0,"groupIDs":[2],"metadata":{"skip":false},"line":20,"column":3,"url":format!("file://{}", test_file.display())},"type":"testStart","time":610}),
            json!({"testID":4,"error":golden_error,"stackTrace":"package:flutter_test/src/_matchers_io.dart 87:9","isFailure":true,"type":"error","time":900}),
            json!({"testID":4,"result":"failure","skipped":false,"hidden":false,"type":"testDone","time":910}),
            json!({"test":{"id":5,"name":"later","suiteID":0,"groupIDs":[2],"metadata":{"skip":true,"skipReason":"todo"},"line":30,"column":3,"url":null},"type":"testStart","time":920}),
            json!({"testID":5,"result":"success","skipped":true,"hidden":false,"type":"testDone","time":921}),
            json!({"test":{"id":6,"name":"throws","suiteID":0,"groupIDs":[2],"metadata":{"skip":false},"line":40,"column":3,"url":null},"type":"testStart","time":930}),
            json!({"testID":6,"error":"Bad state: no element","stackTrace":"dart:core","isFailure":false,"type":"error","time":940}),
            json!({"testID":6,"result":"error","skipped":false,"hidden":false,"type":"testDone","time":950}),
            json!({"success":false,"type":"done","time":1000}),
        ];
        let text: String = events.iter().map(|e| format!("{e}\n")).collect();
        let report = project.path().join("build/tomacco/flutter_test.json");
        fs::create_dir_all(report.parent().unwrap()).unwrap();
        fs::write(&report, text).unwrap();
        assert_eq!(files(project.path()).unwrap(), vec![report.clone()]);
        let value = results(&json!({"path":project.path()})).unwrap();
        assert_eq!(
            value["totals"],
            json!({"tests":4,"passed":1,"failed":1,"errors":1,"skipped":1,"time":0.511})
        );
        assert_eq!(value["suites"][0]["name"], json!(test_file));
        let failed = value["failures"].as_array().unwrap();
        assert_eq!(failed[0]["name"], "home matches golden");
        assert_eq!(failed[0]["line"], 20);
        assert_eq!(
            failed[0]["message"],
            "Golden \"goldens/home.png\": Pixel test failed, 2.50%, 1200px diff detected."
        );
        assert_eq!(failed[1]["kind"], "error");
        let golden = &value["golden_failures"][0];
        assert_eq!(golden["golden"], "goldens/home.png");
        assert_eq!(
            golden["message"],
            "Pixel test failed, 2.50%, 1200px diff detected."
        );
        assert_eq!(golden["test"], "home matches golden");
        assert_eq!(
            golden["images"]["isolatedDiff"],
            json!(failures_path(&failures, "isolatedDiff"))
        );
        assert!(golden["images"].get("maskedDiff").is_none());
        let mut summary = Summary::default();
        summary.add(Path::new("/p/other.json"), "{\"not\":\"a report\"}");
        assert_eq!(summary.ignored.len(), 1);
    }

    fn failures_path(dir: &Path, kind: &str) -> PathBuf {
        dir.join(format!("home_{kind}.png"))
    }

    #[test]
    fn a_golden_without_a_feedback_line_is_looked_up_beside_its_test() {
        let found = golden(
            "Golden \"goldens/a.png\": Image sizes do not match.",
            "file:///p/test/a_test.dart",
        )
        .unwrap();
        assert_eq!(found["failures_directory"], "/p/test/failures");
        assert_eq!(found["images"], json!({}));
        assert!(golden("Expected: <1>", "").is_none());
    }

    #[test]
    fn gradle_layouts_are_found_and_stale_files_left_out() {
        let root = tempfile::tempdir().unwrap();
        let unit = root
            .path()
            .join("app/build/test-results/testDebugUnitTest/TEST-dev.example.CounterTest.xml");
        let connected = root.path().join(
            "app/build/outputs/androidTest-results/connected/debug/TEST-Pixel_API_36(AVD) - 16-_app-.xml",
        );
        let other = root.path().join("app/build/reports/lint-results.xml");
        for (file, text) in [
            (&unit, UNIT),
            (&connected, CONNECTED),
            (&other, "<issues/>"),
        ] {
            fs::create_dir_all(file.parent().unwrap()).unwrap();
            fs::write(file, text).unwrap();
        }
        let found = files(root.path()).unwrap();
        assert_eq!(found.len(), 2);
        assert_eq!(files(&unit).unwrap(), std::slice::from_ref(&unit));
        let report = root.path().join("summary.json");
        let value = results(&json!({"path":root.path(),"report":report})).unwrap();
        assert_eq!(value["files"], 2);
        assert_eq!(value["totals"]["tests"], 5);
        assert_eq!(read_json(&report, None).unwrap()["totals"], value["totals"]);
        let stale = results(&json!({"path":root.path(),"since":now() + 3600.0})).unwrap_err();
        assert_eq!(stale.code, "no_test_results");
        assert!(stale.message.contains("older than the run"));
        let empty = tempfile::tempdir().unwrap();
        assert_eq!(
            results(&json!({"path":empty.path()})).unwrap_err().code,
            "no_test_results"
        );
    }
}
