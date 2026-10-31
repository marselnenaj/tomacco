"""Bounded native reply decoder; this fixture does not run iOS or qualify access."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class ForegroundContractTests(unittest.TestCase):
    def test_status_and_bounded_native_identifier_are_independent(self):
        compiler = shutil.which("clang")
        if not compiler:
            self.skipTest("Clang is required for the actual C decoder fixture")
        repo = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix="tomacco-foreground-contract-") as directory:
            executable = Path(directory) / "foreground-contract"
            result = subprocess.run(
                [compiler, "-O2", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function",
                 "-ffunction-sections", "-fdata-sections",
                 str(repo / "tests/fixtures/ios_foreground_contract_harness.c"),
                 "-Wl,--gc-sections", "-o", str(executable)],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(executable)], check=True, timeout=5)


if __name__ == "__main__":
    unittest.main()
