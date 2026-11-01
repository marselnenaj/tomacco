"""Black-box contract tests of the compiled Rust CLI and actual worker processes."""
import hashlib
import fcntl
import json
import os
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_client import ROOT, StudioError, binary, catalog, dispatch, jobs, preview
from host_patience import PATIENCE


# The built-in catalogue grows with new devices; tests count from the file.
BUILTIN_PROFILES = len(json.loads((ROOT / "assets/models.json").read_text()))


class Isolated(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tomacco-native-test-")
        self.root = Path(self.temp.name)
        # Jobs under test refuse at once under host pressure, as a point-in-time
        # check; waiting for a quiet host would outlast their test deadlines.
        self.env = patch.dict(os.environ, {"TOMACCO_HOME": str(self.root / "state"), "TOMACCO_TEST_ROOT": str(self.root),
                                           "TOMACCO_IOS_PRESSURE_WAIT_SECONDS": "0",
                                           "TOMACCO_IOS_PRESSURE_DIRECTORY": str(self.root / "pressure")})
        # A quiet host: these jobs run synthetic engines, and a real guest's
        # I/O on the same machine must not refuse them.
        (self.root / "pressure").mkdir()
        for resource in ("io", "memory"):
            (self.root / "pressure" / resource).write_text(
                "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
                "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        self.env.start()
        self.workers = []

    def tearDown(self):
        for job in jobs.listing(200):
            if job["state"] not in jobs.TERMINAL:
                jobs.cancel(job["id"])
                jobs.wait(job["id"], 5)
        for worker in self.workers:
            worker.wait(5 * PATIENCE)
            worker.stderr.close()
        self.env.stop()
        self.temp.cleanup()

    def submit(self, steps, timeout=10, artifacts=None, exclusive=""):
        # The durable schema predates this migration. Seed it directly to test
        # old-job compatibility and _worker's process, logging and output contract.
        jobs.listing()
        job_id = uuid.uuid4().hex
        spec = dict(cwd=str(self.root), steps=steps, timeout=timeout, artifacts=artifacts or [], env={}, exclusive=exclusive)
        with closing(sqlite3.connect(self.root / "state/jobs.sqlite3")) as db, db:
            db.execute("INSERT INTO jobs(id,label,state,created,updated,spec) VALUES(?,?,'queued',?,?,?)",
                       (job_id, "fixture", time.time(), time.time(), json.dumps(spec)))
        self.workers.append(subprocess.Popen([binary(), "_worker", job_id], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
        return jobs.get(job_id)

    def wait_log(self, job, value):
        deadline = time.monotonic() + 5 * PATIENCE
        while value not in jobs.logs(job["id"])["text"]:
            self.assertLess(time.monotonic(), deadline, jobs.get(job["id"]))
            time.sleep(.03)

    def tool(self, name, source=None):
        path = self.root / name
        path.write_text("#!" + sys.executable + "\n" + (source or "import json,sys;print(json.dumps(sys.argv[1:]))\n"))
        path.chmod(0o755)
        dispatch("settings.configure", {"executables": {name: str(path)}})
        return str(path)


class NativeDebuggerValidationTests(Isolated):
    def request(self, program, **extra):
        return dispatch("debug.start", {"backend": "android-lldb", "device": "validation-only", "bundle_id": "dev.example.app", "program": str(program), **extra})

    def elf(self, name="library.so", elf_class=2, machine=62):
        file = self.root / name
        header = bytearray(64)
        header[:7] = b"\x7fELF" + bytes([elf_class, 1, 1])
        header[18:20] = machine.to_bytes(2, "little")
        file.write_bytes(header)
        return file

    def test_rejects_non_elf_and_unsupported_architecture_before_contacting_device(self):
        file = self.root / "bad.so"
        for data in (b"short", b"not an ELF library" * 4):
            file.write_bytes(data)
            with self.assertRaises(StudioError) as result: self.request(file)
            self.assertEqual(result.exception.code, "invalid_native_binary")
        for cls, machine in ((2, 3), (1, 62), (2, 999)):
            with self.assertRaises(StudioError) as result: self.request(self.elf(elf_class=cls, machine=machine))
            self.assertEqual(result.exception.code, "unsupported_native_arch")
        self.assertEqual(dispatch("debug.list", {})["sessions"], [])

    def test_rejects_command_paths_and_inapplicable_launch_options(self):
        with self.assertRaises(StudioError) as result: self.request(self.elf("bad\ncommand.so"))
        self.assertEqual(result.exception.code, "invalid_argument")
        for extra in ({"args": ["--flag"]}, {"port": 5039}):
            with self.assertRaises(StudioError) as result: self.request(self.elf(), **extra)
            self.assertEqual(result.exception.code, "invalid_argument")
        self.assertEqual(dispatch("debug.list", {})["sessions"], [])

    def test_foreign_pid_namespace_is_not_treated_as_a_dead_debugger(self):
        session = uuid.uuid4().hex
        directory = self.root / "state/debug" / session
        directory.mkdir(parents=True)
        (directory / "state.json").write_text(json.dumps({"session": session, "pid": os.getpid(), "process_token": "invisible", "process_namespace": "pid:[foreign]", "terminated": False}))
        state = dispatch("debug.list", {})["sessions"][0]
        self.assertFalse(state["terminated"])
        self.assertEqual(state["worker_visibility"], "different_process_namespace")


class RuntimeAudioTests(Isolated):
    """Exercise ownership and concurrency with actual durable worker processes."""
    def setUp(self):
        super().setUp()
        self.owner = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
        self.session = uuid.uuid4().hex
        self.directory = self.root / "state/native-sessions" / self.session
        self.directory.mkdir(parents=True)
        token = Path(f"/proc/{self.owner.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        self.runtime = dict(session=self.session, platform="android", device="audio-test-device",
                            status="connected", pid=self.owner.pid, pid_token=token,
                            control_socket=str(self.root / "runtime.sock"))
        (self.directory / "state.json").write_text(json.dumps(self.runtime))
        (self.directory / "spec.json").write_text(json.dumps(dict(platform="android", device="audio-test-device")))
        self.tool("adb")
        self.tool("scrcpy", "import json,os,sys,time\nfrom pathlib import Path\nroot=Path(os.environ['TOMACCO_TEST_ROOT'])\n(root/'audio-start.json').write_text(json.dumps(dict(args=sys.argv[1:],adb=os.environ['ADB'],server=os.environ['SCRCPY_SERVER_PATH'])))\nprint('audio client ready',flush=True)\ntime.sleep(60)\n")
        (self.root / "scrcpy-server").touch()
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(self.runtime["control_socket"])
        self.listener.listen(16)
        self.listener.settimeout(.1)
        self.closed = threading.Event()
        self.operations = []
        def serve():
            while not self.closed.is_set():
                try: client, _ = self.listener.accept()
                except socket.timeout: continue
                except OSError: break
                with client, client.makefile("rb") as reader:
                    request = json.loads(reader.readline())
                    self.operations.append(request["operation"])
                    if request["operation"] == "stop": self.runtime["status"] = "disconnected"
                    client.sendall(json.dumps(dict(ok=True, result=self.runtime)).encode() + b"\n")
        self.server = threading.Thread(target=serve)
        self.server.start()

    def tearDown(self):
        self.closed.set()
        self.listener.close()
        self.server.join(2)
        self.owner.terminate()
        self.owner.wait(5 * PATIENCE)
        super().tearDown()

    def audio(self, action="status"):
        return dispatch("runtime.audio", dict(session=self.session, action=action))

    def test_concurrent_start_reuses_output_only_job_and_rapid_toggle_is_serialized(self):
        self.assertFalse(self.audio()["configured"])
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.audio("start"), range(6)))
        job_id = results[0]["job_id"]
        self.assertEqual({r["job_id"] for r in results}, {job_id})
        self.wait_log(dict(id=job_id), "audio client ready")
        info = json.loads((self.root / "audio-start.json").read_text())
        self.assertEqual(info["args"], ["--serial", "audio-test-device", "--no-video", "--no-control", "--no-window", "--audio-source=output", "--audio-codec=raw", "--audio-buffer=60", "--require-audio"])
        self.assertEqual(info["adb"], str(self.root / "adb"))
        self.assertEqual(info["server"], str(self.root / "scrcpy-server"))
        self.assertEqual(jobs.get(job_id)["phase"], "streaming")
        self.assertEqual(dispatch("runtime.state", dict(session=self.session))["audio"]["job_id"], job_id)
        self.assertFalse(self.audio("stop")["enabled"])
        restarted = self.audio("start")
        self.assertNotEqual(restarted["job_id"], job_id)
        self.assertEqual(jobs.get(job_id)["state"], "cancelled")
        self.assertEqual(len([j for j in jobs.listing() if j["state"] not in jobs.TERMINAL]), 1)
        dispatch("runtime.stop", dict(session=self.session))
        self.assertEqual(jobs.wait(restarted["job_id"], 5)["state"], "cancelled")
        self.assertIn("stop", self.operations)

    def test_runtime_crash_cancels_audio_without_a_polling_client(self):
        job_id = self.audio("start")["job_id"]
        self.wait_log(dict(id=job_id), "audio client ready")
        self.owner.kill(); self.owner.wait(5 * PATIENCE)
        self.assertEqual(jobs.wait(job_id, 5)["state"], "cancelled")
        self.assertFalse(self.audio()["enabled"])

    def test_audio_failure_keeps_video_connected_and_records_actionable_error(self):
        self.tool("scrcpy", "import sys\nprint('Audio capture failed',flush=True)\nsys.exit(1)\n")
        job_id = self.audio("start")["job_id"]
        self.assertEqual(jobs.wait(job_id, 5)["state"], "failed")
        state = dispatch("runtime.state", dict(session=self.session))
        self.assertEqual(state["status"], "connected")
        self.assertEqual(state["audio"]["status"], "failed")
        self.assertFalse(state["audio"]["enabled"])
        self.assertIn("error", state["audio"])
        self.assertIn("Audio capture failed", Path(state["audio"]["log_path"]).read_text())

    def test_stopped_session_cannot_start_audio_and_ios_is_not_advertised(self):
        self.runtime["status"] = "disconnected"
        with self.assertRaises(StudioError) as error: self.audio("start")
        self.assertEqual(error.exception.code, "runtime_unavailable")
        self.assertEqual(jobs.listing(), [])
        self.runtime["platform"] = "ios"
        (self.directory / "state.json").write_text(json.dumps(self.runtime))
        with self.assertRaises(StudioError) as error: self.audio("start")
        self.assertEqual(error.exception.code, "audio_unsupported")

    def test_explicit_off_is_preserved_and_missing_audio_job_does_not_break_video_stop(self):
        self.assertTrue(self.audio("stop")["configured"])
        (self.directory / "audio.json").write_text(json.dumps(dict(enabled=True, job_id=uuid.uuid4().hex)))
        state = dispatch("runtime.state", dict(session=self.session))
        self.assertEqual(state["status"], "connected")
        self.assertEqual(state["audio"]["status"], "failed")
        dispatch("runtime.stop", dict(session=self.session))
        self.assertIn("stop", self.operations)


class CatalogueTests(Isolated):
    def test_writes_refuse_a_link_planted_at_the_destination(self):
        precious = self.root / "precious"
        precious.write_text("keep")
        link = self.root / "shot.png"
        link.symlink_to(precious)
        profile = catalog.builtin()[0]["id"]
        for operation, arguments in (("catalog.export", {"profile_id": profile, "path": str(link)}),
                                     ("ios.virtual.screenshot", {"path": str(link)}),
                                     ("ios.virtual.record", {"path": str(link.with_suffix(".mp4"))})):
            if operation == "ios.virtual.record":
                link.with_suffix(".mp4").symlink_to(precious)
            with self.assertRaises(StudioError) as caught:
                dispatch(operation, arguments)
            self.assertEqual(caught.exception.code, "invalid_path", operation)
            self.assertIn("symbolic link", str(caught.exception))
        self.assertEqual(precious.read_text(), "keep")
        # A folder that is a link is fine: the profile lands in the real folder.
        real = self.root / "real"
        real.mkdir()
        (self.root / "exports").symlink_to(real)
        catalog.export_profile(profile, str(self.root / "exports/profile.json"))
        self.assertTrue((real / "profile.json").is_file())

    def test_builtins_and_full_round_trip(self):
        profiles = catalog.builtin()
        self.assertEqual(len(profiles), BUILTIN_PROFILES)
        self.assertEqual(len({p["id"] for p in profiles}), BUILTIN_PROFILES)
        for p in profiles:
            self.assertTrue(p["source"].startswith("https://"))
            value = {**p, "id": "custom-" + p["id"]}
            source = self.root / "profile.json"
            source.write_text(json.dumps(value))
            self.assertEqual(catalog.import_profiles(str(source))["imported"], [value["id"]])
            with self.assertRaises(StudioError): catalog.import_profiles(str(source))
            value["name"] = "Updated name"
            source.write_text(json.dumps(value))
            catalog.import_profiles(str(source), True)
            dest = self.root / (value["id"] + ".json")
            catalog.export_profile(value["id"], str(dest))
            self.assertEqual(json.loads(dest.read_text()), value)
            catalog.remove(value["id"])
        self.assertEqual(len(catalog.profiles()), BUILTIN_PROFILES)
        self.assertTrue(all(p["platform"] == "ios" for p in dispatch("catalog.list", {"platform": "ios"})))

    def test_import_atomicity_builtin_protection_and_bad_geometry(self):
        base = {**catalog.builtin()[0], "id": "valid-custom"}
        for invalid in ({**base, "id": "../escape"}, {**base, "colors": ["red"]},
                        {**base, "source": "javascript:alert(1)"}, {**base, "dpr": float("nan")},
                        {**base, "width": True}, {**base, "execute": "whoami"},
                        {**base, "safePortrait": dict(top=299, bottom=299, right=299, left=299)},
                        {**base, "openWidth": 600}, {**base, "colorNames": []}):
            source = self.root / "bad.json"
            source.write_text(json.dumps([base, invalid]))
            with self.assertRaises(StudioError): catalog.import_profiles(str(source))
            self.assertEqual(len(catalog.profiles()), BUILTIN_PROFILES)
        source.write_text(json.dumps(catalog.builtin()[0]))
        with self.assertRaises(StudioError): catalog.import_profiles(str(source), True)
        with self.assertRaises(StudioError): catalog.remove(catalog.builtin()[0]["id"])

    def test_concurrent_imports_preserve_every_profile(self):
        from concurrent.futures import ThreadPoolExecutor
        base = catalog.builtin()[0]
        paths = []
        for i in range(12):
            p = self.root / f"p{i}.json"
            p.write_text(json.dumps({**base, "id": f"parallel-{i}"}))
            paths.append(p)
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda p: catalog.import_profiles(str(p)), paths))
        self.assertEqual(len(catalog.profiles()), BUILTIN_PROFILES + 12)


class JobTests(Isolated):
    def test_parallel_first_database_open_preserves_complete_schema(self):
        for iteration in range(4):
            state = self.root / f'parallel-state-{iteration}'
            barrier = threading.Barrier(16)
            def first_open(_):
                barrier.wait(timeout=10)
                return jobs.listing()
            with patch.dict(os.environ, {'TOMACCO_HOME': str(state)}):
                with ThreadPoolExecutor(max_workers=16) as pool:
                    self.assertEqual(list(pool.map(first_open, range(16))), [[]] * 16)
            with closing(sqlite3.connect(state / 'jobs.sqlite3')) as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
                for table in ('jobs', 'inputs'):
                    self.assertEqual(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)

    def test_public_task_executes_and_self_spawns_native_worker(self):
        (self.root / "tomacco.json").write_text(json.dumps({"tasks": {"check": [sys.executable, "-c", "print('compiled')"]}}))
        job = dispatch("project.execute", {"path": str(self.root), "action": "task", "task": "check"})
        self.assertEqual(jobs.wait(job["id"], 5)["state"], "succeeded")
        self.assertIn("compiled", jobs.logs(job["id"])["text"])

    def test_workspace_serializes_and_waiting_job_cancels(self):
        command = [sys.executable, "-c", "import time;from pathlib import Path;p=Path('active');assert not p.exists();p.touch();print('entered',flush=True);time.sleep(.8);p.unlink()"]
        first = self.submit([{"argv": command}], exclusive=str(self.root))
        self.wait_log(first, "entered")
        second = self.submit([{"argv": command}], exclusive=str(self.root))
        third = self.submit([{"argv": command}], exclusive=str(self.root))
        jobs.cancel(third["id"])
        self.assertEqual(jobs.wait(third["id"], 5)["state"], "cancelled")
        self.assertEqual(jobs.wait(first["id"], 5)["state"], "succeeded")
        self.assertEqual(jobs.wait(second["id"], 5)["state"], "succeeded")
        self.assertIn("Waiting for workspace", jobs.logs(second["id"])["text"])

    def test_process_logs_hashes_and_nonzero_exit(self):
        command = [sys.executable, "-c", "from pathlib import Path;print('compiled');Path('output.txt').write_text('result')"]
        job = self.submit([{"argv": command}], artifacts=["*.txt"])
        done = jobs.wait(job["id"], 5)
        self.assertEqual(done["state"], "succeeded", done)
        self.assertIn("compiled", jobs.logs(job["id"])["text"])
        self.assertEqual(done["result"]["artifacts"][0]["sha256"], hashlib.sha256(b"result").hexdigest())
        job = self.submit([{"argv": [sys.executable, "-c", "raise SystemExit(7)"]}])
        done = jobs.wait(job["id"], 5)
        self.assertEqual((done["state"], done["exit_code"]), ("failed", 7))

    def test_the_data_directory_and_job_logs_are_owner_only(self):
        job = self.submit([{"argv": [sys.executable, "-c", "print('tool output')"]}])
        done = jobs.wait(job["id"], 5)
        self.assertEqual(done["state"], "succeeded", done)
        state = self.root / "state"
        self.assertEqual(state.stat().st_mode & 0o777, 0o700)
        self.assertEqual((state / "jobs").stat().st_mode & 0o777, 0o700)
        self.assertEqual(Path(done["log_path"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual((state / "jobs.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_cancel_kills_descendants(self):
        marker = self.root / "should-not-exist"
        child = "import time;from pathlib import Path;time.sleep(2);Path(" + repr(str(marker)) + ").touch()"
        code = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c'," + repr(child) + "]);print('ready',flush=True);time.sleep(60)"
        job = self.submit([{"argv": [sys.executable, "-c", code]}])
        self.wait_log(job, "ready")
        jobs.cancel(job["id"])
        self.assertEqual(jobs.wait(job["id"], 5)["state"], "cancelled")
        time.sleep(2.1)
        self.assertFalse(marker.exists())

    def test_process_groups_are_reaped_without_container_init(self):
        value = subprocess.run([sys.executable, str(ROOT / "tests/fixtures/job_reaper_host.py")],
                               capture_output=True, text=True, timeout=40)
        self.assertEqual(value.returncode, 0, value.stdout + value.stderr)
        result = json.loads(value.stdout)
        self.assertEqual([row["mode"] for row in result], ["cancel", "exit"])
        self.assertTrue(all(row["descendants_reaped"] == 3 and row["unrelated_process_preserved"] for row in result))

    def test_timeout_finite_capture_and_log_limit(self):
        step = {"argv": [sys.executable, "-c", "import time;print('live',flush=True);time.sleep(60)"]}
        job = self.submit([step], timeout=1)
        self.assertEqual(jobs.wait(job["id"], 5)["state"], "timed_out")
        job = self.submit([{**step, "duration": .2}], timeout=5)
        self.assertEqual(jobs.wait(job["id"], 5)["state"], "succeeded")
        job = self.submit([{"argv": [sys.executable, "-c", "import sys,time;sys.stdout.write('x'*33554433);sys.stdout.flush();time.sleep(60)"]}])
        done = jobs.wait(job["id"], 5)
        self.assertEqual(done["state"], "failed")
        self.assertIn("32 MiB", done["result"]["error"])

    def test_binary_output_is_atomic_and_failure_keeps_previous(self):
        output = self.root / "image.png"
        command = [sys.executable, "-c", "import sys;sys.stdout.buffer.write(bytes([137,80,78,71]))"]
        job = self.submit([{"argv": command, "output": str(output)}])
        self.assertEqual(jobs.wait(job["id"], 5)["state"], "succeeded")
        self.assertEqual(output.read_bytes(), b"\x89PNG")
        job = self.submit([{"argv": command[:-1] + [command[-1] + ";sys.exit(1)"], "output": str(output)}])
        self.assertEqual(jobs.wait(job["id"], 5)["state"], "failed")
        self.assertEqual(output.read_bytes(), b"\x89PNG")
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_interactive_input_and_incremental_logs(self):
        job = self.submit([{"argv": [sys.executable, "-c", "print('ready',flush=True);print(input(),flush=True)"], "interactive": True}])
        self.wait_log(job, "ready")
        jobs.send(job["id"])
        self.assertEqual(jobs.wait(job["id"], 5)["state"], "succeeded")
        first = jobs.logs(job["id"], limit=10)
        second = jobs.logs(job["id"], offset=first["next_offset"])
        self.assertEqual(first["text"] + second["text"], jobs.logs(job["id"])["text"])
        self.assertIn("\nr\n", second["text"])

    def test_worker_recovery_checks_pid_start_token(self):
        jobs.listing()
        job_id = uuid.uuid4().hex
        with closing(sqlite3.connect(self.root / "state/jobs.sqlite3")) as db, db:
            db.execute("INSERT INTO jobs(id,label,state,created,updated,spec,pid,token) VALUES(?,?,'running',?,?,?,?,?)",
                       (job_id, "stale", time.time(), time.time(), '{}', os.getpid(), "wrong-start-token"))
        self.assertEqual(jobs.get(job_id)["state"], "failed")
        self.assertEqual(jobs.listing()[0]["result"]["error"], "Worker exited unexpectedly")

    def test_live_worker_lease_survives_inaccessible_pid_and_cancels_cooperatively(self):
        job = self.submit([{"argv": [sys.executable, "-c", "import time;print('ready',flush=True);time.sleep(60)"]}])
        self.wait_log(job, "ready")
        lease = self.root / "state/jobs" / (job["id"] + ".worker.lock")
        self.assertTrue(lease.is_file())
        # A shared database can be read from a PID namespace that cannot see
        # the host worker. A missing /proc entry must not cancel a live job.
        with closing(sqlite3.connect(self.root / "state/jobs.sqlite3")) as db, db:
            db.execute("UPDATE jobs SET pid=?,token=? WHERE id=?", (2147483647, "foreign-namespace", job["id"]))
        self.assertEqual(jobs.get(job["id"])["state"], "running")
        self.assertEqual(jobs.listing()[0]["state"], "running")
        self.assertIn("ready", jobs.logs(job["id"])["text"])
        jobs.cancel(job["id"])
        self.assertEqual(jobs.wait(job["id"], 5)["state"], "cancelled")
        with lease.open("rb") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_released_worker_lease_recovers_even_when_a_pid_has_been_reused(self):
        jobs.listing()
        job_id = uuid.uuid4().hex
        own_token = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19]
        with closing(sqlite3.connect(self.root / "state/jobs.sqlite3")) as db, db:
            db.execute("INSERT INTO jobs(id,label,state,created,updated,spec,pid,token) VALUES(?,?,'running',?,?,?,?,?)",
                       (job_id, "released lease", time.time(), time.time(), '{}', os.getpid(), own_token))
        directory = self.root / "state/jobs"
        directory.mkdir(exist_ok=True)
        (directory / (job_id + ".worker.lock")).touch()
        self.assertEqual(jobs.get(job_id)["state"], "failed")
        self.assertEqual(jobs.get(job_id)["result"]["error"], "Worker exited unexpectedly")


class AdapterTests(Isolated):
    def test_project_inspection_is_read_only_and_plans_are_argv(self):
        root = self.root / "project with spaces"
        root.mkdir()
        (root / "settings.gradle").touch()
        marker = self.root / "executed"
        (root / "gradlew").write_text("#!/bin/sh\ntouch " + shlex.quote(str(marker)))
        self.assertIn("gradle", dispatch("project.inspect", {"path": str(root)})["kinds"])
        for action, target in (("build", "assembleDebug"), ("test", "testDebugUnitTest"), ("lint", "lintDebug"), ("instrumented-test", "connectedDebugAndroidTest")):
            plan = dispatch("project.plan", {"path": str(root), "action": action})
            self.assertEqual(plan["steps"][0]["argv"], ["/bin/sh", str(root / "gradlew"), "--console=plain", target])
        self.assertFalse(marker.exists())

    def test_xtool_config_preserves_comments_and_other_keys(self):
        (self.root / "xtool.yml").write_text("version: 1\nbundleID: com.example.Old\n# keep this\n")
        dispatch("project.configure", {"path": str(self.root), "application_id": "dev.test.New", "tasks": {"check": ["swift", "test"]}})
        self.assertEqual((self.root / "xtool.yml").read_text(), "version: 1\nbundleID: dev.test.New\n# keep this\n")
        with self.assertRaises(StudioError): dispatch("project.configure", {"path": str(self.root), "tasks": {"bad": "echo bad"}})

    def test_adb_quotes_shell_and_resolves_actual_launcher(self):
        self.tool("adb", "import json,sys\nargs=sys.argv[1:]\nprint('priority=0\\ndev.test.app/.MainActivity' if 'resolve-activity' in args[-1] else json.dumps(args))\n")
        url = "app://test?q=$(touch /tmp/bad)&x='value'"
        value = dispatch("android.deep_link", {"device": "emulator-5554", "url": url})
        self.assertEqual(shlex.split(json.loads(value["stdout"])[-1])[-1], url)
        value = dispatch("android.launch", {"device": "emulator-5554", "bundle_id": "dev.test.app"})
        self.assertEqual(shlex.split(json.loads(value["stdout"])[-1]), ["am", "start", "-W", "-n", "dev.test.app/.MainActivity"])
        with self.assertRaises(StudioError): dispatch("android.input", {"device": "x;touch bad", "action": "tap"})
        self.tool("adb", "print('No activity found')\n")
        with self.assertRaises(StudioError): dispatch("android.launch", {"device": "emulator-5554", "bundle_id": "dev.test.app"})

    def test_native_split_apk_jobs_and_ipa_validation(self):
        self.tool("adb")
        files = [self.root / "base.apk", self.root / "split.apk"]
        for path in files: path.touch()
        job = dispatch("device.install", {"platform": "android", "device": "serial", "paths": list(map(str, files))})
        self.assertIn("install-multiple", job["plan"]["steps"][0]["argv"])
        self.assertEqual(jobs.wait(job["id"], 5)["state"], "succeeded")
        with self.assertRaises(StudioError): dispatch("device.install", {"platform": "ios", "device": "serial", "paths": [str(files[0])]})

    def test_an_app_bundle_is_built_for_the_device_and_installed_through_bundletool(self):
        import zipfile
        self.tool("adb")
        bundle = self.root / "app.aab"
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("BundleConfig.pb", b"")
            archive.writestr("base/manifest/AndroidManifest.xml", b"")
        with self.assertRaises(StudioError) as missing:
            dispatch("device.install", {"platform": "android", "device": "emulator-5554", "paths": [str(bundle)]})
        self.assertEqual(missing.exception.code, "tool_missing")
        self.assertIn("github.com/google/bundletool", missing.exception.args[0] + json.dumps(dispatch("doctor")["tools"]))
        log = self.root / "bundletool.jsonl"
        recorder = ("import json,sys\nargs=sys.argv[1:]\nopen(%r,'a').write(json.dumps(args)+'\\n')\n"
                    "out=[a.split('=',1)[1] for a in args if a.startswith('--output=')]\n"
                    "out and open(out[0],'w').write('apks')\n") % str(log)
        self.tool("bundletool", recorder)
        job = dispatch("device.install", {"platform": "android", "device": "emulator-5554", "paths": [str(bundle)]})
        self.assertEqual(jobs.wait(job["id"], 5)["state"], "succeeded")
        build, install = [json.loads(line) for line in log.read_text().splitlines()]
        adb = str(self.root / "adb")
        self.assertEqual(build[0], "build-apks")
        self.assertIn(f"--bundle={bundle}", build)
        self.assertIn("--connected-device", build)
        self.assertIn("--device-id=emulator-5554", build)
        self.assertIn(f"--adb={adb}", build)
        apks = next(a.split("=", 1)[1] for a in build if a.startswith("--output="))
        self.assertEqual(install, ["install-apks", f"--apks={apks}", "--device-id=emulator-5554", f"--adb={adb}"])
        self.assertFalse(Path(apks).parent.exists(), "the APK set is removed after the job")
        # A JAR runs through java -jar.
        jar = self.root / "bundletool-all.jar"
        jar.write_bytes(b"PK")
        self.tool("java", "import sys,runpy\nsys.argv=sys.argv[2:]\nexec(open(%r).read())\n" % str(self.root / "bundletool"))
        (self.root / "bundletool").chmod(0o644)
        dispatch("settings.configure", {"executables": {"bundletool": str(jar)}})
        log.unlink()
        job = dispatch("device.install", {"platform": "android", "device": "emulator-5554", "paths": [str(bundle)]})
        self.assertEqual(job["plan"]["steps"][0]["argv"][:3], [str(self.root / "java"), "-jar", str(jar)])
        self.assertEqual(jobs.wait(job["id"], 5)["state"], "succeeded")
        # An archive without a bundle configuration is not a bundle.
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("AndroidManifest.xml", b"")
        with self.assertRaises(StudioError) as refused:
            dispatch("device.install", {"platform": "android", "device": "emulator-5554", "paths": [str(bundle)]})
        self.assertEqual(refused.exception.code, "invalid_artifact")

    def test_avd_registry_and_active_device_protection(self):
        path = self.root / "state/avds/Pixel.avd"
        path.mkdir(parents=True)
        (path / "config.ini").write_text("hw.lcd.width=100\nimage.sysdir.1=system-images/test/\n")
        dispatch("android.avd_profile", {"name": "Pixel", "profile_id": "pixel-11"})
        self.assertTrue((path.parent / "Pixel.ini").is_file())
        self.tool("emulator")
        job = dispatch("android.avd_start", {"name": "Pixel", "headless": True})
        self.assertEqual(job["plan"]["env"]["ANDROID_AVD_HOME"], str(path.parent))
        self.assertIn("-no-window", job["plan"]["steps"][0]["argv"])
        self.assertNotIn("-no-audio", job["plan"]["steps"][0]["argv"])
        self.assertEqual(jobs.wait(job["id"], 5)["state"], "succeeded")
        silent = dispatch("android.avd_start", {"name": "Pixel", "headless": True, "audio": False})
        self.assertIn("-no-audio", silent["plan"]["steps"][0]["argv"])
        self.assertEqual(jobs.wait(silent["id"], 5)["state"], "succeeded")
        with (path / "multiinstance.lock").open("a") as active:
            fcntl.flock(active, fcntl.LOCK_EX)
            with self.assertRaises(StudioError): dispatch("android.avd_profile", {"name": "Pixel", "profile_id": "pixel-11"})

    def test_avd_performance_settings_preserve_data_and_skip_old_snapshots(self):
        path = self.root / "state/avds/Performance.avd"
        path.mkdir(parents=True)
        config = path / "config.ini"
        config.write_text("# preserve comments\nhw.ramSize=2G\nhw.cpu.ncore=4\nhw.gpu.mode=auto\n")
        data = path / "userdata-qemu.img"
        data.write_bytes(b"existing user data")
        (path.parent / "Performance.ini").write_text(f"path={path}\n")
        before = config.read_bytes()
        avd = next(v for v in dispatch("android.avds", {})["avds"] if v["name"] == "Performance")
        self.assertEqual(avd["memory_mb"], 2048)
        result = dispatch("android.avd_configure", dict(name="Performance", gpu="host", vulkan="off", memory_mb=4096))
        self.assertEqual(Path(result["backup"]).read_bytes(), before)
        self.assertIn("# preserve comments", config.read_text())
        self.assertIn("hw.cpu.ncore=4", config.read_text())
        self.assertIn("hw.gpu.mode=host", config.read_text())
        self.assertEqual(data.read_bytes(), b"existing user data")
        dispatch("android.avd_configure", dict(name="Performance", cores=8))
        self.assertEqual(Path(result["backup"]).read_bytes(), before)
        dispatch("android.avd_profile", dict(name="Performance", profile_id="pixel-11"))
        self.assertIn("hw.gpu.mode=host", config.read_text())
        self.assertIn("hw.ramSize=4096", config.read_text())
        self.tool("emulator")
        job = dispatch("android.avd_start", dict(name="Performance", headless=True))
        self.assertIn("-no-snapshot-load", job["plan"]["steps"][0]["argv"])
        self.assertIn("-Vulkan", job["plan"]["steps"][0]["argv"])
        self.assertEqual(jobs.wait(job["id"], 5)["state"], "succeeded")
        before = config.read_bytes()
        (path / "hardware-qemu.ini.lock").touch()
        with (path / "multiinstance.lock").open("a") as active:
            fcntl.flock(active, fcntl.LOCK_EX)
            with self.assertRaises(StudioError) as error:
                dispatch("android.avd_configure", dict(name="Performance", gpu="swiftshader"))
            self.assertEqual(error.exception.code, "avd_in_use")
            self.assertEqual(config.read_bytes(), before)
        # The files persist after shutdown/crash; an unlocked device is editable.
        dispatch("android.avd_configure", dict(name="Performance", cores=4))
        self.assertTrue((path / "hardware-qemu.ini.lock").exists())

    def test_avd_settings_reject_pending_start_before_emulator_takes_its_lock(self):
        path = self.root / "state/avds/Starting.avd"
        path.mkdir(parents=True)
        config = path / "config.ini"
        config.write_text("hw.gpu.mode=auto\n")
        (path.parent / "Starting.ini").write_text(f"path={path}\n")
        jobs.listing()
        job_id = uuid.uuid4().hex
        spec = dict(cwd=str(self.root), steps=[], exclusive="android-avd:Starting")
        with closing(sqlite3.connect(self.root / "state/jobs.sqlite3")) as db, db:
            db.execute("INSERT INTO jobs(id,label,state,created,updated,spec) VALUES(?,?,'queued',?,?,?)",
                       (job_id, "pending emulator", time.time(), time.time(), json.dumps(spec)))
        for op, fields in (("android.avd_configure", {"gpu":"host"}),
                           ("android.avd_profile", {"profile_id":"pixel-11"})):
            with self.assertRaises(StudioError) as error:
                dispatch(op, dict(name="Starting", **fields))
            self.assertEqual(error.exception.code, "avd_in_use")
            self.assertEqual(config.read_text(), "hw.gpu.mode=auto\n")
        # A dead, never-started worker is recovered instead of blocking forever.
        with closing(sqlite3.connect(self.root / "state/jobs.sqlite3")) as db, db:
            db.execute("UPDATE jobs SET created=? WHERE id=?", (time.time()-60, job_id))
        dispatch("android.avd_configure", dict(name="Starting", gpu="host"))
        self.assertEqual(jobs.get(job_id)["state"], "failed")
        self.assertIn("hw.gpu.mode=host", config.read_text())

    def test_avd_performance_rejects_invalid_settings_before_writing(self):
        path = self.root / "state/avds/Invalid.avd"
        path.mkdir(parents=True)
        config = path / "config.ini"
        config.write_text("hw.gpu.mode=auto\n")
        (path.parent / "Invalid.ini").write_text(f"path={path}\n")
        for fields in ({}, {"gpu":"bogus"}, {"memory_mb":0}, {"memory_mb":999999}, {"cores":0}, {"cores":33}):
            with self.assertRaises(StudioError):
                dispatch("android.avd_configure", dict(name="Invalid", **fields))
            self.assertEqual(config.read_text(), "hw.gpu.mode=auto\n")
        with self.assertRaises(StudioError):
            dispatch("android.avd_configure", dict(name="../Invalid", gpu="host"))

    def test_emulator_profiles_survive_discovery_and_ambiguous_displays_stay_unknown(self):
        path = self.root / "state/avds/Pixel.avd"
        path.mkdir(parents=True)
        config = path / "config.ini"
        config.write_text("image.sysdir.1=system-images/android-36/google_apis/x86_64/\n")
        dispatch("android.avd_profile", {"name": "Pixel", "profile_id": "pixel-11"})
        sdk = self.root / "sdk"
        image = sdk / "system-images/android-36/google_apis/x86_64/system.img"
        image.parent.mkdir(parents=True); image.touch()
        dispatch("settings.configure", {"android_sdk": str(sdk)})
        self.tool("adb", "import sys\na=sys.argv[1:]\nif a[:1]==['devices']: print('List of devices attached\\nemulator-5554 device model:sdk_gphone64_x86_64')\nelif a[-3:]==['emu','avd','name']: print('Pixel\\nOK')\nelif 'sys.boot_completed' in a[-1]: print('1')\n")
        value = dispatch("android.avds", {})
        avd = next(a for a in value["avds"] if a["name"] == "Pixel")
        self.assertEqual(avd["profile_id"], "pixel-11")
        self.assertEqual(avd["profile_source"], "assigned")
        self.assertEqual(avd["display"], {"width": 1080, "height": 2424, "density": 420})
        self.assertIn("system-images;android-36;google_apis;x86_64", value["system_images"])
        device = dispatch("device.list", {"platform": "android"})["devices"][0]
        self.assertEqual(device["avd"], "Pixel")
        self.assertEqual(device["kind"], "emulator")
        self.assertEqual(device["profile_id"], "pixel-11")
        self.assertTrue(device["booted"])
        config.write_text(config.read_text().replace("tomacco.profile.id=pixel-11\n", ""))
        self.assertEqual(dispatch("device.list", {"platform": "android"})["devices"][0]["profile_source"], "display-match")
        custom = next(p for p in catalog.builtin() if p["id"] == "pixel-11")
        custom.update(id="my-own-phone", name="My own phone")
        source = self.root / "custom.json"; source.write_text(json.dumps(custom))
        catalog.import_profiles(str(source))
        device = dispatch("device.list", {"platform": "android"})["devices"][0]
        self.assertIsNone(device["profile_id"])
        self.assertEqual(device["profile_source"], "unknown")
        dispatch("android.avd_profile", {"name": "Pixel", "profile_id": "my-own-phone"})
        device = dispatch("device.list", {"platform": "android"})["devices"][0]
        self.assertEqual(device["profile_id"], "my-own-phone")
        self.assertEqual(device["name"], "My own phone · Pixel")

    def test_remote_quoting_and_traversal_rejection(self):
        self.tool("ssh")
        dispatch("remote.configure", {"name": "mac", "host": "user@mac", "directory": "/Users/user/my project"})
        job = dispatch("remote.run", {"name": "mac", "argv": ["printf", "$(danger) 'quoted'"]})
        command = job["plan"]["steps"][0]["argv"][-1]
        self.assertEqual(shlex.split(command), ["cd", "--", "/Users/user/my project", "&&", "exec", "printf", "$(danger) 'quoted'"])
        self.assertEqual(jobs.wait(job["id"], 5)["state"], "succeeded")
        with self.assertRaises(StudioError): dispatch("remote.configure", {"name": "mac", "host": "-oProxyCommand=evil", "directory": "/Users/user"})
        with self.assertRaises(StudioError): dispatch("remote.fetch", {"name": "mac", "remote_path": "../secret", "path": str(self.root / "out")})

    def test_created_native_projects_have_no_python_build_dependency(self):
        sdk = self.root / "sdk"
        (sdk / "platforms/android-36").mkdir(parents=True)
        self.tool("gradle")
        dispatch("settings.configure", {"android_sdk": str(sdk)})
        for kind in ("android", "swift-android"):
            project = self.root / kind
            result = dispatch("project.create", {"path": str(project), "kind": kind, "organization": "dev.test_app", "name": "Counter"})
            self.assertEqual(result["api"], 36)
            java = next(project.rglob("*.java")).read_text()
            self.assertEqual('System.loadLibrary' in java, kind == "swift-android")
            self.assertFalse(list(project.rglob("*.py")))
            if kind == "swift-android":
                self.assertIn("Java_dev_test_1app_counter_MainActivity_nextCount", next(project.rglob("Counter.swift")).read_text())
                self.assertIn("ProcessBuilder", (project / "app/build.gradle.kts").read_text())
            with self.assertRaises(StudioError): dispatch("project.create", {"path": str(project), "kind": kind})
        (sdk / "platforms/android-36").rename(sdk / "platforms/android-35")
        with self.assertRaises(StudioError) as old:
            dispatch("project.create", {"path": str(self.root / "old"), "kind": "compose"})
        self.assertEqual(old.exception.code, "sdk_platform_missing")
        (sdk / "platforms/android-35").rename(sdk / "platforms/android-36")
        project = self.root / "compose"
        result = dispatch("project.create", {"path": str(project), "kind": "compose", "organization": "dev.test_app", "name": "Counter"})
        self.assertEqual(result["application_id"], "dev.test_app.counter")
        activity = project / "app/src/main/java/dev/test_app/counter/MainActivity.kt"
        self.assertIn("setContent", activity.read_text())
        self.assertIn('title = "Counter"', activity.read_text())
        test = (project / "app/src/androidTest/java/dev/test_app/counter/CounterTest.kt").read_text()
        self.assertIn("package dev.test_app.counter", test)
        self.assertIn("createComposeRule", test)
        self.assertIn("compileSdk = 36", (project / "app/build.gradle.kts").read_text())
        self.assertFalse(list(project.rglob("*.java")))
        inspected = dispatch("project.inspect", {"path": str(project)})
        app = inspected["android"]["app_modules"][0]
        self.assertEqual((app["application_id"], app["min_sdk"], app["target_sdk"]), ("dev.test_app.counter", 26, 36))

    def test_settings_validation_preserves_previous_configuration(self):
        tool = self.tool("adb")
        before = dispatch("settings.get")
        with self.assertRaises(StudioError): dispatch("settings.configure", {"executables": {"unknown": tool}})
        self.assertEqual(dispatch("settings.get"), before)


    def test_framework_plans_and_swift_sdk_setup(self):
        for tool in ('flutter', 'xtool', 'npm'): self.tool(tool)
        flutter = self.root / 'FlutterApp'
        flutter.mkdir(); (flutter / 'pubspec.yaml').write_text('flutter:\n')
        (flutter / 'android').mkdir(); (flutter / 'ios').mkdir()
        plan = dispatch('project.plan', {'path': str(flutter), 'format': 'aab'})
        self.assertEqual(plan['steps'][0]['argv'][1:], ['build', 'appbundle', '--debug'])
        with self.assertRaises(StudioError): dispatch('project.plan', {'path': str(flutter), 'platform': 'ios'})
        web = self.root / 'WebApp'; web.mkdir()
        (web / 'package.json').write_text(json.dumps({'scripts': {'build': 'vite build'}, 'dependencies': {'react-native': '1', 'expo': '1', '@capacitor/core': '1'}}))
        self.assertEqual(dispatch('project.inspect', {'path': str(web)})['kinds'], ['react-native', 'expo', 'capacitor', 'web'])
        self.assertEqual(dispatch('project.plan', {'path': str(web), 'platform': 'web'})['steps'][0]['argv'][1:], ['run', 'build'])
        ios = self.root / 'IOSApp'; ios.mkdir(); (ios / 'xtool.yml').write_text('bundleID: dev.test.app\n')
        plan = dispatch('project.plan', {'path': str(ios), 'platform': 'ios', 'format': 'ipa', 'sign': True})
        self.assertEqual(plan['steps'][0]['argv'][1:], ['dev', 'build', '--configuration', 'debug', '--sign', '--ipa'])
        package = self.root / 'Swift'; package.mkdir(); (package / 'Package.swift').touch()
        sysroot = self.root / 'sdk/sysroot'; (sysroot / 'usr/include').mkdir(parents=True)
        script = sysroot.parent / 'scripts/setup-android-sdk.sh'; script.parent.mkdir(); script.write_text('exit 0\n')
        ndk = self.root / 'ndk'; ndk.mkdir(); (ndk / 'source.properties').touch()
        self.tool('swift', 'import os;print("sdkRootPath: " + os.environ["TOMACCO_TEST_ROOT"] + "/sdk/sysroot")\n')
        dispatch('settings.configure', {'android_ndk': str(ndk), 'swift_android_sdk': 'swift-test_android'})
        plan = dispatch('swift.plan', {'path': str(package), 'architecture': 'x86_64'})
        self.assertIn('x86_64-unknown-linux-android26', plan['steps'][0]['argv'])
        self.assertTrue(dispatch('swift.status')['android']['configured'])
        job = dispatch('swift.android_configure')
        self.assertEqual(jobs.wait(job['id'], 5)['state'], 'succeeded')
        with self.assertRaises(StudioError): dispatch('swift.sdk_install', {'source': 'https://example.invalid/sdk.tar.gz'})
        with self.assertRaises(StudioError): dispatch('swift.plan', {'path': str(package), 'action': 'test'})

    def test_concurrent_run_reuses_job_and_machine_reload_targets_same_app(self):
        self.tool('flutter', '''import json,sys,time
from pathlib import Path
assert '--machine' in sys.argv
with Path('launches').open('a') as f:f.write('launch\\n')
print(json.dumps([{'event':'app.start','params':{'appId':'fixture-app'}}]),flush=True)
print(json.dumps([{'event':'app.started','params':{'appId':'fixture-app'}}]),flush=True)
for line in sys.stdin:
 value=json.loads(line)[0]
 Path('command.json').write_text(json.dumps(value))
 if value['method']=='app.stop':break
 print(json.dumps([{'id':value['id'],'result':{'code':0}}]),flush=True)
''')
        (self.root/'pubspec.yaml').write_text('flutter:\n');(self.root/'android').mkdir()
        args={'path':str(self.root),'platform':'android','device':'emulator-5554'}
        with ThreadPoolExecutor(max_workers=8) as pool:
            values=list(pool.map(lambda _:dispatch('project.run',args),range(8)))
        self.assertEqual(len({value['id'] for value in values}),1)
        self.assertEqual(sum(bool(value.get('reused')) for value in values),7)
        job=values[0];deadline=time.monotonic()+5*PATIENCE
        while jobs.get(job['id']).get('phase')!='ready':
            self.assertLess(time.monotonic(),deadline);time.sleep(.03)
        self.assertEqual((self.root/'launches').read_text(),'launch\n')
        with self.assertRaises(StudioError) as blocked:
            dispatch('project.run',{**args,'device':'emulator-5556'})
        self.assertEqual(blocked.exception.code,'workspace_busy')
        self.assertEqual(blocked.exception.details['job']['id'],job['id'])
        with self.assertRaises(StudioError) as blocked:
            dispatch('project.execute',{'path':str(self.root),'platform':'android'})
        self.assertEqual(blocked.exception.code,'workspace_busy')
        jobs.send(job['id'],'reload')
        while not (self.root/'command.json').exists():
            self.assertLess(time.monotonic(),deadline);time.sleep(.03)
        command=json.loads((self.root/'command.json').read_text())
        self.assertEqual(command['method'],'app.restart');self.assertEqual(command['params']['appId'],'fixture-app')
        self.assertFalse(command['params']['fullRestart'])
        jobs.send(job['id'],'quit');self.assertEqual(jobs.wait(job['id'],5)['state'],'succeeded')
        again=dispatch('project.run',args);self.assertNotEqual(again['id'],job['id'])

    def test_repeated_build_reuses_active_job_and_allows_next_build_after_finish(self):
        self.tool('flutter','import time;print("building",flush=True);time.sleep(.7)\n')
        (self.root/'pubspec.yaml').write_text('flutter:\n');(self.root/'android').mkdir()
        args={'path':str(self.root),'platform':'android','action':'build'}
        with ThreadPoolExecutor(max_workers=6) as pool:
            values=list(pool.map(lambda _:dispatch('project.execute',args),range(6)))
        self.assertEqual(len({value['id'] for value in values}),1)
        self.assertEqual(jobs.wait(values[0]['id'],5)['state'],'succeeded')
        later=dispatch('project.execute',args);self.assertNotEqual(later['id'],values[0]['id'])

    def test_managed_avd_creation_uses_native_followup_worker(self):
        self.tool('avdmanager', 'import pathlib,sys\np=pathlib.Path(sys.argv[sys.argv.index("--path")+1]);p.mkdir(parents=True);(p/"config.ini").write_text("image.sysdir.1=system-images/test/\\n")\n')
        job = dispatch('android.avd_create', {'name': 'Fixture', 'system_image': 'system-images;android-36;google_apis;x86_64', 'profile_id': 'pixel-11'})
        done = jobs.wait(job['id'], 5)
        self.assertEqual(done['state'], 'succeeded', jobs.logs(job['id']))
        self.assertIn('call', done['plan']['steps'][1]['argv'])
        self.assertNotIn('python', done['plan']['steps'][1]['argv'][0])
        self.assertTrue(any(v['name'] == 'Fixture' and v['managed'] for v in dispatch('android.avds')['avds']))

    def test_corrupt_user_state_and_directory_outputs_return_errors(self):
        state = self.root / 'state'; state.mkdir(exist_ok=True)
        for filename, operation in [('settings.json', 'settings.get'), ('remotes.json', 'remote.list'), ('profiles.json', 'catalog.list')]:
            target = state / filename; target.write_text('[true]')
            with self.assertRaises(StudioError): dispatch(operation)
            target.unlink()
        for operation in ['device.screenshot', 'preview.capture']:
            args = {'path': '/'}
            if operation.startswith('device'): args.update(platform='android', device='fixture')
            with self.assertRaises(StudioError): dispatch(operation, args)


class AndroidControlTests(Isolated):
    """Android device control through a fake adb that records each call and
    answers from adb-replies.json: the first key found in the command wins, and
    a reply that starts with FAIL: exits 1."""
    ADB = ("import json,os,sys,time\nfrom pathlib import Path\nroot=Path(os.environ['TOMACCO_TEST_ROOT'])\na=sys.argv[1:]\n"
           "with (root/'adb-calls.jsonl').open('a') as f: f.write(json.dumps(a)+'\\n')\n"
           "replies=json.loads((root/'adb-replies.json').read_text()) if (root/'adb-replies.json').exists() else {}\n"
           "command=' '.join(a[2:])\n"
           "for key,reply in replies.items():\n"
           "    if key in command: print(reply.removeprefix('FAIL:'),end=''); sys.exit(1 if reply.startswith('FAIL:') else 0)\n"
           "if 'app_process' in command: time.sleep(30)\n")

    def setUp(self):
        super().setUp()
        self.tool("adb", self.ADB)

    def replies(self, **values):
        (self.root / "adb-replies.json").write_text(json.dumps(values))

    def calls(self):
        path = self.root / "adb-calls.jsonl"
        return [json.loads(line)[2:] for line in path.read_text().splitlines()] if path.exists() else []

    def shell_calls(self):
        return [shlex.split(call[1]) for call in self.calls() if call[:1] == ["shell"]]

    def test_unicode_text_is_pasted_through_a_control_only_scrcpy_server(self):
        self.tool("scrcpy")
        (self.root / "scrcpy-server").write_bytes(b"jar")
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        received = {}

        def serve():
            connection, _ = listener.accept()
            with connection:
                connection.sendall(b"\0")
                header = b""
                while len(header) < 14: header += connection.recv(14 - len(header))
                length = int.from_bytes(header[10:14], "big")
                text = b""
                while len(text) < length: text += connection.recv(length - len(text))
                received.update(kind=header[0], paste=header[9], text=text.decode())
                connection.sendall(b"\1" + header[1:9])
                connection.recv(1)
        server = threading.Thread(target=serve, daemon=True)
        server.start()
        self.replies(**{"forward tcp:0": f"{listener.getsockname()[1]}\n", "ro.build.version.sdk": "36\n"})
        value = dispatch("android.input", {"device": "emulator-5554", "action": "text", "text": "Grüße 👋 %s"})
        server.join(5)
        self.assertEqual(value["method"], "clipboard")
        self.assertEqual(received, {"kind": 9, "paste": 1, "text": "Grüße 👋 %s"})
        calls = self.calls()
        push = next(call for call in calls if call[:1] == ["push"])
        self.assertEqual(push[1], str((self.root / "scrcpy-server").resolve()))
        server_args = next(args for args in self.shell_calls() if "app_process" in args)
        for option in ("video=false", "audio=false", "control=true", "tunnel_forward=true"):
            self.assertIn(option, server_args)
        self.assertTrue(any(call[:2] == ["forward", "--remove"] for call in calls))
        self.assertIn(["rm", "-f", push[2]], self.shell_calls())
        # ASCII keeps adb's own text input.
        dispatch("android.input", {"device": "emulator-5554", "action": "text", "text": "plain text"})
        self.assertEqual(self.shell_calls()[-1], ["input", "text", "plain%stext"])

    def test_input_sent_while_recording_becomes_a_scenario(self):
        screen = ('<hierarchy><node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">'
                  '<node class="android.widget.Button" text="Add one" resource-id="" clickable="true" bounds="[0,500][540,600]"/>'
                  '</node></hierarchy>')
        self.replies(**{"cat /data/local/tmp/tomacco-": screen})
        device = {"device": "emulator-5554"}
        dispatch("android.input", {**device, "action": "tap", "x": 100, "y": 550})
        self.assertFalse(any("uiautomator" in " ".join(call) for call in self.calls()), "no hierarchy without a recording")
        dispatch("testing.record", {**device, "action": "start", "name": "Counter"})
        dispatch("android.input", {**device, "action": "tap", "x": 100, "y": 550})
        dispatch("android.input", {**device, "action": "tap", "x": 900, "y": 2000})
        dispatch("android.input", {**device, "action": "keyevent", "key": "KEYCODE_BACK"})
        self.assertEqual(dispatch("testing.record", {**device, "action": "status"})["steps"], 3)
        target = self.root / "counter.json"
        result = dispatch("testing.record", {**device, "action": "stop", "path": str(target)})
        self.assertEqual(result["path"], str(target))
        self.assertEqual(json.loads(target.read_text()), {"name": "Counter", "steps": [
            {"action": "click", "selector": {"text": "Add one"}},
            {"action": "tap", "x": 900, "y": 2000},
            {"action": "key", "key": "KEYCODE_BACK"}]})
        self.assertIn(["input", "tap", "900", "2000"], self.shell_calls())
        self.assertFalse(dispatch("testing.record", {**device, "action": "status"})["recording"])

    def test_lint_checks_a_scenario_against_the_screen_without_acting(self):
        screen = ('<hierarchy><node bounds="[0,0][1080,2400]"><node text="OK" enabled="true" bounds="[0,10][20,30]"/>'
                  '<node text="OK" enabled="true" bounds="[30,10][60,30]"/></node></hierarchy>')
        self.replies(**{"cat /data/local/tmp/tomacco-": screen})
        scenario = self.root / "scenario.json"
        scenario.write_text(json.dumps({"name": "Lint", "steps": [{"action": "click", "selector": {"text": "OK"}},
                                                                   {"action": "tap", "x": 10, "y": 20}]}))
        result = dispatch("testing.lint", {"path": str(scenario), "device": "emulator-5554"})
        self.assertEqual((result["valid"], result["passed"]), (True, False))
        self.assertEqual([(f["step"], f["code"]) for f in result["findings"]], [(0, "ambiguous")])
        self.assertFalse(any(args[:1] == ["input"] for args in self.shell_calls()), "lint never acts")
        saved = self.root / "screen.xml"
        saved.write_text(screen)
        scenario.write_text(json.dumps({"name": "Lint", "steps": [{"action": "click", "selector": {"text": "OK", "index": 0}}]}))
        self.assertTrue(dispatch("testing.lint", {"path": str(scenario), "hierarchy": str(saved)})["passed"])
        scenario.write_text(json.dumps({"name": "Lint", "steps": [{"action": "tap", "x": 10}]}))
        result = dispatch("testing.lint", {"path": str(scenario), "hierarchy": str(saved)})
        self.assertEqual((result["valid"], result["error"]["code"]), (False, "invalid_argument"))
        with self.assertRaises(StudioError):
            dispatch("testing.lint", {"path": str(scenario)})

    def test_a_failed_scenario_is_retried_and_a_late_pass_reported_flaky(self):
        # The first key press fails, every later one works.
        self.tool("adb", self.ADB.replace("command=' '.join(a[2:])\n",
                                          "command=' '.join(a[2:])\n"
                                          "if 'keyevent' in command:\n"
                                          "    marker=root/'failed-once'\n"
                                          "    if not marker.exists(): marker.write_text('1'); print('device offline'); sys.exit(1)\n"))
        scenario = self.root / "scenario.json"
        scenario.write_text(json.dumps({"name": "Back", "steps": [{"action": "key", "key": "KEYCODE_BACK"}]}))
        report = self.root / "report"
        job = dispatch("testing.run", {"path": str(scenario), "directory": str(report), "devices": ["emulator-5554"], "retries": 1})
        done = jobs.wait(job["id"], 60 * PATIENCE)
        self.assertEqual(done["state"], "succeeded", jobs.logs(job["id"])["text"])
        value = json.loads((report / "report.json").read_text())
        self.assertEqual((value["passed"], value["flaky"], value["retries"]), (True, [{"scenario": "Back", "device": "emulator-5554"}], 1))
        result = value["results"][0]
        self.assertTrue(result["flaky"])
        self.assertEqual([attempt["passed"] for attempt in result["attempts"]], [False, True])
        self.assertEqual(result["attempts"][0]["failed_step"], 0)
        self.assertIn("<flakyFailure", (report / "junit.xml").read_text())

    def test_scenarios_are_sharded_across_devices_into_one_report(self):
        scenarios = self.root / "scenarios"
        scenarios.mkdir()
        for number, key in enumerate(["KEYCODE_1", "KEYCODE_2", "KEYCODE_3"]):
            (scenarios / f"{number}.json").write_text(json.dumps({"name": f"Press {number}", "steps": [{"action": "key", "key": key}]}))
        devices = ["emulator-5554", "emulator-5556"]
        run = lambda directory, **extra: jobs.wait(dispatch("testing.run", {"path": str(scenarios), "directory": str(self.root / directory),
                                                                            "devices": devices, **extra})["id"], 60 * PATIENCE)
        self.assertEqual(run("shard", shard=True)["state"], "succeeded")
        value = json.loads((self.root / "shard/report.json").read_text())
        self.assertEqual((value["passed"], value["shard"], value["scenarios"]), (True, True, ["Press 0", "Press 1", "Press 2"]))
        self.assertEqual([r["scenario"] for r in value["results"]], ["Press 0", "Press 1", "Press 2"])
        self.assertTrue({r["device"] for r in value["results"]} <= set(devices))
        # Each scenario ran once, on the device its result names.
        presses = [(call[1], call[-1]) for call in map(json.loads, (self.root / "adb-calls.jsonl").read_text().splitlines())
                   if "keyevent" in call[-1]]
        self.assertEqual(sorted(presses), sorted((r["device"], f"input keyevent KEYCODE_{n + 1}") for n, r in enumerate(value["results"])))
        junit = (self.root / "shard/junit.xml").read_text()
        self.assertIn('tests="3" failures="0"', junit)
        self.assertIn('classname="Press 2"', junit)
        # Without shard every device runs every scenario.
        self.assertEqual(run("matrix")["state"], "succeeded")
        value = json.loads((self.root / "matrix/report.json").read_text())
        self.assertEqual(len(value["results"]), 6)
        (scenarios / "broken.json").write_text(json.dumps({"name": "Broken", "steps": []}))
        with self.assertRaises(StudioError) as broken:
            run("broken", shard=True)
        self.assertIn("broken.json", str(broken.exception))

    def test_unicode_text_names_what_it_needs_when_it_cannot_work(self):
        with self.assertRaises(StudioError) as missing:
            dispatch("android.input", {"device": "emulator-5554", "action": "text", "text": "é"})
        self.assertEqual(missing.exception.code, "text_unsupported")
        self.assertIn("scrcpy", str(missing.exception))
        self.tool("scrcpy")
        (self.root / "scrcpy-server").write_bytes(b"jar")
        self.replies(**{"ro.build.version.sdk": "23\n"})
        with self.assertRaises(StudioError) as old:
            dispatch("android.input", {"device": "emulator-5554", "action": "text", "text": "é"})
        self.assertEqual(old.exception.code, "text_unsupported")
        self.assertFalse(any(call[:1] == ["push"] for call in self.calls()))


    COLD_START = ("Starting: Intent { cmp=dev.test.app/.Main }\nStatus: ok\nLaunchState: COLD\n"
                  "Activity: dev.test.app/.Main\nTotalTime: 412\nWaitTime: 420\nComplete\n")

    def test_startup_is_measured_over_cold_launches_and_saved_as_a_report(self):
        self.replies(**{"resolve-activity": "priority=0 preferredOrder=0\ndev.test.app/.Main\n", "am start -W": self.COLD_START})
        target = self.root / "startup.json"
        value = dispatch("profile.startup", {"device": "emulator-5554", "bundle_id": "dev.test.app", "runs": 3, "report": str(target)})
        self.assertEqual((value["mode"], value["component"], value["mismatched_runs"]), ("cold", "dev.test.app/.Main", 0))
        self.assertEqual(value["metrics"]["startup_cold_median_ms"], 412)
        self.assertEqual(value["total_ms"]["count"], 3)
        self.assertEqual(json.loads(target.read_text())["metrics"], value["metrics"])
        shell = self.shell_calls()
        self.assertEqual(sum(args[:2] == ["am", "force-stop"] for args in shell), 3)
        self.assertEqual(sum(args[:3] == ["am", "start", "-W"] for args in shell), 3)
        # Warm keeps the process: one unmeasured launch, then the activity is recreated.
        (self.root / "adb-calls.jsonl").unlink()
        value = dispatch("profile.startup", {"device": "emulator-5554", "bundle_id": "dev.test.app", "mode": "warm", "runs": 1,
                                             "activity": ".Main"})
        self.assertEqual(value["mismatched_runs"], 1)
        shell = self.shell_calls()
        self.assertFalse(any("force-stop" in args for args in shell))
        self.assertIn(["am", "start", "-W", "--activity-clear-task", "-n", "dev.test.app/.Main"], shell)
        self.replies(**{"resolve-activity": "dev.test.app/.Main\n", "am start -W": "Error: Activity class {dev.test.app/.Main} does not exist.\n"})
        with self.assertRaises(StudioError) as failed:
            dispatch("profile.startup", {"device": "emulator-5554", "bundle_id": "dev.test.app", "runs": 1})
        self.assertEqual(failed.exception.code, "launch_failed")

    GFXINFO = ("** Graphics info for pid 3095 [dev.test.app] **\nTotal frames rendered: 80\nJanky frames: 4 (5.00%)\n"
               "50th percentile: 18ms\n90th percentile: 29ms\n95th percentile: 32ms\n99th percentile: 85ms\n"
               "Number Missed Vsync: 1\nHISTOGRAM: 16ms=70 17ms=6 85ms=2 700ms=1 4950ms=1\n")

    def test_frames_are_counted_between_a_reset_and_a_read_around_a_scenario(self):
        self.replies(**{"gfxinfo dev.test.app reset": "", "gfxinfo dev.test.app": self.GFXINFO})
        scenario = self.root / "scroll.json"
        scenario.write_text(json.dumps({"name": "Scroll", "steps": [{"action": "swipe", "x": 500, "y": 1800, "end_x": 500, "end_y": 400}]}))
        target = self.root / "frames.json"
        value = dispatch("profile.frames", {"device": "emulator-5554", "bundle_id": "dev.test.app", "scenario": str(scenario),
                                            "report": str(target)})
        self.assertEqual(value["scenario"], "Scroll")
        self.assertEqual({k: value["metrics"][k] for k in ("frames_total", "jank_percent", "slow_frames", "frozen_frames", "frame_p90_ms")},
                         {"frames_total": 80, "jank_percent": 5, "slow_frames": 10, "frozen_frames": 2, "frame_p90_ms": 29})
        self.assertEqual(value["processes"][0]["counters"]["missed_vsync"], 1)
        self.assertTrue(target.exists())
        shell = self.shell_calls()
        order = [i for i, args in enumerate(shell) if args[:1] in (["dumpsys"], ["input"])]
        self.assertEqual([shell[i][:2] if shell[i][0] == "input" else shell[i][1:] for i in order],
                         [["gfxinfo", "dev.test.app", "reset"], ["input", "swipe"], ["gfxinfo", "dev.test.app"]])
        self.replies(**{"gfxinfo": "No process found for: dev.test.app\n"})
        with self.assertRaises(StudioError) as stopped:
            dispatch("profile.frames", {"device": "emulator-5554", "bundle_id": "dev.test.app", "seconds": 0})
        self.assertEqual(stopped.exception.code, "app_not_running")

    MEMINFO = ("** MEMINFO in pid 3095 [dev.test.app] **\n"
               "  Dalvik Heap     9747     9648        0        0    16328    15105     7553     7552\n"
               " App Summary\n                       Pss(KB)                        Rss(KB)\n"
               "                        ------                         ------\n"
               "           Java Heap:    11264                          48396\n"
               "           TOTAL PSS:    61440            TOTAL RSS:   221696      TOTAL SWAP (KB):        0\n"
               " Objects\n               Views:      164         ViewRootImpl:        1\n"
               "         AppContexts:        9           Activities:        2\n")

    def test_memory_is_read_from_meminfo_and_a_heap_dump_is_pulled_and_removed(self):
        self.tool("adb", self.ADB.replace("command=' '.join(a[2:])\n", "command=' '.join(a[2:])\n"
                                          "if a[2:3]==['pull']: Path(a[-1]).write_bytes(b'JAVA PROFILE 1.0.3\\0'+b'x'*64); sys.exit(0)\n"))
        self.replies(**{"dumpsys meminfo dev.test.app": self.MEMINFO, "am dumpheap": "File: x\nWaiting for dump to finish...\n"})
        dump = self.root / "app.hprof"
        value = dispatch("profile.memory", {"device": "emulator-5554", "bundle_id": "dev.test.app", "heap_dump": str(dump)})
        self.assertEqual((value["metrics"]["memory_pss_mb"], value["metrics"]["java_heap_pss_mb"], value["metrics"]["activities"]), (60, 11, 2))
        self.assertEqual(value["processes"][0]["heaps"]["dalvik"]["allocated"], 7553)
        self.assertEqual(value["heap_dump"]["path"], str(dump))
        remote = next(args[3] for args in self.shell_calls() if args[:2] == ["am", "dumpheap"])
        self.assertIn(["rm", "-f", remote], self.shell_calls())
        self.replies(**{"dumpsys meminfo": self.MEMINFO, "am dumpheap": "java.lang.SecurityException: Process not debuggable\n"})
        with self.assertRaises(StudioError) as refused:
            dispatch("profile.memory", {"device": "emulator-5554", "bundle_id": "dev.test.app", "heap_dump": str(dump)})
        self.assertEqual(refused.exception.code, "heap_dump_failed")
        self.replies(**{"dumpsys meminfo": "No process found for: dev.test.app\n"})
        with self.assertRaises(StudioError) as stopped:
            dispatch("profile.memory", {"device": "emulator-5554", "bundle_id": "dev.test.app"})
        self.assertEqual(stopped.exception.code, "app_not_running")

    def test_cpu_and_battery_use_are_the_change_over_the_interval(self):
        # Each read of /proc and of batterystats answers the next sample.
        samples = {"/proc/stat": ["100\ncpu  1000 0 1000 8000 0 0 0 0\n3095 (dev.test.app) S 1 1 0 0 -1 0 0 0 0 0 100 50 0 0\n",
                                  "100\ncpu  1100 0 1100 8800 0 0 0 0\n3095 (dev.test.app) S 1 1 0 0 -1 0 0 0 0 0 150 60 0 0\n"],
                   "batterystats": ["  UID u0a200: 0.010\n      cpu=0.010\n", "  UID u0a200: 0.025\n      cpu=0.020 wakelock=0.005\n"]}
        (self.root / "samples.json").write_text(json.dumps(samples))
        self.tool("adb", self.ADB.replace("command=' '.join(a[2:])\n", "command=' '.join(a[2:])\n"
                                          "for key,answers in json.loads((root/'samples.json').read_text()).items():\n"
                                          "    if key in command:\n"
                                          "        counter=root/('count-'+key.strip('/').replace('/','-'))\n"
                                          "        n=int(counter.read_text()) if counter.exists() else 0\n"
                                          "        counter.write_text(str(n+1)); print(answers[min(n,len(answers)-1)],end=''); sys.exit(0)\n"))
        self.replies(**{"list packages -U": "package:dev.test.app uid:10200\n", "ps -A": "  PID NAME\n 3095 dev.test.app\n 3300 dev.test.application\n",
                        "dumpsys battery": "  AC powered: false\n  USB powered: false\n  level: 81\n"})
        value = dispatch("profile.usage", {"device": "emulator-5554", "bundle_id": "dev.test.app", "seconds": 1})
        self.assertEqual(value["metrics"]["cpu_ms"], 600)
        self.assertEqual(value["metrics"]["cpu_device_percent"], 6)
        self.assertAlmostEqual(value["metrics"]["power_mah"], 0.015)
        self.assertAlmostEqual(value["battery"]["parts_mah"]["wakelock"], 0.005)
        self.assertEqual((value["battery"]["uid"], value["battery"]["charging"], value["battery"]["level"]), (10200, False, 81))
        self.assertEqual([p["pid"] for p in value["processes"]], [3095])
        self.assertGreater(value["metrics"]["cpu_percent"], 0)
        self.replies(**{"list packages -U": ""})
        with self.assertRaises(StudioError) as missing:
            dispatch("profile.usage", {"device": "emulator-5554", "bundle_id": "dev.test.app", "seconds": 1})
        self.assertEqual(missing.exception.code, "app_not_installed")

    def test_saved_reports_are_checked_against_a_budget_file_or_inline(self):
        startup, memory = self.root / "startup.json", self.root / "memory.json"
        startup.write_text(json.dumps({"bundle_id": "dev.test.app", "metrics": {"startup_cold_median_ms": 640, "startup_cold_p90_ms": 910}}))
        memory.write_text(json.dumps({"bundle_id": "dev.test.app", "metrics": {"memory_pss_mb": 180.5}}))
        budget = self.root / "budget.json"
        budget.write_text(json.dumps({"startup_cold_median_ms": {"maximum": 800}, "memory_pss_mb": {"maximum": 200}}))
        target = self.root / "verdict.json"
        value = dispatch("profile.budget", {"reports": [str(startup), str(memory)], "budget": str(budget), "report": str(target)})
        self.assertTrue(value["passed"])
        self.assertEqual([(r["metric"], r["value"], r["report"]) for r in value["results"]],
                         [("startup_cold_median_ms", 640, str(startup)), ("memory_pss_mb", 180.5, str(memory))])
        self.assertTrue(json.loads(target.read_text())["passed"])
        value = dispatch("profile.budget", {"reports": [str(startup)], "budgets": {"startup_cold_p90_ms": {"maximum": 900},
                                                                                   "jank_percent": {"maximum": 5}}})
        self.assertFalse(value["passed"])
        self.assertEqual([(r["passed"], r.get("reason")) for r in value["results"]], [(False, None), (False, "measurement_unavailable")])
        for arguments in ({"reports": [str(startup)]}, {"reports": [str(startup)], "budget": str(budget), "budgets": {"x": {"maximum": 1}}}):
            with self.assertRaises(StudioError) as refused:
                dispatch("profile.budget", arguments)
            self.assertEqual(refused.exception.code, "invalid_argument")
        memory.write_text(json.dumps({"bundle_id": "dev.other.app", "metrics": {}}))
        with self.assertRaises(StudioError) as mismatch:
            dispatch("profile.budget", {"reports": [str(startup), str(memory)], "budget": str(budget)})
        self.assertEqual(mismatch.exception.code, "profile_mismatch")

    def test_a_measurement_is_compared_with_a_saved_baseline(self):
        self.replies(**{"dumpsys meminfo dev.test.app": self.MEMINFO})
        baseline, current = self.root / "baseline.json", self.root / "current.json"
        dispatch("profile.memory", {"device": "emulator-5554", "bundle_id": "dev.test.app", "report": str(baseline)})
        self.replies(**{"dumpsys meminfo dev.test.app": self.MEMINFO.replace("61440", "67584")})
        dispatch("profile.memory", {"device": "emulator-5554", "bundle_id": "dev.test.app", "report": str(current)})
        value = dispatch("profile.compare", {"baseline": str(baseline), "current": str(current)})
        self.assertTrue(value["passed"])
        change = next(c for c in value["changes"] if c["metric"] == "memory_pss_mb")
        self.assertEqual((change["baseline"], change["current"], change["delta"], change["delta_percent"]), (60, 66, 6, 10))
        value = dispatch("profile.compare", {"baseline": str(baseline), "current": str(current),
                                             "budgets": {"memory_pss_mb": {"increase_percent": 5}}})
        self.assertEqual((value["passed"], value["results"][0]["increase_percent"]), (False, 10))
        startup = self.root / "startup.json"
        startup.write_text(json.dumps({"bundle_id": "dev.test.app", "kind": "startup", "metrics": {}}))
        with self.assertRaises(StudioError) as mismatch:
            dispatch("profile.compare", {"baseline": str(startup), "current": str(current)})
        self.assertEqual(mismatch.exception.code, "profile_mismatch")

    def test_a_scenario_measures_the_app_and_fails_outside_its_budget(self):
        self.replies(**{"resolve-activity": "dev.test.app/.Main\n", "am start -W": self.COLD_START,
                        "gfxinfo dev.test.app reset": "", "gfxinfo dev.test.app": self.GFXINFO})
        scenario = self.root / "scroll.json"
        steps = [{"action": "measure", "measure": "startup", "bundle_id": "dev.test.app", "runs": 1,
                  "budget": {"startup_cold_median_ms": {"maximum": 500}}},
                 {"action": "measure", "measure": "frames_reset", "bundle_id": "dev.test.app"},
                 {"action": "swipe", "x": 500, "y": 1800, "end_x": 500, "end_y": 400},
                 {"action": "measure", "measure": "frames", "bundle_id": "dev.test.app"}]
        scenario.write_text(json.dumps({"name": "Scroll", "steps": steps}))
        budget = self.root / "budget.json"
        budget.write_text(json.dumps({"jank_percent": {"maximum": 10}, "frames_total": {"minimum": 60}}))
        run = lambda directory, **extra: jobs.wait(dispatch("testing.run", {"path": str(scenario), "directory": str(self.root / directory),
                                                                            "devices": ["emulator-5554"], **extra})["id"], 60 * PATIENCE)
        self.assertEqual(run("within", budget=str(budget))["state"], "succeeded")
        result = json.loads((self.root / "within/report.json").read_text())["results"][0]
        self.assertEqual((result["metrics"]["startup_cold_median_ms"], result["metrics"]["jank_percent"]), (412, 5))
        self.assertTrue(result["budget"]["passed"])
        self.assertTrue(result["steps"][0]["result"]["budget"][0]["passed"])
        # The scenario's budget fails the run; the step's own budget passed.
        self.assertEqual(run("outside", budgets={"jank_percent": {"maximum": 2}})["state"], "failed")
        result = json.loads((self.root / "outside/report.json").read_text())["results"][0]
        self.assertEqual((result["passed"], result["error"]["code"]), (False, "budget_exceeded"))
        self.assertEqual([r["metric"] for r in result["budget"]["results"] if not r["passed"]], ["jank_percent"])

    def test_app_logs_follow_the_user_id_from_android_12_and_the_pid_before(self):
        self.replies(**{"ro.build.version.sdk": "36\n", "list packages -U": "package:dev.test.app.extra uid:10201\npackage:dev.test.app uid:10200\n"})
        job = dispatch("device.logs", {"platform": "android", "device": "emulator-5554", "bundle_id": "dev.test.app", "duration": 1})
        self.assertEqual(job["plan"]["steps"][0]["argv"][-3:], ["-v", "threadtime", "--uid=10200"])
        jobs.wait(job["id"], 5)
        self.replies(**{"ro.build.version.sdk": "36\n", "list packages -U": ""})
        with self.assertRaises(StudioError) as missing:
            dispatch("device.logs", {"platform": "android", "device": "emulator-5554", "bundle_id": "dev.test.app"})
        self.assertEqual(missing.exception.code, "app_not_installed")
        self.replies(**{"ro.build.version.sdk": "29\n", "pidof": "4242\n"})
        job = dispatch("device.logs", {"platform": "android", "device": "emulator-5554", "bundle_id": "dev.test.app", "duration": 1, "filter": "*:E"})
        self.assertEqual(job["plan"]["steps"][0]["argv"][-3:], ["--pid", "4242", "*:E"])
        jobs.wait(job["id"], 5)
        self.replies(**{"ro.build.version.sdk": "29\n"})
        with self.assertRaises(StudioError) as stopped:
            dispatch("device.logs", {"platform": "android", "device": "emulator-5554", "bundle_id": "dev.test.app"})
        self.assertEqual(stopped.exception.code, "app_not_running")

    def test_device_info_sums_up_health_in_one_read_only_call(self):
        self.replies(**{"getprop": "[ro.build.version.release]: [16]\n[ro.build.version.sdk]: [36]\n[ro.build.version.security_patch]: [2026-09-05]\n[ro.product.model]: [Pixel 9]\n",
                        "wm size": "Physical size: 1080x2400\n", "wm density": "Physical density: 420\nOverride density: 480\n",
                        "dumpsys battery": "Current Battery Service state:\n  AC powered: true\n  status: 3\n  health: 2\n  present: true\n  level: 42\n  scale: 100\n  temperature: 312\n",
                        "df -k": "Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/block/dm-5 6000000 2000000 4000000 34% /data\n",
                        "/proc/uptime": "3600.25 7000.00\n"})
        health = dispatch("device.info", {"platform": "android", "device": "emulator-5554"})["health"]
        self.assertEqual(health["android"], {"version": "16", "sdk": 36, "security_patch": "2026-09-05", "build": None})
        self.assertEqual((health["battery"]["level"], health["battery"]["status"], health["battery"]["temperature_celsius"], health["battery"]["plugged"]),
                         (42, "discharging", 31.2, ["ac"]))
        self.assertEqual(health["storage"]["free_bytes"], 4000000 * 1024)
        self.assertEqual(health["screen"], {"size": [1080, 2400], "density": 480, "size_dp": [360, 800], "physical_size": [1080, 2400], "physical_density": 420})
        self.assertEqual(health["uptime_seconds"], 3600)
        self.assertEqual(health["model"], "Pixel 9")
        # Only reads: nothing is set, and a part the device does not answer is null.
        self.assertFalse(any(args[:2] in (["settings", "put"],) or args[:1] == ["wm"] and len(args) > 2 for args in self.shell_calls()))
        self.replies(**{"getprop": "[ro.build.version.sdk]: [30]\n", "wm size": "Physical size: 720x1280\n", "wm density": "Physical density: 320\n",
                        "dumpsys battery": "FAIL:Can't find service: battery\n", "df -k": "FAIL:df: denied\n", "/proc/uptime": "FAIL:no\n"})
        health = dispatch("device.info", {"platform": "android", "device": "emulator-5554"})["health"]
        self.assertEqual((health["battery"], health["storage"], health["uptime_seconds"], health["android"]["version"]), (None, None, None, None))
        self.assertEqual(health["screen"]["size_dp"], [360, 640])

    def test_nicknames_and_pins_show_in_the_device_list(self):
        # The emulator's serial changes with its port; its AVD name does not.
        self.tool("adb", "import json,os,sys\nfrom pathlib import Path\nroot=Path(os.environ['TOMACCO_TEST_ROOT'])\na=sys.argv[1:]\n"
                         "port=(root/'port').read_text() if (root/'port').exists() else '5554'\n"
                         "if a[:1]==['devices']: print(f'List of devices attached\\nR5CT device model:SM_S928B\\nemulator-{port} device model:sdk_gphone\\n')\n"
                         "elif a[2:]==['emu','avd','name']: print('Small_Phone\\nOK')\n"
                         "elif 'boot_completed' in ' '.join(a): print('1')\n")
        listed = lambda: dispatch("device.list", {"platform": "android"})["devices"]
        self.assertEqual([(d["id"], d["pinned"]) for d in listed()], [("R5CT", False), ("emulator-5554", False)])
        self.assertNotIn("nickname", listed()[0])
        value = dispatch("device.label", {"device": "emulator-5554", "nickname": "  Small test phone ", "pinned": True})
        self.assertEqual((value["key"], value["nickname"], value["pinned"]), ("avd:Small_Phone", "Small test phone", True))
        dispatch("device.label", {"device": "R5CT", "nickname": "Galaxy"})
        (self.root / "port").write_text("5556")
        devices = listed()
        # Pinned first, under its nickname, whatever port the emulator has now.
        self.assertEqual([(d["id"], d["name"], d.get("nickname"), d["pinned"]) for d in devices],
                         [("emulator-5556", "Small test phone", "Small test phone", True), ("R5CT", "Galaxy", "Galaxy", False)])
        self.assertEqual(devices[1]["original_name"], "SM S928B")
        # Reading changes nothing; an empty nickname and pinned false remove the label.
        self.assertEqual(dispatch("device.label", {"device": "R5CT"})["nickname"], "Galaxy")
        dispatch("device.label", {"device": "emulator-5556", "nickname": "", "pinned": False})
        self.assertEqual(dispatch("device.label", {"device": "R5CT"})["labels"], {"R5CT": {"nickname": "Galaxy"}})
        self.assertEqual([d["name"] for d in listed()], ["Galaxy", "Small_Phone"])
        # An emulator that is off is named by its AVD.
        self.assertEqual(dispatch("device.label", {"device": "avd:Small_Phone", "pinned": True})["key"], "avd:Small_Phone")
        self.assertEqual([d["id"] for d in listed() if d["pinned"]], ["emulator-5556"])
        with self.assertRaises(StudioError):
            dispatch("device.label", {"device": "avd:../x", "pinned": True})
        for arguments in ({"device": "R5CT", "nickname": "two\nlines"}, {"device": "R5CT", "nickname": "x" * 41}, {"device": "a;b", "pinned": True}):
            with self.assertRaises(StudioError) as refused:
                dispatch("device.label", arguments)
            self.assertEqual(refused.exception.code, "invalid_argument", arguments)

    def test_wireless_pairing_by_qr_code_pairs_and_connects_once_the_phone_scanned_it(self):
        code = dispatch("android.pair", {"path": str(self.root / "pair.png")})
        self.assertRegex(code["qr_text"], r"^WIFI:T:ADB;S:tomacco-[a-z0-9]{8};P:[a-z0-9]{10};;$")
        self.assertEqual(code["qr_text"], f"WIFI:T:ADB;S:{code['service']};P:{code['password']};;")
        self.assertEqual(len(code["qr"]["rows"]), code["qr"]["size"])
        self.assertTrue(code["svg"].startswith("<svg"))
        self.assertEqual((self.root / "pair.png").read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        zbar = shutil.which("zbarimg")
        if zbar:
            decoded = subprocess.run([zbar, "-q", "--raw", str(self.root / "pair.png")], capture_output=True, text=True)
            self.assertEqual(decoded.stdout.strip(), code["qr_text"])
        with self.assertRaises(StudioError) as named:
            dispatch("android.pair", {"path": str(self.root / "pair.jpg")})
        self.assertEqual(named.exception.code, "invalid_path")
        # The phone offers the service once it scanned the code, then its connection service.
        service, password = code["service"], code["password"]
        self.tool("adb", "import json,os,sys\nfrom pathlib import Path\nroot=Path(os.environ['TOMACCO_TEST_ROOT'])\na=sys.argv[1:]\n"
                         "with (root/'adb-calls.jsonl').open('a') as f: f.write(json.dumps(a)+'\\n')\n"
                         "scanned=(root/'scanned').exists()\npaired=(root/'paired').exists()\n"
                         "if a==['mdns','services']:\n"
                         "    print('List of discovered mdns services')\n"
                         f"    if scanned: print('{service}\\t_adb-tls-pairing._tcp.\\t192.168.1.20:41235')\n"
                         "    if paired: print('adb-R5CT-AbCd\\t_adb-tls-connect._tcp.\\t192.168.1.20:37099')\n"
                         f"elif a[:1]==['pair']:\n    ok=a[1:]==['192.168.1.20:41235','{password}']\n    (root/'paired').touch() if ok else None\n"
                         "    print('Successfully paired to 192.168.1.20:41235 [guid=adb-R5CT-AbCd]' if ok else 'Failed: Wrong password or connection was dropped.')\n"
                         "elif a[:1]==['connect']: print('connected to '+a[1])\n")
        with self.assertRaises(StudioError) as early:
            dispatch("android.pair", {"action": "wait", "service": service, "password": password, "timeout": 1})
        self.assertEqual(early.exception.code, "pairing_timeout")
        (self.root / "scanned").touch()
        self.assertEqual([s["name"] for s in dispatch("android.pair", {"action": "services"})["services"]], [service])
        value = dispatch("android.pair", {"action": "wait", "service": service, "password": password, "timeout": 5})
        self.assertEqual(value, {"paired": True, "address": "192.168.1.20:41235", "guid": "adb-R5CT-AbCd", "connected": True, "device": "192.168.1.20:37099"})
        calls = [json.loads(line) for line in (self.root / "adb-calls.jsonl").read_text().splitlines()]
        self.assertIn(["connect", "192.168.1.20:37099"], calls)
        (self.root / "paired").unlink()
        with self.assertRaises(StudioError) as wrong:
            dispatch("android.pair", {"action": "wait", "service": service, "password": "wrongpass1", "timeout": 2})
        self.assertEqual(wrong.exception.code, "pairing_failed")
        for arguments in ({"action": "wait", "service": "a;b", "password": password}, {"action": "wait", "service": service, "password": "x"}):
            with self.assertRaises(StudioError) as refused:
                dispatch("android.pair", arguments)
            self.assertEqual(refused.exception.code, "invalid_argument")
        self.tool("adb", "import sys\nprint('unknown command mdns', file=sys.stderr); sys.exit(1)\n")
        with self.assertRaises(StudioError) as old:
            dispatch("android.pair", {"action": "services"})
        self.assertEqual(old.exception.code, "mdns_unavailable")

    def test_a_recording_stops_early_on_request_and_keeps_the_video(self):
        # screenrecord runs until pkill interrupts it; pull writes the file.
        self.tool("adb", "import json,os,sys,time\nfrom pathlib import Path\nroot=Path(os.environ['TOMACCO_TEST_ROOT'])\na=sys.argv[1:]\n"
                         "with (root/'adb-calls.jsonl').open('a') as f: f.write(json.dumps(a)+'\\n')\n"
                         "command=' '.join(a[2:])\n"
                         "if 'screenrecord --time-limit' in command:\n"
                         "    for _ in range(600):\n"
                         "        if (root/'interrupted').exists(): sys.exit(0)\n"
                         "        time.sleep(.05)\n"
                         "    sys.exit(3)\n"
                         "elif 'pkill' in command: (root/'interrupted').touch()\n"
                         "elif a[2:3]==['pull']: Path(a[4]).write_bytes(b'mp4 video')\n")
        target = self.root / "videos/clip.mp4"
        job = dispatch("android.record", {"device": "emulator-5554", "path": str(target), "seconds": 180})
        self.assertTrue(job["plan"]["steps"][0]["stoppable"])
        with self.assertRaises(StudioError) as early:
            dispatch("job.send", {"job_id": uuid.uuid4().hex, "command": "stop"})
        deadline = time.monotonic() + 10 * PATIENCE
        while jobs.get(job["id"])["state"] != "running":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.05)
        self.assertEqual(dispatch("job.send", {"job_id": job["id"], "command": "stop"})["sent"], "stop")
        done = jobs.wait(job["id"], 30)
        self.assertEqual(done["state"], "succeeded", jobs.logs(job["id"])["text"])
        self.assertEqual(target.read_bytes(), b"mp4 video")
        self.assertIn(str(target), [a["path"] for a in done["result"]["artifacts"]])
        pkill = next(args for args in self.shell_calls() if args[:1] == ["pkill"])
        self.assertEqual(pkill[:3], ["pkill", "-INT", "-f"])
        self.assertRegex(pkill[3], r"^screenrecor\[d\]\.\*[0-9a-f]{32}$")
        self.assertIn("[stop] ending early on request", jobs.logs(job["id"])["text"])
        # Only a running recording can be stopped this way.
        with self.assertRaises(StudioError) as finished:
            dispatch("job.send", {"job_id": job["id"], "command": "stop"})
        self.assertEqual(finished.exception.code, "not_stoppable")

    def test_a_screen_preview_is_saved_at_once_without_a_job(self):
        png = b"\x89PNG\r\n\x1a\n\0\0\0\x0dIHDR" + (1080).to_bytes(4, "big") + (2400).to_bytes(4, "big") + b"\x08\x02\0\0\0rest"
        (self.root / "screen.png").write_bytes(png)
        self.tool("adb", "import sys\nfrom pathlib import Path\n"
                         f"if sys.argv[3:]==['exec-out','screencap','-p']: sys.stdout.buffer.write(Path({str(self.root / 'screen.png')!r}).read_bytes())\n"
                         "else: sys.exit(1)\n")
        before = len(jobs.listing(200))
        target = self.root / "thumbs/emulator-5554.png"
        value = dispatch("android.screen", {"device": "emulator-5554", "path": str(target)})
        self.assertEqual((value["width"], value["height"], value["bytes"]), (1080, 2400, len(png)))
        self.assertEqual(target.read_bytes(), png)
        self.assertEqual(len(jobs.listing(200)), before)
        with self.assertRaises(StudioError) as named:
            dispatch("android.screen", {"device": "emulator-5554", "path": str(self.root / "x.jpg")})
        self.assertEqual(named.exception.code, "invalid_path")
        self.tool("adb", "print('not a picture')\n")
        with self.assertRaises(StudioError) as broken:
            dispatch("android.screen", {"device": "emulator-5554", "path": str(target)})
        self.assertEqual(broken.exception.code, "invalid_screenshot")
        self.assertEqual(target.read_bytes(), png)

    def test_device_settings_apply_only_what_is_asked_and_read_everything_back(self):
        state = {"ro.build.version.sdk": "36\n", "uimode night": "Night mode: yes\n", "get system font_scale": "1.3\n",
                 "get global window_animation_scale": "0\n", "get global transition_animation_scale": "0.0\n",
                 "get global animator_duration_scale": "0\n", "get global airplane_mode_on": "1\n",
                 "get-app-locales": "Locales for dev.test.app for user 0 are [de-DE]\n"}
        self.replies(**state)
        value = dispatch("android.settings", {"device": "emulator-5554", "dark_mode": "on", "font_scale": 1.3, "animations": False,
                                              "airplane_mode": True, "bundle_id": "dev.test.app", "app_locale": "de-de"})
        self.assertEqual(value["changed"], ["dark_mode", "font_scale", "animations", "airplane_mode", "app_locale"])
        self.assertEqual((value["dark_mode"], value["font_scale"], value["animations"], value["airplane_mode"], value["app_locale"]),
                         ("on", 1.3, False, True, "de-DE"))
        shell = self.shell_calls()
        for command in (["cmd", "uimode", "night", "yes"], ["settings", "put", "system", "font_scale", "1.3"],
                        ["settings", "put", "global", "animator_duration_scale", "0"], ["cmd", "connectivity", "airplane-mode", "enable"],
                        ["cmd", "locale", "set-app-locales", "dev.test.app", "--locales", "de-de"]):
            self.assertIn(command, shell)
        # Nothing asked: nothing written, everything read.
        (self.root / "adb-calls.jsonl").unlink()
        value = dispatch("android.settings", {"device": "emulator-5554"})
        self.assertEqual(value["changed"], [])
        self.assertFalse(any(args[:2] in (["settings", "put"], ["cmd", "connectivity"]) or args[-1] in ("yes", "no", "auto") for args in self.shell_calls()))
        # An empty language resets the app to the device language.
        state["get-app-locales"] = "Locales for dev.test.app for user 0 are []\n"
        self.replies(**state)
        dispatch("android.settings", {"device": "emulator-5554", "bundle_id": "dev.test.app", "app_locale": ""})
        self.assertIn(["cmd", "locale", "set-app-locales", "dev.test.app"], self.shell_calls())
        # A value Android ignored is an error, not a silent success.
        with self.assertRaises(StudioError) as ignored:
            dispatch("android.settings", {"device": "emulator-5554", "dark_mode": "off"})
        self.assertEqual(ignored.exception.code, "setting_not_applied")
        self.assertEqual(ignored.exception.details["dark_mode"], "on")

    def test_device_settings_refuse_what_the_android_version_cannot_do_before_changing_anything(self):
        self.replies(**{"ro.build.version.sdk": "29\n"})
        for arguments, code in [({"airplane_mode": True}, "setting_unsupported"), ({"bundle_id": "dev.test.app", "app_locale": "de"}, "setting_unsupported"),
                                ({"app_locale": "de"}, "invalid_argument"), ({"bundle_id": "dev.test.app", "app_locale": "de;reboot"}, "invalid_argument")]:
            with self.assertRaises(StudioError) as refused:
                dispatch("android.settings", {"device": "emulator-5554", "dark_mode": "on", **arguments})
            self.assertEqual(refused.exception.code, code, arguments)
        self.assertFalse(any("uimode" in args for args in self.shell_calls()))
        with self.assertRaises(StudioError):
            dispatch("android.settings", {"device": "emulator-5554", "font_scale": 3})

    # A device that remembers what adb set on it: settings, dark mode and app
    # languages per serial; screencap answers a PNG header of its screen size.
    STATEFUL_ADB = """import json,os,shlex,struct,sys
from pathlib import Path
root=Path(os.environ['TOMACCO_TEST_ROOT']);a=sys.argv[1:]
with (root/'adb-calls.jsonl').open('a') as f: f.write(json.dumps(a)+'\\n')
serial,rest=(a[1],a[2:]) if a[:1]==['-s'] else ('',a)
file=root/f'adb-state-{serial}.json';mine=json.loads(file.read_text()) if file.exists() else {}
w,h,d={'tablet-1':(1600,2560,320)}.get(serial,(1080,2160,420))
if rest[:1]==['exec-out']:
    sys.stdout.buffer.write(b'\\x89PNG\\r\\n\\x1a\\n'+struct.pack('>I',13)+b'IHDR'+struct.pack('>II',w,h));sys.exit(0)
c=shlex.split(rest[1]) if rest[:1]==['shell'] and len(rest)>1 else rest
out=''
defaults={'global/airplane_mode_on':'0','system/font_scale':'1.0','system/accelerometer_rotation':'1','system/user_rotation':'0'}
if c[:2]==['getprop','ro.build.version.sdk']: out='34'
elif c[:2]==['wm','size']: out=f'Physical size: {w}x{h}'
elif c[:2]==['wm','density']: out=f'Physical density: {d}'
elif c[:2]==['settings','get']: out=mine.get(c[2]+'/'+c[3],defaults.get(c[2]+'/'+c[3],'1'))
elif c[:2]==['settings','put']: mine[c[2]+'/'+c[3]]=c[4]
elif c[:3]==['cmd','uimode','night']:
    if len(c)>3: mine['night']=c[3]
    out='Night mode: '+mine.get('night','no')
elif c[:3]==['cmd','locale','set-app-locales']: mine['locale']=c[5] if len(c)>5 else ''
elif c[:3]==['cmd','locale','get-app-locales']: out='Locales for %s for user 0 are [%s]'%(c[3],mine.get('locale',''))
elif 'resolve-activity' in ' '.join(c): out='dev.test.app/.Main'
temp=file.with_suffix('.tmp');temp.write_text(json.dumps(mine));temp.replace(file);print(out)
"""

    def test_store_screenshots_cover_devices_languages_and_themes_and_put_settings_back(self):
        self.tool("adb", self.STATEFUL_ADB)
        out = self.root / "shots"
        screens = [{"name": "home"}, {"name": "detail", "steps": [{"action": "key", "key": "KEYCODE_DPAD_DOWN"}]}]
        base = {"bundle_id": "dev.test.app", "devices": ["emulator-5554", "tablet-1"], "locales": ["en-US", "de-DE"],
                "themes": ["light", "dark"], "screens": screens, "directory": str(out), "settle_ms": 0}
        job = dispatch("android.store_screenshots", base)
        self.assertEqual(job["label"], "Store screenshots · 16")
        done = jobs.wait(job["id"], 120)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual(done["state"], "succeeded", report)
        self.assertEqual((report["ok"], report["play_problems"]), (True, 0))
        for theme in ("light", "dark"):
            for locale in ("en-US", "de-DE"):
                for kind in ("phoneScreenshots", "tenInchScreenshots"):
                    for name in ("01_home.png", "02_detail.png"):
                        self.assertTrue((out / theme / locale / "images" / kind / name).is_file(), (theme, locale, kind, name))
        phone = next(d for d in report["devices"] if d["device"] == "emulator-5554")
        self.assertEqual((phone["kind"], phone["captured"], phone["settings_restored"]), ("phoneScreenshots", 8, True))
        self.assertEqual((phone["screenshots"][0]["width"], phone["screenshots"][0]["height"]), (1080, 2160))
        shell = [shlex.split(c[3]) for c in self.calls_with_serial() if c[:2] == ["-s", "emulator-5554"] and c[2] == "shell"]
        self.assertIn(["cmd", "locale", "set-app-locales", "dev.test.app", "--locales", "de-DE"], shell)
        self.assertIn(["cmd", "uimode", "night", "yes"], shell)
        self.assertIn(["input", "keyevent", "KEYCODE_DPAD_DOWN"], shell)
        state = json.loads((self.root / "adb-state-emulator-5554.json").read_text())
        self.assertEqual((state["night"], state["locale"], state["global/window_animation_scale"]), ("no", "", "1"))
        # Refused before anything runs: a non-empty folder, two phones.
        with self.assertRaises(StudioError) as refused:
            dispatch("android.store_screenshots", base)
        self.assertEqual(refused.exception.code, "path_exists")
        with self.assertRaises(StudioError) as refused:
            dispatch("android.store_screenshots", {**base, "devices": ["emulator-5554", "emulator-5556"], "directory": str(self.root / "two")})
        self.assertEqual(refused.exception.code, "invalid_argument")

    def calls_with_serial(self):
        return [json.loads(line) for line in (self.root / "adb-calls.jsonl").read_text().splitlines()]

    def test_display_orientation_size_and_density_are_set_and_read_back(self):
        state = {"wm size": "Physical size: 1080x2400\nOverride size: 720x1600\n", "wm density": "Physical density: 420\nOverride density: 320\n",
                 "accelerometer_rotation": "0\n", "user_rotation": "1\n"}
        self.replies(**state)
        display = lambda **a: dispatch("android.display", {"device": "emulator-5554", **a})
        value = display(orientation="landscape", width=720, height=1600, density=320)
        self.assertEqual((value["size"], value["density"], value["orientation"], value["changed"]),
                         ({"physical": [1080, 2400], "override": [720, 1600]}, {"physical": 420, "override": 320}, "landscape",
                          ["size", "density", "orientation"]))
        shell = self.shell_calls()
        for command in (["wm", "size", "720x1600"], ["wm", "density", "320"], ["settings", "put", "system", "accelerometer_rotation", "0"],
                        ["settings", "put", "system", "user_rotation", "1"]):
            self.assertIn(command, shell)
        (self.root / "adb-calls.jsonl").unlink()
        self.assertEqual(display()["changed"], [])
        self.assertFalse(any(args[:2] == ["settings", "put"] or (args[0] == "wm" and args[2:]) for args in self.shell_calls()))
        state.update({"wm size": "Physical size: 1080x2400\n", "wm density": "Physical density: 420\n", "accelerometer_rotation": "1\n"})
        self.replies(**state)
        value = display(reset=True)
        self.assertEqual((value["size"]["override"], value["density"]["override"], value["orientation"]), (None, None, "auto"))
        for command in (["wm", "size", "reset"], ["wm", "density", "reset"], ["settings", "put", "system", "accelerometer_rotation", "1"]):
            self.assertIn(command, self.shell_calls())
        with self.assertRaises(StudioError) as ignored:
            display(orientation="portrait")
        self.assertEqual(ignored.exception.code, "setting_not_applied")
        for arguments in ({"width": 720}, {"density": 10}, {"orientation": "sideways"}):
            with self.assertRaises(StudioError): display(**arguments)

    NOTIFICATIONS = ("Current Notification Manager state:\n  Notification List:\n"
                     "    NotificationRecord(0x01d08d09: pkg=dev.test.app user=UserHandle{0} id=7 tag=null importance=3 key=0|dev.test.app|7|null|10200: "
                     "Notification(channel=orders shortcut=null contentView=null vibrate=null sound=null defaults=0 flags=AUTO_CANCEL color=0x00000000 vis=PRIVATE))\n"
                     "      notification=\n        extras={\n            android.title=String (Order shipped)\n            android.text=String (Arrives Monday)\n        }\n"
                     "    NotificationRecord(0x02a82fca: pkg=android user=UserHandle{-1} id=19 tag=null importance=2 key=-1|android|19|null|1000: "
                     "Notification(channel=DEVELOPER shortcut=null defaults=0 flags=ONGOING_EVENT color=0x00000000 vis=PUBLIC))\n"
                     "  Snoozed notifications:\n")

    def test_notifications_are_listed_the_shade_opened_and_one_snoozed(self):
        self.replies(**{"dumpsys notification": self.NOTIFICATIONS, "snooze": "snoozing <-1|android|19|null|1000> until time: later\n"})
        notifications = lambda **a: dispatch("android.notifications", {"device": "emulator-5554", **a})
        listed = notifications()["notifications"]
        self.assertEqual([(n["package"], n.get("title"), n.get("text"), n["ongoing"]) for n in listed],
                         [("dev.test.app", "Order shipped", "Arrives Monday", False), ("android", None, None, True)])
        self.assertEqual([n["key"] for n in notifications(bundle_id="dev.test.app")["notifications"]], ["0|dev.test.app|7|null|10200"])
        notifications(action="open")
        notifications(action="close")
        self.assertIn(["cmd", "statusbar", "expand-notifications"], self.shell_calls())
        self.assertIn(["cmd", "statusbar", "collapse"], self.shell_calls())
        # The fake still lists the key afterwards, so the snooze did not hide it.
        with self.assertRaises(StudioError) as shown:
            notifications(action="snooze", key="0|dev.test.app|7|null|10200", seconds=60)
        self.assertEqual(shown.exception.code, "notification_not_snoozed")
        self.assertIn(["cmd", "notification", "snooze", "--for", "60000", "0|dev.test.app|7|null|10200"], self.shell_calls())
        self.replies(**{"dumpsys notification": self.NOTIFICATIONS.split("    NotificationRecord(0x02a8")[0] + "  Snoozed notifications:\n",
                        "snooze": "snoozing <-1|android|19|null|1000> until time: later\n"})
        self.assertEqual(len(notifications(action="snooze", key="-1|android|19|null|1000")["notifications"]), 1)
        for arguments in ({"action": "snooze"}, {"action": "snooze", "key": "not-a-key"}, {"bundle_id": "x;y"}):
            with self.assertRaises(StudioError): notifications(**arguments)

    def test_a_debuggable_apps_files_are_listed_and_pulled_as_a_tar_archive(self):
        files = lambda **a: dispatch("android.app_files", {"device": "emulator-5554", "bundle_id": "dev.test.app", **a})
        self.replies(**{"stat -c": "6 1793474273 ./files/a b.txt\n120 1793474000 ./shared_prefs/settings.xml\n", "tar -cf": "x" * 2048})
        self.assertEqual(files()["files"], [{"path": "files/a b.txt", "size": 6, "modified": 1793474273},
                                            {"path": "shared_prefs/settings.xml", "size": 120, "modified": 1793474000}])
        self.assertIn(["run-as", "dev.test.app", "find", ".", "-type", "f", "-exec", "stat", "-c", "%s %Y %n", "{}", "+"], self.shell_calls())
        target = self.root / "out/files.tar"
        job = files(action="pull", path="files", output=str(target))
        self.assertEqual(jobs.wait(job["id"], 5)["state"], "succeeded")
        self.assertEqual(target.read_text(), "x" * 2048)
        self.assertEqual(job["plan"]["steps"][0]["argv"][-2:], ["exec-out", "run-as dev.test.app tar -cf - files 2>/dev/null"])
        with self.assertRaises(StudioError) as named:
            files(action="pull", output=str(self.root / "files.zip"))
        self.assertEqual(named.exception.code, "invalid_path")
        for path in ("/data/data/dev.other", "../dev.other", "a;reboot"):
            with self.assertRaises(StudioError): files(path=path)
        self.replies(**{"ls -d": "FAIL:ls: nosuch: No such file or directory\n"})
        with self.assertRaises(StudioError) as missing: files(path="nosuch")
        self.assertEqual(missing.exception.code, "path_not_found")
        self.replies(**{"run-as": "FAIL:run-as: package not debuggable: dev.test.app\n"})
        with self.assertRaises(StudioError) as refused: files()
        self.assertEqual(refused.exception.code, "app_not_debuggable")

    def test_steps_run_on_every_device_as_one_job_with_a_result_per_device(self):
        # R5CT has no launcher activity for the app; emulator-5554 does.
        self.tool("adb", "import json,os,sys\nfrom pathlib import Path\nroot=Path(os.environ['TOMACCO_TEST_ROOT'])\na=sys.argv[1:]\n"
                         "with (root/'adb-calls.jsonl').open('a') as f: f.write(json.dumps(a)+'\\n')\n"
                         "if a[:1]==['devices']: print('List of devices attached\\nemulator-5554 device product:x model:A\\nR5CT device model:B\\nOLD1 offline\\n')\n"
                         "elif 'resolve-activity' in a[-1]: print('dev.test.app/.Main' if a[1]=='emulator-5554' else 'No activity found')\n")
        apk = self.root / "app.apk"
        apk.touch()
        steps = [{"operation": "device.install", "arguments": {"paths": [str(apk)]}},
                 {"operation": "android.launch", "arguments": {"bundle_id": "dev.test.app"}},
                 {"operation": "device.app", "arguments": {"bundle_id": "dev.test.app", "action": "stop"}}]
        job = dispatch("android.each", {"steps": steps})
        self.assertEqual(job["label"], "Run on 2 Android devices")
        done = jobs.wait(job["id"], 30)
        self.assertEqual(done["state"], "failed")
        report = json.loads(Path(next(a["path"] for a in done["result"]["artifacts"] if a["path"].endswith("report.json"))).read_text())
        self.assertEqual(report["failed"], ["R5CT"])
        by_device = {d["device"]: d for d in report["devices"]}
        self.assertEqual([(s["operation"], s["ok"]) for s in by_device["emulator-5554"]["steps"]],
                         [("device.install", True), ("android.launch", True), ("device.app", True)])
        self.assertEqual(by_device["emulator-5554"]["steps"][0]["state"], "succeeded")
        self.assertEqual([(s["operation"], s["ok"]) for s in by_device["R5CT"]["steps"]], [("device.install", True), ("android.launch", False)])
        self.assertEqual(by_device["R5CT"]["steps"][1]["error"]["code"], "launcher_missing")
        installs = [call[:2] for call in map(json.loads, (self.root / "adb-calls.jsonl").read_text().splitlines()) if "install" in call]
        self.assertEqual(sorted(installs), [["-s", "R5CT"], ["-s", "emulator-5554"]])
        # Named devices only, and every step checked before anything runs.
        job = dispatch("android.each", {"steps": steps[:2], "devices": ["emulator-5554"]})
        self.assertEqual(jobs.wait(job["id"], 30)["state"], "succeeded")
        for arguments, code in [({"steps": [{"operation": "android.launch", "arguments": {"device": "x", "bundle_id": "dev.test.app"}}]}, "invalid_argument"),
                                ({"steps": [{"operation": "android.launch", "arguments": {}}]}, "invalid_argument"),
                                ({"steps": [{"operation": "android.emulator", "arguments": {}}]}, "invalid_argument"),
                                ({"steps": steps, "devices": ["x;reboot"]}, "invalid_argument")]:
            with self.assertRaises(StudioError) as refused:
                dispatch("android.each", arguments)
            self.assertEqual(refused.exception.code, code, arguments)
        self.tool("adb", "print('List of devices attached\\n')\n")
        with self.assertRaises(StudioError) as none:
            dispatch("android.each", {"steps": steps})
        self.assertEqual(none.exception.code, "no_devices")

    def test_emulator_snapshots_battery_network_and_location_go_through_the_console(self):
        self.replies(**{"avd snapshot list": "List of snapshots present on all disks:\nID        TAG                 VM SIZE                DATE       VM CLOCK\n--        before-login        98M 2026-10-31 19:42:12   00:03:00.001\nOK\n",
                        "power display": "AC: offline\nstatus: Discharging\nhealth: Good\npresent: true\ncapacity: 15\nOK\n",
                        "network status": "Current network status:\n  minimum latency:  150 ms\n  maximum latency:  550 ms\nOK\n",
                        "emu": "OK\n"})
        emulator = lambda **a: dispatch("android.emulator", {"device": "emulator-5554", **a})
        self.assertEqual(emulator(action="snapshot-list")["snapshots"], ["before-login"])
        emulator(action="snapshot-save", name="before-login")
        emulator(action="snapshot-load", name="before-login")
        emulator(action="snapshot-delete", name="before-login")
        battery = emulator(action="battery", battery_level=15, charging=False)
        self.assertEqual(battery["battery"]["capacity"], "15")
        network = emulator(action="network", network_speed="edge", network_delay="gprs")
        self.assertEqual(network["network"]["minimum_latency"], "150 ms")
        emulator(action="location", longitude=16.37, latitude=48.21)
        consoles = [call[1:] for call in self.calls() if call[:1] == ["emu"]]
        self.assertEqual(consoles, [["avd", "snapshot", "list"], ["avd", "snapshot", "save", "before-login"],
                                    ["avd", "snapshot", "load", "before-login"], ["avd", "snapshot", "del", "before-login"],
                                    ["power", "capacity", "15"], ["power", "ac", "off"], ["power", "status", "discharging"], ["power", "display"],
                                    ["network", "speed", "edge"], ["network", "delay", "150:550"], ["network", "status"],
                                    ["geo", "fix", "16.37", "48.21"]])
        for arguments in ({"action": "battery", "battery_level": 101}, {"action": "network", "network_speed": "warp"},
                          {"action": "snapshot-save", "name": "../x"}):
            with self.assertRaises(StudioError): emulator(**arguments)
        with self.assertRaises(StudioError) as phone:
            dispatch("android.emulator", {"device": "R5CT1234", "action": "snapshot-list"})
        self.assertEqual(phone.exception.code, "not_emulator")

    def test_a_refused_console_command_is_an_error_not_a_success(self):
        # adb emu exits with success when the console answers KO.
        self.replies(**{"avd snapshot load": 'KO: Snapshot load is disabled because "-read-only" was specified\n'})
        with self.assertRaises(StudioError) as refused:
            dispatch("android.emulator", {"device": "emulator-5554", "action": "snapshot-load", "name": "missing"})
        self.assertEqual(refused.exception.code, "emulator_refused")
        self.assertIn("-read-only", str(refused.exception))

class AndroidPackageTests(Isolated):
    """android.package_inspect reads a package with stand-ins for the SDK tools."""
    @staticmethod
    def field(number, payload):
        def varint(value):
            out = bytearray()
            while True:
                out.append((value & 0x7F) | (0x80 if value > 0x7F else 0))
                value >>= 7
                if not value: return bytes(out)
        return varint(number << 3 | 2) + varint(len(payload)) + payload

    def element(self, name, attributes=(), children=()):
        body = self.field(3, name.encode())
        for key, value in attributes:
            body += self.field(4, self.field(2, key.encode()) + self.field(3, value.encode()))
        for child in children:
            body += self.field(5, child)
        return self.field(1, body)

    def test_an_apk_is_read_through_aapt2_and_apksigner(self):
        apk = self.root / "app.apk"
        import zipfile
        with zipfile.ZipFile(apk, "w") as archive:
            archive.writestr("AndroidManifest.xml", b"")
            archive.writestr("lib/x86_64/libcore.so", b"x")
        self.tool("aapt2", "print(\"package: name='dev.test.app' versionCode='3' versionName='1.2' compileSdkVersion='36'\\nminSdkVersion:'26'\\n"
                           "targetSdkVersion:'36'\\nuses-permission: name='android.permission.INTERNET'\\nnative-code: 'arm64-v8a'\")\n")
        self.tool("apksigner", "print('Verifies\\nVerified using v2 scheme (APK Signature Scheme v2): true\\n"
                               "Signer #1 certificate DN: CN=Test\\nSigner #1 certificate SHA-256 digest: 00ff')\n")
        value = dispatch("android.package_inspect", {"path": str(apk)})
        self.assertEqual((value["package"], value["version_code"], value["version_name"], value["min_sdk"], value["target_sdk"]),
                         ("dev.test.app", 3, "1.2", 26, 36))
        self.assertEqual(value["permissions"], ["android.permission.INTERNET"])
        self.assertEqual(value["abis"], ["arm64-v8a", "x86_64"])
        self.assertEqual(value["signing"], {"checked": True, "signed": True, "schemes": ["v2"],
                                            "certificates": [{"subject": "CN=Test", "sha256": "00ff"}]})
        self.tool("apksigner", "import sys;print('DOES NOT VERIFY\\nERROR: Missing META-INF/MANIFEST.MF');sys.exit(1)\n")
        signing = dispatch("android.package_inspect", {"path": str(apk)})["signing"]
        self.assertEqual((signing["signed"], signing["errors"]), (False, ["Missing META-INF/MANIFEST.MF"]))

    def test_a_bundle_is_read_without_aapt2_and_its_signature_through_keytool(self):
        import zipfile
        manifest = self.element("manifest", [("package", "dev.test.bundle"), ("versionCode", "42"), ("versionName", "4.2")], [
            self.element("uses-sdk", [("minSdkVersion", "24"), ("targetSdkVersion", "36")]),
            self.element("uses-permission", [("name", "android.permission.CAMERA")])])
        aab = self.root / "app.aab"
        with zipfile.ZipFile(aab, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("base/manifest/AndroidManifest.xml", manifest)
            archive.writestr("base/lib/arm64-v8a/libcore.so", b"x")
            archive.writestr("base/dex/classes.dex", b"x")
        self.tool("keytool", "print('Signer #1:\\nOwner: CN=Android Debug, O=Android, C=US\\nCertificate fingerprints:\\n  SHA256: 0A:FF')\n")
        value = dispatch("android.package_inspect", {"path": str(aab)})
        self.assertEqual((value["format"], value["package"], value["version_code"], value["min_sdk"], value["target_sdk"]),
                         ("aab", "dev.test.bundle", 42, 24, 36))
        self.assertEqual((value["permissions"], value["abis"], value["modules"]), (["android.permission.CAMERA"], ["arm64-v8a"], ["base"]))
        self.assertEqual(value["signing"]["certificates"], [{"subject": "CN=Android Debug, O=Android, C=US", "sha256": "0aff"}])
        for name, data in (("notes.txt", b"x"), ("broken.apk", b"not a zip")):
            (self.root / name).write_bytes(data)
            with self.assertRaises(StudioError) as refused:
                dispatch("android.package_inspect", {"path": str(self.root / name)})
            self.assertEqual(refused.exception.code, "invalid_artifact")

    def test_size_report_breaks_a_package_down_and_compares_two_builds(self):
        import zipfile
        def apk(name, files):
            path = self.root / name
            with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
                for entry, size in files.items():
                    archive.writestr(entry, b"x" * size)
            return path
        old = apk("old.apk", {"classes.dex": 4000, "lib/arm64-v8a/libcore.so": 2000, "assets/intro.mp4": 900})
        new = apk("new.apk", {"classes.dex": 4500, "lib/arm64-v8a/libcore.so": 2000, "lib/x86_64/libcore.so": 2100,
                              "res/drawable-xxhdpi/hero.png": 300, "res/drawable-mdpi/hero.png": 100})
        value = dispatch("android.size_report", {"path": str(new), "top": 3})
        summary = value["summary"]
        self.assertEqual((value["format"], summary["compressed"], summary["abis"]), ("apk", 9000, {"arm64-v8a": 2000, "x86_64": 2100}))
        self.assertEqual(summary["estimate"]["by_abi_density"]["arm64-v8a/xxhdpi"], 4500 + 2000 + 300)
        self.assertEqual([f["name"] for f in value["largest"]], ["classes.dex", "lib/x86_64/libcore.so", "lib/arm64-v8a/libcore.so"])
        diff = dispatch("android.size_report", {"path": str(new), "baseline": str(old)})["delta"]
        self.assertEqual(diff["summary"]["compressed"], 9000 - 6900)
        self.assertEqual(diff["summary"]["categories"]["assets"]["compressed"], -900)
        self.assertEqual([(f["name"], f["change"], f["delta"]) for f in diff["files"][:2]],
                         [("lib/x86_64/libcore.so", "added", 2100), ("assets/intro.mp4", "removed", -900)])
        # A bundle goes through bundletool's get-size when it is configured.
        aab = self.root / "app.aab"
        with zipfile.ZipFile(aab, "w") as archive:
            archive.writestr("BundleConfig.pb", b"")
            archive.writestr("base/dex/classes.dex", b"x" * 100)
        self.tool("bundletool", "import sys\nfrom pathlib import Path\nif sys.argv[1]=='build-apks': Path(sys.argv[3].split('=',1)[1]).write_bytes(b'PK')\n"
                                "else: print('SDK,ABI,SCREEN_DENSITY,MIN,MAX\\n21-,arm64-v8a,XXHDPI,90,95')\n")
        download = dispatch("android.size_report", {"path": str(aab)})["download"]
        self.assertEqual((download["method"], download["rows"][0]["max"]), ("bundletool get-size total", 95))
        self.assertNotIn("download", dispatch("android.size_report", {"path": str(aab), "bundletool": False}))
        with self.assertRaises(StudioError) as refused:
            dispatch("android.size_report", {"path": str(self.root / "notes.txt") if (self.root / "notes.txt").write_text("x") else ""})
        self.assertEqual(refused.exception.code, "unsupported_artifact")


class ProtocolTests(Isolated):
    def test_cli_schema_errors_and_file_arguments(self):
        schema = json.loads(subprocess.check_output([binary(), "schema"]))["result"]
        self.assertEqual(len(schema["operations"]), len(json.loads((ROOT / "assets/operations.json").read_text())))
        expected = json.loads((ROOT / "assets/operations.json").read_text())
        self.assertEqual(schema["operations"], expected)
        for op, values in (("device.install", {}), ("job.list", {"limit": True}), ("doctor", {"execute": "evil"}), ("preview.capture", {})):
            with self.assertRaises(StudioError): dispatch(op, values)
        args = self.root / "args.json"
        args.write_text('{"platform":"ios"}')
        value = json.loads(subprocess.check_output([binary(), "call", "catalog.list", "@" + str(args)]))
        self.assertTrue(all(p["platform"] == "ios" for p in value["result"]))

    def test_a_session_log_records_calls_and_replay_repeats_the_changing_ones(self):
        log = self.root / "session.jsonl"
        first, second = self.root / "adb-one", self.root / "adb-two"
        for tool in (first, second):
            tool.write_text("#!/bin/sh\n")
            tool.chmod(0o755)
        logged = {**os.environ, "TOMACCO_SESSION_LOG": str(log)}
        call = lambda op, args: subprocess.run([binary(), "call", op, json.dumps(args)], env=logged, capture_output=True, text=True)
        call("catalog.list", {"platform": "ios"})
        call("settings.configure", {"executables": {"adb": str(first)}})
        call("project.inspect", {"path": str(self.root / "missing")})
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "remote.configure", "arguments": {"name": "mac", "host": "mac.local", "user": "me", "password": "hunter2-secret"}}}
        handshake = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
                     {"jsonrpc": "2.0", "method": "notifications/initialized"}, request]
        subprocess.run([binary(), "mcp"], input="\n".join(map(json.dumps, handshake)) + "\n", env=logged, capture_output=True, text=True)
        entries = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([(e["operation"], e["ok"], e["mutating"]) for e in entries],
                         [("catalog.list", True, False), ("settings.configure", True, True), ("project.inspect", False, False),
                          ("remote.configure", False, True)])
        self.assertEqual(entries[0]["result"], {"items": entries[0]["result"]["items"]})
        self.assertNotIn("hunter2", log.read_text())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        dispatch("settings.configure", {"executables": {"adb": str(second)}})
        replay = lambda *flags: json.loads(subprocess.run([binary(), "replay", str(log), *flags], capture_output=True, text=True).stdout)
        plan = replay("--dry-run")["result"]
        self.assertEqual([(c["operation"], c["status"]) for c in plan["calls"]],
                         [("catalog.list", "skipped"), ("settings.configure", "would_run"), ("project.inspect", "skipped"),
                          ("remote.configure", "skipped")])
        self.assertEqual(dispatch("settings.get", {})["executables"]["adb"], str(second))
        done = replay()["result"]
        self.assertEqual((done["ran"], done["skipped"]), (1, 3))
        self.assertEqual(dispatch("settings.get", {})["executables"]["adb"], str(first))
        self.assertFalse(replay("--wait")["ok"])

    def test_mcp_handshake_resources_and_error_recovery(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "catalog.list"}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "device.install", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 5, "method": "resources/read", "params": {"uri": "tomacco://catalog"}},
            {"jsonrpc": "2.0", "id": 6, "method": "resources/read", "params": {"uri": "tomacco://capabilities"}},
        ]
        output = subprocess.check_output([binary(), "mcp"], input='bad\n' + '\n'.join(map(json.dumps, requests)) + '\n', text=True)
        replies = sorted([json.loads(line) for line in output.splitlines()], key=lambda r: -1 if r["id"] is None else r["id"])
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual([r["id"] for r in replies[1:]], [1, 2, 3, 4, 5, 6])
        self.assertEqual(replies[1]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(len(replies[2]["result"]["tools"]), len(json.loads((ROOT / "assets/operations.json").read_text())))
        for tool in replies[2]["result"]["tools"]:
            self.assertEqual(sorted(tool["annotations"]), ["destructiveHint", "idempotentHint", "openWorldHint", "readOnlyHint"], tool["name"])
        self.assertFalse(replies[3]["result"]["isError"])
        # A list result is wrapped, since structured content is an object.
        self.assertEqual(replies[3]["result"]["structuredContent"], {"result": json.loads(replies[3]["result"]["content"][0]["text"])})
        self.assertTrue(replies[4]["result"]["isError"])
        self.assertEqual(replies[4]["result"]["structuredContent"], json.loads(replies[4]["result"]["content"][0]["text"]))
        self.assertIn("contents", replies[5]["result"])
        capabilities = json.loads(replies[6]["result"]["contents"][0]["text"])
        self.assertEqual(sorted(capabilities["ios"]), ["build", "devices", "virtual_iphone"])
        self.assertIsInstance(capabilities["android"]["emulator"]["ready"], bool)
        tool = next(t for t in replies[2]["result"]["tools"] if t["name"] == "capabilities")
        self.assertEqual(tool["outputSchema"]["required"], ["android", "ios", "desktop"])

    def test_mcp_resource_templates_read_a_job_its_log_and_the_reference(self):
        job = self.submit([{"argv": [sys.executable, "-c", "print('first line');print('last line')"]}])
        self.assertEqual(jobs.wait(job["id"], 5)["state"], "succeeded")
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "id": 2, "method": "resources/templates/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": f"tomacco://jobs/{job['id']}"}},
            {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": f"tomacco://jobs/{job['id']}/log"}},
            {"jsonrpc": "2.0", "id": 5, "method": "resources/read", "params": {"uri": "tomacco://reference/project.run"}},
            {"jsonrpc": "2.0", "id": 6, "method": "resources/read", "params": {"uri": "tomacco://jobs/" + "0" * 32}},
            {"jsonrpc": "2.0", "id": 7, "method": "completion/complete",
             "params": {"ref": {"type": "ref/resource", "uri": "tomacco://jobs/{id}"}, "argument": {"name": "id", "value": job["id"][:6]}}},
        ]
        output = subprocess.check_output([binary(), "mcp"], input="\n".join(map(json.dumps, requests)) + "\n", text=True, timeout=30)
        replies = {r["id"]: r for r in map(json.loads, output.splitlines())}
        self.assertEqual(replies[1]["result"]["capabilities"]["completions"], {})
        self.assertEqual(len(replies[2]["result"]["resourceTemplates"]), 3)
        self.assertEqual(json.loads(replies[3]["result"]["contents"][0]["text"])["state"], "succeeded")
        self.assertIn("first line\nlast line\n", replies[4]["result"]["contents"][0]["text"])
        reference = replies[5]["result"]["contents"]
        self.assertTrue(reference[0]["text"].startswith("project.run - "))
        self.assertEqual(json.loads(reference[1]["text"])["name"], "project.run")
        self.assertEqual(replies[6]["error"]["code"], -32602)
        self.assertEqual(replies[7]["result"]["completion"]["values"], [job["id"]])

    def test_mcp_shorter_tool_lists_call_the_same_operations(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "catalog", "arguments": {"operation": "catalog.list", "arguments": {"platform": "ios"}}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "schema", "arguments": {"operation": "job.wait"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "catalog", "arguments": {"operation": "job.list"}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "catalog.list", "arguments": {}}},
        ]
        sizes = {}
        for mode in ("full", "compact", "grouped"):
            output = subprocess.check_output([binary(), "mcp"], input="\n".join(map(json.dumps, requests)) + "\n", text=True,
                                             env={**os.environ, "TOMACCO_MCP_TOOLS": mode}, timeout=30)
            replies = {r["id"]: r for r in map(json.loads, output.splitlines())}
            sizes[mode] = len(json.dumps(replies[2]["result"]["tools"], separators=(",", ":")))
            names = {tool["name"] for tool in replies[2]["result"]["tools"]}
            self.assertEqual("catalog.list" in names, mode != "grouped")
            self.assertEqual("schema" in names, mode != "full")
            self.assertFalse(replies[3]["result"]["isError"], replies[3])
            self.assertTrue(all(p["platform"] == "ios" for p in replies[3]["result"]["structuredContent"]["result"]))
            self.assertEqual(replies[4]["result"]["structuredContent"]["name"], "job.wait")
            self.assertEqual(replies[5]["result"]["structuredContent"]["error"]["code"], "invalid_argument")
            self.assertFalse(replies[6]["result"]["isError"])
        self.assertLess(sizes["compact"], sizes["full"] * 0.6)
        self.assertLess(sizes["grouped"], sizes["full"] * 0.2)

    def test_mcp_wait_does_not_block_other_requests_and_cancellation_preserves_job(self):
        import queue
        job=self.submit([{"argv":[sys.executable,"-c","import time;print('ready',flush=True);time.sleep(30)"]}],timeout=40)
        self.wait_log(job,"ready")
        process=subprocess.Popen([binary(),"mcp"],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        replies=queue.Queue()
        def read():
            for line in process.stdout: replies.put(json.loads(line))
        reader=threading.Thread(target=read,daemon=True);reader.start()
        def send(value):process.stdin.write(json.dumps({"jsonrpc":"2.0",**value})+"\n");process.stdin.flush()
        try:
            send({"id":1,"method":"initialize","params":{}})
            self.assertEqual(replies.get(timeout=3)["id"],1)
            send({"id":2,"method":"tools/call","params":{"name":"job.wait","arguments":{"job_id":job["id"],"seconds":30}}})
            send({"id":3,"method":"tools/call","params":{"name":"catalog.list"}})
            send({"id":4,"method":"ping"})
            immediate=[replies.get(timeout=2),replies.get(timeout=2)]
            self.assertEqual({r["id"] for r in immediate},{3,4})
            send({"method":"notifications/cancelled","params":{"requestId":2}})
            process.stdin.close();process.wait(3*PATIENCE);reader.join(1)
            self.assertTrue(replies.empty(),list(replies.queue))
            self.assertEqual(jobs.get(job["id"])["state"],"running")
        finally:
            if process.poll() is None:process.kill();process.wait()
            if not process.stdin.closed:process.stdin.close()
            process.stdout.close();process.stderr.close()
            jobs.cancel(job["id"]);jobs.wait(job["id"],5)

    def test_mcp_job_wait_reports_progress_and_log_lines_before_its_result(self):
        job=self.submit([{"argv":[sys.executable,"-c","import time\nfor i in range(12):print('step',i,flush=True);time.sleep(0.4)"]}],timeout=40)
        self.wait_log(job,"step 0")
        requests=[{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}},
                  {"jsonrpc":"2.0","id":3,"method":"logging/setLevel","params":{"level":"info"}},
                  {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"job.wait","arguments":{"job_id":job["id"],"seconds":30},"_meta":{"progressToken":"wait-1"}}}]
        output=subprocess.check_output([binary(),"mcp"],input="\n".join(map(json.dumps,requests))+"\n",text=True,timeout=40)
        messages=[json.loads(line) for line in output.splitlines()]
        result=next(i for i,m in enumerate(messages) if m.get("id")==2)
        self.assertEqual(messages[result]["result"]["structuredContent"]["state"],"succeeded")
        progress=[m["params"] for m in messages if m.get("method")=="notifications/progress"]
        self.assertTrue(progress,messages)
        self.assertTrue(all(i<result for i,m in enumerate(messages) if m.get("method")=="notifications/progress"))
        self.assertEqual({p["progressToken"] for p in progress},{"wait-1"})
        self.assertEqual([p["progress"] for p in progress],sorted({p["progress"] for p in progress}))
        self.assertTrue(any("step" in p["message"] for p in progress),progress)
        logged=[m["params"] for i,m in enumerate(messages) if m.get("method")=="notifications/message" and i<result]
        self.assertTrue(logged,messages)
        self.assertTrue(all(p["level"]=="info" and p["logger"]=="job "+job["id"] and p["data"].startswith("step ") for p in logged),logged)

    def test_long_socket_path_and_preview_readiness(self):
        data = self.root / ("nested-project-" * 12) / "state"
        directory = Path('/tmp') / f"tomacco-{os.getuid()}-{hashlib.sha256(os.fsencode(data)).hexdigest()[:16]}"
        directory.mkdir(mode=0o700)
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(directory / 'preview-test.sock'))
        server.listen()
        samples = []
        def serve():
            for _ in range(6):
                conn, _ = server.accept()
                with conn:
                    request = json.loads(conn.makefile('rb').readline())
                    if request['method'] == 'state': result = {'loading': False}
                    else:
                        samples.append(True)
                        result = len(samples) >= 3
                    conn.sendall(json.dumps({'id': 1, 'result': result}).encode() + b'\n')
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {'TOMACCO_HOME': str(data)}):
                self.assertEqual(preview.ready('test', timeout=5), {'loading': False})
            self.assertEqual(len(samples), 3)
            thread.join(1)
        finally:
            server.close()
            (directory / 'preview-test.sock').unlink()
            directory.rmdir()




class ProjectFilesTests(Isolated):
    def test_conflicts_binary_bounds_permissions_and_symlink_escape(self):
        file = self.root / "source.swift"
        file.write_text("let value = 1\n")
        file.chmod(0o755)
        a = {"path": str(self.root), "file": "source.swift"}
        read = dispatch("files.read", a)
        file.write_text("let value = 2\n")
        with self.assertRaises(StudioError) as error:
            dispatch("files.write", {**a, "text": "lost update", "if_match": read["sha256"]})
        self.assertEqual(error.exception.code, "file_conflict")
        self.assertEqual(file.read_text(), "let value = 2\n")
        read = dispatch("files.read", a)
        dispatch("files.write", {**a, "text": "let value = 3\n", "if_match": read["sha256"]})
        self.assertEqual(file.stat().st_mode & 0o777, 0o755)
        (self.root / "outside").symlink_to("/etc")
        with self.assertRaises(StudioError): dispatch("files.read", {**a, "file": "outside/passwd"})
        with self.assertRaises(StudioError): dispatch("files.write", {**a, "file": "../escape", "text": "x", "if_match": ""})
        file.write_bytes(b"\x00\xff")
        with self.assertRaises(StudioError): dispatch("files.read", a)
        file.write_bytes(b"x" * (1024*1024+1))
        with self.assertRaises(StudioError): dispatch("files.read", a)

    def test_listing_search_and_new_files(self):
        base = {"path": str(self.root)}
        dispatch("files.mkdir", {**base, "directory": "src"})
        dispatch("files.write", {**base, "file": "src/main.rs", "text": "fn main() { println!(\"Grüße\"); }\n", "if_match": ""})
        (self.root / "build").mkdir()
        (self.root / "build/generated.txt").write_text("Grüße")
        listed = dispatch("files.list", base)
        self.assertIn("src", [e["name"] for e in listed["entries"]])
        self.assertNotIn("build", [e["name"] for e in listed["entries"]])
        results = dispatch("files.search", {**base, "query": "grüße"})
        self.assertEqual([m["file"] for m in results["matches"]], ["src/main.rs"])
        with self.assertRaises(StudioError): dispatch("files.write", {**base, "file": "src/main.rs", "text": "overwrite", "if_match": ""})




class DoctorDesktopTests(Isolated):
    """doctor asks the dynamic loader whether the desktop can start here."""

    def doctor(self, ldd_output=None):
        desktop = self.root / "tomacco"
        desktop.write_text("#!/bin/sh\nexit 0\n")
        desktop.chmod(0o755)
        env = {**os.environ, "TOMACCO_BINARY": str(desktop)}
        if ldd_output is not None:
            tools = self.root / "fake-ldd"
            tools.mkdir(exist_ok=True)
            (tools / "ldd").write_text("#!/bin/sh\ncat <<'EOF'\n" + ldd_output + "EOF\nexit 1\n")
            (tools / "ldd").chmod(0o755)
            env["PATH"] = str(tools) + os.pathsep + env["PATH"]
        value = subprocess.run([binary(), "call", "doctor", "{}"], capture_output=True, text=True, timeout=120, env=env)
        reply = json.loads(value.stdout)
        self.assertTrue(reply["ok"], reply)
        return reply["result"], desktop

    def test_missing_libraries_and_versions_become_the_first_next_step(self):
        result, desktop = self.doctor(
            "\tlibavcodec.so.63 => not found\n"
            "/x/tomacco: /usr/lib/libQt6Core.so.6: version `Qt_6.12' not found (required by /x/tomacco)\n")
        report = result["desktop"]
        self.assertEqual(report["path"], str(desktop))
        self.assertIs(report["loadable"], False, report)
        self.assertEqual(report["missing_libraries"], ["libavcodec.so.63"])
        self.assertEqual(report["missing_versions"], ["Qt_6.12 in libQt6Core.so.6"])
        step = result["next_steps"][0]
        self.assertEqual(step["goal"], "desktop", result["next_steps"])
        self.assertIn("libavcodec.so.63", step["what"])
        self.assertIn("Qt 6.12", step["how"])

    def test_a_desktop_the_real_loader_accepts_has_no_step(self):
        # The real ldd: a shell script is not a dynamic executable, so not refused.
        result, _ = self.doctor()
        self.assertNotEqual(result["desktop"]["loadable"], False, result["desktop"])
        self.assertNotIn("desktop", [step["goal"] for step in result["next_steps"]])


class ReleaseContractTests(Isolated):
    def test_version_preserves_comments_and_rejects_ambiguous_gradle(self):
        pubspec = self.root / "pubspec.yaml"
        pubspec.write_text("name: example\nversion: 1.0.0+1 # Keep this note\n")
        result = dispatch("release.version", {"path":str(self.root), "version":"2.3.4", "build_number":12})
        self.assertEqual("name: example\nversion: 2.3.4+12 # Keep this note\n", pubspec.read_text())
        self.assertEqual(Path(result["backup"]).read_text(), "name: example\nversion: 1.0.0+1 # Keep this note\n")
        pubspec.unlink()
        (self.root / "app").mkdir()
        gradle=self.root / "app/build.gradle.kts"
        gradle.write_text('versionName = providers.gradleProperty("appVersion").get()\nversionCode = 1\n')
        with self.assertRaises(StudioError): dispatch("release.version", {"path":str(self.root), "version":"2.0.0", "build_number":2})
        self.assertIn('providers.gradleProperty', gradle.read_text())

    def test_version_bump_dry_run_lists_every_file_and_writes_nothing(self):
        (self.root / "app").mkdir()
        gradle = self.root / "app/build.gradle.kts"
        gradle.write_text('android {\n  defaultConfig {\n    versionCode = libs.versions.appCode.get().toInt()\n    versionName = "1.4.2"\n  }\n}\n')
        (self.root / "gradle").mkdir()
        catalog = self.root / "gradle/libs.versions.toml"
        catalog.write_text('[versions]\nappCode = "17"\n')
        before = (gradle.read_text(), catalog.read_text())
        plan = dispatch("release.version", {"path": str(self.root), "bump": "minor", "dry_run": True})
        self.assertEqual((plan["version"], plan["build_number"], plan["dry_run"]), ("1.5.0", 18, True))
        self.assertEqual(sorted((Path(c["file"]).name, c["from"], c["to"]) for c in plan["changes"]),
                         [("build.gradle.kts", "1.4.2", "1.5.0"), ("libs.versions.toml", "17", "18")])
        self.assertEqual((gradle.read_text(), catalog.read_text()), before)
        result = dispatch("release.version", {"path": str(self.root), "bump": "build"})
        self.assertEqual((result["version"], result["build_number"]), ("1.4.2", 18))
        self.assertEqual(catalog.read_text(), '[versions]\nappCode = "18"\n')
        self.assertEqual(sorted(Path(f["backup"]).read_text() for f in result["files"]), [before[1]])
        for arguments in ({"bump": "patch", "version": "1.0.0"}, {}, {"version": "1.0.0"}):
            with self.assertRaises(StudioError) as refused:
                dispatch("release.version", {"path": str(self.root), **arguments})
            self.assertEqual(refused.exception.code, "invalid_argument")

    def test_release_notes_are_written_per_language_and_refused_over_the_limit(self):
        english = self.root / "CHANGELOG.md"
        english.write_text("# Changelog\n\n## [2.0.0] - 2026-11-01\n\n- **Dark mode** everywhere.\n- Faster `sync`.\n\n## 1.9.0\n\n- Old\n")
        german = self.root / "CHANGELOG.de.md"
        german.write_text("## 2.0.0\n\n- Dunkler Modus überall.\n")
        spanish = self.root / "CHANGELOG.es.md"
        spanish.write_text("## 2.0.0\n\n" + "".join(f"- Cambio número {i} con bastante texto\n" for i in range(30)))
        out = self.root / "metadata"
        base = {"changelog": str(english), "section": "2.0.0", "directory": str(out), "build_number": 20}
        result = dispatch("release.notes", {**base, "translations": [{"language": "de-DE", "changelog": str(german)}]})
        self.assertTrue(result["passed"] and result["written"])
        self.assertEqual((out / "en-US/changelogs/20.txt").read_text(), "• Dark mode everywhere.\n• Faster sync.\n")
        self.assertEqual((out / "de-DE/changelogs/20.txt").read_text(), "• Dunkler Modus überall.\n")
        self.assertEqual(result["next"]["arguments"]["release_notes"][1], {"language": "de-DE", "text": "• Dunkler Modus überall."})
        # One language over 500 characters: nothing is written for any.
        spanish_too = {**base, "layout": "whatsnew", "translations": [{"language": "es-419", "changelog": str(spanish)}]}
        result = dispatch("release.notes", spanish_too)
        self.assertEqual((result["passed"], result["written"], result["findings"][0]["code"]), (False, False, "too_long"))
        self.assertNotIn("release_notes", result)
        self.assertFalse((out / "whatsnew-en-US").exists())
        result = dispatch("release.notes", {**spanish_too, "truncate": True})
        self.assertTrue(result["passed"])
        self.assertLessEqual(len((out / "whatsnew-es-419").read_text().rstrip("\n")), 500)
        self.assertEqual(result["findings"][0]["code"], "truncated")
        self.assertFalse(dispatch("release.notes", {**base, "section": "3.0.0", "dry_run": True})["passed"])
        # play.prepare takes the result's language codes, es-419 included.
        with self.assertRaises(StudioError) as refused:
            dispatch("play.prepare", {"credentials": str(self.root / "missing.json"), "package": "dev.example.app", "path": str(self.root / "a.aab"),
                                      "release_name": "2.0.0", "release_notes": result["release_notes"]})
        self.assertNotEqual(refused.exception.code, "invalid_argument")

    def test_signing_health_reads_keytool_and_checks_the_play_setup(self):
        listing = ("Alias name: upload\nEntry type: PrivateKeyEntry\nCertificate[1]:\nOwner: CN=Upload\nIssuer: CN=Upload\n"
                   "Valid from: Sun Nov 01 10:00:00 UTC 2026 until: Fri Nov 01 10:00:00 UTC 2030\n"
                   "Certificate fingerprints:\n\t SHA1: 01:02\n\t SHA256: " + ":".join(["AB"] * 32) + "\n"
                   "Signature algorithm name: SHA256withRSA\nSubject Public Key Algorithm: 4096-bit RSA key\n")
        log = self.root / "keytool.json"
        self.tool("keytool", f"import json,sys\nfrom pathlib import Path\nPath({str(log)!r}).write_text(json.dumps(sys.argv[1:]))\n"
                             f"print({listing!r})\n")
        password = self.root / "password"
        password.write_text("fixture-password\n")
        password.chmod(0o600)
        keystore = self.root / "upload.p12"
        keystore.write_bytes(b"store")
        base = {"keystore": str(keystore), "password_file": str(password)}
        value = dispatch("release.signing_health", {**base, "upload_certificate_sha256": "ab" * 32})
        entry = value["entries"][0]
        self.assertEqual((entry["alias"], entry["key_size"], entry["valid_until"], entry["play_role"]), ("upload", 4096, "2030-11-01", "upload"))
        self.assertEqual(value["findings"][0]["code"], "play_minimum_validity")
        self.assertTrue(value["passed"])
        argv = json.loads(log.read_text())
        self.assertIn("-J-Duser.timezone=UTC", argv)
        self.assertEqual(argv[argv.index("-storepass:file") + 1], str(password))
        self.assertNotIn("fixture-password", json.dumps(value))
        value = dispatch("release.signing_health", {**base, "warn_days": 2000, "app_signing_certificate_sha256": "cd" * 32})
        self.assertEqual([f["code"] for f in value["findings"]], ["expires_soon", "play_minimum_validity", "not_play_key"])
        self.assertFalse(value["passed"])
        # An artifact signed with another key fails.
        import zipfile
        apk = self.root / "app.apk"
        with zipfile.ZipFile(apk, "w") as archive:
            archive.writestr("AndroidManifest.xml", b"")
        self.tool("aapt2", "print(\"package: name='dev.test.app' versionCode='3' versionName='1.2'\")\n")
        self.tool("apksigner", "print('Verifies\\nSigner #1 certificate DN: CN=Other\\nSigner #1 certificate SHA-256 digest: " + "ee" * 32 + "')\n")
        value = dispatch("release.signing_health", {**base, "artifact": str(apk)})
        self.assertEqual((value["artifact"]["matches_keystore"], value["findings"][-1]["code"]), (False, "artifact_other_key"))
        password.chmod(0o644)
        with self.assertRaises(StudioError) as refused:
            dispatch("release.signing_health", base)
        self.assertEqual(refused.exception.code, "secret_permissions")

    def test_checklist_reports_each_release_requirement_with_evidence(self):
        app = self.root / "shop"
        (app / "app/src/main/res/xml").mkdir(parents=True)
        (app / "settings.gradle.kts").write_text('include(":app")\n')
        build = app / "app/build.gradle.kts"
        build.write_text('plugins { id("com.android.application") }\nandroid {\n  namespace = "dev.test.shop"\n  defaultConfig {\n'
                         '    applicationId = "dev.test.shop"\n    targetSdk = 34\n    versionCode = 12\n    versionName = "1.2.0"\n  }\n'
                         '  buildTypes {\n    release {\n      isMinifyEnabled = false\n      signingConfig = signingConfigs.getByName("debug")\n    }\n  }\n}\n')
        (app / "app/src/main/AndroidManifest.xml").write_text(
            '<manifest xmlns:android="http://schemas.android.com/apk/res/android"><application android:networkSecurityConfig="@xml/network"/></manifest>')
        (app / "app/src/main/res/xml/network.xml").write_text(
            '<network-security-config><domain-config cleartextTrafficPermitted="true"><domain>10.0.2.2</domain></domain-config></network-security-config>')
        value = dispatch("release.checklist", {"path": str(app), "previous_version_code": 12})
        status = {c["check"]: (c["status"], c["severity"]) for c in value["checks"]}
        self.assertEqual(status, {"target_sdk": ("fail", "error"), "version_code": ("fail", "error"), "debuggable": ("pass", "info"),
                                  "minify": ("fail", "warning"), "signing_config": ("fail", "error"), "cleartext": ("fail", "warning")})
        self.assertEqual((value["passed"], value["module"], value["required_target_sdk"]), (False, ":app", 36))
        self.assertEqual(next(c for c in value["checks"] if c["check"] == "cleartext")["evidence"]["domains"], ["10.0.2.2"])
        build.write_text(build.read_text().replace("targetSdk = 34", "targetSdk = 36").replace("versionCode = 12", "versionCode = 13")
                         .replace("isMinifyEnabled = false", "isMinifyEnabled = true").replace('getByName("debug")', 'getByName("upload")'))
        value = dispatch("release.checklist", {"path": str(app), "previous_version_code": 12, "allow_cleartext": True})
        self.assertTrue(value["passed"])
        self.assertEqual({c["status"] for c in value["checks"]}, {"pass"})
        self.assertEqual(dispatch("release.checklist", {"path": str(app), "required_target_sdk": 37})["checks"][0]["status"], "fail")
        with self.assertRaises(StudioError) as refused:
            dispatch("release.checklist", {"path": str(self.root / "missing-project")})
        (self.root / "empty").mkdir()
        with self.assertRaises(StudioError) as refused:
            dispatch("release.checklist", {"path": str(self.root / "empty")})
        self.assertEqual(refused.exception.code, "not_android_project")

    def test_a_secret_given_instead_of_its_file_is_not_echoed(self):
        secret = "hunter2-correct-horse"
        output = str(self.root / "release.p12")
        requests = [
            ("release.keystore", {"output": output, "password_file": secret, "alias": "fixture", "distinguished_name": "CN=Fixture"}),
            ("play.commit", {"credentials": secret, "package": "dev.example.app", "edit_id": "e1"}),
            ("settings.configure", {"updates": {"token_file": "ghp_" + "a" * 36}}),
        ]
        for operation, arguments in requests:
            value = subprocess.run([binary(), "call", operation, json.dumps(arguments)], capture_output=True, text=True, timeout=60)
            reply = json.loads(value.stdout)
            self.assertFalse(reply["ok"], reply)
            self.assertEqual(reply["error"]["code"], "path_not_found", reply)
            self.assertNotIn(secret, value.stdout + value.stderr)
            self.assertNotIn("ghp_aaaa", value.stdout + value.stderr)
            self.assertIn("not the secret itself", reply["error"]["remedy"])

    def test_job_records_and_logs_mask_secrets_a_tool_prints(self):
        token = "ghp_" + "b" * 36
        code = f"import sys;print('Authorization: Bearer {token}');print('password=hunter2-staple', file=sys.stderr)"
        job = self.submit([{"argv": [sys.executable, "-c", code, "--token=" + token]}])
        done = jobs.wait(job["id"], 5)
        self.assertEqual(done["state"], "succeeded", done)
        log = jobs.logs(job["id"])["text"]
        self.assertIn("Authorization: [redacted]", log)
        for record in (log, json.dumps(jobs.get(job["id"])), json.dumps(jobs.listing(5)), json.dumps(done)):
            self.assertNotIn(token, record)
            self.assertNotIn("hunter2-staple", record)

    def test_signing_cancel_and_worker_crash_remove_temporary_credentials(self):
        password=self.root/"password";password.write_text("private-fixture-password\n");password.chmod(0o600)
        self.tool("keytool", "import os,time\nfrom pathlib import Path\nPath(os.environ['TOMACCO_TEST_ROOT'],'signer.pid').write_text(str(os.getpid()))\ntime.sleep(60)\n")
        for crash in [False,True]:
            marker=self.root/"signer.pid"
            marker.unlink(missing_ok=True)
            output=self.root/("crash.p12" if crash else "cancel.p12")
            job=dispatch("release.keystore",{"output":str(output),"password_file":str(password),"alias":"fixture","distinguished_name":"CN=Fixture"})
            deadline=time.monotonic()+5*PATIENCE
            while not marker.exists():
                self.assertLess(time.monotonic(),deadline,jobs.get(job["id"]))
                time.sleep(.03)
            signer=int(marker.read_text())
            self.assertTrue(list(self.root.glob('.tomacco-sign-*/key-password')))
            if crash:
                import signal
                with closing(sqlite3.connect(self.root/'state/jobs.sqlite3')) as db:
                    pid=db.execute('SELECT pid FROM jobs WHERE id=?',[job['id']]).fetchone()[0]
                os.kill(pid,signal.SIGKILL)
            else:jobs.cancel(job['id'])
            done=jobs.wait(job['id'],5)
            self.assertEqual(done['state'],'failed' if crash else 'cancelled',done)
            self.assertFalse(list(self.root.glob('.tomacco-sign-*')))
            self.assertFalse(output.exists())
            self.assertNotIn('private-fixture-password',jobs.logs(job['id'])['text'])
            deadline=time.monotonic()+3*PATIENCE
            while Path(f'/proc/{signer}/stat').exists() and Path(f'/proc/{signer}/stat').read_text().rsplit(')',1)[1].split()[0]!='Z':
                self.assertLess(time.monotonic(),deadline,'signing process survived cancellation')
                time.sleep(.03)

    def test_apple_commands_are_typed_quoted_and_keep_failure_evidence(self):
        self.tool("ssh")
        dispatch("remote.configure", {"name":"mac", "host":"build-mac", "directory":"/Users/build/An App"})
        job=dispatch("apple.test", {"remote":"mac", "project":"ios/Runner.xcworkspace", "scheme":"App; $(touch NEVER)", "destination":"platform=iOS Simulator,id=123", "only_testing":["AppUITests/Navigation"]})
        command=job["plan"]["steps"][-1]["argv"][-1]
        self.assertIn("'App; $(touch NEVER)'", command)
        self.assertIn("-only-testing:AppUITests/Navigation", command)
        self.assertTrue(job["plan"]["cleanup"])
        self.assertIn("Tests.xcresult.zip",job["plan"]["always_artifacts"])
        jobs.cancel(job["id"]);jobs.wait(job["id"],5)
        with self.assertRaises(StudioError):dispatch("apple.export", {"remote":"mac", "archive":"../outside", "export_options":"missing"})

    def test_play_prepare_validates_transaction_and_never_commits_implicitly(self):
        key=self.root / "key.pem"
        subprocess.run(["openssl","genpkey","-algorithm","RSA","-pkeyopt","rsa_keygen_bits:2048","-out",str(key)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        credentials=self.root / "account.json"
        credentials.write_text(json.dumps({"type":"service_account","private_key":key.read_text(),"private_key_id":"fixture","client_email":"test@example.iam.gserviceaccount.com","token_uri":"https://oauth2.googleapis.com/token"}))
        credentials.chmod(0o600)
        bundle=self.root / "app.aab";bundle.write_bytes(b"PK\x03\x04contract-fixture")
        self.tool("jarsigner", "print('jar verified.')\n")
        self.tool("curl", r"""import sys,json,os
from pathlib import Path
args=sys.argv[1:]
url=next(a for a in args if a.startswith('https://'))
output=Path(args[args.index('--output')+1])
method=args[args.index('--request')+1]
root=Path(os.environ['TOMACCO_TEST_ROOT'])
with (root/'requests.jsonl').open('a') as file:file.write(json.dumps({'url':url,'method':method})+'\n')
if 'oauth2.googleapis.com' in url:
    body=Path(args[args.index('--data-binary')+1][1:]).read_text()
    assert 'assertion=' in body and body.count('.')>=2
    value={'access_token':'fixture-token','expires_in':3600}
else:
    header=Path(args[args.index('--header')+1][1:]).read_text()
    assert 'Authorization: Bearer fixture-token' in header
    if url.endswith('/edits'):value={'id':'edit123','expiryTimeSeconds':'9999999999'}
    elif '/bundles?' in url:value={'versionCode':42,'sha256':'fixture'}
    elif '/tracks/' in url:
        value=json.loads(Path(args[args.index('--data-binary')+1][1:]).read_text())
        assert value['releases'][0]['versionCodes']==['42']
    else:value={}
output.write_text(json.dumps(value))
print('200',end='')
""")
        job=dispatch("play.prepare", {"credentials":str(credentials),"package":"app.example","path":str(bundle),"release_name":"Contract"})
        done=jobs.wait(job["id"],15)
        self.assertEqual(done["state"],"succeeded",jobs.logs(job["id"]))
        report=json.loads(Path(done["result"]["artifacts"][0]["path"]).read_text())
        self.assertEqual(report["result"]["edit_id"],"edit123")
        self.assertFalse(report["result"]["committed"])
        requests=[json.loads(line) for line in (self.root/'requests.jsonl').read_text().splitlines()]
        self.assertTrue(any(':validate' in r['url'] for r in requests))
        self.assertFalse(any(':commit' in r['url'] for r in requests))
        self.assertNotIn('fixture-token',jobs.logs(job["id"])["text"])
        self.assertFalse(list((self.root/'state/private').glob('*')))


class IphoneLinuxTests(Isolated):
    def test_deployment_launches_actual_signed_id_and_rejects_ambiguous_apps(self):
        project=self.root/'App';project.mkdir()
        (project/'xtool.yml').write_text('bundleID: dev.test.app\n')
        manifest=project/'xtool/Counter.app/Info.plist';manifest.parent.mkdir(parents=True)
        manifest.write_text('<?xml version="1.0"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist><dict><key>CFBundleIdentifier</key><string>dev.test.app</string></dict></plist>')
        inventory=self.root/'apps.json'
        inventory.write_text(json.dumps([{'CFBundleIdentifier':'XTL-ABC123.dev.test.app'}]))
        self.tool('xtool')
        self.tool('go-ios', "import json,os,sys\nfrom pathlib import Path\nr=Path(os.environ['TOMACCO_TEST_ROOT'])\nif sys.argv[1]=='apps':print((r/'apps.json').read_text())\nelse:(r/'launched.json').write_text(json.dumps(sys.argv[1:]))\n")
        args={'path':str(project),'platform':'ios','device':'00008110-0011223344556677'}
        done=jobs.wait(dispatch('project.run',args)['id'],10)
        self.assertEqual(done['state'],'succeeded',jobs.logs(done['id']))
        marker=self.root/'launched.json'
        self.assertEqual(json.loads(marker.read_text()),['launch','XTL-ABC123.dev.test.app','--udid='+args['device']])
        marker.unlink()
        inventory.write_text(json.dumps([{'CFBundleIdentifier':'XTL-ABC123.dev.test.app'},{'CFBundleIdentifier':'XTL-OTHER.dev.test.app'}]))
        done=jobs.wait(dispatch('project.run',args)['id'],10)
        self.assertEqual(done['state'],'failed')
        self.assertFalse(marker.exists())
        done=jobs.wait(dispatch('project.run',{**args,'bundle_id':'XTL-ABC123.dev.test.app'})['id'],10)
        self.assertEqual(done['state'],'succeeded',jobs.logs(done['id']))
        marker.unlink()
        self.tool('xtool','raise SystemExit(1)\n')
        done=jobs.wait(dispatch('project.run',args)['id'],10)
        self.assertEqual(done['state'],'failed')
        self.assertFalse(marker.exists())

    def test_a_virtual_iphone_run_is_refused_before_any_job_when_it_cannot_go(self):
        project=self.root/'App';project.mkdir()
        (project/'xtool.yml').write_text('bundleID: dev.test.app\n')
        self.tool('xtool')
        args={'path':str(project),'platform':'ios','device':'virtual-iphone'}
        with self.assertRaises(StudioError) as caught:
            dispatch('project.run',args)
        self.assertEqual(caught.exception.code,'ios_virtual_unconfigured')
        with self.assertRaises(StudioError) as caught:
            dispatch('project.run',{**args,'platform':'android'})
        self.assertEqual(caught.exception.code,'platform_unsupported')
        other=self.root/'Web';other.mkdir();(other/'package.json').write_text('{"scripts":{"build":"true"}}')
        with self.assertRaises(StudioError) as caught:
            dispatch('project.run',{**args,'path':str(other)})
        self.assertEqual(caught.exception.code,'action_unsupported')
        with self.assertRaises(StudioError) as caught:
            dispatch('project.watch',args)
        self.assertEqual(caught.exception.code,'ios_virtual_unconfigured')
        with self.assertRaises(StudioError) as caught:
            dispatch('project.watch',{**args,'crash_check_seconds':121})
        self.assertEqual(caught.exception.code,'invalid_argument')
        self.assertEqual(jobs.listing(10),[])

    def test_physical_runtime_owns_usb_processes_and_starts_xctest_without_mac(self):
        self.tool('iproxy', (ROOT/'tests/fixtures/iphone_usb.py').read_text())
        self.tool('go-ios', "import json,os,sys,time\nfrom pathlib import Path\nPath(os.environ['TOMACCO_TEST_ROOT'],'runner-process.json').write_text(json.dumps({'pid':os.getpid(),'argv':sys.argv[1:]}))\ntime.sleep(120)\n")
        device='00008110-0011223344556677'
        live=dispatch('runtime.start',{'platform':'ios','device':device,'runner_bundle_id':'dev.test.Runner.xctrunner'})
        try:
            self.assertEqual(live['transport'],'usb/wda')
            self.assertEqual(live['codec'],'mjpeg')
            self.assertFalse(live['mac_required'])
            self.assertEqual(live['point_width'],390)
            runner=json.loads((self.root/'runner-process.json').read_text())
            usb=json.loads((self.root/'usb-process.json').read_text())
            self.assertEqual(runner['argv'],['runwda','--udid='+device,'--bundleid=dev.test.Runner.xctrunner','--testrunnerbundleid=dev.test.Runner.xctrunner','--xctestconfig=WebDriverAgentRunner.xctest'])
            self.assertEqual(usb['argv'][:4],['--udid',device,'--source','127.0.0.1'])
            calls=[json.loads(line) for line in (self.root/'wda-calls.jsonl').read_text().splitlines()]
            caps=next(c['body']['capabilities']['alwaysMatch'] for c in calls if c['path']=='/session')
            self.assertFalse(caps['shouldTerminateApp'])
            with socket.socket(socket.AF_UNIX) as stream:
                stream.settimeout(3);stream.connect(live['video_socket'])
                wire=b''
                while len(wire)<8:wire+=stream.recv(8-len(wire))
                self.assertEqual(wire,b'\x00\x00\x00\x04\xff\xd8\xff\xd9')
            with ThreadPoolExecutor(max_workers=6) as pool:
                reused=list(pool.map(lambda _:dispatch('runtime.start',{'platform':'ios','device':device,'runner_bundle_id':'dev.test.Runner.xctrunner'}),range(6)))
            self.assertTrue(all(item['session']==live['session'] and item['pid']==live['pid'] and item['reused'] for item in reused))
            with self.assertRaises(StudioError) as busy:
                dispatch('runtime.start',{'platform':'ios','device':device})
            self.assertEqual(busy.exception.code,'runtime_configuration_conflict')
            self.assertEqual(busy.exception.details['session'],live['session'])
        finally:
            dispatch('runtime.stop',{'session':live['session']})
        deadline=time.monotonic()+5*PATIENCE
        while Path(live['video_socket']).exists() and time.monotonic()<deadline:time.sleep(.02)
        self.assertFalse(Path(live['video_socket']).exists())
        for owned in (runner,usb):
            with self.assertRaises(ProcessLookupError):os.kill(owned['pid'],0)

    def test_doctor_never_exposes_apple_credentials_and_reports_project_limit(self):
        self.tool('xtool', "import sys\nprint('Not installed' if sys.argv[1]=='sdk' else 'Logged in.\\nTOKEN=private-test-credential')\n")
        self.tool('idevice_id', "print('')\n")
        self.tool('go-ios')
        (self.root/'pubspec.yaml').write_text('flutter:\n')
        result=dispatch('ios.doctor',{'path':str(self.root)})
        self.assertFalse(result['mac_required'])
        self.assertFalse(result['local_modern_ios_emulator'])
        self.assertFalse(result['sdk_ready'])
        self.assertTrue(result['signing_ready'])
        self.assertFalse(result['ready'])
        self.assertNotIn('private-test-credential',json.dumps(result))
        self.assertFalse(next(c for c in result['checks'] if c['name']=='project')['ready'])

    def test_sdk_directory_import_pairing_and_userspace_tunnel_are_real_jobs(self):
        self.tool('xtool');self.tool('idevicepair');self.tool('go-ios')
        sdk=self.root/'Xcode.app';sdk.mkdir()
        job=dispatch('ios.sdk_install',{'path':str(sdk),'slim':True})
        self.assertEqual(job['plan']['steps'][0]['argv'][1:],['sdk','install',str(sdk),'--slim'])
        self.assertEqual(jobs.wait(job['id'],5)['state'],'succeeded')
        device='00008110-0011223344556677'
        for action in ('pair','tunnel','developer-image'):
            job=dispatch('ios.prepare',{'action':action,'device':device})
            args=job['plan']['steps'][0]['argv']
            self.assertNotIn('sudo',args)
            if action=='tunnel':self.assertIn('--userspace',args)
            self.assertEqual(jobs.wait(job['id'],5)['state'],'succeeded')
        with self.assertRaises(StudioError):dispatch('ios.prepare',{'action':'pair','device':'--another-device'})

    def test_runner_digest_failure_preserves_report_and_never_installs(self):
        marker=self.root/'installed'
        self.tool('xtool', f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        self.tool('curl', "import sys\nfrom pathlib import Path\nPath(sys.argv[sys.argv.index('--output')+1]).write_bytes(b'not-the-pinned-runner')\n")
        job=dispatch('ios.wda_prepare',{'device':'00008110-0011223344556677'})
        done=jobs.wait(job['id'],10)
        self.assertEqual(done['state'],'failed',done)
        report=json.loads(Path(done['result']['artifacts'][0]['path']).read_text())
        self.assertEqual(report['error']['code'],'checksum_mismatch')
        self.assertFalse(marker.exists())


if __name__ == "__main__": unittest.main()
