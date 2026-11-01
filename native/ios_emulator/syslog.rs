//! The virtual iPhone's log as structured lines: the guest's serial console
//! (`uart.log` of its guest job), where the kernel prints and launchd and
//! other processes forward their log records, read on the host and split
//! into time, process, PID, level and message.
//!
//! Nothing is asked of the guest: the console file is the engine's own
//! output. A caller follows the log by passing back `next_offset`.
use super::*;

/// How much of the log a first read without an offset looks back.
const TAIL_BYTES: u64 = 256 * 1024;
/// The most one read scans.
const READ_BYTES: u64 = 1024 * 1024;
/// The longest line kept; a longer one is cut.
const LINE_CHARS: usize = 2000;

/// One console line, split as far as its form allows.
#[derive(Debug, Default, PartialEq)]
pub(crate) struct Line {
    pub time: Option<String>,
    pub process: String,
    pub pid: Option<u64>,
    pub service: Option<String>,
    pub sender: Option<String>,
    pub level: Option<String>,
    pub message: String,
}

fn level(name: &str) -> Option<String> {
    let name = name.to_ascii_lowercase();
    [
        "emergency",
        "alert",
        "critical",
        "error",
        "fault",
        "warning",
        "notice",
        "info",
        "debug",
        "default",
    ]
    .contains(&name.as_str())
    .then_some(name)
}

/// A launchd domain, the part of a service label before the service's own
/// name: `system`, `user/UID`, `gui/UID`, `login/ASID` or `pid/PID`. Returns
/// the domain and what follows it (empty for the domain itself).
fn split_domain(label: &str) -> Option<(&str, &str)> {
    if let Some(rest) = label.strip_prefix("system") {
        return match rest {
            "" => Some((label, "")),
            _ => rest.strip_prefix('/').map(|name| ("system", name)),
        };
    }
    for kind in ["user/", "gui/", "login/", "pid/"] {
        let Some(rest) = label.strip_prefix(kind) else {
            continue;
        };
        let (id, name) = rest.split_once('/').unwrap_or((rest, ""));
        if !id.is_empty() && id.chars().all(|c| c.is_ascii_digit()) {
            return Some((&label[..kind.len() + id.len()], name));
        }
    }
    None
}

/// The process and PID a launchd record's parentheses name. Forms seen on a
/// live phone's console: `DOMAIN/SERVICE [PID]` and `DOMAIN/SERVICE` (a
/// service, running or not), `pid/PID [NAME]` (the domain of one process,
/// named by its 15-character command name), a bare domain such as `system`
/// or `user/501` (launchd's own record about it), and a boot task's name.
fn launchd_subject(inner: &str) -> (String, String, Option<u64>) {
    let (label, bracket) = match inner.strip_suffix(']').and_then(|r| r.rsplit_once(" [")) {
        Some((label, bracket)) => (label, Some(bracket)),
        None => (inner, None),
    };
    let number = bracket.and_then(|b| b.parse::<u64>().ok());
    match split_domain(label) {
        // `pid/PID [NAME]`: the bracket names the process, the domain its PID.
        Some((domain, "")) if domain.starts_with("pid/") => match bracket {
            Some(name) if number.is_none() => (
                label.to_string(),
                name.to_string(),
                domain[4..].parse().ok(),
            ),
            _ => (label.to_string(), "launchd".into(), Some(1)),
        },
        Some((_, "")) => (label.to_string(), "launchd".into(), Some(1)),
        Some((_, name)) => (label.to_string(), service_process(name), number),
        None => (label.to_string(), service_process(label), number),
    }
}

/// The process a launchd service label names: an app's bundle identifier
/// for a UIKit application, else the last part of a domain path.
fn service_process(service: &str) -> String {
    if let Some(rest) = service.strip_prefix("UIKitApplication:") {
        let identifier = rest.split('[').next().unwrap_or(rest);
        return identifier
            .rsplit('.')
            .next()
            .unwrap_or(identifier)
            .to_string();
    }
    let last = service.rsplit('/').next().unwrap_or(service);
    last.to_string()
}

/// Splits `name[pid]`, `name(sender)[pid]` and `name` (no PID).
fn process_part(text: &str) -> Option<(String, Option<String>, Option<u64>)> {
    let valid = |name: &str| {
        !name.is_empty()
            && name.len() <= 128
            && name
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || "._-+ ".contains(c))
    };
    let (head, pid) = match text
        .strip_suffix(']')
        .and_then(|rest| rest.rsplit_once('['))
    {
        Some((head, pid)) => (head, Some(pid.parse::<u64>().ok()?)),
        None => (text, None),
    };
    let (name, sender) = match head.strip_suffix(')').and_then(|rest| rest.split_once('(')) {
        Some((name, sender)) => (name, Some(sender.to_string())),
        None => (head, None),
    };
    valid(name).then(|| (name.to_string(), sender, pid))
}

/// One console line as a structured record. Forms, in order:
/// `SUBSYSTEM|TIME (SERVICE [PID]) <Level>: MESSAGE` and
/// `SUBSYSTEM|TIME <Level>: MESSAGE` (launchd's forwarded records),
/// `[MON DD HH:MM:SS HOST ]PROCESS[(SENDER)][PID] <Level>: MESSAGE` and
/// `PROCESS[PID]: MESSAGE` (syslog), and anything else as the kernel's.
pub(crate) fn parse(raw: &str) -> Line {
    let text: String = raw
        .trim_end_matches(['\r', '\n'])
        .chars()
        .take(LINE_CHARS)
        .collect();
    if let Some((subsystem, rest)) = text.split_once('|') {
        if !subsystem.is_empty() && !subsystem.contains(' ') && subsystem.contains('.') {
            if let Some((head, message)) = rest.split_once(">: ") {
                if let Some((before, name)) = head.rsplit_once(" <") {
                    let sender = subsystem
                        .rsplit('.')
                        .next()
                        .unwrap_or(subsystem)
                        .to_string();
                    let (time, service, process, pid) = match before.split_once(" (") {
                        Some((time, inner)) => {
                            let inner = inner.strip_suffix(')').unwrap_or(inner);
                            let (service, process, pid) = launchd_subject(inner);
                            (time, Some(service), process, pid)
                        }
                        // launchd's own record.
                        None if sender == "launchd" => {
                            (before, None, "launchd".to_string(), Some(1))
                        }
                        None => (before, None, sender.clone(), None),
                    };
                    return Line {
                        time: Some(time.to_string()).filter(|t| !t.is_empty()),
                        process,
                        pid,
                        service,
                        sender: Some(subsystem.to_string()),
                        level: level(name),
                        message: message.to_string(),
                    };
                }
            }
        }
    }
    // Syslog: an optional "Mon DD HH:MM:SS host " prefix, then the process.
    let (time, body) = syslog_time(&text);
    let split = body.split_once(": ");
    if let Some((head, message)) = split {
        let (who, name) = match head.rsplit_once(" <") {
            Some((who, name)) if name.ends_with('>') => (who, level(name.trim_end_matches('>'))),
            _ => (head, None),
        };
        if let Some((process, sender, pid)) = process_part(who) {
            // "AMFI: ..." has no PID and no level: the kernel's own print.
            if pid.is_some() || name.is_some() {
                return Line {
                    time,
                    process,
                    pid,
                    service: None,
                    sender,
                    level: name,
                    message: message.to_string(),
                };
            }
        }
    }
    Line {
        process: "kernel".into(),
        message: text,
        ..Line::default()
    }
}

fn syslog_time(text: &str) -> (Option<String>, &str) {
    let months = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    let parts: Vec<&str> = text.splitn(5, ' ').collect();
    if parts.len() == 5
        && months.contains(&parts[0])
        && parts[1].len() <= 2
        && parts[1].chars().all(|c| c.is_ascii_digit())
        && parts[2].len() == 8
        && parts[2].chars().filter(|c| *c == ':').count() == 2
    {
        return (
            Some(format!("{} {} {}", parts[0], parts[1], parts[2])),
            parts[4],
        );
    }
    (None, text)
}

/// Whether a line passes the filters: process (a name or service that
/// contains it, any case), levels (one of them; a line without a level
/// passes only when "none" is among them) and contains (in the message).
fn wanted(line: &Line, process: &str, levels: &[String], contains: &str) -> bool {
    let process = process.to_ascii_lowercase();
    (process.is_empty()
        || line.process.to_ascii_lowercase().contains(&process)
        || line
            .service
            .as_deref()
            .is_some_and(|s| s.to_ascii_lowercase().contains(&process)))
        && (levels.is_empty()
            || levels
                .iter()
                .any(|l| line.level.as_deref().unwrap_or("none") == l))
        && (contains.is_empty() || line.message.contains(contains))
}

/// Reads complete lines from `offset` (or the last TAIL_BYTES when None)
/// of a console log, at most `max_lines` that pass the filters. Returns the
/// lines with their byte offsets, the offset to read on from, whether that
/// stopped short of the end, and whether a stale offset started over.
pub(crate) fn read(
    file: &Path,
    offset: Option<u64>,
    max_lines: usize,
    filter: (&str, &[String], &str),
) -> Result {
    use std::io::{Read, Seek, SeekFrom};
    let mut handle = File::open(file)?;
    let size = handle.metadata()?.len();
    let reset = offset.is_some_and(|offset| offset > size);
    let mut start = match offset {
        Some(offset) if offset <= size => offset,
        Some(_) => 0,
        None => size.saturating_sub(TAIL_BYTES),
    };
    let mut buffer = Vec::new();
    handle.seek(SeekFrom::Start(start))?;
    handle.take(READ_BYTES).read_to_end(&mut buffer)?;
    // Without an offset the tail starts mid-line; begin at the next one.
    if offset.is_none() && start > 0 {
        let skip = buffer
            .iter()
            .position(|b| *b == b'\n')
            .map_or(buffer.len(), |i| i + 1);
        buffer.drain(..skip);
        start += skip as u64;
    }
    let mut lines = Vec::new();
    let mut position = start;
    let mut more = false;
    for chunk in buffer.split_inclusive(|b| *b == b'\n') {
        if !chunk.ends_with(b"\n") {
            break; // An unfinished line is read again next time.
        }
        if lines.len() >= max_lines {
            more = true;
            break;
        }
        let at = position;
        position += chunk.len() as u64;
        let text = String::from_utf8_lossy(chunk);
        if text.trim().is_empty() {
            continue;
        }
        let line = parse(&text);
        if wanted(&line, filter.0, filter.1, filter.2) {
            lines.push(json!({"offset":at,"time":line.time,"process":line.process,"pid":line.pid,
                "service":line.service,"sender":line.sender,"level":line.level,"message":line.message}));
        }
    }
    more = more || position < size && buffer.len() as u64 >= READ_BYTES;
    Ok(json!({"lines":lines,"next_offset":position,"size":size,"more":more,"reset":reset}))
}

/// The structured log of the configured phone's guest job (running or last).
pub fn syslog(a: &Value) -> Result {
    let job = virtual_phone::guest_job()?;
    let plan = jobs::get(&job)?;
    let file = PathBuf::from(s(&plan["plan"], "cwd")).join("paravirtual/uart.log");
    if !fs::symlink_metadata(&file).is_ok_and(|meta| meta.is_file()) {
        return Err(Error::new(
            "ios_virtual_log_unavailable",
            "The guest job has no console log yet",
        )
        .details(json!({"job_id":job,"file":file})));
    }
    let levels: Vec<String> = a["levels"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|v| v.as_str().map(str::to_string))
        .collect();
    let max_lines = a["max_lines"].as_u64().unwrap_or(500).clamp(1, 5000) as usize;
    let mut reply = read(
        &file,
        a["offset"].as_u64(),
        max_lines,
        (s(a, "process"), &levels, s(a, "contains")),
    )?;
    let running = jobs::TERMINAL.iter().all(|state| plan["state"] != *state);
    reply["job_id"] = json!(job);
    reply["running"] = json!(running);
    Ok(reply)
}

#[cfg(test)]
mod tests {
    use super::*;

    const FIXTURE: &str = include_str!("../../tests/fixtures/ios_guest_syslog.log");

    #[test]
    fn each_form_of_console_line_is_split_into_its_parts() {
        let lines: Vec<Line> = FIXTURE.split_inclusive('\n').map(parse).collect();
        assert_eq!(lines[0].process, "kernel");
        assert_eq!(lines[0].message, "SEP/OS is alive in 2 state");
        assert_eq!(
            (
                lines[1].process.as_str(),
                lines[1].pid,
                lines[1].level.as_deref()
            ),
            ("launchd", Some(1), Some("notice"))
        );
        assert_eq!(lines[1].time.as_deref(), Some("2026-10-28 07:19:51.835753"));
        assert_eq!(
            lines[2],
            Line {
                time: Some("2026-10-28 07:19:52.001200".into()),
                process: "com.apple.example.daemond".into(),
                pid: Some(88),
                service: Some("system/com.apple.example.daemond".into()),
                sender: Some("com.apple.xpc.launchd".into()),
                level: Some("notice".into()),
                message: "Service only ran for 0 seconds. Pushing respawn out by 10 seconds."
                    .into(),
            }
        );
        assert_eq!(
            (
                lines[3].process.as_str(),
                lines[3].pid,
                lines[3].level.as_deref()
            ),
            ("dev.tomacco.SampleApp", Some(412), Some("error"))
        );
        assert_eq!(lines[3].message, "exited due to SIGABRT");
        assert_eq!(
            (lines[4].process.as_str(), lines[4].pid),
            ("SampleApp", Some(412))
        );
        assert_eq!(
            (
                lines[5].process.as_str(),
                lines[5].pid,
                lines[5].level.as_deref()
            ),
            ("SampleApp", Some(412), Some("notice"))
        );
        assert_eq!(
            (
                lines[6].process.as_str(),
                lines[6].sender.as_deref(),
                lines[6].pid,
                lines[6].level.as_deref()
            ),
            (
                "example-agent",
                Some("libExample.dylib"),
                Some(77),
                Some("debug")
            )
        );
        assert_eq!(
            (
                lines[7].time.as_deref(),
                lines[7].process.as_str(),
                lines[7].pid,
                lines[7].level.as_deref()
            ),
            (
                Some("Oct 28 07:19:55"),
                "backboardd",
                Some(64),
                Some("fault")
            )
        );
        assert_eq!(lines[7].message, "display pipeline stalled");
        assert_eq!(
            (
                lines[8].process.as_str(),
                lines[8].pid,
                lines[8].level.as_deref()
            ),
            ("quietd", Some(90), None)
        );
        assert_eq!(
            (lines[9].process.as_str(), lines[9].message.as_str()),
            ("kernel", "AMFI: Booted in a VM")
        );
        assert_eq!(lines[11].time.as_deref(), Some("not a date"));
        assert_eq!(lines[11].process, "launchd");
        // A very long line is cut, never refused.
        assert_eq!(parse(&"x".repeat(5000)).message.len(), LINE_CHARS);
    }

    #[test]
    fn the_launchd_record_forms_of_a_live_phone_name_their_process() {
        let lines: Vec<Line> = FIXTURE.split_inclusive('\n').map(parse).collect();
        let subject = |i: usize| {
            (
                lines[i].process.clone(),
                lines[i].pid,
                lines[i].service.clone().unwrap_or_default(),
            )
        };
        let app = "user/501/UIKitApplication:dev.tomacco.SampleApp[3f2a][rb-legacy]";
        // An app's service in the user domain, with and without its PID.
        assert_eq!(subject(12), ("SampleApp".into(), Some(413), app.into()));
        assert_eq!(subject(13), ("SampleApp".into(), None, app.into()));
        // The domain of one process: the bracket is its name, not a PID.
        assert_eq!(subject(14), ("exampled".into(), Some(89), "pid/89".into()));
        // A service in a process's domain is that service, without a PID.
        assert_eq!(
            subject(15),
            (
                "com.example.Endpoint".into(),
                None,
                "pid/74/com.example.Endpoint".into()
            )
        );
        // A bare domain is launchd's own record about it.
        assert_eq!(subject(16), ("launchd".into(), Some(1), "user/501".into()));
        assert_eq!(subject(17), ("launchd".into(), Some(1), "system".into()));
        // A boot task keeps its name.
        assert_eq!(
            subject(18),
            ("example-boot".into(), None, "example-boot".into())
        );
        assert_eq!(
            (subject(19).0, lines[19].level.as_deref()),
            ("com.example.agent".into(), Some("error"))
        );
        assert_eq!(
            (lines[20].process.as_str(), lines[20].pid),
            ("dyld", Some(1))
        );
        // A record cut by a kernel print in the middle stays the kernel's line.
        assert_eq!(lines[21].process, "kernel");
        assert_eq!(
            lines[21].message,
            "ampled [88]) <Notice>: job state = running"
        );
        assert!(split_domain("users/501").is_none());
        assert!(split_domain("user/x/name").is_none());
    }

    #[test]
    fn a_read_returns_complete_filtered_lines_and_where_to_go_on() {
        let root = tempfile::tempdir().unwrap();
        let file = root.path().join("uart.log");
        fs::write(&file, FIXTURE).unwrap();
        let all = read(&file, Some(0), 100, ("", &[], "")).unwrap();
        let lines = all["lines"].as_array().unwrap();
        // Twenty-two complete lines, one of them empty; the unfinished one waits.
        assert_eq!(lines.len(), 21);
        let finished = FIXTURE.rfind('\n').unwrap() as u64 + 1;
        assert_eq!(all["next_offset"], finished);
        assert_eq!(all["more"], false);
        assert_eq!(
            lines[2]["offset"],
            FIXTURE
                .find("com.apple.xpc.launchd|2026-10-28 07:19:52")
                .unwrap()
        );
        // By process (name or service, any case), level and text.
        let app = read(&file, Some(0), 100, ("sampleapp", &[], "")).unwrap();
        assert_eq!(app["lines"].as_array().unwrap().len(), 5);
        let levels = vec!["fault".to_string(), "error".to_string()];
        let bad = read(&file, Some(0), 100, ("", &levels, "")).unwrap();
        let names: Vec<_> = bad["lines"]
            .as_array()
            .unwrap()
            .iter()
            .map(|l| l["process"].clone())
            .collect();
        assert_eq!(
            names,
            vec![
                json!("dev.tomacco.SampleApp"),
                json!("backboardd"),
                json!("com.example.agent")
            ]
        );
        let none = vec!["none".to_string()];
        let plain = read(&file, Some(0), 100, ("", &none, "")).unwrap();
        assert_eq!(plain["lines"].as_array().unwrap().len(), 5);
        let text = read(&file, Some(0), 100, ("", &[], "respawn")).unwrap();
        assert_eq!(text["lines"].as_array().unwrap().len(), 1);
        // Following: a short page says more, and the rest comes from its offset.
        let page = read(&file, Some(0), 4, ("", &[], "")).unwrap();
        assert_eq!(page["more"], true);
        let rest = read(&file, page["next_offset"].as_u64(), 100, ("", &[], "")).unwrap();
        assert_eq!(rest["lines"].as_array().unwrap().len(), 17);
        // The unfinished line comes once it ends; a stale offset starts over.
        let mut grown = FIXTURE.to_string();
        grown.push_str(" now\n");
        fs::write(&file, &grown).unwrap();
        let tail = read(&file, rest["next_offset"].as_u64(), 100, ("", &[], "")).unwrap();
        assert_eq!(
            tail["lines"][0]["message"],
            "partial line without its end now"
        );
        let stale = read(&file, Some(1 << 30), 100, ("", &[], "")).unwrap();
        assert_eq!(stale["reset"], true);
        assert_eq!(stale["lines"].as_array().unwrap().len(), 22);
    }

    #[test]
    fn a_first_read_looks_back_from_the_end_and_starts_on_a_whole_line() {
        let root = tempfile::tempdir().unwrap();
        let file = root.path().join("uart.log");
        let line = "quietd[90]: unloaded 4 services\n";
        fs::write(&file, line.repeat(20000)).unwrap();
        let tail = read(&file, None, 10000, ("", &[], "")).unwrap();
        let lines = tail["lines"].as_array().unwrap();
        assert!(lines.len() <= (TAIL_BYTES as usize) / line.len());
        assert_eq!(lines[0]["offset"].as_u64().unwrap() % line.len() as u64, 0);
        assert_eq!(tail["next_offset"], (line.len() * 20000) as u64);
    }
}
