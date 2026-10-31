//! User-owned persistent debugger sessions shared by CLI, MCP and desktop.
use crate::{android_debug, api, core::*, dap::Dap, devices, jdwp::Jdwp, toolchain as tc};
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::unix::{
        fs::PermissionsExt,
        net::{UnixListener, UnixStream},
        process::CommandExt,
    },
    path::PathBuf,
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};
fn directory() -> Result<PathBuf> {
    let dir = home()?.join("debug");
    private_dir(&dir)?;
    Ok(dir)
}
fn session_dir(id: &str) -> Result<PathBuf> {
    identifier(id, "debug session", Some("[a-f0-9]{32}"))?;
    Ok(directory()?.join(id))
}
fn socket(id: &str) -> Result<PathBuf> {
    let dir = session_dir(id)?;
    let path = dir.join("control.sock");
    if path.as_os_str().len() < 100 {
        return Ok(path);
    }
    // A shared temporary directory: the name carries the user, and a
    // directory someone else created first is refused, never bound into.
    let root = socket_root(80).join(format!(
        "tomacco-debug-{}-{}",
        unsafe { libc::geteuid() },
        &digest(directory()?.to_string_lossy().as_bytes())[..16]
    ));
    private_runtime_dir(&root)?;
    Ok(root.join(format!("{id}.sock")))
}
fn token(pid: u64) -> Option<String> {
    let text = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    text.rsplit_once(')')?
        .1
        .split_whitespace()
        .nth(19)
        .map(String::from)
}
fn process_namespace() -> String {
    fs::read_link("/proc/self/ns/pid")
        .map(|v| v.to_string_lossy().into_owned())
        .unwrap_or_default()
}
fn metadata(id: &str) -> Result {
    let dir = session_dir(id)?;
    let mut v = read_json(&dir.join("state.json"), None)?;
    let worker_alive = token(n(&v, "pid")).as_deref() == v["process_token"].as_str();
    if !b(&v, "terminated") && !worker_alive {
        if s(&v, "process_namespace").is_empty()
            || s(&v, "process_namespace") == process_namespace()
        {
            v["terminated"] = json!(true);
            v["state"] = json!("worker_exited");
        } else {
            v["worker_visibility"] = json!("different_process_namespace");
        }
    }
    if b(&v, "terminated") && dir.join("android-native.json").is_file() {
        let mut cleanup = read_json(&dir.join("android-cleanup.json"), Some(json!({})))?;
        if !worker_alive
            && !b(&cleanup, "complete")
            && s(&v, "process_namespace") == process_namespace()
        {
            cleanup = android_debug::recover(&dir)
                .unwrap_or_else(|e| json!({"complete":false,"errors":[e.value()]}));
        }
        v["cleanup"] = cleanup;
    }
    Ok(v)
}
fn rpc(a: &Value, method: &str) -> Result {
    let mut conn = UnixStream::connect(socket(s(a, "session"))?).map_err(|e| {
        Error::new("debug_session_unavailable", e.to_string())
            .remedy("Start a new debug session or inspect debug.list")
    })?;
    conn.set_read_timeout(Some(Duration::from_secs(30)))?;
    conn.set_write_timeout(Some(Duration::from_secs(5)))?;
    writeln!(conn, "{}", json!({"method":method,"arguments":a}))?;
    let mut line = String::new();
    BufReader::new(conn)
        .take(JSON_LIMIT + 1)
        .read_line(&mut line)?;
    if line.len() as u64 > JSON_LIMIT {
        return Err(Error::new(
            "response_too_large",
            "Debugger response exceeds 2 MiB",
        ));
    }
    let value: Value = serde_json::from_str(&line)?;
    if b(&value, "ok") {
        Ok(value["result"].clone())
    } else {
        Err(
            Error::new(s(&value["error"], "code"), s(&value["error"], "message"))
                .details(value["error"]["details"].clone()),
        )
    }
}
pub fn handle(op: &str, a: &Value) -> Result {
    match op {
        "debug.start" => start(a),
        "debug.list" => {
            let mut result = Vec::new();
            for p in fs::read_dir(directory()?)? {
                let p = p?;
                let id = p.file_name().to_string_lossy().into_owned();
                if let Ok(value) = metadata(&id) {
                    result.push(value);
                }
            }
            result.sort_by(|a, b| {
                b["created"]
                    .as_f64()
                    .unwrap_or(0.0)
                    .total_cmp(&a["created"].as_f64().unwrap_or(0.0))
            });
            Ok(json!({"sessions":result}))
        }
        "debug.state" => {
            let meta = metadata(s(a, "session"))?;
            if b(&meta, "terminated") {
                return Ok(meta);
            }
            // Live queries are ordered after the preceding control request.
            // If the socket closes meanwhile, the final atomic snapshot wins.
            match rpc(a, op) {
                Ok(value) => Ok(value),
                Err(error) => {
                    let latest = metadata(s(a, "session"))?;
                    if b(&latest, "terminated") {
                        Ok(latest)
                    } else {
                        Err(error)
                    }
                }
            }
        }
        "debug.events" => {
            let mut meta = metadata(s(a, "session"))?;
            if !b(&meta, "terminated") {
                match rpc(a, op) {
                    Ok(events) => return Ok(events),
                    Err(error) => {
                        meta = metadata(s(a, "session"))?;
                        if !b(&meta, "terminated") {
                            return Err(error);
                        }
                    }
                }
            }
            if b(&meta, "terminated") {
                let events = read_json(
                    &session_dir(s(a, "session"))?.join("events.json"),
                    Some(json!([])),
                )?;
                Ok(
                    json!({"cursor":meta["event_cursor"],"events":events.as_array().unwrap().iter().filter(|v|n(v,"cursor")>n(a,"after")).take(n(a,"limit") as usize).collect::<Vec<_>>()}),
                )
            } else {
                rpc(a, op)
            }
        }
        _ => rpc(a, op),
    }
}
fn start(a: &Value) -> Result {
    let backend = s(a, "backend");
    if backend == "android-lldb" {
        android_debug::validate(a)?;
    } else if backend == "jdwp" {
        if s(a, "device").is_empty() && n(a, "port") == 0 {
            return Err(Error::new(
                "invalid_argument",
                "JDWP needs an Android device and bundle_id, or a localhost port",
            ));
        }
        if !s(a, "device").is_empty() {
            identifier(
                s(a, "bundle_id"),
                "application ID",
                Some(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+"),
            )?;
        }
    } else if n(a, "pid") == 0 {
        path(s(a, "program"), false, true)?;
    }
    let mut spec = a.clone();
    if backend == "lldb" || backend == "android-lldb" {
        if s(&spec, "cwd").is_empty() {
            spec["cwd"] = json!(std::env::current_dir()?);
        } else {
            path(s(&spec, "cwd"), true, true)?;
        }
        if spec.get("args").is_none() {
            spec["args"] = json!([]);
        }
    }
    let mut fingerprint = spec.clone();
    fingerprint.sort_all_objects();
    let spec_digest = digest(fingerprint.to_string().as_bytes());
    // Serialize admission as well as the worker lifetime. Repeated agent/UI
    // requests reuse this session without restarting or reattaching the app.
    let _admission = if !s(a, "device").is_empty() {
        let lock = Lock::open(&directory()?.join(format!(
            "admission-{}.lock",
            digest(s(a, "device").as_bytes())
        )))?;
        lock.acquire(false)?;
        for entry in fs::read_dir(directory()?)? {
            let entry = entry?;
            let id = entry.file_name().to_string_lossy().into_owned();
            if let Ok(mut active) = metadata(&id) {
                if !b(&active, "terminated") && s(&active, "device") == s(a, "device") {
                    if s(&active, "spec_digest") == spec_digest && b(&active, "ready") {
                        active["reused"] = json!(true);
                        return Ok(active);
                    }
                    return Err(Error::new("device_debug_busy", "An existing debugger owns this Android device; use or disconnect that session first").details(active));
                }
            }
        }
        Some(lock)
    } else {
        None
    };
    let id = uuid::Uuid::new_v4().simple().to_string();
    let dir = session_dir(&id)?;
    private_dir(&dir)?;
    spec["session"] = json!(id);
    spec["spec_digest"] = json!(spec_digest);
    atomic_json(&dir.join("spec.json"), &spec)?;
    let stderr = fs::File::create(dir.join("worker.log"))?;
    let mut cmd = Command::new(crate::core::own_executable()?);
    cmd.args(["_debug_worker", &id])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::from(stderr));
    unsafe {
        cmd.pre_exec(|| {
            if libc::setsid() < 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut child = cmd.spawn()?;
    let pid = child.id();
    thread::spawn(move || {
        let _ = child.wait();
    });
    let end = Instant::now() + Duration::from_secs(50);
    loop {
        if let Ok(v) = read_json(&dir.join("state.json"), None) {
            if let Some(error) = v.get("error") {
                return Err(Error::new(s(error, "code"), s(error, "message")).details(v));
            }
            if b(&v, "ready") {
                return Ok(v);
            }
        }
        if token(pid as u64).is_none() {
            return Err(Error::new(
                "debug_worker_failed",
                format!(
                    "Debug worker exited; inspect {}",
                    dir.join("worker.log").display()
                ),
            ));
        }
        if Instant::now() >= end {
            return Err(
                Error::new("debug_timeout", "Debugger initialization is still pending")
                    .details(json!({"session":id,"pid":pid})),
            );
        }
        thread::sleep(Duration::from_millis(50));
    }
}
struct AndroidCleanup {
    device: String,
    forward: String,
    clear_debug: bool,
    previous_app: String,
    previous_wait: bool,
}
impl Drop for AndroidCleanup {
    fn drop(&mut self) {
        if self.clear_debug {
            let mut restore = argv(&["am", "clear-debug-app"]);
            if !self.previous_app.is_empty() && self.previous_app != "null" {
                restore = argv(&["am", "set-debug-app", "--persistent"]);
                if self.previous_wait {
                    restore.push("-w".into());
                }
                restore.push(self.previous_app.clone());
            }
            if let Ok(args) = devices::shell(&self.device, &restore) {
                let _ = tc::run(&args, 10, None, false);
            }
        }
        if !self.forward.is_empty() {
            if let Ok(mut args) = devices::adb(&self.device) {
                args.extend(argv(&["forward", "--remove", &self.forward]));
                let _ = tc::run(&args, 10, None, false);
            }
        }
    }
}
fn android(a: &Value) -> Result<(u16, AndroidCleanup)> {
    let device = s(a, "device");
    let bundle = s(a, "bundle_id");
    let mut cleanup = AndroidCleanup {
        device: device.into(),
        forward: String::new(),
        clear_debug: false,
        previous_app: String::new(),
        previous_wait: false,
    };
    let run = |args: &[&str]| tc::run(&devices::shell(device, &argv(args))?, 20, None, true);
    if b(a, "launch") {
        let previous = run(&["settings", "get", "global", "debug_app"])?;
        cleanup.previous_app = s(&previous, "stdout").trim().into();
        let previous = run(&["settings", "get", "global", "wait_for_debugger"])?;
        cleanup.previous_wait = s(&previous, "stdout").trim() == "1";
        run(&["am", "force-stop", bundle])?;
        run(&["am", "set-debug-app", "-w", bundle])?;
        cleanup.clear_debug = true;
        let activity = run(&[
            "cmd",
            "package",
            "resolve-activity",
            "--brief",
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.LAUNCHER",
            "-p",
            bundle,
        ])?;
        let component = s(&activity, "stdout")
            .lines()
            .map(str::trim)
            .find(|v| v.starts_with(&format!("{bundle}/")))
            .ok_or_else(|| {
                Error::new("launcher_missing", "Application has no launcher activity")
            })?;
        run(&["am", "start", "-n", component])?;
    }
    let end = Instant::now() + Duration::from_secs(15);
    let pid = loop {
        let v = run(&["pidof", bundle]);
        if let Ok(v) = v {
            if let Some(pid) = s(&v, "stdout").split_whitespace().next() {
                if pid.parse::<u32>().is_ok() {
                    break pid.to_string();
                }
            }
        }
        if Instant::now() >= end {
            return Err(Error::new(
                "process_missing",
                "No Android process to attach; launch a debuggable build",
            ));
        }
        thread::sleep(Duration::from_millis(100));
    };
    let mut cmd = devices::adb(device)?;
    cmd.extend(["forward".into(), "tcp:0".into(), format!("jdwp:{pid}")]);
    let result = tc::run(&cmd, 15, None, true)?;
    let port = s(&result, "stdout")
        .trim()
        .parse::<u16>()
        .map_err(|_| Error::new("debug_forward", "adb did not allocate a debug port"))?;
    cleanup.forward = format!("tcp:{port}");
    Ok((port, cleanup))
}
enum Backend {
    Jdwp(Jdwp),
    Dap(Dap),
}
impl Backend {
    fn poll(&mut self) -> Result<()> {
        match self {
            Self::Jdwp(v) => v.poll(),
            Self::Dap(v) => v.poll(),
        }
    }
    fn state(&self) -> (bool, bool, String) {
        match self {
            Self::Jdwp(v) => (v.stopped, v.terminated, v.thread.clone()),
            Self::Dap(v) => (v.stopped, v.terminated, v.thread.clone()),
        }
    }
    fn events(&mut self) -> Vec<Value> {
        match self {
            Self::Jdwp(v) => std::mem::take(&mut v.events),
            Self::Dap(v) => std::mem::take(&mut v.events),
        }
    }
    fn dispatch(&mut self, op: &str, a: &Value) -> Result {
        let thread = if s(a, "thread_id").is_empty() {
            self.state().2
        } else {
            s(a, "thread_id").into()
        };
        match self {
            Self::Jdwp(v) => match op {
                "debug.breakpoints" => v.breakpoints(
                    s(a, "source"),
                    &a["lines"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|v| v.as_u64().unwrap() as u32)
                        .collect::<Vec<_>>(),
                ),
                "debug.threads" => v.threads(),
                "debug.stack" => v.stack(&thread, n(a, "limit")),
                "debug.variables" => v.variables(&thread, s(a, "frame_id"), s(a, "reference")),
                "debug.control" => v.control(s(a, "action"), &thread),
                "debug.evaluate" => {
                    let expression = s(a, "expression");
                    let mut parts = expression.split('.');
                    let name = parts.next().unwrap_or("");
                    let vars = v.variables(&thread, s(a, "frame_id"), "")?;
                    let mut value = vars["variables"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .find(|v| s(v, "name") == name)
                        .cloned()
                        .ok_or_else(|| {
                            Error::new(
                                "variable_missing",
                                "JDWP expressions support local variables and field paths",
                            )
                        })?;
                    for name in parts {
                        let vars = v.variables("", "", s(&value, "reference"))?;
                        value = vars["variables"]
                            .as_array()
                            .unwrap()
                            .iter()
                            .find(|v| s(v, "name") == name)
                            .cloned()
                            .ok_or_else(|| {
                                Error::new(
                                    "variable_missing",
                                    format!("Field {name} is unavailable"),
                                )
                            })?;
                    }
                    Ok(value)
                }
                _ => Err(Error::new("operation_not_found", op)),
            },
            Self::Dap(v) => {
                let numeric = |key: &str| -> Result<u64> {
                    s(a, key).parse().map_err(|_| {
                        Error::new(
                            "invalid_argument",
                            format!("{key} must be a numeric debugger ID"),
                        )
                    })
                };
                match op{
    "debug.breakpoints"=>v.request("setBreakpoints",json!({"source":{"path":s(a,"source")},"breakpoints":a["lines"].as_array().unwrap().iter().map(|l|json!({"line":l})).collect::<Vec<_>>(),"sourceModified":false})),
    "debug.threads"=>v.request("threads",json!({})),"debug.stack"=>{let mut value=v.request("stackTrace",json!({"threadId":thread.parse::<u64>().map_err(|_|Error::new("invalid_argument","Select a thread"))?,"startFrame":0,"levels":n(a,"limit")}))?;value["frames"]=value["stackFrames"].clone();Ok(value)},
    "debug.variables"=>{if !s(a,"reference").is_empty(){v.request("variables",json!({"variablesReference":numeric("reference")?,"start":n(a,"start"),"count":n(a,"count")}))}else{v.request("scopes",json!({"frameId":numeric("frame_id")?}))}},
    "debug.evaluate"=>{let mut args=json!({"expression":a["expression"],"context":"watch"});if !s(a,"frame_id").is_empty(){args["frameId"]=json!(numeric("frame_id")?);}v.request("evaluate",args)},
    "debug.control"=>v.control(s(a,"action"),&thread),_=>Err(Error::new("operation_not_found",op))}
            }
        }
    }
}
pub fn worker(id: &str) -> Result<()> {
    let dir = session_dir(id)?;
    let spec = read_json(&dir.join("spec.json"), None)?;
    let mut meta = json!({"session":id,"backend":spec["backend"],"device":spec["device"],"bundle_id":spec["bundle_id"],"program":spec["program"],"created":now(),"pid":std::process::id(),"process_token":token(std::process::id()as u64),"ready":false});
    meta["spec_digest"] = spec["spec_digest"].clone();
    meta["process_namespace"] = json!(process_namespace());
    meta["state"] = json!("starting");
    atomic_json(&dir.join("state.json"), &meta)?;
    let result = serve(&dir, &spec, &mut meta);
    if let Err(error) = &result {
        meta["error"] = error.value();
    }
    meta["terminated"] = json!(true);
    meta["state"] = json!("disconnected");
    if dir.join("android-cleanup.json").is_file() {
        meta["cleanup"] = read_json(&dir.join("android-cleanup.json"), None)?;
    }
    atomic_json(&dir.join("state.json"), &meta)?;
    let _ = fs::remove_file(socket(id)?);
    result
}
fn serve(dir: &std::path::Path, spec: &Value, meta: &mut Value) -> Result<()> {
    let mut cleanup = None;
    let mut native_cleanup = None;
    let _device_lock = if !s(spec, "device").is_empty() {
        let lock = Lock::open(&directory()?.join(format!(
            "active-{}.lock",
            digest(s(spec, "device").as_bytes())
        )))?;
        if !lock.acquire(true)? {
            return Err(Error::new(
                "device_debug_busy",
                "This Android device already has a debugger",
            ));
        }
        Some(lock)
    } else {
        None
    };
    let mut backend = if s(spec, "backend") == "jdwp" {
        let port = if !s(spec, "device").is_empty() {
            let (port, guard) = android(spec)?;
            cleanup = Some(guard);
            port
        } else {
            n(spec, "port") as u16
        };
        let mut jdwp = Jdwp::connect(port)?;
        if !s(spec, "device").is_empty() && b(spec, "launch") && !jdwp.stopped && !jdwp.terminated {
            jdwp.control("pause", "")?;
            let threads = jdwp.threads()?;
            if let Some(main) = threads["threads"]
                .as_array()
                .unwrap()
                .iter()
                .find(|v| s(v, "name") == "main")
            {
                jdwp.thread = s(main, "id").to_string();
            }
        }
        Backend::Jdwp(jdwp)
    } else {
        let args = vec![if s(spec, "adapter").is_empty() {
            tc::find("lldb-dap")?
        } else {
            path(s(spec, "adapter"), false, true)?
                .to_string_lossy()
                .into_owned()
        }];
        let target = if s(spec, "backend") == "android-lldb" {
            let native = android_debug::AndroidNative::start(spec, dir)?;
            meta["android_native"] = native.details();
            let target = native.dap_spec.clone();
            native_cleanup = Some(native);
            target
        } else {
            spec.clone()
        };
        let dap = Dap::start(&args, &target, &dir.join("adapter.log"))?;
        meta["capabilities"] = dap.capabilities.clone();
        Backend::Dap(dap)
    };
    let socket = socket(s(spec, "session"))?;
    let listener = UnixListener::bind(&socket)?;
    fs::set_permissions(&socket, fs::Permissions::from_mode(0o600))?;
    listener.set_nonblocking(true)?;
    meta["ready"] = json!(true);
    let mut events = std::collections::VecDeque::new();
    let mut cursor = 0u64;
    let mut persisted_cursor = 0u64;
    let mut previous = String::new();
    let mut last_activity = Instant::now();
    loop {
        backend.poll()?;
        for mut event in backend.events() {
            cursor += 1;
            event["cursor"] = json!(cursor);
            event["time"] = json!(now());
            events.push_back(event);
            if events.len() > 1024 {
                events.pop_front();
            }
        }
        let (stopped, terminated, thread) = backend.state();
        meta["stopped"] = json!(stopped);
        meta["terminated"] = json!(terminated);
        meta["thread_id"] = json!(thread);
        meta["event_cursor"] = json!(cursor);
        meta["state"] = json!(if terminated {
            "terminated"
        } else if stopped {
            "stopped"
        } else {
            "running"
        });
        let state = meta.to_string();
        if state != previous {
            if cursor != persisted_cursor {
                atomic_json(&dir.join("events.json"), &json!(events))?;
                persisted_cursor = cursor;
            }
            atomic_json(&dir.join("state.json"), meta)?;
            previous = state;
        }
        if terminated {
            break;
        }
        if last_activity.elapsed() > Duration::from_secs(86400) {
            let _ = backend.dispatch("debug.control", &json!({"action":"disconnect"}));
            break;
        }
        match listener.accept() {
            Ok((mut conn, _)) => {
                last_activity = Instant::now();
                conn.set_read_timeout(Some(Duration::from_secs(2)))?;
                conn.set_write_timeout(Some(Duration::from_secs(5)))?;
                let mut line = String::new();
                let result = (|| -> Result {
                    BufReader::new(conn.try_clone()?)
                        .take(JSON_LIMIT + 1)
                        .read_line(&mut line)?;
                    if line.len() as u64 > JSON_LIMIT {
                        return Err(Error::new(
                            "input_too_large",
                            "Debugger request exceeds limit",
                        ));
                    }
                    let request: Value = serde_json::from_str(&line)?;
                    let op = s(&request, "method");
                    let a = api::arguments(op, &request["arguments"])?;
                    if op == "debug.state" {
                        return Ok(meta.clone());
                    }
                    if op == "debug.events" {
                        return Ok(
                            json!({"cursor":cursor,"dropped_before":events.front().map(|v|n(v,"cursor")).unwrap_or(0),"events":events.iter().filter(|v|n(v,"cursor")>n(&a,"after")).take(n(&a,"limit")as usize).collect::<Vec<_>>()}),
                        );
                    }
                    backend.dispatch(op, &a)
                })();
                let _ = writeln!(conn, "{}", api::envelope(result));
            }
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(25))
            }
            Err(e) => return Err(e.into()),
        }
    }
    atomic_json(&dir.join("events.json"), &json!(events))?;
    drop(backend);
    drop(native_cleanup);
    drop(cleanup);
    Ok(())
}
