"""Exercise the bridge's allocation, validation and texture-view contracts."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from ios_graphics_location import bridge_path_header


class GpuBridgeTests(unittest.TestCase):
    def test_surface_observer_forwarding_raw_metadata_and_limits(self):
        compiler = shutil.which("clang") or shutil.which("cc")
        if compiler is None:
            self.skipTest("A C compiler is required for the surface observer behavior test")
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix="tomacco-surface-observer-test-") as directory:
            binary = Path(directory) / "surface-test"
            build = subprocess.run([compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                                    str(root / "tests/fixtures/ios_surface_observer_harness.c"),
                                    "-o", str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_memoryless_ownership_validation_views_and_profile_guard(self):
        compiler = shutil.which("clang") or shutil.which("cc")
        if compiler is None:
            self.skipTest("A C compiler is required for the bridge behavior test")
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix="tomacco-bridge-test-") as directory:
            binary = Path(directory) / "bridge-test"
            (Path(directory) / "ios-gpu-path.h").write_bytes(bridge_path_header())
            build = subprocess.run([compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                                    "-I" + directory,
                                    str(root / "tests/fixtures/ios_gpu_bridge_harness.c"),
                                    "-o", str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
