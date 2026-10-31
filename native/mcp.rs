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
                    &json!({"jsonrpc":"2.0","id":id,"result":{"protocolVersion":if PROTOCOLS.contains(&protocol){protocol}else{PROTOCOLS[0]},"capabilities":{"tools":{},"resources":{}},"serverInfo":{"name":"tomacco","version":VERSION},"instructions":"To set Tomacco up for a person, call doctor and work through next_steps in order, asking first where ask_first is true. Start with workspace.list/context for saved project/device recipes. Use files.read/write with SHA-256 revisions to avoid overwriting concurrent edits. Use project.plan before builds, poll job.get/logs until terminal status. Native UI tests use testing.run, source debugging uses debug.*, profiling uses profile.*, releases use release.*. A queued job is not a successful build. Requests may run concurrently. Cancelling a wait does not cancel the durable job; use job.cancel explicitly. Inspect doctor and device.list before native operations. Preview is Chromium, not an iOS runtime."}}),
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
        _ => return Ok(None),
    }))
}
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
}
