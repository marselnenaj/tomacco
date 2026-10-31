//! Validate the real guest's bounded IOKit protocol without a Python runtime.
use crate::core::*;
use serde::{
    Deserialize, Deserializer,
    de::{self, MapAccess, SeqAccess, Visitor},
};
use serde_json::{Value, json};
use std::{collections::BTreeSet, fmt};

const CLASSES: &[&str] = &[
    "IOPlatformExpertDevice",
    "IOMobileFramebuffer",
    "AppleCLCD2",
    "AppleParavirtDisplay",
    "IOSurfaceRoot",
    "IOGPU",
    "IOHIDEventService",
    "AppleDCPExpert",
    "RTBuddyService",
    "AppleSEPManager",
    "AppleKeyStore",
    "IOMedia",
];
const NODES: &[&str] = &[
    "IODeviceTree:/arm-io",
    "IODeviceTree:/arm-io/disp0",
    "IODeviceTree:/arm-io/dispext0",
    "IODeviceTree:/arm-io/dcp",
    "IODeviceTree:/arm-io/sep",
    "IODeviceTree:/arm-io/sgx",
];

// serde_json::Value normally overwrites duplicate fields. Evidence must not.
struct Unique(Value);
impl<'de> Deserialize<'de> for Unique {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        struct Json;
        impl<'de> Visitor<'de> for Json {
            type Value = Unique;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("JSON with unique object keys")
            }
            fn visit_map<M: MapAccess<'de>>(
                self,
                mut map: M,
            ) -> std::result::Result<Unique, M::Error> {
                let mut object = serde_json::Map::new();
                while let Some((key, value)) = map.next_entry::<String, Unique>()? {
                    if object.insert(key, value.0).is_some() {
                        return Err(de::Error::custom("Duplicate JSON key"));
                    }
                }
                Ok(Unique(Value::Object(object)))
            }
            fn visit_seq<S: SeqAccess<'de>>(
                self,
                mut seq: S,
            ) -> std::result::Result<Unique, S::Error> {
                let mut values = Vec::new();
                while let Some(value) = seq.next_element::<Unique>()? {
                    values.push(value.0);
                }
                Ok(Unique(json!(values)))
            }
            fn visit_bool<E: de::Error>(self, v: bool) -> std::result::Result<Unique, E> {
                Ok(Unique(json!(v)))
            }
            fn visit_i64<E: de::Error>(self, v: i64) -> std::result::Result<Unique, E> {
                Ok(Unique(json!(v)))
            }
            fn visit_u64<E: de::Error>(self, v: u64) -> std::result::Result<Unique, E> {
                Ok(Unique(json!(v)))
            }
            fn visit_f64<E: de::Error>(self, v: f64) -> std::result::Result<Unique, E> {
                Ok(Unique(json!(v)))
            }
            fn visit_str<E: de::Error>(self, v: &str) -> std::result::Result<Unique, E> {
                Ok(Unique(json!(v)))
            }
            fn visit_string<E: de::Error>(self, v: String) -> std::result::Result<Unique, E> {
                Ok(Unique(json!(v)))
            }
            fn visit_unit<E: de::Error>(self) -> std::result::Result<Unique, E> {
                Ok(Unique(Value::Null))
            }
        }
        deserializer.deserialize_any(Json)
    }
}
fn ensure(ok: bool, message: &str) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(Error::new("invalid_platform_observations", message))
    }
}
fn unsigned(v: &Value) -> bool {
    v.as_u64().is_some_and(|x| x <= u32::MAX as u64)
}
fn status(v: &Value) -> bool {
    v.as_i64().is_some_and(|x| i32::try_from(x).is_ok())
}
fn text(v: &Value, limit: usize) -> bool {
    v.as_str().is_some_and(|v| v.len() < limit)
}
fn hex(v: &Value, maximum: usize) -> bool {
    v.as_str().is_some_and(|v| {
        v.len() <= maximum * 2
            && v.len() % 2 == 0
            && v.bytes()
                .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
    })
}

pub fn parse(raw: &[u8]) -> Result {
    ensure(raw.len() <= 256 * 1024, "Platform log exceeds 256 KiB")?;
    let decoded = std::str::from_utf8(raw)
        .map_err(|_| Error::new("invalid_platform_observations", "Non-UTF-8 platform data"))?;
    let mut events = Vec::<Value>::new();
    let mut snapshots = Vec::<Value>::new();
    let mut pending = Value::Null;
    let mut rows = Vec::<Value>::new();
    let mut completed = false;
    let mut terminal = Value::Null;
    let mut ticks = Vec::<u64>::new();
    for line in decoded.lines().filter(|v| !v.trim().is_empty()) {
        let record = line.strip_prefix("TOMACCO_PLATFORM ");
        ensure(
            line.len() < 8192 && record.is_some(),
            "Unexpected platform record",
        )?;
        let event: Value = serde_json::from_str::<Unique>(record.unwrap_or_default())?.0;
        let name = s(&event, "event");
        let mut fields = match name {
            "begin" => vec!["format", "uid", "pid", "mode"],
            "snapshot" => vec!["tick", "busy", "busy_error"],
            "service" => vec![
                "tick",
                "query",
                "name",
                "class",
                "path",
                "registry_id",
                "busy",
                "errors",
            ],
            "class" => vec!["tick", "query", "result", "count", "truncated"],
            "node" => vec![
                "tick",
                "path",
                "found",
                "compatible_type",
                "compatible_hex",
                "property_error",
            ],
            "snapshot-end" => vec!["tick", "services"],
            "end" => vec!["snapshots"],
            "error" => vec!["stage"],
            _ => {
                return Err(Error::new(
                    "invalid_platform_observations",
                    "Unknown platform event",
                ));
            }
        };
        if name == "node" && events.first().is_some_and(|v| v["format"] == 2) {
            fields.extend(["class", "class_error", "service_path", "service_path_error"]);
        }
        fields.extend(["seq", "event"]);
        ensure(
            event.as_object().is_some_and(|v| {
                v.len() == fields.len() && fields.iter().all(|k| v.contains_key(*k))
            }),
            "Unexpected platform fields",
        )?;
        ensure(
            events.len() < 1024
                && unsigned(&event["seq"])
                && n(&event, "seq") == events.len() as u64
                && !completed
                && terminal.is_null(),
            "Out-of-order platform event",
        )?;
        ensure(
            !events.is_empty() || name == "begin",
            "Missing platform contract",
        )?;
        match name {
            "begin" => {
                ensure(
                    events.is_empty()
                        && [Some(1), Some(2)].contains(&event["format"].as_u64())
                        && unsigned(&event["uid"])
                        && event["pid"]
                            .as_u64()
                            .is_some_and(|x| (1..=i32::MAX as u64).contains(&x))
                        && ["stdio", "uart"].contains(&s(&event, "mode")),
                    "Invalid platform identity",
                )?;
                ticks = if event["mode"] == "stdio" {
                    vec![0]
                } else {
                    vec![30, 90]
                };
            }
            "snapshot" => {
                ensure(
                    pending.is_null()
                        && ticks.get(snapshots.len()).copied() == event["tick"].as_u64()
                        && unsigned(&event["busy"])
                        && status(&event["busy_error"]),
                    "Invalid snapshot order",
                )?;
                pending = json!({"tick":event["tick"],"busy":event["busy"],"busy_error":event["busy_error"],"classes":{},"nodes":{},"services":[]});
            }
            "service" | "class" | "node" | "snapshot-end" => {
                ensure(
                    pending.is_object()
                        && unsigned(&event["tick"])
                        && event["tick"] == pending["tick"],
                    "Record outside snapshot",
                )?;
                let class_count = pending["classes"].as_object().unwrap().len();
                let node_count = pending["nodes"].as_object().unwrap().len();
                if name == "service" || name == "class" {
                    ensure(
                        CLASSES.get(class_count).copied() == Some(s(&event, "query"))
                            && node_count == 0,
                        "Reordered service query",
                    )?;
                    if name == "service" {
                        let errors = &event["errors"];
                        ensure(
                            rows.len() < 32
                                && text(&event["name"], 128)
                                && text(&event["class"], 128)
                                && text(&event["path"], 512)
                                && s(&event, "registry_id").len() == 18
                                && s(&event, "registry_id").starts_with("0x")
                                && hex(&json!(&s(&event, "registry_id")[2..]), 8)
                                && unsigned(&event["busy"])
                                && errors.as_object().is_some_and(|v| {
                                    v.len() == 5
                                        && ["name", "class", "path", "id", "busy"]
                                            .iter()
                                            .all(|k| status(&errors[*k]))
                                }),
                            "Invalid service identity",
                        )?;
                        ensure(
                            errors["id"] != 0
                                || !rows.iter().any(|r| {
                                    r["errors"]["id"] == 0
                                        && r["registry_id"] == event["registry_id"]
                                }),
                            "Duplicate service",
                        )?;
                        rows.push(event.clone());
                    } else {
                        ensure(
                            status(&event["result"])
                                && event["count"].as_u64() == Some(rows.len() as u64)
                                && event["truncated"].is_boolean()
                                && (event["result"] == 0
                                    || (rows.is_empty() && event["truncated"] == false))
                                && (event["truncated"] == false || rows.len() == 32),
                            "Contradictory service count",
                        )?;
                        pending["classes"][s(&event, "query")] = event.clone();
                        pending["services"]
                            .as_array_mut()
                            .unwrap()
                            .append(&mut rows);
                    }
                } else if name == "node" {
                    ensure(
                        class_count == CLASSES.len()
                            && NODES.get(node_count).copied() == Some(s(&event, "path"))
                            && event["found"].is_boolean()
                            && ["absent", "data", "string", "unsupported"]
                                .contains(&s(&event, "compatible_type"))
                            && hex(&event["compatible_hex"], 256)
                            && [Some(0), Some(1)].contains(&event["property_error"].as_u64()),
                        "Invalid device-tree observation",
                    )?;
                    ensure(
                        (event["found"] == true
                            || (event["compatible_type"] == "absent"
                                && event["property_error"] == 0))
                            && (event["compatible_type"] != "absent"
                                || (event["compatible_hex"] == "" && event["property_error"] == 0))
                            && (event["compatible_type"] != "unsupported"
                                || event["property_error"] == 1)
                            && (event["property_error"] == 0 || event["compatible_hex"] == ""),
                        "Contradictory device-tree property",
                    )?;
                    if events[0]["format"] == 2 {
                        ensure(
                            text(&event["class"], 128)
                                && text(&event["service_path"], 512)
                                && status(&event["class_error"])
                                && status(&event["service_path_error"])
                                && (event["found"] == true
                                    || (event["class"] == ""
                                        && event["service_path"] == ""
                                        && event["class_error"] == -1
                                        && event["service_path_error"] == -1)),
                            "Invalid provider identity",
                        )?;
                    }
                    pending["nodes"][s(&event, "path")] = event.clone();
                } else {
                    ensure(
                        class_count == CLASSES.len()
                            && node_count == NODES.len()
                            && rows.is_empty()
                            && event["services"].as_u64()
                                == Some(pending["services"].as_array().unwrap().len() as u64),
                        "Incomplete snapshot",
                    )?;
                    snapshots.push(pending.take());
                }
            }
            "end" => {
                ensure(
                    pending.is_null()
                        && snapshots.len() == ticks.len()
                        && event["snapshots"].as_u64() == Some(ticks.len() as u64),
                    "Premature platform completion",
                )?;
                completed = true;
            }
            "error" => {
                let stage = s(&event, "stage");
                ensure(
                    !stage.is_empty()
                        && stage.len() <= 128
                        && stage
                            .bytes()
                            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-'),
                    "Invalid failure stage",
                )?;
                terminal = event["stage"].clone();
            }
            _ => unreachable!(),
        }
        events.push(event);
    }
    ensure(!events.is_empty(), "No platform observations")?;
    let missing: BTreeSet<_> = snapshots
        .iter()
        .flat_map(|v| v["classes"].as_object().unwrap().iter())
        .filter(|(_, v)| v["result"] == 0 && v["count"] == 0)
        .map(|(k, _)| k.clone())
        .collect();
    Ok(
        json!({"format":1,"source_sha256":digest(raw),"source_bytes":raw.len(),"identity":events[0],
        "events":events,"snapshots":snapshots,"completed":completed,"partial_snapshot":pending,
        "terminal_error":terminal,"missing_services":missing,"graphical_apps_qualified":false,
        "full_ios_boot_qualified":false,"main_display_qualified":false,"sep_functionality_qualified":false}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_duplicate_fields_and_invalid_identity() {
        for raw in [b"TOMACCO_PLATFORM {\"event\":\"begin\",\"seq\":0,\"seq\":0,\"format\":2,\"uid\":0,\"pid\":1,\"mode\":\"uart\"}".as_slice(),
            b"TOMACCO_PLATFORM {\"event\":\"begin\",\"seq\":0,\"format\":true,\"uid\":0,\"pid\":1,\"mode\":\"uart\"}"] {
            assert!(parse(raw).is_err());
        }
    }
}
