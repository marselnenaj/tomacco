//! What kind of Linux host this is, for the advice that differs between
//! hosts: the distribution and how it installs packages, whether KVM works
//! and why not, the graphics and display the Android emulator would use, and
//! whether memory, processors and disk suit the emulator and the virtual
//! iPhone. It reads /etc, /proc, /sys, /dev and the environment under a root
//! directory, so tests can hand it a made-up machine; it starts nothing and
//! writes nothing.
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

const GIB: u64 = 1 << 30;
/// The virtual iPhone's guest memory plus its largest translation cache and
/// room for the system: `ios.virtual.host_check` fails memory below this.
pub const VIRTUAL_IPHONE_MEMORY: u64 = 12 * GIB;
/// Free space below which the Android SDK, a system image and an emulator's
/// data partition (10 GB in the AVD's config.ini) may not fit.
pub const EMULATOR_DISK: u64 = 16 * GIB;

#[derive(Debug, Default, Clone, PartialEq)]
pub struct Gpu {
    /// nvidia, amd, intel, virtual (a virtual machine's display adapter) or
    /// other, from the PCI vendor and the kernel driver.
    pub vendor: String,
    pub driver: String,
}

#[derive(Debug, Default, Clone)]
pub struct Kvm {
    pub exists: bool,
    pub accessible: bool,
    /// The group that owns /dev/kvm, and whether that group gives access.
    pub group: Option<String>,
    pub group_writable: bool,
    /// Whether /etc/group lists the user in that group, and whether this
    /// process already has it (it does not until the next login).
    pub listed: bool,
    pub active: bool,
    /// Whether the group is in /etc/group at all: on Fedora's ostree
    /// systems it is only in /usr/lib/group, where usermod cannot add to it.
    pub group_in_etc: bool,
}

#[derive(Debug, Default, Clone)]
pub struct Facts {
    pub architecture: String,
    pub os_release: BTreeMap<String, String>,
    /// /run/ostree-booted: Fedora Silverblue, Kinoite, Bazzite and others.
    pub ostree: bool,
    /// toolbox, distrobox, podman or docker when running inside one.
    pub container: Option<String>,
    pub read_only_usr: bool,
    pub cpu_vendor: String,
    pub cpu_flags: Vec<String>,
    pub cpus: usize,
    pub memory_total: Option<u64>,
    pub disk_free: Option<u64>,
    pub kvm: Kvm,
    pub gpus: Vec<Gpu>,
    pub render_node: bool,
    pub nvidia_driver: bool,
    pub env: BTreeMap<String, String>,
}

/// The environment variables the advice depends on.
const ENVIRONMENT: [&str; 7] = [
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XDG_SESSION_TYPE",
    "QT_QPA_PLATFORM",
    "USER",
    "CONTAINER_ID",
    "TOOLBOX_PATH",
];

fn text(path: &Path) -> String {
    fs::read_to_string(path).unwrap_or_default()
}

pub fn os_release(content: &str) -> BTreeMap<String, String> {
    content
        .lines()
        .filter_map(|line| line.split_once('='))
        .map(|(key, value)| {
            (
                key.trim().to_string(),
                value
                    .trim()
                    .trim_matches(|c| c == '"' || c == '\'')
                    .to_string(),
            )
        })
        .collect()
}

fn group_line(groups: &str, gid: u32) -> Option<(&str, Vec<&str>)> {
    groups.lines().find_map(|line| {
        let mut fields = line.split(':');
        let name = fields.next()?;
        fields.next()?;
        (fields.next()?.parse::<u32>().ok()? == gid).then(|| {
            let members = fields
                .next()
                .unwrap_or("")
                .split(',')
                .filter(|m| !m.is_empty())
                .collect();
            (name, members)
        })
    })
}

fn process_has_group(gid: u32) -> bool {
    let mut groups = vec![0 as libc::gid_t; 256];
    let count = unsafe { libc::getgroups(groups.len() as libc::c_int, groups.as_mut_ptr()) };
    let effective = unsafe { libc::getegid() };
    effective == gid || (count > 0 && groups[..count as usize].contains(&gid))
}

fn read_only(path: &Path) -> bool {
    use std::os::unix::ffi::OsStrExt;
    let Ok(name) = std::ffi::CString::new(path.as_os_str().as_bytes()) else {
        return false;
    };
    let mut space: libc::statvfs = unsafe { std::mem::zeroed() };
    let measured = unsafe { libc::statvfs(name.as_ptr(), &mut space) } == 0;
    measured && space.f_flag & libc::ST_RDONLY != 0
}

pub fn free_bytes(path: &Path) -> Option<u64> {
    use std::os::unix::ffi::OsStrExt;
    let name = std::ffi::CString::new(path.as_os_str().as_bytes()).ok()?;
    let mut space: libc::statvfs = unsafe { std::mem::zeroed() };
    (unsafe { libc::statvfs(name.as_ptr(), &mut space) } == 0)
        .then(|| space.f_bavail as u64 * space.f_frsize as u64)
}

fn gpu_vendor(vendor: &str, driver: &str) -> &'static str {
    const VIRTUAL: [&str; 8] = [
        "virtio-pci",
        "virtio_gpu",
        "vmwgfx",
        "qxl",
        "bochs-drm",
        "bochs",
        "cirrus",
        "hyperv_drm",
    ];
    if VIRTUAL.contains(&driver) {
        return "virtual";
    }
    match vendor.trim().to_ascii_lowercase().as_str() {
        "0x10de" => "nvidia",
        "0x1002" => "amd",
        "0x8086" => "intel",
        "0x1af4" | "0x15ad" | "0x1b36" | "0x1234" | "0x1414" => "virtual",
        _ => "other",
    }
}

/// The graphics cards under `root`/sys/class/drm, one per card.
fn gpus(root: &Path) -> Vec<Gpu> {
    let mut found = Vec::new();
    let Ok(entries) = fs::read_dir(root.join("sys/class/drm")) else {
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
        let device = root.join("sys/class/drm").join(&name).join("device");
        let driver = fs::read_link(device.join("driver"))
            .ok()
            .and_then(|link| link.file_name().map(|n| n.to_string_lossy().into_owned()))
            .unwrap_or_default();
        let vendor = gpu_vendor(&text(&device.join("vendor")), &driver).to_string();
        found.push(Gpu { vendor, driver });
    }
    found
}

/// Reads the facts of the machine under `root` ("/" for this one), with the
/// environment from `env` and free space measured at `data`.
pub fn gather(root: &Path, env: impl Fn(&str) -> Option<String>, data: &Path) -> Facts {
    use std::os::unix::fs::MetadataExt;
    let release = {
        let etc = text(&root.join("etc/os-release"));
        if etc.is_empty() {
            os_release(&text(&root.join("usr/lib/os-release")))
        } else {
            os_release(&etc)
        }
    };
    let env: BTreeMap<String, String> = ENVIRONMENT
        .iter()
        .filter_map(|key| env(key).map(|value| (key.to_string(), value)))
        .collect();
    let container = if root.join("run/.toolboxenv").exists() {
        Some("toolbox")
    } else if env.contains_key("CONTAINER_ID") {
        Some("distrobox")
    } else if root.join("run/.containerenv").exists() {
        Some("podman")
    } else if root.join(".dockerenv").exists() {
        Some("docker")
    } else {
        None
    }
    .map(str::to_string);
    let cpuinfo = text(&root.join("proc/cpuinfo"));
    let field = |key: &str| {
        cpuinfo
            .lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                (name.trim() == key).then(|| value.trim().to_string())
            })
            .unwrap_or_default()
    };
    let meminfo = text(&root.join("proc/meminfo"));
    let memory_total = meminfo
        .lines()
        .find_map(|line| line.strip_prefix("MemTotal:"))
        .and_then(|value| value.split_whitespace().next()?.parse::<u64>().ok())
        .map(|kib| kib * 1024);
    let device = root.join("dev/kvm");
    let metadata = fs::metadata(&device).ok();
    let mut kvm = Kvm {
        exists: metadata.is_some(),
        accessible: fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(&device)
            .is_ok(),
        ..Kvm::default()
    };
    if let Some(metadata) = metadata {
        let gid = metadata.gid();
        kvm.group_writable = metadata.mode() & 0o060 == 0o060;
        kvm.active = process_has_group(gid);
        let etc = text(&root.join("etc/group"));
        let user = env.get("USER").cloned().unwrap_or_default();
        if let Some((name, members)) = group_line(&etc, gid) {
            kvm.group = Some(name.to_string());
            kvm.group_in_etc = true;
            kvm.listed = members.contains(&user.as_str());
        } else if let Some((name, _)) = group_line(&text(&root.join("usr/lib/group")), gid) {
            kvm.group = Some(name.to_string());
        }
    }
    let render_node = fs::read_dir(root.join("dev/dri")).is_ok_and(|entries| {
        entries
            .flatten()
            .any(|entry| entry.file_name().to_string_lossy().starts_with("renderD"))
    });
    Facts {
        architecture: std::env::consts::ARCH.to_string(),
        os_release: release,
        ostree: root.join("run/ostree-booted").exists(),
        container,
        read_only_usr: read_only(&root.join("usr")),
        cpu_vendor: field("vendor_id"),
        cpu_flags: field("flags")
            .split_whitespace()
            .map(str::to_string)
            .collect(),
        cpus: std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(0),
        memory_total,
        disk_free: free_bytes(data),
        kvm,
        gpus: gpus(root),
        render_node,
        nvidia_driver: root.join("proc/driver/nvidia/version").exists(),
        env,
    }
}

impl Facts {
    fn names(&self) -> Vec<String> {
        ["ID", "ID_LIKE"]
            .iter()
            .filter_map(|key| self.os_release.get(*key))
            .flat_map(|value| value.split_whitespace().map(str::to_ascii_lowercase))
            .collect()
    }

    fn id(&self) -> &str {
        self.os_release.get("ID").map_or("", String::as_str)
    }

    /// The major version, such as 12 for Debian 12 or 24 for Ubuntu 24.04.
    fn major(&self) -> Option<u64> {
        self.os_release
            .get("VERSION_ID")?
            .split('.')
            .next()?
            .parse()
            .ok()
    }

    /// arch, debian, fedora, suse, nixos or other, from ID and ID_LIKE.
    pub fn family(&self) -> &'static str {
        let names = self.names();
        let any = |wanted: &[&str]| names.iter().any(|name| wanted.contains(&name.as_str()));
        if any(&["nixos"]) {
            "nixos"
        } else if any(&["arch", "steamos"]) {
            "arch"
        } else if any(&["debian", "ubuntu"]) {
            "debian"
        } else if any(&["fedora", "rhel", "centos"]) {
            "fedora"
        } else if names.iter().any(|name| name.contains("suse")) {
            "suse"
        } else {
            "other"
        }
    }

    /// How this system keeps its own files read-only, if it does: steamos,
    /// ostree (Silverblue, Kinoite, Bazzite), nixos (no /usr/lib FHS), or
    /// read-only-usr for any other system whose /usr is mounted read-only.
    pub fn immutable(&self) -> Option<&'static str> {
        if self.id() == "steamos" {
            Some("steamos")
        } else if self.ostree {
            Some("ostree")
        } else if self.family() == "nixos" {
            Some("nixos")
        } else if self.read_only_usr && self.container.is_none() {
            Some("read-only-usr")
        } else {
            None
        }
    }

    fn virtualization(&self) -> bool {
        self.cpu_flags
            .iter()
            .any(|flag| flag == "vmx" || flag == "svm")
    }

    fn in_virtual_machine(&self) -> bool {
        self.cpu_flags.iter().any(|flag| flag == "hypervisor")
    }

    fn user(&self) -> &str {
        self.env.get("USER").map_or("$USER", String::as_str)
    }
}

/// The distribution, as doctor reports it.
pub fn distribution(facts: &Facts) -> Value {
    let get = |key: &str| facts.os_release.get(key).cloned().unwrap_or_default();
    json!({"id":get("ID"),"name":get("PRETTY_NAME"),"version":get("VERSION_ID"),
        "family":facts.family(),"immutable":facts.immutable(),"container":facts.container,
        "architecture":facts.architecture})
}

/// The command that installs a JDK 17 or newer here, as one line of advice.
pub fn jdk(facts: &Facts) -> String {
    let inside_toolbox = "Fedora's ostree systems install development tools in a toolbox: run `toolbox create` and `toolbox enter`, then";
    match (facts.immutable(), facts.family()) {
        (Some("steamos"), _) => "SteamOS's pacman cannot write to its read-only system. Unpack a JDK 21 (for example Eclipse Temurin from adoptium.net) into your home directory and name it with `tomaccoctl call settings.configure '{\"java_home\":\"/home/you/jdk-21\"}'`, or use distrobox".to_string(),
        (Some("nixos"), _) => "On NixOS: nix-shell -p jdk21, or add jdk21 to environment.systemPackages".to_string(),
        (Some("ostree"), _) => format!("{inside_toolbox} sudo dnf install {}", fedora_jdk(facts)),
        (_, "arch") => "On Arch: sudo pacman -S --needed jdk-openjdk".to_string(),
        (_, "debian") => {
            // Debian 13 has no OpenJDK 17; Debian 12 has no 21.
            let package = if facts.id() == "debian" && facts.major().is_some_and(|v| v >= 13)
                || facts.id() == "ubuntu" && facts.major().is_some_and(|v| v >= 24)
            {
                "openjdk-21-jdk-headless"
            } else {
                "openjdk-17-jdk-headless"
            };
            format!("sudo apt-get install {package}")
        }
        (_, "fedora") => format!("sudo dnf install {}", fedora_jdk(facts)),
        (_, "suse") => "sudo zypper install java-21-openjdk-devel".to_string(),
        _ => "Install a JDK 17 or newer from your distribution, or unpack one from adoptium.net and set java_home with settings.configure".to_string(),
    }
}

/// Fedora 44 has OpenJDK 25 and no 21; older releases have 21. A Fedora
/// derivative's own version number says nothing, so it gets 21.
fn fedora_jdk(facts: &Facts) -> &'static str {
    if facts.id() == "fedora" && facts.major().is_some_and(|v| v >= 44) {
        "java-25-openjdk-devel"
    } else {
        "java-21-openjdk-devel"
    }
}

/// Where Gradle 9 comes from here: only Arch's package is new enough for the
/// Android Gradle plugin 9 the starters use.
pub fn gradle(facts: &Facts) -> String {
    match (facts.immutable(), facts.family()) {
        (None, "arch") => "On Arch: sudo pacman -S --needed gradle".to_string(),
        _ => "Unpack Gradle 9 from https://gradle.org/releases into your home directory and name its bin/gradle with `tomaccoctl call settings.configure '{\"executables\":{\"gradle\":\"/home/you/gradle-9.3.1/bin/gradle\"}}'`; distribution packages are older than the Android Gradle plugin 9 needs".to_string(),
    }
}

/// Whether KVM works for the Android emulator, and if not, why and the
/// commands that fix it on this system.
pub fn kvm(facts: &Facts) -> Value {
    let k = &facts.kvm;
    let nested = facts.in_virtual_machine();
    let user = facts.user();
    let module = if facts.cpu_vendor == "AuthenticAMD" {
        "kvm_amd"
    } else {
        "kvm_intel"
    };
    let (status, message, commands): (&str, String, Vec<String>) = if k.accessible {
        let message = if nested {
            "/dev/kvm works. This is a virtual machine, so the emulator runs on nested virtualization, slower than on the host.".to_string()
        } else {
            "/dev/kvm works.".to_string()
        };
        ("ready", message, vec![])
    } else if !k.exists {
        match facts.container.as_deref() {
            Some(engine @ ("docker" | "podman")) => (
                "not_in_container",
                format!("/dev/kvm is not passed into this {engine} container."),
                vec![format!("{engine} run --device /dev/kvm ...")],
            ),
            _ if !facts.virtualization() && nested => (
                "nested_virtualization_off",
                "This is a virtual machine whose processor does not offer VT-x or AMD-V, so there is no /dev/kvm. Turn on nested virtualization for it on the host.".to_string(),
                vec![
                    "libvirt or virt-manager: give the machine the CPU mode host-passthrough".to_string(),
                    "VirtualBox: VBoxManage modifyvm NAME --nested-hw-virt on".to_string(),
                    "Hyper-V: Set-VMProcessor -VMName NAME -ExposeVirtualizationExtensions $true".to_string(),
                    format!("On a Linux host, check: cat /sys/module/{module}/parameters/nested"),
                ],
            ),
            _ if !facts.virtualization() => (
                "virtualization_off",
                "The processor does not report VT-x or AMD-V, so there is no /dev/kvm. It is usually switched off in the firmware settings.".to_string(),
                vec![
                    "Turn on Intel VT-x (Virtualization Technology) or AMD SVM in the firmware settings, then restart".to_string(),
                ],
            ),
            _ => (
                "module_not_loaded",
                format!("The processor supports virtualization, but /dev/kvm is missing: the {module} module is not loaded."),
                vec![format!("sudo modprobe {module}")],
            ),
        }
    } else {
        let group = k.group.clone().unwrap_or_else(|| "kvm".to_string());
        if !k.group_writable || group == "root" {
            (
                "no_group_access",
                format!("/dev/kvm exists, but its group {group} has no access to it."),
                vec![
                    "echo 'KERNEL==\"kvm\", GROUP=\"kvm\", MODE=\"0660\"' | sudo tee /etc/udev/rules.d/65-kvm.rules".to_string(),
                    "sudo udevadm control --reload && sudo udevadm trigger --name-match=kvm".to_string(),
                    format!("sudo usermod -aG kvm {user}"),
                    "Log out and back in".to_string(),
                ],
            )
        } else if k.listed && !k.active {
            (
                "log_in_again",
                format!("You are in the {group} group, but this session started before that."),
                vec![
                    "Log out and back in, or restart".to_string(),
                    format!("For one terminal only: newgrp {group}"),
                ],
            )
        } else {
            let mut commands = Vec::new();
            match facts.immutable() {
                Some("nixos") => commands.push(format!(
                    "Add users.users.{user}.extraGroups = [ \"{group}\" ]; to /etc/nixos/configuration.nix, then: sudo nixos-rebuild switch"
                )),
                immutable => {
                    if immutable == Some("steamos") {
                        commands.push("SteamOS's deck user has no password at first: set one with passwd".to_string());
                    }
                    if !k.group_in_etc {
                        commands.push(format!(
                            "grep -E '^{group}:' /usr/lib/group | sudo tee -a /etc/group"
                        ));
                    }
                    commands.push(format!("sudo usermod -aG {group} {user}"));
                }
            }
            commands.push("Log out and back in".to_string());
            (
                "not_in_group",
                format!("/dev/kvm belongs to the {group} group, and you are not in it."),
                commands,
            )
        }
    };
    json!({"ready":status == "ready","status":status,"message":message,"commands":commands,
        "virtual_machine":nested,"exists":k.exists,"group":k.group})
}

/// Whether the emulator's own window can open here. Its bundled Qt has only
/// the X11 (xcb) platform plugin, so it needs DISPLAY: XWayland on Wayland.
/// Ok(Some(value)) is the QT_QPA_PLATFORM the emulator must get instead of
/// the session's, which may name only wayland and so abort it.
pub fn emulator_window(env: &BTreeMap<String, String>) -> Result<Option<String>, (String, String)> {
    let set = |key: &str| env.get(key).is_some_and(|value| !value.is_empty());
    if !set("DISPLAY") {
        return Err(if set("WAYLAND_DISPLAY") {
            (
                "This Wayland session has no XWayland (DISPLAY is not set), and the Android emulator's window needs X11.".to_string(),
                "Turn on XWayland in your compositor, or start the emulator with headless true and watch it in Tomacco's live view.".to_string(),
            )
        } else {
            (
                "There is no graphical session here (neither DISPLAY nor WAYLAND_DISPLAY is set), so the emulator's window cannot open.".to_string(),
                "Start the emulator with headless true; device.capture and the live view still work.".to_string(),
            )
        });
    }
    let platform = env.get("QT_QPA_PLATFORM").map_or("", String::as_str);
    Ok(
        (!platform.is_empty() && !platform.split(';').any(|p| p.trim() == "xcb"))
            .then(|| "xcb".to_string()),
    )
}

/// The emulator renderer that suits this machine's graphics. `auto` lets the
/// emulator test the host's OpenGL and fall back by itself, as it did on the
/// AMD host Tomacco was tested on; without a hardware GPU driver, software
/// rendering is chosen outright.
pub fn emulator_gpu(facts: &Facts) -> Value {
    let hardware: Vec<&Gpu> = facts
        .gpus
        .iter()
        .filter(|gpu| gpu.vendor != "virtual")
        .collect();
    let mut vendors: Vec<&str> = facts.gpus.iter().map(|gpu| gpu.vendor.as_str()).collect();
    vendors.dedup();
    let session = match (
        facts.env.contains_key("WAYLAND_DISPLAY"),
        facts.env.contains_key("DISPLAY"),
    ) {
        (true, _) => "wayland",
        (false, true) => "x11",
        _ => "none",
    };
    let (recommended, reason) = if hardware.is_empty() && !facts.nvidia_driver {
        (
            "swiftshader",
            if facts.gpus.is_empty() {
                "No graphics card was found under /sys/class/drm, so the emulator renders in software.".to_string()
            } else {
                "Only a virtual machine's display adapter was found, so the emulator renders in software.".to_string()
            },
        )
    } else if !facts.render_node && !facts.nvidia_driver {
        (
            "swiftshader",
            "The graphics card has no render node in /dev/dri, so its driver cannot be used for OpenGL; the emulator renders in software.".to_string(),
        )
    } else {
        ("auto", {
            let mut names: Vec<&str> = hardware
                .iter()
                .map(|gpu| match gpu.vendor.as_str() {
                    "nvidia" => "NVIDIA",
                    "amd" => "AMD",
                    "intel" => "Intel",
                    _ => "other",
                })
                .collect();
            if names.is_empty() {
                names.push("NVIDIA");
            }
            names.dedup();
            format!(
                "Graphics found: {}. The emulator's auto mode uses the card when its OpenGL works and falls back to software rendering otherwise.",
                names.join(", ")
            )
        })
    };
    let mut notes = Vec::new();
    if hardware.iter().any(|gpu| gpu.driver == "nouveau") {
        notes.push("The NVIDIA card uses the nouveau driver. If the emulator's screen stays black or it stops, use gpu swiftshader, or install NVIDIA's driver.");
    }
    if session == "wayland" {
        notes.push("On Wayland the emulator's window is an X11 window through XWayland. If it stays black, try gpu swiftshader.");
    }
    let window = match emulator_window(&facts.env) {
        Ok(_) => "ok",
        Err(_) if session == "wayland" => "no_xwayland",
        Err(_) => "no_display",
    };
    json!({"recommended":recommended,"reason":reason,"vendors":vendors,
        "drivers":facts.gpus.iter().map(|gpu| gpu.driver.as_str()).collect::<Vec<_>>(),
        "render_node":facts.render_node,"session":session,"window":window,
        "qt_platform_override":emulator_window(&facts.env).ok().flatten(),"notes":notes,
        "override":"android.avd_start gpu, or android.avd_configure gpu to keep it"})
}

/// The largest emulator memory (MB) and processor count that suit this
/// host, or None where Tomacco's defaults already fit.
pub fn emulator_limits(facts: &Facts) -> (Option<u64>, Option<u64>) {
    let memory = facts.memory_total.and_then(|total| {
        if total < 6 * GIB {
            Some(1536)
        } else if total < 10 * GIB {
            Some(2048)
        } else {
            None
        }
    });
    let cores = (facts.cpus > 0 && facts.cpus <= 4).then(|| (facts.cpus as u64 / 2).max(1));
    (memory, cores)
}

/// Why the virtual iPhone cannot start on this host, if it cannot.
pub fn virtual_iphone_refusal(facts: &Facts) -> Option<String> {
    if facts.architecture != "x86_64" {
        return Some(format!(
            "The virtual iPhone needs an x86_64 host; this one is {}.",
            facts.architecture
        ));
    }
    let total = facts.memory_total?;
    (total < VIRTUAL_IPHONE_MEMORY).then(|| {
        format!(
            "This computer has {:.1} GiB of memory; the virtual iPhone's guest takes 8 GiB and its translated code up to 2 GiB more, so it needs at least 12 GiB.",
            total as f64 / GIB as f64
        )
    })
}

/// Memory, processors and disk against what the emulator and the virtual
/// iPhone need.
pub fn resources(facts: &Facts) -> Value {
    let (memory, cores) = emulator_limits(facts);
    let mut emulator_notes = Vec::new();
    if let Some(mb) = memory {
        emulator_notes.push(format!(
            "New emulators get {mb} MB of memory, less than usual, to leave room for the system."
        ));
    }
    if let Some(cores) = cores {
        emulator_notes.push(format!(
            "New emulators get {cores} processor(s) of the {} here.",
            facts.cpus
        ));
    }
    if facts.memory_total.is_some_and(|total| total < 4 * GIB) {
        emulator_notes.push("Under 4 GiB of memory the emulator and a Gradle build hardly fit together; build on another machine or use a phone.".to_string());
    }
    if facts.disk_free.is_some_and(|free| free < EMULATOR_DISK) {
        emulator_notes.push(format!(
            "Only {:.1} GiB is free in the home directory: the Android SDK download is about 5 GB and an emulator's data partition may grow to 10 GB.",
            facts.disk_free.unwrap_or(0) as f64 / GIB as f64
        ));
    }
    json!({"memory_total_bytes":facts.memory_total,"cpus":facts.cpus,"disk_free_bytes":facts.disk_free,
        "emulator":{"memory_mb":memory,"cores":cores,"notes":emulator_notes},
        "virtual_iphone":{"fits":virtual_iphone_refusal(facts).is_none(),"reason":virtual_iphone_refusal(facts)}})
}

/// Everything above, as doctor's `host`.
pub fn assess(facts: &Facts) -> Value {
    json!({"distribution":distribution(facts),"kvm":kvm(facts),"emulator_gpu":emulator_gpu(facts),
        "resources":resources(facts),"install":{"jdk":jdk(facts),"gradle":gradle(facts)}})
}

/// This machine's facts, with free space measured in the home directory,
/// where the Android SDK goes by default.
pub fn current() -> Facts {
    let home = std::env::var_os("HOME").map_or_else(|| "/".into(), std::path::PathBuf::from);
    gather(Path::new("/"), |key| std::env::var(key).ok(), &home)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn machine(os: &str) -> Facts {
        Facts {
            architecture: "x86_64".into(),
            os_release: os_release(os),
            cpu_vendor: "GenuineIntel".into(),
            cpu_flags: vec!["vmx".into(), "avx2".into()],
            cpus: 16,
            memory_total: Some(32 * GIB),
            disk_free: Some(200 * GIB),
            kvm: Kvm {
                exists: true,
                accessible: true,
                group: Some("kvm".into()),
                group_writable: true,
                listed: true,
                active: true,
                group_in_etc: true,
            },
            gpus: vec![Gpu {
                vendor: "amd".into(),
                driver: "amdgpu".into(),
            }],
            render_node: true,
            env: [("DISPLAY", ":0"), ("USER", "ada")]
                .into_iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect(),
            ..Facts::default()
        }
    }

    const DEBIAN_12: &str =
        "PRETTY_NAME=\"Debian GNU/Linux 12 (bookworm)\"\nID=debian\nVERSION_ID=\"12\"\n";
    const DEBIAN_13: &str = "ID=debian\nVERSION_ID=\"13\"\n";
    const UBUNTU_24: &str = "ID=ubuntu\nID_LIKE=debian\nVERSION_ID=\"24.04\"\n";
    const UBUNTU_22: &str = "ID=ubuntu\nID_LIKE=debian\nVERSION_ID=\"22.04\"\n";
    const MINT: &str = "ID=linuxmint\nID_LIKE=\"ubuntu debian\"\nVERSION_ID=\"22\"\n";
    const FEDORA_44: &str = "ID=fedora\nVERSION_ID=44\n";
    const FEDORA_42: &str = "ID=fedora\nVERSION_ID=42\n";
    const BAZZITE: &str = "ID=bazzite\nID_LIKE=\"fedora\"\nVERSION_ID=\"44\"\n";
    const TUMBLEWEED: &str = "ID=\"opensuse-tumbleweed\"\nID_LIKE=\"opensuse suse\"\n";
    const STEAMOS: &str = "ID=steamos\nID_LIKE=arch\nVARIANT_ID=steamdeck\n";
    const NIXOS: &str = "ID=nixos\nVERSION_ID=\"25.05\"\n";
    const ARCH: &str = "ID=arch\n";

    #[test]
    fn families_and_immutable_systems_come_from_os_release() {
        for (os, family) in [
            (DEBIAN_12, "debian"),
            (UBUNTU_24, "debian"),
            (MINT, "debian"),
            (FEDORA_44, "fedora"),
            (BAZZITE, "fedora"),
            (TUMBLEWEED, "suse"),
            (STEAMOS, "arch"),
            (ARCH, "arch"),
            (NIXOS, "nixos"),
            ("ID=gentoo\n", "other"),
        ] {
            assert_eq!(machine(os).family(), family, "{os}");
        }
        assert_eq!(machine(STEAMOS).immutable(), Some("steamos"));
        assert_eq!(machine(NIXOS).immutable(), Some("nixos"));
        let mut bazzite = machine(BAZZITE);
        assert_eq!(bazzite.immutable(), None);
        bazzite.ostree = true;
        assert_eq!(bazzite.immutable(), Some("ostree"));
        let mut micro = machine(TUMBLEWEED);
        micro.read_only_usr = true;
        assert_eq!(micro.immutable(), Some("read-only-usr"));
        // A container's read-only /usr is the container's business.
        micro.container = Some("docker".into());
        assert_eq!(micro.immutable(), None);
    }

    #[test]
    fn the_jdk_command_names_a_package_this_release_has() {
        let jdk_of = |os: &str| jdk(&machine(os));
        assert_eq!(
            jdk_of(DEBIAN_12),
            "sudo apt-get install openjdk-17-jdk-headless"
        );
        assert_eq!(
            jdk_of(DEBIAN_13),
            "sudo apt-get install openjdk-21-jdk-headless"
        );
        assert_eq!(
            jdk_of(UBUNTU_24),
            "sudo apt-get install openjdk-21-jdk-headless"
        );
        assert_eq!(
            jdk_of(UBUNTU_22),
            "sudo apt-get install openjdk-17-jdk-headless"
        );
        assert_eq!(jdk_of(MINT), "sudo apt-get install openjdk-17-jdk-headless");
        assert_eq!(jdk_of(FEDORA_44), "sudo dnf install java-25-openjdk-devel");
        assert_eq!(jdk_of(FEDORA_42), "sudo dnf install java-21-openjdk-devel");
        assert_eq!(
            jdk_of(TUMBLEWEED),
            "sudo zypper install java-21-openjdk-devel"
        );
        assert_eq!(jdk_of(ARCH), "On Arch: sudo pacman -S --needed jdk-openjdk");
        assert!(jdk_of(STEAMOS).contains("read-only"));
        assert!(jdk_of(STEAMOS).contains("java_home"));
        assert!(jdk_of(NIXOS).contains("nix-shell -p jdk21"));
        let mut silverblue = machine(FEDORA_44);
        silverblue.ostree = true;
        assert!(jdk(&silverblue).contains("toolbox enter"));
        assert!(jdk(&silverblue).ends_with("sudo dnf install java-25-openjdk-devel"));
        assert!(gradle(&machine(ARCH)).contains("pacman"));
        assert!(gradle(&machine(DEBIAN_12)).contains("gradle.org"));
        assert!(gradle(&machine(STEAMOS)).contains("gradle.org"));
    }

    fn kvm_status(facts: &Facts) -> (String, Vec<String>) {
        let value = kvm(facts);
        (
            value["status"].as_str().unwrap().to_string(),
            value["commands"]
                .as_array()
                .unwrap()
                .iter()
                .map(|c| c.as_str().unwrap().to_string())
                .collect(),
        )
    }

    #[test]
    fn kvm_says_why_it_is_not_usable_and_what_fixes_it() {
        let ready = machine(DEBIAN_12);
        assert_eq!(kvm_status(&ready).0, "ready");
        assert_eq!(kvm(&ready)["ready"], true);

        let mut nested = machine(DEBIAN_12);
        nested.cpu_flags.push("hypervisor".into());
        assert_eq!(kvm(&nested)["virtual_machine"], true);
        assert!(kvm(&nested)["message"].as_str().unwrap().contains("nested"));

        let mut missing = machine(DEBIAN_12);
        missing.kvm = Kvm::default();
        assert_eq!(
            kvm_status(&missing),
            (
                "module_not_loaded".into(),
                vec!["sudo modprobe kvm_intel".into()]
            )
        );
        missing.cpu_vendor = "AuthenticAMD".into();
        assert_eq!(kvm_status(&missing).1, ["sudo modprobe kvm_amd"]);

        missing.cpu_flags = vec!["avx2".into()];
        assert_eq!(kvm_status(&missing).0, "virtualization_off");
        missing.cpu_flags.push("hypervisor".into());
        let (status, commands) = kvm_status(&missing);
        assert_eq!(status, "nested_virtualization_off");
        assert!(commands.iter().any(|c| c.contains("host-passthrough")));
        assert!(
            commands
                .iter()
                .any(|c| c.contains("kvm_amd/parameters/nested"))
        );

        missing.container = Some("docker".into());
        assert_eq!(
            kvm_status(&missing),
            (
                "not_in_container".into(),
                vec!["docker run --device /dev/kvm ...".into()]
            )
        );

        let mut outside = machine(UBUNTU_24);
        outside.kvm.accessible = false;
        outside.kvm.listed = false;
        outside.kvm.active = false;
        assert_eq!(
            kvm_status(&outside),
            (
                "not_in_group".into(),
                vec![
                    "sudo usermod -aG kvm ada".into(),
                    "Log out and back in".into()
                ]
            )
        );
        outside.kvm.listed = true;
        let (status, commands) = kvm_status(&outside);
        assert_eq!(status, "log_in_again");
        assert!(commands.contains(&"For one terminal only: newgrp kvm".to_string()));

        let mut silverblue = machine(FEDORA_44);
        silverblue.ostree = true;
        silverblue.kvm.accessible = false;
        silverblue.kvm.listed = false;
        silverblue.kvm.group_in_etc = false;
        let (_, commands) = kvm_status(&silverblue);
        assert_eq!(
            commands[0],
            "grep -E '^kvm:' /usr/lib/group | sudo tee -a /etc/group"
        );
        assert_eq!(commands[1], "sudo usermod -aG kvm ada");

        let mut nixos = machine(NIXOS);
        nixos.kvm.accessible = false;
        nixos.kvm.listed = false;
        let (_, commands) = kvm_status(&nixos);
        assert!(commands[0].contains("users.users.ada.extraGroups = [ \"kvm\" ]"));
        assert!(commands[0].contains("nixos-rebuild switch"));

        let mut deck = machine(STEAMOS);
        deck.kvm.accessible = false;
        deck.kvm.listed = false;
        let (_, commands) = kvm_status(&deck);
        assert!(commands[0].contains("passwd"));
        assert_eq!(commands[1], "sudo usermod -aG kvm ada");

        let mut root_only = machine(ARCH);
        root_only.kvm.accessible = false;
        root_only.kvm.group = Some("root".into());
        root_only.kvm.group_writable = false;
        let (status, commands) = kvm_status(&root_only);
        assert_eq!(status, "no_group_access");
        assert!(commands[0].contains("65-kvm.rules"));
    }

    fn env(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    #[test]
    fn the_emulator_window_needs_x11_and_the_xcb_plugin() {
        assert_eq!(emulator_window(&env(&[("DISPLAY", ":0")])), Ok(None));
        assert_eq!(
            emulator_window(&env(&[
                ("DISPLAY", ":0"),
                ("QT_QPA_PLATFORM", "wayland;xcb")
            ])),
            Ok(None)
        );
        assert_eq!(
            emulator_window(&env(&[("DISPLAY", ":0"), ("QT_QPA_PLATFORM", "wayland")])),
            Ok(Some("xcb".into()))
        );
        let (message, remedy) =
            emulator_window(&env(&[("WAYLAND_DISPLAY", "wayland-1")])).unwrap_err();
        assert!(message.contains("XWayland"));
        assert!(remedy.contains("headless"));
        let (message, remedy) = emulator_window(&env(&[])).unwrap_err();
        assert!(message.contains("no graphical session"));
        assert!(remedy.contains("headless true"));
    }

    #[test]
    fn the_renderer_follows_the_graphics_found() {
        let amd = machine(ARCH);
        let gpu = emulator_gpu(&amd);
        assert_eq!(gpu["recommended"], "auto");
        assert_eq!(gpu["window"], "ok");
        assert!(gpu["reason"].as_str().unwrap().contains("AMD"));

        let mut none = machine(DEBIAN_12);
        none.gpus.clear();
        none.render_node = false;
        none.env = env(&[]);
        let gpu = emulator_gpu(&none);
        assert_eq!(gpu["recommended"], "swiftshader");
        assert_eq!(gpu["window"], "no_display");
        assert_eq!(gpu["session"], "none");

        let mut vm = machine(FEDORA_44);
        vm.gpus = vec![Gpu {
            vendor: "virtual".into(),
            driver: "virtio-pci".into(),
        }];
        assert_eq!(emulator_gpu(&vm)["recommended"], "swiftshader");
        assert!(
            emulator_gpu(&vm)["reason"]
                .as_str()
                .unwrap()
                .contains("virtual machine")
        );

        let mut no_render = machine(ARCH);
        no_render.render_node = false;
        assert_eq!(emulator_gpu(&no_render)["recommended"], "swiftshader");

        // NVIDIA's own driver renders without a render node of its own.
        let mut nvidia = machine(UBUNTU_24);
        nvidia.gpus = vec![Gpu {
            vendor: "nvidia".into(),
            driver: "nvidia".into(),
        }];
        nvidia.render_node = false;
        nvidia.nvidia_driver = true;
        nvidia.env = env(&[
            ("WAYLAND_DISPLAY", "wayland-0"),
            ("DISPLAY", ":1"),
            ("QT_QPA_PLATFORM", "wayland"),
        ]);
        let gpu = emulator_gpu(&nvidia);
        assert_eq!(gpu["recommended"], "auto");
        assert_eq!(gpu["session"], "wayland");
        assert_eq!(gpu["qt_platform_override"], "xcb");
        assert!(gpu["reason"].as_str().unwrap().contains("NVIDIA"));

        let mut nouveau = machine(DEBIAN_12);
        nouveau.gpus = vec![Gpu {
            vendor: "nvidia".into(),
            driver: "nouveau".into(),
        }];
        let notes = emulator_gpu(&nouveau)["notes"].to_string();
        assert!(notes.contains("nouveau"));

        let mut wayland_only = machine(ARCH);
        wayland_only.env = env(&[("WAYLAND_DISPLAY", "wayland-1")]);
        assert_eq!(emulator_gpu(&wayland_only)["window"], "no_xwayland");
    }

    #[test]
    fn small_hosts_get_smaller_emulators_and_no_virtual_iphone() {
        let big = machine(ARCH);
        assert_eq!(emulator_limits(&big), (None, None));
        assert_eq!(virtual_iphone_refusal(&big), None);
        assert_eq!(resources(&big)["virtual_iphone"]["fits"], true);

        let mut laptop = machine(UBUNTU_24);
        laptop.memory_total = Some(8 * GIB);
        laptop.cpus = 4;
        assert_eq!(emulator_limits(&laptop), (Some(2048), Some(2)));
        let reason = virtual_iphone_refusal(&laptop).unwrap();
        assert!(reason.contains("8.0 GiB"), "{reason}");
        assert!(reason.contains("12 GiB"));
        let value = resources(&laptop);
        assert_eq!(value["emulator"]["memory_mb"], 2048);
        assert_eq!(value["virtual_iphone"]["fits"], false);

        let mut tiny = machine(DEBIAN_12);
        tiny.memory_total = Some(3 * GIB);
        tiny.cpus = 2;
        tiny.disk_free = Some(9 * GIB);
        assert_eq!(emulator_limits(&tiny), (Some(1536), Some(1)));
        let notes = resources(&tiny)["emulator"]["notes"].to_string();
        assert!(notes.contains("Under 4 GiB"));
        assert!(notes.contains("9.0 GiB is free"));

        let mut arm = machine(ARCH);
        arm.architecture = "aarch64".into();
        assert!(virtual_iphone_refusal(&arm).unwrap().contains("x86_64"));
        // 16 GiB sticks report a little less; that still fits.
        let mut sixteen = machine(ARCH);
        sixteen.memory_total = Some(15 * GIB + 600 * (1 << 20));
        assert_eq!(virtual_iphone_refusal(&sixteen), None);
    }

    #[test]
    fn gather_reads_a_machine_under_a_root() {
        use std::os::unix::fs::{PermissionsExt, symlink};
        let root = tempfile::tempdir().unwrap();
        let r = root.path();
        let write = |path: &str, content: &str| {
            let file = r.join(path);
            fs::create_dir_all(file.parent().unwrap()).unwrap();
            fs::write(file, content).unwrap();
        };
        write("usr/lib/os-release", BAZZITE);
        write("run/ostree-booted", "");
        write(
            "proc/cpuinfo",
            "processor\t: 0\nvendor_id\t: AuthenticAMD\nflags\t\t: fpu svm hypervisor avx2\n",
        );
        write(
            "proc/meminfo",
            "MemTotal:       16303428 kB\nMemFree: 1 kB\n",
        );
        write("sys/class/drm/card1/device/vendor", "0x1002\n");
        write("sys/class/drm/card1-DP-1/status", "connected\n");
        write("sys/bus/pci/drivers/amdgpu/.keep", "");
        symlink(
            r.join("sys/bus/pci/drivers/amdgpu"),
            r.join("sys/class/drm/card1/device/driver"),
        )
        .unwrap();
        write("dev/dri/renderD128", "");
        write("dev/kvm", "");
        fs::set_permissions(r.join("dev/kvm"), fs::Permissions::from_mode(0o660)).unwrap();
        let gid = unsafe { libc::getegid() };
        write("usr/lib/group", &format!("kvm:x:{gid}:\n"));
        let facts = gather(
            r,
            |key| match key {
                "WAYLAND_DISPLAY" => Some("wayland-0".into()),
                "USER" => Some("ada".into()),
                _ => None,
            },
            r,
        );
        assert_eq!(facts.family(), "fedora");
        assert_eq!(facts.immutable(), Some("ostree"));
        assert_eq!(facts.cpu_vendor, "AuthenticAMD");
        assert!(facts.virtualization() && facts.in_virtual_machine());
        assert_eq!(facts.memory_total, Some(16303428 * 1024));
        assert_eq!(
            facts.gpus,
            [Gpu {
                vendor: "amd".into(),
                driver: "amdgpu".into()
            }]
        );
        assert!(facts.render_node);
        assert!(facts.kvm.exists && facts.kvm.accessible && facts.kvm.group_writable);
        assert_eq!(facts.kvm.group.as_deref(), Some("kvm"));
        assert!(!facts.kvm.group_in_etc);
        assert!(facts.kvm.active);
        assert!(facts.disk_free.is_some());
        assert_eq!(
            facts.env.get("WAYLAND_DISPLAY").map(String::as_str),
            Some("wayland-0")
        );
        assert_eq!(emulator_gpu(&facts)["window"], "no_xwayland");
        let assessed = assess(&facts);
        assert_eq!(assessed["distribution"]["immutable"], "ostree");
        assert!(
            assessed["install"]["jdk"]
                .as_str()
                .unwrap()
                .contains("toolbox")
        );

        // An empty machine: nothing found, nothing assumed.
        let empty = tempfile::tempdir().unwrap();
        let facts = gather(empty.path(), |_| None, empty.path());
        assert!(!facts.kvm.exists && facts.gpus.is_empty() && facts.memory_total.is_none());
        assert_eq!(facts.family(), "other");
        assert_eq!(kvm(&facts)["status"], "virtualization_off");
    }
}
