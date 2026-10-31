use crate::{catalog, contact_sheet, core::*, visual};
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::unix::{
        fs::{FileTypeExt, MetadataExt},
        net::UnixStream,
    },
    path::PathBuf,
    thread,
    time::{Duration, Instant},
};

pub fn socket_directory() -> Result<PathBuf> {
    let data = home()?;
    let mut directory = data.join("runtime");
    if directory.as_os_str().len() + 54 >= 104 {
        directory = PathBuf::from(format!(
            "/tmp/tomacco-{}-{}",
            unsafe { libc::getuid() },
            &digest(data.as_os_str().as_encoded_bytes())[..16]
        ));
    }
    if let Ok(meta) = fs::symlink_metadata(&directory) {
        // The desktop creates it owner-only. One that others may write could
        // hold a socket they swapped in, so it is not trusted.
        if meta.file_type().is_symlink()
            || !meta.is_dir()
            || meta.uid() != unsafe { libc::getuid() }
            || meta.mode() & 0o022 != 0
        {
            return Err(Error::new(
                "invalid_session",
                "Preview runtime directory is not owned by the current user",
            ));
        }
    }
    Ok(directory)
}
pub fn call(method: &str, params: Value, session: &str, timeout: u64) -> Result {
    identifier(session, "session", Some("[A-Za-z0-9_-]{1,40}"))?;
    let path = socket_directory()?.join(format!("preview-{session}.sock"));
    let invoke = || -> Result {
        let meta = fs::symlink_metadata(&path)?;
        if meta.uid() != unsafe { libc::getuid() } || !meta.file_type().is_socket() {
            return Err(Error::new(
                "invalid_session",
                "Preview socket is not owned by the current user",
            ));
        }
        let mut client = UnixStream::connect(&path)?;
        client.set_read_timeout(Some(Duration::from_secs(timeout.max(1))))?;
        client.set_write_timeout(Some(Duration::from_secs(timeout.max(1))))?;
        writeln!(
            client,
            "{}",
            json!({"id":1,"method":method,"params":params})
        )?;
        let mut bytes = Vec::new();
        BufReader::new(client.take(8 * 1024 * 1024 + 1)).read_until(b'\n', &mut bytes)?;
        if bytes.len() > 8 * 1024 * 1024 {
            return Err(Error::new(
                "response_too_large",
                "Preview result exceeds 8 MiB",
            ));
        }
        if bytes.last() != Some(&b'\n') {
            return Err(Error::new(
                "preview_disconnected",
                "Preview closed before replying",
            ));
        }
        let value: Value = serde_json::from_slice(&bytes)?;
        if let Some(error) = value.get("error") {
            return Err(Error::new(
                error["code"].as_str().unwrap_or("preview_error"),
                error["message"]
                    .as_str()
                    .unwrap_or("Preview operation failed"),
            ));
        }
        Ok(value["result"].clone())
    };
    invoke().map_err(|e| {
        if ["io_error", "invalid_json"].contains(&e.code.as_str()) {
            Error::new(
                "preview_unavailable",
                format!("Cannot reach preview session {session}: {e}"),
            )
            .remedy(format!("Start tomacco --preview-session {session}"))
        } else {
            e
        }
    })
}
pub fn sessions() -> Result {
    let mut values = Vec::new();
    for path in glob_paths(&format!(
        "{}/preview-*.sock",
        glob::Pattern::escape(&socket_directory()?.to_string_lossy())
    ))? {
        let name = path
            .file_stem()
            .unwrap_or_default()
            .to_string_lossy()
            .trim_start_matches("preview-")
            .to_string();
        if let Ok(mut state) = call("state", json!({}), &name, 2) {
            state["session"] = json!(name);
            values.push(state);
        }
    }
    Ok(json!(values))
}
pub fn ready(session: &str, timeout: u64, wait_for: &str) -> Result {
    let deadline = Instant::now() + Duration::from_secs(timeout);
    thread::sleep(Duration::from_millis(250));
    while Instant::now() < deadline {
        let remaining = deadline
            .saturating_duration_since(Instant::now())
            .as_secs()
            .max(1);
        let state = call("state", json!({}), session, remaining)?;
        if !s(&state, "error").is_empty() {
            return Err(Error::new("page_load_failed", s(&state, "error")));
        }
        if !b(&state, "loading") {
            let value = call(
                "evaluate",
                json!({"script":"document.readyState==='complete' && (!document.fonts || document.fonts.status==='loaded') && (!window._flutter || window.__tomaccoFirstFrame===true)"}),
                session,
                remaining,
            )?;
            if value == true
                && (wait_for.is_empty()
                    || call(
                        "evaluate",
                        json!({"script":format!("Boolean({wait_for})")}),
                        session,
                        remaining,
                    )? == true)
            {
                thread::sleep(Duration::from_millis(300));
                return Ok(state);
            }
        }
        thread::sleep(Duration::from_millis(100));
    }
    Err(Error::new("page_timeout", "Page did not become ready")
        .remedy("Check preview.console; pass a readiness predicate for asynchronous content"))
}
pub fn handle(op: &str, a: &Value) -> Result {
    let session = a["session"].as_str().unwrap_or("main");
    match op {
        "preview.sessions" => sessions(),
        "preview.ready" => ready(session, n(a, "timeout"), s(a, "wait_for")),
        "preview.audit" => call(
            "evaluate",
            json!({"script":include_str!("../assets/layout-audit.js")}),
            session,
            30,
        ),
        "preview.matrix" => matrix(a),
        _ => {
            let mut params = a.clone();
            params.as_object_mut().unwrap().remove("session");
            if op == "preview.capture" {
                let path = output_path(s(a, "path"))?;
                fs::create_dir_all(path.parent().unwrap())?;
                params["path"] = json!(path);
            }
            call(op.trim_start_matches("preview."), params, session, 30)
        }
    }
}
fn matrix(a: &Value) -> Result {
    let ids = strings(&a["device_ids"]);
    for id in &ids {
        catalog::find(id)?;
    }
    let output = path(s(a, "directory"), false, false)?;
    fs::create_dir_all(&output)?;
    let orientations = if a["orientations"].is_array() {
        strings(&a["orientations"])
    } else {
        argv(&["portrait", "landscape"])
    };
    let session = s(a, "session");
    let before = call("state", json!({}), session, 30)?;
    let result = (|| -> Result {
        if !s(a, "url").is_empty() {
            call("navigate", json!({"url":s(a,"url")}), session, 30)?;
            ready(session, 30, s(a, "wait_for"))?;
        }
        let mut results = Vec::new();
        for device in ids {
            let folds =
                if b(a, "folded_views") && catalog::find(&device)?.get("openWidth").is_some() {
                    vec![false, true]
                } else {
                    vec![false]
                };
            for unfolded in folds {
                for orientation in &orientations {
                    call(
                        "configure",
                        json!({"device":device,"orientation":orientation,"unfolded":unfolded}),
                        session,
                        30,
                    )?;
                    ready(session, 30, s(a, "wait_for"))?;
                    let label = format!(
                        "{}{}-{}",
                        device,
                        if unfolded { "-open" } else { "" },
                        orientation
                    );
                    let capture = call(
                        "capture",
                        json!({"path":output.join(format!("{label}.png"))}),
                        session,
                        30,
                    )?;
                    let audit = handle("preview.audit", &json!({"session":session}))?;
                    results.push(json!({"device":device,"orientation":orientation,"unfolded":unfolded,"capture":capture,"audit":audit}));
                }
            }
        }
        let mut value = json!({"results":&results,"created":now()});
        if a.get("contact_sheet") != Some(&json!(false)) {
            value["contact_sheet"] = sheet(&results, &output.join("contact-sheet.png"))?;
        }
        atomic_json(&output.join("matrix.json"), &value)?;
        value["manifest"] = json!(output.join("matrix.json"));
        Ok(value)
    })();
    let mut restore = json!({});
    for key in [
        "device",
        "orientation",
        "unfolded",
        "safe_area",
        "guides",
        "zoom",
        "status_bar",
        "chrome_theme",
    ] {
        if let Some(value) = before.get(key) {
            restore[key] = value.clone();
        }
    }
    let restored = call("configure", restore, session, 30);
    let navigation = if !s(a, "url").is_empty() && !s(&before, "url").is_empty() {
        call("navigate", json!({"url":before["url"]}), session, 30)
    } else {
        Ok(Value::Null)
    };
    // Preserve the original error; restoration failures are reported when the run succeeded.
    let result = result?;
    restored?;
    navigation?;
    Ok(result)
}

/// Every capture of a matrix in one labelled PNG.
fn sheet(results: &[Value], target: &std::path::Path) -> Result {
    let started = now();
    let mut cells = Vec::new();
    for result in results {
        let device = s(result, "device");
        let name = catalog::find(device)
            .ok()
            .and_then(|p| p["name"].as_str().map(String::from))
            .unwrap_or_else(|| device.to_string());
        let state = format!(
            "{}{}",
            if b(result, "unfolded") { "open " } else { "" },
            s(result, "orientation")
        );
        let image = visual::decode(&fs::read(s(&result["capture"], "path"))?)?;
        cells.push(contact_sheet::Cell {
            image,
            label: vec![name, state],
        });
    }
    let image = contact_sheet::compose(&cells);
    atomic_write(target, &visual::encode(&image)?)?;
    let mut value = artifact(target, started)?;
    value["width"] = json!(image.width);
    value["height"] = json!(image.height);
    Ok(value)
}
