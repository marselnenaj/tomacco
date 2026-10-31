"""Synthetic fixtures for the strictly bounded local Cryptex kernel policy."""
import hashlib
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time
import unittest

from ios_paravirt_firmware import der_wrap
from ios_paravirt_kernel import (assemble, branch, manifest_name, original_component,
                                patch_kernel, validate_seal)
from test_ios_paravirt import node


def seal(tag=b"cssy"):
    payload = struct.pack("<4I", 2, 0, 1, 32)
    payload += b"".join(bytes([i]) * 32 + bytes(32) for i in (1, 2, 3))
    return der_wrap(0x30, der_wrap(0x16, b"IM4P") + der_wrap(0x16, tag)
                    + der_wrap(0x16, b"0") + der_wrap(4, payload))


class KernelPolicyTests(unittest.TestCase):
    def test_only_original_manifest_metadata_and_supported_seals(self):
        data = seal()
        validate_seal(data, b"cssy")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "components").mkdir()
            (root / "components/seal").write_bytes(data)
            entry = {"Info": {"Path": "seal"}, "Digest": hashlib.sha384(data).digest()}
            self.assertEqual(original_component(root, entry), data)
            (root / "components/seal").write_bytes(data[:-1] + b"x")
            with self.assertRaisesRegex(ValueError, "original iPhone manifest"):
                original_component(root, entry)
        for invalid in (data[:-1], data + b"x", seal(b"casy"),
                        data[:-1] + b"x", data[:21] + bytes(208)):
            with self.subTest(invalid=invalid[:24]), self.assertRaises(ValueError):
                validate_seal(invalid, b"cssy")

    def test_manifest_path_uses_exact_prepared_identity(self):
        tree = node("device-tree", **{"target-sub-type": b"VPHONE600AP\0"})
        tree.children.append(node("chosen", **{"unique-chip-id": struct.pack("<Q", 0x123456)}))
        self.assertEqual(manifest_name(tree.encode()), "apticket.vphone600ap.123456.im4m")
        for bad in (bytes(8), b"short", bytes(9)):
            tree.child("chosen").set("unique-chip-id", bad)
            with self.assertRaisesRegex(ValueError, "device identity"):
                manifest_name(tree.encode())

    def test_wrong_kernel_and_out_of_range_branches_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "exact original"):
            patch_kernel(b"unsupported synthetic kernel", bytes(1000), bytes(28), bytes(80), bytes(24),
                         virtio_used=bytes(20))
        self.assertEqual(branch(0x100, 0xfc), bytes.fromhex("ffffff17"))
        for address in (3, 1 << 27, -(1 << 27) - 4):
            with self.assertRaisesRegex(ValueError, "ARM64 range"):
                branch(0, address)

    @unittest.skipUnless(all(shutil.which(tool) for tool in ("llvm-mc", "llvm-objdump", "llvm-objcopy")),
                         "LLVM assembler tools unavailable")
    def test_assembler_resolves_only_the_expected_continuation(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            code = assemble(out, "good", ".text\npacibsp\nb original_continue\n",
                            0x1000, 0x2000, time.monotonic() + 10)
            self.assertEqual(code, bytes.fromhex("7f2303d5") + branch(0x1004, 0x2000))
            code = assemble(out, "two", ".text\nb original_success\nb original_continue\n",
                            0x1000, 0x2000, time.monotonic() + 10, success=0x3000)
            self.assertEqual(code, branch(0x1000, 0x3000) + branch(0x1004, 0x2000))
            code = assemble(out, "call", ".text\nbl original_cs_blob_get\nb original_continue\n",
                            0x1000, 0x2000, time.monotonic() + 10,
                            calls={"original_cs_blob_get": 0x3000})
            self.assertEqual(code, bytes.fromhex("00080094") + branch(0x1004, 0x2000))
            with self.assertRaisesRegex(ValueError, "relocations"):
                assemble(out, "wrong-kind", ".text\nb original_cs_blob_get\nb original_continue\n",
                         0x1000, 0x2000, time.monotonic() + 10,
                         calls={"original_cs_blob_get": 0x3000})
            with self.assertRaisesRegex(ValueError, "relocations"):
                assemble(out, "bad", ".text\nbl unexpected\nb original_continue\n",
                         0x1000, 0x2000, time.monotonic() + 10)

    @unittest.skipUnless(os.environ.get("TOMACCO_ARM64_TEST_ENGINE"), "Optional ARM64 execution engine unavailable")
    def test_executes_original_arm64_adapters_with_negative_controls(self):
        for count in (2, 3, 4):
            with self.subTest(graphics_hashes=count):
                self.execute_adapters(count)

    def execute_adapters(self, count):
        repo = Path(__file__).resolve().parents[2]
        sources = repo / "integrations/ios-emulator"
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            pins = (sources / "cryptex-seal-pins.S").read_text().replace("original_continue", "pins_continue")
            alias = (sources / "ios27-lockdown-alias.S").read_text().replace("original_continue", "alias_continue")
            container = (sources / "ios27-container-rpc.S").read_text().replace(
                "original_continue", "container_failure").replace("original_success", "container_success")
            gpu = (sources / "ios-gpu-mmap.S").read_text().replace("original_continue", "gpu_continue")
            virtio = (sources / "virtio-used-ring.S").read_text().replace(
                "original_continue", "virtio_continue")
            installer = (sources / "ios-installer-ports.S").read_text().replace(
                "original_continue", "installer_continue").replace("original_success", "installer_success")
            # The adapters run unchanged, with synthetic pin tables and return
            # continuations that record their ABI arguments instead of XNU.
            assembly = f".set tomacco_gpu_hash_count, {count}\n.equ TOMACCO_MANIFEST_BYTES, 23\n"
            assembly += pins + "\n" + alias + "\n" + container + "\n" + gpu + "\n" + virtio + "\n" + installer + """
.p2align 3
os_pin: .fill 229, 1, 0x11
app_pin: .fill 229, 1, 0x22
manifest_pin: .fill 23, 1, 0x33
.p2align 3
allowed_code_hashes: .fill 20, 1, 0x44
                    .fill 20, 1, 0x55
                    .fill 20, 1, 0x66
                    .fill 20, 1, 0x77
installer_code_hash: .fill 20, 1, 0x44
.p2align 2
installer_continue:
ret
installer_success:
mov w0, #77
ret
.global installer_ports_probe
installer_ports_probe:
stp x28, x30, [sp, #-64]!
mov w28, w1
str x2, [sp, #0x28]
bl tomacco_installer_ports
ldp x28, x30, [sp], #64
ret
gpu_continue:
ret
.global gpu_mmap_probe
gpu_mmap_probe:
stp x20, x30, [sp, #-16]!
mov x20, x1
bl tomacco_gpu_mmap
ldp x20, x30, [sp], #16
ret
.global virtio_used_probe
virtio_used_probe:
stp x19, x30, [sp, #-16]!
mov x19, x1
mov x8, x2
bl tomacco_virtio_used_ring
ldp x19, x30, [sp], #16
ret
virtio_continue:
sub w8, w8, #1
bic x0, x9, x8
ret
pins_continue:
adr x8, seen
stp x0, x1, [x8]
stp x2, x3, [x8, #16]
stp x4, x5, [x8, #32]
stp x6, x7, [x8, #48]
mov w0, #77
autibsp
ret
alias_continue:
adr x8, seen
stp x0, x1, [x8]
str x2, [x8, #16]
autibsp
ret
container_failure:
mov x4, #0
b container_record
container_success:
mov x4, #1
container_record:
adr x8, seen
stp x0, x1, [x8]
stp x2, x3, [x8, #16]
mov x0, x4
ret
.global _start
_start:
mov x9, #0x41000000
mov sp, x9
bl run
adr x1, result
str x0, [x1, #8]
mov x0, #0x20
hlt #0xf000
b .
.data
.p2align 3
result: .quad 0x20026, 0
"""
            (out / "adapters.S").write_text(assembly)
            commands = [
                ["llvm-mc", "-triple=aarch64", "-filetype=obj", out / "adapters.S", "-o", out / "adapters.o"],
                ["clang", "--target=aarch64-none-elf", "-O1", "-ffreestanding", "-fno-builtin",
                 "-mgeneral-regs-only", "-fno-stack-protector", f"-DGPU_HASH_COUNT={count}", "-c",
                 repo / "tests/fixtures/ios-kernel-policy-harness.c", "-o", out / "harness.o"],
                [os.environ.get("TOMACCO_ARM64_TEST_LINKER", "ld.lld"), "-Ttext=0x40080000",
                 "--entry=_start", out / "adapters.o", out / "harness.o", "-o", out / "harness.elf"],
                [os.environ["TOMACCO_ARM64_TEST_ENGINE"], "-machine", "virt", "-cpu", "max",
                 "-accel", "tcg", "-m", "64M", "-nodefaults", "-display", "none",
                 "-monitor", "none", "-serial", "none", "-nic", "none",
                 "-semihosting-config", "enable=on,target=native", "-kernel", out / "harness.elf"]]
            for command in commands:
                result = subprocess.run(list(map(str, command)), capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, f"{command[0]}: {result.stdout}\n{result.stderr}")
