#!/usr/bin/env python3
"""Read real IOMedia/APFS data through the public API; never qualify full iOS."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import time

from native_client import atomic_json, dispatch, jobs


def sha256(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--firmware", type=Path, required=True,
                        help="Prepared restore shell containing /tomacco/storage-probe")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    os.environ["TOMACCO_HOME"] = str(out / "state")
    arguments = {"engine": str(args.engine.resolve()), "firmware": str(args.firmware.resolve())}
    inspected = dispatch("ios.emulator.inspect", arguments)
    assert inspected["capabilities"]["boot_media"], inspected
    assert not inspected["capabilities"]["persistent_storage"]
    payload = next(p for p in inspected["firmware"]["payload"] if p["path"] == "/tomacco/storage-probe")
    assert payload["entitlements"] == {"com.apple.private.security.disk-device-access": True}
    inputs = {Path(c["path"]): c["sha256"] for c in inspected["firmware"]["components"].values()
              if c["required_for_probe"]}
    image = Path(inspected["firmware"]["components"]["ramdisk"]["path"])
    with image.open("rb") as file:
        expected = file.read(4096)
    assert expected[32:36] == b"NXSB"
    runs = []
    for enabled in (False, True):
        script = "set -e\nset -o pipefail\n"
        if enabled:
            # Access requires the native program's declared disk entitlement.
            script += "if head -c 4096 /dev/rdisk0 >/dev/null 2>&1; then exit 90; fi\n"
        script += "/tomacco/storage-probe\n"
        extra = {"boot_media": True, "boot_display": {"width": 1179, "height": 2556}} if enabled else {}
        job = dispatch("ios.emulator.exec", {**arguments, **extra, "script": script, "seconds": 40})
        try:
            deadline = time.monotonic() + 100
            while True:
                result = jobs.wait(job["id"], 30)
                if result["state"] in jobs.TERMINAL:
                    break
                assert time.monotonic() < deadline, result
            folder = Path(job["report_directory"])
            report = json.loads((folder / "report.json").read_text())
            output = (folder / "guest-output.bin").read_text()
            assert report["boot_media_requested"] is enabled
            assert report["guest_boot_verified"] and report["guest_shell_verified"]
            assert not report["persistent_storage_verified"] and not report["full_ios_boot_verified"]
            assert not report["qualified_for_app_development"]
            if enabled:
                assert result["state"] == "succeeded" and report["guest_exit_code"] == 0, output
                block = re.search(r"^TOMACCO_STORAGE_BLOCK ([0-9a-f]{8192})$", output, re.M)
                assert block and bytes.fromhex(block[1]) == expected
                media = [plistlib.loads(xml.encode()) for xml in re.findall(
                    r"TOMACCO_IOMEDIA_BEGIN [^\n]*\n(.*?)TOMACCO_IOMEDIA_END", output, re.S)]
                by_name = {entry.get("BSD Name"): entry for entry in media}
                assert {"disk0", "disk1", "disk1s1"} <= by_name.keys(), by_name
                assert by_name["disk0"]["Size"] == image.stat().st_size, by_name["disk0"]
                assert report["frame_provenance"] == "guest-ram-boot-framebuffer"
                assert report["boot_framebuffer_changed"]
                assert "boot-media=on" in json.loads((folder / "command.json").read_text())[2]
            else:
                assert result["state"] == "failed" and report["guest_exit_code"] == 2, output
                assert "TOMACCO_STORAGE iomedia_count=0" in output
                assert "TOMACCO_STORAGE open_error=2" in output
                assert "TOMACCO_STORAGE_BLOCK" not in output
                by_name = {}
            request = json.loads((folder / "request.json").read_text())
            assert not Path(request["machine_directory"]).exists()
            assert not Path(request["socket_directory"]).exists()
            runs.append({"boot_media": enabled, "job": job["id"], "guest_exit_code": report["guest_exit_code"],
                         "report": str(folder / "report.json"), "devices": sorted(by_name)})
        finally:
            if jobs.get(job["id"])["state"] not in jobs.TERMINAL:
                jobs.cancel(job["id"])
                jobs.wait(job["id"], 10)
    assert all(sha256(file) == expected_hash for file, expected_hash in inputs.items())
    result = {"state": "passed", "scope": "real-ios-iomedia-registration-and-read",
              "input_files_unchanged": True, "guest_block_matches_source": True,
              "block_sha256": hashlib.sha256(expected).hexdigest(), "read_bytes": len(expected),
              "payload_entitlements_preserved": True, "macos_used": False,
              "persistent_storage_qualified": False, "full_ios_boot_qualified": False, "runs": runs}
    atomic_json(out / "results.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
