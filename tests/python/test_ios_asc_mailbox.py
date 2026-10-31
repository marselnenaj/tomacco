"""Compile and exercise the production ASC transport without Apple firmware."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class AscMailboxTests(unittest.TestCase):
    def test_controller_contract(self):
        with tempfile.TemporaryDirectory(prefix="tomacco-asc-controller-") as directory:
            executable = Path(directory) / "test"
            compiled = subprocess.run([shutil.which("cc"), "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                                      "-fsanitize=undefined", "-fno-sanitize-recover=all",
                                      "-I", str(ROOT / "integrations/ios-emulator"),
                                      str(ROOT / "tests/fixtures/ios_asc_controller_test.c"), "-o", str(executable)],
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([executable], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("512 sources, IRQ/FIQ events, timers, queues, reset and strict errors passed", result.stdout)

    def test_transport_contract(self):
        compiler = shutil.which("cc")
        self.assertIsNotNone(compiler)
        with tempfile.TemporaryDirectory(prefix="tomacco-asc-core-") as directory:
            executable = Path(directory) / "test"
            subprocess.run([compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=undefined", "-fno-sanitize-recover=all",
                            "-I", str(ROOT / "integrations/ios-emulator"),
                            str(ROOT / "tests/fixtures/ios_asc_mailbox_test.c"), "-o", str(executable)],
                           check=True, capture_output=True, text=True, timeout=30)
            result = subprocess.run([executable], check=True, capture_output=True, text=True, timeout=10)
            self.assertIn("ASC bidirectional queues, wraparound, atomic messages, IRQ levels and strict bus errors passed", result.stdout)
