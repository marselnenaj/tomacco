//! The request ledger: a durable record of each mutating call an agent gave
//! a `request_id`, so a call repeated after a lost reply, a killed client or
//! a restarted device service is answered from what is known instead of
//! being sent again.
//!
//! Before anything is dispatched, the process that executes the call
//! commits the identity, a digest of the normalised arguments (the device
//! as its canonical target, never the session or token), the route,
//! transport and executing instance, and `pending`; the commit reaches the
//! disk first (synchronous=FULL). Inputs the call hands to a runtime
//! worker are recorded by that worker as children (`ID/1`, `ID/step-2`),
//! with the worker as their executor, so an input the device service
//! accepted is on record even when its reply never reached the caller.
//!
//! A repeated identity with the same intent gets the recorded answer and
//! `retained: true`; with other arguments, `request_conflict`. A refusal
//! that provably sent nothing (a reservation, a stale frame, a malformed
//! step) is not retained, so the same identity may be tried again. A
//! record whose executor is gone before it recorded an answer becomes
//! `unknown`: completion cannot be proven, the request is never resent,
//! through the same transport or another, and the answer says to observe.
//! Nothing here makes physical execution exactly-once: a transport that
//! loses an answer leaves the effect unknown, and only a fresh observation
//! can tell.
//!
//! Records hold no typed text and no token: the arguments only as a
//! digest, a summary of action names and lengths, and answers with secrets
//! masked and `text` replaced by its length. They are kept 24 hours and at
//! most `ROWS_MAX` of them; the file is the owner's alone.
use super::*;

const SCHEMA_VERSION: i64 = 1;
const SCHEMA: &str = "CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, parent TEXT, operation TEXT NOT NULL, intent TEXT NOT NULL, device TEXT NOT NULL, route TEXT NOT NULL, transport TEXT NOT NULL, instance TEXT NOT NULL, pid INTEGER NOT NULL, token TEXT NOT NULL, state TEXT NOT NULL, status TEXT, outcome TEXT, summary TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL); \
CREATE INDEX IF NOT EXISTS requests_parent ON requests(parent); \
CREATE INDEX IF NOT EXISTS requests_updated ON requests(updated);";
const RETENTION_SECONDS: f64 = 86_400.0;
const ROWS_MAX: i64 = 20_000;
const OUTCOME_LIMIT: usize = 64 * 1024;
/// A request identity an agent chooses.
const ID_PATTERN: &str = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}";

/// Error codes that prove nothing was dispatched: a retry under the same
/// identity runs again.
const REFUSED: &[&str] = &[
    "invalid_argument",
    "device_reserved",
    "device_input_in_flight",
    "reservation_expired",
    "reservation_revoked",
    "reservation_released",
    "reservation_not_held",
    "device_target_unknown",
    "runtime_unavailable",
    "runtime_busy",
    "frame_stale",
    "mark_not_found",
    "mark_stale",
    "focus_timeout",
    "focus_unobservable",
    "off_screen",
    "invalid_bounds",
    "ui_hierarchy_unavailable",
    "ambiguous_selector",
    "element_disabled",
    "instrumentation_required",
    "invalid_key",
    "policy_denied",
    "approval_denied",
    "path_outside_roots",
    "operation_not_found",
];
/// Error codes of an answer that was lost on the way: the input may or may
/// not have reached the device.
const LOST: &[&str] = &[
    "runtime_timeout",
    "runtime_protocol",
    "runtime_stopped",
    "io_error",
    "invalid_json",
];
/// Operations whose request is one input of the runtime worker: their
/// worker's record of that input is their outcome.
const SINGLE_INPUT: &[&str] = &["runtime.input", "runtime.gesture", "screen.tap"];

thread_local! {
    static LEDGER: RefCell<Option<(PathBuf, Connection)>> = const { RefCell::new(None) };
}

fn with_ledger<T>(action: impl FnOnce(&Connection) -> Result<T>) -> Result<T> {
    let root = home()?;
    LEDGER.with(|cell| {
        let mut slot = cell.borrow_mut();
        if slot.as_ref().is_none_or(|(at, _)| at != &root) {
            *slot = Some((
                root.clone(),
                open_database("requests.sqlite3", SCHEMA, SCHEMA_VERSION, true)?,
            ));
        }
        action(&slot.as_ref().unwrap().1)
    })
}

/// A request identity as an agent gives it.
pub fn checked_id(id: &str) -> Result<&str> {
    identifier(id, "request_id", Some(ID_PATTERN))
}

/// `value` with its object keys sorted, at every depth.
fn canonical(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            let mut out = serde_json::Map::new();
            for key in keys {
                out.insert(key.clone(), canonical(&map[key]));
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(items.iter().map(canonical).collect()),
        other => other.clone(),
    }
}

/// The intent of a call: a digest of its operation and its arguments
/// without the session, device, token and identity (the canonical device is
/// compared on its own); and a summary that names actions and counts
/// characters but holds no text.
pub fn intent(operation: &str, a: &Value) -> (String, Value) {
    let mut kept = a.clone();
    if let Some(map) = kept.as_object_mut() {
        for key in [
            "session",
            "device",
            "reservation_token",
            "request_id",
            "job_id",
            "width",
            "height",
        ] {
            map.remove(key);
        }
    }
    let body = json!({"operation":operation,"arguments":canonical(&kept)});
    let intent = digest(body.to_string().as_bytes());
    let mut summary = json!({"operation":operation});
    for key in ["action", "phase", "key", "mark", "orientation"] {
        if let Some(v) = a.get(key) {
            summary[key] = v.clone();
        }
    }
    if let Some(text) = a["text"].as_str() {
        summary["text_characters"] = json!(text.chars().count());
    }
    if let Some(steps) = a["steps"].as_array() {
        summary["steps"] = json!(
            steps
                .iter()
                .map(|step| s(step, "action"))
                .collect::<Vec<_>>()
        );
    }
    if let Some(contacts) = a["contacts"].as_array() {
        summary["contacts"] = json!(contacts.len());
    }
    (intent, summary)
}

/// An answer as the ledger keeps it: secrets masked, every `text` replaced
/// by its length, and at most `OUTCOME_LIMIT` bytes.
fn scrub(value: &Value) -> Value {
    fn walk(value: &mut Value) {
        match value {
            Value::Array(items) => items.iter_mut().for_each(walk),
            Value::Object(map) => {
                for (key, item) in map.iter_mut() {
                    match item {
                        Value::String(text) if key == "text" || key == "value" => {
                            *item = json!(format!("[{} characters]", text.chars().count()))
                        }
                        _ => walk(item),
                    }
                }
            }
            _ => (),
        }
    }
    let mut kept = value.clone();
    crate::redact::value(&mut kept);
    walk(&mut kept);
    let size = kept.to_string().len();
    if size > OUTCOME_LIMIT {
        let keys: Vec<String> = kept
            .as_object()
            .map(|m| m.keys().cloned().collect())
            .unwrap_or_default();
        return json!({"truncated":true,"bytes":size,"keys":keys});
    }
    kept
}

fn batch_sent_nothing(error: &Error) -> bool {
    let Some(details) = error.details.as_ref() else {
        return false;
    };
    if details["applied_steps"]
        .as_array()
        .is_none_or(|applied| !applied.is_empty())
    {
        return false;
    }
    // The last step ran and failed: it sent nothing only if it refused.
    match details["steps"].as_array().and_then(|steps| steps.last()) {
        Some(step) if s(step, "status") == "failed" && step["mutated"] == true => {
            REFUSED.contains(&s(&step["error"], "code"))
        }
        _ => true,
    }
}

/// How a call ended, for the record: `applied` (the transport took it),
/// `refused` (provably not dispatched), `unknown` (its answer was lost) or
/// `failed` (an answer that is an error; what reached the device is in it).
pub fn classify(operation: &str, result: &Result) -> (&'static str, Value) {
    match result {
        Ok(value) => {
            let status = if operation == "runtime.actions"
                && n(value, "verified") > 0
                && n(value, "verified") >= n(value, "applied")
            {
                "verified"
            } else {
                "applied"
            };
            (status, json!({"ok":true,"result":scrub(value)}))
        }
        Err(error) => {
            let code = error.code.as_str();
            let batch = operation == "runtime.actions"
                && error
                    .details
                    .as_ref()
                    .is_some_and(|d| d.get("applied_steps").is_some());
            let status = if batch {
                if batch_sent_nothing(error) {
                    "refused"
                } else {
                    "failed"
                }
            } else if REFUSED.contains(&code) {
                "refused"
            } else if LOST.contains(&code) {
                "unknown"
            } else {
                "failed"
            };
            (status, json!({"ok":false,"error":scrub(&error.value())}))
        }
    }
}

#[derive(Clone, Debug)]
struct Row {
    id: String,
    parent: Option<String>,
    operation: String,
    intent: String,
    device: String,
    route: String,
    transport: String,
    instance: String,
    pid: i64,
    token: String,
    state: String,
    status: Option<String>,
    outcome: Value,
    summary: Value,
    created: f64,
    updated: f64,
}

const COLUMNS: &str = "id, parent, operation, intent, device, route, transport, instance, pid, token, state, status, outcome, summary, created, updated";

fn row_from(r: &rusqlite::Row) -> rusqlite::Result<Row> {
    let parse = |text: Option<String>| {
        text.and_then(|t| serde_json::from_str(&t).ok())
            .unwrap_or(Value::Null)
    };
    Ok(Row {
        id: r.get(0)?,
        parent: r.get(1)?,
        operation: r.get(2)?,
        intent: r.get(3)?,
        device: r.get(4)?,
        route: r.get(5)?,
        transport: r.get(6)?,
        instance: r.get(7)?,
        pid: r.get(8)?,
        token: r.get(9)?,
        state: r.get(10)?,
        status: r.get(11)?,
        outcome: parse(r.get(12)?),
        summary: parse(r.get(13)?),
        created: r.get(14)?,
        updated: r.get(15)?,
    })
}

fn load(db: &Connection, id: &str) -> Result<Option<Row>> {
    Ok(db
        .query_row(
            &format!("SELECT {COLUMNS} FROM requests WHERE id=?1"),
            params![id],
            row_from,
        )
        .optional()?)
}

fn children(db: &Connection, id: &str) -> Result<Vec<Row>> {
    let mut statement = db.prepare_cached(&format!(
        "SELECT {COLUMNS} FROM requests WHERE parent=?1 ORDER BY created, id"
    ))?;
    let rows = statement
        .query_map(params![id], row_from)?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    Ok(rows)
}

impl Row {
    fn code(&self) -> Value {
        self.outcome["error"]["code"].clone()
    }
    fn brief(&self) -> Value {
        json!({"id":self.id,"state":self.state,"status":self.status,"summary":self.summary,"transport":self.transport,
            "instance":self.instance,"code":self.code(),"updated_at":self.updated})
    }
    fn public(&self, children: &[Row]) -> Value {
        json!({"id":self.id,"parent":self.parent,"operation":self.operation,"device":self.device,"route":self.route,"transport":self.transport,
            "instance":self.instance,"state":self.state,"status":self.status,"summary":self.summary,
            "error":if self.outcome["ok"] == false { json!({"code":self.outcome["error"]["code"],"message":self.outcome["error"]["message"]}) } else { Value::Null },
            "reconciled_from":self.outcome.get("reconciled_from").cloned().unwrap_or(Value::Null),
            "created_at":self.created,"updated_at":self.updated,
            "children":children.iter().map(Row::brief).collect::<Vec<_>>()})
    }
}

/// How a request reaches the device: the operation, its canonical target,
/// the route (`runtime-worker`, `adb`, `virtual-iphone`), the transport and
/// the instance that executes it.
pub struct Dispatch<'a> {
    pub operation: &'a str,
    pub target: &'a Target,
    pub route: &'a str,
    pub transport: String,
    pub instance: String,
}

enum Begin {
    Proceed,
    Answer(Result),
}

/// This process as an executor: its PID and start time.
pub fn executor() -> (i64, String) {
    let pid = std::process::id();
    (i64::from(pid), process_token(pid))
}

/// `pid:PID:START`, the instance a call or worker executes as.
pub fn instance() -> String {
    let (pid, token) = executor();
    format!("pid:{pid}:{token}")
}

fn prune(db: &Connection, at: f64) -> Result<()> {
    db.execute(
        "DELETE FROM requests WHERE state!='pending' AND updated<?1",
        params![at - RETENTION_SECONDS],
    )?;
    let stale: Vec<(String, i64, String)> = {
        let mut statement = db.prepare_cached(
            "SELECT id, pid, token FROM requests WHERE state='pending' AND updated<?1",
        )?;
        statement
            .query_map(params![at - RETENTION_SECONDS], |r| {
                Ok((r.get(0)?, r.get(1)?, r.get(2)?))
            })?
            .collect::<std::result::Result<_, _>>()?
    };
    for (id, pid, token) in stale {
        if !alive(pid, &token) {
            db.execute("DELETE FROM requests WHERE id=?1", params![id])?;
        }
    }
    let count: i64 = db.query_row("SELECT COUNT(*) FROM requests", [], |r| r.get(0))?;
    if count > ROWS_MAX {
        db.execute(
            "DELETE FROM requests WHERE id IN (SELECT id FROM requests WHERE state!='pending' ORDER BY updated LIMIT ?1)",
            params![count - ROWS_MAX],
        )?;
    }
    Ok(())
}

fn write_state(db: &Connection, row: &Row) -> Result<()> {
    db.execute(
        "UPDATE requests SET state=?1, status=?2, outcome=?3, updated=?4 WHERE id=?5",
        params![
            row.state,
            row.status,
            row.outcome.to_string(),
            row.updated,
            row.id
        ],
    )?;
    Ok(())
}

/// Settles a record whose executor is gone: a pending one becomes
/// `unknown`, unless it was one worker input whose worker recorded how it
/// ended. Children are settled first.
fn settle(db: &Connection, mut row: Row, at: f64) -> Result<(Row, Vec<Row>)> {
    let mut kids = Vec::new();
    for child in children(db, &row.id)? {
        kids.push(settle(db, child, at)?.0);
    }
    if row.state == "pending" && !alive(row.pid, &row.token) {
        row.state = "unknown".into();
        row.status = Some("unknown".into());
        row.outcome = json!({"ok":false,"error":{"code":"request_outcome_unknown",
            "message":"The process that executed this request ended before it recorded an answer"}});
        row.updated = at;
        write_state(db, &row)?;
    }
    if row.state == "unknown" && SINGLE_INPUT.contains(&row.operation.as_str()) {
        if let [child] = kids.as_slice() {
            if child.state == "completed" {
                row.state = "completed".into();
                row.status = child.status.clone();
                row.outcome = child.outcome.clone();
                row.outcome["reconciled_from"] = json!("the device service's record of the input");
                row.updated = at;
                write_state(db, &row)?;
            }
        }
    }
    Ok((row, kids))
}

fn conflict(row: &Row, mismatch: &[&str]) -> Error {
    Error::new(
        "request_conflict",
        format!(
            "request_id {} was already used for another request ({} differ); nothing was sent",
            row.id,
            mismatch.join(", ")
        ),
    )
    .remedy("Use a new request_id for a different action; repeat an identity only to ask again about the same action")
    .details(json!({"request":{"id":row.id,"operation":row.operation,"device":row.device,"state":row.state,
        "status":row.status,"created_at":row.created},"differs":mismatch}))
}

fn advice(row: &Row) -> &'static str {
    match (row.state.as_str(), row.status.as_deref()) {
        ("pending", _) => {
            "Still executing; ask again with device.request.get, do not send it again"
        }
        ("completed", Some("refused")) => {
            "Nothing was sent; the same request_id may be tried again"
        }
        ("completed", Some("applied" | "verified")) => {
            "The transport took it; observe the screen to see its effect before acting again"
        }
        ("completed", Some("failed")) => {
            "It ran and failed; its error says what reached the device. Observe before deciding, and use a new request_id for a new attempt"
        }
        _ => {
            "Whether it reached the device is unknown. Observe the screen (runtime.observe, screen.marks) and decide from what is there; never resend it blindly, and use a new request_id for a new attempt"
        }
    }
}

/// The answer for a request that was already recorded.
fn retained(row: &Row, kids: &[Row]) -> Result {
    let mut record = row.public(kids);
    record["retained"] = json!(true);
    record["replayed"] = json!(false);
    record["advice"] = json!(advice(row));
    match (row.state.as_str(), row.status.as_deref()) {
        ("completed", Some("applied" | "verified")) => {
            let mut result = row.outcome["result"].clone();
            if !result.is_object() {
                result = json!({"result":result});
            }
            result["request"] = record;
            Ok(result)
        }
        ("completed", Some("failed" | "refused")) => {
            let error = &row.outcome["error"];
            let mut details = error["details"].clone();
            if !details.is_object() {
                details = json!({"details":details});
            }
            details["request"] = record;
            Err(Error::new(s(error, "code"), format!("{} (recorded answer of request {})", s(error, "message"), row.id))
                .remedy(advice(row))
                .details(details))
        }
        _ => Err(Error::new(
            "request_outcome_unknown",
            format!("Request {} was dispatched, and whether it took effect is unknown; it was not sent again", row.id),
        )
        .remedy(advice(row))
        .details(json!({"request":record,"recorded_error":row.outcome["error"]}))),
    }
}

fn begin(
    db: &Connection,
    id: &str,
    parent: Option<&str>,
    d: &Dispatch,
    intent: &str,
    summary: &Value,
) -> Result<Begin> {
    transaction(db, |db| {
        let at = now();
        if parent.is_none() {
            prune(db, at)?;
        }
        let (pid, token) = executor();
        let Some(row) = load(db, id)? else {
            db.execute(
                &format!("INSERT INTO requests ({COLUMNS}) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, 'pending', NULL, NULL, ?11, ?12, ?12)"),
                params![id, parent, d.operation, intent, d.target.key, d.route, d.transport, d.instance, pid, token, summary.to_string(), at],
            )?;
            return Ok(Begin::Proceed);
        };
        let mut mismatch = Vec::new();
        if row.operation != d.operation {
            mismatch.push("operation");
        }
        if row.device != d.target.key {
            mismatch.push("device");
        }
        if row.intent != intent {
            mismatch.push("arguments");
        }
        if !mismatch.is_empty() {
            return Err(conflict(&row, &mismatch));
        }
        if row.state == "completed" && row.status.as_deref() == Some("refused") {
            // It sent nothing: this attempt takes the record over.
            db.execute(
                "UPDATE requests SET state='pending', status=NULL, outcome=NULL, route=?1, transport=?2, instance=?3, pid=?4, token=?5, updated=?6 WHERE id=?7",
                params![d.route, d.transport, d.instance, pid, token, at, id],
            )?;
            db.execute("DELETE FROM requests WHERE parent=?1", params![id])?;
            return Ok(Begin::Proceed);
        }
        if row.state == "pending" && alive(row.pid, &row.token) {
            let (row, kids) = settle(db, row, at)?;
            let mut record = row.public(&kids);
            record["advice"] = json!(advice(&row));
            return Err(Error::new(
                "request_in_progress",
                format!(
                    "Request {id} is still executing in another process; it was not sent again"
                ),
            )
            .remedy("Wait, then ask with device.request.get; do not resend it")
            .details(json!({"request":record})));
        }
        let (row, kids) = settle(db, row, at)?;
        Ok(Begin::Answer(retained(&row, &kids)))
    })
}

fn finish(db: &Connection, id: &str, operation: &str, result: &Result) -> Result<&'static str> {
    let (status, outcome) = classify(operation, result);
    let state = if status == "unknown" {
        "unknown"
    } else {
        "completed"
    };
    let (pid, token) = executor();
    db.execute(
        "UPDATE requests SET state=?1, status=?2, outcome=?3, updated=?4 WHERE id=?5 AND pid=?6 AND token=?7",
        params![state, status, outcome.to_string(), now(), id, pid, token],
    )?;
    Ok(status)
}

fn attach(result: Result, id: &str, status: Option<&str>) -> Result {
    let record = json!({"id":id,"state":match status { Some("unknown") => "unknown", Some(_) => "completed", None => "unrecorded" },
        "status":status,"retained":false});
    match result {
        Ok(mut value) => {
            if value.is_object() {
                value["request"] = record;
            }
            Ok(value)
        }
        Err(mut error) => {
            let mut details = error
                .details
                .take()
                .map(|d| *d)
                .unwrap_or_else(|| json!({}));
            if !details.is_object() {
                details = json!({"details":details});
            }
            details["request"] = record;
            Err(error.details(details))
        }
    }
}

/// Executes a call that carries `request_id` once: committed before
/// `execute` dispatches anything, answered from the record when repeated.
/// The inputs `execute` hands to a runtime worker become the record's
/// children.
pub fn run(id: &str, d: &Dispatch, a: &Value, execute: impl FnOnce() -> Result) -> Result {
    checked_id(id)?;
    let (intent, summary) = intent(d.operation, a);
    if let Begin::Answer(answer) = with_ledger(|db| begin(db, id, None, d, &intent, &summary))? {
        return answer;
    }
    let context = Context {
        token: super::context().token,
        parent: Some(id.to_string()),
        label: None,
        counter: 0,
    };
    let result = with_context(context, execute);
    let status = with_ledger(|db| finish(db, id, d.operation, &result)).ok();
    attach(result, id, status)
}

/// The child identity a runtime worker was handed: `PARENT/LABEL`.
fn checked_child(id: &str) -> Result<&str> {
    identifier(
        id,
        "request_id",
        Some(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}/[A-Za-z0-9][A-Za-z0-9_.-]{0,39}"),
    )
}

/// Executes one input in the runtime worker under its child identity (none
/// runs it unrecorded): recorded before the device gets it, and never run
/// twice under one identity.
pub fn run_child(id: &str, d: &Dispatch, a: &Value, execute: impl FnOnce() -> Result) -> Result {
    if id.is_empty() {
        return execute();
    }
    checked_child(id)?;
    let parent = id.rsplit_once('/').map(|(p, _)| p);
    let (intent, summary) = intent(d.operation, a);
    if let Begin::Answer(answer) = with_ledger(|db| begin(db, id, parent, d, &intent, &summary))? {
        return answer;
    }
    let result = execute();
    let _ = with_ledger(|db| finish(db, id, d.operation, &result));
    result
}

/// `device.request.get`: one request with its children, settled, and what
/// to do next.
pub fn get(a: &Value) -> Result {
    let id = s(a, "request_id");
    if id.contains('/') {
        checked_child(id)?;
    } else {
        checked_id(id)?;
    }
    with_ledger(|db| {
        transaction(db, |db| {
            let row = load(db, id)?.ok_or_else(|| {
                Error::new("request_not_found", format!("No request {id} is recorded"))
                    .remedy("Requests are kept 24 hours; one never recorded was not sent through Tomacco with this request_id")
            })?;
            let (row, kids) = settle(db, row, now())?;
            let mut record = row.public(&kids);
            record["advice"] = json!(advice(&row));
            record["executor_alive"] = json!(row.state == "pending" && alive(row.pid, &row.token));
            Ok(json!({"request":record}))
        })
    })
}

/// `device.request.list`: the newest requests (not their children).
pub fn list(a: &Value) -> Result {
    let limit = a["limit"].as_u64().unwrap_or(50).clamp(1, 200) as i64;
    let device = match s(a, "device") {
        "" => None,
        _ => Some(target(s(a, "device"), s(a, "platform"), s(a, "remote"))?.key),
    };
    let state = s(a, "state").to_string();
    with_ledger(|db| {
        let mut statement = db.prepare(&format!(
            "SELECT {COLUMNS} FROM requests WHERE parent IS NULL AND (?1 IS NULL OR device=?1) AND (?2='' OR state=?2) ORDER BY created DESC LIMIT ?3"
        ))?;
        let rows = statement
            .query_map(params![device, state, limit], row_from)?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        let requests: Vec<Value> = rows
            .iter()
            .map(|row| {
                let mut brief = row.brief();
                brief["operation"] = json!(row.operation);
                brief["device"] = json!(row.device);
                brief["created_at"] = json!(row.created);
                brief
            })
            .collect();
        Ok(json!({"requests":requests,"count":requests.len(),
            "note":"A pending request whose process is gone shows as unknown once device.request.get settles it"}))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn database(dir: &Path) -> Connection {
        open_path(&dir.join("requests.sqlite3"), SCHEMA, SCHEMA_VERSION, true).unwrap()
    }
    fn dispatch<'a>(operation: &'a str, target: &'a Target) -> Dispatch<'a> {
        Dispatch {
            operation,
            target,
            route: "runtime-worker",
            transport: "fixture".into(),
            instance: instance(),
        }
    }
    fn start(
        db: &Connection,
        id: &str,
        parent: Option<&str>,
        d: &Dispatch,
        a: &Value,
    ) -> Result<Begin> {
        let (intent, summary) = intent(d.operation, a);
        begin(db, id, parent, d, &intent, &summary)
    }
    fn proceeds(begin: Result<Begin>) -> bool {
        matches!(begin, Ok(Begin::Proceed))
    }
    fn answer(begin: Result<Begin>) -> Result {
        match begin.unwrap() {
            Begin::Answer(answer) => answer,
            Begin::Proceed => panic!("it would have been sent again"),
        }
    }
    /// Makes a record look as if its executor had died.
    fn orphan(db: &Connection, id: &str) {
        db.execute(
            "UPDATE requests SET pid=999999999, token='gone' WHERE id=?1",
            params![id],
        )
        .unwrap();
    }

    #[test]
    fn the_same_intent_gets_the_recorded_answer_and_other_arguments_conflict() {
        let dir = tempfile::tempdir().unwrap();
        let db = database(dir.path());
        let pixel = Target::new("android", "", "fixture:login");
        let d = dispatch("runtime.input", &pixel);
        let tap = json!({"session":"a".repeat(32),"action":"tap","x":0.5,"y":0.5});
        assert!(proceeds(start(&db, "tap-1", None, &d, &tap)));
        // Concurrently, the same identity is refused while its executor (this process) lives.
        assert_eq!(
            start(&db, "tap-1", None, &d, &tap).err().unwrap().code,
            "request_in_progress"
        );
        finish(
            &db,
            "tap-1",
            "runtime.input",
            &Ok(json!({"delivered":true})),
        )
        .unwrap();
        // The same tap through another session of the same device is the same intent.
        let again = json!({"session":"b".repeat(32),"action":"tap","x":0.5,"y":0.5});
        let retained = answer(start(&db, "tap-1", None, &d, &again)).unwrap();
        assert_eq!(retained["delivered"], true);
        assert_eq!(retained["request"]["retained"], true);
        assert_eq!(retained["request"]["replayed"], false);
        assert_eq!(retained["request"]["status"], "applied");
        // Another point, operation or device under the same identity conflicts.
        let moved = json!({"session":"a".repeat(32),"action":"tap","x":0.6,"y":0.5});
        let error = start(&db, "tap-1", None, &d, &moved).err().unwrap();
        assert_eq!(error.code, "request_conflict");
        assert_eq!(error.details.unwrap()["differs"], json!(["arguments"]));
        let elsewhere = Target::new("android", "", "emulator-5554");
        let error = start(
            &db,
            "tap-1",
            None,
            &dispatch("runtime.input", &elsewhere),
            &tap,
        )
        .err()
        .unwrap();
        assert_eq!(error.details.unwrap()["differs"], json!(["device"]));
    }

    #[test]
    fn a_refusal_may_be_tried_again_and_a_lost_executor_is_unknown_not_resent() {
        let dir = tempfile::tempdir().unwrap();
        let db = database(dir.path());
        let pixel = Target::new("android", "", "fixture:login");
        let d = dispatch("runtime.input", &pixel);
        let key = json!({"action":"key","key":"enter"});
        assert!(proceeds(start(&db, "k", None, &d, &key)));
        let refused: Result = Err(Error::new("device_reserved", "held"));
        assert_eq!(
            finish(&db, "k", "runtime.input", &refused).unwrap(),
            "refused"
        );
        assert!(
            proceeds(start(&db, "k", None, &d, &key)),
            "nothing was sent, so it runs again"
        );
        // This time its process dies after dispatching.
        orphan(&db, "k");
        let error = answer(start(&db, "k", None, &d, &key)).unwrap_err();
        assert_eq!(error.code, "request_outcome_unknown");
        let details = error.details.unwrap();
        assert_eq!(details["request"]["state"], "unknown");
        assert!(s(&details["request"], "advice").contains("never resend"));
        // And stays unknown on every later ask.
        assert_eq!(
            answer(start(&db, "k", None, &d, &key)).unwrap_err().code,
            "request_outcome_unknown"
        );
    }

    #[test]
    fn the_device_services_record_answers_for_a_reply_that_never_arrived() {
        let dir = tempfile::tempdir().unwrap();
        let db = database(dir.path());
        let pixel = Target::new("android", "", "fixture:login");
        let d = dispatch("runtime.input", &pixel);
        let text = json!({"action":"text","text":"correct horse battery staple"});
        assert!(proceeds(start(&db, "t", None, &d, &text)));
        // The worker recorded its child, then the caller died before the answer arrived.
        assert!(proceeds(start(&db, "t/1", Some("t"), &d, &text)));
        finish(
            &db,
            "t/1",
            "runtime.input",
            &Ok(json!({"delivered":true,"text":"correct horse battery staple"})),
        )
        .unwrap();
        // The worker never runs the child twice.
        assert_eq!(
            answer(start(&db, "t/1", Some("t"), &d, &text)).unwrap()["request"]["retained"],
            true
        );
        orphan(&db, "t");
        let retained = answer(start(&db, "t", None, &d, &text)).unwrap();
        assert_eq!(retained["request"]["status"], "applied");
        assert!(s(&retained["request"], "reconciled_from").contains("device service"));
        // Nothing typed is kept: the summary counts characters, the answer masks text.
        let all: String = db
            .query_row(
                "SELECT group_concat(summary || outcome || intent, '') FROM requests",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(!all.contains("horse"), "{all}");
        assert!(all.contains("text_characters"));
        // A batch whose caller died midway is unknown, with its steps' facts.
        let batch =
            json!({"steps":[{"action":"tap","x":0.1,"y":0.1},{"action":"key","key":"enter"}]});
        let b = dispatch("runtime.actions", &pixel);
        assert!(proceeds(start(&db, "b", None, &b, &batch)));
        assert!(proceeds(start(
            &db,
            "b/step-0",
            Some("b"),
            &d,
            &json!({"action":"tap","x":0.1,"y":0.1})
        )));
        finish(
            &db,
            "b/step-0",
            "runtime.input",
            &Ok(json!({"delivered":true})),
        )
        .unwrap();
        assert!(proceeds(start(
            &db,
            "b/step-1",
            Some("b"),
            &d,
            &json!({"action":"key","key":"enter"})
        )));
        orphan(&db, "b/step-1");
        orphan(&db, "b");
        let error = answer(start(&db, "b", None, &b, &batch)).unwrap_err();
        assert_eq!(error.code, "request_outcome_unknown");
        let steps = &error.details.unwrap()["request"]["children"];
        assert_eq!(steps[0]["status"], "applied");
        assert_eq!(steps[1]["state"], "unknown");
    }

    #[test]
    fn outcomes_keep_refused_failed_unknown_and_verified_apart() {
        let refused_batch = Error::new("focus_timeout", "x").details(json!({"applied_steps":[],"steps":[{"status":"failed","mutated":true,"error":{"code":"focus_timeout"}}]}));
        assert_eq!(
            classify("runtime.actions", &Err(refused_batch)).0,
            "refused"
        );
        let partial = Error::new("focus_timeout", "x").details(json!({"applied_steps":[0],"steps":[{"status":"applied","mutated":true},{"status":"failed","mutated":true,"error":{"code":"focus_timeout"}}]}));
        assert_eq!(classify("runtime.actions", &Err(partial)).0, "failed");
        let sent = Error::new("actions_step_failed", "x").details(json!({"applied_steps":[],"steps":[{"status":"failed","mutated":true,"error":{"code":"runtime_timeout"}}]}));
        assert_eq!(classify("runtime.actions", &Err(sent)).0, "failed");
        assert_eq!(
            classify("runtime.input", &Err(Error::new("runtime_timeout", "x"))).0,
            "unknown"
        );
        assert_eq!(
            classify("runtime.input", &Err(Error::new("mark_stale", "x"))).0,
            "refused"
        );
        assert_eq!(
            classify("runtime.input", &Err(Error::new("unsupported_input", "x"))).0,
            "failed"
        );
        assert_eq!(
            classify("runtime.actions", &Ok(json!({"applied":2,"verified":2}))).0,
            "verified"
        );
        assert_eq!(
            classify("runtime.actions", &Ok(json!({"applied":2,"verified":1}))).0,
            "applied"
        );
        assert!(checked_id("order-42.retry:1").is_ok());
        assert!(checked_id("a/b").is_err() && checked_id("").is_err());
        assert!(checked_child("order-42/step-3").is_ok() && checked_child("order-42").is_err());
    }

    #[test]
    fn retention_and_size_are_bounded() {
        let dir = tempfile::tempdir().unwrap();
        let db = database(dir.path());
        let pixel = Target::new("android", "", "p");
        let d = dispatch("runtime.input", &pixel);
        let key = json!({"action":"key","key":"home"});
        assert!(proceeds(start(&db, "old", None, &d, &key)));
        finish(&db, "old", "runtime.input", &Ok(json!({}))).unwrap();
        db.execute(
            "UPDATE requests SET updated=updated-?1",
            params![RETENTION_SECONDS + 10.0],
        )
        .unwrap();
        assert!(proceeds(start(&db, "new", None, &d, &key)));
        assert!(load(&db, "old").unwrap().is_none());
        let huge = json!({"blob":"x".repeat(OUTCOME_LIMIT * 2)});
        let (_, kept) = classify("runtime.input", &Ok(huge));
        assert_eq!(kept["result"]["truncated"], true);
        assert!(kept.to_string().len() < 1024);
    }
}
