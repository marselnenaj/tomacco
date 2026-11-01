"""The guards around calls from agents, through the compiled CLI and its MCP server:
the agent policy, write roots, desktop approval, the audit log and the session limits."""
import hashlib
import json
import os
import queue
import socket
import stat
import subprocess
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_client import ROOT, StudioError, binary, dispatch, jobs
from host_patience import PATIENCE
from test_product import Isolated


class Mcp:
    """One MCP session with tomaccoctl mcp, request by request."""

    def __init__(self, *arguments, client="test-agent", env=None, cwd=None):
        self.process = subprocess.Popen([binary(), "mcp", *arguments], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, env=env, cwd=cwd)
        self.replies = queue.Queue()
        self.next = 0
        threading.Thread(target=self._read, daemon=True).start()
        reply = self.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                            "clientInfo": {"name": client, "version": "9.9"}})
        assert reply["result"]["serverInfo"]["name"] == "tomacco", reply

    def _read(self):
        for line in self.process.stdout:
            self.replies.put(json.loads(line))

    def request(self, method, params):
        self.next += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        while True:
            reply = self.replies.get(timeout=60 * PATIENCE)
            if reply.get("id") == self.next:
                return reply

    def call(self, name, arguments=None):
        """The result, or the error object of a failed call."""
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})["result"]
        payload = json.loads(result["content"][0]["text"])
        return (payload["error"], True) if result["isError"] else (payload, False)

    def close(self):
        self.process.stdin.close()
        self.process.wait(10 * PATIENCE)
        self.process.stdout.close()
        self.process.stderr.close()


class GuardTests(Isolated):
    def setUp(self):
        super().setUp()
        self.sessions = []

    def tearDown(self):
        for session in self.sessions:
            if session.process.poll() is None:
                session.close()
        super().tearDown()

    def mcp(self, *arguments, **options):
        session = Mcp(*arguments, **options)
        self.sessions.append(session)
        return session

    def project(self, name, tasks=None):
        directory = self.root / name
        directory.mkdir(parents=True)
        (directory / "tomacco.json").write_text(json.dumps({"tasks": tasks or {"check": ["true"]}}))
        return directory

    def audit(self, **filters):
        return dispatch("audit.list", {"limit": 1000, **filters})["entries"]

    def test_a_read_only_server_refuses_changes_and_says_why(self):
        adb = self.root / "adb"
        adb.write_text("#!/bin/sh\n")
        adb.chmod(0o755)
        session = self.mcp("--policy", "read_only")
        error, failed = session.call("settings.configure", {"executables": {"adb": str(adb)}})
        self.assertTrue(failed)
        self.assertEqual(error["code"], "policy_denied")
        self.assertIn("read_only", error["message"])
        self.assertIn("agent.policy", error["remedy"])
        self.assertEqual(error["details"]["chosen_by"], "server")
        self.assertNotIn("executables", dispatch("settings.get", {}))
        result, failed = session.call("catalog.list", {"platform": "ios"})
        self.assertFalse(failed)
        policy, _ = session.call("agent.policy")
        self.assertEqual((policy["policy"]["name"], policy["chosen_by"], policy["client"]), ("read_only", "server", "test-agent"))
        # An unknown policy allows nothing, not everything.
        error, failed = self.mcp("--policy", "nonesuch").call("catalog.list", {})
        self.assertEqual(error["code"], "policy_denied")
        # The variable does the same as the flag; a bad flag stops the server.
        error, _ = self.mcp(env={**os.environ, "TOMACCO_POLICY": "read_only"}).call("project.create", {"path": str(self.root / "new"), "template": "android"})
        self.assertEqual(error["code"], "policy_denied")
        self.assertFalse((self.root / "new").exists())
        refused = subprocess.run([binary(), "mcp", "--polcy", "x"], input="", capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)

    def test_a_client_gets_its_policy_and_no_agent_may_change_it(self):
        dispatch("settings.configure", {"agent_policy": {
            "clients": {"cautious-agent": "no_destructive"},
            "policies": {"builds-only": {"allow": ["project.", "job.", "agent.policy"], "deny": ["job.cancel"], "writes": "anywhere"}},
            "mcp": "builds-only"}})
        error, _ = self.mcp(client="cautious-agent").call("job.cancel", {"job_id": "0" * 32})
        self.assertEqual((error["code"], error["details"]["chosen_by"]), ("policy_denied", "client"))
        error, _ = self.mcp(client="other").call("job.cancel", {"job_id": "0" * 32})
        self.assertEqual(error["code"], "policy_denied")
        self.assertIn("deny list names job.cancel", error["message"])
        session = self.mcp(client="other")
        error, _ = session.call("catalog.list", {})
        self.assertIn("allow list does not name it", error["message"])
        _, failed = session.call("project.inspect", {"path": str(self.project("app"))})
        self.assertFalse(failed)
        # Changing the policy is refused over MCP, even under full.
        error, _ = self.mcp("--policy", "full").call("settings.configure", {"agent_policy": {}})
        self.assertEqual(error["code"], "policy_denied")
        self.assertIn("builds-only", json.dumps(dispatch("settings.get", {})))
        with self.assertRaises(StudioError) as refused:
            dispatch("settings.configure", {"agent_policy": {"mcp": "nonesuch"}})
        self.assertEqual(refused.exception.code, "invalid_argument")
        dispatch("settings.configure", {"agent_policy": {}})
        _, failed = self.mcp(client="cautious-agent").call("catalog.list", {})
        self.assertFalse(failed)

    def test_writes_stay_in_the_roots_under_a_limiting_policy(self):
        project = self.project("app")
        temporary = self.root / "tmp"
        temporary.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        environment = {**os.environ, "TMPDIR": str(temporary), "TOMACCO_POLICY": "no_destructive"}
        session = self.mcp(env=environment, cwd=project)
        profile = json.loads((ROOT / "assets/models.json").read_text())[0]["id"]
        _, failed = session.call("catalog.export", {"profile_id": profile, "path": str(project / "profile.json")})
        self.assertFalse(failed)
        self.assertTrue((project / "profile.json").is_file())
        error, _ = session.call("catalog.export", {"profile_id": profile, "path": str(outside / "profile.json")})
        self.assertEqual(error["code"], "path_outside_roots")
        self.assertIn(str(project), error["remedy"])
        self.assertFalse((outside / "profile.json").exists())
        (project / "escape").symlink_to(outside)
        error, _ = session.call("catalog.export", {"profile_id": profile, "path": str(project / "escape/profile.json")})
        self.assertEqual(error["code"], "path_outside_roots")
        error, _ = session.call("diagnostics.bundle", {"path": str(self.root / "state/settings.json")})
        self.assertEqual(error["code"], "path_outside_roots")
        # TOMACCO_WRITE_ROOTS opens a folder for this server.
        opened = self.mcp(env={**environment, "TOMACCO_WRITE_ROOTS": str(outside)}, cwd=project)
        _, failed = opened.call("catalog.export", {"profile_id": profile, "path": str(outside / "profile.json")})
        self.assertFalse(failed)
        policy, _ = opened.call("agent.policy")
        self.assertIn(str(outside), policy["write_roots"]["open"])

    def desktop(self, answers):
        """A stand-in desktop on the preview socket that answers approve requests in turn."""
        data = self.root / "state"
        directory = data / "runtime"
        if len(str(directory)) + 54 >= 104:
            directory = Path("/tmp") / f"tomacco-{os.getuid()}-{hashlib.sha256(os.fsencode(data)).hexdigest()[:16]}"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / "preview-main.sock"
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(path))
        server.listen()
        asked = []

        def serve():
            for answer in answers:
                connection, _ = server.accept()
                with connection:
                    request = json.loads(connection.makefile("rb").readline())
                    asked.append(request)
                    connection.sendall(json.dumps({"id": 1, "result": answer}).encode() + b"\n")
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        def stop():
            server.close()
            path.unlink()
            if directory.parent == Path("/tmp"):
                directory.rmdir()
        self.addCleanup(stop)
        return asked

    def test_destructive_calls_wait_for_a_yes_at_the_desktop(self):
        project = self.project("app")
        dispatch("settings.configure", {"agent_policy": {"approval": {"destructive": True, "timeout_seconds": 5}}})
        session = self.mcp(client="writer")
        write = {"path": str(project), "file": "notes.txt", "text": "token=ghp_0123456789abcdefghijABCDEFGHIJ", "if_match": ""}
        # No desktop is open: the call runs, as without_desktop allows by default.
        _, failed = session.call("files.write", {**write, "file": "first.txt"})
        self.assertFalse(failed)
        asked = self.desktop([{"approved": False, "reason": "denied"}, {"approved": True}])
        error, _ = session.call("files.write", write)
        self.assertEqual(error["code"], "approval_denied")
        self.assertFalse((project / "notes.txt").exists())
        _, failed = session.call("files.write", write)
        self.assertFalse(failed)
        self.assertTrue((project / "notes.txt").is_file())
        question = asked[0]
        self.assertEqual(question["method"], "approve")
        self.assertEqual((question["params"]["operation"], question["params"]["client"]), ("files.write", "writer"))
        self.assertEqual(question["params"]["title"], "Save source file")
        self.assertNotIn("ghp_", json.dumps(question))
        self.assertEqual(question["params"]["timeout_seconds"], 5)
        # Not destructive, or from a terminal: nobody is asked.
        _, failed = session.call("catalog.list", {})
        self.assertFalse(failed)
        dispatch("files.write", {**write, "file": "third.txt"})
        self.assertEqual(len(asked), 2)
        approvals = [(e["operation"], e["approval"], e["ok"]) for e in self.audit(channel="mcp")]
        self.assertEqual(approvals, [("files.write", "approved", True), ("files.write", "denied", False),
                                     ("files.write", "no_desktop", True)])

    def test_the_audit_log_records_changes_and_refusals_with_the_caller(self):
        project = self.project("app")
        environment = {**os.environ, "TOMACCO_CLIENT": "nightly-script"}
        subprocess.run([binary(), "call", "remote.configure", json.dumps({"name": "mac", "host": "mac.local", "user": "me", "password": "hunter2-secret"})],
                       env=environment, capture_output=True, text=True)
        subprocess.run([binary(), "call", "catalog.list", "{}"], env=environment, capture_output=True, text=True)
        session = self.mcp("--policy", "read_only", client="claude-test")
        session.call("files.write", {"path": str(project), "file": "a.txt", "text": "x" * 4000, "if_match": ""})
        session.call("project.inspect", {"path": str(project)})
        entries = self.audit()
        # Newest first: the refused MCP write, then the CLI change; reads are not logged.
        self.assertEqual([(e["channel"], e["client"], e["operation"], e["ok"], e["code"]) for e in entries[:2]],
                         [("mcp", "claude-test", "files.write", False, "policy_denied"),
                          ("cli", "nightly-script", "remote.configure", False, entries[1]["code"])])
        self.assertFalse(any(e["operation"] in ("catalog.list", "project.inspect") for e in entries))
        self.assertEqual(entries[0]["client_version"], "9.9")
        self.assertEqual(entries[0]["policy"], "read_only")
        self.assertTrue(entries[0]["arguments"]["text"].startswith("[4000 bytes, sha256 "))
        self.assertEqual(entries[1]["arguments"]["password"], "[redacted]")
        log = self.root / "state/audit/audit.jsonl"
        self.assertNotIn("hunter2", log.read_text())
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(log.parent.stat().st_mode), 0o700)
        self.assertEqual([e["client"] for e in self.audit(channel="cli", operation="remote.")], ["nightly-script"])
        self.assertEqual(len(self.audit(failed=True, client="claude-test")), 1)
        # Audit off: nothing more is written.
        dispatch("settings.configure", {"agent_policy": {"audit": False}})
        size = log.stat().st_size
        dispatch("settings.configure", {"jobs": {"max_builds": 2}})
        self.assertEqual(log.stat().st_size, size)

    def test_a_session_runs_a_limited_number_of_jobs_and_large_results_are_saved(self):
        dispatch("settings.configure", {"agent_policy": {"limits": {"jobs_per_session": 1}}})
        session = self.mcp()
        slow = {"wait": [sys.executable, "-c", "import time; time.sleep(60)"]}
        first, failed = session.call("project.execute", {"path": str(self.project("one", slow)), "action": "task", "task": "wait"})
        self.assertFalse(failed, first)
        error, _ = session.call("project.execute", {"path": str(self.project("two", slow)), "action": "task", "task": "wait"})
        self.assertEqual(error["code"], "session_job_limit")
        self.assertEqual(error["details"]["running"], [first["id"]])
        self.assertIn("job.wait", error["remedy"])
        # Another session, or the CLI, has its own count.
        _, failed = self.mcp().call("project.execute", {"path": str(self.project("three")), "action": "task", "task": "check"})
        self.assertFalse(failed)
        jobs.cancel(first["id"])
        jobs.wait(first["id"], 10)
        _, failed = session.call("project.execute", {"path": str(self.project("four")), "action": "task", "task": "check"})
        self.assertFalse(failed)
        # A result above 1 KiB is saved and read back in pieces.
        dispatch("settings.configure", {"agent_policy": {"limits": {"inline_result_kib": 1}}})
        error, _ = session.call("catalog.list", {})
        self.assertEqual(error["code"], "result_too_large")
        self.assertIn("result.read", error["remedy"])
        text, offset = "", 0
        while True:
            piece, failed = session.call("result.read", {"result_id": error["details"]["result_id"], "offset": offset, "limit": 4096})
            self.assertFalse(failed, piece)
            text += piece["text"]
            offset = piece["next_offset"]
            if piece["eof"]:
                break
        self.assertEqual(json.loads(text), dispatch("catalog.list", {}))
        saved = Path(error["details"]["path"])
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
