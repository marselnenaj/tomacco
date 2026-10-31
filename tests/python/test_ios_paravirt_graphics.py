"""Integrity and destination boundaries of the optional guest graphics import."""
import hashlib
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

from ios_paravirt_graphics import (DESTINATION_SHELL, DRIVER_FILES, NAME, cache_entries,
                                   extend_cache, helper_script)
from build_ios_gpu_plugin import BRIDGE_NAME
from ios_graphics_location import GRAPHICS_RELATIVE_PATH


def cache(*entries):
    return struct.pack("<I16sI", 2, bytes(16), len(entries)) + b"".join(entries)


class GraphicsTests(unittest.TestCase):
    def test_preserves_every_original_policy_byte_and_adds_only_two_hashes(self):
        originals = [bytes([value]) * 20 + bytes([2, value, 0, 0]) for value in (1, 2)]
        driver, compiler = b"D" * 20, b"C" * 20
        trusted = driver + bytes.fromhex("02400200")
        combined, report = extend_cache(cache(*originals), cache(trusted), driver, compiler)
        self.assertEqual(set(cache_entries(combined).values()),
                         {*originals, trusted, compiler + trusted[20:]})
        self.assertTrue(report["compiler_platform_policy_grant"])
        self.assertFalse(report["compiler_apple_authenticated"])
        for original, pcc, first, second in [
            (cache(*originals)[:-1], cache(trusted), driver, compiler),
            (cache(originals[0], originals[0]), cache(trusted), driver, compiler),
            (cache(trusted), cache(trusted), driver, compiler),
            (cache(*originals), cache(trusted[:-1] + b"x"), driver, compiler),
            (cache(*originals), cache(trusted), driver, driver),
            (cache(*originals), cache(), driver, compiler),
        ]:
            with self.subTest(first=first, second=second), self.assertRaises(ValueError):
                extend_cache(original, pcc, first, second)

    def test_destination_refuses_all_existing_files_directories_and_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def check():
                return subprocess.run(["sh", "-eu", "-c", DESTINATION_SHELL
                                       + '\nprepare_graphics_destination "$1"', "graphics", str(root)],
                                      capture_output=True, text=True)
            self.assertEqual(check().returncode, 0)
            target = root / GRAPHICS_RELATIVE_PATH
            target.parent.mkdir()
            for kind in ("file", "directory", "link"):
                if kind == "file":
                    target.write_text("existing guest data")
                elif kind == "directory":
                    target.mkdir()
                else:
                    target.symlink_to(root / "missing")
                self.assertNotEqual(check().returncode, 0)
                self.assertIn("already exists", check().stderr)
                target.rmdir() if kind == "directory" else target.unlink()

    def test_adapted_driver_requires_bridge_and_preserves_original_iphone_policy(self):
        original = b"I" * 20 + bytes.fromhex("02400000")
        driver, compiler, adapted, bridge = (bytes([value]) * 20 for value in b"DCAB")
        trusted = driver + bytes.fromhex("02400200")
        combined, report = extend_cache(cache(original), cache(trusted), driver, compiler,
                                        adapted_driver=adapted, bridge=bridge)
        entries = cache_entries(combined)
        self.assertEqual(set(entries.values()),
                         {original, *(digest + trusted[20:] for digest in (compiler, adapted, bridge))})
        self.assertNotIn(driver, entries)
        self.assertFalse(report["original_pcc_driver_entry_installed"])
        self.assertFalse(report["driver_apple_authenticated"])
        self.assertFalse(report["bridge_apple_authenticated"])
        for first, second in ((None, bridge), (adapted, None), (driver, bridge),
                              (adapted, compiler), (bridge, bridge), (adapted[:-1], bridge)):
            with self.subTest(first=first, second=second), self.assertRaises(ValueError):
                extend_cache(cache(original), cache(trusted), driver, compiler,
                             adapted_driver=first, bridge=second)
        with self.assertRaisesRegex(ValueError, "already exists"):
            extend_cache(cache(bridge + trusted[20:]), cache(trusted), driver, compiler,
                         adapted_driver=adapted, bridge=bridge)

    def test_helper_rechecks_payload_and_sets_guest_executable_permissions(self):
        for with_bridge in (False, True):
            with self.subTest(bridge=with_bridge):
                self.check_helper(with_bridge)

    def check_helper(self, with_bridge):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            payload, helper = output / "graphics/payload", output / "helper"
            payload.mkdir(parents=True)
            helper.mkdir()
            rows = []
            for name in [*DRIVER_FILES, NAME, *([BRIDGE_NAME] if with_bridge else [])]:
                path = payload / name
                path.parent.mkdir(exist_ok=True, parents=True)
                data = name.encode()
                path.write_bytes(data)
                rows.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
            graphics = {"files": rows}
            if with_bridge:
                graphics["compiler_bridge"] = {"synthetic": True}
            check, install = helper_script(output, helper, graphics)
            self.assertIn("prepare_graphics_destination /mnt/preboot", check)
            self.assertIn("sha256sum -c /gpu.sha256", install)
            self.assertEqual((helper / "gpu.bundle" / NAME).stat().st_mode & 0o777, 0o755)
            if with_bridge:
                self.assertEqual((helper / "gpu.bundle" / BRIDGE_NAME).stat().st_mode & 0o777, 0o755)
            (payload / (BRIDGE_NAME if with_bridge else NAME)).write_bytes(b"changed")
            second = output / "second"
            second.mkdir()
            with self.assertRaisesRegex(ValueError, "changed after"):
                helper_script(output, second, graphics)
            with self.assertRaisesRegex(ValueError, "inventory"):
                helper_script(output, second, {**graphics, "files": rows + [rows[0]]})
            if with_bridge:
                with self.assertRaisesRegex(ValueError, "inventory"):
                    helper_script(output, second, {"files": rows})
