//! A real update through the real binary: install an old version from a package,
//! `update.apply` against a local HTTP fixture with a test key, and
//! `update.rollback` back to the old one, all in a temporary home. Needs the
//! test hooks, which only debug builds have, and Python for the installer.
#![cfg(debug_assertions)]

use base64::{Engine, engine::general_purpose::STANDARD};
use ed25519_dalek::{Signer, SigningKey};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    fs,
    io::{Read, Write},
    net::TcpListener,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{Arc, Mutex},
};

const ROOT: &str = env!("CARGO_MANIFEST_DIR");
type Pages = Arc<Mutex<HashMap<String, Vec<u8>>>>;

/// A fixture that serves a table of paths, one request per connection.
fn serve(pages: Pages) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap().to_string();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { continue };
            let pages = pages.clone();
            std::thread::spawn(move || {
                let mut request = Vec::new();
                let mut buffer = [0u8; 4096];
                while !request.windows(4).any(|w| w == b"\r\n\r\n") {
                    match stream.read(&mut buffer) {
                        Ok(0) | Err(_) => return,
                        Ok(size) => request.extend_from_slice(&buffer[..size]),
                    }
                }
                let text = String::from_utf8_lossy(&request).into_owned();
                let path = text.split_whitespace().nth(1).unwrap_or("").to_string();
                let body = pages.lock().unwrap().get(&path).cloned();
                let (status, body) = match body {
                    Some(body) => ("200 OK", body),
                    None => ("404 Not Found", b"{}".to_vec()),
                };
                let head = format!(
                    "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                );
                let _ = stream.write_all(head.as_bytes());
                let _ = stream.write_all(&body);
            });
        }
    });
    address
}

fn copy_into(source: &str, target: &Path) {
    fs::create_dir_all(target.parent().unwrap()).unwrap();
    fs::copy(Path::new(ROOT).join(source), target).unwrap();
}

/// The tree CMake's install rules make, with the real tomaccoctl marked as `version`.
fn package_tree(top: &Path, version: &str) {
    let app = "io.github.marselnenaj.tomacco";
    let mut binary = fs::read(env!("CARGO_BIN_EXE_tomaccoctl")).unwrap();
    binary.extend_from_slice(format!("\nTomacco-TEST-VERSION={version}\n").as_bytes());
    fs::create_dir_all(top.join("bin")).unwrap();
    fs::write(top.join("bin/tomaccoctl"), &binary).unwrap();
    fs::set_permissions(
        top.join("bin/tomaccoctl"),
        fs::Permissions::from_mode(0o755),
    )
    .unwrap();
    fs::write(top.join("bin/tomacco"), b"#!/bin/sh\nexit 0\n").unwrap();
    fs::set_permissions(top.join("bin/tomacco"), fs::Permissions::from_mode(0o755)).unwrap();
    copy_into("install.py", &top.join("install.py"));
    let share = top.join("share/tomacco");
    fs::create_dir_all(&share).unwrap();
    fs::write(share.join("VERSION"), format!("{version}\n")).unwrap();
    copy_into(
        "tools/build_source_release.py",
        &share.join("tools/build_source_release.py"),
    );
    fs::create_dir_all(share.join("examples/ios-restore-native")).unwrap();
    fs::write(share.join("examples/ios-restore-native/example.c"), version).unwrap();
    fs::create_dir_all(share.join("integrations/ios-emulator")).unwrap();
    fs::write(share.join("integrations/ios-emulator/README.md"), version).unwrap();
    fs::create_dir_all(top.join("lib/tomacco/assets")).unwrap();
    fs::write(top.join("lib/tomacco/assets/models.json"), b"[]\n").unwrap();
    fs::create_dir_all(top.join("share/doc/tomacco")).unwrap();
    fs::write(top.join("share/doc/tomacco/LICENSE"), version).unwrap();
    copy_into(
        "assets/icon.svg",
        &top.join(format!("share/icons/hicolor/scalable/apps/{app}.svg")),
    );
    copy_into(
        &format!("assets/{app}.desktop"),
        &top.join(format!("share/applications/{app}.desktop")),
    );
    copy_into(
        &format!("assets/{app}.metainfo.xml"),
        &top.join(format!("share/metainfo/{app}.metainfo.xml")),
    );
    copy_into(
        "docs/tomaccoctl.1",
        &top.join("share/man/man1/tomaccoctl.1"),
    );
}

fn package(tree: &Path) -> Vec<u8> {
    let mut archive = tar::Builder::new(flate2::write::GzEncoder::new(
        Vec::new(),
        flate2::Compression::fast(),
    ));
    archive
        .append_dir_all(tree.file_name().unwrap(), tree)
        .unwrap();
    archive.into_inner().unwrap().finish().unwrap()
}

struct Sandbox {
    _directory: tempfile::TempDir,
    home: PathBuf,
    data: PathBuf,
    api: String,
    public_key: String,
}

impl Sandbox {
    /// The installed launcher, as a user would run it, with the fixture's hooks.
    fn command(&self, arguments: &[&str]) -> Command {
        let mut command = Command::new(self.home.join(".local/bin/tomaccoctl"));
        command
            .args(arguments)
            .env("HOME", &self.home)
            .env("TOMACCO_HOME", &self.data)
            .env_remove("TOMACCO_UPDATES")
            .env("TOMACCO_UPDATE_TEST_API", &self.api)
            .env("TOMACCO_UPDATE_TEST_PUBLIC_KEY", &self.public_key)
            .stdin(Stdio::null());
        command
    }
    fn call(&self, operation: &str, arguments: Value) -> Value {
        let output = self
            .command(&["call", operation, &arguments.to_string()])
            .output()
            .unwrap();
        serde_json::from_slice(&output.stdout).unwrap_or_else(|_| {
            panic!(
                "{operation}: {}\n{}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            )
        })
    }
    fn ok(&self, operation: &str, arguments: Value) -> Value {
        let reply = self.call(operation, arguments);
        assert_eq!(reply["ok"], true, "{operation}: {reply}");
        reply["result"].clone()
    }
    fn version(&self) -> String {
        let output = self.command(&["--version"]).output().unwrap();
        String::from_utf8_lossy(&output.stdout)
            .split_whitespace()
            .nth(1)
            .unwrap_or("")
            .to_string()
    }
    fn apply(&self) -> Value {
        let job = self.ok("update.apply", json!({}));
        let id = job["id"].as_str().unwrap().to_string();
        for _ in 0..40 {
            let waited = self.ok("job.wait", json!({"job_id":id,"seconds":15}));
            if ["succeeded", "failed", "cancelled", "timed_out"]
                .contains(&waited["state"].as_str().unwrap_or(""))
            {
                return waited;
            }
        }
        panic!("the update job did not end");
    }
    fn log(&self, job: &Value) -> String {
        fs::read_to_string(job["log_path"].as_str().unwrap()).unwrap_or_default()
    }
}

fn publish(pages: &Pages, api: &str, version: &str, package: &[u8], signature: Option<Vec<u8>>) {
    let name = format!("tomacco-{version}-Linux-{}.tar.gz", std::env::consts::ARCH);
    let mut assets = vec![json!({
        "name":name,"size":package.len(),
        "browser_download_url":format!("http://{api}/download/{name}"),
    })];
    let mut pages = pages.lock().unwrap();
    pages.insert(format!("/download/{name}"), package.to_vec());
    if let Some(signature) = signature {
        let signature_name = format!("{name}.sig");
        assets.push(json!({
            "name":signature_name,"size":signature.len(),
            "browser_download_url":format!("http://{api}/download/{signature_name}"),
        }));
        pages.insert(format!("/download/{signature_name}"), signature);
    }
    let release = json!({"tag_name":format!("v{version}"),"draft":false,"prerelease":false,
        "html_url":"https://github.com/example/release","body":"Notes","assets":assets});
    pages.insert(
        "/repos/marselnenaj/tomacco/releases/latest".into(),
        release.to_string().into_bytes(),
    );
}

/// Where the sandbox lives. It holds several copies of the debug binary,
/// more than a small /tmp may have room for, so without a TMPDIR it goes to
/// Cargo's own scratch directory in target/.
fn sandbox_directory() -> tempfile::TempDir {
    match std::env::var_os("TMPDIR").filter(|value| Path::new(value).is_absolute()) {
        Some(_) => tempfile::tempdir(),
        None => tempfile::tempdir_in(env!("CARGO_TARGET_TMPDIR")),
    }
    .unwrap()
}

#[test]
fn the_sandbox_follows_tmpdir_or_stays_in_target() {
    let directory = sandbox_directory();
    let expected = match std::env::var_os("TMPDIR").filter(|value| Path::new(value).is_absolute()) {
        Some(value) => PathBuf::from(value),
        None => PathBuf::from(env!("CARGO_TARGET_TMPDIR")),
    };
    assert_eq!(
        directory.path().parent().unwrap().canonicalize().unwrap(),
        expected.canonicalize().unwrap()
    );
}

#[test]
fn install_update_and_roll_back_through_the_real_binary() {
    if Command::new("python3").arg("--version").output().is_err() {
        eprintln!("skipped: the installer needs Python");
        return;
    }
    let directory = sandbox_directory();
    let home = directory.path().join("home");
    let data = directory.path().join("data");
    fs::create_dir_all(&home).unwrap();
    let key = SigningKey::from_bytes(&[42; 32]);
    let sign = |bytes: &[u8]| STANDARD.encode(key.sign(bytes).to_bytes()).into_bytes();
    let pages: Pages = Arc::default();
    let address = serve(pages.clone());
    let sandbox = Sandbox {
        home: home.clone(),
        data,
        api: format!("http://{address}"),
        public_key: STANDARD.encode(key.verifying_key().to_bytes()),
        _directory: directory,
    };

    // Install the old version from its package, with its own installer.
    let trees = sandbox._directory.path().join("trees");
    let arch = std::env::consts::ARCH;
    let old = trees.join(format!("tomacco-1.0.0-Linux-{arch}"));
    package_tree(&old, "1.0.0");
    let installed = Command::new("python3")
        .arg(old.join("install.py"))
        .args(["--no-window-rule", "--user-home"])
        .arg(&home)
        .env("HOME", &home)
        .output()
        .unwrap();
    assert!(
        installed.status.success(),
        "{}",
        String::from_utf8_lossy(&installed.stderr)
    );
    assert_eq!(sandbox.version(), "1.0.0");
    let library = home.join(".local/lib/tomacco");
    let old_binary = fs::read(library.join("tomaccoctl")).unwrap();

    // Nothing published: the private repository's 404, in plain words.
    let found = sandbox.ok("update.check", json!({"manual":true}));
    assert_eq!(found["state"], "no_release", "{found}");
    assert_eq!(found["install"]["kind"], "user_install", "{found}");
    assert_eq!(found["can_apply"], true, "{found}");

    let new = trees.join(format!("tomacco-1.0.1-Linux-{arch}"));
    package_tree(&new, "1.0.1");
    let good = package(&new);

    // Unsigned: refused, and the old version stays.
    publish(&pages, &address, "1.0.1", &good, None);
    let job = sandbox.apply();
    assert_eq!(job["state"], "failed", "{job}");
    assert!(sandbox.log(&job).contains("no signature"));
    assert_eq!(sandbox.version(), "1.0.0");

    // Tampered: the signature is of other bytes.
    publish(&pages, &address, "1.0.1", &good, Some(sign(b"other bytes")));
    let job = sandbox.apply();
    assert_eq!(job["state"], "failed", "{job}");
    assert!(sandbox.log(&job).contains("does not match"));
    assert_eq!(sandbox.version(), "1.0.0");

    // Signed, but its new program does not start once installed: the real
    // installer installs it, the health check fails, the journal restores.
    let broken = trees.join(format!("broken/tomacco-1.0.1-Linux-{arch}"));
    package_tree(&broken, "1.0.1");
    let script = fs::read_to_string(broken.join("install.py")).unwrap();
    let replaced = script.replace(
        "installer.write(app / \"tomaccoctl\", cli.read_bytes(), 0o755)",
        "installer.write(app / \"tomaccoctl\", b\"#!/bin/sh\\nexit 3\\n\", 0o755)",
    );
    assert_ne!(script, replaced);
    fs::write(broken.join("install.py"), replaced).unwrap();
    let bad = package(&broken);
    publish(&pages, &address, "1.0.1", &bad, Some(sign(&bad)));
    let job = sandbox.apply();
    assert_eq!(job["state"], "failed", "{job}");
    assert!(
        sandbox.log(&job).contains("Rolling back"),
        "{}",
        sandbox.log(&job)
    );
    assert_eq!(sandbox.version(), "1.0.0");
    assert_eq!(fs::read(library.join("tomaccoctl")).unwrap(), old_binary);
    let status = sandbox.ok("update.status", json!({}));
    assert_eq!(
        status["last_failure"]["code"], "update_install_failed",
        "{status}"
    );
    assert_eq!(status["rollback_available"], false);

    // Signed and sound: installed and journalled.
    publish(&pages, &address, "1.0.1", &good, Some(sign(&good)));
    let found = sandbox.ok("update.check", json!({"manual":true}));
    assert_eq!(found["state"], "update_available", "{found}");
    assert_eq!(found["latest"], "1.0.1");
    let job = sandbox.apply();
    assert_eq!(job["state"], "succeeded", "{}", sandbox.log(&job));
    assert_eq!(sandbox.version(), "1.0.1");
    assert_eq!(
        fs::read_to_string(library.join("VERSION")).unwrap(),
        "1.0.1\n"
    );
    let status = sandbox.ok("update.status", json!({}));
    assert_eq!(status["current"], "1.0.1", "{status}");
    assert_eq!(status["state"], "up_to_date", "{status}");
    assert_eq!(status["rollback_available"], true);
    assert_eq!(status["last_install"]["from"], "1.0.0");
    // Nothing of the download is left beside the installation.
    let left: Vec<_> = fs::read_dir(home.join(".local/lib"))
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    assert_eq!(left, ["tomacco"], "{left:?}");

    // Back: the new binary runs the journal of the installation that made it.
    let undone = sandbox.ok("update.rollback", json!({}));
    assert_eq!(undone["current"], "1.0.0", "{undone}");
    assert_eq!(sandbox.version(), "1.0.0");
    assert_eq!(fs::read(library.join("tomaccoctl")).unwrap(), old_binary);
    assert_eq!(
        fs::read_to_string(library.join("VERSION")).unwrap(),
        "1.0.0\n"
    );
    let status = sandbox.ok("update.status", json!({}));
    assert_eq!(status["rollback_available"], false);
    let again = sandbox.call("update.rollback", json!({}));
    assert_eq!(again["error"]["code"], "update_nothing_to_roll_back");

    // Off refuses an installation nobody asked for, and asks GitHub nothing.
    let waiting = sandbox.call("settings.configure", json!({"updates":{"mode":"off"}}));
    assert_eq!(waiting["ok"], true, "{waiting}");
    assert_eq!(
        sandbox.call("update.apply", json!({}))["error"]["code"],
        "update_off"
    );
    pages.lock().unwrap().clear();
    let before = sandbox.ok("update.status", json!({}))["checked_at"].clone();
    sandbox.ok("update.check", json!({}));
    assert_eq!(sandbox.ok("update.status", json!({}))["checked_at"], before);
}
