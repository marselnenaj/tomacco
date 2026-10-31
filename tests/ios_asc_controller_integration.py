#!/usr/bin/env python3
"""Execute real ARM IRQ/FIQ and timer paths through the experimental ASC/KIC."""
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

FIXTURE = Path(__file__).resolve().parent / "fixtures/ios_asc_controller_test.S"


def exercise(engine, clang, linker):
    observations = []
    with tempfile.TemporaryDirectory(prefix="tomacco-asc-controller-") as temporary:
        root = Path(temporary)
        executable, layout = root / "guest.elf", root / "guest.ld"
        layout.write_text("PHDRS { text PT_LOAD FLAGS(5); }\n"
                          "SECTIONS { . = 0x1000000; .text : { *(.text*) } :text }\n")
        compiled = subprocess.run([str(clang), "--target=aarch64-none-elf", "-nostdlib",
                                   "--ld-path=" + str(linker), "-Wl,-e,_start,-T," + str(layout),
                                   str(FIXTURE), "-o", str(executable)],
                                  capture_output=True, text=True, timeout=30)
        if compiled.returncode:
            raise RuntimeError(compiled.stderr)
        for tracing in (False, True):
            monitor_path, test_path = root / f"monitor-{tracing}", root / f"test-{tracing}"
            command = [str(engine), "-machine", "tomacco-pmp-asc-research", "-m", "2M",
                       "-cpu", "max,midr=0x610f0000", "-nodefaults", "-display", "none", "-S",
                       "-device", f"loader,file={executable},cpu-num=0",
                       "-qtest", f"unix:{test_path},server=on,wait=off", "-qtest-log", "/dev/null",
                       "-qmp", f"unix:{monitor_path},server=on,wait=off"]
            if tracing:
                command += ["-trace", "enable=tomacco_arm_bus_fault", "-trace", "enable=tomacco_asc_controller_mmio"]
            error_log = root / f"stderr-{tracing}"
            with error_log.open("wb") as errors:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=errors, stderr=errors)
                try:
                    with connect(monitor_path, process) as monitor, connect(test_path, process) as test:
                        qmp, qtest = monitor.makefile("rwb", buffering=0), test.makefile("rwb", buffering=0)
                        assert "QMP" in json.loads(qmp.readline())

                        def request(name, arguments=None):
                            qmp.write((json.dumps({"execute": name, "arguments": arguments or {}}) + "\n").encode())
                            while True:
                                response = json.loads(qmp.readline())
                                if "event" in response:
                                    continue
                                assert "return" in response, response
                                return response["return"]

                        def bus(command):
                            qtest.write((command + "\n").encode())
                            reply = qtest.readline().decode().split()
                            assert reply and reply[0] == "OK", reply
                            return reply[1] if len(reply) > 1 else None

                        def snapshot():
                            value = request("qom-get", {"path": "/machine/asc", "property": "controller-state"})
                            assert len(value) < 16384
                            return json.loads(value)

                        request("qmp_capabilities")
                        # No IRQ interception: delivery must reach the actual CPU.
                        bus("writeq 0x300e08800 0x123456789abcdef0")
                        bus("writeq 0x300e08808 0x1234")
                        initial = snapshot()
                        assert initial == snapshot(), "Snapshot mutated controller state"
                        assert initial["queues"][0]["count"] == 1
                        request("cont")
                        deadline = time.monotonic() + 5
                        while True:
                            result = struct.unpack("<25Q", bytes.fromhex(bus("read 0x1100000 200")[2:]))
                            if result[0] or time.monotonic() > deadline:
                                break
                            time.sleep(.01)
                        request("stop")
                        assert result[:6] == (0xa5cc, 4, 2, 3, 4, 7), result
                        assert result[9:15] == (0x10025, 0x10025, 0x101ff, 0x40001, 0x70000, 0x70008), result
                        final = snapshot()
                        assert final == snapshot(), "Snapshot consumed pending output/state"
                        assert final["rejected_accesses"] == 3
                        assert [q["count"] for q in final["queues"]] == [0, 1]
                        assert not any(t["input_asserted"] for t in final["timers"])
                        assert final["outputs"][0]["transitions"] == 8
                        assert final["outputs"][1]["transitions"] == 4
                        assert int(bus("readq 0x300e08830"), 16) == 0x123456789abcdef1
                        assert int(bus("readq 0x300e08838"), 16) & 0xffffffff == 0x4321
                        expected = [(0x300e00044, 4, 0), (0x300e00040, 4, 1), (0x300e04110, 8, 0)]
                        faults = [result[i:i + 3] for i in range(16, 25, 3)]
                        for (esr, far, pc), (address, size, access) in zip(faults, expected):
                            assert esr >> 26 == 0x25 and esr & 0x7f == 0x10 + access * 0x40
                            assert far == address and 0x1000000 <= pc < 0x1001000
                        request("system_reset")
                        reset = snapshot()
                        assert not reset["reads"] and not reset["writes"] and not reset["rejected_accesses"]
                        assert all(q["count"] == 0 and not q["staged"] and not q["enabled"] for q in reset["queues"])
                        assert all(not o["asserted"] and o["next_event"] == 0 for o in reset["outputs"])
                        request("quit")
                        process.wait(timeout=5)
                        qmp.close(); qtest.close()
                        assert process.returncode == 0
                    log = error_log.read_text(errors="replace")
                    events = [EVENT.fullmatch(line) for line in log.splitlines() if "tomacco_arm_bus_fault" in line]
                    assert len(events) == (3 if tracing else 0) and all(events), log[-6000:]
                    assert ("tomacco_asc_controller_mmio" in log) == tracing
                    for event, (address, size, access), (_, _, pc) in zip(events, expected, faults):
                        values = [int(v, 16 if i < 3 else 10) for i, v in enumerate(event.groups())]
                        assert values[:5] == [pc, address, address, size, access] and values[6] == 2, values
                    observations.append({"tracing": tracing, "guest_result": list(result[:15]),
                                         "initial": initial, "final": final, "reset": reset})
                except Exception as error:
                    raise RuntimeError(str(error) + "\n" + error_log.read_text(errors="replace")[-6000:]) from error
                finally:
                    if process.poll() is None:
                        process.kill(); process.wait(timeout=5)
    assert observations[0]["guest_result"] == observations[1]["guest_result"]
    return {"scope": "ARM EL1 controller, IRQ/FIQ, physical/virtual timers, mailbox and aborts; no Apple firmware",
            "engine_sha256": hashlib.sha256(engine.read_bytes()).hexdigest(),
            "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            "observations": observations, "passed": True, "read_only_snapshots_verified": True,
            "pmp_services_qualified": False, "silicon_routing_qualified": False}


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
