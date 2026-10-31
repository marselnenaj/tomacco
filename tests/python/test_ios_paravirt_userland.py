"""Restore selection boundaries; synthetic fixtures are not boot evidence."""
import copy
import hashlib
import json
import plistlib
import unittest
from unittest.mock import patch

import test_ios_userland
from test_ios_paravirt_firmware import im4p
import prepare_ios_userland as importer
import ios_paravirt_seal as seal
from ios_paravirt_firmware import FirmwareBundle
from ios_paravirt_userland import OS_COMPONENTS, attach_userland


class RestoreUserlandTests(unittest.TestCase):
    def setUp(self):
        fixture = test_ios_userland.UserlandTests()
        fixture.setUp()
        self.addCleanup(fixture.temp.cleanup)
        self.args = fixture.arguments()
        root = self.args.pcc_firmware
        original = plistlib.loads((root / "BuildManifest.plist").read_bytes())
        identity = original["BuildIdentities"][0]
        identity.update(ApChipID="0xfe01", ApBoardID="0x91")
        kernel = im4p(b"krnl", b"synthetic PCC kernel")
        (root / "kernel.im4p").write_bytes(kernel)
        identity["Manifest"]["KernelCache"] = {
            "Info": {"Path": "kernel.im4p"}, "Digest": hashlib.sha384(kernel).digest()}
        (root / "BuildManifest.plist").write_bytes(plistlib.dumps(original))
        with patch.object(importer, "extract_gpu", return_value={"synthetic": True}):
            self.record = importer.prepare(self.args)
        self.bundle = FirmwareBundle(root, 0)

    def save_receipt(self):
        (self.args.output / "userland.json").write_text(json.dumps(self.record))

    def test_selects_phone_os_but_preserves_kernel_hardware_and_source_manifests(self):
        original = copy.deepcopy(self.bundle.identity)
        manifests = {p: p.read_bytes() for p in self.args.output.glob("*-BuildManifest.plist")}
        path = attach_userland(self.bundle, self.args.output)
        self.assertEqual(path, self.args.output / "images/system.dmg")
        for key in ("Info", "ApChipID", "ApBoardID"):
            self.assertEqual(self.bundle.identity[key], original[key])
        self.assertEqual(self.bundle.components["KernelCache"], original["Manifest"]["KernelCache"])
        phone = plistlib.loads(manifests[self.args.output / "ios-BuildManifest.plist"])
        for name in OS_COMPONENTS:
            self.assertEqual(self.bundle.components[name], phone["BuildIdentities"][0]["Manifest"][name])
        for name in OS_COMPONENTS[1:]:
            self.assertTrue(self.bundle.source_component(name)[2]["manifest_digest_matched"])
        self.assertEqual(manifests, {p: p.read_bytes() for p in manifests})
        self.assertFalse(self.bundle.version["ios_userland"]["signature_verified"])
        self.assertFalse(self.bundle.version["ios_userland"]["cryptexes_installed"])

    def test_different_pcc_identity_and_incomplete_import_are_rejected(self):
        self.record["pcc"]["identity_index"] += 1
        self.save_receipt()
        with self.assertRaisesRegex(ValueError, "different PCC"):
            attach_userland(self.bundle, self.args.output)
        self.record["state"] = "failed"
        self.save_receipt()
        with self.assertRaisesRegex(ValueError, "completed"):
            attach_userland(self.bundle, self.args.output)

    def test_changed_metadata_and_symlinks_are_rejected(self):
        path = self.args.output / "components/SystemVolume.bin"
        original = path.read_bytes()
        path.write_bytes(original[:-1] + b"X")
        with self.assertRaisesRegex(ValueError, "digest"):
            attach_userland(self.bundle, self.args.output)
        path.unlink()
        path.symlink_to(self.args.firmware / "SystemVolume.bin")
        with self.assertRaises(OSError):
            attach_userland(self.bundle, self.args.output)

    def test_unknown_phone_manifest_and_override_cannot_change_research_policy(self):
        self.bundle.overrides["SystemVolume"] = "other"
        with self.assertRaisesRegex(ValueError, "cannot be replaced"):
            attach_userland(self.bundle, self.args.output)
        with patch.object(seal, "BUILD_MANIFEST_SHA256", self.bundle.version["build_manifest_sha256"]):
            with self.assertRaisesRegex(ValueError, "exact original 24A446"):
                seal.prepare_policy(self.args.pcc_firmware, 0, "must-not-read", {}, self.args.output)

    def test_changed_phone_manifest_and_forged_component_receipt_are_rejected(self):
        self.record["components"]["OS"]["sha384"] = "00" * 48
        self.save_receipt()
        with self.assertRaisesRegex(ValueError, "original manifest"):
            attach_userland(self.bundle, self.args.output)
        self.record["ios"]["build_manifest_sha256"] = "00" * 32
        self.save_receipt()
        with self.assertRaisesRegex(ValueError, "no longer matches"):
            attach_userland(self.bundle, self.args.output)


if __name__ == "__main__":
    unittest.main()
