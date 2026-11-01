//! Screen understanding for coding agents: numbered marks on a screenshot and
//! a tap by mark, the UI tree as short text, text read off pixels, what
//! changed between two screens, and waiting for text or for a screen to
//! settle.
//!
//! On Android the elements come from the UI hierarchy. The virtual iPhone has
//! none, only pixels, so there everything that names text goes through OCR
//! (screen_ocr), and says so in `source`.
use crate::core::*;
use crate::ios_emulator::ui_testing;
use crate::screen_ocr::{self as ocr, Line, Recognizer};
use crate::screen_tree as tree;
use crate::visual::{self, Image};
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
    thread,
    time::{Duration, Instant},
};

/// A screen an operation looks at and taps: an Android device or the virtual
/// iPhone. Tests replace it with recorded screens.
pub trait Screen {
    /// Saves the screen as a PNG and answers its size in pixels.
    fn capture(&mut self, path: &Path) -> Result<(u32, u32)>;
    /// The UI hierarchy as UIAutomator XML, or `None` where the platform
    /// reports none.
    fn hierarchy(&mut self) -> Result<Option<String>>;
    /// What the platform reports about the device and its screen, for
    /// `screen.export`. A screen that reports nothing answers the default.
    fn facts(&mut self, _size: (u32, u32)) -> crate::screen_export::Facts {
        crate::screen_export::Facts::default()
    }
    /// A tap at pixels of the screenshot, or a press held for `hold_ms`.
    fn tap(&mut self, x: i64, y: i64, hold_ms: Option<u64>) -> Result;
}

pub struct Android(pub String);

/// The size of a PNG from its header.
pub fn png_size(path: &Path) -> Result<(u32, u32)> {
    use std::io::Read;
    let mut head = [0u8; 24];
    fs::File::open(path)?.read_exact(&mut head)?;
    if &head[..8] != b"\x89PNG\r\n\x1a\n" {
        return Err(Error::new("unsupported_image", "Not a PNG file"));
    }
    Ok((
        u32::from_be_bytes(head[16..20].try_into().unwrap()),
        u32::from_be_bytes(head[20..24].try_into().unwrap()),
    ))
}

impl Screen for Android {
    fn capture(&mut self, path: &Path) -> Result<(u32, u32)> {
        crate::android_device::screen(&json!({"device":self.0,"path":path}))?;
        png_size(path)
    }
    fn hierarchy(&mut self) -> Result<Option<String>> {
        let reply = crate::devices::handle("android.hierarchy", &json!({"device":self.0}))?;
        Ok(Some(s(&reply, "xml").to_string()))
    }
    fn facts(&mut self, size: (u32, u32)) -> crate::screen_export::Facts {
        crate::screen_export::android_facts(&self.0, size)
    }
    fn tap(&mut self, x: i64, y: i64, hold_ms: Option<u64>) -> Result {
        let mut input = json!({"device":self.0,"action":"tap","x":x,"y":y});
        if let Some(hold) = hold_ms {
            input = json!({"device":self.0,"action":"swipe","x":x,"y":y,"end_x":x,"end_y":y,"duration":hold});
        }
        crate::devices::handle("android.input", &input)
    }
}

/// Runs `action` with the screen a `device` names: an Android serial, or
/// `virtual-iphone`.
pub fn with_screen<T>(
    device: &str,
    action: impl FnOnce(&mut dyn Screen) -> Result<T>,
) -> Result<T> {
    if ui_testing::is_virtual(device) {
        let mut phone = ui_testing::LivePhone::default();
        return action(&mut ui_testing::PhoneScreen(&mut phone));
    }
    identifier(device, "device", None)?;
    action(&mut Android(device.to_string()))
}

pub fn no_hierarchy(operation: &str, device: &str) -> Error {
    Error::new(
        "ui_hierarchy_unavailable",
        format!("{operation} needs a UI hierarchy, and {device} reports none"),
    )
    .remedy("Use screen.marks with source ocr, screen.ocr or screen.wait to work from the pixels")
    .details(json!({"device":device,"ocr_available":ocr::available()}))
}

// ---- Drawing ---------------------------------------------------------------

const DIGITS: [[u8; 7]; 10] = [
    [
        0b01110, 0b10001, 0b10011, 0b10101, 0b11001, 0b10001, 0b01110,
    ],
    [
        0b00100, 0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110,
    ],
    [
        0b01110, 0b10001, 0b00001, 0b00010, 0b00100, 0b01000, 0b11111,
    ],
    [
        0b11110, 0b00001, 0b00001, 0b01110, 0b00001, 0b00001, 0b11110,
    ],
    [
        0b00010, 0b00110, 0b01010, 0b10010, 0b11111, 0b00010, 0b00010,
    ],
    [
        0b11111, 0b10000, 0b11110, 0b00001, 0b00001, 0b10001, 0b01110,
    ],
    [
        0b00110, 0b01000, 0b10000, 0b11110, 0b10001, 0b10001, 0b01110,
    ],
    [
        0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b01000, 0b01000,
    ],
    [
        0b01110, 0b10001, 0b10001, 0b01110, 0b10001, 0b10001, 0b01110,
    ],
    [
        0b01110, 0b10001, 0b10001, 0b01111, 0b00001, 0b00010, 0b01100,
    ],
];

/// Box colours that stay apart from each other and from most app palettes.
const COLOURS: [[u8; 3]; 6] = [
    [220, 38, 38],
    [37, 99, 235],
    [22, 163, 74],
    [234, 88, 12],
    [147, 51, 234],
    [13, 148, 136],
];

fn fill(image: &mut Image, left: i64, top: i64, right: i64, bottom: i64, colour: [u8; 3]) {
    for y in top.max(0)..bottom.min(image.height as i64) {
        for x in left.max(0)..right.min(image.width as i64) {
            let at = ((y as u32 * image.width + x as u32) * 4) as usize;
            image.pixels[at..at + 3].copy_from_slice(&colour);
            image.pixels[at + 3] = 255;
        }
    }
}

fn outline(image: &mut Image, rect: tree::Rect, thickness: i64, colour: [u8; 3]) {
    let (l, t, r, b) = rect;
    fill(image, l, t, r, t + thickness, colour);
    fill(image, l, b - thickness, r, b, colour);
    fill(image, l, t, l + thickness, b, colour);
    fill(image, r - thickness, t, r, b, colour);
}

/// The width and height of a number's tag at `scale`.
fn tag_size(number: usize, scale: i64) -> (i64, i64) {
    let digits = number.to_string().len() as i64;
    ((digits * 6 + 1) * scale + scale, 9 * scale)
}

fn draw_number(image: &mut Image, number: usize, left: i64, top: i64, scale: i64, colour: [u8; 3]) {
    let (width, height) = tag_size(number, scale);
    fill(image, left, top, left + width, top + height, colour);
    for (i, digit) in number.to_string().bytes().enumerate() {
        let glyph = &DIGITS[(digit - b'0') as usize];
        let x0 = left + scale + i as i64 * 6 * scale;
        for (row, bits) in glyph.iter().enumerate() {
            for col in 0..5 {
                if bits >> (4 - col) & 1 == 1 {
                    let (x, y) = (x0 + col * scale, top + scale + row as i64 * scale);
                    fill(image, x, y, x + scale, y + scale, [255, 255, 255]);
                }
            }
        }
    }
}

/// Draws each mark's box and number onto the screenshot. `scale` converts the
/// marks' coordinates to the picture's when the two differ in size.
pub fn draw_marks(image: &mut Image, marks: &[Value], scale: f64) {
    let unit = (i64::from(image.width) / 360).max(2);
    let thickness = (unit / 2).max(2);
    for mark in marks {
        let number = mark["mark"].as_u64().unwrap_or(0) as usize;
        let colour = COLOURS[number % COLOURS.len()];
        let b = &mark["bounds"];
        let at = |key: &str| (b[key].as_f64().unwrap_or(0.0) * scale).round() as i64;
        let rect = (at("left"), at("top"), at("right"), at("bottom"));
        outline(image, rect, thickness, colour);
        // The tag sits inside the box's top-left corner, or inside the picture.
        let (w, h) = tag_size(number, unit);
        let left = rect.0.clamp(0, (i64::from(image.width) - w).max(0));
        let top = rect.1.clamp(0, (i64::from(image.height) - h).max(0));
        draw_number(image, number, left, top, unit, colour);
    }
}

// ---- The marks of the last look -------------------------------------------------

/// Where the marks of each device are kept between `screen.marks` and
/// `screen.tap`.
pub struct Store(PathBuf);

impl Store {
    pub fn home() -> Result<Self> {
        Ok(Store(home()?.join("screen-marks")))
    }
    #[cfg(test)]
    pub fn at(directory: PathBuf) -> Self {
        Store(directory)
    }
    fn stem(&self, device: &str) -> PathBuf {
        self.0.join(&digest(device.as_bytes())[..16])
    }
    pub fn save(&self, device: &str, record: &Value, picture: Option<&[u8]>) -> Result<()> {
        private_dir(&self.0)?;
        let stem = self.stem(device);
        let image = stem.with_extension("png");
        match picture {
            Some(bytes) => atomic_write(&image, bytes)?,
            None => {
                let _ = fs::remove_file(&image);
            }
        }
        atomic_write(&stem.with_extension("json"), record.to_string().as_bytes())
    }
    pub fn load(&self, device: &str) -> Result<(Value, Option<Image>)> {
        let stem = self.stem(device);
        let text = fs::read_to_string(stem.with_extension("json")).map_err(|_| {
            Error::new(
                "marks_missing",
                format!("No marks were made for {device}"),
            )
            .remedy("Call screen.marks (or screen.tree) for this device, look at the result, then tap a mark")
        })?;
        let record: Value = serde_json::from_str(&text).map_err(|e| {
            Error::new(
                "marks_missing",
                format!("The saved marks are unreadable: {e}"),
            )
        })?;
        let picture = fs::read(stem.with_extension("png"))
            .ok()
            .and_then(|bytes| visual::decode(&bytes).ok());
        Ok((record, picture))
    }
}

pub(crate) fn limit_argument(
    a: &Value,
    key: &str,
    default: u64,
    minimum: u64,
    maximum: u64,
) -> Result<usize> {
    let value = a.get(key).and_then(Value::as_u64).unwrap_or(default);
    if !(minimum..=maximum).contains(&value) {
        return Err(Error::new(
            "invalid_argument",
            format!("{key} is a number from {minimum} to {maximum}"),
        ));
    }
    Ok(value as usize)
}

/// The mark made from a line of text read off the screen.
fn line_mark(line: &Line, number: usize) -> Value {
    let found = ocr::Found {
        text: line.text(),
        confidence: line.confidence(),
        left: line.words.iter().map(|w| w.left).min().unwrap_or(0),
        top: line.words.iter().map(|w| w.top).min().unwrap_or(0),
        right: line
            .words
            .iter()
            .map(|w| w.left + w.width)
            .max()
            .unwrap_or(0),
        bottom: line
            .words
            .iter()
            .map(|w| w.top + w.height)
            .max()
            .unwrap_or(0),
        words: line.words.len(),
        fuzzy: false,
    };
    let value = found.value();
    json!({"mark":number,"label":found.text,"text":found.text,"description":"","resource_id":"","class":"",
        "bounds":value["rect"],"confidence":value["confidence"],"source":"ocr"})
}

pub(crate) fn ocr_marks(lines: &[Line], limit: usize) -> (Vec<Value>, bool) {
    let readable: Vec<&Line> = lines
        .iter()
        .filter(|l| l.text().chars().any(char::is_alphanumeric))
        .collect();
    let truncated = readable.len() > limit;
    (
        readable
            .iter()
            .take(limit)
            .enumerate()
            .map(|(i, l)| line_mark(l, i + 1))
            .collect(),
        truncated,
    )
}

pub(crate) fn language(a: &Value) -> Result<&str> {
    let language = a["language"].as_str().unwrap_or("eng");
    if language.is_empty()
        || !language
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'+')
    {
        return Err(Error::new(
            "invalid_argument",
            "language is a Tesseract language code such as eng or eng+deu",
        ));
    }
    Ok(language)
}

pub(crate) fn region(a: &Value) -> Result<Option<(u32, u32, u32, u32)>> {
    let Some(r) = a.get("region").filter(|r| r.is_object()) else {
        return Ok(None);
    };
    let value = |key: &str| u32::try_from(n(r, key)).unwrap_or(u32::MAX);
    if value("width") == 0 || value("height") == 0 {
        return Err(Error::new(
            "invalid_argument",
            "region needs a width and a height of at least 1",
        ));
    }
    Ok(Some((
        value("x"),
        value("y"),
        value("width"),
        value("height"),
    )))
}

/// What `source` the marks and waits use: the hierarchy where the platform has
/// one (unless OCR is asked for), else pixels.
pub(crate) fn hierarchy_for(
    screen: &mut dyn Screen,
    source: &str,
    operation: &str,
    device: &str,
) -> Result<Option<String>> {
    if source == "ocr" {
        return Ok(None);
    }
    match screen.hierarchy()? {
        Some(xml) => Ok(Some(xml)),
        None if source == "hierarchy" => Err(no_hierarchy(operation, device)),
        None => Ok(None),
    }
}

pub(crate) fn scratch() -> Result<tempfile::TempDir> {
    Ok(tempfile::tempdir()?)
}

// ---- screen.marks -----------------------------------------------------------

pub fn marks(
    a: &Value,
    screen: &mut dyn Screen,
    recognizer: &mut dyn Recognizer,
    store: &Store,
) -> Result {
    let device = s(a, "device");
    let target = output_path(s(a, "path"))?;
    if target
        .extension()
        .is_none_or(|e| !e.eq_ignore_ascii_case("png"))
    {
        return Err(Error::new(
            "invalid_path",
            "The marked screenshot is a PNG; name a file ending in .png",
        ));
    }
    let limit = limit_argument(a, "max_marks", 60, 1, 200)?;
    let source = a["source"].as_str().unwrap_or("auto");
    let xml = hierarchy_for(screen, source, "screen.marks", device)?;
    if let (Some(xml), Some(keep)) = (&xml, a["xml_path"].as_str()) {
        atomic_write(&output_path(keep)?, xml.as_bytes())?;
    }
    let work = scratch()?;
    let raw = work.path().join("screen.png");
    let (width, height) = screen.capture(&raw)?;
    let raw_bytes = fs::read(&raw)?;
    let mut picture = visual::decode(&raw_bytes)?;
    let (found, truncated, from, scale, note) = match &xml {
        Some(xml) => {
            let nodes = tree::parse(xml)?;
            // UIAutomator bounds are pixels of the screenshot, so no scaling.
            let scale = 1.0;
            let (found, truncated) = tree::marks(&nodes, limit);
            (
                found,
                truncated,
                "hierarchy",
                scale,
                "Elements the UI hierarchy reports as clickable, long-clickable, checkable or editable and enabled. Content an app draws itself (a canvas, a game, much of a WebView) is not in the hierarchy: use source ocr for that.",
            )
        }
        None => {
            let min = a["min_confidence"].as_f64().unwrap_or(40.0);
            let reading = ocr::read(
                recognizer,
                &raw,
                language(a)?,
                a["invert"].as_str().unwrap_or("auto"),
                min,
                region(a)?,
            )?;
            let (found, truncated) = ocr_marks(&reading.lines, limit);
            (
                found,
                truncated,
                "ocr",
                1.0,
                "Lines of text read off the pixels with Tesseract, with its confidence. An icon without a label, and text it misread or missed, has no mark.",
            )
        }
    };
    draw_marks(&mut picture, &found, scale);
    let bytes = visual::encode(&picture)?;
    atomic_write(&target, &bytes)?;
    store.save(
        device,
        &json!({"device":device,"taken":now(),"source":from,"width":width,"height":height,"marks":found}),
        Some(&raw_bytes),
    )?;
    Ok(
        json!({"path":target,"mime_type":"image/png","bytes":bytes.len(),"sha256":digest(&bytes),
        "width":width,"height":height,"source":from,"count":found.len(),"truncated":truncated,
        "marks":found,"note":note,"tap_with":"screen.tap"}),
    )
}

// ---- screen.tap ----------------------------------------------------------------

/// The fraction of a region of two pictures that differs.
fn region_change(a: &Image, b: &Image, rect: (i64, i64, i64, i64), tolerance: u8) -> f64 {
    if (a.width, a.height) != (b.width, b.height) {
        return 1.0;
    }
    let (l, t) = (
        rect.0.clamp(0, i64::from(a.width)),
        rect.1.clamp(0, i64::from(a.height)),
    );
    let (r, bo) = (
        rect.2.clamp(l, i64::from(a.width)),
        rect.3.clamp(t, i64::from(a.height)),
    );
    let (mut total, mut changed) = (0u64, 0u64);
    for y in t..bo {
        for x in l..r {
            let at = ((y as u32 * a.width + x as u32) * 4) as usize;
            total += 1;
            if (0..3).any(|c| a.pixels[at + c].abs_diff(b.pixels[at + c]) > tolerance) {
                changed += 1;
            }
        }
    }
    if total == 0 {
        1.0
    } else {
        changed as f64 / total as f64
    }
}

pub fn tap(a: &Value, screen: &mut dyn Screen, store: &Store) -> Result {
    let device = s(a, "device");
    let number = a["mark"].as_u64().unwrap_or(0);
    let (record, picture) = store.load(device)?;
    let list = record["marks"].as_array().cloned().unwrap_or_default();
    let mark = list
        .iter()
        .find(|m| m["mark"].as_u64() == Some(number))
        .ok_or_else(|| {
            Error::new(
                "mark_not_found",
                format!(
                    "There is no mark {number}; the last look at {device} made {}",
                    list.len()
                ),
            )
            .remedy("Call screen.marks again and use a number from its marks")
        })?;
    let mut verified = Value::Null;
    if a["verify"] != json!(false) {
        let stale = |why: &str| {
            Error::new(
                "mark_stale",
                format!("Mark {number} no longer matches the screen: {why}"),
            )
            .remedy("The screen changed since screen.marks; call it again and tap the new number")
            .details(json!({"mark":mark}))
        };
        if s(mark, "source") == "hierarchy" {
            let xml = screen
                .hierarchy()?
                .ok_or_else(|| no_hierarchy("screen.tap", device))?;
            if !tree::has_element(&tree::parse(&xml)?, mark) {
                return Err(stale(
                    "its element is not in the UI hierarchy any more, or changed",
                ));
            }
        } else {
            let before = picture.ok_or_else(|| stale("the picture it was made from is gone"))?;
            let work = scratch()?;
            let now_png = work.path().join("now.png");
            screen.capture(&now_png)?;
            let current = visual::decode(&fs::read(&now_png)?)?;
            let b = &mark["bounds"];
            let rect = (
                b["left"].as_i64().unwrap_or(0),
                b["top"].as_i64().unwrap_or(0),
                b["right"].as_i64().unwrap_or(0),
                b["bottom"].as_i64().unwrap_or(0),
            );
            let change = region_change(&before, &current, rect, 32);
            if change > 0.1 {
                return Err(stale(&format!(
                    "{:.0}% of the pixels where it was have changed",
                    change * 100.0
                )));
            }
        }
        verified = json!(true);
    }
    let (x, y) = (
        mark["bounds"]["x"].as_i64().unwrap_or(-1),
        mark["bounds"]["y"].as_i64().unwrap_or(-1),
    );
    if x < 0 || y < 0 {
        return Err(Error::new(
            "invalid_bounds",
            "The mark has no usable position",
        ));
    }
    let hold = a["duration"].as_u64();
    screen.tap(x, y, hold)?;
    Ok(
        json!({"device":device,"mark":number,"tapped":{"x":x,"y":y},"long_press":hold.is_some(),
        "verified":verified,"element":mark}),
    )
}

// ---- screen.tree ----------------------------------------------------------------

pub fn tree_text(a: &Value, screen: &mut dyn Screen, store: &Store) -> Result {
    let device = s(a, "device");
    let max_chars = limit_argument(a, "max_chars", 6000, 500, 100_000)?;
    let xml = screen
        .hierarchy()?
        .ok_or_else(|| no_hierarchy("screen.tree", device))?;
    if let Some(keep) = a["xml_path"].as_str() {
        atomic_write(&output_path(keep)?, xml.as_bytes())?;
    }
    let nodes = tree::parse(&xml)?;
    let numbers = tree::numbering(&nodes, 200);
    let rendered = tree::tree(&nodes, &numbers, max_chars, b(a, "only_interactive"));
    let (found, _) = tree::marks(&nodes, 200);
    store.save(
        device,
        &json!({"device":device,"taken":now(),"source":"hierarchy","width":rendered.width,"height":rendered.height,"marks":found}),
        None,
    )?;
    Ok(
        json!({"text":rendered.text,"chars":rendered.text.chars().count(),"package":rendered.package,
        "width":rendered.width,"height":rendered.height,"nodes":rendered.nodes_total,"shown":rendered.nodes_shown,
        "truncated":rendered.truncated,"marks":found.len(),"source":"hierarchy"}),
    )
}

// ---- screen.ocr -------------------------------------------------------------------

pub fn read_text(
    a: &Value,
    screen: Option<&mut dyn Screen>,
    recognizer: &mut dyn Recognizer,
) -> Result {
    let work = scratch()?;
    let image = match (a["image"].as_str(), screen) {
        (Some(file), _) => path(file, false, true)?,
        (None, Some(screen)) => {
            let file = work.path().join("screen.png");
            screen.capture(&file)?;
            file
        }
        (None, None) => {
            return Err(Error::new(
                "invalid_argument",
                "Give a device or the path of an image",
            ));
        }
    };
    let started = Instant::now();
    let min = a["min_confidence"].as_f64().unwrap_or(40.0);
    let fuzzy = b(a, "fuzzy");
    let reading = ocr::read(
        recognizer,
        &image,
        language(a)?,
        a["invert"].as_str().unwrap_or("auto"),
        min,
        region(a)?,
    )?;
    let mut selector = json!({});
    for key in ["text", "text_contains"] {
        if a.get(key).is_some() {
            selector[key] = a[key].clone();
        }
    }
    if a.get("index").is_some() {
        selector["index"] = a["index"].clone();
    }
    let matches = if ocr::supports(&selector) {
        Some(ocr::select(&reading.lines, &selector, fuzzy))
    } else {
        None
    };
    let limit = limit_argument(a, "max_words", 400, 1, 5000)?;
    let words: Vec<Value> = reading
        .words
        .iter()
        .take(limit)
        .map(|w| {
            json!({"text":w.text,"confidence":(w.confidence * 10.0).round() / 10.0,
            "rect":{"left":w.left,"top":w.top,"right":w.left + w.width,"bottom":w.top + w.height,
                "x":w.left + w.width / 2,"y":w.top + w.height / 2}})
        })
        .collect();
    let lines: Vec<Value> = reading
        .lines
        .iter()
        .take(limit)
        .map(|l| {
            let m = &line_mark(l, 0);
            json!({"text":m["text"],"confidence":m["confidence"],"rect":m["bounds"]})
        })
        .collect();
    let mut result = json!({"source":"ocr","engine":"tesseract","language":language(a)?,"width":reading.width,"height":reading.height,
        "passes":reading.passes,"min_confidence":min,"word_count":reading.words.len(),"words":words,"lines":lines,
        "truncated":reading.words.len() > limit,"seconds":(started.elapsed().as_secs_f64() * 100.0).round() / 100.0,
        "note":"Text recognised from pixels. Words can be misread or missed, especially small, low-contrast or decorative text and a screen with both light and dark areas (read part of it with region); judge by confidence."});
    if let Some(found) = matches {
        result["matches"] = json!(found.iter().map(ocr::Found::value).collect::<Vec<_>>());
    }
    Ok(result)
}

// ---- Differences between two screens ----------------------------------------------

fn ignored(rects: &[(i64, i64, i64, i64)], x: i64, y: i64) -> bool {
    rects
        .iter()
        .any(|r| x >= r.0 && x < r.2 && y >= r.1 && y < r.3)
}

fn ignore_list(value: &Value) -> Vec<(i64, i64, i64, i64)> {
    value
        .as_array()
        .into_iter()
        .flatten()
        .map(|r| {
            let (x, y) = (n(r, "x") as i64, n(r, "y") as i64);
            (x, y, x + n(r, "width") as i64, y + n(r, "height") as i64)
        })
        .collect()
}

/// How many pixels differ by more than `tolerance`, and how many were
/// compared. Pictures of different sizes differ everywhere.
pub fn changed_fraction(a: &Image, b: &Image, tolerance: u8, ignore: &Value) -> f64 {
    if (a.width, a.height) != (b.width, b.height) {
        return 1.0;
    }
    let skip = ignore_list(ignore);
    let (mut total, mut changed) = (0u64, 0u64);
    for y in 0..a.height {
        for x in 0..a.width {
            if !skip.is_empty() && ignored(&skip, i64::from(x), i64::from(y)) {
                continue;
            }
            let at = ((y * a.width + x) * 4) as usize;
            total += 1;
            if (0..3).any(|c| a.pixels[at + c].abs_diff(b.pixels[at + c]) > tolerance) {
                changed += 1;
            }
        }
    }
    if total == 0 {
        0.0
    } else {
        changed as f64 / total as f64
    }
}

const CELL: u32 = 16;

fn position(rect: (u32, u32, u32, u32), width: u32, height: u32) -> String {
    let (cx, cy) = ((rect.0 + rect.2) / 2, (rect.1 + rect.3) / 2);
    let vertical = ["top", "middle", "bottom"][(cy * 3 / height.max(1)).min(2) as usize];
    let horizontal = ["left", "centre", "right"][(cx * 3 / width.max(1)).min(2) as usize];
    format!("{vertical} {horizontal}")
}

/// The regions where two pictures of one size differ: changed pixels are
/// grouped in 16-pixel cells, cells near each other are one region.
pub fn pixel_regions(
    before: &Image,
    after: &Image,
    tolerance: u8,
    ignore: &Value,
    max_regions: usize,
) -> Value {
    let (width, height) = (before.width, before.height);
    let skip = ignore_list(ignore);
    let (columns, rows) = (width.div_ceil(CELL), height.div_ceil(CELL));
    let mut cells = vec![0u32; (columns * rows) as usize];
    let mut extent: Vec<(u32, u32, u32, u32)> = vec![(u32::MAX, u32::MAX, 0, 0); cells.len()];
    let mut compared = 0u64;
    for y in 0..height {
        for x in 0..width {
            if !skip.is_empty() && ignored(&skip, i64::from(x), i64::from(y)) {
                continue;
            }
            compared += 1;
            let at = ((y * width + x) * 4) as usize;
            if (0..3).any(|c| before.pixels[at + c].abs_diff(after.pixels[at + c]) > tolerance) {
                let cell = ((y / CELL) * columns + x / CELL) as usize;
                cells[cell] += 1;
                let e = &mut extent[cell];
                *e = (e.0.min(x), e.1.min(y), e.2.max(x + 1), e.3.max(y + 1));
            }
        }
    }
    // Cells within two cells of each other belong together.
    let mut group = vec![usize::MAX; cells.len()];
    let mut regions: Vec<(u64, (u32, u32, u32, u32))> = Vec::new();
    for start in 0..cells.len() {
        if cells[start] == 0 || group[start] != usize::MAX {
            continue;
        }
        let id = regions.len();
        let mut stack = vec![start];
        group[start] = id;
        let (mut pixels, mut bounds) = (0u64, (u32::MAX, u32::MAX, 0u32, 0u32));
        while let Some(cell) = stack.pop() {
            pixels += u64::from(cells[cell]);
            let e = extent[cell];
            bounds = (
                bounds.0.min(e.0),
                bounds.1.min(e.1),
                bounds.2.max(e.2),
                bounds.3.max(e.3),
            );
            let (cx, cy) = (
                (cell as u32 % columns) as i64,
                (cell as u32 / columns) as i64,
            );
            for dy in -2..=2i64 {
                for dx in -2..=2i64 {
                    let (nx, ny) = (cx + dx, cy + dy);
                    if nx < 0 || ny < 0 || nx >= i64::from(columns) || ny >= i64::from(rows) {
                        continue;
                    }
                    let next = (ny as u32 * columns + nx as u32) as usize;
                    if cells[next] > 0 && group[next] == usize::MAX {
                        group[next] = id;
                        stack.push(next);
                    }
                }
            }
        }
        regions.push((pixels, bounds));
    }
    let changed: u64 = regions.iter().map(|r| r.0).sum();
    regions.sort_by_key(|r| std::cmp::Reverse(r.0));
    let fraction = if compared == 0 {
        0.0
    } else {
        changed as f64 / compared as f64
    };
    let total = regions.len();
    let listed: Vec<Value> = regions
        .iter()
        .take(max_regions)
        .map(|(pixels, r)| {
            let area = u64::from((r.2 - r.0) * (r.3 - r.1)).max(1);
            json!({"x":r.0,"y":r.1,"width":r.2 - r.0,"height":r.3 - r.1,"changed_pixels":pixels,
                "changed_fraction_of_region":((*pixels as f64 / area as f64) * 1000.0).round() / 1000.0,
                "position":position(*r, width, height),
                "likely_status_bar":r.3 <= height / 25})
        })
        .collect();
    let summary = match listed.first() {
        None => "No pixel changed.".to_string(),
        Some(first) => format!(
            "{total} region{} changed, {:.2}% of the pixels; the largest is {}x{} at ({}, {}), {}.",
            if total == 1 { "" } else { "s" },
            fraction * 100.0,
            first["width"],
            first["height"],
            first["x"],
            first["y"],
            first["position"].as_str().unwrap_or("")
        ),
    };
    json!({"kind":"pixels","identical":total == 0,"size_changed":false,"width":width,"height":height,
        "changed_pixels":changed,"changed_fraction":fraction,"region_count":total,"regions":listed,
        "truncated":total > max_regions,"summary":summary})
}

/// Lines of text that appear on one screen and not the other, from two
/// readings.
fn text_changes(before: &[Line], after: &[Line]) -> Value {
    let key = |l: &Line| {
        l.text()
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
            .to_lowercase()
    };
    let mut remaining: Vec<String> = after.iter().map(key).collect();
    let mut removed = Vec::new();
    for line in before {
        match remaining.iter().position(|k| *k == key(line)) {
            Some(i) => {
                remaining.remove(i);
            }
            None => removed.push(line_mark(line, 0)),
        }
    }
    let before_keys: Vec<String> = before.iter().map(key).collect();
    let mut left = before_keys;
    let mut added = Vec::new();
    for line in after {
        match left.iter().position(|k| *k == key(line)) {
            Some(i) => {
                left.remove(i);
            }
            None => added.push(line_mark(line, 0)),
        }
    }
    let shape = |marks: Vec<Value>| -> Vec<Value> {
        marks
            .into_iter()
            .map(|m| json!({"text":m["text"],"rect":m["bounds"],"confidence":m["confidence"]}))
            .collect()
    };
    json!({"added":shape(added),"removed":shape(removed),
        "note":"Read with OCR on both pictures, so a misread word shows as one line removed and one added."})
}

fn is_xml(path: &str) -> bool {
    path.to_ascii_lowercase().ends_with(".xml")
}

pub fn diff(a: &Value, screen: Option<&mut dyn Screen>, recognizer: &mut dyn Recognizer) -> Result {
    let before_path = s(a, "before");
    let limit = limit_argument(a, "max_regions", 12, 1, 100)?;
    let xml_mode = is_xml(before_path);
    if xml_mode {
        let before_xml = fs::read_to_string(path(before_path, false, true)?)?;
        let after_xml = match (a["after"].as_str(), screen) {
            (Some(file), _) => {
                if !is_xml(file) {
                    return Err(Error::new(
                        "invalid_argument",
                        "before and after must both be UI hierarchies (.xml) or both screenshots (.png)",
                    ));
                }
                fs::read_to_string(path(file, false, true)?)?
            }
            (None, Some(screen)) => screen
                .hierarchy()?
                .ok_or_else(|| no_hierarchy("screen.diff", s(a, "device")))?,
            (None, None) => {
                return Err(Error::new(
                    "invalid_argument",
                    "Give after, or a device to compare with",
                ));
            }
        };
        return Ok(tree::compare(
            &tree::parse(&before_xml)?,
            &tree::parse(&after_xml)?,
            limit,
        ));
    }
    let work = scratch()?;
    let before_file = path(before_path, false, true)?;
    let after_file = match (a["after"].as_str(), screen) {
        (Some(file), _) => {
            if is_xml(file) {
                return Err(Error::new(
                    "invalid_argument",
                    "before and after must both be UI hierarchies (.xml) or both screenshots (.png)",
                ));
            }
            path(file, false, true)?
        }
        (None, Some(screen)) => {
            let file = work.path().join("after.png");
            screen.capture(&file)?;
            file
        }
        (None, None) => {
            return Err(Error::new(
                "invalid_argument",
                "Give after, or a device to compare with",
            ));
        }
    };
    let before = visual::decode(&fs::read(&before_file)?)?;
    let after = visual::decode(&fs::read(&after_file)?)?;
    if (before.width, before.height) != (after.width, after.height) {
        return Ok(
            json!({"kind":"pixels","identical":false,"size_changed":true,
            "before_size":{"width":before.width,"height":before.height},"after_size":{"width":after.width,"height":after.height},
            "regions":[],"summary":format!("The screens differ in size ({}x{} and {}x{}); the device may have rotated.", before.width, before.height, after.width, after.height)}),
        );
    }
    let tolerance = n(a, "tolerance").min(255) as u8;
    let tolerance = if a.get("tolerance").is_some() {
        tolerance
    } else {
        24
    };
    let mut result = pixel_regions(&before, &after, tolerance, &a["ignore"], limit);
    if b(a, "text") {
        let min = a["min_confidence"].as_f64().unwrap_or(40.0);
        let language = language(a)?;
        let first = ocr::read(recognizer, &before_file, language, "auto", min, None)?;
        let second = ocr::read(recognizer, &after_file, language, "auto", min, None)?;
        result["text"] = text_changes(&first.lines, &second.lines);
    }
    Ok(result)
}

// ---- Waiting ------------------------------------------------------------------------

/// What a wait saw last, for the error that says why it timed out.
struct Seen {
    lines: Vec<String>,
    fraction: Option<f64>,
}

fn contains_ignoring_case(haystack: &str, needle: &str) -> bool {
    haystack.to_lowercase().contains(&needle.to_lowercase())
}

/// Elements of a hierarchy that show `text`, in their text or description.
fn hierarchy_matches(nodes: &[tree::Node], text: &str) -> Vec<Value> {
    (0..nodes.len())
        .filter(|i| {
            let n = &nodes[*i];
            n.has_area()
                && (contains_ignoring_case(&n.text, text)
                    || contains_ignoring_case(&n.description, text))
        })
        .map(|i| tree::mark(nodes, i, 0))
        .collect()
}

fn pause(interval: Duration, deadline: Instant) {
    thread::sleep(interval.min(deadline.saturating_duration_since(Instant::now())));
}

pub fn wait(a: &Value, screen: &mut dyn Screen, recognizer: &mut dyn Recognizer) -> Result {
    let device = s(a, "device");
    let until = s(a, "until");
    let timeout = a["timeout"].as_u64().unwrap_or(30).clamp(0, 300);
    let interval =
        Duration::from_millis(a["interval_ms"].as_u64().unwrap_or(500).clamp(50, 10_000));
    let started = Instant::now();
    let deadline = started + Duration::from_secs(timeout);
    let work = scratch()?;
    let frame = work.path().join("frame.png");
    let mut seen = Seen {
        lines: Vec::new(),
        fraction: None,
    };
    match until {
        "text" | "text_gone" => {
            let text = s(a, "text");
            if text.trim().is_empty() {
                return Err(Error::new(
                    "invalid_argument",
                    "Waiting for text needs text",
                ));
            }
            let want_present = until == "text";
            let source = a["source"].as_str().unwrap_or("auto");
            let min = a["min_confidence"].as_f64().unwrap_or(40.0);
            let fuzzy = b(a, "fuzzy");
            loop {
                check_cancelled()?;
                let xml = hierarchy_for(screen, source, "screen.wait", device)?;
                let (used, matches) = match &xml {
                    Some(xml) => {
                        let nodes = tree::parse(xml)?;
                        seen.lines = nodes
                            .iter()
                            .filter(|n| !n.text.trim().is_empty())
                            .take(20)
                            .map(|n| n.text.clone())
                            .collect();
                        ("hierarchy", hierarchy_matches(&nodes, text))
                    }
                    None => {
                        screen.capture(&frame)?;
                        let reading =
                            ocr::read(recognizer, &frame, language(a)?, "auto", min, region(a)?)?;
                        seen.lines = reading.lines.iter().take(20).map(Line::text).collect();
                        let found = ocr::find_contained(&reading.lines, text, fuzzy);
                        ("ocr", found.iter().map(ocr::Found::value).collect())
                    }
                };
                if matches.is_empty() != want_present {
                    return Ok(json!({"met":true,"until":until,"source":used,"text":text,
                        "waited_seconds":(started.elapsed().as_secs_f64() * 100.0).round() / 100.0,"matches":matches}));
                }
                if Instant::now() >= deadline {
                    return Err(Error::new(
                        "text_timeout",
                        format!(
                            "The text {text:?} did not {} within {timeout}s",
                            if want_present { "appear" } else { "go away" }
                        ),
                    )
                    .details(
                        json!({"source":used,"text":text,"matches":matches,"seen":seen.lines}),
                    ));
                }
                pause(interval, deadline);
            }
        }
        "stable" => {
            let stable_for =
                Duration::from_millis(a["stable_ms"].as_u64().unwrap_or(1000).clamp(100, 60_000));
            let threshold = a["threshold"].as_f64().unwrap_or(0.001);
            let tolerance = a["tolerance"].as_u64().unwrap_or(24).min(255) as u8;
            let mut previous: Option<Image> = None;
            let mut quiet_since: Option<Instant> = None;
            loop {
                check_cancelled()?;
                screen.capture(&frame)?;
                let current = visual::decode(&fs::read(&frame)?)?;
                if let Some(old) = &previous {
                    let fraction = changed_fraction(old, &current, tolerance, &a["ignore"]);
                    seen.fraction = Some(fraction);
                    if fraction <= threshold {
                        let since = *quiet_since.get_or_insert_with(Instant::now);
                        if since.elapsed() >= stable_for {
                            return Ok(json!({"met":true,"until":"stable","source":"pixels",
                                "waited_seconds":(started.elapsed().as_secs_f64() * 100.0).round() / 100.0,
                                "stable_ms":stable_for.as_millis() as u64,"last_changed_fraction":fraction}));
                        }
                    } else {
                        quiet_since = None;
                    }
                }
                previous = Some(current);
                if Instant::now() >= deadline {
                    return Err(Error::new(
                        "screen_unstable",
                        format!("The screen kept changing for {timeout}s"),
                    )
                    .details(json!({"last_changed_fraction":seen.fraction,"threshold":threshold,"tolerance":tolerance,"stable_ms":stable_for.as_millis() as u64})));
                }
                pause(interval, deadline);
            }
        }
        _ => Err(Error::new(
            "invalid_argument",
            "until is text, text_gone or stable",
        )),
    }
}

/// A `wait_text` or `wait_stable` scenario step as the arguments of `wait`.
pub fn step_arguments(step: &Value, device: &str) -> Result<Value> {
    let mut a = json!({"device":device,"timeout":step["timeout"].as_u64().unwrap_or(10)});
    match s(step, "action") {
        "wait_text" => {
            if step.get("text").is_none() {
                return Err(Error::new(
                    "invalid_argument",
                    "A wait_text step requires text",
                ));
            }
            a["until"] = json!(if s(step, "state") == "absent" {
                "text_gone"
            } else {
                "text"
            });
            a["text"] = step["text"].clone();
            if let Some(source) = step.get("source") {
                a["source"] = source.clone();
            }
        }
        _ => {
            a["until"] = json!("stable");
            if let Some(window) = step.get("duration") {
                a["stable_ms"] = window.clone();
            }
            for key in ["threshold", "tolerance", "ignore"] {
                if let Some(value) = step.get(key) {
                    a[key] = value.clone();
                }
            }
        }
    }
    Ok(a)
}

// ---- Selectors by text, for screens without a hierarchy -----------------------------

const ACCURACY: &str = "Found by reading the screenshot with OCR, not by an element: the text can be misread or missed, so check confidence";

/// `testing.find` by text on a screen with no hierarchy: polls screenshots
/// until the text is (or is not) there.
pub fn ocr_find(screen: &mut dyn Screen, recognizer: &mut dyn Recognizer, a: &Value) -> Result {
    let device = s(a, "device");
    let selector = &a["selector"];
    if !ocr::supports(selector) {
        return Err(Error::new(
            "unsupported_selector",
            format!("{device} has no UI hierarchy; a selector there can have text or text_contains, and index"),
        )
        .remedy("Use text or text_contains, or tap and swipe at pixel coordinates"));
    }
    let expected = a["state"].as_str().unwrap_or("present");
    let timeout = a["timeout"].as_u64().unwrap_or(10);
    let deadline = Instant::now() + Duration::from_secs(timeout);
    let work = scratch()?;
    let frame = work.path().join("frame.png");
    loop {
        check_cancelled()?;
        screen.capture(&frame)?;
        let reading = ocr::read(
            recognizer,
            &frame,
            language(a)?,
            "auto",
            a["min_confidence"].as_f64().unwrap_or(40.0),
            None,
        )?;
        let found = ocr::select(&reading.lines, selector, b(a, "fuzzy"));
        if found.is_empty() == (expected == "absent") {
            return Ok(
                json!({"matches":found.iter().map(ocr::Found::value).collect::<Vec<_>>(),"state":expected,
                "device":device,"source":"ocr","pixel_based":true,"accuracy":ACCURACY}),
            );
        }
        if Instant::now() >= deadline {
            return Err(Error::new("element_timeout", format!("Text did not become {expected} within {timeout}s"))
                .details(json!({"selector":selector,"matches":found.iter().map(ocr::Found::value).collect::<Vec<_>>(),
                    "device":device,"source":"ocr","seen":reading.lines.iter().take(20).map(Line::text).collect::<Vec<_>>()})));
        }
        pause(Duration::from_millis(300), deadline);
    }
}

/// `testing.action` by text: a click or long click at the middle of the one
/// text found. A field cannot be filled this way.
pub fn ocr_action(screen: &mut dyn Screen, recognizer: &mut dyn Recognizer, a: &Value) -> Result {
    let action = s(a, "action");
    if action == "fill" {
        return Err(Error::new(
            "ocr_action_unsupported",
            "A field cannot be filled by text read off the screen",
        )
        .remedy("Tap the field with a click, then type with a text step or ios.emulator.control"));
    }
    let mut query = a.clone();
    query["state"] = json!("present");
    let result = ocr_find(screen, recognizer, &query)?;
    let matches = result["matches"].as_array().cloned().unwrap_or_default();
    if matches.len() != 1 {
        return Err(Error::new(
            "ambiguous_selector",
            format!(
                "Selector matched {} places; refine it or specify index",
                matches.len()
            ),
        )
        .details(result));
    }
    let element = &matches[0];
    let (x, y) = (
        element["rect"]["x"].as_i64().unwrap_or(-1),
        element["rect"]["y"].as_i64().unwrap_or(-1),
    );
    let hold = (action == "long_click").then(|| a["duration"].as_u64().unwrap_or(800));
    screen.tap(x, y, hold)?;
    Ok(
        json!({"device":s(a, "device"),"action":action,"element":element,"source":"ocr","pixel_based":true,"accuracy":ACCURACY}),
    )
}

// ---- Dispatch --------------------------------------------------------------------------

pub fn handle(name: &str, a: &Value) -> Result {
    let device = s(a, "device");
    match name {
        "screen.marks" => {
            let store = Store::home()?;
            with_screen(device, |screen| {
                marks(a, screen, &mut ocr::Tesseract, &store)
            })
        }
        "screen.tap" => {
            let store = Store::home()?;
            with_screen(device, |screen| tap(a, screen, &store))
        }
        "screen.tree" => {
            let store = Store::home()?;
            with_screen(device, |screen| tree_text(a, screen, &store))
        }
        "screen.ocr" => {
            if a.get("image").is_some() == a.get("device").is_some() {
                return Err(Error::new(
                    "invalid_argument",
                    "Give either device or image",
                ));
            }
            if a.get("image").is_some() {
                read_text(a, None, &mut ocr::Tesseract)
            } else {
                with_screen(device, |screen| {
                    read_text(a, Some(screen), &mut ocr::Tesseract)
                })
            }
        }
        "screen.diff" => {
            if crate::screen_export::is_bundle(s(a, "before")) {
                return crate::screen_export::diff_bundles(a);
            }
            if a.get("after").is_none() && a.get("device").is_none() {
                return Err(Error::new(
                    "invalid_argument",
                    "Give after, or a device to compare with",
                ));
            }
            if a.get("device").is_some() {
                with_screen(device, |screen| diff(a, Some(screen), &mut ocr::Tesseract))
            } else {
                diff(a, None, &mut ocr::Tesseract)
            }
        }
        "screen.wait" => with_screen(device, |screen| wait(a, screen, &mut ocr::Tesseract)),
        _ => Err(Error::new(
            "invalid_argument",
            format!("{name} is not a screen operation"),
        )),
    }
}

/// A scenario step on a screen: `wait_text`, `wait_stable`.
pub fn step(
    screen: &mut dyn Screen,
    recognizer: &mut dyn Recognizer,
    step: &Value,
    device: &str,
) -> Result {
    wait(&step_arguments(step, device)?, screen, recognizer)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;

    const LOGIN: &str = include_str!("../tests/fixtures/screen_login.xml");
    const FILLED: &str = include_str!("../tests/fixtures/screen_login_filled.xml");

    fn solid(width: u32, height: u32, shade: u8) -> Image {
        Image {
            width,
            height,
            pixels: [shade, shade, shade, 255].repeat((width * height) as usize),
        }
    }

    fn block(image: &mut Image, x: u32, y: u32, w: u32, h: u32, shade: u8) {
        for row in y..y + h {
            for col in x..x + w {
                let at = ((row * image.width + col) * 4) as usize;
                image.pixels[at..at + 3].fill(shade);
            }
        }
    }

    /// A screen with a queue of pictures and hierarchies, and a log of taps.
    struct Fake {
        pictures: VecDeque<Image>,
        hierarchies: VecDeque<Option<String>>,
        taps: Vec<(i64, i64, Option<u64>)>,
    }
    impl Fake {
        fn new(pictures: Vec<Image>, hierarchies: Vec<Option<&str>>) -> Self {
            Fake {
                pictures: pictures.into(),
                hierarchies: hierarchies
                    .into_iter()
                    .map(|h| h.map(String::from))
                    .collect(),
                taps: Vec::new(),
            }
        }
    }
    impl Screen for Fake {
        fn capture(&mut self, path: &Path) -> Result<(u32, u32)> {
            let picture = if self.pictures.len() > 1 {
                self.pictures.pop_front().unwrap()
            } else {
                self.pictures[0].clone()
            };
            fs::write(path, visual::encode(&picture)?)?;
            Ok((picture.width, picture.height))
        }
        fn hierarchy(&mut self) -> Result<Option<String>> {
            Ok(if self.hierarchies.len() > 1 {
                self.hierarchies.pop_front().unwrap()
            } else {
                self.hierarchies[0].clone()
            })
        }
        fn tap(&mut self, x: i64, y: i64, hold: Option<u64>) -> Result {
            self.taps.push((x, y, hold));
            Ok(json!({}))
        }
    }

    struct NoOcr;
    impl Recognizer for NoOcr {
        fn tsv(&mut self, _: &Path, _: &str) -> Result<String> {
            Err(Error::new("ocr_unavailable", "not in this test"))
        }
    }

    /// Answers the TSV for the nth call, whatever the picture is.
    struct Script(VecDeque<String>);
    impl Recognizer for Script {
        fn tsv(&mut self, _: &Path, _: &str) -> Result<String> {
            Ok(if self.0.len() > 1 {
                self.0.pop_front().unwrap()
            } else {
                self.0[0].clone()
            })
        }
    }
    fn tsv(rows: &[(i64, i64, i64, i64, f64, &str)]) -> String {
        let mut out = String::from(
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n",
        );
        for (i, (l, t, w, h, c, text)) in rows.iter().enumerate() {
            out.push_str(&format!(
                "5\t1\t1\t1\t1\t{}\t{l}\t{t}\t{w}\t{h}\t{c}\t{text}\n",
                i + 1
            ));
        }
        out
    }
    const EMPTY: &str = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n";

    fn store() -> (tempfile::TempDir, Store) {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::at(dir.path().join("marks"));
        (dir, store)
    }

    #[test]
    fn marks_draw_numbered_boxes_and_list_the_elements() {
        let (dir, store) = store();
        let mut fake = Fake::new(vec![solid(1080, 2400, 255)], vec![Some(LOGIN)]);
        let out = dir.path().join("marked.png");
        let xml = dir.path().join("login.xml");
        let result = marks(
            &json!({"device":"emulator-5554","path":out,"xml_path":xml}),
            &mut fake,
            &mut NoOcr,
            &store,
        )
        .unwrap();
        assert_eq!(result["source"], "hierarchy");
        assert_eq!(result["count"], 6);
        assert_eq!(result["marks"][4]["label"], "Sign in");
        assert_eq!(
            result["marks"][4]["resource_id"],
            "dev.example.app:id/sign_in"
        );
        assert_eq!(
            (result["width"].as_u64(), result["height"].as_u64()),
            (Some(1080), Some(2400))
        );
        assert_eq!(fs::read_to_string(&xml).unwrap(), LOGIN);
        // The picture is white; the box of mark 5 (Sign in) is drawn on its edge, and its tag inside.
        let drawn = visual::decode(&fs::read(&out).unwrap()).unwrap();
        let at = |x: u32, y: u32| {
            let i = ((y * 1080 + x) * 4) as usize;
            [drawn.pixels[i], drawn.pixels[i + 1], drawn.pixels[i + 2]]
        };
        assert_eq!(at(500, 1440), COLOURS[5 % COLOURS.len()]);
        assert_eq!(at(500, 1500), [255, 255, 255]);
        assert_eq!(at(142, 1442), COLOURS[5 % COLOURS.len()]);
        assert_eq!(at(10, 10), [255, 255, 255]);
    }

    #[test]
    fn marks_from_pixels_are_the_lines_of_text() {
        let (dir, store) = store();
        let mut fake = Fake::new(vec![solid(600, 800, 250)], vec![None]);
        let mut ocr = Script(VecDeque::from([
            tsv(&[
                (50, 100, 120, 40, 95.0, "General"),
                (50, 300, 100, 40, 90.0, "About"),
                (50, 500, 30, 30, 15.0, "x"),
            ]),
            EMPTY.to_string(),
        ]));
        let out = dir.path().join("marked.png");
        let result = marks(
            &json!({"device":"virtual-iphone","path":out}),
            &mut fake,
            &mut ocr,
            &store,
        )
        .unwrap();
        assert_eq!(result["source"], "ocr");
        assert_eq!(result["count"], 2);
        assert_eq!(result["marks"][1]["label"], "About");
        assert_eq!(result["marks"][1]["bounds"]["x"], 100);
        assert_eq!(result["marks"][1]["source"], "ocr");
        // The hierarchy is not asked for when it does not exist.
        let error = marks(
            &json!({"device":"virtual-iphone","path":out,"source":"hierarchy"}),
            &mut fake,
            &mut ocr,
            &store,
        )
        .unwrap_err();
        assert_eq!(error.code, "ui_hierarchy_unavailable");
    }

    #[test]
    fn tapping_a_mark_checks_the_hierarchy_and_taps_its_centre() {
        let (dir, store) = store();
        let mut fake = Fake::new(vec![solid(1080, 2400, 255)], vec![Some(LOGIN)]);
        marks(
            &json!({"device":"d","path":dir.path().join("m.png")}),
            &mut fake,
            &mut NoOcr,
            &store,
        )
        .unwrap();
        let result = tap(&json!({"device":"d","mark":5}), &mut fake, &store).unwrap();
        assert_eq!(result["tapped"], json!({"x":540,"y":1500}));
        assert_eq!(result["verified"], true);
        assert_eq!(fake.taps, [(540, 1500, None)]);
        let long = tap(
            &json!({"device":"d","mark":5,"duration":900}),
            &mut fake,
            &store,
        )
        .unwrap();
        assert_eq!(long["long_press"], true);
        assert_eq!(fake.taps[1].2, Some(900));
        assert_eq!(
            tap(&json!({"device":"d","mark":9}), &mut fake, &store)
                .unwrap_err()
                .code,
            "mark_not_found"
        );
        assert_eq!(
            tap(&json!({"device":"other","mark":1}), &mut fake, &store)
                .unwrap_err()
                .code,
            "marks_missing"
        );
    }

    #[test]
    fn a_mark_whose_element_changed_is_not_tapped() {
        let (dir, store) = store();
        let mut fake = Fake::new(
            vec![solid(1080, 2400, 255)],
            vec![Some(LOGIN), Some(FILLED)],
        );
        marks(
            &json!({"device":"d","path":dir.path().join("m.png")}),
            &mut fake,
            &mut NoOcr,
            &store,
        )
        .unwrap();
        // Mark 2 is the email field, whose text has changed in the second look.
        let error = tap(&json!({"device":"d","mark":2}), &mut fake, &store).unwrap_err();
        assert_eq!(error.code, "mark_stale");
        assert!(fake.taps.is_empty());
        // Without the check it taps where the element was.
        tap(
            &json!({"device":"d","mark":2,"verify":false}),
            &mut fake,
            &store,
        )
        .unwrap();
        assert_eq!(fake.taps.len(), 1);
    }

    #[test]
    fn a_mark_from_pixels_is_checked_against_the_picture_it_came_from() {
        let (dir, store) = store();
        let mut after = solid(600, 800, 250);
        block(&mut after, 40, 290, 120, 60, 10);
        let mut fake = Fake::new(
            vec![solid(600, 800, 250), solid(600, 800, 250), after],
            vec![None],
        );
        let mut ocr = Script(VecDeque::from([
            tsv(&[(50, 300, 100, 40, 90.0, "About")]),
            EMPTY.to_string(),
        ]));
        marks(
            &json!({"device":"virtual-iphone","path":dir.path().join("m.png")}),
            &mut fake,
            &mut ocr,
            &store,
        )
        .unwrap();
        let ok = tap(
            &json!({"device":"virtual-iphone","mark":1}),
            &mut fake,
            &store,
        )
        .unwrap();
        assert_eq!(ok["tapped"], json!({"x":100,"y":320}));
        // The next picture has a dark block where the text was.
        let stale = tap(
            &json!({"device":"virtual-iphone","mark":1}),
            &mut fake,
            &store,
        )
        .unwrap_err();
        assert_eq!(stale.code, "mark_stale");
        assert_eq!(fake.taps.len(), 1);
    }

    #[test]
    fn the_tree_is_short_text_with_numbers_and_makes_marks_tappable() {
        let (_dir, store) = store();
        let mut fake = Fake::new(vec![solid(10, 10, 0)], vec![Some(LOGIN)]);
        let result = tree_text(&json!({"device":"d","max_chars":2000}), &mut fake, &store).unwrap();
        assert_eq!(result["marks"], 6);
        assert_eq!(result["truncated"], false);
        assert!(s(&result, "text").contains("Button \"Sign in\" id=sign_in #5"));
        assert!(result["chars"].as_u64().unwrap() < 2000);
        tap(&json!({"device":"d","mark":1}), &mut fake, &store).unwrap();
        let none = Fake::new(vec![solid(10, 10, 0)], vec![None]);
        let mut none = none;
        assert_eq!(
            tree_text(&json!({"device":"v"}), &mut none, &store)
                .unwrap_err()
                .code,
            "ui_hierarchy_unavailable"
        );
        assert_eq!(
            tree_text(&json!({"device":"d","max_chars":10}), &mut fake, &store)
                .unwrap_err()
                .code,
            "invalid_argument"
        );
    }

    #[test]
    fn two_hierarchies_on_disk_are_compared_by_element() {
        let dir = tempfile::tempdir().unwrap();
        let (a, b) = (dir.path().join("a.xml"), dir.path().join("b.xml"));
        fs::write(&a, LOGIN).unwrap();
        fs::write(&b, FILLED).unwrap();
        let result = diff(&json!({"before":a,"after":b}), None, &mut NoOcr).unwrap();
        assert_eq!(result["kind"], "hierarchy");
        assert_eq!(result["counts"]["changed"], 2);
        // A hierarchy cannot be compared with a screenshot.
        let png = dir.path().join("a.png");
        fs::write(&png, visual::encode(&solid(4, 4, 0)).unwrap()).unwrap();
        assert_eq!(
            diff(&json!({"before":a,"after":png}), None, &mut NoOcr)
                .unwrap_err()
                .code,
            "invalid_argument"
        );
    }

    #[test]
    fn pixel_changes_are_grouped_into_regions_with_a_position() {
        let before = solid(300, 600, 255);
        let mut after = before.clone();
        block(&mut after, 20, 300, 100, 40, 0);
        block(&mut after, 130, 300, 60, 40, 0);
        block(&mut after, 200, 560, 40, 20, 0);
        // Inside the status bar region and ignored.
        block(&mut after, 250, 2, 30, 10, 0);
        let result = pixel_regions(
            &before,
            &after,
            24,
            &json!([{"x":240,"y":0,"width":60,"height":20}]),
            12,
        );
        assert_eq!(result["identical"], false);
        assert_eq!(result["region_count"], 2);
        let first = &result["regions"][0];
        assert_eq!(
            (
                first["x"].as_u64(),
                first["y"].as_u64(),
                first["width"].as_u64(),
                first["height"].as_u64()
            ),
            (Some(20), Some(300), Some(170), Some(40))
        );
        assert_eq!(first["position"], "middle centre");
        assert_eq!(first["changed_pixels"], 100 * 40 + 60 * 40);
        assert_eq!(result["regions"][1]["position"], "bottom right");
        assert!(s(&result, "summary").starts_with("2 regions changed"));
        assert_eq!(
            pixel_regions(&before, &before, 24, &Value::Null, 12)["summary"],
            "No pixel changed."
        );
    }

    #[test]
    fn a_small_difference_under_the_tolerance_is_not_a_change() {
        let before = solid(100, 100, 100);
        let after = solid(100, 100, 110);
        assert_eq!(
            pixel_regions(&before, &after, 24, &Value::Null, 5)["identical"],
            true
        );
        assert_eq!(
            pixel_regions(&before, &after, 5, &Value::Null, 5)["region_count"],
            1
        );
    }

    #[test]
    fn screens_of_different_sizes_say_so_instead_of_comparing() {
        let dir = tempfile::tempdir().unwrap();
        let (a, b) = (dir.path().join("a.png"), dir.path().join("b.png"));
        fs::write(&a, visual::encode(&solid(10, 20, 0)).unwrap()).unwrap();
        fs::write(&b, visual::encode(&solid(20, 10, 0)).unwrap()).unwrap();
        let result = diff(&json!({"before":a,"after":b}), None, &mut NoOcr).unwrap();
        assert_eq!(result["size_changed"], true);
        assert!(s(&result, "summary").contains("rotated"));
    }

    #[test]
    fn a_diff_with_text_lists_lines_added_and_removed() {
        let dir = tempfile::tempdir().unwrap();
        let (a, b) = (dir.path().join("a.png"), dir.path().join("b.png"));
        fs::write(&a, visual::encode(&solid(200, 200, 255)).unwrap()).unwrap();
        let mut changed = solid(200, 200, 255);
        block(&mut changed, 10, 100, 80, 20, 0);
        fs::write(&b, visual::encode(&changed).unwrap()).unwrap();
        let mut ocr = Script(VecDeque::from([
            tsv(&[
                (10, 10, 80, 20, 90.0, "Title"),
                (10, 100, 80, 20, 90.0, "Loading"),
            ]),
            EMPTY.to_string(),
            tsv(&[
                (10, 10, 80, 20, 90.0, "Title"),
                (10, 100, 80, 20, 90.0, "Done"),
            ]),
            EMPTY.to_string(),
        ]));
        let result = diff(&json!({"before":a,"after":b,"text":true}), None, &mut ocr).unwrap();
        assert_eq!(result["text"]["removed"][0]["text"], "Loading");
        assert_eq!(result["text"]["added"][0]["text"], "Done");
        assert_eq!(result["text"]["added"].as_array().unwrap().len(), 1);
        assert_eq!(result["region_count"], 1);
    }

    #[test]
    fn waiting_for_text_uses_the_hierarchy_where_there_is_one() {
        let mut fake = Fake::new(vec![solid(10, 10, 0)], vec![Some(LOGIN), Some(FILLED)]);
        let result = wait(
            &json!({"device":"d","until":"text","text":"valid password","timeout":5,"interval_ms":50}),
            &mut fake,
            &mut NoOcr,
        )
        .unwrap();
        assert_eq!(result["source"], "hierarchy");
        assert_eq!(result["matches"][0]["text"], "Enter a valid password");
        // Gone: the message is not in the first look and the wait is met at once.
        let mut gone = Fake::new(vec![solid(10, 10, 0)], vec![Some(LOGIN)]);
        let met = wait(
            &json!({"device":"d","until":"text_gone","text":"valid password","timeout":1}),
            &mut gone,
            &mut NoOcr,
        )
        .unwrap();
        assert_eq!(met["met"], true);
    }

    #[test]
    fn waiting_for_text_times_out_with_what_was_seen() {
        let mut fake = Fake::new(vec![solid(10, 10, 0)], vec![Some(LOGIN)]);
        let error = wait(
            &json!({"device":"d","until":"text","text":"Payment complete","timeout":0}),
            &mut fake,
            &mut NoOcr,
        )
        .unwrap_err();
        assert_eq!(error.code, "text_timeout");
        let seen = error.details.unwrap()["seen"].to_string();
        assert!(seen.contains("Sign in"), "{seen}");
    }

    #[test]
    fn without_a_hierarchy_the_wait_reads_the_screen() {
        let mut fake = Fake::new(vec![solid(300, 300, 255)], vec![None]);
        let mut ocr = Script(VecDeque::from([
            tsv(&[(10, 10, 100, 30, 90.0, "Loading")]),
            EMPTY.to_string(),
            tsv(&[
                (10, 10, 100, 30, 90.0, "Welcome"),
                (10, 80, 60, 30, 83.0, "back"),
            ]),
            EMPTY.to_string(),
        ]));
        let result = wait(
            &json!({"device":"virtual-iphone","until":"text","text":"welcome back","timeout":5,"interval_ms":50}),
            &mut fake,
            &mut ocr,
        );
        // "Welcome" and "back" are on different lines here, so the phrase is not found: the wait times out.
        assert_eq!(result.unwrap_err().code, "text_timeout");
        let mut again = Fake::new(vec![solid(300, 300, 255)], vec![None]);
        let mut ocr = Script(VecDeque::from([
            tsv(&[(10, 10, 100, 30, 90.0, "Loading")]),
            EMPTY.to_string(),
            tsv(&[
                (10, 10, 100, 30, 90.0, "Welcome"),
                (115, 10, 60, 30, 83.0, "back"),
            ]),
            EMPTY.to_string(),
        ]));
        let result = wait(
            &json!({"device":"virtual-iphone","until":"text","text":"welcome back","timeout":5,"interval_ms":50}),
            &mut again,
            &mut ocr,
        )
        .unwrap();
        assert_eq!(result["source"], "ocr");
        assert_eq!(result["matches"][0]["words"], 2);
    }

    #[test]
    fn a_screen_is_stable_after_it_stops_changing_for_the_window() {
        let mut moving = solid(100, 100, 255);
        block(&mut moving, 10, 10, 30, 30, 0);
        let mut fake = Fake::new(
            vec![solid(100, 100, 255), moving.clone(), solid(100, 100, 255)],
            vec![None],
        );
        let started = Instant::now();
        let result = wait(
            &json!({"device":"d","until":"stable","stable_ms":300,"timeout":10,"interval_ms":50}),
            &mut fake,
            &mut NoOcr,
        )
        .unwrap();
        assert_eq!(result["source"], "pixels");
        assert!(started.elapsed() >= Duration::from_millis(300));
        // A screen that always changes never settles.
        let frames: Vec<Image> = (0..40u32)
            .map(|i| {
                let mut f = solid(100, 100, 255);
                block(&mut f, i, 0, 20, 100, 0);
                f
            })
            .collect();
        let mut busy = Fake::new(frames, vec![None]);
        let error = wait(
            &json!({"device":"d","until":"stable","stable_ms":500,"timeout":1,"interval_ms":50}),
            &mut busy,
            &mut NoOcr,
        )
        .unwrap_err();
        assert_eq!(error.code, "screen_unstable");
        assert!(
            error.details.unwrap()["last_changed_fraction"]
                .as_f64()
                .unwrap()
                > 0.001
        );
    }

    #[test]
    fn ignored_regions_do_not_count_against_stability() {
        let mut ticking = solid(100, 100, 255);
        block(&mut ticking, 80, 0, 20, 10, 0);
        let mut fake = Fake::new(
            vec![
                solid(100, 100, 255),
                ticking.clone(),
                solid(100, 100, 255),
                ticking,
            ],
            vec![None],
        );
        let result = wait(
            &json!({"device":"d","until":"stable","stable_ms":200,"timeout":10,"interval_ms":50,"ignore":[{"x":70,"y":0,"width":30,"height":20}]}),
            &mut fake,
            &mut NoOcr,
        )
        .unwrap();
        assert_eq!(result["met"], true);
    }

    #[test]
    fn scenario_steps_become_wait_arguments() {
        let text = step_arguments(&json!({"action":"wait_text","text":"Done","state":"absent","timeout":7,"source":"ocr"}), "d").unwrap();
        assert_eq!(
            text,
            json!({"device":"d","timeout":7,"until":"text_gone","text":"Done","source":"ocr"})
        );
        let stable = step_arguments(
            &json!({"action":"wait_stable","duration":1500,"threshold":0.01,"ignore":[]}),
            "d",
        )
        .unwrap();
        assert_eq!(stable["until"], "stable");
        assert_eq!(stable["stable_ms"], 1500);
        assert_eq!(stable["timeout"], 10);
        assert!(step_arguments(&json!({"action":"wait_text"}), "d").is_err());
    }

    #[test]
    fn ocr_of_an_image_reports_words_lines_and_matches() {
        let dir = tempfile::tempdir().unwrap();
        let image = dir.path().join("s.png");
        fs::write(&image, visual::encode(&solid(400, 400, 255)).unwrap()).unwrap();
        let mut ocr = Script(VecDeque::from([
            tsv(&[
                (10, 10, 100, 30, 91.2, "Sign"),
                (115, 10, 30, 30, 88.0, "in"),
                (10, 100, 60, 30, 20.0, "noise"),
            ]),
            EMPTY.to_string(),
        ]));
        let result = read_text(
            &json!({"image":image,"text":"sign in","min_confidence":50}),
            None,
            &mut ocr,
        )
        .unwrap();
        assert_eq!(result["word_count"], 2);
        assert_eq!(result["lines"][0]["text"], "Sign in");
        assert_eq!(result["matches"][0]["rect"]["x"], 77);
        assert_eq!(result["engine"], "tesseract");
        assert!(read_text(&json!({"image":image,"language":"../x"}), None, &mut ocr).is_err());
    }

    #[test]
    fn digits_fit_their_tags_and_stay_inside_the_picture() {
        let mut image = solid(40, 40, 255);
        draw_marks(
            &mut image,
            &[json!({"mark":123,"bounds":{"left":30,"top":30,"right":60,"bottom":60}})],
            1.0,
        );
        assert_eq!(image.pixels.len(), 40 * 40 * 4);
        let (w, h) = tag_size(123, 2);
        assert_eq!((w, h), (2 * 19 + 2, 18));
    }
}
