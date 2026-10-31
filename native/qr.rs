//! A QR code encoder for short text: byte mode, error correction level M,
//! versions 1 to 9 (up to 182 bytes). Enough for the code Android's wireless
//! debugging scans. Follows ISO/IEC 18004.

/// A square of modules; true is dark.
pub struct Code {
    pub size: usize,
    modules: Vec<bool>,
    function: Vec<bool>,
}

// Level M, by version 1..=9 (index 0 unused).
const ECC_PER_BLOCK: [usize; 10] = [0, 10, 16, 26, 18, 24, 16, 18, 22, 22];
const BLOCKS: [usize; 10] = [0, 1, 1, 1, 2, 2, 4, 4, 4, 5];

fn raw_modules(version: usize) -> usize {
    let mut result = (16 * version + 128) * version + 64;
    if version >= 2 {
        let align = version / 7 + 2;
        result -= (25 * align - 10) * align - 55;
        if version >= 7 {
            result -= 36;
        }
    }
    result
}

fn data_codewords(version: usize) -> usize {
    raw_modules(version) / 8 - ECC_PER_BLOCK[version] * BLOCKS[version]
}

fn gf_multiply(x: u8, y: u8) -> u8 {
    let mut z: u16 = 0;
    for i in (0..8).rev() {
        z = (z << 1) ^ ((z >> 7) * 0x11D);
        z ^= ((y as u16 >> i) & 1) * x as u16;
    }
    z as u8
}

/// The Reed-Solomon generator polynomial of this degree, highest term
/// first and its leading 1 left out.
fn rs_divisor(degree: usize) -> Vec<u8> {
    let mut result = vec![0u8; degree];
    result[degree - 1] = 1;
    let mut root: u8 = 1;
    for _ in 0..degree {
        for j in 0..degree {
            result[j] = gf_multiply(result[j], root);
            if j + 1 < degree {
                result[j] ^= result[j + 1];
            }
        }
        root = gf_multiply(root, 0x02);
    }
    result
}

/// The error correction codewords of one block.
pub fn rs_remainder(data: &[u8], degree: usize) -> Vec<u8> {
    let divisor = rs_divisor(degree);
    let mut result = vec![0u8; degree];
    for &b in data {
        let factor = b ^ result[0];
        result.remove(0);
        result.push(0);
        for (r, &d) in result.iter_mut().zip(&divisor) {
            *r ^= gf_multiply(d, factor);
        }
    }
    result
}

/// The data codewords: mode, length, the bytes, terminator and padding.
fn codewords(text: &[u8], version: usize) -> Vec<u8> {
    let capacity = data_codewords(version);
    let mut bits: Vec<bool> = Vec::new();
    let mut push = |value: usize, count: usize| {
        for i in (0..count).rev() {
            bits.push((value >> i) & 1 == 1);
        }
    };
    push(0b0100, 4);
    push(text.len(), 8);
    for &b in text {
        push(b as usize, 8);
    }
    let room = capacity * 8;
    let terminator = (room - bits.len()).min(4);
    bits.extend(std::iter::repeat_n(false, terminator));
    while !bits.len().is_multiple_of(8) {
        bits.push(false);
    }
    let mut bytes: Vec<u8> = bits
        .chunks(8)
        .map(|c| c.iter().fold(0u8, |a, &b| (a << 1) | b as u8))
        .collect();
    for pad in [0xEC, 0x11].iter().cycle() {
        if bytes.len() >= capacity {
            break;
        }
        bytes.push(*pad);
    }
    bytes
}

/// Splits the data into blocks, adds each block's error correction and
/// interleaves them.
fn interleave(data: &[u8], version: usize) -> Vec<u8> {
    let blocks = BLOCKS[version];
    let ecc = ECC_PER_BLOCK[version];
    let raw = raw_modules(version) / 8;
    let short = blocks - raw % blocks;
    let short_len = raw / blocks;
    let mut parts: Vec<(Vec<u8>, Vec<u8>)> = Vec::new();
    let mut at = 0;
    for i in 0..blocks {
        let length = short_len - ecc + usize::from(i >= short);
        let block = data[at..at + length].to_vec();
        at += length;
        let remainder = rs_remainder(&block, ecc);
        parts.push((block, remainder));
    }
    let mut result = Vec::with_capacity(raw);
    for i in 0..=short_len - ecc {
        for (block, _) in &parts {
            if let Some(&b) = block.get(i) {
                result.push(b);
            }
        }
    }
    for i in 0..ecc {
        for (_, remainder) in &parts {
            result.push(remainder[i]);
        }
    }
    result
}

fn alignment_positions(version: usize) -> Vec<usize> {
    if version == 1 {
        return Vec::new();
    }
    let count = version / 7 + 2;
    let size = version * 4 + 17;
    let step = (version * 4 + count * 2 + 1) / (count * 2 - 2) * 2;
    let mut result = vec![6];
    let mut position = size - 7;
    for _ in 0..count - 1 {
        result.insert(1, position);
        position -= step;
    }
    result
}

impl Code {
    fn set(&mut self, x: usize, y: usize, dark: bool) {
        self.modules[y * self.size + x] = dark;
        self.function[y * self.size + x] = true;
    }
    pub fn dark(&self, x: usize, y: usize) -> bool {
        self.modules[y * self.size + x]
    }

    fn finder(&mut self, cx: isize, cy: isize) {
        for dy in -4isize..=4 {
            for dx in -4isize..=4 {
                let (x, y) = (cx + dx, cy + dy);
                if x < 0 || y < 0 || x >= self.size as isize || y >= self.size as isize {
                    continue;
                }
                let distance = dx.abs().max(dy.abs());
                self.set(x as usize, y as usize, distance != 2 && distance != 4);
            }
        }
    }

    fn format(&mut self, mask: usize) {
        // Level M is 0b00.
        let data = mask;
        let mut remainder = data;
        for _ in 0..10 {
            remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537);
        }
        let bits = ((data << 10) | remainder) ^ 0x5412;
        let bit = |i: usize| (bits >> i) & 1 == 1;
        for i in 0..6 {
            self.set(8, i, bit(i));
        }
        self.set(8, 7, bit(6));
        self.set(8, 8, bit(7));
        self.set(7, 8, bit(8));
        for i in 9..15 {
            self.set(14 - i, 8, bit(i));
        }
        let size = self.size;
        for i in 0..8 {
            self.set(size - 1 - i, 8, bit(i));
        }
        for i in 8..15 {
            self.set(8, size - 15 + i, bit(i));
        }
        self.set(8, size - 8, true);
    }

    fn version_bits(&mut self, version: usize) {
        if version < 7 {
            return;
        }
        let mut remainder = version;
        for _ in 0..12 {
            remainder = (remainder << 1) ^ ((remainder >> 11) * 0x1F25);
        }
        let bits = (version << 12) | remainder;
        for i in 0..18 {
            let dark = (bits >> i) & 1 == 1;
            let (a, b) = (self.size - 11 + i % 3, i / 3);
            self.set(a, b, dark);
            self.set(b, a, dark);
        }
    }

    fn function_patterns(&mut self, version: usize) {
        let size = self.size;
        for i in 0..size {
            self.set(6, i, i % 2 == 0);
            self.set(i, 6, i % 2 == 0);
        }
        self.finder(3, 3);
        self.finder(size as isize - 4, 3);
        self.finder(3, size as isize - 4);
        let positions = alignment_positions(version);
        let last = positions.len().saturating_sub(1);
        for (i, &x) in positions.iter().enumerate() {
            for (j, &y) in positions.iter().enumerate() {
                if (i == 0 && j == 0) || (i == 0 && j == last) || (i == last && j == 0) {
                    continue;
                }
                for dy in -2isize..=2 {
                    for dx in -2isize..=2 {
                        let dark = dx.abs().max(dy.abs()) != 1;
                        self.set((x as isize + dx) as usize, (y as isize + dy) as usize, dark);
                    }
                }
            }
        }
        // Reserved now, written for each mask.
        self.format(0);
        self.version_bits(version);
    }

    fn place(&mut self, data: &[u8]) {
        let size = self.size as isize;
        let mut i = 0;
        let mut right = size - 1;
        while right >= 1 {
            if right == 6 {
                right = 5;
            }
            for vertical in 0..size {
                for j in 0..2 {
                    let x = (right - j) as usize;
                    let upward = (right + 1) & 2 == 0;
                    let y = (if upward {
                        size - 1 - vertical
                    } else {
                        vertical
                    }) as usize;
                    if !self.function[y * self.size + x] && i < data.len() * 8 {
                        self.modules[y * self.size + x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1;
                        i += 1;
                    }
                }
            }
            right -= 2;
        }
    }

    fn apply_mask(&mut self, mask: usize) {
        for y in 0..self.size {
            for x in 0..self.size {
                let invert = match mask {
                    0 => (x + y) % 2 == 0,
                    1 => y % 2 == 0,
                    2 => x % 3 == 0,
                    3 => (x + y) % 3 == 0,
                    4 => (x / 3 + y / 2) % 2 == 0,
                    5 => x * y % 2 + x * y % 3 == 0,
                    6 => (x * y % 2 + x * y % 3) % 2 == 0,
                    _ => ((x + y) % 2 + x * y % 3) % 2 == 0,
                };
                if invert && !self.function[y * self.size + x] {
                    self.modules[y * self.size + x] ^= true;
                }
            }
        }
    }

    fn penalty(&self) -> usize {
        let size = self.size;
        let mut total = 0;
        let line = |get: &dyn Fn(usize) -> bool| -> usize {
            let mut score = 0;
            let mut run = 1;
            for i in 1..size {
                if get(i) == get(i - 1) {
                    run += 1;
                    if run == 5 {
                        score += 3;
                    } else if run > 5 {
                        score += 1;
                    }
                } else {
                    run = 1;
                }
            }
            // Finder-like 1:1:3:1:1 with four light modules on one side.
            let pattern = [true, false, true, true, true, false, true];
            for start in 0..size.saturating_sub(6) {
                if (0..7).all(|k| get(start + k) == pattern[k]) {
                    let light = |range: std::ops::Range<isize>| {
                        range
                            .into_iter()
                            .all(|k| k < 0 || k >= size as isize || !get(k as usize))
                    };
                    let s = start as isize;
                    if light(s - 4..s) || light(s + 7..s + 11) {
                        score += 40;
                    }
                }
            }
            score
        };
        for y in 0..size {
            total += line(&|x| self.dark(x, y));
        }
        for x in 0..size {
            total += line(&|y| self.dark(x, y));
        }
        for y in 0..size - 1 {
            for x in 0..size - 1 {
                let c = self.dark(x, y);
                if c == self.dark(x + 1, y)
                    && c == self.dark(x, y + 1)
                    && c == self.dark(x + 1, y + 1)
                {
                    total += 3;
                }
            }
        }
        let dark = self.modules.iter().filter(|&&m| m).count();
        let all = size * size;
        let k = (dark * 20)
            .abs_diff(all * 10)
            .div_ceil(all)
            .saturating_sub(1);
        total + k * 10
    }
}

/// The QR code of this text, or None when it is longer than 182 bytes.
pub fn encode(text: &str) -> Option<Code> {
    let bytes = text.as_bytes();
    let version = (1..=9).find(|&v| data_codewords(v) >= bytes.len() + 2)?;
    let data = interleave(&codewords(bytes, version), version);
    let size = version * 4 + 17;
    let mut base = Code {
        size,
        modules: vec![false; size * size],
        function: vec![false; size * size],
    };
    base.function_patterns(version);
    base.place(&data);
    (0..8)
        .map(|mask| {
            let mut code = Code {
                size,
                modules: base.modules.clone(),
                function: base.function.clone(),
            };
            code.apply_mask(mask);
            code.format(mask);
            (code.penalty(), code)
        })
        .min_by_key(|(penalty, _)| *penalty)
        .map(|(_, code)| code)
}

impl Code {
    /// One string per row, `1` dark and `0` light, without the quiet zone.
    pub fn rows(&self) -> Vec<String> {
        (0..self.size)
            .map(|y| {
                (0..self.size)
                    .map(|x| if self.dark(x, y) { '1' } else { '0' })
                    .collect()
            })
            .collect()
    }
    /// An SVG drawing with a four-module quiet zone.
    pub fn svg(&self) -> String {
        let full = self.size + 8;
        let mut path = String::new();
        for y in 0..self.size {
            for x in 0..self.size {
                if self.dark(x, y) {
                    path.push_str(&format!("M{},{}h1v1h-1z", x + 4, y + 4));
                }
            }
        }
        format!(
            "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 {full} {full}\" shape-rendering=\"crispEdges\"><rect width=\"{full}\" height=\"{full}\" fill=\"#fff\"/><path d=\"{path}\" fill=\"#000\"/></svg>\n"
        )
    }
    /// Grey pixels, `scale` per module, with the quiet zone; light is 255.
    pub fn pixels(&self, scale: usize) -> (Vec<u8>, usize) {
        let full = (self.size + 8) * scale;
        let mut out = vec![255u8; full * full];
        for y in 0..full {
            for x in 0..full {
                let (mx, my) = (x / scale, y / scale);
                if (4..self.size + 4).contains(&mx)
                    && (4..self.size + 4).contains(&my)
                    && self.dark(mx - 4, my - 4)
                {
                    out[y * full + x] = 0;
                }
            }
        }
        (out, full)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reed_solomon_matches_the_worked_example() {
        // HELLO WORLD at 1-M, from the standard's worked example.
        let data = [
            32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17,
        ];
        assert_eq!(
            rs_remainder(&data, 10),
            [196, 35, 39, 119, 235, 215, 231, 226, 93, 23]
        );
    }

    #[test]
    fn capacities_are_those_of_level_m() {
        let capacities: Vec<usize> = (1..=9).map(data_codewords).collect();
        assert_eq!(capacities, [16, 28, 44, 64, 86, 108, 124, 154, 182]);
        assert_eq!(alignment_positions(7), [6, 22, 38]);
        assert!(encode(&"x".repeat(181)).is_none());
    }

    #[test]
    fn a_code_has_its_finders_timing_and_format() {
        let code = encode("WIFI:T:ADB;S:tomacco-abc123;P:k3q9x2m7p1;;").unwrap();
        assert_eq!(code.size, 29); // version 3
        let rows = code.rows();
        assert_eq!(&rows[0][..7], "1111111");
        assert_eq!(&rows[1][..7], "1000001");
        assert_eq!(&rows[0][code.size - 7..], "1111111");
        assert_eq!(&rows[code.size - 1][..7], "1111111");
        assert_eq!(&rows[6][8..code.size - 8], "1010101010101");
        // The dark module.
        assert!(code.dark(8, code.size - 8));
        // Both copies of the format information agree.
        let first: Vec<bool> = (0..6)
            .map(|i| code.dark(8, i))
            .chain([code.dark(8, 7), code.dark(8, 8), code.dark(7, 8)])
            .chain((9..15).map(|i| code.dark(14 - i, 8)))
            .collect();
        let second: Vec<bool> = (0..8)
            .map(|i| code.dark(code.size - 1 - i, 8))
            .chain((8..15).map(|i| code.dark(8, code.size - 15 + i)))
            .collect();
        assert_eq!(first, second);
        let svg = code.svg();
        assert!(svg.starts_with("<svg") && svg.contains("viewBox=\"0 0 37 37\""));
        let (pixels, width) = code.pixels(2);
        assert_eq!((width, pixels.len()), (74, 74 * 74));
        assert_eq!(pixels[8 * 74 + 8], 0);
        assert_eq!(pixels[0], 255);
    }
}
