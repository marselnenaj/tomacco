"""Native C ownership/layout fixture; does not simulate or qualify iOS launch."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class LaunchProcessOwnerTests(unittest.TestCase):
    def test_actual_completion_owner_retains_first_result_and_disposes_once(self):
        compiler = shutil.which("clang")
        if not compiler:
            self.skipTest("Clang is required for the owned C fixture")
        repo = Path(__file__).resolve().parents[2]
        source = repo / "tests/fixtures/ios_launch_process_harness.c"
        with tempfile.TemporaryDirectory(prefix="tomacco-launch-owner-") as directory:
            executable = Path(directory) / "launch-owner"
            result = subprocess.run(
                [compiler, "-O2", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function",
                 "-ffunction-sections", "-fdata-sections", str(source),
                 "-Wl,--gc-sections", "-o", str(executable)],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(executable)], check=True, timeout=5)


if __name__ == "__main__":
    unittest.main()
