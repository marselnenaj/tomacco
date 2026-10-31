#!/usr/bin/env python3
"""Run the owned app through its lifecycle on the one-step virtual iPhone.

A cycle starts the phone (the start opens the profile's app), taps the app's
control, sends the app to the background with Home, opens it again and stops
the phone the careful way. The app appends every event to a log in its own
data container, so each cycle after the first also shows whether that data
survived the reboot.

Only the owned diagnostic app is supported: its log and its single control are
what this script observes. Every check reads the guest; an accepted request is
never taken as its result.
"""
import argparse
import json
import subprocess
import sys
import time

TERMINAL = ("succeeded", "failed", "cancelled")
# The app's one control, "Run native action", in its window's points
# (research-application.c: frame 24, 220, 280 x 48).
CONTROL = (24 + 280 / 2, 220 + 48 / 2)


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}), flush=True)


class CycleFailed(RuntimeError):
    pass


def count(rows, name):
    return sum(row["event"] == name for row in rows)


class Lifecycle:
    def __init__(self, cli, application, pause=5.0, patience=1.0, bundle=None):
        # patience scales every wait; the guest's pace is that of a slow phone.
        self.cli, self.application, self.pause, self.patience = cli, application, pause, patience
        # A guest path to install the app from at the start of each cycle.
        self.bundle = bundle

    def invoke(self, operation, arguments, timeout=180):
        done = subprocess.run([self.cli, "call", operation, json.dumps(arguments)],
                              capture_output=True, text=True, timeout=timeout)
        if not done.stdout.strip():
            raise RuntimeError(f"{operation} produced no reply: {done.stderr.strip()[:300]}")
        return json.loads(done.stdout)

    def call(self, operation, arguments, timeout=180):
        reply = self.invoke(operation, arguments, timeout)
        if not reply.get("ok"):
            raise RuntimeError(f"{operation}: {reply.get('error', {}).get('message', 'failed')}")
        return reply["result"]

    def run_job(self, operation):
        job = self.call(operation, {})
        while job["state"] not in TERMINAL:
            job = self.call("job.wait", {"job_id": job["id"], "seconds": 20})
        return job

    def guest(self, job, operation, argument=None, timeout=60):
        """The guest's native response, or {} when it gave none in time."""
        arguments = {"job_id": job, "operation": operation, "timeout_seconds": timeout}
        if argument is not None:
            arguments["argument"] = argument
        deadline = time.monotonic() + 120 * self.patience
        while True:
            reply = self.invoke("ios.emulator.guest", arguments, timeout + 60)
            response = ((reply.get("result") or {}).get("response") or {}) if reply.get("ok") else {}
            # The guest refuses a request while an earlier worker is still
            # ending; that refusal comes before any work, so asking again is safe.
            if response.get("error") != "guest_operation_busy" or time.monotonic() >= deadline:
                return response
            time.sleep(self.pause)

    def install(self, job, bundle):
        """Install `bundle` (a path in the guest) through installd and wait for
        its outcome. A reply that does not come in time is followed through
        the guest's receipt for that request; the install is never resent."""
        deadline = time.monotonic() + 120 * self.patience
        while True:
            reply = self.invoke("ios.emulator.guest", {"job_id": job, "operation": "install", "argument": bundle,
                                                       "timeout_seconds": 120}, 240)
            response = (reply.get("result") or {}).get("response") or {}
            # Refused before any work while an earlier worker is ending.
            if response.get("error") != "guest_operation_busy" or time.monotonic() >= deadline:
                break
            time.sleep(self.pause)
        details = reply.get("error", {}).get("details") or {}
        request = details.get("request_id")
        if not reply.get("ok") and details.get("request_may_have_executed") is True and request:
            deadline = time.monotonic() + 900 * self.patience
            response = {}
            while time.monotonic() < deadline:
                result = self.call("ios.emulator.guest_result", {"job_id": job, "request_id": request,
                                                                 "timeout_seconds": 30}, 120)
                if result.get("request_id") != request or result.get("state") == "running":
                    time.sleep(self.pause)
                    continue
                response = (result.get("response") or {}) if result.get("state") == "completed" else {}
                break
        if response.get("ok") is not True or response.get("request_accepted") is not True:
            raise CycleFailed(f"installd did not install {bundle}: "
                              f"{response.get('error') or reply.get('error', {}).get('code') or 'no outcome'}")
        return response

    def events(self, job):
        """The app's own log, oldest first; None when it cannot be read now."""
        response = self.guest(job, "logs", "application")
        if response.get("ok") is not True:
            return None
        if response.get("truncated") is True:
            # The guest returns a log's beginning; newer events would never show.
            raise CycleFailed("the app's log is larger than the guest returns; its latest events are not readable")
        rows = []
        for line in (response.get("text") or "").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get("event"), str):
                rows.append(row)
        return rows

    def until(self, what, check, seconds):
        deadline = time.monotonic() + seconds * self.patience
        while True:
            value = check()
            if value:
                return value
            if time.monotonic() >= deadline:
                raise CycleFailed(f"{what} was not observed within {seconds} s")
            time.sleep(self.pause)

    def later(self, job, after, name, seconds):
        """The first `name` event logged after the first `after` rows."""
        def check():
            rows = self.events(job)
            return next((row for row in (rows or [])[after:] if row["event"] == name), None)
        return self.until(f"the app's {name} event", check, seconds)

    def cycle(self, number, carried):
        """One start-to-stop cycle. carried holds the app's launches and taps
        logged by the end of the previous cycle; returns the same for this one."""
        started = time.monotonic()
        job = self.run_job("ios.virtual.start")
        status = self.call("ios.virtual.status", {})
        if job["state"] != "succeeded" or status.get("phase") != "launch-accepted" or not status.get("guest_job"):
            raise CycleFailed(f"the phone did not open the app: {status.get('phase')}: {status.get('error')}")
        guest, run = status["guest_job"], status.get("run")
        emit("started", cycle=number, run=run, seconds=round(time.monotonic() - started),
             display_visible=status.get("display_visible"))
        def launch():
            rows = self.events(guest)
            if not rows:
                return None
            # The log lives in the app's container on the guest's data volume.
            kept = {"launches": count(rows, "begin"), "taps": count(rows, "control-action")}
            if any(kept[key] < carried[key] for key in carried):
                # Lost across the reboot, or with an install that replaced the
                # app's container instead of upgrading it.
                raise CycleFailed(f"the app's data did not survive: {kept} logged, {carried} before")
            # The start's own launch, not the previous cycle's.
            return kept["launches"] > carried["launches"] and rows
        rows = self.until("the app's launch in its log", launch, 120)
        if self.bundle:
            # The install ends the replaced app once installd is done. The
            # installed copy then starts -- FrontBoard may restore the scene by
            # itself -- so everything after it is about that copy.
            receipt = self.install(guest, self.bundle)
            carried = {"launches": count(rows, "begin"), "taps": count(rows, "control-action")}
            asked = []
            def installed():
                rows = launch()
                if rows:
                    return rows
                # iOS refuses an open while FrontBoard is still launching the
                # app; ask again rather than read that refusal as a failure.
                if not asked or time.monotonic() - asked[-1] >= 30 * self.patience:
                    asked.append(time.monotonic())
                    self.guest(guest, "open", self.application, timeout=120)
                return None
            rows = self.until("the installed app's launch in its log", installed, 300)
            emit("installed", cycle=number, bundle=self.bundle, opens=len(asked),
                 replaced_app_ended=receipt.get("running_application_ended"))
        launched = max(index for index, row in enumerate(rows) if row["event"] == "begin")
        self.later(guest, launched, "scene-active", 300)
        window = self.later(guest, launched, "window-state", 60)
        width, height = window.get("width"), window.get("height")
        if not (isinstance(width, (int, float)) and isinstance(height, (int, float)) and width > 0 and height > 0):
            raise CycleFailed("the app reported no window size")
        before = len(self.events(guest) or rows)
        self.call("ios.emulator.control", {"job_id": guest, "action": "click",
                                           "x": CONTROL[0] / width, "y": CONTROL[1] / height})
        self.later(guest, before, "control-action", 60)
        self.call("ios.emulator.control", {"job_id": guest, "action": "home"})
        self.until("the app leaving the foreground", lambda: (
            (front := self.guest(guest, "foreground_state")).get("foreground_identifier_known") is True
            and front.get("frontmost_bundle_id") != self.application), 120)
        before = len(self.events(guest) or rows)
        opened = self.guest(guest, "open", self.application, timeout=120)
        if opened.get("request_accepted") is not True:
            raise CycleFailed(f"iOS refused to reopen the app: {opened.get('error')}")
        self.later(guest, before, "scene-active", 300)
        rows = self.events(guest) or rows
        logged = {"launches": count(rows, "begin"), "taps": count(rows, "control-action")}
        job = self.run_job("ios.virtual.stop")
        status = self.call("ios.virtual.status", {})
        if job["state"] != "succeeded" or status.get("phase") != "stopped" or status.get("closed") is not True:
            raise CycleFailed(f"the phone did not stop cleanly: {status.get('phase')}: {status.get('error')}")
        emit("cycle-passed", cycle=number, run=run, seconds=round(time.monotonic() - started), **logged)
        return logged


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cli", required=True, help="tomaccoctl executable")
    parser.add_argument("--application", default="io.github.marselnenaj.tomacco.research-app")
    parser.add_argument("--cycles", type=int, default=5)
    parser.add_argument("--install", metavar="GUEST_PATH",
                        help="Reinstall the app from this bundle path in the guest at the start of each cycle")
    args = parser.parse_args()
    if not 1 <= args.cycles <= 50:
        parser.error("--cycles must be between 1 and 50")
    lifecycle = Lifecycle(args.cli, args.application, bundle=args.install)
    status = lifecycle.call("ios.virtual.status", {})
    if status.get("closed") is not True:
        parser.error("Stop the virtual iPhone first; every cycle begins with a start")
    carried, passed = {"launches": 0, "taps": 0}, 0
    try:
        for number in range(1, args.cycles + 1):
            carried = lifecycle.cycle(number, carried)
            passed += 1
    except (CycleFailed, RuntimeError, subprocess.SubprocessError, ValueError) as error:
        emit("cycle-failed", cycle=passed + 1, error=str(error))
    emit("done", cycles=args.cycles, passed=passed)
    return 0 if passed == args.cycles else 1


if __name__ == "__main__":
    sys.exit(main())
