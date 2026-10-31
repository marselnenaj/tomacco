"""Strict lifecycle evidence parsing; fixtures do not qualify guest app support."""
import copy
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest

for directory in ("tools", "tests"):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / directory))
import build_ios_research_bootstrap as bootstrap
import extract_ios_application as application
from product_clock import ProductClock


class ApplicationEvidenceTests(unittest.TestCase):
    def test_actual_scene_refusal_is_retained_without_qualifying_a_window(self):
        events = self.startup + [{"event": "did-finish-launching"}, {"event": "request-scene"},
                                {"event": "scene-request-failed", "code": 0}]
        self.source.write_bytes(self.log(events))
        result = application.extract(self.source, self.output)
        self.assertEqual(result["scene_request_failure"]["code"], 0)
        self.assertTrue(result["launch_callback_observed"])
        self.assertFalse(result["scene_connected_observed"])
        self.assertFalse(result["graphical_apps_qualified"])
        events[-1].update(domain="UIKit", message='Fenster abgelehnt: "App"')
        self.assertEqual(application.parse_log(self.log(events))[-1]["domain"], "UIKit")
        for bad in (events[:-2] + [events[-1]], events + [events[-1]],
                    events[:-1] + [{**events[-1], "message": "x" * 257}]):
            with self.assertRaises(ValueError):
                application.parse_log(self.log(bad))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.output = self.root / "app.log", self.root / "capture"
        self.startup = [{"event": "begin"}, {"event": "identity", "uid": 501, "gid": 501},
                        {"event": "load-uikit"}, {"event": "bundle-verified"}, {"event": "enter-application-main"}]
        self.callbacks = [{"event": "did-finish-launching"}, {"event": "scene-connected"},
                          {"event": "window-state", "width": 390.0, "height": 844.0, "scale": 3.0, "hidden": 0, "key": 1},
                          {"event": "scene-active", "activation_state": 0}]

    def log(self, events):
        return b"".join((json.dumps({"sequence": index, **event}) + "\n").encode()
                        for index, event in enumerate(events, 1))

    def test_blocked_start_retains_actual_progress_and_false_qualification(self):
        raw = self.log(self.startup)
        self.source.write_bytes(raw)
        result = application.extract(self.source, self.output)
        self.assertTrue(result["mobile_identity_verified"])
        self.assertTrue(result["application_main_attempted"])
        self.assertEqual(result["last_event"], "enter-application-main")
        self.assertEqual(result["source_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertIsNone(result["process_exit_code"])
        for key in ("finished", "launch_callback_observed", "scene_connected_observed",
                    "visible_key_window_observed", "active_scene_observed", "application_lifecycle_observed",
                    "full_ios_boot_qualified", "graphical_apps_qualified", "physical_touch_qualified"):
            self.assertFalse(result[key], key)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        report = self.output / "manifest.json"
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(report.read_text()), result)

    def test_callbacks_record_only_observations_never_emulator_qualification(self):
        self.source.write_bytes(self.log(self.startup + self.callbacks + [{"event": "control-action", "count": 1}]))
        result = application.extract(self.source, self.output)
        self.assertTrue(result["application_lifecycle_observed"])
        self.assertFalse(result["finished"])
        self.assertFalse(result["full_ios_boot_qualified"])
        self.assertFalse(result["graphical_apps_qualified"])
        self.assertFalse(result["physical_touch_qualified"])

    def test_bad_window_or_identity_is_recorded_without_lifecycle_claim(self):
        cases = [("uid", 0), ("gid", 0), ("width", 0), ("height", 0), ("scale", 0),
                 ("key", 0), ("hidden", 1), ("activation_state", 1)]
        for key, value in cases:
            with self.subTest(key=key):
                events = copy.deepcopy(self.startup + self.callbacks)
                for event in events:
                    if key in event:
                        event[key] = value
                self.source.write_bytes(self.log(events))
                result = application.extract(self.source, self.root / key)
                self.assertFalse(result["application_lifecycle_observed"])

    def test_exit_codes_remain_distinct_from_launch_success(self):
        events = self.startup + [{"event": "application-main-returned", "code": 7}, {"event": "exit", "code": 7}]
        self.source.write_bytes(self.log(events))
        result = application.extract(self.source, self.output)
        self.assertTrue(result["finished"])
        self.assertEqual(result["process_exit_code"], 7)
        self.assertFalse(result["application_lifecycle_observed"])
        self.source.write_bytes(self.log(self.startup[:3] + [{"event": "exit", "code": 20}]))
        result = application.extract(self.source, self.root / "load-failure")
        self.assertFalse(result["application_main_attempted"])
        self.assertEqual(result["process_exit_code"], 20)

    def test_corrupt_or_contradictory_events_fail_before_creating_output(self):
        good = self.log(self.startup + self.callbacks)
        cases = {
            "empty": b"", "noise": b"kernel log\n" + good, "not_object": b"[]\n",
            "bad_json": good[:-3], "duplicate_field": good.replace(b'"sequence": 1', b'"sequence": 1, "sequence": 1'),
            "sequence_bool": good.replace(b'"sequence": 1', b'"sequence": true'),
            "sequence_gap": good.replace(b'"sequence": 2', b'"sequence": 3'),
            "unknown_event": good.replace(b'"begin"', b'"pretend-ready"'),
            "unknown_field": good.replace(b'"begin"', b'"begin", "extra": 1'),
            "nan": good.replace(b'390.0', b'NaN'), "infinite": good.replace(b'390.0', b'1e999'),
            "window_bounds": good.replace(b'844.0', b'99999'),
            "bool_geometry": good.replace(b'390.0', b'true'),
            "bad_identity": good.replace(b'"uid": 501', b'"uid": -1'),
            "bad_activation": good.replace(b'"activation_state": 0', b'"activation_state": 99'),
            "repeated_start": self.log(self.startup + self.startup),
            "early_callback": self.log(self.startup[:-1] + self.callbacks),
            "missing_launch": self.log(self.startup + self.callbacks[1:]),
            "missing_scene": self.log(self.startup + self.callbacks[:1] + self.callbacks[2:]),
            "duplicate_window": self.log(self.startup + self.callbacks + [self.callbacks[2]]),
            "action_count": self.log(self.startup + self.callbacks + [{"event": "control-action", "count": 2}]),
            "inconsistent_exit": self.log(self.startup + [{"event": "application-main-returned", "code": 1}, {"event": "exit", "code": 0}]),
            "unearned_zero_exit": self.log(self.startup[:2] + [{"event": "exit", "code": 0}]),
            "after_return": self.log(self.startup + [{"event": "application-main-returned", "code": 0}] + self.callbacks),
            "after_exit": self.log(self.startup + [{"event": "exit", "code": 20}] + self.callbacks),
            "oversize": b" " * (application.MAX_LOG + 1),
        }
        for name, raw in cases.items():
            with self.subTest(name=name):
                self.source.write_bytes(raw)
                with self.assertRaises(ValueError):
                    application.extract(self.source, self.output)
                self.assertFalse(self.output.exists())

    def test_existing_output_and_symlinks_are_preserved(self):
        self.source.write_bytes(self.log(self.startup))
        self.output.mkdir()
        marker = self.output / "user-content"
        marker.write_bytes(b"preserve")
        with self.assertRaisesRegex(ValueError, "already exists"):
            application.extract(self.source, self.output)
        self.assertEqual(marker.read_bytes(), b"preserve")
        alias = self.root / "alias"
        alias.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "already exists"):
            application.extract(self.source, alias)
        self.assertFalse((self.root / "missing").exists())

    def test_own_bundle_declares_real_scene_class_and_delegate(self):
        info = bootstrap.application_info("27.0.1")
        self.assertEqual(plistlib.loads(plistlib.dumps(info)), info)
        self.assertEqual(info["CFBundleExecutable"], "TomaccoProbe")
        self.assertEqual(info["CFBundlePackageType"], "APPL")
        self.assertEqual(info["MinimumOSVersion"], "27.0.1")
        manifest = info["UIApplicationSceneManifest"]
        self.assertFalse(manifest["UIApplicationSupportsMultipleScenes"])
        scene, = manifest["UISceneConfigurations"]["UIWindowSceneSessionRoleApplication"]
        self.assertEqual(scene["UISceneClassName"], "UIWindowScene")
        self.assertEqual(scene["UISceneDelegateClassName"], "TomaccoResearchSceneDelegate")

    def test_own_job_uses_launchd_mobile_domain_and_does_not_restart(self):
        job = bootstrap.application_job()
        self.assertEqual(job["Label"], bootstrap.application_info("27.0")["CFBundleIdentifier"])
        self.assertEqual(job["UserName"], "mobile")
        self.assertTrue(job["LaunchOnlyOnce"])
        self.assertTrue(job["RunAtLoad"])
        self.assertNotIn("KeepAlive", job)
        self.assertNotIn("MachServices", job)
        self.assertEqual(job["ProgramArguments"], ["/tomacco/TomaccoProbe.app/TomaccoProbe"])

    def test_bootstrap_service_observation_preserves_failures(self):
        lookup = {"event": "service-lookup", "bootstrap_port": 259, "result": 0, "display_port": 771}
        for index, record in enumerate((lookup, {**lookup, "result": 1102, "display_port": 0})):
            events = self.startup[:2] + [record] + self.startup[2:]
            self.source.write_bytes(self.log(events))
            result = application.extract(self.source, self.root / str(index))
            self.assertEqual(result["display_service_lookup_succeeded"], index == 0)
            self.assertFalse(result["application_lifecycle_observed"])
        for record in ({**lookup, "display_port": 0}, {**lookup, "result": False},
                       {**lookup, "bootstrap_port": -1}):
            with self.assertRaises(ValueError):
                application.parse_log(self.log(self.startup[:2] + [record] + self.startup[2:]))
        with self.assertRaises(ValueError):
            application.parse_log(self.log(self.startup + [lookup]))

    def test_delegate_metadata_and_event_budget_remain_bounded(self):
        registration = {"event": "delegate-registration", "app_class": 1, "scene_class": 1,
                        "app_protocol": 0, "scene_protocol": 0}
        events = self.startup[:4] + [registration] + self.startup[4:]
        self.assertEqual(application.parse_log(self.log(events))[4]["app_protocol"], 0)
        invalid = [self.startup + [registration],
                   self.startup[:4] + [registration, registration] + self.startup[4:],
                   self.startup[:4] + [{**registration, "scene_class": True}] + self.startup[4:],
                   self.startup + self.callbacks + [self.callbacks[-1]] * application.MAX_EVENTS]
        for events in invalid:
            with self.assertRaises(ValueError):
                application.parse_log(self.log(events))

    def test_fatal_signal_records_addresses_without_replacing_them_with_success(self):
        fault = {"event": "fatal-signal", "signal": 5, "pc": "0x000000018491407c",
                 "lr": "0x12340001848df85c", "fp": "0x000000016fffef00", "cache_base": "0x0000000180000000",
                 "frames": ["0x123400018491419c"]}
        self.source.write_bytes(self.log(self.startup + [fault]))
        result = application.extract(self.source, self.output)
        self.assertEqual(result["fatal_signal"]["signal"], 5)
        self.assertEqual(result["fatal_signal"]["lr"], fault["lr"])
        self.assertFalse(result["application_lifecycle_observed"])
        self.assertFalse(result["graphical_apps_qualified"])
        self.assertFalse(result["finished"])
        # The recorder remains active while UIApplicationMain returns and the
        # autorelease pool drains; a crash there must preserve its actual signal.
        for prefix in (self.startup[:4], self.startup + [{"event": "application-main-returned", "code": 7}]):
            parsed = application.parse_log(self.log(prefix + [fault]))
            self.assertEqual(parsed[-1]["signal"], 5)
        with self.assertRaises(ValueError):
            application.parse_log(self.log(self.startup[:3] + [fault]))
        for altered in ({**fault, "signal": 9}, {**fault, "pc": "0xNOTHEX"},
                        {**fault, "frames": [fault["pc"]] * 17}, {**fault, "frames": {}}, {**fault, "fp": 99}):
            with self.assertRaises(ValueError):
                application.parse_log(self.log(self.startup + [altered]))
        with self.assertRaises(ValueError):
            application.parse_log(self.log(self.startup + [fault, {"event": "exit", "code": 0}]))

    def test_conflicting_boot_modes_fail_before_reading_inputs_or_creating_output(self):
        common = [sys.executable, bootstrap.__file__, "--seed-manifest", str(self.root / "absent-seed"),
                  "--trustcache", str(self.root / "absent-trustcache"), "--ldid", str(self.root / "absent-signer"),
                  "--linker", str(self.root / "absent-linker"), "--output", str(self.output), "--application-diagnostics"]
        for flags in (["--graphics-diagnostics"], ["--data-filesystem", "apfs", "--restart-probe"]):
            with self.subTest(flags=flags):
                result = subprocess.run(common + flags, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 2)
                self.assertIn("exclusive use of the third UART", result.stderr)
                self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()


# --- Developer app bundles: inspection, and the start that installs them ---

import os
import struct
from unittest.mock import patch

import ios_application_bundle as bundles
import ios_virtual_iphone

RESEARCH = "io.github.marselnenaj.tomacco.research-app"


def macho(cpu=0x100000c, kind=2, magic=0xfeedfacf, entitlements=None):
    """A Mach-O header as the inspector reads it; with entitlements, a
    signature whose slot 5 carries them, as a signed input would."""
    if entitlements is None:
        return struct.pack("<8I", magic, cpu, 0, kind, 0, 0, 0, 0) + bytes(32)
    xml = plistlib.dumps(entitlements)
    blob = struct.pack(">II", 0xfade7171, 8 + len(xml)) + xml
    table = struct.pack(">5I", 0xfade0cc0, 20 + len(blob), 1, 5, 20)
    header = struct.pack("<8I", magic, cpu, 0, kind, 1, 16, 0, 0) + struct.pack("<4I", 0x1d, 16, 48, len(table) + len(blob))
    return header + table + blob


def fat(*cpus):
    """A universal Mach-O header (big-endian on disk) naming one slice per CPU type."""
    return (struct.pack(">II", 0xcafebabe, len(cpus)) + b"".join(struct.pack(">5I", cpu, 0, 4096, 64, 14) for cpu in cpus)
            + bytes(32))


def framework(name="Kit", bundle_id="io.example.kit", binary=None, info=None):
    """The files of an embedded framework, as (path in the app, bytes) pairs."""
    if info is None:
        info = {"CFBundleIdentifier": bundle_id, "CFBundleExecutable": name, "CFBundlePackageType": "FMWK"}
    files = [(f"Frameworks/{name}.framework/Info.plist", info if isinstance(info, bytes) else plistlib.dumps(info))]
    if binary is not False:
        files.append((f"Frameworks/{name}.framework/{name}", macho(kind=6) if binary is None else binary))
    return tuple(files)


def extension(name="Share", bundle_id="io.example.sample.share", binary=None, info=None,
              point="com.apple.share-services", entitlements=None, **keys):
    """The files of an app extension, as (path in the app, bytes) pairs: its
    Info.plist in Xcode's shape (keys override or, as None, remove entries)
    and its executable, signed with entitlements when given."""
    if info is None:
        attributes = {"NSExtensionPointIdentifier": point}
        if point != "com.apple.widgetkit-extension":
            attributes["NSExtensionPrincipalClass"] = "ShareViewController"
        info = {"CFBundleIdentifier": bundle_id, "CFBundleExecutable": name, "CFBundlePackageType": "XPC!",
                "CFBundleName": name, "CFBundleDisplayName": name, "CFBundleVersion": "1",
                "CFBundleShortVersionString": "1.0", "NSExtension": attributes}
        for key, value in keys.items():
            if value is None:
                info.pop(key, None)
            else:
                info[key] = value
    files = [(f"PlugIns/{name}.appex/Info.plist", info if isinstance(info, bytes) else plistlib.dumps(info))]
    if binary is not False:
        files.append((f"PlugIns/{name}.appex/{name}", macho(entitlements=entitlements) if binary is None else binary))
    return tuple(files)


def write_bundle(root, name="Sample.app", bundle_id="io.example.sample", executable="Sample", info=None,
                 program=None, extra=()):
    bundle = root / name
    bundle.mkdir(parents=True)
    if info is None:
        info = {"CFBundleIdentifier": bundle_id, "CFBundleExecutable": executable, "CFBundlePackageType": "APPL",
                "CFBundleName": "Sample", "CFBundleShortVersionString": "1.2"}
    (bundle / "Info.plist").write_bytes(info if isinstance(info, bytes) else plistlib.dumps(info))
    if program is not False:
        (bundle / executable).write_bytes(macho() if program is None else program)
    for relative, data in extra:
        (bundle / relative).parent.mkdir(parents=True, exist_ok=True)
        (bundle / relative).write_bytes(data)
    return bundle


class BundleInspectionTests(unittest.TestCase):
    """What a developer's .app must look like, and the reasons named otherwise."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tomacco-bundle-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_a_plain_arm64_bundle_is_described_with_a_content_digest(self):
        bundle = write_bundle(self.root, extra=(("Assets.car", b"assets"), ("en.lproj/Localizable.strings", b'"a" = "b";'),
                                                ("_CodeSignature/CodeResources", b"old signature")))
        record = bundles.inspect_bundle(bundle)
        self.assertEqual((record["name"], record["bundle_id"], record["executable"], record["display_name"], record["version"]),
                         ("Sample.app", "io.example.sample", "Sample", "Sample", "1.2"))
        self.assertEqual([row["path"] for row in record["files"]],
                         ["Assets.car", "Info.plist", "Sample", "en.lproj/Localizable.strings"])
        self.assertEqual(record["entitlements"], {"application-identifier": "io.example.sample"})
        self.assertEqual(record["entitlements_source"], "default")
        self.assertEqual(record["bytes"], sum(row["bytes"] for row in record["files"]))
        digest = record["content_sha256"]
        # A previous signature is not part of the content; a resource is.
        (bundle / "_CodeSignature/CodeResources").write_bytes(b"other signature")
        self.assertEqual(bundles.inspect_bundle(bundle)["content_sha256"], digest)
        (bundle / "Assets.car").write_bytes(b"assets2")
        self.assertNotEqual(bundles.inspect_bundle(bundle)["content_sha256"], digest)

    def test_embedded_and_explicit_entitlements_are_read_and_checked(self):
        signed = write_bundle(self.root / "signed", program=macho(entitlements={"get-task-allow": True}))
        record = bundles.inspect_bundle(signed)
        self.assertEqual(record["entitlements"], {"get-task-allow": True, "application-identifier": "io.example.sample"})
        self.assertEqual(record["entitlements_source"], "embedded")
        privileged = write_bundle(self.root / "privileged", program=macho(entitlements={"platform-application": True}))
        with self.assertRaisesRegex(ValueError, "platform-application.*not allowed"):
            bundles.inspect_bundle(privileged)
        requested = self.root / "sample.entitlements"
        requested.write_bytes(plistlib.dumps({"keychain-access-groups": ["io.example.shared"]}))
        record = bundles.inspect_bundle(privileged, requested)
        self.assertEqual(record["entitlements"], {"keychain-access-groups": ["io.example.shared"],
                                                  "application-identifier": "io.example.sample"})
        self.assertEqual(record["entitlements_source"], "file")
        requested.write_bytes(b"not a plist")
        with self.assertRaisesRegex(ValueError, "not a property list"):
            bundles.inspect_bundle(privileged, requested)
        requested.write_bytes(plistlib.dumps({"task_for_pid-allow": True}))
        with self.assertRaisesRegex(ValueError, "task_for_pid-allow.*not allowed"):
            bundles.inspect_bundle(privileged, requested)

    def test_every_reason_a_bundle_cannot_be_deployed_is_named(self):
        cases = {
            "no-info": (dict(info=b""), "has no Info.plist"),
            "not-a-plist": (dict(info=b"<plist><dict>"), "not a property list"),
            "no-identifier": (dict(info={"CFBundleExecutable": "Sample"}), "no valid CFBundleIdentifier"),
            "bad-identifier": (dict(info={"CFBundleIdentifier": "io.example/app", "CFBundleExecutable": "Sample"}),
                               "no valid CFBundleIdentifier"),
            "no-executable-name": (dict(info={"CFBundleIdentifier": "io.example.sample"}), "no valid CFBundleExecutable"),
            "missing-executable": (dict(program=False), "has no executable 'Sample'"),
            "not-an-app": (dict(info={"CFBundleIdentifier": "io.example.sample", "CFBundleExecutable": "Sample",
                                      "CFBundlePackageType": "FMWK"}), "not an application"),
            "x86_64": (dict(program=macho(cpu=0x1000007)), "built for x86_64, not arm64"),
            "fat": (dict(program=fat(0x1000007, 0x100000c)), "thin it to the arm64 slice"),
            "fat-without-arm64": (dict(program=fat(0x1000007)), "without an arm64 slice"),
            "arm64e": (dict(program=struct.pack("<8I", 0xfeedfacf, 0x100000c, 2, 2, 0, 0, 0, 0) + bytes(32)),
                       "built for arm64e; build it for arm64"),
            "dylib": (dict(program=macho(kind=6)), "not an executable"),
            "not-mach-o": (dict(program=b"#!/bin/sh\n" + bytes(30)), "not a 64-bit Mach-O executable"),
            "tiny": (dict(program=b"MZ"), "not a Mach-O executable"),
            "plug-in-without-info": (dict(extra=(("PlugIns/Share.appex/Share", macho()),)),
                                     "PlugIns/Share.appex has no Info.plist"),
            "mac-plug-in": (dict(extra=(("Plug-ins/Share.appex/Share", macho()),)), "nested code in Plug-ins/"),
            "second-mach-o": (dict(extra=(("Helper", macho()),)), "second Mach-O file: Helper"),
            "empty-file": (dict(extra=(("Empty.txt", b""),)), "Empty.txt is empty"),
            "big-file": (dict(extra=(("Big.bin", b"x"),)), "larger than 64 MiB"),
        }
        for name, (options, message) in cases.items():
            with self.subTest(name=name):
                root = self.root / name
                bundle = write_bundle(root, **options)
                if name == "no-info":
                    (bundle / "Info.plist").unlink()
                if name == "big-file":
                    with (bundle / "Big.bin").open("wb") as stream:
                        stream.truncate(bundles.MAX_FILE_BYTES + 1)
                with self.assertRaisesRegex(ValueError, message):
                    bundles.inspect_bundle(bundle)
        linked = write_bundle(self.root / "linked")
        (linked / "Resource.txt").symlink_to(linked / "Info.plist")
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            bundles.inspect_bundle(linked)
        with self.assertRaisesRegex(ValueError, "not a valid .app bundle name"):
            bundles.inspect_bundle(write_bundle(self.root / "named", name="Sample"))
        with self.assertRaisesRegex(ValueError, "not an .app directory"):
            bundles.inspect_bundle(self.root / "absent/Sample.app")

    def test_embedded_frameworks_and_libraries_are_the_bundles_other_code(self):
        bundle = write_bundle(self.root, extra=framework() + (
            ("Frameworks/Kit.framework/Strings.bundle/en.lproj/Kit.strings", b'"a" = "b";'),
            ("Frameworks/Kit.framework/_CodeSignature/CodeResources", b"old framework signature"),
            ("Frameworks/libSupport_Core.dylib", macho(kind=6)), ("Assets.car", b"assets")))
        record = bundles.inspect_bundle(bundle)
        self.assertEqual(record["nested_code"], [
            {"path": "Frameworks/Kit.framework/Kit", "kind": "framework", "bundle": "Frameworks/Kit.framework",
             "identifier": "io.example.kit"},
            {"path": "Frameworks/libSupport_Core.dylib", "kind": "library", "identifier": "libSupport_Core"}])
        # An input signature of a framework is replaced like the app's own.
        self.assertEqual([row["path"] for row in record["files"]],
                         ["Assets.car", "Frameworks/Kit.framework/Info.plist", "Frameworks/Kit.framework/Kit",
                          "Frameworks/Kit.framework/Strings.bundle/en.lproj/Kit.strings",
                          "Frameworks/libSupport_Core.dylib", "Info.plist", "Sample"])
        # The content digest the start compares covers embedded code and its resources.
        digest = record["content_sha256"]
        (bundle / "Frameworks/Kit.framework/_CodeSignature/CodeResources").write_bytes(b"other signature")
        self.assertEqual(bundles.inspect_bundle(bundle)["content_sha256"], digest)
        for path in ("Frameworks/Kit.framework/Kit", "Frameworks/libSupport_Core.dylib",
                     "Frameworks/Kit.framework/Strings.bundle/en.lproj/Kit.strings"):
            with self.subTest(path=path):
                original = (bundle / path).read_bytes()
                (bundle / path).write_bytes(original + b"\0")
                self.assertNotEqual(bundles.inspect_bundle(bundle)["content_sha256"], digest)
                (bundle / path).write_bytes(original)
        self.assertEqual(bundles.inspect_bundle(bundle)["content_sha256"], digest)
        # A framework's package type may be left out; a bundle without nested code has none.
        plain = write_bundle(self.root / "plain", extra=framework(info={"CFBundleIdentifier": "io.example.kit",
                                                                         "CFBundleExecutable": "Kit"}))
        self.assertEqual(len(bundles.inspect_bundle(plain)["nested_code"]), 1)
        self.assertEqual(bundles.inspect_bundle(write_bundle(self.root / "none"))["nested_code"], [])

    def test_every_reason_embedded_code_cannot_be_deployed_is_named(self):
        dylib = lambda cpu=0x100000c, kind=6, subtype=0: struct.pack("<8I", 0xfeedfacf, cpu, subtype, kind, 0, 0, 0, 0) + bytes(32)
        cases = {
            "no-framework-info": (framework(info=b"<plist><dict>")[1:], "Kit.framework has no Info.plist"),
            "framework-info-not-plist": (framework(info=b"<plist><dict>"), "Kit.framework/Info.plist is not a property list"),
            "framework-no-identifier": (framework(info={"CFBundleExecutable": "Kit"}), "Kit.framework/Info.plist has no valid CFBundleIdentifier"),
            "framework-bad-identifier": (framework(bundle_id="io.example/kit"), "has no valid CFBundleIdentifier"),
            "framework-no-executable-name": (framework(info={"CFBundleIdentifier": "io.example.kit"}), "has no valid CFBundleExecutable"),
            "framework-missing-binary": (framework(binary=False), "Kit.framework has no binary 'Kit'"),
            "framework-not-fmwk": (framework(info={"CFBundleIdentifier": "io.example.kit", "CFBundleExecutable": "Kit",
                                                   "CFBundlePackageType": "BNDL"}), "not a framework"),
            "framework-app-identity": (framework(bundle_id="io.example.sample"), "uses the bundle identifier io.example.sample of Sample.app"),
            "frameworks-one-identity": (framework() + framework(name="Other"), "Other.framework uses the bundle identifier io.example.kit"),
            "framework-x86_64": (framework(binary=dylib(cpu=0x1000007)), "Kit.framework/Kit is built for x86_64, not arm64"),
            "framework-fat": (framework(binary=fat(0x100000c, 0x1000007)), "Kit is a universal.*thin it to the arm64 slice"),
            "framework-fat-without-arm64": (framework(binary=fat(0x1000007)), "Kit is a universal.*without an arm64 slice"),
            "framework-arm64e": (framework(binary=dylib(subtype=0x80000002)), "Kit is built for arm64e"),
            "framework-executable": (framework(binary=macho()), "Kit is a Mach-O file of type 2, not a dynamic library"),
            "framework-bundle-type": (framework(binary=dylib(kind=8)), "type 8, not a dynamic library"),
            "library-not-mach-o": ((("Frameworks/libSupport.dylib", b"#!/bin/sh\n" + bytes(30)),),
                                   "libSupport.dylib is not a 64-bit Mach-O dynamic library"),
            "library-tiny": ((("Frameworks/libSupport.dylib", b"MZ"),), "not a Mach-O dynamic library"),
            "library-x86_64": ((("Frameworks/libSupport.dylib", dylib(cpu=0x1000007)),), "built for x86_64"),
            "frameworks-other-file": ((("Frameworks/notes.txt", b"notes"),), "Frameworks/ holds only .* not 'notes.txt'"),
            "frameworks-subdirectory": ((("Frameworks/Sub/libSupport.dylib", dylib()),), "Frameworks/ holds only .* not 'Sub'"),
            "framework-outside-frameworks": ((("Resources/Kit.framework/Kit", dylib()),), "framework outside Frameworks/: Resources/Kit.framework"),
            "framework-in-framework": (framework() + (("Frameworks/Kit.framework/Frameworks/Inner.framework/Inner", dylib()),),
                                       "nested code inside a framework: Frameworks/Kit.framework/Frameworks"),
            "nested-app": ((("Payload/Other.app/Other", macho()),), "a nested application: Payload/Other.app"),
            "loose-extension": ((("Extras/Share.appex/Share", macho()),), "an app extension: Extras/Share.appex"),
            "xpc-service": ((("XPCServices/Helper.xpc/Helper", macho()),), "nested code in XPCServices/"),
            "framework-helper": (framework() + (("Frameworks/Kit.framework/Helper", macho()),),
                                 "second Mach-O file: Frameworks/Kit.framework/Helper"),
            "large-library": ((("Frameworks/libSupport.dylib", dylib()),), "libSupport.dylib is larger than 16 MiB"),
        }
        for name, (extra, message) in cases.items():
            with self.subTest(name=name):
                bundle = write_bundle(self.root / name, extra=extra)
                if name == "large-library":
                    with (bundle / "Frameworks/libSupport.dylib").open("r+b") as stream:
                        stream.truncate(bundles.MAX_CODE_BYTES + 1)
                with self.assertRaisesRegex(ValueError, message):
                    bundles.inspect_bundle(bundle)
        # A link out of the bundle, in Frameworks/ too, is refused like any link.
        linked = write_bundle(self.root / "linked-library", extra=framework())
        (linked / "Frameworks/libSystem.dylib").symlink_to("/usr/lib/libSystem.B.dylib")
        with self.assertRaisesRegex(ValueError, "symbolic link or special file: Frameworks/libSystem.dylib"):
            bundles.inspect_bundle(linked)
        linked = write_bundle(self.root / "linked-framework", extra=framework())
        (linked / "Frameworks/Kit.framework/Kit").unlink()
        (linked / "Frameworks/Kit.framework/Kit").symlink_to("../../../../outside/Kit")
        with self.assertRaisesRegex(ValueError, "symbolic link or special file: Frameworks/Kit.framework/Kit"):
            bundles.inspect_bundle(linked)

    def test_app_extensions_are_nested_code_with_identities_and_entitlements_of_their_own(self):
        bundle = write_bundle(self.root, extra=framework() + extension(
            entitlements={"com.apple.security.application-groups": ["group.io.example.sample"]}) + extension(
            name="Widget", bundle_id="io.example.sample.widget", point="com.apple.widgetkit-extension") + (
            ("PlugIns/Share.appex/Base.lproj/MainInterface.strings", b'"a" = "b";'),
            ("PlugIns/Share.appex/_CodeSignature/CodeResources", b"old extension signature")))
        record = bundles.inspect_bundle(bundle)
        self.assertEqual(record["nested_code"], [
            {"path": "Frameworks/Kit.framework/Kit", "kind": "framework", "bundle": "Frameworks/Kit.framework",
             "identifier": "io.example.kit"},
            {"path": "PlugIns/Share.appex/Share", "kind": "extension", "bundle": "PlugIns/Share.appex",
             "identifier": "io.example.sample.share", "extension_point": "com.apple.share-services",
             # An extension's entitlements are its own, under the app's allowlist.
             "entitlements": {"application-identifier": "io.example.sample.share",
                              "com.apple.security.application-groups": ["group.io.example.sample"]}},
            {"path": "PlugIns/Widget.appex/Widget", "kind": "extension", "bundle": "PlugIns/Widget.appex",
             "identifier": "io.example.sample.widget", "extension_point": "com.apple.widgetkit-extension",
             "entitlements": {"application-identifier": "io.example.sample.widget"}}])
        self.assertEqual(record["entitlements"], {"application-identifier": "io.example.sample"})
        # An input signature of an extension is replaced like the app's own.
        self.assertNotIn("PlugIns/Share.appex/_CodeSignature/CodeResources", [row["path"] for row in record["files"]])
        digest = record["content_sha256"]
        (bundle / "PlugIns/Share.appex/_CodeSignature/CodeResources").write_bytes(b"other signature")
        self.assertEqual(bundles.inspect_bundle(bundle)["content_sha256"], digest)
        for path in ("PlugIns/Share.appex/Share", "PlugIns/Share.appex/Info.plist",
                     "PlugIns/Share.appex/Base.lproj/MainInterface.strings"):
            with self.subTest(path=path):
                original = (bundle / path).read_bytes()
                (bundle / path).write_bytes(original + b"\0")
                if path.endswith("Info.plist"):
                    (bundle / path).write_bytes(plistlib.dumps({**plistlib.loads(original), "CFBundleVersion": "2"}))
                self.assertNotEqual(bundles.inspect_bundle(bundle)["content_sha256"], digest)
                (bundle / path).write_bytes(original)
        # A team the app names is the extension's team too, behind either identifier.
        team = {"application-identifier": "ABCDE12345.io.example.sample", "com.apple.developer.team-identifier": "ABCDE12345"}
        same = write_bundle(self.root / "team", program=macho(entitlements=team), extra=extension(
            entitlements={"application-identifier": "ABCDE12345.io.example.sample.share"}))
        self.assertEqual(bundles.inspect_bundle(same)["nested_code"][0]["entitlements"],
                         {"application-identifier": "ABCDE12345.io.example.sample.share"})
        # The package type may be left out; any extension point is accepted
        # (the phone's own catalog decides which it knows).
        plain = write_bundle(self.root / "plain", extra=extension(CFBundlePackageType=None, point="dev.example.custom-point"))
        self.assertEqual(bundles.inspect_bundle(plain)["nested_code"][0]["extension_point"], "dev.example.custom-point")

    def test_every_reason_an_app_extension_cannot_be_deployed_is_named(self):
        storyboard = {"NSExtensionPointIdentifier": "com.apple.share-services", "NSExtensionMainStoryboard": "Main"}
        cases = {
            "no-info": (extension()[1:], "PlugIns/Share.appex has no Info.plist"),
            "info-not-plist": (extension(info=b"<plist><dict>"), "Share.appex/Info.plist is not a property list"),
            "no-identifier": (extension(CFBundleIdentifier=None), "Share.appex/Info.plist has no valid CFBundleIdentifier"),
            "no-executable-name": (extension(CFBundleExecutable=None), "Share.appex/Info.plist has no valid CFBundleExecutable"),
            "missing-executable": (extension(binary=False), "Share.appex has no executable 'Share'"),
            "not-prefixed": (extension(bundle_id="io.example.share"), "not prefixed by its app's identifier io.example.sample.*AppexBundleIDNotPrefixed"),
            "app-identity-prefix-only": (extension(bundle_id="io.example.samples"), "not prefixed by its app's identifier"),
            "dotted-suffix": (extension(bundle_id="io.example.sample.share.more"), "contains a '.'.*AppexBundleIDSuffixContainsPeriod"),
            "not-xpc": (extension(CFBundlePackageType="BNDL"), "Share.appex is a 'BNDL' package, not an app extension"),
            "no-version": (extension(CFBundleVersion=None), "has no CFBundleVersion"),
            "empty-name": (extension(CFBundleName=""), "has no CFBundleName"),
            "display-name-not-string": (extension(CFBundleDisplayName=7), "CFBundleDisplayName that is not a non-empty string"),
            "no-extension-dictionary": (extension(NSExtension=None), "has no NSExtension dictionary"),
            "extension-kit": (extension(NSExtension=None, EXAppExtensionAttributes={"EXExtensionPointIdentifier": "x"}),
                              "ExtensionKit extensions .* belong in Extensions/"),
            "no-point": (extension(NSExtension={"NSExtensionPrincipalClass": "Share"}), "no valid NSExtension NSExtensionPointIdentifier"),
            "bad-point": (extension(point="com apple share"), "no valid NSExtension NSExtensionPointIdentifier"),
            "bad-class": (extension(NSExtension={"NSExtensionPointIdentifier": "com.apple.share-services",
                                                  "NSExtensionPrincipalClass": "Share View"}), "not a class or storyboard name"),
            "class-and-storyboard": (extension(NSExtension={**storyboard, "NSExtensionPrincipalClass": "Share"}),
                                     "names both NSExtensionPrincipalClass and NSExtensionMainStoryboard"),
            "widget-principal-class": (extension(NSExtension={"NSExtensionPointIdentifier": "com.apple.widgetkit-extension",
                                                              "NSExtensionPrincipalClass": "Widget"}),
                                       "com.apple.widgetkit-extension does not allow.*AppexBundleContainsClassOrStoryboard"),
            "dylib-executable": (extension(binary=macho(kind=6)), "Share.appex/Share is a Mach-O file of type 6, not an executable"),
            "x86_64": (extension(binary=macho(cpu=0x1000007)), "Share.appex/Share is built for x86_64"),
            "fat": (extension(binary=fat(0x100000c, 0x1000007)), "Share is a universal.*thin it"),
            "framework-identity": (framework(bundle_id="io.example.sample.share") + extension(),
                                   "Share.appex uses the bundle identifier io.example.sample.share of Sample.app/Frameworks/Kit.framework"),
            "refused-entitlement": (extension(entitlements={"platform-application": True}),
                                    "Share.appex: Entitlement 'platform-application' is not allowed"),
            "foreign-identifier": (extension(entitlements={"application-identifier": "io.example.sample"}),
                                   "Share.appex: Entitlement 'application-identifier' 'io.example.sample' must be the bundle identifier 'io.example.sample.share'"),
            "nested-frameworks": (extension() + (("PlugIns/Share.appex/Frameworks/libInner.dylib", macho(kind=6)),),
                                  "nested code inside an app extension: PlugIns/Share.appex/Frameworks"),
            "nested-extension": (extension() + (("PlugIns/Share.appex/PlugIns/Inner.appex/Inner", macho()),),
                                 "nested code inside an app extension: PlugIns/Share.appex/PlugIns"),
            "framework-in-extension": (extension() + (("PlugIns/Share.appex/Lib/Inner.framework/Inner", macho(kind=6)),),
                                       "nested code inside an app extension: PlugIns/Share.appex/Lib/Inner.framework"),
            "helper-in-extension": (extension() + (("PlugIns/Share.appex/Helper", macho()),),
                                    "second Mach-O file: PlugIns/Share.appex/Helper"),
            "file-in-plug-ins": ((("PlugIns/notes.txt", b"notes"),), "PlugIns/ holds only <Name>.appex app extensions, not 'notes.txt'"),
            "directory-in-plug-ins": ((("PlugIns/Share/Share", macho()),), "PlugIns/ holds only .* not 'Share'"),
            "extension-kit-directory": ((("Extensions/Intents.appex/Intents", macho()),), "nested code in Extensions/"),
            "extension-in-framework": (framework() + (("Frameworks/Kit.framework/PlugIns/Inner.appex/Inner", macho()),),
                                       "nested code inside a framework: Frameworks/Kit.framework/PlugIns"),
        }
        for name, (extra, message) in cases.items():
            with self.subTest(name=name):
                bundle = write_bundle(self.root / name, extra=extra)
                with self.assertRaisesRegex(ValueError, message):
                    bundles.inspect_bundle(bundle)
        # An extension signed for another team than its app is refused.
        team = {"application-identifier": "ABCDE12345.io.example.sample"}
        other = write_bundle(self.root / "other-team", program=macho(entitlements=team), extra=extension(
            entitlements={"application-identifier": "ZZZZZ99999.io.example.sample.share"}))
        with self.assertRaisesRegex(ValueError, "signed for the team ZZZZZ99999, its app Sample.app for ABCDE12345"):
            bundles.inspect_bundle(other)
        linked = write_bundle(self.root / "linked-extension", extra=extension())
        (linked / "PlugIns/Share.appex/Share").unlink()
        (linked / "PlugIns/Share.appex/Share").symlink_to("../../../outside/Share")
        with self.assertRaisesRegex(ValueError, "symbolic link or special file: PlugIns/Share.appex/Share"):
            bundles.inspect_bundle(linked)

    def test_the_command_line_inspector_answers_in_json(self):
        bundle = write_bundle(self.root)
        tool = Path(bundles.__file__)
        done = subprocess.run([sys.executable, str(tool), "inspect", str(bundle)], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        reply = json.loads(done.stdout)
        self.assertEqual((reply["ok"], reply["bundle_id"], reply["file_count"]), (True, "io.example.sample", 2))
        self.assertNotIn("files", reply)
        (bundle / "Info.plist").unlink()
        done = subprocess.run([sys.executable, str(tool), "inspect", str(bundle)], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(done.stdout)["ok"], False)
        self.assertIn("has no Info.plist", json.loads(done.stdout)["error"])


# A stand-in control plane for the start's app installs. It records every
# call and answers from a scenario keyed by operation, the guest operation and
# its argument (most specific key first); a list is consumed in order.
INSTALL_CLI = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ["FAKE_ROOT"])
operation, arguments = sys.argv[2], json.loads(sys.argv[3])
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps({"operation": operation, "arguments": arguments}) + "\n")
scenario = json.loads((root / "scenario.json").read_text())
sub, argument = arguments.get("operation") or "", arguments.get("argument") or arguments.get("request_id") or ""
key = next((k for k in (f"{operation}:{sub}:{argument}", f"{operation}:{sub}", operation) if k in scenario), None)
reply = scenario.get(key, {"ok": False, "error": {"code": "unexpected_call", "message": key or operation}})
if isinstance(reply, list):
    import hashlib
    counter = root / ("count-" + hashlib.sha1(key.encode()).hexdigest())
    index = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(index + 1))
    reply = reply[min(index, len(reply) - 1)]
if operation == "ios.emulator.prepare_guest_tools" and reply.get("ok"):
    output = Path(arguments["output"]); output.mkdir(parents=True)
    (output / "system.raw").write_bytes(b"disk")
    (output / "guest-tools-install.json").write_text(json.dumps({"state": "installed", "graphics_payload": {"state": "verified"},
        "boot_inputs": {"writable_root_disk": str(output / "system.raw"), "trustcache": str(output / "system.tc")}}))
print(json.dumps(reply))
'''


def ok(result):
    return {"ok": True, "result": result}


def installed(argument="", **extra):
    return ok({"response": {"ok": True, "operation": "install", "argument": argument, "request_accepted": True,
                            "running_application_ended": False, **extra}})


class StagedApplicationStartTests(unittest.TestCase):
    """The start installs the profile's apps from the guest-tools build that
    carries them, once per signed executable, before it opens the app."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tomacco-staged-apps-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = self.root / "profile"
        (self.profile / "installs").mkdir(parents=True)
        self.base = self.root / "base/guest-install7"
        self.base.mkdir(parents=True)
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.cli = self.root / "tomaccoctl"
        self.cli.write_text(INSTALL_CLI)
        self.cli.chmod(0o755)
        self.environment = patch.dict(os.environ, {"FAKE_ROOT": str(self.root), "TOMACCO_PARENT_JOB": "a" * 32})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        # The clock replaces the product's name `time`, not the time module's
        # functions, which subprocess uses while it waits for the stand-in
        # control plane (see product_clock).
        self.clock = ProductClock(1000.0)
        clock = patch.object(ios_virtual_iphone, "time", self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        self.events = []
        quiet = patch.object(ios_virtual_iphone, "emit", lambda phase, **details: self.events.append({"phase": phase, **details}))
        quiet.start()
        self.addCleanup(quiet.stop)
        self.first = write_bundle(self.root / "apps", "First.app", "io.example.first", "First", extra=(("Assets.car", b"first"),))
        self.second = write_bundle(self.root / "apps", "Second.app", "io.example.second", "Second")
        self.build(self.first, self.second)
        self.write_profile(applications=[{"bundle": str(self.first)}, {"bundle": str(self.second)}])
        self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)

    def build(self, *staged, cdhashes=None):
        """A guest-tools record carrying the given bundles as they are now."""
        applications = []
        for index, bundle in enumerate(staged):
            inspected = bundles.inspect_bundle(bundle)
            applications.append({"path": "Applications/" + inspected["name"], "name": inspected["name"],
                                 "bundle_id": inspected["bundle_id"], "executable": "Applications/" + inspected["name"] + "/" + inspected["executable"],
                                 "cdhash": (cdhashes or {}).get(inspected["bundle_id"], "%02x" % index * 20),
                                 "entitlements": inspected["entitlements"],
                                 "guest_path": "/System/Cryptexes/Supplemental/Applications/" + inspected["name"],
                                 "source": {"path": str(bundle), "content_sha256": inspected["content_sha256"]}})
        (self.tools / "guest-tools.json").write_text(json.dumps({"format": 1, "applications": applications}))

    def write_profile(self, **extra):
        (self.profile / "profile.json").write_text(json.dumps({
            "format": 1, "prepare": {"support": "/support", "helper_kernel": "/missing/vmlinuz", "seconds": 300},
            "boot": {"kernel": "/kernel", "seconds": 7200, "interactive": True}, "graphics": "/graphics",
            "engine": "/engine.py", "application": RESEARCH, "installs": str(self.profile / "installs"),
            "tools": str(self.tools), **extra}))

    def write_state(self, **state):
        (self.profile / "state.json").write_text(json.dumps(state))

    def state(self):
        return json.loads((self.profile / "state.json").read_text())

    def scenario(self, **replies):
        (self.root / "scenario.json").write_text(json.dumps({name.replace("__", "."): value for name, value in replies.items()}))
        for stale in list(self.root.glob("count-*")) + [self.root / "calls.jsonl"]:
            stale.unlink(missing_ok=True)

    def calls(self, operation=None):
        path = self.root / "calls.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if operation in (None, row["operation"])]

    def installs(self):
        return [row["arguments"]["argument"] for row in self.calls("ios.emulator.guest")
                if row["arguments"].get("operation") == "install"]

    def phone(self):
        return ios_virtual_iphone.Phone(str(self.cli), self.profile)

    def test_waiting_for_the_stand_in_control_plane_does_not_move_the_fixtures_clock(self):
        # As in test_ios_virtual_iphone: subprocess polls a child it waits for
        # with time.sleep, and a fake in the module itself moved the clock by
        # a random amount per call of the stand-in. One that closes its pipes
        # and lives on makes that wait certain.
        self.cli.write_text("#!/usr/bin/env python3\nimport os, time\n"
                            "print('{\"ok\": true, \"result\": {}}', flush=True)\nos.close(1)\nos.close(2)\ntime.sleep(0.3)\n")
        self.assertEqual(self.phone().invoke("job.get", {"job_id": "a" * 32}), {"ok": True, "result": {}})
        self.assertEqual(ios_virtual_iphone.time.monotonic(), 1000.0)

    def test_the_profiles_apps_must_be_carried_unchanged_by_the_guest_tools_build(self):
        phone = self.phone()
        staged = ios_virtual_iphone.staged_applications(phone.profile, self.tools)
        self.assertEqual([(app["bundle_id"], app["name"], app["guest_path"], app["cdhash"]) for app in staged],
                         [("io.example.first", "First.app", "/System/Cryptexes/Supplemental/Applications/First.app", "00" * 20),
                          ("io.example.second", "Second.app", "/System/Cryptexes/Supplemental/Applications/Second.app", "01" * 20)])
        self.assertEqual(ios_virtual_iphone.staged_applications({"application": RESEARCH}, self.tools), [])
        with self.assertRaisesRegex(RuntimeError, "applications must be a list"):
            ios_virtual_iphone.staged_applications({"applications": "x"}, self.tools)
        with self.assertRaisesRegex(RuntimeError, "no readable guest-tools.json"):
            ios_virtual_iphone.staged_applications(phone.profile, self.root / "absent-tools")
        # Not carried: say which build and how to make one.
        self.build(self.first)
        with self.assertRaisesRegex(RuntimeError, "does not carry Second.app .io.example.second.; build guest tools"):
            ios_virtual_iphone.staged_applications(phone.profile, self.tools)
        # Carried, but the bundle changed since.
        self.build(self.first, self.second)
        (self.first / "Assets.car").write_bytes(b"first, rebuilt")
        with self.assertRaisesRegex(RuntimeError, "First.app changed since the guest-tools build .* rebuild guest tools"):
            ios_virtual_iphone.staged_applications(phone.profile, self.tools)
        self.build(self.first, self.second)
        # Other entitlements than the build signed.
        requested = self.root / "first.entitlements"
        requested.write_bytes(plistlib.dumps({"get-task-allow": True}))
        profile = {**phone.profile, "applications": [{"bundle": str(self.first), "entitlements": str(requested)}]}
        with self.assertRaisesRegex(RuntimeError, "other entitlements"):
            ios_virtual_iphone.staged_applications(profile, self.tools)
        # Two bundles with one identity; the diagnostic's identity; an unreadable bundle.
        twin = write_bundle(self.root / "twin", "First.app", "io.example.first", "First")
        profile = {**phone.profile, "applications": [{"bundle": str(self.first)}, {"bundle": str(twin)}]}
        with self.assertRaisesRegex(RuntimeError, "Two app bundles use the bundle identifier io.example.first"):
            ios_virtual_iphone.staged_applications(profile, self.tools)
        probe = write_bundle(self.root / "probe", "TomaccoProbe.app", RESEARCH, "TomaccoProbe")
        with self.assertRaisesRegex(RuntimeError, "owned diagnostic app's bundle identifier"):
            ios_virtual_iphone.staged_applications({"applications": [{"bundle": str(probe)}]}, self.tools)
        (self.second / "Info.plist").unlink()
        with self.assertRaisesRegex(RuntimeError, "Second.app cannot be deployed: Second.app has no Info.plist"):
            ios_virtual_iphone.staged_applications(phone.profile, self.tools)

    def test_each_staged_app_is_installed_once_per_signed_executable(self):
        self.scenario(**{"ios__emulator__guest:install:/System/Cryptexes/Supplemental/Applications/First.app": installed("First"),
                         "ios__emulator__guest:install:/System/Cryptexes/Supplemental/Applications/Second.app": [
                             ok({"response": {"ok": False, "operation": "install", "error": "guest_operation_busy"}}),
                             installed("Second", running_application_ended=True)]})
        phone = self.phone()
        staged = ios_virtual_iphone.staged_applications(phone.profile, self.tools)
        phone.state["guest_job"] = "guest8"
        phone.install_applications("guest8", staged)
        first, second = "/System/Cryptexes/Supplemental/Applications/First.app", "/System/Cryptexes/Supplemental/Applications/Second.app"
        # The busy refusal comes before any work; asking again is safe.
        self.assertEqual(self.installs(), [first, second, second])
        self.assertEqual(self.state()["applications"], {"io.example.first": "00" * 20, "io.example.second": "01" * 20})
        self.assertEqual([event["phase"] for event in self.events], ["installing", "application", "application"])
        self.assertEqual(self.events[-1], {"phase": "application", "bundle_id": "io.example.second", "state": "installed",
                                           "cdhash": "01" * 20, "replaced_app_ended": True})
        # The same executables again: nothing to install, nothing asked.
        self.events.clear()
        self.scenario()
        phone = self.phone()
        phone.install_applications("guest8", staged)
        self.assertEqual(self.calls(), [])
        self.assertEqual([event["state"] for event in self.events], ["installed-before", "installed-before"])
        # A rebuilt first app (new cdhash) is installed again; the second is not.
        self.build(self.first, self.second, cdhashes={"io.example.first": "ab" * 20})
        self.scenario(**{"ios__emulator__guest:install:" + first: installed("First")})
        phone = self.phone()
        phone.install_applications("guest8", ios_virtual_iphone.staged_applications(phone.profile, self.tools))
        self.assertEqual(self.installs(), [first])
        self.assertEqual(self.state()["applications"], {"io.example.first": "ab" * 20, "io.example.second": "01" * 20})

    def test_a_late_install_reply_is_followed_by_its_receipt_and_a_refusal_ends_the_start(self):
        first = "/System/Cryptexes/Supplemental/Applications/First.app"
        request = "e" * 32
        self.write_profile(applications=[{"bundle": str(self.first)}])
        self.scenario(**{"ios__emulator__guest:install": {"ok": False, "error": {"code": "ios_guest_timeout", "details": {
                             "request_id": request, "request_may_have_executed": True}}},
                         "ios__emulator__guest_result:": [
                             ok({"job_id": "guest8", "request_id": request, "state": "running"}),
                             ok({"job_id": "guest8", "request_id": request, "state": "completed",
                                 "response": {"ok": True, "operation": "install", "request_accepted": True}})]})
        phone = self.phone()
        phone.install_applications("guest8", ios_virtual_iphone.staged_applications(phone.profile, self.tools))
        # Sent once; the outcome came through the receipt of that request.
        self.assertEqual(self.installs(), [first])
        self.assertEqual([row["arguments"]["request_id"] for row in self.calls("ios.emulator.guest_result")], [request] * 2)
        self.assertEqual(self.state()["applications"], {"io.example.first": "00" * 20})
        # installd's refusal is the start's error, with iOS's reason; nothing is recorded.
        self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
        self.scenario(**{"ios__emulator__guest:install": ok({"response": {
            "ok": False, "operation": "install", "error": "installation_rejected_by_ios",
            "native_error": {"domain": "MIInstallerErrorDomain", "code": 13}}})})
        phone = self.phone()
        with self.assertRaisesRegex(RuntimeError, "did not install io.example.first from .*First.app: installation_rejected_by_ios.*MIInstallerErrorDomain"):
            phone.install_applications("guest8", ios_virtual_iphone.staged_applications(phone.profile, self.tools))
        self.assertNotIn("io.example.first", self.state().get("applications") or {})
        # A guest that stops meanwhile is finalized, not asked again.
        self.write_state(run=8, install=str(self.base), tools=str(self.tools), closed=False, guest_job="guest8",
                         verified={"run": 7, "install": str(self.base), "tools": str(self.tools)})
        self.scenario(**{"ios__emulator__guest:install": {"ok": False, "error": {"code": "ios_guest_stopped", "message": "gone"}},
                         "ios__emulator__report": {"ok": False, "error": {"code": "job_not_found", "message": "no such job"}}})
        phone = self.phone()
        with self.assertRaisesRegex(RuntimeError, "guest stopped while io.example.first was being installed"):
            phone.install_applications("guest8", ios_virtual_iphone.staged_applications(phone.profile, self.tools))
        self.assertEqual(self.installs(), [first])
        self.assertEqual(self.state()["phase"], "unverified")

    def test_a_start_installs_the_apps_after_the_service_answers_and_before_the_launch(self):
        first, second = "/System/Cryptexes/Supplemental/Applications/First.app", "/System/Cryptexes/Supplemental/Applications/Second.app"
        self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None,
                         applications={"io.example.second": "01" * 20})
        self.scenario(**{
            "ios__emulator__prepare_guest_tools": ok({"id": "prepare", "state": "succeeded"}),
            "ios__emulator__paravirtual": ok({"id": "guest8", "state": "queued"}),
            "job__wait": ok({"id": "guest8", "state": "succeeded"}),
            "job__get": ok({"id": "guest8", "state": "running"}),
            # Guest tools report the adapter's activation in the status; the
            # start installs only once it is active.
            "ios__emulator__guest:status": ok({"response": {"ok": True, "worker_running": False,
                                                            "development_signing_activation": "activated"}}),
            "ios__emulator__guest:install:" + first: installed("First"),
            "ios__emulator__guest:open": ok({"response": {"request_accepted": True}}),
            "ios__emulator__control": ok({"result": {}}),
        })
        self.phone().start(None, True)
        guest = [(row["arguments"]["operation"], row["arguments"].get("argument")) for row in self.calls("ios.emulator.guest")]
        # Boot wait, the adapter's activation, then the one app the base did
        # not have, then the launch.
        self.assertEqual(guest, [("status", None), ("status", None), ("install", first), ("status", None), ("open", RESEARCH)])
        state = self.state()
        self.assertEqual((state["phase"], state["run"], state["applications"]),
                         ("launch-accepted", 8, {"io.example.first": "00" * 20, "io.example.second": "01" * 20}))
        self.assertEqual([event["phase"] for event in self.events if event["phase"] in ("preparing", "installing", "unlocking")],
                         ["preparing", "installing", "unlocking"])
        # A bundle the build does not carry is refused before any copy is made.
        third = write_bundle(self.root / "apps", "Third.app", "io.example.third", "Third")
        self.write_profile(applications=[{"bundle": str(self.first)}, {"bundle": str(third)}])
        self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
        self.scenario()
        with self.assertRaisesRegex(RuntimeError, "does not carry Third.app"):
            self.phone().start(None, True)
        self.assertEqual(self.calls(), [])
        # A profile without applications behaves as before: no install, no reading of any build record.
        (self.tools / "guest-tools.json").unlink()
        self.write_profile()
        self.scenario(**{
            "ios__emulator__prepare_guest_tools": ok({"id": "prepare", "state": "succeeded"}),
            "ios__emulator__paravirtual": ok({"id": "guest9", "state": "queued"}),
            "job__wait": ok({"id": "guest9", "state": "succeeded"}),
            "job__get": ok({"id": "guest9", "state": "running"}),
            "ios__emulator__guest:status": ok({"response": {"ok": True, "worker_running": False}}),
            "ios__emulator__guest:open": ok({"response": {"request_accepted": True}}),
            "ios__emulator__control": ok({"result": {}}),
        })
        self.phone().start(None, True)
        self.assertEqual(self.installs(), [])
        self.assertEqual(self.state()["phase"], "launch-accepted")


import build_ios_guest_tools
import prepare_ios_adapter_pin
from test_ios_adapter_pin import OLD as PINNED_ADAPTER, synthetic_kernel


def executable(path, text="#!/bin/sh\nexit 0\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


def clang(path, version="22.1.8"):
    return executable(path, f"#!/bin/sh\necho 'clang version {version}'\n")


def workspace_sources():
    """What a build from the workspace as it is now records."""
    return {name: hashlib.sha256((build_ios_guest_tools.SOURCES / name).read_bytes()).hexdigest()
            for name in ("guest-app-control.c", "guest-quiet.c")}


class GuestToolsRebuildTests(unittest.TestCase):
    """A start whose tools lack a listed app, or carry an older copy of it,
    builds a set that carries the profile's apps and, when the boot kernel
    does not pin that set's adapter, a kernel that does. The compile is a
    stand-in; the pin is the real one on a synthetic kernel."""
    setUp_staged = StagedApplicationStartTests.setUp
    build = StagedApplicationStartTests.build
    write_profile, write_state = StagedApplicationStartTests.write_profile, StagedApplicationStartTests.write_state
    state, scenario, calls = StagedApplicationStartTests.state, StagedApplicationStartTests.scenario, StagedApplicationStartTests.calls
    installs, phone = StagedApplicationStartTests.installs, StagedApplicationStartTests.phone

    def setUp(self):
        self.setUp_staged()
        self.trustcache = self.root / "system.tc"
        self.trustcache.write_bytes(b"trust cache source")
        self.toolchain = {"clang": str(clang(self.root / "bin/clang-22.1.8")),
                          "linker": str(executable(self.root / "bin/aarch64-apple-darwin-ld")),
                          "ldid": str(executable(self.root / "bin/ldid"))}
        self.kernel = self.root / "kernel/kernelcache.research.vphone600"
        self.kernel.parent.mkdir()
        self.kernel.write_bytes(synthetic_kernel())
        (self.kernel.parent / "adapter-pin.json").write_text(json.dumps({
            "scope": "owned-adapter-identity-only", "origin_sha256": prepare_ios_adapter_pin.INPUT_SHA256,
            "output_sha256": hashlib.sha256(self.kernel.read_bytes()).hexdigest()}))
        self.record(self.first)
        self.profile_with()
        home = patch.dict(os.environ, {"TOMACCO_HOME": str(self.root / "studio-home")})
        home.start()
        self.addCleanup(home.stop)
        self.builds = []
        builder = patch.object(build_ios_guest_tools, "build", self.fake_build)
        builder.start()
        self.addCleanup(builder.stop)

    def profile_with(self, **changes):
        profile = {"prepare": {"support": "/support", "helper_kernel": "/missing/vmlinuz", "seconds": 300,
                               "trustcache": str(self.trustcache)},
                   "boot": {"kernel": str(self.kernel), "seconds": 7200, "interactive": True},
                   "applications": [{"bundle": str(self.first)}, {"bundle": str(self.second)}],
                   "toolchain": self.toolchain}
        profile.update(changes)
        self.write_profile(**profile)

    def profile_json(self):
        return json.loads((self.profile / "profile.json").read_text())

    def written(self, output, bundles_staged, adapter="ab" * 20, **options):
        """A built guest-tools record as build_ios_guest_tools writes it."""
        applications = []
        for index, bundle in enumerate(bundles_staged):
            inspected = bundles.inspect_bundle(bundle)
            applications.append({"path": "Applications/" + inspected["name"], "bundle_id": inspected["bundle_id"],
                                 "name": inspected["name"], "cdhash": "%02x" % (index + 1) * 20,
                                 "entitlements": inspected["entitlements"],
                                 "guest_path": "/System/Cryptexes/Supplemental/Applications/" + inspected["name"],
                                 "source": {"path": str(bundle), "content_sha256": inspected["content_sha256"]}})
        library = output / "Supplemental/usr/libexec/libtomacco-development-signing.dylib"
        library.parent.mkdir(parents=True, exist_ok=True)
        library.write_bytes(b"adapter " + adapter.encode())
        record = {"format": 1, "scope": "ios-owned-guest-tools", "state": "built", "target": "arm64-apple-ios27.0",
                  "trustcache": {"source_sha256": hashlib.sha256(b"trust cache source").hexdigest()},
                  "sources": workspace_sources(),
                  "agent": True, "installer_connection_query": True, "application_log_access": True,
                  "application_termination": True, "time_zone_control": True, "graphics_query_access": False,
                  "quiet_services": True, "virtual_battery": True, "applications": applications,
                  "programs": [{"path": "usr/libexec/libtomacco-development-signing.dylib", "architecture": "arm64e",
                                "cdhash": adapter, "sha256": hashlib.sha256(library.read_bytes()).hexdigest()}],
                  "development_signing": {"policy_table_sha256": "11" * 32}}
        record.update(options)
        (output / "guest-tools.json").write_text(json.dumps(record))

    def record(self, *staged, **options):
        self.written(self.tools, staged, adapter=PINNED_ADAPTER, **options)

    def fake_build(self, arguments):
        """The compile: the record of a set carrying the given apps, whose
        adapter cdhash follows from the app set as the policy table makes it."""
        self.builds.append(arguments)
        arguments.output.mkdir()
        adapter = hashlib.sha1(json.dumps(sorted(arguments.applications)).encode()).hexdigest()
        self.written(arguments.output, [Path(bundle) for bundle in arguments.applications], adapter=adapter)

    def booting(self, guest="guest8"):
        self.scenario(**{
            "ios__emulator__prepare_guest_tools": ok({"id": "prepare", "state": "succeeded"}),
            "ios__emulator__paravirtual": ok({"id": guest, "state": "queued"}),
            "job__wait": ok({"id": guest, "state": "succeeded"}),
            "job__get": ok({"id": guest, "state": "running"}),
            "ios__emulator__guest:status": ok({"response": {"ok": True, "worker_running": False,
                                                            "development_signing_activation": "activated"}}),
            "ios__emulator__guest:install": installed("App"),
            "ios__emulator__guest:open": ok({"response": {"request_accepted": True}}),
            "ios__emulator__control": ok({"result": {}}),
        })

    def test_a_start_rebuilds_tools_that_lack_a_listed_app_and_pins_their_adapter(self):
        before = (self.profile / "profile.json").read_text()
        self.booting()
        self.phone().start(None, True)
        # One build, with the options of the profile's tools and every listed app.
        self.assertEqual(len(self.builds), 1)
        built = self.builds[0]
        output = built.output
        self.assertEqual(output.parent, self.profile / "guest-tools")
        self.assertRegex(output.name, r"^guest-tools-[0-9a-f]{16}$")
        self.assertEqual((built.agent, built.application_probe, built.development_signing, built.quiet_services,
                          built.graphics_query_access, built.minimum_ios, built.allow_unqualified_compiler),
                         (True, True, True, True, False, "27.0", False))
        # The virtual battery of the replaced build is kept (it is not a default of build()).
        self.assertIs(built.virtual_battery, True)
        self.assertEqual((built.trustcache, built.clang, str(built.linker), str(built.ldid)),
                         (self.trustcache, self.toolchain["clang"], self.toolchain["linker"], self.toolchain["ldid"]))
        self.assertEqual((built.applications, built.application_entitlements),
                         ([str(self.first), str(self.second)], ["", ""]))
        # The rebuilt adapter's cdhash is pinned in a kernel derived from the boot kernel.
        adapter = ios_virtual_iphone.development_library_cdhash(output)
        kernel = self.profile / f"kernels/adapter-pin-{adapter}/kernelcache.research.vphone600"
        self.assertTrue(ios_virtual_iphone.kernel_pins_cdhash(kernel, adapter))
        self.assertFalse(ios_virtual_iphone.kernel_pins_cdhash(kernel, PINNED_ADAPTER))
        # The profile names both, its previous content kept beside it.
        profile = self.profile_json()
        self.assertEqual((profile["tools"], profile["boot"]["kernel"]), (str(output), str(kernel)))
        self.assertEqual((self.profile / f"profile.json.before-{output.name}").read_text(), before)
        self.assertEqual(profile["applications"], json.loads(before)["applications"])
        # The start prepares and boots with them and installs both apps.
        self.assertEqual(self.calls("ios.emulator.prepare_guest_tools")[0]["arguments"]["tools"], str(output))
        self.assertEqual(self.calls("ios.emulator.paravirtual")[0]["arguments"]["kernel"], str(kernel))
        self.assertEqual(self.installs(), ["/System/Cryptexes/Supplemental/Applications/First.app",
                                           "/System/Cryptexes/Supplemental/Applications/Second.app"])
        self.assertEqual((self.state()["phase"], self.state()["tools"]), ("launch-accepted", str(output)))
        progress = [(event["phase"], event.get("state")) for event in self.events
                    if event["phase"] in ("guest-tools", "kernel", "profile", "preparing")]
        self.assertEqual(progress, [("guest-tools", "building"), ("guest-tools", "built"), ("kernel", "pinning"),
                                    ("kernel", "pinned"), ("profile", None), ("preparing", None)])
        self.assertIn("does not carry Second.app", self.events[0]["reason"])

    def test_a_set_built_from_the_same_inputs_is_reused_and_a_changed_app_is_built_again(self):
        original = (self.profile / "profile.json").read_text()
        self.booting()
        self.phone().start(None, True)
        first = self.builds[0].output
        # Back to the profile's old tools: the same inputs name the same set and kernel.
        (self.profile / "profile.json").write_text(original)
        self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
        self.events.clear()
        self.booting()
        self.phone().start(None, True)
        self.assertEqual(len(self.builds), 1)
        self.assertEqual([(event["phase"], event["state"]) for event in self.events
                          if event["phase"] in ("guest-tools", "kernel")], [("guest-tools", "reused"), ("kernel", "reused")])
        self.assertEqual(self.profile_json()["tools"], str(first))
        self.assertTrue((self.profile / f"profile.json.before-{first.name}-2").is_file())
        # The tools now carry both apps: the next start builds nothing.
        self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
        self.booting()
        self.phone().start(None, True)
        self.assertEqual(len(self.builds), 1)
        # A rebuilt bundle is another input: a new set, from the rebuilt set's kernel.
        (self.first / "Assets.car").write_bytes(b"first, rebuilt")
        self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
        self.booting()
        self.phone().start(None, True)
        self.assertEqual(len(self.builds), 2)
        self.assertNotEqual(self.builds[1].output, first)
        self.assertEqual(self.profile_json()["tools"], str(self.builds[1].output))

    def test_a_changed_embedded_framework_is_a_changed_app_and_is_built_again(self):
        for relative, data in framework() + (("Frameworks/libSupport.dylib", macho(kind=6)),):
            (self.first / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.first / relative).write_bytes(data)
        self.booting()
        self.phone().start(None, True)
        self.assertEqual(len(self.builds), 1)
        # The tools now carry both apps as they are: nothing to build.
        self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
        self.booting()
        self.phone().start(None, True)
        self.assertEqual(len(self.builds), 1)
        # The content digest the start compares covers each embedded binary.
        for relative in ("Frameworks/Kit.framework/Kit", "Frameworks/libSupport.dylib"):
            with self.subTest(relative=relative):
                (self.first / relative).write_bytes((self.first / relative).read_bytes() + b"\0")
                before = len(self.builds)
                self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
                self.booting()
                self.phone().start(None, True)
                self.assertEqual(len(self.builds), before + 1)
                self.assertEqual(self.profile_json()["tools"], str(self.builds[-1].output))

    def test_a_changed_app_extension_is_a_changed_app_and_is_built_again(self):
        identifier = plistlib.loads((self.first / "Info.plist").read_bytes())["CFBundleIdentifier"]
        for relative, data in extension(bundle_id=identifier + ".share"):
            (self.first / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.first / relative).write_bytes(data)
        self.booting()
        self.phone().start(None, True)
        self.assertEqual(len(self.builds), 1)
        self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
        self.booting()
        self.phone().start(None, True)
        self.assertEqual(len(self.builds), 1)
        # The extension's executable and Info.plist are part of the app's content.
        for relative in ("PlugIns/Share.appex/Share", "PlugIns/Share.appex/Info.plist"):
            with self.subTest(relative=relative):
                data = (self.first / relative).read_bytes()
                (self.first / relative).write_bytes(
                    plistlib.dumps({**plistlib.loads(data), "CFBundleVersion": "2"}) if relative.endswith(".plist")
                    else data + b"\0")
                before = len(self.builds)
                self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
                self.booting()
                self.phone().start(None, True)
                self.assertEqual(len(self.builds), before + 1)

    def test_a_record_from_before_the_auto_lock_grant_is_rebuilt_with_it(self):
        self.record(self.first)
        record = json.loads((self.tools / "guest-tools.json").read_text())
        self.assertNotIn("auto_lock_control", record)
        options = ios_virtual_iphone.rebuild_options(record)
        self.assertEqual(ios_virtual_iphone.rebuild_options({**record, "auto_lock_control": True}), options)
        for claimed in (False, None, "yes"):
            with self.subTest(claimed=claimed), self.assertRaisesRegex(ValueError, "records auto_lock_control as"):
                ios_virtual_iphone.rebuild_options({**record, "auto_lock_control": claimed})
        # The rebuild itself runs with the agent, which the builder grants it with.
        self.booting()
        self.phone().start(None, True)
        self.assertIs(self.builds[0].agent, True)

    def carry_every_app(self, **options):
        """Tools that carry the profile's apps as they are, so that no bundle
        asks for a rebuild, built from older guest sources."""
        self.record(self.first, self.second, **{
            "sources": {**workspace_sources(), "guest-app-control.c": "00" * 32}, **options})

    def test_tools_from_older_guest_sources_are_rebuilt_though_every_app_is_unchanged(self):
        self.carry_every_app()
        before = self.tools
        self.booting()
        self.phone().start(None, True)
        self.assertEqual(len(self.builds), 1)
        output = self.builds[0].output
        self.assertEqual(output.parent, self.profile / "guest-tools")
        self.assertEqual((self.builds[0].applications, self.builds[0].minimum_ios),
                         ([str(self.first), str(self.second)], "27.0"))
        # The same path as a changed app: the adapter is pinned, the profile
        # names both and the start boots with them.
        adapter = ios_virtual_iphone.development_library_cdhash(output)
        kernel = self.profile / f"kernels/adapter-pin-{adapter}/kernelcache.research.vphone600"
        self.assertEqual((self.profile_json()["tools"], self.profile_json()["boot"]["kernel"]), (str(output), str(kernel)))
        self.assertEqual(self.calls("ios.emulator.prepare_guest_tools")[0]["arguments"]["tools"], str(output))
        self.assertEqual(self.state()["tools"], str(output))
        building = next(event for event in self.events if event.get("state") == "building")
        self.assertEqual(building["sources_changed"], ["guest-app-control.c"])
        self.assertIn(f"differ from {before}", building["reason"])
        self.assertIn("guest-app-control.c", building["reason"])
        # The rebuilt set matches the workspace: the next start builds nothing.
        self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
        self.events.clear()
        self.booting()
        self.phone().start(None, True)
        self.assertEqual(len(self.builds), 1)
        self.assertEqual([event for event in self.events if event["phase"] == "guest-tools"], [])

    def test_a_record_that_never_matches_the_workspace_is_not_built_again_and_again(self):
        self.carry_every_app()
        original = self.fake_build

        def always_old(arguments):
            original(arguments)
            record = json.loads((arguments.output / "guest-tools.json").read_text())
            record["sources"]["guest-app-control.c"] = "11" * 32
            (arguments.output / "guest-tools.json").write_text(json.dumps(record))
        with patch.object(build_ios_guest_tools, "build", always_old):
            self.booting()
            self.phone().start(None, True)
            self.assertEqual([event["sources_changed"] for event in self.events if event.get("state") == "differs"],
                             [["guest-app-control.c"]])
            for _start in range(2):
                self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
                self.booting()
                self.phone().start(None, True)
            self.assertEqual(len(self.builds), 1)
            self.assertEqual(self.state()["phase"], "launch-accepted")

    def test_tools_that_cannot_follow_the_guest_sources_are_kept_and_the_start_goes_on(self):
        before = (self.profile / "profile.json").read_text()
        cases = (
            # No toolchain is named or found: the old tools still boot.
            (lambda: patch.object(ios_virtual_iphone, "guest_toolchain", side_effect=ValueError("no clang 22 (looked at: clang-22)")),
             "The start cannot rebuild it by itself: no clang 22"),
            (lambda: patch.object(build_ios_guest_tools, "build", side_effect=subprocess.CalledProcessError(1, "clang")),
             "rebuild into .* failed"),
            (lambda: patch.object(ios_virtual_iphone.Phone, "pinned_kernel", side_effect=RuntimeError("no pin can be derived")),
             "no pin can be derived"),
        )
        for stand_in, reason in cases:
            with self.subTest(reason=reason):
                self.carry_every_app()
                (self.profile / "profile.json").write_text(before)
                self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
                shutil.rmtree(self.profile / "guest-tools", ignore_errors=True)
                self.events.clear()
                self.booting()
                with stand_in():
                    self.phone().start(None, True)
                kept = [event for event in self.events if event.get("state") == "kept"]
                self.assertEqual(len(kept), 1)
                self.assertEqual(kept[0]["tools"], str(self.tools))
                self.assertRegex(kept[0]["reason"], reason)
                self.assertEqual((self.profile / "profile.json").read_text(), before)
                self.assertEqual(self.calls("ios.emulator.prepare_guest_tools")[0]["arguments"]["tools"], str(self.tools))
                self.assertEqual(self.state()["phase"], "launch-accepted")

    def test_a_failed_compile_of_the_guest_sources_is_not_tried_again_until_an_input_changes(self):
        self.carry_every_app()
        attempts = []

        def failing(arguments):
            attempts.append(arguments)
            raise ValueError("guest-setup.h: compile error")
        with patch.object(build_ios_guest_tools, "build", failing):
            self.booting()
            self.phone().start(None, True)
            self.assertEqual(len(attempts), 1)
            state = self.state()
            self.assertRegex(state["guest_tools_failed"]["error"], "compile error")
            # The next start reports it without compiling.
            self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None,
                             guest_tools_failed=state["guest_tools_failed"])
            self.events.clear()
            self.booting()
            self.phone().start(None, True)
            self.assertEqual(len(attempts), 1)
            self.assertRegex(next(event["reason"] for event in self.events if event.get("state") == "kept"),
                             "same inputs failed before: guest-setup.h: compile error")
            # Another input (here an app) is another set, so another attempt.
            self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None,
                             guest_tools_failed=state["guest_tools_failed"])
            (self.first / "Assets.car").write_bytes(b"first, rebuilt")
            self.record(self.first, self.second, sources={**workspace_sources(), "guest-app-control.c": "00" * 32})
            self.booting()
            self.phone().start(None, True)
            self.assertEqual(len(attempts), 2)

    def test_tools_named_for_one_start_are_never_replaced_for_older_guest_sources(self):
        self.carry_every_app()
        before = (self.profile / "profile.json").read_text()
        self.booting()
        self.phone().start(self.tools, True)
        self.assertEqual(self.builds, [])
        self.assertEqual((self.profile / "profile.json").read_text(), before)
        self.assertEqual(self.calls("ios.emulator.prepare_guest_tools")[0]["arguments"]["tools"], str(self.tools))

    def test_tools_without_a_usable_comparison_ask_for_no_rebuild(self):
        # A source the product dropped counts as changed; a workspace with
        # none of the recorded sources has nothing to compare with.
        self.carry_every_app(sources={"dropped.h": "00" * 32, **workspace_sources()})
        self.assertEqual(ios_virtual_iphone.changed_guest_sources(self.tools), ["dropped.h"])
        self.carry_every_app(sources={"dropped.h": "00" * 32})
        self.assertEqual(ios_virtual_iphone.changed_guest_sources(self.tools), [])
        self.carry_every_app(sources=None)
        self.assertEqual(ios_virtual_iphone.changed_guest_sources(self.tools), [])
        self.assertEqual(ios_virtual_iphone.changed_guest_sources(self.root / "no-such-build"), [])

    def test_a_kernel_that_already_pins_the_rebuilt_adapter_is_kept(self):
        with patch.object(ios_virtual_iphone, "kernel_pins_cdhash", lambda kernel, cdhash: True):
            self.booting()
            self.phone().start(None, True)
        self.assertEqual(self.profile_json()["boot"]["kernel"], str(self.kernel))
        self.assertFalse((self.profile / "kernels").exists())

    def test_what_cannot_be_rebuilt_is_refused_before_any_work_and_changes_nothing(self):
        before = (self.profile / "profile.json").read_text()
        cases = (
            (lambda: self.record(self.first, development_signing=None), "built without development signing"),
            (lambda: self.record(self.first, development_signing={}), "no policy table"),
            (lambda: self.record(self.first, experiment_probe_library=True), "an experiment build"),
            (lambda: self.record(self.first, time_zone_control=False), "records time_zone_control as False"),
            # A record from before the auto-lock grant has no key and is rebuilt
            # with it; one recording it otherwise was made by another builder.
            (lambda: self.record(self.first, auto_lock_control=False), "records auto_lock_control as False"),
            (lambda: self.trustcache.write_bytes(b"another trust cache"), "trust cache .* is not the one"),
            (lambda: (self.profile / "profile.json").write_text(json.dumps(
                {**json.loads(before), "toolchain": {**self.toolchain, "clang": str(clang(self.root / "bin/clang-23", "23.1.3"))}})),
             "toolchain.clang names .*clang-23, which is not an executable clang 22"),
        )
        for prepare, reason in cases:
            with self.subTest(reason=reason):
                self.record(self.first)
                self.trustcache.write_bytes(b"trust cache source")
                (self.profile / "profile.json").write_text(before)
                prepare()
                self.scenario()
                with self.assertRaisesRegex(RuntimeError, "does not carry Second.app .* The start cannot rebuild it by itself: .*" + reason):
                    self.phone().start(None, True)
                self.assertEqual((self.builds, self.calls()), ([], []))
                self.assertFalse((self.profile / "guest-tools").exists())
        (self.profile / "profile.json").write_text(before)
        self.record(self.first)
        self.trustcache.write_bytes(b"trust cache source")
        # A build named for this start only is never replaced.
        with self.assertRaisesRegex(RuntimeError, "does not carry Second.app .*; build guest tools"):
            self.phone().start(self.tools, True)
        self.assertEqual(self.builds, [])
        self.assertEqual(list(self.profile.glob("profile.json.before-*")), [])

    def test_a_kernel_no_pin_can_be_derived_from_leaves_the_profile_as_it_was(self):
        before = (self.profile / "profile.json").read_text()
        (self.kernel.parent / "adapter-pin.json").unlink()
        self.scenario()
        with self.assertRaisesRegex(RuntimeError, "does not pin the development-signing adapter's cdhash .* no kernel "
                                                  "pinning it can be derived from it: .*exact recorded reference kernel"):
            self.phone().start(None, True)
        self.assertEqual(((self.profile / "profile.json").read_text(), self.calls()), (before, []))
        # The built set stays for the next start, which needs no second build.
        self.assertEqual(len(self.builds), 1)
        self.assertTrue(self.builds[0].output.is_dir())

    def test_the_toolchain_is_the_configured_one_else_the_recorded_then_the_workspaces(self):
        record = {"toolchain": {}}
        workspace = self.root / "workspace"
        executable(workspace / ios_virtual_iphone.TOOLCHAIN_DEFAULTS["linker"])
        executable(workspace / ios_virtual_iphone.TOOLCHAIN_DEFAULTS["ldid"])
        path = self.root / "path"
        with patch.object(ios_virtual_iphone, "REPOSITORY", workspace), patch.dict(os.environ, {"PATH": str(path)}):
            # Nothing configured or recorded and no clang 22 on PATH: name what was looked for.
            clang(path / "clang", "23.1.3")
            with self.assertRaisesRegex(ValueError, r"no clang 22 \(looked at: clang-22, clang\)\. Name them in profile.json"):
                ios_virtual_iphone.guest_toolchain({}, record)
            clang(path / "clang-22")
            self.assertEqual(ios_virtual_iphone.guest_toolchain({}, record),
                             {"clang": str(path / "clang-22"),
                              "linker": str(workspace / ios_virtual_iphone.TOOLCHAIN_DEFAULTS["linker"]),
                              "ldid": str(workspace / ios_virtual_iphone.TOOLCHAIN_DEFAULTS["ldid"])})
            # The record of the build being replaced comes before the workspace.
            record = {"toolchain": self.toolchain}
            self.assertEqual(ios_virtual_iphone.guest_toolchain({}, record), self.toolchain)
            # settings.json executables are used as named; one that is not there is refused, not passed over.
            settings = self.root / "studio-home/settings.json"
            settings.parent.mkdir()
            settings.write_text(json.dumps({"executables": {"ldid": str(self.root / "missing/ldid")}}))
            with self.assertRaisesRegex(ValueError, "settings.json executables.ldid names .*missing/ldid, which is not an executable"):
                ios_virtual_iphone.guest_toolchain({}, record)
            settings.unlink()
            (workspace / ios_virtual_iphone.TOOLCHAIN_DEFAULTS["linker"]).unlink()
            with self.assertRaisesRegex(ValueError, "no linker \\(looked at: .*aarch64-apple-darwin-ld\\)"):
                ios_virtual_iphone.guest_toolchain({}, {})
            with self.assertRaisesRegex(ValueError, "toolchain must be an object"):
                ios_virtual_iphone.guest_toolchain({"toolchain": "clang"}, {})
        # An installed copy has no .tooling of its own: the workspace holding the build has.
        installed, checkout = self.root / "installed", self.root / "checkout"
        installed.mkdir()
        executable(checkout / ios_virtual_iphone.TOOLCHAIN_DEFAULTS["linker"])
        executable(checkout / ios_virtual_iphone.TOOLCHAIN_DEFAULTS["ldid"])
        build = checkout / "qa/platform/guest-tools"
        build.mkdir(parents=True)
        with patch.object(ios_virtual_iphone, "REPOSITORY", installed), patch.dict(os.environ, {"PATH": str(path)}):
            with self.assertRaisesRegex(ValueError, "no linker"):
                ios_virtual_iphone.guest_toolchain({}, {})
            self.assertEqual(ios_virtual_iphone.guest_toolchain({}, {}, build),
                             {"clang": str(path / "clang-22"),
                              "linker": str(checkout / ios_virtual_iphone.TOOLCHAIN_DEFAULTS["linker"]),
                              "ldid": str(checkout / ios_virtual_iphone.TOOLCHAIN_DEFAULTS["ldid"])})
