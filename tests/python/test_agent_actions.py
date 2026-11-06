"""runtime.actions on the offline fixture runtime: ordered steps, focus before text,
bounded deadlines, stale frames, postconditions and the applied/skipped report of a
failure. Every device helper is trapped; nothing here is a device's performance."""
import shutil
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_fixture import Isolated, write_login_scenario  # noqa: E402

FIELD = {"action": "tap", "x": 0.5, "y": 0.5}


class ActionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.box = Isolated()
        write_login_scenario(cls.box.fixtures, focus_after_ms=150)
        cls.session = cls.box.result("runtime.start", {"platform": "android", "device": "fixture:login"})["session"]

    @classmethod
    def tearDownClass(cls):
        cls.box.close()

    def setUp(self):
        self.box.result("runtime.input", {"session": self.session, "action": "key", "key": "back"})
        self.box.result("runtime.input", {"session": self.session, "action": "key", "key": "back"})
        self.fixture = self.observe()["fixture"]
        self.assertEqual(self.fixture["state"], "home")

    def tearDown(self):
        self.assertEqual(self.box.trapped(), "")

    def observe(self):
        return self.box.result("runtime.observe", {"session": self.session})

    def actions(self, steps, **extra):
        return self.box.call("runtime.actions", {"session": self.session, "steps": steps, **extra})

    def test_text_waits_for_the_focus_the_tap_brings_and_loses_nothing(self):
        reply = self.actions([FIELD, {"action": "text", "text": "ana"}], observe_after=True)
        self.assertTrue(reply["ok"], reply)
        result = reply["result"]
        self.assertEqual([s["status"] for s in result["steps"]], ["applied", "applied"])
        self.assertEqual(result["steps"][1]["focus"]["status"], "focused")
        self.assertGreaterEqual(result["steps"][1]["focus"]["seen"]["polls"], 2, "the focus came after the scenario's delay")
        self.assertEqual(result["applied"], 2)
        self.assertEqual(result["evidence"], "fixture")
        self.assertEqual(result["observation"]["fixture"]["lost_characters"], self.fixture["lost_characters"])
        self.assertEqual(result["observation"]["fixture"]["state"], "typed")

    def test_typing_at_once_loses_characters_which_is_why_the_wait_exists(self):
        reply = self.actions([FIELD, {"action": "text", "text": "ana", "focus": "none"}])
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["steps"][1]["focus"]["status"], "not_checked")
        self.assertEqual(self.observe()["fixture"]["lost_characters"], self.fixture["lost_characters"] + 3)

    def test_no_focus_in_time_types_nothing_and_names_the_applied_steps(self):
        reply = self.actions([{"action": "tap", "x": 0.5, "y": 0.05}, {"action": "text", "text": "ana", "focus_timeout_ms": 300},
                              {"action": "key", "key": "enter"}])
        self.assertFalse(reply["ok"])
        error = reply["error"]
        self.assertEqual(error["code"], "focus_timeout")
        self.assertEqual(error["details"]["applied_steps"], [0])
        self.assertEqual(error["details"]["skipped"], [2])
        self.assertEqual([s["status"] for s in error["details"]["steps"]], ["applied", "failed"])
        after = self.observe()["fixture"]
        self.assertEqual(after["lost_characters"], self.fixture["lost_characters"], "nothing was typed")
        self.assertEqual(after["inputs"], self.fixture["inputs"] + 1, "only the tap reached the device")
        reply = self.actions([{"action": "text", "text": "x", "focus": "require", "focus_timeout_ms": 100}])
        self.assertEqual(reply["error"]["code"], "focus_timeout")

    def test_a_batch_planned_on_another_frame_sends_nothing(self):
        reply = self.actions([FIELD], frame={"width": 640, "height": 360, "orientation": "landscape"})
        self.assertEqual(reply["error"]["code"], "frame_stale")
        self.assertIn("width and height and orientation", reply["error"]["message"])
        self.assertEqual(self.observe()["fixture"]["inputs"], self.fixture["inputs"])
        seen = self.observe()
        reply = self.actions([{"action": "observe", "context": False}], frame={"width": seen["frame"]["width"], "height": seen["frame"]["height"], "id": seen["frame"]["id"]})
        self.assertTrue(reply["ok"], reply)
        self.assertTrue(reply["result"]["frame_checked"])
        self.assertEqual(reply["result"]["steps"][0]["result"]["frame"]["id"], seen["frame"]["id"])

    def test_postconditions_make_steps_verified_or_stop_the_batch_without_a_replay(self):
        self.box.result("screen.marks", {"device": self.session, "path": str(self.box.home / "marks.png")})
        reply = self.actions([{"action": "tap", "mark": 2, "expect": {"text": "Loading", "timeout_ms": 2000}},
                              {"action": "tap", "x": 0.5, "y": 0.5, "expect": {"text": "Welcome", "timeout_ms": 2000}},
                              {"action": "wait_text_gone", "text": "Loading", "timeout_ms": 1000}])
        self.assertTrue(reply["ok"], reply)
        result = reply["result"]
        self.assertEqual([s["status"] for s in result["steps"]], ["verified", "verified", "applied"])
        self.assertEqual(result["steps"][0]["result"]["verified"], True)
        self.assertEqual(result["steps"][0]["expect"]["source"], "hierarchy")
        self.assertEqual(result["verified"], 2)
        # A tap that misses, with a screen that never comes: applied, not verified, and no replay.
        self.box.result("runtime.input", {"session": self.session, "action": "key", "key": "back"})
        before = self.observe()["fixture"]["inputs"]
        reply = self.actions([{"action": "tap", "x": 0.5, "y": 0.05, "expect": {"text": "Nowhere", "timeout_ms": 300}}, {"action": "key", "key": "enter"}])
        self.assertEqual(reply["error"]["code"], "postcondition_failed")
        self.assertEqual(reply["error"]["details"]["applied_steps"], [0])
        self.assertEqual(reply["error"]["details"]["skipped"], [1])
        self.assertEqual(reply["error"]["details"]["steps"][0]["expect"]["met"], False)
        self.assertEqual(self.observe()["fixture"]["inputs"], before + 1)

    def test_the_deadline_bounds_the_batch_and_reports_what_was_skipped(self):
        started = time.monotonic()
        reply = self.actions([{"action": "wait", "ms": 700}, {"action": "wait", "ms": 700}, {"action": "key", "key": "back"}], timeout=1)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(reply["error"]["code"], "actions_timeout")
        self.assertEqual(reply["error"]["details"]["applied_steps"], [])
        self.assertIn(2, reply["error"]["details"]["skipped"])
        reply = self.actions([{"action": "dance"}])
        self.assertEqual(reply["error"]["code"], "invalid_argument")
        reply = self.actions([{"action": "tap", "x": 0.5, "y": 0.5}, {"action": "swipe", "x": 0.5, "y": 0.5}])
        self.assertEqual(reply["error"]["code"], "invalid_argument")
        self.assertEqual(self.observe()["fixture"]["inputs"], self.fixture["inputs"], "a wrong batch sends nothing")

    @unittest.skipUnless(shutil.which("tesseract"), "tesseract is not installed")
    def test_a_postcondition_can_be_read_off_the_pixels(self):
        self.box.result("screen.marks", {"device": self.session, "path": str(self.box.home / "marks.png")})
        reply = self.actions([{"action": "tap", "mark": 2, "expect": {"text": "Loading", "source": "ocr", "timeout_ms": 8000}}], timeout=60)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["steps"][0]["expect"]["source"], "ocr")


if __name__ == "__main__":
    unittest.main()
