"""A long MCP session: thousands of calls of several kinds (tool calls that succeed and
fail, resource reads, listings, pings, cancellations, oversized and malformed lines) and
the resident memory of the server sampled throughout. After a warm-up the growth must
stay small and not follow the call count.

Run: TOMACCO_SOAK=1 TOMACCOCTL=target/debug/tomaccoctl PYTHONPATH=tools:tests/python:tests:. \
     python3 -m unittest tests.python.test_mcp_memory
TOMACCO_MEMORY_CALLS sets the count (default 6000; 600 without TOMACCO_SOAK)."""
import os
import tempfile
import unittest
from pathlib import Path

import robust_support as rb

SOAK = bool(os.environ.get("TOMACCO_SOAK"))
CALLS = int(os.environ.get("TOMACCO_MEMORY_CALLS", "6000" if SOAK else "600"))


class McpMemory(unittest.TestCase):
    def test_resident_memory_stays_bounded_over_thousands_of_calls(self):
        with tempfile.TemporaryDirectory(prefix="mem-") as directory:
            home = Path(directory)
            env = rb.environment(home)
            session = rb.Session(env)
            try:
                session.initialize()
                job = None
                rb.initialise(env, home)
                job = rb.queue_job(env, home, ["true"], home)
                calls = [
                    ("tools/call", {"name": "job.list", "arguments": {"limit": 5}}),
                    ("tools/call", {"name": "job.get", "arguments": {"job_id": job}}),
                    ("tools/call", {"name": "job.get", "arguments": {"job_id": "0" * 32}}),
                    ("tools/call", {"name": "catalog.list", "arguments": {"platform": "ios"}}),
                    ("tools/call", {"name": "doctor", "arguments": {"bogus": 1}}),
                    ("tools/call", {"name": "nonesuch", "arguments": {}}),
                    ("tools/call", {"name": "job.wait", "arguments": {"job_id": job, "seconds": 1},
                                    "_meta": {"progressToken": "p"}}),
                    ("tools/list", {}), ("resources/list", {}), ("prompts/list", {}),
                    ("resources/read", {"uri": "tomacco://docs/agents"}),
                    ("ping", {}), ("nonesuch", {}),
                ]
                samples = []
                for number in range(CALLS):
                    method, params = calls[number % len(calls)]
                    session.request(number + 1, method, params, timeout=60)
                    if number % 7 == 0:
                        session.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                      "params": {"requestId": number}})
                    if number % 50 == 0:
                        session.send_raw(b"{broken\n")
                        while not session.replies.empty() or session.silent(0.001) is False:
                            pass
                    if number % (CALLS // 10) == 0:
                        samples.append((number, session.rss_kib()))
                samples.append((CALLS, session.rss_kib()))
                print("mcp rss (calls, KiB):", samples)
                warm = samples[1][1]
                self.assertLess(samples[-1][1] - warm, 20 * 1024, samples)
                self.assertEqual(session.process.poll(), None)
            finally:
                session.kill()


if __name__ == "__main__":
    unittest.main()
