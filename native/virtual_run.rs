//! project.run on the virtual iPhone: one job that builds an xtool project,
//! installs the built app on the running phone with ios.virtual.deploy
//! (which opens it), and then looks for a crash report of that launch.
//!
//! The job has three steps: `_virtual_run begin` records when the build
//! started, `xtool dev build` builds, and `_virtual_run deploy` does the rest.
//! Each writes the run's report, `build/tomacco/virtual_iphone_run.json` in
//! the project, which the job publishes and project.watch reads for its log
//! line. A report still in state `building` in a failed job means the build
//! failed.
use crate::{apple_dev, core::*, ios_emulator, jobs, toolchain as tc};
use serde_json::{Value, json};
use std::{collections::BTreeSet, path::Path, thread, time::Duration};

pub use ios_emulator::ui_testing::DEVICE;
/// The run's report, relative to the project.
pub const REPORT: &str = "build/tomacco/virtual_iphone_run.json";
/// The longest crash check.
const CRASH_CHECK_LIMIT: u64 = 120;
/// Prefix of the deploy script's event lines in its job log.
const EVENT: &str = "TOMACCO_VIRTUAL_IPHONE ";

/// project.run with device `virtual-iphone`: refused at once unless the
/// project is an xtool project and the phone runs.
pub fn submit(project: &Value, a: &Value, env: Value) -> Result {
    if s(a, "platform") != "ios" {
        return Err(Error::new(
            "platform_unsupported",
            "The virtual iPhone runs iOS apps; pass platform ios",
        ));
    }
    let is_xtool = project["kinds"]
        .as_array()
        .is_some_and(|kinds| kinds.iter().any(|k| k == "xtool"));
    if !is_xtool {
        return Err(Error::new(
            "action_unsupported",
            "project.run builds only xtool projects for the virtual iPhone",
        )
        .remedy("Build an arm64 .app yourself, then install it with ios.virtual.deploy"));
    }
    ios_emulator::virtual_phone::running_guest().map_err(|error| {
        if error.code == "ios_virtual_not_running" {
            error.remedy(
                "Start the phone with ios.virtual.start and wait until ios.virtual.status says launch-accepted",
            )
        } else {
            error
        }
    })?;
    let seconds = a["crash_check_seconds"].as_u64().unwrap_or(10);
    if seconds > CRASH_CHECK_LIMIT {
        return Err(Error::new(
            "invalid_argument",
            format!("crash_check_seconds is 0 to {CRASH_CHECK_LIMIT}"),
        ));
    }
    let root = s(project, "path");
    let request = json!({"path":root,"bundle_id":s(a,"bundle_id"),"crash_check_seconds":seconds});
    let own = own_executable()?;
    jobs::submit(
        &format!("Run {} on the virtual iPhone", s(project, "name")),
        json!({"steps":[
                {"argv":[own,"_virtual_run","begin",request.to_string()]},
                {"argv":[tc::find("xtool")?,"dev","build","--configuration",s(a,"configuration")]},
                {"argv":[own,"_virtual_run","deploy",request.to_string()]}],
            "cwd":root,"env":env,"exclusive":root,"single_active":true,"resource":"build",
            "always_artifacts":[REPORT],"timeout":3600}),
    )
}

/// The `_virtual_run` steps of the job.
pub fn worker(stage: &str, request: &str) -> Result<i32> {
    let request: Value = serde_json::from_str(request)?;
    let root = path(s(&request, "path"), true, true)?;
    let report = root.join(REPORT);
    match stage {
        "begin" => {
            if let Some(parent) = report.parent() {
                std::fs::create_dir_all(parent)?;
            }
            atomic_json(&report, &json!({"state":"building","started":now()}))?;
            Ok(0)
        }
        "deploy" => {
            let started = read_json(&report, Some(json!({})))?["started"].as_f64();
            let build_seconds = started.map(|t| round((now() - t).max(0.0)));
            let mut phone = Live;
            let value = match built(&root, s(&request, "bundle_id")) {
                Ok((app, bundle_id, executable)) => run(
                    &mut phone,
                    &Target {
                        app: &app,
                        bundle_id: &bundle_id,
                        executable: &executable,
                    },
                    n(&request, "crash_check_seconds"),
                    build_seconds,
                ),
                Err(error) => {
                    json!({"state":"failed","build_seconds":build_seconds,"error":error.value()})
                }
            };
            atomic_json(&report, &value)?;
            println!("{}", summary(&value));
            Ok(if value["state"] == "launched" { 0 } else { 1 })
        }
        _ => Err(Error::new("invalid_request", "Unknown virtual run step")),
    }
}

/// The one built app of an xtool project, its bundle identifier and its
/// executable's name; `expected` (when given) must be that identifier.
fn built(root: &Path, expected: &str) -> Result<(std::path::PathBuf, String, String)> {
    let app = apple_dev::built_app(root)?;
    let inspected = ios_emulator::virtual_phone::inspect_application(&app, None)?;
    let bundle_id = s(&inspected, "bundle_id").to_string();
    if !expected.is_empty() && expected != bundle_id {
        return Err(Error::new(
            "bundle_id_mismatch",
            format!("The built app is {bundle_id}, not {expected}"),
        ));
    }
    Ok((app, bundle_id, s(&inspected, "executable").to_string()))
}

fn round(seconds: f64) -> f64 {
    (seconds * 10.0).round() / 10.0
}

/// What the run needs to know of the built app.
pub struct Target<'a> {
    pub app: &'a Path,
    pub bundle_id: &'a str,
    pub executable: &'a str,
}

/// The phone as the run uses it. Tests replace it with recorded replies.
pub trait Phone {
    /// Starts ios.virtual.deploy of the bundle; its job.
    fn deploy(&mut self, bundle: &Path) -> Result;
    /// Waits until the job has ended; the job.
    fn wait(&mut self, job: &str) -> Result;
    /// The job's log text.
    fn log(&mut self, job: &str) -> Result<String>;
    /// testing.crashes on the virtual iPhone for this app.
    fn crashes(&mut self, target: &Target) -> Result;
    fn sleep(&mut self, duration: Duration);
}

struct Live;
impl Phone for Live {
    fn deploy(&mut self, bundle: &Path) -> Result {
        crate::api::dispatch("ios.virtual.deploy", &json!({"bundle":bundle}))
    }
    fn wait(&mut self, job: &str) -> Result {
        loop {
            let done = jobs::wait(job, 60)?;
            if jobs::TERMINAL.contains(&s(&done, "state")) {
                return Ok(done);
            }
        }
    }
    fn log(&mut self, job: &str) -> Result<String> {
        let reply = jobs::logs(&json!({"job_id":job,"offset":0,"limit":4 * 1024 * 1024}))?;
        Ok(s(&reply, "text").to_string())
    }
    fn crashes(&mut self, target: &Target) -> Result {
        ios_emulator::ui_testing::crashes(
            &mut ios_emulator::ui_testing::LivePhone::default(),
            &json!({"device":DEVICE,"process":target.executable,"bundle_id":target.bundle_id}),
        )
    }
    fn sleep(&mut self, duration: Duration) {
        thread::sleep(duration);
    }
}

/// The deploy script's events in its job log, and its closing line.
fn deploy_events(log: &str) -> (Vec<Value>, Value) {
    let mut events = Vec::new();
    let mut last = Value::Null;
    for line in log.lines() {
        if let Some(event) = line.strip_prefix(EVENT) {
            if let Ok(value) = serde_json::from_str::<Value>(event) {
                if value["phase"] == "deploy" {
                    events.push(value);
                }
            }
        } else if let Ok(value) = serde_json::from_str::<Value>(line.trim()) {
            if value.get("state").is_some() {
                last = value;
            }
        }
    }
    (events, last)
}

/// The names of the crash reports a testing.crashes reply lists.
fn report_names(reply: &Value) -> BTreeSet<String> {
    reply["events"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|event| s(&event["report"], "name").to_string())
        .collect()
}

/// Deploys and opens the app, then waits `crash_seconds` and reads its
/// crash reports. A report that was not there before the deploy is a crash
/// of this run; one whose PID is the opened app's says so. The report says
/// what happened in each part, with the seconds each took.
pub fn run(
    phone: &mut impl Phone,
    target: &Target,
    crash_seconds: u64,
    build_seconds: Option<f64>,
) -> Value {
    let mut value = json!({"state":"failed","app":target.app,"bundle_id":target.bundle_id,
        "executable":target.executable,"build_seconds":build_seconds});
    // The reports already there, so that an old crash is not this run's.
    let before = if crash_seconds > 0 {
        phone.crashes(target).ok().map(|reply| report_names(&reply))
    } else {
        None
    };
    let job = match phone.deploy(target.app) {
        Ok(job) => job,
        Err(error) => {
            value["error"] = error.value();
            return value;
        }
    };
    let id = s(&job, "id").to_string();
    let done = match phone.wait(&id) {
        Ok(done) => done,
        Err(error) => {
            value["deploy"] = json!({"job_id":id});
            value["error"] = error.value();
            return value;
        }
    };
    let (events, last) = deploy_events(&phone.log(&id).unwrap_or_default());
    let step = |name: &str| events.iter().find(|e| e["step"] == name);
    let pid = step("opened").and_then(|e| e["pid"].as_u64());
    value["deploy"] = json!({"job_id":id,"state":done["state"],
        "seconds":events.last().map(|e| e["seconds"].clone()),
        "unchanged":step("unchanged").is_some(),
        "steps":events.iter().map(|e| e["step"].clone()).collect::<Vec<_>>()});
    value["pid"] = json!(pid);
    if done["state"] != "succeeded" {
        value["error"] = json!({"code":"ios_virtual_deploy_failed",
            "message":last.get("error").and_then(Value::as_str)
                .map_or_else(|| format!("The deploy job {}", s(&done, "state")), str::to_string)});
        return value;
    }
    value["state"] = json!("launched");
    if crash_seconds == 0 {
        value["crash_check"] = json!({"seconds":0,"checked":false});
        return value;
    }
    phone.sleep(Duration::from_secs(crash_seconds));
    let mut check = json!({"seconds":crash_seconds,"checked":true,"absence_proves_no_crash":false});
    match phone.crashes(target) {
        Ok(reply) => {
            let new: Vec<Value> = reply["events"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|event| {
                    let known = before
                        .as_ref()
                        .is_some_and(|names| names.contains(s(&event["report"], "name")));
                    let same = pid.is_some() && event["pid"].as_u64() == pid;
                    // Without the earlier list only the opened PID tells.
                    same || (!known && before.is_some())
                })
                .cloned()
                .collect();
            check["baseline"] = json!(before.is_some());
            check["collection_complete"] = reply["collection_complete"].clone();
            if !new.is_empty() {
                value["state"] = json!("crashed");
                check["launch_pid_matched"] = json!(
                    new.iter()
                        .any(|e| pid.is_some() && e["pid"].as_u64() == pid)
                );
            }
            check["crashes"] = json!(new);
        }
        Err(error) => {
            check["checked"] = json!(false);
            check["error"] = error.value();
        }
    }
    value["crash_check"] = check;
    value
}

/// One line for a person or an agent: how the run ended and how long each
/// part took.
pub fn summary(report: &Value) -> String {
    let mut parts = Vec::new();
    if let Some(seconds) = report["build_seconds"].as_f64() {
        parts.push(format!("built in {seconds:.1} s"));
    }
    if let Some(seconds) = report["deploy"]["seconds"].as_f64() {
        parts.push(if report["deploy"]["unchanged"] == true {
            format!("unchanged, opened in {seconds:.1} s")
        } else {
            format!("deployed in {seconds:.1} s")
        });
    }
    if let Some(pid) = report["pid"].as_u64() {
        parts.push(format!("pid {pid}"));
    }
    match s(report, "state") {
        "launched" => match report["crash_check"]["seconds"].as_u64() {
            Some(seconds) if report["crash_check"]["checked"] == true => {
                parts.push(format!("no new crash report in {seconds} s"))
            }
            Some(0) => parts.push("crash check off".into()),
            _ => parts.push(format!(
                "crash check failed: {}",
                s(&report["crash_check"]["error"], "message")
            )),
        },
        "crashed" => {
            let crash = &report["crash_check"]["crashes"][0];
            parts.push(format!(
                "CRASHED within {} s: {} {}",
                report["crash_check"]["seconds"],
                s(crash, "exception"),
                s(crash, "message")
            ));
        }
        "building" => parts.push("the build failed".into()),
        _ => parts.push(format!("failed: {}", s(&report["error"], "message"))),
    }
    parts.join(", ")
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fake {
        calls: Vec<String>,
        refuse: Option<(&'static str, &'static str)>,
        state: &'static str,
        log: String,
        crashes: Vec<Value>,
    }
    impl Phone for Fake {
        fn deploy(&mut self, bundle: &Path) -> Result {
            self.calls.push(format!("deploy {}", bundle.display()));
            match self.refuse {
                Some((code, message)) => Err(Error::new(code, message)),
                None => Ok(json!({"id":"d1"})),
            }
        }
        fn wait(&mut self, job: &str) -> Result {
            self.calls.push(format!("wait {job}"));
            Ok(json!({"id":job,"state":self.state}))
        }
        fn log(&mut self, _job: &str) -> Result<String> {
            Ok(self.log.clone())
        }
        fn crashes(&mut self, target: &Target) -> Result {
            self.calls.push(format!("crashes {}", target.executable));
            Ok(if self.crashes.is_empty() {
                json!({"events":[]})
            } else {
                self.crashes.remove(0)
            })
        }
        fn sleep(&mut self, duration: Duration) {
            self.calls.push(format!("sleep {}", duration.as_secs()));
        }
    }
    fn event(step: &str, seconds: f64, extra: Value) -> String {
        let mut value = json!({"phase":"deploy","step":step,"seconds":seconds});
        for (k, v) in extra.as_object().unwrap() {
            value[k] = v.clone();
        }
        format!("{EVENT}{value}")
    }
    fn fake(log: Vec<String>, crashes: Vec<Value>) -> Fake {
        Fake {
            calls: Vec::new(),
            refuse: None,
            state: "succeeded",
            log: log.join("\n"),
            crashes,
        }
    }
    fn crash(name: &str, pid: u64) -> Value {
        json!({"pid":pid,"exception":"EXC_CRASH","message":"boom","report":{"name":name}})
    }
    fn target() -> Target<'static> {
        Target {
            app: Path::new("/p/xtool/Demo.app"),
            bundle_id: "dev.example.Demo",
            executable: "Demo",
        }
    }
    fn opened() -> Vec<String> {
        vec![
            "starting".into(),
            event("signed", 1.0, json!({})),
            event("installed", 20.0, json!({})),
            event("opened", 23.5, json!({"pid":551})),
            event("done", 23.6, json!({"unchanged":false})),
            r#"{"state": "ok", "phase": "launch-accepted"}"#.into(),
        ]
    }

    #[test]
    fn a_launch_without_a_new_crash_report_is_launched_with_timings() {
        let old = json!({"events":[crash("Demo-2026-10-01-000000.ips", 40)]});
        let mut phone = fake(opened(), vec![old.clone(), old]);
        let report = run(&mut phone, &target(), 10, Some(41.23));
        assert_eq!(
            phone.calls,
            [
                "crashes Demo",
                "deploy /p/xtool/Demo.app",
                "wait d1",
                "sleep 10",
                "crashes Demo"
            ]
        );
        assert_eq!(report["state"], "launched");
        assert_eq!(report["pid"], 551);
        assert_eq!(report["deploy"]["seconds"], 23.6);
        assert_eq!(report["deploy"]["unchanged"], false);
        assert_eq!(report["crash_check"]["crashes"], json!([]));
        assert_eq!(
            summary(&report),
            "built in 41.2 s, deployed in 23.6 s, pid 551, no new crash report in 10 s"
        );
    }

    #[test]
    fn a_new_crash_report_of_the_opened_process_makes_the_run_crashed() {
        let before = json!({"events":[crash("Demo-2026-10-01-000000.ips", 40)]});
        let after = json!({"events":[crash("Demo-2026-11-01-101010.ips", 551),
            crash("Demo-2026-10-01-000000.ips", 40)]});
        let mut phone = fake(opened(), vec![before, after]);
        let report = run(&mut phone, &target(), 5, None);
        assert_eq!(report["state"], "crashed");
        assert_eq!(report["crash_check"]["launch_pid_matched"], true);
        assert_eq!(
            report["crash_check"]["crashes"].as_array().unwrap().len(),
            1
        );
        assert_eq!(
            summary(&report),
            "deployed in 23.6 s, pid 551, CRASHED within 5 s: EXC_CRASH boom"
        );
    }

    #[test]
    fn an_unchanged_app_is_reported_as_such_and_the_check_can_be_off() {
        let log = vec![
            event("signed", 0.8, json!({})),
            event("unchanged", 0.8, json!({})),
            event("opened", 3.1, json!({"pid":600})),
            event("done", 3.2, json!({"unchanged":true})),
        ];
        let mut phone = fake(log, vec![]);
        let report = run(&mut phone, &target(), 0, Some(2.0));
        assert_eq!(phone.calls, ["deploy /p/xtool/Demo.app", "wait d1"]);
        assert_eq!(report["deploy"]["unchanged"], true);
        assert_eq!(
            summary(&report),
            "built in 2.0 s, unchanged, opened in 3.2 s, pid 600, crash check off"
        );
    }

    #[test]
    fn a_refused_or_failed_deploy_ends_the_run_with_its_reason() {
        let mut phone = fake(vec![], vec![]);
        phone.refuse = Some(("ios_virtual_not_running", "The phone is off"));
        let report = run(&mut phone, &target(), 0, Some(1.0));
        assert_eq!(report["state"], "failed");
        assert_eq!(report["error"]["code"], "ios_virtual_not_running");
        assert_eq!(summary(&report), "built in 1.0 s, failed: The phone is off");

        let mut phone = fake(
            vec![
                event("signed", 1.0, json!({})),
                r#"{"state": "failed", "error": "installd did not install dev.example.Demo"}"#
                    .into(),
            ],
            vec![],
        );
        phone.state = "failed";
        let report = run(&mut phone, &target(), 10, None);
        assert_eq!(report["state"], "failed");
        assert_eq!(report["error"]["code"], "ios_virtual_deploy_failed");
        assert_eq!(
            report["error"]["message"],
            "installd did not install dev.example.Demo"
        );
        assert!(!phone.calls.iter().any(|c| c.starts_with("sleep")));
    }

    #[test]
    fn a_build_that_failed_says_so() {
        let report = json!({"state":"building","started":1.0});
        assert_eq!(summary(&report), "the build failed");
    }
}
