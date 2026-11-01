//! `project.dependency_updates`: the versions a Gradle build declares, in its
//! versions catalog and build files, against the newest versions in Maven
//! repository metadata; pub, npm and SwiftPM dependencies are in
//! `package_updates`. Reads the project and the repositories; changes nothing.
use crate::{
    core::*,
    gradle_model::{self, Catalog},
    toolchain as tc,
};
use regex::Regex;
use serde_json::{Value, json};
use std::{
    cmp::Ordering,
    collections::BTreeMap,
    net::ToSocketAddrs,
    path::{Path, PathBuf},
    process::Command,
};

pub const REPOSITORIES: &[&str] = &[
    "https://dl.google.com/dl/android/maven2",
    "https://repo.maven.apache.org/maven2",
    "https://plugins.gradle.org/m2",
];

/// One declared dependency or plugin.
#[derive(Debug, Clone, PartialEq)]
pub struct Declared {
    pub kind: &'static str,
    pub name: String,
    pub group: String,
    pub artifact: String,
    pub version: String,
    pub source: String,
}

fn line_of(text: &str, offset: usize) -> usize {
    text[..offset].matches('\n').count() + 1
}

fn relative(path: &Path, root: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .into_owned()
}

/// Every dependency and plugin with a version in the versions catalog and the
/// build files of the build in `dir`.
pub fn declared(dir: &Path) -> Vec<Declared> {
    let mut found = Vec::new();
    if let Some(file) = gradle_model::catalog_file(dir) {
        if let Some(text) = gradle_model::read_text(&file) {
            let catalog: Catalog = gradle_model::parse_catalog(&text);
            let source = relative(&file, dir);
            for entry in &catalog.libraries {
                let Some((group, artifact)) = entry.module.split_once(':') else {
                    continue;
                };
                found.push(Declared {
                    kind: "library",
                    name: entry.key.clone(),
                    group: group.into(),
                    artifact: artifact.into(),
                    version: catalog.version_of(entry),
                    source: format!("{source}:{}", entry.line),
                });
            }
            for entry in &catalog.plugins {
                found.push(Declared {
                    kind: "plugin",
                    name: entry.key.clone(),
                    group: entry.module.clone(),
                    artifact: format!("{}.gradle.plugin", entry.module),
                    version: catalog.version_of(entry),
                    source: format!("{source}:{}", entry.line),
                });
            }
        }
    }
    let coordinate = Regex::new(r#"["']([\w.-]+):([\w.-]+):([^"'\s:@]+)(?:@\w+)?["']"#).unwrap();
    let plugin =
        Regex::new(r#"id\s*\(?\s*["']([\w.-]+)["']\s*\)?\s*version\s*\(?\s*["']([^"']+)["']"#)
            .unwrap();
    let mut files = Vec::new();
    for (_, module) in gradle_model::modules_of(dir) {
        for name in ["build.gradle.kts", "build.gradle"] {
            files.push(module.join(name));
        }
    }
    for name in ["settings.gradle.kts", "settings.gradle"] {
        files.push(dir.join(name));
    }
    for file in files {
        let Some(text) = gradle_model::read_text(&file) else {
            continue;
        };
        let text = gradle_model::strip_comments(&text);
        let source = relative(&file, dir);
        for c in coordinate.captures_iter(&text) {
            found.push(Declared {
                kind: "library",
                name: format!("{}:{}", &c[1], &c[2]),
                group: c[1].into(),
                artifact: c[2].into(),
                version: c[3].into(),
                source: format!("{source}:{}", line_of(&text, c.get(0).unwrap().start())),
            });
        }
        for c in plugin.captures_iter(&text) {
            found.push(Declared {
                kind: "plugin",
                name: c[1].into(),
                group: c[1].into(),
                artifact: format!("{}.gradle.plugin", &c[1]),
                version: c[2].into(),
                source: format!("{source}:{}", line_of(&text, c.get(0).unwrap().start())),
            });
        }
    }
    found
}

#[derive(Debug, PartialEq, Eq)]
enum Token {
    Number(u64),
    Text(String),
}

fn tokens(version: &str) -> Vec<Token> {
    let mut out = Vec::new();
    let mut current = String::new();
    let mut digits = false;
    let flush = |current: &mut String, out: &mut Vec<Token>, digits: bool| {
        if !current.is_empty() {
            out.push(if digits {
                Token::Number(current.parse().unwrap_or(u64::MAX))
            } else {
                Token::Text(current.to_lowercase())
            });
            current.clear();
        }
    };
    for c in version.chars() {
        if matches!(c, '.' | '-' | '_' | '+') {
            flush(&mut current, &mut out, digits);
            continue;
        }
        let is_digit = c.is_ascii_digit();
        if !current.is_empty() && is_digit != digits {
            flush(&mut current, &mut out, digits);
        }
        digits = is_digit;
        current.push(c);
    }
    flush(&mut current, &mut out, digits);
    out
}

fn rank(text: &str) -> u8 {
    match text {
        "dev" => 0,
        "rc" | "cr" => 2,
        "snapshot" => 3,
        "final" | "ga" | "release" => 4,
        "sp" => 5,
        _ => 1,
    }
}

/// Orders versions as Gradle does: numbers numerically and above words,
/// `dev` < other words < `rc` < `snapshot` < `final` < `sp`, and a release
/// above the same version with a qualifier.
pub fn compare(a: &str, b: &str) -> Ordering {
    let (a, b) = (tokens(a), tokens(b));
    for i in 0..a.len().max(b.len()) {
        let order = match (a.get(i), b.get(i)) {
            (Some(Token::Number(x)), Some(Token::Number(y))) => x.cmp(y),
            (Some(Token::Number(_)), Some(Token::Text(_))) => Ordering::Greater,
            (Some(Token::Text(_)), Some(Token::Number(_))) => Ordering::Less,
            (Some(Token::Text(x)), Some(Token::Text(y))) => {
                rank(x).cmp(&rank(y)).then_with(|| x.cmp(y))
            }
            (Some(Token::Number(_)), None) => Ordering::Greater,
            (Some(Token::Text(_)), None) => Ordering::Less,
            (None, Some(Token::Number(_))) => Ordering::Less,
            (None, Some(Token::Text(_))) => Ordering::Greater,
            (None, None) => Ordering::Equal,
        };
        if order != Ordering::Equal {
            return order;
        }
    }
    Ordering::Equal
}

/// Whether a version is a release rather than an alpha, beta, release
/// candidate, milestone, preview or snapshot.
pub fn stable(version: &str) -> bool {
    !tokens(version).iter().any(|t| match t {
        Token::Text(t) => matches!(
            t.as_str(),
            "alpha"
                | "a"
                | "beta"
                | "b"
                | "rc"
                | "cr"
                | "m"
                | "milestone"
                | "dev"
                | "eap"
                | "snapshot"
                | "preview"
                | "pre"
                | "canary"
                | "ea"
        ),
        Token::Number(_) => false,
    })
}

/// The `<version>` entries of a maven-metadata.xml.
pub fn metadata_versions(xml: &str) -> Result<Vec<String>> {
    let document = roxmltree::Document::parse(xml)
        .map_err(|e| Error::new("invalid_metadata", e.to_string()))?;
    Ok(document
        .descendants()
        .filter(|n| {
            n.has_tag_name("version") && n.parent().is_some_and(|p| p.has_tag_name("versions"))
        })
        .filter_map(|n| n.text())
        .map(|t| t.trim().to_string())
        .filter(|t| !t.is_empty())
        .collect())
}

/// One dependency's entry in the report, given the versions its repository lists.
pub fn assess(current: &str, versions: &[String]) -> Value {
    let latest = versions.iter().max_by(|a, b| compare(a, b)).cloned();
    let latest_stable = versions
        .iter()
        .filter(|v| stable(v))
        .max_by(|a, b| compare(a, b))
        .cloned();
    let status = if current.is_empty() {
        "managed"
    } else if current.contains('$') {
        "unresolved"
    } else if current.contains(['+', '[', '(', ',']) || current.starts_with("latest.") {
        "dynamic"
    } else {
        match (&latest_stable, &latest) {
            (Some(s), _) if compare(current, s) == Ordering::Less => "update_available",
            (_, Some(l)) if !stable(current) && compare(current, l) == Ordering::Less => {
                "prerelease_update_available"
            }
            _ => "up_to_date",
        }
    };
    json!({"latest_stable":latest_stable,"latest":latest,"status":status})
}

fn metadata_url(repository: &str, group: &str, artifact: &str) -> String {
    format!(
        "{}/{}/{artifact}/maven-metadata.xml",
        repository.trim_end_matches('/'),
        group.replace('.', "/")
    )
}

enum Fetched {
    Found(String),
    Missing,
    Unreachable(String),
}

fn fetch(curl: &str, url: &str) -> Fetched {
    let output = Command::new(curl)
        .args(["-fsSL", "--max-time", "20", "--proto", "=https,file", url])
        .output();
    match output {
        Ok(o) if o.status.success() => Fetched::Found(String::from_utf8_lossy(&o.stdout).into()),
        // 22: HTTP 4xx/5xx, 37: no such file
        Ok(o) if matches!(o.status.code(), Some(22) | Some(37)) => Fetched::Missing,
        Ok(o) => Fetched::Unreachable(String::from_utf8_lossy(&o.stderr).trim().to_string()),
        Err(e) => Fetched::Unreachable(e.to_string()),
    }
}

fn repositories(a: &Value) -> Result<Vec<String>> {
    let given = strings(&a["repositories"]);
    let list: Vec<String> = if given.is_empty() {
        REPOSITORIES.iter().map(|r| r.to_string()).collect()
    } else {
        given
    };
    for repository in &list {
        if !(repository.starts_with("https://") || repository.starts_with("file:///"))
            || repository.contains(char::is_whitespace)
        {
            return Err(Error::new(
                "invalid_argument",
                format!("A repository must be an https:// or file:/// URL: {repository}"),
            ));
        }
    }
    Ok(list)
}

fn offline(hosts: &[String]) -> Error {
    Error::new(
        "offline",
        format!(
            "The Maven repositories cannot be reached: {} did not resolve",
            hosts.join(", ")
        ),
    )
    .remedy("Connect to the network and run project.dependency_updates again; it reads Maven metadata online")
}

/// Refuses at once when no https repository's host resolves.
fn check_online(repositories: &[String]) -> Result<()> {
    let hosts: Vec<String> = repositories
        .iter()
        .filter_map(|r| r.strip_prefix("https://"))
        .map(|r| r.split(['/', ':']).next().unwrap_or_default().to_string())
        .collect();
    if hosts.is_empty() {
        return Ok(());
    }
    if hosts.iter().any(|h| {
        (h.as_str(), 443)
            .to_socket_addrs()
            .is_ok_and(|mut a| a.next().is_some())
    }) {
        return Ok(());
    }
    Err(offline(&hosts))
}

pub fn updates(a: &Value) -> Result {
    let project = crate::projects::inspect(s(a, "path"))?;
    let root = PathBuf::from(s(&project, "path"));
    let kinds: Vec<&str> = project["kinds"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .collect();
    let repositories = repositories(a)?;
    let mut ecosystems = Vec::new();
    let gradle = project["gradle_directory"].as_str().map(PathBuf::from);
    if let Some(dir) = &gradle {
        let found = declared(dir);
        if !found.is_empty() {
            let declared: Vec<Value> = found
                .iter()
                .map(|d| json!({"kind":d.kind,"name":d.name,"group":d.group,"artifact":d.artifact,"version":d.version,"source":d.source}))
                .collect();
            ecosystems.push(
                json!({"name":"gradle","tool":"curl","gradle_directory":dir,"declared":declared}),
            );
        }
    }
    ecosystems.extend(crate::package_updates::plan(&root, &kinds)?);
    if ecosystems.is_empty() {
        return Err(if gradle.is_some() {
            Error::new(
                "nothing_to_check",
                "No versioned dependency or plugin in the versions catalog or build files",
            )
        } else {
            Error::new(
                "action_unsupported",
                "project.dependency_updates reads Gradle builds, pubspec.yaml, package.json dependencies and Package.resolved; this project has none",
            )
        });
    }
    // A single ecosystem that cannot run is refused at once.
    if let [only] = ecosystems.as_slice() {
        if let Some(error) = only["error"].as_str() {
            return Err(Error::new("tool_missing", error));
        }
    }
    let alone = ecosystems.len() == 1;
    if let Some(gradle) = ecosystems.iter_mut().find(|e| e["name"] == "gradle") {
        match tc::find("curl").and_then(|curl| {
            check_online(&repositories)?;
            Ok(curl)
        }) {
            Ok(curl) => gradle["curl"] = json!(curl),
            Err(error) if alone => return Err(error),
            Err(error) => gradle["error"] = json!(error.message),
        }
    }
    let directory = home()?
        .join("dependencies")
        .join(uuid::Uuid::new_v4().simple().to_string());
    private_dir(&directory)?;
    let report = directory.join("report.json");
    let request = directory.join("request.json");
    atomic_json(
        &request,
        &json!({"project":project["path"],"gradle_directory":gradle,"repositories":repositories,"ecosystems":ecosystems,"report":report}),
    )?;
    crate::jobs::submit(
        &format!("Dependency updates · {}", s(&project, "name")),
        json!({"steps":[{"argv":[own_executable()?,"_dependency_updates",request]}],"cwd":directory,"env":tc::environment()?,"timeout":600,"always_artifacts":["report.json"]}),
    )
}

/// The Gradle entries: fetches each artifact's metadata once, from the first
/// repository that has it.
fn gradle_entries(ecosystem: &Value, repositories: &[String]) -> Result<Vec<Value>> {
    if let Some(error) = ecosystem["error"].as_str() {
        return Err(Error::new("offline", error));
    }
    let curl = s(ecosystem, "curl").to_string();
    let declared = ecosystem["declared"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let mut artifacts: Vec<(String, String)> = declared
        .iter()
        .filter(|d| !s(d, "version").is_empty())
        .map(|d| (s(d, "group").to_string(), s(d, "artifact").to_string()))
        .collect();
    artifacts.sort();
    artifacts.dedup();
    let lookups: Vec<(String, String, Value)> = std::thread::scope(|scope| {
        artifacts
            .chunks(artifacts.len().div_ceil(8).max(1))
            .map(|chunk| {
                let curl = &curl;
                scope.spawn(move || {
                    chunk
                        .iter()
                        .map(|(group, artifact)| {
                            let mut unreachable = Vec::new();
                            for repository in repositories {
                                match fetch(curl, &metadata_url(repository, group, artifact)) {
                                    Fetched::Found(xml) => {
                                        return (group.clone(), artifact.clone(), match metadata_versions(&xml) {
                                            Ok(v) => json!({"repository":repository,"versions":v}),
                                            Err(e) => json!({"repository":repository,"error":e.message}),
                                        });
                                    }
                                    Fetched::Missing => (),
                                    Fetched::Unreachable(e) => unreachable.push(format!("{repository}: {e}")),
                                }
                            }
                            (group.clone(), artifact.clone(), json!({"unreachable":unreachable}))
                        })
                        .collect::<Vec<_>>()
                })
            })
            .collect::<Vec<_>>()
            .into_iter()
            .flat_map(|h| h.join().unwrap_or_default())
            .collect()
    });
    let reached = lookups.iter().any(|(_, _, l)| {
        l.get("repository").is_some() || l["unreachable"].as_array().is_some_and(|u| u.is_empty())
    });
    if !lookups.is_empty() && !reached {
        let mut error = offline(repositories);
        error.message = format!(
            "None of the Maven repositories answered: {}",
            lookups[0].2["unreachable"][0].as_str().unwrap_or("")
        );
        return Err(error);
    }
    let found: BTreeMap<(String, String), Value> =
        lookups.into_iter().map(|(g, a, v)| ((g, a), v)).collect();
    let mut entries = Vec::new();
    for d in &declared {
        let version = s(d, "version");
        let mut entry = json!({"ecosystem":"gradle"});
        for (key, value) in d.as_object().into_iter().flatten() {
            entry[key] = value.clone();
        }
        let lookup = found.get(&(s(d, "group").to_string(), s(d, "artifact").to_string()));
        match lookup {
            _ if version.is_empty() => {
                entry["status"] = json!("managed");
            }
            Some(l) if l.get("versions").is_some() => {
                let versions = strings(&l["versions"]);
                let assessed = assess(version, &versions);
                for key in ["latest_stable", "latest", "status"] {
                    entry[key] = assessed[key].clone();
                }
                entry["repository"] = l["repository"].clone();
            }
            Some(l) if l.get("error").is_some() => {
                entry["status"] = json!("invalid_metadata");
                entry["error"] = l["error"].clone();
            }
            Some(l) if l["unreachable"].as_array().is_some_and(|u| !u.is_empty()) => {
                entry["status"] = json!("unreachable");
                entry["error"] = l["unreachable"].clone();
            }
            _ => {
                entry["status"] = json!("not_found");
            }
        }
        entries.push(entry);
    }
    Ok(entries)
}

/// The `_dependency_updates` worker: checks each ecosystem and writes
/// report.json. An ecosystem that cannot be checked is listed with its
/// error; the job fails only when none could be.
pub fn worker(request: &str) -> Result<()> {
    let spec = read_json(Path::new(request), None)?;
    let repositories = strings(&spec["repositories"]);
    let ecosystems = spec["ecosystems"].as_array().cloned().unwrap_or_default();
    let mut entries = Vec::new();
    let mut checked = Vec::new();
    let mut last_error = None;
    for ecosystem in &ecosystems {
        let name = s(ecosystem, "name");
        eprintln!("Checking {name} dependencies");
        let found = if name == "gradle" {
            gradle_entries(ecosystem, &repositories)
        } else {
            crate::package_updates::run(ecosystem)
        };
        let mut summary = json!({"name":name,"tool":s(ecosystem, "tool")});
        if !s(ecosystem, "source").is_empty() {
            summary["source"] = ecosystem["source"].clone();
        }
        match found {
            Ok(found) => {
                summary["total"] = json!(found.len());
                entries.extend(found);
            }
            Err(error) => {
                eprintln!("{name}: {}", error.message);
                summary["error"] = json!(error.message);
                last_error = Some(error);
            }
        }
        checked.push(summary);
    }
    if let (Some(error), true) = (last_error, checked.iter().all(|c| c.get("error").is_some())) {
        return Err(error);
    }
    let mut summary: BTreeMap<String, u64> = BTreeMap::new();
    for entry in &entries {
        *summary.entry(s(entry, "status").to_string()).or_default() += 1;
    }
    let report = json!({
        "project": spec["project"],
        "gradle_directory": spec["gradle_directory"],
        "repositories": repositories,
        "ecosystems": checked,
        "total": entries.len(),
        "summary": summary,
        "dependencies": entries,
    });
    atomic_json(Path::new(s(&spec, "report")), &report)?;
    println!("{}", serde_json::to_string_pretty(&report)?);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn versions_are_ordered_as_gradle_orders_them() {
        for (a, b) in [
            ("1.10.0", "1.9.9"),
            ("1.0", "1.0-rc1"),
            ("1.0-rc1", "1.0-beta2"),
            ("1.0-beta2", "1.0-alpha9"),
            ("1.0-alpha10", "1.0-alpha9"),
            ("2.2.21", "2.2.21-RC2"),
            ("1.0.1", "1.0"),
            ("2026.09.00", "2026.06.01"),
            ("1.0-alpha", "1.0-dev"),
        ] {
            assert_eq!(compare(a, b), Ordering::Greater, "{a} > {b}");
            assert_eq!(compare(b, a), Ordering::Less, "{b} < {a}");
        }
        assert_eq!(compare("1.0.0", "1.0.0"), Ordering::Equal);
        assert!(stable("1.13.0") && stable("2026.06.01") && stable("33.3.1-android"));
        for v in [
            "1.14.0-alpha03",
            "2.2.21-RC",
            "1.0-beta1",
            "6.0-M1",
            "1.0-SNAPSHOT",
            "2.0.0-dev-123",
        ] {
            assert!(!stable(v), "{v}");
        }
    }

    #[test]
    fn a_dependency_is_assessed_against_the_newest_stable_version() {
        let versions: Vec<String> = ["1.0.0", "1.1.0", "1.2.0-alpha01", "1.1.1"]
            .iter()
            .map(|v| v.to_string())
            .collect();
        let a = assess("1.0.0", &versions);
        assert_eq!(a["status"], "update_available");
        assert_eq!(a["latest_stable"], "1.1.1");
        assert_eq!(a["latest"], "1.2.0-alpha01");
        assert_eq!(assess("1.1.1", &versions)["status"], "up_to_date");
        assert_eq!(
            assess("1.2.0-alpha00", &versions)["status"],
            "prerelease_update_available"
        );
        assert_eq!(assess("1.2.0-alpha01", &versions)["status"], "up_to_date");
        assert_eq!(assess("1.+", &versions)["status"], "dynamic");
        assert_eq!(assess("$kotlin", &versions)["status"], "unresolved");
    }

    #[test]
    fn metadata_lists_its_versions() {
        let xml = "<?xml version=\"1.0\"?><metadata><groupId>g</groupId><artifactId>a</artifactId><versioning><latest>2.0</latest><versions><version>1.0</version><version>2.0</version></versions></versioning></metadata>";
        assert_eq!(metadata_versions(xml).unwrap(), ["1.0", "2.0"]);
        assert!(metadata_versions("<html>").is_err());
        assert_eq!(
            metadata_url("https://repo/m2/", "androidx.core", "core-ktx"),
            "https://repo/m2/androidx/core/core-ktx/maven-metadata.xml"
        );
    }

    #[test]
    fn the_catalog_and_build_files_are_read_with_their_lines() {
        let root = tempfile::tempdir().unwrap();
        let root = root.path();
        let write = |relative: &str, text: &str| {
            let file = root.join(relative);
            fs::create_dir_all(file.parent().unwrap()).unwrap();
            fs::write(file, text).unwrap();
        };
        write("settings.gradle.kts", "include(\":app\")\n");
        write(
            "gradle/libs.versions.toml",
            "[versions]\ncore = \"1.13.0\"\n[libraries]\ncore = { module = \"androidx.core:core-ktx\", version.ref = \"core\" }\nbom-managed = { group = \"androidx.compose.ui\", name = \"ui\" }\n[plugins]\nandroid-application = { id = \"com.android.application\", version = \"8.7.3\" }\n",
        );
        write(
            "build.gradle.kts",
            "plugins {\n    id(\"org.jetbrains.kotlin.android\") version \"2.0.0\" apply false\n}\n",
        );
        write(
            "app/build.gradle",
            "dependencies {\n    // implementation 'com.old:gone:1.0'\n    implementation 'com.squareup.okhttp3:okhttp:4.12.0'\n    implementation \"org.example:lib:$libVersion\"\n    implementation(\"com.google.android.material:material:1.12.0@aar\")\n}\n",
        );
        let found = declared(root);
        let find = |name: &str| found.iter().find(|d| d.name == name).unwrap();
        assert_eq!(find("core").version, "1.13.0");
        assert_eq!(find("core").source, "gradle/libs.versions.toml:4");
        assert_eq!(find("bom-managed").version, "");
        let plugin = find("android-application");
        assert_eq!(
            (plugin.group.as_str(), plugin.artifact.as_str()),
            (
                "com.android.application",
                "com.android.application.gradle.plugin"
            )
        );
        assert_eq!(
            find("org.jetbrains.kotlin.android").source,
            "build.gradle.kts:2"
        );
        assert_eq!(
            find("com.squareup.okhttp3:okhttp").source,
            "app/build.gradle:3"
        );
        assert_eq!(find("org.example:lib").version, "$libVersion");
        assert_eq!(
            find("com.google.android.material:material").version,
            "1.12.0"
        );
        assert!(!found.iter().any(|d| d.group == "com.old"));
    }

    #[test]
    fn repositories_must_be_https_or_local_files() {
        assert_eq!(repositories(&json!({})).unwrap().len(), 3);
        assert!(repositories(&json!({"repositories":["http://insecure/m2"]})).is_err());
        assert!(repositories(&json!({"repositories":["file:///srv/m2"]})).is_ok());
        assert!(check_online(&["file:///srv/m2".to_string()]).is_ok());
        let error = check_online(&["https://nothing.invalid/m2".to_string()]).unwrap_err();
        assert_eq!(error.code, "offline");
    }
}
