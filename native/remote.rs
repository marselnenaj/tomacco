use crate::{core::*, jobs, toolchain as tc};
use serde_json::{Value, json};
use std::{
    fs,
    path::{Component, Path},
};
pub fn listing() -> Result {
    let value = read_json(&home()?.join("remotes.json"), Some(json!({})))?;
    if !value.is_object() {
        return Err(Error::new(
            "invalid_settings",
            "remotes.json must be an object",
        ));
    }
    Ok(value)
}
pub fn workspace(name: &str) -> Result {
    listing()?.get(name).cloned().ok_or_else(|| {
        Error::new(
            "remote_not_found",
            format!("Unknown remote workspace: {name}"),
        )
    })
}
pub fn ssh(config: &Value, command: &str) -> Result<Vec<String>> {
    Ok(argv(&[
        &tc::find("ssh")?,
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "--",
        s(config, "host"),
        command,
    ]))
}
pub fn handle(op: &str, a: &Value) -> Result {
    let name = s(a, "name");
    if op == "remote.list" {
        return listing();
    }
    if op == "remote.configure" {
        identifier(name, "workspace", Some("[A-Za-z0-9_-]{1,40}"))?;
        identifier(
            s(a, "host"),
            "SSH host",
            Some("[A-Za-z0-9][A-Za-z0-9_.@:-]{0,200}"),
        )?;
        let directory = s(a, "directory");
        if !directory.starts_with('/') || directory.contains(['\n', '\0']) {
            return Err(Error::new(
                "invalid_path",
                "Specify the absolute workspace directory on the Mac",
            ));
        }
        let lock = Lock::open(&home()?.join("remotes.lock"))?;
        lock.acquire(false)?;
        let mut values = listing()?;
        values[name] = json!({"host":s(a,"host"),"directory":directory});
        atomic_json(&home()?.join("remotes.json"), &values)?;
        return Ok(json!({"name":name,"host":s(a,"host"),"directory":directory}));
    }
    let config = workspace(name)?;
    let directory = s(&config, "directory");
    let host = s(&config, "host");
    match op {
        "remote.check" => tc::run(
            &ssh(
                &config,
                "uname -s && xcodebuild -version && xcrun simctl list devices available --json",
            )?,
            30,
            None,
            true,
        ),
        "remote.run" => {
            let command = format!(
                "cd -- {} && exec {}",
                shell_quote(directory),
                shell_join(&strings(&a["argv"]))
            );
            jobs::submit(
                &format!("Mac · {name}"),
                json!({"steps":[{"argv":ssh(&config,&command)?}],"cwd":home()?,"timeout":n(a,"timeout"),"env":tc::environment()?}),
            )
        }
        "remote.sync" => {
            let rsync = tc::which("rsync").ok_or_else(|| {
                Error::new(
                    "tool_missing",
                    "Install rsync to synchronize Mac workspaces",
                )
            })?;
            let root = path(s(a, "path"), true, true)?;
            jobs::submit(
                &format!("Sync Mac · {name}"),
                json!({"steps":[{"argv":ssh(&config,&format!("mkdir -p -- {}",shell_quote(directory)))?},{"argv":[rsync,"-az","--protect-args","--exclude=.git","--exclude=.env*","--exclude=.gradle","--exclude=node_modules","--exclude=build","--exclude=.dart_tool","-e","ssh -o BatchMode=yes -o ConnectTimeout=10",format!("{}/",root.display()),format!("{host}:{directory}/")]}],"cwd":root,"timeout":900}),
            )
        }
        "remote.fetch" => {
            let rsync =
                tc::which("rsync").ok_or_else(|| Error::new("tool_missing", "Install rsync"))?;
            let remote = s(a, "remote_path");
            let relative = Path::new(remote);
            if relative.is_absolute()
                || relative.components().any(|c| c == Component::ParentDir)
                || remote.contains('\n')
            {
                return Err(Error::new(
                    "invalid_path",
                    "Select an artifact relative to the remote workspace",
                ));
            }
            let dest = output_path(s(a, "path"))?;
            fs::create_dir_all(dest.parent().unwrap())?;
            jobs::submit(
                "Download Mac artifact",
                json!({"steps":[{"argv":[rsync,"-a","--protect-args","-e","ssh -o BatchMode=yes -o ConnectTimeout=10",format!("{host}:{directory}/{remote}"),dest],"artifacts":[dest]}],"cwd":home()?,"timeout":900}),
            )
        }
        _ => Err(Error::new("operation_not_found", op)),
    }
}
