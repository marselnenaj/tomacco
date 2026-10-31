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
