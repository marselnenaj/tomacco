#!/usr/bin/env python3
"""Exercise the ASC transport and real ARM IRQ/abort paths without Apple firmware."""
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

FIXTURE = Path(__file__).resolve().parent / "fixtures/ios_asc_mailbox_test.S"
AP, IOP = 0x300e08000, 0x300e04000


def exercise(engine, clang, linker):
    observations = []
    with tempfile.TemporaryDirectory(prefix="tomacco-asc-qtest-") as temporary:
        root = Path(temporary)
        executable = root / "guest.elf"
        compiled = subprocess.run([str(clang), "--target=aarch64-none-elf", "-nostdlib",
                                   "--ld-path=" + str(linker), "-Wl,--image-base=0,-Ttext=0x10000,-e,_start",
                                   str(FIXTURE), "-o", str(executable)],
                                  capture_output=True, text=True, timeout=30)
        if compiled.returncode:
            raise RuntimeError("ARM fixture compilation failed:\n" + compiled.stderr)
        for tracing, intercepted in ((False, True), (True, True), (False, False), (True, False)):
            monitor_path, test_path = root / f"monitor-{tracing}-{intercepted}", root / f"test-{tracing}-{intercepted}"
            command = [str(engine), "-machine", "tomacco-asc-mailbox-test", "-m", "64M",
                       "-nodefaults", "-display", "none", "-S",
                       "-device", f"loader,file={executable},cpu-num=0",
                       "-qtest", f"unix:{test_path},server=on,wait=off", "-qtest-log", "/dev/null",
                       "-qmp", f"unix:{monitor_path},server=on,wait=off"]
            if tracing:
                command += ["-trace", "enable=tomacco_asc_mailbox_mmio", "-trace", "enable=tomacco_arm_bus_fault"]
            error_log = root / f"stderr-{tracing}-{intercepted}"
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

                        def read(view, reg, size="q"):
                            return int(bus(f"read{size} {view + reg:#x}"), 16)

                        def write(view, reg, value, size="q"):
                            bus(f"write{size} {view + reg:#x} {value:#x}")

                        def assert_lines(expected):
                            if intercepted:
                                assert {line for line, raised in interrupts.items() if raised} == expected, interrupts

                        request("qmp_capabilities")
                        # QTest output interception disconnects the CPU. The
                        # ARM IRQ test therefore runs in a separate process.
                        if intercepted:
                            bus("irq_intercept_out /machine/asc sysbus-irq")
                        for view in (AP, IOP):
                            assert read(view, 0x110, "l") == read(view, 0x114, "l") == 1 << 17
                        assert_lines({0, 2})
                        for cycle in range(8):
                            for direction, view in enumerate((AP, IOP)):
                                offset = 0x800 + direction * 0x20
                                for i in range(8):
                                    write(view, offset, (cycle << 40) + (direction << 32) + i)
                                    write(view, offset + 8, 0xffffffff - i)
                                status = read(view, 0x110 + direction * 4, "l")
                                assert status & (1 << 16) and status >> 20 == 8
                            assert_lines({1, 3})
                            for i in range(8):
                                for direction, view in enumerate((IOP, AP)):
                                    offset = 0x810 + direction * 0x20
                                    expected = (cycle << 40) + (direction << 32) + i
                                    assert read(view, offset) == read(view, offset) == expected
                                    metadata = read(view, offset + 8)
                                    assert metadata & 0xffffffff == 0xffffffff - i
                                    assert metadata >> 52 == 8 - i
                            assert_lines({0, 2})
                        # A reset drops queued and staged messages and restores
                        # the raw empty IRQ levels. It must not report a reply.
                        write(AP, 0x800, 0xaaaa)
                        write(AP, 0x808, 0xbbbb)
                        write(IOP, 0x820, 0xcccc)
                        request("system_reset")
                        assert read(AP, 0x110, "l") == read(AP, 0x114, "l") == 1 << 17
                        assert_lines({0, 2})
                        result, expected, faults = (), [], []
                        if not intercepted:
                            write(AP, 0x800, 0x123456789abcdef0)
                            write(AP, 0x808, 0x1234)
                            request("cont")
                            deadline = time.monotonic() + 5
                            while True:
                                result = struct.unpack("<48Q", bytes.fromhex(bus("read 0x50000 384")[2:]))
                                if result[0] or time.monotonic() > deadline:
                                    break
                                time.sleep(.01)
                            request("stop")
                            assert result[:6] == (0xa5c0, 10, 1, 4,
                                                  0x123456789abcdef0, 0x123456789abcdef1), result
                            assert_lines({0, 2})
                            expected = [(AP + 0x810, 8, 0), (AP + 0x830, 4, 0),
                                        (AP + 0x800, 1, 1), (AP + 0x830, 8, 1),
                                        (AP + 0x110, 8, 0), (AP + 0x110, 4, 1),
                                        (AP + 0x808, 8, 1), (0x300e00040, 4, 0),
                                        (AP + 0x808, 8, 1), (AP + 0x808, 8, 1)]
                            faults = [result[index:index + 3] for index in range(16, 46, 3)]
                            for (esr, far, pc), (physical, _, access) in zip(faults, expected):
                                assert esr >> 26 == 0x25 and esr & 0x7f == 0x10 + access * 0x40
                                assert far == physical and 0x10000 <= pc < 0x11000
                        request("quit")
                        process.wait(timeout=5)
                        qmp.close(); qtest.close()
                        assert process.returncode == 0
                    log = error_log.read_text(errors="replace")
                    events = [EVENT.fullmatch(line) for line in log.splitlines() if "tomacco_arm_bus_fault" in line]
                    assert len(events) == (10 if tracing and not intercepted else 0) and all(events), log[-6000:]
                    assert ("tomacco_asc_mailbox_mmio" in log) == tracing
                    decoded = []
                    for event, (address, size, access), (_, _, pc) in zip(events, expected, faults):
                        values = [int(v, 16 if i < 3 else 10) for i, v in enumerate(event.groups())]
                        assert values[:5] == [pc, address, address, size, access], values
                        assert values[6] == 2, values
                        decoded.append(dict(zip(("pc", "physical", "virtual", "size", "access", "mmu", "response"), values)))
                    observations.append({"trace_enabled": tracing, "irq_outputs_intercepted": intercepted, "guest_result": list(result[:6]), "faults": decoded})
                except Exception as error:
                    raise RuntimeError(str(error) + "\n" + error_log.read_text(errors="replace")[-6000:]) from error
                finally:
                    if process.poll() is None:
                        process.kill(); process.wait(timeout=5)
    assert observations[2]["guest_result"] == observations[3]["guest_result"]
    with engine.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"scope": "ASC queues, raw IRQ levels, ARM EL1 IRQ and bus faults; no Apple firmware",
            "engine_sha256": checksum, "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            "observations": observations, "fifo_model_depth": 8, "bidirectional_transfer_verified": True,
            "level_interrupts_verified": True, "real_arm_irq_delivery_verified": True,
            "rejected_accesses_preserve_messages": True, "reset_passed": True, "passed": True,
            "silicon_reset_state_measured": False, "soc_interrupt_wiring_qualified": False,
            "pmp_controller_boot_qualified": False, "graphical_apps_qualified": False}


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
