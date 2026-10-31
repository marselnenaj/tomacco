#!/usr/bin/env python3
"""Actual Linux-built C/Embedded Swift execution in an iOS restore-shell guest."""
import argparse
import json
import os
from pathlib import Path
import shutil

from native_client import atomic_json, dispatch, jobs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--firmware", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    os.environ["TOMACCO_HOME"] = str(out / "state")
    arguments = {"engine": str(Path(args.engine).resolve()), "firmware": str(Path(args.firmware).resolve())}
    inspection = dispatch("ios.emulator.inspect", arguments)
    assert inspection["firmware"]["environment"] == "ios-restore-shell"
    assert {p["path"] for p in inspection["firmware"]["payload"]} == {"/tomacco/hello-c", "/tomacco/hello-swift"}
    cases = [
        ("native", "set -e\n/tomacco/hello-c\n/tomacco/hello-swift 0\n/tomacco/hello-swift 100\n/tomacco/hello-swift 1000\n", 0),
        ("invalid-input", "/tomacco/hello-swift 1001\n", 7),
    ]
    completed = []
    for name, script, expected_code in cases:
        job = dispatch("ios.emulator.exec", {**arguments, "script": script, "seconds": 45})
        try:
            result = jobs.wait(job["id"], 60)
            folder = Path(job["report_directory"])
            report = json.loads((folder / "report.json").read_text())
            assert report["guest_exit_code"] == expected_code, report.get("guest_output")
            assert result["state"] == ("succeeded" if expected_code == 0 else "failed")
            assert report["guest_shell_verified"] and not report["full_ios_boot_verified"]
            if name == "native":
                assert "running native ARM64 in iOS" in report["guest_output"]
                assert report["guest_output"].count("Embedded Swift compiled on Linux: Grüße 日本語 😀") == 3
                assert report["guest_output_utf8_valid"]
            request = json.loads((folder / "request.json").read_text())
            assert not Path(request["machine_directory"]).exists()
            assert not Path(request["socket_directory"]).exists()
            for artifact in ("report.json", "console.log", "guest-output.bin"):
                shutil.copyfile(folder / artifact, out / (name + "-" + artifact))
            completed.append({"case": name, "guest_exit_code": expected_code, "passed": True})
        finally:
            if jobs.get(job["id"])["state"] not in jobs.TERMINAL:
                jobs.cancel(job["id"])
                jobs.wait(job["id"], 10)
    record = {"native_cli_execution_passed": True, "embedded_swift_heap_and_unicode_passed": True,
              "guest_failure_propagated": True, "full_ios_app_runtime_passed": False,
              "firmware": inspection["firmware"]["version"], "cases": completed}
    atomic_json(out / "results.json", record)
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
