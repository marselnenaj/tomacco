"""The engine build reuses an engine built from the same pinned inputs and says why it would rebuild otherwise.

Nothing here builds, fetches or runs a compiler: the manifest, the engine and the base tooling are small fakes,
and the toolchain versions and checkout fingerprints are given as the build would observe them."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import build_ios_paravirt as build  # noqa: E402

TOOLCHAIN = {name: [f"{name} 1.0"] for name in build.REUSE_TOOLS}


def arguments(**overrides):
    values = {"cpu_optimization": "portable", "hardening": "qemu", "file_prefix_map": [], "link_translator": False,
              "experimental_patch": [], "rebuild": False, "expect_engine_sha256": None, "pgo": "off",
              "configure_pins": None}
    values.update(overrides)
    return argparse.Namespace(**values)


class EngineReuseTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.root, self.tooling = root / "ios-paravirt", root / "ios-sptm"
        engine = self.root / "sptm-build/qemu-system-aarch64"
        engine.parent.mkdir(parents=True)
        engine.write_bytes(b"engine bytes")
        for name in build.BASE_TOOLING:
            (self.tooling / name).parent.mkdir(parents=True, exist_ok=True)
            (self.tooling / name).write_bytes(b"")
        # No checkouts: their fingerprints are not compared, and the plan says so.
        self.sources = {role: root / role for role in ("sptm", "orchard", "metal2vulkan")}
        self.engine = engine
        self.record = self.manifest_for(arguments())

    def manifest_for(self, args):
        record = {"engine_sha256": hashlib.sha256(b"engine bytes").hexdigest(),
                  "source_commits": {"sptm": build.base.ENGINES["sptm"][1], "orchard": build.ORCHARD[1],
                                     "metal2vulkan": build.METAL2VULKAN[1]},
                  "ordered_patch_contract": build.audit.patch_descriptors({
                      "engine": build.selected_engine_patches(args.experimental_patch),
                      "graphics": build.GRAPHICS_PATCHES, "translator": build.TRANSLATOR_PATCHES}),
                  "cpu_optimization": args.cpu_optimization, "hardening": args.hardening,
                  "file_prefix_map": list(args.file_prefix_map), "translator_link": None, "pgo": None,
                  "configure_pins": None,
                  "toolchain": {name: {"sha256": "0" * 64, "version": version} for name, version in TOOLCHAIN.items()}}
        (self.root / "build-manifest.json").write_text(json.dumps(record))
        return record

    def plan(self, args=None, toolchain=TOOLCHAIN):
        with mock.patch.object(build, "toolchain_versions", return_value=toolchain):
            return build.reuse_plan(args or arguments(), self.root, self.sources, self.tooling)

    def test_an_engine_built_from_the_same_inputs_is_reused(self):
        plan = self.plan()
        self.assertFalse(plan["rebuild"], plan["reasons"])
        self.assertEqual(plan["engine_sha256"], self.record["engine_sha256"])
        self.assertEqual(plan["reasons"], [])
        self.assertEqual(plan["unchecked_checkouts"], ["sptm", "orchard", "metal2vulkan"])

    def test_without_an_earlier_build_it_builds(self):
        (self.root / "build-manifest.json").unlink()
        plan = self.plan()
        self.assertTrue(plan["rebuild"])
        self.assertIn("no complete earlier build", plan["reasons"][0])
        self.assertIsNone(plan["engine_sha256"])

    def test_a_replaced_or_missing_engine_is_rebuilt(self):
        self.engine.write_bytes(b"something else")
        self.assertIn("is not the one its manifest records", " ".join(self.plan()["reasons"]))
        self.engine.unlink()
        self.assertIn("is missing", " ".join(self.plan()["reasons"]))

    def test_a_changed_patch_series_names_the_patch(self):
        contract = self.record["ordered_patch_contract"]
        first = contract["engine"][0]["name"]
        contract["engine"][0] = {"name": first, "sha256": "f" * 64}
        contract["graphics"] = contract["graphics"][:-1]
        (self.root / "build-manifest.json").write_text(json.dumps(self.record))
        reasons = self.plan()["reasons"]
        self.assertIn(f"the engine patch series changed: {first} changed", reasons)
        self.assertIn(f"{build.GRAPHICS_PATCHES[-1].name} is new", " ".join(reasons))

    def test_a_reordered_series_is_rebuilt(self):
        contract = self.record["ordered_patch_contract"]
        contract["translator"] = list(reversed(contract["translator"]))
        (self.root / "build-manifest.json").write_text(json.dumps(self.record))
        self.assertIn("the translator patch series changed: same patches, another order", self.plan()["reasons"])

    def test_other_options_revisions_and_compilers_are_reasons(self):
        self.record["source_commits"]["orchard"] = "0" * 40
        (self.root / "build-manifest.json").write_text(json.dumps(self.record))
        reasons = self.plan(arguments(hardening="off"), toolchain={**TOOLCHAIN, "rustc": ["rustc 2.0"]})["reasons"]
        self.assertIn(f"the pinned orchard revision changed from {'0' * 40} to {build.ORCHARD[1]}", reasons)
        self.assertIn("hardening was 'qemu', now 'off'", reasons)
        self.assertIn("rustc changed from rustc 1.0 to rustc 2.0", reasons)

    def test_checkouts_are_compared_by_fingerprint(self):
        record = {**self.record, "source_trees": {"sptm": {"sha256": "a"}}}
        wanted = {"source_commits": record["source_commits"],
                  "ordered_patch_contract": record["ordered_patch_contract"], "cpu_optimization": "portable",
                  "hardening": "qemu", "file_prefix_map": [], "link_translator": False,
                  "source_trees": {"sptm": {"sha256": "a"}}}
        self.assertEqual(build.reuse_reasons(record, wanted, self.engine), [])
        wanted["source_trees"] = {"sptm": {"sha256": "b"}}
        self.assertEqual(build.reuse_reasons(record, wanted, self.engine),
                         ["the sptm checkout differs from the one the engine was built from"])
        wanted["hardware_error"] = "Preserved different source: hw/arm/vphone-usb.c"
        self.assertIn("hardware sources differ", build.reuse_reasons(record, wanted, self.engine)[0])

    def test_a_native_build_for_another_cpu_is_rebuilt(self):
        record = {**self.record, "cpu_optimization": "native", "host_compatibility": {"cpu": {"flags": "avx2"}}}
        wanted = {"source_commits": record["source_commits"],
                  "ordered_patch_contract": record["ordered_patch_contract"], "cpu_optimization": "native",
                  "hardening": "qemu", "file_prefix_map": [], "link_translator": False,
                  "host_cpu": {"flags": "avx2 avx512f"}}
        self.assertEqual(build.reuse_reasons(record, wanted, self.engine),
                         ["the native build was made for another CPU"])
        wanted["host_cpu"] = {"flags": "avx2"}
        self.assertEqual(build.reuse_reasons(record, wanted, self.engine), [])

    def test_missing_base_tooling_is_rebuilt(self):
        (self.tooling / "build/qemu-system-x86_64").unlink()
        self.assertIn(f"the base tooling lacks {self.tooling / 'build/qemu-system-x86_64'}", self.plan()["reasons"])

    def test_some_options_always_build(self):
        for args, word in ((arguments(rebuild=True), "--rebuild"),
                           (arguments(expect_engine_sha256="0" * 64), "--expect-engine-sha256"),
                           (arguments(pgo="generate"), "--pgo generate"),
                           (arguments(configure_pins="/pins.json"), "--configure-pins")):
            plan = self.plan(args)
            self.assertTrue(plan["rebuild"])
            self.assertTrue(plan["reasons"][0].startswith(word), plan["reasons"])

    def test_the_plan_runs_no_command(self):
        with mock.patch.object(build.base, "run", side_effect=AssertionError("ran a command")), \
                mock.patch.object(build.subprocess, "run", side_effect=AssertionError("ran a command")):
            self.assertFalse(self.plan()["rebuild"])


if __name__ == "__main__":
    unittest.main()
