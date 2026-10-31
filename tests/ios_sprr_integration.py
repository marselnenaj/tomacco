#!/usr/bin/env python3
"""Execute a firmware-free ARM64 permission regression in the real QEMU CPU."""
import argparse
import hashlib
import itertools
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time

from ios_dart_integration import connect

FIXTURE = Path(__file__).resolve().parent / "fixtures/ios_sprr_test.S"


def exercise(engine, clang, linker):
    checks = []
    with tempfile.TemporaryDirectory(prefix="tomacco-sprr-") as temporary:
        root = Path(temporary)
        for host, guarded in itertools.product((0, 1), repeat=2):
            executable = root / f"guest-{host}-{guarded}.elf"
            subprocess.run([str(clang), "--target=aarch64-none-elf", "-nostdlib",
                            "--ld-path=" + str(linker), "-Wl,--image-base=0,-Ttext=0x10000,-e,_start",
                            f"-DGUARDED={guarded}", f"-DHOST={host}", str(FIXTURE), "-o", str(executable)],
                           check=True, capture_output=True, text=True, timeout=30)
            command = [str(engine), "-machine", "tomacco-apple-cpu-test", "-m", "64M",
                       "-nodefaults", "-display", "none", "-S",
                       "-device", f"loader,file={executable},cpu-num=0",
                       "-qtest", f"unix:{root}/test-{guarded},server=on,wait=off",
                       "-qtest-log", "/dev/null",
                       "-qmp", f"unix:{root}/monitor-{guarded},server=on,wait=off"]
            error_log = root / f"stderr-{guarded}"
            with error_log.open("wb") as errors:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=errors, stderr=errors)
                try:
                    with connect(root / f"monitor-{guarded}", process) as monitor, connect(root / f"test-{guarded}", process) as test:
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

                        request("qmp_capabilities")
                        request("cont")
                        deadline = time.monotonic() + 5
                        while True:
                            qtest.write(b"read 0x50000 32\n")
                            reply = qtest.readline().decode().strip().split()
                            assert len(reply) == 2 and reply[0] == "OK", reply
                            status, passed, esr, far = struct.unpack("<QQQQ", bytes.fromhex(reply[1][2:]))
                            if status or time.monotonic() > deadline:
                                break
                            time.sleep(.01)
                        request("stop")
                        registers = request("human-monitor-command", {"command-line": "info registers"})
                        request("quit")
                        process.wait(timeout=5)
                        qmp.close(); qtest.close()
                        assert process.returncode == 0
                        assert status == 0xc14e and passed == 15, (
                            f"host={host}, guarded={guarded}, status={status:#x}, checks={passed:#x}, "
                            f"ESR={esr:#x}, FAR={far:#x}\n{registers.split('Q00=')[0]}")
                        checks.append({"entry": ("GL" if guarded else "EL") + ("2" if host else "1"),
                                       "target": "GL0" if guarded else "EL0",
                                       "bank": "HUPERM" if host else "VUPERM", "independent_banks": True,
                                       "mmio_timebase_matches_cpu_counter": True,
                                       "instruction_fetch": True, "data_read_write": True,
                                       "readonly_write_fault_preserves_bytes": True,
                                       "nonexecutable_page_fault": True,
                                       "exception_bank_and_return": True})
                except Exception as error:
                    raise RuntimeError(str(error) + "\n" + error_log.read_text(errors="replace")[-3000:]) from error
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
    with engine.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"scope": "real ARM64 SPRR translation and fault handling, without Apple firmware",
            "engine_sha256": checksum, "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            "checks": checks, "passed": True, "graphical_apps_qualified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--clang", type=Path, default=shutil.which("clang"))
    parser.add_argument("--linker", type=Path, default=shutil.which("ld.lld"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.clang or not args.linker:
        parser.error("Supply Clang and an LLD ELF linker with --clang and --linker")
    result = exercise(args.engine.resolve(strict=True), args.clang.resolve(strict=True), args.linker.absolute())
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")
