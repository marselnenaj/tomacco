import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "tests/engine/branch-bench.S"
PROBE = ROOT / "tests/engine/jump-cache-probe.S"
WFE = ROOT / "tests/engine/wfe-monitor.S"
GRANULE = ROOT / "tests/engine/granule-probe.S"
# The engine tools/build_ios_paravirt.py builds from the repository's series,
# or the one TOMACCO_ENGINE names (e.g. an archived engine binary).
ENGINE = Path(os.environ.get("TOMACCO_ENGINE")
              or ROOT / ".tooling/ios-paravirt/sptm-build/qemu-system-aarch64")
# A stock QEMU is the control: TOMACCO_STOCK_QEMU names one, else the host's.
STOCK = os.environ.get("TOMACCO_STOCK_QEMU") or shutil.which("qemu-system-aarch64")

# What branch-bench.S folds together: a function of the program alone.
BENCH_SUM = 0x8C878D76966B5CFD
# jump-cache-probe.S: the sum of x0 over 100 calls before and after each change.
PROBE_RESULTS = {"pagea": 100, "pageb": 200, "full": 100, "asid": 200,
                 "directc": 300, "directd": 400, "smca": 100, "smcb": 500}
# The CPU properties of qemu-arm-jump-probe, qemu-arm-bti-property and
# qemu-arm-wfe-monitor.
PROBE_MODES = {name: ("-global", f"max-arm-cpu.tomacco-jmp-probe={mode}")
               for name, mode in (("generic", 0), ("lean", 1), ("inline", 2))}
BTI_OFF = ("-global", "max-arm-cpu.tomacco-bti=off")


def wfe_wait(us):
    return ("-global", f"max-arm-cpu.tomacco-wfe-wait-us={us}")


def run(qemu, source, extra=(), cpus=1, timeout=120):
    """Run a bare-metal tests/engine program on `qemu`'s virt machine; return its name=hex results."""
    clang, objcopy = shutil.which("clang"), shutil.which("llvm-objcopy")
    if not clang or not objcopy:
        raise unittest.SkipTest("Needs clang and llvm-objcopy")
    with tempfile.TemporaryDirectory(prefix="tomacco-branch-") as directory:
        obj, image = Path(directory) / "probe.o", Path(directory) / "probe.bin"
        subprocess.run([clang, "--target=aarch64-none-elf", "-march=armv8.3-a", "-c", source, "-o", obj],
                       check=True, timeout=60)
        subprocess.run([objcopy, "-O", "binary", "--only-section=.text", obj, image], check=True, timeout=60)
        done = subprocess.run(
            [str(qemu), "-M", "virt", "-cpu", "max", "-smp", str(cpus),
             "-accel", "tcg,thread=multi" if cpus > 1 else "tcg", "-m", "128M",
             "-nodefaults", "-nic", "none", "-display", "none", "-monitor", "none", "-serial", "stdio",
             "-device", f"loader,file={image},addr=0x40200000,force-raw=on",
             "-device", "loader,addr=0x40200000,cpu-num=0", *extra],
            capture_output=True, text=True, timeout=timeout)
    values = {name: int(value, 16) for name, value in re.findall(r"([a-z0-9]+)=([0-9a-f]{16})", done.stdout)}
    if not values:
        raise AssertionError(f"{qemu} printed {done.stdout!r} {done.stderr[-500:]!r}")
    return values


def engine_has(prop):
    """Whether the engine's CPU has a qemu-arm-* property (an engine without the patch does not)."""
    done = subprocess.run([str(ENGINE), "-device", "max-arm-cpu,help"], capture_output=True, text=True,
                          timeout=10)
    return f"{prop}=" in done.stdout


def accel_statistics(qemu, source, properties):
    """Run a single-CPU tests/engine program to its PSCI SYSTEM_OFF, then read
    accelerator properties over QMP while the machine is paused."""
    clang, objcopy = shutil.which("clang"), shutil.which("llvm-objcopy")
    if not clang or not objcopy:
        raise unittest.SkipTest("Needs clang and llvm-objcopy")
    with tempfile.TemporaryDirectory(prefix="tomacco-branch-") as directory:
        obj, image = Path(directory) / "probe.o", Path(directory) / "probe.bin"
        serial, sock = Path(directory) / "serial", Path(directory) / "qmp.sock"
        subprocess.run([clang, "--target=aarch64-none-elf", "-march=armv8.3-a", "-c", source, "-o", obj],
                       check=True, timeout=60)
        subprocess.run([objcopy, "-O", "binary", "--only-section=.text", obj, image], check=True, timeout=60)
        engine = subprocess.Popen(
            [str(qemu), "-M", "virt", "-cpu", "max", "-smp", "1", "-accel", "tcg", "-m", "128M",
             "-nodefaults", "-nic", "none", "-display", "none", "-monitor", "none", "-serial", f"file:{serial}",
             "-device", f"loader,file={image},addr=0x40200000,force-raw=on",
             "-device", "loader,addr=0x40200000,cpu-num=0", "-action", "shutdown=pause",
             "-qmp", f"unix:{sock},server=on,wait=off"])
        try:
            deadline = time.monotonic() + 120
            while not (serial.is_file() and "sum=" in serial.read_text()):
                if time.monotonic() > deadline or engine.poll() is not None:
                    raise AssertionError(f"{qemu} did not finish {source.name}")
                time.sleep(0.1)
            with socket.socket(socket.AF_UNIX) as connection:
                connection.connect(str(sock))
                stream = connection.makefile("rw")
                def call(command, **arguments):
                    stream.write(json.dumps({"execute": command, "arguments": arguments}) + "\n")
                    stream.flush()
                    while True:
                        reply = json.loads(stream.readline())
                        if "return" in reply or "error" in reply:
                            return reply
                stream.readline()
                call("qmp_capabilities")
                return {prop: call("qom-get", path="/machine/accel", property=prop).get("return")
                        for prop in properties}
        finally:
            engine.kill()
            engine.wait(timeout=10)


class JumpCacheTests(unittest.TestCase):
    """qemu-tcg-jump-cache and qemu-arm-jump-probe change how a TB exit finds
    the next TB (a 64k-entry jump cache flushed through a fill log, probes
    in translated code); qemu-arm-bti-property drops BTYPE from TB flags.
    None of that may change what the guest computes or which code a
    remapped or rewritten page runs."""

    def setUp(self):
        if not ENGINE.is_file():
            self.skipTest("Needs an engine built by tools/build_ios_paravirt.py")

    def configurations(self):
        yield "engine default", ()
        if engine_has("tomacco-jmp-probe"):
            for name, extra in PROBE_MODES.items():
                yield name, extra
        if engine_has("tomacco-bti"):
            yield "bti off", BTI_OFF

    def test_branch_benchmark_computes_the_same_in_every_configuration(self):
        for name, extra in self.configurations():
            with self.subTest(configuration=name):
                self.assertEqual(run(ENGINE, BENCH, extra)["sum"], BENCH_SUM)

    def test_remapped_and_rewritten_code_is_found_in_every_configuration(self):
        # Page TLBI, full TLBI, ASID change, a direct exit to a remapped
        # page and a rewritten instruction: each must run the new code.
        for name, extra in self.configurations():
            with self.subTest(configuration=name):
                results = run(ENGINE, PROBE, extra)
                self.assertEqual({k: results.get(k) for k in PROBE_RESULTS}, PROBE_RESULTS)

    def test_accelerator_reports_jump_cache_statistics(self):
        # For live measurement: misses (found in the TB hash table), absent
        # (translations follow) and full flushes; branch-bench's "wide" and
        # "flush" phases miss, its TLBIs flush.
        stats = accel_statistics(ENGINE, BENCH, ("jmp-cache-misses", "jmp-cache-absent", "jmp-cache-flushes"))
        if stats["jmp-cache-misses"] is None:
            self.skipTest("Engine predates qemu-tcg-jump-cache")
        self.assertGreater(stats["jmp-cache-misses"], 500000, stats)
        self.assertGreater(stats["jmp-cache-absent"], 8192, stats)
        self.assertGreater(stats["jmp-cache-flushes"], 3000, stats)

    def test_stock_qemu_computes_the_same(self):
        if not STOCK or not Path(STOCK).is_file():
            self.skipTest("Needs a stock qemu-system-aarch64 (TOMACCO_STOCK_QEMU or on PATH)")
        self.assertEqual(run(STOCK, BENCH)["sum"], BENCH_SUM)
        results = run(STOCK, PROBE)
        self.assertEqual({k: results.get(k) for k in PROBE_RESULTS}, PROBE_RESULTS)


class TargetPageTests(unittest.TestCase):
    """qemu-arm-target-page-16k: -M ...,tomacco-page-bits=N chooses the target
    page before the board exists (tomacco-vphone otherwise runs with 1 KiB),
    and tomacco-granule-16k=on is a CPU with only the 16 KiB granule, the only
    CPU that accepts 16 KiB target pages: QEMU maps a whole target page from
    one walk, so a 4 KiB translation under a 16 KiB target page whose VA and
    PA differ modulo 16 KiB would run the wrong code (jump-cache-probe.S)."""

    GRANULE_16K = ("-global", "max-arm-cpu.tomacco-granule-16k=on")

    def setUp(self):
        if not ENGINE.is_file():
            self.skipTest("Needs an engine built by tools/build_ios_paravirt.py")
        if not engine_has("tomacco-granule-16k"):
            self.skipTest("Engine predates qemu-arm-target-page-16k")

    @staticmethod
    def page(bits):
        return ("-M", f"tomacco-page-bits={bits}")

    def test_every_target_page_computes_the_same(self):
        for bits, extra in ((10, ()), (12, ()), (14, self.GRANULE_16K)):
            with self.subTest(bits=bits):
                self.assertEqual(run(ENGINE, BENCH, (*self.page(bits), *extra))["sum"], BENCH_SUM)

    def test_4k_page_tables_still_find_remapped_code_at_1k_and_4k_target_pages(self):
        for bits in (10, 12):
            with self.subTest(bits=bits):
                results = run(ENGINE, PROBE, self.page(bits))
                self.assertEqual({k: results.get(k) for k in PROBE_RESULTS}, PROBE_RESULTS)

    def test_a_16k_machine_refuses_a_cpu_with_smaller_granules(self):
        done = subprocess.run([str(ENGINE), "-M", "virt,tomacco-page-bits=14", "-cpu", "max", "-nodefaults",
                               "-display", "none", "-S"], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("requires a smaller page size", done.stderr)
        self.assertIn("tomacco-granule-16k=on", done.stderr)

    def test_the_16k_cpu_reports_only_the_16k_granule(self):
        field = lambda value, shift: (value >> shift) & 0xf
        plain = run(ENGINE, GRANULE)["mmfr0"]
        self.assertNotEqual(field(plain, 28), 0xf)  # -cpu max: 4 KiB implemented
        only = run(ENGINE, GRANULE, (*self.GRANULE_16K, *self.page(14)))["mmfr0"]
        self.assertEqual((field(only, 28), field(only, 24)), (0xf, 0xf))  # TGRAN4, TGRAN64
        self.assertGreaterEqual(field(only, 20), 1)                        # TGRAN16
        self.assertEqual((field(only, 40), field(only, 36)), (1, 1))      # TGRAN4_2, TGRAN64_2
        self.assertIn(field(only, 32), (2, 3))                            # TGRAN16_2
        # Nothing else changes, and the CPU alone does not change the page.
        mask = ~((0xf << 20) | (0xf << 24) | (0xf << 28) | (0xfff << 32)) & (2**64 - 1)
        self.assertEqual(only & mask, plain & mask)
        self.assertEqual(run(ENGINE, GRANULE, self.GRANULE_16K)["mmfr0"], only)

    def test_a_4k_guest_on_the_16k_only_cpu_is_logged(self):
        # The architecture's IMPDEF answer to an unimplemented granule is
        # another one (so jump-cache-probe.S, written for 4 KiB, goes wrong
        # and is ended); the engine says once that it happened.
        clang, objcopy = shutil.which("clang"), shutil.which("llvm-objcopy")
        if not clang or not objcopy:
            self.skipTest("Needs clang and llvm-objcopy")
        with tempfile.TemporaryDirectory() as directory:
            obj, image, log = (Path(directory) / name for name in ("probe.o", "probe.bin", "debug.log"))
            subprocess.run([clang, "--target=aarch64-none-elf", "-march=armv8.3-a", "-c", PROBE, "-o", obj],
                           check=True, timeout=60)
            subprocess.run([objcopy, "-O", "binary", "--only-section=.text", obj, image], check=True, timeout=60)
            engine = subprocess.Popen(
                [str(ENGINE), "-M", "virt,tomacco-page-bits=14", "-cpu", "max", *self.GRANULE_16K,
                 "-accel", "tcg", "-m", "128M", "-nodefaults", "-nic", "none", "-display", "none",
                 "-monitor", "none", "-serial", "null", "-d", "guest_errors", "-D", str(log),
                 "-device", f"loader,file={image},addr=0x40200000,force-raw=on",
                 "-device", "loader,addr=0x40200000,cpu-num=0"])
            try:
                deadline = time.monotonic() + 30
                while "tomacco-granule-16k" not in (log.read_text() if log.is_file() else ""):
                    if time.monotonic() > deadline or engine.poll() is not None:
                        break
                    time.sleep(0.1)
            finally:
                engine.kill()
                engine.wait(timeout=10)
            text = log.read_text()
        self.assertEqual(text.count("tomacco-granule-16k: guest selected a 4 KiB stage-1 granule"), 1, text)

    def test_only_10_12_and_14_bits_are_accepted_and_the_property_reads_back(self):
        for bits in ("13", "16", "4k"):
            with self.subTest(bits=bits):
                done = subprocess.run([str(ENGINE), "-M", f"virt,tomacco-page-bits={bits}", "-nodefaults",
                                       "-display", "none", "-S"], capture_output=True, text=True, timeout=30)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("tomacco-page-bits must be", done.stderr)
        with tempfile.TemporaryDirectory() as directory:
            sock = Path(directory) / "qmp.sock"
            engine = subprocess.Popen([str(ENGINE), "-M", "virt,tomacco-page-bits=14", "-cpu", "max",
                                       *self.GRANULE_16K, "-nodefaults", "-display", "none", "-S",
                                       "-qmp", f"unix:{sock},server=on,wait=off"])
            try:
                deadline = time.monotonic() + 30
                while not sock.exists():
                    if time.monotonic() > deadline or engine.poll() is not None:
                        raise AssertionError("engine did not start")
                    time.sleep(0.05)
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.connect(str(sock))
                    stream = connection.makefile("rw")
                    stream.readline()
                    for command in ({"execute": "qmp_capabilities"},
                                    {"execute": "qom-get",
                                     "arguments": {"path": "/machine", "property": "tomacco-page-bits"}}):
                        stream.write(json.dumps(command) + "\n")
                        stream.flush()
                        reply = json.loads(stream.readline())
                self.assertEqual(reply.get("return"), 14, reply)
            finally:
                engine.kill()
                engine.wait(timeout=10)


class WaitForEventTests(unittest.TestCase):
    """qemu-arm-wfe-monitor: a WFE with an armed exclusive monitor waits for
    the monitored location (bounded) instead of returning at once, so a
    LDAXR/WFE lock waiter stops re-running its loop. It must still wake on
    the store, on SEV and at its bound, and see a store made before it."""

    US = 1e6

    def setUp(self):
        if not ENGINE.is_file():
            self.skipTest("Needs an engine built by tools/build_ios_paravirt.py")
        if not engine_has("tomacco-wfe-wait-us"):
            self.skipTest("Engine predates qemu-arm-wfe-monitor")

    def probe(self, qemu=None, extra=()):
        r = run(qemu or ENGINE, WFE, extra, cpus=2)
        self.assertEqual(r["rounds"], 200, r)
        tick = r["cntfrq"] / self.US
        return {"wfes": r["wfes"] / r["rounds"], "latency": r["latency"] / r["rounds"] / tick,
                "timeout": r["timeout"] / tick, "changed": r["changed"] / tick, "sev": r["sev"] / tick}

    def test_a_lock_waiter_waits_instead_of_spinning(self):
        # CPU 0 holds the lock 200 us per round: upstream QEMU's waiter runs
        # thousands of WFEs per round, a 50 us bound about four.
        r = self.probe()
        self.assertLess(r["wfes"], 50, r)
        # The release wakes the waiter (the store, not the bound).
        self.assertLess(r["latency"], 40, r)
        # A store between the LDAXR and the WFE ends the wait at once
        # (the time includes translating the probe).
        self.assertLess(r["changed"], 40, r)

    def test_the_wait_is_bounded_and_sev_ends_it(self):
        r = self.probe(extra=wfe_wait(2000))
        # Nobody writes the word: the WFE returns at its bound, not never.
        self.assertGreater(r["timeout"], 1500, r)
        self.assertLess(r["timeout"], 100000, r)
        # SEV 20 us after the waiter armed: woken by it, long before 2 ms.
        self.assertLess(r["sev"], 1000, r)

    def test_zero_restores_upstreams_spin(self):
        r = self.probe(extra=wfe_wait(0))
        self.assertGreater(r["wfes"], 100, r)
        self.assertLess(r["timeout"], 1000, r)

    def test_stock_qemu_spins(self):
        if not STOCK or not Path(STOCK).is_file():
            self.skipTest("Needs a stock qemu-system-aarch64 (TOMACCO_STOCK_QEMU or on PATH)")
        r = self.probe(STOCK)
        self.assertGreater(r["wfes"], 100, r)


if __name__ == "__main__":
    unittest.main()
