//! android.store_screenshots: store screenshots of a running Android app
//! across devices, app languages and light and dark mode, each screen reached
//! by its own short scenario, in the folders fastlane supply uploads.
use crate::{api, core::*, jobs, toolchain as tc};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

const LANGUAGE: &str = r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}";

/// fastlane supply's folder for a screen of this smallest width in dp:
/// phones, 7-inch and 10-inch tablets, as Play groups them.
fn kind(width: u64, height: u64, density: u64) -> &'static str {
    let smallest = width.min(height) * 160 / density.max(1);
    if smallest >= 720 {
        "tenInchScreenshots"
    } else if smallest >= 600 {
        "sevenInchScreenshots"
    } else {
        "phoneScreenshots"
    }
}

/// Google Play's rules for a screenshot: each side 320 to 3840 pixels, and
/// the long side at most twice the short one.
fn play_problems(width: u32, height: u32) -> Vec<String> {
    let mut problems = Vec::new();
    let (short, long) = (width.min(height), width.max(height));
    if short < 320 {
        problems.push(format!("{short} px is under Play's 320 px minimum"));
    }
    if long > 3840 {
        problems.push(format!("{long} px is over Play's 3840 px maximum"));
    }
    if short > 0 && long > short * 2 {
        problems.push(format!("{width}x{height} is longer than Play's 2:1 limit"));
    }
    problems
}

fn png_size(path: &Path) -> Option<(u32, u32)> {
    let bytes = std::fs::read(path).ok()?;
    (bytes.len() >= 24 && bytes.starts_with(b"\x89PNG\r\n\x1a\n")).then(|| {
        (
            u32::from_be_bytes(bytes[16..20].try_into().unwrap()),
            u32::from_be_bytes(bytes[20..24].try_into().unwrap()),
        )
    })
}

fn shot_path(
    directory: &Path,
    theme: &str,
    locale: &str,
    kind: &str,
    index: usize,
    name: &str,
) -> PathBuf {
    directory
        .join(theme)
        .join(locale)
        .join("images")
        .join(kind)
        .join(format!("{:02}_{name}.png", index + 1))
}

/// The display's size and density in effect, overrides first.
fn display(device: &str) -> Result<(u64, u64, u64)> {
    let value = api::dispatch("android.display", &json!({"device":device}))?;
    let pick = |key: &str| {
        let v = &value[key];
        if v["override"].is_null() {
            v["physical"].clone()
        } else {
            v["override"].clone()
        }
    };
    let size = pick("size");
    let density = pick("density").as_u64().unwrap_or(0);
    match (size[0].as_u64(), size[1].as_u64()) {
        (Some(w), Some(h)) if density > 0 => Ok((w, h, density)),
        _ => Err(Error::new(
            "display_unknown",
            format!("{device} reported no screen size and density"),
        )
        .details(value)),
    }
}

pub fn start(a: &Value) -> Result {
    let bundle = identifier(
        s(a, "bundle_id"),
        "application ID",
        Some(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+"),
    )?;
    let devices = strings(&a["devices"]);
    let locales = strings(&a["locales"]);
    let mut themes = strings(&a["themes"]);
    if themes.is_empty() {
        themes.push("light".into());
    }
    for (list, label) in [
        (&devices, "devices"),
        (&locales, "locales"),
        (&themes, "themes"),
    ] {
        let unique: std::collections::HashSet<_> = list.iter().map(|v| v.to_lowercase()).collect();
        if unique.len() != list.len() {
            return Err(Error::new(
                "invalid_argument",
                format!("{label} must be unique"),
            ));
        }
    }
    for locale in &locales {
        identifier(locale, "language tag", Some(LANGUAGE))?;
    }
    let screens = a["screens"].as_array().cloned().unwrap_or_default();
    let mut names = std::collections::HashSet::new();
    for screen in &screens {
        let name = s(screen, "name");
        if !names.insert(name.to_string()) {
            return Err(Error::new(
                "invalid_argument",
                format!("Screen {name} is given twice"),
            ));
        }
        if screen["steps"].as_array().is_some_and(|s| !s.is_empty()) {
            crate::automation::check_scenario(&json!({"name":name,"steps":screen["steps"]}))
                .map_err(|e| Error::new(&e.code, format!("Screen {name}: {}", e.message)))?;
        }
    }
    let output = path(s(a, "directory"), false, false)?;
    if output.exists() && std::fs::read_dir(&output)?.next().is_some() {
        return Err(Error::new(
            "path_exists",
            "Choose an empty or new directory for the screenshots",
        ));
    }
    // Each device's folder follows its screen; two devices of one kind would
    // write the same files.
    let mut targets = Vec::new();
    for device in &devices {
        identifier(device, "Android serial", None)?;
        let (width, height, density) = display(device)?;
        let kind = kind(width, height, density);
        if let Some(other) = targets.iter().find(|t: &&Value| s(t, "kind") == kind) {
            return Err(Error::new(
                "invalid_argument",
                format!(
                    "{device} and {} are both {kind}; use one device per screen size",
                    s(other, "device")
                ),
            ));
        }
        targets.push(
            json!({"device":device,"kind":kind,"width":width,"height":height,"density":density}),
        );
    }
    std::fs::create_dir_all(&output)?;
    let directory = home()?
        .join("store-screenshots")
        .join(uuid::Uuid::new_v4().simple().to_string());
    private_dir(&directory)?;
    let request = directory.join("request.json");
    atomic_json(
        &request,
        &json!({"bundle_id":bundle,"devices":targets,"locales":locales,"themes":themes,"screens":screens,
                "directory":output,"evidence":directory,"settle_ms":a["settle_ms"].as_u64().unwrap_or(1000),"report":output.join("report.json")}),
    )?;
    let count = devices.len() * locales.len() * themes.len() * screens.len();
    jobs::submit(
        &format!("Store screenshots · {count}"),
        json!({"steps":[{"argv":[own_executable()?,"_store_screenshots",request],"artifacts":[output.join("report.json")]}],"cwd":directory,"env":tc::environment()?,"timeout":a["timeout"].as_u64().unwrap_or(3600),"always_artifacts":["report.json"]}),
    )
}

/// One device's captures: every language, theme and screen in turn, then
/// its settings as they were.
fn capture_device(spec: &Value, target: &Value) -> Value {
    let device = s(target, "device");
    let bundle = s(spec, "bundle_id");
    let kind = s(target, "kind");
    let directory = PathBuf::from(s(spec, "directory"));
    let settle = std::time::Duration::from_millis(spec["settle_ms"].as_u64().unwrap_or(1000));
    let mut shots = Vec::new();
    let before = api::dispatch(
        "android.settings",
        &json!({"device":device,"bundle_id":bundle}),
    );
    let result = (|| -> Result<()> {
        if let Err(error) = &before {
            return Err(Error::new(&error.code, error.message.clone()));
        }
        api::dispatch(
            "android.settings",
            &json!({"device":device,"animations":false}),
        )?;
        for locale in strings(&spec["locales"]) {
            for theme in strings(&spec["themes"]) {
                let mode = if theme == "dark" { "on" } else { "off" };
                api::dispatch(
                    "android.settings",
                    &json!({"device":device,"bundle_id":bundle,"app_locale":locale,"dark_mode":mode}),
                )?;
                for (index, screen) in spec["screens"].as_array().into_iter().flatten().enumerate()
                {
                    check_cancelled()?;
                    let name = s(screen, "name");
                    let path = shot_path(&directory, &theme, &locale, kind, index, name);
                    let mut steps = vec![
                        json!({"action":"stop","bundle_id":bundle}),
                        json!({"action":"launch","bundle_id":bundle}),
                    ];
                    steps.extend(screen["steps"].as_array().cloned().unwrap_or_default());
                    let scenario = json!({"name":name,"steps":steps});
                    let run = crate::automation::run_device(
                        device,
                        &scenario,
                        Path::new(s(spec, "evidence")),
                        &format!("{device}-{locale}-{theme}-{name}"),
                        "android",
                    );
                    let mut shot = json!({"locale":locale,"theme":theme,"screen":name,"path":path});
                    if run["passed"] != true {
                        shot["error"] = run["error"].clone();
                        shot["steps"] = run["steps"].clone();
                        shots.push(shot);
                        continue;
                    }
                    std::thread::sleep(settle);
                    match crate::runtime::android::screencap(device, &path) {
                        Ok(()) => {
                            let (width, height) = png_size(&path).unwrap_or((0, 0));
                            shot["width"] = json!(width);
                            shot["height"] = json!(height);
                            shot["play_problems"] = json!(play_problems(width, height));
                        }
                        Err(error) => shot["error"] = error.value(),
                    }
                    shots.push(shot);
                }
            }
        }
        Ok(())
    })();
    // Put back what the captures changed.
    let mut restored = Value::Null;
    if let Ok(before) = &before {
        let mut back = json!({"device":device,"bundle_id":bundle,"animations":before["animations"],"app_locale":before["app_locale"].as_str().unwrap_or("")});
        if ["on", "off", "auto"].contains(&s(before, "dark_mode")) {
            back["dark_mode"] = before["dark_mode"].clone();
        }
        restored = match api::dispatch("android.settings", &back) {
            Ok(_) => json!(true),
            Err(error) => error.value(),
        };
    }
    let failed = shots.iter().filter(|s| s.get("error").is_some()).count();
    json!({"device":device,"kind":kind,"ok":result.is_ok() && failed == 0,"error":result.err().map(|e| e.value()),"captured":shots.len()-failed,"failed":failed,"screenshots":shots,"settings_restored":restored})
}

pub fn worker(request: &str) -> Result<()> {
    let spec = read_json(Path::new(request), None)?;
    let targets = spec["devices"].as_array().cloned().unwrap_or_default();
    let results: Vec<Value> = std::thread::scope(|scope| {
        let handles: Vec<_> = targets
            .iter()
            .map(|t| (t, scope.spawn(|| capture_device(&spec, t))))
            .collect();
        handles
            .into_iter()
            .map(|(t, h)| h.join().unwrap_or_else(|_| json!({"device":t["device"],"ok":false,"error":"The device's worker stopped"})))
            .collect()
    });
    let failed: Vec<String> = results
        .iter()
        .filter(|r| r["ok"] != true)
        .map(|r| s(r, "device").to_string())
        .collect();
    let problems: usize = results
        .iter()
        .flat_map(|r| r["screenshots"].as_array().cloned().unwrap_or_default())
        .filter(|s| s["play_problems"].as_array().is_some_and(|p| !p.is_empty()))
        .count();
    let report = json!({"ok":failed.is_empty(),"directory":spec["directory"],"layout":"THEME/LOCALE/images/KIND/NN_SCREEN.png","devices":results,"failed":failed,"play_problems":problems});
    atomic_json(Path::new(s(&spec, "report")), &report)?;
    println!("{}", serde_json::to_string_pretty(&report)?);
    if !failed.is_empty() {
        return Err(Error::new(
            "capture_failed",
            format!("Screenshots failed on {}", failed.join(", ")),
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn devices_are_grouped_by_smallest_width() {
        assert_eq!(kind(1080, 2400, 420), "phoneScreenshots");
        assert_eq!(kind(1200, 1920, 320), "sevenInchScreenshots");
        assert_eq!(kind(2560, 1600, 320), "tenInchScreenshots");
    }

    #[test]
    fn play_size_rules_are_checked() {
        assert!(play_problems(1080, 2160).is_empty());
        assert_eq!(play_problems(1080, 2400).len(), 1);
        assert_eq!(play_problems(200, 300).len(), 1);
        assert_eq!(play_problems(2000, 4000).len(), 1);
    }

    #[test]
    fn files_go_where_fastlane_supply_reads_them() {
        assert_eq!(
            shot_path(
                Path::new("/s"),
                "dark",
                "de-DE",
                "phoneScreenshots",
                0,
                "home"
            ),
            Path::new("/s/dark/de-DE/images/phoneScreenshots/01_home.png")
        );
    }
}
