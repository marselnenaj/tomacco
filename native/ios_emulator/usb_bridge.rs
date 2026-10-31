//! Private USB transport owned by a live paravirtual guest job.
use super::*;

pub fn start(a: &Value) -> Result {
    let parent_id = s(a, "job_id");
    identifier(parent_id, "job ID", Some("[0-9a-f]{32}"))?;
    let admission = Lock::open(
        &home()?
            .join("locks")
            .join(format!("ios-usb-bridge-{parent_id}.lock")),
    )?;
    admission.acquire(false)?;
    let parent = jobs::get(parent_id)?;
    let operation = s(&parent["plan"]["operation_key"], "operation");
    let script = parent["plan"]["steps"][0]["argv"][1].as_str().unwrap_or("");
    if operation != "ios.emulator.paravirtual"
        || Path::new(script)
            .file_name()
            .is_none_or(|name| name != "run_ios_paravirt.py")
        || !b(&parent["plan"]["operation_key"]["arguments"], "usb")
    {
        return Err(Error::new(
            "invalid_usb_guest",
            "Select a paravirtual iOS job started with usb=true",
        ));
    }
    if jobs::TERMINAL.contains(&s(&parent, "state")) {
        return Err(Error::new(
            "ios_guest_stopped",
            "The virtual iOS job has stopped",
        ));
    }
    let parent_directory = PathBuf::from(s(&parent["plan"], "cwd")).canonicalize()?;
    let root = home()?.join("ios-emulator").canonicalize()?;
    if parent_directory.parent() != Some(root.as_path())
        || parent_directory.join("paravirtual").is_symlink()
    {
        return Err(Error::new(
            "invalid_report_path",
            "Guest request is outside the owned run directory",
        ));
    }
    let exclusive = format!("ios-usb-bridge:{parent_id}");
    let key = json!({"operation":"ios.emulator.usb_bridge","arguments":a});
    let describe = |mut job: Value| {
        let directory = PathBuf::from(s(&job["plan"], "cwd"));
        job["report_directory"] = json!(directory);
        job["bridge"] = json!(directory.join("usbmuxd/bridge.json"));
        job
    };
    if let Some(mut active) = jobs::active_exclusive(&exclusive)? {
        if !b(&active, "cancelling") && active["plan"]["operation_key"] == key {
            active["reused"] = json!(true);
            return Ok(describe(active));
        }
        return Err(Error::new(
            "ios_usb_bridge_busy",
            "This virtual guest already has a USB bridge job",
        )
        .details(json!({"job":active})));
    }
    let directory = root.join(uuid::Uuid::new_v4().simple().to_string());
    private_dir(&directory)?;
    let output = directory.join("usbmuxd");
    let argv = vec![
        toolchain::find("python3")?,
        research::script("ios_paravirt_usbmuxd.py")?
            .to_string_lossy()
            .into_owned(),
        "--request".into(),
        parent_directory
            .join("paravirtual/request.json")
            .to_string_lossy()
            .into_owned(),
        "--output".into(),
        output.to_string_lossy().into_owned(),
        "--seconds".into(),
        n(a, "seconds").to_string(),
        "--wait-for-request".into(),
    ];
    let job = jobs::submit(
        "Connect virtual iOS USB",
        json!({
            "cwd":directory,"timeout":n(a,"seconds")+15,"exclusive":exclusive,"single_active":true,
            "parent_job":parent_id,"operation_key":key,"steps":[{"argv":argv}],
            "always_artifacts":[output.join("bridge.json")]
        }),
    )?;
    Ok(describe(job))
}
