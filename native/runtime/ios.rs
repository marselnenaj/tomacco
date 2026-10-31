//! Apple Simulator transport: idb gRPC over a private SSH-forwarded Unix socket.
use super::*;
use crate::remote;
use tokio_stream::wrappers::ReceiverStream;
use tonic::transport::{Channel, Endpoint};
#[allow(non_camel_case_types)]
pub mod protocol {
    tonic::include_proto!("idb");
}
use protocol::{companion_service_client::CompanionServiceClient, *};
type Client = CompanionServiceClient<Channel>;

pub struct Ios {
    rt: tokio::runtime::Runtime,
    client: Client,
    video: Option<tokio::task::JoinHandle<()>>,
    stream: Option<tokio::sync::mpsc::Sender<VideoStreamRequest>>,
    shared: Shared,
    _tunnel: Option<ManagedProcess>,
    local_socket: Option<PathBuf>,
    touches: Option<(f64, f64)>,
}
impl Drop for Ios {
    fn drop(&mut self) {
        if let Some(stream) = self.stream.take() {
            let _ = stream.try_send(VideoStreamRequest {
                control: Some(video_stream_request::Control::Stop(Empty {})),
            });
        }
        if let Some(video) = self.video.take() {
            video.abort();
        }
        self._tunnel.take();
        if let Some(path) = &self.local_socket {
            let _ = fs::remove_file(path);
        }
    }
}
fn rpc(error: impl std::fmt::Display) -> Error {
    Error::new("apple_runtime", error.to_string())
}
async fn client(socket: PathBuf) -> Result<Client> {
    let channel = Endpoint::from_static("http://[::]:50051")
        .connect_timeout(Duration::from_secs(3))
        .timeout(Duration::from_secs(15))
        .connect_with_connector(tower::service_fn(move |_| {
            let path = socket.clone();
            async move {
                tokio::net::UnixStream::connect(path)
                    .await
                    .map(hyper_util::rt::TokioIo::new)
            }
        }))
        .await
        .map_err(rpc)?;
    Ok(Client::new(channel).max_decoding_message_size(32 * 1024 * 1024))
}
pub fn connect(a: &Value, shared: Shared, dir: &Path) -> Result<Ios> {
    let config = remote::workspace(s(a, "remote"))?;
    let device = identifier(s(a, "device"), "Simulator UUID", Some("[A-Fa-f0-9-]{36}"))?;
    let session = dir.file_name().unwrap().to_string_lossy();
    let remote_dir = format!("/tmp/tomacco-live-{session}");
    let remote_socket = format!("{remote_dir}/idb.sock");
    let local = super::sockets()?.join(format!("{session}-idb.sock"));
    // Keeping stdin open ties the companion lifetime to SSH even without a PTY.
    // Both Unix sockets reside in owner-only directories; no public RPC listener.
    let script = format!(
        r#"set -eu
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
command -v idb_companion >/dev/null || {{ echo 'Install the Mac companion: brew install facebook/fb/idb' >&2; exit 69; }}
test "$(uname -s)" = Darwin
umask 077
mkdir {directory}
child=''
cleanup() {{ if [ -n "$child" ]; then kill "$child" 2>/dev/null || true; wait "$child" 2>/dev/null || true; fi; rm -f {socket}; rmdir {directory} 2>/dev/null || true; }}
trap cleanup EXIT
trap 'exit 0' HUP INT TERM
xcrun simctl boot {device} >/dev/null 2>&1 || true
xcrun simctl bootstatus {device} -b
idb_companion --udid {device} --grpc-domain-sock {socket} --debug-port 0 --log-level info &
child=$!
cat >/dev/null
"#,
        directory = shell_quote(&remote_dir),
        socket = shell_quote(&remote_socket),
        device = shell_quote(device)
    );
    let args = argv(&[
        &tc::find("ssh")?,
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "StreamLocalBindMask=0177",
        "-L",
        &format!("{}:{remote_socket}", local.display()),
        "--",
        s(&config, "host"),
        &format!("sh -c {}", shell_quote(&script)),
    ]);
    let mut cmd = Command::new(&args[0]);
    let output = File::create(dir.join("companion.log"))?;
    cmd.args(&args[1..])
        .stdin(Stdio::piped())
        .stdout(output.try_clone()?)
        .stderr(output)
        .envs(tc::environment()?)
        .process_group(0);
    tc::parent_bound(&mut cmd);
    let mut tunnel = ManagedProcess(cmd.spawn()?);
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()?;
    let deadline = Instant::now() + Duration::from_secs(75);
    let connection = loop {
        if shared.lock().unwrap().stopped.load(Ordering::Relaxed) {
            let _ = fs::remove_file(&local);
            return Err(Error::new("runtime_stopped", "Connection was cancelled"));
        }
        if tunnel.0.try_wait()?.is_some() {
            return Err(Error::new(
                "apple_companion_failed",
                fs::read_to_string(dir.join("companion.log")).unwrap_or_default(),
            )
            .remedy(
                "Connect a Mac with Xcode and idb_companion; check SSH access with remote.check",
            ));
        }
        if let Ok(mut c) = rt.block_on(client(local.clone())) {
            if let Ok(info) = rt.block_on(c.describe(TargetDescriptionRequest {
                fetch_diagnostics: false,
            })) {
                break (c, info.into_inner());
            }
        }
        if Instant::now() >= deadline {
            let _ = fs::remove_file(&local);
            return Err(Error::new(
                "apple_companion_timeout",
                "The Mac simulator did not become ready within 75 seconds",
            )
            .details(json!({"log":dir.join("companion.log")})));
        }
        thread::sleep(Duration::from_millis(300));
    };
    initialize(
        a,
        shared,
        rt,
        connection.0,
        connection.1,
        Some(tunnel),
        Some(local),
    )
}
fn initialize(
    a: &Value,
    shared: Shared,
    rt: tokio::runtime::Runtime,
    mut client: Client,
    info: TargetDescriptionResponse,
    tunnel: Option<ManagedProcess>,
    local_socket: Option<PathBuf>,
) -> Result<Ios> {
    let target = info
        .target_description
        .ok_or_else(|| rpc("Companion returned no target"))?;
    if target.udid != s(a, "device") {
        return Err(rpc("Companion connected to a different device"));
    }
    let screen = target
        .screen_dimensions
        .ok_or_else(|| rpc("Companion returned no screen dimensions"))?;
    if screen.width == 0
        || screen.height == 0
        || screen.width > 16384
        || screen.height > 16384
        || !screen.density.is_finite()
        || screen.density <= 0.0
    {
        return Err(rpc("Invalid simulator screen dimensions"));
    }
    let points_w = if screen.width_points > 0 {
        screen.width_points as f64
    } else {
        screen.width as f64 / screen.density
    };
    let points_h = if screen.height_points > 0 {
        screen.height_points as f64
    } else {
        screen.height as f64 / screen.density
    };
    shared.lock().unwrap().update(json!({"name":target.name,"os_version":target.os_version,"architecture":target.architecture,"width":screen.width,"height":screen.height,"points_width":points_w,"points_height":points_h,"density":screen.density,"codec":"h264","transport":"ssh/idb","simulator":true,"fps_limit":n(a,"fps")}))?;
    let (tx, rx) = tokio::sync::mpsc::channel(2);
    let response = rt.block_on(async {
        tx.send(VideoStreamRequest {
            control: Some(video_stream_request::Control::Start(
                video_stream_request::Start {
                    fps: n(a, "fps"),
                    format: 0,
                    scale_factor: 1.0,
                    avg_bitrate: n(a, "bit_rate") as f64,
                    key_frame_rate: 1.0,
                    ..Default::default()
                },
            )),
        })
        .await
        .map_err(rpc)?;
        client
            .video_stream(ReceiverStream::new(rx))
            .await
            .map_err(rpc)
    })?;
    let mut stream = response.into_inner();
    let hub = shared.clone();
    let video = rt.spawn(async move {
        loop {
            match stream.message().await {
                Ok(Some(VideoStreamResponse {
                    output:
                        Some(video_stream_response::Output::Payload(Payload {
                            source: Some(payload::Source::Data(data)),
                        })),
                })) => {
                    let (config, key) = nal_flags(&data);
                    hub.lock().unwrap().video(data, config, key);
                }
                Ok(Some(_)) => (),
                result => {
                    let mut h = hub.lock().unwrap();
                    if !h.stopped.load(Ordering::Relaxed) {
                        let message = result
                            .err()
                            .map(|e| e.to_string())
                            .unwrap_or_else(|| "Apple video stream ended".into());
                        let _ = h.update(json!({"error":message}));
                        h.stopped.store(true, Ordering::Relaxed);
                    }
                    break;
                }
            }
        }
    });
    Ok(Ios {
        rt,
        client,
        video: Some(video),
        stream: Some(tx),
        shared,
        _tunnel: tunnel,
        local_socket,
        touches: None,
    })
}
// idb emits Annex-B NAL units. SPS/PPS may be separate payloads.
fn nal_flags(data: &[u8]) -> (bool, bool) {
    let mut config = false;
    let mut key = false;
    for bytes in data.windows(4) {
        if bytes[..3] == [0, 0, 1] {
            match bytes[3] & 31 {
                7 | 8 => config = true,
                5 => key = true,
                _ => (),
            }
        }
    }
    (config, key)
}
fn press(action: hid_event::press_action::Action, direction: i32) -> HidEvent {
    HidEvent {
        event: Some(hid_event::Event::Press(hid_event::Press {
            action: Some(hid_event::PressAction {
                action: Some(action),
            }),
            direction,
        })),
    }
}
fn key(code: u64, direction: i32) -> HidEvent {
    press(
        hid_event::press_action::Action::Key(hid_event::Key { keycode: code }),
        direction,
    )
}
impl Ios {
    fn point(&self, a: &Value, x: &str, y: &str) -> Point {
        let h = self.shared.lock().unwrap();
        let width = a["width"].as_f64().unwrap_or(number(&h.state, "width"));
        let height = a["height"].as_f64().unwrap_or(number(&h.state, "height"));
        let density = number(&h.state, "density");
        Point {
            x: number(a, x) * width / density,
            y: number(a, y) * height / density,
        }
    }
    fn hid(&mut self, events: Vec<HidEvent>) -> Result {
        self.rt
            .block_on(self.client.hid(tokio_stream::iter(events)))
            .map_err(rpc)?;
        Ok(json!({"delivered":true}))
    }
}
impl Device for Ios {
    fn input(&mut self, a: &Value) -> Result {
        let mut events = Vec::new();
        match s(a, "action") {
            "tap" | "touch" => {
                require(a, &["x", "y"])?;
                let point = self.point(a, "x", "y");
                if s(a, "action") == "touch" {
                    require(a, &["phase"])?;
                }
                let phases: Vec<i32> = match s(a, "phase") {
                    "down" => vec![0],
                    "move" => vec![0],
                    "up" | "cancel" => vec![1],
                    _ => vec![0, 1],
                };
                for phase in phases {
                    events.push(press(
                        hid_event::press_action::Action::Touch(hid_event::Touch {
                            point: Some(point),
                        }),
                        phase,
                    ));
                }
                self.touches = Some((point.x, point.y));
            }
            "swipe" => {
                require(a, &["x", "y", "x2", "y2"])?;
                events.push(HidEvent {
                    event: Some(hid_event::Event::Swipe(hid_event::Swipe {
                        start: Some(self.point(a, "x", "y")),
                        end: Some(self.point(a, "x2", "y2")),
                        delta: 1.0,
                        duration: number(a, "duration"),
                    })),
                });
            }
            "key" => {
                require(a, &["key"])?;
                let button = match s(a, "key") {
                    "home" => Some(1),
                    "power" => Some(2),
                    "volume_up" => Some(6),
                    "volume_down" => Some(7),
                    _ => None,
                };
                if let Some(button) = button {
                    for dir in [0, 1] {
                        events.push(press(
                            hid_event::press_action::Action::Button(hid_event::Button { button }),
                            dir,
                        ));
                    }
                } else {
                    let code = match s(a, "key") {
                        "enter" => 40,
                        "escape" => 41,
                        "backspace" => 42,
                        "tab" => 43,
                        "delete" => 76,
                        "right" => 79,
                        "left" => 80,
                        "down" => 81,
                        "up" => 82,
                        _ => return Err(rpc("This key is unavailable on iOS")),
                    };
                    events.extend([key(code, 0), key(code, 1)]);
                }
            }
            "text" => {
                require(a, &["text"])?;
                for ch in s(a, "text").chars() {
                    let (code,shift)=ascii_key(ch).ok_or_else(||Error::new("text_requires_accessibility","For non-ASCII text use runtime.input action=fill with the field coordinates; this replaces its full accessible value"))?;
                    if shift {
                        events.push(key(225, 0));
                    }
                    events.extend([key(code, 0), key(code, 1)]);
                    if shift {
                        events.push(key(225, 1));
                    }
                }
            }
            "fill" => {
                require(a, &["text", "x", "y"])?;
                let request = AccessibilityActionRequest {
                    target: Some(accessibility_action_request::Target::Point(
                        self.point(a, "x", "y"),
                    )),
                    match_key: 0,
                    depth: 0,
                    action: Some(accessibility_action_request::Action::SetValue(
                        accessibility_action_request::SetValue {
                            value: s(a, "text").into(),
                        },
                    )),
                };
                self.rt
                    .block_on(self.client.accessibility_action(request))
                    .map_err(rpc)?;
                return Ok(json!({"delivered":true}));
            }
            "rotate" => {
                events.push(HidEvent {
                    event: Some(hid_event::Event::Orientation(hid_event::Orientation {
                        orientation: match s(a, "orientation") {
                            "portrait" => 0,
                            "portrait_upside_down" => 1,
                            "landscape_left" => 2,
                            "landscape_right" => 3,
                            _ => return Err(rpc("Choose an explicit iOS orientation")),
                        },
                    })),
                });
            }
            "shake" => events.push(HidEvent {
                event: Some(hid_event::Event::Shake(Empty {})),
            }),
            "pinch" => {
                require(a, &["x", "y", "scale"])?;
                events.push(HidEvent {
                    event: Some(hid_event::Event::Pinch(hid_event::Pinch {
                        center: Some(self.point(a, "x", "y")),
                        scale: number(a, "scale"),
                        duration: number(a, "duration"),
                        radius: 100.0,
                    })),
                });
            }
            "hinge" => {
                require(a, &["angle"])?;
                events.push(HidEvent {
                    event: Some(hid_event::Event::Hinge(hid_event::Hinge {
                        angle: number(a, "angle"),
                    })),
                });
            }
            _ => return Err(rpc("Unsupported iOS input action")),
        }
        self.hid(events)
    }
    fn capture(&mut self, path: &Path) -> Result {
        let started = now();
        let image = self
            .rt
            .block_on(self.client.screenshot(ScreenshotRequest { format: 0 }))
            .map_err(rpc)?
            .into_inner();
        publish_png(path, &image.image_data)?;
        artifact(path, started)
    }
    fn hierarchy(&mut self) -> Result {
        let value = self
            .rt
            .block_on(self.client.accessibility_info(AccessibilityInfoRequest {
                point: None,
                format: 1,
            }))
            .map_err(rpc)?
            .into_inner();
        Ok(json!({"platform":"ios","tree":serde_json::from_str::<Value>(&value.json)?}))
    }
}
fn ascii_key(ch: char) -> Option<(u64, bool)> {
    if ch.is_ascii_alphabetic() {
        return Some((
            u64::from(ch.to_ascii_lowercase() as u8 - b'a') + 4,
            ch.is_ascii_uppercase(),
        ));
    }
    if ('1'..='9').contains(&ch) {
        return Some((u64::from(ch as u8 - b'1') + 30, false));
    }
    if ch == '0' {
        return Some((39, false));
    }
    for (plain, shift, code) in [
        (' ', ' ', 44),
        ('-', '_', 45),
        ('=', '+', 46),
        ('[', '{', 47),
        (']', '}', 48),
        ('\\', '|', 49),
        (';', ':', 51),
        ('\'', '"', 52),
        ('`', '~', 53),
        (',', '<', 54),
        ('.', '>', 55),
        ('/', '?', 56),
        ('\n', '\n', 40),
        ('\t', '\t', 43),
    ] {
        if ch == plain {
            return Some((code, false));
        }
        if ch == shift {
            return Some((code, true));
        }
    }
    "!@#$%^&*()"
        .chars()
        .position(|c| c == ch)
        .map(|i| (30 + i as u64, true))
}

#[cfg(test)]
mod tests {
    use super::*;
    use protocol::companion_service_server::{CompanionService, CompanionServiceServer};
    use tonic::{Request, Response, Status};
    type Reply<T> = std::result::Result<Response<T>, Status>;
    #[derive(Clone, Default)]
    struct Companion {
        events: Arc<Mutex<Vec<HidEvent>>>,
        values: Arc<Mutex<Vec<AccessibilityActionRequest>>>,
    }
    fn description() -> TargetDescriptionResponse {
        TargetDescriptionResponse {
            target_description: Some(TargetDescription {
                udid: "11111111-2222-3333-4444-555555555555".into(),
                name: "Protocol fixture".into(),
                screen_dimensions: Some(ScreenDimensions {
                    width: 1170,
                    height: 2532,
                    density: 3.0,
                    width_points: 390,
                    height_points: 844,
                }),
                state: "Booted".into(),
                target_type: "simulator".into(),
                os_version: "fixture".into(),
                architecture: "arm64".into(),
            }),
        }
    }
    #[tonic::async_trait]
    #[allow(non_camel_case_types)]
    impl CompanionService for Companion {
        async fn describe(
            &self,
            _: Request<TargetDescriptionRequest>,
        ) -> Reply<TargetDescriptionResponse> {
            Ok(Response::new(description()))
        }
        type video_streamStream = ReceiverStream<std::result::Result<VideoStreamResponse, Status>>;
        async fn video_stream(
            &self,
            request: Request<tonic::Streaming<VideoStreamRequest>>,
        ) -> Reply<Self::video_streamStream> {
            let mut incoming = request.into_inner();
            let start = incoming.message().await?.unwrap();
            assert!(matches!(
                start.control,
                Some(video_stream_request::Control::Start(
                    video_stream_request::Start {
                        fps: 60,
                        format: 0,
                        ..
                    }
                ))
            ));
            let (tx, rx) = tokio::sync::mpsc::channel(8);
            tokio::spawn(async move {
                for bytes in [
                    vec![0, 0, 0, 1, 0x67, 1],
                    vec![0, 0, 0, 1, 0x68, 2],
                    vec![0, 0, 0, 1, 0x65, 3],
                    vec![0, 0, 0, 1, 0x61, 4],
                ] {
                    tx.send(Ok(VideoStreamResponse {
                        output: Some(video_stream_response::Output::Payload(Payload {
                            source: Some(payload::Source::Data(bytes)),
                        })),
                    }))
                    .await
                    .unwrap();
                }
                let _ = incoming.message().await;
                drop(tx);
            });
            Ok(Response::new(ReceiverStream::new(rx)))
        }
        async fn hid(&self, request: Request<tonic::Streaming<HidEvent>>) -> Reply<HidResponse> {
            let mut events = request.into_inner();
            while let Some(event) = events.message().await? {
                self.events.lock().unwrap().push(event)
            }
            Ok(Response::new(HidResponse {}))
        }
        async fn screenshot(&self, _: Request<ScreenshotRequest>) -> Reply<ScreenshotResponse> {
            use base64::Engine;
            let image=base64::engine::general_purpose::STANDARD.decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a83sAAAAASUVORK5CYII=").unwrap();
            Ok(Response::new(ScreenshotResponse {
                image_data: image,
                image_format: "png".into(),
            }))
        }
        async fn accessibility_info(
            &self,
            _: Request<AccessibilityInfoRequest>,
        ) -> Reply<AccessibilityInfoResponse> {
            Ok(Response::new(AccessibilityInfoResponse {
                json: r#"[{"label":"Settings","type":"Button"}]"#.into(),
            }))
        }
        async fn accessibility_action(
            &self,
            request: Request<AccessibilityActionRequest>,
        ) -> Reply<AccessibilityActionResponse> {
            self.values.lock().unwrap().push(request.into_inner());
            Ok(Response::new(AccessibilityActionResponse {}))
        }
    }
    #[test]
    fn apple_grpc_contract_video_hid_capture_and_accessibility() {
        // Protocol acceptance only. This deliberately does not claim an Apple runtime test.
        let temp = tempfile::tempdir().unwrap();
        let socket = temp.path().join("idb.sock");
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();
        let server = Companion::default();
        let observed = server.clone();
        let listener = {
            let _entered = rt.enter();
            tokio::net::UnixListener::bind(&socket).unwrap()
        };
        rt.spawn(async move {
            tonic::transport::Server::builder()
                .add_service(CompanionServiceServer::new(server))
                .serve_with_incoming(tokio_stream::wrappers::UnixListenerStream::new(listener))
                .await
                .unwrap();
        });
        let mut client = rt.block_on(super::client(socket)).unwrap();
        let info = rt
            .block_on(client.describe(TargetDescriptionRequest {
                fetch_diagnostics: false,
            }))
            .unwrap()
            .into_inner();
        let shared = Arc::new(Mutex::new(Hub {
            state: json!({}),
            stopped: Arc::new(AtomicBool::new(false)),
            state_file: temp.path().join("state.json"),
            clients: vec![],
            config: vec![],
            gop: vec![],
            cached_bytes: 0,
            receiving_config: false,
            cached_key: false,
        }));
        let a =
            json!({"device":"11111111-2222-3333-4444-555555555555","fps":60,"bit_rate":12000000});
        let mut device = initialize(&a, shared.clone(), rt, client, info, None, None).unwrap();
        device
            .input(&json!({"action":"tap","x":0.5,"y":0.5}))
            .unwrap();
        let events = observed.events.lock().unwrap().clone();
        assert_eq!(events.len(), 2);
        let Some(hid_event::Event::Press(press)) = &events[0].event else {
            panic!("expected touch")
        };
        let Some(hid_event::press_action::Action::Touch(touch)) =
            &press.action.as_ref().unwrap().action
        else {
            panic!("expected touch")
        };
        assert_eq!(touch.point, Some(Point { x: 195.0, y: 422.0 }));
        device
            .input(&json!({"action":"text","text":"Az!"}))
            .unwrap();
        let before = observed.events.lock().unwrap().len();
        assert!(device.input(&json!({"action":"text","text":"Ä"})).is_err());
        assert_eq!(observed.events.lock().unwrap().len(), before);
        device
            .input(&json!({"action":"fill","x":0.25,"y":0.75,"text":"Grüße 日本 🎵"}))
            .unwrap();
        assert!(
            matches!(&observed.values.lock().unwrap()[0].action,Some(accessibility_action_request::Action::SetValue(value)) if value.value=="Grüße 日本 🎵")
        );
        assert_eq!(device.hierarchy().unwrap()["tree"][0]["label"], "Settings");
        let capture = temp.path().join("capture.png");
        assert!(device.capture(&capture).unwrap()["bytes"].as_u64().unwrap() > 24);
        device
            .input(&json!({"action":"rotate","orientation":"landscape_left"}))
            .unwrap();
        device
            .input(&json!({"action":"tap","x":0.5,"y":0.5,"width":2532,"height":1170}))
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(3);
        while n(&shared.lock().unwrap().state, "packets") < 4 {
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(10));
        }
        let hub = shared.lock().unwrap();
        assert_eq!(hub.config.len(), 2);
        assert_eq!(hub.gop.len(), 2);
        assert_eq!(hub.state["width"], 1170);
        drop(hub);
        drop(device);
    }
}
