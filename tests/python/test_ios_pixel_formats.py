"""Bounded CoreVideo queries; synthetic values establish no native pixel format."""
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import unittest


class PixelFormatOwnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if platform.machine() != "x86_64":
            raise unittest.SkipTest("Synthetic Objective-C dispatch uses the x86_64 host ABI")
        compiler = shutil.which("clang")
        if not compiler:
            raise unittest.SkipTest("Clang is required for the synthetic dispatch trampoline")
        cls.directory = tempfile.TemporaryDirectory(prefix="tomacco-pixel-format-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.binary = Path(cls.directory.name) / "owner"
        fixture = Path(__file__).resolve().parents[1] / "fixtures/ios_pixel_format_harness.c"
        subprocess.run([compiler, "-O2", "-Wall", "-Wextra", "-Werror", str(fixture),
                        "-o", str(cls.binary)], check=True, capture_output=True, text=True, timeout=30)

    def check_case(self, name):
        subprocess.run([str(self.binary), name], check=True, capture_output=True, text=True, timeout=10)

    def test_missing_api_never_creates_an_unreleasable_description(self):
        self.check_case("missing")

    def test_three_exact_formats_and_null_descriptions_remain_unknown(self):
        self.check_case("null")

    def test_every_nonnull_create_is_released_once_even_if_unusable(self):
        self.check_case("ownership")

    def test_selected_values_preserve_numbers_and_report_rejected_types(self):
        self.check_case("fields")

    def test_missing_key_symbols_are_explicit(self):
        self.check_case("symbols")

    def test_nested_and_cyclic_planes_stop_after_one_level(self):
        self.check_case("planes")

    def test_oversized_and_nonarray_planes_are_not_walked(self):
        self.check_case("limits")


if __name__ == "__main__":
    unittest.main()
