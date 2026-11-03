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
}

pub fn connect(a: &Value, shared: Shared, dir: &Path) -> Result<Airplay> {
    validate(a)?;
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))?;
    let port = listener.local_addr()?.port();
    let fps = (n(a, "fps") as u64).clamp(1, 60);
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
    let reason_log = log.clone();
    thread::spawn(move || {
        // UxPlay's video pipeline connects when it starts, before any phone
        // does: who mirrors is known only once its log names the phone.
        let mut seen = String::new();
        while Path::new(&format!("/proc/{pid}")).exists()
            && !fs::read_to_string(format!("/proc/{pid}/stat"))
                .unwrap_or_default()
                .contains(") Z ")
        {
            if watched.lock().unwrap().stopped.load(Ordering::Relaxed) {
                return;
            }
            let latest = fs::read_to_string(&reason_log)
                .unwrap_or_default()
                .lines()
                .rev()
                .find(|l| l.contains("connection request from "))
                .unwrap_or("")
                .to_owned();
            if latest != seen {
                seen = latest;
                let _ = watched.lock().unwrap().update(identify(&reason_log));
            }
            thread::sleep(Duration::from_millis(500));
        }
        let mut hub = watched.lock().unwrap();
        let _ = hub.update(json!({"error":exit_reason(&reason_log)}));
        hub.stopped.store(true, Ordering::Relaxed);
    });
    let hub = shared.clone();
    thread::spawn(move || {
        // Each mirroring run from the phone is one connection; stopping and
        // starting Screen Mirroring again connects anew.
        for stream in listener.incoming() {
            if hub.lock().unwrap().stopped.load(Ordering::Relaxed) {
                break;
            }
            let Ok(mut stream) = stream else { continue };
            let _ = hub.lock().unwrap().update(identify(&log));
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
            let _ = hub.lock().unwrap().update(json!({"mirroring":false}));
        }
    });
    Ok(Airplay { _process: process })
}

/// What UxPlay's log says about the phone that connected: its name and its
/// hardware identifier, which picks the model's frame.
fn identify(log: &Path) -> Value {
    let text = fs::read_to_string(log).unwrap_or_default();
    // Another phone after this one must not keep this one's frame.
    let mut found = json!({"mirroring":true,"name":"iPhone (AirPlay)","client":null,"product_type":null,"profile_id":null,"profile_source":null});
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
                    if let Some(profile) =
                        iphone::profile_for(&crate::catalog::builtin(), product)
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

/// Why UxPlay ended, from its last error line.
fn exit_reason(log: &Path) -> String {
    let text = fs::read_to_string(log).unwrap_or_default();
    match text.lines().rev().find(|l| l.contains("ERROR")) {
        Some(line) if line.contains("socket 98") => {
            "The AirPlay ports are in use: another AirPlay receiver, such as UxPlay, is running".into()
        }
        Some(line) => format!("UxPlay stopped: {}", line.trim_start_matches(['*', ' ']).trim()),
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
                starts.push(if i > 0 && self.buffer[i - 1] == 0 { i - 1 } else { i });
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
    let header = if unit.starts_with(&[0, 0, 0, 1]) { 4 } else { 3 };
    match unit.get(header).map(|b| b & 31) {
        Some(7 | 8) => (true, false),
        Some(5) => (false, true),
        _ => (false, false),
    }
}

impl Device for Airplay {
    fn input(&mut self, _a: &Value) -> Result {
        Err(err(
            "AirPlay mirroring carries no input; connect the iPhone by USB to control it",
        ))
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
            assert_eq!(out, vec![sps.to_vec(), pps.to_vec(), idr.to_vec()], "chunk size {size}");
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
        fs::write(&log, "connection request from Mac (Mac14,2) with deviceID = 1\n").unwrap();
        let other = identify(&log);
        assert!(other["profile_id"].is_null());
        assert_eq!(other["name"], "iPhone (AirPlay)");
        fs::write(&log, "using network ports UDP 7011\n*** ERROR: Error initialising socket 98\n").unwrap();
        assert!(exit_reason(&log).contains("ports are in use"));
        fs::write(&log, "*** ERROR: something else\n").unwrap();
        assert_eq!(exit_reason(&log), "UxPlay stopped: ERROR: something else");
    }
}
