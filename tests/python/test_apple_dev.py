"""iOS from Linux through the pinned omarchy-apple-dev checkout, with fakes.

The fakes stand in for git, Swift, Flutter and upstream's scripts, so no network,
SDK or Apple account is involved. What these tests hold fixed is the contract Tomacco
owes: nothing runs without being asked, nothing runs sudo or fetches from Apple,
scripts run under the private HOME, and a store key's content stays out of jobs.
"""
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from native_client import ROOT, StudioError, binary, dispatch, jobs
from build_source_release import pinned_sources

RECIPE = (ROOT / "tools/install_apple_dev.py").read_text()
REVISION = re.search(r'REVISION = "([0-9a-f]{40})"', RECIPE).group(1)
UPSTREAM = re.search(r'UPSTREAM = "(https://[^"]+)"', RECIPE).group(1)
KEY_TEXT = "-----BEGIN PRIVATE KEY-----\nNOT-A-REAL-KEY-MARKER\n-----END PRIVATE KEY-----\n"
ISSUER = "12345678-1234-1234-1234-123456789abc"


def failure(name, arguments):
    """The whole error of a call that must fail, remedy included, as text."""
    done = subprocess.run([binary(), "call", name, json.dumps(arguments)], capture_output=True, text=True)
    envelope = json.loads(done.stdout)
    assert not envelope["ok"], envelope
    return json.dumps(envelope["error"])


def script(path, source, mode=0o755):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    path.chmod(mode)
    return path


FAKE_GIT = """#!/bin/sh
echo "$@" >> "$TOMACCO_TEST_ROOT/git.log"
case "$1" in
  checkout) mkdir -p .git tools; cp "$TOMACCO_TEST_ROOT/fake-installer.sh" install-toolchain.sh ;;
  rev-parse) echo "${FAKE_COMMIT:-%s}" ;;
esac
exit 0
""" % REVISION

# Records how it was called. Without an archive it stops as upstream does at
# its SDK step, after building xtool and with no SDK registered.
FAKE_INSTALLER = """#!/bin/bash
if [ "$1" = --curses-compat ]; then exit 0; fi
{ echo "args=$*"; env; } >> "$TOMACCO_TEST_ROOT/installer.log"
mkdir -p "$HOME/.local/bin" && : > "$HOME/.local/bin/xtool"
sudo pacman -S llvm >> "$TOMACCO_TEST_ROOT/sudo.log" 2>&1
echo "sudo exit $?" >> "$TOMACCO_TEST_ROOT/sudo.log"
if [ -z "$XCODE_XIP" ]; then echo "One download left"; exit 1; fi
mkdir -p "$XDG_CONFIG_HOME/swiftpm/swift-sdks/darwin.artifactbundle"
"""


class Isolated(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tomacco-apple-dev-")
        self.root = Path(self.temp.name).resolve()
        self.bin = self.root / "bin"
        for name, source in (("git", FAKE_GIT),
                             ("cc", "#!/bin/sh\nexit 0\n"), ("zip", "#!/bin/sh\nexit 0\n"), ("file", "#!/bin/sh\nexit 0\n"),
                             ("rsync", "#!/bin/sh\nexit 0\n"), ("pkg-config", "#!/bin/sh\nexit 0\n"),
                             ("llvm-strip", "#!/bin/sh\nexit 0\n"), ("llvm-lipo", "#!/bin/sh\nexit 0\n"),
                             ("llvm-otool", "#!/bin/sh\nexit 0\n"), ("llvm-install-name-tool", "#!/bin/sh\nexit 0\n"),
                             ("llvm-ar", "#!/bin/sh\nexit 0\n")):
            script(self.bin / name, source)
        script(self.root / "fake-installer.sh", FAKE_INSTALLER)
        self.env = patch.dict(os.environ, {
            "TOMACCO_HOME": str(self.root / "state"), "TOMACCO_TEST_ROOT": str(self.root),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"})
        self.env.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.env.stop)
        self.kit_root = self.root / "state/apple-dev"
        self.checkout = self.kit_root / "checkout" / REVISION
        self.kit_home = self.kit_root / "home"

    def fake_swift(self):
        """A toolchain on PATH that the recipe can find and copy."""
        toolchain = self.root / "toolchain"
        (toolchain / "usr/lib/swift").mkdir(parents=True, exist_ok=True)
        info = json.dumps({"paths": {"runtimeResourcePath": str(toolchain / "usr/lib/swift")}})
        for name in ("swift", "swift-build"):
            script(toolchain / "usr/bin" / name, f"#!/bin/sh\necho '{info}'\n")
        script(self.bin / "swift", f"#!/bin/sh\nexec {toolchain}/usr/bin/swift \"$@\"\n")
        return toolchain

    def upstream(self, relative, source):
        return script(self.checkout / relative, source)

    def installed_toolchain(self, sdk=True):
        """What a finished ios.toolkit_install leaves, as files."""
        self.upstream("install-toolchain.sh", 'XTOOL_SDK_VERSION="epoch=4,darwinTools=1.1.0,oam=1.4.1"\n')
        for tool in (".local/bin/xtool", ".local/bin/rcodesign", "pymobile3-venv/bin/python",
                     ".local/share/tomacco-swift/6.4.0/usr/bin/swift"):
            script(self.kit_home / tool, "#!/bin/sh\nexit 0\n")
        if sdk:
            bundle = self.kit_home / ".config/swiftpm/swift-sdks/darwin.artifactbundle"
            script(bundle / "Developer/Platforms/iPhoneOS.platform/Developer/usr/bin/actool", "#!/bin/sh\nexit 0\n")
            (bundle / "darwin-sdk-version.txt").write_text("epoch=4,darwinTools=1.1.0,oam=1.4.1\n")

    def fake_flutter(self, prepared=True):
        flutter_root = self.root / "flutter"
        flutter = script(flutter_root / "bin/flutter", "#!/bin/sh\nexit 0\n")
        snapshot = flutter_root / "bin/cache/artifacts/engine/ios-release/gen_snapshot_arm64"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_bytes(b"\x7fELF...." if prepared else b"\xcf\xfa\xed\xfe....")
        dispatch("settings.configure", {"executables": {"flutter": str(flutter)}})
        return flutter

    def flutter_project(self):
        project = self.root / "counter"
        (project / "ios").mkdir(parents=True)
        (project / "pubspec.yaml").write_text("name: counter\nflutter:\n  uses-material-design: true\n")
        return project

    def xtool_project(self):
        project = self.root / "app"
        project.mkdir()
        (project / "Package.swift").write_text("// swift-tools-version: 6.0\n")
        (project / "xtool.yml").write_text("version: 1\nbundleID: dev.example.app\n")
        return project

    def succeeded(self, job):
        for _ in range(40):
            done = jobs.wait(job["id"], 5)
            if done["state"] in jobs.TERMINAL:
                break
        self.assertEqual(done["state"], "succeeded", done)
        return done


class PinTests(unittest.TestCase):
    def test_the_recipe_and_the_control_plane_name_one_commit(self):
        native = (ROOT / "native/apple_dev.rs").read_text()
        self.assertIn(f'const REVISION: &str = "{REVISION}"', native)
        self.assertIn(f'const REPOSITORY: &str = "{UPSTREAM}"', native)

    def test_the_source_release_lists_the_pin_as_it_does_the_others(self):
        self.assertIn(("omarchy-apple-dev", UPSTREAM, REVISION, "install_apple_dev.py"), pinned_sources(ROOT / "tools"))

    def test_the_notices_credit_both_authors_with_the_pin(self):
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
        self.assertIn(REVISION[:12], notices)
        self.assertIn("Joshua Warren", notices)
        self.assertIn("dl-alexandre", notices)
        self.assertIn("Permission is hereby granted", notices)

    def test_the_mode_that_downloads_from_apple_is_never_selected(self):
        self.assertIn('"--mode", "full", "--user-only"', RECIPE)
        for source in (RECIPE, (ROOT / "native/apple_dev.rs").read_text()):
            for forbidden in ('"no-xcode"', "--ipsw", "sdk-free/setup", "sdk-free/flutter-build"):
                self.assertNotIn(forbidden, source)
        self.assertNotIn("mode", dispatch_schema("ios.toolkit_install"))

    def test_the_documented_operation_count_is_the_real_one(self):
        count = len(json.loads((ROOT / "assets/operations.json").read_text()))
        self.assertIn(f"{count} typed operations", (ROOT / "README.md").read_text())
        self.assertIn(f"one typed API of {count} operations", (ROOT / "CHANGELOG.md").read_text())

    def test_the_license_warning_is_where_the_import_is_explained(self):
        for path in ("docs/iphone.md", "docs/virtual-ios/usage.md", "docs/agents.md", "docs/legal.md"):
            self.assertIn("license", (ROOT / path).read_text(), path)
        for path in ("docs/iphone.md", "docs/virtual-ios/usage.md", "docs/agents.md"):
            self.assertIn("Read Xcode's license first", " ".join((ROOT / path).read_text().split()), path)
        self.assertRegex((ROOT / "docs/legal.md").read_text(), r"Apple[- ]\w*\s*(hardware|branded)")


def dispatch_schema(name):
    operations = json.loads((ROOT / "assets/operations.json").read_text())
    return next(op for op in operations if op["name"] == name)["inputSchema"]["properties"]


class ToolkitInstallTests(Isolated):
    def test_the_doctor_reports_what_is_missing_and_installs_nothing(self):
        result = dispatch("ios.doctor", {})
        toolkit = result["linux_toolkit"]
        self.assertFalse(toolkit["installed"])
        self.assertFalse(toolkit["install"]["automatic"])
        self.assertFalse(toolkit["install"]["downloads_from_apple"])
        self.assertEqual(toolkit["commit"], REVISION)
        self.assertTrue(any("ios.toolkit_install" in row["remedy"] for row in toolkit["missing"]))
        self.assertNotIn("linux_toolkit", {check["name"] for check in result["checks"]})
        self.assertFalse(self.kit_root.exists(), "the doctor must not create anything")
        self.assertFalse((self.root / "git.log").exists(), "the doctor must not fetch anything")

    def test_a_missing_system_library_is_a_remedy_naming_sudo_for_you_to_run(self):
        self.fake_swift()
        script(self.bin / "pkg-config", "#!/bin/sh\nexit 1\n")
        self.assertIn("sudo pacman", failure("ios.toolkit_install", {"toolchain": True}))
        self.assertFalse((self.root / "git.log").exists())

    def test_the_default_fetches_the_pinned_commit_and_runs_none_of_its_scripts(self):
        done = self.succeeded(dispatch("ios.toolkit_install", {}))
        log = (self.root / "git.log").read_text()
        self.assertIn(f"fetch -q --depth 1 {UPSTREAM} {REVISION}", log)
        self.assertTrue((self.checkout / "install-toolchain.sh").is_file())
        self.assertFalse((self.root / "installer.log").exists())
        self.assertEqual(done["result"]["metadata"]["commit"], REVISION)
        self.assertFalse(done["result"]["metadata"]["downloads_from_apple"])
        self.assertTrue(dispatch("ios.doctor", {})["linux_toolkit"]["installed"])

    def test_a_fetch_that_is_not_the_pinned_commit_leaves_no_checkout(self):
        with patch.dict(os.environ, {"FAKE_COMMIT": "0" * 40}):
            job = dispatch("ios.toolkit_install", {})
            done = jobs.wait(job["id"], 20)
        self.assertEqual(done["state"], "failed", done)
        self.assertFalse(self.checkout.exists())

    def test_the_installer_runs_privately_in_full_mode_and_without_sudo(self):
        self.fake_swift()
        real_xtool = script(self.root / "users-xtool", "#!/bin/sh\nexit 0\n")
        before = real_xtool.read_bytes()
        with patch.dict(os.environ, {"OMARCHY_APPLE_MODE": "no-xcode", "SDK_SRC": "/nonexistent"}):
            self.succeeded(dispatch("ios.toolkit_install", {"toolchain": True}))
        log = (self.root / "installer.log").read_text()
        self.assertIn("args=--mode full --user-only", log)
        self.assertIn(f"HOME={self.kit_home}\n", log)
        self.assertIn(f"XDG_CONFIG_HOME={self.kit_home}/.config\n", log)
        self.assertNotIn("OMARCHY_APPLE_MODE=no-xcode", log)
        self.assertNotIn("SDK_SRC=", log)
        self.assertNotIn("XCODE_XIP=", log)
        self.assertIn(f"PATH={self.kit_root}/guard:", log)
        sudo = (self.root / "sudo.log").read_text()
        self.assertIn("Tomacco does not run sudo", sudo)
        self.assertIn("sudo exit 1", sudo)
        self.assertEqual(real_xtool.read_bytes(), before)
        self.assertTrue((self.kit_home / ".local/share/tomacco-swift/toolchain/usr/bin/swift").is_file())
        report = json.loads((self.kit_root / "install-report.json").read_text())
        self.assertTrue(report["passed"])
        self.assertFalse(report["sdk_ready"], "stopping at the SDK step is reported, not hidden")
        self.assertIn("developer.apple.com", report["next"])

    def test_the_archive_you_name_reaches_the_installer(self):
        self.fake_swift()
        archive = self.root / "Xcode.xip"
        archive.write_bytes(b"pretend")
        self.succeeded(dispatch("ios.toolkit_install", {"toolchain": True, "xcode_xip": str(archive)}))
        self.assertIn(f"XCODE_XIP={archive}\n", (self.root / "installer.log").read_text())
        self.assertTrue(json.loads((self.kit_root / "install-report.json").read_text())["sdk_ready"])

    def test_an_installer_failure_with_an_archive_fails_the_job(self):
        self.fake_swift()
        script(self.root / "fake-installer.sh", '#!/bin/bash\n[ "$1" = --curses-compat ] && exit 0\nexit 3\n')
        archive = self.root / "Xcode.xip"
        archive.write_bytes(b"pretend")
        done = jobs.wait(dispatch("ios.toolkit_install", {"toolchain": True, "xcode_xip": str(archive)})["id"], 20)
        self.assertEqual(done["state"], "failed", done)

    def test_an_archive_without_the_toolchain_is_refused_and_modes_are_not_arguments(self):
        archive = self.root / "Xcode.xip"
        archive.write_bytes(b"pretend")
        with self.assertRaises(StudioError):
            dispatch("ios.toolkit_install", {"xcode_xip": str(archive)})
        with self.assertRaises(StudioError):
            dispatch("ios.toolkit_install", {"mode": "no-xcode"})


class FlutterTests(Isolated):
    def test_each_missing_piece_is_named_before_any_job(self):
        project = self.flutter_project()
        self.assertIn("ios.toolkit_install", failure("ios.flutter_build", {"path": str(project)}))
        self.upstream("flutter/build.sh", "#!/bin/bash\nexit 0\n")
        self.installed_toolchain(sdk=False)
        self.assertIn("ios.sdk_install does not count", failure("ios.flutter_build", {"path": str(project)}))
        self.installed_toolchain()
        self.fake_flutter(prepared=False)
        self.assertIn("ios.flutter_setup", failure("ios.flutter_build", {"path": str(project)}))
        self.fake_flutter()
        (self.bin / "llvm-strip").unlink()
        real = [p for p in os.environ["PATH"].split(os.pathsep) if not (Path(p) / "llvm-strip").exists()]
        with patch.dict(os.environ, {"PATH": os.pathsep.join(real)}):
            self.assertIn("llvm-strip", failure("ios.flutter_build", {"path": str(project)}))

    def test_a_project_without_flutter_ios_is_refused(self):
        project = self.xtool_project()
        with self.assertRaises(StudioError):
            dispatch("ios.flutter_build", {"path": str(project)})

    def test_the_build_is_a_job_under_the_private_home_with_its_products_named(self):
        project = self.flutter_project()
        self.installed_toolchain()
        flutter = self.fake_flutter()
        self.upstream("flutter/build.sh", '#!/bin/bash\n{ echo "args=$*"; env; } > "$TOMACCO_TEST_ROOT/build.log"\n'
                                          'mkdir -p "$1/build/ios-linux" && echo ipa > "$1/build/ios-linux/Runner.ipa"\n')
        done = self.succeeded(dispatch("ios.flutter_build", {"path": str(project), "defines": ["MODE=demo"],
                                                             "bundle_id": "dev.example.counter", "flavor": "demo"}))
        log = (self.root / "build.log").read_text()
        self.assertIn(f"args={project} MODE=demo\n", log)
        self.assertIn(f"HOME={self.kit_home}\n", log)
        self.assertIn(f"FLUTTER={flutter}\n", log)
        self.assertIn("BUNDLE_ID=dev.example.counter\n", log)
        self.assertIn("FLAVOR=demo\n", log)
        metadata = done["result"]["metadata"]
        self.assertEqual(metadata["ipa"], str(project / "build/ios-linux/Runner.ipa"))
        self.assertEqual(metadata["app"], str(project / "build/ios-linux/Payload/Runner.app"))
        self.assertFalse(metadata["signed"])
        self.assertFalse(metadata["tested"])
        self.assertEqual(Path(done["result"]["artifacts"][0]["path"]).name, "Runner.ipa")

    def test_a_malformed_define_is_refused(self):
        project = self.flutter_project()
        self.installed_toolchain()
        self.fake_flutter()
        self.upstream("flutter/build.sh", "#!/bin/bash\nexit 0\n")
        with self.assertRaises(StudioError):
            dispatch("ios.flutter_build", {"path": str(project), "defines": ["--no-define"]})

    def test_setup_runs_the_script_only_when_asked(self):
        with self.assertRaises(StudioError):
            dispatch("ios.flutter_setup", {})
        flutter = self.fake_flutter(prepared=False)
        self.upstream("flutter/setup.sh", '#!/bin/bash\nenv > "$TOMACCO_TEST_ROOT/setup.log"\n')
        done = self.succeeded(dispatch("ios.flutter_setup", {}))
        log = (self.root / "setup.log").read_text()
        self.assertIn(f"FLUTTER={flutter}\n", log)
        self.assertIn(f"HOME={self.kit_home}\n", log)
        self.assertTrue(done["result"]["metadata"]["modifies_flutter_install"])

    def test_project_execute_points_at_the_linux_route(self):
        project = self.flutter_project()
        self.fake_flutter()
        self.assertIn("ios.flutter_build", failure("project.execute", {"path": str(project), "platform": "ios"}))

    def test_the_description_says_it_is_untested(self):
        operations = {op["name"]: op for op in json.loads((ROOT / "assets/operations.json").read_text())}
        self.assertIn("UNTESTED", operations["ios.flutter_build"]["description"])
        self.assertIn("UNTESTED", operations["ios.flutter_setup"]["description"])


class XcodeImportTests(Isolated):
    FIXTURE = ROOT / "tests/fixtures/xcode-sample/Sample.xcodeproj"

    def fake_generator(self):
        self.upstream("tools/xcodeproj2xtool.py", (
            "import os,sys,json\n"
            "out=sys.argv[sys.argv.index('--out')+1]\n"
            "os.makedirs(out,exist_ok=True)\n"
            "open(out+'/xtool.yml','w').write('version: 1\\n')\n"
            "open(os.environ['TOMACCO_TEST_ROOT']+'/generator.json','w').write(json.dumps(sys.argv[1:]))\n"
            "print('wrote',out)\n"
            "print('warning: run-script phase skipped',file=sys.stderr)\n"))

    def test_the_fixture_is_a_project_with_the_source_it_names(self):
        self.assertIn('path = SampleApp.swift', (self.FIXTURE / "project.pbxproj").read_text())
        self.assertTrue((self.FIXTURE.parent / "Sample/SampleApp.swift").is_file())

    def test_the_adapter_and_warnings_come_back_with_the_next_call(self):
        self.fake_generator()
        out = self.root / "adapter"
        result = dispatch("project.import_xcode", {"project": str(self.FIXTURE), "out": str(out),
                                                   "target": "Sample", "bundle_id": "dev.example.sample"})
        self.assertEqual(result["adapter"], str(out))
        self.assertEqual(result["warnings"], ["warning: run-script phase skipped"])
        self.assertEqual(result["next"], {"operation": "project.execute", "arguments": {"path": str(out), "platform": "ios"}})
        arguments = json.loads((self.root / "generator.json").read_text())
        self.assertEqual(arguments, [str(self.FIXTURE), "--out", str(out), "--target", "Sample", "--bundle-id", "dev.example.sample"])

    def test_it_needs_the_checkout_and_says_so(self):
        self.assertIn("ios.toolkit_install", failure("project.import_xcode", {"project": str(self.FIXTURE), "out": str(self.root / "adapter")}))

    def test_a_directory_that_is_no_xcode_project_is_refused(self):
        self.fake_generator()
        with self.assertRaises(StudioError):
            dispatch("project.import_xcode", {"project": str(self.root)})

    def test_a_generator_failure_carries_its_reason(self):
        self.upstream("tools/xcodeproj2xtool.py", "import sys\nsys.exit('error: no application target')\n")
        self.assertIn("no application target", failure("project.import_xcode", {"project": str(self.FIXTURE), "out": str(self.root / "adapter")}))


class CrashTests(Isolated):
    def test_a_report_comes_back_structured(self):
        self.upstream("tools/ips-symbolicate.py", (
            "import sys,json,os\n"
            "open(os.environ['TOMACCO_TEST_ROOT']+'/ips.json','w').write(json.dumps(sys.argv[1:]))\n"
            "print('App: Demo 1.2 (dev.demo)')\nprint('Exception: EXC_BAD_ACCESS (SIGSEGV)')\n"
            "print('Faulting thread: 0 of 2')\nprint('  0  Demo  main+0x4 [resolved locally]')\nprint('  1  dyld  start')\n"
            "print('Last exception backtrace:')\nprint('  0  CoreFoundation  raise+0x8')\n"))
        report = self.root / "crash.ips"
        binary = self.root / "Demo"
        report.write_text("{}\n{}\n")
        binary.write_bytes(b"macho")
        result = dispatch("ios.crash_symbolicate", {"report": str(report), "binaries": [str(binary)]})
        self.assertEqual(result["app"], "Demo 1.2 (dev.demo)")
        self.assertEqual(result["exception"], "EXC_BAD_ACCESS (SIGSEGV)")
        self.assertEqual(result["faulting_thread"], "0 of 2")
        self.assertEqual(result["frames"], ["Demo  main+0x4 [resolved locally]", "dyld  start"])
        self.assertEqual(result["last_exception_backtrace"], ["CoreFoundation  raise+0x8"])
        self.assertEqual(result["resolved_locally"], 1)
        self.assertEqual(json.loads((self.root / "ips.json").read_text()), [str(report), "--binary", str(binary)])

    def test_an_unreadable_report_is_an_error_with_a_remedy(self):
        self.upstream("tools/ips-symbolicate.py", "import sys\nsys.exit('error: line 1 is not JSON')\n")
        report = self.root / "crash.ips"
        report.write_text("Incident Identifier: old form\n")
        self.assertIn("line 1 is not JSON", failure("ios.crash_symbolicate", {"report": str(report)}))

    def test_without_the_checkout_it_says_what_to_install(self):
        report = self.root / "crash.ips"
        report.write_text("{}\n{}\n")
        self.assertIn("ios.toolkit_install", failure("ios.crash_symbolicate", {"report": str(report)}))


class StoreTests(Isolated):
    def key(self, mode=0o600):
        key = self.root / "AuthKey_ABCDE12345.p8"
        key.write_text(KEY_TEXT)
        key.chmod(mode)
        return key

    def ready(self):
        self.installed_toolchain()
        self.upstream("ship.sh", '#!/bin/bash\n{ echo "args=$*"; env; } > "$TOMACCO_TEST_ROOT/ship.log"\necho "== 1. Release build =="\n'
                                 'mkdir -p xtool && echo ipa > xtool/app.ipa\n')
        return self.xtool_project()

    def test_the_key_stays_in_its_file(self):
        project = self.ready()
        key = self.key()
        done = self.succeeded(dispatch("ios.store_ship", {"path": str(project), "upload": True, "key_id": "ABCDE12345",
                                                          "issuer_id": ISSUER, "key_path": str(key)}))
        log = (self.root / "ship.log").read_text()
        self.assertIn("args=--upload\n", log)
        self.assertIn(f"ASC_KEY_PATH={key}\n", log)
        self.assertIn("ASC_KEY_ID=ABCDE12345\n", log)
        self.assertIn(f"HOME={self.kit_home}\n", log)
        self.assertNotIn("NOT-A-REAL-KEY-MARKER", log)
        record = json.dumps(done) + json.dumps(jobs.get(done["id"]))
        logs = dispatch("job.logs", {"job_id": done["id"], "offset": 0})["text"]
        self.assertNotIn("NOT-A-REAL-KEY-MARKER", record)
        self.assertNotIn("NOT-A-REAL-KEY-MARKER", logs)
        self.assertEqual(done["result"]["metadata"]["identity"], "apple-distribution")
        self.assertFalse(done["result"]["metadata"]["submitted_for_review"])
        self.assertEqual(Path(done["result"]["artifacts"][0]["path"]).name, "app.ipa")

    def test_a_key_others_can_read_is_refused(self):
        project = self.ready()
        self.assertIn("readable only by its owner", failure("ios.store_ship", {"path": str(project), "key_id": "ABCDE12345", "issuer_id": ISSUER,
                                        "key_path": str(self.key(0o640))}))
        self.assertFalse((self.root / "ship.log").exists())

    def test_without_a_key_it_signs_for_a_test_and_an_upload_is_refused(self):
        project = self.ready()
        with self.assertRaises(StudioError):
            dispatch("ios.store_ship", {"path": str(project), "upload": True})
        done = self.succeeded(dispatch("ios.store_ship", {"path": str(project)}))
        log = (self.root / "ship.log").read_text()
        self.assertIn("args=\n", log)
        self.assertNotIn("ASC_KEY", log)
        self.assertEqual(done["result"]["metadata"]["identity"], "test")

    def test_half_a_key_and_malformed_ids_are_refused(self):
        project = self.ready()
        key = str(self.key())
        for arguments in ({"key_id": "ABCDE12345"}, {"key_id": "ABCDE12345", "key_path": key},
                          {"key_id": "short", "issuer_id": ISSUER, "key_path": key},
                          {"key_id": "ABCDE12345", "issuer_id": "not-a-uuid", "key_path": key}):
            with self.assertRaises(StudioError):
                dispatch("ios.store_ship", {"path": str(project), **arguments})
        self.assertFalse((self.root / "ship.log").exists())

    def test_it_needs_the_private_toolchain_and_an_xtool_project(self):
        project = self.xtool_project()
        self.upstream("ship.sh", "#!/bin/bash\nexit 0\n")
        self.assertIn("ios.toolkit_install", failure("ios.store_ship", {"path": str(project)}))
        self.installed_toolchain()
        with self.assertRaises(StudioError):
            dispatch("ios.store_ship", {"path": str(self.flutter_project())})


class SwiftUIRouteTests(Isolated):
    FAKE_XTOOL = ("#!/bin/sh\necho \"$(pwd) $*\" >> \"$TOMACCO_TEST_ROOT/xtool.log\"\n"
                  "if [ \"$1\" = new ]; then mkdir -p \"$2\"; printf 'version: 1\\nbundleID: x.y\\n' > \"$2/xtool.yml\"; fi\n")

    def app(self, project, cpu=0x100000C):
        app = project / "xtool/Sample.app"
        app.mkdir(parents=True)
        info = {"CFBundleIdentifier": "dev.example.sample", "CFBundleExecutable": "Sample", "CFBundleName": "Sample",
                "CFBundlePackageType": "APPL", "MinimumOSVersion": "17.0"}
        import plistlib
        (app / "Info.plist").write_bytes(plistlib.dumps(info))
        (app / "Sample").write_bytes(struct.pack("<8I", 0xFEEDFACF, cpu, 0, 2, 0, 0, 0, 0) + bytes(32))
        (app / "Sample").chmod(0o755)
        return app

    def test_one_call_creates_and_builds(self):
        xtool = script(self.bin / "xtool", self.FAKE_XTOOL)
        dispatch("settings.configure", {"executables": {"xtool": str(xtool)}})
        target = self.root / "Hello"
        done = self.succeeded(dispatch("project.create", {"path": str(target), "kind": "xtool", "name": "Hello",
                                                          "organization": "dev.example", "build": True}))
        log = (self.root / "xtool.log").read_text().splitlines()
        self.assertEqual(log[0], f"{self.root} new Hello --skip-setup")
        self.assertEqual(log[1], f"{target} dev build")
        self.assertIn("bundleID: dev.example.hello", (target / "xtool.yml").read_text())
        self.assertEqual(done["result"]["metadata"]["next"]["operation"], "project.app")

    def test_without_build_it_only_creates_and_other_kinds_are_refused(self):
        xtool = script(self.bin / "xtool", self.FAKE_XTOOL)
        dispatch("settings.configure", {"executables": {"xtool": str(xtool)}})
        self.succeeded(dispatch("project.create", {"path": str(self.root / "Plain"), "kind": "xtool", "name": "Plain",
                                                   "organization": "dev.example"}))
        self.assertNotIn("dev build", (self.root / "xtool.log").read_text())
        with self.assertRaises(StudioError):
            dispatch("project.create", {"path": str(self.root / "Other"), "kind": "android", "name": "Other",
                                        "organization": "dev.example", "build": True})

    def test_before_a_build_project_app_says_how_to_build(self):
        self.assertIn("project.execute", failure("project.app", {"path": str(self.xtool_project())}))

    def test_an_accepted_bundle_comes_with_the_calls_that_follow(self):
        project = self.xtool_project()
        app = self.app(project)
        result = dispatch("project.app", {"path": str(project)})
        self.assertEqual(result["app"], str(app))
        self.assertEqual(result["bundle_id"], "dev.example.sample")
        self.assertTrue(result["virtual_device"]["accepted"], result)
        self.assertEqual(result["next"][0], {"operation": "ios.virtual.configure", "arguments": {"add_application": str(app)}})
        self.assertEqual(result["next"][1], {"operation": "ios.virtual.configure", "arguments": {"application": "dev.example.sample"}})

    def test_a_refused_bundle_reports_the_rule_it_broke(self):
        project = self.xtool_project()
        self.app(project, cpu=0x1000007)
        result = dispatch("project.app", {"path": str(project)})
        self.assertFalse(result["virtual_device"]["accepted"])
        self.assertIn("arm64", json.dumps(result["virtual_device"]["error"]))


if __name__ == "__main__":
    unittest.main()
