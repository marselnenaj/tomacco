//! One PNG with every capture of a device matrix side by side, each labelled.
//!
//! Captures share one scale, so the sheet keeps their relative sizes: a tablet
//! stays larger than a phone. Labels use a built-in 5 x 7 pixel font, upper
//! case ASCII, so the sheet needs no font on the host.
use crate::visual::Image;

/// The tallest capture is scaled to this height; smaller ones by the same factor.
const CELL_HEIGHT: u32 = 720;
const MAX_WIDTH: u32 = 4096;
const GAP: u32 = 32;
const PADDING: u32 = 32;
/// Glyph pixels are drawn as squares of this size.
const DOT: u32 = 2;
const LINE: u32 = 7 * DOT + 6;
const BACKGROUND: [u8; 3] = [0xf2, 0xf2, 0xf4];
const INK: [u8; 3] = [0x1c, 0x1d, 0x22];

pub struct Cell {
    pub image: Image,
    pub label: Vec<String>,
}

fn glyph(c: char) -> [u8; 7] {
    match c.to_ascii_uppercase() {
        'A' => [14, 17, 17, 31, 17, 17, 17],
        'B' => [30, 17, 17, 30, 17, 17, 30],
        'C' => [14, 17, 16, 16, 16, 17, 14],
        'D' => [30, 17, 17, 17, 17, 17, 30],
        'E' => [31, 16, 16, 30, 16, 16, 31],
        'F' => [31, 16, 16, 30, 16, 16, 16],
        'G' => [14, 17, 16, 23, 17, 17, 15],
        'H' => [17, 17, 17, 31, 17, 17, 17],
        'I' => [14, 4, 4, 4, 4, 4, 14],
        'J' => [7, 2, 2, 2, 2, 18, 12],
        'K' => [17, 18, 20, 24, 20, 18, 17],
        'L' => [16, 16, 16, 16, 16, 16, 31],
        'M' => [17, 27, 21, 21, 17, 17, 17],
        'N' => [17, 17, 25, 21, 19, 17, 17],
        'O' => [14, 17, 17, 17, 17, 17, 14],
        'P' => [30, 17, 17, 30, 16, 16, 16],
        'Q' => [14, 17, 17, 17, 21, 18, 13],
        'R' => [30, 17, 17, 30, 20, 18, 17],
        'S' => [15, 16, 16, 14, 1, 1, 30],
        'T' => [31, 4, 4, 4, 4, 4, 4],
        'U' => [17, 17, 17, 17, 17, 17, 14],
        'V' => [17, 17, 17, 17, 17, 10, 4],
        'W' => [17, 17, 17, 21, 21, 21, 10],
        'X' => [17, 17, 10, 4, 10, 17, 17],
        'Y' => [17, 17, 10, 4, 4, 4, 4],
        'Z' => [31, 1, 2, 4, 8, 16, 31],
        '0' => [14, 17, 19, 21, 25, 17, 14],
        '1' => [4, 12, 4, 4, 4, 4, 14],
        '2' => [14, 17, 1, 2, 4, 8, 31],
        '3' => [31, 2, 4, 2, 1, 17, 14],
        '4' => [2, 6, 10, 18, 31, 2, 2],
        '5' => [31, 16, 30, 1, 1, 17, 14],
        '6' => [6, 8, 16, 30, 17, 17, 14],
        '7' => [31, 1, 2, 4, 8, 8, 8],
        '8' => [14, 17, 17, 14, 17, 17, 14],
        '9' => [14, 17, 17, 15, 1, 2, 12],
        '-' => [0, 0, 0, 31, 0, 0, 0],
        '.' => [0, 0, 0, 0, 0, 12, 12],
        '_' => [0, 0, 0, 0, 0, 0, 31],
        '(' => [2, 4, 8, 8, 8, 4, 2],
        ')' => [8, 4, 2, 2, 2, 4, 8],
        '/' => [1, 2, 2, 4, 8, 8, 16],
        ':' => [0, 12, 12, 0, 12, 12, 0],
        '+' => [0, 4, 4, 31, 4, 4, 0],
        ' ' => [0; 7],
        _ => [14, 17, 1, 2, 4, 0, 4],
    }
}

fn text_width(text: &str) -> u32 {
    (text.chars().count() as u32 * 6).saturating_sub(1) * DOT
}

/// Shortens a line to `width` pixels.
fn fit(text: &str, width: u32) -> String {
    let most = ((width / DOT + 1) / 6) as usize;
    if text.chars().count() <= most {
        return text.to_string();
    }
    let mut kept: String = text.chars().take(most.saturating_sub(2)).collect();
    kept.push_str("..");
    kept
}

fn put(sheet: &mut Image, x: u32, y: u32, rgb: [u8; 3]) {
    if x < sheet.width && y < sheet.height {
        let at = ((y * sheet.width + x) * 4) as usize;
        sheet.pixels[at..at + 3].copy_from_slice(&rgb);
    }
}

fn draw_text(sheet: &mut Image, text: &str, left: u32, top: u32) {
    for (index, c) in text.chars().enumerate() {
        let origin = left + index as u32 * 6 * DOT;
        for (row, bits) in glyph(c).iter().enumerate() {
            for column in 0..5 {
                if bits & (16 >> column) != 0 {
                    for dy in 0..DOT {
                        for dx in 0..DOT {
                            put(
                                sheet,
                                origin + column * DOT + dx,
                                top + row as u32 * DOT + dy,
                                INK,
                            );
                        }
                    }
                }
            }
        }
    }
}

/// Area-averaged scaling to `width` x `height`, composited over the sheet colour.
fn scaled(image: &Image, width: u32, height: u32) -> Vec<[u8; 3]> {
    let (fx, fy) = (
        f64::from(image.width) / f64::from(width),
        f64::from(image.height) / f64::from(height),
    );
    let mut out = Vec::with_capacity((width * height) as usize);
    for y in 0..height {
        let (top, bottom) = (
            (f64::from(y) * fy) as u32,
            ((f64::from(y + 1) * fy).ceil() as u32).clamp(1, image.height),
        );
        for x in 0..width {
            let (left, right) = (
                (f64::from(x) * fx) as u32,
                ((f64::from(x + 1) * fx).ceil() as u32).clamp(1, image.width),
            );
            let mut sum = [0u64; 3];
            let mut count = 0u64;
            for sy in top..bottom.max(top + 1) {
                for sx in left..right.max(left + 1) {
                    let at = ((sy.min(image.height - 1) * image.width + sx.min(image.width - 1))
                        * 4) as usize;
                    let p = &image.pixels[at..at + 4];
                    let alpha = u64::from(p[3]);
                    for c in 0..3 {
                        sum[c] += (u64::from(p[c]) * alpha
                            + u64::from(BACKGROUND[c]) * (255 - alpha))
                            / 255;
                    }
                    count += 1;
                }
            }
            out.push([
                (sum[0] / count) as u8,
                (sum[1] / count) as u8,
                (sum[2] / count) as u8,
            ]);
        }
    }
    out
}

/// Lays the cells out in rows, left to right, wrapping at a sheet width of 4096 pixels.
pub fn compose(cells: &[Cell]) -> Image {
    let tallest = cells.iter().map(|c| c.image.height).max().unwrap_or(1);
    let factor = (f64::from(CELL_HEIGHT) / f64::from(tallest)).min(1.0);
    let lines = cells.iter().map(|c| c.label.len()).max().unwrap_or(0) as u32;
    let label_height = if lines == 0 { 0 } else { 12 + lines * LINE };
    let sizes: Vec<(u32, u32)> = cells
        .iter()
        .map(|c| {
            (
                ((f64::from(c.image.width) * factor).round() as u32).max(1),
                ((f64::from(c.image.height) * factor).round() as u32).max(1),
            )
        })
        .collect();
    // Each cell is at least as wide as a short label.
    let widths: Vec<u32> = sizes.iter().map(|(w, _)| (*w).max(200)).collect();
    let mut rows: Vec<Vec<usize>> = vec![Vec::new()];
    let mut used = PADDING;
    for (index, width) in widths.iter().enumerate() {
        let row = rows.last_mut().unwrap();
        if !row.is_empty() && used + width + PADDING > MAX_WIDTH {
            rows.push(vec![index]);
            used = PADDING + width + GAP;
        } else {
            row.push(index);
            used += width + GAP;
        }
    }
    let row_height =
        |row: &Vec<usize>| row.iter().map(|&i| sizes[i].1).max().unwrap_or(0) + label_height;
    let sheet_width = rows
        .iter()
        .map(|row| {
            PADDING * 2
                + row.iter().map(|&i| widths[i]).sum::<u32>()
                + GAP * (row.len() as u32).saturating_sub(1)
        })
        .max()
        .unwrap_or(PADDING * 2);
    let sheet_height = PADDING * 2
        + rows.iter().map(row_height).sum::<u32>()
        + GAP * (rows.len() as u32).saturating_sub(1);
    let mut sheet = Image {
        width: sheet_width,
        height: sheet_height,
        pixels: [BACKGROUND[0], BACKGROUND[1], BACKGROUND[2], 255]
            .repeat((sheet_width * sheet_height) as usize),
    };
    let mut top = PADDING;
    for row in &rows {
        let height = row_height(row) - label_height;
        let mut left = PADDING;
        for &index in row {
            let (w, h) = sizes[index];
            let x0 = left + (widths[index] - w) / 2;
            // Captures stand on a common baseline above their labels.
            let y0 = top + height - h;
            for (at, rgb) in scaled(&cells[index].image, w, h).into_iter().enumerate() {
                put(&mut sheet, x0 + at as u32 % w, y0 + at as u32 / w, rgb);
            }
            for (line, text) in cells[index].label.iter().enumerate() {
                let text = fit(text, widths[index]);
                let x = left + (widths[index].saturating_sub(text_width(&text))) / 2;
                draw_text(&mut sheet, &text, x, top + height + 12 + line as u32 * LINE);
            }
            left += widths[index] + GAP;
        }
        top += row_height(row) + GAP;
    }
    sheet
}

#[cfg(test)]
mod tests {
    use super::*;

    fn solid(width: u32, height: u32, rgba: [u8; 4]) -> Image {
        Image {
            width,
            height,
            pixels: rgba.repeat((width * height) as usize),
        }
    }
    fn at(image: &Image, x: u32, y: u32) -> [u8; 4] {
        let i = ((y * image.width + x) * 4) as usize;
        image.pixels[i..i + 4].try_into().unwrap()
    }

    #[test]
    fn captures_keep_their_relative_size_and_get_labels() {
        let phone = solid(800, 1440, [255, 0, 0, 255]);
        let tablet = solid(1600, 1200, [0, 0, 255, 255]);
        let sheet = compose(&[
            Cell {
                image: phone,
                label: vec!["iPhone 18 Pro".into(), "portrait".into()],
            },
            Cell {
                image: tablet,
                label: vec!["Tablet".into(), "landscape".into()],
            },
        ]);
        // Tallest at 720 pixels: the phone 400 x 720, the tablet 800 x 600, one row.
        let label = 12 + 2 * LINE;
        assert_eq!(sheet.width, PADDING * 2 + 400 + GAP + 800);
        assert_eq!(sheet.height, PADDING * 2 + 720 + label);
        assert_eq!(at(&sheet, PADDING + 200, PADDING + 360), [255, 0, 0, 255]);
        // The tablet stands on the same baseline: 120 pixels of background above it.
        let tablet_x = PADDING + 400 + GAP + 400;
        assert_eq!(at(&sheet, tablet_x, PADDING + 60)[..3], BACKGROUND);
        assert_eq!(at(&sheet, tablet_x, PADDING + 720 - 1), [0, 0, 255, 255]);
        // Ink below the phone: its label.
        let label_rows = (PADDING + 720..sheet.height)
            .flat_map(|y| (PADDING..PADDING + 400).map(move |x| (x, y)))
            .filter(|&(x, y)| at(&sheet, x, y)[..3] == INK)
            .count();
        assert!(label_rows > 50, "{label_rows}");
    }

    #[test]
    fn small_captures_are_not_enlarged_and_rows_wrap() {
        let cells: Vec<Cell> = (0..12)
            .map(|_| Cell {
                image: solid(600, 300, [0, 128, 0, 128]),
                label: vec!["x".into()],
            })
            .collect();
        let sheet = compose(&cells);
        assert!(sheet.width <= MAX_WIDTH);
        // Six to a row at 600 + 32 pixels: two rows of 300-pixel captures.
        assert_eq!(sheet.width, PADDING * 2 + 6 * 600 + 5 * GAP);
        assert_eq!(sheet.height, PADDING * 2 + 2 * (300 + 12 + LINE) + GAP);
        // Half-transparent green over the sheet colour.
        let blended = at(&sheet, PADDING + 10, PADDING + 10);
        assert_eq!(blended[1], ((128u32 * 128 + 0xf2 * 127) / 255) as u8);
    }

    #[test]
    fn long_labels_are_shortened_to_the_cell() {
        assert_eq!(fit("short", 400), "short");
        let shortened = fit(&"a".repeat(200), 120);
        assert!(text_width(&shortened) <= 120, "{shortened}");
        assert!(shortened.ends_with(".."));
        assert_eq!(glyph('a'), glyph('A'));
        assert_eq!(glyph('é'), glyph('?'));
    }
}
