//! `ios.virtual.setup`: the virtual iOS device's setup chain
//! (docs/virtual-ios/setup.md) as one guided plan. `plan` reads which stages
//! are done from their outputs under `work` and the checkout's `.tooling`;
//! `run` starts the next stage, or a named one, as a durable job. A stage is
//! done when its output and receipt are complete, so a stage that finished is
//! skipped, and one that stopped halfway is reported for the person to move
//! aside: nothing here deletes. Stages that build for long, need the person's
//! own files or boot a guest ask first. Nothing of Apple's is fetched; the
//! person's own images, template and IM4M stay required.
use super::*;

const IPHONE_IDENTITY: u64 = 0;
const PCC_IDENTITY: u64 = 5;
const KERNEL: &str = "kernelcache.research.vphone600";
/// Stands for the PCC StaticTrustCache until the boot stage has named it.
const UNKNOWN_TRUSTCACHE: &str = "PCC_STATIC_TRUSTCACHE_NAMED_BY_THE_BOOT_STAGE";

/// What a stage does when it runs.
#[derive(Debug, Clone)]
pub(crate) enum Action {
    /// One job of tool steps: (tools/ script, its arguments).
    Tools {
        label: &'static str,
        steps: Vec<(&'static str, Vec<String>)>,
        timeout: u64,
    },
    /// Another operation with these arguments.
    Operation {
        name: &'static str,
        arguments: Value,
    },
    /// Steps the person or an agent runs by hand, in order.
    Manual { commands: Vec<String> },
}

#[derive(Debug, Clone)]
pub(crate) struct Stage {
    pub name: &'static str,
    pub what: &'static str,
    /// "done", "pending", "partial" (an earlier run stopped halfway) or
    /// "unknown" (only the person can tell).
    pub state: &'static str,
    pub detail: Option<String>,
    /// Arguments of this operation the stage still needs.
    pub needs: Vec<&'static str>,
    pub ask_first: bool,
    pub why_ask: Option<&'static str>,
    pub action: Action,
}

/// The paths the chain reads and writes.
pub(crate) struct Layout {
    pub work: PathBuf,
    pub tooling: Option<PathBuf>,
    pub arguments: Value,
    /// The kernel the helper boots: the support's own, else the running one's.
    pub helper_kernel: PathBuf,
    pub accel: &'static str,
    /// The profile directory `ios.virtual.configure` has selected.
    pub configured: Option<PathBuf>,
}

impl Layout {
    fn w(&self, relative: &str) -> PathBuf {
        self.work.join(relative)
    }
    fn t(&self, relative: &str) -> PathBuf {
        self.tooling.as_ref().map_or_else(
            || PathBuf::from(".tooling").join(relative),
            |root| root.join(relative),
        )
    }
    fn given(&self, name: &str) -> Option<PathBuf> {
        let value = s(&self.arguments, name);
        (!value.is_empty()).then(|| PathBuf::from(value))
    }
    fn manifest(&self) -> PathBuf {
        self.given("manifest")
            .unwrap_or_else(|| self.w("your.im4m"))
    }
    /// The decrypted image of a component: given, else where setup.md puts it.
    fn image(&self, argument: &str, firmware: &str, name: &str) -> Option<PathBuf> {
        self.given(argument).or_else(|| {
            let root = self.given(firmware)?;
            let record = read_json(&self.w("inputs.json"), Some(json!({}))).ok()?;
            let file = record["images"][name].as_str()?;
            Some(root.join("decrypted").join(file))
        })
    }
    fn helper(&self) -> Value {
        json!({"support":self.t("ios-sptm/support"),"helper_engine":self.t("ios-sptm/build/qemu-system-x86_64"),
            "helper_kernel":self.helper_kernel,"bios":self.t("ios-sptm/source/pc-bios")})
    }
    fn gpu_trustcache(&self) -> Option<PathBuf> {
        read_json(&self.w("boot/boot-inputs.json"), Some(json!({})))
            .ok()
            .and_then(|record| record["gpu_trustcache"].as_str().map(PathBuf::from))
    }
}

fn string(path: impl AsRef<Path>) -> String {
    path.as_ref().to_string_lossy().into_owned()
}

fn merged(mut base: Value, extra: Value) -> Value {
    for (key, value) in extra.as_object().cloned().unwrap_or_default() {
        base[key] = value;
    }
    base
}

/// done when `receipt` in `directory` says `state`; partial when the
/// directory exists without that; pending otherwise.
fn receipt(directory: &Path, file: &str, state: &str) -> (&'static str, Option<String>) {
    let record = read_json(&directory.join(file), Some(Value::Null)).unwrap_or(Value::Null);
    if record["state"] == state {
        return ("done", None);
    }
    if directory.exists() || directory.is_symlink() {
        return (
            "partial",
            Some(format!(
                "{} exists, but its {file} does not say {state}: an earlier run stopped. Move it aside, then run the stage again.",
                directory.display()
            )),
        );
    }
    ("pending", None)
}

fn files(paths: &[PathBuf]) -> (&'static str, Option<String>) {
    let missing: Vec<String> = paths
        .iter()
        .filter(|path| !path.is_file())
        .map(|path| path.display().to_string())
        .collect();
    match missing.len() {
        0 => ("done", None),
        count if count == paths.len() => ("pending", None),
        _ => (
            "partial",
            Some(format!("still missing: {}", missing.join(", "))),
        ),
    }
}

fn needs(layout: &Layout, names: &[&'static str]) -> Vec<&'static str> {
    names
        .iter()
        .copied()
        .filter(|name| s(&layout.arguments, name).is_empty())
        .collect()
}

/// A tool step that runs only when its output is still missing.
fn step_unless(
    output: &Path,
    script: &'static str,
    arguments: Vec<String>,
) -> Option<(&'static str, Vec<String>)> {
    (!output.exists()).then_some((script, arguments))
}

/// The chain's stages in order, with what each would do now.
pub(crate) fn stages(layout: &Layout) -> Vec<Stage> {
    let l = layout;
    let mut stages = Vec::new();
    let checkout = l.tooling.is_some();
    let engine = l.t("ios-paravirt/sptm-build/qemu-system-aarch64");
    let backend = l.t("ios-guest-control/vhost-device-vsock");
    let jobs = n(&l.arguments, "jobs").max(1).to_string();
    let optimization = match s(&l.arguments, "cpu_optimization") {
        "" => "portable",
        other => other,
    };
    let (state, detail) = files(&[engine.clone(), backend.clone()]);
    stages.push(Stage {
        name: "engine",
        what: "Build the engine, its helper and the guest-control backend",
        state,
        detail: if checkout {
            detail
        } else {
            Some("Needs a Tomacco source checkout: the engine is built into its .tooling.".into())
        },
        needs: vec![],
        ask_first: true,
        why_ask: Some("It fetches pinned sources and builds for a long time; an engine already built from the same inputs is reused."),
        action: Action::Tools {
            label: "Build the virtual iOS engine",
            steps: [
                step_unless(
                    &engine,
                    "build_ios_paravirt.py",
                    vec!["--jobs".into(), jobs.clone(), "--cpu-optimization".into(), optimization.into(),
                        "--output".into(), string(l.t("ios-paravirt")), "--base-tooling".into(), string(l.t("ios-sptm"))],
                ),
                step_unless(
                    &backend,
                    "build_ios_guest_control.py",
                    vec!["--output".into(), string(l.t("ios-guest-control"))],
                ),
            ]
            .into_iter()
            .flatten()
            .collect(),
            timeout: 6 * 3600,
        },
    });
    let support = l.t("ios-sptm/support");
    let (state, detail) = files(&[
        support.join("busybox/busybox"),
        support.join("apfs/apfs.ko"),
        support.join("ldid/ldid"),
        support.join("ipsw/ipsw"),
    ]);
    stages.push(Stage {
        name: "support",
        what: "Prepare the helper support (busybox, APFS module, ldid, ipsw)",
        state,
        detail,
        needs: needs(l, &["iphone_firmware"]),
        ask_first: false,
        why_ask: None,
        action: Action::Tools {
            label: "Prepare the virtual iOS helper support",
            steps: vec![(
                "prepare_ios_emulator.py",
                vec![
                    "--firmware".into(),
                    s(&l.arguments, "iphone_firmware").into(),
                    "--output".into(),
                    string(l.w("restore-shell")),
                ],
            )],
            timeout: 3600,
        },
    });
    let linker = l.t("ios-linker/prefix/bin/aarch64-apple-darwin-ld");
    let (state, detail) = files(std::slice::from_ref(&linker));
    stages.push(Stage {
        name: "linker",
        what: "Build the Apple-format linker",
        state,
        detail,
        needs: needs(l, &["swift_lib"]),
        ask_first: false,
        why_ask: None,
        action: Action::Tools {
            label: "Build the iOS linker",
            steps: vec![(
                "build_ios_linker.py",
                vec![
                    "--swift-lib".into(),
                    s(&l.arguments, "swift_lib").into(),
                    "--jobs".into(),
                    jobs,
                ],
            )],
            timeout: 3600,
        },
    });
    let ldid = support.join("ldid/ldid");
    let tools_common = json!({"linker":linker,"ldid":ldid});
    let (state, detail) = receipt(&l.w("gpu-compiler"), "plugin.json", "built");
    stages.push(Stage {
        name: "gpu_plugin",
        what: "Build the GPU compiler plugin from the vphone-cli checkout",
        state,
        detail,
        needs: needs(l, &["vphone_cli"]),
        ask_first: false,
        why_ask: None,
        action: Action::Operation {
            name: "ios.emulator.build_gpu_plugin",
            arguments: merged(
                json!({"source":s(&l.arguments,"vphone_cli"),"output":l.w("gpu-compiler")}),
                tools_common.clone(),
            ),
        },
    });
    // The tool moves a complete directory into place, receipt included.
    let (state, detail) = match (l.w("boot/boot-inputs.json").is_file(), l.w("boot").exists()) {
        (true, _) => ("done", None),
        (false, true) => (
            "partial",
            Some(format!(
                "{} exists without boot-inputs.json. Move it aside, then run the stage again.",
                l.w("boot").display()
            )),
        ),
        (false, false) => ("pending", None),
    };
    stages.push(Stage {
        name: "boot",
        what: "Unwrap the boot components from your PCC image",
        state,
        detail,
        needs: needs(l, &["pcc_firmware"]),
        ask_first: false,
        why_ask: None,
        action: Action::Tools {
            label: "Unwrap the virtual iOS boot components",
            steps: vec![(
                "prepare_ios_boot_inputs.py",
                vec![
                    "--pcc-firmware".into(),
                    s(&l.arguments, "pcc_firmware").into(),
                    "--ipsw".into(),
                    string(support.join("ipsw/ipsw")),
                    "--output".into(),
                    string(l.w("boot")),
                ],
            )],
            timeout: 1800,
        },
    });
    let restore_tree = l.w("dtree.restore");
    let normal_tree = l.w("dtree.normal");
    let storage = l.w("storage");
    let (state, detail) = files(&[
        restore_tree.clone(),
        normal_tree.clone(),
        storage.join("system.raw"),
        storage.join("auxiliary.raw"),
        storage.join("sep.raw"),
    ]);
    let original_tree = string(l.w("boot/dtree.original"));
    stages.push(Stage {
        name: "trees",
        what: "Prepare the device's two device trees and its disks",
        state,
        detail,
        needs: vec![],
        ask_first: false,
        why_ask: None,
        action: Action::Tools {
            label: "Prepare the virtual iOS device trees and disks",
            steps: [
                step_unless(
                    &restore_tree,
                    "prepare_ios_paravirt.py",
                    vec![
                        original_tree.clone(),
                        string(&restore_tree),
                        "--memory-gib".into(),
                        "8".into(),
                        "--omit-optional-ctrr".into(),
                        "--load-sep".into(),
                        "--sep-handoff".into(),
                        "userspace".into(),
                        "--ap-cpus".into(),
                        "1".into(),
                    ],
                ),
                step_unless(
                    &normal_tree,
                    "prepare_ios_paravirt.py",
                    vec![
                        original_tree,
                        string(&normal_tree),
                        "--memory-gib".into(),
                        "8".into(),
                        "--omit-optional-ctrr".into(),
                        "--load-sep".into(),
                        "--ap-cpus".into(),
                        "6".into(),
                        "--entropy-from".into(),
                        string(&restore_tree),
                    ],
                ),
                step_unless(
                    &storage,
                    "ios_paravirt_storage.py",
                    vec![
                        "--device-tree".into(),
                        string(&restore_tree),
                        "--output".into(),
                        string(&storage),
                        "--root-gib".into(),
                        "32".into(),
                    ],
                ),
            ]
            .into_iter()
            .flatten()
            .collect(),
            timeout: 3600,
        },
    });
    let manifest = l.manifest();
    let (state, detail) = files(std::slice::from_ref(&manifest));
    stages.push(Stage {
        name: "manifest",
        what: "Make your local IM4M research manifest from the public template",
        state,
        detail,
        needs: if manifest.is_file() { vec![] } else { needs(l, &["template", "iphone_firmware", "pcc_firmware"]) },
        ask_first: true,
        why_ask: Some("It needs the public template the person downloads; read setup.md, How do I get the IM4M?, first. Never regenerate it for a device that is already restored."),
        action: Action::Tools {
            label: "Make the local IM4M research manifest",
            steps: vec![(
                "prepare_ios_research_manifest.py",
                vec!["--template".into(), s(&l.arguments, "template").into(), "--pcc-firmware".into(),
                    s(&l.arguments, "pcc_firmware").into(), "--iphone-firmware".into(),
                    s(&l.arguments, "iphone_firmware").into(), "--device-tree".into(), string(&restore_tree),
                    "--output".into(), string(&manifest)],
            )],
            timeout: 600,
        },
    });
    let helper = l.helper();
    let os = l.image("iphone_os_image", "iphone_firmware", "OS");
    let system_os = l.image(
        "iphone_systemos_image",
        "iphone_firmware",
        "Cryptex1,SystemOS",
    );
    let pcc_image = l.image("pcc_system_image", "pcc_firmware", "PCC");
    let mut userland_needs = needs(l, &["iphone_firmware", "pcc_firmware"]);
    for (name, image) in [
        ("iphone_os_image", &os),
        ("iphone_systemos_image", &system_os),
        ("pcc_system_image", &pcc_image),
    ] {
        if image.is_none() {
            userland_needs.push(name);
        }
    }
    let (state, detail) = receipt(&l.w("userland"), "userland.json", "prepared");
    stages.push(Stage {
        name: "userland",
        what: "Import the userland from your decrypted iPhone and PCC images",
        state,
        detail,
        needs: userland_needs,
        ask_first: false,
        why_ask: None,
        action: Action::Operation {
            name: "ios.emulator.prepare_userland",
            arguments: merged(
                json!({"firmware":s(&l.arguments,"iphone_firmware"),"identity":IPHONE_IDENTITY,
                    "pcc_firmware":s(&l.arguments,"pcc_firmware"),"pcc_identity":PCC_IDENTITY,
                    "pcc_system_image":pcc_image.as_ref().map(string).unwrap_or_default(),
                    "components":[{"component":"OS","path":os.as_ref().map(string).unwrap_or_default()},
                        {"component":"Cryptex1,SystemOS","path":system_os.as_ref().map(string).unwrap_or_default()}],
                    "gpu_plugin_build":l.w("gpu-compiler"),"output":l.w("userland")}),
                helper.clone(),
            ),
        },
    });
    let (state, detail) = receipt(&l.w("restore-runtime"), "restore-runtime.json", "prepared");
    stages.push(Stage {
        name: "restore_runtime",
        what: "Prepare the restore runtime with the research seal policy",
        state,
        detail,
        needs: needs(l, &["pcc_firmware"]),
        ask_first: false,
        why_ask: None,
        action: Action::Operation {
            name: "ios.emulator.prepare_restore",
            arguments: merged(
                json!({"ramdisk":l.w("boot/ramdisk.dmg"),"trustcache":l.w("boot/ramdisk.tc"),
                    "output":l.w("restore-runtime"),"accel":l.accel,
                    "research_seal":{"firmware":s(&l.arguments,"pcc_firmware"),"identity":PCC_IDENTITY,
                        "manifest":manifest,"overrides":[],"userland":l.w("userland")}}),
                helper.clone(),
            ),
        },
    });
    let cryptex_first = l.w("cryptex-first");
    let restored = b(&l.arguments, "restored") || cryptex_first.exists();
    let restore_engine = engine.clone();
    stages.push(Stage {
        name: "restore",
        what: "Restore iOS onto the device's disks",
        state: if restored { "done" } else { "unknown" },
        detail: (!restored).then(|| {
            "Only the restore's own result tells whether it finished; pass restored:true once it reported restore_completed:true.".into()
        }),
        needs: vec![],
        ask_first: true,
        why_ask: Some("It boots a virtual iPhone for about half an hour; run these three calls by hand, in order, and job.cancel the bridge and the guest after restore_completed:true."),
        action: Action::Manual {
            commands: vec![
                format!("tomaccoctl call ios.emulator.paravirtual '{}'", json!({"engine":restore_engine,"seconds":3600,
                    "sep_boot_role":"restore","sep_handoff":"userspace","sep_measure_manifest":true,"usb":true,
                    "boot_manifest":manifest,"sep_manifest":manifest,"kernel":l.w("boot/kernel"),"sptm":l.w("boot/sptm"),
                    "txm":l.w("boot/txm"),"sep":l.w("boot/sep"),"device_tree":restore_tree,
                    "ramdisk":l.w("restore-runtime/ramdisk.dmg"),"trustcache":l.w("restore-runtime/ramdisk.tc"),
                    "writable_root_disk":storage.join("system.raw"),"writable_auxiliary_disk":storage.join("auxiliary.raw"),
                    "sep_storage":storage.join("sep.raw")})),
                "tomaccoctl call ios.emulator.usb_bridge '{\"job_id\":\"GUEST_JOB\",\"seconds\":3600}'".into(),
                format!("tomaccoctl call ios.emulator.restore '{}'", json!({"bridge":"BRIDGE",
                    "firmware":s(&l.arguments,"pcc_firmware"),"identity":PCC_IDENTITY,"manifest":manifest,
                    "system_image":l.w("userland/images/system.dmg"),"userland":l.w("userland"),"seconds":3300})),
            ],
        },
    });
    let gpu_trustcache = l.gpu_trustcache();
    let cryptex = |output: PathBuf, development: bool| {
        let mut arguments = merged(
            json!({"disk":storage.join("system.raw"),"userland":l.w("userland"),"kernel":l.w("boot/kernel"),
                "boot_manifest":manifest,"device_tree":normal_tree,
                "gpu_trustcache":gpu_trustcache.as_ref().map_or(UNKNOWN_TRUSTCACHE.into(), string),
                "installer_control":true,"output":output}),
            helper.clone(),
        );
        if development {
            arguments["development_tools"] = json!(l.w("guest-tools"));
        }
        arguments
    };
    let (state, detail) = receipt(&cryptex_first, "cryptex.json", "installed");
    stages.push(Stage {
        name: "cryptex_first",
        what: "Install the Cryptexes (first pass)",
        state,
        detail,
        needs: vec![],
        ask_first: false,
        why_ask: None,
        action: Action::Operation {
            name: "ios.emulator.prepare_cryptexes",
            arguments: cryptex(cryptex_first.clone(), false),
        },
    });
    let (state, detail) = receipt(&l.w("guest-tools"), "guest-tools.json", "built");
    stages.push(Stage {
        name: "guest_tools",
        what: "Build the guest tools from the first pass's trust cache",
        state,
        detail,
        needs: needs(l, &["clang"]),
        ask_first: false,
        why_ask: None,
        action: Action::Operation {
            name: "ios.emulator.build_guest_tools",
            arguments: merged(
                json!({"trustcache":cryptex_first.join("system.tc"),"output":l.w("guest-tools"),
                    "clang":s(&l.arguments,"clang"),"minimum_ios":"27.0","agent":true,"application_probe":true,
                    "development_signing":true,"quiet_services":true}),
                tools_common,
            ),
        },
    });
    let final_cryptex = l.w("cryptex");
    let (state, detail) = receipt(&final_cryptex, "cryptex.json", "installed");
    stages.push(Stage {
        name: "cryptex",
        what: "Install the Cryptexes again, with the guest tools",
        state,
        detail,
        needs: vec![],
        ask_first: false,
        why_ask: None,
        action: Action::Operation {
            name: "ios.emulator.prepare_cryptexes",
            arguments: cryptex(final_cryptex.clone(), true),
        },
    });
    let fileport = l.w("kernel-fileport");
    let kernel = l.w("kernel").join(KERNEL);
    let (state, detail) = files(std::slice::from_ref(&kernel));
    stages.push(Stage {
        name: "kernel",
        what: "Apply the two kernel adapters",
        state,
        detail,
        needs: vec![],
        ask_first: false,
        why_ask: None,
        action: Action::Tools {
            label: "Prepare the virtual iOS kernel",
            steps: [
                step_unless(
                    &fileport,
                    "prepare_ios_fileport_compat.py",
                    vec![
                        "--kernel".into(),
                        string(final_cryptex.join(KERNEL)),
                        "--output".into(),
                        string(&fileport),
                    ],
                ),
                step_unless(
                    &l.w("kernel"),
                    "prepare_ios_workloop_compat.py",
                    vec![
                        "--kernel".into(),
                        string(fileport.join(KERNEL)),
                        "--output".into(),
                        string(l.w("kernel")),
                    ],
                ),
            ]
            .into_iter()
            .flatten()
            .collect(),
            timeout: 1800,
        },
    });
    let (state, detail) = receipt(
        &l.w("first-install"),
        "guest-tools-install.json",
        "installed",
    );
    stages.push(Stage {
        name: "first_install",
        what: "Install the guest tools for the first start",
        state,
        detail,
        needs: vec![],
        ask_first: false,
        why_ask: None,
        action: Action::Operation {
            name: "ios.emulator.prepare_guest_tools",
            arguments: merged(
                json!({"disk":final_cryptex.join("system.raw"),"auxiliary_disk":storage.join("auxiliary.raw"),
                    "sep_storage":storage.join("sep.raw"),"tools":l.w("guest-tools"),
                    "trustcache":final_cryptex.join("system.tc"),"device_tree":final_cryptex.join("device-tree"),
                    "graphics":final_cryptex.join("graphics/payload"),"development_device":true,
                    "output":l.w("first-install")}),
                helper,
            ),
        },
    });
    let profile = l.w("profile");
    let (state, detail) = files(&[profile.join("profile.json")]);
    let mut profile_arguments = vec![
        "--directory".into(),
        string(&profile),
        "--cryptex".into(),
        string(&final_cryptex),
        "--boot-inputs".into(),
        string(l.w("boot")),
        "--kernel".into(),
        string(&kernel),
        "--tools".into(),
        string(l.w("guest-tools")),
        "--install".into(),
        string(l.w("first-install")),
    ];
    if !s(&l.arguments, "clang").is_empty() {
        profile_arguments.extend(["--clang".into(), s(&l.arguments, "clang").into()]);
    }
    stages.push(Stage {
        name: "profile",
        what: "Write the profile",
        state,
        detail,
        needs: vec![],
        ask_first: false,
        why_ask: None,
        action: Action::Tools {
            label: "Write the virtual iOS device profile",
            steps: vec![("ios_virtual_profile.py", profile_arguments)],
            timeout: 600,
        },
    });
    stages.push(Stage {
        name: "configure",
        what: "Select the profile",
        state: if l.configured.as_deref() == Some(profile.as_path()) {
            "done"
        } else {
            "pending"
        },
        detail: None,
        needs: vec![],
        ask_first: false,
        why_ask: None,
        action: Action::Operation {
            name: "ios.virtual.configure",
            arguments: json!({"directory":profile}),
        },
    });
    stages
}

fn shell_word(word: &str) -> String {
    if !word.is_empty()
        && word
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "/._-=,:+@".contains(c))
    {
        word.into()
    } else {
        format!("'{}'", word.replace('\'', "'\\''"))
    }
}

/// What a stage would run, as commands a person can read.
pub(crate) fn commands(action: &Action) -> Vec<String> {
    match action {
        Action::Tools { steps, .. } => steps
            .iter()
            .map(|(script, arguments)| {
                std::iter::once(format!("python3 tools/{script}"))
                    .chain(arguments.iter().map(|word| shell_word(word)))
                    .collect::<Vec<_>>()
                    .join(" ")
            })
            .collect(),
        Action::Operation { name, arguments } => {
            vec![format!(
                "tomaccoctl call {name} {}",
                shell_word(&arguments.to_string())
            )]
        }
        Action::Manual { commands } => commands.clone(),
    }
}

fn describe(stage: &Stage) -> Value {
    json!({"stage":stage.name,"what":stage.what,"state":stage.state,"detail":stage.detail,
        "needs":stage.needs,"ask_first":stage.ask_first,"why_ask":stage.why_ask,
        "manual":matches!(stage.action, Action::Manual{..}),"commands":commands(&stage.action)})
}

/// The first stage that is not done, unless the phone is set up already.
pub(crate) fn next(stages: &[Stage]) -> Option<&Stage> {
    if ready(stages) {
        return None;
    }
    stages.iter().find(|stage| stage.state != "done")
}

/// Whether this work directory's profile is written and selected: the phone
/// starts from it, so the chain has nothing left to run. Earlier outputs may
/// have been moved, deleted or made by an older Tomacco; the start does not
/// read them, and the plan still lists their state.
pub(crate) fn ready(stages: &[Stage]) -> bool {
    ["profile", "configure"].iter().all(|name| {
        stages
            .iter()
            .any(|stage| stage.name == *name && stage.state == "done")
    })
}

fn layout(a: &Value) -> Result<Layout> {
    let work = path(s(a, "work"), true, false)?;
    let tooling = source_checkout().map(|root| root.join(".tooling"));
    let support_kernel = tooling
        .as_ref()
        .map(|root| root.join("ios-sptm/support/apfs/vmlinuz"))
        .filter(|path| path.is_file());
    let release = fs::read_to_string("/proc/sys/kernel/osrelease").unwrap_or_default();
    let helper_kernel = if !s(a, "helper_kernel").is_empty() {
        PathBuf::from(s(a, "helper_kernel"))
    } else {
        support_kernel.unwrap_or_else(|| {
            PathBuf::from(format!("/usr/lib/modules/{}/vmlinuz", release.trim()))
        })
    };
    Ok(Layout {
        work,
        tooling,
        arguments: a.clone(),
        helper_kernel,
        accel: if fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open("/dev/kvm")
            .is_ok()
        {
            "kvm"
        } else {
            "tcg"
        },
        configured: virtual_phone::directory().ok(),
    })
}

/// The person's inputs as tools/check_ios_inputs.py sees them, and the names
/// of the decrypted images it found, kept in `work`/inputs.json.
fn inputs(l: &Layout) -> Value {
    let mut argv = vec![];
    for (argument, flag) in [
        ("iphone_firmware", "--iphone-firmware"),
        ("pcc_firmware", "--pcc-firmware"),
        ("iphone_os_image", "--iphone-os-image"),
        ("iphone_systemos_image", "--iphone-systemos-image"),
        ("pcc_system_image", "--pcc-system-image"),
        ("template", "--template"),
    ] {
        if !s(&l.arguments, argument).is_empty() {
            argv.extend([flag.to_string(), s(&l.arguments, argument).to_string()]);
        }
    }
    if l.manifest().is_file() {
        argv.extend(["--manifest".into(), string(l.manifest())]);
        if l.w("dtree.restore").is_file() {
            argv.extend(["--device-tree".into(), string(l.w("dtree.restore"))]);
        }
    }
    let run = || -> Result<Value> {
        let output = Command::new(toolchain::find("python3")?)
            .arg(research::script("check_ios_inputs.py")?)
            .args(&argv)
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .output()?;
        Ok(serde_json::from_slice(&output.stdout)?)
    };
    run().unwrap_or_else(|error| json!({"ok":null,"error":error.value()}))
}

/// Remembers the image names the check found, so later plans find the images
/// without the arguments.
fn remember_images(l: &Layout, report: &Value) -> Result<()> {
    let mut images = serde_json::Map::new();
    for row in report["checks"].as_array().into_iter().flatten() {
        let key = match s(row, "input") {
            "iPhone OS image" => "OS",
            "iPhone SystemOS image" => "Cryptex1,SystemOS",
            "PCC system image" => "PCC",
            _ => continue,
        };
        if let Some(name) = row["path"]
            .as_str()
            .and_then(|path| Path::new(path).file_name())
        {
            images.insert(key.into(), json!(name.to_string_lossy()));
        }
    }
    if images.is_empty() || !l.work.is_dir() {
        return Ok(());
    }
    atomic_write(
        &l.w("inputs.json"),
        &serde_json::to_vec_pretty(&json!({"images":images}))?,
    )
}

pub fn setup(a: &Value) -> Result {
    let l = layout(a)?;
    let inputs = inputs(&l);
    remember_images(&l, &inputs)?;
    let all = stages(&l);
    if s(a, "step") != "run" {
        let host =
            host_check::check(&json!({})).unwrap_or_else(|error| json!({"error":error.value()}));
        let upcoming = next(&all).map(describe);
        return Ok(json!({
            "work": l.work, "checkout": l.tooling.is_some(),
            "done": all.iter().all(|stage| stage.state == "done"),
            "ready": ready(&all),
            "next": upcoming,
            "stages": all.iter().map(describe).collect::<Vec<_>>(),
            "inputs": inputs, "host": host,
            "note": "Each stage is skipped once its output is complete. Call step run for the next stage, then job.wait its job. Your own restore images, decrypted, the public template and the resulting IM4M stay yours to supply.",
        }));
    }
    run(&l, &all, s(a, "stage"), b(a, "confirm"), &inputs)
}

fn run(l: &Layout, all: &[Stage], name: &str, confirm: bool, inputs: &Value) -> Result {
    let index = if name.is_empty() {
        if ready(all) {
            return Ok(
                json!({"started":false,"done":true,"ready":true,"message":"The profile is written and selected. Start the phone with ios.virtual.start."}),
            );
        }
        match all.iter().position(|stage| stage.state != "done") {
            Some(index) => index,
            None => {
                return Ok(
                    json!({"started":false,"done":true,"message":"Every stage is done. Start the phone with ios.virtual.start."}),
                );
            }
        }
    } else {
        all.iter()
            .position(|stage| stage.name == name)
            .ok_or_else(|| {
                Error::new(
                    "invalid_argument",
                    format!("No setup stage is called {name}"),
                )
            })?
    };
    let stage = &all[index];
    if stage.state == "done" {
        return Ok(
            json!({"started":false,"skipped":true,"stage":describe(stage),
            "message":format!("{} is already done; nothing ran.", stage.what)}),
        );
    }
    if let Some(earlier) = all[..index].iter().find(|stage| stage.state != "done") {
        return Err(Error::new(
            "ios_setup_out_of_order",
            format!(
                "The {} stage is not done yet ({}); the chain runs in order",
                earlier.name, earlier.what
            ),
        )
        .remedy(format!("Run the {} stage first.", earlier.name))
        .details(json!({"stage":describe(earlier)})));
    }
    if stage.state == "partial" {
        return Err(Error::new(
            "ios_setup_partial",
            stage.detail.clone().unwrap_or_default(),
        )
        .remedy("Move the half-finished output aside (nothing is deleted for you), then run the stage again.")
        .details(json!({"stage":describe(stage)})));
    }
    if stage.name == "engine" && l.tooling.is_none() {
        return Err(Error::new(
            "ios_setup_needs_checkout",
            "The engine is built in a Tomacco source checkout's .tooling",
        )
        .remedy("Run tomaccoctl from a source checkout (git clone the project, then use its tomaccoctl)."));
    }
    if !stage.needs.is_empty() {
        return Err(Error::new(
            "ios_setup_needs_input",
            format!("{} needs: {}", stage.what, stage.needs.join(", ")),
        )
        .remedy("Pass these arguments; they are the person's own files and checkouts. docs/virtual-ios/setup.md says where each comes from.")
        .details(json!({"needs":stage.needs,"stage":describe(stage)})));
    }
    if let Action::Operation { arguments, .. } = &stage.action {
        if arguments["gpu_trustcache"] == UNKNOWN_TRUSTCACHE {
            return Err(Error::new(
                "ios_setup_needs_input",
                "The boot stage found no StaticTrustCache in your PCC image's research identity",
            )
            .remedy("Check that pcc_firmware is the unpacked PCC 23E5207q image, move work/boot aside and run the boot stage again."));
        }
    }
    if inputs["ok"] == false && ["support", "manifest", "userland", "boot"].contains(&stage.name) {
        return Err(Error::new(
            "ios_setup_inputs_invalid",
            "Some of the person's own inputs are not the ones the chain needs",
        )
        .remedy("Read each failed entry's problem and fix in inputs; nothing long has started.")
        .details(json!({"inputs":inputs})));
    }
    if let Action::Manual { .. } = stage.action {
        return Err(
            Error::new("ios_setup_manual", format!("{} is run by hand", stage.what))
                .remedy(stage.why_ask.unwrap_or("Run the commands in order."))
                .details(json!({"stage":describe(stage)})),
        );
    }
    if stage.ask_first && !confirm {
        return Err(Error::new(
            "ios_setup_confirm",
            format!("{}: ask the person first", stage.what),
        )
        .remedy(format!(
            "{} Call again with confirm:true once they agree.",
            stage.why_ask.unwrap_or("")
        ))
        .details(json!({"stage":describe(stage),"consent":{
            "argument":"confirm","value":true,
            "question":format!("{} now? {}", stage.what, stage.why_ask.unwrap_or("")).trim_end(),
        }})));
    }
    fs::create_dir_all(&l.work)?;
    let job = match &stage.action {
        Action::Operation { name, arguments } => crate::api::dispatch(name, arguments)?,
        Action::Tools {
            label,
            steps,
            timeout,
        } => {
            let python = toolchain::find("python3")?;
            let mut argv = Vec::new();
            for (script, arguments) in steps {
                let script = match &l.tooling {
                    Some(tooling) if stage.name == "engine" => {
                        tooling.parent().unwrap().join("tools").join(script)
                    }
                    _ => research::script(script)?,
                };
                let mut command = vec![python.clone(), string(script)];
                command.extend(arguments.iter().cloned());
                argv.push(json!({"argv":command}));
            }
            jobs::submit(
                label,
                json!({"cwd":l.tooling.as_ref().and_then(|t| t.parent()).map_or_else(|| l.work.clone(), Path::to_path_buf),
                    "steps":argv,"timeout":timeout,
                    "exclusive":format!("ios-setup:{}", l.work.display()),"single_active":true}),
            )?
        }
        Action::Manual { .. } => unreachable!(),
    };
    Ok(json!({"started":true,"stage":describe(stage),"job":job,
        "message":format!("{} started. job.wait it, then call step run again for the next stage.", stage.what)}))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> (tempfile::TempDir, Layout) {
        let root = tempfile::tempdir().unwrap();
        let work = root.path().join("phone");
        let tooling = root.path().join("checkout/.tooling");
        let layout = Layout {
            work,
            tooling: Some(tooling),
            arguments: json!({"jobs":8,"iphone_firmware":"/phone/iphone-ipsw","pcc_firmware":"/phone/pcc",
                "iphone_os_image":"/phone/iphone-ipsw/decrypted/043-69462-662.dmg",
                "iphone_systemos_image":"/phone/iphone-ipsw/decrypted/043-70113-711.dmg",
                "pcc_system_image":"/phone/pcc/decrypted/094-39278-029.dmg","template":"/phone/ticket.shsh2",
                "vphone_cli":"/phone/vphone-cli","swift_lib":"/swift/usr/lib/swift","clang":"/clang-22/clang"}),
            helper_kernel: "/usr/lib/modules/6.17.1/vmlinuz".into(),
            accel: "kvm",
            configured: None,
        };
        (root, layout)
    }

    fn touch(path: &Path, text: &str) {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, text).unwrap();
    }

    fn stage<'a>(stages: &'a [Stage], name: &str) -> &'a Stage {
        stages.iter().find(|stage| stage.name == name).unwrap()
    }

    #[test]
    fn a_fresh_machine_starts_with_the_engine_and_lists_the_chain_in_order() {
        let (_root, layout) = fixture();
        let all = stages(&layout);
        let names: Vec<&str> = all.iter().map(|stage| stage.name).collect();
        assert_eq!(
            names,
            [
                "engine",
                "support",
                "linker",
                "gpu_plugin",
                "boot",
                "trees",
                "manifest",
                "userland",
                "restore_runtime",
                "restore",
                "cryptex_first",
                "guest_tools",
                "cryptex",
                "kernel",
                "first_install",
                "profile",
                "configure"
            ]
        );
        let first = next(&all).unwrap();
        assert_eq!(first.name, "engine");
        assert!(first.ask_first);
        assert_eq!(
            commands(&first.action)[0],
            format!(
                "python3 tools/build_ios_paravirt.py --jobs 8 --cpu-optimization portable --output {0}/ios-paravirt --base-tooling {0}/ios-sptm",
                layout.t("").display().to_string().trim_end_matches('/')
            )
        );
        // Every operation call the chain makes passes that operation's own argument check.
        for stage in &all {
            if let Action::Operation { name, arguments } = &stage.action {
                crate::api::arguments(name, arguments)
                    .unwrap_or_else(|error| panic!("{}: {:?}", stage.name, error.value()));
            }
        }
    }

    #[test]
    fn finished_stages_are_skipped_and_half_finished_ones_reported() {
        let (_root, layout) = fixture();
        for path in [
            "ios-paravirt/sptm-build/qemu-system-aarch64",
            "ios-guest-control/vhost-device-vsock",
            "ios-sptm/support/busybox/busybox",
            "ios-sptm/support/apfs/apfs.ko",
            "ios-sptm/support/ldid/ldid",
            "ios-sptm/support/ipsw/ipsw",
            "ios-linker/prefix/bin/aarch64-apple-darwin-ld",
        ] {
            touch(&layout.t(path), "");
        }
        touch(
            &layout.w("gpu-compiler/plugin.json"),
            r#"{"state":"built"}"#,
        );
        touch(
            &layout.w("boot/boot-inputs.json"),
            r#"{"gpu_trustcache":"/phone/pcc/Firmware/static.trustcache"}"#,
        );
        let all = stages(&layout);
        assert_eq!(next(&all).unwrap().name, "trees");
        touch(&layout.w("dtree.restore"), "");
        let all = stages(&layout);
        let trees = stage(&all, "trees");
        assert_eq!(trees.state, "partial");
        // The step whose output exists is left out; the others still run.
        let commands = commands(&trees.action);
        assert_eq!(commands.len(), 2);
        assert!(commands[0].contains("--entropy-from"));
        touch(&layout.w("userland/half"), "");
        let userland = stage(&stages(&layout), "userland").clone();
        assert_eq!(userland.state, "partial");
        assert!(userland.detail.unwrap().contains("Move it aside"));
        let cryptex = stage(&stages(&layout), "cryptex_first").clone();
        let Action::Operation { arguments, .. } = cryptex.action else {
            panic!()
        };
        assert_eq!(
            arguments["gpu_trustcache"],
            "/phone/pcc/Firmware/static.trustcache"
        );
    }

    #[test]
    fn the_restore_is_by_hand_and_known_done_only_from_the_person_or_a_later_stage() {
        let (_root, mut layout) = fixture();
        let restore = stage(&stages(&layout), "restore").clone();
        assert_eq!(restore.state, "unknown");
        assert!(matches!(restore.action, Action::Manual { .. }));
        let calls = commands(&restore.action);
        assert_eq!(calls.len(), 3);
        assert!(calls[0].starts_with("tomaccoctl call ios.emulator.paravirtual '"));
        layout.arguments["restored"] = json!(true);
        assert_eq!(stage(&stages(&layout), "restore").state, "done");
        layout.arguments["restored"] = json!(false);
        touch(
            &layout.w("cryptex-first/cryptex.json"),
            r#"{"state":"installed"}"#,
        );
        assert_eq!(stage(&stages(&layout), "restore").state, "done");
    }

    #[test]
    fn missing_person_inputs_are_named_per_stage() {
        let (_root, mut layout) = fixture();
        layout.arguments = json!({"jobs":8});
        let all = stages(&layout);
        assert_eq!(stage(&all, "support").needs, ["iphone_firmware"]);
        assert_eq!(
            stage(&all, "manifest").needs,
            ["template", "iphone_firmware", "pcc_firmware"]
        );
        assert_eq!(
            stage(&all, "userland").needs,
            [
                "iphone_firmware",
                "pcc_firmware",
                "iphone_os_image",
                "iphone_systemos_image",
                "pcc_system_image"
            ]
        );
        // An IM4M the person already has needs no template.
        touch(&layout.w("your.im4m"), "im4m");
        let all = stages(&layout);
        assert_eq!(stage(&all, "manifest").state, "done");
        assert!(stage(&all, "manifest").needs.is_empty());
        // The images found by the input check are remembered for later plans.
        layout.arguments["iphone_firmware"] = json!("/phone/iphone-ipsw");
        layout.arguments["pcc_firmware"] = json!("/phone/pcc");
        fs::create_dir_all(&layout.work).unwrap();
        remember_images(
            &layout,
            &json!({"checks":[
            {"input":"iPhone OS image","path":"/phone/iphone-ipsw/decrypted/043-1.dmg"},
            {"input":"iPhone SystemOS image","path":"/phone/iphone-ipsw/decrypted/043-2.dmg"},
            {"input":"PCC system image","path":"/phone/pcc/decrypted/094-3.dmg"}]}),
        )
        .unwrap();
        let userland = stage(&stages(&layout), "userland").clone();
        assert!(userland.needs.is_empty());
        let Action::Operation { arguments, .. } = userland.action else {
            panic!()
        };
        assert_eq!(
            arguments["pcc_system_image"],
            "/phone/pcc/decrypted/094-3.dmg"
        );
        assert_eq!(
            arguments["components"][1]["path"],
            "/phone/iphone-ipsw/decrypted/043-2.dmg"
        );
    }

    #[test]
    fn run_refuses_what_would_skip_ahead_lack_input_or_need_a_yes() {
        let (_root, mut layout) = fixture();
        let inputs = json!({"ok":true});
        let all = stages(&layout);
        let error = run(&layout, &all, "", false, &inputs).unwrap_err();
        assert_eq!(error.code, "ios_setup_confirm");
        // The argument that carries the person's yes, for MCP elicitation.
        let consent = &error.details.as_ref().unwrap()["consent"];
        assert_eq!(
            (s(consent, "argument"), &consent["value"]),
            ("confirm", &json!(true))
        );
        assert!(s(consent, "question").ends_with('?') || s(consent, "question").ends_with('.'));
        let error = run(&layout, &all, "linker", false, &inputs).unwrap_err();
        assert_eq!(error.code, "ios_setup_out_of_order");
        let error = run(&layout, &all, "nothing", false, &inputs).unwrap_err();
        assert_eq!(error.code, "invalid_argument");
        for path in [
            "ios-paravirt/sptm-build/qemu-system-aarch64",
            "ios-guest-control/vhost-device-vsock",
        ] {
            touch(&layout.t(path), "");
        }
        layout.arguments["iphone_firmware"] = json!("");
        let all = stages(&layout);
        let error = run(&layout, &all, "", false, &inputs).unwrap_err();
        assert_eq!(error.code, "ios_setup_needs_input");
        layout.arguments["iphone_firmware"] = json!("/phone/iphone-ipsw");
        let all = stages(&layout);
        let error = run(&layout, &all, "", false, &json!({"ok":false})).unwrap_err();
        assert_eq!(error.code, "ios_setup_inputs_invalid");
        let skipped = run(&layout, &all, "engine", false, &inputs).unwrap();
        assert_eq!(skipped["skipped"], true);
        assert_eq!(skipped["started"], false);
        layout.tooling = None;
        let all = stages(&layout);
        assert_eq!(
            run(&layout, &all, "", true, &inputs).unwrap_err().code,
            "ios_setup_needs_checkout"
        );
    }

    #[test]
    fn the_restore_stage_is_never_started_by_run() {
        let (_root, layout) = fixture();
        let mut all = stages(&layout);
        for stage in all.iter_mut().take_while(|stage| stage.name != "restore") {
            stage.state = "done";
        }
        let error = run(&layout, &all, "", true, &json!({"ok":true})).unwrap_err();
        assert_eq!(error.code, "ios_setup_manual");
        assert_eq!(
            error.value()["details"]["stage"]["commands"]
                .as_array()
                .unwrap()
                .len(),
            3
        );
    }

    #[test]
    fn the_last_stage_selects_the_profile_and_then_all_is_done() {
        let (_root, mut layout) = fixture();
        layout.configured = Some(layout.w("profile"));
        let all = stages(&layout);
        assert_eq!(stage(&all, "configure").state, "done");
        let mut all = all;
        for stage in all.iter_mut() {
            stage.state = "done";
        }
        let answer = run(&layout, &all, "", false, &json!({"ok":true})).unwrap();
        assert_eq!(answer["done"], true);
        assert!(next(&all).is_none());
    }

    #[test]
    fn a_written_and_selected_profile_is_ready_whatever_came_before() {
        // A work directory set up by hand or by an older Tomacco: the profile
        // runs, but the engine is elsewhere and earlier outputs are partial.
        let (_root, mut layout) = fixture();
        touch(&layout.w("profile/profile.json"), "{}");
        let all = stages(&layout);
        assert_eq!(stage(&all, "profile").state, "done");
        assert!(!ready(&all));
        assert_eq!(next(&all).unwrap().name, "engine");
        layout.configured = Some(layout.w("profile"));
        let all = stages(&layout);
        assert_ne!(stage(&all, "engine").state, "done");
        assert!(ready(&all));
        assert!(next(&all).is_none());
        let answer = run(&layout, &all, "", false, &json!({"ok":true})).unwrap();
        assert_eq!(
            (answer["started"].clone(), answer["ready"].clone()),
            (json!(false), json!(true))
        );
        // A stage asked for by name is still judged on its own.
        let error = run(&layout, &all, "engine", false, &json!({"ok":true})).unwrap_err();
        assert_eq!(error.code, "ios_setup_confirm");
    }

    #[test]
    fn commands_quote_what_a_shell_would_split() {
        assert_eq!(shell_word("/phone/a b"), "'/phone/a b'");
        assert_eq!(shell_word("it's"), "'it'\\''s'");
        assert_eq!(shell_word("--jobs"), "--jobs");
    }
}
