use crate::{core::*, jobs, preview, toolchain};
use serde_json::{Value, json};
use std::{
    os::unix::process::CommandExt,
    path::PathBuf,
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};
/// The desktop program desktop.open starts: TOMACCO_BINARY, the one beside
/// this tomaccoctl, a build in this checkout, the release layout, or PATH.
pub fn binary() -> Result<Option<PathBuf>> {
    let exe = crate::core::own_executable()?;
    let dir = exe.parent().unwrap();
    let mut candidates = vec![dir.join("tomacco")];
    if let Some(root) = crate::core::source_checkout() {
        candidates.push(root.join("build-product/tomacco"));
        candidates.push(root.join("build/tomacco"));
    }
    candidates.push(dir.join("../../bin/tomacco"));
    if let Some(p) = std::env::var_os("TOMACCO_BINARY") {
        candidates.insert(0, p.into());
    }
    if let Some(p) = toolchain::which("tomacco") {
        candidates.push(p);
    }
    Ok(candidates.into_iter().find(|p| toolchain::executable(p)))
}

/// What the release desktop links against, for the advice when it cannot load.
pub const RELEASE_REQUIREMENTS: &str =
    "Qt 6.12 or newer, FFmpeg 9 (libavcodec.so.63) and glibc 2.39 or newer";

/// Whether the desktop program can be loaded on this system: the dynamic
/// loader's view (`ldd`) of the libraries and symbol versions it needs. The
/// release desktop is built on a current Arch Linux; on an older system it
/// stops before its window opens, with the loader's message only on stderr.
pub fn load_check() -> Value {
    let binary = match binary() {
        Ok(Some(binary)) => binary,
        Ok(None) => return json!({"installed":false,"path":null,"loadable":null}),
        Err(error) => {
            return json!({"installed":false,"path":null,"loadable":null,"error":error.value()});
        }
    };
    let mut report = json!({"installed":true,"path":binary,"loadable":null,
        "release_requires":RELEASE_REQUIREMENTS});
    let Some(ldd) = toolchain::which("ldd") else {
        report["reason"] = json!("ldd was not found, so the libraries were not checked");
        return report;
    };
    let args = [
        ldd.to_string_lossy().into_owned(),
        binary.to_string_lossy().into_owned(),
    ];
    match toolchain::run(&args, 10, None, false) {
        Ok(result) => {
            let text = format!("{}\n{}", s(&result, "stdout"), s(&result, "stderr"));
            let (libraries, versions) = loader_problems(&text);
            let dynamic = !text.contains("not a dynamic executable");
            report["loadable"] = json!(libraries.is_empty() && versions.is_empty());
            if result["exit_code"] != 0 && dynamic && libraries.is_empty() && versions.is_empty() {
                report["loadable"] = Value::Null;
                report["reason"] = json!(text.trim().chars().take(500).collect::<String>());
            }
            report["missing_libraries"] = json!(libraries);
            report["missing_versions"] = json!(versions);
        }
        Err(error) => report["error"] = error.value(),
    }
    report
}

/// The libraries the loader could not find, and the symbol versions a found
/// library is too old to have (`GLIBC_2.39 in libc.so.6`), from ldd's output.
fn loader_problems(text: &str) -> (Vec<String>, Vec<String>) {
    let mut libraries = Vec::new();
    let mut versions = Vec::new();
    for line in text.lines().map(str::trim) {
        if let Some(library) = line.strip_suffix("=> not found") {
            let library = library.trim().to_string();
            if !library.is_empty() && !libraries.contains(&library) {
                libraries.push(library);
            }
        } else if let Some((before, after)) = line.split_once("version `") {
            let Some((version, _)) = after.split_once('\'') else {
                continue;
            };
            if !after.contains("not found") {
                continue;
            }
            // "/usr/lib/libc.so.6: version `GLIBC_2.39' not found (required by ...)"
            let library = before
                .trim_end()
                .trim_end_matches(':')
                .rsplit(['/', ' '])
                .next()
                .unwrap_or("");
            let entry = if library.is_empty() {
                version.to_string()
            } else {
                format!("{version} in {library}")
            };
            if !versions.contains(&entry) {
                versions.push(entry);
            }
        }
    }
    (libraries, versions)
}

pub fn status() -> Result {
    let current = jobs::listing(200)?;
    let all = current.as_array().unwrap();
    let running = all
        .iter()
        .filter(|j| ["queued", "running"].contains(&s(j, "state")))
        .count();
    let jobs: Vec<_> = all
        .iter()
        .take(12)
        .map(|j| {
            let mut row = json!({});
            for key in ["id", "label", "state", "created", "updated", "exit_code"] {
                row[key] = j[key].clone();
            }
            row
        })
        .collect();
    let sessions: Vec<_> = preview::sessions()?
        .as_array()
        .unwrap()
        .iter()
        .map(|session| {
            let mut row = json!({});
            for key in ["session", "device", "url", "loading"] {
                row[key] = session[key].clone();
            }
            row
        })
        .collect();
    let workspaces = crate::workspaces::handle("workspace.list", &json!({}))?;
    let runtimes = crate::runtime::handle("runtime.list", &json!({}))?;
    Ok(
        json!({"running":running,"jobs":jobs,"sessions":sessions,"runtimes":runtimes,"workspaces":workspaces,"data_directory":home()?}),
    )
}
/// A desktop that answers on a preview session, as desktop.open talks to it.
trait Desktop {
    fn call(&self, method: &str, params: Value, timeout: u64) -> Result;
    /// The process behind the session, if it can be told.
    fn process(&self) -> Option<u32>;
    /// Ends that process without its cooperation.
    fn terminate(&self, pid: u32);
}

struct LiveDesktop<'a>(&'a str);

impl Desktop for LiveDesktop<'_> {
    fn call(&self, method: &str, params: Value, timeout: u64) -> Result {
        preview::call(method, params, self.0, timeout)
    }
    fn process(&self) -> Option<u32> {
        let pid = preview::peer_pid(self.0)?;
        // Only a Tomacco desktop is ended, never whatever else holds the socket.
        let exe = std::fs::read_link(format!("/proc/{pid}/exe")).ok()?;
        let name = exe.file_name()?.to_string_lossy().into_owned();
        (name == "tomacco" || name.starts_with("tomacco ")).then_some(pid)
    }
    fn terminate(&self, pid: u32) {
        // SAFETY: kill only sends a signal; pid came from the socket's peer credentials.
        unsafe {
            libc::kill(pid as libc::pid_t, libc::SIGTERM);
        }
    }
}

fn unsaved(running: &str, installed: &str) -> Error {
    Error::new(
        "desktop_unsaved",
        format!("The running Tomacco desktop ({running}) is not the installed version {installed}, and it has unsaved files."),
    )
    .remedy("Save or discard them in that window and close it, then open Tomacco again")
}

/// Ends a desktop on the session that is not the installed version, such as
/// the one that was running when an update was installed, so that the
/// installed one can start. Its answer to `quit` is asked for first; a
/// desktop from before `quit` existed is ended with SIGTERM, but only without
/// unsaved files. Returns the version it replaced (null when that desktop did
/// not say), or None when no desktop answers or the running one is current.
fn retire(desktop: &dyn Desktop, installed: &str, patience: Duration) -> Result<Option<Value>> {
    let state = match desktop.call("state", json!({}), 3) {
        Ok(state) => state,
        Err(e) if e.code == "preview_unavailable" => return Ok(None),
        Err(e) => return Err(e),
    };
    let version = s(&state, "version").to_string();
    if version == installed {
        return Ok(None);
    }
    let running = if version.is_empty() {
        "an older version".to_string()
    } else {
        version.clone()
    };
    let process = desktop.process();
    match desktop.call("quit", json!({"reason":"replaced"}), 3) {
        Ok(reply) if reply["quitting"] == true => {}
        Ok(_) => return Err(unsaved(&running, installed)),
        Err(e) if e.code == "preview_unavailable" => {
            return Ok(Some(if version.is_empty() {
                Value::Null
            } else {
                json!(version)
            }));
        }
        // A desktop from before quit existed refuses the unknown request.
        Err(e) if e.code == "preview_error" => {
            if state["workspace"]["dirty"]
                .as_array()
                .is_some_and(|dirty| !dirty.is_empty())
            {
                return Err(unsaved(&running, installed));
            }
            let pid = process.ok_or_else(|| {
                Error::new(
                    "desktop_stale",
                    format!("The running Tomacco desktop ({running}) is not the installed version {installed} and cannot be asked to quit."),
                )
                .remedy("Close that window, then open Tomacco again")
            })?;
            desktop.terminate(pid);
        }
        Err(e) => return Err(e),
    }
    let deadline = Instant::now() + patience;
    loop {
        match desktop.call("state", json!({}), 1) {
            Err(e) if e.code == "preview_unavailable" => break,
            _ if Instant::now() >= deadline => {
                return Err(Error::new(
                    "desktop_stale",
                    format!("The running Tomacco desktop ({running}) did not quit in time for the installed version {installed}."),
                )
                .remedy("Close that window, then open Tomacco again"));
            }
            _ => thread::sleep(Duration::from_millis(100)),
        }
    }
    Ok(Some(if version.is_empty() {
        Value::Null
    } else {
        json!(version)
    }))
}

pub fn open(a: &Value) -> Result {
    let session = s(a, "session");
    let tab = s(a, "tab");
    identifier(session, "session", Some("[A-Za-z0-9_-]{1,40}"))?;
    let mut show = json!({"tab":tab});
    if !s(a, "path").is_empty() {
        show["path"] = json!(path(s(a, "path"), true, true)?);
    }
    if !s(a, "file").is_empty() {
        show["file"] = a["file"].clone();
        show["line"] = a["line"].clone();
    }
    let device_only = b(a, "device_only");
    let replaced = retire(
        &LiveDesktop(session),
        &crate::update::current_version(),
        Duration::from_secs(10),
    )?;
    let note = |mut result: Value| {
        if let Some(version) = &replaced {
            result["replaced_version"] = version.clone();
        }
        result
    };
    if device_only {
        // Never raise an existing main window for a device-only request.
        if let Ok(state) = preview::call("state", json!({}), session, 3) {
            return Ok(note(
                json!({"session":session,"shown":false,"started":false,
                "device_only":true,"state":state["native"]["status"]}),
            ));
        }
    }
    match preview::call("show", show.clone(), session, 3) {
        Ok(value) => return Ok(note(value)),
        Err(e) if e.code != "preview_unavailable" => return Err(e),
        _ => (),
    }
    let binary = binary()?.ok_or_else(|| {
        Error::new("desktop_missing", "Tomacco desktop is not installed")
            .remedy("Build with CMake and run python3 install.py")
    })?;
    let log = private_file(&home()?.join("desktop.log"), true)?;
    let mut command = Command::new(binary);
    command
        .args(["--preview-session", session, "--workspace", tab])
        .args(if device_only {
            &["--device-only"][..]
        } else {
            &[][..]
        })
        .env("QT_QUICK_CONTROLS_STYLE", "Basic")
        .env("QT_QPA_PLATFORMTHEME", "generic")
        .stdin(Stdio::null())
        .stdout(log.try_clone()?)
        .stderr(log);
    unsafe {
        command.pre_exec(|| {
            if libc::setsid() < 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut child = command.spawn()?;
    let deadline = Instant::now() + Duration::from_secs(20);
    while Instant::now() < deadline {
        if child.try_wait()?.is_some() {
            return Err(
                Error::new("desktop_start_failed", "Desktop exited during startup")
                    .remedy(format!("Read {}", home()?.join("desktop.log").display())),
            );
        }
        let ready = if device_only {
            preview::call("state", json!({}), session, 2)
                .map(|_| json!({"session":session,"shown":false,"device_only":true}))
        } else {
            preview::call("show", show.clone(), session, 2)
        };
        if let Ok(mut result) = ready {
            result["started"] = json!(true);
            result["pid"] = json!(child.id());
            let result = note(result);
            thread::spawn(move || {
                let _ = child.wait();
            });
            return Ok(result);
        }
        thread::sleep(Duration::from_millis(200));
    }
    thread::spawn(move || {
        let _ = child.wait();
    });
    Err(Error::new(
        "desktop_start_timeout",
        "Desktop has not opened its control socket",
    )
    .remedy(format!("Read {}", home()?.join("desktop.log").display())))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::{Cell, RefCell};

    /// A desktop on a session: its version, how it answers quit, and whether it is still there.
    struct FakeDesktop {
        version: Option<&'static str>,
        knows_quit: bool,
        unsaved: bool,
        dirty: Vec<&'static str>,
        quits_on_request: bool,
        running: Cell<bool>,
        calls: RefCell<Vec<String>>,
        terminated: Cell<Option<u32>>,
    }
    impl FakeDesktop {
        fn new(version: Option<&'static str>) -> Self {
            FakeDesktop {
                version,
                knows_quit: true,
                unsaved: false,
                dirty: vec![],
                quits_on_request: true,
                running: Cell::new(true),
                calls: RefCell::new(vec![]),
                terminated: Cell::new(None),
            }
        }
        fn methods(&self) -> Vec<String> {
            self.calls.borrow().clone()
        }
    }
    impl Desktop for FakeDesktop {
        fn call(&self, method: &str, _params: Value, _timeout: u64) -> Result {
            self.calls.borrow_mut().push(method.to_string());
            if !self.running.get() {
                return Err(Error::new("preview_unavailable", "gone"));
            }
            match method {
                "state" => {
                    let mut state = json!({"session":"main","workspace":{"dirty":self.dirty}});
                    if let Some(version) = self.version {
                        state["version"] = json!(version);
                    }
                    Ok(state)
                }
                "quit" if !self.knows_quit => Err(Error::new(
                    "preview_error",
                    "Unknown preview operation: quit",
                )),
                "quit" if self.unsaved => Ok(json!({"quitting":false,"unsaved":true})),
                "quit" => {
                    if self.quits_on_request {
                        self.running.set(false);
                    }
                    Ok(json!({"quitting":true}))
                }
                _ => Ok(json!({})),
            }
        }
        fn process(&self) -> Option<u32> {
            Some(4242)
        }
        fn terminate(&self, pid: u32) {
            self.terminated.set(Some(pid));
            self.running.set(false);
        }
    }

    const SOON: Duration = Duration::from_millis(300);

    #[test]
    fn the_installed_version_is_left_running() {
        let desktop = FakeDesktop::new(Some("1.2.0"));
        assert_eq!(retire(&desktop, "1.2.0", SOON).unwrap(), None);
        assert_eq!(desktop.methods(), ["state"]);
        assert!(desktop.running.get());
    }

    #[test]
    fn no_desktop_means_nothing_to_replace() {
        let desktop = FakeDesktop::new(Some("1.1.0"));
        desktop.running.set(false);
        assert_eq!(retire(&desktop, "1.2.0", SOON).unwrap(), None);
    }

    #[test]
    fn an_older_desktop_is_asked_to_quit_and_its_version_reported() {
        let desktop = FakeDesktop::new(Some("1.1.0"));
        assert_eq!(
            retire(&desktop, "1.2.0", SOON).unwrap(),
            Some(json!("1.1.0"))
        );
        assert_eq!(desktop.methods(), ["state", "quit", "state"]);
        assert_eq!(desktop.terminated.get(), None);
    }

    #[test]
    fn a_desktop_with_unsaved_files_stays_and_says_why() {
        let desktop = FakeDesktop {
            unsaved: true,
            ..FakeDesktop::new(Some("1.1.0"))
        };
        let error = retire(&desktop, "1.2.0", SOON).unwrap_err();
        assert_eq!(error.code, "desktop_unsaved");
        assert!(desktop.running.get());
    }

    #[test]
    fn a_desktop_from_before_quit_is_ended_only_without_unsaved_files() {
        let old = FakeDesktop {
            knows_quit: false,
            ..FakeDesktop::new(None)
        };
        assert_eq!(retire(&old, "1.2.0", SOON).unwrap(), Some(Value::Null));
        assert_eq!(old.terminated.get(), Some(4242));
        let busy = FakeDesktop {
            knows_quit: false,
            dirty: vec!["lib/main.dart"],
            ..FakeDesktop::new(None)
        };
        assert_eq!(
            retire(&busy, "1.2.0", SOON).unwrap_err().code,
            "desktop_unsaved"
        );
        assert_eq!(busy.terminated.get(), None);
    }

    #[test]
    fn a_desktop_that_does_not_quit_in_time_is_an_error() {
        let stuck = FakeDesktop {
            quits_on_request: false,
            ..FakeDesktop::new(Some("1.1.0"))
        };
        assert_eq!(
            retire(&stuck, "1.2.0", SOON).unwrap_err().code,
            "desktop_stale"
        );
    }

    #[test]
    fn the_loaders_complaints_are_read_from_ldd() {
        let text = "\tlinux-vdso.so.1 (0x00007ffd)\n\
\tlibQt6Quick.so.6 => not found\n\
\tlibavcodec.so.63 => not found\n\
\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x00007f)\n\
/opt/tomacco/tomacco: /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.39' not found (required by /opt/tomacco/tomacco)\n\
/opt/tomacco/tomacco: /usr/lib/libQt6Core.so.6: version `Qt_6.12' not found (required by /opt/tomacco/tomacco)\n\
/opt/tomacco/tomacco: /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.39' not found (required by /usr/lib/libfoo.so)\n";
        let (libraries, versions) = loader_problems(text);
        assert_eq!(libraries, ["libQt6Quick.so.6", "libavcodec.so.63"]);
        assert_eq!(
            versions,
            ["GLIBC_2.39 in libc.so.6", "Qt_6.12 in libQt6Core.so.6"]
        );
        let healthy = "\tlinux-vdso.so.1 (0x00007ffd)\n\tlibc.so.6 => /usr/lib/libc.so.6 (0x7f)\n";
        assert_eq!(loader_problems(healthy), (vec![], vec![]));
    }
}
