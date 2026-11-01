//! The tool list the MCP server offers, in one of three sizes. `full` is
//! every operation with its whole schema. `compact` keeps every operation
//! but shortens each description to its first sentence and drops argument
//! descriptions. `grouped` offers one tool per area, such as `android`,
//! which takes an operation's name and its arguments. `TOMACCO_MCP_TOOLS`
//! picks one; `full` is the default.
use crate::{api, core::*};
use serde_json::{Map, Value, json};
use std::sync::OnceLock;

/// The environment variable that picks the tool list.
pub const VARIABLE: &str = "TOMACCO_MCP_TOOLS";

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Mode {
    Full,
    Compact,
    Grouped,
}
impl Mode {
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "" | "full" => Some(Mode::Full),
            "compact" => Some(Mode::Compact),
            "grouped" => Some(Mode::Grouped),
            _ => None,
        }
    }
    /// The mode `TOMACCO_MCP_TOOLS` names; an unknown value is the full list,
    /// and the server says so on standard error when it starts.
    pub fn from_env() -> Self {
        Mode::parse(&std::env::var(VARIABLE).unwrap_or_default()).unwrap_or(Mode::Full)
    }
}

/// The tools for tools/list in this mode.
pub fn list(mode: Mode) -> &'static Value {
    static COMPACT: OnceLock<Value> = OnceLock::new();
    static GROUPED: OnceLock<Value> = OnceLock::new();
    match mode {
        Mode::Full => api::manifest(),
        Mode::Compact => COMPACT.get_or_init(|| {
            let mut tools: Vec<Value> = api::manifest()
                .as_array()
                .unwrap()
                .iter()
                .map(compact)
                .collect();
            tools.push(schema_tool());
            json!(tools)
        }),
        Mode::Grouped => GROUPED.get_or_init(grouped),
    }
}

/// The tool that answers one operation's whole schema, for the shorter lists.
pub const SCHEMA_TOOL: &str = "schema";
fn schema_tool() -> Value {
    json!({
        "name": SCHEMA_TOOL,
        "title": "Operation schema",
        "description": "The whole schema of one operation: its description, every argument with its description, and its annotations. Read it before calling an operation whose arguments you do not know.",
        "inputSchema": {"type":"object","properties":{"operation":{"type":"string","minLength":1}},"required":["operation"],"additionalProperties":false},
        "annotations": {"readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false},
    })
}

/// The operation and arguments a tools/call means: the tool's own for an
/// operation, the ones it names for a group tool, and `schema` for the
/// schema tool. A name that is neither is left to the dispatcher to refuse.
pub fn resolve(name: &str, arguments: &Value) -> Result<(String, Value)> {
    if name == SCHEMA_TOOL {
        return Ok((SCHEMA_TOOL.into(), arguments.clone()));
    }
    if api::operation(name).is_ok() || !areas().iter().any(|(area, _)| *area == name) {
        return Ok((name.into(), arguments.clone()));
    }
    let keys: Vec<&String> = arguments
        .as_object()
        .map(|o| o.keys().collect())
        .unwrap_or_default();
    if let Some(key) = keys
        .iter()
        .find(|k| !["operation", "arguments"].contains(&k.as_str()))
    {
        return Err(Error::new(
            "invalid_argument",
            format!("Unknown argument: arguments.{key}"),
        )
        .remedy(format!(
            "{name} takes operation and arguments, the operation's own arguments"
        )));
    }
    let operation = s(arguments, "operation");
    if area(operation) != name || api::operation(operation).is_err() {
        let names: Vec<&str> = api::names().filter(|n| area(n) == name).collect();
        return Err(Error::new(
            "invalid_argument",
            format!("{name} has no operation {operation:?}"),
        )
        .remedy(format!("operation is one of {}", names.join(", "))));
    }
    let inner = arguments.get("arguments").cloned().unwrap_or(json!({}));
    if !inner.is_object() {
        return Err(Error::new(
            "invalid_argument",
            "arguments.arguments must be an object",
        ));
    }
    Ok((operation.into(), inner))
}

/// The schema tool's answer.
pub fn schema(arguments: &Value) -> Result {
    api::operation(s(arguments, "operation")).cloned()
}

/// The part of an operation's name before its first dot, or `general`.
pub fn area(name: &str) -> &str {
    match name.split_once('.') {
        Some((area, _)) => area,
        None => "general",
    }
}
/// Each area in schema order with its operations.
fn areas() -> &'static [(&'static str, Vec<&'static Value>)] {
    static AREAS: OnceLock<Vec<(&'static str, Vec<&'static Value>)>> = OnceLock::new();
    AREAS.get_or_init(|| {
        let mut areas: Vec<(&str, Vec<&Value>)> = Vec::new();
        for op in api::manifest().as_array().unwrap() {
            let name = area(s(op, "name"));
            match areas.iter_mut().find(|(area, _)| *area == name) {
                Some((_, ops)) => ops.push(op),
                None => areas.push((name, vec![op])),
            }
        }
        areas
    })
}

/// One operation with its first sentence for a description and no
/// descriptions of its arguments.
fn compact(op: &Value) -> Value {
    let mut op = op.clone();
    op["description"] = json!(first_sentence(s(&op, "description")));
    strip_descriptions(&mut op["inputSchema"]);
    if let Some(output) = op.get_mut("outputSchema") {
        strip_descriptions(output);
    }
    // MCP reads a missing hint as its default, so only the others are stated.
    if let Some(hints) = op["annotations"].as_object_mut() {
        hints.retain(|key, value| {
            let default = matches!(key.as_str(), "destructiveHint" | "openWorldHint");
            *value != json!(default)
        });
    }
    op
}
fn strip_descriptions(schema: &mut Value) {
    let Some(object) = schema.as_object_mut() else {
        return;
    };
    object.remove("description");
    if object.get("required").is_some_and(|r| r == &json!([])) {
        object.remove("required");
    }
    if let Some(Value::Object(properties)) = object.get_mut("properties") {
        properties.values_mut().for_each(strip_descriptions);
    }
    for key in ["items", "additionalProperties"] {
        if let Some(inner) = object.get_mut(key) {
            strip_descriptions(inner);
        }
    }
    for key in ["anyOf", "oneOf", "allOf"] {
        if let Some(Value::Array(options)) = object.get_mut(key) {
            options.iter_mut().for_each(strip_descriptions);
        }
    }
}
/// The text up to the end of its first sentence: a full stop followed by a
/// space and a capital letter, or the end. At most 200 characters, cut at a word.
pub fn first_sentence(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    let mut end = chars.len();
    for i in 0..chars.len() {
        if chars[i] == '.'
            && chars.get(i + 1) == Some(&' ')
            && chars.get(i + 2).is_some_and(|c| c.is_uppercase())
        {
            end = i + 1;
            break;
        }
    }
    let sentence: String = chars[..end].iter().collect();
    if sentence.chars().count() <= 200 {
        return sentence;
    }
    let cut: String = sentence.chars().take(200).collect();
    match cut.rfind(' ') {
        Some(space) => format!("{}…", cut[..space].trim_end_matches([',', ';', ':'])),
        None => cut,
    }
}

/// One tool per area. Its annotations are the strictest of its operations':
/// read-only and idempotent only when all are, destructive and open-world
/// when any is. Its description lists each operation with its title and
/// what it changes.
fn grouped() -> Value {
    let mut tools: Vec<Value> = areas()
        .iter()
        .map(|(area, ops)| {
            let hint = |op: &Value, key: &str| op["annotations"][key] == true;
            let all = |key: &str| ops.iter().all(|op| hint(op, key));
            let any = |key: &str| ops.iter().any(|op| hint(op, key));
            let listed: Vec<String> = ops
                .iter()
                .map(|op| {
                    let effect = if hint(op, "readOnlyHint") {
                        "read-only"
                    } else if hint(op, "destructiveHint") {
                        "destructive"
                    } else {
                        "writes"
                    };
                    format!("{} ({}; {effect})", s(op, "name"), s(op, "title"))
                })
                .collect();
            let names: Vec<&str> = ops.iter().map(|op| s(op, "name")).collect();
            let mut annotations = Map::new();
            annotations.insert("readOnlyHint".into(), json!(all("readOnlyHint")));
            annotations.insert("destructiveHint".into(), json!(any("destructiveHint")));
            annotations.insert("idempotentHint".into(), json!(all("idempotentHint")));
            annotations.insert("openWorldHint".into(), json!(any("openWorldHint")));
            json!({
                "name": area,
                "title": format!("{area} operations"),
                "description": format!(
                    "Run one {area} operation: its name as operation, its own arguments as arguments. The schema tool gives an operation's arguments. Ask the person before a destructive one. Operations: {}.",
                    listed.join(", ")
                ),
                "inputSchema": {"type":"object","properties":{
                    "operation":{"type":"string","enum":names},
                    "arguments":{"type":"object","default":{}},
                },"required":["operation"],"additionalProperties":false},
                "annotations": annotations,
            })
        })
        .collect();
    tools.push(schema_tool());
    json!(tools)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn size(mode: Mode) -> usize {
        list(mode).to_string().len()
    }
    #[test]
    fn modes_are_named_and_an_unknown_one_is_refused() {
        assert_eq!(Mode::parse(""), Some(Mode::Full));
        assert_eq!(Mode::parse("full"), Some(Mode::Full));
        assert_eq!(Mode::parse("compact"), Some(Mode::Compact));
        assert_eq!(Mode::parse("grouped"), Some(Mode::Grouped));
        assert_eq!(Mode::parse("tiny"), None);
    }
    /// Whether a schema still has a description anywhere; a property that
    /// is itself named description, as testing.find's selector has, is not one.
    fn prose(schema: &Value) -> bool {
        match schema {
            Value::Object(object) => object.iter().any(|(key, value)| {
                (key == "description" && value.is_string())
                    || if key == "properties" {
                        value.as_object().is_some_and(|p| p.values().any(prose))
                    } else {
                        prose(value)
                    }
            }),
            Value::Array(items) => items.iter().any(prose),
            _ => false,
        }
    }
    #[test]
    fn compact_keeps_every_operation_and_its_schema_without_prose() {
        let full = list(Mode::Full).as_array().unwrap();
        let compact = list(Mode::Compact).as_array().unwrap();
        assert_eq!(compact.len(), full.len() + 1);
        assert_eq!(s(compact.last().unwrap(), "name"), SCHEMA_TOOL);
        for (short, whole) in compact.iter().zip(full) {
            assert_eq!(short["name"], whole["name"]);
            // A hint left out is the MCP default: what the full list states.
            for (key, default) in [
                ("readOnlyHint", false),
                ("destructiveHint", true),
                ("idempotentHint", false),
                ("openWorldHint", true),
            ] {
                let hint = short["annotations"]
                    .get(key)
                    .cloned()
                    .unwrap_or(json!(default));
                assert_eq!(hint, whole["annotations"][key], "{} {key}", whole["name"]);
            }
            assert!(
                s(whole, "description").starts_with(s(short, "description").trim_end_matches('…'))
            );
            assert!(!prose(&short["inputSchema"]), "{}", whole["name"]);
            let required = |op: &Value| {
                op["inputSchema"]
                    .get("required")
                    .cloned()
                    .unwrap_or(json!([]))
            };
            assert_eq!(required(short), required(whole), "{}", whole["name"]);
        }
        assert!(size(Mode::Compact) * 10 < size(Mode::Full) * 6);
    }
    #[test]
    fn first_sentences_end_at_a_full_stop_before_a_capital() {
        assert_eq!(
            first_sentence("Wait up to 60 s. Running is valid."),
            "Wait up to 60 s."
        );
        assert_eq!(
            first_sentence("Reads e.g. files. Then"),
            "Reads e.g. files."
        );
        assert_eq!(first_sentence("No full stop"), "No full stop");
        let long = "word ".repeat(60);
        let cut = first_sentence(&long);
        assert!(cut.ends_with('…') && cut.chars().count() <= 201);
    }
    #[test]
    fn grouped_has_one_tool_per_area_with_its_strictest_annotations() {
        let tools = list(Mode::Grouped).as_array().unwrap();
        let named = |name: &str| tools.iter().find(|t| t["name"] == name).unwrap();
        let operations: usize = tools
            .iter()
            .filter(|t| t["name"] != SCHEMA_TOOL)
            .map(|t| {
                t["inputSchema"]["properties"]["operation"]["enum"]
                    .as_array()
                    .unwrap()
                    .len()
            })
            .sum();
        assert_eq!(operations, api::names().count());
        assert_eq!(named("job")["annotations"]["destructiveHint"], true);
        assert_eq!(named("general")["annotations"]["readOnlyHint"], true);
        assert!(s(named("job"), "description").contains("job.cancel (Cancel"));
        assert!(s(named("job"), "description").contains("; destructive)"));
        assert!(tools.len() <= 30);
        // The agent guide names how many tools the grouped list has.
        let agents = include_str!("../docs/agents.md");
        assert!(agents.contains(&format!("`job`: {} tools.", tools.len() - 1)));
        assert!(size(Mode::Grouped) * 5 < size(Mode::Full));
    }
    #[test]
    fn a_group_call_resolves_to_its_operation() {
        let call = |name: &str, arguments: Value| resolve(name, &arguments);
        assert_eq!(
            call(
                "job",
                json!({"operation":"job.wait","arguments":{"job_id":"x"}})
            )
            .unwrap(),
            ("job.wait".to_string(), json!({"job_id":"x"}))
        );
        assert_eq!(
            call("general", json!({"operation":"doctor"})).unwrap(),
            ("doctor".to_string(), json!({}))
        );
        // An operation's own tool still works in every mode.
        assert_eq!(
            call("job.wait", json!({"job_id":"x"})).unwrap(),
            ("job.wait".to_string(), json!({"job_id":"x"}))
        );
        let wrong = call("job", json!({"operation":"device.list"})).unwrap_err();
        assert_eq!(wrong.code, "invalid_argument");
        assert!(wrong.remedy.contains("job.wait"));
        assert!(call("job", json!({"operation":"job.wait","job_id":"x"})).is_err());
        assert!(call("job", json!({"operation":"job.wait","arguments":[]})).is_err());
        assert_eq!(
            schema(&json!({"operation":"job.wait"})).unwrap()["name"],
            "job.wait"
        );
        assert!(schema(&json!({"operation":"job.wiat"})).is_err());
    }
}
