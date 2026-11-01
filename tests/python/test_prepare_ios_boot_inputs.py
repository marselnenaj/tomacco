"""The boot components are found through the PCC research identity and unwrapped into a new directory.

The firmware directory is a synthetic fake, and `ipsw` is a fake that copies its input with a marker."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import prepare_ios_boot_inputs as boot  # noqa: E402

FAKE_IPSW = """#!/bin/sh
# ipsw img4 im4p extract IN -o OUT
[ "$1 $2 $3 $5" = "img4 im4p extract -o" ] || exit 2
printf 'unwrapped:' > "$6" && cat "$4" >> "$6"
"""


class BootInputTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.pcc = self.root / "pcc"
        (self.pcc / "Firmware/all_flash").mkdir(parents=True)
        manifest = {"Manifest": {"KernelCache": {"Info": {"Path": "kernelcache.research.vphone600"}},
                                 "DeviceTree": {"Info": {"Path": "Firmware/all_flash/DeviceTree.vphone600ap.im4p"}},
                                 "RestoreRamDisk": {"Info": {"Path": "094-1.dmg"}},
                                 "RestoreTrustCache": {"Info": {"Path": "Firmware/094-1.dmg.trustcache"}},
                                 "StaticTrustCache": {"Info": {"Path": "Firmware/094-2.dmg.trustcache"}}}}
        (self.pcc / "BuildManifest.plist").write_bytes(plistlib.dumps({"BuildIdentities": [{}] * 5 + [manifest]}))
        for relative in ("kernelcache.research.vphone600", "Firmware/all_flash/DeviceTree.vphone600ap.im4p",
                         "094-1.dmg", "Firmware/094-1.dmg.trustcache", "Firmware/094-2.dmg.trustcache",
                         "Firmware/sptm.vresearch1.release.im4p", "Firmware/txm.iphoneos.research.im4p",
                         "Firmware/all_flash/sep-firmware.vphone600.RELEASE.im4p"):
            (self.pcc / relative).write_bytes(relative.encode())
        self.ipsw = self.root / "ipsw"
        self.ipsw.write_text(FAKE_IPSW)
        self.ipsw.chmod(0o700)
        kernel = b"unwrapped:kernelcache.research.vphone600"
        patcher = mock.patch.object(boot, "KERNEL_SHA256", hashlib.sha256(kernel).hexdigest())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_component_is_unwrapped_with_its_source_and_digest(self):
        output = self.root / "boot"
        record = boot.prepare(self.pcc, self.ipsw, output)
        self.assertEqual(sorted(path.name for path in output.iterdir()),
                         ["boot-inputs.json", "dtree.original", "kernel", "ramdisk.dmg", "ramdisk.tc", "sep",
                          "sptm", "txm"])
        self.assertEqual((output / "sep").read_bytes(),
                         b"unwrapped:Firmware/all_flash/sep-firmware.vphone600.RELEASE.im4p")
        self.assertEqual(record["components"]["ramdisk.tc"]["source"], "Firmware/094-1.dmg.trustcache")
        self.assertEqual(record["gpu_trustcache"], str((self.pcc / "Firmware/094-2.dmg.trustcache").resolve()))
        self.assertEqual(json.loads((output / "boot-inputs.json").read_text()), record)
        self.assertEqual(record["components"]["sptm"]["sha256"],
                         hashlib.sha256(b"unwrapped:Firmware/sptm.vresearch1.release.im4p").hexdigest())

    def test_an_existing_output_is_never_replaced(self):
        (self.root / "boot").mkdir()
        with self.assertRaisesRegex(ValueError, "never replaced"):
            boot.prepare(self.pcc, self.ipsw, self.root / "boot")

    def test_another_kernel_leaves_no_output(self):
        (self.pcc / "kernelcache.research.vphone600").write_bytes(b"another kernel")
        with self.assertRaisesRegex(ValueError, "the unwrapped research kernel has SHA-256"):
            boot.prepare(self.pcc, self.ipsw, self.root / "boot")
        self.assertEqual([path.name for path in self.root.iterdir() if path.name.startswith((".boot", "boot"))], [])

    def test_a_missing_component_says_to_unpack_the_whole_archive(self):
        (self.pcc / "Firmware/txm.iphoneos.research.im4p").unlink()
        with self.assertRaisesRegex(ValueError, "txm.iphoneos.research.im4p is not in .*unpack the whole PCC archive"):
            boot.plan(self.pcc)
        (self.pcc / "Firmware/txm.iphoneos.research.im4p").write_bytes(b"txm")
        (self.pcc / "094-1.dmg").unlink()
        with self.assertRaisesRegex(ValueError, "094-1.dmg is listed but missing"):
            boot.plan(self.pcc)

    def test_the_command_reports_failure_as_json(self):
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            code = boot.main(["--pcc-firmware", str(self.root / "iphone"), "--ipsw", str(self.ipsw),
                              "--output", str(self.root / "boot")])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
