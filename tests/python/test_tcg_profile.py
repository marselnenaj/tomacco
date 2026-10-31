"""The TCG profile plugin (integrations/ios-emulator/tcg-profile.c) on the
engine, and tools/ios_tcg_profile.py's build, windows and image mapping."""
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import ios_tcg_profile as profiler

SOURCE = ROOT / "tests/engine/tcg-profile-mix.S"
# The engine tools/build_ios_paravirt.py builds from the repository's series,
# or the one TOMACCO_ENGINE names (e.g. an archived engine binary).
ENGINE = Path(os.environ.get("TOMACCO_ENGINE")
              or ROOT / ".tooling/ios-paravirt/sptm-build/qemu-system-aarch64")
LOOPS = 1000  # tcg-profile-mix.S
LOAD = 0x40200000
SPIN = 0x40300000


class Engine:
    """Builds the plugin and the probe once; runs the probe on the engine."""
    directory = plugin = image = None

    @classmethod
    def prepare(cls, case):
        if cls.plugin:
            return
        clang, objcopy = shutil.which("clang"), shutil.which("llvm-objcopy")
        if not clang or not objcopy or not ENGINE.is_file():
            case.skipTest("Needs clang, llvm-objcopy and an engine built by tools/build_ios_paravirt.py")
        try:
            profiler.include_directory()
        except ValueError as error:
            case.skipTest(str(error))
        cls.directory = tempfile.TemporaryDirectory(prefix="tomacco-tcg-profile-")
        root = Path(cls.directory.name)
        cls.plugin = Path(profiler.build_plugin(cache=root / "cache")["plugin"])
        obj, cls.image = root / "mix.o", root / "mix.bin"
        subprocess.run([clang, "--target=aarch64-none-elf", "-march=armv8.3-a", "-c", SOURCE, "-o", obj],
                       check=True, timeout=60)
        subprocess.run([objcopy, "-O", "binary", "--only-section=.text", obj, cls.image], check=True, timeout=60)

    @classmethod
    def run(cls, report, spin=0, **options):
        argument = profiler.plugin_argument(cls.plugin, report, **options)
        done = subprocess.run(
            [str(ENGINE), "-M", "virt", "-cpu", "max", "-smp", "1", "-accel", "tcg", "-m", "128M",
             "-nodefaults", "-nic", "none", "-display", "none", "-monitor", "none", "-serial", "stdio",
             "-device", f"loader,file={cls.image},addr={LOAD:#x},force-raw=on",
             "-device", f"loader,addr={SPIN:#x},data={spin},data-len=4",
             "-device", f"loader,addr={LOAD:#x},cpu-num=0", "-plugin", argument],
            capture_output=True, text=True, timeout=120)
        if "faults=0000000000000000" not in done.stdout:
            raise AssertionError(f"probe printed {done.stdout!r} {done.stderr[-800:]!r}")
        return profiler.load(report)


def special(report, name):
    return next((s for s in report["special"] if s["name"] == name), {"count": 0})


class PluginTests(unittest.TestCase):
    def setUp(self):
        Engine.prepare(self)
        temporary = tempfile.TemporaryDirectory(prefix="tomacco-tcg-run-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_each_counted_instruction_executes_once_per_iteration(self):
        report = Engine.run(self.root / "mix.json")
        self.assertEqual(report["kind"], "final")
        self.assertEqual(report["vcpus"], 1)
        for name in ("DC ZVA", "MRS CNTVCT_EL0", "DMB ISH", "SVC #0x80", "ERET", "MRS ESR_EL1"):
            self.assertEqual(special(report, name)["count"], LOOPS, name)
        user = report["classes"]["user"]
        # The loop body: one plain load and store per iteration (the rest
        # come from the start-up and print code), one LSE atomic, an
        # exclusive pair, an acquire/release pair, scalar FP, integer SIMD
        # and FP SIMD.
        self.assertEqual(user["atomic"], LOOPS)
        self.assertEqual(user["exclusive"], 2 * LOOPS)
        self.assertEqual(user["ordered"], 2 * LOOPS)
        self.assertEqual(user["simd_fp"], LOOPS)
        self.assertGreaterEqual(user["fp"], LOOPS)
        self.assertGreaterEqual(user["simd"], LOOPS)
        self.assertGreaterEqual(user["load"], LOOPS)
        self.assertEqual(report["totals"]["kernel_insns"], 0)  # PCs below bit 63
        # The loop's first block is the hottest one with ten instructions.
        top = report["top_blocks"][0]
        self.assertEqual(top["tb_execs"], LOOPS)
        self.assertEqual(top["per_vcpu"], [LOOPS])
        self.assertTrue(LOAD <= int(top["pc"], 16) < LOAD + 0x1000)
        self.assertEqual(top["mix"]["atomic"], 1)
        self.assertIn("DC ZVA", top["special"])
        self.assertNotIn("code", top)
        # Every SVC is a synchronous exception taken from the next PC.
        vcpu = report["per_vcpu"][0]
        self.assertEqual(vcpu["exceptions"], LOOPS)
        self.assertEqual(vcpu["vectors"]["exception:current-spx-sync"], LOOPS)
        self.assertEqual(vcpu["hostcalls"], 1)  # PSCI SYSTEM_OFF
        origin = report["exception_pcs"][0]
        self.assertEqual((origin["count"], origin["vector"]), (LOOPS, "current-spx-sync"))
        svc = special(report, "SVC #0x80")["top"][0]
        self.assertEqual(svc["count"], LOOPS)
        self.assertTrue(int(svc["pc"], 16) < int(origin["from_pc"], 16) < int(svc["pc"], 16) + 0x40)
        # Pages add up to the total.
        self.assertEqual(sum(p["insns"] for p in report["pages"]), report["totals"]["insns"])

    def test_code_option_adds_the_hot_blocks_instruction_words(self):
        report = Engine.run(self.root / "code.json", code=True)
        top = report["top_blocks"][0]
        self.assertTrue(report["code_included"])
        words = top["code"].split()
        self.assertEqual(len(words), top["insns_per_tb"])
        data = b"".join(int(word, 16).to_bytes(4, "little") for word in words)
        self.assertEqual(profiler.fnv1a(data), int(top["hash"], 16))  # the hash the images are matched by

    def test_marks_and_intervals_write_windows_that_add_up(self):
        report = self.root / "spin.json"
        profiler.mark(report, "before the loop")
        final = Engine.run(report, spin=400_000_000, interval=0.2)
        windows = sorted(self.root.glob("spin.window-*.json"))
        self.assertGreaterEqual(len(windows), 2, "the spin should outlast two windows")
        loaded = [profiler.load(path) for path in windows]
        self.assertEqual(loaded[0]["label"], "before the loop")
        self.assertFalse(Path(profiler.profile_paths(report)["mark"]).exists())  # consumed
        self.assertEqual([w["window"] for w in loaded], list(range(1, len(loaded) + 1)))
        self.assertTrue(all(w["kind"] == "window" for w in loaded))
        for previous, current in zip(loaded, loaded[1:]):
            self.assertAlmostEqual(previous["end_seconds"], current["start_seconds"], places=3)
        executed = sum(w["totals"]["tb_execs"] for w in loaded)
        self.assertGreater(executed, 0)
        self.assertLessEqual(executed, final["totals"]["tb_execs"])
        # The measured loop ran before the spin, so DC ZVA is in windows only
        # once in all (or not at all if it ran before the first poll).
        self.assertLessEqual(sum(special(w, "DC ZVA")["count"] for w in loaded), LOOPS)
        self.assertEqual(special(final, "DC ZVA")["count"], LOOPS)

    def test_options_are_refused_by_the_plugin(self):
        bad = profiler.plugin_argument(Engine.plugin, self.root / "bad.json") + ",top=0"
        done = subprocess.run([str(ENGINE), "-M", "virt", "-cpu", "max", "-display", "none",
                               "-nodefaults", "-plugin", bad], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("tomacco-tcg-profile: invalid option top=0", done.stderr)


def macho(segments, entries=(), file_type=0xc):
    """A synthetic little-endian Mach-O header: segments (name, vmaddr,
    vmsize, fileoff, filesize, initprot) and fileset entries (name, vmaddr,
    fileoff). No Apple code."""
    commands = b""
    for name, vmaddr, vmsize, fileoff, filesize, prot in segments:
        commands += struct.pack("<II16sQQQQiiII", 0x19, 72, name.encode(), vmaddr, vmsize, fileoff,
                                filesize, prot, prot, 0, 0)
    for name, vmaddr, fileoff in entries:
        raw = name.encode() + b"\0"
        size = (32 + len(raw) + 7) & ~7
        commands += struct.pack("<IIQQII", 0x80000035, size, vmaddr, fileoff, 32, 0) + raw.ljust(size - 32, b"\0")
    count = len(segments) + len(entries)
    return struct.pack("<IiiIIIII", 0xfeedfacf, 0x0100000c, 2, file_type, count, len(commands), 0, 0) + commands


class ImageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tomacco-tcg-image-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def kernelcache(self, aggregate=True):
        """Two fileset entries; their code is a deterministic pseudo-random
        stream, so every block's hash is unique in the file. Without
        `aggregate` the file's own header has no executable segment."""
        base, text = 0xfffffe0007004000, 0x10000
        code = bytearray()
        state = 0x1234
        for _ in range(0x20000 // 4):
            state = (state * 1103515245 + 12345) & 0xffffffff
            code += state.to_bytes(4, "little")
        top = macho([("__TEXT", base, 0x4000, 0, 0x4000, 1)] +
                    ([("__TEXT_EXEC", base + 0x4000, 0x20000, text, 0x20000, 5)] if aggregate else []),
                    [("com.example.kernel", base, 0x1000), ("com.example.driver", base + 0x14000, 0x2000)])
        kernel = macho([("__TEXT", base, 0x4000, 0, 0x4000, 1),
                        ("__TEXT_EXEC", base + 0x4000, 0x10000, text, 0x10000, 5)], file_type=2)
        driver = macho([("__TEXT_EXEC", base + 0x14000, 0x10000, text + 0x10000, 0x10000, 5)], file_type=0xb)
        data = bytearray(text + len(code))
        data[:len(top)] = top
        data[0x1000:0x1000 + len(kernel)] = kernel
        data[0x2000:0x2000 + len(driver)] = driver
        data[text:] = code
        path = self.root / "kernelcache"
        path.write_bytes(data)
        return path, base, bytes(code)

    def blocks(self, base, code, slide, offsets):
        return [{"pc": "0x%016x" % (base + 0x4000 + offset + slide), "bytes": 24,
                 "hash": "0x%08x" % profiler.fnv1a(code[offset:offset + 24])} for offset in offsets]

    def test_slide_is_found_from_block_hashes_and_pcs_name_their_entry(self):
        path, base, code = self.kernelcache()
        slide = 0x3c8000
        blocks = self.blocks(base, code, slide, (0x40, 0x1238, 0x10a0c, 0x1f000))
        locator = profiler.Locator([("kernel", path)], blocks)
        self.assertEqual(locator.receipt()["images"][0]["slide"], hex(slide))
        self.assertEqual(locator.receipt()["images"][0]["matched_blocks"], 4)
        where = locator.where(base + 0x4000 + 0x1238 + slide)
        self.assertEqual(where, f"com.example.kernel __TEXT_EXEC+0x1238 (unslid 0x{base + 0x4000 + 0x1238:x})")
        self.assertTrue(locator.where(base + 0x14000 + 0x20 + slide).startswith("com.example.driver __TEXT_EXEC+0x20 "))
        self.assertEqual(locator.image_of(base + 0x14000 + slide), "com.example.driver")
        self.assertEqual(locator.where(0x1a0000000), "dyld shared region")
        self.assertEqual(locator.where(0x100004000), "user image below the shared region")

    def test_entries_own_segments_serve_when_the_header_has_no_executable_one(self):
        path, base, code = self.kernelcache(aggregate=False)
        blocks = self.blocks(base, code, 0x10000, (0x80, 0x12000))
        locator = profiler.Locator([("kernel", path)], blocks)
        self.assertEqual(locator.receipt()["images"][0]["slide"], hex(0x10000))
        self.assertTrue(locator.where(base + 0x4000 + 0x12000 + 0x10000).startswith(
            "com.example.driver __TEXT_EXEC+0x2000 "))

    def test_unmatched_blocks_leave_the_image_unplaced(self):
        path, base, code = self.kernelcache()
        blocks = [{"pc": "0xfffffe0007008000", "bytes": 24, "hash": "0x00000001"},
                  {"pc": "0xfffffe0007009000", "bytes": 24, "hash": "0x00000002"}]
        locator = profiler.Locator([("kernel", path)], blocks)
        self.assertIsNone(locator.receipt()["images"][0]["slide"])
        self.assertIn("kernel: no slide matched", locator.notes[0])
        self.assertEqual(locator.where(0xfffffe0007008000), "kernel half (no image)")
        # A known slide can be given instead.
        given = profiler.Locator([("kernel", path)], blocks, {"kernel": 0x4000})
        self.assertTrue(given.where(base + 0x4000 + 0x4000).startswith("com.example.kernel __TEXT_EXEC+0x0 "))

    def test_report_renders_windows_and_summary(self):
        path, base, code = self.kernelcache()
        slide = 0x8000
        top = []
        for rank, block in enumerate(self.blocks(base, code, slide, (0x100, 0x2000, 0x11000)), 1):
            top.append({**block, "rank": rank, "space": "kernel", "tb_execs": 100, "insns_per_tb": 6,
                        "insns": 600, "share": 1 / 3, "cumulative": rank / 3, "translations": 1,
                        "per_vcpu": [100], "mix": {"load": 2, "int": 4}, "special": ["MRS CNTVCT_EL0"]})
        report = {"format": "tomacco-tcg-profile", "version": 1, "kind": "final", "label": "", "start_seconds": 0,
                  "end_seconds": 2, "seconds": 2, "vcpus": 1, "blocks": 3, "executed_blocks": 3, "page_bits": 12,
                  "estimate": "synthetic", "code_included": False,
                  "totals": {"tb_execs": 300, "insns": 1800, "kernel_insns": 1800, "user_insns": 0,
                             "overflow_tb_execs": 0, "overflow_insns": 0},
                  "per_vcpu": [{"vcpu": 0, "tb_execs": 300, "insns": 1800, "kernel_insns": 1800, "user_insns": 0,
                                "idle_seconds": 1, "idle_entries": 3, "interrupts": 5, "exceptions": 2,
                                "hostcalls": 0, "vectors": {}}],
                  "classes": {"kernel": {"load": 600, "int": 1200}, "user": {}},
                  "special": [{"name": "MRS CNTVCT_EL0", "count": 300, "kernel": 300, "user": 0,
                               "top": [{"pc": top[0]["pc"], "count": 100}]}],
                  "exception_pcs": [{"from_pc": top[1]["pc"], "space": "kernel", "vector": "current-spx-sync",
                                     "count": 2}],
                  "exception_pcs_overflow": 0, "pages_total": 1,
                  "pages": [{"page": "0x%016x" % (int(top[0]["pc"], 16) & ~0xfff), "space": "kernel",
                             "insns": 1800, "share": 1, "tb_execs": 300, "blocks": 3}],
                  "top_blocks": top}
        profile = self.root / "tcg-profile.json"
        profile.write_text(json.dumps(report))
        (self.root / "tcg-profile.window-0001.json").write_text(json.dumps({**report, "kind": "window",
                                                                              "window": 1, "label": "swipe"}))
        summary = profiler.render_files(profile, [("kernel", path), ("txm", self.root / "absent")])
        self.assertEqual(summary["images"]["images"][0]["slide"], hex(slide))
        self.assertTrue(summary["top_blocks"][0]["where"].startswith("com.example.kernel __TEXT_EXEC+0x100 "))
        self.assertEqual(summary["top_block_insns_by_image"],
                         [("com.example.kernel", 1200), ("com.example.driver", 600)])
        text = (self.root / "tcg-profile.txt").read_text()
        self.assertIn("MRS CNTVCT_EL0", text)
        self.assertIn("com.example.driver __TEXT_EXEC+0x1000", text)
        self.assertIn("txm:", text)  # the missing image is a note, not a failure
        window = (self.root / "tcg-profile.window-0001.txt").read_text()
        self.assertIn("window 1 'swipe'", window)


class HelperTests(unittest.TestCase):
    def test_paths_and_plugin_argument(self):
        paths = profiler.profile_paths("/run/out/tcg-profile.json")
        self.assertEqual(paths, {"report": "/run/out/tcg-profile.json", "mark": "/run/out/tcg-profile.mark",
                                 "windows": "/run/out/tcg-profile.window-*.json", "text": "/run/out/tcg-profile.txt"})
        self.assertEqual(profiler.plugin_argument("/p,q/lib.so", "/r/a,b.json", interval=2.5, top=10, code=True),
                         "file=/p,,q/lib.so,out=/r/a,,b.json,interval=2.5,top=10,code=on")

    def test_mark_writes_a_single_printable_label_line(self):
        with tempfile.TemporaryDirectory() as directory:
            target = profiler.mark(Path(directory) / "p.json", "swipe 1\n\x00left")
            self.assertEqual(target.read_text(), "swipe 1left\n")
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["p.mark"])

    def test_build_is_cached_by_content(self):
        if not shutil.which("cc") and not shutil.which("gcc"):
            self.skipTest("Needs a C compiler")
        try:
            profiler.include_directory()
        except ValueError as error:
            self.skipTest(str(error))
        with tempfile.TemporaryDirectory() as directory:
            first = profiler.build_plugin(cache=directory)
            modified = Path(first["plugin"]).stat().st_mtime_ns
            time.sleep(0.01)
            second = profiler.build_plugin(cache=directory)
            self.assertEqual(first, second)
            self.assertEqual(Path(second["plugin"]).stat().st_mtime_ns, modified)
            self.assertEqual(first["identity"]["plugin_api_version"], 7)


if __name__ == "__main__":
    unittest.main()


class ShippedHeaderTests(unittest.TestCase):
    def test_the_shipped_plugin_header_is_the_pinned_engines(self):
        # An installed copy builds the plugin against the shipped header.
        import ios_tcg_profile as profile
        shipped = (profile.SHIPPED_INCLUDE / "qemu-plugin.h").read_bytes()
        pinned = profile.PINNED_INCLUDE / "qemu-plugin.h"
        if pinned.is_file():
            self.assertEqual(shipped, pinned.read_bytes())
        self.assertIn(b"SPDX-License-Identifier: GPL-2.0-or-later", shipped)

