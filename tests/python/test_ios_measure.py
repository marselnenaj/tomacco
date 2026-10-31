import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import ios_measure

# A stand-in guest: each action leaves the screen changing for a number of
# frame reads, the app's log follows opens and clicks the way the app writes
# it, and SpringBoard's foreground follows Home and open.
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ["FAKE_ROOT"])
operation, arguments = sys.argv[2], json.loads(sys.argv[3])
scenario = json.loads((root / "scenario.json").read_text())
state_path = root / "state.json"
state = json.loads(state_path.read_text()) if state_path.exists() else {
    "pending": 0, "front": "com.apple.springboard", "log": [], "reads": 0,
    "locked": scenario.get("locked", False), "homes": 0, "page": scenario.get("page", "first")}
ok = lambda result: {"ok": True, "result": result}
reply = None
if operation == "ios.emulator.control":
    action = arguments["action"]
    if action == "frame":
        state["reads"] += 1
        home = state["front"] != "app.example"
        # The Home screen's Clock icon never stops; the app's window is still.
        changed = state["pending"] > 0 or (home and scenario.get("home_ticks", True))
        state["pending"] = max(0, state["pending"] - 1)
        shown = not home and state["pending"] < scenario.get("draw_frames", 2)
        path = root / ("frame-%d.ppm" % state["reads"])
        path.write_bytes(b"P6\n20 40\n255\n" + (b"\xff" if shown else b"\x00") * (20 * 40 * 3))
        reply = ok({"result": {"path": str(path), "sequence": state["reads"], "changed": changed}})
    else:
        if action == "home":
            state["front"], state["pending"] = "com.apple.springboard", 3
            state["homes"] += 1
            state["page"] = "first"
            # A passcode-free lock screen opens with the second press.
            if state["locked"] and state["homes"] >= 2 and not scenario.get("home_cannot_unlock"):
                state["locked"] = False
        elif action == "click":
            state["pending"] = scenario.get("click_frames", 1)
            if scenario.get("taps_register", True):
                state["log"].append({"event": "control-action", "count": 1})
        elif action == "drag":
            state["pending"] = 5
        reply = ok({"result": {"accepted": True}})
elif operation == "ios.emulator.guest":
    guest = arguments["operation"]
    if guest == "screen_state":
        reply = ok({"response": {"ok": True, "screen_state_known": True, "screen_locked": bool(state["locked"])}})
    elif guest == "open" and state["locked"]:
        reply = ok({"response": {"ok": False, "request_accepted": False, "error": "request_rejected_by_ios"}})
    elif guest == "open":
        state["front"], state["pending"] = arguments["argument"], scenario.get("open_frames", 4)
        state["log"] += [{"event": "begin"}, {"event": "window-state", "width": 428.0, "height": 926.0},
                         {"event": "scene-active", "activation_state": 0}]
        reply = ok({"response": {"ok": True, "request_accepted": True}})
    elif guest == "foreground_state":
        reply = ok({"response": {"ok": True, "foreground_identifier_known": True,
                                 "frontmost_bundle_id": state["front"]}})
    elif guest == "logs":
        reply = ok({"response": {"ok": True, "text": "".join(json.dumps(row) + "\n" for row in state["log"])}})
state_path.write_text(json.dumps(state))
print(json.dumps(reply))
'''


class MeasureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="tomacco-measure-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.cli = self.root / "tomaccoctl"
        self.cli.write_text(FAKE_CLI)
        self.cli.chmod(0o755)
        os.environ["FAKE_ROOT"] = str(self.root)
        self.scenario()

    def scenario(self, **values):
        (self.root / "scenario.json").write_text(json.dumps(values))

    def measure(self):
        return ios_measure.Measure(str(self.cli), "guest8", "app.example", settle=0.05, patience=0.05)

    def test_launch_touch_and_swipe_are_tied_to_the_apps_own_log(self):
        measure = self.measure()
        launch = measure.launch()
        self.assertGreater(launch["settled"], 0)
        self.assertLessEqual(launch["shown"], launch["settled"])
        self.assertEqual(launch["window"]["width"], 428.0)
        self.assertGreater(measure.touch(launch["window"]), 0)
        swipe = measure.swipe(seconds=6)
        self.assertGreater(swipe["reads_per_second"], 0)
        self.assertGreater(swipe["changed_frames_per_second"], 0)
        self.assertGreater(swipe["idle_changed_frames_per_second"], 0)
        self.assertTrue(measure.reads)

    def test_the_swipe_is_measured_from_the_first_page(self):
        # The Today page has nothing moving: from there the same phone read 0.25
        # idle and 1.75 swipe, where the first page read 10.2 and 8.2.
        self.scenario(page="today")
        self.measure().swipe(seconds=1)
        self.assertEqual(json.loads((self.root / "state.json").read_text())["page"], "first")

    def test_a_locked_phone_is_unlocked_before_the_launch(self):
        self.scenario(locked=True)
        launch = self.measure().launch()
        self.assertGreater(launch["settled"], 0)

    def test_a_phone_that_stays_locked_is_reported(self):
        self.scenario(locked=True, home_cannot_unlock=True)
        with self.assertRaisesRegex(ios_measure.MeasurementFailed, "stayed locked"):
            self.measure().launch()

    def test_a_tap_the_app_does_not_log_is_not_measured(self):
        self.scenario(taps_register=False)
        measure = self.measure()
        launch = measure.launch()
        with self.assertRaisesRegex(ios_measure.MeasurementFailed, "no action for the tap"):
            measure.touch(launch["window"])

    def test_the_app_window_is_told_from_the_home_screen_by_its_pixels(self):
        white = self.root / "white.ppm"
        white.write_bytes(b"P6\n# QEMU\n4 8\n255\n" + b"\xff" * (4 * 8 * 3))
        self.assertTrue(ios_measure.Measure.app_on_screen(white))
        dark = self.root / "dark.ppm"
        dark.write_bytes(b"P6\n4 8\n255\n" + b"\xff" * (4 * 4 * 3) + b"\x00" * (4 * 4 * 3))
        self.assertFalse(ios_measure.Measure.app_on_screen(dark))

    def test_an_app_screen_that_never_settles_is_reported(self):
        self.scenario(open_frames=10 ** 6, draw_frames=10 ** 7)
        measure = ios_measure.Measure(str(self.cli), "guest8", "app.example", settle=0.05, patience=0.01)
        with self.assertRaisesRegex(ios_measure.MeasurementFailed, "did not settle"):
            measure.launch()

    def test_summary_names_median_and_range(self):
        self.assertEqual(ios_measure.summary([3.0, 1.0, 2.0]), {"count": 3, "median": 2.0, "min": 1.0, "max": 3.0})
        self.assertEqual(ios_measure.summary([]), {"count": 0})


if __name__ == "__main__":
    unittest.main()
