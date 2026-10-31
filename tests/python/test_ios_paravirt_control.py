"""Compiled agent/QMP ownership and input tests; protocol fixtures are not iOS evidence."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import threading
import time
import zlib

from test_product import Isolated
import test_ios_paravirt_job as guest_fixture
from native_client import StudioError, binary, dispatch, jobs
from host_patience import PATIENCE
from unittest.mock import patch


def png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes([0, 255, 0, 0, 255, 0, 0, 255, 255]))) + chunk(b"IEND", b""))


class Monitor:
    def __init__(self, path):
        self.path = path
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(path))
        self.listener.listen(4)
        self.listener.settimeout(0.1)
        self.commands = []
        self.running = True
        self.failed_motion = False
        self.reject_motion = False
        self.reject_touch = False
        self.touch_downs = 0
        self.stopped = threading.Event()
        self.errors = []
        self.capture = png()
        self.frame = b""
        self.jit = ("Translation buffer state:\r\ngen code size       1900000000/2147483648\r\n"
                    "TB count            3500000\r\nTB flush count      8\r\n"
                    "TB invalidate count 480\r\nHost - Guest clock  -15 ms\r\n")
        self.jit_error = None
        self.on_quit = None
        self.quit_error = None
        self.dump_error = None
        self.cpus = [{"cpu-index": 0, "thread-id": 42, "target": "aarch64"}]
        self.block_statistics = [{"node-name": "tomacco-root", "stats": {
            "rd_bytes": 5000000000, "rd_operations": 1234, "rd_total_time_ns": 7654321}}]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        try:
            while not self.stopped.is_set():
                try:
                    peer, _ = self.listener.accept()
                except socket.timeout:
                    continue
                with peer, peer.makefile("rwb") as stream:
                    # A stop keeps its connection open while it waits for the guest.
                    peer.settimeout(30)
                    stream.write(b'{"QMP":{}}\n')
                    stream.flush()
                    for line in stream:
                        request = json.loads(line)
                        self.commands.append(request)
                        command = request["execute"]
                        arguments = request["arguments"]
                        response = {"return": {}, "id": request["id"]}
                        if command == "query-status":
                            response["return"] = {"running": self.running, "status": "running" if self.running else "paused"}
                        elif command == "query-mice":
                            response["return"] = [{"name": "protocol fixture", "absolute": True, "current": True, "index": 0}]
                        elif command == "query-cpus-fast":
                            response["return"] = self.cpus
                        elif command == "query-blockstats":
                            response["return"] = self.block_statistics
                        elif command == "human-monitor-command":
                            assert arguments == {"command-line": "info jit"}
                            if self.jit_error:
                                response = {"error": self.jit_error, "id": request["id"]}
                            else:
                                response["return"] = self.jit
                        elif command == "qom-get":
                            response["return"] = 1 if arguments["property"] == "configured" else 0
                        elif command == "qom-set" and arguments["property"] == "touch-state":
                            if arguments["value"] & (1 << 32):
                                self.touch_downs += 1
                                if self.reject_touch and self.touch_downs == 2:
                                    self.failed_motion = True
                                    response = {"error": {"class": "GenericError", "desc": "Touch queue full"}, "id": request["id"]}
                        elif command == "screendump":
                            assert arguments["format"] in ("png", "ppm")
                            Path(arguments["filename"]).write_bytes(
                                self.capture if arguments["format"] == "png" else self.frame)
                            if self.dump_error:
                                # The command fails after part of the picture is written.
                                Path(arguments["filename"]).write_bytes(self.frame[:7])
                                response = {"error": self.dump_error, "id": request["id"]}
                        elif command == "quit":
                            if self.on_quit:
                                self.on_quit()
                            if self.quit_error:
                                response = {"error": self.quit_error, "id": request["id"]}
                        elif (command == "input-send-event" and self.reject_motion and not self.failed_motion
                              and all(e["type"] == "abs" for e in arguments["events"])):
                            self.failed_motion = True
                            response = {"error": {"class": "GenericError", "desc": "Motion refused"}, "id": request["id"]}
                        # Unsolicited monitor events must not become command replies.
                        stream.write(b'{"event":"TEST_EVENT"}\n' + json.dumps(response).encode() + b"\n")
                        stream.flush()
        except BaseException as error:
            if not self.stopped.is_set():
                self.errors.append(error)

    def close(self):
        self.stopped.set()
        self.thread.join(3)
        self.listener.close()
        self.path.unlink(missing_ok=True)
        assert not self.thread.is_alive()
        assert not self.errors, self.errors


class ParavirtualControlTests(Isolated):
    def guest(self, interactive=True, input_transport="virtio", **launch):
        arguments = guest_fixture.ParavirtualJobTests.arguments(self, wait=True)
        self.job = dispatch("ios.emulator.paravirtual", {**arguments, "interactive": interactive,
                                                       "input_transport": input_transport, "seconds": 30,
                                                       **launch})
        self.output = Path(self.job["report_directory"]) / "paravirtual"
        # The worker is a separate process; a loaded host can take seconds to start it.
        deadline = time.monotonic() + 30
        while not (self.output / "request.json").exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.request = json.loads((self.output / "request.json").read_text())
        return self.job

    def monitor(self):
        monitor = Monitor(Path(self.request["control"]["socket"]))
        self.addCleanup(monitor.close)
        return monitor

    def call(self, **arguments):
        return dispatch("ios.emulator.control", {"job_id": self.job["id"], **arguments})

    def test_launch_exposes_separate_owned_monitor_and_reports_not_ready(self):
        job = self.guest()
        self.assertIn("--interactive", job["plan"]["steps"][0]["argv"])
        command = self.request["command"]
        self.assertEqual(command.count("-qmp"), 2)
        self.assertIn("virtio-keyboard-pci,id=tomacco-keyboard,disable-legacy=on", command)
        self.assertIn("virtio-tablet-pci,id=tomacco-pointer,disable-legacy=on", command)
        self.assertFalse(self.request["control"]["guest_interaction_qualified"])
        with self.assertRaises(StudioError) as error:
            self.call()
        self.assertEqual(error.exception.code, "ios_control_starting")

    def test_status_key_click_and_drag_reach_the_owned_monitor(self):
        self.guest()
        monitor = self.monitor()
        self.assertTrue(self.call()["result"]["state"]["running"])
        self.call(action="key", keys=["meta_l", "h"], hold_ms=40)
        key = next(r for r in monitor.commands if r["execute"] == "send-key")
        self.assertEqual(key["arguments"], {"keys": [{"type": "qcode", "data": "meta_l"}, {"type": "qcode", "data": "h"}], "hold-time": 40})
        result = self.call(action="click", x=0.5, y=1, duration_ms=1)
        self.assertTrue(result["result"]["accepted_by_emulator"])
        self.assertFalse(result["result"]["guest_response_verified"])
        inputs = [r["arguments"]["events"] for r in monitor.commands if r["execute"] == "input-send-event"]
        self.assertEqual(inputs[0][0]["data"]["value"], 16384)
        self.assertEqual(inputs[0][1]["data"]["value"], 32767)
        self.assertTrue(inputs[0][-1]["data"]["down"])
        self.assertFalse(inputs[-1][-1]["data"]["down"])
        self.call(action="drag", x=1, y=0, to_x=0, to_y=1, duration_ms=1)
        end = monitor.commands[-1]["arguments"]["events"]
        self.assertEqual([end[0]["data"]["value"], end[1]["data"]["value"]], [0, 32767])
        self.assertFalse(end[-1]["data"]["down"])

    def test_failed_motion_releases_the_held_button(self):
        self.guest()
        monitor = self.monitor()
        monitor.reject_motion = True
        with self.assertRaises(StudioError) as error:
            self.call(action="drag", x=0, y=0, to_x=1, to_y=1, duration_ms=1)
        self.assertEqual(error.exception.code, "qmp_error")
        self.assertTrue(monitor.failed_motion)
        self.assertFalse(monitor.commands[-1]["arguments"]["events"][-1]["data"]["down"])

    def test_stop_reports_an_unconfirmed_exit_and_never_claims_finalization(self):
        self.guest()
        monitor = self.monitor()
        monitor.quit_error = {"class": "GenericError", "desc": "exit not confirmed"}
        with self.assertRaises(StudioError) as result:
            self.call(action="stop")
        self.assertEqual(result.exception.code, "qmp_error")
        details = result.exception.details
        self.assertTrue(details["request_may_have_executed"])
        self.assertFalse(details["automatic_retry_safe"])
        self.assertEqual(details["recovery"], {"operation": "ios.emulator.report",
                                              "arguments": {"job_id": self.job["id"]}})
        # The record was written before the exit was asked for; an exit that
        # did not happen must not leave it standing as a claim.
        refused = dispatch("ios.emulator.report", {"job_id": self.job["id"]})["stop"]
        self.assertEqual((refused["guest_storage_flushed"], refused["quit_acknowledged"], refused["error"]),
                         (False, False, "qmp_error"))
        # This fixture explicitly rejected exit. A real lost reply is recovered
        # through the final job report, never by an automatic second stop.
        monitor.quit_error = None
        result = self.call(action="stop")["result"]
        self.assertTrue(result["emulator_exit_requested"])
        self.assertFalse(result["native_worker_idle_verified"])
        self.assertFalse(result["guest_shutdown_verified"])
        self.assertFalse(result["storage_hashes_verified"])
        self.assertEqual(result["recovery"], details["recovery"])
        self.assertEqual([r["execute"] for r in monitor.commands],
                         ["qmp_capabilities", "quit", "qmp_capabilities", "quit"])
        # The guest keeps its own record of how it was asked to stop; the
        # report hands it to whoever decides about reusing the disks.
        self.assertTrue(result["stop_recorded"])
        record = dispatch("ios.emulator.report", {"job_id": self.job["id"]})["stop"]
        self.assertEqual((record["format"], record["native_worker_idle_verified"], record["guest_storage_flushed"]),
                         (1, False, False))
        self.assertGreater(record["requested_at"], time.time() - 60)

    def test_report_has_no_stop_record_for_a_guest_that_was_not_stopped(self):
        self.guest()
        self.assertIsNone(dispatch("ios.emulator.report", {"job_id": self.job["id"]})["stop"])
        # A planted link is not a record.
        foreign = self.root / "foreign-stop.json"
        foreign.write_text('{"guest_storage_flushed": true}')
        (self.output / "stop.json").symlink_to(foreign)
        self.assertIsNone(dispatch("ios.emulator.report", {"job_id": self.job["id"]})["stop"])

    def test_stop_requires_native_idle_and_holds_the_framework_reservation(self):
        from test_ios_guest_control import GuestControlTests
        peer = GuestControlTests.guest(self, interactive=True)
        monitor = self.monitor()
        lock = self.root / "state/locks" / f'ios-guest-request-{self.job["id"]}.lock'
        with lock.open("a+b") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(StudioError) as error:
                self.call(action="stop")
            self.assertEqual(error.exception.code, "ios_guest_busy")
        self.assertEqual(peer.requests, [])
        for worker in (True, None, 0):
            peer.reply = lambda _, value=worker: b"TOMACCO_APP_CONTROL " + json.dumps({
                "format": 1, "operation": "status", "ok": True,
                "worker_running": value}).encode() + b"\n"
            with self.assertRaises(StudioError) as error:
                self.call(action="stop")
            self.assertEqual(error.exception.code, "ios_guest_not_idle")
        self.assertNotIn("quit", [r["execute"] for r in monitor.commands])
        observed = []
        def still_reserved():
            with lock.open("a+b") as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observed.append(True)
        def idle(request):
            self.assertEqual(request, {"operation": "status"})
            still_reserved()
            return b'TOMACCO_APP_CONTROL {"format":1,"operation":"status","ok":true,"worker_running":false}\n'
        peer.reply = idle
        monitor.on_quit = still_reserved
        result = self.call(action="stop")["result"]
        self.assertTrue(result["native_worker_idle_verified"])
        self.assertEqual(len(observed), 2)
        self.assertEqual([r["execute"] for r in monitor.commands].count("quit"), 1)
        with lock.open("a+b") as released:
            fcntl.flock(released, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def flushing_guest(self, flushed):
        from test_ios_guest_control import GuestControlTests
        peer = GuestControlTests.guest(self, interactive=True)
        monitor = self.monitor()
        def reply(request):
            row = {"format": 1, "operation": request["operation"], "ok": True}
            if request["operation"] == "status":
                row.update(worker_running=False, storage_flush_supported=True)
            else:
                # The flush is asked for, and answered, before the emulator is told to exit.
                self.assertNotIn("quit", [r["execute"] for r in monitor.commands])
                if flushed:
                    row.update(storage_flushed=True)
                else:
                    row.update(ok=False, error="storage_flush_failed", storage_flushed=False)
            return b"TOMACCO_APP_CONTROL " + json.dumps(row).encode() + b"\n"
        peer.reply = reply
        result = self.call(action="stop")["result"]
        # A flush that is not confirmed is asked for once more before the stop proceeds.
        self.assertEqual([row["operation"] for row in peer.requests],
                         ["status", "flush"] if flushed else ["status", "flush", "flush"])
        self.assertTrue(result["native_worker_idle_verified"])
        self.assertEqual([r["execute"] for r in monitor.commands].count("quit"), 1)
        return result, dispatch("ios.emulator.report", {"job_id": self.job["id"]})["stop"]

    def test_stop_flushes_guest_storage_first_and_records_the_confirmation(self):
        result, record = self.flushing_guest(True)
        self.assertTrue(result["guest_storage_flushed"])
        self.assertEqual((record["native_worker_idle_verified"], record["guest_storage_flushed"]), (True, True))

    def test_stop_observes_the_same_readonly_worker_until_native_idle(self):
        peer, monitor = self.idle_guest()
        idle = peer.reply
        request_id = "0123456789abcdef0123456789abcdef"
        status_count = 0
        lock = self.root / "state/locks" / f'ios-guest-request-{self.job["id"]}.lock'
        def reply(request):
            nonlocal status_count
            with lock.open("a+b") as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if request["operation"] == "status":
                status_count += 1
                if status_count < 3:
                    return b"TOMACCO_APP_CONTROL " + json.dumps({
                        "format": 1, "operation": "status", "ok": True,
                        "worker_running": True, "worker_pid": 150,
                        "worker_operation": "surface_state",
                        "worker_request_id": request_id,
                        "storage_flush_supported": True}).encode() + b"\n"
            return idle(request)
        peer.reply = reply
        result = self.call(action="stop")["result"]
        self.assertEqual([row["operation"] for row in peer.requests],
                         ["status", "status", "status", "flush"])
        self.assertTrue(result["native_worker_idle_verified"])
        self.assertTrue(result["guest_storage_flushed"])
        self.assertEqual([r["execute"] for r in monitor.commands].count("quit"), 1)

    def test_stop_refuses_a_changed_worker_identity_without_flushing(self):
        peer, monitor = self.idle_guest()
        status_count = 0
        def reply(request):
            nonlocal status_count
            self.assertEqual(request["operation"], "status")
            status_count += 1
            return b"TOMACCO_APP_CONTROL " + json.dumps({
                "format": 1, "operation": "status", "ok": True,
                "worker_running": True, "worker_pid": 150 + status_count,
                "worker_operation": "surface_state",
                "worker_request_id": "0123456789abcdef0123456789abcdef"}).encode() + b"\n"
        peer.reply = reply
        with self.assertRaises(StudioError) as error:
            self.call(action="stop")
        self.assertEqual(error.exception.code, "ios_guest_not_idle")
        self.assertEqual([row["operation"] for row in peer.requests], ["status", "status"])
        self.assertNotIn("quit", [r["execute"] for r in monitor.commands])
        self.assertIsNone(self.stop_record())

    def test_stop_does_not_wait_for_or_replay_an_identified_mutation(self):
        peer, monitor = self.idle_guest()
        peer.reply = lambda request: b"TOMACCO_APP_CONTROL " + json.dumps({
            "format": 1, "operation": "status", "ok": True,
            "worker_running": True, "worker_pid": 150,
            "worker_operation": "install",
            "worker_request_id": "0123456789abcdef0123456789abcdef"}).encode() + b"\n"
        with self.assertRaises(StudioError) as error:
            self.call(action="stop")
        self.assertEqual(error.exception.code, "ios_guest_not_idle")
        self.assertEqual([row["operation"] for row in peer.requests], ["status"])
        self.assertNotIn("quit", [r["execute"] for r in monitor.commands])
        self.assertIsNone(self.stop_record())

    def test_stop_status_rechecks_share_the_original_deadline(self):
        peer, monitor = self.idle_guest()
        peer.reply = lambda request: b"TOMACCO_APP_CONTROL " + json.dumps({
            "format": 1, "operation": "status", "ok": True,
            "worker_running": True, "worker_pid": 150,
            "worker_operation": "surface_state",
            "worker_request_id": "0123456789abcdef0123456789abcdef"}).encode() + b"\n"
        started = time.monotonic()
        with self.assertRaises(StudioError) as error:
            self.call(action="stop")
        elapsed = time.monotonic() - started
        self.assertEqual(error.exception.code, "ios_guest_not_idle")
        self.assertGreaterEqual(elapsed, 14)
        self.assertLess(elapsed, 20)
        self.assertGreater(len(peer.requests), 1)
        self.assertLess(len(peer.requests), 100)
        self.assertTrue(all(row["operation"] == "status" for row in peer.requests))
        self.assertNotIn("quit", [r["execute"] for r in monitor.commands])
        self.assertIsNone(self.stop_record())

    def test_stop_proceeds_after_a_refused_flush_and_records_it_as_unconfirmed(self):
        result, record = self.flushing_guest(False)
        self.assertFalse(result["guest_storage_flushed"])
        self.assertEqual((record["native_worker_idle_verified"], record["guest_storage_flushed"]), (True, False))

    def idle_guest(self):
        """A guest control peer that is idle and confirms every flush, and the emulator monitor."""
        from test_ios_guest_control import GuestControlTests
        peer = GuestControlTests.guest(self, interactive=True)
        self.output = Path(self.job["report_directory"]) / "paravirtual"
        monitor = self.monitor()
        def reply(request):
            row = {"format": 1, "operation": request["operation"], "ok": True}
            if request["operation"] == "status":
                row.update(worker_running=False, storage_flush_supported=True)
            else:
                row.update(storage_flushed=True)
            return b"TOMACCO_APP_CONTROL " + json.dumps(row).encode() + b"\n"
        peer.reply = reply
        return peer, monitor

    def stop_record(self):
        return dispatch("ios.emulator.report", {"job_id": self.job["id"]})["stop"]

    def test_a_quit_the_emulator_refuses_takes_back_the_flush_confirmation(self):
        peer, monitor = self.idle_guest()
        monitor.quit_error = {"class": "GenericError", "desc": "exit not confirmed"}
        with self.assertRaises(StudioError) as refused:
            self.call(action="stop")
        self.assertEqual(refused.exception.code, "qmp_error")
        self.assertTrue(refused.exception.details["request_may_have_executed"])
        self.assertEqual([row["operation"] for row in peer.requests], ["status", "flush"])
        # The flush was confirmed, but the emulator did not exit: the guest keeps
        # running and writing. If it later ends without another stop, nothing
        # may vouch for its disks.
        record = self.stop_record()
        self.assertEqual((record["guest_storage_flushed"], record["quit_acknowledged"], record["error"]),
                         (False, False, "qmp_error"))
        self.assertTrue(record["native_worker_idle_verified"])
        # A stop that goes through records its confirmation as before.
        monitor.quit_error = None
        self.assertTrue(self.call(action="stop")["result"]["guest_storage_flushed"])
        record = self.stop_record()
        self.assertEqual((record["guest_storage_flushed"], record["native_worker_idle_verified"]), (True, True))
        self.assertNotIn("error", record)
        self.assertEqual([r["execute"] for r in monitor.commands].count("quit"), 2)

    def test_a_stop_the_client_cancelled_neither_records_nor_quits(self):
        peer, monitor = self.idle_guest()
        flushing, release = threading.Event(), threading.Event()
        idle = peer.reply
        def reply(request):
            if request["operation"] == "flush":
                # The guest is inside sync() while the client gives up.
                flushing.set()
                release.wait(10)
            return idle(request)
        peer.reply = reply
        def send(server, message):
            server.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
            server.stdin.flush()
        server = subprocess.Popen([binary(), "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        try:
            send(server, {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
            send(server, {"id": 2, "method": "tools/call", "params": {
                "name": "ios.emulator.control", "arguments": {"job_id": self.job["id"], "action": "stop"}}})
            self.assertTrue(flushing.wait(10), "the stop never asked the guest to flush")
            send(server, {"method": "notifications/cancelled", "params": {"requestId": 2}})
            # The server ends when its input does, after the request it was running.
            server.communicate(timeout=60)
        finally:
            release.set()
            if server.poll() is None:
                server.kill()
                server.communicate()
        # A cancelled stop asked for exactly one flush, did not count it as a
        # flush that failed, and did not carry on to leave a record or to exit.
        self.assertEqual([row["operation"] for row in peer.requests], ["status", "flush"])
        self.assertNotIn("quit", [r["execute"] for r in monitor.commands])
        self.assertFalse((self.output / "stop.json").exists())
        self.assertIsNone(self.stop_record())
        # The reservation is gone with the request.
        with (self.root / "state/locks" / f'ios-guest-request-{self.job["id"]}.lock').open("a+b") as lane:
            fcntl.flock(lane, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_a_collision_on_the_service_lane_does_not_refuse_the_stop(self):
        peer, monitor = self.idle_guest()
        lock = self.root / "state/locks" / f'ios-guest-service-{self.job["id"]}.lock'
        with lock.open("a+b") as held:
            # Another client's status, log or receipt read holds the service
            # lane for a moment. The stop already owns the framework lane, so
            # this is a collision and not a guest that is busy.
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            released = threading.Timer(1.0, lambda: fcntl.flock(held, fcntl.LOCK_UN))
            released.start()
            started = time.monotonic()
            result = self.call(action="stop")["result"]
            released.join()
        self.assertGreaterEqual(time.monotonic() - started, 0.9)
        self.assertTrue(result["guest_storage_flushed"])
        self.assertEqual([row["operation"] for row in peer.requests], ["status", "flush"])
        self.assertEqual([r["execute"] for r in monitor.commands].count("quit"), 1)

    def test_a_service_lane_that_stays_taken_refuses_the_stop_after_a_bounded_wait(self):
        peer, monitor = self.idle_guest()
        lock = self.root / "state/locks" / f'ios-guest-service-{self.job["id"]}.lock'
        with lock.open("a+b") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            started = time.monotonic()
            with self.assertRaises(StudioError) as error:
                self.call(action="stop")
            waited = time.monotonic() - started
        self.assertEqual(error.exception.code, "ios_guest_busy")
        self.assertTrue(9.5 <= waited < 20, waited)
        # Nothing was asked of the guest, recorded or ended, and nothing stays reserved.
        self.assertEqual(peer.requests, [])
        self.assertNotIn("quit", [r["execute"] for r in monitor.commands])
        self.assertFalse((self.output / "stop.json").exists())
        self.assertTrue(self.call(action="stop")["result"]["guest_storage_flushed"])

    def test_an_emulator_that_has_exited_is_not_reported_as_one_that_is_starting(self):
        # The launcher has published its request, but no monitor yet.
        self.guest()
        with self.assertRaises(StudioError) as error:
            self.call()
        self.assertEqual(error.exception.code, "ios_control_starting")
        # The emulator removes its socket when it exits, while the job still
        # hashes the media; the launcher has published its result by then.
        (self.output / "result.json").write_text("{}")
        for action in ("status", "stop", "frame"):
            with self.assertRaises(StudioError) as error:
                self.call(action=action)
            self.assertEqual(error.exception.code, "ios_control_ended", action)
        (self.output / "result.json").unlink()
        # A stop leaves its record before the emulator exits, and the result
        # follows only after the launcher has noticed.
        (self.output / "stop.json").write_text("{}")
        with self.assertRaises(StudioError) as error:
            self.call()
        self.assertEqual(error.exception.code, "ios_control_ended")
        # A link is no record.
        (self.output / "stop.json").unlink()
        (self.output / "stop.json").symlink_to(self.root / "foreign-record.json")
        with self.assertRaises(StudioError) as error:
            self.call()
        self.assertEqual(error.exception.code, "ios_control_starting")
        # A job that has ended is rejected as before.
        jobs.cancel(self.job["id"])
        jobs.wait(self.job["id"], 8)
        with self.assertRaises(StudioError) as error:
            self.call()
        self.assertEqual(error.exception.code, "ios_guest_stopped")

    def test_metrics_preserve_native_counters_and_never_change_guest_state(self):
        self.guest()
        monitor = self.monitor()
        result = self.call(action="metrics")["result"]
        self.assertEqual(result["cpus"], monitor.cpus)
        self.assertEqual(result["block_statistics"], monitor.block_statistics)
        self.assertLessEqual(result["sample_started_at"], result["sample_finished_at"])
        self.assertEqual(result["tcg"], {
            "available": True, "code_bytes": 1900000000, "capacity_bytes": 2147483648,
            "translation_blocks": 3500000, "flushes": 8, "invalidations": 480,
            "host_minus_guest_ms": -15, "raw_report": monitor.jit})
        monitor.running = False
        monitor.jit = ("gen code size 100/50\nTB count 4\nTB count 8\n"
                       "TB flush count -1\nTB invalidate count 18446744073709551616\n"
                       "Host - Guest clock unknown ms\n")
        partial = self.call(action="metrics")["result"]
        self.assertFalse(partial["state"]["running"])
        self.assertFalse(partial["tcg"]["available"])
        for name in ("code_bytes", "capacity_bytes", "translation_blocks", "flushes",
                     "invalidations", "host_minus_guest_ms"):
            self.assertIsNone(partial["tcg"][name], name)
        self.assertEqual(partial["tcg"]["raw_report"], monitor.jit)
        self.assertEqual([r["execute"] for r in monitor.commands],
                         ["qmp_capabilities", "query-status", "query-cpus-fast",
                          "query-blockstats", "human-monitor-command"] * 2)

    def test_unavailable_translation_metrics_keep_actual_error_and_disk_counters(self):
        self.guest()
        monitor = self.monitor()
        monitor.jit_error = {"class": "CommandNotFound", "desc": "Monitor disabled"}
        result = self.call(action="metrics")["result"]
        self.assertEqual(result["block_statistics"], monitor.block_statistics)
        self.assertFalse(result["tcg"]["available"])
        self.assertEqual(result["tcg"]["error"]["code"], "qmp_error")
        self.assertEqual(json.loads(result["tcg"]["error"]["message"]), monitor.jit_error)

    def test_usb_home_and_touch_use_guest_reports_and_no_mouse_events(self):
        self.guest(input_transport="usb-hid")
        self.assertIn("qemu-xhci,id=tomacco-usb-input,p2=2,p3=0,ports-non-removable=on",
                      self.request["command"])
        self.assertIn("tomacco-ios-hid,id=tomacco-hid,bus=tomacco-usb-input.0,port=1", self.request["command"])
        self.assertFalse(any("virtio-tablet" in arg for arg in self.request["command"]))
        self.assertEqual(self.request["control"]["pointer_kind"], "single-contact-touch")
        monitor = self.monitor()
        status = self.call()["result"]
        self.assertEqual(status["input_transport"], "usb-hid")
        self.assertEqual(status["usb_hid"]["configured"], 1)
        self.assertEqual(status["usb_hid"]["home-reports-delivered"], 0)
        self.call(action="home", hold_ms=70)
        self.assertEqual(monitor.commands[-1]["arguments"], {
            "path": "/machine/peripheral/tomacco-hid", "property": "home-press-ms", "value": 70})
        self.call(action="drag", x=0, y=0.5, to_x=1, to_y=1, duration_ms=1)
        values = [r["arguments"]["value"] for r in monitor.commands if r["execute"] == "qom-set"
                  and r["arguments"]["property"] == "touch-state"]
        self.assertEqual(values, [(16384 << 16) | (1 << 32),
                                  32767 | (32767 << 16) | (1 << 32),
                                  32767 | (32767 << 16)])
        with self.assertRaises(StudioError) as error:
            self.call(action="move", x=0, y=0)
        self.assertEqual(error.exception.code, "ios_touch_has_no_hover")
        self.assertFalse(any(r["execute"] == "input-send-event" for r in monitor.commands))

    def test_home_does_not_alias_an_ordinary_keyboard_key(self):
        self.guest()
        monitor = self.monitor()
        with self.assertRaises(StudioError) as error:
            self.call(action="home")
        self.assertEqual(error.exception.code, "ios_home_unavailable")
        self.assertFalse(any(r["execute"] in ("send-key", "qom-set") for r in monitor.commands))

    def test_usb_backpressure_releases_contact_after_failed_motion(self):
        self.guest(input_transport="usb-hid")
        monitor = self.monitor()
        monitor.reject_touch = True
        with self.assertRaises(StudioError) as error:
            self.call(action="drag", x=0, y=0, to_x=1, to_y=1, duration_ms=1)
        self.assertEqual(error.exception.code, "qmp_error")
        self.assertTrue(monitor.failed_motion)
        self.assertEqual(monitor.commands[-1]["arguments"]["property"], "touch-state")
        self.assertFalse(monitor.commands[-1]["arguments"]["value"] & (1 << 32))

    def lane(self):
        """The desktop's persistent control client: tomaccoctl mcp."""
        process = subprocess.Popen([binary(), "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(lambda: (process.kill(), process.wait(), process.stdout.close(),
                                 process.stdin.closed or process.stdin.close()))
        sequence = iter(range(1, 1000))

        def send(method, params):
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": next(sequence), "method": method,
                                            "params": params}) + "\n")
            process.stdin.flush()
            return json.loads(process.stdout.readline())
        send("initialize", {"protocolVersion": "2025-11-25"})

        def touch(**arguments):
            reply = send("tools/call", {"name": "ios.emulator.control",
                                        "arguments": {"job_id": self.job["id"], **arguments}})["result"]
            payload = json.loads(reply["content"][0]["text"])
            if reply["isError"]:
                raise StudioError(payload["error"])
            return payload
        return process, touch

    def touches(self, monitor):
        return [r["arguments"]["value"] for r in monitor.commands if r["execute"] == "qom-set"
                and r["arguments"]["property"] == "touch-state"]

    def test_a_live_touch_reaches_the_guest_as_it_happens_and_holds_the_control_lane(self):
        self.guest(input_transport="usb-hid")
        monitor = self.monitor()
        _, touch = self.lane()
        lock = self.root / "state/locks" / f'ios-control-{self.job["id"]}.lock'
        self.assertEqual(touch(action="touch_down", x=0, y=0.5)["result"]["contact"], "down")
        self.assertEqual(self.touches(monitor), [(16384 << 16) | (1 << 32)])
        # No other input lands inside the gesture.
        with lock.open("a") as stream, self.assertRaises(BlockingIOError):
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        touch(action="touch_move", x=0.5, y=0.5)
        self.assertEqual(self.touches(monitor)[-1], 16384 | (16384 << 16) | (1 << 32))
        # The same point again keeps the contact alive and reports nothing new.
        touch(action="touch_move", x=0.5, y=0.5)
        self.assertEqual(touch(action="touch_up")["result"]["contact"], "up")
        self.assertEqual(self.touches(monitor), [(16384 << 16) | (1 << 32),
                                                 16384 | (16384 << 16) | (1 << 32),
                                                 16384 | (16384 << 16)])
        with lock.open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaises(StudioError) as error:
            touch(action="touch_move", x=0, y=0)
        self.assertEqual(error.exception.code, "ios_touch_not_down")
        self.assertFalse(any(r["execute"] == "input-send-event" for r in monitor.commands))

    def test_no_contact_outlives_its_client_and_a_release_is_taken_from_anyone(self):
        self.guest(input_transport="usb-hid")
        monitor = self.monitor()
        # A one-shot process would end with the finger down.
        with self.assertRaises(StudioError) as error:
            self.call(action="touch_down", x=0.5, y=0.5)
        self.assertEqual(error.exception.code, "ios_touch_lane_required")
        self.assertEqual(self.touches(monitor), [])
        process, touch = self.lane()
        touch(action="touch_down", x=1, y=1)
        process.stdin.close()
        self.assertEqual(process.wait(10 * PATIENCE), 0)
        self.assertEqual(self.touches(monitor), [32767 | (32767 << 16) | (1 << 32), 32767 | (32767 << 16)])
        # What a client that was killed held is lifted by a one-shot release.
        self.assertEqual(self.call(action="touch_up", x=0, y=0)["result"]["contact"], "up")
        self.assertEqual(self.touches(monitor)[-1], 0)

    def test_a_live_touch_on_virtio_moves_the_held_button(self):
        self.guest()
        monitor = self.monitor()
        _, touch = self.lane()
        touch(action="touch_down", x=0, y=0)
        touch(action="touch_move", x=1, y=1)
        touch(action="touch_up", x=1, y=0)
        events = [r["arguments"]["events"] for r in monitor.commands if r["execute"] == "input-send-event"]
        self.assertTrue(events[0][-1]["data"]["down"])
        self.assertEqual(len(events[1]), 2)
        self.assertEqual([e["data"]["value"] for e in events[2]], [32767, 0])
        self.assertFalse(events[-1][-1]["data"]["down"])
        self.assertEqual(len(events), 4)

    def test_usb_requires_an_interactive_job(self):
        arguments = guest_fixture.ParavirtualJobTests.arguments(self, wait=True)
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.paravirtual", {**arguments, "input_transport": "usb-hid"})
        self.assertEqual(error.exception.code, "invalid_interactive_guest")

    def test_live_frame_returns_actual_png_without_accumulating_captures(self):
        import base64
        self.guest()
        monitor = self.monitor()
        for _ in range(3):
            result = self.call(action="frame")["result"]
            self.assertEqual(base64.b64decode(result["data_url"].split(",", 1)[1]), png())
            self.assertEqual((result["width"], result["height"]), (2, 1))
            self.assertEqual(result["sha256"], hashlib.sha256(png()).hexdigest())
            self.assertNotIn("path", result)
            self.assertEqual(list((self.output / "captures").iterdir()), [])
        monitor.capture = b"Not a PNG"
        with self.assertRaises(StudioError) as error:
            self.call(action="frame")
        self.assertEqual(error.exception.code, "invalid_capture")
        self.assertEqual(list((self.output / "captures").iterdir()), [])

    def test_file_frames_are_private_bounded_and_skip_an_unchanged_picture(self):
        self.guest()
        monitor = self.monitor()
        runtime = self.root / "runtime"
        runtime.mkdir(mode=0o700)
        pixels = lambda value: b"P6\n2 1\n255\n" + bytes([value]) * 6
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
            monitor.frame = pixels(1)
            first = self.call(action="frame", transport="file")["result"]
            directory = runtime / "tomacco/ios-frames" / self.job["id"]
            self.assertEqual((first["width"], first["height"], first["bytes"], first["sequence"], first["changed"]),
                             (2, 1, len(pixels(1)), 1, True))
            self.assertEqual(Path(first["path"]).read_bytes(), pixels(1))
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertNotIn("data_url", first)
            # The same picture keeps its file and number.
            again = self.call(action="frame", transport="file")["result"]
            self.assertEqual((again["path"], again["sequence"], again["changed"]), (first["path"], 1, False))
            for value in (2, 3, 4):
                monitor.frame = pixels(value)
                latest = self.call(action="frame", transport="file")["result"]
            self.assertEqual((latest["sequence"], latest["changed"]), (4, True))
            # Only the newest frame and its predecessor remain.
            self.assertEqual(sorted(path.name for path in directory.iterdir()), ["frame-3.ppm", "frame-4.ppm"])
            # Another data directory of this user shares the runtime directory.
            # A job this one does not know may be its running guest: those
            # frames stay until nothing has touched them for ten minutes.
            foreign = directory.parent / ("0" * 32)
            stale = directory.parent / ("1" * 32)
            for other in (foreign, stale):
                other.mkdir()
                (other / "frame-1.ppm").write_bytes(pixels(9))
            os.utime(stale, (time.time() - 3600, time.time() - 3600))
            monitor.frame = pixels(5)
            self.call(action="frame", transport="file")
            self.assertTrue((foreign / "frame-1.ppm").exists())
            self.assertFalse(stale.exists())
            for broken in (b"P6\n2 1\n255\n" + b"\0" * 5, b"P5\n2 1\n255\n" + b"\0" * 6, b"Not a frame"):
                monitor.frame = broken
                with self.assertRaises(StudioError) as error:
                    self.call(action="frame", transport="file")
                self.assertEqual(error.exception.code, "invalid_capture")
            self.assertEqual(len(list(directory.iterdir())), 2)
            # Nothing of this transport ever reaches the job's own directory.
            self.assertFalse((self.output / "captures").exists())
            # Frames of a guest of this data directory that has ended go with the next capture.
            first_job, first_directory = self.job["id"], directory
            jobs.cancel(first_job)
            deadline = time.monotonic() + 20
            while jobs.get(first_job)["state"] not in ("cancelled", "failed", "succeeded", "timed_out"):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.1)
            self.guest()
            second = self.monitor()
            second.frame = pixels(6)
            self.call(action="frame", transport="file")
            self.assertFalse(first_directory.exists())
            self.assertTrue(foreign.exists())
        for unusable in ({"XDG_RUNTIME_DIR": str(self.root / "missing")}, {"XDG_RUNTIME_DIR": "relative"}):
            with patch.dict(os.environ, unusable):
                with self.assertRaises(StudioError) as error:
                    self.call(action="frame", transport="file")
                self.assertEqual(error.exception.code, "ios_frame_file_unavailable")
        shared = self.root / "shared"
        shared.mkdir()
        shared.chmod(0o755)
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(shared)}):
            with self.assertRaises(StudioError) as error:
                self.call(action="frame", transport="file")
            self.assertEqual(error.exception.code, "ios_frame_file_unavailable")

    def test_a_failed_capture_leaves_no_partial_frame_and_keeps_the_picture_in_view(self):
        self.guest()
        monitor = self.monitor()
        runtime = self.root / "runtime"
        runtime.mkdir(mode=0o700)
        pixels = lambda value: b"P6\n2 1\n255\n" + bytes([value]) * 6
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
            directory = runtime / "tomacco/ios-frames" / self.job["id"]
            names = lambda: sorted(path.name for path in directory.iterdir())
            monitor.frame = pixels(1)
            self.assertEqual(self.call(action="frame", transport="file")["result"]["sequence"], 1)
            # A capture that fails after the emulator has written part of the
            # picture leaves nothing behind that could pass for one.
            monitor.dump_error = {"class": "GenericError", "desc": "No space left on device"}
            with self.assertRaises(StudioError) as error:
                self.call(action="frame", transport="file")
            self.assertEqual(error.exception.code, "qmp_error")
            self.assertEqual(names(), ["frame-1.ppm"])
            monitor.dump_error = None
            monitor.frame = pixels(2)
            second = self.call(action="frame", transport="file")["result"]
            self.assertEqual((second["sequence"], second["changed"]), (2, True))
            # The picture a viewer was handed last is still there for it to decode.
            self.assertEqual(names(), ["frame-1.ppm", "frame-2.ppm"])
            # The remains of a capture that was cut off (the process died) are
            # no picture either: they are not the predecessor, and the picture
            # before them is not deleted for their sake.
            (directory / "frame-3.ppm").write_bytes(pixels(3)[:12])
            monitor.frame = pixels(4)
            latest = self.call(action="frame", transport="file")["result"]
            self.assertEqual((latest["sequence"], latest["changed"]), (4, True))
            self.assertEqual(names(), ["frame-2.ppm", "frame-4.ppm"])
            # An unchanged picture is still recognised against the newest complete one.
            again = self.call(action="frame", transport="file")["result"]
            self.assertEqual((again["sequence"], again["changed"]), (4, False))

    def test_a_panel_too_large_for_file_frames_is_refused_before_the_emulator_dumps_it(self):
        # The emulator would write width x height x 3 bytes (192 MiB here)
        # before anything could refuse them.
        self.guest(display_profile="8192x8192@60:200x200")
        monitor = self.monitor()
        self.assertEqual(self.request["display_profile"]["width"], 8192)
        runtime = self.root / "runtime"
        runtime.mkdir(mode=0o700)
        monitor.frame = b"P6\n2 1\n255\n" + b"\0" * 6
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
            with self.assertRaises(StudioError) as error:
                self.call(action="frame", transport="file")
        # The viewer falls back to inline frames on this code.
        self.assertEqual(error.exception.code, "ios_frame_file_unavailable")
        self.assertNotIn("screendump", [r["execute"] for r in monitor.commands])
        self.assertFalse((runtime / "tomacco").exists())
        self.assertEqual(self.call(action="frame")["result"]["width"], 2)  # inline PNG
        self.assertEqual([r["arguments"]["format"] for r in monitor.commands if r["execute"] == "screendump"],
                         ["png"])

    def test_a_panel_within_the_bound_of_file_frames_is_dumped(self):
        self.guest(display_profile="4096x4096@60:140x140")  # 48 MiB of RGB
        monitor = self.monitor()
        runtime = self.root / "runtime"
        runtime.mkdir(mode=0o700)
        monitor.frame = b"P6\n2 1\n255\n" + b"\0" * 6
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
            result = self.call(action="frame", transport="file")["result"]
        self.assertEqual((result["width"], result["height"], result["changed"]), (2, 1, True))
        self.assertEqual([r["arguments"]["format"] for r in monitor.commands if r["execute"] == "screendump"],
                         ["ppm"])

    def test_capture_stays_in_job_and_records_actual_bytes(self):
        self.guest()
        self.monitor()
        result = self.call(action="screenshot")["result"]
        path = Path(result["path"])
        self.assertEqual(path.parent, self.output / "captures")
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.read_bytes(), png())
        self.assertEqual((result["width"], result["height"]), (2, 1))
        self.assertEqual(result["sha256"], hashlib.sha256(png()).hexdigest())

    def test_paused_guest_and_missing_coordinates_do_not_send_input(self):
        self.guest()
        monitor = self.monitor()
        monitor.running = False
        with self.assertRaises(StudioError) as error:
            self.call(action="key", keys=["h"])
        self.assertEqual(error.exception.code, "ios_guest_paused")
        monitor.running = True
        with self.assertRaises(StudioError) as error:
            self.call(action="click", x=0.2)
        self.assertEqual(error.exception.code, "invalid_coordinates")
        self.assertFalse(any(r["execute"] in ("send-key", "input-send-event") for r in monitor.commands))

    def test_simultaneous_control_and_replaced_request_are_rejected(self):
        self.guest()
        monitor = self.monitor()
        lock = self.root / "state/locks" / f'ios-control-{self.job["id"]}.lock'
        with lock.open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            # A short collision, such as a viewer fetching a frame, is waited out.
            released = threading.Timer(0.4, lambda: fcntl.flock(stream, fcntl.LOCK_UN))
            released.start()
            started = time.monotonic()
            self.assertEqual(self.call(action="status")["action"], "status")
            self.assertGreaterEqual(time.monotonic() - started, 0.3)
            released.join()
            # A lane that stays taken is reported, after a bounded wait.
            fcntl.flock(stream, fcntl.LOCK_EX)
            started = time.monotonic()
            with self.assertRaises(StudioError) as error:
                self.call()
            self.assertEqual(error.exception.code, "ios_control_busy")
            self.assertTrue(2.4 <= time.monotonic() - started < 6)
        self.assertEqual([r["execute"] for r in monitor.commands].count("query-status"), 1)
        request = self.output / "request.json"
        foreign = self.root / "foreign-request.json"
        foreign.write_bytes(request.read_bytes())
        request.unlink()
        request.symlink_to(foreign)
        with self.assertRaises(StudioError) as error:
            self.call()
        self.assertEqual(error.exception.code, "invalid_report_path")

    def virtual_profile(self, guest_job):
        directory = self.root / "virtual-profile"
        (directory / "installs").mkdir(parents=True)
        (directory / "profile.json").write_text(json.dumps({
            "format": 1, "prepare": {}, "boot": {}, "graphics": "/graphics", "engine": "/engine.py",
            "application": "app.example", "installs": str(directory / "installs")}))
        verified = {"run": 2, "install": str(self.root / "verified-install"), "tools": str(self.root / "tools")}
        (directory / "state.json").write_text(json.dumps({
            "run": 3, "install": str(self.root / "current-install"), "tools": str(self.root / "tools"),
            "closed": False, "guest_job": guest_job, "phase": "booting", "verified": verified}))
        dispatch("ios.virtual.configure", {"directory": str(directory)})
        return directory, verified

    def test_virtual_stop_cancels_a_running_start_and_ends_an_unanswering_guest(self):
        # A guest whose service never comes up, as during a boot: its control
        # monitor is not even connected.
        guest = self.guest()
        directory, verified = self.virtual_profile(guest["id"])
        start = dispatch("ios.virtual.start", {"open_app": False})
        deadline = time.monotonic() + 20
        while dispatch("ios.virtual.status")["busy"] is None or "TOMACCO_VIRTUAL_IPHONE" not in dispatch(
                "job.logs", {"job_id": start["id"]})["text"] and jobs.get(start["id"])["state"] == "queued":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.1)
        self.assertEqual(dispatch("ios.virtual.status")["busy"]["action"], "ios.virtual.start")
        stop = dispatch("ios.virtual.stop")
        self.assertEqual(jobs.get(start["id"])["state"], "cancelled")
        def finished(job):
            deadline = time.monotonic() + 120
            while job["state"] not in ("succeeded", "failed", "cancelled", "timed_out"):
                self.assertLess(time.monotonic(), deadline)
                job = jobs.wait(job["id"], 30)
            return job
        # iOS never came up, so there was nothing to save: the guest is gone
        # and the phone is simply off again, on its last verified run.
        self.assertEqual(finished(stop)["state"], "succeeded")
        self.assertIn('"forced"', dispatch("job.logs", {"job_id": stop["id"]})["text"])
        self.assertIn(jobs.get(guest["id"])["state"], ("cancelled", "failed"))
        state = json.loads((directory / "state.json").read_text())
        self.assertEqual((state["phase"], state["closed"], state["run"], state["install"], state["guest_job"], state["error"]),
                         ("stopped", True, verified["run"], verified["install"], None, None))
        self.assertEqual(state["verified"], verified)
        status = dispatch("ios.virtual.status")
        self.assertEqual((status["guest_running"], status["busy"], status["phase"]), (False, None, "stopped"))

    def test_virtual_stop_of_a_guest_that_was_up_and_no_longer_answers_reports_the_loss(self):
        guest = self.guest()
        directory, verified = self.virtual_profile(guest["id"])
        state = json.loads((directory / "state.json").read_text())
        (directory / "state.json").write_text(json.dumps({**state, "phase": "ready"}))
        stop = dispatch("ios.virtual.stop")
        deadline = time.monotonic() + 120
        while stop["state"] not in ("succeeded", "failed", "cancelled", "timed_out"):
            self.assertLess(time.monotonic(), deadline)
            stop = jobs.wait(stop["id"], 30)
        # Whatever happened in that guest was not saved, and the stop says so
        # instead of reporting a clean shutdown.
        self.assertEqual(stop["state"], "failed")
        self.assertIn("ended without saving", dispatch("job.logs", {"job_id": stop["id"]})["text"])
        self.assertIn(jobs.get(guest["id"])["state"], ("cancelled", "failed"))
        state = json.loads((directory / "state.json").read_text())
        self.assertEqual((state["phase"], state["closed"], state["verified"]), ("unverified", False, verified))
        self.assertIn("ended without saving", state["error"])
        self.assertFalse(state["outcome"]["guest_storage_flushed"])
        status = dispatch("ios.virtual.status")
        self.assertEqual((status["guest_running"], status["busy"], status["phase"]), (False, None, "unverified"))

    def test_terminal_guest_and_noninteractive_guest_are_rejected(self):
        self.guest(False)
        with self.assertRaises(StudioError) as error:
            self.call()
        self.assertEqual(error.exception.code, "invalid_interactive_guest")
        jobs.cancel(self.job["id"])
        jobs.wait(self.job["id"], 8)
        self.guest()
        jobs.cancel(self.job["id"])
        jobs.wait(self.job["id"], 8)
        with self.assertRaises(StudioError) as error:
            self.call()
        self.assertEqual(error.exception.code, "ios_guest_stopped")
