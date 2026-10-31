use crate::{core::*, jobs, preview, toolchain};
use serde_json::{Value, json};
use std::{
    os::unix::process::CommandExt,
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};
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
    if device_only {
        // Never raise an existing main window for a device-only request.
        if let Ok(state) = preview::call("state", json!({}), session, 3) {
            return Ok(json!({"session":session,"shown":false,"started":false,
                "device_only":true,"state":state["native"]["status"]}));
        }
    }
    match preview::call("show", show.clone(), session, 3) {
        Ok(value) => return Ok(value),
        Err(e) if e.code != "preview_unavailable" => return Err(e),
        _ => (),
    }
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
    let binary = candidates
        .into_iter()
        .find(|p| toolchain::executable(p))
        .ok_or_else(|| {
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
