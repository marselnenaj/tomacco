"""Firmware-free validation of the loader's shared C bundle parser."""
from pathlib import Path
import copy
import hashlib
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import prepare_ios_exclave as prepare


class ExclaveBundleTests(unittest.TestCase):
    def test_bundle_memory_bounds(self):
        compiler = shutil.which("cc")
        self.assertIsNotNone(compiler, "The emulator tests require a C compiler")
        with tempfile.TemporaryDirectory(prefix="tomacco-cl4-test-") as directory:
            program = Path(directory) / "cl4-test"
            subprocess.run([compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=undefined", "-fno-sanitize-recover=all",
                            "-I", str(ROOT / "integrations/ios-emulator"),
                            str(ROOT / "tests/fixtures/ios_exclave_bundle_test.c"), "-o", str(program)],
                           check=True, capture_output=True, text=True, timeout=30)
            result = subprocess.run([program], check=True, capture_output=True, text=True, timeout=10)
            self.assertIn("permissions and entry guards passed", result.stdout)


class ExclavePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tomacco-cl4-prepare-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.firmware = self.root / "firmware"
        self.firmware.mkdir()
        self.output = self.root / "prepared"
        self.manifest = self.root / "BuildManifest.plist"
        self.ipsw = self.root / "ipsw"
        self.ipsw.write_text("#!/bin/sh\nexit 99\n")
        self.ipsw.chmod(0o700)
        image = bytearray(0x10000)
        image[8:20] = b"DNUB\0\0\x04\0\x01\0\0\0"
        struct.pack_into("<I", image, 20, 4)
        struct.pack_into("<Q", image, 32, 0x4000)
        struct.pack_into("<Q", image, 72, 0x4000)
        for index, (flags, name) in enumerate(((10, b"ktxt"), (14, b"utxt"), (17, b"kdat"), (21, b"ubdl"))):
            struct.pack_into("<QQI4s", image, 0x270 + index * 24, index * 0x4000, 0x4000, flags, name[::-1])
        self.payloads = {"cl4": bytes(image), "integrity": b"iCat" + bytes(28),
                         "trustcache": struct.pack("<I16sI", 1, bytes(16), 1) + bytes(22)}
        entries = {}
        for name, key in prepare.COMPONENTS.items():
            container = b"synthetic container: " + name.encode()
            (self.firmware / (name + ".im4p")).write_bytes(container)
            entries[key] = {"Info": {"Path": name + ".im4p"}, "Digest": hashlib.sha384(container).digest()}
        self.document = {"ProductVersion": "27.0.1", "ProductBuildVersion": "24A446",
                         "BuildIdentities": [{"Info": {"DeviceClass": "d47ap", "Variant": "Customer Erase Install (IPSW)"},
                                              "ApChipID": "0x8140", "Manifest": entries}]}
        self.save_manifest()
        self.inputs = {p: p.read_bytes() for p in self.firmware.iterdir()}

    def save_manifest(self):
        self.manifest.write_bytes(plistlib.dumps(self.document))

    def extract(self, command, **kwargs):
        self.assertEqual(command[1:4], ["img4", "im4p", "extract"])
        copied, target = Path(command[4]), Path(command[6])
        self.assertEqual(copied.read_bytes(), self.inputs[self.firmware / (target.name + ".im4p")])
        target.write_bytes(self.payloads[target.name])
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def run_prepare(self, extract=None):
        with patch.object(prepare.subprocess, "run", side_effect=extract or self.extract):
            return prepare.prepare(self.firmware, self.manifest, "d47ap", self.ipsw, self.output)

    def assert_clean_failure(self, message, extract=None):
        with self.assertRaisesRegex(ValueError, message):
            self.run_prepare(extract)
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob(".tomacco-cl4-*")))

    def test_verified_bundle_is_published_readonly_and_inputs_preserved(self):
        record = self.run_prepare()
        self.assertTrue(record["manifest_digests_verified"])
        self.assertFalse(record["secure_services_qualified"])
        self.assertFalse(record["graphical_apps_qualified"])
        self.assertEqual(record["layout"]["rx_bytes"], 0x8000)
        self.assertEqual(record["layout"]["rw_bytes"], 0x8000)
        for name, payload in self.payloads.items():
            path = self.output / name
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(path.stat().st_mode & 0o777, 0o400)
            self.assertEqual(record["components"][name]["sha256"], hashlib.sha256(payload).hexdigest())
        for path, data in self.inputs.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertEqual((self.output / "BuildManifest.plist").read_bytes(), self.manifest.read_bytes())

    def test_existing_output_and_symlink_are_preserved(self):
        self.output.mkdir()
        (self.output / "user-file").write_text("keep")
        with self.assertRaisesRegex(ValueError, "Output exists"):
            self.run_prepare()
        self.assertEqual((self.output / "user-file").read_text(), "keep")
        link = self.root / "link"
        link.symlink_to(self.output, target_is_directory=True)
        self.output = link
        with self.assertRaisesRegex(ValueError, "Output exists"):
            self.run_prepare()
        self.assertTrue(link.is_symlink())

    def test_manifest_digest_is_checked_before_extraction(self):
        (self.firmware / "cl4.im4p").write_bytes(b"corrupt")
        with patch.object(prepare.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                prepare.prepare(self.firmware, self.manifest, "d47ap", self.ipsw, self.output)
            run.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_component_paths_cannot_escape_firmware(self):
        escaped = self.root / "outside.im4p"
        escaped.write_bytes(self.inputs[self.firmware / "cl4.im4p"])
        entry = self.document["BuildIdentities"][0]["Manifest"]["Ap,cL4"]
        for relative in ("../outside.im4p", str(escaped), "nested/../../outside.im4p"):
            with self.subTest(path=relative):
                entry["Info"]["Path"] = relative
                self.save_manifest()
                self.assert_clean_failure("escapes firmware")
        (self.firmware / "outside.im4p").symlink_to(escaped)
        entry["Info"]["Path"] = "outside.im4p"
        self.save_manifest()
        self.assert_clean_failure("outside firmware")

    def test_identity_must_be_unique_and_match_hardware(self):
        identity = copy.deepcopy(self.document["BuildIdentities"][0])
        for identities in ([], [identity, identity], [{**identity, "ApChipID": "0x8120"}]):
            with self.subTest(identities=len(identities)):
                self.document["BuildIdentities"] = identities
                self.save_manifest()
                self.assert_clean_failure("matching T8140")

    def test_failed_extractor_leaves_no_bundle(self):
        def fail(command, **kwargs):
            Path(command[6]).write_bytes(b"partial")
            return subprocess.CompletedProcess(command, 1, b"", b"extract failed")
        self.assert_clean_failure("Could not unwrap cl4", fail)

    def test_timeout_cleans_temporary_payloads(self):
        def fail(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 120)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_prepare(fail)
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob(".tomacco-cl4-*")))

    def test_sources_changed_during_extraction_are_rejected(self):
        def mutate(command, **kwargs):
            result = self.extract(command, **kwargs)
            if Path(command[6]).name == "trustcache":
                (self.firmware / "cl4.im4p").write_bytes(b"changed")
            return result
        self.assert_clean_failure("changed during preparation", mutate)

    def test_manifest_changed_during_extraction_is_rejected(self):
        def mutate(command, **kwargs):
            result = self.extract(command, **kwargs)
            self.manifest.write_bytes(b"changed")
            return result
        self.assert_clean_failure("changed during preparation", mutate)

    def test_invalid_payloads_are_not_published(self):
        for name, payload, error in (("trustcache", bytes(24), "trust-cache"),
                                     ("trustcache", self.payloads["trustcache"][:-1], "trust-cache"),
                                     ("integrity", b"bad!", "integrity catalog"),
                                     ("cl4", self.payloads["cl4"][:-1], "page-aligned")):
            with self.subTest(component=name, size=len(payload)):
                with patch.dict(self.payloads, {name: payload}):
                    self.assert_clean_failure(error)

    def test_range_gaps_overlaps_order_and_header_disagreement_are_rejected(self):
        for at, fmt, value in ((20, "I", 17), (0x288, "Q", 0), (0x288, "Q", 0x8000),
                               (0x280, "I", 3), (32, "Q", 0x8000), (72, "Q", 0x8000),
                               (0x284 + 24, "I", 0x6b747874), (0x284 + 48, "I", 0),
                               (0x280 + 24, "I", 17)):
            with self.subTest(offset=at, value=value):
                image = bytearray(self.payloads["cl4"])
                struct.pack_into("<" + fmt, image, at, value)
                with patch.dict(self.payloads, {"cl4": bytes(image)}):
                    self.assert_clean_failure("ExclaveCore")


if __name__ == "__main__":
    unittest.main()
