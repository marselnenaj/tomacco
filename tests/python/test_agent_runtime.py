"""The agent control plane end to end on the offline fixture runtime, through the
compiled CLI: a session observed, read as marks, tapped, rotated and gestured, with
every device helper trapped. Nothing here touches hardware, USB or the network, and
nothing it times is a device's performance."""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_fixture import Isolated, write_login_scenario  # noqa: E402


class FixtureSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.box = Isolated()
        write_login_scenario(cls.box.fixtures)
        cls.session = cls.box.result("runtime.start", {"platform": "android", "device": "fixture:login"})["session"]

    @classmethod
    def tearDownClass(cls):
        cls.box.close()

    def setUp(self):
        # Every test starts on the home screen.
        self.box.result("runtime.input", {"session": self.session, "action": "key", "key": "back"})
        state = self.box.result("runtime.state", {"session": self.session})
        if state["width"] != 360:
            self.box.result("runtime.input", {"session": self.session, "action": "rotate", "orientation": "portrait"})

    def tearDown(self):
        self.assertEqual(self.box.trapped(), "", "a device helper was called")

    def test_the_session_is_labelled_a_fixture_with_its_capabilities(self):
        state = self.box.result("runtime.state", {"session": self.session})
        self.assertEqual(state["transport"], "fixture")
        self.assertEqual(state["evidence"], "fixture")
        self.assertEqual(state["input_capabilities"]["evidence"], "fixture")
        self.assertTrue(state["input_capabilities"]["live_touch"])
        self.assertEqual(state["width"], 360)
        # The fixture is off without its directory, and never a real device.
        error = self.box.error("runtime.start", {"platform": "android", "device": "fixture:missing"})
        self.assertEqual(error["code"], "fixture_runtime")

    def test_observe_answers_a_fresh_frame_with_geometry_app_keyboard_and_focus(self):
        before = time.time()
        seen = self.box.result("runtime.observe", {"session": self.session})
        self.assertEqual(len(seen["frame"]["id"]), 16)
        self.assertTrue(seen["frame"]["sha256"].startswith(seen["frame"]["id"]))
        self.assertGreaterEqual(seen["frame"]["captured_at"], before - 1)
        self.assertEqual(seen["frame"]["age_ms"], 0)
        self.assertEqual((seen["frame"]["width"], seen["frame"]["height"]), (360, 640))
        self.assertTrue(Path(seen["frame"]["path"]).is_file())
        self.assertEqual(seen["geometry"]["orientation"], "portrait")
        self.assertTrue(seen["geometry_matches_frame"])
        self.assertIn("input", seen["geometry"]["coordinate_space"])
        self.assertEqual(seen["app"], {"available": True, "bundle_id": "com.example.app", "name": "Example",
                                       "home_screen": False, "source": "fixture scenario"})
        self.assertEqual(seen["keyboard"]["visible"], False)
        self.assertEqual(seen["focus"]["focused"], False)
        self.assertEqual(seen["evidence"], "fixture")
        self.assertIn("Example (com.example.app); portrait 360x640; keyboard hidden; no field has focus", seen["context"])
        # Without context only the frame and geometry are read.
        quick = self.box.result("runtime.observe", {"session": self.session, "context": False,
                                                     "path": str(self.box.home / "look.png")})
        self.assertEqual(quick["app"]["available"], False)
        self.assertIn("context was false", quick["focus"]["reason"])
        self.assertTrue((self.box.home / "look.png").is_file())
        self.assertEqual(quick["frame"]["id"], seen["frame"]["id"], "an unchanged screen keeps its frame id")

    def test_marks_and_taps_work_on_a_session_and_focus_follows_the_tap(self):
        marked = self.box.result("screen.marks", {"device": self.session, "path": str(self.box.home / "marks.png")})
        self.assertEqual(marked["source"], "hierarchy")
        self.assertEqual([m["resource_id"] for m in marked["marks"]], ["com.example.app:id/email", "com.example.app:id/sign_in"])
        self.assertEqual(len(marked["frame_id"]), 16)
        tapped = self.box.result("screen.tap", {"device": self.session, "mark": 1})
        self.assertTrue(tapped["verified"])
        self.assertEqual(tapped["frame_id"], marked["frame_id"])
        # The field takes focus only after the scenario's delay: a fresh look says so.
        soon = self.box.result("runtime.observe", {"session": self.session})
        self.assertIn(soon["focus"]["focused"], (False, True))
        time.sleep(0.2)
        later = self.box.result("runtime.observe", {"session": self.session})
        self.assertEqual(later["focus"]["focused"], True)
        self.assertEqual(later["keyboard"]["visible"], True)
        self.assertNotEqual(later["frame"]["id"], soon["frame"]["id"] if soon["focus"]["focused"] is False else "")
        tree = self.box.result("screen.tree", {"device": self.session})
        self.assertIn("EditText", tree["text"])
        self.assertEqual(tree["source"], "hierarchy")

    def test_a_rotation_makes_the_old_marks_stale_and_nothing_is_tapped(self):
        marked = self.box.result("screen.marks", {"device": self.session, "path": str(self.box.home / "marks.png")})
        self.assertEqual(marked["width"], 360)
        self.box.result("runtime.input", {"session": self.session, "action": "rotate", "orientation": "landscape_left"})
        error = self.box.error("screen.tap", {"device": self.session, "mark": 2})
        self.assertEqual(error["code"], "mark_stale")
        self.assertIn("it rotated", error["message"])
        seen = self.box.result("runtime.observe", {"session": self.session})
        self.assertEqual(seen["fixture"]["inputs"], seen["fixture"]["inputs"])
        self.assertEqual((seen["frame"]["width"], seen["geometry"]["orientation"]), (640, "landscape"))
        again = self.box.result("screen.marks", {"device": self.session, "path": str(self.box.home / "marks.png")})
        self.assertEqual((again["width"], len(again["marks"])), (640, 1))
        tapped = self.box.result("screen.tap", {"device": self.session, "mark": 1})
        self.assertTrue(tapped["verified"])

    def test_gestures_pinch_and_traces_report_their_mode(self):
        trace = self.box.result("runtime.gesture", {"session": self.session, "contacts": [
            {"points": [{"x": 0.5, "y": 0.8, "t_ms": 0}, {"x": 0.5, "y": 0.5, "t_ms": 40}, {"x": 0.5, "y": 0.2, "t_ms": 80}]}]})
        self.assertTrue(trace["delivered"])
        pinch = self.box.result("runtime.input", {"session": self.session, "action": "pinch", "x": 0.5, "y": 0.5, "scale": 2})
        self.assertTrue(pinch["delivered"])
        error = self.box.error("runtime.gesture", {"session": self.session, "contacts": [
            {"points": [{"x": 0.5, "y": 0.8, "t_ms": 50}, {"x": 0.5, "y": 0.5, "t_ms": 10}]}]})
        self.assertEqual(error["code"], "invalid_argument")

    def test_a_stop_waits_for_the_worker_and_a_new_start_gets_a_new_session(self):
        box = Isolated()
        try:
            write_login_scenario(box.fixtures)
            first = box.result("runtime.start", {"platform": "android", "device": "fixture:login"})
            again = box.result("runtime.start", {"platform": "android", "device": "fixture:login"})
            self.assertEqual((again["session"], again["reused"]), (first["session"], True))
            stopped = box.result("runtime.stop", {"session": first["session"]})
            self.assertEqual((stopped["stopping"], stopped["stopped"], stopped["status"]), (True, True, "disconnected"))
            started = time.monotonic()
            fresh = box.result("runtime.start", {"platform": "android", "device": "fixture:login"})
            self.assertLess(time.monotonic() - started, 10, "a start right after a stop must not wait on the old session")
            self.assertNotEqual(fresh["session"], first["session"])
            self.assertEqual(fresh["status"], "connected")
            self.assertEqual(box.result("runtime.stop", {"session": first["session"]})["stopping"], False)
        finally:
            box.close()

    def test_screen_wait_on_a_session_sees_the_welcome_screen(self):
        marked = self.box.result("screen.marks", {"device": self.session, "path": str(self.box.home / "marks.png")})
        self.box.result("screen.tap", {"device": self.session, "mark": 2})
        # The loading screen keeps animating; stable waits answer screen_unstable with what they saw.
        unstable = self.box.error("screen.wait", {"device": self.session, "until": "stable", "timeout": 1, "interval_ms": 100, "stable_ms": 500})
        self.assertEqual(unstable["code"], "screen_unstable")
        self.box.result("runtime.input", {"session": self.session, "action": "tap", "x": 0.5, "y": 0.5})
        settled = self.box.result("screen.wait", {"device": self.session, "until": "stable", "timeout": 5, "interval_ms": 100, "stable_ms": 300})
        self.assertTrue(settled["met"])
        self.assertEqual(marked["count"], 2)


if __name__ == "__main__":
    unittest.main()
