//! Persistent native display sessions. Video and input never pass through the web preview.
use crate::{api, core::*, toolchain as tc};
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::fd::AsRawFd,
    os::unix::{
        fs::PermissionsExt,
        net::{UnixListener, UnixStream},
        process::CommandExt,
    },
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant},
};
pub mod android;
pub mod audio;
pub mod ios;
pub mod iphone;
pub type Shared = Arc<Mutex<Hub>>;
type Packet = Arc<Vec<u8>>;
pub struct Hub {
    pub state: Value,
    pub stopped: Arc<AtomicBool>,
    state_file: PathBuf,
    clients: Vec<mpsc::SyncSender<Packet>>,
    config: Vec<Packet>,
    gop: Vec<Packet>,
    cached_bytes: usize,
    receiving_config: bool,
    cached_key: bool,
}
impl Hub {
    pub fn update(&mut self, patch: Value) -> Result<()> {
        for (key, value) in patch.as_object().unwrap() {
            self.state[key] = value.clone();
        }
        atomic_json(&self.state_file, &self.state)
    }
    pub fn video(&mut self, bytes: Vec<u8>, config: bool, key: bool) {
        if bytes.is_empty() || bytes.len() > 16 * 1024 * 1024 {
            return;
        }
        let packet = Arc::new(bytes);
        if config {
            if !self.receiving_config {
                self.config.clear();
            }
            if self.config.len() < 16 {
                self.config.push(packet.clone());
            }
            self.gop.clear();
            self.cached_bytes = 0;
            // New idb sends SPS/PPS and the keyframe in one payload.
            self.cached_key = key;
            self.receiving_config = !key;
        } else {
            self.receiving_config = false;
            if key {
                self.gop.clear();
                self.cached_bytes = 0;
                self.cached_key = true;
            }
            if self.cached_bytes + packet.len() > 16 * 1024 * 1024 || self.gop.len() >= 500 {
                self.gop.clear();
                self.cached_bytes = 0;
                self.cached_key = false;
            }
            if self.cached_key {
                self.cached_bytes += packet.len();
                self.gop.push(packet.clone());
            }
        }
        self.state["packets"] = json!(n(&self.state, "packets") + 1);
        self.state["video_bytes"] = json!(n(&self.state, "video_bytes") + packet.len() as u64);
        self.clients
            .retain(|client| client.try_send(packet.clone()).is_ok());
    }
}
pub trait Device: Send {
    fn input(&mut self, a: &Value) -> Result;
    fn capture(&mut self, path: &Path) -> Result;
    fn hierarchy(&mut self) -> Result;
}
pub fn number(a: &Value, key: &str) -> f64 {
    a[key].as_f64().unwrap_or(0.0)
}
pub fn require(a: &Value, keys: &[&str]) -> Result<()> {
    for key in keys {
        if a.get(key).is_none() {
            return Err(Error::new(
                "invalid_argument",
                format!("{} requires {key}", s(a, "action")),
            ));
        }
    }
    Ok(())
}
pub fn publish_png(path: &Path, bytes: &[u8]) -> Result<()> {
    if bytes.len() < 24
        || !bytes.starts_with(b"\x89PNG\r\n\x1a\n")
        || bytes.len() > 32 * 1024 * 1024
    {
        return Err(Error::new(
            "invalid_screenshot",
            "Device returned an invalid or oversized PNG",
        ));
    }
    atomic_write(path, bytes)
}
pub struct ManagedProcess(pub Child);
impl Drop for ManagedProcess {
    fn drop(&mut self) {
        tc::stop(&mut self.0);
    }
}
pub fn spawn(args: &[String], log: &Path) -> Result<ManagedProcess> {
    let mut command = Command::new(&args[0]);
    let output = private_file(log, true)?;
    command
        .args(&args[1..])
        .stdin(Stdio::null())
        .stdout(output.try_clone()?)
        .stderr(output)
        .envs(tc::environment()?)
        .process_group(0);
    tc::parent_bound(&mut command);
    Ok(ManagedProcess(command.spawn()?))
}
fn token(pid: u32) -> String {
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
fn directory(id: &str) -> Result<PathBuf> {
    identifier(id, "runtime session", Some("[a-f0-9]{32}"))?;
    Ok(home()?.join("native-sessions").join(id))
}
fn sockets() -> Result<PathBuf> {
    // Room for the directory name and "/<session>-idb.sock".
    let path = socket_root(84).join(format!(
        "tomacco-live-{}-{}",
        unsafe { libc::geteuid() },
        &digest(home()?.to_string_lossy().as_bytes())[..16]
    ));
    private_runtime_dir(&path).map_err(|error| {
        Error::new(
            "runtime_directory",
            "Runtime sockets require an owner-only directory",
        )
        .details(json!({"path":path,"reason":error.message}))
    })?;
    Ok(path)
}
fn metadata(id: &str) -> Result {
    read_json(&directory(id)?.join("state.json"), None)
}
pub fn request(id: &str, operation: &str, args: &Value) -> Result {
    let state = metadata(id)?;
    let mut socket = UnixStream::connect(s(&state, "control_socket")).map_err(|_| {
        Error::new(
            "runtime_unavailable",
            "Native session is no longer connected",
        )
    })?;
    socket.set_read_timeout(Some(Duration::from_secs(if operation == "state" {
        2
    } else {
        30
    })))?;
    socket.set_write_timeout(Some(Duration::from_secs(3)))?;
    writeln!(
        socket,
        "{}",
        json!({"operation":operation,"arguments":args})
    )?;
    let mut bytes = Vec::new();
    BufReader::new(socket)
        .take(JSON_LIMIT + 1)
        .read_until(b'\n', &mut bytes)?;
    if bytes.len() as u64 > JSON_LIMIT {
        return Err(Error::new(
            "runtime_protocol",
            "Runtime response exceeded its limit",
        ));
    }
    let response: Value = serde_json::from_slice(&bytes)?;
    if response["ok"] == true {
        Ok(response["result"].clone())
    } else {
        Err(Error::new(
            s(&response["error"], "code"),
            s(&response["error"], "message"),
        )
        .details(response["error"].clone()))
    }
}
pub fn handle(op: &str, a: &Value) -> Result {
    let id = s(a, "session");
    match op {
        "runtime.start" => start(a),
        "runtime.list" => {
            let mut result = Vec::new();
            for p in glob_paths(&format!(
                "{}/native-sessions/*/state.json",
                glob::Pattern::escape(&home()?.to_string_lossy())
            ))? {
                let value = read_json(&p, None)?;
                result.push(state(s(&value, "session"))?);
            }
            result.sort_by(|a, b| number(b, "created").total_cmp(&number(a, "created")));
            Ok(json!(result))
        }
        "runtime.state" => state(id),
        "runtime.audio" => audio::handle(a),
        "runtime.stop" => {
            let audio = audio::stop(id).unwrap_or_else(audio::failure);
            let value = state(id)?;
            let mut result = if !["starting", "connected"].contains(&s(&value, "status")) {
                json!({"stopping":false,"status":value["status"]})
            } else {
                request(id, "stop", a)?
            };
            if s(&value, "platform") == "android" {
                result["audio"] = audio;
            }
            Ok(result)
        }
        "runtime.input" => request(id, "input", a),
        "runtime.capture" => {
            let mut a = a.clone();
            a["path"] = json!(output_path(s(&a, "path"))?);
            request(id, "capture", &a)
        }
        "runtime.hierarchy" => request(id, "hierarchy", a),
        _ => Err(Error::new("operation_not_found", op)),
    }
}
fn state(id: &str) -> Result {
    let mut value = metadata(id)?;
    if ["starting", "connected"].contains(&s(&value, "status")) {
        if !s(&value, "pid_token").is_empty()
            && token(n(&value, "pid") as u32) != s(&value, "pid_token")
        {
            value["status"] = json!("disconnected");
            value["error"] = json!("Runtime worker exited");
            if s(&value, "platform") == "android" {
                android::recover(&value)?;
            }
            for suffix in ["v.sock", "c.sock", "idb.sock"] {
                let _ = fs::remove_file(sockets()?.join(format!("{id}-{suffix}")));
            }
            atomic_json(&directory(id)?.join("state.json"), &value)?;
        } else if let Ok(live) = request(id, "state", &json!({})) {
            value = live;
        }
    }
    if s(&value, "platform") == "android" {
        if !["starting", "connected"].contains(&s(&value, "status")) {
            let _ = audio::stop(id);
        }
        value["audio"] = audio::state(id).unwrap_or_else(audio::failure);
    }
    Ok(value)
}
fn start(a: &Value) -> Result {
    identifier(s(a, "device"), "device", None)?;
    let key = json!([s(a, "platform"), s(a, "remote"), s(a, "device")]).to_string();
    let admission = Lock::open(
        &home()?
            .join("locks")
            .join(format!("runtime-start-{}.lock", digest(key.as_bytes()))),
    )?;
    admission.acquire(false)?;
    let mut candidates = Vec::new();
    for file in glob_paths(&format!(
        "{}/native-sessions/*/state.json",
        glob::Pattern::escape(&home()?.to_string_lossy())
    ))? {
        let value = read_json(&file, None)?;
        if ["platform", "device", "remote"]
            .iter()
            .any(|field| s(&value, field) != s(a, field))
        {
            continue;
        }
        let value = state(s(&value, "session"))?;
        if ["connected", "starting"].contains(&s(&value, "status")) {
            candidates.push(value);
        }
    }
    candidates.sort_by(|left, right| number(right, "created").total_cmp(&number(left, "created")));
    if let Some(value) = candidates.first() {
        let id = s(value, "session").to_string();
        let old = crate::api::arguments(
            "runtime.start",
            &read_json(&directory(&id)?.join("spec.json"), None)?,
        )?;
        let fields: &[&str] = if s(a, "platform") == "android" {
            &["fps", "bit_rate", "server"]
        } else if s(a, "remote").is_empty() {
            &["fps", "bit_rate", "runner_bundle_id", "runner_mode"]
        } else {
            &["fps", "bit_rate"]
        };
        for &field in fields {
            if a[field] != old[field] {
                return Err(Error::new(
                    "runtime_configuration_conflict",
                    "This device already has a native session with different stream settings",
                )
                .remedy("Reuse the existing session, or stop it before changing stream settings")
                .details(
                    json!({"session":id,"field":field,"current":old[field],"requested":a[field]}),
                ));
            }
        }
        drop(admission);
        let mut value = wait_ready(&id)?;
        value["reused"] = json!(true);
        return audio::attach(value, a);
    }
    if s(a, "platform") == "android" {
        android::server_path(a)?;
    } else if !s(a, "remote").is_empty() {
        crate::remote::workspace(s(a, "remote"))?;
    } else {
        iphone::validate(a)?;
    }
    let id = uuid::Uuid::new_v4().simple().to_string();
    let dir = directory(&id)?;
    private_dir(&dir)?;
    let socket = sockets()?;
    let mut state = json!({"session":id,"status":"starting","platform":a["platform"],"device":a["device"],"remote":a["remote"],"created":now(),"video_socket":socket.join(format!("{id}-v.sock")),"control_socket":socket.join(format!("{id}-c.sock")),"log_path":dir.join("worker.log"),"native_runtime":true,"packets":0,"video_bytes":0});
    atomic_json(&dir.join("spec.json"), a)?;
    atomic_json(&dir.join("state.json"), &state)?;
    let output = private_file(&dir.join("worker.log"), false)?;
    let mut command = Command::new(crate::core::own_executable()?);
    command
        .args(["_runtime_worker", &id])
        .stdin(Stdio::null())
        .stdout(output.try_clone()?)
        .stderr(output);
    unsafe {
        command.pre_exec(|| {
            if libc::setsid() < 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            state["status"] = json!("failed");
            state["error"] = json!(error.to_string());
            atomic_json(&dir.join("state.json"), &state)?;
            return Err(error.into());
        }
    };
    state["pid"] = json!(child.id());
    state["pid_token"] = json!(token(child.id()));
    atomic_json(&dir.join("state.json"), &state)?;
    thread::spawn(move || {
        let _ = child.wait();
    });
    drop(admission);
    audio::attach(wait_ready(&id)?, a)
}
fn wait_ready(id: &str) -> Result {
    let end = Instant::now() + Duration::from_secs(30);
    while Instant::now() < end {
        let state = metadata(id)?;
        if s(&state, "status") == "connected" {
            return Ok(state);
        }
        if s(&state, "status") == "failed" {
            return Err(Error::new("runtime_start_failed", s(&state, "error")).details(state));
        }
        thread::sleep(Duration::from_millis(100));
    }
    Err(Error::new(
        "runtime_start_timeout",
        "Native runtime is still starting; inspect runtime.state",
    )
    .details(metadata(id)?))
}
fn wait_listener(listener: &UnixListener) {
    let mut descriptor = libc::pollfd {
        fd: listener.as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    };
    // Wake immediately on input; a bounded timeout lets stopped workers exit.
    unsafe {
        libc::poll(&mut descriptor, 1, 100);
    }
}
struct Operation {
    op: String,
    args: Value,
    result: mpsc::SyncSender<Result>,
}
pub fn worker(id: &str) -> Result<()> {
    let dir = directory(id)?;
    // The spawning process records the PID before the worker publishes its state.
    let deadline = Instant::now() + Duration::from_secs(2);
    while metadata(id)?["pid"].is_null() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    let spec = read_json(&dir.join("spec.json"), None)?;
    let state = metadata(id)?;
    let stopped = Arc::new(AtomicBool::new(false));
    let shared = Arc::new(Mutex::new(Hub {
        state: state.clone(),
        stopped: stopped.clone(),
        state_file: dir.join("state.json"),
        clients: Vec::new(),
        config: Vec::new(),
        gop: Vec::new(),
        cached_bytes: 0,
        receiving_config: false,
        cached_key: false,
    }));
    let listeners = (|| -> Result<_> {
        let video = UnixListener::bind(s(&state, "video_socket"))?;
        video.set_nonblocking(true)?;
        let control = UnixListener::bind(s(&state, "control_socket"))?;
        control.set_nonblocking(true)?;
        for key in ["video_socket", "control_socket"] {
            fs::set_permissions(s(&state, key), fs::Permissions::from_mode(0o600))?;
        }
        Ok((video, control))
    })();
    let (video, control) = match listeners {
        Ok(pair) => pair,
        Err(error) => {
            shared
                .lock()
                .unwrap()
                .update(json!({"status":"failed","error":error.message,"ended":now()}))?;
            for key in ["video_socket", "control_socket"] {
                let _ = fs::remove_file(s(&state, key));
            }
            return Err(error);
        }
    };
    let (commands, receive) = mpsc::sync_channel::<Operation>(256);
    let count = Arc::new(AtomicUsize::new(0));
    let hub = shared.clone();
    let stopping = stopped.clone();
    thread::spawn(move || {
        while !stopping.load(Ordering::Relaxed) {
            wait_listener(&video);
            if let Ok((mut socket, _)) = video.accept() {
                let (tx, rx) = mpsc::sync_channel::<Packet>(512);
                {
                    let mut h = hub.lock().unwrap();
                    if h.clients.len() >= 32 {
                        continue;
                    }
                    for packet in h.config.iter().chain(h.gop.iter()) {
                        let _ = tx.try_send(packet.clone());
                    }
                    h.clients.push(tx);
                }
                let stopped = stopping.clone();
                thread::spawn(move || {
                    let _ = socket.set_write_timeout(Some(Duration::from_secs(2)));
                    while !stopped.load(Ordering::Relaxed) {
                        match rx.recv_timeout(Duration::from_millis(500)) {
                            Ok(packet) => {
                                if socket
                                    .write_all(&(packet.len() as u32).to_be_bytes())
                                    .and_then(|_| socket.write_all(&packet))
                                    .is_err()
                                {
                                    break;
                                }
                            }
                            Err(mpsc::RecvTimeoutError::Disconnected) => break,
                            Err(mpsc::RecvTimeoutError::Timeout) => (),
                        }
                    }
                });
            }
        }
    });
    let hub = shared.clone();
    let stopping = stopped.clone();
    thread::spawn(move || {
        while !stopping.load(Ordering::Relaxed) {
            wait_listener(&control);
            if let Ok((socket, _)) = control.accept() {
                if count.load(Ordering::Relaxed) >= 32 {
                    continue;
                }
                count.fetch_add(1, Ordering::Relaxed);
                let count = count.clone();
                let commands = commands.clone();
                let hub = hub.clone();
                thread::spawn(move || {
                    let run = || -> Result<()> {
                        socket.set_read_timeout(Some(Duration::from_secs(5)))?;
                        socket.set_write_timeout(Some(Duration::from_secs(3)))?;
                        let mut socket = socket;
                        let mut bytes = Vec::new();
                        BufReader::new(socket.try_clone()?)
                            .take(65537)
                            .read_until(b'\n', &mut bytes)?;
                        if bytes.len() > 65536 {
                            return Err(Error::new("runtime_protocol", "Input too large"));
                        }
                        let value: Value = serde_json::from_slice(&bytes)?;
                        let op = s(&value, "operation");
                        let result = if op == "state" {
                            Ok(hub.lock().unwrap().state.clone())
                        } else if op == "stop" {
                            hub.lock().unwrap().stopped.store(true, Ordering::Relaxed);
                            Ok(json!({"stopping":true}))
                        } else {
                            let (tx, rx) = mpsc::sync_channel(1);
                            commands
                                .try_send(Operation {
                                    op: op.into(),
                                    args: value["arguments"].clone(),
                                    result: tx,
                                })
                                .map_err(|_| {
                                    Error::new("runtime_busy", "Runtime input queue is full")
                                })?;
                            rx.recv_timeout(Duration::from_secs(25)).map_err(|_| {
                                Error::new("runtime_timeout", "Device did not answer")
                            })?
                        };
                        writeln!(socket, "{}", api::envelope(result))?;
                        Ok(())
                    };
                    let _ = run();
                    count.fetch_sub(1, Ordering::Relaxed);
                });
            }
        }
    });
    let result = (|| -> Result<()> {
        let mut device: Box<dyn Device> = if s(&spec, "platform") == "android" {
            Box::new(android::connect(&spec, shared.clone(), &dir)?)
        } else if !s(&spec, "remote").is_empty() {
            Box::new(ios::connect(&spec, shared.clone(), &dir)?)
        } else {
            Box::new(iphone::connect(&spec, shared.clone(), &dir)?)
        };
        shared
            .lock()
            .unwrap()
            .update(json!({"status":"connected"}))?;
        // A recording of a UI test (testing.record) takes Android input.
        let mut recording = if s(&spec, "platform") == "android" {
            crate::recorder::Store::home()
                .ok()
                .map(|store| crate::recorder::Live::new(store, s(&spec, "device")))
        } else {
            None
        };
        while !stopped.load(Ordering::Relaxed) {
            if let Ok(command) = receive.recv_timeout(Duration::from_millis(100)) {
                let answer = match command.op.as_str() {
                    "input" => api::arguments("runtime.input", &command.args).and_then(|a| {
                        let step = recording.as_mut().and_then(|live| {
                            live.before(&a, || {
                                device
                                    .hierarchy()
                                    .ok()
                                    .map(|value| s(&value, "xml").to_string())
                                    .filter(|xml| !xml.is_empty())
                            })
                        });
                        let result = device.input(&a)?;
                        if let Some(live) = &recording {
                            live.after(step);
                        }
                        Ok(result)
                    }),
                    "capture" => api::arguments("runtime.capture", &command.args)
                        .and_then(|a| device.capture(&output_path(s(&a, "path"))?)),
                    "hierarchy" => device.hierarchy(),
                    _ => Err(Error::new("operation_not_found", command.op)),
                };
                let _ = command.result.send(answer);
            }
        }
        Ok(())
    })();
    stopped.store(true, Ordering::Relaxed);
    if s(&spec, "platform") == "android" {
        let _ = audio::stop(id);
    }
    {
        let mut hub = shared.lock().unwrap();
        let patch = match &result {
            Ok(_) => {
                json!({"status":if s(&hub.state,"error").is_empty(){"disconnected"}else{"failed"},"ended":now()})
            }
            Err(e) => json!({"status":"failed","error":e.message,"ended":now()}),
        };
        hub.update(patch)?;
    }
    for key in ["video_socket", "control_socket"] {
        let _ = fs::remove_file(s(&state, key));
    }
    result
}
