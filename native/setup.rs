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
fn next_steps(doctor: &Value, emulator: bool, virtual_device: bool) -> Vec<Value> {
    const DOCS: &str = "https://github.com/marselnenaj/tomacco/blob/main/docs";
    let mut steps = Vec::new();
    let mut step = |goal: &str, what: &str, how: String, ask_first: bool| {
        steps.push(json!({"goal":goal,"what":what,"how":how,"ask_first":ask_first}))
    };
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
    if !virtual_device {
        step(
            "virtual_ios",
            "Set up the virtual iOS device, only if the person wants it",
            format!(
                "Follow {DOCS}/virtual-ios/setup.md, and read its IM4M section before any download: it says what is required and whether setup can finish. It needs about 100 GiB and a 16 GiB graphics card. `python3 tools/ios_virtual_profile.py --check` lists what is still missing."
            ),
            true,
        );
    }
    steps
}

/// The plan an agent reads from the `doctor` operation.
pub fn with_next_steps(mut doctor: Value) -> Value {
    let emulator = api::dispatch("android.avds", &json!({}))
        .is_ok_and(|avds| avds["avds"].as_array().is_some_and(|avds| !avds.is_empty()));
    let virtual_device = api::dispatch("ios.virtual.status", &json!({}))
        .is_ok_and(|status| status["configured"] == true);
    doctor["next_steps"] = json!(next_steps(&doctor, emulator, virtual_device));
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
    let archive = temporary.path().join(&file);
    let archive_text = archive.to_string_lossy().into_owned();
    let url = format!("{REPOSITORY}{file}");
    run_visible(
        "curl",
        &[
            "-fL",
            "--retry",
            "3",
            "--progress-bar",
            "-o",
            &archive_text,
            &url,
        ],
    )?;
    let output = Command::new("sha1sum").arg(&archive).output()?;
    let digest = String::from_utf8_lossy(&output.stdout);
    if digest.split_whitespace().next() != Some(sha1.as_str()) {
        return Err(Error::new(
            "setup_failed",
            "The download does not match Google's checksum",
        ));
    }
    let unpacked = temporary.path().join("unpacked");
    let unpacked_text = unpacked.to_string_lossy().into_owned();
    run_visible("unzip", &["-q", &archive_text, "-d", &unpacked_text])?;
    let target = sdk.join("cmdline-tools/latest");
    std::fs::create_dir_all(sdk.join("cmdline-tools"))?;
    if target.exists() {
        std::fs::remove_dir_all(&target)?;
    }
    std::fs::rename(unpacked.join("cmdline-tools"), &target)?;
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
        assert!(next_steps(&json!({"tools":tools}), true, true).is_empty());
        let steps = next_steps(&json!({"tools":[]}), false, false);
        let goals: Vec<_> = steps
            .iter()
            .map(|step| step["goal"].as_str().unwrap())
            .collect();
        assert_eq!(goals, ["android", "android", "ios", "virtual_ios"]);
        assert!(steps.iter().all(|step| step["ask_first"] == true));
        assert!(steps[3]["how"].as_str().unwrap().contains("IM4M"));
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
