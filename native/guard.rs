//! Guardrails for calls from agents: every operation called through
//! `tomaccoctl call` or the MCP server passes here on its way to
//! `api::dispatch`.
//!
//! - The agent policy (policy.rs) may refuse the operation, or a step of
//!   `android.each` or `workflow.run`, as `policy_denied`.
//! - Under a policy whose writes stay in the roots (scope.rs), a destination
//!   outside them is refused as `path_outside_roots`.
//! - A destructive call from an MCP agent may first need a yes from the
//!   person at the desktop (approval.rs), else `approval_denied`.
//! - An MCP session may run at most `jobs_per_session` jobs at once
//!   (`session_job_limit`), and gets results up to `inline_result_kib`
//!   inline; a larger one is saved for `result.read` (`result_too_large`).
//! - Calls that may change something, and every refused call, go to the
//!   audit log (audit.rs).
//!
//! These limit what an agent does through Tomacco's operations. They are not
//! a sandbox: an agent that can run commands as the user can change
//! settings.json, or do what the operations do, without Tomacco.
mod approval;
mod audit;
pub mod policy;
mod results;
mod scope;

use crate::{api, core::*};
use serde_json::{Value, json};
use std::sync::{
    Mutex, RwLock,
    atomic::{AtomicU64, Ordering},
};

/// Who is calling: `tomaccoctl call` or an MCP client.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Channel {
    Cli,
    Mcp,
}
impl Channel {
    fn name(self) -> &'static str {
        match self {
            Channel::Cli => "cli",
            Channel::Mcp => "mcp",
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct Caller {
    pub channel: Option<Channel>,
    pub client: String,
    pub client_version: String,
    /// The policy the server was started with (`--policy`, TOMACCO_POLICY).
    pub chosen: Option<String>,
}

static CALLER: RwLock<Caller> = RwLock::new(Caller {
    channel: None,
    client: String::new(),
    client_version: String::new(),
    chosen: None,
});

/// Names the policy for this process: `tomaccoctl mcp --policy NAME`.
pub const POLICY_VARIABLE: &str = "TOMACCO_POLICY";
/// A name for the caller of `tomaccoctl call`, recorded in the audit log.
pub const CLIENT_VARIABLE: &str = "TOMACCO_CLIENT";
/// Set to `desktop` by the Tomacco desktop for the commands it runs for the
/// person in front of it; those pass no guard and are not audited.
pub const CALLER_VARIABLE: &str = "TOMACCO_CALLER";

fn environment(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|v| !v.is_empty())
}

/// Marks this process as serving agents on a channel. `chosen` is the
/// policy the command line names; TOMACCO_POLICY is used otherwise.
pub fn enter(channel: Channel, chosen: Option<String>) {
    if environment(CALLER_VARIABLE).as_deref() == Some("desktop") {
        return;
    }
    let mut caller = CALLER.write().unwrap();
    caller.channel = Some(channel);
    caller.chosen = chosen.or_else(|| environment(POLICY_VARIABLE));
    if channel == Channel::Cli {
        caller.client = environment(CLIENT_VARIABLE).unwrap_or_default();
    }
}

/// Records the MCP client's `clientInfo` from initialize.
pub fn set_client(info: &Value) {
    let mut caller = CALLER.write().unwrap();
    caller.client = s(info, "name").chars().take(100).collect();
    caller.client_version = s(info, "version").chars().take(100).collect();
}

pub fn current() -> Caller {
    CALLER.read().unwrap().clone()
}

/// The error codes a guard answers with.
const REFUSALS: [&str; 5] = [
    "policy_denied",
    "policy_invalid",
    "approval_denied",
    "path_outside_roots",
    "session_job_limit",
];

/// What the audit log records about the guards for one call.
#[derive(Default)]
struct Record {
    policy: String,
    approval: Option<&'static str>,
}

/// Calls an operation for an agent: the guards, the operation, the audit
/// log and, over MCP, the size limit of the answer.
pub fn dispatch(name: &str, arguments: &Value) -> Result {
    let caller = current();
    let Some(channel) = caller.channel else {
        return api::dispatch(name, arguments);
    };
    let started = now();
    let clock = std::time::Instant::now();
    let mut record = Record::default();
    let settings = policy::Settings::load();
    let result = match &settings {
        Ok(settings) => {
            JOB_LIMIT.store(settings.jobs_per_session(), Ordering::Relaxed);
            guarded(
                name,
                arguments,
                &caller,
                settings,
                &mut record,
                api::dispatch,
                |question, timeout, session| {
                    let socket = match approval::socket(session) {
                        Ok(socket) => socket,
                        Err(error) => return approval::Answer::Denied(error.message),
                    };
                    approval::ask(&socket, question, std::time::Duration::from_secs(timeout))
                },
            )
        }
        // From a terminal, a new agent_policy replaces one that cannot be read.
        Err(_)
            if channel == Channel::Cli
                && name == "settings.configure"
                && arguments.get("agent_policy").is_some() =>
        {
            api::dispatch(name, arguments)
        }
        Err(error) => {
            Err(Error::new(&error.code, error.message.clone()).remedy(error.remedy.clone()))
        }
    };
    let audit_on = settings.as_ref().map_or(true, |s| s.audit());
    let refused = result
        .as_ref()
        .is_err_and(|e| REFUSALS.contains(&e.code.as_str()));
    if audit_on && (mutating(name) || refused) {
        let entry = entry(
            name,
            arguments,
            &caller,
            channel,
            &record,
            &result,
            started,
            clock.elapsed().as_secs_f64(),
        );
        if let Err(error) = audit::append(&entry) {
            eprintln!("tomaccoctl: cannot write the audit log: {}", error.message);
        }
    }
    match (channel, result) {
        (Channel::Mcp, Ok(value)) if name != "result.read" => {
            let limit = settings.map_or(2048 * 1024, |s| s.inline_result_bytes());
            results::inline_or_saved(name, value, limit)
        }
        (_, result) => result,
    }
}

/// Whether an operation may change something, by its schema; an unknown one
/// counts as changing.
fn mutating(name: &str) -> bool {
    api::operation(name).map_or(true, |op| op["annotations"]["readOnlyHint"] != true)
}

/// The operations a composite call runs as its steps.
fn steps<'a>(name: &str, arguments: &'a Value) -> Vec<(&'a str, &'a Value)> {
    if !matches!(name, "android.each" | "workflow.run") {
        return Vec::new();
    }
    arguments["steps"]
        .as_array()
        .map(|steps| {
            steps
                .iter()
                .filter_map(|step| {
                    step["operation"]
                        .as_str()
                        .map(|op| (op, &step["arguments"]))
                })
                .collect()
        })
        .unwrap_or_default()
}

fn refuse(policy: &policy::Policy, name: &str, source: &str, step_of: Option<&str>) -> Result<()> {
    let op = api::operation(name)?;
    let Some(reason) = policy.refusal(name, &op["annotations"]) else {
        return Ok(());
    };
    let what = match step_of {
        Some(outer) => format!("{outer} has a step {name}, which"),
        None => name.to_string(),
    };
    let chosen_by = match source {
        "server" => "the MCP server's --policy or TOMACCO_POLICY",
        "client" => "agent_policy.clients for this client",
        _ => "agent_policy in settings.json",
    };
    Err(Error::new(
        "policy_denied",
        format!(
            "{what} is not allowed by the agent policy {}: {reason}",
            policy.name
        ),
    )
    .remedy(format!(
        "Do not work around it. Tell the person what you wanted to run and why; they can run it themselves, or change the policy ({chosen_by} chose it). agent.policy shows what is allowed"
    ))
    .details(json!({"operation":name,"policy":policy.name,"reason":reason,"chosen_by":source})))
}

fn guarded(
    name: &str,
    arguments: &Value,
    caller: &Caller,
    settings: &policy::Settings,
    record: &mut Record,
    dispatch: impl Fn(&str, &Value) -> Result,
    ask: impl Fn(&Value, u64, &str) -> approval::Answer,
) -> Result {
    let op = api::operation(name)?;
    let mcp = caller.channel == Some(Channel::Mcp);
    let (chosen, source) = settings.choose(mcp, &caller.client, caller.chosen.as_deref());
    record.policy = chosen.clone();
    let policy = settings.named(&chosen)?;
    refuse(&policy, name, source, None)?;
    let inner = steps(name, arguments);
    for (step, _) in &inner {
        refuse(&policy, step, source, Some(name))?;
    }
    if name == "settings.configure"
        && arguments.get("agent_policy").is_some()
        && (mcp || !policy.unrestricted())
    {
        return Err(Error::new(
            "policy_denied",
            "An agent may not change the agent policy: agent_policy is set from a terminal without a policy, or in the desktop",
        )
        .remedy("Ask the person to change it themselves")
        .details(json!({"operation":name,"policy":policy.name,"reason":"agent_policy"})));
    }
    if policy.roots_only {
        let roots = scope::Roots::current(&policy.write_roots);
        scope::check(
            name,
            &api::arguments(name, arguments)?,
            &policy.name,
            &roots,
        )?;
        for (step, step_arguments) in &inner {
            scope::check(step, step_arguments, &policy.name, &roots)?;
        }
    }
    let destructive = op["annotations"]["destructiveHint"] != false;
    if mcp && destructive && settings.approval_enabled() {
        let timeout = settings.approval_timeout();
        let question = approval::question(name, &caller.client, arguments, timeout);
        match ask(&question, timeout, &settings.approval_session()) {
            approval::Answer::Approved => record.approval = Some("approved"),
            approval::Answer::NoDesktop if settings.approval_without_desktop() => {
                record.approval = Some("no_desktop")
            }
            approval::Answer::NoDesktop => {
                record.approval = Some("no_desktop");
                return Err(approval::denied(
                    name,
                    "no Tomacco desktop is open to ask, and the policy denies destructive calls without one",
                ));
            }
            approval::Answer::Denied(reason) => {
                record.approval = Some("denied");
                return Err(approval::denied(name, &reason));
            }
        }
    }
    dispatch(name, arguments)
}

#[allow(clippy::too_many_arguments)]
fn entry(
    name: &str,
    arguments: &Value,
    caller: &Caller,
    channel: Channel,
    record: &Record,
    result: &Result,
    started: f64,
    seconds: f64,
) -> Value {
    let job = result
        .as_ref()
        .ok()
        .filter(|v| v["id"].as_str().is_some_and(|id| id.len() == 32) && v["state"].is_string())
        .map(|v| v["id"].clone());
    json!({"time":started,"channel":channel.name(),"client":caller.client,"client_version":caller.client_version,
           "operation":name,"arguments":audit::arguments(arguments),"mutating":mutating(name),
           "ok":result.is_ok(),"code":result.as_ref().err().map(|e| e.code.clone()),
           "job_id":job,"policy":record.policy,"approval":record.approval,
           "duration_seconds":(seconds * 1000.0).round() / 1000.0})
}

/// How many jobs this MCP session may run at once; 0 is no limit.
static JOB_LIMIT: AtomicU64 = AtomicU64::new(8);

/// The jobs this MCP session started that may still run, and the slots
/// taken by submissions under way.
struct SessionJobs {
    ids: Vec<String>,
    reserved: usize,
}
static SESSION_JOBS: Mutex<SessionJobs> = Mutex::new(SessionJobs {
    ids: Vec::new(),
    reserved: 0,
});

/// A place for one more job of this session; dropped unused, it is free again.
#[derive(Debug)]
pub struct JobSlot(bool);
impl JobSlot {
    /// The job that took the place.
    pub fn started(mut self, id: &str) {
        if self.0 {
            let mut jobs = SESSION_JOBS.lock().unwrap();
            jobs.ids.push(id.to_string());
            jobs.reserved -= 1;
            self.0 = false;
        }
    }
}
impl Drop for JobSlot {
    fn drop(&mut self) {
        if self.0 {
            SESSION_JOBS.lock().unwrap().reserved -= 1;
        }
    }
}

/// Called by jobs::submit before it records a new job: an MCP session at
/// its limit is refused.
pub fn admit_job() -> Result<JobSlot> {
    if current().channel != Some(Channel::Mcp) {
        return Ok(JobSlot(false));
    }
    admit(JOB_LIMIT.load(Ordering::Relaxed), |id| {
        crate::jobs::get(id).is_ok_and(|job| !crate::jobs::TERMINAL.contains(&s(&job, "state")))
    })
}

fn admit(limit: u64, active: impl Fn(&str) -> bool) -> Result<JobSlot> {
    if limit == 0 {
        return Ok(JobSlot(false));
    }
    let mut jobs = SESSION_JOBS.lock().unwrap();
    jobs.ids.retain(|id| active(id));
    if (jobs.ids.len() + jobs.reserved) as u64 >= limit {
        return Err(Error::new(
            "session_job_limit",
            format!(
                "This MCP session already runs {} jobs, its limit (agent_policy.limits.jobs_per_session)",
                jobs.ids.len() + jobs.reserved
            ),
        )
        .remedy("Wait for one of them with job.wait, or cancel one you no longer need with job.cancel, then start this one")
        .details(json!({"running":jobs.ids,"limit":limit})));
    }
    jobs.reserved += 1;
    Ok(JobSlot(true))
}

/// `agent.policy`: the policy a caller gets, and the guards around it.
pub fn describe(a: &Value) -> Result {
    let settings = policy::Settings::load()?;
    let caller = current();
    let channel = match s(a, "channel") {
        "mcp" => Channel::Mcp,
        "cli" => Channel::Cli,
        _ => caller.channel.unwrap_or(Channel::Cli),
    };
    let client = a["client"]
        .as_str()
        .map_or(caller.client.clone(), str::to_string);
    let chosen = caller
        .chosen
        .as_deref()
        .filter(|_| caller.channel == Some(channel));
    let (name, source) = settings.choose(channel == Channel::Mcp, &client, chosen);
    let policy = settings.named(&name)?;
    let roots = policy
        .roots_only
        .then(|| scope::Roots::current(&policy.write_roots).value());
    Ok(
        json!({"channel":channel.name(),"client":client,"policy":policy.value(),"chosen_by":source,
        "write_roots":roots,
        "approval":{"destructive":settings.approval_enabled(),"timeout_seconds":settings.approval_timeout(),
                    "without_desktop":if settings.approval_without_desktop() {"allow"} else {"deny"},
                    "session":settings.approval_session()},
        "limits":{"jobs_per_session":settings.jobs_per_session(),"inline_result_kib":settings.inline_result_bytes() / 1024},
        "audit":settings.audit(),"configured":settings.0}),
    )
}

/// `audit.list`.
pub fn audit_list(a: &Value) -> Result {
    audit::list(a)
}

/// `result.read`.
pub fn result_read(a: &Value) -> Result {
    results::read(a)
}

/// Checks `agent_policy` for settings.configure.
pub fn validated_policy(value: &Value) -> Result {
    policy::validated(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    fn mcp(client: &str, chosen: Option<&str>) -> Caller {
        Caller {
            channel: Some(Channel::Mcp),
            client: client.into(),
            client_version: "1".into(),
            chosen: chosen.map(str::to_string),
        }
    }
    fn run(
        name: &str,
        arguments: Value,
        caller: &Caller,
        settings: Value,
        answer: approval::Answer,
    ) -> (Result, Vec<String>, Record, usize) {
        let ran = RefCell::new(Vec::new());
        let asked = RefCell::new(0);
        let answer = RefCell::new(Some(answer));
        let mut record = Record::default();
        let result = guarded(
            name,
            &arguments,
            caller,
            &policy::Settings(settings),
            &mut record,
            |op, _| {
                ran.borrow_mut().push(op.to_string());
                Ok(json!({"done":op}))
            },
            |question, timeout, session| {
                assert_eq!(session, "main");
                assert!(timeout >= 5);
                assert!(question["operation"].is_string());
                *asked.borrow_mut() += 1;
                answer.borrow_mut().take().unwrap()
            },
        );
        (result, ran.into_inner(), record, asked.into_inner())
    }
    #[test]
    fn a_policy_refuses_before_anything_runs() {
        let no = approval::Answer::NoDesktop;
        let settings = json!({"mcp":"read_only","clients":{"builder":"no_destructive"}});
        let (result, ran, record, _) = run(
            "project.run",
            json!({"path":"/tmp"}),
            &mcp("x", None),
            settings.clone(),
            no,
        );
        let error = result.unwrap_err();
        assert_eq!(error.code, "policy_denied");
        assert!(error.message.contains("read_only"), "{}", error.message);
        assert!(error.remedy.contains("settings.json"));
        assert!(ran.is_empty());
        assert_eq!(record.policy, "read_only");
        // The client's entry, then the server's own choice.
        let (result, ran, ..) = run(
            "doctor",
            json!({}),
            &mcp("builder", None),
            settings.clone(),
            approval::Answer::NoDesktop,
        );
        assert!(result.is_ok());
        assert_eq!(ran, ["doctor"]);
        let (result, ..) = run(
            "job.cancel",
            json!({"job_id":"x"}),
            &mcp("builder", None),
            settings.clone(),
            approval::Answer::NoDesktop,
        );
        assert!(result.unwrap_err().message.contains("no destructive"));
        let (result, ..) = run(
            "job.cancel",
            json!({"job_id":"x"}),
            &mcp("builder", Some("full")),
            settings.clone(),
            approval::Answer::NoDesktop,
        );
        assert!(result.is_ok());
        // A step of a composite call is checked like the call.
        let each = json!({"steps":[{"operation":"android.launch"},{"operation":"device.install","arguments":{}}]});
        let (result, ran, ..) = run(
            "android.each",
            each,
            &mcp("x", None),
            json!({"policies":{"p":{"deny":["device.install"],"writes":"anywhere"}},"mcp":"p"}),
            approval::Answer::NoDesktop,
        );
        let error = result.unwrap_err();
        assert!(
            error
                .message
                .starts_with("android.each has a step device.install"),
            "{}",
            error.message
        );
        assert!(ran.is_empty());
        // An unknown operation is the usual error, not a refusal.
        let (result, ..) = run(
            "projct.run",
            json!({}),
            &mcp("x", None),
            json!({}),
            approval::Answer::NoDesktop,
        );
        assert_eq!(result.unwrap_err().code, "operation_not_found");
    }
    #[test]
    fn an_agent_cannot_change_the_policy() {
        let change = json!({"agent_policy":{}});
        let (result, ran, ..) = run(
            "settings.configure",
            change.clone(),
            &mcp("x", None),
            json!({}),
            approval::Answer::NoDesktop,
        );
        assert_eq!(result.unwrap_err().code, "policy_denied");
        assert!(ran.is_empty());
        let cli = Caller {
            channel: Some(Channel::Cli),
            ..Caller::default()
        };
        let (result, ..) = run(
            "settings.configure",
            change.clone(),
            &cli,
            json!({}),
            approval::Answer::NoDesktop,
        );
        assert!(result.is_ok());
        let (result, ..) = run(
            "settings.configure",
            change,
            &cli,
            json!({"cli":"no_destructive"}),
            approval::Answer::NoDesktop,
        );
        assert_eq!(result.unwrap_err().code, "policy_denied");
    }
    #[test]
    fn destinations_outside_the_roots_are_refused_under_a_limiting_policy() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().canonicalize().unwrap();
        let settings = json!({"mcp":"p","policies":{"p":{"write_roots":[root]}}});
        let inside = root.join("shot.png");
        let (result, ..) = run(
            "device.capture",
            json!({"platform":"android","device":"d","path":inside}),
            &mcp("x", None),
            settings.clone(),
            approval::Answer::NoDesktop,
        );
        assert!(result.is_ok());
        let (result, ran, ..) = run(
            "device.capture",
            json!({"platform":"android","device":"d","path":"/proc/self/escape.png"}),
            &mcp("x", None),
            settings.clone(),
            approval::Answer::NoDesktop,
        );
        assert_eq!(result.unwrap_err().code, "path_outside_roots");
        assert!(ran.is_empty());
        // full writes anywhere, as before.
        let (result, ..) = run(
            "device.capture",
            json!({"platform":"android","device":"d","path":"/proc/self/escape.png"}),
            &mcp("x", None),
            json!({}),
            approval::Answer::NoDesktop,
        );
        assert!(result.is_ok());
    }
    #[test]
    fn destructive_mcp_calls_wait_for_the_desktop_when_asked_to() {
        let on = json!({"approval":{"destructive":true,"timeout_seconds":5}});
        let args = json!({"job_id":"x"});
        let (result, ran, record, asked) = run(
            "job.cancel",
            args.clone(),
            &mcp("x", None),
            on.clone(),
            approval::Answer::Approved,
        );
        assert!(result.is_ok());
        assert_eq!(
            (ran.len(), asked, record.approval),
            (1, 1, Some("approved"))
        );
        let (result, ran, record, _) = run(
            "job.cancel",
            args.clone(),
            &mcp("x", None),
            on.clone(),
            approval::Answer::Denied("timeout".into()),
        );
        let error = result.unwrap_err();
        assert_eq!(error.code, "approval_denied");
        assert!(error.message.ends_with("timeout"));
        assert!(ran.is_empty());
        assert_eq!(record.approval, Some("denied"));
        let (result, _, record, _) = run(
            "job.cancel",
            args.clone(),
            &mcp("x", None),
            on.clone(),
            approval::Answer::NoDesktop,
        );
        assert!(result.is_ok());
        assert_eq!(record.approval, Some("no_desktop"));
        let strict = json!({"approval":{"destructive":true,"without_desktop":"deny"}});
        let (result, ran, ..) = run(
            "job.cancel",
            args.clone(),
            &mcp("x", None),
            strict,
            approval::Answer::NoDesktop,
        );
        assert_eq!(result.unwrap_err().code, "approval_denied");
        assert!(ran.is_empty());
        // Not destructive, from the CLI, or with approval off: nobody is asked.
        let (_, _, _, asked) = run(
            "project.plan",
            json!({"path":"/tmp"}),
            &mcp("x", None),
            on.clone(),
            approval::Answer::Approved,
        );
        assert_eq!(asked, 0);
        let cli = Caller {
            channel: Some(Channel::Cli),
            ..Caller::default()
        };
        let (_, _, _, asked) = run(
            "job.cancel",
            args.clone(),
            &cli,
            on,
            approval::Answer::Approved,
        );
        assert_eq!(asked, 0);
        let (_, _, _, asked) = run(
            "job.cancel",
            args,
            &mcp("x", None),
            json!({}),
            approval::Answer::Approved,
        );
        assert_eq!(asked, 0);
    }
    #[test]
    fn a_session_runs_at_most_its_limit_of_jobs() {
        let active = |id: &str| id != "done";
        let first = admit(2, active).unwrap();
        first.started("a");
        let second = admit(2, active).unwrap();
        // A slot under way counts too.
        let error = admit(2, active).unwrap_err();
        assert_eq!(error.code, "session_job_limit");
        drop(second);
        admit(2, active).unwrap().started("done");
        // A finished job frees its place.
        admit(2, active).unwrap().started("b");
        assert_eq!(
            admit(2, active).unwrap_err().details.unwrap()["running"],
            json!(["a", "b"])
        );
        assert!(admit(0, active).is_ok());
        SESSION_JOBS.lock().unwrap().ids.clear();
    }
    #[test]
    fn the_audit_entry_names_the_caller_and_masks_the_arguments() {
        let record = Record {
            policy: "full".into(),
            approval: Some("approved"),
        };
        let job = Ok(json!({"id":"0123456789abcdef0123456789abcdef","state":"queued"}));
        let logged = entry(
            "project.run",
            &json!({"path":"/p","password":"pw"}),
            &mcp("claude-code", None),
            Channel::Mcp,
            &record,
            &job,
            1.0,
            0.5,
        );
        assert_eq!(logged["client"], "claude-code");
        assert_eq!(logged["channel"], "mcp");
        assert_eq!(logged["arguments"]["password"], crate::redact::MASK);
        assert_eq!(logged["job_id"], "0123456789abcdef0123456789abcdef");
        assert_eq!(logged["code"], Value::Null);
        assert_eq!(logged["approval"], "approved");
        let failed = Err(Error::new("policy_denied", "no"));
        let logged = entry(
            "files.write",
            &json!({}),
            &mcp("x", None),
            Channel::Mcp,
            &record,
            &failed,
            1.0,
            0.5,
        );
        assert_eq!(
            (logged["ok"].clone(), logged["code"].clone()),
            (json!(false), json!("policy_denied"))
        );
    }
}
