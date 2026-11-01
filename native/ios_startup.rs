//! App startup on an iPhone, for profile.startup: the virtual iPhone through
//! its guest service and frame reads (ios_emulator::startup), and a USB
//! iPhone through go-ios (this file). Neither has a launch timer like
//! Android's `am start -W`, so a launch is timed on the screen: from the open
//! request to the first picture that differs from the one before the launch
//! and stays different, and to the screen keeping still afterwards. The time
//! the launch request took to return, with the process it started, is
//! reported beside them. The report has the shape and the metric names of the
//! Android one, so profile.compare and profile.budget read it the same way.
use crate::{core::*, iphone_device, toolchain as tc};
use serde_json::{Map, Value, json};
use std::collections::VecDeque;
use std::io::{BufRead, BufReader, Read, Write};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex, mpsc};
use std::time::{Duration, Instant};

/// A screen as a grid of grey cells: each the average of its pixels.
pub type Signature = Vec<u8>;
pub const GRID_WIDTH: usize = 32;
pub const GRID_HEIGHT: usize = 64;
/// A cell has changed when its grey differs by more than this.
const CELL_DELTA: u8 = 12;
/// The screen differs from the one before the launch when this share of its
/// cells has changed. On the virtual iPhone's Home screen the Clock icon's
/// second hand and the status bar's time change under 0.5 % of the cells, the
/// two with a pressed icon 2.2 %; an app's window over it changes 92 %.
pub const CHANGE_FRACTION: f64 = 0.05;
/// Between two pictures, a change of this share is motion: the screen has not
/// settled yet. The ticking Clock icon stays below it.
pub const MOTION_FRACTION: f64 = 0.005;

/// The share of cells that differ between two signatures; all of them when
/// the signatures do not match in size.
pub fn changed_fraction(a: &[u8], b: &[u8]) -> f64 {
    if a.len() != b.len() || a.is_empty() {
        return 1.0;
    }
    let changed = a
        .iter()
        .zip(b)
        .filter(|(x, y)| x.abs_diff(**y) > CELL_DELTA)
        .count();
    changed as f64 / a.len() as f64
}

/// The signature of 8-bit pixels with `channels` bytes each (3 for RGB, 4 for
/// RGBA; alpha is ignored), rows top to bottom.
pub fn signature(pixels: &[u8], width: usize, height: usize, channels: usize) -> Option<Signature> {
    if width < GRID_WIDTH
        || height < GRID_HEIGHT
        || channels < 3
        || pixels.len() < width * height * channels
    {
        return None;
    }
    let columns: Vec<usize> = (0..width).map(|x| x * GRID_WIDTH / width).collect();
    let mut sums = vec![0u64; GRID_WIDTH * GRID_HEIGHT];
    let mut counts = vec![0u64; GRID_WIDTH * GRID_HEIGHT];
    for y in 0..height {
        let row = &pixels[y * width * channels..(y + 1) * width * channels];
        let base = y * GRID_HEIGHT / height * GRID_WIDTH;
        for (x, pixel) in row.chunks_exact(channels).enumerate() {
            let grey =
                (u64::from(pixel[0]) * 3 + u64::from(pixel[1]) * 6 + u64::from(pixel[2])) / 10;
            sums[base + columns[x]] += grey;
            counts[base + columns[x]] += 1;
        }
    }
    Some(
        sums.iter()
            .zip(&counts)
            .map(|(sum, count)| (sum / (*count).max(1)) as u8)
            .collect(),
    )
}

/// The signature of a binary PPM (P6, 8 bits), as the virtual iPhone's
/// frame reads write them.
pub fn signature_ppm(data: &[u8]) -> Option<Signature> {
    let mut fields = Vec::new();
    let mut at = 0;
    while fields.len() < 4 {
        while data.get(at)?.is_ascii_whitespace() {
            at += 1;
        }
        if data[at] == b'#' {
            while *data.get(at)? != b'\n' {
                at += 1;
            }
            continue;
        }
        let start = at;
        while !data.get(at)?.is_ascii_whitespace() {
            at += 1;
        }
        fields.push(std::str::from_utf8(&data[start..at]).ok()?);
    }
    // One whitespace byte ends the header.
    let pixels = data.get(at + 1..)?;
    if fields[0] != "P6" || fields[3] != "255" {
        return None;
    }
    let width: usize = fields[1].parse().ok().filter(|w| (1..=16384).contains(w))?;
    let height: usize = fields[2].parse().ok().filter(|h| (1..=16384).contains(h))?;
    signature(pixels, width, height, 3)
}

/// Follows the screen after an open request: the first picture that differs
/// from the one before the launch and stays different until the screen
/// settles, and the last motion before `settle_ms` without any.
pub struct Detector {
    baseline: Signature,
    previous: Signature,
    settle_ms: f64,
    pub first_change: Option<f64>,
    last_motion: Option<f64>,
    pub settled: Option<f64>,
}

impl Detector {
    pub fn new(baseline: Signature, settle_ms: f64) -> Self {
        Detector {
            previous: baseline.clone(),
            baseline,
            settle_ms,
            first_change: None,
            last_motion: None,
            settled: None,
        }
    }

    /// One look at the screen at `at` milliseconds: a new picture, or `None`
    /// when it is the same as the last one. True once the screen settled.
    pub fn feed(&mut self, at: f64, picture: Option<&[u8]>) -> bool {
        if self.settled.is_some() {
            return true;
        }
        if let Some(picture) = picture {
            if changed_fraction(&self.baseline, picture) >= CHANGE_FRACTION {
                if self.first_change.is_none() {
                    self.first_change = Some(at);
                    self.last_motion = Some(at);
                }
            } else {
                // Back to the screen before the launch: that change did not stay.
                self.first_change = None;
                self.last_motion = None;
            }
            if self.first_change.is_some()
                && changed_fraction(&self.previous, picture) >= MOTION_FRACTION
            {
                self.last_motion = Some(at);
            }
            self.previous = picture.to_vec();
        }
        if let (Some(_), Some(last)) = (self.first_change, self.last_motion) {
            if at - last >= self.settle_ms {
                self.settled = Some(last);
            }
        }
        self.settled.is_some()
    }
}

/// One look at the screen. `sample` is false for a look that saw no new
/// picture because none arrived (a stream), true for every read of the screen.
pub struct Frame {
    pub at_ms: f64,
    pub picture: Option<Signature>,
    pub sample: bool,
}

/// What an open request answered: when it returned, on the launcher's clock,
/// the process it names and the reply.
pub struct Opened {
    pub completed_ms: f64,
    pub pid: Option<u64>,
    pub reply: Value,
}

pub type Opener = Box<dyn FnOnce() -> Result<Opened> + Send>;

/// A phone whose app launches can be timed.
pub trait Launcher {
    /// Milliseconds on the launcher's clock, which Frame and Opened use too.
    fn now_ms(&self) -> f64;
    /// The screen now, or the next picture a stream delivered.
    fn frame(&mut self) -> Result<Frame>;
    /// Before each launch, unmeasured: leave a lock screen, wait for the phone.
    fn prepare(&mut self) -> Result<()> {
        Ok(())
    }
    /// End the app's process for a cold launch; what was done.
    fn terminate(&mut self) -> Result<Value>;
    /// Send the app to the background, keeping its process, for a hot launch.
    fn background(&mut self) -> Result<Value>;
    /// The open request, run on another thread while the screen is read.
    fn opener(&mut self) -> Opener;
    /// When the app's first log line after `since_ms` arrived, if it is followed.
    fn first_log_ms(&mut self, _since_ms: f64) -> Option<f64> {
        None
    }
}

/// How the launches are measured.
pub struct Settings {
    pub mode: String,
    pub runs: u64,
    pub settle_ms: f64,
    /// How long a launch may take to show and settle.
    pub launch_timeout_ms: f64,
    /// How long the screen before a launch may take to keep still.
    pub quiet_timeout_ms: f64,
}

/// The screen once it kept still for `quiet_ms`, or as it is at the timeout;
/// true when it did keep still.
fn still_screen(
    phone: &mut dyn Launcher,
    quiet_ms: f64,
    timeout_ms: f64,
) -> Result<(Signature, bool)> {
    let started = phone.now_ms();
    let mut latest: Option<Signature> = None;
    let mut since = started;
    loop {
        check_cancelled()?;
        let frame = phone.frame()?;
        if let Some(picture) = frame.picture {
            let moved = latest
                .as_ref()
                .is_none_or(|old| changed_fraction(old, &picture) >= MOTION_FRACTION);
            if moved {
                since = frame.at_ms;
            }
            latest = Some(picture);
        }
        let elapsed = frame.at_ms - started;
        match &latest {
            Some(picture) if frame.at_ms - since >= quiet_ms => return Ok((picture.clone(), true)),
            Some(picture) if elapsed >= timeout_ms => return Ok((picture.clone(), false)),
            None if elapsed >= timeout_ms => {
                return Err(Error::new(
                    "startup_unobserved",
                    "No picture of the screen arrived before the launch",
                )
                .remedy("Check that the phone's screen can be read (a screenshot) and try again"));
            }
            _ => (),
        }
    }
}

fn joined(handle: std::thread::JoinHandle<Result<Opened>>) -> Result<Opened> {
    handle
        .join()
        .map_err(|_| Error::new("tool_failed", "The open request ended unexpectedly"))?
}

/// The median of values, or None.
fn median(values: &[f64]) -> Option<f64> {
    let summary = crate::perf::summary(values);
    summary["median"].as_f64()
}

fn ms(value: f64) -> f64 {
    (value * 10.0).round() / 10.0
}

/// One timed launch; `previous_pid` is the app's process before it.
fn launch(
    phone: &mut dyn Launcher,
    settings: &Settings,
    number: u64,
    previous_pid: Option<u64>,
) -> Result<Value> {
    phone.prepare()?;
    let action = if settings.mode == "cold" {
        phone.terminate()?
    } else {
        phone.background()?
    };
    let (baseline, still) = still_screen(
        phone,
        settings.settle_ms.min(2000.0),
        settings.quiet_timeout_ms,
    )?;
    let mut detector = Detector::new(baseline, settings.settle_ms);
    let start = phone.now_ms();
    let mut request = Some(std::thread::spawn(phone.opener()));
    let mut opened = None;
    let mut samples = Vec::new();
    let mut pictures = 0u64;
    loop {
        check_cancelled()?;
        if request.as_ref().is_some_and(|h| h.is_finished()) {
            opened = Some(joined(request.take().unwrap())?);
        }
        let frame = phone.frame()?;
        if frame.sample {
            samples.push(frame.at_ms);
        }
        pictures += u64::from(frame.picture.is_some());
        let settled = detector.feed(frame.at_ms, frame.picture.as_deref());
        if (settled && request.is_none()) || frame.at_ms - start >= settings.launch_timeout_ms {
            break;
        }
    }
    if let Some(handle) = request.take() {
        opened = Some(joined(handle)?);
    }
    let opened = opened.unwrap();
    let first = detector.first_change.ok_or_else(|| {
        Error::new(
            "startup_unobserved",
            format!(
                "The screen did not change for {} s after the app was opened",
                (settings.launch_timeout_ms / 1000.0).round()
            ),
        )
        .remedy("Check that the app opens and draws on this phone (a screenshot after device.app launch), then try again")
        .details(json!({"run":number,"open":opened.reply}))
    })?;
    let intervals: Vec<f64> = samples.windows(2).map(|w| w[1] - w[0]).collect();
    let state = match (previous_pid, opened.pid) {
        (Some(old), Some(new)) if old == new => "hot",
        (Some(_), Some(_)) => "cold",
        _ => "",
    };
    let first_log = phone.first_log_ms(start).map(|at| ms(at - start));
    Ok(json!({"run":number,"launch_state":state,"pid":opened.pid,
        "first_frame_ms":ms(first-start),
        "launched_ms":ms(opened.completed_ms-start),
        "settled_ms":detector.settled.map(|at| ms(at-start)),
        "first_log_ms":first_log,
        "baseline_still":still,"pictures":pictures,
        "resolution_ms":median(&intervals).map(ms),
        "before":action}))
}

/// Opens the app once unmeasured, then times `runs` launches.
pub fn measure(phone: &mut dyn Launcher, settings: &Settings) -> Result<Vec<Value>> {
    // The first open shows that the app opens at all and names its process,
    // so that each measured launch can say whether it started a new one.
    let opened = (phone.opener())()?;
    let mut pid = opened.pid;
    still_screen(phone, settings.settle_ms, settings.launch_timeout_ms)?;
    let mut runs = Vec::new();
    for number in 1..=settings.runs {
        check_cancelled()?;
        let run = launch(phone, settings, number, pid)?;
        if let Some(new) = run["pid"].as_u64() {
            pid = Some(new);
        }
        runs.push(run);
    }
    Ok(runs)
}

/// The startup report of an iPhone: the Android report's metric names, the
/// time to the first changed picture as their value, and the request and
/// settle times beside them.
pub fn report(
    device: &str,
    kind: &str,
    id: &str,
    settings: &Settings,
    runs: &[Value],
    note: &str,
) -> Value {
    let mode = settings.mode.as_str();
    let values = |key: &str| -> Vec<f64> { runs.iter().filter_map(|r| r[key].as_f64()).collect() };
    let summary = |key: &str| {
        let mut value = crate::perf::summary(&values(key));
        for statistic in ["median", "mean"] {
            if let Some(number) = value[statistic].as_f64() {
                value[statistic] = json!(ms(number));
            }
        }
        value
    };
    let first = summary("first_frame_ms");
    let launched = summary("launched_ms");
    let settled = summary("settled_ms");
    let mut metrics = Map::new();
    for statistic in ["median", "p90", "min", "max", "mean"] {
        metrics.insert(
            format!("startup_{mode}_{statistic}_ms"),
            first[statistic].clone(),
        );
    }
    for (name, summary) in [("launched", &launched), ("settled", &settled)] {
        for statistic in ["median", "p90"] {
            metrics.insert(
                format!("startup_{mode}_{name}_{statistic}_ms"),
                summary[statistic].clone(),
            );
        }
    }
    let first_log = summary("first_log_ms");
    if kind == "iphone" {
        for statistic in ["median", "p90"] {
            metrics.insert(
                format!("startup_{mode}_first_log_{statistic}_ms"),
                first_log[statistic].clone(),
            );
        }
    }
    let mismatched = runs
        .iter()
        .filter(|r| !s(r, "launch_state").is_empty() && s(r, "launch_state") != mode)
        .count();
    let mut value = json!({"version":1,"kind":"startup","bundle_id":id,"device":device,
        "device_kind":kind,"created":now(),"metrics":metrics,"mode":mode,
        "method":"screen","settle_ms":settings.settle_ms,
        "first_frame_ms":first,"launched_ms":launched,"settled_ms":settled,
        "resolution_ms":median(&values("resolution_ms")).map(ms),
        "mismatched_runs":mismatched,"runs":runs,"note":note});
    if kind == "iphone" {
        value["first_log_ms"] = first_log;
    }
    value
}

/// The iPhone's modes: cold and hot. Android's warm (the process kept, the
/// activity created again) has no counterpart an iPhone app can be put in
/// from outside.
pub fn mode(a: &Value) -> Result<String> {
    match s(a, "mode") {
        "" | "cold" => Ok("cold".into()),
        "hot" => Ok("hot".into()),
        other => Err(Error::new(
            "invalid_argument",
            format!("An iPhone launch is measured cold or hot, not {other}"),
        )
        .remedy("Use cold (the app's process ended before each launch) or hot (the app sent to the background and opened again)")),
    }
}

pub fn settle_ms(a: &Value) -> f64 {
    a["settle_ms"].as_u64().unwrap_or(2000).clamp(200, 10000) as f64
}

// A USB-connected iPhone, through go-ios.

/// The pid go-ios `launch` logs: `{"level":"info","msg":"Process launched","pid":1234}`
/// as JSON, or `pid=1234` with --nojson.
pub fn launched_pid(text: &str) -> Option<u64> {
    let pattern = regex::Regex::new(r#"(?:"pid"\s*:\s*|\bpid=)(\d+)"#).unwrap();
    pattern
        .captures_iter(text)
        .filter_map(|c| c[1].parse::<u64>().ok())
        .last()
}

/// JPEG pictures cut out of go-ios's MJPEG stream by their start and end
/// markers, as tools/ios_device_record.py does.
#[derive(Default)]
pub struct Pictures {
    buffer: Vec<u8>,
}

impl Pictures {
    pub fn feed(&mut self, data: &[u8]) -> Result<Vec<Vec<u8>>> {
        self.buffer.extend_from_slice(data);
        let mut found = Vec::new();
        loop {
            let Some(start) = find(&self.buffer, b"\xff\xd8", 0) else {
                // Keep a trailing 0xff: it may begin the next start marker.
                let keep = self.buffer.len().saturating_sub(1);
                self.buffer.drain(..keep);
                break;
            };
            let Some(end) = find(&self.buffer, b"\xff\xd9", start + 2) else {
                self.buffer.drain(..start);
                if self.buffer.len() > 16 << 20 {
                    return Err(Error::new(
                        "invalid_capture",
                        "A picture in go-ios's screenshot stream is larger than 16 MiB",
                    ));
                }
                break;
            };
            found.push(self.buffer[start..end + 2].to_vec());
            self.buffer.drain(..end + 2);
        }
        Ok(found)
    }
}

fn find(haystack: &[u8], needle: &[u8], from: usize) -> Option<usize> {
    haystack
        .get(from..)?
        .windows(needle.len())
        .position(|w| w == needle)
        .map(|at| at + from)
}

/// The ffmpeg command that turns the stream's JPEG pictures into signatures:
/// each picture scaled to the grid in grey, one raw frame per picture.
pub fn decoder_command(ffmpeg: &str) -> Vec<String> {
    argv(&[
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "image2pipe",
        "-c:v",
        "mjpeg",
        "-i",
        "pipe:0",
        "-vf",
        &format!("scale={GRID_WIDTH}:{GRID_HEIGHT}:flags=area,format=gray"),
        "-fps_mode",
        "passthrough",
        "-f",
        "rawvideo",
        "pipe:1",
    ])
}

/// Child processes ended when the measurement ends.
struct Children(Vec<Child>);
impl Drop for Children {
    fn drop(&mut self) {
        for child in &mut self.0 {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

struct Iphone {
    go: String,
    device: String,
    installed: String,
    base: Instant,
    pictures: mpsc::Receiver<(f64, Signature)>,
    stream_error: Arc<Mutex<Option<String>>>,
    logs: Arc<Mutex<Vec<f64>>>,
    _children: Children,
}

fn elapsed_ms(base: Instant) -> f64 {
    base.elapsed().as_secs_f64() * 1000.0
}

impl Iphone {
    fn go_ios(&self, args: &[&str]) -> Result {
        let mut command = argv(&[&self.go]);
        command.extend(argv(args));
        command.push(format!("--udid={}", self.device));
        tc::run(&command, 60, None, false)
    }
}

impl Launcher for Iphone {
    fn now_ms(&self) -> f64 {
        elapsed_ms(self.base)
    }

    fn frame(&mut self) -> Result<Frame> {
        match self.pictures.recv_timeout(Duration::from_millis(50)) {
            Ok((at_ms, picture)) => Ok(Frame {
                at_ms,
                picture: Some(picture),
                sample: true,
            }),
            Err(mpsc::RecvTimeoutError::Timeout) => Ok(Frame {
                at_ms: self.now_ms(),
                picture: None,
                sample: false,
            }),
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                let reason = self
                    .stream_error
                    .lock()
                    .unwrap()
                    .clone()
                    .unwrap_or_else(|| "go-ios ended the screenshot stream".into());
                Err(Error::new("startup_unobserved", reason).remedy(
                    "Mount the developer image with ios.prepare (and keep a tunnel running on iOS 17 and newer), then try again",
                ))
            }
        }
    }

    fn terminate(&mut self) -> Result<Value> {
        // go-ios answers an error when the app is not running; either way it
        // is not running afterwards.
        let done = self.go_ios(&["kill", &self.installed])?;
        Ok(json!({"action":"go-ios kill","exit_code":done["exit_code"]}))
    }

    fn background(&mut self) -> Result<Value> {
        // go-ios has no Home button; opening Settings sends the app to the
        // background as Home would.
        let done = self.go_ios(&["launch", SETTINGS_APP])?;
        if done["exit_code"] != 0 {
            return Err(Error::new(
                "app_action_failed",
                "go-ios could not open Settings to send the app to the background",
            )
            .details(json!({"output":format!("{}{}", s(&done,"stdout"), s(&done,"stderr")).chars().take(2000).collect::<String>()})));
        }
        Ok(json!({"action":"go-ios launch","bundle_id":SETTINGS_APP}))
    }

    fn opener(&mut self) -> Opener {
        let command = argv(&[
            &self.go,
            "launch",
            &self.installed,
            &format!("--udid={}", self.device),
        ]);
        let base = self.base;
        Box::new(move || {
            let done = tc::run(&command, 120, None, false)?;
            let completed_ms = elapsed_ms(base);
            let output = format!("{}\n{}", s(&done, "stdout"), s(&done, "stderr"));
            if done["exit_code"] != 0 {
                return Err(Error::new("launch_failed", "go-ios could not launch the app")
                    .remedy("Unlock the iPhone, check ios.device_status, and launch the app once with device.app")
                    .details(json!({"output":output.chars().take(2000).collect::<String>()})));
            }
            Ok(Opened {
                completed_ms,
                pid: launched_pid(&output),
                reply: json!({"exit_code":done["exit_code"]}),
            })
        })
    }

    fn first_log_ms(&mut self, since_ms: f64) -> Option<f64> {
        self.logs
            .lock()
            .unwrap()
            .iter()
            .copied()
            .find(|at| *at >= since_ms)
    }
}

const SETTINGS_APP: &str = "com.apple.Preferences";

fn free_port() -> Result<u16> {
    Ok(std::net::TcpListener::bind("127.0.0.1:0")?
        .local_addr()?
        .port())
}

/// Reads go-ios's MJPEG stream over HTTP/1.0, which Go's server sends
/// unchunked, hands every picture to ffmpeg and notes when it arrived; a
/// second thread pairs ffmpeg's grey frames with those times in order.
fn stream(
    port: u16,
    decoder: &mut Child,
    base: Instant,
    error: Arc<Mutex<Option<String>>>,
) -> mpsc::Receiver<(f64, Signature)> {
    let (sender, receiver) = mpsc::channel();
    let arrivals: Arc<Mutex<VecDeque<f64>>> = Arc::default();
    let mut input = decoder.stdin.take().unwrap();
    let mut output = decoder.stdout.take().unwrap();
    let times = arrivals.clone();
    let failed = error.clone();
    std::thread::spawn(move || {
        let fail = |message: String| *failed.lock().unwrap() = Some(message);
        let deadline = Instant::now() + Duration::from_secs(30);
        let mut connection = loop {
            match std::net::TcpStream::connect(("127.0.0.1", port)) {
                Ok(connection) => break connection,
                Err(_) if Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(200))
                }
                Err(_) => {
                    return fail(format!(
                        "go-ios served no screenshot stream on port {port} within 30 s"
                    ));
                }
            }
        };
        if connection
            .write_all(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            .is_err()
        {
            return fail("The screenshot stream refused the request".into());
        }
        let mut pictures = Pictures::default();
        let mut buffer = vec![0u8; 65536];
        loop {
            let read = match connection.read(&mut buffer) {
                Ok(0) | Err(_) => return fail("go-ios ended the screenshot stream".into()),
                Ok(read) => read,
            };
            let found = match pictures.feed(&buffer[..read]) {
                Ok(found) => found,
                Err(error) => return fail(error.message),
            };
            for picture in found {
                times.lock().unwrap().push_back(elapsed_ms(base));
                if input
                    .write_all(&picture)
                    .and_then(|_| input.flush())
                    .is_err()
                {
                    return fail("ffmpeg stopped decoding the screenshot stream".into());
                }
            }
        }
    });
    std::thread::spawn(move || {
        let mut frame = vec![0u8; GRID_WIDTH * GRID_HEIGHT];
        while output.read_exact(&mut frame).is_ok() {
            let Some(at) = arrivals.lock().unwrap().pop_front() else {
                break;
            };
            if sender.send((at, frame.clone())).is_err() {
                break;
            }
        }
    });
    receiver
}

/// Follows go-ios syslog and notes when each line of the app's process arrived.
fn follow_log(child: &mut Child, process: String, base: Instant) -> Arc<Mutex<Vec<f64>>> {
    let arrivals: Arc<Mutex<Vec<f64>>> = Arc::default();
    let stdout = child.stdout.take().unwrap();
    let noted = arrivals.clone();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            let Ok(line) = line else { break };
            if iphone_device::keep(&line, &process, "").is_some() {
                noted.lock().unwrap().push(elapsed_ms(base));
            }
        }
    });
    arrivals
}

fn spawn(args: &[String], stdin: bool) -> Result<Child> {
    Command::new(&args[0])
        .args(&args[1..])
        .stdin(if stdin { Stdio::piped() } else { Stdio::null() })
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|error| Error::new("tool_failed", format!("{} did not start: {error}", args[0])))
}

const IPHONE_NOTE: &str = "Measured on the iPhone's screen through go-ios's screenshot stream, so the times are coarse: \
a change is seen when the next streamed picture arrives (resolution_ms between pictures) plus the stream's own delay, and \
every time includes go-ios connecting to the phone over USB for the launch request. Compare iPhone reports with each other, \
not with Android or the virtual iPhone.";

/// profile.startup on a USB-connected iPhone.
pub fn iphone(a: &Value) -> Result {
    let device = iphone_device::udid(s(a, "device"))?.to_string();
    let id = identifier(
        s(a, "bundle_id"),
        "application ID",
        Some(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+"),
    )?;
    if !s(a, "activity").is_empty() {
        return Err(Error::new(
            "invalid_argument",
            "activity is an Android argument; an iPhone app opens at its own first screen",
        ));
    }
    let settings = Settings {
        mode: mode(a)?,
        runs: a["runs"].as_u64().unwrap_or(5).clamp(1, 30),
        settle_ms: settle_ms(a),
        launch_timeout_ms: 60_000.0,
        quiet_timeout_ms: 20_000.0,
    };
    let go = tc::find("go-ios")?;
    let ffmpeg = tc::find("ffmpeg")?;
    let listed = iphone_device::apps(&device, false)?;
    let app = iphone_device::installed(&listed, id)?;
    let installed = identifier(
        s(app, "CFBundleIdentifier"),
        "installed application ID",
        Some(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+"),
    )?
    .to_string();
    let process = iphone_device::executable(app)?;
    let base = Instant::now();
    let port = free_port()?;
    let mut children = Children(Vec::new());
    children.0.push(spawn(
        &argv(&[
            &go,
            "screenshot",
            "--stream",
            &format!("--port={port}"),
            &format!("--udid={device}"),
        ]),
        false,
    )?);
    let mut decoder = spawn(&decoder_command(&ffmpeg), true)?;
    let stream_error = Arc::new(Mutex::new(None));
    let pictures = stream(port, &mut decoder, base, stream_error.clone());
    children.0.push(decoder);
    let mut syslog = spawn(&argv(&[&go, "syslog", &format!("--udid={device}")]), false)?;
    let logs = follow_log(&mut syslog, process, base);
    children.0.push(syslog);
    let mut phone = Iphone {
        go,
        device: device.clone(),
        installed,
        base,
        pictures,
        stream_error,
        logs,
        _children: children,
    };
    let runs = measure(&mut phone, &settings)?;
    let mut value = report(&device, "iphone", id, &settings, &runs, IPHONE_NOTE);
    value["installed_bundle_id"] = json!(phone.installed);
    crate::perf::finish(a, value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;
    use std::rc::Rc;

    /// Grid signatures of screenshots of the virtual iPhone, scaled with
    /// ffmpeg's area filter: its Home screen, the same Home screen moments
    /// later with an icon pressed and the clock moved, and the research app's
    /// window before and after its button changed the label.
    fn recorded() -> Value {
        serde_json::from_str(include_str!("../tests/fixtures/ios_startup_frames.json")).unwrap()
    }

    fn picture(name: &str) -> Signature {
        let hex = recorded()["signatures"][name].as_str().unwrap().to_string();
        (0..hex.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
            .collect()
    }

    #[test]
    fn recorded_screens_are_told_apart_and_idle_changes_are_not() {
        let home = picture("home");
        let home_later = picture("home_icon_pressed");
        let app = picture("app");
        let app_after = picture("app_after_action");
        assert_eq!(home.len(), GRID_WIDTH * GRID_HEIGHT);
        assert!(changed_fraction(&home, &app) > 0.9);
        let pressed = changed_fraction(&home, &home_later);
        assert!(pressed < CHANGE_FRACTION, "{pressed}");
        assert!(changed_fraction(&app, &app_after) < CHANGE_FRACTION);
        assert_eq!(changed_fraction(&home, &home[..10]), 1.0);
    }

    #[test]
    fn a_ppm_becomes_a_grid_of_grey_cells() {
        // 64 x 128: the left half black, the right half white.
        let (width, height) = (64, 128);
        let mut data = format!("P6\n# a comment\n{width} {height}\n255\n").into_bytes();
        for _ in 0..height {
            for x in 0..width {
                data.extend_from_slice(if x < 32 { &[0, 0, 0] } else { &[255, 255, 255] });
            }
        }
        let grid = signature_ppm(&data).unwrap();
        assert_eq!(grid.len(), GRID_WIDTH * GRID_HEIGHT);
        assert_eq!((grid[0], grid[15], grid[16], grid[31]), (0, 0, 255, 255));
        assert_eq!(grid[GRID_WIDTH * 63 + 31], 255);
        assert!(signature_ppm(b"P6\n64 128\n255\n").is_none(), "too short");
        assert!(signature_ppm(b"P5\n64 128\n255\nxx").is_none());
        let rgba = vec![200u8; 40 * 80 * 4];
        assert_eq!(signature(&rgba, 40, 80, 4).unwrap()[0], 200);
        assert!(
            signature(&rgba, 20, 80, 4).is_none(),
            "narrower than the grid"
        );
    }

    /// A launch as the frame reads see it: the Home screen ticking, the app's
    /// window zooming in over a few pictures, then still.
    fn launch_frames() -> Vec<(f64, Option<Signature>)> {
        let home = picture("home");
        let ticked = picture("home_icon_pressed");
        let app = picture("app");
        // Halfway between Home and the app: the zoom animation.
        let zoom: Signature = home
            .iter()
            .zip(&app)
            .enumerate()
            .map(|(i, (h, a))| if i % 3 == 0 { *a } else { *h })
            .collect();
        let mut frames = vec![
            (100.0, Some(ticked.clone())),
            (200.0, None),
            (300.0, Some(home.clone())),
            (400.0, Some(zoom)),
            (500.0, Some(app.clone())),
            (600.0, None),
        ];
        frames.extend((7..40).map(|i| (f64::from(i) * 100.0, None)));
        frames
    }

    #[test]
    fn the_first_changed_picture_and_the_settle_are_found() {
        let mut detector = Detector::new(picture("home"), 2000.0);
        let mut settled_at = None;
        for (at, frame) in launch_frames() {
            if detector.feed(at, frame.as_deref()) {
                settled_at = Some(at);
                break;
            }
        }
        assert_eq!(detector.first_change, Some(400.0));
        assert_eq!(detector.settled, Some(500.0));
        assert_eq!(settled_at, Some(2500.0));
    }

    #[test]
    fn a_change_that_goes_back_is_not_the_launch() {
        let home = picture("home");
        let app = picture("app");
        let mut detector = Detector::new(home.clone(), 1000.0);
        detector.feed(100.0, Some(&app));
        assert_eq!(detector.first_change, Some(100.0));
        // A notification banner that went away again, say.
        detector.feed(200.0, Some(&home));
        assert_eq!(detector.first_change, None);
        detector.feed(900.0, Some(&app));
        assert!(!detector.feed(1800.0, None));
        assert!(detector.feed(1900.0, None));
        assert_eq!(
            (detector.first_change, detector.settled),
            (Some(900.0), Some(900.0))
        );
    }

    /// A phone that plays recorded frames on a clock of its own: every look
    /// at the screen takes 100 ms, each launch shows `launch_frames`.
    struct Recorded {
        clock: Rc<Cell<f64>>,
        script: VecDeque<(f64, Option<Signature>)>,
        launch_at: Option<f64>,
        pids: VecDeque<u64>,
        calls: Vec<String>,
        open_ms: f64,
        home: Signature,
    }

    impl Recorded {
        fn new(pids: &[u64]) -> Self {
            Recorded {
                clock: Rc::new(Cell::new(0.0)),
                script: VecDeque::new(),
                launch_at: None,
                pids: pids.iter().copied().collect(),
                calls: Vec::new(),
                open_ms: 250.0,
                home: picture("home"),
            }
        }
    }

    impl Launcher for Recorded {
        fn now_ms(&self) -> f64 {
            self.clock.get()
        }
        fn frame(&mut self) -> Result<Frame> {
            let at = self.clock.get() + 100.0;
            self.clock.set(at);
            let picture = match (self.launch_at, self.script.front()) {
                // The Home screen before a launch.
                (None, _) => Some(self.home.clone()),
                (Some(launch), Some((offset, _))) if launch + offset <= at => {
                    self.script.pop_front().unwrap().1
                }
                _ => None,
            };
            Ok(Frame {
                at_ms: at,
                picture,
                sample: true,
            })
        }
        fn terminate(&mut self) -> Result<Value> {
            self.calls.push("terminate".into());
            self.launch_at = None;
            Ok(json!({"action":"terminate"}))
        }
        fn background(&mut self) -> Result<Value> {
            self.calls.push("background".into());
            self.launch_at = None;
            Ok(json!({"action":"home"}))
        }
        fn opener(&mut self) -> Opener {
            self.calls.push("open".into());
            let launch = self.clock.get();
            self.launch_at = Some(launch);
            self.script = launch_frames().into_iter().collect();
            let completed_ms = launch + self.open_ms;
            let pid = self.pids.pop_front();
            Box::new(move || {
                Ok(Opened {
                    completed_ms,
                    pid,
                    reply: json!({"ok":true}),
                })
            })
        }
    }

    fn settings(mode: &str, runs: u64) -> Settings {
        Settings {
            mode: mode.into(),
            runs,
            settle_ms: 2000.0,
            launch_timeout_ms: 60_000.0,
            quiet_timeout_ms: 20_000.0,
        }
    }

    #[test]
    fn cold_runs_end_the_app_and_report_android_metric_names() {
        let mut phone = Recorded::new(&[400, 401, 402, 402]);
        let settings = settings("cold", 3);
        let runs = measure(&mut phone, &settings).unwrap();
        assert_eq!(
            phone.calls,
            [
                "open",
                "terminate",
                "open",
                "terminate",
                "open",
                "terminate",
                "open"
            ]
        );
        // The open request starts 100 ms before the first look at the screen.
        assert_eq!(runs[0]["first_frame_ms"], 400.0);
        assert_eq!(runs[0]["launched_ms"], 250.0);
        assert_eq!(runs[0]["settled_ms"], 500.0);
        assert_eq!(runs[0]["resolution_ms"], 100.0);
        assert_eq!(runs[0]["launch_state"], "cold");
        assert_eq!(runs[2]["launch_state"], "hot", "the same process: not cold");
        let value = report(
            "virtual-iphone",
            "virtual-iphone",
            "dev.example.app",
            &settings,
            &runs,
            "note",
        );
        let metrics = &value["metrics"];
        assert_eq!(metrics["startup_cold_median_ms"], 400.0);
        assert_eq!(metrics["startup_cold_p90_ms"], 400.0);
        assert_eq!(metrics["startup_cold_launched_median_ms"], 250.0);
        assert_eq!(metrics["startup_cold_settled_p90_ms"], 500.0);
        assert!(metrics.get("startup_cold_first_log_median_ms").is_none());
        assert_eq!(value["mismatched_runs"], 1);
        assert_eq!(value["device_kind"], "virtual-iphone");
        assert_eq!(value["kind"], "startup");
        // profile.budget reads these metrics as it reads Android's.
        let budget = json!({"startup_cold_median_ms":{"maximum":500},"startup_cold_settled_p90_ms":{"maximum":400}});
        let (passed, results) = crate::perf::check_budget(&budget, metrics.as_object().unwrap());
        assert!(!passed);
        assert_eq!(results[0]["passed"], true);
        assert_eq!(results[1]["passed"], false);
    }

    #[test]
    fn hot_runs_send_the_app_away_and_keep_its_process() {
        let mut phone = Recorded::new(&[400, 400, 400]);
        let runs = measure(&mut phone, &settings("hot", 2)).unwrap();
        assert_eq!(
            phone.calls,
            ["open", "background", "open", "background", "open"]
        );
        assert!(runs.iter().all(|r| r["launch_state"] == "hot"));
        let value = report(
            "00008110-0011223344556677",
            "iphone",
            "dev.example.app",
            &settings("hot", 2),
            &runs,
            "note",
        );
        assert_eq!(value["mismatched_runs"], 0);
        assert!(value["metrics"]["startup_hot_first_log_median_ms"].is_null());
        assert_eq!(value["metrics"]["startup_hot_median_ms"], 400.0);
    }

    #[test]
    fn a_launch_that_never_shows_fails_and_an_unknown_process_is_no_mismatch() {
        let mut phone = Recorded::new(&[]);
        let mut settings = settings("cold", 1);
        settings.launch_timeout_ms = 3000.0;
        // The first open shows; afterwards the screen keeps showing Home.
        let runs = measure(&mut phone, &settings).unwrap();
        assert_eq!(runs[0]["launch_state"], "");
        assert!(runs[0]["pid"].is_null());
        struct Blank(Recorded);
        impl Launcher for Blank {
            fn now_ms(&self) -> f64 {
                self.0.now_ms()
            }
            fn frame(&mut self) -> Result<Frame> {
                let mut frame = self.0.frame()?;
                frame.picture = Some(picture("home"));
                Ok(frame)
            }
            fn terminate(&mut self) -> Result<Value> {
                self.0.terminate()
            }
            fn background(&mut self) -> Result<Value> {
                self.0.background()
            }
            fn opener(&mut self) -> Opener {
                self.0.opener()
            }
        }
        let error = measure(&mut Blank(Recorded::new(&[1, 2])), &settings).unwrap_err();
        assert_eq!(error.code, "startup_unobserved");
        assert!(mode(&json!({"mode":"warm"})).is_err());
        assert_eq!(mode(&json!({})).unwrap(), "cold");
        assert_eq!(settle_ms(&json!({"settle_ms":50})), 200.0);
    }

    #[test]
    fn go_ios_launch_output_names_the_process() {
        assert_eq!(
            launched_pid(
                "{\"level\":\"info\",\"msg\":\"Process launched\",\"pid\":1234,\"time\":\"2026-11-01T10:00:00Z\"}\n"
            ),
            Some(1234)
        );
        assert_eq!(
            launched_pid("time=\"x\" level=info msg=\"Process launched\" pid=77\n"),
            Some(77)
        );
        assert_eq!(launched_pid("nothing here"), None);
    }

    #[test]
    fn the_mjpeg_stream_is_cut_into_pictures_across_reads() {
        let mut pictures = Pictures::default();
        let header = b"HTTP/1.0 200 OK\r\nContent-Type: multipart/x-mixed-replace;boundary=b\r\n\r\n--b\r\n\r\n";
        let mut first = header.to_vec();
        first.extend_from_slice(b"\xff\xd8one\xff");
        assert!(pictures.feed(&first).unwrap().is_empty());
        let found = pictures
            .feed(b"\xd9\r\n--b\r\n\r\n\xff\xd8two\xff\xd9\r\n--b\r\n\xff")
            .unwrap();
        assert_eq!(
            found,
            [
                b"\xff\xd8one\xff\xd9".to_vec(),
                b"\xff\xd8two\xff\xd9".to_vec()
            ]
        );
        assert_eq!(
            pictures.feed(b"\xd8three\xff\xd9").unwrap(),
            [b"\xff\xd8three\xff\xd9".to_vec()]
        );
        let command = decoder_command("/usr/bin/ffmpeg");
        assert!(command.contains(&"scale=32:64:flags=area,format=gray".to_string()));
    }
}
