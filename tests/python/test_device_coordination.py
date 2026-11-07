"""Device reservations and the request ledger, through the compiled CLI, its MCP server
and the runtime worker's control socket, on the offline fixture runtime.

Every call is its own tomaccoctl process, as agents call it, and several run at once
where the contract is about concurrency. The fixture journals each input it accepted
(never text) so a test can count what reached the "device" across processes and
sessions. Every device helper is trapped: nothing here touches hardware, and nothing
here is a device's performance or a physical qualification."""
import json
import os
import queue
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_fixture import Isolated, write_login_scenario  # noqa: E402
from native_client import binary  # noqa: E402

BACK = {"action": "key", "key": "back"}
HOME = {"action": "key", "key": "home"}
ENTER = {"action": "key", "key": "enter"}


def scenario(box, name, latency_ms=0, **knobs):
    directory = write_login_scenario(box.fixtures, name=name, latency_ms=latency_ms)
    path = directory / "scenario.json"
    value = json.loads(path.read_text())
    value.update(journal=True, **knobs)
    path.write_text(json.dumps(value))
    return directory


def journal(directory):
    path = Path(directory) / "journal.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Coordination(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.box = Isolated(prefix="coordination-")
        cls.env = cls.box.env

    @classmethod
    def tearDownClass(cls):
        cls.box.close()

    def tearDown(self):
        self.assertEqual(self.box.trapped(), "", "a device helper was reached")

    # ---- helpers -----------------------------------------------------------------

    def call(self, operation, arguments=None, env=None):
        return self.box.call(operation, arguments or {}) if env is None else self.run_with(operation, arguments, env)

    def run_with(self, operation, arguments, env):
        done = subprocess.run([binary(), "call", operation, json.dumps(arguments or {})], env=env,
                              capture_output=True, text=True, timeout=120)
        return json.loads(done.stdout)

    def ok(self, operation, arguments=None):
        reply = self.call(operation, arguments)
        self.assertTrue(reply["ok"], reply)
        return reply["result"]

    def refused(self, operation, arguments, code):
        reply = self.call(operation, arguments)
        self.assertFalse(reply["ok"], reply)
        self.assertEqual(reply["error"]["code"], code, reply)
        return reply["error"]

    def background(self, operation, arguments):
        """A call running in its own process; join() answers its envelope."""
        out = queue.Queue()
        thread = threading.Thread(target=lambda: out.put(self.call(operation, arguments)), daemon=True)
        thread.start()
        return lambda: (thread.join(120), out.get_nowait())[1]

    def start(self, name):
        return self.ok("runtime.start", {"platform": "android", "device": f"fixture:{name}"})["session"]

    def acquire(self, device, owner, **extra):
        return self.ok("device.reservation.acquire", {"device": device, "owner": owner, **extra})

    def input(self, session, step, **extra):
        return self.call("runtime.input", {"session": session, **step, **extra})

    # ---- reservations ------------------------------------------------------------

    def test_two_owners_contend_on_one_device_and_other_devices_stay_independent(self):
        one = scenario(self.box, "contend-one")
        two = scenario(self.box, "contend-two")
        first, second = self.start("contend-one"), self.start("contend-two")
        held = self.acquire(first, "agent-a", task="checkout", reason="multi-step flow", ttl_ms=60000)
        token = held["reservation_token"]
        self.assertEqual(held["device"]["key"], "android/local/fixture:contend-one")
        self.assertEqual(held["device"]["resolved_from"], "session")
        self.assertIn("Cooperative", held["boundary"])
        # The same device under its own name is the same reservation.
        error = self.refused("device.reservation.acquire", {"device": "fixture:contend-one", "platform": "android", "owner": "agent-a"},
                             "device_reserved")
        self.assertEqual(error["details"]["held_by"]["owner"], "agent-a", "a matching label is no authentication")
        self.assertEqual(error["details"]["held_by"]["task"], "checkout")
        self.assertNotIn(token, json.dumps(error))
        # Input without the token is refused before it is queued; with it, it lands.
        self.refused("runtime.input", {"session": first, **BACK}, "device_reserved")
        self.assertEqual(journal(one), [])
        self.assertTrue(self.input(first, BACK, reservation_token=token)["ok"])
        self.assertEqual(len(journal(one)), 1)
        # The other device is not affected, and has its own reservation.
        self.assertTrue(self.input(second, BACK)["ok"])
        other = self.acquire(second, "agent-b")
        self.refused("runtime.input", {"session": second, **BACK, "reservation_token": token}, "reservation_not_held")
        self.assertEqual(len(journal(two)), 1)
        # Status names holders and expiry, never a token.
        state = self.ok("device.reservation.get", {"device": first, "reservation_token": token})
        self.assertEqual((state["reserved"], state["token"], state["reservation"]["owner"]), (True, "holds", "agent-a"))
        self.assertGreater(state["reservation"]["expires_in_ms"], 50000)
        listed = self.ok("device.reservation.list")
        self.assertEqual({r["device"] for r in listed["reservations"]} >= {held["device"]["key"], other["device"]["key"]}, True)
        self.assertNotIn(token, json.dumps([state, listed]))
        self.assertNotIn(other["reservation_token"], json.dumps([state, listed]))
        for device, reservation in ((first, held), (second, other)):
            self.ok("device.reservation.release", {"device": device, "reservation_token": reservation["reservation_token"]})

    def test_parallel_acquisitions_have_exactly_one_winner(self):
        scenario(self.box, "race")
        session = self.start("race")
        calls = [self.background("device.reservation.acquire", {"device": session, "owner": f"agent-{i}"}) for i in range(8)]
        replies = [join() for join in calls]
        winners = [r for r in replies if r["ok"]]
        self.assertEqual(len(winners), 1, replies)
        self.assertEqual({r["error"]["code"] for r in replies if not r["ok"]}, {"device_reserved"})
        self.ok("device.reservation.release", {"device": session, "reservation_token": winners[0]["result"]["reservation_token"]})

    def test_a_reservation_that_expires_while_a_request_is_queued_refuses_it(self):
        slow = scenario(self.box, "queued", latency_ms=1500)
        session = self.start("queued")
        token = self.acquire(session, "agent", ttl_ms=1000)["reservation_token"]
        first = self.background("runtime.input", {"session": session, **BACK, "reservation_token": token, "request_id": "q-1"})
        time.sleep(0.3)
        # Accepted at the queue (the reservation still holds), refused at execution 1.5 s later.
        second = self.background("runtime.input", {"session": session, **HOME, "reservation_token": token, "request_id": "q-2"})
        one, two = first(), second()
        self.assertTrue(one["ok"], one)
        self.assertFalse(two["ok"], two)
        self.assertEqual(two["error"]["code"], "reservation_expired")
        self.assertEqual([entry["key"] for entry in journal(slow)], ["back"], "the queued input never reached the device")
        record = self.ok("device.request.get", {"request_id": "q-2"})["request"]
        self.assertEqual((record["status"], record["children"][0]["status"]), ("refused", "refused"))
        self.assertEqual(self.ok("device.request.get", {"request_id": "q-1"})["request"]["status"], "applied")
        # Expired means free: anyone may act, and a renewal cannot revive it.
        self.refused("device.reservation.renew", {"device": session, "reservation_token": token}, "reservation_expired")
        self.assertTrue(self.input(session, BACK)["ok"])
        # It sent nothing, so the same identity runs now, once.
        self.assertTrue(self.input(session, HOME, request_id="q-2")["ok"])
        self.assertEqual([entry["key"] for entry in journal(slow)], ["back", "back", "home"])

    def test_renewal_release_takeover_stale_tokens_and_a_restarted_service(self):
        directory = scenario(self.box, "lifecycle")
        session = self.start("lifecycle")
        token = self.acquire(session, "agent", ttl_ms=1000)["reservation_token"]
        renewed = self.ok("device.reservation.renew", {"device": session, "reservation_token": token, "ttl_ms": 30000})
        self.assertGreater(renewed["reservation"]["expires_in_ms"], 20000)
        time.sleep(1.3)
        self.assertTrue(self.input(session, BACK, reservation_token=token)["ok"], "the renewal outlived the first ttl")
        # The device service restarts: the reservation is the device's, not the session's.
        self.ok("runtime.stop", {"session": session})
        session = self.start("lifecycle")
        self.refused("runtime.input", {"session": session, **BACK}, "device_reserved")
        self.assertTrue(self.input(session, BACK, reservation_token=token)["ok"])
        released = self.ok("device.reservation.release", {"device": session, "reservation_token": token})
        self.assertTrue(released["released"])
        self.refused("runtime.input", {"session": session, **BACK, "reservation_token": token}, "reservation_released")
        again = self.ok("device.reservation.release", {"device": session, "reservation_token": token})
        self.assertEqual((again["released"], again["state"]), (False, "released"))
        # A person takes over: the holder's next input is refused with the reason.
        holder = self.acquire(session, "agent")["reservation_token"]
        taken = self.ok("device.reservation.takeover", {"device": session, "reason": "I need the phone", "acquire": True, "owner": "desk-person"})
        self.assertEqual(taken["revoked"]["owner"], "agent")
        error = self.refused("runtime.input", {"session": session, **BACK, "reservation_token": holder}, "reservation_revoked")
        self.assertIn("I need the phone", error["details"]["ended"]["note"])
        self.assertEqual(error["details"]["held_by"]["owner"], "desk-person")
        person = taken["reservation"]["reservation_token"]
        self.assertTrue(self.input(session, BACK, reservation_token=person)["ok"])
        # Tokens nobody issued, and ones that are not tokens at all (never quoted back).
        self.refused("runtime.input", {"session": session, **BACK, "reservation_token": "tmres_" + "0" * 64}, "reservation_not_held")
        error = self.refused("runtime.input", {"session": session, **BACK, "reservation_token": "hunter2-" + "x" * 62}, "invalid_argument")
        self.assertNotIn("hunter2", json.dumps(error))
        self.ok("device.reservation.release", {"device": session, "reservation_token": person})
        self.assertEqual(len(journal(directory)), 3, "only the three admitted inputs reached the device")

    def test_a_reserved_batch_is_never_interleaved_and_an_unreserved_one_stops_at_a_new_reservation(self):
        directory = scenario(self.box, "batch")
        session = self.start("batch")
        token = self.acquire(session, "agent-a")["reservation_token"]
        batch = self.background("runtime.actions", {"session": session, "reservation_token": token, "steps": [
            {"action": "tap", "x": 0.5, "y": 0.47}, {"action": "wait", "ms": 1500}, {"action": "text", "text": "ana"}, ENTER]})
        time.sleep(0.6)
        self.refused("runtime.input", {"session": session, **BACK}, "device_reserved")
        self.refused("device.reservation.acquire", {"device": session, "owner": "agent-b", "wait_ms": 0}, "device_reserved")
        reply = batch()
        self.assertTrue(reply["ok"], reply)
        self.assertEqual([entry["action"] for entry in journal(directory)], ["tap", "text", "key"])
        self.ok("device.reservation.release", {"device": session, "reservation_token": token})
        # Unreserved, the batch runs until someone reserves the device; then its next input is refused.
        before = len(journal(directory))
        batch = self.background("runtime.actions", {"session": session, "steps": [BACK, {"action": "wait", "ms": 1500}, BACK]})
        time.sleep(0.6)
        other = self.acquire(session, "agent-b")["reservation_token"]
        reply = batch()
        self.assertFalse(reply["ok"], reply)
        details = reply["error"]["details"]
        self.assertEqual(details["applied_steps"], [0])
        self.assertEqual(details["steps"][2]["error"]["code"], "device_reserved")
        self.assertEqual(len(journal(directory)), before + 1)
        self.ok("device.reservation.release", {"device": session, "reservation_token": other})

    def test_a_held_touch_from_the_desktop_delays_an_acquisition_and_then_is_refused(self):
        directory = scenario(self.box, "desktop")
        session = self.start("desktop")
        state = self.ok("runtime.state", {"session": session})

        def raw(arguments):
            # What the desktop's live view sends: no token, no request identity.
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(10)
                client.connect(state["control_socket"])
                client.sendall((json.dumps({"operation": "input", "arguments": {"session": session, **arguments}}) + "\n").encode())
                data = b""
                while not data.endswith(b"\n"):
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            return json.loads(data)

        self.assertTrue(raw({"action": "touch", "phase": "down", "x": 0.5, "y": 0.1})["ok"])
        self.assertTrue(raw({"action": "touch", "phase": "move", "x": 0.5, "y": 0.2})["ok"])
        error = self.refused("device.reservation.acquire", {"device": session, "owner": "agent", "wait_ms": 300}, "device_input_in_flight")
        self.assertEqual(error["details"]["in_flight"]["touches"], 1)
        self.assertTrue(raw({"action": "touch", "phase": "up", "x": 0.5, "y": 0.2})["ok"])
        token = self.acquire(session, "agent", wait_ms=0)["reservation_token"]
        refused = raw({"action": "tap", "x": 0.5, "y": 0.5})
        self.assertEqual(refused["error"]["code"], "device_reserved")
        self.assertIn("agent", refused["error"]["message"])
        # Lifting a finger is always allowed, so none is left down.
        self.assertTrue(raw({"action": "touch", "phase": "cancel", "x": 0.5, "y": 0.2})["ok"])
        self.assertEqual([entry["phase"] for entry in journal(directory)], ["down", "move", "up", "cancel"])
        self.ok("device.reservation.release", {"device": session, "reservation_token": token})

    def test_direct_android_and_virtual_iphone_input_meet_the_same_guard_before_their_transport(self):
        token = self.acquire("emulator-5554", "agent", platform="android")["reservation_token"]
        self.refused("android.input", {"device": "emulator-5554", "action": "keyevent", "key": "KEYCODE_HOME"}, "device_reserved")
        self.refused("screen.tap", {"device": "emulator-5554", "mark": 1}, "device_reserved")
        self.assertEqual(self.box.trapped(), "", "refused before adb ran")
        # With the token the guard passes and adb is reached (trapped here).
        reply = self.call("android.input", {"device": "emulator-5554", "action": "keyevent", "key": "KEYCODE_HOME", "reservation_token": token})
        self.assertFalse(reply["ok"])
        self.assertNotIn(reply["error"]["code"], ("device_reserved", "reservation_not_held"))
        self.assertIn("TRAP", self.box.trapped())
        self.box.trap_log.unlink()
        self.ok("device.reservation.release", {"device": "emulator-5554", "reservation_token": token})
        held = self.acquire("virtual-iphone", "agent")["reservation_token"]
        job = "0" * 32
        self.refused("ios.emulator.control", {"job_id": job, "action": "click", "x": 0.5, "y": 0.5}, "device_reserved")
        reply = self.call("ios.emulator.control", {"job_id": job, "action": "click", "x": 0.5, "y": 0.5, "reservation_token": held})
        self.assertNotEqual(reply["error"]["code"], "device_reserved", "past the guard, the missing guest answers")
        # Frames and status are not input and pass a reservation.
        status = self.call("ios.emulator.control", {"job_id": job, "action": "status"})
        self.assertNotEqual(status.get("error", {}).get("code"), "device_reserved")
        self.ok("device.reservation.release", {"device": "virtual-iphone", "reservation_token": held})

    def test_mark_taps_and_gestures_follow_the_same_contracts(self):
        directory = scenario(self.box, "marks")
        session = self.start("marks")
        self.ok("screen.marks", {"device": session, "path": str(self.box.home / "marks.png")})
        tap = self.call("screen.tap", {"device": session, "mark": 1, "request_id": "mark-1"})
        self.assertTrue(tap["ok"], tap)
        repeat = self.call("screen.tap", {"device": session, "mark": 1, "request_id": "mark-1"})
        self.assertTrue(repeat["result"]["request"]["retained"], repeat)
        self.assertEqual(len(journal(directory)), 1)
        record = self.ok("device.request.get", {"request_id": "mark-1"})["request"]
        self.assertEqual([child["id"] for child in record["children"]], ["mark-1/1"])
        token = self.acquire(session, "agent")["reservation_token"]
        swipe = {"session": session, "contacts": [{"points": [{"x": 0.5, "y": 0.8, "t_ms": 0}, {"x": 0.5, "y": 0.2, "t_ms": 80}]}]}
        self.refused("runtime.gesture", swipe, "device_reserved")
        self.refused("screen.tap", {"device": session, "mark": 1}, "device_reserved")
        gesture = self.call("runtime.gesture", {**swipe, "reservation_token": token, "request_id": "swipe-1"})
        self.assertTrue(gesture["ok"], gesture)
        self.assertEqual(gesture["result"]["mode"], "live")
        # A malformed trace is refused before it is recorded.
        bad = {"session": session, "contacts": [{"points": [{"x": 0.5, "y": 0.8, "t_ms": 50}, {"x": 0.5, "y": 0.2, "t_ms": 10}]}]}
        self.refused("runtime.gesture", {**bad, "reservation_token": token, "request_id": "swipe-bad"}, "invalid_argument")
        self.refused("device.request.get", {"request_id": "swipe-bad"}, "request_not_found")
        self.assertEqual([entry["action"] for entry in journal(directory)], ["tap", "gesture"])
        self.ok("device.reservation.release", {"device": session, "reservation_token": token})

    # ---- the request ledger ----------------------------------------------------

    def test_the_ledger_survives_process_exit_and_a_repeated_identity_is_not_sent_again(self):
        directory = scenario(self.box, "ledger")
        session = self.start("ledger")
        first = self.input(session, BACK, request_id="back-1")
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["result"]["request"], {"id": "back-1", "state": "completed", "status": "applied", "retained": False})
        # A later process, even through a new session of the same device, gets the record.
        self.ok("runtime.stop", {"session": session})
        session = self.start("ledger")
        again = self.input(session, BACK, request_id="back-1")
        self.assertTrue(again["ok"], again)
        self.assertEqual((again["result"]["request"]["retained"], again["result"]["request"]["replayed"]), (True, False))
        self.assertEqual(len(journal(directory)), 1)
        # The same identity for another intent conflicts and sends nothing.
        conflict = self.refused("runtime.input", {"session": session, **HOME, "request_id": "back-1"}, "request_conflict")
        self.assertEqual(conflict["details"]["differs"], ["arguments"])
        record = self.ok("device.request.get", {"request_id": "back-1"})["request"]
        self.assertEqual((record["state"], record["status"], record["route"]), ("completed", "applied", "runtime-worker"))
        self.assertEqual([child["id"] for child in record["children"]], ["back-1/1"])
        self.assertEqual(record["children"][0]["transport"], "fixture")
        # Malformed requests mutate and record nothing.
        self.refused("runtime.input", {"session": session, **BACK, "request_id": "has space"}, "invalid_argument")
        self.refused("runtime.actions", {"session": session, "request_id": "bad-batch", "steps": [BACK, {"action": "swipe", "x": 0.5}]},
                     "invalid_argument")
        self.refused("device.request.get", {"request_id": "bad-batch"}, "request_not_found")
        self.assertEqual(len(journal(directory)), 1)
        # A batch under one identity: its steps are its children, and a repeat is the record.
        steps = [{"action": "tap", "x": 0.5, "y": 0.47}, {"action": "wait", "ms": 200}, {"action": "text", "text": "ana"}]
        batch = self.call("runtime.actions", {"session": session, "request_id": "login-1", "steps": steps})
        self.assertTrue(batch["ok"], batch)
        repeat = self.call("runtime.actions", {"session": session, "request_id": "login-1", "steps": steps})
        self.assertTrue(repeat["result"]["request"]["retained"])
        self.assertEqual(len(journal(directory)), 3)
        record = self.ok("device.request.get", {"request_id": "login-1"})["request"]
        self.assertEqual([child["id"] for child in record["children"]], ["login-1/step-0", "login-1/step-2"])
        # Refused before anything was prepared: nothing recorded, and the identity runs once it is allowed.
        token = self.acquire(session, "other")["reservation_token"]
        self.refused("runtime.input", {"session": session, **HOME, "request_id": "home-1"}, "device_reserved")
        self.refused("device.request.get", {"request_id": "home-1"}, "request_not_found")
        self.ok("device.reservation.release", {"device": session, "reservation_token": token})
        self.assertTrue(self.input(session, HOME, request_id="home-1")["ok"])
        self.assertEqual(len(journal(directory)), 4)

    def test_a_lost_answer_is_unknown_and_never_replayed_or_failed_over(self):
        directory = scenario(self.box, "lossy", lose_reply_after=HOME, crash_after=ENTER)
        session = self.start("lossy")
        # The fixture took the input, and its answer was lost.
        lost = self.input(session, HOME, request_id="home-lost")
        self.assertEqual(lost["error"]["code"], "runtime_timeout")
        self.assertEqual(lost["error"]["details"]["request"]["status"], "unknown")
        repeat = self.input(session, HOME, request_id="home-lost")
        self.assertEqual(repeat["error"]["code"], "request_outcome_unknown")
        self.assertIn("never resend", repeat["error"]["remedy"])
        self.assertEqual(sum(1 for e in journal(directory) if e["key"] == "home"), 1)
        # The device service dies right after it accepted the input.
        crashed = self.input(session, ENTER, request_id="enter-crash")
        self.assertFalse(crashed["ok"], crashed)
        deadline = time.monotonic() + 10
        while self.ok("runtime.state", {"session": session})["status"] == "connected" and time.monotonic() < deadline:
            time.sleep(0.1)
        # A new service instance (another session) is not used to send it again.
        replacement = self.start("lossy")
        self.assertNotEqual(replacement, session)
        repeat = self.input(replacement, ENTER, request_id="enter-crash")
        self.assertEqual(repeat["error"]["code"], "request_outcome_unknown", repeat)
        record = self.ok("device.request.get", {"request_id": "enter-crash"})["request"]
        self.assertEqual(record["state"], "unknown")
        self.assertEqual(record["children"][0]["state"], "unknown")
        self.assertIn(session, record["children"][0]["instance"], "the record names the instance that had it")
        self.assertEqual(sum(1 for e in journal(directory) if e["key"] == "enter"), 1)
        # Verified, refused and unknown stay apart in the list.
        states = {r["id"]: (r["state"], r["status"]) for r in self.ok("device.request.list", {"device": replacement, "limit": 200})["requests"]}
        self.assertEqual(states["home-lost"], ("unknown", "unknown"))
        self.assertEqual(states["enter-crash"][0], "unknown")

    def test_a_killed_caller_is_answered_from_the_worker_record_without_a_second_input(self):
        directory = scenario(self.box, "killed", latency_ms=1200)
        session = self.start("killed")
        process = subprocess.Popen([binary(), "call", "runtime.input", json.dumps({"session": session, **BACK, "request_id": "slow-1"})],
                                   env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        process.kill()
        process.wait(10)
        time.sleep(1.5)
        repeat = self.input(session, BACK, request_id="slow-1")
        self.assertTrue(repeat["ok"], repeat)
        self.assertTrue(repeat["result"]["request"]["retained"])
        self.assertIn("device service", repeat["result"]["request"]["reconciled_from"])
        self.assertEqual(len(journal(directory)), 1)

    # ---- disclosure and equivalence ------------------------------------------

    def test_audit_status_records_and_logs_hold_no_token_and_no_typed_text(self):
        scenario(self.box, "privacy")
        session = self.start("privacy")
        secret = "correct-horse-battery-7731"
        token = self.acquire(session, "agent")["reservation_token"]
        steps = [{"action": "tap", "x": 0.5, "y": 0.47}, {"action": "text", "text": secret}]
        self.assertTrue(self.call("runtime.actions", {"session": session, "steps": steps, "reservation_token": token, "request_id": "pw-1"})["ok"])
        self.refused("runtime.input", {"session": session, "action": "text", "text": secret, "reservation_token": "tmres_" + "1" * 64},
                     "reservation_not_held")
        shown = json.dumps([self.ok("device.reservation.get", {"device": session, "reservation_token": token}),
                            self.ok("device.reservation.list"), self.ok("device.request.get", {"request_id": "pw-1"}),
                            self.ok("device.request.get", {"request_id": "pw-1/step-1"}), self.ok("device.request.list"),
                            self.ok("audit.list", {"limit": 500})])
        self.assertNotIn(token, shown)
        self.assertNotIn(token[6:], shown)
        self.assertNotIn(secret, shown)
        stored = b""
        for path in (self.box.home / "tomacco").rglob("*"):
            if path.is_file() and not path.name.endswith(".png"):
                stored += path.read_bytes()
        self.assertNotIn(token[6:].encode(), stored, "no file under the data directory holds the token")
        self.assertNotIn(secret.encode(), stored, "no file holds the typed text")
        for database in (self.box.home / "tomacco/coordination").glob("*.sqlite3"):
            self.assertEqual(database.stat().st_mode & 0o077, 0, database)
        self.ok("device.reservation.release", {"device": session, "reservation_token": token})

    def test_mcp_offers_the_same_operations_and_records_its_client(self):
        schema = json.loads(subprocess.run([binary(), "schema"], env=self.env, capture_output=True, text=True, timeout=60).stdout)
        listed = schema["result"]
        listed = listed["operations"] if isinstance(listed, dict) else listed
        operations = {o["name"]: o for o in listed}
        process = subprocess.Popen([binary(), "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=self.env)
        try:
            def request(number, method, params):
                process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": number, "method": method, "params": params}) + "\n")
                process.stdin.flush()
                while True:
                    reply = json.loads(process.stdout.readline())
                    if reply.get("id") == number:
                        return reply
            request(1, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "coordination-test", "version": "1"}})
            tools = {t["name"]: t for t in request(2, "tools/list", {})["result"]["tools"]}
            for name in ("device.reservation.acquire", "device.reservation.get", "device.reservation.list", "device.reservation.renew",
                         "device.reservation.release", "device.reservation.takeover", "device.request.get", "device.request.list"):
                self.assertEqual(tools[name]["inputSchema"], operations[name]["inputSchema"], name)
            for name in ("runtime.input", "runtime.gesture", "runtime.actions", "screen.tap", "android.input", "testing.action", "ios.emulator.control"):
                self.assertIn("reservation_token", tools[name]["inputSchema"]["properties"], name)
                self.assertIn("request_id", tools[name]["inputSchema"]["properties"], name)
            reply = request(3, "tools/call", {"name": "device.reservation.acquire", "arguments": {"device": "emulator-5560", "platform": "android", "owner": "mcp agent"}})
            held = json.loads(reply["result"]["content"][0]["text"])
            self.assertEqual((held["channel"], held["client"]), ("mcp", "coordination-test"))
            reply = request(4, "tools/call", {"name": "device.reservation.release", "arguments": {"device": "emulator-5560", "reservation_token": held["reservation_token"]}})
            self.assertFalse(reply["result"]["isError"], reply)
        finally:
            process.stdin.close()
            process.wait(20)
            process.stdout.close()
        audit = self.ok("audit.list", {"limit": 500, "operation": "device.reservation"})
        self.assertTrue(any(e["client"] == "coordination-test" for e in audit["entries"]))
        self.assertNotIn(held["reservation_token"], json.dumps(audit))


if __name__ == "__main__":
    unittest.main()
