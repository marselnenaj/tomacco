"""Phone.ensure_unlocked and the unlock action with scripted API replies; no guest runs."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ios_virtual_iphone
from ios_virtual_iphone import Phone


class FakeTime:
    """A monotonic clock that sleeping moves on, and the real wall clock."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0.001)

    @staticmethod
    def time():
        return 1.0


def worker(supported=True):
    return {"ok": True, "result": {"response": {"ok": True, "worker_running": False,
                                                "screen_state_supported": supported}}}


def screen(locked, passcode=False, awake=True):
    return {"ok": True, "result": {"response": {"ok": True, "screen_state_known": True, "screen_locked": locked,
                                                "passcode_enabled": passcode, "screen_awake_known": True,
                                                "screen_awake": awake}}}


ACCEPTED = {"ok": True, "result": {"result": {"accepted_by_emulator": True}}}


class EnsureUnlockedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tomacco-unlock-")
        self.directory = Path(self.temp.name)
        (self.directory / "profile.json").write_text(json.dumps({"format": 1, "application": "dev.app"}))
        (self.directory / "state.json").write_text(json.dumps({"run": 1, "closed": False, "guest_job": "job"}))
        self.phone = Phone("/unused", self.directory)
        self.calls, self.replies = [], []

        def invoke(operation, arguments, *args, **kwargs):
            self.calls.append((operation, arguments.get("operation") or arguments.get("action")))
            self.assertTrue(self.replies, f"Unexpected extra call {operation} {arguments}")
            expected, reply = self.replies.pop(0)
            self.assertEqual((operation, arguments.get("operation") or arguments.get("action")), expected)
            return reply

        self.phone.invoke = invoke
        self.clock = FakeTime()
        for patcher in (patch.object(ios_virtual_iphone, "time", self.clock),
                        patch.object(ios_virtual_iphone, "emit", lambda *args, **kwargs: None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)

    def script(self, *steps):
        names = {"status": ("ios.emulator.guest", "status"), "screen": ("ios.emulator.guest", "screen_state"),
                 "home": ("ios.emulator.control", "home"), "control": ("ios.emulator.control", "status")}
        self.replies = [(names[name], reply) for name, reply in steps]

    def test_an_unlocked_screen_is_left_alone(self):
        self.script(("status", worker()), ("screen", screen(False)))
        self.assertEqual(self.phone.ensure_unlocked("job", self.clock.now + 600), "unlocked")
        self.assertNotIn(("ios.emulator.control", "home"), self.calls)

    def test_a_passcode_free_lock_screen_is_left_the_way_the_start_leaves_it(self):
        # An awake lock screen: Home, then a fresh observation that it is unlocked.
        self.script(("status", worker()), ("screen", screen(True)), ("home", ACCEPTED),
                    ("status", worker()), ("screen", screen(False)))
        self.assertEqual(self.phone.ensure_unlocked("job", self.clock.now + 600), "unlocked")
        self.assertEqual(self.replies, [])
        # A dark one whose side button cannot wake it gets Home as well.
        self.script(("status", worker()), ("screen", screen(True, awake=False)),
                    ("control", {"ok": True, "result": {"result": {"power_button_supported": False}}}),
                    ("home", ACCEPTED), ("status", worker()), ("screen", screen(False)))
        self.assertEqual(self.phone.ensure_unlocked("job", self.clock.now + 600), "unlocked")

    def test_a_passcode_is_never_entered(self):
        self.script(("status", worker()), ("screen", screen(True, passcode=True)))
        with self.assertRaisesRegex(RuntimeError, "passcode"):
            self.phone.ensure_unlocked("job", self.clock.now + 600)
        self.assertNotIn(("ios.emulator.control", "home"), self.calls)

    def test_guest_tools_that_cannot_report_the_screen_are_not_pressed(self):
        self.script(("status", worker(supported=False)))
        self.assertEqual(self.phone.ensure_unlocked("job", self.clock.now + 600), "not-checked")
        self.script(("status", worker()), ("screen", {"ok": True, "result": {"response": {
            "ok": False, "error": "screen_query_api_unavailable"}}}))
        self.assertEqual(self.phone.ensure_unlocked("job", self.clock.now + 600), "not-checked")

    def test_a_screen_that_stays_locked_ends_at_the_deadline_with_a_clear_error(self):
        # Home is accepted every time and changes nothing.
        answers = {"status": worker(), "screen_state": screen(True), "home": ACCEPTED}
        presses = []

        def invoke(operation, arguments, *args, **kwargs):
            name = arguments.get("operation") or arguments.get("action")
            if name == "home":
                presses.append(self.clock.now)
            return answers[name]
        self.phone.invoke = invoke
        with self.assertRaisesRegex(RuntimeError, "stayed on its lock screen; unlock it in the phone window"):
            self.phone.ensure_unlocked("job", self.clock.now + 600)
        # Paced as in the start: a press and its follow-up, then a minute's rest.
        self.assertGreaterEqual(len(presses), 4)
        self.assertLess(len(presses), 40)
        self.assertGreaterEqual(presses[2] - presses[1], 60)

    def test_a_stopped_guest_and_a_phone_that_is_off(self):
        self.script(("status", {"ok": False, "error": {"code": "ios_guest_stopped"}}))
        with self.assertRaisesRegex(RuntimeError, "guest stopped"):
            self.phone.ensure_unlocked("job", self.clock.now + 600)
        self.phone.guest_running = lambda: False
        with self.assertRaisesRegex(RuntimeError, "not running"):
            self.phone.unlock_screen()


if __name__ == "__main__":
    unittest.main()
