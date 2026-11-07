"""The verified fill (fill with verify_value) through tomaccoctl and MCP on the offline
fixture runtime: scripted editors stand in for WebDriverAgent's, with delayed focus,
placeholders, a secure field and a lost reply. The WebDriverAgent request contract
itself is tested in native/runtime/iphone.rs against a loopback double. Every device
helper is trapped; nothing here is a device's behaviour or performance."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_fixture import Isolated, write_login_scenario, write_search_scenario  # noqa: E402
from robust_support import Session  # noqa: E402

SEARCH = {"x": 0.5, "y": 0.2}
NOTE = {"x": 0.5, "y": 0.35}
PIN = {"x": 0.5, "y": 0.5}
TEXT = "Grüße 日本 \U0001f3b5 مرحبا שלום é %s $(literal)"


class VerifiedFillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.box = Isolated(prefix="fill-")
        write_login_scenario(cls.box.fixtures)
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls.box.close()

    def tearDown(self):
        self.assertEqual(self.box.trapped(), "")

    def start(self, **scenario):
        """A fresh session on a new search scenario."""
        type(self).count += 1
        name = f"search{self.count}"
        write_search_scenario(self.box.fixtures, name, **scenario)
        return self.box.result("runtime.start", {"platform": "android", "device": f"fixture:{name}"})["session"]

    def fixture(self, session):
        return self.box.result("runtime.observe", {"session": session})["fixture"]

    def fill(self, session, text, at=SEARCH, **extra):
        return self.box.call("runtime.input", {"session": session, "action": "fill", "text": text, "verify_value": True, **at, **extra})

    def test_only_a_transport_that_reads_editors_back_offers_it(self):
        session = self.start()
        offered = self.box.result("runtime.state", {"session": session})["input_capabilities"]["fill_verification"]
        self.assertEqual((offered["supported"], offered["evidence"]), (True, "fixture"))
        login = self.box.result("runtime.start", {"platform": "android", "device": "fixture:login"})["session"]
        state = self.box.result("runtime.state", {"session": login})["input_capabilities"]["fill_verification"]
        self.assertFalse(state["supported"])
        self.assertIn("reason", state)
        before = self.fixture(login)["inputs"]
        reply = self.fill(login, "ana", at={"x": 0.5, "y": 0.5})
        self.assertEqual(reply["error"]["code"], "fill_verification_unsupported", reply)
        reply = self.box.call("runtime.actions", {"session": login, "steps": [{"action": "fill", "x": 0.5, "y": 0.5, "text": "ana", "verify_value": True}]})
        self.assertEqual(reply["error"]["code"], "fill_verification_unsupported", reply)
        self.assertEqual(reply["error"]["details"]["applied_steps"], [])
        self.assertEqual(self.fixture(login)["inputs"], before, "nothing was sent, not even the tap")
        # The arguments of a verified fill are refused elsewhere, before anything is sent.
        reply = self.box.call("runtime.input", {"session": login, "action": "fill", "x": 0.5, "y": 0.5, "text": "a", "submit": True})
        self.assertEqual(reply["error"]["code"], "invalid_argument", reply)
        self.assertEqual(self.fixture(login)["inputs"], before)
        self.box.call("runtime.stop", {"session": login})

    def test_the_value_is_read_back_and_a_matching_one_is_not_typed_again_over_cli_and_mcp(self):
        session = self.start()
        reply = self.fill(session, "tomacco")
        self.assertTrue(reply["ok"], reply)
        result = reply["result"]
        self.assertEqual((result["verified"], result["field_status"]), (True, "verified"))
        self.assertEqual(result["editor"]["name"], "query")
        self.assertEqual(result["before"]["state"], "placeholder", "the empty field showed its placeholder")
        self.assertGreaterEqual(result["focus_polls"], 2, "the focus came after the scenario's delay")
        self.assertEqual(result["fixture"]["field_writes"]["value"], 1)
        self.assertEqual(result["fixture"]["field_values"]["q-1"], "tomacco")
        # The same fill over MCP: the same operation, verified with nothing written.
        mcp = Session(self.box.env)
        try:
            mcp.initialize()
            reply = mcp.request(1, "tools/call", {"name": "runtime.input", "arguments": {
                "session": session, "action": "fill", "text": "tomacco", "verify_value": True, **SEARCH}}, timeout=60)
            structured = reply["result"]["structuredContent"]
            self.assertFalse(reply["result"]["isError"], reply)
            self.assertEqual(structured["field_status"], "already_matching")
            self.assertEqual(structured["writes"], [])
            reply = mcp.request(2, "tools/call", {"name": "runtime.input", "arguments": {
                "session": session, "action": "fill", "text": "x", "verify_value": True, **PIN}}, timeout=60)
            self.assertTrue(reply["result"]["isError"])
            self.assertEqual(reply["result"]["structuredContent"]["error"]["code"], "field_unsupported")
        finally:
            mcp.kill()
        after = self.fixture(session)
        self.assertEqual(after["field_writes"]["value"], 1, "the matching value was not typed again")
        self.assertEqual(after["field_writes"]["clear"], 1)
        # Full Unicode, then a legitimate clear to empty.
        reply = self.fill(session, TEXT, at=NOTE)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["value"]["chars"], len(TEXT))
        self.assertEqual(self.fixture(session)["field_values"]["note-1"], TEXT)
        reply = self.fill(session, "", at=NOTE)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["typed"], False)
        self.assertEqual(self.fixture(session)["field_values"]["note-1"], "")

    def test_a_placeholder_or_a_secure_field_is_never_taken_for_or_read_as_a_value(self):
        session = self.start()
        reply = self.fill(session, "Search")
        self.assertEqual(reply["error"]["code"], "field_uncertain", reply)
        self.assertEqual(reply["error"]["details"]["details"]["reason"], "value_matches_placeholder")
        reply = self.fill(session, "1357", at=PIN)
        self.assertEqual(reply["error"]["code"], "field_unsupported", reply)
        self.assertEqual(reply["error"]["details"]["details"]["reason"], "secure_field")
        self.assertNotIn("2468", str(reply))
        after = self.fixture(session)
        self.assertEqual(after["secure_reads"], 0, "the PIN's value was never requested")
        self.assertEqual(after["field_values"].get("pin-1", "2468"), "2468", "nothing was typed into it")
        self.assertEqual(after["field_writes"]["value"], 1, "only the placeholder case typed, once")

    def test_focus_that_stays_on_another_field_types_nothing(self):
        session = self.start(focused_element="note-1", focus_after_ms=0)
        reply = self.box.call("runtime.input", {"session": session, "action": "fill", "text": "tomacco", "verify_value": True,
                                                "x": 0.5, "y": 0.9, "focus_timeout_ms": 300})
        self.assertEqual(reply["error"]["code"], "field_wrong_target", reply)
        self.assertEqual(reply["error"]["details"]["details"]["focused"]["name"], "note")
        after = self.fixture(session)
        self.assertEqual(after["field_writes"], {"clear": 0, "value": 0, "submit": 0})
        self.assertEqual(after["field_values"].get("note-1", "draft"), "draft")
        # Named, the field that moved away from the tap is the intended one.
        reply = self.box.call("runtime.input", {"session": session, "action": "fill", "text": "tomacco", "verify_value": True,
                                                "x": 0.5, "y": 0.9, "field_name": "note", "focus_timeout_ms": 300})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(self.fixture(session)["field_values"]["note-1"], "tomacco")

    def test_search_is_submitted_once_and_a_wrong_destination_is_not_repeated(self):
        session = self.start()
        step = {"action": "fill", "text": "tomacco", "verify_value": True, "submit": True, **SEARCH}
        reply = self.box.call("runtime.actions", {"session": session, "steps": [dict(step, expect={"text": "Results for tomacco", "timeout_ms": 2000})]})
        self.assertTrue(reply["ok"], reply)
        record = reply["result"]["steps"][0]
        self.assertEqual((record["status"], record["field_verified"]), ("verified", True))
        self.assertEqual(record["result"]["submitted"], True)
        self.assertEqual(self.fixture(session)["field_writes"]["submit"], 1)
        self.box.result("runtime.input", {"session": session, "action": "key", "key": "back"})
        # Already matching, submitted once, and the expected screen does not come:
        # the batch stops, the step counts as applied, nothing is sent again.
        reply = self.box.call("runtime.actions", {"session": session, "steps": [
            dict(step, expect={"text": "Nowhere", "timeout_ms": 300}), {"action": "key", "key": "enter"}]})
        self.assertEqual(reply["error"]["code"], "postcondition_failed", reply)
        details = reply["error"]["details"]
        self.assertEqual((details["applied_steps"], details["uncertain_steps"], details["skipped"]), ([0], [], [1]))
        self.assertEqual(details["steps"][0]["result"]["field_status"], "already_matching")
        after = self.fixture(session)
        self.assertEqual(after["field_writes"], {"clear": 1, "value": 1, "submit": 2})

    def test_a_lost_reply_is_uncertain_and_the_batch_never_sends_it_again(self):
        session = self.start(write="lose_reply")
        before = self.fixture(session)
        reply = self.box.call("runtime.actions", {"session": session, "steps": [
            {"action": "fill", "text": "tomacco", "verify_value": True, "submit": True, **SEARCH},
            {"action": "key", "key": "enter"}]})
        self.assertEqual(reply["error"]["code"], "field_uncertain", reply)
        details = reply["error"]["details"]
        self.assertEqual((details["applied_steps"], details["uncertain_steps"], details["skipped"]), ([], [0], [1]))
        self.assertEqual(details["steps"][0]["status"], "uncertain")
        self.assertIn("never resend", reply["error"]["remedy"])
        after = self.fixture(session)
        self.assertEqual(after["field_writes"]["value"], 1, "typed exactly once")
        self.assertEqual(after["field_writes"]["submit"], 0, "an unverified value is never submitted")
        self.assertEqual(after["field_values"]["q-1"], "tomacco", "the text did land; only its answer was lost")
        self.assertEqual(after["inputs"], before["inputs"] + 1, "only the focusing tap besides the write; no Enter")


if __name__ == "__main__":
    unittest.main()
