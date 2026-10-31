"""Exercise the product wake/launch owner; scripted replies are not iOS proof."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from ios_virtual_iphone import Phone


class VirtualWakeSequenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tomacco-wake-sequence-")
        self.root = Path(self.temp.name)
        (self.root / "profile.json").write_text(json.dumps({"format": 1, "application": "dev.app"}))
        (self.root / "state.json").write_text(json.dumps({"run": 1, "closed": False, "guest_job": "job"}))
        self.phone = Phone("/unused", self.root)
        self.calls, self.replies = [], []
        self.clock = 100.0

        def sleep(seconds):
            self.clock += seconds

        self.sleep = patch("ios_virtual_iphone.time.sleep", sleep)
        self.monotonic = patch("ios_virtual_iphone.time.monotonic", lambda: self.clock)
        self.emit = patch("ios_virtual_iphone.emit", lambda *args, **kwargs: None)
        self.sleep.start()
        self.monotonic.start()
        self.emit.start()

        def invoke(operation, arguments, *args, **kwargs):
            key = ("result" if operation == "ios.emulator.guest_result" else
                   arguments.get("operation", arguments.get("action")))
            self.calls.append((operation, key))
            self.assertTrue(self.replies, "Unexpected extra API call")
            expected, reply = self.replies.pop(0)
            self.assertEqual(key, expected)
            return reply

        self.phone.invoke = invoke

    def tearDown(self):
        self.emit.stop()
        self.monotonic.stop()
        self.sleep.stop()
        self.temp.cleanup()

    @staticmethod
    def idle():
        return {"ok": True, "result": {"response": {"ok": True,
                "worker_running": False, "screen_state_supported": True}}}

    @staticmethod
    def screen(locked=True, awake=False, passcode=False, request_id=None):
        row = {"ok": True, "operation": "screen_state",
                "screen_state_known": True, "screen_locked": locked,
                "passcode_enabled": passcode, "screen_awake_known": True,
                "screen_awake": awake}
        if request_id is not None:
            row["request_id"] = request_id
        return {"ok": True, "result": {"response": row}}

    @staticmethod
    def control(**fields):
        return {"ok": True, "result": {"result": fields}}

    @staticmethod
    def opened():
        return {"ok": True, "result": {"job_id": "job", "request_id": "a" * 32,
                "response": {"operation": "open", "argument": "dev.app", "request_id": "a" * 32,
                             "request_accepted": True}}}

    def completed_open(self):
        receipt = {**self.opened()["result"], "state": "completed", "ready_for_next_request": True}
        return [("open", self.opened()), ("result", {"ok": True, "result": receipt})]

    def boot_lock(self):
        return [("status", self.idle()), ("screen_state", self.screen()),
                ("status", self.control(power_button_supported=True))]

    def test_side_is_followed_by_a_new_screen_query_before_home(self):
        self.replies = self.boot_lock() + [
            ("power_button", self.control(accepted_by_emulator=True)),
            ("status", self.idle()), ("screen_state", self.screen(awake=True)),
            ("home", self.control(accepted_by_emulator=True)),
            ("status", self.idle()), ("screen_state", self.screen(locked=False, awake=True)),
            *self.completed_open()]
        self.phone.launch("job", time.monotonic() + 30)
        self.assertEqual([key for _, key in self.calls], [
            "status", "screen_state", "status", "power_button", "status", "screen_state",
            "home", "status", "screen_state", "open", "result"])
        self.assertEqual(self.phone.state["unlock_wake_receipt"]["state"], "accepted")

    def test_unknown_side_outcome_still_requires_a_fresh_query(self):
        self.replies = self.boot_lock() + [
            ("power_button", {"ok": False, "error": {"code": "qmp_timeout"}}),
            ("status", self.idle()), ("screen_state", self.screen(locked=False, awake=True)),
            *self.completed_open()]
        self.phone.launch("job", time.monotonic() + 30)
        self.assertEqual(self.phone.state["unlock_wake_receipt"]["state"], "unknown")
        self.assertNotIn("home", [key for _, key in self.calls])

    def test_fresh_passcode_state_prevents_home_and_app_open(self):
        self.replies = self.boot_lock() + [
            ("power_button", self.control(accepted_by_emulator=True)),
            ("status", self.idle()), ("screen_state", self.screen(awake=True, passcode=True))]
        with self.assertRaisesRegex(RuntimeError, "passcode"):
            self.phone.launch("job", time.monotonic() + 30)
        self.assertNotIn("home", [key for _, key in self.calls])
        self.assertNotIn("open", [key for _, key in self.calls])

    def test_failed_query_after_side_does_not_license_home(self):
        failed = {"ok": True, "result": {"response": {"ok": False,
                  "screen_state_known": False, "error": "screen_query_rpc_failed"}}}
        self.replies = self.boot_lock() + [
            ("power_button", self.control(accepted_by_emulator=True)),
            ("status", self.idle()), ("screen_state", failed),
            ("status", self.idle()), ("screen_state", self.screen(locked=False, awake=True)),
            *self.completed_open()]
        self.phone.launch("job", time.monotonic() + 30)
        self.assertEqual([key for _, key in self.calls].count("power_button"), 1)
        self.assertNotIn("home", [key for _, key in self.calls])

    def test_awake_screen_goes_directly_to_home_without_side(self):
        self.replies = [("status", self.idle()), ("screen_state", self.screen(awake=True)),
                        ("home", self.control(accepted_by_emulator=True)),
                        ("status", self.idle()), ("screen_state", self.screen(locked=False, awake=True)),
                        *self.completed_open()]
        self.phone.launch("job", time.monotonic() + 30)
        self.assertNotIn("power_button", [key for _, key in self.calls])

    def test_retained_side_intent_is_not_replayed_when_adopting_the_job(self):
        for state in ("submitting", "unknown", "accepted"):
            with self.subTest(state=state):
                self.phone.save(unlock_wake_receipt={"job_id": "job", "state": state})
                self.assertFalse(self.phone.wake_for_unlock("job", {"screen_awake_known": True,
                                                                    "screen_awake": False}))
                self.assertEqual(self.calls, [])

    def test_unavailable_side_does_not_block_standard_home(self):
        self.replies = [("status", self.idle()), ("screen_state", self.screen()),
                        ("status", self.control(power_button_supported=False)),
                        ("home", self.control(accepted_by_emulator=True)),
                        ("status", self.idle()), ("screen_state", self.screen(locked=False, awake=True)),
                        *self.completed_open()]
        self.phone.launch("job", time.monotonic() + 30)
        self.assertNotIn("power_button", [key for _, key in self.calls])

    def test_completed_wake_then_a_new_sleep_can_begin_a_distinct_cycle(self):
        self.phone.save(unlock_wake_receipt={"job_id": "job", "state": "accepted", "cycle": 1})
        awake = self.screen(awake=True, request_id="b" * 32)["result"]["response"]
        self.assertFalse(self.phone.wake_for_unlock("job", awake))
        asleep = self.screen(request_id="c" * 32)["result"]["response"]
        self.replies = [("status", self.control(power_button_supported=True)),
                        ("power_button", self.control(accepted_by_emulator=True))]
        self.assertTrue(self.phone.wake_for_unlock("job", asleep))
        self.assertEqual(self.phone.state["unlock_wake_receipt"]["cycle"], 2)
        history = self.phone.state["unlock_wake_history"]
        self.assertEqual(history[-1]["awake_request_id"], "b" * 32)
        self.assertEqual(history[-1]["next_sleep_request_id"], "c" * 32)
        self.assertFalse(self.phone.wake_for_unlock("job", asleep))
        self.assertEqual([key for _, key in self.calls].count("power_button"), 1)

    def test_unknown_wake_is_never_renewed_by_later_screen_states(self):
        for state in ("submitting", "unknown"):
            self.phone.save(unlock_wake_receipt={"job_id": "job", "state": state})
            self.assertFalse(self.phone.wake_for_unlock("job", self.screen(
                awake=True, request_id="b" * 32)["result"]["response"]))
            self.assertFalse(self.phone.wake_for_unlock("job", self.screen(
                request_id="c" * 32)["result"]["response"]))
        self.assertEqual(self.calls, [])

    def test_renewal_requires_two_valid_distinct_native_observations(self):
        for change in ({"ok": False}, {"screen_state_known": False},
                       {"screen_awake_known": False}, {"request_id": "invalid"},
                       {"request_id": "b" * 32}, {"passcode_enabled": True}):
            self.phone.save(unlock_wake_receipt={"job_id": "job", "state": "accepted",
                                               "awake_request_id": "b" * 32})
            native = self.screen(request_id="c" * 32)["result"]["response"]
            native.update(change)
            self.assertFalse(self.phone.wake_for_unlock("job", native))
        self.assertEqual(self.calls, [])

    def test_launch_can_reawaken_after_confirmed_awake_screen_sleeps_again(self):
        self.replies = self.boot_lock() + [
            ("power_button", self.control(accepted_by_emulator=True)),
            ("status", self.idle()), ("screen_state", self.screen(awake=True, request_id="b" * 32)),
            ("home", self.control(accepted_by_emulator=True)),
            ("status", self.idle()), ("screen_state", self.screen(awake=True, request_id="c" * 32)),
            ("home", self.control(accepted_by_emulator=True))]
        for _ in range(3):
            self.replies += [("status", self.idle()), ("screen_state", self.screen(awake=True))]
        self.replies += [
            ("status", self.idle()), ("screen_state", self.screen(request_id="d" * 32)),
            ("status", self.control(power_button_supported=True)),
            ("power_button", self.control(accepted_by_emulator=True)),
            ("status", self.idle()), ("screen_state", self.screen(awake=True, request_id="e" * 32)),
            ("home", self.control(accepted_by_emulator=True)),
            ("status", self.idle()), ("screen_state", self.screen(locked=False, awake=True)),
            *self.completed_open()]
        self.phone.launch("job", time.monotonic() + 200)
        self.assertEqual([key for _, key in self.calls].count("power_button"), 2)
        self.assertEqual([key for _, key in self.calls].count("open"), 1)


if __name__ == "__main__":
    unittest.main()
