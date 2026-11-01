//! The installer: installs a release package for one user, rolls an
//! installation back and uninstalls it, as `tomaccoctl _install` (with the
//! flags of `install.py`) and `tomaccoctl uninstall`.
//!
//! Every change goes through a write-ahead journal in
//! `~/.local/share/tomacco/install-backups/<stamp>/manifest.json`, with the
//! replaced files beside it. The journal and backups have the layout
//! `install.py` writes, so either installer rolls back or uninstalls what the
//! other installed.
//!
//! Where an integration is added (a file the installation owns, or an edit to
//! the user's own configuration): its own method on [`Installer`] like
//! [`Installer::window_rule`], called from [`install_package`] after the
//! program's files; what it owns outright goes into [`OWNED`] (and
//! [`OWNED_DIRECTORIES`]) so `--uninstall` removes it, and an edit to the
//! user's configuration gets an `unedit_*` counterpart called first in
//! [`Installer::uninstall`].
use crate::{core::*, product::*};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, HashSet},
    ffi::CString,
    fs::{self, File},
    io::{self, Read, Write},
    os::unix::{
        ffi::OsStrExt,
        fs::{DirBuilderExt, MetadataExt, PermissionsExt},
        io::AsRawFd,
    },
    path::{Component, Path, PathBuf},
};

pub const APP_ID: &str = "io.github.marselnenaj.tomacco";
/// Mutable user data: projects, device profiles, settings, runtime state and
/// the installation backups. Neither an installation nor an uninstallation
/// touches it.
const USER_DATA: &str = ".local/share/tomacco";
/// man finds pages in ../share/man beside each directory on PATH, so
/// ~/.local/bin/tomaccoctl gets its page from here.
const MANUAL_PAGE: &str = ".local/share/man/man1/tomaccoctl.1";
const DESKTOP_ENTRY: &str = ".local/share/applications/io.github.marselnenaj.tomacco.desktop";
const ICON: &str = ".local/share/icons/hicolor/scalable/apps/io.github.marselnenaj.tomacco.svg";
const METAINFO: &str = ".local/share/metainfo/io.github.marselnenaj.tomacco.metainfo.xml";
const PLUGIN: &str = ".config/omarchy/plugins/io.github.marselnenaj.tomacco";
const DOCUMENTS: &str = ".local/share/doc/tomacco";
const SHELL_CONFIG: &str = ".config/omarchy/shell.json";
const HYPR_CONFIG: &str = ".config/hypr/hyprland.lua";
const HYPR_RULES_FILE: &str = ".config/hypr/tomacco.lua";
/// What an installation owns outright. `--uninstall` removes these files when
/// they are still exactly as installed; everything else it leaves alone.
pub(crate) const OWNED: &[&str] = &[
    ".local/bin/tomacco",
    ".local/bin/tomaccoctl",
    INSTALLED_LIBRARY,
    DOCUMENTS,
    MANUAL_PAGE,
    DESKTOP_ENTRY,
    ICON,
    METAINFO,
    PLUGIN,
    HYPR_RULES_FILE,
];
/// Directories the installation creates for itself, removed once empty.
const OWNED_DIRECTORIES: &[&str] = &[INSTALLED_LIBRARY, DOCUMENTS, PLUGIN];
/// The agent skill: installed with the program, and linked into the skills
/// directory of each coding agent whose configuration directory exists.
const SKILL: &str = ".local/lib/tomacco/skills/tomacco";
const AGENT_SKILL_LINKS: &[&str] = &[
    ".claude/skills/tomacco",
    ".codex/skills/tomacco",
    ".agents/skills/tomacco",
];
const HYPR_REQUIRE: &str = "require(\"hypr.tomacco\")";
const HYPR_COMMENT: &str = "-- Tomacco mobile workspace.";
/// A portable package carries Qt, Qt WebEngine and FFmpeg here; the
/// installation keeps them in ~/.local/lib/tomacco/runtime, and qt.conf beside
/// the program points Qt at them.
const RUNTIME: &str = "lib/tomacco/runtime";
const QT_CONF: &str = "[Paths]\nPrefix=runtime\n";
/// Files are staged and published in batches of up to this many: the journal
/// is written and the disk flushed once a batch, not once a file.
const BATCH: usize = 512;
/// The prefix of a file staged beside its destination and not yet published.
/// One that a killed run left behind is deleted by the next run that stages there.
pub(crate) const STAGED: &str = ".tomacco-staged-";

pub const HYPR_RULES: &str = r#"-- Tomacco: floating mobile workspace; F11 toggles fullscreen.
o.window({ class = "^io[.]github[.]marselnenaj[.]tomacco$", title = "negative:^Tomacco – .*$" }, {
  float = true,
  tile = false,
  center = true,
  opacity = "1.0 override 1.0 override",
  size = { 1380, 970 },
})

-- Detached device window: the app draws the phone itself, so the compositor
-- adds no border, shadow, rounding or blur and never resizes it.
o.window({ class = "^io[.]github[.]marselnenaj[.]tomacco$", title = "^Tomacco – .*$" }, {
  float = true,
  tile = false,
  border_size = 0,
  rounding = 0,
  no_shadow = true,
  no_blur = true,
  opacity = "1.0 override 1.0 override",
})
"#;

fn refuse(message: impl Into<String>) -> Error {
    Error::new("install_refused", message)
}

fn io_context(what: &Path) -> impl Fn(io::Error) -> Error + '_ {
    move |error| Error::new("io_error", format!("{}: {error}", what.display()))
}

fn hex_digest(bytes: &[u8]) -> String {
    digest(bytes)
}

fn file_digest(path: &Path) -> io::Result<String> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0u8; 1 << 20];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(hasher
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect())
}

fn is_symlink(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok_and(|meta| meta.file_type().is_symlink())
}

/// A regular file, following links, as Python's `Path.is_file`.
fn is_file(path: &Path) -> bool {
    fs::metadata(path).is_ok_and(|meta| meta.is_file())
}

fn is_dir(path: &Path) -> bool {
    fs::metadata(path).is_ok_and(|meta| meta.is_dir())
}

fn mode_of(path: &Path) -> io::Result<u32> {
    Ok(fs::metadata(path)?.permissions().mode() & 0o777)
}

fn executable(path: &Path) -> bool {
    let Ok(text) = CString::new(path.as_os_str().as_bytes()) else {
        return false;
    };
    // SAFETY: text is a valid NUL-terminated string for the duration of the call.
    unsafe { libc::access(text.as_ptr(), libc::X_OK) == 0 }
}

/// The path with every existing part resolved and the rest appended, as
/// Python's non-strict `Path.resolve`.
fn resolve(path: &Path) -> PathBuf {
    let mut existing = path;
    let mut rest = Vec::new();
    loop {
        if let Ok(found) = existing.canonicalize() {
            return rest.iter().rev().fold(found, |path, name| path.join(name));
        }
        match (existing.parent(), existing.file_name()) {
            (Some(parent), Some(name)) => {
                rest.push(name.to_os_string());
                existing = if parent.as_os_str().is_empty() {
                    Path::new(".")
                } else {
                    parent
                };
            }
            _ => return path.to_path_buf(),
        }
    }
}

/// A relative path as Python's `str(Path(...))` spells it: no `.` parts, no
/// doubled or trailing slashes.
fn normal(relative: &Path) -> String {
    relative
        .components()
        .filter(|component| *component != Component::CurDir)
        .collect::<PathBuf>()
        .to_string_lossy()
        .into_owned()
}

/// JSON as Python's `json.dumps(value, indent=2)` writes it: non-ASCII
/// characters escaped.
pub(crate) fn ascii_json(value: &Value) -> String {
    let text = serde_json::to_string_pretty(value).expect("JSON values serialise");
    let mut out = String::with_capacity(text.len());
    for character in text.chars() {
        if character.is_ascii() {
            out.push(character);
        } else {
            let mut units = [0u16; 2];
            for unit in character.encode_utf16(&mut units) {
                out.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    out
}

/// JSON as `json.dumps(value, ensure_ascii=False, indent=2) + "\n"`.
fn config_json(value: &Value) -> String {
    serde_json::to_string_pretty(value).expect("JSON values serialise") + "\n"
}

/// The local time as `%Y%m%d-%H%M%S-%f`, the name of a run's backup directory.
fn stamp() -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let seconds = now.as_secs() as libc::time_t;
    // SAFETY: tm is plain data, filled by localtime_r from a valid time_t.
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    unsafe { libc::localtime_r(&seconds, &mut tm) };
    format!(
        "{:04}{:02}{:02}-{:02}{:02}{:02}-{:06}",
        tm.tm_year + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec,
        now.subsec_micros()
    )
}

fn state(sha256: String, mode: u32) -> Value {
    json!({"sha256": sha256, "mode": mode})
}

fn layout_id(item: &Value) -> Option<&str> {
    match item {
        Value::String(text) => Some(text),
        Value::Object(map) => map.get("id").and_then(Value::as_str),
        _ => None,
    }
}

fn places_widget(layout: &Map<String, Value>) -> bool {
    layout
        .values()
        .filter_map(Value::as_array)
        .flatten()
        .any(|item| layout_id(item) == Some(APP_ID))
}

/// Gets the data and names of the files and directories in `paths` onto the
/// disk: one syncfs(2) for each file system they are on or, where that is not
/// available, one fsync for each path. Returns how many fsyncs it made.
fn make_durable(paths: &[PathBuf], syncfs: bool) -> io::Result<usize> {
    if syncfs {
        let mut devices: Vec<(u64, &PathBuf)> = Vec::new();
        for path in paths {
            if let Ok(meta) = fs::metadata(path) {
                if !devices.iter().any(|(device, _)| *device == meta.dev()) {
                    devices.push((meta.dev(), path));
                }
            }
        }
        let mut failed = false;
        for (_, path) in devices {
            let file = File::open(path)?;
            // SAFETY: the descriptor is open for the duration of the call.
            failed = unsafe { libc::syncfs(file.as_raw_fd()) } != 0;
            if failed {
                break;
            }
        }
        if !failed {
            return Ok(0);
        }
    }
    let mut synced = 0;
    for path in paths {
        let file = match File::open(path) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error),
        };
        file.sync_all()?;
        synced += 1;
    }
    Ok(synced)
}

/// What tests watch and change. A real run has the defaults.
pub(crate) struct Hooks {
    pub batch: usize,
    pub syncfs: bool,
    pub link: bool,
    /// Called before each staged change is published (renamed or unlinked).
    pub before_publish: Option<Box<dyn FnMut(&Path) -> io::Result<()>>>,
    /// When set, records journal sizes, durable calls, fsyncs and publications.
    pub trace: Option<Vec<Event>>,
}

#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Event {
    Journal(usize),
    Durable(Vec<PathBuf>),
    Fsyncs(usize),
    Publish(PathBuf),
}

impl Default for Hooks {
    fn default() -> Self {
        Hooks {
            batch: BATCH,
            syncfs: true,
            link: true,
            before_publish: None,
            trace: None,
        }
    }
}

impl Hooks {
    /// A debug build's hooks for the tests that kill an installation: a smaller
    /// batch, and an exit before the given publication as on a power cut.
    fn from_environment() -> Self {
        #[allow(unused_mut)]
        let mut hooks = Hooks::default();
        #[cfg(debug_assertions)]
        {
            if let Some(batch) = std::env::var("TOMACCO_INSTALL_TEST_BATCH")
                .ok()
                .and_then(|value| value.parse().ok())
            {
                hooks.batch = batch;
            }
            if let Some(at) = std::env::var("TOMACCO_INSTALL_TEST_KILL_AT")
                .ok()
                .and_then(|value| value.parse::<usize>().ok())
            {
                let mut count = 0;
                hooks.before_publish = Some(Box::new(move |_| {
                    count += 1;
                    if count == at {
                        // SAFETY: ends the process at once, without cleanup, on purpose.
                        unsafe { libc::_exit(9) };
                    }
                    Ok(())
                }));
            }
        }
        hooks
    }
}

struct Staged {
    /// The temporary file beside the destination, or None for a removal.
    temp: Option<PathBuf>,
    path: PathBuf,
    saved: Option<PathBuf>,
}

/// Changes files in a home through a write-ahead journal.
///
/// `write` and `remove` stage a change: the new content goes into a temporary
/// file beside the destination and the old content into this run's backup
/// directory. `flush`, which runs every batch of changes and from `finish`,
/// records the batch in manifest.json, gets the staged files, the backups and
/// the manifest onto the disk, and only then publishes the batch by renaming
/// or unlinking. After an interruption each journalled file is either as it
/// was before or as installed, and `rollback` accepts both.
pub struct Installer {
    pub user: PathBuf,
    pub backup: PathBuf,
    version: String,
    pub changed: Vec<PathBuf>,
    pub records: Vec<Value>,
    pending: Vec<Staged>,
    pending_paths: HashSet<PathBuf>,
    swept: HashSet<PathBuf>,
    private: bool,
    batched: bool,
    journalled: usize,
    pub(crate) hooks: Hooks,
}

impl Installer {
    pub fn new(user: &Path, version: &str) -> Self {
        let user = resolve(user);
        let backup = user.join(INSTALL_BACKUPS).join(stamp());
        Installer {
            user,
            backup,
            version: version.to_string(),
            changed: Vec::new(),
            records: Vec::new(),
            pending: Vec::new(),
            pending_paths: HashSet::new(),
            swept: HashSet::new(),
            private: false,
            batched: false,
            journalled: 0,
            hooks: Hooks::from_environment(),
        }
    }

    fn event(&mut self, event: Event) {
        if let Some(trace) = self.hooks.trace.as_mut() {
            trace.push(event);
        }
    }

    /// Creates this run's backup directory and the data directories above it
    /// owner-only: backups are copies of the user's configuration, and
    /// tomaccoctl keeps job records and credentials' paths beside them.
    fn private_backup(&mut self) -> Result<()> {
        if self.private {
            return Ok(());
        }
        for directory in [
            self.user.join(USER_DATA),
            self.user.join(INSTALL_BACKUPS),
            self.backup.clone(),
        ] {
            if !is_dir(&directory) {
                if let Some(parent) = directory.parent() {
                    fs::create_dir_all(parent).map_err(io_context(parent))?;
                }
                match fs::DirBuilder::new().mode(0o700).create(&directory) {
                    Err(error) if error.kind() != io::ErrorKind::AlreadyExists => {
                        return Err(io_context(&directory)(error));
                    }
                    _ => (),
                }
            }
            let meta = fs::metadata(&directory).map_err(io_context(&directory))?;
            // SAFETY: geteuid has no preconditions.
            if meta.uid() != unsafe { libc::geteuid() } {
                return Err(refuse(format!(
                    "Refusing a backup directory this user does not own: {}",
                    directory.display()
                )));
            }
            fs::set_permissions(&directory, fs::Permissions::from_mode(0o700))
                .map_err(io_context(&directory))?;
        }
        self.private = true;
        Ok(())
    }

    fn destination(&self, relative: &Path) -> Result<PathBuf> {
        self.destination_of(relative, false)
    }

    /// The path in the home; with `link`, the path may be a symbolic link itself.
    fn destination_of(&self, relative: &Path, link: bool) -> Result<PathBuf> {
        if relative.is_absolute()
            || relative
                .components()
                .any(|component| component == Component::ParentDir)
            || normal(relative).is_empty()
        {
            return Err(refuse(
                "Installation paths must stay inside the selected home",
            ));
        }
        let path = self.user.join(relative);
        let parent = path.parent().unwrap_or(&self.user);
        if !resolve(parent).starts_with(&self.user) || (is_symlink(&path) && !link) {
            return Err(refuse(format!(
                "Refusing to follow an installation symlink: {}",
                path.display()
            )));
        }
        Ok(path)
    }

    /// Deletes the staged files an interrupted run left in `directory`, once a
    /// run: they were never published, so nothing refers to them.
    fn sweep(&mut self, directory: &Path) -> Result<()> {
        if !self.swept.insert(directory.to_path_buf()) {
            return Ok(());
        }
        let Ok(entries) = fs::read_dir(directory) else {
            return Ok(());
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if entry.file_name().as_bytes().starts_with(STAGED.as_bytes())
                && (is_symlink(&path) || is_file(&path))
            {
                fs::remove_file(&path).map_err(io_context(&path))?;
            }
        }
        Ok(())
    }

    /// Keeps the current content of `path` in this run's backup. A file is
    /// only ever replaced by renaming over it or unlinked, never changed in
    /// place, so a hard link holds the old content; a copy where linking fails.
    fn save(&mut self, relative: &Path, path: &Path) -> Result<PathBuf> {
        let saved = self.backup.join(relative);
        self.private_backup()?;
        let parent = saved.parent().unwrap_or(&self.backup);
        fs::create_dir_all(parent).map_err(io_context(parent))?;
        let linked = self.hooks.link && fs::hard_link(path, &saved).is_ok();
        if !linked {
            fs::copy(path, &saved).map_err(io_context(path))?;
        }
        Ok(saved)
    }

    fn stage(
        &mut self,
        relative: &Path,
        staged: Staged,
        after: Option<Value>,
        before: Option<Value>,
    ) -> Result<()> {
        self.stage_with(relative, staged, after, before, Vec::new())
    }

    fn stage_with(
        &mut self,
        relative: &Path,
        staged: Staged,
        after: Option<Value>,
        before: Option<Value>,
        created: Vec<String>,
    ) -> Result<()> {
        let mut record = json!({"path": normal(relative), "before": before, "after": after});
        if !created.is_empty() {
            record["created"] = json!(created);
        }
        self.records.push(record);
        self.pending_paths.insert(staged.path.clone());
        self.pending.push(staged);
        if self.pending.len() >= if self.batched { self.hooks.batch } else { 1 } {
            self.flush()?;
        }
        Ok(())
    }

    pub fn write(&mut self, relative: impl AsRef<Path>, data: &[u8], mode: u32) -> Result<()> {
        let relative = relative.as_ref();
        let path = self.destination(relative)?;
        if self.pending_paths.contains(&path) {
            self.flush()?;
        }
        let parent = path.parent().unwrap_or(&self.user).to_path_buf();
        fs::create_dir_all(&parent).map_err(io_context(&parent))?;
        if is_symlink(&path) {
            return Err(refuse(format!(
                "Refusing to replace symlink: {}",
                path.display()
            )));
        }
        let mut old = None;
        let mut saved = None;
        if path.exists() {
            let current = fs::read(&path).map_err(io_context(&path))?;
            let current_mode = mode_of(&path).map_err(io_context(&path))?;
            if current == data && current_mode == mode {
                return Ok(());
            }
            saved = Some(self.save(relative, &path)?);
            old = Some(state(hex_digest(&current), current_mode));
        }
        self.sweep(&parent)?;
        let temp = tempfile::Builder::new()
            .prefix(STAGED)
            .tempfile_in(&parent)
            .map_err(io_context(&parent))?;
        let written = (|| {
            temp.as_file().write_all(data)?;
            fs::set_permissions(temp.path(), fs::Permissions::from_mode(mode))
        })();
        written.map_err(io_context(temp.path()))?;
        let temp = temp
            .into_temp_path()
            .keep()
            .map_err(|error| io_context(&parent)(error.error))?;
        self.stage(
            relative,
            Staged {
                temp: Some(temp),
                path,
                saved,
            },
            Some(state(hex_digest(data), mode)),
            old,
        )
    }

    pub fn write_text(&mut self, relative: impl AsRef<Path>, text: &str) -> Result<()> {
        self.write(relative, text.as_bytes(), 0o644)
    }

    pub fn remove(&mut self, relative: impl AsRef<Path>) -> Result<()> {
        let relative = relative.as_ref();
        let target = self.destination(relative)?;
        if self.pending_paths.contains(&target) {
            self.flush()?;
        }
        let data = fs::read(&target).map_err(io_context(&target))?;
        let previous = state(
            hex_digest(&data),
            mode_of(&target).map_err(io_context(&target))?,
        );
        let saved = self.save(relative, &target)?;
        self.stage(
            relative,
            Staged {
                temp: None,
                path: target,
                saved: Some(saved),
            },
            None,
            Some(previous),
        )
    }

    /// Stages a symbolic link at `relative` to the absolute `target`. The
    /// journal entry's state is {"symlink": target}; directories the link
    /// needed are made now and listed under "created", so removing the link
    /// can remove them again.
    pub fn link(&mut self, relative: impl AsRef<Path>, target: &str) -> Result<()> {
        let relative = relative.as_ref();
        let path = self.destination_of(relative, true)?;
        if self.pending_paths.contains(&path) {
            self.flush()?;
        }
        if is_symlink(&path) {
            if fs::read_link(&path).is_ok_and(|current| current.as_os_str() == target) {
                return Ok(());
            }
        }
        if is_symlink(&path) || path.exists() {
            return Err(refuse(format!("Refusing to replace {}", path.display())));
        }
        let mut missing = Vec::new();
        let mut parent = path.parent().unwrap_or(&self.user).to_path_buf();
        while !is_dir(&parent) {
            missing.push(parent.clone());
            parent = parent.parent().unwrap_or(Path::new("/")).to_path_buf();
        }
        let mut created = Vec::new();
        for directory in missing.iter().rev() {
            fs::create_dir(directory).map_err(io_context(directory))?;
            created.push(normal(directory.strip_prefix(&self.user).unwrap_or(directory)));
        }
        let parent = path.parent().unwrap_or(&self.user).to_path_buf();
        self.sweep(&parent)?;
        let token = uuid::Uuid::new_v4().simple().to_string();
        let temp = parent.join(format!("{STAGED}{}", &token[..16]));
        std::os::unix::fs::symlink(target, &temp).map_err(io_context(&temp))?;
        self.stage_with(
            relative,
            Staged {
                temp: Some(temp),
                path,
                saved: None,
            },
            Some(json!({"symlink": target})),
            None,
            created,
        )
    }

    /// Stages the removal of the symbolic link at `relative`.
    pub fn unlink(&mut self, relative: impl AsRef<Path>) -> Result<()> {
        let relative = relative.as_ref();
        let path = self.destination_of(relative, true)?;
        if self.pending_paths.contains(&path) {
            self.flush()?;
        }
        let target = fs::read_link(&path).map_err(io_context(&path))?;
        self.stage(
            relative,
            Staged {
                temp: None,
                path,
                saved: None,
            },
            None,
            Some(json!({"symlink": target.to_string_lossy()})),
        )
    }

    /// Removes the directories a link needed, deepest first, while they are empty.
    fn remove_created(&mut self, directories: &[String]) -> Result<()> {
        self.flush()?;
        let mut unique: Vec<&String> = directories.iter().collect();
        unique.sort();
        unique.dedup();
        unique.sort_by_key(|name| std::cmp::Reverse(Path::new(name).components().count()));
        for relative in unique {
            let path = self.user.join(relative);
            if is_dir(&path)
                && !is_symlink(&path)
                && fs::read_dir(&path).is_ok_and(|mut entries| entries.next().is_none())
            {
                fs::remove_dir(&path).map_err(io_context(&path))?;
            }
        }
        Ok(())
    }

    /// Writes manifest.json with every change recorded so far; the caller makes it durable.
    fn journal(&mut self) -> Result<PathBuf> {
        self.private_backup()?;
        self.journalled = self.records.len();
        let journalled = self.journalled;
        self.event(Event::Journal(journalled));
        let manifest = self.backup.join("manifest.json");
        let created = self
            .backup
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned();
        let value = json!({"format": 1, "created": created, "version": self.version,
            "files": self.records});
        let mut temporary = tempfile::Builder::new()
            .prefix(".manifest-")
            .tempfile_in(&self.backup)
            .map_err(io_context(&self.backup))?;
        temporary
            .write_all((ascii_json(&value) + "\n").as_bytes())
            .map_err(io_context(&manifest))?;
        temporary
            .persist(&manifest)
            .map_err(|error| io_context(&manifest)(error.error))?;
        Ok(manifest)
    }

    fn durable(&mut self, paths: Vec<PathBuf>) -> Result<()> {
        let syncfs = self.hooks.syncfs;
        let synced = make_durable(&paths, syncfs).map_err(io_context(&self.backup))?;
        self.event(Event::Durable(paths));
        if synced > 0 {
            self.event(Event::Fsyncs(synced));
        }
        Ok(())
    }

    /// Journals the staged batch, makes it and its backups durable, then publishes it.
    pub fn flush(&mut self) -> Result<()> {
        if self.pending.is_empty() {
            return Ok(());
        }
        let mut pending = std::mem::take(&mut self.pending);
        self.pending_paths.clear();
        let result = self.publish(&mut pending);
        for staged in &pending {
            if let Some(temp) = &staged.temp {
                // A staged link may point nowhere yet.
                let _ = fs::remove_file(temp);
            }
        }
        result
    }

    fn publish(&mut self, pending: &mut [Staged]) -> Result<()> {
        let manifest = self.journal()?;
        // Write-ahead: the new content, the old content and the journal are on
        // the disk before any destination changes.
        let mut paths = vec![manifest, self.backup.clone()];
        paths.extend(pending.iter().filter_map(|staged| staged.temp.clone()));
        paths.extend(pending.iter().filter_map(|staged| staged.saved.clone()));
        self.durable(paths)?;
        let mut directories: Vec<PathBuf> = Vec::new();
        for staged in pending.iter_mut() {
            if let Some(hook) = self.hooks.before_publish.as_mut() {
                hook(&staged.path).map_err(io_context(&staged.path))?;
            }
            match &staged.temp {
                Some(temp) => fs::rename(temp, &staged.path),
                None => fs::remove_file(&staged.path),
            }
            .map_err(io_context(&staged.path))?;
            staged.temp = None;
            staged.saved = None;
            let parent = staged.path.parent().unwrap_or(&self.user).to_path_buf();
            if !directories.contains(&parent) {
                directories.push(parent);
            }
            self.changed.push(staged.path.clone());
            let path = staged.path.clone();
            self.event(Event::Publish(path));
        }
        self.durable(directories)
    }

    /// Stages changes in batches until `body` ends, which publishes the rest.
    /// Inside it a change is visible only after the next flush; outside it
    /// each change is published at once.
    pub fn batch<T>(&mut self, body: impl FnOnce(&mut Self) -> Result<T>) -> Result<T> {
        self.batched = true;
        let result = body(self).and_then(|value| self.flush().map(|()| value));
        self.batched = false;
        if result.is_err() {
            // Staged and never journalled: nothing refers to these files.
            for staged in std::mem::take(&mut self.pending) {
                if let Some(temp) = staged.temp {
                    let _ = fs::remove_file(temp);
                }
            }
            self.pending_paths.clear();
        }
        result
    }

    pub fn finish(&mut self) -> Result<Option<PathBuf>> {
        self.flush()?;
        if !self.records.is_empty() && self.journalled != self.records.len() {
            let manifest = self.journal()?;
            let backup = self.backup.clone();
            self.durable(vec![manifest, backup])?;
        }
        Ok((!self.records.is_empty()).then(|| self.backup.clone()))
    }

    pub fn rollback(&mut self, identifier: &str) -> Result {
        if identifier.is_empty()
            || identifier == "."
            || identifier == ".."
            || identifier.contains('/')
        {
            return Err(refuse("Use a backup ID from --list-backups"));
        }
        let source = self.user.join(INSTALL_BACKUPS).join(identifier);
        let manifest_path = source.join("manifest.json");
        if !is_file(&manifest_path) {
            return Err(refuse(
                "This backup has no rollback manifest; its files remain available for manual recovery",
            ));
        }
        let manifest: Value = serde_json::from_slice(
            &fs::read(&manifest_path).map_err(io_context(&manifest_path))?,
        )?;
        let entries = match (&manifest["format"], manifest["files"].as_array()) {
            (Value::Number(format), Some(files)) if format.as_u64() == Some(1) => files.clone(),
            _ => return Err(refuse("Unsupported backup manifest")),
        };
        let mut changes = Vec::new();
        let source_resolved = resolve(&source);
        // Validate the complete restore before changing any current file.
        for entry in &entries {
            let relative = PathBuf::from(entry["path"].as_str().unwrap_or(""));
            let symlink = [&entry["before"], &entry["after"]]
                .iter()
                .any(|state| state.get("symlink").is_some());
            let target = self.destination_of(&relative, symlink)?;
            let changed_since = || {
                refuse(format!(
                    "Preserved a file changed since installation: {}",
                    target.display()
                ))
            };
            let Some(current) = current_state(&target)? else {
                return Err(changed_since());
            };
            if current == entry["before"] {
                // A journalled write may not have been published before interruption.
                continue;
            }
            if current != entry["after"] {
                return Err(changed_since());
            }
            let previous = entry["before"].clone();
            if symlink {
                changes.push((relative, previous, Change::Link(entry.clone())));
                continue;
            }
            let mut content = Change::Remove;
            if !previous.is_null() {
                let saved = source.join(&relative);
                if is_symlink(&saved)
                    || !resolve(&saved).starts_with(&source_resolved)
                    || !is_file(&saved)
                {
                    return Err(refuse(format!(
                        "Missing backup file: {}",
                        relative.display()
                    )));
                }
                if file_digest(&saved).map_err(io_context(&saved))? != s(&previous, "sha256") {
                    return Err(refuse(format!(
                        "Backup checksum mismatch: {}",
                        relative.display()
                    )));
                }
                content = Change::Restore(saved);
            }
            changes.push((relative, previous, content));
        }
        // The backups are read again one at a time, not held in memory together.
        let mut created = Vec::new();
        self.batch(|this| {
            for (relative, previous, change) in &changes {
                match change {
                    // A symbolic link, by its journal entry.
                    Change::Link(entry) => {
                        if !previous.is_null() {
                            this.link(relative, s(previous, "symlink"))?;
                        } else {
                            this.unlink(relative)?;
                            created.extend(strings(&entry["created"]));
                        }
                    }
                    Change::Restore(saved) => {
                        let content = fs::read(saved).map_err(io_context(saved))?;
                        if hex_digest(&content) != s(previous, "sha256") {
                            return Err(refuse(format!(
                                "Backup checksum mismatch: {}",
                                relative.display()
                            )));
                        }
                        let mode = previous["mode"].as_u64().unwrap_or(0o644) as u32;
                        this.write(relative, &content, mode)?;
                    }
                    // Only a file recorded as newly created by this installation is removed.
                    Change::Remove => this.remove(relative)?,
                }
            }
            Ok(())
        })?;
        self.remove_created(&created)?;
        self.finish()?;
        Ok(json!({"restored": identifier, "changed": self.changed.len(),
            "backups": self.backup.to_string_lossy()}))
    }

    pub fn launchers(&mut self, portable: bool) -> Result<()> {
        let target = self.user.join(INSTALLED_LIBRARY);
        for name in [DESKTOP_NAME, CLI_NAME] {
            let launcher = self.user.join(INSTALLED_LAUNCHERS).join(name);
            if launcher.exists() {
                let text = fs::read(&launcher).map_err(io_context(&launcher))?;
                if !String::from_utf8_lossy(&text).contains(INSTALLER_MARKER) {
                    return Err(refuse(format!(
                        "Refusing to replace another command: {}",
                        launcher.display()
                    )));
                }
            }
            let script = launcher_script(name, &target, portable);
            self.write(
                Path::new(INSTALLED_LAUNCHERS).join(name),
                script.as_bytes(),
                0o755,
            )?;
        }
        Ok(())
    }

    /// Installs a portable package's bundled libraries, or none, and removes
    /// what an earlier installation's runtime left that this one does not
    /// have: a regular package must not find qt.conf pointing at another Qt.
    /// A file changed since it was installed is kept.
    pub fn runtime(&mut self, source: Option<&Path>) -> Result<()> {
        let app = Path::new(INSTALLED_LIBRARY);
        let runtime = app.join("runtime");
        let qt_conf = app.join("qt.conf");
        let mut keep = HashSet::new();
        if let Some(source) = source {
            for path in files_below(source)? {
                let relative = runtime.join(path.strip_prefix(source).unwrap_or(&path));
                let data = fs::read(&path).map_err(io_context(&path))?;
                let mode = if executable(&path) { 0o755 } else { 0o644 };
                self.write(&relative, &data, mode)?;
                keep.insert(relative);
            }
            self.write_text(&qt_conf, QT_CONF)?;
            keep.insert(qt_conf.clone());
        }
        self.flush()?;
        for (recorded, entries) in self.history() {
            let relative = PathBuf::from(&recorded);
            if keep.contains(&relative) {
                continue;
            }
            if relative != qt_conf && !relative.starts_with(&runtime) {
                continue;
            }
            if as_installed(&entries, &self.user.join(&relative)) {
                self.remove(&relative)?;
            }
        }
        self.prune(&runtime)
    }

    /// Publishes what is staged, then removes the empty directories below
    /// `directory` and it, and the staged files an interrupted run left there.
    pub fn prune(&mut self, directory: &Path) -> Result<()> {
        self.flush()?;
        let root = self.user.join(directory);
        if !is_dir(&root) || is_symlink(&root) {
            return Ok(());
        }
        let mut directories = vec![root.clone()];
        let mut index = 0;
        while index < directories.len() {
            let current = directories[index].clone();
            index += 1;
            for entry in fs::read_dir(&current).map_err(io_context(&current))? {
                let path = entry?.path();
                if is_dir(&path) && !is_symlink(&path) {
                    directories.push(path);
                }
            }
        }
        directories.sort_by_key(|path| std::cmp::Reverse(path.components().count()));
        for path in directories {
            delete_staged(&path)?;
            if fs::read_dir(&path)
                .map_err(io_context(&path))?
                .next()
                .is_none()
            {
                fs::remove_dir(&path).map_err(io_context(&path))?;
            }
        }
        Ok(())
    }

    /// Installs the agent skill from `source` (the package's
    /// share/tomacco/agent-skill/tomacco) beside the program and, with `link`,
    /// links it into ~/.claude/skills, ~/.codex/skills and ~/.agents/skills
    /// where that agent's configuration directory exists. An entry already
    /// there that is not Tomacco's own link is left alone. An update adds no
    /// link the person removed after an earlier installation made it. Without
    /// `link`, links to the installed skill are removed.
    pub fn agent_skill(&mut self, source: &Path, link: bool, update: bool) -> Result {
        let skill = Path::new(SKILL);
        let mut keep = HashSet::new();
        for path in files_below(source)? {
            let relative = skill.join(path.strip_prefix(source).unwrap_or(&path));
            let data = fs::read(&path).map_err(io_context(&path))?;
            self.write(&relative, &data, 0o644)?;
            keep.insert(relative);
        }
        self.flush()?;
        let history = self.history();
        for (recorded, entries) in &history {
            let relative = PathBuf::from(recorded);
            if relative.starts_with(skill)
                && !keep.contains(&relative)
                && as_installed(entries, &self.user.join(&relative))
            {
                self.remove(&relative)?;
            }
        }
        self.prune(skill)?;
        if !link {
            self.remove_skill_links(&history)?;
            return Ok(json!({"linked": [], "skipped": []}));
        }
        let target = self.user.join(SKILL).to_string_lossy().into_owned();
        let (mut linked, mut skipped) = (Vec::new(), Vec::new());
        for relative in AGENT_SKILL_LINKS {
            let path = self.user.join(relative);
            let agent = path.parent().and_then(Path::parent).unwrap_or(&self.user);
            if !is_dir(agent) {
                continue;
            }
            let shown = path.to_string_lossy().into_owned();
            if self.destination_of(Path::new(relative), true).is_err() {
                skipped.push(shown);
                continue;
            }
            let entries = history.get(*relative).map(Vec::as_slice).unwrap_or(&[]);
            if is_symlink(&path) {
                let current = fs::read_link(&path).map_err(io_context(&path))?;
                if self.ours(&path) {
                    linked.push(shown);
                    continue;
                }
                let theirs = entries.last().is_none_or(|entry| {
                    entry["after"] != json!({"symlink": current.to_string_lossy()})
                });
                if theirs {
                    skipped.push(shown);
                    continue;
                }
                // Tomacco's own link from an installation in another place.
                self.unlink(relative)?;
            } else if path.exists() {
                skipped.push(shown);
                continue;
            } else if update && !entries.is_empty() {
                continue;
            }
            self.link(relative, &target)?;
            linked.push(shown);
        }
        Ok(json!({"linked": linked, "skipped": skipped}))
    }

    /// Whether the symbolic link at `path` points at the installed skill.
    fn ours(&self, path: &Path) -> bool {
        let target = self.user.join(SKILL);
        is_symlink(path)
            && (fs::read_link(path).is_ok_and(|current| current.as_os_str() == target.as_os_str())
                || (is_dir(&target) && resolve(path) == resolve(&target)))
    }

    /// Removes the agent links to the installed skill, whoever made them, and
    /// the skills directories an installation created for them once empty.
    fn remove_skill_links(&mut self, history: &BTreeMap<String, Vec<Value>>) -> Result<()> {
        let mut created = Vec::new();
        for relative in AGENT_SKILL_LINKS {
            let path = self.user.join(relative);
            if self.destination_of(Path::new(relative), true).is_err() {
                continue;
            }
            if self.ours(&path) {
                self.unlink(relative)?;
                for entry in history.get(*relative).into_iter().flatten() {
                    created.extend(strings(&entry["created"]));
                }
            }
        }
        self.remove_created(&created)
    }

    /// Whether the bar names the widget.
    pub fn widget_placed(&self) -> bool {
        fs::read(self.user.join(SHELL_CONFIG))
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
            .and_then(|value| value["bar"]["layout"].as_object().map(places_widget))
            .unwrap_or(false)
    }

    /// Installs the Omarchy bar widget from `source` (the package's
    /// share/tomacco/omarchy) and places it on the bar.
    pub fn omarchy(&mut self, source: &Path, enable: bool) -> Result<()> {
        let config = self.user.join(SHELL_CONFIG);
        if !is_file(&config) {
            return Err(refuse(
                "Omarchy shell.json was not found. Install without --omarchy on this desktop.",
            ));
        }
        let mut value: Value =
            serde_json::from_slice(&fs::read(&config).map_err(io_context(&config))?)?;
        if !value.is_object() {
            return Err(refuse(format!("{} is not a JSON object", config.display())));
        }
        if !value["bar"]["layout"].is_object() {
            let default = Path::new("/usr/share/omarchy/config/omarchy/shell.json");
            if !is_file(default) {
                return Err(refuse("No Omarchy bar layout found; configure a bar first."));
            }
            let found: Value =
                serde_json::from_slice(&fs::read(default).map_err(io_context(default))?)?;
            let layout = found["bar"]["layout"].clone();
            let object = value.as_object_mut().expect("checked above");
            let bar = object.entry("bar").or_insert_with(|| json!({}));
            if !bar.is_object() {
                *bar = json!({});
            }
            bar["layout"] = layout;
        }
        let layout = value["bar"]["layout"]
            .as_object_mut()
            .ok_or_else(|| refuse("No Omarchy bar layout found; configure a bar first."))?;
        if !places_widget(layout) {
            let right = layout.entry("right").or_insert_with(|| json!([]));
            let right = right
                .as_array_mut()
                .ok_or_else(|| refuse("The Omarchy bar's right section is not a list"))?;
            // Beside the bar's agents widget (omarchy.agents on the default
            // bar, whoever provides it here), else first in the section.
            let index = right
                .iter()
                .position(|item| {
                    layout_id(item).is_some_and(|id| id.rsplit('.').next() == Some("agents"))
                })
                .map_or(0, |index| index + 1);
            right.insert(index, json!({"id": APP_ID}));
        }
        // An update nobody asked to enable anything for: a disabled widget stays disabled.
        if enable {
            if let Some(disabled) = value
                .get_mut("disabledPlugins")
                .and_then(Value::as_array_mut)
            {
                disabled.retain(|item| item.as_str() != Some(APP_ID));
            }
        }
        // The shell compiles a plugin file once per URL and keeps that result
        // until it restarts. Each build therefore gets its own directory; the
        // shell's change watcher then loads an upgrade within a second. (Qt
        // rejects other file names for the component, so the name stays.)
        let plugin = Path::new(PLUGIN);
        let panel_path = source.join("MobilePanel.qml");
        let panel = fs::read(&panel_path).map_err(io_context(&panel_path))?;
        let build = hex_digest(&panel)[..12].to_string();
        let entry = format!("{build}/MobilePanel.qml");
        let manifest_path = source.join("manifest.json");
        let mut manifest: Value = serde_json::from_slice(
            &fs::read(&manifest_path).map_err(io_context(&manifest_path))?,
        )?;
        manifest["entryPoints"]["barWidget"] = json!(entry);
        let built = String::from_utf8_lossy(&panel).replace("@TOMACCO_PANEL_BUILD@", &build);
        // Bytes as they are where the panel is not UTF-8 text.
        let built = if std::str::from_utf8(&panel).is_ok() {
            built.into_bytes()
        } else {
            replace_bytes(&panel, b"@TOMACCO_PANEL_BUILD@", build.as_bytes())
        };
        self.write(plugin.join(&entry), &built, 0o644)?;
        let mut extras: Vec<PathBuf> = fs::read_dir(source)
            .map_err(io_context(source))?
            .filter_map(|entry| entry.ok().map(|entry| entry.path()))
            .filter(|path| is_file(path))
            .filter(|path| {
                let name = path.file_name().unwrap_or_default();
                name != "MobilePanel.qml" && name != "manifest.json"
            })
            .collect();
        extras.sort();
        for extra in extras {
            let data = fs::read(&extra).map_err(io_context(&extra))?;
            self.write(plugin.join(extra.file_name().unwrap_or_default()), &data, 0o644)?;
        }
        // The manifest switches the entry point only after its file exists.
        self.write_text(plugin.join("manifest.json"), &config_json(&manifest))?;
        // Remove earlier copies of the widget, and nothing else in this
        // directory: the file an older installer put at the top level, and its
        // build directories. A user's own MobilePanelLocal.qml or
        // MobilePanel2.qml is not ours to delete.
        let installed = self.user.join(plugin);
        let mut stale = vec![installed.join("MobilePanel.qml")];
        if let Ok(entries) = fs::read_dir(&installed) {
            for entry in entries.flatten() {
                let name = entry.file_name().to_string_lossy().into_owned();
                if name.len() == 12
                    && name
                        .bytes()
                        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
                    && name != build
                {
                    let candidate = entry.path().join("MobilePanel.qml");
                    if candidate.exists() || is_symlink(&candidate) {
                        stale.push(candidate);
                    }
                }
            }
        }
        stale.sort();
        for path in stale {
            let parent = path.parent().unwrap_or(&installed).to_path_buf();
            if is_file(&path) && !is_symlink(&path) && !is_symlink(&parent) {
                let relative = path
                    .strip_prefix(&self.user)
                    .unwrap_or(&path)
                    .to_path_buf();
                self.remove(&relative)?;
                self.flush()?;
                if parent != installed
                    && fs::read_dir(&parent)
                        .map_err(io_context(&parent))?
                        .next()
                        .is_none()
                {
                    fs::remove_dir(&parent).map_err(io_context(&parent))?;
                }
            }
        }
        self.write_text(SHELL_CONFIG, &config_json(&value))
    }

    /// Every file the installations recorded, with its journal entries, oldest first.
    pub fn history(&self) -> BTreeMap<String, Vec<Value>> {
        let mut history: BTreeMap<String, Vec<Value>> = BTreeMap::new();
        for manifest in manifests(&self.user) {
            for entry in manifest["files"].as_array().into_iter().flatten() {
                if let Some(path) = entry["path"].as_str() {
                    history
                        .entry(path.to_string())
                        .or_default()
                        .push(entry.clone());
                }
            }
        }
        history
    }

    /// Removes what the installations wrote, as recorded in their journals.
    ///
    /// Files Tomacco owns are removed only while they are exactly as an
    /// installation left them; a file the user changed since is kept and
    /// reported. The edits to the user's own Hyprland and Omarchy
    /// configuration are undone in place. User data under
    /// ~/.local/share/tomacco (projects, device profiles, settings and these
    /// backups) is never touched, and the uninstallation is itself journalled,
    /// so `--rollback` with its ID reinstalls everything it removed.
    pub fn uninstall(&mut self) -> Result {
        let history = self.history();
        let mut kept = Vec::new();
        // The user's own configuration first, so it never names a removed file.
        self.unedit_window_rule()?;
        self.unedit_omarchy()?;
        self.remove_skill_links(&history)?;
        self.batch(|this| {
            for (recorded, entries) in &history {
                let relative = PathBuf::from(recorded);
                if relative.starts_with(USER_DATA)
                    || !OWNED.iter().any(|owned| relative.starts_with(owned))
                {
                    continue;
                }
                let path = this.user.join(&relative);
                if is_symlink(&path) {
                    kept.push(path.to_string_lossy().into_owned());
                    continue;
                }
                let Ok(target) = this.destination(&relative) else {
                    kept.push(path.to_string_lossy().into_owned());
                    continue;
                };
                if !is_file(&target) {
                    continue;
                }
                if !as_installed(entries, &target) {
                    kept.push(target.to_string_lossy().into_owned());
                    continue;
                }
                this.remove(&relative)?;
            }
            Ok(())
        })?;
        for directory in OWNED_DIRECTORIES {
            self.prune(Path::new(directory))?;
        }
        self.finish()?;
        Ok(json!({"removed": self.changed.len(), "kept": kept,
            "backups": (!self.records.is_empty()).then(|| self.backup.to_string_lossy().into_owned()),
            "user_data": self.user.join(USER_DATA).to_string_lossy()}))
    }

    fn unedit_window_rule(&mut self) -> Result<()> {
        let config = self.user.join(HYPR_CONFIG);
        if !is_file(&config) || is_symlink(&config) {
            return Ok(());
        }
        let text = fs::read_to_string(&config).map_err(io_context(&config))?;
        if !text.contains(HYPR_REQUIRE) {
            return Ok(());
        }
        let lines: Vec<&str> = split_lines(&text)
            .into_iter()
            .filter(|line| {
                let line = line.trim();
                line != HYPR_REQUIRE && line != HYPR_COMMENT
            })
            .collect();
        let edited = lines.join("\n").trim_end().to_string() + "\n";
        let mode = mode_of(&config).map_err(io_context(&config))?;
        self.write(HYPR_CONFIG, edited.as_bytes(), mode)
    }

    fn unedit_omarchy(&mut self) -> Result<()> {
        let config = self.user.join(SHELL_CONFIG);
        if !is_file(&config) || is_symlink(&config) {
            return Ok(());
        }
        let Ok(mut value) =
            serde_json::from_slice::<Value>(&fs::read(&config).map_err(io_context(&config))?)
        else {
            return Ok(());
        };
        let Some(layout) = value
            .get_mut("bar")
            .and_then(Value::as_object_mut)
            .and_then(|bar| bar.get_mut("layout"))
            .and_then(Value::as_object_mut)
        else {
            return Ok(());
        };
        if !places_widget(layout) {
            return Ok(());
        }
        for items in layout.values_mut() {
            if let Some(items) = items.as_array_mut() {
                items.retain(|item| layout_id(item) != Some(APP_ID));
            }
        }
        let mode = mode_of(&config).map_err(io_context(&config))?;
        self.write(SHELL_CONFIG, config_json(&value).as_bytes(), mode)
    }

    /// Installs the rules and requires them, once: a rollback checks each
    /// journal entry against the file, so hyprland.lua gets one write a run.
    pub fn window_rule(&mut self) -> Result<()> {
        let config = self.user.join(HYPR_CONFIG);
        if !is_file(&config) {
            return Ok(());
        }
        self.write_text(HYPR_RULES_FILE, HYPR_RULES)?;
        let text = fs::read_to_string(&config).map_err(io_context(&config))?;
        if !text.contains(HYPR_REQUIRE) {
            let edited = format!(
                "{}\n\n{HYPR_COMMENT}\n{HYPR_REQUIRE}\n",
                text.trim_end()
            );
            let mode = mode_of(&config).map_err(io_context(&config))?;
            self.write(HYPR_CONFIG, edited.as_bytes(), mode)?;
        }
        Ok(())
    }
}

/// What a rollback does to one journalled path.
enum Change {
    /// A symbolic link, with its journal entry.
    Link(Value),
    /// The file comes back from this backup copy.
    Restore(PathBuf),
    /// The file was new; it goes.
    Remove,
}

/// What a journal entry records for `target`: a symlink's target, a file's
/// digest and mode, null when nothing is there; None for anything else.
fn current_state(target: &Path) -> Result<Option<Value>> {
    if is_symlink(target) {
        let link = fs::read_link(target).map_err(io_context(target))?;
        return Ok(Some(json!({"symlink": link.to_string_lossy()})));
    }
    if is_file(target) {
        return Ok(Some(state(
            file_digest(target).map_err(io_context(target))?,
            mode_of(target).map_err(io_context(target))?,
        )));
    }
    Ok(if target.exists() { None } else { Some(Value::Null) })
}

fn replace_bytes(data: &[u8], from: &[u8], to: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(data.len());
    let mut index = 0;
    while index < data.len() {
        if data[index..].starts_with(from) {
            out.extend_from_slice(to);
            index += from.len();
        } else {
            out.push(data[index]);
            index += 1;
        }
    }
    out
}

/// Lines as Python's `str.splitlines` finds them in a text file: split at
/// `\n`, `\r\n` and `\r`, without the line ends.
fn split_lines(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let bytes = text.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'\n' => {
                lines.push(&text[start..index]);
                start = index + 1;
            }
            b'\r' => {
                lines.push(&text[start..index]);
                if bytes.get(index + 1) == Some(&b'\n') {
                    index += 1;
                }
                start = index + 1;
            }
            _ => (),
        }
        index += 1;
    }
    if start < bytes.len() {
        lines.push(&text[start..]);
    }
    lines
}

fn delete_staged(directory: &Path) -> Result<()> {
    let Ok(entries) = fs::read_dir(directory) else {
        return Ok(());
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if entry.file_name().as_bytes().starts_with(STAGED.as_bytes())
            && is_file(&path)
            && !is_symlink(&path)
        {
            fs::remove_file(&path).map_err(io_context(&path))?;
        }
    }
    Ok(())
}

/// Every file below `root` (following links to files, not into linked
/// directories), sorted.
fn files_below(root: &Path) -> Result<Vec<PathBuf>> {
    let mut files = Vec::new();
    let mut directories = vec![root.to_path_buf()];
    while let Some(directory) = directories.pop() {
        for entry in fs::read_dir(&directory).map_err(io_context(&directory))? {
            let path = entry?.path();
            if is_symlink(&path) {
                if is_file(&path) {
                    files.push(path);
                }
            } else if is_dir(&path) {
                directories.push(path);
            } else if is_file(&path) {
                files.push(path);
            }
        }
    }
    files.sort();
    Ok(files)
}

/// Whether `target` is exactly as an installation left it, by its journal
/// entries. The newest entry may never have been published, when its run was
/// interrupted: then the file is as the entry found it.
pub(crate) fn as_installed(entries: &[Value], target: &Path) -> bool {
    if !is_file(target) || is_symlink(target) {
        return false;
    }
    let Ok(digest) = file_digest(target) else {
        return false;
    };
    let Some(newest) = entries.last() else {
        return false;
    };
    let sha = |value: &Value| value["sha256"].as_str().map(str::to_string);
    if !newest["after"].is_null() {
        if sha(&newest["after"]).as_deref() == Some(&digest) {
            return true;
        }
        // An interrupted update: the file is what the installation before wrote.
        return sha(&newest["before"]).as_deref() == Some(&digest)
            && entries.len() > 1
            && sha(&entries[entries.len() - 2]["after"]).as_deref() == Some(&digest);
    }
    // A removal is journalled only for a file as installed; an interrupted one
    // left that file in place.
    sha(&newest["before"]).as_deref() == Some(&digest)
}

/// Every journalled installation, rollback and uninstallation, oldest first.
pub(crate) fn manifests(user: &Path) -> Vec<Value> {
    let mut found = Vec::new();
    for (_, path) in manifest_paths(user) {
        let Ok(bytes) = fs::read(&path) else { continue };
        let Ok(value) = serde_json::from_slice::<Value>(&bytes) else {
            continue;
        };
        if value["format"].as_u64() == Some(1) && value["files"].is_array() {
            found.push(value);
        }
    }
    found
}

fn manifest_paths(user: &Path) -> Vec<(String, PathBuf)> {
    let mut paths = Vec::new();
    if let Ok(entries) = fs::read_dir(user.join(INSTALL_BACKUPS)) {
        for entry in entries.flatten() {
            let path = entry.path().join("manifest.json");
            if path.exists() {
                paths.push((entry.file_name().to_string_lossy().into_owned(), path));
            }
        }
    }
    paths.sort();
    paths
}

fn launcher_script(name: &str, target: &Path, portable: bool) -> String {
    let mut environment = String::new();
    if name == DESKTOP_NAME {
        environment.push_str("export QT_QPA_PLATFORMTHEME=generic\nexport QT_QUICK_CONTROLS_STYLE=Basic\n");
        if portable {
            // The bundled Qt loads only its own plugins and QML modules.
            environment.push_str("unset QT_PLUGIN_PATH QML_IMPORT_PATH QML2_IMPORT_PATH\n");
        }
    }
    format!(
        "#!/bin/sh\n{INSTALLER_MARKER}\n{environment}exec {} \"$@\"\n",
        shell_quote(&target.join(name).to_string_lossy())
    )
}

/// The menu entry from the package, its commands pointing at the installed launcher.
fn desktop_entry(template: &str, executable: &str) -> String {
    let quoted = format!(
        "\"{}\"",
        executable
            .replace('\\', "\\\\")
            .replace('"', "\\\"")
            .replace('`', "\\`")
            .replace('$', "\\$")
            .replace('%', "%%")
    );
    let mut lines = Vec::new();
    for line in split_lines(template) {
        match line.strip_prefix("Exec=tomacco") {
            Some(rest) => lines.push(format!("Exec={quoted}{rest}")),
            None => lines.push(line.to_string()),
        }
    }
    lines.join("\n") + "\n"
}

// ---------------------------------------------------------------------------
// The release package
// ---------------------------------------------------------------------------

/// An unpacked release package: `bin/`, `lib/` and `share/` as CMake installed
/// them, with `share/tomacco/VERSION`.
pub struct Package {
    pub root: PathBuf,
    pub version: String,
}

impl Package {
    pub fn open(root: &Path) -> Option<Package> {
        let version = fs::read_to_string(root.join("share").join(PACKAGE_PREFIX).join("VERSION")).ok()?;
        Some(Package {
            root: root.to_path_buf(),
            version: version.trim().to_string(),
        })
    }

    fn share(&self, relative: &str) -> PathBuf {
        self.root.join("share").join(PACKAGE_PREFIX).join(relative)
    }

    /// Name -> content of the engine recipe files: sources, patches, licenses
    /// and the README; the developer notes stay in the source tree.
    fn integration_files(&self) -> Result<Vec<(String, PathBuf)>> {
        let directory = self.share("integrations/ios-emulator");
        Ok(sorted_files(&directory)?
            .into_iter()
            .filter(|(name, _)| name == "README.md" || !name.ends_with(".md"))
            .collect())
    }
}

/// The regular files directly in `directory` (following links), by name.
fn sorted_files(directory: &Path) -> Result<Vec<(String, PathBuf)>> {
    let mut files = Vec::new();
    for entry in fs::read_dir(directory).map_err(io_context(directory))? {
        let entry = entry?;
        let path = entry.path();
        if is_file(&path) {
            files.push((entry.file_name().to_string_lossy().into_owned(), path));
        }
    }
    files.sort();
    Ok(files)
}

pub struct Options {
    pub binary: PathBuf,
    pub cli: Option<PathBuf>,
    pub omarchy: bool,
    pub no_window_rule: bool,
    pub no_agent_skill: bool,
    pub update: bool,
}

/// Installs `package` for the user of `installer`, as `install.py` without
/// `--rollback`, `--uninstall` or `--list-backups`.
pub fn install_package(installer: &mut Installer, package: &Package, options: &Options) -> Result {
    let binary = &options.binary;
    if !is_file(binary) {
        return Err(usage(format!(
            "No desktop binary at {}. Build with CMake first or pass --binary.",
            binary.display()
        )));
    }
    let cli = match &options.cli {
        Some(cli) => cli.clone(),
        None => resolve(binary)
            .parent()
            .unwrap_or(Path::new("/"))
            .join(CLI_NAME),
    };
    let elf = is_file(&cli)
        && File::open(&cli)
            .and_then(|mut file| {
                let mut magic = [0u8; 4];
                file.read_exact(&mut magic).map(|()| magic)
            })
            .is_ok_and(|magic| &magic == b"\x7fELF");
    if !elf {
        return Err(usage(
            "Build the Rust control plane first, or pass --cli pointing to its native ELF binary.",
        ));
    }
    let root = &package.root;
    let runtime = Some(root.join(RUNTIME)).filter(|path| is_dir(path));
    let app = Path::new(INSTALLED_LIBRARY);
    let launcher = installer.user.join(INSTALLED_LAUNCHERS).join(DESKTOP_NAME);
    // The program's own files in batches; the integrations below edit the
    // user's configuration one file at a time, in order.
    installer.batch(|installer| {
        installer.launchers(runtime.is_some())?;
        let read = |path: &Path| fs::read(path).map_err(io_context(path));
        installer.write(app.join(DESKTOP_NAME), &read(binary)?, 0o755)?;
        #[allow(unused_mut)]
        let mut cli_bytes = read(&cli)?;
        #[cfg(debug_assertions)]
        if package.share("TEST-BROKEN-CLI").exists() {
            // The update test's package whose program does not start once installed.
            cli_bytes = b"#!/bin/sh\nexit 3\n".to_vec();
        }
        installer.write(app.join(CLI_NAME), &cli_bytes, 0o755)?;
        installer.runtime(runtime.as_deref())?;
        for (name, path) in sorted_files(&package.share("tools"))? {
            if name.ends_with(".py") {
                installer.write(app.join("tools").join(&name), &read(&path)?, 0o755)?;
            }
        }
        for (name, path) in sorted_files(&package.share("examples/ios-restore-native"))? {
            installer.write(
                app.join("examples/ios-restore-native").join(&name),
                &read(&path)?,
                0o644,
            )?;
        }
        for (name, path) in package.integration_files()? {
            installer.write(
                app.join("integrations/ios-emulator").join(&name),
                &read(&path)?,
                0o644,
            )?;
        }
        for (name, path) in sorted_files(&root.join("lib/tomacco/assets"))? {
            if name.ends_with(".json") {
                installer.write(app.join("assets").join(&name), &read(&path)?, 0o644)?;
            }
        }
        installer.write_text(app.join("VERSION"), &format!("{}\n", package.version))?;
        // The Python installer, kept for older updaters and for anyone who
        // runs ~/.local/lib/tomacco/install.py; it hands over to the
        // tomaccoctl beside it.
        let script = root.join("install.py");
        if is_file(&script) {
            installer.write(app.join("install.py"), &read(&script)?, 0o755)?;
        }
        // The guides and legal documents; the About dialog opens LICENSE,
        // THIRD_PARTY_NOTICES.md and legal.md from here.
        for (name, path) in sorted_files(&root.join("share/doc/tomacco"))? {
            installer.write(Path::new(DOCUMENTS).join(&name), &read(&path)?, 0o644)?;
        }
        installer.write(MANUAL_PAGE, &read(&root.join("share/man/man1/tomaccoctl.1"))?, 0o644)?;
        installer.write(
            ICON,
            &read(&root.join(format!("share/icons/hicolor/scalable/apps/{APP_ID}.svg")))?,
            0o644,
        )?;
        let template = read(&root.join(format!("share/applications/{APP_ID}.desktop")))?;
        let entry = desktop_entry(
            &String::from_utf8_lossy(&template),
            &launcher.to_string_lossy(),
        );
        installer.write_text(DESKTOP_ENTRY, &entry)?;
        // The software-center listing; it names the menu entry above.
        installer.write(
            METAINFO,
            &read(&root.join(format!("share/metainfo/{APP_ID}.metainfo.xml")))?,
            0o644,
        )?;
        Ok(())
    })?;
    let skill = installer.agent_skill(
        &package.share("agent-skill/tomacco"),
        !options.no_agent_skill,
        options.update,
    )?;
    let omarchy = package.share("omarchy");
    if options.omarchy {
        installer.omarchy(&omarchy, true)?;
    } else if options.update && installer.widget_placed() {
        installer.omarchy(&omarchy, false)?;
    }
    if !options.no_window_rule
        && (!options.update || installer.user.join(HYPR_RULES_FILE).exists())
    {
        installer.window_rule()?;
    }
    installer.finish()?;
    refresh_menu(&installer.user, true)?;
    Ok(json!({
        "installed": launcher.to_string_lossy(),
        "cli": installer.user.join(INSTALLED_LAUNCHERS).join(CLI_NAME).to_string_lossy(),
        "changed": installer.changed.len(),
        "backups": installer.backup.exists().then(|| installer.backup.to_string_lossy().into_owned()),
        "omarchy": options.omarchy,
        "agent_skill": skill,
    }))
}

/// The desktop's menu cache, refreshed when the installation is for this
/// user's own home.
fn refresh_menu(user: &Path, check: bool) -> Result<()> {
    if user != resolve(&user_home()) {
        return Ok(());
    }
    let Some(tool) = crate::toolchain::find_optional("update-desktop-database")? else {
        return Ok(());
    };
    let status = std::process::Command::new(tool)
        .arg(user.join(".local/share/applications"))
        .stdin(std::process::Stdio::null())
        .status();
    if check && !status.is_ok_and(|status| status.success()) {
        return Err(Error::new(
            "install_refused",
            "update-desktop-database failed",
        ));
    }
    Ok(())
}

/// Uninstalls from `user`'s home, as `--uninstall`.
pub fn uninstall(user: &Path, version: &str) -> Result {
    let mut installer = Installer::new(user, version);
    let result = installer.uninstall()?;
    refresh_menu(&installer.user, false)?;
    Ok(result)
}

/// Rolls `user`'s home back to before the run that made backup `identifier`.
pub fn rollback(user: &Path, version: &str, identifier: &str) -> Result {
    Installer::new(user, version).rollback(identifier)
}

/// The journalled runs, as `--list-backups`.
pub fn list_backups(user: &Path) -> Value {
    let mut entries = Vec::new();
    for (id, path) in manifest_paths(user) {
        let Some(value) = fs::read(&path)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        else {
            continue;
        };
        entries.push(json!({"id": id, "version": value.get("version").cloned().unwrap_or(Value::Null),
            "files": value["files"].as_array().map_or(0, Vec::len)}));
    }
    Value::Array(entries)
}

// ---------------------------------------------------------------------------
// tomaccoctl _install
// ---------------------------------------------------------------------------

/// A mistake on the command line: exit status 2, as argparse's.
fn usage(message: impl Into<String>) -> Error {
    Error::new("invalid_request", message)
}

const USAGE: &str = "usage: tomaccoctl _install [-h] [--binary BINARY] [--cli CLI] [--user-home USER_HOME] [--omarchy]\n                \
     [--no-window-rule] [--no-agent-skill] [--update] [--list-backups] [--rollback BACKUP_ID] [--uninstall] [--version]\n                \
     [--package DIRECTORY]";

const HELP: &str = "\
Install the desktop, control plane and optional Omarchy panel for one user.

options:
  -h, --help            show this help message and exit
  --binary BINARY       The desktop binary (default: the package's bin/tomacco)
  --cli CLI             Native tomaccoctl binary; defaults to the desktop binary's directory
  --user-home USER_HOME
                        Alternate home for packaging/installation tests
  --omarchy             Install and enable the Omarchy bar/popup plugin
  --no-window-rule
  --no-agent-skill      Do not link the agent skill into ~/.claude/skills, ~/.codex/skills and ~/.agents/skills,
                        and remove such links an earlier installation made
  --update              Keep the integrations as they are: refresh the Omarchy panel and window rules only where
                        they are already in use
  --list-backups        List installations with a verified rollback manifest
  --rollback BACKUP_ID  Restore an installation, preserving files subsequently edited by the user
  --uninstall           Remove the installed files and integrations; keeps user data, device profiles, backups and
                        files you changed
  --version             show program's version number and exit
  --package DIRECTORY   The unpacked release package to install (default: the one this tomaccoctl is in)";

#[derive(Default)]
struct Arguments {
    binary: Option<PathBuf>,
    cli: Option<PathBuf>,
    user_home: Option<PathBuf>,
    omarchy: bool,
    no_window_rule: bool,
    no_agent_skill: bool,
    update: bool,
    list_backups: bool,
    rollback: Option<String>,
    uninstall: bool,
    version: bool,
    help: bool,
    package_docs: bool,
    package: Option<PathBuf>,
}

const LONG: &[(&str, bool)] = &[
    ("--binary", true),
    ("--cli", true),
    ("--user-home", true),
    ("--omarchy", false),
    ("--no-window-rule", false),
    ("--no-agent-skill", false),
    ("--update", false),
    ("--list-backups", false),
    ("--rollback", true),
    ("--uninstall", false),
    ("--version", false),
    ("--help", false),
    ("--package-docs", true),
    ("--package", true),
];

/// The flags of install.py, as argparse reads them: `--flag value` or
/// `--flag=value`, and any unambiguous prefix of a flag.
fn parse(args: &[String]) -> Result<Arguments> {
    let mut parsed = Arguments::default();
    let mut index = 0;
    while index < args.len() {
        let argument = &args[index];
        index += 1;
        if argument == "-h" {
            parsed.help = true;
            continue;
        }
        let (name, inline) = match argument.split_once('=') {
            Some((name, value)) if name.starts_with("--") => (name, Some(value.to_string())),
            _ => (argument.as_str(), None),
        };
        if !name.starts_with("--") {
            return Err(usage(format!("unrecognized arguments: {argument}")));
        }
        let (flag, takes) = match LONG.iter().find(|(long, _)| *long == name) {
            Some(found) => *found,
            None => {
                let matches: Vec<_> = LONG.iter().filter(|(long, _)| long.starts_with(name)).collect();
                match matches.as_slice() {
                    [found] => **found,
                    [] => return Err(usage(format!("unrecognized arguments: {argument}"))),
                    _ => {
                        let names: Vec<_> = matches.iter().map(|(long, _)| *long).collect();
                        return Err(usage(format!(
                            "ambiguous option: {name} could match {}",
                            names.join(", ")
                        )));
                    }
                }
            }
        };
        let value = if takes {
            match inline {
                Some(value) => Some(value),
                None => {
                    let value = args
                        .get(index)
                        .filter(|value| !value.starts_with("--"))
                        .ok_or_else(|| usage(format!("argument {flag}: expected one argument")))?;
                    index += 1;
                    Some(value.clone())
                }
            }
        } else if inline.is_some() {
            return Err(usage(format!("argument {flag}: ignored explicit argument")));
        } else {
            None
        };
        let path = || PathBuf::from(value.clone().unwrap_or_default());
        match flag {
            "--binary" => parsed.binary = Some(path()),
            "--cli" => parsed.cli = Some(path()),
            "--user-home" => parsed.user_home = Some(path()),
            "--omarchy" => parsed.omarchy = true,
            "--no-window-rule" => parsed.no_window_rule = true,
            "--no-agent-skill" => parsed.no_agent_skill = true,
            "--update" => parsed.update = true,
            "--list-backups" => parsed.list_backups = true,
            "--rollback" => parsed.rollback = value,
            "--uninstall" => parsed.uninstall = true,
            "--version" => parsed.version = true,
            "--help" => parsed.help = true,
            "--package-docs" => parsed.package_docs = true,
            "--package" => parsed.package = Some(path()),
            _ => unreachable!("every flag is listed"),
        }
    }
    Ok(parsed)
}

/// Where this tomaccoctl stands: in an unpacked package (`<root>/bin/tomaccoctl`),
/// or in an installation (`~/.local/lib/tomacco/tomaccoctl` beside `VERSION`).
enum Origin {
    Package(Package),
    Installed(String),
    Elsewhere,
}

fn origin(package: Option<&Path>) -> Result<Origin> {
    if let Some(root) = package {
        let root = resolve(root);
        return Package::open(&root).map(Origin::Package).ok_or_else(|| {
            usage(format!(
                "{} is no release package: it has no share/{PACKAGE_PREFIX}/VERSION.",
                root.display()
            ))
        });
    }
    let executable = own_executable()?.canonicalize()?;
    let directory = executable.parent().unwrap_or(Path::new("/"));
    if directory.file_name().is_some_and(|name| name == "bin") {
        if let Some(found) = directory.parent().and_then(Package::open) {
            return Ok(Origin::Package(found));
        }
    }
    if let Ok(version) = fs::read_to_string(directory.join("VERSION")) {
        return Ok(Origin::Installed(version.trim().to_string()));
    }
    Ok(Origin::Elsewhere)
}

fn print(value: &Value) {
    let _ = writeln!(io::stdout(), "{}", ascii_json(value));
}

/// `tomaccoctl _install`: install.py's command line. Prints its JSON result on
/// stdout; a refusal goes to stderr with exit status 1, a mistake on the
/// command line with 2.
pub fn command(args: &[String]) -> Result<i32> {
    let parsed = match parse(args) {
        Ok(parsed) => parsed,
        Err(error) => {
            eprintln!("{USAGE}\ntomaccoctl _install: error: {}", error.message);
            return Ok(2);
        }
    };
    if parsed.help {
        println!("{USAGE}\n\n{HELP}");
        return Ok(0);
    }
    match run(parsed) {
        Ok(()) => Ok(0),
        Err(error) if error.code == "invalid_request" => {
            eprintln!("{USAGE}\ntomaccoctl _install: error: {}", error.message);
            Ok(2)
        }
        Err(error) => {
            eprintln!("{}", error.message);
            Ok(1)
        }
    }
}

fn run(parsed: Arguments) -> Result<()> {
    let origin = origin(parsed.package.as_deref())?;
    let version = match &origin {
        Origin::Package(package) => package.version.clone(),
        Origin::Installed(version) => version.clone(),
        Origin::Elsewhere => crate::update::current_version(),
    };
    if parsed.version {
        println!("Tomacco installer {version}");
        return Ok(());
    }
    if parsed.package_docs {
        return Err(usage(
            "--package-docs needs the source tree: run install.py from a checkout.",
        ));
    }
    let user = parsed.user_home.clone().unwrap_or_else(user_home);
    if parsed.list_backups {
        print(&list_backups(&user));
        return Ok(());
    }
    if let Some(identifier) = &parsed.rollback {
        print(&rollback(&user, &version, identifier)?);
        return Ok(());
    }
    if parsed.uninstall {
        print(&uninstall(&user, &version)?);
        return Ok(());
    }
    let package = match origin {
        Origin::Package(package) => package,
        Origin::Installed(_) => {
            return Err(usage(
                "This installed copy only uninstalls (--uninstall), rolls back (--rollback) and lists backups \
                 (--list-backups). Install from a release package.",
            ));
        }
        Origin::Elsewhere => {
            return Err(usage(
                "This tomaccoctl is not in a release package. Run the package's bin/tomaccoctl, pass --package, \
                 or install from a checkout with install.py.",
            ));
        }
    };
    let options = Options {
        binary: parsed
            .binary
            .unwrap_or_else(|| package.root.join("bin").join(DESKTOP_NAME)),
        cli: parsed.cli,
        omarchy: parsed.omarchy,
        no_window_rule: parsed.no_window_rule,
        no_agent_skill: parsed.no_agent_skill,
        update: parsed.update,
    };
    let mut installer = Installer::new(&user, &package.version);
    print(&install_package(&mut installer, &package, &options)?);
    Ok(())
}

#[cfg(test)]
#[path = "install_tests.rs"]
mod tests;
