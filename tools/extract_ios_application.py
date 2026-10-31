#!/usr/bin/env python3
"""Validate observed UIApplication/scene callbacks from the research app UART.

This records the guest's actual progress, including attempts blocked before any
delegate callback. Callback observations never qualify a complete iOS emulator.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re

MAX_LOG = 64 * 1024
MAX_EVENTS = 256
FIELDS = {
    "begin": (), "identity": ("uid", "gid"), "load-uikit": (), "bundle-verified": (),
    "service-lookup": ("bootstrap_port", "result", "display_port"),
    "delegate-registration": ("app_class", "scene_class", "app_protocol", "scene_protocol"),
    "enter-application-main": (), "did-finish-launching": (), "scene-connected": (),
    "request-scene": (), "scene-request-failed": ("code",),
    "duplicate-scene": (), "window-creation-failed": (),
    "window-state": ("width", "height", "scale", "hidden", "key"),
    "scene-active": ("activation_state",), "control-action": ("count",),
    "application-main-returned": ("code",), "exit": ("code",),
    "fatal-signal": ("signal", "pc", "lr", "fp", "cache_base", "frames"),
}
STARTUP = ("begin", "identity", "load-uikit", "bundle-verified", "enter-application-main")


def unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate application event field")
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError("Non-finite application event value: " + value)


def integer(value, low=0, high=0xffffffff):
    return type(value) is int and low <= value <= high


def parse_log(raw):
    if len(raw) > MAX_LOG:
        raise ValueError("Application log exceeds 64 KiB")
    events, seen, startup, exited, returned, actions = [], set(), 0, False, None, 0
    for line in raw.decode("utf-8", errors="strict").splitlines():
        if not line.strip():
            continue
        if exited or len(events) >= MAX_EVENTS or len(line.encode("utf-8")) > 4096:
            raise ValueError("Trailing or oversized application events")
        event = json.loads(line, object_pairs_hook=unique_object, parse_constant=invalid_constant)
        if not isinstance(event, dict):
            raise ValueError("Application event must be an object")
        name = event.get("event")
        fields = set(event)
        details = name == "scene-request-failed" and {"domain", "message"} <= fields
        if len(line.encode("utf-8")) > 1024 and not details:
            raise ValueError("Oversized application event")
        if details:
            if not all(isinstance(event[key], str) and len(event[key]) <= 256 for key in ("domain", "message")):
                raise ValueError("Malformed or oversized scene error details")
            fields -= {"domain", "message"}
        if (not isinstance(name, str) or name not in FIELDS or
                fields != {"sequence", "event", *FIELDS[name]} or
                not integer(event["sequence"], 1, MAX_EVENTS) or event["sequence"] != len(events) + 1):
            raise ValueError("Unknown, malformed or out-of-order application event")
        if name in STARTUP:
            if startup >= len(STARTUP) or name != STARTUP[startup] or name in seen:
                raise ValueError("Missing or repeated application startup stage")
            startup += 1
        elif name == "delegate-registration":
            if startup != 4 or name in seen or not all(integer(event[key], 0, 1) for key in FIELDS[name]):
                raise ValueError("Invalid delegate registration observation")
        elif name == "service-lookup":
            if (startup != 2 or name in seen or not integer(event["result"], -0x80000000, 0x7fffffff) or
                    not all(integer(event[key]) for key in ("bootstrap_port", "display_port")) or
                    (event["result"] == 0 and event["display_port"] in (0, 0xffffffff))):
                raise ValueError("Invalid bootstrap service observation")
        elif name == "fatal-signal":
            if startup < 4:
                raise ValueError("Fatal trace before the crash recorder is installed")
        elif name != "exit" and startup != len(STARTUP):
            raise ValueError("Application callback before UIApplicationMain attempt")
        if name == "identity" and not all(integer(event[key]) for key in ("uid", "gid")):
            raise ValueError("Invalid guest identity")
        if name == "did-finish-launching" and name in seen:
            raise ValueError("Repeated application launch callback")
        if name == "request-scene" and ("did-finish-launching" not in seen or name in seen):
            raise ValueError("Scene request without a single application launch")
        if name == "scene-request-failed" and ("request-scene" not in seen or name in seen or
                not integer(event["code"], -0x8000000000000000, 0x7fffffffffffffff)):
            raise ValueError("Invalid scene request failure")
        if name == "scene-connected" and ("did-finish-launching" not in seen or name in seen):
            raise ValueError("Scene connection without a single application launch")
        if name in ("duplicate-scene", "window-creation-failed", "window-state", "scene-active", "control-action") and "scene-connected" not in seen:
            raise ValueError("Scene event before scene connection")
        if name == "window-state":
            if (name in seen or not all(type(event[key]) in (int, float) and math.isfinite(event[key]) and
                                       0 <= event[key] <= limit for key, limit in (("width", 16384), ("height", 16384), ("scale", 8))) or
                    not integer(event["hidden"], 0, 1) or not integer(event["key"], 0, 1)):
                raise ValueError("Invalid or repeated window observation")
        if name == "scene-active" and not integer(event["activation_state"], -1, 2):
            raise ValueError("Invalid scene activation state")
        if name == "control-action":
            if "window-state" not in seen or not integer(event["count"], 1) or event["count"] != actions + 1:
                raise ValueError("Unexpected control action count")
            actions = event["count"]
        if name == "fatal-signal":
            addresses = [event[key] for key in ("pc", "lr", "fp", "cache_base")]
            if (not integer(event["signal"], 5, 6) or not isinstance(event["frames"], list) or len(event["frames"]) > 16 or
                    not all(isinstance(value, str) and re.fullmatch(r"0x[0-9a-f]{16}", value)
                            for value in addresses + event["frames"])):
                raise ValueError("Malformed or oversized fatal signal evidence")
            exited = True
        if name in ("application-main-returned", "exit"):
            if not integer(event["code"], -0x80000000, 0x7fffffff):
                raise ValueError("Invalid application return code")
            if name == "application-main-returned":
                if returned is not None:
                    raise ValueError("Repeated UIApplicationMain return")
                returned = event["code"]
            else:
                if not events or (returned is not None and returned != event["code"]) or (event["code"] == 0 and returned is None):
                    raise ValueError("Exit without startup or with contradictory return code")
                exited = True
        elif returned is not None and name != "fatal-signal":
            raise ValueError("Callback after UIApplicationMain returned")
        events.append(event)
        seen.add(name)
    if not events:
        raise ValueError("No application events; inspect guest console and process diagnostics")
    return events


def extract(source, output):
    if not source.is_file():
        raise ValueError("Application input must be a regular file")
    with source.open("rb") as handle:
        raw = handle.read(MAX_LOG + 1)
    events = parse_log(raw)
    by_name = {event["event"]: event for event in events}
    identity = by_name.get("identity", {})
    mobile = identity.get("uid") == 501 and identity.get("gid") == 501
    window = by_name.get("window-state", {})
    visible = bool(window and min(window["width"], window["height"], window["scale"]) > 0 and
                   window["hidden"] == 0 and window["key"] == 1)
    active = by_name.get("scene-active", {}).get("activation_state") == 0
    lookup = by_name.get("service-lookup", {})
    lifecycle = (mobile and "did-finish-launching" in by_name and visible and active and
                 not {"duplicate-scene", "window-creation-failed"}.intersection(by_name))
    result = {"format": 1, "scope": "guest-application-lifecycle-observations", "source": str(source.resolve()),
              "source_sha256": hashlib.sha256(raw).hexdigest(), "events": events,
              "last_event": events[-1]["event"], "finished": "exit" in by_name,
              "process_exit_code": by_name.get("exit", {}).get("code"),
              "fatal_signal": by_name.get("fatal-signal"),
              "mobile_identity_verified": mobile, "application_main_attempted": "enter-application-main" in by_name,
              "display_service_lookup_succeeded": bool(lookup and lookup["result"] == 0),
              "launch_callback_observed": "did-finish-launching" in by_name,
              "scene_connected_observed": "scene-connected" in by_name,
              "scene_request_failure": by_name.get("scene-request-failed"),
              "visible_key_window_observed": visible, "active_scene_observed": active,
              "application_lifecycle_observed": bool(lifecycle),
              "full_ios_boot_qualified": False, "graphical_apps_qualified": False,
              "physical_touch_qualified": False}
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; choose a new directory")
    output.mkdir(mode=0o700, parents=True)
    path = output / "manifest.json"
    with path.open("x") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    path.chmod(0o600)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--console", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(extract(args.console, args.output), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error))
