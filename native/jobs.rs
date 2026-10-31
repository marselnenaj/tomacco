use crate::{core::*, toolchain};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    fs::{self, File, OpenOptions},
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
const SCHEMA_VERSION: i64 = 1;
const SCHEMA: &str = "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY,label TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,spec TEXT NOT NULL,pid INTEGER,token TEXT,exit_code INTEGER,result TEXT,cancel INTEGER DEFAULT 0); CREATE TABLE IF NOT EXISTS inputs (id INTEGER PRIMARY KEY,job TEXT NOT NULL,text TEXT NOT NULL); CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created DESC); CREATE INDEX IF NOT EXISTS inputs_job ON inputs(job);";
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
        "BEGIN IMMEDIATE; {SCHEMA} PRAGMA user_version={SCHEMA_VERSION}; COMMIT;"
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
    let db = database()?;
    retrying(Instant::now() + READ_RETRY, || {
        let rows = db
            .prepare("SELECT * FROM jobs ORDER BY created DESC LIMIT ?")?
            .query_map([limit as i64], row)?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        Ok(json!(
            rows.into_iter()
                .map(|r| public(&db, r))
                .collect::<Result<Vec<_>>>()?
        ))
    })
}
pub fn submit(label: &str, mut spec: Value) -> Result {
    check_cancelled()?;
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
fn execute_worker(db: &Connection, id: &str, spec: &Value) -> Result<(String, i32, Value)> {
    let log_dir = home()?.join("jobs");
    private_dir(&log_dir)?;
    let mut log = OpenOptions::new()
        .create(true)
        .truncate(true)
        .read(true)
        .write(true)
        .open(log_dir.join(format!("{id}.log")))?;
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
    let publish_phase = |phase: &str, app_id: &str| -> Result<()> {
        retrying(Instant::now() + WORKER_RETRY, || {
            db.execute(
                "UPDATE jobs SET result=?,updated=? WHERE id=?",
                params![
                    json!({"phase":phase,"app_id":app_id}).to_string(),
                    now(),
                    id
                ],
            )?;
            Ok(())
        })
    };
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
        loop {
            if machine {
                let mut bytes = Vec::new();
                std::io::Read::by_ref(&mut event_file)
                    .take(256 * 1024)
                    .read_to_end(&mut bytes)?;
                event_buffer.push_str(&String::from_utf8_lossy(&bytes));
                while let Some(end) = event_buffer.find('\n') {
                    let line: String = event_buffer.drain(..=end).collect();
                    if let Ok(Value::Array(events)) = serde_json::from_str::<Value>(line.trim()) {
                        for event in events {
                            match s(&event, "event") {
                                "app.start" => {
                                    app_id = s(&event["params"], "appId").into();
                                    publish_phase(&phase, &app_id)?;
                                }
                                "app.started" => {
                                    phase = "ready".into();
                                    publish_phase(&phase, &app_id)?;
                                }
                                "app.stop" => {
                                    phase = "stopping".into();
                                    publish_phase(&phase, &app_id)?;
                                }
                                _ => (),
                            }
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
            if let Some(value) = interrupt(&log)? {
                state = value.into();
                cancel_tree(db, id)?;
                if value == "failed" {
                    result["error"] = json!("Log exceeded 32 MiB; use shorter captures or filters");
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
