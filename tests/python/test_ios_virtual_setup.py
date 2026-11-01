"""ios.virtual.setup and ios.virtual.host_check through tomaccoctl: the plan of a machine with nothing prepared,
the refusals of run, and that neither starts anything or writes outside work. No guest and no build runs."""
import json
import tarfile
import unittest

from test_product import Isolated
from native_client import StudioError, dispatch


class VirtualSetupTests(Isolated):
    def test_the_plan_of_an_empty_work_directory_starts_with_the_engine_and_writes_nothing(self):
        work = self.root / "phone"
        plan = dispatch("ios.virtual.setup", {"work": str(work)})
        self.assertEqual(plan["next"]["stage"], "engine")
        self.assertEqual(len(plan["stages"]), 17)
        self.assertEqual(plan["stages"][-1]["stage"], "configure")
        self.assertEqual(plan["inputs"]["summary"], "no inputs were given to check")
        self.assertIn(plan["host"]["status"], ("pass", "warn", "fail"))
        self.assertFalse(work.exists())
        restore = next(stage for stage in plan["stages"] if stage["stage"] == "restore")
        self.assertTrue(restore["manual"])
        self.assertEqual(restore["state"], "unknown")

    def test_run_asks_first_and_keeps_the_order(self):
        work = self.root / "phone"
        with self.assertRaises(StudioError) as refused:
            dispatch("ios.virtual.setup", {"work": str(work), "step": "run"})
        self.assertIn(refused.exception.code, ("ios_setup_confirm", "ios_setup_needs_checkout"))
        with self.assertRaises(StudioError) as refused:
            dispatch("ios.virtual.setup", {"work": str(work), "step": "run", "stage": "userland"})
        self.assertEqual(refused.exception.code, "ios_setup_out_of_order")
        self.assertFalse(work.exists())

    def test_a_wrong_image_is_explained_in_the_plan(self):
        firmware = self.root / "iphone"
        firmware.mkdir()
        plan = dispatch("ios.virtual.setup", {"work": str(self.root / "phone"), "iphone_firmware": str(firmware)})
        row = next(row for row in plan["inputs"]["checks"] if row["input"] == "iPhone image")
        self.assertEqual(row["status"], "fail")
        self.assertIn("not an unpacked restore image", row["problem"])

    def test_the_setup_needs_a_work_directory(self):
        with self.assertRaises(StudioError) as refused:
            dispatch("ios.virtual.setup", {})
        self.assertEqual(refused.exception.code, "invalid_argument")

    def test_the_host_check_lists_every_item_with_a_status(self):
        answer = dispatch("ios.virtual.host_check", {"directory": str(self.root)})
        self.assertEqual([item["check"] for item in answer["checks"]],
                         ["architecture", "cpu", "kvm", "memory", "disk", "graphics", "helper_kernel", "passt"])
        for item in answer["checks"]:
            self.assertIn(item["status"], ("pass", "warn", "fail"))
            self.assertEqual(item["remedy"] is None, item["status"] == "pass")
        self.assertEqual(answer["ready"], answer["failed"] == 0)

    def test_the_report_bundle_carries_the_virtual_device_part(self):
        output = self.root / "report.tar.gz"
        answer = dispatch("diagnostics.bundle", {"path": str(output), "doctor": False})
        self.assertIn("virtual-ios.json", answer["files"])
        with tarfile.open(output) as archive:
            part = json.load(archive.extractfile("tomacco-report/virtual-ios.json"))
            readme = archive.extractfile("tomacco-report/README.txt").read().decode()
        self.assertFalse(part["configured"])
        self.assertIn("checks", part["host"])
        self.assertIn("virtual-ios.json", readme)
        answer = dispatch("diagnostics.bundle", {"path": str(self.root / "plain.tar.gz"), "doctor": False,
                                                 "virtual_ios": False})
        self.assertNotIn("virtual-ios.json", answer["files"])


if __name__ == "__main__":
    unittest.main()
