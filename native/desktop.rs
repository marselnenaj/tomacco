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
/// What the portable package's desktop needs from the system (tools/build_portable.py).
pub const PORTABLE_REQUIREMENTS: &str =
    "glibc 2.35 or newer and the system libraries listed in share/doc/tomacco/PORTABLE.md";

/// The packages that provide each library the portable package expects from
/// the system: (library, Debian and Ubuntu package, Fedora package). Read with
/// dpkg -S and rpm -qf in debian:12, ubuntu:22.04, ubuntu:24.04, debian:13 and
/// fedora:44 containers.
const HOST_PACKAGES: &[(&str, &str, &str)] = &[
    ("ld-linux-x86-64.so.2", "libc6", "glibc"),
    ("libc.so.6", "libc6", "glibc"),
    ("libdl.so.2", "libc6", "glibc"),
    ("libm.so.6", "libc6", "glibc"),
    ("libpthread.so.0", "libc6", "glibc"),
    ("libresolv.so.2", "libc6", "glibc"),
    ("libgcc_s.so.1", "libgcc-s1", "libgcc"),
    ("libstdc++.so.6", "libstdc++6", "libstdc++"),
    ("libz.so.1", "zlib1g", "zlib-ng-compat"),
    ("libzstd.so.1", "libzstd1", "libzstd"),
    ("libexpat.so.1", "libexpat1", "expat"),
    ("libudev.so.1", "libudev1", "systemd-libs"),
    ("libdbus-1.so.3", "libdbus-1-3", "dbus-libs"),
    ("libasound.so.2", "libasound2", "alsa-lib"),
    ("libfontconfig.so.1", "libfontconfig1", "fontconfig"),
    ("libfreetype.so.6", "libfreetype6", "freetype"),
    ("libgbm.so.1", "libgbm1", "mesa-libgbm"),
    ("libEGL.so.1", "libegl1", "libglvnd-egl"),
    ("libGL.so.1", "libgl1", "libglvnd-glx"),
    ("libGLX.so.0", "libglx0", "libglvnd-glx"),
    ("libOpenGL.so.0", "libopengl0", "libglvnd-opengl"),
    ("libglib-2.0.so.0", "libglib2.0-0", "glib2"),
    ("libgio-2.0.so.0", "libglib2.0-0", "glib2"),
    ("libgobject-2.0.so.0", "libglib2.0-0", "glib2"),
    ("libgthread-2.0.so.0", "libglib2.0-0", "glib2"),
    ("libnss3.so", "libnss3", "nss"),
    ("libsmime3.so", "libnss3", "nss"),
    ("libnssutil3.so", "libnss3", "nss-util"),
    ("libnspr4.so", "libnspr4", "nspr"),
    ("libbrotlicommon.so.1", "libbrotli1", "libbrotli"),
    ("libbrotlidec.so.1", "libbrotli1", "libbrotli"),
    ("libgssapi_krb5.so.2", "libgssapi-krb5-2", "krb5-libs"),
    ("libX11.so.6", "libx11-6", "libX11"),
    ("libX11-xcb.so.1", "libx11-xcb1", "libX11-xcb"),
    ("libXcomposite.so.1", "libxcomposite1", "libXcomposite"),
    ("libXdamage.so.1", "libxdamage1", "libXdamage"),
    ("libXext.so.6", "libxext6", "libXext"),
    ("libXfixes.so.3", "libxfixes3", "libXfixes"),
    ("libXrandr.so.2", "libxrandr2", "libXrandr"),
    ("libXtst.so.6", "libxtst6", "libXtst"),
    ("libxcb.so.1", "libxcb1", "libxcb"),
    ("libxcb-dri3.so.0", "libxcb-dri3-0", "libxcb"),
    ("libxcb-glx.so.0", "libxcb-glx0", "libxcb"),
    ("libxcb-randr.so.0", "libxcb-randr0", "libxcb"),
    ("libxcb-render.so.0", "libxcb-render0", "libxcb"),
    ("libxcb-shape.so.0", "libxcb-shape0", "libxcb"),
    ("libxcb-shm.so.0", "libxcb-shm0", "libxcb"),
    ("libxcb-sync.so.1", "libxcb-sync1", "libxcb"),
    ("libxcb-xfixes.so.0", "libxcb-xfixes0", "libxcb"),
    ("libxcb-xkb.so.1", "libxcb-xkb1", "libxcb"),
    ("libxkbcommon.so.0", "libxkbcommon0", "libxkbcommon"),
    (
        "libxkbcommon-x11.so.0",
        "libxkbcommon-x11-0",
        "libxkbcommon-x11",
    ),
    ("libxkbfile.so.1", "libxkbfile1", "libxkbfile"),
    (
        "libwayland-client.so.0",
        "libwayland-client0",
        "libwayland-client",
    ),
    (
        "libwayland-cursor.so.0",
        "libwayland-cursor0",
        "libwayland-cursor",
    ),
    ("libwayland-egl.so.1", "libwayland-egl1", "libwayland-egl"),
];

/// Debian packages renamed with a t64 suffix in the 64-bit time transition
/// (Ubuntu 24.04, Debian 13); the old name is then only a virtual package that
/// apt refuses to pick a provider for.
const T64_RENAMED: &[&str] = &["libasound2", "libglib2.0-0"];

/// apt or dnf, from the text of /etc/os-release: its ID or ID_LIKE.
fn package_manager(os_release: &str) -> Option<&'static str> {
    let mut names = Vec::new();
    for line in os_release.lines() {
        if let Some((key, value)) = line.split_once('=')
            && (key == "ID" || key == "ID_LIKE")
        {
            names.extend(
                value
                    .trim_matches(|c| c == '"' || c == '\'')
                    .split_whitespace()
                    .map(str::to_ascii_lowercase),
            );
        }
    }
    let any = |wanted: &[&str]| names.iter().any(|name| wanted.contains(&name.as_str()));
    if any(&["debian", "ubuntu"]) {
        Some("apt")
    } else if any(&["fedora", "rhel", "centos"]) {
        Some("dnf")
    } else {
        None
    }
}

/// The command that installs the packages providing the missing libraries,
/// on Debian, Ubuntu, Fedora and their derivatives: `manager`, `packages`,
/// `command`, and `unknown`, the libraries no package here is known for.
/// `apt_has` says whether apt knows a package by that name.
fn host_packages(
    missing: &[String],
    os_release: &str,
    apt_has: impl Fn(&str) -> bool,
) -> Option<Value> {
    let manager = package_manager(os_release)?;
    let mut packages: Vec<String> = Vec::new();
    let mut unknown = Vec::new();
    for library in missing {
        let Some(&(_, debian, fedora)) = HOST_PACKAGES.iter().find(|row| row.0 == library) else {
            unknown.push(library.clone());
            continue;
        };
        let package = if manager == "dnf" {
            fedora.to_string()
        } else if T64_RENAMED.contains(&debian) && apt_has(&format!("{debian}t64")) {
            format!("{debian}t64")
        } else {
            debian.to_string()
        };
        if !packages.contains(&package) {
            packages.push(package);
        }
    }
    let command = (!packages.is_empty()).then(|| {
        let install = if manager == "dnf" {
            "sudo dnf install"
        } else {
            "sudo apt-get install"
        };
        format!("{install} {}", packages.join(" "))
    });
    Some(json!({"manager":manager,"packages":packages,"command":command,"unknown":unknown}))
}

fn apt_knows(package: &str) -> bool {
    let Some(apt_cache) = toolchain::which("apt-cache") else {
        return false;
    };
    let args = [
        apt_cache.to_string_lossy().into_owned(),
        "show".to_string(),
        "--no-all-versions".to_string(),
        package.to_string(),
    ];
    toolchain::run(&args, 10, None, false)
        .is_ok_and(|result| result["exit_code"] == 0 && !s(&result, "stdout").trim().is_empty())
}

/// The bundled Qt of a portable package's desktop: runtime/ beside the
/// installed program, or lib/tomacco/runtime in the unpacked package.
fn portable_runtime(binary: &std::path::Path) -> Option<PathBuf> {
    let directory = binary.parent()?;
    [
        directory.join("runtime"),
        directory.join("../lib/tomacco/runtime"),
    ]
    .into_iter()
    .find(|runtime| runtime.join("lib").is_dir())
}

/// What else of a portable runtime loads system libraries the program itself
/// does not: the X11 and Wayland platform plugins and WebEngine's helper process.
const PORTABLE_LOADED: &[&str] = &[
    "plugins/platforms/libqxcb.so",
    "plugins/platforms/libqwayland.so",
    "libexec/QtWebEngineProcess",
];

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
    let runtime = portable_runtime(&binary);
    let portable = runtime.is_some();
    let requires = if portable {
        PORTABLE_REQUIREMENTS
    } else {
        RELEASE_REQUIREMENTS
    };
    let mut report = json!({"installed":true,"path":binary,"loadable":null,
        "portable":portable,"release_requires":requires});
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
            let mut text = format!("{}\n{}", s(&result, "stdout"), s(&result, "stderr"));
            let dynamic = !text.contains("not a dynamic executable");
            for part in PORTABLE_LOADED {
                let Some(path) = runtime.as_ref().map(|runtime| runtime.join(part)) else {
                    break;
                };
                if !path.is_file() {
                    continue;
                }
                let args = [
                    ldd.to_string_lossy().into_owned(),
                    path.to_string_lossy().into_owned(),
                ];
                if let Ok(more) = toolchain::run(&args, 10, None, false) {
                    text.push_str(&format!("\n{}\n{}", s(&more, "stdout"), s(&more, "stderr")));
                }
            }
            let (libraries, versions) = loader_problems(&text);
            report["loadable"] = json!(libraries.is_empty() && versions.is_empty());
            if result["exit_code"] != 0 && dynamic && libraries.is_empty() && versions.is_empty() {
                report["loadable"] = Value::Null;
                report["reason"] = json!(text.trim().chars().take(500).collect::<String>());
            }
            if !libraries.is_empty() {
                let os_release = std::fs::read_to_string("/etc/os-release")
                    .or_else(|_| std::fs::read_to_string("/usr/lib/os-release"))
                    .unwrap_or_default();
                if let Some(install) = host_packages(&libraries, &os_release, apt_knows) {
                    report["install"] = install;
                }
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

    /// ldd's report for the portable desktop on a Debian or Fedora system
    /// without its NSS, ALSA and xkbfile libraries.
    const PORTABLE_LDD: &str = "\tlinux-vdso.so.1 (0x00007ffd)\n\
\tlibQt6WebEngineQuick.so.6 => /home/me/.local/lib/tomacco/runtime/lib/libQt6WebEngineQuick.so.6 (0x7f)\n\
\tlibnss3.so => not found\n\
\tlibsmime3.so => not found\n\
\tlibnssutil3.so => not found\n\
\tlibnspr4.so => not found\n\
\tlibasound.so.2 => not found\n\
\tlibglib-2.0.so.0 => not found\n\
\tlibxkbfile.so.1 => not found\n\
\tlibmystery.so.7 => not found\n";

    #[test]
    fn missing_libraries_name_the_packages_to_install() {
        let (libraries, _) = loader_problems(PORTABLE_LDD);
        let bookworm =
            "PRETTY_NAME=\"Debian GNU/Linux 12 (bookworm)\"\nID=debian\nVERSION_ID=\"12\"\n";
        let fix = host_packages(&libraries, bookworm, |_| false).unwrap();
        assert_eq!(fix["manager"], "apt");
        assert_eq!(
            fix["command"],
            "sudo apt-get install libnss3 libnspr4 libasound2 libglib2.0-0 libxkbfile1"
        );
        assert_eq!(fix["unknown"], json!(["libmystery.so.7"]));
        // Ubuntu 24.04 renamed two of them; apt says so.
        let noble = "ID=ubuntu\nID_LIKE=debian\nVERSION_ID=\"24.04\"\n";
        let fix = host_packages(&libraries, noble, |name| name.ends_with("t64")).unwrap();
        assert_eq!(
            fix["command"],
            "sudo apt-get install libnss3 libnspr4 libasound2t64 libglib2.0-0t64 libxkbfile1"
        );
        // A derivative is known by its ID_LIKE.
        let mint = "NAME=\"Linux Mint\"\nID=linuxmint\nID_LIKE=\"ubuntu debian\"\n";
        assert_eq!(
            host_packages(&libraries, mint, |_| false).unwrap()["manager"],
            "apt"
        );
        let fedora = "NAME=\"Fedora Linux\"\nID=fedora\nVERSION_ID=44\n";
        let fix =
            host_packages(&libraries, fedora, |_| panic!("apt is not asked on Fedora")).unwrap();
        assert_eq!(
            fix["command"],
            "sudo dnf install nss nss-util nspr alsa-lib glib2 libxkbfile"
        );
        let alma = "ID=\"almalinux\"\nID_LIKE=\"rhel centos fedora\"\n";
        assert_eq!(
            host_packages(&libraries, alma, |_| false).unwrap()["manager"],
            "dnf"
        );
        // Elsewhere, or with nothing known to install, no command.
        assert!(host_packages(&libraries, "ID=arch\n", |_| false).is_none());
        let only_unknown =
            host_packages(&["libmystery.so.7".to_string()], bookworm, |_| false).unwrap();
        assert_eq!(only_unknown["command"], Value::Null);
    }

    #[test]
    fn every_library_the_portable_package_leaves_to_the_system_has_its_packages() {
        // The host libraries tools/build_portable.py allows, as the 1.1.0
        // portable package's PORTABLE.md lists them.
        let expected = "ld-linux-x86-64.so.2 libEGL.so.1 libGL.so.1 libGLX.so.0 libOpenGL.so.0 libX11-xcb.so.1 libX11.so.6 \
libXcomposite.so.1 libXdamage.so.1 libXext.so.6 libXfixes.so.3 libXrandr.so.2 libXtst.so.6 libasound.so.2 libbrotlicommon.so.1 \
libbrotlidec.so.1 libc.so.6 libdbus-1.so.3 libdl.so.2 libexpat.so.1 libfontconfig.so.1 libfreetype.so.6 libgbm.so.1 libgcc_s.so.1 \
libgio-2.0.so.0 libglib-2.0.so.0 libgobject-2.0.so.0 libgssapi_krb5.so.2 libgthread-2.0.so.0 libm.so.6 libnspr4.so libnss3.so \
libnssutil3.so libpthread.so.0 libresolv.so.2 libsmime3.so libstdc++.so.6 libudev.so.1 libwayland-client.so.0 \
libwayland-cursor.so.0 libwayland-egl.so.1 libxcb-dri3.so.0 libxcb-glx.so.0 libxcb-randr.so.0 libxcb-render.so.0 \
libxcb-shape.so.0 libxcb-shm.so.0 libxcb-sync.so.1 libxcb-xfixes.so.0 libxcb-xkb.so.1 libxcb.so.1 libxkbcommon-x11.so.0 \
libxkbcommon.so.0 libxkbfile.so.1 libz.so.1 libzstd.so.1";
        let libraries: Vec<String> = expected.split_whitespace().map(String::from).collect();
        let fix = host_packages(&libraries, "ID=debian\n", |_| false).unwrap();
        assert_eq!(fix["unknown"], json!([]));
        let fix = host_packages(&libraries, "ID=fedora\n", |_| false).unwrap();
        assert_eq!(fix["unknown"], json!([]));
    }

    #[test]
    fn a_portable_desktop_is_known_by_its_runtime() {
        let root = std::env::temp_dir().join(format!("tomacco-portable-{}", std::process::id()));
        let installed = root.join("home/.local/lib/tomacco");
        std::fs::create_dir_all(installed.join("runtime/lib")).unwrap();
        let package = root.join("package");
        std::fs::create_dir_all(package.join("bin")).unwrap();
        std::fs::create_dir_all(package.join("lib/tomacco/runtime/lib")).unwrap();
        let regular = root.join("regular/bin");
        std::fs::create_dir_all(&regular).unwrap();
        assert_eq!(
            portable_runtime(&installed.join("tomacco")),
            Some(installed.join("runtime"))
        );
        assert!(portable_runtime(&package.join("bin/tomacco")).is_some());
        assert_eq!(portable_runtime(&regular.join("tomacco")), None);
        std::fs::remove_dir_all(root).unwrap();
    }
}
