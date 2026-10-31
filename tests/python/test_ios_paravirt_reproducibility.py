"""The engine build's reproducibility options: path maps, configure pins, the translator link, the expected digest."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import build_ios_paravirt as build  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
PREPARED = REPO / ".tooling/ios-paravirt"
QUALIFIED_PINS = REPO / "integrations/ios-emulator/qemu-configure-pins.json"

# The shapes QEMU's meson.build uses to tie a host lookup to a feature option.
MESON_BUILD = """
pam = cc.find_library('pam', has_headers: ['security/pam_appl.h'],
                      required: get_option('auth_pam'))
canokey = dependency('canokey-qemu', required: get_option('canokey'))
gio = dependency('gio-2.0', required: get_option('gio'), method: 'pkg-config')
gdbus_codegen = find_program('gdbus-codegen', required: get_option('gio'))
numa = cc.find_library('numa', has_headers: ['numa.h'])
threads = dependency('threads')
"""
BUILDOPTIONS = "".join(
    f"    --enable-{flag}) printf \"%s\" -D{flag.replace('-', '_')}=enabled ;;\n"
    f"    --disable-{flag}) printf \"%s\" -D{flag.replace('-', '_')}=disabled ;;\n"
    for flag in ("numa", "libnfs", "lzfse", "alsa", "auth-pam", "canokey", "gio", "bochs"))
MESON_LOG = """Library pam found: YES
Run-time dependency canokey-qemu found: NO (tried pkgconfig)
Run-time dependency gio-2.0 found: YES 2.90.1
Program gdbus-codegen found: NO
Has header "numa.h" : NO (cached)
Dependency libnfs skipped: feature libnfs disabled
Dependency libcbor found: YES 0.14.0 (cached)
"""


def feature(name, value):
    return {"name": name, "value": value, "section": "user", "type": "combo",
            "choices": ["enabled", "disabled", "auto"]}


def reference_build(root, options, header, log=MESON_LOG, buildoptions=BUILDOPTIONS):
    """A configured QEMU build directory and the source it was configured from, in miniature."""
    root = Path(root)
    reference, source = root / "build", root / "source"
    (reference / "meson-info").mkdir(parents=True)
    (reference / "meson-logs").mkdir()
    (reference / "meson-info/intro-buildoptions.json").write_text(json.dumps(options))
    (reference / "meson-logs/meson-log.txt").write_text(log)
    (reference / "config-host.h").write_text(header)
    for name in ("aarch64-softmmu-config-devices.h", "aarch64-softmmu-config-target.h"):
        (reference / name).write_text(f"/* {name} */\n#define CONFIG_ARM 1\n")
    (source / "scripts").mkdir(parents=True)
    (source / "meson.build").write_text(MESON_BUILD)
    (source / "scripts/meson-buildoptions.sh").write_text(buildoptions)
    return reference, source


class PatchSeriesTests(unittest.TestCase):
    def engine_receipt(self):
        """The build-receipt.json of an engine you built, named by TOMACCO_ENGINE_RECEIPT."""
        name = os.environ.get("TOMACCO_ENGINE_RECEIPT")
        if not name:
            self.skipTest("TOMACCO_ENGINE_RECEIPT does not name an engine's build-receipt.json")
        return json.loads(Path(name).read_text())

    def test_the_default_series_names_every_patch_and_the_rest_is_opt_in(self):
        # The asynchronous readback adds its patch only when asked.
        names = [path.name for path in build.GRAPHICS_PATCHES]
        self.assertEqual(names[-3:], ["reims-rung-blank-verdict.patch", "reims-gva-resident-lifetime.patch",
                                      "reims-resident-lifetime-rest.patch"])
        self.assertEqual(list(build.EXPERIMENTAL_GRAPHICS_PATCHES), ["reims-async-readback"])
        for path in build.EXPERIMENTAL_GRAPHICS_PATCHES.values():
            self.assertNotIn(path, build.GRAPHICS_PATCHES)
            self.assertTrue(path.is_file(), path)
        every = {path.name for path in build.INTEGRATION.glob("reims-*.patch")}
        listed = set(names) | {path.name for path in build.EXPERIMENTAL_GRAPHICS_PATCHES.values()}
        self.assertEqual(every - listed, set(), "an integration patch is in neither series")

    def test_the_default_series_reproduce_an_engines_receipt(self):
        receipt = self.engine_receipt()
        digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual({path.name: digest(path) for path in build.GRAPHICS_PATCHES}, receipt["graphics_patches"])
        self.assertEqual(list(receipt["graphics_patches"]), [path.name for path in build.GRAPHICS_PATCHES])
        self.assertEqual({path.name: digest(path) for path in build.PATCHES}, receipt["engine_patches"])
        self.assertEqual(list(receipt["engine_patches"]), [path.name for path in build.PATCHES])

    def test_the_default_engine_series_ends_with_the_throughput_patches_and_the_16k_pages(self):
        # The load-acquire barrier, the helper-free pointer-authentication
        # bypass, the four throughput patches and the two for 16 KiB pages; no
        # engine patch is opt-in now. The hot trust-cache update follows them.
        self.assertEqual(build.PATCHES[-1].name, "vphone-trust-cache-update.patch")
        self.assertEqual([path.name for path in build.PATCHES[-9:-1]],
                         ["qemu-arm-rcsc-load-acquire.patch", "qemu-arm-pauth-bypass-inline.patch",
                          "qemu-tcg-jump-cache.patch", "qemu-arm-jump-probe.patch", "qemu-arm-bti-property.patch",
                          "qemu-arm-wfe-monitor.patch", "qemu-arm-target-page-16k.patch",
                          "qemu-tcg-jump-cache-page-scale.patch"])
        self.assertEqual(build.EXPERIMENTAL_PATCHES, {})
        self.assertEqual(build.selected_engine_patches([]), build.PATCHES)
        self.assertEqual(build.selected_engine_patches(["reims-async-readback"]), build.PATCHES)
        every = {path.name for path in build.INTEGRATION.glob("qemu-*.patch")}
        listed = {path.name for path in build.PATCHES} | {path.name for path in build.EXPERIMENTAL_PATCHES.values()}
        self.assertEqual(every - listed, set(), "an engine patch is in neither series")

    def test_a_future_experimental_engine_patch_applies_in_series_order_with_its_prerequisites(self):
        # The mechanism stays for optional patches, empty as it is today.
        first, second = Path("/series/first.patch"), Path("/series/second.patch")
        with mock.patch.dict(build.EXPERIMENTAL_PATCHES, {"first": first, "second": second}), \
                mock.patch.dict(build.EXPERIMENTAL_PATCH_REQUIRES, {"second": ("first",)}):
            # Asked in any order, they follow the series in registration order.
            self.assertEqual(build.selected_engine_patches(["second", "first"]), build.PATCHES + (first, second))
            self.assertEqual(build.selected_engine_patches(["first"]), build.PATCHES + (first,))
            with self.assertRaisesRegex(ValueError, "--experimental-patch second also needs: first"):
                build.selected_engine_patches(["second"])


class TargetPagePatchTests(unittest.TestCase):
    PATCH = REPO / "integrations/ios-emulator/qemu-arm-target-page-16k.patch"

    def test_the_patch_decides_the_page_size_before_the_board_and_lets_its_cpus_accept_it(self):
        text = self.PATCH.read_text()
        # The machine option is read in qemu_create_machine, ahead of the
        # board's minimum_page_bits, and admits exactly 4 and 16 KiB.
        self.assertIn("tomacco_request_target_page_bits(qdict);\n"
                      "+    if (!tomacco_target_page_bits_requested && machine_class->minimum_page_bits) {", text)
        self.assertIn("(bits != 10 && bits != 12 && bits != 14)", text)
        # Only the 16 KiB-only CPU accepts 16 KiB target pages (QEMU maps a
        # whole target page from one walk); it reports no 4 or 64 KiB
        # granule, at either stage. Other CPUs keep refusing.
        self.assertIn("+            pagebits = 14;", text)
        self.assertNotIn("pagebits = TARGET_PAGE_BITS;", text)
        for field, value in (("TGRAN4", "0xf"), ("TGRAN64", "0xf"), ("TGRAN4_2", "1"), ("TGRAN64_2", "1")):
            self.assertIn(f"FIELD_DP64_IDREG(isar, ID_AA64MMFR0, {field}, {value});", text)
        self.assertIn('DEFINE_PROP_BOOL("tomacco-granule-16k", ARMCPU, tomacco_granule16k, false)', text)
        # The machine property of the same name only reads the size back.
        self.assertIn('object_class_property_add(oc, "tomacco-page-bits", "uint8"', text)


class PgoTests(unittest.TestCase):
    """build_ios_paravirt.py --pgo: the flags of each stage and what the receipt says."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tomacco-pgo-test-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.build = self.root / "sptm-build"
        self.data = self.root / "profile"
        self.runs = []

    def run_tool(self, argv, *, env=None, capture=False, cwd=None):
        self.runs.append([str(arg) for arg in argv])
        class Done:
            stdout = "rustc 1.99.1\nLLVM version: 23.1.1\n" if str(argv[0]) == "rustc" else \
                "LLVM (http://llvm.org/):\n  LLVM version 23.1.3\n"
        if len(argv) > 1 and argv[1] == "merge":
            Path(argv[3]).write_bytes(b"merged")
        return Done()

    def plan(self, mode, env=None):
        return build.pgo_plan(mode, self.data, self.build, self.root, self.run_tool,
                              env or {"PATH": os.environ["PATH"]})

    def test_off_adds_nothing(self):
        self.assertEqual(self.plan("off"), ([], [], None))
        self.assertFalse(self.data.exists())

    def test_generate_instruments_both_compilers_with_atomic_counters_and_build_relative_names(self):
        c, rust, record = self.plan("generate")
        self.assertEqual(c, [f"-fprofile-generate={self.data / 'c'}",
                             f"-fprofile-prefix-path={self.build}"])
        self.assertEqual(rust, [f"-Cprofile-generate={self.data / 'rust'}"])
        self.assertEqual(record["mode"], "generate")
        self.assertTrue(self.data.is_dir())

    def test_generate_refuses_a_directory_that_already_holds_training(self):
        (self.data / "c").mkdir(parents=True)
        (self.data / "c/tcg#tcg.c.gcda").write_bytes(b"x")
        with self.assertRaisesRegex(RuntimeError, "empty profile directory"):
            self.plan("generate")

    def test_build_time_profile_is_cleared_but_nothing_else(self):
        (self.data / "c").mkdir(parents=True)
        (self.data / "rust").mkdir()
        (self.data / "c/a.gcda").write_bytes(b"a")
        (self.data / "rust/default_1_2.profraw").write_bytes(b"r")
        (self.data / build.PGO_MARKER).write_text("{}")
        self.assertEqual(build.clear_build_time_profile(self.data), 2)
        self.assertEqual(sorted(p.name for p in self.data.rglob("*") if p.is_file()), [build.PGO_MARKER])

    def test_use_needs_training_and_records_the_digest_of_what_it_consumed(self):
        with self.assertRaisesRegex(RuntimeError, "no .gcda profile"):
            self.plan("use")
        (self.data / "c").mkdir(parents=True)
        (self.data / "c/tcg#tcg.c.gcda").write_bytes(b"counters")
        c, rust, record = self.plan("use")
        self.assertEqual(c[:5], [f"-fprofile-use={self.data / 'c'}", "-fprofile-correction", "-fprofile-partial-training",
                                 "-Wno-missing-profile", "-Wno-error=coverage-mismatch"])
        self.assertEqual(c[-1], f"-fprofile-prefix-path={self.build}")
        self.assertEqual(rust, [])
        self.assertIsNone(record["rust_profile"])
        self.assertEqual(record["c_profile"]["files"], 1)
        first = record["profile_sha256"]
        # Other counters, another digest.
        (self.data / "c/tcg#tcg.c.gcda").write_bytes(b"other")
        self.assertNotEqual(self.plan("use")[2]["profile_sha256"], first)

    def test_use_merges_rust_raw_profiles_only_with_rustcs_llvm_major(self):
        (self.data / "c").mkdir(parents=True)
        (self.data / "rust").mkdir()
        (self.data / "c/a.gcda").write_bytes(b"a")
        (self.data / "rust/default_1_2.profraw").write_bytes(b"r")
        tools = self.root / "bin"
        tools.mkdir()
        (tools / "llvm-profdata").write_text("#!/bin/sh\n")
        (tools / "llvm-profdata").chmod(0o755)
        env = {"PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"}
        c, rust, record = self.plan("use", env)
        merged = self.root / "pgo-rust.profdata"
        self.assertEqual(rust, [f"-Cprofile-use={merged}"])
        self.assertEqual(record["rust_profile"]["llvm_major"], 23)
        merge = next(run for run in self.runs if run[1:2] == ["merge"])
        self.assertEqual(merge[2:], ["-o", str(merged), str(self.data / "rust/default_1_2.profraw")])


class HardeningTests(unittest.TestCase):
    def test_the_default_keeps_qemus_hardening_and_off_overrides_each_flag_it_adds(self):
        self.assertEqual(build.hardening_overrides("qemu"), [])
        overrides = build.hardening_overrides("off")
        # Each override names the flag QEMU's meson.build adds, so the later one wins.
        source = (PREPARED / "sptm-source/meson.build")
        if source.is_file():
            text = source.read_text()
            for added in ("-fstack-protector-strong", "-ftrivial-auto-var-init=zero",
                          "-fzero-call-used-regs=used-gpr"):
                self.assertIn(added, text)
        self.assertEqual(overrides, ["-fno-stack-protector", "-ftrivial-auto-var-init=uninitialized",
                                     "-fzero-call-used-regs=skip"])
        with self.assertRaises(ValueError):
            build.hardening_overrides("partial")


class ConfigurePinTests(unittest.TestCase):
    OPTIONS = [feature("numa", "auto"), feature("libnfs", "disabled"), feature("lzfse", "enabled"),
               feature("alsa", "auto"), feature("auth_pam", "auto"), feature("canokey", "auto"),
               feature("gio", "auto"), feature("bochs", "auto"), feature("dtc:yaml", "auto"),
               {"name": "plugins", "value": True, "section": "user", "type": "boolean"},
               {"name": "fdt", "value": "internal", "section": "user", "type": "combo",
                "choices": ["disabled", "enabled", "auto", "system", "internal"]}]
    HEADER = "#pragma once\n\n#undef CONFIG_NUMA\n\n#define CONFIG_AUDIO_ALSA\n\n#define CONFIG_GIO\n"

    def test_every_host_detected_feature_is_pinned_to_what_the_reference_found(self):
        with tempfile.TemporaryDirectory() as root:
            reference, source = reference_build(root, self.OPTIONS, self.HEADER)
            pins = build.configure_pins(reference, source)
        # Requested values first (libnfs came from a later `meson configure`, lzfse from
        # the recipe), then the macros of config-host.h, then the Meson log's lookups.
        self.assertEqual(pins["flags"], ["--disable-numa", "--disable-libnfs", "--enable-lzfse",
                                         "--enable-alsa", "--enable-auth-pam", "--disable-canokey"])
        self.assertEqual(pins["evidence"]["numa"], "config-host.h undefines CONFIG_NUMA")
        self.assertEqual(pins["evidence"]["alsa"], "config-host.h defines CONFIG_AUDIO_ALSA")
        self.assertEqual(pins["evidence"]["libnfs"], "requested disabled")
        self.assertEqual(pins["evidence"]["auth_pam"], "meson-log: pam found")
        self.assertEqual(pins["evidence"]["canokey"], "meson-log: canokey-qemu not found")
        # gio was found, but --enable-gio would also demand gdbus-codegen, which auto tolerated missing.
        self.assertIn("gdbus-codegen", pins["unpinned"]["gio"])
        self.assertIn("bochs", pins["unpinned"])
        for absent in ("dtc:yaml", "plugins", "fdt", "gio", "bochs"):
            self.assertNotIn(absent, pins["evidence"])
        self.assertEqual(set(pins["headers"]), set(build.CONFIGURE_HEADERS))
        self.assertEqual(pins["headers"]["config-host.h"], self.HEADER)
        self.assertEqual(set(pins["reference"]["files"]),
                         {"meson-info/intro-buildoptions.json", "config-host.h", "meson-logs/meson-log.txt",
                          *build.CONFIGURE_HEADERS})

    def test_a_pin_the_configure_cannot_translate_and_a_bare_directory_are_refused(self):
        with tempfile.TemporaryDirectory() as root:
            reference, source = reference_build(root, self.OPTIONS, self.HEADER,
                                                buildoptions=BUILDOPTIONS.replace("--disable-numa)", "--x)"))
            with self.assertRaises(RuntimeError) as refused:
                build.configure_pins(reference, source)
            self.assertIn("--disable-numa", str(refused.exception))
            (reference / "meson-logs/meson-log.txt").unlink()
            with self.assertRaises(RuntimeError) as refused:
                build.configure_pins(reference, source)
            self.assertIn("meson-logs/meson-log.txt", str(refused.exception))

    def test_a_recorded_pins_file_is_read_back_and_other_shapes_are_refused(self):
        with tempfile.TemporaryDirectory() as root:
            reference, source = reference_build(root, self.OPTIONS, self.HEADER)
            pins = build.configure_pins(reference, source)
            record = Path(root) / "configure-pins.json"
            record.write_text(json.dumps(pins))
            self.assertEqual(build.load_configure_pins(record, None), pins)
            self.assertEqual(build.load_configure_pins(reference, source), pins)
            # A record with only flags and headers would pass preflight and leave the manifest
            # writer without reference, evidence and unpinned after the whole build has run.
            truncated = {key: pins[key] for key in ("flags", "headers")}
            for shape in ({"flags": "x"}, {"flags": ["-Dnuma=disabled"], "headers": pins["headers"]},
                          {"flags": pins["flags"], "headers": {"config-host.h": ""}}, [], truncated,
                          dict(pins, unpinned=["kvm"])):
                record.write_text(json.dumps(shape))
                with self.assertRaises(RuntimeError, msg=repr(shape)):
                    build.load_configure_pins(record, None)

    def test_pins_join_the_recipe_without_contradicting_it(self):
        recipe = ["--target-list=aarch64-softmmu", "--disable-sdl", "--enable-fdt=internal", "--extra-cflags=-O3"]
        self.assertEqual(build.pinned_configure_flags(recipe, ["--disable-sdl", "--disable-numa", "--enable-alsa"]),
                         [*recipe, "--disable-numa", "--enable-alsa"])
        with self.assertRaises(RuntimeError):
            build.pinned_configure_flags(recipe, ["--enable-sdl"])
        with self.assertRaises(RuntimeError):
            build.pinned_configure_flags(recipe, ["-Dnuma=disabled"])

    def test_parity_names_the_header_and_the_lines_that_drifted(self):
        headers = {"config-host.h": "#undef CONFIG_NUMA\n#define CONFIG_ZSTD\n",
                   "aarch64-softmmu-config-target.h": "#define TARGET_AARCH64 1\n"}
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "config-host.h").write_text("#define CONFIG_NUMA\n#define CONFIG_ZSTD\n#define HAVE_NUMA_HAS_PREFERRED_MANY\n")
            (Path(root) / "aarch64-softmmu-config-target.h").write_text(headers["aarch64-softmmu-config-target.h"])
            report, drifted = build.configure_parity(root, headers)
        self.assertEqual(drifted, ["config-host.h"])
        self.assertEqual(report["aarch64-softmmu-config-target.h"], {"identical": True})
        self.assertFalse(report["config-host.h"]["identical"])
        lines = report["config-host.h"]["differences"]
        self.assertIn("-#undef CONFIG_NUMA", lines)
        self.assertIn("+#define CONFIG_NUMA", lines)
        self.assertIn("+#define HAVE_NUMA_HAS_PREFERRED_MANY", lines)

    def test_lookups_and_their_results_are_read_from_meson_build_and_the_log(self):
        self.assertEqual(build.feature_lookups(MESON_BUILD),
                         {"auth_pam": ["pam"], "canokey": ["canokey-qemu"], "gio": ["gio-2.0", "gdbus-codegen"]})
        self.assertEqual(build.lookup_results(MESON_LOG),
                         {"pam": True, "canokey-qemu": False, "gio-2.0": True, "gdbus-codegen": False,
                          "numa.h": False, "libcbor": True})
        self.assertEqual(build.configured_macros("#define CONFIG_TCG 1\n#undef CONFIG_NUMA\n# define X\n"),
                         {"CONFIG_TCG": True, "CONFIG_NUMA": False, "X": True})
        self.assertEqual(build.accepted_configure_flags(BUILDOPTIONS) & {"--enable-numa", "--disable-gio", "--enable-x"},
                         {"--enable-numa", "--disable-gio"})


class CacheParameterTests(unittest.TestCase):
    def test_every_cpu_gets_the_performance_cores_geometry(self):
        performance = {"l1-cache-size": 48, "l1-cache-line-size": 64, "l2-cache-size": 36864}
        efficiency = {"l1-cache-size": 32, "l1-cache-line-size": 64, "l2-cache-size": 36864}
        flags, record = build.cache_parameter_flags({0: performance, 1: performance, 16: efficiency})
        self.assertEqual(flags, ["--param=l1-cache-size=48", "--param=l1-cache-line-size=64",
                                 "--param=l2-cache-size=36864"])
        self.assertEqual(record, {"pinned": performance, "cpus": 3,
                                  "observed": {"l1-cache-size": {"32": 1, "48": 2},
                                               "l1-cache-line-size": {"64": 3},
                                               "l2-cache-size": {"36864": 3}}})
        # A compiler that reports no parameters pins nothing and says so.
        self.assertEqual(build.cache_parameter_flags({0: {}, 1: {}}),
                         ([], {"pinned": {}, "observed": {}, "cpus": 2}))

    def test_the_compilers_report_is_read_as_gcc_prints_it(self):
        report = ("The following options are target specific:\n"
                  "  --param=l1-cache-line-size= \t\t64\n  --param=l1-cache-size= \t\t48\n"
                  "  --param=l2-cache-size= \t\t36864\n  --param=max-inline-insns-auto= \t\t15\n")
        self.assertEqual({name: int(value) for name, value in build.CACHE_PARAMETER.findall(report)},
                         {"l1-cache-size": 48, "l1-cache-line-size": 64, "l2-cache-size": 36864})

    CC1 = (" /usr/lib/gcc/x86_64-pc-linux-gnu/16/cc1 -E -quiet -v /dev/null -o /dev/null -march=alderlake "
           "-mmmx -mno-avx512f -mavxvnni --param l1-cache-size={l1} --param l1-cache-line-size=64 "
           "--param l2-cache-size=36864 -mtune=alderlake -dumpbase null\n")
    DRIVER = "Using built-in specs.\nCOLLECT_GCC=cc\n{cc1}COMPILER_PATH=/usr/lib/gcc\n"

    def test_the_drivers_expansion_is_read_from_its_cc1_line(self):
        self.assertEqual(build.native_expansion(self.DRIVER.format(cc1=self.CC1.format(l1=48))),
                         (["-march=alderlake", "-mmmx", "-mno-avx512f", "-mavxvnni", "-mtune=alderlake"],
                          {"l1-cache-size": 48, "l1-cache-line-size": 64, "l2-cache-size": 36864}))
        self.assertEqual(build.native_expansion("clang -cc1 -triple x86_64 -target-cpu alderlake\n"), (None, {}))

    def test_the_flags_name_the_instruction_set_once_and_the_largest_caches(self):
        performance = build.native_expansion(self.DRIVER.format(cc1=self.CC1.format(l1=48)))
        efficiency = build.native_expansion(self.DRIVER.format(cc1=self.CC1.format(l1=32)))
        flags, record = build.native_target_flags({0: performance, 1: efficiency, 2: efficiency})
        self.assertEqual(flags, ["-march=alderlake", "-mmmx", "-mno-avx512f", "-mavxvnni", "-mtune=alderlake",
                                 "--param=l1-cache-size=48", "--param=l1-cache-line-size=64",
                                 "--param=l2-cache-size=36864"])
        self.assertNotIn("-march=native", flags)
        self.assertEqual(record["pinned"], {"l1-cache-size": 48, "l1-cache-line-size": 64, "l2-cache-size": 36864})
        self.assertEqual(record["observed"]["l1-cache-size"], {"32": 2, "48": 1})
        self.assertTrue(record["expanded"])
        # A driver that does not expand -march=native (clang) keeps the native flags and says so.
        self.assertEqual(build.native_target_flags({0: (None, {})}),
                         (["-march=native", "-mtune=native"], {"expanded": False, "cpus": 1}))
        other = (["-march=sapphirerapids", "-mtune=sapphirerapids"], {"l1-cache-size": 48})
        with self.assertRaises(RuntimeError):
            build.native_target_flags({0: performance, 1: other})

    @unittest.skipUnless(shutil.which("cc"), "Needs a C compiler")
    def test_the_host_compiler_is_asked_on_every_cpu_and_the_affinity_restored(self):
        before = os.sched_getaffinity(0)
        observations = build.observe_native_target("cc", dict(os.environ))
        self.assertEqual(set(observations), before)
        self.assertEqual(os.sched_getaffinity(0), before)


class SeededCompilerTests(unittest.TestCase):
    def test_the_wrapper_seeds_gcc_from_the_output_path_only(self):
        with tempfile.TemporaryDirectory() as root:
            bin_dir = Path(root) / "bin"
            bin_dir.mkdir()
            for name in ("cc", "c++"):
                fake = bin_dir / name
                fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
                fake.chmod(0o755)
            wrappers = build.seeded_compilers(root, {"PATH": str(bin_dir)})
            self.assertEqual(wrappers, {"CC": f"{root}/cc-seeded", "CXX": f"{root}/c++-seeded"})
            import hashlib
            import subprocess
            seed = hashlib.sha256(b"libauthz.a.p/authz_base.c.o").hexdigest()[:16]
            run = lambda *argv: subprocess.run(argv, capture_output=True, text=True, check=True).stdout.split("\n")[:-1]
            self.assertEqual(run(wrappers["CC"], "-c", "base.c", "-o", "libauthz.a.p/authz_base.c.o"),
                             ["-c", "base.c", "-o", "libauthz.a.p/authz_base.c.o", f"-frandom-seed={seed}"])
            self.assertEqual(run(wrappers["CXX"], "--version"), ["--version"])
            # The same output path seeds the same whatever the source directory or the core.
            self.assertEqual(run(wrappers["CC"], "-c", "/elsewhere/base.c", "-o", "libauthz.a.p/authz_base.c.o")[-1],
                             f"-frandom-seed={seed}")


class EnvironmentTests(unittest.TestCase):
    def test_the_build_scripts_hash_seed_and_locale_are_fixed(self):
        env = build.deterministic_environment({"PATH": "/usr/bin", "PYTHONHASHSEED": "random", "LC_ALL": "de_AT.UTF-8"})
        self.assertEqual({key: env[key] for key in ("PATH", "PYTHONHASHSEED", "LC_ALL")},
                         {"PATH": "/usr/bin", "PYTHONHASHSEED": "0", "LC_ALL": "C.UTF-8"})

    def test_cargo_is_kept_from_naming_the_renderers_objects_after_a_session(self):
        # The orchard release profile says `incremental = true`; the environment overrides it,
        # so the archive's member names, and with them its digest, are the same in every build.
        env = build.deterministic_environment({"PATH": "/usr/bin", "CARGO_INCREMENTAL": "1"})
        self.assertEqual(env.get("CARGO_INCREMENTAL"), "0")
        self.assertEqual(build.deterministic_environment({}).get("CARGO_INCREMENTAL"), "0")


class TranslatorLinkTests(unittest.TestCase):
    def test_the_link_is_relative_and_its_map_names_the_recorded_translator_path(self):
        with tempfile.TemporaryDirectory() as root:
            orchard, translator = Path(root) / "orchard", Path(root) / "metal2vulkan"
            orchard.mkdir()
            translator.mkdir()
            link, flag = build.link_translator(orchard, translator, [f"{root}=/recorded/tree"])
            self.assertEqual(link, orchard / "reims-vgpu/target/metal2vulkan")
            self.assertEqual(os.readlink(link), "../../../metal2vulkan")
            self.assertEqual(link.resolve(), translator.resolve())
            self.assertEqual(flag, f"--remap-path-prefix={link}=/recorded/tree/metal2vulkan")
            # Without a map the recorded path is the translator's own; a second call keeps the link.
            self.assertEqual(build.link_translator(orchard, translator, []),
                             (link, f"--remap-path-prefix={link}={translator}"))
            link.unlink()
            link.write_text("not a link")
            with self.assertRaises(RuntimeError):
                build.link_translator(orchard, translator, [])


@unittest.skipUnless((PREPARED / "sptm-build/config-host.h").exists() and QUALIFIED_PINS.exists(),
                     "Needs a prepared engine tree and the recorded pins")
class QualifiedPinTests(unittest.TestCase):
    def test_the_recorded_pins_are_what_the_build_directory_yields(self):
        derived = build.configure_pins(PREPARED / "sptm-build", PREPARED / "sptm-source")
        recorded = json.loads(QUALIFIED_PINS.read_text())
        self.assertEqual(recorded["flags"], derived["flags"])
        self.assertEqual(recorded["headers"], derived["headers"])
        self.assertIn("--disable-numa", recorded["flags"])
        self.assertIn("--disable-virglrenderer", recorded["flags"])
        self.assertIn("gio", recorded["unpinned"])


class PrefixMapTests(unittest.TestCase):
    def test_a_map_reaches_the_c_compiler_and_rustc_alike(self):
        cflags, rustflags = build.prefix_map_flags(["/tmp/fresh=/home/user/.tooling/ios-paravirt"])
        self.assertEqual(cflags, ["-ffile-prefix-map=/tmp/fresh=/home/user/.tooling/ios-paravirt"])
        self.assertEqual(rustflags, ["--remap-path-prefix=/tmp/fresh=/home/user/.tooling/ios-paravirt"])
        self.assertEqual(build.prefix_map_flags([]), ([], []))
        # Several maps keep their order; a later map may name a longer prefix.
        cflags, _ = build.prefix_map_flags(["/a=/b", "/a/c=/d"])
        self.assertEqual(cflags, ["-ffile-prefix-map=/a=/b", "-ffile-prefix-map=/a/c=/d"])

    def test_a_map_without_both_sides_or_with_a_space_is_refused(self):
        # QEMU's configure splits --extra-cflags on whitespace, so a path with
        # a space cannot be passed through it; the other shapes map nothing.
        for item in ("/tmp/fresh", "=/x", "/x=", "/a b=/c", "/a=/b\n", ""):
            with self.assertRaises(RuntimeError, msg=repr(item)):
                build.prefix_map_flags([item])


class ExpectedDigestTests(unittest.TestCase):
    def test_the_expected_digest_is_normalised_before_the_build(self):
        digest = "00d72173ca6799c564f609bb290bf137f040ce2594a7b951d2c55a02ebc169fa"
        self.assertIsNone(build.expected_engine_digest(None))
        self.assertEqual(build.expected_engine_digest(" " + digest.upper() + "\n"), digest)
        for bad in ("", "00d72173", "g" * 64, digest + "0"):
            with self.assertRaises(RuntimeError, msg=repr(bad)):
                build.expected_engine_digest(bad)

    def test_the_record_says_whether_the_engine_reproduced(self):
        record = {"engine_sha256": "a" * 64}
        self.assertIsNone(build.reproducibility_check(record, None))
        self.assertEqual(build.reproducibility_check(record, "a" * 64),
                         {"expected_engine_sha256": "a" * 64, "engine_sha256": "a" * 64, "reproduced": True})
        self.assertEqual(build.reproducibility_check(record, "b" * 64),
                         {"expected_engine_sha256": "b" * 64, "engine_sha256": "a" * 64, "reproduced": False})


if __name__ == "__main__":
    unittest.main()
