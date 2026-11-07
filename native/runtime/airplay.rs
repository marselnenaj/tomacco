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
    control: Control,
}
/// The phone's pointer and keyboard over Bluetooth, apart from UxPlay.
struct Control {
    /// The Bluetooth pointer and keyboard that carry input, when BlueZ serves them.
    hid: Option<Box<dyn bluetooth::Link>>,
    /// Why BlueZ serves none.
    unavailable: String,
    /// Where the phone's pointer is, 0..1 of the picture, and since when.
    last: Option<(f64, f64, Instant)>,
    /// When the last pointer report went out, to pace them.
    sent: Option<Instant>,
    held: u8,
    /// The phone's subscriptions to the pointer when it last heard from us.
    subscriptions: u64,
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

/// Decoded audio that may wait for UxPlay's audio sink, at most.
const AUDIO_BACKLOG_MS: u64 = 100;
/// Before UxPlay's audio sink, a queue that drops its oldest audio beyond
/// AUDIO_BACKLOG_MS. With -vsync no the sink plays unsynchronised
/// (sync=false, no timestamps) and never skips: audio that arrives late in a
/// burst, after a Wi-Fi stall or UxPlay's wait for a resent packet, was
/// played in full afterwards and its delay stayed for the rest of the mirror.
/// Ordinary jitter below the bound loses nothing, and the sink's own buffer
/// is unchanged (tests/airplay_audio_backlog_integration.py).
const AUDIO_QUEUE: &str =
    "queue max-size-buffers=0 max-size-bytes=0 max-size-time=100000000 leaky=downstream";

/// UxPlay's options after its path.
fn uxplay_options(fps: u64, pipeline: &str, audio_sink: Option<&str>) -> Vec<String> {
    let fps = fps.to_string();
    let mut options = argv(&[
        "-n",
        RECEIVER,
        "-nh",
        // The fixed ports a firewall rule can name: TCP 7000-7001, 7100, UDP 6000-6001, 7011.
        "-p",
        // With -vrtp UxPlay timestamps no video either way (video_renderer.c
        // sets sync only for its own sinks), so this decides audio alone: it
        // plays as it arrives instead of on the phone's clock.
        "-vsync",
        "no",
        "-fps",
        &fps,
        // An iPhone Air's own panel; the phone fits its picture into this size.
        "-s",
        "1260x2736@60",
        // The phone's own reports of the frame rate it sends, in the log.
        "-FPSdata",
    ]);
    if let Some(sink) = audio_sink {
        options.extend(argv(&["-as", sink]));
    }
    options.extend(argv(&["-vrtp", pipeline]));
    options
}
/// The startup file UxPlay reads itself: $UXPLAYRC, ~/.uxplayrc or ~/.config/uxplayrc.
fn uxplay_config() -> Option<String> {
    let home = std::env::var_os("HOME").map(PathBuf::from);
    std::env::var_os("UXPLAYRC")
        .map(PathBuf::from)
        .into_iter()
        .chain(
            home.iter()
                .flat_map(|h| [h.join(".uxplayrc"), h.join(".config/uxplayrc")]),
        )
        .find(|path| path.exists())
        .and_then(|path| fs::read_to_string(path).ok())
}
/// UxPlay's -as: its default sink behind AUDIO_QUEUE, unless the person's
/// UxPlay startup file chooses the sink, which then stays as they set it.
fn audio_sink(config: Option<&str>) -> Option<String> {
    let chosen = config.is_some_and(|text| {
        text.lines().any(|line| {
            !line.starts_with('#') && matches!(line.split_whitespace().next(), Some("as" | "-as"))
        })
    });
    (!chosen).then(|| format!("{AUDIO_QUEUE} ! autoaudiosink"))
}

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
    let audio = audio_sink(uxplay_config().as_deref());
    // UxPlay buffers its log when it is not a terminal; line buffering lets the
    // session read who connected while the mirror runs.
    let mut command = argv(&[&tc::find("stdbuf")?, "-oL", "-eL", &tc::find("uxplay")?]);
    command.extend(uxplay_options(fps, &pipeline, audio.as_deref()));
    let process = spawn(&command, &log)?;
    // audio_backlog_ms is null when the person's UxPlay startup file picks the sink.
    shared.lock().unwrap().update(json!({"name":"iPhone (AirPlay)","transport":"wifi/airplay","codec":"h264","receiver":RECEIVER,"mirroring":false,"mac_required":false,
        "audio_backlog_ms":audio.as_ref().map(|_| AUDIO_BACKLOG_MS)}))?;
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
        // A requested stop kills the owned process too. Do not race that
        // normal shutdown into a failed session using an older warning line.
        if !hub.stopped.load(Ordering::Relaxed) {
            let _ = hub.update(json!({"mirroring":false,"error":exit_reason(&reason_log)}));
            hub.stopped.store(true, Ordering::Relaxed);
        }
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
    let control = match bluetooth::Hid::start(pairing, dir.join("bluetooth.log")) {
        Ok(hid) => {
            shared
                .lock()
                .unwrap()
                .update(json!({"bluetooth":{"status":"advertising","name":bluetooth::NAME}}))?;
            Control::new(Some(Box::new(hid)), String::new())
        }
        Err(e) => {
            shared
                .lock()
                .unwrap()
                .update(json!({"bluetooth":{"status":"unavailable","error":e.message}}))?;
            Control::new(None, e.message)
        }
    };
    Ok(Airplay {
        _process: process,
        shared,
        control,
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
                || line.contains("lost connection with client")
                || line.starts_with("Initialized server socket(s)")
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
    match text.lines().rev().find(|l| l.contains("ERROR") || l.contains("GStreamer error")) {
        Some(line) if line.contains("socket 98") => {
            "The AirPlay ports are in use: another AirPlay receiver, such as UxPlay, is running"
                .into()
        }
        Some(line) if line.contains("feedback request") || line.contains("lost connection with client") =>
            "The iPhone stopped responding over Wi-Fi. Restart AirPlay, then select Tomacco again in Screen Mirroring.".into(),
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
        // An IDR picture can contain several type-5 slices. Only its first
        // slice starts a new GOP; clearing on every slice loses most of the
        // picture for a window that joins later. first_mb_in_slice is ue(v):
        // its value is zero exactly when its first bit is one.
        Some(5) => (false, unit.get(header + 1).is_some_and(|b| b & 0x80 != 0)),
        _ => (false, false),
    }
}

const NOT_PAIRED: &str = "While the iPhone mirrors to Tomacco, pair it under Settings > Bluetooth and turn on Settings > Accessibility > Touch > AssistiveTouch";

impl Control {
    fn new(hid: Option<Box<dyn bluetooth::Link>>, unavailable: String) -> Control {
        Control {
            hid,
            unavailable,
            last: None,
            sent: None,
            held: 0,
            subscriptions: 0,
        }
    }
    fn listening(&self) -> bool {
        self.hid.as_ref().is_some_and(|hid| hid.listening())
    }
    /// The phone stopped listening or listens anew, as after a Bluetooth
    /// reconnect: it let go of any button it held, and where its pointer is
    /// now is unknown. A button kept held here would turn every later plain
    /// move into a drag.
    fn forget(&mut self) {
        self.held = 0;
        self.last = None;
    }
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
    fn at(&self, x: f64, y: f64) -> bool {
        matches!(self.last, Some((lx, ly, _)) if lx == x.clamp(0.0, 1.0) && ly == y.clamp(0.0, 1.0))
    }
    /// Presses at (x, y): moves there, lets the pointer rest, then presses.
    fn press(&mut self, x: f64, y: f64) -> Result<bool> {
        let mut ok = true;
        if !self.at(x, y) {
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
    /// Lets go at (x, y). A held drag first moves there: a button change
    /// lands where the pointer was before its report (see SETTLE), so a
    /// release that also moved would end the drag short of (x, y).
    fn release(&mut self, x: f64, y: f64) -> Result<bool> {
        let mut ok = true;
        if self.held != 0 && !self.at(x, y) {
            ok &= self.report(x, y, self.held)?;
        }
        self.held = 0;
        Ok(ok && self.report(x, y, 0)?)
    }
    fn input(&mut self, a: &Value) -> Result {
        let action = s(a, "action");
        let listening = self.listening();
        // Pointer moves without a button reach nobody before pairing; that is
        // no error, or every mouse move over the window would report it.
        if !listening {
            self.forget();
            if action == "touch" && ["hover", "move", "cancel"].contains(&s(a, "phase")) {
                return Ok(json!({"delivered":false}));
            }
            if self.hid.is_none() {
                return Err(err(format!(
                    "Control needs Bluetooth: {}",
                    self.unavailable
                )));
            }
            return Err(Error::new("bluetooth_not_paired", NOT_PAIRED));
        }
        let subscriptions = self.hid.as_ref().unwrap().subscriptions();
        if subscriptions != self.subscriptions {
            self.subscriptions = subscriptions;
            self.forget();
        }
        let delivered = match action {
            "touch" => {
                let (x, y) = (number(a, "x"), number(a, "y"));
                match s(a, "phase") {
                    "down" => self.press(x, y)?,
                    "up" => self.release(x, y)?,
                    // The pointer left the window: a held button lets go where
                    // the pointer is, without moving it on.
                    "cancel" if self.held != 0 => match self.last {
                        Some((lx, ly, _)) => self.release(lx, ly)?,
                        None => self.release(x, y)?,
                    },
                    "cancel" => true,
                    // A pointer without a button presses nothing: it never
                    // carries one, and does not move a finger that is down.
                    "hover" if self.held != 0 => false,
                    // Plain moves come with every mouse move over the picture;
                    // report() paces them, and the desktop keeps only the
                    // newest one waiting, so the last position always lands.
                    "hover" => self.report(x, y, 0)?,
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
}

impl Airplay {
    fn note(&self, hid_listening: bool) {
        let mut hub = self.shared.lock().unwrap();
        if hub.state["bluetooth"]["listening"].as_bool() != Some(hid_listening) {
            let _ = hub.update(json!({"bluetooth":{"status":if hid_listening{"connected"}else{"advertising"},"name":bluetooth::NAME,"listening":hid_listening}}));
        }
    }
}

/// What AirPlay input can do: everything goes over the Bluetooth pointer and
/// keyboard, so without one BlueZ serves there is no input at all.
fn capabilities(bluetooth: bool) -> Value {
    let mut c = json!({"transport":"wifi/airplay","evidence":"device","capture":false,"hierarchy":false,
        "live_touch":true,"atomic_trace":false,"max_contacts":1,"pinch":false,"fill":"types into the focused field",
        "text":"US-layout keys","rotate":false,"exact_scroll":"pointer drag",
        "keys":["home","volume_up","volume_down","enter","escape","backspace","tab","delete","right","left","down","up"],
        "note":"A Bluetooth pointer with AssistiveTouch: one contact, live, once the phone is paired and listens (runtime.state bluetooth.listening). Screenshots come from the desktop window, not this session; runtime.observe and screen.* on this session answer an error."});
    if !bluetooth {
        for key in ["live_touch", "fill", "text", "exact_scroll"] {
            c[key] = json!(false);
        }
        c["max_contacts"] = json!(0);
        c["keys"] = json!([]);
        c["note"] = json!(
            "No input: AirPlay carries none, and Bluetooth is unavailable for this session (runtime.state bluetooth.error). Screenshots come from the desktop window, not this session; runtime.observe and screen.* on this session answer an error."
        );
    }
    c
}

impl Device for Airplay {
    fn input(&mut self, a: &Value) -> Result {
        if self.control.hid.is_some() {
            self.note(self.control.listening());
        }
        self.control.input(a)
    }
    fn capture(&mut self, _path: &Path) -> Result {
        Err(err("Capture the AirPlay mirror from the desktop window"))
    }
    fn hierarchy(&mut self) -> Result {
        Err(err("AirPlay mirroring has no accessibility tree"))
    }
    fn capabilities(&self) -> Value {
        capabilities(self.control.hid.is_some())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicU64;

    /// A pointer report: when, x, y, buttons.
    type Report = (Instant, f64, f64, u8);
    /// A Bluetooth link that records each pointer report with its time.
    #[derive(Clone, Default)]
    struct Fake {
        listening: Arc<AtomicBool>,
        subscriptions: Arc<AtomicU64>,
        reports: Arc<Mutex<Vec<Report>>>,
    }
    impl bluetooth::Link for Fake {
        fn listening(&self) -> bool {
            self.listening.load(Ordering::Relaxed)
        }
        fn subscriptions(&self) -> u64 {
            self.subscriptions.load(Ordering::Relaxed)
        }
        fn point(&mut self, x: f64, y: f64, buttons: u8) -> Result<bool> {
            self.reports
                .lock()
                .unwrap()
                .push((Instant::now(), x, y, buttons));
            Ok(true)
        }
        fn keys(&mut self, _modifiers: u8, _key: u8) -> Result<bool> {
            Ok(true)
        }
        fn consumer(&mut self, _usage: u16) -> Result<bool> {
            Ok(true)
        }
    }
    impl Fake {
        /// The phone subscribes (again), as on pairing or a reconnect.
        fn subscribe(&self) {
            self.subscriptions.fetch_add(1, Ordering::Relaxed);
            self.listening.store(true, Ordering::Relaxed);
        }
        fn take(&self) -> Vec<Report> {
            std::mem::take(&mut *self.reports.lock().unwrap())
        }
        fn points(&self) -> Vec<(f64, f64, u8)> {
            self.take()
                .into_iter()
                .map(|(_, x, y, b)| (x, y, b))
                .collect()
        }
    }
    fn control() -> (Control, Fake) {
        let fake = Fake::default();
        fake.subscribe();
        (
            Control::new(Some(Box::new(fake.clone())), String::new()),
            fake,
        )
    }
    fn touch(phase: &str, x: f64, y: f64) -> Value {
        json!({"action":"touch","phase":phase,"x":x,"y":y})
    }

    #[test]
    fn a_button_held_when_the_phone_stopped_listening_is_not_held_after_it_listens_again() {
        let (mut c, fake) = control();
        c.input(&touch("down", 0.2, 0.2)).unwrap();
        // The Bluetooth link drops mid-drag; the release cannot reach the phone.
        fake.listening.store(false, Ordering::Relaxed);
        assert_eq!(
            c.input(&touch("up", 0.3, 0.3)).unwrap_err().code,
            "bluetooth_not_paired"
        );
        fake.subscribe();
        fake.take();
        // A plain mouse move afterwards moves the pointer, with no button.
        assert_eq!(
            c.input(&touch("hover", 0.6, 0.6)).unwrap()["delivered"],
            true
        );
        assert_eq!(fake.points(), vec![(0.6, 0.6, 0)]);
        // The same after a reconnect between two inputs, seen only by the new subscription.
        c.input(&touch("down", 0.6, 0.6)).unwrap();
        fake.subscribe();
        fake.take();
        c.input(&touch("hover", 0.7, 0.7)).unwrap();
        assert_eq!(fake.points(), vec![(0.7, 0.7, 0)]);
    }

    #[test]
    fn after_a_reconnect_a_press_moves_and_settles_first() {
        let (mut c, fake) = control();
        c.input(&touch("hover", 0.4, 0.4)).unwrap();
        thread::sleep(SETTLE);
        fake.take();
        // Resting where the pointer is, a press goes out at once.
        c.input(&touch("down", 0.4, 0.4)).unwrap();
        c.input(&touch("up", 0.4, 0.4)).unwrap();
        assert_eq!(fake.points(), vec![(0.4, 0.4, 1), (0.4, 0.4, 0)]);
        thread::sleep(SETTLE);
        // The phone reconnected: where its pointer is is unknown.
        fake.subscribe();
        c.input(&touch("down", 0.4, 0.4)).unwrap();
        let reports = fake.take();
        assert_eq!(reports.len(), 2);
        assert_eq!((reports[0].3, reports[1].3), (0, 1));
        assert!(reports[1].0 - reports[0].0 >= SETTLE);
    }

    #[test]
    fn a_hover_never_carries_a_button() {
        let (mut c, fake) = control();
        c.input(&touch("down", 0.2, 0.2)).unwrap();
        fake.take();
        // Hover while a finger is down neither drags it nor lifts it.
        assert_eq!(
            c.input(&touch("hover", 0.8, 0.8)).unwrap()["delivered"],
            false
        );
        assert!(fake.points().is_empty());
        c.input(&touch("move", 0.3, 0.3)).unwrap();
        c.input(&touch("up", 0.3, 0.3)).unwrap();
        assert_eq!(fake.points(), vec![(0.3, 0.3, 1), (0.3, 0.3, 0)]);
    }

    #[test]
    fn the_last_of_quick_mouse_moves_reaches_the_phone() {
        let (mut c, fake) = control();
        for i in 1..=4 {
            c.input(&touch("hover", 0.1 * i as f64, 0.5)).unwrap();
        }
        let reports = fake.take();
        // None is dropped for coming within PACE of the one before; each waits.
        let xs: Vec<f64> = reports.iter().map(|r| r.1).collect();
        assert_eq!(xs, vec![0.1, 0.2, 0.30000000000000004, 0.4]);
        for pair in reports.windows(2) {
            assert!(pair[1].0 - pair[0].0 >= PACE);
        }
        assert!(reports.iter().all(|r| r.3 == 0));
    }

    #[test]
    fn a_drag_ends_where_the_mouse_let_go_and_a_cancel_lifts_in_place() {
        let (mut c, fake) = control();
        c.input(&touch("down", 0.5, 0.8)).unwrap();
        c.input(&touch("move", 0.5, 0.6)).unwrap();
        // The release comes at a point the last move did not reach.
        c.input(&touch("up", 0.5, 0.4)).unwrap();
        assert_eq!(
            fake.points(),
            vec![
                (0.5, 0.8, 0),
                (0.5, 0.8, 1),
                (0.5, 0.6, 1),
                (0.5, 0.4, 1),
                (0.5, 0.4, 0)
            ]
        );
        c.input(&touch("down", 0.5, 0.4)).unwrap();
        c.input(&touch("move", 0.5, 0.2)).unwrap();
        fake.take();
        // Leaving the window lifts the finger where it is.
        c.input(&touch("cancel", 0.9, 0.9)).unwrap();
        assert_eq!(fake.points(), vec![(0.5, 0.2, 0)]);
        assert_eq!(
            c.input(&touch("cancel", 0.9, 0.9)).unwrap()["delivered"],
            true
        );
        assert!(fake.points().is_empty());
        // A swipe ends on its last step: one release, no extra move.
        c.input(&json!({"action":"swipe","x":0.5,"y":0.8,"x2":0.5,"y2":0.2,"duration":0.05}))
            .unwrap();
        let swipe = fake.points();
        assert_eq!(swipe.last(), Some(&(0.5, 0.2, 0)));
        assert_eq!(swipe[swipe.len() - 2], (0.5, 0.2, 1));
        assert_eq!(swipe.iter().filter(|r| r.2 == 1).count(), swipe.len() - 2);
    }

    #[test]
    fn without_bluetooth_no_input_is_advertised() {
        let none = capabilities(false);
        assert_eq!(none["live_touch"], false);
        assert_eq!(none["max_contacts"], 0);
        assert_eq!(none["keys"], json!([]));
        assert_eq!(none["text"], false);
        assert_eq!(none["fill"], false);
        let served = capabilities(true);
        assert_eq!(served["live_touch"], true);
        assert_eq!(served["max_contacts"], 1);
        assert_eq!(served["keys"].as_array().unwrap().len(), 12);
        let mut c = Control::new(None, "Bluetooth is off".into());
        assert_eq!(
            c.input(&touch("hover", 0.5, 0.5)).unwrap()["delivered"],
            false
        );
        let e = c.input(&touch("down", 0.5, 0.5)).unwrap_err();
        assert_eq!(e.message, "Control needs Bluetooth: Bluetooth is off");
    }

    #[test]
    fn uxplay_bounds_the_audio_backlog_unless_its_startup_file_picks_the_sink() {
        assert!(AUDIO_QUEUE.contains(&format!("max-size-time={}", AUDIO_BACKLOG_MS * 1_000_000)));
        assert!(AUDIO_QUEUE.contains("leaky=downstream"));
        let sink = audio_sink(None).unwrap();
        assert_eq!(sink, format!("{AUDIO_QUEUE} ! autoaudiosink"));
        assert_eq!(
            audio_sink(Some("# as pulsesink\nfps 30\n")).as_deref(),
            Some(sink.as_str())
        );
        assert_eq!(audio_sink(Some("nh\nas pipewiresink\n")), None);
        assert_eq!(audio_sink(Some("-as 0\n")), None);
        let options = uxplay_options(60, "PIPE", Some(&sink));
        let at = |o: &str| options.iter().position(|v| v == o).unwrap();
        assert_eq!(options[at("-as") + 1], sink);
        // Video stays unsynchronised and last; the other options are as before.
        assert_eq!(options[at("-vsync") + 1], "no");
        assert_eq!(&options[options.len() - 2..], ["-vrtp", "PIPE"]);
        assert_eq!(&options[..4], ["-n", RECEIVER, "-nh", "-p"]);
        assert!(!uxplay_options(60, "PIPE", None).contains(&"-as".to_string()));
    }
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
        // A subsequent IDR slice has first_mb_in_slice > 0 (ue(v) starts
        // with zero). It belongs to the same cached picture as the first.
        assert_eq!(flags(&[0, 0, 1, 0x65, 0x40]), (false, false));
        assert_eq!(flags(&[0, 0, 1, 0x65]), (false, false));
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
        assert!(mirroring(
            "raop_rtp_mirror starting mirroring\nConnection closed on socket 25\n"
        ));
        assert!(!mirroring(
            "raop_rtp_mirror starting mirroring\n***ERROR lost connection with client (network problem?)\n"
        ));
        assert!(!mirroring(
            "raop_rtp_mirror starting mirroring\nInitialized server socket(s)\n"
        ));
        assert!(mirroring(
            "Connection closed on socket 25\nAccepted IPv4 client on socket 31, port 7000\nraop_rtp_mirror starting mirroring\n"
        ));
        fs::write(&log, "*** ERROR: something else\n").unwrap();
        assert_eq!(exit_reason(&log), "UxPlay stopped: ERROR: something else");
        fs::write(&log,"*** ERROR: 6 seconds since last client feedback request\nGStreamer error (audio): wrong format\n").unwrap();
        assert_eq!(
            exit_reason(&log),
            "UxPlay stopped: GStreamer error (audio): wrong format"
        );
        fs::write(
            &log,
            "*** ERROR: 6 seconds since last client feedback request\n",
        )
        .unwrap();
        assert!(exit_reason(&log).contains("iPhone stopped responding"));
    }
}
