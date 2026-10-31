#!/usr/bin/env python3
"""Qualify managed observations against a real, locally prepared iOS image.

This never qualifies SpringBoard or graphical applications. Protocol-fixture
tests live in tests/python/test_ios_research_runtime.py and are separate.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

from native_client import dispatch, jobs, atomic_json
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from extract_ios_platform import parse_log


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--firmware", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--job-id", help="Validate this already-submitted run instead of starting one")
    parser.add_argument("--seconds", type=int, default=360)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    arguments = {"engine": str(Path(args.engine).resolve()), "firmware": str(Path(args.firmware).resolve()), "seconds": args.seconds}
    job = jobs.get(args.job_id) if args.job_id else dispatch("ios.emulator.research", arguments)
    operation = job["plan"]["operation_key"]
    assert operation["operation"] == "ios.emulator.research"
    assert all(operation["arguments"][key] == value for key, value in arguments.items())
    atomic_json(output / "job.json", job)
    deadline = time.monotonic() + args.seconds + 360
    try:
        while job["state"] not in jobs.TERMINAL and time.monotonic() < deadline:
            job = jobs.wait(job["id"], 30)
            print("Research job:", job["state"], flush=True)
        value = dispatch("ios.emulator.report", {"job_id": job["id"]})
        atomic_json(output / "api-report.json", value)
        report = value["report"]
        folder = Path(value["report_directory"])
        for name in ("report.json", "request.json", "command.json", "engine.log", "console.log", "process.log", "graphics.log", "platform.log", "platform.json", "frame.png"):
            if (folder / name).is_file():
                shutil.copyfile(folder / name, output / name)
        assert job["state"] == "succeeded", report.get("error")
        assert report["state"] == "research_observed"
        assert report["guest_observations_verified"] and report["working_image_flushed"]
        assert not report["graphical_apps_verified"] and not report["full_ios_boot_verified"]
        assert not report["sep_functionality_verified"] and not report["macos_vm_used"]
        raw = (folder / "platform.log").read_bytes()
        independent = parse_log(raw)
        assert independent["completed"] and len(independent["snapshots"]) == 2
        assert independent["snapshots"] == report["platform"]["snapshots"]
        assert independent["events"] == report["platform"]["events"]
        assert report["platform"]["source_sha256"] == hashlib.sha256(raw).hexdigest()
        assert (folder / "frame.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        request = json.loads((folder / "request.json").read_text())
        assert not Path(request["machine_directory"]).exists()
        assert not Path(request["socket_directory"]).exists()
        source = Path(request["firmware"]["components"]["ramdisk"]["path"])
        with source.open("rb") as file:
            assert hashlib.file_digest(file, "sha256").hexdigest() == request["firmware"]["components"]["ramdisk"]["sha256"]
        pid = report["engine_pid"]
        assert not Path(f"/proc/{pid}").exists()
        result = {"managed_guest_observations_passed": True, "independent_parser_matches": True,
                  "source_image_preserved": True, "working_image_flushed": True,
                  "process_and_disposable_directories_removed": True,
                  "platform_sha256": report["platform"]["source_sha256"], "platform_bytes": len(raw),
                  "records": len(independent["events"]), "job_id": job["id"],
                  "full_ios_app_runtime_passed": False}
        atomic_json(output / "results.json", result)
        print(json.dumps(result, indent=2))
    finally:
        if jobs.get(job["id"])["state"] not in jobs.TERMINAL:
            jobs.cancel(job["id"])
            jobs.wait(job["id"], 10)


if __name__ == "__main__":
    main()
