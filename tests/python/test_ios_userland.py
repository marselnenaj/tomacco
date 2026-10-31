"""Synthetic import boundaries. Fixtures never represent an iOS boot."""
import hashlib
import io
import json
from pathlib import Path
import plistlib
import struct
import sys
import tarfile
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import prepare_ios_userland as userland
from build_ios_gpu_plugin import (BRIDGE_LOG_HEADER, BRIDGE_LOG_HEADER_SHA256, BRIDGE_NAME,
                                  BRIDGE_SOURCE, BRIDGE_SOURCE_SHA256,
                                  BRIDGE_SURFACE_HEADER_PROFILES, METAL_UUID,
                                  bridge_path_header)
from ios_graphics_location import BUNDLE_PATH, PLUGIN_PERSONALITY
from ios_paravirt_firmware import der_wrap


def image():
    data = bytearray(4096)
    data[32:48] = b"NXSB" + struct.pack("<IQ", 4096, 1)
    return bytes(data)


def component():
    return der_wrap(0x30, b"".join(der_wrap(tag, payload) for tag, payload in
                                  [(0x16, b"IM4P"), (0x16, b"test"),
                                   (0x16, b"synthetic"), (4, b"not Apple firmware")]))


class UserlandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def source(self, *, pcc=False):
        root = self.root / ("pcc" if pcc else "phone")
        root.mkdir()
        components = {}
        for name in (("OS",) if pcc else userland.COMPONENTS):
            data = image() if name in userland.IMAGES else component()
            relative = name + ".bin"
            (root / relative).write_bytes(data)
            components[name] = {"Info": {"Path": relative}, "Digest": hashlib.sha384(data).digest()}
        manifest = {"ProductVersion": "26.4", "ProductBuildVersion": "synthetic",
                    "SupportedProductTypes": ["iPhone99,11" if pcc else "iPhone17,3"],
                    "BuildIdentities": [{"Info": {"DeviceClass": "vphone600ap" if pcc else "d47ap",
                                                 "Variant": ("Darwin Cloud Customer Erase Install (IPSW)" if pcc
                                                             else "Customer Erase Install (IPSW)")},
                                         "Manifest": components}]}
        (root / "BuildManifest.plist").write_bytes(plistlib.dumps(manifest))
        return root

    def arguments(self):
        phone, pcc = self.source(), self.source(pcc=True)
        return SimpleNamespace(firmware=phone, identity=0, pcc_firmware=pcc, pcc_identity=0,
                               pcc_system_image=pcc / "OS.bin", component=[], seconds=60,
                               output=self.root / "output", support=self.root, helper_engine=self.root / "qemu",
                               helper_kernel=self.root / "kernel", bios=self.root, accel="tcg")

    def test_import_keeps_independent_verified_images_and_all_original_metadata(self):
        args = self.arguments()
        before = {p: p.read_bytes() for p in args.firmware.iterdir()}
        with patch.object(userland, "extract_gpu", return_value={"synthetic": True}) as extract:
            report = userland.prepare(args)
        self.assertEqual(report["state"], "prepared")
        self.assertEqual(set(report["components"]), set(userland.COMPONENTS))
        self.assertTrue(all(row["manifest_digest_matched"] for row in report["components"].values()))
        self.assertFalse(report["guest_boot_attempted"])
        self.assertFalse(report["authentication_policy_modified"])
        self.assertFalse(report["qualified_for_app_development"])
        self.assertEqual(before, {p: p.read_bytes() for p in args.firmware.iterdir()})
        target = Path(report["components"]["OS"]["path"])
        self.assertNotEqual(target.stat().st_ino, (args.firmware / "OS.bin").stat().st_ino)
        (args.firmware / "OS.bin").write_bytes(b"source changed later")
        self.assertEqual(target.read_bytes(), image())
        self.assertEqual(args.output.stat().st_mode & 0o777, 0o700)
        extract.assert_called_once()
        with self.assertRaisesRegex(ValueError, "new userland"):
            userland.prepare(args)

    def test_corrupt_image_stops_before_the_helper_and_retains_failure(self):
        args = self.arguments()
        (args.firmware / "OS.bin").write_bytes(b"wrong build")
        with patch.object(userland, "extract_gpu") as extract, self.assertRaisesRegex(ValueError, "digest"):
            userland.prepare(args)
        extract.assert_not_called()
        report = json.loads((args.output / "userland.json").read_text())
        self.assertEqual(report["state"], "failed")
        self.assertEqual((args.firmware / "OS.bin").read_bytes(), b"wrong build")

    def test_wrong_metadata_stops_before_helper(self):
        args = self.arguments()
        data = bytearray((args.firmware / "SystemVolume.bin").read_bytes())
        data[-1] ^= 1
        (args.firmware / "SystemVolume.bin").write_bytes(data)
        with patch.object(userland, "extract_gpu") as extract, self.assertRaisesRegex(ValueError, "SystemVolume"):
            userland.prepare(args)
        extract.assert_not_called()

    def test_package_symlinks_and_component_escape_are_rejected(self):
        root = self.source()
        external = self.root / "external"
        external.mkdir()
        (external / "disk").write_bytes(image())
        (root / "link").symlink_to(external, target_is_directory=True)
        for relative in ("../external/disk", "/etc/passwd", "link/disk", "a//disk", "a/./disk"):
            with self.subTest(relative=relative), self.assertRaises((ValueError, OSError)):
                userland.snapshot(root, relative, self.root / "copy", time.monotonic() + 2)

    def test_a_streamed_copy_that_cannot_fit_is_refused_before_writing(self):
        root = self.source()
        (root / "disk").write_bytes(image())
        free = SimpleNamespace(f_bavail=(1 << 30) // 4096, f_frsize=4096)
        unsupported = OSError(userland.errno.EOPNOTSUPP, "no reflink")
        with patch.object(userland.fcntl, "ioctl", side_effect=unsupported), \
                patch.object(userland.os, "statvfs", return_value=free), \
                self.assertRaisesRegex(OSError, r"needs 1\.0 GiB in .*; 1\.0 GiB are free") as refused:
            userland.snapshot(root, "disk", self.root / "copy", time.monotonic() + 2)
        self.assertEqual(refused.exception.errno, userland.errno.ENOSPC)
        self.assertEqual((self.root / "copy").stat().st_size, 0)
        # With room for the image and the headroom the same copy is made.
        free.f_bavail += 1
        with patch.object(userland.fcntl, "ioctl", side_effect=unsupported), \
                patch.object(userland.os, "statvfs", return_value=free):
            copied = userland.snapshot(root, "disk", self.root / "copy2", time.monotonic() + 2)
        self.assertEqual((copied["copy_method"], (self.root / "copy2").read_bytes()), ("stream", image()))

    def test_raw_apfs_geometry_required_even_with_matching_digest(self):
        for index, data in enumerate((b"AEA1" + bytes(4092), b"not an image", image() + b"trailing")):
            source = self.root / f"disk-{index}"
            source.write_bytes(data)
            with self.assertRaises(ValueError):
                userland.import_image(self.root, source.name, self.root / f"copy-{index}",
                                      hashlib.sha384(data).digest(), time.monotonic() + 2)

    def archive(self, entries):
        output = self.root / "gpu.tar"
        with tarfile.open(output, "w") as archive:
            for name, data, kind in entries:
                entry = tarfile.TarInfo(name)
                entry.type = kind
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    entry.linkname = "/tmp/escape"
                else:
                    entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
        return output

    def bundle(self):
        return [(userland.BUNDLE + "/" + relative, data, tarfile.REGTYPE) for relative, data in [
            (userland.DRIVER, bytes.fromhex("cffaedfe0c000001") + b"synthetic"),
            ("Info.plist", plistlib.dumps({"CFBundleIdentifier": "com.apple.driver." + userland.DRIVER,
                                          "DTPlatformVersion": "26.4"})),
            ("_CodeSignature/CodeResources", plistlib.dumps({"files": {}}))]]

    def test_gpu_original_files_retained_and_version_checked(self):
        archive = self.archive(self.bundle())
        result = userland.unpack_gpu(archive, self.root / "gpu", "26.4")
        self.assertTrue(result["original_code_signature_preserved"])
        self.assertFalse(result["signature_verified"])
        self.assertFalse(result["compiler_plugin_present"])
        self.assertEqual(len(result["files"]), 3)
        with self.assertRaisesRegex(ValueError, "platform version"):
            userland.unpack_gpu(archive, self.root / "other", "27.0")

    def test_gpu_export_rejects_links_escape_and_duplicates(self):
        for i, extra in enumerate([
                (userland.BUNDLE + "/alias", b"", tarfile.SYMTYPE),
                (userland.BUNDLE + "/alias", b"", tarfile.LNKTYPE),
                (userland.BUNDLE + "/../escape", b"bad", tarfile.REGTYPE),
                ("outside/file", b"bad", tarfile.REGTYPE),
                self.bundle()[0]]):
            with self.subTest(entry=extra[0]), self.assertRaises(ValueError):
                userland.unpack_gpu(self.archive([*self.bundle(), extra]), self.root / f"bad-{i}", "26.4")

    def test_cancellation_leaves_no_prepared_receipt(self):
        args = self.arguments()
        with patch.object(userland, "extract_gpu", side_effect=KeyboardInterrupt("cancelled")), self.assertRaises(KeyboardInterrupt):
            userland.prepare(args)
        self.assertEqual(json.loads((args.output / "userland.json").read_text())["state"], "cancelled")

    def plugin(self):
        directory = self.root / "plugin-build"
        directory.mkdir()
        data = struct.pack("<8I", 0xfeedfacf, 0x100000c, 0x80000002, 6, 1, 16, 0, 0)
        data += struct.pack("<4I", 0x1d, 16, 48, 101)
        cd = struct.pack(">9I4BI", 0xfade0c02, 81, 0x20001, 2, 49, 44, 0, 1, 48, 32, 2, 0, 12, 0)
        cd += b"test\0" + hashlib.sha256(data).digest()
        data += struct.pack(">5I", 0xfade0cc0, 101, 1, 0, 20) + cd
        (directory / userland.PLUGIN_NAME).write_bytes(data)
        (directory / "UPSTREAM-LICENSE").write_text("Synthetic test fixture; not firmware")
        record = {"scope": "ios-paravirtual-gpu-compiler-plugin", "state": "built",
                  "source_sha256": userland.PLUGIN_SOURCE_SHA256,
                  "plugin": {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}}
        (directory / "plugin.json").write_text(json.dumps(record))
        return directory

    def test_optional_compiler_kept_separate_from_original_gpu_bundle(self):
        args = self.arguments()
        args.gpu_plugin_build = self.plugin()
        archive = self.archive(self.bundle())
        with patch.object(userland, "extract_gpu", side_effect=lambda *a:
                          userland.unpack_gpu(archive, args.output / "gpu", "26.4")):
            report = userland.prepare(args)
        compiler = report["gpu_compiler"]
        self.assertFalse(compiler["original_apple_bundle_modified"])
        self.assertFalse(compiler["guest_execution_verified"])
        self.assertEqual(Path(compiler["path"]).read_bytes(),
                         (args.gpu_plugin_build / userland.PLUGIN_NAME).read_bytes())
        self.assertNotEqual(Path(compiler["path"]).stat().st_ino,
                            (args.gpu_plugin_build / userland.PLUGIN_NAME).stat().st_ino)
        self.assertFalse(report["gpu"]["compiler_plugin_present"])
        self.assertFalse((args.output / "gpu" / userland.PLUGIN_NAME).exists())
        self.assertTrue(compiler["integrity"]["code_pages_verified"])
        self.assertEqual(compiler["integrity"]["flags"], 2)

    def test_changed_compiler_rejected_before_large_images_or_helper(self):
        args = self.arguments()
        args.gpu_plugin_build = self.plugin()
        library = args.gpu_plugin_build / userland.PLUGIN_NAME
        library.write_bytes(library.read_bytes() + b"changed")
        with patch.object(userland, "import_image") as copy, patch.object(userland, "extract_gpu") as extract:
            with self.assertRaisesRegex(ValueError, "changed after"):
                userland.prepare(args)
        copy.assert_not_called()
        extract.assert_not_called()
        report = json.loads((args.output / "userland.json").read_text())
        self.assertEqual((report["state"], report["phase"]), ("failed", "gpu-compiler"))

    def bridge(self, directory):
        # Valid synthetic CodeDirectory; this fixture never executes guest code.
        data = (directory / userland.PLUGIN_NAME).read_bytes()
        (directory / BRIDGE_NAME).write_bytes(data)
        source = Path(__file__).resolve().parents[2] / "integrations/ios-emulator" / BRIDGE_SOURCE
        (directory / BRIDGE_SOURCE).write_bytes(source.read_bytes())
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), BRIDGE_SOURCE_SHA256)
        # The bridge build ships its diagnostic, IOSurface and path headers
        # beside the source; the import rechecks each against the record.
        surface = BRIDGE_SURFACE_HEADER_PROFILES.get(BRIDGE_SOURCE_SHA256, {})
        for name in (BRIDGE_LOG_HEADER, *surface):
            (directory / name).write_bytes((source.parent / name).read_bytes())
        path_header = bridge_path_header()
        (directory / "ios-gpu-path.h").write_bytes(path_header)
        record = json.loads((directory / "plugin.json").read_text())
        record["bridge"] = {"source_sha256": BRIDGE_SOURCE_SHA256, "metal_uuid": METAL_UUID,
                            "llvm_version": 32023, "graphics_driver_uuid": "7E55CCEA-9621-3AF3-9396-E285AB7DEE23",
                            "memoryless_storage": "private-backing-logical-memoryless",
                            "graphics_bundle_path": BUNDLE_PATH, "graphics_plugin_personality": PLUGIN_PERSONALITY,
                            "log_header_sha256": BRIDGE_LOG_HEADER_SHA256,
                            "path_header_sha256": hashlib.sha256(path_header).hexdigest(),
                            **({"surface_header_sha256": surface} if surface else {}),
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "bytes": len(data), "integrity": userland.inspect_plugin(data)}
        (directory / "plugin.json").write_text(json.dumps(record))
        return record

    def test_bridge_import_copies_validated_source_and_binary(self):
        directory = self.plugin()
        record = self.bridge(directory)
        output = self.root / "import"
        output.mkdir()
        result = userland.import_plugin(directory, output)
        self.assertEqual(result["bridge"]["integrity"], record["bridge"]["integrity"])
        for name in (BRIDGE_NAME, BRIDGE_SOURCE):
            imported = output / "gpu-compiler" / name
            self.assertEqual(imported.read_bytes(), (directory / name).read_bytes())
            self.assertNotEqual(imported.stat().st_ino, (directory / name).stat().st_ino)
            self.assertEqual(imported.stat().st_mode & 0o777, 0o600)

    def test_bridge_mutations_fail_before_creating_import_directory(self):
        directory = self.plugin()
        record = self.bridge(directory)
        originals = {name: (directory / name).read_bytes()
                     for name in (BRIDGE_NAME, BRIDGE_SOURCE, "plugin.json")}
        output = self.root / "import"
        output.mkdir()
        for mutation in ("source", "binary", "uuid", "driver_uuid", "memoryless", "llvm", "bundle_path",
                         "personality", "signature", "symlink", "missing"):
            with self.subTest(mutation=mutation):
                changed = json.loads(json.dumps(record))
                if mutation == "source":
                    (directory / BRIDGE_SOURCE).write_bytes(b"changed")
                elif mutation == "binary":
                    (directory / BRIDGE_NAME).write_bytes(originals[BRIDGE_NAME] + b"changed")
                elif mutation == "uuid":
                    changed["bridge"]["metal_uuid"] = "unsupported"
                elif mutation == "driver_uuid":
                    changed["bridge"]["graphics_driver_uuid"] = "unsupported"
                elif mutation == "memoryless":
                    changed["bridge"]["memoryless_storage"] = "unsupported"
                elif mutation == "llvm":
                    changed["bridge"]["llvm_version"] += 1
                elif mutation == "bundle_path":
                    changed["bridge"]["graphics_bundle_path"] = "/private/preboot/tomacco/gpu.bundle"
                elif mutation == "personality":
                    changed["bridge"]["graphics_plugin_personality"] = "../../../private/preboot/tomacco/gpu"
                elif mutation == "signature":
                    changed["bridge"]["integrity"]["cdhash"] = "00" * 20
                elif mutation in ("symlink", "missing"):
                    (directory / BRIDGE_NAME).unlink()
                    if mutation == "symlink":
                        (directory / BRIDGE_NAME).symlink_to(directory / userland.PLUGIN_NAME)
                (directory / "plugin.json").write_text(json.dumps(changed))
                with self.assertRaises((ValueError, OSError)):
                    userland.import_plugin(directory, output)
                self.assertFalse((output / "gpu-compiler").exists())
                for name, data in originals.items():
                    (directory / name).unlink(missing_ok=True)
                    (directory / name).write_bytes(data)

    def test_signature_requires_adhoc_flag_and_matching_code_pages(self):
        directory = self.plugin()
        original = (directory / userland.PLUGIN_NAME).read_bytes()
        wrong_flags = bytearray(original)
        struct.pack_into(">I", wrong_flags, 48 + 20 + 12, 0)
        wrong_page = bytearray(original)
        wrong_page[28] ^= 1
        for data, message in [(wrong_flags, "CS_ADHOC"), (wrong_page, "code page changed"),
                              (original[:50], "signature bounds")]:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                userland.inspect_plugin(data)


if __name__ == "__main__":
    unittest.main()
