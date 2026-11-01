//! A test history across runs: each test case or UI scenario outcome of a
//! run, one JSON line each, in a store file. testing.run and the test jobs of
//! project.execute add theirs when they end; testing.history reads the store
//! into a pass rate per test, so a test that flips between passing and
//! failing over time shows as flaky.
use crate::{core::*, test_results};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::{BufRead, BufReader, Write},
    path::{Path, PathBuf},
};

/// A store larger than this many entries keeps its newest `KEEP`.
const MAX_ENTRIES: usize = 100_000;
const KEEP: usize = 80_000;
/// The outcomes `recent` shows, newest last.
const RECENT: usize = 20;

/// The store `a` names, or the default one in Tomacco's data directory.
pub fn store_path(a: &Value) -> Result<PathBuf> {
    if s(a, "store").is_empty() {
        Ok(home()?.join("testing").join("history.jsonl"))
    } else {
        output_path(s(a, "store"))
    }
}

fn read_entries(store: &Path) -> Result<Vec<Value>> {
    if !store.is_file() {
        return Ok(Vec::new());
    }
    let file = fs::File::open(store)?;
    Ok(BufReader::new(file)
        .lines()
        .map_while(std::result::Result::ok)
        .filter_map(|line| serde_json::from_str::<Value>(&line).ok())
        .filter(|entry| entry.is_object())
        .collect())
}

/// Appends entries whose `key` the store does not hold yet; returns how
/// many it added. The store keeps its newest entries when it grows large.
pub fn append(store: &Path, entries: Vec<Value>) -> Result<usize> {
    if entries.is_empty() {
        return Ok(0);
    }
    let lock = Lock::open(&store.with_extension("lock"))?;
    lock.acquire(false)?;
    let existing = read_entries(store)?;
    let known: BTreeSet<String> = existing.iter().map(|e| s(e, "key").to_string()).collect();
    let fresh: Vec<Value> = entries
        .into_iter()
        .filter(|e| !known.contains(s(e, "key")))
        .collect();
    if fresh.is_empty() {
        return Ok(0);
    }
    if existing.len() + fresh.len() > MAX_ENTRIES {
        let mut kept: Vec<Value> = existing;
        kept.extend(fresh.iter().cloned());
        let start = kept.len().saturating_sub(KEEP);
        let text: String = kept[start..].iter().map(|e| format!("{e}\n")).collect();
        atomic_write(store, text.as_bytes())?;
    } else {
        if let Some(parent) = store.parent() {
            private_dir(parent)?;
        }
        let mut file = private_file(store, true)?;
        for entry in &fresh {
            writeln!(file, "{entry}")?;
        }
        file.sync_all()?;
    }
    Ok(fresh.len())
}

/// The history entries of a testing.run report: one per scenario and device.
pub fn run_entries(directory: &Path, report: &Value, suite: &str) -> Vec<Value> {
    let run = digest(format!("{}|{}", directory.display(), report["created"]).as_bytes())[..16]
        .to_string();
    report["results"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|result| {
            let outcome = if b(result, "flaky") {
                "flaky"
            } else if b(result, "passed") {
                "passed"
            } else {
                "failed"
            };
            json!({
                "key":format!("{run}|{}|{}", s(result, "scenario"), s(result, "device")),
                "run":run,"time":report["created"],"suite":suite,"kind":"scenario",
                "name":result["scenario"],"device":result["device"],"outcome":outcome,
                "duration":result["duration_seconds"],"message":result["error"]["message"],
                "source":directory,
            })
        })
        .collect()
}

/// The history entries of the JUnit and Dart JSON result files under `root`
/// written at or after `since`: one per test case. A file is recorded once.
pub fn result_entries(root: &Path, since: f64, suite: &str) -> Result<Vec<Value>> {
    let mut entries = Vec::new();
    for file in test_results::recent_files(root, since)? {
        let modified = test_results::modified(&file);
        let size = fs::metadata(&file)?.len();
        let file_key =
            digest(format!("{}|{modified}|{size}", file.display()).as_bytes())[..16].to_string();
        let text = String::from_utf8_lossy(&fs::read(&file)?).into_owned();
        for (index, case) in test_results::cases(&text).into_iter().enumerate() {
            let name = match (s(&case, "class"), s(&case, "name")) {
                ("", name) => name.to_string(),
                (class, name) => format!("{class}.{name}"),
            };
            entries.push(json!({
                "key":format!("{file_key}|{index}"),"run":file_key,"time":modified,"suite":suite,
                "kind":"test","name":name,"device":case["device"],"outcome":case["outcome"],
                "duration":case["time"],"message":case["message"],"source":file,
            }));
        }
    }
    Ok(entries)
}

/// Whether a directory is a testing.run report directory.
pub fn is_run_directory(directory: &Path) -> bool {
    directory.join("report.json").is_file() && directory.join("run.json").is_file()
}

fn record(a: &Value, store: &Path) -> Result {
    let root = path(s(a, "path"), false, true)?;
    let suite = if s(a, "suite").is_empty() {
        root.to_string_lossy().into_owned()
    } else {
        s(a, "suite").to_string()
    };
    let entries = if root.is_dir() && is_run_directory(&root) {
        let report = read_json(&root.join("report.json"), None)?;
        if !b(&report, "complete") {
            return Err(Error::new(
                "run_incomplete",
                "This testing.run report is not complete yet",
            )
            .remedy("Wait for the testing.run job to end, then record it"));
        }
        run_entries(&root, &report, &suite)
    } else {
        let mut since = a["since"].as_f64().unwrap_or(0.0);
        if !s(a, "job_id").is_empty() {
            let job = crate::jobs::get(s(a, "job_id"))?;
            since = since.max(job["created"].as_f64().unwrap_or(0.0) - 2.0);
        }
        result_entries(&root, since, &suite)?
    };
    if entries.is_empty() {
        return Err(Error::new(
            "no_test_results",
            format!("No test results to record under {}", root.display()),
        )
        .remedy("Give a testing.run report directory, a project after its test job, or a JUnit or Dart JSON result file"));
    }
    let found = entries.len();
    let added = append(store, entries)?;
    Ok(json!({"store":store,"recorded":added,"already_recorded":found - added,"suite":suite}))
}

fn passing(outcome: &str) -> Option<bool> {
    match outcome {
        "passed" | "flaky" => Some(true),
        "failed" | "error" => Some(false),
        _ => None,
    }
}

/// One test's line in the summary, from its entries in time order.
fn summarize(name: &str, device: &str, entries: &[&Value]) -> Value {
    let count = |wanted: &[&str]| {
        entries
            .iter()
            .filter(|e| wanted.contains(&s(e, "outcome")))
            .count()
    };
    let (passed, failed, skipped, retried) = (
        count(&["passed", "flaky"]),
        count(&["failed", "error"]),
        count(&["skipped"]),
        count(&["flaky"]),
    );
    let outcomes: Vec<bool> = entries
        .iter()
        .filter_map(|e| passing(s(e, "outcome")))
        .collect();
    let flips = outcomes.windows(2).filter(|w| w[0] != w[1]).count();
    let recent: String = entries
        .iter()
        .rev()
        .take(RECENT)
        .rev()
        .map(|e| match s(e, "outcome") {
            "passed" => 'P',
            "flaky" => 'R',
            "skipped" => 'S',
            _ => 'F',
        })
        .collect();
    let last_failure = entries
        .iter()
        .rev()
        .find(|e| passing(s(e, "outcome")) == Some(false))
        .map(|e| json!({"time":e["time"],"message":e["message"],"source":e["source"]}));
    let rate = if passed + failed == 0 {
        Value::Null
    } else {
        json!((passed as f64 * 1000.0 / (passed + failed) as f64).round() / 10.0)
    };
    let mut suites: Vec<&str> = entries.iter().map(|e| s(e, "suite")).collect();
    suites.sort();
    suites.dedup();
    let mut value = json!({
        "name":name,"kind":entries.last().map(|e| e["kind"].clone()).unwrap_or_default(),
        "runs":entries.len(),"passed":passed,"failed":failed,"skipped":skipped,"passed_after_retry":retried,
        "pass_rate":rate,"flips":flips,"flaky":(passed > 0 && failed > 0) || retried > 0,
        "recent":recent,"last_outcome":entries.last().map(|e| e["outcome"].clone()).unwrap_or_default(),
        "first_seen":entries.first().map(|e| e["time"].clone()).unwrap_or_default(),
        "last_seen":entries.last().map(|e| e["time"].clone()).unwrap_or_default(),
        "last_failure":last_failure,"suites":suites,
    });
    if !device.is_empty() {
        value["device"] = json!(device);
    }
    value
}

fn summary(a: &Value, store: &Path) -> Result {
    let entries = read_entries(store)?;
    let name = s(a, "name").to_lowercase();
    let suite = s(a, "suite");
    let since = a["since"].as_f64().unwrap_or(0.0);
    let by_device = b(a, "by_device");
    let mut groups: BTreeMap<(String, String), Vec<&Value>> = BTreeMap::new();
    for entry in &entries {
        if (!name.is_empty() && !s(entry, "name").to_lowercase().contains(&name))
            || (!suite.is_empty() && !s(entry, "suite").contains(suite))
            || entry["time"].as_f64().unwrap_or(0.0) < since
        {
            continue;
        }
        let device = if by_device {
            s(entry, "device").to_string()
        } else {
            String::new()
        };
        groups
            .entry((s(entry, "name").to_string(), device))
            .or_default()
            .push(entry);
    }
    let last = a["last"].as_u64().unwrap_or(50) as usize;
    let min_runs = a["min_runs"].as_u64().unwrap_or(1) as usize;
    let mut tests: Vec<Value> = groups
        .iter_mut()
        .filter_map(|((name, device), list)| {
            list.sort_by(|x, y| {
                x["time"]
                    .as_f64()
                    .unwrap_or(0.0)
                    .total_cmp(&y["time"].as_f64().unwrap_or(0.0))
            });
            let start = list.len().saturating_sub(last);
            let window = &list[start..];
            (window.len() >= min_runs).then(|| summarize(name, device, window))
        })
        .collect();
    let flaky_total = tests.iter().filter(|t| b(t, "flaky")).count();
    let failing_total = tests
        .iter()
        .filter(|t| t["pass_rate"].as_f64() == Some(0.0))
        .count();
    if b(a, "flaky_only") {
        tests.retain(|t| b(t, "flaky"));
    }
    // Flaky tests first, the least reliable of them first.
    tests.sort_by(|x, y| {
        b(y, "flaky")
            .cmp(&b(x, "flaky"))
            .then_with(|| {
                x["pass_rate"]
                    .as_f64()
                    .unwrap_or(101.0)
                    .total_cmp(&y["pass_rate"].as_f64().unwrap_or(101.0))
            })
            .then_with(|| n(y, "flips").cmp(&n(x, "flips")))
            .then_with(|| s(x, "name").cmp(s(y, "name")))
    });
    let total = tests.len();
    let limit = a["limit"].as_u64().unwrap_or(50) as usize;
    tests.truncate(limit);
    let runs: BTreeSet<&str> = entries.iter().map(|e| s(e, "run")).collect();
    Ok(json!({
        "store":store,"entries":entries.len(),"runs":runs.len(),"tests_total":total,
        "flaky_total":flaky_total,"always_failing_total":failing_total,"last":last,"tests":tests,
    }))
}

pub fn history(a: &Value) -> Result {
    let store = store_path(a)?;
    match s(a, "action") {
        "record" => record(a, &store),
        _ => summary(a, &store),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn junit(outcomes: &[(&str, &str)]) -> String {
        let mut xml = String::from("<testsuite name=\"dev.example.LoginTest\">");
        for (name, outcome) in outcomes {
            let body = match *outcome {
                "failed" => "<failure message=\"timed out\">stack</failure>",
                "skipped" => "<skipped/>",
                _ => "",
            };
            xml.push_str(&format!(
                "<testcase classname=\"dev.example.LoginTest\" name=\"{name}\" time=\"0.5\">{body}</testcase>"
            ));
        }
        xml + "</testsuite>"
    }

    #[test]
    fn runs_are_recorded_once_and_summarised_into_pass_rates() {
        let root = tempfile::tempdir().unwrap();
        let store = root.path().join("history.jsonl");
        let results = root.path().join("app/build/test-results/testDebugUnitTest");
        fs::create_dir_all(&results).unwrap();
        let file = results.join("TEST-dev.example.LoginTest.xml");
        let runs = [
            [
                ("signsIn", "passed"),
                ("rejects", "passed"),
                ("later", "skipped"),
            ],
            [
                ("signsIn", "failed"),
                ("rejects", "passed"),
                ("later", "skipped"),
            ],
            [
                ("signsIn", "passed"),
                ("rejects", "passed"),
                ("later", "skipped"),
            ],
        ];
        for (number, run) in runs.iter().enumerate() {
            fs::write(&file, junit(run)).unwrap();
            // Each run is a new file version: another size or time.
            let time = std::time::SystemTime::UNIX_EPOCH
                + std::time::Duration::from_secs(1_800_000_000 + number as u64 * 60);
            fs::File::options()
                .write(true)
                .open(&file)
                .unwrap()
                .set_modified(time)
                .unwrap();
            let recorded = history(
                &json!({"action":"record","path":root.path(),"store":store,"suite":"MyApp"}),
            )
            .unwrap();
            assert_eq!(recorded["recorded"], 3);
            let again = history(
                &json!({"action":"record","path":root.path(),"store":store,"suite":"MyApp"}),
            )
            .unwrap();
            assert_eq!(again["recorded"], 0);
            assert_eq!(again["already_recorded"], 3);
        }
        let value = history(&json!({"store":store})).unwrap();
        assert_eq!(value["entries"], 9);
        assert_eq!(value["runs"], 3);
        assert_eq!(value["tests_total"], 3);
        assert_eq!(value["flaky_total"], 1);
        let first = &value["tests"][0];
        assert_eq!(first["name"], "dev.example.LoginTest.signsIn");
        assert_eq!(first["flaky"], true);
        assert_eq!(first["pass_rate"], 66.7);
        assert_eq!(first["flips"], 2);
        assert_eq!(first["recent"], "PFP");
        assert_eq!(first["last_failure"]["message"], "timed out");
        assert_eq!(first["suites"], json!(["MyApp"]));
        let later = value["tests"]
            .as_array()
            .unwrap()
            .iter()
            .find(|t| t["name"] == "dev.example.LoginTest.later")
            .unwrap();
        assert_eq!(later["pass_rate"], Value::Null);
        assert_eq!(later["recent"], "SSS");
        let only = history(&json!({"store":store,"flaky_only":true})).unwrap();
        assert_eq!(only["tests"].as_array().unwrap().len(), 1);
        let window = history(&json!({"store":store,"last":1,"name":"signsin"})).unwrap();
        assert_eq!(window["tests"][0]["recent"], "P");
        assert_eq!(window["tests"][0]["flaky"], false);
        let none = history(&json!({"store":store,"min_runs":4})).unwrap();
        assert_eq!(none["tests_total"], 0);
    }

    #[test]
    fn ui_scenario_runs_are_recorded_per_device_with_retried_passes() {
        let root = tempfile::tempdir().unwrap();
        let store = root.path().join("h.jsonl");
        for (number, (first, second)) in [(true, false), (true, true)].iter().enumerate() {
            let run = root.path().join(format!("run{number}"));
            fs::create_dir_all(&run).unwrap();
            fs::write(run.join("run.json"), "{}").unwrap();
            let report = json!({"name":"login","created":1_800_000_000.0 + number as f64,"complete":true,"results":[
                {"scenario":"Login","device":"emulator-5554","passed":*first,"flaky":number == 1,"duration_seconds":3.0},
                {"scenario":"Login","device":"emulator-5556","passed":*second,"duration_seconds":4.0,"error":{"message":"screen_mismatch"}}]});
            atomic_json(&run.join("report.json"), &report).unwrap();
            let recorded = history(&json!({"action":"record","path":run,"store":store})).unwrap();
            assert_eq!(recorded["recorded"], 2);
        }
        let value = history(&json!({"store":store,"by_device":true})).unwrap();
        assert_eq!(value["tests_total"], 2);
        let flaky: Vec<&Value> = value["tests"].as_array().unwrap().iter().collect();
        assert_eq!(flaky[0]["flaky"], true);
        let device_5556 = flaky
            .iter()
            .find(|t| t["device"] == "emulator-5556")
            .unwrap();
        assert_eq!(device_5556["recent"], "FP");
        assert_eq!(device_5556["last_failure"]["message"], "screen_mismatch");
        let device_5554 = flaky
            .iter()
            .find(|t| t["device"] == "emulator-5554")
            .unwrap();
        assert_eq!(device_5554["recent"], "PR");
        assert_eq!(device_5554["passed_after_retry"], 1);
        let merged = history(&json!({"store":store})).unwrap();
        assert_eq!(merged["tests"][0]["runs"], 4);
        assert_eq!(merged["tests"][0]["kind"], "scenario");
        // An unfinished run is refused.
        let open = root.path().join("open");
        fs::create_dir_all(&open).unwrap();
        fs::write(open.join("run.json"), "{}").unwrap();
        atomic_json(
            &open.join("report.json"),
            &json!({"complete":false,"results":[]}),
        )
        .unwrap();
        assert_eq!(
            history(&json!({"action":"record","path":open,"store":store}))
                .unwrap_err()
                .code,
            "run_incomplete"
        );
    }

    #[test]
    fn a_large_store_keeps_its_newest_entries() {
        let root = tempfile::tempdir().unwrap();
        let store = root.path().join("h.jsonl");
        let text: String = (0..MAX_ENTRIES)
            .map(|i| {
                format!(
                    "{}\n",
                    json!({"key":i.to_string(),"name":"t","outcome":"passed","time":i})
                )
            })
            .collect();
        fs::write(&store, text).unwrap();
        assert_eq!(
            append(
                &store,
                vec![json!({"key":"new","name":"t","outcome":"failed","time":1e9})]
            )
            .unwrap(),
            1
        );
        let kept = read_entries(&store).unwrap();
        assert_eq!(kept.len(), KEEP);
        assert_eq!(kept.last().unwrap()["key"], "new");
    }
}
