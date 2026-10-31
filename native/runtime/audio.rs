//! Session-owned Android output playback through the installed scrcpy client.
//! Capture is output-only: this path never selects a microphone source.
use super::*;
use crate::jobs;

fn record_path(id: &str) -> Result<PathBuf> {
    Ok(directory(id)?.join("audio.json"))
}

pub fn state(id: &str) -> Result {
    let record = read_json(&record_path(id)?, Some(json!({})))?;
    if s(&record, "job_id").is_empty() {
        return Ok(
            json!({"enabled":false,"status":"off","configured":record["enabled"].is_boolean()}),
        );
    }
    let job = jobs::get(s(&record, "job_id"))?;
    let active = !jobs::TERMINAL.contains(&s(&job, "state"));
    let mut result = json!({"enabled":active && b(&record,"enabled"),"configured":true,"status":if active && !b(&record,"enabled") { "stopping" } else { s(&job,"state") },"job_id":job["id"],"device":record["device"],"source":"output","codec":"raw","buffer_ms":60});
    if s(&job, "state") == "failed" || s(&job, "state") == "timed_out" {
        result["error"] = json!("Android audio forwarding failed; inspect this audio job's log");
        result["log_path"] = job["log_path"].clone();
    }
    Ok(result)
}

pub fn handle(a: &Value) -> Result {
    let id = s(a, "session");
    let runtime = metadata(id)?;
    if s(&runtime, "platform") != "android" {
        return Err(Error::new(
            "audio_unsupported",
            "Output forwarding is currently available for Android 11 and newer",
        ));
    }
    if s(a, "action") == "status" {
        return state(id);
    }
    let lock = Lock::open(&home()?.join("locks").join(format!(
        "runtime-audio-{}.lock",
        digest(s(&runtime, "device").as_bytes())
    )))?;
    lock.acquire(false)?;
    if s(a, "action") == "stop" {
        return stop_locked(id);
    }
    let live = request(id, "state", &json!({}))?;
    if s(&live, "status") != "connected" {
        return Err(Error::new(
            "runtime_unavailable",
            "Connect the Android runtime before enabling audio",
        ));
    }
    let current = state(id)?;
    if b(&current, "enabled") {
        let mut current = current;
        current["reused"] = json!(true);
        return Ok(current);
    }
    // A stop request is asynchronous. Wait for its process group to finish
    // before admitting the next capture, otherwise rapid toggles overlap.
    if s(&current, "status") == "stopping"
        && !jobs::TERMINAL.contains(&s(&jobs::wait(s(&current, "job_id"), 5)?, "state"))
    {
        return Err(Error::new(
            "audio_stopping",
            "The previous audio stream is still stopping; retry shortly",
        ));
    }
    let client = tc::find("scrcpy")?;
    let spec = read_json(&directory(id)?.join("spec.json"), None)?;
    let server = android::server_path(&spec)?;
    let mut env = serde_json::to_value(tc::environment()?)?;
    env["ADB"] = json!(tc::find("adb")?);
    env["SCRCPY_SERVER_PATH"] = json!(server);
    let device = s(&runtime, "device");
    let job = jobs::submit(
        &format!("Android audio · {device}"),
        json!({
            "steps":[{"argv":[client,"--serial",device,"--no-video","--no-control","--no-window","--audio-source=output","--audio-codec=raw","--audio-buffer=60","--require-audio"],"phase":"streaming"}],
            "cwd":home()?,"timeout":0,"env":env,"exclusive":format!("runtime-audio:{device}"),"single_active":true,
            "operation_key":{"runtime_audio":id},
            "runtime_owner":{"pid":runtime["pid"],"token":runtime["pid_token"]}
        }),
    )?;
    if let Err(error) = atomic_json(
        &record_path(id)?,
        &json!({"job_id":job["id"],"enabled":true,"device":device}),
    ) {
        if !b(&job, "reused") {
            let _ = jobs::cancel(s(&job, "id"));
        }
        return Err(error);
    }
    let mut result = state(id)?;
    result["reused"] = json!(b(&job, "reused"));
    Ok(result)
}

fn stop_locked(id: &str) -> Result {
    let mut record = read_json(&record_path(id)?, Some(json!({})))?;
    // Status polling also reaps disconnected sessions. Already-stopped audio
    // must not rewrite and fsync its record on every runtime.list request.
    if record["enabled"] == false {
        return state(id);
    }
    if !s(&record, "job_id").is_empty() {
        let job = jobs::get(s(&record, "job_id"))?;
        if !jobs::TERMINAL.contains(&s(&job, "state")) {
            jobs::cancel(s(&job, "id"))?;
        }
    }
    record["enabled"] = json!(false);
    atomic_json(&record_path(id)?, &record)?;
    state(id)
}

pub fn stop(id: &str) -> Result {
    if !record_path(id)?.is_file() {
        return Ok(json!({"enabled":false,"status":"off"}));
    }
    handle(&json!({"session":id,"action":"stop"}))
}

pub fn attach(mut value: Value, a: &Value) -> Result {
    if s(&value, "platform") == "android" {
        let action = if b(a, "audio") { "start" } else { "stop" };
        value["audio"] =
            handle(&json!({"session":value["session"],"action":action})).unwrap_or_else(failure);
    }
    Ok(value)
}

pub fn failure(error: Error) -> Value {
    json!({"enabled":false,"status":"failed","error":error.message})
}
