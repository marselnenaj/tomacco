//! project.watch: rebuilds and runs an app again whenever its sources
//! change. A durable job polls the project's files, waits until they have
//! been quiet for the debounce time, then calls project.run and waits for
//! it; for a Flutter app it keeps one Flutter session and sends it Hot
//! Reload instead. Each cycle writes one line to the job's log.
use crate::{core::*, jobs, projects};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
    thread,
    time::{Duration, Instant},
};

/// Directories whose changes are build output, tool state or dependencies.
const IGNORED: &[&str] = &[
    "build",
    "Build",
    "DerivedData",
    "Pods",
    "node_modules",
    "target",
    "out",
    ".git",
    ".gradle",
    ".dart_tool",
    ".idea",
    ".cxx",
    ".kotlin",
    ".expo",
    ".build",
    ".swiftpm",
    ".vscode",
    ".fvm",
];
/// More files than this are not watched; the scan says so once.
const FILE_LIMIT: usize = 100_000;

pub fn watch(a: &Value) -> Result {
    let project = projects::inspect(s(a, "path"))?;
    identifier(s(a, "device"), "device identifier", None)?;
    let root = PathBuf::from(s(&project, "path"));
    let mut ignore: Vec<String> = IGNORED.iter().map(|name| name.to_string()).collect();
    for name in strings(&a["ignore"]) {
        if name.is_empty() || name.contains('/') || name == "." || name == ".." {
            return Err(Error::new(
                "invalid_argument",
                "ignore takes directory names, such as generated",
            ));
        }
        ignore.push(name);
    }
    let mut run = json!({"path":root,"platform":a["platform"],"device":a["device"]});
    for key in ["bundle_id", "configuration", "variant"] {
        if let Some(value) = a.get(key) {
            run[key] = value.clone();
        }
    }
    // The run is checked now, so that a project it cannot run is refused
    // before the job starts rather than in its first cycle.
    crate::api::arguments("project.run", &run)?;
    let flutter = project["kinds"]
        .as_array()
        .is_some_and(|kinds| kinds.iter().any(|k| k == "flutter"));
    let directory = home()?.join("watch");
    private_dir(&directory)?;
    let file = directory.join(format!("{}.json", uuid::Uuid::new_v4().simple()));
    let spec = json!({"root":root,"run":run,"flutter":flutter,"ignore":ignore,
                      "debounce_ms":a["debounce_ms"].as_u64().unwrap_or(800)});
    atomic_json(&file, &spec)?;
    jobs::submit(
        &format!("Watch {}", s(&project, "name")),
        json!({"steps":[{"argv":[own_executable()?,"_watch",file]}],"cwd":root,
               "timeout":a["timeout"].as_u64().unwrap_or(86400)}),
    )
}

/// Each file under root with its modification time and size, leaving out
/// ignored directories and links.
fn scan(root: &Path, ignore: &[String]) -> (BTreeMap<PathBuf, (u128, u64)>, bool) {
    let mut files = BTreeMap::new();
    let mut pending = vec![root.to_path_buf()];
    while let Some(directory) = pending.pop() {
        let Ok(entries) = std::fs::read_dir(&directory) else {
            continue;
        };
        for entry in entries.flatten() {
            let Ok(kind) = entry.file_type() else {
                continue;
            };
            let name = entry.file_name();
            if kind.is_dir() {
                if !ignore.iter().any(|i| *i == *name.to_string_lossy()) {
                    pending.push(entry.path());
                }
            } else if kind.is_file() {
                if files.len() >= FILE_LIMIT {
                    return (files, true);
                }
                if let Ok(metadata) = entry.metadata() {
                    let modified = metadata
                        .modified()
                        .ok()
                        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                        .map_or(0, |d| d.as_nanos());
                    let relative = entry
                        .path()
                        .strip_prefix(root)
                        .unwrap_or(&entry.path())
                        .to_path_buf();
                    files.insert(relative, (modified, metadata.len()));
                }
            }
        }
    }
    (files, false)
}
/// The files added, changed or removed between two scans.
fn changes(
    before: &BTreeMap<PathBuf, (u128, u64)>,
    after: &BTreeMap<PathBuf, (u128, u64)>,
) -> Vec<String> {
    let mut changed: Vec<String> = after
        .iter()
        .filter(|(file, stamp)| before.get(*file) != Some(stamp))
        .map(|(file, _)| file.to_string_lossy().into_owned())
        .collect();
    changed.extend(
        before
            .keys()
            .filter(|file| !after.contains_key(*file))
            .map(|file| file.to_string_lossy().into_owned()),
    );
    changed.sort();
    changed
}
fn describe(files: &[String]) -> String {
    let shown: Vec<&str> = files.iter().take(3).map(String::as_str).collect();
    let more = if files.len() > 3 {
        format!(", and {} more", files.len() - 3)
    } else {
        String::new()
    };
    format!(
        "{} file{} changed ({}{more})",
        files.len(),
        if files.len() == 1 { "" } else { "s" },
        shown.join(", ")
    )
}

/// What a cycle does: run the app once, or with a Flutter session reload it.
pub trait Runner {
    /// Starts project.run and waits for its job; the line to log.
    fn deploy(&mut self) -> String;
}
struct Live {
    run: Value,
    flutter: bool,
    session: Option<String>,
}
impl Runner for Live {
    fn deploy(&mut self) -> String {
        let started = Instant::now();
        if self.flutter {
            if let Some(id) = &self.session {
                let running = jobs::get(id).is_ok_and(|job| s(&job, "state") == "running");
                if running {
                    return match jobs::send(id, "reload") {
                        Ok(_) => format!("Hot Reload sent to Flutter session {id}"),
                        Err(error) => {
                            format!("Hot Reload could not be sent to {id}: {}", error.message)
                        }
                    };
                }
            }
            return match crate::api::dispatch("project.run", &self.run) {
                Ok(job) => {
                    self.session = Some(s(&job, "id").to_string());
                    format!("started Flutter session {}", s(&job, "id"))
                }
                Err(error) => format!("failed to start: {} ({})", error.message, error.code),
            };
        }
        let job = match crate::api::dispatch("project.run", &self.run) {
            Ok(job) => job,
            Err(error) => return format!("failed to start: {} ({})", error.message, error.code),
        };
        let id = s(&job, "id").to_string();
        loop {
            match jobs::wait(&id, 60) {
                Ok(done) if jobs::TERMINAL.contains(&s(&done, "state")) => {
                    let seconds = started.elapsed().as_secs_f64();
                    return if s(&done, "state") == "succeeded" {
                        format!("built, installed and launched in {seconds:.1} s (job {id})")
                    } else {
                        format!(
                            "{} after {seconds:.1} s; read job.logs of {id}",
                            s(&done, "state")
                        )
                    };
                }
                Ok(_) => (),
                Err(error) => return format!("lost job {id}: {}", error.message),
            }
        }
    }
}

/// The watch loop: one deployment at the start, then one per settled change.
/// `cycles` bounds it for tests; the job runs it until it is cancelled.
fn watch_loop(
    root: &Path,
    ignore: &[String],
    debounce: Duration,
    poll: Duration,
    runner: &mut impl Runner,
    log: &mut impl std::io::Write,
    cycles: Option<u64>,
) -> Result<()> {
    let (mut known, limited) = scan(root, ignore);
    writeln!(
        log,
        "watching {} files under {}{}",
        known.len(),
        root.display(),
        if limited {
            format!("; only the first {FILE_LIMIT} files are watched")
        } else {
            String::new()
        }
    )?;
    writeln!(log, "cycle 0: {}", runner.deploy())?;
    log.flush()?;
    let mut cycle = 0;
    while cycles.is_none_or(|limit| cycle < limit) {
        thread::sleep(poll);
        let (mut now, _) = scan(root, ignore);
        let mut changed = changes(&known, &now);
        if changed.is_empty() {
            continue;
        }
        // Debounce: wait until a scan finds nothing new.
        loop {
            thread::sleep(debounce);
            let (again, _) = scan(root, ignore);
            if again == now {
                break;
            }
            now = again;
        }
        changed = changes(&known, &now);
        known = now;
        if changed.is_empty() {
            continue;
        }
        cycle += 1;
        writeln!(
            log,
            "cycle {cycle}: {}: {}",
            describe(&changed),
            runner.deploy()
        )?;
        log.flush()?;
    }
    Ok(())
}
pub fn worker(file: &str) -> Result<()> {
    let spec = read_json(&path(file, false, true)?, None)?;
    let root = path(s(&spec, "root"), true, true)?;
    let mut runner = Live {
        run: spec["run"].clone(),
        flutter: b(&spec, "flutter"),
        session: None,
    };
    watch_loop(
        &root,
        &strings(&spec["ignore"]),
        Duration::from_millis(n(&spec, "debounce_ms").clamp(100, 10_000)),
        Duration::from_millis(500),
        &mut runner,
        &mut std::io::stdout(),
        None,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Fake(Vec<String>);
    impl Runner for Fake {
        fn deploy(&mut self) -> String {
            self.0.push("deploy".into());
            format!("deployed {}", self.0.len())
        }
    }
    #[test]
    fn changes_outside_build_directories_are_found_and_named() {
        let root = tempfile::tempdir().unwrap();
        let ignore: Vec<String> = IGNORED.iter().map(|s| s.to_string()).collect();
        std::fs::create_dir_all(root.path().join("app/src")).unwrap();
        std::fs::create_dir_all(root.path().join("app/build/outputs")).unwrap();
        std::fs::write(root.path().join("app/src/Main.kt"), "a").unwrap();
        let (before, limited) = scan(root.path(), &ignore);
        assert!(!limited);
        assert_eq!(before.len(), 1);
        std::fs::write(root.path().join("app/build/outputs/app.apk"), "x").unwrap();
        std::fs::write(root.path().join("app/src/Main.kt"), "ab").unwrap();
        std::fs::write(root.path().join("app/src/New.kt"), "b").unwrap();
        let (after, _) = scan(root.path(), &ignore);
        assert_eq!(
            changes(&before, &after),
            ["app/src/Main.kt", "app/src/New.kt"]
        );
        std::fs::remove_file(root.path().join("app/src/Main.kt")).unwrap();
        let (removed, _) = scan(root.path(), &ignore);
        assert_eq!(changes(&after, &removed), ["app/src/Main.kt"]);
        let many: Vec<String> = (0..5).map(|i| format!("f{i}")).collect();
        assert_eq!(describe(&many), "5 files changed (f0, f1, f2, and 2 more)");
        assert_eq!(describe(&many[..1]), "1 file changed (f0)");
    }
    #[test]
    fn a_settled_change_starts_one_cycle_and_build_output_none() {
        let root = tempfile::tempdir().unwrap();
        let ignore: Vec<String> = IGNORED.iter().map(|s| s.to_string()).collect();
        std::fs::create_dir_all(root.path().join("lib")).unwrap();
        std::fs::write(root.path().join("lib/main.dart"), "1").unwrap();
        let writer = {
            let root = root.path().to_path_buf();
            thread::spawn(move || {
                thread::sleep(Duration::from_millis(60));
                std::fs::create_dir_all(root.join("build")).unwrap();
                std::fs::write(root.join("build/out.bin"), "x").unwrap();
                // A burst of saves is one cycle.
                for i in 0..3 {
                    std::fs::write(root.join("lib/main.dart"), format!("change {i}")).unwrap();
                    thread::sleep(Duration::from_millis(10));
                }
            })
        };
        let mut runner = Fake(Vec::new());
        let mut log = Vec::new();
        watch_loop(
            root.path(),
            &ignore,
            Duration::from_millis(150),
            Duration::from_millis(20),
            &mut runner,
            &mut log,
            Some(1),
        )
        .unwrap();
        writer.join().unwrap();
        let text = String::from_utf8(log).unwrap();
        let lines: Vec<&str> = text.lines().collect();
        assert_eq!(lines.len(), 3, "{text}");
        assert!(lines[0].starts_with("watching 1 files under "));
        assert_eq!(lines[1], "cycle 0: deployed 1");
        assert_eq!(
            lines[2],
            "cycle 1: 1 file changed (lib/main.dart): deployed 2"
        );
    }
}
