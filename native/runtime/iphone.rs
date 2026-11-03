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
    catalogue.as_array()?.iter().find(|p| {
        p["platform"] == "ios"
            && p["productTypes"]
                .as_array()
                .is_some_and(|t| t.iter().any(|v| v == product_type))
    }).cloned()
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
        .map_err(|_| err("USB connection timed out"))??;
        let (mut sender, connection) = hyper::client::conn::http1::handshake(TokioIo::new(socket))
            .await
            .map_err(|e| err(e.to_string()))?;
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
            .map_err(|e| err(e.to_string()))?;
        tokio::time::timeout(Duration::from_secs(20), sender.send_request(request))
            .await
            .map_err(|_| err("WebDriverAgent did not respond"))?
            .map_err(|e| err(e.to_string()))
    }
    async fn json(&self, method: &str, path: &str, body: Option<&Value>) -> Result {
        let response = self.request(method, path, body).await?;
        let status = response.status();
        let mut incoming = response.into_body();
        let mut bytes = Vec::new();
        while let Some(frame) = tokio::time::timeout(Duration::from_secs(20), incoming.frame())
            .await
            .map_err(|_| err("WebDriverAgent response timed out"))?
        {
            let frame = frame.map_err(|e| err(e.to_string()))?;
            if let Ok(data) = frame.into_data() {
                if bytes.len() + data.len() > LIMIT {
                    return Err(err("WebDriverAgent response exceeds 16 MiB"));
                }
                bytes.extend_from_slice(&data);
            }
        }
        let result: Value = serde_json::from_slice(&bytes)?;
        if !status.is_success() || result["value"]["error"].is_string() {
            return Err(err(result["value"]["message"]
                .as_str()
                .unwrap_or("WebDriverAgent request failed"))
            .details(json!({"status":status.as_u16(),"response":result})));
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

pub struct Iphone {
    rt: tokio::runtime::Runtime,
    http: Http,
    session: String,
    width: f64,
    height: f64,
    gesture: Vec<Value>,
    last_touch: Option<Instant>,
    video: Option<tokio::task::JoinHandle<()>>,
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
    fn call(&self, method: &str, route: &str, body: Option<&Value>) -> Result {
        self.rt
            .block_on(
                self.http
                    .json(method, &format!("/session/{}{route}", self.session), body),
            )
            .map(|v| v["value"].clone())
    }
    fn dimensions(&mut self) -> Result<()> {
        let size = self.call("GET", "/window/size", None)?;
        self.width = number(&size, "width");
        self.height = number(&size, "height");
        if self.width <= 0.0 || self.height <= 0.0 {
            return Err(err("iPhone returned an invalid screen size"));
        }
        Ok(())
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
        self.call("POST", "/actions", Some(&json!({"actions":[{"type":"pointer","id":"tomacco-touch","parameters":{"pointerType":"touch"},"actions":actions}]})))
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
        gesture: Vec::new(),
        last_touch: None,
        video: None,
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
    device.video = Some(device.rt.spawn(async move {
        let mut body = stream.into_body();
        let mut decoder = Multipart::default();
        let result: Result<()> = async {
            while let Some(frame) = tokio::time::timeout(Duration::from_secs(20), body.frame())
                .await
                .map_err(|_| err("iPhone video stalled"))?
            {
                let frame = frame.map_err(|e| err(e.to_string()))?;
                if let Ok(data) = frame.into_data() {
                    for jpeg in decoder.push(&data)? {
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
                self.dimensions()?;
                Ok(result)
            }
            _ => Err(err(
                "This input action is not supported by the physical iPhone transport",
            )),
        }
    }
    fn capture(&mut self, path: &Path) -> Result {
        let data = self.screenshot()?;
        atomic_write(path, &data)?;
        artifact(path, 0.0)
    }
    fn hierarchy(&mut self) -> Result {
        let value = self.call("GET", "/source", None)?;
        Ok(json!({"xml":value,"transport":"usb/wda"}))
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
                if route.ends_with("/orientation") {
                    landscape = body["orientation"] == "LANDSCAPE";
                }
                let value = if route.ends_with("/window/size") {
                    if landscape {
                        json!({"width":844,"height":390})
                    } else {
                        json!({"width":390,"height":844})
                    }
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
        let mut device = Iphone {
            rt: tokio::runtime::Runtime::new().unwrap(),
            http: Http { port },
            session: "test".into(),
            width: 0.0,
            height: 0.0,
            gesture: Vec::new(),
            last_touch: None,
            video: None,
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
        drop(device);
        stopped.store(true, Ordering::Relaxed);
        server.join().unwrap();
        let calls = calls.lock().unwrap();
        let pointer = |v: &Value| v["body"]["actions"][0]["actions"].as_array().cloned().unwrap_or_default();
        let actions = calls
            .iter()
            .filter(|v| s(v, "route").ends_with("/actions"))
            .collect::<Vec<_>>();
        // A tap is a pointer action, so XCTest's idle wait does not delay it.
        let taps = actions
            .iter()
            .filter(|v| pointer(v).iter().any(|a| a["type"] == "pause"))
            .collect::<Vec<_>>();
        assert_eq!(taps.len(), 2);
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
        assert_eq!(calls.last().unwrap()["method"], "DELETE");
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
        for bad in ["", "iPhone", "iPhone18", "iPhone18,", "iPhone,4", "iPhone18,4,1", "Watch7,1", "iPhone1234,1"] {
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
