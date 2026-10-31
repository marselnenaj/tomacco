//! JUnit XML results of a project's tests, read into one summary.
//!
//! Gradle writes them to build/test-results (unit tests) and
//! build/outputs/androidTest-results (instrumented tests); testing.run writes
//! junit.xml into its report directory.
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
}

impl Summary {
    /// Adds one file. A file that is not JUnit XML is listed in `ignored`.
    pub fn add(&mut self, file: &Path, text: &str) {
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
        })
    }
}

fn modified(file: &Path) -> f64 {
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
                format!("No JUnit XML results under {}", root.display())
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
        assert_eq!(files(&unit).unwrap(), [unit.clone()]);
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
