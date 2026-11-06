"""Human handoff through the compiled CLI and the MCP server: a request outlives the
process that made it, the desktop or a terminal completes it, an agent over MCP
cannot, and the wait answers a fresh look at the session instead of a replay."""
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_fixture import Isolated, write_login_scenario  # noqa: E402
from robust_support import Session  # noqa: E402


class HandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.box = Isolated()
        write_login_scenario(cls.box.fixtures)
        cls.session = cls.box.result("runtime.start", {"platform": "android", "device": "fixture:login"})["session"]

    @classmethod
    def tearDownClass(cls):
        cls.box.close()

    def mcp_call(self, mcp, ident, name, arguments):
        reply = mcp.request(ident, "tools/call", {"name": name, "arguments": arguments})
        text = next(c["text"] for c in reply["result"]["content"] if c["type"] == "text")
        return reply["result"]["isError"], json.loads(text)

    def test_a_request_survives_processes_and_only_the_person_completes_it(self):
        made = self.box.result("handoff.request", {"reason": "Confirm the purchase with Face ID", "kind": "purchase",
                                                    "session": self.session, "instructions": "Look at the phone"})
        self.assertEqual(made["state"], "waiting")
        self.assertIn("Do not type", made["note"])
        # Another process sees it; the desktop's poll is handoff.list.
        listed = self.box.result("handoff.list", {})
        self.assertEqual([h["id"] for h in listed["handoffs"]], [made["id"]])
        # An agent over MCP may wait, but not complete it for the person.
        mcp = Session(self.box.env)
        try:
            mcp.initialize()
            failed, error = self.mcp_call(mcp, 1, "handoff.complete", {"id": made["id"]})
            self.assertTrue(failed)
            self.assertEqual(error["error"]["code"], "handoff_person_only")
            failed, waited = self.mcp_call(mcp, 2, "handoff.wait", {"id": made["id"], "seconds": 1})
            self.assertFalse(failed)
            self.assertEqual(waited["state"], "waiting")
            self.assertEqual(self.box.result("handoff.get", {"id": made["id"]})["state"], "waiting")
            # The person completes it from a terminal (the desktop's banner calls the same operation).
            done = self.box.result("handoff.complete", {"id": made["id"], "note": "paid"})
            self.assertEqual((done["already"], done["handoff"]["completed"]["by"]), (False, "cli"))
            failed, waited = self.mcp_call(mcp, 3, "handoff.wait", {"id": made["id"], "seconds": 5})
            self.assertFalse(failed)
            self.assertEqual(waited["state"], "completed")
            self.assertTrue(waited["resume"]["observe_first"])
            self.assertEqual(waited["resume"]["session"], self.session)
            self.assertEqual(len(waited["observation"]["frame"]["id"]), 16, "a fresh look, not a replay")
            self.assertEqual(waited["observation"]["evidence"], "fixture")
        finally:
            mcp.kill()
        self.assertEqual(self.box.error("handoff.cancel", {"id": made["id"]})["code"], "handoff_not_waiting")
        self.assertEqual(self.box.result("handoff.list", {"state": "completed"})["count"], 1)
        self.assertEqual(self.box.trapped(), "")

    def test_an_agent_can_withdraw_its_own_request(self):
        made = self.box.result("handoff.request", {"reason": "Unlock the phone", "kind": "passcode"})
        gone = self.box.result("handoff.cancel", {"id": made["id"], "reason": "not needed"})
        self.assertEqual(gone["handoff"]["state"], "cancelled")
        self.assertEqual(self.box.error("handoff.complete", {"id": made["id"]})["code"], "handoff_not_waiting")
        self.assertEqual(self.box.error("handoff.request", {"reason": "x", "kind": "dance"})["code"], "invalid_argument")
        self.assertEqual(self.box.error("handoff.get", {"id": "0" * 32})["code"], "handoff_not_found")


if __name__ == "__main__":
    unittest.main()
