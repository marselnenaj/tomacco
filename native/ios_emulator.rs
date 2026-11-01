//! Direct Linux iOS restore-shell execution and separate kernel research probes.
pub mod control;
pub mod crash_reports;
mod disk_overlay;
pub mod guest_control;
pub mod guest_tools;
mod metrics;
pub mod paravirtual;
pub mod pmp;
mod pmp_debug;
pub mod research;
pub mod restore;
pub mod syslog;
pub mod usb_bridge;
pub mod userland;
pub mod virtual_phone;
pub mod virtual_screen;
use crate::{core::*, jobs, toolchain};
use base64::{Engine as _, engine::general_purpose::STANDARD};
use serde_json::{Value, json};
use std::{
    fs::{self, File, OpenOptions},
    io::{BufRead, BufReader, Read, Write},
    os::unix::{
        fs::{MetadataExt, PermissionsExt},
        net::UnixStream,
        process::CommandExt,
    },
    path::{Component, Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

const LOG_LIMIT: usize = 8 * 1024 * 1024;
const COMPONENTS: &[(&str, &str, bool)] = &[
    ("kernel", "KernelCache", true),
    ("device_tree", "DeviceTree", true),
    ("ramdisk", "RestoreRamDisk", true),
    ("trustcache", "RestoreTrustCache", true),
    ("sptm", "Ap,SecurePageTableMonitor", false),
    ("txm", "Ap,TrustedExecutionMonitor", false),
    ("sep", "SEP", false),
];

fn plist_value(node: roxmltree::Node<'_, '_>) -> Result<Value> {
    plist_value_at(node, 0)
}
fn plist_value_at(node: roxmltree::Node<'_, '_>, depth: usize) -> Result<Value> {
    if depth > 64 {
        return Err(Error::new(
            "invalid_firmware",
            "Manifest nesting exceeds 64 levels",
        ));
    }
    Ok(match node.tag_name().name() {
        "dict" => {
            let mut result = serde_json::Map::new();
            let mut children = node.children().filter(|n| n.is_element());
            while let Some(key) = children.next() {
                if !key.has_tag_name("key") {
                    return Err(Error::new("invalid_firmware", "Malformed plist dictionary"));
                }
                let value = children
                    .next()
                    .ok_or_else(|| Error::new("invalid_firmware", "Missing plist value"))?;
                if result
                    .insert(
                        key.text().unwrap_or_default().to_string(),
                        plist_value_at(value, depth + 1)?,
                    )
                    .is_some()
                {
                    return Err(Error::new("invalid_firmware", "Duplicate manifest key"));
                }
            }
            Value::Object(result)
        }
        "array" => Value::Array(
            node.children()
                .filter(|n| n.is_element())
                .map(|value| plist_value_at(value, depth + 1))
                .collect::<Result<Vec<_>>>()?,
        ),
        "true" => json!(true),
        "false" => json!(false),
        _ => json!(node.text().unwrap_or_default()),
    })
}

fn firmware(root: &Path, variant: &str, board: &str) -> Result<Value> {
    let manifest = root.join("BuildManifest.plist");
    let mut bytes = Vec::new();
    File::open(&manifest)?
        .take(16 * 1024 * 1024 + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() > 16 * 1024 * 1024 {
        return Err(Error::new(
            "invalid_firmware",
            "BuildManifest exceeds 16 MiB",
        ));
    }
    let text = std::str::from_utf8(&bytes)
        .map_err(|_| Error::new("invalid_firmware", "An XML BuildManifest.plist is required"))?;
    let document = roxmltree::Document::parse_with_options(
        text,
        roxmltree::ParsingOptions {
            allow_dtd: true,
            ..Default::default()
        },
    )
    .map_err(|e| Error::new("invalid_firmware", e.to_string()))?;
    let dictionary = document
        .root_element()
        .children()
        .find(|n| n.has_tag_name("dict"))
        .ok_or_else(|| Error::new("invalid_firmware", "Missing manifest dictionary"))?;
    let manifest = plist_value(dictionary)?;
    let wanted = if variant == "research" {
        "Research Customer Erase Install (IPSW)"
    } else {
        "Customer Erase Install (IPSW)"
    };
    let identity = manifest["BuildIdentities"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|i| s(&i["Info"], "DeviceClass") == board && s(&i["Info"], "Variant") == wanted)
        .ok_or_else(|| {
            Error::new(
                "unsupported_firmware",
                "No matching board/variant erase identity in the firmware",
            )
        })?;
    let mut components = serde_json::Map::new();
    let mut missing = Vec::new();
    for &(name, key, required) in COMPONENTS {
        let relative = s(&identity["Manifest"][key]["Info"], "Path");
        let mut local = Value::Null;
        if !relative.is_empty() {
            let relative_path = Path::new(relative);
            if relative_path
                .components()
                .any(|c| !matches!(c, Component::Normal(_)))
            {
                return Err(Error::new(
                    "invalid_firmware",
                    "Manifest component path escapes firmware directory",
                ));
            }
            // Accept an extracted IPSW tree or an explicitly extracted flat component set.
            for candidate in [
                root.join(relative_path),
                root.join(relative_path.file_name().unwrap()),
            ] {
                if candidate.is_file() {
                    let candidate = candidate.canonicalize()?;
                    if !candidate.starts_with(root) {
                        return Err(Error::new(
                            "invalid_firmware",
                            "Firmware symlink escapes firmware directory",
                        ));
                    }
                    local = json!(candidate);
                    break;
                }
            }
        }
        if required && local.is_null() {
            missing.push(name);
        }
        components.insert(
            name.into(),
            json!({"manifest_path":relative,"path":local,"required_for_probe":required}),
        );
    }
    Ok(json!({
        "version":manifest["ProductVersion"],"build":manifest["ProductBuildVersion"],
        "device":manifest["SupportedProductTypes"].get(0).and_then(Value::as_str).unwrap_or("iPhone12,1"),"board":board,"variant":variant,
        "requires_sptm":identity["Manifest"].get("Ap,SecurePageTableMonitor").is_some(),
        "components":components,"missing_probe_components":missing,
        "manifest_sha256":digest(&bytes)
    }))
}

fn prepared_firmware(root: &Path, variant: &str) -> Result<Value> {
    let prepared = read_json(&root.join("tomacco-ios.json"), None)?;
    if prepared["format"] != 1
        || prepared["backend"] != "sptm"
        || !["ios-restore-shell", "ios-system-research"].contains(&s(&prepared, "environment"))
        || prepared["variant"] != variant
    {
        return Err(Error::new(
            "invalid_firmware",
            "Prepare a matching restore-shell image with tools/prepare_ios_emulator.py",
        ));
    }
    let mut value = firmware(root, variant, s(&prepared, "board"))?;
    if value["manifest_sha256"] != prepared["manifest_sha256"]
        || value["version"] != prepared["version"]
        || value["build"] != prepared["build"]
    {
        return Err(Error::new(
            "invalid_firmware",
            "Prepared image does not match its firmware manifest",
        ));
    }
    let exclave = prepared["environment"] == "ios-system-research"
        && ["exclave-dart", "exclave-mcc"].contains(&s(&prepared, "hardware_profile"));
    let mut names = vec![
        "kernel",
        "device_tree",
        "ramdisk",
        "trustcache",
        "sptm",
        "txm",
    ];
    if exclave {
        names.extend(["cl4", "exclave_integrity", "exclave_trustcache"]);
    }
    for name in names {
        let relative = Path::new(s(&prepared["components"][name], "path"));
        if relative.as_os_str().is_empty()
            || relative
                .components()
                .any(|c| !matches!(c, Component::Normal(_)))
        {
            return Err(Error::new(
                "invalid_firmware",
                "Prepared component path escapes firmware directory",
            ));
        }
        let file = root.join(relative).canonicalize()?;
        if !file.starts_with(root) || !file.is_file() {
            return Err(Error::new(
                "invalid_firmware",
                "Prepared component escapes firmware directory",
            ));
        }
        let metadata = input_artifact(&file)?;
        if metadata["sha256"] != prepared["components"][name]["sha256"] {
            return Err(Error::new(
                "firmware_modified",
                format!("Prepared component checksum mismatch: {name}"),
            ));
        }
        value["components"][name] =
            json!({"path":file,"required_for_probe":true,"sha256":metadata["sha256"]});
    }
    value["missing_probe_components"] = json!([]);
    value["environment"] = prepared["environment"].clone();
    if prepared["environment"] == "ios-system-research" {
        let hardware = prepared
            .get("hardware_profile")
            .and_then(Value::as_str)
            .unwrap_or("none");
        if prepared["memory_mb"] != 32768
            || prepared["diagnostics"]["platform"] != true
            || prepared["full_ios_boot_qualified"] != false
            || prepared["graphical_apps_qualified"] != false
            || !["none", "dcp-dart", "exclave-dart", "exclave-mcc"].contains(&hardware)
            || (exclave && prepared["secure_services_qualified"] != false)
            || (prepared.get("hardware_profile").is_some()
                && !prepared["hardware_profile"].is_string())
        {
            return Err(Error::new(
                "invalid_firmware",
                "Invalid full-system research contract",
            ));
        }
        value["memory_mb"] = prepared["memory_mb"].clone();
        value["diagnostics"] = prepared["diagnostics"].clone();
        value["hardware_profile"] = json!(hardware);
        value["secure_services_qualified"] = json!(false);
    }
    value["payload"] = prepared
        .get("payload")
        .cloned()
        .unwrap_or_else(|| json!([]));
    Ok(value)
}

pub fn inspect(a: &Value) -> Result {
    let root = path(s(a, "firmware"), true, true)?;
    let engine = path(s(a, "engine"), false, true)?;
    if !toolchain::executable(&engine) {
        return Err(Error::new("invalid_engine", "Engine is not executable"));
    }
    let version = toolchain::run(
        &[engine.to_string_lossy().into_owned(), "--version".into()],
        10,
        None,
        true,
    )?;
    let machines = toolchain::run(
        &[
            engine.to_string_lossy().into_owned(),
            "-machine".into(),
            "help".into(),
        ],
        10,
        None,
        true,
    )?;
    let supported = |name| {
        s(&machines, "stdout")
            .lines()
            .any(|line| line.split_whitespace().next() == Some(name))
    };
    let backend = if supported("darwin") {
        "sptm"
    } else if supported("t8030") {
        "inferno"
    } else {
        return Err(Error::new(
            "invalid_engine",
            "The selected engine implements neither the Darwin/SPTM nor Apple T8030 machine",
        ));
    };
    let (
        boot_framebuffer,
        boot_media,
        file_backed_boot_media,
        dart_iommu,
        exclave_core_loader,
        mcc_registers,
        ptd_mailbox,
    ) = if backend == "sptm" {
        let options = toolchain::run(
            &[
                engine.to_string_lossy().into_owned(),
                "-machine".into(),
                "darwin,help".into(),
            ],
            10,
            None,
            false,
        )?;
        let supports = |name| {
            s(&options, "stdout")
                .lines()
                .any(|line| line.trim_start().starts_with(name))
        };
        (
            supports("boot-display="),
            supports("boot-media="),
            supports("boot-storage="),
            supports("research-dart="),
            supports("research-cl4=")
                && supports("exclave-integrity=")
                && supports("exclave-trustcache="),
            supports("research-mcc="),
            supports("research-ptd="),
        )
    } else {
        (false, false, false, false, false, false, false)
    };
    let (arm_bus_fault_trace, mcc_mmio_trace, ptd_mmio_trace) = if backend == "sptm" {
        let traces = toolchain::run(
            &[
                engine.to_string_lossy().into_owned(),
                "-trace".into(),
                "help".into(),
            ],
            10,
            None,
            false,
        )?;
        let supports_trace = |name: &str| {
            ["stdout", "stderr"]
                .iter()
                .any(|channel| s(&traces, channel).lines().any(|line| line.trim() == name))
        };
        (
            supports_trace("tomacco_arm_bus_fault"),
            supports_trace("tomacco_mcc_mmio"),
            supports_trace("tomacco_ptd_mmio"),
        )
    } else {
        (false, false, false)
    };
    let firmware = if backend == "sptm" {
        prepared_firmware(&root, s(a, "variant"))?
    } else {
        firmware(&root, s(a, "variant"), "n104ap")?
    };
    Ok(
        json!({"engine":engine,"engine_version":s(&version,"stdout").trim(),
        "backend":backend,"firmware":firmware,"firmware_directory":root,
        "host":std::env::consts::ARCH,"execution":"local-arm64-emulation",
        "macos_vm_required":false,"qualified_for_app_development":false,
        "capabilities":{"boot_framebuffer":boot_framebuffer,"boot_media":boot_media,
                        "file_backed_boot_media":file_backed_boot_media,
                        "dart_iommu":dart_iommu,
                        "arm_bus_fault_trace":arm_bus_fault_trace,
                        "mcc_registers":mcc_registers,"mcc_mmio_trace":mcc_mmio_trace,
                        "ptd_mailbox":ptd_mailbox,"ptd_mmio_trace":ptd_mmio_trace,
                        "exclave_core_loader":exclave_core_loader,"secure_services":false,
                        "persistent_storage":false,"graphical_apps":false},
        "scope":if firmware["environment"]=="ios-system-research" {"Full-system research; no working main display, SEP or graphical apps"} else if backend=="sptm" {"iOS restore shell; no SpringBoard, graphical apps or audio"} else {"kernel bring-up; no verified iOS userspace, app installation, graphics or audio"}}),
    )
}

fn working_image_identity(value: &str, firmware: &Value) -> Result<Value> {
    let metadata = fs::symlink_metadata(expand(value))?;
    let file = path(value, false, true)?;
    let source = fs::metadata(s(&firmware["components"]["ramdisk"], "path"))?;
    if !metadata.is_file()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.nlink() != 1
        || metadata.mode() & 0o077 != 0
        || metadata.len() == 0
        || metadata.len() % 16384 != 0
        || metadata.len() != source.len()
        || (metadata.dev() == source.dev() && metadata.ino() == source.ino())
    {
        return Err(Error::new(
            "invalid_working_image",
            "Working image must be a separate owner-only regular copy, with one link and the same page-aligned size as the source ramdisk",
        ));
    }
    Ok(json!({"path":file,"device":metadata.dev(),"inode":metadata.ino(),"bytes":metadata.len()}))
}

pub fn probe(a: &Value) -> Result {
    let admission = Lock::open(&home()?.join("locks/ios-emulator-admission.lock"))?;
    admission.acquire(false)?;
    let mut key = a.clone();
    for name in ["engine", "firmware"] {
        key[name] = json!(path(s(a, name), name == "firmware", true)?);
    }
    let operation_key = json!({"operation":"ios.emulator.probe","arguments":key});
    if let Some(mut active) = jobs::active_exclusive("tomacco-ios-kernel-probe")? {
        if !b(&active, "cancelling") && active["plan"]["operation_key"] == operation_key {
            active["reused"] = json!(true);
            active["report_directory"] = active["plan"]["cwd"].clone();
            return Ok(active);
        }
        return Err(
            Error::new("ios_emulator_busy", "An iOS emulator job is already active")
                .details(json!({"job":active})),
        );
    }
    if a["script"]
        .as_str()
        .is_some_and(|script| script.len() > 32768)
    {
        return Err(Error::new(
            "input_too_large",
            "Guest scripts are limited to 32768 UTF-8 bytes",
        ));
    }
    let inspected = inspect(a)?;
    if inspected["firmware"]["environment"] == "ios-system-research" {
        return Err(Error::new(
            "research_environment",
            "Use ios.emulator.research for this full-system research bundle; it is not a command shell",
        ));
    }
    if a.get("working_image").is_some()
        && (a["boot_media"] != true || inspected["capabilities"]["file_backed_boot_media"] != true)
    {
        return Err(Error::new(
            "unsupported_working_image",
            "Working images require boot_media=true and an engine providing boot-storage",
        ));
    }
    if a["boot_media"] == true && inspected["capabilities"]["boot_media"] != true {
        return Err(Error::new(
            "unsupported_boot_media",
            "This engine cannot register the RAM image with IOMedia; rebuild the SPTM engine with Tomacco's patch",
        ));
    }
    if a.get("boot_display").is_some() && inspected["capabilities"]["boot_framebuffer"] != true {
        return Err(Error::new(
            "unsupported_boot_display",
            "This engine does not provide the guest boot framebuffer; rebuild the SPTM engine with Tomacco's patch",
        ));
    }
    if a.get("script").is_some() && inspected["backend"] != "sptm" {
        return Err(Error::new(
            "unsupported_engine",
            "Guest commands require the Darwin/SPTM restore-shell backend",
        ));
    }
    if inspected["firmware"]["missing_probe_components"]
        .as_array()
        .is_some_and(|v| !v.is_empty())
    {
        return Err(Error::new(
            "firmware_incomplete",
            "Extract the required boot components first",
        )
        .details(inspected));
    }
    let working_image = if a.get("working_image").is_some() {
        working_image_identity(s(a, "working_image"), &inspected["firmware"])?
    } else {
        Value::Null
    };
    let id = uuid::Uuid::new_v4().simple().to_string();
    let directory = home()?.join("ios-emulator").join(&id);
    private_dir(&directory)?;
    let machine = tempfile::Builder::new()
        .prefix("machine-")
        .tempdir_in(&directory)?;
    let socket = tempfile::Builder::new()
        .prefix("tomacco-ios-")
        .tempdir_in("/tmp")?;
    for path in [machine.path(), socket.path()] {
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    let mut request = inspected;
    request["probe_id"] = json!(id);
    request["directory"] = json!(directory);
    request["machine_directory"] = json!(machine.path());
    request["socket_directory"] = json!(socket.path());
    request["seconds"] = a["seconds"].clone();
    request["script"] = a["script"].clone();
    request["boot_display"] = a["boot_display"].clone();
    request["boot_media"] = json!(a["boot_media"] == true);
    request["working_image"] = working_image["path"].clone();
    request["working_image_identity"] = working_image;
    atomic_write(
        &directory.join("request.json"),
        &serde_json::to_vec_pretty(&request)?,
    )?;
    let cleanup = json!([
        jobs::temporary_directory(machine.path())?,
        jobs::temporary_directory(socket.path())?
    ]);
    let machine = machine.keep();
    let socket = socket.keep();
    let label = if a.get("script").is_some() {
        "iOS guest command"
    } else if request["backend"] == "sptm" {
        "iOS restore-shell boot"
    } else {
        "iOS kernel probe"
    };
    let result = jobs::submit(
        label,
        json!({
            "cwd":directory,"timeout":n(a,"seconds")+90,"exclusive":"tomacco-ios-kernel-probe", "single_active":true,
            "operation_key":operation_key,
            "steps":[{"argv":[crate::core::own_executable()?,"_ios_emulator_probe",directory.join("request.json")]}],
            "temporary_directories":cleanup,
            "always_artifacts":[directory.join("report.json"),directory.join("engine.log"),directory.join("console.log"),directory.join("guest-output.bin"),directory.join("frame.png"),directory.join("frame-before-boot.png"),directory.join("command.json")]
        }),
    );
    match result {
        Ok(mut job) => {
            job["probe_id"] = json!(id);
            job["report_directory"] = json!(directory);
            Ok(job)
        }
        Err(error) => {
            let _ = fs::remove_dir_all(machine);
            let _ = fs::remove_dir_all(socket);
            Err(error)
        }
    }
}

struct Qmp {
    reader: BufReader<UnixStream>,
    sequence: u64,
    transcript: Vec<Value>,
}
impl Qmp {
    fn open(socket: &Path) -> Result<Self> {
        let stream = UnixStream::connect(socket)?;
        stream.set_read_timeout(Some(Duration::from_millis(250)))?;
        stream.set_write_timeout(Some(Duration::from_secs(2)))?;
        let mut client = Self {
            reader: BufReader::new(stream),
            sequence: 0,
            transcript: Vec::new(),
        };
        let greeting = client.message(Instant::now() + Duration::from_secs(5))?;
        if greeting.get("QMP").is_none() {
            return Err(Error::new("invalid_qmp", "Missing QMP greeting"));
        }
        client.call("qmp_capabilities", json!({}))?;
        Ok(client)
    }
    fn message(&mut self, deadline: Instant) -> Result<Value> {
        let mut bytes = Vec::new();
        loop {
            if Instant::now() >= deadline {
                return Err(Error::new(
                    "qmp_timeout",
                    "Emulator control channel stopped responding",
                ));
            }
            // Limit each read, including peers that send a line without a newline.
            match self.reader.fill_buf() {
                Ok([]) => {
                    return Err(Error::new(
                        "qmp_closed",
                        "Emulator closed its control channel",
                    ));
                }
                Ok(chunk) => {
                    let length = chunk
                        .iter()
                        .position(|b| *b == b'\n')
                        .map_or(chunk.len(), |p| p + 1);
                    if bytes.len() + length > 1024 * 1024 {
                        return Err(Error::new("invalid_qmp", "QMP message exceeds 1 MiB"));
                    }
                    bytes.extend_from_slice(&chunk[..length]);
                    self.reader.consume(length);
                    if bytes.last() == Some(&b'\n') {
                        return Ok(serde_json::from_slice(&bytes)?);
                    }
                }
                Err(e)
                    if matches!(
                        e.kind(),
                        std::io::ErrorKind::WouldBlock
                            | std::io::ErrorKind::TimedOut
                            | std::io::ErrorKind::Interrupted
                    ) => {}
                Err(e) => return Err(e.into()),
            }
        }
    }
    fn call(&mut self, operation: &str, arguments: Value) -> Result<Value> {
        self.sequence += 1;
        let request = json!({"execute":operation,"arguments":arguments,"id":self.sequence});
        let mut bytes = serde_json::to_vec(&request)?;
        bytes.push(b'\n');
        self.reader.get_mut().write_all(&bytes)?;
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            let response = self.message(deadline)?;
            if response["id"] == request["id"] {
                self.transcript
                    .push(json!({"request":request,"response":response}));
                if let Some(error) = response.get("error") {
                    return Err(Error::new("qmp_error", error.to_string()));
                }
                return Ok(response["return"].clone());
            }
        }
    }
}

struct Engine(Child);
impl Engine {
    fn wait_for_shutdown(&mut self) -> Result<()> {
        let deadline = Instant::now() + Duration::from_secs(15);
        loop {
            if let Some(status) = self.0.try_wait()? {
                return if status.success() {
                    Ok(())
                } else {
                    Err(Error::new(
                        "engine_shutdown_failed",
                        format!("Engine exited with {status}"),
                    ))
                };
            }
            if Instant::now() >= deadline {
                return Err(Error::new(
                    "engine_shutdown_timeout",
                    "Engine did not finish flushing its working image",
                ));
            }
            thread::sleep(Duration::from_millis(20));
        }
    }
}
impl Drop for Engine {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
fn log_reader(
    mut pipe: impl Read + Send + 'static,
    log: Arc<Mutex<(File, usize)>>,
    truncated: Arc<AtomicBool>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let mut bytes = [0u8; 8192];
        while let Ok(length) = pipe.read(&mut bytes) {
            if length == 0 {
                break;
            }
            let Ok(mut log) = log.lock() else {
                break;
            };
            let count = length.min(LOG_LIMIT.saturating_sub(log.1));
            if log.0.write_all(&bytes[..count]).is_err() {
                break;
            }
            log.1 += count;
            if count < length {
                truncated.store(true, Ordering::Relaxed);
            }
        }
    })
}

fn boot_arguments(request: &Value) -> Result<Vec<String>> {
    let machine = PathBuf::from(s(request, "machine_directory"));
    let socket = PathBuf::from(s(request, "socket_directory"));
    let component = |name: &str| s(&request["firmware"]["components"][name], "path").to_string();
    // QEMU uses comma-separated key/value options even when exec uses argv.
    let escape = |value: &str| value.replace(',', ",,");
    if request["backend"] == "sptm" {
        let display = &request["boot_display"];
        let mut machine_name = if display.is_object() {
            let width = n(display, "width");
            let height = n(display, "height");
            if !(64..=4096).contains(&width) || !(64..=4096).contains(&height) {
                return Err(Error::new(
                    "invalid_boot_display",
                    "Boot display dimensions must be 64..4096",
                ));
            }
            format!("darwin,boot-display={width}x{height}")
        } else {
            "darwin".into()
        };
        if request["boot_media"] == true {
            machine_name.push_str(",boot-media=on");
        }
        if ["dcp-dart", "exclave-dart", "exclave-mcc"]
            .contains(&s(&request["firmware"], "hardware_profile"))
        {
            machine_name.push_str(",research-dart=on");
        }
        if request["firmware"]["hardware_profile"] == "exclave-mcc" {
            machine_name.push_str(",research-mcc=on");
            if request["capabilities"]["ptd_mailbox"] == true {
                machine_name.push_str(",research-ptd=on");
            }
        }
        if ["exclave-dart", "exclave-mcc"].contains(&s(&request["firmware"], "hardware_profile")) {
            for (option, name) in [
                ("research-cl4", "cl4"),
                ("exclave-integrity", "exclave_integrity"),
                ("exclave-trustcache", "exclave_trustcache"),
            ] {
                machine_name.push_str(&format!(",{option}={}", escape(&component(name))));
            }
        }
        if let Some(image) = request["working_image"].as_str() {
            machine_name.push_str(",boot-storage=");
            machine_name.push_str(&escape(image));
        }
        let mut kernel_args = "rd=md0 serial=3 -v -noprogress wdt=-1 wlan-olyhal-abort".to_string();
        if request["firmware"]["hardware_profile"] == "exclave-mcc" {
            // Original driver option: this functional memory model supplies
            // no physical memory-traffic counters. Do not invent readings.
            kernel_args.push_str(" mcc_cntr_cfg=0");
        }
        return Ok(vec![
            s(request, "engine").into(),
            "-M".into(),
            machine_name,
            "-bootkc".into(),
            component("kernel"),
            "-dtree".into(),
            component("device_tree"),
            "-sptm".into(),
            component("sptm"),
            "-txm".into(),
            component("txm"),
            "-tc".into(),
            component("trustcache"),
            "-ramdisk".into(),
            component("ramdisk"),
            "-args".into(),
            kernel_args,
            "-m".into(),
            "8G".into(),
            "-display".into(),
            "none".into(),
            "-monitor".into(),
            "none".into(),
            "-chardev".into(),
            format!(
                "socket,id=serial0,path={},server=on,wait=off",
                escape(&socket.join("console.sock").to_string_lossy())
            ),
            "-serial".into(),
            "chardev:serial0".into(),
            "-nic".into(),
            "none".into(),
            "-no-reboot".into(),
            "-S".into(),
            "-qmp".into(),
            format!(
                "unix:{},server=on,wait=off",
                escape(&socket.join("qmp.sock").to_string_lossy())
            ),
        ]);
    }
    let mut argv=vec![s(request,"engine").into(),"-M".into(),
        format!("t8030,trustcache={},kaslr-off=true,usb-conn-addr={}",escape(&component("trustcache")),escape(&socket.join("usb.sock").to_string_lossy())),
        "-kernel".into(),component("kernel"),"-dtb".into(),component("device_tree"),"-initrd".into(),component("ramdisk"),
        "-append".into(),"tlto_us=-1 mtxspin=-1 agm-genuine=1 agm-authentic=1 agm-trusted=1 serial=3 wdt=-1 -vm_compressor_wk_sw -v debug=0x14e".into(),
        "-smp".into(),"6".into(),"-m".into(),"4G".into(),"-display".into(),"none".into(),"-audiodev".into(),"none,id=audio0".into(),
        "-serial".into(),"stdio".into(),"-monitor".into(),"none".into(),"-no-reboot".into(),"-S".into(),
        "-qmp".into(),format!("unix:{},server=on,wait=off",escape(&socket.join("qmp.sock").to_string_lossy()))];
    for (index, (name, size, nstype)) in [
        ("root", 32u64 * 1024 * 1024 * 1024, 1),
        ("firmware", 8 * 1024 * 1024, 2),
        ("syscfg", 128 * 1024, 3),
        ("ctrl_bits", 8192, 4),
        ("nvram", 8192, 5),
        ("effaceable", 4096, 6),
        ("panic_log", 1024 * 1024, 8),
    ]
    .into_iter()
    .enumerate()
    {
        let file = machine.join(name);
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&file)?
            .set_len(size)?;
        argv.extend(["-drive".into(),format!("file={},format=raw,if=none,id={name}",escape(&file.to_string_lossy())),
            "-device".into(),format!("{},drive={name},id={name},bus=nvme-bus.0,nsid={},nstype={nstype},logical_block_size=4096,physical_block_size=4096",
                if name=="nvram" {"apple-nvram"} else {"nvme-ns"},index+1)]);
    }
    Ok(argv)
}

fn register(text: &str, name: &str) -> Option<u64> {
    text.split_whitespace().find_map(|part| {
        part.strip_prefix(name)
            .and_then(|p| p.strip_prefix('='))
            .and_then(|p| u64::from_str_radix(p, 16).ok())
    })
}
fn snapshot(qmp: &mut Qmp) -> Result<Value> {
    let status = qmp.call("query-status", json!({}))?;
    let registers = qmp.call(
        "human-monitor-command",
        json!({"command-line":"info registers"}),
    )?;
    let text = registers.as_str().unwrap_or_default();
    Ok(
        json!({"time":now(),"status":status,"pc":register(text,"PC"),"x0":register(text,"X00"),"registers":text}),
    )
}

fn input_artifact(file: &Path) -> Result<Value> {
    let mut metadata = artifact(file, now())?;
    // These are read-only inputs, not outputs produced or modified by this job.
    metadata.as_object_mut().unwrap().remove("modified_in_job");
    Ok(metadata)
}

fn capture_frame(qmp: &mut Qmp, file: &Path, started: f64) -> Result<Value> {
    qmp.call("screendump", json!({"filename":file,"format":"png"}))?;
    artifact(file, started)
}

struct Console {
    stream: UnixStream,
    bytes: Arc<Mutex<Vec<u8>>>,
    error: Arc<Mutex<Option<Error>>>,
    reader: Option<thread::JoinHandle<()>>,
    nonce: String,
    sent: bool,
    packets: Vec<String>,
    next_packet: usize,
    awaiting_ack: Option<usize>,
}
impl Console {
    fn open(socket: &Path, directory: &Path) -> Result<Self> {
        let stream = UnixStream::connect(socket)?;
        stream.set_write_timeout(Some(Duration::from_secs(3)))?;
        let mut input = stream.try_clone()?;
        let mut log = private_file(&directory.join("console.log"), false)?;
        let bytes = Arc::new(Mutex::new(Vec::<u8>::new()));
        let error = Arc::new(Mutex::new(None));
        let buffer = bytes.clone();
        let failure = error.clone();
        let reader = thread::spawn(move || {
            let result = (|| -> Result<()> {
                let mut chunk = [0u8; 8192];
                loop {
                    let count = match input.read(&mut chunk) {
                        Ok(0) => return Ok(()),
                        Ok(count) => count,
                        Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                        Err(e) => return Err(e.into()),
                    };
                    let mut buffer = buffer.lock().unwrap();
                    if buffer.len() + count > LOG_LIMIT {
                        return Err(Error::new(
                            "console_log_limit",
                            "Guest console exceeded 8 MiB",
                        ));
                    }
                    log.write_all(&chunk[..count])?;
                    buffer.extend_from_slice(&chunk[..count]);
                }
            })();
            if let Err(e) = result {
                *failure.lock().unwrap() = Some(e);
            }
        });
        Ok(Self {
            stream,
            bytes,
            error,
            reader: Some(reader),
            nonce: uuid::Uuid::new_v4().simple().to_string(),
            sent: false,
            packets: Vec::new(),
            next_packet: 0,
            awaiting_ack: None,
        })
    }
    fn poll(&mut self, script: &str) -> Result<Option<(i32, Vec<u8>)>> {
        if let Some(error) = self.error.lock().unwrap().take() {
            return Err(error);
        }
        let text = String::from_utf8_lossy(&self.bytes.lock().unwrap()).replace('\r', "");
        if !self.sent && text.contains("bash-") && text.contains("# ") {
            let encoded = STANDARD.encode(script);
            let acknowledge = |index| {
                format!(
                    "printf '\\n%s%s:{index}\\n' 'TOMACCO_ACK_' '{}'",
                    self.nonce
                )
            };
            self.packets.push(format!(
                "/bin/stty -echo; set -o pipefail; tomacco_input=''; {}\n",
                acknowledge(0)
            ));
            for (index, chunk) in encoded.as_bytes().chunks(512).enumerate() {
                self.packets.push(format!(
                    "tomacco_input=\"${{tomacco_input}}{}\"; {}\n",
                    std::str::from_utf8(chunk).unwrap(),
                    acknowledge(index + 1)
                ));
            }
            // The nonce never appears as a complete marker in the echoed input.
            // Apple's console output masks bytes to seven bits. Encode both ways.
            // Acknowledged short lines avoid overflowing the guest TTY input queue.
            // A shell variable avoids temporary files in the read-only restore root.
            self.packets.push(format!(
                "printf '\\n%s%s\\n' 'TOMACCO_BEGIN_' '{}'; printf '%s' \"$tomacco_input\" | /bin/base64 -d | /bin/sh 2>&1 | /bin/base64 | while IFS= read -r tomacco_line; do printf '%s%s:%s\\n' 'TOMACCO_OUTPUT_' '{}' \"$tomacco_line\"; done; tomacco_rc=$?; printf '\\n%s%s:%d\\n' 'TOMACCO_END_' '{}' \"$tomacco_rc\"; unset tomacco_input\n",
                self.nonce,self.nonce,self.nonce));
            self.sent = true;
        }
        if let Some(index) = self.awaiting_ack {
            let marker = format!("TOMACCO_ACK_{}:{index}", self.nonce);
            if text.lines().any(|line| line == marker) {
                self.awaiting_ack = None;
            }
        }
        if self.awaiting_ack.is_none() && self.next_packet < self.packets.len() {
            self.stream
                .write_all(self.packets[self.next_packet].as_bytes())?;
            if self.next_packet + 1 < self.packets.len() {
                self.awaiting_ack = Some(self.next_packet);
            }
            self.next_packet += 1;
        }
        let begin = format!("TOMACCO_BEGIN_{}", self.nonce);
        let end = format!("TOMACCO_END_{}:", self.nonce);
        let output_marker = format!("TOMACCO_OUTPUT_{}:", self.nonce);
        let mut started = false;
        let mut encoded = String::new();
        for line in text.lines() {
            if line == begin {
                started = true;
                continue;
            }
            if started {
                if let Some(code) = line.strip_prefix(&end).and_then(|v| v.parse::<i32>().ok()) {
                    let output = STANDARD.decode(encoded).map_err(|_| {
                        Error::new(
                            "invalid_guest_response",
                            "Guest output framing was corrupted; inspect console.log",
                        )
                    })?;
                    return Ok(Some((code, output)));
                }
                if let Some(chunk) = line.strip_prefix(&output_marker) {
                    encoded.push_str(chunk);
                }
            }
        }
        Ok(None)
    }
}

impl Drop for Console {
    fn drop(&mut self) {
        let _ = self.stream.shutdown(std::net::Shutdown::Both);
        if let Some(reader) = self.reader.take() {
            let _ = reader.join();
        }
    }
}

pub fn worker(specification: &str) -> Result<()> {
    let request = read_json(&path(specification, false, true)?, None)?;
    let directory = PathBuf::from(s(&request, "directory"));
    let mut report = json!({"probe_id":request["probe_id"],"firmware":request["firmware"],
        "engine":request["engine"],"engine_version":request["engine_version"],"backend":request["backend"],
        "macos_vm_used":false,"qualified_for_app_development":false,"guest_boot_verified":false,
        "boot_display":request["boot_display"],"graphical_apps_verified":false,
        "boot_media_requested":request["boot_media"] == true,"persistent_storage_verified":false,
        "working_image":request["working_image"],"working_image_flushed":false,
        "full_ios_boot_verified":false,"guest_environment":if request["backend"]=="sptm" {"ios-restore-shell"} else {"kernel-bringup"},
        "observations":[],"state":"starting","started":now()});
    let result = (|| -> Result<()> {
        let argv = boot_arguments(&request)?;
        atomic_write(
            &directory.join("command.json"),
            &serde_json::to_vec_pretty(&argv)?,
        )?;
        report["engine_artifact"] = input_artifact(Path::new(s(&request, "engine")))?;
        let mut inputs = serde_json::Map::new();
        for &(name, _, _) in COMPONENTS {
            if request["firmware"]["components"][name]["required_for_probe"] == true {
                let file = s(&request["firmware"]["components"][name], "path");
                inputs.insert(name.into(), input_artifact(Path::new(file))?);
            }
        }
        report["input_artifacts"] = Value::Object(inputs);
        if let Some(image) = request["working_image"].as_str() {
            let identity = working_image_identity(image, &request["firmware"])?;
            if identity != request["working_image_identity"] {
                return Err(Error::new(
                    "working_image_changed",
                    "Working image was replaced before the job started",
                ));
            }
            report["working_image_before"] = input_artifact(Path::new(image))?;
        }
        let mut command = Command::new(&argv[0]);
        command
            .args(&argv[1..])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        toolchain::parent_bound(&mut command);
        unsafe {
            command.pre_exec(|| {
                let limit = libc::rlimit {
                    rlim_cur: 0,
                    rlim_max: 0,
                };
                if libc::setrlimit(libc::RLIMIT_CORE, &limit) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                if libc::getpriority(libc::PRIO_PROCESS, 0) < 10
                    && libc::setpriority(libc::PRIO_PROCESS, 0, 10) != 0
                {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let mut engine = Engine(command.spawn()?);
        let log = Arc::new(Mutex::new((
            private_file(&directory.join("engine.log"), false)?,
            0,
        )));
        let truncated = Arc::new(AtomicBool::new(false));
        let readers = [
            log_reader(
                engine.0.stdout.take().unwrap(),
                log.clone(),
                truncated.clone(),
            ),
            log_reader(engine.0.stderr.take().unwrap(), log, truncated.clone()),
        ];
        let capture = (|| -> Result<()> {
            let socket = PathBuf::from(s(&request, "socket_directory")).join("qmp.sock");
            let deadline = Instant::now() + Duration::from_secs(20);
            while !socket.exists() {
                if let Some(status) = engine.0.try_wait()? {
                    return Err(Error::new(
                        "engine_exited",
                        format!("Emulator exited before QMP: {status}"),
                    ));
                }
                if Instant::now() >= deadline {
                    return Err(Error::new(
                        "engine_timeout",
                        "Emulator did not open its control socket",
                    ));
                }
                thread::sleep(Duration::from_millis(50));
            }
            let mut qmp = Qmp::open(&socket)?;
            let mut console = if request["backend"] == "sptm" {
                Some(Console::open(
                    &socket.with_file_name("console.sock"),
                    &directory,
                )?)
            } else {
                None
            };
            let observation = (|| -> Result<()> {
                report["observations"]
                    .as_array_mut()
                    .unwrap()
                    .push(snapshot(&mut qmp)?);
                if request["boot_display"].is_object() {
                    report["frame_before_boot"] =
                        capture_frame(&mut qmp, &directory.join("frame-before-boot.png"), now())?;
                }
                qmp.call("cont", json!({}))?;
                let deadline = Instant::now() + Duration::from_secs(n(&request, "seconds"));
                let mut next_snapshot = Instant::now();
                while Instant::now() < deadline {
                    thread::sleep(
                        Duration::from_millis(if console.is_some() { 25 } else { 500 })
                            .min(deadline.saturating_duration_since(Instant::now())),
                    );
                    if truncated.load(Ordering::Relaxed) {
                        return Err(Error::new(
                            "engine_log_limit",
                            "Emulator exceeded the 8 MiB log budget",
                        ));
                    }
                    if let Some(status) = engine.0.try_wait()? {
                        return Err(Error::new(
                            "engine_exited",
                            format!("Emulator exited during boot: {status}"),
                        ));
                    }
                    let x0 = if Instant::now() >= next_snapshot {
                        let next = snapshot(&mut qmp)?;
                        let x0 = next["x0"].as_u64();
                        report["observations"].as_array_mut().unwrap().push(next);
                        next_snapshot = Instant::now() + Duration::from_millis(500);
                        x0
                    } else {
                        None
                    };
                    if let Some(console) = console.as_mut() {
                        let script=request["script"].as_str().unwrap_or("set -e\nuname -a\nid\ncat /System/Library/CoreServices/SystemVersion.plist\n");
                        if let Some((code, output)) = console.poll(script)? {
                            report["guest_boot_verified"] = json!(true);
                            report["guest_shell_verified"] = json!(true);
                            report["guest_exit_code"] = json!(code);
                            atomic_write(&directory.join("guest-output.bin"), &output)?;
                            report["guest_output_artifact"] = artifact(
                                &directory.join("guest-output.bin"),
                                report["started"].as_f64().unwrap_or_default(),
                            )?;
                            report["guest_output_utf8_valid"] =
                                json!(std::str::from_utf8(&output).is_ok());
                            report["guest_output"] = json!(String::from_utf8_lossy(&output));
                            report["state"] = json!(if code != 0 {
                                "guest_script_failed"
                            } else if request["script"].is_string() {
                                "guest_script_succeeded"
                            } else {
                                "restore_shell_verified"
                            });
                            break;
                        }
                    }
                    if matches!(x0, Some(0xdeadb001 | 0xdeadb002)) {
                        report["state"] = json!("early_boot_failure");
                        report["diagnosis"] = json!(
                            "XNU SPTM startup entered its invalid CPU/reset-handler loop. This engine does not establish the required SPTM/TXM boot environment."
                        );
                        break;
                    }
                }
                if console.is_some() && report["guest_shell_verified"] != true {
                    return Err(Error::new(
                        "guest_shell_timeout",
                        "No verified command response from the restore shell within the requested time",
                    ));
                }
                Ok(())
            })();
            // Preserve the actual final display even when boot or a guest
            // command fails. Stop the CPU before reading shared framebuffer RAM.
            let stopped = qmp.call("stop", json!({}));
            let frame = stopped.and_then(|_| {
                capture_frame(
                    &mut qmp,
                    &directory.join("frame.png"),
                    report["started"].as_f64().unwrap_or_default(),
                )
            });
            report["frame_capture"] = match &frame {
                Ok(value) => value.clone(),
                Err(e) => e.value(),
            };
            if request["boot_display"].is_object() {
                report["frame_provenance"] = json!("guest-ram-boot-framebuffer");
                report["boot_framebuffer_changed"] = json!(
                    report["frame_before_boot"]["sha256"].is_string()
                        && report["frame_capture"]["sha256"].is_string()
                        && report["frame_before_boot"]["sha256"]
                            != report["frame_capture"]["sha256"]
                );
            }
            let _ = qmp.call("quit", json!({}));
            report["qmp"] = json!(qmp.transcript);
            observation?;
            if request["boot_display"].is_object() {
                frame?;
            }
            Ok(())
        })();
        let shutdown = if request["working_image"].is_string() && capture.is_ok() {
            engine.wait_for_shutdown()
        } else {
            Ok(())
        };
        drop(engine);
        for reader in readers {
            let _ = reader.join();
        }
        report["log_truncated"] = json!(truncated.load(Ordering::Relaxed));
        if let Some(image) = request["working_image"].as_str() {
            report["working_image_after"] = input_artifact(Path::new(image))?;
            let marker = format!(
                "TOMACCO_BOOT_STORAGE_FLUSH bytes={} msync=0 fsync=0",
                request["working_image_identity"]["bytes"]
            );
            let flushed = fs::read_to_string(directory.join("engine.log"))?
                .lines()
                .any(|line| line == marker);
            report["working_image_flushed"] = json!(flushed && shutdown.is_ok());
            shutdown?;
            if capture.is_ok() && !flushed {
                return Err(Error::new(
                    "working_image_flush_failed",
                    "Engine did not confirm flushing the working image",
                ));
            }
        }
        capture
    })();
    let observations = report["observations"].as_array().unwrap();
    let initial = observations.first().and_then(|v| v["pc"].as_u64());
    let changed = initial.is_some_and(|initial| {
        observations
            .iter()
            .skip(1)
            .any(|v| v["pc"].as_u64().is_some_and(|pc| pc != initial))
    });
    report["program_counter_changed"] = json!(changed);
    report["finished"] = json!(now());
    if let Err(ref error) = result {
        report["state"] = json!("probe_failed");
        report["error"] = error.value();
    } else if report["state"] == "starting" {
        report["state"] = json!("observation_complete_unqualified");
    }
    atomic_write(
        &directory.join("report.json"),
        &serde_json::to_vec_pretty(&report)?,
    )?;
    println!(
        "{}",
        json!({"report":directory.join("report.json"),"state":report["state"],"program_counter_changed":changed,"guest_boot_verified":report["guest_boot_verified"],"full_ios_boot_verified":false})
    );
    result?;
    if report["state"] == "early_boot_failure" {
        return Err(Error::new("ios_boot_failed", s(&report, "diagnosis")));
    }
    if report["state"] == "guest_script_failed" {
        return Err(Error::new(
            "guest_script_failed",
            format!("Guest script exited with {}", report["guest_exit_code"]),
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn registers_require_exact_names_and_valid_hex() {
        assert_eq!(
            register("PC=0000000804a081a0 X00=00000000deadb002 X01=aa", "PC"),
            Some(0x804a081a0)
        );
        assert_eq!(register("X000=deaf X00=broken", "X00"), None);
    }
    #[test]
    fn qmp_handles_fragmented_messages_events_and_ids() {
        let temp = socket_tempdir();
        let socket = temp.path().join("qmp");
        let listener = std::os::unix::net::UnixListener::bind(&socket).unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream.write_all(b"{\"QMP\":").unwrap();
            thread::sleep(Duration::from_millis(300));
            stream.write_all(b"{}}\r\n").unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            for _ in 0..2 {
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                let v: Value = serde_json::from_str(&line).unwrap();
                stream
                    .write_all(b"{\"event\":\"RESUME\"}\n{\"return\":{},\"id\":99}\n")
                    .unwrap();
                writeln!(
                    stream,
                    "{}",
                    json!({"return":{"running":false},"id":v["id"]})
                )
                .unwrap();
            }
        });
        let mut qmp = Qmp::open(&socket).unwrap();
        assert_eq!(
            qmp.call("query-status", json!({})).unwrap()["running"],
            false
        );
        server.join().unwrap();
    }
}
