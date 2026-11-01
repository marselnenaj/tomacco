//! `tomaccoctl omarchy`: how Tomacco stands on an Omarchy desktop, and the few
//! things the desktop's own commands do not cover.
//!
//! - `status` lists the integrations (panel, window rules, keys, menu entries,
//!   agent skill) and which of them are outdated or missing; `repair`
//!   installs those again from the copies the installation kept.
//! - `screenshot` saves a picture of the connected device where Omarchy keeps
//!   screenshots, copies it to the clipboard and says so in a notification.
//! - `notify on|off` turns on notifications for jobs that end while no
//!   desktop is open, which `jobs::worker` sends through [`job_ended`].
use crate::{core::*, install};
use serde_json::{Value, json};
use std::{
    io::Write,
    path::{Path, PathBuf},
};

/// A job shorter than this ends without a notification: the person who
/// started it is still looking at it.
pub const NOTIFY_AFTER_SECONDS: f64 = 15.0;

pub fn command(args: &[String]) -> Result<i32> {
    let json_output = args.iter().any(|arg| arg == "--json");
    let words: Vec<&str> = args
        .iter()
        .map(String::as_str)
        .filter(|arg| *arg != "--json")
        .collect();
    match words.as_slice() {
        [] | ["status"] => status(json_output),
        ["repair"] => repair(json_output),
        ["screenshot"] => screenshot(true),
        ["screenshot", "--quiet"] => screenshot(false),
        ["notify"] => {
            let on = notifying()?;
            println!(
                "Job notifications from the command line: {}",
                if on { "on" } else { "off" }
            );
            Ok(0)
        }
        ["notify", setting @ ("on" | "off")] => {
            set_notifying(*setting == "on")?;
            println!("Job notifications from the command line: {setting}");
            Ok(0)
        }
        _ => Err(Error::new(
            "invalid_request",
            "Usage: tomaccoctl omarchy [status|repair|screenshot [--quiet]|notify [on|off]] [--json]",
        )
        .remedy("Run tomaccoctl --help for the commands")),
    }
}

fn status(json_output: bool) -> Result<i32> {
    let mut report = install::omarchy_status(&user_home());
    report["notifications"] = notification_state()?;
    let healthy = report["healthy"] == true;
    if json_output {
        println!("{}", report);
        return Ok(if healthy { 0 } else { 1 });
    }
    if report["omarchy"] != true {
        println!("Omarchy was not found on this computer.");
    }
    for item in report["components"].as_array().into_iter().flatten() {
        println!(
            "{:<10}{:<14}{}",
            item["name"].as_str().unwrap_or(""),
            item["state"].as_str().unwrap_or(""),
            item["detail"].as_str().unwrap_or("")
        );
    }
    let notifications = &report["notifications"];
    println!(
        "{:<10}{:<14}{}",
        "notify",
        if notifications["jobs"] == true {
            "on"
        } else {
            "off"
        },
        notifications["detail"].as_str().unwrap_or("")
    );
    if !healthy {
        if report["repairable"] == true {
            println!("\nRepair what is outdated or missing: tomaccoctl omarchy repair");
        } else {
            println!(
                "\nSomething is outdated or missing, and this installation kept no copy to repair it from. \
                 Run the installer of the release again."
            );
        }
    }
    Ok(if healthy { 0 } else { 1 })
}

fn repair(json_output: bool) -> Result<i32> {
    let result = install::omarchy_repair(&user_home(), &crate::update::current_version())?;
    if json_output {
        println!("{result}");
        return Ok(0);
    }
    let done: Vec<&str> = result["repaired"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .collect();
    if done.is_empty() {
        println!("Nothing to repair.");
    } else {
        println!("Installed again: {}.", done.join(", "));
        if let Some(backups) = result["backups"].as_str() {
            println!("The previous files are kept in {backups}.");
        }
    }
    Ok(0)
}

/// Where Omarchy puts screenshots: `OMARCHY_SCREENSHOT_DIR`, else the
/// Pictures folder of `~/.config/user-dirs.dirs`, else `~/Pictures`.
pub fn screenshot_directory() -> PathBuf {
    screenshot_directory_in(
        std::env::var("OMARCHY_SCREENSHOT_DIR").ok().as_deref(),
        &user_home(),
    )
}

fn screenshot_directory_in(configured: Option<&str>, home: &Path) -> PathBuf {
    if let Some(directory) = configured.filter(|directory| !directory.trim().is_empty()) {
        let directory = directory.replace("$HOME", &home.to_string_lossy());
        let directory = directory
            .strip_prefix("~/")
            .map_or(directory.clone(), |rest| {
                format!("{}/{rest}", home.to_string_lossy())
            });
        return PathBuf::from(directory);
    }
    let dirs = std::fs::read_to_string(home.join(".config/user-dirs.dirs")).unwrap_or_default();
    for line in dirs.lines() {
        if let Some(value) = line.trim().strip_prefix("XDG_PICTURES_DIR=") {
            let value = value
                .trim()
                .trim_matches('"')
                .replace("$HOME", &home.to_string_lossy());
            if value.starts_with('/') {
                return PathBuf::from(value);
            }
        }
    }
    home.join("Pictures")
}

/// The local time as `2026-11-01-153000`.
fn stamp() -> String {
    let seconds = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as libc::time_t;
    // SAFETY: tm is plain data, filled by localtime_r from a valid time_t.
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    unsafe { libc::localtime_r(&seconds, &mut tm) };
    format!(
        "{:04}-{:02}-{:02}-{:02}{:02}{:02}",
        tm.tm_year + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec
    )
}

fn screenshot(announce: bool) -> Result<i32> {
    crate::guard::enter(crate::guard::Channel::Cli, None);
    let runtimes = crate::api::dispatch("runtime.list", &json!({}))?;
    let live = runtimes
        .as_array()
        .into_iter()
        .flatten()
        .find(|runtime| s(runtime, "status") == "connected")
        .ok_or_else(|| {
            Error::new(
                "no_live_device",
                "No device is connected, so there is no screen to capture",
            )
            .remedy("Connect a device or start an emulator, then try again")
        })?;
    let directory = screenshot_directory();
    std::fs::create_dir_all(&directory)?;
    let name: String = s(live, "device")
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                c
            } else {
                '-'
            }
        })
        .collect();
    let target = directory.join(format!("{name}-{}.png", stamp()));
    crate::api::dispatch(
        "runtime.capture",
        &json!({"session": s(live, "session"), "path": target}),
    )?;
    let copied = copy_to_clipboard(&target);
    if announce {
        notify(
            "Device screenshot",
            &format!(
                "{}{}",
                target.display(),
                if copied { " (copied)" } else { "" }
            ),
            Some(&target),
            false,
        );
    }
    println!("{}", json!({"path": target, "copied": copied}));
    Ok(0)
}

/// Copies a PNG to the Wayland clipboard; false where wl-copy is missing or fails.
fn copy_to_clipboard(path: &Path) -> bool {
    let Ok(Some(tool)) = crate::toolchain::find_optional("wl-copy") else {
        return false;
    };
    let Ok(file) = std::fs::File::open(path) else {
        return false;
    };
    std::process::Command::new(tool)
        .args(["--type", "image/png"])
        .stdin(file)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .is_ok_and(|status| status.success())
}

/// Sends a notification through notify-send, which talks to the desktop's
/// notification service (mako, or the Omarchy shell's) over D-Bus. Nothing
/// happens where notify-send is not installed.
fn notify(title: &str, body: &str, icon: Option<&Path>, urgent: bool) {
    let Ok(Some(tool)) = crate::toolchain::find_optional("notify-send") else {
        return;
    };
    let mut command = std::process::Command::new(tool);
    command.arg("--app-name=Tomacco");
    command.arg(format!(
        "--icon={}",
        icon.map_or("io.github.marselnenaj.tomacco".to_string(), |p| p
            .to_string_lossy()
            .into_owned())
    ));
    command.arg(if urgent {
        "--urgency=critical"
    } else {
        "--urgency=normal"
    });
    let _ = command
        .args([title, body])
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status();
}

fn notifying() -> Result<bool> {
    Ok(crate::toolchain::settings()?["notifications"]["jobs"] == true)
}

fn set_notifying(on: bool) -> Result<()> {
    let lock = Lock::open(&home()?.join("settings.lock"))?;
    lock.acquire(false)?;
    let mut config = crate::toolchain::settings()?;
    if !config["notifications"].is_object() {
        config["notifications"] = json!({});
    }
    config["notifications"]["jobs"] = json!(on);
    atomic_json(&home()?.join("settings.json"), &config)
}

/// The notification setting and whether it can work, for `status`.
fn notification_state() -> Result {
    let on = notifying()?;
    let sender = crate::toolchain::find_optional("notify-send")?.is_some();
    let panel = panel_notifies();
    let detail = match (on, sender, panel) {
        (true, false, _) => "notify-send is not installed, so nothing is sent".to_string(),
        (true, true, true) => {
            "The Omarchy panel notifies about builds too; turn one of them off to avoid two notices"
                .to_string()
        }
        (true, true, false) => format!(
            "Notices for jobs of {} s or more that end while no desktop is open",
            NOTIFY_AFTER_SECONDS as u32
        ),
        (false, _, _) => "tomaccoctl omarchy notify on turns it on".to_string(),
    };
    Ok(json!({"jobs": on, "notify_send": sender, "panel": panel, "detail": detail}))
}

/// Whether the panel is placed on the bar with its build notices on.
fn panel_notifies() -> bool {
    let Ok(text) = std::fs::read_to_string(user_home().join(".config/omarchy/shell.json")) else {
        return false;
    };
    let Ok(value) = serde_json::from_str::<Value>(&text) else {
        return false;
    };
    value["bar"]["layout"]
        .as_object()
        .into_iter()
        .flat_map(|layout| layout.values())
        .filter_map(Value::as_array)
        .flatten()
        .any(|item| {
            item["id"] == install::APP_ID && item["notifyBuilds"] != false
                || item.as_str() == Some(install::APP_ID)
        })
}

/// Whether a job that ended warrants a notification from the command line:
/// the setting is on, the job ran long and did not end by being cancelled,
/// and no desktop is open to say so itself.
pub fn wants_notice(settings: &Value, state: &str, seconds: f64, desktop_open: bool) -> bool {
    settings["notifications"]["jobs"] == true
        && !desktop_open
        && seconds >= NOTIFY_AFTER_SECONDS
        && ["succeeded", "failed", "timed_out"].contains(&state)
}

/// Called by a job's worker when the job has ended.
pub fn job_ended(label: &str, state: &str, seconds: f64) {
    let Ok(settings) = crate::toolchain::settings() else {
        return;
    };
    if settings["notifications"]["jobs"] != true {
        return;
    }
    let desktop_open = crate::preview::sessions()
        .ok()
        .and_then(|sessions| sessions.as_array().map(|list| !list.is_empty()))
        .unwrap_or(false);
    if !wants_notice(&settings, state, seconds, desktop_open) {
        return;
    }
    let verdict = match state {
        "succeeded" => "Succeeded",
        "timed_out" => "Timed out",
        _ => "Failed",
    };
    notify(label, verdict, None, state != "succeeded");
    let _ = std::io::stdout().flush();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn screenshots_go_where_omarchy_puts_them() {
        let home = Path::new("/home/someone");
        assert_eq!(
            screenshot_directory_in(Some("$HOME/Pictures/Screenshots"), home),
            Path::new("/home/someone/Pictures/Screenshots")
        );
        assert_eq!(
            screenshot_directory_in(Some("~/Shots"), home),
            Path::new("/home/someone/Shots")
        );
        assert_eq!(
            screenshot_directory_in(Some("/mnt/shots"), home),
            Path::new("/mnt/shots")
        );
        assert_eq!(
            screenshot_directory_in(Some("  "), home),
            Path::new("/home/someone/Pictures")
        );
        assert_eq!(
            screenshot_directory_in(None, home),
            Path::new("/home/someone/Pictures")
        );
    }

    #[test]
    fn the_pictures_folder_comes_from_the_user_dirs_file() {
        let directory = tempfile::tempdir().unwrap();
        let home = directory.path();
        std::fs::create_dir_all(home.join(".config")).unwrap();
        std::fs::write(
            home.join(".config/user-dirs.dirs"),
            "XDG_DESKTOP_DIR=\"$HOME/Desktop\"\nXDG_PICTURES_DIR=\"$HOME/Bilder\"\n",
        )
        .unwrap();
        assert_eq!(screenshot_directory_in(None, home), home.join("Bilder"));
        assert_eq!(
            screenshot_directory_in(Some("/tmp/x"), home),
            Path::new("/tmp/x")
        );
    }

    #[test]
    fn only_long_jobs_that_end_for_real_notify_when_no_desktop_is_open() {
        let on = json!({"notifications": {"jobs": true}});
        let off = json!({});
        assert!(wants_notice(&on, "succeeded", 60.0, false));
        assert!(wants_notice(&on, "failed", 16.0, false));
        assert!(wants_notice(&on, "timed_out", 900.0, false));
        assert!(!wants_notice(&on, "succeeded", 3.0, false));
        assert!(!wants_notice(&on, "cancelled", 60.0, false));
        assert!(!wants_notice(&on, "succeeded", 60.0, true));
        assert!(!wants_notice(&off, "failed", 60.0, false));
        assert!(!wants_notice(
            &json!({"notifications": {"jobs": false}}),
            "failed",
            60.0,
            false
        ));
    }
}
