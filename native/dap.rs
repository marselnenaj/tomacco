//! Debug Adapter Protocol transport for LLDB, including remote stdio adapters.
use crate::{core::*, toolchain};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, VecDeque},
    fs::File,
    io::{BufRead, BufReader, Write},
    os::unix::process::CommandExt,
    path::Path,
    process::{Child, ChildStdin, Command, Stdio},
    sync::mpsc::{self, Receiver},
    thread,
    time::{Duration, Instant},
};

pub struct Dap {
    child: Child,
    input: ChildStdin,
    rx: Receiver<Result<Value>>,
    sequence: u64,
    responses: BTreeMap<u64, Value>,
    pending: VecDeque<Value>,
    pub events: Vec<Value>,
    pub stopped: bool,
    pub terminated: bool,
    pub thread: String,
    pub capabilities: Value,
}
fn read_message(reader: &mut impl BufRead) -> Result<Value> {
    let mut length = None;
    let mut bytes = 0;
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line)? == 0 {
            return Err(Error::new(
                "debug_disconnected",
                "Debug adapter closed stdout",
            ));
        }
        bytes += line.len();
        if bytes > 8192 {
            return Err(Error::new("debug_protocol", "DAP header too large"));
        }
        if line == "\r\n" || line == "\n" {
            break;
        }
        if let Some((key, value)) = line.trim().split_once(':') {
            if key.eq_ignore_ascii_case("Content-Length") {
                length = Some(
                    value
                        .trim()
                        .parse::<usize>()
                        .map_err(|_| Error::new("debug_protocol", "Invalid DAP content length"))?,
                );
            }
        }
    }
    let length = length
        .filter(|n| *n <= 8 * 1024 * 1024)
        .ok_or_else(|| Error::new("debug_protocol", "Missing or oversized DAP content length"))?;
    let mut bytes = vec![0; length];
    reader.read_exact(&mut bytes)?;
    Ok(serde_json::from_slice(&bytes)?)
}
impl Dap {
    pub fn start(args: &[String], spec: &Value, log: &Path) -> Result<Self> {
        let mut command = Command::new(&args[0]);
        command
            .args(&args[1..])
            .envs(toolchain::environment()?)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::from(File::create(log)?))
            .process_group(0);
        if let Some(env) = spec["environment"].as_object() {
            for (k, v) in env {
                command.env(
                    k,
                    v.as_str().ok_or_else(|| {
                        Error::new(
                            "invalid_argument",
                            "Debug environment values must be strings",
                        )
                    })?,
                );
            }
        }
        toolchain::parent_bound(&mut command);
        let mut child = command.spawn()?;
        let input = child.stdin.take().unwrap();
        let stdout = child.stdout.take().unwrap();
        let (tx, rx) = mpsc::sync_channel(1024);
        thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            loop {
                let result = read_message(&mut reader);
                let failed = result.is_err();
                if tx.send(result).is_err() || failed {
                    break;
                }
            }
        });
        let mut dap = Self {
            child,
            input,
            rx,
            sequence: 0,
            responses: BTreeMap::new(),
            pending: VecDeque::new(),
            events: Vec::new(),
            stopped: false,
            terminated: false,
            thread: String::new(),
            capabilities: json!({}),
        };
        dap.capabilities=dap.request("initialize",json!({"clientID":"tomacco","clientName":"Tomacco","adapterID":"lldb","pathFormat":"path","linesStartAt1":true,"columnsStartAt1":true,"supportsVariableType":true,"supportsVariablePaging":true,"supportsRunInTerminalRequest":false}))?;
        let mut launch = json!({"name":"Tomacco","request":"launch","type":"lldb","program":spec["program"],"cwd":spec["cwd"],"args":spec["args"],"stopOnEntry":spec["stop_on_entry"].as_bool().unwrap_or(true),"disableASLR":false});
        if let Some(map) = spec.get("source_map") {
            launch["sourceMap"] = map.clone();
        }
        if let Some(env) = spec.get("environment") {
            launch["env"] = env.clone();
        }
        let method = if let Some(port) = spec["native_attach_port"].as_u64() {
            launch["request"] = json!("attach");
            launch["preRunCommands"] = spec["native_pre_run"].clone();
            launch["attachCommands"] = json!([format!("gdb-remote 127.0.0.1:{port}")]);
            "attach"
        } else if let Some(pid) = spec["pid"].as_u64() {
            launch["pid"] = json!(pid);
            launch["request"] = json!("attach");
            "attach"
        } else {
            "launch"
        };
        let launch_id = dap.begin(method, launch)?;
        let end = Instant::now() + Duration::from_secs(20);
        loop {
            dap.receive(Duration::from_millis(200))?;
            if let Some(reply) = dap.responses.get(&launch_id) {
                if !b(reply, "success") {
                    return Err(Error::new("debug_adapter_error", s(reply, "message"))
                        .details(reply.clone()));
                }
            }
            if dap.pending.iter().any(|v| s(v, "event") == "initialized") {
                break;
            }
            if Instant::now() >= end {
                return Err(Error::new(
                    "debug_timeout",
                    "Adapter did not initialize the target",
                ));
            }
        }
        dap.request("configurationDone", json!({}))?;
        dap.response(launch_id)?;
        dap.poll()?;
        Ok(dap)
    }
    fn write(&mut self, value: &Value) -> Result<()> {
        let text = serde_json::to_vec(value)?;
        write!(self.input, "Content-Length: {}\r\n\r\n", text.len())?;
        self.input.write_all(&text)?;
        self.input.flush()?;
        Ok(())
    }
    fn begin(&mut self, command: &str, arguments: Value) -> Result<u64> {
        self.sequence += 1;
        self.write(
            &json!({"seq":self.sequence,"type":"request","command":command,"arguments":arguments}),
        )?;
        Ok(self.sequence)
    }
    fn receive(&mut self, timeout: Duration) -> Result<()> {
        match self.rx.recv_timeout(timeout) {
            Ok(Ok(message)) => self.route(message),
            Ok(Err(e)) => {
                self.terminated = true;
                Err(e)
            }
            Err(mpsc::RecvTimeoutError::Timeout) => Ok(()),
            Err(e) => {
                self.terminated = true;
                Err(Error::new("debug_disconnected", e.to_string()))
            }
        }
    }
    fn route(&mut self, value: Value) -> Result<()> {
        match s(&value, "type") {
            "response" => {
                self.responses.insert(n(&value, "request_seq"), value);
            }
            "event" => {
                if self.pending.len() >= 1024 {
                    self.pending.pop_front();
                }
                self.pending.push_back(value);
            }
            "request" => {
                self.sequence += 1;
                self.write(&json!({"seq":self.sequence,"type":"response","request_seq":value["seq"],"command":value["command"],"success":false,"message":"Tomacco does not allow adapter-initiated shell execution"}))?;
            }
            _ => return Err(Error::new("debug_protocol", "Invalid DAP message type")),
        }
        Ok(())
    }
    fn response(&mut self, id: u64) -> Result {
        let end = Instant::now() + Duration::from_secs(20);
        loop {
            if let Some(reply) = self.responses.remove(&id) {
                if !b(&reply, "success") {
                    return Err(
                        Error::new("debug_adapter_error", s(&reply, "message")).details(reply)
                    );
                }
                return Ok(reply["body"].clone());
            }
            if Instant::now() >= end {
                return Err(Error::new(
                    "debug_timeout",
                    "Debug adapter request timed out",
                ));
            }
            self.receive(Duration::from_millis(200))?;
        }
    }
    pub fn request(&mut self, command: &str, arguments: Value) -> Result {
        let id = self.begin(command, arguments)?;
        self.response(id)
    }
    pub fn poll(&mut self) -> Result<()> {
        while let Ok(message) = self.rx.try_recv() {
            match message {
                Ok(value) => self.route(value)?,
                Err(e) => {
                    self.terminated = true;
                    self.events
                        .push(json!({"event":"terminated","error":e.value()}));
                    break;
                }
            }
        }
        while let Some(value) = self.pending.pop_front() {
            let event = s(&value, "event");
            if event == "stopped" {
                self.stopped = true;
                self.thread = value["body"]["threadId"].as_u64().unwrap_or(0).to_string();
            }
            if event == "continued" {
                self.stopped = false;
            }
            if event == "terminated" || event == "exited" {
                self.terminated = true;
            }
            self.events.push(value);
        }
        Ok(())
    }
    pub fn control(&mut self, action: &str, thread: &str) -> Result {
        let id = thread.parse::<u64>().unwrap_or(0);
        let arguments = if action == "disconnect" {
            json!({"terminateDebuggee":false})
        } else {
            json!({"threadId":id,"singleThread":false})
        };
        let result = self.request(action, arguments)?;
        if ["continue", "next", "stepIn", "stepOut"].contains(&action) {
            self.stopped = false;
        }
        if action == "disconnect" {
            self.terminated = true;
        }
        Ok(result)
    }
}
impl Drop for Dap {
    fn drop(&mut self) {
        toolchain::stop(&mut self.child);
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn framing_is_length_delimited_and_bounded() {
        let mut data = BufReader::new(&b"Content-Length: 16\r\n\r\n{\"type\":\"event\"}"[..]);
        assert_eq!(read_message(&mut data).unwrap()["type"], "event");
        assert!(
            read_message(&mut BufReader::new(
                &b"Content-Length: 99999999\r\n\r\n"[..]
            ))
            .is_err()
        );
    }
}
