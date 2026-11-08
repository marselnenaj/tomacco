"""Fresh observe context and partial failures on a loopback WDA double only."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import sys
import time
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]
from iphone_observe_fixture import CONTEXT, ObserveDouble


class IphoneObserveTests(unittest.TestCase):
    def setUp(self):
        self.double = ObserveDouble()

    def tearDown(self):
        try:
            self.double.assert_safe()
        finally:
            self.double.close()

    def test_overlaps_only_independent_reads_and_refreshes_each_observation(self):
        for revision in (1, 2):
            with self.subTest(revision=revision):
                self.double.plan(delay_ms=40, revision=revision, keyboard=revision == 2)
                begin = len(self.double.events())
                result = self.double.observe()
                self.assertEqual(result["app"]["name"], str(revision))
                self.assertEqual(result["keyboard"]["visible"], revision == 2)
                self.assertIs(result["focus"]["focused"], True)
                events = self.double.events()[begin:]
                starts = [e for e in events if e["event"] == "start"]
                for route in CONTEXT | {"/screenshot"}:
                    self.assertEqual(sum(e["route"] == route for e in starts), 1, starts)
                # Assert the request dependency, without a wall-clock threshold:
                # all three reads start before any context response completes.
                context = [e for e in events if e["route"] in CONTEXT]
                self.assertEqual([e["event"] for e in context[:3]], ["start"] * 3, context)
                self.assertEqual(next(e["method"] for e in starts if e["route"] == "/elements"), "POST")
                self.assertFalse(any(e["method"] == "DELETE" or e["route"].endswith("/actions") for e in starts))

    def test_context_false_skips_all_context_reads_but_captures_again(self):
        self.double.observe()
        begin = len(self.double.events())
        result = self.double.observe(context=False)
        starts = [e for e in self.double.events()[begin:] if e["event"] == "start"]
        self.assertEqual([e["route"] for e in starts], ["/screenshot"])
        for field in ("app", "keyboard", "focus"):
            self.assertFalse(result[field]["available"])

    def test_context_failures_remain_independent_and_do_not_poison_next_read(self):
        cases = [
            ("/wda/activeAppInfo", "unsupported operation", "app", None),
            ("/elements", "unknown error", "keyboard", None),
            ("/element/active", "no such element", "focus", False),
            ("/element/active", "not found", "focus", False),
            ("/element/active", "session refused", "focus", None),
        ]
        for route, error, field, focused in cases:
            with self.subTest(route=route, error=error):
                self.double.plan(revision=5, keyboard=True, faults={route: {"error": error, "status": 404}})
                result = self.double.observe()
                self.assertEqual(result[field]["available"], focused is not None, result)
                if focused is not None:
                    self.assertIs(result[field]["focused"], focused)
                else:
                    self.assertIn(error, result[field]["reason"])
                for other in {"app", "keyboard", "focus"} - {field}:
                    self.assertIs(result[other]["available"], True)
        self.double.plan(revision=6, keyboard=False)
        result = self.double.observe()
        self.assertEqual(result["app"]["name"], "6")
        self.assertFalse(result["keyboard"]["visible"])
        self.assertTrue(result["focus"]["focused"])

    def test_unanswered_context_read_is_unavailable_while_siblings_succeed(self):
        self.double.plan(faults={"/elements": {"error": "disconnect", "disconnect": True}})
        result = self.double.observe()
        self.assertFalse(result["keyboard"]["available"])
        self.assertTrue(result["app"]["available"])
        self.assertTrue(result["focus"]["focused"])

    def test_worker_waits_for_context_before_executing_the_next_input(self):
        self.double.plan(delay_ms=120)
        begin = len(self.double.events())
        with ThreadPoolExecutor(max_workers=2) as pool:
            observe = pool.submit(self.double.observe)
            deadline = time.monotonic() + 5
            while not any(e["route"] in CONTEXT for e in self.double.events()[begin:]):
                self.assertLess(time.monotonic(), deadline, "observe did not reach WDA")
                time.sleep(.005)
            action = pool.submit(self.double.result, "runtime.input", {"session": self.double.session,
                                                                       "action": "key", "key": "home"})
            observe.result(timeout=10)
            action.result(timeout=10)
        events = self.double.events()[begin:]
        finished = [e["at_ns"] for e in events if e["route"] in CONTEXT and e["event"] == "end"]
        acted = [e["at_ns"] for e in events if e["route"] == "/wda/homescreen" and e["event"] == "start"]
        self.assertEqual(len(finished), 3)
        self.assertEqual(len(acted), 1)
        self.assertGreater(acted[0], max(finished))


if __name__ == "__main__":
    unittest.main()
