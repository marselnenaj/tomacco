use crate::core::*;
use serde_json::{Value, json};
use std::collections::HashSet;

pub fn builtin() -> Value {
    serde_json::from_str(include_str!("../assets/models.json"))
        .expect("validated built-in catalogue")
}
pub fn check(profile: &Value) -> Result<()> {
    let schema: Value = serde_json::from_str(include_str!("../assets/device-profile.schema.json"))?;
    validate(profile, &schema, "profile")?;
    if profile["colors"].as_array().map(Vec::len) != profile["colorNames"].as_array().map(Vec::len)
    {
        return Err(Error::new(
            "invalid_profile",
            "colors and colorNames must have equal length",
        ));
    }
    if profile.get("openWidth").is_some() != profile.get("openHeight").is_some() {
        return Err(Error::new(
            "invalid_profile",
            "Foldables require both openWidth and openHeight",
        ));
    }
    let source_pattern = regex::Regex::new(r"^https?://[^\s]+$").unwrap();
    for key in ["source", "designSource"] {
        let value = s(profile, key);
        if !value.is_empty() && !source_pattern.is_match(value) {
            return Err(Error::new(
                "invalid_profile",
                format!("{key} must be an HTTP(S) URL"),
            ));
        }
    }
    for key in [
        "safePortrait",
        "safeLandscape",
        "openSafePortrait",
        "openSafeLandscape",
    ] {
        if profile.get(key).is_none() {
            continue;
        }
        let (mut w, mut h) = (
            profile["width"].as_f64().unwrap(),
            profile["height"].as_f64().unwrap(),
        );
        if key.starts_with("open") {
            w = profile["openWidth"].as_f64().unwrap_or(w);
            h = profile["openHeight"].as_f64().unwrap_or(h);
        }
        let (w, h) = if key.contains("Landscape") {
            (w.max(h), w.min(h))
        } else {
            (w.min(h), w.max(h))
        };
        let area = &profile[key];
        if area["left"].as_f64().unwrap() + area["right"].as_f64().unwrap() >= w
            || area["top"].as_f64().unwrap() + area["bottom"].as_f64().unwrap() >= h
        {
            return Err(Error::new(
                "invalid_profile",
                format!("{key} leaves no usable viewport"),
            ));
        }
    }
    Ok(())
}
pub fn profiles(platform: &str) -> Result {
    let mut result = Vec::new();
    for (data, custom) in [
        (builtin(), false),
        (
            read_json(&home()?.join("profiles.json"), Some(json!([])))?,
            true,
        ),
    ] {
        for mut p in data
            .as_array()
            .ok_or_else(|| Error::new("invalid_profile", "Catalogue must be an array"))?
            .clone()
        {
            if !p.is_object() {
                return Err(Error::new(
                    "invalid_profile",
                    "Each catalogue profile must be an object",
                ));
            }
            if platform.is_empty() || s(&p, "platform") == platform {
                p["custom"] = json!(custom);
                result.push(p);
            }
        }
    }
    Ok(json!(result))
}
pub fn find(id: &str) -> Result {
    profiles("")?
        .as_array()
        .unwrap()
        .iter()
        .find(|p| s(p, "id") == id)
        .cloned()
        .ok_or_else(|| Error::new("profile_not_found", format!("Unknown device profile: {id}")))
}
pub fn import(a: &Value) -> Result {
    let source = read_json(&path(s(a, "path"), false, true)?, None)?;
    let items = if source.is_array() {
        source.as_array().unwrap().clone()
    } else {
        vec![source]
    };
    if items.is_empty() || items.len() > 100 {
        return Err(Error::new(
            "invalid_profile",
            "Import between 1 and 100 profiles at a time",
        ));
    }
    let mut ids = HashSet::new();
    for p in &items {
        check(p)?;
        if !ids.insert(s(p, "id")) {
            return Err(Error::new(
                "duplicate_profile",
                "Import contains duplicate IDs",
            ));
        }
    }
    if builtin()
        .as_array()
        .unwrap()
        .iter()
        .any(|p| ids.contains(s(p, "id")))
    {
        return Err(Error::new(
            "builtin_profile",
            "Choose a new ID; built-in profiles cannot be replaced",
        ));
    }
    let lock = Lock::open(&home()?.join("profiles.lock"))?;
    lock.acquire(false)?;
    let mut existing = read_json(&home()?.join("profiles.json"), Some(json!([])))?
        .as_array()
        .ok_or_else(|| Error::new("invalid_profile", "Custom catalogue must be an array"))?
        .clone();
    if !b(a, "replace") && existing.iter().any(|p| ids.contains(s(p, "id"))) {
        return Err(Error::new(
            "duplicate_profile",
            "Profile exists; use replace=true to update it",
        ));
    }
    existing.retain(|p| !ids.contains(s(p, "id")));
    if existing.len() + items.len() > 1000 {
        return Err(Error::new(
            "catalogue_full",
            "At most 1000 custom profiles are supported",
        ));
    }
    let imported: Vec<_> = items.iter().map(|p| p["id"].clone()).collect();
    existing.extend(items);
    atomic_json(&home()?.join("profiles.json"), &json!(existing))?;
    Ok(json!({"imported":imported}))
}
pub fn export(a: &Value) -> Result {
    let mut value = find(s(a, "profile_id"))?;
    value.as_object_mut().unwrap().remove("custom");
    let p = path(s(a, "path"), false, false)?;
    if p.exists() {
        return Err(Error::new(
            "file_exists",
            format!("Export destination already exists: {}", p.display()),
        ));
    }
    atomic_json(&p, &value)?;
    Ok(json!({"path":p,"profile":value}))
}
pub fn remove(id: &str) -> Result {
    if !b(&find(id)?, "custom") {
        return Err(Error::new(
            "builtin_profile",
            "Built-in profiles cannot be removed",
        ));
    }
    let lock = Lock::open(&home()?.join("profiles.lock"))?;
    lock.acquire(false)?;
    let mut values = read_json(&home()?.join("profiles.json"), Some(json!([])))?;
    values
        .as_array_mut()
        .ok_or_else(|| Error::new("invalid_profile", "Custom catalogue must be an array"))?
        .retain(|p| s(p, "id") != id);
    atomic_json(&home()?.join("profiles.json"), &values)?;
    Ok(json!({"removed":id}))
}
