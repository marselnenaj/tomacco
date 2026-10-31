#!/usr/bin/env python3
"""Qualify real file-backed restore-shell jobs through MCP with local firmware.

Requires a prepared storage-probe guest. Does not qualify a graphical iPhone,
encryption, APFS crash recovery, or guest writes across restarts.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


def digest(file):
    with file.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, default=Path("build-product/tomaccoctl"))
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cli, engine, firmware = (p.expanduser().resolve(strict=True) for p in (args.cli, args.engine, args.firmware))
    output = args.output.expanduser().absolute()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    environment = {**os.environ, "TOMACCO_HOME": str(output / "state")}

    def call(name, arguments):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "tomacco-working-image-check", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": name, "arguments": arguments}}]
        completed = subprocess.run([str(cli), "mcp"], env=environment, text=True,
                                   input="".join(json.dumps(r) + "\n" for r in requests),
                                   capture_output=True, timeout=90)
        assert completed.returncode == 0, completed.stderr
        responses = {r["id"]: r for r in map(json.loads, completed.stdout.splitlines())}
        assert "result" in responses[1] and "result" in responses[2], responses
        result = responses[2]["result"]
        assert not result.get("isError"), result
        return json.loads(result["content"][0]["text"])

    common = {"engine": str(engine), "firmware": str(firmware)}
    inspection = call("ios.emulator.inspect", common)
    assert inspection["capabilities"]["file_backed_boot_media"], inspection
    source = Path(inspection["firmware"]["components"]["ramdisk"]["path"])
    source_hash = digest(source)
    with source.open("rb") as stream:
        expected = stream.read(4096)
    working = output / "working, with spaces.apfs"
    subprocess.run(["cp", "--reflink=auto", "--", str(source), str(working)], check=True)
    working.chmod(0o600)
    records = []
    terminal = {"succeeded", "failed", "timed_out", "cancelled"}
    for iteration in range(2):
        job = call("ios.emulator.exec", {**common, "boot_media": True,
                   "working_image": str(working), "seconds": 40,
                   "script": "set -e\n/tomacco/storage-probe\n"})
        try:
            deadline = time.monotonic() + 110
            while True:
                result = call("job.wait", {"job_id": job["id"], "seconds": 30})
                if result["state"] in terminal:
                    break
                assert time.monotonic() < deadline, result
            folder = Path(job["report_directory"])
            report = json.loads((folder / "report.json").read_text())
            assert result["state"] == "succeeded", report
            assert report["guest_exit_code"] == 0 and report["guest_shell_verified"]
            block = re.search(r"^TOMACCO_STORAGE_BLOCK ([0-9a-f]{8192})$", report["guest_output"], re.M)
            assert block and bytes.fromhex(block[1]) == expected
            assert report["working_image_flushed"]
            assert report["working_image_after"]["sha256"] == digest(working)
            assert not report["persistent_storage_verified"] and not report["full_ios_boot_verified"]
            request = json.loads((folder / "request.json").read_text())
            assert not Path(request["machine_directory"]).exists()
            assert not Path(request["socket_directory"]).exists()
            with working.open("r+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            records.append({"iteration": iteration, "job": job["id"], "report": str(folder / "report.json"),
                            "read_bytes": len(expected), "working_image_flushed": True,
                            "temporary_resources_removed": True, "working_image_retained_unlocked": True})
        finally:
            if call("job.get", {"job_id": job["id"]})["state"] not in terminal:
                call("job.cancel", {"job_id": job["id"]})
                call("job.wait", {"job_id": job["id"], "seconds": 10})
    assert digest(source) == source_hash
    record = {"state": "passed", "transport": "mcp-stdio", "cli": str(cli), "cli_sha256": digest(cli),
              "source_preserved": True, "source_sha256": source_hash, "working_image": str(working),
              "runs": records, "full_ios_boot_qualified": False, "cross_restart_guest_writes_qualified": False}
    (output / "results.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
