#!/usr/bin/env python3
"""Qualify local kernel diagnostics or actual iOS restore-shell execution.

This accepts local firmware. It does not download or distribute Apple software.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
from native_client import dispatch, jobs, atomic_json

def check_cancellation(arguments, out):
    job = dispatch("ios.emulator.exec", {**arguments, "script": "sleep 90\n", "seconds": 120})
    folder = Path(job["report_directory"])
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            console = folder / "console.log"
            if console.exists() and "/bin/sleep" in console.read_text(errors="replace"):
                break
            time.sleep(.05)
        else:
            raise AssertionError("Guest did not start the cancellable sleep command")
        with sqlite3.connect(f"file:{out / 'state/jobs.sqlite3'}?mode=ro", uri=True) as db:
            pid = db.execute("SELECT pid FROM jobs WHERE id=?", (job["id"],)).fetchone()[0]
        processes = []
        def record(pid):
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            processes.append((pid, stat[19]))
            for child in Path(f"/proc/{pid}/task/{pid}/children").read_text().split():
                record(int(child))
        record(pid)
        assert len(processes) >= 3, processes
        jobs.cancel(job["id"])
        assert jobs.wait(job["id"], 10)["state"] == "cancelled"
        for pid, token in processes:
            file = Path(f"/proc/{pid}/stat")
            if file.exists():
                stat = file.read_text().rsplit(")", 1)[1].split()
                assert stat[0] == "Z" or stat[19] != token, (pid, stat[0])
        request = json.loads((folder / "request.json").read_text())
        assert not Path(request["socket_directory"]).exists()
        assert not Path(request["machine_directory"]).exists()
        shutil.copyfile(folder / "console.log", out / "cancellation-console.log")
        result = {"live_guest_cancelled": True, "processes_terminated": len(processes), "owned_directories_removed": True}
        atomic_json(out / "cancellation.json", result)
        return result
    finally:
        if jobs.get(job["id"])["state"] not in jobs.TERMINAL:
            jobs.cancel(job["id"])
            jobs.wait(job["id"], 10)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--firmware", required=True)
    parser.add_argument("--output", default="qa/ios-emulator/acceptance")
    parser.add_argument("--variant", choices=["research", "release"], default="release")
    args = parser.parse_args()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    os.environ["TOMACCO_HOME"] = str(out / "state")
    arguments = {"engine": str(Path(args.engine).resolve()), "firmware": str(Path(args.firmware).resolve()), "variant": args.variant}
    inspected = dispatch("ios.emulator.inspect", arguments)
    atomic_json(out / "inspection.json", inspected)
    assert not inspected["qualified_for_app_development"]
    assert not inspected["macos_vm_required"]
    is_sptm = inspected["backend"] == "sptm"
    job = dispatch("ios.emulator.probe", {**arguments, "seconds": 30})
    try:
        result = jobs.wait(job["id"], 60)
        assert result["state"] in jobs.TERMINAL, result
        folder = Path(job["report_directory"])
        report = json.loads((folder / "report.json").read_text())
        assert report["program_counter_changed"], {key: report.get(key) for key in ("state", "error", "guest_exit_code", "guest_output")}
        assert not report["macos_vm_used"]
        assert not report["qualified_for_app_development"]
        assert not report["full_ios_boot_verified"]
        if is_sptm:
            assert result["state"] == "succeeded", {key: report.get(key) for key in ("state", "error", "guest_exit_code", "guest_output")}
            assert report["guest_shell_verified"] and report["guest_boot_verified"]
            assert "uid=0(root)" in report["guest_output"]
            assert "Darwin Kernel Version" in report["guest_output"]
            assert inspected["firmware"]["version"] in report["guest_output"]
            assert inspected["firmware"]["build"] in report["guest_output"]
        else:
            assert not report["guest_boot_verified"]
        request = json.loads((folder / "request.json").read_text())
        assert not Path(request["machine_directory"]).exists()
        assert not Path(request["socket_directory"]).exists()
        for name in ("report.json", "command.json", "engine.log", "console.log", "frame.png"):
            if (folder / name).exists():
                shutil.copyfile(folder / name, out / name)
        atomic_json(out / "results.json", {
            "control_plane_passed": True, "full_ios_app_runtime_passed": False,
            "restore_shell_passed": is_sptm,
            "observed_state": report["state"], "job_state": result["state"],
            "firmware": report["firmware"]["version"], "program_counter_changed": True,
            "report": str(out / "report.json"), "owned_disks_cleaned": True,
        })
        print((out / "results.json").read_text())
    finally:
        if jobs.get(job["id"])["state"] not in jobs.TERMINAL:
            jobs.cancel(job["id"])
            jobs.wait(job["id"], 10)

    if is_sptm:
        for name, script, expected_code, expected_output in [
            ("unicode", "set -e\nprintf '%s\\n' 'Grüße 日本語 😀 $HOME `literal`'\nuname -m\n", 0, "Grüße 日本語 😀 $HOME `literal`"),
            ("failure", "printf 'expected-error\\n'\nexit 7\n", 7, "expected-error"),
            ("long-script", "# " + "x" * 24000 + "\nprintf 'long-script-complete\\n'\n", 0, "long-script-complete"),
            ("binary", "printf '\\000\\377\\200\\012'\n", 0, ""),
        ]:
            job = dispatch("ios.emulator.exec", {**arguments, "script": script, "seconds": 30})
            try:
                result = jobs.wait(job["id"], 60)
                folder = Path(job["report_directory"])
                report = json.loads((folder / "report.json").read_text())
                assert result["state"] == ("succeeded" if expected_code == 0 else "failed"), {key: report.get(key) for key in ("state", "error", "guest_exit_code", "guest_output")}
                assert report["guest_exit_code"] == expected_code, {key: report.get(key) for key in ("state", "error", "guest_exit_code", "guest_output")}
                assert expected_output in report["guest_output"], {key: report.get(key) for key in ("state", "error", "guest_exit_code", "guest_output")}
                assert report["guest_shell_verified"] and not report["full_ios_boot_verified"]
                if name == "binary":
                    assert (folder / "guest-output.bin").read_bytes() == bytes([0, 255, 128, 10])
                    assert not report["guest_output_utf8_valid"]
                else:
                    assert report["guest_output_utf8_valid"]
                request = json.loads((folder / "request.json").read_text())
                assert not Path(request["socket_directory"]).exists()
                assert not Path(request["machine_directory"]).exists()
                shutil.copyfile(folder / "report.json", out / f"{name}-report.json")
                shutil.copyfile(folder / "console.log", out / f"{name}-console.log")
            finally:
                if jobs.get(job["id"])["state"] not in jobs.TERMINAL:
                    jobs.cancel(job["id"])
                    jobs.wait(job["id"], 10)
        summary = json.loads((out / "results.json").read_text())
        summary.update(unicode_script_passed=True, nonzero_exit_propagated=True, large_script_passed=True, binary_output_exact=True)
        summary.update(check_cancellation(arguments, out))
        summary["acceptance_passed"] = True
        atomic_json(out / "results.json", summary)
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
