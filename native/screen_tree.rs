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
        self.class.ends_with("EditText") || self.class.ends_with("AutoCompleteTextView")
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
