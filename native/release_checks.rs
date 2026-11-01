//! release.checklist: what Google Play and a release build need, read from
//! an Android project's build files and manifest without running Gradle.
use crate::{core::*, gradle_model as gm};
use regex::Regex;
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

/// The target API level Google Play requires of new apps and app updates:
/// each year's level, from 31 August, by Unix time.
const PLAY_TARGET_SDK: &[(i64, u64)] = &[(1_756_598_400, 35), (1_788_134_400, 36)];

fn required_target_sdk(at: i64) -> u64 {
    PLAY_TARGET_SDK
        .iter()
        .rev()
        .find(|(from, _)| at >= *from)
        .map(|(_, level)| *level)
        .unwrap_or(34)
}

/// The release build type's settings: (debuggable, minify, shrink,
/// signingConfig) as written, None when not set.
#[derive(Debug, Default, PartialEq)]
struct Release {
    debuggable: Option<String>,
    minify: Option<String>,
    shrink: Option<String>,
    signing: Option<String>,
}

fn release_type(script: &str) -> Release {
    let android = gm::blocks(script, "android")
        .into_iter()
        .max_by_key(|b| b.len())
        .unwrap_or("");
    let Some(body) = gm::block(android, "buildTypes")
        .and_then(|types| {
            gm::children(types)
                .into_iter()
                .find(|(n, _)| n == "release")
        })
        .map(|(_, body)| body)
    else {
        return Release::default();
    };
    let text = gm::flat(body);
    let get = |names: &[&str]| gm::property(&text, names).map(|v| v.trim().to_string());
    Release {
        debuggable: get(&["isDebuggable", "debuggable"]),
        minify: get(&["isMinifyEnabled", "minifyEnabled"]),
        shrink: get(&["isShrinkResources", "shrinkResources"]),
        signing: get(&["signingConfig"]),
    }
}

/// The manifest's application flags: debuggable, usesCleartextTraffic and
/// the network security config resource name.
fn manifest_flags(xml: &str) -> (Option<String>, Option<String>, Option<String>) {
    let Ok(doc) = roxmltree::Document::parse(xml) else {
        return (None, None, None);
    };
    const NS: &str = "http://schemas.android.com/apk/res/android";
    let Some(app) = doc.descendants().find(|n| n.has_tag_name("application")) else {
        return (None, None, None);
    };
    let get = |name: &str| app.attribute((NS, name)).map(String::from);
    (
        get("debuggable"),
        get("usesCleartextTraffic"),
        get("networkSecurityConfig").map(|v| v.trim_start_matches("@xml/").to_string()),
    )
}

/// Where a network security config permits cleartext: "base" for every
/// domain, or the domains of each domain-config that does.
fn cleartext_permitted(xml: &str) -> (bool, Vec<String>) {
    let Ok(doc) = roxmltree::Document::parse(xml) else {
        return (false, Vec::new());
    };
    let permits = |n: roxmltree::Node| n.attribute("cleartextTrafficPermitted") == Some("true");
    let base = doc
        .descendants()
        .any(|n| n.has_tag_name("base-config") && permits(n));
    let domains = doc
        .descendants()
        .filter(|n| n.has_tag_name("domain-config") && permits(*n))
        .flat_map(|n| {
            n.children()
                .filter(|c| c.has_tag_name("domain"))
                .filter_map(|c| c.text().map(|t| t.trim().to_string()))
                .collect::<Vec<_>>()
        })
        .collect();
    (base, domains)
}

struct Checks {
    list: Vec<Value>,
}
impl Checks {
    fn add(&mut self, check: &str, status: &str, severity: &str, message: String, evidence: Value) {
        self.list.push(json!({"check":check,"status":status,"severity":if status=="pass"{"info"}else{severity},"message":message,"evidence":evidence}));
    }
}

/// The Gradle build of a project: the root itself, or Flutter's and React
/// Native's android/ directory.
fn gradle_root(root: &Path) -> Option<PathBuf> {
    [root.to_path_buf(), root.join("android")]
        .into_iter()
        .find(|d| {
            gm::settings_file(d).is_some()
                || d.join("app").join("build.gradle").is_file()
                || d.join("app").join("build.gradle.kts").is_file()
        })
}

pub fn checklist(a: &Value) -> Result {
    let root = path(s(a, "path"), true, true)?;
    let gradle = gradle_root(&root).ok_or_else(|| {
        Error::new(
            "not_android_project",
            "No Android Gradle build in this project",
        )
        .remedy("Pass the project root that holds settings.gradle(.kts), or its android/ directory")
    })?;
    let model = gm::read(&gradle);
    let modules = model["app_modules"].as_array().cloned().unwrap_or_default();
    let wanted = s(a, "module").trim_start_matches(':');
    let module = modules
        .iter()
        .find(|m| wanted.is_empty() || s(m, "module").trim_start_matches(':') == wanted)
        .cloned()
        .ok_or_else(|| {
            Error::new("module_not_found", "No Android application module found").details(
                json!({"modules":modules.iter().map(|m| m["module"].clone()).collect::<Vec<_>>()}),
            )
        })?;
    let directory = PathBuf::from(s(&module, "directory"));
    let script_file = gm::build_file(&directory);
    let script = script_file
        .as_ref()
        .and_then(|f| gm::read_text(f))
        .map(|t| gm::strip_comments(&t))
        .unwrap_or_default();
    let release = release_type(&script);
    let manifest_file = directory.join("src/main/AndroidManifest.xml");
    let manifest = gm::read_text(&manifest_file).unwrap_or_default();
    let (manifest_debuggable, cleartext, security) = manifest_flags(&manifest);
    let artifact = if s(a, "artifact").is_empty() {
        None
    } else {
        Some(crate::android_package::inspect(
            &json!({"path":s(a,"artifact")}),
        )?)
    };
    let allow_cleartext = b(a, "allow_cleartext");
    let required = a["required_target_sdk"]
        .as_u64()
        .unwrap_or_else(|| required_target_sdk(now() as i64));
    let mut checks = Checks { list: Vec::new() };

    let (target, target_from) = match &artifact {
        Some(value) if value["target_sdk"].is_u64() => (value["target_sdk"].as_u64(), "artifact"),
        _ => (module["target_sdk"].as_u64(), "build file"),
    };
    match target {
        Some(level) if level >= required => checks.add("target_sdk", "pass", "error", format!("targetSdk {level} meets Google Play's {required}"), json!({"target_sdk":level,"required":required,"from":target_from})),
        Some(level) => checks.add("target_sdk", "fail", "error", format!("targetSdk {level} is below the {required} Google Play requires of new apps and updates"), json!({"target_sdk":level,"required":required,"from":target_from})),
        None => checks.add("target_sdk", "unknown", "warning", "targetSdk is computed by the build; pass artifact to read it from the built package".into(), json!({"required":required,"unresolved":module["unresolved"]})),
    }

    let (code, code_from) = match &artifact {
        Some(value) if value["version_code"].is_u64() => {
            (value["version_code"].as_u64(), "artifact")
        }
        _ => (
            crate::release_version::current(&root)
                .ok()
                .and_then(|(_, c)| c),
            "version files",
        ),
    };
    let previous = match a["previous_version_code"].as_u64() {
        Some(previous) => Some(previous),
        None if !s(a, "baseline").is_empty() => {
            crate::android_package::inspect(&json!({"path":s(a,"baseline")}))?["version_code"]
                .as_u64()
        }
        None => None,
    };
    match (code, previous) {
        (Some(code), Some(previous)) if code > previous => checks.add(
            "version_code",
            "pass",
            "error",
            format!("versionCode {code} is above the previous {previous}"),
            json!({"version_code":code,"previous":previous,"from":code_from}),
        ),
        (Some(code), Some(previous)) => checks.add(
            "version_code",
            "fail",
            "error",
            format!(
                "versionCode {code} is not above the previous {previous}; Google Play refuses it"
            ),
            json!({"version_code":code,"previous":previous,"from":code_from}),
        ),
        (code, None) => checks.add(
            "version_code",
            "unknown",
            "warning",
            "Pass previous_version_code or baseline to compare with the last release".into(),
            json!({"version_code":code,"from":code_from}),
        ),
        (None, Some(previous)) => checks.add(
            "version_code",
            "unknown",
            "warning",
            "versionCode is computed by the build; pass artifact to read it".into(),
            json!({"previous":previous}),
        ),
    }

    let debuggable_type = release.debuggable.as_deref() == Some("true");
    let debuggable_manifest = manifest_debuggable.as_deref() == Some("true");
    if debuggable_type || debuggable_manifest {
        checks.add("debuggable", "fail", "error", "The release build is debuggable; Google Play refuses debuggable uploads".into(), json!({"build_type":release.debuggable,"manifest":manifest_debuggable,"file":if debuggable_type {script_file.clone()} else {Some(manifest_file.clone())}}));
    } else {
        checks.add(
            "debuggable",
            "pass",
            "error",
            "The release build is not debuggable".into(),
            json!({"build_type":release.debuggable,"manifest":manifest_debuggable}),
        );
    }

    match release.minify.as_deref() {
        Some("true") => checks.add(
            "minify",
            "pass",
            "warning",
            "R8 shrinks and obfuscates the release build".into(),
            json!({"minify":true,"shrink_resources":release.shrink}),
        ),
        Some("false") | None => checks.add(
            "minify",
            "fail",
            "warning",
            "minify is off for release: R8 neither shrinks nor obfuscates the code".into(),
            json!({"minify":release.minify,"file":script_file}),
        ),
        Some(other) => checks.add(
            "minify",
            "unknown",
            "warning",
            format!("minify is computed by the build: {other}"),
            json!({"minify":other}),
        ),
    }

    match release.signing.as_deref() {
        Some(config) if Regex::new(r#"(?:getByName\(\s*["']debug["']\s*\)|\.debug\b|named\(\s*["']debug["']\s*\))"#).unwrap().is_match(config) =>
            checks.add("signing_config", "fail", "error", "The release build is signed with the debug key".into(), json!({"signing_config":config,"file":script_file})),
        Some(config) => checks.add("signing_config", "pass", "warning", "The release build type has a signing config".into(), json!({"signing_config":config})),
        None => checks.add("signing_config", "fail", "warning", "The release build type has no signing config, so Gradle builds it unsigned; sign it with release.sign before uploading".into(), json!({"file":script_file})),
    }

    let mut cleartext_evidence = json!({"uses_cleartext_traffic":cleartext,"network_security_config":security,"allowed":allow_cleartext});
    let mut permitted = cleartext.as_deref() == Some("true");
    let mut domains = Vec::new();
    if let Some(name) = &security {
        let file = directory
            .join("src/main/res/xml")
            .join(format!("{name}.xml"));
        if let Some(xml) = gm::read_text(&file) {
            let (base, listed) = cleartext_permitted(&xml);
            permitted |= base;
            domains = listed;
            cleartext_evidence["file"] = json!(file);
        }
    }
    cleartext_evidence["domains"] = json!(domains);
    if permitted {
        checks.add(
            "cleartext",
            if allow_cleartext { "pass" } else { "fail" },
            "error",
            "The app permits cleartext HTTP to every host".into(),
            cleartext_evidence,
        );
    } else if !domains.is_empty() {
        checks.add(
            "cleartext",
            if allow_cleartext { "pass" } else { "fail" },
            "warning",
            format!("Cleartext HTTP is permitted to {}", domains.join(", ")),
            cleartext_evidence,
        );
    } else {
        checks.add(
            "cleartext",
            "pass",
            "error",
            "No cleartext HTTP is permitted".into(),
            cleartext_evidence,
        );
    }

    let failed: Vec<&Value> = checks
        .list
        .iter()
        .filter(|c| c["status"] == "fail" && c["severity"] == "error")
        .collect();
    let passed = failed.is_empty();
    Ok(json!({
        "passed": passed,
        "module": module["module"],
        "application_id": module["application_id"],
        "required_target_sdk": required,
        "checks": checks.list,
        "read_from": if artifact.is_some() { "build files, manifest and the artifact" } else { "build files and manifest, without running Gradle" },
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plays_target_level_follows_the_date() {
        assert_eq!(required_target_sdk(1_756_598_399), 34);
        assert_eq!(required_target_sdk(1_756_598_400), 35);
        assert_eq!(required_target_sdk(1_788_134_400), 36);
    }

    #[test]
    fn the_release_build_type_is_read_in_both_dialects() {
        let kts = "android {\n buildTypes {\n  getByName(\"release\") {\n   isMinifyEnabled = true\n   isShrinkResources = true\n   signingConfig = signingConfigs.getByName(\"debug\")\n  }\n  debug { isDebuggable = true }\n }\n}\n";
        assert_eq!(
            release_type(kts),
            Release {
                debuggable: None,
                minify: Some("true".into()),
                shrink: Some("true".into()),
                signing: Some("signingConfigs.getByName(\"debug\")".into())
            }
        );
        let groovy = "android {\n buildTypes {\n  release {\n   minifyEnabled false\n   debuggable true\n   signingConfig signingConfigs.upload\n  }\n }\n}\n";
        let r = release_type(groovy);
        assert_eq!(
            (
                r.minify.as_deref(),
                r.debuggable.as_deref(),
                r.signing.as_deref()
            ),
            (Some("false"), Some("true"), Some("signingConfigs.upload"))
        );
        assert_eq!(release_type("android { }"), Release::default());
    }

    #[test]
    fn manifest_and_network_security_config_are_read() {
        let manifest = r#"<manifest xmlns:android="http://schemas.android.com/apk/res/android"><application android:usesCleartextTraffic="true" android:networkSecurityConfig="@xml/network"/></manifest>"#;
        assert_eq!(
            manifest_flags(manifest),
            (None, Some("true".into()), Some("network".into()))
        );
        let config = "<network-security-config><base-config cleartextTrafficPermitted=\"false\"/><domain-config cleartextTrafficPermitted=\"true\"><domain includeSubdomains=\"true\">10.0.2.2</domain></domain-config></network-security-config>";
        assert_eq!(
            cleartext_permitted(config),
            (false, vec!["10.0.2.2".to_string()])
        );
    }
}
