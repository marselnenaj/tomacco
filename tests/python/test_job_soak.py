"""A soak of the durable job system: hundreds of short jobs started in parallel, some
cancelled, some failing, some whose worker is killed with SIGKILL. Afterwards no job
is lost or stuck, the database is consistent, no worker or child process is left, and
no worker used more than a few dozen file descriptors or threads.

Jobs are recorded the way `jobs::submit` records them (tests/python/robust_support.py)
and run by the real `tomaccoctl _worker`; no device or toolchain is needed.

Run: TOMACCO_SOAK=1 TOMACCOCTL=target/debug/tomaccoctl PYTHONPATH=tools:tests/python:tests:. \
     python3 -m unittest tests.python.test_job_soak
TOMACCO_SOAK_JOBS sets the count (default 300 with TOMACCO_SOAK=1; a short run of 40
jobs is part of the ordinary suite), TOMACCO_FUZZ_SEED the seed."""
import contextlib
import os
import random
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import robust_support as rb

SOAK = bool(os.environ.get("TOMACCO_SOAK"))
JOBS = int(os.environ.get("TOMACCO_SOAK_JOBS", "300" if SOAK else "40"))
SEED = int(os.environ.get("TOMACCO_FUZZ_SEED", "20260501"))
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}
MARK = "tomacco-soak-marker"


def worker_processes(jobs):
    """(pid, descriptors, threads) of every running `_worker` of this test's jobs."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().split(b"\0")
            if len(command) > 2 and command[1] == b"_worker" and command[2].decode() in jobs:
                found.append((int(entry.name), len(list((entry / "fd").iterdir())),
                              len(list((entry / "task").iterdir())), command[2].decode()))
        except OSError:
            pass
    return found


class JobSoak(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="soak-")
        self.home = Path(self.directory.name)
        self.env = rb.environment(self.home)
        rb.initialise(self.env, self.home)

    def tearDown(self):
        for row in rb.rows(self.home):
            if row["state"] not in TERMINAL:
                rb.call(self.env, "job.cancel", {"job_id": row["id"]})
        self.directory.cleanup()

    def test_hundreds_of_jobs_with_cancellations_and_killed_workers_all_end(self):
        rng = random.Random(SEED)
        kinds = {}
        plan = []
        for index in range(JOBS):
            kind = rng.choices(["ok", "fail", "cancel", "kill", "timeout"], [50, 15, 15, 15, 5])[0]
            argv = {"ok": ["sh", "-c", f"sleep 0.{rng.randint(1, 9)}; echo {MARK}"],
                    "fail": ["sh", "-c", "echo bad >&2; exit 3"],
                    "cancel": ["sleep", "120"], "kill": ["sleep", "120"], "timeout": ["sleep", "120"]}[kind]
            plan.append((kind, argv))
        peak_fds = peak_threads = 0
        started = time.time()
        workers = []
        for kind, argv in plan:
            # A job that times out has a one second limit.
            job = rb.queue_job(self.env, self.home, argv, self.home, timeout=1 if kind == "timeout" else 600, start=False)
            kinds[job] = kind
        # Start them in windows so a laptop is not asked for 300 processes at once.
        pending = list(kinds)
        running = []
        killed, cancelled = set(), set()
        deadline = time.time() + 300
        while time.time() < deadline:
            while pending and len([p for p in running if p.poll() is None]) < 40:
                job = pending.pop(0)
                running.append(rb.start_worker(self.env, job))
                workers.append(job)
            rows = {r["id"]: r for r in rb.rows(self.home)}
            for job, kind in kinds.items():
                row = rows[job]
                if kind == "cancel" and job not in cancelled and row["state"] in ("queued", "running") and job in workers:
                    if rng.random() < 0.5 or row["state"] == "running":
                        rb.call(self.env, "job.cancel", {"job_id": job})
                        cancelled.add(job)
                if kind == "kill" and job not in killed and row["state"] == "running" and row["pid"]:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(row["pid"], 9)
                    killed.add(job)
            for _, fds, threads, _job in worker_processes(kinds):
                peak_fds, peak_threads = max(peak_fds, fds), max(peak_threads, threads)
            states = [r["state"] for r in rows.values()]
            if not pending and all(state in TERMINAL for state in states):
                break
            # A reader notices a dead worker and fails its job; ask for the ones that look stuck.
            for job, row in rows.items():
                if row["state"] in ("queued", "running") and job in workers and kinds[job] == "kill" and job in killed:
                    rb.call(self.env, "job.get", {"job_id": job})
            time.sleep(0.2)
        for process in running:
            process.poll()
        rows = {r["id"]: r for r in rb.rows(self.home)}
        self.assertEqual(len(rows), JOBS, "jobs were lost")
        stuck = {j: r["state"] for j, r in rows.items() if r["state"] not in TERMINAL}
        self.assertEqual(stuck, {}, f"stuck jobs (seed {SEED})")
        for job, kind in kinds.items():
            state = rows[job]["state"]
            expected = {"ok": {"succeeded"}, "fail": {"failed"}, "cancel": {"cancelled"},
                        "kill": {"failed", "succeeded"}, "timeout": {"timed_out"}}[kind]
            self.assertIn(state, expected, f"{kind} job {job} ended {state}: {rows[job]['result']}")
            if kind == "kill" and state == "failed":
                self.assertIn("Worker exited unexpectedly", rows[job]["result"])
        # The database is consistent and keeps no input of a finished job.
        with contextlib.closing(sqlite3.connect(self.home / "jobs.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT count(*) FROM inputs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE state='succeeded' AND exit_code!=0").fetchone()[0], 0)
        # Nothing stays running: no worker, and no `sleep 120` a killed or cancelled job started.
        deadline = time.time() + 30
        while time.time() < deadline and (worker_processes(kinds) or self.leftover_sleeps()):
            time.sleep(0.3)
        self.assertEqual(worker_processes(kinds), [])
        self.assertEqual(self.leftover_sleeps(), [], "a child of a killed or cancelled job was left running")
        # Bounded resources in the workers.
        self.assertLess(peak_fds, 64, "descriptors per worker")
        self.assertLess(peak_threads, 32, "threads per worker")
        print(f"soak: {JOBS} jobs in {time.time() - started:.0f}s, cancelled {len(cancelled)}, killed {len(killed)}, "
              f"peak worker fds {peak_fds}, threads {peak_threads}")

    def leftover_sleeps(self):
        found = []
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                try:
                    if (entry / "cmdline").read_bytes().split(b"\0")[:2] == [b"sleep", b"120"] and \
                            (entry / "environ").read_bytes().find(b"TOMACCO_PARENT_JOB=") >= 0 and \
                            f"TOMACCO_HOME={self.home}".encode() in (entry / "environ").read_bytes():
                        found.append(entry.name)
                except OSError:
                    pass
        return found


if __name__ == "__main__":
    unittest.main()
