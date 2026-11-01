"""Where a virtual iPhone start or stop spent its time.

A start's steps come from three records: the times the start itself noted
(tools/ios_virtual_iphone.py), the step times of the disk preparation
(guest-tools-install.json, tools/prepare_ios_guest_tools.py) and the launcher's
timing in the guest job's request.json (tools/run_ios_paravirt.py). Each is
optional: a step whose ends are unknown is folded into the next known one.
Reads nothing and writes nothing itself.
"""
import math

# A start's marks in the order they happen. Each step is named after the mark
# it ends at and lasts from the previous known mark.
START_MARKS = (
    ("begin", None),
    ("preparing", "checks"),            # app inspection, a guest-tools rebuild, kernel checks
    ("copy-storage", "preparation"),    # the preparation job until its disk copy begins
    ("install-preboot", "disk_copy"),   # copy, overlay or flatten of the disks
    ("complete", "guest_tools"),        # the helper that installs the guest tools
    ("booting", "boot_request"),
    ("emulator_started", "launcher"),   # the launcher before the engine; input_hashing is split out
    ("guest_ready", "boot"),            # engine start until the guest service answers
    ("applications_installed", "app_install"),
    ("settings_applied", "guest_settings"),  # watchdog, display scale, time zone, Auto-Lock
    ("launched", "unlock_and_launch"),
    ("finished", "display_check"),
)


def _time(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) else None


def _round(seconds):
    return round(max(seconds, 0.0), 1)


def preparation_marks(record):
    """The wall times the preparation's steps began, from its step_times."""
    marks = {}
    rows = record.get("step_times") if isinstance(record, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("step"), str) and _time(row.get("started")) is not None:
            marks.setdefault(row["step"], _time(row["started"]))
    return marks


def launcher_timing(request):
    """emulator_started (wall time) and input_hashing_seconds from a launcher
    request; empty for a launcher that did not record them."""
    timing = request.get("timing") if isinstance(request, dict) else None
    if not isinstance(timing, dict):
        return {}
    return {key: _time(timing.get(key)) for key in ("emulator_started", "input_hashing_seconds",
                                                    "input_hashing_bytes", "engine_exited",
                                                    "output_hashing_seconds", "output_hashing_bytes")
            if _time(timing.get(key)) is not None}


def summary(steps, total):
    known = [row for row in steps if row["seconds"] is not None]
    return {"steps": steps, "total_seconds": _round(total) if total is not None else None,
            "slowest": max(known, key=lambda row: row["seconds"])["step"] if known else None}


def start_breakdown(marks, preparation=None, request=None):
    """The steps of a start, from the start's own marks (wall times by name),
    the preparation record and the launcher request. Steps whose marks are
    missing are left out and their time goes to the next step."""
    times = {name: _time(value) for name, value in (marks or {}).items()}
    for name, value in preparation_marks(preparation).items():
        if name in ("copy-storage", "install-preboot", "complete"):
            times.setdefault(name, value)
    launch = launcher_timing(request)
    if "emulator_started" in launch:
        times.setdefault("emulator_started", launch["emulator_started"])
    steps, previous, skipped = [], None, []
    for name, step in START_MARKS:
        moment = times.get(name)
        if moment is None:
            if previous is not None and step is not None:
                skipped.append(step)
            continue
        if previous is not None and step is not None:
            seconds = moment - previous
            if name == "emulator_started" and "input_hashing_seconds" in launch:
                hashing = min(launch["input_hashing_seconds"], max(seconds, 0.0))
                row = {"step": "input_hashing", "seconds": _round(hashing)}
                if "input_hashing_bytes" in launch:
                    row["bytes"] = int(launch["input_hashing_bytes"])
                steps.append(row)
                seconds -= hashing
            row = {"step": step, "seconds": _round(seconds)}
            if skipped:
                # Steps whose marks are unknown took part of this time.
                row["includes"] = skipped
            steps.append(row)
            skipped = []
        previous = moment
    method = ((preparation or {}).get("root_disk") or {}).get("method") if isinstance(preparation, dict) else None
    for row in steps:
        if row["step"] == "disk_copy" and isinstance(method, str):
            row["method"] = method
    begin, end = times.get("begin"), previous
    return summary(steps, end - begin if begin is not None and end is not None else None)


def stop_breakdown(begin, report, finished):
    """The steps of a stop: until the engine exited (the guest's flush and
    exit), the hashing of its disks afterwards, and the rest (the report and
    the state). `report` is the launcher's result.json."""
    begin, finished = _time(begin), _time(finished)
    launch = launcher_timing(report)
    steps = []
    exited, hashing = launch.get("engine_exited"), launch.get("output_hashing_seconds")
    # A guest that ended before the stop (its session limit) also hashed its
    # disks before it: only the hashing is this stop's to report then.
    during = exited is not None and begin is not None and exited >= begin
    if during:
        steps.append({"step": "guest_exit", "seconds": _round(exited - begin)})
    if hashing is not None:
        row = {"step": "output_hashing", "seconds": _round(hashing)}
        if "output_hashing_bytes" in launch:
            row["bytes"] = int(launch["output_hashing_bytes"])
        steps.append(row)
    if during and finished is not None:
        steps.append({"step": "finalize", "seconds": _round(finished - exited - (hashing or 0.0))})
    return summary(steps, finished - begin if begin is not None and finished is not None else None)
