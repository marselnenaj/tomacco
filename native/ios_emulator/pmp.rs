//! Execute a verified PMP image in an isolated ARM64 address space.
//! Reaching an instruction or collecting a fault does not qualify power services.
use super::research::{bus_faults, script, sysreg_faults};
use super::*;

const MACHINE: &str = "tomacco-pmp-research";
const ASC_MACHINE: &str = "tomacco-pmp-asc-research";

fn cpu_identity(a: &Value) -> Result<(u64, u64)> {
    let parse = |name: &str, default: u64, mask: u64| -> Result<u64> {
        let text = s(a, name);
        if text.is_empty() {
            return Ok(default);
        }
        let value = text.strip_prefix("0x").and_then(|hex| {
            (!hex.is_empty() && hex.len() <= 16 && hex.bytes().all(|b| b.is_ascii_hexdigit()))
                .then(|| u64::from_str_radix(hex, 16).ok())
                .flatten()
        });
        value.filter(|value| value & !mask == 0).ok_or_else(|| {
            Error::new(
                "invalid_pmp_cpu_identity",
                format!("Invalid {name}: use a hexadecimal value within mask 0x{mask:x}"),
            )
        })
    };
    Ok((
        parse("cpu_midr", 0x610f0000, 0xffffffff)?,
        parse("cpu_affinity", 0, 0xff00ffffff)?,
    ))
}

fn stop_address(value: &str) -> Result<Option<u64>> {
    if value.is_empty() {
        return Ok(None);
    }
    let address = value.strip_prefix("0x").and_then(|hex| {
        (!hex.is_empty() && hex.len() <= 16 && hex.bytes().all(|b| b.is_ascii_hexdigit()))
            .then(|| u64::from_str_radix(hex, 16).ok())
            .flatten()
    });
    match address {
        Some(address) if address % 4 == 0 => Ok(Some(address)),
        _ => Err(Error::new(
            "invalid_pmp_breakpoint",
            "Use an aligned ARM64 instruction address in hexadecimal",
        )),
    }
}

fn executable_address(firmware: &Value, address: u64) -> bool {
    firmware["layout"]["segments"]
        .as_array()
        .is_some_and(|segments| {
            segments.iter().any(|segment| {
                n(segment, "permissions") & 4 != 0
                    && address >= n(segment, "address")
                    && address
                        .checked_sub(n(segment, "address"))
                        .and_then(|offset| offset.checked_add(4))
                        .is_some_and(|end| end <= n(segment, "file_bytes"))
            })
        })
}

fn verified_bundle(root: &Path) -> Result<Value> {
    let response = toolchain::run(
        &[
            toolchain::find("python3")?,
            script("prepare_ios_pmp.py")?.to_string_lossy().into_owned(),
            "--verify".into(),
            root.to_string_lossy().into_owned(),
        ],
        20,
        None,
        false,
    )?;
    if response["exit_code"] != 0 {
        return Err(Error::new(
            "invalid_pmp_bundle",
            s(&response, "stderr").trim(),
        ));
    }
    Ok(serde_json::from_str(s(&response, "stdout"))?)
}

pub fn prepare(a: &Value) -> Result {
    let output = path(s(a, "output"), true, false)?;
    if output.exists() || output.is_symlink() {
        return Err(Error::new(
            "output_exists",
            "Choose a new PMP bundle directory",
        ));
    }
    let mut argv = vec![
        toolchain::find("python3")?,
        script("prepare_ios_pmp.py")?.to_string_lossy().into_owned(),
        "--firmware".into(),
        path(s(a, "firmware"), true, true)?
            .to_string_lossy()
            .into_owned(),
        "--manifest".into(),
        path(s(a, "manifest"), false, true)?
            .to_string_lossy()
            .into_owned(),
        "--board".into(),
        s(a, "board").into(),
        "--output".into(),
        output.to_string_lossy().into_owned(),
    ];
    fs::create_dir_all(output.parent().unwrap())?;
    let stage = tempfile::Builder::new()
        .prefix(".tomacco-pmp-")
        .tempdir_in(output.parent().unwrap())?;
    fs::set_permissions(stage.path(), fs::Permissions::from_mode(0o700))?;
    argv.extend([
        "--staging".into(),
        stage.path().to_string_lossy().into_owned(),
    ]);
    let result = jobs::submit(
        "Prepare PMP firmware",
        json!({
            "cwd":home()?, "steps":[{"argv":argv,"artifacts":[output.join("pmp-research.json")]}],
            "timeout":180,"exclusive":format!("ios-pmp-prepare:{}",output.display()),"single_active":true,
            "temporary_directories":[jobs::temporary_directory(stage.path())?]
        }),
    );
    if result.is_ok() {
        let _ = stage.keep();
    }
    result
}

pub fn inspect(a: &Value) -> Result {
    let (midr, affinity) = cpu_identity(a)?;
    let engine = path(s(a, "engine"), false, true)?;
    if engine.metadata()?.permissions().mode() & 0o111 == 0 {
        return Err(Error::new("invalid_engine", "Engine is not executable"));
    }
    let root = path(s(a, "firmware"), true, true)?;
    let firmware = verified_bundle(&root)?;
    let stop_at = stop_address(s(a, "stop_at"))?;
    if stop_at.is_some_and(|address| !executable_address(&firmware, address)) {
        return Err(Error::new(
            "invalid_pmp_breakpoint",
            "Breakpoint must be inside verified executable firmware bytes",
        ));
    }
    let invoke = |options: &[&str]| {
        let mut argv = vec![engine.to_string_lossy().into_owned()];
        argv.extend(options.iter().map(|s| (*s).into()));
        toolchain::run(&argv, 10, None, true)
    };
    let machines = invoke(&["-machine", "help"])?;
    let has_machine = |name| {
        s(&machines, "stdout")
            .lines()
            .any(|l| l.split_whitespace().next() == Some(name))
    };
    if !has_machine(MACHINE) {
        return Err(Error::new(
            "pmp_engine_required",
            "Build an engine with tomacco-pmp-research",
        ));
    }
    let traces = invoke(&["-trace", "help"])?;
    let has_trace = |name| {
        ["stdout", "stderr"]
            .iter()
            .any(|channel| s(&traces, channel).lines().any(|l| l.trim() == name))
    };
    if !has_trace("tomacco_arm_bus_fault") {
        return Err(Error::new(
            "pmp_trace_required",
            "PMP research requires architectural bus-fault tracing",
        ));
    }
    if !has_trace("tomacco_arm_sysreg_fault") {
        return Err(Error::new("pmp_cpu_policy_required", "Rebuild the engine with strict PMP system-register handling")
            .remedy("Use the current tools/build_ios_emulator.py; older engines silently skip unknown registers"));
    }
    let controller = match s(a, "controller") {
        "" | "none" => "none",
        "asc-v6" => "asc-v6",
        _ => {
            return Err(Error::new(
                "invalid_argument",
                "Unknown PMP controller model",
            ));
        }
    };
    let asc_available = has_machine(ASC_MACHINE) && has_trace("tomacco_asc_controller_mmio");
    if controller == "asc-v6" && !asc_available {
        return Err(Error::new(
            "pmp_controller_required",
            "Build an engine with tomacco-pmp-asc-research and ASC controller tracing",
        ));
    }
    let version = invoke(&["--version"])?;
    Ok(
        json!({"engine":engine,"engine_version":s(&version,"stdout").trim(),
        "engine_artifact":input_artifact(&engine)?,"firmware_directory":root,"firmware":firmware,
        "machine":if controller == "asc-v6" {ASC_MACHINE} else {MACHINE},
        "execution":"isolated-arm64-firmware","macos_vm_required":false,
        "stop_at":stop_at.map(|address|format!("0x{address:x}")).unwrap_or_default(),
        "capture_ram":b(a,"capture_ram"),
        "debug_policy":{"breakpoint":"TCG hardware instruction breakpoint","guest_code_written":false,
            "ram_base":"0x1000000","ram_bytes":2097152,"transport":"private Unix socket"},
        "controller":controller,"experimental_asc_available":asc_available,
        "controller_policy":if controller == "asc-v6" {json!({
            "revision":1,"configuration_reset":"zero writable fields; unmeasured silicon state",
            "external_interrupts":512,"mailbox_channels":1,"fifo_depth":8,
            "arbitration":"timer before external before mailbox; lowest source first",
            "timing_controls_emulated":false,"architectural_timers_connected":true,
            "timer_frequency_hz":24000000,"independent_kic_timers_emulated":false,
            "soc_wiring_qualified":false,"power_services_qualified":false
        })} else {Value::Null},
        "diagnostic_machines":{"asc_mailbox":has_machine("tomacco-asc-mailbox-test")
            && has_trace("tomacco_asc_mailbox_mmio")},
        "cpu_policy":{"model":"max","midr":format!("0x{midr:08x}"),
            "affinity":format!("0x{affinity:x}"),"exception_level":1,
            "identity_source":"explicit-laboratory-configuration",
            "implementer":format!("0x{:02x}",(midr>>24)&255),
            "part":format!("0x{:03x}",(midr>>4)&4095),
            "variant":(midr>>20)&15,"revision":midr&15,
            "counter_frequency_hz":24000000,"measured_silicon_identity":false,
            "unknown_system_registers":"architectural-undef",
            "pmp_tlbi_alias":"S1_6_c8_c7_0: invalidate all local translations from EL1",
            "pmp_tlbi_silicon_scope_qualified":false},
        "capabilities":{"isolated_execution":true,"arm_bus_fault_trace":true,
            "asc_controller":false,"pmp_power_services":false,"graphical_apps":false}}),
    )
}

pub fn start(a: &Value) -> Result {
    let admission = Lock::open(&home()?.join("locks/ios-emulator-admission.lock"))?;
    admission.acquire(false)?;
    let mut key = a.clone();
    let (midr, affinity) = cpu_identity(a)?;
    key["cpu_midr"] = json!(format!("0x{midr:08x}"));
    key["cpu_affinity"] = json!(format!("0x{affinity:x}"));
    key["stop_at"] = json!(
        stop_address(s(a, "stop_at"))?
            .map(|address| format!("0x{address:x}"))
            .unwrap_or_default()
    );
    for name in ["engine", "firmware"] {
        key[name] = json!(path(s(a, name), name == "firmware", true)?);
    }
    let operation_key = json!({"operation":"ios.emulator.pmp","arguments":key});
    if let Some(mut active) = jobs::active_exclusive("tomacco-ios-kernel-probe")? {
        if !b(&active, "cancelling") && active["plan"]["operation_key"] == operation_key {
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
    let mut request = inspect(&key)?;
    let id = uuid::Uuid::new_v4().simple().to_string();
    let directory = home()?.join("ios-emulator").join(&id);
    private_dir(&directory)?;
    let machine = tempfile::Builder::new()
        .prefix("machine-")
        .tempdir_in(&directory)?;
    let sockets = tempfile::Builder::new()
        .prefix("tomacco-pmp-")
        .tempdir_in("/tmp")?;
    for dir in [machine.path(), sockets.path()] {
        fs::set_permissions(dir, fs::Permissions::from_mode(0o700))?;
    }
    request["probe_id"] = json!(id);
    request["directory"] = json!(directory);
    request["machine_directory"] = json!(machine.path());
    request["socket_directory"] = json!(sockets.path());
    request["seconds"] = key["seconds"].clone();
    atomic_write(
        &directory.join("request.json"),
        &serde_json::to_vec_pretty(&request)?,
    )?;
    let result = jobs::submit(
        "PMP firmware execution",
        json!({
            "cwd":directory,"timeout":n(a,"seconds")+90,
            "exclusive":"tomacco-ios-kernel-probe","single_active":true,"operation_key":operation_key,
            "steps":[{"argv":[crate::core::own_executable()?,"_ios_pmp",directory.join("request.json")]}],
            "temporary_directories":[jobs::temporary_directory(machine.path())?,jobs::temporary_directory(sockets.path())?],
            "always_artifacts":[directory.join("report.json"),directory.join("engine.log"),directory.join("command.json"),directory.join("ram.bin")]
        }),
    );
    if result.is_ok() {
        let _ = machine.keep();
        let _ = sockets.keep();
    }
    result.map(|mut job| {
        job["report_directory"] = json!(directory);
        job
    })
}

fn commands(request: &Value) -> Vec<String> {
    let machine = Path::new(s(request, "machine_directory"));
    let sockets = Path::new(s(request, "socket_directory"));
    let escape = |p: PathBuf| p.to_string_lossy().replace(',', ",,");
    let controller = s(request, "controller") == "asc-v6";
    let mut argv = vec![
        s(request, "engine").into(),
        "-machine".into(),
        if controller { ASC_MACHINE } else { MACHINE }.into(),
        "-cpu".into(),
        format!(
            "max,midr={},mp-affinity={},tomacco-strict-sysregs=on",
            s(&request["cpu_policy"], "midr"),
            s(&request["cpu_policy"], "affinity")
        ),
        "-m".into(),
        "2M".into(),
        "-nodefaults".into(),
        "-display".into(),
        "none".into(),
        "-monitor".into(),
        "none".into(),
        "-S".into(),
        "-device".into(),
        format!("loader,file={},cpu-num=0", escape(machine.join("pmp.elf"))),
        "-qmp".into(),
        format!(
            "unix:{},server=on,wait=off",
            escape(sockets.join("qmp.sock"))
        ),
        "-trace".into(),
        "enable=tomacco_arm_bus_fault".into(),
        "-trace".into(),
        "enable=tomacco_arm_sysreg_fault".into(),
        "-d".into(),
        "int".into(),
    ];
    if controller {
        argv.extend(["-trace".into(), "enable=tomacco_asc_controller_mmio".into()]);
    }
    if !s(request, "stop_at").is_empty() {
        argv.extend([
            "-chardev".into(),
            format!(
                "socket,path={},server=on,wait=off,id=pmp-debug",
                escape(sockets.join("debug.sock"))
            ),
            "-gdb".into(),
            "chardev:pmp-debug".into(),
        ]);
    }
    argv
}

fn capture_ram(qmp: &mut Qmp, directory: &Path) -> Result<Value> {
    let file = directory.join("ram.bin");
    if file.exists() || file.is_symlink() {
        return Err(Error::new(
            "pmp_memory_capture",
            "Refusing to overwrite an existing RAM capture",
        ));
    }
    qmp.call(
        "pmemsave",
        json!({"val":0x1000000,"size":2097152,"filename":file}),
    )?;
    if file.metadata()?.len() != 2097152 || file.is_symlink() {
        return Err(Error::new(
            "pmp_memory_capture",
            "Incomplete PMP RAM capture",
        ));
    }
    fs::set_permissions(&file, fs::Permissions::from_mode(0o600))?;
    let mut record = input_artifact(&file)?;
    record["physical_base"] = json!("0x1000000");
    record["guest_stopped"] = json!(true);
    Ok(record)
}

pub fn memory(a: &Value) -> Result {
    use std::os::unix::fs::OpenOptionsExt;
    let job = super::research::report(a)?;
    let report = &job["report"];
    if ["queued", "running"].contains(&s(&job, "job_state")) {
        return Err(Error::new(
            "pmp_capture_pending",
            "Wait for the PMP job to finish before reading its capture",
        ));
    }
    let capture = &report["memory_capture"];
    if report["environment"] != "isolated-pmp-firmware-research" || !capture.is_object() {
        return Err(
            Error::new("pmp_memory_unavailable", "This job has no PMP RAM capture")
                .remedy("Run ios.emulator.pmp with capture_ram:true"),
        );
    }
    let address = s(a, "address")
        .strip_prefix("0x")
        .filter(|hex| {
            !hex.is_empty() && hex.len() <= 16 && hex.bytes().all(|b| b.is_ascii_hexdigit())
        })
        .and_then(|hex| u64::from_str_radix(hex, 16).ok())
        .ok_or_else(|| Error::new("invalid_memory_range", "Use a hexadecimal physical address"))?;
    let length = n(a, "length");
    let offset = address.checked_sub(0x1000000);
    if !(1..=4096).contains(&length)
        || !offset
            .and_then(|off| off.checked_add(length))
            .is_some_and(|end| end <= 2097152)
    {
        return Err(Error::new(
            "invalid_memory_range",
            "Read 1..4096 bytes inside physical PMP RAM [0x1000000, 0x1200000)",
        ));
    }
    let file = Path::new(s(&job, "report_directory")).join("ram.bin");
    if capture["path"] != json!(file)
        || capture["bytes"] != 2097152
        || capture["physical_base"] != "0x1000000"
    {
        return Err(Error::new(
            "pmp_memory_modified",
            "RAM capture metadata is inconsistent",
        ));
    }
    let input = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(&file)?;
    if !input.metadata()?.is_file() || input.metadata()?.len() != 2097152 {
        return Err(Error::new(
            "pmp_memory_modified",
            "Expected a complete regular RAM capture",
        ));
    }
    let mut bytes = Vec::new();
    input.take(2097153).read_to_end(&mut bytes)?;
    if bytes.len() != 2097152 || digest(&bytes) != s(capture, "sha256") {
        return Err(Error::new(
            "pmp_memory_modified",
            "RAM capture differs from the recorded SHA-256",
        ));
    }
    let offset = offset.unwrap() as usize;
    let data = &bytes[offset..offset + length as usize];
    Ok(
        json!({"job_id":job["job_id"],"address":format!("0x{address:x}"),"bytes":length,
            "sha256":capture["sha256"],"encoding":"hex",
            "data":data.iter().map(|byte|format!("{byte:02x}")).collect::<String>(),
            "ascii":data.iter().map(|byte|if (32..=126).contains(byte){*byte as char}else{'.'}).collect::<String>(),
            "u32_le":data.chunks_exact(4).map(|chunk|format!("0x{:08x}",u32::from_le_bytes(chunk.try_into().unwrap()))).collect::<Vec<_>>(),
            "u64_le":data.chunks_exact(8).map(|chunk|format!("0x{:016x}",u64::from_le_bytes(chunk.try_into().unwrap()))).collect::<Vec<_>>()
        }),
    )
}

fn pmp_snapshot(qmp: &mut Qmp, controller: bool) -> Result<Value> {
    let mut value = snapshot(qmp)?;
    // Hex strings preserve the full ARM64 value through JavaScript and MCP clients.
    let text = s(&value, "registers");
    let names = ["PC", "SP", "PSTATE"]
        .into_iter()
        .map(str::to_owned)
        .chain((0..31).map(|index| format!("X{index:02}")));
    let registers = names
        .filter_map(|name| {
            register(text, &name)
                .map(|number| json!({"name":name,"value":format!("0x{number:016x}")}))
        })
        .collect::<Vec<_>>();
    value["cpu_registers"] = json!(registers);
    if controller {
        let state = qmp
            .call(
                "qom-get",
                json!({"path":"/machine/asc","property":"controller-state"}),
            )
            .and_then(|response| {
                let text = response.as_str().filter(|text| text.len() <= 16384);
                let parsed: Value = serde_json::from_str(text.ok_or_else(|| {
                    Error::new(
                        "invalid_controller_snapshot",
                        "Expected bounded controller JSON",
                    )
                })?)?;
                if parsed["revision"] != 1 || !parsed["queues"].is_array() {
                    return Err(Error::new(
                        "invalid_controller_snapshot",
                        "Unsupported controller snapshot format",
                    ));
                }
                Ok(parsed)
            });
        match state {
            Ok(state) => value["controller_state"] = state,
            Err(error) => value["controller_snapshot_error"] = error.value(),
        }
    }
    Ok(value)
}

pub fn worker(file: &str) -> Result<()> {
    let request = read_json(&path(file, false, true)?, None)?;
    let directory = PathBuf::from(s(&request, "directory"));
    let controller = s(&request, "controller") == "asc-v6";
    let stop_at = stop_address(s(&request, "stop_at"))?;
    let mut report = json!({"format":1,"probe_id":request["probe_id"],
        "environment":"isolated-pmp-firmware-research","started":now(),"state":"preparing",
        "firmware":request["firmware"],"cpu_policy":request["cpu_policy"],
        "diagnostic_machines":request["diagnostic_machines"],
        "controller":request["controller"],"controller_policy":request["controller_policy"],
        "stop_at":request["stop_at"],"debug_policy":request["debug_policy"],"breakpoint_hit":false,
        "firmware_instructions_observed":false,"pmp_boot_verified":false,
        "power_services_verified":false,"full_ios_boot_verified":false,"graphical_apps_verified":false,
        "macos_vm_used":false,"observations":[],"bus_faults":bus_faults::parse("",true),
        "sysreg_faults":sysreg_faults::parse("")});
    let save = |r: &Value| {
        atomic_write(
            &directory.join("report.json"),
            &serde_json::to_vec_pretty(r).unwrap(),
        )
    };
    save(&report)?;
    let result = (|| -> Result<()> {
        let engine_file = Path::new(s(&request, "engine"));
        let actual = input_artifact(engine_file)?;
        if actual["sha256"] != request["engine_artifact"]["sha256"] {
            return Err(Error::new(
                "engine_changed",
                "PMP engine changed after submission",
            ));
        }
        report["engine_artifact"] = actual;
        let root = Path::new(s(&request, "firmware_directory"));
        if verified_bundle(root)? != request["firmware"] {
            return Err(Error::new(
                "firmware_modified",
                "PMP bundle changed after submission",
            ));
        }
        let executable = Path::new(s(&request, "machine_directory")).join("pmp.elf");
        let mut source = File::open(root.join("pmp.elf"))?.take(9 * 1024 * 1024);
        let mut copy = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&executable)?;
        std::io::copy(&mut source, &mut copy)?;
        copy.sync_all()?;
        drop(copy);
        let actual = input_artifact(&executable)?;
        if actual["sha256"] != request["firmware"]["components"]["executable"]["sha256"] {
            return Err(Error::new(
                "firmware_modified",
                "PMP executable changed while copying",
            ));
        }
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o400))?;
        report["executed_artifact"] = actual;
        let argv = commands(&request);
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
        let log_path = directory.join("engine.log");
        let log = Arc::new(Mutex::new((File::create(&log_path)?, 0)));
        let truncated = Arc::new(AtomicBool::new(false));
        let readers = [
            log_reader(
                engine.0.stdout.take().unwrap(),
                log.clone(),
                truncated.clone(),
            ),
            log_reader(engine.0.stderr.take().unwrap(), log, truncated.clone()),
        ];
        let observed = (|| -> Result<()> {
            let socket = Path::new(s(&request, "socket_directory")).join("qmp.sock");
            let ready = Instant::now() + Duration::from_secs(20);
            while !socket.exists() {
                if engine.0.try_wait()?.is_some() {
                    return Err(Error::new("engine_exited", "PMP engine exited before QMP"));
                }
                if Instant::now() >= ready {
                    return Err(Error::new("engine_timeout", "PMP engine did not open QMP"));
                }
                thread::sleep(Duration::from_millis(25));
            }
            let mut qmp = Qmp::open(&socket)?;
            let cpus = qmp.call("query-cpus-fast", json!({}))?;
            let cpu_path = s(&cpus[0], "qom-path");
            if cpu_path.is_empty()
                || qmp.call(
                    "qom-get",
                    json!({"path":cpu_path,"property":"tomacco-strict-sysregs"}),
                )? != true
                || qmp.call(
                    "qom-get",
                    json!({"path":"/machine","property":"pmp-tlbi-alias"}),
                )? != true
            {
                return Err(Error::new(
                    "pmp_cpu_policy_required",
                    "PMP CPU did not enable strict system-register handling",
                ));
            }
            report["strict_sysregs_verified"] = json!(true);
            let midr = qmp.call("qom-get", json!({"path":cpu_path,"property":"midr"}))?;
            let affinity =
                qmp.call("qom-get", json!({"path":cpu_path,"property":"mp-affinity"}))?;
            let (wanted_midr, wanted_affinity) = cpu_identity(&json!({
                "cpu_midr":request["cpu_policy"]["midr"],
                "cpu_affinity":request["cpu_policy"]["affinity"]
            }))?;
            if midr.as_u64() != Some(wanted_midr) || affinity.as_u64() != Some(wanted_affinity) {
                return Err(Error::new(
                    "pmp_cpu_identity_mismatch",
                    "Engine CPU identity differs from the requested configuration",
                )
                .details(json!({"midr":midr,"affinity":affinity})));
            }
            report["cpu_identity_verified"] = json!(true);
            let mut debugger = match stop_at {
                Some(address) => Some(super::pmp_debug::Breakpoint::open(
                    &socket.with_file_name("debug.sock"),
                    address,
                )?),
                None => None,
            };
            let observation = (|| -> Result<()> {
                report["observations"]
                    .as_array_mut()
                    .unwrap()
                    .push(pmp_snapshot(&mut qmp, controller)?);
                report["state"] = json!("executing");
                save(&report)?;
                if let Some(debugger) = debugger.as_mut() {
                    debugger.resume()?;
                } else {
                    qmp.call("cont", json!({}))?;
                }
                let deadline = Instant::now() + Duration::from_secs(n(&request, "seconds"));
                let mut next = Instant::now() + Duration::from_secs(1);
                let mut tail = File::open(&log_path)?;
                let mut unfinished = String::new();
                while Instant::now() < deadline {
                    thread::sleep(Duration::from_millis(25));
                    if truncated.load(Ordering::Relaxed) {
                        return Err(Error::new(
                            "engine_log_limit",
                            "PMP engine exceeded the 8 MiB log budget",
                        ));
                    }
                    if engine.0.try_wait()?.is_some() {
                        return Err(Error::new(
                            "engine_exited",
                            "PMP engine exited during observation",
                        ));
                    }
                    if let (Some(debugger), Some(address)) = (debugger.as_mut(), stop_at) {
                        let status = qmp.call("query-status", json!({}))?;
                        if status["running"] == false && status["status"] == "debug" {
                            let reply = debugger.stopped()?;
                            let state = pmp_snapshot(&mut qmp, controller)?;
                            report["observations"]
                                .as_array_mut()
                                .unwrap()
                                .push(state.clone());
                            if state["pc"].as_u64() != Some(address) {
                                return Err(Error::new("pmp_debug_stop", "CPU stopped at a different instruction")
                                    .details(json!({"requested":format!("0x{address:x}"),"observed":state["pc"]})));
                            }
                            report["breakpoint_hit"] = json!(true);
                            report["breakpoint_stop"] =
                                json!({"address":format!("0x{address:x}"),"response":reply});
                            return Ok(());
                        }
                    }
                    // The bounded shared trace parser never turns a partial
                    // record or unrelated text into an architectural fault.
                    tail.read_to_string(&mut unfinished)?;
                    if let Some(end) = unfinished.rfind('\n') {
                        let faults = bus_faults::parse(&unfinished[..=end], true);
                        let sysregs = sysreg_faults::parse(&unfinished[..=end]);
                        unfinished = unfinished[end + 1..].to_owned();
                        if !sysregs["events"].as_array().unwrap().is_empty() {
                            report["sysreg_faults"] = sysregs;
                            return Err(Error::new(
                                "pmp_sysreg_fault",
                                "PMP accessed an unimplemented CPU system register",
                            )
                            .details(
                                json!({"first_sysreg_fault":report["sysreg_faults"]["events"][0]}),
                            ));
                        }
                        if !faults["events"].as_array().unwrap().is_empty() {
                            report["bus_faults"] = faults;
                            return Err(Error::new(
                                "pmp_bus_fault",
                                "PMP accessed unsupported hardware or an unsupported register transaction",
                            )
                            .details(json!({"first_fault":report["bus_faults"]["events"][0]})));
                        }
                    }
                    if Instant::now() >= next {
                        report["observations"]
                            .as_array_mut()
                            .unwrap()
                            .push(pmp_snapshot(&mut qmp, controller)?);
                        save(&report)?;
                        next = Instant::now() + Duration::from_secs(1);
                    }
                }
                Err(Error::new(
                    "pmp_observation_timeout",
                    "PMP observation expired; firmware boot is unverified",
                ))
            })();
            let mut observation = observation;
            let stopped = qmp.call("stop", json!({}));
            if stopped.is_ok() {
                if let Ok(value) = pmp_snapshot(&mut qmp, controller) {
                    report["observations"].as_array_mut().unwrap().push(value);
                }
                if b(&request, "capture_ram") {
                    match capture_ram(&mut qmp, &directory) {
                        Ok(capture) => report["memory_capture"] = capture,
                        Err(error) => {
                            report["memory_capture_error"] = error.value();
                            if observation.is_ok() {
                                observation = Err(error);
                            }
                        }
                    }
                }
            } else if b(&request, "capture_ram") {
                report["memory_capture_error"] = json!({"code":"pmp_memory_capture","message":"CPU could not be stopped for a consistent capture"});
                if observation.is_ok() {
                    observation = stopped.map(|_| ());
                }
            }
            if let Some(debugger) = debugger.as_ref() {
                report["debugger"] = json!(debugger.transcript);
            }
            let quit = qmp.call("quit", json!({}));
            report["qmp"] = json!(qmp.transcript);
            let shutdown = quit.and_then(|_| engine.wait_for_shutdown());
            report["engine_shutdown_verified"] = json!(shutdown.is_ok());
            if observation.is_ok() {
                shutdown?;
            }
            observation
        })();
        drop(engine);
        for reader in readers {
            let _ = reader.join();
        }
        let text = fs::read_to_string(&log_path)?;
        report["bus_faults"] = bus_faults::parse(&text, true);
        report["bus_faults"]["source"] = input_artifact(&log_path)?;
        report["sysreg_faults"] = sysreg_faults::parse(&text);
        report["sysreg_faults"]["source"] = report["bus_faults"]["source"].clone();
        report["firmware_instructions_observed"] = json!(
            report["bus_faults"]["events"]
                .as_array()
                .unwrap()
                .iter()
                .chain(report["sysreg_faults"]["events"].as_array().unwrap().iter())
                .any(
                    |event| u64::from_str_radix(s(event, "pc").trim_start_matches("0x"), 16)
                        .is_ok_and(|pc| executable_address(&request["firmware"], pc))
                )
                || (b(&report, "breakpoint_hit")
                    && stop_at.is_some_and(
                        |address| address != n(&request["firmware"]["layout"], "entry")
                    ))
        );
        report["source_executable_preserved"] = json!(
            input_artifact(&root.join("pmp.elf"))?["sha256"]
                == request["firmware"]["components"]["executable"]["sha256"]
        );
        observed
    })();
    report["finished"] = json!(now());
    report["state"] = json!(if b(&report, "breakpoint_hit") && result.is_ok() {
        "pmp_breakpoint_reached"
    } else {
        "pmp_execution_stopped"
    });
    if let Err(ref error) = result {
        report["error"] = error.value();
    }
    save(&report)?;
    println!(
        "{}",
        json!({"report":directory.join("report.json"),"state":report["state"],"power_services_verified":false})
    );
    result
}
