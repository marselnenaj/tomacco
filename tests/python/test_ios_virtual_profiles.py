"""Several virtual iPhone profiles: listed, and switched only while the phone is off."""
import json
from pathlib import Path
import unittest

from native_client import StudioError, dispatch, jobs
import test_ios_paravirt_job
from test_product import Isolated


def write_profile(directory, **state):
    (directory / "installs").mkdir(parents=True)
    (directory / "profile.json").write_text(json.dumps({
        "format": 1, "prepare": {}, "boot": {}, "graphics": "/graphics", "engine": "/engine.py",
        "application": "app.example", "installs": str(directory / "installs")}))
    (directory / "state.json").write_text(json.dumps({
        "run": 3, "install": str(directory / "installs/guest-install3"), "tools": "/tools",
        "closed": True, "guest_job": None, **state}))
    return directory


def running_guest(test):
    """A guest job that stays running: the process fixture of the paravirtual job tests."""
    arguments = test_ios_paravirt_job.ParavirtualJobTests.arguments(test, wait=True)
    return dispatch("ios.emulator.paravirtual", arguments)["id"]


class Profiles(Isolated):
    def test_profiles_are_remembered_and_the_current_one_is_marked(self):
        self.assertEqual(dispatch("ios.virtual.profiles"), {"current": None, "profiles": [], "busy": False,
                                                            "can_switch": True})
        first = write_profile(self.root / "first")
        second = write_profile(self.root / "second", run=9)
        dispatch("ios.virtual.configure", {"directory": str(first)})
        dispatch("ios.virtual.configure", {"directory": str(second)})
        listed = dispatch("ios.virtual.profiles")
        self.assertEqual(listed["current"], str(second))
        self.assertEqual([(row["directory"], row["current"], row["run"]) for row in listed["profiles"]],
                         [(str(second), True, 9), (str(first), False, 3)])
        self.assertTrue(all(row["valid"] and row["start_disks"] == "copy" and row["saved_states"] == 0
                            for row in listed["profiles"]))
        # Choosing one again moves it to the front; nothing is listed twice.
        dispatch("ios.virtual.configure", {"directory": str(first)})
        self.assertEqual([row["directory"] for row in dispatch("ios.virtual.profiles")["profiles"]],
                         [str(first), str(second)])
        # A profile that broke since is listed with the reason, not dropped.
        (second / "state.json").write_text("{}")
        rows = {row["directory"]: row for row in dispatch("ios.virtual.profiles")["profiles"]}
        self.assertFalse(rows[str(second)]["valid"])
        self.assertEqual(rows[str(second)]["error"]["code"], "ios_virtual_profile_invalid")
        with self.assertRaises(StudioError) as error:
            dispatch("ios.virtual.configure", {"directory": str(second)})
        self.assertEqual(error.exception.code, "ios_virtual_profile_invalid")
        self.assertEqual(dispatch("ios.virtual.status")["directory"], str(first))

    def test_another_profile_is_refused_while_the_current_phone_runs(self):
        guest = running_guest(self)
        first = write_profile(self.root / "first", closed=False, guest_job=guest)
        second = write_profile(self.root / "second")
        dispatch("ios.virtual.configure", {"directory": str(first)})
        with self.assertRaises(StudioError) as error:
            dispatch("ios.virtual.configure", {"directory": str(second)})
        self.assertEqual(error.exception.code, "ios_virtual_running")
        self.assertEqual(error.exception.details["guest_job"], guest)
        listed = dispatch("ios.virtual.profiles")
        self.assertFalse(listed["can_switch"])
        self.assertTrue(listed["profiles"][0]["running"])
        # The same profile again, or only its model, is no switch.
        dispatch("ios.virtual.configure", {"directory": str(first)})
        dispatch("ios.virtual.configure", {"model": "16"})
        self.assertEqual(dispatch("ios.virtual.status")["directory"], str(first))
        jobs.cancel(guest)
        jobs.wait(guest, 30)
        dispatch("ios.virtual.configure", {"directory": str(second)})
        self.assertEqual(dispatch("ios.virtual.profiles")["current"], str(second))


if __name__ == "__main__":
    unittest.main()
