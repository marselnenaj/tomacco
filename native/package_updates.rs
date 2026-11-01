//! `project.dependency_updates` beyond Gradle: pub (`pub outdated --json`),
//! npm, pnpm and Yarn (their `outdated` JSON) and SwiftPM (the pins of
//! Package.resolved against the tags of each repository, from
//! `git ls-remote`). Each reads the project and changes nothing in it.
use crate::{
    core::*,
    dependencies::{assess, compare},
    javascript, toolchain as tc,
};
use serde_json::{Map, Value, json};
use std::{
    cmp::Ordering,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};

/// What a project has beyond Gradle, each as a request entry for the worker;
/// a tool that is missing is an entry with an error.
pub fn plan(root: &Path, kinds: &[&str]) -> Result<Vec<Value>> {
    let mut found = Vec::new();
    if root.join("pubspec.yaml").is_file() {
        let tool = if kinds.contains(&"flutter") {
            "flutter"
        } else {
            "dart"
        };
        found.push(match tc::find(tool) {
            Ok(path) => {
                json!({"name":"pub","tool":tool,"argv":[path,"pub","outdated","--json"],"cwd":root})
            }
            Err(error) => json!({"name":"pub","tool":tool,"error":error.message}),
        });
    }
    if root.join("package.json").is_file() {
        let package = read_json(&root.join("package.json"), None)?;
        let mut declared = Map::new();
        for (key, kind) in [("dependencies", "direct"), ("devDependencies", "dev")] {
            for (name, range) in package[key].as_object().into_iter().flatten() {
                declared.insert(
                    name.clone(),
                    json!({"range":range.as_str().unwrap_or(""),"kind":kind}),
                );
            }
        }
        if !declared.is_empty() {
            let manager = match javascript::package_manager(root) {
                "pnpm" => "pnpm",
                "yarn" => "yarn",
                // Bun has no JSON report; npm reads the same node_modules.
                _ => "npm",
            };
            let args: &[&str] = match manager {
                "pnpm" => &["outdated", "--format", "json"],
                _ => &["outdated", "--json"],
            };
            let mut entry = json!({"name":"npm","tool":manager,"cwd":root,"declared":declared});
            match tc::find(manager) {
                Ok(path) => {
                    let mut argv = vec![path];
                    argv.extend(args.iter().map(|a| a.to_string()));
                    entry["argv"] = json!(argv);
                }
                Err(error) => entry["error"] = json!(error.message),
            }
            found.push(entry);
        }
    }
    if let Some(resolved) = package_resolved(root) {
        let text = std::fs::read_to_string(&resolved)?;
        let pins = pins(&text)?;
        let mut entry = json!({"name":"swiftpm","tool":"git","pins":pins,
            "source":resolved.strip_prefix(root).unwrap_or(&resolved)});
        match tc::find("git") {
            Ok(path) => entry["git"] = json!(path),
            Err(error) => entry["error"] = json!(error.message),
        }
        found.push(entry);
    }
    Ok(found)
}

/// Package.resolved of a SwiftPM package, or of an Xcode project or workspace.
pub fn package_resolved(root: &Path) -> Option<PathBuf> {
    let direct = root.join("Package.resolved");
    if direct.is_file() {
        return Some(direct);
    }
    let base = glob::Pattern::escape(&root.to_string_lossy());
    [
        "*.xcworkspace/xcshareddata/swiftpm/Package.resolved",
        "*.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved",
    ]
    .iter()
    .flat_map(|p| glob_paths(&format!("{base}/{p}")).unwrap_or_default())
    .find(|p| p.is_file())
}

/// The pins of Package.resolved, version 1 (`object.pins`) or 2 and 3.
pub fn pins(text: &str) -> Result<Vec<Value>> {
    let value: Value = serde_json::from_str(text)
        .map_err(|e| Error::new("invalid_project", format!("Package.resolved: {e}")))?;
    let list = value["pins"]
        .as_array()
        .or_else(|| value["object"]["pins"].as_array())
        .cloned()
        .unwrap_or_default();
    Ok(list
        .iter()
        .map(|pin| {
            let name = [s(pin, "identity"), s(pin, "package")]
                .into_iter()
                .find(|v| !v.is_empty())
                .unwrap_or_default();
            let location = [s(pin, "location"), s(pin, "repositoryURL")]
                .into_iter()
                .find(|v| !v.is_empty())
                .unwrap_or_default();
            let state = &pin["state"];
            json!({"name":name,"location":location,"version":state["version"].as_str().unwrap_or(""),
                   "revision":s(state, "revision"),"branch":state["branch"].as_str().unwrap_or("")})
        })
        .collect())
}

/// One entry of the report for a pub package from `pub outdated --json`.
pub fn pub_entries(text: &str) -> Result<Vec<Value>> {
    let start = text
        .find('{')
        .ok_or_else(|| Error::new("tool_output", "pub outdated printed no JSON"))?;
    let value: Value = serde_json::Deserializer::from_str(&text[start..])
        .into_iter::<Value>()
        .next()
        .ok_or_else(|| Error::new("tool_output", "pub outdated printed no JSON"))?
        .map_err(|e| Error::new("tool_output", format!("pub outdated JSON: {e}")))?;
    Ok(value["packages"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|p| matches!(s(p, "kind"), "direct" | "dev"))
        .map(|p| {
            let version = |key: &str| p[key]["version"].as_str().unwrap_or("").to_string();
            let (current, latest) = (version("current"), version("latest"));
            let status = if current.is_empty() {
                "not_installed"
            } else if !latest.is_empty() && compare(&current, &latest) == Ordering::Less {
                "update_available"
            } else {
                "up_to_date"
            };
            let mut entry = json!({"ecosystem":"pub","kind":s(p, "kind"),"name":s(p, "package"),
                "version":current,"upgradable":version("upgradable"),"resolvable":version("resolvable"),
                "latest_stable":latest,"latest":latest,"status":status,"source":"pubspec.yaml"});
            if b(p, "isDiscontinued") {
                entry["discontinued"] = json!(true);
            }
            entry
        })
        .collect())
}

/// The outdated packages of `npm outdated --json`, `pnpm outdated --format
/// json` or Yarn 1's `yarn outdated --json`, by name: current, wanted, latest.
fn outdated(manager: &str, text: &str) -> Result<Map<String, Value>> {
    if manager == "yarn" {
        let mut found = Map::new();
        for line in text.lines() {
            let Ok(event) = serde_json::from_str::<Value>(line.trim()) else {
                continue;
            };
            if s(&event, "type") != "table" {
                continue;
            }
            for row in event["data"]["body"].as_array().into_iter().flatten() {
                let cell = |i: usize| row[i].as_str().unwrap_or("").to_string();
                found.insert(
                    cell(0),
                    json!({"current":cell(1),"wanted":cell(2),"latest":cell(3)}),
                );
            }
        }
        return Ok(found);
    }
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return Ok(Map::new());
    }
    let value: Value = serde_json::from_str(trimmed)
        .map_err(|e| Error::new("tool_output", format!("{manager} outdated JSON: {e}")))?;
    if let Some(error) = value.get("error") {
        return Err(Error::new(
            "tool_output",
            format!(
                "{manager} outdated: {}",
                error["summary"].as_str().unwrap_or(&error.to_string())
            ),
        ));
    }
    Ok(value
        .as_object()
        .cloned()
        .unwrap_or_default()
        .into_iter()
        // npm lists one entry per workspace location as an array.
        .map(|(name, info)| match info {
            Value::Array(list) => (name, list.into_iter().next().unwrap_or(Value::Null)),
            other => (name, other),
        })
        .collect())
}

/// One entry per declared dependency: npm and its kin list only outdated
/// packages, so one they leave out is up to date.
pub fn npm_entries(manager: &str, text: &str, declared: &Value) -> Result<Vec<Value>> {
    let outdated = outdated(manager, text)?;
    Ok(declared
        .as_object()
        .into_iter()
        .flatten()
        .map(|(name, info)| {
            let mut entry = json!({"ecosystem":"npm","kind":s(info, "kind"),"name":name,
                "declared":s(info, "range"),"source":"package.json"});
            match outdated.get(name) {
                None => {
                    entry["status"] = json!("up_to_date");
                }
                Some(o) => {
                    let current = s(o, "current");
                    let latest = s(o, "latest");
                    entry["version"] = json!(current);
                    entry["wanted"] = json!(s(o, "wanted"));
                    entry["latest_stable"] = json!(latest);
                    entry["latest"] = json!(latest);
                    entry["status"] = json!(if current.is_empty() || current == "MISSING" {
                        "not_installed"
                    } else if latest.is_empty() || compare(current, latest) != Ordering::Less {
                        "up_to_date"
                    } else {
                        "update_available"
                    });
                }
            }
            entry
        })
        .collect())
}

/// The release versions among `git ls-remote --tags` output, `v` prefixes removed.
pub fn tag_versions(text: &str) -> Vec<String> {
    let version = regex::Regex::new(r"^\d+(\.\d+)*([-+][0-9A-Za-z.-]+)?$").unwrap();
    text.lines()
        .filter_map(|line| line.split('\t').nth(1))
        .filter_map(|r| r.strip_prefix("refs/tags/"))
        .map(|t| t.trim_end_matches("^{}"))
        .map(|t| t.strip_prefix('v').unwrap_or(t).to_string())
        .filter(|t| version.is_match(t))
        .collect()
}

/// A SwiftPM pin assessed against its repository's tags.
pub fn swift_entry(
    pin: &Value,
    source: &str,
    tags: std::result::Result<Vec<String>, String>,
) -> Value {
    let mut entry = json!({"ecosystem":"swiftpm","kind":"package","name":s(pin, "name"),
        "location":s(pin, "location"),"version":s(pin, "version"),"source":source});
    if s(pin, "version").is_empty() {
        entry["status"] = json!("unversioned");
        entry["revision"] = pin["revision"].clone();
        if !s(pin, "branch").is_empty() {
            entry["branch"] = pin["branch"].clone();
        }
        return entry;
    }
    match tags {
        Ok(tags) if tags.is_empty() => entry["status"] = json!("not_found"),
        Ok(tags) => {
            let assessed = assess(s(pin, "version"), &tags);
            for key in ["latest_stable", "latest", "status"] {
                entry[key] = assessed[key].clone();
            }
        }
        Err(error) => {
            entry["status"] = json!("unreachable");
            entry["error"] = json!(error);
        }
    }
    entry
}

/// Runs a command for at most `seconds`, without a terminal to prompt on.
fn output(
    argv: &[String],
    cwd: Option<&str>,
    seconds: u64,
) -> std::result::Result<(i32, String, String), String> {
    let (program, rest) = argv.split_first().ok_or("missing command")?;
    let mut command = Command::new(program);
    command
        .args(rest)
        .env("GIT_TERMINAL_PROMPT", "0")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(cwd) = cwd {
        command.current_dir(cwd);
    }
    let mut child = command.spawn().map_err(|e| format!("{program}: {e}"))?;
    let (mut out, mut err) = (child.stdout.take().unwrap(), child.stderr.take().unwrap());
    let reader = thread::spawn(move || {
        let mut text = String::new();
        let _ = std::io::Read::read_to_string(&mut out, &mut text);
        text
    });
    let errors = thread::spawn(move || {
        let mut text = String::new();
        let _ = std::io::Read::read_to_string(&mut err, &mut text);
        text
    });
    let deadline = Instant::now() + Duration::from_secs(seconds);
    let status = loop {
        if let Some(status) = child.try_wait().map_err(|e| e.to_string())? {
            break status;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!("no answer within {seconds} s"));
        }
        thread::sleep(Duration::from_millis(50));
    };
    Ok((
        status.code().unwrap_or(-1),
        reader.join().unwrap_or_default(),
        errors.join().unwrap_or_default(),
    ))
}

fn tail(text: &str) -> String {
    let lines: Vec<&str> = text.trim().lines().collect();
    lines[lines.len().saturating_sub(10)..].join("\n")
}

/// The entries of one ecosystem from the worker's request.
pub fn run(ecosystem: &Value) -> Result<Vec<Value>> {
    if let Some(error) = ecosystem["error"].as_str() {
        return Err(Error::new("tool_missing", error));
    }
    match s(ecosystem, "name") {
        "pub" | "npm" => {
            let argv = strings(&ecosystem["argv"]);
            let (code, stdout, stderr) = output(&argv, Some(s(ecosystem, "cwd")), 300)
                .map_err(|e| Error::new("tool_failed", e))?;
            let parsed = if s(ecosystem, "name") == "pub" {
                pub_entries(&stdout)
            } else {
                npm_entries(s(ecosystem, "tool"), &stdout, &ecosystem["declared"])
            };
            parsed.map_err(|e| {
                Error::new(
                    "tool_failed",
                    format!(
                        "{} (exit {code}): {}",
                        e.message,
                        tail(&format!("{stdout}\n{stderr}"))
                    ),
                )
            })
        }
        "swiftpm" => {
            let git = s(ecosystem, "git").to_string();
            let source = s(ecosystem, "source").to_string();
            let pins = ecosystem["pins"].as_array().cloned().unwrap_or_default();
            Ok(thread::scope(|scope| {
                pins.chunks(pins.len().div_ceil(8).max(1))
                    .map(|chunk| {
                        let (git, source) = (&git, &source);
                        scope.spawn(move || {
                            chunk
                                .iter()
                                .map(|pin| {
                                    let location = s(pin, "location");
                                    let tags = if s(pin, "version").is_empty() {
                                        Ok(Vec::new())
                                    } else if location.is_empty() || location.starts_with('-') {
                                        Err(format!("invalid location {location:?}"))
                                    } else {
                                        output(
                                            &argv(&[
                                                git.as_str(),
                                                "ls-remote",
                                                "--tags",
                                                "--refs",
                                                location,
                                            ]),
                                            None,
                                            30,
                                        )
                                        .and_then(
                                            |(code, out, err)| {
                                                if code == 0 {
                                                    Ok(tag_versions(&out))
                                                } else {
                                                    Err(tail(&err))
                                                }
                                            },
                                        )
                                    };
                                    swift_entry(pin, source, tags)
                                })
                                .collect::<Vec<_>>()
                        })
                    })
                    .collect::<Vec<_>>()
                    .into_iter()
                    .flat_map(|h| h.join().unwrap_or_default())
                    .collect()
            }))
        }
        other => Err(Error::new(
            "invalid_request",
            format!("Unknown ecosystem {other}"),
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pub_outdated_lists_direct_and_dev_packages() {
        let text = r#"Resolving...
{"packages":[
 {"package":"http","kind":"direct","isDiscontinued":false,"current":{"version":"1.1.0"},"upgradable":{"version":"1.2.2"},"resolvable":{"version":"1.2.2"},"latest":{"version":"1.2.2"}},
 {"package":"flutter_lints","kind":"dev","isDiscontinued":true,"current":{"version":"5.0.0"},"upgradable":{"version":"5.0.0"},"resolvable":{"version":"5.0.0"},"latest":{"version":"5.0.0"}},
 {"package":"meta","kind":"transitive","current":{"version":"1.0.0"},"latest":{"version":"1.16.0"}},
 {"package":"intl","kind":"direct","current":null,"upgradable":null,"resolvable":{"version":"0.20.2"},"latest":{"version":"0.20.2"}}
]}"#;
        let entries = pub_entries(text).unwrap();
        assert_eq!(entries.len(), 3);
        assert_eq!(entries[0]["status"], "update_available");
        assert_eq!(entries[0]["latest"], "1.2.2");
        assert_eq!(entries[1]["status"], "up_to_date");
        assert_eq!(entries[1]["discontinued"], true);
        assert_eq!(entries[2]["status"], "not_installed");
    }

    #[test]
    fn npm_pnpm_and_yarn_reports_cover_every_declared_dependency() {
        let declared = json!({"react":{"range":"^18.2.0","kind":"direct"},"left-pad":{"range":"1.0.0","kind":"direct"},"jest":{"range":"^29.0.0","kind":"dev"}});
        let npm = r#"{"react":{"current":"18.2.0","wanted":"18.3.1","latest":"19.1.0","dependent":"app","location":"node_modules/react","type":"dependencies"},"jest":{"wanted":"29.7.0","latest":"30.0.0","dependent":"app","location":"node_modules/jest","type":"devDependencies"}}"#;
        let entries = npm_entries("npm", npm, &declared).unwrap();
        let by = |name: &str| entries.iter().find(|e| e["name"] == name).unwrap().clone();
        assert_eq!(by("react")["status"], "update_available");
        assert_eq!(by("react")["wanted"], "18.3.1");
        assert_eq!(by("left-pad")["status"], "up_to_date");
        assert_eq!(by("jest")["status"], "not_installed");
        assert_eq!(by("jest")["kind"], "dev");
        let yarn = "{\"type\":\"info\",\"data\":\"Color legend\"}\n{\"type\":\"table\",\"data\":{\"head\":[\"Package\",\"Current\",\"Wanted\",\"Latest\",\"Package Type\",\"URL\"],\"body\":[[\"react\",\"18.2.0\",\"18.2.0\",\"19.1.0\",\"dependencies\",\"https://react.dev\"]]}}\n";
        let entries = npm_entries("yarn", yarn, &declared).unwrap();
        assert_eq!(
            entries
                .iter()
                .filter(|e| e["status"] == "update_available")
                .count(),
            1
        );
        let pnpm = r#"{"react":{"current":"19.1.0","latest":"19.1.0","wanted":"19.1.0","isDeprecated":false,"dependencyType":"dependencies"}}"#;
        let entries = npm_entries("pnpm", pnpm, &declared).unwrap();
        assert!(entries.iter().all(|e| e["status"] == "up_to_date"));
        assert!(npm_entries("npm", "", &declared).is_ok());
        let error = npm_entries(
            "npm",
            r#"{"error":{"code":"ENOTFOUND","summary":"request failed"}}"#,
            &declared,
        )
        .unwrap_err();
        assert!(error.message.contains("request failed"));
    }

    #[test]
    fn swift_pins_are_compared_with_repository_tags() {
        let v2 = r#"{"pins":[{"identity":"swift-log","kind":"remoteSourceControl","location":"https://github.com/apple/swift-log.git","state":{"revision":"abc","version":"1.5.0"}},{"identity":"local-fork","kind":"remoteSourceControl","location":"https://example.com/fork.git","state":{"branch":"main","revision":"def"}}],"version":2}"#;
        let pins = pins(v2).unwrap();
        assert_eq!(pins[0]["name"], "swift-log");
        let v1 = r#"{"object":{"pins":[{"package":"Alamofire","repositoryURL":"https://github.com/Alamofire/Alamofire.git","state":{"branch":null,"revision":"x","version":"5.8.0"}}]},"version":1}"#;
        assert_eq!(
            super::pins(v1).unwrap()[0]["location"],
            "https://github.com/Alamofire/Alamofire.git"
        );
        let tags = tag_versions(
            "aaa\trefs/tags/1.4.0\nbbb\trefs/tags/1.6.0\nccc\trefs/tags/v1.7.0-beta.1\nddd\trefs/tags/nightly\n",
        );
        assert_eq!(tags, vec!["1.4.0", "1.6.0", "1.7.0-beta.1"]);
        let entry = swift_entry(&pins[0], "Package.resolved", Ok(tags));
        assert_eq!(entry["status"], "update_available");
        assert_eq!(entry["latest_stable"], "1.6.0");
        assert_eq!(entry["latest"], "1.7.0-beta.1");
        assert_eq!(
            swift_entry(&pins[1], "Package.resolved", Ok(vec![]))["status"],
            "unversioned"
        );
        assert_eq!(
            swift_entry(&pins[0], "Package.resolved", Err("timeout".into()))["status"],
            "unreachable"
        );
    }
}
