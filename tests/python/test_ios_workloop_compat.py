from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from ios_paravirt_kernel import assemble  # noqa: E402
import prepare_ios_workloop_compat as compat  # noqa: E402

ADAPTER = ROOT / "integrations/ios-emulator/ios27-workloop-discover.S"
DRIVER = ROOT / "tests/engine/workloop-discover-adapter.S"
ENGINE = ROOT / ".tooling/ios-paravirt/sptm-build/qemu-system-aarch64"


class WorkloopDiscoverAdapterTests(unittest.TestCase):
    """filt_wlupdate must not let an owner discovery (SYNC_WAKE |
    DISCOVER_OWNER | IGNORE_ESTALE) wake a knote that is still waiting in
    SYNC_WAIT: that woke locationd threads as owners of a queue another
    thread owned. Everything else must reach the original
    code with the replaced instruction's result."""

    def test_the_adapter_runs_unchanged_against_every_case(self):
        clang, objcopy = shutil.which("clang"), shutil.which("llvm-objcopy")
        if not clang or not objcopy or not ENGINE.is_file():
            self.skipTest("Needs clang, llvm-objcopy and an engine built by tools/build_ios_paravirt.py")
        # A raw image has no linker: a branch to a global symbol stays an
        # unresolved relocation (a branch to itself), so the symbol is made local.
        adapter = ADAPTER.read_text().replace("original_continue", "workloop_continue").replace(
            "original_success", "workloop_stale").replace(".global tomacco_workloop_discover\n", "")
        with tempfile.TemporaryDirectory(prefix="tomacco-workloop-") as directory:
            source = Path(directory) / "workloop.S"
            # The driver's _start must be the first instruction of the raw image.
            source.write_text(DRIVER.read_text().replace("_start:", "_start:\n\tb\tdriver\ndriver:", 1)
                              .replace("\t.text\n", "\t.text\n", 1) + "\n" + adapter)
            obj, image = Path(directory) / "workloop.o", Path(directory) / "workloop.bin"
            subprocess.run([clang, "--target=aarch64-none-elf", "-march=armv8.1-a", "-c", source, "-o", obj],
                           check=True, timeout=60)
            subprocess.run([objcopy, "-O", "binary", "--only-section=.text", obj, image], check=True, timeout=60)
            done = subprocess.run(
                [ENGINE, "-M", "virt", "-cpu", "max", "-smp", "1", "-accel", "tcg", "-m", "128M",
                 "-nodefaults", "-nic", "none", "-display", "none", "-monitor", "none", "-serial", "stdio",
                 "-device", f"loader,file={image},addr=0x40200000,force-raw=on",
                 "-device", "loader,addr=0x40200000,cpu-num=0"],
                capture_output=True, text=True, timeout=120)
        match = re.search(r"workloop=([0-9a-f]{16})/([0-9a-f]{16})", done.stdout)
        self.assertIsNotNone(match, done.stdout + done.stderr)
        self.assertEqual((int(match[1], 16), int(match[2], 16)), (0, 14))

    def test_the_candidate_changes_one_instruction_and_an_empty_cave(self):
        if not shutil.which("llvm-mc") or not shutil.which("llvm-objcopy"):
            self.skipTest("Needs llvm-mc and llvm-objcopy")
        original = bytearray(0x21a6000)
        original[compat.ENTRY:compat.ENTRY + 8] = compat.ENTRY_BYTES
        original[compat.STALE:compat.STALE + len(compat.STALE_BYTES)] = compat.STALE_BYTES
        address, expected = compat.STALE_ERROR
        original[address:address + 4] = expected
        with tempfile.TemporaryDirectory(prefix="tomacco-workloop-candidate-") as directory:
            import time
            code = assemble(Path(directory), "workloop", ADAPTER.read_text(), compat.CAVE,
                            compat.ENTRY + 4, time.monotonic() + 60, success=compat.STALE)
        candidate = compat.patch(bytes(original), code)
        changed = [i for i in range(len(original)) if candidate[i] != original[i]]
        self.assertTrue(all(compat.ENTRY <= i < compat.ENTRY + 4 or compat.CAVE <= i < compat.CAVE + len(code)
                            for i in changed))
        branch = struct.unpack_from("<I", candidate, compat.ENTRY)[0]
        self.assertEqual(branch >> 26, 0b000101)  # B
        offset = (branch & 0x3ffffff) << 2
        offset -= (1 << 28) if offset & (1 << 27) else 0
        self.assertEqual(compat.ENTRY + offset, compat.CAVE)
        # Its two exits: the ESTALE path and the instruction after the entry.
        exits = []
        for at in range(0, len(code), 4):
            word = struct.unpack_from("<I", code, at)[0]
            if word >> 26 == 0b000101:
                target = (word & 0x3ffffff) << 2
                target -= (1 << 28) if target & (1 << 27) else 0
                exits.append(compat.CAVE + at + target)
        self.assertEqual(exits, [compat.STALE, compat.ENTRY + 4])
        self.assertEqual(code[-8:-4], compat.ENTRY_BYTES[:4])  # the replaced instruction, unchanged
        for broken in (compat.ENTRY, compat.STALE, compat.STALE_ERROR[0], compat.CAVE + 4):
            damaged = bytearray(original)
            damaged[broken] ^= 1
            with self.assertRaises(ValueError):
                compat.patch(bytes(damaged), code)


if __name__ == "__main__":
    unittest.main()
