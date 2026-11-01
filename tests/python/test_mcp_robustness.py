"""The MCP stdio server against a misbehaving client: malformed JSON-RPC, huge or
odd messages, unknown methods, notifications, cancellations and stdin closing in the
middle of a call. The server must answer as JSON-RPC 2.0 and MCP say, and neither
hang nor panic. No device or toolchain is needed; the home is a scratch directory.

Run: TOMACCOCTL=target/debug/tomaccoctl PYTHONPATH=tools:tests/python:tests:. \
     python3 -m unittest tests.python.test_mcp_robustness"""
import json
import os
import queue
import tempfile
import time
import unittest
from pathlib import Path

from native_client import binary
import robust_support as rb

LIMIT = 2 * 1024 * 1024


class McpRobustness(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mcp-")
        self.home = Path(self.directory.name)
        self.env = rb.environment(self.home)
        self.sessions = []

    def tearDown(self):
        for session in self.sessions:
            session.kill()
        self.directory.cleanup()

    def start(self, initialize=True):
        session = rb.Session(self.env)
        self.sessions.append(session)
        if initialize:
            session.initialize()
        return session

    def assertAlive(self, session):
        reply = session.request(900001, "ping", {})
        self.assertEqual(reply["result"], {})
        self.assertIsNone(session.process.poll())

    def error(self, session, raw, code, ident=None):
        session.send_raw(raw)
        reply = session.reply()
        self.assertEqual(reply["error"]["code"], code, reply)
        self.assertEqual(reply["id"], ident, reply)
        self.assertAlive(session)

    def test_malformed_json_gets_a_parse_error_and_the_session_goes_on(self):
        session = self.start()
        for raw in [b"{not json}\n", b"\n", b"   \n", b'{"jsonrpc":"2.0","id":1,\n', b"\xff\xfe\x00\n",
                    b"\x00\x00\x00\n", b'{"a":' + b"[" * 300 + b"]" * 300 + b"}\n", b'"\xc3\x28"\n',
                    b"nul\n", b'{"jsonrpc":"2.0","id":1,"method":"ping"}}\n']:
            with self.subTest(raw=raw[:30]):
                self.error(session, raw, -32700)

    def test_a_message_that_is_not_a_request_object_is_an_invalid_request(self):
        session = self.start()
        for raw in [b"[]\n", b"[1,2]\n", b'[{"jsonrpc":"2.0","id":1,"method":"ping"}]\n', b"1\n", b'"text"\n',
                    b"null\n", b"true\n", b"{}\n", b'{"jsonrpc":"1.0","id":1,"method":"ping"}\n',
                    b'{"id":1,"method":"ping"}\n']:
            with self.subTest(raw=raw[:40]):
                self.error(session, raw, -32600)

    def test_a_request_with_a_bad_method_or_id_is_refused(self):
        session = self.start()
        for message in [{"jsonrpc": "2.0", "id": 1, "method": 5}, {"jsonrpc": "2.0", "id": 1, "method": None},
                        {"jsonrpc": "2.0", "id": 1, "method": ["ping"]}]:
            with self.subTest(message=message):
                self.error(session, json.dumps(message).encode() + b"\n", -32600)
        for ident in [None, True, [1], {"a": 1}]:
            with self.subTest(ident=ident):
                self.error(session, json.dumps({"jsonrpc": "2.0", "id": ident, "method": "ping"}).encode() + b"\n", -32600)
        # Strings and numbers, including odd ones, are fine and echoed back as they came.
        for ident in ["", "a b", "é\u0000", 0, -1, 2**53, 1.5]:
            with self.subTest(ident=ident):
                self.assertEqual(session.request(ident, "ping", {})["result"], {})

    def test_params_must_be_an_object(self):
        session = self.start()
        for params in [None, 1, "x", [], [1]]:
            with self.subTest(params=params):
                session.send({"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": params})
                reply = session.reply()
                self.assertEqual((reply["id"], reply["error"]["code"]), (5, -32602), reply)
        self.assertAlive(session)

    def test_unknown_methods_are_answered_and_unknown_notifications_are_not(self):
        session = self.start()
        for method in ["nonesuch", "tools/nonesuch", "", "initialize/x", "notifications/initialized", "x" * 5000]:
            with self.subTest(method=method[:20]):
                reply = session.request(7, method, {})
                self.assertEqual(reply["error"]["code"], -32601, reply)
        for method in ["nonesuch", "notifications/nonesuch", "ping", "tools/list", "tools/call", "initialize",
                       "notifications/cancelled", "logging/setLevel", "resources/read"]:
            with self.subTest(notification=method):
                session.send({"jsonrpc": "2.0", "method": method, "params": {"name": "doctor", "requestId": 1}})
                session.send({"jsonrpc": "2.0", "method": method})
                self.assertTrue(session.silent(0.2), method)
        self.assertAlive(session)

    def test_requests_before_initialize_are_refused_except_ping(self):
        session = self.start(initialize=False)
        self.assertEqual(session.request(1, "ping", {})["result"], {})
        for method in ["tools/list", "tools/call", "resources/list", "prompts/list"]:
            self.assertEqual(session.request(2, method, {})["error"]["code"], -32000, method)
        session.initialize()
        self.assertIn("tools", session.request(3, "tools/list", {})["result"])
        # A second initialize is answered again, not a crash.
        self.assertEqual(session.request(4, "initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"], "2025-11-25")

    def test_garbage_initialize_parameters_do_not_crash(self):
        for params in [{}, {"protocolVersion": 5}, {"clientInfo": 1}, {"clientInfo": "x", "capabilities": []},
                       {"capabilities": {"elicitation": 3}}, {"clientInfo": {"name": ["x"], "version": {}}},
                       {"clientInfo": {"name": "é" * 100000}}]:
            with self.subTest(params=str(params)[:50]):
                session = self.start(initialize=False)
                reply = session.request(1, "initialize", params)
                self.assertIn("result", reply)
                self.assertAlive(session)
                session.kill()

    def test_wrong_tool_call_shapes_get_errors_not_panics(self):
        session = self.start()
        shapes = [{}, {"name": 5}, {"name": None}, {"name": "nonesuch"}, {"name": "doctor", "arguments": []},
                  {"name": "doctor", "arguments": "x"}, {"name": "doctor", "arguments": {"a": 1}},
                  {"name": "job.get", "arguments": {"job_id": {"a": [1]}}},
                  {"name": "job.get", "arguments": {"job_id": "\u0000"}}, {"name": "x" * 100000},
                  {"name": "job.get", "arguments": {"job_id": "a" * 100000}}, {"name": "job.get", "_meta": 5},
                  {"name": "job.get", "arguments": {"job_id": "0" * 32}, "_meta": {"progressToken": {"x": 1}}},
                  {"name": "job.get", "arguments": {"job_id": "0" * 32}, "_meta": {"progressToken": [1]}}]
        for params in shapes:
            with self.subTest(params=str(params)[:60]):
                reply = session.request(11, "tools/call", params, timeout=60)
                self.assertTrue("error" in reply or reply["result"].get("isError"), reply)
        for method, params in [("resources/read", {}), ("resources/read", {"uri": 5}), ("resources/read", {"uri": "x://y"}),
                               ("prompts/get", {}), ("prompts/get", {"name": "nonesuch"}),
                               ("logging/setLevel", {}), ("logging/setLevel", {"level": 5})]:
            with self.subTest(method=method, params=str(params)):
                reply = session.request(12, method, params)
                self.assertIn("error", reply, reply)
        self.assertAlive(session)

    def test_a_message_at_the_limit_is_read_and_one_over_it_is_refused_without_ending_the_session(self):
        session = self.start()
        filler = "a" * (LIMIT - 200)
        session.send({"jsonrpc": "2.0", "id": 21, "method": "tools/call",
                      "params": {"name": "job.get", "arguments": {"job_id": filler}}})
        reply = session.reply(60)
        self.assertEqual(reply["id"], 21)
        self.assertTrue("error" in reply or reply["result"]["isError"])
        # Over the limit: refused, the rest of the line skipped, the session usable.
        session.send_raw(b'{"jsonrpc":"2.0","id":22,"method":"ping","params":{"x":"' + b"a" * (LIMIT * 3) + b'"}}\n')
        reply = session.reply(60)
        self.assertEqual(reply["error"]["code"], -32700, reply)
        self.assertIn("2 MiB", reply["error"]["message"])
        self.assertAlive(session)

    def test_a_line_without_a_newline_that_never_ends_does_not_grow_the_server(self):
        session = self.start()
        before = session.rss_kib()
        for _ in range(40):
            session.send_raw(b"a" * (1024 * 1024))
            time.sleep(0.01)
            if not session.replies.empty():
                break
        reply = session.reply(30)
        self.assertEqual(reply["error"]["code"], -32700)
        for _ in range(10):
            session.send_raw(b"a" * (1024 * 1024))
        self.assertLess(session.rss_kib() - before, 64 * 1024)
        session.send_raw(b"\n")
        self.assertAlive(session)

    def test_the_last_line_without_a_newline_is_still_answered(self):
        session = self.start()
        session.send_raw(b'{"jsonrpc":"2.0","id":31,"method":"ping"}')
        session.process.stdin.close()
        reply = session.reply()
        self.assertEqual(reply["id"], 31)
        self.assertEqual(session.process.wait(30), 0)

    def long_job(self, seconds=60):
        rb.initialise(self.env, self.home)
        return rb.queue_job(self.env, self.home, ["sleep", str(seconds)], self.home)

    def wait_state(self, job, state, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            current = [r for r in rb.rows(self.home) if r["id"] == job][0]["state"]
            if current == state:
                return
            time.sleep(0.1)
        self.fail(f"job {job} never became {state}")

    def wait_call(self, job, ident, seconds=50):
        return {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                "params": {"name": "job.wait", "arguments": {"job_id": job, "seconds": seconds}}}

    def test_cancelling_a_running_call_ends_it_without_a_response_and_the_job_goes_on(self):
        session = self.start()
        job = self.long_job()
        self.wait_state(job, "running")
        session.send(self.wait_call(job, 41))
        self.assertTrue(session.silent(0.5))
        session.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 41}})
        self.assertTrue(session.silent(1.5))
        # The ID is free again once the cancelled call has ended.
        deadline = time.time() + 15
        while True:
            session.send({"jsonrpc": "2.0", "id": 41, "method": "ping"})
            reply = session.reply()
            if "result" in reply:
                break
            self.assertEqual(reply["error"]["code"], -32600)
            self.assertLess(time.time(), deadline)
            time.sleep(0.2)
        self.assertEqual([r for r in rb.rows(self.home) if r["id"] == job][0]["state"], "running")
        rb.call(self.env, "job.cancel", {"job_id": job})

    def test_odd_cancellations_are_ignored(self):
        session = self.start()
        for params in [{}, {"requestId": None}, {"requestId": 99}, {"requestId": "x"}, {"requestId": [1]}, {"requestId": {}},
                       "x", 5, None, []]:
            session.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": params})
        # With an ID it is a request, not a notification: answered, not obeyed.
        reply = session.request(51, "notifications/cancelled", {"requestId": 1})
        self.assertIn("error", reply)
        self.assertTrue(session.silent(0.3))
        self.assertAlive(session)

    def test_a_request_id_in_progress_is_refused_and_the_sixteen_call_limit_holds(self):
        session = self.start()
        job = self.long_job()
        self.wait_state(job, "running")
        session.send(self.wait_call(job, 61))
        session.send(self.wait_call(job, 61))
        reply = session.reply()
        self.assertEqual((reply["id"], reply["error"]["code"]), (61, -32600), reply)
        for ident in range(62, 62 + 20):
            session.send(self.wait_call(job, ident))
        limited = [session.reply() for _ in range(5)]
        self.assertEqual({r["error"]["code"] for r in limited}, {-32001}, limited)
        for ident in [61, *range(62, 77)]:
            session.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": ident}})
        self.assertTrue(session.silent(1))
        self.assertAlive(session)
        rb.call(self.env, "job.cancel", {"job_id": job})

    def test_closing_stdin_in_the_middle_of_a_call_ends_the_server_promptly(self):
        session = self.start()
        job = self.long_job()
        self.wait_state(job, "running")
        session.send(self.wait_call(job, 71, seconds=50))
        time.sleep(0.5)
        started = time.time()
        status = session.finish(timeout=30)
        self.assertEqual(status, 0)
        self.assertLess(time.time() - started, 15)
        self.assertNotIn(b"panicked", session.stderr)
        self.assertEqual([r for r in rb.rows(self.home) if r["id"] == job][0]["state"], "running")
        rb.call(self.env, "job.cancel", {"job_id": job})

    def test_a_client_that_goes_away_without_reading_does_not_hang_the_server(self):
        import subprocess
        process = subprocess.Popen([binary(), "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=self.env)
        process.stdout.close()
        try:
            for ident in range(1, 50):
                process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": ident, "method": "ping"}).encode() + b"\n")
                process.stdin.flush()
                time.sleep(0.01)
        except OSError:
            pass
        try:
            process.stdin.close()
        except OSError:
            pass
        process.wait(30)
        self.assertNotIn(b"panicked", process.stderr.read())

    def test_a_fast_burst_of_mixed_good_and_bad_lines_is_answered_in_order_of_arrival(self):
        session = self.start()
        lines = []
        for index in range(300):
            lines.append(json.dumps({"jsonrpc": "2.0", "id": 1000 + index, "method": "ping"}).encode())
            lines.append(b"{broken" if index % 3 == 0 else b"[]")
        session.send_raw(b"\n".join(lines) + b"\n")
        seen = [session.reply(60) for _ in range(600)]
        pings = [r["id"] for r in seen if r.get("result") == {}]
        self.assertEqual(pings, list(range(1000, 1300)))
        self.assertAlive(session)


if __name__ == "__main__":
    unittest.main()
