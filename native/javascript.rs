//! React Native, Expo and Capacitor projects: what `project.inspect` says
//! about them and how `project.run` builds and starts their Android app.
use crate::{core::*, jobs, toolchain as tc};
use serde_json::{Value, json};
use std::{
    net::{Ipv4Addr, SocketAddr, TcpStream},
    path::Path,
    process::Command,
    thread,
    time::{Duration, Instant},
};

/// Metro's port, which React Native debug builds load their JavaScript from.
pub const METRO_PORT: u16 = 8081;

/// The package manager a lockfile names; npm without one.
pub fn package_manager(root: &Path) -> &'static str {
    [
        ("bun.lockb", "bun"),
        ("bun.lock", "bun"),
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
    ]
    .iter()
    .find(|(file, _)| root.join(file).is_file())
    .map(|(_, name)| *name)
    .unwrap_or("npm")
}

/// The framework of a project's `kinds`, most specific first.
pub fn framework(kinds: &[&str]) -> Option<&'static str> {
    ["expo", "capacitor", "react-native"]
        .into_iter()
        .find(|k| kinds.contains(k))
}

/// The application ID in capacitor.config.json or .ts.
pub fn capacitor_app_id(root: &Path) -> Option<String> {
    let json = root.join("capacitor.config.json");
    if let Ok(value) = read_json(&json, None) {
        return value["appId"].as_str().map(String::from);
    }
    let re = regex::Regex::new(r#"appId\s*:\s*["']([\w.]+)["']"#).unwrap();
    for name in ["capacitor.config.ts", "capacitor.config.js"] {
        if let Some(text) = crate::gradle_model::read_text(&root.join(name)) {
            if let Some(c) = re.captures(&text) {
                return Some(c[1].to_string());
            }
        }
    }
    None
}

/// What `project.inspect` reports about a React Native, Expo or Capacitor project.
pub fn describe(root: &Path, framework: &str, gradle: Option<&Path>) -> Value {
    let manager = package_manager(root);
    let dependency = match framework {
        "expo" => "expo",
        "capacitor" => "@capacitor/core",
        _ => "react-native",
    };
    let android = root.join("android");
    let android = (gradle == Some(android.as_path())).then_some(android);
    let run = match (framework, &android) {
        ("expo", _) => {
            "npx expo run:android, which writes android/ with prebuild when it is missing and serves the JavaScript with Metro"
        }
        ("react-native", Some(_)) => {
            "Gradle installs android/, then Metro serves the JavaScript of a debug build"
        }
        ("capacitor", Some(_)) => {
            "the web build script, npx cap sync android, then Gradle installs android/"
        }
        ("capacitor", None) => "needs android/ first: npx cap add android",
        _ => "needs an android/ directory",
    };
    let mut value = json!({
        "framework": framework,
        "package_manager": manager,
        "dependencies_installed": root.join("node_modules").join(dependency).is_dir(),
        "android_project": android,
        "android_run": run,
    });
    if framework == "capacitor" {
        value["app_id"] = json!(capacitor_app_id(root));
    }
    value
}

fn install_command(root: &Path) -> String {
    format!("{} install", package_manager(root))
}

/// Refuses before a build when node_modules lacks the framework.
fn require_dependencies(root: &Path, package: &str) -> Result<()> {
    if root.join("node_modules").join(package).is_dir() {
        return Ok(());
    }
    Err(Error::new(
        "dependencies_missing",
        format!(
            "{package} is not installed in {}/node_modules",
            root.display()
        ),
    )
    .remedy(format!(
        "Run `{}` in {}, then call project.run again",
        install_command(root),
        root.display()
    )))
}

fn require_android(project: &Value, framework: &str) -> Result<()> {
    let root = Path::new(s(project, "path"));
    if Path::new(s(project, "gradle_directory")) == root.join("android") {
        return Ok(());
    }
    let remedy = match framework {
        "capacitor" => {
            "Add the Android platform with `npx cap add android`, then call project.run again"
        }
        _ => {
            "This project has no android/ directory with a Gradle build; create it with the framework's tools (for Expo, `npx expo prebuild --platform android`)"
        }
    };
    Err(Error::new(
        "android_project_missing",
        format!("No Gradle project in {}/android", root.display()),
    )
    .remedy(remedy))
}

fn npx() -> Result<String> {
    tc::find("npx")
}

/// `project.run` for an Expo app: `npx expo run:android` on the device. A
/// debug run keeps Metro serving until the job is cancelled.
pub fn run_expo(project: &Value, a: &Value, mut env: Value) -> Result {
    let root = Path::new(s(project, "path"));
    let npx = npx()?;
    require_dependencies(root, "expo")?;
    let device = s(a, "device");
    let mut args = argv(&[&npx, "expo", "run:android", "--device", device]);
    let release = s(a, "configuration") == "release";
    let variant = s(a, "variant");
    if !variant.is_empty() {
        identifier(variant, "build variant", Some("[a-z][A-Za-z0-9]{0,99}"))?;
        args.extend(argv(&["--variant", variant]));
    } else if release {
        args.extend(argv(&["--variant", "release"]));
    }
    let serves = !release && !variant.to_lowercase().ends_with("release");
    if !serves {
        args.push("--no-bundler".into());
    }
    env["ANDROID_SERIAL"] = json!(device);
    let mut spec = json!({"steps":[{"argv":args,"interactive":serves}],"cwd":root,"env":env,"artifacts":["android/**/build/outputs/apk/**/*.apk"],"exclusive":root,"single_active":true});
    if serves {
        spec["timeout"] = json!(86400);
        // job.send reload asks Expo's Metro to reload the app.
        spec["steps"][0]["metro_port"] = json!(METRO_PORT);
    }
    jobs::submit(&format!("Run {}", s(project, "name")), spec)
}

/// The steps that bring a React Native debug build up: Metro, then the port
/// reversed to the device, then the app.
pub fn metro_step(project: &Value, device: &str, launch: Vec<String>) -> Result<Value> {
    let root = s(project, "path");
    let spec = json!({
        "serve": [npx()?, "react-native", "start", "--port", METRO_PORT.to_string()],
        "cwd": root,
        "port": METRO_PORT,
        "then": [
            [tc::find("adb")?, "-s", device, "reverse", format!("tcp:{METRO_PORT}"), format!("tcp:{METRO_PORT}")],
            launch,
        ],
    });
    Ok(
        json!({"argv":[own_executable()?,"_serve_then",spec.to_string()],"cwd":root,"interactive":true,"metro_port":METRO_PORT}),
    )
}

/// Checks before a React Native run.
pub fn prepare_react_native(project: &Value) -> Result<()> {
    let root = Path::new(s(project, "path"));
    require_android(project, "react-native")?;
    require_dependencies(root, "react-native")?;
    // The React Native Gradle plugin runs node during the build.
    tc::find("node")?;
    npx()?;
    Ok(())
}

/// Checks before a Capacitor run, and the steps before Gradle: the web
/// build when package.json has one, then `npx cap sync android`.
pub fn capacitor_steps(project: &Value) -> Result<Vec<Value>> {
    let root = Path::new(s(project, "path"));
    require_android(project, "capacitor")?;
    require_dependencies(root, "@capacitor/cli")?;
    let npx = npx()?;
    let mut steps = Vec::new();
    let package = read_json(&root.join("package.json"), None)?;
    if package["scripts"].get("build").is_some() {
        let manager = package_manager(root);
        steps.push(json!({"argv":[tc::find(if manager == "npm" { "npm" } else { manager }).unwrap_or_else(|_| manager.to_string()),"run","build"],"cwd":root}));
    }
    steps.push(json!({"argv":[npx,"cap","sync","android"],"cwd":root}));
    Ok(steps)
}

/// Asks the Metro server on `port` to reload the apps connected to it, through
/// its `/reload` endpoint. Returns Metro's HTTP status.
pub fn metro_reload(port: u16) -> Result<u16> {
    use std::io::{Read, Write};
    let address = SocketAddr::from((Ipv4Addr::LOCALHOST, port));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(2)).map_err(|e| {
        Error::new(
            "metro_unreachable",
            format!("Metro does not answer on port {port}: {e}"),
        )
        .remedy(
            "Wait until the job's log says Metro is serving, or run the app again with project.run",
        )
    })?;
    stream.set_read_timeout(Some(Duration::from_secs(5)))?;
    stream.set_write_timeout(Some(Duration::from_secs(5)))?;
    write!(
        stream,
        "POST /reload HTTP/1.1\r\nHost: localhost:{port}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    )?;
    let mut head = [0u8; 64];
    let read = stream.read(&mut head)?;
    let text = String::from_utf8_lossy(&head[..read]);
    text.split_whitespace()
        .nth(1)
        .filter(|_| text.starts_with("HTTP/"))
        .and_then(|code| code.parse().ok())
        .ok_or_else(|| {
            Error::new(
                "reload_failed",
                format!("Port {port} did not answer as an HTTP server"),
            )
        })
}

fn listening(port: u16) -> bool {
    TcpStream::connect_timeout(
        &SocketAddr::from((Ipv4Addr::LOCALHOST, port)),
        Duration::from_millis(300),
    )
    .is_ok()
}

/// The `_serve_then` worker: starts a server, waits for its port, runs the
/// commands that follow in order, and then stays until the server exits.
pub fn serve_worker(request: &str) -> Result<()> {
    let spec: Value = serde_json::from_str(request)?;
    let port = u16::try_from(n(&spec, "port"))
        .map_err(|_| Error::new("invalid_request", "Invalid port"))?;
    let wait = Duration::from_secs(spec["wait"].as_u64().unwrap_or(180));
    if listening(port) {
        return Err(Error::new(
            "port_busy",
            format!("Port {port} is already in use, perhaps by another Metro bundler"),
        )
        .remedy("Stop the other bundler, or its project.run job, then run again"));
    }
    let serve = strings(&spec["serve"]);
    let (program, rest) = serve
        .split_first()
        .ok_or_else(|| Error::new("invalid_request", "Missing server command"))?;
    let mut server = Command::new(program);
    server.args(rest).current_dir(s(&spec, "cwd"));
    tc::parent_bound(&mut server);
    let mut server = server.spawn()?;
    let deadline = Instant::now() + wait;
    while !listening(port) {
        if let Some(status) = server.try_wait()? {
            return Err(Error::new(
                "server_failed",
                format!("{program} exited ({status}) before it listened on port {port}"),
            ));
        }
        if Instant::now() >= deadline {
            let _ = server.kill();
            return Err(Error::new(
                "server_timeout",
                format!(
                    "Nothing listened on port {port} within {} s",
                    wait.as_secs()
                ),
            ));
        }
        thread::sleep(Duration::from_millis(250));
    }
    eprintln!("Port {port} is listening");
    for command in spec["then"].as_array().into_iter().flatten() {
        let args = strings(command);
        let Some((program, rest)) = args.split_first() else {
            continue;
        };
        let status = Command::new(program).args(rest).status()?;
        if !status.success() {
            let _ = server.kill();
            let _ = server.wait();
            return Err(Error::new(
                "step_failed",
                format!("{program} failed ({status})"),
            ));
        }
    }
    eprintln!("Serving on port {port}; cancel the job to stop");
    let status = server.wait()?;
    if status.success() {
        Ok(())
    } else {
        Err(Error::new(
            "server_failed",
            format!("{program} exited ({status})"),
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn the_package_manager_follows_the_lockfile() {
        let root = tempfile::tempdir().unwrap();
        assert_eq!(package_manager(root.path()), "npm");
        fs::write(root.path().join("yarn.lock"), "").unwrap();
        assert_eq!(package_manager(root.path()), "yarn");
        fs::write(root.path().join("pnpm-lock.yaml"), "").unwrap();
        assert_eq!(package_manager(root.path()), "pnpm");
    }

    #[test]
    fn missing_dependencies_name_the_install_command() {
        let root = tempfile::tempdir().unwrap();
        fs::write(root.path().join("yarn.lock"), "").unwrap();
        let error = require_dependencies(root.path(), "react-native").unwrap_err();
        assert_eq!(error.code, "dependencies_missing");
        assert!(error.remedy.contains("`yarn install`"));
        fs::create_dir_all(root.path().join("node_modules/react-native")).unwrap();
        assert!(require_dependencies(root.path(), "react-native").is_ok());
    }

    #[test]
    fn the_capacitor_app_id_is_read_from_its_config() {
        let root = tempfile::tempdir().unwrap();
        assert_eq!(capacitor_app_id(root.path()), None);
        fs::write(
            root.path().join("capacitor.config.ts"),
            "const config: CapacitorConfig = {\n  appId: 'io.ionic.starter',\n  webDir: 'dist'\n};\n",
        )
        .unwrap();
        assert_eq!(capacitor_app_id(root.path()).unwrap(), "io.ionic.starter");
        fs::write(
            root.path().join("capacitor.config.json"),
            r#"{"appId":"dev.cap.app"}"#,
        )
        .unwrap();
        assert_eq!(capacitor_app_id(root.path()).unwrap(), "dev.cap.app");
    }

    #[test]
    fn a_missing_android_directory_names_the_frameworks_command() {
        let project = json!({"path":"/work/app","gradle_directory":null});
        let error = require_android(&project, "capacitor").unwrap_err();
        assert_eq!(error.code, "android_project_missing");
        assert!(error.remedy.contains("npx cap add android"));
        let error = require_android(&project, "react-native").unwrap_err();
        assert!(error.remedy.contains("android/"));
        let project = json!({"path":"/work/app","gradle_directory":"/work/app/android"});
        assert!(require_android(&project, "capacitor").is_ok());
    }

    #[test]
    fn metro_reload_posts_to_the_reload_endpoint() {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0u8; 512];
            let read = stream.read(&mut request).unwrap();
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
                .unwrap();
            String::from_utf8_lossy(&request[..read]).into_owned()
        });
        assert_eq!(metro_reload(port).unwrap(), 200);
        assert!(
            server
                .join()
                .unwrap()
                .starts_with("POST /reload HTTP/1.1\r\n")
        );
        let closed = free_port();
        assert_eq!(metro_reload(closed).unwrap_err().code, "metro_unreachable");
    }

    fn free_port() -> u16 {
        std::net::TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port()
    }

    #[test]
    fn the_server_is_up_before_the_following_commands_run() {
        let dir = tempfile::tempdir().unwrap();
        let port = free_port();
        let marker = dir.path().join("then");
        // A server that listens and exits after accepting a connection made
        // once the follow-up commands ran. It checks only after an accept, so
        // it cannot exit before the follow-up's own connection arrives.
        let server = format!(
            "import socket,os,time\ns=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);time.sleep(0.3);s.bind(('127.0.0.1',{port}));s.listen()\nwhile True:\n  c,_=s.accept();done=os.path.exists({marker:?});c.close()\n  if done: break\n"
        );
        let spec = json!({
            "serve": ["python3", "-c", server],
            "cwd": dir.path(),
            "port": port,
            "wait": 20,
            "then": [["sh", "-c", format!("touch '{}'; python3 -c \"import socket;socket.create_connection(('127.0.0.1',{port}))\"", marker.display())]],
        });
        serve_worker(&spec.to_string()).unwrap();
        assert!(marker.exists());
    }

    #[test]
    fn a_busy_port_or_a_failing_server_is_reported() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let spec = json!({"serve":["true"],"cwd":"/","port":port,"then":[]});
        assert_eq!(
            serve_worker(&spec.to_string()).unwrap_err().code,
            "port_busy"
        );
        drop(listener);
        let spec = json!({"serve":["false"],"cwd":"/","port":free_port(),"wait":5,"then":[]});
        assert_eq!(
            serve_worker(&spec.to_string()).unwrap_err().code,
            "server_failed"
        );
    }
}
