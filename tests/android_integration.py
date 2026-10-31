#!/usr/bin/env python3
"""End-to-end acceptance test for the generated Android counter starter."""
import argparse
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from native_client import dispatch, atomic_json, jobs

parser = argparse.ArgumentParser()
parser.add_argument("--project", required=True)
parser.add_argument("--device", required=True)
parser.add_argument("--output", default="qa/android")
args = parser.parse_args()
project = dispatch("project.inspect", {"path": args.project})
bundle = project["configuration"]["applicationId"]
if not bundle.startswith("io.github.marselnenaj.tomacco."):
    raise SystemExit("This acceptance test only clears/controls io.github.marselnenaj.tomacco test apps.")
output = Path(args.output).resolve()
output.mkdir(parents=True, exist_ok=True)
checks = []


def finish(job):
    deadline = time.monotonic() + 600
    while job["state"] not in jobs.TERMINAL and time.monotonic() < deadline:
        job = jobs.wait(job["id"], 5)
    assert job["state"] == "succeeded", {"job": job, "logs": jobs.logs(job["id"])}
    checks.append({"label": job["label"], "job": job["id"], "state": job["state"]})
    return job


def nodes():
    return list(ET.fromstring(dispatch("android.hierarchy", {"device": args.device})["xml"]).iter("node"))


def tap_text(text):
    node = next(n for n in nodes() if n.attrib.get("text", "").lower() == text.lower())
    x1, y1, x2, y2 = map(int, re.findall(r"\d+", node.attrib["bounds"]))
    dispatch("android.input", {"device": args.device, "action": "tap", "x": (x1+x2)//2, "y": (y1+y2)//2})


deployed = finish(dispatch("project.run", {"path": args.project, "platform": "android", "device": args.device, "bundle_id": bundle}))
if "swift" in project["kinds"]:
    apk = next(p for p in deployed["result"]["artifacts"] if p["path"].endswith(".apk"))
    with zipfile.ZipFile(apk["path"]) as archive:
        for abi in ("arm64-v8a", "x86_64"):
            binary = archive.read("lib/" + abi + "/libTomaccoCore.so")
            assert binary[:4] == b"\x7fELF"
            assert b"MainActivity_nextCount" in binary
            assert "lib/" + abi + "/libc++_shared.so" in archive.namelist()
    checks.append({"label": "APK contains Swift JNI machine code and C++ runtime for ARM64 and x86_64", "state": "succeeded", "artifact": apk})
base = {"platform": "android", "device": args.device, "bundle_id": bundle}
finish(dispatch("device.app", {**base, "action": "clear-data"}))
finish(dispatch("device.app", {**base, "action": "launch"}))
time.sleep(1)
assert any(n.attrib.get("text") == "0" for n in nodes())
tap_text("Add one")
assert any(n.attrib.get("text") == "1" for n in nodes())
checks.append({"label": "Native UI hierarchy + coordinate input", "state": "succeeded"})
finish(dispatch("device.app", {**base, "action": "stop"}))
finish(dispatch("device.app", {**base, "action": "launch"}))
assert any(n.attrib.get("text") == "1" for n in nodes())
checks.append({"label": "App state persists across stop/relaunch", "state": "succeeded"})
screen = finish(dispatch("device.screenshot", {"platform": "android", "device": args.device, "path": str(output / "counter.png")}))
assert (output / "counter.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
memory = dispatch("android.performance", {"device": args.device, "bundle_id": bundle})
(output / "memory.txt").write_text(memory["stdout"])
assert bundle in memory["stdout"]
finish(dispatch("device.logs", {"platform": "android", "device": args.device, "bundle_id": bundle, "duration": 2}))
tap_text("Reset")
assert any(n.attrib.get("text") == "0" for n in nodes())
atomic_json(output / "results.json", {"passed": True, "device": dispatch("device.info", {"platform": "android", "device": args.device}), "checks": checks, "screenshot": screen["result"]})
print(json.dumps({"passed": True, "checks": len(checks), "output": str(output)}))
