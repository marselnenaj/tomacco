"""Production PTD register-state tests; no Apple firmware required."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PtdTests(unittest.TestCase):
    def test_transport_contract(self):
        compiler = shutil.which("cc")
        self.assertIsNotNone(compiler)
        with tempfile.TemporaryDirectory(prefix="tomacco-ptd-core-") as directory:
            executable = Path(directory) / "test"
            subprocess.run([compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=undefined", "-fno-sanitize-recover=all",
                            "-I", str(ROOT / "integrations/ios-emulator"),
                            str(ROOT / "tests/fixtures/ios_ptd_test.c"), "-o", str(executable)],
                           check=True, capture_output=True, text=True, timeout=30)
            result = subprocess.run([executable], check=True, capture_output=True, text=True, timeout=10)
            self.assertIn("PTD messages, consumption, dropped updates, timestamps and IRQ routing passed", result.stdout)
