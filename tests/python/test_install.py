import json
import re
import tempfile
import unittest
from pathlib import Path

from install import APP_ID, Installer

ROOT = Path(__file__).resolve().parents[2]


class InstallerTests(unittest.TestCase):
    def test_verified_rollback_and_conflicting_user_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            user=Path(directory)
            old=user/'app';old.write_text('old');old.chmod(0o755)
            install=Installer(user);install.write(Path('app'),'new',0o755);install.write(Path('new-file'),'added');install.finish()
            old.write_text('user changes')
            with self.assertRaises(SystemExit):Installer(user).rollback(install.backup.name)
            self.assertEqual(old.read_text(),'user changes')
            self.assertTrue((user/'new-file').exists())
            old.write_text('new')
            result=Installer(user).rollback(install.backup.name)
            self.assertEqual(result['changed'],2)
            self.assertEqual(old.read_text(),'old')
            self.assertEqual(old.stat().st_mode & 0o777,0o755)
            self.assertFalse((user/'new-file').exists())
            # The rollback itself is reversible, including removed new files.
            Installer(user).rollback(Path(result['backups']).name)
            self.assertEqual(old.read_text(),'new')
            self.assertEqual((user/'new-file').read_text(),'added')
            with self.assertRaises(SystemExit):Installer(user).rollback('../escape')

    def test_backups_and_the_data_directory_are_owner_only(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            user=Path(directory);(user/'app').write_text('old')
            previous=os.umask(0)
            try:
                install=Installer(user);install.write(Path('app'),'new');install.finish()
            finally:
                os.umask(previous)
            for path in (user/'.local/share/tomacco',user/'.local/share/tomacco/install-backups',install.backup):
                self.assertEqual(path.stat().st_mode & 0o777,0o700,path)
            # One an earlier version left open is tightened by the next install.
            (user/'.local/share/tomacco').chmod(0o755)
            again=Installer(user);again.write(Path('app'),'newer');again.finish()
            self.assertEqual((user/'.local/share/tomacco').stat().st_mode & 0o777,0o700)

    def test_write_ahead_journal_handles_an_unpublished_write(self):
        from unittest.mock import patch
        import os
        with tempfile.TemporaryDirectory() as directory:
            user=Path(directory);(user/'app').write_text('old')
            installer=Installer(user);original=os.replace
            def interrupt(source,target):
                if Path(target)==user/'app':raise OSError('simulated interruption before publication')
                return original(source,target)
            with patch('install.os.replace',side_effect=interrupt):
                with self.assertRaises(OSError):installer.write(Path('app'),'new')
            self.assertTrue((installer.backup/'manifest.json').is_file())
            result=Installer(user).rollback(installer.backup.name)
            self.assertEqual(result['changed'],0)
            self.assertEqual((user/'app').read_text(),'old')

    def test_omarchy_preserves_layout_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            user = Path(directory)
            path = user / ".config/omarchy/shell.json"
            path.parent.mkdir(parents=True)
            value = {"bar": {"position": "left", "layout": {"left": [{"id": "custom.one"}], "right": [{"id": "custom.two", "setting": 42}]}}, "idle": {"lock": 123}, "disabledPlugins": ["custom.off"]}
            path.write_text(json.dumps(value))
            before = path.read_bytes()
            install = Installer(user)
            install.omarchy()
            result = json.loads(path.read_text())
            self.assertEqual(result["idle"], value["idle"])
            self.assertEqual(result["bar"]["layout"]["left"], value["bar"]["layout"]["left"])
            self.assertEqual(result["bar"]["layout"]["right"][1], value["bar"]["layout"]["right"][0])
            self.assertEqual((install.backup / ".config/omarchy/shell.json").read_bytes(), before)
            again = Installer(user)
            again.omarchy()
            self.assertEqual(again.changed, [])
            self.assertEqual(sum(p["id"] == APP_ID for p in result["bar"]["layout"]["right"]), 1)

    def test_omarchy_widget_sits_beside_the_bars_agents_widget_whoever_provides_it(self):
        for right, expected in (
                ([{"id": "omarchy.tray"}, {"id": "omarchy.agents"}, {"id": "omarchy.audio"}], 2),
                ([{"id": "omarchy.tray"}, {"id": "someone.agents", "setting": 1}, "omarchy.audio"], 2),
                (["omarchy.tray", "omarchy.agents"], 2),
                ([{"id": "omarchy.tray"}, {"id": "someone.subagents"}], 0),
                ([], 0)):
            with self.subTest(right=right), tempfile.TemporaryDirectory() as directory:
                user = Path(directory)
                path = user / ".config/omarchy/shell.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"bar": {"layout": {"right": right}}}))
                Installer(user).omarchy()
                self.assertEqual(json.loads(path.read_text())["bar"]["layout"]["right"],
                                 right[:expected] + [{"id": APP_ID}] + right[expected:])

    def test_omarchy_widget_entry_is_content_addressed_and_replaces_older_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            user = Path(directory)
            (user / ".config/omarchy").mkdir(parents=True)
            (user / ".config/omarchy/shell.json").write_text(json.dumps({"bar": {"layout": {"right": []}}}))
            plugin = user / ".config/omarchy/plugins" / APP_ID
            (plugin / "0123456789ab").mkdir(parents=True)
            (plugin / "notes").mkdir()
            # Earlier installations: the shell still has these URLs compiled.
            (plugin / "MobilePanel.qml").write_text("old widget")
            (plugin / "0123456789ab/MobilePanel.qml").write_text("older build")
            (plugin / "notes/MobilePanel.qml").write_text("not ours")
            (plugin / "Panel.qml").write_text("kept")
            # Files of the user's own that merely start like the widget's name.
            user_files = {"MobilePanelLocal.qml": "my own variant", "MobilePanel2.qml": "my copy",
                            "MobilePanel.0123456789ab.qml": "my backup"}
            for name, content in user_files.items():
                (plugin / name).write_text(content)
            install = Installer(user)
            install.omarchy()
            manifest = json.loads((plugin / "manifest.json").read_text())
            entry = manifest["entryPoints"]["barWidget"]
            self.assertRegex(entry, r"^[0-9a-f]{12}/MobilePanel[.]qml$")
            build = entry.split("/")[0]
            self.assertEqual(sorted(str(p.relative_to(plugin)) for p in plugin.rglob("MobilePanel*.qml")),
                             sorted([entry, "notes/MobilePanel.qml", *user_files]))
            for name, content in user_files.items():
                self.assertEqual((plugin / name).read_text(), content)
            self.assertFalse((plugin / "MobilePanel.qml").exists())
            self.assertFalse((plugin / "0123456789ab").exists())
            text = (plugin / entry).read_text()
            self.assertNotIn("@TOMACCO_PANEL_BUILD@", text)
            self.assertIn('build: "' + build + '"', text)
            self.assertEqual((plugin / "Panel.qml").read_text(), "kept")
            again = Installer(user)
            again.omarchy()
            self.assertEqual(again.changed, [])
            install.finish()
            Installer(user).rollback(install.backup.name)
            self.assertEqual((plugin / "MobilePanel.qml").read_text(), "old widget")
            self.assertEqual((plugin / "0123456789ab/MobilePanel.qml").read_text(), "older build")
            self.assertFalse((plugin / entry).exists())
            for name, content in user_files.items():
                self.assertEqual((plugin / name).read_text(), content)

    def test_window_rules_separate_the_device_window_and_are_installed_once(self):
        import re
        from install import HYPR_RULES
        # Hyprland matches a rule's patterns against the whole string. The main
        # window and the frameless device window share one class and are told
        # apart by title only, so each rule must claim exactly its own window.
        rules = re.findall(r'o[.]window[(][{] class = "([^"]+)", title = "([^"]+)" [}], [{](.*?)\n[}][)]', HYPR_RULES, re.S)
        self.assertEqual(len(rules), 2)
        def matching(app_class, title):
            found = []
            for pattern, title_pattern, body in rules:
                negative = title_pattern.startswith("negative:")
                hit = re.fullmatch(title_pattern.removeprefix("negative:"), title) is not None
                if re.fullmatch(pattern, app_class) and hit != negative:
                    found.append(body)
            return found
        main = matching(APP_ID, "Tomacco")
        device = matching(APP_ID, "Tomacco – Pixel 11")
        phone = matching(APP_ID, "Tomacco – Virtuelles iOS-Gerät")
        self.assertEqual((len(main), len(device), len(phone)), (1, 1, 1))
        self.assertIn("size = { 1380, 970 }", main[0])
        # The app draws the device itself: nothing from the compositor around
        # it, and no size the compositor could force on it.
        for body in device + phone:
            self.assertIsNone(re.search(r"^\s*size\s*=", body, re.M))
            for setting in ("float = true", "border_size = 0", "rounding = 0", "no_shadow = true", "no_blur = true"):
                self.assertIn(setting, body)
        self.assertEqual(matching("org.example.other", "Tomacco"), [])
        self.assertEqual(matching(APP_ID + ".helper", "Tomacco"), [])
        with tempfile.TemporaryDirectory() as directory:
            user = Path(directory)
            # Without a Hyprland configuration nothing is written.
            install = Installer(user)
            install.window_rule()
            self.assertEqual(install.changed, [])
            self.assertFalse((user / ".config/hypr").exists())
            config = user / ".config/hypr/hyprland.lua"
            config.parent.mkdir(parents=True)
            config.write_text('require("hypr.looknfeel")\n')
            install = Installer(user)
            install.window_rule()
            self.assertEqual((user / ".config/hypr/tomacco.lua").read_text(), HYPR_RULES)
            text = config.read_text()
            self.assertTrue(text.startswith('require("hypr.looknfeel")\n'))
            self.assertEqual(text.count('require("hypr.tomacco")'), 1)
            self.assertEqual((install.backup / ".config/hypr/hyprland.lua").read_text(), 'require("hypr.looknfeel")\n')
            again = Installer(user)
            again.window_rule()
            self.assertEqual(again.changed, [])
            self.assertEqual(config.read_text(), text)

    def test_every_tool_is_part_of_the_cmake_package(self):
        # install.py copies the whole tools directory; the CMake package lists
        # files one by one, so a tool added later would silently be left out.
        root = Path(__file__).resolve().parents[2]
        listed = (root / "CMakeLists.txt").read_text()
        missing = [tool.name for tool in sorted((root / "tools").glob("*.py")) if "tools/" + tool.name not in listed]
        self.assertEqual(missing, [])

    def test_metainfo_describes_the_installed_application(self):
        import shutil
        import subprocess
        import xml.etree.ElementTree as ElementTree
        from install import VERSION
        source = ROOT / "assets" / (APP_ID + ".metainfo.xml")
        component = ElementTree.parse(source).getroot()
        # Software centers join the listing to the menu entry by its id.
        self.assertEqual(component.findtext("id"), APP_ID)
        self.assertEqual(component.findtext("launchable[@type='desktop-id']"), APP_ID + ".desktop")
        self.assertEqual(component.findtext("project_license"), "GPL-2.0-or-later")
        self.assertEqual(component.findtext("developer/name"), "Marsel Nenaj")
        # The newest release is the version both installers build.
        self.assertEqual(component.find("releases/release").get("version"), VERSION)
        for kind in ("homepage", "bugtracker"):
            self.assertTrue(component.findtext(f"url[@type='{kind}']").startswith("https://github.com/marselnenaj/tomacco"))
        self.assertIsNotNone(component.find("content_rating"))
        self.assertIn("install(FILES assets/" + APP_ID + ".metainfo.xml DESTINATION share/metainfo)", (ROOT / "CMakeLists.txt").read_text())
        if shutil.which("appstreamcli"):
            check = subprocess.run(["appstreamcli", "validate", "--no-net", "--pedantic", str(source)], capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_the_package_ships_only_text_from_the_source_tree(self):
        # Users supply their own iOS restore image; no Apple firmware, kernel,
        # Mach-O or disk image may reach the package. Everything it copies
        # from the repository is text: sources, patches, scripts and docs.
        root = Path(__file__).resolve().parents[2]
        listed = (root / "CMakeLists.txt").read_text()
        shipped = set()
        for kind, arguments in re.findall(r"install\((DIRECTORY|FILES|PROGRAMS)\s+([^)]*)\)", listed):
            for word in arguments.split():
                if word in ("DESTINATION", "RENAME"):
                    break
                path = root / word.strip('"')
                if "${" in word:
                    continue
                self.assertTrue(path.exists(), word)
                shipped.update(p for p in ([path] if path.is_file() else path.rglob("*")) if p.is_file())
        self.assertGreater(len(shipped), 100)
        binary = []
        for path in sorted(shipped):
            data = path.read_bytes()
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                binary.append(str(path.relative_to(root)))
                continue
            if b"\0" in data:
                binary.append(str(path.relative_to(root)))
        self.assertEqual(binary, [])

    def test_the_package_installs_the_user_documents_with_working_links(self):
        # share/doc/tomacco is flat and holds the guides and legal texts only; links to
        # anything else name this version's copy on GitHub. install.py and CMake
        # install the same set.
        from unittest.mock import patch
        from install import INTEGRATION_NOTES, VERSION, documents, integration_files
        docs = documents()
        self.assertNotIn("architecture.md", docs)
        for name in ("README.md", "LICENSE", "COPYRIGHT", "COPYING.GPL-3.0", "THIRD_PARTY_NOTICES.md", "THIRD_PARTY.md",
                     "legal.md", "privacy.md", "SOURCE.md", "guides.md", "getting-started.md", "desktop.md", "agents.md",
                     "android.md", "iphone.md", "troubleshooting.md", "virtual-ios.md", "virtual-ios-setup.md",
                     "virtual-ios-usage.md", "virtual-ios-troubleshooting.md", "RELEASE_NOTES.md", "research-engines.md"):
            self.assertIn(name, docs)
        self.assertIn(b"GNU GENERAL PUBLIC LICENSE\n                       Version 3, 29 June 2007", docs["COPYING.GPL-3.0"])
        for name, data in docs.items():
            if name.endswith(".md"):
                for target in re.findall(r"\]\(([^)\s]+)\)", data.decode()):
                    self.assertTrue(target.startswith(("https://", "#")) or target.split("#")[0] in docs, (name, target))
        self.assertIn(f"](https://github.com/marselnenaj/tomacco/blob/v{VERSION}/docs/architecture.md)",
                      docs["guides.md"].decode())
        # SOURCE.md names the version, the commit and the release that offers the source.
        source = docs["SOURCE.md"].decode()
        self.assertIn(f"Tomacco {VERSION}**", source)
        self.assertIn(f"https://github.com/marselnenaj/tomacco/releases/tag/v{VERSION}", source)
        self.assertNotIn("@TOMACCO_", source)
        with patch.dict("os.environ", {"TOMACCO_SOURCE_COMMIT": "`0123abc`"}):
            self.assertIn("commit\n`0123abc`.", documents()["SOURCE.md"].decode())
        listed = (ROOT / "CMakeLists.txt").read_text()
        self.assertIn('install.py" --package-docs', listed)
        self.assertNotIn("install(DIRECTORY docs/", listed)
        # The developer notes beside the engine recipes are not installed either.
        self.assertIn('PATTERN "*.md" EXCLUDE', listed)
        self.assertTrue(INTEGRATION_NOTES.match("pmp-research.md"))
        self.assertFalse(INTEGRATION_NOTES.match("README.md"))
        recipes = integration_files()
        self.assertNotIn("pmp-research.md", recipes)
        self.assertIn("vphone-loader.patch", recipes)
        for target in re.findall(r"\]\(([^)\s]+)\)", recipes["README.md"].decode()):
            self.assertTrue(target.startswith("https://") or target.split("#")[0] in recipes, target)

    def test_existing_commands_and_symlinks_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            user = Path(directory)
            command = user / ".local/bin/tomacco"
            command.parent.mkdir(parents=True)
            command.write_text("#!/bin/sh\necho another application\n")
            with self.assertRaises(SystemExit):
                Installer(user).launchers()
            destination = user / "destination"
            destination.write_text("preserved")
            (user / "link").symlink_to(destination)
            with self.assertRaises(SystemExit):
                Installer(user).write(Path("link"), "changed")
            self.assertEqual(destination.read_text(), "preserved")


class UninstallTests(unittest.TestCase):
    """--uninstall removes what installations wrote and nothing that belongs to the user."""

    @staticmethod
    def run_installer(home, *arguments):
        import subprocess
        import sys
        # Always an alternate home: the tests never touch the real one.
        result = subprocess.run([sys.executable, str(ROOT / "install.py"), "--user-home", str(home), *arguments],
                                capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def test_install_then_uninstall_leaves_only_user_data(self):
        import shutil
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            build = root / "build"
            build.mkdir()
            (build / "tomacco").write_bytes(b"\x7fELF desktop")
            (build / "tomaccoctl").write_bytes(b"\x7fELF control plane")
            # The user's own things: data, device profiles, a project, another command and their desktop configuration.
            data = home / ".local/share/tomacco"
            (data / "projects").mkdir(parents=True)
            (data / "profiles.json").write_text("[]")
            (data / "projects/app.json").write_text("{}")
            other = home / ".local/bin/another-tool"
            other.parent.mkdir(parents=True)
            other.write_text("#!/bin/sh\n")
            hypr = home / ".config/hypr/hyprland.lua"
            hypr.parent.mkdir(parents=True)
            hypr.write_text('require("hypr.looknfeel")\n')
            shell = home / ".config/omarchy/shell.json"
            shell.parent.mkdir(parents=True)
            shell.write_text(json.dumps({"bar": {"layout": {"right": [{"id": "custom.clock"}]}}, "idle": {"lock": 300}}))
            before = {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()}

            installed = self.run_installer(home, "--binary", str(build / "tomacco"), "--omarchy")
            self.assertTrue((home / ".local/bin/tomacco").is_file())
            entry = home / ".local/share/applications" / (APP_ID + ".desktop")
            text = entry.read_text()
            self.assertIn('Exec="' + str(home / ".local/bin/tomacco") + '"', text)
            self.assertIn("Name[de]=", text)
            self.assertIn("StartupWMClass=" + APP_ID, text)
            if shutil.which("desktop-file-validate"):
                check = subprocess.run(["desktop-file-validate", str(entry)], capture_output=True, text=True)
                self.assertEqual((check.returncode, check.stdout + check.stderr), (0, ""))
            metainfo = home / ".local/share/metainfo" / (APP_ID + ".metainfo.xml")
            self.assertEqual(metainfo.read_bytes(), (ROOT / "assets" / (APP_ID + ".metainfo.xml")).read_bytes())
            from install import VERSION
            self.assertEqual((home / ".local/lib/tomacco/VERSION").read_text(), VERSION + "\n")
            self.assertTrue((home / ".local/share/doc/tomacco/LICENSE").is_file())
            manual = home / ".local/share/man/man1/tomaccoctl.1"
            self.assertEqual(manual.read_bytes(), (ROOT / "docs/tomaccoctl.1").read_bytes())
            self.assertIn(APP_ID, shell.read_text())
            self.assertIn('require("hypr.tomacco")', hypr.read_text())
            # The user edits one installed document; that file is theirs now.
            changed = home / ".local/share/doc/tomacco/desktop.md"
            changed.write_text("my notes")

            result = self.run_installer(home, "--uninstall")
            self.assertEqual(result["kept"], [str(changed)])
            self.assertEqual(result["user_data"], str(data))
            after = {p.relative_to(home): p.read_bytes() for p in home.rglob("*")
                     if p.is_file() and not p.is_relative_to(data / "install-backups")}
            # Everything the user had is as it was, the edited document stays, and nothing else remains.
            expected = dict(before)
            expected[changed.relative_to(home)] = b"my notes"
            expected[shell.relative_to(home)] = after[shell.relative_to(home)]
            self.assertEqual(after, expected)
            self.assertEqual(json.loads(shell.read_text()), {"bar": {"layout": {"right": [{"id": "custom.clock"}]}}, "idle": {"lock": 300}})
            self.assertEqual(hypr.read_text(), 'require("hypr.looknfeel")\n')
            self.assertFalse((home / ".local/lib/tomacco").exists())
            self.assertFalse(metainfo.exists())
            self.assertFalse(manual.exists())
            self.assertFalse((home / ".config/omarchy/plugins" / APP_ID).exists())
            self.assertTrue((home / ".local/share/applications").is_dir())
            # A second uninstallation has nothing left to do.
            again = self.run_installer(home, "--uninstall")
            self.assertEqual((again["removed"], again["backups"]), (0, None))
            # The uninstallation is journalled like an installation: rolling it back reinstalls.
            self.run_installer(home, "--rollback", Path(result["backups"]).name)
            self.assertEqual((home / ".local/lib/tomacco/tomaccoctl").read_bytes(), b"\x7fELF control plane")
            self.assertEqual(entry.read_text(), text)
            self.assertTrue(metainfo.is_file())
            self.assertIn('require("hypr.tomacco")', hypr.read_text())
            self.assertEqual(installed["cli"], str(home / ".local/bin/tomaccoctl"))

    def test_the_installed_copy_uninstalls_without_the_package(self):
        """`tomaccoctl uninstall` runs the copy in ~/.local/lib/tomacco, because the
        package the one-line installer unpacked is gone by then."""
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            build = root / "build"
            build.mkdir()
            (build / "tomacco").write_bytes(b"\x7fELF desktop")
            (build / "tomaccoctl").write_bytes(b"\x7fELF control plane")
            self.run_installer(home, "--binary", str(build / "tomacco"))
            copy = home / ".local/lib/tomacco/install.py"
            self.assertEqual(copy.read_bytes(), (ROOT / "install.py").read_bytes())
            self.assertEqual(copy.stat().st_mode & 0o777, 0o755)
            run = lambda *arguments: subprocess.run([sys.executable, str(copy), "--user-home", str(home), *arguments],
                                                    capture_output=True, text=True, timeout=120)
            from install import VERSION
            self.assertEqual(run("--version").stdout.strip(), f"Tomacco installer {VERSION}")
            # It installs nothing itself: it has no program to install.
            refused = run("--binary", str(build / "tomacco"))
            self.assertEqual(refused.returncode, 2)
            self.assertIn("only uninstalls", refused.stderr)
            done = run("--uninstall")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(done.stdout)["kept"], [])
            self.assertFalse((home / ".local/lib/tomacco").exists())
            self.assertFalse((home / ".local/bin/tomaccoctl").exists())

    def test_uninstall_skips_files_it_does_not_own(self):
        with tempfile.TemporaryDirectory() as directory:
            user = Path(directory)
            install = Installer(user)
            # A journalled write outside the installation's own places, as a future installer might record.
            install.write(Path(".config/someone-else.conf"), "theirs")
            install.write(Path(".local/share/tomacco/settings.json"), "{}")
            install.write(Path(".local/lib/tomacco/VERSION"), "1\n")
            install.finish()
            link_target = user / "elsewhere"
            link_target.write_text("not ours")
            (user / ".local/bin").mkdir(parents=True)
            (user / ".local/bin/tomaccoctl").symlink_to(link_target)
            install = Installer(user)
            install.records.append({"path": ".local/bin/tomaccoctl", "before": None, "after": {"sha256": "0" * 64, "mode": 0o755}})
            install.finish()
            result = Installer(user).uninstall()
            self.assertEqual(result["removed"], 1)
            self.assertEqual(result["kept"], [str(user / ".local/bin/tomaccoctl")])
            self.assertEqual((user / ".config/someone-else.conf").read_text(), "theirs")
            self.assertEqual((user / ".local/share/tomacco/settings.json").read_text(), "{}")
            self.assertEqual(link_target.read_text(), "not ours")
            self.assertFalse((user / ".local/lib/tomacco").exists())


class RepeatedInstallationTests(unittest.TestCase):
    """What a second run, --update and --no-window-rule do to an installation."""

    @staticmethod
    def installed_over(home, *arguments):
        build = home.parent / "build"
        build.mkdir(exist_ok=True)
        (build / "tomacco").write_bytes(b"\x7fELF desktop")
        (build / "tomaccoctl").write_bytes(b"\x7fELF control plane")
        return UninstallTests.run_installer(home, "--binary", str(build / "tomacco"), *arguments)

    def test_a_second_installation_changes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / "home"
            (home / ".config/hypr").mkdir(parents=True)
            (home / ".config/hypr/hyprland.lua").write_text('require("hypr.looknfeel")\n')
            self.assertGreater(self.installed_over(home)["changed"], 0)
            self.assertEqual(self.installed_over(home)["changed"], 0)

    def test_no_window_rule_leaves_the_hyprland_configuration_as_it_is(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / "home"
            (home / ".config/hypr").mkdir(parents=True)
            (home / ".config/hypr/hyprland.lua").write_text('require("hypr.looknfeel")\n')
            self.installed_over(home, "--no-window-rule")
            self.assertEqual((home / ".config/hypr/hyprland.lua").read_text(), 'require("hypr.looknfeel")\n')
            self.assertFalse((home / ".config/hypr/tomacco.lua").exists())

    def test_update_refreshes_only_the_integrations_in_use(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / "home"
            (home / ".config/hypr").mkdir(parents=True)
            (home / ".config/omarchy").mkdir(parents=True)
            (home / ".config/hypr/hyprland.lua").write_text('require("hypr.looknfeel")\n')
            shell = home / ".config/omarchy/shell.json"
            shell.write_text(json.dumps({"bar": {"layout": {"right": [{"id": "custom.clock"}]}}}))
            before = shell.read_text()
            self.installed_over(home, "--update")
            self.assertEqual(shell.read_text(), before)
            self.assertFalse((home / ".config/omarchy/plugins" / APP_ID).exists())
            self.assertFalse((home / ".config/hypr/tomacco.lua").exists())
            # A placed widget is refreshed where it is, and a disabled one stays disabled.
            layout = {"left": [{"id": "custom.one"}, {"id": APP_ID, "notifyBuilds": False}], "right": [{"id": "custom.clock"}]}
            shell.write_text(json.dumps({"bar": {"layout": layout}, "disabledPlugins": [APP_ID, "custom.off"]}))
            self.installed_over(home, "--update")
            value = json.loads(shell.read_text())
            self.assertEqual(value["bar"]["layout"], layout)
            self.assertEqual(value["disabledPlugins"], [APP_ID, "custom.off"])
            self.assertTrue((home / ".config/omarchy/plugins" / APP_ID / "manifest.json").is_file())

    def test_the_app_names_its_organization_and_menu_entry_before_reading_a_setting(self):
        # main() sets the organization behind ~/.config/<organization>/Tomacco.conf.
        main = (ROOT / "src/main.cpp").read_text()
        self.assertIn('app.setDesktopFileName("' + APP_ID + '");', main)
        organization = main.find('app.setOrganizationName("marselnenaj");')
        self.assertTrue(0 < organization < main.find("Backend::storedLanguage()"))


class AgentSkillTests(unittest.TestCase):
    """The agent skill is installed beside the program and linked into the skills
    directories of the agents configured here, and only there."""

    install = staticmethod(RepeatedInstallationTests.installed_over)

    def test_the_skill_is_linked_where_an_agent_is_configured_and_removed_exactly(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / "home"
            skill = home / ".local/lib/tomacco/skills/tomacco"
            # Claude Code with a skill of its own, Codex without a skills directory,
            # a non-Tomacco entry under ~/.agents/skills and no Gemini directory at all.
            own = home / ".claude/skills/other/SKILL.md"
            own.parent.mkdir(parents=True)
            own.write_text("theirs")
            (home / ".codex").mkdir()
            foreign = home / ".agents/skills/tomacco/SKILL.md"
            foreign.parent.mkdir(parents=True)
            foreign.write_text("not Tomacco's")
            result = self.install(home, "--no-window-rule")
            self.assertEqual(result["agent_skill"], {"linked": [str(home / ".claude/skills/tomacco"), str(home / ".codex/skills/tomacco")],
                                                     "skipped": [str(home / ".agents/skills/tomacco")]})
            for agent in (".claude", ".codex"):
                self.assertEqual(os.readlink(home / agent / "skills/tomacco"), str(skill))
            self.assertEqual((home / ".claude/skills/tomacco/SKILL.md").read_bytes(),
                             (ROOT / "integrations/agent-skill/tomacco/SKILL.md").read_bytes())
            self.assertTrue((skill / "examples.md").is_file())
            self.assertEqual(foreign.read_text(), "not Tomacco's")
            self.assertFalse((home / ".gemini").exists())
            # A second run changes nothing.
            self.assertEqual(self.install(home, "--no-window-rule")["changed"], 0)

            removed = UninstallTests.run_installer(home, "--uninstall")
            self.assertEqual(removed["kept"], [])
            self.assertFalse(os.path.lexists(home / ".claude/skills/tomacco"))
            self.assertEqual(own.read_text(), "theirs")
            # The skills directory the installation made for Codex goes with the link.
            self.assertFalse((home / ".codex/skills").exists())
            self.assertTrue((home / ".codex").is_dir())
            self.assertEqual(foreign.read_text(), "not Tomacco's")
            self.assertFalse(skill.exists())

            # Rolling back the uninstallation restores the links; rolling that back removes them again.
            restored = UninstallTests.run_installer(home, "--rollback", Path(removed["backups"]).name)
            for agent in (".claude", ".codex"):
                self.assertEqual(os.readlink(home / agent / "skills/tomacco"), str(skill))
            self.assertTrue((home / ".codex/skills/tomacco/SKILL.md").is_file())
            UninstallTests.run_installer(home, "--rollback", Path(restored["backups"]).name)
            self.assertFalse(os.path.lexists(home / ".codex/skills/tomacco"))
            self.assertFalse(os.path.lexists(home / ".claude/skills/tomacco"))

    def test_rolling_back_an_installation_removes_its_links(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / "home"
            (home / ".agents/skills").mkdir(parents=True)
            result = self.install(home, "--no-window-rule")
            link = home / ".agents/skills/tomacco"
            self.assertTrue(link.is_symlink())
            Installer(home).rollback(Path(result["backups"]).name)
            self.assertFalse(os.path.lexists(link))
            # The skills directory was the person's: it stays.
            self.assertTrue((home / ".agents/skills").is_dir())

    def test_a_link_changed_since_is_not_removed(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / "home"
            (home / ".claude").mkdir(parents=True)
            self.install(home, "--no-window-rule")
            link = home / ".claude/skills/tomacco"
            mine = home / "my-skill"
            mine.mkdir()
            link.unlink()
            link.symlink_to(mine)
            UninstallTests.run_installer(home, "--uninstall")
            self.assertEqual(os.readlink(link), str(mine))
            # And a later installation does not replace it.
            result = self.install(home, "--no-window-rule")
            self.assertEqual(result["agent_skill"]["skipped"], [str(link)])
            self.assertEqual(os.readlink(link), str(mine))

    def test_uninstall_removes_a_link_tomaccoctl_setup_made(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / "home"
            self.install(home, "--no-window-rule")
            # `tomaccoctl setup` links the installed skill without the journal.
            link = home / ".codex/skills/tomacco"
            link.parent.mkdir(parents=True)
            link.symlink_to(home / ".local/lib/tomacco/skills/tomacco")
            removed = UninstallTests.run_installer(home, "--uninstall")
            self.assertFalse(os.path.lexists(link))
            self.assertTrue((home / ".codex/skills").is_dir())
            UninstallTests.run_installer(home, "--rollback", Path(removed["backups"]).name)
            self.assertTrue(link.is_symlink())

    def test_without_an_agent_directory_nothing_is_linked(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / "home"
            result = self.install(home, "--no-window-rule")
            self.assertEqual(result["agent_skill"], {"linked": [], "skipped": []})
            for agent in (".claude", ".codex", ".agents"):
                self.assertFalse((home / agent).exists())
            self.assertTrue((home / ".local/lib/tomacco/skills/tomacco/SKILL.md").is_file())

    def test_no_agent_skill_links_nothing_and_removes_earlier_links(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / "home"
            (home / ".claude/skills").mkdir(parents=True)
            self.install(home, "--no-window-rule")
            self.assertTrue((home / ".claude/skills/tomacco").is_symlink())
            result = self.install(home, "--no-window-rule", "--no-agent-skill")
            self.assertEqual(result["agent_skill"], {"linked": [], "skipped": []})
            self.assertFalse(os.path.lexists(home / ".claude/skills/tomacco"))
            self.assertTrue((home / ".claude/skills").is_dir())
            # An update respects that: it adds no link the person removed.
            self.install(home, "--no-window-rule", "--update")
            self.assertFalse(os.path.lexists(home / ".claude/skills/tomacco"))

    def test_an_update_links_a_new_agent_but_not_one_the_person_unlinked(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / "home"
            (home / ".claude").mkdir(parents=True)
            self.install(home, "--no-window-rule")
            (home / ".claude/skills/tomacco").unlink()
            # Codex installed after Tomacco.
            (home / ".codex").mkdir()
            # A file an older skill had, still as that installation left it, is removed.
            stale = Installer(home)
            stale.write(Path(".local/lib/tomacco/skills/tomacco/old.md"), "old")
            stale.finish()
            result = self.install(home, "--no-window-rule", "--update")
            self.assertEqual(result["agent_skill"]["linked"], [str(home / ".codex/skills/tomacco")])
            self.assertFalse(os.path.lexists(home / ".claude/skills/tomacco"))
            self.assertFalse((home / ".local/lib/tomacco/skills/tomacco/old.md").exists())
            # A plain installation links it again.
            self.install(home, "--no-window-rule")
            self.assertTrue((home / ".claude/skills/tomacco").is_symlink())


def fake_package(root, runtime, version="1.0.0"):
    """A release tarball's tree as CMake installs it, with fake binaries and,
    for a portable package, the given runtime files (path -> (content, executable))."""
    import shutil
    from install import DOCUMENTS, PACKAGE_PATHS
    top = root / f"tomacco-{version}-Linux-x86_64{'-portable' if runtime else ''}"
    (top / "bin").mkdir(parents=True)
    shutil.copy2(ROOT / "install.py", top / "install.py")
    (top / "bin/tomacco").write_bytes(b"\x7fELF desktop")
    (top / "bin/tomaccoctl").write_bytes(b"\x7fELF control plane " + version.encode())
    (top / "share/tomacco").mkdir(parents=True)
    (top / "share/tomacco/VERSION").write_text(version + "\n")
    for source, packaged in PACKAGE_PATHS.items():
        if (ROOT / source).is_dir():
            shutil.copytree(ROOT / source, top / packaged)
        else:
            (top / packaged).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / source, top / packaged)
    (top / "share/doc/tomacco").mkdir(parents=True)
    for name in DOCUMENTS.values():
        (top / "share/doc/tomacco" / name).write_text(name)
    for relative, (content, executable) in runtime.items():
        path = top / "lib/tomacco/runtime" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o755 if executable else 0o644)
    return top


class PortablePackageTests(unittest.TestCase):
    """install.py installs a portable package's bundled Qt and FFmpeg beside the
    program, and removes them again when a regular package replaces it."""

    RUNTIME = {"lib/libQt6Core.so.6": (b"\x7fELF qt", False), "lib/libavcodec.so.63": (b"\x7fELF ffmpeg", False),
               "plugins/platforms/libqxcb.so": (b"\x7fELF xcb", False), "libexec/QtWebEngineProcess": (b"\x7fELF helper", True),
               "libexec/qt.conf": (b"[Paths]\nPrefix=..\n", False), "qml/QtQuick/qmldir": (b"module QtQuick\n", False)}

    @staticmethod
    def install(package, home, *arguments):
        import subprocess
        import sys
        result = subprocess.run([sys.executable, str(package / "install.py"), "--user-home", str(home), "--no-window-rule", *arguments],
                                capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def test_a_portable_package_installs_its_runtime_and_a_regular_one_removes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / "home"
            library = home / ".local/lib/tomacco"
            self.install(fake_package(root / "a", self.RUNTIME), home)
            for relative, (content, executable) in self.RUNTIME.items():
                installed = library / "runtime" / relative
                self.assertEqual(installed.read_bytes(), content, relative)
                self.assertEqual(installed.stat().st_mode & 0o777, 0o755 if executable else 0o644, relative)
            self.assertEqual((library / "qt.conf").read_text(), "[Paths]\nPrefix=runtime\n")
            launcher = (home / ".local/bin/tomacco").read_text()
            self.assertIn("unset QT_PLUGIN_PATH QML_IMPORT_PATH QML2_IMPORT_PATH", launcher)
            self.assertNotIn("unset", (home / ".local/bin/tomaccoctl").read_text())

            # A newer portable package without one of the files: the stale copy goes,
            # unless the user changed it since.
            newer = dict(self.RUNTIME)
            del newer["lib/libavcodec.so.63"]
            del newer["qml/QtQuick/qmldir"]
            (library / "runtime/qml/QtQuick/qmldir").write_text("my change")
            self.install(fake_package(root / "b", newer, "1.0.1"), home)
            self.assertFalse((library / "runtime/lib/libavcodec.so.63").exists())
            self.assertEqual((library / "runtime/qml/QtQuick/qmldir").read_text(), "my change")
            self.assertTrue((library / "runtime/lib/libQt6Core.so.6").is_file())

            # A regular package leaves no qt.conf pointing at a Qt it does not use.
            (library / "runtime/qml/QtQuick/qmldir").unlink()
            self.install(fake_package(root / "c", {}, "1.0.2"), home)
            self.assertFalse((library / "runtime").exists())
            self.assertFalse((library / "qt.conf").exists())
            self.assertNotIn("unset", (home / ".local/bin/tomacco").read_text())
            self.assertEqual((library / "tomaccoctl").read_bytes(), b"\x7fELF control plane 1.0.2")

    def test_uninstalling_a_portable_installation_removes_the_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / "home"
            self.install(fake_package(root / "a", self.RUNTIME), home)
            result = self.install(home / ".local/lib/tomacco", home, "--uninstall")
            self.assertEqual(result["kept"], [])
            self.assertFalse((home / ".local/lib/tomacco").exists())
            # The uninstallation is journalled: rolling it back brings the runtime back.
            self.install(root / "a/tomacco-1.0.0-Linux-x86_64-portable", home, "--rollback", Path(result["backups"]).name)
            self.assertEqual((home / ".local/lib/tomacco/runtime/libexec/QtWebEngineProcess").read_bytes(), b"\x7fELF helper")



class BatchedJournalTests(unittest.TestCase):
    """Changes are journalled and flushed once a batch; an interruption at any
    point leaves every file as it was or as installed, and rollback and
    uninstall still work."""

    FILES = {f".local/lib/tomacco/runtime/lib/lib{index}.so": f"library {index}" for index in range(7)}

    @staticmethod
    def journal(installer):
        return json.loads((installer.backup / "manifest.json").read_text())["files"]

    @staticmethod
    def staged(home):
        from install import STAGED
        return sorted(str(p) for p in home.rglob(STAGED + "*"))

    def test_a_batch_is_journalled_and_made_durable_once_before_it_is_published(self):
        from unittest.mock import patch
        import install
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".local/lib/tomacco/runtime/lib").mkdir(parents=True)
            (home / ".local/lib/tomacco/runtime/lib/lib0.so").write_text("old")
            durable, published, journals = [], [], []
            real_durable, real_replace, real_journal = install.make_durable, install.os.replace, install.Installer.journal

            def make_durable(paths):
                manifest = next((Path(p) for p in paths if Path(p).name == "manifest.json"), None)
                if manifest:
                    durable.append({entry["path"] for entry in json.loads(manifest.read_text())["files"]})
                return real_durable(paths)

            def replace(source, target):
                if Path(target).is_relative_to(home / ".local/lib"):
                    # Write-ahead: the journal naming this file was made durable before it is published.
                    self.assertIn(str(Path(target).relative_to(home)), durable[-1])
                    published.append(target)
                return real_replace(source, target)

            def journal(self_):
                journals.append(len(self_.records))
                return real_journal(self_)

            with patch.object(install, "BATCH", 3), patch.object(install, "make_durable", side_effect=make_durable), \
                    patch("install.os.replace", side_effect=replace), patch.object(install.Installer, "journal", journal):
                installer = Installer(home)
                with installer.batch():
                    for relative, text in self.FILES.items():
                        installer.write(Path(relative), text)
                installer.finish()
            self.assertEqual(len(published), 7)
            # Three batches of at most three: not one journal per file.
            self.assertEqual(journals, [3, 6, 7])
            self.assertEqual({p.read_text() for p in (home / ".local/lib/tomacco/runtime/lib").iterdir()}, set(self.FILES.values()))
            # The old content is kept in the backup.
            self.assertEqual((installer.backup / ".local/lib/tomacco/runtime/lib/lib0.so").read_text(), "old")
            self.assertEqual(self.staged(home), [])

    def test_an_interruption_inside_a_batch_can_be_rolled_back(self):
        from unittest.mock import patch
        import install
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".local/lib/tomacco/runtime/lib").mkdir(parents=True)
            for index in range(7):
                (home / f".local/lib/tomacco/runtime/lib/lib{index}.so").write_text(f"old {index}")
            real_replace = install.os.replace
            count = []

            def replace(source, target):
                if Path(target).name.startswith("lib"):
                    count.append(target)
                    if len(count) == 5:
                        raise OSError("simulated interruption in the middle of a batch")
                return real_replace(source, target)

            installer = Installer(home)
            with patch.object(install, "BATCH", 3), patch("install.os.replace", side_effect=replace):
                with self.assertRaises(OSError):
                    with installer.batch():
                        for relative, text in self.FILES.items():
                            installer.write(Path(relative), text)
            # The second batch was journalled whole; its unpublished files were never changed.
            self.assertEqual(len(self.journal(installer)), 6)
            self.assertEqual(self.staged(home), [])
            texts = [(home / relative).read_text() for relative in self.FILES]
            self.assertEqual(texts, ["library 0", "library 1", "library 2", "library 3", "old 4", "old 5", "old 6"])
            result = Installer(home).rollback(installer.backup.name)
            self.assertEqual(result["changed"], 4)
            self.assertEqual([(home / relative).read_text() for relative in self.FILES], [f"old {i}" for i in range(7)])

    def kill_installation_after(self, package, home, replacements):
        """Runs install.py as a process that dies (no cleanup, no finally) at
        the given rename, as on a power cut or SIGKILL."""
        import subprocess
        import sys
        script = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(package)!r})\n"
            f"sys.argv = ['install.py', '--user-home', {str(home)!r}, '--no-window-rule']\n"
            "import install\n"
            "install.BATCH = 4\n"
            "real, count = os.replace, [0]\n"
            "def replace(source, target):\n"
            "    if os.path.basename(source).startswith(install.STAGED):\n"
            "        count[0] += 1\n"
            f"        if count[0] == {replacements}: os._exit(9)\n"
            "    return real(source, target)\n"
            "install.os.replace = replace\n"
            "install.main()\n")
        done = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 9, done.stderr)

    def test_a_killed_installation_rolls_back_reinstalls_and_uninstalls_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / "home"
            package = fake_package(root / "a", PortablePackageTests.RUNTIME)
            # An earlier installation, then an update killed halfway through.
            PortablePackageTests.install(package, home)
            before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file() and "install-backups" not in p.parts}
            newer = fake_package(root / "b", {name: (content + b" 1.0.1", executable) for name, (content, executable)
                                              in PortablePackageTests.RUNTIME.items()}, "1.0.1")
            self.kill_installation_after(newer, home, 6)
            self.assertNotEqual(self.staged(home), [], "the killed run left staged files")
            latest = sorted((home / ".local/share/tomacco/install-backups").iterdir())[-1]
            restored = PortablePackageTests.install(package, home, "--rollback", latest.name)
            self.assertGreater(restored["changed"], 0)
            after = {p: p.read_bytes() for p in home.rglob("*") if p.is_file() and "install-backups" not in p.parts
                     and not p.name.startswith(".tomacco-staged-")}
            self.assertEqual(after, before)
            # Installing again finishes the update and sweeps what the killed run staged.
            self.kill_installation_after(newer, home, 3)
            PortablePackageTests.install(newer, home)
            self.assertEqual(self.staged(home), [])
            self.assertEqual((home / ".local/lib/tomacco/runtime/lib/libQt6Core.so.6").read_bytes(), b"\x7fELF qt 1.0.1")
            result = PortablePackageTests.install(home / ".local/lib/tomacco", home, "--uninstall")
            self.assertEqual(result["kept"], [])
            self.assertFalse((home / ".local/lib/tomacco").exists())

    def test_a_killed_uninstallation_finishes_when_run_again(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / "home"
            package = fake_package(root / "a", PortablePackageTests.RUNTIME)
            PortablePackageTests.install(package, home)
            copy = home / ".local/lib/tomacco/install.py"
            script = ("import os, sys\n"
                      f"sys.path.insert(0, {str(copy.parent)!r})\n"
                      f"sys.argv = ['install.py', '--user-home', {str(home)!r}, '--uninstall']\n"
                      "import install\ninstall.BATCH = 4\nreal, count = os.unlink, [0]\n"
                      "def unlink(path, *a, **k):\n"
                      "    if '.local/lib/tomacco/' in str(path):\n"
                      "        count[0] += 1\n"
                      "        if count[0] == 6: os._exit(9)\n"
                      "    return real(path, *a, **k)\n"
                      "install.Path.unlink = lambda self, missing_ok=False: unlink(self)\n"
                      "install.main()\n")
            done = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 9, done.stderr)
            self.assertTrue(copy.exists() or (home / ".local/lib/tomacco").exists())
            result = PortablePackageTests.install(package, home, "--uninstall")
            self.assertEqual(result["kept"], [])
            self.assertEqual(sorted(str(p) for p in (home / ".local/lib").rglob("*")), [])
            self.assertFalse((home / ".local/bin/tomaccoctl").exists())

    def test_the_backup_survives_when_hard_links_are_refused(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "app").write_text("old")
            installer = Installer(home)
            with patch("install.os.link", side_effect=OSError("cross-device link")):
                installer.write(Path("app"), "new")
            installer.finish()
            self.assertEqual((installer.backup / "app").read_text(), "old")
            self.assertEqual(Installer(home).rollback(installer.backup.name)["changed"], 1)
            self.assertEqual((home / "app").read_text(), "old")

    def test_without_syncfs_every_file_is_flushed_on_its_own(self):
        from unittest.mock import patch
        import install
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            synced = []
            real_fsync = install.os.fsync
            with patch.object(install, "SYNCFS", None), patch("install.os.fsync", side_effect=lambda fd: synced.append(fd) or real_fsync(fd)):
                installer = Installer(home)
                with installer.batch():
                    for relative, text in self.FILES.items():
                        installer.write(Path(relative), text)
                installer.finish()
            # Seven staged files, the manifest and the backup directory, then the destination directory.
            self.assertEqual(len(synced), 9 + 1)
            self.assertEqual(len(self.journal(installer)), 7)

class PanelAndDocumentationTests(unittest.TestCase):
    """The Omarchy panel needs Quickshell to run, so what it promises is checked in its source."""

    @staticmethod
    def panel():
        return (ROOT / "integrations/omarchy/MobilePanel.qml").read_text(encoding="utf-8")

    @staticmethod
    def readme():
        # The desktop guide documents the panel and the device windows; one line of text, so a
        # sentence wrapped in the file reads the same.
        return " ".join((ROOT / "docs/desktop.md").read_text(encoding="utf-8").split())

    def test_status_poll_is_fast_only_for_an_open_popup_or_real_work_on_the_owner_bar(self):
        found = re.findall(r"Timer \{ interval: ([^;]+?) \? 2000 : 6000;[^}]*onTriggered: root[.]refresh\(\) \}", self.panel())
        self.assertEqual(len(found), 1, "the panel's status poll timer was not found")
        condition = found[0]
        self.assertRegex(condition, r"^[\w.\s|&()<>=!]+$", "expected a plain boolean expression")
        def fast(opened, owner, work, services):
            """The condition for the fast rate, evaluated for the given panel state."""
            names = {"opened": opened, "notificationOwner": owner, "workJobs": [None] * work,
                     "liveJobs": [None] * (work + services)}
            code = re.sub(r"root[.]", "", condition)
            code = re.sub(r"(\w+)[.]length", r"len(\1)", code)
            code = code.replace("!==", "!=").replace("===", "==").replace("||", " or ").replace("&&", " and ")
            code = re.sub(r"!(?!=)", " not ", code)
            return bool(eval(code, {"__builtins__": {}, "len": len}, names))
        # Every bar instance polls: fast while its own popup is open ...
        self.assertTrue(fast(opened=True, owner=False, work=0, services=0))
        self.assertTrue(fast(opened=True, owner=True, work=0, services=2))
        # ... and slowly otherwise. Emulators, audio bridges and the virtual iPhone run for hours and are
        # services, not work; the other monitors' bars see the same jobs as the owner's.
        self.assertFalse(fast(opened=False, owner=True, work=0, services=0))
        self.assertFalse(fast(opened=False, owner=True, work=0, services=3))
        self.assertFalse(fast(opened=False, owner=None, work=2, services=1))
        self.assertFalse(fast(opened=False, owner=False, work=2, services=1))
        # Real work shows quickly on the bar that notifies about it.
        self.assertTrue(fast(opened=False, owner=True, work=1, services=1))

    def test_documented_panel_keys_are_the_keys_the_panel_handles(self):
        panel, readme = self.panel(), self.readme()
        listed = re.search(r"Keys in the open panel: (.+?)[.] The same actions", readme)
        self.assertIsNotNone(listed, "the desktop guide lists no panel keys")
        documented = {item.split()[0].lower() for item in listed.group(1).split(", ")}
        handler = re.search(r"onTextKey: function\(key\) \{(.*?)\n            \}", panel, re.S)
        self.assertIsNotNone(handler, "the panel's key handler was not found")
        self.assertEqual(documented, set(re.findall(r'key==="(\w)"', handler.group(1))))
        # Omarchy's PanelKeyCatcher turns h, j, k, l and x into cursor movement and delete before the
        # panel sees them, so none of those can be a panel key.
        self.assertEqual(documented & set("hjklx"), set())
        # The key badges on the tiles are the same keys.
        self.assertLessEqual({badge.lower() for badge in re.findall(r'Tile \{[^}]*?key: "(\w)"', panel)}, documented)

    def test_panel_speaks_english_with_german_on_a_german_system(self):
        # The panel runs inside the Omarchy shell, without the desktop's translation catalogue.
        panel = self.panel()
        self.assertIn('Qt.locale().name.startsWith("de")', panel)
        self.assertIn("function tr(english,deutsch) { return german ? deutsch : english }", panel)
        pair = r'tr\("(?:[^"\\]|\\.)*","(?:[^"\\]|\\.)*"\)'
        calls = re.findall(pair, panel)
        self.assertGreater(len(calls), 60)
        rest = re.sub(pair, "", panel)
        code = "\n".join(line for line in rest.splitlines() if not line.lstrip().startswith("//"))
        # What remains may name data, never words of the interface.
        for literal in re.findall(r'"((?:[^"\\]|\\.)*)"', code):
            self.assertIsNone(re.search(r"[äöüÄÖÜß]", literal), literal)
        for word in ("Workspace", "Bereit", "Gerät", "Fenster", "Emulator beenden", "Auftrag", "Kein "):
            self.assertNotRegex(" ".join(re.findall(r'"([^"]*)"', code)), word)
        # English comes first in each pair: the first string of a call holds no German letter.
        for call in calls:
            self.assertIsNone(re.search(r"[äöüÄÖÜß]", call.split('","')[0]), call)

    def test_readme_does_not_promise_a_single_device_window(self):
        # NativeWorkspace and IosGuestView each have a DeviceWindow of their own, and nothing
        # makes them exclusive.
        readme = self.readme()
        self.assertFalse("One device window exists at a time" in readme, "the desktop guide still promises a single device window")
        self.assertTrue("Each device view, the native device and the virtual iOS device, can be detached into its own window" in readme,
                        "the desktop guide does not say that each device view has its own window")
