//! iOS from Linux through a pinned checkout of Joshua Warren's MIT-licensed
//! omarchy-apple-dev. Tomacco carries none of its code. `ios.toolkit_install`
//! fetches one commit on request into Tomacco's data directory, and these
//! operations call its scripts with HOME pointing there, so that they cannot
//! replace the user's xtool, SDK or sign-in. Nothing here downloads from Apple,
//! and upstream's no-Xcode mode, which can, is not offered.
use crate::{core::*, ios_emulator, jobs, projects, toolchain as tc};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    io::Read,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
};

/// Keep equal to UPSTREAM and REVISION in tools/install_apple_dev.py, the
/// recipe that fetches it; tests/python/test_apple_dev.py compares them.
const REPOSITORY: &str = "https://github.com/joshuaswarren/omarchy-apple-dev";
const REVISION: &str = "79bda716bf207924efc25bce923bda261dc67837";

const OWN_PACKAGES: &str = "Tomacco never runs sudo or an AUR helper. Install it yourself, for example on Arch: sudo pacman -S --needed";
const TOOLKIT_REMEDY: &str = "Call ios.toolkit_install. It fetches the pinned checkout into Tomacco's data directory and runs nothing else unless you pass toolchain";
const TOOLCHAIN_REMEDY: &str = "Call ios.toolkit_install with toolchain:true and xcode_xip naming an Xcode archive you downloaded from Apple yourself";

fn root() -> Result<PathBuf> {
    Ok(home()?.join("apple-dev"))
}
fn checkout() -> Result<PathBuf> {
    Ok(root()?.join("checkout").join(REVISION))
}
fn kit_home() -> Result<PathBuf> {
    Ok(root()?.join("home"))
}
fn sdk_bundle() -> Result<PathBuf> {
    Ok(kit_home()?.join(".config/swiftpm/swift-sdks/darwin.artifactbundle"))
}
/// The bin directory of the toolchain copy `ios.toolkit_install` made.
fn swift_bin() -> Result<Option<PathBuf>> {
    let pattern = format!(
        "{}/.local/share/tomacco-swift/*/usr/bin",
        glob::Pattern::escape(&kit_home()?.to_string_lossy())
    );
    Ok(glob_paths(&pattern)?
        .into_iter()
        .find(|bin| tc::executable(&bin.join("swift"))))
}
/// The checkout, or the error that says how to get it.
fn script(relative: &str) -> Result<PathBuf> {
    let file = checkout()?.join(relative);
    if !file.is_file() {
        return Err(Error::new(
            "toolkit_missing",
            format!("The pinned omarchy-apple-dev checkout has no {relative}"),
        )
        .remedy(TOOLKIT_REMEDY));
    }
    Ok(file)
}

fn installed_python_tool(relative: &str, arguments: Vec<String>) -> Result<Vec<String>> {
    let mut args = vec![
        tc::find("python3")?,
        "-I".into(),
        script(relative)?.to_string_lossy().into_owned(),
    ];
    args.extend(arguments);
    Ok(args)
}

/// The environment of every script from the checkout: the private HOME, the
/// private toolchain first, and a `sudo` that refuses.
fn kit_environment() -> Result<BTreeMap<String, String>> {
    let mut env = tc::environment()?;
    let kit = kit_home()?;
    let mut bins = vec![root()?.join("guard")];
    bins.extend(swift_bin()?);
    bins.push(kit.join(".local/bin"));
    bins.extend(std::env::split_paths(&env["PATH"]));
    env.insert(
        "PATH".into(),
        std::env::join_paths(bins)
            .map_err(|e| Error::new("invalid_path", e.to_string()))?
            .to_string_lossy()
            .into_owned(),
    );
    let text = kit.to_string_lossy();
    env.insert("HOME".into(), text.to_string());
    env.insert("XDG_CONFIG_HOME".into(), format!("{text}/.config"));
    env.insert("XDG_CACHE_HOME".into(), format!("{text}/.cache"));
    env.insert("XDG_DATA_HOME".into(), format!("{text}/.local/share"));
    env.insert("OMARCHY_APPLE_MODE".into(), "full".into());
    Ok(env)
}

/// What the machine must already have: (name, found, packages that provide it).
fn system_tools() -> Result<Vec<(&'static str, bool, &'static str)>> {
    let mut rows = Vec::new();
    for (name, packages) in [
        ("git", "git"),
        ("cc", "base-devel"),
        ("zip", "zip"),
        ("python3", "python"),
        ("file", "file"),
        ("rsync", "rsync"),
        ("llvm-strip", "llvm"),
        ("llvm-lipo", "llvm"),
        ("llvm-otool", "llvm"),
        ("llvm-install-name-tool", "llvm"),
        ("llvm-ar", "llvm"),
    ] {
        rows.push((name, tc::managed_which(name)?.is_some(), packages));
    }
    let libraries = match tc::managed_which("pkg-config")? {
        Some(tool) => tc::run(
            &argv(&[
                &tool.to_string_lossy(),
                "--exists",
                "libimobiledevice-1.0",
                "openssl",
            ]),
            10,
            None,
            false,
        )
        .is_ok_and(|done| done["exit_code"] == 0),
        None => false,
    };
    rows.push(("libimobiledevice", libraries, "libimobiledevice openssl"));
    Ok(rows)
}
fn missing_system(tools: &[&str]) -> Result<Vec<Value>> {
    Ok(system_tools()?
        .into_iter()
        .filter(|(name, found, _)| !found && tools.contains(name))
        .map(|(name, _, packages)| {
            json!({"name":name,"remedy":format!("{OWN_PACKAGES} {packages}")})
        })
        .collect())
}
const TOOLCHAIN_NEEDS: &[&str] = &["git", "cc", "zip", "python3", "libimobiledevice"];
const FLUTTER_NEEDS: &[&str] = &[
    "python3",
    "zip",
    "file",
    "rsync",
    "llvm-strip",
    "llvm-lipo",
    "llvm-otool",
    "llvm-install-name-tool",
    "llvm-ar",
];
fn require_system(tools: &[&str]) -> Result<()> {
    let missing = missing_system(tools)?;
    if missing.is_empty() {
        return Ok(());
    }
    let names = missing
        .iter()
        .map(|row| s(row, "name"))
        .collect::<Vec<_>>()
        .join(", ");
    let remedy = s(&missing[0], "remedy").to_string();
    Err(
        Error::new("system_packages_missing", format!("Missing: {names}"))
            .remedy(remedy)
            .details(json!({"missing":missing})),
    )
}

/// The SDK stamp the pinned xtool fork writes and requires, read from the checkout.
fn expected_stamp(checkout: &Path) -> Option<String> {
    fs::read_to_string(checkout.join("install-toolchain.sh"))
        .ok()?
        .lines()
        .find_map(|line| line.strip_prefix("XTOOL_SDK_VERSION=\""))
        .and_then(|rest| rest.strip_suffix('"'))
        .map(str::to_owned)
}
/// Whether the Flutter install carries the gen_snapshot flutter/setup.sh builds.
fn flutter_prepared(flutter: &str) -> bool {
    let Some(flutter_root) = Path::new(flutter)
        .canonicalize()
        .ok()
        .and_then(|file| Some(file.parent()?.parent()?.to_path_buf()))
    else {
        return false;
    };
    let mut magic = [0u8; 4];
    fs::File::open(flutter_root.join("bin/cache/artifacts/engine/ios-release/gen_snapshot_arm64"))
        .and_then(|mut file| file.read_exact(&mut magic))
        .is_ok()
        && &magic == b"\x7fELF"
}

/// The `linux_toolkit` section of ios.doctor. It starts and fetches nothing,
/// and what it lacks never counts against the iPhone route's own `ready`.
pub fn status() -> Result {
    let checkout = checkout()?;
    let kit = kit_home()?;
    let installed = checkout.join("install-toolchain.sh").is_file();
    let xtool = kit.join(".local/bin/xtool");
    let rcodesign = kit.join(".local/bin/rcodesign");
    let python = kit.join("pymobile3-venv/bin/python");
    let swift = swift_bin()?.map(|bin| bin.join("swift"));
    let toolchain = tc::executable(&xtool)
        && tc::executable(&rcodesign)
        && tc::executable(&python)
        && swift.is_some();
    let bundle = sdk_bundle()?;
    let stamp = fs::read_to_string(bundle.join("darwin-sdk-version.txt"))
        .ok()
        .map(|text| text.trim().to_owned());
    let expected = expected_stamp(&checkout);
    let actool = bundle.join("Developer/Platforms/iPhoneOS.platform/Developer/usr/bin/actool");
    let sdk = stamp.is_some() && stamp == expected && tc::executable(&actool);
    let flutter = tc::find_optional("flutter")?;
    let prepared = flutter.as_deref().is_some_and(flutter_prepared);
    let mut missing = Vec::new();
    if !installed {
        missing.push(json!({"name":"checkout","remedy":TOOLKIT_REMEDY}));
    }
    if !toolchain {
        missing.push(json!({"name":"toolchain","remedy":TOOLCHAIN_REMEDY}));
    }
    if !sdk {
        missing.push(json!({"name":"sdk","remedy":TOOLCHAIN_REMEDY}));
    }
    if tc::find_optional("swift")?.is_none() {
        missing.push(json!({"name":"swift","remedy":"Install Swift 6.4 from https://www.swift.org/install/linux/ (Tomacco does not install it, and upstream's route through the AUR's swift-bin needs an AUR helper you run yourself)"}));
    }
    if flutter.is_none() {
        missing.push(json!({"name":"flutter","remedy":"Install Flutter yourself and set its path with settings.configure, for Flutter iOS builds"}));
    } else if !prepared {
        missing.push(json!({"name":"flutter_gen_snapshot","remedy":"Call ios.flutter_setup once per Flutter version, for Flutter iOS builds"}));
    }
    let needs: Vec<&str> = TOOLCHAIN_NEEDS
        .iter()
        .chain(FLUTTER_NEEDS)
        .copied()
        .collect();
    missing.extend(missing_system(&needs)?);
    Ok(json!({
        "repository":REPOSITORY,"commit":REVISION,"license":"MIT","installed":installed,"path":checkout,
        "private_home":kit,
        "toolchain":{"ready":toolchain,"xtool":xtool,"rcodesign":rcodesign,"python":python,"swift":swift},
        "sdk":{"ready":sdk,"path":bundle,"stamp":stamp,"required_stamp":expected,"actool":tc::executable(&actool),
            "separate_from_ios_sdk_install":true},
        "flutter":{"found":flutter.is_some(),"path":flutter,"gen_snapshot_prepared":prepared},
        "missing":missing,
        "install":{"operation":"ios.toolkit_install","automatic":false,
            "runs":["git fetch of the pinned commit into path",
                "with toolchain:true only: its install-toolchain.sh --mode full --user-only, under private_home"],
            "downloads_from_apple":false},
    }))
}

fn toolkit_install(a: &Value) -> Result {
    let wants_toolchain = b(a, "toolchain");
    let xip = s(a, "xcode_xip");
    if !xip.is_empty() && !wants_toolchain {
        return Err(Error::new(
            "invalid_argument",
            "xcode_xip is only used with toolchain:true",
        ));
    }
    tc::find("git")?;
    let root = root()?;
    private_dir(&root)?;
    let mut args = vec![
        tc::find("python3")?,
        ios_emulator::research::script("install_apple_dev.py")?
            .to_string_lossy()
            .into_owned(),
        "--root".into(),
        root.to_string_lossy().into_owned(),
    ];
    let mut timeout = 900;
    if wants_toolchain {
        tc::find("swift")?;
        require_system(TOOLCHAIN_NEEDS)?;
        args.push("--toolchain".into());
        timeout = 7200;
        if !xip.is_empty() {
            let source = expand(xip);
            let source = path(xip, source.is_dir(), true)?;
            args.extend(["--xcode-xip".into(), source.to_string_lossy().into_owned()]);
        }
    }
    jobs::submit(
        "Install iOS toolkit",
        json!({"steps":[{"argv":args}],"cwd":root,"env":tc::environment()?,"timeout":timeout,
            "exclusive":"apple-dev:install","single_active":true,
            "operation_key":{"operation":"ios.toolkit_install","arguments":a},
            "always_artifacts":["install-report.json"],
            "metadata":{"repository":REPOSITORY,"commit":REVISION,"downloads_from_apple":false}}),
    )
}

fn toolchain_ready() -> Result<()> {
    let state = status()?;
    if !b(&state["toolchain"], "ready") {
        return Err(Error::new(
            "toolkit_toolchain_missing",
            "The private omarchy-apple-dev toolchain is not installed",
        )
        .remedy(TOOLCHAIN_REMEDY)
        .details(json!({"toolchain":state["toolchain"]})));
    }
    if !b(&state["sdk"], "ready") {
        return Err(Error::new(
            "toolkit_sdk_missing",
            "The private toolchain has no current SDK. An SDK from ios.sdk_install does not count: the pinned xtool fork refuses its older stamp",
        )
        .remedy(TOOLCHAIN_REMEDY)
        .details(json!({"sdk":state["sdk"]})));
    }
    Ok(())
}

fn flutter_setup(_: &Value) -> Result {
    let setup = script("flutter/setup.sh")?;
    let flutter = tc::find("flutter")?;
    tc::find("git")?;
    let mut env = kit_environment()?;
    env.insert("FLUTTER".into(), flutter);
    jobs::submit(
        "Prepare Flutter for iOS on Linux",
        json!({"steps":[{"argv":["bash",setup]}],"cwd":checkout()?,"env":env,"timeout":7200,
            "exclusive":"apple-dev:flutter-setup","single_active":true,
            "operation_key":{"operation":"ios.flutter_setup"},
            "metadata":{"tested":false,"modifies_flutter_install":true}}),
    )
}

fn flutter_build(a: &Value) -> Result {
    let project = projects::inspect(s(a, "path"))?;
    let app = PathBuf::from(s(&project, "path"));
    let is_flutter = project["kinds"]
        .as_array()
        .is_some_and(|kinds| kinds.iter().any(|kind| kind == "flutter"));
    if !is_flutter || !app.join("ios").is_dir() {
        return Err(Error::new(
            "flutter_ios_missing",
            "This is not a Flutter project with an ios directory",
        )
        .remedy("Run flutter create --platforms ios . in the project"));
    }
    let build = script("flutter/build.sh")?;
    toolchain_ready()?;
    require_system(FLUTTER_NEEDS)?;
    let flutter = tc::find("flutter")?;
    if !flutter_prepared(&flutter) {
        return Err(Error::new(
            "flutter_setup_required",
            "This Flutter install still has Apple's macOS gen_snapshot",
        )
        .remedy("Call ios.flutter_setup once per Flutter version"));
    }
    let mut args = vec!["bash".to_string(), build.to_string_lossy().into_owned()];
    args.push(app.to_string_lossy().into_owned());
    for define in strings(&a["defines"]) {
        identifier(
            &define,
            "Dart define",
            Some(r"[A-Za-z_][A-Za-z0-9_]*=[^\x00]*"),
        )?;
        args.push(define);
    }
    let mut env = kit_environment()?;
    env.insert("FLUTTER".into(), flutter);
    let bundle = s(a, "bundle_id");
    if !bundle.is_empty() {
        identifier(
            bundle,
            "bundle ID",
            Some(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+"),
        )?;
        env.insert("BUNDLE_ID".into(), bundle.into());
    }
    for (key, name) in [("flavor", "FLAVOR"), ("target", "TARGET")] {
        if !s(a, key).is_empty() {
            env.insert(name.into(), s(a, key).into());
        }
    }
    let out = app.join("build/ios-linux");
    jobs::submit(
        &format!("Build Flutter iOS · {}", s(&project, "name")),
        json!({"steps":[{"argv":args}],"cwd":app,"env":env,"timeout":a["timeout"],
            "exclusive":app,"single_active":true,
            "operation_key":{"operation":"ios.flutter_build","arguments":a},
            "artifacts":["build/ios-linux/Runner.ipa"],
            "metadata":{"ipa":out.join("Runner.ipa"),"app":out.join("Payload/Runner.app"),"signed":false,
                "tested":false,"note":"Release mode, arm64 device build; not a virtual-device build"}}),
    )
}

fn import_xcode(a: &Value) -> Result {
    let project = path(s(a, "project"), true, true)?;
    if project.extension().is_none_or(|e| e != "xcodeproj")
        || !project.join("project.pbxproj").is_file()
    {
        return Err(Error::new(
            "invalid_xcode_project",
            format!("{} is not an .xcodeproj directory", project.display()),
        ));
    }
    let out = if s(a, "out").is_empty() {
        project.parent().unwrap().join("omarchy-xtool")
    } else {
        path(s(a, "out"), false, false)?
    };
    let mut arguments = vec![
        project.to_string_lossy().into_owned(),
        "--out".to_string(),
        out.to_string_lossy().into_owned(),
    ];
    if !s(a, "target").is_empty() {
        arguments.extend(["--target".into(), s(a, "target").into()]);
    }
    if !s(a, "bundle_id").is_empty() {
        identifier(
            s(a, "bundle_id"),
            "bundle ID",
            Some(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+"),
        )?;
        arguments.extend(["--bundle-id".into(), s(a, "bundle_id").into()]);
    }
    let args = installed_python_tool("tools/xcodeproj2xtool.py", arguments)?;
    let done = tc::run(&args, 300, None, false)?;
    let output = format!("{}\n{}", s(&done, "stdout"), s(&done, "stderr"));
    let warnings: Vec<&str> = output
        .lines()
        .filter(|line| line.contains("warning:"))
        .collect();
    if done["exit_code"] != 0 || !out.join("xtool.yml").is_file() {
        return Err(Error::new(
            "xcode_import_failed",
            output.trim().chars().take(4000).collect::<String>(),
        )
        .remedy("Read the reason above; pass target if the project has several application targets")
        .details(json!({"exit_code":done["exit_code"]})));
    }
    Ok(
        json!({"project":project,"adapter":out,"warnings":warnings,"output":output.trim(),
        "next":{"operation":"project.execute","arguments":{"path":out,"platform":"ios"}}}),
    )
}

/// One structured frame list and the summary lines of ips-symbolicate.py.
fn parse_report(text: &str) -> Value {
    let mut report = json!({"frames":[],"last_exception_backtrace":[]});
    let mut section = "frames";
    for line in text.lines() {
        if line == "Last exception backtrace:" {
            section = "last_exception_backtrace";
        } else if let Some(frame) = line.strip_prefix("  ") {
            let frame = frame
                .trim_start_matches(|c: char| c.is_ascii_digit())
                .trim();
            report[section].as_array_mut().unwrap().push(json!(frame));
        } else if let Some((key, value)) = line.split_once(": ") {
            let name = match key {
                "App" => "app",
                "OS" => "os",
                "Incident" => "incident",
                "Exception" => "exception",
                "Termination" => "termination",
                "Faulting thread" => "faulting_thread",
                _ => continue,
            };
            report[name] = json!(value);
        }
    }
    report
}

fn crash_symbolicate(a: &Value) -> Result {
    let report = path(s(a, "report"), false, true)?;
    let mut arguments = vec![report.to_string_lossy().into_owned()];
    for binary in strings(&a["binaries"]) {
        arguments.push("--binary".into());
        arguments.push(path(&binary, false, true)?.to_string_lossy().into_owned());
    }
    let mut args = installed_python_tool("tools/ips-symbolicate.py", arguments)?;
    // swift-demangle comes with the toolchain, which is rarely on PATH by itself.
    if let Some(bin) = swift_bin()? {
        let mut command = vec![
            tc::which("env")
                .ok_or_else(|| Error::new("tool_missing", "env is missing"))?
                .to_string_lossy()
                .into_owned(),
            format!(
                "PATH={}:{}",
                bin.display(),
                std::env::var("PATH").unwrap_or_default()
            ),
        ];
        command.append(&mut args);
        args = command;
    }
    let done = tc::run(&args, 120, None, false)?;
    if done["exit_code"] != 0 {
        return Err(Error::new(
            "crash_report_unreadable",
            s(&done, "stderr")
                .trim()
                .chars()
                .take(2000)
                .collect::<String>(),
        )
        .remedy("Pass a modern JSON .ips report; the older plain-text form is not read"));
    }
    let text = s(&done, "stdout");
    let mut result = parse_report(text);
    let frames = result["frames"].as_array().unwrap();
    let extra = result["last_exception_backtrace"].as_array().unwrap();
    let resolved = frames
        .iter()
        .chain(extra)
        .filter(|frame| frame.as_str().unwrap_or("").contains("[resolved locally]"))
        .count();
    result["resolved_locally"] = json!(resolved);
    result["warnings"] = json!(s(&done, "stderr").lines().collect::<Vec<_>>());
    result["output"] = json!(text);
    Ok(result)
}

/// A .p8 key: an owner-only regular file. Only its path travels; its content
/// is read by upstream's asc.py alone, to sign the App Store Connect token.
fn store_key(value: &str) -> Result<PathBuf> {
    let file = secret_path(value, "key_path")?;
    let metadata = fs::metadata(&file)?;
    if metadata.permissions().mode() & 0o077 != 0 {
        return Err(Error::new(
            "secret_permissions",
            "The App Store Connect .p8 key must be readable only by its owner",
        )
        .remedy("chmod 600 the file"));
    }
    if !metadata.is_file() || metadata.len() > 16 * 1024 {
        return Err(Error::new(
            "invalid_secret",
            "Name a small .p8 private key file",
        ));
    }
    Ok(file)
}

fn store_ship(a: &Value) -> Result {
    let project = projects::inspect(s(a, "path"))?;
    let app = PathBuf::from(s(&project, "path"));
    if !project["kinds"]
        .as_array()
        .is_some_and(|kinds| kinds.iter().any(|kind| kind == "xtool"))
    {
        return Err(
            Error::new("xtool_project_required", "ship.sh builds xtool projects")
                .remedy("Create one with project.create kind xtool, or use project.import_xcode"),
        );
    }
    let ship = script("ship.sh")?;
    toolchain_ready()?;
    require_system(&["zip"])?;
    let (key_id, issuer, key) = (s(a, "key_id"), s(a, "issuer_id"), s(a, "key_path"));
    let given = [key_id, issuer, key]
        .iter()
        .filter(|v| !v.is_empty())
        .count();
    if given != 0 && given != 3 {
        return Err(Error::new(
            "store_key_incomplete",
            "key_id, issuer_id and key_path go together",
        ));
    }
    if b(a, "upload") && given == 0 {
        return Err(Error::new(
            "store_key_required",
            "An upload needs key_id, issuer_id and key_path",
        )
        .remedy("Create a team key under Users and Access > Integrations in App Store Connect, and the app record once"));
    }
    let mut env = kit_environment()?;
    if given == 3 {
        identifier(key_id, "App Store Connect key ID", Some("[A-Z0-9]{10}"))?;
        identifier(
            issuer,
            "App Store Connect issuer ID",
            Some("[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"),
        )?;
        env.insert("ASC_KEY_ID".into(), key_id.into());
        env.insert("ASC_ISSUER_ID".into(), issuer.into());
        env.insert(
            "ASC_KEY_PATH".into(),
            store_key(key)?.to_string_lossy().into_owned(),
        );
    }
    let mut args = vec!["bash".to_string(), ship.to_string_lossy().into_owned()];
    if b(a, "upload") {
        args.push("--upload".into());
    }
    jobs::submit(
        &format!("Prepare for App Store · {}", s(&project, "name")),
        json!({"steps":[{"argv":args}],"cwd":app,"env":env,"timeout":a["timeout"],
            "exclusive":app,"single_active":true,
            "operation_key":{"operation":"ios.store_ship","arguments":{"path":a["path"],"upload":a["upload"]}},
            "artifacts":["xtool/*.ipa"],
            "metadata":{"identity":if given==3{"apple-distribution"}else{"test"},"uploaded":b(a,"upload"),
                "submitted_for_review":false,"live_upload_qualified":false}}),
    )
}

/// The built .app of an xtool project, and whether the virtual device's bundle
/// rules accept it. Reads files only: no virtual device is used.
fn app_product(a: &Value) -> Result {
    let project = projects::inspect(s(a, "path"))?;
    let root = PathBuf::from(s(&project, "path"));
    let apps = glob_paths(&format!(
        "{}/xtool/*.app",
        glob::Pattern::escape(&root.to_string_lossy())
    ))?;
    let [app] = apps.as_slice() else {
        return Err(Error::new(
            "app_not_built",
            format!(
                "Expected one xtool/*.app in {}, found {}",
                root.display(),
                apps.len()
            ),
        )
        .remedy(
            "Run project.execute with platform ios for an xtool project, and wait for its job",
        ));
    };
    match ios_emulator::virtual_phone::inspect_application(app, None) {
        Ok(found) => {
            let id = s(&found, "bundle_id");
            Ok(json!({"app":app,"bundle_id":id,"name":found["name"],
                "virtual_device":{"accepted":true},
                // No reboot when the phone runs: this one call does what `next` does.
                "next_on_a_running_phone":{"operation":"ios.virtual.deploy","arguments":{"bundle":app}},
                "next":[
                    {"operation":"ios.virtual.configure","arguments":{"add_application":app}},
                    {"operation":"ios.virtual.configure","arguments":{"application":id}},
                    {"operation":"ios.virtual.start","arguments":{}}]}))
        }
        Err(error) => {
            Ok(json!({"app":app,"virtual_device":{"accepted":false,"error":error.value()}}))
        }
    }
}

pub fn handle(op: &str, a: &Value) -> Result {
    match op {
        "ios.toolkit_install" => toolkit_install(a),
        "ios.flutter_setup" => flutter_setup(a),
        "ios.flutter_build" => flutter_build(a),
        "ios.crash_symbolicate" => crash_symbolicate(a),
        "ios.store_ship" => store_ship(a),
        "project.import_xcode" => import_xcode(a),
        "project.app" => app_product(a),
        _ => Err(Error::new("operation_not_found", op)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_report_becomes_summary_and_two_frame_lists() {
        let report = parse_report(
            "App: Demo 1.2 (dev.demo)\nOS: iPhone OS 27.0\nException: EXC_CRASH (SIGABRT)\nFaulting thread: 0 of 2\n  0  Demo  main+4 [resolved locally]\n  1  dyld  start\nLast exception backtrace:\n  0  CoreFoundation  raise+8\n",
        );
        assert_eq!(report["app"], "Demo 1.2 (dev.demo)");
        assert_eq!(report["faulting_thread"], "0 of 2");
        assert_eq!(report["frames"][0], "Demo  main+4 [resolved locally]");
        assert_eq!(report["frames"].as_array().unwrap().len(), 2);
        assert_eq!(
            report["last_exception_backtrace"][0],
            "CoreFoundation  raise+8"
        );
    }
}
