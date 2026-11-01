//! The graphics card's memory and the host's pressure, as `ios.virtual.status`
//! reports them on every call and as a start warns about them.
//!
//! While a guest runs, its launcher publishes each check of the graphics
//! memory guard (`gpu-memory.json` beside its result, tools/run_ios_paravirt.py):
//! the card that guest renders on, with how many short checks in a row there
//! were. Otherwise, and when that record is old, the card with the most
//! memory under /sys stands in, as `ios.virtual.host_check` reads it. Host
//! pressure is Linux PSI's "full" share from /proc/pressure, judged by the
//! limits the launcher's admission uses (tools/ios_host_pressure.py). This
//! reads files only; it starts and writes nothing.
use super::*;

const GIB: f64 = (1u64 << 30) as f64;
/// The launcher's record of the guard, beside its result.json.
const GUARD_RECORD: &str = "gpu-memory.json";
/// The guard checks every 5 s; an older record no longer describes the card.
const GUARD_RECORD_FRESH: f64 = 30.0;
/// FULL_PRESSURE_LIMITS in tools/ios_host_pressure.py, in percent.
const PRESSURE_LIMITS: [(&str, f64); 2] = [("io", 20.0), ("memory", 10.0)];

fn gib(bytes: u64) -> String {
    format!("{:.1} GiB", bytes as f64 / GIB)
}

/// The guard's view of the card: its free memory against the eighth of it
/// the guard keeps, from the running guest's record when that is fresh, else
/// from the biggest card /sys describes. Null when no card reports memory.
pub(crate) fn gpu(record: Option<&Value>, cards: &[host_check::Card], now: f64) -> Value {
    let fresh = record.filter(|record| {
        record["format"] == 1
            && record["checked"]
                .as_f64()
                .is_some_and(|checked| (now - checked).abs() <= GUARD_RECORD_FRESH)
            && record["total_bytes"]
                .as_u64()
                .is_some_and(|total| total > 0)
            && record["used_bytes"].is_u64()
    });
    let (source, used, total, short_checks, stopping, checked) = if let Some(record) = fresh {
        (
            "guard",
            record["used_bytes"].as_u64().unwrap_or(0),
            record["total_bytes"].as_u64().unwrap_or(0),
            record["short_checks"].as_u64(),
            record["stopping"].as_bool(),
            record["checked"].clone(),
        )
    } else {
        let Some(card) = cards
            .iter()
            .filter(|card| {
                card.vram_total.is_some_and(|total| total > 0) && card.vram_used.is_some()
            })
            .max_by_key(|card| card.vram_total)
        else {
            return Value::Null;
        };
        (
            "sysfs",
            card.vram_used.unwrap_or(0),
            card.vram_total.unwrap_or(0),
            None,
            None,
            json!(now),
        )
    };
    let free = total.saturating_sub(used);
    let limit = total / 8;
    let level = if free < limit {
        "short"
    } else if free < total / 4 {
        "low"
    } else {
        "ok"
    };
    json!({"source":source,"used_bytes":used,"total_bytes":total,"free_bytes":free,
        "guard_limit_free_bytes":limit,"level":level,"short_checks":short_checks,
        "guard_stopping":stopping,"checked":checked})
}

/// One /proc/pressure file's "full" line as {avg10, avg60} percent, or None.
fn full_line(text: &str) -> Option<(f64, f64)> {
    let line = text.lines().find(|line| line.starts_with("full "))?;
    let field = |name: &str| {
        line.split_whitespace()
            .find_map(|field| field.strip_prefix(name)?.strip_prefix('='))
            .and_then(|value| value.parse::<f64>().ok())
            .filter(|value| value.is_finite() && (0.0..=100.0).contains(value))
    };
    Some((field("avg10")?, field("avg60")?))
}

/// I/O and memory pressure against the launcher's limits. `read` gives a
/// resource's PSI text (None when the kernel has no PSI).
pub(crate) fn pressure(read: impl Fn(&str) -> Option<String>) -> Value {
    let mut resources = serde_json::Map::new();
    let mut blocked = Vec::new();
    let mut observed = true;
    for (resource, limit) in PRESSURE_LIMITS {
        let row = match read(resource).as_deref().and_then(full_line) {
            Some((avg10, avg60)) => {
                if avg10 >= limit || avg60 >= limit {
                    blocked.push(resource);
                }
                json!({"full_avg10_percent":avg10,"full_avg60_percent":avg60,"limit_percent":limit})
            }
            None => {
                observed = false;
                json!({"full_avg10_percent":null,"full_avg60_percent":null,"limit_percent":limit})
            }
        };
        resources.insert(resource.into(), row);
    }
    json!({"measure":"psi","observed":observed,"blocked":blocked,"resources":resources})
}

/// What a start should know before it begins, as {check, message} rows.
pub(crate) fn warnings(gpu: &Value, pressure: &Value) -> Vec<Value> {
    let mut rows = Vec::new();
    if gpu.is_object() {
        let (free, total) = (n(gpu, "free_bytes"), n(gpu, "total_bytes"));
        match s(gpu, "level") {
            "short" => rows.push(json!({"check":"gpu","message":format!(
                "Only {} of the graphics card's {} is free, under the eighth the graphics memory guard keeps: it \
                 would stop the session soon after the boot. Close other programs that use the card first.",
                gib(free), gib(total))})),
            "low" => rows.push(json!({"check":"gpu","message":format!(
                "Only {} of the graphics card's {} is free. The graphics memory guard stops a session when under \
                 {} stays free; close other programs that use the card if you can.",
                gib(free), gib(total), gib(n(gpu, "guard_limit_free_bytes")))})),
            _ => {}
        }
    }
    let blocked: Vec<&str> = pressure["blocked"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .collect();
    if !blocked.is_empty() {
        rows.push(json!({"check":"pressure","message":format!(
            "The host is under {} pressure now. The start waits up to five minutes for it to ease before it copies \
             and boots, and refuses if it does not.",
            blocked.join(" and "))}));
    }
    rows
}

/// The guard's record beside a guest job's result, when that job runs.
fn guard_record(guest: Option<&Value>) -> Option<Value> {
    let job = guest?;
    if jobs::TERMINAL.contains(&s(job, "state")) {
        return None;
    }
    let file = PathBuf::from(s(&job["plan"], "cwd"))
        .join("paravirtual")
        .join(GUARD_RECORD);
    if file.is_symlink() || fs::metadata(&file).ok()?.len() > 4096 {
        return None;
    }
    read_json(&file, None).ok()
}

/// The status's `host` block: the card's memory and the host's pressure.
pub(crate) fn host(guest: Option<&Value>) -> Value {
    json!({
        "gpu":gpu(guard_record(guest).as_ref(),&host_check::cards(Path::new("/sys")),now()),
        "pressure":pressure(|resource| fs::read_to_string(Path::new("/proc/pressure").join(resource)).ok()),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const G: u64 = 1 << 30;

    fn card(total: u64, used: u64) -> host_check::Card {
        host_check::Card {
            pci: "0000:03:00.0".into(),
            driver: "amdgpu".into(),
            vram_total: Some(total),
            vram_used: Some(used),
        }
    }

    #[test]
    fn the_running_guests_guard_record_wins_while_it_is_fresh() {
        let record = json!({"format":1,"checked":1000.0,"used_bytes":15 * G,"total_bytes":16 * G,
            "short_checks":1,"stopping":false});
        let seen = gpu(Some(&record), &[card(16 * G, G)], 1010.0);
        assert_eq!(seen["source"], "guard");
        assert_eq!(seen["free_bytes"], G);
        assert_eq!(seen["guard_limit_free_bytes"], 2 * G);
        assert_eq!(seen["level"], "short");
        assert_eq!(seen["short_checks"], 1);
        assert_eq!(seen["guard_stopping"], false);
        // A record older than half a minute, or of another shape, gives way to /sys.
        for stale in [
            gpu(Some(&record), &[card(16 * G, G)], 1100.0),
            gpu(
                Some(&json!({"format":1,"checked":1000.0,"used_bytes":null,"total_bytes":16 * G})),
                &[card(16 * G, G)],
                1000.0,
            ),
        ] {
            assert_eq!(stale["source"], "sysfs");
            assert_eq!(stale["free_bytes"], 15 * G);
            assert_eq!(stale["level"], "ok");
            assert!(stale["short_checks"].is_null());
        }
    }

    #[test]
    fn the_biggest_card_stands_in_and_a_card_without_memory_is_unknown() {
        let integrated = host_check::Card {
            pci: "0000:00:02.0".into(),
            driver: "i915".into(),
            vram_total: None,
            vram_used: None,
        };
        let seen = gpu(
            None,
            &[integrated.clone(), card(8 * G, 5 * G), card(16 * G, 13 * G)],
            5.0,
        );
        assert_eq!(
            (seen["total_bytes"].clone(), seen["level"].clone()),
            (json!(16 * G), json!("low"))
        );
        assert!(gpu(None, &[integrated], 5.0).is_null());
        assert!(gpu(None, &[], 5.0).is_null());
    }

    #[test]
    fn pressure_is_judged_by_the_launchers_limits() {
        let quiet = "some avg10=1.00 avg60=2.00 avg300=0.50 total=1\nfull avg10=0.50 avg60=1.00 avg300=0.10 total=1\n";
        let busy = "some avg10=60.00 avg60=40.00 avg300=9.00 total=1\nfull avg10=25.00 avg60=12.00 avg300=3.00 total=1\n";
        let seen = pressure(|resource| Some(if resource == "io" { busy } else { quiet }.into()));
        assert_eq!(seen["blocked"], json!(["io"]));
        assert_eq!(seen["observed"], true);
        assert_eq!(seen["resources"]["io"]["full_avg10_percent"], 25.0);
        assert_eq!(seen["resources"]["memory"]["limit_percent"], 10.0);
        // The one-minute window alone also counts, as in the admission.
        let lingering = "full avg10=1.00 avg60=11.00 avg300=3.00 total=1\n";
        assert_eq!(
            pressure(|_| Some(lingering.into()))["blocked"],
            json!(["memory"])
        );
        // A kernel without PSI, or a line of another form, is unknown, never quiet.
        let unknown = pressure(|_| None);
        assert_eq!(
            (unknown["observed"].clone(), unknown["blocked"].clone()),
            (json!(false), json!([]))
        );
        assert_eq!(
            pressure(|_| Some("full avg10=x avg60=1.0\n".into()))["observed"],
            false
        );
    }

    #[test]
    fn a_start_is_warned_of_a_full_card_and_of_pressure() {
        let quiet = pressure(|_| Some("full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n".into()));
        assert!(warnings(&gpu(None, &[card(16 * G, G)], 0.0), &quiet).is_empty());
        assert!(warnings(&Value::Null, &quiet).is_empty());
        let low = warnings(&gpu(None, &[card(16 * G, 13 * G)], 0.0), &quiet);
        assert_eq!(low.len(), 1);
        assert!(
            s(&low[0], "message")
                .starts_with("Only 3.0 GiB of the graphics card's 16.0 GiB is free.")
        );
        let short = warnings(&gpu(None, &[card(16 * G, 15 * G)], 0.0), &quiet);
        assert!(s(&short[0], "message").contains("would stop the session"));
        let busy = pressure(|_| Some("full avg10=50.00 avg60=50.00 avg300=0.00 total=0\n".into()));
        let rows = warnings(&Value::Null, &busy);
        assert_eq!(rows[0]["check"], "pressure");
        assert!(s(&rows[0], "message").contains("io and memory pressure"));
    }
}
