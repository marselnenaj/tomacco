"""One-step virtual iPhone: orchestration logic and its durable operations."""
import itertools
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zlib

for directory in ("tools", "tests"):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / directory))
import ios_virtual_iphone
from native_client import StudioError, dispatch, jobs
from product_clock import ProductClock
from test_product import Isolated

# A stand-in control plane. It records every call and answers from a scenario:
# per operation either one reply or a list consumed in order (the last repeats).
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ["FAKE_ROOT"])
operation, arguments = sys.argv[2], json.loads(sys.argv[3])
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps({"operation": operation, "arguments": arguments,
                          "parent": os.environ.get("TOMACCO_PARENT_JOB")}) + "\n")
scenario = json.loads((root / "scenario.json").read_text())
reply = scenario[operation]
if isinstance(reply, list):
    counter = root / (operation + ".count")
    index = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(index + 1))
    reply = reply[min(index, len(reply) - 1)]
if operation == "ios.emulator.prepare_guest_tools" and reply.get("ok"):
    output = Path(arguments["output"]); output.mkdir(parents=True)
    (output / "system.raw").write_bytes(b"disk")
    (output / "guest-tools-install.json").write_text(json.dumps({
        "state": reply["result"].get("record", "installed"), "graphics_payload": {"state": "restored"},
        "boot_inputs": {"writable_root_disk": str(output / "system.raw"), "trustcache": str(output / "system.tc")}}))
print(json.dumps(reply))
'''


def ok(result):
    return {"ok": True, "result": result}


def refused(reason):
    return ok({"response": {"request_accepted": False, "native_error": {"underlying": {"code_description": reason}}}})


def ready():
    """The guest service answers and no native request is in progress."""
    return ok({"response": {"ok": True, "worker_running": False}})


def launched(request_id, accepted=True, **response):
    """A completed, correlated receipt of the profile's app launch."""
    return ok({"job_id": "guest8", "request_id": request_id, "state": "completed", "ready_for_next_request": True,
               "response": {"request_id": request_id, "operation": "open", "argument": "app.example",
                            "request_accepted": accepted, **response}})


def following(request_id):
    return ok({"job_id": "guest8", "request_id": request_id, "state": "running"})


def submitted(request_id):
    """A launch whose reply was lost after the guest may have executed it."""
    return {"ok": False, "error": {"code": "ios_guest_timeout", "message": "no answer in time",
                                   "details": {"request_id": request_id, "request_may_have_executed": True}}}


def frame(path, *pixel):
    """A 2x2 RGB PNG capture, black unless one pixel is given."""
    rows = b"".join(b"\0" + bytes(pixel or (0, 0, 0)) + bytes(3) for _row in range(2))
    chunk = lambda kind, body: (len(body).to_bytes(4, "big") + kind + body
                                + zlib.crc32(kind + body).to_bytes(4, "big"))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", (2).to_bytes(4, "big") * 2 + bytes((8, 2, 0, 0, 0)))
                     + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    return ok({"result": {"path": str(path), "width": 2, "height": 2}})


def control_error(code, **details):
    """An error reply of the control plane."""
    return {"ok": False, "error": {"code": code, "message": code + " message", "details": details}}


class VirtualPhoneLogic(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tomacco-virtual-iphone-")
        self.root = Path(self.temp.name)
        self.profile = self.root / "profile"
        (self.profile / "installs").mkdir(parents=True)
        self.base = self.root / "elsewhere/guest-install7"
        self.base.mkdir(parents=True)
        self.tools = self.root / "tools7"
        (self.profile / "profile.json").write_text(json.dumps({
            "format": 1, "prepare": {"support": "/support", "helper_kernel": "/missing/vmlinuz", "seconds": 300},
            "boot": {"kernel": "/kernel", "seconds": 7200, "interactive": True},
            "graphics": "/graphics", "engine": "/engine.py", "application": "app.example",
            "installs": str(self.profile / "installs")}))
        self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
        self.cli = self.root / "tomaccoctl"
        self.cli.write_text(FAKE_CLI)
        self.cli.chmod(self.cli.stat().st_mode | stat.S_IXUSR)
        # As inside a durable job: the worker names itself as the parent of
        # every job its step submits.
        self.environment = patch.dict(os.environ, {"FAKE_ROOT": str(self.root), "TOMACCO_PARENT_JOB": "a" * 32})
        self.environment.start()
        # Deadlines are monotonic: a sleep advances a fake clock instead of
        # waiting, so a deadline still expires without real waiting. The clock
        # replaces the product's name `time`, not the time module's functions:
        # subprocess polls the stand-in with time.sleep while it waits for it,
        # and a fake there moved this clock at random (see product_clock).
        self.clock = ProductClock(1000.0)
        self.time = patch.object(ios_virtual_iphone, "time", self.clock)
        self.time.start()
        self.quiet = patch.object(ios_virtual_iphone, "emit", lambda *_phase, **_details: None)
        self.quiet.start()

    def tearDown(self):
        self.quiet.stop()
        self.time.stop()
        self.environment.stop()
        self.temp.cleanup()

    def test_each_phase_change_is_logged_with_its_start_and_a_start_begins_a_new_log(self):
        phone = ios_virtual_iphone.Phone(str(self.cli), self.profile)
        moments = iter([10.0, 20.0, 25.0, 30.0, 40.0, 50.0])
        with patch.object(ios_virtual_iphone.time, "time", lambda: next(moments), create=True):
            phone.save(phase="stopped")
            phone.save(phase="preparing", error=None)
            phone.save(phase="booting")
            # Writing the same phase again, or no phase, keeps its start.
            phone.save(phase="booting", guest_job="b" * 32)
            phone.save(error="x")
            phone.save(phase="unlocking")
        self.assertEqual(self.state()["phase_log"], [
            {"phase": "preparing", "started": 20.0}, {"phase": "booting", "started": 25.0},
            {"phase": "unlocking", "started": 50.0}])
        # A stop goes on in the same log; only the last entries are kept.
        for number in range(40):
            phone.save(phase=f"phase-{number}")
        log = self.state()["phase_log"]
        self.assertEqual(len(log), ios_virtual_iphone.PHASE_LOG_LIMIT)
        self.assertEqual(log[-1]["phase"], "phase-39")

    def write_state(self, **state):
        (self.profile / "state.json").write_text(json.dumps(state))

    def state(self):
        return json.loads((self.profile / "state.json").read_text())

    def scenario(self, **replies):
        defaults = {
            "ios.emulator.prepare_guest_tools": ok({"id": "prepare", "state": "succeeded"}),
            "ios.emulator.paravirtual": ok({"id": "guest8", "state": "queued"}),
            "job.wait": ok({"id": "guest8", "state": "succeeded"}),
            "job.get": ok({"id": "guest8", "state": "running"}),
            # Boot status, then one idle status before each launch attempt.
            "ios.emulator.guest": [ready(), ready(), refused("Locked"), ready(),
                                   ok({"response": {"request_accepted": True}})],
            "ios.emulator.control": ok({"result": {"emulator_exit_requested": True}}),
            "ios.emulator.report": ok({"report": {"exit_code": 0, "storage_hashes_complete": True},
                                       "stop": {"guest_storage_flushed": True}}),
            "job.cancel": ok({"id": "guest8", "state": "cancelled"}),
        }
        defaults.update({name.replace("__", "."): value for name, value in replies.items()})
        (self.root / "scenario.json").write_text(json.dumps(defaults))
        for stale in list(self.root.glob("*.count")) + [self.root / "calls.jsonl"]:
            stale.unlink(missing_ok=True)

    def calls(self, operation=None):
        """Recorded calls; a look at the panel counts as "screenshot", not as a
        control of the phone."""
        path = self.root / "calls.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        for row in rows:
            if row["operation"] == "ios.emulator.control" and row["arguments"].get("action") == "screenshot":
                row["operation"] = "screenshot"
        return [row for row in rows if operation in (None, row["operation"])]

    def phone(self, model=None, unlock="home"):
        return ios_virtual_iphone.Phone(str(self.cli), self.profile, model, unlock)

    def test_a_busy_job_database_is_asked_again_while_waiting(self):
        locked = {"ok": False, "error": {"code": "database_error", "message": "database is locked"}}
        self.scenario(**{"job.wait": [locked, locked, ok({"id": "j", "state": "succeeded"})]})
        with patch.object(ios_virtual_iphone.time, "sleep"):
            self.assertEqual(self.phone().wait({"id": "j", "state": "running"})["state"], "succeeded")
        self.scenario(**{"job.wait": [locked] * 7})
        with patch.object(ios_virtual_iphone.time, "sleep"), self.assertRaisesRegex(RuntimeError, "database is locked"):
            self.phone().wait({"id": "j", "state": "running"})
        self.scenario(**{"job.wait": {"ok": False, "error": {"code": "job_not_found", "message": "gone"}}})
        with self.assertRaisesRegex(RuntimeError, "gone"):
            self.phone().wait({"id": "j", "state": "running"})

    def test_waiting_for_the_stand_in_control_plane_does_not_move_the_fixtures_clock(self):
        # subprocess polls a child it waits for with time.sleep, 1 ms doubling
        # to 50 ms per poll, looked up on the time module at each call. With
        # the fake sleep installed in the module itself, every poll moved this
        # clock for as long as the child stayed unreaped after closing its
        # pipes: 17 s inside one screen_state call in one run, which shifted
        # the launch's 15 s poll cadence and re-armed the lock screen a cycle
        # early (test_an_awake_lock_screen..., about one run in ten, more on a
        # loaded host). A stand-in that closes its pipes and lives on makes
        # that wait certain.
        self.cli.write_text("#!/usr/bin/env python3\nimport os, time\n"
                            "print('{\"ok\": true, \"result\": {\"reaped\": \"late\"}}', flush=True)\n"
                            "os.close(1)\nos.close(2)\ntime.sleep(0.3)\n")
        self.assertEqual(self.phone().invoke("job.get", {"job_id": "a" * 32}), {"ok": True, "result": {"reaped": "late"}})
        self.assertEqual(ios_virtual_iphone.time.monotonic(), 1000.0)
        self.assertEqual((time.sleep.__module__, time.monotonic.__module__), ("time", "time"))

    def test_a_kernel_kept_beside_the_support_module_is_preferred_over_the_running_one(self):
        support = self.root / "support"
        (support / "apfs").mkdir(parents=True)
        (support / "apfs/vmlinuz").write_bytes(b"pinned")
        profile = json.loads((self.profile / "profile.json").read_text())
        profile["prepare"]["support"] = str(support)
        (self.profile / "profile.json").write_text(json.dumps(profile))
        self.scenario()
        self.phone().start(None, True)
        prepare = self.calls("ios.emulator.prepare_guest_tools")[0]["arguments"]
        self.assertEqual(prepare["helper_kernel"], str(support / "apfs/vmlinuz"))

    def test_start_prepares_from_the_verified_run_and_waits_for_ios_to_accept_the_launch(self):
        self.scenario()
        self.phone().start(None, True)
        prepare = self.calls("ios.emulator.prepare_guest_tools")[0]["arguments"]
        output = self.profile / "installs/guest-install8"
        self.assertEqual(prepare["disk"], str(self.base / "system.raw"))
        self.assertEqual((prepare["tools"], prepare["previous_tools"]), (str(self.tools), str(self.tools)))
        self.assertEqual((prepare["graphics"], prepare["output"]), ("/graphics", str(output)))
        # A missing helper kernel falls back to the running kernel, never to an empty path.
        self.assertEqual(prepare["helper_kernel"], f"/usr/lib/modules/{os.uname().release}/vmlinuz")
        boot = self.calls("ios.emulator.paravirtual")[0]["arguments"]
        self.assertEqual((boot["kernel"], boot["engine"], boot["writable_root_disk"]),
                         ("/kernel", "/engine.py", str(output / "system.raw")))
        # The guest is an independent job: it must not end when this start
        # finishes. Preparation stays a child and is cancelled with the start.
        self.assertIsNone(self.calls("ios.emulator.paravirtual")[0]["parent"])
        self.assertEqual(self.calls("ios.emulator.prepare_guest_tools")[0]["parent"], "a" * 32)
        guest = self.calls("ios.emulator.guest")
        # Each launch is submitted only to an idle guest service.
        self.assertEqual([row["arguments"]["operation"] for row in guest],
                         ["status", "status", "open", "status", "open"])
        self.assertEqual(len(self.calls("ios.emulator.control")), 2)  # one Home press per launch attempt
        state = self.state()
        self.assertEqual((state["run"], state["phase"], state["closed"], state["guest_job"]),
                         (8, "launch-accepted", False, "guest8"))
        self.assertTrue(state["application_launch_accepted"])
        # Acceptance by iOS is not a visible scene.
        self.assertFalse(state["application_scene_verified"])
        self.assertEqual(state["launch_receipt"]["state"], "accepted")
        self.assertEqual(state["created"], [str(output)])
        # The base stays the verified run until this one has closed cleanly.
        self.assertEqual(state["verified"]["install"], str(self.base))

    def test_a_kernel_that_looks_for_the_driver_elsewhere_is_refused_before_preparing(self):
        kernel = self.root / "kernelcache"
        profile = json.loads((self.profile / "profile.json").read_text())
        profile["boot"]["kernel"] = str(kernel)
        (self.profile / "profile.json").write_text(json.dumps(profile))
        self.scenario()
        kernel.write_bytes(b"\0<string>../../../private/preboot/tomacco/gpu</string>\0")
        with self.assertRaisesRegex(RuntimeError, "looks for the Metal driver at ../../../private/preboot/tomacco/gpu"):
            self.phone().start(None, True)
        self.assertEqual(self.calls(), [])
        kernel.write_bytes(b"\0<string>" + ios_virtual_iphone.PLUGIN_PERSONALITY.encode() + b"</string>\0")
        self.phone().start(None, True)
        self.assertEqual(self.state()["phase"], "launch-accepted")

    def test_a_start_refuses_a_kernel_that_does_not_pin_the_adapter_cdhash(self):
        # The kernel pins the development-signing adapter's cdhash in its
        # executable-mapping allow-list; a kernel prepared for different guest
        # tools leaves the adapter unmappable and every owned install rejected.
        # The start must refuse before booting such a phone.
        cdhash = "ab" * 20
        kernel = self.root / "kernelcache"
        tools = self.root / "tools-dev"
        tools.mkdir()
        (tools / "guest-tools.json").write_text(json.dumps({"programs": [
            {"path": "usr/libexec/tomacco-platform"},
            {"path": "usr/libexec/libtomacco-development-signing.dylib", "cdhash": cdhash}]}))
        profile = json.loads((self.profile / "profile.json").read_text())
        profile["boot"]["kernel"] = str(kernel)
        profile["tools"] = str(tools)
        (self.profile / "profile.json").write_text(json.dumps(profile))
        self.scenario()
        # A kernel without the adapter's cdhash is refused before any work.
        kernel.write_bytes(b"\0" * 64 + ios_virtual_iphone.PLUGIN_PERSONALITY.encode())
        with self.assertRaisesRegex(RuntimeError, "does not pin the development-signing adapter's cdhash " + cdhash):
            self.phone().start(None, True)
        self.assertEqual(self.calls(), [])
        # A kernel that pins it boots.
        kernel.write_bytes(b"\0" + bytes.fromhex(cdhash) + b"\0" + ios_virtual_iphone.PLUGIN_PERSONALITY.encode())
        self.phone().start(None, True)
        self.assertEqual(self.state()["phase"], "launch-accepted")

    def test_a_base_from_another_trust_cache_and_graphics_is_verified_with_its_own(self):
        profile = json.loads((self.profile / "profile.json").read_text())
        profile["prepare"]["trustcache"] = "/tc-new"
        (self.profile / "profile.json").write_text(json.dumps(profile))
        self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None,
                         trustcache="/tc-old", graphics="/graphics-old")
        self.scenario()
        self.phone().start(None, True)
        prepare = self.calls("ios.emulator.prepare_guest_tools")[0]["arguments"]
        self.assertEqual((prepare["trustcache"], prepare["previous_trustcache"]), ("/tc-new", "/tc-old"))
        self.assertEqual((prepare["graphics"], prepare["previous_graphics"]), ("/graphics", "/graphics-old"))
        self.assertEqual((self.state()["trustcache"], self.state()["graphics"]), ("/tc-new", "/graphics"))
        # A base prepared with the profile's own inputs, or recorded before
        # they were kept, is verified against those.
        for recorded in ({"trustcache": "/tc-new", "graphics": "/graphics"}, {}):
            self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None,
                             **recorded)
            self.scenario()
            self.phone().start(None, True)
            prepare = self.calls("ios.emulator.prepare_guest_tools")[0]["arguments"]
            self.assertNotIn("previous_trustcache", prepare)
            self.assertNotIn("previous_graphics", prepare)

    def test_a_bridge_that_opens_the_compiler_elsewhere_is_refused_before_preparing(self):
        graphics = self.root / "graphics"
        graphics.mkdir()
        profile = json.loads((self.profile / "profile.json").read_text())
        profile["graphics"] = str(graphics)
        (self.profile / "profile.json").write_text(json.dumps(profile))
        self.scenario()
        library = "/" + ios_virtual_iphone.COMPILER_LIBRARY_NAME
        (graphics / "p").write_bytes(b"\0/System/Library/Extensions/../../../private/preboot/tomacco/gpu.bundle"
                                     + library.encode() + b"\0/private/preboot/tomacco/gpu.bundle" + library.encode() + b"\0")
        with self.assertRaisesRegex(RuntimeError, "opens the compiler plugin at /private/preboot/tomacco/gpu.bundle/"):
            self.phone().start(None, True)
        self.assertEqual(self.calls(), [])
        (graphics / "p").write_bytes(b"\0" + (ios_virtual_iphone.BUNDLE_PATH + library).encode() + b"\0")
        self.phone().start(None, True)
        self.assertEqual(self.state()["phase"], "launch-accepted")

    def test_the_start_reports_a_panel_that_stays_black_without_failing(self):
        black, lit = frame(self.root / "black.png"), frame(self.root / "lit.png", 255, 255, 255)
        self.scenario(ios__emulator__control=[ok({"result": {}}), ok({"result": {}}), black, black, black, black])
        self.phone().start(None, True)
        self.assertEqual(len(self.calls("screenshot")), 4)  # looked again for a while before saying so
        self.assertEqual((self.state()["phase"], self.state()["display_visible"]), ("launch-accepted", False))
        self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
        self.scenario(ios__emulator__control=[ok({"result": {}}), ok({"result": {}}), black, lit, black])
        self.phone().start(None, True)
        self.assertEqual((len(self.calls("screenshot")), self.state()["display_visible"]), (2, True))
        # A frame that cannot be read says nothing either way; boot-only does not look.
        self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
        self.scenario()
        self.phone().start(None, True)
        self.assertIsNone(self.state()["display_visible"])
        self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
        self.scenario()
        self.phone().start(None, False)
        self.assertEqual((self.calls("screenshot"), self.state()["display_visible"]), ([], None))

    def test_a_start_names_the_host_software_changed_since_the_last_start_with_a_picture(self):
        from unittest import mock
        lit = frame(self.root / "lit.png", 255, 255, 255)
        black = frame(self.root / "black.png")
        def start(host, *frames):
            self.write_state(**{**self.state(), "run": 7, "install": str(self.base), "tools": str(self.tools),
                                "closed": True, "guest_job": None})
            self.scenario(ios__emulator__control=[ok({"result": {}}), ok({"result": {}}), *frames])
            events = []
            with mock.patch.object(ios_virtual_iphone, "host_software", return_value=host), \
                    mock.patch.object(ios_virtual_iphone, "emit",
                                      lambda phase, **details: events.append({"phase": phase, **details})):
                self.phone().start(None, True)
            return events
        old = {"kernel": "7.2.8-9-omarchy", "mesa": "1:26.2.0-1", "clang": "22.1.8-1"}
        self.assertFalse([e for e in start(old, black, lit) if e["phase"] == "host-changed"])
        self.assertEqual(self.state()["host_software"], old)
        # A start whose panel stays black does not vouch for the host it ran on.
        new = {**old, "mesa": "1:26.3.0-1", "clang": "23.1.3-3"}
        changed = [e for e in start(new, black, black, black, black) if e["phase"] == "host-changed"]
        self.assertEqual(changed[0]["changes"], {"clang": ["22.1.8-1", "23.1.3-3"],
                                                  "mesa": ["1:26.2.0-1", "1:26.3.0-1"]})
        self.assertEqual(self.state()["host_software"], old)
        # Named again on the next start, until one ends with a picture.
        self.assertTrue([e for e in start(new, black, lit) if e["phase"] == "host-changed"])
        self.assertEqual(self.state()["host_software"], new)
        self.assertFalse([e for e in start(new, black, lit) if e["phase"] == "host-changed"])

    def test_the_start_keeps_whether_the_metal_driver_had_to_be_restored(self):
        # The copy holding the preparation record is deleted by later starts.
        self.scenario()
        self.phone().start(None, False)
        self.assertEqual(self.state()["graphics_driver"], "restored")

    def test_host_software_names_the_kernel_and_the_packages_the_package_manager_knows(self):
        from unittest import mock
        done = mock.Mock(stdout="mesa 1:26.2.0-1\nclang 22.1.8-1\nerror: package 'rust' was not found\n")
        with mock.patch.object(ios_virtual_iphone.shutil, "which", return_value="/usr/bin/pacman"), \
                mock.patch.object(ios_virtual_iphone.subprocess, "run", return_value=done):
            record = ios_virtual_iphone.host_software()
        self.assertEqual(record, {"kernel": os.uname().release, "mesa": "1:26.2.0-1", "clang": "22.1.8-1"})
        with mock.patch.object(ios_virtual_iphone.shutil, "which", return_value=None):
            self.assertEqual(ios_virtual_iphone.host_software(), {"kernel": os.uname().release})

    def test_a_device_model_replaces_the_profiles_panel_and_is_unlocked_with_a_swipe(self):
        profile = json.loads((self.profile / "profile.json").read_text())
        profile["boot"]["display_profile"] = "1170x2532@60:65x141"
        (self.profile / "profile.json").write_text(json.dumps(profile))
        self.scenario()
        self.phone("16", "swipe").start(None, True)
        boot = self.calls("ios.emulator.paravirtual")[0]["arguments"]
        self.assertEqual(boot["device_model"], "16")
        self.assertNotIn("display_profile", boot)
        self.assertEqual(self.state()["model"], "16")
        # No Home button on such a device: every unlock wakes the display with
        # the Home usage and follows it at once with a slow swipe up from the
        # bottom edge.
        unlocks = [row["arguments"] for row in self.calls("ios.emulator.control")]
        self.assertEqual([arguments["action"] for arguments in unlocks], ["home", "drag", "home", "drag"])
        for arguments in unlocks[1::2]:
            self.assertEqual((arguments["x"], arguments["to_x"]), (0.5, 0.5))
            self.assertGreater(arguments["y"], 0.97)
            self.assertLess(arguments["to_y"], 0.4)
            self.assertGreaterEqual(arguments["duration_ms"], 1000)
        # Without a model the profile's own device is started, with its Home button.
        self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
        self.scenario()
        self.phone().start(None, True)
        boot = self.calls("ios.emulator.paravirtual")[0]["arguments"]
        self.assertNotIn("device_model", boot)
        self.assertEqual(boot["display_profile"], "1170x2532@60:65x141")
        self.assertEqual(self.state()["model"], "")
        self.assertEqual({row["arguments"]["action"] for row in self.calls("ios.emulator.control")}, {"home"})

    def test_a_launch_that_outlives_the_wait_is_followed_by_its_receipt_not_sent_again(self):
        first, second = "1" * 32, "2" * 32
        self.scenario(ios__emulator__guest=[ready(), ready(), submitted(first)],
                      ios__emulator__guest_result=[following(first), following(first), launched(first)])
        self.phone().start(None, True)
        self.assertEqual([row["arguments"]["operation"] for row in self.calls("ios.emulator.guest")],
                         ["status", "status", "open"])
        self.assertEqual([row["arguments"]["request_id"] for row in self.calls("ios.emulator.guest_result")], [first] * 3)
        # One Home press for the one launch; none while it was in flight.
        self.assertEqual(len(self.calls("ios.emulator.control")), 1)
        state = self.state()
        self.assertEqual((state["phase"], state["application_launch_accepted"]), ("launch-accepted", True))
        self.assertEqual((state["launch_receipt"]["request_id"], state["launch_receipt"]["native_worker_idle_verified"]),
                         (first, True))
        # A busy native worker means nothing was submitted: the launch is
        # asked again once the service is idle, without another Home press.
        self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
        busy = ok({"response": {"ok": False, "error": "guest_operation_busy", "worker_operation": "open",
                                "worker_running": True, "worker_request_id": second}})
        self.scenario(ios__emulator__guest=[ready(), ready(), busy, ready(), ok({"response": {"request_accepted": True}})])
        self.phone().start(None, True)
        self.assertEqual([row["arguments"]["operation"] for row in self.calls("ios.emulator.guest")],
                         ["status", "status", "open", "status", "open"])
        self.assertEqual(self.calls("ios.emulator.guest_result"), [])
        self.assertEqual(len(self.calls("ios.emulator.control")), 1)
        self.assertEqual(self.state()["phase"], "launch-accepted")
        # A start that adopts the running guest follows the launch an earlier
        # start submitted, by its receipt; it is never sent a second time.
        pending = {"job_id": "guest8", "application": "app.example", "state": "pending", "request_id": second}
        self.write_state(run=8, install="/x", tools=str(self.tools), closed=False, guest_job="guest8",
                         launch_receipt=pending,
                         verified={"run": 7, "install": str(self.base), "tools": str(self.tools)})
        self.scenario(ios__emulator__guest=[ready()],
                      ios__emulator__guest_result=[following(second), launched(second)])
        self.phone().start(None, True)
        self.assertEqual([row["arguments"]["operation"] for row in self.calls("ios.emulator.guest")], ["status"])
        self.assertEqual([row["arguments"]["request_id"] for row in self.calls("ios.emulator.guest_result")], [second] * 2)
        self.assertEqual((self.calls("ios.emulator.control"), self.state()["phase"]), ([], "launch-accepted"))
        # When that earlier launch's outcome cannot be established it is
        # reported as uncertain, never replayed.
        self.write_state(run=8, install="/x", tools=str(self.tools), closed=False, guest_job="guest8",
                         launch_receipt=pending,
                         verified={"run": 7, "install": str(self.base), "tools": str(self.tools)})
        self.scenario(ios__emulator__guest=[ready()],
                      ios__emulator__guest_result=ok({"job_id": "guest8", "request_id": second, "state": "unknown"}))
        with self.assertRaises(RuntimeError) as uncertain:
            self.phone().start(None, True)
        self.assertIn("will not be replayed", str(uncertain.exception))
        self.assertEqual([row["arguments"]["operation"] for row in self.calls("ios.emulator.guest")], ["status"])
        self.assertEqual(self.state()["phase"], "launch-uncertain")
        # A followed launch that iOS refused for a reason other than the lock ends the start.
        self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
        disabled = {"native_error": {"underlying": {"code_description": "Disabled"}}}
        self.scenario(ios__emulator__guest=[ready(), ready(), submitted(first)],
                      ios__emulator__guest_result=launched(first, accepted=False, **disabled))
        with self.assertRaises(RuntimeError) as error:
            self.phone().start(None, True)
        self.assertIn("Disabled", str(error.exception))

    def test_a_swipe_device_whose_side_button_cannot_wake_is_unlocked_with_home_then_swipe(self):
        # Observed on a model 16 guest: the side button is acknowledged and the
        # display stays dark; Home wakes it for a few seconds, and a second Home
        # shortly after leaves the passcode-free lock screen.
        capable = ok({"response": {"ok": True, "worker_running": False, "screen_state_supported": True}})

        def screen(locked, awake):
            return ok({"response": {"ok": True, "operation": "screen_state", "screen_state_known": True,
                                    "screen_locked": locked, "passcode_enabled": False,
                                    "screen_awake_known": True, "screen_awake": awake}})

        pressed = ok({"result": {"accepted_by_emulator": True}})
        self.scenario(ios__emulator__guest=[capable, capable, screen(True, False), capable, screen(True, True),
                                            capable, screen(False, True), ok({"response": {"request_accepted": True}})],
                      ios__emulator__control=[ok({"result": {"power_button_supported": False}}),
                                              pressed, pressed, pressed, pressed])
        self.phone("16", "swipe").start(None, True)
        self.assertEqual([row["arguments"]["action"] for row in self.calls("ios.emulator.control")],
                         ["status", "home", "drag", "home", "drag"])
        self.assertEqual([row["arguments"]["operation"] for row in self.calls("ios.emulator.guest")][-2:],
                         ["screen_state", "open"])
        self.assertEqual(self.state()["phase"], "launch-accepted")

    def test_a_screen_query_that_ends_at_its_guest_bound_is_observed_again(self):
        # Guest tools 358 end a screen query SpringBoard leaves unanswered after
        # 20 s; that receipt is an unanswered read, not a failed start.
        capable = ok({"response": {"ok": True, "worker_running": False, "screen_state_supported": True}})
        request = "c" * 32
        timed_out = ok({"job_id": "guest8", "request_id": request,
                        "response": {"operation": "screen_state", "request_id": request, "ok": False,
                                     "error": "guest_worker_timeout"}})
        unlocked = ok({"response": {"ok": True, "operation": "screen_state", "screen_state_known": True,
                                    "screen_locked": False, "passcode_enabled": False}})
        self.scenario(ios__emulator__guest=[capable, capable, timed_out, capable, unlocked,
                                            ok({"response": {"request_accepted": True}})])
        self.phone().start(None, True)
        self.assertEqual(self.calls("ios.emulator.guest_result"), [])
        self.assertEqual([row["arguments"]["operation"] for row in self.calls("ios.emulator.guest")][-2:],
                         ["screen_state", "open"])
        self.assertEqual(self.state()["phase"], "launch-accepted")

    def test_an_awake_lock_screen_that_home_does_not_unlock_is_put_to_sleep_once(self):
        # After SpringBoard restarts, an awake lock screen can take every Home
        # press without unlocking; one side press (sleep) and the ordinary
        # wake path unlock it.
        capable = ok({"response": {"ok": True, "worker_running": False, "screen_state_supported": True}})

        def screen(locked, awake):
            return ok({"response": {"ok": True, "operation": "screen_state", "screen_state_known": True,
                                    "screen_locked": locked, "passcode_enabled": False,
                                    "screen_awake_known": True, "screen_awake": awake}})

        pressed = ok({"result": {"accepted_by_emulator": True}})
        side = ok({"result": {"power_button_supported": True}})
        # Two Home cycles (press, follow-up, then 60 s of polls) and the poll that re-arms.
        stale = [capable, screen(True, True)] * 11
        self.scenario(ios__emulator__guest=[capable] + stale + [capable, screen(True, False), capable, screen(True, True),
                                                    capable, screen(False, True),
                                                    ok({"response": {"request_accepted": True}})],
                      ios__emulator__control=[pressed] * 4 + [side, pressed, side, pressed, pressed])
        self.phone().start(None, True)
        self.assertEqual([row["arguments"]["action"] for row in self.calls("ios.emulator.control")],
                         ["home"] * 4 + ["status", "power_button", "status", "power_button", "home"])
        self.assertEqual([row["arguments"]["operation"] for row in self.calls("ios.emulator.guest")][-2:],
                         ["screen_state", "open"])
        self.assertEqual(self.state()["phase"], "launch-accepted")

    def display_state(self, chosen=3, main=0, **native):
        return ok({"response": {"ok": True, "operation": "display_state", "native_display": {
            "chosen_display_scale_known": chosen is not None, "chosen_display_scale": chosen or 0,
            "main_screen_scale_known": main is not None, "main_screen_scale": main or 0}, **native}})

    def repairing(self, *replies, **others):
        """One start of a guest that can repair the scale; the fake answers in order."""
        capable = ok({"response": {"ok": True, "worker_running": False, "display_state_supported": True,
                                   "display_cache_refresh_supported": True, "ui_restart_supported": True}})
        self.scenario(ios__emulator__guest=[capable, *replies], **others)
        self.phone().start(None, False)
        return [row["arguments"]["operation"] for row in self.calls("ios.emulator.guest")]

    def fresh_start(self, **state):
        self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None, **state)

    def test_a_stale_main_screen_scale_is_repaired_once_and_confirmed_by_a_new_worker(self):
        refreshed = ok({"response": {"ok": True, "operation": "display_refresh", "restart_required": True}})
        restarted = ok({"response": {"ok": True}})
        self.assertEqual(self.repairing(self.display_state(3, 0), refreshed, self.display_state(3, 3), restarted),
                         ["status", "display_state", "display_refresh", "display_state", "restart_ui"])
        record = self.state()["display_scale"]
        self.assertEqual((record["outcome"], record["chosen"], record["main_screen_scale"], record["springboard_restarted"]),
                         ("repaired", 3, 0, True))
        self.assertEqual(self.state()["phase"], "service-ready")
        # The repair persists: the next start reads and does nothing else.
        self.fresh_start(display_scale=record, verified={"run": record["run"], "install": str(self.base), "tools": str(self.tools)})
        self.assertEqual(self.repairing(self.display_state(3, 3.0)), ["status", "display_state"])
        self.assertEqual(self.state()["display_scale"]["outcome"], "current")
        self.assertEqual(self.state()["phase"], "service-ready")

    def test_a_scale_repair_that_does_not_confirm_restarts_nothing_and_is_not_repeated(self):
        refreshed = ok({"response": {"ok": True, "operation": "display_refresh"}})
        self.assertEqual(self.repairing(self.display_state(3, 0), refreshed, self.display_state(3, 0)),
                         ["status", "display_state", "display_refresh", "display_state"])
        record = self.state()["display_scale"]
        self.assertEqual(record["outcome"], "not_confirmed")
        self.assertEqual(self.state()["phase"], "service-ready")
        # A copy made from that run carries the attempt: a read and nothing more.
        self.fresh_start(display_scale=record, verified={"run": record["run"], "install": str(self.base), "tools": str(self.tools)})
        self.assertEqual(self.repairing(self.display_state(3, 0)), ["status", "display_state"])
        # A failure reported by the guest may have changed the cache as well.
        self.fresh_start()
        self.assertEqual(self.repairing(self.display_state(3, 0),
                                        ok({"response": {"ok": False, "error": "display_refresh_helper_not_acknowledged"}})),
                         ["status", "display_state", "display_refresh"])
        record = self.state()["display_scale"]
        self.assertEqual(record["outcome"], "display_refresh_helper_not_acknowledged")
        self.fresh_start(display_scale=record, verified={"run": record["run"], "install": str(self.base), "tools": str(self.tools)})
        self.assertEqual(self.repairing(self.display_state(3, 0)), ["status", "display_state"])
        # A run that was given up did not reach the copy this start boots: its
        # attempt does not stop another. Nor does one for another panel's scale.
        refreshed_again = [self.display_state(3, 0), refreshed, self.display_state(3, 3), ok({"response": {"ok": True}})]
        self.fresh_start(display_scale={**record, "run": 8}, verified={"run": 7, "install": str(self.base), "tools": str(self.tools)})
        self.assertIn("display_refresh", self.repairing(*refreshed_again))
        self.fresh_start(display_scale={**record, "chosen": 2}, verified={"run": 8, "install": str(self.base), "tools": str(self.tools)})
        self.assertIn("display_refresh", self.repairing(*refreshed_again))

    def test_a_scale_refresh_with_a_late_reply_is_followed_not_sent_again(self):
        request = "e" * 32
        self.assertEqual(self.repairing(
            self.display_state(3, 0), submitted(request), self.display_state(3, 3), ok({"response": {"ok": True}}),
            ios__emulator__guest_result=[following(request), ok({
                "job_id": "guest8", "request_id": request, "state": "completed",
                "response": {"ok": True, "operation": "display_refresh", "request_id": request}})]),
            ["status", "display_state", "display_refresh", "display_state", "restart_ui"])
        self.assertEqual(self.state()["display_scale"]["outcome"], "repaired")
        # A busy worker has not begun: asking again is safe, within the bound.
        self.fresh_start()
        busy = ok({"response": {"ok": False, "error": "guest_operation_busy"}})
        self.assertEqual(self.repairing(busy, self.display_state(3, 0), busy, ok({"response": {"ok": True}}),
                                        self.display_state(3, 3), ok({"response": {"ok": True}})),
                         ["status", "display_state", "display_state", "display_refresh", "display_refresh",
                          "display_state", "restart_ui"])

    def test_a_scale_refresh_refused_before_any_work_is_no_attempt(self):
        self.assertEqual(self.repairing(self.display_state(3, 0), control_error("ios_guest_starting")),
                         ["status", "display_state", "display_refresh"])
        self.assertIsNone(self.state()["display_scale"])
        self.assertEqual(self.state()["phase"], "service-ready")
        self.fresh_start(display_scale=None)
        self.assertEqual(self.repairing(self.display_state(3, 0), ok({"response": {"ok": True}}),
                                        self.display_state(3, 3), ok({"response": {"ok": True}})),
                         ["status", "display_state", "display_refresh", "display_state", "restart_ui"])

    def test_the_scale_is_left_alone_when_it_cannot_be_judged_and_the_start_goes_on(self):
        for reply in (self.display_state(0, 0), self.display_state(None, 0), self.display_state(3, None),
                      self.display_state(5, 0), ok({"response": {"ok": False, "error": "display_query_contract_unqualified"}}),
                      control_error("ios_guest_starting")):
            with self.subTest(reply=reply):
                self.fresh_start()
                self.assertEqual(self.repairing(reply), ["status", "display_state"])
                self.assertNotIn("display_scale", self.state())
                self.assertEqual(self.state()["phase"], "service-ready")
        # A guest without the operations is not asked at all.
        self.fresh_start()
        self.scenario(ios__emulator__guest=[ok({"response": {"ok": True, "worker_running": False, "display_state_supported": True}})])
        self.phone().start(None, False)
        self.assertEqual([row["arguments"]["operation"] for row in self.calls("ios.emulator.guest")], ["status"])
        # A control plane that fails outright does not end the start either.
        self.fresh_start()
        with patch.object(ios_virtual_iphone.Phone, "repair_display_scale", side_effect=OSError("no such file")):
            self.assertEqual(self.repairing(), ["status"])
        self.assertEqual(self.state()["phase"], "service-ready")

    def test_the_launch_watchdog_is_scaled_once_per_data_volume(self):
        capable = ok({"response": {"ok": True, "worker_running": False, "launch_watchdog_scale_supported": True}})

        def scaled(previous, scale=8):
            return ok({"response": {"ok": True, "operation": "watchdog_scale", "previous_scale": previous,
                                    "scale": scale}})

        def operations():
            return [row["arguments"]["operation"] for row in self.calls("ios.emulator.guest")]

        def fresh():
            self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)

        # The first guest that can: the default is set and SpringBoard restarted to read it.
        self.scenario(ios__emulator__guest=[capable, scaled(None), ok({"response": {"ok": True}})])
        self.phone().start(None, False)
        self.assertEqual(operations(), ["status", "watchdog_scale", "restart_ui"])
        self.assertEqual(self.calls("ios.emulator.guest")[1]["arguments"]["argument"], "8")
        self.assertEqual((self.state()["launch_watchdog_scale"], self.state()["phase"]), (8, "service-ready"))
        # A run from a base that already carries the value needs no restart.
        fresh()
        self.scenario(ios__emulator__guest=[capable, scaled(8)])
        self.phone().start(None, False)
        self.assertEqual(operations(), ["status", "watchdog_scale"])
        # Guest tools that also scale the scene allowances report the resume
        # scale: a base that carries only the launch scale is restarted once,
        # and one that carries both is not.
        def both(previous, previous_resume, scale=8):
            return ok({"response": {"ok": True, "operation": "watchdog_scale", "previous_scale": previous,
                                    "scale": scale, "previous_resume_scale": previous_resume,
                                    "resume_scale": scale}})
        fresh()
        self.scenario(ios__emulator__guest=[capable, both(8, None), ok({"response": {"ok": True}})])
        self.phone().start(None, False)
        self.assertEqual(operations(), ["status", "watchdog_scale", "restart_ui"])
        fresh()
        self.scenario(ios__emulator__guest=[capable, both(8, 8)])
        self.phone().start(None, False)
        self.assertEqual(operations(), ["status", "watchdog_scale"])
        # Declining watchdog terminations is read at SpringBoard's start too.
        fresh()
        self.scenario(ios__emulator__guest=[capable, ok({"response": {
            "ok": True, "operation": "watchdog_scale", "previous_scale": 8, "scale": 8, "previous_resume_scale": 8,
            "resume_scale": 8, "previous_no_watchdogs": None, "no_watchdogs": 1}}), ok({"response": {"ok": True}})])
        self.phone().start(None, False)
        self.assertEqual(operations(), ["status", "watchdog_scale", "restart_ui"])
        # A reply that is late (the preference daemon right after a boot) is
        # followed through its receipt, not sent again.
        request = "d" * 32
        fresh()
        self.scenario(ios__emulator__guest=[capable, submitted(request), ok({"response": {"ok": True}})],
                      ios__emulator__guest_result=[
                          following(request),
                          ok({"job_id": "guest8", "request_id": request, "state": "completed",
                              "response": {"ok": True, "operation": "watchdog_scale", "request_id": request,
                                           "previous_scale": 8, "scale": 8, "previous_resume_scale": None,
                                           "resume_scale": 8}})])
        self.phone().start(None, False)
        self.assertEqual(operations(), ["status", "watchdog_scale", "restart_ui"])
        self.assertEqual(len(self.calls("ios.emulator.guest_result")), 2)
        self.assertEqual(self.state()["launch_watchdog_scale"], 8)
        # A receipt that never completes leaves the start going without it.
        fresh()
        self.scenario(ios__emulator__guest=[capable, submitted(request)],
                      ios__emulator__guest_result=ok({"job_id": "guest8", "request_id": request,
                                                      "state": "unknown"}))
        self.phone().start(None, False)
        self.assertEqual(operations(), ["status", "watchdog_scale"])
        self.assertEqual(self.state()["phase"], "service-ready")
        # The profile names its own factor. A refusal leaves iOS's own allowances
        # and is no reason to stop the start.
        profile = json.loads((self.profile / "profile.json").read_text())
        (self.profile / "profile.json").write_text(json.dumps({**profile, "launch_watchdog_scale": 12}))
        fresh()
        self.scenario(ios__emulator__guest=[capable, ok({"response": {"ok": False, "error": "virtual_setup_required"}})])
        self.phone().start(None, False)
        self.assertEqual(operations(), ["status", "watchdog_scale"])
        self.assertEqual(self.calls("ios.emulator.guest")[1]["arguments"]["argument"], "12")
        self.assertNotIn("launch_watchdog_scale", self.state())
        self.assertEqual(self.state()["phase"], "service-ready")
        # A guest without the operation is never asked.
        fresh()
        self.scenario()
        self.phone().start(None, False)
        self.assertEqual(operations(), ["status"])

    def test_the_hosts_time_zone_is_given_to_a_guest_that_can_take_it(self):
        capable = ok({"response": {"ok": True, "worker_running": False, "time_zone_supported": True}})
        lines = []

        def zoned(previous, zone="Europe/Vienna"):
            return ok({"response": {"ok": True, "operation": "time_zone", "time_zone": zone,
                                    "previous_link": "/var/db/timezone/zoneinfo/" + previous,
                                    "link": "/var/db/timezone/zoneinfo/" + zone}})

        def operations():
            return [row["arguments"]["operation"] for row in self.calls("ios.emulator.guest")]

        def fresh():
            self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
            lines.clear()

        def start():
            with patch.object(ios_virtual_iphone, "emit", lambda phase, **details: lines.append({"phase": phase, **details})):
                self.phone().start(None, False)
            return [line for line in lines if line["phase"] == "time-zone"]

        with patch.object(ios_virtual_iphone, "host_time_zone", lambda: "Europe/Vienna"):
            # The image's zone is replaced by the host's; the start goes on to its end.
            self.scenario(ios__emulator__guest=[capable, zoned("America/Los_Angeles")])
            self.assertEqual(start(), [{"phase": "time-zone", "outcome": "applied", "zone": "Europe/Vienna",
                                        "previous_link": "/var/db/timezone/zoneinfo/America/Los_Angeles",
                                        "link": "/var/db/timezone/zoneinfo/Europe/Vienna", "changed": True}])
            self.assertEqual(operations(), ["status", "time_zone"])
            self.assertEqual(self.calls("ios.emulator.guest")[1]["arguments"],
                             {"job_id": "guest8", "operation": "time_zone", "argument": "Europe/Vienna",
                              "timeout_seconds": 30})
            # A worker busy with an earlier request refuses before any work:
            # the zone is asked again instead of being given up.
            fresh()
            busy = ok({"response": {"ok": False, "error": "guest_operation_busy"}})
            with patch.object(ios_virtual_iphone.time, "sleep", lambda seconds: None):
                self.scenario(ios__emulator__guest=[capable, busy, busy, zoned("America/Los_Angeles")])
                self.assertEqual([line["outcome"] for line in start()], ["applied"])
            self.assertEqual(operations(), ["status", "time_zone", "time_zone", "time_zone"])
            self.assertEqual(self.state()["time_zone"]["outcome"], "applied")
            self.assertEqual(self.state()["phase"], "service-ready")
            # A base that already carries the zone: asked again (harmless), nothing changed.
            fresh()
            self.scenario(ios__emulator__guest=[capable, zoned("Europe/Vienna")])
            self.assertEqual(start()[0]["changed"], False)
            self.assertEqual(self.state()["time_zone"]["changed"], False)
            # Guest tools without the operation are not asked.
            fresh()
            self.scenario()
            self.assertEqual(start(), [{"phase": "time-zone", "outcome": "skipped", "reason": "unsupported",
                                        "zone": "Europe/Vienna"}])
            self.assertEqual(operations(), ["status"])
            self.assertEqual(self.state()["time_zone"], {"zone": "Europe/Vienna", "outcome": "skipped",
                                                         "reason": "unsupported"})
            # A control service without the tzlink entitlement refuses; the
            # refusal is recorded and the start is not failed by it.
            fresh()
            self.scenario(ios__emulator__guest=[capable, ok({"response": {
                "ok": False, "operation": "time_zone", "error": "time_zone_not_permitted", "link_error": 1}})])
            self.assertEqual(start(), [{"phase": "time-zone", "outcome": "time_zone_not_permitted",
                                        "zone": "Europe/Vienna"}])
            self.assertEqual(self.state()["time_zone"], {"zone": "Europe/Vienna", "outcome": "time_zone_not_permitted"})
            self.assertEqual(self.state()["phase"], "service-ready")
            # A tomaccoctl that does not know the operation refuses it on the host; same.
            fresh()
            self.scenario(ios__emulator__guest=[capable, {"ok": False, "error": {"code": "invalid_guest_request",
                                                                                "message": "Use ..."}}])
            self.assertEqual(start()[0]["outcome"], "invalid_guest_request")
            self.assertEqual(self.state()["phase"], "service-ready")
            # A late reply (the link daemon starts on demand) is followed
            # through its receipt and never sent again.
            request = "e" * 32
            fresh()
            self.scenario(ios__emulator__guest=[capable, submitted(request)],
                          ios__emulator__guest_result=[
                              following(request),
                              ok({"job_id": "guest8", "request_id": request, "state": "completed",
                                  "response": {"ok": True, "operation": "time_zone", "request_id": request,
                                               "time_zone": "Europe/Vienna",
                                               "previous_link": "/var/db/timezone/zoneinfo/America/Los_Angeles",
                                               "link": "/var/db/timezone/zoneinfo/Europe/Vienna"}})])
            self.assertEqual(start()[0]["outcome"], "applied")
            self.assertEqual(operations(), ["status", "time_zone"])
            self.assertEqual(len(self.calls("ios.emulator.guest_result")), 2)
            # A receipt that never completes leaves the outcome to the next start.
            fresh()
            self.scenario(ios__emulator__guest=[capable, submitted(request)],
                          ios__emulator__guest_result=ok({"job_id": "guest8", "request_id": request,
                                                          "state": "unknown"}))
            self.assertEqual(start()[0]["outcome"], "ios_guest_timeout")
            self.assertEqual(self.state()["phase"], "service-ready")
        # A host whose zone is not a zoneinfo name has nothing to give.
        with patch.object(ios_virtual_iphone, "host_time_zone", lambda: None):
            fresh()
            self.scenario(ios__emulator__guest=[capable, zoned("America/Los_Angeles")])
            self.assertEqual(start(), [{"phase": "time-zone", "outcome": "skipped", "reason": "host_zone_unknown",
                                        "zone": None}])
            self.assertEqual(operations(), ["status"])

    def test_the_start_turns_glass_and_motion_into_plain_surfaces(self):
        capable = ok({"response": {"ok": True, "worker_running": False, "display_effects_supported": True}})
        lines = []

        def effects(previous, now=True):
            return ok({"response": {"ok": True, "operation": "effects", "argument": "reduced",
                                    "previous_reduce_transparency": previous, "previous_reduce_motion": previous,
                                    "reduce_transparency": now, "reduce_motion": now}})

        def operations():
            return [(row["arguments"]["operation"], row["arguments"].get("argument"))
                    for row in self.calls("ios.emulator.guest")]

        def fresh():
            self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
            lines.clear()

        def start():
            with patch.object(ios_virtual_iphone, "emit", lambda phase, **details: lines.append({"phase": phase, **details})):
                self.phone().start(None, False)
            return [line for line in lines if line["phase"] == "effects"]

        # The blurred materials cost most of the renderer: reduced by default.
        fresh()
        self.scenario(ios__emulator__guest=[capable, effects(False)])
        self.assertEqual(start(), [{"phase": "effects", "outcome": "applied", "wanted": "reduced",
                                    "reduce_transparency": True, "reduce_motion": True, "changed": True}])
        self.assertEqual(operations(), [("status", None), ("effects", "reduced")])
        self.assertEqual(self.state()["effects"]["outcome"], "applied")
        # A busy worker is asked again; a refusal is recorded and the start goes on.
        fresh()
        self.scenario(ios__emulator__guest=[capable, ok({"response": {"ok": False, "error": "guest_operation_busy"}}),
                                            effects(True)])
        with patch.object(ios_virtual_iphone.time, "sleep"):
            self.assertEqual(start()[0]["changed"], False)
        refused = ok({"response": {"ok": False, "operation": "effects", "error": "effects_not_applied"}})
        # Reduce Motion takes effect after its read-back: asked once more.
        fresh()
        self.scenario(ios__emulator__guest=[capable, refused, effects(True)])
        with patch.object(ios_virtual_iphone.time, "sleep"):
            self.assertEqual(start()[0]["outcome"], "applied")
        self.assertEqual(operations(), [("status", None), ("effects", "reduced"), ("effects", "reduced")])
        fresh()
        self.scenario(ios__emulator__guest=[capable, refused, refused])
        with patch.object(ios_virtual_iphone.time, "sleep"):
            self.assertEqual(start(), [{"phase": "effects", "outcome": "effects_not_applied", "wanted": "reduced"}])
        self.assertEqual(self.state()["phase"], "service-ready")
        # Guest tools without the operation are left as they are.
        fresh()
        self.scenario(ios__emulator__guest=[ok({"response": {"ok": True, "worker_running": False}})])
        self.assertEqual(start(), [{"phase": "effects", "outcome": "skipped", "reason": "unsupported"}])
        # The profile keeps iOS's own look when it asks for it.
        profile = json.loads((self.profile / "profile.json").read_text())
        profile["effects"] = "original"
        (self.profile / "profile.json").write_text(json.dumps(profile))
        fresh()
        self.scenario(ios__emulator__guest=[capable, effects(True, now=False)])
        start()
        self.assertEqual(operations(), [("status", None), ("effects", "original")])

    def test_a_guest_that_can_take_it_is_kept_from_ever_locking(self):
        capable = ok({"response": {"ok": True, "worker_running": False, "auto_lock_supported": True}})
        lines = []

        def never(previous, method="managed_configuration", **extra):
            return ok({"response": {"ok": True, "operation": "auto_lock", "argument": "never", "auto_lock": "never",
                                    "method": method, "previous_max_inactivity": previous,
                                    "max_inactivity": 2147483647, "user_max_inactivity": 2147483647, **extra}})

        def operations():
            return [row["arguments"]["operation"] for row in self.calls("ios.emulator.guest")]

        def fresh():
            self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
            lines.clear()

        def start():
            with patch.object(ios_virtual_iphone, "emit", lambda phase, **details: lines.append({"phase": phase, **details})):
                self.phone().start(None, False)
            return [line for line in lines if line["phase"] == "auto-lock"]

        # The image's 30 seconds become Never; the start goes on to its end.
        fresh()
        self.scenario(ios__emulator__guest=[capable, never(30)])
        self.assertEqual(start(), [{"phase": "auto-lock", "outcome": "applied", "method": "managed_configuration",
                                    "previous_max_inactivity": 30, "max_inactivity": 2147483647, "changed": True}])
        self.assertEqual(operations(), ["status", "auto_lock"])
        self.assertEqual(self.calls("ios.emulator.guest")[1]["arguments"],
                         {"job_id": "guest8", "operation": "auto_lock", "argument": "never", "timeout_seconds": 90})
        self.assertEqual(self.state()["auto_lock"]["outcome"], "applied")
        self.assertEqual(self.state()["phase"], "service-ready")
        # A base that already carries Never: asked again (harmless), nothing changed.
        fresh()
        self.scenario(ios__emulator__guest=[capable, never(2147483647)])
        self.assertIs(start()[0]["changed"], False)
        # SpringBoard's own switch when profiled did not take the value.
        fresh()
        self.scenario(ios__emulator__guest=[capable, never(30, "springboard_defaults", max_inactivity=30,
                                                           previous_springboard_dont_lock_ever=None,
                                                           springboard_dont_lock_ever=True)])
        self.assertEqual(start()[0], {"phase": "auto-lock", "outcome": "applied", "method": "springboard_defaults",
                                      "previous_max_inactivity": 30, "max_inactivity": 30, "changed": True})
        # A worker busy with an earlier request refuses before any work: asked again.
        fresh()
        busy = ok({"response": {"ok": False, "error": "guest_operation_busy"}})
        with patch.object(ios_virtual_iphone.time, "sleep", lambda seconds: None):
            self.scenario(ios__emulator__guest=[capable, busy, busy, never(30)])
            self.assertEqual([line["outcome"] for line in start()], ["applied"])
        self.assertEqual(operations(), ["status", "auto_lock", "auto_lock", "auto_lock"])
        # Guest tools without the operation are not asked.
        fresh()
        self.scenario()
        self.assertEqual(start(), [{"phase": "auto-lock", "outcome": "skipped", "reason": "unsupported"}])
        self.assertEqual(operations(), ["status"])
        self.assertEqual(self.state()["auto_lock"], {"outcome": "skipped", "reason": "unsupported"})
        # A refusal is recorded and does not fail the start.
        fresh()
        self.scenario(ios__emulator__guest=[capable, ok({"response": {
            "ok": False, "operation": "auto_lock", "error": "auto_lock_not_applied"}})])
        self.assertEqual(start(), [{"phase": "auto-lock", "outcome": "auto_lock_not_applied"}])
        self.assertEqual(self.state()["auto_lock"], {"outcome": "auto_lock_not_applied"})
        self.assertEqual(self.state()["phase"], "service-ready")
        # A tomaccoctl that does not know the operation refuses it on the host; same.
        fresh()
        self.scenario(ios__emulator__guest=[capable, {"ok": False, "error": {"code": "invalid_guest_request",
                                                                            "message": "Use ..."}}])
        self.assertEqual(start()[0]["outcome"], "invalid_guest_request")
        self.assertEqual(self.state()["phase"], "service-ready")
        # A late reply (profiled starts on demand) is followed through its receipt.
        request = "f" * 32
        fresh()
        self.scenario(ios__emulator__guest=[capable, submitted(request)],
                      ios__emulator__guest_result=[
                          following(request),
                          ok({"job_id": "guest8", "request_id": request, "state": "completed",
                              "response": {"ok": True, "operation": "auto_lock", "request_id": request,
                                           "auto_lock": "never", "method": "managed_configuration",
                                           "previous_max_inactivity": 30, "max_inactivity": 2147483647}})])
        self.assertEqual(start()[0]["outcome"], "applied")
        self.assertEqual(operations(), ["status", "auto_lock"])
        self.assertEqual(len(self.calls("ios.emulator.guest_result")), 2)
        # A receipt that never completes: Never is asked once more (a first
        # request can time out after it applied), and a second timeout
        # leaves the outcome to the next start.
        fresh()
        self.scenario(ios__emulator__guest=[capable, submitted(request), submitted(request)],
                      ios__emulator__guest_result=ok({"job_id": "guest8", "request_id": request,
                                                      "state": "unknown"}))
        self.assertEqual(start()[0]["outcome"], "ios_guest_timeout")
        self.assertEqual(operations(), ["status", "auto_lock", "auto_lock"])
        self.assertEqual(self.state()["phase"], "service-ready")
        fresh()
        self.scenario(ios__emulator__guest=[capable, submitted(request), never(2147483647)],
                      ios__emulator__guest_result=ok({"job_id": "guest8", "request_id": request,
                                                      "state": "unknown"}))
        self.assertEqual(start()[0]["outcome"], "applied")
        self.assertEqual(operations(), ["status", "auto_lock", "auto_lock"])

    def test_the_hosts_zone_is_read_from_the_localtime_link_or_tz(self):
        valid = ios_virtual_iphone.valid_time_zone
        for name in ("UTC", "Europe/Vienna", "America/Argentina/Buenos_Aires", "Etc/GMT+1", "A" * 32):
            self.assertTrue(valid(name), name)
        for name in ("", "/UTC", "UTC/", "Europe//Vienna", "A/B/C/D", "../zoneinfo", "Europe/Vienna.",
                     "Europe Vienna", "Europe/Zürich", "A" * 33, "CET-1CEST,M3.5.0,M10.5.0/3", None, 7):
            self.assertFalse(valid(name), repr(name))

        def zone(link, tz=None):
            environment = {"TZ": tz} if tz is not None else {}
            with patch.dict(os.environ, environment, clear=False):
                if tz is None:
                    os.environ.pop("TZ", None)
                read = (lambda path: link) if link is not None else None

                def readlink(path):
                    if read is None:
                        raise OSError(22, "not a link")
                    return read(path)
                with patch.object(ios_virtual_iphone.os, "readlink", readlink):
                    return ios_virtual_iphone.host_time_zone()

        self.assertEqual(zone("/usr/share/zoneinfo/Europe/Vienna"), "Europe/Vienna")
        self.assertEqual(zone("../usr/share/zoneinfo/America/Argentina/Buenos_Aires"), "America/Argentina/Buenos_Aires")
        self.assertEqual(zone("/usr/share/zoneinfo/Europe/Vienna", "UTC"), "Europe/Vienna")
        # A copied file, a target outside the zoneinfo tree, or no link: TZ speaks.
        self.assertEqual(zone(None, "UTC"), "UTC")
        self.assertEqual(zone(None, ":Europe/Berlin"), "Europe/Berlin")
        self.assertEqual(zone(None, "/usr/share/zoneinfo/Etc/GMT+1"), "Etc/GMT+1")
        self.assertEqual(zone("/etc/my-zone", "Asia/Tokyo"), "Asia/Tokyo")
        self.assertIsNone(zone(None, "CET-1CEST,M3.5.0,M10.5.0/3"))
        self.assertIsNone(zone(None, ""))
        self.assertIsNone(zone(None))
        self.assertIsNone(zone("/usr/share/zoneinfo/", None))

    def test_a_running_guest_is_adopted_and_boot_only_skips_the_launch(self):
        self.write_state(run=8, install="/x", tools="/t", closed=False, guest_job="guest8",
                         verified={"run": 7, "install": str(self.base), "tools": str(self.tools)})
        self.scenario()
        self.phone().start(None, False)
        self.assertEqual(self.calls("ios.emulator.prepare_guest_tools") + self.calls("ios.emulator.paravirtual"), [])
        self.assertEqual([row["arguments"]["operation"] for row in self.calls("ios.emulator.guest")], ["status"])
        self.assertEqual(self.state()["phase"], "service-ready")
        self.assertFalse(self.state()["application_launch_accepted"])

    def test_an_unverified_run_is_never_the_base_of_the_next_start(self):
        unverified = self.profile / "installs/guest-install8"
        unverified.mkdir()
        self.write_state(run=8, install=str(unverified), tools="/other-tools", closed=False, guest_job="old",
                         created=[str(unverified)],
                         verified={"run": 7, "install": str(self.base), "tools": str(self.tools)})
        self.scenario(job__get=ok({"id": "old", "state": "failed"}),
                      ios__emulator__report=ok({"report": {"exit_code": 0, "storage_hashes_complete": False}}))
        self.phone().start(None, False)
        prepare = self.calls("ios.emulator.prepare_guest_tools")[0]["arguments"]
        self.assertEqual(prepare["disk"], str(self.base / "system.raw"))
        self.assertEqual(prepare["previous_tools"], str(self.tools))
        self.assertTrue(prepare["output"].endswith("guest-install9"))
        self.assertTrue(unverified.is_dir())  # kept for inspection, just not reused
        # A report that cannot be read is unverified too, not an error that blocks every start.
        self.write_state(run=9, install="/x", tools="/t", closed=False, guest_job="gone",
                         verified={"run": 7, "install": str(self.base), "tools": str(self.tools)})
        self.scenario(job__get={"ok": False, "error": {"code": "job_not_found", "message": "unknown"}},
                      ios__emulator__report={"ok": False, "error": {"code": "job_not_found", "message": "unknown"}})
        self.phone().start(None, False)
        self.assertEqual(self.calls("ios.emulator.prepare_guest_tools")[0]["arguments"]["disk"],
                         str(self.base / "system.raw"))
        # A control plane that cannot be asked right now is not an answer:
        # the run is preserved and nothing new is prepared.
        self.write_state(run=9, install="/x", tools="/t", closed=False, guest_job="guest9",
                         verified={"run": 7, "install": str(self.base), "tools": str(self.tools)})
        self.scenario(job__get={"ok": False, "error": {"code": "database_locked", "message": "locked"}})
        with self.assertRaises(ios_virtual_iphone.GuestObservationError):
            self.phone().start(None, False)
        self.assertEqual(self.calls("ios.emulator.prepare_guest_tools"), [])
        self.assertEqual((self.state()["run"], self.state()["guest_job"]), (9, "guest9"))

    def test_stop_verifies_storage_and_prunes_only_its_own_older_copies(self):
        installs = self.profile / "installs"
        copies = [installs / f"guest-install{number}" for number in (8, 9, 10)]
        for copy in copies:
            copy.mkdir()
        outside = self.root / "elsewhere/guest-install6"
        outside.mkdir()
        self.write_state(run=10, install=str(copies[2]), tools=str(self.tools), closed=False, guest_job="guest10",
                         created=[str(outside)] + [str(copy) for copy in copies],
                         verified={"run": 7, "install": str(self.base), "tools": str(self.tools)})
        self.scenario(job__get=ok({"id": "guest10", "state": "running"}))
        self.phone().stop()
        self.assertEqual([row["arguments"]["action"] for row in self.calls("ios.emulator.control")], ["stop"])
        state = self.state()
        self.assertEqual((state["closed"], state["phase"]), (True, "stopped"))
        self.assertEqual(state["verified"], {"run": 10, "install": str(copies[2]), "tools": str(self.tools)})
        self.assertFalse(copies[0].exists())
        self.assertTrue(copies[1].is_dir() and copies[2].is_dir())
        # A recorded path outside the profile's installs directory is never removed.
        self.assertTrue(outside.is_dir() and self.base.is_dir())
        self.assertEqual(state["created"], [str(outside), str(copies[1]), str(copies[2])])
        self.assertTrue(state["verdict"]["verified"])
        # Unverified storage is reported as a failure and keeps the earlier base.
        self.write_state(run=11, install="/x", tools="/t", closed=False, guest_job="guest11",
                         verified={"run": 10, "install": str(copies[2]), "tools": str(self.tools)})
        self.scenario(job__get=ok({"id": "guest11", "state": "running"}),
                      ios__emulator__report=ok({"report": {"exit_code": 0, "storage_hashes_complete": False},
                                                "stop": {"guest_storage_flushed": True}}))
        with self.assertRaises(RuntimeError):
            self.phone().stop()
        self.assertEqual((self.state()["closed"], self.state()["phase"]), (False, "unverified"))
        self.assertEqual(self.state()["verified"]["run"], 10)

    def test_a_start_drops_the_rollback_copy_before_it_copies_the_base(self):
        installs = self.profile / "installs"
        older, base, unclosed = (installs / f"guest-install{number}" for number in (8, 9, 10))
        for copy in (older, base):
            copy.mkdir()
        verified = {"run": 9, "install": str(base), "tools": str(self.tools)}
        self.write_state(run=9, install=str(base), tools=str(self.tools), closed=True, guest_job=None,
                         created=[str(older), str(base)], verified=verified)
        # Removed before the copy is made, so also when the copy then fails.
        self.scenario(ios__emulator__prepare_guest_tools=ok({"id": "prepare", "state": "failed"}))
        with self.assertRaises(RuntimeError):
            self.phone().start(None, False)
        self.assertFalse(older.exists())
        self.assertEqual(list(installs.iterdir()), [base])
        self.assertEqual(self.state()["created"], [str(base)])
        # The copy of a last run that did not close is kept for inspection.
        unclosed.mkdir()
        self.write_state(**{**self.state(), "run": 10, "install": str(unclosed), "closed": False,
                            "guest_job": "guest10", "created": [str(base), str(unclosed)]})
        self.scenario(job__get=ok({"id": "guest10", "state": "succeeded"}),
                      ios__emulator__report=ok({"report": {"exit_code": 0, "storage_hashes_complete": False},
                                                "stop": {"guest_storage_flushed": True}}))
        self.phone().start(None, False)
        new = installs / "guest-install11"
        self.assertTrue(base.is_dir() and unclosed.is_dir() and new.is_dir())
        self.assertEqual(self.state()["created"], [str(base), str(unclosed), str(new)])

    def test_an_overlay_start_asks_for_an_overlay_and_keeps_the_copies_it_reads(self):
        from test_ios_disk_overlay import write_qcow2
        profile = json.loads((self.profile / "profile.json").read_text())
        helper = self.root / "helper/qemu-system-x86_64"
        helper.parent.mkdir()
        (helper.parent / "qemu-img").write_text("#!/bin/sh\n")
        (helper.parent / "qemu-img").chmod(0o755)
        profile["prepare"]["helper_engine"] = str(helper)
        profile["start_disks"] = "overlay"
        (self.profile / "profile.json").write_text(json.dumps(profile))
        installs = self.profile / "installs"
        older, base = installs / "guest-install8", installs / "guest-install9"
        for copy in (older, base):
            copy.mkdir()
        (older / "system.raw").write_bytes(bytes(8192))
        write_qcow2(base / "system.qcow2", 8192, backing=str(older / "system.raw"))
        verified = {"run": 9, "install": str(base), "tools": str(self.tools)}
        self.write_state(run=9, install=str(base), tools=str(self.tools), closed=True, guest_job=None,
                         created=[str(older), str(base)], verified=verified)
        self.scenario(ios__emulator__prepare_guest_tools=ok({"id": "prepare", "state": "failed"}))
        with self.assertRaises(RuntimeError):
            self.phone().start(None, False)
        prepare = self.calls("ios.emulator.prepare_guest_tools")[0]["arguments"]
        self.assertEqual(prepare["disk"], str(base / "system.qcow2"))
        self.assertEqual((prepare["root_overlay"], prepare["qemu_img"]), (True, str(helper.parent / "qemu-img")))
        # The older copy is the verified base's backing file: the start's prune keeps it.
        self.assertTrue(older.is_dir())
        self.assertEqual(self.state()["created"], [str(older), str(base)])
        # Without qemu-img the start copies and says so; an overlay is never asked for.
        (helper.parent / "qemu-img").unlink()
        events = []
        with patch.object(ios_virtual_iphone, "emit", lambda phase, **details: events.append((phase, details))), \
                patch.object(ios_virtual_iphone.ios_disk_overlay.shutil, "which", lambda _name: None):
            with self.assertRaises(RuntimeError):
                self.phone().start(None, False)
        prepare = self.calls("ios.emulator.prepare_guest_tools")[-1]["arguments"]
        self.assertFalse(prepare["root_overlay"])
        self.assertNotIn("qemu_img", prepare)
        self.assertIn(("start-disks", {"method": "copy", "reason": "no qemu-img beside the helper engine or on PATH"}),
                      events)

    def test_a_stop_without_confirmed_flush_never_becomes_the_base(self):
        current = self.profile / "installs/guest-install8"
        current.mkdir()
        verified = {"run": 7, "install": str(self.base), "tools": str(self.tools)}
        clean = {"exit_code": 0, "storage_hashes_complete": True}
        # No record at all (the guest ended by itself), and a recorded refusal.
        for stop in (None, {"guest_storage_flushed": False}, {"guest_storage_flushed": "yes"}):
            self.write_state(run=8, install=str(current), tools=str(self.tools), closed=False, guest_job="guest8",
                             created=[str(current)], verified=verified)
            self.scenario(ios__emulator__report=ok({"report": clean, "stop": stop}))
            with self.assertRaises(RuntimeError) as error:
                self.phone().stop()
            self.assertIn("without verified storage", str(error.exception))
            state = self.state()
            self.assertEqual((state["closed"], state["phase"], state["verified"]), (False, "unverified", verified))
            self.assertEqual(state["outcome"], {"outcome": None, **clean, "guest_storage_flushed": False})
            # The same evidence in plain words, in the state and in the stop's error.
            self.assertFalse(state["verdict"]["verified"])
            self.assertIn("ios.virtual.stop", state["verdict"]["remedy"])
            self.assertIn("flush", str(error.exception))
            if stop is None:
                self.assertIn("No stop asked the guest to flush its storage first", str(error.exception))
        self.assertTrue(current.is_dir())

    def test_a_failed_or_interrupted_start_does_not_block_or_outlive_the_next_one(self):
        installs = self.profile / "installs"
        # Preparation created the copy and then failed.
        self.scenario(ios__emulator__prepare_guest_tools=ok({"id": "prepare", "state": "failed"}))
        with self.assertRaises(RuntimeError):
            self.phone().start(None, False)
        self.assertEqual(list(installs.iterdir()), [])
        state = self.state()
        self.assertEqual((state["run"], state["attempted"], state["attempt"], state.get("created", [])), (7, 8, None, []))
        # The start was killed while it prepared: only the record of the attempt is left.
        leftover = installs / "guest-install9"
        leftover.mkdir()
        (leftover / "system.raw").write_bytes(b"partial")
        self.write_state(**{**state, "attempted": 9, "attempt": {"run": 9, "install": str(leftover)}})
        # A copy this state knows nothing about is never touched, only avoided.
        unknown = installs / "guest-install10"
        unknown.mkdir()
        self.scenario()
        self.phone().start(None, False)
        self.assertFalse(leftover.exists())
        self.assertTrue(unknown.is_dir())
        state = self.state()
        self.assertEqual((state["run"], state["attempt"], state["created"]),
                         (11, None, [str(installs / "guest-install11")]))
        # A leftover that a guest still has locked is in use, not garbage.
        busy = installs / "guest-install12"
        busy.mkdir()
        (busy / "system.raw").write_bytes(b"live")
        self.write_state(**{**state, "closed": True, "guest_job": None, "attempted": 12,
                            "attempt": {"run": 12, "install": str(busy)}})
        self.scenario()
        with (busy / "system.raw").open("rb") as held:
            ios_virtual_iphone.fcntl.flock(held, ios_virtual_iphone.fcntl.LOCK_EX)
            with self.assertRaises(RuntimeError) as error:
                self.phone().start(None, False)
        self.assertIn("is in use", str(error.exception))
        self.assertTrue(busy.is_dir())
        self.assertEqual(self.calls("ios.emulator.prepare_guest_tools"), [])
        # An attempt recorded outside the profile's own directory is never removed.
        outside = self.root / "elsewhere/guest-install13"
        outside.mkdir()
        self.write_state(**{**state, "closed": True, "guest_job": None,
                            "attempt": {"run": 13, "install": str(outside)}})
        self.scenario()
        self.phone().start(None, False)
        self.assertTrue(outside.is_dir())

    def test_stop_ends_an_unanswering_guest_without_saving_and_refuses_one_that_works(self):
        verified = {"run": 7, "install": str(self.base), "tools": str(self.tools)}
        def running(**replies):
            self.write_state(run=8, install="/x", tools=str(self.tools), closed=False, guest_job="guest8",
                             phase="ready", verified=verified)
            self.scenario(job__get=ok({"id": "guest8", "state": "running"}), **replies)
        def refusal(code, **details):
            return {"ok": False, "error": {"code": code, "message": code + " message", "details": details}}
        # The guest answers but is working: nothing is ended, the phone stays
        # up, and it is not asked again (its work does not end by waiting 20 s).
        for code in ("ios_guest_busy", "ios_guest_not_idle"):
            running(ios__emulator__control=refusal(code, native_status={"worker_running": True}))
            with self.assertRaises(RuntimeError) as error:
                self.phone().stop()
            self.assertIn("Stop refused", str(error.exception))
            self.assertEqual(len(self.calls("ios.emulator.control")), 1)
            self.assertEqual(self.calls("job.cancel") + self.calls("job.wait") + self.calls("ios.emulator.report"), [])
            self.assertEqual((self.state()["phase"], self.state()["closed"]), ("ready", False))
        # No answer from the guest service (hung): ended without saving, but
        # only after it was asked several times; a loaded guest can miss one
        # deadline and must not lose its state for that. A monitor that does
        # not exist yet cannot answer later either.
        for code, asked in (("ios_guest_timeout", 4), ("qmp_error", 4), ("ios_control_starting", 1)):
            running(ios__emulator__control=refusal(code),
                    ios__emulator__report=ok({"report": {"outcome": "interrupted", "exit_code": -15,
                                                         "storage_hashes_complete": False}, "stop": None}))
            with self.assertRaises(RuntimeError) as error:
                self.phone().stop()
            self.assertIn("ended without saving", str(error.exception))
            self.assertEqual(len(self.calls("ios.emulator.control")), asked)
            self.assertEqual([row["arguments"] for row in self.calls("job.cancel")], [{"job_id": "guest8"}])
            self.assertEqual(len(self.calls("job.wait")), 1)
            state = self.state()
            self.assertEqual((state["phase"], state["closed"], state["verified"]), ("unverified", False, verified))
        # A guest that only missed a deadline is stopped the careful way after all.
        running(ios__emulator__control=[refusal("ios_guest_timeout"), refusal("ios_guest_timeout"),
                                        ok({"result": {"emulator_exit_requested": True}})])
        self.phone().stop()
        self.assertEqual(len(self.calls("ios.emulator.control")), 3)
        self.assertEqual(self.calls("job.cancel"), [])
        self.assertEqual((self.state()["phase"], self.state()["closed"]), ("stopped", True))
        # Stopped before iOS ever came up: the run is forgotten with its disk
        # copy, and the phone is off on its last verified run. Not an error.
        never_up = self.profile / "installs/guest-install8"
        never_up.mkdir()
        self.write_state(run=8, install=str(never_up), tools=str(self.tools), closed=False, guest_job="guest8",
                         phase="booting", created=[str(never_up)], attempted=8, verified=verified)
        self.scenario(job__get=ok({"id": "guest8", "state": "running"}),
                      ios__emulator__control=refusal("ios_guest_starting"))
        self.phone().stop()
        self.assertEqual(len(self.calls("job.cancel")), 1)
        self.assertEqual(self.calls("ios.emulator.report"), [])
        self.assertFalse(never_up.exists())
        state = self.state()
        self.assertEqual((state["run"], state["install"], state["closed"], state["phase"], state["guest_job"],
                          state["error"], state["created"], state["attempted"]),
                         (7, str(self.base), True, "stopped", None, None, [], 8))
        self.assertTrue(self.base.is_dir())
        # The next start gets a number of its own.
        self.scenario()
        self.phone().start(None, False)
        self.assertEqual(self.state()["run"], 9)
        # A guest that ended on its own meanwhile needs no force.
        running(ios__emulator__control=refusal("ios_guest_stopped"))
        self.phone().stop()
        self.assertEqual(self.calls("job.cancel"), [])
        self.assertEqual(self.state()["phase"], "stopped")
        # A cancellation that is refused while the guest keeps running is an error of its own.
        running(ios__emulator__control=refusal("ios_guest_timeout"),
                job__cancel={"ok": False, "error": {"code": "job_error", "message": "cannot signal"}})
        with self.assertRaises(RuntimeError) as error:
            self.phone().stop()
        self.assertIn("could not be stopped", str(error.exception))

    def test_a_boot_refused_for_host_pressure_says_so(self):
        stopped = {"ok": False, "error": {"code": "ios_guest_stopped", "message": "stopped"}}
        for outcome, expected in (("host-pressure-blocked", "too busy"),
                                  ("kernel-panic", "stopped during boot (kernel-panic)")):
            with self.subTest(outcome=outcome):
                self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=True, guest_job=None)
                self.scenario(ios__emulator__guest=stopped,
                              ios__emulator__report=ok({"report": {"outcome": outcome, "exit_code": None,
                                                                   "storage_hashes_complete": False}}))
                with self.assertRaises(RuntimeError) as error:
                    self.phone().start(None, False)
                self.assertIn(expected, str(error.exception))

    def guest_up(self, phase="ready", **replies):
        """A running guest of the given phase whose control plane answers as scenario says."""
        self.write_state(run=8, install="/x", tools=str(self.tools), closed=False, guest_job="guest8", phase=phase,
                         verified={"run": 7, "install": str(self.base), "tools": str(self.tools)})
        self.scenario(job__get=ok({"id": "guest8", "state": "running"}), **replies)

    def test_a_busy_control_lane_is_asked_again_and_then_refuses_without_forcing(self):
        stopped = ok({"result": {"emulator_exit_requested": True}})
        # A viewer's gesture or another stop holds the lane: that says nothing
        # about the guest. The stop is asked four times, like after any
        # transient error, and refused if the lane stays taken. It never
        # falls through to ending the guest by force.
        self.guest_up(ios__emulator__control=control_error("ios_control_busy"))
        with self.assertRaises(RuntimeError) as refusal:
            self.phone().stop()
        self.assertIn("Stop refused", str(refusal.exception))
        self.assertEqual(len(self.calls("ios.emulator.control")), 4)
        self.assertEqual(self.calls("job.cancel") + self.calls("job.wait") + self.calls("ios.emulator.report"), [])
        self.assertEqual((self.state()["phase"], self.state()["closed"]), ("ready", False))
        # A lane that is free again by the third attempt: stopped the careful way.
        self.guest_up(ios__emulator__control=[control_error("ios_control_busy"), control_error("ios_control_busy"), stopped])
        self.phone().stop()
        self.assertEqual(len(self.calls("ios.emulator.control")), 3)
        self.assertEqual(self.calls("job.cancel"), [])
        self.assertEqual((self.state()["phase"], self.state()["closed"]), ("stopped", True))
        # A guest that never came up has one attempt, and is still one that
        # never came up when that is refused, so a later stop forgets it.
        self.guest_up("booting", ios__emulator__control=control_error("ios_control_busy"))
        with self.assertRaises(RuntimeError):
            self.phone().stop()
        self.assertEqual(len(self.calls("ios.emulator.control")), 1)
        self.assertEqual((self.state()["phase"], self.state()["closed"]), ("booting", False))

    def test_an_emulator_that_has_exited_is_waited_for_and_never_cancelled(self):
        # The session-limit stop has quit the emulator and the job is hashing
        # its disks; the control socket is gone. Ending the job now would turn
        # a verified end into an unverified one.
        self.guest_up(ios__emulator__control=control_error("ios_control_ended"))
        self.phone().stop()
        self.assertEqual(len(self.calls("ios.emulator.control")), 1)  # not asked again
        self.assertEqual(self.calls("job.cancel"), [])
        self.assertEqual(len(self.calls("job.wait")), 1)
        state = self.state()
        self.assertEqual((state["closed"], state["phase"], state["verified"]["run"]), (True, "stopped", 8))
        # What the job's report says decides, as after any stop: an end
        # without a confirmed flush is reported, not trusted.
        self.guest_up(ios__emulator__control=control_error("ios_control_ended"),
                      ios__emulator__report=ok({"report": {"exit_code": 0, "storage_hashes_complete": True}, "stop": None}))
        with self.assertRaises(RuntimeError) as unverified:
            self.phone().stop()
        self.assertIn("without verified storage", str(unverified.exception))
        self.assertEqual(self.calls("job.cancel"), [])
        self.assertEqual((self.state()["closed"], self.state()["phase"], self.state()["verified"]["run"]),
                         (False, "unverified", 7))
        # A guest that never came up has nothing to save: it is forgotten.
        never_up = self.profile / "installs/guest-install8"
        never_up.mkdir()
        self.write_state(run=8, install=str(never_up), tools=str(self.tools), closed=False, guest_job="guest8",
                         phase="booting", created=[str(never_up)], attempted=8,
                         verified={"run": 7, "install": str(self.base), "tools": str(self.tools)})
        self.scenario(job__get=ok({"id": "guest8", "state": "running"}),
                      ios__emulator__control=control_error("ios_control_ended"))
        self.phone().stop()
        self.assertEqual(len(self.calls("job.cancel")), 1)
        self.assertFalse(never_up.exists())
        self.assertEqual((self.state()["phase"], self.state()["run"], self.state()["guest_job"]), ("stopped", 7, None))

    def test_a_start_that_gives_up_on_a_booting_guest_leaves_it_one_that_never_came_up(self):
        # The guest service never answers. After an hour the start gives up
        # while the guest job still runs; the clock is moved on instead of waited for.
        self.scenario(ios__emulator__guest=control_error("ios_guest_timeout"))
        clock = itertools.count(1000, 2000)
        arguments = ["tool", "start", "--cli", str(self.cli), "--profile", str(self.profile)]
        with patch("sys.argv", arguments), patch("builtins.print") as printed, \
                patch.object(ios_virtual_iphone.time, "monotonic", side_effect=lambda: next(clock)):
            self.assertEqual(ios_virtual_iphone.main(), 1)
        failed = json.loads(printed.call_args_list[-1].args[0])
        self.assertEqual(failed["state"], "failed")
        # The guest still boots: the error says how to go on with it.
        self.assertTrue(failed["error"].endswith(
            "; the guest is still running: call ios.virtual.start again to continue with it, without a new disk copy"))
        state = self.state()
        self.assertIn("did not answer", state["error"])
        # Not "failed": the guest is still booting and a stop must know that.
        self.assertEqual((state["phase"], state["closed"], state["guest_job"]), ("booting", False, "guest8"))
        copy = self.profile / "installs/guest-install8"
        self.assertTrue(copy.is_dir())
        # The stop forgets the run with its disk copy, as for any guest that
        # never came up: no error, the phone is off on its last verified run.
        self.scenario(job__get=ok({"id": "guest8", "state": "running"}),
                      ios__emulator__control=control_error("ios_guest_starting"))
        with patch("sys.argv", ["tool", "stop", "--cli", str(self.cli), "--profile", str(self.profile)]), \
                patch("builtins.print") as printed:
            self.assertEqual(ios_virtual_iphone.main(), 0)
        self.assertEqual(len(self.calls("ios.emulator.control")), 1)
        self.assertEqual(len(self.calls("job.cancel")), 1)
        self.assertEqual(self.calls("ios.emulator.report"), [])
        self.assertFalse(copy.exists())
        state = self.state()
        self.assertEqual((state["phase"], state["closed"], state["run"], state["install"], state["guest_job"],
                          state["error"]), ("stopped", True, 7, str(self.base), None, None))
        self.assertEqual(json.loads(printed.call_args_list[-1].args[0])["state"], "ok")
        # A state written by an earlier version of this tool, or by a start
        # that got further, is not a guest that never came up.
        for phase in ("unlocking", "ready", "failed"):
            self.guest_up(phase, ios__emulator__control=control_error("ios_guest_timeout"),
                          ios__emulator__report=ok({"report": {"outcome": "interrupted", "exit_code": -15,
                                                               "storage_hashes_complete": False}, "stop": None}))
            with self.assertRaises(RuntimeError) as lost:
                self.phone().stop()
            self.assertIn("ended without saving", str(lost.exception))
            self.assertEqual(len(self.calls("ios.emulator.control")), 4)
            self.assertEqual((self.state()["phase"], self.state()["closed"]), ("unverified", False))

    def test_a_failed_starts_hint_follows_the_phase_and_needs_a_running_guest(self):
        for phase, expected in (("launch-uncertain", "with open_app false"),
                                ("awaiting-unlock", "unlock it in the phone window"),
                                ("failed", "without a new disk copy")):
            self.guest_up(phase)
            self.assertIn(expected, ios_virtual_iphone.resume_hint(self.phone()))
        # A guest that has ended, or one that cannot be observed, gets no hint.
        self.guest_up("failed")
        self.scenario(job__get=ok({"id": "guest8", "state": "failed"}))
        self.assertEqual(ios_virtual_iphone.resume_hint(self.phone()), "")
        self.scenario(job__get={"ok": False, "error": {"code": "database_error", "message": "locked"}})
        self.assertEqual(ios_virtual_iphone.resume_hint(self.phone()), "")

    def test_a_native_refusal_other_than_locked_ends_the_start_and_is_recorded(self):
        self.scenario(ios__emulator__guest=[ready(), ready(), refused("Disabled")])
        with patch("sys.argv", ["tool", "start", "--cli", str(self.cli), "--profile", str(self.profile)]), \
                patch("builtins.print") as printed:
            self.assertEqual(ios_virtual_iphone.main(), 1)
        self.assertEqual(json.loads(printed.call_args_list[-1].args[0])["state"], "failed")
        self.assertIn("call ios.virtual.start again", json.loads(printed.call_args_list[-1].args[0])["error"])
        state = self.state()
        self.assertEqual(state["phase"], "failed")
        self.assertIn("Disabled", state["error"])
        # Boot status, the idle check and the one refused launch: no further attempts.
        self.assertEqual([row["arguments"]["operation"] for row in self.calls("ios.emulator.guest")],
                         ["status", "status", "open"])

    def test_development_activation_is_read_from_the_last_cycle_of_the_log(self):
        read = ios_virtual_iphone.development_activation_from_log
        cycle = '{"event":"development-setup","state":"starting","phase":"library-integrity"}'
        active = '{"event":"development-signing","state":"active","policy":"owned-cdhash","entries":2,"bundle_id":"io.x"}'
        activated = '{"event":"development-setup","state":"activated","controller_released":true}'
        failed = '{"event":"development-setup","state":"failed","phase":"library-activation-unconfirmed","error":60}'
        submit = '{"event":"development-setup","state":"configuration-submitted","pid":232}'
        join = "\n".join
        self.assertEqual(read(""), "absent")
        self.assertEqual(read("not a development cycle\n"), "absent")
        self.assertEqual(read(join([cycle, submit, active])), "activated")
        self.assertEqual(read(join([cycle, submit, activated])), "activated")
        self.assertEqual(read(join([cycle, submit, failed])), "failed")
        self.assertEqual(read(join([cycle, submit])), "pending")
        # Only the last boot's cycle decides; an earlier success does not carry.
        self.assertEqual(read(join([cycle, submit, active, activated, cycle, submit, failed])), "failed")
        self.assertEqual(read(join([cycle, submit, failed, cycle, submit, active])), "activated")
        # The adapter's own active event counts even when the setup then fails to
        # confirm its controller release: the hook is in installd either way.
        self.assertEqual(read(join([cycle, submit, active, failed])), "activated")
        # The loaded liveness marker is not activation on its own: the adapter
        # ran but has not hooked installd, so the start keeps waiting.
        loaded = '{"event":"development-signing","state":"loaded"}'
        self.assertEqual(read(join([cycle, submit, loaded])), "pending")
        self.assertEqual(read(join([cycle, submit, loaded, active])), "activated")
        self.assertEqual(read(join([cycle, submit, loaded, failed])), "failed")
        # The configuration-detail and copy-probe diagnostics are not activation.
        detail = '{"event":"development-setup","state":"configuration-detail","pid":268,"insert_library":"/x.dylib","adapter_stderr_fd":2,"ack_fd":198}'
        probe = '{"event":"development-setup","state":"copy-probe","which":"copy","path":"/x.dylib","size":50864,"expected_size":50864,"dlopen":false,"error":"dlopen(/x.dylib): code signature invalid"}'
        self.assertEqual(read(join([cycle, detail, probe, submit])), "pending")
        self.assertEqual(read(join([cycle, detail, probe, submit, failed])), "failed")

    def _guest_phone(self, replies):
        """A phone whose ios.emulator.guest calls are answered by `replies`,
        a function of the request's operation and argument."""
        phone = self.phone()
        def invoke(operation, arguments, timeout=180, independent=False):
            self.assertEqual(operation, "ios.emulator.guest")
            return replies(arguments.get("operation"), arguments.get("argument"))
        phone.invoke = invoke
        return phone

    def test_development_signing_state_prefers_status_then_the_development_log(self):
        cycle = '{"event":"development-setup","state":"starting","phase":"library-integrity"}'
        activated = '{"event":"development-setup","state":"activated","controller_released":true}'

        def guest(status_value):
            def replies(operation, argument):
                if operation == "status":
                    response = {"ok": True}
                    if status_value is not None:
                        response["development_signing_activation"] = status_value
                    return ok({"response": response})
                self.assertEqual((operation, argument), ("logs", "development"))
                return ok({"response": {"ok": True, "text": cycle + "\n" + activated}})
            return replies
        self.assertEqual(self._guest_phone(guest("activated")).development_signing_state("g"), "activated")
        self.assertEqual(self._guest_phone(guest("failed")).development_signing_state("g"), "failed")
        # A guest-tools build whose status does not report it falls back to the log.
        self.assertEqual(self._guest_phone(guest(None)).development_signing_state("g"), "activated")

    def test_the_owned_app_is_staged_with_the_signature_of_this_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            tools = Path(temporary)
            self.assertEqual(ios_virtual_iphone.owned_application(tools), [])
            (tools / "guest-tools.json").write_text(json.dumps({"guest_root": "/System/Cryptexes/Supplemental", "programs": [
                {"path": "usr/libexec/tomacco-platform", "cdhash": "a" * 40}]}))
            self.assertEqual(ios_virtual_iphone.owned_application(tools), [])
            (tools / "guest-tools.json").write_text(json.dumps({"guest_root": "/System/Cryptexes/Supplemental", "programs": [
                {"path": "usr/libexec/TomaccoProbe.app/TomaccoProbe", "cdhash": "b" * 40}]}))
            self.assertEqual(ios_virtual_iphone.owned_application(tools), [{
                "bundle_id": ios_virtual_iphone.RESEARCH_APPLICATION, "name": "TomaccoProbe.app",
                "guest_path": "/System/Cryptexes/Supplemental/usr/libexec/TomaccoProbe.app", "cdhash": "b" * 40}])

    def test_a_staged_app_is_not_installed_until_the_adapter_has_activated(self):
        cycle = '{"event":"development-setup","state":"starting","phase":"library-integrity"}'
        failed = '{"event":"development-setup","state":"failed","phase":"library-activation-unconfirmed","error":60}'
        app = [{"bundle_id": "io.github.marselnenaj.tomacco.sample-app"}]
        # No staged apps: the adapter is not needed, and the guest is not asked.
        empty = self.phone()
        empty.invoke = lambda *a, **k: self.fail("a build with no staged apps must not be waited for")
        empty.await_development_signing("g", [])
        # The adapter failed to activate: the start refuses before any install.
        def failing(operation, argument):
            if operation == "status":
                return ok({"response": {"ok": True}})
            return ok({"response": {"ok": True, "text": cycle + "\n" + failed}})
        with self.assertRaisesRegex(RuntimeError, "did not activate"):
            self._guest_phone(failing).await_development_signing("g", app)
        # The adapter is active: the wait returns so the install can proceed.
        active = self._guest_phone(lambda operation, argument:
                                   ok({"response": {"ok": True, "development_signing_activation": "activated"}}))
        active.await_development_signing("g", app)

    def test_a_failed_staged_install_is_recorded_and_the_start_goes_on(self):
        # The adapter did not activate; the phone must still get
        # its time zone and its launch, with the failure recorded.
        app = [{"bundle_id": "io.github.marselnenaj.tomacco.sample-app"}]
        phone = self.phone()
        calls = []
        phone.await_development_signing = lambda job, staged: (_ for _ in ()).throw(
            RuntimeError("installd's development-signing adapter did not activate (it reported a failed setup)"))
        phone.install_applications = lambda job, staged: calls.append("install")
        phone.apply_time_zone = lambda job, response: calls.append("time_zone")
        phone.launch = lambda job, deadline: calls.append("launch")
        with patch.object(ios_virtual_iphone, "staged_applications", lambda profile, tools: app):
            self.scenario(ios__emulator__guest=[ok({"response": {"ok": True, "worker_running": False}})])
            phone.start(None, True)
        self.assertEqual(calls, ["time_zone", "launch"])
        self.assertIn("did not activate", self.state()["applications_error"])
        self.assertEqual(self.state()["phase"], "launch-accepted")

    def test_the_staged_install_runs_after_activation_and_before_the_time_zone(self):
        self.write_state(run=7, install=str(self.base), tools=str(self.tools), closed=False, guest_job="guest8",
                         verified={"run": 7, "install": str(self.base), "tools": str(self.tools)})
        phone = self.phone()
        phone.guest_running = lambda: True
        order = []
        phone.apply_watchdog_scale = lambda *a, **k: order.append("watchdog")
        phone.await_development_signing = lambda job, staged: order.append("await")
        phone.install_applications = lambda job, staged: order.append("install")
        phone.apply_time_zone = lambda *a, **k: order.append("time_zone")
        phone.apply_auto_lock = lambda *a, **k: order.append("auto_lock")
        phone.launch = lambda *a, **k: order.append("launch")
        phone.observe_display = lambda *a, **k: order.append("observe")
        phone.invoke = lambda operation, arguments, timeout=180, independent=False: ready()
        app = {"bundle_id": "io.github.marselnenaj.tomacco.sample-app", "name": "TomaccoSample.app",
               "guest_path": "/g/TomaccoSample.app", "cdhash": "ab" * 20}
        with patch.object(ios_virtual_iphone, "staged_applications", lambda profile, tools: [app]):
            phone.start(None, True)
        self.assertEqual([step for step in order if step in ("await", "install", "time_zone", "auto_lock", "launch")],
                         ["await", "install", "time_zone", "auto_lock", "launch"])


class VirtualPhoneOperations(Isolated):
    def profile(self, **state):
        directory = self.root / "profile"
        (directory / "installs").mkdir(parents=True)
        (directory / "profile.json").write_text(json.dumps({
            "format": 1, "prepare": {}, "boot": {}, "graphics": "/graphics", "engine": "/engine.py",
            "application": "app.example", "installs": str(directory / "installs")}))
        (directory / "state.json").write_text(json.dumps({
            "run": 3, "install": str(self.root / "missing-install"), "tools": str(self.root / "missing-tools"),
            "closed": True, "guest_job": None, **state}))
        return directory

    def test_status_and_configuration_validate_the_profile(self):
        self.assertEqual(dispatch("ios.virtual.status"), {"configured": False, "experimental": True})
        for operation in ("ios.virtual.start", "ios.virtual.stop"):
            with self.assertRaises(StudioError) as error:
                dispatch(operation)
            self.assertEqual(error.exception.code, "ios_virtual_unconfigured")
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(StudioError) as error:
            dispatch("ios.virtual.configure", {"directory": str(empty)})
        self.assertEqual(error.exception.code, "ios_virtual_profile_invalid")
        directory = self.profile()
        (directory / "state.json").write_text(json.dumps({"run": 3, "closed": "yes"}))
        with self.assertRaises(StudioError):
            dispatch("ios.virtual.configure", {"directory": str(directory)})
        self.assertEqual(dispatch("ios.virtual.status")["configured"], False)
        (directory / "state.json").write_text(json.dumps({
            "run": 3, "install": "/i", "tools": "/t", "closed": True, "guest_job": "0" * 32, "phase": "stopped"}))
        status = dispatch("ios.virtual.configure", {"directory": str(directory)})
        self.assertEqual((status["configured"], status["run"], status["phase"], status["closed"]),
                         (True, 3, "stopped", True))
        # A guest job this data directory does not know is unknown, not stopped.
        self.assertIsNone(status["guest_state"])
        self.assertFalse(status["guest_state_known"])
        self.assertFalse(status["guest_running"])
        self.assertIsNone(status["busy"])
        # Nothing is left halfway, nothing has finished yet, and the host's
        # load is reported on every call (this host's card may report nothing).
        self.assertEqual((status["next"], status["last_run"]), (None, None))
        self.assertEqual(status["host"]["pressure"]["measure"], "psi")
        self.assertTrue(status["host"]["gpu"] is None or status["host"]["gpu"]["source"] == "sysfs")
        # A phone that does not run has no liveness to judge, probed or not.
        self.assertIsNone(status["liveness"])
        self.assertIsNone(dispatch("ios.virtual.status", {"probe": True})["liveness"])
        # The silence limit is a profile setting with bounds.
        dispatch("ios.virtual.configure", {"stall_seconds": 120})
        self.assertEqual(json.loads((directory / "profile.json").read_text())["stall_seconds"], 120)
        for wrong in (5, 100000):
            with self.assertRaises(StudioError):
                dispatch("ios.virtual.configure", {"stall_seconds": wrong})

    def test_a_start_whose_disk_copy_cannot_fit_is_refused_before_any_job(self):
        base = self.root / "base"
        base.mkdir()
        # A sparse 8 TiB root disk: more than any test host has free.
        with (base / "system.raw").open("wb") as stream:
            stream.truncate(8 << 40)
        for name in ("auxiliary.raw", "sep.raw"):
            (base / name).write_bytes(b"\0" * 4096)
        directory = self.profile(install=str(base))
        disk = dispatch("ios.virtual.configure", {"directory": str(directory)})["start_disk"]
        # The status says so before any start, without writing a probe: on a
        # filesystem that might clone, the answer stays open.
        self.assertEqual(disk["needed_bytes"], (8 << 40) + 8192 + (1 << 30))
        self.assertIn(disk["enough"], (False, None))
        self.assertEqual(list((directory / "installs").iterdir()), [])
        try:
            dispatch("ios.virtual.start")
        except StudioError as error:
            self.assertEqual(error.code, "ios_virtual_disk_space")
            self.assertIn("8193.0 GiB", str(error))
            self.assertIn("installs", str(error))
            self.assertEqual(error.details["installs"], str(directory / "installs"))
        else:
            self.skipTest("the installs filesystem clones files, so the copy needs no room")
        self.assertEqual([job for job in jobs.listing(20) if "virtual" in job["label"]], [])

    def test_a_screenshot_needs_a_running_phone_and_a_png_path(self):
        directory = self.profile()
        dispatch("ios.virtual.configure", {"directory": str(directory)})
        target = self.root / "shots/phone.png"
        with self.assertRaises(StudioError) as error:
            dispatch("ios.virtual.screenshot", {"path": str(target), "frame": "device"})
        self.assertEqual(error.exception.code, "ios_virtual_not_running")
        with self.assertRaises(StudioError) as error:
            dispatch("ios.virtual.screenshot", {"path": str(self.root / "phone.jpg")})
        self.assertEqual(error.exception.code, "invalid_path")
        with self.assertRaises(StudioError):
            dispatch("ios.virtual.screenshot", {"path": str(target), "frame": "photo"})
        self.assertFalse(target.exists())

    def test_a_recording_needs_a_running_phone_and_an_mp4_path(self):
        directory = self.profile()
        dispatch("ios.virtual.configure", {"directory": str(directory)})
        with self.assertRaises(StudioError) as error:
            dispatch("ios.virtual.record", {"path": str(self.root / "clip.mov")})
        self.assertEqual(error.exception.code, "invalid_path")
        for arguments in ({"seconds": 0}, {"seconds": 601}, {"fps": 31}):
            with self.assertRaises(StudioError):
                dispatch("ios.virtual.record", {"path": str(self.root / "clip.mp4"), **arguments})
        with self.assertRaises(StudioError) as error:
            dispatch("ios.virtual.record", {"path": str(self.root / "clip.mp4"), "seconds": 5})
        self.assertIn(error.exception.code, ("ios_virtual_not_running", "tool_missing"))
        self.assertEqual([job for job in jobs.listing(20) if "Record" in job["label"]], [])

    def test_the_device_model_is_a_setting_of_its_own_and_reaches_the_start(self):
        with self.assertRaises(StudioError) as error:
            dispatch("ios.virtual.configure", {"model": "16"})
        self.assertEqual(error.exception.code, "ios_virtual_unconfigured")
        directory = self.profile()
        status = dispatch("ios.virtual.configure", {"directory": str(directory)})
        self.assertEqual((status["model"], status["running_model"], status["guest_ended"]), ("", "", False))
        models = {model["id"]: model for model in status["models"]}
        # Apple's own variant, and models derived from it; each with its panel.
        self.assertEqual(models["16"]["source"], "apple")
        self.assertEqual(models["16"]["panel"], "1179x2556@60:65x141")
        self.assertTrue(all(model["source"] in ("apple", "derived") and model["name"] for model in models.values()))
        self.assertNotIn("pixel-11", models)
        for unknown in ("pixel-11", "iphone-99"):
            with self.assertRaises(StudioError) as error:
                dispatch("ios.virtual.configure", {"model": unknown})
            self.assertEqual(error.exception.code, "invalid_device_model")
        # The model alone keeps the profile; the profile alone keeps the model.
        self.assertEqual(dispatch("ios.virtual.configure", {"model": "16"})["model"], "16")
        self.assertEqual(dispatch("ios.virtual.status")["directory"], str(directory))
        self.assertEqual(dispatch("ios.virtual.configure", {"directory": str(directory)})["model"], "16")
        job = dispatch("ios.virtual.start", {"open_app": False})
        # A start that boots says first what it should know about the host.
        self.assertIsInstance(job["warnings"], list)
        argv = job["plan"]["steps"][0]["argv"]
        self.assertEqual(argv[argv.index("--model"):], ["--model", "16", "--unlock", "swipe"])
        jobs.wait(job["id"], 60)
        # Back to the profile's own device.
        self.assertEqual(dispatch("ios.virtual.configure", {"model": ""})["model"], "")
        job = dispatch("ios.virtual.start", {"open_app": False})
        self.assertNotIn("--model", job["plan"]["steps"][0]["argv"])
        jobs.wait(job["id"], 60)

    def test_start_is_one_durable_job_that_records_its_failure(self):
        directory = self.profile()
        dispatch("ios.virtual.configure", {"directory": str(directory)})
        job = dispatch("ios.virtual.start", {"open_app": False})
        argv = job["plan"]["steps"][0]["argv"]
        self.assertEqual(Path(argv[1]).name, "ios_virtual_iphone.py")
        self.assertEqual(argv[2:3] + argv[5:], ["start", "--profile", str(directory), "--no-app"])
        final = jobs.wait(job["id"], 60)
        # The disks named by this profile do not exist: preparation is refused
        # and the job fails without ever starting a guest.
        self.assertEqual(final["state"], "failed")
        state = json.loads((directory / "state.json").read_text())
        self.assertEqual(state["phase"], "failed")
        self.assertTrue(state["error"])
        self.assertTrue(state["closed"])
        self.assertIsNone(state["guest_job"])
        status = dispatch("ios.virtual.status")
        self.assertEqual((status["phase"], status["busy"]), ("failed", None))


if __name__ == "__main__":
    unittest.main()
