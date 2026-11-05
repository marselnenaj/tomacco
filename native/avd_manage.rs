//! Managing existing AVDs as files: how much space each takes and whether it
//! may be running, a duplicate under a new name, a real rename and a wipe of
//! its user data. Nothing here reaches a device or the adb server: whether an
//! AVD runs is read from Tomacco's own emulator jobs and the lock files the
//! emulator keeps inside the AVD while it runs.
//!
//! A rename goes through the SDK's own `avdmanager move avd`, and a nickname
//! (device.label) is a separate thing that a rename carries over. A rename or
//! a wipe names the AVD a second time in `confirm`.
use crate::{catalog, core::*, devices, jobs, toolchain as tc};
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
};

/// What a wipe removes: the emulator recreates each from the system image on
/// the next boot. The SD card image and the AVD's configuration stay.
pub const USER_DATA: [&str; 5] = [
    "userdata-qemu.img",
    "userdata-qemu.img.qcow2",
    "cache.img",
    "cache.img.qcow2",
    "snapshots",
];
const PARTIAL: &str = ".partial";

/// One AVD on disk: its registry file and directory.
pub struct Avd {
    pub name: String,
    pub home: PathBuf,
    pub registry: PathBuf,
    pub directory: PathBuf,
}

pub fn find(name: &str) -> Result<Avd> {
    devices::avd_name(name)?;
    for home in devices::avd_homes()? {
        let registry = home.join(format!("{name}.ini"));
        if registry.is_file() {
            let directory = devices::ini_values(&registry)
                .get("path")
                .map(PathBuf::from)
                .filter(|p| p.is_absolute())
                .unwrap_or_else(|| home.join(format!("{name}.avd")));
            return Ok(Avd {
                name: name.into(),
                home,
                registry,
                directory,
            });
        }
    }
    Err(Error::new("avd_not_found", format!("Unknown AVD: {name}"))
        .remedy("List the AVDs with android.avds"))
}
fn exists(name: &str) -> Result<bool> {
    Ok(devices::avd_homes()?
        .iter()
        .any(|h| h.join(format!("{name}.ini")).exists() || h.join(format!("{name}.avd")).exists()))
}

/// Bytes of a file or a directory tree, not following symbolic links.
pub fn size(path: &Path) -> u64 {
    let Ok(meta) = fs::symlink_metadata(path) else {
        return 0;
    };
    if !meta.is_dir() {
        return meta.len();
    }
    fs::read_dir(path)
        .map(|entries| entries.flatten().map(|e| size(&e.path())).sum())
        .unwrap_or(0)
}
/// The emulator's lock files inside a running AVD (hardware-qemu.ini.lock,
/// multiinstance.lock, userdata-qemu.img.lock and the like).
pub fn locks(directory: &Path) -> Vec<String> {
    let mut found: Vec<String> = fs::read_dir(directory)
        .map(|entries| {
            entries
                .flatten()
                .map(|e| e.file_name().to_string_lossy().into_owned())
                .filter(|n| n.ends_with(".lock"))
                .collect()
        })
        .unwrap_or_default();
    found.sort();
    found
}
/// Refuses while Tomacco runs the emulator or its lock files say it may run.
pub fn not_running(avd: &Avd) -> Result<()> {
    if let Some(job) = jobs::active_exclusive(&format!("android-avd:{}", avd.name))? {
        return Err(Error::new("avd_in_use", format!("{} is running", avd.name))
            .remedy("Stop the emulator first, with android.emulator stop or in the desktop")
            .details(json!({"job":job})));
    }
    let held = locks(&avd.directory);
    if !held.is_empty() {
        return Err(Error::new(
            "avd_in_use",
            format!("{} may be running: its lock files exist", avd.name),
        )
        .remedy("Stop the emulator, including one started outside Tomacco. Lock files left by a crash go away when the emulator starts and stops cleanly once")
        .details(json!({"locks":held})));
    }
    Ok(())
}
fn confirmed(a: &Value, name: &str) -> Result<()> {
    if s(a, "confirm") != name {
        return Err(Error::new(
            "avd_confirmation_required",
            format!("Name the AVD again in confirm to {} {name}", s(a, "action")),
        )
        .remedy("Pass confirm with the exact AVD name after the user agreed"));
    }
    Ok(())
}

/// Space and state of every AVD, and what a wipe of each would free.
pub fn usage() -> Result<Value> {
    let mut values = Vec::new();
    let managed = home()?.join("avds");
    for home in devices::avd_homes()? {
        let Ok(entries) = fs::read_dir(&home) else {
            continue;
        };
        let mut registries: Vec<PathBuf> = entries
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.extension().is_some_and(|x| x == "ini"))
            .collect();
        registries.sort();
        for registry in registries {
            let name = registry
                .file_stem()
                .unwrap_or_default()
                .to_string_lossy()
                .into_owned();
            let Ok(avd) = find(&name) else { continue };
            if avd.registry != registry {
                continue;
            }
            let wipe: u64 = USER_DATA.iter().map(|f| size(&avd.directory.join(f))).sum();
            let running = jobs::active_exclusive(&format!("android-avd:{name}"))?.is_some();
            values.push(json!({"name":name,"path":avd.directory,"managed":home==managed,
                "bytes":size(&avd.directory),"wipe_bytes":wipe,"running":running,"locks":locks(&avd.directory)}));
        }
    }
    let partial: Vec<String> = fs::read_dir(&managed)
        .map(|e| {
            e.flatten()
                .map(|e| e.file_name().to_string_lossy().into_owned())
                .filter(|n| n.ends_with(PARTIAL))
                .collect()
        })
        .unwrap_or_default();
    Ok(
        json!({"avds":values,"free_bytes":crate::host::free_bytes(&managed).or_else(|| crate::host::free_bytes(&home().ok()?)),
        "wipe_removes":USER_DATA,"partial_copies":partial}),
    )
}

/// Removes the AVD's user data so the next boot starts like a new device.
pub fn wipe(a: &Value) -> Result<Value> {
    let avd = find(s(a, "name"))?;
    confirmed(a, &avd.name)?;
    let _lock = start_lock(&avd.name)?;
    not_running(&avd)?;
    let mut removed = Vec::new();
    let mut freed = 0;
    for entry in USER_DATA {
        let path = avd.directory.join(entry);
        let Ok(meta) = fs::symlink_metadata(&path) else {
            continue;
        };
        let bytes = size(&path);
        if meta.is_dir() {
            fs::remove_dir_all(&path)?;
        } else {
            fs::remove_file(&path)?;
        }
        freed += bytes;
        removed.push(json!({"file":entry,"bytes":bytes}));
    }
    Ok(
        json!({"name":avd.name,"action":"wipe","removed":removed,"freed_bytes":freed,
        "kept":["config.ini","sdcard.img"],"next_boot":"cold, like a new device"}),
    )
}

fn start_lock(name: &str) -> Result<Lock> {
    let lock = Lock::open(
        &home()?
            .join("locks")
            .join(format!("avd-start-{}.lock", digest(name.as_bytes()))),
    )?;
    if !lock.acquire(true)? {
        return Err(Error::new(
            "avd_in_use",
            format!("{name} is being started or changed"),
        ));
    }
    Ok(lock)
}

/// A real rename through `avdmanager move avd`; the nickname moves along.
pub fn rename(a: &Value) -> Result<Value> {
    let avd = find(s(a, "name"))?;
    let new = devices::avd_name(s(a, "new_name"))?.to_string();
    confirmed(a, &avd.name)?;
    if exists(&new)? {
        return Err(Error::new(
            "avd_exists",
            format!("An AVD named {new} exists"),
        ));
    }
    let _lock = start_lock(&avd.name)?;
    not_running(&avd)?;
    // avdmanager finds the AVD in the directory ANDROID_AVD_HOME names.
    let home_variable = format!("ANDROID_AVD_HOME={}", avd.home.display());
    let tool = tc::find("avdmanager")?;
    let args = argv(&[
        "env",
        &home_variable,
        &tool,
        "move",
        "avd",
        "--name",
        &avd.name,
        "--rename",
        &new,
    ]);
    tc::run(&args, 120, None, true)?;
    let renamed = find(&new).map_err(|_| {
        Error::new(
            "command_failed",
            format!("avdmanager did not rename {} to {new}", avd.name),
        )
    })?;
    if avd.registry.exists() {
        return Err(Error::new(
            "command_failed",
            format!("{} still exists after the rename", avd.name),
        ));
    }
    let nickname = devices::move_label(&format!("avd:{}", avd.name), &format!("avd:{new}"))?;
    Ok(
        json!({"action":"rename","from":avd.name,"to":new,"path":renamed.directory,"label_moved":nickname}),
    )
}

/// Starts a job that copies an AVD under a new name into Tomacco's AVD
/// directory. The copy appears only when complete: until the last step it
/// is a hidden `.partial` directory without a registry file.
pub fn duplicate(a: &Value) -> Result<Value> {
    let avd = find(s(a, "name"))?;
    let new = devices::avd_name(s(a, "new_name"))?.to_string();
    if exists(&new)? {
        return Err(Error::new(
            "avd_exists",
            format!("An AVD named {new} exists"),
        ));
    }
    not_running(&avd)?;
    let managed = home()?.join("avds");
    private_dir(&managed)?;
    let needed = size(&avd.directory) - size(&avd.directory.join("snapshots"));
    if let Some(free) = crate::host::free_bytes(&managed) {
        if free < needed + needed / 20 + (512 << 20) {
            return Err(Error::new(
                "avd_space",
                format!(
                    "Duplicating {} needs about {} MB; {} MB are free",
                    avd.name,
                    needed >> 20,
                    free >> 20
                ),
            )
            .details(json!({"needed_bytes":needed,"free_bytes":free})));
        }
    }
    let partial = managed.join(format!("{new}.avd{PARTIAL}"));
    let request = json!({"source":avd.directory,"source_registry":avd.registry,"name":new,"partial":partial,"managed":managed});
    let own = own_executable()?;
    jobs::submit(
        &format!("Duplicate {} as {new}", avd.name),
        json!({"steps":[
            {"argv":[own,"_avd_duplicate","prepare",request.to_string()]},
            {"argv":["cp","-a","--reflink=auto","--no-target-directory",avd.directory,partial]},
            {"argv":[own,"_avd_duplicate","finish",request.to_string()]}],
            "cwd":managed,"timeout":7200,"env":{},"exclusive":format!("android-avd:{}",avd.name),"single_active":true}),
    )
    .map(|mut job| {
        job["needed_bytes"] = json!(needed);
        job
    })
}

/// The duplicate job's own steps. `prepare` removes an earlier partial copy
/// of the same name; `finish` drops the copied lock files and snapshots (they
/// name the source's paths), renames the AVD inside, then publishes it.
pub fn duplicate_step(stage: &str, request: &str) -> Result<()> {
    let r: Value = serde_json::from_str(request)?;
    let name = devices::avd_name(s(&r, "name"))?.to_string();
    let managed = home()?.join("avds");
    let partial = PathBuf::from(s(&r, "partial"));
    if partial != managed.join(format!("{name}.avd{PARTIAL}")) {
        return Err(Error::new(
            "invalid_request",
            "The partial copy is outside Tomacco's AVD directory",
        ));
    }
    match stage {
        "prepare" => {
            if partial.exists() {
                fs::remove_dir_all(&partial)?;
            }
            Ok(())
        }
        "finish" => finish(&r, &name, &managed, &partial),
        _ => Err(Error::new("invalid_request", stage.to_string())),
    }
}
fn finish(r: &Value, name: &str, managed: &Path, partial: &Path) -> Result<()> {
    for lock in locks(partial) {
        let path = partial.join(lock);
        if path.is_dir() {
            fs::remove_dir_all(path)?;
        } else {
            fs::remove_file(path)?;
        }
    }
    if partial.join("snapshots").exists() {
        fs::remove_dir_all(partial.join("snapshots"))?;
    }
    let config = partial.join("config.ini");
    let text = fs::read_to_string(&config).unwrap_or_default();
    let mut lines: Vec<String> = text
        .lines()
        .filter(|l| {
            let key = l.split('=').next().unwrap_or("").trim();
            !matches!(key, "AvdId" | "avd.ini.displayname" | "tomacco.coldBoot")
        })
        .map(String::from)
        .collect();
    lines.push(format!("AvdId={name}"));
    lines.push(format!("avd.ini.displayname={}", name.replace('_', " ")));
    atomic_write(&config, (lines.join("\n") + "\n").as_bytes())?;
    let directory = managed.join(format!("{name}.avd"));
    if directory.exists() || managed.join(format!("{name}.ini")).exists() {
        return Err(Error::new(
            "avd_exists",
            format!("An AVD named {name} appeared meanwhile"),
        ));
    }
    fs::rename(partial, &directory)?;
    let source = devices::ini_values(Path::new(s(r, "source_registry")));
    let mut registry = format!(
        "avd.ini.encoding=UTF-8\npath={}\npath.rel=avd/{name}.avd\n",
        directory.display()
    );
    if let Some(target) = source.get("target") {
        registry.push_str(&format!("target={target}\n"));
    }
    atomic_write(&managed.join(format!("{name}.ini")), registry.as_bytes())
}

/// Typical download size of a system image by its tag. The SDK's package list
/// gives no sizes offline, so this is an estimate and the result says so.
fn image_estimate(system_image: &str) -> u64 {
    let tag = system_image.split(';').nth(2).unwrap_or("");
    let mb: u64 = if tag.contains("playstore") {
        1700
    } else if tag.contains("google_apis") {
        1400
    } else if tag.contains("wear") {
        700
    } else {
        900
    };
    mb << 20
}
/// Space a new AVD takes after its first boot, before apps: an estimate from
/// the emulator's default user data and cache images.
const AVD_ESTIMATE: u64 = 2 << 30;

/// What setting up an emulator from an image would do, without doing it: is
/// the image installed, how much would be downloaded and stored, are the
/// licences accepted, are the tools there, is the name free. Reads only files.
pub fn plan(a: &Value) -> Result<Value> {
    let name = devices::avd_name(s(a, "name"))?;
    let image = devices::system_image(s(a, "system_image"))?;
    let profile = catalog::find(s(a, "profile_id"))?;
    if s(&profile, "platform") != "android" {
        return Err(Error::new("invalid_profile", "Select an Android profile"));
    }
    let mut blockers = Vec::new();
    let form_factor = match devices::form_factor_supported(image) {
        Ok(()) => json!({"kind":devices::form_factor(image),"supported":true}),
        Err(e) => {
            blockers.push(e.code.to_string());
            json!({"kind":devices::form_factor(image),"supported":false,"reason":e.message,"remedy":e.remedy})
        }
    };
    if exists(name)? {
        blockers.push("avd_exists".into());
    }
    let sdk = tc::android_sdk()?;
    let image_installed = devices::installed_system_images()?
        .iter()
        .any(|i| i == image);
    let licenses_accepted = sdk
        .as_ref()
        .is_some_and(|p| p.join("licenses").join("android-sdk-license").is_file());
    let mut tools = serde_json::Map::new();
    for tool in ["sdkmanager", "avdmanager", "emulator", "adb"] {
        let found = tc::find_optional(tool)?;
        if found.is_none() && (tool != "sdkmanager" || !image_installed) {
            blockers.push(format!("{tool}_missing"));
        }
        tools.insert(tool.into(), json!(found));
    }
    if sdk.is_none() {
        blockers.push("sdk_missing".into());
    }
    let managed = home()?.join("avds");
    let free = crate::host::free_bytes(&managed).or_else(|| crate::host::free_bytes(&home().ok()?));
    let download = if image_installed {
        0
    } else {
        image_estimate(image)
    };
    let needed = download + AVD_ESTIMATE;
    if free.is_some_and(|f| f < needed) {
        blockers.push("avd_space".into());
    }
    blockers.sort();
    blockers.dedup();
    let start = a.get("start").filter(|v| v.is_object()).cloned();
    let steps = json!([
        {"step":"install_image","needed":!image_installed,"package":image,"estimate_bytes":download,
         "needs_license":!image_installed && !licenses_accepted,"cancellable":true},
        {"step":"create","needed":true,"name":name,"path":managed.join(format!("{name}.avd"))},
        {"step":"profile","needed":true,"profile_id":s(&profile,"id"),"profile_name":s(&profile,"name")},
        {"step":"start","needed":start.is_some(),"arguments":start}
    ]);
    Ok(json!({
        "name":name,"system_image":image,"profile_id":s(&profile,"id"),"profile_name":s(&profile,"name"),
        "form_factor":form_factor,"image_installed":image_installed,"licenses_accepted":licenses_accepted,
        "sdk":sdk,"tools":tools,"free_bytes":free,
        "estimate":{"download_bytes":download,"avd_bytes":AVD_ESTIMATE,"total_bytes":needed,
                    "source":"typical sizes; the SDK gives no exact size before the download"},
        "steps":steps,"blockers":blockers,"ready":blockers.is_empty()
    }))
}

/// Installs the image when missing, creates the AVD, applies the profile and,
/// when asked, starts it: one job whose steps the plan names, cancellable at
/// any step. A missing image is downloaded only with the licences accepted,
/// either already in the SDK or by `accept_licenses`.
pub fn setup(a: &Value) -> Result<Value> {
    let plan = plan(a)?;
    let name = s(&plan, "name").to_string();
    let image = s(&plan, "system_image").to_string();
    if !b(&plan, "ready") {
        let blockers = strings(&plan["blockers"]);
        return Err(Error::new(
            "avd_setup_blocked",
            format!("Setting up {name} cannot start: {}", blockers.join(", ")),
        )
        .remedy("android.avd_plan lists each blocker; fix them and plan again")
        .details(plan));
    }
    let install = !b(&plan, "image_installed");
    if install && !b(&plan, "licenses_accepted") && !b(a, "accept_licenses") {
        return Err(Error::new(
            "sdk_license_required",
            format!("{image} is not installed and Google's Android SDK licence is not accepted"),
        )
        .remedy("Ask the person, then call again with accept_licenses true; nothing was downloaded or created")
        .details(json!({"plan":plan})));
    }
    let admission = Lock::open(
        &home()?
            .join("locks")
            .join(format!("avd-create-{}.lock", digest(name.as_bytes()))),
    )?;
    admission.acquire(false)?;
    let managed = home()?.join("avds");
    private_dir(&managed)?;
    let target = managed.join(format!("{name}.avd"));
    let own = own_executable()?;
    let mut steps = Vec::new();
    if install {
        let mut args = vec![tc::find("sdkmanager")?];
        args.extend(devices::sdk_args()?);
        args.push("--install".into());
        args.push(image.clone());
        let mut step = json!({"argv":args,"label":"install_image"});
        if b(a, "accept_licenses") {
            step["input"] = json!("y\n".repeat(100));
        }
        steps.push(step);
    }
    steps.push(json!({"argv":[tc::find("avdmanager")?,"create","avd","--name",name,"--package",image,"--path",target],"input":"no\n","label":"create"}));
    steps.push(json!({"argv":[own,"call","android.avd_profile",json!({"name":name,"profile_id":s(a,"profile_id")}).to_string()],"label":"profile"}));
    if let Some(start) = a.get("start").filter(|v| v.is_object()) {
        let mut arguments = start.clone();
        arguments["name"] = json!(name);
        if arguments.get("headless").is_none() {
            arguments["headless"] = json!(true);
        }
        steps.push(
            json!({"argv":[own,"call","android.avd_start",arguments.to_string()],"label":"start"}),
        );
    }
    let mut env = serde_json::to_value(tc::environment()?)?;
    env["ANDROID_AVD_HOME"] = json!(managed);
    let mut job = jobs::submit(
        &format!("Set up {name}"),
        json!({"steps":steps,"timeout":if install {3600} else {300},"cwd":home()?,"env":env,
               "exclusive":format!("android-avd-create:{name}"),"single_active":true}),
    )?;
    job["plan"] = plan;
    Ok(job)
}

pub fn handle(a: &Value) -> Result<Value> {
    match s(a, "action") {
        "duplicate" => duplicate(a),
        "rename" => rename(a),
        "wipe" => wipe(a),
        other => Err(Error::new(
            "invalid_argument",
            format!("Unknown action {other}"),
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sizes_and_lock_files_are_read_without_following_links() {
        let temp = tempfile::tempdir().unwrap();
        let avd = temp.path().join("Pixel.avd");
        fs::create_dir_all(avd.join("snapshots/default_boot")).unwrap();
        fs::write(avd.join("userdata-qemu.img"), vec![0u8; 1000]).unwrap();
        fs::write(avd.join("snapshots/default_boot/ram.bin"), vec![0u8; 500]).unwrap();
        fs::write(temp.path().join("outside"), vec![0u8; 9000]).unwrap();
        std::os::unix::fs::symlink(temp.path().join("outside"), avd.join("link")).unwrap();
        assert_eq!(size(&avd.join("snapshots")), 500);
        assert!(size(&avd) < 2000, "a link is not followed");
        assert!(locks(&avd).is_empty());
        fs::create_dir(avd.join("hardware-qemu.ini.lock")).unwrap();
        fs::write(avd.join("multiinstance.lock"), b"").unwrap();
        assert_eq!(
            locks(&avd),
            ["hardware-qemu.ini.lock", "multiinstance.lock"]
        );
    }

    #[test]
    fn a_finished_copy_gets_its_own_identity_and_no_snapshots_or_locks() {
        let temp = tempfile::tempdir().unwrap();
        let managed = temp.path().join("avds");
        let source_registry = temp.path().join("Base.ini");
        fs::write(
            &source_registry,
            "path=/elsewhere/Base.avd
target=android-36
",
        )
        .unwrap();
        let partial = managed.join("Copy.avd.partial");
        fs::create_dir_all(partial.join("snapshots/default_boot")).unwrap();
        fs::write(partial.join("snapshots/default_boot/ram.bin"), b"x").unwrap();
        fs::write(partial.join("hardware-qemu.ini.lock"), b"").unwrap();
        fs::write(partial.join("userdata-qemu.img"), b"data").unwrap();
        fs::write(
            partial.join("config.ini"),
            "AvdId=Base
avd.ini.displayname=Base
hw.lcd.width=1080
tomacco.coldBoot=yes
",
        )
        .unwrap();
        let request = json!({"source_registry":source_registry});
        finish(&request, "Copy", &managed, &partial).unwrap();
        let copy = managed.join("Copy.avd");
        assert!(!partial.exists() && copy.is_dir());
        assert!(!copy.join("snapshots").exists() && locks(&copy).is_empty());
        assert!(copy.join("userdata-qemu.img").is_file());
        let config = devices::ini_values(&copy.join("config.ini"));
        assert_eq!(config["AvdId"], "Copy");
        assert_eq!(config["avd.ini.displayname"], "Copy");
        assert_eq!(config["hw.lcd.width"], "1080");
        assert!(
            !config.contains_key("tomacco.coldBoot"),
            "no stale cold-boot flag"
        );
        let registry = devices::ini_values(&managed.join("Copy.ini"));
        assert_eq!(registry["path"], copy.to_string_lossy());
        assert_eq!(registry["target"], "android-36");
        // A second finish into the same name refuses rather than replacing.
        fs::create_dir_all(&partial).unwrap();
        fs::write(partial.join("config.ini"), "").unwrap();
        assert_eq!(
            finish(&request, "Copy", &managed, &partial)
                .unwrap_err()
                .code,
            "avd_exists"
        );
    }

    #[test]
    fn download_estimates_are_labelled_by_tag() {
        assert!(
            image_estimate("system-images;android-36;google_apis_playstore;x86_64")
                > image_estimate("system-images;android-36;google_apis;x86_64")
        );
        assert!(
            image_estimate("system-images;android-36;google_apis;x86_64")
                > image_estimate("system-images;android-36;default;x86_64")
        );
        assert_eq!(
            image_estimate("system-images;android-36;android-wear;x86_64"),
            700 << 20
        );
    }

    #[test]
    fn a_destructive_action_needs_the_name_again() {
        assert_eq!(
            confirmed(&json!({"action":"wipe","confirm":"pixel"}), "Pixel")
                .unwrap_err()
                .code,
            "avd_confirmation_required"
        );
        assert!(confirmed(&json!({"action":"wipe","confirm":"Pixel"}), "Pixel").is_ok());
    }
}
