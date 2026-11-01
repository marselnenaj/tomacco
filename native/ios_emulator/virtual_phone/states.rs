//! Saved phone states: a name kept on a verified run's disk copy, so later
//! starts can begin from it again.
//!
//! A start never changes its base, and a verified run's disks never change
//! after its stop, so saving copies nothing: the name pins the run's install
//! in `saved-states.json` beside `state.json`, and the start's pruning
//! (`Phone.prune` in the script) and `ios.virtual.cleanup` keep every pinned
//! install and what its overlay reads. Using a state makes it the base of the
//! next start, which then copies from it (or lays an overlay on it).
use super::*;

const FILE: &str = "saved-states.json";
const NAME: &str = "[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}";
/// The fields of a run a start needs from its base (`Phone.base`).
const BASE_FIELDS: [&str; 6] = [
    "run",
    "install",
    "tools",
    "trustcache",
    "graphics",
    "applications",
];

/// The saved states of a profile, by name, or an empty map. A file of
/// another shape is an error: deleting copies on a misread pin list is not.
pub(super) fn saved(directory: &Path) -> Result<serde_json::Map<String, Value>> {
    let file = directory.join(FILE);
    if fs::symlink_metadata(&file).is_err() {
        return Ok(serde_json::Map::new());
    }
    let record = read_json(&file, None)?;
    match (&record["format"], record["states"].as_object()) {
        (Value::Number(format), Some(states))
            if format.as_u64() == Some(1)
                && states
                    .values()
                    .all(|row| row["install"].as_str().is_some_and(|p| !p.is_empty())) =>
        {
            Ok(states.clone())
        }
        _ => Err(Error::new(
            "ios_virtual_profile_invalid",
            format!("{FILE} is not a list of saved phone states"),
        )
        .details(json!({"file":file}))),
    }
}

/// The installs the saved states pin.
pub(super) fn pinned(directory: &Path) -> Vec<(PathBuf, String)> {
    saved(directory)
        .unwrap_or_default()
        .iter()
        .map(|(name, row)| (PathBuf::from(s(row, "install")), name.clone()))
        .collect()
}

fn write(directory: &Path, states: serde_json::Map<String, Value>) -> Result<()> {
    atomic_json(&directory.join(FILE), &json!({"format":1,"states":states}))
}

fn name(a: &Value) -> Result<String> {
    Ok(identifier(s(a, "name"), "state name", Some(NAME))?.to_string())
}

fn refuse_busy() -> Result<()> {
    if let Some(job) = jobs::active_exclusive(EXCLUSIVE)? {
        return Err(Error::new(
            "ios_virtual_busy",
            "A start or stop is in progress; try again when it has ended",
        )
        .details(json!({"job":job})));
    }
    Ok(())
}

/// The verified run of a state, as the base a start reads (`Phone.base`).
fn verified(state: &Value) -> Option<Value> {
    if state["verified"]["install"].as_str().is_some() {
        return Some(state["verified"].clone());
    }
    // A state written before verified runs were recorded: its closed run.
    (state["closed"] == true && text(state, "install")).then(|| {
        let mut base = serde_json::Map::new();
        for key in BASE_FIELDS {
            if !state[key].is_null() {
                base.insert(key.into(), state[key].clone());
            }
        }
        Value::Object(base)
    })
}

fn row(name: &str, record: &Value, state: &Value) -> Value {
    let install = PathBuf::from(s(record, "install"));
    let exists = fs::symlink_metadata(disk_overlay::root_disk(&install)).is_ok_and(|m| m.is_file());
    json!({"name":name,"run":record["run"],"install":install,"saved":record["saved"],
        "model":record.get("model").cloned().unwrap_or(json!("")),
        "exists":exists,"bytes":allocated_bytes(&install),
        "base":state["verified"]["install"] == record["install"]})
}

/// Lists the saved states, and which one (if any) the next start's base is.
pub fn list(_a: &Value) -> Result {
    let directory = directory()?;
    let (_, state) = records(&directory)?;
    let states = saved(&directory)?;
    let rows: Vec<Value> = states
        .iter()
        .map(|(name, record)| row(name, record, &state))
        .collect();
    let base = verified(&state);
    let base_saved: Vec<&String> = states
        .iter()
        .filter(|(_, record)| {
            base.as_ref()
                .is_some_and(|b| b["install"] == record["install"])
        })
        .map(|(name, _)| name)
        .collect();
    Ok(json!({"directory":directory,"states":rows,
        "base":base.map(|b| json!({"run":b["run"],"install":b["install"]})).unwrap_or(Value::Null),
        "base_saved_as":base_saved}))
}

/// Keeps the last verified run under a name. Nothing is copied.
pub fn save(a: &Value) -> Result {
    let name = name(a)?;
    let directory = directory()?;
    let (_, state) = records(&directory)?;
    refuse_busy()?;
    let Some(mut base) = verified(&state) else {
        return Err(Error::new(
            "ios_virtual_no_verified_run",
            "The phone has no verified run to save; stop a run with ios.virtual.stop first",
        ));
    };
    let mut states = saved(&directory)?;
    if states.contains_key(&name) && a["replace"] != true {
        return Err(Error::new(
            "ios_virtual_state_exists",
            format!("A saved state is already called {name:?}"),
        )
        .remedy(
            "Choose another name, or pass replace:true to point it at the current verified run",
        ));
    }
    base["saved"] = json!(now());
    // The model the run was last started as, for the list; a start uses the
    // model configured at the time.
    base["model"] = state.get("model").cloned().unwrap_or(json!(""));
    states.insert(name.clone(), base.clone());
    write(&directory, states)?;
    Ok(json!({"saved":row(&name,&base,&state)}))
}

/// Removes a name, and the disk copy it kept when nothing else keeps it.
pub fn delete(a: &Value) -> Result {
    let name = name(a)?;
    let directory = directory()?;
    let (profile, state) = records(&directory)?;
    refuse_busy()?;
    let mut states = saved(&directory)?;
    let Some(record) = states.remove(&name) else {
        return Err(Error::new(
            "ios_virtual_state_unknown",
            format!("No saved state is called {name:?}"),
        )
        .details(json!({"states":saved(&directory)?.keys().collect::<Vec<_>>()})));
    };
    write(&directory, states.clone())?;
    let install = PathBuf::from(s(&record, "install"));
    let installs = PathBuf::from(s(&profile, "installs"));
    // Kept: the base, the current run, an attempt, another name, and what
    // an overlay among those reads.
    let mut keepers: Vec<(PathBuf, String)> = Vec::new();
    for (key, reason) in [
        (&state["verified"]["install"], "verified-base"),
        (&state["install"], "current-run"),
        (&state["attempt"]["install"], "start-attempt"),
    ] {
        if let Some(path) = key.as_str() {
            keepers.push((PathBuf::from(path), reason.into()));
        }
    }
    for (other, row) in &states {
        keepers.push((
            PathBuf::from(s(row, "install")),
            format!("saved-state:{other}"),
        ));
    }
    let roots: Vec<PathBuf> = keepers.iter().map(|(path, _)| path.clone()).collect();
    let shielded = disk_overlay::with_backing(&roots);
    let reason = keepers
        .iter()
        .find(|(path, _)| path == &install)
        .map(|(_, reason)| reason.clone())
        .or_else(|| disk_overlay::among(&shielded, &install).then(|| "overlay-backing".into()));
    let bytes = allocated_bytes(&install);
    let (removed, kept_because) = match reason {
        Some(reason) => (false, Value::from(reason)),
        None if !owned_copy(&installs, &install) => {
            (false, json!("not a disk copy Tomacco made in installs"))
        }
        None => match remove_copy(&installs, &install) {
            Ok(()) => (true, Value::Null),
            Err(reason) => (false, json!(reason)),
        },
    };
    Ok(json!({"deleted":name,"install":install,"removed":removed,
        "freed_bytes":if removed {bytes} else {0},"kept_because":kept_because}))
}

/// Makes a saved state the base of the next start. The phone must be off.
/// The current base is given up only when a name keeps it or with
/// discard_current; otherwise a later start's pruning could delete it.
pub fn use_state(a: &Value) -> Result {
    let name = name(a)?;
    let directory = directory()?;
    let (_, mut state) = records(&directory)?;
    refuse_busy()?;
    if guest_is_running(&state) || state["closed"] != true {
        return Err(Error::new(
            "ios_virtual_running",
            "The phone is running or its last run has not been stopped; stop it first",
        ));
    }
    let states = saved(&directory)?;
    let Some(record) = states.get(&name).cloned() else {
        return Err(Error::new(
            "ios_virtual_state_unknown",
            format!("No saved state is called {name:?}"),
        )
        .details(json!({"states":states.keys().collect::<Vec<_>>()})));
    };
    let install = PathBuf::from(s(&record, "install"));
    if !fs::symlink_metadata(disk_overlay::root_disk(&install)).is_ok_and(|m| m.is_file()) {
        return Err(Error::new(
            "ios_virtual_state_missing",
            format!("The disks of the saved state {name:?} are gone"),
        )
        .details(json!({"install":install})));
    }
    let current = verified(&state);
    let current_saved = current
        .as_ref()
        .is_none_or(|base| states.values().any(|row| row["install"] == base["install"]));
    if !current_saved && a["discard_current"] != true {
        return Err(Error::new(
            "ios_virtual_state_unsaved",
            "The current verified run is not saved; a later start may delete its disks",
        )
        .remedy("Save it first with ios.virtual.state_save, or pass discard_current:true")
        .details(json!({"current":current})));
    }
    let mut base = serde_json::Map::new();
    for key in BASE_FIELDS {
        if !record[key].is_null() {
            base.insert(key.into(), record[key].clone());
        }
    }
    let base = Value::Object(base);
    for key in BASE_FIELDS {
        state[key] = base[key].clone();
    }
    state["verified"] = base.clone();
    state["closed"] = json!(true);
    state["phase"] = json!("stopped");
    state["error"] = Value::Null;
    state["guest_job"] = Value::Null;
    state["attempt"] = Value::Null;
    state["outcome"] = json!({"outcome":"saved-state","name":name});
    state["updated"] = json!(now());
    atomic_json(&directory.join("state.json"), &state)?;
    Ok(json!({"used":name,"base":base,"previous":current}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_are_short_plain_words() {
        for good in ["clean", "Before login", "v1.2_test-3"] {
            assert!(name(&json!({"name":good})).is_ok(), "{good}");
        }
        for bad in ["", " lead", "../x", "a/b", &"x".repeat(65)] {
            assert!(name(&json!({"name":bad})).is_err(), "{bad}");
        }
    }

    #[test]
    fn the_pin_list_is_read_strictly() {
        let root = tempfile::tempdir().unwrap();
        assert!(saved(root.path()).unwrap().is_empty());
        fs::write(
            root.path().join(FILE),
            r#"{"format":1,"states":{"a":{"install":"/x","run":3}}}"#,
        )
        .unwrap();
        assert_eq!(pinned(root.path()), vec![(PathBuf::from("/x"), "a".into())]);
        fs::write(root.path().join(FILE), r#"{"format":1,"states":{"a":{}}}"#).unwrap();
        assert_eq!(
            saved(root.path()).unwrap_err().code,
            "ios_virtual_profile_invalid"
        );
        assert!(pinned(root.path()).is_empty());
    }

    #[test]
    fn the_base_of_an_old_state_is_its_closed_run() {
        let state = json!({"closed":true,"run":4,"install":"/i","tools":"/t"});
        assert_eq!(
            verified(&state).unwrap(),
            json!({"run":4,"install":"/i","tools":"/t"})
        );
        assert!(verified(&json!({"closed":false,"run":4,"install":"/i"})).is_none());
        let state = json!({"closed":false,"verified":{"run":3,"install":"/v"}});
        assert_eq!(verified(&state).unwrap()["run"], 3);
    }
}
