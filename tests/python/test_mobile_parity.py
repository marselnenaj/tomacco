"""The phone parity matrix covers every operation and stays in step with assets/operations.json.

A unittest module so the control-plane suite (unittest discover) runs it; it was
previously written for pytest only and silently ran zero tests there.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import build_mobile_parity as parity  # noqa: E402


class ParityMatrix(unittest.TestCase):
    def test_every_operation_is_classified(self):
        data = parity.build()
        names = [op["name"] for op in json.loads((ROOT / "assets" / "operations.json").read_text(encoding="utf-8"))]
        self.assertEqual([row["operation"] for row in data["operations"]], names)
        self.assertTrue(all(row["phone"] in parity.STATUSES for row in data["operations"]))
        self.assertEqual(sum(data["counts"].values()), len(names))

    def test_an_unknown_operation_family_is_an_error_not_a_default(self):
        with self.assertRaises(KeyError):
            parity.classify("nothing.like_this")

    def test_remote_and_conditional_rows_name_a_requirement_and_a_test(self):
        for row in parity.build()["operations"]:
            self.assertTrue(row["next_test"], row["operation"])
            self.assertTrue(row["evidence"], row["operation"])
            if row["phone"] in ("remote", "conditional"):
                self.assertTrue(row["requires"], row["operation"])
            self.assertIs(row["verified_on_pixel"], False, "nothing is verified on the Pixel until the device block is lifted")

    def test_known_verdicts(self):
        verdict = {row["operation"]: row["phone"] for row in parity.build()["operations"]}
        expected = {
            "android.avd_start": "remote", "android.emulator": "remote", "android.avd_create": "local",
            "ios.virtual.start": "remote", "ios.ipa_inspect": "local", "preview.navigate": "conditional",
            "demo.export": "local", "project.run": "local", "job.cancel": "local", "remote.run": "local",
            # Route playback needs an emulator console (KVM host); reading a GPX file does not.
            "gpx.inspect": "local", "gpx.play": "remote", "gpx.status": "remote",
            # Handoffs are records the person completes on the device in front of them.
            "handoff.request": "local", "handoff.complete": "local", "diagnostics.report_draft": "local",
        }
        for name, status in expected.items():
            self.assertEqual(verdict[name], status, name)

    def test_every_workspace_route_has_a_row(self):
        routes = {r["route"] for r in parity.build()["ui_routes"]}
        for section in ("project", "devices", "jobs", "tools", "debug", "testing", "profiling", "files", "release", "demo"):
            self.assertIn(section, routes)

    def test_generated_files_are_current(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "build_mobile_parity.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
