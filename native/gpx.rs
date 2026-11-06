//! GPX routes: read a file, say what is in it, and play it back on an
//! emulator's location as a durable job that can be paused, resumed, looped
//! and stopped.
//!
//! Playback reaches the device through the emulator console (`geo fix`), the
//! same path `android.emulator` with `location` takes. That is the only
//! location transport Tomacco has today: a physical Android phone needs a
//! mock-location app and an iPhone a developer-mode location service,
//! neither of which is wired here, so both answer `gpx_unsupported`. The
//! emulator does not report its current fix, so the previous location is not
//! known and nothing claims to restore it.
//!
//! The parser accepts no DOCTYPE or entity declarations (a GPX file needs
//! none, and an external entity could read files or reach the network), and
//! bounds the file size and the point count.
use crate::core::*;
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, Write},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicU8, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

const SIZE_LIMIT: u64 = 16 * 1024 * 1024;
const POINT_LIMIT: usize = 200_000;

#[derive(Debug, Clone, PartialEq)]
pub struct Point {
    pub lat: f64,
    pub lon: f64,
    pub ele: Option<f64>,
    /// Unix seconds, when the point carries a `<time>`.
    pub time: Option<f64>,
}

#[derive(Debug, Default)]
pub struct Route {
    pub name: String,
    pub points: Vec<Point>,
    pub tracks: usize,
    pub segments: usize,
    pub routes: usize,
    pub waypoints: usize,
    /// `track`, `route` or `waypoints`: where the points came from.
    pub source: &'static str,
}

fn invalid(message: impl Into<String>) -> Error {
    Error::new("gpx_invalid", message)
}

/// Days since 1970-01-01 of a civil date (Howard Hinnant's algorithm).
fn days_from_civil(y: i64, m: u32, d: u32) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = (m as i64 + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d as i64 - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// An ISO 8601 time as GPX writes it (`2024-05-01T10:00:00Z`, with an
/// optional fraction and a `Z` or `±HH:MM` offset) as Unix seconds.
pub fn parse_time(text: &str) -> Option<f64> {
    let text = text.trim();
    let (date, rest) = text.split_once('T')?;
    let mut parts = date.split('-');
    let year: i64 = parts.next()?.parse().ok()?;
    let month: u32 = parts.next()?.parse().ok()?;
    let day: u32 = parts.next()?.parse().ok()?;
    if parts.next().is_some() || !(1..=12).contains(&month) || !(1..=31).contains(&day) {
        return None;
    }
    let (clock, offset) = match rest.find(['Z', 'z', '+', '-']) {
        Some(at) => (&rest[..at], &rest[at..]),
        None => (rest, "Z"),
    };
    let mut clock_parts = clock.split(':');
    let hour: u32 = clock_parts.next()?.parse().ok()?;
    let minute: u32 = clock_parts.next()?.parse().ok()?;
    let second: f64 = clock_parts.next().unwrap_or("0").parse().ok()?;
    if hour > 23 || minute > 59 || !(0.0..61.0).contains(&second) {
        return None;
    }
    let offset_seconds = match offset {
        "Z" | "z" | "" => 0.0,
        signed => {
            let sign = if signed.starts_with('-') { -1.0 } else { 1.0 };
            let (h, m) = signed[1..].split_once(':').unwrap_or((&signed[1..], "0"));
            sign * (h.parse::<f64>().ok()? * 3600.0 + m.parse::<f64>().ok()? * 60.0)
        }
    };
    Some(days_from_civil(year, month, day) as f64 * 86_400.0 + f64::from(hour) * 3600.0 + f64::from(minute) * 60.0 + second - offset_seconds)
}

fn coordinate(node: roxmltree::Node, name: &str, limit: f64) -> Result<f64> {
    let value: f64 = node
        .attribute(name)
        .ok_or_else(|| invalid(format!("A point has no {name}")))?
        .trim()
        .parse()
        .map_err(|_| invalid(format!("A point's {name} is not a number")))?;
    if !value.is_finite() || value.abs() > limit {
        return Err(invalid(format!("A point's {name} {value} is out of range")));
    }
    Ok(value)
}

fn point(node: roxmltree::Node) -> Result<Point> {
    let lat = coordinate(node, "lat", 90.0)?;
    let lon = coordinate(node, "lon", 180.0)?;
    let child_text = |name: &str| {
        node.children()
            .find(|c| c.is_element() && c.tag_name().name() == name)
            .and_then(|c| c.text())
            .map(str::trim)
            .filter(|t| !t.is_empty())
    };
    let ele = child_text("ele").and_then(|t| t.parse::<f64>().ok()).filter(|e| e.is_finite() && e.abs() < 100_000.0);
    let time = match child_text("time") {
        Some(text) => Some(parse_time(text).ok_or_else(|| invalid(format!("A point's time {text:?} is not ISO 8601")))?),
        None => None,
    };
    Ok(Point { lat, lon, ele, time })
}

/// Reads a GPX document: the track points in document order (or the route
/// points, or the waypoints, when there are no track points).
pub fn parse(text: &str) -> Result<Route> {
    let head: String = text.chars().take(4096).collect::<String>().to_ascii_lowercase();
    if text.to_ascii_lowercase().contains("<!doctype") || text.to_ascii_lowercase().contains("<!entity") {
        return Err(invalid("A GPX file with a DOCTYPE or entity declaration is not read: it could point at other files or the network")
            .remedy("Remove the declaration; GPX needs none"));
    }
    let document = roxmltree::Document::parse(text).map_err(|e| invalid(format!("Not well-formed XML: {e}")))?;
    let root = document.root_element();
    if root.tag_name().name() != "gpx" {
        return Err(invalid(format!("The document's root is <{}>, not <gpx>", root.tag_name().name())));
    }
    let _ = head;
    let mut route = Route::default();
    route.name = root
        .children()
        .find(|c| c.is_element() && c.tag_name().name() == "metadata")
        .and_then(|m| m.children().find(|c| c.is_element() && c.tag_name().name() == "name"))
        .and_then(|n| n.text())
        .map(|t| t.trim().chars().take(120).collect())
        .unwrap_or_default();
    let mut track_points = Vec::new();
    let mut route_points = Vec::new();
    let mut waypoints = Vec::new();
    for element in root.children().filter(|c| c.is_element()) {
        match element.tag_name().name() {
            "trk" => {
                route.tracks += 1;
                if route.name.is_empty() {
                    if let Some(name) = element.children().find(|c| c.is_element() && c.tag_name().name() == "name").and_then(|n| n.text()) {
                        route.name = name.trim().chars().take(120).collect();
                    }
                }
                for segment in element.children().filter(|c| c.is_element() && c.tag_name().name() == "trkseg") {
                    route.segments += 1;
                    for p in segment.children().filter(|c| c.is_element() && c.tag_name().name() == "trkpt") {
                        track_points.push(point(p)?);
                        if track_points.len() > POINT_LIMIT {
                            return Err(Error::new("gpx_too_large", format!("More than {POINT_LIMIT} points")));
                        }
                    }
                }
            }
            "rte" => {
                route.routes += 1;
                for p in element.children().filter(|c| c.is_element() && c.tag_name().name() == "rtept") {
                    route_points.push(point(p)?);
                    if route_points.len() > POINT_LIMIT {
                        return Err(Error::new("gpx_too_large", format!("More than {POINT_LIMIT} points")));
                    }
                }
            }
            "wpt" => {
                route.waypoints += 1;
                waypoints.push(point(element)?);
                if waypoints.len() > POINT_LIMIT {
                    return Err(Error::new("gpx_too_large", format!("More than {POINT_LIMIT} points")));
                }
            }
            _ => {}
        }
    }
    let (points, source) = if !track_points.is_empty() {
        (track_points, "track")
    } else if !route_points.is_empty() {
        (route_points, "route")
    } else {
        (waypoints, "waypoints")
    };
    if points.len() < 2 {
        return Err(invalid("A route needs at least two points"));
    }
    route.points = points;
    route.source = source;
    Ok(route)
}

/// Great-circle distance in metres.
pub fn haversine(a: &Point, b: &Point) -> f64 {
    let r = 6_371_008.8;
    let (lat1, lat2) = (a.lat.to_radians(), b.lat.to_radians());
    let dlat = lat2 - lat1;
    let dlon = (b.lon - a.lon).to_radians();
    let h = (dlat / 2.0).sin().powi(2) + lat1.cos() * lat2.cos() * (dlon / 2.0).sin().powi(2);
    2.0 * r * h.sqrt().asin()
}

/// Whether every point has a time and the times never go backwards.
pub fn timed(points: &[Point]) -> (bool, bool) {
    let all = points.iter().all(|p| p.time.is_some());
    let ordered = all
        && points
            .windows(2)
            .all(|w| w[1].time.unwrap() >= w[0].time.unwrap());
    (all, ordered)
}

/// What a route is, for a person and a planner: counts, bounds, distance,
/// duration and speed where the points carry time, and how untimed points
/// are played (document order at a fixed interval).
pub fn summary(route: &Route) -> Value {
    let points = &route.points;
    let mut distance = 0.0;
    for w in points.windows(2) {
        distance += haversine(&w[0], &w[1]);
    }
    let (lat_min, lat_max) = points.iter().fold((90.0f64, -90.0f64), |(lo, hi), p| (lo.min(p.lat), hi.max(p.lat)));
    let (lon_min, lon_max) = points.iter().fold((180.0f64, -180.0f64), |(lo, hi), p| (lo.min(p.lon), hi.max(p.lon)));
    let (all_timed, ordered) = timed(points);
    let duration = if all_timed {
        Some(points.last().unwrap().time.unwrap() - points[0].time.unwrap())
    } else {
        None
    };
    let round = |v: f64| (v * 1000.0).round() / 1000.0;
    let speed = duration.filter(|d| *d > 0.0).map(|d| round(distance / d * 3.6));
    let timing = if !all_timed {
        json!({"timed":false,"ordering":"document order","note":"The points carry no times (or not all do): playback sends them in document order, one every interval_ms, divided by speed."})
    } else if !ordered {
        json!({"timed":true,"ordering":"document order","note":"Some times go backwards: playback keeps document order and treats a backwards step as the interval."})
    } else {
        json!({"timed":true,"ordering":"time","note":"Playback waits the recorded time between points, divided by speed."})
    };
    json!({"name":route.name,"source":route.source,"points":points.len(),"tracks":route.tracks,"segments":route.segments,
        "routes":route.routes,"waypoints":route.waypoints,"with_elevation":points.iter().filter(|p| p.ele.is_some()).count(),
        "bounds":{"lat_min":lat_min,"lat_max":lat_max,"lon_min":lon_min,"lon_max":lon_max},
        "first":{"lat":points[0].lat,"lon":points[0].lon},"last":{"lat":points.last().unwrap().lat,"lon":points.last().unwrap().lon},
        "distance_m":round(distance),"duration_s":duration.map(round),"average_speed_kmh":speed,"timing":timing})
}

fn read_file(value: &str) -> Result<(PathBuf, String)> {
    let file = path(value, false, true)?;
    let size = fs::metadata(&file)?.len();
    if size > SIZE_LIMIT {
        return Err(Error::new("gpx_too_large", format!("The file is {size} bytes; the limit is {SIZE_LIMIT}")));
    }
    let text = fs::read_to_string(&file).map_err(|e| invalid(format!("The file is not UTF-8 text: {e}")))?;
    Ok((file, text))
}

/// Which devices playback reaches, and why not the others.
fn transport(device: &str) -> Result<&'static str> {
    identifier(device, "device", None)?;
    if device.starts_with("emulator-") {
        return Ok("emulator console (geo fix)");
    }
    let why = if device == "virtual-iphone" || device.contains('-') && device.len() >= 25 {
        "an iPhone takes locations only through a developer location service, which is not wired here"
    } else {
        "a physical Android device takes mock locations only through a mock-location app, which is not wired here"
    };
    Err(Error::new("gpx_unsupported", format!("Route playback reaches Android emulators only; {why}"))
        .remedy("Play the route on an emulator (emulator-NNNN from device.list), or set single fixes with the device's own tools")
        .details(json!({"device":device,"supported":["android emulator"]})))
}

pub fn inspect(a: &Value) -> Result {
    let (file, text) = read_file(s(a, "path"))?;
    let route = parse(&text)?;
    let mut result = summary(&route);
    result["path"] = json!(file);
    result["bytes"] = json!(text.len());
    result["playback"] = json!({"supported":["android emulator (emulator-NNNN) through the console"],
        "unsupported":{"android device":"needs a mock-location app, not wired","iphone":"needs a developer location service, not wired"},
        "baseline":"the emulator does not report its current fix, so the previous location is unknown and is not restored",
        "play_with":"gpx.play"});
    if let Some(device) = a["device"].as_str() {
        result["device"] = match transport(device) {
            Ok(how) => json!({"id":device,"supported":true,"transport":how}),
            Err(error) => json!({"id":device,"supported":false,"error":error.value()}),
        };
    }
    Ok(result)
}

fn requests_dir() -> Result<PathBuf> {
    let dir = home()?.join("gpx");
    private_dir(&dir)?;
    Ok(dir)
}

pub fn play(a: &Value) -> Result {
    let device = s(a, "device");
    let how = transport(device)?;
    let (file, text) = read_file(s(a, "path"))?;
    let route = parse(&text)?;
    let speed = a["speed"].as_f64().unwrap_or(1.0).clamp(0.1, 100.0);
    let interval = a["interval_ms"].as_u64().unwrap_or(1000).clamp(100, 60_000);
    let repeat = b(a, "loop");
    let start = a["start_index"].as_u64().unwrap_or(0) as usize;
    if start >= route.points.len() {
        return Err(Error::new("invalid_argument", format!("start_index {start} is past the last point")));
    }
    let summary = summary(&route);
    let (all_timed, ordered) = timed(&route.points);
    let recorded = if all_timed && ordered {
        summary["duration_s"].as_f64().unwrap_or(0.0)
    } else {
        (route.points.len() as f64) * interval as f64 / 1000.0
    };
    let estimated = recorded / speed;
    let id = uuid::Uuid::new_v4().simple().to_string();
    let request = requests_dir()?.join(format!("{id}.json"));
    let points: Vec<Value> = route
        .points
        .iter()
        .map(|p| json!({"lat":p.lat,"lon":p.lon,"ele":p.ele,"time":p.time}))
        .collect();
    atomic_json(
        &request,
        &json!({"id":id,"device":device,"path":file,"speed":speed,"interval_ms":interval,"loop":repeat,"start_index":start,
            "points":points,"timed":all_timed && ordered,"status_file":requests_dir()?.join(format!("{id}.status.json"))}),
    )?;
    let timeout = if repeat { 86_400 } else { ((estimated * 2.0) as u64 + 120).min(86_400) };
    let step = json!({"argv":[own_executable()?.to_string_lossy(),"_gpx_play",request.to_string_lossy()],
        "interactive":true,"stoppable":true,"pausable":true,"phase":"streaming"});
    let job = crate::devices::submit("Play route", json!([step]), timeout, json!({}))?;
    Ok(json!({"job":job,"route":summary,"device":device,"transport":how,"speed":speed,"interval_ms":interval,"loop":repeat,
        "start_index":start,"estimated_seconds":(estimated * 10.0).round() / 10.0,
        "baseline":{"known":false,"restorable":false,"note":"The emulator does not report its current fix; the location before playback is unknown and is not restored. Set one with android.emulator location afterwards if you need to."},
        "controls":{"pause":"job.send command pause","resume":"job.send command resume","stop":"job.send command stop (keeps the last fix) or job.cancel","status":"gpx.status"},
        "request":request}))
}

/// Where playback is: the status file the worker writes with every fix.
pub fn status(a: &Value) -> Result {
    let id = s(a, "job_id");
    let job = crate::jobs::get(id)?;
    let argv = job["plan"]["steps"][0]["argv"].clone();
    let request = argv[2].as_str().filter(|_| argv[1] == "_gpx_play").ok_or_else(|| {
        Error::new("gpx_not_playback", "This job is not a route playback").remedy("Pass the job gpx.play answered")
    })?;
    let status_file = Path::new(request).with_file_name(
        Path::new(request)
            .file_stem()
            .map(|s| format!("{}.status.json", s.to_string_lossy()))
            .unwrap_or_default(),
    );
    let progress = read_json(&status_file, Some(json!({"index":Value::Null,"state":"not_started"})))?;
    Ok(json!({"job_id":id,"job_state":job["state"],"progress":progress}))
}

/// The playback worker: `tomaccoctl _gpx_play REQUEST`. Reads pause, resume
/// and stop from standard input, which `job.send` feeds.
pub fn worker(request: &str) -> Result<()> {
    let spec = read_json(Path::new(request), None)?;
    let device = s(&spec, "device").to_string();
    let status_file = PathBuf::from(s(&spec, "status_file"));
    let points = spec["points"].as_array().cloned().unwrap_or_default();
    let speed = spec["speed"].as_f64().unwrap_or(1.0).clamp(0.1, 100.0);
    let interval = Duration::from_millis(n(&spec, "interval_ms").max(100));
    let timed = spec["timed"] == true;
    let repeat = spec["loop"] == true;
    let start = n(&spec, "start_index") as usize;
    // 0 playing, 1 paused, 2 stop. `job.send stop` and `job.cancel` arrive as
    // signals, which end playback the same graceful way as a stop line.
    let control = Arc::new(AtomicU8::new(0));
    static SIGNALLED: AtomicU8 = AtomicU8::new(0);
    extern "C" fn on_signal(_: libc::c_int) {
        SIGNALLED.store(2, Ordering::Relaxed);
    }
    // SAFETY: the handler only stores into an atomic.
    unsafe {
        libc::signal(libc::SIGINT, on_signal as *const () as usize);
        libc::signal(libc::SIGTERM, on_signal as *const () as usize);
    }
    let flag = control.clone();
    thread::spawn(move || {
        for line in std::io::stdin().lock().lines() {
            let Ok(line) = line else { break };
            match line.trim() {
                "pause" => flag.store(1, Ordering::Relaxed),
                "resume" => flag.store(0, Ordering::Relaxed),
                "stop" | "q" => flag.store(2, Ordering::Relaxed),
                _ => {}
            }
        }
    });
    let mut out = std::io::stdout();
    let mut lap = 0u64;
    let mut sent = 0u64;
    let write_status = |state: &str, index: usize, lap: u64, sent: u64, point: &Value| {
        let _ = atomic_json(&status_file, &json!({"state":state,"index":index,"total":points.len(),"lap":lap,"sent":sent,
            "lat":point["lat"],"lon":point["lon"],"updated":now(),"device":device}));
    };
    'laps: loop {
        let mut index = if lap == 0 { start } else { 0 };
        while index < points.len() {
            match control.load(Ordering::Relaxed).max(SIGNALLED.load(Ordering::Relaxed)) {
                2 => {
                    write_status("stopped", index, lap, sent, &points[index.min(points.len() - 1)]);
                    writeln!(out, "[gpx] stopped at point {index}; the last fix stays")?;
                    return Ok(());
                }
                1 => {
                    write_status("paused", index, lap, sent, &points[index]);
                    thread::sleep(Duration::from_millis(100));
                    continue;
                }
                _ => {}
            }
            let point = &points[index];
            let started = Instant::now();
            let mut command = vec!["geo".to_string(), "fix".to_string(), point["lon"].to_string(), point["lat"].to_string()];
            if let Some(ele) = point["ele"].as_f64() {
                command.push(ele.to_string());
            }
            let words: Vec<&str> = command.iter().map(String::as_str).collect();
            crate::devices::console(&device, &words)?;
            sent += 1;
            write_status("playing", index, lap, sent, point);
            writeln!(out, "[gpx] {index}/{} lat {} lon {}", points.len(), point["lat"], point["lon"])?;
            out.flush()?;
            let wait = if timed && index + 1 < points.len() {
                let dt = points[index + 1]["time"].as_f64().unwrap_or(0.0) - point["time"].as_f64().unwrap_or(0.0);
                if dt > 0.0 {
                    Duration::from_secs_f64((dt / speed).min(3600.0))
                } else {
                    interval.div_f64(speed)
                }
            } else {
                interval.div_f64(speed)
            };
            let due = started + wait;
            while Instant::now() < due {
                if control.load(Ordering::Relaxed) == 2 || SIGNALLED.load(Ordering::Relaxed) == 2 {
                    continue 'laps;
                }
                thread::sleep(Duration::from_millis(50).min(due.saturating_duration_since(Instant::now())));
            }
            index += 1;
        }
        if !repeat {
            break;
        }
        lap += 1;
    }
    let last = points.last().cloned().unwrap_or(Value::Null);
    write_status("finished", points.len(), lap, sent, &last);
    writeln!(out, "[gpx] finished: {sent} fixes sent")?;
    Ok(())
}

pub fn handle(op: &str, a: &Value) -> Result {
    match op {
        "gpx.inspect" => inspect(a),
        "gpx.play" => play(a),
        "gpx.status" => status(a),
        _ => Err(Error::new("operation_not_found", op)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const TRACK: &str = r#"<?xml version="1.0"?>
<gpx version="1.1" creator="test"><metadata><name>Ring</name></metadata>
<trk><name>Morning</name><trkseg>
<trkpt lat="48.2082" lon="16.3738"><ele>171</ele><time>2026-05-01T10:00:00Z</time></trkpt>
<trkpt lat="48.2092" lon="16.3748"><ele>172.5</ele><time>2026-05-01T10:00:30.5Z</time></trkpt>
<trkpt lat="48.2102" lon="16.3758"><time>2026-05-01T12:01:00+02:00</time></trkpt>
</trkseg></trk></gpx>"#;
    #[test]
    fn a_timed_track_is_read_with_distance_duration_and_order() {
        let route = parse(TRACK).unwrap();
        assert_eq!((route.name.as_str(), route.source, route.points.len(), route.tracks, route.segments), ("Ring", "track", 3, 1, 1));
        assert_eq!(route.points[1].ele, Some(172.5));
        assert_eq!(route.points[2].time, Some(parse_time("2026-05-01T10:01:00Z").unwrap()));
        let summary = summary(&route);
        assert_eq!(summary["timing"]["ordering"], "time");
        assert_eq!(summary["duration_s"], 60.0);
        let distance = summary["distance_m"].as_f64().unwrap();
        assert!((distance - 270.0).abs() < 15.0, "{distance}");
        assert!(summary["average_speed_kmh"].as_f64().unwrap() > 10.0);
        assert_eq!(summary["with_elevation"], 2);
        assert_eq!(parse_time("1970-01-02T00:00:00Z"), Some(86_400.0));
        assert_eq!(parse_time("2024-03-01T00:00:00Z"), Some(1_709_251_200.0));
        assert_eq!(parse_time("not a time"), None);
    }
    #[test]
    fn untimed_routes_waypoints_entities_and_bad_points_are_handled() {
        let route = parse("<gpx><rte><rtept lat=\"1\" lon=\"2\"/><rtept lat=\"1.1\" lon=\"2\"/></rte></gpx>").unwrap();
        assert_eq!(route.source, "route");
        let summary = summary(&route);
        assert_eq!(summary["timing"]["timed"], false);
        assert_eq!(summary["timing"]["ordering"], "document order");
        assert!(summary["duration_s"].is_null());
        let points = parse("<gpx><wpt lat=\"1\" lon=\"2\"/><wpt lat=\"1\" lon=\"3\"/></gpx>").unwrap();
        assert_eq!(points.source, "waypoints");
        // Times that go backwards keep document order.
        let back = parse("<gpx><trk><trkseg><trkpt lat=\"1\" lon=\"2\"><time>2026-01-01T00:00:10Z</time></trkpt><trkpt lat=\"1\" lon=\"2.1\"><time>2026-01-01T00:00:00Z</time></trkpt></trkseg></trk></gpx>").unwrap();
        assert_eq!(super::summary(&back)["timing"]["ordering"], "document order");
        let entity = "<!DOCTYPE gpx [<!ENTITY x SYSTEM \"file:///etc/passwd\">]><gpx><wpt lat=\"1\" lon=\"2\"/><wpt lat=\"1\" lon=\"3\"/></gpx>";
        assert_eq!(parse(entity).unwrap_err().code, "gpx_invalid");
        assert_eq!(parse("<gpx><wpt lat=\"91\" lon=\"2\"/><wpt lat=\"1\" lon=\"3\"/></gpx>").unwrap_err().code, "gpx_invalid");
        assert_eq!(parse("<gpx><wpt lat=\"1\" lon=\"2\"/></gpx>").unwrap_err().code, "gpx_invalid");
        assert_eq!(parse("<kml/>").unwrap_err().code, "gpx_invalid");
        assert_eq!(parse("<gpx><trk><trkseg><trkpt lat=\"1\" lon=\"2\"><time>yesterday</time></trkpt></trkseg></trk></gpx>").unwrap_err().code, "gpx_invalid");
        assert_eq!(transport("emulator-5554").unwrap(), "emulator console (geo fix)");
        assert_eq!(transport("R5CT123").unwrap_err().code, "gpx_unsupported");
        assert_eq!(transport("00008110-001A2B3C4D5E6F7G").unwrap_err().code, "gpx_unsupported");
    }
}
