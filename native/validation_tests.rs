//! Property tests of argument validation. For every operation's input schema a
//! small generator (no extra dependency) builds values that satisfy it, values
//! that break exactly one rule of it, and arbitrary JSON. Validation must never
//! panic, must accept the first, and must answer the second with an
//! `invalid_argument` error whose envelope names a remedy.
//!
//! `TOMACCO_FUZZ_SEED` fixes the seed (the failing one is in every message) and
//! `TOMACCO_FUZZ_ROUNDS` sets how many values are built per operation (default
//! 40; `TOMACCO_FUZZ_ROUNDS=2000 cargo test --locked validation_properties`).
use super::*;

struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        // xorshift64*
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545F4914F6CDD1D)
    }
    fn below(&mut self, n: u64) -> u64 {
        if n == 0 { 0 } else { self.next() % n }
    }
    fn chance(&mut self, percent: u64) -> bool {
        self.below(100) < percent
    }
    fn pick<'a, T>(&mut self, items: &'a [T]) -> &'a T {
        &items[self.below(items.len() as u64) as usize]
    }
}

const WORDS: &[&str] = &[
    "a",
    "abc",
    "x1",
    "A9",
    "build",
    "ios",
    "my-app_2",
    "0x1f",
    "0x00ff00ff",
    "en",
    "en-US",
    "a.b-c",
    "node-1",
    "dead",
    "emulator-5554",
    "/tmp",
    "hello world",
    "Abc 1.2-3",
    "a1b2c3d4e5f60718293a4b5c6d7e8f90",
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "",
];

fn matches_pattern(text: &str, pattern: &str) -> bool {
    regex::Regex::new(&format!("^(?:{pattern})$"))
        .unwrap()
        .is_match(text)
}

fn length_bounds(schema: &Value) -> (usize, usize) {
    (
        schema["minLength"].as_u64().unwrap_or(0) as usize,
        schema["maxLength"].as_u64().unwrap_or(100000) as usize,
    )
}

fn string_for(schema: &Value, rng: &mut Rng) -> Option<Value> {
    let (low, high) = length_bounds(schema);
    let fits = |text: &str| {
        let length = text.chars().count();
        length >= low
            && length <= high
            && schema["pattern"]
                .as_str()
                .is_none_or(|pattern| matches_pattern(text, pattern))
    };
    let mut candidates: Vec<String> = WORDS.iter().map(|word| word.to_string()).collect();
    candidates.push("a".repeat(low.max(1).min(high)));
    candidates.push("é".repeat(low.max(1).min(high.min(8))));
    candidates.push(format!("{:032x}", rng.next()));
    candidates.retain(|text| fits(text));
    (!candidates.is_empty()).then(|| json!(rng.pick(&candidates)))
}

fn number_for(schema: &Value, rng: &mut Rng, integer: bool) -> Value {
    let low = schema["minimum"].as_f64().unwrap_or(-1000.0).max(-1e9);
    let high = schema["maximum"]
        .as_f64()
        .unwrap_or(1000.0)
        .min(1e9)
        .max(low);
    let (low_whole, high_whole) = (low.ceil() as i64, high.floor() as i64);
    if low_whole <= high_whole && (integer || rng.chance(50)) {
        return json!(match rng.below(4) {
            0 => low_whole,
            1 => high_whole,
            _ => low_whole + rng.below((high_whole - low_whole + 1) as u64) as i64,
        });
    }
    json!((low + (high - low) * (rng.below(1001) as f64 / 1000.0)).clamp(low, high))
}

/// A value satisfying the schema, or None when the generator cannot build one.
fn valid(schema: &Value, rng: &mut Rng, depth: u32) -> Option<Value> {
    if let Some(choices) = schema["enum"].as_array() {
        return Some(rng.pick(choices).clone());
    }
    match schema["type"].as_str().unwrap_or("") {
        "object" => {
            let mut object = serde_json::Map::new();
            let required: Vec<&str> = schema["required"]
                .as_array()
                .map(|r| r.iter().filter_map(Value::as_str).collect())
                .unwrap_or_default();
            let minimum = schema["minProperties"].as_u64().unwrap_or(0) as usize;
            let properties = schema["properties"].as_object();
            for (key, spec) in properties.into_iter().flatten() {
                let wanted = required.contains(&key.as_str())
                    || object.len() < minimum
                    || (depth < 4 && rng.chance(50));
                if wanted {
                    match valid(spec, rng, depth + 1) {
                        Some(value) => {
                            object.insert(key.clone(), value);
                        }
                        None if required.contains(&key.as_str()) => return None,
                        None => (),
                    }
                }
            }
            if schema["additionalProperties"].is_object() {
                for _ in 0..rng.below(3) {
                    if let Some(value) = valid(&schema["additionalProperties"], rng, depth + 1) {
                        object.insert(format!("k{}", rng.below(5)), value);
                    }
                }
            }
            (object.len() >= minimum).then(|| Value::Object(object))
        }
        "array" => {
            let low = schema["minItems"].as_u64().unwrap_or(0);
            let high = schema["maxItems"].as_u64().unwrap_or(10000).min(low + 3);
            let length = low + rng.below(high - low + 1);
            let mut items: Vec<Value> = Vec::new();
            let mut attempts = 0;
            while (items.len() as u64) < length && attempts < 50 {
                attempts += 1;
                let item = valid(&schema["items"], rng, depth + 1)?;
                if schema["uniqueItems"] == true && items.contains(&item) {
                    continue;
                }
                items.push(item);
            }
            ((items.len() as u64) >= low).then(|| Value::Array(items))
        }
        "string" => string_for(schema, rng),
        "boolean" => Some(json!(rng.chance(50))),
        "integer" => Some(number_for(schema, rng, true)),
        "number" => Some(number_for(schema, rng, false)),
        "" => Some(json!(rng.below(100))),
        _ => None,
    }
}

fn wrong_type(schema: &Value, rng: &mut Rng) -> Value {
    let candidates = [
        ("object", json!({})),
        ("array", json!([])),
        ("string", json!("text")),
        ("boolean", json!(true)),
        ("integer", json!(7)),
        ("number", json!(1.5)),
        ("null", Value::Null),
    ];
    let kind = schema["type"].as_str().unwrap_or("");
    let options: Vec<&Value> = candidates
        .iter()
        .filter(|(name, value)| *name != kind && !(kind == "number" && value.is_number()))
        .map(|(_, value)| value)
        .collect();
    (*rng.pick(&options)).clone()
}

/// Breaks one rule of the schema somewhere in `value`. False when the value
/// has no place where a rule could be broken.
fn corrupt(value: &mut Value, schema: &Value, rng: &mut Rng) -> bool {
    let mut options: Vec<&str> = vec!["type"];
    if schema["enum"].is_array() {
        options.push("enum");
    }
    match schema["type"].as_str().unwrap_or("") {
        "object" => {
            if schema["additionalProperties"] == false {
                options.push("unknown");
            }
            if schema["required"].as_array().is_some_and(|r| !r.is_empty()) {
                options.push("missing");
            }
            if value.as_object().is_some_and(|o| !o.is_empty()) {
                options.push("descend");
                options.push("descend");
            }
            if schema["maxProperties"].is_u64() {
                options.push("many");
            }
        }
        "array" => {
            if schema["maxItems"].is_u64() {
                options.push("long");
            }
            if schema["minItems"].as_u64().unwrap_or(0) > 0 {
                options.push("short");
            }
            if schema["uniqueItems"] == true && value.as_array().is_some_and(|a| !a.is_empty()) {
                options.push("duplicate");
            }
            if value.as_array().is_some_and(|a| !a.is_empty()) {
                options.push("descend");
            }
        }
        "string" => {
            options.push("nul");
            if schema["maxLength"].is_u64() {
                options.push("long");
            }
            if schema["minLength"].as_u64().unwrap_or(0) > 0 {
                options.push("short");
            }
            if schema["pattern"].is_string() {
                options.push("pattern");
            }
        }
        "integer" | "number" => {
            if schema["minimum"].is_number() {
                options.push("below");
            }
            if schema["maximum"].is_number() {
                options.push("above");
            }
            if schema["type"] == "integer" {
                options.push("fraction");
            }
        }
        _ => (),
    }
    let choice = *rng.pick(&options);
    if schema["type"].as_str().unwrap_or("").is_empty() && choice == "type" {
        return false;
    }
    match choice {
        "type" => *value = wrong_type(schema, rng),
        "enum" => *value = json!("\u{1}not-a-choice"),
        "unknown" => {
            value
                .as_object_mut()
                .unwrap()
                .insert("zz_unknown".into(), json!(1));
        }
        "missing" => {
            let required = schema["required"].as_array().unwrap();
            let key = rng.pick(required).as_str().unwrap().to_string();
            if value.as_object_mut().unwrap().remove(&key).is_none() {
                return false;
            }
        }
        "many" => {
            let limit = schema["maxProperties"].as_u64().unwrap();
            for index in 0..=limit {
                value
                    .as_object_mut()
                    .unwrap()
                    .insert(format!("extra{index}"), json!(1));
            }
        }
        "descend" => {
            if let Some(object) = value.as_object_mut() {
                let keys: Vec<String> = object.keys().cloned().collect();
                let key = rng.pick(&keys).clone();
                let spec = if schema["properties"].get(&key).is_some() {
                    &schema["properties"][&key]
                } else if schema["additionalProperties"].is_object() {
                    &schema["additionalProperties"]
                } else {
                    return false;
                };
                return corrupt(object.get_mut(&key).unwrap(), spec, rng);
            }
            let items = value.as_array_mut().unwrap();
            let index = rng.below(items.len() as u64) as usize;
            return corrupt(&mut items[index], &schema["items"], rng);
        }
        "long" if schema["type"] == "array" => {
            let limit = schema["maxItems"].as_u64().unwrap();
            let items = value.as_array_mut().unwrap();
            let fill = items.first().cloned().unwrap_or(json!(0));
            if schema["uniqueItems"] == true {
                return false;
            }
            items.resize(limit as usize + 1, fill);
        }
        "short" if schema["type"] == "array" => value.as_array_mut().unwrap().clear(),
        "duplicate" => {
            let items = value.as_array_mut().unwrap();
            let first = items[0].clone();
            items.push(first);
            if let Some(limit) = schema["maxItems"].as_u64() {
                if items.len() as u64 > limit {
                    return false;
                }
            }
        }
        "nul" => *value = json!("a\0b"),
        "long" => {
            let limit = schema["maxLength"].as_u64().unwrap() as usize;
            *value = json!("a".repeat(limit + 1));
        }
        "short" => *value = json!(""),
        "pattern" => *value = json!("\u{1}\n not matching \u{7f}"),
        "below" => *value = json!(schema["minimum"].as_f64().unwrap() - 1.0),
        "above" => *value = json!(schema["maximum"].as_f64().unwrap() + 1.0),
        "fraction" => *value = json!(1.5),
        _ => return false,
    }
    true
}

fn arbitrary(rng: &mut Rng, depth: u32) -> Value {
    match rng.below(if depth > 3 { 6 } else { 8 }) {
        0 => Value::Null,
        1 => json!(rng.chance(50)),
        2 => json!(rng.next() as i64),
        3 => json!(rng.next() as f64 / 7.0),
        4 => json!(u64::MAX - rng.below(3)),
        5 => json!(*rng.pick(WORDS)),
        6 => Value::Array(
            (0..rng.below(4))
                .map(|_| arbitrary(rng, depth + 1))
                .collect(),
        ),
        _ => Value::Object(
            (0..rng.below(4))
                .map(|i| (format!("k{}", rng.below(4) + i), arbitrary(rng, depth + 1)))
                .collect(),
        ),
    }
}

fn mutate_names(rng: &mut Rng, names: &[&str]) -> String {
    let mut name = rng.pick(names).to_string();
    for _ in 0..rng.below(3) {
        let position = rng.below(name.len() as u64 + 1) as usize;
        match rng.below(3) {
            0 if position < name.len() => {
                name.remove(position);
            }
            1 => name.insert(position, 'q'),
            _ => name.truncate(position),
        }
    }
    name
}

fn seed() -> u64 {
    std::env::var("TOMACCO_FUZZ_SEED")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(0x70_6d_61_63_63_6f)
}

fn rounds() -> u64 {
    std::env::var("TOMACCO_FUZZ_ROUNDS")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(40)
}

fn answered_with_remedy(error: Error, context: &str) {
    assert_eq!(
        error.code, "invalid_argument",
        "{context}: {}",
        error.message
    );
    let envelope = envelope(Err(error));
    assert_eq!(envelope["ok"], false, "{context}");
    assert!(
        !s(&envelope["error"], "remedy").is_empty(),
        "{context}: no remedy in {envelope}"
    );
}

#[test]
fn valid_arguments_pass_and_each_broken_rule_is_a_typed_error_with_a_remedy() {
    let seed = seed();
    let mut built = 0;
    let mut broken = 0;
    for op in manifest().as_array().unwrap() {
        let name = s(op, "name");
        let schema = &op["inputSchema"];
        let mut rng = Rng(seed
            ^ name.bytes().fold(1469598103934665603u64, |h, b| {
                (h ^ b as u64).wrapping_mul(1099511628211)
            }));
        for round in 0..rounds() {
            let context = format!("{name} seed {seed} round {round}");
            let Some(value) = valid(schema, &mut rng, 0) else {
                continue;
            };
            built += 1;
            if let Err(error) = arguments(name, &value) {
                panic!("{context}: valid {value} was refused: {}", error.message);
            }
            let mut bad = value.clone();
            if corrupt(&mut bad, schema, &mut rng) && bad != value {
                broken += 1;
                match arguments(name, &bad) {
                    Ok(_) => panic!("{context}: invalid {bad} was accepted (from {value})"),
                    Err(error) => answered_with_remedy(error, &format!("{context}: {bad}")),
                }
            }
        }
    }
    assert!(
        built >= 236 * 20 / 2 && broken > built / 2,
        "{built} {broken}"
    );
}

#[test]
fn arbitrary_json_never_panics_validation() {
    let seed = seed();
    let names: Vec<&str> = names().collect();
    let mut rng = Rng(seed);
    for round in 0..rounds() * 200 {
        let name = mutate_names(&mut rng, &names);
        let value = arbitrary(&mut rng, 0);
        let context = format!("{name:?} {value} seed {seed} round {round}");
        match arguments(&name, &value) {
            Ok(_) => {}
            Err(error) => {
                assert!(
                    ["operation_not_found", "invalid_argument"].contains(&error.code.as_str()),
                    "{context}: {}",
                    error.code
                );
                let envelope = envelope(Err(error));
                assert!(!s(&envelope["error"], "remedy").is_empty(), "{context}");
            }
        }
    }
}

#[test]
fn deep_and_wide_values_are_refused_without_overflowing_the_stack() {
    // Arguments arrive through serde_json, which stops at 128 levels.
    let too_deep = format!("{}1{}", "[".repeat(200), "]".repeat(200));
    assert!(serde_json::from_str::<Value>(&too_deep).is_err());
    let deepest = format!("{}1{}", "[".repeat(120), "]".repeat(120));
    let nested: Value = serde_json::from_str(&deepest).unwrap();
    let error = arguments("job.get", &json!({"job_id": nested})).unwrap_err();
    assert_eq!(error.code, "invalid_argument");
    let wide: serde_json::Map<String, Value> =
        (0..20000).map(|i| (format!("k{i}"), json!(i))).collect();
    let error = arguments("doctor", &Value::Object(wide)).unwrap_err();
    assert_eq!(error.code, "invalid_argument");
}

#[test]
fn an_array_that_must_have_unique_items_refuses_a_repeated_one() {
    let error = arguments("job.list", &json!({"state": ["queued", "queued"]})).unwrap_err();
    assert_eq!(error.code, "invalid_argument");
    assert!(
        error
            .message
            .contains("arguments.state[1] repeats arguments.state[0]")
    );
    assert!(arguments("job.list", &json!({"state": ["queued", "failed"]})).is_ok());
}
