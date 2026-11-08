//! Human handoff: an agent asks the person to do something it must not do
//! itself (Face ID, a passcode, a sign-in, a purchase, a system permission),
//! waits, and goes on only after the person says it is done.
//!
//! A request is a file under `handoffs/` in the data directory, so it
//! survives the agent's process, the MCP connection and the desktop. The
//! desktop shows a banner for every waiting request; `handoff.complete` is
//! the person's action, from the banner or the terminal, never from an agent
//! over MCP. Completion does not replay anything: `handoff.wait` answers a
//! fresh observation of the session, when one is named, and the agent
//! decides from what is on the screen now.
//!
//! This is separate from the approval of destructive calls (guard): an
//! approval lets a call run; a handoff hands the device to the person. No
//! secret is asked for or stored here; `instructions` tell the person what
//! to do on the device, not what to type into Tomacco.
use crate::core::*;
use serde_json::{Value, json};
use std::{
    fs,
    path::PathBuf,
    thread,
    time::{Duration, Instant},
};

pub const KINDS: [&str; 6] = [
    "biometric",
    "passcode",
    "sign_in",
    "purchase",
    "permission",
    "other",
];
const STATES: [&str; 4] = ["waiting", "completed", "cancelled", "expired"];
const DEFAULT_TIMEOUT: u64 = 15 * 60;

#[cfg(test)]
thread_local! {
    static TEST_ROOT: std::cell::RefCell<Option<PathBuf>> = const { std::cell::RefCell::new(None) };
}

fn root() -> Result<PathBuf> {
    #[cfg(test)]
    if let Some(path) = TEST_ROOT.with(|r| r.borrow().clone()) {
        private_dir(&path)?;
        return Ok(path);
    }
    let path = home()?.join("handoffs");
    private_dir(&path)?;
    Ok(path)
}

fn lock() -> Result<Lock> {
    let lock = Lock::open(&root()?.join(".lock"))?;
    lock.acquire(false)?;
    Ok(lock)
}

fn record_path(id: &str) -> Result<PathBuf> {
    identifier(id, "handoff ID", Some("[0-9a-f]{32}"))?;
    Ok(root()?.join(format!("{id}.json")))
}

fn not_found(id: &str) -> Error {
    Error::new("handoff_not_found", format!("No handoff {id}"))
        .remedy("List the open handoffs with handoff.list")
}

fn not_waiting(record: &Value) -> Error {
    Error::new(
        "handoff_not_waiting",
        format!("Handoff {} is {}", s(record, "id"), s(record, "state")),
    )
    .remedy("A handoff ends once; ask for a new one with handoff.request if the person has to act again")
    .details(json!({"handoff":record}))
}

/// Reads a record and settles an expired wait while at it.
fn load(id: &str) -> Result<Value> {
    let path = record_path(id)?;
    if !path.exists() {
        return Err(not_found(id));
    }
    let mut record = read_json(&path, None)?;
    if s(&record, "state") == "waiting" && now() >= number(&record, "expires_at") {
        record["state"] = json!("expired");
        record["updated"] = json!(now());
        atomic_json(&path, &record)?;
    }
    Ok(record)
}

fn number(value: &Value, key: &str) -> f64 {
    value[key].as_f64().unwrap_or(0.0)
}

fn text(a: &Value, key: &str, limit: usize, required: bool) -> Result<String> {
    let value = s(a, key).trim().to_string();
    if value.is_empty() && required {
        return Err(Error::new(
            "invalid_argument",
            format!("{key} says what the person has to do"),
        ));
    }
    if value.chars().count() > limit || value.chars().any(|c| c.is_control() && c != '\n') {
        return Err(Error::new(
            "invalid_argument",
            format!("{key} is at most {limit} characters of plain text"),
        ));
    }
    Ok(value)
}

/// What the agent should do once the person is done: look first.
fn resume(record: &Value) -> Value {
    let mut resume = json!({"observe_first":true,
        "note":"The person acted on the device. Nothing was replayed: observe the screen (runtime.observe or screen.marks) and continue from what is there, not from the step that needed the person."});
    if !s(record, "session").is_empty() {
        resume["session"] = record["session"].clone();
    }
    resume
}

pub fn request(a: &Value) -> Result {
    let reason = text(a, "reason", 500, true)?;
    let instructions = text(a, "instructions", 2000, false)?;
    let kind = a["kind"].as_str().unwrap_or("other");
    if !KINDS.contains(&kind) {
        return Err(Error::new(
            "invalid_argument",
            format!("kind is one of {}", KINDS.join(", ")),
        ));
    }
    let session = s(a, "session");
    if !session.is_empty() {
        identifier(session, "session", Some("[0-9a-f]{32}"))?;
    }
    let job = s(a, "job");
    if !job.is_empty() {
        identifier(job, "job ID", Some("[0-9a-f]{32}"))?;
    }
    let timeout = a["timeout_seconds"]
        .as_u64()
        .unwrap_or(DEFAULT_TIMEOUT)
        .clamp(60, 86_400);
    let caller = crate::guard::current();
    let id = uuid::Uuid::new_v4().simple().to_string();
    let created = now();
    let record = json!({"id":id,"state":"waiting","kind":kind,"reason":reason,"instructions":instructions,
        "session":session,"job":job,"client":caller.client,"created":created,"updated":created,
        "expires_at":created + timeout as f64,"timeout_seconds":timeout,"completed":Value::Null,"cancelled":Value::Null});
    let _lock = lock()?;
    atomic_json(&record_path(&id)?, &record)?;
    let mut answer = record;
    answer["wait_with"] = json!("handoff.wait");
    answer["person_completes_with"] = json!(
        "the Done button on the Tomacco desktop, or: tomaccoctl call handoff.complete '{\"id\":\"ID\"}'"
    );
    answer["note"] = json!(
        "Do not type the person's passcode, password or code into any field, and do not try to get past the prompt: wait for the person, then observe the screen again."
    );
    Ok(answer)
}

pub fn get(a: &Value) -> Result {
    load(s(a, "id"))
}

pub fn list(a: &Value) -> Result {
    let wanted = a["state"].as_str().unwrap_or("waiting");
    if wanted != "all" && !STATES.contains(&wanted) {
        return Err(Error::new(
            "invalid_argument",
            "state is waiting, completed, cancelled, expired or all",
        ));
    }
    let limit = a["limit"].as_u64().unwrap_or(20).clamp(1, 200) as usize;
    let dir = root()?;
    let mut records = Vec::new();
    let mut problems = Vec::new();
    let mut entries: Vec<_> = fs::read_dir(&dir)?.flatten().map(|e| e.path()).collect();
    entries.sort();
    for path in entries {
        let Some(stem) = path.file_stem().and_then(|v| v.to_str()) else {
            continue;
        };
        if path.extension().and_then(|e| e.to_str()) != Some("json") || stem.len() != 32 {
            continue;
        }
        match load(stem) {
            Ok(record) => {
                if wanted == "all" || s(&record, "state") == wanted {
                    records.push(record);
                }
            }
            Err(error) => problems.push(json!({"path":path,"error":error.value()})),
        }
    }
    records.sort_by(|a, b| number(b, "created").total_cmp(&number(a, "created")));
    let total = records.len();
    records.truncate(limit);
    Ok(json!({"handoffs":records,"count":total,"truncated":total > limit,"problems":problems}))
}

/// Waits until the handoff is no longer waiting, at most `seconds`. A
/// completed handoff with a session answers a fresh observation of it.
pub fn wait(a: &Value) -> Result {
    let id = s(a, "id");
    let seconds = a["seconds"].as_u64().unwrap_or(30).clamp(1, 60);
    let deadline = Instant::now() + Duration::from_secs(seconds);
    let record = loop {
        check_cancelled()?;
        let record = load(id)?;
        if s(&record, "state") != "waiting" || Instant::now() >= deadline {
            break record;
        }
        thread::sleep(Duration::from_millis(200));
    };
    let mut answer = json!({"handoff":record,"state":record["state"],"waited_seconds":seconds.min(1 + (Instant::now() + Duration::from_secs(seconds) - deadline).as_secs())});
    if s(&record, "state") == "completed" {
        let resume = resume(&record);
        answer["resume"] = resume;
        if !s(&record, "session").is_empty() {
            match crate::runtime::handle("runtime.observe", &json!({"session":record["session"]})) {
                Ok(seen) => answer["observation"] = seen,
                Err(error) => answer["observation_error"] = error.value(),
            }
        }
    } else if s(&record, "state") == "waiting" {
        answer["note"] = json!(
            "Still waiting for the person; call handoff.wait again. Do not act on the device meanwhile."
        );
    }
    Ok(answer)
}

/// The person says it is done. Agents over MCP cannot say so for them.
pub fn complete(a: &Value) -> Result {
    let caller = crate::guard::current();
    if caller.channel == Some(crate::guard::Channel::Mcp) {
        return Err(Error::new(
            "handoff_person_only",
            "Only the person completes a handoff: from the Tomacco desktop's banner or their terminal",
        )
        .remedy("Wait with handoff.wait until the person has acted; do not complete it yourself")
        .details(json!({"id":a["id"],"client":caller.client})));
    }
    let id = s(a, "id");
    let note = text(a, "note", 500, false)?;
    let _lock = lock()?;
    let mut record = load(id)?;
    match s(&record, "state") {
        "waiting" => {
            record["state"] = json!("completed");
            record["updated"] = json!(now());
            record["completed"] = json!({"at":now(),"by":if caller.channel == Some(crate::guard::Channel::Cli) {"cli"} else {"desktop"},"note":note});
            atomic_json(&record_path(id)?, &record)?;
            Ok(json!({"handoff":record,"already":false,"resume":resume(&record)}))
        }
        "completed" => Ok(json!({"handoff":record,"already":true,"resume":resume(&record)})),
        _ => Err(not_waiting(&record)),
    }
}

pub fn cancel(a: &Value) -> Result {
    let id = s(a, "id");
    let reason = text(a, "reason", 500, false)?;
    let _lock = lock()?;
    let mut record = load(id)?;
    match s(&record, "state") {
        "waiting" => {
            record["state"] = json!("cancelled");
            record["updated"] = json!(now());
            record["cancelled"] =
                json!({"at":now(),"reason":reason,"by":crate::guard::current().client});
            atomic_json(&record_path(id)?, &record)?;
            Ok(json!({"handoff":record,"already":false}))
        }
        "cancelled" => Ok(json!({"handoff":record,"already":true})),
        _ => Err(not_waiting(&record)),
    }
}

pub fn handle(op: &str, a: &Value) -> Result {
    match op {
        "handoff.request" => request(a),
        "handoff.get" => get(a),
        "handoff.list" => list(a),
        "handoff.wait" => wait(a),
        "handoff.complete" => complete(a),
        "handoff.cancel" => cancel(a),
        _ => Err(Error::new("operation_not_found", op)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch() -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        TEST_ROOT.with(|r| *r.borrow_mut() = Some(dir.path().join("handoffs")));
        dir
    }

    #[test]
    fn a_request_waits_until_the_person_completes_it_and_then_points_at_a_fresh_look() {
        let _dir = scratch();
        let made = request(&json!({"reason":"Confirm the purchase with Face ID","kind":"purchase","instructions":"Look at the phone"})).unwrap();
        let id = s(&made, "id").to_string();
        assert_eq!(made["state"], "waiting");
        assert_eq!(made["wait_with"], "handoff.wait");
        assert!(s(&made, "note").contains("Do not type"));
        let listed = list(&json!({})).unwrap();
        assert_eq!(listed["count"], 1);
        assert_eq!(listed["handoffs"][0]["id"], id);
        let still = wait(&json!({"id":id,"seconds":1})).unwrap();
        assert_eq!(still["state"], "waiting");
        assert!(s(&still, "note").contains("Still waiting"));
        let done = complete(&json!({"id":id,"note":"paid"})).unwrap();
        assert_eq!(done["already"], false);
        assert_eq!(done["handoff"]["completed"]["note"], "paid");
        assert_eq!(done["resume"]["observe_first"], true);
        let again = complete(&json!({"id":id})).unwrap();
        assert_eq!(again["already"], true);
        let after = wait(&json!({"id":id,"seconds":1})).unwrap();
        assert_eq!(after["state"], "completed");
        assert!(after.get("observation").is_none(), "no session was named");
        assert_eq!(list(&json!({"state":"waiting"})).unwrap()["count"], 0);
        assert_eq!(list(&json!({"state":"all"})).unwrap()["count"], 1);
        assert_eq!(
            cancel(&json!({"id":id})).unwrap_err().code,
            "handoff_not_waiting"
        );
    }

    #[test]
    fn cancellation_expiry_and_bad_requests() {
        let _dir = scratch();
        assert_eq!(
            request(&json!({"reason":""})).unwrap_err().code,
            "invalid_argument"
        );
        assert_eq!(
            request(&json!({"reason":"x","kind":"dance"}))
                .unwrap_err()
                .code,
            "invalid_argument"
        );
        assert_eq!(
            request(&json!({"reason":"x","session":"nope"}))
                .unwrap_err()
                .code,
            "invalid_argument"
        );
        let made = request(&json!({"reason":"Unlock the phone","kind":"passcode"})).unwrap();
        let id = s(&made, "id").to_string();
        let gone = cancel(&json!({"id":id,"reason":"not needed"})).unwrap();
        assert_eq!(gone["handoff"]["state"], "cancelled");
        assert_eq!(
            complete(&json!({"id":id})).unwrap_err().code,
            "handoff_not_waiting"
        );
        assert_eq!(
            get(&json!({"id":"0".repeat(32)})).unwrap_err().code,
            "handoff_not_found"
        );
        // A request past its time expires when it is next read.
        let late =
            request(&json!({"reason":"Sign in","kind":"sign_in","timeout_seconds":60})).unwrap();
        let late_id = s(&late, "id").to_string();
        let path = record_path(&late_id).unwrap();
        let mut record = read_json(&path, None).unwrap();
        record["expires_at"] = json!(now() - 1.0);
        atomic_json(&path, &record).unwrap();
        assert_eq!(get(&json!({"id":late_id})).unwrap()["state"], "expired");
        assert_eq!(
            complete(&json!({"id":late_id})).unwrap_err().code,
            "handoff_not_waiting"
        );
    }
}
