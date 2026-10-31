#!/usr/bin/env python3
"""Verify real guest framebuffer capture through the public Rust API.

Requires locally prepared Apple firmware and the patched SPTM engine. This is
boot-display acceptance, never qualification of SpringBoard or UIKit apps.
"""
import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from PIL import Image
from native_client import dispatch, jobs, atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    os.environ["TOMACCO_HOME"] = str(out / "state")
    arguments = {"engine": str(args.engine.resolve()), "firmware": str(args.firmware.resolve()),
                 "boot_display": {"width": 1179, "height": 2556}, "seconds": 40}
    inspection = dispatch("ios.emulator.inspect", {key: arguments[key] for key in ("engine", "firmware")})
    assert inspection["capabilities"]["boot_framebuffer"]
    assert not inspection["capabilities"]["graphical_apps"]
    results = []
    for operation, extra, expected_state in (
        ("ios.emulator.probe", {}, "succeeded"),
        ("ios.emulator.exec", {"script": "printf 'actual guest failure test\\n'\nexit 7\n"}, "failed"),
    ):
        job = dispatch(operation, {**arguments, **extra})
        try:
            deadline = time.monotonic() + 90
            while True:
                result = jobs.wait(job["id"], 30)
                if result["state"] in jobs.TERMINAL:
                    break
                assert time.monotonic() < deadline, result
            assert result["state"] == expected_state, result
            directory = Path(job["report_directory"])
            report = json.loads((directory / "report.json").read_text())
            assert report["guest_boot_verified"] and report["guest_shell_verified"], report
            assert report["guest_exit_code"] == (7 if extra else 0)
            assert not report["full_ios_boot_verified"] and not report["graphical_apps_verified"]
            assert not report["qualified_for_app_development"]
            assert report["frame_provenance"] == "guest-ram-boot-framebuffer"
            assert report["boot_framebuffer_changed"]
            for key in ("frame_before_boot", "frame_capture"):
                artifact = report[key]
                file = Path(artifact["path"])
                assert file.is_relative_to(directory)
                assert hashlib.sha256(file.read_bytes()).hexdigest() == artifact["sha256"]
                with Image.open(file) as frame:
                    assert frame.format == "PNG" and frame.size == (1179, 2556)
                    colors = frame.convert("RGB").getcolors(1 << 24)
                    assert len(colors) == 1 if key == "frame_before_boot" else len(colors) > 1
            request = json.loads((directory / "request.json").read_text())
            assert not Path(request["machine_directory"]).exists()
            assert not Path(request["socket_directory"]).exists()
            results.append({"operation": operation, "job": job["id"], "state": result["state"],
                            "report": str(directory / "report.json"), "guest_exit_code": report["guest_exit_code"],
                            "frame_capture": report["frame_capture"], "boot_framebuffer_changed": True})
        finally:
            if jobs.get(job["id"])["state"] not in jobs.TERMINAL:
                jobs.cancel(job["id"])
                jobs.wait(job["id"], 10)
    value = {"state": "passed", "scope": "real-ios-boot-framebuffer", "graphical_apps_qualified": False,
             "inspection": inspection, "runs": results}
    atomic_json(out / "results.json", value)
    print(json.dumps(value, indent=2))


if __name__ == "__main__":
    main()
