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
    /// The error as clients see it, with any secret it quotes masked. An
    /// error without a remedy of its own gets its code's general one from
    /// assets/errors.json.
    pub fn value(&self) -> Value {
        let mut value = serde_json::to_value(self).expect("error is serializable");
        if self.remedy.is_empty() {
            if let Some(remedy) = general_remedy(&self.code) {
                value["remedy"] = json!(remedy);
            }
        }
        crate::redact::value(&mut value);
        value
    }
}
/// Every error code with what it means and what to do, as the error
/// reference (docs/reference/errors.md) lists them.
pub fn error_catalogue() -> &'static serde_json::Map<String, Value> {
    static CATALOGUE: std::sync::OnceLock<serde_json::Map<String, Value>> =
        std::sync::OnceLock::new();
    CATALOGUE.get_or_init(|| {
        serde_json::from_str(include_str!("../assets/errors.json"))
            .expect("the error catalogue is a JSON object")
    })
}
/// The general remedy for an error code, when the catalogue has one.
pub fn general_remedy(code: &str) -> Option<&'static str> {
    error_catalogue().get(code)?["remedy"].as_str()
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        crate::redact::text(&self.message).fmt(f)
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
/// `value` as an absolute path, `~` expanded and `.` and `..` folded away,
/// with no link resolved.
fn lexical(value: &str) -> Result<PathBuf> {
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
    Ok(normalized)
}
pub fn path(value: &str, directory: bool, exists: bool) -> Result<PathBuf> {
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
    let normalized = lexical(value)?;
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
/// The longest Unix socket path the kernel accepts: sun_path holds 108
/// bytes with the terminating NUL.
pub const SOCKET_PATH_LIMIT: usize = 107;
/// A shared directory to make private socket directories in, short enough
/// that `reserve` more bytes (the directory and socket names under it) still
/// fit a socket address. TMPDIR comes first, as for any temporary file; a
/// TMPDIR too long for that (a build sandbox, a nested workspace) gives way to
/// the user's private runtime directory, and then to /tmp.
pub fn socket_root(reserve: usize) -> PathBuf {
    socket_root_from(
        std::env::var_os("TMPDIR").map(PathBuf::from),
        std::env::var_os("XDG_RUNTIME_DIR").map(PathBuf::from),
        reserve,
    )
}
fn socket_root_from(
    temporary: Option<PathBuf>,
    runtime: Option<PathBuf>,
    reserve: usize,
) -> PathBuf {
    use std::os::unix::fs::MetadataExt;
    let fits = |path: &Path| path.as_os_str().len() + reserve <= SOCKET_PATH_LIMIT;
    let usable = |path: &PathBuf| path.is_absolute() && path.is_dir() && fits(path);
    if let Some(path) = temporary.filter(usable) {
        return path;
    }
    // SAFETY: geteuid has no preconditions.
    let owner = unsafe { libc::geteuid() };
    if let Some(path) = runtime.filter(usable).filter(|path| {
        fs::symlink_metadata(path)
            .is_ok_and(|meta| meta.is_dir() && meta.uid() == owner && meta.mode() & 0o077 == 0)
    }) {
        return path;
    }
    PathBuf::from("/tmp")
}
/// A fresh private directory for test sockets, whatever TMPDIR is.
#[cfg(test)]
pub fn socket_tempdir() -> tempfile::TempDir {
    tempfile::Builder::new()
        .prefix("tomacco-test-")
        .tempdir_in(socket_root(48))
        .expect("a private socket directory")
}
/// A file an operation writes: a screenshot, a recording, a trace, a report,
/// an export, a signed release. Its folders may be links (a Pictures folder on
/// another disk); the file itself must not be. Tools such as adb pull and
/// rsync write through a link, so one planted at the destination, in /tmp
/// say, would point the write at another file of the user. A directory, a
/// FIFO or a device is not a file destination either.
pub fn output_path(value: &str) -> Result<PathBuf> {
    let named = lexical(value)?;
    let (Some(parent), Some(name)) = (named.parent(), named.file_name()) else {
        return Err(Error::new(
            "invalid_path",
            "Select a file destination, not a directory",
        ));
    };
    let target = path(&parent.to_string_lossy(), false, false)?.join(name);
    match fs::symlink_metadata(&target) {
        Ok(meta) if meta.file_type().is_symlink() => Err(Error::new(
            "invalid_path",
            format!(
                "The destination is a symbolic link, which would redirect the write: {}",
                target.display()
            ),
        )
        .remedy("Name the file itself, or remove the link")),
        Ok(meta) if meta.is_dir() => Err(Error::new(
            "invalid_path",
            "Select a file destination, not a directory",
        )),
        Ok(meta) if !meta.is_file() => Err(Error::new(
            "invalid_path",
            format!(
                "The destination is not a regular file: {}",
                target.display()
            ),
        )),
        _ => Ok(target),
    }
}
/// Resolves the path of a file that holds a secret (a password, a token, a
/// key). Its error never quotes the value: a secret given where its file's
/// path belongs must not be echoed into an error, a job record or an MCP
/// transcript.
pub fn secret_path(value: &str, label: &str) -> Result<PathBuf> {
    path(value, false, true).map_err(|_| {
        Error::new(
            "path_not_found",
            format!("No file at the path given for {label}"),
        )
        .remedy(format!(
            "Give {label} the absolute path of an owner-only file that holds the secret, not the secret itself"
        ))
    })
}
/// Creates a missing directory owner-only. The directory itself is made
/// with mode 0700 in one step, so it is never open to others, whatever the
/// umask; missing parents get the usual permissions. An existing directory
/// is left as it is.
pub fn private_dir(path: &Path) -> Result<()> {
    use std::os::unix::fs::DirBuilderExt;
    if path.exists() {
        return Ok(());
    }
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    match fs::DirBuilder::new().mode(0o700).create(path) {
        Ok(()) => (),
        // Another process made it in the meantime.
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => return Ok(()),
        Err(error) => return Err(error.into()),
    }
    // The umask can only have removed bits; this keeps the owner's.
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    Ok(())
}
/// Creates or truncates a file only its owner may read: logs that hold
/// commands, environments and tool output. A file left with wider
/// permissions by an earlier version is tightened.
pub fn private_file(path: &Path, append: bool) -> Result<File> {
    use std::os::unix::fs::OpenOptionsExt;
    let file = OpenOptions::new()
        .create(true)
        .write(true)
        .read(!append)
        .append(append)
        .truncate(!append)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    if file.metadata()?.permissions().mode() & 0o077 != 0 {
        file.set_permissions(fs::Permissions::from_mode(0o600))?;
    }
    Ok(file)
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
/// The candidate a mistyped name most likely meant: the fewest edits, within a third of its length (at least two), or else the one
/// candidate whose last dotted parts are the name (`ios.virtual.start` for
/// `virtual.start`). None when nothing is close enough to suggest.
pub fn closest<'a>(name: &str, candidates: impl IntoIterator<Item = &'a str>) -> Option<&'a str> {
    // Edits are insertions, deletions, substitutions and swaps of two
    // neighbouring characters, the commonest typing slip.
    fn distance(a: &str, b: &str) -> usize {
        let (a, b): (Vec<char>, Vec<char>) = (a.chars().collect(), b.chars().collect());
        let mut d = vec![vec![0; b.len() + 1]; a.len() + 1];
        for (i, row) in d.iter_mut().enumerate() {
            row[0] = i;
        }
        for (j, cell) in d[0].iter_mut().enumerate() {
            *cell = j;
        }
        for i in 1..=a.len() {
            for j in 1..=b.len() {
                let cost = usize::from(a[i - 1] != b[j - 1]);
                d[i][j] = (d[i - 1][j] + 1)
                    .min(d[i][j - 1] + 1)
                    .min(d[i - 1][j - 1] + cost);
                if i > 1 && j > 1 && a[i - 1] == b[j - 2] && a[i - 2] == b[j - 1] {
                    d[i][j] = d[i][j].min(d[i - 2][j - 2] + 1);
                }
            }
        }
        d[a.len()][b.len()]
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
    fn an_output_file_may_sit_behind_linked_folders_but_not_be_a_link() {
        let parent = tempfile::tempdir().unwrap();
        let root = parent.path().canonicalize().unwrap();
        let precious = root.join("precious");
        fs::write(&precious, b"keep").unwrap();
        // New and existing regular files are fine.
        let new = root.join("shot.png");
        assert_eq!(output_path(&new.to_string_lossy()).unwrap(), new);
        assert_eq!(output_path(&precious.to_string_lossy()).unwrap(), precious);
        // A linked folder is followed, as for a Pictures folder on another disk.
        let real = root.join("real");
        fs::create_dir(&real).unwrap();
        std::os::unix::fs::symlink(&real, root.join("pictures")).unwrap();
        assert_eq!(
            output_path(&root.join("pictures/../pictures/a.png").to_string_lossy()).unwrap(),
            real.join("a.png")
        );
        // A link at the destination, to a file or to nothing yet, is refused.
        for (name, to) in [
            ("planted", precious.clone()),
            ("dangling", root.join("none")),
        ] {
            let link = root.join(name);
            std::os::unix::fs::symlink(&to, &link).unwrap();
            let error = output_path(&link.to_string_lossy()).unwrap_err();
            assert_eq!(error.code, "invalid_path");
            assert!(error.message.contains("symbolic link"), "{}", error.message);
        }
        // A link behind a linked folder too.
        std::os::unix::fs::symlink(&precious, real.join("b.png")).unwrap();
        assert!(output_path(&root.join("pictures/b.png").to_string_lossy()).is_err());
        // Directories, a FIFO, and nothing at all are not file destinations.
        assert!(output_path(&real.to_string_lossy()).is_err());
        assert!(output_path(&format!("{}/", real.display())).is_err());
        assert!(output_path("/").is_err());
        let fifo = root.join("fifo");
        let name = std::ffi::CString::new(fifo.to_string_lossy().as_bytes()).unwrap();
        // SAFETY: name is a valid NUL-terminated path.
        assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
        assert!(output_path(&fifo.to_string_lossy()).is_err());
        assert_eq!(fs::read(&precious).unwrap(), b"keep");
    }

    #[test]
    fn errors_never_show_a_secret_they_quote() {
        let error = Error::new(
            "update_check_failed",
            "curl: header = \"Authorization: Bearer ghp_0123456789abcdefABCDEF0123\"",
        )
        .remedy("Retry with password=hunter2-x")
        .details(json!({"token":"abc","transport":"https://u:hunter3@example.com/"}));
        let shown = format!("{} {}", error, error.value());
        for secret in ["ghp_0123", "hunter2-x", "\"abc\"", "hunter3"] {
            assert!(!shown.contains(secret), "{shown}");
        }
        // A path the error is about stays readable.
        let missing = secret_path("hunter2-typed-here", "password_file").unwrap_err();
        assert_eq!(missing.code, "path_not_found");
        assert!(!missing.value().to_string().contains("hunter2-typed-here"));
        assert!(
            Error::new("path_not_found", "Path not found: /a/b/key.txt")
                .value()
                .to_string()
                .contains("/a/b/key.txt")
        );
    }

    #[test]
    fn private_directories_and_logs_are_their_owners_alone() {
        let parent = tempfile::tempdir().unwrap();
        // SAFETY: umask only changes this process's file creation mask.
        let previous = unsafe { libc::umask(0) };
        let directory = parent.path().join("a/b/data");
        let created = private_dir(&directory);
        let log = private_file(&directory.join("job.log"), false);
        unsafe { libc::umask(previous) };
        created.unwrap();
        assert_eq!(fs::metadata(&directory).unwrap().mode() & 0o777, 0o700);
        let mut log = log.unwrap();
        log.write_all(b"secret output").unwrap();
        assert_eq!(
            fs::metadata(directory.join("job.log")).unwrap().mode() & 0o777,
            0o600
        );
        // An existing log from an older version is tightened and truncated.
        let old = directory.join("old.log");
        fs::write(&old, b"before").unwrap();
        fs::set_permissions(&old, fs::Permissions::from_mode(0o644)).unwrap();
        drop(private_file(&old, false).unwrap());
        assert_eq!(fs::metadata(&old).unwrap().mode() & 0o777, 0o600);
        assert_eq!(fs::read(&old).unwrap(), b"");
        // Appending keeps what is there.
        fs::write(&old, b"kept").unwrap();
        private_file(&old, true).unwrap().write_all(b"+").unwrap();
        assert_eq!(fs::read(&old).unwrap(), b"kept+");
        // A link planted at a log path is not followed.
        let link = directory.join("link.log");
        std::os::unix::fs::symlink(&old, &link).unwrap();
        assert!(private_file(&link, false).is_err());
        assert_eq!(fs::read(&old).unwrap(), b"kept+");
    }

    #[test]
    fn sockets_go_to_a_root_short_enough_for_their_address() {
        // In /tmp, so that the short paths stay short under any TMPDIR.
        let parent = tempfile::tempdir_in("/tmp").unwrap();
        let short = parent.path().to_path_buf();
        let long = short.join("x".repeat(90));
        fs::create_dir(&long).unwrap();
        let runtime = short.join("run");
        fs::create_dir(&runtime).unwrap();
        fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700)).unwrap();
        let reserve = SOCKET_PATH_LIMIT - runtime.as_os_str().len();
        // A short TMPDIR is used as it is.
        assert_eq!(
            socket_root_from(Some(short.clone()), Some(runtime.clone()), 0),
            short
        );
        // A long one gives way to a private runtime directory that fits.
        assert_eq!(
            socket_root_from(Some(long.clone()), Some(runtime.clone()), reserve),
            runtime
        );
        // A runtime directory others may enter is not used.
        fs::set_permissions(&runtime, fs::Permissions::from_mode(0o755)).unwrap();
        assert_eq!(
            socket_root_from(Some(long.clone()), Some(runtime.clone()), reserve),
            Path::new("/tmp")
        );
        // Neither set, relative or missing: /tmp.
        assert_eq!(socket_root_from(None, None, 20), Path::new("/tmp"));
        assert_eq!(
            socket_root_from(Some("relative".into()), None, 20),
            Path::new("/tmp")
        );
        assert_eq!(
            socket_root_from(Some(short.join("missing")), None, 20),
            Path::new("/tmp")
        );
        let directory = socket_tempdir();
        assert!(directory.path().as_os_str().len() + 40 <= SOCKET_PATH_LIMIT);
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

#[cfg(test)]
mod error_catalogue_tests {
    use super::*;
    /// A source file without its `#[cfg(test)] mod` blocks, whose errors
    /// are made up for the tests.
    fn without_tests(text: &str) -> String {
        let pattern =
            regex::Regex::new(r"#\[cfg\(test\)\]\s*(?:#\[[^\]]*\]\s*)*mod \w+\s*\{").unwrap();
        let mut kept = String::new();
        let mut from = 0;
        while let Some(found) = pattern.find_at(text, from) {
            kept.push_str(&text[from..found.start()]);
            // An unclosed block, such as one whose strings hold braces, runs to the end.
            let mut depth = 1;
            let mut end = text.len();
            for (offset, c) in text[found.end()..].char_indices() {
                depth += match c {
                    '{' => 1,
                    '}' => -1,
                    _ => 0,
                };
                if depth == 0 {
                    end = found.end() + offset + 1;
                    break;
                }
            }
            from = end;
        }
        kept.push_str(&text[from..]);
        kept
    }
    /// Every error code written as `Error::new("code", ...)` in native/,
    /// with a file it is in.
    fn codes_in_sources() -> std::collections::BTreeMap<String, String> {
        let pattern = regex::Regex::new(r#"Error::new\(\s*"([a-z0-9_]+)""#).unwrap();
        let mut codes = std::collections::BTreeMap::new();
        let mut directories = vec![Path::new(env!("CARGO_MANIFEST_DIR")).join("native")];
        while let Some(directory) = directories.pop() {
            for entry in fs::read_dir(&directory).unwrap() {
                let path = entry.unwrap().path();
                let name = path.file_name().unwrap().to_string_lossy().into_owned();
                if path.is_dir() {
                    directories.push(path);
                } else if name.ends_with(".rs") && !name.ends_with("_tests.rs") {
                    let text = without_tests(&fs::read_to_string(&path).unwrap());
                    for found in pattern.captures_iter(&text) {
                        codes.entry(found[1].to_string()).or_insert(name.clone());
                    }
                }
            }
        }
        codes
    }
    /// Codes made from a variable, which the scan cannot see.
    const DYNAMIC: &[&str] = &["job_failed", "job_cancelled", "job_timed_out"];

    #[test]
    fn every_error_code_has_a_cause_and_a_remedy_in_the_catalogue() {
        let catalogue = error_catalogue();
        let codes = codes_in_sources();
        assert!(
            codes.len() > 400,
            "the scan found only {} codes",
            codes.len()
        );
        let missing: Vec<String> = codes
            .iter()
            .filter(|(code, _)| !catalogue.contains_key(*code))
            .map(|(code, file)| format!("{code} ({file})"))
            .collect();
        assert!(
            missing.is_empty(),
            "add these codes to assets/errors.json with a cause and a remedy, then run python3 tools/build_reference.py: {}",
            missing.join(", ")
        );
        for (code, entry) in catalogue {
            for key in ["cause", "remedy"] {
                assert!(
                    entry[key]
                        .as_str()
                        .is_some_and(|text| text.trim().len() > 3),
                    "{code} has no {key}"
                );
            }
            assert!(
                codes.contains_key(code) || DYNAMIC.contains(&code.as_str()),
                "assets/errors.json lists {code}, which no code returns any more"
            );
        }
    }
    #[test]
    fn an_error_without_a_remedy_gets_its_codes_general_one() {
        let bare = Error::new("job_not_found", "Unknown job x").value();
        assert_eq!(bare["remedy"], general_remedy("job_not_found").unwrap());
        let own = Error::new("job_not_found", "Unknown job x")
            .remedy("Use job.list")
            .value();
        assert_eq!(own["remedy"], "Use job.list");
        let unknown = Error::new("from_an_adapter", "x").value();
        assert_eq!(unknown["remedy"], "");
    }
}
