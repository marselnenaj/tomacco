//! `tomaccoctl setup`: a readable check of this machine that offers to fix what
//! it can. It changes nothing without a yes, from the person or from `--yes`.
use crate::agent_clients as clients;
use crate::api;
use crate::core::*;
use crate::mcp_tools::Mode;
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
        &format!("Java, for Android builds: {}", jdk_advice(doctor)),
    );
    line(
        has(doctor, "gradle"),
        &format!(
            "Gradle, for new Android projects: {}",
            gradle_advice(doctor)
        ),
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
        &format!("KVM, for a fast Android emulator: {}", kvm_advice(doctor)),
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
            desktop_remedy(desktop),
            true,
        );
    }
    if !has(doctor, "java") || !has(doctor, "gradle") {
        let mut how = Vec::new();
        if !has(doctor, "java") {
            how.push(format!("JDK: {}.", jdk_advice(doctor)));
        }
        if !has(doctor, "gradle") {
            how.push(format!("Gradle: {}.", gradle_advice(doctor)));
        }
        step(
            "android",
            "Install a JDK 17 or newer and Gradle",
            how.join(" "),
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
    if doctor["kvm"]["accessible"] == false && doctor["kvm"]["status"].is_string() {
        step(
            "android",
            &format!(
                "Make KVM usable for a fast Android emulator: {}",
                s(&doctor["kvm"], "message")
            ),
            kvm_advice(doctor),
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

/// How to install a JDK here, from doctor's `host`, with the Arch command
/// when doctor did not say.
fn jdk_advice(doctor: &Value) -> String {
    doctor["host"]["install"]["jdk"]
        .as_str()
        .unwrap_or("On Arch: sudo pacman -S --needed jdk-openjdk")
        .to_string()
}

fn gradle_advice(doctor: &Value) -> String {
    doctor["host"]["install"]["gradle"]
        .as_str()
        .unwrap_or("On Arch: sudo pacman -S --needed gradle")
        .to_string()
}

/// What makes /dev/kvm usable here: the commands doctor found for this
/// system, or the general advice.
fn kvm_advice(doctor: &Value) -> String {
    let commands = strings(&doctor["kvm"]["commands"]);
    if commands.is_empty() {
        "enable virtualization and join the kvm group".to_string()
    } else {
        commands.join("; ")
    }
}

/// How to make the desktop start: the command that installs the missing
/// system libraries where doctor knows their packages, else what it needs.
fn desktop_remedy(desktop: &Value) -> String {
    let install = &desktop["install"];
    let command = s(install, "command");
    let unknown = strings(&install["unknown"]);
    let versions = strings(&desktop["missing_versions"]);
    if !command.is_empty() && unknown.is_empty() && versions.is_empty() {
        return format!(
            "Install the missing system libraries: {command}. tomaccoctl itself works without them."
        );
    }
    let requires = desktop["release_requires"]
        .as_str()
        .unwrap_or(crate::desktop::RELEASE_REQUIREMENTS);
    let mut how = String::new();
    if !command.is_empty() {
        how.push_str(&format!(
            "Install the system libraries it can name: {command}. "
        ));
    }
    if desktop["portable"] == true {
        how.push_str(&format!(
            "The portable desktop needs {requires}. tomaccoctl itself works without it."
        ));
    } else {
        how.push_str(&format!(
            "The release desktop needs {requires}. Update the system (on Arch: sudo pacman -Syu), install the portable package, or build the desktop from a checkout on this system: {DOCS}/getting-started.md#debian-ubuntu-and-fedora. tomaccoctl itself works without it."
        ));
    }
    how
}

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
            "how":format!("{} The whole chain is in chain, in order; {DOCS}/virtual-ios/setup.md explains it. ios.virtual.host_check says whether this machine can run it, and ios.virtual.setup with a work directory plans the chain from what is already done and runs it stage by stage. Read its IM4M section before anything is built or downloaded: it says what is required and whether setup can finish. It needs about 100 GiB and a 16 GiB graphics card.",
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
    /// Questions and their yes go to standard error: standard output carries JSON.
    aside: bool,
}
impl Answers {
    fn ask(&self, question: &str) -> bool {
        if self.yes {
            if self.aside {
                eprintln!("{question} yes");
            } else {
                println!("{question} yes");
            }
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

/// What `setup` and `setup agents` take from the command line.
#[derive(Debug, PartialEq)]
struct Arguments {
    action: Action,
    yes: bool,
    json: bool,
    tools: Option<Mode>,
    opencode: Option<clients::Layout>,
    /// Only these clients, by id; empty for all.
    only: Vec<clients::ClientId>,
}
#[derive(Debug, Clone, Copy, PartialEq)]
enum Action {
    /// The whole check: agents, then Android.
    Setup,
    /// The agents only.
    Agents,
    /// The agents' state, changing nothing.
    Status,
    /// Tomacco's own entries and skill links taken out again.
    Remove,
}
impl Action {
    fn name(self) -> &'static str {
        match self {
            Self::Setup => "setup",
            Self::Agents => "connect",
            Self::Status => "status",
            Self::Remove => "remove",
        }
    }
}

const USAGE: &str = "Usage: tomaccoctl setup [--yes] [--tools full|compact|grouped] [--opencode-config v1|v2]\n       tomaccoctl setup agents [status|remove] [--yes] [--json] [--client ID] [--tools MODE] [--opencode-config v1|v2]";

fn parse(arguments: &[String]) -> std::result::Result<Arguments, String> {
    let mut parsed = Arguments {
        action: Action::Setup,
        yes: false,
        json: false,
        tools: None,
        opencode: None,
        only: Vec::new(),
    };
    let mut words = arguments.iter().map(String::as_str);
    while let Some(word) = words.next() {
        match word {
            "agents" if parsed.action == Action::Setup => parsed.action = Action::Agents,
            "status" if parsed.action == Action::Agents => parsed.action = Action::Status,
            "remove" if parsed.action == Action::Agents => parsed.action = Action::Remove,
            "--yes" | "-y" => parsed.yes = true,
            "--json" if parsed.action != Action::Setup => parsed.json = true,
            "--tools" => {
                let value = words.next().unwrap_or("");
                parsed.tools = Some(
                    Mode::parse(value)
                        .filter(|_| !value.is_empty())
                        .ok_or_else(|| {
                            format!(
                                "--tools takes full, compact or grouped, not {value:?}. {USAGE}"
                            )
                        })?,
                );
            }
            "--client" if parsed.action != Action::Setup => {
                let value = words.next().unwrap_or("");
                parsed
                    .only
                    .push(clients::ClientId::parse(value).ok_or_else(|| {
                        format!(
                            "--client takes one of {}, not {value:?}. {USAGE}",
                            clients::CLIENTS
                                .iter()
                                .map(|c| c.id())
                                .collect::<Vec<_>>()
                                .join(", ")
                        )
                    })?);
            }
            "--opencode-config" => {
                let value = words.next().unwrap_or("");
                parsed.opencode = Some(clients::Layout::parse(value).ok_or_else(|| {
                    format!("--opencode-config takes v1 or v2, not {value:?}. {USAGE}")
                })?);
            }
            other => return Err(format!("Unknown option {other}. {USAGE}")),
        }
    }
    Ok(parsed)
}

/// The printed line for one client's report, in the style of the rest of
/// setup: a verdict, the client, and what to do when something is left.
fn client_line(report: &clients::Report, action: Action) -> Option<String> {
    use clients::State;
    let name = report.id.name();
    let detail = report.state.detail().unwrap_or("");
    let file = report
        .file
        .as_ref()
        .map(|path| path.display().to_string())
        .unwrap_or_default();
    let with_snippet = |head: String| {
        let mut text = head;
        if let Some(command) = &report.command {
            text.push_str(&format!("\n             {command}"));
        }
        if !report.snippet.is_empty()
            && !matches!(
                report.id,
                clients::ClientId::ClaudeCode | clients::ClientId::Codex
            )
        {
            text.push_str(&format!("\n             In {file}:"));
            for line in report.snippet.lines() {
                text.push_str(&format!("\n               {line}"));
            }
        }
        text
    };
    Some(match &report.state {
        State::Absent if action == Action::Status => {
            format!("  -        {name} is not on this machine")
        }
        State::Absent => return None,
        State::Connected | State::Added => format!("  ok       {name} is connected"),
        State::Updated => format!("  ok       {name} is connected (entry updated)"),
        State::Removed => format!("  ok       {name} is disconnected"),
        State::Declined => format!("  skipped  {name}"),
        State::NotConnected if action == Action::Remove => return None,
        State::NotConnected => {
            format!("  missing  {name} is not connected: tomaccoctl setup agents")
        }
        State::Disabled => format!(
            "  off      {name} has {} switched off in {file}; left as it is",
            product::PRODUCT_NAME
        ),
        State::Outdated(_) => format!(
            "  outdated {name}: its {} entry {detail}; tomaccoctl setup agents updates it",
            clients::SERVER
        ),
        State::Conflict(_) => with_snippet(format!("  conflict {name}: {detail}; left as it is")),
        State::Manual(_) => with_snippet(format!("  manual   {name}: {detail}. Add it yourself:")),
        State::Unknown(_) => with_snippet(format!("  unknown  {name}: {detail}")),
        State::Failed(_) => format!("  failed   {name}: {detail}"),
    })
}

fn skill_line(
    target: &clients::SkillTarget,
    state: &clients::SkillState,
    action: Action,
) -> Option<String> {
    use clients::SkillState;
    let label = &target.label;
    Some(match state {
        SkillState::Absent => return None,
        SkillState::Linked | SkillState::Added => format!("  ok       {label} has the agent skill"),
        SkillState::Removed => format!("  ok       Agent skill removed from {label}"),
        SkillState::NotLinked if action == Action::Remove => return None,
        SkillState::NotLinked => {
            format!("  missing  Agent skill for {label}: tomaccoctl setup agents links it")
        }
        SkillState::Declined => format!("  skipped  Agent skill for {label}"),
        SkillState::Taken(link) => format!(
            "  manual   Agent skill for {label}: {} is not {}'s; left as it is",
            link.display(),
            product::PRODUCT_NAME
        ),
        SkillState::Failed(error) => format!("  failed   Agent skill for {label}: {error}"),
    })
}

/// Connects the coding agents on this machine to Tomacco, reports them or
/// takes Tomacco out of them again, each after the person's yes, and links
/// the agent skill where each one reads it. The result is the report that
/// `--json` prints; the lines are printed as it goes unless `json` is set.
fn agents(arguments: &Arguments, answers: &Answers) -> Result<Value> {
    let action = arguments.action;
    let env = clients::Environment::current()?;
    let options = clients::Options {
        program: std::env::current_exe()?,
        tools: arguments.tools,
        opencode: arguments.opencode,
    };
    let mut ask = |question: &str| answers.ask(question);
    let mut reports = Vec::new();
    for id in clients::CLIENTS {
        if !arguments.only.is_empty() && !arguments.only.contains(&id) {
            continue;
        }
        let resolved = clients::resolve(id, &env, &options);
        let report = match action {
            Action::Status => clients::inspect(&resolved, &options),
            Action::Remove => clients::remove(&env, &resolved, &options, &mut ask),
            Action::Setup | Action::Agents => clients::connect(&env, &resolved, &options, &mut ask),
        };
        if !arguments.json {
            if let Some(line) = client_line(&report, action) {
                println!("{line}");
            }
            if action == Action::Status {
                for note in &report.notes {
                    println!("           {note}");
                }
            }
        }
        reports.push(report);
    }
    let found =
        |id: clients::ClientId| reports.iter().any(|report| report.id == id && report.found);
    let targets = clients::skill_targets(&env, &found);
    let mut skills = Vec::new();
    for target in &targets {
        let state = match action {
            Action::Status => Ok(clients::skill_state(&env.home, target)),
            Action::Remove => clients::unlink_skill(&env.home, target),
            Action::Setup | Action::Agents => clients::link_skill(&env.home, target, &mut ask),
        }
        .unwrap_or_else(|error| clients::SkillState::Failed(error.message));
        if !arguments.json {
            if let Some(line) = skill_line(target, &state, action) {
                println!("{line}");
            }
        }
        let detail = match &state {
            clients::SkillState::Taken(link) => Some(link.display().to_string()),
            clients::SkillState::Failed(error) => Some(error.clone()),
            _ => None,
        };
        skills.push(json!({
            "label": target.label,
            "link": target.directory.join("skills").join(product::REPO_NAME),
            "state": state.kind(),
            "detail": detail,
        }));
    }
    let attention = reports
        .iter()
        .filter(|report| report.found)
        .filter(|report| {
            use clients::State;
            matches!(
                report.state,
                State::NotConnected
                    | State::Outdated(_)
                    | State::Conflict(_)
                    | State::Manual(_)
                    | State::Unknown(_)
                    | State::Failed(_)
            )
        })
        .count();
    Ok(json!({
        "action": action.name(),
        "program": options.program,
        "tools": options.tools.map(Mode::name),
        "clients": reports.iter().map(clients::Report::value).collect::<Vec<_>>(),
        "skills": skills,
        "attention": attention,
    }))
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
        println!("  Android needs a JDK first. {}", jdk_advice(doctor));
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
    let parsed = match parse(arguments) {
        Ok(parsed) => parsed,
        Err(message) => {
            println!("{message}");
            return Ok(2);
        }
    };
    let answers = Answers {
        yes: parsed.yes,
        interactive: std::io::stdin().is_terminal() && !parsed.json,
        aside: parsed.json,
    };
    if parsed.action != Action::Setup {
        let report = agents(&parsed, &answers)?;
        if parsed.json {
            println!("{}", serde_json::to_string_pretty(&report)?);
        }
        // A status that needs a hand is exit 1, as `tomaccoctl omarchy status` is.
        let attention = report["attention"].as_u64().unwrap_or(0);
        return Ok(if parsed.action == Action::Status && attention > 0 {
            1
        } else {
            0
        });
    }
    let doctor = api::dispatch("doctor", &json!({}))?;
    let virtual_device = api::dispatch("ios.virtual.status", &json!({}))
        .is_ok_and(|status| status["configured"] == true);
    println!(
        "{} {}\n",
        product::PRODUCT_NAME,
        crate::update::current_version()
    );
    print_report(&report(&doctor));
    if doctor["desktop"]["loadable"] == false {
        println!(
            "  missing  Desktop libraries: {}",
            desktop_remedy(&doctor["desktop"])
        );
    }
    if virtual_device {
        println!("  ok       Virtual iOS device");
    } else {
        println!(
            "  optional Virtual iOS device, a research feature: see docs/virtual-ios/setup.md"
        );
    }
    println!();
    agents(&parsed, &answers)?;
    set_up_android(&doctor, &answers)?;
    println!("\nStart the app with `tomacco`. docs/getting-started.md has your first project.");
    Ok(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_steps_name_this_hosts_jdk_and_kvm_commands() {
        let doctor = json!({"tools":[{"name":"sdkmanager","available":true},{"name":"adb","available":true},
                {"name":"emulator","available":true}],
            "kvm":{"accessible":false,"status":"not_in_group","message":"/dev/kvm belongs to the kvm group, and you are not in it.",
                "commands":["sudo usermod -aG kvm ada","Log out and back in"]},
            "host":{"install":{"jdk":"sudo apt-get install openjdk-17-jdk-headless","gradle":"Unpack Gradle 9 from https://gradle.org/releases"}}});
        let steps = next_steps(&doctor, true, &json!({"configured":true}));
        let jdk = steps
            .iter()
            .find(|step| step["what"] == "Install a JDK 17 or newer and Gradle")
            .unwrap();
        assert_eq!(
            jdk["how"],
            "JDK: sudo apt-get install openjdk-17-jdk-headless. Gradle: Unpack Gradle 9 from https://gradle.org/releases."
        );
        let kvm = steps
            .iter()
            .find(|step| {
                step["what"]
                    .as_str()
                    .unwrap()
                    .starts_with("Make KVM usable")
            })
            .unwrap();
        assert_eq!(kvm["how"], "sudo usermod -aG kvm ada; Log out and back in");
        assert_eq!(kvm["ask_first"], true);
        let lines = report(&doctor);
        assert!(
            lines[0]
                .1
                .ends_with("sudo apt-get install openjdk-17-jdk-headless")
        );
        assert!(
            lines[4]
                .1
                .ends_with("sudo usermod -aG kvm ada; Log out and back in")
        );
        // Without doctor's host facts the advice is the one for Arch, and a
        // usable KVM adds no step.
        let mut plain = doctor.clone();
        plain["host"] = Value::Null;
        plain["kvm"] = json!({"accessible":true});
        let steps = next_steps(&plain, true, &json!({"configured":true}));
        assert!(steps[0]["how"].as_str().unwrap().contains("pacman"));
        assert!(
            !steps
                .iter()
                .any(|step| step["what"].as_str().unwrap().starts_with("Make KVM"))
        );
    }

    #[test]
    fn a_missing_tool_names_its_remedy_and_a_present_one_only_its_name() {
        let doctor = json!({"tools":[{"name":"java","available":true},{"name":"gradle","available":false}],"kvm":{"accessible":true}});
        let lines = report(&doctor);
        assert!(lines[0].0);
        assert!(!lines[1].0);
        assert!(lines[1].1.contains("pacman -S --needed gradle"));
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
        // A portable desktop on Debian that lacks only libraries doctor knows: the command alone.
        let mut portable = broken.clone();
        portable["desktop"] = json!({"installed":true,"path":"/home/me/.local/lib/tomacco/tomacco","loadable":false,
            "portable":true,"release_requires":crate::desktop::PORTABLE_REQUIREMENTS,
            "missing_libraries":["libnss3.so","libxkbfile.so.1"],"missing_versions":[],
            "install":{"manager":"apt","packages":["libnss3","libxkbfile1"],
                "command":"sudo apt-get install libnss3 libxkbfile1","unknown":[]}});
        let steps = next_steps(
            &portable,
            true,
            &json!({"configured":true,"phase":"stopped","start_disk":{"enough":true}}),
        );
        assert_eq!(
            steps[0]["how"],
            "Install the missing system libraries: sudo apt-get install libnss3 libxkbfile1. tomaccoctl itself works without them."
        );
        // With a library no package is known for, the command and what the desktop needs.
        portable["desktop"]["install"]["unknown"] = json!(["libmystery.so.7"]);
        let how = desktop_remedy(&portable["desktop"]);
        assert!(
            how.starts_with("Install the system libraries it can name: sudo apt-get install libnss3 libxkbfile1. ")
                && how.contains("glibc 2.35"),
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
    fn setup_and_setup_agents_take_their_own_options() {
        let words =
            |text: &str| -> Vec<String> { text.split_whitespace().map(String::from).collect() };
        let plain = parse(&[]).unwrap();
        assert_eq!(plain.action, Action::Setup);
        assert!(!plain.yes && !plain.json && plain.tools.is_none() && plain.opencode.is_none());
        let full = parse(&words("--yes --tools grouped --opencode-config v1")).unwrap();
        assert_eq!(
            (full.yes, full.tools, full.opencode),
            (true, Some(Mode::Grouped), Some(clients::Layout::OpenCodeV1))
        );
        let status = parse(&words("agents status --json")).unwrap();
        assert_eq!((status.action, status.json), (Action::Status, true));
        let remove = parse(&words("agents remove -y --client hermes --client omp")).unwrap();
        assert_eq!((remove.action, remove.yes), (Action::Remove, true));
        assert_eq!(
            remove.only,
            [clients::ClientId::Hermes, clients::ClientId::Omp]
        );
        assert_eq!(parse(&words("agents")).unwrap().action, Action::Agents);
        for wrong in [
            "--json",
            "status",
            "agents agents",
            "--tools tiny",
            "--tools",
            "--opencode-config 3",
            "--what",
            "--client hermes",
            "agents --client grok",
        ] {
            let error = parse(&words(wrong)).unwrap_err();
            assert!(error.contains("Usage"), "{wrong}: {error}");
        }
    }

    #[test]
    fn nothing_changes_without_a_person_or_yes() {
        let answers = Answers {
            yes: false,
            interactive: false,
            aside: false,
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
