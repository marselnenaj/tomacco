//! The installer's behaviour, as tests/python/test_install.py specifies it for
//! install.py: the journal, rollback, the integrations, and what an
//! interruption at any point leaves.
use super::*;
use std::os::unix::fs::symlink;

const ROOT: &str = env!("CARGO_MANIFEST_DIR");

fn home() -> (tempfile::TempDir, PathBuf) {
    let directory = tempfile::tempdir().unwrap();
    let home = directory.path().canonicalize().unwrap();
    (directory, home)
}

fn installer(home: &Path) -> Installer {
    let mut installer = Installer::new(home, "9.9.9");
    installer.hooks = Hooks::default();
    installer
}

fn read(path: impl AsRef<Path>) -> String {
    fs::read_to_string(path).unwrap()
}

fn write_file(path: impl AsRef<Path>, text: &str) {
    let path = path.as_ref();
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, text).unwrap();
}

fn mode(path: impl AsRef<Path>) -> u32 {
    fs::metadata(path).unwrap().permissions().mode() & 0o777
}

fn backup_name(installer: &Installer) -> String {
    installer
        .backup
        .file_name()
        .unwrap()
        .to_string_lossy()
        .into_owned()
}

fn journal(installer: &Installer) -> Vec<Value> {
    let manifest: Value =
        serde_json::from_slice(&fs::read(installer.backup.join("manifest.json")).unwrap()).unwrap();
    manifest["files"].as_array().unwrap().clone()
}

fn staged(home: &Path) -> Vec<PathBuf> {
    let mut found = Vec::new();
    let mut directories = vec![home.to_path_buf()];
    while let Some(directory) = directories.pop() {
        for entry in fs::read_dir(&directory).unwrap().flatten() {
            let path = entry.path();
            if entry.file_name().to_string_lossy().starts_with(STAGED) {
                found.push(path.clone());
            }
            if fs::symlink_metadata(&path).unwrap().is_dir() {
                directories.push(path);
            }
        }
    }
    found
}

fn omarchy_source() -> PathBuf {
    Path::new(ROOT).join("integrations/omarchy")
}

/// Distinct backup directories for runs started within one microsecond.
fn next(home: &Path) -> Installer {
    std::thread::sleep(std::time::Duration::from_millis(2));
    installer(home)
}

#[test]
fn verified_rollback_and_conflicting_user_changes() {
    let (_directory, user) = home();
    let old = user.join("app");
    fs::write(&old, "old").unwrap();
    fs::set_permissions(&old, fs::Permissions::from_mode(0o755)).unwrap();
    let mut install = installer(&user);
    install.write("app", b"new", 0o755).unwrap();
    install.write("new-file", b"added", 0o644).unwrap();
    install.finish().unwrap();
    fs::write(&old, "user changes").unwrap();
    assert!(next(&user).rollback(&backup_name(&install)).is_err());
    assert_eq!(read(&old), "user changes");
    assert!(user.join("new-file").exists());
    fs::write(&old, "new").unwrap();
    let result = next(&user).rollback(&backup_name(&install)).unwrap();
    assert_eq!(result["changed"], 2);
    assert_eq!(read(&old), "old");
    assert_eq!(mode(&old), 0o755);
    assert!(!user.join("new-file").exists());
    // The rollback itself is reversible, including removed new files.
    let backups = PathBuf::from(result["backups"].as_str().unwrap());
    next(&user)
        .rollback(&backups.file_name().unwrap().to_string_lossy())
        .unwrap();
    assert_eq!(read(&old), "new");
    assert_eq!(read(user.join("new-file")), "added");
    for refused in ["../escape", "", ".", ".."] {
        assert!(next(&user).rollback(refused).is_err(), "{refused}");
    }
}

#[test]
fn backups_and_the_data_directory_are_owner_only() {
    let (_directory, user) = home();
    fs::write(user.join("app"), "old").unwrap();
    // SAFETY: umask only changes this process's file mode mask.
    let previous = unsafe { libc::umask(0) };
    let mut install = installer(&user);
    let result = install.write("app", b"new", 0o644);
    // SAFETY: as above.
    unsafe { libc::umask(previous) };
    result.unwrap();
    install.finish().unwrap();
    for path in [
        user.join(".local/share/tomacco"),
        user.join(".local/share/tomacco/install-backups"),
        install.backup.clone(),
    ] {
        assert_eq!(mode(&path), 0o700, "{}", path.display());
    }
    // One an earlier version left open is tightened by the next install.
    fs::set_permissions(
        user.join(".local/share/tomacco"),
        fs::Permissions::from_mode(0o755),
    )
    .unwrap();
    let mut again = next(&user);
    again.write("app", b"newer", 0o644).unwrap();
    again.finish().unwrap();
    assert_eq!(mode(user.join(".local/share/tomacco")), 0o700);
}

#[test]
fn the_write_ahead_journal_handles_an_unpublished_write() {
    let (_directory, user) = home();
    fs::write(user.join("app"), "old").unwrap();
    let mut install = installer(&user);
    let app = user.join("app");
    install.hooks.before_publish = Some(Box::new(move |path| {
        if path == app {
            Err(io::Error::other(
                "simulated interruption before publication",
            ))
        } else {
            Ok(())
        }
    }));
    assert!(install.write("app", b"new", 0o644).is_err());
    assert!(install.backup.join("manifest.json").is_file());
    assert_eq!(staged(&user), Vec::<PathBuf>::new());
    let result = next(&user).rollback(&backup_name(&install)).unwrap();
    assert_eq!(result["changed"], 0);
    assert_eq!(read(user.join("app")), "old");
}

#[test]
fn omarchy_preserves_the_layout_and_is_idempotent() {
    let (_directory, user) = home();
    let path = user.join(".config/omarchy/shell.json");
    let value = json!({"bar": {"position": "left", "layout": {"left": [{"id": "custom.one"}],
        "right": [{"id": "custom.two", "setting": 42}]}}, "idle": {"lock": 123},
        "disabledPlugins": ["custom.off", APP_ID]});
    write_file(&path, &value.to_string());
    let before = fs::read(&path).unwrap();
    let mut install = installer(&user);
    install.omarchy(&omarchy_source(), true).unwrap();
    let result: Value = serde_json::from_str(&read(&path)).unwrap();
    assert_eq!(result["idle"], value["idle"]);
    assert_eq!(
        result["bar"]["layout"]["left"],
        value["bar"]["layout"]["left"]
    );
    assert_eq!(result["bar"]["layout"]["right"][0], json!({"id": APP_ID}));
    assert_eq!(
        result["bar"]["layout"]["right"][1],
        value["bar"]["layout"]["right"][0]
    );
    assert_eq!(result["disabledPlugins"], json!(["custom.off"]));
    assert_eq!(
        fs::read(install.backup.join(".config/omarchy/shell.json")).unwrap(),
        before
    );
    let mut again = next(&user);
    again.omarchy(&omarchy_source(), true).unwrap();
    assert_eq!(again.changed, Vec::<PathBuf>::new());
}

#[test]
fn omarchy_keeps_a_disabled_widget_disabled_on_an_update() {
    let (_directory, user) = home();
    let path = user.join(".config/omarchy/shell.json");
    write_file(
        &path,
        &json!({"bar": {"layout": {"left": [{"id": APP_ID, "notifyBuilds": false}]}},
            "disabledPlugins": [APP_ID]})
        .to_string(),
    );
    let mut install = installer(&user);
    assert!(install.widget_placed());
    install.omarchy(&omarchy_source(), false).unwrap();
    let result: Value = serde_json::from_str(&read(&path)).unwrap();
    assert_eq!(result["disabledPlugins"], json!([APP_ID]));
    assert_eq!(
        result["bar"]["layout"]["left"],
        json!([{"id": APP_ID, "notifyBuilds": false}])
    );
}

#[test]
fn the_omarchy_widget_sits_beside_the_bars_agents_widget_whoever_provides_it() {
    let cases = [
        (
            json!([{"id": "omarchy.tray"}, {"id": "omarchy.agents"}, {"id": "omarchy.audio"}]),
            2,
        ),
        (
            json!([{"id": "omarchy.tray"}, {"id": "someone.agents", "setting": 1}, "omarchy.audio"]),
            2,
        ),
        (json!(["omarchy.tray", "omarchy.agents"]), 2),
        (
            json!([{"id": "omarchy.tray"}, {"id": "someone.subagents"}]),
            0,
        ),
        (json!([]), 0),
    ];
    for (right, expected) in cases {
        let (_directory, user) = home();
        let path = user.join(".config/omarchy/shell.json");
        write_file(
            &path,
            &json!({"bar": {"layout": {"right": right}}}).to_string(),
        );
        installer(&user).omarchy(&omarchy_source(), true).unwrap();
        let mut wanted = right.as_array().unwrap().clone();
        wanted.insert(expected, json!({"id": APP_ID}));
        let result: Value = serde_json::from_str(&read(&path)).unwrap();
        assert_eq!(result["bar"]["layout"]["right"], Value::Array(wanted));
    }
}

#[test]
fn the_omarchy_widget_entry_is_content_addressed_and_replaces_older_copies() {
    let (_directory, user) = home();
    write_file(
        user.join(".config/omarchy/shell.json"),
        r#"{"bar": {"layout": {"right": []}}}"#,
    );
    let plugin = user.join(PLUGIN);
    write_file(plugin.join("MobilePanel.qml"), "old widget");
    write_file(plugin.join("0123456789ab/MobilePanel.qml"), "older build");
    write_file(plugin.join("notes/MobilePanel.qml"), "not ours");
    write_file(plugin.join("Panel.qml"), "kept");
    let own = [
        ("MobilePanelLocal.qml", "my own variant"),
        ("MobilePanel2.qml", "my copy"),
        ("MobilePanel.0123456789ab.qml", "my backup"),
    ];
    for (name, content) in own {
        write_file(plugin.join(name), content);
    }
    let mut install = installer(&user);
    install.omarchy(&omarchy_source(), true).unwrap();
    let manifest: Value = serde_json::from_str(&read(plugin.join("manifest.json"))).unwrap();
    let entry = manifest["entryPoints"]["barWidget"]
        .as_str()
        .unwrap()
        .to_string();
    let build = entry.split('/').next().unwrap().to_string();
    assert_eq!(build.len(), 12);
    assert!(entry.ends_with("/MobilePanel.qml"));
    assert!(!plugin.join("MobilePanel.qml").exists());
    assert!(!plugin.join("0123456789ab").exists());
    assert_eq!(read(plugin.join("notes/MobilePanel.qml")), "not ours");
    for (name, content) in own {
        assert_eq!(read(plugin.join(name)), content);
    }
    let text = read(plugin.join(&entry));
    assert!(!text.contains("@TOMACCO_PANEL_BUILD@"));
    assert!(text.contains(&format!("build: \"{build}\"")));
    assert_eq!(read(plugin.join("Panel.qml")), "kept");
    // The manifest keeps the source's key order, written as install.py writes it.
    let source = read(omarchy_source().join("manifest.json"));
    let mut expected: Value = serde_json::from_str(&source).unwrap();
    expected["entryPoints"]["barWidget"] = json!(entry);
    assert_eq!(
        read(plugin.join("manifest.json")),
        serde_json::to_string_pretty(&expected).unwrap() + "\n"
    );
    let mut again = next(&user);
    again.omarchy(&omarchy_source(), true).unwrap();
    assert_eq!(again.changed, Vec::<PathBuf>::new());
    install.finish().unwrap();
    next(&user).rollback(&backup_name(&install)).unwrap();
    assert_eq!(read(plugin.join("MobilePanel.qml")), "old widget");
    assert_eq!(
        read(plugin.join("0123456789ab/MobilePanel.qml")),
        "older build"
    );
    assert!(!plugin.join(&entry).exists());
}

#[test]
fn window_rules_are_installed_once_and_undone_in_place() {
    let (_directory, user) = home();
    // Without a Hyprland configuration nothing is written.
    let mut install = installer(&user);
    install.window_rule().unwrap();
    assert!(install.changed.is_empty());
    assert!(!user.join(".config/hypr").exists());
    let config = user.join(".config/hypr/hyprland.lua");
    write_file(&config, "require(\"hypr.looknfeel\")\n");
    let mut install = next(&user);
    install.window_rule().unwrap();
    assert_eq!(read(user.join(".config/hypr/tomacco.lua")), HYPR_RULES);
    assert_eq!(
        read(&config),
        "require(\"hypr.looknfeel\")\n\n-- Tomacco mobile workspace.\nrequire(\"hypr.tomacco\")\n"
    );
    assert_eq!(
        read(install.backup.join(".config/hypr/hyprland.lua")),
        "require(\"hypr.looknfeel\")\n"
    );
    let mut again = next(&user);
    again.window_rule().unwrap();
    assert!(again.changed.is_empty());
    let mut undo = next(&user);
    undo.unedit_window_rule().unwrap();
    assert_eq!(read(&config), "require(\"hypr.looknfeel\")\n");
}

#[test]
fn existing_commands_and_symlinks_are_preserved() {
    let (_directory, user) = home();
    write_file(
        user.join(".local/bin/tomacco"),
        "#!/bin/sh\necho another application\n",
    );
    assert!(installer(&user).launchers(false).is_err());
    let destination = user.join("destination");
    fs::write(&destination, "preserved").unwrap();
    symlink(&destination, user.join("link")).unwrap();
    assert!(installer(&user).write("link", b"changed", 0o644).is_err());
    assert_eq!(read(&destination), "preserved");
    // Nor through a linked directory out of the home.
    let outside = tempfile::tempdir().unwrap();
    symlink(outside.path(), user.join("elsewhere")).unwrap();
    assert!(
        installer(&user)
            .write("elsewhere/file", b"x", 0o644)
            .is_err()
    );
    assert!(!outside.path().join("file").exists());
    for refused in ["/etc/passwd", "../x", ""] {
        assert!(installer(&user).write(refused, b"x", 0o644).is_err());
    }
}

#[test]
fn launchers_name_the_program_and_unset_qt_paths_only_for_a_portable_desktop() {
    let target = Path::new("/home/a b/.local/lib/tomacco");
    assert_eq!(
        launcher_script("tomacco", target, true),
        "#!/bin/sh\n# Managed by tomacco/install.py\nexport QT_QPA_PLATFORMTHEME=generic\nexport QT_QUICK_CONTROLS_STYLE=Basic\n\
         unset QT_PLUGIN_PATH QML_IMPORT_PATH QML2_IMPORT_PATH\nexec '/home/a b/.local/lib/tomacco/tomacco' \"$@\"\n"
    );
    assert_eq!(
        launcher_script("tomaccoctl", target, true),
        "#!/bin/sh\n# Managed by tomacco/install.py\nexec '/home/a b/.local/lib/tomacco/tomaccoctl' \"$@\"\n"
    );
}

#[test]
fn the_desktop_entry_quotes_the_launcher() {
    let template = "[Desktop Entry]\nExec=tomacco %U\nTryExec=tomacco\n";
    assert_eq!(
        desktop_entry(template, "/home/x$y \"q\"/.local/bin/tomacco"),
        "[Desktop Entry]\nExec=\"/home/x\\$y \\\"q\\\"/.local/bin/tomacco\" %U\nTryExec=tomacco\n"
    );
}

#[test]
fn uninstall_skips_files_it_does_not_own() {
    let (_directory, user) = home();
    let mut install = installer(&user);
    // A journalled write outside the installation's own places, as a future installer might record.
    install
        .write_text(".config/someone-else.conf", "theirs")
        .unwrap();
    install
        .write_text(".local/share/tomacco/settings.json", "{}")
        .unwrap();
    install
        .write_text(".local/lib/tomacco/VERSION", "1\n")
        .unwrap();
    install.finish().unwrap();
    let link_target = user.join("elsewhere");
    fs::write(&link_target, "not ours").unwrap();
    fs::create_dir_all(user.join(".local/bin")).unwrap();
    symlink(&link_target, user.join(".local/bin/tomaccoctl")).unwrap();
    let mut install = next(&user);
    install
        .records
        .push(json!({"path": ".local/bin/tomaccoctl", "before": null,
        "after": {"sha256": "0".repeat(64), "mode": 0o755}}));
    install.finish().unwrap();
    let result = next(&user).uninstall().unwrap();
    assert_eq!(result["removed"], 1);
    assert_eq!(
        result["kept"],
        json!([user.join(".local/bin/tomaccoctl").to_string_lossy()])
    );
    assert_eq!(read(user.join(".config/someone-else.conf")), "theirs");
    assert_eq!(read(user.join(".local/share/tomacco/settings.json")), "{}");
    assert_eq!(read(&link_target), "not ours");
    assert!(!user.join(".local/lib/tomacco").exists());
}

const FILES: usize = 7;

fn library(index: usize) -> String {
    format!(".local/lib/tomacco/runtime/lib/lib{index}.so")
}

#[test]
fn a_batch_is_journalled_and_made_durable_once_before_it_is_published() {
    let (_directory, user) = home();
    write_file(user.join(library(0)), "old");
    let mut install = installer(&user);
    install.hooks.batch = 3;
    install.hooks.trace = Some(Vec::new());
    install
        .batch(|install| {
            for index in 0..FILES {
                install.write_text(library(index), &format!("library {index}"))?;
            }
            Ok(())
        })
        .unwrap();
    install.finish().unwrap();
    let events = install.hooks.trace.take().unwrap();
    // Three batches of at most three: not one journal per file.
    let journals: Vec<usize> = events
        .iter()
        .filter_map(|event| match event {
            Event::Journal(size) => Some(*size),
            _ => None,
        })
        .collect();
    assert_eq!(journals, [3, 6, 7]);
    // Write-ahead: a file is published only after a journal naming it was
    // written and made durable together with the staged files.
    let (mut journalled, mut durable, mut published) = (0, false, 0);
    for event in &events {
        match event {
            Event::Journal(size) => {
                journalled = *size;
                durable = false;
            }
            Event::Durable(paths) if paths.iter().any(|path| path.ends_with("manifest.json")) => {
                durable = true;
            }
            Event::Publish(path) => {
                let relative = path.strip_prefix(&user).unwrap().to_string_lossy();
                let index = install
                    .records
                    .iter()
                    .position(|record| s(record, "path") == relative)
                    .unwrap();
                assert!(durable && index < journalled, "{relative} published early");
                published += 1;
            }
            _ => (),
        }
    }
    assert_eq!(published, FILES);
    for index in 0..FILES {
        assert_eq!(read(user.join(library(index))), format!("library {index}"));
    }
    // The old content is kept in the backup.
    assert_eq!(read(install.backup.join(library(0))), "old");
    assert!(staged(&user).is_empty());
}

#[test]
fn an_interruption_inside_a_batch_can_be_rolled_back() {
    let (_directory, user) = home();
    for index in 0..FILES {
        write_file(user.join(library(index)), &format!("old {index}"));
    }
    let mut install = installer(&user);
    install.hooks.batch = 3;
    let mut count = 0;
    install.hooks.before_publish = Some(Box::new(move |_| {
        count += 1;
        if count == 5 {
            Err(io::Error::other(
                "simulated interruption in the middle of a batch",
            ))
        } else {
            Ok(())
        }
    }));
    let result = install.batch(|install| {
        for index in 0..FILES {
            install.write_text(library(index), &format!("library {index}"))?;
        }
        Ok(())
    });
    assert!(result.is_err());
    // The second batch was journalled whole; its unpublished files were never changed.
    assert_eq!(journal(&install).len(), 6);
    assert!(staged(&user).is_empty());
    let texts: Vec<String> = (0..FILES)
        .map(|index| read(user.join(library(index))))
        .collect();
    assert_eq!(
        texts,
        [
            "library 0",
            "library 1",
            "library 2",
            "library 3",
            "old 4",
            "old 5",
            "old 6"
        ]
    );
    let result = next(&user).rollback(&backup_name(&install)).unwrap();
    assert_eq!(result["changed"], 4);
    let texts: Vec<String> = (0..FILES)
        .map(|index| read(user.join(library(index))))
        .collect();
    assert_eq!(
        texts,
        (0..FILES).map(|i| format!("old {i}")).collect::<Vec<_>>()
    );
}

#[test]
fn an_interrupted_uninstallation_finishes_when_run_again() {
    let (_directory, user) = home();
    let mut install = installer(&user);
    install
        .batch(|install| {
            install.launchers(false)?;
            for index in 0..FILES {
                install.write_text(library(index), "x")?;
            }
            Ok(())
        })
        .unwrap();
    install.finish().unwrap();
    let mut uninstall = next(&user);
    uninstall.hooks.batch = 4;
    let mut count = 0;
    uninstall.hooks.before_publish = Some(Box::new(move |_| {
        count += 1;
        if count == 6 {
            Err(io::Error::other("killed"))
        } else {
            Ok(())
        }
    }));
    assert!(uninstall.uninstall().is_err());
    let result = next(&user).uninstall().unwrap();
    assert_eq!(result["kept"], json!([]));
    assert!(!user.join(".local/lib/tomacco").exists());
    assert!(!user.join(".local/bin/tomaccoctl").exists());
}

#[test]
fn the_backup_survives_when_hard_links_are_refused() {
    let (_directory, user) = home();
    fs::write(user.join("app"), "old").unwrap();
    let mut install = installer(&user);
    install.hooks.link = false;
    install.write_text("app", "new").unwrap();
    install.finish().unwrap();
    let saved = install.backup.join("app");
    assert_eq!(read(&saved), "old");
    assert_eq!(fs::metadata(&saved).unwrap().nlink(), 1);
    assert_eq!(
        next(&user).rollback(&backup_name(&install)).unwrap()["changed"],
        1
    );
    assert_eq!(read(user.join("app")), "old");
}

#[test]
fn without_syncfs_every_file_is_flushed_on_its_own() {
    let (_directory, user) = home();
    let mut install = installer(&user);
    install.hooks.syncfs = false;
    install.hooks.trace = Some(Vec::new());
    install
        .batch(|install| {
            for index in 0..FILES {
                install.write_text(library(index), &format!("library {index}"))?;
            }
            Ok(())
        })
        .unwrap();
    install.finish().unwrap();
    let synced: usize = install
        .hooks
        .trace
        .take()
        .unwrap()
        .iter()
        .map(|event| match event {
            Event::Fsyncs(count) => *count,
            _ => 0,
        })
        .sum();
    // Seven staged files, the manifest and the backup directory, then the destination directory.
    assert_eq!(synced, 9 + 1);
    assert_eq!(journal(&install).len(), FILES);
}

#[test]
fn a_staged_file_a_killed_run_left_is_swept_by_the_next_write_there() {
    let (_directory, user) = home();
    write_file(user.join(".local/lib/tomacco/.tomacco-staged-abc"), "left");
    write_file(user.join(".local/lib/tomacco/keep"), "kept");
    let mut install = installer(&user);
    install
        .write_text(".local/lib/tomacco/VERSION", "1\n")
        .unwrap();
    assert!(staged(&user).is_empty());
    assert_eq!(read(user.join(".local/lib/tomacco/keep")), "kept");
}

#[test]
fn the_skill_links_are_journalled_and_undone_with_the_directories_they_needed() {
    let (_directory, user) = home();
    let source = Path::new(ROOT).join("integrations/agent-skill/tomacco");
    fs::create_dir_all(user.join(".codex")).unwrap();
    write_file(user.join(".agents/skills/tomacco/SKILL.md"), "theirs");
    let mut install = installer(&user);
    let result = install.agent_skill(&source, true, false).unwrap();
    let link = user.join(".codex/skills/tomacco");
    assert_eq!(
        result,
        json!({"linked": [link.to_string_lossy()],
            "skipped": [user.join(".agents/skills/tomacco").to_string_lossy()]})
    );
    assert_eq!(fs::read_link(&link).unwrap(), user.join(SKILL));
    let records = journal(&install);
    let entry = records
        .iter()
        .find(|record| record["path"] == ".codex/skills/tomacco")
        .unwrap();
    assert_eq!(
        entry,
        &json!({"path": ".codex/skills/tomacco", "before": null,
            "after": {"symlink": user.join(SKILL).to_string_lossy()}, "created": [".codex/skills"]})
    );
    install.finish().unwrap();
    let again = next(&user).agent_skill(&source, true, false).unwrap();
    assert_eq!(again["linked"], json!([link.to_string_lossy()]));
    next(&user).rollback(&backup_name(&install)).unwrap();
    assert!(fs::symlink_metadata(&link).is_err());
    assert!(!user.join(".codex/skills").exists());
    assert!(user.join(".codex").is_dir());
    // A rollback restores files, not directories, as install.py's does.
    assert!(!user.join(SKILL).join("SKILL.md").exists());
    assert_eq!(read(user.join(".agents/skills/tomacco/SKILL.md")), "theirs");
}

#[test]
fn json_is_written_as_install_py_writes_it() {
    let value = json!({"a": [], "b": {}, "c": "ü😀", "d": [1, {"e": null}]});
    assert_eq!(
        ascii_json(&value),
        "{\n  \"a\": [],\n  \"b\": {},\n  \"c\": \"\\u00fc\\ud83d\\ude00\",\n  \"d\": [\n    1,\n    {\n      \"e\": null\n    }\n  ]\n}"
    );
    assert_eq!(config_json(&json!({"c": "ü"})), "{\n  \"c\": \"ü\"\n}\n");
    assert_eq!(split_lines("a\r\nb\rc\n\nd"), ["a", "b", "c", "", "d"]);
    assert_eq!(normal(Path::new("./a//b/")), "a/b");
}

#[test]
fn the_command_line_reads_as_argparse_reads_it() {
    let args = |line: &[&str]| parse(&line.iter().map(|s| s.to_string()).collect::<Vec<_>>());
    let parsed = args(&["--user-home=/h", "--omar", "--rollback", "x", "--no-agent"]).unwrap();
    assert_eq!(parsed.user_home, Some(PathBuf::from("/h")));
    assert!(parsed.omarchy && parsed.no_agent_skill);
    assert_eq!(parsed.rollback.as_deref(), Some("x"));
    assert!(args(&["--no"]).is_err(), "ambiguous");
    assert!(args(&["--rollback"]).is_err());
    assert!(args(&["--frobnicate"]).is_err());
    assert!(args(&["stray"]).is_err());
    assert!(args(&["--omarchy=1"]).is_err());
    // --package is exact as written, and not taken for --package-docs.
    let parsed = args(&["--package", "/p"]).unwrap();
    assert_eq!(parsed.package, Some(PathBuf::from("/p")));
    assert!(!parsed.package_docs);
}
