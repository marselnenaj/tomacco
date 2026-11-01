use crate::{core::*, toolchain};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    fs::{self, File},
    io::{Read, Seek, SeekFrom, Write},
    os::unix::fs::MetadataExt,
    os::unix::process::CommandExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};

pub const TERMINAL: &[&str] = &["succeeded", "failed", "cancelled", "timed_out"];
const LOG_LIMIT: u64 = 32 * 1024 * 1024;
/// Register an already-created private directory for worker-owned cleanup.
/// Inode identity prevents recovery from deleting a replacement at the same path.
pub fn temporary_directory(directory: &Path) -> Result<Value> {
    let metadata = fs::symlink_metadata(directory)?;
    if !metadata.is_dir() || metadata.mode() & 0o077 != 0 {
        return Err(Error::new(
            "invalid_temporary_directory",
            "Temporary directories must be private",
        ));
    }
    Ok(
        json!({"path":directory,"device":metadata.dev(),"inode":metadata.ino(),"owner":metadata.uid()}),
    )
}
fn remove_temporary_directories(spec: &Value) -> Vec<Value> {
    let mut errors = Vec::new();
    for entry in spec["temporary_directories"]
        .as_array()
        .into_iter()
        .flatten()
    {
        let result = (|| -> Result<()> {
            let path = Path::new(s(entry, "path"));
            let metadata = match fs::symlink_metadata(path) {
                Ok(value) => value,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
                Err(error) => return Err(error.into()),
            };
            if !metadata.is_dir()
                || metadata.dev() != n(entry, "device")
                || metadata.ino() != n(entry, "inode")
                || u64::from(metadata.uid()) != n(entry, "owner")
                || metadata.mode() & 0o077 != 0
            {
                return Err(Error::new(
                    "temporary_directory_changed",
                    "Temporary directory identity changed; preserved replacement",
                )
                .details(json!({"path":path})));
            }
            fs::remove_dir_all(path)?;
            Ok(())
        })();
        if let Err(error) = result {
            errors.push(error.value());
        }
    }
    errors
}
/// The jobs schema this version reads and writes (PRAGMA user_version).
/// Version 2 adds the `events` table and the triggers that fill it.
const SCHEMA_VERSION: i64 = 2;
const SCHEMA: &str = "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY,label TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,spec TEXT NOT NULL,pid INTEGER,token TEXT,exit_code INTEGER,result TEXT,cancel INTEGER DEFAULT 0); CREATE TABLE IF NOT EXISTS inputs (id INTEGER PRIMARY KEY,job TEXT NOT NULL,text TEXT NOT NULL); CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created DESC); CREATE INDEX IF NOT EXISTS inputs_job ON inputs(job);";
/// A job's timeline. Triggers record every state change, phase change and
/// cancellation request, whichever code path or Tomacco version writes the
/// row, and drop a job's events with the job. JSON is read only when it is
/// valid (CASE evaluates lazily), so a malformed result never fails a write.
const EVENTS_SCHEMA: &str = "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY,job TEXT NOT NULL,time REAL NOT NULL,kind TEXT NOT NULL,value TEXT NOT NULL,detail TEXT); CREATE INDEX IF NOT EXISTS events_job ON events(job,id); \
CREATE TRIGGER IF NOT EXISTS job_event_created AFTER INSERT ON jobs BEGIN INSERT INTO events(job,time,kind,value) VALUES(NEW.id,NEW.created,'state',NEW.state); END; \
CREATE TRIGGER IF NOT EXISTS job_event_state AFTER UPDATE OF state ON jobs WHEN NEW.state IS NOT OLD.state BEGIN INSERT INTO events(job,time,kind,value,detail) VALUES(NEW.id,MAX((julianday('now')-2440587.5)*86400.0,NEW.created),'state',NEW.state,json_object('exit_code',NEW.exit_code,'error',CASE WHEN json_valid(NEW.result) THEN json_extract(NEW.result,'$.error') END)); END; \
CREATE TRIGGER IF NOT EXISTS job_event_phase AFTER UPDATE OF result ON jobs WHEN (CASE WHEN json_valid(NEW.result) THEN json_extract(NEW.result,'$.phase') END) IS NOT NULL AND (CASE WHEN json_valid(NEW.result) THEN json_extract(NEW.result,'$.phase') END) IS NOT (CASE WHEN json_valid(OLD.result) THEN json_extract(OLD.result,'$.phase') END) BEGIN INSERT INTO events(job,time,kind,value,detail) VALUES(NEW.id,MAX((julianday('now')-2440587.5)*86400.0,NEW.created),'phase',json_extract(NEW.result,'$.phase'),json_object('reason',json_extract(NEW.result,'$.reason'))); END; \
CREATE TRIGGER IF NOT EXISTS job_event_cancel AFTER UPDATE OF cancel ON jobs WHEN NEW.cancel=1 AND COALESCE(OLD.cancel,0)=0 BEGIN INSERT INTO events(job,time,kind,value) VALUES(NEW.id,MAX((julianday('now')-2440587.5)*86400.0,NEW.created),'cancel_requested',''); END; \
CREATE TRIGGER IF NOT EXISTS job_event_deleted AFTER DELETE ON jobs BEGIN DELETE FROM events WHERE job=OLD.id; END;";
/// How long a single statement waits for a lock another connection holds.
const BUSY_TIMEOUT: Duration = Duration::from_secs(15);
/// How long read-only requests keep asking a contended database again.
const READ_RETRY: Duration = Duration::from_secs(60);
/// A worker records its job's progress and end against any contention: a
/// slow writer elsewhere must not fail or lose a durable job.
const WORKER_RETRY: Duration = Duration::from_secs(600);
pub fn database() -> Result<Connection> {
    let root = home()?;
    retrying(Instant::now() + READ_RETRY, || open(&root))
}
/// Opens the job database in `root`. Many local clients (desktop polling,
/// long-lived MCP servers, job workers, CLI calls) share it, so an open of
/// an initialised database only reads its header: no schema statement, no
/// initialisation lock and no write lock. Only a new or older database is
/// initialised, once, under the lock and inside one IMMEDIATE transaction.
pub(crate) fn open(root: &Path) -> Result<Connection> {
    let path = root.join("jobs.sqlite3");
    let db = Connection::open(&path)?;
    db.busy_timeout(BUSY_TIMEOUT)?;
    // A connection that closes last would otherwise checkpoint under an
    // exclusive lock that stalls every other client while it syncs. The
    // automatic checkpoints of committing writers still bound the WAL.
    db.set_db_config(
        rusqlite::config::DbConfig::SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE,
        true,
    )?;
    if !initialised(&db)? {
        initialise(root, &db)?;
    }
    restrict(&path);
    Ok(db)
}
fn initialised(db: &Connection) -> Result<bool> {
    let version: i64 = db.query_row("PRAGMA user_version", [], |r| r.get(0))?;
    let mode: String = db.query_row("PRAGMA journal_mode", [], |r| r.get(0))?;
    Ok(version >= SCHEMA_VERSION && mode.eq_ignore_ascii_case("wal"))
}
fn initialise(root: &Path, db: &Connection) -> Result<()> {
    // SQLite's journal-mode transition can return BUSY immediately when
    // simultaneous first connections hold competing read locks. Serialize
    // initialization across CLI processes and MCP threads; release this lock
    // before admitting or executing any job.
    let initialization = Lock::open(&root.join("locks/jobs-initialize.lock"))?;
    let deadline = Instant::now() + BUSY_TIMEOUT;
    while !initialization.acquire(true)? {
        check_cancelled()?;
        if Instant::now() >= deadline {
            return Err(Error::new(
                "database_busy",
                "Timed out waiting for job database initialization",
            ));
        }
        thread::sleep(Duration::from_millis(10));
    }
    if initialised(db)? {
        return Ok(());
    }
    // journal_mode cannot change inside a transaction.
    db.execute_batch("PRAGMA journal_mode=WAL;")?;
    db.execute_batch(&format!(
        "BEGIN IMMEDIATE; {SCHEMA} {EVENTS_SCHEMA} PRAGMA user_version={SCHEMA_VERSION}; COMMIT;"
    ))
    .inspect_err(|_| {
        let _ = db.execute_batch("ROLLBACK;");
    })?;
    Ok(())
}
/// Job records contain commands, environments and tool output; they are the
/// user's alone even where an older version created the files with 0644.
fn restrict(database: &Path) {
    use std::os::unix::fs::PermissionsExt;
    for suffix in ["", "-wal", "-shm"] {
        let file = PathBuf::from(format!("{}{suffix}", database.display()));
        if let Ok(metadata) = fs::symlink_metadata(&file) {
            if metadata.is_file() && metadata.mode() & 0o077 != 0 {
                let _ = fs::set_permissions(&file, fs::Permissions::from_mode(0o600));
            }
        }
    }
}
/// Asks a read-only request again while the database is merely contended
/// (SQLITE_BUSY after the busy timeout). A waiting client must not fail, and
/// must never cause a durable job to change, because another writer was slow.
fn retrying<T>(deadline: Instant, mut action: impl FnMut() -> Result<T>) -> Result<T> {
    let mut pause = Duration::from_millis(20);
    loop {
        match action() {
            Err(error) if error.is_database_busy() && Instant::now() < deadline => {
                check_cancelled()?;
                thread::sleep(pause.min(deadline.saturating_duration_since(Instant::now())));
                pause = (pause * 2).min(Duration::from_secs(1));
            }
            other => return other,
        }
    }
}
fn data_root(db: &Connection) -> Result<PathBuf> {
    db.path()
        .filter(|path| !path.is_empty())
        .and_then(|path| Path::new(path).parent().map(Path::to_path_buf))
        .ok_or_else(|| Error::new("database_error", "The job database has no directory"))
}
fn lease_path(db: &Connection, id: &str) -> Result<PathBuf> {
    Ok(data_root(db)?
        .join("jobs")
        .join(format!("{id}.worker.lock")))
}
fn token(pid: u32) -> String {
    fs::read_to_string(format!("/proc/{pid}/stat"))
        .ok()
        .and_then(|s| {
            s.rsplit_once(')').map(|(_, s)| {
                if s.split_whitespace().next() == Some("Z") {
                    String::new()
                } else {
                    s.split_whitespace().nth(19).unwrap_or("").to_string()
                }
            })
        })
        .unwrap_or_default()
}
struct Row {
    value: Value,
    pid: Option<u32>,
    token: String,
    cancel: bool,
}
fn row(r: &rusqlite::Row<'_>) -> rusqlite::Result<Row> {
    Ok(Row {
        value: json!({"id":r.get::<_,String>("id")?,"label":r.get::<_,String>("label")?,"state":r.get::<_,String>("state")?,"created":r.get::<_,f64>("created")?,"updated":r.get::<_,f64>("updated")?,"exit_code":r.get::<_,Option<i32>>("exit_code")?,"plan":r.get::<_,String>("spec")?,"result":r.get::<_,Option<String>>("result")?}),
        pid: r.get("pid")?,
        token: r.get::<_, Option<String>>("token")?.unwrap_or_default(),
        cancel: r.get::<_, i32>("cancel")? != 0,
    })
}
fn get_row(db: &Connection, id: &str) -> Result<Row> {
    identifier(id, "job ID", Some("[0-9a-f]{32}"))?;
    db.query_row("SELECT * FROM jobs WHERE id=?", [id], row)
        .optional()?
        .ok_or_else(|| Error::new("job_not_found", format!("Unknown job {id}")))
}
fn worker_alive(db: &Connection, row: &Row) -> Result<bool> {
    // /proc PIDs are local to a namespace. An agent container may share the
    // job database without being able to see its host worker. The worker owns
    // this kernel-released lease until it commits its terminal state.
    let lease = lease_path(db, s(&row.value, "id"))?;
    if lease.try_exists()? {
        return lease_held(&lease);
    }
    // Compatibility with workers started before leases were introduced.
    Ok(row
        .pid
        .is_some_and(|pid| token(pid) == row.token && !row.token.is_empty()))
}
fn lease_held(lease: &Path) -> Result<bool> {
    Ok(!Lock::open(lease)?.acquire(true)?)
}
/// Why a job that is not terminal can no longer finish, if it cannot.
fn abandoned(db: &Connection, row: &Row) -> Result<Option<&'static str>> {
    let state = s(&row.value, "state");
    if state == "running" && !worker_alive(db, row)? {
        return Ok(Some("Worker exited unexpectedly"));
    }
    if state == "queued" && now() - row.value["created"].as_f64().unwrap_or(0.0) > 30.0 {
        // A worker takes its lease before it opens the database, so a slow
        // start on a busy host is not mistaken for one that never happened.
        let lease = lease_path(db, s(&row.value, "id"))?;
        if !(lease.try_exists()? && lease_held(&lease)?) {
            return Ok(Some("Worker did not start within 30 seconds"));
        }
    }
    Ok(None)
}
/// Marks a job whose worker is gone as failed. The decision is taken again
/// inside one IMMEDIATE transaction, on the row as it is now: a reader that
/// observed `queued` and then waited for the write lock must not cancel a
/// worker that has meanwhile started (that turned live jobs `cancelled`).
/// Temporary directories are removed only after the commit, outside it.
fn recover(db: &Connection, id: &str) -> Result<()> {
    let transaction =
        rusqlite::Transaction::new_unchecked(db, rusqlite::TransactionBehavior::Immediate)?;
    let current = get_row(db, id)?;
    let Some(reason) = abandoned(db, &current)? else {
        return Ok(());
    };
    let spec: Value = serde_json::from_str(s(&current.value, "plan"))?;
    cancel_tree(db, id)?;
    db.execute(
        "UPDATE jobs SET state='failed',result=?,updated=? WHERE id=? AND state IN ('queued','running')",
        params![json!({"error":reason}).to_string(), now(), id],
    )?;
    transaction.commit()?;
    let cleanup_errors = remove_temporary_directories(&spec);
    if !cleanup_errors.is_empty() {
        db.execute(
            "UPDATE jobs SET result=? WHERE id=? AND state='failed'",
            params![
                json!({"error":reason,"cleanup_errors":cleanup_errors}).to_string(),
                id
            ],
        )?;
    }
    Ok(())
}
fn public(db: &Connection, mut row: Row) -> Result {
    let id = s(&row.value, "id").to_string();
    if abandoned(db, &row)?.is_some() {
        match recover(db, &id) {
            Ok(()) => row = get_row(db, &id)?,
            // A reader never fails, or changes a job, because the database is
            // contended; a later reader recovers the abandoned job.
            Err(error) if error.is_database_busy() => (),
            Err(error) => return Err(error),
        }
    }
    row.value["plan"] = serde_json::from_str(s(&row.value, "plan"))?;
    row.value["result"] = if let Some(text) = row.value["result"].as_str() {
        serde_json::from_str(text)?
    } else {
        Value::Null
    };
    row.value["phase"] = row.value["result"]["phase"].clone();
    if s(&row.value, "state") == "queued" && !s(&row.value["result"], "reason").is_empty() {
        row.value["queued_reason"] = row.value["result"]["reason"].clone();
    }
    row.value["interactive"] = json!(
        row.value["plan"]["steps"]
            .as_array()
            .is_some_and(|steps| steps.iter().any(|step| b(step, "interactive")))
    );
    // Older workers have no machine-event state. Their interactive session is
    // still reusable, and should not be labelled as another build forever.
    if row.value["phase"].is_null()
        && b(&row.value, "interactive")
        && s(&row.value, "state") == "running"
    {
        row.value["phase"] = json!("session");
    }
    row.value["log_path"] = json!(data_root(db)?.join("jobs").join(format!("{id}.log")));
    Ok(row.value)
}
fn read(db: &Connection, id: &str) -> Result {
    public(db, get_row(db, id)?)
}
pub fn get(id: &str) -> Result {
    identifier(id, "job ID", Some("[0-9a-f]{32}"))?;
    let db = database()?;
    retrying(Instant::now() + READ_RETRY, || read(&db, id))
}
/// Recover dead workers before reporting an exclusive resource as occupied.
/// Callers must hold their resource admission lock while checking and mutating.
pub fn active_exclusive(exclusive: &str) -> Result<Option<Value>> {
    let db = database()?;
    let rows = db.prepare("SELECT * FROM jobs WHERE state IN ('queued','running') AND json_extract(spec,'$.exclusive')=? ORDER BY created")?
        .query_map([exclusive], row)?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    for row in rows {
        let cancelling = row.cancel;
        let mut job = public(&db, row)?;
        if !TERMINAL.contains(&s(&job, "state")) {
            job["cancelling"] = json!(cancelling);
            return Ok(Some(job));
        }
    }
    Ok(None)
}
/// Every job still queued or running except `except`, dead workers recovered.
pub fn active(except: &str) -> Result<Vec<Value>> {
    let db = database()?;
    let rows = db
        .prepare("SELECT * FROM jobs WHERE state IN ('queued','running') ORDER BY created")?
        .query_map([], row)?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    let mut found = Vec::new();
    for row in rows {
        let job = public(&db, row)?;
        if s(&job, "id") != except && !TERMINAL.contains(&s(&job, "state")) {
            found.push(job);
        }
    }
    Ok(found)
}
pub fn listing(limit: u64) -> Result {
    list(&json!({ "limit": limit }))
}
/// The newest jobs first, narrowed by `state`, `operation` (a prefix of the
/// operation that started the job), `project` (a directory the job works in
/// or locks), `since` (created at or after, seconds since 1970) and `before`
/// (the next page: jobs created before that job).
pub fn list(a: &Value) -> Result {
    let db = database()?;
    retrying(Instant::now() + READ_RETRY, || query(&db, a))
}
fn like_prefix(value: &str) -> String {
    let mut escaped = String::new();
    for c in value.chars() {
        if matches!(c, '\\' | '%' | '_') {
            escaped.push('\\');
        }
        escaped.push(c);
    }
    escaped + "%"
}
fn query(db: &Connection, a: &Value) -> Result {
    let mut clauses = Vec::new();
    let mut values: Vec<rusqlite::types::Value> = Vec::new();
    let states = strings(&a["state"]);
    if !states.is_empty() {
        clauses.push(format!("state IN ({})", vec!["?"; states.len()].join(",")));
        values.extend(states.iter().cloned().map(Into::into));
    }
    if !s(a, "operation").is_empty() {
        clauses.push("json_extract(spec,'$.operation') LIKE ? ESCAPE '\\'".into());
        values.push(like_prefix(s(a, "operation")).into());
    }
    if !s(a, "project").is_empty() {
        let project = expand(s(a, "project"));
        if !project.is_absolute() {
            return Err(Error::new(
                "invalid_argument",
                "project must be an absolute path",
            ));
        }
        let project = project.to_string_lossy().trim_end_matches('/').to_string();
        clauses.push("(json_extract(spec,'$.exclusive')=? OR json_extract(spec,'$.cwd')=? OR json_extract(spec,'$.cwd') LIKE ? ESCAPE '\\')".into());
        values.push(project.clone().into());
        values.push(project.clone().into());
        values.push(like_prefix(&format!("{project}/")).into());
    }
    if let Some(since) = a["since"].as_f64() {
        clauses.push("created>=?".into());
        values.push(since.into());
    }
    if !s(a, "before").is_empty() {
        let before = get_row(db, s(a, "before"))?;
        let created = before.value["created"].as_f64().unwrap_or(0.0);
        clauses.push("(created<? OR (created=? AND id<?))".into());
        values.push(created.into());
        values.push(created.into());
        values.push(s(a, "before").to_string().into());
    }
    let filter = if clauses.is_empty() {
        String::new()
    } else {
        format!("WHERE {}", clauses.join(" AND "))
    };
    values.push((a["limit"].as_u64().unwrap_or(30) as i64).into());
    let rows = db
        .prepare(&format!(
            "SELECT * FROM jobs {filter} ORDER BY created DESC, id DESC LIMIT ?"
        ))?
        .query_map(rusqlite::params_from_iter(values), row)?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    let mut jobs = Vec::new();
    for row in rows {
        let job = public(db, row)?;
        // A job whose worker died is recorded as failed while it is read.
        if states.is_empty() || states.iter().any(|state| state == s(&job, "state")) {
            jobs.push(job);
        }
    }
    Ok(json!(jobs))
}
thread_local! {
    static REQUEST: std::cell::RefCell<Option<(String, Value)>> = const { std::cell::RefCell::new(None) };
}
/// The operation this thread is answering, for the jobs it submits, and the
/// `retry` its caller asked for (only operations whose schema has `retry`,
/// where repeating the job is safe, accept one). Nested operations restore
/// the outer one when they end.
pub struct Request(Option<(String, Value)>);
impl Request {
    pub fn enter(operation: &str, retry: &Value) -> Self {
        Request(REQUEST.with(|r| r.replace(Some((operation.to_string(), retry.clone())))))
    }
}
impl Drop for Request {
    fn drop(&mut self) {
        let previous = self.0.take();
        REQUEST.with(|r| *r.borrow_mut() = previous);
    }
}
fn requested_operation() -> Option<String> {
    REQUEST.with(|r| r.borrow().as_ref().map(|(operation, _)| operation.clone()))
}
fn requested_retry() -> Option<Value> {
    REQUEST.with(|r| {
        r.borrow()
            .as_ref()
            .map(|(_, retry)| retry.clone())
            .filter(|retry| n(retry, "count") > 0)
            .map(|retry| {
                json!({
                    "count": n(&retry, "count").min(RETRY_LIMIT),
                    "backoff_seconds": retry["backoff_seconds"].as_u64().unwrap_or(5).clamp(1, RETRY_PAUSE_LIMIT),
                })
            })
    })
}
pub fn submit(label: &str, mut spec: Value) -> Result {
    check_cancelled()?;
    if spec.get("operation").is_none() {
        if let Some(operation) = requested_operation() {
            spec["operation"] = json!(operation);
        }
    }
    if spec.get("retry").is_none() {
        if let Some(retry) = requested_retry() {
            spec["retry"] = retry;
        }
    }
    if let Ok(parent) = std::env::var("TOMACCO_PARENT_JOB") {
        if !parent.is_empty() {
            identifier(&parent, "parent job", Some("[a-f0-9]{32}"))?;
            let row = get_row(&database()?, &parent)?;
            if row.cancel || TERMINAL.contains(&s(&row.value, "state")) {
                return Err(Error::new(
                    "parent_stopped",
                    "The parent job has already stopped",
                ));
            }
            spec["parent_job"] = json!(parent);
        }
    }
    let steps = spec["steps"]
        .as_array()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| Error::new("invalid_job", "A job needs at least one command"))?;
    for step in steps {
        if !step["argv"].as_array().is_some_and(|a| {
            !a.is_empty()
                && a.iter()
                    .all(|v| v.as_str().is_some_and(|s| !s.contains('\0')))
        }) {
            return Err(Error::new(
                "invalid_job",
                "Commands must be non-empty arrays of strings",
            ));
        }
    }
    if spec.get("timeout").is_none() {
        spec["timeout"] = json!(1800);
    }
    if spec.get("artifacts").is_none() {
        spec["artifacts"] = json!([]);
    }
    if spec.get("env").is_none() {
        spec["env"] = json!({});
    }
    if spec.get("exclusive").is_none() {
        spec["exclusive"] = json!("");
    }
    spec["cwd"] = json!(path(s(&spec, "cwd"), true, true)?);
    spec.as_object_mut().unwrap().remove("label");
    spec.as_object_mut().unwrap().remove("project");
    let id = uuid::Uuid::new_v4().simple().to_string();
    let db = database()?;
    // Serialize admission separately from the worker's workspace lock. A live
    // Flutter session can hold that lock for hours; submitting must never wait
    // for it or enqueue another launch behind it.
    let _admission = if b(&spec, "single_active") && !s(&spec, "exclusive").is_empty() {
        let lock = Lock::open(&home()?.join("locks").join(format!(
            "submit-{}.lock",
            digest(s(&spec, "exclusive").as_bytes())
        )))?;
        lock.acquire(false)?;
        let rows = db.prepare("SELECT * FROM jobs WHERE state IN ('queued','running') AND json_extract(spec,'$.exclusive')=? ORDER BY created")?
            .query_map([s(&spec, "exclusive")], row)?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        for row in rows {
            let cancelling = row.cancel;
            let mut active = public(&db, row)?;
            if TERMINAL.contains(&s(&active, "state")) {
                continue;
            }
            if !cancelling && same_work(&active["plan"], &spec) {
                active["reused"] = json!(true);
                return Ok(active);
            }
            return Err(Error::new("workspace_busy", "This project already has an active build or app session")
                .remedy("Reuse its job ID, use Hot Reload for a running Flutter app, or stop that job before starting different work")
                .details(json!({"job":active,"cancelling":cancelling})));
        }
        Some(lock)
    } else {
        None
    };
    db.execute(
        "INSERT INTO jobs(id,label,state,created,updated,spec) VALUES(?,?,'queued',?,?,?)",
        params![id, label, now(), now(), spec.to_string()],
    )?;
    let mut command = Command::new(crate::core::own_executable()?);
    command
        .args(["_worker", &id])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    // SAFETY: setsid is async-signal-safe and invoked only in the forked child.
    unsafe {
        command.pre_exec(|| {
            if libc::setsid() < 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    match command.spawn() {
        Ok(mut child) => {
            thread::spawn(move || {
                let _ = child.wait();
            });
        }
        Err(e) => {
            db.execute(
                "UPDATE jobs SET state='failed',result=?,updated=? WHERE id=?",
                params![json!({"error":e.to_string()}).to_string(), now(), id],
            )?;
            return Err(Error::new("worker_start_failed", e.to_string()));
        }
    }
    get(&id)
}

/// Records the URIs a Flutter machine event names: the VM service from
/// `app.debugPort`, the DevTools server from `app.devTools` (with the address
/// that opens it on this app) and the Dart Tooling Daemon from `app.dtd`.
/// Whether the event was one of them.
pub fn flutter_uris(flutter: &mut Value, event: &str, params: &Value) -> bool {
    let uri = |key: &str| params[key].as_str().filter(|u| !u.is_empty());
    match event {
        "app.debugPort" => {
            let Some(ws) = uri("wsUri") else {
                return false;
            };
            flutter["vm_service_uri"] = json!(ws);
            let http = ws
                .replacen("ws://", "http://", 1)
                .replacen("wss://", "https://", 1);
            flutter["vm_service_http_uri"] = json!(http.strip_suffix("ws").unwrap_or(&http));
        }
        "app.devTools" => {
            let Some(devtools) = uri("uri").or_else(|| uri("url")) else {
                return false;
            };
            flutter["devtools_uri"] = json!(devtools);
        }
        "app.dtd" => {
            let Some(dtd) = uri("uri") else {
                return false;
            };
            flutter["dtd_uri"] = json!(dtd);
        }
        _ => return false,
    }
    if let (Some(devtools), Some(service)) = (
        flutter["devtools_uri"].as_str(),
        flutter["vm_service_http_uri"].as_str(),
    ) {
        if !devtools.contains('?') {
            flutter["devtools_app_uri"] = json!(format!("{devtools}?uri={service}"));
        }
    }
    true
}

fn same_work(left: &Value, right: &Value) -> bool {
    if left["cwd"] != right["cwd"] || left["parent_job"] != right["parent_job"] {
        return false;
    }
    if left["operation_key"].is_object() && right["operation_key"].is_object() {
        return left["operation_key"] == right["operation_key"];
    }
    // Adopt sessions created by earlier Tomacco versions as well.
    // The executable, target, build flags and working directory must match.
    let normalize = |value: &Value| {
        let mut steps = value["steps"].clone();
        for step in steps.as_array_mut().into_iter().flatten() {
            if s(step, "protocol") == "flutter-machine" {
                step.as_object_mut().unwrap().remove("protocol");
                if let Some(args) = step["argv"].as_array_mut() {
                    args.retain(|arg| arg != "--machine");
                }
            }
        }
        steps
    };
    normalize(left) == normalize(right)
}
pub fn logs(a: &Value) -> Result {
    let id = s(a, "job_id");
    let job = get(id)?;
    let p = PathBuf::from(s(&job, "log_path"));
    let offset = n(a, "offset");
    let mut data = Vec::new();
    let mut size = 0;
    if p.exists() {
        let mut file = File::open(&p)?;
        size = file.metadata()?.len();
        file.seek(SeekFrom::Start(offset))?;
        file.take(n(a, "limit")).read_to_end(&mut data)?;
    }
    Ok(
        json!({"id":id,"state":job["state"],"offset":offset,"next_offset":offset+data.len() as u64,"text":String::from_utf8_lossy(&data),"eof":offset+data.len() as u64>=size}),
    )
}
/// The input job.send stop queues for a stoppable step.
const STOP_INPUT: &str = "\u{1}stop";
pub fn cancel(id: &str) -> Result {
    identifier(id, "job ID", Some("[0-9a-f]{32}"))?;
    let db = database()?;
    // Setting the flag is idempotent; a contended database is asked again.
    retrying(Instant::now() + READ_RETRY, || {
        let row = get_row(&db, id)?;
        let active = !TERMINAL.contains(&s(&row.value, "state"));
        if active {
            cancel_tree(&db, id)?;
        }
        Ok(json!({"id":id,"cancellation_requested":active}))
    })
}
fn cancel_tree(db: &Connection, id: &str) -> Result<()> {
    db.execute("WITH RECURSIVE descendants(id) AS (SELECT ?1 UNION SELECT j.id FROM jobs j JOIN descendants d ON json_extract(j.spec,'$.parent_job')=d.id) UPDATE jobs SET cancel=1,updated=?2 WHERE id IN descendants AND state IN ('queued','running')",params![id,now()])?;
    Ok(())
}
pub fn send(id: &str, command: &str) -> Result {
    let db = database()?;
    let row = get_row(&db, id)?;
    let plan: Value = serde_json::from_str(s(&row.value, "plan"))?;
    if command == "stop" {
        // A recording ends early and keeps what it has.
        if s(&row.value, "state") != "running"
            || !plan["steps"]
                .as_array()
                .is_some_and(|a| a.iter().any(|s| b(s, "stoppable")))
        {
            return Err(Error::new(
                "not_stoppable",
                "This job has no running step that stop ends early",
            )
            .remedy("job.cancel ends any job, without keeping its result"));
        }
        retrying(Instant::now() + READ_RETRY, || {
            Ok(db.execute(
                "INSERT INTO inputs(job,text) VALUES(?,?)",
                params![id, STOP_INPUT],
            )?)
        })?;
        return Ok(json!({"id":id,"sent":command}));
    }
    if s(&row.value, "state") != "running"
        || !plan["steps"]
            .as_array()
            .is_some_and(|a| a.iter().any(|s| b(s, "interactive")))
    {
        return Err(Error::new(
            "not_interactive",
            "This job has no interactive session",
        ));
    }
    let text = match command {
        "reload" => "r",
        "restart" => "R",
        "quit" => "q",
        _ => {
            return Err(Error::new(
                "invalid_argument",
                "Unknown interactive command",
            ));
        }
    };
    // SQLITE_BUSY is returned before anything is written, so asking again
    // never queues the command twice.
    retrying(Instant::now() + READ_RETRY, || {
        Ok(db.execute(
            "INSERT INTO inputs(job,text) VALUES(?,?)",
            params![id, text],
        )?)
    })?;
    Ok(json!({"id":id,"sent":command}))
}
/// Waiting only reads. It uses one connection for the whole wait, and a
/// contended database is asked again: the deadline then still returns the
/// job, and only a database that stays locked well past it is an error.
/// Neither a failed nor an abandoned wait changes the job.
pub fn wait(id: &str, seconds: u64) -> Result {
    identifier(id, "job ID", Some("[0-9a-f]{32}"))?;
    let deadline = Instant::now() + Duration::from_secs(seconds);
    let db = database()?;
    loop {
        check_cancelled()?;
        let job = retrying(deadline.max(Instant::now()) + READ_RETRY, || read(&db, id))?;
        if TERMINAL.contains(&s(&job, "state")) || Instant::now() >= deadline {
            return Ok(job);
        }
        thread::sleep(Duration::from_millis(100));
    }
}

/// A job's recorded state transitions, phase changes and cancellation
/// requests in order, from event `after` on. Jobs created before Tomacco
/// recorded events get their creation and, once ended, their end from the
/// job record, marked `synthesized`.
pub fn events(id: &str, after: u64, limit: u64) -> Result {
    identifier(id, "job ID", Some("[0-9a-f]{32}"))?;
    let db = database()?;
    retrying(Instant::now() + READ_RETRY, || {
        timeline(&db, id, after, limit)
    })
}
fn timeline(db: &Connection, id: &str, after: u64, limit: u64) -> Result {
    // Reading the job first records a dead worker's end as an event.
    let job = read(db, id)?;
    let mut events = db
        .prepare(
            "SELECT id,time,kind,value,detail FROM events WHERE job=? AND id>? ORDER BY id LIMIT ?",
        )?
        .query_map(params![id, after as i64, limit as i64], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, f64>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, String>(3)?,
                r.get::<_, Option<String>>(4)?,
            ))
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?
        .into_iter()
        .map(|(event, time, kind, value, detail)| {
            let mut entry = json!({"id":event,"time":time,"kind":kind,"value":value});
            let detail: Value = detail
                .and_then(|text| serde_json::from_str(&text).ok())
                .unwrap_or(Value::Null);
            if let Some(fields) = detail.as_object() {
                for (key, value) in fields.iter().filter(|(_, v)| !v.is_null()) {
                    entry[key] = value.clone();
                }
            }
            entry
        })
        .collect::<Vec<_>>();
    let recorded: i64 = db.query_row("SELECT count(*) FROM events WHERE job=?", [id], |r| {
        r.get(0)
    })?;
    let next_after = events.last().map_or(after, |e| n(e, "id"));
    if recorded == 0 && after == 0 {
        events.push(json!({"id":0,"time":job["created"],"kind":"state","value":"queued","synthesized":true}));
        if TERMINAL.contains(&s(&job, "state")) {
            events.push(json!({"id":0,"time":job["updated"],"kind":"state","value":job["state"],"exit_code":job["exit_code"],"synthesized":true}));
        }
    }
    Ok(json!({
        "id":id,"label":job["label"],"state":job["state"],"phase":job["phase"],
        "created":job["created"],"updated":job["updated"],
        "events":events,"next_after":next_after,"recorded":recorded>0,
    }))
}

struct ManagedChild(Child);
impl Drop for ManagedChild {
    fn drop(&mut self) {
        toolchain::stop(&mut self.0);
    }
}
struct PartialOutput {
    temporary: PathBuf,
    target: PathBuf,
}
impl Drop for PartialOutput {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.temporary);
    }
}
/// Jobs that take a share of the machine name a `resource` in their spec.
/// Settings `jobs.KEY` caps how many of them run at once (0: no cap); the
/// rest wait in `queued` with the reason.
pub const RESOURCES: &[(&str, &str, u64)] =
    &[("build", "max_builds", 2), ("emulator", "max_emulators", 0)];
pub fn resource_limit(settings: &Value, resource: &str) -> Option<(&'static str, u64)> {
    RESOURCES
        .iter()
        .find(|(name, _, _)| *name == resource)
        .map(|(_, key, default)| (*key, settings["jobs"][*key].as_u64().unwrap_or(*default)))
}
/// One of `limit` slots of `resource`, if one is free. The kernel releases
/// it with the worker, however the worker ends.
fn free_slot(root: &Path, resource: &str, limit: u64) -> Result<Option<Lock>> {
    for index in 0..limit {
        let lock = Lock::open(
            &root
                .join("locks")
                .join(format!("slot-{resource}-{index}.lock")),
        )?;
        if lock.acquire(true)? {
            return Ok(Some(lock));
        }
    }
    Ok(None)
}
enum Admission {
    Admitted(Option<Lock>),
    Stopped,
}
/// Waits, still `queued`, until the job's resource has a free slot. The
/// reason names the jobs holding the slots and is published when it changes.
fn admit(
    db: &Connection,
    root: &Path,
    id: &str,
    spec: &Value,
    limit: impl Fn() -> Option<(&'static str, u64)>,
) -> Result<Admission> {
    let resource = s(spec, "resource");
    let mut published = String::new();
    loop {
        let Some((key, limit)) = limit().filter(|(_, limit)| *limit > 0) else {
            return Ok(Admission::Admitted(None));
        };
        if let Some(slot) = free_slot(root, resource, limit)? {
            return Ok(Admission::Admitted(Some(slot)));
        }
        let row = retrying(Instant::now() + WORKER_RETRY, || get_row(db, id))?;
        let parent_stopped = !s(spec, "parent_job").is_empty() && {
            let parent = retrying(Instant::now() + WORKER_RETRY, || {
                get_row(db, s(spec, "parent_job"))
            })?;
            parent.cancel || TERMINAL.contains(&s(&parent.value, "state"))
        };
        if row.cancel || parent_stopped {
            return Ok(Admission::Stopped);
        }
        let holders = db
            .prepare("SELECT id FROM jobs WHERE state='running' AND json_extract(spec,'$.resource')=? ORDER BY created")?
            .query_map([resource], |r| r.get::<_, String>(0))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        let reason = format!(
            "Waiting for a {resource} slot: {limit} of {limit} in use{}. settings.configure jobs.{key} changes the limit",
            if holders.is_empty() {
                String::new()
            } else {
                format!(" by {}", holders.join(", "))
            }
        );
        if reason != published {
            retrying(Instant::now() + WORKER_RETRY, || {
                Ok(db.execute(
                    "UPDATE jobs SET result=?,updated=? WHERE id=? AND state='queued'",
                    params![
                        json!({"phase":"queued","reason":reason}).to_string(),
                        now(),
                        id
                    ],
                )?)
            })?;
            published = reason;
        }
        thread::sleep(Duration::from_millis(250));
    }
}
pub fn worker(id: &str) -> Result<()> {
    identifier(id, "job ID", Some("[0-9a-f]{32}"))?;
    // The lease comes first: while the database is contended a reader sees a
    // queued job whose worker exists, rather than one that never started.
    // Readers probe the lease for an instant, so a collision is asked again;
    // another worker of the same job holds it for that job's whole run.
    let lease = Lock::open(&home()?.join("jobs").join(format!("{id}.worker.lock")))?;
    let deadline = Instant::now() + Duration::from_secs(2);
    while !lease.acquire(true)? {
        if Instant::now() >= deadline {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(10));
    }
    let db = database()?;
    let row = retrying(Instant::now() + WORKER_RETRY, || get_row(&db, id))?;
    if s(&row.value, "state") != "queued" {
        return Ok(());
    }
    let spec: Value = serde_json::from_str(s(&row.value, "plan"))?;
    let _slot = if s(&spec, "resource").is_empty() {
        None
    } else {
        let root = home()?;
        match admit(&db, &root, id, &spec, || {
            resource_limit(
                &toolchain::settings().unwrap_or_default(),
                s(&spec, "resource"),
            )
        })? {
            Admission::Admitted(slot) => slot,
            Admission::Stopped => {
                let errors = remove_temporary_directories(&spec);
                let mut result = json!({"artifacts":[],"error":"Cancelled while queued"});
                if !errors.is_empty() {
                    result["cleanup_errors"] = json!(errors);
                }
                retrying(Instant::now() + WORKER_RETRY, || {
                    Ok(db.execute(
                        "UPDATE jobs SET state='cancelled',exit_code=-1,result=?,updated=? WHERE id=? AND state='queued'",
                        params![result.to_string(), now(), id],
                    )?)
                })?;
                return Ok(());
            }
        }
    };
    let pid = std::process::id();
    if retrying(Instant::now() + WORKER_RETRY, || {
        Ok(db.execute(
            "UPDATE jobs SET state='running',pid=?,token=?,updated=? WHERE id=? AND state='queued'",
            params![pid, token(pid), now(), id],
        )?)
    })? != 1
    {
        return Ok(());
    }
    let result = toolchain::prepare_job_reaper().and_then(|()| execute_worker(&db, id, &spec));
    let (state, code, mut result) = match result {
        Ok(v) => v,
        Err(e) => (
            "failed".to_string(),
            -1,
            json!({"artifacts":[],"error":e.to_string()}),
        ),
    };
    let mut cleanup_errors = remove_temporary_directories(&spec);
    if let Some(cleanup) = spec["cleanup"].as_array() {
        for command in cleanup {
            let (args, timeout) = if command.is_array() {
                (strings(command), 10)
            } else {
                (
                    strings(&command["argv"]),
                    command["timeout"].as_u64().unwrap_or(10).clamp(1, 600),
                )
            };
            if let Err(error) = toolchain::run(&args, timeout, None, true) {
                cleanup_errors.push(error.value());
            }
        }
    }
    if !cleanup_errors.is_empty() {
        result["cleanup_errors"] = json!(cleanup_errors);
    }
    if spec["cleanup"].as_array().is_some_and(|v| !v.is_empty()) {
        let mut paths = std::collections::BTreeSet::new();
        for item in result["artifacts"].as_array().into_iter().flatten() {
            paths.insert(s(item, "path").to_string());
        }
        for pattern in strings(&spec["always_artifacts"]) {
            let pattern = if Path::new(&pattern).is_absolute() {
                pattern
            } else {
                format!("{}/{}", glob::Pattern::escape(s(&spec, "cwd")), pattern)
            };
            for file in glob_paths(&pattern)? {
                if file.is_file() {
                    paths.insert(file.to_string_lossy().into_owned());
                }
            }
        }
        result["artifacts"] = json!(
            paths
                .into_iter()
                .take(100)
                .map(|p| artifact(Path::new(&p), row.value["created"].as_f64().unwrap_or(0.0)))
                .collect::<Result<Vec<_>>>()?
        );
    }
    if let Some(metadata) = spec.get("metadata") {
        result["metadata"] = metadata.clone();
    }
    let result = result.to_string();
    retrying(Instant::now() + WORKER_RETRY, || {
        db.execute(
            "UPDATE jobs SET state=?,exit_code=?,result=?,updated=? WHERE id=?",
            params![state, code, result, now(), id],
        )?;
        Ok(db.execute("DELETE FROM inputs WHERE job=?", [id])?)
    })?;
    Ok(())
}
/// The most retries a job may ask for, and the longest pause before one.
const RETRY_LIMIT: u64 = 5;
const RETRY_PAUSE_LIMIT: u64 = 600;
/// The pause before attempt `attempt + 1` after attempt `attempt` failed,
/// doubling from `backoff_seconds`, or None when no retry is left.
pub fn retry_delay(retry: &Value, attempt: u64) -> Option<u64> {
    if !retry.is_object() || attempt > n(retry, "count").min(RETRY_LIMIT) {
        return None;
    }
    let backoff = retry["backoff_seconds"].as_u64().unwrap_or(5);
    Some(
        backoff
            .saturating_mul(1 << (attempt - 1).min(16))
            .min(RETRY_PAUSE_LIMIT),
    )
}
fn execute_worker(db: &Connection, id: &str, spec: &Value) -> Result<(String, i32, Value)> {
    let log_dir = home()?.join("jobs");
    private_dir(&log_dir)?;
    let mut log = private_file(&log_dir.join(format!("{id}.log")), false)?;
    // Runtime services live with their owning process, rather than expiring
    // during a long development session. PID start tokens prevent PID reuse.
    let runtime_owner = spec
        .get("runtime_owner")
        .filter(|owner| n(owner, "pid") > 0 && !s(owner, "token").is_empty());
    let deadline = if runtime_owner.is_some() && n(spec, "timeout") == 0 {
        None
    } else {
        Some(Instant::now() + Duration::from_secs(n(spec, "timeout")))
    };
    let mut started = now();
    let mut state = "succeeded".to_string();
    let mut code = 0;
    let mut result = json!({"artifacts":[]});
    let mut output_paths = BTreeSet::new();
    // A Flutter session's state also carries its VM service and DevTools URIs.
    let publish_session = |phase: &str, app_id: &str, flutter: &Value| -> Result<()> {
        let mut state = json!({"phase":phase,"app_id":app_id});
        if flutter.as_object().is_some_and(|f| !f.is_empty()) {
            state["flutter"] = flutter.clone();
        }
        retrying(Instant::now() + WORKER_RETRY, || {
            db.execute(
                "UPDATE jobs SET result=?,updated=? WHERE id=?",
                params![state.to_string(), now(), id],
            )?;
            Ok(())
        })
    };
    let publish_phase = |phase: &str, app_id: &str| publish_session(phase, app_id, &Value::Null);
    let interrupt = |log: &File| -> Result<Option<&'static str>> {
        let parent_stopped = if !s(spec, "parent_job").is_empty() {
            let parent = retrying(Instant::now() + WORKER_RETRY, || {
                get_row(db, s(spec, "parent_job"))
            })?;
            parent.cancel
                || TERMINAL.contains(&s(&parent.value, "state"))
                || (parent.pid.is_some() && !worker_alive(db, &parent)?)
        } else {
            false
        };
        let runtime_stopped =
            runtime_owner.is_some_and(|owner| token(n(owner, "pid") as u32) != s(owner, "token"));
        if retrying(Instant::now() + WORKER_RETRY, || get_row(db, id))?.cancel
            || parent_stopped
            || runtime_stopped
        {
            Ok(Some("cancelled"))
        } else if deadline.is_some_and(|deadline| Instant::now() >= deadline) {
            Ok(Some("timed_out"))
        } else if log.metadata()?.len() >= LOG_LIMIT {
            Ok(Some("failed"))
        } else {
            Ok(None)
        }
    };
    let _lock = if !s(spec, "exclusive").is_empty() {
        let lock = Lock::open(
            &home()?
                .join("locks")
                .join(format!("{}.lock", digest(s(spec, "exclusive").as_bytes()))),
        )?;
        let mut waiting = false;
        loop {
            if let Some(value) = interrupt(&log)? {
                state = value.into();
                break;
            }
            if lock.acquire(true)? {
                started = now();
                break;
            }
            if !waiting {
                writeln!(log, "Waiting for workspace: {}", s(spec, "exclusive"))?;
                publish_phase("waiting", "")?;
                waiting = true;
            }
            thread::sleep(Duration::from_millis(100));
        }
        Some(lock)
    } else {
        None
    };
    let mut attempts = Vec::new();
    let mut attempt = 1;
    loop {
        let attempt_started = now();
        for step in spec["steps"]
            .as_array()
            .ok_or_else(|| Error::new("invalid_job", "Missing steps"))?
        {
            if state != "succeeded" {
                break;
            }
            if let Some(value) = interrupt(&log)? {
                state = value.into();
                break;
            }
            writeln!(log, "\n$ {}", step["argv"])?;
            publish_phase(
                if s(step, "phase") == "streaming" {
                    "streaming"
                } else {
                    "building"
                },
                "",
            )?;
            let machine = s(step, "protocol") == "flutter-machine";
            let mut event_file = File::open(log_dir.join(format!("{id}.log")))?;
            event_file.seek(SeekFrom::End(0))?;
            let mut event_buffer = String::new();
            let mut app_id = String::new();
            let mut phase = String::from("building");
            let mut flutter = json!({});
            let output = if !s(step, "output").is_empty() {
                let target = PathBuf::from(s(step, "output"));
                fs::create_dir_all(target.parent().unwrap())?;
                Some(PartialOutput {
                    temporary: target.with_file_name(format!(
                        "{}.{}.part",
                        target.file_name().unwrap_or_default().to_string_lossy(),
                        id
                    )),
                    target,
                })
            } else {
                None
            };
            let args = strings(&step["argv"]);
            let mut command = Command::new(
                args.first()
                    .ok_or_else(|| Error::new("invalid_job", "Missing command"))?,
            );
            command
                .args(&args[1..])
                .current_dir(step["cwd"].as_str().unwrap_or(s(spec, "cwd")))
                .process_group(0)
                .env("TOMACCO_PARENT_JOB", id);
            if let Some(env) = spec["env"].as_object() {
                for (key, value) in env {
                    command.env(
                        key,
                        value.as_str().ok_or_else(|| {
                            Error::new("invalid_job", "Environment values must be strings")
                        })?,
                    );
                }
            }
            command
                .stdin(if step.get("input").is_some() || b(step, "interactive") {
                    Stdio::piped()
                } else {
                    Stdio::null()
                })
                .stdout(if let Some(ref output) = output {
                    Stdio::from(File::create(&output.temporary)?)
                } else {
                    Stdio::from(log.try_clone()?)
                })
                .stderr(Stdio::from(log.try_clone()?));
            toolchain::parent_bound(&mut command);
            let mut proc = ManagedChild(command.spawn()?);
            if let Some(input) = step["input"].as_str() {
                if let Some(mut stdin) = proc.0.stdin.take() {
                    let text = input.to_string();
                    thread::spawn(move || {
                        let _ = stdin.write_all(text.as_bytes());
                    });
                }
            }
            let capture_deadline = step["duration"]
                .as_f64()
                .filter(|n| n.is_finite() && *n >= 0.0 && *n <= 86400.0)
                .map(|n| Instant::now() + Duration::from_secs_f64(n));
            let mut captured = false;
            // When job.send stop asked a stoppable step to end: it then has
            // this long to finish by itself.
            let mut stopping: Option<Instant> = None;
            loop {
                if machine {
                    let mut bytes = Vec::new();
                    std::io::Read::by_ref(&mut event_file)
                        .take(256 * 1024)
                        .read_to_end(&mut bytes)?;
                    event_buffer.push_str(&String::from_utf8_lossy(&bytes));
                    while let Some(end) = event_buffer.find('\n') {
                        let line: String = event_buffer.drain(..=end).collect();
                        if let Ok(Value::Array(events)) = serde_json::from_str::<Value>(line.trim())
                        {
                            for event in events {
                                match s(&event, "event") {
                                    "app.start" => {
                                        app_id = s(&event["params"], "appId").into();
                                    }
                                    "app.started" => phase = "ready".into(),
                                    "app.stop" => phase = "stopping".into(),
                                    name => {
                                        if !flutter_uris(&mut flutter, name, &event["params"]) {
                                            continue;
                                        }
                                    }
                                }
                                publish_session(&phase, &app_id, &flutter)?;
                            }
                        }
                    }
                    if event_buffer.len() > 256 * 1024 {
                        event_buffer.clear();
                    }
                }
                if let Some(exit) = proc.0.try_wait()? {
                    code = exit.code().unwrap_or(-1);
                    break;
                }
                if b(step, "interactive") {
                    // A contended poll is skipped; the inputs stay queued for the next.
                    let inputs = match (|| -> Result<Vec<(i64, String)>> {
                        Ok(db
                            .prepare("SELECT id,text FROM inputs WHERE job=? ORDER BY id")?
                            .query_map([id], |r| Ok((r.get(0)?, r.get(1)?)))?
                            .collect::<std::result::Result<Vec<_>, _>>()?)
                    })() {
                        Err(error) if error.is_database_busy() => Vec::new(),
                        other => other?,
                    };
                    for (input, text) in inputs {
                        if let Some(stdin) = proc.0.stdin.as_mut() {
                            let message = if machine {
                                if app_id.is_empty() {
                                    continue;
                                }
                                match text.as_str() {
                                "r" | "R" => json!([{"id":input,"method":"app.restart","params":{"appId":app_id,"fullRestart":text=="R","pause":false}}]).to_string(),
                                "q" => json!([{"id":input,"method":"app.stop","params":{"appId":app_id}}]).to_string(),
                                _ => return Err(Error::new("invalid_input","Unknown Flutter command")),
                            }
                            } else {
                                text
                            };
                            let _ = writeln!(stdin, "{message}");
                            let _ = stdin.flush();
                        }
                        retrying(Instant::now() + WORKER_RETRY, || {
                            Ok(db.execute("DELETE FROM inputs WHERE id=?", [input])?)
                        })?;
                    }
                }
                if b(step, "stoppable") && stopping.is_none() {
                    let asked = match (|| -> Result<Vec<i64>> {
                        Ok(db
                            .prepare("SELECT id FROM inputs WHERE job=? AND text=?")?
                            .query_map(params![id, STOP_INPUT], |r| r.get(0))?
                            .collect::<std::result::Result<Vec<_>, _>>()?)
                    })() {
                        Err(error) if error.is_database_busy() => Vec::new(),
                        other => other?,
                    };
                    if !asked.is_empty() {
                        for input in asked {
                            retrying(Instant::now() + WORKER_RETRY, || {
                                Ok(db.execute("DELETE FROM inputs WHERE id=?", [input])?)
                            })?;
                        }
                        writeln!(log, "\n[stop] ending early on request")?;
                        let stop = strings(&step["stop_argv"]);
                        if stop.is_empty() {
                            // SAFETY: kill receives the child's own process ID.
                            unsafe {
                                libc::kill(proc.0.id() as i32, libc::SIGINT);
                            }
                        } else if let Err(error) = toolchain::run(&stop, 15, None, false) {
                            writeln!(log, "[stop] {}", error.message)?;
                        }
                        stopping = Some(Instant::now() + Duration::from_secs(30));
                    }
                }
                if stopping.is_some_and(|end| Instant::now() >= end) {
                    writeln!(log, "[stop] the step did not end within 30 s")?;
                    toolchain::stop(&mut proc.0);
                    code = proc.0.wait()?.code().unwrap_or(-1);
                    break;
                }
                if let Some(value) = interrupt(&log)? {
                    state = value.into();
                    cancel_tree(db, id)?;
                    if value == "failed" {
                        result["error"] =
                            json!("Log exceeded 32 MiB; use shorter captures or filters");
                    }
                    toolchain::stop(&mut proc.0);
                    code = proc.0.wait()?.code().unwrap_or(-1);
                    break;
                }
                if capture_deadline.is_some_and(|end| Instant::now() >= end) {
                    toolchain::stop(&mut proc.0);
                    captured = true;
                    break;
                }
                thread::sleep(Duration::from_millis(50));
            }
            if captured {
                code = 0;
            }
            if state == "succeeded" && code != 0 {
                state = "failed".into();
            }
            drop(proc);
            if state != "succeeded" {
                break;
            }
            if let Some(output) = output {
                if fs::metadata(&output.temporary)?.len() < n(step, "minimum_bytes") {
                    return Err(Error::new(
                        "invalid_artifact",
                        "Captured artifact is empty or too small",
                    ));
                }
                if !s(step, "magic_hex").is_empty() {
                    let signature = s(step, "magic_hex");
                    if signature.len() > 64 || !signature.len().is_multiple_of(2) {
                        return Err(Error::new("invalid_job", "Invalid artifact signature"));
                    }
                    let expected = (0..signature.len())
                        .step_by(2)
                        .map(|i| u8::from_str_radix(&signature[i..i + 2], 16))
                        .collect::<std::result::Result<Vec<_>, _>>()
                        .map_err(|_| Error::new("invalid_job", "Invalid artifact signature"))?;
                    let mut prefix = vec![0; expected.len()];
                    std::io::Read::read_exact(&mut File::open(&output.temporary)?, &mut prefix)?;
                    if prefix != expected {
                        return Err(Error::new(
                            "invalid_artifact",
                            "Captured data does not match the expected file format",
                        ));
                    }
                }
                File::open(&output.temporary)?.sync_all()?;
                fs::rename(&output.temporary, &output.target)?;
                output_paths.insert(output.target.clone());
            }
            output_paths.extend(strings(&step["artifacts"]).into_iter().map(PathBuf::from));
        }
        attempts.push(json!({"attempt":attempt,"state":state,"exit_code":code,"started":attempt_started,"ended":now()}));
        let Some(delay) = retry_delay(&spec["retry"], attempt).filter(|_| state == "failed") else {
            break;
        };
        writeln!(
            log,
            "\n[failed] exit={code}; attempt {} of {} in {delay} s",
            attempt + 1,
            1 + n(&spec["retry"], "count")
        )?;
        retrying(Instant::now() + WORKER_RETRY, || {
            Ok(db.execute(
                "INSERT INTO events(job,time,kind,value,detail) VALUES(?,?,'retry',?,?)",
                params![
                    id,
                    now(),
                    (attempt + 1).to_string(),
                    json!({"exit_code":code,"delay_seconds":delay}).to_string()
                ],
            )?)
        })?;
        let resume = Instant::now() + Duration::from_secs(delay);
        let mut stopped = None;
        while Instant::now() < resume {
            stopped = interrupt(&log)?;
            if stopped.is_some() {
                break;
            }
            thread::sleep(Duration::from_millis(100));
        }
        if let Some(value) = stopped {
            state = value.into();
            break;
        }
        state = "succeeded".into();
        code = 0;
        output_paths.clear();
        attempt += 1;
    }
    if spec["retry"].is_object() {
        result["attempts"] = json!(attempts);
    }
    writeln!(log, "\n[{state}] exit={code}")?;
    if state == "succeeded" {
        for pattern in strings(&spec["artifacts"]) {
            output_paths.extend(glob_paths(&format!(
                "{}/{}",
                glob::Pattern::escape(s(spec, "cwd")),
                pattern
            ))?);
        }
    }
    // Test failures still produce useful reports. Only explicit diagnostic
    // patterns may publish files from an unsuccessful job.
    for pattern in strings(&spec["always_artifacts"]) {
        let pattern = if Path::new(&pattern).is_absolute() {
            pattern
        } else {
            format!("{}/{}", glob::Pattern::escape(s(spec, "cwd")), pattern)
        };
        output_paths.extend(glob_paths(&pattern)?);
    }
    let mut artifacts = Vec::new();
    for p in output_paths.into_iter().filter(|p| p.is_file()).take(100) {
        artifacts.push(artifact(&p, started)?);
    }
    result["artifacts"] = json!(artifacts);
    Ok((state, code, result))
}

#[cfg(test)]
mod flutter_session_tests {
    use super::*;

    #[test]
    fn a_flutter_session_names_its_vm_service_and_devtools() {
        let mut flutter = json!({});
        assert!(!flutter_uris(&mut flutter, "app.log", &json!({"log":"x"})));
        assert!(!flutter_uris(&mut flutter, "app.debugPort", &json!({})));
        assert!(flutter_uris(
            &mut flutter,
            "app.debugPort",
            &json!({"appId":"a","port":41815,"wsUri":"ws://127.0.0.1:41815/AbC_dE=/ws","baseUri":"file:///data/user/0/dev.x/code_cache/appXYZ/app/"})
        ));
        assert_eq!(flutter["vm_service_uri"], "ws://127.0.0.1:41815/AbC_dE=/ws");
        assert_eq!(
            flutter["vm_service_http_uri"],
            "http://127.0.0.1:41815/AbC_dE=/"
        );
        assert!(flutter.get("devtools_app_uri").is_none());
        assert!(flutter_uris(
            &mut flutter,
            "app.devTools",
            &json!({"uri":"http://127.0.0.1:9100"})
        ));
        assert_eq!(
            flutter["devtools_app_uri"],
            "http://127.0.0.1:9100?uri=http://127.0.0.1:41815/AbC_dE=/"
        );
        assert!(flutter_uris(
            &mut flutter,
            "app.dtd",
            &json!({"uri":"ws://127.0.0.1:9200/dtd"})
        ));
        assert_eq!(flutter["dtd_uri"], "ws://127.0.0.1:9200/dtd");
    }
}

#[cfg(test)]
mod database_tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn insert(db: &Connection, id: &str, state: &str, age: f64) {
        db.execute(
            "INSERT INTO jobs(id,label,state,created,updated,spec) VALUES(?,?,?,?,?,?)",
            params![
                id,
                "test",
                state,
                now() - age,
                now() - age,
                r#"{"steps":[]}"#
            ],
        )
        .unwrap();
    }
    fn cancel_flag(db: &Connection, id: &str) -> bool {
        get_row(db, id).unwrap().cancel
    }
    fn lease(root: &Path, id: &str) -> Lock {
        let lease = Lock::open(&root.join("jobs").join(format!("{id}.worker.lock"))).unwrap();
        assert!(lease.acquire(true).unwrap());
        lease
    }

    #[test]
    fn an_initialised_database_opens_without_schema_writes_or_locks() {
        let root = tempfile::tempdir().unwrap();
        let first = open(root.path()).unwrap();
        let version: i64 = first
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION);
        let mode = fs::metadata(root.path().join("jobs.sqlite3"))
            .unwrap()
            .permissions()
            .mode();
        assert_eq!(mode & 0o077, 0, "job records are owner-only");
        // Another client holds the write lock for as long as it likes.
        first.execute_batch("BEGIN IMMEDIATE").unwrap();
        insert(&first, &"a".repeat(32), "queued", 0.0);
        let started = Instant::now();
        let second = open(root.path()).unwrap();
        assert!(started.elapsed() < Duration::from_secs(2));
        assert_eq!(
            second
                .query_row("SELECT count(*) FROM jobs", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            0
        );
        first.execute_batch("COMMIT").unwrap();
    }

    #[test]
    fn an_older_database_is_upgraded_once() {
        let root = tempfile::tempdir().unwrap();
        {
            // As versions before the schema version created it.
            let old = Connection::open(root.path().join("jobs.sqlite3")).unwrap();
            old.execute_batch(&format!("PRAGMA journal_mode=WAL; {SCHEMA}"))
                .unwrap();
            insert(&old, &"b".repeat(32), "succeeded", 0.0);
        }
        let db = open(root.path()).unwrap();
        assert!(initialised(&db).unwrap());
        assert_eq!(
            s(&get_row(&db, &"b".repeat(32)).unwrap().value, "state"),
            "succeeded"
        );
    }

    #[test]
    fn a_reader_does_not_fail_while_another_connection_holds_the_write_lock() {
        let root = tempfile::tempdir().unwrap();
        let id = "c".repeat(32);
        let writer = open(root.path()).unwrap();
        // Looks abandoned, so the reader would like to record its failure.
        insert(&writer, &id, "queued", 60.0);
        writer.execute_batch("BEGIN IMMEDIATE").unwrap();
        let reader = open(root.path()).unwrap();
        reader.busy_timeout(Duration::from_millis(20)).unwrap();
        let release = thread::spawn(move || {
            thread::sleep(Duration::from_millis(300));
            writer.execute_batch("COMMIT").unwrap();
        });
        let job = retrying(Instant::now() + Duration::from_secs(10), || {
            read(&reader, &id)
        })
        .expect("a contended database is not an error for a reader");
        assert!(["queued", "failed"].contains(&s(&job, "state")));
        release.join().unwrap();
        // A later reader records the abandoned job once the lock is free.
        assert_eq!(s(&read(&reader, &id).unwrap(), "state"), "failed");
    }

    #[test]
    fn a_stale_queued_observation_does_not_cancel_a_worker_that_has_started() {
        let root = tempfile::tempdir().unwrap();
        let db = open(root.path()).unwrap();
        let id = "d".repeat(32);
        insert(&db, &id, "queued", 60.0);
        // A reader saw the job queued for over 30 seconds...
        let observed = get_row(&db, &id).unwrap();
        assert!(abandoned(&db, &observed).unwrap().is_some());
        // ...and while it waited for the write lock, the worker took its
        // lease and started running. The recovery decides on the row as it
        // is now (this race turned a live stop job `cancelled`, exit -1).
        let _worker = lease(root.path(), &id);
        db.execute("UPDATE jobs SET state='running' WHERE id=?", [&id])
            .unwrap();
        recover(&db, &id).unwrap();
        assert!(!cancel_flag(&db, &id));
        assert_eq!(s(&read(&db, &id).unwrap(), "state"), "running");
        // public() with the stale observation changes nothing either.
        public(&db, observed).unwrap();
        assert!(!cancel_flag(&db, &id));
        assert_eq!(s(&get_row(&db, &id).unwrap().value, "state"), "running");
    }

    #[test]
    fn a_slow_worker_holding_its_lease_is_not_failed_while_queued() {
        let root = tempfile::tempdir().unwrap();
        let db = open(root.path()).unwrap();
        let id = "e".repeat(32);
        insert(&db, &id, "queued", 60.0);
        let worker = lease(root.path(), &id);
        assert_eq!(s(&read(&db, &id).unwrap(), "state"), "queued");
        assert!(!cancel_flag(&db, &id));
        drop(worker);
        // Without its worker the job is abandoned and recorded as such.
        let job = read(&db, &id).unwrap();
        assert_eq!(s(&job, "state"), "failed");
        assert_eq!(
            s(&job["result"], "error"),
            "Worker did not start within 30 seconds"
        );
    }

    #[test]
    fn a_running_job_without_its_worker_is_failed_and_its_children_cancelled() {
        let root = tempfile::tempdir().unwrap();
        let db = open(root.path()).unwrap();
        let parent = "f".repeat(32);
        let child = "0".repeat(32);
        insert(&db, &parent, "running", 1.0);
        db.execute(
            "INSERT INTO jobs(id,label,state,created,updated,spec) VALUES(?,?,'running',?,?,?)",
            params![
                child,
                "child",
                now(),
                now(),
                json!({"steps":[],"parent_job":parent}).to_string()
            ],
        )
        .unwrap();
        let _child_worker = lease(root.path(), &child);
        // The parent's lease file exists but nobody holds it.
        drop(lease(root.path(), &parent));
        assert_eq!(s(&read(&db, &parent).unwrap(), "state"), "failed");
        assert!(cancel_flag(&db, &child));
        assert_eq!(s(&read(&db, &child).unwrap(), "state"), "running");
    }

    #[test]
    fn every_state_and_phase_change_is_recorded_as_an_event_in_order() {
        let root = tempfile::tempdir().unwrap();
        let db = open(root.path()).unwrap();
        let id = "1".repeat(32);
        insert(&db, &id, "queued", 0.0);
        let _worker = lease(root.path(), &id);
        let set = |sql: &str| {
            db.execute(sql, [&id]).unwrap();
        };
        set("UPDATE jobs SET state='running' WHERE id=?");
        set(r#"UPDATE jobs SET result='{"phase":"waiting"}' WHERE id=?"#);
        set(r#"UPDATE jobs SET result='{"phase":"waiting"}' WHERE id=?"#);
        set(r#"UPDATE jobs SET result='{"phase":"building"}' WHERE id=?"#);
        // A malformed result is no event, and no error.
        set("UPDATE jobs SET result='not json' WHERE id=?");
        cancel_tree(&db, &id).unwrap();
        set(
            r#"UPDATE jobs SET state='cancelled',exit_code=-1,result='{"error":"stopped"}' WHERE id=?"#,
        );
        let value = timeline(&db, &id, 0, 100).unwrap();
        let kinds: Vec<String> = value["events"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| format!("{}:{}", s(e, "kind"), s(e, "value")))
            .collect();
        assert_eq!(
            kinds,
            [
                "state:queued",
                "state:running",
                "phase:waiting",
                "phase:building",
                "cancel_requested:",
                "state:cancelled"
            ]
        );
        let events = value["events"].as_array().unwrap();
        assert_eq!(events[5]["exit_code"], -1);
        assert_eq!(s(&events[5], "error"), "stopped");
        assert!(
            events
                .windows(2)
                .all(|w| w[0]["time"].as_f64() <= w[1]["time"].as_f64())
        );
        assert!(b(&value, "recorded"));
        // Paging continues after the last event seen.
        let first = timeline(&db, &id, 0, 2).unwrap();
        assert_eq!(first["events"].as_array().unwrap().len(), 2);
        let rest = timeline(&db, &id, n(&first, "next_after"), 100).unwrap();
        assert_eq!(rest["events"].as_array().unwrap().len(), 4);
        let none = timeline(&db, &id, n(&rest, "next_after"), 100).unwrap();
        assert_eq!(none["events"], json!([]));
        assert_eq!(none["next_after"], rest["next_after"]);
        // Removing a job removes its events.
        set("DELETE FROM jobs WHERE id=?");
        let left: i64 = db
            .query_row("SELECT count(*) FROM events", [], |r| r.get(0))
            .unwrap();
        assert_eq!(left, 0);
    }

    #[test]
    fn a_job_from_before_events_gets_its_timeline_from_its_record() {
        let root = tempfile::tempdir().unwrap();
        let id = "2".repeat(32);
        {
            let old = Connection::open(root.path().join("jobs.sqlite3")).unwrap();
            old.execute_batch(&format!(
                "PRAGMA journal_mode=WAL; {SCHEMA} PRAGMA user_version=1;"
            ))
            .unwrap();
            insert(&old, &id, "succeeded", 5.0);
        }
        let db = open(root.path()).unwrap();
        let version: i64 = db
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION);
        let value = timeline(&db, &id, 0, 100).unwrap();
        assert!(!b(&value, "recorded"));
        let events = value["events"].as_array().unwrap();
        assert_eq!(events.len(), 2);
        assert!(events.iter().all(|e| b(e, "synthesized")));
        assert_eq!(s(&events[1], "value"), "succeeded");
        // New changes to it are recorded from now on.
        db.execute("UPDATE jobs SET state='failed' WHERE id=?", [&id])
            .unwrap();
        let value = timeline(&db, &id, 0, 100).unwrap();
        assert_eq!(value["events"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn listing_filters_by_state_operation_project_and_time_and_pages() {
        let root = tempfile::tempdir().unwrap();
        let db = open(root.path()).unwrap();
        let add = |id: &str, state: &str, age: f64, spec: Value| {
            db.execute(
                "INSERT INTO jobs(id,label,state,created,updated,spec) VALUES(?,?,?,?,?,?)",
                params![
                    id,
                    "test",
                    state,
                    1000.0 - age,
                    1000.0 - age,
                    spec.to_string()
                ],
            )
            .unwrap();
        };
        let (a, b_, c, d, e) = (
            "a".repeat(32),
            "b".repeat(32),
            "c".repeat(32),
            "d".repeat(32),
            "e".repeat(32),
        );
        add(
            &a,
            "succeeded",
            50.0,
            json!({"steps":[],"operation":"project.execute","cwd":"/work/app","exclusive":"/work/app"}),
        );
        add(
            &b_,
            "failed",
            40.0,
            json!({"steps":[],"operation":"project.run","cwd":"/work/app/android","exclusive":"/work/app"}),
        );
        add(
            &c,
            "failed",
            30.0,
            json!({"steps":[],"operation":"android.sdk_install","cwd":"/data"}),
        );
        add(
            &d,
            "succeeded",
            20.0,
            json!({"steps":[],"operation":"project_x.other","cwd":"/work/app2"}),
        );
        add(&e, "cancelled", 10.0, json!({"steps":[]}));
        let ids = |a: Value| -> Vec<String> {
            query(&db, &a)
                .unwrap()
                .as_array()
                .unwrap()
                .iter()
                .map(|j| s(j, "id")[..1].to_string())
                .collect()
        };
        assert_eq!(ids(json!({"limit":30})), ["e", "d", "c", "b", "a"]);
        assert_eq!(ids(json!({"limit":30,"state":["failed"]})), ["c", "b"]);
        // The prefix is literal: "_" is no wildcard.
        assert_eq!(ids(json!({"limit":30,"operation":"project."})), ["b", "a"]);
        assert_eq!(ids(json!({"limit":30,"operation":"project_"})), ["d"]);
        // A project matches its lock, its directory and below, not a sibling.
        assert_eq!(ids(json!({"limit":30,"project":"/work/app/"})), ["b", "a"]);
        assert_eq!(ids(json!({"limit":30,"since":970.0})), ["e", "d", "c"]);
        assert_eq!(
            ids(json!({"limit":30,"state":["failed","succeeded"],"since":955.0})),
            ["d", "c", "b"]
        );
        // Pages follow each other without gaps or repeats.
        assert_eq!(ids(json!({"limit":2})), ["e", "d"]);
        assert_eq!(ids(json!({"limit":2,"before":d})), ["c", "b"]);
        assert_eq!(ids(json!({"limit":2,"before":b_})), ["a"]);
        assert!(query(&db, &json!({"limit":2,"project":"relative"})).is_err());
        assert_eq!(
            query(&db, &json!({"limit":2,"before":"f".repeat(32)}))
                .unwrap_err()
                .code,
            "job_not_found"
        );
    }

    #[test]
    fn a_job_records_the_operation_that_submitted_it() {
        assert_eq!(requested_operation(), None);
        {
            let _outer = Request::enter("project.run", &Value::Null);
            assert_eq!(requested_operation().as_deref(), Some("project.run"));
            {
                let _inner = Request::enter(
                    "android.sdk_install",
                    &json!({"count":9,"backoff_seconds":0}),
                );
                assert_eq!(
                    requested_operation().as_deref(),
                    Some("android.sdk_install")
                );
                assert_eq!(
                    requested_retry(),
                    Some(json!({"count":5,"backoff_seconds":1}))
                );
            }
            assert_eq!(requested_operation().as_deref(), Some("project.run"));
            assert_eq!(requested_retry(), None);
        }
        assert_eq!(requested_operation(), None);
    }

    #[test]
    fn a_heavy_job_waits_queued_with_its_reason_until_a_slot_is_free() {
        let root = tempfile::tempdir().unwrap();
        let db = open(root.path()).unwrap();
        let (holder, waiting) = ("3".repeat(32), "4".repeat(32));
        let spec = json!({"steps":[],"resource":"build"});
        for (id, state) in [(&holder, "running"), (&waiting, "queued")] {
            db.execute(
                "INSERT INTO jobs(id,label,state,created,updated,spec) VALUES(?,?,?,?,?,?)",
                params![id, "Build", state, now(), now(), spec.to_string()],
            )
            .unwrap();
        }
        let _holder_worker = lease(root.path(), &holder);
        let first = free_slot(root.path(), "build", 2).unwrap().unwrap();
        let second = free_slot(root.path(), "build", 2).unwrap().unwrap();
        assert!(free_slot(root.path(), "build", 2).unwrap().is_none());
        // Another resource has its own slots.
        assert!(free_slot(root.path(), "emulator", 1).unwrap().is_some());
        let release = thread::spawn(move || {
            thread::sleep(Duration::from_millis(600));
            drop(first);
        });
        let admitted = admit(&db, root.path(), &waiting, &spec, || {
            Some(("max_builds", 2))
        })
        .unwrap();
        release.join().unwrap();
        assert!(matches!(admitted, Admission::Admitted(Some(_))));
        let _waiting_worker = lease(root.path(), &waiting);
        let job = read(&db, &waiting).unwrap();
        assert_eq!(s(&job, "state"), "queued");
        assert_eq!(s(&job, "phase"), "queued");
        let reason = s(&job, "queued_reason");
        assert!(
            reason.contains("2 of 2") && reason.contains(&holder),
            "{reason}"
        );
        assert!(reason.contains("jobs.max_builds"));
        let events = timeline(&db, &waiting, 0, 100).unwrap();
        assert!(
            events["events"]
                .as_array()
                .unwrap()
                .iter()
                .any(|e| s(e, "value") == "queued" && s(e, "kind") == "phase")
        );
        // Cancelling a queued job stops the wait.
        drop(admitted);
        let _third = free_slot(root.path(), "build", 2).unwrap().unwrap();
        cancel_tree(&db, &waiting).unwrap();
        let stopped = admit(&db, root.path(), &waiting, &spec, || {
            Some(("max_builds", 2))
        })
        .unwrap();
        assert!(matches!(stopped, Admission::Stopped));
        // Without a limit a job is admitted at once.
        let unlimited = admit(&db, root.path(), &waiting, &spec, || {
            Some(("max_builds", 0))
        })
        .unwrap();
        assert!(matches!(unlimited, Admission::Admitted(None)));
        drop(second);
    }

    #[test]
    fn retries_pause_longer_each_time_and_stop_at_the_count() {
        let retry = json!({"count":3,"backoff_seconds":4});
        assert_eq!(
            (1..=4).map(|a| retry_delay(&retry, a)).collect::<Vec<_>>(),
            [Some(4), Some(8), Some(16), None]
        );
        assert_eq!(retry_delay(&Value::Null, 1), None);
        assert_eq!(retry_delay(&json!({"count":0}), 1), None);
        // Never more than five retries, never longer than ten minutes.
        let greedy = json!({"count":50,"backoff_seconds":500});
        assert_eq!(retry_delay(&greedy, 2), Some(RETRY_PAUSE_LIMIT));
        assert_eq!(retry_delay(&greedy, 6), None);
    }

    #[test]
    fn resource_limits_come_from_settings_with_defaults() {
        assert_eq!(resource_limit(&json!({}), "build"), Some(("max_builds", 2)));
        assert_eq!(
            resource_limit(&json!({}), "emulator"),
            Some(("max_emulators", 0))
        );
        assert_eq!(
            resource_limit(&json!({"jobs":{"max_builds":5}}), "build"),
            Some(("max_builds", 5))
        );
        assert_eq!(resource_limit(&json!({}), "other"), None);
    }

    #[test]
    fn contention_is_retried_and_real_errors_are_not() {
        let mut calls = 0;
        let value = retrying(Instant::now() + Duration::from_secs(5), || {
            calls += 1;
            if calls < 3 {
                Err(Error::new("database_error", "database is locked")
                    .details(json!({"sqlite":"busy"})))
            } else {
                Ok(calls)
            }
        })
        .unwrap();
        assert_eq!(value, 3);
        let mut calls = 0;
        let error = retrying(Instant::now() + Duration::from_secs(5), || -> Result<()> {
            calls += 1;
            Err(Error::new("database_error", "malformed"))
        })
        .unwrap_err();
        assert_eq!((calls, error.code.as_str()), (1, "database_error"));
        // A busy SQLite error is recognised as contention.
        let busy = rusqlite::Error::SqliteFailure(
            rusqlite::ffi::Error::new(rusqlite::ffi::SQLITE_BUSY),
            None,
        );
        assert!(Error::from(busy).is_database_busy());
    }
}
