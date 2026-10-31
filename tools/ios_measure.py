#!/usr/bin/env python3
"""Measure the running virtual iPhone on a fixed workload.

Three measurements, each repeated: how long the owned app takes from the
open request until its window is on screen and until that screen is still,
how long a tap on its control takes to change the screen, and how many
distinct frames a Home-screen page swipe produces per second against the Home
screen's own idle rate (the Clock icon's second hand never stops). Frames are
read through the control plane's file transport, so every time is bounded
below by one frame read; the report states the frame-read interval it
observed. The app is recognised by its white window where the Home screen
shows the wallpaper between its icon columns, and the app's own log ties a
launch to its active scene and a tap to its logged action.

This reads and drives a guest that is already running; it does not start,
stop or install anything.
"""
import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time

# The app's one control, "Run native action", in its window's points
# (research-application.c: frame 24, 220, 280 x 48).
CONTROL = (24 + 280 / 2, 220 + 48 / 2)
# Below the app's label and control its window is plain white; on the Home
# screen these points fall between the icon columns, on the wallpaper.
APP_BACKGROUND = ((0.25, 0.45), (0.5, 0.45), (0.75, 0.45), (0.25, 0.75), (0.75, 0.75))


class MeasurementFailed(RuntimeError):
    pass


def summary(values):
    values = sorted(values)
    return {"count": len(values), "median": round(statistics.median(values), 3),
            "min": round(values[0], 3), "max": round(values[-1], 3)} if values else {"count": 0}


class Measure:
    def __init__(self, cli, job, application, settle=2.0, patience=1.0):
        self.cli, self.job, self.application = cli, job, application
        self.settle, self.patience = settle, patience
        self.reads = []

    def invoke(self, operation, arguments, timeout=180):
        done = subprocess.run([self.cli, "call", operation, json.dumps(arguments)],
                              capture_output=True, text=True, timeout=timeout)
        if not done.stdout.strip():
            raise MeasurementFailed(f"{operation} produced no reply: {done.stderr.strip()[:300]}")
        return json.loads(done.stdout)

    def control(self, action, **fields):
        reply = self.invoke("ios.emulator.control", {"job_id": self.job, "action": action, **fields})
        if not reply.get("ok"):
            raise MeasurementFailed(f"{action}: {reply.get('error', {}).get('message', 'failed')}")
        return (reply.get("result") or {}).get("result") or {}

    def guest(self, operation, argument=None, timeout=60):
        arguments = {"job_id": self.job, "operation": operation, "timeout_seconds": timeout}
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
            time.sleep(5 * self.patience)

    def frame(self):
        """The frame read now: whether it changed, when it was read, its file."""
        started = time.monotonic()
        frame = self.control("frame", transport="file")
        ended = time.monotonic()
        self.reads.append(ended - started)
        if not isinstance(frame.get("changed"), bool) or not isinstance(frame.get("path"), str):
            raise MeasurementFailed("The frame reply does not name its file and whether it changed")
        return frame["changed"], ended, frame["path"]

    @staticmethod
    def app_on_screen(path):
        """Whether the app's white window covers the points the Home screen leaves dark."""
        data = Path(path).read_bytes()
        fields, offset = [], 0
        while len(fields) < 4:
            while data[offset:offset + 1].isspace():
                offset += 1
            if data[offset:offset + 1] == b"#":
                offset = data.index(b"\n", offset) + 1
                continue
            end = offset
            while not data[end:end + 1].isspace():
                end += 1
            fields.append(data[offset:end])
            offset = end
        if fields[0] != b"P6" or fields[3] != b"255":
            raise MeasurementFailed("The frame file is not an 8-bit PPM")
        width, height, pixels = int(fields[1]), int(fields[2]), data[offset + 1:]
        if len(pixels) < width * height * 3:
            raise MeasurementFailed("The frame file is shorter than its picture")
        def white(x, y):
            at = (min(int(y * height), height - 1) * width + min(int(x * width), width - 1)) * 3
            return min(pixels[at:at + 3]) >= 230
        return all(white(x, y) for x, y in APP_BACKGROUND)

    def first_change(self, since, seconds):
        """Seconds from `since` to the first frame read that shows a change."""
        deadline = since + seconds * self.patience
        while time.monotonic() < deadline:
            changed, at, _ = self.frame()
            if changed:
                return at - since
        raise MeasurementFailed(f"The screen did not change within {seconds} s")

    def app_shown(self, since, seconds):
        """Seconds from `since` to the first frame with the app's window."""
        deadline = since + seconds * self.patience
        while time.monotonic() < deadline:
            _, at, path = self.frame()
            if self.app_on_screen(path):
                return at - since
        raise MeasurementFailed(f"The app's window did not appear within {seconds} s")

    def settled(self, since, seconds):
        """Seconds from `since` to the last change before `settle` quiet seconds.
        Only for the app's own screen: the Home screen is never still."""
        deadline, last, quiet = since + seconds * self.patience, since, None
        while time.monotonic() < deadline:
            changed, at, _ = self.frame()
            if changed:
                last, quiet = at, None
            elif quiet is None:
                quiet = at
            elif at - quiet >= self.settle:
                return last - since
        raise MeasurementFailed(f"The screen did not settle within {seconds} s")

    def changes_per_second(self, seconds):
        changes, started = 0, time.monotonic()
        while time.monotonic() - started < seconds:
            changes += self.frame()[0]
        return changes / (time.monotonic() - started)

    def events(self):
        response = self.guest("logs", "application")
        rows = []
        for line in (response.get("text") or "").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get("event"), str):
                rows.append(row)
        return rows

    def foreground(self):
        front = self.guest("foreground_state")
        return front.get("foreground_identifier_known") is True and front.get("frontmost_bundle_id")

    def unlocked(self):
        """Leave the lock screen the screen auto-locks to after a few idle
        minutes: Home wakes and opens a passcode-free one; after a SpringBoard
        restart Home alone can stay on an awake lock screen, where a side
        press and Home do it. An open request is refused while locked."""
        def state():
            screen = self.guest("screen_state")
            return screen.get("screen_locked") if screen.get("screen_state_known") is True else None
        if state() is False:
            return
        for presses in (("home", "home"), ("power_button", "home"), ("home", "home")):
            for action in presses:
                self.control(action)
                time.sleep(2 * self.patience)
            deadline = time.monotonic() + 30 * self.patience
            while time.monotonic() < deadline:
                if state() is False:
                    return
                time.sleep(2 * self.patience)
        raise MeasurementFailed("The phone stayed locked")

    def home(self):
        """Leave the app for the Home screen."""
        if self.foreground() != self.application:
            return
        self.control("home")
        deadline = time.monotonic() + 60 * self.patience
        while self.foreground() == self.application:
            if time.monotonic() >= deadline:
                raise MeasurementFailed("The app stayed in front after Home")
            time.sleep(2 * self.patience)
        # Let the closing animation end before the next measurement starts.
        time.sleep(3 * self.patience)

    def launch(self):
        self.unlocked()
        self.home()
        before = len(self.events())
        started = time.monotonic()
        opened = self.guest("open", self.application, timeout=120)
        if opened.get("request_accepted") is not True:
            raise MeasurementFailed(f"iOS refused to open the app: {opened.get('error')}")
        accepted = time.monotonic() - started
        shown = self.app_shown(started, 300)
        # A screen still from its first frame on settled when it was shown.
        settled = max(shown, self.settled(started, 300))
        rows = self.events()[before:]
        if not any(row["event"] == "scene-active" for row in rows):
            raise MeasurementFailed("The app logged no active scene for this launch")
        window = next((row for row in reversed(self.events()) if row["event"] == "window-state"), None)
        return {"accepted": accepted, "shown": shown, "settled": settled, "window": window}

    def touch(self, window):
        width, height = window.get("width"), window.get("height")
        before = len(self.events())
        self.settled(time.monotonic(), 60)
        started = time.monotonic()
        self.control("click", x=CONTROL[0] / width, y=CONTROL[1] / height)
        latency = self.first_change(started, 60)
        if not any(row["event"] == "control-action" for row in self.events()[before:]):
            raise MeasurementFailed("The app logged no action for the tap")
        return latency

    def swipe(self, duration_ms=900, seconds=4.0):
        """Distinct frames per second while a Home-screen page swipe plays,
        and on the same screen idle (its Clock icon keeps ticking)."""
        self.unlocked()
        self.home()
        # Always from the first page, where the Clock icon ticks. Home takes the
        # Home screen back to it; an app's Home leaves whatever page was showing.
        # On the Today page, which has nothing moving, the same engine read 0.25
        # idle and 1.75 swipe where the first page read 10.2 and 8.2.
        self.control("home")
        time.sleep(3 * self.patience)
        idle = self.changes_per_second(seconds * self.patience)
        reads = len(self.reads)
        started = time.monotonic()
        # The drag request returns only when the drag is over, by which time
        # the page has mostly settled: counted after it, a swipe read 0.25-0.5
        # changed frames a second where frames read during it show 3.3.
        # The drag is therefore sent from a thread while the
        # frames are read.
        drag = threading.Thread(target=self.control, args=("drag",),
                                kwargs={"x": 0.85, "y": 0.5, "to_x": 0.15, "to_y": 0.5, "duration_ms": duration_ms})
        drag.start()
        swiping = self.changes_per_second(seconds * self.patience)
        drag.join()
        rate = (len(self.reads) - reads) / (time.monotonic() - started)
        # Swipe back so every repetition starts on the same page.
        self.control("drag", x=0.15, y=0.5, to_x=0.85, to_y=0.5, duration_ms=duration_ms)
        return {"changed_frames_per_second": swiping, "idle_changed_frames_per_second": idle,
                "reads_per_second": rate}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cli", required=True, help="tomaccoctl executable")
    parser.add_argument("--job", help="Guest job; default: the one-step iPhone's running guest")
    parser.add_argument("--application", default="io.github.marselnenaj.tomacco.research-app")
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 20:
        parser.error("--repeat must be between 1 and 20")
    job = args.job
    if job is None:
        status = Measure(args.cli, None, args.application).invoke("ios.virtual.status", {})
        job = (status.get("result") or {}).get("guest_job")
        if not job or (status.get("result") or {}).get("closed") is not False:
            parser.error("The one-step iPhone is not running; start it or pass --job")
    measure = Measure(args.cli, job, args.application)
    launches, touches, swipes, failures = [], [], [], []
    for number in range(args.repeat):
        try:
            launch = measure.launch()
            launches.append(launch)
            touches.append(measure.touch(launch["window"]))
            swipes.append(measure.swipe())
        except MeasurementFailed as error:
            failures.append({"repetition": number + 1, "error": str(error)})
    report = {
        "job": job, "repetitions": args.repeat, "failures": failures,
        "launch_request_accepted_seconds": summary([row["accepted"] for row in launches]),
        "launch_shown_seconds": summary([row["shown"] for row in launches]),
        "launch_settled_seconds": summary([row["settled"] for row in launches]),
        "touch_to_change_seconds": summary(touches),
        "swipe_changed_frames_per_second": summary([row["changed_frames_per_second"] for row in swipes]),
        "home_idle_changed_frames_per_second": summary([row["idle_changed_frames_per_second"] for row in swipes]),
        "frame_read_seconds": summary(measure.reads),
        "note": "Times are bounded below by one frame read; frames per second by the read rate.",
    }
    print(json.dumps(report, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
