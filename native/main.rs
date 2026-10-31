mod accessibility;
mod android_debug;
mod android_device;
mod android_package;
mod api;
mod apple;
mod apple_dev;
mod automation;
mod capabilities;
mod catalog;
mod completions;
mod contact_sheet;
mod core;
mod crashes;
mod dap;
mod debugger;
mod dependencies;
mod describe;
mod desktop;
mod devices;
mod diagnostics;
mod download;
mod files;
mod gradle_model;
mod instrumentation;
mod ios_emulator;
mod iphone_setup;
mod javascript;
mod jdwp;
mod jobs;
mod mcp;
mod play_store;
mod preview;
mod product;
mod profiling;
mod projects;
mod recorder;
mod redact;
mod releases;
mod remote;
mod runtime;
mod session_log;
mod setup;
mod storage;
mod swift;
mod test_results;
mod toolchain;
mod update;
mod visual;
mod watch;
mod workspaces;
use core::*;
use serde_json::{Value, json};
use std::io::{Read, Write};
fn main() {
    let (args, options) = Options::parse(std::env::args().skip(1).collect());
    let code = match run(&args, &options) {
        Ok(code) => code,
        Err(error) => {
            print_envelope(Err(error), &options);
            1
        }
    };
    std::process::exit(code);
}
/// The flags of the public commands, wherever they stand on the line. The
/// internal `_worker` commands, and `setup`, `update` and `uninstall`, which
/// read their own, keep theirs.
#[derive(Debug, Default, PartialEq)]
struct Options {
    flags: Vec<&'static str>,
    unknown: Vec<String>,
}
impl Options {
    const FLAGS: &[&str] = &["--names", "--pretty", "--compact", "--wait", "--dry-run"];
    /// Commands that parse their own arguments.
    const OWN: &[&str] = &["setup", "update", "uninstall"];
    /// What every command that prints an envelope takes.
    const OUTPUT: &[&str] = &["--pretty", "--compact"];
    fn parse(args: Vec<String>) -> (Vec<String>, Options) {
        let mut options = Options::default();
        if args
            .first()
            .is_some_and(|a| a.starts_with('_') || Self::OWN.contains(&a.as_str()))
        {
            return (args, options);
        }
        let mut rest = Vec::new();
        for arg in args {
            if let Some(flag) = Self::FLAGS.iter().find(|f| **f == arg) {
                options.flags.push(flag);
            } else if arg.starts_with("--") && arg != "--help" && arg != "--version" {
                options.unknown.push(arg);
            } else {
                rest.push(arg);
            }
        }
        (rest, options)
    }
    fn given(&self, flag: &str) -> bool {
        self.flags.contains(&flag)
    }
    /// Refuses a flag this command does not take, naming the closest one.
    fn check(&self, allowed: &[&str]) -> Result<()> {
        let allowed: Vec<&str> = allowed.iter().chain(Self::OUTPUT).copied().collect();
        if self.given("--pretty") && self.given("--compact") {
            return Err(Error::new(
                "invalid_request",
                "--pretty and --compact exclude each other",
            ));
        }
        let Some(flag) = self.unknown.first().map(String::as_str).or(self
            .flags
            .iter()
            .copied()
            .find(|f| !allowed.contains(f)))
        else {
            return Ok(());
        };
        let remedy = match closest(flag, allowed.iter().copied()) {
            Some(near) => format!("Did you mean {near}?"),
            None => "Run tomaccoctl --help for the commands and their flags".to_string(),
        };
        Err(Error::new("invalid_request", format!("Unknown option: {flag}")).remedy(remedy))
    }
    /// Indented JSON for a person: asked for with --pretty, or by default
    /// when standard output is a terminal. Anything else, a pipe or a file,
    /// gets one line, byte for byte what scripts have always read.
    fn pretty(&self, terminal: bool) -> bool {
        self.given("--pretty") || (terminal && !self.given("--compact"))
    }
}
fn render(value: &Value, pretty: bool) -> String {
    if pretty {
        serde_json::to_string_pretty(value).expect("JSON values serialize")
    } else {
        value.to_string()
    }
}
fn print_envelope(result: Result, options: &Options) {
    use std::io::IsTerminal;
    let pretty = options.pretty(std::io::stdout().is_terminal());
    // A reader that stopped early (`tomaccoctl schema | head`) is no reason to panic.
    let _ = writeln!(
        std::io::stdout(),
        "{}",
        render(&api::envelope(result), pretty)
    );
}
/// What a public command prints: one JSON envelope, or plain text.
#[derive(Debug)]
enum Reply {
    Envelope(Result),
    Text(String),
}
/// `tomaccoctl schema`: every operation, one operation by name, or with
/// `--names` the names alone, one per line, for scripts and completion.
fn schema(args: &[String], options: &Options) -> Result<Reply> {
    options.check(&["--names"])?;
    if args.len() > 1 || (options.given("--names") && !args.is_empty()) {
        return Err(Error::new(
            "invalid_request",
            "Expected one operation name, or --names",
        ));
    }
    if options.given("--names") {
        return Ok(Reply::Text(
            api::names().map(|name| format!("{name}\n")).collect(),
        ));
    }
    Ok(Reply::Envelope(match args.first() {
        Some(name) => api::operation(name).cloned(),
        None => Ok(json!({"version":VERSION,"operations":api::manifest()})),
    }))
}
/// The id of a job record, as an operation that starts work returns it.
fn job_id(result: &Value) -> Option<&str> {
    let id = result["id"].as_str()?;
    (id.len() == 32
        && id.bytes().all(|c| c.is_ascii_hexdigit())
        && result["state"].is_string()
        && result["log_path"].is_string())
    .then_some(id)
}
/// `call --wait`: follows a job to its end, writing its log to `log` as it
/// grows. The job's last record is the result when it succeeded; any other
/// end is an error that carries the record. Interrupting the CLI leaves the
/// durable job running.
fn follow(id: &str, call: impl Fn(&str, &Value) -> Result, log: &mut impl Write) -> Result {
    let mut offset = 0;
    loop {
        let job = call("job.wait", &json!({"job_id":id,"seconds":2}))?;
        // The log is read after the wait, so a job that has just ended has
        // all of its text written before its result.
        loop {
            let read = call("job.logs", &json!({"job_id":id,"offset":offset}))?;
            let text = s(&read, "text");
            let _ = log.write_all(text.as_bytes());
            offset = n(&read, "next_offset").max(offset);
            if text.is_empty() || read["eof"] == true {
                break;
            }
        }
        let _ = log.flush();
        let state = s(&job, "state");
        if !jobs::TERMINAL.contains(&state) {
            continue;
        }
        if state == "succeeded" {
            return Ok(job);
        }
        return Err(Error::new(
            &format!("job_{state}"),
            format!("Job {id} ({}) ended {state}", s(&job, "label")),
        )
        .remedy(format!(
            "Read the log above, or tomaccoctl call job.logs '{{\"job_id\":\"{id}\"}}'"
        ))
        .details(json!({"job":job})));
    }
}
/// The arguments of `tomaccoctl call`: inline JSON, `@FILE`, or `@-` for
/// standard input. Callers pass secrets' paths and whole source files; argv
/// is world-readable in /proc and limited to 128 KiB per argument, so the
/// desktop sends its arguments through standard input.
fn call_arguments(text: &str, stdin: impl Read) -> Result<Value> {
    let mut bytes = Vec::new();
    if text == "@-" {
        stdin.take(JSON_LIMIT + 1).read_to_end(&mut bytes)?;
    } else if let Some(file) = text.strip_prefix('@') {
        std::fs::File::open(path(file, false, true)?)?
            .take(JSON_LIMIT + 1)
            .read_to_end(&mut bytes)?;
    } else {
        bytes = text.as_bytes().to_vec();
    }
    if bytes.len() as u64 > JSON_LIMIT {
        return Err(Error::new("input_too_large", "Arguments exceed 2 MiB"));
    }
    Ok(serde_json::from_slice(&bytes)?)
}
/// The command-line help: what tomaccoctl is, its commands and where the
/// operations are described.
fn help() -> String {
    format!(
        "tomaccoctl {VERSION} - the Tomacco control plane

Usage:
  tomaccoctl call OPERATION [JSON | @FILE | @-] [--wait]
                    Run one operation; arguments inline, from a file or from
                    standard input. Prints one JSON envelope (ok, result or
                    error) and exits 0 on success, 1 on failure. With --wait,
                    an operation that starts a job is followed until the job
                    ends: its log goes to standard error, the envelope holds
                    the final job, and only a succeeded job exits 0.
  tomaccoctl schema [OPERATION | --names]
                    Print every operation with its argument schema (JSON),
                    only the named one, or with --names just the names, one
                    per line.
  tomaccoctl help [OPERATION]
                    Without an operation, print this help. With one, describe
                    it for a person: what it does, its arguments with their
                    types, choices and defaults, and an example call. The
                    same as tomaccoctl call OPERATION --help.
  tomaccoctl setup [--yes]
                    Check this machine in plain text and offer to install the
                    Android SDK and connect your coding agents.
  tomaccoctl doctor  Check the toolchains and devices this machine can use.
  tomaccoctl report [FILE.tar.gz]
                    Write a bug-report bundle (version, host, doctor,
                    settings, recent failed jobs and their log ends) with
                    secrets masked. Nothing is sent; read it before sharing.
  tomaccoctl mcp     Serve the same operations to a coding agent over stdio
                    (Model Context Protocol; no network port).
  tomaccoctl replay FILE [--dry-run]
                    Call again, in order, the operations of a session log
                    that changed something, waiting for each job; stops at
                    the first failure. With --dry-run, list them only. Set
                    TOMACCO_SESSION_LOG to a file to log the operations of
                    call and mcp, with recognised secrets masked.
  tomaccoctl completions bash|zsh|fish
                    Print a completion script for commands, flags and
                    operation names, e.g. source <(tomaccoctl completions bash)
  tomaccoctl update [--check]
                    Check for a new release and install it (update.check,
                    then update.apply); --check only checks.
  tomaccoctl uninstall
                    Remove this installation; keeps your data and the files
                    you changed, and prints how to undo it.
  tomaccoctl --version, -V
  tomaccoctl --help, -h

Options of call, schema, doctor, report and replay:
  --pretty          Indent the JSON. This is the default when standard output
                    is a terminal; a pipe or a file gets one line.
  --compact         One line of JSON, even at a terminal.

Examples:
  tomaccoctl call workspace.list
  tomaccoctl call ios.virtual.status
  echo '{{\"job_id\":\"ID\",\"seconds\":60}}' | tomaccoctl call job.wait @-

The desktop app, this CLI and the MCP server share one typed operation
schema; long work runs as durable jobs (job.wait, job.cancel, job.list).
Data lives in ~/.local/share/tomacco (TOMACCO_HOME overrides it).
Documentation: docs/agents.md. License: GPL-2.0-or-later."
    )
}
/// `tomaccoctl help [OPERATION]`, and `call OPERATION --help`: the
/// command-line help, or one operation described for a person.
fn help_command(args: &[String], options: &Options) -> Result<String> {
    options.check(&[])?;
    match args {
        [] => Ok(help() + "\n"),
        [name] => describe::operation(name),
        _ => Err(Error::new("invalid_request", "Expected one operation name")),
    }
}

fn run(args: &[String], options: &Options) -> Result<i32> {
    match args.first().map(String::as_str) {
        Some("--version") | Some("-V") => {
            println!(
                "{} {} ({})",
                product::CLI_NAME,
                update::current_version(),
                product::PRODUCT_NAME
            );
            return Ok(0);
        }
        Some("--help") | Some("-h") | None => {
            println!("{}", help());
            return Ok(0);
        }
        Some("setup") => return setup::run(&args[1..]),
        Some("update") => return update::update_command(&args[1..]),
        Some("uninstall") => return update::uninstall_command(&args[1..]),
        Some("mcp") => {
            mcp::serve(std::io::stdin().lock(), std::io::stdout())?;
            return Ok(0);
        }
        Some("_worker") => {
            jobs::worker(
                args.get(1)
                    .ok_or_else(|| Error::new("invalid_request", "Missing job ID"))?,
            )?;
            return Ok(0);
        }
        Some("_ios_research") => {
            ios_emulator::research::worker(
                args.get(1)
                    .ok_or_else(|| Error::new("invalid_request", "Missing research request"))?,
            )?;
            return Ok(0);
        }
        Some("_ios_pmp") => {
            ios_emulator::pmp::worker(
                args.get(1)
                    .ok_or_else(|| Error::new("invalid_request", "Missing PMP request"))?,
            )?;
            return Ok(0);
        }
        Some("_dependency_updates") => {
            dependencies::worker(
                args.get(1)
                    .ok_or_else(|| Error::new("invalid_request", "Missing dependency request"))?,
            )?;
            return Ok(0);
        }
        Some("_serve_then") => {
            javascript::serve_worker(
                args.get(1)
                    .ok_or_else(|| Error::new("invalid_request", "Missing server request"))?,
            )?;
            return Ok(0);
        }
        Some("_android_each") => {
            android_device::each_worker(
                args.get(1)
                    .ok_or_else(|| Error::new("invalid_request", "Missing device request"))?,
            )?;
            return Ok(0);
        }
        Some("_play") => {
            play_store::worker(
                args.get(1)
                    .ok_or_else(|| Error::new("invalid_request", "Missing store specification"))?,
            )?;
            return Ok(0);
        }
        Some("_release") => {
            releases::worker(
                args.get(1).ok_or_else(|| {
                    Error::new("invalid_request", "Missing release specification")
                })?,
            )?;
            return Ok(0);
        }
        Some("_update") => {
            update::worker(
                args.get(1)
                    .ok_or_else(|| Error::new("invalid_request", "Missing update request"))?,
            )?;
            return Ok(0);
        }
        Some("_runtime_worker") => {
            runtime::worker(
                args.get(1)
                    .ok_or_else(|| Error::new("invalid_request", "Missing session ID"))?,
            )?;
            return Ok(0);
        }
        Some("_iphone_setup") => {
            iphone_setup::worker(
                args.get(1)
                    .ok_or_else(|| Error::new("invalid_request", "Missing iPhone setup request"))?,
            )?;
            return Ok(0);
        }
        Some("_ios_emulator_probe") => {
            ios_emulator::worker(args.get(1).ok_or_else(|| {
                Error::new("invalid_request", "Missing emulator probe specification")
            })?)?;
            return Ok(0);
        }
        Some("_iphone_launch") => {
            let request: Value =
                serde_json::from_str(args.get(1).ok_or_else(|| {
                    Error::new("invalid_request", "Missing iPhone launch request")
                })?)?;
            println!("{}", iphone_setup::launch_installed(&request)?);
            return Ok(0);
        }
        Some("_debug_worker") => {
            debugger::worker(
                args.get(1)
                    .ok_or_else(|| Error::new("invalid_request", "Missing session ID"))?,
            )?;
            return Ok(0);
        }
        Some("_watch") => {
            watch::worker(
                args.get(1)
                    .ok_or_else(|| Error::new("invalid_request", "Missing watch specification"))?,
            )?;
            return Ok(0);
        }
        Some("_automation") => {
            automation::worker(
                args.get(1).ok_or_else(|| {
                    Error::new("invalid_request", "Missing scenario specification")
                })?,
            )?;
            return Ok(0);
        }
        _ => (),
    }
    let result = match args[0].as_str() {
        "schema" => match schema(&args[1..], options)? {
            Reply::Envelope(result) => result,
            Reply::Text(text) => {
                let _ = write!(std::io::stdout(), "{text}");
                return Ok(0);
            }
        },
        "completions" => {
            options.check(&[])?;
            let [shell] = &args[1..] else {
                return Err(Error::new(
                    "invalid_request",
                    format!("Name one shell: {}", completions::SHELLS.join(", ")),
                ));
            };
            let _ = write!(std::io::stdout(), "{}", completions::script(shell)?);
            return Ok(0);
        }
        "help" => {
            let _ = write!(std::io::stdout(), "{}", help_command(&args[1..], options)?);
            return Ok(0);
        }
        "doctor" => options
            .check(&[])
            .and_then(|()| api::dispatch("doctor", &json!({}))),
        "report" => options.check(&[]).and_then(|()| match &args[1..] {
            [] => api::dispatch("diagnostics.bundle", &json!({})),
            [path] => {
                // A relative file is named from the current directory.
                let path = std::env::current_dir()?.join(path);
                api::dispatch("diagnostics.bundle", &json!({ "path": path }))
            }
            _ => Err(Error::new(
                "invalid_request",
                "Name at most one output file",
            )),
        }),
        "call" => {
            options.check(&["--wait"])?;
            let op = args
                .get(1)
                .ok_or_else(|| Error::new("invalid_request", "Missing operation name"))?;
            if args[2..].iter().any(|arg| arg == "--help" || arg == "-h") {
                let _ = write!(std::io::stdout(), "{}", help_command(&args[1..2], options)?);
                return Ok(0);
            }
            if args.len() > 3 {
                return Err(Error::new(
                    "invalid_request",
                    "Expected one JSON argument or @file",
                ));
            }
            let arguments = call_arguments(
                args.get(2).map(String::as_str).unwrap_or("{}"),
                std::io::stdin().lock(),
            )?;
            match session_log::call(op, &arguments) {
                Ok(result) if options.given("--wait") => match job_id(&result) {
                    Some(id) => follow(id, api::dispatch, &mut std::io::stderr()),
                    None => Ok(result),
                },
                result => result,
            }
        }
        "replay" => options
            .check(&["--dry-run"])
            .and_then(|()| match &args[1..] {
                [file] => session_log::replay(
                    &std::fs::read_to_string(path(file, false, true)?)?,
                    options.given("--dry-run"),
                    api::dispatch,
                ),
                _ => Err(Error::new("invalid_request", "Name one session log file")),
            }),
        _ => Err(
            Error::new("invalid_request", format!("Unknown command: {}", args[0]))
                .remedy("Run tomaccoctl --help for the commands"),
        ),
    };
    let status = if result.is_ok() { 0 } else { 1 };
    print_envelope(result, options);
    Ok(status)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn line(args: &[&str]) -> (Vec<String>, Options) {
        Options::parse(args.iter().map(|a| a.to_string()).collect())
    }
    #[test]
    fn flags_are_taken_from_anywhere_but_internal_commands_keep_theirs() {
        let (args, options) = line(&["--names", "schema"]);
        assert_eq!(args, ["schema"]);
        assert!(options.given("--names"));
        let (args, options) = line(&["schema", "--nmaes"]);
        assert_eq!(args, ["schema"]);
        let error = options.check(&["--names"]).unwrap_err();
        assert_eq!(error.message, "Unknown option: --nmaes");
        assert_eq!(error.remedy, "Did you mean --names?");
        // A flag that exists, given to a command that does not take it.
        let (_, options) = line(&["call", "doctor", "--names"]);
        assert_eq!(
            options.check(&[]).unwrap_err().message,
            "Unknown option: --names"
        );
        let (args, options) = line(&["_worker", "--names"]);
        assert_eq!(args, ["_worker", "--names"]);
        assert_eq!(options, Options::default());
        let (args, _) = line(&["setup", "--yes"]);
        assert_eq!(args, ["setup", "--yes"]);
        let (args, _) = line(&["update", "--check"]);
        assert_eq!(args, ["update", "--check"]);
    }
    #[test]
    fn json_is_indented_at_a_terminal_or_when_asked_and_one_line_otherwise() {
        let value = api::envelope(Ok(json!({"a":[1,2]})));
        let (_, plain) = line(&["call", "doctor"]);
        // Through a pipe the output stays exactly what scripts read before.
        assert!(!plain.pretty(false));
        assert_eq!(
            render(&value, plain.pretty(false)),
            r#"{"ok":true,"result":{"a":[1,2]}}"#
        );
        assert!(plain.pretty(true));
        assert_eq!(
            render(&value, true),
            "{\n  \"ok\": true,\n  \"result\": {\n    \"a\": [\n      1,\n      2\n    ]\n  }\n}"
        );
        let (_, pretty) = line(&["call", "doctor", "--pretty"]);
        assert!(pretty.pretty(false));
        let (_, compact) = line(&["--compact", "schema"]);
        assert!(!compact.pretty(true));
        assert!(compact.check(&[]).is_ok());
        let (_, both) = line(&["schema", "--pretty", "--compact"]);
        assert!(both.check(&[]).is_err());
    }
    /// A job that runs for two waits, writing its log in pieces, then ends `end`.
    fn fake_job(end: &'static str) -> impl Fn(&str, &Value) -> Result {
        let waits = std::cell::Cell::new(0);
        let log = "building\nlinking\ndone\n";
        move |operation, arguments| {
            assert_eq!(arguments["job_id"], "0123456789abcdef0123456789abcdef");
            match operation {
                "job.wait" => {
                    waits.set(waits.get() + 1);
                    let state = if waits.get() < 3 { "running" } else { end };
                    Ok(json!({"id":arguments["job_id"],"label":"Build","state":state}))
                }
                "job.logs" => {
                    // Ten bytes more of the log on each wait, five at a time.
                    let offset = n(arguments, "offset") as usize;
                    let written = (waits.get() * 10).min(log.len());
                    let end = (offset + 5).min(written);
                    Ok(json!({"text":&log[offset..end],"next_offset":end,"eof":end == written}))
                }
                _ => panic!("unexpected {operation}"),
            }
        }
    }
    #[test]
    fn wait_follows_a_job_to_its_end_and_streams_its_log() {
        let id = "0123456789abcdef0123456789abcdef";
        let mut log = Vec::new();
        let job = follow(id, fake_job("succeeded"), &mut log).unwrap();
        assert_eq!(job["state"], "succeeded");
        assert_eq!(String::from_utf8(log).unwrap(), "building\nlinking\ndone\n");
        let mut log = Vec::new();
        let error = follow(id, fake_job("failed"), &mut log).unwrap_err();
        assert_eq!(error.code, "job_failed");
        assert_eq!(error.details.unwrap()["job"]["state"], "failed");
        assert_eq!(log.len(), 22);
        // Only a job record is followed.
        let record = json!({"id":id,"state":"queued","log_path":"/tmp/x.log"});
        assert_eq!(job_id(&record), Some(id));
        assert_eq!(job_id(&json!({"id":id,"state":"queued"})), None);
        assert_eq!(
            job_id(&json!({"id":"short","state":"queued","log_path":"/x"})),
            None
        );
        let (_, options) = line(&["call", "project.run", "{}", "--wait"]);
        assert!(options.check(&["--wait"]).is_ok());
        let (_, options) = line(&["schema", "--wait"]);
        assert!(options.check(&["--names"]).is_err());
    }
    #[test]
    fn schema_prints_every_operation_one_operation_or_the_names() {
        let reply = |args: &[&str]| {
            let (args, options) = line(args);
            schema(&args[1..], &options)
        };
        let Reply::Envelope(Ok(all)) = reply(&["schema"]).unwrap() else {
            panic!("the whole schema is JSON")
        };
        assert_eq!(&all["operations"], api::manifest());
        let Reply::Envelope(Ok(one)) = reply(&["schema", "job.wait"]).unwrap() else {
            panic!("one operation is JSON")
        };
        assert_eq!(one["name"], "job.wait");
        assert!(one["inputSchema"]["properties"]["seconds"].is_object());
        let Reply::Envelope(Err(error)) = reply(&["schema", "job.wiat"]).unwrap() else {
            panic!("an unknown operation is an error")
        };
        assert!(error.remedy.starts_with("Did you mean job.wait?"));
        let Reply::Text(names) = reply(&["schema", "--names"]).unwrap() else {
            panic!("names are plain text")
        };
        let names: Vec<&str> = names.lines().collect();
        assert_eq!(names.len(), api::manifest().as_array().unwrap().len());
        assert_eq!(names[0], "doctor");
        assert!(names.contains(&"ios.virtual.start"));
        assert!(reply(&["schema", "job.wait", "job.get"]).is_err());
        assert!(reply(&["schema", "job.wait", "--names"]).is_err());
    }
    /// docs/tomaccoctl.1 is written by hand; it names every command and
    /// flag the help names, and no other flag.
    #[test]
    fn help_and_manual_page_name_the_same_commands_and_flags() {
        let manual = include_str!("../docs/tomaccoctl.1");
        let help = help();
        let commands: Vec<&str> = help
            .lines()
            .filter_map(|line| line.strip_prefix("  tomaccoctl "))
            .filter_map(|rest| rest.split_whitespace().next())
            .filter(|word| !word.starts_with('-'))
            .collect();
        assert!(commands.len() >= 9, "{commands:?}");
        for (name, _) in completions::COMMANDS {
            assert!(commands.contains(name), "the help lacks {name}");
        }
        for command in &commands {
            assert!(
                manual.contains(&format!(".B tomaccoctl {command}\n")),
                "SYNOPSIS lacks {command}"
            );
            assert!(
                manual.lines().collect::<Vec<_>>().windows(2).any(|pair| {
                    let words: Vec<&str> = pair[1].split_whitespace().collect();
                    pair[0] == ".TP"
                        && words.len() >= 2
                        && matches!(words[0], ".B" | ".BR" | ".BI")
                        && words[1].trim_matches('"') == *command
                }),
                "COMMANDS lacks {command}"
            );
        }
        let flag = regex::Regex::new(r"(?:^|[^\\w-])(--[a-z]+(?:-[a-z]+)*|-[a-zA-Z])\b").unwrap();
        let in_help: std::collections::BTreeSet<&str> = flag
            .captures_iter(&help)
            .map(|c| c.get(1).unwrap().as_str())
            .filter(|f| f.starts_with("--") || matches!(*f, "-h" | "-V"))
            .collect();
        let roff = regex::Regex::new(r"(\\-\\-[a-z]+(?:\\-[a-z]+)*|\\-[a-zA-Z])\b").unwrap();
        let in_manual: std::collections::BTreeSet<String> = roff
            .captures_iter(manual)
            .map(|c| c.get(1).unwrap().as_str().replace("\\-", "-"))
            .collect();
        let in_help: std::collections::BTreeSet<String> = in_help
            .into_iter()
            .chain(Options::FLAGS.iter().copied())
            .map(String::from)
            .collect();
        assert_eq!(in_manual, in_help);
    }
    #[test]
    fn help_describes_the_commands_or_one_operation() {
        let help = |words: &[&str]| {
            let (args, options) = line(words);
            help_command(&args[1..], &options)
        };
        assert!(
            help(&["help"])
                .unwrap()
                .contains("tomaccoctl help [OPERATION]")
        );
        let text = help(&["help", "job.wait"]).unwrap();
        assert!(text.starts_with("job.wait - Wait for job"));
        assert!(text.contains("tomaccoctl call job.wait '"));
        let error = help(&["help", "job.wiat"]).unwrap_err();
        assert_eq!(error.code, "operation_not_found");
        assert!(help(&["help", "job.wait", "job.get"]).is_err());
        assert_eq!(
            help(&["help", "job.wait", "--names"]).unwrap_err().message,
            "Unknown option: --names"
        );
        // `call OPERATION --help` keeps --help for the operation.
        let (args, options) = line(&["call", "job.wait", "--help"]);
        assert_eq!(args, ["call", "job.wait", "--help"]);
        assert!(options.check(&["--wait"]).is_ok());
    }
    #[test]
    fn call_arguments_come_inline_from_a_file_or_from_standard_input() {
        assert_eq!(
            call_arguments(r#"{"a":1}"#, &b""[..]).unwrap(),
            json!({"a":1})
        );
        // A source file far above the 128 KiB argv limit, through stdin.
        let text = "x".repeat(1024 * 1024);
        let input = json!({"text":text}).to_string();
        assert_eq!(
            call_arguments("@-", input.as_bytes()).unwrap()["text"]
                .as_str()
                .unwrap()
                .len(),
            1024 * 1024
        );
        let oversized = vec![b' '; JSON_LIMIT as usize + 1];
        assert_eq!(
            call_arguments("@-", &oversized[..]).unwrap_err().code,
            "input_too_large"
        );
        assert!(call_arguments("@-", &b"not json"[..]).is_err());
    }
}
