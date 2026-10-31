//! Downloads of SDK and tool archives that resume and are verified before
//! use.
//!
//! A download goes to `NAME.part` next to its destination. An interrupted
//! one is continued with an HTTP range request on the next call, and the file
//! becomes `NAME` only after its checksum matches. A file that fails the
//! check, and partial files left untouched for a week, are deleted.
use crate::core::*;
use sha2::Digest;
use std::{
    fs::{self, File},
    io::Read,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, SystemTime},
};

/// How long an abandoned partial download is kept for resuming.
const PARTIAL_AGE: Duration = Duration::from_secs(7 * 24 * 3600);
/// curl's exit status when the server cannot continue at an offset.
const CURL_CANNOT_RESUME: i32 = 33;
/// curl's exit status for an HTTP error with --fail, such as 416 for a range
/// past the end of a file that changed.
const CURL_HTTP_ERROR: i32 = 22;

#[derive(Clone, Debug, PartialEq)]
pub enum Checksum {
    Sha256(String),
    /// Google's repository index publishes only SHA-1.
    Sha1(String),
}

pub struct Request<'a> {
    pub url: &'a str,
    pub destination: &'a Path,
    pub checksum: Checksum,
    pub max_bytes: u64,
    /// Show curl's progress bar on the terminal rather than capture it.
    pub progress: bool,
    /// Tests serve plain HTTP on loopback; real downloads are HTTPS only.
    pub allow_http: bool,
    pub curl: &'a str,
}

/// The directory for cached and partial downloads.
pub fn directory() -> Result<PathBuf> {
    let directory = home()?.join("downloads");
    private_dir(&directory)?;
    Ok(directory)
}

pub fn file_checksum(path: &Path, checksum: &Checksum) -> Result<bool> {
    let mut file = File::open(path)?;
    let mut buffer = vec![0; 1024 * 1024];
    match checksum {
        Checksum::Sha256(expected) => {
            let mut hasher = sha2::Sha256::new();
            loop {
                let read = file.read(&mut buffer)?;
                if read == 0 {
                    break;
                }
                hasher.update(&buffer[..read]);
            }
            let actual: String = hasher
                .finalize()
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect();
            Ok(actual.eq_ignore_ascii_case(expected))
        }
        Checksum::Sha1(expected) => {
            let output = Command::new("sha1sum").arg(path).output()?;
            if !output.status.success() {
                return Err(Error::new("tool_missing", "sha1sum failed"));
            }
            Ok(String::from_utf8_lossy(&output.stdout)
                .split_whitespace()
                .next()
                .is_some_and(|actual| actual.eq_ignore_ascii_case(expected)))
        }
    }
}

fn partial_path(destination: &Path) -> PathBuf {
    let mut name = destination.file_name().unwrap_or_default().to_os_string();
    name.push(".part");
    destination.with_file_name(name)
}

/// Removes `*.part` files in `directory` that nobody wrote to for `age`.
pub fn remove_stale_partials(directory: &Path, age: Duration) -> Vec<PathBuf> {
    let mut removed = Vec::new();
    let cutoff = SystemTime::now().checked_sub(age);
    for entry in fs::read_dir(directory).into_iter().flatten().flatten() {
        let path = entry.path();
        let stale = entry.metadata().ok().filter(|m| m.is_file()).and_then(|m| {
            m.modified()
                .ok()
                .zip(cutoff)
                .map(|(modified, cutoff)| modified < cutoff)
        });
        if path.extension().is_some_and(|e| e == "part")
            && stale == Some(true)
            && fs::remove_file(&path).is_ok()
        {
            removed.push(path);
        }
    }
    removed
}

fn curl_arguments(request: &Request<'_>, partial: &Path) -> Vec<String> {
    let protocols = if request.allow_http {
        "=http,https"
    } else {
        "=https"
    };
    let mut args = argv(&[
        "--fail",
        "--location",
        "--proto",
        protocols,
        "--proto-redir",
        protocols,
        "--retry",
        "3",
        "--connect-timeout",
        "30",
        "--speed-limit",
        "1024",
        "--speed-time",
        "60",
        "--max-filesize",
        &request.max_bytes.to_string(),
        "--continue-at",
        "-",
        "--output",
        &partial.to_string_lossy(),
    ]);
    args.push(if request.progress {
        "--progress-bar".into()
    } else {
        "--silent".into()
    });
    args.push("--show-error".into());
    args.push(request.url.into());
    args
}

fn run_curl(request: &Request<'_>, partial: &Path) -> Result<(i32, String)> {
    let mut command = Command::new(request.curl);
    command
        .args(curl_arguments(request, partial))
        .stdin(Stdio::null());
    if request.progress {
        let status = command.status()?;
        return Ok((status.code().unwrap_or(-1), String::new()));
    }
    let output = command.output()?;
    Ok((
        output.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&output.stderr).trim().to_string(),
    ))
}

/// The verified file at `request.destination`, downloaded or continued as
/// needed. A destination that already holds the right bytes is reused.
pub fn fetch(request: &Request<'_>) -> Result<serde_json::Value> {
    let destination = request.destination;
    if let Some(parent) = destination.parent() {
        private_dir(parent)?;
        remove_stale_partials(parent, PARTIAL_AGE);
    }
    if destination.is_file() {
        if file_checksum(destination, &request.checksum)? {
            return Ok(serde_json::json!({"path":destination,"reused":true,"resumed":false}));
        }
        fs::remove_file(destination)?;
    }
    let partial = partial_path(destination);
    let resumed_from = fs::metadata(&partial).map(|m| m.len()).unwrap_or(0);
    let (mut code, mut message) = run_curl(request, &partial)?;
    if code != 0 && resumed_from > 0 && [CURL_CANNOT_RESUME, CURL_HTTP_ERROR].contains(&code) {
        // The server cannot continue, or the file changed under the range:
        // start again from nothing, once.
        let _ = fs::remove_file(&partial);
        (code, message) = run_curl(request, &partial)?;
    }
    if code != 0 {
        let kept = fs::metadata(&partial).map(|m| m.len()).unwrap_or(0);
        let mut error = Error::new(
            "download_failed",
            format!(
                "Downloading {} failed (curl exit {code}): {message}",
                request.url
            ),
        )
        .details(serde_json::json!({"url":request.url,"partial_bytes":kept}));
        if kept > 0 {
            error = error.remedy("Run it again: the download continues where it stopped");
        }
        return Err(error);
    }
    if !file_checksum(&partial, &request.checksum)? {
        let _ = fs::remove_file(&partial);
        return Err(Error::new(
            "checksum_mismatch",
            format!(
                "The download from {} does not match its published checksum and was deleted",
                request.url
            ),
        ));
    }
    fs::rename(&partial, destination)?;
    Ok(
        serde_json::json!({"path":destination,"reused":false,"resumed":resumed_from>0,"resumed_from":resumed_from}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        io::{BufRead, BufReader, Write},
        net::TcpListener,
        sync::{Arc, Mutex},
        thread,
    };

    fn sha256(bytes: &[u8]) -> String {
        sha2::Sha256::digest(bytes)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect()
    }

    /// How the test server answers each request, in order.
    #[derive(Clone, Copy)]
    enum Answer {
        /// Send the headers for the whole remainder but only this many bytes.
        Cut(usize),
        /// Honour a Range header.
        Ranges,
        /// Ignore a Range header and send everything.
        Whole,
    }

    /// A loopback HTTP server; returns its URL and the Range headers it saw.
    fn serve(body: Vec<u8>, answers: Vec<Answer>) -> (String, Arc<Mutex<Vec<String>>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/file.zip", listener.local_addr().unwrap());
        let seen = Arc::new(Mutex::new(Vec::new()));
        let record = seen.clone();
        thread::spawn(move || {
            for answer in answers {
                let Ok((mut stream, _)) = listener.accept() else {
                    return;
                };
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut start = 0;
                let mut range = String::new();
                loop {
                    let mut line = String::new();
                    if reader.read_line(&mut line).unwrap_or(0) == 0 || line == "\r\n" {
                        break;
                    }
                    if let Some(value) = line.to_ascii_lowercase().strip_prefix("range: bytes=") {
                        range = value.trim().to_string();
                        start = range.trim_end_matches('-').parse().unwrap_or(0);
                    }
                }
                record.lock().unwrap().push(range.clone());
                let (status, from, sent) = match answer {
                    Answer::Cut(bytes) => ("200 OK", 0, bytes),
                    Answer::Whole => ("200 OK", 0, body.len()),
                    Answer::Ranges if start > 0 => ("206 Partial Content", start, body.len()),
                    Answer::Ranges => ("200 OK", 0, body.len()),
                };
                let mut head = format!(
                    "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n",
                    body.len() - from
                );
                if from > 0 {
                    head.push_str(&format!(
                        "Content-Range: bytes {from}-{}/{}\r\n",
                        body.len() - 1,
                        body.len()
                    ));
                }
                head.push_str("\r\n");
                let _ = stream.write_all(head.as_bytes());
                let _ = stream.write_all(&body[from..sent.max(from)]);
            }
        });
        (url, seen)
    }

    fn curl() -> Option<String> {
        crate::toolchain::which("curl").map(|p| p.to_string_lossy().into_owned())
    }

    fn request<'a>(url: &'a str, destination: &'a Path, sum: &str, curl: &'a str) -> Request<'a> {
        Request {
            url,
            destination,
            checksum: Checksum::Sha256(sum.to_string()),
            max_bytes: 1 << 20,
            progress: false,
            allow_http: true,
            curl,
        }
    }

    #[test]
    fn an_interrupted_download_continues_with_a_range_request() {
        let Some(curl) = curl() else {
            eprintln!("skipped: needs curl");
            return;
        };
        let body: Vec<u8> = (0..200_000u32).map(|i| (i % 251) as u8).collect();
        let sum = sha256(&body);
        // The connection breaks after 50 000 bytes.
        let (url, seen) = serve(body.clone(), vec![Answer::Cut(50_000), Answer::Ranges]);
        let root = tempfile::tempdir().unwrap();
        let destination = root.path().join("file.zip");
        let value = match fetch(&request(&url, &destination, &sum, &curl)) {
            // curl may continue by itself within the same call.
            Ok(value) => value,
            Err(error) => {
                assert_eq!(error.code, "download_failed");
                assert_eq!(error.details.as_ref().unwrap()["partial_bytes"], 50_000);
                assert!(!error.remedy.is_empty());
                assert!(!destination.exists());
                fetch(&request(&url, &destination, &sum, &curl)).unwrap()
            }
        };
        assert_eq!(fs::read(&destination).unwrap(), body);
        assert!(!partial_path(&destination).exists());
        let ranges = seen.lock().unwrap().clone();
        assert_eq!(ranges[0], "");
        assert!(
            ranges[1..].iter().any(|r| r.starts_with("50000-")),
            "{ranges:?}"
        );
        assert_eq!(value["reused"], false);
        // A verified file is reused without a request.
        let again = fetch(&request(
            "http://127.0.0.1:9/unused",
            &destination,
            &sum,
            &curl,
        ))
        .unwrap();
        assert_eq!(again["reused"], true);
    }

    #[test]
    fn a_partial_file_is_resumed_on_the_next_call_and_a_server_without_ranges_restarts() {
        let Some(curl) = curl() else {
            eprintln!("skipped: needs curl");
            return;
        };
        let body: Vec<u8> = (0..100_000u32).map(|i| (i % 7) as u8).collect();
        let sum = sha256(&body);
        let root = tempfile::tempdir().unwrap();
        let destination = root.path().join("file.zip");
        // Left by an earlier, interrupted call.
        fs::write(partial_path(&destination), &body[..30_000]).unwrap();
        let (url, seen) = serve(body.clone(), vec![Answer::Ranges]);
        let value = fetch(&request(&url, &destination, &sum, &curl)).unwrap();
        assert_eq!(fs::read(&destination).unwrap(), body);
        assert_eq!(value["resumed"], true);
        assert_eq!(value["resumed_from"], 30_000);
        assert_eq!(seen.lock().unwrap().as_slice(), ["30000-"]);
        // A server that ignores ranges: the partial file is dropped and the
        // download starts again.
        fs::remove_file(&destination).unwrap();
        fs::write(partial_path(&destination), &body[..30_000]).unwrap();
        let (url, _) = serve(body.clone(), vec![Answer::Whole, Answer::Whole]);
        fetch(&request(&url, &destination, &sum, &curl)).unwrap();
        assert_eq!(fs::read(&destination).unwrap(), body);
    }

    #[test]
    fn a_download_with_the_wrong_checksum_is_deleted_and_never_used() {
        let Some(curl) = curl() else {
            eprintln!("skipped: needs curl");
            return;
        };
        let body = b"not the archive".to_vec();
        let (url, _) = serve(body, vec![Answer::Ranges]);
        let root = tempfile::tempdir().unwrap();
        let destination = root.path().join("file.zip");
        let error = fetch(&request(&url, &destination, &"0".repeat(64), &curl)).unwrap_err();
        assert_eq!(error.code, "checksum_mismatch");
        assert!(!destination.exists());
        assert!(!partial_path(&destination).exists());
        // A stale destination with the wrong bytes is replaced, not trusted.
        fs::write(&destination, b"old").unwrap();
        let good = b"good".to_vec();
        let (url, _) = serve(good.clone(), vec![Answer::Ranges]);
        fetch(&request(&url, &destination, &sha256(&good), &curl)).unwrap();
        assert_eq!(fs::read(&destination).unwrap(), good);
    }

    #[test]
    fn real_downloads_are_https_only_and_resume() {
        let destination = Path::new("/data/downloads/tools.zip");
        let mut real = request("https://example.com/tools.zip", destination, "", "curl");
        real.allow_http = false;
        let args = curl_arguments(&real, &partial_path(destination));
        let after = |flag: &str| args[args.iter().position(|a| a == flag).unwrap() + 1].clone();
        assert_eq!(after("--proto"), "=https");
        assert_eq!(after("--proto-redir"), "=https");
        assert_eq!(after("--continue-at"), "-");
        assert_eq!(after("--output"), "/data/downloads/tools.zip.part");
        assert_eq!(args.last().unwrap(), "https://example.com/tools.zip");
    }

    #[test]
    fn only_old_partial_files_are_cleaned_up() {
        let root = tempfile::tempdir().unwrap();
        let old = root.path().join("old.zip.part");
        let fresh = root.path().join("fresh.zip.part");
        let kept = root.path().join("verified.zip");
        for path in [&old, &fresh, &kept] {
            fs::write(path, b"x").unwrap();
        }
        let long_ago = SystemTime::now() - Duration::from_secs(30 * 24 * 3600);
        for path in [&old, &kept] {
            File::options()
                .write(true)
                .open(path)
                .unwrap()
                .set_modified(long_ago)
                .unwrap();
        }
        assert_eq!(
            remove_stale_partials(root.path(), PARTIAL_AGE),
            std::slice::from_ref(&old)
        );
        assert!(!old.exists() && fresh.exists() && kept.exists());
    }

    #[test]
    fn checksums_are_compared_without_regard_to_case() {
        let root = tempfile::tempdir().unwrap();
        let file = root.path().join("f");
        fs::write(&file, b"abc").unwrap();
        let sum = sha256(b"abc").to_uppercase();
        assert!(file_checksum(&file, &Checksum::Sha256(sum)).unwrap());
        assert!(!file_checksum(&file, &Checksum::Sha256("00".into())).unwrap());
        if crate::toolchain::which("sha1sum").is_some() {
            let sha1 = "a9993e364706816aba3e25717850c26c9cd0d89d";
            assert!(file_checksum(&file, &Checksum::Sha1(sha1.into())).unwrap());
        }
    }
}
