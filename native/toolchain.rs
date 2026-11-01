use crate::core::*;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    io::Read,
    os::unix::{fs::PermissionsExt, process::CommandExt},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};

pub const TOOLS: &[(&str, &[&str], &str)] = &[
    ("keytool", &["-help"], "Configure a JDK using java_home"),
    ("jarsigner", &["-help"], "Configure a JDK using java_home"),
    ("apksigner", &["version"], "Install Android SDK build-tools"),
    ("zipalign", &["-h"], "Install Android SDK build-tools"),
    ("aapt2", &["version"], "Install Android SDK build-tools"),
    (
        "bundletool",
        &["version"],
        "Download bundletool-all-VERSION.jar from https://github.com/google/bundletool/releases and set it with settings.configure {\"executables\":{\"bundletool\":\"/absolute/bundletool-all.jar\"}}",
    ),
    (
        "curl",
        &["--version"],
        "Install curl for authenticated store uploads",
    ),
    (
        "openssl",
        &["version"],
        "Install OpenSSL for service-account authentication",
    ),
    (
        "lldb-dap",
        &["--version"],
        "Configure the LLDB DAP adapter shipped with Swift or LLVM",
    ),
    (
        "trace_processor",
        &["--version"],
        "Configure the official Perfetto trace processor",
    ),
    ("adb", &["version"], "Install Android SDK platform-tools"),
    (
        "emulator",
        &["-version"],
        "Install the Android SDK emulator package",
    ),
    (
        "sdkmanager",
        &["--version"],
        "Configure the Android SDK command-line tools",
    ),
    (
        "avdmanager",
        &["list", "target"],
        "Configure the Android SDK command-line tools",
    ),
    (
        "java",
        &["-version"],
        "Configure a JDK 17 or newer using settings.configure",
    ),
    (
        "gradle",
        &["--version"],
        "Use a project's Gradle wrapper or configure Gradle",
    ),
    (
        "flutter",
        &["--version"],
        "Install Flutter and set its executable path",
    ),
    (
        "dart",
        &["--version"],
        "Install the Dart SDK, or Flutter, which ships one in its bin directory",
    ),
    (
        "xtool",
        &["--version"],
        "Install xtool from https://github.com/xtool-org/xtool",
    ),
    (
        "swift",
        &["--version"],
        "Install Swift from https://www.swift.org/install/linux/",
    ),
    ("idevice_id", &["--version"], "Install libimobiledevice"),
    (
        "idevicepair",
        &["--version"],
        "Install libimobiledevice for iPhone pairing",
    ),
    (
        "iproxy",
        &["--version"],
        "Install libusbmuxd for private iPhone USB forwarding",
    ),
    (
        "go-ios",
        &["version"],
        "Install go-ios for native iPhone services on Linux",
    ),
    ("ideviceinfo", &["--version"], "Install libimobiledevice"),
    ("idevicesyslog", &["--version"], "Install libimobiledevice"),
    (
        "ideviceprovision",
        &["--version"],
        "Install libimobiledevice to list an iPhone's provisioning profiles",
    ),
    (
        "idevicescreenshot",
        &["--version"],
        "Install libimobiledevice and mount the developer disk image",
    ),
    (
        "scrcpy",
        &["--version"],
        "Install scrcpy for live Android display and input",
    ),
    ("node", &["--version"], "Install Node.js"),
    ("npm", &["--version"], "Install npm"),
    (
        "npx",
        &["--version"],
        "Install Node.js with npm, which provides npx",
    ),
    ("pnpm", &["--version"], "Install pnpm"),
    ("yarn", &["--version"], "Install Yarn"),
    ("git", &["--version"], "Install Git"),
    ("ssh", &["-V"], "Install OpenSSH"),
];
pub fn settings() -> Result {
    let value = read_json(&home()?.join("settings.json"), Some(json!({})))?;
    if !value.is_object() || value.get("executables").is_some_and(|v| !v.is_object()) {
        return Err(Error::new(
            "invalid_settings",
            "settings.json and executables must be objects",
        ));
    }
    Ok(value)
}
pub fn configure(a: &Value) -> Result {
    let lock = Lock::open(&home()?.join("settings.lock"))?;
    lock.acquire(false)?;
    let mut config = settings()?;
    for key in ["android_sdk", "java_home", "android_ndk"] {
        if a.get(key).is_some() {
            config[key] = json!(path(s(a, key), true, true)?);
        }
    }
    if let Some(values) = a["executables"].as_object() {
        for (name, value) in values {
            if !TOOLS.iter().any(|t| t.0 == name) {
                return Err(Error::new("invalid_tool", format!("Unknown tool: {name}")));
            }
            let p = path(
                value.as_str().ok_or_else(|| {
                    Error::new("invalid_argument", "Executable path must be a string")
                })?,
                false,
                true,
            )?;
            if !executable(&p) && !runs_as_jar(name, &p) {
                return Err(Error::new(
                    "not_executable",
                    format!("Not executable: {}", p.display()),
                ));
            }
            if !config["executables"].is_object() {
                config["executables"] = json!({});
            }
            config["executables"][name] = json!(p);
        }
    }
    if a.get("swift_android_sdk").is_some() {
        config["swift_android_sdk"] = json!(identifier(
            s(a, "swift_android_sdk"),
            "Swift SDK identifier",
            None
        )?);
    }
    if a["updates"].is_object() {
        crate::update::configure(&mut config, &a["updates"])?;
    }
    for (_, key, _) in crate::jobs::RESOURCES {
        if let Some(limit) = a["jobs"][*key].as_u64() {
            if !config["jobs"].is_object() {
                config["jobs"] = json!({});
            }
            config["jobs"][*key] = json!(limit);
        }
    }
    if a.get("agent_policy").is_some() {
        config["agent_policy"] = crate::guard::validated_policy(&a["agent_policy"])?;
    }
    atomic_json(&home()?.join("settings.json"), &config)?;
    Ok(config)
}
/// bundletool ships as a JAR that runs through Java rather than as an
/// executable.
fn runs_as_jar(name: &str, path: &Path) -> bool {
    name == "bundletool" && path.is_file() && path.extension().is_some_and(|x| x == "jar")
}
/// The command that starts a tool: a configured bundletool JAR runs through
/// `java -jar`.
pub fn command(name: &str) -> Result<Vec<String>> {
    let tool = find(name)?;
    if runs_as_jar(name, Path::new(&tool)) {
        return Ok(vec![find("java")?, "-jar".into(), tool]);
    }
    Ok(vec![tool])
}
pub fn executable(path: &Path) -> bool {
    path.is_file()
        && fs::metadata(path)
            .map(|m| m.permissions().mode() & 0o111 != 0)
            .unwrap_or(false)
}
pub fn which(name: &str) -> Option<PathBuf> {
    std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
        .map(|p| p.join(name))
        .find(|p| executable(p))
}
pub fn android_sdk() -> Result<Option<PathBuf>> {
    let conf = settings()?;
    Ok([
        Some(s(&conf, "android_sdk").to_string()),
        std::env::var("ANDROID_HOME").ok(),
        std::env::var("ANDROID_SDK_ROOT").ok(),
        Some(
            user_home()
                .join("Android/Sdk")
                .to_string_lossy()
                .into_owned(),
        ),
        Some(home()?.join("sdk/android").to_string_lossy().into_owned()),
    ]
    .into_iter()
    .flatten()
    .filter(|p| !p.is_empty())
    .map(PathBuf::from)
    .find(|p| p.is_dir()))
}
pub fn android_ndk() -> Result<Option<PathBuf>> {
    let config = settings()?;
    let configured = if !s(&config, "android_ndk").is_empty() {
        Some(s(&config, "android_ndk").to_string())
    } else {
        std::env::var("ANDROID_NDK_HOME").ok()
    };
    if let Some(p) = configured {
        let p = PathBuf::from(p);
        return Ok(p.is_dir().then_some(p));
    }
    let Some(sdk) = android_sdk()? else {
        return Ok(None);
    };
    let mut paths = glob_paths(&format!(
        "{}/ndk/*",
        glob::Pattern::escape(&sdk.to_string_lossy())
    ))?;
    paths.retain(|p| p.join("source.properties").is_file());
    paths.sort_by_key(|p| {
        p.file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .split(|c: char| !c.is_ascii_digit())
            .filter_map(|p| p.parse::<u64>().ok())
            .collect::<Vec<_>>()
    });
    Ok(paths.pop())
}
pub fn environment() -> Result<BTreeMap<String, String>> {
    let config = settings()?;
    let mut env = BTreeMap::new();
    let mut bins = Vec::new();
    if let Some(sdk) = android_sdk()? {
        env.insert("ANDROID_HOME".into(), sdk.to_string_lossy().into_owned());
        env.insert(
            "ANDROID_SDK_ROOT".into(),
            sdk.to_string_lossy().into_owned(),
        );
        bins.extend([sdk.join("platform-tools"), sdk.join("emulator")]);
    }
    if !s(&config, "java_home").is_empty() {
        env.insert("JAVA_HOME".into(), s(&config, "java_home").into());
        bins.insert(0, PathBuf::from(s(&config, "java_home")).join("bin"));
    }
    if let Some(ndk) = android_ndk()? {
        env.insert(
            "ANDROID_NDK_HOME".into(),
            ndk.to_string_lossy().into_owned(),
        );
    }
    if !s(&config, "swift_android_sdk").is_empty() {
        env.insert(
            "SWIFT_ANDROID_SDK".into(),
            s(&config, "swift_android_sdk").into(),
        );
    }
    if let Some(swift) = config["executables"]["swift"].as_str() {
        env.insert("SWIFT_EXECUTABLE".into(), swift.into());
        // xtool resolves both swift and swiftc through PATH. Follow configured
        // symlinks to the toolchain, but never prepend an unrelated directory
        // for a standalone wrapper that has no matching `swift` beside it.
        if let Ok(resolved) = Path::new(swift).canonicalize() {
            if let Some(bin) = resolved.parent() {
                let candidate = bin.join("swift");
                if executable(&candidate)
                    && candidate.canonicalize().ok().as_ref() == Some(&resolved)
                {
                    bins.insert(0, bin.to_path_buf());
                }
            }
        }
    }
    bins.extend(std::env::split_paths(
        &std::env::var_os("PATH").unwrap_or_default(),
    ));
    env.insert(
        "PATH".into(),
        std::env::join_paths(bins)
            .map_err(|e| Error::new("invalid_path", e.to_string()))?
            .to_string_lossy()
            .into_owned(),
    );
    Ok(env)
}
/// Resolve the executable that managed child processes find through PATH.
pub fn managed_which(name: &str) -> Result<Option<PathBuf>> {
    let env = environment()?;
    Ok(std::env::split_paths(&env["PATH"])
        .map(|bin| bin.join(name))
        .find(|candidate| executable(candidate)))
}
pub fn find_optional(name: &str) -> Result<Option<String>> {
    let config = settings()?;
    let mut candidates = Vec::new();
    if let Some(value) = config["executables"][name].as_str() {
        candidates.push(PathBuf::from(value));
    }
    if let Some(sdk) = android_sdk()? {
        match name {
            "adb" => candidates.push(sdk.join("platform-tools/adb")),
            "emulator" => candidates.push(sdk.join("emulator/emulator")),
            "apksigner" | "zipalign" | "aapt2" => {
                let mut found = glob_paths(&format!(
                    "{}/build-tools/*/{name}",
                    glob::Pattern::escape(&sdk.to_string_lossy())
                ))?;
                found.sort_by_key(|p| {
                    p.parent()
                        .unwrap()
                        .file_name()
                        .unwrap_or_default()
                        .to_string_lossy()
                        .split('.')
                        .filter_map(|s| s.parse::<u32>().ok())
                        .collect::<Vec<_>>()
                });
                found.reverse();
                candidates.extend(found);
            }
            "sdkmanager" | "avdmanager" => {
                let mut found = glob_paths(&format!(
                    "{}/cmdline-tools/*/bin/{name}",
                    glob::Pattern::escape(&sdk.to_string_lossy())
                ))?;
                found.reverse();
                candidates.extend(found);
            }
            _ => (),
        }
    }
    if ["java", "keytool", "jarsigner"].contains(&name) && !s(&config, "java_home").is_empty() {
        candidates.insert(
            0,
            PathBuf::from(s(&config, "java_home")).join(format!("bin/{name}")),
        );
    }
    if let Some(p) = which(name) {
        candidates.push(p);
    }
    candidates.push(user_home().join(".local/bin").join(name));
    Ok(candidates
        .into_iter()
        .find(|p| executable(p) || runs_as_jar(name, p))
        .map(|p| p.to_string_lossy().into_owned()))
}
pub fn find(name: &str) -> Result<String> {
    find_optional(name)?.ok_or_else(|| {
        Error::new(
            "tool_missing",
            format!("{name} is not installed/configured"),
        )
        .remedy(
            TOOLS
                .iter()
                .find(|t| t.0 == name)
                .map(|t| t.2.to_string())
                .unwrap_or_else(|| format!("Install {name}")),
        )
    })
}
pub fn stop(child: &mut Child) {
    // Each managed command has its own process group; never signal caller's group.
    let pid = child.id() as i32;
    unsafe {
        libc::kill(-pid, libc::SIGTERM);
    }
    let deadline = Instant::now() + Duration::from_secs(2);
    while Instant::now() < deadline {
        if child.try_wait().ok().flatten().is_some() {
            break;
        }
        thread::sleep(Duration::from_millis(20));
    }
    // Descendants may survive their group leader. Kill the entire group even then.
    unsafe {
        libc::kill(-pid, libc::SIGKILL);
    }
    let _ = child.wait();
    // A job worker adopts orphaned descendants. Reap only this command's
    // stopped group, never another concurrently managed command or a detached
    // service. A container's PID 1 may not reap zombies on our behalf.
    #[cfg(target_os = "linux")]
    {
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            let waited = unsafe { libc::waitpid(-pid, std::ptr::null_mut(), libc::WNOHANG) };
            if waited > 0 {
                continue;
            }
            if waited < 0 && std::io::Error::last_os_error().raw_os_error() != Some(libc::EINTR) {
                break;
            }
            if Instant::now() >= deadline {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
    }
}

/// Only durable, dedicated job workers opt in. Changing the process-wide
/// subreaper setting in the multithreaded MCP/CLI host would steal child owners.
pub fn prepare_job_reaper() -> Result<()> {
    #[cfg(target_os = "linux")]
    if unsafe { libc::prctl(libc::PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) } < 0 {
        return Err(Error::new(
            "worker_reaper_unavailable",
            std::io::Error::last_os_error().to_string(),
        ));
    }
    Ok(())
}
/// A durable worker may outlive its client; its subprocesses must not outlive
/// the worker itself (for example after cancellation or a crash).
pub fn parent_bound(command: &mut Command) {
    #[cfg(target_os = "linux")]
    {
        let parent = std::process::id() as libc::pid_t;
        unsafe {
            command.pre_exec(move || {
                if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL) < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                if libc::getppid() != parent {
                    return Err(std::io::Error::other(
                        "Worker exited before subprocess startup",
                    ));
                }
                Ok(())
            });
        }
    }
}
fn capture(
    mut stream: impl Read + Send + 'static,
) -> thread::JoinHandle<std::io::Result<(Vec<u8>, bool)>> {
    thread::spawn(move || {
        let mut output = Vec::new();
        let mut buffer = [0u8; 16384];
        let mut truncated = false;
        loop {
            let size = stream.read(&mut buffer)?;
            if size == 0 {
                break;
            }
            let take = size.min((8 * 1024 * 1024usize).saturating_sub(output.len()));
            output.extend_from_slice(&buffer[..take]);
            truncated |= take < size;
        }
        Ok((output, truncated))
    })
}
pub fn run(args: &[String], timeout: u64, cwd: Option<&Path>, check: bool) -> Result {
    check_cancelled()?;
    if args.is_empty() {
        return Err(Error::new("invalid_command", "Missing command"));
    }
    let mut command = Command::new(&args[0]);
    command
        .args(&args[1..])
        .envs(environment()?)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    if let Some(cwd) = cwd {
        command.current_dir(cwd);
    }
    parent_bound(&mut command);
    let mut child = command
        .spawn()
        .map_err(|e| Error::new("tool_start_failed", e.to_string()))?;
    let output = capture(child.stdout.take().unwrap());
    let error = capture(child.stderr.take().unwrap());
    let deadline = Instant::now() + Duration::from_secs(timeout);
    let status = loop {
        if let Some(code) = child.try_wait()? {
            break Some(code);
        }
        if Instant::now() >= deadline || check_cancelled().is_err() {
            stop(&mut child);
            break None;
        }
        thread::sleep(Duration::from_millis(10));
    };
    // Close pipes held by orphan subprocesses before joining readers.
    unsafe {
        libc::kill(-(child.id() as i32), libc::SIGKILL);
    }
    let (out, out_truncated) = output
        .join()
        .map_err(|_| Error::new("capture_failed", "stdout reader failed"))??;
    let (err, err_truncated) = error
        .join()
        .map_err(|_| Error::new("capture_failed", "stderr reader failed"))??;
    let Some(status) = status else {
        check_cancelled()?;
        return Err(Error::new(
            "tool_timeout",
            format!("{} exceeded {timeout}s", args[0]),
        ));
    };
    let code = status.code().unwrap_or(-1);
    let stdout = String::from_utf8_lossy(&out);
    let stderr = String::from_utf8_lossy(&err);
    if check && code != 0 {
        return Err(Error::new("tool_failed",format!("{} exited {code}",Path::new(&args[0]).file_name().unwrap_or_default().to_string_lossy())).details(json!({"stdout":stdout.chars().rev().take(16000).collect::<String>().chars().rev().collect::<String>(),"stderr":stderr.chars().rev().take(16000).collect::<String>().chars().rev().collect::<String>()})));
    }
    if out_truncated || err_truncated {
        return Err(Error::new(
            "response_too_large",
            "Command output exceeded 8 MiB; use a job with bounded logs",
        ));
    }
    Ok(json!({"exit_code":code,"stdout":stdout,"stderr":stderr}))
}
/// Runs `count` independent checks on up to `workers` threads, a slow one
/// never holding back the rest, and returns their results in order.
fn in_parallel<T: Send>(
    count: usize,
    workers: usize,
    check: impl Fn(usize) -> T + Sync,
) -> Vec<Option<T>> {
    use std::sync::{
        Mutex,
        atomic::{AtomicUsize, Ordering},
    };
    let next = AtomicUsize::new(0);
    let results: Mutex<Vec<Option<T>>> = Mutex::new((0..count).map(|_| None).collect());
    thread::scope(|scope| {
        for _ in 0..workers.clamp(1, count.max(1)) {
            scope.spawn(|| {
                loop {
                    let index = next.fetch_add(1, Ordering::Relaxed);
                    if index >= count {
                        break;
                    }
                    // A check that panics leaves None, as a lost result.
                    let value =
                        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| check(index)));
                    results.lock().unwrap_or_else(|e| e.into_inner())[index] = value.ok();
                }
            });
        }
    });
    results.into_inner().unwrap_or_else(|e| e.into_inner())
}
fn tool_status(name: &str, args: &[&str], remedy: &str) -> Result {
    let p = find_optional(name)?;
    let mut value = json!({"name":name,"path":p,"available":false,"remedy":remedy});
    if p.is_some() {
        let started = command(name).and_then(|mut argv| {
            argv.extend(args.iter().map(|s| s.to_string()));
            run(&argv, 10, None, false)
        });
        match started {
            Ok(result) => {
                let text = if s(&result, "stdout").is_empty() {
                    s(&result, "stderr")
                } else {
                    s(&result, "stdout")
                };
                value["available"] = json!(
                    result["exit_code"] == 0
                        || (name == "zipalign" && text.contains("Zip alignment utility"))
                );
                value["version"] = json!(text.trim().chars().take(2000).collect::<String>());
                value["exit_code"] = result["exit_code"].clone();
            }
            Err(e) => value["error"] = e.value(),
        }
    }
    Ok(value)
}
fn ios_sdk_status() -> Result {
    Ok(if let Some(p) = find_optional("xtool")? {
        run(&argv(&[&p, "sdk", "status"]), 15, None, true)
            .unwrap_or_else(|e| json!({"error":e.value()}))
    } else {
        Value::Null
    })
}
pub fn doctor() -> Result {
    let cancelled = cancellation();
    // Every tool's version check, and xtool's SDK status, at once: each one
    // only waits for its own process. Up to 16 at a time.
    let mut results = in_parallel(TOOLS.len() + 2, 16, |index| {
        with_cancellation(cancelled.clone(), || match TOOLS.get(index) {
            Some(&(name, args, remedy)) => tool_status(name, args, remedy),
            None if index == TOOLS.len() => ios_sdk_status(),
            None => Ok(crate::desktop::load_check()),
        })
    })
    .into_iter()
    .map(|result| {
        result.unwrap_or_else(|| Err(Error::new("tool_failed", "Doctor worker panicked")))
    })
    .collect::<Result<Vec<Value>>>()?;
    let desktop = results.pop().unwrap_or(Value::Null);
    let ios = results.pop().unwrap_or(Value::Null);
    let tools = results;
    let host = crate::host::current();
    let kvm = host.kvm.accessible;
    let assessed = crate::host::assess(&host);
    let advice = &assessed["kvm"];
    Ok(
        json!({"host":assessed,"tools":tools,"android_sdk":android_sdk()?.map(|p| p.to_string_lossy().into_owned()).unwrap_or_default(),"android_ndk":android_ndk()?.map(|p| p.to_string_lossy().into_owned()).unwrap_or_default(),"ios_sdk":ios,"desktop":desktop,"kvm":{"exists":host.kvm.exists,"accessible":kvm,"status":advice["status"],"message":advice["message"],"commands":advice["commands"]},"settings":settings()?,"data_directory":home()?,"ios_runtime":"Apple Simulator requires macOS; Linux builds use xtool and a real device"}),
    )
}

#[cfg(test)]
mod doctor_tests {
    use super::*;
    use std::time::{Duration, Instant};

    #[test]
    fn checks_run_side_by_side_and_keep_their_order() {
        let started = Instant::now();
        // One slow check among quick ones: with chunks, everything after it
        // would wait; here the total is about the slowest one.
        let results = in_parallel(12, 4, |index| {
            thread::sleep(Duration::from_millis(if index == 0 { 300 } else { 50 }));
            index * 10
        });
        let elapsed = started.elapsed();
        assert_eq!(
            results,
            (0..12).map(|index| Some(index * 10)).collect::<Vec<_>>()
        );
        // 11 quick checks on the three other workers: about 200 ms, beside the slow 300.
        assert!(elapsed < Duration::from_millis(1000), "{elapsed:?}");
        assert!(in_parallel(0, 4, |index| index).is_empty());
    }

    #[test]
    fn a_check_that_panics_loses_only_its_own_result() {
        let results = in_parallel(3, 2, |index| {
            assert_ne!(index, 1, "fixture panic");
            index
        });
        assert_eq!(results, [Some(0), None, Some(2)]);
    }
}
