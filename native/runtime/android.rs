//! scrcpy 4.1 transport. The separately installed upstream server supplies OS pixels.
use super::*;
use crate::devices;
use std::net::{Shutdown, TcpStream};

pub fn server_path(a: &Value) -> Result<PathBuf> {
    if !s(a, "server").is_empty() {
        return path(s(a, "server"), false, true);
    }
    let client = PathBuf::from(tc::find("scrcpy")?);
    for candidate in [
        client.parent().unwrap().join("scrcpy-server"),
        client
            .parent()
            .unwrap()
            .join("../share/scrcpy/scrcpy-server"),
        PathBuf::from("/usr/share/scrcpy/scrcpy-server"),
    ] {
        if candidate.is_file() {
            return Ok(candidate.canonicalize()?);
        }
    }
    Err(Error::new(
        "scrcpy_server_missing",
        "The native Android view requires the scrcpy 4.1 server",
    )
    .remedy("Install scrcpy 4.1 or pass the server JAR path to runtime.start"))
}
struct Forward {
    device: String,
    port: u16,
    jar: String,
}
pub fn recover(state: &Value) -> Result<()> {
    let port = n(state, "adb_port");
    let scid = n(state, "scrcpy_id");
    if port == 0 || port > 65535 || scid > 0x7fff_ffff {
        return Ok(());
    }
    let mut command = devices::adb(s(state, "device"))?;
    command.extend(argv(&["forward", "--list"]));
    let current = tc::run(&command, 5, None, true)?;
    let expected = format!(
        "{} tcp:{port} localabstract:scrcpy_{scid:08x}",
        s(state, "device")
    );
    // A reused ADB port belongs to its new owner and must not be removed.
    if s(&current, "stdout")
        .lines()
        .any(|line| line.trim() == expected)
    {
        let _cleanup = Forward {
            device: s(state, "device").into(),
            port: port as u16,
            jar: format!("/data/local/tmp/tomacco-live-{scid:08x}.jar"),
        };
    }
    Ok(())
}
impl Drop for Forward {
    fn drop(&mut self) {
        if let Ok(mut command) = devices::adb(&self.device) {
            command.extend(argv(&[
                "forward",
                "--remove",
                &format!("tcp:{}", self.port),
            ]));
            let _ = tc::run(&command, 5, None, false);
        }
        if let Ok(command) = devices::shell(&self.device, &argv(&["rm", "-f", &self.jar])) {
            let _ = tc::run(&command, 5, None, false);
        }
    }
}
pub struct Android {
    control: TcpStream,
    video: TcpStream,
    shared: Shared,
    device: String,
    _server: ManagedProcess,
    _forward: Forward,
}
impl Drop for Android {
    fn drop(&mut self) {
        let _ = self.control.shutdown(Shutdown::Both);
        let _ = self.video.shutdown(Shutdown::Both);
    }
}
pub fn connect(a: &Value, shared: Shared, dir: &Path) -> Result<Android> {
    let device = s(a, "device");
    let scid = (uuid::Uuid::new_v4().as_u128() as u32) & 0x7fff_ffff;
    let jar = format!("/data/local/tmp/tomacco-live-{scid:08x}.jar");
    let mut push = devices::adb(device)?;
    push.extend(vec![
        "push".into(),
        server_path(a)?.to_string_lossy().into_owned(),
        jar.clone(),
    ]);
    tc::run(&push, 20, None, true)?;
    let mut forward = devices::adb(device)?;
    forward.extend(argv(&[
        "forward",
        "tcp:0",
        &format!("localabstract:scrcpy_{scid:08x}"),
    ]));
    let response = tc::run(&forward, 10, None, true)?;
    let port = s(&response, "stdout")
        .trim()
        .parse::<u16>()
        .map_err(|_| Error::new("adb_forward", "ADB did not allocate a forwarding port"))?;
    let guard = Forward {
        device: device.into(),
        port,
        jar: jar.clone(),
    };
    shared
        .lock()
        .unwrap()
        .update(json!({"adb_port":port,"scrcpy_id":scid}))?;
    let args = vec![
        "env".into(),
        format!("CLASSPATH={jar}"),
        "app_process".into(),
        "/".into(),
        "com.genymobile.scrcpy.Server".into(),
        "4.1".into(),
        format!("scid={scid:08x}"),
        "tunnel_forward=true".into(),
        "audio=false".into(),
        "video_codec=h264".into(),
        format!("max_fps={}", n(a, "fps")),
        format!("video_bit_rate={}", n(a, "bit_rate")),
        "clipboard_autosync=false".into(),
        "video_codec_options=i-frame-interval=1".into(),
    ];
    let mut server = spawn(&devices::shell(device, &args)?, &dir.join("server.log"))?;
    let deadline = Instant::now() + Duration::from_secs(15);
    let mut video = loop {
        if shared.lock().unwrap().stopped.load(Ordering::Relaxed) {
            return Err(Error::new("runtime_stopped", "Connection was cancelled"));
        }
        if server.0.try_wait()?.is_some() {
            return Err(Error::new(
                "scrcpy_failed",
                format!(
                    "Android display server exited: {}",
                    fs::read_to_string(dir.join("server.log")).unwrap_or_default()
                ),
            )
            .remedy("Tomacco uses the scrcpy 4.1 wire protocol; install the matching server"));
        }
        if let Ok(mut stream) = TcpStream::connect(("127.0.0.1", port)) {
            stream.set_read_timeout(Some(Duration::from_millis(500)))?;
            let mut dummy = [255];
            if stream.read_exact(&mut dummy).is_ok() && dummy == [0] {
                break stream;
            }
        }
        if Instant::now() > deadline {
            return Err(Error::new(
                "scrcpy_timeout",
                "Android display server did not connect",
            ));
        }
        thread::sleep(Duration::from_millis(100));
    };
    // scrcpy sends its device/codec headers only after both sockets are accepted.
    let control = TcpStream::connect(("127.0.0.1", port))?;
    control.set_nodelay(true)?;
    control.set_write_timeout(Some(Duration::from_secs(2)))?;
    video.set_read_timeout(Some(Duration::from_secs(10)))?;
    let mut name = [0u8; 64];
    video.read_exact(&mut name)?;
    let mut codec = [0u8; 4];
    video.read_exact(&mut codec)?;
    if codec != *b"h264" {
        return Err(Error::new(
            "unsupported_codec",
            "Android did not negotiate H.264",
        ));
    }
    let device_name = String::from_utf8_lossy(&name)
        .trim_end_matches('\0')
        .to_owned();
    let mut header = [0u8; 12];
    video.read_exact(&mut header)?;
    if header[0] & 0x80 == 0 {
        return Err(Error::new(
            "scrcpy_protocol",
            "Expected scrcpy 4.1 display metadata",
        ));
    }
    let (width, height) = dimensions(&header)?;
    shared.lock().unwrap().update(json!({"name":device_name,"width":width,"height":height,"codec":"h264","transport":"adb/scrcpy-4.1","packetization":"access-unit","fps_limit":n(a,"fps")}))?;
    // An idle Android display need not emit frames. Drop shuts down this socket.
    video.set_read_timeout(None)?;
    let mut reader = video.try_clone()?;
    let hub = shared.clone();
    thread::spawn(move || {
        let result = (|| -> Result<()> {
            loop {
                let mut header = [0u8; 12];
                reader.read_exact(&mut header)?;
                if header[0] & 0x80 != 0 {
                    let (width, height) = dimensions(&header)?;
                    hub.lock()
                        .unwrap()
                        .update(json!({"width":width,"height":height}))?;
                    continue;
                }
                let flags = u64::from_be_bytes(header[..8].try_into().unwrap());
                let size = u32::from_be_bytes(header[8..].try_into().unwrap()) as usize;
                if size == 0 || size > 16 * 1024 * 1024 {
                    return Err(Error::new(
                        "video_protocol",
                        "Invalid Android video packet length",
                    ));
                }
                let mut bytes = vec![0; size];
                reader.read_exact(&mut bytes)?;
                hub.lock()
                    .unwrap()
                    .video(bytes, flags & (1 << 62) != 0, flags & (1 << 61) != 0);
            }
        })();
        let mut h = hub.lock().unwrap();
        if !h.stopped.load(Ordering::Relaxed) {
            let _ = h.update(json!({"error":result.unwrap_err().message}));
            h.stopped.store(true, Ordering::Relaxed);
        }
    });
    let mut drain = control.try_clone()?;
    thread::spawn(move || {
        let _ = std::io::copy(&mut drain, &mut std::io::sink());
    });
    Ok(Android {
        control,
        video,
        shared,
        device: device.into(),
        _server: server,
        _forward: guard,
    })
}
/// scrcpy's limit for one clipboard text, in bytes.
pub const PASTE_LIMIT: usize = (1 << 18) - 14;
/// A scrcpy SET_CLIPBOARD message that also presses paste. A nonzero
/// sequence asks the server to acknowledge once the clipboard is set.
pub fn clipboard_packet(sequence: u64, text: &str) -> Vec<u8> {
    let bytes = text.as_bytes();
    let mut packet = vec![9];
    packet.extend(sequence.to_be_bytes());
    packet.push(1);
    packet.extend((bytes.len() as u32).to_be_bytes());
    packet.extend(bytes);
    packet
}
/// Reads device messages until the clipboard acknowledgement of `sequence`.
fn await_clipboard_ack(stream: &mut impl Read, sequence: u64) -> Result<()> {
    loop {
        let mut kind = [0u8];
        stream.read_exact(&mut kind)?;
        match kind[0] {
            // Device clipboard text, which autosync off should not send.
            0 => {
                let mut length = [0u8; 4];
                stream.read_exact(&mut length)?;
                let length = u32::from_be_bytes(length) as u64;
                std::io::copy(&mut stream.take(length), &mut std::io::sink())?;
            }
            1 => {
                let mut value = [0u8; 8];
                stream.read_exact(&mut value)?;
                if u64::from_be_bytes(value) == sequence {
                    return Ok(());
                }
            }
            _ => {
                return Err(Error::new(
                    "scrcpy_protocol",
                    "Unexpected message from the Android control server",
                ));
            }
        }
    }
}
/// Pastes UTF-8 text into the focused field without a display session. A
/// control-only scrcpy server sets the device clipboard and presses paste;
/// `adb shell input text` cannot type characters outside ASCII.
pub fn paste(device: &str, text: &str) -> Result {
    if text.len() > PASTE_LIMIT {
        return Err(Error::new(
            "text_too_long",
            format!("Text through the clipboard is limited to {PASTE_LIMIT} bytes"),
        ));
    }
    let server = server_path(&json!({})).map_err(|error| {
        Error::new(
            "text_unsupported",
            "Text outside ASCII goes through the device clipboard, which needs the scrcpy 4.1 server",
        )
        .remedy("Configure scrcpy with settings.configure, or run testing.setup and use testing.action with fill")
        .details(error.value())
    })?;
    let sdk = tc::run(
        &devices::shell(device, &argv(&["getprop", "ro.build.version.sdk"]))?,
        10,
        None,
        true,
    )?;
    if s(&sdk, "stdout").trim().parse::<u64>().unwrap_or(0) < 24 {
        return Err(Error::new(
            "text_unsupported",
            "Pasting text outside ASCII needs Android 7 (API 24) or newer",
        )
        .remedy("Run testing.setup and use testing.action with fill"));
    }
    let scid = (uuid::Uuid::new_v4().as_u128() as u32) & 0x7fff_ffff;
    let jar = format!("/data/local/tmp/tomacco-paste-{scid:08x}.jar");
    let mut push = devices::adb(device)?;
    push.extend(vec![
        "push".into(),
        server.to_string_lossy().into_owned(),
        jar.clone(),
    ]);
    tc::run(&push, 20, None, true)?;
    let mut forward = devices::adb(device)?;
    forward.extend(argv(&[
        "forward",
        "tcp:0",
        &format!("localabstract:scrcpy_{scid:08x}"),
    ]));
    let response = tc::run(&forward, 10, None, true)?;
    let port = s(&response, "stdout")
        .trim()
        .parse::<u16>()
        .map_err(|_| Error::new("adb_forward", "ADB did not allocate a forwarding port"))?;
    let _forward = Forward {
        device: device.into(),
        port,
        jar: jar.clone(),
    };
    let logs = tempfile::tempdir()?;
    let log = logs.path().join("server.log");
    let args = vec![
        "env".into(),
        format!("CLASSPATH={jar}"),
        "app_process".into(),
        "/".into(),
        "com.genymobile.scrcpy.Server".into(),
        "4.1".into(),
        format!("scid={scid:08x}"),
        "tunnel_forward=true".into(),
        "video=false".into(),
        "audio=false".into(),
        "control=true".into(),
        "send_device_meta=false".into(),
        "clipboard_autosync=false".into(),
        "cleanup=false".into(),
    ];
    let mut server = spawn(&devices::shell(device, &args)?, &log)?;
    let deadline = Instant::now() + Duration::from_secs(15);
    let mut control = loop {
        if server.0.try_wait()?.is_some() {
            return Err(Error::new(
                "scrcpy_failed",
                format!(
                    "Android control server exited: {}",
                    fs::read_to_string(&log).unwrap_or_default()
                ),
            )
            .remedy("Tomacco uses the scrcpy 4.1 wire protocol; install the matching server"));
        }
        if let Ok(mut stream) = TcpStream::connect(("127.0.0.1", port)) {
            stream.set_read_timeout(Some(Duration::from_millis(500)))?;
            let mut dummy = [255];
            if stream.read_exact(&mut dummy).is_ok() && dummy == [0] {
                break stream;
            }
        }
        if Instant::now() > deadline {
            return Err(Error::new(
                "scrcpy_timeout",
                "Android control server did not connect",
            ));
        }
        thread::sleep(Duration::from_millis(100));
    };
    control.set_nodelay(true)?;
    control.set_write_timeout(Some(Duration::from_secs(5)))?;
    control.set_read_timeout(Some(Duration::from_secs(10)))?;
    let sequence = (uuid::Uuid::new_v4().as_u128() as u64) | 1;
    control.write_all(&clipboard_packet(sequence, text))?;
    await_clipboard_ack(&mut control, sequence).map_err(|error| {
        Error::new(
            "paste_unconfirmed",
            "Android did not confirm the clipboard text",
        )
        .details(error.value())
    })?;
    // The server injects paste asynchronously before it acknowledges; give the
    // event a moment to leave the server process before it is stopped.
    thread::sleep(Duration::from_millis(200));
    let _ = control.shutdown(Shutdown::Both);
    drop(server);
    Ok(json!({"delivered":true,"method":"clipboard","bytes":text.len()}))
}
fn dimensions(header: &[u8; 12]) -> Result<(u32, u32)> {
    let width = u32::from_be_bytes(header[4..8].try_into().unwrap());
    let height = u32::from_be_bytes(header[8..12].try_into().unwrap());
    if width == 0 || height == 0 || width > 16384 || height > 16384 {
        return Err(Error::new("video_protocol", "Invalid display dimensions"));
    }
    Ok((width, height))
}
impl Android {
    fn touch(&mut self, a: &Value, phase: u8) -> Result<()> {
        let h = self.shared.lock().unwrap();
        let width = n(&h.state, "width") as u16;
        let height = n(&h.state, "height") as u16;
        let x = (number(a, "x") * f64::from(width - 1)).round() as u32;
        let y = (number(a, "y") * f64::from(height - 1)).round() as u32;
        drop(h);
        let mut packet = vec![2, phase];
        packet.extend(0u64.to_be_bytes());
        packet.extend(x.to_be_bytes());
        packet.extend(y.to_be_bytes());
        packet.extend(width.to_be_bytes());
        packet.extend(height.to_be_bytes());
        packet.extend(
            (if phase == 1 || phase == 3 {
                0u16
            } else {
                65535u16
            })
            .to_be_bytes(),
        );
        packet.extend([0; 8]);
        self.control.write_all(&packet)?;
        Ok(())
    }
    fn key(&mut self, key: u32) -> Result<()> {
        for direction in [0, 1] {
            let mut packet = vec![0, direction];
            packet.extend(key.to_be_bytes());
            packet.extend([0; 8]);
            self.control.write_all(&packet)?;
        }
        Ok(())
    }
}
impl Device for Android {
    fn input(&mut self, a: &Value) -> Result {
        match s(a, "action") {
            "touch" => {
                require(a, &["x", "y", "phase"])?;
                self.touch(
                    a,
                    match s(a, "phase") {
                        "down" => 0,
                        "up" => 1,
                        "move" => 2,
                        _ => 3,
                    },
                )?;
            }
            "tap" => {
                require(a, &["x", "y"])?;
                self.touch(a, 0)?;
                self.touch(a, 1)?;
            }
            "swipe" => {
                require(a, &["x", "y", "x2", "y2"])?;
                let duration = number(a, "duration");
                let steps = (duration * 60.0).ceil().max(1.0) as u32;
                self.touch(a, 0)?;
                for i in 1..=steps {
                    let mut p = a.clone();
                    let t = f64::from(i) / f64::from(steps);
                    p["x"] = json!(number(a, "x") + (number(a, "x2") - number(a, "x")) * t);
                    p["y"] = json!(number(a, "y") + (number(a, "y2") - number(a, "y")) * t);
                    thread::sleep(Duration::from_secs_f64(duration / f64::from(steps)));
                    self.touch(&p, if i == steps { 1 } else { 2 })?;
                }
            }
            "key" => {
                require(a, &["key"])?;
                let key = match s(a, "key") {
                    "home" => 3,
                    "back" | "escape" => 4,
                    "up" => 19,
                    "down" => 20,
                    "left" => 21,
                    "right" => 22,
                    "enter" => 66,
                    "backspace" => 67,
                    "delete" => 112,
                    "tab" => 61,
                    "power" => 26,
                    "volume_up" => 24,
                    "volume_down" => 25,
                    "apps" => 187,
                    _ => return Err(Error::new("unsupported_key", "Unknown Android key")),
                };
                self.key(key)?;
            }
            "text" => {
                require(a, &["text"])?;
                // Android's input-text protocol is ASCII-only. Clipboard paste retains Unicode.
                self.control.write_all(&clipboard_packet(0, s(a, "text")))?;
            }
            "rotate" => self.control.write_all(&[11])?,
            _ => {
                return Err(Error::new(
                    "unsupported_input",
                    "This action is not supported by the Android runtime",
                ));
            }
        }
        Ok(json!({"delivered":true}))
    }
    fn capture(&mut self, target: &Path) -> Result {
        let start = now();
        fs::create_dir_all(target.parent().unwrap())?;
        let temp = tempfile::Builder::new()
            .suffix(".png")
            .tempfile_in(target.parent().unwrap())?;
        let mut args = devices::adb(&self.device)?;
        args.extend(argv(&["exec-out", "screencap", "-p"]));
        let mut command = Command::new(&args[0]);
        command
            .args(&args[1..])
            .envs(tc::environment()?)
            .stdin(Stdio::null())
            .stdout(temp.reopen()?)
            .stderr(Stdio::null())
            .process_group(0);
        tc::parent_bound(&mut command);
        let mut child = ManagedProcess(command.spawn()?);
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            if let Some(status) = child.0.try_wait()? {
                if !status.success() {
                    return Err(Error::new("capture_failed", "ADB screenshot failed"));
                }
                break;
            }
            if Instant::now() > deadline {
                return Err(Error::new("capture_timeout", "ADB screenshot timed out"));
            }
            thread::sleep(Duration::from_millis(20));
        }
        let bytes = fs::read(temp.path())?;
        publish_png(target, &bytes)?;
        artifact(target, start)
    }
    fn hierarchy(&mut self) -> Result {
        devices::handle("android.hierarchy", &json!({"device":self.device}))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clipboard_packets_carry_utf8_paste_and_their_sequence() {
        let packet = clipboard_packet(7, "Grüße 👋");
        assert_eq!(packet[0], 9);
        assert_eq!(u64::from_be_bytes(packet[1..9].try_into().unwrap()), 7);
        assert_eq!(packet[9], 1, "paste after setting the clipboard");
        let length = u32::from_be_bytes(packet[10..14].try_into().unwrap()) as usize;
        assert_eq!(length, "Grüße 👋".len());
        assert_eq!(std::str::from_utf8(&packet[14..]).unwrap(), "Grüße 👋");
    }

    #[test]
    fn only_the_matching_acknowledgement_completes_a_paste() {
        let mut stream: Vec<u8> = vec![0, 0, 0, 0, 2, b'h', b'i', 1];
        stream.extend(3u64.to_be_bytes());
        stream.push(1);
        stream.extend(5u64.to_be_bytes());
        assert!(await_clipboard_ack(&mut stream.as_slice(), 5).is_ok());
        let mut stream: Vec<u8> = vec![1];
        stream.extend(3u64.to_be_bytes());
        assert!(await_clipboard_ack(&mut stream.as_slice(), 5).is_err());
        assert!(await_clipboard_ack(&mut [7u8].as_slice(), 5).is_err());
    }
}
