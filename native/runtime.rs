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
pub mod actions;
pub mod airplay;
pub mod android;
pub mod audio;
pub mod bluetooth;
pub mod field;
pub mod fixture;
pub mod ios;
pub mod iphone;
pub type Shared = Arc<Mutex<Hub>>;
type Packet = Arc<Vec<u8>>;
fn write_packet(socket: &mut UnixStream, packet: &[u8]) -> std::io::Result<()> {
    socket
        .write_all(&(packet.len() as u32).to_be_bytes())
        .and_then(|_| socket.write_all(packet))
}
fn serve_video(
    mut socket: UnixStream,
    replay: Vec<Packet>,
    rx: mpsc::Receiver<Packet>,
    stopped: Arc<AtomicBool>,
    viewers: Arc<AtomicUsize>,
) {
    let _ = socket.set_write_timeout(Some(Duration::from_secs(2)));
    // Replay is separate from the bounded live queue: filling that queue
    // here silently dropped the tail of a long GOP.
    for packet in replay {
        if write_packet(&mut socket, &packet).is_err() {
            viewers.fetch_sub(1, Ordering::Relaxed);
            return;
        }
    }
    while !stopped.load(Ordering::Relaxed) {
        match rx.recv_timeout(Duration::from_millis(500)) {
            Ok(packet) => {
                if write_packet(&mut socket, &packet).is_err() {
                    break;
                }
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
            Err(mpsc::RecvTimeoutError::Timeout) if peer_closed(&socket) => break,
            Err(mpsc::RecvTimeoutError::Timeout) => (),
        }
    }
    viewers.fetch_sub(1, Ordering::Relaxed);
}
pub struct Hub {
    pub state: Value,
    pub stopped: Arc<AtomicBool>,
    state_file: PathBuf,
    clients: Vec<mpsc::SyncSender<Packet>>,
    config: Vec<Packet>,
    pending_config: Vec<Packet>,
    gop: Vec<Packet>,
    cached_bytes: usize,
    receiving_config: bool,
    cached_key: bool,
    /// Desktop windows reading the video now.
    viewers: Arc<AtomicUsize>,
}
/// Whether the other end of a viewer's socket has closed it. Viewers never
/// write, so a readable socket with nothing to read is a closed one.
fn peer_closed(socket: &std::os::unix::net::UnixStream) -> bool {
    use std::os::fd::AsRawFd;
    let mut byte = 0u8;
    let read = unsafe {
        libc::recv(
            socket.as_raw_fd(),
            (&mut byte as *mut u8).cast(),
            1,
            libc::MSG_PEEK | libc::MSG_DONTWAIT,
        )
    };
    read == 0
}
impl Hub {
    pub fn viewers(&self) -> usize {
        self.viewers.load(Ordering::Relaxed)
    }
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
                self.pending_config.clear();
            }
            if self.pending_config.len() < 16
                && self.pending_config.iter().map(|p| p.len()).sum::<usize>() + packet.len()
                    <= 16 * 1024 * 1024
            {
                self.pending_config.push(packet.clone());
            }
            self.receiving_config = true;
            if key {
                // New idb sends SPS/PPS and the keyframe in one payload.
                self.finish_config();
                self.gop.clear();
                self.cached_bytes = 0;
                self.cached_key = true;
            }
        } else {
            self.finish_config();
            if key {
                self.gop.clear();
                self.cached_bytes = 0;
                self.cached_key = true;
            }
            // AirPlay sends multiple NAL slices per picture. A limit of 500
            // packets discarded even a short GOP on a high-resolution phone.
            // Keep the byte bound and a separate bound on allocation overhead.
            if self.cached_bytes + packet.len() > 16 * 1024 * 1024 || self.gop.len() >= 16384 {
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
        self.state["replay_ready"] = json!(self.cached_key);
        self.clients
            .retain(|client| client.try_send(packet.clone()).is_ok());
    }
    fn finish_config(&mut self) {
        if !self.receiving_config {
            return;
        }
        self.receiving_config = false;
        // GStreamer repeats SPS/PPS. They do not invalidate the IDR and all
        // dependent pictures that a newly opened window needs to decode.
        if self.pending_config != self.config {
            self.config = std::mem::take(&mut self.pending_config);
            self.gop.clear();
            self.cached_bytes = 0;
            self.cached_key = false;
        } else {
            self.pending_config.clear();
        }
    }
    fn replay(&self) -> Vec<Packet> {
        // A subscriber may arrive between SPS and PPS. Replay parameter sets
        // received since the last complete bundle as well, before live data.
        self.config
            .iter()
            .chain(self.gop.iter())
            .chain(self.pending_config.iter())
            .cloned()
            .collect()
    }
}
pub trait Device: Send {
    fn input(&mut self, a: &Value) -> Result;
    fn capture(&mut self, path: &Path) -> Result;
    fn hierarchy(&mut self) -> Result;
    /// The screen as inputs and frames see it now: `width` and `height` in
    /// frame pixels, `orientation`, `rotation` (quarter turns or null) and
    /// where the numbers come from. A transport that cannot tell answers
    /// `available: false` with the reason, never a guess.
    fn geometry(&mut self) -> Result {
        Ok(
            json!({"available":false,"reason":"This transport does not report its screen geometry",
            "coordinate_space":coordinate_space()}),
        )
    }
    /// The foreground app, the keyboard and the input focus, each
    /// `available: true` with what was observed or `available: false` with
    /// the reason. Nothing here is inferred from a screenshot.
    fn context(&mut self) -> Result {
        Ok(
            json!({"app":unavailable("This transport does not report the foreground app"),
            "keyboard":unavailable("This transport does not report the keyboard"),
            "focus":unavailable("This transport does not report the input focus")}),
        )
    }
    /// What input this transport can deliver, as `runtime.state` reports it
    /// under `input_capabilities`: whether touch streams live or is sent as
    /// one atomic trace on touch-up, how many contacts, pinch, fill, rotate.
    fn capabilities(&self) -> Value;
    /// `fill` with `verify_value` (native/runtime/field.rs). A transport
    /// that cannot read an editor back keeps this default, which sends
    /// nothing and says so, never a generic success.
    fn verified_fill(&mut self, _a: &Value) -> Result {
        Err(field::unsupported_transport())
    }
    /// What `input_capabilities.fill_verification` says about this transport.
    fn fill_verification(&self) -> Value {
        field::not_offered("This transport cannot read a focused editor's value back")
    }
}
/// The capabilities `runtime.state` and `runtime.observe` report, with
/// whether a verified fill is offered.
fn advertised(device: &dyn Device) -> Value {
    let mut capabilities = device.capabilities();
    capabilities["fill_verification"] = device.fill_verification();
    capabilities
}
/// One `runtime.input` on the device: a verified fill goes to the
/// transport's field driver, anything else to its input as before.
fn deliver(device: &mut dyn Device, a: &Value) -> Result {
    field::check(a)?;
    if s(a, "action") == "fill" && b(a, "verify_value") {
        device.verified_fill(a)
    } else {
        device.input(a)
    }
}
/// A field a transport cannot observe.
pub fn unavailable(reason: &str) -> Value {
    json!({"available":false,"reason":reason})
}
/// How frames and inputs relate, the same for every transport.
pub fn coordinate_space() -> Value {
    json!({"frame":"pixels of the captured PNG, origin top-left",
        "input":"runtime.input and runtime.actions take 0..1 of the frame's width and height",
        "screen_operations":"screen.* take pixels of the last frame"})
}
/// `portrait` or `landscape` from a frame's size alone.
pub fn derived_orientation(width: u32, height: u32) -> &'static str {
    if width > height {
        "landscape"
    } else {
        "portrait"
    }
}
/// One fresh look at a session: a frame with its id and time, the geometry
/// it was taken in, and what the transport can say about app, keyboard and
/// focus. Every part names its source; what the transport cannot observe is
/// `available: false`.
fn observe(device: &mut dyn Device, a: &Value, state: &Value, default_path: &Path) -> Result {
    let path = if s(a, "path").is_empty() {
        default_path.to_path_buf()
    } else {
        output_path(s(a, "path"))?
    };
    let started = Instant::now();
    let captured_at = now();
    let frame = device.capture(&path)?;
    let capture_ms = started.elapsed().as_millis() as u64;
    let (width, height) = crate::screen::png_size(&path)?;
    let mut geometry = device.geometry()?;
    if geometry.get("width").is_none() {
        geometry["frame_width"] = json!(width);
        geometry["frame_height"] = json!(height);
    }
    let mismatch = geometry["width"]
        .as_u64()
        .is_some_and(|w| w != u64::from(width))
        || geometry["height"]
            .as_u64()
            .is_some_and(|h| h != u64::from(height));
    let context = if a["context"] == json!(false) {
        let skipped = json!({"available":false,"reason":"not read: context was false"});
        json!({"app":skipped,"keyboard":skipped,"focus":skipped})
    } else {
        device.context()?
    };
    let sha = s(&frame, "sha256").to_string();
    let id = sha.chars().take(16).collect::<String>();
    let orientation = geometry["orientation"]
        .as_str()
        .unwrap_or_else(|| derived_orientation(width, height))
        .to_string();
    let summary = {
        let app = &context["app"];
        let app_text = if app["available"] == true {
            if app["home_screen"] == true {
                "Home Screen".to_string()
            } else {
                format!("{} ({})", s(app, "name"), s(app, "bundle_id"))
            }
        } else {
            "app unknown".to_string()
        };
        let keyboard = match context["keyboard"]["visible"].as_bool() {
            Some(true) => "keyboard shown",
            Some(false) => "keyboard hidden",
            None => "keyboard unknown",
        };
        let focus = match context["focus"]["focused"].as_bool() {
            Some(true) => "a field has focus",
            Some(false) => "no field has focus",
            None => "focus unknown",
        };
        format!("{app_text}; {orientation} {width}x{height}; {keyboard}; {focus}")
    };
    Ok(json!({
        "session":state["session"],"platform":state["platform"],"transport":state["transport"],
        "evidence":state.get("evidence").cloned().unwrap_or(json!("device")),
        "frame":{"id":id,"sha256":sha,"path":frame["path"],"mime_type":"image/png","bytes":frame["bytes"],
            "width":width,"height":height,"captured_at":captured_at,"capture_ms":capture_ms,"age_ms":0,
            "source":"screenshot"},
        "geometry":geometry,"geometry_matches_frame":!mismatch,
        "orientation":orientation,
        "app":context["app"],"keyboard":context["keyboard"],"focus":context["focus"],
        "fixture":context.get("fixture").cloned().unwrap_or(Value::Null),
        "input_capabilities":advertised(device),
        "context":summary,
        "note":"age_ms is 0 at capture; add the time since captured_at before acting on this frame. Fields with available false were not observed, not guessed."
    }))
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
    tc::adapt_go_ios_usbmux(&mut command)?;
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
/// A session's record (platform, remote, device, transport, status), read
/// from its directory without asking its worker; also once it ended.
pub fn session_record(id: &str) -> Result {
    metadata(id)
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
    let mut message = json!({"operation":operation,"arguments":args});
    if matches!(operation, "input" | "gesture") {
        // The call's reservation token and ledger identity travel beside the
        // arguments, which the worker validates against the public schema.
        let block = crate::coordination::worker_block();
        if !block.is_null() {
            message["coordination"] = block;
        }
    }
    writeln!(socket, "{message}")?;
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
                json!({"stopping":false,"stopped":true,"status":value["status"]})
            } else {
                let mut result = request(id, "stop", a)?;
                // The worker ends within its poll interval; a start right after
                // this call must not find the old session still "connected".
                let end = Instant::now() + Duration::from_secs(5);
                let mut status = String::new();
                while Instant::now() < end {
                    status = s(&metadata(id)?, "status").to_string();
                    if !["starting", "connected"].contains(&status.as_str()) {
                        break;
                    }
                    thread::sleep(Duration::from_millis(50));
                }
                result["stopped"] = json!(!["starting", "connected"].contains(&status.as_str()));
                result["status"] = json!(status);
                result
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
        "runtime.observe" => {
            let mut a = a.clone();
            if !s(&a, "path").is_empty() {
                a["path"] = json!(output_path(s(&a, "path"))?);
            }
            request(id, "observe", &a)
        }
        "runtime.gesture" => request(id, "gesture", a),
        "runtime.actions" => actions::run(a),
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
    if fixture::requested(a) {
        fixture::validate(a)?;
    } else if s(a, "platform") == "android" {
        android::server_path(a)?;
    } else if !s(a, "remote").is_empty() {
        crate::remote::workspace(s(a, "remote"))?;
    } else if airplay::requested(a) {
        airplay::validate(a)?;
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
        if s(&state, "status") == "disconnected" {
            return Err(Error::new(
                "runtime_start_failed",
                "The session ended before it was ready",
            )
            .remedy("Start it again with runtime.start; a session stopped at the same moment is not reused")
            .details(state));
        }
        thread::sleep(Duration::from_millis(100));
    }
    Err(Error::new(
        "runtime_start_timeout",
        "Native runtime is still starting; inspect runtime.state",
    )
    .details(metadata(id)?))
}
/// A `runtime.gesture` call as the `gesture` input action a transport takes:
/// the contacts with their timestamped points, checked for order and bounds.
pub fn gesture_input(a: &Value) -> Value {
    let mut input = a.clone();
    input["action"] = json!("gesture");
    input
}
/// Checks a gesture's contacts: 1 to 5, each 2 to 240 points with
/// non-decreasing `t_ms` within 0..=10000 and coordinates within 0..1.
pub fn check_gesture(a: &Value) -> Result<Vec<Vec<(f64, f64, u64)>>> {
    let contacts = a["contacts"]
        .as_array()
        .filter(|c| !c.is_empty() && c.len() <= 5)
        .ok_or_else(|| Error::new("invalid_argument", "contacts lists 1 to 5 contacts"))?;
    let mut out = Vec::new();
    for contact in contacts {
        let points = contact["points"]
            .as_array()
            .filter(|p| p.len() >= 2 && p.len() <= 240)
            .ok_or_else(|| Error::new("invalid_argument", "Each contact has 2 to 240 points"))?;
        let mut trace = Vec::new();
        let mut last = 0;
        for point in points {
            let (x, y) = (number(point, "x"), number(point, "y"));
            let t = n(point, "t_ms");
            if !(0.0..=1.0).contains(&x) || !(0.0..=1.0).contains(&y) || t > 10_000 || t < last {
                return Err(Error::new(
                    "invalid_argument",
                    "Points need x and y within 0..1 and t_ms in order within 10 seconds",
                ));
            }
            last = t;
            trace.push((x, y, t));
        }
        out.push(trace);
    }
    Ok(out)
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
    /// The caller's reservation token and ledger identity, if any.
    coordination: Value,
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
        pending_config: Vec::new(),
        gop: Vec::new(),
        cached_bytes: 0,
        receiving_config: false,
        cached_key: false,
        viewers: Arc::new(AtomicUsize::new(0)),
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
            if let Ok((socket, _)) = video.accept() {
                let (tx, rx) = mpsc::sync_channel::<Packet>(512);
                let replay = {
                    let mut h = hub.lock().unwrap();
                    if h.clients.len() >= 32 {
                        continue;
                    }
                    let replay = h.replay();
                    h.clients.push(tx);
                    h.viewers.fetch_add(1, Ordering::Relaxed);
                    replay
                };
                let stopped = stopping.clone();
                let viewers = hub.lock().unwrap().viewers.clone();
                thread::spawn(move || serve_video(socket, replay, rx, stopped, viewers));
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
                                    coordination: value["coordination"].clone(),
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
        let mut device: Box<dyn Device> = if fixture::requested(&spec) {
            Box::new(fixture::connect(&spec, shared.clone(), &dir)?)
        } else if s(&spec, "platform") == "android" {
            Box::new(android::connect(&spec, shared.clone(), &dir)?)
        } else if !s(&spec, "remote").is_empty() {
            Box::new(ios::connect(&spec, shared.clone(), &dir)?)
        } else if airplay::requested(&spec) {
            Box::new(airplay::connect(&spec, shared.clone(), &dir)?)
        } else {
            Box::new(iphone::connect(&spec, shared.clone(), &dir)?)
        };
        shared.lock().unwrap().update(
            json!({"status":"connected","input_capabilities":advertised(device.as_ref())}),
        )?;
        // The device this session shows, as reservations and the request
        // ledger name it, and this worker as the instance that executes.
        let mut target = crate::coordination::Target::new(
            s(&spec, "platform"),
            s(&spec, "remote"),
            s(&spec, "device"),
        );
        target.session = Some(id.to_string());
        let transport = s(&shared.lock().unwrap().state, "transport").to_string();
        let instance = format!("session:{id}:{}", crate::coordination::ledger::instance());
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
                        let kind = crate::coordination::Kind::of_input(&a);
                        crate::coordination::worker_input(
                            &target,
                            &transport,
                            &instance,
                            "runtime.input",
                            &a,
                            &command.coordination,
                            kind,
                            || {
                                let step = recording.as_mut().and_then(|live| {
                                    live.before(&a, || {
                                        device
                                            .hierarchy()
                                            .ok()
                                            .map(|value| s(&value, "xml").to_string())
                                            .filter(|xml| !xml.is_empty())
                                    })
                                });
                                let result = deliver(device.as_mut(), &a)?;
                                if let Some(live) = &recording {
                                    live.after(step);
                                }
                                Ok(result)
                            },
                        )
                    }),
                    "capture" => api::arguments("runtime.capture", &command.args)
                        .and_then(|a| device.capture(&output_path(s(&a, "path"))?)),
                    "hierarchy" => device.hierarchy(),
                    "geometry" => device.geometry(),
                    "context" => device.context(),
                    "observe" => api::arguments("runtime.observe", &command.args).and_then(|a| {
                        let state = shared.lock().unwrap().state.clone();
                        observe(device.as_mut(), &a, &state, &dir.join("observe.png"))
                    }),
                    "gesture" => api::arguments("runtime.gesture", &command.args)
                        .and_then(|a| check_gesture(&a).map(|_| a))
                        .and_then(|a| {
                            crate::coordination::worker_input(
                                &target,
                                &transport,
                                &instance,
                                "runtime.gesture",
                                &a,
                                &command.coordination,
                                crate::coordination::Kind::Action,
                                || device.input(&gesture_input(&a)),
                            )
                        }),
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

#[cfg(test)]
mod replay_tests {
    use super::*;
    fn hub() -> Hub {
        Hub {
            state: json!({}),
            stopped: Arc::new(AtomicBool::new(false)),
            state_file: PathBuf::new(),
            clients: vec![],
            config: vec![],
            pending_config: vec![],
            gop: vec![],
            cached_bytes: 0,
            receiving_config: false,
            cached_key: false,
            viewers: Arc::new(AtomicUsize::new(0)),
        }
    }
    #[test]
    fn repeated_parameter_sets_preserve_a_decodable_gop_for_late_viewers() {
        let mut h = hub();
        h.video(vec![7, 1], true, false);
        h.video(vec![8, 1], true, false);
        h.video(vec![5, 1], false, true);
        for i in 0..700 {
            if i % 100 == 0 {
                h.video(vec![7, 1], true, false);
                h.video(vec![8, 1], true, false);
            }
            h.video(vec![1, (i % 255) as u8], false, false);
        }
        assert_eq!(h.config.len(), 2);
        assert_eq!(h.gop.len(), 701);
        assert_eq!(h.gop[0].as_ref(), &[5, 1]);
        assert!(h.cached_key);
        assert_eq!(h.state["replay_ready"], true);
        // A changed PPS cannot reuse pictures encoded with the old one.
        h.video(vec![7, 1], true, false);
        h.video(vec![8, 2], true, false);
        h.video(vec![1, 3], false, false);
        assert!(!h.cached_key);
        assert!(h.gop.is_empty());
        h.video(vec![5, 2], false, true);
        assert_eq!(h.gop.len(), 1);
        assert!(h.cached_key);
    }
    #[test]
    fn combined_idb_configuration_and_keyframe_remain_replayable() {
        let mut h = hub();
        h.video(vec![7, 8, 5], true, true);
        h.video(vec![1, 1], false, false);
        assert!(h.cached_key);
        assert_eq!(h.config.len(), 1);
        assert_eq!(h.gop.len(), 1);
        h.video(vec![7, 8, 5], true, true);
        assert!(h.gop.is_empty());
        assert!(h.cached_key);
    }
    #[test]
    fn replay_memory_is_bounded_and_a_new_idr_recovers_it() {
        let mut h = hub();
        h.video(vec![5], false, true);
        h.video(vec![1; 16 * 1024 * 1024], false, false);
        assert!(!h.cached_key);
        assert!(h.gop.is_empty());
        assert_eq!(h.cached_bytes, 0);
        h.video(vec![5, 2], false, true);
        assert!(h.cached_key);
        assert_eq!(h.cached_bytes, 2);
    }
    #[test]
    fn a_replay_longer_than_the_live_queue_reaches_the_socket_in_full() {
        let (server, mut client) = UnixStream::pair().unwrap();
        client
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        let (tx, rx) = mpsc::sync_channel(512);
        let replay = (0..700u32)
            .map(|i| Arc::new(i.to_be_bytes().to_vec()))
            .collect();
        let viewers = Arc::new(AtomicUsize::new(1));
        let observed = viewers.clone();
        let stopped = Arc::new(AtomicBool::new(false));
        let worker = thread::spawn(move || serve_video(server, replay, rx, stopped, viewers));
        tx.send(Arc::new(700u32.to_be_bytes().to_vec())).unwrap();
        for expected in 0..701u32 {
            let mut header = [0; 4];
            client.read_exact(&mut header).unwrap();
            assert_eq!(u32::from_be_bytes(header), 4);
            client.read_exact(&mut header).unwrap();
            assert_eq!(u32::from_be_bytes(header), expected);
        }
        drop(tx);
        worker.join().unwrap();
        assert_eq!(observed.load(Ordering::Relaxed), 0);
    }
}
