"""The up-front check of the person's own inputs explains each mismatch in plain words.

The firmware directories, disk images, template and IM4M here are small synthetic fakes. The pinned
manifest digests cannot be met by a fake, so the tests pin the digests of their own fake manifests."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import plistlib
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import check_ios_inputs as inputs  # noqa: E402
import prepare_ios_research_manifest as research  # noqa: E402
from test_ios_paravirt import node  # noqa: E402
from test_ios_paravirt_manifest import ticket_for_image  # noqa: E402

APFS = bytes(32) + b"NXSB" + bytes(12)
ECID = 0x1122334455667788


def component(path, seed):
    return {"Info": {"Path": path}, "Digest": hashlib.sha384(seed.encode()).digest()}


def write_firmware(root, build, devices, identities):
    root.mkdir(parents=True)
    manifest = {"ProductBuildVersion": build, "ProductVersion": "x", "SupportedProductTypes": devices,
                "BuildIdentities": [{"Manifest": entries} for entries in identities]}
    data = plistlib.dumps(manifest)
    (root / "BuildManifest.plist").write_bytes(data)
    for entries in identities:
        for entry in entries.values():
            target = root / entry["Info"]["Path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"component")
    return hashlib.sha256(data).hexdigest()


class InputCheckTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.iphone_components = {"OS": component("043-1.dmg.aea", "os"),
                                  "Cryptex1,SystemOS": component("043-2.dmg.aea", "systemos")}
        for name in research.OS_COMPONENTS:
            self.iphone_components.setdefault(name, component(f"{name}.bin", name))
        iphone_digest = write_firmware(self.root / "iphone", "24A446", ["iPhone17,3"], [self.iphone_components])
        pcc_entries = {name: component(f"Firmware/{name}.im4p", name) for name in inputs.PCC_BOOT}
        pcc_entries["OS"] = component("094-3.dmg.aea", "pcc-os")
        pcc_digest = write_firmware(self.root / "pcc", "23E5207q", ["vphone600"],
                                    [{}, {}, {}, {}, {}, pcc_entries])
        for name in inputs.PCC_BOOT_FILES:
            (self.root / "pcc/Firmware" / name).write_bytes(b"im4p")
        for directory_name, names in (("iphone", ("043-1.dmg", "043-2.dmg")), ("pcc", ("094-3.dmg",))):
            (self.root / directory_name / "decrypted").mkdir()
            for name in names:
                (self.root / directory_name / "decrypted" / name).write_bytes(APFS + b"rest")
        self.template = ticket_for_image(b"template")
        (self.root / "ticket.der").write_bytes(self.template)
        pcc = {name: {"Info": {}, "Digest": hashlib.sha384(b"pcc" + name.encode()).digest()}
               for name in research.OS_COMPONENTS}
        (self.root / "your.im4m").write_bytes(research.rebuild(self.template, pcc, self.iphone_components, ECID))
        tree = node("device-tree")
        tree.children.append(node("chosen", **{"unique-chip-id": struct.pack("<Q", ECID)}))
        (self.root / "dtree.restore").write_bytes(tree.encode())
        for name, value in (("IPHONE_BUILD_SHA256", iphone_digest), ("PCC_BUILD_SHA256", pcc_digest),
                            ("TEMPLATE_SHA256", hashlib.sha256(self.template).hexdigest())):
            patcher = mock.patch.object(inputs, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_check(self, *extra, everything=True):
        argv = ["--iphone-firmware", str(self.root / "iphone"), "--pcc-firmware", str(self.root / "pcc"),
                "--template", str(self.root / "ticket.der"), "--manifest", str(self.root / "your.im4m"),
                "--device-tree", str(self.root / "dtree.restore")] if everything else []
        return inputs.inspect(inputs.parser().parse_args([*argv, *extra]))

    def row(self, report, name):
        return next(row for row in report["checks"] if row["input"] == name)

    def test_matching_inputs_pass_and_the_decrypted_images_are_found_where_setup_puts_them(self):
        report = self.run_check()
        self.assertTrue(report["ok"], json.dumps(report, indent=1))
        self.assertEqual(self.row(report, "iPhone OS image")["path"], str(self.root / "iphone/decrypted/043-1.dmg"))
        self.assertEqual(self.row(report, "PCC system image")["path"], str(self.root / "pcc/decrypted/094-3.dmg"))
        self.assertEqual(self.row(report, "IM4M")["ecid"], hex(ECID))

    def test_nothing_given_is_skipped_not_failed(self):
        report = self.run_check(everything=False)
        self.assertTrue(report["ok"])
        self.assertEqual({row["status"] for row in report["checks"]}, {"skipped"})
        self.assertEqual(report["summary"], "no inputs were given to check")

    def test_another_build_is_named_with_the_one_the_chain_needs(self):
        manifest = plistlib.loads((self.root / "iphone/BuildManifest.plist").read_bytes())
        manifest["ProductBuildVersion"] = "24B5000"
        (self.root / "iphone/BuildManifest.plist").write_bytes(plistlib.dumps(manifest))
        row = self.row(self.run_check(), "iPhone image")
        self.assertEqual(row["status"], "fail")
        self.assertIn("it holds iPhone17,3 x build 24B5000, but the chain needs iPhone17,3 27.0.1 build 24A446",
                      row["problem"])

    def test_swapped_directories_are_called_swapped(self):
        report = inputs.inspect(inputs.parser().parse_args(
            ["--iphone-firmware", str(self.root / "pcc"), "--pcc-firmware", str(self.root / "iphone")]))
        self.assertEqual(self.row(report, "iPhone image")["problem"], "this is the PCC image, not the iPhone image")
        self.assertIn("swapped", self.row(report, "PCC image")["fix"])

    def test_a_directory_that_is_not_unpacked_or_lacks_files_says_so(self):
        (self.root / "pcc/Firmware/SEP.im4p").unlink()
        (self.root / "pcc/Firmware/txm.iphoneos.research.im4p").unlink()
        row = self.row(self.run_check(), "PCC image files")
        self.assertEqual(row["problem"], "lacks SEP (Firmware/SEP.im4p), txm.iphoneos.research.im4p")
        (self.root / "iphone/BuildManifest.plist").unlink()
        self.assertIn("not an unpacked restore image", self.row(self.run_check(), "iPhone image")["problem"])

    def test_an_encrypted_or_foreign_image_is_refused_in_words(self):
        (self.root / "iphone/decrypted/043-1.dmg").write_bytes(b"AEA1" + bytes(60))
        (self.root / "iphone/decrypted/043-2.dmg").write_bytes(bytes(64))
        report = self.run_check()
        self.assertIn("still an encrypted AEA archive", self.row(report, "iPhone OS image")["problem"])
        self.assertIn("not an APFS disk image", self.row(report, "iPhone SystemOS image")["problem"])
        self.assertFalse(report["ok"])

    def test_an_image_with_another_name_is_a_warning(self):
        other = self.root / "other.dmg"
        other.write_bytes(APFS)
        report = self.run_check("--pcc-system-image", str(other))
        row = self.row(report, "PCC system image")
        self.assertEqual(row["status"], "warn")
        self.assertIn("094-3.dmg", row["problem"])
        self.assertTrue(report["ok"])

    def test_another_template_is_refused(self):
        (self.root / "ticket.der").write_bytes(ticket_for_image(b"another"))
        self.assertIn("not the documented research template", self.row(self.run_check(), "IM4M template")["problem"])
        (self.root / "ticket.der").write_bytes(plistlib.dumps({"ApImg4Ticket": self.template}))
        self.assertEqual(self.row(self.run_check(), "IM4M template")["status"], "pass")

    def test_an_im4m_for_another_image_or_device_is_explained(self):
        pcc = {name: {"Info": {}, "Digest": hashlib.sha384(name.encode()).digest()} for name in research.OS_COMPONENTS}
        phone = {name: {"Info": {}, "Digest": hashlib.sha384(b"other" + name.encode()).digest()}
                 for name in research.OS_COMPONENTS}
        (self.root / "your.im4m").write_bytes(research.rebuild(self.template, pcc, phone, ECID))
        self.assertIn("made for another iPhone image than build 24A446",
                      self.row(self.run_check(), "IM4M")["problem"])
        (self.root / "your.im4m").write_bytes(research.rebuild(self.template, pcc, self.iphone_components, 17))
        self.assertIn("it names device 0x11, but the device tree is device 0x1122334455667788",
                      self.row(self.run_check(), "IM4M")["problem"])
        (self.root / "your.im4m").write_bytes(bytes(10000))
        self.assertIn("more than an IM4M's 9216", self.row(self.run_check(), "IM4M")["problem"])
        (self.root / "your.im4m").write_bytes(b"not der")
        self.assertIn("not a DER IM4M", self.row(self.run_check(), "IM4M")["problem"])

    def test_the_command_prints_json_and_fails_when_an_input_is_wrong(self):
        for template, code in ((self.root / "ticket.der", 0), (self.root / "missing", 1)):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(inputs.main(["--template", str(template)]), code)
            self.assertEqual(json.loads(output.getvalue())["ok"], code == 0)

if __name__ == "__main__":
    unittest.main()
