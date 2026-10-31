"""Actual compiled status API and durable job context; no iOS execution."""
import json
import sys

from native_client import dispatch, jobs
from test_product import Isolated


class VirtualProcessStatusTests(Isolated):
    def test_status_exposes_only_the_current_accepted_process_observation(self):
        job = self.submit([{"argv": [sys.executable, "-c", "import time; time.sleep(30)"]}])
        directory = self.root / "profile"
        directory.mkdir()
        profile = {"format": 1, "prepare": {}, "boot": {}, "graphics": "/graphics",
                   "engine": "/engine", "application": "dev.app", "installs": str(directory / "installs")}
        (directory / "profile.json").write_text(json.dumps(profile))
        observation = {"pid": 551, "worker_pid": 550, "process_lifetime_bound": False,
                       "snapshot_atomic_with_handle": False, "start_snapshot_known": False}
        state = {"run": 1, "install": "/install", "tools": "/tools", "closed": False,
                 "guest_job": job["id"], "phase": "launch-accepted",
                 "application_launch_accepted": True, "application_scene_verified": False,
                 "launch_receipt": {"state": "accepted", "job_id": job["id"],
                                    "application": "dev.app", "process_observation": observation}}
        path = directory / "state.json"
        path.write_text(json.dumps(state))
        result = dispatch("ios.virtual.configure", {"directory": str(directory)})
        self.assertEqual(result["application_process"], observation)
        self.assertFalse(result["application_scene_verified"])
        for key, value in [("job_id", "older"), ("application", "other.app"), ("state", "unknown")]:
            wrong = {**state, "launch_receipt": {**state["launch_receipt"], key: value}}
            path.write_text(json.dumps(wrong))
            self.assertIsNone(dispatch("ios.virtual.status")["application_process"])
        jobs.cancel(job["id"])
        jobs.wait(job["id"], 5)
        path.write_text(json.dumps({**state, "closed": True, "phase": "stopped"}))
        self.assertIsNone(dispatch("ios.virtual.status")["application_process"])
