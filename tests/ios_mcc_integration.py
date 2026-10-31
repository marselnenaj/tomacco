#!/usr/bin/env python3
"""Run the real MCC device, reset and ARM faults without Apple firmware."""
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

FIXTURE = Path(__file__).resolve().parent / "fixtures/ios_mcc_fault_test.S"


def exercise(engine, clang, linker):
    observations = []
    with tempfile.TemporaryDirectory(prefix="tomacco-mcc-qtest-") as temporary:
        root = Path(temporary)
        executable = root / "guest.elf"
        subprocess.run([str(clang), "--target=aarch64-none-elf", "-nostdlib",
                        "--ld-path=" + str(linker), "-Wl,--image-base=0,-Ttext=0x10000,-e,_start",
                        str(FIXTURE), "-o", str(executable)],
                       check=True, capture_output=True, text=True, timeout=30)
        for tracing in (False, True):
            monitor_path, test_path = root / f"monitor-{tracing}", root / f"test-{tracing}"
            command = [str(engine), "-machine", "tomacco-mcc-test", "-m", "64M",
                       "-nodefaults", "-display", "none", "-S",
                       "-device", f"loader,file={executable},cpu-num=0",
                       "-qtest", f"unix:{test_path},server=on,wait=off", "-qtest-log", "/dev/null",
                       "-qmp", f"unix:{monitor_path},server=on,wait=off"]
            if tracing:
                command += ["-trace", "enable=tomacco_mcc_mmio", "-trace", "enable=tomacco_arm_bus_fault"]
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

                        def bus(command):
                            qtest.write((command + "\n").encode())
                            reply = qtest.readline().decode().strip().split()
                            assert reply and reply[0] == "OK", reply
                            return int(reply[1], 16) if len(reply) > 1 else None

                        request("qmp_capabilities")
                        bus("writel 0x220000010 0x1234")
                        bus("writel 0x220000014 0x5678")
                        assert bus("readl 0x220000010") == 0x1234
                        assert bus("readl 0x220000014") == 0x5678
                        for plane in range(4):
                            control = 0x220002800 + plane * 0x40000
                            assert bus(f"readl {control:#x}") == 0x8000000c
                            for ways in (0, 3, 12):
                                bus(f"writel {control:#x} {ways:#x}")
                                assert bus(f"readl {control + 4:#x}") == ways * 0x1000100
                                # Real coherent RAM survives changing the cache
                                # mode. The controller must not alias its RAM.
                                bus("writel 0x60000 0xabcdef12")
                                assert bus("readl 0x60000") == 0xabcdef12
                            bus(f"writel {control:#x} {plane:#x}")
                        for channel in range(4):
                            config = 0x220401d48 + channel * 0x200000
                            assert bus(f"readl {config:#x}") == 0x12345678
                            bus(f"writel {config:#x} {0xf00d0000 + channel:#x}")
                        for channel in range(4):
                            assert bus(f"readl {0x220401d48 + channel * 0x200000:#x}") == 0xf00d0000 + channel
                            assert bus(f"readl {0x220002804 + channel * 0x40000:#x}") == channel * 0x1000100
                        for plane in range(4):
                            for group in range(32):
                                address = 0x22000a000 + plane * 0x40000 + group * 4
                                assert bus(f"readl {address:#x}") == 0
                                bus(f"writel {address:#x} {(plane * 32 + group) % 64:#x}")
                        for plane in range(4):
                            for group in range(32):
                                address = 0x22000a000 + plane * 0x40000 + group * 4
                                assert bus(f"readl {address:#x}") == (plane * 32 + group) % 64
                        bus("writel 0x220102800 7")
                        bus("writel 0x22010a07c 63")
                        bus("writel 0x22010082c 1")
                        for plane in range(4):
                            assert bus(f"readl {0x220002804 + plane * 0x40000:#x}") == 0x7000700
                            assert bus(f"readl {0x22000a07c + plane * 0x40000:#x}") == 63
                            assert bus(f"readl {0x22000082c + plane * 0x40000:#x}") == 1
                            bus(f"writel {0x22000082c + plane * 0x40000:#x} {plane % 2}")
                        for plane in range(4):
                            assert bus(f"readl {0x22000082c + plane * 0x40000:#x}") == plane % 2
                        request("system_reset")
                        for channel in range(4):
                            assert bus(f"readl {0x220401d48 + channel * 0x200000:#x}") == 0x12345678
                            assert bus(f"readl {0x220002804 + channel * 0x40000:#x}") == 0x0c000c00
                            assert bus(f"readl {0x22000082c + channel * 0x40000:#x}") == 0
                            for group in range(32):
                                assert bus(f"readl {0x22000a000 + channel * 0x40000 + group * 4:#x}") == 0
                        request("cont")
                        deadline = time.monotonic() + 5
                        while True:
                            qtest.write(b"read 0x50000 24\n")
                            reply = qtest.readline().decode().split()
                            assert len(reply) == 2 and reply[0] == "OK", reply
                            result = struct.unpack("<QQII", bytes.fromhex(reply[1][2:]))
                            if result[0] or time.monotonic() > deadline:
                                break
                            time.sleep(.01)
                        request("stop")
                        assert bus("readl 0x60000") == 0xabcdef12
                        assert bus("readl 0x220000010") == 0x1234
                        assert bus("readl 0x220000014") == 0x5678
                        request("quit")
                        process.wait(timeout=5)
                        qmp.close(); qtest.close()
                        assert process.returncode == 0
                        assert result == (0xc14e, 18, 0x8000000c, 0x12345678), result
                    log = error_log.read_text(errors="replace")
                    events = [EVENT.fullmatch(line) for line in log.splitlines() if "tomacco_arm_bus_fault" in line]
                    # One exception is an architectural alignment fault;
                    # it occurs before any transaction reaches the device.
                    assert len(events) == (17 if tracing else 0) and all(events), log
                    decoded = []
                    expected = [(0x220002808, 4, 0), (0x220401d4c, 4, 0),
                                (0x220002804, 4, 1), (0x220002800, 8, 1),
                                (0x220002800, 1, 1), (0x220002800, 2, 1),
                                (0x220002800, 4, 1),
                                (0x220002800, 4, 1), (0x220401d48, 8, 1),
                                (0x22004a028, 4, 1), (0x22004a028, 8, 1),
                                (0x22004a028, 1, 1), (0x22004a080, 4, 0),
                                (0x220000828, 8, 1), (0x22000082c, 1, 1),
                                (0x22010082c, 4, 1), (0x220102800, 4, 1)]
                    for event, (address, size, access) in zip(events, expected):
                        values = [int(v, 16 if i < 3 else 10) for i, v in enumerate(event.groups())]
                        assert values[1:5] == [address, address, size, access], values
                        assert values[6] == 2, values
                        decoded.append(dict(zip(("pc", "physical", "virtual", "size", "access", "mmu", "response"), values)))
                    observations.append({"trace_enabled": tracing, "guest_result": list(result), "faults": decoded})
                except Exception as error:
                    raise RuntimeError(str(error) + "\n" + error_log.read_text(errors="replace")[-6000:]) from error
                finally:
                    if process.poll() is None:
                        process.kill(); process.wait(timeout=5)
    assert observations[0]["guest_result"] == observations[1]["guest_result"]
    with engine.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"scope": "MCC MMIO, real ARM exceptions and QEMU lifecycle; no Apple firmware",
            "engine_sha256": checksum, "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            "observations": observations, "channels_independent": True, "cache_planes_independent": True,
            "dsid_quota_groups_independent": True,
            "broadcast_updates_all_planes": True, "maintenance_gate_independent": True,
            "reset_passed": True, "coherent_ram_preserved": True,
            "existing_ctrr_registers_preserved": True,
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
