//! Device coordination: expiring device reservations, the admission of each
//! live input against them, and (ledger.rs) a durable per-action request
//! ledger.
//!
//! A reservation names one canonical device (`platform/scope/device`, a
//! runtime session resolved to the device it shows) and is held through an
//! opaque capability, `reservation_token`, that only `acquire` (and a
//! `takeover` that acquires) answers. The database keeps its SHA-256 alone;
//! status, audit, errors and the ledger never carry the token. The owner,
//! task and reason are labels the caller gives: they describe the holder,
//! they do not authenticate it. The boundary is the operating-system
//! account: every local client of one user can read and write the same
//! owner-only files, so a reservation is a cooperative guard between
//! clients that use Tomacco's operations, not a lock against the user's own
//! processes, raw adb/SSH/fastboot or agents that bypass Tomacco.
//!
//! Every live input route passes `admit` right before it reaches the
//! transport: the runtime worker's device loop (runtime.input,
//! runtime.gesture, runtime.actions, screen.tap on a session and the
//! desktop's live view), `android.input` (and through it screen.tap,
//! testing.action and testing.run on an Android serial) and the virtual
//! iPhone's input controls. An unreserved device takes input as before. A
//! reserved one takes input only with its token; a token whose reservation
//! expired, was released or was revoked is refused with that reason. A
//! discrete input is recorded as admitted while it runs, and a touch stays
//! admitted from down to up (at most `CONTACT_SECONDS`), so an acquisition
//! waits for both to end instead of slipping in between. Releasing or
//! revoking stops what is queued, never what a device already received.
pub mod ledger;

use crate::core::*;
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use std::{
    cell::RefCell,
    fs,
    path::{Path, PathBuf},
    thread,
    time::{Duration, Instant},
};

/// The shortest, default and longest reservation, in milliseconds.
pub const TTL_MIN_MS: u64 = 1_000;
pub const TTL_DEFAULT_MS: u64 = 60_000;
pub const TTL_MAX_MS: u64 = 300_000;
/// How long `acquire` waits for admitted inputs by default, and at most.
const WAIT_DEFAULT_MS: u64 = 2_000;
const WAIT_MAX_MS: u64 = 30_000;
/// A discrete input counts as in flight at most this long, whatever its
/// process does; a touch from down to up at most `CONTACT_SECONDS`.
const ACTION_SECONDS: f64 = 120.0;
const CONTACT_SECONDS: f64 = 60.0;
/// Ended reservations kept so a stale token gets its reason, at most.
const ENDED_KEPT: i64 = 2048;
const ENDED_SECONDS: f64 = 86_400.0;
/// Devices with a reservation at once, at most.
const RESERVATIONS_MAX: i64 = 4096;
/// A reservation token: a fixed prefix, then 64 hexadecimal digits of
/// randomness. The prefix lets redaction recognise one anywhere.
pub const TOKEN_PREFIX: &str = "tmres_";
const SCHEMA_VERSION: i64 = 1;
const SCHEMA: &str = "CREATE TABLE IF NOT EXISTS reservations (device TEXT PRIMARY KEY, capability TEXT NOT NULL UNIQUE, owner TEXT NOT NULL, task TEXT NOT NULL, reason TEXT NOT NULL, client TEXT NOT NULL, channel TEXT NOT NULL, acquired REAL NOT NULL, renewed REAL NOT NULL, expires REAL NOT NULL, ttl_ms INTEGER NOT NULL); \
CREATE TABLE IF NOT EXISTS ended (capability TEXT PRIMARY KEY, device TEXT NOT NULL, owner TEXT NOT NULL, task TEXT NOT NULL, how TEXT NOT NULL, at REAL NOT NULL, note TEXT NOT NULL); \
CREATE INDEX IF NOT EXISTS ended_at ON ended(at); \
CREATE TABLE IF NOT EXISTS admissions (id TEXT PRIMARY KEY, device TEXT NOT NULL, kind TEXT NOT NULL, capability TEXT NOT NULL, pid INTEGER NOT NULL, token TEXT NOT NULL, started REAL NOT NULL, expires REAL NOT NULL); \
CREATE INDEX IF NOT EXISTS admissions_device ON admissions(device);";

fn directory() -> Result<PathBuf> {
    let dir = home()?.join("coordination");
    private_dir(&dir)?;
    Ok(dir)
}

/// Files are the owner's alone, also where an older umask was wider.
pub(crate) fn restrict(database: &Path) {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    for suffix in ["", "-wal", "-shm"] {
        let file = PathBuf::from(format!("{}{suffix}", database.display()));
        if let Ok(metadata) = fs::symlink_metadata(&file) {
            if metadata.is_file() && metadata.mode() & 0o077 != 0 {
                let _ = fs::set_permissions(&file, fs::Permissions::from_mode(0o600));
            }
        }
    }
}

/// Opens (and on first use creates) one of the coordination databases in
/// WAL mode. `durable` makes every commit reach the disk before it returns:
/// the ledger needs that, the reservations do not (a crash of the process
/// loses nothing in WAL mode; a power cut at most loses a reservation, whose
/// token is then refused as unknown).
pub(crate) fn open_database(
    name: &str,
    schema: &str,
    version: i64,
    durable: bool,
) -> Result<Connection> {
    open_path(&directory()?.join(name), schema, version, durable)
}
pub(crate) fn open_path(
    path: &Path,
    schema: &str,
    version: i64,
    durable: bool,
) -> Result<Connection> {
    use std::os::unix::fs::OpenOptionsExt;
    let path = path.to_path_buf();
    let dir = path.parent().map(Path::to_path_buf).unwrap_or_default();
    // Created owner-only before SQLite opens it with the umask.
    let _ = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(&path)?;
    let db = Connection::open(&path)?;
    db.busy_timeout(Duration::from_secs(10))?;
    let current: i64 = db.query_row("PRAGMA user_version", [], |r| r.get(0))?;
    let mode: String = db.query_row("PRAGMA journal_mode", [], |r| r.get(0))?;
    if current < version || !mode.eq_ignore_ascii_case("wal") {
        let lock = Lock::open(&dir.join(format!(
            "{}.init.lock",
            path.file_name().unwrap_or_default().to_string_lossy()
        )))?;
        lock.acquire(false)?;
        db.execute_batch("PRAGMA journal_mode=WAL;")?;
        db.execute_batch(&format!(
            "BEGIN IMMEDIATE; {schema} PRAGMA user_version={version}; COMMIT;"
        ))
        .inspect_err(|_| {
            let _ = db.execute_batch("ROLLBACK;");
        })?;
    }
    db.execute_batch(if durable {
        "PRAGMA synchronous=FULL;"
    } else {
        "PRAGMA synchronous=NORMAL;"
    })?;
    restrict(&path);
    Ok(db)
}

thread_local! {
    /// One connection per thread, opened on first use and kept, so the
    /// runtime worker's input loop does not open a database per touch.
    static RESERVATIONS: RefCell<Option<(PathBuf, Connection)>> = const { RefCell::new(None) };
    /// Canonical devices whose input this thread has admitted and is
    /// delivering now; a nested admission of the same device passes.
    static HELD: RefCell<Vec<String>> = const { RefCell::new(Vec::new()) };
    /// The reservation token and request identity of the call this thread
    /// serves (`Context`), handed to the runtime worker with each input.
    static CONTEXT: RefCell<Context> = RefCell::new(Context::default());
}

/// Runs `action` with this thread's reservation database.
fn with_db<T>(action: impl FnOnce(&Connection) -> Result<T>) -> Result<T> {
    let root = home()?;
    RESERVATIONS.with(|cell| {
        let mut slot = cell.borrow_mut();
        if slot.as_ref().is_none_or(|(at, _)| at != &root) {
            *slot = Some((
                root.clone(),
                open_database("reservations.sqlite3", SCHEMA, SCHEMA_VERSION, false)?,
            ));
        }
        action(&slot.as_ref().unwrap().1)
    })
}

/// Runs `action` inside one IMMEDIATE transaction: it holds the database's
/// write lock from its first read, so a check and the write that follows it
/// cannot interleave with another process's.
fn transaction<T>(db: &Connection, action: impl FnOnce(&Connection) -> Result<T>) -> Result<T> {
    db.execute_batch("BEGIN IMMEDIATE;")?;
    match action(db) {
        Ok(value) => {
            db.execute_batch("COMMIT;").inspect_err(|_| {
                let _ = db.execute_batch("ROLLBACK;");
            })?;
            Ok(value)
        }
        Err(error) => {
            let _ = db.execute_batch("ROLLBACK;");
            Err(error)
        }
    }
}

/// The start time of a process, from /proc: with the PID it names one
/// process, also after the PID is reused. Empty for a process that is gone.
pub(crate) fn process_token(pid: u32) -> String {
    fs::read_to_string(format!("/proc/{pid}/stat"))
        .ok()
        .and_then(|text| {
            text.rsplit_once(')').map(|(_, fields)| {
                if fields.split_whitespace().next() == Some("Z") {
                    String::new()
                } else {
                    fields.split_whitespace().nth(19).unwrap_or("").to_owned()
                }
            })
        })
        .unwrap_or_default()
}
pub(crate) fn alive(pid: i64, token: &str) -> bool {
    pid > 0 && !token.is_empty() && process_token(pid as u32) == token
}

/// The SHA-256 a token is kept and compared as.
fn fingerprint(token: &str) -> String {
    digest(token.as_bytes())
}

fn new_token() -> String {
    format!(
        "{TOKEN_PREFIX}{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    )
}

/// A token as given: the prefix and 64 lowercase hexadecimal digits.
fn checked_token(token: &str) -> Result<&str> {
    let well_formed = token.len() == TOKEN_PREFIX.len() + 64
        && token.starts_with(TOKEN_PREFIX)
        && token[TOKEN_PREFIX.len()..]
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c));
    if !well_formed {
        // The value is never quoted: it may be a secret pasted in the wrong place.
        return Err(Error::new(
            "invalid_argument",
            "reservation_token is not a token device.reservation.acquire answered",
        )
        .remedy("Pass the reservation_token exactly as device.reservation.acquire answered it, or acquire a new reservation"));
    }
    Ok(token)
}

// ---- The call's context ------------------------------------------------------

/// What a participating call carries besides its arguments: the reservation
/// token it acts under and the ledger request it executes for. Both are
/// taken out of the arguments by `api::dispatch`, so no handler, job record
/// or worker queue sees the token among the arguments.
#[derive(Clone, Default, Debug)]
pub struct Context {
    pub token: Option<String>,
    /// The ledger request this call executes; each input it hands to the
    /// runtime worker is recorded there as one of its children.
    pub parent: Option<String>,
    /// The name of the next child (a batch step), else it is numbered.
    pub label: Option<String>,
    counter: u32,
}

/// The context of the call this thread serves.
pub fn context() -> Context {
    CONTEXT.with(|c| c.borrow().clone())
}

/// Runs `action` in `context`, and restores the one before afterwards.
pub fn with_context<T>(context: Context, action: impl FnOnce() -> T) -> T {
    let previous = CONTEXT.with(|c| c.replace(context));
    struct Restore(Option<Context>);
    impl Drop for Restore {
        fn drop(&mut self) {
            if let Some(previous) = self.0.take() {
                CONTEXT.with(|c| c.replace(previous));
            }
        }
    }
    let _restore = Restore(Some(previous));
    action()
}

/// Names the children the call creates from now on (`None` numbers them).
pub fn set_label(label: Option<String>) {
    CONTEXT.with(|c| c.borrow_mut().label = label);
}

/// The ledger identity of the next input this call hands to the runtime
/// worker: `PARENT/LABEL`, or `PARENT/N` counting from 1.
fn next_child() -> Option<String> {
    CONTEXT.with(|c| {
        let mut context = c.borrow_mut();
        let parent = context.parent.clone()?;
        context.counter += 1;
        Some(match &context.label {
            Some(label) => format!("{parent}/{label}"),
            None => format!("{parent}/{}", context.counter),
        })
    })
}

/// The `coordination` member of a control-socket request for an input:
/// the token and the child request identity. Null when there is neither.
pub fn worker_block() -> Value {
    let token = context().token;
    let request = next_child();
    if token.is_none() && request.is_none() {
        return Value::Null;
    }
    json!({"reservation_token":token,"request_id":request})
}

// ---- Canonical devices --------------------------------------------------------

/// One device as coordination knows it, whatever name a call used for it:
/// a runtime session is the device it shows, a nickname the device it names.
#[derive(Clone, Debug, PartialEq)]
pub struct Target {
    /// `platform/scope/device`, scope `local` or `remote:NAME`.
    pub key: String,
    pub platform: String,
    pub remote: String,
    pub device: String,
    /// The runtime session the call named, if it named one.
    pub session: Option<String>,
    /// The session's transport (`fixture`, `adb/scrcpy`, ...) when known.
    pub transport: String,
    /// How the name was resolved: id, session, nickname, virtual.
    pub resolved_from: &'static str,
    pub platform_inferred: bool,
}

impl Target {
    pub fn new(platform: &str, remote: &str, device: &str) -> Self {
        let scope = if remote.is_empty() {
            "local".to_string()
        } else {
            format!("remote:{remote}")
        };
        Target {
            key: format!("{platform}/{scope}/{device}"),
            platform: platform.to_string(),
            remote: remote.to_string(),
            device: device.to_string(),
            session: None,
            transport: String::new(),
            resolved_from: "id",
            platform_inferred: false,
        }
    }
    pub fn value(&self) -> Value {
        json!({"key":self.key,"platform":self.platform,"remote":if self.remote.is_empty() { Value::Null } else { json!(self.remote) },
            "device":self.device,"session":self.session,"resolved_from":self.resolved_from,
            "platform_inferred":self.platform_inferred})
    }
}

/// The virtual iPhone, one per data directory whatever job runs it.
pub fn virtual_iphone() -> Target {
    let mut target = Target::new("ios", "", crate::ios_emulator::ui_testing::DEVICE);
    target.resolved_from = "virtual";
    target
}

/// The device a runtime session shows, from its record (also once it ended).
pub fn session_target(id: &str) -> Result<Target> {
    let record = crate::runtime::session_record(id).map_err(|_| {
        Error::new(
            "device_target_unknown",
            format!("No runtime session {id} is recorded here"),
        )
        .remedy("Use a session ID from runtime.list, or name the device by its serial or UDID with platform")
    })?;
    let mut target = Target::new(
        s(&record, "platform"),
        s(&record, "remote"),
        s(&record, "device"),
    );
    target.session = Some(id.to_string());
    target.transport = s(&record, "transport").to_string();
    target.resolved_from = "session";
    Ok(target)
}

fn udid_shaped(device: &str) -> bool {
    let hex = |text: &str| text.bytes().all(|c| c.is_ascii_hexdigit());
    (device.len() == 40 && hex(device))
        || (device.len() == 25
            && device.as_bytes()[8] == b'-'
            && hex(&device[..8])
            && hex(&device[9..]))
}

/// The device a nickname given with device.label names, offline: labels
/// are kept by serial or UDID, except an emulator's, kept by its AVD name,
/// whose serial only adb can tell.
fn nickname(device: &str) -> Result<Option<String>> {
    let labels = read_json(&home()?.join("device-labels.json"), Some(json!({})))?;
    let Some(labels) = labels.as_object() else {
        return Ok(None);
    };
    if labels.contains_key(device) {
        return Ok(None);
    }
    let wanted = device.to_lowercase();
    let found: Vec<&String> = labels
        .iter()
        .filter(|(_, label)| s(label, "nickname").to_lowercase() == wanted)
        .map(|(key, _)| key)
        .collect();
    match found.as_slice() {
        [] => Ok(None),
        [key] if key.starts_with("avd:") => Err(Error::new(
            "device_target_unknown",
            format!("{device:?} is the nickname of an emulator, whose serial changes with its port"),
        )
        .remedy("Name the emulator by its serial from device.list (emulator-5554), or by its runtime session ID")),
        [key] => Ok(Some(key.to_string())),
        _ => Err(Error::new(
            "device_target_unknown",
            format!("{} devices have the nickname {device:?}; nothing was chosen", found.len()),
        )
        .remedy("Name the device by its serial or UDID from device.list")),
    }
}

/// The canonical device `device` names: a runtime session ID, the virtual
/// iPhone, a nickname, or a serial or UDID on `platform` (inferred when
/// empty: iOS for a remote or a UDID, Android otherwise) and `remote`.
pub fn target(device: &str, platform: &str, remote: &str) -> Result<Target> {
    if crate::screen::is_session(device) {
        let found = session_target(device)?;
        if (!platform.is_empty() && platform != found.platform)
            || (!remote.is_empty() && remote != found.remote)
        {
            return Err(Error::new(
                "invalid_argument",
                format!(
                    "Session {device} shows {}, not the platform or remote given",
                    found.key
                ),
            ));
        }
        return Ok(found);
    }
    if crate::ios_emulator::ui_testing::is_virtual(device) {
        return Ok(virtual_iphone());
    }
    identifier(device, "device", None)?;
    if !remote.is_empty() {
        identifier(remote, "remote", None)?;
    }
    let (id, from) = match nickname(device)? {
        Some(id) => (id, "nickname"),
        None => (device.to_string(), "id"),
    };
    let inferred = platform.is_empty();
    let platform = match platform {
        "" if !remote.is_empty() || udid_shaped(&id) => "ios",
        "" => "android",
        "android" | "ios" => platform,
        other => {
            return Err(Error::new(
                "invalid_argument",
                format!("Unknown platform {other:?}"),
            ));
        }
    };
    let mut found = Target::new(platform, remote, &id);
    found.resolved_from = from;
    found.platform_inferred = inferred;
    Ok(found)
}

/// The target named by a coordination operation's `device`, `platform`
/// and `remote` arguments.
fn target_of(a: &Value) -> Result<Target> {
    target(s(a, "device"), s(a, "platform"), s(a, "remote"))
}

// ---- Reservations -------------------------------------------------------------

/// What every answer about a reservation says about its strength.
pub const BOUNDARY: &str = "Cooperative: Tomacco's own input routes honour it for every client of this user account. The token is the capability; owner, task and reason are labels, not authentication. Raw adb, SSH, fastboot and tools that bypass Tomacco are not governed.";

#[derive(Clone, Debug)]
struct Holder {
    capability: String,
    owner: String,
    task: String,
    reason: String,
    client: String,
    channel: String,
    acquired: f64,
    renewed: f64,
    expires: f64,
    ttl_ms: u64,
}

impl Holder {
    /// The holder as status and errors show it: never the capability.
    fn public(&self, at: f64) -> Value {
        json!({"owner":self.owner,"task":self.task,"reason":self.reason,"client":self.client,"channel":self.channel,
            "acquired_at":self.acquired,"renewed_at":self.renewed,"expires_at":self.expires,
            "expires_in_ms":((self.expires - at) * 1000.0).max(0.0).round() as u64,"ttl_ms":self.ttl_ms})
    }
}

fn holder(db: &Connection, key: &str) -> Result<Option<Holder>> {
    Ok(db
        .query_row(
            "SELECT capability, owner, task, reason, client, channel, acquired, renewed, expires, ttl_ms FROM reservations WHERE device=?1",
            params![key],
            |r| {
                Ok(Holder {
                    capability: r.get(0)?,
                    owner: r.get(1)?,
                    task: r.get(2)?,
                    reason: r.get(3)?,
                    client: r.get(4)?,
                    channel: r.get(5)?,
                    acquired: r.get(6)?,
                    renewed: r.get(7)?,
                    expires: r.get(8)?,
                    ttl_ms: r.get::<_, i64>(9)? as u64,
                })
            },
        )
        .optional()?)
}

fn end(db: &Connection, key: &str, holder: &Holder, how: &str, at: f64, note: &str) -> Result<()> {
    db.execute("DELETE FROM reservations WHERE device=?1", params![key])?;
    db.execute(
        "INSERT OR REPLACE INTO ended (capability, device, owner, task, how, at, note) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![holder.capability, key, holder.owner, holder.task, how, at, note.chars().take(500).collect::<String>()],
    )?;
    Ok(())
}

/// Ends every reservation whose time is up, and forgets stale admissions
/// of `key` (a process that is gone, a touch held past its bound).
fn purge(db: &Connection, key: &str, at: f64) -> Result<()> {
    let expired: Vec<(String, f64)> = {
        let mut statement =
            db.prepare_cached("SELECT device, expires FROM reservations WHERE expires<=?1")?;
        statement
            .query_map(params![at], |r| Ok((r.get(0)?, r.get(1)?)))?
            .collect::<std::result::Result<_, _>>()?
    };
    for (device, expires) in expired {
        if let Some(found) = holder(db, &device)? {
            end(db, &device, &found, "expired", expires, "its time ran out")?;
        }
    }
    let rows: Vec<(String, i64, String, f64)> = {
        let mut statement =
            db.prepare_cached("SELECT id, pid, token, expires FROM admissions WHERE device=?1")?;
        statement
            .query_map(params![key], |r| {
                Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))
            })?
            .collect::<std::result::Result<_, _>>()?
    };
    for (id, pid, token, expires) in rows {
        if expires <= at || (pid > 0 && !alive(pid, &token)) {
            db.execute("DELETE FROM admissions WHERE id=?1", params![id])?;
        }
    }
    Ok(())
}

fn prune_ended(db: &Connection, at: f64) -> Result<()> {
    db.execute("DELETE FROM ended WHERE at<?1", params![at - ENDED_SECONDS])?;
    db.execute(
        "DELETE FROM ended WHERE capability NOT IN (SELECT capability FROM ended ORDER BY at DESC LIMIT ?1)",
        params![ENDED_KEPT],
    )?;
    db.execute("DELETE FROM admissions WHERE expires<=?1", params![at])?;
    Ok(())
}

/// Inputs admitted on `key` that still run: discrete ones and held touches.
#[derive(Default, Debug)]
struct Busy {
    actions: u64,
    contacts: u64,
}
fn in_flight(db: &Connection, key: &str, at: f64) -> Result<Busy> {
    let mut busy = Busy::default();
    let mut statement =
        db.prepare_cached("SELECT kind, pid, token, expires FROM admissions WHERE device=?1")?;
    let rows = statement.query_map(params![key], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, i64>(1)?,
            r.get::<_, String>(2)?,
            r.get::<_, f64>(3)?,
        ))
    })?;
    for row in rows {
        let (kind, pid, token, expires) = row?;
        if expires <= at || (pid > 0 && !alive(pid, &token)) {
            continue;
        }
        if kind == "contact" {
            busy.contacts += 1;
        } else {
            busy.actions += 1;
        }
    }
    Ok(busy)
}

fn reserved(target: &Target, holder: &Holder, at: f64) -> Error {
    let task = if holder.task.is_empty() {
        String::new()
    } else {
        format!(" for {:?}", holder.task)
    };
    Error::new(
        "device_reserved",
        format!(
            "{} is reserved by {:?}{task} for {} more seconds; nothing was sent",
            target.key,
            holder.owner,
            ((holder.expires - at).max(0.0)).ceil() as u64
        ),
    )
    .remedy("Wait until it expires or its holder releases it (device.reservation.get shows who holds it and until when); a person can take it over with device.reservation.takeover. Reading the screen stays possible")
    .details(json!({"device":target.value(),"held_by":holder.public(at)}))
}

/// The refusal of a token that does not hold `target` now.
fn stale(
    db: &Connection,
    hash: &str,
    target: &Target,
    current: Option<&Holder>,
    at: f64,
) -> Result<Error> {
    let ended = db
        .query_row(
            "SELECT device, owner, task, how, at, note FROM ended WHERE capability=?1",
            params![hash],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, f64>(4)?,
                    r.get::<_, String>(5)?,
                ))
            },
        )
        .optional()?;
    let held_by = current.map(|h| h.public(at));
    let error = match ended {
        Some((device, owner, task, how, when, note)) if device == target.key => {
            let sent = "this input was not sent";
            match how.as_str() {
                "expired" => Error::new(
                    "reservation_expired",
                    format!("The reservation of {} expired; {sent}", target.key),
                ),
                "revoked" => Error::new(
                    "reservation_revoked",
                    format!("The reservation of {} was taken over; {sent}", target.key),
                ),
                _ => Error::new(
                    "reservation_released",
                    format!("The reservation of {} was released; {sent}", target.key),
                ),
            }
            .remedy("Inputs sent before it ended reached the device and are not undone. Observe the screen, then acquire a new reservation with device.reservation.acquire before acting again")
            .details(json!({"device":target.value(),"ended":{"how":how,"at":when,"owner":owner,"task":task,"note":note},"held_by":held_by}))
        }
        Some(_) => Error::new(
            "reservation_not_held",
            format!("This reservation_token belongs to another device, not {}; nothing was sent", target.key),
        )
        .remedy("Use the token device.reservation.acquire answered for this device, or acquire one for it")
        .details(json!({"device":target.value(),"held_by":held_by})),
        None => Error::new(
            "reservation_not_held",
            format!("This reservation_token holds no reservation of {}; nothing was sent", target.key),
        )
        .remedy("Acquire a reservation with device.reservation.acquire and use the token it answers; a token from another data directory or a forgotten reservation is not known here")
        .details(json!({"device":target.value(),"held_by":held_by})),
    };
    Ok(error)
}

/// Whether the caller (holding `hash`, or nothing) may send input to
/// `target` now. `write` ends expired reservations on the way; a read-only
/// check treats them as ended without writing.
fn decide(
    db: &Connection,
    target: &Target,
    hash: Option<&str>,
    at: f64,
    write: bool,
) -> Result<Option<Holder>> {
    if write {
        purge(db, &target.key, at)?;
    }
    let current = holder(db, &target.key)?;
    let (current, expired_own) = match current {
        Some(h) if h.expires <= at => {
            let own = hash == Some(h.capability.as_str());
            (None, own.then_some(h))
        }
        other => (other, None),
    };
    if let Some(h) = expired_own {
        return Err(Error::new(
            "reservation_expired",
            format!("The reservation of {} expired; this input was not sent", target.key),
        )
        .remedy("Inputs sent before it ended reached the device and are not undone. Observe the screen, then acquire a new reservation with device.reservation.acquire before acting again")
        .details(json!({"device":target.value(),"ended":{"how":"expired","at":h.expires,"owner":h.owner,"task":h.task}})));
    }
    match (hash, current) {
        (Some(hash), Some(h)) if h.capability == hash => Ok(Some(h)),
        (Some(hash), current) => Err(stale(db, hash, target, current.as_ref(), at)?),
        (None, Some(h)) => Err(reserved(target, &h, at)),
        (None, None) => Ok(None),
    }
}

/// The read-only check before a call queues or prepares an input: the same
/// answer `admit` gives, without admitting anything. A call refused here
/// sent nothing.
pub fn check(target: &Target, token: Option<&str>) -> Result<()> {
    if let Some(token) = token {
        checked_token(token)?;
    }
    let hash = token.map(fingerprint);
    with_db(|db| decide(db, target, hash.as_deref(), now(), false).map(|_| ()))
}

/// What an input is, for its admission.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Kind {
    /// A tap, swipe, key, text, gesture...: admitted while it runs.
    Action,
    /// A finger down: admitted until its up or cancel.
    ContactBegin,
    /// A move of a finger that is down.
    ContactContinue,
    /// The finger's up.
    ContactEnd,
    /// Lifting a finger without completing it: always allowed, so no
    /// finger is left down on a device when a reservation begins.
    ContactCancel,
    /// A pointer that moves without a button (AirPlay hover): checked,
    /// not recorded, as it cannot press anything.
    Pointer,
}

impl Kind {
    /// The kind of a `runtime.input` (or a control-socket input).
    pub fn of_input(a: &Value) -> Kind {
        match (s(a, "action"), s(a, "phase")) {
            ("touch", "down") => Kind::ContactBegin,
            ("touch", "move") => Kind::ContactContinue,
            ("touch", "up") => Kind::ContactEnd,
            ("touch", "cancel") => Kind::ContactCancel,
            ("touch", "hover") => Kind::Pointer,
            _ => Kind::Action,
        }
    }
}

/// An input admitted on a device; dropped, its record goes.
#[must_use]
pub struct Admission {
    id: Option<String>,
    key: String,
    nested: bool,
}

impl Drop for Admission {
    fn drop(&mut self) {
        if self.nested {
            return;
        }
        HELD.with(|held| {
            let mut held = held.borrow_mut();
            if let Some(at) = held.iter().rposition(|k| k == &self.key) {
                held.remove(at);
            }
        });
        if let Some(id) = self.id.take() {
            let _ = with_db(|db| {
                db.execute("DELETE FROM admissions WHERE id=?1", params![id])?;
                Ok(())
            });
        }
    }
}

fn contact_id(key: &str, hash: Option<&str>) -> String {
    format!("contact:{key}:{}", hash.unwrap_or("none"))
}

fn admit_in(
    db: &Connection,
    target: &Target,
    kind: Kind,
    hash: Option<&str>,
    process_bound: bool,
    at: f64,
) -> Result<Option<String>> {
    transaction(db, |db| {
        let contact = contact_id(&target.key, hash);
        let open = || -> Result<bool> {
            Ok(db
                .query_row(
                    "SELECT pid, token FROM admissions WHERE id=?1 AND expires>?2",
                    params![contact, at],
                    |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)),
                )
                .optional()?
                .is_some_and(|(pid, token)| pid == 0 || alive(pid, &token)))
        };
        match kind {
            Kind::ContactCancel => {
                db.execute("DELETE FROM admissions WHERE id=?1", params![contact])?;
                return Ok(None);
            }
            Kind::ContactContinue if open()? => return Ok(None),
            Kind::ContactEnd if open()? => {
                db.execute("DELETE FROM admissions WHERE id=?1", params![contact])?;
                return Ok(None);
            }
            _ => (),
        }
        decide(db, target, hash, at, true)?;
        let (pid, token) = if process_bound || kind == Kind::Action {
            let pid = std::process::id();
            (i64::from(pid), process_token(pid))
        } else {
            (0, String::new())
        };
        match kind {
            Kind::ContactBegin => {
                db.execute(
                    "INSERT OR REPLACE INTO admissions (id, device, kind, capability, pid, token, started, expires) VALUES (?1, ?2, 'contact', ?3, ?4, ?5, ?6, ?7)",
                    params![contact, target.key, hash.unwrap_or(""), pid, token, at, at + CONTACT_SECONDS],
                )?;
                Ok(None)
            }
            Kind::Action => {
                let id = uuid::Uuid::new_v4().simple().to_string();
                db.execute(
                    "INSERT INTO admissions (id, device, kind, capability, pid, token, started, expires) VALUES (?1, ?2, 'action', ?3, ?4, ?5, ?6, ?7)",
                    params![id, target.key, hash.unwrap_or(""), pid, token, at, at + ACTION_SECONDS],
                )?;
                Ok(Some(id))
            }
            _ => Ok(None),
        }
    })
}

/// Admits one input to `target` right before it is delivered: refused when
/// another holds the device or the token's reservation ended, else recorded
/// as in flight until the returned admission drops. `process_bound` ties a
/// held touch to this process (the runtime worker, which outlives it);
/// a call whose process ends after each event leaves it to the time bound.
pub fn admit(
    target: &Target,
    kind: Kind,
    token: Option<&str>,
    process_bound: bool,
) -> Result<Admission> {
    if let Some(token) = token {
        checked_token(token)?;
    }
    let nested = HELD.with(|held| held.borrow().iter().any(|k| k == &target.key));
    if nested {
        return Ok(Admission {
            id: None,
            key: target.key.clone(),
            nested: true,
        });
    }
    let hash = token.map(fingerprint);
    let id = with_db(|db| admit_in(db, target, kind, hash.as_deref(), process_bound, now()))?;
    HELD.with(|held| held.borrow_mut().push(target.key.clone()));
    Ok(Admission {
        id,
        key: target.key.clone(),
        nested: false,
    })
}

/// Runs `deliver` as one admitted input of `target` under the call's token.
pub fn guarded<T>(target: &Target, kind: Kind, deliver: impl FnOnce() -> Result<T>) -> Result<T> {
    let token = context().token;
    let _admission = admit(target, kind, token.as_deref(), false)?;
    deliver()
}

/// One input in the runtime worker's device loop, right before the device
/// gets it: recorded under its child identity when the caller gave one
/// (and answered from that record if it already ran), then admitted under
/// the caller's token, then delivered. Inputs without a `coordination`
/// block (the desktop's live view) are admitted without a token.
#[allow(clippy::too_many_arguments)]
pub fn worker_input(
    target: &Target,
    transport: &str,
    instance: &str,
    operation: &str,
    a: &Value,
    block: &Value,
    kind: Kind,
    deliver: impl FnOnce() -> Result,
) -> Result {
    let token = block["reservation_token"].as_str();
    let d = ledger::Dispatch {
        operation,
        target,
        route: "runtime-worker",
        transport: transport.to_string(),
        instance: instance.to_string(),
    };
    ledger::run_child(s(block, "request_id"), &d, a, || {
        let _admission = admit(target, kind, token, true)?;
        deliver()
    })
}

// ---- Operations ---------------------------------------------------------------

/// The channel and client name the caller reported, recorded with a
/// reservation for people to read. Neither is authenticated.
fn caller() -> (String, String) {
    let caller = crate::guard::current();
    let channel = match caller.channel {
        Some(crate::guard::Channel::Mcp) => "mcp",
        Some(crate::guard::Channel::Cli) => "cli",
        None if std::env::var(crate::guard::CALLER_VARIABLE).as_deref() == Ok("desktop") => {
            "desktop"
        }
        None => "direct",
    };
    (channel.to_string(), caller.client)
}

fn label(a: &Value, key: &str, limit: usize) -> String {
    s(a, key)
        .trim()
        .chars()
        .filter(|c| !c.is_control())
        .take(limit)
        .collect()
}

fn ttl(a: &Value) -> u64 {
    a["ttl_ms"]
        .as_u64()
        .unwrap_or(TTL_DEFAULT_MS)
        .clamp(TTL_MIN_MS, TTL_MAX_MS)
}

fn in_flight_error(target: &Target, busy: &Busy, waited_ms: u64) -> Error {
    Error::new(
        "device_input_in_flight",
        format!(
            "Another client's input to {} is under way ({} inputs, {} touches held down) after waiting {waited_ms} ms; nothing was reserved",
            target.key, busy.actions, busy.contacts
        ),
    )
    .remedy("Try again in a moment or with a longer wait_ms; a touch held down counts until it is lifted, at most 60 seconds")
    .details(json!({"device":target.value(),"in_flight":{"inputs":busy.actions,"touches":busy.contacts},"waited_ms":waited_ms}))
}

/// One attempt to take `target`: refused when held, `Err(busy)` while an
/// admitted input runs, else the new token with its time and expiry.
#[allow(clippy::too_many_arguments)]
fn try_acquire(
    db: &Connection,
    target: &Target,
    owner: &str,
    task: &str,
    reason: &str,
    ttl_ms: u64,
    channel: &str,
    client: &str,
    at: f64,
) -> Result<std::result::Result<(String, f64, f64), Busy>> {
    transaction(db, |db| {
        purge(db, &target.key, at)?;
        prune_ended(db, at)?;
        if let Some(h) = holder(db, &target.key)? {
            return Err(reserved(target, &h, at));
        }
        let busy = in_flight(db, &target.key, at)?;
        if busy.actions + busy.contacts > 0 {
            return Ok(Err(busy));
        }
        let held: i64 = db.query_row("SELECT COUNT(*) FROM reservations", [], |r| r.get(0))?;
        if held >= RESERVATIONS_MAX {
            return Err(Error::new(
                "reservation_limit",
                format!("{held} devices are reserved already, the most at once; nothing was reserved"),
            )
            .remedy("Release reservations no longer needed (device.reservation.list shows them); each also ends by itself at its expiry"));
        }
        let token = new_token();
        let expires = at + ttl_ms as f64 / 1000.0;
        db.execute(
            "INSERT INTO reservations (device, capability, owner, task, reason, client, channel, acquired, renewed, expires, ttl_ms) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?8, ?9, ?10)",
            params![target.key, fingerprint(&token), owner, task, reason, client, channel, at, expires, ttl_ms as i64],
        )?;
        Ok(Ok((token, at, expires)))
    })
}

/// Takes `target` for a new holder, once no admitted input runs on it; the
/// first holder to commit wins, a second is refused with the first's labels.
fn acquire_in(
    target: &Target,
    owner: &str,
    task: &str,
    reason: &str,
    ttl_ms: u64,
    wait_ms: u64,
) -> Result {
    let (channel, client) = caller();
    let started = Instant::now();
    let deadline = started + Duration::from_millis(wait_ms);
    loop {
        check_cancelled()?;
        let attempt = with_db(|db| {
            try_acquire(
                db,
                target,
                owner,
                task,
                reason,
                ttl_ms,
                &channel,
                &client,
                now(),
            )
        })?;
        match attempt {
            Ok((token, at, expires)) => {
                return Ok(
                    json!({"device":target.value(),"reservation_token":token,"owner":owner,"task":task,"reason":reason,
                    "client":client,"channel":channel,"acquired_at":at,"expires_at":expires,"ttl_ms":ttl_ms,
                    "note":"Pass reservation_token to every input on this device, renew before expires_at, release when done. The token is shown only here.",
                    "boundary":BOUNDARY}),
                );
            }
            Err(busy) => {
                if Instant::now() >= deadline {
                    return Err(in_flight_error(
                        target,
                        &busy,
                        started.elapsed().as_millis() as u64,
                    ));
                }
                thread::sleep(
                    Duration::from_millis(25)
                        .min(deadline.saturating_duration_since(Instant::now())),
                );
            }
        }
    }
}

fn required_token(a: &Value) -> Result<String> {
    let token = s(a, "reservation_token");
    if token.is_empty() {
        return Err(Error::new(
            "invalid_argument",
            "reservation_token is required",
        ));
    }
    Ok(checked_token(token)?.to_string())
}

fn acquire(a: &Value) -> Result {
    let target = target_of(a)?;
    let owner = label(a, "owner", 100);
    if owner.is_empty() {
        return Err(Error::new(
            "invalid_argument",
            "owner names who reserves the device",
        ));
    }
    let wait = a["wait_ms"]
        .as_u64()
        .unwrap_or(WAIT_DEFAULT_MS)
        .min(WAIT_MAX_MS);
    acquire_in(
        &target,
        &owner,
        &label(a, "task", 200),
        &label(a, "reason", 500),
        ttl(a),
        wait,
    )
}

fn renew(a: &Value) -> Result {
    let target = target_of(a)?;
    let token = required_token(a)?;
    let ttl_ms = ttl(a);
    with_db(|db| {
        transaction(db, |db| {
            let at = now();
            let mut h =
                decide(db, &target, Some(&fingerprint(&token)), at, true)?.ok_or_else(|| {
                    Error::new(
                        "reservation_not_held",
                        "This reservation_token holds no reservation",
                    )
                })?;
            h.expires = at + ttl_ms as f64 / 1000.0;
            h.renewed = at;
            h.ttl_ms = ttl_ms;
            db.execute(
                "UPDATE reservations SET expires=?1, renewed=?2, ttl_ms=?3 WHERE device=?4",
                params![h.expires, at, ttl_ms as i64, target.key],
            )?;
            Ok(
                json!({"device":target.value(),"renewed":true,"reservation":h.public(at),"boundary":BOUNDARY}),
            )
        })
    })
}

fn release(a: &Value) -> Result {
    let target = target_of(a)?;
    let token = required_token(a)?;
    let hash = fingerprint(&token);
    with_db(|db| {
        transaction(db, |db| {
            let at = now();
            match decide(db, &target, Some(&hash), at, true) {
                Ok(Some(h)) => {
                    end(
                        db,
                        &target.key,
                        &h,
                        "released",
                        at,
                        "released by its holder",
                    )?;
                    Ok(
                        json!({"device":target.value(),"released":true,"reservation":h.public(at),
                        "note":"Inputs the device already received are not undone; inputs still queued under this token are refused"}),
                    )
                }
                Ok(None) => Err(Error::new(
                    "reservation_not_held",
                    "This reservation_token holds no reservation",
                )),
                // Already over: releasing again changes nothing.
                Err(error)
                    if matches!(
                        error.code.as_str(),
                        "reservation_expired" | "reservation_released" | "reservation_revoked"
                    ) =>
                {
                    Ok(
                        json!({"device":target.value(),"released":false,"state":error.code.trim_start_matches("reservation_"),
                        "ended":error.details.as_ref().map(|d| d["ended"].clone()).unwrap_or(Value::Null)}),
                    )
                }
                Err(error) => Err(error),
            }
        })
    })
}

/// Ends another holder's reservation on a person's explicit request, and
/// optionally reserves the device for the caller once admitted inputs end.
fn takeover(a: &Value) -> Result {
    let target = target_of(a)?;
    let reason = label(a, "reason", 500);
    if reason.is_empty() {
        return Err(Error::new(
            "invalid_argument",
            "reason says why the reservation is taken over",
        ));
    }
    let (channel, client) = caller();
    let note = format!(
        "taken over through {channel}{}: {reason}",
        if client.is_empty() {
            String::new()
        } else {
            format!(" by {client}")
        }
    );
    let revoked = with_db(|db| {
        transaction(db, |db| {
            let at = now();
            purge(db, &target.key, at)?;
            match holder(db, &target.key)? {
                Some(h) => {
                    end(db, &target.key, &h, "revoked", at, &note)?;
                    Ok(h.public(at))
                }
                None => Ok(Value::Null),
            }
        })
    })?;
    let reservation = if a["acquire"] == true {
        let owner = label(a, "owner", 100);
        let owner = if owner.is_empty() {
            "person".to_string()
        } else {
            owner
        };
        let wait = a["wait_ms"].as_u64().unwrap_or(5_000).min(WAIT_MAX_MS);
        acquire_in(
            &target,
            &owner,
            &label(a, "task", 200),
            &reason,
            ttl(a),
            wait,
        )?
    } else {
        Value::Null
    };
    Ok(
        json!({"device":target.value(),"revoked":revoked,"reservation":reservation,
        "note":"The previous holder's queued inputs are refused as reservation_revoked; what it already sent is not undone",
        "boundary":BOUNDARY}),
    )
}

fn status(db: &Connection, target: &Target, hash: Option<&str>, at: f64) -> Result {
    let current = holder(db, &target.key)?.filter(|h| h.expires > at);
    let busy = in_flight(db, &target.key, at)?;
    let last = db
        .query_row(
            "SELECT owner, task, how, at FROM ended WHERE device=?1 ORDER BY at DESC LIMIT 1",
            params![target.key],
            |r| Ok(json!({"owner":r.get::<_, String>(0)?,"task":r.get::<_, String>(1)?,"how":r.get::<_, String>(2)?,"at":r.get::<_, f64>(3)?})),
        )
        .optional()?;
    let token = match hash {
        None => Value::Null,
        Some(hash) => match decide(db, target, Some(hash), at, false) {
            Ok(_) => json!("holds"),
            Err(error) => json!(error.code.trim_start_matches("reservation_")),
        },
    };
    Ok(
        json!({"device":target.value(),"reserved":current.is_some(),"reservation":current.map(|h| h.public(at)),
        "token":token,"busy":busy.actions + busy.contacts > 0,"in_flight":{"inputs":busy.actions,"touches":busy.contacts},
        "last_ended":last,"boundary":BOUNDARY}),
    )
}

fn get(a: &Value) -> Result {
    let target = target_of(a)?;
    let hash = match s(a, "reservation_token") {
        "" => None,
        token => Some(fingerprint(checked_token(token)?)),
    };
    with_db(|db| status(db, &target, hash.as_deref(), now()))
}

fn list(_a: &Value) -> Result {
    with_db(|db| {
        let at = now();
        let keys: Vec<String> = {
            let mut statement =
                db.prepare("SELECT device FROM reservations WHERE expires>?1 ORDER BY device")?;
            statement
                .query_map(params![at], |r| r.get(0))?
                .collect::<std::result::Result<_, _>>()?
        };
        let mut out = Vec::new();
        for key in keys {
            if let Some(h) = holder(db, &key)? {
                let busy = in_flight(db, &key, at)?;
                out.push(json!({"device":key,"reservation":h.public(at),"in_flight":{"inputs":busy.actions,"touches":busy.contacts}}));
            }
        }
        Ok(json!({"reservations":out,"count":out.len(),"boundary":BOUNDARY}))
    })
}

// ---- Participating operations -------------------------------------------------

/// The input operations that take `reservation_token` and `request_id`, and
/// the route their inputs take to the device. `ios.emulator.control`
/// participates for its input actions only.
pub const PARTICIPATING: &[&str] = &[
    "runtime.input",
    "runtime.gesture",
    "runtime.actions",
    "screen.tap",
    "android.input",
    "testing.action",
    "ios.emulator.control",
];

fn route_of(name: &str, a: &Value) -> Result<(Target, &'static str)> {
    let device = s(a, "device");
    match name {
        "runtime.input" | "runtime.gesture" | "runtime.actions" => {
            Ok((session_target(s(a, "session"))?, "runtime-worker"))
        }
        "android.input" => {
            identifier(device, "Android serial", None)?;
            Ok((Target::new("android", "", device), "adb"))
        }
        "ios.emulator.control" => Ok((virtual_iphone(), "virtual-iphone")),
        _ if crate::ios_emulator::ui_testing::is_virtual(device) => {
            Ok((virtual_iphone(), "virtual-iphone"))
        }
        _ if crate::screen::is_session(device) => Ok((session_target(device)?, "runtime-worker")),
        _ => {
            identifier(device, "device", None)?;
            Ok((Target::new("android", "", device), "adb"))
        }
    }
}

/// How a participating call's input is admitted, for the read-only check
/// before it is prepared or queued; `None` for a call that sends no input
/// (a batch of waits and looks, a status or frame of the virtual iPhone) or
/// continues a touch already admitted.
fn early_kind(name: &str, a: &Value) -> Option<Kind> {
    let kind = match name {
        "runtime.input" => Kind::of_input(a),
        "ios.emulator.control" => crate::ios_emulator::control::input_kind(s(a, "action"))?,
        "runtime.actions" => {
            let steps = a["steps"].as_array()?;
            if !steps
                .iter()
                .any(|step| crate::runtime::actions::mutates(s(step, "action")))
            {
                return None;
            }
            Kind::Action
        }
        _ => Kind::Action,
    };
    matches!(kind, Kind::Action | Kind::ContactBegin).then_some(kind)
}

/// The checks a call would fail before it sends anything, run before it is
/// recorded: a malformed request leaves no record and changes nothing.
fn prevalidate(name: &str, a: &Value) -> Result<()> {
    match name {
        "runtime.actions" => {
            let steps = a["steps"].as_array().cloned().unwrap_or_default();
            if steps.is_empty() {
                return Err(Error::new(
                    "invalid_argument",
                    "steps lists at least one step",
                ));
            }
            crate::runtime::actions::check(&steps)
        }
        "runtime.gesture" => crate::runtime::check_gesture(a).map(|_| ()),
        _ => Ok(()),
    }
}

fn take(a: &mut Value, key: &str) -> Option<String> {
    a.as_object_mut()
        .and_then(|map| map.remove(key))
        .and_then(|value| value.as_str().map(str::to_string))
        .filter(|value| !value.is_empty())
}

/// Runs a participating operation's handler `route` with its reservation
/// token and request identity taken out of its arguments: the token goes
/// to this thread's context, which every enforcement point reads, and a
/// `request_id` makes the call one record of the request ledger. Every
/// other operation runs as it is.
pub fn participate(name: &str, mut a: Value, route: impl FnOnce(Value) -> Result) -> Result {
    if !PARTICIPATING.contains(&name) {
        return route(a);
    }
    let token = take(&mut a, "reservation_token");
    let request = take(&mut a, "request_id");
    if let Some(token) = &token {
        checked_token(token)?;
    }
    if name == "ios.emulator.control"
        && crate::ios_emulator::control::input_kind(s(&a, "action")).is_none()
    {
        if request.is_some() {
            return Err(Error::new(
                "invalid_argument",
                format!(
                    "request_id applies to input actions; {} is not one",
                    s(&a, "action")
                ),
            ));
        }
        return route(a);
    }
    let call = Context {
        token,
        ..Context::default()
    };
    with_context(call, || {
        // Refused here, the call sent nothing; every input is admitted again
        // right before its transport gets it. A name that resolves to no
        // device is left to the operation's own error.
        if early_kind(name, &a).is_some() {
            if let Ok((target, _)) = route_of(name, &a) {
                check(&target, context().token.as_deref())?;
            }
        }
        let Some(id) = request else {
            return route(a);
        };
        ledger::checked_id(&id)?;
        prevalidate(name, &a)?;
        let (target, path) = route_of(name, &a)?;
        let d = ledger::Dispatch {
            operation: name,
            target: &target,
            route: path,
            transport: target.transport.clone(),
            instance: ledger::instance(),
        };
        let recorded = a.clone();
        ledger::run(&id, &d, &recorded, move || route(a))
    })
}

/// `device.reservation.*` and `device.request.*`.
pub fn handle(name: &str, a: &Value) -> Result {
    match name {
        "device.reservation.acquire" => acquire(a),
        "device.reservation.get" => get(a),
        "device.reservation.list" => list(a),
        "device.reservation.renew" => renew(a),
        "device.reservation.release" => release(a),
        "device.reservation.takeover" => takeover(a),
        "device.request.get" => ledger::get(a),
        "device.request.list" => ledger::list(a),
        _ => Err(Error::new("operation_not_found", name)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn database(dir: &Path) -> Connection {
        open_path(
            &dir.join("reservations.sqlite3"),
            SCHEMA,
            SCHEMA_VERSION,
            false,
        )
        .unwrap()
    }
    fn take(
        db: &Connection,
        target: &Target,
        owner: &str,
        ttl_ms: u64,
        at: f64,
    ) -> Result<std::result::Result<(String, f64, f64), Busy>> {
        try_acquire(
            db,
            target,
            owner,
            "checkout flow",
            "testing",
            ttl_ms,
            "cli",
            "test",
            at,
        )
    }
    fn code<T: std::fmt::Debug>(result: Result<T>) -> String {
        result.unwrap_err().code
    }

    #[test]
    fn one_owner_holds_a_device_and_others_are_refused_with_its_labels_only() {
        let dir = tempfile::tempdir().unwrap();
        let db = database(dir.path());
        let pixel = Target::new("android", "", "emulator-5554");
        let other = Target::new("android", "", "emulator-5556");
        let at = 1_000.0;
        let (token, _, expires) = take(&db, &pixel, "agent-a", 60_000, at).unwrap().unwrap();
        assert!(checked_token(&token).is_ok());
        assert_eq!(expires, at + 60.0);
        // A second owner, whatever its label says, is refused and learns who holds it.
        let refused = take(&db, &pixel, "agent-a", 60_000, at + 1.0).unwrap_err();
        assert_eq!(refused.code, "device_reserved");
        let shown = refused.value().to_string();
        assert!(
            shown.contains("agent-a") && shown.contains("checkout flow"),
            "{shown}"
        );
        assert!(
            !shown.contains(&token) && !shown.contains(&fingerprint(&token)),
            "{shown}"
        );
        // Input: the token passes, nothing else does; another device is independent.
        let hash = fingerprint(&token);
        assert!(
            admit_in(&db, &pixel, Kind::Action, Some(&hash), false, at + 2.0)
                .unwrap()
                .is_some()
        );
        assert_eq!(
            code(admit_in(&db, &pixel, Kind::Action, None, false, at + 2.0)),
            "device_reserved"
        );
        assert_eq!(
            code(admit_in(&db, &pixel, Kind::Pointer, None, false, at + 2.0)),
            "device_reserved"
        );
        assert!(admit_in(&db, &other, Kind::Action, None, false, at + 2.0).is_ok());
        assert_eq!(
            code(admit_in(
                &db,
                &other,
                Kind::Action,
                Some(&hash),
                false,
                at + 2.0
            )),
            "reservation_not_held"
        );
        // The holder's own admission does not block its renewal, but blocks another's acquisition once it is over.
        db.execute("DELETE FROM admissions", []).unwrap();
        let status = status(&db, &pixel, Some(&hash), at + 3.0).unwrap();
        assert_eq!(status["reserved"], true);
        assert_eq!(status["token"], "holds");
        assert_eq!(status["reservation"]["owner"], "agent-a");
        assert!(!status.to_string().contains(&hash));
    }

    #[test]
    fn a_token_whose_reservation_ended_is_refused_with_how_it_ended() {
        let dir = tempfile::tempdir().unwrap();
        let db = database(dir.path());
        let pixel = Target::new("android", "", "serial-1");
        let at = 5_000.0;
        // Expired while a request was queued: refused at execution, also before a write purged it.
        let (expiring, ..) = take(&db, &pixel, "a", 1_000, at).unwrap().unwrap();
        let hash = fingerprint(&expiring);
        assert!(decide(&db, &pixel, Some(&hash), at + 0.5, false).is_ok());
        assert_eq!(
            code(decide(&db, &pixel, Some(&hash), at + 1.5, false)),
            "reservation_expired"
        );
        assert_eq!(
            code(admit_in(
                &db,
                &pixel,
                Kind::Action,
                Some(&hash),
                false,
                at + 1.5
            )),
            "reservation_expired"
        );
        // Expired means free for anyone.
        assert!(admit_in(&db, &pixel, Kind::Action, None, false, at + 1.6).is_ok());
        db.execute("DELETE FROM admissions", []).unwrap();
        // Released.
        let (released, ..) = take(&db, &pixel, "b", 60_000, at + 2.0).unwrap().unwrap();
        let h = holder(&db, &pixel.key).unwrap().unwrap();
        end(
            &db,
            &pixel.key,
            &h,
            "released",
            at + 3.0,
            "released by its holder",
        )
        .unwrap();
        assert_eq!(
            code(admit_in(
                &db,
                &pixel,
                Kind::Action,
                Some(&fingerprint(&released)),
                false,
                at + 4.0
            )),
            "reservation_released"
        );
        // Revoked by a takeover, while a new holder has it: the old token learns both.
        let (old, ..) = take(&db, &pixel, "c", 60_000, at + 5.0).unwrap().unwrap();
        let h = holder(&db, &pixel.key).unwrap().unwrap();
        end(
            &db,
            &pixel.key,
            &h,
            "revoked",
            at + 6.0,
            "taken over through desktop: person needs the phone",
        )
        .unwrap();
        take(&db, &pixel, "person", 60_000, at + 6.0)
            .unwrap()
            .unwrap();
        let error = admit_in(
            &db,
            &pixel,
            Kind::Action,
            Some(&fingerprint(&old)),
            false,
            at + 7.0,
        )
        .unwrap_err();
        assert_eq!(error.code, "reservation_revoked");
        let details = error.details.unwrap();
        assert_eq!(
            details["ended"]["note"],
            "taken over through desktop: person needs the phone"
        );
        assert_eq!(details["held_by"]["owner"], "person");
        // A well-formed token nobody issued here.
        let unknown = format!("{TOKEN_PREFIX}{}", "a".repeat(64));
        assert_eq!(
            code(admit_in(
                &db,
                &pixel,
                Kind::Action,
                Some(&fingerprint(&unknown)),
                false,
                at + 7.0
            )),
            "reservation_not_held"
        );
        // A malformed one is refused without being quoted.
        let error = checked_token("hunter2-not-a-token").unwrap_err();
        assert!(!error.value().to_string().contains("hunter2"));
    }

    #[test]
    fn an_acquisition_waits_for_admitted_inputs_and_held_touches_but_not_for_dead_ones() {
        let dir = tempfile::tempdir().unwrap();
        let db = database(dir.path());
        let pixel = Target::new("ios", "mac", "00008110-001A2B3C4D5E6F70");
        let at = 9_000.0;
        // An unreserved caller's tap is in flight in this (live) process.
        let id = admit_in(&db, &pixel, Kind::Action, None, false, at)
            .unwrap()
            .unwrap();
        let busy = take(&db, &pixel, "agent", 60_000, at + 0.1)
            .unwrap()
            .unwrap_err();
        assert_eq!((busy.actions, busy.contacts), (1, 0));
        db.execute("DELETE FROM admissions WHERE id=?1", params![id])
            .unwrap();
        // A finger is down from a process that ends after each event: it holds until up.
        assert!(
            admit_in(&db, &pixel, Kind::ContactBegin, None, false, at)
                .unwrap()
                .is_none()
        );
        assert!(
            take(&db, &pixel, "agent", 60_000, at + 1.0)
                .unwrap()
                .is_err()
        );
        assert!(admit_in(&db, &pixel, Kind::ContactContinue, None, false, at + 1.0).is_ok());
        assert!(admit_in(&db, &pixel, Kind::ContactEnd, None, false, at + 1.1).is_ok());
        let (token, ..) = take(&db, &pixel, "agent", 60_000, at + 1.2)
            .unwrap()
            .unwrap();
        // Now reserved: a stray move or up of the unreserved caller is refused, a cancel is not.
        assert_eq!(
            code(admit_in(
                &db,
                &pixel,
                Kind::ContactContinue,
                None,
                false,
                at + 1.3
            )),
            "device_reserved"
        );
        assert_eq!(
            code(admit_in(
                &db,
                &pixel,
                Kind::ContactEnd,
                None,
                false,
                at + 1.3
            )),
            "device_reserved"
        );
        assert!(admit_in(&db, &pixel, Kind::ContactCancel, None, false, at + 1.3).is_ok());
        let h = holder(&db, &pixel.key).unwrap().unwrap();
        end(&db, &pixel.key, &h, "released", at + 2.0, "").unwrap();
        drop(token);
        // A touch left down past its bound, and an input of a process that is gone, block nothing.
        assert!(
            admit_in(&db, &pixel, Kind::ContactBegin, None, false, at + 3.0)
                .unwrap()
                .is_none()
        );
        db.execute(
            "INSERT INTO admissions (id, device, kind, capability, pid, token, started, expires) VALUES ('dead', ?1, 'action', '', 999999999, 'gone', ?2, ?3)",
            params![pixel.key, at + 3.0, at + 500.0],
        )
        .unwrap();
        assert!(
            take(
                &db,
                &pixel,
                "agent",
                60_000,
                at + 3.0 + CONTACT_SECONDS + 1.0
            )
            .unwrap()
            .is_ok()
        );
    }

    #[test]
    fn names_resolve_to_one_canonical_device_per_platform_and_scope() {
        assert_eq!(
            Target::new("android", "", "emulator-5554").key,
            "android/local/emulator-5554"
        );
        assert_eq!(Target::new("ios", "mac", "x").key, "ios/remote:mac/x");
        assert!(udid_shaped("00008110-001A2B3C4D5E6F70"));
        assert!(udid_shaped(&"ab".repeat(20)));
        assert!(!udid_shaped("emulator-5554") && !udid_shaped("R58M123ABC"));
        assert_eq!(virtual_iphone().key, "ios/local/virtual-iphone");
        assert_eq!(
            Kind::of_input(&json!({"action":"touch","phase":"down"})),
            Kind::ContactBegin
        );
        assert_eq!(
            Kind::of_input(&json!({"action":"touch","phase":"hover"})),
            Kind::Pointer
        );
        assert_eq!(
            Kind::of_input(&json!({"action":"text","text":"x"})),
            Kind::Action
        );
    }

    #[test]
    fn tokens_are_masked_wherever_they_are_quoted() {
        let token = new_token();
        let mut value = json!({"reservation_token":token,"note":format!("use {token} next")});
        crate::redact::value(&mut value);
        assert!(
            !value.to_string().contains(&token[TOKEN_PREFIX.len()..]),
            "{value}"
        );
        assert!(!crate::redact::text(&format!("x {token} y")).contains(&token));
    }
}
