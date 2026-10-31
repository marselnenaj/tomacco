#!/usr/bin/env python3
"""Verify opt-in bus-fault traces against real ARM64 exceptions, no firmware."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import time

from ios_dart_integration import connect

FIXTURE = Path(__file__).resolve().parent / "fixtures/ios_bus_fault_test.S"
EVENT = re.compile(r"tomacco_arm_bus_fault pc=0x([0-9a-f]+) physical=0x([0-9a-f]+) virtual=0x([0-9a-f]+) size=([0-9]+) access=([0-9]+) mmu=([0-9]+) response=([0-9]+)")


def exercise(engine, clang, linker):
    observations = []
    with tempfile.TemporaryDirectory(prefix="tomacco-bus-fault-") as temporary:
        root = Path(temporary)
        executable = root / "guest.elf"
        subprocess.run([str(clang), "--target=aarch64-none-elf", "-nostdlib",
                        "--ld-path=" + str(linker), "-Wl,--image-base=0,-Ttext=0x10000,-e,_start",
                        str(FIXTURE), "-o", str(executable)],
                       check=True, capture_output=True, text=True, timeout=30)
        for enabled in (False, True):
            monitor_path, test_path = root / f"monitor-{enabled}", root / f"test-{enabled}"
            command = [str(engine), "-machine", "tomacco-apple-cpu-test", "-m", "64M",
                       "-nodefaults", "-display", "none", "-S",
                       "-device", f"loader,file={executable},cpu-num=0",
                       "-qtest", f"unix:{test_path},server=on,wait=off", "-qtest-log", "/dev/null",
                       "-qmp", f"unix:{monitor_path},server=on,wait=off"]
            if enabled:
                command += ["-trace", "enable=tomacco_arm_bus_fault"]
            error_log = root / f"stderr-{enabled}"
            with error_log.open("wb") as errors:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=errors, stderr=errors)
                try:
                    with connect(monitor_path, process) as monitor, connect(test_path, process) as test:
                        qmp, qtest = monitor.makefile("rwb", buffering=0), test.makefile("rwb", buffering=0)
                        assert "QMP" in json.loads(qmp.readline())

                        def request(name):
                            qmp.write((json.dumps({"execute": name}) + "\n").encode())
                            while True:
                                response = json.loads(qmp.readline())
                                if "event" in response:
                                    continue
                                assert "return" in response, response
                                return response["return"]

                        request("qmp_capabilities")
                        request("cont")
                        deadline = time.monotonic() + 5
                        while True:
                            qtest.write(b"read 0x50000 64\n")
                            reply = qtest.readline().decode().split()
                            assert len(reply) == 2 and reply[0] == "OK", reply
                            result = struct.unpack("<8Q", bytes.fromhex(reply[1][2:]))
                            if result[0] or time.monotonic() > deadline:
                                break
                            time.sleep(.01)
                        request("stop")
                        request("quit")
                        process.wait(timeout=5)
                        qmp.close(); qtest.close()
                        assert process.returncode == 0
                        assert result[:2] == (0xc14e, 2), result
                        assert result[2] >> 26 == result[4] >> 26 == 0x25, result
                        assert (result[2] & 0x7f, result[4] & 0x7f) == (0x10, 0x50), result
                        assert (result[3], result[5]) == (0x20001000, 0x20002000), result
                    log = error_log.read_text(errors="replace")
                    events = [EVENT.fullmatch(line) for line in log.splitlines() if "tomacco_arm_bus_fault" in line]
                    assert len(events) == (2 if enabled else 0) and all(events), log
                    decoded = []
                    for event, far, pc, size, access in zip(events, (result[3], result[5]), result[6:], (4, 8), (0, 1)):
                        values = [int(value, 16 if i < 3 else 10) for i, value in enumerate(event.groups())]
                        assert values[:5] == [pc, far, far, size, access], values
                        assert values[6] == 2, values  # MEMTX_DECODE_ERROR
                        decoded.append(dict(zip(("pc", "physical", "virtual", "size", "access", "mmu", "response"), values)))
                    observations.append({"trace_enabled": enabled, "guest_result": list(result), "events": decoded})
                except Exception as error:
                    raise RuntimeError(str(error) + "\n" + error_log.read_text(errors="replace")[-3000:]) from error
                finally:
                    if process.poll() is None:
                        process.kill(); process.wait(timeout=5)
    assert observations[0]["guest_result"] == observations[1]["guest_result"]
    with engine.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"scope": "real ARM64 bus faults with tracing enabled and disabled, no Apple firmware",
            "engine_sha256": checksum, "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            "observations": observations, "guest_fault_behavior_preserved": True,
            "passed": True, "graphical_apps_qualified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--clang", type=Path, default=shutil.which("clang"))
    parser.add_argument("--linker", type=Path, default=shutil.which("ld.lld"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.clang or not args.linker:
        parser.error("Supply Clang and an LLD ELF linker")
    result = exercise(args.engine.resolve(strict=True), args.clang.resolve(strict=True), args.linker.absolute())
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")
