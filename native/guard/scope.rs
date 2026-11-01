//! Where the operations an agent calls may write, under a policy whose
//! `writes` is `roots`: inside the write roots, never into Tomacco's own
//! settings or audit log.
//!
//! The roots are Tomacco's data directory, the temporary directory, the
//! policy's `write_roots`, the directories TOMACCO_WRITE_ROOTS lists, and the
//! directory the server or command was started in, unless that is `/`, the
//! home directory or above it. Only destinations an operation takes as an
//! argument are checked; what a build's own scripts write is not.
use crate::core::*;
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

pub const VARIABLE: &str = "TOMACCO_WRITE_ROOTS";

/// The arguments each operation writes to: a file or directory it creates
/// or replaces, or the project directory it changes.
const DESTINATIONS: &[(&str, &[&str])] = &[
    ("catalog.export", &["path"]),
    ("project.create", &["path"]),
    ("project.configure", &["path"]),
    ("project.import_xcode", &["out"]),
    ("diagnostics.bundle", &["path"]),
    ("device.screenshot", &["path"]),
    ("device.capture", &["path"]),
    ("android.screen", &["path"]),
    ("screen.marks", &["path", "xml_path"]),
    ("screen.tree", &["xml_path"]),
    ("android.record", &["path"]),
    ("android.pair", &["path"]),
    ("android.store_screenshots", &["directory"]),
    ("android.app_files", &["output"]),
    ("ios.app_files", &["output"]),
    ("ios.record", &["path"]),
    ("ios.crash_reports", &["directory"]),
    ("remote.fetch", &["path"]),
    ("preview.capture", &["path"]),
    ("preview.matrix", &["directory"]),
    ("testing.run", &["directory"]),
    ("testing.record", &["path"]),
    ("profile.capture", &["path"]),
    ("profile.analyze", &["report"]),
    ("profile.compare", &["report"]),
    ("profile.startup", &["report"]),
    ("profile.frames", &["report"]),
    ("profile.memory", &["report"]),
    ("profile.usage", &["report"]),
    ("profile.budget", &["report"]),
    ("profile.macrobenchmark", &["report"]),
    ("profile.baseline_profile", &["report"]),
    ("profile.baseline_install", &["path"]),
    ("profile.baseline_compare", &["report"]),
    ("files.write", &["path"]),
    ("files.mkdir", &["path"]),
    ("release.keystore", &["output"]),
    ("release.sign", &["output"]),
    ("release.version", &["path"]),
    ("release.notes", &["directory"]),
    ("runtime.capture", &["path"]),
    ("ios.virtual.crash_reports", &["directory"]),
    ("ios.virtual.screenshot", &["path"]),
    ("ios.virtual.record", &["path"]),
    ("ios.emulator.build_gpu_plugin", &["output"]),
    ("ios.emulator.prepare_userland", &["output"]),
    ("ios.emulator.prepare_restore", &["output"]),
    ("ios.emulator.build_guest_tools", &["output"]),
    ("ios.emulator.prepare_guest_tools", &["output"]),
    ("ios.emulator.prepare_cryptexes", &["output"]),
    ("ios.emulator.prepare_research", &["output"]),
    ("ios.emulator.prepare_pmp", &["output"]),
];

/// The arguments of an operation that name where it writes.
pub fn destinations(name: &str) -> &'static [&'static str] {
    DESTINATIONS
        .iter()
        .find(|(op, _)| *op == name)
        .map_or(&[], |(_, keys)| keys)
}

/// The write roots and the paths inside them that stay closed.
#[derive(Debug, Clone)]
pub struct Roots {
    pub open: Vec<PathBuf>,
    pub closed: Vec<PathBuf>,
}

fn resolved(p: &Path) -> Option<PathBuf> {
    path(&p.to_string_lossy(), false, false).ok()
}

impl Roots {
    /// The roots for a policy's own `write_roots`, from this process's
    /// environment and working directory.
    pub fn current(write_roots: &[String]) -> Roots {
        let data = home().ok();
        let mut named: Vec<PathBuf> = data.iter().cloned().collect();
        named.push(std::env::temp_dir());
        named.extend(write_roots.iter().map(PathBuf::from));
        if let Ok(listed) = std::env::var(VARIABLE) {
            named.extend(
                listed
                    .split(':')
                    .filter(|p| Path::new(p).is_absolute() && *p != "/")
                    .map(PathBuf::from),
            );
        }
        if let Ok(directory) = std::env::current_dir() {
            let user = user_home();
            if directory != Path::new("/") && !user.starts_with(&directory) {
                named.push(directory);
            }
        }
        let mut open: Vec<PathBuf> = Vec::new();
        for root in named.iter().filter_map(|p| resolved(p)) {
            if !open.contains(&root) {
                open.push(root);
            }
        }
        let closed = data
            .map(|data| {
                ["settings.json", "settings.lock", "audit", "results"]
                    .iter()
                    .filter_map(|name| resolved(&data.join(name)))
                    .collect()
            })
            .unwrap_or_default();
        Roots { open, closed }
    }
    /// Refuses a destination outside the roots, or inside a closed path.
    pub fn check(&self, operation: &str, policy: &str, key: &str, value: &str) -> Result<()> {
        let target = path(value, false, false)?;
        let inside = |root: &PathBuf| target.starts_with(root);
        if let Some(closed) = self.closed.iter().find(|c| inside(c)) {
            return Err(Error::new(
                "path_outside_roots",
                format!(
                    "{operation} may not write {} under the agent policy {policy}: Tomacco's own settings, audit log and saved results stay closed to agents",
                    closed.display()
                ),
            )
            .remedy("Write somewhere else; the person changes settings from a terminal or the desktop")
            .details(json!({"argument":key,"path":target,"policy":policy})));
        }
        if self.open.iter().any(inside) {
            return Ok(());
        }
        Err(Error::new(
            "path_outside_roots",
            format!(
                "{operation} may write only inside the write roots under the agent policy {policy}, and arguments.{key} names {}",
                target.display()
            ),
        )
        .remedy(format!(
            "Write inside one of {}, or ask the person to add the folder to the policy's write_roots in agent_policy, or to {VARIABLE} for this server",
            self.open
                .iter()
                .map(|p| p.display().to_string())
                .collect::<Vec<_>>()
                .join(", ")
        ))
        .details(json!({"argument":key,"path":target,"policy":policy,"roots":self.open})))
    }
    pub fn value(&self) -> Value {
        json!({"open":self.open,"closed":self.closed})
    }
}

/// Checks every destination an operation's arguments name.
pub fn check(name: &str, arguments: &Value, policy: &str, roots: &Roots) -> Result<()> {
    for key in destinations(name) {
        if let Some(value) = arguments[*key].as_str().filter(|v| !v.trim().is_empty()) {
            roots.check(name, policy, key, value)?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn roots(open: &[&Path], closed: &[&Path]) -> Roots {
        Roots {
            open: open.iter().map(|p| p.canonicalize().unwrap()).collect(),
            closed: closed.iter().map(|p| resolved(p).unwrap()).collect(),
        }
    }
    #[test]
    fn writes_stay_inside_the_roots_also_through_links_and_dots() {
        let temp = tempfile::tempdir().unwrap();
        let project = temp.path().join("project");
        let outside = temp.path().join("outside");
        std::fs::create_dir_all(&project).unwrap();
        std::fs::create_dir_all(&outside).unwrap();
        std::os::unix::fs::symlink(&outside, project.join("escape")).unwrap();
        let data = temp.path().join("data");
        std::fs::create_dir_all(&data).unwrap();
        let roots = roots(&[&project, &data], &[&data.join("settings.json")]);
        let ok = |args: Value| check("files.write", &args, "safe", &roots);
        assert!(ok(json!({"path":project})).is_ok());
        assert!(ok(json!({"path":project.join("new/deeper")})).is_ok());
        for bad in [
            outside.clone(),
            project.join("escape"),
            project.join("escape/new"),
            project.join("../outside"),
        ] {
            let error = ok(json!({ "path": bad })).unwrap_err();
            assert_eq!(error.code, "path_outside_roots", "{}", bad.display());
            assert!(error.message.contains("arguments.path"));
        }
        let error = check(
            "diagnostics.bundle",
            &json!({"path":data.join("settings.json")}),
            "safe",
            &roots,
        )
        .unwrap_err();
        assert!(error.message.contains("stay closed"), "{}", error.message);
        // An operation that writes nowhere, and an empty destination, pass.
        assert!(check("project.run", &json!({"path":outside}), "safe", &roots).is_ok());
        assert!(check("device.capture", &json!({"path":""}), "safe", &roots).is_ok());
    }
    #[test]
    fn every_destination_is_an_argument_of_its_operation() {
        for (name, keys) in DESTINATIONS {
            let op = crate::api::operation(name).unwrap();
            assert_ne!(op["annotations"]["readOnlyHint"], true, "{name}");
            for key in *keys {
                assert!(
                    op["inputSchema"]["properties"].get(*key).is_some(),
                    "{name} has no argument {key}"
                );
            }
        }
    }
}
