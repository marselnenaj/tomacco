#!/usr/bin/env python3
"""Exercise the built QEMU DART device without Apple firmware or an iOS claim."""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time


def connect(path, process):
    deadline = time.monotonic() + 10
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.settimeout(3)
    while True:
        try:
            peer.connect(str(path))
            return peer
        except (FileNotFoundError, ConnectionRefusedError):
            if process.poll() is not None or time.monotonic() > deadline:
                peer.close()
                raise RuntimeError("QEMU did not create its test socket")
            time.sleep(.025)


def exercise(engine):
    checks = []
    with tempfile.TemporaryDirectory(prefix="tomacco-dart-qtest-") as temporary:
        root = Path(temporary)
        command = [str(engine), "-machine", "tomacco-dart-test", "-m", "64M", "-nodefaults", "-display", "none", "-S",
                   "-qtest", f"unix:{root}/test,server=on,wait=off", "-qtest-log", "/dev/null",
                   "-qmp", f"unix:{root}/monitor,server=on,wait=off"]
        with (root / "stderr").open("wb") as errors:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=errors, stderr=errors)
            try:
                with connect(root / "monitor", process) as monitor, connect(root / "test", process) as test:
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

                    interrupts = {}
                    def bus(command):
                        qtest.write((command + "\n").encode())
                        while True:
                            reply = qtest.readline().decode().strip().split()
                            if reply and reply[0] == "IRQ":
                                interrupts[int(reply[2])] = reply[1] == "raise"
                                continue
                            break
                        if not reply:
                            raise RuntimeError(f"QTest disconnected during {command}")
                        assert reply[0] == "OK", reply
                        return int(reply[1], 16) if len(reply) > 1 else None

                    def mmio(reg, value=None):
                        return bus(f"readl {0x10000000 + reg:#x}" if value is None else f"writel {0x10000000 + reg:#x} {value:#x}")

                    request("qmp_capabilities")
                    children = request("qom-list", {"path": "/machine/dart"})
                    assert sum(row["type"] == "child<tomacco-dart-t8110-iommu>" for row in children) == 256
                    checks.append("256 distinct DMA stream regions")
                    assert mmio(0) == 0x0e000001 and mmio(12) == 0x01000100
                    mmio(0, 0)
                    assert mmio(0) == 0x0e000001
                    checks.append("read-only MMIO hardware parameters")
                    mmio(0xc00, 0x00800000)
                    mmio(0x105c, 9)
                    mmio(0x145c, 5)
                    assert mmio(0xc00) == 0x00800000 and mmio(0x105c) == 9 and mmio(0x145c) == 5
                    mmio(0xc20, 0x00800000)
                    assert mmio(0xc00) == 0
                    checks.append("stream enable/disable and translation registers")
                    mmio(0x200, 1)
                    mmio(0x208, 1)
                    mmio(0x204, 1)
                    mmio(0x145c, 0)
                    assert mmio(0x145c) == 5 and mmio(0x200) == 1
                    checks.append("irreversible register lock until device reset")
                    mmio(0x80, 0x117)
                    assert mmio(0x80) == 0x117
                    checks.append("synchronous stream invalidation")
                    request("system_reset")
                    assert mmio(0x145c) == 0 and mmio(0x105c) == 0 and mmio(0x208) == 0
                    checks.append("QEMU reset propagation")
                    bus("irq_intercept_out /machine/dart sysbus-irq")
                    # Hardware page walk: TTBR -> directory -> page -> real RAM.
                    va, physical, window = 0x12345054, 0x20000, 1 << 44
                    pmd = 0x4000 + ((va >> 25) & 2047) * 8
                    pte = 0x8000 + ((va >> 14) & 2047) * 8
                    bus(f"writeq {pmd:#x} {(0x8000 >> 4) | 1:#x}")
                    descriptor = (physical >> 4) | 1 | (0xfff << 40)
                    bus(f"writeq {pte:#x} {descriptor:#x}")
                    mmio(0xc00, 1 << 23); mmio(0x105c, 1); mmio(0x145c, 5)
                    bus(f"writel {window + va:#x} 0x71caf00d")
                    assert bus(f"readl {physical + (va & 0x3fff):#x}") == 0x71caf00d
                    assert bus(f"readl {window + va:#x}") == 0x71caf00d
                    checks.append("real QEMU DMA write/read through guest page tables")
                    bus(f"writeq {pte:#x} {descriptor | 4:#x}")
                    mmio(0x80, 0x117)
                    bus(f"writel {window + va:#x} 0xdeadbeef")
                    assert bus(f"readl {physical + (va & 0x3fff):#x}") == 0x71caf00d
                    assert mmio(0x100) & 0x80000010 == 0x80000010
                    assert mmio(0x170) == va and interrupts.get(0) is True
                    mmio(0x100, 0x80000000)
                    assert mmio(0x100) == 0 and interrupts.get(0) is False
                    checks.append("blocked DMA writes preserve RAM and raise/clear IRQ")
                    # One permitted word surrounded by protected bytes. A bus
                    # transfer crossing both bounds must not widen the grant.
                    unit = (va & 0x3fff) // 4
                    bus(f"writeq {pte:#x} {(physical >> 4) | 1 | (unit << 52) | (unit << 40):#x}")
                    for offset in (-4, 4):
                        bus(f"writel {physical + (va & 0x3fff) + offset:#x} 0xaabbccdd")
                    mmio(0x80, 0x117)
                    bus(f"write {window + va - 4:#x} 12 0x111111112222222233333333")
                    assert bus(f"readl {physical + (va & 0x3fff):#x}") == 0x22222222
                    for offset in (-4, 4):
                        assert bus(f"readl {physical + (va & 0x3fff) + offset:#x}") == 0xaabbccdd
                    assert mmio(0x170) == va - 4 and interrupts.get(0) is True
                    mmio(0x104, 0x10)
                    assert interrupts.get(0) is False
                    mmio(0x104, 0)
                    assert interrupts.get(0) is True
                    mmio(0x100, 0x80000000)
                    checks.append("cross-boundary DMA preserves four-byte subpage guards and masked IRQ state")
                    mmio(0x105c, 6)
                    assert bus(f"readl {window + physical + (va & 0x3fff):#x}") == 0x22222222
                    checks.append("bypass accesses physical RAM without a page-table walk")
                    mmio(0x105c, 1)
                    # A table pointing back into MMIO must fail, never recurse.
                    mmio(0x145c, (0x10000000 >> 12) | 1)
                    bus(f"readl {window + va:#x}")
                    assert mmio(0x100) & 0x80000080 == 0x80000080
                    checks.append("MMIO page-table pointers rejected without recursion")
                    request("quit")
                    process.wait(timeout=5)
                    assert process.returncode == 0
                    qmp.close(); qtest.close()
            except Exception as error:
                raise RuntimeError(f"{error}; QEMU status={process.poll()}; " +
                                   (root / "stderr").read_text(errors="replace")[-4000:]) from error
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
    with engine.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"scope": "QEMU DART DMA, MMIO and lifecycle, without Apple firmware",
            "engine_sha256": checksum, "checks": checks, "passed": True,
            "guest_dma_qualified": False, "graphical_apps_qualified": False}


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
