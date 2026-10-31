//! `tomaccoctl setup`: a readable check of this machine that offers to fix what
//! it can. It changes nothing without a yes, from the person or from `--yes`.
use crate::api;
use crate::core::*;
use crate::product;
use serde_json::{Value, json};
use std::io::{BufRead, IsTerminal, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

const REPOSITORY_INDEX: &str = "https://dl.google.com/android/repository/repository2-3.xml";
const REPOSITORY: &str = "https://dl.google.com/android/repository/";
const SYSTEM_IMAGE: &str = "system-images;android-36;google_apis;x86_64";
const SDK_PACKAGES: [&str; 5] = [
    "platform-tools",
    "emulator",
    "platforms;android-36",
    "build-tools;36.0.0",
    SYSTEM_IMAGE,
];
const EMULATOR_NAME: &str = "Tomacco";
const EMULATOR_PROFILE: &str = "pixel-11";

/// Agents Tomacco can register with, and how each one adds a local server.
const AGENTS: [(&str, &str, &[&str]); 2] = [
    (
        "Claude Code",
        "claude",
        &[
            "mcp",
            "add",
            "--transport",
            "stdio",
            "--scope",
            "user",
            "tomacco",
            "--",
        ],
    ),
    ("Codex", "codex", &["mcp", "add", "tomacco", "--"]),
];

fn tool<'a>(doctor: &'a Value, name: &str) -> Option<&'a Value> {
    doctor["tools"]
        .as_array()?
        .iter()
        .find(|tool| tool["name"] == name)
}
fn has(doctor: &Value, name: &str) -> bool {
    tool(doctor, name).is_some_and(|tool| tool["available"] == true)
}

/// One line per thing a person cares about: what works, and if not, what would fix it.
fn report(doctor: &Value) -> Vec<(bool, String)> {
    let mut lines = Vec::new();
    let mut line = |ok: bool, text: &str| lines.push((ok, text.to_string()));
    line(
        has(doctor, "java"),
        "Java, for Android builds: install a JDK 17 or newer",
    );
    line(
        has(doctor, "gradle"),
        "Gradle, for new Android projects: install gradle",
    );
    line(
        has(doctor, "sdkmanager"),
        "Android SDK: `tomaccoctl setup` can install it",
    );
    line(
        has(doctor, "adb") && has(doctor, "emulator"),
        "Android device tools: `tomaccoctl setup` can install them",
    );
    line(
        doctor["kvm"]["accessible"] == true,
        "KVM, for a fast Android emulator: enable virtualization and join the kvm group",
    );
    line(
        has(doctor, "swift"),
        "Swift, for iOS and Swift on Android: see docs/iphone.md",
    );
    line(
        has(doctor, "xtool"),
        "xtool, for iOS builds on Linux: see docs/iphone.md",
    );
    lines
}

fn print_report(lines: &[(bool, String)]) {
    for (ok, text) in lines {
        // The advice after the colon only matters while something is missing.
        let name = text.split(':').next().unwrap_or(text);
        if *ok {
            println!("  ok       {name}");
        } else {
            println!("  missing  {text}");
        }
    }
}

/// What is left to do, in order, for an agent setting Tomacco up for a person. `ask_first`
/// means the step needs the person's yes: it costs them a download, a license or a password.
fn next_steps(doctor: &Value, emulator: bool, virtual_ios: &Value) -> Vec<Value> {
    let mut steps = Vec::new();
    let mut step = |goal: &str, what: &str, how: String, ask_first: bool| {
        steps.push(json!({"goal":goal,"what":what,"how":how,"ask_first":ask_first}))
    };
    if doctor["desktop"]["loadable"] == false {
        let desktop = &doctor["desktop"];
        let missing: Vec<String> = strings(&desktop["missing_libraries"])
            .into_iter()
            .chain(strings(&desktop["missing_versions"]))
            .collect();
        step(
            "desktop",
            &format!(
                "The desktop at {} cannot start on this system; the loader is missing {}",
                s(desktop, "path"),
                missing.join(", ")
            ),
            format!(
                "The release desktop needs {}. Update the system (on Arch: sudo pacman -Syu), or build the desktop from a checkout on this system: {DOCS}/getting-started.md#debian-ubuntu-and-fedora. tomaccoctl itself works without it.",
                crate::desktop::RELEASE_REQUIREMENTS
            ),
            true,
        );
    }
    if !has(doctor, "java") || !has(doctor, "gradle") {
        step(
            "android",
            "Install a JDK 17 or newer and Gradle",
            "On Arch: sudo pacman -S --needed jdk-openjdk gradle".to_string(),
            true,
        );
    }
    if !has(doctor, "sdkmanager") || !has(doctor, "adb") || !has(doctor, "emulator") || !emulator {
        step(
            "android",
            "Install the Android SDK and create an emulator",
            "Run: tomaccoctl setup --yes. It downloads about 5 GB and accepts Google's Android SDK license, so tell the person that first.".to_string(),
            true,
        );
    }
    if !has(doctor, "swift") || !has(doctor, "xtool") {
        step(
            "ios",
            "Set up iOS builds on Linux",
            format!("Follow {DOCS}/iphone.md. Only the person can sign in to an Apple account."),
            true,
        );
    }
    steps.extend(virtual_ios_steps(virtual_ios));
    steps
}

const DOCS: &str = "https://github.com/marselnenaj/tomacco/blob/main/docs";

/// The virtual iOS device's setup chain, in order, as far as cheap checks of
/// this machine can tell: each stage with `done` true, false, or null when
/// only the person's own files or a later check can say. The stages that
/// build tools look in the checkout's `.tooling` (null without a checkout).
fn virtual_ios_chain(tooling: Option<&Path>) -> Vec<Value> {
    let exists = |relative: &[&str]| -> Value {
        tooling.map_or(Value::Null, |root| {
            json!(relative.iter().all(|path| root.join(path).is_file()))
        })
    };
    vec![
        json!({"stage":"engine","what":"Build the engine and the guest-control backend",
            "done":exists(&["ios-paravirt/sptm-build/qemu-system-aarch64",
                "ios-guest-control/vhost-device-vsock"]),
            "how":"In a Tomacco checkout: python3 tools/build_ios_paravirt.py --jobs 8 --cpu-optimization native, then python3 tools/build_ios_guest_control.py --output .tooling/ios-guest-control (setup.md, chain step 1). It builds for many minutes.",
            "ask_first":true}),
        json!({"stage":"helper","what":"Prepare the helper support and the linker",
            "done":exists(&["ios-sptm/build/qemu-system-x86_64","ios-sptm/support/busybox/busybox",
                "ios-sptm/support/apfs/apfs.ko","ios-linker/prefix/bin/aarch64-apple-darwin-ld"]),
            "how":"python3 tools/prepare_ios_emulator.py --firmware IPHONE_IPSW --output RESTORE_SHELL and python3 tools/build_ios_linker.py (setup.md, chain step 2). It needs the person's unpacked iPhone image.",
            "ask_first":true}),
        json!({"stage":"images","what":"The person's own restore images and IM4M",
            "done":null,
            "how":format!("Only the person can supply them: {DOCS}/virtual-ios/setup.md#obtain-the-apple-images and #how-do-i-get-the-im4m. Read the IM4M section before any download."),
            "ask_first":true}),
        json!({"stage":"chain","what":"Prepare the boot components, userland, restore, Cryptexes, guest tools, kernel and first install",
            "done":null,
            "how":format!("{DOCS}/virtual-ios/setup.md#the-chain, steps 3 to 10. The restore alone takes about half an hour; setup needs about 100 GiB."),
            "ask_first":true}),
        json!({"stage":"profile","what":"Write the profile",
            "done":false,
            "how":"python3 tools/ios_virtual_profile.py --check with the chain's outputs names each input still missing and the command that makes it; then the same with --directory writes the profile (setup.md, chain step 11).",
            "ask_first":false}),
        json!({"stage":"configure","what":"Select the profile",
            "done":false,
            "how":"tomaccoctl call ios.virtual.configure '{\"directory\":\"/absolute/profile\"}', then ios.virtual.start.",
            "ask_first":false}),
    ]
}

/// The steps for the virtual iOS device, from what `virtual_ios_summary`
/// found: without a profile, one step naming the chain's next stage with the
/// whole chain; with one, what keeps the next start from working.
fn virtual_ios_steps(summary: &Value) -> Vec<Value> {
    let mut steps = Vec::new();
    if summary["host_supported"] == false {
        return steps;
    }
    if let Some(error) = summary["status_error"].as_object() {
        steps.push(json!({"goal":"virtual_ios","what":"Fix the virtual iOS device profile",
            "how":format!("ios.virtual.status refuses it: {} {} Check the inputs with python3 tools/ios_virtual_profile.py --check.",
                error.get("message").and_then(Value::as_str).unwrap_or("unknown error"),
                error.get("remedy").and_then(Value::as_str).unwrap_or("")),
            "ask_first":false}));
        return steps;
    }
    if summary["configured"] != true {
        let chain = summary["chain"].as_array().cloned().unwrap_or_default();
        let next = chain
            .iter()
            .find(|stage| stage["done"] != true)
            .cloned()
            .unwrap_or(Value::Null);
        steps.push(json!({"goal":"virtual_ios",
            "what":format!("Set up the virtual iOS device, only if the person wants it. Next: {}",
                next["what"].as_str().unwrap_or("select the profile")),
            "how":format!("{} The whole chain is in chain, in order; {DOCS}/virtual-ios/setup.md explains it. Read its IM4M section before anything is built or downloaded: it says what is required and whether setup can finish. It needs about 100 GiB and a 16 GiB graphics card.",
                next["how"].as_str().unwrap_or("")),
            "ask_first":true,"next_stage":next["stage"],"chain":chain}));
        return steps;
    }
    let disk = &summary["start_disk"];
    if disk["enough"] == false {
        let short = disk["needed_bytes"].as_u64().unwrap_or(0).saturating_sub(
            disk["free_bytes"].as_u64().unwrap_or(0)
                + disk["reclaimable_bytes"].as_u64().unwrap_or(0),
        );
        steps.push(json!({"goal":"virtual_ios","what":"Free disk space for the next virtual iPhone start",
            "how":format!("The start needs {:.1} GiB more on the filesystem of {}. ios.virtual.cleanup lists older disk copies it can delete (a dry run first); anything else there is the person's to delete.",
                short as f64 / (1u64 << 30) as f64, disk["installs"].as_str().unwrap_or("installs")),
            "ask_first":true}));
    }
    if summary["phase"] == "failed" {
        steps.push(json!({"goal":"virtual_ios","what":"Read why the last virtual iPhone start failed",
            "how":format!("ios.virtual.status error: {}. {DOCS}/virtual-ios/troubleshooting.md has the causes and fixes.",
                summary["error"].as_str().unwrap_or("not recorded")),
            "ask_first":false}));
    }
    if let Some(error) = summary["applications_error"].as_str() {
        steps.push(json!({"goal":"virtual_ios","what":"The last virtual iPhone start installed no apps",
            "how":format!("{error}. {DOCS}/virtual-ios/troubleshooting.md#an-app-does-not-open says what to do."),
            "ask_first":false}));
    }
    steps
}

/// The `.tooling` directory of the source checkout this program belongs to,
/// where the chain's builds go; None for an installed program without one.
fn tooling() -> Option<PathBuf> {
    source_checkout().map(|root| root.join(".tooling"))
}

/// What the doctor knows about the virtual iOS device: the status of a
/// configured profile, the refusal of one that cannot be read, or the setup
/// chain of a machine without one.
fn virtual_ios_summary() -> Value {
    if std::env::consts::ARCH != "x86_64" {
        return json!({"host_supported":false});
    }
    match api::dispatch("ios.virtual.status", &json!({})) {
        Ok(status) if status["configured"] == true => json!({"configured":true,
            "start_disk":status["start_disk"],"phase":status["phase"],"error":status["error"],
            "applications_error":status["applications_error"]}),
        Ok(_) => json!({"configured":false,"chain":virtual_ios_chain(tooling().as_deref())}),
        Err(error) => json!({"configured":true,"status_error":error.value()}),
    }
}

/// The plan an agent reads from the `doctor` operation.
pub fn with_next_steps(mut doctor: Value) -> Value {
    let emulator = api::dispatch("android.avds", &json!({}))
        .is_ok_and(|avds| avds["avds"].as_array().is_some_and(|avds| !avds.is_empty()));
    doctor["next_steps"] = json!(next_steps(&doctor, emulator, &virtual_ios_summary()));
    doctor
}

struct Answers {
    yes: bool,
    interactive: bool,
}
impl Answers {
    fn ask(&self, question: &str) -> bool {
        if self.yes {
            println!("{question} yes");
            return true;
        }
        if !self.interactive {
            return false;
        }
        print!("{question} [Y/n] ");
        let _ = std::io::stdout().flush();
        let mut answer = String::new();
        if std::io::stdin().lock().read_line(&mut answer).is_err() {
            return false;
        }
        accepted(&answer)
    }
}
fn accepted(answer: &str) -> bool {
    matches!(
        answer.trim().to_lowercase().as_str(),
        "" | "y" | "yes" | "j" | "ja"
    )
}

fn quiet(program: &str, args: &[&str]) -> bool {
    Command::new(program)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|status| status.success())
}
fn on_path(program: &str) -> bool {
    std::env::var_os("PATH").is_some_and(|path| {
        std::env::split_paths(&path).any(|directory| directory.join(program).is_file())
    })
}

fn connect_agents(answers: &Answers) -> Result<()> {
    let program = std::env::current_exe()?;
    let program = program.to_string_lossy().into_owned();
    for (name, command, add) in AGENTS {
        if !on_path(command) {
            continue;
        }
        if quiet(command, &["mcp", "get", "tomacco"]) {
            println!("  ok       {name} is connected");
            continue;
        }
        if !answers.ask(&format!("Connect {name} to {}?", product::PRODUCT_NAME)) {
            println!("  skipped  {name}");
            continue;
        }
        let mut args: Vec<&str> = add.to_vec();
        args.extend([program.as_str(), "mcp"]);
        if quiet(command, &args) {
            println!("  ok       {name} is connected");
        } else {
            println!(
                "  failed   {name}: run `{command} {}` yourself",
                args.join(" ")
            );
        }
    }
    Ok(())
}

/// The latest command-line tools for Linux from Google's repository index:
/// file name and SHA-1, the only digest the index publishes.
fn newest_command_line_tools(index: &str) -> Option<(String, String)> {
    let document = roxmltree::Document::parse(index).ok()?;
    let package = document
        .descendants()
        .find(|node| node.attribute("path") == Some("cmdline-tools;latest"))?;
    package
        .descendants()
        .filter(|node| node.has_tag_name("archive"))
        .find_map(|archive| {
            let host = archive
                .descendants()
                .find(|node| node.has_tag_name("host-os"))?
                .text()?;
            if host != "linux" {
                return None;
            }
            let complete = archive
                .descendants()
                .find(|node| node.has_tag_name("complete"))?;
            let text = |name: &str| {
                complete
                    .descendants()
                    .find(|node| node.has_tag_name(name))?
                    .text()
                    .map(str::to_string)
            };
            Some((text("url")?, text("checksum")?))
        })
}

fn run_visible(program: &str, args: &[&str]) -> Result<()> {
    let status = Command::new(program)
        .args(args)
        .stdin(Stdio::null())
        .status()?;
    if status.success() {
        Ok(())
    } else {
        Err(Error::new(
            "setup_failed",
            format!("{program} failed: {status}"),
        ))
    }
}

fn install_command_line_tools(sdk: &Path) -> Result<()> {
    let temporary = tempfile::tempdir()?;
    let index = temporary.path().join("index.xml");
    let index_text = index.to_string_lossy().into_owned();
    run_visible(
        "curl",
        &["-fsSL", "--retry", "3", "-o", &index_text, REPOSITORY_INDEX],
    )?;
    let text = std::fs::read_to_string(&index)?;
    let (file, sha1) = newest_command_line_tools(&text).ok_or_else(|| {
        Error::new(
            "setup_failed",
            "Google's repository index lists no Linux command-line tools",
        )
    })?;
    identifier(&file, "archive name", Some(r"[A-Za-z0-9_.-]+"))?;
    identifier(&sha1, "checksum", Some(r"[0-9a-f]{40}"))?;
    // An interrupted download continues on the next run; the archive is
    // used only once it matches Google's checksum.
    let archive = crate::download::directory()?.join(&file);
    let archive_text = archive.to_string_lossy().into_owned();
    let url = format!("{REPOSITORY}{file}");
    crate::download::fetch(&crate::download::Request {
        url: &url,
        destination: &archive,
        checksum: crate::download::Checksum::Sha1(sha1),
        max_bytes: 2 * 1024 * 1024 * 1024,
        progress: true,
        allow_http: false,
        curl: "curl",
    })?;
    let unpacked = temporary.path().join("unpacked");
    let unpacked_text = unpacked.to_string_lossy().into_owned();
    run_visible("unzip", &["-q", &archive_text, "-d", &unpacked_text])?;
    let target = sdk.join("cmdline-tools/latest");
    std::fs::create_dir_all(sdk.join("cmdline-tools"))?;
    if target.exists() {
        std::fs::remove_dir_all(&target)?;
    }
    std::fs::rename(unpacked.join("cmdline-tools"), &target)?;
    let _ = std::fs::remove_file(&archive);
    Ok(())
}

/// Waits for a job the operations started, however long it takes.
fn wait(reply: &Value) -> Result<()> {
    let id = reply["id"].as_str().unwrap_or_default().to_string();
    if id.is_empty() {
        return Ok(());
    }
    loop {
        let state = api::dispatch("job.wait", &json!({"job_id":id,"seconds":60}))?;
        match state["state"].as_str().unwrap_or_default() {
            "succeeded" => return Ok(()),
            "queued" | "running" => {}
            other => {
                return Err(Error::new(
                    "setup_failed",
                    format!(
                        "{} {other}. See `tomaccoctl call job.logs` for job {id}",
                        state["label"]
                    ),
                ));
            }
        }
    }
}

fn default_sdk() -> Result<PathBuf> {
    Ok(PathBuf::from(
        std::env::var("HOME").map_err(|_| Error::new("setup_failed", "HOME is not set"))?,
    )
    .join("Android/Sdk"))
}

fn set_up_android(doctor: &Value, answers: &Answers) -> Result<()> {
    if !cfg!(target_arch = "x86_64") {
        println!(
            "  Google ships no Android emulator for this computer. Connect an Android phone instead."
        );
        return Ok(());
    }
    if !has(doctor, "java") {
        println!("  Android needs a JDK first. On Arch: sudo pacman -S jdk-openjdk gradle");
        return Ok(());
    }
    let ready = has(doctor, "sdkmanager") && has(doctor, "adb") && has(doctor, "emulator");
    let avds = api::dispatch("android.avds", &json!({})).unwrap_or_default();
    let has_emulator = avds["avds"].as_array().is_some_and(|avds| !avds.is_empty());
    if ready && has_emulator {
        println!("  ok       Android emulator");
        return Ok(());
    }
    println!(
        "\nAndroid: this downloads the SDK from Google, about 5 GB, and accepts Google's Android SDK\nlicense (https://developer.android.com/studio/terms) on your behalf."
    );
    if !answers.ask("Install the Android SDK and create an emulator?") {
        println!("  skipped  Android");
        return Ok(());
    }
    if !has(doctor, "sdkmanager") {
        let sdk = doctor["android_sdk"]
            .as_str()
            .filter(|path| !path.is_empty())
            .map(PathBuf::from)
            .map_or_else(default_sdk, Ok)?;
        install_command_line_tools(&sdk)?;
        api::dispatch("settings.configure", &json!({"android_sdk":sdk}))?;
    }
    println!("Installing the SDK packages. This takes a while.");
    wait(&api::dispatch(
        "android.sdk_install",
        &json!({"packages":SDK_PACKAGES,"accept_licenses":true}),
    )?)?;
    if !has_emulator {
        wait(&api::dispatch(
            "android.avd_create",
            &json!({"name":EMULATOR_NAME,"system_image":SYSTEM_IMAGE,"profile_id":EMULATOR_PROFILE}),
        )?)?;
    }
    println!("  ok       Android emulator `{EMULATOR_NAME}`");
    Ok(())
}

pub fn run(arguments: &[String]) -> Result<i32> {
    let mut yes = false;
    for argument in arguments {
        match argument.as_str() {
            "--yes" | "-y" => yes = true,
            other => {
                println!("Unknown option {other}. Usage: tomaccoctl setup [--yes]");
                return Ok(2);
            }
        }
    }
    let answers = Answers {
        yes,
        interactive: std::io::stdin().is_terminal(),
    };
    let doctor = api::dispatch("doctor", &json!({}))?;
    let virtual_device = api::dispatch("ios.virtual.status", &json!({}))
        .is_ok_and(|status| status["configured"] == true);
    println!(
        "{} {}\n",
        product::PRODUCT_NAME,
        crate::update::current_version()
    );
    print_report(&report(&doctor));
    if virtual_device {
        println!("  ok       Virtual iOS device");
    } else {
        println!(
            "  optional Virtual iOS device, a research feature: see docs/virtual-ios/setup.md"
        );
    }
    println!();
    connect_agents(&answers)?;
    set_up_android(&doctor, &answers)?;
    println!("\nStart the app with `tomacco`. docs/getting-started.md has your first project.");
    Ok(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_missing_tool_names_its_remedy_and_a_present_one_only_its_name() {
        let doctor = json!({"tools":[{"name":"java","available":true},{"name":"gradle","available":false}],"kvm":{"accessible":true}});
        let lines = report(&doctor);
        assert!(lines[0].0);
        assert!(!lines[1].0);
        assert!(lines[1].1.contains("install gradle"));
        assert!(
            lines
                .iter()
                .find(|line| line.1.starts_with("KVM"))
                .unwrap()
                .0
        );
    }

    #[test]
    fn only_a_clear_yes_or_enter_counts() {
        for answer in ["", "\n", "y\n", "Yes\n", "j\n", "JA\n"] {
            assert!(accepted(answer), "{answer}");
        }
        for answer in ["n\n", "no\n", "nein\n", "maybe\n"] {
            assert!(!accepted(answer), "{answer}");
        }
    }

    #[test]
    fn next_steps_name_what_is_left_and_nothing_when_all_is_ready() {
        let tools: Vec<Value> = [
            "java",
            "gradle",
            "sdkmanager",
            "adb",
            "emulator",
            "swift",
            "xtool",
        ]
        .iter()
        .map(|name| json!({"name":name,"available":true}))
        .collect();
        let ready = json!({"configured":true,"phase":"stopped","start_disk":{"enough":true}});
        assert!(next_steps(&json!({"tools":tools}), true, &ready).is_empty());
        let unconfigured = json!({"configured":false,"chain":virtual_ios_chain(None)});
        let steps = next_steps(&json!({"tools":[]}), false, &unconfigured);
        let goals: Vec<_> = steps
            .iter()
            .map(|step| step["goal"].as_str().unwrap())
            .collect();
        assert_eq!(goals, ["android", "android", "ios", "virtual_ios"]);
        assert!(steps.iter().all(|step| step["ask_first"] == true));
        assert!(steps[3]["how"].as_str().unwrap().contains("IM4M"));
    }

    #[test]
    fn the_virtual_ios_step_names_the_next_missing_stage_of_the_chain() {
        let root = tempfile::tempdir().unwrap();
        let tooling = root.path();
        let stages = |chain: &[Value]| -> Vec<Value> {
            chain.iter().map(|stage| stage["stage"].clone()).collect()
        };
        let chain = virtual_ios_chain(Some(tooling));
        assert_eq!(
            stages(&chain),
            [
                "engine",
                "helper",
                "images",
                "chain",
                "profile",
                "configure"
            ]
        );
        assert_eq!(chain[0]["done"], false);
        let step = &virtual_ios_steps(&json!({"configured":false,"chain":chain}))[0];
        assert_eq!(step["next_stage"], "engine");
        assert!(step["what"].as_str().unwrap().contains("Build the engine"));
        assert!(
            step["how"]
                .as_str()
                .unwrap()
                .contains("build_ios_paravirt.py")
        );
        // With the engine and helper built, the person's images come next.
        for file in [
            "ios-paravirt/sptm-build/qemu-system-aarch64",
            "ios-guest-control/vhost-device-vsock",
            "ios-sptm/build/qemu-system-x86_64",
            "ios-sptm/support/busybox/busybox",
            "ios-sptm/support/apfs/apfs.ko",
            "ios-linker/prefix/bin/aarch64-apple-darwin-ld",
        ] {
            std::fs::create_dir_all(tooling.join(file).parent().unwrap()).unwrap();
            std::fs::write(tooling.join(file), b"").unwrap();
        }
        let chain = virtual_ios_chain(Some(tooling));
        assert_eq!(
            (&chain[0]["done"], &chain[1]["done"]),
            (&json!(true), &json!(true))
        );
        let step = &virtual_ios_steps(&json!({"configured":false,"chain":chain}))[0];
        assert_eq!(step["next_stage"], "images");
        assert!(step["how"].as_str().unwrap().contains("IM4M"));
        // Without a checkout the build stages are unknown, and still named first.
        let chain = virtual_ios_chain(None);
        assert_eq!(chain[0]["done"], Value::Null);
        let step = &virtual_ios_steps(&json!({"configured":false,"chain":chain}))[0];
        assert_eq!(step["next_stage"], "engine");
        // A host that cannot run it gets no step at all.
        assert!(virtual_ios_steps(&json!({"host_supported":false})).is_empty());
    }

    #[test]
    fn a_configured_phone_gets_steps_only_for_what_stops_the_next_start() {
        assert!(
            virtual_ios_steps(&json!({"configured":true,"phase":"stopped",
            "start_disk":{"enough":null},"applications_error":null}))
            .is_empty()
        );
        let steps = virtual_ios_steps(&json!({"configured":true,"phase":"failed",
            "error":"The boot did not reach the Home screen",
            "start_disk":{"enough":false,"needed_bytes":36u64 << 30,"free_bytes":20u64 << 30,
                "reclaimable_bytes":6u64 << 30,"installs":"/phone/installs"},
            "applications_error":"installd did not install dev.example.App"}));
        let what: Vec<_> = steps
            .iter()
            .map(|step| step["what"].as_str().unwrap())
            .collect();
        assert_eq!(
            what,
            [
                "Free disk space for the next virtual iPhone start",
                "Read why the last virtual iPhone start failed",
                "The last virtual iPhone start installed no apps"
            ]
        );
        assert!(steps[0]["how"].as_str().unwrap().contains("10.0 GiB more"));
        assert!(
            steps[0]["how"]
                .as_str()
                .unwrap()
                .contains("ios.virtual.cleanup")
        );
        assert_eq!(steps[0]["ask_first"], true);
        assert!(steps[1]["how"].as_str().unwrap().contains("Home screen"));
        let broken = virtual_ios_steps(&json!({"configured":true,"status_error":{
            "code":"ios_virtual_profile_invalid","message":"profile.json is not a format 1 virtual iOS device profile"}}));
        assert_eq!(broken.len(), 1);
        assert!(broken[0]["how"].as_str().unwrap().contains("format 1"));
    }

    #[test]
    fn a_desktop_that_cannot_load_comes_first_with_what_it_lacks() {
        let tools: Vec<Value> = [
            "java",
            "gradle",
            "sdkmanager",
            "adb",
            "emulator",
            "swift",
            "xtool",
        ]
        .iter()
        .map(|name| json!({"name":name,"available":true}))
        .collect();
        let broken = json!({"tools":tools,"desktop":{"installed":true,"path":"/home/me/.local/lib/tomacco/tomacco",
            "loadable":false,"missing_libraries":["libavcodec.so.63"],"missing_versions":["Qt_6.12 in libQt6Core.so.6"]}});
        let steps = next_steps(
            &broken,
            true,
            &json!({"configured":true,"phase":"stopped","start_disk":{"enough":true}}),
        );
        assert_eq!(steps.len(), 1);
        assert_eq!(steps[0]["goal"], "desktop");
        let what = steps[0]["what"].as_str().unwrap();
        assert!(
            what.contains("libavcodec.so.63") && what.contains("Qt_6.12"),
            "{what}"
        );
        let how = steps[0]["how"].as_str().unwrap();
        assert!(
            how.contains("Qt 6.12") && how.contains("FFmpeg 9") && how.contains("glibc 2.39"),
            "{how}"
        );
        // Loadable, not installed, or not checked: nothing to do.
        for desktop in [
            json!({"loadable":true}),
            json!({"installed":false,"loadable":null}),
            json!({"loadable":null}),
        ] {
            let doctor = json!({"tools":broken["tools"],"desktop":desktop});
            assert!(
                next_steps(
                    &doctor,
                    true,
                    &json!({"configured":true,"phase":"stopped","start_disk":{"enough":true}})
                )
                .is_empty()
            );
        }
    }

    #[test]
    fn nothing_changes_without_a_person_or_yes() {
        let answers = Answers {
            yes: false,
            interactive: false,
        };
        assert!(!answers.ask("Install?"));
    }

    #[test]
    fn the_index_gives_the_linux_tools_and_their_checksum() {
        let index = r#"<sdk:sdk-repository xmlns:sdk="http://schemas.android.com/sdk/android/repo/repository2/03">
          <remotePackage path="platform-tools"><archives><archive><complete><url>wrong.zip</url><checksum>0</checksum></complete><host-os>linux</host-os></archive></archives></remotePackage>
          <remotePackage path="cmdline-tools;latest"><archives>
            <archive><complete><url>mac.zip</url><checksum>1</checksum></complete><host-os>macosx</host-os></archive>
            <archive><complete><checksum>abc</checksum><url>commandlinetools-linux-1_latest.zip</url></complete><host-os>linux</host-os></archive>
          </archives></remotePackage></sdk:sdk-repository>"#;
        assert_eq!(
            newest_command_line_tools(index),
            Some((
                "commandlinetools-linux-1_latest.zip".to_string(),
                "abc".to_string()
            ))
        );
        assert_eq!(newest_command_line_tools("<a/>"), None);
    }
}
