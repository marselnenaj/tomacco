//! Serialized input and capture on the monitor owned by a live iOS job.
use super::*;
use std::os::unix::fs::{FileExt, FileTypeExt, OpenOptionsExt};

fn coordinates(a: &Value, x: &str, y: &str) -> Result<(u32, u32)> {
    let value = |name| {
        a[name]
            .as_f64()
            .filter(|v| v.is_finite() && (0.0..=1.0).contains(v))
            .map(|v| (v * 32767.0).round() as u32)
            .ok_or_else(|| {
                Error::new(
                    "invalid_coordinates",
                    format!("{name} must be between 0 and 1"),
                )
            })
    };
    Ok((value(x)?, value(y)?))
}

const HID_PATH: &str = "/machine/peripheral/tomacco-hid";

/// How long an action waits for its turn. A live viewer takes the control
/// lane for a few milliseconds several times a second and a gesture holds it
/// for its whole duration; an input or a stop that arrives meanwhile is
/// serialized behind it instead of being refused.
const LANE_WAIT: Duration = Duration::from_millis(2500);

/// The largest uncompressed frame the file transport takes.
const FRAME_FILE_LIMIT: u64 = 64 * 1024 * 1024;

/// Bytes of a binary PPM picture: "P6\n<width> <height>\n255\n" and RGB.
pub(super) fn ppm_bytes(width: u64, height: u64) -> u64 {
    (format!("P6\n{width} {height}\n255\n").len() as u64)
        .saturating_add(width.saturating_mul(height).saturating_mul(3))
}

/// The size of a frame file that is a complete, plausible PPM picture.
pub(super) fn ppm_frame(path: &Path) -> Option<(u64, u64)> {
    let metadata = fs::symlink_metadata(path).ok()?;
    if !metadata.is_file() || metadata.len() > FRAME_FILE_LIMIT {
        return None;
    }
    // Binary PPM: "P6\n<width> <height>\n255\n" followed by RGB bytes.
    let mut header = [0u8; 32];
    let read = File::open(path).ok()?.read(&mut header).ok()?;
    let text = String::from_utf8_lossy(&header[..read]).into_owned();
    let mut lines = text.splitn(4, '\n');
    let size = (lines.next() == Some("P6"))
        .then(|| lines.next())
        .flatten()
        .and_then(|line| line.split_once(' '))
        .and_then(|(w, h)| Some((w.parse::<u64>().ok()?, h.parse::<u64>().ok()?)));
    let depth_ok = lines.next() == Some("255");
    let (width, height) =
        size.filter(|(w, h)| depth_ok && (1..=16384).contains(w) && (1..=16384).contains(h))?;
    (metadata.len() == ppm_bytes(width, height)).then_some((width, height))
}

/// Compare complete captures without allocating two full-resolution pictures.
/// A missing, short or unreadable file is never evidence of an unchanged frame.
fn same_frame_files(left: &Path, right: &Path, bytes: u64) -> std::io::Result<bool> {
    if bytes == 0 || bytes > FRAME_FILE_LIMIT {
        return Ok(false);
    }
    let open = |path: &Path| {
        fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(path)
    };
    let mut left = open(left)?;
    let mut right = open(right)?;
    let left_before = left.metadata()?;
    let right_before = right.metadata()?;
    if !left_before.is_file()
        || !right_before.is_file()
        || left_before.len() != bytes
        || right_before.len() != bytes
    {
        return Ok(false);
    }
    let mut left_chunk = [0u8; 64 * 1024];
    let mut right_chunk = [0u8; 64 * 1024];
    let mut remaining = bytes;
    while remaining != 0 {
        let count = remaining.min(left_chunk.len() as u64) as usize;
        left.read_exact(&mut left_chunk[..count])?;
        right.read_exact(&mut right_chunk[..count])?;
        if left_chunk[..count] != right_chunk[..count] {
            return Ok(false);
        }
        remaining -= count as u64;
    }
    // The QMP command has completed and the control lane owns these captures.
    // Still reject an observed file change while the comparison was in progress.
    let unchanged = |before: &fs::Metadata, after: fs::Metadata| {
        before.len() == after.len()
            && before.mtime() == after.mtime()
            && before.mtime_nsec() == after.mtime_nsec()
            && before.ctime() == after.ctime()
            && before.ctime_nsec() == after.ctime_nsec()
    };
    Ok(unchanged(&left_before, left.metadata()?) && unchanged(&right_before, right.metadata()?))
}

/// The private directory that holds a guest's live frame files.
///
/// The runtime directory is memory-backed and private to the user, so nothing
/// reaches a disk. Frames of guests that have ended are removed on the way.
fn frame_directory(runtime: &Path, id: &str) -> Result<PathBuf> {
    let unavailable = || {
        Error::new(
            "ios_frame_file_unavailable",
            "No private runtime directory is available for live frames",
        )
        .remedy("Use the default inline frame transport")
    };
    if !private_directory(runtime) {
        return Err(unavailable());
    }
    let root = runtime.join("tomacco").join("ios-frames");
    let directory = root.join(id);
    for path in [root.parent().unwrap(), root.as_path(), directory.as_path()] {
        if fs::symlink_metadata(path).is_err() {
            fs::create_dir(path)?;
            fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
        }
        if !private_directory(path) {
            return Err(unavailable());
        }
    }
    // Frames of guests that have ended would otherwise stay until logout. The
    // runtime directory is shared by every data directory of this user, so a
    // job this one does not know may be another one's running guest: such
    // frames are only removed once nothing has touched them for ten minutes.
    for entry in fs::read_dir(&root)?.flatten() {
        let name = entry.file_name();
        let Some(name) = name.to_str() else { continue };
        if name == id
            || identifier(name, "job ID", Some("[0-9a-f]{32}")).is_err()
            || !entry.file_type().is_ok_and(|kind| kind.is_dir())
        {
            continue;
        }
        let ended = match jobs::get(name) {
            Ok(job) => jobs::TERMINAL.contains(&s(&job, "state")),
            Err(_) => entry
                .metadata()
                .and_then(|meta| meta.modified())
                .ok()
                .and_then(|modified| modified.elapsed().ok())
                .is_some_and(|age| age > Duration::from_secs(600)),
        };
        if ended {
            let _ = fs::remove_dir_all(entry.path());
        }
    }
    Ok(directory)
}

/// `$XDG_RUNTIME_DIR` when it is an absolute directory private to this user.
fn private_runtime() -> Option<PathBuf> {
    std::env::var_os("XDG_RUNTIME_DIR")
        .map(PathBuf::from)
        .filter(|path| path.is_absolute() && private_directory(path))
}

/// A real directory (not a link) of this user that nobody else may enter.
fn private_directory(path: &Path) -> bool {
    let owner = unsafe { libc::geteuid() };
    fs::symlink_metadata(path)
        .is_ok_and(|meta| meta.is_dir() && meta.uid() == owner && meta.mode() & 0o077 == 0)
}

/// One complete frame file a viewer was handed: its sequence number and,
/// when it came from the engine's frame mailbox, that frame's number there.
#[derive(Clone, Debug, PartialEq, Eq)]
struct FrameName {
    sequence: u64,
    mailbox: Option<u64>,
}

impl FrameName {
    /// `frame-<sequence>.ppm` from a capture, `frame-<sequence>.m<frame>.ppm`
    /// from the mailbox.
    fn parse(name: &str) -> Option<Self> {
        let stem = name.strip_prefix("frame-")?.strip_suffix(".ppm")?;
        let number = |text: &str| {
            (!text.is_empty() && text.bytes().all(|b| b.is_ascii_digit()))
                .then(|| text.parse::<u64>().ok())
                .flatten()
        };
        Some(match stem.split_once(".m") {
            Some((sequence, frame)) => FrameName {
                sequence: number(sequence)?,
                mailbox: Some(number(frame)?),
            },
            None => FrameName {
                sequence: number(stem)?,
                mailbox: None,
            },
        })
    }

    fn file_name(&self) -> String {
        match self.mailbox {
            Some(frame) => format!("frame-{}.m{frame}.ppm", self.sequence),
            None => format!("frame-{}.ppm", self.sequence),
        }
    }
}

/// The frames a directory holds: the next sequence number and every complete
/// picture, newest last. Anything else there is removed.
fn frame_files(directory: &Path) -> Result<(u64, Vec<(FrameName, PathBuf)>)> {
    let mut earlier = Vec::new();
    for entry in fs::read_dir(directory)?.flatten() {
        let name = entry.file_name();
        match name.to_str().and_then(FrameName::parse) {
            Some(name) => earlier.push((name, entry.path())),
            // Nothing else belongs here; an interrupted capture leaves no name we keep.
            None => {
                let _ = fs::remove_file(entry.path());
            }
        }
    }
    // Numbers only grow, so that no name ever stands for two pictures.
    let sequence = earlier
        .iter()
        .map(|(name, _)| name.sequence)
        .max()
        .unwrap_or(0)
        .checked_add(1)
        .ok_or_else(|| Error::new("invalid_capture", "Live frame sequence is exhausted"))?;
    // What an interrupted capture left behind is no picture. Whatever a viewer
    // was handed was checked when it was handed out, so it is never this.
    earlier.retain(|(_, path)| {
        let complete = ppm_frame(path).is_some();
        if !complete {
            let _ = fs::remove_file(path);
        }
        complete
    });
    earlier.sort_by_key(|(name, _)| name.sequence);
    Ok((sequence, earlier))
}

/// Keep `keep` and the picture a viewer may still be decoding, the newest of
/// `earlier`; remove every other frame.
fn retire_frames(earlier: Vec<(FrameName, PathBuf)>, keep: &Path) {
    let predecessor = earlier.last().map(|(_, path)| path.clone());
    for (_, path) in earlier {
        if path != keep && Some(&path) != predecessor.as_ref() {
            let _ = fs::remove_file(path);
        }
    }
}

/// The reply that names one frame file.
fn frame_reply(path: &Path, name: &FrameName, size: (u64, u64), changed: bool) -> Value {
    let (width, height) = size;
    json!({"mime_type":"image/x-portable-pixmap","bytes":ppm_bytes(width, height),
        "width":width,"height":height,"path":path,"sequence":name.sequence,"changed":changed,
        "source":if name.mailbox.is_some() {"mailbox"} else {"screendump"}})
}

/// A live frame as an uncompressed file in the user's runtime directory.
///
/// A viewer that refreshes several times a second should not pay for PNG
/// compression, base64 and a multi-megabyte JSON reply per frame. Only this
/// frame and its predecessor are kept, because a viewer may still be decoding
/// the previous one. This is the fallback for an engine that publishes no
/// frame mailbox ([`mailbox_frame_file`]): QEMU writes the picture with
/// `screendump` on its main loop, so every poll costs the guest that loop.
fn frame_file(client: &mut Qmp, id: &str, request: &Value) -> Result {
    // The launcher allows the emulator to write files of the size of one such
    // picture and no more (tools/run_ios_paravirt.py). One that would exceed
    // what this transport takes is not asked for: the emulator would write it
    // before it could be refused, and the viewer falls back to inline frames.
    let panel = &request["display_profile"];
    if ppm_bytes(n(panel, "width"), n(panel, "height")) > FRAME_FILE_LIMIT {
        return Err(Error::new(
            "ios_frame_file_unavailable",
            "The display is too large for uncompressed live frames",
        )
        .remedy("Use the default inline frame transport"));
    }
    let runtime = private_runtime().ok_or_else(|| {
        Error::new(
            "ios_frame_file_unavailable",
            "No private runtime directory is available for live frames",
        )
        .remedy("Use the default inline frame transport")
    })?;
    let directory = frame_directory(&runtime, id)?;
    let (sequence, earlier) = frame_files(&directory)?;
    let name = FrameName {
        sequence,
        mailbox: None,
    };
    let image = directory.join(name.file_name());
    client
        .call("screendump", json!({"filename":image,"format":"ppm"}))
        .inspect_err(|_| {
            // A command that failed may have left part of a picture.
            let _ = fs::remove_file(&image);
        })?;
    let invalid = |image: &Path| {
        let _ = fs::remove_file(image);
        Error::new("invalid_capture", "Emulator did not produce a valid frame")
    };
    let Some(size) = ppm_frame(&image) else {
        return Err(invalid(&image));
    };
    let bytes = ppm_bytes(size.0, size.1);
    // An unchanged picture keeps its file and number, so a viewer can skip
    // the reload and slow down while nothing moves.
    if let Some((previous, path)) = earlier.last() {
        let same = same_frame_files(path, &image, bytes).unwrap_or(false);
        if same {
            let _ = fs::remove_file(&image);
            return Ok(frame_reply(path, previous, size, false));
        }
    }
    retire_frames(earlier, &image);
    Ok(frame_reply(&image, &name, size, true))
}

/// The frame mailbox format the engine writes (reims-frame-mailbox.patch,
/// `runtime::frame_mailbox`): a 4 KiB header, then two slots.
const MAILBOX_MAGIC: &[u8; 8] = b"PSFRMBX1";
const MAILBOX_HEADER_BYTES: u64 = 4096;
const MAILBOX_SLOTS: u64 = 2;
const MAILBOX_FORMAT_BGRA8: u32 = u32::from_le_bytes(*b"BGRA");

/// The newest frame a mailbox names, before its pixels are read.
#[derive(Debug, PartialEq, Eq)]
struct MailboxSlot {
    number: u64,
    width: u32,
    height: u32,
    offset: u64,
    bytes: u64,
    sequence_at: u64,
    sequence: u64,
}

#[derive(Debug, PartialEq, Eq)]
enum MailboxState {
    /// Nothing published yet, or the slots are being laid out again.
    Empty,
    /// The slot was being rewritten; ask again.
    Torn,
    Ready(MailboxSlot),
}

/// Read a mailbox header. A file that is not a mailbox is an error; a slot
/// whose sequence or description does not hold is `Torn`.
fn mailbox_state(file: &File) -> std::io::Result<MailboxState> {
    let mut header = [0u8; 192];
    file.read_exact_at(&mut header, 0)?;
    let u32_at = |at: usize| u32::from_le_bytes(header[at..at + 4].try_into().unwrap());
    let u64_at = |at: usize| u64::from_le_bytes(header[at..at + 8].try_into().unwrap());
    if &header[..8] != MAILBOX_MAGIC
        || u32_at(8) != 1
        || u64::from(u32_at(12)) != MAILBOX_HEADER_BYTES
        || u64::from(u32_at(16)) != MAILBOX_SLOTS
    {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "not a frame mailbox",
        ));
    }
    let capacity = u64_at(24);
    let number = u64_at(40);
    if number == 0 || capacity == 0 {
        return Ok(MailboxState::Empty);
    }
    let slot = number % MAILBOX_SLOTS;
    let at = 64 + 64 * slot as usize;
    let sequence = u64_at(at);
    let (width, height, stride, format, bytes) = (
        u32_at(at + 8),
        u32_at(at + 12),
        u32_at(at + 16),
        u32_at(at + 20),
        u64_at(at + 24),
    );
    let edges = 1..=16384;
    if sequence != number.saturating_mul(2)
        || !edges.contains(&width)
        || !edges.contains(&height)
        || u64::from(stride) != u64::from(width) * 4
        || format != MAILBOX_FORMAT_BGRA8
        || bytes != u64::from(stride) * u64::from(height)
        || bytes > capacity
        || ppm_bytes(width.into(), height.into()) > FRAME_FILE_LIMIT
    {
        return Ok(MailboxState::Torn);
    }
    Ok(MailboxState::Ready(MailboxSlot {
        number,
        width,
        height,
        offset: MAILBOX_HEADER_BYTES + slot * capacity,
        bytes,
        sequence_at: at as u64,
        sequence,
    }))
}

/// The slot's pixels, or `None` when the writer began rewriting it meanwhile.
fn mailbox_pixels(file: &File, slot: &MailboxSlot) -> std::io::Result<Option<Vec<u8>>> {
    let mut pixels = vec![0u8; slot.bytes as usize];
    file.read_exact_at(&mut pixels, slot.offset)?;
    let mut after = [0u8; 8];
    file.read_exact_at(&mut after, slot.sequence_at)?;
    Ok((u64::from_le_bytes(after) == slot.sequence).then_some(pixels))
}

/// A binary PPM of tight BGRA8 rows (alpha dropped, as `screendump` does).
fn bgra_ppm(pixels: &[u8], width: u32, height: u32) -> Vec<u8> {
    let header = format!("P6\n{width} {height}\n255\n");
    let count = width as usize * height as usize;
    let mut out = vec![0u8; header.len() + count * 3];
    out[..header.len()].copy_from_slice(header.as_bytes());
    for (rgb, bgra) in out[header.len()..]
        .chunks_exact_mut(3)
        .zip(pixels.chunks_exact(4))
    {
        rgb[0] = bgra[2];
        rgb[1] = bgra[1];
        rgb[2] = bgra[0];
    }
    out
}

/// The guest's frame mailbox, when the launcher named one where it creates
/// them (`$XDG_RUNTIME_DIR/tomacco/ios-mailbox/<token>/frames`, see
/// tools/run_ios_paravirt.py) and the engine has created it there, private to
/// this user. `None` sends the viewer to the monitor's `screendump`.
fn mailbox_file(runtime: &Path, request: &Value) -> Option<File> {
    let path = PathBuf::from(request["frame_mailbox"]["path"].as_str()?);
    let root = runtime.join("tomacco").join("ios-mailbox");
    let directory = path.parent()?;
    let token = directory.file_name()?.to_str()?;
    if path.file_name()? != "frames"
        || directory.parent() != Some(root.as_path())
        || identifier(token, "frame mailbox", Some("[0-9a-f]{32}")).is_err()
        || ![root.parent()?, root.as_path(), directory]
            .into_iter()
            .all(private_directory)
    {
        return None;
    }
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(&path)
        .ok()?;
    let metadata = file.metadata().ok()?;
    let owner = unsafe { libc::geteuid() };
    (metadata.is_file() && metadata.uid() == owner && metadata.mode() & 0o077 == 0).then_some(file)
}

/// The number of the newest frame the guest's mailbox holds: 0 before its
/// first present, `None` without a mailbox (an older engine, no private
/// runtime directory) or while a slot is being rewritten. The renderer
/// publishes only changed frames, so a number that stays the same means a
/// still picture, not by itself a stalled guest. One header read; no file is
/// written.
pub(super) fn mailbox_frame_number(request: &Value) -> Option<u64> {
    mailbox_frame_number_in(&private_runtime()?, request)
}

fn mailbox_frame_number_in(runtime: &Path, request: &Value) -> Option<u64> {
    let file = mailbox_file(runtime, request)?;
    match mailbox_state(&file).ok()? {
        MailboxState::Empty => Some(0),
        MailboxState::Ready(slot) => Some(slot.number),
        MailboxState::Torn => None,
    }
}

/// A live frame from the engine's frame mailbox, without the emulator's
/// monitor: the renderer publishes every changed present frame from a thread
/// of its own, and the picture is converted to PPM here, in this process.
/// An unchanged frame costs one header read and touches no file. `None` when
/// there is no mailbox or no frame in it yet (an older engine, a boot before
/// its first present, a geometry change in progress): the caller asks the
/// monitor instead.
fn mailbox_frame_file(id: &str, request: &Value) -> Result<Option<Value>> {
    let Some(runtime) = private_runtime() else {
        return Ok(None);
    };
    mailbox_frame_file_in(&runtime, id, request)
}

fn mailbox_frame_file_in(runtime: &Path, id: &str, request: &Value) -> Result<Option<Value>> {
    let Some(file) = mailbox_file(runtime, request) else {
        return Ok(None);
    };
    let directory = frame_directory(runtime, id)?;
    let (sequence, earlier) = frame_files(&directory)?;
    // The slot a reader holds is rewritten two frames later; a reader that is
    // overtaken three times running leaves this poll to the monitor.
    for _ in 0..3 {
        let slot = match mailbox_state(&file) {
            Ok(MailboxState::Ready(slot)) => slot,
            Ok(MailboxState::Torn) => continue,
            Ok(MailboxState::Empty) | Err(_) => return Ok(None),
        };
        let size = (u64::from(slot.width), u64::from(slot.height));
        if let Some((name, path)) = earlier.last()
            && name.mailbox == Some(slot.number)
        {
            return Ok(Some(frame_reply(path, name, size, false)));
        }
        // A short read is a file being laid out again for another geometry.
        let Ok(Some(pixels)) = mailbox_pixels(&file, &slot) else {
            continue;
        };
        let name = FrameName {
            sequence,
            mailbox: Some(slot.number),
        };
        let image = directory.join(name.file_name());
        // Written under a name no viewer is handed, then renamed: a reader of
        // the final name never sees part of a picture, and an interrupted
        // write is removed with the next poll.
        let partial = directory.join(format!(".{}.partial", name.file_name()));
        let written = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&partial)
            .and_then(|mut stream| stream.write_all(&bgra_ppm(&pixels, slot.width, slot.height)))
            .and_then(|()| fs::rename(&partial, &image));
        if let Err(error) = written {
            let _ = fs::remove_file(&partial);
            return Err(error.into());
        }
        retire_frames(earlier, &image);
        return Ok(Some(frame_reply(&image, &name, size, true)));
    }
    Ok(None)
}

fn pointer(client: &mut Qmp, x: u32, y: u32, button: Option<bool>, usb: bool) -> Result<Value> {
    if usb {
        let value = u64::from(x) | (u64::from(y) << 16) | (u64::from(button.unwrap_or(true)) << 32);
        return client.call(
            "qom-set",
            json!({"path":HID_PATH,"property":"touch-state","value":value}),
        );
    }
    let mut events = vec![
        json!({"type":"abs","data":{"axis":"x","value":x}}),
        json!({"type":"abs","data":{"axis":"y","value":y}}),
    ];
    if let Some(down) = button {
        events.push(json!({"type":"btn","data":{"button":"left","down":down}}));
    }
    client.call("input-send-event", json!({"events":events}))
}

/// After a mutation was submitted, a transport failure does not license replay.
/// A release acknowledgement concerns QEMU acceptance, never guest delivery.
fn input_failure(error: Error, action: &str, stage: &str, release: Option<Value>) -> Error {
    let mut details = error
        .details
        .as_deref()
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    details.insert("action".into(), json!(action));
    details.insert("input_stage".into(), json!(stage));
    details.insert("request_may_have_executed".into(), json!(true));
    details.insert("automatic_retry_safe".into(), json!(false));
    if let Some(release) = release {
        details.insert("release".into(), release);
    }
    error.details(Value::Object(details))
}

fn release_receipt(release: &Result<Value>) -> Value {
    match release {
        Ok(_) => json!({"attempted":true,"accepted_by_emulator":true,
            "guest_delivery_verified":false}),
        Err(error) => json!({"attempted":true,"accepted_by_emulator":null,
            "guest_delivery_verified":false,"request_may_have_executed":true,
            "error":error.value()}),
    }
}

fn input(client: &mut Qmp, a: &Value, action: &str, usb: bool) -> Result<Value> {
    running(client)?;
    match action {
        "home" => {
            if !usb {
                return Err(Error::new(
                    "ios_home_unavailable",
                    "Start with input_transport=usb-hid for the iOS Home usage",
                ));
            }
            let hold = a["hold_ms"].as_u64().unwrap_or(100);
            if !(1..=1000).contains(&hold) {
                return Err(Error::new("invalid_hold", "Home hold must be 1–1000 ms"));
            }
            client
                .call(
                    "qom-set",
                    json!({"path":HID_PATH,"property":"home-press-ms","value":hold}),
                )
                .map_err(|error| input_failure(error, action, "home", None))?;
        }
        "key" => {
            let keys = a["keys"]
                .as_array()
                .filter(|keys| !keys.is_empty() && keys.len() <= 8)
                .ok_or_else(|| Error::new("invalid_keys", "Supply one to eight QEMU key names"))?;
            if keys
                .iter()
                .any(|key| key.as_str().is_none_or(|s| s.is_empty() || s.len() > 32))
            {
                return Err(Error::new("invalid_keys", "Invalid key name"));
            }
            let keys: Vec<_> = keys
                .iter()
                .map(|key| json!({"type":"qcode","data":key}))
                .collect();
            let hold = a["hold_ms"].as_u64().unwrap_or(100);
            if !(1..=1000).contains(&hold) {
                return Err(Error::new("invalid_hold", "Key hold must be 1–1000 ms"));
            }
            // QEMU owns the release timer, even if this client disconnects.
            client
                .call("send-key", json!({"keys":keys,"hold-time":hold}))
                .map_err(|error| input_failure(error, action, "key", None))?;
        }
        "move" | "click" | "drag" => {
            let (x, y) = coordinates(a, "x", "y")?;
            if action == "move" {
                if usb {
                    return Err(Error::new(
                        "ios_touch_has_no_hover",
                        "Use click or drag for a touchscreen contact",
                    ));
                }
                pointer(client, x, y, None, false)
                    .map_err(|error| input_failure(error, action, "move", None))?;
            } else {
                let (end_x, end_y) = if action == "drag" {
                    coordinates(a, "to_x", "to_y")?
                } else {
                    (x, y)
                };
                let duration =
                    a["duration_ms"]
                        .as_u64()
                        .unwrap_or(if action == "drag" { 400 } else { 60 });
                if !(1..=2000).contains(&duration) {
                    return Err(Error::new(
                        "invalid_duration",
                        "Pointer duration must be 1–2000 ms",
                    ));
                }
                // A drag that lifts while still moving is read as a fling, and a
                // scroll view keeps going after it; resting at the end point
                // first leaves no release velocity, so the content stops where
                // the finger did.
                let rest = if action == "drag" {
                    a["hold_ms"].as_u64().unwrap_or(0)
                } else {
                    0
                };
                if rest > 1000 {
                    return Err(Error::new("invalid_hold", "A drag rests 1–1000 ms"));
                }
                if let Err(error) = pointer(client, x, y, Some(true), usb) {
                    let released = pointer(client, x, y, Some(false), usb);
                    return Err(input_failure(
                        error,
                        action,
                        "press",
                        Some(release_receipt(&released)),
                    ));
                }
                let moved: Result<()> = (|| {
                    let steps = if action == "drag" {
                        duration.div_ceil(20).min(60)
                    } else {
                        1
                    };
                    let started = Instant::now();
                    for step in 1..=steps {
                        if started.elapsed() > Duration::from_millis(duration + 5000) {
                            return Err(Error::new(
                                "ios_input_timeout",
                                "Pointer sequence exceeded its deadline",
                            ));
                        }
                        thread::sleep(
                            (started + Duration::from_millis(duration * step / steps))
                                .saturating_duration_since(Instant::now()),
                        );
                        if action == "drag" {
                            let interpolate = |start: u32, end: u32| {
                                (i64::from(start)
                                    + (i64::from(end) - i64::from(start)) * step as i64
                                        / steps as i64) as u32
                            };
                            pointer(
                                client,
                                interpolate(x, end_x),
                                interpolate(y, end_y),
                                None,
                                usb,
                            )?;
                        }
                    }
                    if rest > 0 {
                        thread::sleep(Duration::from_millis(rest));
                    }
                    Ok(())
                })();
                // Always attempt release after a failed intermediate motion.
                let released = pointer(client, end_x, end_y, Some(false), usb);
                let receipt = release_receipt(&released);
                moved.map_err(|error| {
                    input_failure(error, action, "motion", Some(receipt.clone()))
                })?;
                released.map_err(|error| input_failure(error, action, "release", Some(receipt)))?;
            }
        }
        _ => {
            return Err(Error::new(
                "invalid_control_action",
                "Unsupported input action",
            ));
        }
    }
    Ok(json!({"accepted_by_emulator":true,"guest_response_verified":false}))
}

/// A held contact without any event for this long is lifted: a client that
/// stopped sending (or forgot touch_up) must not keep the control lane. A
/// client that holds still sends touch_move at the same point to keep it.
const TOUCH_IDLE: Duration = Duration::from_secs(5);

/// A contact held down by a persistent client. From touch_down to touch_up
/// it owns the guest's control lane and monitor, so no other input lands
/// inside the gesture; file frames take their own lane and keep coming.
struct Touch {
    job: String,
    client: Qmp,
    usb: bool,
    at: (u32, u32),
    last: Instant,
    generation: u64,
    _lane: Lock,
}

impl Touch {
    fn report(&mut self, at: (u32, u32), down: Option<bool>) -> Result<Value> {
        let result = pointer(&mut self.client, at.0, at.1, down, self.usb);
        // A gesture may last; its acknowledgements are not kept.
        self.client.transcript.clear();
        result
    }
    fn lift(mut self) -> Value {
        let at = self.at;
        release_receipt(&self.report(at, Some(false)))
    }
}

/// The contact of this process, if any. A one-shot call cannot hold one.
struct TouchLane {
    held: Mutex<Option<Touch>>,
    generation: std::sync::atomic::AtomicU64,
}

static TOUCHES: TouchLane = TouchLane::new();
static TOUCH_CLIENT: AtomicBool = AtomicBool::new(false);

fn touch_accepted(contact: &str) -> Value {
    json!({"accepted_by_emulator":true,"guest_response_verified":false,"contact":contact})
}

impl TouchLane {
    const fn new() -> Self {
        Self {
            held: Mutex::new(None),
            generation: std::sync::atomic::AtomicU64::new(0),
        }
    }
    fn held(&self) -> std::sync::MutexGuard<'_, Option<Touch>> {
        self.held
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
    /// Presses at `at`; `open` takes the guest's control lane and monitor.
    /// A contact this client never released is lifted first.
    fn down(
        &self,
        job: &str,
        at: (u32, u32),
        open: impl FnOnce() -> Result<(Qmp, bool, Lock)>,
    ) -> Result<(Value, u64)> {
        let mut held = self.held();
        let replaced = held.take().map(Touch::lift);
        let (client, usb, lane) = open()?;
        let generation = self.generation.fetch_add(1, Ordering::Relaxed) + 1;
        let mut touch = Touch {
            job: job.to_string(),
            client,
            usb,
            at,
            last: Instant::now(),
            generation,
            _lane: lane,
        };
        if let Err(error) = touch.report(at, Some(true)) {
            let receipt = touch.lift();
            return Err(input_failure(error, "touch_down", "press", Some(receipt)));
        }
        *held = Some(touch);
        let mut result = touch_accepted("down");
        if let Some(receipt) = replaced {
            result["replaced_contact"] = receipt;
        }
        Ok((result, generation))
    }
    /// Moves the held contact. The same point again only keeps it alive.
    fn moved(&self, job: &str, at: (u32, u32)) -> Result<Value> {
        let mut held = self.held();
        let Some(touch) = held.as_mut().filter(|touch| touch.job == job) else {
            return Err(Error::new(
                "ios_touch_not_down",
                "No contact is held down for this guest",
            )
            .remedy("Press again with touch_down"));
        };
        touch.last = Instant::now();
        if touch.at != at {
            if let Err(error) = touch.report(at, None) {
                let receipt = held.take().map(Touch::lift);
                return Err(input_failure(error, "touch_move", "motion", receipt));
            }
            touch.at = at;
        }
        Ok(touch_accepted("down"))
    }
    /// Lifts the contact held for `job`, at `at` when given; `None` when
    /// this client holds none for it.
    fn up(&self, job: &str, at: Option<(u32, u32)>) -> Option<Result<Value>> {
        let mut touch = {
            let mut held = self.held();
            if !held.as_ref().is_some_and(|touch| touch.job == job) {
                return None;
            }
            held.take()?
        };
        // The release carries no position on the touchscreen: move there first.
        let moved = match at {
            Some(at) if at != touch.at => touch.report(at, None).map(|_| touch.at = at),
            _ => Ok(()),
        };
        let released = touch.report(touch.at, Some(false));
        let receipt = release_receipt(&released);
        Some(
            moved
                .map_err(|error| input_failure(error, "touch_up", "motion", Some(receipt.clone())))
                .and_then(|()| {
                    released
                        .map_err(|error| input_failure(error, "touch_up", "release", Some(receipt)))
                })
                .map(|_| touch_accepted("up")),
        )
    }
    /// Lifts the contact of `generation` once it has been idle for `idle`;
    /// false once that contact is gone.
    fn expire(&self, generation: u64, idle: Duration) -> bool {
        let mut held = self.held();
        match held.as_ref() {
            Some(touch) if touch.generation == generation => {
                if touch.last.elapsed() < idle {
                    return true;
                }
                if let Some(touch) = held.take() {
                    touch.lift();
                }
                false
            }
            _ => false,
        }
    }
    fn release(&self) {
        let touch = self.held().take();
        if let Some(touch) = touch {
            touch.lift();
        }
    }
}

/// Makes this process a persistent client (tomaccoctl mcp) that may hold a
/// contact down; whatever it still holds is lifted when the client ends.
pub struct TouchClient(());
impl TouchClient {
    pub fn open() -> Self {
        TOUCH_CLIENT.store(true, Ordering::Relaxed);
        Self(())
    }
}
impl Drop for TouchClient {
    fn drop(&mut self) {
        TOUCHES.release();
    }
}

fn touch(id: &str, a: &Value, action: &str) -> Result<Value> {
    match action {
        "touch_down" => {
            if !TOUCH_CLIENT.load(Ordering::Relaxed) {
                return Err(Error::new(
                    "ios_touch_lane_required",
                    "touch_down needs a persistent client; a one-shot call would end with the contact down",
                )
                .remedy("Send touches through tomaccoctl mcp, or use click or drag"));
            }
            let at = coordinates(a, "x", "y")?;
            let (result, generation) = TOUCHES.down(id, at, || {
                let lane = lane(id, "control")?;
                let guest = guest(id)?;
                let mut client = Qmp::open(&guest.socket)?;
                running(&mut client)?;
                Ok((client, guest.usb, lane))
            })?;
            thread::spawn(move || {
                while TOUCHES.expire(generation, TOUCH_IDLE) {
                    thread::sleep(Duration::from_millis(100));
                }
            });
            Ok(result)
        }
        "touch_move" => TOUCHES.moved(id, coordinates(a, "x", "y")?),
        "touch_up" => {
            let at = if a.get("x").is_some() || a.get("y").is_some() {
                Some(coordinates(a, "x", "y")?)
            } else {
                None
            };
            if let Some(result) = TOUCHES.up(id, at) {
                return result;
            }
            // Held by no client of this process, perhaps by one that ended:
            // the release is sent anyway, and no contact is left down.
            let _lane = lane(id, "control")?;
            let guest = guest(id)?;
            let mut client = Qmp::open(&guest.socket)?;
            let released = match at {
                Some((x, y)) => pointer(&mut client, x, y, Some(false), guest.usb),
                None if guest.usb => pointer(&mut client, 0, 0, Some(false), true),
                None => client.call(
                    "input-send-event",
                    json!({"events":[{"type":"btn","data":{"button":"left","down":false}}]}),
                ),
            };
            released.map_err(|error| input_failure(error, action, "release", None))?;
            Ok(touch_accepted("up"))
        }
        _ => Err(Error::new(
            "invalid_control_action",
            "Unsupported touch action",
        )),
    }
}

/// Takes one of a guest's lanes, waiting up to LANE_WAIT for its turn:
/// "control" owns the monitor and input, "frame" the live frame files.
fn lane(id: &str, kind: &str) -> Result<Lock> {
    let lock = Lock::open(&home()?.join("locks").join(format!("ios-{kind}-{id}.lock")))?;
    let waiting = Instant::now();
    while !lock.acquire(true)? {
        if waiting.elapsed() >= LANE_WAIT {
            return Err(Error::new(
                "ios_control_busy",
                "Another control action is in progress for this guest",
            )
            .details(json!({"request_may_have_executed":false,"automatic_retry_safe":true})));
        }
        thread::sleep(Duration::from_millis(3));
    }
    Ok(lock)
}

/// A live interactive guest of this data directory and its owned monitor.
struct Guest {
    job: Value,
    output: PathBuf,
    request: Value,
    socket: PathBuf,
    usb: bool,
}

fn guest(id: &str) -> Result<Guest> {
    let job = jobs::get(id)?;
    let plan = &job["plan"];
    if s(&plan["operation_key"], "operation") != "ios.emulator.paravirtual"
        || !b(&plan["operation_key"]["arguments"], "interactive")
        || Path::new(plan["steps"][0]["argv"][1].as_str().unwrap_or(""))
            .file_name()
            .is_none_or(|name| name != "run_ios_paravirt.py")
    {
        return Err(Error::new(
            "invalid_interactive_guest",
            "Select an iOS job started with interactive=true",
        ));
    }
    if b(&job, "cancelling") || jobs::TERMINAL.contains(&s(&job, "state")) {
        return Err(Error::new(
            "ios_guest_stopped",
            "The virtual iOS job has stopped",
        ));
    }
    let root = home()?.join("ios-emulator").canonicalize()?;
    let directory = PathBuf::from(s(plan, "cwd")).canonicalize()?;
    let output = directory.join("paravirtual");
    let request = output.join("request.json");
    if directory.parent() != Some(root.as_path()) || output.is_symlink() || request.is_symlink() {
        return Err(Error::new(
            "invalid_report_path",
            "Control request is outside the owned guest directory",
        ));
    }
    if !request.is_file() {
        return Err(Error::new(
            "ios_control_starting",
            "The guest has not published its control monitor yet",
        ));
    }
    if fs::metadata(&request)?.len() > 1024 * 1024 {
        return Err(Error::new(
            "invalid_report_path",
            "Control request exceeds 1 MiB",
        ));
    }
    let request = read_json(&request, None)?;
    let usb = s(&request["control"], "input_transport") == "usb-hid";
    let socket = PathBuf::from(s(&request["control"], "socket"));
    let parent = socket
        .parent()
        .ok_or_else(|| Error::new("invalid_control_socket", "Missing control directory"))?;
    let metadata = fs::symlink_metadata(parent)?;
    let owner = unsafe { libc::geteuid() };
    if s(&request["control"], "protocol") != "qmp"
        || socket.file_name().is_none_or(|name| name != "control.sock")
        || !metadata.is_dir()
        || metadata.uid() != owner
        || metadata.mode() & 0o077 != 0
    {
        return Err(Error::new(
            "invalid_control_socket",
            "Control monitor must be in its owner's private directory",
        ));
    }
    let socket_metadata = fs::symlink_metadata(&socket).map_err(|_| {
        // The emulator removes its listening socket when it exits, while the
        // job still hashes the media. The launcher publishes its result as
        // soon as the emulator has gone, and a stop leaves its record before
        // the emulator exits: either file tells an emulator that has ended
        // from one that has not published its monitor yet.
        let published =
            |name: &str| fs::symlink_metadata(output.join(name)).is_ok_and(|meta| meta.is_file());
        if published("result.json") || published("stop.json") {
            Error::new(
                "ios_control_ended",
                "The emulator is no longer running; the job is finishing",
            )
        } else {
            Error::new(
                "ios_control_starting",
                "The control monitor is not connected yet",
            )
        }
    })?;
    if !socket_metadata.file_type().is_socket() || socket_metadata.uid() != owner {
        return Err(Error::new(
            "invalid_control_socket",
            "Control monitor is not an owned Unix socket",
        ));
    }
    Ok(Guest {
        job,
        output,
        request,
        socket,
        usb,
    })
}

fn running(client: &mut Qmp) -> Result<()> {
    if !b(&client.call("query-status", json!({}))?, "running") {
        return Err(Error::new(
            "ios_guest_paused",
            "The virtual iOS CPU is not running",
        ));
    }
    Ok(())
}

/// How iOS reads the USB keyboard's keys: the layout of the guest's language,
/// and setting it in the guest has no effect (`keyboard_layout` in
/// guest-host-channel.h reports the language).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum TextLayout {
    Us,
    German,
}

/// The modifiers and the QEMU key name that type one character; None for what
/// the layout cannot type directly. Characters the US keyboard has at another
/// place follow the German Mac layout iOS uses (Option is `alt_r`). Its dead keys
/// (`^`, `` ` ``, `~`) are left out: they wait for a next key.
fn text_keys(character: char, layout: TextLayout) -> Option<(&'static [&'static str], String)> {
    const PLAIN: &[&str] = &[];
    const SHIFT: &[&str] = &["shift"];
    const OPTION: &[&str] = &["alt_r"];
    const SHIFT_OPTION: &[&str] = &["shift", "alt_r"];
    let key = |modifiers: &'static [&'static str], name: &str| Some((modifiers, name.to_string()));
    match character {
        '0'..='9' => return key(PLAIN, &character.to_string()),
        ' ' => return key(PLAIN, "spc"),
        '\n' => return key(PLAIN, "ret"),
        '\t' => return key(PLAIN, "tab"),
        '.' => return key(PLAIN, "dot"),
        ',' => return key(PLAIN, "comma"),
        _ => {}
    }
    if character.is_ascii_alphabetic() {
        let lower = character.to_ascii_lowercase();
        // The German layout has these two where the US one has the other.
        let name = match (layout, lower) {
            (TextLayout::German, 'y') => 'z',
            (TextLayout::German, 'z') => 'y',
            _ => lower,
        };
        let modifiers = if character.is_ascii_uppercase() {
            SHIFT
        } else {
            PLAIN
        };
        return key(modifiers, &name.to_string());
    }
    match layout {
        TextLayout::Us => match character {
            '-' => key(PLAIN, "minus"),
            '=' => key(PLAIN, "equal"),
            '[' => key(PLAIN, "bracket_left"),
            ']' => key(PLAIN, "bracket_right"),
            '\\' => key(PLAIN, "backslash"),
            ';' => key(PLAIN, "semicolon"),
            '\'' => key(PLAIN, "apostrophe"),
            '`' => key(PLAIN, "grave_accent"),
            '/' => key(PLAIN, "slash"),
            '!' => key(SHIFT, "1"),
            '@' => key(SHIFT, "2"),
            '#' => key(SHIFT, "3"),
            '$' => key(SHIFT, "4"),
            '%' => key(SHIFT, "5"),
            '^' => key(SHIFT, "6"),
            '&' => key(SHIFT, "7"),
            '*' => key(SHIFT, "8"),
            '(' => key(SHIFT, "9"),
            ')' => key(SHIFT, "0"),
            '_' => key(SHIFT, "minus"),
            '+' => key(SHIFT, "equal"),
            '{' => key(SHIFT, "bracket_left"),
            '}' => key(SHIFT, "bracket_right"),
            '|' => key(SHIFT, "backslash"),
            ':' => key(SHIFT, "semicolon"),
            '"' => key(SHIFT, "apostrophe"),
            '~' => key(SHIFT, "grave_accent"),
            '<' => key(SHIFT, "comma"),
            '>' => key(SHIFT, "dot"),
            '?' => key(SHIFT, "slash"),
            _ => None,
        },
        TextLayout::German => match character {
            'ß' => key(PLAIN, "minus"),
            'ü' => key(PLAIN, "bracket_left"),
            'Ü' => key(SHIFT, "bracket_left"),
            'ö' => key(PLAIN, "semicolon"),
            'Ö' => key(SHIFT, "semicolon"),
            'ä' => key(PLAIN, "apostrophe"),
            'Ä' => key(SHIFT, "apostrophe"),
            '+' => key(PLAIN, "bracket_right"),
            '*' => key(SHIFT, "bracket_right"),
            '#' => key(PLAIN, "backslash"),
            '\'' => key(SHIFT, "backslash"),
            '-' => key(PLAIN, "slash"),
            '_' => key(SHIFT, "slash"),
            ';' => key(SHIFT, "comma"),
            ':' => key(SHIFT, "dot"),
            '<' => key(PLAIN, "less"),
            '>' => key(SHIFT, "less"),
            '!' => key(SHIFT, "1"),
            '"' => key(SHIFT, "2"),
            '$' => key(SHIFT, "4"),
            '%' => key(SHIFT, "5"),
            '&' => key(SHIFT, "6"),
            '/' => key(SHIFT, "7"),
            '(' => key(SHIFT, "8"),
            ')' => key(SHIFT, "9"),
            '=' => key(SHIFT, "0"),
            '?' => key(SHIFT, "minus"),
            '@' => key(OPTION, "l"),
            '[' => key(OPTION, "5"),
            ']' => key(OPTION, "6"),
            '|' => key(OPTION, "7"),
            '{' => key(OPTION, "8"),
            '}' => key(OPTION, "9"),
            '\\' => key(SHIFT_OPTION, "7"),
            _ => None,
        },
    }
}

/// A layout from its name: what a caller passes, or the guest's `language`.
fn text_layout(name: &str) -> Option<TextLayout> {
    match name {
        "us" | "en" => Some(TextLayout::Us),
        "de" => Some(TextLayout::German),
        _ => None,
    }
}

/// Type a text on the USB keyboard, one key at a time. iOS repeats a held key
/// and the emulator releases it on a timer of its own, so each waits its turn.
fn type_text(client: &mut Qmp, text: &str, layout: TextLayout) -> Result<Value> {
    const HOLD_MS: u64 = 40;
    if text.is_empty() || text.chars().count() > 1024 {
        return Err(Error::new(
            "invalid_text",
            "Supply 1 to 1024 characters of text",
        ));
    }
    let keys = text
        .chars()
        .map(|character| {
            text_keys(character, layout).ok_or_else(|| {
                Error::new(
                    "invalid_text",
                    format!("This keyboard layout cannot type {character:?}"),
                )
            })
        })
        .collect::<Result<Vec<_>>>()?;
    for (index, (modifiers, name)) in keys.iter().enumerate() {
        let pressed: Vec<_> = modifiers
            .iter()
            .chain(std::iter::once(&name.as_str()))
            .map(|key| json!({"type":"qcode","data":key}))
            .collect();
        client
            .call("send-key", json!({"keys":pressed,"hold-time":HOLD_MS}))
            .map_err(|error| error.details(json!({"action":"text","characters_sent":index})))?;
        std::thread::sleep(Duration::from_millis(2 * HOLD_MS));
    }
    Ok(json!({"characters":keys.len(),"layout":if layout == TextLayout::German {"de"} else {"us"}}))
}

/// Replace the running boot's trust-cache module (`vphone-trust-cache-update`):
/// the guest is stopped for the write, and goes on whatever became of it. A
/// guest that was not running is refused and left as it is.
fn trust_cache_update(client: &mut Qmp, file: &str) -> Result<Value> {
    let file = path(file, false, true)?;
    if !fs::metadata(&file).is_ok_and(|meta| meta.is_file()) {
        return Err(Error::new(
            "invalid_trust_cache",
            "path must name a trust-cache module file",
        ));
    }
    running(client)?;
    client.call("stop", json!({}))?;
    let written = client.call(
        "qom-set",
        json!({"path":"/machine","property":"trust-cache-update","value":file}),
    );
    client.call("cont", json!({}))?;
    written?;
    Ok(json!({"updated":true}))
}

pub fn handle(a: &Value) -> Result {
    let id = s(a, "job_id");
    identifier(id, "job ID", Some("[0-9a-f]{32}"))?;
    let action = s(a, "action");
    if action.starts_with("touch_") {
        let result = touch(id, a, action)?;
        return Ok(json!({"job_id":id,"action":action,"result":result}));
    }
    // A live frame the engine has published needs no monitor at all: the
    // poll then costs the guest's main loop nothing, and takes only the frame
    // lane, so the picture keeps following a finger that holds the control lane.
    if action == "frame" && s(a, "transport") == "file" {
        let _frames = lane(id, "frame")?;
        if let Some(result) = mailbox_frame_file(id, &guest(id)?.request)? {
            return Ok(json!({"job_id":id,"action":"frame","result":result}));
        }
    }
    let _lane = lane(id, "control")?;
    let Guest {
        job,
        output,
        request,
        socket,
        usb,
    } = guest(id)?;
    let plan = &job["plan"];
    let mut client = Qmp::open(&socket)?;
    let result = match action {
        "stop" => {
            // Preserve unknown native mutations. Reserving the framework lane
            // also prevents a new request between the idle check and QMP quit.
            let reservation =
                if plan["operation_key"]["arguments"]["guest_control_backend"].is_string() {
                    Some(guest_control::reserve_idle(id)?)
                } else {
                    None
                };
            let recovery = json!({"operation":"ios.emulator.report","arguments":{"job_id":id}});
            let flushed = reservation.as_ref().is_some_and(|(_, flushed)| *flushed);
            let idle_verified = reservation.is_some();
            let stop_record = output.join("stop.json");
            let requested_at = now();
            // The guest's own record of how it was asked to stop. Whoever
            // later decides whether these disks may be reused reads it with
            // the report; no record means the flush was not confirmed. A
            // record that cannot be written must not keep a guest running.
            // A client that has withdrawn the request neither leaves a record
            // nor ends the emulator.
            check_cancelled()?;
            let recorded = atomic_json(
                &stop_record,
                &json!({"format":1,"requested_at":requested_at,
                    "native_worker_idle_verified":idle_verified,
                    "guest_storage_flushed":flushed}),
            )
            .is_ok();
            // The record claims a flush that counts only if the emulator then
            // exits. When it does not, or the request is withdrawn on the
            // way, the guest keeps running and writing: the claim is taken
            // back, and no record at all is better than a stale one.
            let retract = |error: Error| {
                let rewritten = atomic_json(
                    &stop_record,
                    &json!({"format":1,"requested_at":requested_at,
                        "native_worker_idle_verified":idle_verified,
                        "guest_storage_flushed":false,"quit_acknowledged":false,
                        "error":error.code}),
                )
                .is_ok();
                if !rewritten {
                    let _ = fs::remove_file(&stop_record);
                }
                error
            };
            check_cancelled().map_err(retract)?;
            client.call("quit", json!({})).map_err(|error| {
                retract(error).details(json!({"job_id":id,"action":"stop",
                    "request_may_have_executed":true,"automatic_retry_safe":false,
                    "recovery":recovery}))
            })?;
            // The durable job must still observe process exit and hash media.
            // QMP acknowledgement is neither guest shutdown nor finalization.
            json!({"emulator_exit_requested":true,"native_worker_idle_verified":reservation.is_some(),
                "guest_storage_flushed":flushed,"stop_recorded":recorded,
                "guest_shutdown_verified":false,"storage_hashes_verified":false,"recovery":recovery})
        }
        "power_button" => {
            // The owned tomacco-vphone machine connects this notification to
            // its timed GPIO key, not to emulator termination. Other machine
            // types may interpret system_powerdown as a shutdown request.
            if s(&request, "machine") != "tomacco-vphone" {
                return Err(Error::new(
                    "ios_power_button_unavailable",
                    "The timed side button requires the tomacco-vphone machine",
                ));
            }
            if !b(&client.call("query-status", json!({}))?, "running") {
                return Err(Error::new(
                    "ios_guest_paused",
                    "The virtual iOS CPU is not running",
                ));
            }
            client
                .call("system_powerdown", json!({}))
                .map_err(|error| input_failure(error, action, "side_button", None))?;
            json!({"accepted_by_emulator":true,"guest_response_verified":false,
                "screen_state_verified":false,"button":"side","hold_ms":100})
        }
        "metrics" => metrics::read(&mut client, &request)?,
        "status" => {
            let mut status = json!({"state":client.call("query-status", json!({}))?,
                "pointers":client.call("query-mice", json!({}))?,
                "power_button_supported":s(&request, "machine") == "tomacco-vphone",
                "requested_display_profile":request.get("display_profile").cloned().unwrap_or(Value::Null),
                "input_transport":if usb {"usb-hid"} else {"virtio"}});
            if usb {
                let mut hid = serde_json::Map::new();
                for name in [
                    "configured",
                    "home-pending",
                    "touch-pending",
                    "touch-active",
                    "home-reports-delivered",
                    "touch-reports-delivered",
                ] {
                    hid.insert(
                        name.to_string(),
                        client.call("qom-get", json!({"path":HID_PATH,"property":name}))?,
                    );
                }
                status["usb_hid"] = Value::Object(hid);
            }
            status
        }
        "frame" if s(a, "transport") == "file" => {
            let _frames = lane(id, "frame")?;
            frame_file(&mut client, id, &request)?
        }
        "screenshot" | "frame" => {
            let captures = output.join("captures");
            if captures.is_symlink() {
                return Err(Error::new(
                    "invalid_capture_path",
                    "Capture directory was replaced",
                ));
            }
            private_dir(&captures)?;
            // Live frames own a temporary directory. Success and every error
            // release it; polling must not accumulate permanent screenshots.
            let temporary = if action == "frame" {
                Some(tempfile::tempdir_in(&captures)?)
            } else {
                None
            };
            let image = match &temporary {
                Some(dir) => dir.path().join("frame.png"),
                None => captures.join(format!("{}.png", uuid::Uuid::new_v4().simple())),
            };
            client.call("screendump", json!({"filename":image,"format":"png"}))?;
            let metadata = fs::symlink_metadata(&image)?;
            let limit = if temporary.is_some() { 16 } else { 128 } * 1024 * 1024;
            if !metadata.is_file() || metadata.len() > limit {
                return Err(Error::new(
                    "invalid_capture",
                    "Emulator produced an invalid or oversized capture",
                ));
            }
            let bytes = fs::read(&image)?;
            if bytes.len() < 24 || &bytes[..8] != b"\x89PNG\r\n\x1a\n" || &bytes[12..16] != b"IHDR"
            {
                return Err(Error::new(
                    "invalid_capture",
                    "Emulator did not produce a PNG capture",
                ));
            }
            let mut result = json!({"mime_type":"image/png","bytes":bytes.len(),
                "sha256":digest(&bytes),"width":u32::from_be_bytes(bytes[16..20].try_into().unwrap()),
                "height":u32::from_be_bytes(bytes[20..24].try_into().unwrap())});
            if temporary.is_some() {
                use base64::Engine;
                result["data_url"] = json!(format!(
                    "data:image/png;base64,{}",
                    base64::engine::general_purpose::STANDARD.encode(&bytes)
                ));
            } else {
                result["path"] = json!(image);
            }
            result
        }
        "home" | "key" | "move" | "click" | "drag" => input(&mut client, a, action, usb)?,
        "trust_cache_update" => trust_cache_update(&mut client, s(a, "path"))?,
        "text" => {
            // The guest's language decides the layout, unless the caller says.
            let name = match s(a, "layout") {
                "" => s(
                    &guest_control::call(&json!({"job_id":id,"operation":"keyboard_layout",
                        "timeout_seconds":30}))?["response"],
                    "language",
                )
                .to_string(),
                named => named.to_string(),
            };
            let layout = text_layout(&name).ok_or_else(|| {
                Error::new(
                    "invalid_text_layout",
                    format!("No key table for the layout {name:?}; name layout us or de"),
                )
            })?;
            type_text(&mut client, s(a, "text"), layout)?
        }
        _ => {
            return Err(Error::new(
                "invalid_control_action",
                "Unsupported control action",
            ));
        }
    };
    Ok(json!({"job_id":id,"action":action,"result":result}))
}

#[cfg(test)]
mod text_tests {
    use super::*;

    fn keys(character: char, layout: TextLayout) -> Option<(Vec<&'static str>, String)> {
        text_keys(character, layout).map(|(modifiers, name)| (modifiers.to_vec(), name))
    }

    #[test]
    fn a_text_becomes_the_keys_of_a_us_keyboard() {
        let us = TextLayout::Us;
        assert_eq!(keys('a', us), Some((vec![], "a".into())));
        assert_eq!(keys('Z', us), Some((vec!["shift"], "z".into())));
        assert_eq!(keys('&', us), Some((vec!["shift"], "7".into())));
        assert_eq!(keys(' ', us), Some((vec![], "spc".into())));
        assert_eq!(keys('\n', us), Some((vec![], "ret".into())));
        assert_eq!(keys('?', us), Some((vec!["shift"], "slash".into())));
    }

    /// `Hello Zy-_@[]{}|~` typed through US keys on a German guest reads
    /// `Hello Yzß?"ü+Ü*'>`: the table below is that reading, reversed.
    #[test]
    fn a_german_guest_reads_these_keys() {
        let de = TextLayout::German;
        assert_eq!(keys('z', de), Some((vec![], "y".into())));
        assert_eq!(keys('Y', de), Some((vec!["shift"], "z".into())));
        assert_eq!(keys('ß', de), Some((vec![], "minus".into())));
        assert_eq!(keys('?', de), Some((vec!["shift"], "minus".into())));
        assert_eq!(keys('"', de), Some((vec!["shift"], "2".into())));
        assert_eq!(keys('ü', de), Some((vec![], "bracket_left".into())));
        assert_eq!(keys('+', de), Some((vec![], "bracket_right".into())));
        assert_eq!(keys('Ü', de), Some((vec!["shift"], "bracket_left".into())));
        assert_eq!(keys('*', de), Some((vec!["shift"], "bracket_right".into())));
        assert_eq!(keys('\'', de), Some((vec!["shift"], "backslash".into())));
        assert_eq!(keys('-', de), Some((vec![], "slash".into())));
    }

    #[test]
    fn what_a_layout_lacks_is_refused() {
        for character in ['€', '語', '\u{7f}'] {
            assert_eq!(keys(character, TextLayout::Us), None);
            assert_eq!(keys(character, TextLayout::German), None);
        }
        assert_eq!(keys('ü', TextLayout::Us), None);
        // A dead key waits for a next one; it is no way to type it.
        assert_eq!(keys('^', TextLayout::German), None);
    }

    #[test]
    fn a_layout_is_named_or_follows_the_language() {
        assert_eq!(text_layout("us"), Some(TextLayout::Us));
        assert_eq!(text_layout("en"), Some(TextLayout::Us));
        assert_eq!(text_layout("de"), Some(TextLayout::German));
        assert_eq!(text_layout("fr"), None);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_frame_file_is_a_complete_picture_of_the_size_its_header_states() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("frame.ppm");
        let picture = [b"P6\n2 1\n255\n".as_slice(), &[7; 6]].concat();
        assert_eq!(ppm_bytes(2, 1), picture.len() as u64);
        fs::write(&path, &picture).unwrap();
        assert_eq!(ppm_frame(&path), Some((2, 1)));
        // Part of a picture, one with too much, another format or depth, a
        // header that does not state a size, and no picture at all.
        let broken: [&[u8]; 8] = [
            &picture[..picture.len() - 1],
            b"P6\n2 1\n255\n\0\0\0\0\0\0\0",
            b"P5\n2 1\n255\n\0\0\0\0\0\0",
            b"P6\n2 1\n65535\n\0\0\0\0\0\0",
            b"P6\n0 1\n255\n",
            b"P6\n2 1\n",
            b"Not a frame",
            b"",
        ];
        for bytes in broken {
            fs::write(&path, bytes).unwrap();
            assert_eq!(ppm_frame(&path), None, "{bytes:?}");
        }
        // A link is not followed, and a file that is gone is no picture.
        fs::write(&path, &picture).unwrap();
        let link = directory.path().join("link.ppm");
        std::os::unix::fs::symlink(&path, &link).unwrap();
        assert_eq!(ppm_frame(&link), None);
        fs::remove_file(&path).unwrap();
        assert_eq!(ppm_frame(&path), None);
    }

    #[test]
    fn only_a_frame_the_transport_takes_is_asked_of_the_emulator() {
        // "P6\n1179 2556\n255\n" and three bytes per pixel.
        assert_eq!(ppm_bytes(1179, 2556), 17 + 1179 * 2556 * 3);
        assert!(ppm_bytes(4096, 4096) <= FRAME_FILE_LIMIT);
        assert!(ppm_bytes(4096, 5461) <= FRAME_FILE_LIMIT);
        assert!(ppm_bytes(4096, 5462) > FRAME_FILE_LIMIT);
        assert!(ppm_bytes(8192, 8192) > FRAME_FILE_LIMIT);
        // A request without a panel states no size, and no size overflows.
        assert!(ppm_bytes(0, 0) <= FRAME_FILE_LIMIT);
        assert!(ppm_bytes(u64::MAX, u64::MAX) > FRAME_FILE_LIMIT);
    }

    /// A mailbox as the engine lays it out (runtime::frame_mailbox): header,
    /// two slots of `capacity`, frame `number` in slot `number % 2`.
    fn mailbox(path: &Path, frames: &[(u64, u32, u32, &[u8])]) -> File {
        let capacity = frames
            .iter()
            .map(|(_, _, _, pixels)| pixels.len() as u64)
            .max()
            .unwrap_or(0);
        let mut bytes = vec![0u8; (MAILBOX_HEADER_BYTES + 2 * capacity) as usize];
        bytes[..8].copy_from_slice(MAILBOX_MAGIC);
        bytes[8..12].copy_from_slice(&1u32.to_le_bytes());
        bytes[12..16].copy_from_slice(&4096u32.to_le_bytes());
        bytes[16..20].copy_from_slice(&2u32.to_le_bytes());
        bytes[24..32].copy_from_slice(&capacity.to_le_bytes());
        for slot in 0..2 {
            let at = 64 + 64 * slot;
            bytes[at..at + 8].copy_from_slice(&u64::MAX.to_le_bytes());
        }
        let mut latest = 0;
        for &(number, width, height, pixels) in frames {
            let slot = (number % 2) as usize;
            let at = 64 + 64 * slot;
            bytes[at..at + 8].copy_from_slice(&(2 * number).to_le_bytes());
            bytes[at + 8..at + 12].copy_from_slice(&width.to_le_bytes());
            bytes[at + 12..at + 16].copy_from_slice(&height.to_le_bytes());
            bytes[at + 16..at + 20].copy_from_slice(&(width * 4).to_le_bytes());
            bytes[at + 20..at + 24].copy_from_slice(b"BGRA");
            bytes[at + 24..at + 32].copy_from_slice(&(pixels.len() as u64).to_le_bytes());
            let data = (MAILBOX_HEADER_BYTES + slot as u64 * capacity) as usize;
            bytes[data..data + pixels.len()].copy_from_slice(pixels);
            latest = latest.max(number);
        }
        bytes[40..48].copy_from_slice(&latest.to_le_bytes());
        let _ = fs::remove_file(path);
        fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(path)
            .unwrap()
            .write_all(&bytes)
            .unwrap();
        File::open(path).unwrap()
    }

    #[test]
    fn a_frame_name_states_its_sequence_and_where_its_picture_came_from() {
        let capture = FrameName {
            sequence: 7,
            mailbox: None,
        };
        let published = FrameName {
            sequence: 8,
            mailbox: Some(3),
        };
        assert_eq!(capture.file_name(), "frame-7.ppm");
        assert_eq!(published.file_name(), "frame-8.m3.ppm");
        assert_eq!(FrameName::parse("frame-7.ppm"), Some(capture));
        assert_eq!(FrameName::parse("frame-8.m3.ppm"), Some(published));
        for other in [
            "frame-.ppm",
            "frame-8.m.ppm",
            "frame-.m3.ppm",
            "frame-+8.ppm",
            "frame-8.m-3.ppm",
            "frame-8.ppm.partial",
            ".frame-8.m3.ppm.partial",
            "frame-8.x3.ppm",
            "frames",
        ] {
            assert_eq!(FrameName::parse(other), None, "{other}");
        }
    }

    #[test]
    fn a_mailbox_frame_becomes_the_ppm_screendump_would_have_written() {
        // Two pixels, B G R A each: the alpha byte is not part of the picture.
        let ppm = bgra_ppm(&[1, 2, 3, 255, 10, 20, 30, 0], 2, 1);
        assert_eq!(
            ppm,
            [b"P6\n2 1\n255\n".as_slice(), &[3, 2, 1, 30, 20, 10]].concat()
        );
        assert_eq!(ppm.len() as u64, ppm_bytes(2, 1));
    }

    #[test]
    fn only_a_complete_slot_of_the_newest_frame_is_read_from_a_mailbox() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("frames");
        let first = [9u8; 2 * 2 * 4];
        let second = [7u8; 2 * 2 * 4];
        let file = mailbox(&path, &[(1, 2, 2, &first), (2, 2, 2, &second)]);
        let MailboxState::Ready(slot) = mailbox_state(&file).unwrap() else {
            panic!("no frame");
        };
        assert_eq!(
            (slot.number, slot.width, slot.height, slot.bytes),
            (2, 2, 2, 16)
        );
        assert_eq!(mailbox_pixels(&file, &slot).unwrap().unwrap(), second);
        // The writer began frame 4 in the same slot while it was read.
        let writer = fs::OpenOptions::new().write(true).open(&path).unwrap();
        writer
            .write_all_at(&9u64.to_le_bytes(), slot.sequence_at)
            .unwrap();
        assert_eq!(mailbox_pixels(&file, &slot).unwrap(), None);
        assert_eq!(mailbox_state(&file).unwrap(), MailboxState::Torn);
        // A slot that does not describe tight BGRA8 rows is no frame either.
        writer
            .write_all_at(&4u64.to_le_bytes(), slot.sequence_at)
            .unwrap();
        writer.write_all_at(b"RGBA", slot.sequence_at + 20).unwrap();
        assert_eq!(mailbox_state(&file).unwrap(), MailboxState::Torn);
        // Nothing published yet, and a file that is no mailbox.
        let empty = mailbox(&path, &[]);
        assert_eq!(mailbox_state(&empty).unwrap(), MailboxState::Empty);
        fs::write(&path, vec![0u8; 4096]).unwrap();
        assert!(mailbox_state(&File::open(&path).unwrap()).is_err());
        fs::write(&path, b"short").unwrap();
        assert!(mailbox_state(&File::open(&path).unwrap()).is_err());
    }

    #[test]
    fn a_mailbox_is_read_only_where_the_launcher_creates_it_privately() {
        let runtime = tempfile::tempdir().unwrap();
        let root = runtime.path().join("tomacco/ios-mailbox");
        let token = "0123456789abcdef0123456789abcdef";
        fs::create_dir_all(root.join(token)).unwrap();
        for path in [root.parent().unwrap(), root.as_path(), &root.join(token)] {
            fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
        }
        let path = root.join(token).join("frames");
        mailbox(&path, &[(1, 1, 1, &[1, 2, 3, 4])]);
        let request = |path: &Path| json!({"frame_mailbox":{"path":path}});
        assert!(mailbox_file(runtime.path(), &request(&path)).is_some());
        // The newest frame's number is one header read away.
        assert_eq!(
            mailbox_frame_number_in(runtime.path(), &request(&path)),
            Some(1)
        );
        assert_eq!(mailbox_frame_number_in(runtime.path(), &json!({})), None);
        // No mailbox named, another file name, a directory that is not a
        // launcher token or lies elsewhere.
        assert!(mailbox_file(runtime.path(), &json!({})).is_none());
        assert!(mailbox_file(runtime.path(), &request(&root.join(token).join("other"))).is_none());
        fs::create_dir(root.join("not-a-token")).unwrap();
        fs::set_permissions(root.join("not-a-token"), fs::Permissions::from_mode(0o700)).unwrap();
        mailbox(
            &root.join("not-a-token/frames"),
            &[(1, 1, 1, &[1, 2, 3, 4])],
        );
        assert!(mailbox_file(runtime.path(), &request(&root.join("not-a-token/frames"))).is_none());
        let elsewhere = tempfile::tempdir().unwrap();
        let foreign = elsewhere.path().join(token);
        fs::create_dir(&foreign).unwrap();
        mailbox(&foreign.join("frames"), &[(1, 1, 1, &[1, 2, 3, 4])]);
        assert!(mailbox_file(runtime.path(), &request(&foreign.join("frames"))).is_none());
        // A file others may read, a directory others may enter, and a link.
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(mailbox_file(runtime.path(), &request(&path)).is_none());
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        fs::set_permissions(root.join(token), fs::Permissions::from_mode(0o750)).unwrap();
        assert!(mailbox_file(runtime.path(), &request(&path)).is_none());
        fs::set_permissions(root.join(token), fs::Permissions::from_mode(0o700)).unwrap();
        let target = root.join(token).join("target");
        fs::rename(&path, &target).unwrap();
        std::os::unix::fs::symlink(&target, &path).unwrap();
        assert!(mailbox_file(runtime.path(), &request(&path)).is_none());
    }

    #[test]
    fn a_mailbox_frame_is_converted_once_and_then_answers_unchanged() {
        let runtime = tempfile::tempdir().unwrap();
        let token = "fedcba9876543210fedcba9876543210";
        let box_directory = runtime.path().join("tomacco/ios-mailbox").join(token);
        fs::create_dir_all(&box_directory).unwrap();
        for path in box_directory.ancestors().take(4) {
            fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
        }
        let path = box_directory.join("frames");
        let request = json!({"frame_mailbox":{"path":path}});
        let id = "00112233445566778899aabbccddeeff";
        // An engine that has published nothing leaves the poll to the monitor.
        mailbox(&path, &[]);
        assert_eq!(
            mailbox_frame_file_in(runtime.path(), id, &request).unwrap(),
            None
        );
        let first = [[1u8, 2, 3, 255]; 6].concat();
        mailbox(&path, &[(1, 3, 2, &first)]);
        let reply = mailbox_frame_file_in(runtime.path(), id, &request)
            .unwrap()
            .unwrap();
        assert_eq!(reply["changed"], true);
        assert_eq!(reply["sequence"], 1);
        assert_eq!(reply["source"], "mailbox");
        assert_eq!(
            (reply["width"].as_u64(), reply["height"].as_u64()),
            (Some(3), Some(2))
        );
        let image = PathBuf::from(reply["path"].as_str().unwrap());
        assert_eq!(image.file_name().unwrap(), "frame-1.m1.ppm");
        assert_eq!(ppm_frame(&image), Some((3, 2)));
        assert_eq!(reply["bytes"], fs::metadata(&image).unwrap().len());
        assert_eq!(
            fs::read(&image).unwrap(),
            [b"P6\n3 2\n255\n".as_slice(), &[3, 2, 1].repeat(6)].concat()
        );
        // The same frame number again: the same file, nothing written.
        let modified = fs::metadata(&image).unwrap().modified().unwrap();
        let again = mailbox_frame_file_in(runtime.path(), id, &request)
            .unwrap()
            .unwrap();
        assert_eq!(again["changed"], false);
        assert_eq!(
            (&again["path"], &again["sequence"]),
            (&reply["path"], &reply["sequence"])
        );
        assert_eq!(fs::metadata(&image).unwrap().modified().unwrap(), modified);
        // A newer frame gets the next number; the one a viewer may still be
        // decoding stays, older ones go, and so does what is not a frame.
        let directory = image.parent().unwrap().to_path_buf();
        fs::write(directory.join(".frame-9.m9.ppm.partial"), b"P6").unwrap();
        mailbox(&path, &[(1, 3, 2, &first), (2, 3, 2, &[9u8; 24])]);
        let second = mailbox_frame_file_in(runtime.path(), id, &request)
            .unwrap()
            .unwrap();
        assert_eq!(
            (second["changed"].as_bool(), second["sequence"].as_u64()),
            (Some(true), Some(2))
        );
        mailbox(&path, &[(3, 3, 2, &[5u8; 24]), (2, 3, 2, &[9u8; 24])]);
        let third = mailbox_frame_file_in(runtime.path(), id, &request)
            .unwrap()
            .unwrap();
        assert_eq!(third["sequence"], 3);
        let mut names: Vec<_> = fs::read_dir(&directory)
            .unwrap()
            .map(|entry| entry.unwrap().file_name().into_string().unwrap())
            .collect();
        names.sort();
        assert_eq!(names, ["frame-2.m2.ppm", "frame-3.m3.ppm"]);
        // A geometry change in progress empties the mailbox: the monitor answers.
        mailbox(&path, &[]);
        assert_eq!(
            mailbox_frame_file_in(runtime.path(), id, &request).unwrap(),
            None
        );
        // No mailbox at all is no error either.
        fs::remove_file(&path).unwrap();
        assert_eq!(
            mailbox_frame_file_in(runtime.path(), id, &request).unwrap(),
            None
        );
    }

    /// A monitor that acknowledges every command and records the touchscreen
    /// writes; the `refuse`-th one is refused, as a full report queue is.
    struct Monitor {
        directory: tempfile::TempDir,
        touches: Arc<Mutex<Vec<u64>>>,
    }

    fn monitor(refuse: usize) -> Monitor {
        let directory = socket_tempdir();
        let listener =
            std::os::unix::net::UnixListener::bind(directory.path().join("control.sock")).unwrap();
        let touches = Arc::new(Mutex::new(Vec::new()));
        let seen = touches.clone();
        thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { return };
                let seen = seen.clone();
                thread::spawn(move || {
                    let _ = stream.write_all(b"{\"QMP\":{}}\n");
                    for line in BufReader::new(stream.try_clone().unwrap()).lines() {
                        let Ok(line) = line else { return };
                        let request: Value = serde_json::from_str(&line).unwrap();
                        let mut reply = json!({"return":{},"id":request["id"]});
                        if request["execute"] == "query-status" {
                            reply["return"] = json!({"running":true});
                        }
                        if request["arguments"]["property"] == "touch-state" {
                            let mut seen = seen.lock().unwrap();
                            seen.push(request["arguments"]["value"].as_u64().unwrap());
                            if seen.len() == refuse {
                                reply = json!({"error":{"class":"GenericError",
                                    "desc":"Touch reports await guest consumption"},"id":request["id"]});
                            }
                        }
                        if writeln!(stream, "{reply}").is_err() {
                            return;
                        }
                    }
                });
            }
        });
        Monitor { directory, touches }
    }

    impl Monitor {
        fn lane(&self) -> Lock {
            Lock::open(&self.directory.path().join("locks").join("control.lock")).unwrap()
        }
        /// Takes the lane as `lane()` in the product does: waiting a moment.
        /// A flock belongs to the open file, and a process another test
        /// forks in parallel holds a copy of every descriptor until its
        /// exec closes them; a lane this process released can stay taken
        /// for that instant. A lane still held by a contact is not released
        /// by waiting, so this never passes for one that was not freed.
        fn acquire_soon(&self) -> Option<Lock> {
            let lane = self.lane();
            let deadline = Instant::now() + Duration::from_secs(5);
            loop {
                if lane.acquire(true).unwrap() {
                    return Some(lane);
                }
                if Instant::now() >= deadline {
                    return None;
                }
                thread::sleep(Duration::from_millis(2));
            }
        }
        /// What touch_down's caller hands the lane: the monitor and the taken control lane.
        fn open(&self) -> impl FnOnce() -> Result<(Qmp, bool, Lock)> + '_ {
            move || {
                let lane = self
                    .acquire_soon()
                    .expect("the control lane is still taken");
                Ok((
                    Qmp::open(&self.directory.path().join("control.sock"))?,
                    true,
                    lane,
                ))
            }
        }
        fn touches(&self) -> Vec<u64> {
            self.touches.lock().unwrap().clone()
        }
        /// The lane has been released (see acquire_soon).
        fn lane_free(&self) -> bool {
            self.acquire_soon().is_some()
        }
        /// The lane is held right now, by the contact under test.
        fn lane_held(&self) -> bool {
            !self.lane().acquire(true).unwrap()
        }
    }

    fn contact(x: u64, y: u64, down: bool) -> u64 {
        x | (y << 16) | (u64::from(down) << 32)
    }

    #[test]
    fn a_held_contact_reports_each_move_as_it_happens_and_lifts_once() {
        let lane = TouchLane::new();
        let monitor = monitor(0);
        let (down, generation) = lane.down("a", (100, 200), monitor.open()).unwrap();
        assert_eq!(down["contact"], "down");
        assert_eq!(monitor.touches(), [contact(100, 200, true)]);
        // No other input lands inside the gesture.
        assert!(monitor.lane_held());
        lane.moved("a", (300, 400)).unwrap();
        // The same point again only keeps the contact alive.
        lane.moved("a", (300, 400)).unwrap();
        assert_eq!(monitor.touches().len(), 2);
        // Another guest's contact is neither moved nor lifted.
        assert_eq!(
            lane.moved("b", (1, 1)).unwrap_err().code,
            "ios_touch_not_down"
        );
        assert!(lane.up("b", None).is_none());
        // The touchscreen's release has no position: the contact moves there first.
        let up = lane.up("a", Some((500, 600))).unwrap().unwrap();
        assert_eq!(up["contact"], "up");
        assert_eq!(
            monitor.touches(),
            [
                contact(100, 200, true),
                contact(300, 400, true),
                contact(500, 600, true),
                contact(500, 600, false)
            ]
        );
        assert!(monitor.lane_free());
        assert!(lane.up("a", None).is_none());
        assert!(!lane.expire(generation, Duration::ZERO));
    }

    #[test]
    fn a_contact_replaced_left_idle_or_given_up_is_lifted_and_frees_the_lane() {
        let lane = TouchLane::new();
        let monitor = monitor(0);
        let (_, first) = lane.down("a", (1, 1), monitor.open()).unwrap();
        // A press while one is still held: that one is lifted before the
        // lane is taken again, never left down.
        let (again, second) = lane.down("a", (2, 2), monitor.open()).unwrap();
        assert_eq!(again["replaced_contact"]["accepted_by_emulator"], true);
        assert_eq!(
            monitor.touches(),
            [
                contact(1, 1, true),
                contact(1, 1, false),
                contact(2, 2, true)
            ]
        );
        assert!(!lane.expire(first, Duration::ZERO));
        assert!(lane.expire(second, Duration::from_secs(60)));
        assert!(monitor.lane_held());
        // A client that stopped sending loses the contact.
        assert!(!lane.expire(second, Duration::ZERO));
        assert_eq!(monitor.touches().last(), Some(&contact(2, 2, false)));
        assert!(monitor.lane_free());
        assert_eq!(
            lane.moved("a", (3, 3)).unwrap_err().code,
            "ios_touch_not_down"
        );
        // A client that ends lifts what it holds.
        lane.down("a", (4, 4), monitor.open()).unwrap();
        lane.release();
        assert_eq!(monitor.touches().last(), Some(&contact(4, 4, false)));
        assert!(monitor.lane_free());
    }

    #[test]
    fn a_refused_move_lifts_the_contact_and_says_it_may_have_moved() {
        let lane = TouchLane::new();
        let monitor = monitor(2);
        lane.down("a", (1, 1), monitor.open()).unwrap();
        let error = lane.moved("a", (5, 5)).unwrap_err();
        assert_eq!(error.code, "qmp_error");
        let details = error.details.as_deref().unwrap();
        assert_eq!(details["input_stage"], "motion");
        assert_eq!(details["request_may_have_executed"], true);
        assert_eq!(details["release"]["accepted_by_emulator"], true);
        assert_eq!(
            monitor.touches(),
            [
                contact(1, 1, true),
                contact(5, 5, true),
                contact(1, 1, false)
            ]
        );
        assert!(monitor.lane_free());
        assert!(lane.up("a", None).is_none());
    }
}
