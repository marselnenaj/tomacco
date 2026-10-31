"""Exercise the production MCC register state with undefined-behavior checks."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class MccTests(unittest.TestCase):
    def test_register_contract(self):
        compiler = shutil.which("cc")
        self.assertIsNotNone(compiler, "The emulator hardware tests require a C compiler")
        with tempfile.TemporaryDirectory(prefix="tomacco-mcc-core-") as directory:
            program = Path(directory) / "mcc-test"
            subprocess.run([compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=undefined", "-fno-sanitize-recover=all",
                            "-I", str(ROOT / "integrations/ios-emulator"),
                            str(ROOT / "tests/fixtures/ios_mcc_test.c"), "-o", str(program)],
                           check=True, capture_output=True, text=True, timeout=30)
            result = subprocess.run([program], check=True, capture_output=True, text=True, timeout=10)
            self.assertIn("MCC channel isolation, cache transitions, strict faults and reset passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
