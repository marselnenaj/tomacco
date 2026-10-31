#!/usr/bin/env python3
"""Advance the actual QEMU Exclave MMIO counter with a deterministic clock."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time

from ios_dart_integration import connect


def exercise(engine):
    samples = []
    with tempfile.TemporaryDirectory(prefix="tomacco-timebase-") as temporary:
        root = Path(temporary)
        command = [str(engine), "-machine", "tomacco-apple-cpu-test", "-accel", "qtest",
                   "-nodefaults", "-display", "none", "-S",
                   "-qtest", f"unix:{root}/test,server=on,wait=off", "-qtest-log", "/dev/null",
                   "-qmp", f"unix:{root}/monitor,server=on,wait=off"]
        with (root / "stderr").open("wb") as errors:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=errors, stderr=errors)
            try:
                with connect(root / "monitor", process) as monitor, connect(root / "test", process) as test:
                    qmp, qtest = monitor.makefile("rwb", buffering=0), test.makefile("rwb", buffering=0)
                    assert "QMP" in json.loads(qmp.readline())

                    def request(name):
                        qmp.write((json.dumps({"execute": name}) + "\n").encode())
                        while True:
                            result = json.loads(qmp.readline())
                            if "event" in result:
                                continue
                            assert "return" in result, result
                            return result["return"]

                    def bus(command):
                        qtest.write((command + "\n").encode())
                        result = qtest.readline().decode().split()
                        assert result and result[0] == "OK", result
                        return int(result[1], 0) if len(result) > 1 else None

                    request("qmp_capabilities")
                    # The firmware-free board sets CNTFRQ to exactly 1 MHz.
                    for ns in (0, 999, 1000, 1000000000, (2**32 + 17) * 1000):
                        assert bus(f"clock_set {ns}") == ns
                        count = bus("readq 0x10000000")
                        assert count == ns // 1000, (ns, count)
                        samples.append({"virtual_ns": ns, "counter": count})
                    before = bus("readq 0x10000000")
                    time.sleep(.02)
                    assert bus("readq 0x10000000") == before
                    bus("writeq 0x10000000 0xffffffffffffffff")
                    assert bus("readq 0x10000000") == before
                    request("system_reset")
                    assert bus("readq 0x10000000") == before
                    request("quit")
                    process.wait(timeout=5)
                    qmp.close(); qtest.close()
                    assert process.returncode == 0
            except Exception as error:
                raise RuntimeError(str(error) + "\n" + (root / "stderr").read_text(errors="replace")[-2000:]) from error
            finally:
                if process.poll() is None:
                    process.kill(); process.wait(timeout=5)
    with engine.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"scope": "real QEMU timebase MMIO, deterministic virtual time, no firmware",
            "engine_sha256": checksum, "samples": samples, "passed": True,
            "paused_clock_preserved": True, "writes_cannot_change_counter": True,
            "cpu_reset_does_not_reset_system_time": True, "graphical_apps_qualified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = exercise(args.engine.resolve(strict=True))
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")
