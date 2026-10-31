"""Exercise the exact portable projection embedded in the engine patch."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class KernelSurfaceSourceTests(unittest.TestCase):
    def test_correlated_trace_rows_fit_qemu_argument_limit(self):
        root = Path(__file__).resolve().parents[2]
        patch = (root / "integrations/ios-emulator/vphone-iosurface-source-observation.patch").read_text()
        events = [line[1:] for line in patch.splitlines()
                  if line.startswith("+tomacco_arm_iosurface_")]
        self.assertEqual(len(events), 4)
        for event in events:
            arguments = event.split("(", 1)[1].split(")", 1)[0].split(",")
            self.assertLessEqual(len(arguments), 10)
            if event.startswith("tomacco_arm_iosurface_source_kernel("):
                self.assertIn("uint64_t original_base", arguments)
                self.assertIn(" uint64_t loaded_base", arguments)
                continue
            self.assertIn("unsigned cpu", arguments)
            self.assertIn(" unsigned attempt", arguments)

    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("clang") or shutil.which("cc")
        if not compiler:
            raise unittest.SkipTest("A C compiler is required for the surface source owner")
        cls.directory = tempfile.TemporaryDirectory(prefix="tomacco-kernel-surface-")
        cls.addClassCleanup(cls.directory.cleanup)
        root = Path(__file__).resolve().parents[2]
        directory = Path(cls.directory.name)
        patch = (root / "integrations/ios-emulator/vphone-iosurface-source-observation.patch").read_text()
        name = "target/arm/tcg/vphone-surface-source.h"
        block = patch.split(f"diff --git a/{name} b/{name}\n", 1)[1].split("diff --git ", 1)[0]
        assert "--- /dev/null\n" in block
        contents = "".join(line[1:] for line in block.splitlines(keepends=True)
                           if line.startswith("+") and not line.startswith("+++"))
        (directory / "vphone-surface-source.h").write_text(contents)
        cls.binary = directory / "owner"
        fixture = root / "tests/fixtures/ios_kernel_surface_harness.c"
        subprocess.run([compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-pthread", "-I", str(directory), str(fixture), "-o", str(cls.binary)],
                       check=True, capture_output=True, text=True, timeout=30)

    def check_case(self, case):
        subprocess.run([str(self.binary), case], check=True, capture_output=True, text=True, timeout=10)

    def test_native_id_bits_and_nonplanar_zero_values_are_preserved(self):
        self.check_case("decode")

    def test_short_objects_and_unrepresentable_shapes_are_refused(self):
        self.check_case("bounds")

    def test_only_the_original_el1_instruction_entry_is_selected(self):
        self.check_case("pc")

    def test_eight_threads_cannot_reopen_or_duplicate_the_capture_budget(self):
        self.check_case("budget")

    def test_startup_binding_rejects_bad_bases_and_guard_overflow(self):
        self.check_case("placement")

    def test_relocated_entry_still_requires_exact_pc_el_and_instruction(self):
        self.check_case("relocated")


if __name__ == "__main__":
    unittest.main()
