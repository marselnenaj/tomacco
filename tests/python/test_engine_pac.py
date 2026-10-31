import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "tests/engine/pac-probe.S"
SWEEP = ROOT / "tests/engine/pac-sweep.S"
# The engine tools/build_ios_paravirt.py builds from the repository's series,
# or the one TOMACCO_ENGINE names (e.g. an archived engine binary).
ENGINE = Path(os.environ.get("TOMACCO_ENGINE")
              or ROOT / ".tooling/ios-paravirt/sptm-build/qemu-system-aarch64")
# A stock QEMU is the control: TOMACCO_STOCK_QEMU names one, else the host's.
STOCK = os.environ.get("TOMACCO_STOCK_QEMU") or shutil.which("qemu-system-aarch64")

P = 0x0000001234567890  # a user pointer: bit 55 clear, no PAC bits set
K = 0xFFFFFF8012345678  # a kernel pointer: bit 55 and every PAC bit set
PAC_FAIL = 0x1C  # ESR_EL1.EC of a FEAT_FPAC authentication failure
# Bits 63:39 except 55 carry the PAC of a 39-bit pointer without TBI.
PAC_BITS = ((1 << 64) - (1 << 39)) & ~(1 << 55)


# The engine's switch back to the per-instruction helpers
# (qemu-arm-pauth-bypass-inline's tomacco-pauth-fast CPU property).
HELPERS = ("-global", "max-arm-cpu.tomacco-pauth-fast=off")


def run(qemu, source, extra=()):
    """Run a bare-metal tests/engine program on `qemu`'s virt machine; return what it printed."""
    clang, objcopy = shutil.which("clang"), shutil.which("llvm-objcopy")
    if not clang or not objcopy:
        raise unittest.SkipTest("Needs clang and llvm-objcopy")
    with tempfile.TemporaryDirectory(prefix="tomacco-pac-") as directory:
        obj, image = Path(directory) / "pac.o", Path(directory) / "pac.bin"
        subprocess.run([clang, "--target=aarch64-none-elf", "-march=armv8.3-a", "-c", source, "-o", obj],
                       check=True, timeout=60)
        subprocess.run([objcopy, "-O", "binary", "--only-section=.text", obj, image], check=True, timeout=60)
        return subprocess.run(
            [str(qemu), "-M", "virt", "-cpu", "max", "-smp", "1", "-accel", "tcg", "-m", "128M",
             "-nodefaults", "-nic", "none", "-display", "none", "-monitor", "none", "-serial", "stdio",
             "-device", f"loader,file={image},addr=0x40200000,force-raw=on",
             "-device", "loader,addr=0x40200000,cpu-num=0", *extra],
            capture_output=True, text=True, timeout=120)


def probe(qemu, extra=()):
    """Run tests/engine/pac-probe.S on `qemu` and return its results by name."""
    done = run(qemu, SOURCE, extra)
    results = {name: int(value, 16) for name, value in re.findall(r"([a-z]+)=([0-9a-f]{16})", done.stdout)}
    if len(results) != 13:
        raise AssertionError(f"probe printed {sorted(results)}: {done.stdout!r} {done.stderr[-500:]!r}")
    return results


def sweep(qemu, extra=()):
    """Run tests/engine/pac-sweep.S on `qemu`; its checksums in entry order."""
    done = run(qemu, SWEEP, extra)
    found = re.findall(r"c([0-9])=([0-9a-f]{16})", done.stdout)
    if [int(index) for index, _ in found] != list(range(len(SWEEP_CONFIGS))):
        raise AssertionError(f"sweep printed {done.stdout!r} {done.stderr[-500:]!r}")
    return [int(value, 16) for _, value in found]


EN_IA, EN_IB, EN_DA, EN_DB = 1 << 31, 1 << 30, 1 << 27, 1 << 13
# pac-sweep.S's `configs`: TCR_EL1 and the SCTLR_EL1 key bits of each entry.
SWEEP_CONFIGS = [
    (25 | (25 << 16) | (2 << 30), EN_IA | EN_IB | EN_DA | EN_DB),
    (17 | (2 << 14) | (25 << 16) | (1 << 30) | (1 << 37), EN_IA | EN_DA),
    (10 | (50 << 16) | (2 << 30) | (1 << 38) | (1 << 52), EN_IB | EN_DB),
    (25 | (1 << 14) | (13 << 16) | (3 << 30) | (3 << 37) | (1 << 51), EN_IA | EN_IB | EN_DA | EN_DB),
    (25 | (25 << 16) | (2 << 30), 0),
    (25 | (25 << 16) | (2 << 30) | (3 << 37), EN_IA),
    (25 | (25 << 16) | (2 << 30), EN_IA | EN_IB | EN_DA | EN_DB),
]
M64 = (1 << 64) - 1


def pac_field(tcr, select, data):
    """pauth_ptr_mask(aa64_va_parameters()) at EL1 for -cpu max (FEAT_TTST, FEAT_LVA, no MTE)."""
    tsz = (tcr >> (16 if select else 0)) & 0x3F
    granule_64k = ((tcr >> 30) & 3) == 3 if select else ((tcr >> 14) & 3) == 1
    tsz = min(max(tsz, 12 if granule_64k else 16), 48 - granule_64k)
    tbi = (tcr >> (37 + select)) & 1
    if not data:
        tbi &= ~(tcr >> (51 + select)) & 1
    bottom, top = 64 - tsz, 64 - 8 * tbi
    return ((1 << (top - bottom)) - 1) << bottom


def strip(tcr, x, data):
    """pauth_original_ptr: the PAC field refilled from bit 55."""
    select = (x >> 55) & 1
    field = pac_field(tcr, select, data)
    return x | field if select else x & ~field & M64


def sweep_model():
    """pac-sweep.S's checksums under the bypass: PAC* is the identity, AUT*
    strips while its key is enabled, XPAC* strips while any key is."""
    state, sums = 0x1234, []
    for tcr, keys in SWEEP_CONFIGS:
        total = 0
        def fold(value):
            nonlocal total
            total = (((total >> 7) | (total << 57)) & M64) ^ value
        aut = lambda x, key, data: strip(tcr, x, data) if keys & key else x
        xpac = lambda x, data: strip(tcr, x, data) if keys else x
        for _ in range(64):
            state = (state * 0x5851F42D4C957F2D + 0x14057B7EF767814F) & M64
            x = state
            state = (state * 0x5851F42D4C957F2D + 0x14057B7EF767814F) & M64
            for value in (aut(x, EN_IA, False), aut(x, EN_IB, False), aut(x, EN_DA, True),
                          aut(x, EN_DB, True), aut(x, EN_IA, False), aut(x, EN_DB, True),
                          xpac(x, False), xpac(x, True), x, x,
                          aut(x, EN_IA, False), aut(x, EN_IB, False), xpac(x, False), x):
                fold(value)
            if keys & EN_DA:
                fold(0x0123456789ABCDEF)  # LDRAA of `cell` through a pointer with PAC bits
        sums.append(total)
    return sums


class EnginePointerAuthenticationTests(unittest.TestCase):
    """The research engine (SPTM base) bypasses pointer authentication: docs/legal.md
    documents that, and this test pins it so a change in the engine's behaviour is noticed. A stock
    QEMU, when one is available, shows what the architecture does instead."""

    def test_engine_bypass_is_exactly_the_documented_bound(self):
        if not ENGINE.is_file():
            self.skipTest("Needs an engine built by tools/build_ios_paravirt.py")
        r = probe(ENGINE)
        # pac* is the identity for user and kernel pointers alike.
        self.assertEqual(r["pac"], P)
        self.assertEqual(r["dpac"], P)
        self.assertEqual(r["kpac"], K)
        # aut* never fails: a signed, an unsigned and a wrong-key pointer all
        # come back as the pointer with its tag bits refilled from bit 55.
        self.assertEqual(r["aut"], P)
        self.assertEqual(r["badaut"], P)
        self.assertEqual(r["daut"], P)
        self.assertEqual(r["dbad"], P)
        self.assertEqual(r["wrongkey"], P)
        self.assertEqual(r["faults"], 0)
        # xpac* refills the tag bits from bit 55.
        self.assertEqual(r["xpac"], P)
        self.assertEqual(r["kxpac"], K)
        # pacga computes a real code.
        self.assertNotEqual(r["pacga"], 0)
        self.assertEqual(r["pacga"] & 0xFFFFFFFF, 0)  # a PACGA result keeps its low half zero

    def test_engine_bypass_holds_for_every_address_shape_and_key_set(self):
        # Address size per half (clamped values included), granules, TBI/TBID
        # and the enabled keys all change what AUT* and XPAC* strip; every TCR
        # or SCTLR write in between must reach the instructions after it.
        if not ENGINE.is_file():
            self.skipTest("Needs an engine built by tools/build_ios_paravirt.py")
        self.assertEqual(sweep(ENGINE), sweep_model())

    def test_engine_helper_free_bypass_matches_its_helpers(self):
        # qemu-arm-pauth-bypass-inline evaluates the bypass in translated code;
        # its tomacco-pauth-fast=off switch restores the helpers of earlier
        # engines. Both must give the same results.
        if not ENGINE.is_file():
            self.skipTest("Needs an engine built by tools/build_ios_paravirt.py")
        done = run(ENGINE, SWEEP, HELPERS)
        if "tomacco-pauth-fast" in done.stderr:
            self.skipTest("Engine predates qemu-arm-pauth-bypass-inline")
        self.assertEqual(sweep(ENGINE, HELPERS), sweep(ENGINE))
        self.assertEqual(probe(ENGINE, HELPERS), probe(ENGINE))

    def test_stock_qemu_authenticates_where_the_engine_does_not(self):
        if not STOCK or not Path(STOCK).is_file():
            self.skipTest("Needs a stock qemu-system-aarch64 (TOMACCO_STOCK_QEMU or on PATH)")
        r = probe(STOCK)
        # Signing changes the PAC bits and leaves the pointer bits alone.
        self.assertNotEqual(r["pac"], P)
        self.assertEqual(r["pac"] & ~PAC_BITS, P)
        self.assertNotEqual(r["dpac"], P)
        self.assertNotEqual(r["kpac"], K)
        # A correct authentication restores the pointer; stripping does too.
        self.assertEqual(r["aut"], P)
        self.assertEqual(r["daut"], P)
        self.assertEqual(r["xpac"], P)
        self.assertEqual(r["kxpac"], K)
        self.assertNotEqual(r["pacga"], 0)
        # An unsigned or wrong-key pointer fails: a PAC-fail exception
        # (FEAT_FPAC) or an error code in the PAC field, never the pointer.
        failed = r["faults"]
        if failed:
            self.assertEqual(r["esr"] >> 26, PAC_FAIL)
            self.assertEqual(failed, 3)  # badaut, dbad, wrongkey
        else:
            for name in ("badaut", "dbad", "wrongkey"):
                self.assertNotEqual(r[name], P, name)
                self.assertEqual(r[name] & ~PAC_BITS, P, name)


if __name__ == "__main__":
    unittest.main()
