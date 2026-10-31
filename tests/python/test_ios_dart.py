"""Compile and execute the same translation core used by the optional QEMU model."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import build_ios_emulator as engine


class DartTests(unittest.TestCase):
    def test_engine_sources_preserve_existing_edits(self):
        with tempfile.TemporaryDirectory(prefix="tomacco-dart-source-") as directory:
            source = Path(directory)
            (source / "hw/arm").mkdir(parents=True)
            record = engine.stage_hardware_sources(source, "sptm")
            self.assertEqual(set(record), {"hw/arm/" + name for name in engine.HARDWARE_SOURCES})
            self.assertEqual(engine.stage_hardware_sources(source, "sptm"), record)
            target = source / "hw/arm/dart-t8110.h"
            target.write_text("user hardware changes")
            with self.assertRaisesRegex(RuntimeError, "Preserved different"):
                engine.stage_hardware_sources(source, "sptm")
            self.assertEqual(target.read_text(), "user hardware changes")
            target.unlink()
            target.symlink_to(ROOT / "integrations/ios-emulator/dart-t8110.h")
            with self.assertRaisesRegex(RuntimeError, "Preserved different"):
                engine.stage_hardware_sources(source, "sptm")
            self.assertEqual(engine.stage_hardware_sources(source, "inferno"), {})

    def test_dma_contract(self):
        compiler = shutil.which("cc")
        self.assertIsNotNone(compiler, "The emulator hardware tests require a C compiler")
        with tempfile.TemporaryDirectory(prefix="tomacco-dart-test-") as directory:
            program = Path(directory) / "dart-test"
            subprocess.run([compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=undefined", "-fno-sanitize-recover=all",
                            "-I", str(ROOT / "integrations/ios-emulator"),
                            str(ROOT / "tests/fixtures/ios_dart_test.c"), "-o", str(program)],
                           check=True, capture_output=True, text=True, timeout=30)
            result = subprocess.run([program], check=True, capture_output=True, text=True, timeout=10)
            self.assertIn("boundaries, faults, IRQ and locks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
