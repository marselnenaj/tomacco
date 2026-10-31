//! `tomaccoctl help OPERATION` and `call OPERATION --help`: one operation
//! described for a person, from the same schema `tomaccoctl schema` prints.
use crate::{api, core::*};
use serde_json::{Map, Value};

const WIDTH: usize = 79;

/// The text of one operation: title, description, arguments with their
/// types, limits and defaults, what it changes, and one example call.
pub fn operation(name: &str) -> Result<String> {
    let op = api::operation(name)?;
    let schema = &op["inputSchema"];
    let mut out = format!("{name} - {}\n\n", s(op, "title"));
    out += &wrap(s(op, "description"), 2);
    let required: Vec<&str> = schema["required"]
        .as_array()
        .map(|keys| keys.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default();
    match schema["properties"].as_object() {
        Some(properties) if !properties.is_empty() => {
            out += "\nArguments (one JSON object):\n";
            for (key, spec) in properties {
                out += &format!("  {key}\n");
                out += &wrap(&kind(spec, required.contains(&key.as_str())), 6);
                if !s(spec, "description").is_empty() {
                    out += &wrap(s(spec, "description"), 6);
                }
            }
        }
        _ => out += "\nTakes no arguments.\n",
    }
    let hints = &op["annotations"];
    out += "\n";
    out += &wrap(
        if b(hints, "readOnlyHint") {
            "Reads only; changes nothing."
        } else if b(hints, "destructiveHint") {
            "Changes state and may delete or overwrite data."
        } else {
            "Changes state."
        },
        0,
    );
    let example = example(schema);
    out += "\nExample:\n  tomaccoctl call ";
    out += name;
    if example.as_object().is_some_and(|o| !o.is_empty()) {
        out += &format!(" '{}'", example.to_string().replace('\'', r"'\''"));
    }
    out += "\n";
    if example.to_string().contains("\"<") {
        out += "  (replace the values in angle brackets)\n";
    }
    out += "\nThe full schema: tomaccoctl schema ";
    out += name;
    out += "\n";
    Ok(out)
}

/// One argument's type and constraints in a line: "integer, 0 to 60,
/// default 30, optional".
fn kind(spec: &Value, required: bool) -> String {
    let mut parts = vec![match s(spec, "type") {
        "array" => match s(&spec["items"], "type") {
            "" => "array".to_string(),
            item => format!("array of {item}"),
        },
        "" => "any".to_string(),
        other => other.to_string(),
    }];
    if let Some(choices) = spec["enum"].as_array() {
        let names: Vec<String> = choices.iter().map(plain).collect();
        parts.push(format!("one of: {}", names.join(", ")));
    }
    if let Some(choices) = spec["items"]["enum"].as_array() {
        let names: Vec<String> = choices.iter().map(plain).collect();
        parts.push(format!("items one of: {}", names.join(", ")));
    }
    match (spec.get("minimum"), spec.get("maximum")) {
        (Some(low), Some(high)) => parts.push(format!("{low} to {high}")),
        (Some(low), None) => parts.push(format!("at least {low}")),
        (None, Some(high)) => parts.push(format!("at most {high}")),
        (None, None) => (),
    }
    match (n(spec, "minLength"), spec["maxLength"].as_u64()) {
        (0, Some(high)) => parts.push(format!("up to {high} characters")),
        (1, Some(high)) => parts.push(format!("non-empty, up to {high} characters")),
        (1, None) => parts.push("non-empty".to_string()),
        (low, Some(high)) => parts.push(format!("{low} to {high} characters")),
        (low, None) if low > 1 => parts.push(format!("at least {low} characters")),
        _ => (),
    }
    match (n(spec, "minItems"), spec["maxItems"].as_u64()) {
        (0, Some(high)) => parts.push(format!("up to {high} items")),
        (low, Some(high)) => parts.push(format!("{low} to {high} items")),
        (low, None) if low > 0 => parts.push(format!("at least {low} items")),
        _ => (),
    }
    if let Some(pattern) = spec["pattern"].as_str() {
        parts.push(format!("matching {pattern}"));
    }
    if let Some(keys) = spec["properties"].as_object() {
        let keys: Vec<&str> = keys.keys().map(String::as_str).collect();
        parts.push(format!("keys: {}", keys.join(", ")));
    }
    if let Some(value) = spec.get("default") {
        parts.push(format!("default {value}"));
    }
    parts.push(if required { "required" } else { "optional" }.to_string());
    parts.join(", ")
}

/// A JSON value as a person reads it in a list: strings without quotes.
fn plain(value: &Value) -> String {
    value
        .as_str()
        .map(String::from)
        .unwrap_or_else(|| value.to_string())
}

/// Arguments that show the call: every required key with its default, its
/// first choice, its lower limit or a placeholder in angle brackets.
pub fn example(schema: &Value) -> Value {
    let mut out = Map::new();
    for key in schema["required"].as_array().into_iter().flatten() {
        let Some(key) = key.as_str() else { continue };
        out.insert(key.to_string(), value(key, &schema["properties"][key]));
    }
    // An object that needs some keys, whichever: the first ones.
    let needed = n(schema, "minProperties") as usize;
    for (key, spec) in schema["properties"].as_object().into_iter().flatten() {
        if out.len() >= needed {
            break;
        }
        if !out.contains_key(key) {
            out.insert(key.clone(), value(key, spec));
        }
    }
    // A map from names the caller chooses: one entry.
    if out.len() < needed && schema["additionalProperties"].is_object() {
        out.insert(
            "<name>".to_string(),
            value("name", &schema["additionalProperties"]),
        );
    }
    Value::Object(out)
}

fn value(key: &str, spec: &Value) -> Value {
    if let Some(value) = spec.get("default") {
        return value.clone();
    }
    if let Some(first) = spec["enum"].as_array().and_then(|choices| choices.first()) {
        return first.clone();
    }
    match s(spec, "type") {
        "integer" | "number" => spec.get("minimum").cloned().unwrap_or(Value::from(1)),
        "boolean" => Value::Bool(true),
        "array" => {
            let item = value(key.trim_end_matches('s'), &spec["items"]);
            Value::Array(vec![item; n(spec, "minItems").max(1) as usize])
        }
        "object" => example(spec),
        _ => Value::String(format!("<{key}>")),
    }
}

/// Words filled to the line width, each line indented.
fn wrap(text: &str, indent: usize) -> String {
    let mut out = String::new();
    let mut line = String::new();
    for word in text.split_whitespace() {
        if !line.is_empty() && indent + line.len() + 1 + word.len() > WIDTH {
            out += &format!("{}{line}\n", " ".repeat(indent));
            line.clear();
        }
        if !line.is_empty() {
            line.push(' ');
        }
        line += word;
    }
    if !line.is_empty() {
        out += &format!("{}{line}\n", " ".repeat(indent));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_operation_is_described_with_its_arguments_and_an_example() {
        let text = operation("job.wait").unwrap();
        assert!(text.starts_with("job.wait - Wait for job\n\n  Wait up to 60 seconds"));
        assert!(
            text.contains("  job_id\n      string, required\n"),
            "{text}"
        );
        assert!(
            text.contains("  seconds\n      integer, 0 to 60, default 30, optional\n"),
            "{text}"
        );
        assert!(text.contains("Reads only; changes nothing."));
        assert!(
            text.contains("Example:\n  tomaccoctl call job.wait '{\"job_id\":\"<job_id>\"}'\n")
        );
        assert!(text.contains("(replace the values in angle brackets)"));
        // Choices and items.
        let text = operation("testing.run").unwrap();
        assert!(
            text.contains("array of string, 1 to 32 items, required"),
            "{text}"
        );
        assert!(text.contains(r#""devices":["<device>"]"#), "{text}");
        let text = operation("doctor").unwrap();
        assert!(text.contains("Takes no arguments."));
        assert!(text.contains("Example:\n  tomaccoctl call doctor\n"));
        assert!(!text.contains("angle brackets"));
        let error = operation("job.wiat").unwrap_err();
        assert_eq!(error.code, "operation_not_found");
        assert!(error.remedy.starts_with("Did you mean job.wait?"));
    }

    #[test]
    fn every_operation_has_help_that_fits_a_terminal_and_a_valid_example() {
        let enums = api::names()
            .filter(|name| {
                let schema = &api::operation(name).unwrap()["inputSchema"];
                schema["properties"]
                    .as_object()
                    .unwrap()
                    .values()
                    .any(|p| p["enum"].is_array())
            })
            .count();
        assert!(enums > 10);
        for name in api::names() {
            let text = operation(name).unwrap();
            for line in text.lines() {
                // A long pattern or a long word may run over; prose wraps.
                assert!(
                    line.chars().count() <= WIDTH || !line.contains(". "),
                    "{name}: {line}"
                );
            }
            // The example is a valid call but for the placeholders, which
            // cannot match a pattern: types, required keys, choices, ranges.
            let schema = &api::operation(name).unwrap()["inputSchema"];
            validate(&example(schema), &without_patterns(schema), "arguments")
                .unwrap_or_else(|e| panic!("{name}: {}", e.message));
        }
    }

    fn without_patterns(schema: &Value) -> Value {
        match schema {
            Value::Object(map) => Value::Object(
                map.iter()
                    .filter(|(key, _)| *key != "pattern")
                    .map(|(key, value)| (key.clone(), without_patterns(value)))
                    .collect(),
            ),
            other => other.clone(),
        }
    }
}
