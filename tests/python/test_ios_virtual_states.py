"""Saved phone states of the virtual iPhone: list, save, use and delete."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

for directory in ("tools", "tests"):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / directory))
import ios_virtual_iphone
from native_client import StudioError, dispatch
from test_product import Isolated


def write_profile(directory):
    (directory / "installs").mkdir(parents=True)
    (directory / "profile.json").write_text(json.dumps({
        "format": 1, "prepare": {}, "boot": {}, "graphics": "/graphics", "engine": "/engine.py",
        "application": "app.example", "installs": str(directory / "installs")}))


def copy(directory, run):
    install = directory / "installs" / f"guest-install{run}"
    install.mkdir()
    for name in ("system.raw", "auxiliary.raw", "sep.raw"):
        (install / name).write_bytes(bytes(4096))
    return install


class SavedStates(Isolated):
    def setUp(self):
        super().setUp()
        self.directory = self.root / "profile"
        write_profile(self.directory)
        self.installs = {run: copy(self.directory, run) for run in (2, 3, 4)}
        self.verify(3, created=[2, 3])
        dispatch("ios.virtual.configure", {"directory": str(self.directory)})

    def verify(self, run, created, **extra):
        install = str(self.installs[run])
        state = {"run": run, "install": install, "tools": "/tools", "closed": True, "guest_job": None,
                 "verified": {"run": run, "install": install, "tools": "/tools"},
                 "created": [str(self.installs[number]) for number in created], **extra}
        (self.directory / "state.json").write_text(json.dumps(state))

    def state(self):
        return json.loads((self.directory / "state.json").read_text())

    def refused(self, operation, arguments, code):
        with self.assertRaises(StudioError) as error:
            dispatch(operation, arguments)
        self.assertEqual(error.exception.code, code)
        return error.exception

    def test_a_state_pins_the_verified_run_and_survives_cleanup(self):
        self.assertEqual(dispatch("ios.virtual.states")["states"], [])
        saved = dispatch("ios.virtual.state_save", {"name": "Clean phone"})["saved"]
        self.assertEqual((saved["name"], saved["run"], saved["install"], saved["base"], saved["exists"]),
                         ("Clean phone", 3, str(self.installs[3]), True, True))
        listed = dispatch("ios.virtual.states")
        self.assertEqual([row["name"] for row in listed["states"]], ["Clean phone"])
        self.assertEqual(listed["base_saved_as"], ["Clean phone"])
        self.refused("ios.virtual.state_save", {"name": "Clean phone"}, "ios_virtual_state_exists")
        self.assertEqual(dispatch("ios.virtual.state_save", {"name": "Clean phone", "replace": True})["saved"]["run"], 3)
        for bad in ("", "../x", " lead"):
            with self.assertRaises(StudioError):
                dispatch("ios.virtual.state_save", {"name": bad})
        # A later run becomes the base; the saved run is kept by name.
        self.verify(4, created=[2, 3, 4])
        plan = dispatch("ios.virtual.cleanup")
        self.assertEqual([row["path"] for row in plan["copies"]], [str(self.installs[2])])
        self.assertIn({"path": str(self.installs[3]), "reason": "saved-state:Clean phone"}, plan["kept"])
        self.assertFalse(dispatch("ios.virtual.states")["states"][0]["base"])

    def test_using_a_state_needs_a_stopped_phone_and_a_kept_current_run(self):
        dispatch("ios.virtual.state_save", {"name": "clean"})
        self.verify(4, created=[2, 3, 4])
        error = self.refused("ios.virtual.state_use", {"name": "clean"}, "ios_virtual_state_unsaved")
        self.assertEqual(error.details["current"]["run"], 4)
        self.refused("ios.virtual.state_use", {"name": "other"}, "ios_virtual_state_unknown")
        used = dispatch("ios.virtual.state_use", {"name": "clean", "discard_current": True})
        self.assertEqual((used["used"], used["previous"]["run"]), ("clean", 4))
        state = self.state()
        self.assertEqual((state["verified"]["install"], state["install"], state["run"], state["closed"], state["phase"]),
                         (str(self.installs[3]), str(self.installs[3]), 3, True, "stopped"))
        self.assertEqual(state["created"], [str(self.installs[number]) for number in (2, 3, 4)])
        self.assertTrue(dispatch("ios.virtual.states")["states"][0]["base"])
        # A saved current run may be left without discard_current.
        dispatch("ios.virtual.state_save", {"name": "four", "replace": True})
        self.assertEqual(dispatch("ios.virtual.state_use", {"name": "clean"})["used"], "clean")
        # Not while a run is open, nor when its disks are gone.
        self.verify(4, created=[2, 3, 4], closed=False)
        self.refused("ios.virtual.state_use", {"name": "clean"}, "ios_virtual_running")
        self.verify(4, created=[2, 3, 4])
        dispatch("ios.virtual.state_save", {"name": "four", "replace": True})
        (self.installs[3] / "system.raw").unlink()
        self.refused("ios.virtual.state_use", {"name": "clean"}, "ios_virtual_state_missing")

    def test_deleting_a_state_removes_its_copy_only_when_nothing_else_keeps_it(self):
        dispatch("ios.virtual.state_save", {"name": "clean"})
        # The base is never deleted with its name.
        deleted = dispatch("ios.virtual.state_delete", {"name": "clean"})
        self.assertEqual((deleted["removed"], deleted["kept_because"]), (False, "verified-base"))
        self.assertTrue(self.installs[3].is_dir())
        dispatch("ios.virtual.state_save", {"name": "clean"})
        dispatch("ios.virtual.state_save", {"name": "twin"})
        self.verify(4, created=[2, 3, 4])
        deleted = dispatch("ios.virtual.state_delete", {"name": "clean"})
        self.assertEqual((deleted["removed"], deleted["kept_because"]), (False, "saved-state:twin"))
        deleted = dispatch("ios.virtual.state_delete", {"name": "twin"})
        self.assertTrue(deleted["removed"])
        self.assertGreater(deleted["freed_bytes"], 0)
        self.assertFalse(self.installs[3].exists())
        self.refused("ios.virtual.state_delete", {"name": "twin"}, "ios_virtual_state_unknown")
        self.assertEqual(dispatch("ios.virtual.states")["states"], [])

    def test_no_verified_run_means_nothing_to_save(self):
        (self.directory / "state.json").write_text(json.dumps({
            "run": 5, "install": str(self.installs[4]), "tools": "/t", "closed": False, "guest_job": None}))
        self.refused("ios.virtual.state_save", {"name": "x"}, "ios_virtual_no_verified_run")


class PruneKeepsSavedStates(unittest.TestCase):
    def test_the_starts_pruning_keeps_saved_runs_and_refuses_on_an_unreadable_list(self):
        with tempfile.TemporaryDirectory(prefix="tomacco-states-") as name, \
                patch.object(ios_virtual_iphone, "emit", lambda *_a, **_k: None):
            directory = Path(name) / "profile"
            write_profile(directory)
            installs = [copy(directory, run) for run in (1, 2, 3, 4)]
            (directory / "state.json").write_text(json.dumps({
                "run": 4, "install": str(installs[3]), "tools": "/t", "closed": True,
                "verified": {"run": 4, "install": str(installs[3]), "tools": "/t"},
                "created": [str(path) for path in installs]}))
            (directory / "saved-states.json").write_text(json.dumps(
                {"format": 1, "states": {"one": {"run": 1, "install": str(installs[0])}}}))
            phone = ios_virtual_iphone.Phone("/cli", directory)
            (directory / "saved-states.json").write_text("{broken")
            phone.prune(keep=1)
            self.assertTrue(all(path.is_dir() for path in installs))
            (directory / "saved-states.json").write_text(json.dumps(
                {"format": 1, "states": {"one": {"run": 1, "install": str(installs[0])}}}))
            phone.prune(keep=1)
            self.assertEqual([path.is_dir() for path in installs], [True, False, False, True])


if __name__ == "__main__":
    unittest.main()
