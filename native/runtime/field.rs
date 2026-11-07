//! Verified field input: `fill` with `verify_value`, behind both
//! `runtime.input` and the `fill` steps of `runtime.actions`.
//!
//! A plain `fill` taps, clears and types and answers what the transport
//! answered. A verified one also establishes which editor took the focus,
//! what it held before and what it holds afterwards, and ends in one of
//! three ways:
//!
//! - **verified**: the focused editor is the intended one and reading its
//!   value back gives exactly the requested text, or it already held that
//!   text and nothing was written (`already_matching`);
//! - **refused before typing** (`field_tap_failed`, `field_not_focused`,
//!   `field_wrong_target`, `field_unsupported`, `field_stale`,
//!   `field_unobservable`): no text was cleared or typed, only the focusing
//!   tap may have reached the device;
//! - **uncertain** (`field_uncertain`): a clear or write was sent and what
//!   it did could not be established: the read-back differs, the reply was
//!   lost, the editor was replaced, or the value is indistinguishable from
//!   the placeholder. Nothing is typed again; the caller observes and decides.
//!
//! XCUITest reports an empty editor's placeholder as its value, so a value
//! equal to the placeholder is never taken as entered text. Secure editors
//! are never read: their value is not requested at all. Text is compared
//! exactly, code point for code point.
//!
//! The algorithm is the same for every transport that can drive it; a
//! transport provides the reads and writes as a [`FieldDriver`]. A transport
//! without one answers `fill_verification_unsupported` and sends nothing.
use super::*;

/// The editor classes whose value can be read back and compared.
pub const SUPPORTED: &[&str] = &[
    "XCUIElementTypeTextField",
    "XCUIElementTypeSearchField",
    "XCUIElementTypeTextView",
];
/// A password editor: typed into on request, never read.
pub const SECURE: &str = "XCUIElementTypeSecureTextField";
/// How long a verified fill waits for the editor after its tap, at most.
pub const FOCUS_MAX_MS: u64 = 10_000;
const FOCUS_DEFAULT_MS: u64 = 2_000;
/// How long the read-back may take to show the written text.
const READBACK_MS: u64 = 1_000;
/// The most of an observed value an error quotes.
const QUOTE_CHARS: usize = 256;

/// How a driver call went wrong, as far as the caller can know.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Fault {
    /// The request never left: no connection, nothing changed.
    NotSent,
    /// The transport answered with a refusal.
    Refused,
    /// The element no longer exists (replaced since it was found).
    Stale,
    /// There is no such element (for the focus: nothing is focused).
    Missing,
    /// The request may have been carried out, but no answer came back.
    Lost,
}
impl Fault {
    fn name(self) -> &'static str {
        match self {
            Fault::NotSent => "not_sent",
            Fault::Refused => "refused",
            Fault::Stale => "stale",
            Fault::Missing => "missing",
            Fault::Lost => "lost_reply",
        }
    }
    fn gone(self) -> bool {
        matches!(self, Fault::Stale | Fault::Missing)
    }
}

#[derive(Debug, Clone)]
pub struct Failure {
    pub fault: Fault,
    pub message: String,
}
impl Failure {
    pub fn new(fault: Fault, message: impl Into<String>) -> Self {
        Self {
            fault,
            message: message.into(),
        }
    }
}
pub type Step<T> = std::result::Result<T, Failure>;

/// What a driver can say about an element without reading its value.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Editor {
    pub class: String,
    pub name: String,
    pub label: String,
    pub placeholder: String,
    /// Left, top, width, height in the space `FieldDriver::tap` answers.
    pub rect: Option<(f64, f64, f64, f64)>,
    pub enabled: bool,
}
impl Editor {
    fn secure(&self) -> bool {
        self.class == SECURE
    }
    fn supported(&self) -> bool {
        SUPPORTED.contains(&self.class.as_str())
    }
    fn contains(&self, (x, y): (f64, f64)) -> bool {
        // Two points of slack for rounding between the tap and the frame.
        self.rect.is_some_and(|(l, t, w, h)| {
            w > 0.0
                && h > 0.0
                && x >= l - 2.0
                && x <= l + w + 2.0
                && y >= t - 2.0
                && y <= t + h + 2.0
        })
    }
    fn value(&self, id: &str) -> Value {
        json!({"element":id,"class":self.class,"name":self.name,"label":self.label,
            "placeholder":!self.placeholder.is_empty(),"enabled":self.enabled,
            "rect":self.rect.map(|(l, t, w, h)| json!({"x":l,"y":t,"width":w,"height":h}))})
    }
}

/// The reads and writes one transport offers for an editor.
pub trait FieldDriver {
    /// Where the facts come from, such as `wda` or `fixture`.
    fn evidence(&self) -> &'static str;
    /// Taps the step's x, y to focus the editor; answers the point tapped in
    /// the space of `Editor::rect`.
    fn tap(&mut self, a: &Value) -> Step<(f64, f64)>;
    /// The element with the keyboard focus, None when nothing has it.
    fn active(&mut self) -> Step<Option<String>>;
    fn describe(&mut self, id: &str) -> Step<Editor>;
    /// The editor's value as the transport reports it (None for none). Never
    /// called for a secure editor.
    fn value(&mut self, id: &str) -> Step<Option<String>>;
    fn clear(&mut self, id: &str) -> Step<()>;
    fn write(&mut self, id: &str, text: &str) -> Step<()>;
    /// One Return to the focused editor.
    fn submit(&mut self) -> Step<()>;
}

/// The capability a transport without a driver advertises.
pub fn not_offered(reason: &str) -> Value {
    json!({"supported":false,"reason":reason})
}

/// The answer of a transport without a driver: nothing was sent.
pub fn unsupported_transport() -> Error {
    Error::new(
        "fill_verification_unsupported",
        "This transport cannot read an editor back, so a verified fill is not offered here; nothing was sent",
    )
    .remedy("Send fill without verify_value and check the result with expect or a fresh observation, or use a session whose input_capabilities.fill_verification.supported is true")
    .details(json!({"typed":false,"tapped":false}))
}

fn invalid(message: &str) -> Error {
    Error::new("invalid_argument", message)
}

/// Refuses arguments a fill does not take, before anything is sent:
/// `submit` and `field_name` belong to a verified fill only.
pub fn check(a: &Value) -> Result<()> {
    let verified = b(a, "verify_value");
    if !verified && (a.get("submit").is_some_and(|v| v == true) || a.get("field_name").is_some()) {
        return Err(invalid("submit and field_name need verify_value true"));
    }
    if s(a, "action") != "fill"
        && (verified || a.get("submit").is_some() || a.get("field_name").is_some())
    {
        return Err(invalid(
            "verify_value, submit and field_name belong to fill",
        ));
    }
    if verified {
        if !a["text"].is_string() {
            return Err(invalid(
                "A verified fill needs text (an empty string clears the field)",
            ));
        }
        if a.get("x").is_none() || a.get("y").is_none() {
            return Err(invalid("A verified fill needs the field's x and y"));
        }
        if a.get("field_name").is_some_and(|v| {
            v.as_str()
                .is_none_or(|n| n.is_empty() || n.chars().count() > 256)
        }) {
            return Err(invalid("field_name is 1 to 256 characters"));
        }
        if a["focus_timeout_ms"]
            .as_u64()
            .is_some_and(|ms| ms > FOCUS_MAX_MS)
        {
            return Err(invalid(
                "focus_timeout_ms is at most 10000 for a verified fill",
            ));
        }
    }
    Ok(())
}

/// What an editor showed, apart from what it holds.
#[derive(Debug, Clone, PartialEq)]
enum Shown {
    Empty,
    /// The value equals the placeholder: empty, or holding exactly that text.
    Placeholder,
    Text(String),
}
impl Shown {
    fn of(value: Option<&str>, placeholder: &str) -> Self {
        match value {
            None | Some("") => Shown::Empty,
            Some(v) if !placeholder.is_empty() && v == placeholder => Shown::Placeholder,
            Some(v) => Shown::Text(v.to_string()),
        }
    }
    fn name(&self) -> &'static str {
        match self {
            Shown::Empty => "empty",
            Shown::Placeholder => "placeholder",
            Shown::Text(_) => "text",
        }
    }
    fn evidence(&self) -> Value {
        match self {
            Shown::Text(v) => json!({"state":"text","chars":v.chars().count()}),
            other => json!({"state":other.name(),"chars":0}),
        }
    }
    fn quoted(&self) -> Value {
        match self {
            Shown::Text(v) => {
                let chars = v.chars().count();
                json!({"state":"text","chars":chars,"value":v.chars().take(QUOTE_CHARS).collect::<String>(),"quoted_in_full":chars <= QUOTE_CHARS})
            }
            other => json!({"state":other.name(),"chars":0}),
        }
    }
}

enum Verdict {
    Exact,
    /// Equal to the placeholder as well as the text: cannot be told apart.
    Ambiguous,
    Mismatch,
}
fn verdict(shown: &Shown, text: &str) -> Verdict {
    match (shown, text.is_empty()) {
        // An emptied editor shows its placeholder: no entered text is left.
        (Shown::Empty | Shown::Placeholder, true) => Verdict::Exact,
        (Shown::Text(_), true) => Verdict::Mismatch,
        (Shown::Text(v), false) if v == text => Verdict::Exact,
        (Shown::Placeholder, false) => Verdict::Ambiguous,
        _ => Verdict::Mismatch,
    }
}

/// One clear, write or submit sent, and how it ended.
fn sent(op: &str, outcome: &str) -> Value {
    json!({"op":op,"outcome":outcome})
}

struct Run {
    evidence: &'static str,
    editor: Option<Value>,
    writes: Vec<Value>,
    polls: u64,
    started: Instant,
}
impl Run {
    fn base(&self) -> Value {
        json!({"evidence":self.evidence,"writes":self.writes,"focus_polls":self.polls,
            "elapsed_ms":self.started.elapsed().as_millis() as u64,
            "editor":self.editor.clone().unwrap_or(Value::Null),"retried":false})
    }
    /// Nothing was cleared or typed.
    fn refused(&self, error: Error, extra: Value) -> Error {
        let mut details = self.base();
        details["typed"] = json!(false);
        details["tapped"] = json!(error.code != "field_tap_failed");
        merge(&mut details, extra);
        error.details(details)
    }
    /// Something was sent and what it did is not known.
    fn uncertain(&self, reason: &str, message: String, extra: Value) -> Error {
        let mut details = self.base();
        details["outcome"] = json!("unknown");
        details["reason"] = json!(reason);
        details["typed"] = json!("unknown");
        merge(&mut details, extra);
        Error::new("field_uncertain", message)
            .remedy("Do not type it again blindly: the field may already hold all or part of the text. Observe the field (screen.tree or runtime.observe) and decide from what it shows")
            .details(details)
    }
}
fn merge(into: &mut Value, extra: Value) {
    if let (Some(into), Some(extra)) = (into.as_object_mut(), extra.as_object()) {
        for (k, v) in extra {
            into.insert(k.clone(), v.clone());
        }
    }
}

/// A verified fill. See the module documentation for its outcomes.
pub fn fill(driver: &mut dyn FieldDriver, a: &Value) -> Result {
    check(a)?;
    let text = s(a, "text").to_string();
    let wanted_name = a["field_name"].as_str().map(str::to_string);
    let wait = Duration::from_millis(
        a["focus_timeout_ms"]
            .as_u64()
            .unwrap_or(FOCUS_DEFAULT_MS)
            .min(FOCUS_MAX_MS),
    );
    let submit = b(a, "submit");
    let started = Instant::now();
    let mut run = Run {
        evidence: driver.evidence(),
        editor: None,
        writes: Vec::new(),
        polls: 0,
        started,
    };
    let point = driver.tap(a).map_err(|f| {
        run.refused(
            Error::new("field_tap_failed", format!("The tap that focuses the field failed ({}): {}", f.fault.name(), f.message)).remedy("Nothing was typed. Observe the screen and try again once the session answers"),
            json!({"fault":f.fault.name(),"tapped":if f.fault == Fault::NotSent {json!(false)} else {json!("unknown")}}),
        )
    })?;
    // The editor that took the focus, waited for within the bound.
    let end = Instant::now() + wait;
    let mut other: Option<(String, Editor)> = None;
    let (id, editor) = loop {
        check_cancelled()?;
        run.polls += 1;
        match driver.active() {
            Ok(Some(id)) => match driver.describe(&id) {
                Ok(editor) => {
                    let intended = match &wanted_name {
                        Some(name) => editor.name == *name || editor.label == *name,
                        None => editor.contains(point),
                    };
                    if intended {
                        break (id, editor);
                    }
                    other = Some((id, editor));
                }
                // Replaced while it took the focus: look again.
                Err(f) if f.fault.gone() => {}
                Err(f) => {
                    return Err(run.refused(
            Error::new("field_unobservable", format!("The focused element could not be read ({}): {}", f.fault.name(), f.message)).remedy("Nothing was typed. Observe the screen; send fill without verify_value only if the field cannot be read"),
            json!({"stage":"describe","fault":f.fault.name()}),
        ));
                }
            },
            Ok(None) => {}
            Err(f) => {
                return Err(run.refused(
            Error::new("field_unobservable", format!("The input focus could not be read ({}): {}", f.fault.name(), f.message)).remedy("Nothing was typed. Observe the screen and try again once the session answers"),
            json!({"stage":"focus","fault":f.fault.name()}),
        ));
            }
        }
        if Instant::now() >= end {
            let waited = json!({"waited_ms":started.elapsed().as_millis() as u64,"field_name":wanted_name,
                "tapped_at":{"x":point.0,"y":point.1}});
            return Err(match other {
                Some((id, editor)) => {
                    let mut extra = waited;
                    extra["focused"] = editor.value(&id);
                    run.refused(
            Error::new("field_wrong_target", match &wanted_name {
                            Some(name) => format!("The focused element is not the field named {name:?}; nothing was typed"),
                            None => "The focused element does not cover the tapped point, so it is not the field that was tapped; nothing was typed".to_string(),
                        }).remedy("Observe the screen, tap the field itself (or name it with field_name when it moves as it takes the focus) and fill again"),
            extra,
        )
                }
                None => run.refused(
            Error::new("field_not_focused", "No field took the focus within the wait; nothing was typed".to_string()).remedy("Tap the field itself, wait longer with focus_timeout_ms, or observe the screen first"),
            waited,
        ),
            });
        }
        thread::sleep(Duration::from_millis(60).min(end.saturating_duration_since(Instant::now())));
    };
    run.editor = Some(editor.value(&id));
    if editor.secure() {
        return Err(run.refused(
            Error::new("field_unsupported", "The focused field is a secure (password) field: its value is never read, so the fill cannot be verified; nothing was typed".to_string()).remedy("Send fill without verify_value to type into it unverified, and check the screen that follows instead"),
            json!({"reason":"secure_field"}),
        ));
    }
    if !editor.supported() {
        return Err(run.refused(
            Error::new("field_unsupported", format!("The focused element is a {}, not a text field whose value can be read back; nothing was typed", editor.class)).remedy("Observe the screen; for content without a readable editor send fill or text without verify_value"),
            json!({"reason":"not_a_text_editor"}),
        ));
    }
    if !editor.enabled {
        return Err(run.refused(
            Error::new(
                "field_unsupported",
                "The focused field is disabled; nothing was typed".to_string(),
            )
            .remedy("Observe the screen and fill once the field is enabled"),
            json!({"reason":"disabled"}),
        ));
    }
    let before = match driver.value(&id) {
        Ok(value) => Shown::of(value.as_deref(), &editor.placeholder),
        Err(f) if f.fault.gone() => {
            return Err(run.refused(
                Error::new(
                    "field_stale",
                    "The field was replaced before anything was typed; nothing was typed"
                        .to_string(),
                )
                .remedy("Observe the screen and fill the field that is there now"),
                json!({"stage":"before","fault":f.fault.name()}),
            ));
        }
        Err(f) => {
            return Err(run.refused(
            Error::new("field_unobservable", format!("The field's value could not be read ({}): {}; nothing was typed", f.fault.name(), f.message)).remedy("Observe the screen; send fill without verify_value only if this field cannot be read"),
            json!({"stage":"before","fault":f.fault.name()}),
        ));
        }
    };
    let already = matches!(verdict(&before, &text), Verdict::Exact) && before != Shown::Placeholder;
    let mut readback = before.clone();
    if !already {
        if before != Shown::Empty {
            match driver.clear(&id) {
                Ok(()) => run.writes.push(sent("clear", "answered")),
                Err(f) if f.fault == Fault::NotSent => {
                    return Err(run.refused(
                        Error::new(
                            "field_unobservable",
                            format!(
                                "The clear could not be sent: {}; nothing was typed",
                                f.message
                            ),
                        )
                        .remedy("Observe the screen and try again once the session answers"),
                        json!({"stage":"clear","fault":f.fault.name()}),
                    ));
                }
                Err(f) if f.fault.gone() => {
                    return Err(run.refused(
                        Error::new(
                            "field_stale",
                            "The field was replaced before it was cleared; nothing was typed"
                                .to_string(),
                        )
                        .remedy("Observe the screen and fill the field that is there now"),
                        json!({"stage":"clear","fault":f.fault.name()}),
                    ));
                }
                Err(f) => {
                    run.writes.push(sent("clear", f.fault.name()));
                    return Err(run.uncertain(
                        "clear_unknown",
                        format!(
                            "The clear was sent, but its outcome is unknown ({}): {}",
                            f.fault.name(),
                            f.message
                        ),
                        json!({"before":before.evidence()}),
                    ));
                }
            }
        }
        if !text.is_empty() {
            let partial = !run.writes.is_empty();
            match driver.write(&id, &text) {
                Ok(()) => run.writes.push(sent("value", "answered")),
                Err(f) if !partial && f.fault == Fault::NotSent => {
                    return Err(run.refused(
                        Error::new(
                            "field_unobservable",
                            format!(
                                "The text could not be sent: {}; nothing was typed",
                                f.message
                            ),
                        )
                        .remedy("Observe the screen and try again once the session answers"),
                        json!({"stage":"value","fault":f.fault.name()}),
                    ));
                }
                Err(f) if !partial && f.fault.gone() => {
                    return Err(run.refused(
                        Error::new(
                            "field_stale",
                            "The field was replaced before the text was typed; nothing was typed"
                                .to_string(),
                        )
                        .remedy("Observe the screen and fill the field that is there now"),
                        json!({"stage":"value","fault":f.fault.name()}),
                    ));
                }
                Err(f) => {
                    run.writes.push(sent("value", f.fault.name()));
                    return Err(run.uncertain(
                        if partial {
                            "partial_write"
                        } else {
                            "write_unknown"
                        },
                        format!(
                            "The text was sent, but its outcome is unknown ({}): {}",
                            f.fault.name(),
                            f.message
                        ),
                        json!({"before":before.evidence()}),
                    ));
                }
            }
        }
        // The exact value, read back from the same element.
        let end = Instant::now() + Duration::from_millis(READBACK_MS);
        loop {
            match driver.value(&id) {
                Ok(value) => readback = Shown::of(value.as_deref(), &editor.placeholder),
                Err(f) if f.fault.gone() => {
                    return Err(run.uncertain(
                        "field_replaced",
                        "The text was sent, but the field was replaced before its value could be read back".to_string(),
                        json!({"before":before.evidence(),"fault":f.fault.name()}),
                    ));
                }
                Err(f) => {
                    return Err(run.uncertain(
                        "readback_unavailable",
                        format!("The text was sent, but the field's value could not be read back ({}): {}", f.fault.name(), f.message),
                        json!({"before":before.evidence(),"fault":f.fault.name()}),
                    ));
                }
            }
            match verdict(&readback, &text) {
                Verdict::Exact => break,
                Verdict::Ambiguous => {
                    return Err(run.uncertain(
                        "value_matches_placeholder",
                        "The field shows exactly its placeholder, which is also the requested text: the entered value cannot be told apart from an empty field".to_string(),
                        json!({"before":before.evidence(),"observed":readback.evidence()}),
                    ));
                }
                Verdict::Mismatch if Instant::now() >= end => {
                    return Err(run.uncertain(
                        "value_mismatch",
                        "The text was sent, but the field's value read back is not exactly the requested text".to_string(),
                        json!({"before":before.evidence(),"observed":readback.quoted(),"expected_chars":text.chars().count()}),
                    ));
                }
                Verdict::Mismatch => thread::sleep(Duration::from_millis(80)),
            }
        }
    }
    let mut submitted = false;
    if submit {
        // Return goes to whatever has the focus, so it must still be this field.
        match driver.active() {
            Ok(Some(now)) if now == id => {}
            other => {
                let focus = match other {
                    Ok(Some(_)) => "another element",
                    Ok(None) => "nothing",
                    Err(_) => "unknown",
                };
                let mut details = run.base();
                details["value_verified"] = json!(true);
                details["submitted"] = json!(false);
                details["focus"] = json!(focus);
                return Err(Error::new(
                    "field_submit_not_sent",
                    format!("The value was verified, but the focus is now on {focus}, so Return was not sent"),
                )
                .remedy("The field holds the text; do not fill it again. Observe the screen and submit with the screen's own button or a key step")
                .details(details));
            }
        }
        match driver.submit() {
            Ok(()) => {
                run.writes.push(sent("submit", "answered"));
                submitted = true;
            }
            Err(f) if f.fault == Fault::NotSent => {
                let mut details = run.base();
                details["value_verified"] = json!(true);
                details["submitted"] = json!(false);
                return Err(Error::new(
                    "field_submit_not_sent",
                    format!("The value was verified, but Return could not be sent: {}", f.message),
                )
                .remedy("The field holds the text; do not fill it again. Observe the screen and submit once the session answers")
                .details(details));
            }
            Err(f) => {
                run.writes.push(sent("submit", f.fault.name()));
                return Err(run.uncertain(
                    "submit_unknown",
                    format!("The value was verified and Return was sent, but its outcome is unknown ({}): {}", f.fault.name(), f.message),
                    json!({"value_verified":true,"submitted":"unknown"}),
                ));
            }
        }
    }
    let mut result = run.base();
    result["verified"] = json!(true);
    result["field_status"] = json!(if already {
        "already_matching"
    } else {
        "verified"
    });
    result["already_matching"] = json!(already);
    result["before"] = before.evidence();
    result["value"] = json!({"chars":text.chars().count(),"match":"exact",
        "shows_placeholder":readback == Shown::Placeholder});
    result["submitted"] = json!(submitted);
    result["typed"] = json!(!already && !text.is_empty());
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn placeholders_are_never_taken_for_entered_text() {
        assert_eq!(Shown::of(Some("Search"), "Search"), Shown::Placeholder);
        assert_eq!(Shown::of(Some("Search"), ""), Shown::Text("Search".into()));
        assert_eq!(Shown::of(None, "Search"), Shown::Empty);
        assert!(matches!(
            verdict(&Shown::Placeholder, "Search"),
            Verdict::Ambiguous
        ));
        assert!(matches!(verdict(&Shown::Placeholder, ""), Verdict::Exact));
        assert!(matches!(verdict(&Shown::Empty, "a"), Verdict::Mismatch));
        // Exact, code point for code point: a decomposed é is another text.
        assert!(matches!(
            verdict(&Shown::Text("e\u{301}".into()), "\u{e9}"),
            Verdict::Mismatch
        ));
    }
    #[test]
    fn arguments_that_only_a_verified_fill_takes_are_refused_elsewhere() {
        assert!(check(&json!({"action":"fill","x":0.5,"y":0.5,"text":"a","submit":true})).is_err());
        assert!(
            check(&json!({"action":"fill","x":0.5,"y":0.5,"text":"a","field_name":"q"})).is_err()
        );
        assert!(check(&json!({"action":"text","text":"a","verify_value":true})).is_err());
        assert!(check(&json!({"action":"fill","x":0.5,"y":0.5,"verify_value":true})).is_err());
        assert!(check(&json!({"action":"fill","x":0.5,"y":0.5,"text":"","verify_value":true,"focus_timeout_ms":20000})).is_err());
        assert!(check(&json!({"action":"fill","x":0.5,"y":0.5,"text":"","verify_value":true,"submit":true,"field_name":"q"})).is_ok());
        assert!(check(&json!({"action":"fill","x":0.5,"y":0.5,"text":"a"})).is_ok());
    }
}
