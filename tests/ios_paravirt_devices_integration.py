#!/usr/bin/env python3
"""Exercise real vphone QEMU peripherals, with the firmware CPU paused.

Requires a local run_ios_paravirt.py request receipt and its original firmware.
No guest boot, SEP services, graphical rendering or app compatibility is claimed.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile

from ios_dart_integration import connect
from ios_paravirt_virtio import check_virtio_storage
from ios_paravirt_usb_integration import check_virtio_usb
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from run_ios_paravirt import QMP, file_digest, stop_child
from prepare_ios_paravirt import ap_cpu_count, decode


class GuestMemory:
    """Use QEMU's real GDB memory interface for the SEP's private address space."""
    def __init__(self, socket, sep_cpu_index=33):
        self.socket = socket
        self.socket.settimeout(5)
        assert self.packet(f"Hg{sep_cpu_index + 1}") == b"OK"

    def packet(self, command):
        payload = command.encode()
        self.socket.sendall(b"$" + payload + b"#" + f"{sum(payload) & 255:02x}".encode())
        assert self.socket.recv(1) == b"+"
        assert self.socket.recv(1) == b"$"
        result = bytearray()
        while len(result) < 1024 * 1024:
            value = self.socket.recv(1)
            if not value:
                raise EOFError("GDB disconnected")
            if value == b"#":
                checksum = bytes(self.socket.recv(1) + self.socket.recv(1))
                assert int(checksum, 16) == sum(result) & 255
                self.socket.sendall(b"+")
                return bytes(result)
            result += value
        raise ValueError("Oversized GDB packet")

    def read(self, address, size):
        return bytes.fromhex(self.packet(f"m{address:x},{size:x}").decode())

    def write(self, address, data):
        assert self.packet(f"M{address:x},{len(data):x}:{data.hex()}") == b"OK"



def check_peripherals(bus, read, write, qmp, interrupts, interrupt_events, arena, payload, disk, original_disk, checks):
    def mmio(offset, value=None):
        if value is None:
            return int(bus(f"readl {0x30220000 + offset:#x}"), 16)
        bus(f"writel {0x30220000 + offset:#x} {value:#x}")
    def fifo(*words):
        for word in words:
            mmio(0x200, word)
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51")
    cbc = bytes.fromhex("7649abac8119b246cee98e9b12e9197d5086cb9b507219ee95db113a917678b2")
    ecb = bytes.fromhex("3ad77bb40d7a3660a89ecaf32466ef97")
    iv = bytes(range(16))
    src, dst, saved_iv = arena, arena + 0x100, arena + 0x200
    def crypt(key_context, iv_context, data):
        write(src, data)
        write(dst, bytes(len(data)))
        high = ((src >> 32) << 16) | (dst >> 32)
        fifo(0x50000000 | (key_context << 27) | (iv_context << 25) | len(data),
             high, src & 0xffffffff, dst & 0xffffffff)
        return read(dst, len(data))
    assert int(bus("readl 0x30230000"), 16) == 0xf
    for value in (0, 1, 2, 4, 8, 0xffffffff):
        bus(f"writel 0x30230000 {value:#x}")
        assert int(bus("readl 0x30230000"), 16) == 0xf
    checks.append("Unprovisioned UID/GID/scrambler keys are advertised disabled and guest writes cannot enable them")
    mmio(0x1c, 0x24)
    fifo(0x10100000, *struct.unpack("<4I", key))  # key0: ECB encrypt
    fifo(0x18010000, *struct.unpack("<4I", key))  # key1: CBC decrypt
    fifo(0x2c000000, *struct.unpack("<4I", iv))  # IV context3
    assert crypt(0, 0, plain[:16]) == ecb
    assert crypt(1, 3, cbc) == plain
    checks.append("AES-128 ECB/CBC known-answer vectors with independent key contexts")
    fifo(0x18110000, *struct.unpack("<4I", key))  # key1: CBC encrypt
    fifo(0x2c000000, *struct.unpack("<4I", iv))
    assert crypt(1, 3, plain[:16]) == cbc[:16]
    assert crypt(1, 3, plain[16:]) == cbc[16:]
    fifo(0x6c000000 | (saved_iv >> 32), saved_iv & 0xffffffff)
    assert read(saved_iv, 16) == cbc[16:]
    checks.append("CBC continuation and stored IV use the selected IV context")
    fifo(0x8800005a)
    assert mmio(0x30) == 0x5a and interrupts.get(18) is True
    mmio(0x18, 0x20)
    assert interrupts.get(18) is False
    for selector in range(1, 8):
        fifo(0x10000000 | selector << 24)
        assert mmio(0x18) & 4 and interrupts.get(18) is True
        mmio(0x18, 4)
    fifo(0x10100000, *struct.unpack("<4I", key))
    assert crypt(0, 0, plain[:16]) == ecb
    checks.append("IRQ acknowledgement and explicit hardware-key refusal with FIFO recovery")
    assert int(bus("readl 0x30000000"), 16) == 1
    assert int(bus("readl 0x30010000"), 16) == 1  # transport ID, not media state
    sector, data, status, descriptor = arena + 0x300, arena + 0x400, arena + 0x600, arena + 0x700
    write(sector, struct.pack("<4I", 0, 0, 8, 0))
    for operation in (0x30001, 0x10001):
        write(status, b"\xff")
        write(descriptor, struct.pack("<QIIQIIQII", sector, 16, 0, data, 512, operation, status, 1, 0))
        bus(f"writeq 0x30000408 {descriptor:#x}")
        assert read(status, 1) == (b"\0" if operation == 0x30001 else b"\1")
        assert read(data, 512) == payload
    assert file_digest(disk) == original_disk
    write(descriptor, struct.pack("<QIIQIIQII", sector, 16, 0, data, 512, 0x30001, status, 1, 0))
    write(status, b"\xff")
    bus(f"writeq 0x30010408 {descriptor:#x}")
    assert read(status, 1) == b"\0" and read(data, 512) == bytes(512)
    checks.append("BDIF reads real ROOT/AUX bytes and rejects writes")
    # Genuine driver descriptor ABI: completion is separate
    # from the error result and cannot target MMIO via DMA.
    for version, operation, completed, prop, buffer, size, error in (
        (1, 1, 0, 1, data, 512, 2),
        (1, 1, 0, 77, data, 512, 45),
        (2, 1, 0, 1, data, 512, 45),
        (1, 2, 0, 1, data, 512, 45),
        (1, 1, 1, 1, data, 512, 22),
        (1, 1, 0, 1, 0x30250000, 16, 14),
        (1, 1, 0, 1, (1 << 64) - 8, 16, 14),
        (1, 1, 0, 1, arena + 0x10000 - 8, 16, 14),
    ):
        request_bytes = struct.pack("<HBBIQQ", version, operation, completed, prop, buffer, size)
        write(descriptor, request_bytes)
        bus(f"writeq 0x4ffff8 {descriptor:#x}")
        actual = struct.unpack("<HBBIQQ", read(descriptor, 24))
        assert actual == (version, operation, 1, prop, error, 0), actual
    checks.append("Host-property completion, Darwin errors, and DMA bounds without a fabricated host key")
    # Drive virtual time without executing either firmware CPU.
    avp = int(bus("readq 0x30240000"), 16)
    bus("writel 0x20050008 1000")
    bus("writel 0x20050004 1002")
    bus("writel 0x20050010 1")
    bus("clock_step 2000000000")
    assert int(bus("readl 0x20050000"), 16) == 1002
    assert int(bus("readq 0x30240000"), 16) - avp == 2000000
    alarm = int(bus("readl 0x20050018"), 16)
    assert alarm == 1 and interrupts.get(2) is True, (alarm, interrupts, interrupt_events)
    bus("writel 0x2005001c 1")
    assert interrupts.get(2) is False
    bus("writeq 0x30240018 1")
    bus("writeq 0x30240020 6")
    assert int(bus("readq 0x30240018"), 16) == 1
    assert int(bus("readq 0x30240020"), 16) == 6
    assert int(bus("readq 0x30240030"), 16) == 0
    checks.append("PL031 time/load/alarm IRQ and acknowledgement; AVP microsecond clock advances with VM time")
    assert int(bus("readl 0x40000000"), 16) == 0x00081b36  # QEMU GPEX
    assert int(bus("readl 0x40008000"), 16) == 0xffffffff  # absent PCI function
    assert int(bus("readl 0x1fff0008"), 16) == 0x80a00080
    interrupt_events.clear()
    bus("writel 0x1fff0040 160")
    assert interrupt_events == [(128, True), (128, False)], interrupt_events
    checks.append("Real PCI ECAM enumeration and Apple-compatible MSI frame route INTID 160 to the AP GIC")
    bus("writel 0x2006040c 0x40")  # pinned iOS driver's rising-edge detection
    bus("writel 0x20060410 0x40")  # pinned AppleVirtualPlatformButtons pin6
    assert "return" in qmp.call("system_powerdown")
    assert int(bus("readl 0x20060100"), 16) == 0x40
    assert int(bus("readl 0x20060020"), 16) == 0  # unrelated macOS pin3 stays low
    assert int(bus("readl 0x20060418"), 16) == 0x40 and interrupts.get(5) is True
    bus("clock_step 100000000")
    assert int(bus("readl 0x20060100"), 16) == 0
    bus("writel 0x2006041c 0x40")
    assert interrupts.get(5) is False
    checks.append("QMP power button pulses the pinned iOS pin6 for100ms, leaves pin3 low and produces an acknowledged IRQ")


def check_boot_measurements(bus, sep, checks):
    def read_word(offset):
        return int(bus(f"readl {0x600000 + offset:#x}"), 16)

    def write_word(offset, value):
        bus(f"writel {0x600000 + offset:#x} {value:#x}")

    # The bootloader writes 48 digest bytes, one metadata word, then zeros;
    # it publishes the record with bit 0 and locks it with bit 16. Exercise
    # all data words and independent banks, observed through the SEP CPU.
    for index, offset in enumerate((0, 0x90, 0xd8)):
        address = 0x600000 + offset
        assert sep.read(address, 0x48) == bytes(0x48)
        words = [0x10203040 + index * 0x100 + word for word in range(12)]
        words += [index + 7, 0, 0, 0, 0]
        expected = struct.pack("<17I", *words)
        for word, value in enumerate(words):
            write_word(offset + 4 * word, value)
        assert read_word(offset + 0x44) == 0  # Writes do not claim validity.
        write_word(offset + 0x44, 1)
        assert sep.read(address, 0x48) == expected + struct.pack("<I", 1)
        write_word(offset + 0x44, 0)
        assert read_word(offset + 0x44) == 0  # Valid can clear before locking.
        write_word(offset + 0x44, 0x10001)
        write_word(offset, 0xffffffff)
        write_word(offset + 0x40, 0xffffffff)
        write_word(offset + 0x44, 0)
        assert sep.read(address, 0x48) == expected + struct.pack("<I", 0x10001)
        assert sep.packet(f"M{address:x},4:00000000").startswith(b"E")
        assert sep.read(address, 0x44) == expected
    assert read_word(0x400) == 0
    for value, expected in ((1, 1), (2, 3), (0, 3), (4, 3)):
        write_word(0x400, value)
        assert read_word(0x400) == expected
        assert sep.read(0x600400, 4) == struct.pack("<I", expected)
    assert sep.packet("M600400,4:00000000").startswith(b"E")
    for offset in (0x48, 0x120, 0x404, 0x7ffc):
        assert sep.packet(f"m{0x600000 + offset:x},4").startswith(b"E")
    checks.append("ASMF shares guest-written measurement banks and accumulated boot flags between AP and SEP; locks are irreversible until reset, SEP writes and unknown registers fail")


def storage_command(sep, operation=0, sector=0, data=None, *, data_address=0x08001000,
                    data_length=512, data_flags=None, done_address=0x08000020,
                    done_flags=0x20001, descriptor_address=0x08000040):
    """Submit the original SEP driver's three-descriptor ABI through real MMIO."""
    if data is not None:
        sep.write(0x08001000, data)
    sep.write(0x08000000, struct.pack("<IIQ", operation, 0, sector))
    sep.write(0x08000020, b"\xff")
    flags = (0x30001 if operation == 0 else 0x10001) if data_flags is None else data_flags
    sep.write(0x08000040,
              struct.pack("<QII", 0x08000000, 16, 0x10001) +
              struct.pack("<QII", data_address, data_length, flags) +
              struct.pack("<QII", done_address, 1, done_flags))
    result = sep.packet("M30020408,8:" + struct.pack("<Q", descriptor_address).hex())
    assert sep.read(0x30020010, 4) == struct.pack("<I", 1)
    return result, sep.read(0x08000020, 1)


def storage_setup(sep, sectors):
    for address, expected, size in ((0x30020000, 1, 4), (0x30020004, 2, 4),
                                    (0x30020008, 0x420, 8), (0x30020420, sectors, 4),
                                    (0x30020434, 512, 4)):
        assert int.from_bytes(sep.read(address, size), "little") == expected
    sep.write(0x30020400, struct.pack("<I", 1))
    sep.write(0x30020404, struct.pack("<I", 3))
    assert sep.read(0x30020400, 4) == struct.pack("<I", 1)
    assert sep.read(0x30020404, 4) == struct.pack("<I", 3)


def check_storage(sep, read, disk, checks):
    sectors = disk.stat().st_size // 512
    storage_setup(sep, sectors)
    expected = bytearray(disk.read_bytes())
    for sector in (0, sectors // 2, sectors - 1):
        result = storage_command(sep, sector=sector)
        assert result == (b"OK", b"\0"), (sector, result)
        assert sep.read(0x08001000, 512) == expected[sector * 512:(sector + 1) * 512]
        payload = bytes((sector + i) % 256 for i in range(512))
        assert storage_command(sep, 1, sector, payload) == (b"OK", b"\0")
        expected[sector * 512:(sector + 1) * 512] = payload
        assert disk.read_bytes() == expected
        assert storage_command(sep, sector=sector, data=bytes(512)) == (b"OK", b"\0")
        assert sep.read(0x08001000, 512) == payload
    # The DMA buffer belongs exclusively to the SEP address space.
    assert read(0x08001000, 512) != payload
    checks.append("VSTR reads and flushes real 512-byte sectors at both disk boundaries; SEP-only DMA and exact host-file contents verified")
    for kwargs in ({"sector": sectors}, {"sector": 0xffffffffffffffff},
                   {"operation": 7}, {"data_length": 511}, {"data_flags": 0x10001},
                   {"data_address": 0x08003e01}, {"data_address": 0xfffffffffffffff0},
                   {"data_address": 0x30020408}, {"data_address": 0x70000000}):
        sep.write(0x08001000, b"x" * 512)
        assert storage_command(sep, **kwargs) == (b"OK", b"\x01")
        assert sep.read(0x08001000, 512) == b"x" * 512
        assert disk.read_bytes() == expected
    for kwargs in ({"done_address": 0x30020408}, {"done_flags": 0x10001},
                   {"descriptor_address": 0xfffffffffffffff0},
                   {"descriptor_address": 0x08000041}):
        result, status = storage_command(sep, **kwargs)
        assert result.startswith(b"E") and status == b"\xff"
        assert disk.read_bytes() == expected
    assert sep.packet("M30020404,4:04000000").startswith(b"E")
    assert sep.packet("m30020200,4").startswith(b"E")
    checks.append("VSTR rejects out-of-range sectors, malformed descriptors, unsupported commands and DMA to MMIO/AP RAM without mutating media")


def check_storage_reset(sep, disk, checks):
    for address, size in ((0x30020400, 4), (0x30020404, 4), (0x30020408, 8)):
        assert sep.read(address, size) == bytes(size)
    assert sep.read(0x08000000, 128) == bytes(128)
    assert sep.read(0x08001000, 512) == bytes(512)
    storage_setup(sep, disk.stat().st_size // 512)
    assert storage_command(sep, sector=0) == (b"OK", b"\0")
    assert sep.read(0x08001000, 512) == disk.read_bytes()[:512]
    assert any(sep.read(0x08001000, 512))
    checks.append("VSTR reset clears queue state and volatile DMA buffers while retaining the actual stored sectors")


def check_storage_reopen(command, root, disk, checks, boot_role, handoff, sep_cpu_index):
    # Userspace submission is a restore-only profile. Preloaded firmware can
    # reopen the same media in either explicit payload role.
    other_role = boot_role if handoff == "userspace" else ("restore" if boot_role == "normal" else "normal")
    command = list(command)
    index = command.index("-M") + 1
    command[index] = command[index].replace(",sep-role=" + boot_role, ",sep-role=" + other_role)
    with (root / "reopen.log").open("wb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True,
                                   env={**os.environ, "REIMS_VGPU_RAIL": "vulkan", "REIMS_VGPU_WINDOW": "0"})
        qmp = None
        try:
            with connect(root / "monitor", process):
                pass
            qmp = QMP(root / "monitor")
            with connect(root / "gdb", process) as remote:
                sep = GuestMemory(remote, sep_cpu_index)
                tag = b"rsep" if other_role == "restore" else b"sepi"
                assert sep.read(0x04020028, 4) == tag[::-1]
                storage_setup(sep, disk.stat().st_size // 512)
                expected = disk.read_bytes()
                for sector in (0, len(expected) // 1024, len(expected) // 512 - 1):
                    assert storage_command(sep, sector=sector) == (b"OK", b"\0")
                    assert sep.read(0x08001000, 512) == expected[sector * 512:(sector + 1) * 512]
            qmp.call("quit")
            process.wait(timeout=5)
            assert process.returncode == 0
        finally:
            stop_child(process)
            if qmp is not None:
                qmp.close()
    checks.append("VSTR data survives full QEMU restart with the selected restore handoff" if handoff == "userspace" else
                  "VSTR data survives full QEMU restart; both normal sepi and restore rsep payload tags reach the real handoff")


def check_sep(bus, read, write, qmp, interrupts, arena, inputs, root, process, checks, boot_role, handoff, measure_manifest, sep_cpu_index):
    data = arena + 0x400
    payload = bytes(range(256)) * 2
    with connect(root / "gdb", process) as remote:
        sep = GuestMemory(remote, sep_cpu_index)
        check_boot_measurements(bus, sep, checks)
        check_storage(sep, read, root / "sep-storage.img", checks)
        entropy = [sep.read(0x400038, 4) for _ in range(32)]
        assert len(set(entropy)) > 30 and any(any(word) for word in entropy)
        assert sep.packet("M400038,4:00000000").startswith(b"E")
        assert sep.read(0x400038, 4) not in entropy
        checks.append("SEP TRNG reads fresh words from QEMU guest entropy; writes cannot fix or replace the random source")
        boot_record = sep.read(0x04020000, 0xe0)
        assert struct.unpack_from("<I", boot_record, 0)[0] == 0x53455042
        assert struct.unpack_from("<I", boot_record, 8)[0] == 1
        assert struct.unpack_from("<I", boot_record, 0x10)[0] == len(boot_record)
        tag = b"rsep" if boot_role == "restore" else b"sepi"
        assert boot_record[0x28:0x2c] == tag[::-1]
        seed = boot_record[0x80:0xc0]
        # Catch the old partially filled seed as well as a repeated constant
        # from a reference ROM. These bytes come from QEMU's guest RNG.
        assert all(any(seed[i:i + 16]) for i in range(0, len(seed), 16))
        assert len(set(seed[i:i + 16] for i in range(0, len(seed), 16))) == 4
        if measure_manifest:
            expected_hash = hashlib.sha384(inputs["sep-manifest"].read_bytes()).digest()
            assert boot_record[0x30:0x38] == b"\x01" + bytes(7)
            assert boot_record[0x38:0x68] == expected_hash
            assert boot_record[0x68:0x70] == bytes(8)
            assert f"TOMACCO_SEP_MANIFEST_MEASURED sha384={expected_hash.hex()} " in (root / "host.log").read_text()
            checks.append("Opt-in SEPB digest is SHA-384 of the exact complete IM4M; its independent policy byte remains zero")
        else:
            assert boot_record[0x30:0x70] == bytes(0x40)
            assert "TOMACCO_SEP_MANIFEST_MEASURED" not in (root / "host.log").read_text()
        checks.append("SEPB v1 declares its 224-byte extent and supplies all 64 seed bytes without authentication claims")
        assert sep.read(0x06000000, 16) == inputs["sep"].read_bytes()[:16]
        marker = "RESERVED" if handoff == "userspace" else "LOADED"
        loaded = re.search(r"TOMACCO_SEPFW_" + marker + r" base=(0x[0-9a-f]+) bytes=(\d+)",
                           (root / "host.log").read_text())
        assert loaded is not None
        base, length = int(loaded[1], 16), int(loaded[2])
        firmware = inputs["sep"].read_bytes()
        capacity = len(firmware) + (0x110000 if handoff == "userspace" else 0)
        assert length == capacity and base >= 0x70000000
        expected = bytes(length) if handoff == "userspace" else firmware
        for offset in range(0, length, 65536):
            assert read(base + offset, min(65536, length - offset)) == expected[offset:offset + 65536]
        assert sep.read(base, 64) == expected[:64]
        checks.append("AP SEPFW reserves a zeroed region for userspace submission while private SEP RAM contains the original firmware" if handoff == "userspace" else
                      "AP SEPFW handoff contains the exact validated image also loaded into private SEP RAM")
        if "sep-manifest" in inputs:
            manifest = inputs["sep-manifest"].read_bytes()
            assert sep.read(0x06000000 + len(firmware), len(manifest)) == manifest
            assert sep.read(0x04020070, 4) == struct.pack("<I", len(firmware))
            assert sep.read(0x04020078, 4) == struct.pack("<I", len(manifest))
            checks.append("IM4M bytes and manifest offset/length survive the real SEP reset handoff; signature not qualified")
        write(data, payload)
        assert sep.read(data, 512) == payload
        sep.write(0x07fff000, b"private-SEP")
        assert read(0x07fff000, 11) != b"private-SEP"
        assert int(bus("readl 0x3025001c"), 16) == 1 << 17
        bus("writel 0x30250018 0x20")
        for index in range(8):
            # AP's native STP: two consecutive 64-bit writes.
            bus(f"writeq 0x30250200 {index + 1}")
            bus("writeq 0x30250208 0")
        assert int(bus("readl 0x30250020"), 16) == (8 << 20) | (1 << 16)
        for index in range(8):
            actual = sep.read(0x30250100, 16)
            assert struct.unpack_from("<Q", actual)[0] == index + 1
            assert struct.unpack_from("<I", actual, 12)[0] & 0xf00000 == (8 - index) << 20
        assert int(bus("readl 0x30250020"), 16) == 1 << 17
        # SEP uses four word stores instead of AP's STP.
        for offset, word in ((0, 0x12345678), (4, 0xabcdef01), (8, 0x42), (12, 0)):
            sep.write(0x30250200 + offset, struct.pack("<I", word))
        assert int(bus("readl 0x3025001c"), 16) == 1 << 20
        assert interrupts.get(20) is True
        assert int(bus("readq 0x30250100"), 16) == 0xabcdef0112345678
        assert int(bus("readq 0x30250108"), 16) == (1 << 52) | 0x42
        assert interrupts.get(20) is False
    checks.append("Isolated SEP RAM, real shared AP RAM, both mailbox directions, FIFO order/full/empty/count, and AP receive IRQ")
    return boot_record


def exercise(engine, request, loader_manifest=None):
    record = json.loads(request.read_text())
    if record.get("machine") != "tomacco-vphone" or record["engine_sha256"] != file_digest(engine):
        raise ValueError("Receipt must identify this exact virtual iPhone engine")
    inputs = {}
    for key in ("bootkc", "sptm", "txm", "dtree", "ramdisk", "tc",
                *(name for name in ("sep", "sep-manifest") if name in record["inputs"])):
        entry = record["inputs"][key]
        path = Path(entry["path"]).resolve(strict=True)
        if file_digest(path) != entry["sha256"]:
            raise ValueError(f"Changed firmware input: {key}")
        inputs[key] = path
    if loader_manifest is not None:
        if "sep" not in inputs or "sep-manifest" in inputs:
            raise ValueError("Loader-only manifest requires a SEP receipt without an existing manifest")
        inputs["sep-manifest"] = loader_manifest.resolve(strict=True)
    tree = decode(inputs["dtree"].read_bytes())
    ap_cpus = ap_cpu_count(tree)
    memory = int.from_bytes(tree.child("chosen").get("dram-size"), "little")
    # Work at the end of guest RAM, outside the firmware's initial load arena.
    arena = 0x70000000 + memory - 0x10000
    machine = "tomacco-vphone," + ",".join(k + "=" + str(v).replace(",", ",,") for k, v in inputs.items())
    boot_role = record.get("sep_boot_role", "normal")
    handoff = record.get("sep_handoff", "preloaded")
    measure_manifest = record.get("sep_manifest_measurement", {}).get("requested", False)
    if type(measure_manifest) is not bool or (measure_manifest and "sep-manifest" not in inputs):
        raise ValueError("SEP measurement requires an explicit manifest and boolean request")
    if boot_role not in ("normal", "restore"):
        raise ValueError("Unknown SEP boot role in receipt")
    if handoff not in ("preloaded", "userspace") or (handoff == "userspace" and boot_role != "restore"):
        raise ValueError("Unsupported SEP handoff in receipt")
    if "sep" in inputs:
        machine += ",sep-role=" + boot_role + ",sep-handoff=" + handoff
    if measure_manifest:
        machine += ",sep-measure-manifest=on"
    checks = []
    with (engine.parent / ".tomacco-vphone-session.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for backend in (("qtest", "tcg") if "sep" in inputs else ("qtest",)):
            with tempfile.TemporaryDirectory(prefix="tomacco-vphone-devices-") as temporary:
                root = Path(temporary)
                disk = root / "read-only.img"
                payload = bytes(range(256)) * 2
                disk.write_bytes(bytes(4096) + payload + bytes(4096))
                original_disk = file_digest(disk)
                auxiliary = root / "auxiliary.img"
                auxiliary.write_bytes(bytes(1024 * 1024))
                sep_disk = root / "sep-storage.img"
                if "sep" in inputs:
                    sep_disk.write_bytes(bytes(512 * 1024))
                command = [str(engine), "-M", machine, "-m", f"{memory >> 30}G", "-accel", backend, "-S", "-display", "none",
                           "-smp", f"{ap_cpus},maxcpus={ap_cpus + 1}" if "sep" in inputs else str(ap_cpus),
                           "-serial", "null", "-monitor", "none", "-action", "panic=pause",
                           "-d", "guest_errors,unimp", "-D", str(root / "device-debug.log"),
                           "-rtc", "clock=vm",
                           "-qmp", f"unix:{root}/monitor,server=on,wait=off",
                           "-qtest", f"unix:{root}/test,server=on,wait=off", "-qtest-log", "/dev/null",
                           "-drive", f"if=none,id=tomacco-root,format=raw,readonly=on,file={disk}"]
                command += ["-blockdev", json.dumps({"driver": "raw", "node-name": "tomacco-aux",
                            "file": {"driver": "file", "filename": str(auxiliary), "locking": "on"}}),
                            "-device", "vmapple-virtio-blk-pci,variant=aux,drive=tomacco-aux,addr=7,werror=report"]
                command += ["-chardev", f"socket,id=usb,path={root}/usb,server=on,wait=off",
                            "-device", "tomacco-vphone-usb-pci,chardev=usb,addr=8"]
                if "sep" in inputs:
                    command += ["-drive", f"if=none,id=tomacco-sep-storage,format=raw,file.locking=on,file={sep_disk}"]
                if backend == "tcg":
                    command += ["-gdb", f"unix:{root}/gdb,server=on,wait=off"]
                with (root / "host.log").open("wb") as log:
                    process = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True,
                                               env={**os.environ, "REIMS_VGPU_RAIL": "vulkan", "REIMS_VGPU_WINDOW": "0"})
                    qmp = None
                    try:
                        with connect(root / "monitor", process):
                            pass
                        qmp = QMP(root / "monitor")
                        expected_cpu_indexes = list(range(ap_cpus + ("sep" in inputs)))
                        actual_cpu_indexes = sorted(
                            item["cpu-index"] for item in qmp.call("query-cpus-fast"))
                        assert actual_cpu_indexes == expected_cpu_indexes, actual_cpu_indexes
                        checks.append(f"QEMU exposes {ap_cpus} AP vCPUs and {int('sep' in inputs)} private SEP vCPUs")
                        with connect(root / "test", process) as peer, peer.makefile("rwb", buffering=0) as wire:
                            interrupts = {}
                            interrupt_events = []
                            def bus(command):
                                wire.write(command.encode() + b"\n")
                                for _ in range(128):
                                    reply = wire.readline().decode().split()
                                    if reply and reply[0] == "IRQ":
                                        interrupts[int(reply[2])] = reply[1] == "raise"
                                        interrupt_events.append((int(reply[2]), reply[1] == "raise"))
                                        continue
                                    assert reply and reply[0] == "OK", (command, reply)
                                    return reply[1] if len(reply) > 1 else None
                                raise RuntimeError("QTest interrupt limit exceeded")
                            def read(address, size):
                                return bytes.fromhex(bus(f"read {address:#x} {size}")[2:])
                            def write(address, value):
                                bus(f"write {address:#x} {len(value)} 0x{value.hex()}")
                            bus("irq_intercept_in /machine/interrupts")
                            if backend == "qtest":
                                # QTest never executes the firmware CPUs, but
                                # VM timers must be enabled to dispatch alarms.
                                assert "return" in qmp.call("cont")
                                serial = (tree.get("serial-number") if "serial-number" in tree.props else
                                          f"PSV{int.from_bytes(tree.child('chosen').get('random-seed')[:8], 'little'):016x}".encode() + b"\0")
                                # Actual VMAppleCfg layout: 0x100 + 128 CPU
                                # IDs (4 bytes each) + 0x200 scratch bytes.
                                assert read(0x400500, len(serial)) == serial
                                checks.append("VMApple configuration serial matches the prepared VM identity")
                                check_peripherals(bus, read, write, qmp, interrupts, interrupt_events,
                                                  arena, payload, disk, original_disk, checks)
                                check_virtio_storage(bus, read, write, qmp, arena, auxiliary, checks)
                                check_virtio_usb(bus, read, write, qmp, arena - 0xf0000, root / "usb", checks)
                            else:
                                sep_boot_record = check_sep(
                                    bus, read, write, qmp, interrupts, arena,
                                    inputs, root, process, checks, boot_role, handoff, measure_manifest, ap_cpus)
                            assert "return" in qmp.call("system_reset")
                            assert int(bus("readl 0x30220018"), 16) == 0 and int(bus("readl 0x30220030"), 16) == 0
                            assert int(bus("readl 0x30230000"), 16) == 0xf
                            assert int(bus("readq 0x30240018"), 16) == 0
                            assert int(bus("readq 0x30240020"), 16) == 0
                            if "sep" in inputs:
                                assert int(bus("readl 0x30250018"), 16) == 0
                                assert int(bus("readl 0x3025001c"), 16) == 1 << 17
                                if backend == "tcg":
                                    with connect(root / "gdb", process) as remote:
                                        sep = GuestMemory(remote, ap_cpus)
                                        check_storage_reset(sep, sep_disk, checks)
                                        for offset in (0, 0x90, 0xd8):
                                            assert sep.read(0x600000 + offset, 0x48) == bytes(0x48)
                                            assert int(bus(f"readl {0x600000 + offset + 0x44:#x}"), 16) == 0
                                        assert sep.read(0x600400, 4) == bytes(4)
                                        assert int(bus("readl 0x600400"), 16) == 0
                                        checks.append("ASMF reset clears measurement bytes, valid flags, locks and accumulated boot flags in both CPU views")
                                        reset_record = sep.read(0x04020000, 0xe0)
                                        assert reset_record[:0x80] == sep_boot_record[:0x80]
                                        assert reset_record[0xc0:] == sep_boot_record[0xc0:]
                                        for offset in range(0x80, 0xc0, 16):
                                            assert reset_record[offset:offset + 16] != sep_boot_record[offset:offset + 16]
                                            assert any(reset_record[offset:offset + 16])
                                        checks.append("SEP reset refreshes all four seed blocks while preserving the handoff metadata")
                                        if "sep-manifest" in inputs:
                                            firmware = inputs["sep"].read_bytes()
                                            manifest = inputs["sep-manifest"].read_bytes()
                                            assert sep.read(0x06000000 + len(firmware), len(manifest)) == manifest
                            checks.append("Device reset clears command and interrupt state")
                            assert int(bus("readb 0x20070000"), 16) & 1
                            # Observe the real QMP panic event, not just the MMIO value.
                            bus("writeb 0x20070000 1")
                            for _ in range(16):
                                event = qmp.receive()
                                if event.get("event") == "GUEST_PANICKED":
                                    break
                            else:
                                raise AssertionError("Missing guest panic notification")
                            checks.append("pvpanic forwards an actual GUEST_PANICKED QMP event")
                        qmp.call("quit")
                        process.wait(timeout=5)
                        assert process.returncode == 0
                    except Exception as error:
                        debug = root / "device-debug.log"
                        detail = debug.read_text(errors="replace")[-2500:] if debug.exists() else ""
                        raise RuntimeError(str(error) + "\n" + (root / "host.log").read_text(errors="replace")[-2500:] + detail) from error
                    finally:
                        stop_child(process)
                        if qmp is not None:
                            qmp.close()
                if backend == "tcg":
                    check_storage_reopen(command, root, sep_disk, checks, boot_role, handoff, ap_cpus)
                if backend == "tcg" and "sep-manifest" in inputs:
                    # Real loader rejects damaged DER before either CPU executes.
                    manifest = inputs["sep-manifest"].read_bytes()
                    for damaged in (manifest[:-1], manifest + b"\0"):
                        invalid = root / "invalid.im4m"
                        invalid.write_bytes(damaged)
                        bad_inputs = {**inputs, "sep-manifest": invalid}
                        bad_machine = "tomacco-vphone," + ",".join(
                            k + "=" + str(v).replace(",", ",,") for k, v in bad_inputs.items())
                        bad_machine += ",sep-role=" + boot_role + ",sep-handoff=" + handoff
                        if measure_manifest:
                            bad_machine += ",sep-measure-manifest=on"
                        bad_command = [*command]
                        bad_command[bad_command.index("-M") + 1] = bad_machine
                        rejected = subprocess.run(bad_command, capture_output=True, timeout=15)
                        assert rejected.returncode != 0
                        assert b"complete DER IM4M envelope" in rejected.stderr
                    checks.append("SEP loader refuses truncated IM4M and trailing data before CPU execution")
    return {"engine_sha256": file_digest(engine), "passed": True, "checks": checks,
            "sep_handoff": handoff if "sep" in inputs else None,
            "sep_manifest_measurement_requested": measure_manifest,
            "loader_manifest_sha256": file_digest(inputs["sep-manifest"]) if "sep-manifest" in inputs else None,
            "scope": "real QEMU virtual iPhone peripherals; firmware CPUs paused",
            "qualified_for_app_development": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--loader-manifest", type=Path,
                        help="Additional IM4M for paused loader tests only; never executed or cryptographically qualified")
    args = parser.parse_args()
    result = json.dumps(exercise(args.engine.resolve(strict=True), args.request, args.loader_manifest), indent=2) + "\n"
    if args.output:
        with args.output.open("x") as stream:
            stream.write(result)
    print(result, end="")
