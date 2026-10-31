#!/usr/bin/env python3
"""Hold adopted zombies like a container without init; never hide them in tests."""
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

SCRIPT = Path(__file__).resolve()


def child(level, mode):
    # The leader exits on TERM; both descendants require the group's KILL.
    if level != "leader":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if level != "leaf":
        next_level = "branch" if level == "leader" else "leaf"
        subprocess.Popen([sys.executable, str(SCRIPT), next_level, mode])
        deadline = time.monotonic() + 10
        while not Path(next_level + ".pid").exists():
            assert time.monotonic() < deadline
            time.sleep(.01)
    pending = Path(level + ".pid.pending")
    pending.write_text(str(os.getpid()))
    pending.replace(level + ".pid")
    if level == "leader" and mode == "exit":
        return
    while True:
        time.sleep(1)


def host():
    sys.path.insert(0, str(SCRIPT.parents[1]))
    from native_client import dispatch, jobs
    # Without the product fix this process adopts the job's killed descendants
    # and deliberately does not wait for them before the assertions below.
    assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
    results = []
    unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], start_new_session=True)
    try:
        with tempfile.TemporaryDirectory(prefix="tomacco-reaper-test-") as temporary:
            root = Path(temporary)
            os.environ["TOMACCO_HOME"] = str(root / "state")
            for mode in ("cancel", "exit"):
                project = root / mode; project.mkdir()
                argv = [sys.executable, str(SCRIPT), "leader", mode]
                (project / "tomacco.json").write_text(json.dumps({"tasks":{"tree":argv}}))
                job = dispatch("project.execute", {"path":str(project),"action":"task","task":"tree"})
                try:
                    deadline = time.monotonic() + 10
                    while not (project / "leader.pid").exists():
                        assert time.monotonic() < deadline, jobs.logs(job["id"])
                        time.sleep(.01)
                    pids = [int((project / (level + ".pid")).read_text()) for level in ("leader", "branch", "leaf")]
                    if mode == "cancel": jobs.cancel(job["id"])
                    final = jobs.wait(job["id"], 10)
                    assert final["state"] == ("cancelled" if mode == "cancel" else "succeeded"), final
                    remnants = {}
                    for pid in pids:
                        try: remnants[pid] = Path(f"/proc/{pid}/stat").read_text()
                        except FileNotFoundError: pass
                    assert not remnants, {"mode":mode,"unreaped_processes":remnants}
                    assert unrelated.poll() is None
                    results.append({"mode":mode,"descendants_reaped":3,"unrelated_process_preserved":True})
                finally:
                    if jobs.get(job["id"])["state"] not in jobs.TERMINAL:
                        jobs.cancel(job["id"]); jobs.wait(job["id"], 10)
    finally:
        unrelated.terminate(); unrelated.wait(timeout=5)
        # Clean up test failures only after observing them, never before assertions.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0: time.sleep(.01)
            except ChildProcessError: break
    print(json.dumps(results))


if __name__ == "__main__":
    if len(sys.argv) == 1: host()
    else: child(sys.argv[1], sys.argv[2])
