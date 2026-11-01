"""project.run on the virtual iPhone, as a job: build, deploy, report."""
import json
import unittest

from native_client import dispatch, jobs
from test_ios_virtual_profiles import running_guest, write_profile
from test_product import Isolated


class VirtualRun(Isolated):
    def test_a_run_builds_then_reports_why_the_app_could_not_go(self):
        directory = write_profile(self.root / "profile")
        dispatch("ios.virtual.configure", {"directory": str(directory)})
        project = self.root / "App"
        project.mkdir()
        (project / "xtool.yml").write_text("bundleID: dev.test.app\n")
        # A stand-in xtool that "builds" an app bundle which is not arm64 iOS.
        self.tool("xtool", "import os,plistlib\nfrom pathlib import Path\n"
                           "app=Path('xtool/App.app');app.mkdir(parents=True,exist_ok=True)\n"
                           "(app/'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'dev.test.app',"
                           "'CFBundleExecutable':'App'}))\n(app/'App').write_bytes(b'not a program')\n")
        guest = running_guest(self)
        try:
            state = json.loads((directory / "state.json").read_text())
            (directory / "state.json").write_text(json.dumps({**state, "closed": False, "guest_job": guest}))
            job = dispatch("project.run", {"path": str(project), "platform": "ios", "device": "virtual-iphone",
                                           "crash_check_seconds": 0})
            done = jobs.wait(job["id"], 60)
            self.assertEqual(done["state"], "failed", jobs.logs(job["id"]))
            report_path = project / "build/tomacco/virtual_iphone_run.json"
            self.assertIn(str(report_path), [item["path"] for item in done["result"]["artifacts"]])
            report = json.loads(report_path.read_text())
            self.assertEqual(report["state"], "failed")
            self.assertIsInstance(report["build_seconds"], float)
            self.assertTrue(report["error"]["message"])
            self.assertIn("failed: ", jobs.logs(job["id"])["text"])
        finally:
            jobs.cancel(guest)
            jobs.wait(guest, 30)


if __name__ == "__main__":
    unittest.main()
