//! Pictures of the running virtual iPhone's screen, kept as files.
//!
//! The picture comes from the same frame `ios.emulator.control` hands a
//! viewer (`frame` with the file transport), so taking one costs the guest no
//! more than a viewer's refresh.
use super::*;

/// The colour of the plain bezel `frame:"device"` draws around the screen.
const BEZEL: [u8; 3] = [10, 10, 12];

/// A picture of tight RGB rows, as a binary PPM holds it.
#[derive(Debug, Clone, PartialEq)]
struct Picture {
    width: usize,
    height: usize,
    rgb: Vec<u8>,
}

/// Reads a complete binary PPM ("P6\n<width> <height>\n255\n" and RGB).
fn read_ppm(bytes: &[u8]) -> Option<Picture> {
    let mut fields = Vec::new();
    let mut start = None;
    let mut index = 0;
    while fields.len() < 4 && index < bytes.len().min(64) {
        let space = bytes[index].is_ascii_whitespace();
        match (start, space) {
            (None, false) => start = Some(index),
            (Some(from), true) => {
                fields.push(std::str::from_utf8(&bytes[from..index]).ok()?);
                start = None;
            }
            _ => {}
        }
        index += 1;
    }
    if fields.len() != 4 || fields[0] != "P6" || fields[3] != "255" {
        return None;
    }
    let width: usize = fields[1].parse().ok()?;
    let height: usize = fields[2].parse().ok()?;
    if !(1..=16384).contains(&width) || !(1..=16384).contains(&height) {
        return None;
    }
    let rgb = bytes.get(index..)?;
    (rgb.len() == width * height * 3).then(|| Picture {
        width,
        height,
        rgb: rgb.to_vec(),
    })
}

/// How much of the pixel at (x, y) lies inside a rectangle with rounded
/// corners, from 0 to 1, smoothed over one pixel at the curve.
fn coverage(x: usize, y: usize, rect: (f64, f64, f64, f64), radius: f64) -> f64 {
    let (left, top, width, height) = rect;
    let (px, py) = (x as f64 + 0.5, y as f64 + 0.5);
    if px < left || py < top || px > left + width || py > top + height {
        return 0.0;
    }
    let radius = radius.min(width / 2.0).min(height / 2.0).max(0.0);
    if radius == 0.0 {
        return 1.0;
    }
    let cx = px.clamp(left + radius, left + width - radius);
    let cy = py.clamp(top + radius, top + height - radius);
    let distance = ((px - cx).powi(2) + (py - cy).powi(2)).sqrt();
    (radius - distance + 0.5).clamp(0.0, 1.0)
}

/// The corner radius and bezel, in pixels of this picture, of the model the
/// phone runs as: the catalogue's points scaled by the picture's width. The
/// Home-button phone has square corners and a bezel of a 25th of its width.
fn geometry(model: &str, width: usize) -> (f64, f64) {
    let profile = crate::catalog::builtin()
        .as_array()
        .into_iter()
        .flatten()
        .find(|profile| profile["platform"] == "ios" && profile["id"] == model)
        .cloned();
    match profile {
        Some(profile) if !model.is_empty() && profile["width"].as_f64().unwrap_or(0.0) > 0.0 => {
            let scale = width as f64 / profile["width"].as_f64().unwrap_or(1.0);
            (
                profile["radius"].as_f64().unwrap_or(0.0) * scale,
                profile["bezel"].as_f64().unwrap_or(8.0) * scale,
            )
        }
        _ => (0.0, (width as f64 / 25.0).round()),
    }
}

/// The picture as RGBA: with `frame` "rounded", the screen's corners cut
/// round; with "device", inside a plain black bezel with rounded outer
/// corners. Outside is transparent. Returns the pixels and their size.
fn framed(picture: &Picture, frame: &str, radius: f64, bezel: f64) -> (Vec<u8>, usize, usize) {
    let border = if frame == "device" {
        bezel.round().max(1.0) as usize
    } else {
        0
    };
    let (width, height) = (picture.width + 2 * border, picture.height + 2 * border);
    let screen = (
        border as f64,
        border as f64,
        picture.width as f64,
        picture.height as f64,
    );
    let outer = (0.0, 0.0, width as f64, height as f64);
    let outer_radius = if radius > 0.0 {
        radius + border as f64
    } else {
        border as f64
    };
    let mut rgba = vec![0u8; width * height * 4];
    for y in 0..height {
        for x in 0..width {
            let inside = coverage(x, y, screen, radius);
            let pixel = &mut rgba[(y * width + x) * 4..][..4];
            let source = (x >= border && x < border + picture.width)
                .then(|| y.checked_sub(border))
                .flatten()
                .filter(|row| *row < picture.height)
                .map(|row| &picture.rgb[(row * picture.width + x - border) * 3..][..3]);
            if frame == "device" {
                let body = coverage(x, y, outer, outer_radius);
                for channel in 0..3 {
                    let screen = source.map_or(0.0, |rgb| rgb[channel] as f64);
                    pixel[channel] =
                        (screen * inside + BEZEL[channel] as f64 * (1.0 - inside)).round() as u8;
                }
                pixel[3] = (body * 255.0).round() as u8;
            } else if let Some(rgb) = source {
                pixel[..3].copy_from_slice(rgb);
                pixel[3] = (inside * 255.0).round() as u8;
            }
        }
    }
    (rgba, width, height)
}

/// CRC-32 (ISO 3309), as PNG chunks carry it.
fn crc32(parts: &[&[u8]]) -> u32 {
    let mut crc = !0u32;
    for byte in parts.iter().flat_map(|part| part.iter()) {
        crc ^= *byte as u32;
        for _ in 0..8 {
            crc = if crc & 1 != 0 {
                (crc >> 1) ^ 0xEDB8_8320
            } else {
                crc >> 1
            };
        }
    }
    !crc
}

/// An 8-bit PNG of tight rows: RGB with 3 channels, RGBA with 4.
pub(crate) fn png(pixels: &[u8], width: usize, height: usize, channels: usize) -> Result<Vec<u8>> {
    use flate2::{Compression, write::ZlibEncoder};
    let mut encoder = ZlibEncoder::new(Vec::new(), Compression::fast());
    for row in pixels.chunks_exact(width * channels).take(height) {
        encoder.write_all(&[0])?;
        encoder.write_all(row)?;
    }
    let data = encoder.finish()?;
    let mut header = Vec::with_capacity(13);
    header.extend((width as u32).to_be_bytes());
    header.extend((height as u32).to_be_bytes());
    header.extend([8, if channels == 4 { 6 } else { 2 }, 0, 0, 0]);
    let mut out = b"\x89PNG\r\n\x1a\n".to_vec();
    for (kind, body) in [
        (b"IHDR", header.as_slice()),
        (b"IDAT", data.as_slice()),
        (b"IEND", &[][..]),
    ] {
        out.extend((body.len() as u32).to_be_bytes());
        out.extend(kind);
        out.extend(body);
        out.extend(crc32(&[kind, body]).to_be_bytes());
    }
    Ok(out)
}

/// The latest frame of the running phone, read from the file the frame
/// transport wrote before a later frame can retire it.
fn current_frame(job: &str) -> Result<Picture> {
    let reply = control::handle(&json!({"job_id":job,"action":"frame","transport":"file"}))?;
    let path = PathBuf::from(s(&reply["result"], "path"));
    let size = control::ppm_frame(&path).ok_or_else(|| {
        Error::new(
            "invalid_capture",
            "The frame file is not a complete picture",
        )
    })?;
    let bytes = fs::read(&path)?;
    read_ppm(&bytes)
        .filter(|picture| (picture.width as u64, picture.height as u64) == size)
        .ok_or_else(|| {
            Error::new(
                "invalid_capture",
                "The frame changed while it was read; try again",
            )
        })
}

pub fn screenshot(a: &Value) -> Result {
    let target = output_path(s(a, "path"))?;
    if target
        .extension()
        .is_none_or(|extension| !extension.eq_ignore_ascii_case("png"))
    {
        return Err(Error::new(
            "invalid_path",
            "The screenshot is a PNG; name a file ending in .png",
        ));
    }
    let frame = match s(a, "frame") {
        "" => "none",
        frame @ ("none" | "rounded" | "device") => frame,
        _ => {
            return Err(Error::new(
                "invalid_argument",
                "frame is none, rounded or device",
            ));
        }
    };
    let (job, model) = virtual_phone::running_guest()?;
    let picture = current_frame(&job)?;
    let (radius, bezel) = geometry(&model, picture.width);
    let (bytes, width, height) = if frame == "none" {
        (
            png(&picture.rgb, picture.width, picture.height, 3)?,
            picture.width,
            picture.height,
        )
    } else {
        let (rgba, width, height) = framed(&picture, frame, radius, bezel);
        (png(&rgba, width, height, 4)?, width, height)
    };
    atomic_write(&target, &bytes)?;
    Ok(
        json!({"path":target,"mime_type":"image/png","bytes":bytes.len(),"sha256":digest(&bytes),
        "width":width,"height":height,"screen_width":picture.width,"screen_height":picture.height,
        "frame":frame,"corner_radius":if frame == "none" {0.0} else {radius.round()},
        "bezel":if frame == "device" {bezel.round().max(1.0)} else {0.0},
        "job_id":job,"model":model}),
    )
}

/// The bounds of a recording: seconds, and frames per second.
const RECORD_SECONDS: (u64, u64) = (1, 600);
const RECORD_FPS: (u64, u64) = (1, 30);

/// Checks a recording's arguments: an `.mp4` path, and seconds and frames
/// per second within their bounds (defaults 10 and 10).
fn recording(a: &Value) -> Result<(PathBuf, u64, u64)> {
    let target = output_path(s(a, "path"))?;
    if target
        .extension()
        .is_none_or(|extension| !extension.eq_ignore_ascii_case("mp4"))
    {
        return Err(Error::new(
            "invalid_path",
            "The recording is an MP4 video; name a file ending in .mp4",
        ));
    }
    let seconds = a["seconds"].as_u64().unwrap_or(10);
    let fps = a["fps"].as_u64().unwrap_or(10);
    if !(RECORD_SECONDS.0..=RECORD_SECONDS.1).contains(&seconds)
        || !(RECORD_FPS.0..=RECORD_FPS.1).contains(&fps)
    {
        return Err(Error::new(
            "invalid_argument",
            "seconds must be 1 to 600 and fps 1 to 30",
        ));
    }
    Ok((target, seconds, fps))
}

/// Records the running phone's screen for `seconds` into an MP4 as a job
/// (tools/ios_virtual_record.py): its frames, polled `fps` times a second
/// through `ios.emulator.control`, encoded by ffmpeg as they come.
pub fn record(a: &Value) -> Result {
    let (target, seconds, fps) = recording(a)?;
    let ffmpeg = toolchain::find("ffmpeg")?;
    let (job, _) = virtual_phone::running_guest()?;
    let argv = vec![
        toolchain::find("python3")?,
        research::script("ios_virtual_record.py")?
            .to_string_lossy()
            .into_owned(),
        "--cli".into(),
        crate::core::own_executable()?
            .to_string_lossy()
            .into_owned(),
        "--job".into(),
        job.clone(),
        "--output".into(),
        target.to_string_lossy().into_owned(),
        "--seconds".into(),
        seconds.to_string(),
        "--fps".into(),
        fps.to_string(),
        "--ffmpeg".into(),
        ffmpeg,
    ];
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut submitted = jobs::submit(
        "Record virtual iOS device screen",
        json!({"cwd":home()?,"timeout":seconds + 180,"exclusive":"ios-virtual-record","single_active":true,
            "operation_key":{"operation":"ios.virtual.record","arguments":{"path":target,"seconds":seconds,"fps":fps}},
            "steps":[{"argv":argv,"artifacts":[target]}]}),
    )?;
    submitted["guest_job"] = json!(job);
    Ok(submitted)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn picture(width: usize, height: usize, rgb: [u8; 3]) -> Picture {
        Picture {
            width,
            height,
            rgb: rgb.repeat(width * height),
        }
    }

    /// Decodes a PNG this module wrote: chunks, CRCs and unfiltered rows.
    fn decode(png: &[u8]) -> (usize, usize, u8, Vec<u8>) {
        use std::io::Read as _;
        assert_eq!(&png[..8], b"\x89PNG\r\n\x1a\n");
        let (mut at, mut header, mut data) = (8, Vec::new(), Vec::new());
        while at < png.len() {
            let length = u32::from_be_bytes(png[at..at + 4].try_into().unwrap()) as usize;
            let kind = &png[at + 4..at + 8];
            let body = &png[at + 8..at + 8 + length];
            let crc =
                u32::from_be_bytes(png[at + 8 + length..at + 12 + length].try_into().unwrap());
            assert_eq!(crc, crc32(&[kind, body]));
            match kind {
                b"IHDR" => header = body.to_vec(),
                b"IDAT" => data.extend(body),
                _ => {}
            }
            at += 12 + length;
        }
        let width = u32::from_be_bytes(header[..4].try_into().unwrap()) as usize;
        let height = u32::from_be_bytes(header[4..8].try_into().unwrap()) as usize;
        let mut raw = Vec::new();
        flate2::read::ZlibDecoder::new(data.as_slice())
            .read_to_end(&mut raw)
            .unwrap();
        let channels = if header[9] == 6 { 4 } else { 3 };
        let rows: Vec<u8> = raw
            .chunks_exact(width * channels + 1)
            .flat_map(|row| {
                assert_eq!(row[0], 0);
                row[1..].to_vec()
            })
            .collect();
        assert_eq!(rows.len(), width * height * channels);
        (width, height, header[9], rows)
    }

    #[test]
    fn the_crc_is_the_one_png_uses() {
        assert_eq!(crc32(&[b"IEND"]), 0xAE42_6082);
        assert_eq!(crc32(&[b"IE", b"ND"]), 0xAE42_6082);
    }

    #[test]
    fn a_ppm_frame_is_read_only_when_complete() {
        let mut bytes = b"P6\n2 1\n255\n".to_vec();
        bytes.extend([1, 2, 3, 4, 5, 6]);
        assert_eq!(
            read_ppm(&bytes),
            Some(Picture {
                width: 2,
                height: 1,
                rgb: vec![1, 2, 3, 4, 5, 6]
            })
        );
        assert_eq!(read_ppm(&bytes[..bytes.len() - 1]), None);
        assert_eq!(read_ppm(b"P5\n1 1\n255\n\0"), None);
        assert_eq!(read_ppm(b"P6\n1 1\n65535\n\0\0\0\0\0\0"), None);
        assert_eq!(read_ppm(b"P6\n0 1\n255\n"), None);
    }

    #[test]
    fn a_plain_screenshot_is_the_frame_as_an_rgb_png() {
        let frame = Picture {
            width: 2,
            height: 2,
            rgb: vec![255, 0, 0, 0, 255, 0, 0, 0, 255, 9, 9, 9],
        };
        let (width, height, kind, rows) = decode(&png(&frame.rgb, 2, 2, 3).unwrap());
        assert_eq!((width, height, kind), (2, 2, 2));
        assert_eq!(rows, frame.rgb);
    }

    #[test]
    fn rounded_corners_are_transparent_and_the_middle_is_the_screen() {
        let frame = picture(40, 80, [200, 100, 50]);
        let (rgba, width, height) = framed(&frame, "rounded", 12.0, 0.0);
        assert_eq!((width, height), (40, 80));
        let at = |x: usize, y: usize| &rgba[(y * width + x) * 4..][..4];
        for (x, y) in [(0, 0), (39, 0), (0, 79), (39, 79)] {
            assert_eq!(at(x, y)[3], 0, "corner {x},{y}");
        }
        assert_eq!(at(20, 40), [200, 100, 50, 255]);
        assert_eq!(at(20, 0), [200, 100, 50, 255]);
        // The curve is smoothed: some pixel along it is partly covered.
        assert!((0..12).any(|i| (1..255).contains(&at(i, i)[3])));
        let (_, _, kind, rows) = decode(&png(&rgba, width, height, 4).unwrap());
        assert_eq!((kind, rows), (6, rgba));
    }

    #[test]
    fn a_device_frame_puts_a_bezel_around_the_rounded_screen() {
        let frame = picture(40, 80, [200, 100, 50]);
        let (rgba, width, height) = framed(&frame, "device", 12.0, 4.0);
        assert_eq!((width, height), (48, 88));
        let at = |x: usize, y: usize| &rgba[(y * width + x) * 4..][..4];
        // Transparent outside the body, bezel at its edge, screen inside.
        assert_eq!(at(0, 0)[3], 0);
        assert_eq!(at(24, 1), [BEZEL[0], BEZEL[1], BEZEL[2], 255]);
        assert_eq!(at(24, 44), [200, 100, 50, 255]);
        // The screen's corner shows the bezel, not the picture.
        assert_eq!(at(6, 6), [BEZEL[0], BEZEL[1], BEZEL[2], 255]);
    }

    #[test]
    fn the_corners_and_bezel_follow_the_model_and_the_pictures_scale() {
        // The iPhone 18 Pro: 402 points wide, radius 62, bezel 16.
        let (radius, bezel) = geometry("18pro", 1206);
        assert_eq!((radius, bezel), (186.0, 48.0));
        // The Home-button phone has square corners.
        assert_eq!(geometry("", 750), (0.0, 30.0));
        assert_eq!(geometry("no-such-model", 750), (0.0, 30.0));
    }

    #[test]
    fn a_recording_is_an_mp4_of_bounded_length_and_rate() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("clip.mp4");
        let (target, seconds, fps) = recording(&json!({"path":path})).unwrap();
        assert_eq!((target, seconds, fps), (path.clone(), 10, 10));
        assert_eq!(
            recording(&json!({"path":path,"seconds":600,"fps":30}))
                .unwrap()
                .1,
            600
        );
        for refused in [
            json!({"path":root.path().join("clip.mov")}),
            json!({"path":path,"seconds":0}),
            json!({"path":path,"seconds":601}),
            json!({"path":path,"fps":31}),
            json!({"path":path,"fps":0}),
        ] {
            assert!(recording(&refused).is_err(), "{refused}");
        }
    }
}
