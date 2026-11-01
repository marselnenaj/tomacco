//! Code coverage of a project's tests, read from JaCoCo XML reports: the
//! ones the Android Gradle plugin writes for unit tests
//! (`enableUnitTestCoverage`) and instrumented tests
//! (`enableAndroidTestCoverage`), the Gradle `jacoco` plugin's
//! `jacocoTestReport`, and Kover's XML report, which has the same format.
//! Several reports, such as a module's unit and instrumented tests, are
//! merged line by line: a line is covered when any report covers it.
use crate::{core::*, jobs};
use serde_json::{Map, Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
};

const COUNTERS: [&str; 6] = [
    "INSTRUCTION",
    "BRANCH",
    "LINE",
    "COMPLEXITY",
    "METHOD",
    "CLASS",
];
const MAX_REPORT_BYTES: u64 = 256 * 1024 * 1024;

/// Where JaCoCo and Kover XML reports are written, below a project.
const PATTERNS: [&str; 4] = [
    "**/build/reports/coverage/**/*.xml",
    "**/build/reports/jacoco/**/*.xml",
    "**/build/reports/kover/**/*.xml",
    "*.xml",
];

#[derive(Clone, Copy, Default, Debug, PartialEq)]
struct Count {
    missed: u64,
    covered: u64,
}

impl Count {
    fn add(&mut self, other: Count) {
        self.missed += other.missed;
        self.covered += other.covered;
    }
    fn value(self) -> Value {
        json!({"covered":self.covered,"missed":self.missed,"total":self.covered + self.missed,
               "percent":percent(self.covered, self.missed)})
    }
}

fn percent(covered: u64, missed: u64) -> Value {
    let total = covered + missed;
    if total == 0 {
        Value::Null
    } else {
        json!((covered as f64 * 10000.0 / total as f64).round() / 100.0)
    }
}

/// One line of a source file: missed and covered instructions and branches.
#[derive(Clone, Copy, Default)]
struct Line {
    mi: u64,
    ci: u64,
    mb: u64,
    cb: u64,
}

#[derive(Default, Clone)]
struct SourceFile {
    module: String,
    package: String,
    name: String,
    lines: BTreeMap<u64, Line>,
    /// The file's own counters, used when the report lists no lines.
    counters: BTreeMap<String, Count>,
}

impl SourceFile {
    fn key(&self) -> String {
        format!("{}|{}", self.module, self.path())
    }
    fn path(&self) -> String {
        if self.package.is_empty() {
            self.name.clone()
        } else {
            format!("{}/{}", self.package, self.name)
        }
    }
    fn line_count(&self) -> Count {
        if self.lines.is_empty() {
            return self.counters.get("LINE").copied().unwrap_or_default();
        }
        let covered = self.lines.values().filter(|l| l.ci > 0).count() as u64;
        // A line without instructions is not a line JaCoCo counts.
        let counted = self.lines.values().filter(|l| l.ci + l.mi > 0).count() as u64;
        Count {
            covered,
            missed: counted - covered,
        }
    }
    fn branch_count(&self) -> Count {
        if self.lines.is_empty() {
            return self.counters.get("BRANCH").copied().unwrap_or_default();
        }
        self.lines.values().fold(Count::default(), |mut c, l| {
            c.covered += l.cb;
            c.missed += l.mb;
            c
        })
    }
    /// Lines with instructions none of which ran, as ranges like `12-15`.
    fn missed_lines(&self) -> Vec<String> {
        let mut ranges: Vec<(u64, u64)> = Vec::new();
        for (&number, line) in &self.lines {
            if line.ci == 0 && line.mi > 0 {
                match ranges.last_mut() {
                    Some((_, end)) if *end + 1 == number => *end = number,
                    _ => ranges.push((number, number)),
                }
            }
        }
        ranges
            .into_iter()
            .map(|(a, b)| {
                if a == b {
                    a.to_string()
                } else {
                    format!("{a}-{b}")
                }
            })
            .collect()
    }
    /// Takes the other report's lines in: covered when either covers it.
    fn merge(&mut self, other: &SourceFile) {
        for (&number, line) in &other.lines {
            let entry = self.lines.entry(number).or_insert(*line);
            if entry.ci == 0 && line.ci > 0 {
                entry.ci = line.ci;
                entry.mi = line.mi;
            }
            let total = (entry.mb + entry.cb).max(line.mb + line.cb);
            let covered = entry.cb.max(line.cb);
            entry.cb = covered;
            entry.mb = total - covered;
        }
        if self.lines.is_empty() {
            for (name, count) in &other.counters {
                let entry = self.counters.entry(name.clone()).or_default();
                if count.covered > entry.covered {
                    *entry = *count;
                }
            }
        }
    }
}

#[derive(Default, Clone)]
struct Class {
    module: String,
    name: String,
    source: String,
    counters: BTreeMap<String, Count>,
}

/// One JaCoCo or Kover XML report.
#[derive(Default)]
struct Report {
    file: PathBuf,
    kind: &'static str,
    module: String,
    name: String,
    counters: BTreeMap<String, Count>,
    files: Vec<SourceFile>,
    classes: Vec<Class>,
}

fn counters_of(node: roxmltree::Node) -> BTreeMap<String, Count> {
    node.children()
        .filter(|c| c.has_tag_name("counter"))
        .filter_map(|c| {
            let number = |key| c.attribute(key).and_then(|v| v.parse::<u64>().ok());
            Some((
                c.attribute("type")?.to_string(),
                Count {
                    missed: number("missed")?,
                    covered: number("covered")?,
                },
            ))
        })
        .collect()
}

/// What made a report, from where it lies.
fn kind_of(file: &Path) -> &'static str {
    let text = file.to_string_lossy();
    if text.contains("/build/reports/kover/") {
        "kover"
    } else if text.contains("/androidTest/") || text.contains("AndroidTest") {
        "instrumented"
    } else if text.contains("/build/reports/coverage/test/") || text.contains("UnitTest") {
        "unit"
    } else if text.contains("/build/reports/jacoco/") {
        "jacoco"
    } else {
        "report"
    }
}

/// The Gradle path of the module a report under `root` belongs to.
fn module_of(root: &Path, file: &Path) -> String {
    let relative = file.strip_prefix(root).unwrap_or(file).to_string_lossy();
    match relative.find("build/reports/") {
        Some(0) => ":".into(),
        Some(at) => format!(
            ":{}",
            relative[..at].trim_end_matches('/').replace('/', ":")
        ),
        None => String::new(),
    }
}

/// Parses one report; None when the text is not a JaCoCo-format report.
fn parse(file: &Path, module: &str, text: &str) -> std::result::Result<Report, String> {
    let options = roxmltree::ParsingOptions {
        allow_dtd: true,
        ..Default::default()
    };
    let document = roxmltree::Document::parse_with_options(text, options)
        .map_err(|e| format!("not XML: {e}"))?;
    let root = document.root_element();
    if !root.has_tag_name("report") {
        return Err("not a JaCoCo or Kover XML report".into());
    }
    let mut report = Report {
        file: file.to_path_buf(),
        kind: kind_of(file),
        module: module.to_string(),
        name: root.attribute("name").unwrap_or("").to_string(),
        counters: counters_of(root),
        ..Default::default()
    };
    for package in root.descendants().filter(|n| n.has_tag_name("package")) {
        let package_name = package.attribute("name").unwrap_or("").to_string();
        for child in package.children() {
            if child.has_tag_name("sourcefile") {
                let lines = child
                    .children()
                    .filter(|l| l.has_tag_name("line"))
                    .filter_map(|l| {
                        let number = |key| {
                            l.attribute(key)
                                .and_then(|v| v.parse::<u64>().ok())
                                .unwrap_or(0)
                        };
                        let nr = l.attribute("nr")?.parse::<u64>().ok()?;
                        Some((
                            nr,
                            Line {
                                mi: number("mi"),
                                ci: number("ci"),
                                mb: number("mb"),
                                cb: number("cb"),
                            },
                        ))
                    })
                    .collect();
                report.files.push(SourceFile {
                    module: module.to_string(),
                    package: package_name.clone(),
                    name: child.attribute("name").unwrap_or("").to_string(),
                    lines,
                    counters: counters_of(child),
                });
            } else if child.has_tag_name("class") {
                report.classes.push(Class {
                    module: module.to_string(),
                    name: child.attribute("name").unwrap_or("").replace('/', "."),
                    source: child
                        .attribute("sourcefilename")
                        .map(|s| {
                            if package_name.is_empty() {
                                s.to_string()
                            } else {
                                format!("{package_name}/{s}")
                            }
                        })
                        .unwrap_or_default(),
                    counters: counters_of(child),
                });
            }
        }
    }
    if report.counters.is_empty() {
        // A report always has its totals; derive them when one leaves them out.
        for class in &report.classes {
            for (name, count) in &class.counters {
                report.counters.entry(name.clone()).or_default().add(*count);
            }
        }
    }
    Ok(report)
}

fn totals_value(counters: &BTreeMap<String, Count>) -> Value {
    let mut out = Map::new();
    for name in COUNTERS {
        out.insert(
            name.to_lowercase(),
            counters.get(name).map_or(Value::Null, |c| c.value()),
        );
    }
    Value::Object(out)
}

fn report_files(root: &Path) -> Result<Vec<PathBuf>> {
    if root.is_file() {
        return Ok(vec![root.to_path_buf()]);
    }
    let base = glob::Pattern::escape(&root.to_string_lossy());
    let mut found = BTreeSet::new();
    for pattern in PATTERNS {
        for file in glob_paths(&format!("{base}/{pattern}"))? {
            let text = file.to_string_lossy();
            if !text.contains("/node_modules/") && !text.contains("/.gradle/") && file.is_file() {
                found.insert(file);
            }
        }
    }
    Ok(found.into_iter().collect())
}

/// The coverage of every report under `root`, merged, as the full value
/// `testing.coverage` returns (every file and class).
fn measure(root: &Path, since: f64) -> Result<Value> {
    let mut reports = Vec::new();
    let mut ignored = Vec::new();
    let mut older = 0;
    for file in report_files(root)? {
        let meta = fs::metadata(&file)?;
        let modified = meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map_or(0.0, |d| d.as_secs_f64());
        if modified < since {
            older += 1;
            continue;
        }
        if meta.len() > MAX_REPORT_BYTES {
            ignored.push(json!({"file":file,"reason":"larger than 256 MiB"}));
            continue;
        }
        let text = String::from_utf8_lossy(&fs::read(&file)?).into_owned();
        // Other XML files in the same directories, such as lint results.
        if !text.contains("<report") {
            continue;
        }
        let module = if root.is_file() {
            String::new()
        } else {
            module_of(root, &file)
        };
        match parse(&file, &module, &text) {
            Ok(report) => reports.push(report),
            Err(reason) => ignored.push(json!({"file":file,"reason":reason})),
        }
    }
    if reports.is_empty() {
        return Err(Error::new(
            "no_coverage_reports",
            if older > 0 {
                format!("{older} coverage reports under {} are older than the run", root.display())
            } else {
                format!("No JaCoCo or Kover XML coverage report under {}", root.display())
            },
        )
        .remedy("Run the tests with coverage first: project.execute with coverage:true and action test or instrumented-test. The build must enable it: enableUnitTestCoverage or enableAndroidTestCoverage in the debug build type, the jacoco plugin, or Kover")
        .details(json!({"ignored":ignored})));
    }
    Ok(summarize(root, &reports, ignored, older))
}

fn summarize(root: &Path, reports: &[Report], ignored: Vec<Value>, older: usize) -> Value {
    let mut files: BTreeMap<String, SourceFile> = BTreeMap::new();
    let mut classes: BTreeMap<String, Class> = BTreeMap::new();
    for report in reports {
        for file in &report.files {
            match files.get_mut(&file.key()) {
                Some(existing) => existing.merge(file),
                None => {
                    files.insert(file.key(), file.clone());
                }
            }
        }
        for class in &report.classes {
            let key = format!("{}|{}", class.module, class.name);
            let better = classes.get(&key).is_none_or(|kept| {
                class.counters.get("INSTRUCTION").map_or(0, |c| c.covered)
                    > kept.counters.get("INSTRUCTION").map_or(0, |c| c.covered)
            });
            if better {
                classes.insert(key, class.clone());
            }
        }
    }
    let totals = if reports.len() == 1 {
        reports[0].counters.clone()
    } else {
        let mut totals: BTreeMap<String, Count> = BTreeMap::new();
        for file in files.values() {
            totals
                .entry("LINE".into())
                .or_default()
                .add(file.line_count());
            totals
                .entry("BRANCH".into())
                .or_default()
                .add(file.branch_count());
        }
        for class in classes.values() {
            for name in ["INSTRUCTION", "COMPLEXITY", "METHOD", "CLASS"] {
                if let Some(count) = class.counters.get(name) {
                    totals.entry(name.into()).or_default().add(*count);
                }
            }
        }
        totals
    };
    let mut file_values: Vec<Value> = files
        .values()
        .map(|f| {
            json!({"module":f.module,"path":f.path(),"lines":f.line_count().value(),
                   "branches":f.branch_count().value(),"missed_lines":f.missed_lines()})
        })
        .collect();
    sort_by_line_percent(&mut file_values, "lines");
    let mut class_values: Vec<Value> = classes
        .values()
        .map(|c| {
            let mut value = json!({"module":c.module,"name":c.name,"source":c.source});
            for name in COUNTERS {
                if let Some(count) = c.counters.get(name) {
                    value[name.to_lowercase()] = count.value();
                }
            }
            value
        })
        .collect();
    sort_by_line_percent(&mut class_values, "line");
    json!({
        "path":root,
        "totals":totals_value(&totals),
        "merged":reports.len() > 1,
        "reports":reports.iter().map(|r| json!({"file":r.file,"kind":r.kind,"module":r.module,"name":r.name,
            "totals":totals_value(&r.counters)})).collect::<Vec<_>>(),
        "files":file_values,
        "classes":class_values,
        "ignored":ignored,
        "older_files_skipped":older,
    })
}

/// Least covered first; files without counted lines last.
fn sort_by_line_percent(values: &mut [Value], key: &str) {
    values.sort_by(|a, b| {
        let pa = a[key]["percent"].as_f64().unwrap_or(f64::INFINITY);
        let pb = b[key]["percent"].as_f64().unwrap_or(f64::INFINITY);
        pa.total_cmp(&pb)
            .then_with(|| n(&b[key], "missed").cmp(&n(&a[key], "missed")))
            .then_with(|| s(a, "path").cmp(s(b, "path")))
    });
}

/// The baseline's full coverage value: a JSON report an earlier
/// testing.coverage wrote, or JaCoCo XML (a file or a directory).
fn baseline_value(value: &str) -> Result<Value> {
    let at = path(value, false, true)?;
    if at.is_file() && at.extension().is_some_and(|e| e == "json") {
        let report = read_json(&at, None)?;
        if !report["totals"].is_object() || !report["files"].is_array() {
            return Err(Error::new(
                "invalid_baseline",
                format!("{} is not a report testing.coverage wrote", at.display()),
            )
            .remedy("Give the JSON file an earlier testing.coverage wrote with report, or a JaCoCo XML report"));
        }
        return Ok(report);
    }
    measure(&at, 0.0)
}

fn change(after: &Value, before: &Value) -> Value {
    match (after.as_f64(), before.as_f64()) {
        (Some(a), Some(b)) => json!(((a - b) * 100.0).round() / 100.0),
        _ => Value::Null,
    }
}

/// What changed from `before` to `after`: totals, and each file whose line
/// coverage moved, appeared or went away.
fn delta(after: &Value, before: &Value) -> Value {
    let mut totals = Map::new();
    for name in COUNTERS {
        let key = name.to_lowercase();
        let (a, b) = (&after["totals"][&key], &before["totals"][&key]);
        if a.is_null() && b.is_null() {
            continue;
        }
        totals.insert(
            key,
            json!({"before":b["percent"],"after":a["percent"],"change":change(&a["percent"], &b["percent"]),
                   "covered_change":n(a, "covered") as i64 - n(b, "covered") as i64}),
        );
    }
    let index = |value: &Value| -> BTreeMap<String, Value> {
        value["files"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|f| (format!("{}|{}", s(f, "module"), s(f, "path")), f.clone()))
            .collect()
    };
    let (now_files, then_files) = (index(after), index(before));
    let mut changed = Vec::new();
    let mut added = Vec::new();
    for (key, file) in &now_files {
        let after_percent = &file["lines"]["percent"];
        match then_files.get(key) {
            Some(old) => {
                let before_percent = &old["lines"]["percent"];
                if after_percent != before_percent {
                    changed.push(json!({"module":file["module"],"path":file["path"],
                        "before":before_percent,"after":after_percent,"change":change(after_percent, before_percent)}));
                }
            }
            None => added
                .push(json!({"module":file["module"],"path":file["path"],"after":after_percent})),
        }
    }
    let removed: Vec<Value> = then_files
        .iter()
        .filter(|(key, _)| !now_files.contains_key(*key))
        .map(|(_, f)| json!({"module":f["module"],"path":f["path"],"before":f["lines"]["percent"]}))
        .collect();
    changed.sort_by(|a, b| {
        a["change"]
            .as_f64()
            .unwrap_or(0.0)
            .total_cmp(&b["change"].as_f64().unwrap_or(0.0))
    });
    let regressions = changed
        .iter()
        .filter(|c| c["change"].as_f64().is_some_and(|v| v < 0.0))
        .count();
    json!({"totals":totals,"files":changed,"new_files":added,"removed_files":removed,"regressions":regressions})
}

pub fn coverage(a: &Value) -> Result {
    let root = path(s(a, "path"), false, true)?;
    let mut since = a["since"].as_f64().unwrap_or(0.0);
    if !s(a, "job_id").is_empty() {
        let job = jobs::get(s(a, "job_id"))?;
        since = since.max(job["created"].as_f64().unwrap_or(0.0) - 2.0);
    }
    let mut value = measure(&root, since)?;
    let line = value["totals"]["line"]["percent"].as_f64();
    let mut checks = Vec::new();
    if let Some(minimum) = a["minimum"].as_f64() {
        let passed = line.is_some_and(|p| p >= minimum);
        checks
            .push(json!({"check":"minimum","line_percent":line,"minimum":minimum,"passed":passed}));
    }
    if !s(a, "baseline").is_empty() {
        let before = baseline_value(s(a, "baseline"))?;
        let mut found = delta(&value, &before);
        found["baseline"] = json!(s(a, "baseline"));
        if let Some(limit) = a["max_drop"].as_f64() {
            let dropped = -found["totals"]["line"]["change"].as_f64().unwrap_or(0.0);
            checks.push(
                json!({"check":"max_drop","line_change":found["totals"]["line"]["change"],
                               "max_drop":limit,"passed":dropped <= limit}),
            );
        }
        value["delta"] = found;
    }
    value["passed"] = json!(checks.iter().all(|c| b(c, "passed")));
    value["checks"] = json!(checks);
    if !s(a, "report").is_empty() {
        atomic_json(&output_path(s(a, "report"))?, &value)?;
    }
    // The answer lists the least covered files and classes; the report
    // keeps them all, for a later baseline.
    let limit = a["limit"].as_u64().unwrap_or(50) as usize;
    for key in ["files", "classes"] {
        let all = value[key].as_array().cloned().unwrap_or_default();
        value[format!("{key}_total")] = json!(all.len());
        value[key] = json!(all.into_iter().take(limit).collect::<Vec<_>>());
    }
    if let Some(delta) = value.get_mut("delta") {
        let all = delta["files"].as_array().cloned().unwrap_or_default();
        delta["files_total"] = json!(all.len());
        delta["files"] = json!(all.into_iter().take(limit).collect::<Vec<_>>());
    }
    Ok(value)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    /// A JaCoCo report as the Android Gradle plugin writes it: DOCTYPE,
    /// packages with classes and source files, and the report's totals.
    pub const UNIT: &str = r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><!DOCTYPE report PUBLIC "-//JACOCO//DTD Report 1.1//EN" "report.dtd"><report name="debug"><sessioninfo id="host-1" start="1" dump="2"/><package name="dev/example/app"><class name="dev/example/app/Counter" sourcefilename="Counter.kt"><method name="add" desc="()V" line="5"><counter type="INSTRUCTION" missed="0" covered="6"/><counter type="LINE" missed="0" covered="2"/></method><counter type="INSTRUCTION" missed="4" covered="6"/><counter type="BRANCH" missed="1" covered="1"/><counter type="LINE" missed="2" covered="2"/><counter type="COMPLEXITY" missed="1" covered="1"/><counter type="METHOD" missed="1" covered="1"/><counter type="CLASS" missed="0" covered="1"/></class><class name="dev/example/app/Screen" sourcefilename="Screen.kt"><counter type="INSTRUCTION" missed="10" covered="0"/><counter type="LINE" missed="3" covered="0"/><counter type="METHOD" missed="1" covered="0"/><counter type="CLASS" missed="1" covered="0"/></class><sourcefile name="Counter.kt"><line nr="5" mi="0" ci="3" mb="0" cb="0"/><line nr="6" mi="0" ci="3" mb="0" cb="0"/><line nr="8" mi="2" ci="0" mb="1" cb="1"/><line nr="9" mi="2" ci="0" mb="0" cb="0"/><counter type="LINE" missed="2" covered="2"/></sourcefile><sourcefile name="Screen.kt"><line nr="3" mi="4" ci="0" mb="0" cb="0"/><line nr="4" mi="3" ci="0" mb="0" cb="0"/><line nr="5" mi="3" ci="0" mb="0" cb="0"/><counter type="LINE" missed="3" covered="0"/></sourcefile></package><counter type="INSTRUCTION" missed="14" covered="6"/><counter type="BRANCH" missed="1" covered="1"/><counter type="LINE" missed="5" covered="2"/><counter type="COMPLEXITY" missed="2" covered="1"/><counter type="METHOD" missed="2" covered="1"/><counter type="CLASS" missed="1" covered="1"/></report>"#;
    /// The same module's instrumented tests, which run Screen and line 8.
    pub const CONNECTED: &str = r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><!DOCTYPE report PUBLIC "-//JACOCO//DTD Report 1.1//EN" "report.dtd"><report name="debug"><package name="dev/example/app"><class name="dev/example/app/Counter" sourcefilename="Counter.kt"><counter type="INSTRUCTION" missed="8" covered="2"/><counter type="LINE" missed="3" covered="1"/><counter type="CLASS" missed="0" covered="1"/></class><class name="dev/example/app/Screen" sourcefilename="Screen.kt"><counter type="INSTRUCTION" missed="0" covered="10"/><counter type="LINE" missed="0" covered="3"/><counter type="METHOD" missed="0" covered="1"/><counter type="CLASS" missed="0" covered="1"/></class><sourcefile name="Counter.kt"><line nr="5" mi="3" ci="0" mb="0" cb="0"/><line nr="6" mi="3" ci="0" mb="0" cb="0"/><line nr="8" mi="0" ci="2" mb="0" cb="2"/><line nr="9" mi="2" ci="0" mb="0" cb="0"/></sourcefile><sourcefile name="Screen.kt"><line nr="3" mi="0" ci="4" mb="0" cb="0"/><line nr="4" mi="0" ci="3" mb="0" cb="0"/><line nr="5" mi="0" ci="3" mb="0" cb="0"/></sourcefile></package><counter type="INSTRUCTION" missed="8" covered="12"/><counter type="LINE" missed="3" covered="4"/></report>"#;

    fn write(root: &Path, relative: &str, text: &str) -> PathBuf {
        let file = root.join(relative);
        fs::create_dir_all(file.parent().unwrap()).unwrap();
        fs::write(&file, text).unwrap();
        file
    }

    #[test]
    fn one_report_gives_totals_files_classes_and_missed_lines() {
        let root = tempfile::tempdir().unwrap();
        write(
            root.path(),
            "app/build/reports/coverage/test/debug/report.xml",
            UNIT,
        );
        write(
            root.path(),
            "app/build/reports/lint-results-debug.xml",
            "<issues/>",
        );
        let value = coverage(&json!({"path":root.path(),"minimum":50.0})).unwrap();
        assert_eq!(value["merged"], false);
        assert_eq!(
            value["totals"]["line"],
            json!({"covered":2,"missed":5,"total":7,"percent":28.57})
        );
        assert_eq!(value["totals"]["branch"]["percent"], 50.0);
        assert_eq!(value["reports"][0]["kind"], "unit");
        assert_eq!(value["reports"][0]["module"], ":app");
        // Least covered first.
        assert_eq!(value["files"][0]["path"], "dev/example/app/Screen.kt");
        assert_eq!(value["files"][0]["missed_lines"], json!(["3-5"]));
        assert_eq!(value["files"][1]["missed_lines"], json!(["8-9"]));
        assert_eq!(value["classes"][0]["name"], "dev.example.app.Screen");
        assert_eq!(value["classes"][1]["source"], "dev/example/app/Counter.kt");
        assert_eq!(value["passed"], false);
        assert_eq!(value["checks"][0]["check"], "minimum");
    }

    #[test]
    fn unit_and_instrumented_reports_merge_line_by_line() {
        let root = tempfile::tempdir().unwrap();
        write(
            root.path(),
            "app/build/reports/coverage/test/debug/report.xml",
            UNIT,
        );
        write(
            root.path(),
            "app/build/reports/coverage/androidTest/debug/connected/report.xml",
            CONNECTED,
        );
        let value = coverage(&json!({"path":root.path()})).unwrap();
        assert_eq!(value["merged"], true);
        let kinds: Vec<&str> = value["reports"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| s(r, "kind"))
            .collect();
        assert_eq!(kinds, ["instrumented", "unit"]);
        // Lines 5, 6 and 8 of Counter and all of Screen; only line 9 missed.
        assert_eq!(
            value["totals"]["line"],
            json!({"covered":6,"missed":1,"total":7,"percent":85.71})
        );
        assert_eq!(value["totals"]["branch"]["percent"], 100.0);
        assert_eq!(value["files"][0]["missed_lines"], json!(["9"]));
        // Each class from the report that covers it most.
        let screen = value["classes"]
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["name"] == "dev.example.app.Screen")
            .unwrap();
        assert_eq!(screen["line"]["percent"], 100.0);
    }

    #[test]
    fn a_baseline_report_gives_deltas_and_a_drop_check() {
        let root = tempfile::tempdir().unwrap();
        let before = write(root.path(), "before/report.xml", CONNECTED);
        write(
            root.path(),
            "app/build/reports/coverage/test/debug/report.xml",
            UNIT,
        );
        let saved = root.path().join("before.json");
        let first = coverage(&json!({"path":before,"report":saved})).unwrap();
        assert_eq!(first["totals"]["line"]["percent"], 57.14);
        let value = coverage(
            &json!({"path":root.path().join("app"),"baseline":saved,"max_drop":5.0,"limit":1}),
        )
        .unwrap();
        let delta = &value["delta"];
        assert_eq!(delta["totals"]["line"]["before"], 57.14);
        assert_eq!(delta["totals"]["line"]["after"], 28.57);
        assert_eq!(delta["totals"]["line"]["change"], -28.57);
        assert_eq!(delta["totals"]["line"]["covered_change"], -2);
        // The baseline's files had no module; these are new under :app.
        assert_eq!(delta["new_files"].as_array().unwrap().len(), 2);
        assert_eq!(delta["removed_files"].as_array().unwrap().len(), 2);
        assert_eq!(value["passed"], false);
        assert_eq!(value["files"].as_array().unwrap().len(), 1);
        assert_eq!(value["files_total"], 2);
        // A baseline of the same reports changes nothing.
        let xml = root
            .path()
            .join("app/build/reports/coverage/test/debug/report.xml");
        let same = coverage(&json!({"path":root.path().join("app"),"baseline":xml})).unwrap();
        // The XML baseline is read without a module, so compare the totals.
        assert_eq!(same["delta"]["totals"]["line"]["change"], 0.0);
        assert_eq!(same["passed"], true);
    }

    #[test]
    fn file_level_changes_are_listed_most_dropped_first() {
        let mut before = json!({"totals":{"line":{"percent":50.0,"covered":1}},"files":[
            {"module":":app","path":"a/A.kt","lines":{"percent":80.0}},
            {"module":":app","path":"a/B.kt","lines":{"percent":10.0}},
            {"module":":app","path":"a/Gone.kt","lines":{"percent":10.0}}]});
        let after = json!({"totals":{"line":{"percent":40.0,"covered":2}},"files":[
            {"module":":app","path":"a/A.kt","lines":{"percent":60.0}},
            {"module":":app","path":"a/B.kt","lines":{"percent":30.0}},
            {"module":":app","path":"a/New.kt","lines":{"percent":0.0}}]});
        let found = delta(&after, &before);
        assert_eq!(found["files"][0]["path"], "a/A.kt");
        assert_eq!(found["files"][0]["change"], -20.0);
        assert_eq!(found["files"][1]["change"], 20.0);
        assert_eq!(found["regressions"], 1);
        assert_eq!(found["new_files"][0]["path"], "a/New.kt");
        assert_eq!(found["removed_files"][0]["path"], "a/Gone.kt");
        assert_eq!(found["totals"]["line"]["change"], -10.0);
        before["files"] = json!([]);
        assert_eq!(
            delta(&after, &before)["new_files"]
                .as_array()
                .unwrap()
                .len(),
            3
        );
    }

    #[test]
    fn kover_reports_and_missing_or_stale_reports() {
        let root = tempfile::tempdir().unwrap();
        let kover = UNIT.replace("<report name=\"debug\">", "<report name=\"Kover report\">");
        write(root.path(), "build/reports/kover/report.xml", &kover);
        let value = coverage(&json!({"path":root.path()})).unwrap();
        assert_eq!(value["reports"][0]["kind"], "kover");
        assert_eq!(value["reports"][0]["module"], ":");
        let stale = coverage(&json!({"path":root.path(),"since":now() + 3600.0})).unwrap_err();
        assert_eq!(stale.code, "no_coverage_reports");
        assert!(stale.message.contains("older than the run"));
        let empty = tempfile::tempdir().unwrap();
        assert_eq!(
            coverage(&json!({"path":empty.path()})).unwrap_err().code,
            "no_coverage_reports"
        );
        let bad = write(root.path(), "bad.json", "{\"x\":1}");
        assert_eq!(
            coverage(&json!({"path":root.path(),"baseline":bad}))
                .unwrap_err()
                .code,
            "invalid_baseline"
        );
    }
}
