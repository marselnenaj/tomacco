"""The profile generator: what a fresh user is told, what it writes, and the kernel chain it relies on."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import ios_virtual_iphone  # noqa: E402
import ios_virtual_profile as tool  # noqa: E402
from ios_paravirt_kernel import OWN_INPUT_RANGES, kernel_profile  # noqa: E402
import prepare_ios_adapter_pin as pinning  # noqa: E402
import prepare_ios_fileport_compat as fileport  # noqa: E402
import prepare_ios_workloop_compat as workloop  # noqa: E402

KERNEL_LENGTH = max(end for _start, end in OWN_INPUT_RANGES) + 64
ADAPTER = "1474774d2867bad583f620047e7e277975083e0d"


class ArtifactsCase(unittest.TestCase):
    """A workspace of stand-in artifacts: real paths and shapes, no firmware.

    The checks that parse Apple formats or signed payloads are replaced; the
    tools they call have their own tests. A kernel is its bytes' profile."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="tomacco-profile-")
        self.addCleanup(directory.cleanup)
        self.root = root = Path(directory.name)
        def write(name, data=b"x", mode=0o644):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(mode)
            return path
        self.write = write
        self.engine = write("engine/qemu-system-aarch64", b"engine", 0o755)
        self.backend = write("control/vhost-device-vsock", b"backend", 0o755)
        self.helper = write("helper/qemu-system-x86_64", b"helper", 0o755)
        write("support/busybox/busybox"), write("support/apfs/apfs.ko")
        self.helper_kernel = write("support/apfs/vmlinuz")
        (root / "bios").mkdir()
        for name in ("sptm", "txm", "sep", "ramdisk.dmg"):
            write("boot/" + name)
        for name in ("system.tc", "device-tree", "system.root_hash"):
            write("cryptex/" + name)
        self.manifest = write("cryptex/graft-objects/apticket.im4m", b"manifest")
        write("cryptex/cryptex.json", json.dumps({"boot_inputs": {"boot_manifest": str(self.manifest)}}).encode())
        write("cryptex/graphics/payload/p", b"\0")
        (root / "tools").mkdir()
        self.tools = root / "tools"
        write("tools/guest-tools.json", json.dumps({"programs": [
            {"path": "usr/libexec/libtomacco-development-signing.dylib", "cdhash": ADAPTER}]}).encode())
        self.kernel_data = bytes(KERNEL_LENGTH - 20) + bytes.fromhex(ADAPTER)
        self.kernel = write("kernel/" + pinning.KERNEL_NAME, self.kernel_data)
        for name in ("system.raw", "auxiliary.raw", "sep.raw"):
            write("install/" + name)
        self.install = root / "install"
        self.install_record({"state": "installed", "build": {"files": ["same"]}})
        for name, value in (("PINNABLE_PROFILE", kernel_profile(self.kernel_data)),
                            ("payload", lambda _tools, _cache: ({"files": ["same"]}, {}, b"")),
                            ("graphics_payload", lambda _directory: None),
                            ("validate_im4m", lambda _data: None),
                            ("manifest_name", lambda _tree: "apticket.im4m"),
                            ("decode", lambda _data: None), ("ap_cpu_count", lambda _tree: 6)):
            patcher = mock.patch.object(tool, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def install_record(self, record):
        (self.install / "guest-tools-install.json").write_text(json.dumps(record))

    def options(self, **changes):
        values = {"engine": self.engine, "guest-control-backend": self.backend, "helper-engine": self.helper,
                  "bios": self.root / "bios", "support": self.root / "support", "helper-kernel": self.helper_kernel,
                  "cryptex": self.root / "cryptex", "boot-inputs": self.root / "boot", "tools": self.tools,
                  "kernel": self.kernel, "install": self.install, **changes}
        return [item for key, value in values.items() if value is not None for item in (f"--{key}", str(value))]

    def run_tool(self, *options):
        """(exit code, its JSON reply) of the tool's command line."""
        lines = []
        with mock.patch.object(sys, "argv", ["ios_virtual_profile.py", *options]), \
                mock.patch("builtins.print", lambda text, **kwargs: None if kwargs.get("file") else lines.append(text)):
            code = tool.main()
        return code, json.loads(lines[-1])

    def problems(self, **changes):
        code, reply = self.run_tool("--check", *self.options(**changes))
        self.assertEqual(code == 0, reply["ok"])
        return {row["input"]: row for row in reply["missing"]}


class GeneratorTests(ArtifactsCase):
    def test_a_complete_set_of_artifacts_checks_out(self):
        self.assertEqual(self.problems(), {})

    def test_a_fresh_workspace_hears_every_step_in_order_with_its_command(self):
        with mock.patch.object(tool, "REPO", self.root / "fresh"):
            code, reply = self.run_tool("--check")
        self.assertEqual(code, 1)
        rows = reply["missing"]
        order = [row["input"] for row in rows]
        for earlier, later in (("engine", "guest tools"), ("system trust cache", "guest tools"),
                               ("guest tools", "kernel"), ("kernel", "first guest-tools install")):
            self.assertLess(order.index(earlier), order.index(later))
        by_name = {row["input"]: row for row in rows}
        self.assertIn("tools/build_ios_paravirt.py", by_name["engine"]["make"])
        self.assertIn("tools/build_ios_guest_control.py", by_name["guest control backend"]["make"])
        self.assertIn("ios.emulator.prepare_cryptexes", by_name["system trust cache"]["make"])
        self.assertIn("ios.emulator.build_guest_tools", by_name["guest tools"]["make"])
        self.assertIn("prepare_ios_workloop_compat.py", by_name["kernel"]["make"])
        self.assertIn("ios.emulator.prepare_guest_tools", by_name["first guest-tools install"]["make"])
        # The one input no tool makes says so, instead of naming a command.
        self.assertIn("no tool here makes it", by_name["boot manifest"]["make"])
        self.assertTrue(all(row["make"] and row["problem"] for row in rows))

    def test_a_check_writes_nothing(self):
        directory = self.root / "phone"
        code, _reply = self.run_tool("--check", "--directory", str(directory), *self.options())
        self.assertEqual(code, 0)
        self.assertFalse(directory.exists())

    def test_the_written_profile_has_the_engine_options_and_what_a_start_reads(self):
        directory = self.root / "phone"
        code, reply = self.run_tool("--directory", str(directory), *self.options())
        self.assertEqual(code, 0, reply)
        self.assertEqual(reply["engine_options"], {"target_page": "16k", "bti": "off"})
        self.assertIn(str(directory), reply["next"])
        profile = json.loads((directory / "profile.json").read_text())
        state = json.loads((directory / "state.json").read_text())
        # What ios.virtual.configure (virtual_phone.rs records) demands.
        self.assertEqual(profile["format"], 1)
        for key in ("graphics", "engine", "application", "installs"):
            self.assertTrue(profile[key])
        self.assertIsInstance(state["run"], int)
        self.assertIs(state["closed"], True)
        self.assertEqual((state["install"], state["tools"]), (str(self.install), str(self.tools)))
        self.assertIsNone(state["guest_job"])
        boot = profile["boot"]
        self.assertEqual((boot["target_page"], boot["bti"]), ("16k", "off"))
        self.assertIn(" cpus=6 ", boot["boot_args"])
        self.assertEqual(boot["boot_manifest"], str(self.manifest))
        self.assertEqual(boot["sep_manifest"], str(self.manifest))
        self.assertEqual(boot["kernel"], str(self.kernel))
        self.assertEqual(profile["prepare"]["trustcache"], str(self.root / "cryptex/system.tc"))
        self.assertEqual(profile["graphics"], str(self.root / "cryptex/graphics/payload"))
        self.assertTrue((directory / "installs").is_dir())
        self.assertEqual((directory / "profile.json").stat().st_mode & 0o777, 0o600)
        # A start takes the first install as its verified base, and sends the options to the launcher.
        phone = ios_virtual_iphone.Phone(None, directory)
        self.assertEqual(phone.state["verified"]["install"], str(self.install))
        self.assertEqual(phone.state["verified"]["trustcache"], profile["prepare"]["trustcache"])
        self.assertEqual(phone.tools_for_start(None), self.tools)
        request = ios_virtual_iphone.boot_request(phone.profile, {"device_tree": "/tree"}, None)
        self.assertEqual((request["target_page"], request["bti"]), ("16k", "off"))
        self.assertEqual(request["engine"], str(self.engine))

    def test_an_existing_profile_is_never_overwritten(self):
        directory = self.root / "phone"
        directory.mkdir()
        (directory / "profile.json").write_text("mine")
        code, reply = self.run_tool("--directory", str(directory), *self.options())
        self.assertEqual(code, 1)
        self.assertIn("never overwritten", reply["error"])
        self.assertEqual((directory / "profile.json").read_text(), "mine")
        self.assertFalse((directory / "state.json").exists())

    def test_clang_goes_into_the_toolchain_only_when_given(self):
        for name, extra in (("plain", ()), ("named", ("--clang", "/toolchains/clang"))):
            directory = self.root / name
            self.assertEqual(self.run_tool("--directory", str(directory), *self.options(), *extra)[0], 0)
            profile = json.loads((directory / "profile.json").read_text())
            self.assertEqual(profile.get("toolchain"), {"clang": "/toolchains/clang"} if extra else None)

    def test_an_engine_built_without_the_options_patches_is_refused_by_its_own_manifest(self):
        digest = hashlib.sha256(b"engine").hexdigest()
        manifest = self.engine.parent / "build-manifest.json"
        manifest.write_text(json.dumps({"engine_sha256": digest, "patches": {"qemu-arm-bti-property.patch": "x"}}))
        row = self.problems()["engine"]
        self.assertIn("qemu-arm-target-page-16k.patch", row["problem"])
        self.assertIn("build_ios_paravirt.py", row["make"])
        # The manifest of another binary says nothing about this one.
        manifest.write_text(json.dumps({"engine_sha256": "0" * 64, "patches": {}}))
        self.assertEqual(self.problems(), {})
        manifest.write_text(json.dumps({"engine_sha256": digest, "patches": dict.fromkeys(tool.ENGINE_PATCHES, "x")}))
        self.assertEqual(self.problems(), {})

    def test_each_step_of_the_kernel_chain_names_the_next_one(self):
        for name, reason in (("PREPARED_PROFILE", "fileport and workloop"), ("FILEPORT_PROFILE", "workloop step")):
            with mock.patch.object(tool, name, kernel_profile(self.kernel_data)), \
                    mock.patch.object(tool, "PINNABLE_PROFILE", "other"):
                self.assertIn(reason, self.problems()["kernel"]["problem"])
        with mock.patch.object(tool, "PINNABLE_PROFILE", "other"):
            row = self.problems()["kernel"]
            self.assertIn("not derived from a prepared original", row["problem"])
            self.assertIn("prepare_ios_fileport_compat.py", row["make"])

    def test_a_kernel_for_other_tools_or_of_another_name_is_refused(self):
        unpinned = self.write("other/" + pinning.KERNEL_NAME, bytes(KERNEL_LENGTH))
        with mock.patch.object(tool, "PINNABLE_PROFILE", kernel_profile(bytes(KERNEL_LENGTH))):
            self.assertIn(f"does not pin the development-signing adapter {ADAPTER}",
                          self.problems(kernel=unpinned)["kernel"]["problem"])
        renamed = self.write("other/kernelcache", self.kernel_data)
        self.assertIn(pinning.KERNEL_NAME, self.problems(kernel=renamed)["kernel"]["problem"])

    def test_an_install_of_other_tools_or_an_unfinished_one_is_refused(self):
        self.install_record({"state": "installed", "build": {"files": ["other"]}})
        self.assertIn("another guest-tools build", self.problems()["first guest-tools install"]["problem"])
        self.install_record({"state": "failed"})
        self.assertIn("is failed, not installed", self.problems()["first guest-tools install"]["problem"])
        self.install_record({"state": "installed", "build": {"files": ["same"]}})
        (self.install / "sep.raw").unlink()
        self.assertIn("lacks sep.raw", self.problems()["first guest-tools install"]["problem"])

    def test_tools_of_another_trust_cache_report_the_reason_of_the_check(self):
        def refuse(_tools, _cache):
            raise ValueError("Guest tools were built for a different trust cache")
        with mock.patch.object(tool, "payload", refuse):
            self.assertIn("different trust cache", self.problems()["guest tools"]["problem"])

    def test_the_real_checks_refuse_bad_inputs_without_the_stand_ins(self):
        self.write("cryptex/graphics/payload/extra.a2s")
        with mock.patch.object(tool, "graphics_payload", __import__("prepare_ios_guest_tools").graphics_payload):
            self.assertIn("unexpected", self.problems()["graphics payload"]["problem"])
        with mock.patch.object(tool, "validate_im4m", __import__("ios_paravirt_firmware").validate_im4m):
            self.assertIn("boot manifest", self.problems())
        with mock.patch.object(tool, "decode", __import__("prepare_ios_paravirt").decode):
            self.assertIn("device tree", self.problems())


class KernelProfileTests(unittest.TestCase):
    def test_only_the_users_own_bytes_are_left_out_of_a_profile(self):
        data = bytearray(KERNEL_LENGTH)
        base = kernel_profile(bytes(data))
        for start, end in OWN_INPUT_RANGES:
            changed = bytearray(data)
            changed[start] = changed[end - 1] = 1
            self.assertEqual(kernel_profile(bytes(changed)), base)
            changed = bytearray(data)
            changed[start - 1] = 1
            self.assertNotEqual(kernel_profile(bytes(changed)), base)
            changed = bytearray(data)
            changed[end] = 1
            self.assertNotEqual(kernel_profile(bytes(changed)), base)

    def test_the_tools_still_refuse_a_kernel_of_any_other_profile(self):
        with tempfile.TemporaryDirectory(prefix="tomacco-profile-") as directory:
            kernel = Path(directory) / pinning.KERNEL_NAME
            kernel.write_bytes(bytes(KERNEL_LENGTH))
            with self.assertRaisesRegex(ValueError, "exact recorded input kernel"):
                fileport.build(kernel, Path(directory) / "out")
            with self.assertRaisesRegex(ValueError, "exact recorded input kernel"):
                workloop.build(kernel, Path(directory) / "out")
            self.assertFalse((Path(directory) / "out").exists())


if __name__ == "__main__":
    unittest.main()
