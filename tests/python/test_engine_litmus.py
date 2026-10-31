import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "tests/engine/litmus-store-release-load-acquire.S"
# The engine tools/build_ios_paravirt.py builds from the repository's series,
# or the one TOMACCO_ENGINE names (e.g. an archived engine binary).
ENGINE = Path(os.environ.get("TOMACCO_ENGINE")
              or ROOT / ".tooling/ios-paravirt/sptm-build/qemu-system-aarch64")


class StoreReleaseLoadAcquireTests(unittest.TestCase):
    """AArch64 orders a Load-Acquire after an earlier Store-Release (RCsc);
    compilers build seq_cst atomics on that. Without the series' barrier an
    x86 host lets the load pass the store: about 14,000 of 2,000,000 rounds
    of this test read 0 on both CPUs."""

    def test_no_round_reads_zero_on_both_cpus(self):
        clang, objcopy = shutil.which("clang"), shutil.which("llvm-objcopy")
        if not clang or not objcopy or not ENGINE.is_file():
            self.skipTest("Needs clang, llvm-objcopy and an engine built by tools/build_ios_paravirt.py")
        with tempfile.TemporaryDirectory(prefix="tomacco-litmus-") as directory:
            image = Path(directory) / "litmus.bin"
            obj = Path(directory) / "litmus.o"
            subprocess.run([clang, "--target=aarch64-none-elf", "-march=armv8.1-a", "-c", SOURCE, "-o", obj],
                           check=True, timeout=60)
            subprocess.run([objcopy, "-O", "binary", "--only-section=.text", obj, image], check=True, timeout=60)
            # QEMU's virt machine keeps its device tree at the start of RAM.
            done = subprocess.run(
                [ENGINE, "-M", "virt", "-cpu", "max", "-smp", "2", "-accel", "tcg,thread=multi", "-m", "128M",
                 "-nodefaults", "-nic", "none", "-display", "none", "-monitor", "none", "-serial", "stdio",
                 "-device", f"loader,file={image},addr=0x40200000,force-raw=on",
                 "-device", "loader,addr=0x40200000,cpu-num=0"],
                capture_output=True, text=True, timeout=300)
        match = re.search(r"forbidden=([0-9a-f]{16})", done.stdout)
        self.assertIsNotNone(match, done.stdout + done.stderr)
        self.assertEqual(int(match[1], 16), 0)


if __name__ == "__main__":
    unittest.main()
