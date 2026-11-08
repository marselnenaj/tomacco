//! Optional, process-isolated warm OCR for persistent MCP hosts.
//!
//! No foreign code runs in the MCP process. A single try-locked worker retains
//! one model; concurrent callers, custom binaries and large images use the CLI.
use crate::core::*;
use serde_json::json;
use std::{
    ffi::OsString,
    fs::{self, File},
    io::Read,
    os::{
        fd::{AsRawFd, RawFd},
        unix::{fs::MetadataExt, process::CommandExt},
    },
    path::{Path, PathBuf},
    process::{Child, ChildStdin, ChildStdout, Command, Stdio},
    sync::{
        Mutex, OnceLock,
        atomic::{AtomicBool, Ordering},
        mpsc::{self, SyncSender},
    },
    time::{Duration, Instant},
};

#[path = "screen_ocr_worker.rs"]
mod worker;
pub(super) const MAX_OUTPUT: usize = 8 * 1024 * 1024;
const MAX_REQUEST: usize = 16 * 1024;
const MAX_PIXELS: u64 = 9_000_000;
const MAX_RSS_KIB: u64 = 256 * 1024;
const MAX_READS: u32 = 128;
const REQUEST_TIME: Duration = Duration::from_secs(90);
const INIT_TIME: Duration = Duration::from_secs(10);
pub(super) const IDLE_TIME: Duration = Duration::from_secs(60);
const RETIRE_IDLE: Duration = Duration::from_secs(55);
static ENABLED: AtomicBool = AtomicBool::new(false);

pub(crate) fn enable() {
    let _ = spawn_owner();
    ENABLED.store(true, Ordering::Relaxed);
}
pub(crate) fn worker_main(language: &str) -> i32 {
    worker::run(language)
}

fn configured_override() -> bool {
    std::env::var_os("TOMACCO_TESSERACT").is_some_and(|v| !v.is_empty())
}

/// Only a standard ELF Tesseract gets an automatic thread budget. A wrapper's
/// environment and an explicitly supplied OpenMP limit remain unchanged.
fn standard_binary(binary: &Path) -> bool {
    if binary != Path::new("/usr/bin/tesseract") {
        return false;
    }
    let Ok(meta) = fs::metadata(binary) else {
        return false;
    };
    // Host root ownership can be remapped by a sandbox/container. The fixed
    // system path, non-writable mode, ELF dependency and version match identify
    // the standard engine without depending on the namespace's numeric UID.
    if !meta.is_file() || meta.mode() & 0o022 != 0 || meta.len() > 4 * 1024 * 1024 {
        return false;
    }
    let Ok(bytes) = fs::read(binary) else {
        return false;
    };
    bytes.starts_with(b"\x7fELF") && bytes.windows(18).any(|s| s == b"libtesseract.so.5\0")
}

pub(super) fn thread_overrides(binary: &Path) -> Vec<(&'static str, &'static str)> {
    thread_policy(
        std::env::var_os("OMP_THREAD_LIMIT").is_some()
            || std::env::var_os("OMP_NUM_THREADS").is_some(),
        standard_binary(binary),
    )
}
fn thread_policy(explicit_limit: bool, standard: bool) -> Vec<(&'static str, &'static str)> {
    if !explicit_limit && standard {
        vec![("OMP_THREAD_LIMIT", "1")]
    } else {
        vec![]
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct Key {
    language: String,
    data: Option<OsString>,
    threads: Option<OsString>,
    num_threads: Option<OsString>,
    binary: (u64, u64, i64, i64, u32),
    models: Vec<(PathBuf, Option<(u64, i64, i64)>)>,
}
impl Key {
    fn new(binary: &Path, language: &str) -> Option<Self> {
        let meta = fs::metadata(binary).ok()?;
        let data = std::env::var_os("TESSDATA_PREFIX");
        let dirs = data
            .as_ref()
            .map(|p| vec![PathBuf::from(p)])
            .unwrap_or_else(|| {
                vec![
                    "/usr/share/tessdata".into(),
                    "/usr/share/tesseract-ocr/5/tessdata".into(),
                    "/usr/local/share/tessdata".into(),
                ]
            });
        let models = dirs
            .iter()
            .flat_map(|dir| {
                language.split('+').map(move |lang| {
                    dir.join(format!("{}.traineddata", lang.trim_start_matches('~')))
                })
            })
            .map(|p| {
                let m = fs::metadata(&p)
                    .ok()
                    .map(|m| (m.len(), m.mtime(), m.mtime_nsec()));
                (p, m)
            })
            .collect();
        Some(Self {
            language: language.into(),
            data,
            threads: std::env::var_os("OMP_THREAD_LIMIT"),
            num_threads: std::env::var_os("OMP_NUM_THREADS"),
            binary: (
                meta.ino(),
                meta.len(),
                meta.mtime(),
                meta.mtime_nsec(),
                meta.mode(),
            ),
            models,
        })
    }
}

#[derive(Default)]
struct Pool {
    worker: Option<Worker>,
    disabled: bool,
    unsupported: Option<Key>,
}
fn pool() -> &'static Mutex<Pool> {
    static POOL: OnceLock<Mutex<Pool>> = OnceLock::new();
    POOL.get_or_init(|| Mutex::new(Pool::default()))
}
pub(crate) fn shutdown() {
    ENABLED.store(false, Ordering::Relaxed);
    if let Ok(mut state) = pool().lock() {
        state.worker.take();
    }
    // Pooled children are retired before their creator thread exits. The
    // private child also closes on EOF or parent death if the host is killed.
    if let Some(owner) = spawn_owner() {
        let _ = owner.try_send(SpawnJob::Stop);
    }
}

// Linux PDEATHSIG follows the creating *thread*, not just its process. MCP
// request threads are short-lived, so all retained workers are created by one
// long-lived session owner. The owner never calls Tesseract or foreign code.
enum SpawnJob {
    Start(Command, SyncSender<std::io::Result<Spawned>>),
    Stop,
}
struct Spawned(Option<Child>);
impl Drop for Spawned {
    fn drop(&mut self) {
        // Also runs when a cancelled receiver drops a queued spawn reply.
        if let Some(child) = &mut self.0 {
            crate::toolchain::stop(child);
        }
    }
}
fn spawn_owner() -> Option<&'static SyncSender<SpawnJob>> {
    static OWNER: OnceLock<Option<SyncSender<SpawnJob>>> = OnceLock::new();
    OWNER
        .get_or_init(|| {
            let (sender, receiver) = mpsc::sync_channel(1);
            std::thread::Builder::new()
                .name("ocr-process-owner".into())
                .spawn(move || {
                    loop {
                        let job = match receiver.recv_timeout(Duration::from_secs(1)) {
                            Ok(job) => job,
                            Err(mpsc::RecvTimeoutError::Timeout) => {
                                // Start callers hold Pool while awaiting this owner,
                                // so never block on that lock. Retire/reap an idle
                                // model without another thread or SIGCHLD handler.
                                if let Ok(mut state) = pool().try_lock() {
                                    if state.worker.as_mut().is_some_and(|w| {
                                        w.last_used.elapsed() >= RETIRE_IDLE
                                            || w.child.try_wait().ok().flatten().is_some()
                                    }) {
                                        state.worker.take();
                                    }
                                }
                                continue;
                            }
                            Err(_) => break,
                        };
                        match job {
                            SpawnJob::Start(mut command, reply) => {
                                let result = command.spawn().map(|child| Spawned(Some(child)));
                                // Failed delivery drops Spawned, which kills and reaps.
                                let _ = reply.send(result);
                            }
                            SpawnJob::Stop => break,
                        }
                    }
                })
                .ok()
                .map(|_| sender)
        })
        .as_ref()
}
fn owned_spawn(command: Command, deadline: Instant) -> Result<Child> {
    let owner = spawn_owner()
        .ok_or_else(|| Error::new("ocr_worker_unavailable", "OCR process owner unavailable"))?;
    let (reply, receiver) = mpsc::sync_channel(1);
    owner
        .try_send(SpawnJob::Start(command, reply))
        .map_err(|_| Error::new("ocr_worker_unavailable", "OCR process owner busy"))?;
    loop {
        check_cancelled()?;
        if Instant::now() >= deadline {
            return Err(Error::new(
                "tool_timeout",
                "OCR worker initialization exceeded its time limit",
            ));
        }
        match receiver.recv_timeout(
            deadline
                .saturating_duration_since(Instant::now())
                .min(Duration::from_millis(25)),
        ) {
            Ok(result) => {
                let mut spawned = result?;
                check_cancelled()?;
                return Ok(spawned.0.take().unwrap());
            }
            Err(mpsc::RecvTimeoutError::Timeout) => continue,
            Err(_) => {
                return Err(Error::new(
                    "ocr_worker_unavailable",
                    "OCR process owner exited",
                ));
            }
        }
    }
}

fn supported_image(image: &Path) -> bool {
    let mut bytes = [0u8; 24];
    File::open(image)
        .and_then(|mut f| f.read_exact(&mut bytes))
        .is_ok()
        && &bytes[..8] == b"\x89PNG\r\n\x1a\n"
        && &bytes[12..16] == b"IHDR"
        && {
            let w = u32::from_be_bytes(bytes[16..20].try_into().unwrap()) as u64;
            let h = u32::from_be_bytes(bytes[20..24].try_into().unwrap()) as u64;
            w > 0 && h > 0 && w * h <= MAX_PIXELS
        }
}

/// None selects the existing subprocess path. A busy warm engine never queues
/// unrelated OCR calls, and cancellation/timeouts never retry another90s read.
pub(super) fn recognize(binary: &Path, image: &Path, language: &str) -> Result<Option<String>> {
    if !ENABLED.load(Ordering::Relaxed)
        || configured_override()
        || !standard_binary(binary)
        || !supported_image(image)
    {
        return Ok(None);
    }
    check_cancelled()?;
    let Ok(mut state) = pool().try_lock() else {
        return Ok(None);
    };
    if state.disabled {
        return Ok(None);
    }
    let Some(key) = Key::new(binary, language) else {
        return Ok(None);
    };
    if state.unsupported.as_ref() == Some(&key) {
        return Ok(None);
    }
    if state.worker.as_mut().is_some_and(|w| {
        w.key != key
            || w.reads >= MAX_READS
            || w.rss_kib() > MAX_RSS_KIB
            || w.last_used.elapsed() >= RETIRE_IDLE
            || w.child.try_wait().ok().flatten().is_some()
    }) {
        state.worker.take();
    }
    let deadline = Instant::now() + REQUEST_TIME;
    if state.worker.is_none() {
        match Worker::start(binary, key, deadline) {
            Ok(Start::Ready(w)) => state.worker = Some(w),
            Ok(Start::Unsupported(key)) => {
                state.unsupported = Some(key);
                return Ok(None);
            }
            Ok(Start::Unavailable) => {
                state.disabled = true;
                return Ok(None);
            }
            Err(e) if e.code == "request_cancelled" || e.code == "tool_timeout" => return Err(e),
            Err(_) => {
                state.disabled = true;
                return Ok(None);
            }
        }
    }
    let result = state.worker.as_mut().unwrap().read(image, deadline);
    match result {
        Ok(Reply::Text(text)) => {
            if state
                .worker
                .as_ref()
                .is_some_and(|w| w.rss_kib() > MAX_RSS_KIB)
            {
                state.worker.take();
            }
            Ok(Some(text))
        }
        Ok(Reply::Fallback) => {
            state.unsupported = state.worker.take().map(|w| w.key.clone());
            Ok(None)
        }
        Ok(Reply::Broken) => {
            state.worker.take();
            state.disabled = true;
            Err(worker_failed())
        }
        Err(e) => {
            state.worker.take();
            if e.code == "request_cancelled"
                || e.code == "tool_timeout"
                || e.code == "response_too_large"
                || e.code == "ocr_failed"
            {
                return Err(e);
            }
            state.disabled = true;
            Err(worker_failed())
        }
    }
}

fn worker_failed() -> Error {
    Error::new("ocr_failed", "The native OCR worker failed")
        .remedy("Retry the read; this MCP server now uses the standard Tesseract subprocess")
}

enum Reply {
    Text(String),
    Fallback,
    Broken,
}
enum Start {
    Ready(Worker),
    Unsupported(Key),
    Unavailable,
}
struct Worker {
    child: Child,
    input: ChildStdin,
    output: ChildStdout,
    key: Key,
    reads: u32,
    last_used: Instant,
}
fn host_executable() -> Result<PathBuf> {
    // execve resolves self in the forked child, whose image is still this MCP
    // host. It remains valid after an installer unlinks/replaces the on-disk
    // binary, and guarantees the same private wire protocol as the parent.
    #[cfg(target_os = "linux")]
    if Path::new("/proc/self/exe").is_file() {
        return Ok("/proc/self/exe".into());
    }
    Ok(std::env::current_exe()?)
}
impl Worker {
    fn start(binary: &Path, key: Key, deadline: Instant) -> Result<Start> {
        // Confirm the CLI and dynamically loaded C API report the same version.
        let args = vec![binary.to_string_lossy().into_owned(), "--version".into()];
        let version =
            crate::toolchain::run_with_env(&args, 5, None, false, &thread_overrides(binary))?;
        if version["exit_code"] != 0 {
            return Ok(Start::Unavailable);
        }
        let expected = s(&version, "stdout")
            .lines()
            .next()
            .and_then(|s| s.strip_prefix("tesseract "))
            .unwrap_or("")
            .trim();
        if !expected.starts_with("5.") {
            return Ok(Start::Unavailable);
        }
        let mut command = Command::new(host_executable()?);
        command
            .args(["_ocr_engine", &key.language])
            .envs(crate::toolchain::environment()?)
            .envs(thread_overrides(binary))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .process_group(0);
        crate::toolchain::parent_bound(&mut command);
        let init_deadline = (Instant::now() + INIT_TIME).min(deadline);
        let mut child = owned_spawn(command, init_deadline)?;
        let input = child.stdin.take().unwrap();
        let output = child.stdout.take().unwrap();
        let mut this = Self {
            child,
            input,
            output,
            key,
            reads: 0,
            last_used: Instant::now(),
        };
        nonblocking(this.input.as_raw_fd())?;
        nonblocking(this.output.as_raw_fd())?;
        let (status, data) = this.receive(init_deadline, 4096)?;
        if status == 1 {
            return Ok(Start::Unsupported(this.key.clone()));
        }
        if status != 0 || data != expected.as_bytes() {
            return Ok(Start::Unavailable);
        }
        Ok(Start::Ready(this))
    }
    fn rss_kib(&self) -> u64 {
        fs::read_to_string(format!("/proc/{}/status", self.child.id()))
            .ok()
            .and_then(|s| {
                s.lines()
                    .find(|l| l.starts_with("VmRSS:"))
                    .and_then(|l| l.split_whitespace().nth(1))
                    .and_then(|v| v.parse().ok())
            })
            .unwrap_or(0)
    }
    fn read(&mut self, image: &Path, deadline: Instant) -> Result<Reply> {
        let data = serde_json::to_vec(
            &json!({"image":image,"timeout_ms":deadline.saturating_duration_since(Instant::now()).as_millis().min(90_000)}),
        )?;
        if data.len() > MAX_REQUEST {
            return Ok(Reply::Fallback);
        }
        let mut request = (data.len() as u32).to_be_bytes().to_vec();
        request.extend(data);
        transfer(
            self.input.as_raw_fd(),
            &mut request,
            true,
            deadline,
            Some(self.child.id()),
        )?;
        let (status, data) = self.receive(deadline, MAX_OUTPUT)?;
        self.reads += 1;
        self.last_used = Instant::now();
        check_cancelled()?;
        match status {
            0 => Ok(Reply::Text(String::from_utf8_lossy(&data).into_owned())),
            1 => Ok(Reply::Fallback),
            2 => Ok(Reply::Broken),
            3 => Err(Error::new(
                "ocr_failed",
                "Tesseract could not read the screenshot",
            )),
            4 => Err(Error::new("response_too_large", "OCR output exceeded8MiB")),
            5 => Err(Error::new(
                "tool_timeout",
                "OCR recognition exceeded its time limit",
            )),
            _ => Err(Error::new(
                "ocr_worker_protocol",
                "Invalid OCR worker reply",
            )),
        }
    }
    fn receive(&mut self, deadline: Instant, limit: usize) -> Result<(u8, Vec<u8>)> {
        let mut header = [0u8; 5];
        transfer(
            self.output.as_raw_fd(),
            &mut header,
            false,
            deadline,
            Some(self.child.id()),
        )?;
        let len = u32::from_be_bytes(header[1..].try_into().unwrap()) as usize;
        if len > limit {
            return Err(Error::new(
                "ocr_worker_protocol",
                "OCR reply exceeded its frame bound",
            ));
        }
        let mut data = vec![0; len];
        transfer(
            self.output.as_raw_fd(),
            &mut data,
            false,
            deadline,
            Some(self.child.id()),
        )?;
        Ok((header[0], data))
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        // A clean idle exit was already reaped; never signal a recycled PID.
        // This worker does not launch descendants (only library threads).
        if self.child.try_wait().ok().flatten().is_some() {
            return;
        }
        crate::toolchain::stop(&mut self.child);
    }
}

fn nonblocking(fd: RawFd) -> Result<()> {
    // SAFETY: fd belongs to this worker's owned pipe; flags do not affect other
    // processes' descriptors or the MCP host's stdin/stdout.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(())
}
fn transfer(
    fd: RawFd,
    bytes: &mut [u8],
    write: bool,
    deadline: Instant,
    pid: Option<u32>,
) -> Result<()> {
    let mut offset = 0;
    while offset < bytes.len() {
        check_cancelled()?;
        if Instant::now() >= deadline {
            return Err(Error::new(
                "tool_timeout",
                "OCR worker exceeded its time limit",
            ));
        }
        if let Some(pid) = pid {
            let rss = fs::read_to_string(format!("/proc/{pid}/status"))
                .ok()
                .and_then(|s| {
                    s.lines()
                        .find(|l| l.starts_with("VmRSS:"))
                        .and_then(|l| l.split_whitespace().nth(1))
                        .and_then(|v| v.parse::<u64>().ok())
                });
            if rss.is_some_and(|rss| rss > MAX_RSS_KIB) {
                return Err(Error::new(
                    "ocr_worker_memory",
                    "OCR worker exceeded its memory bound",
                ));
            }
        }
        // SAFETY: the borrowed slice is live and bounded for this synchronous
        // syscall; these fds are nonblocking and never shared with another read.
        let n = unsafe {
            if write {
                libc::write(fd, bytes[offset..].as_ptr().cast(), bytes.len() - offset)
            } else {
                libc::read(
                    fd,
                    bytes[offset..].as_mut_ptr().cast(),
                    bytes.len() - offset,
                )
            }
        };
        if n > 0 {
            offset += n as usize;
            continue;
        }
        if n == 0 {
            return Err(Error::new(
                "ocr_worker_closed",
                "OCR worker closed its pipe",
            ));
        }
        let error = std::io::Error::last_os_error();
        if !matches!(error.raw_os_error(), Some(libc::EAGAIN) | Some(libc::EINTR)) {
            return Err(error.into());
        }
        let mut poll = libc::pollfd {
            fd,
            events: if write { libc::POLLOUT } else { libc::POLLIN },
            revents: 0,
        };
        let timeout = deadline
            .saturating_duration_since(Instant::now())
            .as_millis()
            .min(25) as i32;
        // SAFETY: poll points at one initialized, live pollfd for this call.
        unsafe {
            libc::poll(&mut poll, 1, timeout);
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::os::unix::net::UnixStream;
    #[test]
    fn thread_budget_preserves_explicit_limits_and_wrappers() {
        assert_eq!(thread_policy(false, true), vec![("OMP_THREAD_LIMIT", "1")]);
        assert!(thread_policy(true, true).is_empty());
        assert!(thread_policy(false, false).is_empty());
    }
    #[test]
    fn pipe_reads_partial_chunks_without_losing_bytes() {
        let (reader, mut writer) = UnixStream::pair().unwrap();
        nonblocking(reader.as_raw_fd()).unwrap();
        let thread = std::thread::spawn(move || {
            writer.write_all(b"part").unwrap();
            std::thread::sleep(Duration::from_millis(3));
            writer.write_all(b"ial").unwrap();
        });
        let mut b = [0; 7];
        transfer(
            reader.as_raw_fd(),
            &mut b,
            false,
            Instant::now() + Duration::from_secs(1),
            None,
        )
        .unwrap();
        assert_eq!(&b, b"partial");
        thread.join().unwrap();
    }
    #[test]
    fn pipe_wait_is_bounded_and_cancellable() {
        let (reader, _writer) = UnixStream::pair().unwrap();
        nonblocking(reader.as_raw_fd()).unwrap();
        let mut b = [0; 1];
        let e = transfer(
            reader.as_raw_fd(),
            &mut b,
            false,
            Instant::now() + Duration::from_millis(5),
            None,
        )
        .unwrap_err();
        assert_eq!(e.code, "tool_timeout");
        let token = std::sync::Arc::new(AtomicBool::new(true));
        let e = with_cancellation(Some(token), || {
            transfer(
                reader.as_raw_fd(),
                &mut b,
                false,
                Instant::now() + Duration::from_secs(1),
                None,
            )
        })
        .unwrap_err();
        assert_eq!(e.code, "request_cancelled");
    }
    #[test]
    fn image_cap_does_not_admit_zero_or_huge_images() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("input.png");
        let header = |w: u32, h: u32| {
            let mut b = b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR".to_vec();
            b.extend(w.to_be_bytes());
            b.extend(h.to_be_bytes());
            b
        };
        fs::write(&p, header(3840, 2160)).unwrap();
        assert!(supported_image(&p));
        fs::write(&p, header(8000, 8000)).unwrap();
        assert!(!supported_image(&p));
        fs::write(&p, header(0, 200)).unwrap();
        assert!(!supported_image(&p));
    }
    #[test]
    fn abandoned_spawn_reply_kills_and_reaps_the_undelivered_child() {
        let dir = tempfile::tempdir().unwrap();
        let pidfile = dir.path().join("child.pid");
        let mut command = Command::new("/usr/bin/python3");
        command.args(["-c","import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"])
            .arg(&pidfile).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).process_group(0);
        crate::toolchain::parent_bound(&mut command);
        // Zero capacity deliberately holds the owned child at the handoff.
        let (reply, receiver) = mpsc::sync_channel(0);
        spawn_owner()
            .unwrap()
            .try_send(SpawnJob::Start(command, reply))
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(3);
        while !pidfile.exists() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(2));
        }
        let pid = fs::read_to_string(&pidfile).unwrap();
        let proc = PathBuf::from(format!("/proc/{}", pid.trim()));
        assert!(proc.exists());
        drop(receiver);
        let deadline = Instant::now() + Duration::from_secs(3);
        while proc.exists() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(2));
        }
        assert!(
            !proc.exists(),
            "undelivered child must be killed and reaped"
        );
    }
}
