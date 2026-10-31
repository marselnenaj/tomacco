"""Actual orchestration owner with scripted API replies, not native iOS proof."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from ios_virtual_iphone import GuestObservationError, Phone


class VirtualScreenRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tomacco-screen-recovery-")
        self.directory = Path(self.temp.name)
        (self.directory / "profile.json").write_text(json.dumps({"format": 1, "application": "dev.app"}))
        (self.directory / "state.json").write_text(json.dumps({"run": 1, "closed": False, "guest_job": "job"}))
        self.phone = Phone("/unused", self.directory)
        self.calls = []
        self.replies = []

        def invoke(operation, arguments, *args, **kwargs):
            self.calls.append((operation, arguments))
            self.assertTrue(self.replies, "Unexpected extra API call")
            expected, reply = self.replies.pop(0)
            self.assertEqual(operation, expected)
            return reply

        self.phone.invoke = invoke
        self.sleep = patch("ios_virtual_iphone.time.sleep", lambda _: None)
        self.sleep.start()
        self.emit = patch("ios_virtual_iphone.emit", lambda *args, **kwargs: None)
        self.emit.start()
        self.request = "a" * 32

    def tearDown(self):
        self.emit.stop()
        self.sleep.stop()
        self.temp.cleanup()

    def timeout(self):
        return {"ok": False, "error": {"code": "ios_guest_timeout", "details": {
            "request_id": self.request, "request_may_have_executed": True}}}

    def recovered(self, state="completed", ready=True, **changes):
        result = {"job_id": "job", "request_id": self.request, "state": state,
                  "ready_for_next_request": ready, "response": {
                      "operation": "screen_state", "request_id": self.request,
                      "ok": True, "native_status": 0, "screen_state_known": True,
                      "screen_locked": False, "passcode_enabled": False}}
        result.update(changes)
        return {"ok": True, "result": result}

    def test_late_screen_reply_is_followed_without_a_second_screen_query(self):
        final = self.recovered()
        self.replies = [("ios.emulator.guest", self.timeout()),
                        ("ios.emulator.guest_result", self.recovered("running", False)),
                        ("ios.emulator.guest_result", self.recovered(ready=False)),
                        ("ios.emulator.guest_result", final)]
        result = self.phone.screen_reply("job", time.monotonic() + 30)
        self.assertEqual(result["result"]["response"], final["result"]["response"])
        self.assertEqual([op for op, _ in self.calls].count("ios.emulator.guest"), 1)
        self.assertEqual([args["request_id"] for op, args in self.calls
                          if op == "ios.emulator.guest_result"], [self.request] * 3)

    def test_immediate_screen_reply_waits_for_its_worker_before_returning(self):
        terminal = self.recovered()
        initial = {"ok": True, "result": {
            "job_id": "job", "request_id": self.request,
            "response": terminal["result"]["response"]}}
        self.replies = [("ios.emulator.guest", initial),
                        ("ios.emulator.guest_result", self.recovered(ready=False)),
                        ("ios.emulator.guest_result", terminal)]
        result = self.phone.screen_reply("job", time.monotonic() + 30)
        self.assertEqual(len(self.calls), 3)
        self.assertFalse(self.replies)
        self.assertTrue(result["result"]["ready_for_next_request"])
        self.assertEqual(result["result"]["response"], initial["result"]["response"])

    def test_argument_free_diagnostic_reads_share_the_original_receipt_owner(self):
        for operation in ("foreground_state", "graphics_state", "display_state"):
            with self.subTest(operation=operation):
                self.calls.clear()
                terminal = self.recovered()
                terminal["result"]["response"]["operation"] = operation
                self.replies = [("ios.emulator.guest", self.timeout()),
                                ("ios.emulator.guest_result", terminal)]
                result = self.phone.query_reply("job", operation, time.monotonic() + 30)
                self.assertEqual(self.calls[0][1]["operation"], operation)
                self.assertEqual(result["result"]["response"]["operation"], operation)
                self.assertTrue(result["result"]["ready_for_next_request"])

    def test_immediate_receipt_identity_and_terminal_payload_must_match(self):
        for change in ({"job_id": "other"}, {"request_id": "b" * 32},
                       {"response": {"operation": "open", "request_id": self.request}},
                       {"response": {"operation": "screen_state", "request_id": "b" * 32}},
                       {"response": {**self.recovered()["result"]["response"], "screen_locked": True}}):
            with self.subTest(change=change):
                self.calls.clear()
                initial = {"ok": True, "result": {
                    "job_id": "job", "request_id": self.request,
                    "response": self.recovered()["result"]["response"]}}
                terminal = self.recovered(**change)
                self.replies = [("ios.emulator.guest", initial),
                                ("ios.emulator.guest_result", terminal)]
                with self.assertRaises(GuestObservationError):
                    self.phone.screen_reply("job", time.monotonic() + 30)
                self.assertFalse(any(op == "ios.emulator.control" for op, _ in self.calls))

    def test_completed_direct_reply_without_retained_receipt_does_not_claim_idle(self):
        initial = {"ok": True, "result": {
            "job_id": "job", "request_id": self.request,
            "response": self.recovered()["result"]["response"]}}
        self.replies = [("ios.emulator.guest", initial),
                        ("ios.emulator.guest_result", self.recovered("unknown", False))]
        with self.assertRaises(GuestObservationError):
            self.phone.screen_reply("job", time.monotonic() + 30)
        self.assertEqual(len(self.calls), 2)

    def test_query_owner_rejects_mutations_before_any_submission(self):
        for operation in ("open", "install", "display_refresh", "restart_ui", "cancel"):
            with self.subTest(operation=operation), self.assertRaises(GuestObservationError):
                self.phone.query_reply("job", operation, time.monotonic() + 30)
        self.assertFalse(self.calls)

    def test_direct_reply_context_is_checked_before_receipt_lookup(self):
        for change in ({"job_id": "other"}, {"request_id": "invalid"},
                       {"response": {"operation": "open", "request_id": self.request}},
                       {"response": {"operation": "screen_state", "request_id": "b" * 32}}):
            with self.subTest(change=change):
                self.calls.clear()
                result = {"job_id": "job", "request_id": self.request,
                          "response": self.recovered()["result"]["response"]}
                result.update(change)
                self.replies = [("ios.emulator.guest", {"ok": True, "result": result})]
                with self.assertRaises(GuestObservationError):
                    self.phone.screen_reply("job", time.monotonic() + 30)
                self.assertEqual(len(self.calls), 1)

    def test_parent_busy_refusal_is_preserved_without_receipt_or_replay(self):
        for error in ("guest_operation_busy", "worker_start_failed"):
            with self.subTest(error=error):
                self.calls.clear()
                reply = {"ok": True, "result": {
                    "job_id": "job", "request_id": self.request, "response": {
                        "operation": "screen_state", "request_id": self.request,
                        "ok": False, "error": error}}}
                self.replies = [("ios.emulator.guest", reply)]
                self.assertEqual(self.phone.screen_reply("job", time.monotonic() + 30), reply)
                self.assertEqual(len(self.calls), 1)

    def test_failed_native_rpc_is_retained_through_worker_exit(self):
        terminal = self.recovered()
        terminal["result"]["response"] = {
            "operation": "foreground_state", "request_id": self.request, "ok": False,
            "foreground_native_status": 5, "foreground_identifier_known": False,
            "error": "foreground_query_rpc_failed"}
        initial = {"ok": True, "result": {
            "job_id": "job", "request_id": self.request,
            "response": terminal["result"]["response"]}}
        self.replies = [("ios.emulator.guest", initial),
                        ("ios.emulator.guest_result", terminal)]
        result = self.phone.query_reply("job", "foreground_state", time.monotonic() + 30)
        self.assertEqual(result["result"]["response"]["foreground_native_status"], 5)
        self.assertFalse(result["result"]["response"]["ok"])
        self.assertNotIn("frontmost_bundle_id", result["result"]["response"])

    def test_receipt_lookup_contention_and_guest_exit_never_replay_the_query(self):
        original = self.timeout()
        stopped = {"ok": False, "error": {"code": "ios_guest_stopped"}}
        self.replies = [("ios.emulator.guest", original),
                        ("ios.emulator.guest_result", {"ok": False, "error": {"code": "ios_guest_busy"}}),
                        ("ios.emulator.guest_result", stopped)]
        self.assertEqual(self.phone.screen_reply("job", time.monotonic() + 30), stopped)
        self.assertEqual([op for op, _ in self.calls].count("ios.emulator.guest"), 1)

    def test_native_service_not_ready_is_preserved_for_the_launch_wait_loop(self):
        reply = {"ok": True, "result": {"response": {"ok": False,
                 "screen_state_known": False, "error": "screen_query_service_unavailable"}}}
        self.replies = [("ios.emulator.guest", reply)]
        self.assertEqual(self.phone.screen_reply("job", time.monotonic() + 30), reply)
        self.assertEqual(len(self.calls), 1)

    def test_foreign_screen_receipts_are_refused_before_inputs(self):
        for change in [{"job_id": "other"}, {"request_id": "b" * 32},
                       {"response": {"operation": "open", "request_id": self.request}},
                       {"response": {"operation": "screen_state", "request_id": "b" * 32}}]:
            with self.subTest(change=change):
                self.calls.clear()
                self.replies = [("ios.emulator.guest", self.timeout()),
                                ("ios.emulator.guest_result", self.recovered(**change))]
                with self.assertRaises(GuestObservationError):
                    self.phone.screen_reply("job", time.monotonic() + 30)
                self.assertFalse(any(op == "ios.emulator.control" for op, _ in self.calls))

    def test_missing_retained_outcome_is_unknown_and_not_an_unlock(self):
        original = self.timeout()
        self.replies = [("ios.emulator.guest", original),
                        ("ios.emulator.guest_result", self.recovered("unknown", False))]
        self.assertEqual(self.phone.screen_reply("job", time.monotonic() + 30), original)
        self.assertEqual(len(self.calls), 2)

    def test_deadline_and_invalid_identity_do_not_submit_inputs_or_a_new_query(self):
        self.replies = []
        with self.assertRaises(GuestObservationError):
            self.phone.screen_reply("job", time.monotonic() - 1)
        self.assertEqual(len(self.calls), 0)
        reply = self.timeout()
        reply["error"]["details"]["request_id"] = "invalid"
        self.calls.clear()
        self.replies = [("ios.emulator.guest", reply)]
        self.assertEqual(self.phone.screen_reply("job", time.monotonic() + 30), reply)
        self.assertEqual(len(self.calls), 1)

    @staticmethod
    def process_reply():
        return {"operation": "open", "argument": "dev.app", "request_accepted": True,
                "pid": 550, "worker_pid": 550, "pid_role": "request-worker",
                "launch_process_pid": 551, "launch_process_pid_known": True,
                "launch_process_pid_source": "FrontBoard-completion-BSProcessHandle.pid",
                "launch_process_lifetime_bound": False,
                "launch_process_snapshot_atomic_with_handle": False,
                "launch_process_handle_class": "BSProcessHandle",
                "launch_process_start_snapshot_known": True,
                "launch_process_start_snapshot": {"source": "PROC_PIDTBSDINFO", "pid": 551,
                    "single_native_record": True, "start_seconds": 1792000000,
                    "start_microseconds": 123456, "process_name": "Preferences"}}

    def test_process_metadata_is_bounded_and_never_uses_the_worker_pid_as_app_pid(self):
        native = self.process_reply()
        observation = self.phone.process_observation(native, "dev.app")
        self.assertEqual((observation["pid"], observation["worker_pid"]), (551, 550))
        self.assertTrue(observation["start_snapshot_known"])
        self.assertFalse(observation["process_lifetime_bound"])
        for invalid in [True, False, 0, 1, -1, 2147483648, "551", 551.0, None]:
            with self.subTest(pid=invalid):
                row = {**native, "launch_process_pid": invalid}
                self.assertIsNone(self.phone.process_observation(row, "dev.app"))
        self.assertIsNone(self.phone.process_observation({"pid": 550}, "dev.app"))
        self.assertIsNone(self.phone.process_observation(native, "other.app"))

    def test_invalid_start_fields_remain_pid_only_observations(self):
        for key, value in [("pid", 550), ("pid", True), ("start_seconds", 0),
                           ("start_seconds", True), ("start_microseconds", -1),
                           ("start_microseconds", 1000000), ("single_native_record", False)]:
            with self.subTest(key=key, value=value):
                native = copy.deepcopy(self.process_reply())
                native["launch_process_start_snapshot"][key] = value
                observation = self.phone.process_observation(native, "dev.app")
                self.assertEqual(observation["pid"], 551)
                self.assertFalse(observation["start_snapshot_known"])
                self.assertNotIn("start_snapshot", observation)

    def test_new_or_uncertain_launch_intent_clears_the_previous_process_observation(self):
        observation = self.phone.process_observation(self.process_reply(), "dev.app")
        self.phone.remember_launch("job", "accepted", self.request, observation)
        self.assertEqual(self.phone.state["launch_receipt"]["process_observation"], observation)
        for state in ["submitting", "unknown", "refused"]:
            self.phone.remember_launch("job", state, self.request)
            self.assertNotIn("process_observation", self.phone.state["launch_receipt"])


if __name__ == "__main__":
    unittest.main()
