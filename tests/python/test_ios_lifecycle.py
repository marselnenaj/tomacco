import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import ios_lifecycle

# A stand-in control plane whose guest keeps the owned app's log the way the
# app does: every start appends a launch, every click on the control a tap,
# every open a new active scene. The log survives stops unless told not to.
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ["FAKE_ROOT"])
operation, arguments = sys.argv[2], json.loads(sys.argv[3])
calls = root / "calls.jsonl"
history = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
with calls.open("a") as log:
    log.write(json.dumps({"operation": operation, "arguments": arguments}) + "\n")
scenario = json.loads((root / "scenario.json").read_text())
ok = lambda result: {"ok": True, "result": result}
def count(name, **match):
    return sum(1 for row in history if row["operation"] == name
               and all(row["arguments"].get(key) == value for key, value in match.items()))
starts, stops = count("ios.virtual.start"), count("ios.virtual.stop")
running = starts > stops
if operation in ("ios.virtual.start", "ios.virtual.stop"):
    reply = ok({"id": "job%d" % len(history), "state": "running"})
elif operation == "job.wait":
    reply = ok({"id": arguments["job_id"], "state": "succeeded"})
elif operation == "ios.virtual.status":
    reply = ok({"phase": "launch-accepted" if running else "stopped", "closed": not running, "run": 100 + starts,
                "guest_job": "guest%d" % starts if running else None, "display_visible": True})
elif operation == "ios.emulator.control":
    reply = ok({"job_id": arguments["job_id"], "action": arguments["action"], "result": {"accepted": True}})
elif operation == "ios.emulator.guest_result":
    polls = count("ios.emulator.guest_result")
    reply = ok({"job_id": arguments["job_id"], "request_id": arguments["request_id"],
                "state": "running" if polls < 2 else "completed",
                "response": {"ok": True, "operation": "install", "request_accepted": True}})
elif arguments["operation"] == "logs":
    rows, start, alive, opened_since_install = [], 0, False, None
    for row in history:
        if row["operation"] == "ios.virtual.start":
            start += 1
            alive = True
            if start in scenario.get("data_lost_at_start", []):
                rows = []
            rows += [{"event": "begin"}, {"event": "scene-active", "activation_state": 0},
                     {"event": "window-state", "width": 393.0, "height": 852.0, "scale": 3.0}]
        elif row["operation"] == "ios.emulator.control" and row["arguments"]["action"] == "click":
            if scenario.get("taps_register", True):
                rows.append({"event": "control-action", "count": 1})
        elif row["operation"] == "ios.emulator.guest" and row["arguments"]["operation"] == "install":
            alive, opened_since_install = False, 0
        elif row["operation"] == "ios.emulator.guest" and row["arguments"]["operation"] == "open":
            if opened_since_install is not None:
                opened_since_install += 1
                if opened_since_install <= scenario.get("refused_opens", 0):
                    continue
            if not alive:
                rows += [{"event": "begin"}, {"event": "window-state", "width": 393.0, "height": 852.0}]
                alive = True
            rows.append({"event": "scene-active", "activation_state": 0})
    reply = ok({"response": {"ok": True, "operation": "logs", "truncated": scenario.get("truncated", False),
                             "text": "".join(json.dumps(row) + "\n" for row in rows)}})
elif arguments["operation"] == "foreground_state":
    front = "com.apple.springboard"
    for row in history:
        if row["operation"] == "ios.virtual.start" or (
                row["operation"] == "ios.emulator.guest" and row["arguments"].get("operation") == "open"):
            front = "app.example"
        elif row["operation"] == "ios.emulator.control" and row["arguments"]["action"] == "home":
            front = "com.apple.springboard"
    reply = ok({"response": {"ok": True, "foreground_identifier_known": True, "frontmost_bundle_id": front}})
elif arguments["operation"] == "install":
    mode = scenario.get("install", "direct")
    if count("ios.emulator.guest", operation="install") < scenario.get("busy_installs", 0):
        reply = ok({"response": {"ok": False, "operation": "install", "error": "guest_operation_busy"}})
    elif mode == "late":
        reply = {"ok": False, "error": {"code": "ios_guest_timeout", "details": {
            "request_id": "e" * 32, "request_may_have_executed": True}}}
    else:
        reply = ok({"response": {"ok": mode == "direct", "operation": "install",
                                 "request_accepted": mode == "direct",
                                 **({} if mode == "direct" else {"error": "installation_rejected_by_ios"})}})
elif arguments["operation"] == "open":
    # The first open after Home can find the previous query's worker ending.
    busy = scenario.get("busy_opens", 0) > count("ios.emulator.guest", operation="open") - count("ios.virtual.start") + 1
    installs = [index for index, row in enumerate(history) if row["operation"] == "ios.emulator.guest"
                and row["arguments"].get("operation") == "install"]
    # Right after an install iOS refuses opens while it launches the app itself.
    refused = bool(installs) and sum(1 for row in history[installs[-1]:] if row["operation"] == "ios.emulator.guest"
                                     and row["arguments"].get("operation") == "open") < scenario.get("refused_opens", 0)
    reply = ok({"response": {"ok": False, "error": "guest_operation_busy"}} if busy else
               {"response": {"ok": False, "request_accepted": False, "error": "request_rejected_by_ios"}} if refused else
               {"response": {"ok": True, "request_accepted": True}})
print(json.dumps(reply))
'''


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="tomacco-lifecycle-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.cli = self.root / "tomaccoctl"
        self.cli.write_text(FAKE_CLI)
        self.cli.chmod(0o755)
        # The stand-in's root is this test's alone, not left to later modules.
        environment = patch.dict(os.environ, {"FAKE_ROOT": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        self.scenario()

    def scenario(self, **values):
        (self.root / "scenario.json").write_text(json.dumps(values))

    def calls(self, operation):
        return [row["arguments"] for row in map(json.loads, (self.root / "calls.jsonl").read_text().splitlines())
                if row["operation"] == operation]

    def lifecycle(self):
        return ios_lifecycle.Lifecycle(str(self.cli), "app.example", pause=0, patience=0.01)

    def test_cycles_tap_background_reopen_and_keep_the_apps_data_across_stops(self):
        lifecycle = self.lifecycle()
        carried = {"launches": 0, "taps": 0}
        for number in (1, 2, 3):
            carried = lifecycle.cycle(number, carried)
            self.assertEqual(carried, {"launches": number, "taps": number})
        clicks = [call for call in self.calls("ios.emulator.control") if call["action"] == "click"]
        # The control's centre in the window the app reported, as a fraction.
        self.assertEqual({(call["x"], call["y"]) for call in clicks}, {(164 / 393, 244 / 852)})
        self.assertEqual([call["action"] for call in self.calls("ios.emulator.control")], ["click", "home"] * 3)
        self.assertEqual([call["argument"] for call in self.calls("ios.emulator.guest")
                          if call["operation"] == "open"], ["app.example"] * 3)
        self.assertEqual((len(self.calls("ios.virtual.start")), len(self.calls("ios.virtual.stop"))), (3, 3))

    def test_a_cycle_can_reinstall_the_app_and_runs_on_the_installed_copy(self):
        for mode in ("direct", "late"):
            with self.subTest(mode=mode):
                (self.root / "calls.jsonl").unlink(missing_ok=True)
                self.scenario(install=mode)
                lifecycle = ios_lifecycle.Lifecycle(str(self.cli), "app.example", pause=0, patience=0.01,
                                                    bundle="/System/Example.app")
                carried = lifecycle.cycle(1, {"launches": 0, "taps": 0})
                installs = [call for call in self.calls("ios.emulator.guest") if call["operation"] == "install"]
                self.assertEqual([call["argument"] for call in installs], ["/System/Example.app"])
                # The installed copy is opened after the install, then reopened after Home.
                self.assertEqual(len([call for call in self.calls("ios.emulator.guest")
                                      if call["operation"] == "open"]), 2)
                self.assertEqual(carried["taps"], 1)
                # Twice running, then the completed receipt.
                self.assertEqual(len(self.calls("ios.emulator.guest_result")), 3 if mode == "late" else 0)
                (self.root / "calls.jsonl").unlink()
        # An install refused while the previous worker ends is asked again.
        (self.root / "calls.jsonl").unlink(missing_ok=True)
        self.scenario(install="direct", busy_installs=2)
        lifecycle = ios_lifecycle.Lifecycle(str(self.cli), "app.example", pause=0, patience=0.01,
                                            bundle="/System/Example.app")
        lifecycle.cycle(1, {"launches": 0, "taps": 0})
        self.assertEqual(len([call for call in self.calls("ios.emulator.guest") if call["operation"] == "install"]), 3)
        (self.root / "calls.jsonl").unlink()
        self.scenario(install="rejected")
        lifecycle = ios_lifecycle.Lifecycle(str(self.cli), "app.example", pause=0, patience=0.01,
                                            bundle="/System/Example.app")
        with self.assertRaisesRegex(ios_lifecycle.CycleFailed, "installation_rejected_by_ios"):
            lifecycle.cycle(1, {"launches": 0, "taps": 0})

    def test_an_open_refused_while_ios_launches_the_installed_copy_is_asked_again(self):
        # Right after the install iOS can refuse the open while it is still
        # launching the app itself; the installed copy comes up anyway.
        self.scenario(install="direct", refused_opens=2)
        lifecycle = ios_lifecycle.Lifecycle(str(self.cli), "app.example", pause=0, patience=0.01,
                                            bundle="/System/Example.app")
        carried = lifecycle.cycle(1, {"launches": 0, "taps": 0})
        opens = [call for call in self.calls("ios.emulator.guest") if call["operation"] == "open"]
        # Two refused, the accepted one, and the reopen after Home.
        self.assertEqual(len(opens), 4)
        self.assertEqual(carried["taps"], 1)

    def test_a_busy_guest_is_asked_again_to_reopen_the_app(self):
        self.scenario(busy_opens=2)
        self.lifecycle().cycle(1, {"launches": 0, "taps": 0})
        opens = [call for call in self.calls("ios.emulator.guest") if call["operation"] == "open"]
        self.assertEqual(len(opens), 3)

    def test_a_tap_the_app_never_logs_fails_the_cycle(self):
        self.scenario(taps_register=False)
        with self.assertRaisesRegex(ios_lifecycle.CycleFailed, "control-action"):
            self.lifecycle().cycle(1, {"launches": 0, "taps": 0})
        # The phone is left running for inspection.
        self.assertEqual(self.calls("ios.virtual.stop"), [])

    def test_taps_lost_across_a_reboot_fail_the_next_cycle(self):
        self.scenario(data_lost_at_start=[2])
        lifecycle = self.lifecycle()
        carried = lifecycle.cycle(1, {"launches": 0, "taps": 0})
        with self.assertRaisesRegex(ios_lifecycle.CycleFailed, "did not survive"):
            lifecycle.cycle(2, carried)

    def test_a_log_beyond_the_guests_read_bound_is_named(self):
        self.scenario(truncated=True)
        with self.assertRaisesRegex(ios_lifecycle.CycleFailed, "larger than the guest returns"):
            self.lifecycle().cycle(1, {"launches": 0, "taps": 0})

    def test_a_log_without_the_starts_own_launch_is_not_taken_for_it(self):
        lifecycle = self.lifecycle()
        # The log already holds a launch from before; this start adds one more.
        with self.assertRaisesRegex(ios_lifecycle.CycleFailed, "the app's launch in its log"):
            lifecycle.cycle(1, {"launches": 1, "taps": 0})

    def test_the_script_refuses_a_phone_that_is_already_running(self):
        lifecycle = self.lifecycle()
        lifecycle.run_job("ios.virtual.start")
        done = subprocess.run([sys.executable, str(Path(ios_lifecycle.__file__)), "--cli", str(self.cli),
                               "--cycles", "1"], capture_output=True, text=True, env=os.environ)
        self.assertEqual(done.returncode, 2)
        self.assertIn("Stop the virtual iPhone first", done.stderr)


if __name__ == "__main__":
    unittest.main()
