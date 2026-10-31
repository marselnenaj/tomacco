"""ios.virtual.status reports what the start recorded about the time-zone
step and the apps installed on the run's volume; control-plane tests, no
guest is booted."""
import json

from test_product import Isolated
from native_client import dispatch


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
