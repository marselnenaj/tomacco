//! Screenshot baselines of `assert_screen` and `wait_screen` steps, kept in
//! a project folder: review lists each screen step of a testing.run report
//! as new, changed, passed or not reached, with its screenshot and
//! difference image; approve copies the chosen screenshots over their
//! baselines; list shows the baselines a folder holds.
use crate::core::*;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};

const MAX_LISTED: usize = 1000;

/// A screen's identifier in review and approve.
fn screen_id(scenario: u64, step: u64, device: &str) -> String {
    format!("s{scenario:03}-{step:03}@{device}")
}

/// Every screen step of a testing.run report directory, with what happened
/// to it on each device.
fn screens(run: &Path) -> Result<Vec<Value>> {
    if !run.join("report.json").is_file() || !run.join("run.json").is_file() {
        return Err(Error::new(
            "not_a_run_directory",
            format!("{} is not a testing.run report directory", run.display()),
        )
        .remedy("Give run, the directory a testing.run wrote report.json and run.json to"));
    }
    let spec = read_json(&run.join("run.json"), None)?;
    let report = read_json(&run.join("report.json"), None)?;
    let mut found = Vec::new();
    for result in report["results"].as_array().into_iter().flatten() {
        let index = n(result, "scenario_index");
        let scenario = &spec["scenarios"][index as usize];
        let device = s(result, "device");
        let steps: BTreeMap<u64, &Value> = result["steps"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|step| (n(step, "index"), step))
            .collect();
        for (number, planned) in scenario["steps"]
            .as_array()
            .into_iter()
            .flatten()
            .enumerate()
        {
            if !["assert_screen", "wait_screen"].contains(&s(planned, "action")) {
                continue;
            }
            let number = number as u64;
            let baseline = s(planned, "baseline");
            let mut screen = json!({
                "id":screen_id(index, number, device),"scenario":scenario["name"],"device":device,
                "step":number,"label":planned["label"],"baseline":baseline,
                "baseline_exists":Path::new(baseline).is_file(),
            });
            let Some(step) = steps.get(&number) else {
                screen["status"] = json!("not_reached");
                found.push(screen);
                continue;
            };
            let outcome = &step["result"];
            let error = &outcome["error"];
            let actual = step["images"]["screenshot"]
                .as_str()
                .or(outcome["actual"].as_str())
                .or(error["details"]["actual"].as_str())
                .unwrap_or("");
            let status = if b(outcome, "new_baseline") || s(error, "code") == "baseline_missing" {
                "new"
            } else if b(step, "passed") {
                "passed"
            } else if b(&error["details"], "size_mismatch") {
                "size_changed"
            } else if ["screen_mismatch", "screen_timeout"].contains(&s(error, "code")) {
                "changed"
            } else {
                "failed"
            };
            screen["status"] = json!(status);
            screen["actual"] = json!(actual);
            screen["diff"] = step["images"]["diff"].clone();
            screen["changed_fraction"] = outcome["changed_fraction"]
                .as_f64()
                .or(error["details"]["changed_fraction"].as_f64())
                .map_or(Value::Null, |v| json!(v));
            if status == "failed" {
                screen["error"] = error.clone();
            }
            found.push(screen);
        }
    }
    Ok(found)
}

fn review(a: &Value) -> Result {
    let run = path(s(a, "run"), true, true)?;
    let screens = screens(&run)?;
    let mut counts = BTreeMap::new();
    for screen in &screens {
        *counts
            .entry(s(screen, "status").to_string())
            .or_insert(0u64) += 1;
    }
    let to_review: Vec<&Value> = screens
        .iter()
        .filter(|s| ["new", "changed", "size_changed"].contains(&crate::core::s(s, "status")))
        .collect();
    let mut value =
        json!({"run":run,"counts":counts,"to_review":to_review.len(),"screens":screens});
    if !b(a, "all_screens") {
        value["screens"] = json!(to_review);
    }
    Ok(value)
}

fn approve(a: &Value) -> Result {
    let run = path(s(a, "run"), true, true)?;
    let screens = screens(&run)?;
    let ids = strings(&a["screens"]);
    let all = b(a, "all");
    if ids.is_empty() && !all {
        return Err(Error::new(
            "invalid_argument",
            "Name the screens to approve, or pass all:true for every new and changed screen",
        )
        .remedy("testing.baselines review lists each screen's id"));
    }
    let approvable =
        |screen: &Value| ["new", "changed", "size_changed"].contains(&s(screen, "status"));
    let mut chosen = Vec::new();
    for id in &ids {
        let screen = screens
            .iter()
            .find(|screen| s(screen, "id") == id)
            .ok_or_else(|| {
                Error::new("screen_not_found", format!("No screen {id} in this run"))
                    .remedy("Use an id from testing.baselines review")
            })?;
        if s(screen, "actual").is_empty() || !Path::new(s(screen, "actual")).is_file() {
            return Err(Error::new(
                "screenshot_missing",
                format!(
                    "Screen {id} has no screenshot to approve (status {})",
                    s(screen, "status")
                ),
            ));
        }
        chosen.push(screen);
    }
    if all {
        for screen in screens.iter().filter(|s| approvable(s)) {
            if !chosen.iter().any(|c| c["id"] == screen["id"]) {
                chosen.push(screen);
            }
        }
    }
    // One baseline, one image: devices that disagree are refused before
    // anything is written.
    let mut by_baseline: BTreeMap<&str, (String, &Value)> = BTreeMap::new();
    for screen in &chosen {
        let actual = PathBuf::from(s(screen, "actual"));
        let hash = digest(&fs::read(&actual)?);
        if let Some((kept, other)) = by_baseline.get(s(screen, "baseline")) {
            if *kept != hash {
                return Err(Error::new(
                    "baseline_conflict",
                    format!(
                        "{} and {} would write different images to {}",
                        s(other, "id"),
                        s(screen, "id"),
                        s(screen, "baseline")
                    ),
                )
                .remedy("Approve one of them by its id"));
            }
        }
        by_baseline.insert(s(screen, "baseline"), (hash, screen));
    }
    let mut approved = Vec::new();
    for (baseline, (_, screen)) in by_baseline {
        let target = output_path(baseline)?;
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent)?;
        }
        atomic_write(&target, &fs::read(s(screen, "actual"))?)?;
        approved.push(json!({"id":screen["id"],"status":screen["status"],"baseline":artifact(&target, now())?}));
    }
    Ok(json!({"run":run,"approved":approved}))
}

/// Width and height from a PNG's header.
fn png_size(file: &Path) -> Option<(u32, u32)> {
    use std::io::Read;
    let mut header = [0u8; 24];
    fs::File::open(file).ok()?.read_exact(&mut header).ok()?;
    if &header[..8] != b"\x89PNG\r\n\x1a\n" || &header[12..16] != b"IHDR" {
        return None;
    }
    Some((
        u32::from_be_bytes(header[16..20].try_into().ok()?),
        u32::from_be_bytes(header[20..24].try_into().ok()?),
    ))
}

fn list(a: &Value) -> Result {
    let directory = path(s(a, "directory"), true, true)?;
    let pattern = format!(
        "{}/**/*.png",
        glob::Pattern::escape(&directory.to_string_lossy())
    );
    let files = glob_paths(&pattern)?;
    let total = files.len();
    let baselines: Vec<Value> = files
        .into_iter()
        .take(MAX_LISTED)
        .map(|file| {
            let size = png_size(&file);
            json!({
                "name":file.strip_prefix(&directory).unwrap_or(&file),"path":file,
                "bytes":fs::metadata(&file).map(|m| m.len()).unwrap_or(0),
                "width":size.map(|s| s.0),"height":size.map(|s| s.1),
                "modified":crate::test_results::modified(&file),
            })
        })
        .collect();
    Ok(json!({"directory":directory,"total":total,"baselines":baselines}))
}

pub fn baselines(a: &Value) -> Result {
    match s(a, "action") {
        "approve" => approve(a),
        "list" => list(a),
        _ => review(a),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn png(color: u8) -> Vec<u8> {
        crate::visual::encode(&crate::visual::Image {
            width: 2,
            height: 1,
            pixels: vec![color, 0, 0, 255, color, 0, 0, 255],
        })
        .unwrap()
    }

    /// A run of one scenario with four screen steps on two devices: the
    /// first saw a new screen, a changed one and a passed one, and did not
    /// reach the fourth; the second stopped at the new screen.
    fn fixture() -> (tempfile::TempDir, PathBuf, PathBuf) {
        let root = tempfile::tempdir().unwrap();
        let run = root.path().join("run");
        let base = root.path().join("baselines");
        fs::create_dir_all(&run).unwrap();
        fs::create_dir_all(base.join("login")).unwrap();
        fs::write(base.join("login/home.png"), png(1)).unwrap();
        fs::write(base.join("login/done.png"), png(3)).unwrap();
        for (name, color) in [
            ("a-000.png", 9),
            ("a-001.png", 2),
            ("a-002.png", 3),
            ("b-000.png", 8),
        ] {
            fs::write(run.join(name), png(color)).unwrap();
        }
        let step =
            |action: &str, baseline: &str| json!({"action":action,"baseline":base.join(baseline)});
        atomic_json(&run.join("run.json"), &json!({"scenarios":[{"name":"Login","steps":[
            step("assert_screen","login/new.png"),step("assert_screen","login/home.png"),
            step("wait_screen","login/done.png"),{"action":"tap","x":1,"y":1},step("assert_screen","login/later.png")]}]})).unwrap();
        atomic_json(&run.join("report.json"), &json!({"complete":true,"results":[
            {"scenario":"Login","scenario_index":0,"device":"emulator-5554","passed":false,"steps":[
                {"index":0,"passed":true,"result":{"passed":true,"new_baseline":true,"baseline":base.join("login/new.png"),"actual":run.join("a-000.png")},
                 "images":{"screenshot":run.join("a-000.png")}},
                {"index":1,"passed":false,"result":{"error":{"code":"screen_mismatch","details":{"changed_fraction":0.5}}},
                 "images":{"screenshot":run.join("a-001.png"),"baseline":base.join("login/home.png")}},
                {"index":2,"passed":true,"result":{"passed":true,"changed_fraction":0.0},"images":{"screenshot":run.join("a-002.png")}}]},
            {"scenario":"Login","scenario_index":0,"device":"emulator-5556","passed":false,"steps":[
                {"index":0,"passed":false,"result":{"error":{"code":"baseline_missing","details":{"actual":run.join("b-000.png")}}}}]}]})).unwrap();
        (root, run, base)
    }

    #[test]
    fn review_lists_new_changed_passed_and_unreached_screens() {
        let (_root, run, _base) = fixture();
        let value = baselines(&json!({"run":run,"all_screens":true})).unwrap();
        assert_eq!(
            value["counts"],
            json!({"changed":1,"new":2,"not_reached":4,"passed":1})
        );
        let screens = value["screens"].as_array().unwrap();
        assert_eq!(screens[0]["id"], "s000-000@emulator-5554");
        assert_eq!(screens[0]["status"], "new");
        assert_eq!(screens[1]["status"], "changed");
        assert_eq!(screens[1]["changed_fraction"], 0.5);
        assert_eq!(screens[2]["status"], "passed");
        assert_eq!(screens[3]["status"], "not_reached");
        assert_eq!(screens[4]["actual"], json!(run.join("b-000.png")));
        let short = baselines(&json!({"run":run})).unwrap();
        assert_eq!(short["to_review"], 3);
        assert_eq!(short["screens"].as_array().unwrap().len(), 3);
    }

    #[test]
    fn approve_copies_screenshots_and_refuses_conflicts() {
        let (_root, run, base) = fixture();
        // The two devices saw different new screens for the same baseline.
        let conflict = baselines(&json!({"action":"approve","run":run,"all":true})).unwrap_err();
        assert_eq!(conflict.code, "baseline_conflict");
        assert!(!base.join("login/new.png").exists());
        let value = baselines(&json!({"action":"approve","run":run,
            "screens":["s000-000@emulator-5556","s000-001@emulator-5554"]}))
        .unwrap();
        assert_eq!(value["approved"].as_array().unwrap().len(), 2);
        assert_eq!(fs::read(base.join("login/new.png")).unwrap(), png(8));
        assert_eq!(fs::read(base.join("login/home.png")).unwrap(), png(2));
        assert_eq!(
            baselines(&json!({"action":"approve","run":run,"screens":["s000-009@x"]}))
                .unwrap_err()
                .code,
            "screen_not_found"
        );
        assert_eq!(
            baselines(&json!({"action":"approve","run":run,"screens":["s000-004@emulator-5554"]}))
                .unwrap_err()
                .code,
            "screenshot_missing"
        );
        assert_eq!(
            baselines(&json!({"action":"approve","run":run}))
                .unwrap_err()
                .code,
            "invalid_argument"
        );
        let listed = baselines(&json!({"action":"list","directory":base})).unwrap();
        assert_eq!(listed["total"], 3);
        assert_eq!(listed["baselines"][0]["name"], "login/done.png");
        assert_eq!(listed["baselines"][0]["width"], 2);
        assert_eq!(listed["baselines"][0]["height"], 1);
        assert_eq!(
            baselines(&json!({"run":base})).unwrap_err().code,
            "not_a_run_directory"
        );
    }
}
