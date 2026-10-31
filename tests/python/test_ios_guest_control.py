"""Real CLI/Unix-stream boundaries. These fixtures are not iOS execution evidence."""
import base64
import fcntl
import json
import os
from pathlib import Path
import socket
import threading
import time

from native_client import StudioError, dispatch, jobs
from test_product import Isolated
from host_patience import PATIENCE
import test_ios_paravirt_job as guest_fixture


class Peer:
    def __init__(self, path):
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(path))
        self.listener.listen(8)
        self.listener.settimeout(.1)
        self.requests = []
        self.errors = []
        self.stop = threading.Event()
        self.reply = lambda request: b"TOMACCO_APP_CONTROL " + json.dumps({
            "format": 1, "operation": request["operation"], "ok": True,
            "application_scene_verified": False}).encode() + b"\n"
        self.handshake = b"OK 1339\n"
        self.delay = 0
        self.fragment = 113
        self.clients = []
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        try:
            while not self.stop.is_set():
                try:
                    client, _ = self.listener.accept()
                except socket.timeout:
                    continue
                worker = threading.Thread(target=self.respond, args=(client,), daemon=True)
                self.clients.append(worker)
                worker.start()
        except BaseException as error:
            self.errors.append(error)

    def respond(self, client):
        try:
            with client, client.makefile("rb") as reader:
                client.settimeout(2)
                assert reader.readline() == b"CONNECT 1339\n"
                if not self.handshake:
                    return
                client.sendall(self.handshake)
                request = json.loads(reader.readline())
                self.requests.append(request)
                reply = self.reply(request)
                for offset in range(0, len(reply), self.fragment):
                    if self.stop.wait(self.delay):
                        break
                    try:
                        client.sendall(reply[offset:offset + self.fragment])
                    except (BrokenPipeError, ConnectionResetError):
                        break
        except BaseException as error:
            self.errors.append(error)

    def close(self):
        self.stop.set()
        self.thread.join(3)
        self.listener.close()
        assert not self.thread.is_alive()
        for worker in self.clients:
            worker.join(3)
            assert not worker.is_alive()
        assert not self.errors, self.errors


class GuestControlTests(Isolated):
    def test_backend_start_failure_reports_without_starting_the_emulator(self):
        arguments = guest_fixture.ParavirtualJobTests.arguments(self, wait=True)
        marker = self.root / "emulator-started"
        Path(arguments["engine"]).write_text("#!/bin/sh\ntouch '" + str(marker) + "'\n")
        backend = self.root / "failed-backend"
        backend.write_text("#!/bin/sh\necho 'fixture startup refused'\nexit 7\n")
        backend.chmod(0o755)
        job = dispatch("ios.emulator.paravirtual", {**arguments, "guest_control_backend": str(backend)})
        self.assertEqual(jobs.wait(job["id"], 8)["state"], "failed")
        report = dispatch("ios.emulator.report", {"job_id": job["id"]})["report"]
        self.assertEqual(report["outcome"], "guest-control-start-error")
        self.assertFalse(report["qualified_for_app_development"])
        self.assertFalse(marker.exists())

    def guest(self, interactive=False):
        arguments = guest_fixture.ParavirtualJobTests.arguments(self, wait=True)
        backend = self.root / "vsock-fixture"
        backend.write_text("""#!/usr/bin/python3
import os, socket, sys, time
from pathlib import Path
Path(os.environ['TOMACCO_TEST_ROOT'], 'backend.pid').write_text(str(os.getpid()))
s = socket.socket(socket.AF_UNIX)
s.bind(sys.argv[sys.argv.index('--socket') + 1])
s.listen()
time.sleep(60)
""")
        backend.chmod(0o755)
        self.job = dispatch("ios.emulator.paravirtual", {
            **arguments, "guest_control_backend": str(backend), "seconds": 30,
            "interactive": interactive})
        self.receipt = Path(self.job["report_directory"]) / "paravirtual/request.json"
        deadline = time.monotonic() + 6 * PATIENCE
        while not self.receipt.exists():
            self.assertLess(time.monotonic(), deadline, jobs.logs(self.job["id"]))
            time.sleep(.02)
        self.request = json.loads(self.receipt.read_text())
        self.peer = Peer(Path(self.request["guest_control"]["socket"]))
        self.addCleanup(self.peer.close)
        return self.peer

    def call(self, operation="inspect", **arguments):
        return dispatch("ios.emulator.guest", {"job_id": self.job["id"],
                                              "operation": operation, **arguments})

    def test_persistence_probe_preserves_native_result_and_bounds_the_challenge(self):
        peer = self.guest()
        challenge = "0123456789abcdef" * 4
        native = {"format": 1, "operation": "persistence_probe", "ok": True,
                  "class_c_verified": True, "contents_match": True,
                  "paired_reboot_verified": False}
        peer.reply = lambda request: b"TOMACCO_APP_CONTROL " + json.dumps({
            **native, "request_id": request["request_id"]}).encode() + b"\n"
        for action in ("create", "read"):
            result = self.call("persistence_probe", argument=f"{action}:{challenge}")
            self.assertEqual(result["response"]["contents_match"], True)
            self.assertFalse(result["response"]["paired_reboot_verified"])
            self.assertEqual(peer.requests[-1]["argument"], f"{action}:{challenge}")
        count = len(peer.requests)
        for invalid in (None, "", "read:../file", f"delete:{challenge}",
                        "read:" + "A" * 64, "create:" + "a" * 65):
            with self.subTest(argument=invalid), self.assertRaises(StudioError):
                self.call("persistence_probe", **({"argument": invalid} if invalid is not None else {}))
        self.assertEqual(len(peer.requests), count)

    def test_persistence_create_and_read_recover_original_receipts_without_replay(self):
        peer = self.guest()
        challenge = "a" * 64
        for action in ("create", "read"):
            peer.reply = lambda _: b""
            with self.assertRaises(StudioError) as lost:
                self.call("persistence_probe", argument=f"{action}:{challenge}")
            details = lost.exception.details
            self.assertEqual(details["automatic_retry_safe"], action == "read")
            receipt = {"format": 1, "operation": "persistence_probe", "ok": False,
                       "request_id": details["request_id"], "error": "persistence_set_class_failed",
                       "errno": 13, "probe_action": action, "file_created": action == "create"}
            def reply(request):
                row = {"format": 1, "operation": request["operation"], "ok": True}
                if request["operation"] == "status":
                    row.update(worker_running=False, worker_request_id=None)
                else:
                    row.update(text=json.dumps(receipt), truncated=False)
                return b"TOMACCO_APP_CONTROL " + json.dumps(row).encode() + b"\n"
            peer.reply = reply
            recovered = dispatch(details["recovery"]["operation"], details["recovery"]["arguments"])
            self.assertEqual(recovered["response"], receipt)
            self.assertEqual(recovered["state"], "completed")
            self.assertTrue(recovered["ready_for_next_request"])
            self.assertFalse(recovered["response"]["ok"])
            self.assertEqual([r["operation"] for r in peer.requests[-3:]],
                             ["persistence_probe", "status", "logs"])

    @staticmethod
    def crash_report_fixture():
        header = {"app_name": "Preferences", "incident_id": "fixture-incident"}
        body = {"pid": 551, "procName": "Preferences",
                "procLaunch": "2026-10-13 11:00:00 +0000",
                "captureTime": "2026-10-13 11:11:24 +0000"}
        content = (json.dumps(header) + "\n" + json.dumps(body)).encode()
        return {"format": 1, "operation": "crash_reports", "ok": True,
                "process": "Preferences", "collection_complete": False,
                "reports": [{"name": "Preferences-2026-10-13-111124.ips", "content_complete": True,
                             "content_bytes": len(content), "observed_file_bytes": len(content),
                             "content_base64": base64.b64encode(content).decode(),
                             "file_consistency_verified": False}]}

    def assert_crash_report_identity(self, result, native):
        self.assertEqual(result["response"], native)
        projection = result["crash_report_identities"]
        self.assertEqual(projection["state"], "projected")
        self.assertFalse(projection["process_lifetime_bound"])
        self.assertFalse(projection["absence_proves_no_crash"])
        identity, = projection["identities"]
        self.assertEqual(identity["state"], "parsed")
        self.assertEqual(identity["report_index"], 0)
        self.assertEqual(identity["pid"], 551)
        self.assertEqual(identity["process"], "Preferences")
        self.assertEqual(identity["capture_time"], "2026-10-13 11:11:24 +0000")
        self.assertFalse(identity["process_lifetime_bound"])
        self.assertFalse(identity["native_file_consistency_verified"])

    def test_direct_crash_reports_preserve_bytes_and_project_historical_identity(self):
        peer = self.guest()
        native = self.crash_report_fixture()
        peer.reply = lambda _: b"TOMACCO_APP_CONTROL " + json.dumps(native).encode() + b"\n"
        result = self.call("crash_reports", argument="Preferences")
        self.assert_crash_report_identity(result, native)
        self.assertEqual(peer.requests, [{"operation": "crash_reports", "argument": "Preferences",
                                         "request_id": result["request_id"]}])

    def test_recovered_crash_reports_project_identity_without_replaying_query(self):
        peer = self.guest()
        request_id = "a" * 32
        native = {**self.crash_report_fixture(), "request_id": request_id}
        def reply(request):
            row = {"format": 1, "operation": request["operation"], "ok": True}
            if request["operation"] == "status":
                row.update(worker_running=False)
            else:
                row.update(text=json.dumps(native), truncated=False)
            return b"TOMACCO_APP_CONTROL " + json.dumps(row).encode() + b"\n"
        peer.reply = reply
        result = dispatch("ios.emulator.guest_result", {
            "job_id": self.job["id"], "request_id": request_id})
        self.assertEqual(result["state"], "completed")
        self.assert_crash_report_identity(result, native)
        self.assertEqual(peer.requests, [{"operation": "status"},
                                         {"operation": "logs", "argument": "operation"}])

    def test_fragmented_large_reply_and_guest_refusal_remain_actual_responses(self):
        peer = self.guest()
        value = {"format": 1, "operation": "logs", "ok": True, "text": "aä📱" * 32000}
        peer.reply = lambda _: b"TOMACCO_APP_CONTROL " + json.dumps(value, ensure_ascii=False).encode() + b"\n"
        response = self.call("logs", argument="application")
        self.assertTrue(response["guest_response_received"])
        self.assertEqual(response["response"], value)
        self.assertEqual(peer.requests, [{"operation": "logs", "argument": "application",
                                         "request_id": response["request_id"]}])
        self.assertRegex(response["request_id"], r"^[0-9a-f]{32}$")
        peer.reply = lambda _: b'TOMACCO_APP_CONTROL {"format":1,"operation":"open","ok":false,"error":"request_rejected_by_ios"}\n'
        response = self.call("open", argument="dev.example.app")
        self.assertFalse(response["response"]["ok"])

    def test_targeted_lookup_and_explicit_worker_cancel_use_the_guest_protocol(self):
        peer = self.guest()
        lookup = self.call("lookup", argument="dev.example.app")
        self.call("cancel")
        self.assertEqual(peer.requests, [{"operation": "lookup", "argument": "dev.example.app",
                                         "request_id": lookup["request_id"]},
                                         {"operation": "cancel"}])

    def test_setup_state_and_refusal_keep_the_native_result(self):
        peer = self.guest()
        def reply(request):
            row = {"format": 1, "operation": request["operation"],
                   "request_id": request["request_id"], "activation_state": "Unactivated",
                   "native_setup_required": True, "virtual_device_verified": True,
                   "application_scene_verified": False, "ok": request["operation"] == "setup"}
            if request["operation"] == "setup_finish":
                row["error"] = "device_not_activated"
            return b"TOMACCO_APP_CONTROL " + json.dumps(row).encode() + b"\n"
        peer.reply = reply
        for operation in ("setup", "setup_finish"):
            result = dispatch("ios.emulator.guest", {"job_id": self.job["id"], "operation": operation})
            self.assertEqual(result["response"]["request_id"], result["request_id"])
            self.assertEqual(result["response"]["activation_state"], "Unactivated")
            self.assertEqual(result["response"]["ok"], operation == "setup")
            self.assertFalse(result["response"]["application_scene_verified"])
            with self.assertRaises(StudioError):
                dispatch("ios.emulator.guest", {"job_id": self.job["id"], "operation": operation,
                                                "argument": "unexpected"})
        self.assertEqual([row["operation"] for row in peer.requests], ["setup", "setup_finish"])

    def test_ui_restart_takes_no_argument_and_keeps_the_native_receipt(self):
        peer = self.guest()
        peer.reply = lambda request: b"TOMACCO_APP_CONTROL " + json.dumps({
            "format": 1, "operation": "restart_ui", "request_id": request["request_id"], "ok": True,
            "previous_pid": 36, "ui_pid": 412, "storage_flushed": True,
            "application_scene_verified": False}).encode() + b"\n"
        result = dispatch("ios.emulator.guest", {"job_id": self.job["id"], "operation": "restart_ui"})
        self.assertEqual(result["response"]["ui_pid"], 412)
        self.assertFalse(result["response"]["application_scene_verified"])
        with self.assertRaises(StudioError):
            dispatch("ios.emulator.guest", {"job_id": self.job["id"], "operation": "restart_ui",
                                            "argument": "unexpected"})
        self.assertEqual([row["operation"] for row in peer.requests], ["restart_ui"])

    def test_foreground_query_preserves_native_denial_and_identifier_without_scene_claim(self):
        peer = self.guest()
        for status in (5, 0):
            def reply(request):
                row = {"format": 1, "operation": "foreground_state",
                       "request_id": request["request_id"], "ok": status == 0,
                       "foreground_native_status": status,
                       "foreground_identifier_known": status == 0,
                       "application_scene_verified": False}
                if status:
                    row["error"] = "foreground_query_rpc_failed"
                else:
                    row["frontmost_bundle_id"] = "com.apple.Preferences"
                return b"TOMACCO_APP_CONTROL " + json.dumps(row).encode() + b"\n"
            peer.reply = reply
            result = self.call("foreground_state")
            self.assertEqual(result["response"]["foreground_native_status"], status)
            self.assertEqual(result["response"]["foreground_identifier_known"], status == 0)
            self.assertFalse(result["response"]["application_scene_verified"])
        with self.assertRaises(StudioError):
            self.call("foreground_state", argument="unexpected")
        self.assertEqual([row["operation"] for row in peer.requests],
                         ["foreground_state", "foreground_state"])

    def test_native_launch_errors_keep_frontboard_cause_without_replay(self):
        peer = self.guest()
        native = {
            "domain": "FBSOpenApplicationServiceErrorDomain", "code": 1,
            "code_description": "RequestDenied", "message": "Application launch rejected",
            "underlying": {
                "domain": "FBSOpenApplicationErrorDomain", "code": 6,
                "code_description": "Busy", "failure_reason": "Device setup is not yet complete.",
            },
        }
        def reply(request):
            row = {"format": 1, "operation": "open", "request_id": request["request_id"],
                   "ok": False, "request_accepted": False, "application_scene_verified": False,
                   "launch_service": "FrontBoard", "error": "request_rejected_by_ios",
                   "native_error": native}
            return b"TOMACCO_APP_CONTROL " + json.dumps(row).encode() + b"\n"
        peer.reply = reply
        response = self.call("open", argument="dev.example.app")
        self.assertTrue(response["guest_response_received"])
        self.assertFalse(response["response"]["request_accepted"])
        self.assertFalse(response["response"]["application_scene_verified"])
        self.assertEqual(response["response"]["native_error"], native)
        self.assertEqual(response["response"]["launch_service"], "FrontBoard")
        self.assertEqual(peer.requests, [{"operation": "open", "argument": "dev.example.app",
                                         "request_id": response["request_id"]}])

    @staticmethod
    def launch_process_fixture():
        return {"format": 1, "operation": "open", "pid": 425, "worker_pid": 425,
                "pid_role": "request-worker", "ok": True, "request_accepted": True,
                "argument": "com.apple.Preferences", "launch_service": "FrontBoard",
                "application_scene_verified": False,
                "launch_process_pid_known": True, "launch_process_pid": 551,
                "launch_process_pid_source": "FrontBoard-completion-BSProcessHandle.pid",
                "launch_process_start_snapshot_known": True,
                "launch_process_start_snapshot": {"pid": 551, "start_seconds": 1791890000,
                                                  "start_microseconds": 596100,
                                                  "process_name": "Preferences"},
                "launch_process_lifetime_bound": False,
                "launch_process_snapshot_atomic_with_handle": False}

    def test_direct_open_preserves_native_app_identity_separately_from_worker_pid(self):
        peer = self.guest()
        native = self.launch_process_fixture()
        peer.reply = lambda _: b"TOMACCO_APP_CONTROL " + json.dumps(native).encode() + b"\n"
        result = self.call("open", argument="com.apple.Preferences")
        self.assertEqual(result["response"], native)
        self.assertNotEqual(native["pid"], native["launch_process_pid"])
        self.assertFalse(result["response"]["application_scene_verified"])
        self.assertFalse(result["response"]["launch_process_lifetime_bound"])
        self.assertEqual(len(peer.requests), 1)

    def test_recovered_open_preserves_process_handle_identity_without_launch_replay(self):
        peer = self.guest()
        request_id = "b" * 32
        native = {**self.launch_process_fixture(), "request_id": request_id}

        def reply(request):
            row = {"format": 1, "operation": request["operation"], "ok": True}
            if request["operation"] == "status":
                row.update(worker_running=False)
            else:
                row.update(text=json.dumps(native), truncated=False)
            return b"TOMACCO_APP_CONTROL " + json.dumps(row).encode() + b"\n"

        peer.reply = reply
        result = dispatch("ios.emulator.guest_result", {
            "job_id": self.job["id"], "request_id": request_id})
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["response"], native)
        self.assertEqual(peer.requests, [{"operation": "status"},
                                         {"operation": "logs", "argument": "operation"}])

    def test_framework_queries_recover_late_results_without_resubmission(self):
        peer = self.guest()
        for operation, argument in (("lookup", "dev.example.app"), ("inspect", None),
                                    ("logs", "application"), ("foreground_state", None)):
            with self.subTest(operation=operation):
                peer.reply = lambda _: b""
                with self.assertRaises(StudioError) as timeout:
                    self.call(operation, **({"argument": argument} if argument else {}))
                details = timeout.exception.details
                request_id = details["request_id"]
                self.assertRegex(request_id, r"^[0-9a-f]{32}$")
                self.assertTrue(details["automatic_retry_safe"])
                receipt = {"format": 1, "operation": operation, "ok": True,
                           "request_id": request_id, "installed": False}
                def reply(request):
                    row = {"format": 1, "operation": request["operation"], "ok": True}
                    if request["operation"] == "status":
                        row.update(worker_running=False, worker_request_id=None)
                    else:
                        row.update(text=json.dumps(receipt), truncated=False)
                    return b"TOMACCO_APP_CONTROL " + json.dumps(row).encode() + b"\n"
                peer.reply = reply
                result = dispatch(details["recovery"]["operation"], details["recovery"]["arguments"])
                self.assertEqual(result["state"], "completed")
                self.assertEqual(result["response"], receipt)
                self.assertEqual([r["operation"] for r in peer.requests[-3:]],
                                 [operation, "status", "logs"])
                self.assertNotIn("request_id", peer.requests[-2])
                self.assertNotIn("request_id", peer.requests[-1])

    def test_install_preserves_native_failure_and_does_not_retry_unknown_result(self):
        peer = self.guest()
        native = {"domain": "MIInstallerErrorDomain", "code": 13, "message": "Rejected package",
                  "underlying": {"domain": "ExampleVerification", "code": -1, "message": "Invalid signature"}}
        reply = {"format": 1, "operation": "install", "ok": False,
                 "error": "installation_rejected_by_ios", "native_error": native}
        peer.reply = lambda _: b"TOMACCO_APP_CONTROL " + json.dumps(reply).encode() + b"\n"
        self.assertEqual(self.call("install", argument="/staging/Test.app")["response"], reply)
        peer.reply = lambda _: b""
        with self.assertRaises(StudioError) as error:
            self.call("install", argument="/staging/Test.ipa", bundle_id="dev.example.app")
        self.assertTrue(error.exception.details["request_may_have_executed"])
        self.assertFalse(error.exception.details["automatic_retry_safe"])
        self.assertEqual(len(peer.requests), 2)
        self.assertEqual(peer.requests[-1]["bundle_id"], "dev.example.app")
        for invalid in ({"operation": "install", "argument": "/staging/Test.ipa"},
                        {"operation": "install", "argument": "/staging/Test.ipa", "bundle_id": "invalid/id"},
                        {"operation": "install", "argument": "/staging/Test.app", "bundle_id": 42},
                        {"operation": "lookup", "argument": "dev.example.app", "bundle_id": "dev.example.app"}):
            with self.subTest(request=invalid), self.assertRaises(StudioError) as rejected:
                self.call(**invalid)
            self.assertIn(rejected.exception.code, ("invalid_argument", "invalid_guest_request"))
        self.assertEqual(len(peer.requests), 2, "invalid identities must never reach the guest")
        with self.assertRaises(StudioError):
            self.call("install", argument="relative/Test.app")
        self.assertEqual(len(peer.requests), 2)
        with self.assertRaises(StudioError):
            self.call("cancel", argument="unrelated-process")
        self.assertEqual(len(peer.requests), 2)

    def test_status_logs_and_cancel_remain_available_during_a_pending_install(self):
        peer = self.guest()
        received, release = threading.Event(), threading.Event()
        outcome = []
        def reply(request):
            if request["operation"] == "install":
                received.set()
                if not release.wait(10):
                    raise AssertionError("pending install was not released")
            row = {"format": 1, "operation": request["operation"], "ok": True}
            if request["operation"] == "status":
                row.update(worker_running=True, worker_pid=42, worker_operation="install",
                           worker_request_id=peer.requests[0]["request_id"])
            return b"TOMACCO_APP_CONTROL " + json.dumps(row).encode() + b"\n"
        peer.reply = reply
        def install():
            try:
                outcome.append(self.call("install", argument="/staging/Test.app", timeout_seconds=10))
            except BaseException as error:
                outcome.append(error)
        pending = threading.Thread(target=install, daemon=True)
        pending.start()
        try:
            self.assertTrue(received.wait(3 * PATIENCE), "install request did not arrive")
            with self.assertRaises(StudioError) as error:
                self.call("lookup", argument="dev.example.app")
            self.assertEqual(error.exception.code, "ios_guest_busy")
            with self.assertRaises(StudioError) as error:
                self.call("logs", argument="application")
            self.assertEqual(error.exception.code, "ios_guest_busy")
            status = self.call("status", timeout_seconds=2)["response"]
            self.assertTrue(status["worker_running"])
            self.assertEqual(status["worker_request_id"], peer.requests[0]["request_id"])
            self.call("logs", argument="operation", timeout_seconds=2)
            self.call("cancel", timeout_seconds=2)
            self.assertTrue(pending.is_alive(), "diagnostics must not depend on the install reply")
        finally:
            release.set()
            pending.join(4)
        self.assertFalse(pending.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], dict)
        self.assertTrue(outcome[0]["guest_response_received"])
        self.assertEqual([row["operation"] for row in peer.requests], ["install", "status", "logs", "cancel"])

    def test_mutation_receipts_keep_the_id_across_late_or_mismatched_responses(self):
        peer = self.guest()
        def reply(request):
            return b"TOMACCO_APP_CONTROL " + json.dumps({
                "format": 1, "operation": request["operation"], "ok": True,
                "request_id": request["request_id"]}).encode() + b"\n"
        peer.reply = reply
        result = self.call("install", argument="/staging/Test.app")
        first_id = result["request_id"]
        self.assertRegex(first_id, r"^[0-9a-f]{32}$")
        self.assertEqual(first_id, peer.requests[0]["request_id"])
        self.assertEqual(first_id, result["response"]["request_id"])
        peer.reply = lambda request: reply({**request, "request_id": first_id})
        with self.assertRaises(StudioError) as error:
            self.call("install", argument="/staging/Test.app")
        self.assertEqual(error.exception.code, "invalid_guest_response")
        self.assertNotEqual(error.exception.details["request_id"], first_id)
        self.assertEqual(error.exception.details["request_id"], peer.requests[1]["request_id"])
        self.assertFalse(error.exception.details["automatic_retry_safe"])
        peer.reply = lambda _: b""
        with self.assertRaises(StudioError) as error:
            self.call("open", argument="dev.example.app")
        self.assertEqual(error.exception.details["request_id"], peer.requests[2]["request_id"])
        self.assertEqual(len({row["request_id"] for row in peer.requests}), 3)
        peer.reply = lambda _: b'TOMACCO_APP_CONTROL {"format":1,"operation":"logs","ok":true,"text":"last receipt"}\n'
        self.assertEqual(self.call("logs", argument="operation")["response"]["text"], "last receipt")
        self.assertEqual(peer.requests[-1], {"operation": "logs", "argument": "operation"})
        self.assertEqual(len(peer.requests), 4, "mutations must not be retried")

    def test_incomplete_mutation_is_ambiguous_and_never_retried(self):
        peer = self.guest()
        peer.reply = lambda _: b"TOMACCO_APP_CONTROL "
        with self.assertRaises(StudioError) as error:
            self.call("register", argument="/private/dev/Example.app")
        self.assertEqual(error.exception.code, "ios_guest_disconnected")
        self.assertTrue(error.exception.details["request_may_have_executed"])
        self.assertFalse(error.exception.details["automatic_retry_safe"])
        self.assertEqual(len(peer.requests), 1)
        self.assertEqual(error.exception.details["recovery"], {
            "operation": "ios.emulator.guest_result", "arguments": {
                "job_id": self.job["id"], "request_id": peer.requests[0]["request_id"]}})
        peer.handshake = b""
        with self.assertRaises(StudioError) as error:
            self.call()
        self.assertEqual(error.exception.code, "ios_guest_starting")
        self.assertEqual(len(peer.requests), 1)

    def test_result_correlates_receipts_without_replaying_the_mutation(self):
        peer = self.guest()
        request_id, previous_id = "a" * 32, "b" * 32
        success = {"format": 1, "operation": "install", "ok": True,
                   "request_id": request_id, "request_accepted": True,
                   "application_scene_verified": False}
        rejected = {**success, "ok": False, "error": "installation_rejected_by_ios",
                    "native_error": {"domain": "MIInstallerErrorDomain", "code": 13}}
        cancelled = {**rejected, "error": "guest_worker_cancelled",
                     "request_may_have_executed": True, "automatic_retry_safe": False}
        timed_out = {**cancelled, "error": "guest_worker_timeout"}
        unconfirmed_launch = {**cancelled, "operation": "open", "error": "launch_completion_unconfirmed"}
        unconfirmed_setup = {**cancelled, "operation": "setup_finish", "error": "setup_write_unconfirmed"}
        cases = [
            (True, success, "completed"),  # A callback after status wins.
            (False, rejected, "completed"),
            (True, {**success, "request_id": previous_id}, "running"),
            (False, {**success, "request_id": previous_id}, "unknown"),
            (False, cancelled, "unknown"),
            (True, timed_out, "unknown"),
            (False, unconfirmed_launch, "unknown"),
            (False, unconfirmed_setup, "unknown"),
            (False, {**success, "operation": "setup", "native_setup_required": True}, "completed"),
            (False, {**success, "operation": "setup_finish", "restart_required": True}, "completed"),
            (True, None, "running"),
            (False, None, "unknown"),
        ]
        for running, receipt, state in cases:
            with self.subTest(state=state, running=running, receipt=receipt):
                def reply(request):
                    row = {"format": 1, "operation": request["operation"], "ok": True}
                    if request["operation"] == "status":
                        row.update(worker_running=running, worker_pid=42 if running else None,
                                   worker_operation="install" if running else None,
                                   worker_request_id=request_id if running else None)
                    elif receipt is None:
                        row.update(ok=False, error="log_unavailable")
                    else:
                        row.update(truncated=False, text=json.dumps(receipt))
                    return b"TOMACCO_APP_CONTROL " + json.dumps(row).encode() + b"\n"
                peer.reply = reply
                result = dispatch("ios.emulator.guest_result", {
                    "job_id": self.job["id"], "request_id": request_id})
                self.assertEqual(result["state"], state)
                self.assertEqual(result["outcome_known"], state == "completed")
                self.assertEqual(result["ready_for_next_request"], state == "completed" and not running)
                self.assertFalse(result["mutation_retry_safe"])
                self.assertEqual(result["response"], receipt if receipt and
                                 receipt["request_id"] == request_id else None)
                self.assertEqual(result["receipt_error"] is not None, receipt is None)
        self.assertEqual(peer.requests, [{"operation": "status"},
                                         {"operation": "logs", "argument": "operation"}] * len(cases))

    def test_result_refuses_malformed_receipts_and_invalid_request_identity(self):
        peer = self.guest()
        request_id = "a" * 32
        arguments = {"job_id": self.job["id"], "request_id": request_id}
        receipt = {"format": 1, "operation": "install", "ok": True, "request_id": request_id}
        for invalid in ("", "a" * 31, "A" * 32, "../result", 42):
            with self.subTest(identity=invalid), self.assertRaises(StudioError):
                dispatch("ios.emulator.guest_result", {**arguments, "request_id": invalid})
        self.assertEqual(peer.requests, [])
        for extra in ({"text": "{"}, {"truncated": True},
                      {"text": json.dumps({**receipt, "operation": "status"})},
                      {"text": json.dumps({**receipt, "ok": "true"})},
                      {"text": json.dumps({**receipt, "request_id": "invalid"})}):
            def reply(request):
                row = {"format": 1, "operation": request["operation"], "ok": True}
                if request["operation"] == "status":
                    row.update(worker_running=False)
                else:
                    row.update(text=json.dumps(receipt), truncated=False)
                    row.update(extra)
                return b"TOMACCO_APP_CONTROL " + json.dumps(row).encode() + b"\n"
            peer.reply = reply
            with self.subTest(receipt=extra), self.assertRaises(StudioError) as error:
                dispatch("ios.emulator.guest_result", arguments)
            self.assertEqual(error.exception.code, "invalid_guest_receipt")

    def test_install_progress_is_correlated_and_cannot_claim_completion(self):
        peer = self.guest()
        request_id = "a" * 32
        progress = {"format": 1, "operation": "install", "request_id": request_id,
                    "sequence": 8, "phase": "installer", "native_status": "Complete",
                    "percent_complete": 100}
        cases = [
            (True, progress, None, True, False),
            (True, {**progress, "phase": "installer-connect"}, None, True, False),
            (False, progress, None, True, False),
            (True, {**progress, "request_id": "b" * 32}, None, False, False),
            (True, {**progress, "percent_complete": 101}, None, False, True),
            (True, {**progress, "percent_complete": True}, None, False, True),
            (True, {**progress, "sequence": 0}, None, False, True),
            (True, {**progress, "phase": "success"}, None, False, True),
            (True, {**progress, "native_status": "📱" * 65}, None, False, True),
            (True, {**progress, "request_id": "unknown"}, None, False, True),
            (False, {**progress, "sequence": "8"}, True, False, True),
            (True, progress, False, True, False),
        ]
        for running, observed, accepted, has_progress, invalid in cases:
            with self.subTest(running=running, progress=observed, accepted=accepted):
                def reply(request):
                    row = {"format": 1, "operation": request["operation"], "ok": True}
                    if request["operation"] == "status":
                        row.update(worker_running=running, worker_request_id=request_id,
                                   installation_progress_supported=True, installation_progress=observed)
                    elif accepted is None:
                        row.update(ok=False, error="log_unavailable")
                    else:
                        row.update(truncated=False, text=json.dumps({"format": 1,
                            "operation": "install", "request_id": request_id, "ok": accepted}))
                    return b"TOMACCO_APP_CONTROL " + json.dumps(row).encode() + b"\n"
                peer.reply = reply
                result = dispatch("ios.emulator.guest_result", {
                    "job_id": self.job["id"], "request_id": request_id})
                self.assertEqual(result["state"], "completed" if accepted is not None else
                                 "running" if running else "unknown")
                self.assertEqual(result["outcome_known"], accepted is not None)
                self.assertFalse(result["mutation_retry_safe"])
                self.assertEqual(result["progress"] is not None, has_progress)
                self.assertEqual(result["progress_error"] is not None, invalid)
                self.assertTrue(result["progress_supported"])
                if has_progress:
                    self.assertEqual(result["progress"]["percent_complete"], 100)
                    self.assertEqual(result["progress"]["native_status"], "Complete")
                    self.assertNotIn("ok", result["progress"])
                if accepted is not None:
                    self.assertEqual(result["response"]["ok"], accepted)
        self.assertEqual(peer.requests, [{"operation": "status"},
                                         {"operation": "logs", "argument": "operation"}] * len(cases))

    def test_result_status_and_receipt_share_one_deadline(self):
        peer = self.guest()
        request_id = "a" * 32
        def reply(request):
            row = {"format": 1, "operation": request["operation"], "ok": True}
            if request["operation"] == "status":
                row.update(worker_running=True, worker_request_id=request_id)
            else:
                row.update(ok=False, error="log_unavailable")
            return b"TOMACCO_APP_CONTROL " + json.dumps(row).encode() + b"\n"
        peer.reply = reply
        peer.fragment, peer.delay = 50, .24
        started = time.monotonic()
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.guest_result", {
                "job_id": self.job["id"], "request_id": request_id, "timeout_seconds": 1})
        self.assertEqual(error.exception.code, "ios_guest_timeout")
        self.assertLess(time.monotonic() - started, 1.8)
        self.assertEqual(peer.requests, [{"operation": "status"},
                                         {"operation": "logs", "argument": "operation"}])

    def test_trickled_bytes_cannot_extend_the_absolute_deadline(self):
        peer = self.guest()
        peer.fragment, peer.delay = 1, .04
        peer.reply = lambda _: b"TOMACCO_APP_CONTROL " + b" " * 300
        start = time.monotonic()
        with self.assertRaises(StudioError) as error:
            self.call(timeout_seconds=1)
        self.assertEqual(error.exception.code, "ios_guest_timeout")
        self.assertLess(time.monotonic() - start, 2.5)
        self.assertTrue(error.exception.details["automatic_retry_safe"])

    def test_malformed_oversized_and_wrong_operation_never_qualify(self):
        peer = self.guest()
        for reply in (b"TOMACCO_APP_CONTROL \n",
                      b'TOMACCO_APP_CONTROL {"format":1,"operation":"open","ok":true}\n',
                      b'TOMACCO_APP_CONTROL {"format":1,"operation":"inspect","ok":"true"}\n',
                      b"TOMACCO_APP_CONTROL " + b" " * (1024 * 1024 + 129)):
            with self.subTest(size=len(reply)):
                peer.reply = lambda _, reply=reply: reply
                with self.assertRaises(StudioError) as error:
                    self.call()
                self.assertEqual(error.exception.code, "invalid_guest_response")

    def test_lock_path_and_stopped_job_are_checked_before_sending(self):
        peer = self.guest()
        lock = self.root / "state/locks" / ("ios-guest-request-" + self.job["id"] + ".lock")
        with lock.open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with self.assertRaises(StudioError) as error:
                self.call()
            self.assertEqual(error.exception.code, "ios_guest_busy")
        parent = Path(self.request["guest_control"]["socket"]).parent
        parent.chmod(0o755)
        try:
            with self.assertRaises(StudioError) as error:
                self.call()
            self.assertEqual(error.exception.code, "invalid_control_socket")
        finally:
            parent.chmod(0o700)
        self.assertEqual(peer.requests, [])
        backend_pid = int((self.root / "backend.pid").read_text())
        jobs.cancel(self.job["id"])
        self.assertEqual(jobs.wait(self.job["id"], 8)["state"], "cancelled")
        self.assertFalse(Path(f"/proc/{backend_pid}").exists(), "transport backend must be reaped")
        with self.assertRaises(StudioError) as error:
            self.call()
        self.assertEqual(error.exception.code, "ios_guest_stopped")
