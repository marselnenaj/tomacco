//! release.notes: Google Play "What's new" text per language from a section
//! of a Markdown changelog, checked against Play's 500-character limit.
use crate::core::*;
use regex::Regex;
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

/// Google Play's limit for the release notes of one language, in characters.
pub const LIMIT: usize = 500;
const LANGUAGE: &str = r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}";

/// The lines under the heading that names `wanted` (the first section when
/// empty), up to the next heading of the same or a higher level.
fn section(markdown: &str, wanted: &str) -> Option<(String, String)> {
    let heading = Regex::new(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$").unwrap();
    let wanted = wanted.trim().trim_matches(['[', ']']).to_lowercase();
    let wanted = wanted.strip_prefix('v').unwrap_or(&wanted).to_string();
    let mut found: Option<(usize, String)> = None;
    let mut body = Vec::new();
    for line in markdown.lines() {
        if let Some(c) = heading.captures(line) {
            let level = c[1].len();
            if let Some((at, _)) = &found {
                if level <= *at {
                    break;
                }
            } else if level >= 2 {
                let title = c[2].replace(['[', ']'], "");
                let lower = title.trim().to_lowercase();
                let lower = lower.strip_prefix('v').unwrap_or(&lower);
                let matches = wanted.is_empty()
                    || lower == wanted
                    || lower.starts_with(&format!("{wanted} "));
                if matches {
                    found = Some((level, title.trim().to_string()));
                }
                continue;
            }
        }
        if found.is_some() {
            body.push(line);
        }
    }
    found.map(|(_, title)| (title, body.join("\n")))
}

/// Plain text Play shows as it is: bullets as •, emphasis, code and link
/// markup removed, a bullet's continuation lines joined to it.
fn plain(markdown: &str) -> String {
    let link = Regex::new(r"\[([^\]]+)\]\([^)]*\)").unwrap();
    let strong = Regex::new(r"\*\*([^*]+)\*\*|__([^_]+)__").unwrap();
    let code = Regex::new(r"`([^`]*)`").unwrap();
    let bullet = Regex::new(r"^([ \t]*)[-*+][ \t]+(.*)$").unwrap();
    let heading = Regex::new(r"^#{1,6}[ \t]+(.*?)[ \t]*#*$").unwrap();
    let mut lines: Vec<String> = Vec::new();
    let mut in_bullet = false;
    for raw in markdown.lines() {
        let text = link.replace_all(raw, "$1");
        let text = strong.replace_all(&text, "$1$2");
        let text = code.replace_all(&text, "$1");
        let text = text.trim_end();
        if text.trim().is_empty() {
            in_bullet = false;
            if lines.last().is_some_and(|l| !l.is_empty()) {
                lines.push(String::new());
            }
            continue;
        }
        if let Some(c) = heading.captures(text) {
            in_bullet = false;
            lines.push(c[1].to_string());
        } else if let Some(c) = bullet.captures(text) {
            in_bullet = true;
            let depth = c[1].replace('\t', "  ").len() / 2;
            lines.push(format!("{}• {}", "  ".repeat(depth.min(3)), &c[2]));
        } else if in_bullet && raw.starts_with([' ', '\t']) {
            let last = lines.last_mut().unwrap();
            last.push(' ');
            last.push_str(text.trim());
        } else {
            in_bullet = false;
            lines.push(text.trim().to_string());
        }
    }
    while lines.last().is_some_and(String::is_empty) {
        lines.pop();
    }
    while lines.first().is_some_and(String::is_empty) {
        lines.remove(0);
    }
    lines.join("\n")
}

/// The text cut at whole lines to fit the limit, and how many were dropped.
fn shorten(text: &str) -> (String, usize) {
    let lines: Vec<&str> = text.lines().collect();
    for keep in (1..=lines.len()).rev() {
        let candidate = lines[..keep].join("\n").trim_end().to_string();
        if candidate.chars().count() <= LIMIT {
            return (candidate, lines.len() - keep);
        }
    }
    (text.chars().take(LIMIT).collect(), 0)
}

fn target(directory: &Path, layout: &str, language: &str, build: u64) -> PathBuf {
    match layout {
        "whatsnew" => directory.join(format!("whatsnew-{language}")),
        "play-publisher" => directory
            .join("release-notes")
            .join(language)
            .join("default.txt"),
        _ => directory
            .join(language)
            .join("changelogs")
            .join(if build > 0 {
                format!("{build}.txt")
            } else {
                "default.txt".into()
            }),
    }
}

pub fn notes(a: &Value) -> Result {
    let wanted = s(a, "section");
    let truncate = b(a, "truncate");
    let dry_run = b(a, "dry_run");
    let mut sources = vec![(
        if s(a, "language").is_empty() {
            "en-US"
        } else {
            s(a, "language")
        }
        .to_string(),
        s(a, "changelog").to_string(),
    )];
    for item in a["translations"].as_array().into_iter().flatten() {
        sources.push((
            s(item, "language").to_string(),
            s(item, "changelog").to_string(),
        ));
    }
    let mut seen = std::collections::HashSet::new();
    for (language, _) in &sources {
        identifier(language, "Play language code", Some(LANGUAGE))?;
        if !seen.insert(language.to_lowercase()) {
            return Err(Error::new(
                "invalid_argument",
                format!("{language} is given twice"),
            ));
        }
    }
    let directory = if s(a, "directory").is_empty() {
        None
    } else {
        Some(path(s(a, "directory"), true, false)?)
    };
    if directory.is_none() && !dry_run {
        return Err(Error::new(
            "invalid_argument",
            "Pass directory, or dry_run:true to only read the notes",
        ));
    }
    let layout = if s(a, "layout").is_empty() {
        "fastlane"
    } else {
        s(a, "layout")
    };
    let mut languages = Vec::new();
    let mut release_notes = Vec::new();
    let mut findings = Vec::new();
    for (language, changelog) in &sources {
        let file = path(changelog, false, true)?;
        let markdown = std::fs::read_to_string(&file)?;
        let Some((title, body)) = section(&markdown, wanted) else {
            findings.push(json!({"language":language,"severity":"error","code":"section_missing","message":format!("{} has no section {wanted:?}", file.display())}));
            continue;
        };
        let full = plain(&body);
        let length = full.chars().count();
        let mut entry =
            json!({"language":language,"changelog":file,"section":title,"characters":length});
        let text = if full.is_empty() {
            findings.push(json!({"language":language,"severity":"error","code":"empty","message":format!("Section {title} of {} has no text", file.display())}));
            full
        } else if length > LIMIT {
            if truncate {
                let (short, dropped) = shorten(&full);
                findings.push(json!({"language":language,"severity":"warning","code":"truncated","message":format!("{length} characters cut to {} by dropping the last {dropped} lines", short.chars().count()),"characters":length}));
                entry["characters"] = json!(short.chars().count());
                short
            } else {
                findings.push(json!({"language":language,"severity":"error","code":"too_long","message":format!("{length} characters; Google Play allows {LIMIT}"),"characters":length}));
                full
            }
        } else {
            full
        };
        if let Some(directory) = &directory {
            entry["path"] = json!(target(directory, layout, language, n(a, "build_number")));
        }
        entry["text"] = json!(text);
        languages.push(entry);
    }
    let passed = !findings.iter().any(|f| f["severity"] == "error");
    if passed {
        for entry in &languages {
            release_notes.push(json!({"language":entry["language"],"text":entry["text"]}));
        }
    }
    let mut written = false;
    if passed && !dry_run {
        for entry in &languages {
            let file = PathBuf::from(s(entry, "path"));
            if file.is_symlink() {
                return Err(Error::new(
                    "invalid_path",
                    format!("{} is a symbolic link", file.display()),
                ));
            }
            let mut text = s(entry, "text").to_string();
            text.push('\n');
            atomic_write(&file, text.as_bytes())?;
        }
        written = true;
    }
    let mut result = json!({"passed":passed,"limit":LIMIT,"layout":layout,"written":written,"languages":languages,"findings":findings});
    if passed {
        result["release_notes"] = json!(release_notes);
        result["next"] =
            json!({"operation":"play.prepare","arguments":{"release_notes":release_notes}});
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    const CHANGELOG: &str = "# Changelog\n\n## Unreleased\n\n- Nothing yet\n\n## [1.2.0] - 2026-10-30\n\n### Added\n\n- **Dark mode** for the\n  whole app.\n- Export to [CSV](https://example.com/csv) with `--all`.\n  - nested detail\n\n### Fixed\n\n* A crash on start.\n\n## 1.1.0\n\n- Old\n";

    #[test]
    fn a_section_is_found_by_version_or_first_and_made_plain() {
        let (title, body) = section(CHANGELOG, "1.2.0").unwrap();
        assert_eq!(title, "1.2.0 - 2026-10-30");
        assert_eq!(
            plain(&body),
            "Added\n\n• Dark mode for the whole app.\n• Export to CSV with --all.\n  • nested detail\n\nFixed\n\n• A crash on start."
        );
        assert_eq!(section(CHANGELOG, "").unwrap().0, "Unreleased");
        assert_eq!(plain(&section(CHANGELOG, "v1.1.0").unwrap().1), "• Old");
        assert!(section(CHANGELOG, "1.2").is_none());
        assert!(section(CHANGELOG, "2.0.0").is_none());
    }

    #[test]
    fn long_notes_are_cut_at_whole_lines() {
        let text = (0..40)
            .map(|i| format!("• Change number {i:02} here"))
            .collect::<Vec<_>>()
            .join("\n");
        let (short, dropped) = shorten(&text);
        assert!(short.chars().count() <= LIMIT);
        assert!(short.ends_with("here"));
        assert_eq!(short.lines().count() + dropped, 40);
        // Characters, not bytes: 400 accented letters fit.
        let accents = "é".repeat(400);
        assert_eq!(shorten(&accents), (accents.clone(), 0));
    }

    #[test]
    fn layouts_name_the_files_each_tool_reads() {
        let d = Path::new("/p");
        assert_eq!(
            target(d, "fastlane", "de-DE", 0),
            Path::new("/p/de-DE/changelogs/default.txt")
        );
        assert_eq!(
            target(d, "fastlane", "de-DE", 42),
            Path::new("/p/de-DE/changelogs/42.txt")
        );
        assert_eq!(
            target(d, "play-publisher", "de-DE", 42),
            Path::new("/p/release-notes/de-DE/default.txt")
        );
        assert_eq!(
            target(d, "whatsnew", "de-DE", 0),
            Path::new("/p/whatsnew-de-DE")
        );
    }
}
