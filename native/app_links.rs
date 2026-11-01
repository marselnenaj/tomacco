//! Android App Links: the verification state of an app's web domains, a new
//! verification, which app a link opens, and states set by hand for tests.
use crate::{core::*, devices, toolchain as tc};
use serde_json::{Map, Value, json};
use std::{thread::sleep, time::Duration};

fn shell(device: &str, args: &[&str]) -> Result<String> {
    let result = tc::run(&devices::shell(device, &argv(args))?, 30, None, true)?;
    Ok(s(&result, "stdout").replace('\r', ""))
}

fn bundle(id: &str) -> Result<&str> {
    identifier(
        id,
        "application ID",
        Some(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+"),
    )
}

/// `pm get-app-links --user cur PACKAGE`: the state of every domain the app
/// asks to verify, and the user's choices for its links.
pub fn parse_status(output: &str) -> Value {
    let mut domains = Map::new();
    let mut enabled = Vec::new();
    let mut disabled = Vec::new();
    let mut allowed = Value::Null;
    let mut id = Value::Null;
    let mut section = "";
    for line in output.lines() {
        let text = line.trim();
        if text.is_empty() {
            continue;
        }
        if let Some(value) = text.strip_prefix("ID:") {
            id = json!(value.trim());
        } else if text == "Domain verification state:" {
            section = "domains";
        } else if text.starts_with("User ") && text.ends_with(':') {
            section = "user";
        } else if let Some(value) = text.strip_prefix("Verification link handling allowed:") {
            allowed = json!(value.trim() == "true");
        } else if text == "Enabled:" {
            section = "enabled";
        } else if text == "Disabled:" {
            section = "disabled";
        } else if text == "Selection state:" || text.starts_with("Signatures:") {
        } else if section == "domains" {
            if let Some((domain, state)) = text.split_once(": ") {
                domains.insert(domain.to_string(), json!(state.trim()));
            }
        } else if section == "enabled" && !text.contains(':') {
            enabled.push(text.to_string());
        } else if section == "disabled" && !text.contains(':') {
            disabled.push(text.to_string());
        }
    }
    let verified: Vec<_> = domains
        .iter()
        .filter(|(_, state)| {
            matches!(
                state.as_str(),
                Some(
                    "verified"
                        | "approved"
                        | "migrated"
                        | "restored"
                        | "system_configured"
                        | "pre_verified"
                )
            )
        })
        .map(|(domain, _)| domain.clone())
        .collect();
    let unverified: Vec<_> = domains
        .keys()
        .filter(|d| !verified.contains(d))
        .cloned()
        .collect();
    json!({"id":id,"domains":domains,"verified":verified,"unverified":unverified,
        "link_handling_allowed":allowed,"user_enabled":enabled,"user_disabled":disabled})
}

/// `cmd package resolve-activity --brief`: the component on the last line,
/// or none.
fn resolved(output: &str) -> Option<String> {
    let last = output.lines().map(str::trim).rfind(|l| !l.is_empty())?;
    (last.contains('/') && !last.contains(' ')).then(|| last.to_string())
}

/// `cmd package query-activities --brief`: every component listed.
fn candidates(output: &str) -> Vec<String> {
    output
        .lines()
        .map(str::trim)
        .filter(|l| l.contains('/') && !l.contains(' ') && !l.contains('='))
        .map(String::from)
        .collect()
}

const CHOOSER: &str = "android/com.android.internal.app.ResolverActivity";

fn status(device: &str, id: &str) -> Result {
    // `pm path` exits 1 for a package that is not installed.
    let installed = tc::run(
        &devices::shell(device, &argv(&["pm", "path", id]))?,
        30,
        None,
        false,
    )?;
    let installed = s(&installed, "stdout");
    if !installed.contains("package:") {
        return Err(Error::new(
            "app_not_installed",
            format!("{id} is not installed on {device}"),
        ));
    }
    let output = shell(device, &["pm", "get-app-links", "--user", "cur", id])?;
    Ok(parse_status(&output))
}

pub fn app_links(a: &Value) -> Result {
    let device = identifier(s(a, "device"), "device ID", None)?;
    let action = s(a, "action");
    let id = s(a, "bundle_id");
    if action != "resolve" && id.is_empty() {
        return Err(Error::new(
            "invalid_argument",
            format!("{action} needs bundle_id"),
        ));
    }
    if !id.is_empty() {
        bundle(id)?;
    }
    match action {
        "resolve" => {
            let url = s(a, "url");
            if !regex::Regex::new(r"^[A-Za-z][A-Za-z0-9+.-]*://[^\s]+$")
                .unwrap()
                .is_match(url)
            {
                return Err(Error::new(
                    "invalid_url",
                    "Use a complete link with a scheme, such as https://example.com/item/1",
                ));
            }
            let intent = [
                "-a",
                "android.intent.action.VIEW",
                "-c",
                "android.intent.category.BROWSABLE",
                "-d",
                url,
            ];
            // MATCH_DEFAULT_ONLY, as when a link is opened: without it the
            // chooser answers even for a verified domain.
            let mut command = vec![
                "cmd",
                "package",
                "resolve-activity",
                "--brief",
                "--query-flags",
                "0x10000",
            ];
            command.extend(intent);
            let handler = resolved(&shell(device, &command)?);
            command[2] = "query-activities";
            let all = candidates(&shell(device, &command)?);
            let chooser = handler.as_deref() == Some(CHOOSER);
            let package = handler
                .as_deref()
                .filter(|_| !chooser)
                .and_then(|h| h.split('/').next())
                .map(String::from);
            let mut result = json!({"device":device,"action":action,"url":url,
                "handler":if chooser {Value::Null} else {json!(handler)},"package":package,
                "chooser":chooser,"candidates":all});
            if !id.is_empty() {
                result["opens_in_app"] = json!(package.as_deref() == Some(id));
            }
            Ok(result)
        }
        "verify" => {
            status(device, id)?;
            shell(device, &["pm", "verify-app-links", "--re-verify", id])?;
            let seconds = n(a, "seconds");
            let started = std::time::Instant::now();
            let mut state = status(device, id)?;
            // The verifier fetches assetlinks.json from each domain; a
            // domain stays `none` until its answer is recorded.
            while started.elapsed().as_secs() < seconds
                && state["domains"]
                    .as_object()
                    .is_some_and(|d| d.values().any(|v| v == "none"))
            {
                sleep(Duration::from_secs(1));
                state = status(device, id)?;
            }
            state["device"] = json!(device);
            state["bundle_id"] = json!(id);
            state["action"] = json!(action);
            state["waited_seconds"] = json!(started.elapsed().as_secs());
            Ok(state)
        }
        "set" => {
            status(device, id)?;
            let mut domains = strings(&a["domains"]);
            for domain in &domains {
                identifier(domain, "domain", Some(r"[A-Za-z0-9*.-]+"))?;
            }
            if domains.is_empty() {
                domains.push("all".into());
            }
            let state = match s(a, "state") {
                "denied" => "STATE_DENIED",
                "none" => "STATE_NO_RESPONSE",
                _ => "STATE_APPROVED",
            };
            let mut command = vec!["pm", "set-app-links", "--package", id, state];
            command.extend(domains.iter().map(String::as_str));
            shell(device, &command)?;
            let mut state = status(device, id)?;
            state["device"] = json!(device);
            state["bundle_id"] = json!(id);
            state["action"] = json!(action);
            Ok(state)
        }
        _ => {
            let mut state = status(device, id)?;
            state["device"] = json!(device);
            state["bundle_id"] = json!(id);
            state["action"] = json!(action);
            Ok(state)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const YOUTUBE: &str = "  com.google.android.youtube:\n    ID: 3041a01b-8aea\n    Signatures: [7F:D2]\n    Domain verification state:\n      youtu.be: system_configured\n      example.com: none\n      www.example.com: 1024\n    User 0:\n      Verification link handling allowed: true\n      Selection state:\n        Enabled:\n          example.com\n        Disabled:\n          youtu.be\n";

    #[test]
    fn verification_state_and_user_choices_are_read() {
        let status = parse_status(YOUTUBE);
        assert_eq!(status["id"], "3041a01b-8aea");
        assert_eq!(status["domains"]["youtu.be"], "system_configured");
        assert_eq!(status["domains"]["www.example.com"], "1024");
        assert_eq!(status["verified"], json!(["youtu.be"]));
        assert_eq!(
            status["unverified"],
            json!(["example.com", "www.example.com"])
        );
        assert_eq!(status["link_handling_allowed"], true);
        assert_eq!(status["user_enabled"], json!(["example.com"]));
        assert_eq!(status["user_disabled"], json!(["youtu.be"]));
        assert_eq!(parse_status("")["domains"], json!({}));
    }

    #[test]
    fn the_handler_and_the_candidates_are_read() {
        let one = "priority=0 preferredOrder=0 match=0x208000 specificIndex=-1 isDefault=true\ncom.android.chrome/com.google.android.apps.chrome.IntentDispatcher\n";
        assert_eq!(
            resolved(one).as_deref(),
            Some("com.android.chrome/com.google.android.apps.chrome.IntentDispatcher")
        );
        assert_eq!(resolved("No activity found\n"), None);
        let all = "2 activities found:\n  Activity #0:\n    priority=0 match=0x508000\n    com.google.android.youtube/.UrlActivity\n  Activity #1:\n    priority=0\n    com.android.chrome/com.google.android.apps.chrome.IntentDispatcher\n";
        assert_eq!(
            candidates(all),
            [
                "com.google.android.youtube/.UrlActivity",
                "com.android.chrome/com.google.android.apps.chrome.IntentDispatcher"
            ]
        );
    }
}
