//! Physical iPhone runtime on Linux: private USB forwards and WebDriverAgent.
//! WDA's MJPEG frames are decoded directly by Qt, without video re-encoding.
use super::*;
use base64::{Engine, engine::general_purpose::STANDARD};
use http_body_util::{BodyExt, Full};
use hyper::{
    Request,
    body::{Bytes, Incoming},
};
use hyper_util::rt::TokioIo;
use std::net::{Ipv4Addr, TcpListener};

const LIMIT: usize = 16 * 1024 * 1024;
const DEFAULT_RUNNER: &str = "com.facebook.WebDriverAgentRunner.xctrunner";

fn err(message: impl Into<String>) -> Error {
    Error::new("iphone_runtime", message)
}
/// A request that never reached WebDriverAgent: nothing on the phone changed.
fn not_sent(message: impl Into<String>) -> Error {
    err(message).details(json!({"delivery":"not_sent"}))
}
/// A request that may have reached WebDriverAgent, with no answer to show
/// what it did.
fn unanswered(message: impl Into<String>) -> Error {
    err(message).details(json!({"delivery":"unknown"}))
}
/// The hardware identifier the iPhone reports, such as `iPhone18,4`, or ""
/// when ideviceinfo is missing or the answer is not one.
fn product_type(device: &str) -> String {
    let Ok(tool) = tc::find("ideviceinfo") else {
        return String::new();
    };
    std::process::Command::new(tool)
        .args(["-u", device, "-k", "ProductType"])
        .stdin(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_owned())
        .filter(|v| valid_product_type(v))
        .unwrap_or_default()
}
pub(crate) fn valid_product_type(value: &str) -> bool {
    let Some(numbers) = value
        .strip_prefix("iPhone")
        .or_else(|| value.strip_prefix("iPad"))
    else {
        return false;
    };
    let mut parts = numbers.split(',');
    let ok = |p: Option<&str>| {
        p.is_some_and(|p| !p.is_empty() && p.len() <= 3 && p.bytes().all(|c| c.is_ascii_digit()))
    };
    ok(parts.next()) && ok(parts.next()) && parts.next().is_none()
}
/// The catalogue profile whose `productTypes` names this hardware identifier:
/// that proves the physical model, unlike the display size alone.
pub(crate) fn profile_for(catalogue: &Value, product_type: &str) -> Option<Value> {
    if product_type.is_empty() {
        return None;
    }
    catalogue
        .as_array()?
        .iter()
        .find(|p| {
            p["platform"] == "ios"
                && p["productTypes"]
                    .as_array()
                    .is_some_and(|t| t.iter().any(|v| v == product_type))
        })
        .cloned()
}
pub fn validate(a: &Value) -> Result<()> {
    identifier(s(a, "device"), "iPhone UDID", Some("[A-Fa-f0-9-]{16,64}"))?;
    tc::find("iproxy")?;
    if s(a, "runner_mode") != "external" {
        tc::find("go-ios")?;
    }
    if !s(a, "runner_bundle_id").is_empty() {
        identifier(
            s(a, "runner_bundle_id"),
            "runner bundle ID",
            Some(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+"),
        )?;
    }
    Ok(())
}

#[derive(Clone)]
struct Http {
    port: u16,
}
impl Http {
    async fn request(
        &self,
        method: &str,
        path: &str,
        body: Option<&Value>,
    ) -> Result<hyper::Response<Incoming>> {
        // Only our loopback USB forwarding endpoint is accepted; no arbitrary URL.
        let socket = tokio::time::timeout(
            Duration::from_secs(3),
            tokio::net::TcpStream::connect((Ipv4Addr::LOCALHOST, self.port)),
        )
        .await
        .map_err(|_| not_sent("USB connection timed out"))?
        .map_err(|e| not_sent(e.to_string()))?;
        let (mut sender, connection) = hyper::client::conn::http1::handshake(TokioIo::new(socket))
            .await
            .map_err(|e| not_sent(e.to_string()))?;
        tokio::spawn(async move {
            let _ = connection.await;
        });
        let request = Request::builder()
            .method(method)
            .uri(path)
            .header("Host", format!("127.0.0.1:{}", self.port))
            .header("Content-Type", "application/json")
            .header("Connection", "close")
            .body(Full::new(Bytes::from(
                body.map(Value::to_string).unwrap_or_default(),
            )))
            .map_err(|e| not_sent(e.to_string()))?;
        // From here on the request may have been carried out.
        tokio::time::timeout(Duration::from_secs(20), sender.send_request(request))
            .await
            .map_err(|_| unanswered("WebDriverAgent did not respond"))?
            .map_err(|e| unanswered(e.to_string()))
    }
    async fn json(&self, method: &str, path: &str, body: Option<&Value>) -> Result {
        let response = self.request(method, path, body).await?;
        let status = response.status();
        let mut incoming = response.into_body();
        let mut bytes = Vec::new();
        while let Some(frame) = tokio::time::timeout(Duration::from_secs(20), incoming.frame())
            .await
            .map_err(|_| unanswered("WebDriverAgent response timed out"))?
        {
            let frame = frame.map_err(|e| unanswered(e.to_string()))?;
            if let Ok(data) = frame.into_data() {
                if bytes.len() + data.len() > LIMIT {
                    return Err(unanswered("WebDriverAgent response exceeds 16 MiB"));
                }
                bytes.extend_from_slice(&data);
            }
        }
        let result: Value = serde_json::from_slice(&bytes)
            .map_err(|e| unanswered(format!("WebDriverAgent answered no JSON: {e}")))?;
        if !status.is_success() || result["value"]["error"].is_string() {
            return Err(err(result["value"]["message"]
                .as_str()
                .unwrap_or("WebDriverAgent request failed"))
            .details(json!({"status":status.as_u16(),"response":result,"delivery":"answered"})));
        }
        Ok(result)
    }
}

#[derive(Default)]
struct Multipart {
    buffer: Vec<u8>,
    length: Option<usize>,
}
impl Multipart {
    fn push(&mut self, data: &[u8]) -> Result<Vec<Vec<u8>>> {
        if self.buffer.len() + data.len() > LIMIT + 8192 {
            return Err(err("MJPEG buffer limit exceeded"));
        }
        self.buffer.extend_from_slice(data);
        let mut frames = Vec::new();
        loop {
            if self.length.is_none() {
                // WebDriverAgent ends each part with "\r\n\r\n" before the next
                // boundary; skip the line breaks so they are not read as an
                // empty part header.
                let gap = self
                    .buffer
                    .iter()
                    .take_while(|b| matches!(b, b'\r' | b'\n'))
                    .count();
                self.buffer.drain(..gap);
                let Some(end) = self.buffer.windows(4).position(|v| v == b"\r\n\r\n") else {
                    if self.buffer.len() > 8192 {
                        return Err(err("Invalid MJPEG part headers"));
                    }
                    break;
                };
                let header = std::str::from_utf8(&self.buffer[..end])
                    .map_err(|_| err("Invalid MJPEG header encoding"))?;
                let mut length = None;
                let mut jpeg = false;
                for line in header.lines() {
                    if let Some((key, value)) = line.split_once(':') {
                        if key.eq_ignore_ascii_case("content-length") {
                            if length.is_some() {
                                return Err(err("Duplicate MJPEG length"));
                            }
                            length = Some(
                                value
                                    .trim()
                                    .parse::<usize>()
                                    .map_err(|_| err("Invalid MJPEG length"))?,
                            );
                        }
                        if key.eq_ignore_ascii_case("content-type") {
                            // WebDriverAgent sends the non-standard "image/jpg".
                            let kind = value.trim();
                            jpeg = kind.eq_ignore_ascii_case("image/jpeg")
                                || kind.eq_ignore_ascii_case("image/jpg");
                        }
                    }
                }
                let length = length
                    .filter(|n| (4..=LIMIT).contains(n))
                    .ok_or_else(|| err("Missing or oversized MJPEG frame"))?;
                if !jpeg {
                    return Err(err("The iPhone stream is not JPEG"));
                }
                self.buffer.drain(..end + 4);
                self.length = Some(length);
            }
            let length = self.length.unwrap();
            if self.buffer.len() < length {
                break;
            }
            let frame: Vec<_> = self.buffer.drain(..length).collect();
            if !frame.starts_with(&[0xff, 0xd8]) || !frame.ends_with(&[0xff, 0xd9]) {
                return Err(err("Invalid JPEG frame"));
            }
            frames.push(frame);
            self.length = None;
        }
        Ok(frames)
    }
}

/// The size a JPEG's start-of-frame header declares, or None when the data
/// is not a baseline or progressive JPEG with one.
pub fn jpeg_size(data: &[u8]) -> Option<(u32, u32)> {
    if data.len() < 4 || data[0] != 0xff || data[1] != 0xd8 {
        return None;
    }
    let mut at = 2;
    while at + 9 < data.len() {
        if data[at] != 0xff {
            return None;
        }
        let marker = data[at + 1];
        if marker == 0xff {
            at += 1;
            continue;
        }
        if (0xd0..=0xd8).contains(&marker) || marker == 0x01 {
            at += 2;
            continue;
        }
        let length = usize::from(u16::from_be_bytes([data[at + 2], data[at + 3]]));
        if matches!(
            marker,
            0xc0 | 0xc1
                | 0xc2
                | 0xc3
                | 0xc5
                | 0xc6
                | 0xc7
                | 0xc9
                | 0xca
                | 0xcb
                | 0xcd
                | 0xce
                | 0xcf
        ) {
            if length < 7 {
                return None;
            }
            let height = u32::from(u16::from_be_bytes([data[at + 5], data[at + 6]]));
            let width = u32::from(u16::from_be_bytes([data[at + 7], data[at + 8]]));
            return (width > 0 && height > 0).then_some((width, height));
        }
        if marker == 0xda || marker == 0xd9 {
            return None;
        }
        at += 2 + length;
    }
    None
}

/// `portrait`/`landscape`, the finer device orientation and the quarter
/// turns from WebDriverAgent's orientation names.
fn orientation_facts(value: &str) -> (&'static str, &'static str, u64) {
    match value {
        "PORTRAIT" => ("portrait", "portrait", 0),
        "UIA_DEVICE_ORIENTATION_PORTRAIT_UPSIDEDOWN" => ("portrait", "portrait_upside_down", 2),
        "LANDSCAPE" => ("landscape", "landscape_left", 1),
        "UIA_DEVICE_ORIENTATION_LANDSCAPERIGHT" => ("landscape", "landscape_right", 3),
        _ => ("unknown", "unknown", 0),
    }
}

pub struct Iphone {
    rt: tokio::runtime::Runtime,
    http: Http,
    session: String,
    width: f64,
    height: f64,
    /// WebDriverAgent's name for the orientation the point size was read in.
    orientation: String,
    /// Bumped by the video task whenever the MJPEG frame size changes: the
    /// phone turned (or its display changed) without us asking, so the
    /// cached point size and orientation are stale.
    epoch: Arc<std::sync::atomic::AtomicU64>,
    seen_epoch: u64,
    /// How often /window/size was requested, for tests and runtime.state.
    size_requests: u64,
    gesture: Vec<Value>,
    last_touch: Option<Instant>,
    video: Option<tokio::task::JoinHandle<()>>,
    /// The session's state, for the frame size the video task records.
    shared_state: Option<Shared>,
    _processes: Vec<ManagedProcess>,
    _lock: Option<Lock>,
}
impl Drop for Iphone {
    fn drop(&mut self) {
        if let Some(video) = self.video.take() {
            video.abort();
        }
        if !self.session.is_empty() {
            let _ = self.rt.block_on(async {
                tokio::time::timeout(
                    Duration::from_secs(2),
                    self.http
                        .json("DELETE", &format!("/session/{}", self.session), None),
                )
                .await
            });
        }
    }
}
impl Iphone {
    async fn query(&self, method: &str, route: &str, body: Option<&Value>) -> Result {
        self.http
            .json(method, &format!("/session/{}{route}", self.session), body)
            .await
            .map(|v| v["value"].clone())
    }
    fn call(&self, method: &str, route: &str, body: Option<&Value>) -> Result {
        self.rt.block_on(self.query(method, route, body))
    }
    /// The point size and orientation, requested from the phone only when
    /// unknown, forced, or after the video frames changed size: a roundtrip
    /// per tap was most of a tap's cost, and a cache without that
    /// invalidation would map taps wrongly after the person turned the phone.
    fn refresh_geometry(&mut self, force: bool) -> Result<()> {
        let epoch = self.epoch.load(Ordering::Relaxed);
        if !force && self.width > 0.0 && self.height > 0.0 && epoch == self.seen_epoch {
            return Ok(());
        }
        let size = self.call("GET", "/window/size", None)?;
        self.size_requests += 1;
        self.width = number(&size, "width");
        self.height = number(&size, "height");
        if self.width <= 0.0 || self.height <= 0.0 {
            return Err(err("iPhone returned an invalid screen size"));
        }
        self.orientation = self
            .call("GET", "/orientation", None)
            .ok()
            .and_then(|v| v.as_str().map(String::from))
            .unwrap_or_default();
        self.seen_epoch = epoch;
        Ok(())
    }
    fn dimensions(&mut self) -> Result<()> {
        self.refresh_geometry(false)
    }
    fn point(&self, a: &Value, x: &str, y: &str) -> Value {
        json!({"x":(number(a,x)*(self.width-1.0)).round(),"y":(number(a,y)*(self.height-1.0)).round()})
    }
    fn text(&self, text: &str) -> Result {
        self.call("POST", "/wda/keys", Some(&json!({"value":[text]})))
    }
    fn screenshot(&self) -> Result<Vec<u8>> {
        let encoded = self.call("GET", "/screenshot", None)?;
        let data = STANDARD
            .decode(
                encoded
                    .as_str()
                    .ok_or_else(|| err("Invalid screenshot response"))?,
            )
            .map_err(|e| err(e.to_string()))?;
        if data.len() < 24 || !data.starts_with(b"\x89PNG\r\n\x1a\n") {
            return Err(err("The iPhone did not return a PNG screenshot"));
        }
        Ok(data)
    }
    fn actions(&self, actions: Value) -> Result {
        self.pointer_sources(vec![("tomacco-touch", actions)])
    }
    /// One W3C actions request with a touch pointer per source: WebDriverAgent
    /// runs them together, so two sources are two fingers.
    fn pointer_sources(&self, sources: Vec<(&str, Value)>) -> Result {
        let actions: Vec<Value> = sources
            .into_iter()
            .map(|(id, actions)| json!({"type":"pointer","id":id,"parameters":{"pointerType":"touch"},"actions":actions}))
            .collect();
        self.call("POST", "/actions", Some(&json!({"actions":actions})))
    }
    /// A finger's W3C sequence from a trace of (x, y, t_ms) in 0..1.
    fn finger(&self, trace: &[(f64, f64, u64)]) -> Vec<Value> {
        let at = |x: f64, y: f64| json!({"x":(x * (self.width - 1.0)).round(),"y":(y * (self.height - 1.0)).round()});
        let mut out = Vec::new();
        let first = at(trace[0].0, trace[0].1);
        out.push(json!({"type":"pointerMove","duration":0,"x":first["x"],"y":first["y"],"origin":"viewport"}));
        out.push(json!({"type":"pointerDown","button":0}));
        let mut last_t = trace[0].2;
        for (x, y, t) in &trace[1..] {
            let p = at(*x, *y);
            out.push(json!({"type":"pointerMove","duration":t - last_t,"x":p["x"],"y":p["y"],"origin":"viewport"}));
            last_t = *t;
        }
        out.push(json!({"type":"pointerUp","button":0}));
        out
    }
    /// Whether a field has the keyboard focus: WebDriverAgent's active
    /// element, which answers `no such element` when nothing has focus.
    fn focused(result: Result) -> Value {
        match result {
            Ok(_) => json!({"available":true,"focused":true,"source":"wda /element/active"}),
            Err(error) => {
                let text = format!("{} {:?}", error.message, error.details).to_lowercase();
                if text.contains("no such element") || text.contains("not found") {
                    json!({"available":true,"focused":false,"source":"wda /element/active"})
                } else {
                    super::unavailable(&format!("wda /element/active failed: {}", error.message))
                }
            }
        }
    }
}

/// How a WebDriverAgent call failed, for the verified fill: never sent,
/// answered with a refusal (W3C `stale element reference` and `no such
/// element` told apart), or sent without an answer.
fn failure(error: Error) -> field::Failure {
    let details = error.details.as_deref().cloned().unwrap_or(Value::Null);
    let fault = match s(&details, "delivery") {
        "not_sent" => field::Fault::NotSent,
        "answered" => match s(&details["response"]["value"], "error") {
            "stale element reference" => field::Fault::Stale,
            "no such element" => field::Fault::Missing,
            _ => field::Fault::Refused,
        },
        _ => field::Fault::Lost,
    };
    field::Failure::new(fault, error.message)
}
fn element_id(value: &Value) -> Option<&str> {
    value["element-6066-11e4-a52e-4f735466cecf"]
        .as_str()
        .or_else(|| value["ELEMENT"].as_str())
}
/// WebDriverAgent's element routes for the verified fill. Each read is one
/// request on the same session and element ID; the value of a secure field
/// is never requested (native/runtime/field.rs checks the class first).
impl field::FieldDriver for Iphone {
    fn evidence(&self) -> &'static str {
        "wda"
    }
    fn tap(&mut self, a: &Value) -> field::Step<(f64, f64)> {
        self.dimensions().map_err(failure)?;
        let at = self.point(a, "x", "y");
        self.call("POST", "/wda/tap", Some(&at)).map_err(failure)?;
        Ok((number(&at, "x"), number(&at, "y")))
    }
    fn active(&mut self) -> field::Step<Option<String>> {
        match self.call("GET", "/element/active", None).map_err(failure) {
            Ok(found) => match element_id(&found) {
                Some(id) if identifier(id, "element ID", Some("[A-Za-z0-9-]+")).is_ok() => {
                    Ok(Some(id.to_string()))
                }
                Some(_) => Err(field::Failure::new(
                    field::Fault::Refused,
                    "WebDriverAgent answered an invalid element ID",
                )),
                None => Ok(None),
            },
            Err(f) if f.fault == field::Fault::Missing => Ok(None),
            Err(f) => Err(f),
        }
    }
    fn describe(&mut self, id: &str) -> field::Step<field::Editor> {
        let attribute = |name: &str| {
            self.call("GET", &format!("/element/{id}/attribute/{name}"), None)
                .map_err(failure)
        };
        let text = |v: Value| v.as_str().unwrap_or("").to_string();
        let class = text(attribute("type")?);
        let name = text(attribute("name")?);
        let label = text(attribute("label")?);
        let placeholder = text(attribute("placeholderValue")?);
        let enabled = match attribute("enabled")? {
            Value::Bool(v) => v,
            Value::String(v) => v == "true" || v == "1",
            Value::Number(v) => v.as_u64() == Some(1),
            _ => false,
        };
        let rect = self
            .call("GET", &format!("/element/{id}/rect"), None)
            .map_err(failure)?;
        let rect = ["x", "y", "width", "height"]
            .iter()
            .map(|k| rect[k].as_f64())
            .collect::<Option<Vec<f64>>>()
            .map(|r| (r[0], r[1], r[2], r[3]));
        Ok(field::Editor {
            class,
            name,
            label,
            placeholder,
            rect,
            enabled,
        })
    }
    fn value(&mut self, id: &str) -> field::Step<Option<String>> {
        let value = self
            .call("GET", &format!("/element/{id}/attribute/value"), None)
            .map_err(failure)?;
        Ok(match value {
            Value::Null => None,
            Value::String(v) => Some(v),
            other => Some(other.to_string()),
        })
    }
    fn clear(&mut self, id: &str) -> field::Step<()> {
        self.call("POST", &format!("/element/{id}/clear"), Some(&json!({})))
            .map(|_| ())
            .map_err(failure)
    }
    fn write(&mut self, id: &str, text: &str) -> field::Step<()> {
        self.call(
            "POST",
            &format!("/element/{id}/value"),
            Some(&json!({"value":[text]})),
        )
        .map(|_| ())
        .map_err(failure)
    }
    fn submit(&mut self) -> field::Step<()> {
        self.text("\n").map(|_| ()).map_err(failure)
    }
}

pub fn connect(a: &Value, shared: Shared, dir: &Path) -> Result<Iphone> {
    validate(a)?;
    let lock = Lock::open(
        &home()?
            .join("locks")
            .join(format!("iphone-{}.lock", digest(s(a, "device").as_bytes()))),
    )?;
    if !lock.acquire(true)? {
        return Err(Error::new(
            "device_busy",
            "This iPhone already has an active native session",
        ));
    }
    let rt = tokio::runtime::Runtime::new()?;
    // Reserve separate ephemeral ports before handing them to iproxy. A failed
    // bind is reported by that owned child; no existing forwarding is replaced.
    let first = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))?;
    let second = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))?;
    let port = first.local_addr()?.port();
    let video_port = second.local_addr()?.port();
    let args = argv(&[
        &tc::find("iproxy")?,
        "--udid",
        s(a, "device"),
        "--source",
        "127.0.0.1",
        &format!("{port}:8100"),
        &format!("{video_port}:9100"),
    ]);
    drop(first);
    drop(second);
    let mut processes = vec![spawn(&args, &dir.join("usb.log"))?];
    let bundle = if s(a, "runner_bundle_id").is_empty() {
        DEFAULT_RUNNER
    } else {
        s(a, "runner_bundle_id")
    };
    let mode = s(a, "runner_mode");
    if mode != "external" {
        let go = tc::find("go-ios")?;
        // WDA's HTTP server runs inside an XCTest. A plain app launch does not
        // establish the testmanagerd session or its developer-library environment.
        let args = argv(&[
            &go,
            "runwda",
            &format!("--udid={}", s(a, "device")),
            &format!("--bundleid={bundle}"),
            &format!("--testrunnerbundleid={bundle}"),
            "--xctestconfig=WebDriverAgentRunner.xctest",
        ]);
        processes.push(spawn(&args, &dir.join("runner.log"))?);
    }
    let http = Http { port };
    let deadline = Instant::now() + Duration::from_secs(70);
    loop {
        if shared.lock().unwrap().stopped.load(Ordering::Relaxed) {
            return Err(err("iPhone connection cancelled"));
        }
        if let Some(status) = processes[0].0.try_wait()? {
            return Err(err(format!(
                "USB forward stopped ({status}); check usbmuxd and trust this computer on the iPhone"
            )));
        }
        if processes.len() > 1 {
            if let Some(status) = processes[1].0.try_wait()? {
                if !status.success() {
                    return Err(err("The iPhone runner could not start. Check Developer Mode, the mounted developer image, tunnel and signed WebDriverAgent installation.").details(json!({"log_path":dir.join("runner.log")})));
                }
            }
        }
        if rt.block_on(http.json("GET", "/status", None)).is_ok() {
            break;
        }
        if Instant::now() >= deadline {
            return Err(err(
                "WebDriverAgent did not become ready. Install and sign the runner, enable Developer Mode and prepare the iPhone connection.",
            ));
        }
        thread::sleep(Duration::from_millis(200));
    }
    let created = rt.block_on(http.json("POST","/session",Some(&json!({"capabilities":{"alwaysMatch":{"shouldWaitForQuiescence":false,"shouldTerminateApp":false,"waitForIdleTimeout":0}}}))))?;
    let session = created["sessionId"]
        .as_str()
        .or_else(|| created["value"]["sessionId"].as_str())
        .filter(|v| !v.is_empty() && v.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-'))
        .ok_or_else(|| err("WebDriverAgent returned no valid session ID"))?
        .to_owned();
    let mut device = Iphone {
        rt,
        http,
        session,
        width: 0.0,
        height: 0.0,
        orientation: String::new(),
        epoch: Arc::new(std::sync::atomic::AtomicU64::new(0)),
        seen_epoch: 0,
        size_requests: 0,
        gesture: Vec::new(),
        last_touch: None,
        video: None,
        shared_state: Some(shared.clone()),
        _processes: processes,
        _lock: Some(lock),
    };
    device.call("POST","/appium/settings",Some(&json!({"settings":{"screenshotQuality":0,"mjpegServerScreenshotQuality":75,"mjpegServerFramerate":n(a,"fps").min(60),"mjpegScalingFactor":100,
        // WDA otherwise waits up to 2 s after each gesture for animations to
        // settle and up to 10 s for the app to go idle before a tap: a remote
        // finger must land at once, as on the phone.
        "animationCoolOffTimeout":0,"waitForIdleTimeout":0}})))?;
    device.dimensions()?;
    let png = device.screenshot()?;
    let width = u32::from_be_bytes(png[16..20].try_into().unwrap());
    let height = u32::from_be_bytes(png[20..24].try_into().unwrap());
    let product = product_type(s(a, "device"));
    let profile = profile_for(&crate::catalog::builtin(), &product);
    let mut details = json!({"name":profile.as_ref().map_or("iPhone / iPad",|p|s(p,"name")),"transport":"usb/wda","codec":"mjpeg","width":width,"height":height,"point_width":device.width,"point_height":device.height,"runner_bundle_id":bundle,"mac_required":false});
    if !product.is_empty() {
        details["product_type"] = json!(product);
    }
    if let Some(profile) = &profile {
        details["profile_id"] = profile["id"].clone();
        details["profile_source"] = json!("product-type");
    }
    shared.lock().unwrap().update(details)?;
    let stream = device
        .rt
        .block_on(Http { port: video_port }.request("GET", "/", None))?;
    if !stream.status().is_success() {
        return Err(err("The iPhone MJPEG stream is unavailable"));
    }
    let epoch = device.epoch.clone();
    device.video = Some(device.rt.spawn(async move {
        let mut body = stream.into_body();
        let mut decoder = Multipart::default();
        let mut frame_size: Option<(u32, u32)> = None;
        let result: Result<()> = async {
            while let Some(frame) = tokio::time::timeout(Duration::from_secs(20), body.frame())
                .await
                .map_err(|_| err("iPhone video stalled"))?
            {
                let frame = frame.map_err(|e| err(e.to_string()))?;
                if let Ok(data) = frame.into_data() {
                    for jpeg in decoder.push(&data)? {
                        // The frames follow the screen: a changed size means
                        // the phone turned, and the cached point size is stale.
                        if let Some(size) = jpeg_size(&jpeg) {
                            if frame_size.is_some_and(|known| known != size) {
                                epoch.fetch_add(1, Ordering::Relaxed);
                                let _ = shared
                                    .lock()
                                    .unwrap()
                                    .update(json!({"width":size.0,"height":size.1,"geometry_epoch":epoch.load(Ordering::Relaxed)}));
                            }
                            frame_size = Some(size);
                        }
                        shared.lock().unwrap().video(jpeg, false, true);
                    }
                }
            }
            Err(err("iPhone video disconnected"))
        }
        .await;
        if let Err(error) = result {
            let mut hub = shared.lock().unwrap();
            let _ = hub.update(json!({"error":error.message}));
            hub.stopped.store(true, Ordering::Relaxed);
        }
    }));
    Ok(device)
}

impl Device for Iphone {
    fn input(&mut self, a: &Value) -> Result {
        match s(a, "action") {
            "tap" => {
                self.dimensions()?;
                // A pointer action skips the idle wait of XCTest's coordinate tap.
                let at = self.point(a, "x", "y");
                self.actions(json!([{"type":"pointerMove","duration":0,"x":at["x"],"y":at["y"],"origin":"viewport"},{"type":"pointerDown","button":0},{"type":"pause","duration":50},{"type":"pointerUp","button":0}]))
            }
            "touch" => {
                let phase = s(a, "phase");
                if phase == "cancel" {
                    self.gesture.clear();
                    self.last_touch = None;
                    return Ok(json!({"cancelled":true}));
                }
                if phase == "down" {
                    self.dimensions()?;
                    self.gesture.clear();
                    self.last_touch = Some(Instant::now());
                }
                if self.last_touch.is_none() {
                    return Err(err("Touch requires a preceding down event"));
                }
                let mut point = self.point(a, "x", "y");
                point["type"] = json!("pointerMove");
                point["origin"] = json!("viewport");
                point["duration"] = json!(if phase == "down" {
                    0
                } else {
                    self.last_touch.unwrap().elapsed().as_millis().min(1000) as u64
                });
                if self.gesture.len() >= 240 {
                    self.gesture.clear();
                    self.last_touch = None;
                    return Err(err("Gesture exceeds the input limit"));
                }
                self.gesture.push(point);
                self.last_touch = Some(Instant::now());
                if phase == "down" {
                    self.gesture.push(json!({"type":"pointerDown","button":0}));
                }
                if phase == "up" {
                    self.gesture.push(json!({"type":"pointerUp","button":0}));
                    self.last_touch = None;
                    let gesture = std::mem::take(&mut self.gesture);
                    return self.actions(json!(gesture));
                }
                Ok(json!({"buffered":true}))
            }
            "swipe" => {
                self.dimensions()?;
                let from = self.point(a, "x", "y");
                let to = self.point(a, "x2", "y2");
                self.actions(json!([{"type":"pointerMove","duration":0,"x":from["x"],"y":from["y"],"origin":"viewport"},{"type":"pointerDown","button":0},{"type":"pointerMove","duration":(number(a,"duration")*1000.0).round(),"x":to["x"],"y":to["y"],"origin":"viewport"},{"type":"pointerUp","button":0}]))
            }
            "text" => self.text(s(a, "text")),
            "fill" => {
                self.dimensions()?;
                self.call("POST", "/wda/tap", Some(&self.point(a, "x", "y")))?;
                let active = self.call("GET", "/element/active", None)?;
                let id = active["element-6066-11e4-a52e-4f735466cecf"]
                    .as_str()
                    .or_else(|| active["ELEMENT"].as_str())
                    .ok_or_else(|| err("No focused accessible field"))?;
                identifier(id, "element ID", Some("[A-Za-z0-9-]+"))?;
                self.call("POST", &format!("/element/{id}/clear"), Some(&json!({})))?;
                if s(a, "text").is_empty() {
                    Ok(json!({"cleared":true}))
                } else {
                    self.call(
                        "POST",
                        &format!("/element/{id}/value"),
                        Some(&json!({"value":[s(a,"text")]})),
                    )
                }
            }
            "key" => match s(a, "key") {
                "home" => {
                    self.rt
                        .block_on(self.http.json("POST", "/wda/homescreen", Some(&json!({}))))
                }
                "enter" => self.text("\n"),
                "backspace" | "delete" => self.text("\u{8}"),
                "tab" => self.text("\t"),
                _ => Err(err("This hardware key is not supported by WebDriverAgent")),
            },
            "rotate" => {
                let orientation = match s(a, "orientation") {
                    "portrait" => "PORTRAIT",
                    "portrait_upside_down" => "UIA_DEVICE_ORIENTATION_PORTRAIT_UPSIDEDOWN",
                    "landscape_left" => "LANDSCAPE",
                    "landscape_right" => "UIA_DEVICE_ORIENTATION_LANDSCAPERIGHT",
                    _ => return Err(err("Choose an explicit iPhone orientation")),
                };
                let result = self.call(
                    "POST",
                    "/orientation",
                    Some(&json!({"orientation":orientation})),
                )?;
                self.refresh_geometry(true)?;
                Ok(result)
            }
            "pinch" => {
                require(a, &["x", "y", "scale"])?;
                self.dimensions()?;
                // Two touch pointers either side of the centre, moved apart
                // (scale > 1) or together, in one atomic W3C request.
                let (cx, cy) = (number(a, "x"), number(a, "y"));
                let scale = number(a, "scale").clamp(0.05, 20.0);
                let ms = (a["duration"].as_f64().unwrap_or(0.3).clamp(0.05, 5.0) * 1000.0).round()
                    as u64;
                let (from, to) = if scale >= 1.0 {
                    (0.08, (0.08 * scale).min(0.45))
                } else {
                    (0.3, (0.3 * scale).max(0.02))
                };
                let left = vec![
                    ((cx - from).clamp(0.0, 1.0), cy, 0),
                    ((cx - to).clamp(0.0, 1.0), cy, ms),
                ];
                let right = vec![
                    ((cx + from).clamp(0.0, 1.0), cy, 0),
                    ((cx + to).clamp(0.0, 1.0), cy, ms),
                ];
                let (a1, a2) = (self.finger(&left), self.finger(&right));
                self.pointer_sources(vec![("finger-1", json!(a1)), ("finger-2", json!(a2))])?;
                Ok(json!({"delivered":true,"mode":"atomic_trace","contacts":2}))
            }
            "gesture" => {
                let contacts = super::check_gesture(a)?;
                self.dimensions()?;
                let names = ["finger-1", "finger-2", "finger-3", "finger-4", "finger-5"];
                let sources: Vec<(&str, Value)> = contacts
                    .iter()
                    .enumerate()
                    .map(|(i, trace)| (names[i], json!(self.finger(trace))))
                    .collect();
                let count = sources.len();
                let started = Instant::now();
                self.pointer_sources(sources)?;
                Ok(
                    json!({"delivered":true,"mode":"atomic_trace","contacts":count,
                    "elapsed_ms":started.elapsed().as_millis() as u64,
                    "note":"The whole trace was submitted as one WebDriverAgent actions request; timing between contacts follows its tick model."}),
                )
            }
            _ => Err(err(
                "This input action is not supported by the physical iPhone transport",
            )),
        }
    }
    fn verified_fill(&mut self, a: &Value) -> Result {
        field::fill(self, a)
    }
    fn fill_verification(&self) -> Value {
        json!({"supported":true,"evidence":"wda","editors":field::SUPPORTED,
            "note":"Reads the focused element's type, rect and placeholder and its value before and after writing, on one WebDriverAgent session and element ID. Secure fields are refused unread. Contract-tested against a loopback WebDriverAgent double, not yet on hardware."})
    }
    fn capture(&mut self, path: &Path) -> Result {
        let data = self.screenshot()?;
        atomic_write(path, &data)?;
        artifact(path, 0.0)
    }
    fn hierarchy(&mut self) -> Result {
        let value = self.call("GET", "/source", None)?;
        Ok(
            json!({"xml":value,"transport":"usb/wda","format":"wda","point_width":self.width,"point_height":self.height}),
        )
    }
    fn geometry(&mut self) -> Result {
        self.refresh_geometry(false)?;
        let h = self.shared_size();
        let (orientation, device_orientation, rotation) = orientation_facts(&self.orientation);
        let scale = if self.width > 0.0 {
            f64::from(h.0) / self.width
        } else {
            0.0
        };
        Ok(
            json!({"width":h.0,"height":h.1,"point_width":self.width,"point_height":self.height,
            "scale":(scale * 100.0).round() / 100.0,"orientation":orientation,"device_orientation":device_orientation,
            "rotation":rotation,"geometry_epoch":self.seen_epoch,"size_requests":self.size_requests,
            "source":"wda /window/size and /orientation, cached until the MJPEG frame size changes",
            "coordinate_space":super::coordinate_space()}),
        )
    }
    fn context(&mut self) -> Result {
        let keyboard_query = json!({"using":"class chain","value":"**/XCUIElementTypeKeyboard"});
        // These are independent reads on the same WDA session. Await all three
        // (including failures) before returning to the worker's command queue;
        // input stays serialized and every observation still reads fresh state.
        // No context cache: a person may change the phone between observations.
        let (app, keyboard, focus) = self.rt.block_on(async {
            tokio::join!(
                self.query("GET", "/wda/activeAppInfo", None),
                self.query("POST", "/elements", Some(&keyboard_query)),
                self.query("GET", "/element/active", None),
            )
        });
        let app = match app {
            Ok(info) => {
                let bundle = s(&info, "bundleId").to_string();
                json!({"available":!bundle.is_empty(),"bundle_id":bundle,"name":info["name"],"pid":info["pid"],
                    "home_screen":bundle == "com.apple.springboard","source":"wda /wda/activeAppInfo"})
            }
            Err(error) => {
                super::unavailable(&format!("wda /wda/activeAppInfo failed: {}", error.message))
            }
        };
        let keyboard = match keyboard {
            Ok(found) => {
                json!({"available":true,"visible":found.as_array().is_some_and(|f| !f.is_empty()),
                "source":"wda class chain **/XCUIElementTypeKeyboard"})
            }
            Err(error) => super::unavailable(&format!("wda /elements failed: {}", error.message)),
        };
        Ok(json!({"app":app,"keyboard":keyboard,"focus":Self::focused(focus)}))
    }
    fn capabilities(&self) -> Value {
        json!({"transport":"usb/wda","evidence":"device","capture":true,"hierarchy":"wda xml (converted for screen.*)",
            "live_touch":false,"atomic_trace":true,"max_contacts":5,"pinch":true,"fill":true,"text":"unicode",
            "rotate":"explicit orientation","exact_scroll":true,
            "keys":["home","enter","backspace","delete","tab"],
            "note":"touch down/move events are buffered and sent as one atomic WebDriverAgent actions request on touch-up; runtime.gesture sends a timestamped trace the same way. There is no live incremental drag over WebDriverAgent. Pinch and multi-contact are not yet hardware-qualified here."})
    }
}
impl Iphone {
    /// The frame size runtime.state carries, as the video task keeps it.
    fn shared_size(&self) -> (u32, u32) {
        match &self.shared_state {
            Some(shared) => {
                let h = shared.lock().unwrap();
                (n(&h.state, "width") as u32, n(&h.state, "height") as u32)
            }
            None => (0, 0),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn physical_protocol_maps_points_unicode_rotation_and_failure() {
        // A real loopback HTTP contract, explicitly not a physical-iPhone test.
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        listener.set_nonblocking(true).unwrap();
        let port = listener.local_addr().unwrap().port();
        let calls = Arc::new(Mutex::new(Vec::<Value>::new()));
        let seen = calls.clone();
        let stopped = Arc::new(AtomicBool::new(false));
        let stop = stopped.clone();
        let server = thread::spawn(move || {
            let mut landscape = false;
            while !stop.load(Ordering::Relaxed) {
                let Ok((mut socket, _)) = listener.accept() else {
                    thread::sleep(Duration::from_millis(2));
                    continue;
                };
                socket
                    .set_read_timeout(Some(Duration::from_secs(2)))
                    .unwrap();
                let mut reader = BufReader::new(socket.try_clone().unwrap());
                let mut line = String::new();
                if reader.read_line(&mut line).unwrap_or(0) == 0 {
                    continue;
                }
                let words = line.split_whitespace().collect::<Vec<_>>();
                let method = words[0].to_owned();
                let route = words[1].to_owned();
                let mut length = 0;
                loop {
                    line.clear();
                    reader.read_line(&mut line).unwrap();
                    if line == "\r\n" {
                        break;
                    }
                    if let Some((key, value)) = line.split_once(':') {
                        if key.eq_ignore_ascii_case("content-length") {
                            length = value.trim().parse::<usize>().unwrap();
                        }
                    }
                }
                let mut body = vec![0; length];
                reader.read_exact(&mut body).unwrap();
                let body = if body.is_empty() {
                    Value::Null
                } else {
                    serde_json::from_slice::<Value>(&body).unwrap()
                };
                seen.lock()
                    .unwrap()
                    .push(json!({"method":method,"route":route,"body":body}));
                if route.ends_with("/orientation") && method == "POST" {
                    landscape = body["orientation"] == "LANDSCAPE";
                }
                let value = if route.ends_with("/window/size") {
                    if landscape {
                        json!({"width":844,"height":390})
                    } else {
                        json!({"width":390,"height":844})
                    }
                } else if route.ends_with("/orientation") && method == "GET" {
                    json!(if landscape { "LANDSCAPE" } else { "PORTRAIT" })
                } else if route.ends_with("/wda/activeAppInfo") {
                    json!({"bundleId":"com.apple.Preferences","name":"Settings","pid":411})
                } else if route.ends_with("/elements") {
                    json!([])
                } else if route.ends_with("/screenshot") {
                    json!(
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jD1sAAAAASUVORK5CYII="
                    )
                } else if route.ends_with("/element/active") {
                    json!({"element-6066-11e4-a52e-4f735466cecf":"field-1"})
                } else if route.ends_with("/source") {
                    json!("<App><TextField label=\"Name\"/></App>")
                } else if route.ends_with("/failure") {
                    json!({"error":"invalid argument","message":"field refused"})
                } else {
                    Value::Null
                };
                let response = json!({"value":value}).to_string();
                let status = if route.ends_with("/failure") {
                    400
                } else {
                    200
                };
                write!(socket,"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{response}",response.len()).unwrap();
            }
        });
        let epoch = Arc::new(std::sync::atomic::AtomicU64::new(0));
        let mut device = Iphone {
            rt: tokio::runtime::Runtime::new().unwrap(),
            http: Http { port },
            session: "test".into(),
            width: 0.0,
            height: 0.0,
            orientation: String::new(),
            epoch: epoch.clone(),
            seen_epoch: 0,
            size_requests: 0,
            gesture: Vec::new(),
            last_touch: None,
            video: None,
            shared_state: None,
            _processes: Vec::new(),
            _lock: None,
        };
        device
            .input(&json!({"action":"tap","x":0.25,"y":0.5}))
            .unwrap();
        let unicode = "Grüße 日本 🎵 %s $(literal)";
        device
            .input(&json!({"action":"text","text":unicode}))
            .unwrap();
        device
            .input(&json!({"action":"fill","text":unicode,"x":0.5,"y":0.5}))
            .unwrap();
        device
            .input(&json!({"action":"fill","text":"","x":0.5,"y":0.5}))
            .unwrap();
        device
            .input(&json!({"action":"touch","phase":"down","x":0.1,"y":0.1}))
            .unwrap();
        device
            .input(&json!({"action":"touch","phase":"up","x":0.8,"y":0.7}))
            .unwrap();
        device
            .input(&json!({"action":"rotate","orientation":"landscape_left"}))
            .unwrap();
        device
            .input(&json!({"action":"tap","x":0.5,"y":0.25}))
            .unwrap();
        let directory = tempfile::tempdir().unwrap();
        let capture = device
            .capture(&directory.path().join("capture.png"))
            .unwrap();
        assert!(n(&capture, "bytes") > 24);
        assert!(s(&device.hierarchy().unwrap(), "xml").contains("TextField"));
        assert_eq!(
            device.call("GET", "/failure", None).unwrap_err().message,
            "field refused"
        );
        // The point size was requested once at first use and once after the
        // rotation; every tap, touch and fill in between used the cache.
        assert_eq!(device.size_requests, 2);
        let geometry = device.geometry().unwrap();
        assert_eq!(geometry["point_width"], 844.0);
        assert_eq!(geometry["orientation"], "landscape");
        assert_eq!(geometry["rotation"], 1);
        // A changed MJPEG frame size (the person turned the phone) invalidates it.
        epoch.fetch_add(1, Ordering::Relaxed);
        device
            .input(&json!({"action":"tap","x":0.5,"y":0.25}))
            .unwrap();
        assert_eq!(device.size_requests, 3);
        let context = device.context().unwrap();
        assert_eq!(context["app"]["bundle_id"], "com.apple.Preferences");
        assert_eq!(context["app"]["home_screen"], false);
        assert_eq!(context["keyboard"]["visible"], false);
        assert_eq!(context["focus"]["focused"], true);
        assert_eq!(device.capabilities()["live_touch"], false);
        let pinch = device
            .input(&json!({"action":"pinch","x":0.5,"y":0.5,"scale":2.0,"duration":0.2}))
            .unwrap();
        assert_eq!(pinch["contacts"], 2);
        let trace = device
            .input(&json!({"action":"gesture","contacts":[{"points":[{"x":0.5,"y":0.8,"t_ms":0},{"x":0.5,"y":0.6,"t_ms":120},{"x":0.5,"y":0.2,"t_ms":400}]}]}))
            .unwrap();
        assert_eq!(trace["mode"], "atomic_trace");
        assert!(
            device
                .input(&json!({"action":"gesture","contacts":[{"points":[{"x":0.5,"y":0.8,"t_ms":50},{"x":0.5,"y":0.6,"t_ms":0}]}]}))
                .is_err()
        );
        drop(device);
        stopped.store(true, Ordering::Relaxed);
        server.join().unwrap();
        let calls = calls.lock().unwrap();
        let pointer = |v: &Value| {
            v["body"]["actions"][0]["actions"]
                .as_array()
                .cloned()
                .unwrap_or_default()
        };
        let actions = calls
            .iter()
            .filter(|v| s(v, "route").ends_with("/actions"))
            .collect::<Vec<_>>();
        // A tap is a pointer action, so XCTest's idle wait does not delay it.
        let taps = actions
            .iter()
            .filter(|v| pointer(v).iter().any(|a| a["type"] == "pause"))
            .collect::<Vec<_>>();
        // Two taps of the scenario and the one after the frame size changed.
        assert_eq!(taps.len(), 3);
        let at = |v: &Value| json!({"x":pointer(v)[0]["x"],"y":pointer(v)[0]["y"]});
        assert_eq!(at(taps[0]), json!({"x":97.0,"y":422.0}));
        assert_eq!(at(taps[1]), json!({"x":422.0,"y":97.0}));
        assert!(
            calls
                .iter()
                .any(|v| s(v, "route").ends_with("/wda/keys") && v["body"]["value"][0] == unicode)
        );
        assert_eq!(
            calls
                .iter()
                .filter(|v| s(v, "route").ends_with("/element/field-1/clear"))
                .count(),
            2
        );
        let gesture = actions
            .iter()
            .find(|v| !pointer(v).iter().any(|a| a["type"] == "pause"))
            .unwrap();
        assert_eq!(pointer(gesture).len(), 4);
        // The pinch is one request with two touch pointers that move apart.
        let pinch = actions
            .iter()
            .find(|v| {
                v["body"]["actions"]
                    .as_array()
                    .is_some_and(|a| a.len() == 2)
            })
            .unwrap();
        let finger = |i: usize| pinch["body"]["actions"][i]["actions"].clone();
        assert!(finger(0)[0]["x"].as_f64().unwrap() > finger(0)[2]["x"].as_f64().unwrap());
        assert!(finger(1)[0]["x"].as_f64().unwrap() < finger(1)[2]["x"].as_f64().unwrap());
        assert_eq!(finger(0)[2]["duration"], 200);
        // The trace keeps its timestamps as move durations.
        let trace = actions
            .iter()
            .find(|v| pointer(v).len() == 5 && pointer(v)[2]["duration"] == 120)
            .unwrap();
        assert_eq!(pointer(trace)[3]["duration"], 280);
        assert_eq!(calls.last().unwrap()["method"], "DELETE");
    }
    // ---- Verified fill against a loopback WebDriverAgent double -------------
    // A contract test of the requests and their order, explicitly not a
    // physical-iPhone test: the double answers as WebDriverAgent's W3C routes
    // do, including `no such element`, `stale element reference`, an empty
    // field's placeholder reported as its value, and a lost reply.

    #[derive(Clone)]
    struct El {
        class: &'static str,
        name: String,
        placeholder: String,
        value: String,
        rect: (f64, f64, f64, f64),
        enabled: bool,
    }
    fn el(class: &'static str, name: &str, placeholder: &str, value: &str, top: f64) -> El {
        El {
            class,
            name: name.into(),
            placeholder: placeholder.into(),
            value: value.into(),
            rect: (20.0, top, 350.0, 44.0),
            enabled: true,
        }
    }
    #[derive(Default)]
    struct Wda {
        elements: std::collections::BTreeMap<String, El>,
        active: Option<String>,
        /// The element a tap focuses, after `focus_after` polls of the focus.
        on_tap: Option<String>,
        focus_after: u32,
        pending: Option<u32>,
        /// accept, lose_reply, autocorrect, stale_value or replace_after_write.
        mode: &'static str,
        calls: Vec<Value>,
    }
    /// One WebDriverAgent answer, or None to drop the connection unanswered.
    fn answer(w: &mut Wda, method: &str, route: &str, body: &Value) -> Option<(u16, Value)> {
        let ok = |v: Value| Some((200, json!({"value":v})));
        let fail = |status: u16, error: &str| {
            Some((
                status,
                json!({"value":{"error":error,"message":format!("{error} (double)")}}),
            ))
        };
        let route = route
            .strip_prefix("/session/test")
            .unwrap_or(route)
            .to_string();
        let parts: Vec<&str> = route.trim_start_matches('/').split('/').collect();
        let shown = |e: &El| {
            if e.value.is_empty() {
                if e.placeholder.is_empty() {
                    Value::Null
                } else {
                    json!(e.placeholder)
                }
            } else {
                json!(e.value)
            }
        };
        match (method, parts.as_slice()) {
            ("GET", ["window", "size"]) => ok(json!({"width":390,"height":844})),
            ("GET", ["orientation"]) => ok(json!("PORTRAIT")),
            ("POST", ["wda", "tap"]) => {
                if w.on_tap.is_some() {
                    if w.focus_after == 0 {
                        w.active = w.on_tap.clone();
                    } else {
                        w.pending = Some(w.focus_after);
                    }
                }
                ok(Value::Null)
            }
            ("GET", ["element", "active"]) => {
                if let Some(left) = w.pending {
                    if left <= 1 {
                        w.pending = None;
                        w.active = w.on_tap.clone();
                    } else {
                        w.pending = Some(left - 1);
                    }
                }
                match &w.active {
                    Some(id) => ok(json!({"element-6066-11e4-a52e-4f735466cecf":id})),
                    None => fail(404, "no such element"),
                }
            }
            ("GET", ["element", id, "attribute", name]) => {
                let Some(e) = w.elements.get(*id) else {
                    return fail(404, "stale element reference");
                };
                if *name == "value" && w.mode == "stale_value" {
                    return fail(404, "stale element reference");
                }
                ok(match *name {
                    "type" => json!(e.class),
                    "name" | "label" => json!(e.name),
                    "placeholderValue" => {
                        if e.placeholder.is_empty() {
                            Value::Null
                        } else {
                            json!(e.placeholder)
                        }
                    }
                    "enabled" => json!(e.enabled),
                    "value" if e.class == field::SECURE => json!("\u{2022}\u{2022}\u{2022}"),
                    "value" => shown(e),
                    _ => Value::Null,
                })
            }
            ("GET", ["element", id, "rect"]) => match w.elements.get(*id) {
                Some(e) => {
                    ok(json!({"x":e.rect.0,"y":e.rect.1,"width":e.rect.2,"height":e.rect.3}))
                }
                None => fail(404, "stale element reference"),
            },
            ("POST", ["element", id, "clear"]) => match w.elements.get_mut(*id) {
                Some(e) => {
                    e.value.clear();
                    ok(Value::Null)
                }
                None => fail(404, "stale element reference"),
            },
            ("POST", ["element", id, "value"]) => {
                let text = body["value"][0].as_str().unwrap_or("").to_string();
                let mode = w.mode;
                let Some(e) = w.elements.get_mut(*id) else {
                    return fail(404, "stale element reference");
                };
                e.value.push_str(&if mode == "autocorrect" {
                    text.replace("teh", "the")
                } else {
                    text
                });
                match mode {
                    "lose_reply" => None,
                    "replace_after_write" => {
                        let rebuilt = w.elements.remove(*id).unwrap();
                        w.elements.insert(format!("{id}-2"), rebuilt);
                        w.active = Some(format!("{id}-2"));
                        ok(Value::Null)
                    }
                    _ => ok(Value::Null),
                }
            }
            _ => ok(Value::Null),
        }
    }
    /// Runs a fill against the double; answers the result, every request in
    /// order and the values the double's fields hold afterwards.
    fn fill_on(
        wda: Wda,
        a: Value,
    ) -> (
        Result,
        Vec<Value>,
        std::collections::BTreeMap<String, String>,
    ) {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        listener.set_nonblocking(true).unwrap();
        let port = listener.local_addr().unwrap().port();
        let state = Arc::new(Mutex::new(wda));
        let shared = state.clone();
        let stop = Arc::new(AtomicBool::new(false));
        let stopping = stop.clone();
        let server = thread::spawn(move || {
            while !stopping.load(Ordering::Relaxed) {
                let Ok((mut socket, _)) = listener.accept() else {
                    thread::sleep(Duration::from_millis(2));
                    continue;
                };
                socket.set_nonblocking(false).unwrap();
                socket
                    .set_read_timeout(Some(Duration::from_secs(2)))
                    .unwrap();
                let mut reader = BufReader::new(socket.try_clone().unwrap());
                let mut line = String::new();
                if reader.read_line(&mut line).unwrap_or(0) == 0 {
                    continue;
                }
                let words = line
                    .split_whitespace()
                    .map(str::to_string)
                    .collect::<Vec<_>>();
                let mut length = 0;
                loop {
                    line.clear();
                    reader.read_line(&mut line).unwrap();
                    if line == "\r\n" {
                        break;
                    }
                    if let Some((key, value)) = line.split_once(':') {
                        if key.eq_ignore_ascii_case("content-length") {
                            length = value.trim().parse::<usize>().unwrap();
                        }
                    }
                }
                let mut body = vec![0; length];
                reader.read_exact(&mut body).unwrap();
                let body = serde_json::from_slice::<Value>(&body).unwrap_or(Value::Null);
                let mut w = shared.lock().unwrap();
                w.calls
                    .push(json!({"method":words[0],"route":words[1],"body":body}));
                let reply = answer(&mut w, &words[0], &words[1], &body);
                drop(w);
                if let Some((status, value)) = reply {
                    let text = value.to_string();
                    let _ = write!(
                        socket,
                        "HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{text}",
                        text.len()
                    );
                }
                // None: the request was carried out and the answer is lost.
            }
        });
        let mut device = Iphone {
            rt: tokio::runtime::Runtime::new().unwrap(),
            http: Http { port },
            session: "test".into(),
            width: 0.0,
            height: 0.0,
            orientation: String::new(),
            epoch: Arc::new(std::sync::atomic::AtomicU64::new(0)),
            seen_epoch: 0,
            size_requests: 0,
            gesture: Vec::new(),
            last_touch: None,
            video: None,
            shared_state: None,
            _processes: Vec::new(),
            _lock: None,
        };
        // The runtime worker's own dispatch, as runtime.input reaches it.
        let result = super::super::deliver(&mut device, &a);
        drop(device);
        stop.store(true, Ordering::Relaxed);
        server.join().unwrap();
        let w = state.lock().unwrap();
        let values = w
            .elements
            .iter()
            .map(|(k, e)| (k.clone(), e.value.clone()))
            .collect();
        (result, w.calls.clone(), values)
    }
    fn requests(calls: &[Value], method: &str, suffix: &str) -> usize {
        calls
            .iter()
            .filter(|c| c["method"] == method && s(c, "route").ends_with(suffix))
            .count()
    }
    /// The clears and value writes the double received.
    fn writes(calls: &[Value]) -> (usize, usize) {
        (
            requests(calls, "POST", "/clear"),
            requests(calls, "POST", "/value"),
        )
    }
    /// A screen with the intended field at the tap (0.5, 0.2 is 195, 169 in
    /// points) and another one below it.
    fn screen(field: El) -> Wda {
        let mut w = Wda {
            mode: "accept",
            on_tap: Some("field-1".into()),
            ..Default::default()
        };
        w.elements.insert("field-1".into(), field);
        w.elements.insert(
            "other-1".into(),
            el("XCUIElementTypeTextField", "other", "", "kept", 400.0),
        );
        w
    }
    fn fill(text: &str) -> Value {
        json!({"action":"fill","x":0.5,"y":0.2,"text":text,"verify_value":true,"focus_timeout_ms":400})
    }
    fn code(result: &Result) -> String {
        result
            .as_ref()
            .err()
            .map(|e| e.code.clone())
            .unwrap_or_default()
    }
    fn details(result: &Result) -> Value {
        result
            .as_ref()
            .err()
            .and_then(|e| e.details.as_deref().cloned())
            .unwrap_or(Value::Null)
    }
    const FIELD: &str = "XCUIElementTypeTextField";
    const SEARCH: &str = "XCUIElementTypeSearchField";

    #[test]
    fn a_verified_fill_reads_the_exact_value_back_and_skips_a_matching_one() {
        let (result, calls, values) = fill_on(
            screen(el(FIELD, "email", "Email", "old@example.com", 150.0)),
            fill("ana@example.com"),
        );
        let result = result.unwrap();
        assert_eq!(
            (result["verified"].clone(), result["field_status"].clone()),
            (json!(true), json!("verified"))
        );
        assert_eq!(
            result["writes"],
            json!([{"op":"clear","outcome":"answered"},{"op":"value","outcome":"answered"}])
        );
        assert_eq!(result["editor"]["element"], "field-1");
        assert_eq!(result["before"], json!({"state":"text","chars":15}));
        assert_eq!(writes(&calls), (1, 1));
        assert_eq!(values["field-1"], "ana@example.com");
        // Read before and after the write, on the same element ID.
        assert_eq!(
            requests(&calls, "GET", "/element/field-1/attribute/value"),
            2
        );
        let order: Vec<&str> = calls
            .iter()
            .map(|c| s(c, "route"))
            .filter(|r| r.contains("field-1") && (r.ends_with("/value") || r.ends_with("/clear")))
            .collect();
        assert_eq!(
            order,
            [
                "/session/test/element/field-1/attribute/value",
                "/session/test/element/field-1/clear",
                "/session/test/element/field-1/value",
                "/session/test/element/field-1/attribute/value"
            ]
        );
        // Already holding the text: verified with nothing written.
        let (result, calls, _) = fill_on(
            screen(el(FIELD, "email", "Email", "ana@example.com", 150.0)),
            fill("ana@example.com"),
        );
        let result = result.unwrap();
        assert_eq!(result["field_status"], "already_matching");
        assert_eq!(result["writes"], json!([]));
        assert_eq!(result["typed"], false);
        assert_eq!(writes(&calls), (0, 0));
    }

    #[test]
    fn a_placeholder_equal_to_the_text_is_never_taken_for_entered_text() {
        // Empty, showing "Search": not skipped, written once, and the read-back
        // "Search" cannot be told from the placeholder, so it is uncertain.
        let (result, calls, values) = fill_on(
            screen(el(SEARCH, "query", "Search", "", 150.0)),
            fill("Search"),
        );
        assert_eq!(code(&result), "field_uncertain");
        assert_eq!(details(&result)["reason"], "value_matches_placeholder");
        assert_eq!(details(&result)["before"]["state"], "placeholder");
        assert_eq!(writes(&calls).1, 1, "typed exactly once, never again");
        assert_eq!(values["field-1"], "Search");
        // Clearing a field to its placeholder is verified: no entered text is left.
        let (result, calls, values) = fill_on(
            screen(el(SEARCH, "query", "Search", "old query", 150.0)),
            fill(""),
        );
        let result = result.unwrap();
        assert_eq!(result["value"]["shows_placeholder"], true);
        assert_eq!(
            writes(&calls),
            (1, 0),
            "an empty text clears and types nothing"
        );
        assert_eq!(values["field-1"], "");
    }

    #[test]
    fn focus_must_reach_the_tapped_field_within_the_wait() {
        // The field takes the focus on the fourth look: waited for, then verified.
        let mut w = screen(el(FIELD, "name", "", "", 150.0));
        w.focus_after = 4;
        let (result, _, _) = fill_on(w, fill("Ada"));
        let result = result.unwrap();
        assert!(n(&result, "focus_polls") >= 4, "{result}");
        // Nothing takes the focus: nothing typed.
        let mut w = screen(el(FIELD, "name", "", "", 150.0));
        w.on_tap = None;
        let (result, calls, _) = fill_on(w, fill("Ada"));
        assert_eq!(code(&result), "field_not_focused");
        assert_eq!(details(&result)["typed"], false);
        assert_eq!(writes(&calls), (0, 0));
        // Another field keeps the focus: it does not cover the tap, nothing typed.
        let mut w = screen(el(FIELD, "name", "", "", 150.0));
        w.on_tap = None;
        w.active = Some("other-1".into());
        let (result, calls, values) = fill_on(w, fill("Ada"));
        assert_eq!(code(&result), "field_wrong_target");
        assert_eq!(details(&result)["focused"]["name"], "other");
        assert_eq!(writes(&calls), (0, 0));
        assert_eq!(values["other-1"], "kept");
        // A search field that moves as it takes the focus is found by its name.
        let mut moved = el(SEARCH, "query", "Search", "", 40.0);
        moved.rect.1 = 40.0;
        let mut a = fill("tomacco");
        a["field_name"] = json!("query");
        let (result, _, _) = fill_on(screen(moved.clone()), a.clone());
        assert_eq!(result.unwrap()["field_status"], "verified");
        let (result, _, _) = fill_on(screen(moved), fill("tomacco"));
        assert_eq!(
            code(&result),
            "field_wrong_target",
            "without a name, a field away from the tap is not trusted"
        );
    }

    #[test]
    fn a_replaced_or_unreadable_field_never_reports_success() {
        // Replaced before anything was typed: refused, nothing written.
        let mut w = screen(el(FIELD, "name", "", "old", 150.0));
        w.mode = "stale_value";
        let (result, calls, _) = fill_on(w, fill("Ada"));
        assert_eq!(code(&result), "field_stale");
        assert_eq!(writes(&calls), (0, 0));
        // Replaced as it was written: the text went out once, the outcome is unknown.
        let mut w = screen(el(FIELD, "name", "", "", 150.0));
        w.mode = "replace_after_write";
        let (result, calls, _) = fill_on(w, fill("Ada"));
        assert_eq!(code(&result), "field_uncertain");
        assert_eq!(details(&result)["reason"], "field_replaced");
        assert_eq!(writes(&calls), (0, 1));
        // A different value read back (autocorrection): uncertain, typed once.
        let mut w = screen(el(FIELD, "name", "", "", 150.0));
        w.mode = "autocorrect";
        let (result, calls, values) = fill_on(w, fill("teh cat"));
        assert_eq!(code(&result), "field_uncertain");
        assert_eq!(details(&result)["reason"], "value_mismatch");
        assert_eq!(details(&result)["observed"]["value"], "the cat");
        assert_eq!(writes(&calls), (0, 1));
        assert_eq!(values["field-1"], "the cat");
        // The write was carried out and its answer lost: uncertain, exactly one write.
        let mut w = screen(el(FIELD, "name", "", "", 150.0));
        w.mode = "lose_reply";
        let (result, calls, values) = fill_on(w, fill("Ada"));
        assert_eq!(code(&result), "field_uncertain");
        assert_eq!(details(&result)["reason"], "write_unknown");
        assert_eq!(
            details(&result)["writes"],
            json!([{"op":"value","outcome":"lost_reply"}])
        );
        assert_eq!(details(&result)["retried"], false);
        assert_eq!(writes(&calls), (0, 1));
        assert_eq!(values["field-1"], "Ada", "the double did type it");
        let last = calls
            .iter()
            .rev()
            .find(|c| c["method"] != "DELETE")
            .unwrap();
        assert!(
            s(last, "route").ends_with("/element/field-1/value"),
            "nothing followed the lost write: {last}"
        );
    }

    #[test]
    fn unicode_is_written_and_compared_exactly() {
        let text = "Gr\u{fc}\u{df}e \u{65e5}\u{672c} \u{1f3b5} \u{645}\u{631}\u{62d}\u{628}\u{627} \u{5e9}\u{5dc}\u{5d5}\u{5dd} e\u{301} %s $(literal) \"q\"";
        let (result, calls, values) = fill_on(screen(el(FIELD, "name", "", "", 150.0)), fill(text));
        let result = result.unwrap();
        assert_eq!(result["value"]["chars"], text.chars().count());
        assert_eq!(values["field-1"], text);
        let write = calls
            .iter()
            .find(|c| s(c, "route").ends_with("/element/field-1/value"))
            .unwrap();
        assert_eq!(write["body"]["value"], json!([text]));
        // Precomposed and decomposed é are different texts: not verified.
        let (result, calls, _) = fill_on(
            screen(el(FIELD, "name", "", "caf\u{e9}", 150.0)),
            fill("cafe\u{301}"),
        );
        assert_eq!(result.unwrap()["field_status"], "verified");
        assert_eq!(
            writes(&calls),
            (1, 1),
            "a canonically equal value is still rewritten, not skipped"
        );
    }

    #[test]
    fn secure_and_unsupported_editors_are_refused_unread() {
        let (result, calls, values) = fill_on(
            screen(el(field::SECURE, "password", "Password", "hunter2", 150.0)),
            fill("x"),
        );
        assert_eq!(code(&result), "field_unsupported");
        assert_eq!(details(&result)["reason"], "secure_field");
        assert_eq!(
            requests(&calls, "GET", "/attribute/value"),
            0,
            "a secure field's value is never requested"
        );
        assert_eq!(writes(&calls), (0, 0));
        assert_eq!(values["field-1"], "hunter2");
        assert!(!result.unwrap_err().value().to_string().contains("hunter2"));
        let (result, calls, _) = fill_on(
            screen(el("XCUIElementTypeButton", "Go", "", "", 150.0)),
            fill("x"),
        );
        assert_eq!(code(&result), "field_unsupported");
        assert_eq!(details(&result)["reason"], "not_a_text_editor");
        assert_eq!(writes(&calls), (0, 0));
        let mut disabled = el(FIELD, "name", "", "", 150.0);
        disabled.enabled = false;
        let (result, calls, _) = fill_on(screen(disabled), fill("x"));
        assert_eq!(details(&result)["reason"], "disabled");
        assert_eq!(writes(&calls), (0, 0));
    }

    #[test]
    fn submit_sends_one_return_only_after_the_value_is_verified() {
        let mut a = fill("tomacco");
        a["submit"] = json!(true);
        let (result, calls, _) =
            fill_on(screen(el(SEARCH, "query", "Search", "", 150.0)), a.clone());
        let result = result.unwrap();
        assert_eq!(result["submitted"], true);
        let keys: Vec<&Value> = calls
            .iter()
            .filter(|c| s(c, "route").ends_with("/wda/keys"))
            .collect();
        assert_eq!(keys.len(), 1);
        assert_eq!(keys[0]["body"]["value"], json!(["\n"]));
        let value_at = calls
            .iter()
            .position(|c| s(c, "route").ends_with("/element/field-1/value"))
            .unwrap();
        let keys_at = calls
            .iter()
            .position(|c| s(c, "route").ends_with("/wda/keys"))
            .unwrap();
        assert!(keys_at > value_at);
        // A value that does not match is not submitted.
        let mut w = screen(el(SEARCH, "query", "Search", "", 150.0));
        w.mode = "autocorrect";
        let mut a = fill("teh");
        a["submit"] = json!(true);
        let (result, calls, _) = fill_on(w, a);
        assert_eq!(code(&result), "field_uncertain");
        assert_eq!(requests(&calls, "POST", "/wda/keys"), 0);
        // submit without verify_value is refused before anything is sent.
        let (result, calls, _) = fill_on(
            screen(el(SEARCH, "query", "", "", 150.0)),
            json!({"action":"fill","x":0.5,"y":0.2,"text":"a","submit":true}),
        );
        assert_eq!(code(&result), "invalid_argument");
        assert!(calls.iter().all(|c| c["method"] == "DELETE"), "{calls:?}");
    }

    #[test]
    fn an_unreachable_agent_is_named_and_nothing_counts_as_typed() {
        // No double behind the port: the tap was never sent.
        let port = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
            .unwrap()
            .local_addr()
            .unwrap()
            .port();
        let mut device = Iphone {
            rt: tokio::runtime::Runtime::new().unwrap(),
            http: Http { port },
            session: String::new(),
            width: 0.0,
            height: 0.0,
            orientation: String::new(),
            epoch: Arc::new(std::sync::atomic::AtomicU64::new(0)),
            seen_epoch: 0,
            size_requests: 0,
            gesture: Vec::new(),
            last_touch: None,
            video: None,
            shared_state: None,
            _processes: Vec::new(),
            _lock: None,
        };
        let result = super::super::deliver(&mut device, &fill("Ada"));
        assert_eq!(code(&result), "field_tap_failed");
        assert_eq!(details(&result)["tapped"], false);
        assert_eq!(details(&result)["typed"], false);
        assert_eq!(device.fill_verification()["supported"], true);
    }
    #[test]
    fn jpeg_sizes_are_read_from_the_start_of_frame_header() {
        let mut jpeg = vec![0xff, 0xd8, 0xff, 0xe0, 0x00, 0x04, 0x4a, 0x46];
        jpeg.extend([0xff, 0xc0, 0x00, 0x11, 0x08, 0x03, 0x4c, 0x01, 0x86, 0x03]);
        jpeg.extend([0; 12]);
        assert_eq!(jpeg_size(&jpeg), Some((390, 844)));
        assert_eq!(jpeg_size(b"\xff\xd8\xff\xd9"), None);
        assert_eq!(jpeg_size(b"\x89PNG"), None);
        assert_eq!(
            orientation_facts("UIA_DEVICE_ORIENTATION_LANDSCAPERIGHT"),
            ("landscape", "landscape_right", 3)
        );
    }
    #[test]
    fn fragmented_mjpeg_is_bounded_and_rejects_corrupt_frames() {
        let jpeg = b"\xff\xd8test\xff\xd9";
        let mut wire = format!(
            "--BoundaryString\r\nContent-type: image/jpeg\r\nContent-Length: {}\r\n\r\n",
            jpeg.len()
        )
        .into_bytes();
        wire.extend(jpeg);
        wire.extend(b"\r\n");
        let mut parser = Multipart::default();
        let mut frames = Vec::new();
        for chunk in wire.repeat(2).chunks(3) {
            frames.extend(parser.push(chunk).unwrap());
        }
        assert_eq!(frames, vec![jpeg.to_vec(), jpeg.to_vec()]);
        assert!(
            Multipart::default()
                .push(b"--b\r\nContent-Type: image/jpeg\r\nContent-Length: 999999999\r\n\r\n")
                .is_err()
        );
        assert!(
            Multipart::default()
                .push(b"--b\r\nContent-Type: image/jpeg\r\nContent-Length: 4\r\n\r\nbad!")
                .is_err()
        );
        assert!(Multipart::default().push(&vec![b'x'; 8193]).is_err());
    }
    #[test]
    fn the_hardware_identifier_picks_the_frame() {
        let catalogue = crate::catalog::builtin();
        assert_eq!(profile_for(&catalogue, "iPhone18,4").unwrap()["id"], "air");
        assert!(profile_for(&catalogue, "iPhone1,1").is_none());
        assert!(profile_for(&catalogue, "").is_none());
        for good in ["iPhone18,4", "iPad16,6"] {
            assert!(valid_product_type(good), "{good}");
        }
        for bad in [
            "",
            "iPhone",
            "iPhone18",
            "iPhone18,",
            "iPhone,4",
            "iPhone18,4,1",
            "Watch7,1",
            "iPhone1234,1",
        ] {
            assert!(!valid_product_type(bad), "{bad}");
        }
    }
    #[test]
    fn webdriveragent_mjpeg_parts_are_read() {
        // The exact part framing of WebDriverAgent's MJPEG server: the
        // non-standard "image/jpg" type and "\r\n\r\n" after each picture.
        let jpeg = b"\xff\xd8test\xff\xd9";
        let mut wire = format!(
            "--BoundaryString\r\nContent-type: image/jpg\r\nContent-Length: {}\r\n\r\n",
            jpeg.len()
        )
        .into_bytes();
        wire.extend(jpeg);
        wire.extend(b"\r\n\r\n");
        for size in [1, 3, 7, wire.len() * 3] {
            let mut parser = Multipart::default();
            let mut frames = Vec::new();
            for chunk in wire.repeat(3).chunks(size) {
                frames.extend(parser.push(chunk).unwrap());
            }
            assert_eq!(frames, vec![jpeg.to_vec(); 3], "chunk size {size}");
        }
        assert!(
            Multipart::default()
                .push(b"--b\r\nContent-Type: image/png\r\nContent-Length: 8\r\n\r\n")
                .is_err()
        );
    }
}
