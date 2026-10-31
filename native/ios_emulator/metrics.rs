//! Read-only emulator measurements. Cumulative counters are not application FPS.
use super::*;

fn field<'a>(report: &'a str, name: &str) -> Option<&'a str> {
    let mut matches = report.lines().filter_map(|line| {
        line.strip_prefix(name)
            .filter(|value| value.starts_with(char::is_whitespace))
            .map(str::trim)
    });
    let value = matches.next()?;
    matches.next().is_none().then_some(value)
}

fn translation(report: &str) -> Value {
    let cache = field(report, "gen code size")
        .and_then(|value| value.split_once('/'))
        .and_then(|(used, capacity)| {
            Some((used.parse::<u64>().ok()?, capacity.parse::<u64>().ok()?))
        })
        .filter(|(used, capacity)| *capacity > 0 && used <= capacity);
    let counter = |name| field(report, name).and_then(|value| value.parse::<u64>().ok());
    let blocks = counter("TB count");
    let clock = field(report, "Host - Guest clock")
        .and_then(|value| value.strip_suffix(" ms"))
        .and_then(|value| value.parse::<i64>().ok());
    json!({
        "available":cache.is_some() || blocks.is_some(),
        "code_bytes":cache.map(|value|value.0),
        "capacity_bytes":cache.map(|value|value.1),
        "translation_blocks":blocks,
        "flushes":counter("TB flush count"),
        "invalidations":counter("TB invalidate count"),
        "host_minus_guest_ms":clock,
        "raw_report":report
    })
}

pub(super) fn read(client: &mut Qmp, request: &Value) -> Result<Value> {
    let started = now();
    let state = client.call("query-status", json!({}))?;
    let cpus = client.call("query-cpus-fast", json!({}))?;
    let block_statistics = client.call("query-blockstats", json!({}))?;
    let tcg = match client.call("human-monitor-command", json!({"command-line":"info jit"})) {
        Ok(value) => translation(value.as_str().ok_or_else(|| {
            Error::new(
                "invalid_qmp",
                "Translation statistics must be a text report",
            )
        })?),
        Err(error) if error.code == "qmp_error" => json!({"available":false,"error":error}),
        Err(error) => return Err(error),
    };
    let wide_chaining = if request
        .get("translation_cache_mib")
        .and_then(Value::as_u64)
        .is_some_and(|mib| mib > 2048)
    {
        match client.call(
            "qom-get",
            json!({"path":"/machine/accel", "property":"wide-tb-chain-patches"}),
        ) {
            Ok(value) => {
                json!({"available":true, "far_chain_selections":value.as_u64().ok_or_else(||
                Error::new("invalid_qmp", "Wide TB-chain counter must be a nonnegative integer"))?,
                "semantics":"Far-chain link selections since engine startup, not executed guest branches or FPS"})
            }
            Err(error) if error.code == "qmp_error" => json!({"available":false,"error":error}),
            Err(error) => return Err(error),
        }
    } else {
        json!({"available":false, "reason":"legacy_cache_request"})
    };
    // Measurements are taken sequentially without pausing or resetting the
    // guest. Preserve native disk/device identities and original units.
    Ok(json!({
        "sample_started_at":started,"sample_finished_at":now(),
        "state":state,"cpus":cpus,"block_statistics":block_statistics,
        "clock_mode":request.get("clock_mode"),
        "host_cpu_placement":request.get("host_cpu_placement"),
        "requested_translation_cache_mib":request.get("translation_cache_mib"),
        "wide_tb_chaining":wide_chaining,
        "tcg":tcg
    }))
}
