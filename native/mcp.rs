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
                    &json!({"jsonrpc":"2.0","id":id,"result":{"protocolVersion":if PROTOCOLS.contains(&protocol){protocol}else{PROTOCOLS[0]},"capabilities":{"tools":{},"resources":{},"prompts":{}},"serverInfo":{"name":"tomacco","version":VERSION},"instructions":"To set Tomacco up for a person, call doctor and work through next_steps in order, asking first where ask_first is true. Start with workspace.list/context for saved project/device recipes. Use files.read/write with SHA-256 revisions to avoid overwriting concurrent edits. Use project.plan before builds, poll job.get/logs until terminal status. Native UI tests use testing.run, source debugging uses debug.*, profiling uses profile.*, releases use release.*. A queued job is not a successful build. Requests may run concurrently. Cancelling a wait does not cancel the durable job; use job.cancel explicitly. Inspect doctor and device.list before native operations. Preview is Chromium, not an iOS runtime."}}),
                )?;
                continue;
            }
            if method != "ping" && !initialized {
                respond(&sink, &rpc_error(id, -32000, "Initialize first"))?;
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
            scope.spawn(move || {
                let result =
                    with_cancellation(Some(token.clone()), || response(id, &method, &params));
                if !token.load(Ordering::Relaxed) {
                    let _ = respond(sink, &result);
                }
                active.lock().unwrap().remove(&key);
            });
        }
    })
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
            let result = api::dispatch(name, params.get("arguments").unwrap_or(&json!({})));
            match result {
                Ok(value) => {
                    let mut content = vec![json!({"type":"text","text":value.to_string()})];
                    if ["preview.capture", "device.capture", "runtime.capture"].contains(&name) {
                        let path = Path::new(s(&value, "path"));
                        if fs::metadata(path).is_ok_and(|m| m.len() <= 4 * 1024 * 1024) {
                            let data = fs::read(path)?;
                            if data.starts_with(b"\x89PNG\r\n\x1a\n") {
                                content.push(json!({"type":"image","mimeType":"image/png","data":STANDARD.encode(data)}));
                            }
                        }
                    }
                    json!({"content":content,"isError":false})
                }
                Err(error) => {
                    json!({"content":[{"type":"text","text":json!({"error":error.value()}).to_string()}],"isError":true})
                }
            }
        }
        "resources/list" => {
            json!({"resources":[{"uri":"tomacco://catalog","name":"Device catalogue","mimeType":"application/json"},{"uri":"tomacco://jobs","name":"Recent jobs","mimeType":"application/json"},{"uri":"tomacco://workspaces","name":"Saved development workspaces","mimeType":"application/json"},{"uri":"tomacco://debug","name":"Native debug sessions","mimeType":"application/json"},{"uri":"tomacco://runtimes","name":"Native display sessions","mimeType":"application/json"}]})
        }
        "resources/read" => {
            let uri = s(params, "uri");
            let op = match uri {
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
