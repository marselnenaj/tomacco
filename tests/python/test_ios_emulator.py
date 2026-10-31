"""Control-plane and failure-containment tests, not iOS boot qualification."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import sys
import time
import unittest

from test_product import Isolated
from native_client import StudioError, dispatch, jobs
from host_patience import PATIENCE

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
import ios_virtual_iphone
import run_ios_paravirt as run


class VirtualIphoneDisplayTests(unittest.TestCase):
    """The one-step phone's panel. A profile can name the Home-button phone's
    1170x2532 panel; a start as a model must leave that panel to the model (the control plane
    refuses a request that names both)."""
    PROFILE = {"format": 1, "engine": "/engines/qemu-system-aarch64",
               "boot": {"seconds": 21600, "interactive": True, "display_profile": "1170x2532@60:65x141"}}
    INPUTS = {"kernel": "/copy/kernelcache", "device_tree": "/copy/device-tree"}

    def test_a_model_start_leaves_the_profiles_panel_to_the_model(self):
        boot = ios_virtual_iphone.boot_request(self.PROFILE, self.INPUTS, "16")
        self.assertNotIn("display_profile", boot)
        self.assertEqual(boot["device_model"], "16")
        self.assertEqual((boot["engine"], boot["kernel"], boot["input_transport"]),
                         ("/engines/qemu-system-aarch64", "/copy/kernelcache", "usb-hid"))
        # The profile itself is left as it was.
        self.assertEqual(self.PROFILE["boot"]["display_profile"], "1170x2532@60:65x141")

    def test_the_classic_start_keeps_the_profiles_panel_and_an_explicit_transport(self):
        boot = ios_virtual_iphone.boot_request(self.PROFILE, self.INPUTS, None)
        self.assertEqual(boot["display_profile"], "1170x2532@60:65x141")
        self.assertNotIn("device_model", boot)
        chosen = ios_virtual_iphone.boot_request({**self.PROFILE, "boot": {**self.PROFILE["boot"], "input_transport": "virtio"}},
                                                 self.INPUTS, "")
        self.assertEqual(chosen["input_transport"], "virtio")
        quiet = ios_virtual_iphone.boot_request({**self.PROFILE, "boot": {"seconds": 60}}, self.INPUTS, None)
        self.assertNotIn("input_transport", quiet)

    def test_the_profiles_engine_options_reach_the_guest_request_unchanged(self):
        # These CPU properties are fixed at start: boot.bti, boot.jump_probe
        # and boot.wfe_wait_us go to ios.emulator.paravirtual as they are (its
        # schema checks them), and a profile without them requests none.
        options = {"bti": "off", "jump_probe": "inline", "wfe_wait_us": 0, "target_page": "16k"}
        profile = {**self.PROFILE, "boot": {**self.PROFILE["boot"], **options}}
        for model in (None, "16"):
            with self.subTest(model=model):
                boot = ios_virtual_iphone.boot_request(profile, self.INPUTS, model)
                self.assertEqual({key: boot[key] for key in options}, options)
        plain = ios_virtual_iphone.boot_request(self.PROFILE, self.INPUTS, None)
        self.assertFalse(set(options) & set(plain))

    def test_every_catalogue_panel_derives_the_scale_its_model_draws_at(self):
        """The launcher writes /chosen/display-scale from the panel's density
        (with the placeholder 0 the guest draws 428x926 points at 2x
        on the 1179x2556 panel). Every Apple panel of the catalogue is a 3x
        panel of 457-466 ppi; the Duo's invented 7.6-inch fold is 326 ppi,
        which the rule reads as 2x although its catalogue entry says 3."""
        models = {model["id"]: model for model in json.loads((REPO / "assets/models.json").read_text())}
        panels = {identifier: model["virtual"]["panel"] for identifier, model in models.items()
                  if model.get("virtual", {}).get("panel")}
        self.assertEqual(set(panels), {"18pro", "18promax", "duo", "air", "17", "17e", "16"})
        for identifier, panel in panels.items():
            profile = run.parse_display_profile(panel)
            expected = 2 if identifier == "duo" else models[identifier]["dpr"]
            with self.subTest(model=identifier, panel=panel):
                self.assertEqual(run.display_scale_for_profile(profile), expected)
                self.assertEqual(models[identifier]["dpr"], 3)
        self.assertEqual(run.display_scale_for_profile(run.parse_display_profile(self.PROFILE["boot"]["display_profile"])), 3)


class VirtualIphoneApplicationTests(Isolated):
    """A developer adds their app to the virtual iPhone through
    ios.virtual.configure, without editing profile.json by hand; the build
    job passes app bundles to the guest-tools builder."""
    RESEARCH = "io.github.marselnenaj.tomacco.research-app"

    def profile(self):
        directory = self.root / "profile"
        (directory / "installs").mkdir(parents=True)
        (directory / "profile.json").write_text(json.dumps({
            "format": 1, "prepare": {"seconds": 300}, "boot": {}, "graphics": "/graphics", "engine": "/engine.py",
            "application": self.RESEARCH, "installs": str(directory / "installs"), "launch_watchdog_scale": 8}))
        (directory / "state.json").write_text(json.dumps({
            "run": 3, "install": str(self.root / "missing-install"), "tools": str(self.root / "missing-tools"),
            "closed": True, "guest_job": None}))
        return directory

    def bundle(self, name, bundle_id, cpu=0x100000c):
        import struct
        bundle = self.root / "apps" / name
        bundle.mkdir(parents=True)
        (bundle / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle_id, "CFBundleExecutable": name[:-4],
                                                             "CFBundlePackageType": "APPL", "CFBundleName": name[:-4]}))
        (bundle / name[:-4]).write_bytes(struct.pack("<8I", 0xfeedfacf, cpu, 0, 2, 0, 0, 0, 0) + bytes(32))
        return bundle

    def test_apps_are_added_chosen_and_removed_through_configure(self):
        directory = self.profile()
        self.assertEqual(dispatch("ios.virtual.configure", {"directory": str(directory)})["applications"], [])
        sample = self.bundle("Sample.app", "io.example.sample")
        status = dispatch("ios.virtual.configure", {"add_application": str(sample)})
        self.assertEqual([(row["bundle"], row["bundle_id"], row["name"]) for row in status["applications"]],
                         [(str(sample), "io.example.sample", "Sample.app")])
        profile = json.loads((directory / "profile.json").read_text())
        # Only the app list changed; every other input stays as written.
        self.assertEqual((profile["engine"], profile["launch_watchdog_scale"], profile["application"]),
                         ("/engine.py", 8, self.RESEARCH))
        self.assertEqual(profile["applications"], status["applications"])
        # The app to open is one of the staged ones, or the diagnostic.
        self.assertEqual(dispatch("ios.virtual.configure", {"application": "io.example.sample"})["application"],
                         "io.example.sample")
        with self.assertRaises(StudioError) as error:
            dispatch("ios.virtual.configure", {"application": "io.example.other"})
        self.assertEqual(error.exception.code, "invalid_application")
        self.assertEqual(dispatch("ios.virtual.status")["application"], "io.example.sample")
        # A bundle that cannot be deployed is refused with the reason, before it is listed.
        broken = self.bundle("Broken.app", "io.example.broken", cpu=0x1000007)
        with self.assertRaises(StudioError) as error:
            dispatch("ios.virtual.configure", {"add_application": str(broken)})
        self.assertEqual(error.exception.code, "invalid_application_bundle")
        self.assertIn("built for x86_64, not arm64", str(error.exception))
        with self.assertRaises(StudioError) as error:
            dispatch("ios.virtual.configure", {"add_application": str(self.root / "apps/Absent.app")})
        self.assertEqual(error.exception.code, "path_not_found")
        probe = self.bundle("TomaccoProbe.app", self.RESEARCH)
        with self.assertRaises(StudioError) as error:
            dispatch("ios.virtual.configure", {"add_application": str(probe)})
        self.assertEqual(error.exception.code, "invalid_application_bundle")
        # One identity, one bundle; the same path again replaces its entry.
        twin = self.bundle("Twin.app", "io.example.sample")
        with self.assertRaises(StudioError) as error:
            dispatch("ios.virtual.configure", {"add_application": str(twin)})
        self.assertEqual(error.exception.code, "ios_virtual_application_conflict")
        status = dispatch("ios.virtual.configure", {"add_application": str(sample)})
        self.assertEqual(len(status["applications"]), 1)
        # Removing the app the start opens falls back to the diagnostic.
        status = dispatch("ios.virtual.configure", {"remove_application": "io.example.sample"})
        self.assertEqual((status["applications"], status["application"]), ([], self.RESEARCH))
        with self.assertRaises(StudioError) as error:
            dispatch("ios.virtual.configure", {"remove_application": str(sample)})
        self.assertEqual(error.exception.code, "ios_virtual_application_unknown")
        # The clang a start rebuilds guest tools with; empty removes it again.
        clang = self.root / "clang-22"
        clang.write_text("#!/bin/sh\n")
        clang.chmod(0o755)
        dispatch("ios.virtual.configure", {"clang": str(clang)})
        self.assertEqual(json.loads((directory / "profile.json").read_text())["toolchain"], {"clang": str(clang)})
        plain = self.root / "not-executable"
        plain.write_text("")
        with self.assertRaises(StudioError) as error:
            dispatch("ios.virtual.configure", {"clang": str(plain)})
        self.assertEqual(error.exception.code, "invalid_toolchain")
        dispatch("ios.virtual.configure", {"clang": ""})
        self.assertEqual(json.loads((directory / "profile.json").read_text())["toolchain"], {})
        # A malformed list in profile.json makes the profile invalid, not silently empty.
        profile = json.loads((directory / "profile.json").read_text())
        profile["applications"] = [{"path": str(sample)}]
        (directory / "profile.json").write_text(json.dumps(profile))
        with self.assertRaises(StudioError) as error:
            dispatch("ios.virtual.status")
        self.assertEqual(error.exception.code, "ios_virtual_profile_invalid")

    def test_the_build_job_passes_app_bundles_and_their_entitlements_to_the_builder(self):
        (self.root / "trustcache").write_bytes(b"\2\0\0\0" + bytes(16) + bytes(4))
        tools = {}
        for name in ("clang", "linker", "ldid"):
            path = self.root / name
            path.write_text("#!/bin/sh\nexit 3\n")
            path.chmod(0o755)
            tools[name] = str(path)
        sample, other = self.bundle("Sample.app", "io.example.sample"), self.bundle("Other.app", "io.example.other")
        entitlements = self.root / "sample.entitlements"
        entitlements.write_bytes(plistlib.dumps({"get-task-allow": True}))
        request = {**tools, "trustcache": str(self.root / "trustcache"), "minimum_ios": "27.0", "application_probe": True,
                   "agent": True, "development_signing": True,
                   "applications": [{"bundle": str(sample), "entitlements": str(entitlements)}, {"bundle": str(other)}]}
        job = dispatch("ios.emulator.build_guest_tools", {**request, "output": str(self.root / "apps-build")})
        argv = job["plan"]["steps"][0]["argv"]
        self.assertEqual(argv[argv.index("--application"):],
                         ["--application", str(sample), "--application-entitlements", str(entitlements),
                          "--application", str(other), "--application-entitlements", ""])
        self.assertEqual(jobs.wait(job["id"], 20)["state"], "failed")
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.build_guest_tools", {**request, "development_signing": False, "output": str(self.root / "plain")})
        self.assertEqual(error.exception.code, "invalid_argument")
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.build_guest_tools", {**request, "output": str(self.root / "absent"),
                                                        "applications": [{"bundle": str(self.root / "apps/Absent.app")}]})
        self.assertEqual(error.exception.code, "path_not_found")
        with self.assertRaises(StudioError):
            dispatch("ios.emulator.build_guest_tools", {**request, "output": str(self.root / "shape"),
                                                        "applications": [str(sample)]})
        self.assertEqual(len(jobs.listing()), 1)


class IosEmulatorTests(Isolated):
    def fixture(self, *, complete=True, action="raise SystemExit(7)"):
        firmware = self.root / "firmware, with spaces"
        firmware.mkdir()
        parts = {name: {"Info": {"Path": path}} for name, path in {
            "KernelCache": "kernel.im4p", "DeviceTree": "Firmware/dt.im4p",
            "RestoreRamDisk": "restore.dmg", "RestoreTrustCache": "Firmware/trust.im4p",
            "Ap,SecurePageTableMonitor": "Firmware/sptm.im4p",
        }.items()}
        manifest = {"ProductVersion": "27.0.1", "ProductBuildVersion": "24A446", "BuildIdentities": [
            {"Info": {"DeviceClass": "n104ap", "Variant": variant}, "Manifest": parts}
            for variant in ("Research Customer Erase Install (IPSW)", "Customer Erase Install (IPSW)")]}
        (firmware / "BuildManifest.plist").write_bytes(plistlib.dumps(manifest))
        if complete:
            for name in ("kernel.im4p", "dt.im4p", "restore.dmg", "trust.im4p"):
                (firmware / name).write_bytes(b"test-only, not Apple firmware")
        engine = self.root / "test-engine"
        engine.write_text(f"#!{sys.executable}\nimport sys,os,time\nfrom pathlib import Path\n"
                          "if '--version' in sys.argv: print('Test-only protocol fixture');sys.exit(0)\n"
                          "if sys.argv[1:]==['-machine','help']: print('t8030  test fixture');sys.exit(0)\n"
                          "print('deliberate fixture failure',file=sys.stderr,flush=True)\n" + action + "\n")
        engine.chmod(0o755)
        return {"engine": str(engine), "firmware": str(firmware), "variant": "research"}, manifest

    def test_identity_variants_and_missing_components_are_honest(self):
        arguments, _ = self.fixture(complete=False)
        for variant in ("research", "release"):
            inspected = dispatch("ios.emulator.inspect", {**arguments, "variant": variant})
            self.assertEqual(inspected["firmware"]["version"], "27.0.1")
            self.assertTrue(inspected["firmware"]["requires_sptm"])
            self.assertFalse(inspected["qualified_for_app_development"])
            self.assertFalse(inspected["macos_vm_required"])
            self.assertEqual(len(inspected["firmware"]["missing_probe_components"]), 4)
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.probe", arguments)
        self.assertEqual(error.exception.code, "firmware_incomplete")
        self.assertEqual(jobs.listing(), [])

    def test_manifest_paths_and_symlinks_stay_inside_firmware(self):
        arguments, manifest = self.fixture()
        firmware = Path(arguments["firmware"])
        manifest["BuildIdentities"][0]["Manifest"]["KernelCache"]["Info"]["Path"] = "../outside"
        (firmware / "BuildManifest.plist").write_bytes(plistlib.dumps(manifest))
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.inspect", arguments)
        self.assertEqual(error.exception.code, "invalid_firmware")

        manifest["BuildIdentities"][0]["Manifest"]["KernelCache"]["Info"]["Path"] = "kernel.im4p"
        (firmware / "BuildManifest.plist").write_bytes(plistlib.dumps(manifest))
        outside = self.root / "outside"
        outside.write_bytes(b"outside")
        (firmware / "kernel.im4p").unlink()
        (firmware / "kernel.im4p").symlink_to(outside)
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.inspect", arguments)
        self.assertEqual(error.exception.code, "invalid_firmware")

    def test_boot_display_rejects_missing_hardware_before_starting_job(self):
        arguments, _ = self.fixture()
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.probe", {**arguments, "boot_display": {"width": 1179, "height": 2556}})
        self.assertEqual(error.exception.code, "unsupported_boot_display")
        self.assertEqual(jobs.listing(), [])

    def test_boot_display_dimensions_are_bounded_before_launch(self):
        arguments, _ = self.fixture()
        for display in ({"width": 63, "height": 2556}, {"width": 1179, "height": 4097},
                        {"width": "1179", "height": 2556}, {"width": 1179}):
            with self.subTest(display=display), self.assertRaises(StudioError):
                dispatch("ios.emulator.probe", {**arguments, "boot_display": display})
        self.assertEqual(jobs.listing(), [])

    def test_boot_media_rejects_missing_hardware_and_invalid_types_before_launch(self):
        arguments, _ = self.fixture()
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.probe", {**arguments, "boot_media": True})
        self.assertEqual(error.exception.code, "unsupported_boot_media")
        for value in ("true", 1, {}, None):
            with self.subTest(value=value), self.assertRaises(StudioError):
                dispatch("ios.emulator.probe", {**arguments, "boot_media": value})
        self.assertEqual(jobs.listing(), [])

    def prepared_fixture(self):
        arguments, _ = self.fixture()
        engine = Path(arguments["engine"])
        engine.write_text(engine.read_text().replace("t8030  test fixture", "darwin  test fixture").replace(
            "print('deliberate fixture failure'",
            "if sys.argv[1:]==['-machine','darwin,help']: print('boot-media=<bool>\\nboot-storage=<string>');sys.exit(0)\n"
            "print('deliberate fixture failure'"))
        root = Path(arguments["firmware"])
        (root / "restore.dmg").write_bytes(b"fixture only".ljust(16384, b"\0"))
        parts = {"kernel": "kernel.im4p", "device_tree": "dt.im4p", "ramdisk": "restore.dmg",
                 "trustcache": "trust.im4p", "sptm": "sptm", "txm": "txm"}
        (root / "sptm").write_bytes(b"fixture-only monitor")
        (root / "txm").write_bytes(b"fixture-only monitor")
        prepared = {"format": 1, "backend": "sptm", "environment": "ios-restore-shell", "board": "n104ap",
                    "variant": "research", "version": "27.0.1", "build": "24A446",
                    "manifest_sha256": hashlib.sha256((root / "BuildManifest.plist").read_bytes()).hexdigest(),
                    "components": {name: {"path": path, "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest()}
                                   for name, path in parts.items()}}
        (root / "tomacco-ios.json").write_text(json.dumps(prepared))
        return arguments

    def test_prepared_sptm_components_require_matching_hashes(self):
        arguments = self.prepared_fixture()
        root = Path(arguments["firmware"])
        inspected = dispatch("ios.emulator.inspect", arguments)
        self.assertEqual(inspected["backend"], "sptm")
        self.assertFalse(inspected["qualified_for_app_development"])
        self.assertEqual(inspected["firmware"]["environment"], "ios-restore-shell")
        (root / "txm").write_bytes(b"changed after preparation")
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.probe", arguments)
        self.assertEqual(error.exception.code, "firmware_modified")
        self.assertEqual(jobs.listing(), [])

    def test_working_image_requires_hardware_and_explicit_boot_media(self):
        arguments, _ = self.fixture()
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.probe", {**arguments, "working_image": str(self.root / "unused")})
        self.assertEqual(error.exception.code, "unsupported_working_image")
        self.assertEqual(jobs.listing(), [])

    def test_working_image_preserves_source_rejects_aliases_and_escapes_commas(self):
        arguments = self.prepared_fixture()
        source = Path(arguments["firmware"]) / "restore.dmg"
        source.chmod(0o600)
        original = source.read_bytes()
        working = self.root / "working, with spaces.apfs"
        working.write_bytes(original)
        working.chmod(0o600)
        inspected = dispatch("ios.emulator.inspect", arguments)
        self.assertTrue(inspected["capabilities"]["file_backed_boot_media"])
        self.assertFalse(inspected["capabilities"]["persistent_storage"])
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.probe", {**arguments, "working_image": str(working)})
        self.assertEqual(error.exception.code, "unsupported_working_image")
        args = {**arguments, "boot_media": True}
        link = self.root / "linked-image"
        link.symlink_to(working)
        wrong_size = self.root / "truncated-image"
        wrong_size.write_bytes(original[:-1])
        wrong_size.chmod(0o600)
        for rejected in (source, link, wrong_size):
            with self.subTest(path=rejected), self.assertRaises(StudioError) as error:
                dispatch("ios.emulator.probe", {**args, "working_image": str(rejected)})
            self.assertEqual(error.exception.code, "invalid_working_image")
        hardlink = self.root / "hardlink-image"
        os.link(working, hardlink)
        with self.assertRaises(StudioError):
            dispatch("ios.emulator.probe", {**args, "working_image": str(working)})
        hardlink.unlink()
        working.chmod(0o644)
        with self.assertRaises(StudioError):
            dispatch("ios.emulator.probe", {**args, "working_image": str(working)})
        self.assertEqual(jobs.listing(), [])
        working.chmod(0o600)
        job = dispatch("ios.emulator.probe", {**args, "working_image": str(working), "seconds": 1})
        self.assertEqual(jobs.wait(job["id"], 10)["state"], "failed")
        folder = Path(job["report_directory"])
        command = json.loads((folder / "command.json").read_text())
        self.assertIn("boot-storage=" + str(working).replace(",", ",,"), command[command.index("-M") + 1])
        report = json.loads((folder / "report.json").read_text())
        self.assertFalse(report["working_image_flushed"])
        self.assertFalse(report["persistent_storage_verified"])
        self.assertEqual(working.read_bytes(), original)
        self.assertEqual(source.read_bytes(), original)

    def test_failed_engine_retains_evidence_and_removes_disks(self):
        arguments, _ = self.fixture()
        job = dispatch("ios.emulator.probe", {**arguments, "seconds": 1})
        result = jobs.wait(job["id"], 10)
        self.assertEqual(result["state"], "failed", result)
        folder = Path(job["report_directory"])
        report = json.loads((folder / "report.json").read_text())
        self.assertFalse(report["guest_boot_verified"])
        self.assertFalse(report["program_counter_changed"])
        self.assertEqual(report["state"], "probe_failed")
        self.assertEqual(report["error"]["code"], "engine_exited")
        self.assertEqual(len(report["input_artifacts"]), 4)
        self.assertNotIn("modified_in_job", report["engine_artifact"])
        self.assertTrue(all("modified_in_job" not in value for value in report["input_artifacts"].values()))
        self.assertIn("deliberate fixture failure", (folder / "engine.log").read_text())
        self.assertTrue(any(Path(a["path"]).name == "report.json" for a in result["result"]["artifacts"]))
        request = json.loads((folder / "request.json").read_text())
        self.assertFalse(Path(request["machine_directory"]).exists())
        self.assertFalse(Path(request["socket_directory"]).exists())
        command = json.loads((folder / "command.json").read_text())
        self.assertTrue(any("firmware,, with spaces" in part for part in command if "trustcache=" in part))

    def test_cancel_kills_engine_and_cleans_owned_directories(self):
        arguments, _ = self.fixture(action="Path(os.environ['TOMACCO_TEST_ROOT'],'engine.pid').write_text(str(os.getpid()));time.sleep(60)")
        job = dispatch("ios.emulator.probe", {**arguments, "seconds": 60})
        deadline = time.monotonic() + 8 * PATIENCE
        pidfile = self.root / "engine.pid"
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(.03)
        self.assertTrue(pidfile.exists())
        jobs.cancel(job["id"])
        self.assertEqual(jobs.wait(job["id"], 10)["state"], "cancelled")
        process = Path("/proc") / pidfile.read_text() / "stat"
        self.assertTrue(not process.exists() or process.read_text().split(")", 1)[1].split()[0] == "Z")
        request = json.loads((Path(job["report_directory"]) / "request.json").read_text())
        self.assertFalse(Path(request["machine_directory"]).exists())
        self.assertFalse(Path(request["socket_directory"]).exists())


class GuestFailureClassifierTests(unittest.TestCase):
    """How run_ios_paravirt accounts for launchd's records of abnormal ends.

    The log samples are launchd's own serial records, shortened to the process
    and the end."""

    def setUp(self):
        import tempfile
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        import run_ios_paravirt
        self.run = run_ios_paravirt
        directory = tempfile.TemporaryDirectory(prefix="tomacco-guest-classifier-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    @staticmethod
    def record(service, pid, end):
        return f"com.apple.xpc.launchd|2026-10-28 07:19:51.835753 ({service} [{pid}]) <Notice>: {end}"

    def evidence(self, rows):
        (self.root / "uart.log").write_text("\n".join(rows) + "\n")
        return self.run.guest_evidence(self.root)

    def rows(self, evidence):
        return {(row["service"], row["kind"], row["count"], row["accounted"])
                for row in evidence["userspace_failures"]}

    def test_the_guards_ends_of_locationd_are_accounted_and_a_crash_of_settings_or_an_app_is_not(self):
        # tomacco-quiet ends locationd every 390 s. Those ends are its own
        # doing: listed, accounted for, and counted as the respawn storm
        # they are.
        guard = [self.record("user/501/com.apple.locationd", 500 + n,
                             f"exited due to SIGKILL | sent by tomacco-quiet[106], ran for {389947 + n}ms")
                 for n in range(6)]
        evidence = self.evidence(guard + [
            # launchd's own SIGKILL is an ordinary end, as before.
            self.record("user/501/com.apple.locationd", 360, "exited due to SIGKILL | sent by launchd[1]")])
        self.assertEqual(self.rows(evidence), {("com.apple.locationd", "guard-ended", 6, "locationd-guard")})
        self.assertEqual((evidence["unaccounted_failures"], evidence["accounted_failures"]), (0, 6))
        self.assertEqual(evidence["respawn_storms"],
                         [{"service": "com.apple.locationd", "failures": 6, "accounted": True}])
        record = {"guest_evidence": evidence, "exit_code": 0, "registers": {}}
        self.assertEqual(self.run.run_outcome(record), "bounded-session")
        # The guard may end nothing but locationd; Settings' SIGTRAP and a developer's app's are unaccounted.
        evidence = self.evidence(guard + [
            self.record("user/501/com.apple.chronod", 118, "exited due to SIGKILL | sent by tomacco-quiet[106], ran for 12392ms"),
            self.record("user/501/UIKitApplication:com.apple.Preferences[92bb][rb-legacy]", 985,
                        "exited due to SIGTRAP | sent by exc handler[985], ran for 11839ms"),
            self.record("user/501/UIKitApplication:dev.example.app[1a2b][rb-legacy]", 990,
                        "exited due to SIGTRAP | sent by exc handler[990], ran for 2839ms")])
        self.assertEqual({row for row in self.rows(evidence) if row[3] is None}, {
            ("com.apple.chronod", "guard-ended", 1, None), ("com.apple.Preferences", "SIGTRAP", 1, None),
            ("dev.example.app", "SIGTRAP", 1, None)})
        self.assertEqual(evidence["unaccounted_failures"], 3)
        self.assertEqual(self.run.run_outcome({**record, "guest_evidence": evidence}), "guest-process-crash")

    def test_a_rejected_control_service_is_found_under_the_project_identifier(self):
        # Guest tools label the control service with the project's identifier.
        line = self.record("system/io.github.marselnenaj.tomacco.application-inventory", 120,
                           "removing service since it exited with consistent failure - OS_REASON_EXEC")
        self.assertEqual(self.evidence([line])["guest_control_exec_failures"], [line])
        other = self.record("system/io.github.example.application-inventory", 120,
                            "removing service since it exited with consistent failure - OS_REASON_EXEC")
        self.assertEqual(self.evidence([other])["guest_control_exec_failures"], [])

    def test_only_an_ios_widget_ended_by_chronods_watchdog_is_accounted(self):
        # A developer's widget extension that misses chronod's deadline is
        # what the developer needs to see, like an app FrontBoard ends.
        watchdog = ("exited with exit reason (namespace: 20 code: 0x3e9) - OS_REASON_WATCHDOG | <RBSTerminateContext|"
                    " domain:20 code:0x000003E9 explanation:ChronodWidgetExtensionWatchdog>")
        evidence = self.evidence([
            self.record("pid/118/com.apple.weather.widget.AAEB66BD-0224-43FE-B608-5F06CADF3CDF", 700, watchdog),
            self.record("pid/118/dev.example.app.WidgetExtension.AAEB66BD-0224-43FE-B608-5F06CADF3CDF", 701, watchdog)])
        self.assertEqual(self.rows(evidence), {
            ("com.apple.weather.widget", "extension-watchdog", 1, "widget-render-deadline"),
            ("dev.example.app.WidgetExtension", "extension-watchdog", 1, None)})
        self.assertEqual((evidence["unaccounted_failures"], evidence["accounted_failures"]), (1, 1))
