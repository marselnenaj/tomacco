"""The PhoneUse-derived features together, through the compiled CLI on the offline
fixture runtime: a verified fill (runtime/field.rs) under a device reservation and a
request identity (coordination.rs, coordination/ledger.rs).

The point is the seam between them. A verified fill that sent text whose effect is
unknown must be recorded as unknown, never as a refusal the same request_id could run
again; a fill that provably sent nothing may be repeated; a reserved device takes a
verified fill only with its token. The fixture counts every clear, write and submit,
so "never typed again" is observed, not inferred. Every device helper is trapped;
nothing here touches hardware or measures a device."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_fixture import Isolated, write_login_scenario, write_search_scenario  # noqa: E402

SEARCH = {"x": 0.5, "y": 0.2}
SECRET = "Grüße tomacco 4242"


class VerifiedFillUnderCoordination(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.box = Isolated(prefix="phoneuse-")
        write_login_scenario(cls.box.fixtures)
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls.box.close()

    def tearDown(self):
        self.assertEqual(self.box.trapped(), "", "a device helper was reached")

    def start(self, **scenario):
        type(self).count += 1
        name = f"psearch{self.count}"
        write_search_scenario(self.box.fixtures, name, **scenario)
        return self.box.result("runtime.start", {"platform": "android", "device": f"fixture:{name}"})["session"]

    def fixture(self, session):
        return self.box.result("runtime.observe", {"session": session})["fixture"]

    def stored(self):
        """Every byte the coordination state keeps on disk."""
        root = Path(self.box.env["TOMACCO_HOME"]) / "coordination"
        return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())

    def test_an_uncertain_verified_fill_in_a_batch_is_unknown_and_never_typed_again(self):
        session = self.start(write="lose_reply")
        token = self.box.result("device.reservation.acquire", {"device": session, "owner": "agent"})["reservation_token"]
        batch = {"session": session, "reservation_token": token, "request_id": "search-1",
                 "steps": [{"action": "fill", "text": SECRET, "verify_value": True, "submit": True, **SEARCH}]}
        reply = self.box.call("runtime.actions", batch)
        self.assertEqual(reply["error"]["code"], "field_uncertain", reply)
        details = reply["error"]["details"]
        self.assertEqual((details["applied_steps"], details["uncertain_steps"]), ([], [0]))
        # Not "refused": that would let the same request_id type it a second time.
        self.assertEqual((details["request"]["state"], details["request"]["status"]), ("unknown", "unknown"), details["request"])
        self.assertEqual(self.fixture(session)["field_writes"]["value"], 1)

        repeat = self.box.call("runtime.actions", batch)
        self.assertEqual(repeat["error"]["code"], "request_outcome_unknown", repeat)
        self.assertEqual(repeat["error"]["details"]["request"]["replayed"], False)
        after = self.fixture(session)
        self.assertEqual(after["field_writes"]["value"], 1, "the repeat typed nothing")
        self.assertEqual(after["field_writes"]["submit"], 0, "an unverified value is never submitted")

        record = self.box.result("device.request.get", {"request_id": "search-1"})["request"]
        self.assertEqual(record["state"], "unknown", record)
        self.assertEqual([child["state"] for child in record["children"]], ["unknown"], record)
        # Neither the typed text nor the token is kept or shown.
        shown = json.dumps(record) + json.dumps(repeat)
        self.assertNotIn(SECRET, shown)
        self.assertNotIn(token, shown)
        self.assertNotIn(SECRET.encode(), self.stored())
        self.assertNotIn(token.encode(), self.stored())
        self.box.result("device.reservation.release", {"device": session, "reservation_token": token})

    def test_a_single_uncertain_verified_fill_is_unknown_and_never_typed_again(self):
        session = self.start(write="lose_reply")
        fill = {"session": session, "action": "fill", "text": SECRET, "verify_value": True, "request_id": "one-fill", **SEARCH}
        reply = self.box.call("runtime.input", fill)
        self.assertEqual(reply["error"]["code"], "field_uncertain", reply)
        self.assertEqual(reply["error"]["details"]["request"]["status"], "unknown", reply)
        repeat = self.box.call("runtime.input", fill)
        self.assertEqual(repeat["error"]["code"], "request_outcome_unknown", repeat)
        self.assertEqual(self.fixture(session)["field_writes"]["value"], 1)
        # Changed text under the same identity is another request.
        conflict = self.box.call("runtime.input", dict(fill, text=SECRET + "!"))
        self.assertEqual(conflict["error"]["code"], "request_conflict", conflict)
        self.assertEqual(self.fixture(session)["field_writes"]["value"], 1)

    def test_a_verified_value_whose_return_was_not_sent_counts_as_applied(self):
        session = self.start(write="blur")
        batch = {"session": session, "request_id": "search-blur", "steps": [
            {"action": "fill", "text": "tomacco", "verify_value": True, "submit": True, **SEARCH},
            {"action": "key", "key": "enter"}]}
        reply = self.box.call("runtime.actions", batch)
        self.assertEqual(reply["error"]["code"], "field_submit_not_sent", reply)
        details = reply["error"]["details"]
        # The text is in the field: the step reached the device and must not be resent.
        self.assertEqual((details["applied_steps"], details["uncertain_steps"], details["skipped"]), ([0], [], [1]))
        self.assertEqual(details["steps"][0]["status"], "applied")
        self.assertEqual(details["request"]["status"], "failed", details["request"])
        repeat = self.box.call("runtime.actions", batch)
        self.assertEqual(repeat["error"]["code"], "field_submit_not_sent", repeat)
        self.assertEqual(repeat["error"]["details"]["request"]["retained"], True, "the recorded answer, not a second run")
        after = self.fixture(session)
        self.assertEqual((after["field_writes"]["value"], after["field_writes"]["submit"]), (1, 0))
        self.assertEqual(after["field_values"]["q-1"], "tomacco")

    def test_a_verified_fill_that_sent_nothing_is_refused_and_may_run_again(self):
        login = self.box.result("runtime.start", {"platform": "android", "device": "fixture:login"})["session"]
        before = self.fixture(login)["inputs"]
        fill = {"session": login, "action": "fill", "text": "ana", "verify_value": True, "request_id": "no-reader", "x": 0.5, "y": 0.5}
        for _ in range(2):
            reply = self.box.call("runtime.input", fill)
            self.assertEqual(reply["error"]["code"], "fill_verification_unsupported", reply)
            self.assertEqual(reply["error"]["details"]["request"]["status"], "refused", reply)
            self.assertEqual(reply["error"]["details"]["request"]["retained"], False, "it ran again, not a recorded answer")
        self.assertEqual(self.fixture(login)["inputs"], before, "nothing was sent, not even the tap")
        self.box.call("runtime.stop", {"session": login})

    def test_a_reserved_device_takes_a_verified_fill_only_with_its_token(self):
        session = self.start()
        token = self.box.result("device.reservation.acquire", {"device": session, "owner": "agent-a"})["reservation_token"]
        fill = {"session": session, "action": "fill", "text": "tomacco", "verify_value": True, **SEARCH}
        reply = self.box.call("runtime.input", fill)
        self.assertEqual(reply["error"]["code"], "device_reserved", reply)
        step = {k: v for k, v in fill.items() if k != "session"}
        reply = self.box.call("runtime.actions", {"session": session, "steps": [step]})
        self.assertEqual(reply["error"]["code"], "device_reserved", reply)
        untouched = self.fixture(session)
        self.assertEqual(untouched["field_writes"], {"clear": 0, "value": 0, "submit": 0})
        self.assertEqual(untouched["inputs"], 0, "not even the focusing tap")

        reply = self.box.call("runtime.input", dict(fill, reservation_token=token, request_id="fill-ok"))
        self.assertTrue(reply["ok"], reply)
        self.assertEqual((reply["result"]["field_status"], reply["result"]["request"]["status"]), ("verified", "applied"))
        repeat = self.box.call("runtime.input", dict(fill, reservation_token=token, request_id="fill-ok"))
        self.assertTrue(repeat["ok"], repeat)
        self.assertEqual(repeat["result"]["request"]["retained"], True)
        self.assertEqual(self.fixture(session)["field_writes"]["value"], 1, "the recorded answer, not a second fill")

        # Released: the old token no longer admits anything, and nothing is typed.
        self.box.result("device.reservation.release", {"device": session, "reservation_token": token})
        reply = self.box.call("runtime.input", dict(fill, text="again", reservation_token=token))
        self.assertEqual(reply["error"]["code"], "reservation_released", reply)
        self.assertEqual(self.fixture(session)["field_writes"]["value"], 1)


if __name__ == "__main__":
    unittest.main()
