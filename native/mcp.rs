use crate::{api, core::*};
use base64::{Engine as _, engine::general_purpose::STANDARD};
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, Read, Write},
    path::Path,
};
const PROTOCOLS: &[&str] = &["2025-11-25", "2025-06-18", "2024-11-05"];
fn rpc_error(id: Value, code: i32, message: &str) -> Value {
    json!({"jsonrpc":"2.0","id":id,"error":{"code":code,"message":message}})
}
fn respond(sink: &std::sync::Mutex<impl Write>, value: &Value) -> Result<()> {
    let mut sink = sink
        .lock()
        .map_err(|_| Error::new("transport_failed", "MCP writer unavailable"))?;
    writeln!(sink, "{value}")?;
    sink.flush()?;
    Ok(())
}
pub fn serve(mut source: impl BufRead, sink: impl Write + Send) -> Result<()> {
    use std::sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    };
    // A contact a client holds down is lifted however this server ends.
    let _touches = crate::ios_emulator::control::TouchClient::open();
    let sink = Mutex::new(sink);
    let active = Mutex::new(std::collections::HashMap::<String, Cancellation>::new());
    // Log messages are sent once the client asks for them with logging/setLevel.
    let level: Level = Arc::new(std::sync::atomic::AtomicU8::new(u8::MAX));
    std::thread::scope(|scope| -> Result<()> {
        let mut initialized = false;
        loop {
            let mut line = Vec::new();
            source
                .by_ref()
                .take(JSON_LIMIT + 1)
                .read_until(b'\n', &mut line)?;
            if line.is_empty() {
                return Ok(());
            }
            if line.len() as u64 > JSON_LIMIT {
                respond(
                    &sink,
                    &rpc_error(Value::Null, -32700, "Message exceeds 2 MiB"),
                )?;
                return Ok(());
            }
            let request: Value = match serde_json::from_slice(&line) {
                Ok(value) => value,
                Err(_) => {
                    respond(&sink, &rpc_error(Value::Null, -32700, "Invalid JSON"))?;
                    continue;
                }
            };
            if !request.is_object() || request["jsonrpc"] != "2.0" || !request["method"].is_string()
            {
                respond(
                    &sink,
                    &rpc_error(Value::Null, -32600, "Invalid JSON-RPC request"),
                )?;
                continue;
            }
            let method = s(&request, "method");
            let params = request.get("params").cloned().unwrap_or(json!({}));
            let Some(id) = request.get("id") else {
                if method == "notifications/cancelled" {
                    if let Some(token) =
                        active.lock().unwrap().get(&params["requestId"].to_string())
                    {
                        token.store(true, Ordering::Relaxed)
                    }
                }
                continue;
            };
            let id = id.clone();
            if !id.is_string() && !id.is_number() {
                respond(
                    &sink,
                    &rpc_error(Value::Null, -32600, "Request ID must be a string or number"),
                )?;
                continue;
            }
            if !params.is_object() {
                respond(&sink, &rpc_error(id, -32602, "params must be an object"))?;
                continue;
            }
            if method == "initialize" {
                initialized = true;
                let protocol = s(&params, "protocolVersion");
                respond(
                    &sink,
                    &json!({"jsonrpc":"2.0","id":id,"result":{"protocolVersion":if PROTOCOLS.contains(&protocol){protocol}else{PROTOCOLS[0]},"capabilities":{"tools":{},"resources":{},"prompts":{},"logging":{},"completions":{}},"serverInfo":{"name":"tomacco","version":VERSION},"instructions":"To see what this computer can do right now (Android emulator and devices, iOS builds, iPhones, the virtual iPhone), call capabilities. To set Tomacco up for a person, call doctor and work through next_steps in order, asking first where ask_first is true. Start with workspace.list/context for saved project/device recipes. Use files.read/write with SHA-256 revisions to avoid overwriting concurrent edits. Use project.plan before builds, poll job.get/logs until terminal status. Native UI tests use testing.run, source debugging uses debug.*, profiling uses profile.*, releases use release.*. A queued job is not a successful build. Requests may run concurrently. Cancelling a wait does not cancel the durable job; use job.cancel explicitly. Inspect doctor and device.list before native operations. Preview is Chromium, not an iOS runtime. The guides are resources: read tomacco://docs/agents and tomacco://docs/recipes."}}),
                )?;
                continue;
            }
            if method != "ping" && !initialized {
                respond(&sink, &rpc_error(id, -32000, "Initialize first"))?;
                continue;
            }
            if method == "logging/setLevel" {
                let reply = match LEVELS
                    .iter()
                    .position(|level| *level == s(&params, "level"))
                {
                    Some(index) => {
                        level.store(index as u8, Ordering::Relaxed);
                        json!({"jsonrpc":"2.0","id":id,"result":{}})
                    }
                    None => rpc_error(id, -32602, "level must be one of the syslog levels"),
                };
                respond(&sink, &reply)?;
                continue;
            }
            // Small discovery methods are immediate; slow tools/resources run independently.
            if !matches!(method, "tools/call" | "resources/read") {
                let result = response(id, method, &params);
                respond(&sink, &result)?;
                continue;
            }
            let key = id.to_string();
            let token = Arc::new(AtomicBool::new(false));
            {
                let mut requests = active.lock().unwrap();
                if requests.contains_key(&key) {
                    respond(
                        &sink,
                        &rpc_error(id, -32600, "Request ID is already in progress"),
                    )?;
                    continue;
                }
                if requests.len() >= 16 {
                    respond(
                        &sink,
                        &rpc_error(
                            id,
                            -32001,
                            "16 requests are already running; retry after one completes",
                        ),
                    )?;
                    continue;
                }
                requests.insert(key.clone(), token.clone());
            }
            let method = method.to_owned();
            let sink = &sink;
            let active = &active;
            let level = level.clone();
            scope.spawn(move || {
                let follower = Follower::new(&method, &params, level);
                let (done, finished) = std::sync::mpsc::channel::<()>();
                let result = std::thread::scope(|inner| {
                    // Notifications about this request stop before its response is sent.
                    if let Some(mut follower) = follower {
                        let token = token.clone();
                        inner.spawn(move || {
                            while let Err(std::sync::mpsc::RecvTimeoutError::Timeout) =
                                finished.recv_timeout(TICK)
                            {
                                if token.load(Ordering::Relaxed) {
                                    return;
                                }
                                for notification in follower.tick(api::dispatch) {
                                    let _ = respond(sink, &notification);
                                }
                            }
                        });
                    }
                    let result =
                        with_cancellation(Some(token.clone()), || response(id, &method, &params));
                    drop(done);
                    result
                });
                if !token.load(Ordering::Relaxed) {
                    let _ = respond(sink, &result);
                }
                active.lock().unwrap().remove(&key);
            });
        }
    })
}
/// How often a running tool call reports its progress.
const TICK: std::time::Duration = std::time::Duration::from_secs(1);

/// The MCP log levels, least severe first.
const LEVELS: [&str; 8] = [
    "debug",
    "info",
    "notice",
    "warning",
    "error",
    "critical",
    "alert",
    "emergency",
];
/// The index in LEVELS of the least severe message the client wants;
/// u8::MAX until it calls logging/setLevel.
type Level = std::sync::Arc<std::sync::atomic::AtomicU8>;

/// Reports on one running tool call: `notifications/progress` once a
/// second when the client sent a progress token. For `job.wait` the message
/// is the job's state and its latest log line, and the total is the wait;
/// and each new log line is a `notifications/message` at level info while
/// the client's level lets info through.
struct Follower {
    token: Option<Value>,
    level: Level,
    tool: String,
    /// The job a `job.wait` waits for, and how far its log has been read.
    job: Option<String>,
    offset: Option<u64>,
    partial: String,
    last_line: String,
    total: Option<u64>,
    ticks: u64,
}
impl Follower {
    fn new(method: &str, params: &Value, level: Level) -> Option<Self> {
        if method != "tools/call" {
            return None;
        }
        let token = Some(params["_meta"]["progressToken"].clone())
            .filter(|token| token.is_string() || token.is_i64() || token.is_u64());
        let tool = s(params, "name").to_string();
        let arguments = &params["arguments"];
        let job = (tool == "job.wait")
            .then(|| s(arguments, "job_id").to_string())
            .filter(|id| !id.is_empty());
        if token.is_none() && job.is_none() {
            return None;
        }
        let total = job
            .is_some()
            .then(|| arguments["seconds"].as_u64().unwrap_or(30));
        Some(Follower {
            token,
            level,
            tool,
            job,
            offset: None,
            partial: String::new(),
            last_line: String::new(),
            total,
            ticks: 0,
        })
    }
    /// The notifications for one more second of waiting.
    fn tick(&mut self, dispatch: impl Fn(&str, &Value) -> Result) -> Vec<Value> {
        self.ticks += 1;
        let mut notifications = Vec::new();
        let logging = self.level.load(std::sync::atomic::Ordering::Relaxed) <= 1;
        let mut message = format!("{} running for {} s", self.tool, self.ticks);
        if let Some(id) = self.job.clone().filter(|_| logging || self.token.is_some()) {
            if let Some((state, lines)) = self.read_log(&id, &dispatch) {
                if let Some(line) = lines.last() {
                    self.last_line = line.chars().take(200).collect();
                }
                message = format!("Job {state}");
                if !self.last_line.is_empty() {
                    message = format!("{message}: {}", self.last_line);
                }
                if logging {
                    notifications.extend(lines.into_iter().map(|line| {
                        json!({"jsonrpc":"2.0","method":"notifications/message",
                            "params":{"level":"info","logger":format!("job {id}"),"data":line}})
                    }));
                }
            }
        }
        if let Some(token) = &self.token {
            let mut progress =
                json!({"progressToken":token,"progress":self.ticks,"message":message});
            if let Some(total) = self.total {
                progress["total"] = json!(total.max(self.ticks));
            }
            notifications
                .push(json!({"jsonrpc":"2.0","method":"notifications/progress","params":progress}));
        }
        notifications
    }
    /// The job's state and the complete log lines written since the last
    /// tick. The first tick starts at the end of the log, so a repeated wait
    /// does not report the same lines again.
    fn read_log(
        &mut self,
        id: &str,
        dispatch: &impl Fn(&str, &Value) -> Result,
    ) -> Option<(String, Vec<String>)> {
        let offset = match self.offset {
            Some(offset) => offset,
            None => {
                let job = dispatch("job.get", &json!({"job_id":id})).ok()?;
                let size = fs::metadata(s(&job, "log_path")).map_or(0, |m| m.len());
                self.offset = Some(size);
                return Some((s(&job, "state").to_string(), Vec::new()));
            }
        };
        let logs = dispatch("job.logs", &json!({"job_id":id,"offset":offset})).ok()?;
        self.offset = logs["next_offset"].as_u64().or(Some(offset));
        self.partial.push_str(s(&logs, "text"));
        let mut lines = Vec::new();
        while let Some(end) = self.partial.find('\n') {
            let line: String = self.partial.drain(..=end).collect();
            let line = line.trim_end().to_string();
            if !line.is_empty() {
                lines.push(line);
            }
        }
        Some((s(&logs, "state").to_string(), lines))
    }
}

fn response(id: Value, method: &str, params: &Value) -> Value {
    match handle(method, params) {
        Ok(Some(value)) => json!({"jsonrpc":"2.0","id":id,"result":value}),
        Ok(None) => rpc_error(id, -32601, "Method not found"),
        Err(error) => rpc_error(id, -32602, &error.message),
    }
}

fn handle(method: &str, params: &Value) -> Result<Option<Value>> {
    Ok(Some(match method {
        "ping" => json!({}),
        "tools/list" => json!({"tools":api::manifest()}),
        "tools/call" => {
            let name = s(params, "name");
            let result =
                crate::session_log::call(name, params.get("arguments").unwrap_or(&json!({})));
            match result {
                Ok(value) => {
                    let mut content = vec![json!({"type":"text","text":value.to_string()})];
                    if [
                        "preview.capture",
                        "device.capture",
                        "runtime.capture",
                        "ios.virtual.screenshot",
                    ]
                    .contains(&name)
                    {
                        let path = Path::new(s(&value, "path"));
                        if fs::metadata(path).is_ok_and(|m| m.len() <= 4 * 1024 * 1024) {
                            let data = fs::read(path)?;
                            if data.starts_with(b"\x89PNG\r\n\x1a\n") {
                                content.push(json!({"type":"image","mimeType":"image/png","data":STANDARD.encode(data)}));
                            }
                        }
                    }
                    json!({"content":content,"structuredContent":structured(value),"isError":false})
                }
                Err(error) => {
                    let error = json!({"error":error.value()});
                    let mut reply = json!({"content":[{"type":"text","text":error.to_string()}],"isError":true});
                    // A declared outputSchema describes results, not errors.
                    if api::operation(name).is_ok_and(|op| op.get("outputSchema").is_none()) {
                        reply["structuredContent"] = error;
                    }
                    reply
                }
            }
        }
        "resources/list" => {
            let mut resources = vec![
                json!({"uri":"tomacco://capabilities","name":"What this computer can do","description":"The capabilities operation's result: Android, iOS and the virtual iPhone, each ready or with what it lacks.","mimeType":"application/json"}),
                json!({"uri":"tomacco://catalog","name":"Device catalogue","mimeType":"application/json"}),
                json!({"uri":"tomacco://jobs","name":"Recent jobs","mimeType":"application/json"}),
                json!({"uri":"tomacco://workspaces","name":"Saved development workspaces","mimeType":"application/json"}),
                json!({"uri":"tomacco://debug","name":"Native debug sessions","mimeType":"application/json"}),
                json!({"uri":"tomacco://runtimes","name":"Native display sessions","mimeType":"application/json"}),
                json!({"uri":"tomacco://devices","name":"Connected devices","description":"The device.list operation's result: Android and iOS devices, and why none are listed.","mimeType":"application/json"}),
            ];
            resources.extend(GUIDES.iter().map(|guide| {
                json!({"uri":format!("tomacco://docs/{}",guide.name),"name":guide.title,"description":guide.description,"mimeType":"text/markdown","size":guide.text.len()})
            }));
            json!({ "resources": resources })
        }
        "resources/read" => {
            let uri = s(params, "uri");
            if let Some(name) = uri.strip_prefix("tomacco://docs/") {
                let guide = GUIDES.iter().find(|g| g.name == name).ok_or_else(|| {
                    Error::new("resource_not_found", format!("Unknown guide: {name}"))
                })?;
                return Ok(Some(
                    json!({"contents":[{"uri":uri,"mimeType":"text/markdown","text":guide.text}]}),
                ));
            }
            if let Some(contents) = template(uri, api::dispatch)? {
                return Ok(Some(json!({ "contents": contents })));
            }
            let op = match uri {
                "tomacco://capabilities" => "capabilities",
                "tomacco://devices" => "device.list",
                "tomacco://catalog" => "catalog.list",
                "tomacco://jobs" => "job.list",
                "tomacco://workspaces" => "workspace.list",
                "tomacco://debug" => "debug.list",
                "tomacco://runtimes" => "runtime.list",
                _ => return Err(Error::new("resource_not_found", "Unknown resource")),
            };
            let value = api::dispatch(op, &json!({}))?;
            json!({"contents":[{"uri":uri,"mimeType":"application/json","text":value.to_string()}]})
        }
        "resources/templates/list" => {
            json!({"resourceTemplates":TEMPLATES.iter().map(|(uri, name, description, mime)| {
            json!({"uriTemplate":uri,"name":name,"description":description,"mimeType":mime})
        }).collect::<Vec<_>>()})
        }
        "completion/complete" => json!({ "completion": complete(params, api::dispatch) }),
        "prompts/list" => json!({"prompts":PROMPTS.iter().map(|p| {
            let arguments: Vec<Value> = p.arguments.iter().map(|(name, description)| {
                json!({"name":name,"description":description,"required":true})
            }).collect();
            json!({"name":p.name,"title":p.title,"description":p.description,"arguments":arguments})
        }).collect::<Vec<_>>()}),
        "prompts/get" => {
            let name = s(params, "name");
            let prompt = PROMPTS
                .iter()
                .find(|p| p.name == name)
                .ok_or_else(|| Error::new("prompt_not_found", format!("Unknown prompt: {name}")))?;
            let mut text = prompt.text.to_string();
            for (argument, _) in prompt.arguments {
                let value = params["arguments"][argument].as_str().unwrap_or_default();
                if value.is_empty() {
                    return Err(Error::new(
                        "invalid_argument",
                        format!("Prompt {name} needs the argument {argument}"),
                    ));
                }
                text = text.replace(&format!("{{{argument}}}"), value);
            }
            json!({"description":prompt.description,"messages":[{"role":"user","content":{"type":"text","text":text}}]})
        }
        _ => return Ok(None),
    }))
}

/// A result as MCP structured content, which must be an object: an object
/// result is itself, anything else is `{"result": value}`. The text content
/// keeps the result exactly as the CLI prints it.
fn structured(value: Value) -> Value {
    if value.is_object() {
        value
    } else {
        json!({ "result": value })
    }
}

/// Resources made from read-only operations, one per value of their
/// argument: (URI template, name, description, MIME type).
const TEMPLATES: &[(&str, &str, &str, &str)] = &[
    (
        "tomacco://jobs/{id}",
        "Job",
        "One job as job.get answers it: plan, state, phase, exit code and artifacts.",
        "application/json",
    ),
    (
        "tomacco://jobs/{id}/log",
        "Job log",
        "The last 64 KiB of a job's log, secrets masked; job.logs reads the rest.",
        "text/plain",
    ),
    (
        "tomacco://reference/{operation}",
        "Operation reference",
        "One operation as tomaccoctl help describes it, then its JSON Schema with annotations.",
        "text/plain",
    ),
];
/// How much of a job's log `tomacco://jobs/{id}/log` returns: its end.
const LOG_TAIL: u64 = 64 * 1024;

/// The contents of a resource that one of TEMPLATES matches, or None when
/// none does.
fn template(uri: &str, dispatch: impl Fn(&str, &Value) -> Result) -> Result<Option<Value>> {
    if let Some(name) = uri.strip_prefix("tomacco://reference/") {
        let schema = api::operation(name).map_err(|error| {
            Error::new("resource_not_found", error.message).remedy(error.remedy)
        })?;
        return Ok(Some(json!([
            {"uri":uri,"mimeType":"text/plain","text":crate::describe::operation(name)?},
            {"uri":uri,"mimeType":"application/json","text":schema.to_string()},
        ])));
    }
    let Some(rest) = uri.strip_prefix("tomacco://jobs/") else {
        return Ok(None);
    };
    let (id, log) = match rest.strip_suffix("/log") {
        Some(id) => (id, true),
        None => (rest, false),
    };
    if id.len() != 32
        || !id
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    {
        return Err(
            Error::new("resource_not_found", format!("Not a job ID: {id}")).remedy(
                "Job IDs are 32 lowercase hexadecimal characters; tomacco://jobs lists recent jobs",
            ),
        );
    }
    let job = dispatch("job.get", &json!({ "job_id": id }))?;
    if !log {
        return Ok(Some(
            json!([{"uri":uri,"mimeType":"application/json","text":job.to_string()}]),
        ));
    }
    let size = fs::metadata(s(&job, "log_path")).map_or(0, |m| m.len());
    let logs = dispatch(
        "job.logs",
        &json!({"job_id":id,"offset":size.saturating_sub(LOG_TAIL),"limit":LOG_TAIL}),
    )?;
    Ok(Some(
        json!([{"uri":uri,"mimeType":"text/plain","text":s(&logs, "text")}]),
    ))
}

/// Values for an argument of a resource template that start with what the
/// client has typed: operation names, or the IDs of recent jobs.
fn complete(params: &Value, dispatch: impl Fn(&str, &Value) -> Result) -> Value {
    let typed = s(&params["argument"], "value");
    let template = s(&params["ref"], "uri");
    let mut values: Vec<String> = match (s(&params["argument"], "name"), template) {
        ("operation", "tomacco://reference/{operation}") => api::names()
            .filter(|name| name.starts_with(typed))
            .map(str::to_string)
            .collect(),
        ("id", "tomacco://jobs/{id}" | "tomacco://jobs/{id}/log") => {
            dispatch("job.list", &json!({"limit":100}))
                .ok()
                .and_then(|jobs| jobs.as_array().cloned())
                .unwrap_or_default()
                .iter()
                .map(|job| s(job, "id").to_string())
                .filter(|id| id.starts_with(typed))
                .collect()
        }
        _ => Vec::new(),
    };
    let total = values.len();
    values.truncate(100);
    json!({"values":values,"total":total,"hasMore":total > 100})
}

/// A guide from docs/, compiled in so that it matches this version and an
/// agent can read it over MCP as `tomacco://docs/NAME`. Links between guides
/// stay relative to docs/.
struct Guide {
    name: &'static str,
    title: &'static str,
    description: &'static str,
    text: &'static str,
}
const GUIDES: &[Guide] = &[
    Guide {
        name: "agents",
        title: "Agent guide",
        description: "The operation contract, jobs, and each platform's calls.",
        text: include_str!("../docs/agents.md"),
    },
    Guide {
        name: "recipes",
        title: "Recipes",
        description: "Short sequences of real calls for common tasks.",
        text: include_str!("../docs/recipes.md"),
    },
    Guide {
        name: "getting-started",
        title: "Getting started",
        description: "Install, set up and the first app on each route.",
        text: include_str!("../docs/getting-started.md"),
    },
    Guide {
        name: "android",
        title: "Android",
        description: "Android and Swift on Android: SDK, emulators, builds, tests.",
        text: include_str!("../docs/android.md"),
    },
    Guide {
        name: "iphone",
        title: "iPhone",
        description: "A physical iPhone over USB, Xcode projects and the Mac route.",
        text: include_str!("../docs/iphone.md"),
    },
    Guide {
        name: "virtual-ios",
        title: "Virtual iOS device: usage",
        description: "Start, stop, deploy, touch and screenshots on the virtual iPhone.",
        text: include_str!("../docs/virtual-ios/usage.md"),
    },
    Guide {
        name: "troubleshooting",
        title: "Troubleshooting",
        description: "Known errors and what to do about them.",
        text: include_str!("../docs/troubleshooting.md"),
    },
    Guide {
        name: "faq",
        title: "FAQ",
        description: "Short answers to common questions.",
        text: include_str!("../docs/faq.md"),
    },
];

/// A recipe a client offers its user, such as a slash command. Each names
/// only real operations; `{argument}` in the text is replaced on prompts/get.
struct Prompt {
    name: &'static str,
    title: &'static str,
    description: &'static str,
    arguments: &'static [(&'static str, &'static str)],
    text: &'static str,
}
const PROMPTS: &[Prompt] = &[
    Prompt {
        name: "setup",
        title: "Set up this machine",
        description: "Check this machine and set up what Tomacco still needs, asking before anything that costs a download, a license or a password.",
        arguments: &[],
        text: "Set up Tomacco on this machine.

1. Call `doctor` and read `next_steps`. They are in order.
2. Work through them one at a time. A step with `ask_first: true` costs me a download, a license or a password: tell me what it costs and wait for my yes before you do it. Only I can sign in to an Apple account.
3. After each step, call `doctor` again to see that it took.
4. The virtual iOS device is optional and large. Set it up only if I say I want it.

Finish with a short list of what works now and what is still missing.",
    },
    Prompt {
        name: "run-android-app",
        title: "Run my Android app",
        description: "Build an Android project, run it on a device or emulator, and show the screen.",
        arguments: &[("path", "Absolute path of the project")],
        text: "Run the Android app in {path}.

1. Call `project.inspect` with path {path} and check that `android` is among its platforms. If it is not, stop and tell me why.
2. Call `device.list` with platform `android`. If no device is listed, call `android.avds`, start one with `android.avd_start`, and call `device.list` again until it appears. If there is no emulator at all, call `doctor` and tell me its `next_steps`.
3. Call `project.run` with path {path}, platform `android` and the device's id. It returns a job: call `job.wait` with its id until the state is `succeeded`, `failed`, `cancelled` or `timed_out`. Queued or running is not success. If it did not succeed, read `job.logs` and tell me what went wrong.
4. Call `device.capture` with the platform, the device and an absolute path for the picture, and tell me what the app shows.",
    },
    Prompt {
        name: "run-virtual-iphone",
        title: "Run my app on the virtual iPhone",
        description: "Build an xtool SwiftUI project and open it on Tomacco's virtual iOS device.",
        arguments: &[("path", "Absolute path of the xtool project")],
        text: "Run the iOS app in {path} on the virtual iPhone.

1. Call `ios.virtual.status`. If `configured` is not true, the virtual device is not set up: stop and tell me, and point me to `doctor`'s `next_steps`.
2. Call `project.app` with path {path}. If it answers `app_not_built`, call `project.execute` with path {path} and platform `ios`, follow the job with `job.wait` until it ends, and call `project.app` again. If `virtual_device.accepted` is false, tell me its error and stop.
3. Call `ios.virtual.deploy` with `bundle` set to the `app` path. If the phone is running, this installs and opens the app without a reboot. Follow its job with `job.wait` and `job.logs`.
4. If deploy answers `ios_virtual_not_running`, make the calls in `project.app`'s `next` instead: `ios.virtual.configure` with `add_application`, then with `application`, then `ios.virtual.start`. The start takes many minutes; follow it with `job.logs` and `ios.virtual.status`.
5. Read `ios.virtual.status`: `application_launch_accepted` and `display_visible`. Look at the screen with `ios.emulator.control`, `action` `frame`, on the `guest_job` it reports, and tell me what the app shows. A launch that was accepted is not yet proof that the app is visible.",
    },
];
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn transport_recovers_from_invalid_messages() {
        let input=b"garbage\n{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}\n{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-06-18\"}}\n{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/list\"}\n";
        let mut output = Vec::new();
        serve(&input[..], &mut output).unwrap();
        let values: Vec<Value> = String::from_utf8(output)
            .unwrap()
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        assert_eq!(values[0]["error"]["code"], -32700);
        assert_eq!(values[1]["error"]["code"], -32000);
        assert_eq!(values[2]["result"]["protocolVersion"], "2025-06-18");
        assert_eq!(
            values[3]["result"]["tools"].as_array().unwrap().len(),
            crate::api::manifest().as_array().unwrap().len()
        );
    }
    #[test]
    fn results_and_errors_come_as_structured_content_too() {
        assert_eq!(structured(json!({"id":"a"})), json!({"id":"a"}));
        assert_eq!(structured(json!([1, 2])), json!({"result":[1,2]}));
        assert_eq!(structured(json!("text")), json!({"result":"text"}));
        let reply = handle(
            "tools/call",
            &json!({"name":"device.install","arguments":{}}),
        )
        .unwrap()
        .unwrap();
        assert_eq!(reply["isError"], true);
        assert_eq!(
            reply["structuredContent"]["error"]["code"],
            "invalid_argument"
        );
        let text: Value = serde_json::from_str(s(&reply["content"][0], "text")).unwrap();
        assert_eq!(text, reply["structuredContent"]);
    }
    #[test]
    fn a_progress_token_gets_progress_and_a_job_wait_its_state_and_log() {
        let call = |arguments: Value, meta: Value| json!({"name":"job.wait","arguments":arguments,"_meta":meta});
        let id = "0123456789abcdef0123456789abcdef";
        let off = || -> Level { std::sync::Arc::new(u8::MAX.into()) };
        assert!(
            Follower::new(
                "tools/call",
                &json!({"name":"job.get","arguments":{"job_id":id}}),
                off()
            )
            .is_none()
        );
        assert!(
            Follower::new(
                "resources/read",
                &json!({"_meta":{"progressToken":1}}),
                off()
            )
            .is_none()
        );
        // Any other tool reports how long it has run.
        let mut other = Follower::new(
            "tools/call",
            &json!({"name":"device.capture","_meta":{"progressToken":"t"}}),
            off(),
        )
        .unwrap();
        let first = other.tick(|_, _| unreachable!());
        assert_eq!(first[0]["method"], "notifications/progress");
        assert_eq!(first[0]["params"]["progressToken"], "t");
        assert_eq!(first[0]["params"]["progress"], 1);
        assert!(first[0]["params"].get("total").is_none());
        assert_eq!(
            other.tick(|_, _| unreachable!())[0]["params"]["progress"],
            2
        );

        let directory = tempfile::tempdir().unwrap();
        let log = directory.path().join("job.log");
        fs::write(&log, "old line\n").unwrap();
        let path = log.to_string_lossy().into_owned();
        let pieces = std::cell::RefCell::new(vec!["building\nlin", "king\n"]);
        let dispatch = |operation: &str, arguments: &Value| -> Result {
            assert_eq!(arguments["job_id"], id);
            match operation {
                "job.get" => Ok(json!({"state":"running","log_path":path})),
                "job.logs" => {
                    assert_eq!(arguments["offset"], 9);
                    let text = pieces.borrow_mut().remove(0);
                    Ok(json!({"state":"running","text":text,"next_offset":9}))
                }
                _ => panic!("unexpected {operation}"),
            }
        };
        let mut wait = Follower::new(
            "tools/call",
            &call(
                json!({"job_id":id,"seconds":20}),
                json!({"progressToken":7}),
            ),
            off(),
        )
        .unwrap();
        let messages: Vec<Value> = (0..3)
            .map(|_| wait.tick(dispatch)[0]["params"].clone())
            .collect();
        // The log is read from where it ended when the wait began.
        assert_eq!(messages[0]["message"], "Job running");
        assert_eq!(messages[1]["message"], "Job running: building");
        assert_eq!(messages[2]["message"], "Job running: linking");
        assert_eq!(messages[2]["progress"], 3);
        assert_eq!(messages[2]["total"], 20);
        assert_eq!(messages[2]["progressToken"], 7);
    }
    #[test]
    fn job_log_lines_are_log_messages_once_the_client_asks_for_info() {
        let id = "0123456789abcdef0123456789abcdef";
        let directory = tempfile::tempdir().unwrap();
        let log = directory.path().join("job.log");
        fs::write(&log, "").unwrap();
        let path = log.to_string_lossy().into_owned();
        let reads = std::cell::Cell::new(0);
        let dispatch = |operation: &str, _: &Value| -> Result {
            reads.set(reads.get() + 1);
            match operation {
                "job.get" => Ok(json!({"state":"running","log_path":path})),
                "job.logs" => Ok(json!({"state":"running","text":"one\ntwo\n","next_offset":8})),
                _ => panic!("unexpected {operation}"),
            }
        };
        let level: Level = std::sync::Arc::new(u8::MAX.into());
        let params = json!({"name":"job.wait","arguments":{"job_id":id}});
        let mut wait = Follower::new("tools/call", &params, level.clone()).unwrap();
        // Before logging/setLevel, and at warning, nothing is read or sent.
        assert!(wait.tick(dispatch).is_empty());
        level.store(3, std::sync::atomic::Ordering::Relaxed);
        assert!(wait.tick(dispatch).is_empty());
        assert_eq!(reads.get(), 0);
        level.store(1, std::sync::atomic::Ordering::Relaxed);
        assert!(wait.tick(dispatch).is_empty());
        let sent = wait.tick(dispatch);
        assert_eq!(sent.len(), 2);
        assert_eq!(sent[0]["method"], "notifications/message");
        assert_eq!(sent[0]["params"]["level"], "info");
        assert_eq!(sent[0]["params"]["logger"], format!("job {id}"));
        assert_eq!(sent[1]["params"]["data"], "two");

        let input = b"{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{}}\n{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"logging/setLevel\",\"params\":{\"level\":\"loud\"}}\n{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"logging/setLevel\",\"params\":{\"level\":\"debug\"}}\n";
        let mut output = Vec::new();
        serve(&input[..], &mut output).unwrap();
        let values: Vec<Value> = String::from_utf8(output)
            .unwrap()
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        assert_eq!(values[0]["result"]["capabilities"]["logging"], json!({}));
        assert_eq!(values[1]["error"]["code"], -32602);
        assert_eq!(values[2]["result"], json!({}));
    }
    #[test]
    fn guides_are_listed_and_read_as_markdown() {
        let list = handle("resources/list", &json!({})).unwrap().unwrap();
        let uris: Vec<&str> = list["resources"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| s(r, "uri"))
            .collect();
        assert!(uris.contains(&"tomacco://jobs"));
        assert!(uris.contains(&"tomacco://capabilities"));
        assert!(uris.contains(&"tomacco://docs/recipes"));
        assert!(uris.contains(&"tomacco://docs/agents"));
        let read = handle("resources/read", &json!({"uri":"tomacco://docs/recipes"}))
            .unwrap()
            .unwrap();
        assert_eq!(read["contents"][0]["mimeType"], "text/markdown");
        assert!(s(&read["contents"][0], "text").starts_with("# Recipes"));
        let unknown = handle("resources/read", &json!({"uri":"tomacco://docs/../legal"}));
        assert_eq!(unknown.unwrap_err().code, "resource_not_found");
        // Every guide named in the agent guide's resource list is served.
        let agents = include_str!("../docs/agents.md");
        for guide in GUIDES {
            assert!(
                agents.contains(&format!("tomacco://docs/{}", guide.name)),
                "docs/agents.md does not list {}",
                guide.name
            );
        }
    }
    #[test]
    fn templates_mirror_read_only_operations() {
        let list = handle("resources/templates/list", &json!({}))
            .unwrap()
            .unwrap();
        let uris: Vec<&str> = list["resourceTemplates"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| s(r, "uriTemplate"))
            .collect();
        assert_eq!(
            uris,
            [
                "tomacco://jobs/{id}",
                "tomacco://jobs/{id}/log",
                "tomacco://reference/{operation}"
            ]
        );
        let list = handle("resources/list", &json!({})).unwrap().unwrap();
        assert!(
            list["resources"]
                .as_array()
                .unwrap()
                .iter()
                .any(|r| r["uri"] == "tomacco://devices")
        );

        let reference = template("tomacco://reference/job.wait", |_, _| unreachable!())
            .unwrap()
            .unwrap();
        assert!(s(&reference[0], "text").starts_with("job.wait - Wait for job"));
        let schema: Value = serde_json::from_str(s(&reference[1], "text")).unwrap();
        assert_eq!(schema["annotations"]["readOnlyHint"], true);
        let unknown = template("tomacco://reference/job.wiat", |_, _| unreachable!()).unwrap_err();
        assert_eq!(unknown.code, "resource_not_found");
        assert!(unknown.remedy.starts_with("Did you mean job.wait?"));
        assert!(
            template("tomacco://jobs", |_, _| unreachable!())
                .unwrap()
                .is_none()
        );
        assert!(
            template("tomacco://catalog", |_, _| unreachable!())
                .unwrap()
                .is_none()
        );
        assert_eq!(
            template("tomacco://jobs/../x", |_, _| unreachable!())
                .unwrap_err()
                .code,
            "resource_not_found"
        );

        let id = "0123456789abcdef0123456789abcdef";
        let directory = tempfile::tempdir().unwrap();
        let log = directory.path().join("job.log");
        fs::write(&log, vec![b'x'; 70 * 1024]).unwrap();
        let path = log.to_string_lossy().into_owned();
        let dispatch = |operation: &str, arguments: &Value| -> Result {
            assert_eq!(arguments["job_id"], id);
            match operation {
                "job.get" => Ok(json!({"id":id,"state":"running","log_path":path})),
                "job.logs" => {
                    assert_eq!(arguments["offset"], 6 * 1024);
                    assert_eq!(arguments["limit"], 64 * 1024);
                    Ok(json!({"text":"the end\n"}))
                }
                _ => panic!("unexpected {operation}"),
            }
        };
        let job = template(&format!("tomacco://jobs/{id}"), dispatch)
            .unwrap()
            .unwrap();
        assert_eq!(job[0]["mimeType"], "application/json");
        assert!(s(&job[0], "text").contains("\"running\""));
        let tail = template(&format!("tomacco://jobs/{id}/log"), dispatch)
            .unwrap()
            .unwrap();
        assert_eq!(tail[0]["mimeType"], "text/plain");
        assert_eq!(tail[0]["text"], "the end\n");
    }
    #[test]
    fn template_arguments_are_completed() {
        let jobs = |operation: &str, _: &Value| -> Result {
            assert_eq!(operation, "job.list");
            Ok(json!([{"id":"aa11"},{"id":"ab22"},{"id":"bb33"}]))
        };
        let ask = |uri: &str, name: &str, value: &str| {
            complete(
                &json!({"ref":{"type":"ref/resource","uri":uri},"argument":{"name":name,"value":value}}),
                jobs,
            )
        };
        let done = ask("tomacco://reference/{operation}", "operation", "job.wa");
        assert_eq!(done["values"], json!(["job.wait"]));
        assert_eq!(done["hasMore"], false);
        let done = ask("tomacco://jobs/{id}/log", "id", "a");
        assert_eq!(done["values"], json!(["aa11", "ab22"]));
        assert_eq!(
            ask("tomacco://reference/{operation}", "operation", "")["total"],
            api::names().count()
        );
        assert_eq!(
            ask("tomacco://reference/{operation}", "operation", "")["hasMore"],
            true
        );
        assert_eq!(ask("tomacco://nothing/{x}", "x", "")["values"], json!([]));
    }
    #[test]
    fn prompts_are_listed_and_filled_in() {
        let list = handle("prompts/list", &json!({})).unwrap().unwrap();
        let names: Vec<&str> = list["prompts"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| s(p, "name"))
            .collect();
        assert_eq!(names, ["setup", "run-android-app", "run-virtual-iphone"]);
        assert_eq!(list["prompts"][1]["arguments"][0]["name"], "path");
        assert_eq!(list["prompts"][1]["arguments"][0]["required"], true);
        let got = handle(
            "prompts/get",
            &json!({"name":"run-android-app","arguments":{"path":"/work/App"}}),
        )
        .unwrap()
        .unwrap();
        assert_eq!(got["messages"][0]["role"], "user");
        let text = s(&got["messages"][0]["content"], "text");
        assert!(text.contains("path /work/App"));
        assert!(!text.contains("{path}"));
        let missing = handle("prompts/get", &json!({"name":"run-android-app"})).unwrap_err();
        assert_eq!(missing.code, "invalid_argument");
        assert!(handle("prompts/get", &json!({"name":"nothing"})).is_err());
        assert!(handle("prompts/get", &json!({"name":"setup"})).is_ok());
    }
    #[test]
    fn prompts_name_only_operations_that_exist() {
        // A dotted name in backticks whose first part is an operation's,
        // such as `job.wait`; field paths like `virtual_device.accepted` are not.
        let pattern = regex::Regex::new(r"`([a-z_]+)((?:\.[a-z_]+)+)`").unwrap();
        let areas: std::collections::HashSet<&str> = crate::api::names()
            .filter_map(|name| name.split_once('.').map(|(area, _)| area))
            .collect();
        for prompt in PROMPTS {
            let mut found = 0;
            for name in pattern
                .captures_iter(prompt.text)
                .filter(|c| areas.contains(&c[1]))
                .map(|c| format!("{}{}", &c[1], &c[2]))
            {
                assert!(
                    crate::api::operation(&name).is_ok(),
                    "{} names {name}",
                    prompt.name
                );
                found += 1;
            }
            assert!(found > 0 || prompt.text.contains("`doctor`"));
        }
    }
}
