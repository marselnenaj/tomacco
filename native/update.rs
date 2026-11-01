//! Updates from the project's GitHub releases.
//!
//! `update.check` asks GitHub which release is the latest (or, on the
//! `prerelease` channel, the newest release including pre-releases), `update.apply` is a
//! durable job that downloads, verifies and installs it, and `update.rollback`
//! undoes the last installation. A package is installed only when its detached
//! Ed25519 signature verifies against a key compiled into this binary. The
//! installation itself is the package's own installer (`bin/tomaccoctl
//! _install`, see `install.rs`), so that its journal, backups and rollback
//! hold for an update as for any other installation; rolling back and
//! uninstalling run in this process.
use crate::{
    core::*,
    install,
    ios_emulator::virtual_phone,
    jobs,
    product::*,
    releases, runtime,
    toolchain::{self as tc},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use ed25519_dalek::{Signature, VerifyingKey};
use semver::Version;
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    ffi::CString,
    fs::{self, File, OpenOptions},
    io::{Cursor, Read, Seek, Write},
    os::unix::{ffi::OsStrExt, fs::PermissionsExt, process::CommandExt},
    path::{Component, Path, PathBuf},
    process::{Command, Stdio},
};

const API: &str = "https://api.github.com";
const API_HOST: &str = "api.github.com";
const GITHUB_HOST: &str = "github.com";
/// Where GitHub's download URLs lead. It has redirected release assets to both
/// of the last two: `objects.` before 2025, `release-assets.` since.
const DOWNLOAD_HOSTS: &[&str] = &[
    GITHUB_HOST,
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
];
const USER_AGENT: &str = "updater";
const CHECK_INTERVAL: f64 = 24.0 * 3600.0;
/// A check that failed, say offline, is asked again sooner than a day later.
const RETRY_INTERVAL: f64 = 6.0 * 3600.0;
const RELEASE_LIMIT: u64 = 2 * 1024 * 1024;
/// How many recent releases the pre-release channel looks at.
const RELEASE_PAGE: usize = 30;
const SMALL_ASSET_LIMIT: u64 = 4096;
const PACKAGE_LIMIT: u64 = 512 * 1024 * 1024;
const UNPACKED_LIMIT: u64 = 2048 * 1024 * 1024;
const ENTRY_LIMIT: u64 = 50_000;
const REDIRECT_LIMIT: usize = 5;
const NOTES_LIMIT: usize = 4000;
const PHONE_MESSAGE: &str = "The virtual iOS device is running. Stop it, then install the update.";

// ---------------------------------------------------------------------------
// Where requests go and which key counts
// ---------------------------------------------------------------------------

#[derive(Clone)]
struct Endpoints {
    api: String,
    api_host: String,
    download_hosts: Vec<String>,
    download_prefix: String,
    /// Only ever true for the local fixture of a debug build's tests.
    loopback: bool,
    /// The trusted release keys: the current one and, during a rotation, the next.
    public_keys: Vec<[u8; 32]>,
}

fn decode_key(text: &str) -> Option<[u8; 32]> {
    STANDARD.decode(text.trim()).ok()?.try_into().ok()
}

/// The release keys compiled into this binary, the current one first. An
/// empty constant adds none.
pub(crate) fn release_keys() -> Vec<[u8; 32]> {
    [
        RELEASE_SIGNING_PUBLIC_KEY_BASE64,
        NEXT_RELEASE_SIGNING_PUBLIC_KEY_BASE64,
    ]
    .iter()
    .filter_map(|text| decode_key(text))
    .collect()
}

fn endpoints() -> Endpoints {
    #[cfg(debug_assertions)]
    if let Some(test) = test_endpoints() {
        return test;
    }
    Endpoints {
        api: API.into(),
        api_host: API_HOST.into(),
        download_hosts: DOWNLOAD_HOSTS.iter().map(|h| h.to_string()).collect(),
        download_prefix: format!(
            "https://{GITHUB_HOST}/{REPO_OWNER}/{REPO_NAME}/releases/download/"
        ),
        loopback: false,
        public_keys: release_keys(),
    }
}

/// The end-to-end test's fixture and key. Debug builds only: a release binary
/// has neither these variables nor any way to accept another key or host.
#[cfg(debug_assertions)]
fn test_endpoints() -> Option<Endpoints> {
    let api = std::env::var("TOMACCO_UPDATE_TEST_API").ok()?;
    let host = api.strip_prefix("http://")?.to_string();
    Some(Endpoints {
        download_hosts: vec![host.clone()],
        download_prefix: format!("{api}/download/"),
        api_host: host,
        api,
        loopback: true,
        public_keys: test_keys().unwrap_or_default(),
    })
}

/// The keys of a debug build's tests: one, or several separated by commas.
#[cfg(debug_assertions)]
fn test_keys() -> Option<Vec<[u8; 32]>> {
    let keys = std::env::var("TOMACCO_UPDATE_TEST_PUBLIC_KEY").ok()?;
    Some(keys.split(',').filter_map(decode_key).collect())
}

/// The keys `tomaccoctl verify` accepts: the compiled ones, or in a debug
/// build the tests' own.
pub(crate) fn trusted_keys() -> Vec<[u8; 32]> {
    #[cfg(debug_assertions)]
    if let Some(keys) = test_keys() {
        return keys;
    }
    release_keys()
}

/// This program's version. A debug build reads a marker a test appended to its
/// executable, so that a test can have two real binaries of different versions.
pub fn current_version() -> String {
    #[cfg(debug_assertions)]
    if let Some(version) = test_version() {
        return version;
    }
    VERSION.to_string()
}

#[cfg(debug_assertions)]
fn test_version() -> Option<String> {
    let mut file = File::open("/proc/self/exe").ok()?;
    file.seek(std::io::SeekFrom::End(-64)).ok()?;
    let mut tail = Vec::new();
    file.read_to_end(&mut tail).ok()?;
    let text = String::from_utf8_lossy(&tail);
    let (_, version) = text.rsplit_once("Tomacco-TEST-VERSION=")?;
    Some(version.trim().to_string())
}

/// `https://host/...`, with exactly one of the permitted hosts and nothing that
/// two URL parsers could read differently. Returns the host.
fn permitted_url(url: &str, hosts: &[String], endpoints: &Endpoints) -> Result<String> {
    let refuse = |reason: &str| {
        Error::new(
            "update_wrong_host",
            format!("Refused the download address {url:?}: {reason}"),
        )
    };
    if url.len() > 8192
        || !url.is_ascii()
        || url
            .bytes()
            .any(|byte| byte <= b' ' || byte == b'\\' || byte == 0x7f)
    {
        return Err(refuse("it contains characters a URL must not have"));
    }
    if url.contains("/../") || url.to_ascii_lowercase().contains("%2e") {
        return Err(refuse("it climbs out of its path"));
    }
    let (rest, plain) = if let Some(rest) = url.strip_prefix("https://") {
        (rest, false)
    } else if let Some(rest) = url.strip_prefix("http://").filter(|_| endpoints.loopback) {
        (rest, true)
    } else {
        return Err(refuse("it does not use TLS (https)"));
    };
    let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
    if authority.contains('@') {
        return Err(refuse("it names a user"));
    }
    let host = authority.to_ascii_lowercase();
    if plain && !host.starts_with("127.0.0.1:") {
        return Err(refuse("it does not use TLS (https)"));
    }
    if !hosts.contains(&host) {
        return Err(refuse("this host is not one GitHub downloads come from"));
    }
    Ok(host)
}

// ---------------------------------------------------------------------------
// The transport: curl in production, a fake in tests
// ---------------------------------------------------------------------------

#[derive(Debug)]
enum NetFailure {
    Offline,
    TooLarge,
    Other(String),
}

struct Fetch<'a> {
    url: &'a str,
    accept: &'a str,
    token: Option<&'a str>,
    limit: u64,
    timeout: u64,
}

struct Reply {
    status: u16,
    location: Option<String>,
}

trait Sink: Write {
    fn restart(&mut self) -> std::io::Result<()>;
}
impl Sink for Vec<u8> {
    fn restart(&mut self) -> std::io::Result<()> {
        self.clear();
        Ok(())
    }
}
impl Sink for File {
    fn restart(&mut self) -> std::io::Result<()> {
        self.set_len(0)?;
        self.rewind()
    }
}

trait Transport {
    /// One request, no redirects followed. Writes the body to `sink` as it
    /// arrives and stops with TooLarge at `limit` bytes.
    fn fetch(
        &self,
        request: &Fetch,
        sink: &mut dyn Write,
    ) -> std::result::Result<Reply, NetFailure>;
}

struct Curl {
    program: String,
    loopback: bool,
}

/// The token is not among these: argv can be read by other local users, so it
/// goes to curl on standard input.
fn curl_arguments(request: &Fetch, headers: &Path, loopback: bool) -> Vec<String> {
    let mut arguments = argv(&[
        "-q",
        "--silent",
        "--show-error",
        "--globoff",
        "--tlsv1.2",
        "--proto",
        if loopback { "=http,https" } else { "=https" },
        "--connect-timeout",
        "15",
        "--speed-limit",
        "1024",
        "--speed-time",
        "60",
        "--user-agent",
        USER_AGENT,
        "--config",
        "-",
    ]);
    arguments.extend([
        "--max-time".into(),
        request.timeout.to_string(),
        "--max-filesize".into(),
        request.limit.to_string(),
        "--dump-header".into(),
        headers.to_string_lossy().into_owned(),
        "--url".into(),
        request.url.into(),
    ]);
    arguments
}

fn curl_configuration(request: &Fetch) -> String {
    let mut text = format!(
        "header = \"Accept: {}\"\nheader = \"X-GitHub-Api-Version: 2022-11-28\"\n",
        request.accept
    );
    if let Some(token) = request.token {
        text.push_str(&format!("header = \"Authorization: Bearer {token}\"\n"));
    }
    text
}

fn parse_headers(text: &str) -> Option<Reply> {
    // A proxy's CONNECT answer comes first; the last block is the server's.
    let block = text
        .split("\r\n\r\n")
        .filter(|block| block.starts_with("HTTP/"))
        .last()?;
    let mut lines = block.lines();
    let status = lines.next()?.split_whitespace().nth(1)?.parse().ok()?;
    let location = lines.find_map(|line| {
        let (name, value) = line.split_once(':')?;
        name.eq_ignore_ascii_case("location")
            .then(|| value.trim().to_string())
    });
    Some(Reply { status, location })
}

impl Transport for Curl {
    fn fetch(
        &self,
        request: &Fetch,
        sink: &mut dyn Write,
    ) -> std::result::Result<Reply, NetFailure> {
        let other = |error: &dyn std::fmt::Display| NetFailure::Other(error.to_string());
        let scratch = tempfile::tempdir().map_err(|e| other(&e))?;
        let headers = scratch.path().join("headers");
        let mut command = Command::new(&self.program);
        command
            .args(curl_arguments(request, &headers, self.loopback))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0);
        tc::parent_bound(&mut command);
        let mut child = command.spawn().map_err(|e| other(&e))?;
        let configuration = curl_configuration(request);
        let mut stdin = child.stdin.take().expect("curl's standard input is piped");
        stdin
            .write_all(configuration.as_bytes())
            .map_err(|e| other(&e))?;
        drop(stdin);
        let mut stdout = child.stdout.take().expect("curl's output is piped");
        let mut buffer = vec![0u8; 256 * 1024];
        let mut received = 0u64;
        let copied = loop {
            let size = match stdout.read(&mut buffer) {
                Ok(0) => break Ok(()),
                Ok(size) => size,
                Err(error) => break Err(other(&error)),
            };
            received += size as u64;
            if received > request.limit {
                break Err(NetFailure::TooLarge);
            }
            if let Err(error) = sink.write_all(&buffer[..size]) {
                break Err(other(&error));
            }
        };
        if copied.is_err() {
            let _ = child.kill();
        }
        drop(stdout);
        let mut message = String::new();
        if let Some(mut stderr) = child.stderr.take() {
            let _ = stderr.by_ref().take(4096).read_to_string(&mut message);
        }
        let status = child.wait().map_err(|e| other(&e))?;
        copied?;
        match status.code() {
            Some(0) => (),
            Some(63) => return Err(NetFailure::TooLarge),
            Some(5 | 6 | 7 | 28 | 52 | 55 | 56) => {
                return Err(NetFailure::Offline);
            }
            _ => return Err(NetFailure::Other(message.trim().to_string())),
        }
        let text = fs::read_to_string(&headers).map_err(|e| other(&e))?;
        parse_headers(&text)
            .ok_or_else(|| NetFailure::Other("The server sent no HTTP status".into()))
    }
}

/// Follows redirects by hand, checking each hop against `hosts`. The token goes
/// only to GitHub's API: a redirect target is someone else's storage.
#[allow(clippy::too_many_arguments)]
fn follow(
    transport: &dyn Transport,
    endpoints: &Endpoints,
    hosts: &[String],
    url: &str,
    accept: &str,
    token: Option<&str>,
    limit: u64,
    timeout: u64,
    sink: &mut dyn Sink,
) -> std::result::Result<u16, FollowError> {
    let mut current = url.to_string();
    for _ in 0..=REDIRECT_LIMIT {
        let host = permitted_url(&current, hosts, endpoints).map_err(FollowError::Refused)?;
        sink.restart()
            .map_err(|e| FollowError::Net(NetFailure::Other(e.to_string())))?;
        let sent = Fetch {
            url: &current,
            accept,
            token: token.filter(|_| host == endpoints.api_host),
            limit,
            timeout,
        };
        let mut limited = Limited {
            sink: &mut *sink,
            left: limit,
        };
        let reply = transport
            .fetch(&sent, &mut limited)
            .map_err(FollowError::Net)?;
        if matches!(reply.status, 301 | 302 | 303 | 307 | 308) {
            let location = reply.location.ok_or_else(|| {
                FollowError::Net(NetFailure::Other("A redirect named no address".into()))
            })?;
            current = if location.starts_with('/') {
                let scheme = if current.starts_with("https://") {
                    "https"
                } else {
                    "http"
                };
                format!("{scheme}://{host}{location}")
            } else {
                location
            };
            continue;
        }
        return Ok(reply.status);
    }
    Err(FollowError::Net(NetFailure::Other(
        "Too many redirects".into(),
    )))
}

#[derive(Debug)]
enum FollowError {
    Refused(Error),
    Net(NetFailure),
}

/// Holds any transport to the limit, the fake in tests as well as curl.
struct Limited<'a> {
    sink: &'a mut dyn Sink,
    left: u64,
}
impl Write for Limited<'_> {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if bytes.len() as u64 > self.left {
            return Err(std::io::Error::other("The download exceeds its size limit"));
        }
        self.left -= bytes.len() as u64;
        self.sink.write_all(bytes)?;
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.sink.flush()
    }
}

// ---------------------------------------------------------------------------
// The latest release
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
struct Release {
    version: Version,
    notes_url: String,
    notes: String,
    assets: Vec<Value>,
}

#[derive(Debug)]
enum Lookup {
    NoRelease,
    Found(Release),
    Offline,
    Failed(String),
}

/// The two update channels: `stable` follows GitHub's latest release,
/// `prerelease` also takes releases GitHub marks as pre-releases.
pub const CHANNELS: &[&str] = &["stable", "prerelease"];

fn release_url(endpoints: &Endpoints, channel: &str) -> String {
    if channel == "prerelease" {
        format!(
            "{}/repos/{REPO_OWNER}/{REPO_NAME}/releases?per_page={RELEASE_PAGE}",
            endpoints.api
        )
    } else {
        format!(
            "{}/repos/{REPO_OWNER}/{REPO_NAME}/releases/latest",
            endpoints.api
        )
    }
}

/// What the latest-release answer means. A draft or a pre-release is not a
/// release for this purpose, whichever way GitHub came to return it.
fn parse_release(body: &[u8]) -> std::result::Result<Option<Release>, String> {
    let value: Value = serde_json::from_slice(body)
        .map_err(|_| "GitHub's answer was not valid JSON".to_string())?;
    release_from(&value, false)
}

/// The release the pre-release channel offers from GitHub's list of releases:
/// the highest version among those that are not drafts, pre-releases included.
/// An entry whose tag is not a semantic version is passed over, not an error.
fn select_release(body: &[u8], prerelease: bool) -> std::result::Result<Option<Release>, String> {
    let value: Value = serde_json::from_slice(body)
        .map_err(|_| "GitHub's answer was not valid JSON".to_string())?;
    let list = value
        .as_array()
        .ok_or_else(|| "GitHub's list of releases was not a list".to_string())?;
    Ok(list
        .iter()
        .take(RELEASE_PAGE)
        .filter_map(|entry| release_from(entry, prerelease).ok().flatten())
        .max_by(|a, b| a.version.cmp(&b.version)))
}

/// One release object, if the channel takes it. Drafts never count; a release
/// GitHub marks as a pre-release, or whose version has a pre-release part,
/// counts only for the pre-release channel.
fn release_from(value: &Value, prerelease: bool) -> std::result::Result<Option<Release>, String> {
    if value["draft"] != false || !value["prerelease"].is_boolean() {
        return Ok(None);
    }
    if value["prerelease"] == true && !prerelease {
        return Ok(None);
    }
    let tag = s(value, "tag_name");
    let version = Version::parse(tag.strip_prefix('v').unwrap_or(tag))
        .map_err(|_| format!("The latest release's tag {tag:?} is not a semantic version"))?;
    if !version.pre.is_empty() && !prerelease {
        return Ok(None);
    }
    let notes_url = s(value, "html_url");
    Ok(Some(Release {
        version,
        notes_url: if notes_url.starts_with(&format!("https://{GITHUB_HOST}/")) {
            notes_url.to_string()
        } else {
            String::new()
        },
        notes: s(value, "body").chars().take(NOTES_LIMIT).collect(),
        assets: value["assets"].as_array().cloned().unwrap_or_default(),
    }))
}

fn lookup(
    transport: &dyn Transport,
    endpoints: &Endpoints,
    token: Option<&str>,
    channel: &str,
) -> Lookup {
    let hosts = [endpoints.api_host.clone()];
    let mut body = Vec::new();
    let status = follow(
        transport,
        endpoints,
        &hosts,
        &release_url(endpoints, channel),
        "application/vnd.github+json",
        token,
        RELEASE_LIMIT,
        30,
        &mut body,
    );
    match status {
        Ok(200) => match if channel == "prerelease" {
            select_release(&body, true)
        } else {
            parse_release(&body)
        } {
            Ok(Some(release)) => Lookup::Found(release),
            Ok(None) => Lookup::NoRelease,
            Err(message) => Lookup::Failed(message),
        },
        // Also how GitHub answers for a private repository without access.
        Ok(404) => Lookup::NoRelease,
        Ok(401) => Lookup::Failed("GitHub refused the configured token".into()),
        Ok(403 | 429) => Lookup::Failed("GitHub's request limit is reached for now".into()),
        Ok(status) => Lookup::Failed(format!("GitHub answered with status {status}")),
        Err(FollowError::Net(NetFailure::Offline)) => Lookup::Offline,
        Err(FollowError::Net(NetFailure::TooLarge)) => {
            Lookup::Failed("GitHub's answer was too large".into())
        }
        Err(FollowError::Net(NetFailure::Other(message))) => Lookup::Failed(message),
        Err(FollowError::Refused(error)) => Lookup::Failed(error.message),
    }
}

/// True only when `latest` is newer: an update never downgrades.
fn applies(current: &str, latest: &Version) -> bool {
    Version::parse(current).is_ok_and(|current| *latest > current)
}

/// The package this installation updates from: the portable one when it was
/// installed from a portable package, the regular one otherwise.
fn package_name(version: &Version) -> String {
    let portable = std::env::current_exe()
        .ok()
        .and_then(|executable| executable.parent().map(is_portable))
        .unwrap_or(false);
    package_file(version, portable)
}

/// A portable package (`tools/build_portable.py`) bundles Qt and FFmpeg;
/// install.py puts them in `runtime/` beside the program, with `qt.conf`.
fn is_portable(library: &Path) -> bool {
    library.join("runtime").is_dir() && library.join("qt.conf").is_file()
}

fn package_file(version: &Version, portable: bool) -> String {
    format!(
        "{PACKAGE_PREFIX}-{version}-{}-{}{}.tar.gz",
        if cfg!(target_os = "linux") {
            "Linux"
        } else {
            std::env::consts::OS
        },
        std::env::consts::ARCH,
        if portable { "-portable" } else { "" }
    )
}

// ---------------------------------------------------------------------------
// Authenticity
// ---------------------------------------------------------------------------

/// The signature file holds base64 of the 64 signature bytes. Returns the
/// position in `keys` of the key it verifies against.
pub(crate) fn verify_signature(
    keys: &[[u8; 32]],
    package: &[u8],
    signature: &[u8],
) -> Result<usize> {
    signing_key(keys, package, signature).map_err(|why| {
        Error::new(
            "update_signature_invalid",
            format!("Refused the update: {why}. Nothing was installed."),
        )
    })
}

/// Which of `keys` made `signature` over `message`, or why none did.
pub(crate) fn signing_key(
    keys: &[[u8; 32]],
    message: &[u8],
    signature: &[u8],
) -> std::result::Result<usize, &'static str> {
    let text = std::str::from_utf8(signature).map_err(|_| "its signature file is not text")?;
    let bytes: [u8; 64] = STANDARD
        .decode(text.trim())
        .ok()
        .and_then(|bytes| bytes.try_into().ok())
        .ok_or("its signature file is not an Ed25519 signature")?;
    let signature = Signature::from_bytes(&bytes);
    keys.iter()
        .position(|key| {
            VerifyingKey::from_bytes(key)
                .is_ok_and(|key| key.verify_strict(message, &signature).is_ok())
        })
        .ok_or("its signature does not match the package and this program's release keys")
}

fn verify_checksum(package: &[u8], text: &[u8]) -> Result<()> {
    let expected = String::from_utf8_lossy(text);
    let expected = expected.split_whitespace().next().unwrap_or("");
    if !expected.eq_ignore_ascii_case(&digest(package)) {
        return Err(Error::new(
            "update_checksum_mismatch",
            "Refused the update: the package does not match its published checksum. Nothing was installed.",
        ));
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Unpacking
// ---------------------------------------------------------------------------

/// `..` and absolute targets out, as a path relative to the package root.
fn contained(path: &Path) -> Option<PathBuf> {
    let mut clean = PathBuf::new();
    for part in path.components() {
        match part {
            Component::Normal(name) => clean.push(name),
            Component::CurDir => (),
            _ => return None,
        }
    }
    (!clean.as_os_str().is_empty()).then_some(clean)
}

/// Whether a symlink at `entry` (relative) pointing to `target` stays inside.
fn link_stays_inside(entry: &Path, target: &Path) -> bool {
    if target.is_absolute() {
        return false;
    }
    let mut depth = entry.components().count() as i64 - 1;
    for part in target.components() {
        match part {
            Component::Normal(_) => depth += 1,
            Component::CurDir => (),
            Component::ParentDir => {
                depth -= 1;
                if depth < 0 {
                    return false;
                }
            }
            _ => return false,
        }
    }
    true
}

/// Unpacks a `.tar.gz` below `destination`. Refuses the whole package for any
/// absolute path, `..`, link leaving the tree, write through a link, hard link,
/// device or duplicate, and at the size and entry limits.
fn unpack(package: &[u8], destination: &Path) -> Result<()> {
    let refuse = |what: String| {
        Error::new(
            "update_unsafe_package",
            format!("Refused the update: {what}. Nothing was installed."),
        )
    };
    let mut archive = tar::Archive::new(flate2::read::GzDecoder::new(Cursor::new(package)));
    let mut total = 0u64;
    let mut count = 0u64;
    for entry in archive
        .entries()
        .map_err(|e| refuse(format!("the package cannot be read ({e})")))?
    {
        let mut entry = entry.map_err(|e| refuse(format!("the package cannot be read ({e})")))?;
        count += 1;
        if count > ENTRY_LIMIT {
            return Err(refuse("the package holds too many files".into()));
        }
        let name = entry
            .path()
            .map_err(|e| refuse(format!("a path in the package cannot be read ({e})")))?
            .into_owned();
        let relative = contained(&name)
            .ok_or_else(|| refuse(format!("the path {} leaves the package", name.display())))?;
        let target = destination.join(&relative);
        let mut ancestor = destination.to_path_buf();
        for part in relative.parent().into_iter().flat_map(Path::components) {
            ancestor.push(part);
            match fs::symlink_metadata(&ancestor) {
                Ok(meta) if meta.is_dir() => (),
                Ok(_) => {
                    return Err(refuse(format!(
                        "{} is written through a link",
                        name.display()
                    )));
                }
                Err(_) => fs::create_dir(&ancestor)?,
            }
        }
        let kind = entry.header().entry_type();
        if kind.is_dir() {
            match fs::symlink_metadata(&target) {
                Ok(meta) if meta.is_dir() => (),
                Ok(_) => return Err(refuse(format!("{} is a duplicate", name.display()))),
                Err(_) => fs::create_dir(&target)?,
            }
        } else if kind.is_file() {
            let mode = entry.header().mode().unwrap_or(0o644) & 0o755;
            // create_new: neither an earlier entry nor a link is written through.
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&target)
                .map_err(|_| refuse(format!("{} is a duplicate", name.display())))?;
            let copied = std::io::copy(
                &mut entry.by_ref().take(UNPACKED_LIMIT - total + 1),
                &mut file,
            )
            .map_err(|e| refuse(format!("the package cannot be read ({e})")))?;
            total += copied;
            if total > UNPACKED_LIMIT {
                return Err(refuse("the package unpacks to too much data".into()));
            }
            file.set_permissions(fs::Permissions::from_mode(mode))?;
        } else if kind.is_symlink() {
            let link = entry
                .link_name()
                .map_err(|e| refuse(format!("a link in the package cannot be read ({e})")))?
                .ok_or_else(|| refuse(format!("{} is an empty link", name.display())))?
                .into_owned();
            if !link_stays_inside(&relative, &link) {
                return Err(refuse(format!(
                    "the link {} leaves the package",
                    name.display()
                )));
            }
            std::os::unix::fs::symlink(&link, &target)
                .map_err(|_| refuse(format!("{} is a duplicate", name.display())))?;
        } else {
            return Err(refuse(format!(
                "{} is neither a file, a directory nor a link",
                name.display()
            )));
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Where this program is installed
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
struct Installation {
    kind: &'static str,
    home: Option<PathBuf>,
    message: String,
}

fn accessible(directory: &Path) -> bool {
    let Ok(path) = CString::new(directory.as_os_str().as_bytes()) else {
        return false;
    };
    // SAFETY: path is a valid NUL-terminated string for the duration of the call.
    unsafe { libc::access(path.as_ptr(), libc::W_OK | libc::X_OK) == 0 }
}

/// How this executable got here, and so whether an update may touch it.
fn installation(executable: &Path) -> Installation {
    let leave = |kind, message: String| Installation {
        kind,
        home: None,
        message,
    };
    if let Some(checkout) = checkout_above(executable) {
        return leave(
            "source_checkout",
            format!(
                "This {PRODUCT_NAME} runs from a source checkout ({}). Update it with Git and a new build.",
                checkout.display()
            ),
        );
    }
    let relative = Path::new(INSTALLED_LIBRARY).join(CLI_NAME);
    let home = executable
        .to_str()
        .and_then(|text| text.strip_suffix(&format!("/{}", relative.display())))
        .map(PathBuf::from);
    let Some(home) = home else {
        let system = ["/usr/", "/opt/", "/nix/", "/snap/", "/var/lib/flatpak/"]
            .iter()
            .any(|root| executable.starts_with(root));
        return if system {
            leave(
                "system_package",
                format!(
                    "This {PRODUCT_NAME} belongs to a system package ({}). Update it with your package manager.",
                    executable.display()
                ),
            )
        } else {
            leave(
                "unmanaged",
                format!(
                    "This {PRODUCT_NAME} was not installed by its installer ({}), so it is left alone.",
                    executable.display()
                ),
            )
        };
    };
    let launcher = home.join(INSTALLED_LAUNCHERS).join(CLI_NAME);
    let managed = fs::read_to_string(&launcher).is_ok_and(|text| text.contains(INSTALLER_MARKER));
    if !managed || !home.join(INSTALLED_LIBRARY).join("VERSION").is_file() {
        return leave(
            "unmanaged",
            format!(
                "This {PRODUCT_NAME} was not installed by its installer ({}), so it is left alone.",
                executable.display()
            ),
        );
    }
    let library = home.join(INSTALLED_LIBRARY);
    for directory in [
        library.as_path(),
        library.parent().unwrap_or(&library),
        &home.join(INSTALLED_LAUNCHERS),
    ] {
        if !accessible(directory) {
            return leave(
                "unwritable",
                format!(
                    "{} cannot be written, so this {PRODUCT_NAME} cannot be updated here.",
                    directory.display()
                ),
            );
        }
    }
    Installation {
        kind: "user_install",
        home: Some(home),
        message: String::new(),
    }
}

fn this_installation() -> Result<Installation> {
    Ok(installation(&own_executable()?.canonicalize()?))
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
struct Mode {
    /// `off`, `notify` or `auto`, after any forcing.
    effective: String,
    /// `build` or `environment` when that turned updates off.
    forced: Option<&'static str>,
    /// `stable` or `prerelease`.
    channel: String,
}

fn forced_by(build: Option<&str>, environment: Option<&str>) -> Option<&'static str> {
    if build.is_some_and(|value| value == "off") {
        Some("build")
    } else if environment.is_some_and(|value| value == "off") {
        Some("environment")
    } else {
        None
    }
}

fn mode_from(settings: &Value, forced: Option<&'static str>) -> Mode {
    let configured = settings["updates"]["mode"]
        .as_str()
        .filter(|mode| ["off", "notify", "auto"].contains(mode))
        .unwrap_or("notify");
    let channel = settings["updates"]["channel"]
        .as_str()
        .filter(|channel| CHANNELS.contains(channel))
        .unwrap_or("stable");
    Mode {
        effective: if forced.is_some() { "off" } else { configured }.to_string(),
        forced,
        channel: channel.to_string(),
    }
}

fn mode() -> Result<Mode> {
    let forced = forced_by(
        option_env!("TOMACCO_UPDATES_BUILD_OFF"),
        std::env::var(UPDATES_ENVIRONMENT).ok().as_deref(),
    );
    Ok(mode_from(&tc::settings()?, forced))
}

fn forced_message(forced: &str) -> String {
    match forced {
        "build" => format!("Updates are turned off in this build of {PRODUCT_NAME}."),
        _ => format!("Updates are turned off by the environment variable {UPDATES_ENVIRONMENT}."),
    }
}

/// The optional token, from an owner-only file like every other secret. A
/// token is one line of URL-safe characters, so it cannot change curl's options.
fn token() -> Result<Option<String>> {
    let settings = tc::settings()?;
    let Some(file) = settings["updates"]["token_file"]
        .as_str()
        .filter(|file| !file.is_empty())
    else {
        return Ok(None);
    };
    let path = releases::secret(file)?;
    let text = fs::read_to_string(path)?;
    let token = text.trim();
    if token.is_empty()
        || !token
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
    {
        return Err(Error::new(
            "invalid_secret",
            "The GitHub token file must hold one token",
        ));
    }
    Ok(Some(token.to_string()))
}

/// `settings.configure`'s `updates` argument, applied to the settings object.
pub fn configure(config: &mut Value, updates: &Value) -> Result<()> {
    if !config["updates"].is_object() {
        config["updates"] = json!({});
    }
    if let Some(mode) = updates["mode"].as_str() {
        config["updates"]["mode"] = json!(mode);
    }
    if let Some(channel) = updates["channel"].as_str() {
        if !CHANNELS.contains(&channel) {
            return Err(Error::new(
                "invalid_request",
                "updates.channel is stable or prerelease",
            ));
        }
        config["updates"]["channel"] = json!(channel);
    }
    if let Some(file) = updates["token_file"].as_str() {
        if file.is_empty() {
            config["updates"]
                .as_object_mut()
                .unwrap()
                .remove("token_file");
        } else {
            config["updates"]["token_file"] = json!(releases::secret(file)?);
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

fn state_directory() -> Result<PathBuf> {
    let directory = home()?.join("updates");
    private_dir(&directory)?;
    Ok(directory)
}

fn read_state() -> Result<Value> {
    let file = state_directory()?.join("state.json");
    Ok(read_json(&file, Some(json!({}))).unwrap_or_else(|_| json!({})))
}

fn change_state(change: impl FnOnce(&mut Value)) -> Result<Value> {
    let directory = state_directory()?;
    let lock = Lock::open(&directory.join("state.lock"))?;
    lock.acquire(false)?;
    let mut state = read_state()?;
    if !state.is_object() {
        state = json!({});
    }
    change(&mut state);
    atomic_json(&directory.join("state.json"), &state)?;
    Ok(state)
}

// ---------------------------------------------------------------------------
// update.check
// ---------------------------------------------------------------------------

fn due(state: &Value, time: f64) -> bool {
    let attempted = state["checked_at"].as_f64().unwrap_or(0.0);
    let wait = if state["check"]["state"] == "offline" || state["check"]["state"] == "error" {
        RETRY_INTERVAL
    } else {
        CHECK_INTERVAL
    };
    time - attempted >= wait
}

/// What a check found, for the state file and the reply, with the channel it asked.
fn outcome(lookup: Lookup, current: &str, channel: &str) -> Value {
    let mut found = match lookup {
        Lookup::NoRelease => json!({"state":"no_release","update_available":false,
            "message":"No release is available yet."}),
        Lookup::Offline => json!({"state":"offline","update_available":false,
            "message":"GitHub could not be reached. This computer may be offline."}),
        Lookup::Failed(message) => {
            json!({"state":"error","update_available":false,"message":message})
        }
        Lookup::Found(release) => {
            let available = applies(current, &release.version);
            let package = release
                .assets
                .iter()
                .any(|asset| s(asset, "name") == package_name(&release.version));
            let (state, message) = if !available {
                (
                    "up_to_date",
                    format!("{PRODUCT_NAME} {current} is up to date."),
                )
            } else if !package {
                (
                    "no_package",
                    format!(
                        "Release {} has no package for this system.",
                        release.version
                    ),
                )
            } else {
                (
                    "update_available",
                    format!("{PRODUCT_NAME} {} is available.", release.version),
                )
            };
            json!({"state":state,"latest":release.version.to_string(),
                "prerelease":!release.version.pre.is_empty(),
                "update_available":available && package,
                "notes_url":release.notes_url,"notes":release.notes,"message":message})
        }
    };
    found["channel"] = json!(channel);
    found
}

/// The channel a recorded check asked; checks from before channels were stable.
fn checked_channel(state: &Value) -> &str {
    state["check"]["channel"].as_str().unwrap_or("stable")
}

/// The one place a check decides whether to use the network. `None`: no
/// request; the recorded result stands. Off asks only when the caller says the
/// user asked, and forced off never does.
fn check_needed(mode: &Mode, manual: bool, state: &Value, time: f64) -> bool {
    if mode.forced.is_some() {
        return false;
    }
    if manual {
        return true;
    }
    // A changed channel makes the recorded result stale.
    mode.effective != "off" && (due(state, time) || checked_channel(state) != mode.channel)
}

#[allow(clippy::too_many_arguments)]
fn check_with(
    transport: &dyn Transport,
    endpoints: &Endpoints,
    token: Option<&str>,
    mode: &Mode,
    manual: bool,
    state: &Value,
    time: f64,
    current: &str,
) -> Option<Value> {
    check_needed(mode, manual, state, time).then(|| {
        outcome(
            lookup(transport, endpoints, token, &mode.channel),
            current,
            &mode.channel,
        )
    })
}

fn curl(endpoints: &Endpoints) -> Result<Curl> {
    Ok(Curl {
        program: tc::find("curl")?,
        loopback: endpoints.loopback,
    })
}

fn check(a: &Value) -> Result {
    let manual = b(a, "manual");
    let mode = mode()?;
    let endpoints = endpoints();
    let state = read_state()?;
    let current = current_version();
    let found = if check_needed(&mode, manual, &state, now()) {
        // A check that cannot start (no curl, a bad token file) is recorded like
        // one that failed: it must not disturb whoever started it unasked.
        Some(
            match token().and_then(|token| Ok((curl(&endpoints)?, token))) {
                Ok((transport, token)) => check_with(
                    &transport,
                    &endpoints,
                    token.as_deref(),
                    &mode,
                    manual,
                    &state,
                    now(),
                    &current,
                )
                .unwrap_or_else(|| json!({})),
                Err(error) => {
                    json!({"state":"error","update_available":false,"message":error.message,
                        "channel":mode.channel})
                }
            },
        )
    } else {
        None
    };
    if let Some(found) = found {
        change_state(|state| {
            state["check"] = found;
            state["checked_at"] = json!(now());
        })?;
    }
    report()
}

/// The last check as it applies to this version, and what could be done now.
fn report() -> Result {
    let state = read_state()?;
    let current = current_version();
    let mode = mode()?;
    let endpoints = endpoints();
    let install = this_installation()?;
    // A check on the other channel says nothing about this one.
    let mut check = if state["check"].is_object() && checked_channel(&state) == mode.channel {
        state["check"].clone()
    } else {
        json!({"state":"not_checked","update_available":false,"message":""})
    };
    // Recorded before an installation or a rollback: the version may have moved.
    if let Some(latest) = check["latest"]
        .as_str()
        .and_then(|v| Version::parse(v).ok())
    {
        let available = check["state"] == "update_available" || check["state"] == "up_to_date";
        if available {
            let newer = applies(&current, &latest);
            check["update_available"] = json!(newer);
            check["state"] = json!(if newer {
                "update_available"
            } else {
                "up_to_date"
            });
            check["message"] = json!(if newer {
                format!("{PRODUCT_NAME} {latest} is available.")
            } else {
                format!("{PRODUCT_NAME} {current} is up to date.")
            });
        }
    }
    let blocked = if let Some(forced) = mode.forced {
        forced_message(forced)
    } else if endpoints.public_keys.is_empty() {
        missing_key().message
    } else if install.kind != "user_install" {
        install.message.clone()
    } else {
        String::new()
    };
    let mut reply = json!({
        "product":PRODUCT_NAME,
        "current":current,
        "mode":mode.effective,
        "channel":mode.channel,
        "forced":mode.forced,
        "signing_key_configured":!endpoints.public_keys.is_empty(),
        "install":{"kind":install.kind,"message":install.message},
        "can_apply":blocked.is_empty(),
        "blocked_reason":blocked,
        "checked_at":state["checked_at"],
    });
    for (key, value) in check.as_object().into_iter().flatten() {
        reply[key] = value.clone();
    }
    Ok(reply)
}

fn missing_key() -> Error {
    Error::new(
        "update_signing_key_missing",
        format!(
            "This build of {PRODUCT_NAME} has no release signing key, so it cannot verify an update and will not install one."
        ),
    )
}

// ---------------------------------------------------------------------------
// update.status
// ---------------------------------------------------------------------------

fn active_update_job() -> Result<Option<Value>> {
    jobs::active_exclusive("update")
}

fn status() -> Result {
    let state = read_state()?;
    let mut reply = report()?;
    let job = active_update_job()?;
    reply["job"] = job.clone().map_or(
        Value::Null,
        |job| json!({"id":job["id"],"state":job["state"],"phase":job["phase"]}),
    );
    reply["last_failure"] = state["last_failure"].clone();
    reply["last_install"] = state["last_install"].clone();
    reply["rollback_available"] = json!(state["last_install"].is_object());
    reply["interrupted"] = json!(job.is_none() && state["transaction"].is_object());
    reply["busy_reason"] = json!(busy("").ok().flatten().unwrap_or_default());
    Ok(reply)
}

// ---------------------------------------------------------------------------
// What must not be running
// ---------------------------------------------------------------------------

fn busy_reason(jobs: &[Value], phone_open: bool, sessions: usize) -> Option<String> {
    if phone_open {
        return Some(PHONE_MESSAGE.to_string());
    }
    if let Some(job) = jobs.first() {
        return Some(format!(
            "A job is running ({}). Wait for it to end or cancel it, then install the update.",
            s(job, "label")
        ));
    }
    (sessions > 0).then(|| {
        "A device display session is running. Close it, then install the update.".to_string()
    })
}

/// Why nothing may be installed now, if something is running. `except` is the
/// update job asking.
fn busy(except: &str) -> Result<Option<String>> {
    let active = jobs::active(except)?;
    // The virtual phone's engine is a job; this says which one it is. A profile
    // that cannot be read changes nothing: its job would still be listed.
    let phone_open = !active.is_empty()
        && virtual_phone::status(&json!({})).is_ok_and(|status| status["closed"] == false);
    let sessions = runtime::handle("runtime.list", &json!({}))?
        .as_array()
        .map_or(0, |rows| {
            rows.iter()
                .filter(|row| ["starting", "connected"].contains(&s(row, "status")))
                .count()
        });
    Ok(busy_reason(&active, phone_open, sessions))
}

// ---------------------------------------------------------------------------
// update.apply
// ---------------------------------------------------------------------------

fn refuse_unless_managed(install: &Installation) -> Result<PathBuf> {
    match (&install.home, install.kind) {
        (Some(home), "user_install") => Ok(home.clone()),
        _ => Err(Error::new(
            &format!("update_{}", install.kind),
            install.message.clone(),
        )),
    }
}

fn admit(a: &Value, mode: &Mode, state: &Value, current: &str) -> Result<()> {
    if let Some(forced) = mode.forced {
        return Err(Error::new("update_forced_off", forced_message(forced)));
    }
    if b(a, "auto") {
        if mode.effective != "auto" {
            return Err(Error::new(
                "update_auto_off",
                "Updates are not set to install by themselves.",
            ));
        }
        let latest = state["check"]["latest"].as_str();
        let newer = latest
            .and_then(|latest| Version::parse(latest).ok())
            .is_some_and(|latest| applies(current, &latest));
        if !newer
            || state["check"]["update_available"] != true
            || checked_channel(state) != mode.channel
        {
            return Err(Error::new(
                "update_not_available",
                "No newer version is known yet.",
            ));
        }
        if latest == state["hold_version"].as_str() {
            return Err(Error::new(
                "update_held",
                "This version failed to install or was rolled back, so it is not installed by itself again.",
            ));
        }
    } else if mode.effective == "off" && !b(a, "manual") {
        return Err(Error::new(
            "update_off",
            "Updates are turned off. Install one from the Settings, or pass manual when the user asked for it.",
        ));
    }
    Ok(())
}

fn apply(a: &Value) -> Result {
    let mode = mode()?;
    let state = read_state()?;
    admit(a, &mode, &state, &current_version())?;
    let endpoints = endpoints();
    if endpoints.public_keys.is_empty() {
        return Err(missing_key());
    }
    let install = this_installation()?;
    let user = refuse_unless_managed(&install)?;
    tc::find("curl")?;
    if let Some(job) = active_update_job()? {
        let mut job = job;
        job["reused"] = json!(true);
        return Ok(job);
    }
    if let Some(reason) = busy("")? {
        return Err(Error::new("update_busy", reason));
    }
    repair_interrupted(&user)?;
    // A temporary directory beside the installation, so on its filesystem.
    let parent = user.join(INSTALLED_LIBRARY);
    let parent = parent.parent().unwrap_or(&user);
    let scratch = tempfile::Builder::new()
        .prefix(&format!(".{PACKAGE_PREFIX}-update-"))
        .tempdir_in(parent)
        .map_err(|error| {
            Error::new(
                "update_unwritable",
                format!("{} cannot be written ({error}).", parent.display()),
            )
        })?;
    fs::set_permissions(scratch.path(), fs::Permissions::from_mode(0o700))?;
    let request = scratch.path().join("request.json");
    atomic_json(
        &request,
        &json!({"home":user,"scratch":scratch.path()}),
    )?;
    let job = jobs::submit(
        &format!("Install {PRODUCT_NAME} update"),
        json!({
            "steps":[{"argv":[own_executable()?,"_update",request]}],
            "cwd":scratch.path(),
            "timeout":1800,
            "exclusive":"update",
            "single_active":true,
            "operation_key":{"operation":"update.apply"},
            "temporary_directories":[jobs::temporary_directory(scratch.path())?],
        }),
    )?;
    let _ = scratch.keep();
    Ok(job)
}

// The steps a job runs, with what the network and the installer do abstracted.

struct Verified {
    version: Version,
    package: Vec<u8>,
}

fn download_small(
    transport: &dyn Transport,
    endpoints: &Endpoints,
    token: Option<&str>,
    asset: &Value,
) -> Result<Vec<u8>> {
    let mut body = Vec::new();
    fetch_asset(
        transport,
        endpoints,
        token,
        asset,
        SMALL_ASSET_LIMIT,
        &mut body,
    )?;
    Ok(body)
}

fn fetch_asset(
    transport: &dyn Transport,
    endpoints: &Endpoints,
    token: Option<&str>,
    asset: &Value,
    limit: u64,
    sink: &mut dyn Sink,
) -> Result<()> {
    let mut hosts = endpoints.download_hosts.clone();
    // A private repository's assets come through the API, with the token.
    let via_api = token.is_some() && !s(asset, "url").is_empty();
    let url = if via_api {
        hosts.push(endpoints.api_host.clone());
        let url = s(asset, "url");
        if !url.starts_with(&format!(
            "{}/repos/{REPO_OWNER}/{REPO_NAME}/releases/assets/",
            endpoints.api
        )) {
            return Err(Error::new(
                "update_wrong_host",
                format!("Refused the download address {url:?}: it is not this project's."),
            ));
        }
        url
    } else {
        let url = s(asset, "browser_download_url");
        permitted_url(url, &hosts, endpoints)?;
        if !url.starts_with(&endpoints.download_prefix) {
            return Err(Error::new(
                "update_wrong_host",
                format!("Refused the download address {url:?}: it is not this project's."),
            ));
        }
        url
    };
    let accept = "application/octet-stream";
    match follow(
        transport, endpoints, &hosts, url, accept, token, limit, 1800, sink,
    ) {
        Ok(200) => Ok(()),
        Ok(status) => Err(Error::new(
            "update_download_failed",
            format!(
                "Downloading {} failed: GitHub answered with status {status}.",
                s(asset, "name")
            ),
        )),
        Err(FollowError::Refused(error)) => Err(error),
        Err(FollowError::Net(NetFailure::TooLarge)) => Err(Error::new(
            "update_too_large",
            format!("Refused {}: it exceeds the size limit.", s(asset, "name")),
        )),
        Err(FollowError::Net(NetFailure::Offline)) => Err(Error::new(
            "update_offline",
            "GitHub could not be reached during the download. This computer may be offline.",
        )),
        Err(FollowError::Net(NetFailure::Other(message))) => Err(Error::new(
            "update_download_failed",
            format!("Downloading {} failed: {message}", s(asset, "name")),
        )),
    }
}

/// Everything up to a verified package in memory: extraction then reads the
/// very bytes the signature covered.
fn download_and_verify(
    transport: &dyn Transport,
    endpoints: &Endpoints,
    token: Option<&str>,
    current: &str,
    scratch: &Path,
    attempted: &mut Option<Version>,
    channel: &str,
) -> Result<Verified> {
    if endpoints.public_keys.is_empty() {
        return Err(missing_key());
    }
    let release = match lookup(transport, endpoints, token, channel) {
        Lookup::Found(release) => release,
        Lookup::NoRelease => {
            return Err(Error::new(
                "update_no_release",
                "No release is available yet.",
            ));
        }
        Lookup::Offline => {
            return Err(Error::new(
                "update_offline",
                "GitHub could not be reached. This computer may be offline.",
            ));
        }
        Lookup::Failed(message) => return Err(Error::new("update_check_failed", message)),
    };
    if !applies(current, &release.version) {
        return Err(Error::new(
            "update_not_newer",
            format!(
                "Release {} is not newer than {current}; an update never downgrades.",
                release.version
            ),
        ));
    }
    *attempted = Some(release.version.clone());
    let name = package_name(&release.version);
    let asset = |wanted: &str| {
        release
            .assets
            .iter()
            .find(|asset| s(asset, "name") == wanted)
    };
    let package_asset = asset(&name).ok_or_else(|| {
        Error::new(
            "update_no_package",
            format!("Release {} has no package {name}.", release.version),
        )
    })?;
    let signature_asset = asset(&format!("{name}.sig")).ok_or_else(|| {
        Error::new(
            "update_unsigned",
            format!(
                "Refused the update: release {} has no signature ({name}.sig). Nothing was installed.",
                release.version
            ),
        )
    })?;
    let signature = download_small(transport, endpoints, token, signature_asset)?;
    let size = package_asset["size"].as_u64().unwrap_or(0);
    if size > PACKAGE_LIMIT {
        return Err(Error::new(
            "update_too_large",
            format!("Refused {name}: it exceeds the size limit."),
        ));
    }
    let path = scratch.join(&name);
    eprintln!("Downloading {name}");
    {
        let mut file = File::create(&path)?;
        fetch_asset(
            transport,
            endpoints,
            token,
            package_asset,
            PACKAGE_LIMIT,
            &mut file,
        )?;
        file.sync_all()?;
    }
    let package = fs::read(&path)?;
    eprintln!("Verifying the signature");
    verify_signature(&endpoints.public_keys, &package, &signature)?;
    if let Some(checksum) = asset(&format!("{name}.sha256")) {
        verify_checksum(
            &package,
            &download_small(transport, endpoints, token, checksum)?,
        )?;
    }
    Ok(Verified {
        version: release.version,
        package,
    })
}

/// Unpacks a verified package and checks that it is what the release said: the
/// version inside is signed, the tag is not, so an older signed package under a
/// newer tag ends here.
fn unpack_package(verified: &Verified, scratch: &Path) -> Result<PathBuf> {
    let unpacked = scratch.join("unpacked");
    fs::create_dir(&unpacked)?;
    unpack(&verified.package, &unpacked)?;
    let name = package_name(&verified.version);
    let root = unpacked.join(name.strip_suffix(".tar.gz").unwrap_or(&name));
    let refuse = |what: &str| {
        Error::new(
            "update_invalid_package",
            format!("Refused the update: {what}. Nothing was installed."),
        )
    };
    let version = fs::read_to_string(root.join("share").join(PACKAGE_PREFIX).join("VERSION"))
        .map_err(|_| refuse("the package names no version"))?;
    if version.trim() != verified.version.to_string() {
        return Err(refuse(
            "the version inside the package is not the release's",
        ));
    }
    for file in [
        format!("bin/{DESKTOP_NAME}"),
        format!("bin/{CLI_NAME}"),
    ] {
        if !fs::symlink_metadata(root.join(&file)).is_ok_and(|meta| meta.is_file()) {
            return Err(refuse(&format!("the package has no {file}")));
        }
    }
    Ok(root)
}

trait Installer {
    /// Runs the installation. An error may leave it partly done.
    fn install(&self) -> Result<()>;
    /// The journal entry this installation made, if it got as far as one.
    fn journal(&self) -> Result<Option<String>>;
    fn rollback(&self, journal: &str) -> Result<()>;
    fn healthy(&self, expected: &str) -> Result<()>;
}

/// Installs, checks the result and undoes the installation if either fails.
fn install_checked(installer: &dyn Installer, expected: &str) -> Result<String> {
    let failure = match installer
        .install()
        .and_then(|()| installer.healthy(expected))
    {
        Ok(()) => {
            return installer.journal()?.ok_or_else(|| {
                Error::new(
                    "update_install_failed",
                    "The installer reported no journal for this installation.",
                )
            });
        }
        Err(error) => error,
    };
    eprintln!("Installation failed: {failure}. Rolling back.");
    let undone = match installer.journal()? {
        Some(journal) => installer
            .rollback(&journal)
            .map_err(|error| (journal, error)),
        None => Ok(()),
    };
    match undone {
        Ok(()) => Err(Error::new(
            "update_install_failed",
            format!(
                "The update was not installed and the previous version was restored: {failure}"
            ),
        )
        .details(json!({"cause":failure.value()}))),
        Err((journal, error)) => Err(Error::new(
            "update_rollback_failed",
            format!(
                "The update failed ({failure}) and restoring the previous version failed too ({error}). Its files are in the backup {journal}."
            ),
        )
        .remedy("Run the installer with --rollback and this backup ID, or install again")),
    }
}

/// The package's own installer for installing; this process for rolling back.
struct JournalledInstaller {
    /// The package's `bin/tomaccoctl`; None where only a rollback is wanted.
    program: Option<PathBuf>,
    home: PathBuf,
    known: BTreeSet<String>,
}

fn backups(user: &Path) -> Result<BTreeSet<String>> {
    let mut names = BTreeSet::new();
    match fs::read_dir(user.join(INSTALL_BACKUPS)) {
        Ok(entries) => {
            for entry in entries {
                names.insert(entry?.file_name().to_string_lossy().into_owned());
            }
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => (),
        Err(error) => return Err(error.into()),
    }
    Ok(names)
}

impl Installer for JournalledInstaller {
    fn install(&self) -> Result<()> {
        let program = self
            .program
            .as_ref()
            .ok_or_else(|| Error::new("update_installer_failed", "No package to install"))?;
        let command = vec![
            program.to_string_lossy().into_owned(),
            "_install".into(),
            "--user-home".into(),
            self.home.to_string_lossy().into_owned(),
            "--update".into(),
        ];
        let done = tc::run(&command, 900, program.parent(), false)?;
        eprint!("{}", s(&done, "stdout"));
        eprint!("{}", s(&done, "stderr"));
        if done["exit_code"] != 0 {
            let message = s(&done, "stderr")
                .trim()
                .lines()
                .next_back()
                .unwrap_or("The installer failed")
                .to_string();
            return Err(Error::new("update_installer_failed", message));
        }
        Ok(())
    }
    /// Every run journals into one new backup. Of several new ones the oldest is
    /// the installation: a later one is an interrupted rollback of it, and
    /// rolling back the installation again finishes that rollback.
    fn journal(&self) -> Result<Option<String>> {
        Ok(backups(&self.home)?
            .into_iter()
            .find(|name| !self.known.contains(name)))
    }
    fn rollback(&self, journal: &str) -> Result<()> {
        let done = install::rollback(&self.home, &current_version(), journal)
            .map_err(|error| Error::new("update_installer_failed", error.message))?;
        eprintln!("{done}");
        Ok(())
    }
    fn healthy(&self, expected: &str) -> Result<()> {
        let launcher = self.home.join(INSTALLED_LAUNCHERS).join(CLI_NAME);
        reports_version(&launcher, expected)
    }
}

fn reports_version(program: &Path, expected: &str) -> Result<()> {
    let unhealthy = |what: String| {
        Error::new(
            "update_health_check_failed",
            format!("{} {what}", program.display()),
        )
    };
    let done = tc::run(
        &[program.to_string_lossy().into_owned(), "--version".into()],
        30,
        None,
        false,
    )
    .map_err(|error| unhealthy(format!("did not run: {error}")))?;
    let printed = s(&done, "stdout").split_whitespace().nth(1).unwrap_or("");
    if done["exit_code"] != 0 || printed != expected {
        return Err(unhealthy(format!(
            "reports version {printed:?} instead of {expected}"
        )));
    }
    Ok(())
}

/// A copy of the package's install.py, in the data directory. This version
/// rolls back in its own process; the copy is for an older tomaccoctl that
/// finds this state after a downgrade, which runs the `script` it names.
fn keep_installer(root: &Path, version: &str) -> Result<Option<PathBuf>> {
    let kept = state_directory()?.join("rollback").join(version);
    if kept.exists() {
        fs::remove_dir_all(&kept)?;
    }
    if !root.join("install.py").is_file() {
        return Ok(None);
    }
    let share = kept.join("share").join(PACKAGE_PREFIX);
    fs::create_dir_all(&share)?;
    fs::set_permissions(&kept, fs::Permissions::from_mode(0o700))?;
    fs::copy(root.join("install.py"), kept.join("install.py"))?;
    fs::write(share.join("VERSION"), format!("{version}\n"))?;
    Ok(Some(kept.join("install.py")))
}

fn prune_installers(keep: &str) -> Result<()> {
    let directory = state_directory()?.join("rollback");
    let entries = match fs::read_dir(directory) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error.into()),
    };
    for entry in entries {
        let entry = entry?;
        if entry.file_name().to_string_lossy() != keep {
            fs::remove_dir_all(entry.path())?;
        }
    }
    Ok(())
}

/// A worker that died inside the installer leaves a transaction; it is undone
/// before anything else is installed or rolled back.
fn repair_interrupted(user: &Path) -> Result<()> {
    let state = read_state()?;
    let transaction = &state["transaction"];
    if !transaction.is_object() {
        return Ok(());
    }
    let installer = JournalledInstaller {
        program: None,
        home: user.to_path_buf(),
        known: strings(&transaction["known"]).into_iter().collect(),
    };
    eprintln!("Restoring the installation an interrupted update left");
    if let Some(journal) = installer.journal()? {
        installer.rollback(&journal)?;
    }
    change_state(|state| {
        state.as_object_mut().unwrap().remove("transaction");
    })?;
    Ok(())
}

fn record_failure(version: Option<&Version>, error: &Error) -> Result<()> {
    let refusal = error.code.clone();
    let message = error.message.clone();
    let held = version.map(ToString::to_string);
    change_state(|state| {
        state["last_failure"] = json!({"code":refusal,"message":message,"version":held,"at":now()});
        if let Some(version) = held {
            state["hold_version"] = json!(version);
        }
    })?;
    Ok(())
}

pub fn worker(request: &str) -> Result<()> {
    let request = read_json(Path::new(request), None)?;
    let scratch = PathBuf::from(s(&request, "scratch"));
    let user = PathBuf::from(s(&request, "home"));
    let current = current_version();
    let parent = std::env::var("TOMACCO_PARENT_JOB").unwrap_or_default();
    let mut attempted = None;
    let result = (|| -> Result {
        let endpoints = endpoints();
        let token = token()?;
        let transport = curl(&endpoints)?;
        let channel = mode()?.channel;
        let verified = download_and_verify(
            &transport,
            &endpoints,
            token.as_deref(),
            &current,
            &scratch,
            &mut attempted,
            &channel,
        )?;
        let version = verified.version.to_string();
        let root = unpack_package(&verified, &scratch)?;
        drop(verified);
        reports_version(&root.join("bin").join(CLI_NAME), &version)?;
        // Time has passed since the job was admitted.
        if let Some(reason) = busy(&parent)? {
            return Err(Error::new("update_busy", reason));
        }
        let script = keep_installer(&root, &version)?;
        let known = backups(&user)?;
        change_state(|state| {
            state["transaction"] =
                json!({"from":current,"to":version,"script":script,"known":known});
        })?;
        let installer = JournalledInstaller {
            program: Some(root.join("bin").join(CLI_NAME)),
            home: user.clone(),
            known,
        };
        eprintln!("Installing {version}");
        let installed = install_checked(&installer, &version);
        change_state(|state| {
            state.as_object_mut().unwrap().remove("transaction");
        })?;
        let journal = installed?;
        prune_installers(&version)?;
        change_state(|state| {
            state["last_install"] = json!({"from":current,"to":version,"journal":journal,
                "script":script,"at":now()});
            let object = state.as_object_mut().unwrap();
            object.remove("last_failure");
            object.remove("hold_version");
        })?;
        eprintln!("Installed {version}. Restart to use it.");
        Ok(json!({"installed":version,"from":current,"restart_required":true}))
    })();
    match result {
        Ok(_) => Ok(()),
        Err(error) => {
            // A busy machine is a reason to try later, not to give up on this version.
            let hold = (error.code != "update_busy" && error.code != "update_offline")
                .then_some(attempted.as_ref())
                .flatten();
            record_failure(hold, &error)?;
            Err(error)
        }
    }
}

// ---------------------------------------------------------------------------
// update.rollback
// ---------------------------------------------------------------------------

fn rollback() -> Result {
    let install = this_installation()?;
    let user = refuse_unless_managed(&install)?;
    if active_update_job()?.is_some() {
        return Err(Error::new(
            "update_busy",
            "An update is being installed right now.",
        ));
    }
    if let Some(reason) = busy("")? {
        return Err(Error::new("update_busy", reason));
    }
    repair_interrupted(&user)?;
    let state = read_state()?;
    let last = &state["last_install"];
    if !last.is_object() {
        return Err(Error::new(
            "update_nothing_to_roll_back",
            "There is no installed update to roll back.",
        ));
    }
    let installer = JournalledInstaller {
        program: None,
        home: user,
        known: BTreeSet::new(),
    };
    installer.rollback(s(last, "journal"))?;
    installer.healthy(s(last, "from"))?;
    let (from, to) = (s(last, "from").to_string(), s(last, "to").to_string());
    let held = to.clone();
    change_state(|state| {
        state.as_object_mut().unwrap().remove("last_install");
        // Installing it by itself again would undo what was just asked for.
        state["hold_version"] = json!(held);
    })?;
    Ok(json!({"rolled_back":to,"current":from,"restart_required":true}))
}

// ---------------------------------------------------------------------------
// tomaccoctl update and tomaccoctl uninstall
// ---------------------------------------------------------------------------

/// `tomaccoctl update [--check]`: `update.check` as the user's own request,
/// then, unless `--check`, `update.apply` and its job to the end. Prints one
/// JSON envelope: the check, or the finished job.
pub fn update_command(args: &[String]) -> Result<i32> {
    let check_only = match args {
        [] => false,
        [flag] if flag == "--check" => true,
        _ => {
            return Err(
                Error::new("invalid_request", "Usage: tomaccoctl update [--check]")
                    .remedy("Run tomaccoctl --help for the commands"),
            );
        }
    };
    let manual = json!({"manual":true});
    let found = crate::api::dispatch("update.check", &manual)?;
    if check_only || found["update_available"] != true {
        let _ = writeln!(std::io::stdout(), "{}", crate::api::envelope(Ok(found)));
        return Ok(0);
    }
    eprintln!("Installing {PRODUCT_NAME} {}", s(&found, "latest"));
    let mut job = crate::api::dispatch("update.apply", &manual)?;
    let id = s(&job, "id").to_string();
    while matches!(s(&job, "state"), "queued" | "running") {
        job = crate::api::dispatch("job.wait", &json!({"job_id":id,"seconds":60}))?;
    }
    let succeeded = s(&job, "state") == "succeeded";
    let _ = writeln!(std::io::stdout(), "{}", crate::api::envelope(Ok(job)));
    Ok(if succeeded { 0 } else { 1 })
}

/// `tomaccoctl uninstall`: the installer's `--uninstall`, in this process. It
/// keeps user data and files the user changed, and journals what it removes,
/// so a rollback brings the installation back.
pub fn uninstall_command(args: &[String]) -> Result<i32> {
    if !args.is_empty() {
        return Err(Error::new("invalid_request", "Usage: tomaccoctl uninstall")
            .remedy("Run tomaccoctl --help for the commands"));
    }
    let install = this_installation()?;
    let (Some(home), "user_install") = (install.home.clone(), install.kind) else {
        return Err(Error::new(
            &format!("uninstall_{}", install.kind),
            format!(
                "{} The installer did not install this copy, so it does not remove it.",
                install.message
            ),
        ));
    };
    let result = match install::uninstall(&home, &current_version()) {
        Ok(result) => result,
        Err(error) => {
            eprintln!("{}", error.message);
            return Ok(1);
        }
    };
    let _ = writeln!(std::io::stdout(), "{}", install::ascii_json(&result));
    // The uninstallation's journal holds a copy of the program it removed,
    // which rolls it back.
    if let Some(backups) = result["backups"].as_str().map(PathBuf::from) {
        let id = backups.file_name().unwrap_or_default().to_string_lossy();
        eprintln!(
            "To undo: {} _install --rollback {id}",
            shell_quote(
                &backups
                    .join(INSTALLED_LIBRARY)
                    .join(CLI_NAME)
                    .to_string_lossy()
            )
        );
    }
    Ok(0)
}

pub fn handle(op: &str, a: &Value) -> Result {
    match op {
        "update.check" => check(a),
        "update.apply" => apply(a),
        "update.status" => status(),
        "update.rollback" => rollback(),
        _ => Err(Error::new("operation_not_found", op)),
    }
}

#[cfg(test)]
#[path = "update_tests.rs"]
mod tests;
