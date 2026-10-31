//! Saved development contexts. Opening a workspace never executes project code.
use crate::{api, core::*, devices, jobs, projects, toolchain};
use serde_json::{Value, json};
fn listing() -> Result {
    let values = read_json(&home()?.join("workspaces.json"), Some(json!({})))?;
    if !values.is_object()
        || values
            .as_object()
            .unwrap()
            .values()
            .any(|value| !value.is_object() || !value["path"].is_string())
    {
        return Err(Error::new(
            "invalid_workspaces",
            "Saved workspaces are malformed; restore workspaces.json from a backup",
        ));
    }
    Ok(values)
}
fn workspace(id: &str) -> Result {
    listing()?
        .get(id)
        .cloned()
        .ok_or_else(|| Error::new("workspace_missing", format!("Unknown workspace: {id}")))
}
pub fn handle(op: &str, a: &Value) -> Result {
    let id = s(a, "name");
    match op {
        "workspace.list" => listing(),
        "workspace.save" => {
            identifier(id, "workspace name", Some("[A-Za-z0-9_-]{1,64}"))?;
            let project = projects::inspect(s(a, "path"))?;
            let mut value = a.clone();
            value["path"] = project["path"].clone();
            value["updated"] = json!(now());
            if !s(a, "scenario").is_empty() {
                value["scenario"] = json!(path(s(a, "scenario"), false, true)?);
            }
            if !s(a, "bundle_id").is_empty() {
                identifier(
                    s(a, "bundle_id"),
                    "application ID",
                    Some(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+"),
                )?;
            }
            let lock = Lock::open(&home()?.join("workspaces.lock"))?;
            lock.acquire(false)?;
            let mut values = listing()?;
            values[id] = value.clone();
            atomic_json(&home()?.join("workspaces.json"), &values)?;
            Ok(value)
        }
        "workspace.remove" => {
            let lock = Lock::open(&home()?.join("workspaces.lock"))?;
            lock.acquire(false)?;
            let mut values = listing()?;
            if values.as_object_mut().unwrap().remove(id).is_none() {
                return Err(Error::new("workspace_missing", id));
            }
            atomic_json(&home()?.join("workspaces.json"), &values)?;
            Ok(json!({"removed":id}))
        }
        "workspace.context" => {
            let value = workspace(id)?;
            let project = projects::inspect(s(&value, "path"))?;
            let mut ready = json!({});
            for name in [
                "adb",
                "flutter",
                "swift",
                "xtool",
                "lldb-dap",
                "trace_processor",
            ] {
                ready[name] = json!({"available":toolchain::find_optional(name)?.is_some()});
            }
            let recent = jobs::listing(200)?
                .as_array()
                .unwrap()
                .iter()
                .filter(|job| {
                    job["plan"]["cwd"] == project["path"] || job["plan"]["workspace"] == id
                })
                .take(15)
                .cloned()
                .collect::<Vec<_>>();
            let mut recipes = json!({});
            for action in ["test", "lint", "build"] {
                recipes[action] = json!({"operation":"project.execute","arguments":{"path":project["path"],"platform":value["platform"],"action":action}});
            }
            recipes["run"] = json!({"operation":"project.run","arguments":{"path":project["path"],"platform":value["platform"],"device":value["device"],"bundle_id":value["bundle_id"]}});
            if !s(&value, "scenario").is_empty() {
                recipes["ui_test"] = json!({"operation":"testing.run","arguments":{"path":value["scenario"],"devices":[value["device"]],"directory":home()?.join("reports").join(format!("{id}-{}",uuid::Uuid::new_v4().simple()))}});
            }
            Ok(
                json!({"workspace":value,"project":project,"tools":ready,"devices":devices::listing(s(&value,"platform"))?,"jobs":recent,"recipes":recipes}),
            )
        }
        "workspace.execute" => {
            let value = workspace(id)?;
            let action = s(a, "action");
            if action == "run" {
                api::dispatch(
                    "project.run",
                    &json!({"path":value["path"],"platform":value["platform"],"device":value["device"],"bundle_id":value["bundle_id"]}),
                )
            } else if action == "ui_test" {
                if s(&value, "scenario").is_empty() {
                    return Err(Error::new(
                        "scenario_missing",
                        "Save a scenario file in this workspace first",
                    ));
                }
                api::dispatch(
                    "testing.run",
                    &json!({"path":value["scenario"],"devices":[value["device"]],"directory":home()?.join("reports").join(format!("{id}-{}",uuid::Uuid::new_v4().simple()))}),
                )
            } else {
                api::dispatch(
                    "project.execute",
                    &json!({"path":value["path"],"platform":value["platform"],"action":action,"configuration":a["configuration"]}),
                )
            }
        }
        _ => Err(Error::new("operation_not_found", op)),
    }
}
