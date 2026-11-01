//! Reads an Android Gradle build without running Gradle: its app modules, their
//! application IDs, build types, flavors, variants and SDK levels, and the
//! versions catalog. What a build script computes at run time is reported as
//! unresolved, with the expression, instead of being guessed.
use regex::Regex;
use serde_json::{Map, Value, json};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};

/// Build files larger than this are not read.
const MAX_FILE: u64 = 1 << 20;

pub fn read_text(path: &Path) -> Option<String> {
    let meta = fs::metadata(path).ok()?;
    if !meta.is_file() || meta.len() > MAX_FILE {
        return None;
    }
    fs::read_to_string(path).ok()
}

/// The text with `//` and `/* */` comments blanked, strings kept.
pub fn strip_comments(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    let mut quote: Option<u8> = None;
    while i < bytes.len() {
        let c = bytes[i];
        if let Some(q) = quote {
            out.push(c);
            if c == b'\\' && i + 1 < bytes.len() {
                out.push(bytes[i + 1]);
                i += 2;
                continue;
            }
            if c == q || c == b'\n' {
                quote = None;
            }
            i += 1;
            continue;
        }
        if c == b'"' || c == b'\'' {
            quote = Some(c);
            out.push(c);
            i += 1;
        } else if c == b'/' && bytes.get(i + 1) == Some(&b'/') {
            while i < bytes.len() && bytes[i] != b'\n' {
                i += 1;
            }
        } else if c == b'/' && bytes.get(i + 1) == Some(&b'*') {
            i += 2;
            while i < bytes.len() && !(bytes[i] == b'*' && bytes.get(i + 1) == Some(&b'/')) {
                if bytes[i] == b'\n' {
                    out.push(b'\n');
                }
                i += 1;
            }
            i += 2;
        } else {
            out.push(c);
            i += 1;
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// The index just past the `}` that closes the `{` at `open`, skipping strings.
fn closing(text: &str, open: usize) -> Option<usize> {
    let bytes = text.as_bytes();
    let mut depth = 0usize;
    let mut quote: Option<u8> = None;
    let mut i = open;
    while i < bytes.len() {
        let c = bytes[i];
        if let Some(q) = quote {
            if c == b'\\' {
                i += 1;
            } else if c == q || c == b'\n' {
                quote = None;
            }
        } else if c == b'"' || c == b'\'' {
            quote = Some(c);
        } else if c == b'{' {
            depth += 1;
        } else if c == b'}' {
            depth -= 1;
            if depth == 0 {
                return Some(i + 1);
            }
        }
        i += 1;
    }
    None
}

/// The bodies of every `name { ... }` block in the text, outermost first.
pub fn blocks<'a>(text: &'a str, name: &str) -> Vec<&'a str> {
    let re = Regex::new(&format!(r"(?:^|[^\w.]){}\s*\{{", regex::escape(name))).unwrap();
    let mut found = Vec::new();
    let mut from = 0;
    while let Some(m) = re.find_at(text, from) {
        let open = m.end() - 1;
        match closing(text, open) {
            Some(end) => {
                found.push(&text[open + 1..end - 1]);
                from = end;
            }
            None => break,
        }
    }
    found
}

pub fn block<'a>(text: &'a str, name: &str) -> Option<&'a str> {
    blocks(text, name).into_iter().next()
}

/// The text of a block without its nested blocks.
pub fn flat(text: &str) -> String {
    let mut out = String::new();
    let mut i = 0;
    while let Some(offset) = text[i..].find('{') {
        let open = i + offset;
        out.push_str(&text[i..open]);
        match closing(text, open) {
            Some(end) => {
                out.push('\n');
                i = end;
            }
            None => return out,
        }
    }
    out.push_str(&text[i..]);
    out
}

/// The named blocks directly inside a block such as `buildTypes` or
/// `productFlavors`: `release { }`, `create("free") { }`, `getByName("debug") { }`.
pub fn children(text: &str) -> Vec<(String, &str)> {
    let header = Regex::new(
        r#"(?:^|[\s;}])(?:(?:create|getByName|register|maybeCreate|named)\s*\(\s*["']([\w-]+)["']\s*\)\s*(?:\.\s*(?:apply|configure)\s*)?|([A-Za-z_]\w*)|["']([\w-]+)["'])\s*$"#,
    )
    .unwrap();
    let mut found = Vec::new();
    let mut i = 0;
    let mut last = 0;
    while let Some(offset) = text[i..].find('{') {
        let open = i + offset;
        let Some(end) = closing(text, open) else {
            break;
        };
        if let Some(c) = header.captures(&text[last..open]) {
            let name = c
                .get(1)
                .or(c.get(2))
                .or(c.get(3))
                .map(|m| m.as_str())
                .unwrap_or_default();
            if ![
                "all",
                "configureEach",
                "each",
                "forEach",
                "apply",
                "configure",
            ]
            .contains(&name)
            {
                found.push((name.to_string(), &text[open + 1..end - 1]));
            }
        }
        i = end;
        last = end;
    }
    found
}

/// The raw expression assigned to one of `names` in a flat block:
/// `name = value`, `name value`, `name(value)` or `name.set(value)`.
pub fn property(text: &str, names: &[&str]) -> Option<String> {
    for name in names {
        let re = Regex::new(&format!(
            r"(?m)(?:^|[\s;(]){}(?:\s*=\s*|\s*\.set\(\s*|\s*\(\s*|[ \t]+)([^\n;]+)",
            regex::escape(name)
        ))
        .unwrap();
        if let Some(c) = re.captures(text) {
            let mut value = c[1].trim().to_string();
            // name(value) and name.set(value) leave a closing parenthesis.
            let opens = value.matches('(').count();
            let closes = value.matches(')').count();
            for _ in opens..closes {
                if let Some(stripped) = value.trim_end().strip_suffix(')') {
                    value = stripped.trim_end().to_string();
                }
            }
            if !value.is_empty() {
                return Some(value);
            }
        }
    }
    None
}

fn unquote(value: &str) -> Option<String> {
    let v = value.trim();
    for q in ['"', '\''] {
        if v.len() >= 2 && v.starts_with(q) && v.ends_with(q) && !v[1..v.len() - 1].contains(q) {
            return Some(v[1..v.len() - 1].to_string());
        }
    }
    None
}

/// A key of the versions catalog as Gradle's accessors spell it:
/// `android-compileSdk` and `android_compileSdk` are `android.compileSdk`.
pub fn accessor(key: &str) -> String {
    key.replace(['-', '_'], ".")
}

#[derive(Debug, Default, Clone)]
pub struct Catalog {
    pub versions: BTreeMap<String, String>,
    /// key -> (module "group:artifact", version or empty, line)
    pub libraries: Vec<CatalogEntry>,
    pub plugins: Vec<CatalogEntry>,
}

#[derive(Debug, Default, Clone)]
pub struct CatalogEntry {
    pub key: String,
    pub module: String,
    pub version: String,
    pub version_ref: String,
    pub line: usize,
}

/// One TOML value of the catalog subset: a string or an inline table.
fn toml_value(text: &str) -> (Option<String>, BTreeMap<String, String>) {
    let text = text.trim();
    if let Some(s) = unquote(text) {
        return (Some(s), BTreeMap::new());
    }
    let mut table = BTreeMap::new();
    if let Some(inner) = text
        .strip_prefix('{')
        .and_then(|t| t.trim_end().strip_suffix('}'))
    {
        // key = "value" or key = { k = "v", ... }; keys may be dotted.
        let pair = Regex::new(
            r#"([\w.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|\{([^}]*)\}|(true|false|[\d.]+))"#,
        )
        .unwrap();
        for c in pair.captures_iter(inner) {
            let key = c[1].to_string();
            if let Some(nested) = c.get(4) {
                for n in pair.captures_iter(nested.as_str()) {
                    let value = n.get(2).or(n.get(3)).or(n.get(5)).map(|m| m.as_str());
                    table.insert(format!("{key}.{}", &n[1]), value.unwrap_or("").into());
                }
            } else {
                let value = c.get(2).or(c.get(3)).or(c.get(5)).map(|m| m.as_str());
                table.insert(key, value.unwrap_or("").into());
            }
        }
    }
    (None, table)
}

/// Reads the `[versions]`, `[libraries]` and `[plugins]` of a versions catalog.
pub fn parse_catalog(text: &str) -> Catalog {
    let mut catalog = Catalog::default();
    let mut section = String::new();
    let entry = Regex::new(r#"^\s*("?)([\w.-]+)("?)\s*=\s*(.+?)\s*$"#).unwrap();
    for (index, raw) in text.lines().enumerate() {
        let line = match raw.find(" #") {
            Some(at) if !raw[..at].contains('"') || raw[..at].matches('"').count() % 2 == 0 => {
                &raw[..at]
            }
            _ => raw,
        };
        let trimmed = line.trim();
        if trimmed.starts_with('#') || trimmed.is_empty() {
            continue;
        }
        if trimmed.starts_with('[') {
            section = trimmed.trim_matches(['[', ']', ' ']).to_string();
            continue;
        }
        let Some(c) = entry.captures(line) else {
            continue;
        };
        let key = c[2].to_string();
        let (string, table) = toml_value(&c[4]);
        let first = |keys: &[&str]| {
            keys.iter()
                .find_map(|k| table.get(*k).cloned())
                .unwrap_or_default()
        };
        match section.as_str() {
            "versions" => {
                let value =
                    string.unwrap_or_else(|| first(&["strictly", "require", "prefer"]).to_string());
                catalog.versions.insert(key, value);
            }
            "libraries" | "plugins" => {
                let mut item = CatalogEntry {
                    key,
                    line: index + 1,
                    ..Default::default()
                };
                if let Some(s) = string {
                    // "group:artifact:version" or "plugin.id:version"
                    let parts: Vec<&str> = s.split(':').collect();
                    if section == "plugins" {
                        item.module = parts[0].to_string();
                        item.version = parts.get(1).unwrap_or(&"").to_string();
                    } else {
                        item.module = parts[..parts.len().min(2)].join(":");
                        item.version = parts.get(2).unwrap_or(&"").to_string();
                    }
                } else {
                    item.module = if section == "plugins" {
                        first(&["id"])
                    } else if table.contains_key("module") {
                        first(&["module"])
                    } else if table.contains_key("group") {
                        format!("{}:{}", first(&["group"]), first(&["name"]))
                    } else {
                        String::new()
                    };
                    item.version_ref = first(&["version.ref"]);
                    item.version = first(&[
                        "version",
                        "version.strictly",
                        "version.require",
                        "version.prefer",
                    ]);
                }
                if !item.module.is_empty() {
                    if section == "plugins" {
                        catalog.plugins.push(item);
                    } else {
                        catalog.libraries.push(item);
                    }
                }
            }
            _ => (),
        }
    }
    catalog
}

impl Catalog {
    pub fn version_of(&self, entry: &CatalogEntry) -> String {
        if entry.version_ref.is_empty() {
            entry.version.clone()
        } else {
            self.versions
                .get(&entry.version_ref)
                .cloned()
                .unwrap_or_default()
        }
    }
    fn version_by_accessor(&self, path: &str) -> Option<String> {
        self.versions
            .iter()
            .find(|(k, _)| accessor(k) == path)
            .map(|(_, v)| v.clone())
    }
    fn plugin_by_accessor(&self, path: &str) -> Option<&CatalogEntry> {
        self.plugins.iter().find(|p| accessor(&p.key) == path)
    }
}

/// Values a module's expressions can refer to: the catalog, `ext` and
/// `extra` properties, gradle.properties and the file's own `val`s.
#[derive(Default)]
struct Context {
    catalog: Catalog,
    properties: BTreeMap<String, String>,
}

fn collect_properties(text: &str, into: &mut BTreeMap<String, String>) {
    let assignments = [
        r#"(?m)^\s*(?:val|def|var)\s+(\w+)\s*(?::\s*\w+\s*)?=\s*([^\n;]+)"#,
        r#"(?m)^\s*(?:rootProject\.|project\.)?ext(?:ra)?\.(\w+)\s*=\s*([^\n;]+)"#,
        r#"(?m)extra\[\s*"(\w+)"\s*\]\s*=\s*([^\n;]+)"#,
        r#"(?m)extra\.set\(\s*"(\w+)"\s*,\s*([^\n;]+)\)"#,
        r#"(?m)^\s*val\s+(\w+)\s+by\s+extra\(\s*([^\n;]+)\)"#,
    ];
    for pattern in assignments {
        for c in Regex::new(pattern).unwrap().captures_iter(text) {
            into.entry(c[1].to_string())
                .or_insert_with(|| c[2].trim().to_string());
        }
    }
    let assignment = Regex::new(r#"(?m)^\s*(\w+)\s*=\s*([^\n;]+)"#).unwrap();
    for ext in blocks(text, "ext") {
        for c in assignment.captures_iter(&flat(ext)) {
            into.entry(c[1].to_string())
                .or_insert_with(|| c[2].trim().to_string());
        }
    }
}

fn read_gradle_properties(path: &Path, into: &mut BTreeMap<String, String>) {
    if let Some(text) = read_text(path) {
        for line in text.lines() {
            let line = line.trim();
            if line.starts_with('#') || line.starts_with('!') {
                continue;
            }
            if let Some((k, v)) = line.split_once(['=', ':']) {
                into.entry(k.trim().to_string())
                    .or_insert_with(|| format!("\"{}\"", v.trim()));
            }
        }
    }
}

impl Context {
    /// Resolves an expression to a value, or None when only Gradle could.
    fn resolve(&self, expression: &str, depth: usize) -> Option<Value> {
        if depth > 8 {
            return None;
        }
        let mut e = expression.trim().trim_end_matches(',').trim();
        for suffix in [
            ".toInteger()",
            ".toInt()",
            ".get()",
            " as Integer",
            " as Int",
            " as String",
            ".toString()",
            "!!",
        ] {
            while let Some(rest) = e.strip_suffix(suffix) {
                e = rest.trim_end();
            }
        }
        if let Some(inner) = e.strip_prefix('(').and_then(|v| v.strip_suffix(')')) {
            return self.resolve(inner, depth + 1);
        }
        if let Some(text) = unquote(e) {
            if text.contains('$') {
                return None;
            }
            return Some(match text.parse::<u64>() {
                Ok(n) => json!(n),
                Err(_) => json!(text),
            });
        }
        if let Ok(n) = e.parse::<u64>() {
            return Some(json!(n));
        }
        let call = Regex::new(
            r#"^(?:release|Integer\.parseInt|Integer\.valueOf|providers\.gradleProperty|project\.property|property|findProperty|project\.findProperty|rootProject\.findProperty)\(\s*(.+?)\s*\)(?:\s*\{[^}]*\})?$"#,
        )
        .unwrap();
        if let Some(c) = call.captures(e) {
            let inner = c[1].to_string();
            if e.starts_with("release") || e.starts_with("Integer") {
                return self.resolve(&inner, depth + 1);
            }
            let name = unquote(&inner)?;
            return self
                .properties
                .get(&name)
                .and_then(|v| self.resolve(v, depth + 1));
        }
        if let Some(path) = e.strip_prefix("libs.versions.") {
            return self
                .catalog
                .version_by_accessor(path)
                .map(|v| self.resolve(&format!("\"{v}\""), depth + 1))?;
        }
        let property = Regex::new(
            r#"^(?:(?:rootProject|project)\.)?(?:(?:ext|extra|properties)(?:\.get\(\s*["'](\w+)["']\s*\)|\[\s*["'](\w+)["']\s*\]|\.(\w+))|(\w+))$"#,
        )
        .unwrap();
        if let Some(c) = property.captures(e) {
            let name = c.get(1).or(c.get(2)).or(c.get(3)).or(c.get(4))?.as_str();
            if let Some(value) = self.properties.get(name) {
                return self.resolve(value, depth + 1);
            }
        }
        None
    }
}

fn settings_includes(text: &str) -> Vec<String> {
    let text = strip_comments(text);
    let mut modules = Vec::new();
    let statement = Regex::new(r#"(?m)^\s*include\s*\(?([^\n]*)"#).unwrap();
    let quoted = Regex::new(r#"["'](:?[\w.:-]+)["']"#).unwrap();
    for c in statement.captures_iter(&text) {
        for q in quoted.captures_iter(&c[1]) {
            let name = q[1].trim_start_matches(':').to_string();
            if !name.is_empty() && !modules.contains(&name) {
                modules.push(name);
            }
        }
    }
    modules
}

fn project_dirs(text: &str) -> BTreeMap<String, String> {
    let re = Regex::new(
        r#"project\(\s*["']:?([\w.:-]+)["']\s*\)\.projectDir\s*=\s*(?:file|File)\(\s*["']([^"']+)["']\s*\)"#,
    )
    .unwrap();
    re.captures_iter(&strip_comments(text))
        .map(|c| (c[1].to_string(), c[2].to_string()))
        .collect()
}

pub fn build_file(dir: &Path) -> Option<PathBuf> {
    ["build.gradle.kts", "build.gradle"]
        .iter()
        .map(|f| dir.join(f))
        .find(|p| p.is_file())
}

pub fn settings_file(dir: &Path) -> Option<PathBuf> {
    ["settings.gradle.kts", "settings.gradle"]
        .iter()
        .map(|f| dir.join(f))
        .find(|p| p.is_file())
}

/// The versions catalog of a build, `gradle/libs.versions.toml` by default.
pub fn catalog_file(dir: &Path) -> Option<PathBuf> {
    let default = dir.join("gradle/libs.versions.toml");
    default.is_file().then_some(default)
}

/// Every module directory of a build: the root and its includes, as
/// (Gradle path, directory). Includes outside the build are skipped.
pub fn modules_of(dir: &Path) -> Vec<(String, PathBuf)> {
    let mut found = vec![(String::new(), dir.to_path_buf())];
    if let Some(text) = settings_file(dir).and_then(|p| read_text(&p)) {
        let dirs = project_dirs(&text);
        for name in settings_includes(&text) {
            let relative = dirs
                .get(&name)
                .cloned()
                .unwrap_or_else(|| name.replace(':', "/"));
            if relative.starts_with('/') || relative.split('/').any(|p| p == "..") {
                continue;
            }
            found.push((name, dir.join(relative)));
        }
    }
    found
}

/// The script without plugin declarations marked `apply false`, with which a
/// root build declares plugins for its modules.
fn without_apply_false(text: &str) -> String {
    Regex::new(r"(?m)^[^\n]*\bapply\s*\(?\s*false[^\n]*$")
        .unwrap()
        .replace_all(text, "")
        .into_owned()
}

/// Whether a build script applies the Android application plugin, directly,
/// through a catalog alias or through a convention plugin named for it.
fn applies_application(text: &str, catalog: &Catalog) -> bool {
    let text = &without_apply_false(text);
    if text.contains("com.android.application") {
        return true;
    }
    let alias = Regex::new(r#"alias\(\s*libs\.plugins\.([\w.]+)\s*\)"#).unwrap();
    for c in alias.captures_iter(text) {
        match catalog.plugin_by_accessor(&c[1]) {
            Some(p) if p.module == "com.android.application" => return true,
            None if accessor(&c[1]).to_lowercase().replace('.', "") == "androidapplication" => {
                return true;
            }
            _ => (),
        }
    }
    Regex::new(r#"id\s*\(?\s*["'][\w.-]*android[.-]application[\w.-]*["']"#)
        .unwrap()
        .is_match(text)
}

fn capitalize(value: &str) -> String {
    let mut chars = value.chars();
    match chars.next() {
        Some(c) => c.to_uppercase().collect::<String>() + chars.as_str(),
        None => String::new(),
    }
}

/// The string quoted in each of the arguments, in order.
fn quoted_list(text: &str) -> Vec<String> {
    Regex::new(r#"["']([\w-]+)["']"#)
        .unwrap()
        .captures_iter(text)
        .map(|c| c[1].to_string())
        .collect()
}

struct Field<'a> {
    name: &'a str,
    names: &'a [&'a str],
}

const FIELDS: &[Field] = &[
    Field {
        name: "application_id",
        names: &["applicationId"],
    },
    Field {
        name: "min_sdk",
        names: &["minSdk", "minSdkVersion"],
    },
    Field {
        name: "target_sdk",
        names: &["targetSdk", "targetSdkVersion"],
    },
];

fn read_field(
    ctx: &Context,
    block: &str,
    field: &Field,
    unresolved: &mut Vec<Value>,
    place: &str,
) -> Value {
    let text = flat(block);
    // AGP 9: minSdk { version = release(26) }
    for name in field.names {
        if let Some(inner) = blocks(block, name).first() {
            if let Some(expression) = property(&flat(inner), &["version"]) {
                return match ctx.resolve(&expression, 0) {
                    Some(v) => v,
                    None => {
                        unresolved
                            .push(json!({"field":field.name,"in":place,"expression":expression}));
                        Value::Null
                    }
                };
            }
        }
    }
    match property(&text, field.names) {
        Some(expression) => match ctx.resolve(&expression, 0) {
            Some(v) => v,
            None => {
                unresolved.push(json!({"field":field.name,"in":place,"expression":expression}));
                Value::Null
            }
        },
        None => Value::Null,
    }
}

fn read_string(
    ctx: &Context,
    block: &str,
    names: &[&str],
    field: &str,
    place: &str,
    unresolved: &mut Vec<Value>,
) -> Option<String> {
    let expression = property(&flat(block), names)?;
    match ctx.resolve(&expression, 0) {
        Some(Value::String(s)) => Some(s),
        Some(other) => Some(other.to_string()),
        None => {
            unresolved.push(json!({"field":field,"in":place,"expression":expression}));
            None
        }
    }
}

/// One app module's model.
fn app_module(ctx: &Context, gradle_path: &str, dir: &Path, script: &str) -> Value {
    let mut unresolved = Vec::new();
    let android = blocks(script, "android")
        .into_iter()
        .max_by_key(|b| b.len())
        .unwrap_or("");
    let default_config = block(android, "defaultConfig").unwrap_or("");
    let namespace = read_string(
        ctx,
        android,
        &["namespace"],
        "namespace",
        "android",
        &mut unresolved,
    );
    let mut fields = Map::new();
    for field in FIELDS {
        fields.insert(
            field.name.into(),
            read_field(ctx, default_config, field, &mut unresolved, "defaultConfig"),
        );
    }
    let compile = Field {
        name: "compile_sdk",
        names: &["compileSdk", "compileSdkVersion"],
    };
    fields.insert(
        "compile_sdk".into(),
        read_field(ctx, android, &compile, &mut unresolved, "android"),
    );
    let base_id = fields["application_id"].as_str().map(String::from);
    let id_source = if base_id.is_some() {
        "applicationId"
    } else if namespace.is_some() {
        "namespace"
    } else {
        ""
    };
    let base_id = base_id.or(namespace.clone());

    // Build types: debug and release always exist.
    let mut build_types: Vec<(String, String)> = vec![
        ("debug".into(), String::new()),
        ("release".into(), String::new()),
    ];
    if let Some(types) = block(android, "buildTypes") {
        for (name, body) in children(types) {
            let place = format!("buildTypes.{name}");
            let suffix = read_string(
                ctx,
                body,
                &["applicationIdSuffix"],
                "application_id_suffix",
                &place,
                &mut unresolved,
            )
            .unwrap_or_default();
            match build_types.iter_mut().find(|(n, _)| *n == name) {
                Some(entry) => entry.1 = suffix,
                None => build_types.push((name, suffix)),
            }
        }
    }
    // Flavor dimensions in priority order.
    let mut dimensions = Vec::new();
    let flat_android = flat(android);
    for c in Regex::new(r#"(?m)flavorDimensions[^\n]*"#)
        .unwrap()
        .find_iter(&flat_android)
    {
        for d in quoted_list(c.as_str()) {
            if !dimensions.contains(&d) {
                dimensions.push(d);
            }
        }
    }
    let mut flavors = Vec::new();
    if let Some(block) = block(android, "productFlavors") {
        for (name, body) in children(block) {
            let place = format!("productFlavors.{name}");
            let text = flat(body);
            let dimension = property(&text, &["dimension"])
                .and_then(|d| unquote(&d))
                .or_else(|| (dimensions.len() == 1).then(|| dimensions[0].clone()))
                .unwrap_or_default();
            if !dimension.is_empty() && !dimensions.contains(&dimension) {
                dimensions.push(dimension.clone());
            }
            let mut flavor = json!({"name":name,"dimension":dimension});
            if let Some(id) = read_string(
                ctx,
                body,
                &["applicationId"],
                "application_id",
                &place,
                &mut unresolved,
            ) {
                flavor["application_id"] = json!(id);
            }
            if let Some(suffix) = read_string(
                ctx,
                body,
                &["applicationIdSuffix"],
                "application_id_suffix",
                &place,
                &mut unresolved,
            ) {
                flavor["application_id_suffix"] = json!(suffix);
            }
            for field in &FIELDS[1..] {
                let value = read_field(ctx, body, field, &mut unresolved, &place);
                if !value.is_null() {
                    flavor[field.name] = value;
                }
            }
            flavors.push(flavor);
        }
    }
    // Every combination of one flavor per dimension, then each build type.
    let mut combinations: Vec<Vec<&Value>> = vec![vec![]];
    if !flavors.is_empty() {
        let groups: Vec<Vec<&Value>> = if dimensions.is_empty() {
            vec![flavors.iter().collect()]
        } else {
            dimensions
                .iter()
                .map(|d| flavors.iter().filter(|f| f["dimension"] == *d).collect())
                .filter(|g: &Vec<&Value>| !g.is_empty())
                .collect()
        };
        for group in groups {
            combinations = combinations
                .into_iter()
                .flat_map(|prefix| {
                    group.iter().map(move |f| {
                        let mut next = prefix.clone();
                        next.push(*f);
                        next
                    })
                })
                .collect();
        }
    }
    let mut variants = Vec::new();
    for combination in &combinations {
        for (build_type, type_suffix) in &build_types {
            let mut name = String::new();
            for f in combination {
                let part = f["name"].as_str().unwrap_or_default();
                name.push_str(&if name.is_empty() {
                    part.to_string()
                } else {
                    capitalize(part)
                });
            }
            name.push_str(&if name.is_empty() {
                build_type.clone()
            } else {
                capitalize(build_type)
            });
            // The first dimension's applicationId wins; suffixes add up.
            let id = combination
                .iter()
                .find_map(|f| f["application_id"].as_str().map(String::from))
                .or(base_id.clone())
                .map(|mut id| {
                    for f in combination {
                        if let Some(s) = f["application_id_suffix"].as_str() {
                            id.push_str(s);
                        }
                    }
                    id.push_str(type_suffix);
                    id
                });
            variants.push(json!({
                "name":name,
                "build_type":build_type,
                "flavors":combination.iter().map(|f| f["name"].clone()).collect::<Vec<_>>(),
                "application_id":id,
            }));
        }
    }
    let mut module = json!({
        "module": if gradle_path.is_empty() { String::new() } else { format!(":{gradle_path}") },
        "directory": dir,
        "namespace": namespace,
        "application_id": base_id,
        "application_id_source": id_source,
        "build_types": build_types.iter().map(|(n, _)| n.clone()).collect::<Vec<_>>(),
        "flavor_dimensions": dimensions,
        "flavors": flavors,
        "variants": variants,
        "unresolved": unresolved,
    });
    for (k, v) in fields {
        if k != "application_id" {
            module[k] = v;
        }
    }
    if android.is_empty() {
        module["note"] = json!(
            "No android block in this module's build file; a convention plugin configures it, and only Gradle can report its settings"
        );
    }
    module
}

/// The Android app modules of the Gradle build in `dir`, read from its files.
pub fn read(dir: &Path) -> Value {
    let mut ctx = Context::default();
    if let Some(text) = catalog_file(dir).and_then(|p| read_text(&p)) {
        ctx.catalog = parse_catalog(&text);
    }
    read_gradle_properties(&dir.join("gradle.properties"), &mut ctx.properties);
    for file in [
        "build.gradle",
        "build.gradle.kts",
        "variables.gradle",
        "gradle/variables.gradle",
    ] {
        if let Some(text) = read_text(&dir.join(file)) {
            collect_properties(&strip_comments(&text), &mut ctx.properties);
        }
    }
    let mut apps = Vec::new();
    for (gradle_path, module_dir) in modules_of(dir) {
        let Some(script) = build_file(&module_dir).and_then(|p| read_text(&p)) else {
            continue;
        };
        let script = strip_comments(&script);
        if !applies_application(&script, &ctx.catalog) {
            continue;
        }
        let mut local = Context {
            catalog: ctx.catalog.clone(),
            properties: BTreeMap::new(),
        };
        collect_properties(&script, &mut local.properties);
        for (k, v) in &ctx.properties {
            local.properties.entry(k.clone()).or_insert(v.clone());
        }
        apps.push(app_module(&local, &gradle_path, &module_dir, &script));
    }
    let mut ids: Vec<String> = Vec::new();
    for app in &apps {
        for variant in app["variants"].as_array().into_iter().flatten() {
            if let Some(id) = variant["application_id"].as_str() {
                if !ids.iter().any(|i| i == id) {
                    ids.push(id.to_string());
                }
            }
        }
    }
    json!({"app_modules":apps,"application_ids":ids,"read_from":"build files, without running Gradle"})
}

/// Whether a build script applies a plugin, by id or through a catalog alias,
/// or an alias whose name, without separators, is one of `names`.
fn applies(text: &str, catalog: &Catalog, ids: &[&str], names: &[&str]) -> bool {
    let quoted = Regex::new(r#"["']([\w.-]+)["']"#).unwrap();
    if quoted.captures_iter(text).any(|c| ids.contains(&&c[1])) {
        return true;
    }
    let alias = Regex::new(r#"alias\(\s*libs\.plugins\.([\w.]+)\s*\)"#).unwrap();
    alias
        .captures_iter(text)
        .any(|c| match catalog.plugin_by_accessor(&c[1]) {
            Some(p) => ids.contains(&p.module.as_str()),
            None => names.contains(&accessor(&c[1]).to_lowercase().replace('.', "").as_str()),
        })
}

/// What iOS needs in a Kotlin Multiplatform project.
pub const KMP_IOS_NEEDS: &str = "Kotlin/Native links iOS frameworks only on macOS with Xcode; on Linux Gradle skips the Apple binaries. Build the iOS app on a Mac: configure it with remote.configure, then use apple.build for the Xcode project or remote.run for Gradle.";

/// A Kotlin Multiplatform build: its modules and their targets, whether it
/// uses Compose Multiplatform, and what its iOS side needs. None for a build
/// without the Kotlin Multiplatform plugin.
pub fn multiplatform(dir: &Path) -> Option<Value> {
    let catalog = catalog_file(dir)
        .and_then(|p| read_text(&p))
        .map(|t| parse_catalog(&t))
        .unwrap_or_default();
    let target = Regex::new(
        r"(?:^|[^\w.])(androidTarget|androidLibrary|iosX64|iosArm64|iosSimulatorArm64|jvm|js|wasmJs|wasmWasi|macosX64|macosArm64|linuxX64|linuxArm64|mingwX64|tvosArm64|watchosArm64)\s*[({]",
    )
    .unwrap();
    let mut modules = Vec::new();
    let mut compose = false;
    let mut ios = Vec::new();
    for (gradle_path, module_dir) in modules_of(dir) {
        let Some(script) = build_file(&module_dir).and_then(|p| read_text(&p)) else {
            continue;
        };
        let script = strip_comments(&script);
        let script = without_apply_false(&script);
        if script.contains("kotlin(\"multiplatform\")")
            || applies(
                &script,
                &catalog,
                &["org.jetbrains.kotlin.multiplatform"],
                &["kotlinmultiplatform", "multiplatform"],
            )
        {
            let mut targets: Vec<String> = Vec::new();
            for body in blocks(&script, "kotlin") {
                for c in target.captures_iter(body) {
                    let name = c[1].replace("androidLibrary", "androidTarget");
                    if !targets.contains(&name) {
                        targets.push(name);
                    }
                }
            }
            for t in targets.iter().filter(|t| t.starts_with("ios")) {
                if !ios.contains(t) {
                    ios.push(t.clone());
                }
            }
            compose |= applies(
                &script,
                &catalog,
                &["org.jetbrains.compose"],
                &["composemultiplatform", "jetbrainscompose"],
            );
            modules.push(json!({
                "module": if gradle_path.is_empty() { String::new() } else { format!(":{gradle_path}") },
                "targets": targets,
            }));
        }
    }
    if modules.is_empty() {
        return None;
    }
    let xcode = fs::read_dir(dir)
        .into_iter()
        .flatten()
        .filter_map(|e| e.ok())
        .filter(|e| e.path().is_dir())
        .flat_map(|e| {
            fs::read_dir(e.path())
                .into_iter()
                .flatten()
                .filter_map(|e| e.ok())
        })
        .map(|e| e.path())
        .find(|p| p.extension().is_some_and(|x| x == "xcodeproj"));
    Some(json!({
        "compose_multiplatform": compose,
        "modules": modules,
        "ios": {
            "targets": ios,
            "xcode_project": xcode,
            "builds_on_linux": false,
            "needs": KMP_IOS_NEEDS,
        },
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(root: &Path, relative: &str, text: &str) {
        let file = root.join(relative);
        fs::create_dir_all(file.parent().unwrap()).unwrap();
        fs::write(file, text).unwrap();
    }

    #[test]
    fn comments_go_and_strings_stay() {
        let text = "a = \"http://x\" // gone\n/* block\n */b = 'c'";
        assert_eq!(strip_comments(text), "a = \"http://x\" \n\nb = 'c'");
    }

    #[test]
    fn blocks_children_and_properties_are_found() {
        let text = "android {\n defaultConfig { minSdk = 24 }\n buildTypes {\n  release { isMinifyEnabled = true }\n  create(\"staging\") { applicationIdSuffix = \".staging\" }\n  getByName(\"debug\") { }\n }\n}";
        let android = block(text, "android").unwrap();
        assert_eq!(
            property(&flat(block(android, "defaultConfig").unwrap()), &["minSdk"]).unwrap(),
            "24"
        );
        let names: Vec<_> = children(block(android, "buildTypes").unwrap())
            .into_iter()
            .map(|c| c.0)
            .collect();
        assert_eq!(names, ["release", "staging", "debug"]);
        assert_eq!(
            property("minSdkVersion(21)", &["minSdkVersion"]).unwrap(),
            "21"
        );
        assert_eq!(
            property("  minSdkVersion 21", &["minSdkVersion"]).unwrap(),
            "21"
        );
        assert!(property("minSdkVersion 21", &["minSdk"]).is_none());
    }

    #[test]
    fn the_catalog_subset_is_read() {
        let catalog = parse_catalog(
            "[versions]\nagp = \"8.7.3\" # comment\nandroid-minSdk = \"24\"\nkotlin = { strictly = \"2.1.0\" }\n\n[libraries]\ncore = { module = \"androidx.core:core-ktx\", version.ref = \"core\" }\nlegacy = \"com.squareup:okio:3.9.0\"\nsplit = { group = \"com.google\", name = \"gson\", version = \"2.11.0\" }\nbom = { group = \"androidx.compose\", name = \"compose-bom\", version = { strictly = \"2024.01.00\" } }\n[plugins]\nandroid-application = { id = \"com.android.application\", version.ref = \"agp\" }\nshort = \"org.example.plugin:1.0\"\n",
        );
        assert_eq!(catalog.versions["agp"], "8.7.3");
        assert_eq!(catalog.versions["kotlin"], "2.1.0");
        assert_eq!(catalog.libraries.len(), 4);
        assert_eq!(catalog.libraries[0].module, "androidx.core:core-ktx");
        assert_eq!(catalog.libraries[0].version_ref, "core");
        assert_eq!(catalog.libraries[1].version, "3.9.0");
        assert_eq!(catalog.libraries[2].module, "com.google:gson");
        assert_eq!(catalog.libraries[3].version, "2024.01.00");
        assert_eq!(catalog.version_of(&catalog.plugins[0]), "8.7.3");
        assert_eq!(catalog.libraries[2].line, 9);
        assert_eq!(catalog.plugins[1].module, "org.example.plugin");
        assert_eq!(catalog.plugins[1].version, "1.0");
        assert_eq!(catalog.version_by_accessor("android.minSdk").unwrap(), "24");
    }

    #[test]
    fn a_kotlin_dsl_app_with_flavors_and_a_catalog_is_read() {
        let root = tempfile::tempdir().unwrap();
        let root = root.path();
        write(
            root,
            "settings.gradle.kts",
            "rootProject.name = \"Demo\"\ninclude(\":app\")\ninclude(\":core:data\")\n",
        );
        write(
            root,
            "gradle/libs.versions.toml",
            "[versions]\nandroid-minSdk = \"24\"\ntargetSdk = \"35\"\n[plugins]\napp = { id = \"com.android.application\", version = \"8.7.3\" }\n",
        );
        write(
            root,
            "build.gradle.kts",
            "plugins {\n alias(libs.plugins.app) apply false\n id(\"com.android.application\") version \"8.7.3\" apply false\n}\n",
        );
        write(
            root,
            "core/data/build.gradle.kts",
            "plugins { id(\"com.android.library\") }\nandroid { namespace = \"dev.demo.data\" }\n",
        );
        write(
            root,
            "app/build.gradle.kts",
            r#"plugins { alias(libs.plugins.app) }
android {
    namespace = "dev.demo"
    compileSdk { version = release(36) }
    defaultConfig {
        applicationId = "dev.demo.app" // the store id
        minSdk = libs.versions.android.minSdk.get().toInt()
        targetSdk = libs.versions.targetSdk.get().toInt()
    }
    buildTypes {
        debug { applicationIdSuffix = ".debug" }
        create("benchmark") { initWith(getByName("release")) }
    }
    flavorDimensions += listOf("tier", "env")
    productFlavors {
        create("free") { dimension = "tier"; applicationIdSuffix = ".free" }
        create("paid") { dimension = "tier" }
        create("prod") { dimension = "env" }
        create("dev") { dimension = "env"; applicationId = "dev.demo.internal"; minSdk = 30 }
    }
}
"#,
        );
        let model = read(root);
        let apps = model["app_modules"].as_array().unwrap();
        assert_eq!(apps.len(), 1);
        let app = &apps[0];
        assert_eq!(app["module"], ":app");
        assert_eq!(app["application_id"], "dev.demo.app");
        assert_eq!(app["namespace"], "dev.demo");
        assert_eq!(app["min_sdk"], 24);
        assert_eq!(app["target_sdk"], 35);
        assert_eq!(app["compile_sdk"], 36);
        assert_eq!(app["build_types"], json!(["debug", "release", "benchmark"]));
        assert_eq!(app["flavor_dimensions"], json!(["tier", "env"]));
        assert_eq!(app["flavors"][3]["min_sdk"], 30);
        let variants = app["variants"].as_array().unwrap();
        assert_eq!(variants.len(), 12);
        let id = |name: &str| {
            variants.iter().find(|v| v["name"] == name).unwrap()["application_id"].clone()
        };
        assert_eq!(id("freeProdDebug"), "dev.demo.app.free.debug");
        assert_eq!(id("paidProdRelease"), "dev.demo.app");
        assert_eq!(id("freeDevBenchmark"), "dev.demo.internal.free");
        assert!(app["unresolved"].as_array().unwrap().is_empty());
        assert!(
            model["application_ids"]
                .as_array()
                .unwrap()
                .contains(&json!("dev.demo.app.free.debug"))
        );
    }

    #[test]
    fn a_groovy_react_native_style_app_resolves_root_ext_values() {
        let root = tempfile::tempdir().unwrap();
        let root = root.path();
        write(
            root,
            "settings.gradle",
            "rootProject.name = 'Demo'\ninclude ':app'\nincludeBuild('../node_modules/@react-native/gradle-plugin')\n",
        );
        write(
            root,
            "build.gradle",
            "buildscript {\n    ext {\n        buildToolsVersion = \"35.0.0\"\n        minSdkVersion = 24\n        compileSdkVersion = 35\n        targetSdkVersion = 34\n    }\n}\n",
        );
        write(
            root,
            "app/build.gradle",
            "apply plugin: \"com.android.application\"\napply plugin: \"com.facebook.react\"\nandroid {\n    ndkVersion rootProject.ext.ndkVersion\n    compileSdk rootProject.ext.compileSdkVersion\n    namespace \"com.demo\"\n    defaultConfig {\n        applicationId \"com.demo\"\n        minSdkVersion rootProject.ext.minSdkVersion\n        targetSdkVersion rootProject.ext.targetSdkVersion\n        versionCode 1\n    }\n    buildTypes {\n        debug {\n            signingConfig signingConfigs.debug\n        }\n        release {\n            minifyEnabled enableProguardInReleaseBuilds\n        }\n    }\n}\n",
        );
        let app = &read(root)["app_modules"][0];
        assert_eq!(app["application_id"], "com.demo");
        assert_eq!(app["min_sdk"], 24);
        assert_eq!(app["target_sdk"], 34);
        assert_eq!(app["compile_sdk"], 35);
        assert_eq!(app["variants"][0]["name"], "debug");
        assert_eq!(app["variants"][0]["application_id"], "com.demo");
    }

    #[test]
    fn computed_values_are_reported_as_unresolved_and_namespace_stands_in() {
        let root = tempfile::tempdir().unwrap();
        let root = root.path();
        write(root, "settings.gradle.kts", "include(\":androidApp\")\n");
        write(
            root,
            "androidApp/build.gradle.kts",
            "plugins { id(\"com.android.application\") }\nandroid {\n namespace = \"dev.kmp.app\"\n defaultConfig { minSdk = computeMin() }\n}\n",
        );
        let app = &read(root)["app_modules"][0];
        assert_eq!(app["module"], ":androidApp");
        assert_eq!(app["application_id"], "dev.kmp.app");
        assert_eq!(app["application_id_source"], "namespace");
        assert_eq!(app["min_sdk"], Value::Null);
        assert_eq!(app["unresolved"][0]["field"], "min_sdk");
        assert_eq!(app["unresolved"][0]["expression"], "computeMin()");
    }

    #[test]
    fn a_convention_plugin_module_is_found_but_not_guessed() {
        let root = tempfile::tempdir().unwrap();
        let root = root.path();
        write(
            root,
            "settings.gradle.kts",
            "include(\":app\", \":outside\")\nproject(\":outside\").projectDir = file(\"../elsewhere\")\n",
        );
        write(
            root,
            "app/build.gradle.kts",
            "plugins { id(\"nowinandroid.android.application\") }\n",
        );
        let model = read(root);
        let app = &model["app_modules"][0];
        assert_eq!(model["app_modules"].as_array().unwrap().len(), 1);
        assert_eq!(app["application_id"], Value::Null);
        assert!(app["note"].as_str().unwrap().contains("convention plugin"));
        assert_eq!(model["application_ids"], json!([]));
    }

    #[test]
    fn a_compose_multiplatform_layout_is_recognised_with_its_ios_needs() {
        let root = tempfile::tempdir().unwrap();
        let root = root.path();
        write(
            root,
            "settings.gradle.kts",
            "rootProject.name = \"Kmp\"\ninclude(\":composeApp\")\ninclude(\":shared\")\n",
        );
        write(
            root,
            "gradle/libs.versions.toml",
            "[versions]\nandroid-minSdk = \"24\"\n[plugins]\nandroidApplication = { id = \"com.android.application\", version = \"8.7.3\" }\nkotlinMultiplatform = { id = \"org.jetbrains.kotlin.multiplatform\", version = \"2.2.10\" }\ncomposeMultiplatform = { id = \"org.jetbrains.compose\", version = \"1.9.0\" }\n",
        );
        write(
            root,
            "build.gradle.kts",
            "plugins {\n alias(libs.plugins.androidApplication) apply false\n alias(libs.plugins.kotlinMultiplatform) apply false\n}\n",
        );
        write(
            root,
            "composeApp/build.gradle.kts",
            "plugins {\n alias(libs.plugins.kotlinMultiplatform)\n alias(libs.plugins.androidApplication)\n alias(libs.plugins.composeMultiplatform)\n}\nkotlin {\n androidTarget { }\n listOf(iosX64(), iosArm64(), iosSimulatorArm64()).forEach { it.binaries.framework { baseName = \"ComposeApp\" } }\n jvm(\"desktop\")\n}\nandroid {\n namespace = \"dev.kmp\"\n defaultConfig { applicationId = \"dev.kmp.app\"; minSdk = libs.versions.android.minSdk.get().toInt() }\n}\n",
        );
        write(
            root,
            "shared/build.gradle.kts",
            "plugins { kotlin(\"multiplatform\") }\nkotlin { jvm() }\n",
        );
        fs::create_dir_all(root.join("iosApp/iosApp.xcodeproj")).unwrap();
        let mp = multiplatform(root).unwrap();
        assert_eq!(mp["compose_multiplatform"], true);
        assert_eq!(mp["modules"][0]["module"], ":composeApp");
        assert_eq!(
            mp["modules"][0]["targets"],
            json!([
                "androidTarget",
                "iosX64",
                "iosArm64",
                "iosSimulatorArm64",
                "jvm"
            ])
        );
        assert_eq!(mp["modules"][1]["targets"], json!(["jvm"]));
        assert_eq!(mp["ios"]["targets"].as_array().unwrap().len(), 3);
        assert!(
            mp["ios"]["xcode_project"]
                .as_str()
                .unwrap()
                .ends_with("iosApp/iosApp.xcodeproj")
        );
        assert_eq!(mp["ios"]["builds_on_linux"], false);
        let app = &read(root)["app_modules"][0];
        assert_eq!(app["module"], ":composeApp");
        assert_eq!(app["application_id"], "dev.kmp.app");
        assert_eq!(app["min_sdk"], 24);
    }

    #[test]
    fn an_android_only_build_is_not_multiplatform() {
        let root = tempfile::tempdir().unwrap();
        write(
            root.path(),
            "build.gradle.kts",
            "plugins { id(\"com.android.application\"); kotlin(\"android\") }\n",
        );
        assert!(multiplatform(root.path()).is_none());
    }
}
