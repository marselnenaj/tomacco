"""The host-side persistence probe tool against a stand-in tomaccoctl.

These fixtures replay the shapes the real control plane returns and the shapes the class survey defines; they are not iOS
execution evidence."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import ios_persistence_probe as probe

# The stand-in control plane answers like tomaccoctl: ios.virtual.status names
# the guest job; ios.emulator.guest answers the persistence probe the way the
# guest's worker does, by scenario.
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
if operation == "ios.virtual.status":
    reply = ok({"run": 324, "phase": "failed", "guest_running": scenario.get("guest_running", True),
                "guest_job": "771d3800ce3c4a9796f88ee7ada1e2f5" if scenario.get("guest_running", True) else None})
else:
    assert operation == "ios.emulator.guest" and arguments["operation"] == "persistence_probe"
    action, _, challenge = arguments["argument"].partition(":")
    probes = sum(1 for row in history if row["operation"] == "ios.emulator.guest")
    request = "3c99e8f852ee4fd6a7fb62d77d83fc1" + str(probes % 10)
    base = {"format": 1, "operation": "persistence_probe", "uid": 501, "pid": 752 + probes, "request_id": request,
            "challenge": challenge, "probe_action": action, "automatic_retry_safe": action != "create",
            "paired_reboot_verified": False}
    transport = lambda response: ok({"job_id": arguments["job_id"], "guest_response_received": True,
                                     "request_id": request, "response": response})
    if probes < scenario.get("busy", 0):
        reply = transport({**base, "ok": False, "error": "guest_operation_busy"})
    elif scenario.get("lost"):
        # The host-level shape tomaccoctl gives when the worker's receipt never
        # came: the request may have run on the guest, so it must not be resent.
        reply = {"ok": False, "error": {"code": "guest_worker_timeout",
                                        "message": "The guest did not answer within 120 seconds",
                                        "details": {"request_id": request, "request_may_have_executed": True}}}
    elif action in ("classes", "inode") and scenario.get("guest_tools", 370) < 370:
        reply = {"ok": False, "error": {"code": "invalid_guest_response",
                                        "message": "Native guest response does not match the request",
                                        "details": {"request_id": request, "request_may_have_executed": True}}}
    elif action == "read" and scenario.get("directory_missing"):
        reply = transport({**base, "ok": False, "error": "persistence_directory_unavailable", "errno": 2,
                           "file_created": False, "contents_match": False, "class_c_verified": False,
                           "read_completed": False, "private_directory_created": False})
    elif action == "inode":
        reply = transport({**base, "ok": True, "inode": int(challenge), "volume_fsid": 16777229, "path_known": True,
                           "path": "/private/var/mobile/Library/Caches/example.db", "object_type": 1,
                           "protection_class": scenario.get("inode_class", 6)})
    elif action in ("create", "read"):
        created = action == "create"
        reply = transport({**base, "ok": True, "file_created": created, "contents_match": True,
                           "class_c_verified": True, "protection_class": 3, "read_bytes": 64, "read_completed": True,
                           "file_inode": 201568, "file_fully_flushed": created, "directory_synced": created,
                           "parent_directory_synced": created, "private_directory_created": created,
                           "private_directory_uid": 501, "private_directory_mode": 448})
    else:
        reply = transport({**base, "ok": True, "classes_surveyed": 4, "classes_readable": 3,
                           "private_directory_created": False, "private_directory_uid": 501,
                           "private_directory_mode": 448,
                           "class_1_action": "create", "class_1_inode": 201570, "class_1_contents_match": False,
                           "class_1_error": "persistence_set_class_failed", "class_1_errno": 1, "class_1_removed": True,
                           "class_2_action": "create", "class_2_inode": 201571, "class_2_protection_class": 2,
                           "class_2_read_bytes": 64, "class_2_contents_match": True,
                           "class_3_action": "read", "class_3_inode": 201572, "class_3_protection_class": 3,
                           "class_3_read_bytes": 64, "class_3_contents_match": True,
                           "class_4_action": "read", "class_4_inode": 201573, "class_4_protection_class": 4,
                           "class_4_read_bytes": 64, "class_4_contents_match": True})
print(json.dumps(reply))
'''

# Lines of a uart.log, in their order.
UART_EXCERPT = """\
"AppleSEPKeyStore":pid:10,:6137: disabling use of effaceable storage, using fake key, 0xffffffff
apfs_migrate_aks_key_class:2432: failed to get wrapped media key (uuid = 61706673-7575-6964-0002-766F6C756D06, cur_tag = 2), error = 2
"AppleSEPKeyStore":pid:11,:2726: mapped uuid 61706673-7575-6964-0040-766F6C756D01 to bag -5, persona none, compressed uuid: 2048435297, flags: 1
MKB_INIT: Could not open /private/var/keybags/systembag.kb: No such file or directory
MKB_INIT: No system keybag found on filesystem.
MKB_INIT: No system keybag loaded.
"AppleSEPKeyStore":pid:18,sel:120:7561: Loaded identity 071ED7A3-3AF7-4D1F-8C57-03BF5AFDC8D4 as session -501
"AppleSEPKeyStore":pid:0,:3937: Sending First Unlock notification for handle -501
"AppleSEPKeyStore":pid:0,:3941: Sending lock state change unlocked (0) for handle -501 (0x1)
"AppleSEPKeyStore":pid:18,sel:65:2726: mapped uuid 61706673-7575-6964-0002-766F6C756D06 to bag -501, persona none, compressed uuid: 118820932, flags: 0
_process_matches_constraint: error: Requirement not satisfied: Constraint not matched
apfs_unwrap_key:2054: AKS unwrap_key failed, error = e007c013
retain_crypto_states:10475: disk1s7 failed to retain file dstream crypto states (error 1)
icp_open_retain:1838: disk1s7 retain_crypto_states failed on ino 191838 with 1
"AppleSEPKeyStore":pid:36,:13253: operation failed (sel: 135 ret: e00002e6)
"AppleSEPKeyStore":pid:78,:13253: operation failed (sel: 57 ret: e00002c2)
"""


class PersistenceProbeToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="tomacco-persistence-tool-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.cli = self.root / "tomaccoctl"
        self.cli.write_text(FAKE_CLI)
        self.cli.chmod(0o755)
        os.environ["FAKE_ROOT"] = str(self.root)
        self.scenario()

    def scenario(self, **values):
        (self.root / "scenario.json").write_text(json.dumps(values))

    def calls(self, operation):
        return [row["arguments"] for row in map(json.loads, (self.root / "calls.jsonl").read_text().splitlines())
                if row["operation"] == operation]

    def run_tool(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = probe.main(["--cli", str(self.cli), "--pause", "0", "--patience", "0.01", *arguments])
            except SystemExit as stop:  # argparse errors
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def test_create_generates_a_challenge_takes_the_job_from_the_status_and_prints_the_class_c_row(self):
        code, out, _ = self.run_tool("create")
        self.assertEqual(code, 0, out)
        (call,) = self.calls("ios.emulator.guest")
        self.assertEqual((call["job_id"], call["operation"], call["timeout_seconds"]),
                         ("771d3800ce3c4a9796f88ee7ada1e2f5", "persistence_probe", 120))
        self.assertRegex(call["argument"], r"^create:[0-9a-f]{64}$")
        self.assertEqual(len(self.calls("ios.virtual.status")), 1)
        row = [line for line in out.splitlines() if line.startswith("C (3)")]
        self.assertEqual(len(row), 1, out)
        self.assertEqual(row[0].split(), ["C", "(3)", "create", "201568", "3", "64", "yes", "ok"])
        self.assertIn("one boot only", out)
        self.assertIn(call["argument"][7:], out)

    def test_read_needs_the_challenge_and_keeps_a_native_refusal_visible(self):
        code, _, err = self.run_tool("read")
        self.assertEqual(code, 2)
        self.assertIn("--challenge", err)
        self.assertFalse((self.root / "calls.jsonl").exists())
        self.scenario(directory_missing=True)
        challenge = "f234f9ab643546aa93074142631aa2e13a39bbd84de0cf3bba5ee9c7db06e696"
        code, out, _ = self.run_tool("read", "--challenge", challenge, "--job", "771d3800ce3c4a9796f88ee7ada1e2f5")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.calls("ios.virtual.status"), [])
        self.assertEqual(self.calls("ios.emulator.guest")[0]["argument"], f"read:{challenge}")
        self.assertIn("persistence_directory_unavailable errno 2 (ENOENT)", out)
        row = [line for line in out.splitlines() if line.startswith("C (3)")][0]
        self.assertIn("read", row)
        self.assertIn("no", row.split())

    def test_the_class_survey_prints_one_row_per_class_with_the_failing_step(self):
        code, out, _ = self.run_tool("classes", "--challenge", "ab" * 32)
        self.assertEqual(code, 1, out)
        self.assertEqual(self.calls("ios.emulator.guest")[0]["argument"], "classes:" + "ab" * 32)
        lines = out.splitlines()
        rows = {line.split()[0]: line for line in lines if re.match(r"^[A-D] \(\d\)", line)}
        self.assertEqual(sorted(rows), ["A", "B", "C", "D"])
        self.assertIn("persistence_set_class_failed errno 1 (EPERM) (file removed)", rows["A"])
        self.assertNotIn("yes", rows["A"].split())
        self.assertEqual(rows["B"].split(), ["B", "(2)", "create", "201571", "2", "64", "yes", "ok"])
        self.assertEqual(rows["C"].split()[2:], ["read", "201572", "3", "64", "yes", "ok"])
        self.assertEqual(rows["D"].split()[2:], ["read", "201573", "4", "64", "yes", "ok"])
        self.assertTrue(any(line.startswith("3/4 classes read back") for line in lines), out)
        self.assertIn("NSFileProtectionCompleteUntilFirstUserAuthentication", out)

    def test_an_inode_lookup_prints_the_path_and_class_without_opening_the_file(self):
        code, _, err = self.run_tool("inode")
        self.assertEqual(code, 2)
        self.assertIn("--inode", err)
        code, out, _ = self.run_tool("inode", "--inode", "191838")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls("ios.emulator.guest")[0]["argument"], "inode:191838")
        self.assertIn("inode 191838 on volume fsid 16777229:", out)
        self.assertIn("path /private/var/mobile/Library/Caches/example.db", out)
        self.assertIn("object type 1 (regular file)", out)
        self.assertIn("protection class F (6) (the system's own: E keybag-only, F ephemeral key, CX)", out)
        self.assertIn("the file was not opened", out)
        self.scenario(inode_class=1)
        code, out, _ = self.run_tool("inode", "--inode", "7")
        self.assertIn("protection class A (1) NSFileProtectionComplete", out)
        self.scenario(guest_tools=369)
        code, out, _ = self.run_tool("inode", "--inode", "7")
        self.assertEqual(code, 2)
        self.assertIn("the inode lookup needs guest tools from 370", out)

    def test_guest_tools_or_a_cli_without_the_survey_are_named(self):
        self.scenario(guest_tools=369)
        code, out, _ = self.run_tool("classes")
        self.assertEqual(code, 2, out)
        self.assertIn("guest tools from 370", out)
        self.assertIn("invalid_guest_response", out)

    def test_a_busy_guest_is_asked_again_with_the_same_argument(self):
        self.scenario(busy=1)
        code, out, _ = self.run_tool("create")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.calls("ios.emulator.guest")), 2)
        self.assertEqual(len({call["argument"] for call in self.calls("ios.emulator.guest")}), 1)

    def test_a_request_with_an_unknown_outcome_is_reported_and_not_resent(self):
        self.scenario(lost=True)
        code, out, err = self.run_tool("create")
        self.assertEqual(code, 2, out + err)
        (call,) = self.calls("ios.emulator.guest")
        self.assertRegex(call["argument"], r"^create:[0-9a-f]{64}$")
        self.assertIn("guest_worker_timeout: The guest did not answer within 120 seconds", out)
        self.assertIn("request 3c99e8f852ee4fd6a7fb62d77d83fc10 may have executed", out)
        # Busy refusals before the lost reply are asked again; the lost one is not.
        (self.root / "calls.jsonl").unlink()
        self.scenario(busy=1, lost=True)
        code, out, _ = self.run_tool("read", "--challenge", "ab" * 32)
        self.assertEqual(code, 2, out)
        self.assertEqual([call["argument"] for call in self.calls("ios.emulator.guest")], ["read:" + "ab" * 32] * 2)
        self.assertIn("request 3c99e8f852ee4fd6a7fb62d77d83fc11 may have executed", out)

    def test_a_guest_that_is_not_running_is_refused_before_any_request(self):
        self.scenario(guest_running=False)
        code, _, err = self.run_tool("read", "--challenge", "0" * 64)
        self.assertEqual(code, 2)
        self.assertIn("no running guest", err)
        self.assertEqual(self.calls("ios.emulator.guest"), [])

    def test_the_raw_reply_is_saved_and_printed_as_json_when_asked(self):
        saved = self.root / "reply.json"
        code, out, _ = self.run_tool("create", "--save", str(saved), "--json")
        self.assertEqual(code, 0)
        reply = json.loads(saved.read_text())
        self.assertEqual(json.loads(out), reply)
        self.assertEqual(reply["result"]["response"]["file_inode"], 201568)
        self.assertEqual(reply["result"]["response"]["probe_action"], "create")

    def test_ioreturn_values_decode_into_system_subsystem_and_code(self):
        self.assertEqual(probe.decode_ioreturn("e00002c2")["name"], "kIOReturnBadArgument")
        self.assertEqual(probe.decode_ioreturn("e00002e6")["name"], "kIOReturnUnsupportedMode")
        keystore = probe.decode_ioreturn("e007c013")
        self.assertEqual((keystore["system"], keystore["subsystem"], keystore["family"], keystore["code"]),
                         ("iokit", 31, "keystore", 0x13))
        self.assertEqual(keystore["name"], "keystore family code 0x13")
        self.assertEqual(probe.decode_ioreturn("0")["name"], "kIOReturnSuccess")
        self.assertIsNone(probe.decode_ioreturn("10000003")["name"])

    def test_the_keybag_summary_reads_the_run_324_boot(self):
        summary = probe.keybag_summary(UART_EXCERPT)
        self.assertEqual(summary["fake_effaceable_key"], {"line": 1, "key": "0xffffffff"})
        self.assertEqual(summary["system_keybag_file"],
                         {"line": 4, "path": "/private/var/keybags/systembag.kb", "present": False})
        self.assertEqual(summary["system_keybag_loaded"]["text"], "No system keybag loaded")
        self.assertEqual(summary["identities"], [{"line": 7, "identity": "071ED7A3-3AF7-4D1F-8C57-03BF5AFDC8D4",
                                                  "session": -501}])
        self.assertEqual(summary["first_unlock"], [{"line": 8, "handle": -501}])
        self.assertEqual(summary["lock_states"], [{"line": 9, "state": "unlocked", "value": 0, "handle": -501}])
        self.assertEqual([(bag["volume"][-2:], bag["bag"]) for bag in summary["volume_bags"]],
                         [("01", -5), ("06", -501)])
        self.assertEqual(summary["media_key_failures"],
                         [{"line": 2, "volume": "61706673-7575-6964-0002-766F6C756D06", "tag": 2, "errno": 2}])
        (unwrap,) = summary["unwrap_failures"]
        self.assertEqual((unwrap["line"], unwrap["volume"], unwrap["inode"], unwrap["errno"]), (12, "disk1s7", 191838, 1))
        self.assertEqual((unwrap["error"]["subsystem"], unwrap["error"]["code"]), (31, 0x13))
        self.assertEqual([(f["pid"], f["selector"], f["error"]["name"]) for f in summary["keystore_failures"]],
                         [(36, 135, "kIOReturnUnsupportedMode"), (78, 57, "kIOReturnBadArgument")])
        text = probe.format_keybag(summary)
        self.assertIn("systembag.kb absent", text)
        self.assertIn("e007c013 = keystore family code 0x13 (system iokit, subsystem 31, code 0x13); "
                      "disk1s7 inode 191838, opener gets errno 1 (EPERM)", text)
        self.assertIn("pid 36 selector 135 -> e00002e6 = kIOReturnUnsupportedMode", text)

    def test_the_keybag_action_reads_a_log_without_touching_the_guest(self):
        log = self.root / "uart.log"
        log.write_text(UART_EXCERPT)
        saved = self.root / "keybag.json"
        code, out, _ = self.run_tool("keybag", "--uart-log", str(log), "--save", str(saved))
        self.assertEqual(code, 0)
        self.assertFalse((self.root / "calls.jsonl").exists())
        self.assertIn("first unlock notified for handle -501", out)
        self.assertEqual(json.loads(saved.read_text())["unwrap_failures"][0]["inode"], 191838)
        code, _, err = self.run_tool("keybag")
        self.assertEqual(code, 2)
        self.assertIn("--uart-log", err)


if __name__ == "__main__":
    unittest.main()
