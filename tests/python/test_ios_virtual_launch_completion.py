"""Exercise the actual launch owner; scripted replies do not qualify iOS scenes."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ios_virtual_iphone import Phone


class VirtualLaunchCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tomacco-launch-completion-")
        self.root = Path(self.temp.name)
        (self.root / "profile.json").write_text(json.dumps({"format": 1, "application": "dev.app"}))
        (self.root / "state.json").write_text(json.dumps({"run": 1, "closed": False, "guest_job": "job"}))
        self.phone = Phone("/unused", self.root)
        self.calls, self.replies = [], []
        self.clock = 100.0
        self.request = "a" * 32
        self.patches = [patch("ios_virtual_iphone.time.sleep", self.sleep),
                        patch("ios_virtual_iphone.time.monotonic", lambda: self.clock),
                        patch("ios_virtual_iphone.emit", lambda *args, **kwargs: None)]
        for item in self.patches:
            item.start()
        self.bind(self.phone)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def sleep(self, seconds):
        self.clock += seconds

    def bind(self, phone):
        def invoke(operation, arguments, *args, **kwargs):
            self.calls.append((operation, dict(arguments)))
            self.assertTrue(self.replies, "Unexpected API call")
            expected, reply = self.replies.pop(0)
            self.assertEqual(operation, expected)
            return reply() if callable(reply) else copy.deepcopy(reply)

        phone.invoke = invoke
        # Query completion itself is qualified in the screen-recovery owner.
        phone.screen_reply = lambda *_: {"ok": True, "result": {"response": {
            "ok": True, "screen_state_known": True, "screen_locked": False,
            "passcode_enabled": False}}}

    @staticmethod
    def idle():
        return {"ok": True, "result": {"response": {"ok": True, "worker_running": False,
                                                    "screen_state_supported": True}}}

    def native(self, accepted=True, reason=None):
        row = {"operation": "open", "argument": "dev.app", "request_id": self.request,
               "request_accepted": accepted, "worker_pid": 123}
        if reason:
            row["native_error"] = {"underlying": {"code_description": reason}}
        return row

    def direct(self, native=None):
        return {"ok": True, "result": {"job_id": "job", "request_id": self.request,
                                       "response": native if native is not None else self.native()}}

    def receipt(self, native=None, state="completed", ready=True):
        result = {**self.direct(native)["result"], "state": state, "ready_for_next_request": ready}
        return {"ok": True, "result": result}

    def begin(self, direct=None):
        self.replies = [("ios.emulator.guest", self.idle()),
                        ("ios.emulator.guest", direct if direct is not None else self.direct())]

    def run_launch(self, seconds=30):
        self.phone.launch("job", self.clock + seconds)

    def assert_one_open_no_input(self):
        self.assertEqual([args.get("operation") for op, args in self.calls
                          if op == "ios.emulator.guest"], ["status", "open"])
        self.assertFalse(any(op == "ios.emulator.control" for op, _ in self.calls))
        self.assertTrue(all(args["request_id"] == self.request for op, args in self.calls
                            if op == "ios.emulator.guest_result"))

    def test_direct_acceptance_waits_for_original_worker_idle(self):
        self.begin()
        self.replies += [("ios.emulator.guest_result", self.receipt(state="running", ready=False)),
                         ("ios.emulator.guest_result", self.receipt(ready=False)),
                         ("ios.emulator.guest_result", self.receipt())]
        self.run_launch()
        self.assertFalse(self.replies)
        self.assert_one_open_no_input()
        self.assertTrue(self.phone.state["launch_receipt"]["native_worker_idle_verified"])
        self.assertLess(self.clock, 101)

    def test_direct_refusal_is_drained_before_a_permitted_busy_retry(self):
        native = self.native(False, "Busy")
        self.begin(self.direct(native))
        self.replies += [("ios.emulator.guest_result", self.receipt(native, ready=False)),
                         ("ios.emulator.guest_result", self.receipt(native)),
                         ("ios.emulator.guest", self.idle()),
                         ("ios.emulator.guest", self.direct()),
                         ("ios.emulator.guest_result", self.receipt())]
        self.run_launch()
        self.assertFalse(self.replies)
        self.assertEqual([op for op, _ in self.calls], ["ios.emulator.guest"] * 2 +
                         ["ios.emulator.guest_result"] * 2 + ["ios.emulator.guest"] * 2 +
                         ["ios.emulator.guest_result"])

    def test_direct_context_is_validated_before_any_receipt_lookup(self):
        for key, value in [("job_id", "other"), ("request_id", "invalid"), ("request_id", None)]:
            with self.subTest(key=key, value=value):
                self.calls.clear()
                changed = self.direct()
                changed["result"][key] = value
                self.begin(changed)
                with self.assertRaisesRegex(RuntimeError, "direct launch reply identity"):
                    self.run_launch()
                self.assert_one_open_no_input()
                self.assertEqual(self.phone.state["launch_receipt"]["state"], "unknown")
                # No untrusted foreign identity becomes an adoptable request.
                self.assertIsNone(self.phone.state["launch_receipt"]["request_id"])
                self.phone.save(launch_receipt={})

    def test_direct_native_context_is_validated(self):
        for key, value in [("operation", "screen_state"), ("argument", "other.app"),
                           ("request_id", "b" * 32)]:
            with self.subTest(key=key):
                self.calls.clear()
                native = self.native()
                native[key] = value
                self.begin(self.direct(native))
                with self.assertRaisesRegex(RuntimeError, "direct launch reply identity"):
                    self.run_launch()
                self.assert_one_open_no_input()
                self.phone.save(launch_receipt={})

    def test_foreign_or_changed_terminal_response_never_accepts_or_replays(self):
        for change in [{"job_id": "other"}, {"request_id": "b" * 32},
                       {"response": self.native(False, "Busy")},
                       {"response": {**self.native(), "worker_pid": 124}},
                       {"response": {**self.native(), "request_accepted": 1}},
                       {"response": {**self.native(), "argument": "other.app"}}]:
            with self.subTest(change=change):
                self.calls.clear()
                self.begin()
                terminal = self.receipt(ready=False)
                terminal["result"].update(change)
                self.replies.append(("ios.emulator.guest_result", terminal))
                with self.assertRaises(RuntimeError):
                    self.run_launch()
                self.assert_one_open_no_input()
                self.assertEqual(self.phone.state["launch_receipt"]["state"], "unknown")
                self.assertEqual(self.phone.state["launch_receipt"]["native_response"], self.native())
                self.phone.save(launch_receipt={})

    def test_missing_completed_receipt_remains_unknown(self):
        self.begin()
        self.replies += [("ios.emulator.guest_result", self.receipt(state="unknown", ready=False))]
        with self.assertRaisesRegex(RuntimeError, "cannot establish"):
            self.run_launch()
        self.assert_one_open_no_input()
        self.assertEqual(self.phone.state["launch_receipt"]["state"], "unknown")

    def test_interrupted_direct_reply_resumes_only_its_durable_receipt(self):
        def interrupted():
            raise RuntimeError("receipt connection interrupted")
        self.begin()
        self.replies.append(("ios.emulator.guest_result", interrupted))
        with self.assertRaisesRegex(RuntimeError, "connection interrupted"):
            self.run_launch()
        saved = json.loads((self.root / "state.json").read_text())["launch_receipt"]
        self.assertEqual(saved["state"], "pending")
        self.assertEqual(saved["native_response"], self.native())
        self.calls.clear()
        self.phone = Phone("/unused", self.root)
        self.bind(self.phone)
        self.replies = [("ios.emulator.guest_result", self.receipt())]
        self.run_launch()
        self.assertEqual([op for op, _ in self.calls], ["ios.emulator.guest_result"])
        self.assertTrue(self.phone.state["launch_receipt"]["native_worker_idle_verified"])

    def test_deadline_keeps_direct_acceptance_pending_without_an_extra_lookup(self):
        def late_direct():
            self.clock += 30
            return self.direct()
        self.begin(late_direct)
        with self.assertRaisesRegex(RuntimeError, "deadline"):
            self.run_launch()
        self.assert_one_open_no_input()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.phone.state["launch_receipt"]["state"], "pending")
        self.assertNotIn("native_worker_idle_verified", self.phone.state["launch_receipt"])

    def test_receipt_contention_never_resubmits_open_or_home(self):
        self.begin()
        self.replies += [("ios.emulator.guest_result", {"ok": False, "error": {"code": "ios_guest_busy"}}),
                         ("ios.emulator.guest_result", self.receipt())]
        self.run_launch()
        self.assertFalse(self.replies)
        self.assert_one_open_no_input()

    def test_timed_out_original_launch_uses_the_same_completion_owner(self):
        timeout = {"ok": False, "error": {"code": "ios_guest_timeout", "details": {
            "request_id": self.request, "request_may_have_executed": True}}}
        self.begin(timeout)
        self.replies += [("ios.emulator.guest_result", self.receipt())]
        self.run_launch()
        self.assert_one_open_no_input()
        self.assertTrue(self.phone.state["launch_receipt"]["native_worker_idle_verified"])

    def test_known_worker_start_failure_has_no_worker_receipt(self):
        self.begin({"ok": True, "result": {"response": {"ok": False, "error": "worker_start_failed"}}})
        with self.assertRaisesRegex(RuntimeError, "worker could not start"):
            self.run_launch()
        self.assert_one_open_no_input()
        self.assertEqual(self.phone.state["launch_receipt"]["state"], "not-submitted")

    def test_definitive_disabled_refusal_still_waits_for_worker_completion(self):
        native = self.native(False, "Disabled")
        self.begin(self.direct(native))
        self.replies += [("ios.emulator.guest_result", self.receipt(native, ready=False)),
                         ("ios.emulator.guest_result", self.receipt(native))]
        with self.assertRaisesRegex(RuntimeError, "Disabled"):
            self.run_launch()
        self.assertFalse(self.replies)
        self.assert_one_open_no_input()
        self.assertEqual(self.phone.state["launch_receipt"]["state"], "refused")

    def test_legacy_uncorrelated_acceptance_does_not_claim_native_idle(self):
        self.begin({"ok": True, "result": {"response": {"request_accepted": True}}})
        self.run_launch()
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("native_worker_idle_verified", self.phone.state["launch_receipt"])

    def test_retained_response_with_wrong_context_is_not_followed(self):
        self.phone.save(launch_receipt={"job_id": "job", "application": "dev.app", "state": "pending",
                                       "request_id": self.request,
                                       "native_response": {**self.native(), "argument": "other.app"}})
        with self.assertRaisesRegex(RuntimeError, "retained direct launch reply identity"):
            self.run_launch()
        self.assertFalse(self.calls)


if __name__ == "__main__":
    unittest.main()
