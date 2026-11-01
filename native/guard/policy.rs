//! The agent policy: which operations an agent may call through `tomaccoctl
//! call` or the MCP server, and where the operations it calls may write.
//!
//! It lives in settings.json as `agent_policy`:
//!
//! ```json
//! {"mcp": "no_destructive", "cli": "full", "clients": {"codex": "read_only"},
//!  "policies": {"builds": {"mode": "no_destructive", "allow": ["project.", "job."],
//!               "deny": ["play."], "writes": "roots", "write_roots": ["/home/me/src"]}},
//!  "approval": {"destructive": true, "timeout_seconds": 30, "without_desktop": "allow"},
//!  "limits": {"jobs_per_session": 8, "inline_result_kib": 2048},
//!  "audit": true}
//! ```
//!
//! `full`, `no_destructive` and `read_only` always exist; a named policy may
//! not reuse those names. Nothing configured means `full` for every caller,
//! which is how Tomacco behaved before.
use crate::core::*;
use serde_json::{Value, json};

pub const BUILT_IN: [&str; 3] = ["full", "no_destructive", "read_only"];
const MODES: [&str; 3] = BUILT_IN;
const TOP_KEYS: [&str; 7] = [
    "mcp", "cli", "clients", "policies", "approval", "limits", "audit",
];
const POLICY_KEYS: [&str; 5] = ["mode", "allow", "deny", "writes", "write_roots"];

/// One policy, built in or named in settings.
#[derive(Clone, Debug, PartialEq)]
pub struct Policy {
    pub name: String,
    pub mode: String,
    pub allow: Vec<String>,
    pub deny: Vec<String>,
    /// Whether writes are limited to the roots (scope.rs).
    pub roots_only: bool,
    pub write_roots: Vec<String>,
}

impl Policy {
    fn built_in(name: &str) -> Option<Policy> {
        BUILT_IN.contains(&name).then(|| Policy {
            name: name.into(),
            mode: name.into(),
            allow: Vec::new(),
            deny: Vec::new(),
            roots_only: name != "full",
            write_roots: Vec::new(),
        })
    }
    fn named(name: &str, spec: &Value) -> Policy {
        let mode = spec["mode"].as_str().unwrap_or("full").to_string();
        Policy {
            name: name.into(),
            roots_only: spec["writes"].as_str().unwrap_or("roots") == "roots",
            mode,
            allow: strings(&spec["allow"]),
            deny: strings(&spec["deny"]),
            write_roots: strings(&spec["write_roots"]),
        }
    }
    /// Whether this policy changes nothing about what a caller may do.
    pub fn unrestricted(&self) -> bool {
        self.mode == "full" && self.allow.is_empty() && self.deny.is_empty() && !self.roots_only
    }
    /// Why the policy refuses an operation with these annotations, or None.
    pub fn refusal(&self, name: &str, annotations: &Value) -> Option<String> {
        if let Some(entry) = self.deny.iter().find(|entry| matches(entry, name)) {
            return Some(format!("its deny list names {entry}"));
        }
        if !self.allow.is_empty() && !self.allow.iter().any(|entry| matches(entry, name)) {
            return Some("its allow list does not name it".into());
        }
        let read_only = annotations["readOnlyHint"] == true;
        // A missing destructiveHint means destructive, as MCP reads it.
        let destructive = annotations["destructiveHint"] != false;
        match self.mode.as_str() {
            "read_only" if !read_only => Some("it allows only read-only operations".into()),
            "no_destructive" if destructive && !read_only => {
                Some("it allows no destructive operations".into())
            }
            _ => None,
        }
    }
    pub fn value(&self) -> Value {
        json!({"name":self.name,"mode":self.mode,"allow":self.allow,"deny":self.deny,
               "writes":if self.roots_only {"roots"} else {"anywhere"},"write_roots":self.write_roots})
    }
}

/// An allow or deny entry: an operation's name, a prefix ending in a dot
/// (`play.`, also written `play.*`), or `*` for every operation.
fn matches(entry: &str, name: &str) -> bool {
    let entry = entry.strip_suffix('*').unwrap_or(entry);
    entry.is_empty() || entry == name || (entry.ends_with('.') && name.starts_with(entry))
}

/// The `agent_policy` object of settings.json, or an empty one.
#[derive(Clone, Debug, Default)]
pub struct Settings(pub Value);

impl Settings {
    /// Reads settings.json. A file that cannot be read or holds an invalid
    /// policy is an error: the guard fails closed rather than open.
    pub fn load() -> Result<Settings> {
        let all = crate::toolchain::settings()?;
        let policy = all.get("agent_policy").cloned().unwrap_or(json!({}));
        validated(&policy).map_err(|error| {
            Error::new(
                "policy_invalid",
                format!("agent_policy in settings.json is invalid: {}", error.message),
            )
            .remedy("Correct agent_policy with tomaccoctl call settings.configure from a terminal, or edit settings.json")
        })?;
        Ok(Settings(policy))
    }
    /// The policy a name selects; an unknown name is an error.
    pub fn named(&self, name: &str) -> Result<Policy> {
        if let Some(policy) = Policy::built_in(name) {
            return Ok(policy);
        }
        match self.0["policies"].get(name) {
            Some(spec) => Ok(Policy::named(name, spec)),
            None => Err(Error::new(
                "policy_denied",
                format!("The agent policy {name} is not defined, so nothing is allowed"),
            )
            .remedy(format!(
                "Use full, no_destructive, read_only or a policy that agent_policy.policies in settings.json defines; known: {}",
                self.known().join(", ")
            ))
            .details(json!({"policy":name}))),
        }
    }
    fn known(&self) -> Vec<String> {
        let mut names: Vec<String> = BUILT_IN.iter().map(|n| n.to_string()).collect();
        if let Some(defined) = self.0["policies"].as_object() {
            names.extend(defined.keys().cloned());
        }
        names
    }
    /// The name of the policy for a caller, and where it came from: the
    /// server's own choice (`--policy` or TOMACCO_POLICY), the client's entry,
    /// the channel's default, or `full`.
    pub fn choose(&self, mcp: bool, client: &str, chosen: Option<&str>) -> (String, &'static str) {
        if let Some(name) = chosen.filter(|n| !n.is_empty()) {
            return (name.to_string(), "server");
        }
        if mcp {
            if let Some(name) = self.0["clients"][client].as_str() {
                return (name.to_string(), "client");
            }
        }
        match self.0[if mcp { "mcp" } else { "cli" }].as_str() {
            Some(name) => (name.to_string(), "default"),
            None => ("full".to_string(), "built_in"),
        }
    }
    pub fn approval_enabled(&self) -> bool {
        self.0["approval"]["destructive"] == true
    }
    pub fn approval_timeout(&self) -> u64 {
        self.0["approval"]["timeout_seconds"].as_u64().unwrap_or(30)
    }
    pub fn approval_session(&self) -> String {
        self.0["approval"]["session"]
            .as_str()
            .unwrap_or("main")
            .to_string()
    }
    /// Whether a destructive call runs when no desktop answers.
    pub fn approval_without_desktop(&self) -> bool {
        self.0["approval"]["without_desktop"].as_str() != Some("deny")
    }
    /// At most this many jobs one MCP session has started run at once; 0 is no limit.
    pub fn jobs_per_session(&self) -> u64 {
        self.0["limits"]["jobs_per_session"].as_u64().unwrap_or(8)
    }
    /// The largest result an MCP session gets inline, in bytes.
    pub fn inline_result_bytes(&self) -> usize {
        self.0["limits"]["inline_result_kib"]
            .as_u64()
            .unwrap_or(2048) as usize
            * 1024
    }
    pub fn audit(&self) -> bool {
        self.0["audit"] != false
    }
}

fn invalid(message: impl Into<String>) -> Error {
    Error::new("invalid_argument", message)
}
fn names_list(value: &Value, label: &str) -> Result<()> {
    let Some(items) = value.as_array() else {
        return Err(invalid(format!(
            "{label} must be a list of operation names"
        )));
    };
    for item in items {
        let entry = item.as_str().unwrap_or_default();
        let bare = entry.strip_suffix('*').unwrap_or(entry);
        let known = bare.is_empty()
            || (bare.ends_with('.') && crate::api::names().any(|n| n.starts_with(bare)))
            || crate::api::operation(bare).is_ok();
        if entry.is_empty() || !known {
            return Err(invalid(format!(
                "{label} names {item}, which is no operation, prefix ending in a dot, or *"
            )));
        }
    }
    Ok(())
}
fn policy_name(value: &Value, label: &str, defined: &Value) -> Result<()> {
    let name = value.as_str().unwrap_or_default();
    if BUILT_IN.contains(&name) || defined.get(name).is_some() {
        return Ok(());
    }
    Err(invalid(format!(
        "{label} names the policy {value}, which is neither built in nor in policies"
    )))
}
fn bounded(value: &Value, label: &str, min: u64, max: u64) -> Result<()> {
    match value.as_u64() {
        Some(n) if (min..=max).contains(&n) => Ok(()),
        _ => Err(invalid(format!(
            "{label} must be a whole number from {min} to {max}"
        ))),
    }
}

/// Checks an `agent_policy` object, as settings.configure takes it and
/// settings.json holds it, and returns it unchanged.
pub fn validated(policy: &Value) -> Result {
    let Some(top) = policy.as_object() else {
        return Err(invalid("agent_policy must be an object"));
    };
    if let Some(key) = top.keys().find(|k| !TOP_KEYS.contains(&k.as_str())) {
        return Err(invalid(format!(
            "agent_policy has no key {key}; it takes {}",
            TOP_KEYS.join(", ")
        )));
    }
    let defined = policy.get("policies").cloned().unwrap_or(json!({}));
    let Some(policies) = defined.as_object() else {
        return Err(invalid("agent_policy.policies must be an object"));
    };
    for (name, spec) in policies {
        identifier(name, "policy name", Some("[A-Za-z0-9_-]{1,40}"))?;
        if BUILT_IN.contains(&name.as_str()) {
            return Err(invalid(format!(
                "agent_policy.policies may not redefine the built-in policy {name}"
            )));
        }
        let Some(fields) = spec.as_object() else {
            return Err(invalid(format!(
                "agent_policy.policies.{name} must be an object"
            )));
        };
        if let Some(key) = fields.keys().find(|k| !POLICY_KEYS.contains(&k.as_str())) {
            return Err(invalid(format!(
                "agent_policy.policies.{name} has no key {key}; it takes {}",
                POLICY_KEYS.join(", ")
            )));
        }
        let label = format!("agent_policy.policies.{name}");
        if let Some(mode) = spec.get("mode") {
            if !mode.as_str().is_some_and(|m| MODES.contains(&m)) {
                return Err(invalid(format!(
                    "{label}.mode must be one of {}",
                    MODES.join(", ")
                )));
            }
        }
        for key in ["allow", "deny"] {
            if let Some(list) = spec.get(key) {
                names_list(list, &format!("{label}.{key}"))?;
            }
        }
        if let Some(writes) = spec.get("writes") {
            if !matches!(writes.as_str(), Some("roots" | "anywhere")) {
                return Err(invalid(format!("{label}.writes must be roots or anywhere")));
            }
        }
        if let Some(roots) = spec.get("write_roots") {
            let ok = roots.as_array().is_some_and(|items| {
                items.iter().all(|item| {
                    item.as_str()
                        .is_some_and(|p| std::path::Path::new(p).is_absolute() && p != "/")
                })
            });
            if !ok {
                return Err(invalid(format!(
                    "{label}.write_roots must be a list of absolute directories other than /"
                )));
            }
        }
    }
    for key in ["mcp", "cli"] {
        if let Some(name) = policy.get(key) {
            policy_name(name, &format!("agent_policy.{key}"), &defined)?;
        }
    }
    if let Some(clients) = policy.get("clients") {
        let Some(clients) = clients.as_object() else {
            return Err(invalid("agent_policy.clients must be an object"));
        };
        for (client, name) in clients {
            policy_name(name, &format!("agent_policy.clients.{client}"), &defined)?;
        }
    }
    if let Some(approval) = policy.get("approval") {
        let Some(fields) = approval.as_object() else {
            return Err(invalid("agent_policy.approval must be an object"));
        };
        for (key, value) in fields {
            match key.as_str() {
                "destructive" if value.is_boolean() => {}
                "timeout_seconds" => {
                    bounded(value, "agent_policy.approval.timeout_seconds", 5, 30)?
                }
                "without_desktop" if matches!(value.as_str(), Some("allow" | "deny")) => {}
                "session" if value.as_str().is_some() => {
                    identifier(
                        s(approval, "session"),
                        "approval session",
                        Some("[A-Za-z0-9_-]{1,40}"),
                    )?;
                }
                _ => {
                    return Err(invalid(format!(
                        "agent_policy.approval.{key} is not valid; it takes destructive (true or false), timeout_seconds (5 to 30), without_desktop (allow or deny) and session"
                    )));
                }
            }
        }
    }
    if let Some(limits) = policy.get("limits") {
        let Some(fields) = limits.as_object() else {
            return Err(invalid("agent_policy.limits must be an object"));
        };
        for (key, value) in fields {
            match key.as_str() {
                "jobs_per_session" => {
                    bounded(value, "agent_policy.limits.jobs_per_session", 0, 64)?
                }
                "inline_result_kib" => {
                    bounded(value, "agent_policy.limits.inline_result_kib", 1, 16384)?
                }
                _ => {
                    return Err(invalid(format!(
                        "agent_policy.limits has no key {key}; it takes jobs_per_session and inline_result_kib"
                    )));
                }
            }
        }
    }
    if policy.get("audit").is_some_and(|v| !v.is_boolean()) {
        return Err(invalid("agent_policy.audit must be true or false"));
    }
    Ok(policy.clone())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn hints(read_only: bool, destructive: bool) -> Value {
        json!({"readOnlyHint":read_only,"destructiveHint":destructive,"idempotentHint":read_only,"openWorldHint":false})
    }
    #[test]
    fn the_built_in_policies_refuse_by_annotation() {
        let settings = Settings::default();
        let full = settings.named("full").unwrap();
        assert!(full.unrestricted());
        assert_eq!(full.refusal("files.write", &hints(false, true)), None);
        let safe = settings.named("no_destructive").unwrap();
        assert!(safe.roots_only);
        assert_eq!(safe.refusal("project.run", &hints(false, false)), None);
        assert_eq!(safe.refusal("doctor", &hints(true, false)), None);
        assert!(safe.refusal("files.write", &hints(false, true)).is_some());
        // An operation that does not say is destructive, as MCP reads it.
        assert!(
            safe.refusal("x.y", &json!({"readOnlyHint":false}))
                .is_some()
        );
        let reader = settings.named("read_only").unwrap();
        assert_eq!(reader.refusal("job.wait", &hints(true, false)), None);
        assert!(
            reader
                .refusal("project.run", &hints(false, false))
                .unwrap()
                .contains("read-only")
        );
    }
    #[test]
    fn allow_and_deny_lists_take_names_prefixes_and_a_star() {
        let settings = Settings(
            json!({"policies":{"builds":{"allow":["project.","job.*","doctor"],"deny":["project.watch"]}}}),
        );
        let builds = settings.named("builds").unwrap();
        assert_eq!(builds.mode, "full");
        assert!(builds.roots_only);
        let open = hints(false, false);
        assert_eq!(builds.refusal("project.run", &open), None);
        assert_eq!(builds.refusal("job.cancel", &hints(false, true)), None);
        assert_eq!(builds.refusal("doctor", &hints(true, false)), None);
        assert_eq!(
            builds.refusal("project.watch", &open).unwrap(),
            "its deny list names project.watch"
        );
        assert!(builds.refusal("device.install", &open).is_some());
        // A prefix ends at a dot: job. does not take jobs.x.
        assert!(!matches("job.", "jobs.list"));
        assert!(matches("*", "anything"));
        let error = settings.named("missing").unwrap_err();
        assert_eq!(error.code, "policy_denied");
        assert!(error.remedy.contains("builds"));
    }
    #[test]
    fn the_server_choice_beats_the_client_entry_and_the_default() {
        let settings = Settings(
            json!({"mcp":"no_destructive","cli":"read_only","clients":{"codex":"read_only"}}),
        );
        assert_eq!(
            settings.choose(true, "codex", Some("full")),
            ("full".into(), "server")
        );
        assert_eq!(
            settings.choose(true, "codex", None),
            ("read_only".into(), "client")
        );
        assert_eq!(
            settings.choose(true, "other", None),
            ("no_destructive".into(), "default")
        );
        assert_eq!(
            settings.choose(false, "codex", None),
            ("read_only".into(), "default")
        );
        assert_eq!(
            Settings::default().choose(true, "x", Some("")),
            ("full".into(), "built_in")
        );
        assert_eq!(Settings::default().jobs_per_session(), 8);
        assert_eq!(Settings::default().inline_result_bytes(), 2048 * 1024);
        assert!(Settings::default().audit());
        assert!(!Settings::default().approval_enabled());
        assert!(Settings::default().approval_without_desktop());
    }
    #[test]
    fn an_invalid_policy_is_refused_with_the_reason() {
        let ok = json!({"mcp":"builds","clients":{"claude-code":"read_only"},
            "policies":{"builds":{"mode":"no_destructive","allow":["project."],"deny":["play.*"],"writes":"roots","write_roots":["/src"]}},
            "approval":{"destructive":true,"timeout_seconds":20,"without_desktop":"deny","session":"main"},
            "limits":{"jobs_per_session":2,"inline_result_kib":64},"audit":false});
        assert_eq!(validated(&ok).unwrap(), ok);
        assert!(validated(&json!({})).is_ok());
        for (bad, says) in [
            (json!([]), "must be an object"),
            (json!({"mpc":"full"}), "no key mpc"),
            (json!({"mcp":"nothing"}), "neither built in"),
            (json!({"policies":{"full":{}}}), "may not redefine"),
            (json!({"policies":{"x":{"mode":"open"}}}), "mode must be"),
            (
                json!({"policies":{"x":{"allow":["projct.run"]}}}),
                "no operation",
            ),
            (json!({"policies":{"x":{"deny":"play."}}}), "must be a list"),
            (
                json!({"policies":{"x":{"write_roots":["relative"]}}}),
                "absolute",
            ),
            (json!({"policies":{"x":{"write_roots":["/"]}}}), "absolute"),
            (
                json!({"policies":{"x":{"writes":"some"}}}),
                "roots or anywhere",
            ),
            (json!({"approval":{"timeout_seconds":300}}), "5 to 30"),
            (json!({"approval":{"destructive":"yes"}}), "is not valid"),
            (json!({"limits":{"jobs_per_session":-1}}), "0 to 64"),
            (json!({"limits":{"results":1}}), "no key results"),
            (json!({"audit":"off"}), "true or false"),
        ] {
            let error = validated(&bad).unwrap_err();
            assert!(error.message.contains(says), "{bad}: {}", error.message);
        }
    }
}
