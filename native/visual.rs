//! Visual regression: compare a screenshot with a baseline PNG.
//!
//! The PNG reader covers what screenshots are: 8- and 16-bit grey, grey with
//! alpha, RGB, RGBA and palette images (1 to 8 bits), not interlaced. Pixels
//! are compared as RGBA with 8 bits per channel.
use crate::core::*;
use flate2::{Compression, read::ZlibDecoder, write::ZlibEncoder};
use serde_json::{Value, json};
use std::{
    fs,
    io::{Read, Write},
    path::Path,
};

const SIGNATURE: &[u8] = b"\x89PNG\r\n\x1a\n";
/// 64 megapixels: larger than any phone or tablet screen.
const MAX_PIXELS: u64 = 64 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq)]
pub struct Image {
    pub width: u32,
    pub height: u32,
    /// RGBA, 8 bits per channel, rows top to bottom.
    pub pixels: Vec<u8>,
}

fn unsupported(message: impl Into<String>) -> Error {
    Error::new("unsupported_image", message)
}

pub fn decode(data: &[u8]) -> Result<Image> {
    if !data.starts_with(SIGNATURE) {
        return Err(unsupported("Not a PNG file"));
    }
    let mut at = SIGNATURE.len();
    let (mut header, mut palette, mut transparency, mut compressed) =
        (None, Vec::new(), Vec::new(), Vec::new());
    while at + 12 <= data.len() {
        let length = u32::from_be_bytes(data[at..at + 4].try_into().unwrap()) as usize;
        let kind = &data[at + 4..at + 8];
        let end = at
            .checked_add(12 + length)
            .filter(|end| *end <= data.len())
            .ok_or_else(|| unsupported("The PNG file is truncated"))?;
        let body = &data[at + 8..at + 8 + length];
        match kind {
            b"IHDR" if length == 13 => header = Some(body.to_vec()),
            b"PLTE" => palette = body.to_vec(),
            b"tRNS" => transparency = body.to_vec(),
            b"IDAT" => compressed.extend_from_slice(body),
            b"IEND" => break,
            _ => (),
        }
        at = end;
    }
    let header = header.ok_or_else(|| unsupported("The PNG file has no header"))?;
    let width = u32::from_be_bytes(header[0..4].try_into().unwrap());
    let height = u32::from_be_bytes(header[4..8].try_into().unwrap());
    let (depth, color, interlace) = (header[8], header[9], header[12]);
    if width == 0 || height == 0 || u64::from(width) * u64::from(height) > MAX_PIXELS {
        return Err(unsupported(format!(
            "Unsupported image size {width}x{height}"
        )));
    }
    if interlace != 0 {
        return Err(unsupported("Interlaced PNG files are not supported"));
    }
    let channels: usize = match (color, depth) {
        (0, 1 | 2 | 4 | 8 | 16) => 1,
        (3, 1 | 2 | 4 | 8) => 1,
        (4, 8 | 16) => 2,
        (2, 8 | 16) => 3,
        (6, 8 | 16) => 4,
        _ => {
            return Err(unsupported(format!(
                "Unsupported PNG colour type {color} with {depth} bits"
            )));
        }
    };
    let bits_per_pixel = channels * depth as usize;
    let stride = (width as usize * bits_per_pixel).div_ceil(8);
    let step = bits_per_pixel.div_ceil(8);
    let expected = (stride + 1) * height as usize;
    let mut raw = Vec::with_capacity(expected);
    ZlibDecoder::new(&compressed[..])
        .take(expected as u64 + 1)
        .read_to_end(&mut raw)
        .map_err(|e| unsupported(format!("The PNG data is damaged: {e}")))?;
    if raw.len() != expected {
        return Err(unsupported("The PNG data has the wrong length"));
    }
    let mut rows = vec![0u8; stride * height as usize];
    for y in 0..height as usize {
        let filter = raw[y * (stride + 1)];
        let line = &raw[y * (stride + 1) + 1..(y + 1) * (stride + 1)];
        let (done, current) = rows.split_at_mut(y * stride);
        let previous = if y == 0 {
            None
        } else {
            Some(&done[(y - 1) * stride..])
        };
        let current = &mut current[..stride];
        for x in 0..stride {
            let a = if x >= step { current[x - step] } else { 0 };
            let b = previous.map_or(0, |p| p[x]);
            let c = if x >= step {
                previous.map_or(0, |p| p[x - step])
            } else {
                0
            };
            current[x] = line[x].wrapping_add(match filter {
                0 => 0,
                1 => a,
                2 => b,
                3 => ((u16::from(a) + u16::from(b)) / 2) as u8,
                4 => paeth(a, b, c),
                _ => return Err(unsupported(format!("Unknown PNG filter {filter}"))),
            });
        }
    }
    let mut pixels = Vec::with_capacity(width as usize * height as usize * 4);
    for y in 0..height as usize {
        let row = &rows[y * stride..(y + 1) * stride];
        for x in 0..width as usize {
            let sample = |channel: usize| -> u8 {
                if depth == 16 {
                    row[(x * channels + channel) * 2]
                } else if depth == 8 {
                    row[x * channels + channel]
                } else {
                    let bit = x * depth as usize;
                    let byte = row[bit / 8];
                    let shift = 8 - depth as usize - bit % 8;
                    (byte >> shift) & ((1u8 << depth) - 1)
                }
            };
            let wide = |channel: usize| -> u16 {
                if depth == 16 {
                    u16::from_be_bytes([
                        row[(x * channels + channel) * 2],
                        row[(x * channels + channel) * 2 + 1],
                    ])
                } else {
                    u16::from(sample(channel))
                }
            };
            let rgba = match color {
                0 => {
                    let value = sample(0);
                    let grey = if depth < 8 {
                        (u32::from(value) * 255 / ((1u32 << depth) - 1)) as u8
                    } else {
                        value
                    };
                    let clear = transparency.len() >= 2
                        && wide(0) == u16::from_be_bytes([transparency[0], transparency[1]]);
                    [grey, grey, grey, if clear { 0 } else { 255 }]
                }
                3 => {
                    let index = sample(0) as usize;
                    let entry = palette
                        .get(index * 3..index * 3 + 3)
                        .ok_or_else(|| unsupported("A palette index is out of range"))?;
                    [
                        entry[0],
                        entry[1],
                        entry[2],
                        transparency.get(index).copied().unwrap_or(255),
                    ]
                }
                4 => [sample(0), sample(0), sample(0), sample(1)],
                2 => {
                    let clear = transparency.len() >= 6
                        && (0..3).all(|c| {
                            wide(c)
                                == u16::from_be_bytes([
                                    transparency[c * 2],
                                    transparency[c * 2 + 1],
                                ])
                        });
                    [sample(0), sample(1), sample(2), if clear { 0 } else { 255 }]
                }
                _ => [sample(0), sample(1), sample(2), sample(3)],
            };
            pixels.extend_from_slice(&rgba);
        }
    }
    Ok(Image {
        width,
        height,
        pixels,
    })
}

fn paeth(a: u8, b: u8, c: u8) -> u8 {
    let p = i16::from(a) + i16::from(b) - i16::from(c);
    let (pa, pb, pc) = (
        (p - i16::from(a)).abs(),
        (p - i16::from(b)).abs(),
        (p - i16::from(c)).abs(),
    );
    if pa <= pb && pa <= pc {
        a
    } else if pb <= pc {
        b
    } else {
        c
    }
}

fn chunk(out: &mut Vec<u8>, kind: &[u8; 4], body: &[u8]) {
    out.extend_from_slice(&(body.len() as u32).to_be_bytes());
    out.extend_from_slice(kind);
    out.extend_from_slice(body);
    let mut crc = flate2::Crc::new();
    crc.update(kind);
    crc.update(body);
    out.extend_from_slice(&crc.sum().to_be_bytes());
}

pub fn encode(image: &Image) -> Result<Vec<u8>> {
    let stride = image.width as usize * 4;
    let mut encoder = ZlibEncoder::new(Vec::new(), Compression::default());
    for row in image.pixels.chunks(stride) {
        // Filter 1 (Sub) suits the flat areas of a difference image.
        let mut line = Vec::with_capacity(stride + 1);
        line.push(1);
        for x in 0..stride {
            line.push(row[x].wrapping_sub(if x >= 4 { row[x - 4] } else { 0 }));
        }
        encoder.write_all(&line)?;
    }
    let mut header = Vec::with_capacity(13);
    header.extend_from_slice(&image.width.to_be_bytes());
    header.extend_from_slice(&image.height.to_be_bytes());
    header.extend_from_slice(&[8, 6, 0, 0, 0]);
    let mut out = SIGNATURE.to_vec();
    chunk(&mut out, b"IHDR", &header);
    chunk(&mut out, b"IDAT", &encoder.finish()?);
    chunk(&mut out, b"IEND", &[]);
    Ok(out)
}

/// A rectangle in pixels, clipped to the image.
#[derive(Debug, Clone, Copy)]
struct Region {
    left: u32,
    top: u32,
    right: u32,
    bottom: u32,
}

fn regions(value: &Value, width: u32, height: u32) -> Vec<Region> {
    value
        .as_array()
        .into_iter()
        .flatten()
        .map(|r| {
            let left = n(r, "x").min(u64::from(width)) as u32;
            let top = n(r, "y").min(u64::from(height)) as u32;
            Region {
                left,
                top,
                right: (u64::from(left) + n(r, "width")).min(u64::from(width)) as u32,
                bottom: (u64::from(top) + n(r, "height")).min(u64::from(height)) as u32,
            }
        })
        .collect()
}

pub struct Comparison {
    pub compared: u64,
    pub changed: u64,
    pub max_difference: u8,
    /// left, top, right, bottom (exclusive) of the changed pixels.
    pub bounds: Option<(u32, u32, u32, u32)>,
    pub diff: Image,
}

/// Compares two images of the same size. A pixel has changed when any
/// channel differs by more than `tolerance`. Ignored regions are neither
/// compared nor counted.
pub fn compare(actual: &Image, baseline: &Image, tolerance: u8, ignore: &Value) -> Comparison {
    let (width, height) = (baseline.width, baseline.height);
    let ignored = regions(ignore, width, height);
    let mut diff = Vec::with_capacity(baseline.pixels.len());
    let (mut compared, mut changed, mut max_difference) = (0u64, 0u64, 0u8);
    let mut bounds: Option<(u32, u32, u32, u32)> = None;
    for y in 0..height {
        for x in 0..width {
            let at = ((y * width + x) * 4) as usize;
            let (old, new) = (&baseline.pixels[at..at + 4], &actual.pixels[at..at + 4]);
            let grey =
                ((u32::from(old[0]) * 3 + u32::from(old[1]) * 6 + u32::from(old[2])) / 10) as u8;
            // The baseline faded towards white, so that changes stand out.
            let faded = 255 - (255 - grey) / 4;
            if ignored
                .iter()
                .any(|r| x >= r.left && x < r.right && y >= r.top && y < r.bottom)
            {
                diff.extend_from_slice(&[faded / 2, faded / 2, faded, 255]);
                continue;
            }
            compared += 1;
            let difference = (0..4).map(|c| old[c].abs_diff(new[c])).max().unwrap();
            max_difference = max_difference.max(difference);
            if difference > tolerance {
                changed += 1;
                bounds = Some(match bounds {
                    None => (x, y, x + 1, y + 1),
                    Some((l, t, r, b)) => (l.min(x), t.min(y), r.max(x + 1), b.max(y + 1)),
                });
                diff.extend_from_slice(&[255, 0, 0, 255]);
            } else {
                diff.extend_from_slice(&[faded, faded, faded, 255]);
            }
        }
    }
    Comparison {
        compared,
        changed,
        max_difference,
        bounds,
        diff: Image {
            width,
            height,
            pixels: diff,
        },
    }
}

fn read_image(file: &Path) -> Result<Image> {
    let metadata = fs::metadata(file)?;
    if metadata.len() > 256 * 1024 * 1024 {
        return Err(unsupported("The image is larger than 256 MiB"));
    }
    decode(&fs::read(file)?).map_err(|e| {
        let message = format!("{}: {}", file.display(), e.message);
        Error::new(&e.code, message)
    })
}

pub fn visual_diff(a: &Value) -> Result {
    let actual_path = path(s(a, "actual"), false, true)?;
    let baseline_path = path(s(a, "baseline"), false, false)?;
    let actual = read_image(&actual_path)?;
    if b(a, "update_baseline") {
        if baseline_path.is_dir() {
            return Err(Error::new(
                "invalid_path",
                "The baseline must be a file, not a directory",
            ));
        }
        if let Some(parent) = baseline_path.parent() {
            fs::create_dir_all(parent)?;
        }
        atomic_write(&baseline_path, &fs::read(&actual_path)?)?;
        return Ok(
            json!({"passed":true,"baseline_updated":true,"baseline":artifact(&baseline_path, now())?,"width":actual.width,"height":actual.height}),
        );
    }
    if !baseline_path.is_file() {
        return Err(Error::new(
            "baseline_missing",
            format!("No baseline at {}", baseline_path.display()),
        )
        .remedy("Look at the actual image; if it is right, call again with update_baseline:true to keep it as the baseline"));
    }
    let baseline = read_image(&baseline_path)?;
    let threshold = a["threshold"].as_f64().unwrap_or(0.0);
    let tolerance = n(a, "tolerance").min(255) as u8;
    if (actual.width, actual.height) != (baseline.width, baseline.height) {
        return Ok(json!({
            "passed":false,"size_mismatch":true,"changed_fraction":1.0,"threshold":threshold,
            "actual_size":{"width":actual.width,"height":actual.height},
            "baseline_size":{"width":baseline.width,"height":baseline.height},"diff":Value::Null,
        }));
    }
    let result = compare(&actual, &baseline, tolerance, &a["ignore"]);
    let fraction = if result.compared == 0 {
        0.0
    } else {
        result.changed as f64 / result.compared as f64
    };
    let diff = if s(a, "diff").is_empty() {
        Value::Null
    } else {
        let target = output_path(s(a, "diff"))?;
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent)?;
        }
        atomic_write(&target, &encode(&result.diff)?)?;
        artifact(&target, now())?
    };
    Ok(json!({
        "passed":fraction <= threshold,
        "size_mismatch":false,
        "changed_pixels":result.changed,
        "compared_pixels":result.compared,
        "changed_fraction":fraction,
        "threshold":threshold,
        "tolerance":tolerance,
        "max_difference":result.max_difference,
        "width":baseline.width,
        "height":baseline.height,
        "changed_bounds":result.bounds.map(|(l,t,r,b)| json!({"x":l,"y":t,"width":r-l,"height":b-t})),
        "diff":diff,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn image(width: u32, height: u32, colour: [u8; 4]) -> Image {
        Image {
            width,
            height,
            pixels: colour.repeat((width * height) as usize),
        }
    }
    fn set(image: &mut Image, x: u32, y: u32, colour: [u8; 4]) {
        let at = ((y * image.width + x) * 4) as usize;
        image.pixels[at..at + 4].copy_from_slice(&colour);
    }
    /// A PNG written by hand: one filter byte per row and raw samples.
    fn handmade(width: u32, height: u32, depth: u8, color: u8, rows: &[Vec<u8>]) -> Vec<u8> {
        let mut raw = Vec::new();
        for row in rows {
            raw.extend_from_slice(row);
        }
        let mut encoder = ZlibEncoder::new(Vec::new(), Compression::fast());
        encoder.write_all(&raw).unwrap();
        let mut header = width.to_be_bytes().to_vec();
        header.extend_from_slice(&height.to_be_bytes());
        header.extend_from_slice(&[depth, color, 0, 0, 0]);
        let mut out = SIGNATURE.to_vec();
        chunk(&mut out, b"IHDR", &header);
        if color == 3 {
            chunk(&mut out, b"PLTE", &[0, 0, 0, 255, 0, 0, 0, 0, 255]);
            chunk(&mut out, b"tRNS", &[255, 128]);
        }
        chunk(&mut out, b"IDAT", &encoder.finish().unwrap());
        chunk(&mut out, b"IEND", &[]);
        out
    }

    #[test]
    fn encoding_and_decoding_round_trip() {
        let mut picture = image(5, 3, [10, 20, 30, 255]);
        set(&mut picture, 4, 2, [200, 100, 0, 128]);
        set(&mut picture, 0, 1, [0, 0, 0, 0]);
        let decoded = decode(&encode(&picture).unwrap()).unwrap();
        assert_eq!(decoded, picture);
    }

    #[test]
    fn filters_palettes_grey_and_sixteen_bits_decode() {
        // RGB, rows filtered None, Sub, Up, Average and Paeth.
        let rows = vec![
            vec![0, 10, 20, 30, 40, 50, 60],
            vec![1, 10, 20, 30, 30, 30, 30],
            vec![2, 0, 0, 0, 0, 0, 0],
            vec![3, 5, 10, 15, 15, 15, 15],
            vec![4, 0, 0, 0, 0, 0, 0],
        ];
        let picture = decode(&handmade(2, 5, 8, 2, &rows)).unwrap();
        assert_eq!(&picture.pixels[0..8], &[10, 20, 30, 255, 40, 50, 60, 255]);
        assert_eq!(&picture.pixels[8..16], &[10, 20, 30, 255, 40, 50, 60, 255]);
        assert_eq!(&picture.pixels[16..24], &[10, 20, 30, 255, 40, 50, 60, 255]);
        // Average: first pixel (0 + 10) / 2 + 5 = 10; second (10 + 40) / 2 + 15 = 40.
        assert_eq!(&picture.pixels[24..32], &[10, 20, 30, 255, 40, 50, 60, 255]);
        assert_eq!(&picture.pixels[32..40], &[10, 20, 30, 255, 40, 50, 60, 255]);
        // Two-bit palette with transparency: indices 0, 1, 2, 1.
        let palette = decode(&handmade(4, 1, 2, 3, &[vec![0, 0b00_01_10_01]])).unwrap();
        assert_eq!(
            palette.pixels,
            [0, 0, 0, 255, 255, 0, 0, 128, 0, 0, 255, 255, 255, 0, 0, 128]
        );
        // One-bit grey: black and white.
        let grey = decode(&handmade(2, 1, 1, 0, &[vec![0, 0b0100_0000]])).unwrap();
        assert_eq!(grey.pixels, [0, 0, 0, 255, 255, 255, 255, 255]);
        // Sixteen-bit RGBA keeps the high byte.
        let wide = decode(&handmade(1, 1, 16, 6, &[vec![0, 1, 2, 3, 4, 5, 6, 7, 8]])).unwrap();
        assert_eq!(wide.pixels, [1, 3, 5, 7]);
        assert_eq!(decode(b"GIF89a").unwrap_err().code, "unsupported_image");
        let mut truncated = handmade(2, 5, 8, 2, &rows);
        truncated.truncate(40);
        assert!(decode(&truncated).is_err());
    }

    #[test]
    fn changes_are_counted_bounded_and_ignored_regions_skipped() {
        let baseline = image(10, 10, [255, 255, 255, 255]);
        let mut actual = baseline.clone();
        set(&mut actual, 2, 3, [0, 0, 0, 255]);
        set(&mut actual, 4, 5, [250, 255, 255, 255]);
        set(&mut actual, 8, 8, [0, 0, 0, 255]);
        let all = compare(&actual, &baseline, 0, &Value::Null);
        assert_eq!(
            (all.compared, all.changed, all.max_difference),
            (100, 3, 255)
        );
        assert_eq!(all.bounds, Some((2, 3, 9, 9)));
        let tolerant = compare(&actual, &baseline, 8, &Value::Null);
        assert_eq!(tolerant.changed, 2);
        // The clock in the corner is ignored; the region is clipped to the image.
        let ignored = compare(
            &actual,
            &baseline,
            8,
            &json!([{"x":7,"y":7,"width":100,"height":100}]),
        );
        assert_eq!((ignored.compared, ignored.changed), (91, 1));
        assert_eq!(ignored.bounds, Some((2, 3, 3, 4)));
        assert_eq!(
            &ignored.diff.pixels[(3 * 10 + 2) * 4..][..4],
            &[255, 0, 0, 255]
        );
        assert_ne!(
            &ignored.diff.pixels[(8 * 10 + 8) * 4..][..4],
            &[255, 0, 0, 255]
        );
    }

    #[test]
    fn files_are_compared_and_baselines_kept_only_when_asked() {
        let root = tempfile::tempdir().unwrap();
        let (actual, baseline, diff) = (
            root.path().join("actual.png"),
            root.path().join("baselines/home.png"),
            root.path().join("out/diff.png"),
        );
        let mut picture = image(4, 4, [0, 0, 255, 255]);
        fs::write(&actual, encode(&picture).unwrap()).unwrap();
        let call = |extra: Value| {
            let mut a = json!({"actual":actual,"baseline":baseline,"tolerance":0,"threshold":0.0});
            for (k, v) in extra.as_object().unwrap() {
                a[k] = v.clone();
            }
            visual_diff(&a)
        };
        assert_eq!(call(json!({})).unwrap_err().code, "baseline_missing");
        let kept = call(json!({"update_baseline":true})).unwrap();
        assert_eq!(kept["baseline_updated"], true);
        assert_eq!(call(json!({})).unwrap()["passed"], true);
        set(&mut picture, 1, 1, [255, 0, 0, 255]);
        fs::write(&actual, encode(&picture).unwrap()).unwrap();
        let failed = call(json!({"diff":diff})).unwrap();
        assert_eq!(failed["passed"], false);
        assert_eq!(failed["changed_pixels"], 1);
        assert_eq!(failed["changed_fraction"], 1.0 / 16.0);
        assert_eq!(
            failed["changed_bounds"],
            json!({"x":1,"y":1,"width":1,"height":1})
        );
        assert_eq!(decode(&fs::read(&diff).unwrap()).unwrap().width, 4);
        assert_eq!(call(json!({"threshold":0.1})).unwrap()["passed"], true);
        fs::write(&actual, encode(&image(5, 4, [0, 0, 0, 255])).unwrap()).unwrap();
        let mismatch = call(json!({})).unwrap();
        assert_eq!(mismatch["size_mismatch"], true);
        assert_eq!(mismatch["passed"], false);
    }
}
