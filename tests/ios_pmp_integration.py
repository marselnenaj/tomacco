#!/usr/bin/env python3
"""Exercise the isolated PMP ARM64 machine without any Apple firmware."""
import argparse
import hashlib
import json
import re
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time

from ios_bus_fault_integration import EVENT
from ios_dart_integration import connect

FIXTURE = Path(__file__).resolve().parent / "fixtures/ios_pmp_test.S"


def exercise(engine, clang, linker, midr=0x610f0000, affinity=0):
    observations = []
    with tempfile.TemporaryDirectory(prefix="tomacco-pmp-machine-") as temporary:
        root = Path(temporary)
        executable = root / "guest.elf"
        layout = root / "guest.ld"
        layout.write_text("PHDRS { text PT_LOAD FLAGS(5); }\n"
                          "SECTIONS { . = 0x1000000; .text : { *(.text*) } :text }\n")
        subprocess.run([str(clang), "--target=aarch64-none-elf", "-nostdlib",
                        "--ld-path=" + str(linker), "-Wl,-e,_start,-T," + str(layout),
                        str(FIXTURE), "-o", str(executable)],
                       check=True, capture_output=True, text=True, timeout=30)
        for enabled in (False, True):
            monitor_path, test_path = root / f"monitor-{enabled}", root / f"test-{enabled}"
            command = [str(engine), "-machine", "tomacco-pmp-research", "-m", "2M",
                       "-cpu", f"max,midr={midr:#x},mp-affinity={affinity:#x}", "-nodefaults", "-display", "none", "-S",
                       "-device", f"loader,file={executable},cpu-num=0",
                       "-qtest", f"unix:{test_path},server=on,wait=off", "-qtest-log", "/dev/null",
                       "-qmp", f"unix:{monitor_path},server=on,wait=off"]
            if enabled:
                command += ["-trace", "enable=tomacco_arm_bus_fault", "-trace", "enable=tomacco_arm_sysreg_fault"]
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
                            qtest.write(b"read 0x1100000 240\n")
                            reply = qtest.readline().decode().split()
                            assert len(reply) == 2 and reply[0] == "OK", reply
                            result = struct.unpack("<30Q", bytes.fromhex(reply[1][2:]))
                            if result[0] or time.monotonic() > deadline:
                                break
                            time.sleep(.01)
                        request("stop")
                        request("quit")
                        process.wait(timeout=5)
                        qmp.close(); qtest.close()
                        assert process.returncode == 0
                        assert result[:5] == (0xc14e, 4, 4, 24000000, midr), result
                        assert result[29] == affinity | 0x80000000, result
                        assert result[5] & 0xffffffff == 0xd5034fdf, result  # MSR DAIFSet.
                        assert result[6:8] == (0xfedcba9876543210, 1), result
                        faults = [result[index:index + 3] for index in range(8, 20, 3)]
                        addresses = (0xfffff8, 0x1200000, 0x300e00040, 0x300e00400)
                        for index, (esr, far, pc) in enumerate(faults):
                            assert esr >> 26 == 0x25 and esr & 0x7f == (0x50 if index == 3 else 0x10), faults
                            assert far == addresses[index] and 0x1000000 <= pc < 0x1001000, faults
                        assert result[24:29] == (0xfedcba9876543210, 24000000, 2, 0x1111, 0x2222), result
                        for esr, pc in (result[20:22], result[22:24]):
                            assert esr == 0x2000000 and 0x1000000 <= pc < 0x1001000, result
                    log = error_log.read_text(errors="replace")
                    events = [EVENT.fullmatch(line) for line in log.splitlines() if "tomacco_arm_bus_fault" in line]
                    assert len(events) == (4 if enabled else 0) and all(events), log
                    decoded = []
                    for index, (event, (_, far, pc)) in enumerate(zip(events, faults)):
                        values = [int(value, 16 if i < 3 else 10) for i, value in enumerate(event.groups())]
                        assert values[:5] == [pc, far, far, 8 if index < 2 else 4, int(index == 3)], values
                        assert values[6] == 2, values
                        decoded.append(dict(zip(("pc", "physical", "virtual", "size", "access", "mmu", "response"), values)))
                    sysregs = [re.fullmatch(r'tomacco_arm_sysreg_fault pc=0x([0-9a-f]+) instruction=0x([0-9a-f]{8}) el=(\d)', line)
                               for line in log.splitlines() if line.startswith('tomacco_arm_sysreg_fault ')]
                    assert len(sysregs) == (2 if enabled else 0) and all(sysregs), log
                    sysreg_events = [[int(m[1],16),int(m[2],16),int(m[3])] for m in sysregs]
                    if enabled:
                        assert sysreg_events == [[result[21],0xd53ff4c5,1],[result[23],0xd51df705,1]], sysreg_events
                    observations.append({"trace_enabled": enabled, "guest_result": list(result), "events": decoded,
                                         "sysreg_events":sysreg_events})
                except Exception as error:
                    raise RuntimeError(str(error) + "\n" + error_log.read_text(errors="replace")[-3000:]) from error
                finally:
                    if process.poll() is None:
                        process.kill(); process.wait(timeout=5)
    assert observations[0]["guest_result"] == observations[1]["guest_result"]
    with engine.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"scope": "isolated ARM64 PMP machine, original test instructions, no Apple firmware",
            "engine_sha256": checksum, "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            "observations": observations, "ram_bounds_verified": True, "el1_counter_verified": True,
            "unmapped_hardware_faults_verified": True, "strict_system_register_faults_verified":True,
            "pmp_tlbi_translation_change_verified":True, "passed": True, "pmp_power_services_qualified": False,
            "cpu_identity":{"midr":hex(midr),"affinity":hex(affinity),"guest_mpidr":hex(observations[0]['guest_result'][29])}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--clang", type=Path, default=shutil.which("clang"))
    parser.add_argument("--linker", type=Path, default=shutil.which("ld.lld"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cpu-midr", type=lambda value:int(value,0), default=0x610f0000)
    parser.add_argument("--cpu-affinity", type=lambda value:int(value,0), default=0)
    args = parser.parse_args()
    if not args.clang or not args.linker:
        parser.error("Supply Clang and an LLD ELF linker")
    result = exercise(args.engine.resolve(strict=True), args.clang.resolve(strict=True), args.linker.absolute(), args.cpu_midr, args.cpu_affinity)
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")
