"""The helpers of the virtual iOS device stop with a clear error on a host they cannot serve."""
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import build_ios_emulator  # noqa: E402
import ios_virtual_profile  # noqa: E402
import prepare_ios_emulator  # noqa: E402


class HostArchitectureTests(unittest.TestCase):
    def refused(self, module, *options):
        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", [module.__name__, *options]), \
                mock.patch("platform.machine", return_value="aarch64"), \
                mock.patch("platform.system", return_value="Linux"), \
                contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as stopped:
            module.main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("needs an x86_64 Linux host", stderr.getvalue())
        self.assertIn("aarch64", stderr.getvalue())

    def test_preparing_a_guest_is_refused(self):
        self.refused(prepare_ios_emulator, "--output", "/nowhere", "--firmware", "/nowhere")

    def test_building_the_engine_is_refused(self):
        self.refused(build_ios_emulator)

    def test_making_a_profile_is_refused(self):
        self.refused(ios_virtual_profile, "--check")


if __name__ == "__main__":
    unittest.main()
