"""Actual bounded capability owner with synthetic metadata, never a GPU oracle."""
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import unittest


class MetalCapabilityOwnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if platform.machine() != "x86_64":
            raise unittest.SkipTest("Synthetic integer dispatch uses the x86_64 host ABI")
        compiler = shutil.which("clang")
        if not compiler:
            raise unittest.SkipTest("Clang is required for the owned C fixture")
        cls.directory = tempfile.TemporaryDirectory(prefix="tomacco-metal-capabilities-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.binary = Path(cls.directory.name) / "owner"
        fixture = Path(__file__).resolve().parents[1] / "fixtures/ios_metal_capabilities_harness.c"
        result = subprocess.run([compiler, "-O2", "-Wall", "-Wextra", "-Werror", str(fixture),
                                 "-o", str(cls.binary)], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr)

    def check_case(self, name):
        subprocess.run([str(self.binary), name], check=True, capture_output=True, text=True, timeout=10)

    def test_missing_device_is_unknown_without_native_calls(self):
        self.check_case("missing-device")

    def test_missing_methods_are_unknown_instead_of_false(self):
        self.check_case("missing-methods")

    def test_every_runtime_method_field_is_guarded(self):
        self.check_case("incompatible")

    def test_observed_false_is_known(self):
        self.check_case("false")

    def test_device_and_feature_queries_are_distinct_bounded_receivers(self):
        self.check_case("distinct")

    def test_nil_feature_queries_does_not_invent_answers(self):
        self.check_case("nil")

    def test_profile_and_object_getter_have_exact_independent_guards(self):
        self.check_case("getter-guard")

    def test_family_argument_requires_exact_signed_64_bit_encoding(self):
        self.check_case("family-guard")

    def test_family_return_count_self_and_selector_are_guarded(self):
        self.check_case("family-method-guard")

    def test_texture_usage_and_extended_xr_are_independent_answers(self):
        self.check_case("usage-independent")

    def test_missing_virtualization_api_remains_unknown_without_a_call(self):
        self.check_case("virtual-unavailable")

    def test_virtualization_boolean_retains_raw_answer_and_process_scope(self):
        self.check_case("virtual-answers")

    def test_virtualization_image_metadata_does_not_gate_the_observed_answer(self):
        self.check_case("virtual-metadata")


if __name__ == "__main__":
    unittest.main()
