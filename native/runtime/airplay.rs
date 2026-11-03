//! AirPlay screen mirroring from an iPhone over Wi-Fi. UxPlay receives the
//! mirror and hands its H.264 video, unchanged, to this session over a local
//! TCP socket; the desktop decodes it as it decodes the Simulator's stream.
use super::*;
use std::io::Read;
use std::net::{Ipv4Addr, TcpListener};

/// The device name that asks runtime.start for the AirPlay receiver.
pub const DEVICE: &str = "airplay";
/// The name the iPhone lists under Screen Mirroring.
const RECEIVER: &str = "Tomacco";

fn err(message: impl Into<String>) -> Error {
    Error::new("airplay_runtime", message)
}
pub fn requested(a: &Value) -> bool {
    s(a, "platform") == "ios" && s(a, "remote").is_empty() && s(a, "device") == DEVICE
}
pub fn validate(_a: &Value) -> Result<()> {
    tc::find("uxplay")?;
    Ok(())
}

pub struct Airplay {
    _process: ManagedProcess,
    shared: Shared,
    /// The Bluetooth pointer and keyboard that carry input, when BlueZ serves them.
    hid: Option<bluetooth::Hid>,
    /// Where the phone's pointer is, 0..1 of the picture, and since when.
    last: Option<(f64, f64, Instant)>,
    /// When the last pointer report went out, to pace them.
    sent: Option<Instant>,
    held: u8,
}
// The pointer is absolute (bluetooth::POINTER): it sits where the desktop's
// is. Timings measured by Jalrux's iphone-control on iOS 26.5.2 (MIT):
/// A press lands where the pointer was before the report, so it rests at the
/// point this long first (20 ms missed one press in two).
const SETTLE: Duration = Duration::from_millis(60);
/// A tap's press to release.
const HOLD: Duration = Duration::from_millis(50);
/// About one report per BLE connection event; faster ones queue up as lag.
const PACE: Duration = Duration::from_millis(8);
/// A swipe moves the pointer this often.
const DRAG_STEP: Duration = Duration::from_millis(12);

pub fn connect(a: &Value, shared: Shared, dir: &Path) -> Result<Airplay> {
    validate(a)?;
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))?;
    let port = listener.local_addr()?.port();
    let fps = n(a, "fps").clamp(1, 60);
    // -vrtp replaces UxPlay's own decoder and window: the mirror's H.264 goes
    // to this session as an Annex-B byte stream, with SPS/PPS before each IDR.
    let pipeline = format!(
        "config-interval=1 ! rtph264depay ! h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream ! tcpclientsink host=127.0.0.1 port={port}"
    );
    let log = dir.join("uxplay.log");
    // UxPlay buffers its log when it is not a terminal; line buffering lets the
    // session read who connected while the mirror runs.
    let process = spawn(
        &argv(&[
            &tc::find("stdbuf")?,
            "-oL",
            "-eL",
            &tc::find("uxplay")?,
            "-n",
            RECEIVER,
            "-nh",
            // The fixed ports a firewall rule can name: TCP 7000-7001, 7100, UDP 6000-6001, 7011.
            "-p",
            "-vsync",
            "no",
            "-fps",
            &fps.to_string(),
            // An iPhone Air's own panel; the phone fits its picture into this size.
            "-s",
            "1260x2736@60",
            // The phone's own reports of the frame rate it sends, in the log.
            "-FPSdata",
            "-vrtp",
            &pipeline,
        ]),
        &log,
    )?;
    shared.lock().unwrap().update(json!({"name":"iPhone (AirPlay)","transport":"wifi/airplay","codec":"h264","receiver":RECEIVER,"mirroring":false,"mac_required":false}))?;
    // The session ends when UxPlay does, with its own reason, such as ports
    // another receiver holds.
    let pid = process.0.id();
    let watched = shared.clone();
    // Bluetooth pairing stays open while a phone mirrors here.
    let pairing = Arc::new(AtomicBool::new(false));
    let mirror_pairing = pairing.clone();
    let reason_log = log.clone();
    thread::spawn(move || {
        // UxPlay's video pipeline connects when it starts, before any phone
        // does: who mirrors is known only once its log names the phone.
        let mut seen = String::new();
        let mut mirrored = false;
        while Path::new(&format!("/proc/{pid}")).exists()
            && !fs::read_to_string(format!("/proc/{pid}/stat"))
                .unwrap_or_default()
                .contains(") Z ")
        {
            if watched.lock().unwrap().stopped.load(Ordering::Relaxed) {
                return;
            }
            let text = fs::read_to_string(&reason_log).unwrap_or_default();
            let latest = text
                .lines()
                .rev()
                .find(|l| l.contains("connection request from "))
                .unwrap_or("")
                .to_owned();
            if latest != seen {
                seen = latest;
                let _ = watched.lock().unwrap().update(identify(&reason_log));
            }
            // UxPlay keeps its video pipeline, and so this socket, open when the
            // phone stops mirroring: only its log tells.
            let now = mirroring(&text);
            if now != mirrored {
                mirrored = now;
                let mut hub = watched.lock().unwrap();
                let _ = hub.update(json!({"mirroring":now}));
                mirror_pairing.store(now, Ordering::Relaxed);
                // A mirror that starts with no window showing this session opens
                // the floating device window, as on a Mac.
                if now && hub.viewers() == 0 {
                    open_window(s(&hub.state, "session"));
                }
            }
            thread::sleep(Duration::from_millis(500));
        }
        let mut hub = watched.lock().unwrap();
        let _ = hub.update(json!({"error":exit_reason(&reason_log)}));
        hub.stopped.store(true, Ordering::Relaxed);
    });
    let hub = shared.clone();
    thread::spawn(move || {
        // UxPlay's pipeline connects once and stays through mirroring runs;
        // should it reconnect, the stream starts again with parameter sets.
        for stream in listener.incoming() {
            if hub.lock().unwrap().stopped.load(Ordering::Relaxed) {
                break;
            }
            let Ok(mut stream) = stream else { continue };
            let mut units = Units::default();
            let mut buffer = vec![0; 256 * 1024];
            loop {
                match stream.read(&mut buffer) {
                    Ok(0) | Err(_) => break,
                    Ok(read) => {
                        for unit in units.push(&buffer[..read]) {
                            let (config, key) = flags(&unit);
                            hub.lock().unwrap().video(unit, config, key);
                        }
                    }
                }
            }
        }
    });
    // Input goes over Bluetooth; without it the mirror still shows.
    let hid = match bluetooth::Hid::start(pairing, dir.join("bluetooth.log")) {
        Ok(hid) => {
            shared
                .lock()
                .unwrap()
                .update(json!({"bluetooth":{"status":"advertising","name":bluetooth::NAME}}))?;
            Some(hid)
        }
        Err(e) => {
            shared
                .lock()
                .unwrap()
                .update(json!({"bluetooth":{"status":"unavailable","error":e.message}}))?;
            None
        }
    };
    Ok(Airplay {
        _process: process,
        shared,
        hid,
        last: None,
        sent: None,
        held: 0,
    })
}

/// What UxPlay's log says about the phone that connected: its name and its
/// hardware identifier, which picks the model's frame.
fn identify(log: &Path) -> Value {
    let text = fs::read_to_string(log).unwrap_or_default();
    // Another phone after this one must not keep this one's frame.
    let mut found = json!({"name":"iPhone (AirPlay)","client":null,"product_type":null,"profile_id":null,"profile_source":null});
    let line = text
        .lines()
        .rev()
        .find(|l| l.contains("connection request from "));
    if let Some(line) = line {
        let rest = line.split("connection request from ").nth(1).unwrap_or("");
        if let (Some(open), Some(close)) = (rest.rfind(" ("), rest.rfind(')')) {
            if open < close {
                let product = &rest[open + 2..close];
                found["client"] = json!(rest[..open].trim());
                if iphone::valid_product_type(product) {
                    found["product_type"] = json!(product);
                    if let Some(profile) = iphone::profile_for(&crate::catalog::builtin(), product)
                    {
                        found["name"] = json!(format!("{} (AirPlay)", s(&profile, "name")));
                        found["profile_id"] = profile["id"].clone();
                        found["profile_source"] = json!("product-type");
                    }
                }
            }
        }
    }
    found
}

/// Opens the desktop's floating device window on this session, apart from
/// the session: closing the window leaves the receiver running.
fn open_window(session: &str) {
    use std::os::unix::process::CommandExt;
    if session.len() != 32 || !session.bytes().all(|c| c.is_ascii_hexdigit()) {
        return;
    }
    // A desktop that already runs takes the request: it shows the session in
    // its floating window. A second desktop would only find it and quit.
    if crate::preview::call(
        "configure",
        json!({"native":{"session":session,"mirror":true}}),
        "main",
        3,
    )
    .is_ok()
    {
        return;
    }
    let Ok(own) = crate::core::own_executable() else {
        return;
    };
    let desktop = own.with_file_name("tomacco");
    if !desktop.is_file() {
        return;
    }
    let mut command = std::process::Command::new(desktop);
    command
        .args([
            "--workspace",
            "runtime",
            "--device-only",
            "--session",
            session,
        ])
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    unsafe {
        command.pre_exec(|| {
            libc::setsid();
            Ok(())
        });
    }
    if let Ok(mut child) = command.spawn() {
        thread::spawn(move || {
            let _ = child.wait();
        });
    }
}
/// Whether a phone mirrors now: the last start or stop UxPlay logged.
fn mirroring(log: &str) -> bool {
    log.lines()
        .rev()
        .find_map(|line| {
            if line.contains("raop_rtp_mirror starting mirroring") {
                Some(true)
            } else if line.contains("raop_rtp_mirror->running is no longer true")
                || line.starts_with("Connection closed on socket")
            {
                Some(false)
            } else {
                None
            }
        })
        .unwrap_or(false)
}
/// Why UxPlay ended, from its last error line.
fn exit_reason(log: &Path) -> String {
    let text = fs::read_to_string(log).unwrap_or_default();
    match text.lines().rev().find(|l| l.contains("ERROR")) {
        Some(line) if line.contains("socket 98") => {
            "The AirPlay ports are in use: another AirPlay receiver, such as UxPlay, is running"
                .into()
        }
        Some(line) => format!(
            "UxPlay stopped: {}",
            line.trim_start_matches(['*', ' ']).trim()
        ),
        None => "UxPlay stopped".into(),
    }
}

/// Cuts an Annex-B byte stream into NAL units, each with its start code.
#[derive(Default)]
struct Units {
    buffer: Vec<u8>,
}
impl Units {
    fn push(&mut self, data: &[u8]) -> Vec<Vec<u8>> {
        if self.buffer.len() + data.len() > 16 * 1024 * 1024 {
            // A unit this large is not video; start again at the next start code.
            self.buffer.clear();
        }
        self.buffer.extend_from_slice(data);
        let mut starts = Vec::new();
        let mut i = 0;
        while i + 3 <= self.buffer.len() {
            if self.buffer[i..i + 3] == [0, 0, 1] {
                starts.push(if i > 0 && self.buffer[i - 1] == 0 {
                    i - 1
                } else {
                    i
                });
                i += 3;
            } else {
                i += 1;
            }
        }
        let mut units = Vec::new();
        for pair in starts.windows(2) {
            units.push(self.buffer[pair[0]..pair[1]].to_vec());
        }
        match starts.last() {
            Some(&last) => {
                self.buffer.drain(..last);
            }
            None if self.buffer.len() > 3 => {
                let keep = self.buffer.len() - 3;
                self.buffer.drain(..keep);
            }
            None => (),
        }
        units
    }
}
/// Whether a unit is a parameter set (SPS or PPS) and whether it is an IDR picture.
fn flags(unit: &[u8]) -> (bool, bool) {
    let header = if unit.starts_with(&[0, 0, 0, 1]) {
        4
    } else {
        3
    };
    match unit.get(header).map(|b| b & 31) {
        Some(7 | 8) => (true, false),
        Some(5) => (false, true),
        _ => (false, false),
    }
}

const NOT_PAIRED: &str = "While the iPhone mirrors to Tomacco, pair it under Settings > Bluetooth and turn on Settings > Accessibility > Touch > AssistiveTouch";

impl Airplay {
    /// Sends the pointer to (x, y) with `buttons` held, paced to the link.
    fn report(&mut self, x: f64, y: f64, buttons: u8) -> Result<bool> {
        if let Some(gap) = self.sent.map(|t| t.elapsed()).filter(|e| *e < PACE) {
            thread::sleep(PACE - gap);
        }
        let (x, y) = (x.clamp(0.0, 1.0), y.clamp(0.0, 1.0));
        let ok = self.hid.as_mut().unwrap().point(x, y, buttons)?;
        let now = Instant::now();
        self.sent = Some(now);
        if !matches!(self.last, Some((lx, ly, _)) if lx == x && ly == y) {
            self.last = Some((x, y, now));
        }
        Ok(ok)
    }
    /// Presses at (x, y): moves there, lets the pointer rest, then presses.
    fn press(&mut self, x: f64, y: f64) -> Result<bool> {
        let mut ok = true;
        if !matches!(self.last, Some((lx, ly, _)) if lx == x.clamp(0.0, 1.0) && ly == y.clamp(0.0, 1.0))
        {
            ok &= self.report(x, y, 0)?;
        }
        if let Some(rest) = self
            .last
            .map(|(_, _, t)| t.elapsed())
            .filter(|e| *e < SETTLE)
        {
            thread::sleep(SETTLE - rest);
        }
        ok &= self.report(x, y, 1)?;
        self.held = 1;
        Ok(ok)
    }
    fn release(&mut self, x: f64, y: f64) -> Result<bool> {
        self.held = 0;
        self.report(x, y, 0)
    }
    fn note(&self, hid_listening: bool) {
        let mut hub = self.shared.lock().unwrap();
        if hub.state["bluetooth"]["listening"].as_bool() != Some(hid_listening) {
            let _ = hub.update(json!({"bluetooth":{"status":if hid_listening{"connected"}else{"advertising"},"name":bluetooth::NAME,"listening":hid_listening}}));
        }
    }
}

impl Device for Airplay {
    fn input(&mut self, a: &Value) -> Result {
        let action = s(a, "action");
        let listening = self.hid.as_ref().is_some_and(|hid| hid.listening());
        if self.hid.is_some() {
            self.note(listening);
        }
        // Pointer moves without a button reach nobody before pairing; that is
        // no error, or every mouse move over the window would report it.
        if !listening {
            if action == "touch" && ["hover", "move", "cancel"].contains(&s(a, "phase")) {
                return Ok(json!({"delivered":false}));
            }
            if self.hid.is_none() {
                let reason = s(&self.shared.lock().unwrap().state["bluetooth"], "error").to_owned();
                return Err(err(format!("Control needs Bluetooth: {reason}")));
            }
            return Err(Error::new("bluetooth_not_paired", NOT_PAIRED));
        }
        let delivered = match action {
            "touch" => {
                let (x, y) = (number(a, "x"), number(a, "y"));
                match s(a, "phase") {
                    "down" => self.press(x, y)?,
                    "up" => self.release(x, y)?,
                    // The pointer left the window: a held button lets go there.
                    "cancel" if self.held != 0 => self.release(x, y)?,
                    "cancel" => true,
                    // Plain moves come with every mouse move over the picture;
                    // one faster than the link would only queue up as lag.
                    "hover" if self.sent.is_some_and(|t| t.elapsed() < PACE) => true,
                    _ => self.report(x, y, self.held)?,
                }
            }
            "tap" => {
                let (x, y) = (number(a, "x"), number(a, "y"));
                let ok = self.press(x, y)?;
                thread::sleep(HOLD);
                ok && self.release(x, y)?
            }
            "swipe" => {
                let (x, y) = (number(a, "x"), number(a, "y"));
                let (ex, ey) = (number(a, "x2"), number(a, "y2"));
                let mut ok = self.press(x, y)?;
                let duration = Duration::from_secs_f64(number(a, "duration").clamp(0.05, 5.0));
                let steps = (duration.as_millis() / DRAG_STEP.as_millis()).max(1) as u32;
                for i in 1..=steps {
                    let f = i as f64 / steps as f64;
                    thread::sleep(DRAG_STEP);
                    ok &= self.report(x + (ex - x) * f, y + (ey - y) * f, 1)?;
                }
                ok && self.release(ex, ey)?
            }
            "key" => {
                let hid = self.hid.as_mut().unwrap();
                match s(a, "key") {
                    // iOS goes home on Command-H from a hardware keyboard; it
                    // ignores the consumer Home key.
                    "home" => hid.keys(0x08, 0x0B)?,
                    "volume_up" => hid.consumer(0x00E9)?,
                    "volume_down" => hid.consumer(0x00EA)?,
                    "enter" => hid.keys(0, 0x28)?,
                    "escape" => hid.keys(0, 0x29)?,
                    "backspace" => hid.keys(0, 0x2A)?,
                    "tab" => hid.keys(0, 0x2B)?,
                    "delete" => hid.keys(0, 0x4C)?,
                    "right" => hid.keys(0, 0x4F)?,
                    "left" => hid.keys(0, 0x50)?,
                    "down" => hid.keys(0, 0x51)?,
                    "up" => hid.keys(0, 0x52)?,
                    other => return Err(err(format!("The Bluetooth keyboard has no {other} key"))),
                }
            }
            // fill taps the field first elsewhere; here the field the pointer
            // last pressed has the focus, so it types like text.
            "text" | "fill" => {
                let hid = self.hid.as_mut().unwrap();
                let mut skipped = 0;
                let mut ok = true;
                for c in s(a, "text").chars() {
                    match bluetooth::usage(c) {
                        Some((key, shift)) => ok &= hid.keys(if shift { 0x02 } else { 0 }, key)?,
                        None => skipped += 1,
                    }
                }
                if skipped > 0 {
                    return Ok(json!({"delivered":ok,"skipped_characters":skipped}));
                }
                ok
            }
            other => {
                return Err(err(format!("AirPlay control has no {other} input")));
            }
        };
        Ok(json!({"delivered":delivered}))
    }
    fn capture(&mut self, _path: &Path) -> Result {
        Err(err("Capture the AirPlay mirror from the desktop window"))
    }
    fn hierarchy(&mut self) -> Result {
        Err(err("AirPlay mirroring has no accessibility tree"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn the_byte_stream_is_cut_into_units_across_reads() {
        let sps = [0, 0, 0, 1, 0x67, 0x42, 0, 0x1f];
        let pps = [0, 0, 0, 1, 0x68, 0xce, 0x3c, 0x80];
        let idr = [0, 0, 1, 0x65, 0x88, 0x84, 0, 0, 3, 1];
        let next = [0, 0, 0, 1, 0x41, 0x9a];
        let wire = [&sps[..], &pps, &idr, &next].concat();
        for size in [1, 2, 5, wire.len()] {
            let mut units = Units::default();
            let mut out = Vec::new();
            for chunk in wire.chunks(size) {
                out.extend(units.push(chunk));
            }
            // The last unit waits for the next start code.
            assert_eq!(
                out,
                vec![sps.to_vec(), pps.to_vec(), idr.to_vec()],
                "chunk size {size}"
            );
        }
        assert_eq!(flags(&sps), (true, false));
        assert_eq!(flags(&pps), (true, false));
        assert_eq!(flags(&idr), (false, true));
        assert_eq!(flags(&next), (false, false));
    }
    #[test]
    fn the_phone_that_connects_picks_the_frame() {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("uxplay.log");
        fs::write(
            &log,
            "Initialized server socket(s)\nconnection request from iPhone Mm (iPhone18,4) with deviceID = 64:31:35:80:B3:0B\n",
        )
        .unwrap();
        let found = identify(&log);
        assert_eq!(found["client"], "iPhone Mm");
        assert_eq!(found["product_type"], "iPhone18,4");
        assert_eq!(found["profile_id"], "air");
        assert_eq!(found["name"], "iPhone Air (AirPlay)");
        fs::write(
            &log,
            "connection request from Mac (Mac14,2) with deviceID = 1\n",
        )
        .unwrap();
        let other = identify(&log);
        assert!(other["profile_id"].is_null());
        assert_eq!(other["name"], "iPhone (AirPlay)");
        fs::write(
            &log,
            "using network ports UDP 7011\n*** ERROR: Error initialising socket 98\n",
        )
        .unwrap();
        assert!(exit_reason(&log).contains("ports are in use"));
        assert!(!mirroring(""));
        assert!(mirroring(
            "raop_rtp_mirror starting mirroring\nBegin streaming to GStreamer video pipeline\n"
        ));
        assert!(!mirroring(
            "raop_rtp_mirror starting mirroring\nraop_rtp_mirror->running is no longer true\n"
        ));
        assert!(!mirroring(
            "raop_rtp_mirror starting mirroring\nConnection closed on socket 25\n"
        ));
        assert!(mirroring(
            "Connection closed on socket 25\nAccepted IPv4 client on socket 31, port 7000\nraop_rtp_mirror starting mirroring\n"
        ));
        fs::write(&log, "*** ERROR: something else\n").unwrap();
        assert_eq!(exit_reason(&log), "UxPlay stopped: ERROR: something else");
    }
}
