//! Bounded full-system jobs. A completed observation never implies a usable iPhone.
use super::*;
use std::os::unix::{fs::OpenOptionsExt, io::AsRawFd};

pub(super) mod bus_faults;
pub mod platform;
pub(super) mod sysreg_faults;

pub fn report(a: &Value) -> Result {
    let job = jobs::get(s(a, "job_id"))?;
    let operation = job["plan"]["steps"][0]["argv"][1].as_str().unwrap_or("");
    let is_restore = job["plan"]["operation_key"]["operation"] == "ios.emulator.restore"
        && Path::new(operation)
            .file_name()
            .is_some_and(|name| name == "ios_paravirt_restore.py");
    let is_prepare = job["plan"]["operation_key"]["operation"] == "ios.emulator.prepare_restore"
        && Path::new(operation)
            .file_name()
            .is_some_and(|name| name == "prepare_ios_paravirt_restore.py");
    let is_userland = job["plan"]["operation_key"]["operation"] == "ios.emulator.prepare_userland"
        && Path::new(operation)
            .file_name()
            .is_some_and(|name| name == "prepare_ios_userland.py");
    let is_gpu_plugin = job["plan"]["operation_key"]["operation"]
        == "ios.emulator.build_gpu_plugin"
        && Path::new(operation)
            .file_name()
            .is_some_and(|name| name == "build_ios_gpu_plugin.py");
    let is_cryptex = job["plan"]["operation_key"]["operation"] == "ios.emulator.prepare_cryptexes"
        && Path::new(operation)
            .file_name()
            .is_some_and(|name| name == "prepare_ios_cryptex.py");
    let is_guest_build = job["plan"]["operation_key"]["operation"]
        == "ios.emulator.build_guest_tools"
        && Path::new(operation)
            .file_name()
            .is_some_and(|name| name == "build_ios_guest_tools.py");
    let is_guest_install = job["plan"]["operation_key"]["operation"]
        == "ios.emulator.prepare_guest_tools"
        && Path::new(operation)
            .file_name()
            .is_some_and(|name| name == "prepare_ios_guest_tools.py");
    let is_paravirtual = job["plan"]["operation_key"]["operation"] == "ios.emulator.paravirtual"
        && Path::new(operation)
            .file_name()
            .is_some_and(|name| name == "run_ios_paravirt.py");
    let is_usb_bridge = job["plan"]["operation_key"]["operation"] == "ios.emulator.usb_bridge"
        && Path::new(operation)
            .file_name()
            .is_some_and(|name| name == "ios_paravirt_usbmuxd.py");
    if is_prepare
        || is_userland
        || is_gpu_plugin
        || is_cryptex
        || is_guest_build
        || is_guest_install
    {
        let directory = PathBuf::from(s(&job["plan"]["operation_key"]["arguments"], "output"));
        let file = directory.join(if is_userland {
            "userland.json"
        } else if is_gpu_plugin {
            "plugin.json"
        } else if is_cryptex {
            "cryptex.json"
        } else if is_guest_build {
            "guest-tools.json"
        } else if is_guest_install {
            "guest-tools-install.json"
        } else {
            "restore-runtime.json"
        });
        let value = if file.is_file() {
            if directory.is_symlink()
                || file.canonicalize()?.parent() != Some(directory.as_path())
                || fs::metadata(&file)?.len() > 1024 * 1024
            {
                return Err(Error::new(
                    "invalid_report_path",
                    "Preparation report escapes its output or exceeds its size limit",
                ));
            }
            read_json(&file, None)?
        } else {
            Value::Null
        };
        return Ok(
            json!({"job_id":job["id"],"job_state":job["state"],"report":value,"report_directory":directory}),
        );
    }
    if !is_restore
        && !is_paravirtual
        && !is_usb_bridge
        && !["_ios_research", "_ios_emulator_probe", "_ios_pmp"].contains(&operation)
    {
        return Err(Error::new(
            "not_ios_emulator_job",
            "Select an iOS emulator job",
        ));
    }
    let directory = PathBuf::from(s(&job["plan"], "cwd")).canonicalize()?;
    let root = home()?.join("ios-emulator").canonicalize()?;
    if directory.parent() != Some(root.as_path()) {
        return Err(Error::new(
            "invalid_report_path",
            "Report is outside the owned iOS run directory",
        ));
    }
    let report_directory = if is_restore {
        directory.join("restore")
    } else if is_paravirtual {
        directory.join("paravirtual")
    } else if is_usb_bridge {
        directory.join("usbmuxd")
    } else {
        directory.clone()
    };
    let file = report_directory.join(if is_restore {
        "restore.json"
    } else if is_paravirtual {
        "result.json"
    } else if is_usb_bridge {
        "bridge.json"
    } else {
        "report.json"
    });
    let value = if file.is_file() {
        if report_directory.is_symlink()
            || file.canonicalize()?.parent() != Some(report_directory.as_path())
            || fs::metadata(&file)?.len() > 8 * 1024 * 1024
        {
            return Err(Error::new(
                "invalid_report_path",
                "Report escapes its run or exceeds its size limit",
            ));
        }
        read_json(&file, None)?
    } else {
        Value::Null
    };
    let mut reply = json!({"job_id":job["id"],"job_state":job["state"],"report":value,"report_directory":directory});
    if is_paravirtual {
        // How a stop was requested through the guest's control lane, with
        // whether the guest confirmed flushing its storage first. Null for a
        // guest that ended in any other way.
        let file = report_directory.join("stop.json");
        reply["stop"] =
            if file.is_file() && !file.is_symlink() && fs::metadata(&file)?.len() <= 64 * 1024 {
                read_json(&file, None)?
            } else {
                Value::Null
            };
    }
    Ok(reply)
}

pub(crate) fn script(name: &str) -> Result<PathBuf> {
    let exe = crate::core::own_executable()?;
    let dir = exe.parent().unwrap();
    [
        Some(dir.join("tools").join(name)),
        Some(dir.join("../share/tomacco/tools").join(name)),
        crate::core::source_checkout().map(|root| root.join("tools").join(name)),
    ]
    .into_iter()
    .flatten()
    .find(|p| p.is_file())
    .ok_or_else(|| {
        Error::new(
            "research_tools_missing",
            "Install the iOS research preparation tools",
        )
    })
}

pub fn prepare(a: &Value) -> Result {
    let mut argv = vec![
        toolchain::find("python3")?,
        script("prepare_ios_research_runtime.py")?
            .to_string_lossy()
            .into_owned(),
    ];
    let output = path(s(a, "output"), true, false)?;
    for name in ["firmware", "inputs", "bootstrap", "image", "output"] {
        let file = path(s(a, name), name != "image", name != "output")?;
        if name == "output" && (file.exists() || file.is_symlink()) {
            return Err(Error::new(
                "output_exists",
                "Choose a new research bundle directory",
            ));
        }
        argv.extend([format!("--{name}"), file.to_string_lossy().into_owned()]);
    }
    if a.get("exclave").is_some() {
        argv.extend([
            "--exclave".into(),
            path(s(a, "exclave"), true, true)?
                .to_string_lossy()
                .into_owned(),
        ]);
    }
    fs::create_dir_all(output.parent().unwrap())?;
    let stage = tempfile::Builder::new()
        .prefix(".tomacco-ios-prepare-")
        .tempdir_in(output.parent().unwrap())?;
    fs::set_permissions(stage.path(), fs::Permissions::from_mode(0o700))?;
    argv.extend([
        "--staging".into(),
        stage.path().to_string_lossy().into_owned(),
    ]);
    let result = jobs::submit(
        "Prepare iOS research bundle",
        json!({
            "cwd":home()?, "steps":[{"argv":argv,"artifacts":[output.join("tomacco-ios.json")]}], "timeout":1800,
            "exclusive":format!("ios-research-prepare:{}",output.display()), "single_active":true,
            "temporary_directories":[jobs::temporary_directory(stage.path())?]
        }),
    );
    if result.is_ok() {
        let _ = stage.keep();
    }
    result
}

pub fn start(a: &Value) -> Result {
    let admission = Lock::open(&home()?.join("locks/ios-emulator-admission.lock"))?;
    admission.acquire(false)?;
    let mut key = a.clone();
    for name in ["engine", "firmware"] {
        key[name] = json!(path(s(a, name), name == "firmware", true)?);
    }
    if let Some(mut active) = jobs::active_exclusive("tomacco-ios-kernel-probe")? {
        if !b(&active, "cancelling")
            && active["plan"]["operation_key"]
                == json!({"operation":"ios.emulator.research","arguments":key})
        {
            active["reused"] = json!(true);
            active["report_directory"] = active["plan"]["cwd"].clone();
            return Ok(active);
        }
        return Err(
            Error::new("ios_emulator_busy", "An iOS emulator job is already active")
                .remedy("Reuse its job or cancel it before starting a different guest")
                .details(json!({"job":active})),
        );
    }
    let inspected = inspect(&key)?;
    if inspected["backend"] != "sptm"
        || inspected["firmware"]["environment"] != "ios-system-research"
    {
        return Err(Error::new(
            "research_bundle_required",
            "Prepare a full-system bundle with ios.emulator.prepare_research",
        ));
    }
    for flag in ["boot_framebuffer", "boot_media", "file_backed_boot_media"] {
        if inspected["capabilities"][flag] != true {
            return Err(Error::new(
                "research_hardware_missing",
                format!("Engine does not support {flag}"),
            ));
        }
    }
    if ["dcp-dart", "exclave-dart", "exclave-mcc"]
        .contains(&s(&inspected["firmware"], "hardware_profile"))
        && inspected["capabilities"]["dart_iommu"] != true
    {
        return Err(Error::new(
            "research_hardware_missing",
            "This bundle requires an engine built with the T8110 DART model",
        ));
    }
    if ["exclave-dart", "exclave-mcc"].contains(&s(&inspected["firmware"], "hardware_profile"))
        && inspected["capabilities"]["exclave_core_loader"] != true
    {
        return Err(Error::new(
            "research_hardware_missing",
            "This bundle requires an engine with the ExclaveCore loader",
        ));
    }
    if inspected["firmware"]["hardware_profile"] == "exclave-mcc"
        && inspected["capabilities"]["mcc_registers"] != true
    {
        return Err(Error::new(
            "research_hardware_missing",
            "This bundle requires an engine with the T8140 MCC register model",
        ));
    }
    let id = uuid::Uuid::new_v4().simple().to_string();
    let directory = home()?.join("ios-emulator").join(&id);
    private_dir(&directory)?;
    let machine = tempfile::Builder::new()
        .prefix("machine-")
        .tempdir_in(&directory)?;
    let sockets = tempfile::Builder::new()
        .prefix("tomacco-ios-")
        .tempdir_in("/tmp")?;
    for directory in [machine.path(), sockets.path()] {
        fs::set_permissions(directory, fs::Permissions::from_mode(0o700))?;
    }
    let mut request = inspected;
    request["probe_id"] = json!(id);
    request["directory"] = json!(directory);
    request["machine_directory"] = json!(machine.path());
    request["socket_directory"] = json!(sockets.path());
    request["seconds"] = key["seconds"].clone();
    request["boot_display"] = key["boot_display"].clone();
    request["boot_media"] = json!(true);
    request["working_image"] = json!(machine.path().join("working.apfs"));
    request["engine_artifact"] = input_artifact(Path::new(s(&request, "engine")))?;
    let cleanup = json!([
        jobs::temporary_directory(machine.path())?,
        jobs::temporary_directory(sockets.path())?
    ]);
    atomic_write(
        &directory.join("request.json"),
        &serde_json::to_vec_pretty(&request)?,
    )?;
    let result = jobs::submit(
        "iOS system research",
        json!({
            "cwd":directory,"timeout":n(a,"seconds")+300,"exclusive":"tomacco-ios-kernel-probe","single_active":true,
            "operation_key":{"operation":"ios.emulator.research","arguments":key},
            "steps":[{"argv":[crate::core::own_executable()?,"_ios_research",directory.join("request.json")]}],
            "temporary_directories":cleanup,
            "always_artifacts":[directory.join("report.json"),directory.join("engine.log"),directory.join("console.log"),
                directory.join("process.log"),directory.join("graphics.log"),directory.join("platform.log"),
                directory.join("platform.json"),directory.join("frame.png"),directory.join("command.json")]
        }),
    );
    if result.is_ok() {
        let _ = machine.keep();
        let _ = sockets.keep();
    }
    let mut job = result?;
    job["probe_id"] = json!(id);
    job["report_directory"] = json!(directory);
    Ok(job)
}

fn clone_image(source: &Path, destination: &Path) -> Result<()> {
    let mut input = File::open(source)?;
    let mut output = OpenOptions::new()
        .write(true)
        .read(true)
        .create_new(true)
        .mode(0o600)
        .open(destination)?;
    // Linux FICLONE creates an independent copy-on-write image, never a hardlink.
    if unsafe { libc::ioctl(output.as_raw_fd(), 0x40049409, input.as_raw_fd()) } != 0 {
        std::io::copy(&mut input, &mut output)?;
    }
    output.sync_all()?;
    Ok(())
}

struct Serial {
    stream: UnixStream,
    reader: Option<thread::JoinHandle<()>>,
    state: Arc<Mutex<SerialState>>,
}
#[derive(Default)]
struct SerialState {
    size: usize,
    error: Option<(&'static str, String)>,
}

fn kernel_panic(line: &[u8]) -> Option<String> {
    let text = String::from_utf8_lossy(line);
    let (cpu, rest) = text.strip_prefix("panic(cpu ")?.split_once(" caller 0x")?;
    let (address, message) = rest.split_once("): ")?;
    if cpu.is_empty()
        || !cpu.bytes().all(|b| b.is_ascii_digit())
        || address.is_empty()
        || address.len() > 16
        || !address.bytes().all(|b| b.is_ascii_hexdigit())
        || message.trim().is_empty()
    {
        return None;
    }
    Some(text.trim_end().to_string())
}

impl Serial {
    fn open(socket: &Path, output: &Path, name: &'static str, limit: usize) -> Result<Self> {
        let mut input = UnixStream::connect(socket)?;
        let stream = input.try_clone()?;
        let mut file = File::create(output)?;
        let state = Arc::new(Mutex::new(SerialState::default()));
        let progress = state.clone();
        // A guest UART write may hold QEMU's control lock. Draining only between
        // QMP calls deadlocks once that socket fills. Each bounded log therefore
        // has an independent reader, including during stop/quit and image flush.
        let reader = thread::Builder::new()
            .name(format!("ios-{name}"))
            .spawn(move || {
                let mut data = [0u8; 16384];
                let mut size = 0;
                let mut discard = false;
                let mut console_line = Vec::new();
                loop {
                    match input.read(&mut data) {
                        Ok(0) => break,
                        Ok(count) => {
                            if discard {
                                continue;
                            }
                            let allowed = count.min(limit.saturating_sub(size));
                            if let Err(error) = file.write_all(&data[..allowed]) {
                                progress.lock().unwrap().error =
                                    Some(("research_serial_io", format!("{name}: {error}")));
                                discard = true;
                                continue;
                            }
                            size += allowed;
                            let mut state = progress.lock().unwrap();
                            state.size = size;
                            if name == "console" && state.error.is_none() {
                                for byte in &data[..allowed] {
                                    if *byte == b'\n' {
                                        if let Some(message) = kernel_panic(&console_line) {
                                            state.error = Some(("guest_kernel_panic", message));
                                            break;
                                        }
                                        console_line.clear();
                                    } else if console_line.len() < 1024 {
                                        console_line.push(*byte);
                                    }
                                }
                            }
                            if allowed != count {
                                state.error.get_or_insert((
                                    "research_log_limit",
                                    format!("{name} exceeded its log budget"),
                                ));
                                // Continue consuming without storing more data,
                                // so a failed job can still send QMP quit safely.
                                discard = true;
                            }
                        }
                        Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                        Err(error) => {
                            progress
                                .lock()
                                .unwrap()
                                .error
                                .get_or_insert(("research_serial_io", format!("{name}: {error}")));
                            break;
                        }
                    }
                }
            })?;
        Ok(Self {
            stream,
            reader: Some(reader),
            state,
        })
    }
    fn size(&self) -> Result<usize> {
        let state = self.state.lock().unwrap();
        if let Some((code, message)) = &state.error {
            return Err(Error::new(code, message.clone()));
        }
        Ok(state.size)
    }
    // Called after confirmed engine exit: consume all buffered bytes through
    // EOF before parsing the final evidence. Drop also handles early failures.
    fn finish(&mut self) -> Result<()> {
        if let Some(reader) = self.reader.take() {
            reader.join().map_err(|_| {
                Error::new(
                    "research_serial_failed",
                    "Serial reader terminated unexpectedly",
                )
            })?;
        }
        self.size()?;
        Ok(())
    }
}
impl Drop for Serial {
    fn drop(&mut self) {
        let _ = self.stream.shutdown(std::net::Shutdown::Both);
        if let Some(reader) = self.reader.take() {
            let _ = reader.join();
        }
    }
}

fn commands(request: &Value) -> Result<Vec<String>> {
    let mut args = boot_arguments(request)?;
    if request["capabilities"]["arm_bus_fault_trace"] == true {
        args.extend(["-trace".into(), "enable=tomacco_arm_bus_fault".into()]);
    }
    if request["firmware"]["hardware_profile"] == "exclave-mcc"
        && request["capabilities"]["mcc_mmio_trace"] == true
    {
        args.extend(["-trace".into(), "enable=tomacco_mcc_mmio".into()]);
    }
    if request["firmware"]["hardware_profile"] == "exclave-mcc"
        && request["capabilities"]["ptd_mailbox"] == true
        && request["capabilities"]["ptd_mmio_trace"] == true
    {
        args.extend(["-trace".into(), "enable=tomacco_ptd_mmio".into()]);
    }
    let index = args.iter().position(|v| v == "-m").unwrap() + 1;
    args[index] = format!("{}M", n(&request["firmware"], "memory_mb"));
    let sockets = PathBuf::from(s(request, "socket_directory"));
    for (index, name) in ["process", "graphics", "platform"].iter().enumerate() {
        args.extend([
            "-chardev".into(),
            format!(
                "socket,id=research{index},path={},server=on,wait=off",
                sockets
                    .join(format!("{name}.sock"))
                    .to_string_lossy()
                    .replace(',', ",,")
            ),
            "-serial".into(),
            format!("chardev:research{index}"),
        ]);
    }
    Ok(args)
}

pub fn worker(file: &str) -> Result<()> {
    let request = read_json(&path(file, false, true)?, None)?;
    let directory = PathBuf::from(s(&request, "directory"));
    let mut report = json!({"format":1,"probe_id":request["probe_id"],"environment":"ios-system-research",
        "firmware":request["firmware"],
        "started":now(),"state":"preparing","guest_observations_verified":false,"full_ios_boot_verified":false,
        "graphical_apps_verified":false,"sep_functionality_verified":false,"macos_vm_used":false,
        "working_image_disposable":true,"observations":[]});
    let trace_enabled = request["capabilities"]["arm_bus_fault_trace"] == true;
    report["bus_faults"] = bus_faults::parse("", trace_enabled);
    let save = |r: &Value| {
        atomic_write(
            &directory.join("report.json"),
            &serde_json::to_vec_pretty(r).unwrap(),
        )
    };
    save(&report)?;
    let result = (|| -> Result<()> {
        let actual = input_artifact(Path::new(s(&request, "engine")))?;
        if actual["sha256"] != request["engine_artifact"]["sha256"] {
            return Err(Error::new(
                "engine_changed",
                "Engine changed after submission",
            ));
        }
        report["engine_artifact"] = actual;
        for (name, part) in request["firmware"]["components"]
            .as_object()
            .ok_or_else(|| Error::new("invalid_firmware", "Research components are missing"))?
        {
            if name == "ramdisk" || part["required_for_probe"] != true {
                continue;
            }
            if input_artifact(Path::new(s(part, "path")))?["sha256"] != part["sha256"] {
                return Err(Error::new(
                    "firmware_modified",
                    format!("Research component changed: {name}"),
                ));
            }
        }
        let source = Path::new(s(&request["firmware"]["components"]["ramdisk"], "path"));
        let working = Path::new(s(&request, "working_image"));
        clone_image(source, working)?;
        let copy = input_artifact(working)?;
        if copy["sha256"] != request["firmware"]["components"]["ramdisk"]["sha256"] {
            return Err(Error::new(
                "firmware_modified",
                "Research image changed after submission",
            ));
        }
        report["working_image_before"] = copy;
        let argv = commands(&request)?;
        atomic_write(
            &directory.join("command.json"),
            &serde_json::to_vec_pretty(&argv)?,
        )?;
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
                if libc::setrlimit(libc::RLIMIT_CORE, &limit) != 0
                    || (libc::getpriority(libc::PRIO_PROCESS, 0) < 10
                        && libc::setpriority(libc::PRIO_PROCESS, 0, 10) != 0)
                {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let mut engine = Engine(command.spawn()?);
        report["engine_pid"] = json!(engine.0.id());
        let log = Arc::new(Mutex::new((File::create(directory.join("engine.log"))?, 0)));
        let truncated = Arc::new(AtomicBool::new(false));
        let readers = [
            log_reader(
                engine.0.stdout.take().unwrap(),
                log.clone(),
                truncated.clone(),
            ),
            log_reader(engine.0.stderr.take().unwrap(), log, truncated.clone()),
        ];
        let sockets = PathBuf::from(s(&request, "socket_directory"));
        let observed = (|| -> Result<()> {
            let ready = Instant::now() + Duration::from_secs(30);
            while !sockets.join("qmp.sock").exists() {
                if engine.0.try_wait()?.is_some() {
                    return Err(Error::new("engine_exited", "Emulator exited before QMP"));
                }
                if Instant::now() >= ready {
                    return Err(Error::new("engine_timeout", "Emulator did not open QMP"));
                }
                thread::sleep(Duration::from_millis(50));
            }
            let mut qmp = Qmp::open(&sockets.join("qmp.sock"))?;
            let mut streams = Vec::new();
            for (name, limit) in [
                ("console", LOG_LIMIT),
                ("process", LOG_LIMIT),
                ("graphics", 16 * 1024 * 1024),
                ("platform", 256 * 1024),
            ] {
                streams.push(Serial::open(
                    &sockets.join(format!("{name}.sock")),
                    &directory.join(format!("{name}.log")),
                    name,
                    limit,
                )?);
            }
            report["state"] = json!("observing");
            save(&report)?;
            println!("iOS guest started; collecting real platform observations");
            let observation = (|| -> Result<()> {
                qmp.call("cont", json!({}))?;
                let deadline = Instant::now() + Duration::from_secs(n(&request, "seconds"));
                let mut next = Instant::now();
                let mut last_platform_size = 0;
                while Instant::now() < deadline {
                    for stream in &streams {
                        stream.size()?;
                    }
                    if truncated.load(Ordering::Relaxed) {
                        return Err(Error::new(
                            "research_log_limit",
                            "Engine exceeded its log budget",
                        ));
                    }
                    if engine.0.try_wait()?.is_some() {
                        return Err(Error::new(
                            "engine_exited",
                            "Emulator exited during research",
                        ));
                    }
                    if Instant::now() >= next {
                        report["observations"]
                            .as_array_mut()
                            .unwrap()
                            .push(snapshot(&mut qmp)?);
                        save(&report)?;
                        next = Instant::now() + Duration::from_secs(5);
                    }
                    let length = streams[3].size()?;
                    if length > last_platform_size {
                        last_platform_size = length;
                        let raw = fs::read(directory.join("platform.log"))?;
                        // A line may be split between socket reads. Parse complete records only.
                        if raw.ends_with(b"\n") {
                            let observed = platform::parse(&raw)?;
                            report["platform"] = observed.clone();
                            if observed["completed"] == true {
                                complete_uart(&observed)?;
                                report["platform"] = observed;
                                report["guest_observations_verified"] = json!(true);
                                break;
                            }
                            if observed["terminal_error"].is_string() {
                                return Err(Error::new(
                                    "guest_diagnostic_failed",
                                    s(&observed, "terminal_error"),
                                ));
                            }
                        }
                    }
                    thread::sleep(Duration::from_millis(25));
                }
                if report["guest_observations_verified"] != true {
                    return Err(Error::new(
                        "guest_observation_timeout",
                        "The guest did not complete both platform observations",
                    ));
                }
                Ok(())
            })();
            let stopped = qmp.call("stop", json!({}));
            let frame_error = match stopped.and_then(|_| {
                capture_frame(
                    &mut qmp,
                    &directory.join("frame.png"),
                    report["started"].as_f64().unwrap(),
                )
            }) {
                Ok(v) => {
                    report["frame_capture"] = v;
                    None
                }
                Err(e) => {
                    report["frame_capture"] = e.value();
                    Some(e)
                }
            };
            report["frame_provenance"] = json!("guest-ram-boot-framebuffer");
            let quit = qmp.call("quit", json!({}));
            let shutdown = quit.and_then(|_| engine.wait_for_shutdown());
            let mut serial_error = None;
            for stream in &mut streams {
                if shutdown.is_err() {
                    let _ = stream.stream.shutdown(std::net::Shutdown::Both);
                }
                if let Err(error) = stream.finish() {
                    serial_error.get_or_insert(error);
                }
            }
            report["qmp"] = json!(qmp.transcript);
            let mut evidence_error = None;
            if directory.join("platform.log").metadata()?.len() > 0 {
                match platform::parse(&fs::read(directory.join("platform.log"))?) {
                    Ok(value) => {
                        atomic_write(
                            &directory.join("platform.json"),
                            &serde_json::to_vec_pretty(&value)?,
                        )?;
                        report["platform"] = value;
                    }
                    Err(e) => {
                        report["platform_error"] = e.value();
                        report["guest_observations_verified"] = json!(false);
                        evidence_error = Some(e);
                    }
                }
            }
            observation?;
            shutdown?;
            if let Some(error) = serial_error {
                return Err(error);
            }
            if let Some(error) = frame_error {
                return Err(error);
            }
            if let Some(error) = evidence_error {
                return Err(error);
            }
            complete_uart(&report["platform"])?;
            Ok(())
        })();
        drop(engine);
        for reader in readers {
            let _ = reader.join();
        }
        let text = fs::read_to_string(directory.join("engine.log"))?;
        report["bus_faults"] = bus_faults::parse(&text, trace_enabled);
        report["bus_faults"]["source"] = input_artifact(&directory.join("engine.log"))?;
        report["working_image_flushed"] = json!(text.contains(&format!(
            "TOMACCO_BOOT_STORAGE_FLUSH bytes={} msync=0 fsync=0",
            working.metadata()?.len()
        )));
        observed?;
        if report["working_image_flushed"] != true {
            return Err(Error::new(
                "storage_flush_unverified",
                "Engine did not confirm flushing the disposable image",
            ));
        }
        Ok(())
    })();
    report["finished"] = json!(now());
    report["state"] = json!(if result.is_ok() {
        "research_observed"
    } else {
        "research_failed"
    });
    if let Err(ref error) = result {
        report["error"] = error.value();
    }
    save(&report)?;
    println!(
        "{}",
        json!({"state":report["state"],"report":directory.join("report.json"),"graphical_apps_verified":false})
    );
    result
}

fn complete_uart(observed: &Value) -> Result<()> {
    if observed["completed"] != true
        || observed["identity"]["mode"] != "uart"
        || observed["snapshots"].as_array().map(Vec::len) != Some(2)
    {
        return Err(Error::new(
            "invalid_platform_observations",
            "Expected both scheduled UART observations",
        ));
    }
    Ok(())
}
