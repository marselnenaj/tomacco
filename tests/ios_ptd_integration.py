#!/usr/bin/env python3
"""Exercise PTD message/IRQ semantics and real ARM faults without Apple firmware."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time

from ios_bus_fault_integration import EVENT
from ios_dart_integration import connect

FIXTURE = Path(__file__).resolve().parent / "fixtures/ios_ptd_fault_test.S"
BASE = 0x3003c0000


def exercise(engine, clang, linker):
    observations = []
    with tempfile.TemporaryDirectory(prefix="tomacco-ptd-qtest-") as temporary:
        root = Path(temporary)
        executable = root / "guest.elf"
        subprocess.run([str(clang), "--target=aarch64-none-elf", "-nostdlib",
                        "--ld-path=" + str(linker), "-Wl,--image-base=0,-Ttext=0x10000,-e,_start",
                        str(FIXTURE), "-o", str(executable)],
                       check=True, capture_output=True, text=True, timeout=30)
        for tracing in (False, True):
            monitor_path, test_path = root / f"monitor-{tracing}", root / f"test-{tracing}"
            command = [str(engine), "-machine", "tomacco-ptd-test", "-m", "64M",
                       "-nodefaults", "-display", "none", "-S",
                       "-device", f"loader,file={executable},cpu-num=0",
                       "-qtest", f"unix:{test_path},server=on,wait=off", "-qtest-log", "/dev/null",
                       "-qmp", f"unix:{monitor_path},server=on,wait=off"]
            if tracing:
                command += ["-trace", "enable=tomacco_ptd_mmio", "-trace", "enable=tomacco_arm_bus_fault"]
            error_log = root / f"stderr-{tracing}"
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

                        interrupts = {}

                        def bus(command):
                            qtest.write((command + "\n").encode())
                            while True:
                                reply = qtest.readline().decode().strip().split()
                                if reply and reply[0] == "IRQ":
                                    interrupts[int(reply[2])] = reply[1] == "raise"
                                    continue
                                assert reply and reply[0] == "OK", reply
                                return reply[1] if len(reply) > 1 else None

                        def read(reg, size="q"):
                            return int(bus(f"read{size} {BASE + reg:#x}"), 16)

                        def write(reg, value, size="q"):
                            bus(f"write{size} {BASE + reg:#x} {value:#x}")

                        def asserted():
                            return {line for line, raised in interrupts.items() if raised}

                        request("qmp_capabilities")
                        bus("irq_intercept_out /machine/ptd sysbus-irq")
                        for entry in range(512):
                            assert read(entry * 16) == read(entry * 16 + 8) == 0
                            write(0x10000 + entry * 8, 0x1234567800000000 + entry)
                        for word in range(16):
                            assert read(0x18000 + word * 4, "l") == 0xffffffff
                        for word in range(0, 16, 2):
                            assert read(0x18000 + word * 4) == 0xffffffffffffffff
                        assert not asserted(), "Pending events must remain masked by default"
                        for entry in range(0, 512, 4):
                            register = 0x18400 + (entry // 8) * 4 + ((entry // 4) % 2) * 0x200
                            route = sum(((entry + i) % 32) << (i * 8) for i in range(4))
                            write(register, route, "l")
                            assert read(register, "l") == route
                        for word in range(16):
                            write(0x18200 + word * 4, 0xffffffff, "l")
                        assert asserted() == set(range(32))
                        for entry in range(512):
                            assert read(entry * 16) == 0x1234567800000000 + entry
                            metadata = read(entry * 16 + 8)
                            assert metadata & 3 == 2, (entry, metadata)
                            assert read(entry * 16 + 8) == metadata & ~3
                            # Many entries share a line. Reading one must not
                            # clear a line with another unconsumed message.
                            remaining = set(range(32)) if entry < 480 else set(range(entry - 479, 32))
                            assert asserted() == remaining, (entry, asserted())
                        for word in range(16):
                            assert read(0x18000 + word * 4, "l") == 0
                        write(0x10000, 0xfeed)
                        write(0x10000, 0)
                        assert asserted() == {0}
                        write(0x18400, 0x0302011f, "l")
                        assert asserted() == {31}, "Rerouting must lower the previous line"
                        write(0x18300, 1, "l")
                        assert not asserted() and read(0x18000, "l") == 1
                        write(0x18200, 1, "l")
                        assert asserted() == {31}
                        assert read(0) == 0 and read(8) & 3 == 3
                        assert not asserted() and read(8) & 3 == 0
                        write(0x10000 + 511 * 8, 0xaabb)
                        assert asserted() == {31}
                        request("system_reset")
                        for entry in range(512):
                            assert read(entry * 16) == read(entry * 16 + 8) == 0
                        for word in range(16):
                            assert read(0x18000 + word * 4, "l") == 0
                        for index in range(64):
                            assert read(0x18400 + index * 4, "l") == 0
                            assert read(0x18600 + index * 4, "l") == 0
                        assert not asserted()
                        # Another reset makes the bounded MMIO trace available
                        # for the guest, independently of the long QTest phase.
                        request("system_reset")
                        request("cont")
                        deadline = time.monotonic() + 5
                        while True:
                            result = struct.unpack("<6Q", bytes.fromhex(bus("read 0x50000 48")[2:]))
                            if result[0] or time.monotonic() > deadline:
                                break
                            time.sleep(.01)
                        request("stop")
                        assert not asserted(), "Reset must also clear notification enables"
                        request("quit")
                        process.wait(timeout=5)
                        qmp.close(); qtest.close()
                        assert process.returncode == 0
                        assert result[:4] == (0x5054, 15, 3, 1), result
                    log = error_log.read_text(errors="replace")
                    events = [EVENT.fullmatch(line) for line in log.splitlines() if "tomacco_arm_bus_fault" in line]
                    assert len(events) == (13 if tracing else 0) and all(events), log[-8000:]
                    expected = [(0x1b18, 4, 0), (0x1b18, 2, 0), (0x1b10, 8, 1),
                                (0x10d88, 4, 1), (0x10d88, 1, 1), (0x10d88, 8, 0),
                                (0x2000, 8, 0), (0x18040, 4, 0), (0x18000, 4, 1),
                                (0x18400, 8, 1), (0x18400, 4, 1), (0x18500, 4, 1), (0x18200, 4, 0)]
                    decoded = []
                    for event, (offset, size, access) in zip(events, expected):
                        values = [int(v, 16 if i < 3 else 10) for i, v in enumerate(event.groups())]
                        assert values[1:5] == [BASE + offset, BASE + offset, size, access], values
                        assert values[6] == 2, values
                        decoded.append(dict(zip(("pc", "physical", "virtual", "size", "access", "mmu", "response"), values)))
                    observations.append({"trace_enabled": tracing, "guest_result": list(result[:4]), "faults": decoded})
                except Exception as error:
                    raise RuntimeError(str(error) + "\n" + error_log.read_text(errors="replace")[-6000:]) from error
                finally:
                    if process.poll() is None:
                        process.kill(); process.wait(timeout=5)
    assert observations[0]["guest_result"] == observations[1]["guest_result"]
    with engine.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"scope": "PTD transport, IRQ output, ARM counter and exceptions; no Apple firmware",
            "engine_sha256": checksum, "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            "observations": observations, "entries": 512, "interrupt_outputs": 32,
            "masked_pending_preserved": True, "overwrite_detected": True,
            "all_routing_fields_qualified": True, "read_consumes_event": True,
            "reset_passed": True, "timestamp_uses_guest_counter": True,
            "passed": True, "pmp_firmware_executed": False,
            "soc_interrupt_wiring_qualified": False, "graphical_apps_qualified": False}


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
