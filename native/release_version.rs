//! release.version: one version name and build number across the version
//! files of a project: pubspec.yaml, Expo's app.json, package.json, Gradle
//! build files with their versions catalog and gradle.properties, and the
//! Info.plist of an xtool project.
use crate::{core::*, gradle_model::accessor};
use regex::Regex;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};

const AMBIGUOUS: &str = "Version is computed by the build; update its source property instead";

/// The files to change, each with its original and its edited text, and
/// what changed in them.
#[derive(Default)]
struct Plan {
    texts: BTreeMap<PathBuf, (String, String)>,
    changes: Vec<Value>,
    skipped: Vec<Value>,
    versions: Vec<String>,
    builds: Vec<u64>,
}

/// The new version name and build number; None reads only.
type Target<'a> = Option<(&'a str, u64)>;

impl Plan {
    fn text(&mut self, file: &Path) -> Result<String> {
        if let Some((_, current)) = self.texts.get(file) {
            return Ok(current.clone());
        }
        let text = fs::read_to_string(file)?;
        self.texts
            .insert(file.to_path_buf(), (text.clone(), text.clone()));
        Ok(text)
    }
    fn set(&mut self, file: &Path, text: String) {
        if let Some(entry) = self.texts.get_mut(file) {
            entry.1 = text;
        }
    }
    fn found(&mut self, file: &Path, field: &str, from: &str, to: Option<String>, code: bool) {
        if !code {
            if !from.is_empty() {
                self.versions.push(from.to_string());
            }
        } else if let Ok(code) = from.trim().parse::<u64>() {
            self.builds.push(code);
        }
        if let Some(to) = to {
            self.changes
                .push(json!({"file":file,"field":field,"from":from,"to":to}));
        }
    }
}

/// Replaces the one match of `re` in `text`; capture 2 is the value, 1 and 3
/// what stays around it.
fn replace_one(
    text: &str,
    re: &Regex,
    value: Option<&str>,
    what: &str,
) -> Result<(String, String)> {
    let count = re.find_iter(text).count();
    if count != 1 {
        return Err(Error::new(
            "version_ambiguous",
            format!("Expected exactly one {what}, found {count}"),
        ));
    }
    let captures = re.captures(text).unwrap();
    let current = captures[2].to_string();
    let Some(value) = value else {
        return Ok((current, text.to_string()));
    };
    let edited = re
        .replace(text, |c: &regex::Captures| {
            format!("{}{value}{}", &c[1], &c[3])
        })
        .into_owned();
    Ok((current, edited))
}

fn pubspec(plan: &mut Plan, file: &Path, target: Target) -> Result<()> {
    let text = plan.text(file)?;
    let re = Regex::new(r"(?m)^(version:[ \t]*)([^\r\n#]*?)([ \t]*(?:#[^\r\n]*)?\r?)$").unwrap();
    let value = target.map(|(name, code)| format!("{name}+{code}"));
    let (current, edited) = replace_one(&text, &re, value.as_deref(), "pubspec version")
        .map_err(|_| Error::new("version_ambiguous", "Expected exactly one pubspec version"))?;
    let (name, code) = current.split_once('+').unwrap_or((&current, ""));
    plan.found(
        file,
        "version",
        name,
        target.map(|(n, _)| n.to_string()),
        false,
    );
    plan.found(
        file,
        "build_number",
        code,
        target.map(|(_, c)| c.to_string()),
        true,
    );
    plan.set(file, edited);
    Ok(())
}

fn write_json(value: &Value, original: &str) -> Result<String> {
    let mut text = serde_json::to_string_pretty(value)?;
    if original.ends_with('\n') {
        text.push('\n');
    }
    Ok(text)
}

fn number_text(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

/// Expo's app.json: expo.version, expo.android.versionCode and
/// expo.ios.buildNumber, the last two when their platform is configured.
fn expo(plan: &mut Plan, file: &Path, target: Target) -> Result<bool> {
    let text = plan.text(file)?;
    let mut value: Value = serde_json::from_str(&text)
        .map_err(|e| Error::new("invalid_json", format!("{}: {e}", file.display())))?;
    let Some(expo) = value.get_mut("expo").filter(|e| e.is_object()) else {
        return Ok(false);
    };
    let version = number_text(&expo["version"]);
    plan.found(
        file,
        "expo.version",
        &version,
        target.map(|(n, _)| n.to_string()),
        false,
    );
    if let Some((name, _)) = target {
        expo["version"] = json!(name);
    }
    if expo["android"].is_object() {
        let code = number_text(&expo["android"]["versionCode"]);
        plan.found(
            file,
            "expo.android.versionCode",
            &code,
            target.map(|(_, c)| c.to_string()),
            true,
        );
        if let Some((_, code)) = target {
            expo["android"]["versionCode"] = json!(code);
        }
    }
    if expo["ios"].is_object() {
        let build = number_text(&expo["ios"]["buildNumber"]);
        plan.found(
            file,
            "expo.ios.buildNumber",
            &build,
            target.map(|(_, c)| c.to_string()),
            true,
        );
        if let Some((_, code)) = target {
            expo["ios"]["buildNumber"] = json!(code.to_string());
        }
    }
    if target.is_some() {
        let edited = write_json(&value, &text)?;
        plan.set(file, edited);
    }
    Ok(true)
}

/// package.json of a React Native or Expo app.
fn package_json(plan: &mut Plan, file: &Path, target: Target) -> Result<bool> {
    let text = plan.text(file)?;
    let mut value: Value = serde_json::from_str(&text)
        .map_err(|e| Error::new("invalid_json", format!("{}: {e}", file.display())))?;
    let mobile = ["dependencies", "devDependencies"]
        .iter()
        .any(|k| value[k].get("react-native").is_some() || value[k].get("expo").is_some());
    if !mobile {
        plan.texts.remove(file);
        return Ok(false);
    }
    let version = number_text(&value["version"]);
    plan.found(
        file,
        "version",
        &version,
        target.map(|(n, _)| n.to_string()),
        false,
    );
    if let Some((name, _)) = target {
        value["version"] = json!(name);
        let edited = write_json(&value, &text)?;
        plan.set(file, edited);
    }
    Ok(true)
}

/// Where a Gradle versionName or versionCode expression gets its value.
#[derive(Debug, PartialEq)]
enum Source {
    Literal,
    Flutter,
    Catalog(String),
    Property(String),
}

fn source(expression: &str) -> Option<Source> {
    let e = expression.trim();
    let e = e
        .strip_suffix(".toInteger()")
        .or_else(|| e.strip_suffix(".toInt()"))
        .unwrap_or(e)
        .trim();
    if Regex::new(r#"^(?:"[^"$]*"|'[^']*'|\d+)$"#)
        .unwrap()
        .is_match(e)
    {
        return Some(Source::Literal);
    }
    if Regex::new(
        r"^(?:flutter\.version(?:Name|Code)|flutterVersion(?:Name|Code)(?:\.toInteger\(\))?)$",
    )
    .unwrap()
    .is_match(e)
    {
        return Some(Source::Flutter);
    }
    if let Some(c) = Regex::new(r"^libs\.versions\.([\w.]+?)\.get\(\)$")
        .unwrap()
        .captures(e)
    {
        return Some(Source::Catalog(c[1].to_string()));
    }
    let property = Regex::new(
        r#"^(?:providers\.gradleProperty|(?:project\.)?(?:property|findProperty)|project\.properties\.get)\(\s*["']([\w.-]+)["']\s*\)(?:\.get\(\))?(?:\s+as\s+String)?!?!?$"#,
    )
    .unwrap();
    if let Some(c) = property.captures(e) {
        return Some(Source::Property(c[1].to_string()));
    }
    None
}

fn gradle(plan: &mut Plan, file: &Path, root: &Path, flutter: bool, target: Target) -> Result<()> {
    let text = plan.text(file)?;
    let mut edited = text.clone();
    for (field, is_code) in [("versionName", false), ("versionCode", true)] {
        let re = Regex::new(&format!(
            r"(?m)^([ \t]*{field}(?:[ \t]*=[ \t]*|[ \t]+))([^\r\n]*?)([ \t]*(?://[^\r\n]*)?\r?)$"
        ))
        .unwrap();
        let count = re.find_iter(&edited).count();
        if count != 1 {
            return Err(Error::new("version_ambiguous", AMBIGUOUS)
                .details(json!({"file":file,"field":field,"occurrences":count})));
        }
        let expression = re.captures(&edited).unwrap()[2].to_string();
        let value = target.map(|(name, code)| {
            if is_code {
                code.to_string()
            } else {
                name.to_string()
            }
        });
        match source(&expression) {
            Some(Source::Literal) => {
                let current = expression.trim_matches(['"', '\'']).to_string();
                plan.found(file, field, &current, value.clone(), is_code);
                if let Some(value) = value {
                    let quoted = if is_code {
                        value
                    } else {
                        format!("\"{value}\"")
                    };
                    edited = re
                        .replace(&edited, |c: &regex::Captures| {
                            format!("{}{quoted}{}", &c[1], &c[3])
                        })
                        .into_owned();
                }
            }
            Some(Source::Flutter) if flutter => {
                plan.skipped
                    .push(json!({"file":file,"field":field,"reason":"follows pubspec.yaml"}));
            }
            Some(Source::Catalog(key)) => {
                let catalog = root.join("gradle/libs.versions.toml");
                let toml = plan.text(&catalog).map_err(|_| {
                    Error::new("version_ambiguous", AMBIGUOUS)
                        .details(json!({"file":file,"field":field,"expression":expression}))
                })?;
                let names = crate::gradle_model::parse_catalog(&toml);
                let Some(entry) = names.versions.keys().find(|k| accessor(k) == key) else {
                    return Err(Error::new(
                        "version_ambiguous",
                        format!("The versions catalog has no version {key}"),
                    ));
                };
                let re = Regex::new(&format!(
                    r#"(?m)^([ \t]*"?{}"?[ \t]*=[ \t]*["'])([^"'\r\n]*)(["'])"#,
                    regex::escape(entry)
                ))
                .unwrap();
                let (current, next) = replace_one(
                    &toml,
                    &re,
                    value.as_deref(),
                    &format!("catalog version {entry}"),
                )?;
                plan.found(
                    &catalog,
                    &format!("versions.{entry}"),
                    &current,
                    value,
                    is_code,
                );
                plan.set(&catalog, next);
            }
            Some(Source::Property(key)) => {
                let properties = root.join("gradle.properties");
                let text = plan.text(&properties).map_err(|_| {
                    Error::new("version_ambiguous", AMBIGUOUS)
                        .details(json!({"file":file,"field":field,"expression":expression}))
                })?;
                let re = Regex::new(&format!(
                    r"(?m)^([ \t]*{}[ \t]*[=:][ \t]*)([^\r\n]*?)([ \t]*\r?)$",
                    regex::escape(&key)
                ))
                .unwrap();
                let (current, next) =
                    replace_one(&text, &re, value.as_deref(), &format!("property {key}")).map_err(
                        |e| e.details(json!({"file":file,"field":field,"expression":expression})),
                    )?;
                plan.found(&properties, &key, &current, value, is_code);
                plan.set(&properties, next);
            }
            _ => {
                return Err(Error::new("version_ambiguous", AMBIGUOUS)
                    .details(json!({"file":file,"field":field,"expression":expression})));
            }
        }
    }
    plan.set(file, edited);
    Ok(())
}

/// The Info.plist an xtool.yml names with infoPath, inside the project.
fn xtool_plist(root: &Path) -> Option<PathBuf> {
    let config = ["xtool.yml", "xtool.yaml"]
        .iter()
        .map(|n| root.join(n))
        .find(|p| p.is_file())?;
    let text = fs::read_to_string(config).ok()?;
    let c = Regex::new(r#"(?m)^infoPath:[ \t]*["']?([^"'\r\n#]+?)["']?[ \t]*(?:#.*)?$"#)
        .unwrap()
        .captures(&text)?;
    let file = root.join(c[1].trim());
    let real = file.canonicalize().ok()?;
    (real.starts_with(root.canonicalize().ok()?) && real.is_file()).then_some(file)
}

fn plist(plan: &mut Plan, file: &Path, target: Target) -> Result<()> {
    let mut text = plan.text(file)?;
    for (key, is_code) in [
        ("CFBundleShortVersionString", false),
        ("CFBundleVersion", true),
    ] {
        let value = target.map(|(name, code)| {
            if is_code {
                code.to_string()
            } else {
                name.to_string()
            }
        });
        let re = Regex::new(&format!(r"(<key>{key}</key>\s*<string>)([^<]*)(</string>)")).unwrap();
        match re.find_iter(&text).count() {
            1 => {
                let (current, next) = replace_one(&text, &re, value.as_deref(), key)?;
                plan.found(file, key, &current, value, is_code);
                text = next;
            }
            0 => {
                plan.found(file, key, "", value.clone(), is_code);
                if let Some(value) = value {
                    let at = text.rfind("</dict>").ok_or_else(|| {
                        Error::new(
                            "version_unsupported",
                            "Info.plist is not an XML property list",
                        )
                    })?;
                    text.insert_str(
                        at,
                        &format!("\t<key>{key}</key>\n\t<string>{value}</string>\n"),
                    );
                }
            }
            _ => {
                return Err(Error::new(
                    "version_ambiguous",
                    format!("Expected one {key} in Info.plist"),
                ));
            }
        }
    }
    plan.set(file, text);
    Ok(())
}

/// Reads, or with a target edits, every version file of the project.
fn plan(root: &Path, target: Target) -> Result<Plan> {
    let mut plan = Plan::default();
    let flutter = root.join("pubspec.yaml").is_file();
    if flutter {
        pubspec(&mut plan, &root.join("pubspec.yaml"), target)?;
    }
    let mut expo_app = false;
    if root.join("app.json").is_file() {
        expo_app = expo(&mut plan, &root.join("app.json"), target)?;
        if !expo_app {
            plan.texts.remove(&root.join("app.json"));
        }
    }
    if root.join("package.json").is_file() {
        package_json(&mut plan, &root.join("package.json"), target)?;
    }
    for (directory, gradle_root) in [
        ("app", root.to_path_buf()),
        ("android/app", root.join("android")),
    ] {
        let Some(file) = ["build.gradle.kts", "build.gradle"]
            .iter()
            .map(|n| root.join(directory).join(n))
            .find(|p| p.is_file())
        else {
            continue;
        };
        // Expo's prebuilt android/ project takes its version from app.json at
        // the next prebuild; a literal there is still changed.
        let result = gradle(&mut plan, &file, &gradle_root, flutter, target);
        if let Err(error) = result {
            if expo_app && error.code == "version_ambiguous" {
                plan.texts.remove(&file);
                plan.skipped.push(json!({"file":file,"reason":"computed by the build; Expo sets it from app.json"}));
                continue;
            }
            return Err(error);
        }
    }
    if let Some(file) = xtool_plist(root) {
        plist(&mut plan, &file, target)?;
    } else if root.join("xtool.yml").is_file() || root.join("xtool.yaml").is_file() {
        plan.skipped.push(json!({"file":root.join("xtool.yml"),"reason":"names no infoPath; xtool writes its own Info.plist"}));
    }
    if plan.texts.is_empty() {
        return Err(Error::new(
            "version_unsupported",
            "No Flutter, Expo, React Native, Android or xtool version file found",
        )
        .details(json!({"skipped":plan.skipped})));
    }
    Ok(plan)
}

fn bumped(current: &str, part: &str) -> Result<String> {
    let c = Regex::new(r"^(\d+)\.(\d+)\.(\d+)")
        .unwrap()
        .captures(current.trim())
        .ok_or_else(|| {
            Error::new(
                "version_unsupported",
                format!("The current version {current:?} is not major.minor.patch; pass version"),
            )
        })?;
    let [major, minor, patch]: [u64; 3] = [1, 2, 3].map(|i| c[i].parse().unwrap_or(0));
    Ok(match part {
        "major" => format!("{}.0.0", major + 1),
        "minor" => format!("{major}.{}.0", minor + 1),
        "patch" => format!("{major}.{minor}.{}", patch + 1),
        _ => format!("{major}.{minor}.{patch}"),
    })
}

/// The version name and highest build number the project's version files
/// hold now, or the error that stops reading them.
pub fn current(root: &Path) -> Result<(Option<String>, Option<u64>)> {
    let plan = plan(root, None)?;
    Ok((
        plan.versions.first().cloned(),
        plan.builds.iter().max().copied(),
    ))
}

pub fn version(a: &Value) -> Result {
    let root = path(s(a, "path"), true, true)?;
    let bump = s(a, "bump");
    let given = s(a, "version");
    if given.is_empty() == bump.is_empty() {
        return Err(Error::new(
            "invalid_argument",
            "Pass either version or bump",
        ));
    }
    let lock = Lock::open(&home()?.join("locks").join(format!(
        "files-{}.lock",
        digest(root.to_string_lossy().as_bytes())
    )))?;
    lock.acquire(false)?;
    let current = plan(&root, None)?;
    let name = if given.is_empty() {
        let base = current
            .versions
            .first()
            .cloned()
            .unwrap_or_else(|| "0.0.0".into());
        bumped(&base, bump)?
    } else {
        given.to_string()
    };
    identifier(
        &name,
        "version",
        Some(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?"),
    )?;
    let code = match a.get("build_number").and_then(Value::as_u64) {
        Some(code) => code,
        None if !given.is_empty() => {
            return Err(Error::new(
                "invalid_argument",
                "Pass build_number with version",
            ));
        }
        None => current.builds.iter().max().copied().unwrap_or(0) + 1,
    };
    if !(1..=2_100_000_000).contains(&code) {
        return Err(Error::new(
            "invalid_argument",
            "build_number is 1 to 2100000000",
        ));
    }
    let plan = plan(&root, Some((&name, code)))?;
    if name.contains('-')
        && plan
            .changes
            .iter()
            .any(|c| s(c, "field") == "CFBundleShortVersionString")
    {
        return Err(Error::new(
            "invalid_argument",
            "An Info.plist version is numbers only, such as 1.2.3",
        ));
    }
    let dry_run = b(a, "dry_run");
    let mut files = Vec::new();
    let edited: Vec<_> = plan.texts.iter().filter(|(_, (o, t))| o != t).collect();
    if !dry_run {
        let batch = uuid::Uuid::new_v4().simple().to_string();
        let mut backups = Vec::new();
        for (index, (_, (original, _))) in edited.iter().enumerate() {
            let backup = home()?
                .join("version-backups")
                .join(format!("{batch}-{index}.txt"));
            atomic_write(&backup, original.as_bytes())?;
            backups.push(backup);
        }
        for ((file, (_, text)), backup) in edited.iter().zip(backups) {
            let mode = fs::metadata(file)?.permissions();
            atomic_write(file, text.as_bytes())?;
            fs::set_permissions(file, mode)?;
            files.push(json!({"file":file,"backup":backup,"sha256":digest(text.as_bytes())}));
        }
    } else {
        for (file, (_, text)) in &edited {
            files.push(json!({"file":file,"sha256":digest(text.as_bytes())}));
        }
    }
    let mut result = json!({"version":name,"build_number":code,"dry_run":dry_run,"previous":{"version":current.versions.first(),"build_number":current.builds.iter().max()},"changes":plan.changes,"files":files,"skipped":plan.skipped});
    if let Some(first) = files.first() {
        result["file"] = first["file"].clone();
        if !dry_run {
            result["backup"] = first["backup"].clone();
        }
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn project(files: &[(&str, &str)]) -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        for (name, text) in files {
            let file = dir.path().join(name);
            fs::create_dir_all(file.parent().unwrap()).unwrap();
            fs::write(file, text).unwrap();
        }
        dir
    }
    fn texts(plan: &Plan) -> BTreeMap<String, String> {
        plan.texts
            .iter()
            .map(|(f, (_, t))| {
                (
                    f.file_name().unwrap().to_string_lossy().into_owned(),
                    t.clone(),
                )
            })
            .collect()
    }

    #[test]
    fn gradle_sources_are_classified() {
        assert_eq!(source("\"1.2.3\""), Some(Source::Literal));
        assert_eq!(source("12"), Some(Source::Literal));
        assert_eq!(source("flutter.versionCode"), Some(Source::Flutter));
        assert_eq!(
            source("flutterVersionCode.toInteger()"),
            Some(Source::Flutter)
        );
        assert_eq!(
            source("libs.versions.app.versionCode.get().toInt()"),
            Some(Source::Catalog("app.versionCode".into()))
        );
        assert_eq!(
            source("providers.gradleProperty(\"appVersion\").get()"),
            Some(Source::Property("appVersion".into()))
        );
        assert_eq!(
            source("project.property(\"VERSION_CODE\").toInteger()"),
            Some(Source::Property("VERSION_CODE".into()))
        );
        assert_eq!(source("\"${major}.${minor}\""), None);
        assert_eq!(source("computeCode()"), None);
    }

    #[test]
    fn a_versions_catalog_and_gradle_properties_are_followed() {
        let dir = project(&[
            (
                "app/build.gradle.kts",
                "android {\n  defaultConfig {\n    versionCode = libs.versions.app.versionCode.get().toInt()\n    versionName = providers.gradleProperty(\"appVersion\").get()\n  }\n}\n",
            ),
            (
                "gradle/libs.versions.toml",
                "[versions]\napp-versionCode = \"41\" # bump on release\nkotlin = \"2.2.0\"\n",
            ),
            (
                "gradle.properties",
                "org.gradle.jvmargs=-Xmx2g\nappVersion=1.4.0\n",
            ),
        ]);
        let read = plan(dir.path(), None).unwrap();
        assert_eq!(
            (read.versions.clone(), read.builds.clone()),
            (vec!["1.4.0".to_string()], vec![41])
        );
        let edit = plan(dir.path(), Some(("1.5.0", 42))).unwrap();
        let t = texts(&edit);
        assert_eq!(
            t["libs.versions.toml"],
            "[versions]\napp-versionCode = \"42\" # bump on release\nkotlin = \"2.2.0\"\n"
        );
        assert_eq!(
            t["gradle.properties"],
            "org.gradle.jvmargs=-Xmx2g\nappVersion=1.5.0\n"
        );
        assert!(t["build.gradle.kts"].contains("libs.versions.app.versionCode"));
        assert_eq!(edit.changes.len(), 2);
    }

    #[test]
    fn expo_package_json_and_prebuilt_android_change_together() {
        let dir = project(&[
            (
                "app.json",
                "{\n  \"expo\": {\n    \"name\": \"x\",\n    \"version\": \"1.0.0\",\n    \"android\": {\"versionCode\": 3},\n    \"ios\": {\"buildNumber\": \"3\"}\n  }\n}\n",
            ),
            (
                "package.json",
                "{\"name\":\"x\",\"version\":\"1.0.0\",\"dependencies\":{\"expo\":\"~54.0.0\"}}",
            ),
            (
                "android/app/build.gradle",
                "android {\n    defaultConfig {\n        versionCode 3\n        versionName \"1.0.0\"\n    }\n}\n",
            ),
        ]);
        let edit = plan(dir.path(), Some(("1.1.0", 4))).unwrap();
        let t = texts(&edit);
        let app: Value = serde_json::from_str(&t["app.json"]).unwrap();
        assert_eq!(
            (
                app["expo"]["version"].as_str(),
                app["expo"]["android"]["versionCode"].as_u64(),
                app["expo"]["ios"]["buildNumber"].as_str()
            ),
            (Some("1.1.0"), Some(4), Some("4"))
        );
        assert!(t["app.json"].ends_with("}\n"));
        assert!(t["package.json"].contains("\"version\": \"1.1.0\""));
        assert!(
            t["build.gradle"].contains("versionCode 4\n")
                && t["build.gradle"].contains("versionName \"1.1.0\"\n")
        );
        // A web package.json is not an app version file.
        let web = project(&[(
            "package.json",
            "{\"version\":\"1.0.0\",\"dependencies\":{\"react\":\"19\"}}",
        )]);
        assert_eq!(
            plan(web.path(), None).err().unwrap().code,
            "version_unsupported"
        );
    }

    #[test]
    fn flutter_android_follows_pubspec_and_an_xtool_plist_is_edited() {
        let dir = project(&[
            ("pubspec.yaml", "name: x\nversion: 1.0.0+7\n"),
            (
                "android/app/build.gradle.kts",
                "android { defaultConfig {\n versionCode = flutter.versionCode\n versionName = flutter.versionName\n} }\n",
            ),
        ]);
        let edit = plan(dir.path(), Some(("2.0.0", 8))).unwrap();
        assert_eq!(texts(&edit)["pubspec.yaml"], "name: x\nversion: 2.0.0+8\n");
        assert_eq!(edit.skipped.len(), 2);
        let ios = project(&[
            (
                "xtool.yml",
                "version: 1\nbundleID: dev.x\ninfoPath: Info.plist\n",
            ),
            (
                "Info.plist",
                "<plist><dict>\n\t<key>CFBundleShortVersionString</key>\n\t<string>1.0</string>\n</dict></plist>\n",
            ),
        ]);
        let edit = plan(ios.path(), Some(("1.1.0", 5))).unwrap();
        let plist = &texts(&edit)["Info.plist"];
        assert!(plist.contains("<string>1.1.0</string>"));
        assert!(plist.contains("<key>CFBundleVersion</key>\n\t<string>5</string>\n</dict>"));
        assert_eq!(edit.versions, ["1.0"]);
    }

    #[test]
    fn bumps_and_computed_versions() {
        assert_eq!(bumped("1.2.3", "patch").unwrap(), "1.2.4");
        assert_eq!(bumped("1.2.3-rc.1", "minor").unwrap(), "1.3.0");
        assert_eq!(bumped("1.2.3", "major").unwrap(), "2.0.0");
        assert_eq!(bumped("1.2.3", "build").unwrap(), "1.2.3");
        assert!(bumped("1.2", "patch").is_err());
        let dir = project(&[(
            "app/build.gradle",
            "versionCode computeCode()\nversionName \"1.0\"\n",
        )]);
        assert_eq!(
            plan(dir.path(), None).err().unwrap().code,
            "version_ambiguous"
        );
    }
}
