use serde::Serialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::{fs::PermissionsExt, io::AsRawFd},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

pub type Result<T = Value> = std::result::Result<T, Error>;
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
pub const JSON_LIMIT: u64 = 2 * 1024 * 1024;
pub type Cancellation = std::sync::Arc<std::sync::atomic::AtomicBool>;
thread_local! {
    static REQUEST_CANCELLATION: std::cell::RefCell<Option<Cancellation>> = const { std::cell::RefCell::new(None) };
}
pub fn cancellation() -> Option<Cancellation> {
    REQUEST_CANCELLATION.with(|value| value.borrow().clone())
}
pub fn with_cancellation<T>(token: Option<Cancellation>, action: impl FnOnce() -> T) -> T {
    let previous = REQUEST_CANCELLATION.with(|value| value.replace(token));
    struct Restore(Option<Cancellation>);
    impl Drop for Restore {
        fn drop(&mut self) {
            REQUEST_CANCELLATION.with(|value| value.replace(self.0.take()));
        }
    }
    let _restore = Restore(previous);
    action()
}
pub fn check_cancelled() -> Result<()> {
    if cancellation().is_some_and(|value| value.load(std::sync::atomic::Ordering::Relaxed)) {
        Err(Error::new(
            "request_cancelled",
            "The client cancelled this request",
        ))
    } else {
        Ok(())
    }
}

#[derive(Debug, Serialize)]
pub struct Error {
    pub code: String,
    pub message: String,
    pub remedy: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub details: Option<Box<Value>>,
}
impl Error {
    pub fn new(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            remedy: String::new(),
            details: None,
        }
    }
    pub fn remedy(mut self, value: impl Into<String>) -> Self {
        self.remedy = value.into();
        self
    }
    pub fn details(mut self, value: Value) -> Self {
        self.details = Some(Box::new(value));
        self
    }
    pub fn value(&self) -> Value {
        serde_json::to_value(self).expect("error is serializable")
    }
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.message.fmt(f)
    }
}
impl std::error::Error for Error {}
impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Self::new("io_error", e.to_string())
    }
}
impl From<serde_json::Error> for Error {
    fn from(e: serde_json::Error) -> Self {
        Self::new("invalid_json", e.to_string())
    }
}
impl From<rusqlite::Error> for Error {
    fn from(e: rusqlite::Error) -> Self {
        let contended = matches!(
            e.sqlite_error_code(),
            Some(rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked)
        );
        let error = Self::new("database_error", e.to_string());
        // The code stays database_error for existing clients; the detail lets
        // read paths tell lock contention, which is retried, from real faults.
        if contended {
            error.details(json!({"sqlite":"busy"}))
        } else {
            error
        }
    }
}
impl Error {
    /// SQLite lock contention (SQLITE_BUSY / SQLITE_LOCKED), never a data fault.
    pub fn is_database_busy(&self) -> bool {
        self.code == "database_error"
            && self
                .details
                .as_ref()
                .is_some_and(|details| details["sqlite"] == "busy")
    }
}

pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
/// The source checkout this executable runs from (build-product/tomaccoctl,
/// build/tomaccoctl or Cargo's target directory inside it), found from the
/// executable's location, so no build path is compiled into a release binary.
/// Debug builds (cargo test) also know the checkout they were compiled from.
pub fn source_checkout() -> Option<PathBuf> {
    let executable = own_executable().ok()?;
    if let Some(found) = checkout_above(&executable) {
        return Some(found);
    }
    #[cfg(debug_assertions)]
    {
        let compiled = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        if checkout_above(&compiled.join("target")).as_ref() == Some(&compiled) {
            return Some(compiled);
        }
    }
    None
}
/// The checkout an executable sits in, found from its location alone.
pub fn checkout_above(executable: &Path) -> Option<PathBuf> {
    executable
        .ancestors()
        .skip(1)
        .take(5)
        .find(|directory| {
            directory.join("Cargo.toml").is_file()
                && directory.join("native/main.rs").is_file()
                && directory.join("tools").is_dir()
        })
        .map(Path::to_path_buf)
}
/// This program's path, also after an update replaced its file while it runs:
/// the kernel then reports the old path with " (deleted)", which cannot be
/// started, and the new program at the old path is the one to start.
pub fn own_executable() -> std::io::Result<PathBuf> {
    Ok(without_deleted_suffix(std::env::current_exe()?))
}
fn without_deleted_suffix(executable: PathBuf) -> PathBuf {
    match executable
        .to_str()
        .and_then(|text| text.strip_suffix(" (deleted)"))
    {
        Some(original) if !executable.exists() => PathBuf::from(original),
        _ => executable,
    }
}
pub fn user_home() -> PathBuf {
    // Without HOME (systemd units, sanitised environments) the account's home
    // from the password database is used. A shared /tmp fallback would let
    // another local user pre-create the data directory or plant executables
    // in the ~/.local/bin search path.
    std::env::var_os("HOME")
        .filter(|value| Path::new(value).is_absolute())
        .map(PathBuf::from)
        .or_else(account_home)
        .unwrap_or_else(|| PathBuf::from("/nonexistent"))
}
fn account_home() -> Option<PathBuf> {
    use std::os::unix::ffi::OsStrExt;
    let mut buffer = vec![0u8; 16384];
    loop {
        // SAFETY: passwd and result are plain out-parameters; buffer outlives
        // the call and its length is passed exactly. getpwuid_r is reentrant.
        let mut passwd: libc::passwd = unsafe { std::mem::zeroed() };
        let mut result: *mut libc::passwd = std::ptr::null_mut();
        let code = unsafe {
            libc::getpwuid_r(
                libc::geteuid(),
                &mut passwd,
                buffer.as_mut_ptr().cast(),
                buffer.len(),
                &mut result,
            )
        };
        if code == libc::ERANGE && buffer.len() < 1024 * 1024 {
            buffer.resize(buffer.len() * 4, 0);
            continue;
        }
        if code != 0 || result.is_null() || passwd.pw_dir.is_null() {
            return None;
        }
        // SAFETY: pw_dir points into buffer, NUL-terminated by getpwuid_r.
        let directory = unsafe { std::ffi::CStr::from_ptr(passwd.pw_dir) };
        let directory = Path::new(std::ffi::OsStr::from_bytes(directory.to_bytes()));
        return directory.is_absolute().then(|| directory.to_path_buf());
    }
}
pub fn expand(value: &str) -> PathBuf {
    if value == "~" {
        user_home()
    } else if let Some(tail) = value.strip_prefix("~/") {
        user_home().join(tail)
    } else {
        PathBuf::from(value)
    }
}
pub fn path(value: &str, directory: bool, exists: bool) -> Result<PathBuf> {
    if value.trim().is_empty() || value.contains('\0') {
        return Err(Error::new(
            "invalid_path",
            "A non-empty local path is required",
        ));
    }
    let expanded = expand(value);
    let absolute = if expanded.is_absolute() {
        expanded
    } else {
        std::env::current_dir()?.join(expanded)
    };
    // Canonicalize existing ancestors as Python Path.resolve() did, including new destinations.
    fn resolve(p: &Path) -> std::io::Result<PathBuf> {
        if p.exists() {
            return p.canonicalize();
        }
        let parent = p
            .parent()
            .ok_or_else(|| std::io::Error::other("Invalid path"))?;
        let name = p
            .file_name()
            .ok_or_else(|| std::io::Error::other("Invalid path"))?;
        Ok(resolve(parent)?.join(name))
    }
    let normalized: PathBuf = absolute.components().fold(PathBuf::new(), |mut p, part| {
        match part {
            std::path::Component::ParentDir => {
                p.pop();
            }
            std::path::Component::CurDir => (),
            _ => p.push(part),
        };
        p
    });
    let resolved = resolve(&normalized)?;
    if exists
        && !(if directory {
            resolved.is_dir()
        } else {
            resolved.exists()
        })
    {
        return Err(Error::new(
            "path_not_found",
            format!("Path not found: {}", resolved.display()),
        ));
    }
    Ok(resolved)
}
pub fn home() -> Result<PathBuf> {
    let p = path(
        &std::env::var("TOMACCO_HOME").unwrap_or_else(|_| {
            user_home()
                .join(".local/share/tomacco")
                .to_string_lossy()
                .into_owned()
        }),
        false,
        false,
    )?;
    private_dir(&p)?;
    owner_only(&p)?;
    Ok(p)
}
/// The data directory holds the job database, logs and private scratch
/// space. Children such as jobs.sqlite3 are created with the umask, so the
/// directory itself must keep other users out: one created by an earlier
/// version, the desktop or an installer with 0755 is tightened, and one that
/// belongs to someone else is refused rather than written into.
fn owner_only(directory: &Path) -> Result<()> {
    use std::os::unix::fs::MetadataExt;
    let metadata = fs::metadata(directory)?;
    // SAFETY: geteuid has no preconditions.
    let owner = unsafe { libc::geteuid() };
    if !metadata.is_dir() || metadata.uid() != owner {
        return Err(Error::new(
            "unsafe_data_directory",
            format!(
                "The Tomacco data directory is not a directory owned by this user: {}",
                directory.display()
            ),
        )
        .remedy("Set TOMACCO_HOME to a directory you own"));
    }
    if metadata.mode() & 0o077 != 0 {
        fs::set_permissions(directory, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}
/// A directory for sockets or other runtime endpoints, possibly in a shared
/// location such as /tmp. It is created owner-only in one step, and an
/// existing one is used only when it is a real directory owned by this user
/// that nobody else can enter: another user may have created it first.
pub fn private_runtime_dir(path: &Path) -> Result<()> {
    use std::os::unix::fs::{DirBuilderExt, MetadataExt};
    match fs::DirBuilder::new().mode(0o700).create(path) {
        Ok(()) => (),
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => (),
        Err(error) => return Err(error.into()),
    }
    let metadata = fs::symlink_metadata(path)?;
    // SAFETY: geteuid has no preconditions.
    if !metadata.is_dir()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.mode() & 0o077 != 0
    {
        return Err(Error::new(
            "unsafe_runtime_directory",
            format!(
                "Runtime directory is not a private directory of this user: {}",
                path.display()
            ),
        )
        .remedy("Remove it, or set TMPDIR to a directory only you can write"));
    }
    Ok(())
}
pub fn output_path(value: &str) -> Result<PathBuf> {
    let p = path(value, false, false)?;
    if p.file_name().is_none() || p.is_dir() {
        return Err(Error::new(
            "invalid_path",
            "Select a file destination, not a directory",
        ));
    }
    Ok(p)
}
pub fn private_dir(path: &Path) -> Result<()> {
    if !path.exists() {
        fs::create_dir_all(path)?;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}
pub fn read_json(path: &Path, default: Option<Value>) -> Result {
    if !path.exists() {
        if let Some(value) = default {
            return Ok(value);
        }
    }
    let file = File::open(path).map_err(|e| {
        Error::new(
            "invalid_json",
            format!("Cannot read {}: {e}", path.display()),
        )
    })?;
    let mut bytes = Vec::new();
    file.take(JSON_LIMIT + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > JSON_LIMIT {
        return Err(Error::new(
            "input_too_large",
            format!("JSON exceeds 2 MiB: {}", path.display()),
        ));
    }
    Ok(serde_json::from_slice(&bytes)?)
}
pub fn atomic_write(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| Error::new("invalid_path", "Destination has no parent"))?;
    private_dir(parent)?;
    let mut temp = tempfile::NamedTempFile::new_in(parent)?;
    temp.write_all(bytes)?;
    temp.as_file().sync_all()?;
    temp.persist(path).map_err(|e| Error::from(e.error))?;
    File::open(parent)?.sync_all()?;
    Ok(())
}
pub fn atomic_json(path: &Path, value: &Value) -> Result<()> {
    let mut bytes = serde_json::to_vec_pretty(value)?;
    bytes.push(b'\n');
    atomic_write(path, &bytes)
}
pub fn identifier<'a>(value: &'a str, label: &str, pattern: Option<&str>) -> Result<&'a str> {
    let pattern = pattern.unwrap_or(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}");
    if !regex::Regex::new(&format!("^(?:{pattern})$"))
        .map_err(|e| Error::new("invalid_schema", e.to_string()))?
        .is_match(value)
    {
        return Err(Error::new(
            "invalid_argument",
            format!("Invalid {label}: {value:?}"),
        ));
    }
    Ok(value)
}
/// The candidate a mistyped name most likely meant: the fewest single-character
/// edits, within a third of its length (at least two), or else the one
/// candidate whose last dotted parts are the name (`ios.virtual.start` for
/// `virtual.start`). None when nothing is close enough to suggest.
pub fn closest<'a>(name: &str, candidates: impl IntoIterator<Item = &'a str>) -> Option<&'a str> {
    fn distance(a: &str, b: &str) -> usize {
        let b: Vec<char> = b.chars().collect();
        let mut row: Vec<usize> = (0..=b.len()).collect();
        for (i, x) in a.chars().enumerate() {
            let mut diagonal = row[0];
            row[0] = i + 1;
            for (j, y) in b.iter().enumerate() {
                let above = row[j + 1];
                row[j + 1] = (above + 1)
                    .min(row[j] + 1)
                    .min(diagonal + usize::from(x != *y));
                diagonal = above;
            }
        }
        row[b.len()]
    }
    let name = name.to_ascii_lowercase();
    let limit = (name.chars().count() / 3).max(2);
    let suffix = format!(".{name}");
    let mut best: Option<(usize, &str)> = None;
    let mut suffixed = Vec::new();
    for candidate in candidates {
        if candidate.ends_with(&suffix) {
            suffixed.push(candidate);
        }
        let d = distance(&name, candidate);
        if d <= limit && best.is_none_or(|(e, _)| d < e) {
            best = Some((d, candidate));
        }
    }
    best.map(|(_, c)| c)
        .or_else(|| (suffixed.len() == 1).then(|| suffixed[0]))
}
pub fn validate(value: &Value, schema: &Value, name: &str) -> Result<()> {
    let kind = schema["type"].as_str().unwrap_or("");
    let valid = match kind {
        "object" => value.is_object(),
        "array" => value.is_array(),
        "string" => value.is_string(),
        "boolean" => value.is_boolean(),
        "integer" => value.is_i64() || value.is_u64(),
        "number" => value.is_number(),
        "" => true,
        _ => false,
    };
    let fail = |message: String| Error::new("invalid_argument", message);
    if !valid {
        return Err(fail(format!("{name} must be {kind}")));
    }
    if let Some(choices) = schema["enum"].as_array() {
        if !choices.contains(value) {
            return Err(fail(format!("{name} must be one of {}", schema["enum"])));
        }
    }
    match kind {
        "object" => {
            let count = value.as_object().unwrap().len() as u64;
            if count < schema["minProperties"].as_u64().unwrap_or(0)
                || count > schema["maxProperties"].as_u64().unwrap_or(10000)
            {
                return Err(fail(format!("{name} has an invalid property count")));
            }
            // Unknown keys come first: a misspelt required argument is
            // answered with its spelling, not as missing.
            for key in value.as_object().unwrap().keys() {
                if schema["properties"].get(key).is_none()
                    && schema["additionalProperties"] == false
                {
                    let known: Vec<&str> = schema["properties"]
                        .as_object()
                        .map(|p| p.keys().map(String::as_str).collect())
                        .unwrap_or_default();
                    let remedy = match closest(key, known.iter().copied()) {
                        Some(near) => format!("Did you mean {name}.{near}?"),
                        None if known.is_empty() => format!("{name} must be an empty object"),
                        None => format!("Accepted keys of {name}: {}", known.join(", ")),
                    };
                    return Err(fail(format!("Unknown argument: {name}.{key}")).remedy(remedy));
                }
            }
            if let Some(required) = schema["required"].as_array() {
                for key in required {
                    let key = key.as_str().unwrap_or("");
                    if value.get(key).is_none() {
                        return Err(fail(format!("{name}.{key} is required")));
                    }
                }
            }
            for (key, item) in value.as_object().unwrap() {
                if let Some(spec) = schema["properties"].get(key) {
                    validate(item, spec, &format!("{name}.{key}"))?;
                } else if schema["additionalProperties"].is_object() {
                    validate(
                        item,
                        &schema["additionalProperties"],
                        &format!("{name}.{key}"),
                    )?;
                }
            }
        }
        "array" => {
            let items = value.as_array().unwrap();
            let len = items.len() as u64;
            if len < schema["minItems"].as_u64().unwrap_or(0)
                || len > schema["maxItems"].as_u64().unwrap_or(10000)
            {
                return Err(fail(format!("{name} has an invalid item count")));
            }
            for (i, item) in items.iter().enumerate() {
                validate(item, &schema["items"], &format!("{name}[{i}]"))?;
            }
        }
        "string" => {
            let s = value.as_str().unwrap();
            let len = s.chars().count() as u64;
            if s.contains('\0')
                || len > schema["maxLength"].as_u64().unwrap_or(100000)
                || len < schema["minLength"].as_u64().unwrap_or(0)
            {
                return Err(fail(format!("Invalid string length/content: {name}")));
            }
            if let Some(pattern) = schema["pattern"].as_str() {
                identifier(s, name, Some(pattern))?;
            }
        }
        "integer" | "number" => {
            let n = value.as_f64().unwrap();
            if n < schema["minimum"].as_f64().unwrap_or(f64::NEG_INFINITY)
                || n > schema["maximum"].as_f64().unwrap_or(f64::INFINITY)
            {
                return Err(fail(format!("{name} is out of range")));
            }
        }
        _ => (),
    };
    Ok(())
}
pub fn s<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}
pub fn b(v: &Value, key: &str) -> bool {
    v[key].as_bool().unwrap_or(false)
}
pub fn n(v: &Value, key: &str) -> u64 {
    v[key].as_u64().unwrap_or(0)
}
pub fn strings(v: &Value) -> Vec<String> {
    v.as_array()
        .map(|a| {
            a.iter()
                .filter_map(|s| s.as_str().map(String::from))
                .collect()
        })
        .unwrap_or_default()
}
pub fn argv(values: &[&str]) -> Vec<String> {
    values.iter().map(|s| s.to_string()).collect()
}
pub fn shell_quote(s: &str) -> String {
    if !s.is_empty()
        && s.bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"_@%+=:,./-".contains(&c))
    {
        s.into()
    } else {
        format!("'{}'", s.replace('\'', "'\"'\"'"))
    }
}
pub fn shell_join(args: &[String]) -> String {
    args.iter()
        .map(|s| shell_quote(s))
        .collect::<Vec<_>>()
        .join(" ")
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}
pub fn digest(bytes: &[u8]) -> String {
    hex(&Sha256::digest(bytes))
}
pub fn artifact(path: &Path, started: f64) -> Result {
    let mut file = File::open(path)?;
    let mut hash = Sha256::new();
    let mut buffer = [0u8; 65536];
    loop {
        let size = file.read(&mut buffer)?;
        if size == 0 {
            break;
        }
        hash.update(&buffer[..size]);
    }
    let meta = file.metadata()?;
    Ok(
        json!({"path":path.canonicalize()?,"bytes":meta.len(),"sha256":hex(&hash.finalize()),"modified_in_job":meta.modified()?.duration_since(UNIX_EPOCH).unwrap_or_default().as_secs_f64() >= started - 2.0}),
    )
}
pub struct Lock(File);
impl Lock {
    pub fn open(path: &Path) -> Result<Self> {
        private_dir(path.parent().unwrap())?;
        Ok(Self(
            OpenOptions::new().create(true).append(true).open(path)?,
        ))
    }
    pub fn acquire(&self, nonblocking: bool) -> Result<bool> {
        // SAFETY: flock receives an owned, live file descriptor; Lock keeps it alive.
        if unsafe {
            libc::flock(
                self.0.as_raw_fd(),
                libc::LOCK_EX | if nonblocking { libc::LOCK_NB } else { 0 },
            )
        } == 0
        {
            return Ok(true);
        }
        let error = std::io::Error::last_os_error();
        if error.kind() == std::io::ErrorKind::WouldBlock {
            return Ok(false);
        }
        Err(error.into())
    }
}
pub fn glob_paths(pattern: &str) -> Result<Vec<PathBuf>> {
    let mut values: Vec<_> = glob::glob(pattern)
        .map_err(|e| Error::new("invalid_glob", e.to_string()))?
        .filter_map(|r| r.ok())
        .collect();
    values.sort();
    Ok(values)
}

#[cfg(test)]
mod directory_tests {
    use super::*;
    use std::os::unix::fs::MetadataExt;

    #[test]
    fn a_replaced_running_executable_is_started_at_its_own_path() {
        let parent = tempfile::tempdir().unwrap();
        let path = parent.path().join("tomaccoctl");
        let deleted = PathBuf::from(format!("{} (deleted)", path.display()));
        assert_eq!(without_deleted_suffix(deleted.clone()), path);
        assert_eq!(without_deleted_suffix(path.clone()), path);
        // A file that really has this name stays what it is.
        fs::write(&deleted, b"").unwrap();
        assert_eq!(without_deleted_suffix(deleted.clone()), deleted);
    }

    #[test]
    fn runtime_directories_are_created_private_and_foreign_ones_refused() {
        let parent = tempfile::tempdir().unwrap();
        let fresh = parent.path().join("sockets");
        private_runtime_dir(&fresh).unwrap();
        assert_eq!(fs::metadata(&fresh).unwrap().mode() & 0o777, 0o700);
        // Reused as it is.
        private_runtime_dir(&fresh).unwrap();
        // One another user could enter or write, as a squatter in /tmp leaves it.
        let open = parent.path().join("open");
        fs::create_dir(&open).unwrap();
        fs::set_permissions(&open, fs::Permissions::from_mode(0o777)).unwrap();
        assert_eq!(
            private_runtime_dir(&open).unwrap_err().code,
            "unsafe_runtime_directory"
        );
        // A symlink to a private directory is not that directory.
        let link = parent.path().join("link");
        std::os::unix::fs::symlink(&fresh, &link).unwrap();
        assert!(private_runtime_dir(&link).is_err());
        let file = parent.path().join("file");
        fs::write(&file, b"").unwrap();
        assert!(private_runtime_dir(&file).is_err());
    }

    #[test]
    fn the_data_directory_is_tightened_to_its_owner() {
        let parent = tempfile::tempdir().unwrap();
        let data = parent.path().join("data");
        fs::create_dir(&data).unwrap();
        fs::set_permissions(&data, fs::Permissions::from_mode(0o755)).unwrap();
        owner_only(&data).unwrap();
        assert_eq!(fs::metadata(&data).unwrap().mode() & 0o777, 0o700);
        assert!(owner_only(&parent.path().join("missing")).is_err());
    }

    #[test]
    fn the_closest_name_is_suggested_only_when_it_is_close() {
        let names = [
            "job.wait",
            "job.logs",
            "ios.virtual.start",
            "android.avd_start",
        ];
        assert_eq!(closest("job.wiat", names), Some("job.wait"));
        assert_eq!(closest("JOB.LOGS", names), Some("job.logs"));
        assert_eq!(closest("virtual.start", names), Some("ios.virtual.start"));
        assert_eq!(closest("start", names), Some("ios.virtual.start"));
        // Two operations end in `.start`: neither is the likely one.
        assert_eq!(
            closest("start", ["ios.virtual.start", "runtime.start"]),
            None
        );
        assert_eq!(closest("profile.capture", names), None);
        assert_eq!(closest("x", std::iter::empty()), None);
    }

    #[test]
    fn the_account_home_is_known_without_home() {
        let home = account_home().expect("the test account has a home directory");
        assert!(home.is_absolute());
        assert_ne!(home, Path::new("/tmp"));
    }
}
