"""Synthetic public-lookup ownership and raw-metadata projection."""
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unittest


class SurfaceStateOwnerTests(unittest.TestCase):
    def test_public_operation_schema_exposes_the_native_query(self):
        root = Path(__file__).resolve().parents[2]
        operations = json.loads((root / "assets/operations.json").read_text())
        operation = next(item for item in operations if item["name"] == "ios.emulator.guest")
        self.assertIn("surface_state", operation["inputSchema"]["properties"]["operation"]["enum"])

    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("clang") or shutil.which("cc")
        if not compiler:
            raise unittest.SkipTest("C compiler required for the retained lookup owner")
        cls.directory = tempfile.TemporaryDirectory(prefix="tomacco-surface-state-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.binary = Path(cls.directory.name) / "owner"
        fixture = Path(__file__).resolve().parents[1] / "fixtures/ios_surface_state_harness.c"
        subprocess.run([compiler, "-O2", "-Wall", "-Wextra", "-Werror", str(fixture),
                        "-o", str(cls.binary)], check=True, capture_output=True, text=True, timeout=30)

    def check_case(self, case):
        subprocess.run([str(self.binary), case], check=True, capture_output=True, text=True, timeout=10)

    def test_only_four_distinct_canonical_u32_ids_are_accepted(self):
        self.check_case("ids")

    def test_missing_release_or_identity_api_never_starts_a_lookup(self):
        self.check_case("missing")

    def test_nonnull_results_are_released_once_including_identity_mismatch(self):
        self.check_case("ownership")

    def test_full_native_bits_are_preserved_without_enum_or_pixel_claims(self):
        self.check_case("raw")

    def test_plane_and_component_limits_never_truncate_into_valid_metadata(self):
        self.check_case("bounds")

    def test_nonplanar_zero_and_missing_component_apis_remain_explicit(self):
        self.check_case("zero")
