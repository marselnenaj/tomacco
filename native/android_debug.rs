//! Native Android attach through the installed NDK, run-as and an ADB-only socket.
use crate::{core::*, devices, toolchain as tc};
use serde_json::{Value, json};
use std::{
    fs::File,
    io::Read,
    os::unix::process::CommandExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};

pub fn elf_arch(file: &Path) -> Result<&'static str> {
    let mut header = [0; 20];
    File::open(file)?.read_exact(&mut header).map_err(|_| {
        Error::new(
            "invalid_native_binary",
            "Select an unstripped Android ELF library or executable",
        )
    })?;
    if &header[..4] != b"\x7fELF" || header[5] != 1 || header[6] != 1 {
        return Err(Error::new(
            "invalid_native_binary",
            "Expected a little-endian Android ELF binary",
        ));
    }
    match (header[4], u16::from_le_bytes([header[18], header[19]])) {
        (2, 62) => Ok("x86_64"),
        (1, 3) => Ok("i386"),
        (2, 183) => Ok("aarch64"),
        (1, 40) => Ok("arm"),
        (2, 243) => Ok("riscv64"),
        _ => Err(Error::new(
            "unsupported_native_arch",
            "The ELF architecture is not supported by the Android NDK debugger",
        )),
    }
}

pub fn lldb_quote(text: &str) -> Result<String> {
    if text.chars().any(char::is_control) {
        return Err(Error::new(
            "invalid_argument",
            "Debugger paths cannot contain control characters",
        ));
    }
    Ok(format!(
        "\"{}\"",
        text.replace('\\', "\\\\").replace('"', "\\\"")
    ))
}

pub fn validate(a: &Value) -> Result<()> {
    identifier(s(a, "device"), "Android serial", None)?;
    identifier(
        s(a, "bundle_id"),
        "application ID",
        Some(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+"),
    )?;
    let program = path(s(a, "program"), false, true)?;
    elf_arch(&program)?;
    lldb_quote(&program.to_string_lossy())?;
    if let Some(paths) = a["symbol_paths"].as_array() {
        for p in paths {
            let dir = path(p.as_str().unwrap_or(""), true, true)?;
            lldb_quote(&dir.to_string_lossy())?;
        }
    }
    if a.get("port").is_some() || a["args"].as_array().is_some_and(|v| !v.is_empty()) {
        return Err(Error::new(
            "invalid_argument",
            "Android native debugging selects its own ADB port and attaches to an app process; args and port do not apply",
        ));
    }
    Ok(())
}

fn shell(device: &str, args: &[String]) -> Result {
    tc::run(&devices::shell(device, args)?, 10, None, true)
}

fn run_as(device: &str, bundle: &str, args: &[String]) -> Result {
    let mut command = argv(&["run-as", bundle]);
    command.extend_from_slice(args);
    shell(device, &command)
}

fn server_path(arch: &str) -> Result<PathBuf> {
    let ndk = tc::android_ndk()?.ok_or_else(|| {
        Error::new(
            "ndk_missing",
            "Configure an Android NDK using settings.configure android_ndk",
        )
    })?;
    let mut servers = glob_paths(&format!(
        "{}/toolchains/llvm/prebuilt/linux-*/lib*/clang/*/lib/linux/{arch}/lldb-server",
        glob::Pattern::escape(&ndk.to_string_lossy())
    ))?;
    servers.retain(|p| p.is_file());
    servers.sort();
    let server = servers.pop().ok_or_else(|| {
        Error::new(
            "lldb_server_missing",
            format!("The configured NDK has no {arch} lldb-server"),
        )
    })?;
    if elf_arch(&server)? != arch {
        return Err(Error::new(
            "lldb_server_arch",
            "NDK lldb-server architecture does not match the target",
        ));
    }
    Ok(server)
}

fn command(device: &str, args: &[String], log: &Path) -> Result<Command> {
    let mut adb = devices::adb(device)?;
    adb.extend(["shell".into(), "-T".into(), shell_join(args)]);
    let output = private_file(log, false)?;
    let mut command = Command::new(&adb[0]);
    command
        .args(&adb[1..])
        .envs(tc::environment()?)
        .stdin(Stdio::null())
        .stdout(Stdio::from(output.try_clone()?))
        .stderr(Stdio::from(output))
        .process_group(0);
    tc::parent_bound(&mut command);
    Ok(command)
}

fn bounded_wait(child: &mut Child, seconds: u64) -> Result<()> {
    let end = Instant::now() + Duration::from_secs(seconds);
    loop {
        if let Some(status) = child.try_wait()? {
            return if status.success() {
                Ok(())
            } else {
                Err(Error::new(
                    "native_debug_setup",
                    "Device debugger transfer failed; inspect native-transfer.log",
                ))
            };
        }
        if Instant::now() >= end {
            tc::stop(child);
            return Err(Error::new(
                "native_debug_timeout",
                "Timed out transferring the NDK debugger",
            ));
        }
        thread::sleep(Duration::from_millis(30));
    }
}

pub struct AndroidNative {
    dir: PathBuf,
    record: Value,
    child: Option<Child>,
    pub dap_spec: Value,
}

impl AndroidNative {
    pub fn start(spec: &Value, dir: &Path) -> Result<Self> {
        validate(spec)?;
        let device = s(spec, "device");
        let bundle = s(spec, "bundle_id");
        let id = identifier(s(spec, "session"), "debug session", Some("[a-f0-9]{32}"))?;
        // run-as is the actual Android debuggability/ownership gate. No root or
        // device-wide security configuration is requested or modified.
        let identity = run_as(device, bundle, &argv(&["id", "-u"]))
            .map_err(|e| Error::new("app_not_debuggable", "Android refused run-as for this app; install a debuggable build for the current primary user").details(e.value()))?;
        let uid = s(&identity, "stdout").trim().parse::<u32>().map_err(|_| {
            Error::new(
                "native_debug_identity",
                "run-as did not return an application UID",
            )
        })?;
        if uid < 10000 {
            return Err(Error::new(
                "native_debug_identity",
                "Expected an unprivileged Android application UID",
            ));
        }
        let find = || -> Result<Vec<u32>> {
            let value = tc::run(
                &devices::shell(device, &argv(&["pidof", bundle]))?,
                5,
                None,
                false,
            )?;
            Ok(s(&value, "stdout")
                .split_whitespace()
                .filter_map(|v| v.parse().ok())
                .collect())
        };
        let mut pids = find()?;
        if n(spec, "pid") == 0 && pids.is_empty() && b(spec, "launch") {
            let activity = shell(
                device,
                &argv(&[
                    "cmd",
                    "package",
                    "resolve-activity",
                    "--brief",
                    "-a",
                    "android.intent.action.MAIN",
                    "-c",
                    "android.intent.category.LAUNCHER",
                    "-p",
                    bundle,
                ]),
            )?;
            let component = s(&activity, "stdout")
                .lines()
                .map(str::trim)
                .find(|line| line.starts_with(&format!("{bundle}/")))
                .ok_or_else(|| {
                    Error::new("launcher_missing", "Application has no launcher activity")
                })?;
            shell(device, &argv(&["am", "start", "-n", component]))?;
            let end = Instant::now() + Duration::from_secs(10);
            while pids.is_empty() && Instant::now() < end {
                thread::sleep(Duration::from_millis(100));
                pids = find()?;
            }
        }
        let pid = if n(spec, "pid") != 0 {
            n(spec, "pid") as u32
        } else if pids.len() == 1 {
            pids[0]
        } else {
            return Err(Error::new(
                "native_process_missing",
                "Select the PID of a running app process, or enable launch for a debuggable build",
            )
            .details(json!({"pids":pids})));
        };
        let value = run_as(
            device,
            bundle,
            &argv(&["cat", &format!("/proc/{pid}/cmdline")]),
        )?;
        let process = s(&value, "stdout").split('\0').next().unwrap_or("");
        if process != bundle && !process.starts_with(&format!("{bundle}:")) {
            return Err(Error::new(
                "native_process_mismatch",
                "The selected PID does not belong to the requested application",
            ));
        }
        let process_name = process.to_string();
        let executable = run_as(
            device,
            bundle,
            &argv(&["readlink", &format!("/proc/{pid}/exe")]),
        )?;
        let remote_exe = s(&executable, "stdout").trim();
        if !remote_exe.starts_with("/system/bin/app_process") && !remote_exe.starts_with("/apex/") {
            return Err(Error::new(
                "native_process_executable",
                "Expected an Android app process executable",
            ));
        }
        let local_exe = dir.join("app_process");
        let mut pull = devices::adb(device)?;
        pull.extend(argv(&["pull", remote_exe, &local_exe.to_string_lossy()]));
        tc::run(&pull, 20, None, true)?;
        let arch = elf_arch(&local_exe)?;
        let symbols = path(s(spec, "program"), false, true)?;
        if elf_arch(&symbols)? != arch {
            return Err(Error::new(
                "native_arch_mismatch",
                "The unstripped binary and Android process have different architectures",
            )
            .details(json!({"process_arch":arch,"symbol_arch":elf_arch(&symbols)?})));
        }
        let server = server_path(arch)?;
        let remote_dir = format!("code_cache/.tomacco-debug-{id}");
        let socket_name = format!("tomacco-lldb-{id}");
        let mut native = Self {
            dir: dir.into(),
            record: json!({"session":id,"device":device,"bundle_id":bundle,"target_pid":pid,"process_name":process_name,"uid":uid,"arch":arch,"remote_dir":remote_dir,"socket_name":socket_name,"port":null}),
            child: None,
            dap_spec: spec.clone(),
        };
        atomic_json(&dir.join("android-native.json"), &native.record)?;
        run_as(device, bundle, &argv(&["mkdir", "-m", "700", &remote_dir]))?;
        let remote_server = format!("{remote_dir}/lldb-server");
        let mut transfer = command(
            device,
            &argv(&[
                "run-as",
                bundle,
                "sh",
                "-c",
                "umask 077; cat > \"$1\"",
                "tomacco-transfer",
                &remote_server,
            ]),
            &dir.join("native-transfer.log"),
        )?;
        transfer.stdin(Stdio::from(File::open(&server)?));
        let mut transfer = transfer.spawn()?;
        bounded_wait(&mut transfer, 30)?;
        run_as(device, bundle, &argv(&["chmod", "700", &remote_server]))?;
        let mut server_command = command(
            device,
            &argv(&[
                "run-as",
                bundle,
                "sh",
                "-c",
                "echo $$ > \"$4\"; exec \"$1\" gdbserver \"$2\" --attach \"$3\"",
                "tomacco-server",
                &remote_server,
                &format!("unix-abstract://{socket_name}"),
                &pid.to_string(),
                &format!("{remote_dir}/server.pid"),
            ]),
            &dir.join("native-server.log"),
        )?;
        native.child = Some(server_command.spawn()?);
        let end = Instant::now() + Duration::from_secs(10);
        loop {
            if native.child.as_mut().unwrap().try_wait()?.is_some() {
                return Err(Error::new(
                    "native_debug_server_failed",
                    "NDK lldb-server exited; inspect native-server.log",
                )
                .details(json!({"log":dir.join("native-server.log")})));
            }
            let sockets = shell(device, &argv(&["cat", "/proc/net/unix"]))?;
            if s(&sockets, "stdout")
                .lines()
                .any(|line| line.split_whitespace().last() == Some(&format!("@{socket_name}")))
            {
                break;
            }
            if Instant::now() >= end {
                return Err(Error::new(
                    "native_debug_timeout",
                    "NDK lldb-server did not open its private socket",
                ));
            }
            thread::sleep(Duration::from_millis(100));
        }
        let mut forward = devices::adb(device)?;
        forward.extend(argv(&[
            "forward",
            "tcp:0",
            &format!("localabstract:{socket_name}"),
        ]));
        let port = tc::run(&forward, 10, None, true)?;
        let port = s(&port, "stdout")
            .trim()
            .parse::<u16>()
            .ok()
            .filter(|p| *p > 0)
            .ok_or_else(|| {
                Error::new(
                    "debug_forward",
                    "ADB did not allocate a local native-debug port",
                )
            })?;
        native.record["port"] = json!(port);
        native.record["server_source"] = json!(server);
        atomic_json(&dir.join("android-native.json"), &native.record)?;
        let mut paths = vec![symbols.parent().unwrap().to_path_buf()];
        if let Some(extra) = spec["symbol_paths"].as_array() {
            for p in extra {
                paths.push(path(p.as_str().unwrap(), true, true)?);
            }
        }
        paths.sort();
        paths.dedup();
        let paths = paths
            .iter()
            .map(|p| lldb_quote(&p.to_string_lossy()))
            .collect::<Result<Vec<_>>>()?
            .join(" ");
        native.dap_spec["program"] = json!(local_exe);
        native.dap_spec["pid"] = Value::Null;
        native.dap_spec["native_attach_port"] = json!(port);
        native.dap_spec["native_pre_run"] = json!([
            format!("settings set target.exec-search-paths {paths}"),
            format!(
                "target modules add {}",
                lldb_quote(&symbols.to_string_lossy())?
            )
        ]);
        Ok(native)
    }

    pub fn details(&self) -> Value {
        self.record.clone()
    }
}

/// Reconcile only this session's unique app directory and ADB mapping. This is
/// also used after a dead worker is discovered by debug.state/list/start.
pub fn recover(dir: &Path) -> Result<Value> {
    let lock = Lock::open(&dir.join("android-cleanup.lock"))?;
    lock.acquire(false)?;
    let previous = read_json(&dir.join("android-cleanup.json"), Some(json!({})))?;
    if b(&previous, "complete") {
        return Ok(previous);
    }
    let record = read_json(&dir.join("android-native.json"), None)?;
    let id = identifier(s(&record, "session"), "debug session", Some("[a-f0-9]{32}"))?;
    let device = s(&record, "device");
    let bundle = identifier(
        s(&record, "bundle_id"),
        "application ID",
        Some(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+"),
    )?;
    let remote_dir = format!("code_cache/.tomacco-debug-{id}");
    let remote = format!("localabstract:tomacco-lldb-{id}");
    let mut errors = Vec::new();
    let mut adb = devices::adb(device)?;
    adb.extend(argv(&["forward", "--list"]));
    match tc::run(&adb, 5, None, true) {
        Ok(list) => {
            for line in s(&list, "stdout").lines() {
                let parts: Vec<_> = line.split_whitespace().collect();
                if parts.len() == 3 && parts[0] == device && parts[2] == remote {
                    let mut remove = devices::adb(device)?;
                    remove.extend(argv(&["forward", "--remove", parts[1]]));
                    if let Err(e) = tc::run(&remove, 5, None, true) {
                        errors.push(e.value());
                    }
                }
            }
        }
        Err(e) => errors.push(e.value()),
    }
    // The executable path contains the unguessable session ID; validate it
    // before signaling the PID, so stale PID files cannot target another app.
    let script = "if [ -f \"$1/server.pid\" ]; then p=$(cat \"$1/server.pid\"); case \"$p\" in ''|*[!0-9]*) exit 2;; esac; e=$(readlink /proc/\"$p\"/exe); case \"$e\" in */\"$1\"/lldb-server) kill -TERM \"$p\" || exit 3;; esac; fi; rm -rf -- \"$1\"";
    if let Err(e) = run_as(
        device,
        bundle,
        &argv(&["sh", "-c", script, "tomacco-cleanup", &remote_dir]),
    ) {
        errors.push(e.value());
    }
    let result = json!({"complete":errors.is_empty(),"errors":errors});
    atomic_json(&dir.join("android-cleanup.json"), &result)?;
    Ok(result)
}

impl Drop for AndroidNative {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
            tc::stop(&mut child);
        }
        let _ = recover(&self.dir);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn lldb_paths_are_single_quoted_arguments_not_commands() {
        assert_eq!(
            lldb_quote("/tmp/A path/quote\"\\lib.so").unwrap(),
            "\"/tmp/A path/quote\\\"\\\\lib.so\""
        );
        for value in ["a\nb", "a\rb", "a\0b"] {
            assert!(lldb_quote(value).is_err());
        }
    }
}
