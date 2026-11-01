"""Helpers of the robustness harnesses: a scratch TOMACCO_HOME, jobs queued the way
`jobs::submit` records them (so a test can run thousands of short commands without
an operation per command), and an MCP session that tolerates a broken peer."""
import contextlib
import json
import os
import queue
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from native_client import binary


def scratch(prefix="rb-"):
    """A short private directory: TMPDIR if set (keeps socket paths short), else the system's."""
    return tempfile.TemporaryDirectory(prefix=prefix)


def environment(home, **extra):
    env = dict(os.environ)
    env.update(TOMACCO_HOME=str(home), HOME=str(home))
    env.update(extra)
    return env


def call(env, operation, arguments=None, timeout=120):
    """The envelope `tomaccoctl call` prints."""
    run = subprocess.run([binary(), "call", operation, json.dumps(arguments or {})], env=env,
                         capture_output=True, text=True, timeout=timeout)
    return json.loads(run.stdout)


def initialise(env, home):
    """Creates the job database (the first call of a home does)."""
    call(env, "job.list", {"limit": 1})
    return Path(home) / "jobs.sqlite3"


def queue_job(env, home, argv, cwd, timeout=600, start=True):
    """Records a queued job for `argv` as `submit` does and starts its detached worker.
    Returns the job ID."""
    job = uuid.uuid4().hex
    spec = {"steps": [{"argv": argv}], "timeout": timeout, "artifacts": [], "env": {},
            "exclusive": "", "cwd": str(cwd)}
    now = time.time()
    with contextlib.closing(sqlite3.connect(Path(home) / "jobs.sqlite3", timeout=30)) as db, db:
        db.execute("INSERT INTO jobs(id,label,state,created,updated,spec) VALUES(?,?,'queued',?,?,?)",
                   (job, "soak", now, now, json.dumps(spec)))
    if start:
        start_worker(env, job)
    return job


def start_worker(env, job):
    return subprocess.Popen([binary(), "_worker", job], env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def rows(home):
    """Every job row as a dict."""
    with contextlib.closing(sqlite3.connect(Path(home) / "jobs.sqlite3", timeout=30)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM jobs")]


class Session:
    """One `tomaccoctl mcp` process spoken to line by line. Replies are collected on a
    queue by a reader thread, so a server that goes quiet fails a test by timeout
    instead of hanging it."""

    def __init__(self, env, *arguments):
        self.process = subprocess.Popen([binary(), "mcp", *arguments], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.replies = queue.Queue()
        self.closed = threading.Event()
        self.stderr = b""
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain, daemon=True).start()

    def _read(self):
        for line in self.process.stdout:
            try:
                self.replies.put(json.loads(line))
            except ValueError:
                self.replies.put({"unparsable": line})
        self.closed.set()

    def _drain(self):
        self.stderr = self.process.stderr.read()

    def send_raw(self, data):
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def send(self, message):
        self.send_raw(json.dumps(message).encode() + b"\n")

    def reply(self, timeout=30):
        return self.replies.get(timeout=timeout)

    def silent(self, seconds=0.5):
        """True if no reply arrives within the time."""
        try:
            self.replies.get(timeout=seconds)
            return False
        except queue.Empty:
            return True

    def request(self, ident, method, params=None, timeout=30):
        self.send({"jsonrpc": "2.0", "id": ident, "method": method, **({} if params is None else {"params": params})})
        while True:
            reply = self.reply(timeout)
            if reply.get("id") == ident:
                return reply

    def initialize(self):
        reply = self.request(0, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                               "clientInfo": {"name": "robustness", "version": "1"}})
        assert reply["result"]["serverInfo"]["name"] == "tomacco", reply
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return reply

    def finish(self, timeout=30):
        """Closes stdin and returns the exit status; fails if the server does not exit."""
        try:
            self.process.stdin.close()
        except OSError:
            pass
        return self.process.wait(timeout)

    def kill(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                pipe.close()
            except OSError:
                pass

    def rss_kib(self):
        for line in Path(f"/proc/{self.process.pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
        return 0
