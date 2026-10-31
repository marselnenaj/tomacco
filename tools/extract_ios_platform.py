#!/usr/bin/env python3
"""Validate actual IOKit observations; service presence does not qualify a device."""
import argparse
import hashlib
import json
from pathlib import Path
import re

CLASSES = ("IOPlatformExpertDevice", "IOMobileFramebuffer", "AppleCLCD2", "AppleParavirtDisplay",
           "IOSurfaceRoot", "IOGPU", "IOHIDEventService", "AppleDCPExpert", "RTBuddyService",
           "AppleSEPManager", "AppleKeyStore", "IOMedia")
NODE_PATHS = tuple("IODeviceTree:/arm-io" + suffix for suffix in
                   ("", "/disp0", "/dispext0", "/dcp", "/sep", "/sgx"))
MAX_LOG = 256 * 1024
CLASS_LIMIT = 32
COMPAT_BYTES = 256
FIELDS = {
    "begin": ("format", "uid", "pid", "mode"),
    "snapshot": ("tick", "busy", "busy_error"),
    "service": ("tick", "query", "name", "class", "path", "registry_id", "busy", "errors"),
    "class": ("tick", "query", "result", "count", "truncated"),
    "node": ("tick", "path", "found", "compatible_type", "compatible_hex", "property_error"),
    "snapshot-end": ("tick", "services"), "end": ("snapshots",), "error": ("stage",),
}
NODE_PROVIDER_FIELDS = ("class", "class_error", "service_path", "service_path_error")


def integer(value, low=0, high=0xffffffff):
    return type(value) is int and low <= value <= high


def error_code(value):
    return integer(value, -0x80000000, 0x7fffffff)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def parse_log(raw):
    if len(raw) > MAX_LOG:
        raise ValueError("Platform log exceeds 256 KiB")
    events, snapshots = [], []
    pending = None
    ended = False
    terminal_error = None
    expected_ticks = []
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        if not line.startswith("TOMACCO_PLATFORM ") or len(line.encode()) >= 8192:
            raise ValueError("Unexpected or oversized platform record; use the dedicated UART")
        event = json.loads(line[len("TOMACCO_PLATFORM "):], object_pairs_hook=unique_object)
        if not isinstance(event, dict) or len(events) >= 1024:
            raise ValueError("Invalid or excessive platform events")
        name = event.get("event")
        fields = set(FIELDS.get(name, ()))
        if name == "node" and events and events[0]["format"] == 2:
            fields.update(NODE_PROVIDER_FIELDS)
        if (name not in FIELDS or set(event) != {"event", "seq", *fields} or
                not integer(event["seq"]) or event["seq"] != len(events) or ended or terminal_error):
            raise ValueError("Unknown, out-of-order or post-terminal platform event")
        if not events and name != "begin":
            raise ValueError("Platform log must begin with its contract")
        if name == "begin":
            if (events or event["format"] not in (1, 2) or type(event["format"]) is not int or
                    event["mode"] not in ("stdio", "uart") or not integer(event["uid"]) or
                    not integer(event["pid"], 1, 0x7fffffff)):
                raise ValueError("Invalid platform contract or identity")
            expected_ticks = [0] if event["mode"] == "stdio" else [30, 90]
        elif name == "snapshot":
            if (pending is not None or len(snapshots) >= len(expected_ticks) or
                    not integer(event["tick"]) or event["tick"] != expected_ticks[len(snapshots)] or
                    not integer(event["busy"]) or not error_code(event["busy_error"])):
                raise ValueError("Invalid snapshot order or busy state")
            pending = {"tick": event["tick"], "busy": event["busy"], "busy_error": event["busy_error"],
                       "classes": {}, "nodes": {}, "services": [], "uncommitted_services": []}
        elif name in ("service", "class", "node", "snapshot-end"):
            if pending is None or not integer(event["tick"]) or event["tick"] != pending["tick"]:
                raise ValueError("Record outside its snapshot")
            if name in ("service", "class"):
                index = len(pending["classes"])
                if index >= len(CLASSES) or event["query"] != CLASSES[index] or pending["nodes"]:
                    raise ValueError("Missing, duplicate or reordered service query")
                rows = pending["uncommitted_services"]
                if name == "service":
                    if (len(rows) >= CLASS_LIMIT or
                            not all(isinstance(event[k], str) and len(event[k].encode()) < size
                                    for k, size in (("name", 128), ("class", 128), ("path", 512))) or
                            not isinstance(event["registry_id"], str) or
                            not re.fullmatch(r"0x[0-9a-f]{16}", event["registry_id"]) or
                            not integer(event["busy"]) or not isinstance(event["errors"], dict) or
                            set(event["errors"]) != {"name", "class", "path", "id", "busy"} or
                            not all(error_code(v) for v in event["errors"].values())):
                        raise ValueError("Invalid service identity or API results")
                    if event["errors"]["id"] == 0 and any(row["errors"]["id"] == 0 and
                            row["registry_id"] == event["registry_id"] for row in rows):
                        raise ValueError("Duplicate service in one query")
                    rows.append(event)
                else:
                    if (not error_code(event["result"]) or not integer(event["count"], 0, CLASS_LIMIT) or
                            event["count"] != len(rows) or type(event["truncated"]) is not bool or
                            (event["result"] != 0 and (rows or event["truncated"])) or
                            (event["truncated"] and event["count"] != CLASS_LIMIT)):
                        raise ValueError("Service count contradicts its observations")
                    pending["classes"][event["query"]] = event
                    pending["services"].extend(rows)
                    pending["uncommitted_services"] = []
            elif name == "node":
                index = len(pending["nodes"])
                if (len(pending["classes"]) != len(CLASSES) or index >= len(NODE_PATHS) or
                        event["path"] != NODE_PATHS[index] or type(event["found"]) is not bool or
                        event["compatible_type"] not in ("absent", "data", "string", "unsupported") or
                        not isinstance(event["compatible_hex"], str) or
                        not re.fullmatch(r"(?:[0-9a-f]{2}){0,256}", event["compatible_hex"]) or
                        not integer(event["property_error"], 0, 1)):
                    raise ValueError("Invalid or reordered device-tree observation")
                if ((not event["found"] and (event["compatible_type"] != "absent" or event["property_error"])) or
                        (event["compatible_type"] == "absent" and (event["compatible_hex"] or event["property_error"])) or
                        (event["compatible_type"] == "unsupported" and not event["property_error"]) or
                        (event["property_error"] and event["compatible_hex"])):
                    raise ValueError("Device-tree compatibility contradicts its status")
                if events[0]["format"] == 2:
                    if (not all(isinstance(event[k], str) and len(event[k].encode()) < size
                                for k, size in (("class", 128), ("service_path", 512))) or
                            not error_code(event["class_error"]) or not error_code(event["service_path_error"]) or
                            (not event["found"] and (event["class"] or event["service_path"] or
                                event["class_error"] != -1 or event["service_path_error"] != -1))):
                        raise ValueError("Invalid device-tree provider identity")
                pending["nodes"][event["path"]] = event
            else:
                if (len(pending["classes"]) != len(CLASSES) or len(pending["nodes"]) != len(NODE_PATHS) or
                        pending["uncommitted_services"] or not integer(event["services"]) or
                        event["services"] != len(pending["services"])):
                    raise ValueError("Incomplete or contradictory snapshot completion")
                pending.pop("uncommitted_services")
                snapshots.append(pending)
                pending = None
        elif name == "end":
            if (pending is not None or not integer(event["snapshots"]) or
                    event["snapshots"] != len(expected_ticks) or len(snapshots) != len(expected_ticks)):
                raise ValueError("Premature or contradictory platform completion")
            ended = True
        elif name == "error":
            if not isinstance(event["stage"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", event["stage"]):
                raise ValueError("Invalid platform failure")
            terminal_error = event["stage"]
        events.append(event)
    if not events:
        raise ValueError("No platform observations")
    return {"identity": events[0], "events": events, "snapshots": snapshots, "completed": ended,
            "partial_snapshot": pending, "terminal_error": terminal_error}


def extract(source, output):
    if not source.is_file():
        raise ValueError("Platform input must be a regular file")
    with source.open("rb") as handle:
        raw = handle.read(MAX_LOG + 1)
    report = parse_log(raw)
    observations = []
    for snapshot in report["snapshots"]:
        def present(name):
            query = snapshot["classes"][name]
            return query["count"] > 0 if query["result"] == 0 else None
        observations.append({"tick": snapshot["tick"], "apple_sep_manager_present": present("AppleSEPManager"),
                             "mobile_framebuffer_present": present("IOMobileFramebuffer"),
                             "paravirtual_display_present": present("AppleParavirtDisplay"),
                             "gpu_service_present": present("IOGPU")})
    manifest = {"format": 1, "scope": "read-only-guest-platform-observations",
                "source_sha256": hashlib.sha256(raw).hexdigest(), "source_bytes": len(raw),
                "events": len(report["events"]), "completed": report["completed"],
                "completed_snapshots": len(report["snapshots"]), "terminal_error": report["terminal_error"],
                "observations": observations, "main_display_qualified": False,
                "sep_functionality_qualified": False, "graphical_apps_qualified": False,
                "full_ios_boot_qualified": False}
    # Validate everything before creating output, and never replace prior evidence.
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; choose a new directory")
    output.mkdir(parents=True, mode=0o700)
    for name, value in (("observations.json", report), ("manifest.json", manifest)):
        file = output / name
        with file.open("x") as handle:
            file.chmod(0o600)
            json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--console", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(extract(args.console, args.output), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as error:
        raise SystemExit(str(error))
