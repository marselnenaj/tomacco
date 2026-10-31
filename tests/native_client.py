"""Test-only client for the public native CLI. Never installed with the app."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

# The tests' own bounds for stand-in processes stretch on a loaded host.
from host_patience import PATIENCE

ROOT = Path(__file__).resolve().parent.parent


class StudioError(Exception):
    def __init__(self, value):
        self.code = value.get("code")
        self.details = value.get("details")
        super().__init__(value.get("message", str(value)))


def binary():
    return os.environ.get("TOMACCOCTL", str(ROOT / "tomaccoctl"))


def dispatch(operation, arguments=None):
    value = subprocess.run([binary(), "call", operation, json.dumps(arguments or {}, allow_nan=False)],
                           capture_output=True, text=True, timeout=100 * PATIENCE)
    try:
        result = json.loads(value.stdout)
    except ValueError:
        raise AssertionError((value.returncode, value.stdout, value.stderr))
    if not result["ok"]:
        raise StudioError(result["error"])
    assert value.returncode == 0, value.stderr
    return result["result"]


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(file.name, path)


class Jobs:
    TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}
    def get(self, job_id): return dispatch("job.get", {"job_id": job_id})
    # job.wait's own bound on one call (assets/operations.json); a wait stretched
    # beyond it on a loaded host repeats the call instead of asking for more.
    LONGEST = 60

    def wait(self, job_id, seconds=30):
        remaining = seconds * PATIENCE
        while True:
            portion = min(self.LONGEST, remaining)
            job = dispatch("job.wait", {"job_id": job_id, "seconds": portion})
            remaining -= portion
            if remaining <= 0 or job.get("state") in self.TERMINAL:
                return job
    def logs(self, job_id, **kwargs): return dispatch("job.logs", {"job_id": job_id, **kwargs})
    def cancel(self, job_id): return dispatch("job.cancel", {"job_id": job_id})
    def listing(self, limit=30): return dispatch("job.list", {"limit": limit})
    def send(self, job_id, command="reload"): return dispatch("job.send", {"job_id": job_id, "command": command})


class Catalogue:
    def profiles(self): return dispatch("catalog.list")
    def builtin(self): return [{k: v for k, v in p.items() if k != "custom"} for p in self.profiles() if not p["custom"]]
    def find(self, profile_id): return next(p for p in self.profiles() if p["id"] == profile_id)
    def import_profiles(self, path, replace=False): return dispatch("catalog.import", {"path": path, "replace": replace})
    def remove(self, profile_id): return dispatch("catalog.remove", {"profile_id": profile_id})
    def export_profile(self, profile_id, path): return dispatch("catalog.export", {"profile_id": profile_id, "path": path})


class Preview:
    def call(self, method, parameters=None, session="main", **kwargs):
        return dispatch("preview." + method, {**(parameters or {}), "session": session})
    def invoke(self, method, session="main", **kwargs): return self.call(method, kwargs, session)
    def ready(self, session="main", **kwargs): return dispatch("preview.ready", {"session": session, **kwargs})


jobs, catalog, preview = Jobs(), Catalogue(), Preview()
