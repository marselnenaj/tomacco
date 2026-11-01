//! `project.analyze`: the static analysers a project already has, run once,
//! their findings read into one list of issues with file, line, severity and
//! code. Dart and Flutter projects use `dart analyze --format=json`; a
//! JavaScript or TypeScript project uses the ESLint and TypeScript compiler
//! installed in its node_modules. Nothing is installed or downloaded.
use crate::{core::*, toolchain as tc};
use regex::Regex;
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    process::Command,
};

/// At most this many issues are listed; the totals count all of them.
const MAX_ISSUES: usize = 2000;
const ANALYZERS: &[&str] = &["dart", "eslint", "tsc"];

fn issue(
    tool: &str,
    file: &str,
    line: u64,
    column: u64,
    severity: &str,
    code: &str,
    message: &str,
) -> Value {
    json!({"tool":tool,"file":file,"line":line,"column":column,"severity":severity,"code":code,"message":message.trim()})
}

/// A path relative to the project root when it lies inside it.
fn relative(file: &str, root: &Path) -> String {
    let file = file.strip_prefix("file://").unwrap_or(file);
    Path::new(file)
        .strip_prefix(root)
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|_| file.to_string())
}

/// `dart analyze --format=json`: one JSON object, possibly after other lines.
pub fn parse_dart(text: &str, root: &Path) -> Result<Vec<Value>> {
    let start = text
        .find("{\"version\"")
        .or_else(|| text.find('{'))
        .ok_or_else(|| Error::new("analyzer_output", "dart analyze printed no JSON"))?;
    let value: Value = serde_json::Deserializer::from_str(&text[start..])
        .into_iter::<Value>()
        .next()
        .ok_or_else(|| Error::new("analyzer_output", "dart analyze printed no JSON"))?
        .map_err(|e| Error::new("analyzer_output", format!("dart analyze JSON: {e}")))?;
    Ok(value["diagnostics"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|d| {
            let location = &d["location"];
            let start = &location["range"]["start"];
            let mut found = issue(
                "dart",
                &relative(s(location, "file"), root),
                n(start, "line"),
                n(start, "column"),
                &s(d, "severity").to_lowercase(),
                s(d, "code"),
                s(d, "problemMessage"),
            );
            found["type"] = json!(s(d, "type"));
            if !s(d, "correctionMessage").is_empty() {
                found["correction"] = json!(s(d, "correctionMessage"));
            }
            if !s(d, "documentation").is_empty() {
                found["documentation"] = json!(s(d, "documentation"));
            }
            found
        })
        .collect())
}

/// `eslint --format json`: an array of files with their messages.
pub fn parse_eslint(text: &str, root: &Path) -> Result<Vec<Value>> {
    let start = text
        .find('[')
        .ok_or_else(|| Error::new("analyzer_output", "eslint printed no JSON"))?;
    let files: Value = serde_json::Deserializer::from_str(&text[start..])
        .into_iter::<Value>()
        .next()
        .ok_or_else(|| Error::new("analyzer_output", "eslint printed no JSON"))?
        .map_err(|e| Error::new("analyzer_output", format!("eslint JSON: {e}")))?;
    let mut found = Vec::new();
    for file in files.as_array().into_iter().flatten() {
        let path = relative(s(file, "filePath"), root);
        for m in file["messages"].as_array().into_iter().flatten() {
            let severity = if n(m, "severity") >= 2 || b(m, "fatal") {
                "error"
            } else {
                "warning"
            };
            let code =
                m["ruleId"]
                    .as_str()
                    .unwrap_or(if b(m, "fatal") { "parse-error" } else { "" });
            found.push(issue(
                "eslint",
                &path,
                n(m, "line"),
                n(m, "column"),
                severity,
                code,
                s(m, "message"),
            ));
        }
    }
    Ok(found)
}

/// `tsc --noEmit --pretty false`: `file(line,col): error TS1234: message`,
/// with indented continuation lines, or `error TS1234: message` for the
/// whole program.
pub fn parse_tsc(text: &str, root: &Path) -> Vec<Value> {
    let located =
        Regex::new(r"^(.+?)\((\d+),(\d+)\): (error|warning|message) (TS\d+): (.*)$").unwrap();
    let global = Regex::new(r"^(error|warning|message) (TS\d+): (.*)$").unwrap();
    let mut found: Vec<Value> = Vec::new();
    for line in text.lines() {
        let severity = |word: &str| if word == "message" { "info" } else { word }.to_string();
        if let Some(c) = located.captures(line) {
            found.push(issue(
                "tsc",
                &relative(&c[1], root),
                c[2].parse().unwrap_or(0),
                c[3].parse().unwrap_or(0),
                &severity(&c[4]),
                &c[5],
                &c[6],
            ));
        } else if let Some(c) = global.captures(line) {
            found.push(issue("tsc", "", 0, 0, &severity(&c[1]), &c[2], &c[3]));
        } else if line.starts_with(char::is_whitespace) && !line.trim().is_empty() {
            if let Some(last) = found.last_mut() {
                let message = format!("{}\n{}", s(last, "message"), line.trim());
                last["message"] = json!(message);
            }
        }
    }
    found
}

/// The analysers this project has, each with its argv and working directory.
fn plan(root: &Path, kinds: &[&str], wanted: &[String]) -> Result<Vec<Value>> {
    let want = |name: &str| wanted.is_empty() || wanted.iter().any(|w| w == name);
    let mut planned = Vec::new();
    let mut missing = Vec::new();
    if root.join("pubspec.yaml").is_file() && want("dart") {
        // Flutter ships its Dart SDK in bin/ beside the flutter command.
        let dart = tc::find_optional("dart")?.or_else(|| {
            kinds
                .contains(&"flutter")
                .then(|| tc::find_optional("flutter").ok().flatten())
                .flatten()
                .map(|f| Path::new(&f).with_file_name("dart"))
                .filter(|d| tc::executable(d))
                .map(|d| d.to_string_lossy().into_owned())
        });
        match dart {
            Some(dart) => planned.push(
                json!({"tool":"dart","argv":[dart,"analyze","--format=json","."],"cwd":root}),
            ),
            None => missing.push("dart (or flutter, whose bin/dart it uses)".to_string()),
        }
    }
    if root.join("package.json").is_file() {
        let local = |name: &str| root.join("node_modules/.bin").join(name);
        if want("eslint") {
            if tc::executable(&local("eslint")) {
                planned.push(json!({"tool":"eslint","argv":[local("eslint"),"--format","json","."],"cwd":root}));
            } else if !wanted.is_empty() {
                missing.push("eslint in node_modules/.bin".into());
            }
        }
        if want("tsc") {
            if root.join("tsconfig.json").is_file() && tc::executable(&local("tsc")) {
                planned.push(json!({"tool":"tsc","argv":[local("tsc"),"--noEmit","--pretty","false"],"cwd":root}));
            } else if !wanted.is_empty() {
                missing.push("tsc in node_modules/.bin with a tsconfig.json".into());
            }
        }
    }
    for name in wanted {
        if !planned.iter().any(|p| s(p, "tool") == name)
            && !missing.iter().any(|m| m.starts_with(name.as_str()))
        {
            missing.push(format!("{name}: does not apply to this project"));
        }
    }
    if !missing.is_empty() && (!wanted.is_empty() || planned.is_empty()) {
        return Err(Error::new(
            "tool_missing",
            format!("Cannot run: {}", missing.join("; ")),
        )
        .remedy("Install Flutter or the Dart SDK for Dart projects; for JavaScript projects install eslint or typescript with the project's package manager"));
    }
    if planned.is_empty() {
        return Err(Error::new(
            "nothing_to_analyze",
            "No analyser applies: the project has no pubspec.yaml, and no eslint or tsc (with tsconfig.json) in node_modules/.bin",
        )
        .remedy("Install eslint or typescript as a dev dependency, or open a Dart or Flutter project"));
    }
    Ok(planned)
}

pub fn analyze(a: &Value) -> Result {
    let project = crate::projects::inspect(s(a, "path"))?;
    let root = PathBuf::from(s(&project, "path"));
    let kinds: Vec<&str> = project["kinds"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .collect();
    let wanted = strings(&a["tools"]);
    if let Some(bad) = wanted.iter().find(|w| !ANALYZERS.contains(&w.as_str())) {
        return Err(Error::new(
            "invalid_argument",
            format!("Unknown analyser {bad}"),
        ));
    }
    let planned = plan(&root, &kinds, &wanted)?;
    let directory = home()?
        .join("analysis")
        .join(uuid::Uuid::new_v4().simple().to_string());
    private_dir(&directory)?;
    let request = directory.join("request.json");
    atomic_json(
        &request,
        &json!({"project":root,"analyzers":planned,"report":directory.join("report.json")}),
    )?;
    let timeout = a["timeout"].as_u64().unwrap_or(900);
    crate::jobs::submit(
        &format!("Analyze · {}", s(&project, "name")),
        json!({"steps":[{"argv":[own_executable()?,"_project_analyze",request]}],"cwd":directory,"env":tc::environment()?,"timeout":timeout,"always_artifacts":["report.json"],"metadata":{"project":root}}),
    )
}

fn tail(text: &str) -> String {
    let lines: Vec<&str> = text.trim().lines().collect();
    lines[lines.len().saturating_sub(20)..].join("\n")
}

/// The report of the analysers' outputs: `(tool, exit code, stdout, stderr)`.
pub fn report(root: &Path, outputs: &[(String, Vec<String>, i32, String, String)]) -> Value {
    let mut issues = Vec::new();
    let mut tools = Vec::new();
    for (tool, argv, code, stdout, stderr) in outputs {
        let parsed = match tool.as_str() {
            "dart" => parse_dart(stdout, root),
            "eslint" => parse_eslint(stdout, root),
            _ => Ok(parse_tsc(stdout, root)),
        };
        let mut entry = json!({"tool":tool,"argv":argv,"exit_code":code});
        match parsed {
            // tsc prints nothing when clean, and exits non-zero only with errors.
            Ok(found) if tool == "tsc" && found.is_empty() && *code != 0 => {
                entry["error"] = json!(tail(&format!("{stdout}\n{stderr}")));
            }
            Ok(found) => {
                entry["issues"] = json!(found.len());
                issues.extend(found);
            }
            Err(error) => {
                entry["error"] = json!(format!(
                    "{}: {}",
                    error.message,
                    tail(&format!("{stdout}\n{stderr}"))
                ));
            }
        }
        tools.push(entry);
    }
    let count = |severity: &str| issues.iter().filter(|i| i["severity"] == severity).count();
    let (errors, warnings, infos) = (count("error"), count("warning"), count("info"));
    let failed_tools = tools.iter().filter(|t| t.get("error").is_some()).count();
    let total = issues.len();
    issues.truncate(MAX_ISSUES);
    json!({
        "project":root,
        "passed": errors == 0 && failed_tools == 0,
        "totals":{"issues":total,"error":errors,"warning":warnings,"info":infos},
        "tools":tools,
        "issues":issues,
        "issues_truncated": total > MAX_ISSUES,
    })
}

/// The `_project_analyze` worker: runs each analyser in the project and
/// writes report.json. It fails only when no analyser produced a result.
pub fn worker(request: &str) -> Result<()> {
    let spec = read_json(Path::new(request), None)?;
    let root = PathBuf::from(s(&spec, "project"));
    let mut outputs = Vec::new();
    for analyzer in spec["analyzers"].as_array().into_iter().flatten() {
        let args = strings(&analyzer["argv"]);
        let tool = s(analyzer, "tool").to_string();
        eprintln!("$ {}", shell_join(&args));
        let output = Command::new(&args[0])
            .args(&args[1..])
            .current_dir(s(analyzer, "cwd"))
            .output();
        match output {
            Ok(o) => outputs.push((
                tool,
                args,
                o.status.code().unwrap_or(-1),
                String::from_utf8_lossy(&o.stdout).into_owned(),
                String::from_utf8_lossy(&o.stderr).into_owned(),
            )),
            Err(e) => outputs.push((tool, args, -1, String::new(), e.to_string())),
        }
    }
    let report = report(&root, &outputs);
    atomic_json(Path::new(s(&spec, "report")), &report)?;
    for tool in report["tools"].as_array().into_iter().flatten() {
        match tool.get("error") {
            Some(error) => eprintln!(
                "{}: did not run: {}",
                s(tool, "tool"),
                error.as_str().unwrap_or("")
            ),
            None => eprintln!("{}: {} issues", s(tool, "tool"), tool["issues"]),
        }
    }
    let totals = &report["totals"];
    println!(
        "{} errors, {} warnings, {} infos",
        totals["error"], totals["warning"], totals["info"]
    );
    if report["tools"]
        .as_array()
        .is_some_and(|t| t.iter().all(|t| t.get("error").is_some()))
    {
        return Err(Error::new(
            "analyzer_failed",
            "No analyser produced a result; see report.json",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    const DART: &str = r#"Analyzing app...
{"version":1,"diagnostics":[{"code":"unused_import","severity":"WARNING","type":"STATIC_WARNING","location":{"file":"/p/app/lib/main.dart","range":{"start":{"offset":7,"line":1,"column":8},"end":{"offset":20,"line":1,"column":21}}},"problemMessage":"Unused import: 'dart:io'.","correctionMessage":"Try removing the import directive.","documentation":"https://dart.dev/diagnostics/unused_import"},{"code":"undefined_identifier","severity":"ERROR","type":"COMPILE_TIME_ERROR","location":{"file":"/p/app/lib/main.dart","range":{"start":{"offset":40,"line":4,"column":3},"end":{"offset":44,"line":4,"column":7}}},"problemMessage":"Undefined name 'nope'."}]}"#;

    #[test]
    fn dart_json_diagnostics_become_issues() {
        let found = parse_dart(DART, Path::new("/p/app")).unwrap();
        assert_eq!(found.len(), 2);
        assert_eq!(found[0]["file"], "lib/main.dart");
        assert_eq!(found[0]["line"], 1);
        assert_eq!(found[0]["column"], 8);
        assert_eq!(found[0]["severity"], "warning");
        assert_eq!(found[0]["code"], "unused_import");
        assert_eq!(found[0]["correction"], "Try removing the import directive.");
        assert_eq!(found[1]["severity"], "error");
        assert!(parse_dart("No issues found!", Path::new("/p")).is_err());
    }

    #[test]
    fn eslint_and_tsc_output_become_issues() {
        let eslint = r#"[{"filePath":"/p/web/src/a.js","messages":[{"ruleId":"no-unused-vars","severity":2,"message":"'x' is assigned a value but never used.","line":3,"column":7},{"ruleId":"semi","severity":1,"message":"Missing semicolon.","line":5,"column":2}],"errorCount":1,"warningCount":1},{"filePath":"/p/web/src/b.js","messages":[{"ruleId":null,"fatal":true,"severity":2,"message":"Parsing error: Unexpected token","line":1,"column":1}]}]"#;
        let found = parse_eslint(eslint, Path::new("/p/web")).unwrap();
        assert_eq!(found.len(), 3);
        assert_eq!(found[0]["file"], "src/a.js");
        assert_eq!(found[0]["severity"], "error");
        assert_eq!(found[1]["severity"], "warning");
        assert_eq!(found[1]["code"], "semi");
        assert_eq!(found[2]["code"], "parse-error");
        let tsc = "src/a.ts(3,7): error TS2322: Type 'string' is not assignable to type 'number'.\n  The expected type comes from here.\nerror TS5058: The specified path does not exist: 'tsconfig.app.json'.\n";
        let found = parse_tsc(tsc, Path::new("/p/web"));
        assert_eq!(found.len(), 2);
        assert_eq!(found[0]["file"], "src/a.ts");
        assert_eq!(found[0]["line"], 3);
        assert_eq!(found[0]["code"], "TS2322");
        assert!(s(&found[0], "message").ends_with("The expected type comes from here."));
        assert_eq!(found[1]["file"], "");
    }

    #[test]
    fn the_report_counts_severities_and_names_tools_that_did_not_run() {
        let root = Path::new("/p/app");
        let report = report(
            root,
            &[
                (
                    "dart".into(),
                    vec!["dart".into()],
                    3,
                    DART.into(),
                    String::new(),
                ),
                (
                    "tsc".into(),
                    vec!["tsc".into()],
                    0,
                    String::new(),
                    String::new(),
                ),
                (
                    "eslint".into(),
                    vec!["eslint".into()],
                    2,
                    String::new(),
                    "Oops! Something went wrong!".into(),
                ),
            ],
        );
        assert_eq!(report["passed"], false);
        assert_eq!(
            report["totals"],
            json!({"issues":2,"error":1,"warning":1,"info":0})
        );
        assert_eq!(report["tools"][1]["issues"], 0);
        assert!(s(&report["tools"][2], "error").contains("Something went wrong"));
    }
}
