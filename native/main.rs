mod android_debug;
mod api;
mod apple;
mod apple_dev;
mod automation;
mod catalog;
mod core;
mod dap;
mod debugger;
mod desktop;
mod devices;
mod files;
mod instrumentation;
mod ios_emulator;
mod iphone_setup;
mod jdwp;
mod jobs;
mod mcp;
mod play_store;
mod preview;
mod product;
mod profiling;
mod projects;
mod releases;
mod remote;
mod runtime;
mod setup;
mod swift;
mod toolchain;
mod update;
mod workspaces;
use core::*;
use serde_json::{Value, json};
use std::io::{Read, Write};
fn main() {
    let code = match run() {
        Ok(code) => code,
        Err(error) => {
            let _ = writeln!(std::io::stdout(), "{}", api::envelope(Err(error)));
            1
        }
    };
    std::process::exit(code);
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
  tomaccoctl call OPERATION [JSON | @FILE | @-]
                    Run one operation; arguments inline, from a file or from
                    standard input. Prints one JSON envelope (ok, result or
                    error) and exits 0 on success, 1 on failure.
  tomaccoctl schema  Print every operation with its argument schema (JSON).
  tomaccoctl setup [--yes]
                    Check this machine in plain text and offer to install the
                    Android SDK and connect your coding agents.
  tomaccoctl doctor  Check the toolchains and devices this machine can use.
  tomaccoctl mcp     Serve the same operations to a coding agent over stdio
                    (Model Context Protocol; no network port).
  tomaccoctl --version, -V
  tomaccoctl --help, -h

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

fn run() -> Result<i32> {
    let args: Vec<_> = std::env::args().skip(1).collect();
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
        "schema" => Ok(json!({"version":VERSION,"operations":api::manifest()})),
        "doctor" => api::dispatch("doctor", &json!({})),
        "call" => {
            let op = args
                .get(1)
                .ok_or_else(|| Error::new("invalid_request", "Missing operation name"))?;
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
            api::dispatch(op, &arguments)
        }
        _ => Err(
            Error::new("invalid_request", format!("Unknown command: {}", args[0]))
                .remedy("Run tomaccoctl --help for the commands"),
        ),
    };
    let status = if result.is_ok() { 0 } else { 1 };
    // A reader that stopped early (`tomaccoctl schema | head`) is no reason to panic.
    let _ = writeln!(std::io::stdout(), "{}", api::envelope(result));
    Ok(status)
}

#[cfg(test)]
mod tests {
    use super::*;
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
