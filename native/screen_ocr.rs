//! Reading text off a screenshot with Tesseract, for screens that have no UI
//! hierarchy: the virtual iPhone, games, WebViews and canvases.
//!
//! This is recognition of pixels, not knowledge of controls. A word can be
//! misread or missed (small, low-contrast or decorative text, and text on a
//! busy picture, are the usual cases), and an icon without a label is
//! invisible to it. Every word carries Tesseract's confidence so a caller can
//! judge for itself.
use crate::core::*;
use crate::visual::{self, Image};
use serde_json::{Value, json};
use std::path::Path;

#[derive(Debug, Clone, PartialEq)]
pub struct Word {
    pub text: String,
    /// Tesseract's confidence, 0 to 100.
    pub confidence: f64,
    pub left: i64,
    pub top: i64,
    pub width: i64,
    pub height: i64,
}

impl Word {
    fn right(&self) -> i64 {
        self.left + self.width
    }
    fn bottom(&self) -> i64 {
        self.top + self.height
    }
    fn area(&self) -> i64 {
        self.width.max(0) * self.height.max(0)
    }
}

/// A run of words on one line of the screen.
#[derive(Debug, Clone)]
pub struct Line {
    pub words: Vec<Word>,
}

impl Line {
    pub fn text(&self) -> String {
        self.words
            .iter()
            .map(|w| w.text.as_str())
            .collect::<Vec<_>>()
            .join(" ")
    }
    pub fn confidence(&self) -> f64 {
        words_confidence(&self.words)
    }
}

fn words_confidence(words: &[Word]) -> f64 {
    if words.is_empty() {
        return 0.0;
    }
    words.iter().map(|w| w.confidence).sum::<f64>() / words.len() as f64
}

/// What Tesseract's TSV output has: words (level 5) with a confidence of 0 or
/// more and some text.
pub fn parse_tsv(tsv: &str) -> Vec<Word> {
    tsv.lines()
        .filter_map(|row| {
            let f: Vec<&str> = row.split('\t').collect();
            if f.len() < 12 || f[0] != "5" {
                return None;
            }
            let confidence: f64 = f[10].trim().parse().ok()?;
            let text = f[11].trim();
            if confidence < 0.0 || text.is_empty() {
                return None;
            }
            let number = |i: usize| f[i].trim().parse::<i64>().ok();
            Some(Word {
                text: text.to_string(),
                confidence,
                left: number(6)?,
                top: number(7)?,
                width: number(8)?,
                height: number(9)?,
            })
        })
        .collect()
}

/// Words by their position into lines: words whose middles are at about the
/// same height and that are no further apart than a character or two. Two
/// buttons side by side stay two lines.
pub fn lines(words: &[Word]) -> Vec<Line> {
    let mut order: Vec<&Word> = words.iter().collect();
    order.sort_by_key(|w| (w.left, w.top));
    let mut found: Vec<Line> = Vec::new();
    for word in order {
        let middle = word.top + word.height / 2;
        let placed = found.iter_mut().find(|line| {
            let height = line
                .words
                .iter()
                .map(|w| w.height)
                .max()
                .unwrap_or(1)
                .max(word.height)
                .max(1);
            let centre = line.words.iter().map(|w| w.top + w.height / 2).sum::<i64>()
                / line.words.len() as i64;
            let left = line.words.iter().map(|w| w.left).min().unwrap_or(0);
            let right = line.words.iter().map(Word::right).max().unwrap_or(0);
            let gap = (word.left - right).max(left - word.right()).max(0);
            (centre - middle).abs() * 2 <= height && gap <= height * 6 / 5
        });
        match placed {
            Some(line) => line.words.push(word.clone()),
            None => found.push(Line {
                words: vec![word.clone()],
            }),
        }
    }
    for line in &mut found {
        line.words.sort_by_key(|w| w.left);
    }
    found.sort_by_key(|l| {
        let w = &l.words[0];
        (w.top + w.height / 2, w.left)
    });
    found
}

fn overlap(a: &Word, b: &Word) -> f64 {
    let width = (a.right().min(b.right()) - a.left.max(b.left)).max(0);
    let height = (a.bottom().min(b.bottom()) - a.top.max(b.top)).max(0);
    let shared = (width * height) as f64;
    let smaller = a.area().min(b.area()).max(1) as f64;
    shared / smaller
}

/// Two readings of one screen (as it is and with its colours inverted) in one
/// list: a word seen in both counts once, with the better confidence.
pub fn merge(first: Vec<Word>, second: Vec<Word>) -> Vec<Word> {
    let mut merged = first;
    for word in second {
        match merged.iter_mut().find(|m| overlap(m, &word) > 0.5) {
            Some(existing) => {
                if word.confidence > existing.confidence {
                    *existing = word;
                }
            }
            None => merged.push(word),
        }
    }
    merged
}

// ---- Matching ------------------------------------------------------------

/// A word as compared: lower case, without punctuation at its ends.
fn token(text: &str) -> String {
    text.trim_matches(|c: char| !c.is_alphanumeric())
        .to_lowercase()
}

fn distance(a: &str, b: &str) -> usize {
    let (a, b): (Vec<char>, Vec<char>) = (a.chars().collect(), b.chars().collect());
    let mut row: Vec<usize> = (0..=b.len()).collect();
    for (i, ca) in a.iter().enumerate() {
        let mut previous = row[0];
        row[0] = i + 1;
        for (j, cb) in b.iter().enumerate() {
            let held = row[j + 1];
            row[j + 1] = (previous + usize::from(ca != cb))
                .min(row[j] + 1)
                .min(row[j + 1] + 1);
            previous = held;
        }
    }
    row[b.len()]
}

/// Whether a word read from the screen is the wanted one. With `fuzzy`, a
/// word of five or more letters may differ by one letter.
fn same_token(read: &str, wanted: &str, fuzzy: bool) -> bool {
    read == wanted || (fuzzy && wanted.chars().count() >= 5 && distance(read, wanted) <= 1)
}

#[derive(Debug, Clone, PartialEq)]
pub struct Found {
    pub text: String,
    pub confidence: f64,
    pub left: i64,
    pub top: i64,
    pub right: i64,
    pub bottom: i64,
    pub words: usize,
    /// Matched with a one-letter difference.
    pub fuzzy: bool,
}

impl Found {
    pub fn value(&self) -> Value {
        json!({"text":self.text,"confidence":(self.confidence * 10.0).round() / 10.0,"source":"ocr","words":self.words,"fuzzy":self.fuzzy,
            "rect":{"left":self.left,"top":self.top,"right":self.right,"bottom":self.bottom,
                "x":(self.left + self.right) / 2,"y":(self.top + self.bottom) / 2,
                "width":self.right - self.left,"height":self.bottom - self.top}})
    }
}

fn found(words: &[Word], fuzzy: bool) -> Found {
    Found {
        text: words
            .iter()
            .map(|w| w.text.as_str())
            .collect::<Vec<_>>()
            .join(" "),
        confidence: words_confidence(words),
        left: words.iter().map(|w| w.left).min().unwrap(),
        top: words.iter().map(|w| w.top).min().unwrap(),
        right: words.iter().map(Word::right).max().unwrap(),
        bottom: words.iter().map(Word::bottom).max().unwrap(),
        words: words.len(),
        fuzzy,
    }
}

/// Where the words of `text` stand one after another in a line, as whole
/// words and in any letter case. Tesseract often splits and joins words
/// differently from a person, so a phrase is matched word for word only.
pub fn find_phrase(lines: &[Line], text: &str, fuzzy: bool) -> Vec<Found> {
    let wanted: Vec<String> = text
        .split_whitespace()
        .map(token)
        .filter(|t| !t.is_empty())
        .collect();
    let mut result = Vec::new();
    if wanted.is_empty() {
        return result;
    }
    let squeezed = wanted.concat();
    for line in lines {
        let read: Vec<String> = line.words.iter().map(|w| token(&w.text)).collect();
        let mut at = 0;
        while at < read.len() {
            let exact = at + wanted.len() <= read.len()
                && (0..wanted.len()).all(|i| read[at + i] == wanted[i]);
            let close = !exact
                && fuzzy
                && at + wanted.len() <= read.len()
                && (0..wanted.len()).all(|i| same_token(&read[at + i], &wanted[i], true));
            // Tesseract often drops or adds a space: "Sign in" read as "Signin".
            let joined = if exact || close {
                None
            } else {
                (1..=(wanted.len() + 1).min(read.len() - at))
                    .find(|k| same_token(&read[at..at + k].concat(), &squeezed, fuzzy))
            };
            if exact || close {
                result.push(found(&line.words[at..at + wanted.len()], close));
                at += wanted.len();
            } else if let Some(k) = joined {
                let loose = read[at..at + k].concat() != squeezed;
                result.push(found(&line.words[at..at + k], loose));
                at += k;
            } else {
                at += 1;
            }
        }
    }
    result
}

/// Where `needle` stands inside a line's text, as part of a word or across
/// words; the box covers the words it touches.
pub fn find_contained(lines: &[Line], needle: &str, fuzzy: bool) -> Vec<Found> {
    let needle: String = needle
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase();
    let mut result = Vec::new();
    if needle.is_empty() {
        return result;
    }
    for line in lines {
        let mut text = String::new();
        let mut spans = Vec::new();
        for word in &line.words {
            if !text.is_empty() {
                text.push(' ');
            }
            let start = text.len();
            text.push_str(&word.text.to_lowercase());
            spans.push((start, text.len()));
        }
        // The same text without spaces, for a space Tesseract dropped or added.
        let (squeezed_text, squeezed_needle) = (text.replace(' ', ""), needle.replace(' ', ""));
        let mut from = 0;
        while let Some(offset) = text[from..].find(&needle) {
            let (start, end) = (from + offset, from + offset + needle.len());
            let touched: Vec<Word> = line
                .words
                .iter()
                .zip(&spans)
                .filter(|(_, (s, e))| *s < end && *e > start)
                .map(|(w, _)| w.clone())
                .collect();
            if !touched.is_empty() {
                result.push(found(&touched, false));
            }
            from = end;
        }
        if !squeezed_needle.is_empty()
            && squeezed_text.contains(&squeezed_needle)
            && !text.contains(&needle)
        {
            // Map the match in the squeezed text back to the words it touches.
            let start = squeezed_text.find(&squeezed_needle).unwrap();
            let end = start + squeezed_needle.len();
            let mut offset = 0;
            let mut touched = Vec::new();
            for word in &line.words {
                let length = word.text.to_lowercase().replace(' ', "").len();
                if offset < end && offset + length > start {
                    touched.push(word.clone());
                }
                offset += length;
            }
            if !touched.is_empty() {
                result.push(found(&touched, false));
            }
        }
        if result.is_empty() && fuzzy {
            // One misread letter in a single-word needle.
            if !needle.contains(' ') && needle.chars().count() >= 5 {
                for word in &line.words {
                    if distance(&token(&word.text), &needle) <= 1 {
                        result.push(found(std::slice::from_ref(word), true));
                    }
                }
            }
        }
    }
    result
}

/// The matches of a selector with `text` (whole words) or `text_contains`,
/// top to bottom, left to right.
pub fn select(lines: &[Line], selector: &Value, fuzzy: bool) -> Vec<Found> {
    let mut matches = match (
        selector["text"].as_str(),
        selector["text_contains"].as_str(),
    ) {
        (Some(text), _) => find_phrase(lines, text, fuzzy),
        (None, Some(part)) => find_contained(lines, part, fuzzy),
        _ => Vec::new(),
    };
    matches.sort_by_key(|m| ((m.top + m.bottom) / 2, m.left));
    if let Some(index) = selector["index"].as_u64() {
        matches = matches
            .into_iter()
            .nth(index as usize)
            .into_iter()
            .collect();
    }
    matches
}

/// Selector properties OCR can answer: text read off the pixels.
pub fn supports(selector: &Value) -> bool {
    selector.as_object().is_some_and(|object| {
        object
            .keys()
            .all(|k| matches!(k.as_str(), "text" | "text_contains" | "index"))
            && (object.contains_key("text") || object.contains_key("text_contains"))
    })
}

// ---- Running Tesseract -----------------------------------------------------

/// Something that reads a PNG and answers Tesseract's TSV. Tests replace it.
pub trait Recognizer {
    fn tsv(&mut self, image: &Path, language: &str) -> Result<String>;
}

pub struct Tesseract;

/// The tesseract executable: `TOMACCO_TESSERACT`, else the first on PATH.
pub fn binary() -> Option<std::path::PathBuf> {
    if let Some(chosen) = std::env::var_os("TOMACCO_TESSERACT").filter(|v| !v.is_empty()) {
        return Some(chosen.into());
    }
    crate::toolchain::which("tesseract")
}

pub fn unavailable() -> Error {
    Error::new(
        "ocr_unavailable",
        "Reading text off a screenshot needs tesseract, and it is not installed",
    )
    .remedy("Install tesseract and its English data (Arch: pacman -S tesseract tesseract-data-eng; Debian and Ubuntu: apt install tesseract-ocr), or set TOMACCO_TESSERACT to the executable")
}

impl Recognizer for Tesseract {
    fn tsv(&mut self, image: &Path, language: &str) -> Result<String> {
        let binary = binary().ok_or_else(unavailable)?;
        let command = vec![
            binary.to_string_lossy().into_owned(),
            image.to_string_lossy().into_owned(),
            "stdout".into(),
            "-l".into(),
            language.into(),
            "--psm".into(),
            "11".into(),
            "tsv".into(),
        ];
        let done = crate::toolchain::run(&command, 90, None, false)?;
        if done["exit_code"] != 0 {
            let stderr = s(&done, "stderr");
            let first = stderr.lines().next().unwrap_or("");
            let message = format!("tesseract could not read the screenshot: {first}");
            let missing = stderr.contains("Failed loading language")
                || stderr.contains("Error opening data file");
            let error = if missing {
                Error::new("ocr_language_missing", message)
            } else {
                Error::new("ocr_failed", message)
            };
            return Err(error.details(json!({"stderr":stderr})));
        }
        Ok(s(&done, "stdout").to_string())
    }
}

/// Whether tesseract is installed, for `capabilities`.
pub fn available() -> bool {
    binary().is_some_and(|b| b.is_file())
}

pub fn inverted(image: &Image) -> Image {
    let mut pixels = image.pixels.clone();
    for pixel in pixels.chunks_exact_mut(4) {
        for channel in &mut pixel[..3] {
            *channel = 255 - *channel;
        }
    }
    Image {
        width: image.width,
        height: image.height,
        pixels,
    }
}

pub struct Reading {
    pub words: Vec<Word>,
    pub lines: Vec<Line>,
    pub width: u32,
    pub height: u32,
    /// How many readings were made: 1, or 2 with the colours inverted.
    pub passes: usize,
}

pub fn cropped(image: &Image, region: (u32, u32, u32, u32)) -> Image {
    let (x, y, w, h) = region;
    let (x, y) = (x.min(image.width), y.min(image.height));
    let (w, h) = (w.min(image.width - x), h.min(image.height - y));
    let mut pixels = Vec::with_capacity((w * h * 4) as usize);
    for row in y..y + h {
        let start = ((row * image.width + x) * 4) as usize;
        pixels.extend_from_slice(&image.pixels[start..start + (w * 4) as usize]);
    }
    Image {
        width: w,
        height: h,
        pixels,
    }
}

/// Reads the words of a PNG, or of `region` (x, y, width, height) of it, in
/// pixels of the whole picture. With `invert` `auto` or `always` the picture
/// is read a second time with its colours inverted, because Tesseract reads
/// dark text on a light ground far better than the reverse (a dark theme);
/// the readings are merged. A picture with both light and dark areas is read
/// as one: the minority area may be missed, and `region` reads it alone.
pub fn read(
    recognizer: &mut dyn Recognizer,
    image: &Path,
    language: &str,
    invert: &str,
    min_confidence: f64,
    region: Option<(u32, u32, u32, u32)>,
) -> Result<Reading> {
    let bytes = std::fs::read(image)?;
    let picture = visual::decode(&bytes)?;
    let (width, height) = (picture.width, picture.height);
    let directory = tempfile::tempdir()?;
    let (picture, source, offset) = match region {
        Some(region) => {
            let part = cropped(&picture, region);
            if part.width == 0 || part.height == 0 {
                return Err(Error::new(
                    "invalid_argument",
                    "The region lies outside the picture",
                ));
            }
            let file = directory.path().join("region.png");
            std::fs::write(&file, visual::encode(&part)?)?;
            (
                part,
                file,
                (region.0.min(width) as i64, region.1.min(height) as i64),
            )
        }
        None => (picture, image.to_path_buf(), (0, 0)),
    };
    let mut words = Vec::new();
    let mut passes = 0;
    if invert != "always" {
        words = parse_tsv(&recognizer.tsv(&source, language)?);
        passes += 1;
    }
    if invert != "never" {
        let flipped = directory.path().join("inverted.png");
        std::fs::write(&flipped, visual::encode(&inverted(&picture))?)?;
        words = merge(words, parse_tsv(&recognizer.tsv(&flipped, language)?));
        passes += 1;
    }
    for word in &mut words {
        word.left += offset.0;
        word.top += offset.1;
    }
    words.retain(|w| w.confidence >= min_confidence);
    words.sort_by_key(|w| (w.top, w.left));
    let lines = lines(&words);
    Ok(Reading {
        words,
        lines,
        width,
        height,
        passes,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const HEADER: &str = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n";

    fn tsv(rows: &[(i64, i64, i64, i64, f64, &str)]) -> String {
        let mut out = HEADER.to_string();
        out.push_str("1\t1\t0\t0\t0\t0\t0\t0\t1080\t2400\t-1\t\n");
        out.push_str("4\t1\t1\t1\t1\t0\t10\t10\t500\t40\t-1\t\n");
        for (i, (l, t, w, h, c, text)) in rows.iter().enumerate() {
            out.push_str(&format!(
                "5\t1\t1\t1\t1\t{}\t{l}\t{t}\t{w}\t{h}\t{c}\t{text}\n",
                i + 1
            ));
        }
        out
    }

    fn sample() -> Vec<Word> {
        parse_tsv(&tsv(&[
            (100, 200, 120, 40, 96.5, "Wi-Fi"),
            (100, 300, 150, 40, 91.0, "Sign"),
            (265, 300, 40, 40, 90.0, "in"),
            (700, 300, 200, 40, 88.0, "Forgot?"),
            (100, 400, 50, 40, 12.0, "n0ise"),
            (100, 500, 90, 40, 0.0, " "),
        ]))
    }

    #[test]
    fn tesseract_tsv_becomes_words_with_boxes_and_confidence() {
        let words = sample();
        assert_eq!(words.len(), 5);
        assert_eq!(
            words[0],
            Word {
                text: "Wi-Fi".into(),
                confidence: 96.5,
                left: 100,
                top: 200,
                width: 120,
                height: 40
            }
        );
        // The page, block and line rows and the empty word are not words.
        assert!(words.iter().all(|w| !w.text.trim().is_empty()));
    }

    #[test]
    fn words_join_into_lines_but_separate_elements_stay_apart() {
        let all = lines(&sample());
        let texts: Vec<String> = all.iter().map(Line::text).collect();
        assert_eq!(texts, ["Wi-Fi", "Sign in", "Forgot?", "n0ise"]);
        assert!((all[1].confidence() - 90.5).abs() < 1e-9);
    }

    #[test]
    fn a_phrase_matches_whole_words_in_any_case_and_boxes_them() {
        let all = lines(&sample());
        let m = find_phrase(&all, "sign IN", false);
        assert_eq!(m.len(), 1);
        assert_eq!(
            (m[0].left, m[0].top, m[0].right, m[0].bottom),
            (100, 300, 305, 340)
        );
        assert_eq!(m[0].words, 2);
        // Punctuation at the ends of a word does not matter; a part of a word is not a word.
        assert_eq!(find_phrase(&all, "forgot", false).len(), 1);
        assert!(find_phrase(&all, "sig", false).is_empty());
        assert!(find_phrase(&all, "Sign Forgot?", false).is_empty());
    }

    #[test]
    fn contained_text_matches_part_of_a_word_and_across_words() {
        let all = lines(&sample());
        assert_eq!(find_contained(&all, "wi-", false).len(), 1);
        let across = find_contained(&all, "n i", false);
        assert_eq!(across.len(), 1);
        assert_eq!(across[0].words, 2);
        assert!(find_contained(&all, "wifi", false).is_empty());
    }

    #[test]
    fn a_misread_letter_matches_only_when_asked_to() {
        let words = parse_tsv(&tsv(&[(10, 10, 200, 40, 70.0, "Settlngs")]));
        let all = lines(&words);
        assert!(find_phrase(&all, "Settings", false).is_empty());
        let m = find_phrase(&all, "Settings", true);
        assert_eq!(m.len(), 1);
        assert!(m[0].fuzzy);
        // Short words are never matched loosely.
        let short = lines(&parse_tsv(&tsv(&[(10, 10, 80, 40, 70.0, "Hom3")])));
        assert!(find_phrase(&short, "Home", true).is_empty());
    }

    #[test]
    fn a_selector_picks_by_text_and_index_and_orders_top_to_bottom() {
        let words = parse_tsv(&tsv(&[
            (100, 600, 80, 40, 90.0, "OK"),
            (100, 100, 80, 40, 90.0, "OK"),
        ]));
        let all = lines(&words);
        let first = select(&all, &json!({"text":"ok"}), false);
        assert_eq!((first[0].top, first[1].top), (100, 600));
        let second = select(&all, &json!({"text":"ok","index":1}), false);
        assert_eq!(second.len(), 1);
        assert_eq!(second[0].top, 600);
        assert_eq!(first[0].value()["rect"]["x"], 140);
    }

    #[test]
    fn only_text_selectors_can_be_answered_from_pixels() {
        assert!(supports(&json!({"text":"x"})));
        assert!(supports(&json!({"text_contains":"x","index":1})));
        assert!(!supports(&json!({"resource_id":"x"})));
        assert!(!supports(&json!({"text":"x","clickable":true})));
        assert!(!supports(&json!({"index":0})));
    }

    #[test]
    fn two_readings_of_one_screen_count_a_word_once_with_the_better_confidence() {
        let a = parse_tsv(&tsv(&[(100, 100, 100, 40, 40.0, "Menu")]));
        let b = parse_tsv(&tsv(&[
            (102, 101, 98, 40, 88.0, "Menu"),
            (100, 300, 100, 40, 70.0, "Light"),
        ]));
        let merged = merge(a, b);
        assert_eq!(merged.len(), 2);
        assert_eq!(merged[0].confidence, 88.0);
    }

    struct Fake(Vec<String>);
    impl Recognizer for Fake {
        fn tsv(&mut self, _image: &Path, _language: &str) -> Result<String> {
            Ok(self.0.remove(0))
        }
    }

    fn png(dir: &Path, shade: u8) -> std::path::PathBuf {
        let path = dir.join("screen.png");
        let image = Image {
            width: 8,
            height: 8,
            pixels: [shade, shade, shade, 255].repeat(64),
        };
        std::fs::write(&path, visual::encode(&image).unwrap()).unwrap();
        path
    }

    #[test]
    fn reading_runs_twice_for_auto_and_drops_words_below_the_confidence() {
        let dir = tempfile::tempdir().unwrap();
        let image = png(dir.path(), 20);
        let mut fake = Fake(vec![
            tsv(&[
                (10, 10, 100, 40, 95.0, "Hello"),
                (10, 100, 100, 40, 20.0, "junk"),
            ]),
            tsv(&[(10, 200, 100, 40, 80.0, "Dark")]),
        ]);
        let reading = read(&mut fake, &image, "eng", "auto", 40.0, None).unwrap();
        assert_eq!(reading.passes, 2);
        let texts: Vec<&str> = reading.words.iter().map(|w| w.text.as_str()).collect();
        assert_eq!(texts, ["Hello", "Dark"]);
        assert_eq!((reading.width, reading.height), (8, 8));
        let mut once = Fake(vec![tsv(&[(10, 10, 100, 40, 95.0, "Hello")])]);
        assert_eq!(
            read(&mut once, &image, "eng", "never", 0.0, None)
                .unwrap()
                .passes,
            1
        );
    }

    #[test]
    fn inverting_flips_colour_channels_and_keeps_alpha() {
        let image = Image {
            width: 1,
            height: 1,
            pixels: vec![10, 20, 30, 200],
        };
        assert_eq!(inverted(&image).pixels, [245, 235, 225, 200]);
    }

    /// Needs the installed tesseract: a PNG of black words on white, made once
    /// with ImageMagick, is read back with boxes inside the picture.
    #[test]
    fn the_installed_tesseract_reads_a_rendered_screen() {
        if !available() {
            eprintln!("skipped: tesseract is not installed");
            return;
        }
        let image = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/screen_text.png");
        let reading = read(&mut Tesseract, &image, "eng", "auto", 40.0, None).unwrap();
        assert_eq!((reading.width, reading.height), (900, 520));
        let seen = |r: &Reading| r.lines.iter().map(Line::text).collect::<Vec<_>>();
        for (wanted, near_y) in [("Settings", 90), ("Sign in", 205), ("Forgot password", 345)] {
            let m = find_contained(&reading.lines, wanted, false);
            assert!(!m.is_empty(), "{wanted} not read; got {:?}", seen(&reading));
            let middle = (m[0].top + m[0].bottom) / 2;
            assert!((middle - near_y).abs() < 40, "{wanted}: {:?}", m[0]);
        }
        // A light area in a dark picture is read as a region, offset to the whole picture.
        let night = read(
            &mut Tesseract,
            &image,
            "eng",
            "auto",
            40.0,
            Some((0, 400, 900, 120)),
        )
        .unwrap();
        let m = find_phrase(&night.lines, "Night mode", false);
        assert_eq!(m.len(), 1, "{:?}", seen(&night));
        assert!(m[0].top >= 400 && m[0].bottom <= 520);
        // The same screen with its colours inverted (a dark theme) is read too.
        let dark = tempfile::tempdir().unwrap();
        let flipped = dark.path().join("dark.png");
        let picture = visual::decode(&std::fs::read(&image).unwrap()).unwrap();
        std::fs::write(&flipped, visual::encode(&inverted(&picture)).unwrap()).unwrap();
        let reading = read(&mut Tesseract, &flipped, "eng", "auto", 40.0, None).unwrap();
        assert_eq!(reading.passes, 2);
        assert!(
            !find_contained(&reading.lines, "Settings", false).is_empty(),
            "{:?}",
            seen(&reading)
        );
    }
}
