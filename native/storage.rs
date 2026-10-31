//! Housekeeping of Tomacco's data directory: old job records and logs, and
//! old report directories. A dry run unless `apply` is given.
//!
//! Only what Tomacco wrote for finished work is ever removed: records of
//! jobs that ended, their logs, log files without a record, and directories
//! under `reports/`. Queued and running jobs, emulators, SDKs, the virtual
//! iOS device, update backups and settings are never touched, and nor are
//! artifacts a job built in a project.
use crate::{core::*, jobs};
use rusqlite::{Connection, params};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashSet},
    fs,
    path::Path,
    time::UNIX_EPOCH,
};

const DAY: f64 = 86400.0;
const LISTED: usize = 500;

fn modified(metadata: &fs::Metadata) -> f64 {
    metadata
        .modified()
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map_or(0.0, |d| d.as_secs_f64())
}

/// Bytes and the newest modification time under `path`, links not followed.
fn measure(path: &Path) -> (u64, f64) {
    let Ok(metadata) = fs::symlink_metadata(path) else {
        return (0, 0.0);
    };
    let (mut bytes, mut newest) = (metadata.len(), modified(&metadata));
    if metadata.is_dir() {
        for entry in fs::read_dir(path).into_iter().flatten().flatten() {
            let (b, n) = measure(&entry.path());
            bytes += b;
            newest = newest.max(n);
        }
    }
    (bytes, newest)
}

fn file_bytes(path: &Path) -> u64 {
    fs::symlink_metadata(path).map_or(0, |m| m.len())
}

/// The files of one job in `jobs/`: its log, worker lease and anything else
/// named after its id.
fn job_files(root: &Path, id: &str) -> Vec<std::path::PathBuf> {
    fs::read_dir(root.join("jobs"))
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.path())
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.split('.').next() == Some(id))
        })
        .collect()
}

pub struct Options {
    pub cutoff: f64,
    pub max_log_bytes: Option<u64>,
    pub jobs: bool,
    pub reports: bool,
    pub apply: bool,
}

/// What would be removed, or was, under `root` with the job database `db`.
pub fn prune(root: &Path, db: &Connection, options: &Options) -> Result {
    let mut candidates: Vec<Value> = Vec::new();
    if options.jobs {
        let mut statement = db.prepare(
            "SELECT id,label,state,updated FROM jobs WHERE state IN ('succeeded','failed','cancelled','timed_out') ORDER BY updated",
        )?;
        let finished = statement
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, f64>(3)?,
                ))
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        let mut kept_bytes = 0u64;
        let mut kept = Vec::new();
        for (id, label, state, updated) in finished {
            let bytes: u64 = job_files(root, &id).iter().map(|p| file_bytes(p)).sum();
            let entry = json!({"kind":"job","id":id,"label":label,"state":state,"bytes":bytes,"age_days":((now()-updated)/DAY*10.0).round()/10.0});
            if updated < options.cutoff {
                candidates.push(entry);
            } else {
                kept_bytes += bytes;
                kept.push(entry);
            }
        }
        // Over the size limit, the oldest finished jobs go first.
        if let Some(limit) = options.max_log_bytes {
            for entry in kept {
                if kept_bytes <= limit {
                    break;
                }
                kept_bytes -= n(&entry, "bytes");
                candidates.push(entry);
            }
        }
        let known: HashSet<String> = db
            .prepare("SELECT id FROM jobs")?
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<std::result::Result<_, _>>()?;
        let mut orphans: BTreeMap<String, u64> = BTreeMap::new();
        let mut ages: BTreeMap<String, f64> = BTreeMap::new();
        for entry in fs::read_dir(root.join("jobs"))
            .into_iter()
            .flatten()
            .flatten()
        {
            let name = entry.file_name().to_string_lossy().into_owned();
            let id = name.split('.').next().unwrap_or("").to_string();
            let Ok(metadata) = entry.metadata() else {
                continue;
            };
            if id.len() != 32 || !id.bytes().all(|c| c.is_ascii_hexdigit()) || known.contains(&id) {
                continue;
            }
            *orphans.entry(id.clone()).or_default() += metadata.len();
            let age = ages.entry(id).or_insert(0.0);
            *age = age.max(modified(&metadata));
        }
        for (id, bytes) in orphans {
            if ages[&id] < options.cutoff {
                candidates.push(json!({"kind":"job_files","id":id,"bytes":bytes,"age_days":((now()-ages[&id])/DAY*10.0).round()/10.0}));
            }
        }
    }
    if options.reports {
        for entry in fs::read_dir(root.join("reports"))
            .into_iter()
            .flatten()
            .flatten()
        {
            let path = entry.path();
            let (bytes, newest) = measure(&path);
            if newest < options.cutoff {
                candidates.push(json!({"kind":"report","path":path,"bytes":bytes,"age_days":((now()-newest)/DAY*10.0).round()/10.0}));
            }
        }
    }
    let total: u64 = candidates.iter().map(|c| n(c, "bytes")).sum();
    let mut removed = 0u64;
    let mut freed = 0u64;
    let mut errors = Vec::new();
    if options.apply {
        for candidate in &candidates {
            let outcome = (|| -> Result<()> {
                match s(candidate, "kind") {
                    "job" => {
                        let id = s(candidate, "id");
                        // Only while it is still finished: a record is never
                        // taken from a job that something revived.
                        let changed = db.execute(
                            "DELETE FROM jobs WHERE id=? AND state IN ('succeeded','failed','cancelled','timed_out')",
                            params![id],
                        )?;
                        if changed == 1 {
                            db.execute("DELETE FROM inputs WHERE job=?", params![id])?;
                            for file in job_files(root, id) {
                                fs::remove_file(file)?;
                            }
                        }
                    }
                    "job_files" => {
                        for file in job_files(root, s(candidate, "id")) {
                            fs::remove_file(file)?;
                        }
                    }
                    _ => {
                        let path = Path::new(s(candidate, "path"));
                        if path.parent() != Some(&root.join("reports")) {
                            return Err(Error::new("invalid_path", "Not a report directory"));
                        }
                        if fs::symlink_metadata(path)?.is_dir() {
                            fs::remove_dir_all(path)?;
                        } else {
                            fs::remove_file(path)?;
                        }
                    }
                }
                Ok(())
            })();
            match outcome {
                Ok(()) => {
                    removed += 1;
                    freed += n(candidate, "bytes");
                }
                Err(error) => {
                    errors.push(json!({"candidate":candidate,"error":error.value()}));
                }
            }
        }
    }
    let count = candidates.len();
    candidates.truncate(LISTED);
    Ok(json!({
        "dry_run":!options.apply,"cutoff":options.cutoff,
        "candidates":candidates,"candidate_count":count,"candidate_bytes":total,
        "removed":removed,"freed_bytes":freed,"errors":errors,
    }))
}

/// The size of each top-level entry of the data directory.
fn usage(root: &Path) -> Value {
    let mut entries: Vec<(String, u64)> = fs::read_dir(root)
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| {
            (
                e.file_name().to_string_lossy().into_owned(),
                measure(&e.path()).0,
            )
        })
        .collect();
    entries.sort_by(|a, b| b.1.cmp(&a.1));
    let total: u64 = entries.iter().map(|e| e.1).sum();
    json!({"path":root,"bytes":total,"entries":entries.into_iter().map(|(name,bytes)| json!({"name":name,"bytes":bytes})).collect::<Vec<_>>()})
}

pub fn handle(a: &Value) -> Result {
    let root = home()?;
    let include = strings(&a["include"]);
    let options = Options {
        cutoff: now() - a["older_than_days"].as_f64().unwrap_or(30.0) * DAY,
        max_log_bytes: a["max_job_log_mb"].as_u64().map(|mb| mb * 1024 * 1024),
        jobs: include.iter().any(|i| i == "jobs"),
        reports: include.iter().any(|i| i == "reports"),
        apply: b(a, "apply"),
    };
    let db = jobs::database()?;
    let mut value = prune(&root, &db, &options)?;
    value["usage"] = usage(&root);
    if !options.apply && n(&value, "candidate_count") > 0 {
        value["next"] = json!(
            "Check the candidates, then call storage.prune again with the same arguments and apply:true"
        );
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn insert(db: &Connection, id: &str, state: &str, age_days: f64) {
        db.execute(
            "INSERT INTO jobs(id,label,state,created,updated,spec) VALUES(?,?,?,?,?,?)",
            params![
                id,
                "Build",
                state,
                now() - age_days * DAY,
                now() - age_days * DAY,
                "{}"
            ],
        )
        .unwrap();
    }
    fn log(root: &Path, id: &str, bytes: usize) {
        fs::write(
            root.join("jobs").join(format!("{id}.log")),
            vec![b'x'; bytes],
        )
        .unwrap();
        fs::write(root.join("jobs").join(format!("{id}.worker.lock")), b"").unwrap();
    }
    fn ids(value: &Value) -> Vec<String> {
        value["candidates"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| format!("{}:{}", s(c, "kind"), s(c, "id")))
            .collect()
    }

    #[test]
    fn only_finished_old_jobs_and_old_reports_are_removed_and_only_when_applied() {
        let root = tempfile::tempdir().unwrap();
        let root = root.path();
        fs::create_dir_all(root.join("jobs")).unwrap();
        let db = jobs::open(root).unwrap();
        let (old, recent, running, orphan) = (
            "a".repeat(32),
            "b".repeat(32),
            "c".repeat(32),
            "d".repeat(32),
        );
        insert(&db, &old, "succeeded", 40.0);
        insert(&db, &recent, "failed", 1.0);
        insert(&db, &running, "running", 90.0);
        for id in [&old, &recent, &running, &orphan] {
            log(root, id, 100);
        }
        // An orphan log is old by its modification time.
        fs::remove_file(root.join("jobs").join(format!("{orphan}.worker.lock"))).unwrap();
        let file = fs::File::options()
            .write(true)
            .open(root.join("jobs").join(format!("{orphan}.log")))
            .unwrap();
        file.set_modified(UNIX_EPOCH + std::time::Duration::from_secs(1_000_000))
            .unwrap();
        let report = root.join("reports/old-run");
        fs::create_dir_all(&report).unwrap();
        fs::write(report.join("junit.xml"), b"<testsuite/>").unwrap();
        let mut options = Options {
            cutoff: now() - 30.0 * DAY,
            max_log_bytes: None,
            jobs: true,
            reports: false,
            apply: false,
        };
        let dry = prune(root, &db, &options).unwrap();
        assert_eq!(dry["dry_run"], true);
        assert_eq!(
            ids(&dry),
            [format!("job:{old}"), format!("job_files:{orphan}")]
        );
        assert_eq!(dry["candidate_bytes"], 200);
        assert!(root.join("jobs").join(format!("{old}.log")).exists());
        // Over a size limit, the oldest finished job that is still young goes too.
        options.max_log_bytes = Some(50);
        let limited = prune(root, &db, &options).unwrap();
        assert!(ids(&limited).contains(&format!("job:{recent}")));
        assert!(!ids(&limited).contains(&format!("job:{running}")));
        options.max_log_bytes = None;
        options.apply = true;
        options.reports = true;
        let applied = prune(root, &db, &options).unwrap();
        assert_eq!(applied["removed"], 2);
        assert_eq!(applied["errors"], json!([]));
        assert!(!root.join("jobs").join(format!("{old}.log")).exists());
        assert!(!root.join("jobs").join(format!("{orphan}.log")).exists());
        assert!(root.join("jobs").join(format!("{recent}.log")).exists());
        assert!(root.join("jobs").join(format!("{running}.log")).exists());
        // The report was written just now, so it stays.
        assert!(report.exists());
        let remaining: i64 = db
            .query_row("SELECT count(*) FROM jobs", [], |r| r.get(0))
            .unwrap();
        assert_eq!(remaining, 2);
        options.cutoff = now() + 10.0;
        options.jobs = false;
        let reports = prune(root, &db, &options).unwrap();
        assert_eq!(reports["removed"], 1);
        assert!(!report.exists());
        assert!(root.join("reports").exists());
    }
}
