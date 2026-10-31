"""ios.virtual.status reports what the start recorded about the time-zone
step and the apps installed on the run's volume; control-plane tests, no
guest is booted."""
import json

from test_product import Isolated
from native_client import StudioError, dispatch


class VirtualIphoneStatusRecordTests(Isolated):
    """The desktop's iOS-Testsystem dialog reads both records from the
    status: the zone outcome for its status area, the installed apps for the
    state of each listed bundle."""
    RESEARCH = "io.github.marselnenaj.tomacco.research-app"

    def profile(self, name, state):
        directory = self.root / name
        (directory / "installs").mkdir(parents=True)
        (directory / "profile.json").write_text(json.dumps({
            "format": 1, "prepare": {"seconds": 300}, "boot": {}, "graphics": "/graphics", "engine": "/engine.py",
            "application": self.RESEARCH, "installs": str(directory / "installs")}))
        (directory / "state.json").write_text(json.dumps({
            "run": 324, "install": str(self.root / "missing-install"), "tools": str(self.root / "missing-tools"),
            "closed": True, "guest_job": None, **state}))
        return directory

    def test_the_time_zone_step_and_the_installed_apps_are_reported(self):
        applied = {"zone": "Europe/Vienna", "outcome": "applied", "previous_link": "/usr/share/zoneinfo/America/Los_Angeles",
                   "link": "/usr/share/zoneinfo/Europe/Vienna", "changed": True}
        directory = self.profile("applied", {"time_zone": applied, "applications": {"io.example.sample": "00" * 20}})
        status = dispatch("ios.virtual.configure", {"directory": str(directory)})
        self.assertEqual(status["time_zone"], {**applied, "reason": None})
        self.assertEqual(status["installed_applications"], {"io.example.sample": "00" * 20})
        # The guest's worker stayed busy and installd refused the staged app.
        busy = self.profile("busy", {"time_zone": {"zone": "Europe/Vienna", "outcome": "guest_operation_busy"},
                                     "applications": {}})
        status = dispatch("ios.virtual.configure", {"directory": str(busy)})
        self.assertEqual(status["time_zone"], {"zone": "Europe/Vienna", "outcome": "guest_operation_busy",
                                               "previous_link": None, "link": None, "changed": None, "reason": None})
        self.assertEqual(status["installed_applications"], {})
        # A state from before the step (runs up to 323) and before app deployment.
        before = self.profile("before", {})
        status = dispatch("ios.virtual.configure", {"directory": str(before)})
        self.assertIsNone(status["time_zone"])
        self.assertEqual(status["installed_applications"], {})
        self.assertEqual(dispatch("ios.virtual.status")["time_zone"], None)

    def test_the_phase_times_and_the_copy_progress_are_reported(self):
        directory = self.profile("timed", {"phase": "stopped", "phase_log": [
            {"phase": "stopping", "started": 100.0}, {"phase": "stopped", "started": 130.0}]})
        status = dispatch("ios.virtual.configure", {"directory": str(directory)})
        self.assertEqual(status["phases"][0], {"phase": "stopping", "started": 100.0, "seconds": 30.0})
        self.assertEqual(status["phase_started"], 130.0)
        self.assertGreater(status["phase_elapsed_seconds"], 0)
        # Not preparing: no copy to follow.
        self.assertIsNone(status["copy_progress"])
        before = self.profile("untimed", {})
        status = dispatch("ios.virtual.configure", {"directory": str(before)})
        self.assertEqual((status["phases"], status["phase_started"], status["phase_elapsed_seconds"]), ([], None, None))



class VirtualIphoneAppsTests(Isolated):
    """ios.virtual.apps joins the profile's list, each bundle's Info.plist on
    the host and the run's installed record; no guest is asked."""
    RESEARCH = VirtualIphoneStatusRecordTests.RESEARCH
    profile = VirtualIphoneStatusRecordTests.profile

    def test_versions_installed_state_and_the_app_the_start_opens(self):
        import plistlib
        bundle = self.root / "Sample.app"
        bundle.mkdir()
        (bundle / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "io.example.sample", "CFBundleShortVersionString": "1.3",
            "CFBundleVersion": "9", "MinimumOSVersion": "17.0", "CFBundleExecutable": "Sample"}))
        directory = self.profile("apps", {"applications": {"io.example.sample": "00" * 20,
                                                           "io.example.removed": "11" * 20}})
        profile = json.loads((directory / "profile.json").read_text())
        profile["applications"] = [
            {"bundle": str(bundle), "bundle_id": "io.example.sample", "name": "Sample.app", "display_name": "Sample"},
            {"bundle": str(self.root / "Gone.app"), "bundle_id": "io.example.gone", "name": "Gone.app",
             "display_name": "Gone"}]
        profile["application"] = "io.example.sample"
        (directory / "profile.json").write_text(json.dumps(profile))
        dispatch("ios.virtual.configure", {"directory": str(directory)})
        reply = dispatch("ios.virtual.apps")
        rows = {row["bundle_id"]: row for row in reply["applications"]}
        self.assertEqual(list(rows), [self.RESEARCH, "io.example.sample", "io.example.gone", "io.example.removed"])
        sample = rows["io.example.sample"]
        self.assertEqual((sample["version"], sample["build"], sample["minimum_os"]), ("1.3", "9", "17.0"))
        self.assertTrue(sample["installed"])
        self.assertTrue(sample["opens_at_start"])
        self.assertFalse(rows["io.example.gone"]["bundle_readable"])
        self.assertIn("does not exist", rows["io.example.gone"]["bundle_error"])
        self.assertFalse(rows["io.example.removed"]["listed"])
        self.assertFalse(reply["guest_running"])

    def test_crash_reports_need_a_running_phone_and_a_report_name(self):
        directory = self.profile("crashes", {})
        dispatch("ios.virtual.configure", {"directory": str(directory)})
        with self.assertRaises(StudioError) as refused:
            dispatch("ios.virtual.crash_reports", {"process": "MyApp", "directory": str(self.root / "out")})
        self.assertEqual(refused.exception.code, "ios_virtual_not_running")
        self.assertFalse((self.root / "out").exists(), "nothing is created for a phone that is off")
        for name in ("../etc", ".hidden", "-x", "a b"):
            with self.assertRaises(StudioError) as refused:
                dispatch("ios.virtual.crash_reports", {"process": name})
            self.assertEqual(refused.exception.code, "invalid_argument", name)
