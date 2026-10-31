#!/usr/bin/env python3
"""Exercise the public PMP debugger on locally supplied firmware and real QEMU.

No firmware is downloaded or distributed. Success qualifies a diagnostic stop,
not a booted PMP controller, power services or an iOS application runtime.
"""
import argparse
import hashlib
from pathlib import Path

from native_client import atomic_json, dispatch, jobs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True, type=Path)
    parser.add_argument("--firmware", required=True, type=Path)
    parser.add_argument("--stop-at", required=True)
    parser.add_argument("--controller", choices=("none", "asc-v6"), default="none")
    parser.add_argument("--cpu-midr", default="0x610f0000")
    parser.add_argument("--cpu-affinity", default="0x0")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    request = {"engine":str(args.engine.resolve()),"firmware":str(args.firmware.resolve()),
               "stop_at":args.stop_at,"controller":args.controller,"capture_ram":True,
               "cpu_midr":args.cpu_midr,"cpu_affinity":args.cpu_affinity}
    inspect = dispatch("ios.emulator.inspect_pmp", request)
    evidence = {"scope":"pmp-diagnostic-stop-only","inspect":inspect}
    job = None
    try:
        job = dispatch("ios.emulator.pmp", {**request,"seconds":15})
        evidence["job"] = jobs.wait(job["id"],45)
        report = evidence["report"] = dispatch("ios.emulator.report", {"job_id":job["id"]})["report"]
        assert evidence["job"]["state"] == "succeeded", jobs.logs(job["id"])
        assert report["breakpoint_hit"] and report["engine_shutdown_verified"]
        assert report["strict_sysregs_verified"]
        assert report["cpu_identity_verified"]
        assert not report["sysreg_faults"]["events"]
        assert report["source_executable_preserved"]
        address = int(args.stop_at,16)
        assert report["observations"][-1]["pc"] == address
        assert report["observations"][-1]["status"]["running"] is False
        assert report["firmware_instructions_observed"] == (address != inspect["firmware"]["layout"]["entry"])
        for flag in ("pmp_boot_verified","power_services_verified","full_ios_boot_verified","graphical_apps_verified"):
            assert report[flag] is False
        capture = report["memory_capture"]
        assert capture["bytes"] == 2097152 and capture["guest_stopped"]
        assert Path(capture["path"]).stat().st_mode & 0o777 == 0o600
        # Check actual RAM at the breakpoint against the original instruction.
        code = dispatch("ios.emulator.memory", {"job_id":job["id"],"address":args.stop_at,"length":4})
        layout = inspect["firmware"]["layout"]
        segment = next(s for s in layout["segments"] if s["address"] <= address < s["address"]+s["file_bytes"])
        offset = segment["file_offset"] + address - segment["address"]
        payload = args.firmware / inspect["firmware"]["components"]["payload"]["path"]
        assert code["data"] == payload.read_bytes()[offset:offset+4].hex()
        evidence["instruction_preserved"] = True
        evidence["capture_sha256"] = capture["sha256"]
        assert hashlib.sha256(Path(capture["path"]).read_bytes()).hexdigest() == capture["sha256"]
        for directory in job["plan"]["temporary_directories"]:
            assert not Path(directory["path"]).exists()
        assert not Path("/proc",str(report["engine_pid"])).exists()
        evidence["accepted"] = True
        print(f"PMP breakpoint reached: {job['id']} at {args.stop_at}; RAM hash verified; no boot qualification")
    finally:
        if job and jobs.get(job["id"])["state"] not in jobs.TERMINAL:
            jobs.cancel(job["id"])
            jobs.wait(job["id"],15)
        atomic_json(args.output, evidence)


if __name__ == "__main__":
    main()
