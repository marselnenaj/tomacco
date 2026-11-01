//! `ios.virtual.host_check`: whether this machine can run the virtual iOS
//! device, as pass, warn or fail per item with a remedy. It reads /proc,
//! /sys, the PATH and the filesystem it is asked about; it starts nothing
//! and writes nothing.
use super::*;

const GIB: u64 = 1 << 30;
/// The guest's memory and the largest translation cache a profile sets.
const GUEST_MEMORY: u64 = 10 * GIB;
/// What setup.md budgets for the whole chain, and one session's disk copy.
const SETUP_DISK: u64 = 100 * GIB;
const START_DISK: u64 = 32 * GIB;
/// The graphics memory the README asks for. A "16 GiB" card reports a little
/// less (the measured one: 15.98 GiB), so 15 GiB counts as enough.
const WANTED_VRAM: u64 = 16 * GIB;
const ENOUGH_VRAM: u64 = 15 * GIB;
/// Memory with room for the desktop beside the guest.
const COMFORTABLE_MEMORY: u64 = 16 * GIB;
const DOCS: &str = "docs/virtual-ios";

/// One graphics card as /sys describes it.
#[derive(Debug, Default, Clone)]
pub(crate) struct Card {
    pub pci: String,
    pub driver: String,
    pub vram_total: Option<u64>,
    pub vram_used: Option<u64>,
}

/// Everything the check looks at, gathered first so the verdicts can be tested
/// with any machine's facts.
#[derive(Debug, Default, Clone)]
pub(crate) struct Facts {
    pub architecture: String,
    pub cpus: usize,
    pub cpu_flags: Vec<String>,
    pub memory_total: Option<u64>,
    pub memory_available: Option<u64>,
    pub kvm: bool,
    pub directory: PathBuf,
    pub free: Option<u64>,
    /// What the next step needs on that filesystem, and which step it is.
    pub needed: u64,
    pub needed_for: String,
    pub cards: Vec<Card>,
    pub vulkan_drivers: Vec<String>,
    pub kernel: String,
    /// The helper support directory, and what its APFS module was built for.
    pub support: Option<PathBuf>,
    pub apfs_release: Option<String>,
    pub apfs_vmlinuz: bool,
    pub passt: Option<PathBuf>,
}

fn item(name: &str, status: &str, found: Value, message: String, remedy: &str) -> Value {
    json!({"check":name,"status":status,"found":found,"message":message,
        "remedy":if status == "pass" { Value::Null } else { json!(remedy) }})
}

fn gib(bytes: u64) -> String {
    format!("{:.1} GiB", bytes as f64 / GIB as f64)
}

/// The verdicts, in a fixed order.
pub(crate) fn evaluate(facts: &Facts) -> Vec<Value> {
    let mut items = Vec::new();
    let x86 = facts.architecture == "x86_64";
    items.push(item(
        "architecture",
        if x86 { "pass" } else { "fail" },
        json!(facts.architecture),
        format!("The host is {}.", facts.architecture),
        "The virtual iOS device needs an x86_64 Linux host; the engine and its helper are built for it only.",
    ));
    let virtualization = facts
        .cpu_flags
        .iter()
        .any(|flag| flag == "vmx" || flag == "svm");
    let cpu_status = if facts.cpus < 8 { "warn" } else { "pass" };
    items.push(item(
        "cpu",
        cpu_status,
        json!({"logical_cpus":facts.cpus,"virtualization":virtualization,
            "avx2":facts.cpu_flags.iter().any(|flag| flag == "avx2")}),
        format!(
            "{} logical processors. Every guest processor is emulated: a start uses six application processors and a SEP.",
            facts.cpus
        ),
        "Fewer than 8 logical processors work but are slower than anything measured. Close other heavy work during a start.",
    ));
    items.push(item(
        "kvm",
        if facts.kvm { "pass" } else { "warn" },
        json!({"dev_kvm":facts.kvm,"virtualization":virtualization}),
        if facts.kvm {
            "/dev/kvm is usable, so the x86_64 helper that prepares disks runs accelerated.".into()
        } else {
            "/dev/kvm is not usable; the helper that prepares disks falls back to emulation and is much slower.".into()
        },
        if virtualization {
            "Add yourself to the kvm group (or the group that owns /dev/kvm) and log in again."
        } else {
            "Turn on virtualization (VT-x or AMD-V) in the firmware settings, then load kvm_intel or kvm_amd."
        },
    ));
    let memory = match facts.memory_total {
        None => item(
            "memory",
            "warn",
            Value::Null,
            "The memory size could not be read from /proc/meminfo.".into(),
            "Make sure the machine has at least 16 GiB.",
        ),
        Some(total) => {
            let status = if total < GUEST_MEMORY + 2 * GIB {
                "fail"
            } else if total < COMFORTABLE_MEMORY {
                "warn"
            } else {
                "pass"
            };
            item(
                "memory",
                status,
                json!({"total_bytes":total,"available_bytes":facts.memory_available}),
                format!(
                    "{} of memory, {} available now. The guest takes 8 GiB and up to 2 GiB for translated code.",
                    gib(total),
                    facts.memory_available.map_or("unknown".into(), gib)
                ),
                "With less than 16 GiB, lower translation_cache_mib in profile.json and close other programs; under 12 GiB the guest does not fit.",
            )
        }
    };
    items.push(memory);
    items.push(match facts.free {
        None => item(
            "disk",
            "warn",
            json!({"directory":facts.directory}),
            format!("The free space on the filesystem of {} could not be read.", facts.directory.display()),
            "Name an existing directory with directory.",
        ),
        Some(free) => {
            let status = if free < facts.needed {
                "fail"
            } else if free < facts.needed + START_DISK {
                "warn"
            } else {
                "pass"
            };
            item(
                "disk",
                status,
                json!({"directory":facts.directory,"free_bytes":free,"needed_bytes":facts.needed,"for":facts.needed_for}),
                format!(
                    "{} free on the filesystem of {}; {} needs about {}.",
                    gib(free),
                    facts.directory.display(),
                    facts.needed_for,
                    gib(facts.needed)
                ),
                "Free space there, or put the chain's outputs or the profile's installs on a filesystem with room. btrfs, or XFS with reflink, shares a session's disk copy with its base.",
            )
        }
    });
    items.push(graphics(facts));
    items.push(helper_kernel(facts));
    items.push(item(
        "passt",
        if facts.passt.is_some() { "pass" } else { "warn" },
        json!(facts.passt),
        match &facts.passt {
            Some(path) => format!("passt is at {}; the guest gets a network.", path.display()),
            None => "passt is not installed, so the guest boots without a network.".into(),
        },
        "Install passt from your distribution (the package is called passt) if apps in the guest need a network.",
    ));
    items
}

fn graphics(facts: &Facts) -> Value {
    let cards: Vec<Value> = facts
        .cards
        .iter()
        .map(|card| json!({"pci":card.pci,"driver":card.driver,"vram_total_bytes":card.vram_total,"vram_used_bytes":card.vram_used}))
        .collect();
    let found = json!({"cards":cards,"vulkan_drivers":facts.vulkan_drivers});
    if facts.vulkan_drivers.is_empty() {
        return item(
            "graphics",
            "fail",
            found,
            "No Vulkan driver is installed (no ICD file in /usr/share/vulkan/icd.d or /etc/vulkan/icd.d).".into(),
            "Install your card's Vulkan driver: Mesa's vulkan-radeon or vulkan-intel, or NVIDIA's driver with its Vulkan support.",
        );
    }
    let Some(card) = facts
        .cards
        .iter()
        .filter(|card| card.vram_total.is_some())
        .max_by_key(|card| card.vram_total)
    else {
        return item(
            "graphics",
            "warn",
            found,
            "A Vulkan driver is installed, but no card reports its graphics memory here, so its size is unknown.".into(),
            "The guest was measured on a 16 GiB card. Check your card's memory yourself; drivers other than amdgpu do not report it in /sys.",
        );
    };
    let total = card.vram_total.unwrap_or(0);
    let free = total.saturating_sub(card.vram_used.unwrap_or(0));
    let (status, remedy) = if total < WANTED_VRAM / 2 {
        (
            "fail",
            "The guest's graphics need about 16 GiB of graphics memory; this card has much less.",
        )
    } else if total < ENOUGH_VRAM {
        (
            "warn",
            "Below 16 GiB the graphics memory guard may stop sessions. Close other graphics work.",
        )
    } else if free < total / 8 {
        (
            "warn",
            "Under an eighth of the card is free now, so the graphics memory guard would stop a session at once. Close other graphics work.",
        )
    } else {
        ("pass", "")
    };
    item(
        "graphics",
        status,
        found,
        format!(
            "The card at {} ({}) has {} of graphics memory, {} free now.",
            card.pci,
            card.driver,
            gib(total),
            gib(free)
        ),
        remedy,
    )
}

fn helper_kernel(facts: &Facts) -> Value {
    let found = json!({"running":facts.kernel,"support":facts.support,
        "apfs_module_for":facts.apfs_release,"apfs_vmlinuz":facts.apfs_vmlinuz});
    let remedy = "Run python3 tools/prepare_ios_emulator.py again (setup.md, chain step 2), or keep the matching vmlinuz beside the module as apfs/vmlinuz.";
    match (&facts.support, &facts.apfs_release) {
        (None, _) | (Some(_), None) => item(
            "helper_kernel",
            "warn",
            found,
            "The helper support with its APFS module is not prepared yet, so it cannot be compared with the running kernel.".into(),
            "Chain step 2 in setup.md prepares it.",
        ),
        (Some(_), Some(release)) if *release == facts.kernel => item(
            "helper_kernel",
            "pass",
            found,
            format!("The helper's APFS module was built for the running kernel {release}."),
            remedy,
        ),
        (Some(_), Some(release)) if facts.apfs_vmlinuz => item(
            "helper_kernel",
            "pass",
            found,
            format!(
                "The helper's APFS module was built for {release}, not the running {}, and its own vmlinuz is kept beside it.",
                facts.kernel
            ),
            remedy,
        ),
        (Some(_), Some(release)) => item(
            "helper_kernel",
            "fail",
            found,
            format!(
                "The helper's APFS module was built for kernel {release}, but {} is running and no apfs/vmlinuz is kept; preparing disks fails.",
                facts.kernel
            ),
            remedy,
        ),
    }
}

fn read_text(path: &Path) -> String {
    fs::read_to_string(path).unwrap_or_default()
}

fn meminfo(text: &str, key: &str) -> Option<u64> {
    text.lines()
        .find_map(|line| line.strip_prefix(key)?.strip_prefix(':'))
        .and_then(|value| value.split_whitespace().next()?.parse::<u64>().ok())
        .map(|kib| kib * 1024)
}

/// The kernel release a module was built for, from its `vermagic=` string.
pub(crate) fn module_release(bytes: &[u8]) -> Option<String> {
    let marker = b"vermagic=";
    let start = bytes
        .windows(marker.len())
        .position(|window| window == marker)?
        + marker.len();
    let rest = &bytes[start..];
    let end = rest
        .iter()
        .position(|byte| *byte == 0 || *byte == b' ')
        .unwrap_or(rest.len());
    let release = String::from_utf8_lossy(&rest[..end]).into_owned();
    (!release.is_empty()).then_some(release)
}

/// The cards under `sys`/class/drm, one per PCI device.
pub(crate) fn cards(sys: &Path) -> Vec<Card> {
    let mut found: Vec<Card> = Vec::new();
    let Ok(entries) = fs::read_dir(sys.join("class/drm")) else {
        return found;
    };
    let mut names: Vec<String> = entries
        .flatten()
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .filter(|name| {
            name.strip_prefix("card")
                .is_some_and(|rest| !rest.is_empty() && rest.chars().all(|c| c.is_ascii_digit()))
        })
        .collect();
    names.sort();
    for name in names {
        let device = sys.join("class/drm").join(&name).join("device");
        let pci = fs::canonicalize(&device)
            .ok()
            .and_then(|path| {
                path.file_name()
                    .map(|name| name.to_string_lossy().into_owned())
            })
            .unwrap_or(name);
        if found.iter().any(|card| card.pci == pci) {
            continue;
        }
        let number = |file: &str| read_text(&device.join(file)).trim().parse::<u64>().ok();
        found.push(Card {
            driver: fs::read_link(device.join("driver"))
                .ok()
                .and_then(|path| {
                    path.file_name()
                        .map(|name| name.to_string_lossy().into_owned())
                })
                .unwrap_or_default(),
            vram_total: number("mem_info_vram_total"),
            vram_used: number("mem_info_vram_used"),
            pci,
        });
    }
    found
}

/// The Vulkan drivers (ICD manifests) installed under `root`.
pub(crate) fn vulkan_drivers(root: &Path) -> Vec<String> {
    let mut drivers: Vec<String> = ["usr/share/vulkan/icd.d", "etc/vulkan/icd.d"]
        .iter()
        .filter_map(|directory| fs::read_dir(root.join(directory)).ok())
        .flatten()
        .flatten()
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .filter(|name| name.ends_with(".json"))
        .collect();
    drivers.sort();
    drivers.dedup();
    drivers
}

/// The helper support directory: the profile's, else the checkout's.
fn support_directory(profile: Option<&Value>, given: &str) -> Option<PathBuf> {
    if !given.is_empty() {
        return Some(PathBuf::from(given));
    }
    if let Some(support) = profile.and_then(|profile| profile["prepare"]["support"].as_str()) {
        return Some(PathBuf::from(support));
    }
    source_checkout().map(|root| root.join(".tooling/ios-sptm/support"))
}

/// What the next start of the configured profile needs on its `installs`,
/// from `start_disk` in `ios.virtual.status`: an overlay start copies only the
/// small disks, a copy (or a flattening) the whole root disk.
fn start_need(disk: &Value) -> (u64, String) {
    match (disk["method"].as_str(), disk["needed_bytes"].as_u64()) {
        (Some("overlay"), Some(needed)) => (needed, "one overlay start".to_string()),
        _ => (START_DISK, "one start's disk copy".to_string()),
    }
}

pub(crate) fn gather(a: &Value) -> Result<Facts> {
    let profile = virtual_phone::directory()
        .ok()
        .and_then(|directory| virtual_phone::records(&directory).ok())
        .map(|(profile, _)| profile);
    let (directory, needed, needed_for) = if !s(a, "directory").is_empty() {
        (
            path(s(a, "directory"), true, true)?,
            SETUP_DISK,
            "the setup chain".to_string(),
        )
    } else if let Some(installs) = profile.as_ref().and_then(|p| p["installs"].as_str()) {
        let (needed, needed_for) = start_need(&virtual_phone::next_start_disk());
        (PathBuf::from(installs), needed, needed_for)
    } else {
        (home()?, SETUP_DISK, "the setup chain".to_string())
    };
    let cpuinfo = read_text(Path::new("/proc/cpuinfo"));
    let cpu_flags = cpuinfo
        .lines()
        .find_map(|line| line.strip_prefix("flags"))
        .and_then(|rest| rest.split_once(':'))
        .map(|(_, flags)| flags.split_whitespace().map(str::to_string).collect())
        .unwrap_or_default();
    let meminfo_text = read_text(Path::new("/proc/meminfo"));
    let support = support_directory(profile.as_ref(), s(a, "support")).filter(|path| path.is_dir());
    let apfs = support.as_ref().map(|support| support.join("apfs"));
    Ok(Facts {
        architecture: std::env::consts::ARCH.into(),
        cpus: std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(0),
        cpu_flags,
        memory_total: meminfo(&meminfo_text, "MemTotal"),
        memory_available: meminfo(&meminfo_text, "MemAvailable"),
        kvm: fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open("/dev/kvm")
            .is_ok(),
        free: virtual_phone::free_bytes(&directory),
        directory,
        needed,
        needed_for,
        cards: cards(Path::new("/sys")),
        vulkan_drivers: vulkan_drivers(Path::new("/")),
        kernel: read_text(Path::new("/proc/sys/kernel/osrelease"))
            .trim()
            .to_string(),
        apfs_release: apfs
            .as_ref()
            .and_then(|apfs| fs::read(apfs.join("apfs.ko")).ok())
            .and_then(|bytes| module_release(&bytes)),
        apfs_vmlinuz: apfs
            .as_ref()
            .is_some_and(|apfs| apfs.join("vmlinuz").is_file()),
        support,
        passt: toolchain::which("passt"),
    })
}

/// The answer from a set of verdicts.
pub(crate) fn summary(items: Vec<Value>) -> Value {
    let count = |status: &str| items.iter().filter(|item| item["status"] == status).count();
    let (failed, warned) = (count("fail"), count("warn"));
    json!({
        "ready": failed == 0,
        "status": if failed > 0 { "fail" } else if warned > 0 { "warn" } else { "pass" },
        "failed": failed, "warned": warned,
        "checks": items,
        "guide": format!("{DOCS}/README.md#what-it-needs"),
        "note": "A pass means this machine meets what the virtual iOS device was measured with, not that it was qualified on it.",
    })
}

pub fn check(a: &Value) -> Result {
    Ok(summary(evaluate(&gather(a)?)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ready() -> Facts {
        Facts {
            architecture: "x86_64".into(),
            cpus: 32,
            cpu_flags: vec!["vmx".into(), "avx2".into()],
            memory_total: Some(64 * GIB),
            memory_available: Some(40 * GIB),
            kvm: true,
            directory: "/phone".into(),
            free: Some(400 * GIB),
            needed: SETUP_DISK,
            needed_for: "the setup chain".into(),
            cards: vec![Card {
                pci: "0000:03:00.0".into(),
                driver: "amdgpu".into(),
                vram_total: Some(16 * GIB),
                vram_used: Some(GIB),
            }],
            vulkan_drivers: vec!["radeon_icd.x86_64.json".into()],
            kernel: "6.17.1-arch1-1".into(),
            support: Some("/x/support".into()),
            apfs_release: Some("6.17.1-arch1-1".into()),
            apfs_vmlinuz: false,
            passt: Some("/usr/bin/passt".into()),
        }
    }

    fn status(facts: &Facts, name: &str) -> (String, Value) {
        let items = evaluate(facts);
        let found = items.iter().find(|item| item["check"] == name).unwrap();
        (found["status"].as_str().unwrap().to_string(), found.clone())
    }

    #[test]
    fn a_machine_like_the_measured_one_passes_everything() {
        let answer = summary(evaluate(&ready()));
        assert_eq!(answer["status"], "pass", "{answer:#}");
        assert_eq!(answer["ready"], true);
        let names: Vec<&str> = answer["checks"]
            .as_array()
            .unwrap()
            .iter()
            .map(|item| item["check"].as_str().unwrap())
            .collect();
        assert_eq!(
            names,
            [
                "architecture",
                "cpu",
                "kvm",
                "memory",
                "disk",
                "graphics",
                "helper_kernel",
                "passt"
            ]
        );
        assert!(
            answer["checks"]
                .as_array()
                .unwrap()
                .iter()
                .all(|item| item["remedy"].is_null())
        );
    }

    #[test]
    fn each_shortfall_names_its_remedy() {
        let mut facts = ready();
        facts.architecture = "aarch64".into();
        facts.memory_total = Some(8 * GIB);
        facts.free = Some(50 * GIB);
        facts.passt = None;
        facts.kvm = false;
        facts.cpu_flags.clear();
        let answer = summary(evaluate(&facts));
        assert_eq!(answer["ready"], false);
        assert_eq!(answer["failed"], 3);
        assert_eq!(status(&facts, "architecture").0, "fail");
        assert_eq!(status(&facts, "memory").0, "fail");
        let (disk, item) = status(&facts, "disk");
        assert_eq!(disk, "fail");
        assert!(
            item["message"]
                .as_str()
                .unwrap()
                .contains("the setup chain needs about 100.0 GiB")
        );
        let (passt, item) = status(&facts, "passt");
        assert_eq!(passt, "warn");
        assert!(item["remedy"].as_str().unwrap().contains("Install passt"));
        let (kvm, item) = status(&facts, "kvm");
        assert_eq!(kvm, "warn");
        assert!(
            item["remedy"]
                .as_str()
                .unwrap()
                .contains("firmware settings")
        );
        facts.cpu_flags = vec!["svm".into()];
        assert!(
            status(&facts, "kvm").1["remedy"]
                .as_str()
                .unwrap()
                .contains("kvm group")
        );
    }

    #[test]
    fn an_overlay_start_needs_only_its_small_disks() {
        let overlay = json!({"method":"overlay","needed_bytes":GIB + 33 * (1 << 20)});
        let (needed, what) = start_need(&overlay);
        assert_eq!(needed, GIB + 33 * (1 << 20));
        assert!(what.contains("overlay"));
        // A copy, a flattening or an unreadable base reckon with a whole copy.
        for disk in [
            json!({"method":"copy","needed_bytes":GIB}),
            json!({"method":"flatten","needed_bytes":GIB}),
            Value::Null,
        ] {
            assert_eq!(
                start_need(&disk),
                (START_DISK, "one start's disk copy".to_string())
            );
        }
        let mut facts = ready();
        facts.free = Some(20 * GIB);
        facts.needed = needed;
        facts.needed_for = what;
        assert_ne!(status(&facts, "disk").0, "fail");
    }

    #[test]
    fn memory_and_disk_have_a_warning_band() {
        let mut facts = ready();
        facts.memory_total = Some(14 * GIB);
        facts.free = Some(110 * GIB);
        facts.cpus = 4;
        assert_eq!(status(&facts, "memory").0, "warn");
        assert_eq!(status(&facts, "disk").0, "warn");
        assert_eq!(status(&facts, "cpu").0, "warn");
        facts.memory_total = None;
        facts.free = None;
        assert_eq!(status(&facts, "memory").0, "warn");
        assert_eq!(status(&facts, "disk").0, "warn");
    }

    #[test]
    fn graphics_needs_a_vulkan_driver_and_reads_the_largest_card() {
        let mut facts = ready();
        facts.cards.insert(
            0,
            Card {
                pci: "0000:00:02.0".into(),
                driver: "i915".into(),
                ..Card::default()
            },
        );
        let (graphics, item) = status(&facts, "graphics");
        assert_eq!(graphics, "pass");
        assert!(
            item["message"]
                .as_str()
                .unwrap()
                .contains("0000:03:00.0 (amdgpu) has 16.0 GiB")
        );
        // The measured "16 GiB" card reports 15.98 GiB.
        facts.cards[1].vram_total = Some(17163091968);
        assert_eq!(status(&facts, "graphics").0, "pass");
        facts.cards[1].vram_used = Some(15 * GIB);
        assert!(
            status(&facts, "graphics").1["remedy"]
                .as_str()
                .unwrap()
                .contains("an eighth")
        );
        facts.cards[1].vram_total = Some(12 * GIB);
        facts.cards[1].vram_used = Some(0);
        assert_eq!(status(&facts, "graphics").0, "warn");
        facts.cards[1].vram_total = Some(4 * GIB);
        assert_eq!(status(&facts, "graphics").0, "fail");
        facts.cards.remove(1);
        assert!(
            status(&facts, "graphics").1["message"]
                .as_str()
                .unwrap()
                .contains("size is unknown")
        );
        facts.vulkan_drivers.clear();
        assert_eq!(status(&facts, "graphics").0, "fail");
    }

    #[test]
    fn the_helper_kernel_must_match_the_running_one_unless_its_vmlinuz_is_kept() {
        let mut facts = ready();
        facts.apfs_release = Some("6.16.0-arch1-1".into());
        let (helper, item) = status(&facts, "helper_kernel");
        assert_eq!(helper, "fail");
        assert!(
            item["message"]
                .as_str()
                .unwrap()
                .contains("built for kernel 6.16.0-arch1-1, but 6.17.1-arch1-1 is running")
        );
        facts.apfs_vmlinuz = true;
        assert_eq!(status(&facts, "helper_kernel").0, "pass");
        facts.support = None;
        assert_eq!(status(&facts, "helper_kernel").0, "warn");
    }

    #[test]
    fn host_facts_are_read_from_proc_and_sys_shapes() {
        let root = tempfile::tempdir().unwrap();
        let sys = root.path().join("sys");
        let device = sys.join("devices/pci0000:00/0000:03:00.0");
        fs::create_dir_all(&device).unwrap();
        fs::create_dir_all(sys.join("bus/pci/drivers/amdgpu")).unwrap();
        std::os::unix::fs::symlink(sys.join("bus/pci/drivers/amdgpu"), device.join("driver"))
            .unwrap();
        fs::write(device.join("mem_info_vram_total"), "17163091968\n").unwrap();
        fs::write(device.join("mem_info_vram_used"), "1073741824\n").unwrap();
        fs::create_dir_all(sys.join("class/drm/card1")).unwrap();
        fs::create_dir_all(sys.join("class/drm/card1-DP-1")).unwrap();
        std::os::unix::fs::symlink(&device, sys.join("class/drm/card1/device")).unwrap();
        let found = cards(&sys);
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].pci, "0000:03:00.0");
        assert_eq!(found[0].driver, "amdgpu");
        assert_eq!(found[0].vram_total, Some(17163091968));
        fs::create_dir_all(root.path().join("usr/share/vulkan/icd.d")).unwrap();
        fs::write(
            root.path()
                .join("usr/share/vulkan/icd.d/radeon_icd.x86_64.json"),
            "{}",
        )
        .unwrap();
        assert_eq!(vulkan_drivers(root.path()), ["radeon_icd.x86_64.json"]);
        assert_eq!(
            meminfo(
                "MemTotal:       65536 kB\nMemAvailable:   1024 kB\n",
                "MemAvailable"
            ),
            Some(1024 * 1024)
        );
        let module = b"\0license=GPL\0vermagic=6.17.1-arch1-1 SMP preempt mod_unload \0";
        assert_eq!(module_release(module).as_deref(), Some("6.17.1-arch1-1"));
        assert_eq!(module_release(b"no marker"), None);
    }
}
