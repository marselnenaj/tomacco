//! What an agent needs from Android's UI hierarchy to understand a screen:
//! the interactive elements as numbered marks, a short text rendering of the
//! tree, and what changed between two hierarchies.
//!
//! These functions read the UIAutomator XML that `android.hierarchy` returns
//! and nothing else, so they run on recorded hierarchies in tests.
use crate::core::*;
use serde_json::{Value, json};

/// A rectangle in pixels: left, top, right, bottom.
pub type Rect = (i64, i64, i64, i64);

#[derive(Debug, Clone, PartialEq)]
pub struct Node {
    pub depth: usize,
    pub parent: Option<usize>,
    pub class: String,
    pub text: String,
    pub description: String,
    pub id: String,
    pub package: String,
    pub rect: Rect,
    pub clickable: bool,
    pub long_clickable: bool,
    pub checkable: bool,
    pub checked: bool,
    pub enabled: bool,
    pub focused: bool,
    pub focusable: bool,
    pub scrollable: bool,
    pub selected: bool,
    pub password: bool,
}

impl Node {
    pub fn editable(&self) -> bool {
        self.class.ends_with("EditText")
            || self.class.ends_with("AutoCompleteTextView")
            || matches!(
                self.class.as_str(),
                "XCUIElementTypeTextField"
                    | "XCUIElementTypeSecureTextField"
                    | "XCUIElementTypeTextView"
                    | "XCUIElementTypeSearchField"
            )
    }
    /// Something a person can press, switch or type into.
    pub fn interactive(&self) -> bool {
        self.clickable || self.long_clickable || self.checkable || self.editable()
    }
    pub fn has_area(&self) -> bool {
        self.rect.2 > self.rect.0 && self.rect.3 > self.rect.1
    }
    /// The class without its package: `Button`, not `android.widget.Button`.
    pub fn short_class(&self) -> &str {
        self.class.rsplit('.').next().unwrap_or("")
    }
    /// The resource id without its package: `login` for `com.example:id/login`.
    pub fn short_id(&self) -> &str {
        self.id.rsplit('/').next().unwrap_or("")
    }
}

fn bounds(text: &str) -> Rect {
    static PATTERN: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let pattern = PATTERN
        .get_or_init(|| regex::Regex::new(r"^\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]$").unwrap());
    match pattern.captures(text) {
        Some(c) => {
            let v = |i: usize| c[i].parse::<i64>().unwrap_or(0);
            (v(1), v(2), v(3), v(4))
        }
        None => (0, 0, 0, 0),
    }
}

/// The nodes of a UIAutomator dump in document order.
pub fn parse(xml: &str) -> Result<Vec<Node>> {
    let document = roxmltree::Document::parse(xml)
        .map_err(|e| Error::new("invalid_hierarchy", e.to_string()))?;
    fn walk(element: roxmltree::Node, parent: Option<usize>, depth: usize, out: &mut Vec<Node>) {
        for child in element.children().filter(|c| c.is_element()) {
            if !child.has_tag_name("node") {
                walk(child, parent, depth, out);
                continue;
            }
            let attribute = |name: &str| child.attribute(name).unwrap_or("").to_string();
            let flag = |name: &str| child.attribute(name) == Some("true");
            out.push(Node {
                depth,
                parent,
                class: attribute("class"),
                text: attribute("text"),
                description: attribute("content-desc"),
                id: attribute("resource-id"),
                package: attribute("package"),
                rect: bounds(child.attribute("bounds").unwrap_or("")),
                clickable: flag("clickable"),
                long_clickable: flag("long-clickable"),
                checkable: flag("checkable"),
                checked: flag("checked"),
                enabled: child.attribute("enabled") != Some("false"),
                focused: flag("focused"),
                focusable: flag("focusable"),
                scrollable: flag("scrollable"),
                selected: flag("selected"),
                password: flag("password"),
            });
            let at = out.len() - 1;
            walk(child, Some(at), depth + 1, out);
        }
    }
    let mut nodes = Vec::new();
    walk(document.root(), None, 0, &mut nodes);
    Ok(nodes)
}

/// Whether a hierarchy is WebDriverAgent's XML of a physical iPhone rather
/// than a UIAutomator dump.
pub fn is_wda(xml: &str) -> bool {
    let head: String = xml.chars().take(4000).collect();
    head.contains("<AppiumAUT") || head.contains("<XCUIElementType")
}

fn xml_escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

/// WebDriverAgent's page source as a UIAutomator-style dump, so every screen
/// operation reads a physical iPhone the way it reads Android. Points become
/// pixels of the screenshot through `scale`; `rotation` is the quarter turns
/// the frame was taken in. Elements WebDriverAgent marks invisible keep no
/// area, so they are never marked or tapped.
pub fn wda_to_uiautomator(xml: &str, scale: f64, rotation: u64) -> Result<String> {
    let document = roxmltree::Document::parse(xml)
        .map_err(|e| Error::new("invalid_hierarchy", e.to_string()))?;
    let scale = if scale.is_finite() && scale > 0.0 { scale } else { 1.0 };
    let mut out = format!("<?xml version='1.0' encoding='UTF-8'?><hierarchy rotation=\"{rotation}\" source=\"wda\">");
    fn walk(element: roxmltree::Node, scale: f64, out: &mut String) {
        for child in element.children().filter(|c| c.is_element()) {
            let kind = child.tag_name().name();
            if !kind.starts_with("XCUIElementType") {
                walk(child, scale, out);
                continue;
            }
            let attribute = |name: &str| child.attribute(name).unwrap_or("");
            let short = kind.trim_start_matches("XCUIElementType");
            let visible = attribute("visible") != "false";
            let px = |name: &str| (attribute(name).parse::<f64>().unwrap_or(0.0) * scale).round() as i64;
            let (l, t, w, h) = (px("x"), px("y"), px("width"), px("height"));
            let bounds = if visible && w > 0 && h > 0 {
                format!("[{l},{t}][{},{}]", l + w, t + h)
            } else {
                "[0,0][0,0]".to_string()
            };
            let value = attribute("value");
            let label = attribute("label");
            let name = attribute("name");
            let editable = matches!(short, "TextField" | "SecureTextField" | "TextView" | "SearchField");
            let checkable = matches!(short, "Switch" | "Toggle");
            let clickable = matches!(
                short,
                "Button" | "Cell" | "Link" | "Switch" | "Toggle" | "SegmentedControl" | "Tab" | "MenuItem" | "Key"
                    | "PageIndicator" | "Stepper" | "Slider" | "Picker" | "PickerWheel" | "Image" | "Icon"
                    | "CollectionView" | "TabBar" | "NavigationBar"
            ) && !matches!(short, "CollectionView" | "TabBar" | "NavigationBar" | "Image" | "Icon")
                || (matches!(short, "Image" | "Icon") && !name.is_empty() && attribute("accessible") == "true");
            let scrollable = matches!(short, "ScrollView" | "Table" | "CollectionView" | "WebView");
            // UIAutomator's text is what is shown; the description is the accessibility label.
            let text = if checkable || (!editable && value.is_empty()) { label } else { value };
            let description = if label != text { label } else if name != text { name } else { "" };
            let checked = checkable && (value == "1" || value == "true" || value == "on");
            out.push_str(&format!(
                "<node class=\"{}\" text=\"{}\" content-desc=\"{}\" resource-id=\"{}\" package=\"\" bounds=\"{}\" clickable=\"{}\" long-clickable=\"false\" checkable=\"{}\" checked=\"{}\" enabled=\"{}\" focused=\"{}\" focusable=\"{}\" scrollable=\"{}\" selected=\"{}\" password=\"{}\">",
                xml_escape(kind),
                xml_escape(text),
                xml_escape(description),
                xml_escape(name),
                bounds,
                clickable,
                checkable,
                checked,
                attribute("enabled") != "false",
                attribute("focused") == "true",
                editable || clickable,
                scrollable,
                attribute("selected") == "true",
                short == "SecureTextField"
            ));
            walk(child, scale, out);
            out.push_str("</node>");
        }
    }
    walk(document.root(), scale, &mut out);
    out.push_str("</hierarchy>");
    Ok(out)
}

/// The screen's size: the biggest node's right and bottom edge, which is the
/// window's root.
pub fn screen_size(nodes: &[Node]) -> (i64, i64) {
    nodes
        .iter()
        .fold((0, 0), |(w, h), n| (w.max(n.rect.2), h.max(n.rect.3)))
}

fn descends(nodes: &[Node], mut at: usize, ancestor: usize) -> bool {
    while let Some(parent) = nodes[at].parent {
        if parent == ancestor {
            return true;
        }
        at = parent;
    }
    false
}

/// The text a person reads on or inside an element: its own text or
/// description, else the text of the elements inside it.
pub fn label(nodes: &[Node], at: usize) -> String {
    let node = &nodes[at];
    if !node.text.trim().is_empty() {
        return one_line(&node.text);
    }
    if !node.description.trim().is_empty() {
        return one_line(&node.description);
    }
    let inside: Vec<String> = (at + 1..nodes.len())
        .take_while(|i| descends(nodes, *i, at))
        .filter_map(|i| {
            let n = &nodes[i];
            [&n.text, &n.description]
                .into_iter()
                .find(|t| !t.trim().is_empty())
                .map(|t| one_line(t))
        })
        .take(3)
        .collect();
    inside.join(" | ")
}

fn one_line(text: &str) -> String {
    let joined = text.split_whitespace().collect::<Vec<_>>().join(" ");
    shorten(&joined, 80)
}

fn shorten(text: &str, limit: usize) -> String {
    if text.chars().count() <= limit {
        return text.to_string();
    }
    let kept: String = text.chars().take(limit.saturating_sub(1)).collect();
    format!("{kept}…")
}

/// The nodes that are marked: interactive, enabled, with a visible area, not
/// the outer twin of an interactive element with the same area. Document
/// order, which is mostly top to bottom.
pub fn interactive(nodes: &[Node]) -> Vec<usize> {
    let (width, height) = screen_size(nodes);
    let candidates: Vec<usize> = (0..nodes.len())
        .filter(|i| {
            let n = &nodes[*i];
            n.interactive()
                && n.enabled
                && n.has_area()
                && n.rect.2 > 0
                && n.rect.3 > 0
                && n.rect.0 < width
                && n.rect.1 < height
        })
        .collect();
    candidates
        .iter()
        .copied()
        .filter(|outer| {
            !candidates.iter().any(|inner| {
                inner > outer
                    && descends(nodes, *inner, *outer)
                    && nodes[*inner].rect == nodes[*outer].rect
            })
        })
        .collect()
}

/// A mark's centre, inside the part of the element that is on the screen.
fn centre(rect: Rect, width: i64, height: i64) -> (i64, i64) {
    let (l, t) = (rect.0.max(0), rect.1.max(0));
    let (r, b) = (rect.2.min(width.max(1)), rect.3.min(height.max(1)));
    ((l + r) / 2, (t + b) / 2)
}

/// A node as a mark: number, label and what to call it.
pub fn mark(nodes: &[Node], at: usize, number: usize) -> Value {
    let n = &nodes[at];
    let (width, height) = screen_size(nodes);
    let (x, y) = centre(n.rect, width, height);
    let mut mark = json!({"mark":number,"label":label(nodes, at),"text":n.text,"description":n.description,
        "resource_id":n.id,"class":n.class,"package":n.package,
        "bounds":{"left":n.rect.0,"top":n.rect.1,"right":n.rect.2,"bottom":n.rect.3,"x":x,"y":y,
            "width":n.rect.2 - n.rect.0,"height":n.rect.3 - n.rect.1},
        "clickable":n.clickable,"editable":n.editable(),"checkable":n.checkable,"source":"hierarchy"});
    if n.checkable {
        mark["checked"] = json!(n.checked);
    }
    mark
}

/// The marks of a hierarchy, at most `limit`, and whether more were left out.
pub fn marks(nodes: &[Node], limit: usize) -> (Vec<Value>, bool) {
    let all = interactive(nodes);
    let truncated = all.len() > limit;
    let marks = all
        .iter()
        .take(limit)
        .enumerate()
        .map(|(i, at)| mark(nodes, *at, i + 1))
        .collect();
    (marks, truncated)
}

/// Whether the hierarchy still has the element a mark was made from: same
/// class, resource id, text and bounds.
pub fn has_element(nodes: &[Node], mark: &Value) -> bool {
    let b = &mark["bounds"];
    let rect = (
        b["left"].as_i64().unwrap_or(-1),
        b["top"].as_i64().unwrap_or(-1),
        b["right"].as_i64().unwrap_or(-1),
        b["bottom"].as_i64().unwrap_or(-1),
    );
    nodes.iter().any(|n| {
        n.rect == rect
            && n.class == s(mark, "class")
            && n.id == s(mark, "resource_id")
            && n.text == s(mark, "text")
            && n.description == s(mark, "description")
            && n.enabled
    })
}

/// Whether the tree shows a node: it carries text, is interactive or scrolls.
/// Plain containers are left out and their children move up a level.
fn shown(n: &Node) -> bool {
    n.has_area()
        && (!n.text.trim().is_empty()
            || !n.description.trim().is_empty()
            || n.interactive()
            || n.scrollable)
}

pub struct Tree {
    pub text: String,
    pub nodes_total: usize,
    pub nodes_shown: usize,
    pub truncated: bool,
    pub package: String,
    pub width: i64,
    pub height: i64,
}

/// The package most nodes belong to, which is the app in front.
pub fn app_package_of(nodes: &[Node]) -> String {
    app_package(nodes)
}
fn app_package(nodes: &[Node]) -> String {
    let mut counts: Vec<(&str, usize)> = Vec::new();
    for n in nodes.iter().filter(|n| !n.package.is_empty()) {
        match counts.iter_mut().find(|(p, _)| *p == n.package) {
            Some(entry) => entry.1 += 1,
            None => counts.push((&n.package, 1)),
        }
    }
    counts
        .into_iter()
        .max_by_key(|(_, count)| *count)
        .map(|(p, _)| p.to_string())
        .unwrap_or_default()
}

/// The tree as indented text, one line per shown node, within `max_chars`.
/// `numbers` maps a node to its mark number.
pub fn tree(
    nodes: &[Node],
    numbers: &[Option<usize>],
    max_chars: usize,
    only_interactive: bool,
) -> Tree {
    let (width, height) = screen_size(nodes);
    let package = app_package(nodes);
    let header = format!("screen {width}x{height} package {package}\n");
    let mut text = header;
    let mut depth_of = vec![0usize; nodes.len()];
    let mut lines = Vec::new();
    for (i, n) in nodes.iter().enumerate() {
        let parent_depth = n.parent.map(|p| depth_of[p]).unwrap_or(0);
        let wanted = shown(n) && (!only_interactive || n.interactive() || numbers[i].is_some());
        depth_of[i] = if wanted {
            parent_depth + 1
        } else {
            parent_depth
        };
        if !wanted {
            continue;
        }
        let mut line = "  ".repeat(depth_of[i] - 1);
        line.push_str(n.short_class());
        let shown_text = if n.text.trim().is_empty() {
            &n.description
        } else {
            &n.text
        };
        if !shown_text.trim().is_empty() {
            line.push_str(&format!(" \"{}\"", one_line(shown_text)));
        }
        if !n.text.trim().is_empty() && !n.description.trim().is_empty() && n.text != n.description
        {
            line.push_str(&format!(" desc=\"{}\"", one_line(&n.description)));
        }
        if !n.id.is_empty() {
            line.push_str(&format!(" id={}", n.short_id()));
        }
        if let Some(number) = numbers[i] {
            line.push_str(&format!(" #{number}"));
        }
        line.push_str(&format!(
            " ({},{},{},{})",
            n.rect.0, n.rect.1, n.rect.2, n.rect.3
        ));
        for (flag, name) in [
            (n.checkable && n.checked, "checked"),
            (n.selected, "selected"),
            (n.focused, "focused"),
            (!n.enabled, "disabled"),
            (n.scrollable, "scrollable"),
            (n.password, "password"),
            (n.editable(), "editable"),
        ] {
            if flag {
                line.push(' ');
                line.push_str(name);
            }
        }
        lines.push(line);
    }
    let total = lines.len();
    let mut count = 0;
    for line in &lines {
        // Keep room for the closing line that says how much is missing.
        if text.chars().count() + line.chars().count() + 1 > max_chars.saturating_sub(70)
            && count < total
        {
            break;
        }
        text.push_str(line);
        text.push('\n');
        count += 1;
    }
    let truncated = count < total;
    if truncated {
        text.push_str(&format!(
            "… {} more nodes not shown; raise max_chars\n",
            total - count
        ));
    }
    Tree {
        text,
        nodes_total: total,
        nodes_shown: count,
        truncated,
        package,
        width,
        height,
    }
}

/// The mark number of each node, in the order `interactive` numbers them,
/// up to `limit`.
pub fn numbering(nodes: &[Node], limit: usize) -> Vec<Option<usize>> {
    let mut numbers = vec![None; nodes.len()];
    for (i, at) in interactive(nodes).into_iter().take(limit).enumerate() {
        numbers[at] = Some(i + 1);
    }
    numbers
}

// ---- Compact records: relevant nodes from the whole tree -------------------
//
// `tree` renders shown nodes in document order and stops at its budget, so a
// long header can push a checkout button at the bottom of the screen out of
// the text. `compact` first looks at every node, then spends the budget by
// relevance: the focused node, editors and nodes matching the task's query
// first, then enabled controls, stateful widgets and scroll regions, then
// disabled controls and static text, then what lies outside the window.
// Within each of these, repeated siblings (the fourth and later of one class
// under one parent) come after the rest, and the window's four horizontal
// bands take turns, so no band and no long list takes the whole budget. A record that does not fit is left out and a
// shorter one may use the room (off-screen ones only when everything on the
// screen fitted). Records are whole lines or absent, never cut,
// and come out in document order with their `nN` reference (the node's index
// in the hierarchy), mark number, bounds and state.
//
// A static leaf text is folded into the one control it belongs to: the
// nearest clickable ancestor, inside no scroll region between them, that is
// not an editor and holds no other control, editor or scroll region, and
// whose bounds contain the text's. The control's own text, description and id
// stay as they are; the folded text is shown apart as `label="…" from=nN`, so
// no selector changes. Anything else stays a record of its own. `detail`
// answers the complete record of any reference, folded or omitted.

/// Classes that hold repeated items.
fn collection(n: &Node) -> bool {
    let class = n.short_class();
    ["RecyclerView", "ListView", "GridView", "XCUIElementTypeTable", "XCUIElementTypeCollectionView"]
        .iter()
        .any(|c| class.ends_with(c))
}

fn container(n: &Node) -> bool {
    n.scrollable || collection(n)
}

fn inside(inner: Rect, outer: Rect) -> bool {
    inner.0 >= outer.0 && inner.1 >= outer.1 && inner.2 <= outer.2 && inner.3 <= outer.3
}

/// The window: the right and bottom edge of the top-level nodes, so a node
/// laid out below it is off the screen rather than making it taller.
fn viewport(nodes: &[Node]) -> (i64, i64) {
    let windows = nodes.iter().filter(|n| n.parent.is_none() && n.has_area());
    let (w, h) = windows.fold((0, 0), |(w, h), n| (w.max(n.rect.2), h.max(n.rect.3)));
    if w > 0 && h > 0 { (w, h) } else { screen_size(nodes) }
}

/// Whether a node lies wholly outside the screen.
fn offscreen(n: &Node, width: i64, height: i64) -> bool {
    n.rect.2 <= 0 || n.rect.3 <= 0 || n.rect.0 >= width || n.rect.1 >= height
}

/// Which leaf texts fold into which control: `into[leaf] = Some(control)`.
pub fn folding(nodes: &[Node]) -> Vec<Option<usize>> {
    let count = nodes.len();
    let has_text = |n: &Node| !n.text.trim().is_empty() || !n.description.trim().is_empty();
    // Shown descendants and controls below each node.
    let mut shown_below = vec![false; count];
    let mut actionable_below = vec![0usize; count];
    for n in nodes {
        let actionable = n.interactive() || container(n);
        let visible = shown(n);
        let mut at = n.parent;
        while let Some(p) = at {
            if visible {
                shown_below[p] = true;
            }
            if actionable {
                actionable_below[p] += 1;
            }
            at = nodes[p].parent;
        }
    }
    let mut into = vec![None; count];
    let mut groups: std::collections::BTreeMap<usize, Vec<usize>> = Default::default();
    for (i, n) in nodes.iter().enumerate() {
        let leaf = shown(n) && has_text(n) && !n.interactive() && !container(n) && !n.password && !shown_below[i];
        if !leaf {
            continue;
        }
        let mut at = n.parent;
        let mut owner = None;
        while let Some(p) = at {
            let a = &nodes[p];
            if container(a) {
                break;
            }
            if a.interactive() {
                owner = Some(p);
                break;
            }
            at = a.parent;
        }
        if let Some(p) = owner {
            let a = &nodes[p];
            if !a.editable() && shown(a) && actionable_below[p] == 0 && inside(n.rect, a.rect) {
                groups.entry(p).or_default().push(i);
            }
        }
    }
    for (owner, leaves) in groups {
        // More than four texts in one control is a layout, not a label.
        if leaves.len() <= 4 {
            for leaf in leaves {
                into[leaf] = Some(owner);
            }
        }
    }
    into
}

/// What `compact` chose and left out.
pub struct Compact {
    pub text: String,
    /// Records there were (shown nodes not folded into a control).
    pub records: usize,
    pub selected: Vec<usize>,
    /// Records left out, the controls and editors first, then in document order.
    pub omitted: Vec<usize>,
    pub omitted_by: Value,
    pub folded: usize,
    /// Nodes with text or a control that have no area (invisible).
    pub hidden: usize,
    pub truncated: bool,
    pub package: String,
    pub width: i64,
    pub height: i64,
}

fn tokens(query: &str) -> Vec<String> {
    let query = query.to_lowercase();
    let mut out: Vec<String> = query.split_whitespace().map(str::to_string).collect();
    if out.len() > 1 {
        out.push(query.split_whitespace().collect::<Vec<_>>().join(" "));
    }
    out.into_iter().filter(|t| !t.is_empty()).take(20).collect()
}

/// The text a record is known by: its text, description or folded label.
fn known_as(n: &Node, folded: &str) -> String {
    if !n.text.trim().is_empty() && !n.password {
        one_line(&n.text)
    } else if !n.description.trim().is_empty() {
        one_line(&n.description)
    } else {
        folded.to_string()
    }
}

fn kind(n: &Node) -> &'static str {
    if n.editable() {
        "editable"
    } else if n.checkable || n.selected {
        "stateful"
    } else if n.interactive() {
        "interactive"
    } else if container(n) {
        "scroll"
    } else {
        "text"
    }
}

/// The relevant records of the whole tree within `max_chars` characters.
/// `digest` names the hierarchy for `detail` and stale checks.
pub fn compact(
    nodes: &[Node],
    numbers: &[Option<usize>],
    max_chars: usize,
    query: &str,
    only_interactive: bool,
    digest: &str,
) -> Compact {
    let (width, height) = viewport(nodes);
    let package = app_package(nodes);
    let into = folding(nodes);
    let hidden = nodes
        .iter()
        .filter(|n| !n.has_area() && (n.interactive() || !n.text.trim().is_empty() || !n.description.trim().is_empty()))
        .count();
    let wanted = |i: usize| {
        let n = &nodes[i];
        shown(n) && into[i].is_none() && (!only_interactive || n.interactive() || numbers[i].is_some() || container(n))
    };
    let records: Vec<usize> = (0..nodes.len()).filter(|i| wanted(*i)).collect();
    let is_record = {
        let mut flags = vec![false; nodes.len()];
        for i in &records {
            flags[*i] = true;
        }
        flags
    };
    // Folded labels and their sources, per control.
    let mut labels: std::collections::BTreeMap<usize, Vec<usize>> = Default::default();
    for (leaf, owner) in into.iter().enumerate() {
        if let Some(owner) = owner {
            labels.entry(*owner).or_default().push(leaf);
        }
    }
    let folded_label = |i: usize| -> String {
        labels
            .get(&i)
            .map(|leaves| {
                leaves
                    .iter()
                    .map(|l| known_as(&nodes[*l], ""))
                    .collect::<Vec<_>>()
                    .join(" | ")
            })
            .unwrap_or_default()
    };
    // The same name on more than one record gets the container it is in.
    let mut names: std::collections::BTreeMap<String, usize> = Default::default();
    for i in &records {
        let name = known_as(&nodes[*i], &folded_label(*i));
        if !name.is_empty() {
            *names.entry(name).or_default() += 1;
        }
    }
    // The nearest ancestor known by something else than the record itself:
    // its own text, the texts inside it (as `label` reads them), or its id.
    let context = |i: usize, own: &str| -> Option<String> {
        let mut at = nodes[i].parent;
        while let Some(p) = at {
            let a = &nodes[p];
            if a.has_area() {
                let mut name = known_as(a, &folded_label(p));
                if name.is_empty() {
                    name = label(nodes, p);
                }
                if name.is_empty() {
                    name = a.short_id().to_string();
                }
                if !name.is_empty() && name != own {
                    return Some(format!("n{p}:\"{}\"", shorten(&name, 40)));
                }
            }
            at = a.parent;
        }
        None
    };
    let terms = tokens(query);
    // The fourth and later sibling of one class under one parent is a
    // repetition (list items, links): it comes after the rest of its tier.
    let mut repeated = vec![false; nodes.len()];
    let mut seen: std::collections::BTreeMap<(Option<usize>, &str), usize> = Default::default();
    for i in &records {
        let count = seen.entry((nodes[*i].parent, nodes[*i].short_class())).or_default();
        repeated[*i] = *count >= 3;
        *count += 1;
    }
    let mut lines: Vec<String> = vec![String::new(); nodes.len()];
    let mut tier = vec![0usize; nodes.len()];
    let mut band = vec![0usize; nodes.len()];
    for i in &records {
        let i = *i;
        let n = &nodes[i];
        let depth = {
            let mut d = 0;
            let mut at = n.parent;
            while let Some(p) = at {
                if is_record[p] {
                    d += 1;
                }
                at = nodes[p].parent;
            }
            d.min(12)
        };
        let mut line = " ".repeat(depth);
        line.push_str(&format!("n{i} {}", n.short_class()));
        let mut cut = false;
        let mut quote = |text: &str| {
            let flat = text.split_whitespace().collect::<Vec<_>>().join(" ");
            cut |= flat.chars().count() > 80;
            one_line(text)
        };
        if n.password {
            // A password field's text is never repeated, whatever it shows.
        } else if !n.text.trim().is_empty() {
            line.push_str(&format!(" \"{}\"", quote(&n.text)));
        }
        let description_shown = n.description.trim().is_empty() || (!n.password && n.text == n.description);
        if !description_shown {
            if n.text.trim().is_empty() || n.password {
                line.push_str(&format!(" \"{}\"", quote(&n.description)));
            } else {
                line.push_str(&format!(" desc=\"{}\"", quote(&n.description)));
            }
        }
        let label = folded_label(i);
        if !label.is_empty() {
            let sources: Vec<String> = labels[&i].iter().map(|l| format!("n{l}")).collect();
            line.push_str(&format!(" label=\"{}\" from={}", label, sources.join(",")));
        }
        if !n.id.is_empty() {
            line.push_str(&format!(" id={}", n.short_id()));
        }
        if let Some(number) = numbers[i] {
            line.push_str(&format!(" #{number}"));
        }
        line.push_str(&format!(" ({},{},{},{})", n.rect.0, n.rect.1, n.rect.2, n.rect.3));
        let off = offscreen(n, width, height);
        for (flag, name) in [
            (n.checkable && n.checked, "checked"),
            (n.checkable && !n.checked, "unchecked"),
            (n.selected, "selected"),
            (n.focused, "focused"),
            (!n.enabled, "disabled"),
            (n.scrollable, "scrollable"),
            (n.password, "password"),
            (n.editable(), "editable"),
            (off, "offscreen"),
            (cut, "cut"),
        ] {
            if flag {
                line.push(' ');
                line.push_str(name);
            }
        }
        let name = known_as(n, &label);
        if names.get(&name).is_some_and(|c| *c > 1) {
            if let Some(found) = context(i, &name) {
                line.push_str(&format!(" in={found}"));
            }
        }
        let haystack = format!("{} {} {} {}", n.text, n.description, n.id, label).to_lowercase();
        let matched = terms.iter().any(|t| haystack.contains(t.as_str()));
        tier[i] = if n.focused || n.editable() || matched {
            0
        } else if off {
            3
        } else if (n.interactive() && n.enabled) || container(n) {
            1
        } else {
            2
        };
        let centre = ((n.rect.1.max(0) + n.rect.3.min(height.max(1))) / 2).clamp(0, height.max(1) - 1);
        band[i] = ((centre * 4) / height.max(1)).clamp(0, 3) as usize;
        lines[i] = line;
    }
    // Spend the budget: tier by tier, the four bands in turn, document order within a band.
    let mut order = Vec::with_capacity(records.len());
    for t in 0..8 {
        let mut bands: Vec<std::collections::VecDeque<usize>> = vec![Default::default(); 4];
        for i in &records {
            if tier[*i] * 2 + usize::from(repeated[*i]) == t {
                bands[band[*i]].push_back(*i);
            }
        }
        while bands.iter().any(|b| !b.is_empty()) {
            for b in bands.iter_mut() {
                if let Some(i) = b.pop_front() {
                    order.push(i);
                }
            }
        }
    }
    let header = format!(
        "screen {width}x{height} package {} mode compact digest {digest}\n",
        shorten(&package, 100)
    );
    let footer = |omitted: usize, controls: usize, offscreen: usize, folded: usize, hidden: usize| {
        format!("… {omitted} records not shown ({controls} controls, {offscreen} offscreen); {folded} texts folded into their controls; {hidden} hidden; mode detail with refs shows any record\n")
    };
    let most = nodes.len().max(1);
    let reserve = footer(most, most, most, most, most).chars().count();
    let folded = into.iter().filter(|o| o.is_some()).count();
    let mut left = max_chars.saturating_sub(header.chars().count() + reserve);
    // A record that does not fit is left out whole and a shorter one may take
    // the room, except that off-screen records only come in when everything
    // on the screen fitted.
    let mut chosen = vec![false; nodes.len()];
    let mut dropped_on_screen = false;
    for i in &order {
        let cost = lines[*i].chars().count() + 1;
        if tier[*i] == 3 && dropped_on_screen {
            continue;
        }
        if cost <= left {
            left -= cost;
            chosen[*i] = true;
        } else if tier[*i] < 3 {
            dropped_on_screen = true;
        }
    }
    let selected: Vec<usize> = records.iter().copied().filter(|i| chosen[*i]).collect();
    let mut omitted: Vec<usize> = records.iter().copied().filter(|i| !chosen[*i]).collect();
    omitted.sort_by_key(|i| (tier[*i] > 1, *i));
    let mut by = json!({"interactive":0,"editable":0,"stateful":0,"scroll":0,"text":0,"offscreen":0});
    for i in &omitted {
        let k = if tier[*i] == 3 { "offscreen" } else { kind(&nodes[*i]) };
        by[k] = json!(by[k].as_u64().unwrap_or(0) + 1);
    }
    let mut text = header;
    for i in &selected {
        text.push_str(&lines[*i]);
        text.push('\n');
    }
    let truncated = !omitted.is_empty();
    if truncated || folded > 0 || hidden > 0 {
        let controls = omitted.iter().filter(|i| nodes[**i].interactive()).count();
        let off = omitted.iter().filter(|i| tier[**i] == 3).count();
        text.push_str(&footer(omitted.len(), controls, off, folded, hidden));
    }
    Compact {
        text,
        records: records.len(),
        selected,
        omitted,
        omitted_by: by,
        folded,
        hidden,
        truncated,
        package,
        width,
        height,
    }
}

/// The longest text a detail record quotes from one attribute.
const DETAIL_CHARS: usize = 4000;

/// A reference `nN` as a node index, or why it is none.
pub fn reference(nodes: &[Node], text: &str) -> Result<usize> {
    text.strip_prefix('n')
        .and_then(|d| d.parse::<usize>().ok())
        .filter(|i| *i < nodes.len())
        .ok_or_else(|| {
            Error::new(
                "invalid_argument",
                format!("{text:?} is not a node of this hierarchy ({} nodes, n0 to n{})", nodes.len(), nodes.len().saturating_sub(1)),
            )
        })
}

/// The complete record of a node: every attribute the hierarchy has, its
/// parent and children, its mark, and what was folded into or out of it.
/// A password field's text is withheld.
pub fn detail(nodes: &[Node], numbers: &[Option<usize>], at: usize) -> Value {
    let n = &nodes[at];
    let into = folding(nodes);
    let full = |text: &str| -> Value {
        let chars = text.chars().count();
        if chars <= DETAIL_CHARS {
            json!(text)
        } else {
            json!(text.chars().take(DETAIL_CHARS).collect::<String>())
        }
    };
    let children: Vec<usize> = (at + 1..nodes.len()).filter(|i| nodes[*i].parent == Some(at)).collect();
    let sources: Vec<String> = into
        .iter()
        .enumerate()
        .filter(|(_, o)| **o == Some(at))
        .map(|(i, _)| format!("n{i}"))
        .collect();
    let mut record = json!({"ref":format!("n{at}"),"class":n.class,
        "text":if n.password { Value::Null } else { full(&n.text) },
        "description":full(&n.description),"resource_id":n.id,"package":n.package,
        "bounds":{"left":n.rect.0,"top":n.rect.1,"right":n.rect.2,"bottom":n.rect.3,
            "width":n.rect.2 - n.rect.0,"height":n.rect.3 - n.rect.1},
        "clickable":n.clickable,"long_clickable":n.long_clickable,"checkable":n.checkable,"checked":n.checked,
        "enabled":n.enabled,"focused":n.focused,"focusable":n.focusable,"scrollable":n.scrollable,
        "selected":n.selected,"password":n.password,"editable":n.editable(),"has_area":n.has_area(),
        "mark":numbers[at],"label":label(nodes, at),
        "parent":n.parent.map(|p| format!("n{p}")),
        "children":children.iter().take(50).map(|c| format!("n{c}")).collect::<Vec<_>>(),
        "children_total":children.len(),
        "label_sources":sources,"folded_into":into[at].map(|o| format!("n{o}")),"source":"hierarchy"});
    if n.password {
        record["text_withheld"] = json!("password");
    }
    for key in ["text", "description"] {
        let original = if key == "text" { &n.text } else { &n.description };
        if original.chars().count() > DETAIL_CHARS && !(key == "text" && n.password) {
            record[format!("{key}_cut")] = json!(true);
            record[format!("{key}_chars")] = json!(original.chars().count());
        }
    }
    record
}

// ---- Differences between two hierarchies ---------------------------------

/// An element as the difference lists it.
fn element(nodes: &[Node], at: usize) -> Value {
    let n = &nodes[at];
    json!({"label":label(nodes, at),"class":n.class,"resource_id":n.id,"text":n.text,"description":n.description,
        "bounds":{"left":n.rect.0,"top":n.rect.1,"right":n.rect.2,"bottom":n.rect.3,
            "x":(n.rect.0 + n.rect.2) / 2,"y":(n.rect.1 + n.rect.3) / 2}})
}

/// The elements that count in a difference: those with text or a description,
/// and interactive ones.
fn meaningful(nodes: &[Node]) -> Vec<usize> {
    (0..nodes.len())
        .filter(|i| {
            let n = &nodes[*i];
            n.has_area()
                && (!n.text.trim().is_empty()
                    || !n.description.trim().is_empty()
                    || n.interactive())
        })
        .collect()
}

fn key(n: &Node) -> String {
    let name = if n.text.is_empty() {
        &n.description
    } else {
        &n.text
    };
    if n.id.is_empty() {
        format!("{}|{}", n.class, name)
    } else {
        format!("{}|{}", n.class, n.id)
    }
}

fn moved(a: Rect, b: Rect) -> bool {
    let limit = 2;
    (a.0 - b.0).abs() > limit
        || (a.1 - b.1).abs() > limit
        || (a.2 - b.2).abs() > limit
        || (a.3 - b.3).abs() > limit
}

fn changes(before: &Node, after: &Node) -> serde_json::Map<String, Value> {
    let mut found = serde_json::Map::new();
    let mut note = |name: &str, from: Value, to: Value| {
        if from != to {
            found.insert(name.into(), json!({"from":from,"to":to}));
        }
    };
    note("text", json!(before.text), json!(after.text));
    note(
        "description",
        json!(before.description),
        json!(after.description),
    );
    note("enabled", json!(before.enabled), json!(after.enabled));
    note("checked", json!(before.checked), json!(after.checked));
    note("selected", json!(before.selected), json!(after.selected));
    note("focused", json!(before.focused), json!(after.focused));
    if moved(before.rect, after.rect) {
        let r = |r: Rect| json!({"left":r.0,"top":r.1,"right":r.2,"bottom":r.3});
        found.insert(
            "bounds".into(),
            json!({"from":r(before.rect),"to":r(after.rect)}),
        );
    }
    found
}

/// What changed between two hierarchies: elements added, removed and changed
/// (text, state or position), at most `limit` of each.
pub fn compare(before: &[Node], after: &[Node], limit: usize) -> Value {
    let old = meaningful(before);
    let new = meaningful(after);
    let mut matched_new = vec![false; new.len()];
    let mut pairs: Vec<(usize, usize)> = Vec::new();
    let mut left_old: Vec<usize> = Vec::new();
    for o in &old {
        let wanted = key(&before[*o]);
        // The nth element with this key matches the nth one.
        let ordinal = old
            .iter()
            .take_while(|x| *x != o)
            .filter(|x| key(&before[**x]) == wanted)
            .count();
        let found = new
            .iter()
            .enumerate()
            .filter(|(_, n)| key(&after[**n]) == wanted)
            .nth(ordinal);
        match found {
            Some((slot, n)) if !matched_new[slot] => {
                matched_new[slot] = true;
                pairs.push((*o, *n));
            }
            _ => left_old.push(*o),
        }
    }
    let mut left_new: Vec<usize> = new
        .iter()
        .enumerate()
        .filter(|(slot, _)| !matched_new[*slot])
        .map(|(_, n)| *n)
        .collect();
    // An element of the same class in the same place whose text changed is
    // one element, not a removal and an addition.
    let mut still_old = Vec::new();
    for o in left_old {
        let same_place = left_new.iter().position(|n| {
            after[*n].class == before[o].class && !moved(after[*n].rect, before[o].rect)
        });
        match same_place {
            Some(slot) => {
                let n = left_new.remove(slot);
                pairs.push((o, n));
            }
            None => still_old.push(o),
        }
    }
    let mut changed = Vec::new();
    for (o, n) in &pairs {
        let found = changes(&before[*o], &after[*n]);
        if !found.is_empty() {
            changed.push(json!({"element":element(after, *n),"changes":found}));
        }
    }
    let matched_fraction = if old.is_empty() {
        1.0
    } else {
        pairs.len() as f64 / old.len() as f64
    };
    let (before_package, after_package) = (app_package(before), app_package(after));
    let different_screen =
        before_package != after_package || (old.len() >= 4 && matched_fraction < 0.3);
    let identical = changed.is_empty() && still_old.is_empty() && left_new.is_empty();
    let cut = |list: Vec<Value>| -> (Vec<Value>, bool) {
        let more = list.len() > limit;
        (list.into_iter().take(limit).collect(), more)
    };
    let removed_total = still_old.len();
    let added_total = left_new.len();
    let changed_total = changed.len();
    let (removed, removed_more) = cut(still_old.iter().map(|o| element(before, *o)).collect());
    let (added, added_more) = cut(left_new.iter().map(|n| element(after, *n)).collect());
    let (changed, changed_more) = cut(changed);
    let summary = if identical {
        "No element changed.".to_string()
    } else if different_screen {
        let names = if before_package != after_package {
            format!(" ({before_package} to {after_package})")
        } else {
            String::new()
        };
        format!("A different screen{names}: {removed_total} elements gone, {added_total} new.")
    } else {
        format!("{added_total} added, {removed_total} removed, {changed_total} changed.")
    };
    json!({"kind":"hierarchy","identical":identical,"different_screen":different_screen,"summary":summary,
        "before_package":before_package,"after_package":after_package,
        "added":added,"removed":removed,"changed":changed,
        "counts":{"added":added_total,"removed":removed_total,"changed":changed_total,"unchanged":pairs.len() - changed_total},
        "truncated":added_more || removed_more || changed_more})
}

#[cfg(test)]
mod wda_tests {
    use super::*;
    const SOURCE: &str = r#"<?xml version="1.0" encoding="UTF-8"?>
<AppiumAUT>
  <XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Settings" label="Settings" enabled="true" visible="true" x="0" y="0" width="390" height="844" index="0">
    <XCUIElementTypeWindow type="XCUIElementTypeWindow" enabled="true" visible="true" x="0" y="0" width="390" height="844" index="0">
      <XCUIElementTypeTextField type="XCUIElementTypeTextField" value="ana@example.com" name="email" label="Email" enabled="true" visible="true" focused="true" x="20" y="100" width="350" height="44" index="0"/>
      <XCUIElementTypeButton type="XCUIElementTypeButton" name="Sign in" label="Sign in" enabled="true" visible="true" x="20" y="200" width="350" height="50" index="1"/>
      <XCUIElementTypeButton type="XCUIElementTypeButton" name="Hidden" label="Hidden" enabled="true" visible="false" x="20" y="300" width="350" height="50" index="2"/>
      <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" value="1" name="Remember me" label="Remember me" enabled="true" visible="true" x="20" y="400" width="51" height="31" index="3"/>
      <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Forgot &lt;password&gt;?" name="Forgot?" label="Forgot?" enabled="true" visible="true" x="20" y="500" width="200" height="20" index="4"/>
    </XCUIElementTypeWindow>
  </XCUIElementTypeApplication>
</AppiumAUT>"#;
    #[test]
    fn webdriveragent_source_becomes_a_hierarchy_in_screenshot_pixels() {
        assert!(is_wda(SOURCE));
        assert!(!is_wda("<hierarchy rotation=\"0\"><node class=\"a\"/></hierarchy>"));
        let converted = wda_to_uiautomator(SOURCE, 3.0, 0).unwrap();
        let nodes = parse(&converted).unwrap();
        assert_eq!(screen_size(&nodes), (1170, 2532));
        let field = nodes.iter().find(|n| n.id == "email").unwrap();
        assert!(field.editable() && field.focused);
        assert_eq!((field.text.as_str(), field.description.as_str()), ("ana@example.com", "Email"));
        assert_eq!(field.rect, (60, 300, 1110, 432));
        let (marks, _) = marks(&nodes, 50);
        let labels: Vec<&str> = marks.iter().map(|m| m["label"].as_str().unwrap()).collect();
        assert_eq!(labels, ["ana@example.com", "Sign in", "Remember me"]);
        assert_eq!(marks[2]["checked"], true);
        assert_eq!(marks[1]["bounds"]["x"], 585);
        // The invisible button has no area and the static text is not a mark, but it is in the tree.
        let numbers = numbering(&nodes, 50);
        let text = tree(&nodes, &numbers, 4000, false).text;
        assert!(text.contains("StaticText \"Forgot <password>?\""), "{text}");
        assert!(!text.contains("Hidden"));
        assert!(has_element(&nodes, &marks[1]));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A login screen as UIAutomator dumps it (shortened, same attributes).
    pub const LOGIN: &str = include_str!("../tests/fixtures/screen_login.xml");
    pub const LOGIN_FILLED: &str = include_str!("../tests/fixtures/screen_login_filled.xml");

    #[test]
    fn a_dump_becomes_nodes_with_depth_and_parents() {
        let nodes = parse(LOGIN).unwrap();
        assert_eq!(nodes[0].depth, 0);
        assert_eq!(nodes[0].parent, None);
        assert!(nodes.iter().skip(1).all(|n| n.parent.is_some()));
        assert_eq!(screen_size(&nodes), (1080, 2400));
        assert!(parse("<hierarchy").is_err());
    }

    #[test]
    fn marks_are_the_enabled_interactive_elements_in_document_order() {
        let nodes = parse(LOGIN).unwrap();
        let (marks, truncated) = marks(&nodes, 60);
        assert!(!truncated);
        let labels: Vec<&str> = marks.iter().map(|m| s(m, "label")).collect();
        assert_eq!(
            labels,
            [
                "Back",
                "Email",
                "Password",
                "Remember me",
                "Sign in",
                "Forgot password?"
            ]
        );
        assert_eq!(marks[4]["mark"], 5);
        assert_eq!(marks[4]["resource_id"], "dev.example.app:id/sign_in");
        assert_eq!(marks[4]["bounds"]["x"], 540);
        assert_eq!(marks[4]["bounds"]["y"], 1500);
        // The disabled Google button is not marked; the checkbox says whether it is checked.
        assert!(!labels.contains(&"Sign in with Google"));
        assert_eq!(marks[3]["checked"], false);
        // The clickable row has no text of its own and takes its children's.
        assert_eq!(marks[5]["text"], "");
    }

    #[test]
    fn a_limit_keeps_the_first_marks_and_says_so() {
        let nodes = parse(LOGIN).unwrap();
        let (marks, truncated) = marks(&nodes, 2);
        assert_eq!(marks.len(), 2);
        assert!(truncated);
    }

    #[test]
    fn the_outer_twin_of_an_element_with_the_same_area_is_not_marked_twice() {
        let xml = r#"<hierarchy><node class="android.widget.FrameLayout" package="p" clickable="true" enabled="true" bounds="[0,0][100,100]">
            <node class="android.widget.Button" text="Go" package="p" clickable="true" enabled="true" bounds="[0,0][100,100]"/></node></hierarchy>"#;
        let nodes = parse(xml).unwrap();
        assert_eq!(interactive(&nodes), [1]);
    }

    #[test]
    fn the_tree_leaves_out_plain_containers_and_numbers_the_marks() {
        let nodes = parse(LOGIN).unwrap();
        let numbers = numbering(&nodes, 60);
        let t = tree(&nodes, &numbers, 6000, false);
        assert!(!t.truncated);
        assert!(
            t.text
                .starts_with("screen 1080x2400 package dev.example.app\n")
        );
        assert!(
            t.text
                .contains("Button \"Sign in\" id=sign_in #5 (140,1440,940,1560)"),
            "{}",
            t.text
        );
        assert!(
            t.text
                .contains("CheckBox \"Remember me\" id=remember #4 (140,1100,940,1200)")
        );
        assert!(t.text.contains("EditText \"Email\"") && t.text.contains("editable"));
        assert!(t.text.contains("disabled"));
        assert!(!t.text.contains("FrameLayout"));
        // A child shows one level under what is shown above it.
        assert!(
            t.text.contains("\n  TextView \"Welcome back\"")
                || t.text.contains("\nTextView \"Welcome back\"")
        );
        assert!(t.nodes_shown < nodes.len());
    }

    #[test]
    fn the_tree_stops_at_the_cap_and_says_how_much_is_missing() {
        let nodes = parse(LOGIN).unwrap();
        let numbers = numbering(&nodes, 60);
        let t = tree(&nodes, &numbers, 400, false);
        assert!(t.truncated);
        assert!(t.text.chars().count() <= 400, "{}", t.text.chars().count());
        assert!(t.text.contains("more nodes not shown; raise max_chars"));
        assert!(t.nodes_shown < t.nodes_total);
    }

    #[test]
    fn the_interactive_tree_has_only_what_can_be_pressed() {
        let nodes = parse(LOGIN).unwrap();
        let t = tree(&nodes, &numbering(&nodes, 60), 6000, true);
        assert!(!t.text.contains("Welcome back"));
        assert!(t.text.contains("Sign in"));
    }

    #[test]
    fn a_mark_is_found_again_only_while_its_element_is_unchanged() {
        let nodes = parse(LOGIN).unwrap();
        let (marks, _) = super::marks(&nodes, 60);
        assert!(has_element(&nodes, &marks[4]));
        let filled = parse(LOGIN_FILLED).unwrap();
        // Sign in is unchanged; the email field now has text.
        assert!(has_element(&filled, &marks[4]));
        assert!(!has_element(&filled, &marks[1]));
    }

    #[test]
    fn a_difference_lists_added_removed_and_changed_elements() {
        let before = parse(LOGIN).unwrap();
        let after = parse(LOGIN_FILLED).unwrap();
        let d = compare(&before, &after, 50);
        assert_eq!(d["identical"], false);
        assert_eq!(d["different_screen"], false);
        let changed = d["changed"].as_array().unwrap();
        let email = changed
            .iter()
            .find(|c| c["element"]["resource_id"] == "dev.example.app:id/email")
            .unwrap();
        assert_eq!(email["changes"]["text"]["from"], "Email");
        assert_eq!(email["changes"]["text"]["to"], "ada@example.org");
        let remember = changed
            .iter()
            .find(|c| c["element"]["resource_id"] == "dev.example.app:id/remember")
            .unwrap();
        assert_eq!(
            remember["changes"]["checked"],
            json!({"from":false,"to":true})
        );
        let added: Vec<&str> = d["added"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| s(e, "label"))
            .collect();
        assert_eq!(added, ["Enter a valid password"]);
        assert_eq!(d["removed"], json!([]));
        assert_eq!(d["summary"], "1 added, 0 removed, 2 changed.");
    }

    #[test]
    fn two_equal_hierarchies_are_identical() {
        let nodes = parse(LOGIN).unwrap();
        let d = compare(&nodes, &nodes, 50);
        assert_eq!(d["identical"], true);
        assert_eq!(d["summary"], "No element changed.");
    }

    #[test]
    fn another_app_is_a_different_screen() {
        let login = parse(LOGIN).unwrap();
        let other = parse(r#"<hierarchy><node class="android.widget.FrameLayout" package="com.android.settings" bounds="[0,0][1080,2400]">
            <node class="android.widget.TextView" text="Settings" package="com.android.settings" bounds="[0,100][500,200]"/></node></hierarchy>"#).unwrap();
        let d = compare(&login, &other, 50);
        assert_eq!(d["different_screen"], true);
        assert!(s(&d, "summary").contains("dev.example.app to com.android.settings"));
    }

    #[test]
    fn long_text_is_cut_and_whitespace_folded() {
        assert_eq!(one_line("a \n  b"), "a b");
        let long = "x".repeat(200);
        assert_eq!(one_line(&long).chars().count(), 80);
        assert!(one_line(&long).ends_with('…'));
    }
}

#[cfg(test)]
mod compact_tests {
    use super::*;

    fn node(class: &str, attributes: &str, children: &[String]) -> String {
        let enabled = if attributes.contains("enabled=") { "" } else { "enabled=\"true\" " };
        format!(
            "<node class=\"{class}\" package=\"dev.example.shop\" {enabled}{attributes}>{}</node>",
            children.concat()
        )
    }
    fn text(words: &str, bounds: &str) -> String {
        node("android.widget.TextView", &format!("text=\"{words}\" bounds=\"{bounds}\""), &[])
    }
    fn button(words: &str, id: &str, bounds: &str, extra: &str) -> String {
        node(
            "android.widget.Button",
            &format!("text=\"{words}\" resource-id=\"dev.example.shop:id/{id}\" bounds=\"{bounds}\" clickable=\"true\" {extra}"),
            &[],
        )
    }
    /// A shop screen of 1080x2400: a long header of headlines and links,
    /// then a search field, a cart list with duplicate Delete buttons, a
    /// switch, a disabled button, the Pay now button at the bottom, a hidden
    /// promotion, a button below the screen and an Arabic greeting.
    pub fn shop() -> String {
        let mut header = Vec::new();
        for i in 0..30 {
            let top = 20 + i * 26;
            header.push(text(
                &format!("Headline {i}: a long sentence of the news ticker that fills the header of the shop with words"),
                &format!("[20,{top}][1060,{}]", top + 24),
            ));
        }
        for i in 0..24 {
            let top = 820 + i * 32;
            header.push(button(&format!("Category {i}"), &format!("category_{i}"), &format!("[20,{top}][1060,{}]", top + 30), ""));
        }
        let header = node("android.widget.LinearLayout", "bounds=\"[0,0][1080,1600]\"", &header);
        let search = node(
            "android.widget.EditText",
            "text=\"Search products\" resource-id=\"dev.example.shop:id/search\" bounds=\"[40,1650][1040,1750]\" clickable=\"true\" focusable=\"true\"",
            &[],
        );
        let row = |name: &str, top: i64, inner: Vec<String>| {
            node(
                "android.widget.LinearLayout",
                &format!("bounds=\"[0,{top}][1080,{}]\" clickable=\"true\"", top + 120),
                &[vec![text(name, &format!("[40,{}][500,{}]", top + 20, top + 100))], inner].concat(),
            )
        };
        let cart = node(
            "androidx.recyclerview.widget.RecyclerView",
            "resource-id=\"dev.example.shop:id/cart_list\" bounds=\"[0,1760][1080,2150]\" scrollable=\"true\"",
            &[
                row("Apple", 1760, vec![text("€1.20", "[600,1780][1040,1860]")]),
                row("Pear", 1880, vec![button("Delete", "delete", "[800,1900][1040,1980]", "")]),
                row("Plum", 2000, vec![button("Delete", "delete", "[800,2020][1040,2100]", "")]),
            ],
        );
        let gift = node(
            "android.widget.Switch",
            "text=\"Gift wrap\" resource-id=\"dev.example.shop:id/gift\" bounds=\"[40,2160][500,2210]\" checkable=\"true\" checked=\"false\" clickable=\"true\"",
            &[],
        );
        let coupon = button("Coupon", "coupon", "[540,2160][1040,2210]", "enabled=\"false\"");
        let pay = button("Pay now", "pay", "[40,2220][1040,2340]", "");
        let hidden = text("Hidden promo", "[0,0][0,0]");
        let below = button("Below fold", "below", "[40,2500][1040,2600]", "");
        let greeting = node(
            "android.widget.LinearLayout",
            "bounds=\"[0,2340][1080,2400]\" clickable=\"true\" resource-id=\"dev.example.shop:id/greeting\"",
            &[text("مرحبا بالعالم 👋 שלום", "[40,2350][1040,2395]")],
        );
        format!(
            "<?xml version='1.0' encoding='UTF-8'?><hierarchy rotation=\"0\">{}</hierarchy>",
            node(
                "android.widget.FrameLayout",
                "bounds=\"[0,0][1080,2400]\"",
                &[header, search, cart, gift, coupon, pay, hidden, below, greeting]
            )
        )
    }
    fn lines(text: &str) -> Vec<&str> {
        text.lines()
            .filter(|l| !l.starts_with("screen ") && !l.starts_with('…'))
            .collect()
    }
    fn find<'a>(text: &'a str, words: &str) -> Option<&'a str> {
        lines(text).into_iter().find(|l| l.contains(words))
    }
    fn index(nodes: &[Node], words: &str) -> usize {
        nodes.iter().position(|n| n.text == words).unwrap()
    }

    #[test]
    fn the_whole_tree_is_chosen_from_so_a_bottom_control_survives_a_long_header() {
        let nodes = parse(&shop()).unwrap();
        let numbers = numbering(&nodes, 200);
        let old = tree(&nodes, &numbers, 2000, false);
        assert!(old.truncated && !old.text.contains("Pay now"), "the document-order prefix loses it");
        let c = compact(&nodes, &numbers, 2000, "", false, "0123456789abcdef");
        assert!(c.text.chars().count() <= 2000, "{}", c.text.chars().count());
        assert!(c.truncated);
        let pay = index(&nodes, "Pay now");
        let line = find(&c.text, "Pay now").expect(&c.text);
        // The raw identity is kept: reference, class, id, mark and bounds.
        assert!(line.contains(&format!("n{pay} Button \"Pay now\" id=pay #{} (40,2220,1040,2340)", numbers[pay].unwrap())), "{line}");
        assert!(find(&c.text, "Search products").unwrap().ends_with("editable"));
        assert!(find(&c.text, "Gift wrap").unwrap().contains("unchecked"));
        assert!(find(&c.text, "cart_list").unwrap().contains("scrollable"));
        // A disabled control waits behind the enabled ones, and keeps its state.
        assert!(find(&c.text, "Coupon").is_none());
        let roomy = compact(&nodes, &numbers, 4000, "", false, "0123456789abcdef");
        assert!(find(&roomy.text, "Coupon").unwrap().contains("disabled"));
        // What was left out is counted and can be named.
        assert_eq!(c.omitted.len() + c.selected.len(), c.records);
        assert!(c.omitted_by["text"].as_u64().unwrap() > 0);
        assert!(c.text.ends_with("mode detail with refs shows any record\n"));
        assert!(!c.text.contains("Hidden promo") && c.hidden == 1);
        // The marks of the default tree are the marks of the compact one.
        assert_eq!(numbers, numbering(&nodes, 200));
    }

    #[test]
    fn records_are_whole_and_the_same_at_any_budget() {
        let nodes = parse(&shop()).unwrap();
        let numbers = numbering(&nodes, 200);
        let all = compact(&nodes, &numbers, 100_000, "", false, "0123456789abcdef");
        assert!(!all.truncated);
        let every: std::collections::BTreeSet<&str> = lines(&all.text).into_iter().collect();
        for budget in [500, 777, 1200, 2000, 3500, 6000] {
            let c = compact(&nodes, &numbers, budget, "", false, "0123456789abcdef");
            assert!(c.text.chars().count() <= budget, "{budget}: {}", c.text.chars().count());
            for line in lines(&c.text) {
                assert!(every.contains(line), "{budget}: a record that is not whole: {line}");
            }
            // Deterministic: the same input gives the same text.
            assert_eq!(c.text, compact(&nodes, &numbers, budget, "", false, "0123456789abcdef").text);
            // Selected records come out in document order.
            let mut sorted = c.selected.clone();
            sorted.sort();
            assert_eq!(sorted, c.selected);
        }
        // The smallest budget still keeps the editor first and says what is missing.
        let small = compact(&nodes, &numbers, 500, "", false, "0123456789abcdef");
        assert!(small.text.contains("Search products"), "{}", small.text);
        assert!(small.text.contains("records not shown"));
    }

    #[test]
    fn static_labels_fold_only_into_the_one_control_they_belong_to() {
        let nodes = parse(&shop()).unwrap();
        let numbers = numbering(&nodes, 200);
        let c = compact(&nodes, &numbers, 100_000, "", false, "0123456789abcdef");
        let (apple, price) = (index(&nodes, "Apple"), index(&nodes, "€1.20"));
        let row = nodes[apple].parent.unwrap();
        let line = find(&c.text, "label=\"Apple | €1.20\"").expect(&c.text);
        assert!(line.contains(&format!("n{row} LinearLayout label=\"Apple | €1.20\" from=n{apple},n{price}")), "{line}");
        assert!(!lines(&c.text).iter().any(|l| l.contains("\"Apple\"")), "the folded leaf is no record of its own");
        // The control's own text stays empty: a selector on it is unchanged.
        assert_eq!(nodes[row].text, "");
        let d = detail(&nodes, &numbers, row);
        assert_eq!(d["label_sources"], json!([format!("n{apple}"), format!("n{price}")]));
        assert_eq!(detail(&nodes, &numbers, apple)["folded_into"], format!("n{row}"));
        // A row that also holds a button is ambiguous: its text stays a record.
        let pear = index(&nodes, "Pear");
        assert!(find(&c.text, &format!("n{pear} TextView \"Pear\"")).is_some());
        assert_eq!(detail(&nodes, &numbers, pear)["folded_into"], Value::Null);
        // Text directly in a scroll region or in the header is never folded.
        assert!(find(&c.text, "Headline 3:").is_some());
    }

    #[test]
    fn duplicate_labels_name_their_container_and_keep_their_own_identity() {
        let nodes = parse(&shop()).unwrap();
        let numbers = numbering(&nodes, 200);
        let c = compact(&nodes, &numbers, 100_000, "", false, "0123456789abcdef");
        let deletes: Vec<&str> = lines(&c.text).into_iter().filter(|l| l.contains("\"Delete\"")).collect();
        assert_eq!(deletes.len(), 2);
        assert!(deletes[0].contains("in=") && deletes[0].contains("Pear"), "{}", deletes[0]);
        assert!(deletes[1].contains("in=") && deletes[1].contains("Plum"), "{}", deletes[1]);
        assert_ne!(deletes[0], deletes[1]);
        // Both keep the same raw resource id; only the reference and bounds differ.
        assert!(deletes.iter().all(|l| l.contains(" id=delete ")));
        // A unique label gets no context.
        assert!(!find(&c.text, "Pay now").unwrap().contains("in="));
    }

    #[test]
    fn the_task_query_and_the_screen_decide_what_a_small_budget_keeps() {
        let nodes = parse(&shop()).unwrap();
        let numbers = numbering(&nodes, 200);
        let plain = compact(&nodes, &numbers, 500, "", false, "0123456789abcdef");
        let asked = compact(&nodes, &numbers, 500, "coupon", false, "0123456789abcdef");
        assert!(asked.text.contains("\"Coupon\""), "{}", asked.text);
        assert!(!plain.text.contains("\"Coupon\""), "without the query the disabled button waits for room");
        // Off-screen nodes come last, and say so when there is room.
        let all = compact(&nodes, &numbers, 100_000, "", false, "0123456789abcdef");
        assert!(find(&all.text, "Below fold").unwrap().contains("offscreen"));
        let some = compact(&nodes, &numbers, 3000, "", false, "0123456789abcdef");
        assert!(find(&some.text, "Below fold").is_none(), "on-screen controls came first");
        assert!(some.omitted_by["offscreen"].as_u64().unwrap() >= 1);
        // A long text is cut in the record, marked so, and whole in detail.
        let line = find(&all.text, "Headline 0:").unwrap();
        assert!(line.ends_with(" cut") && line.contains('…'), "{line}");
        let headline = nodes.iter().position(|n| n.text.starts_with("Headline 0:")).unwrap();
        assert_eq!(detail(&nodes, &numbers, headline)["text"], nodes[headline].text);
    }

    #[test]
    fn unicode_and_right_to_left_text_is_counted_in_characters_and_kept_whole() {
        let nodes = parse(&shop()).unwrap();
        let numbers = numbering(&nodes, 200);
        let greeting = "مرحبا بالعالم 👋 שלום";
        for budget in [1500, 4000, 100_000] {
            let c = compact(&nodes, &numbers, budget, "مرحبا", false, "0123456789abcdef");
            assert!(c.text.chars().count() <= budget);
            let line = find(&c.text, "greeting").expect(&c.text);
            assert!(line.contains(&format!("label=\"{greeting}\"")), "{line}");
            assert!(c.text.len() > c.text.chars().count(), "bytes are not the budget");
        }
    }

    #[test]
    fn nested_scroll_regions_editors_and_password_fields_stay_distinct() {
        let xml = format!(
            "<hierarchy rotation=\"0\">{}</hierarchy>",
            node("android.widget.ScrollView", "resource-id=\"app:id/outer\" bounds=\"[0,0][1080,2000]\" scrollable=\"true\"", &[
                node("android.widget.LinearLayout", "resource-id=\"app:id/card\" bounds=\"[0,0][1080,600]\" clickable=\"true\"", &[
                    text("Note", "[20,20][300,80]"),
                    node("android.widget.EditText", "resource-id=\"app:id/note\" text=\"Your note\" bounds=\"[20,100][1060,200]\" clickable=\"true\"", &[]),
                ]),
                node("androidx.recyclerview.widget.RecyclerView", "resource-id=\"app:id/inner\" bounds=\"[0,600][1080,1400]\" scrollable=\"true\"", &[
                    text("Loose text", "[20,620][800,700]"),
                ]),
                node("android.widget.EditText", "resource-id=\"app:id/pin\" text=\"••••\" password=\"true\" bounds=\"[20,1500][1060,1600]\" clickable=\"true\"", &[]),
            ])
        );
        let nodes = parse(&xml).unwrap();
        let numbers = numbering(&nodes, 200);
        let c = compact(&nodes, &numbers, 100_000, "", false, "0123456789abcdef");
        assert!(find(&c.text, "id=outer").unwrap().contains("scrollable"));
        assert!(find(&c.text, "id=inner").unwrap().contains("scrollable"));
        // The inner region is a record inside the outer one.
        assert!(find(&c.text, "id=inner").unwrap().starts_with(' '));
        // The card holds an editor, so its text is not folded into it.
        assert!(find(&c.text, "TextView \"Note\"").is_some(), "{}", c.text);
        assert!(find(&c.text, "\"Your note\"").unwrap().contains("editable"));
        assert!(find(&c.text, "TextView \"Loose text\"").is_some());
        assert_eq!(c.folded, 0);
        // A password field is a record, but its text is never repeated.
        let pin = find(&c.text, "id=pin").unwrap();
        assert!(pin.contains("password") && !pin.contains('•'), "{pin}");
        let at = nodes.iter().position(|n| n.id == "app:id/pin").unwrap();
        let d = detail(&nodes, &numbers, at);
        assert_eq!(d["text"], Value::Null);
        assert_eq!(d["text_withheld"], "password");
        assert!(reference(&nodes, "n999").is_err() && reference(&nodes, "x1").is_err());
        assert_eq!(reference(&nodes, &format!("n{at}")).unwrap(), at);
    }

    /// The same offline fixture through both modes at the same budgets: the
    /// characters, bytes and marks each keeps, on and off the window.
    /// Fixture data, not a measurement of any device or model.
    #[test]
    fn at_equal_budgets_compact_keeps_the_on_screen_controls_the_tree_keeps() {
        let nodes = parse(&shop()).unwrap();
        let numbers = numbering(&nodes, 200);
        let (_, height) = viewport(&nodes);
        let on_screen: Vec<usize> = (0..nodes.len()).filter(|i| numbers[*i].is_some() && nodes[*i].rect.1 < height).collect();
        let off_screen = numbers.iter().flatten().count() - on_screen.len();
        let kept = |text: &str, wanted: &[usize]| {
            wanted
                .iter()
                .filter(|i| text.split(char::is_whitespace).any(|w| w == format!("#{}", numbers[**i].unwrap())))
                .count()
        };
        let full_tree = tree(&nodes, &numbers, 100_000, false).text;
        let full_compact = compact(&nodes, &numbers, 100_000, "", false, "0123456789abcdef").text;
        let mut rows = vec![format!(
            "unbudgeted: tree {} chars, compact {} chars; {} marks on the window, {off_screen} below it",
            full_tree.chars().count(), full_compact.chars().count(), on_screen.len()
        )];
        for budget in [1500, 3000, 6000] {
            let t = tree(&nodes, &numbers, budget, false);
            let c = compact(&nodes, &numbers, budget, "", false, "0123456789abcdef");
            let all: Vec<usize> = (0..nodes.len()).filter(|i| numbers[*i].is_some()).collect();
            let (t_on, c_on) = (kept(&t.text, &on_screen), kept(&c.text, &on_screen));
            assert!(c_on >= t_on, "{budget}: compact keeps {c_on} on-screen marks, tree {t_on}");
            assert!(c.text.contains("Pay now"), "{budget}");
            rows.push(format!(
                "budget {budget}: tree {} chars {} bytes, marks {}/{} (on window {t_on}/{}), pay_now={}; compact {} chars {} bytes, marks {}/{} (on window {c_on}/{}), pay_now=true, omitted {}",
                t.text.chars().count(), t.text.len(), kept(&t.text, &all), all.len(), on_screen.len(), t.text.contains("Pay now"),
                c.text.chars().count(), c.text.len(), kept(&c.text, &all), all.len(), on_screen.len(), c.omitted.len()
            ));
        }
        eprintln!("FIXTURE-MEASUREMENT\n{}", rows.join("\n"));
    }
}
