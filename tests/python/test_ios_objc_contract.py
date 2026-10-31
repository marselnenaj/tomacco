"""Compile the actual metadata guard; scripted records do not qualify iOS."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class ObjcContractTests(unittest.TestCase):
    def test_class_arguments_are_distinct_and_all_method_fields_remain_guarded(self):
        compiler = shutil.which("clang")
        if not compiler:
            self.skipTest("Clang is required for the owned C fixture")
        repo = Path(__file__).resolve().parents[2]
        source = repo / "tests/fixtures/ios_objc_contract_harness.c"
        with tempfile.TemporaryDirectory(prefix="tomacco-objc-contract-") as directory:
            binary = Path(directory)/"contract"
            result = subprocess.run([compiler, "-O2", "-Wall", "-Wextra", "-Werror",
                                     str(source), "-o", str(binary)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(binary)], check=True, timeout=5)


if __name__ == "__main__":
    unittest.main()
