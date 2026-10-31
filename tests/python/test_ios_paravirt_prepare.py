"""Restore preparation guards and real worker failures; no Apple firmware fixtures."""
import contextlib
import ctypes
import hashlib
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import prepare_ios_paravirt_restore as prepare
import ios_paravirt_seal as seal
from ios_paravirt_firmware import assemble_img4
from test_ios_paravirt_firmware import TICKET, im4p
from test_product import Isolated
from native_client import StudioError, dispatch, jobs


class PrepareTests(unittest.TestCase):
    def test_branches_decode_to_exact_target_and_reject_overflow_or_misalignment(self):
        for start, end in ((0x29c50, 0x2cda10), (0x2cda40, 0x1b04f0),
                           (0, (1 << 27) - 4), (1 << 27, 0)):
            instruction, = struct.unpack("<I", prepare.branch(start, end))
            immediate = instruction & 0x3ffffff
            if immediate & 0x2000000:
                immediate -= 1 << 26
            self.assertEqual(instruction >> 26, 0b100101)
            self.assertEqual(start + immediate * 4, end)
        for start, end in ((0, 3), (0, 1 << 27), ((1 << 27) + 4, 0)):
            with self.assertRaises(ValueError):
                prepare.branch(start, end)

    def test_unknown_daemon_cannot_be_patched(self):
        with self.assertRaisesRegex(ValueError, "unmodified PCC"):
            prepare.patch_binary(bytes(prepare.TEXT_END), bytes(512))

    def test_trustcache_preserves_every_existing_entry_and_adds_cdhash_once(self):
        entries = [bytes([value]) * 20 + bytes([2, value]) for value in (1, 2)]
        source = struct.pack("<I16sI", 1, bytes(16), 2) + b"".join(entries)
        cdhash = "03" * 20
        result = prepare.extend_trustcache(source, [cdhash, cdhash])
        version, _uuid, count = struct.unpack("<I16sI", result[:24])
        self.assertEqual((version, count, len(result)), (1, 3, 90))
        self.assertEqual({result[p:p + 22] for p in range(24, len(result), 22)},
                         {*entries, bytes.fromhex(cdhash) + b"\x02\x00"})
        for bad in (source[:-1], source + b"x", bytes(24),
                    struct.pack("<I16sI", 1, bytes(16), 2) + entries[0] * 2):
            with self.assertRaises(ValueError):
                prepare.extend_trustcache(bad, [cdhash])
        for hashes in ([], ["abc"], ["g" * 40]):
            with self.assertRaises(ValueError):
                prepare.extend_trustcache(source, hashes)

    def test_cli_failure_retains_private_receipt_and_never_overwrites_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            args = [part for name in ("ramdisk", "trustcache", "support", "helper-engine",
                                      "helper-kernel", "bios")
                    for part in ("--" + name, str(root / name))] + ["--output", str(output)]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(prepare.main(args), 1)
            receipt = output / "restore-runtime.json"
            before = receipt.read_bytes()
            value = json.loads(before)
            self.assertEqual(value["state"], "failed")
            self.assertEqual(value["phase"], "validate-inputs")
            self.assertFalse(value["qualified_for_app_development"])
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                prepare.main(args)
            self.assertEqual(receipt.read_bytes(), before)


class SealPolicyTests(unittest.TestCase):
    def test_pins_cover_complete_containers_and_select_only_original_payload(self):
        for name, tag in seal.COMPONENTS.items():
            payload = bytes(range(251)) * 3
            image = assemble_img4(im4p(tag, payload), TICKET)
            pin = seal.image_pin(name, image)
            self.assertEqual(pin["sha384"], hashlib.sha384(image).hexdigest())
            self.assertEqual(pin["bytes"], len(image))
            self.assertEqual(image[pin["payload_offset"]:pin["payload_offset"] + pin["payload_bytes"]], payload)
            for wrong in (b"invalid", image[:-1], image + b"x", assemble_img4(im4p(b"krnl", payload), TICKET)):
                with self.assertRaises(ValueError):
                    seal.image_pin(name, wrong)

    def test_unknown_build_is_rejected_before_reading_a_ticket_or_component(self):
        with patch.object(seal, "FirmwareBundle") as bundle:
            bundle.return_value.version = {"build_manifest_sha256": "0" * 64}
            with self.assertRaisesRegex(ValueError, "exact original"):
                seal.prepare_policy("unused", 0, "must-not-read", {})
            bundle.return_value.image.assert_not_called()
        with self.assertRaisesRegex(ValueError, "two sealing components"):
            seal.prepare_policy("unused", 0, "unused", {"KernelCache": "unused"})

    def test_compiled_adapter_checks_both_hashes_and_preserves_original_fallback(self):
        # Execute the actual adapter C with synthetic Image4 inputs and real
        # OpenSSL SHA-384. This is not firmware and does not modify a guest.
        images = [assemble_img4(im4p(tag, b"payload-" + tag), TICKET) for tag in seal.COMPONENTS.values()]
        pins = [seal.image_pin(name, data) for name, data in zip(seal.COMPONENTS, images)]
        values = []
        for pin in pins:
            values.append("{" + ",".join(str(pin[k]) for k in ("tag", "bytes", "payload_offset", "payload_bytes"))
                          + ",{" + ",".join(str(v) for v in bytes.fromhex(pin["sha384"])) + "}}")
        source = (prepare.INTEGRATION / "restore-seal-pins.c").read_text()
        source += """
#include <stdlib.h>
#include <openssl/sha.h>
struct data { const unsigned char *bytes; long length; };
const unsigned char *CFDataGetBytePtr(void *p) { return ((struct data *)p)->bytes; }
long CFDataGetLength(void *p) { return ((struct data *)p)->length; }
unsigned char *CC_SHA384(const void *p, unsigned int n, unsigned char *out) { return SHA384(p, n, out); }
int fail_allocation;
void *malloc_type_malloc(size_t n, unsigned long type) { return fail_allocation ? NULL : malloc(n); }
int original(void *data, void *manifest, unsigned int tag, unsigned long variant,
             void **payload, size_t *size, void **error) { return -7; }
"""
        source += "const struct seal_pin tomacco_seal_pins[2] = {" + ",".join(values) + "};\n"
        class Data(ctypes.Structure):
            _fields_ = [("bytes", ctypes.c_void_p), ("length", ctypes.c_long)]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "adapter.c").write_text(source)
            subprocess.run(["cc", "-shared", "-fPIC", "-O2", "-fno-builtin", root / "adapter.c", "-lcrypto",
                            "-o", root / "adapter.so"], check=True, capture_output=True, timeout=30)
            library = ctypes.CDLL(str(root / "adapter.so"))
            call = library.tomacco_local_seal
            call.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_ulong,
                             ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_ulong),
                             ctypes.POINTER(ctypes.c_void_p)]
            call.restype = ctypes.c_int
            library.free.argtypes = [ctypes.c_void_p]
            for pin, image in zip(pins, images):
                for kind in ("match", "changed", "short", "tag", "manifest", "variant", "allocation", "null"):
                    with self.subTest(component=pin["component"], kind=kind):
                        data = bytearray(image)
                        if kind == "changed":
                            data[pin["payload_offset"]] ^= 1
                        buffer = ctypes.create_string_buffer(bytes(data))
                        value = Data(ctypes.cast(buffer, ctypes.c_void_p), len(data) - (kind == "short"))
                        output, size, error = ctypes.c_void_p(), ctypes.c_ulong(999), ctypes.c_void_p(123)
                        ctypes.c_int.in_dll(library, "fail_allocation").value = kind == "allocation"
                        status = call(None if kind == "null" else ctypes.byref(value), 1 if kind == "manifest" else None,
                                      pin["tag"] ^ (kind == "tag"), int(kind == "variant"),
                                      ctypes.byref(output), ctypes.byref(size), ctypes.byref(error))
                        if kind == "match":
                            self.assertEqual(status, 1)
                            self.assertEqual(ctypes.string_at(output, size.value), b"payload-" + pin["tag"].to_bytes(4, "big"))
                            self.assertIsNone(error.value)
                            library.free(output)
                        else:
                            self.assertEqual(status, 0 if kind == "allocation" else -7)
                            self.assertIsNone(output.value)
                            self.assertEqual((size.value, error.value), (999, 123))


class PrepareJobTests(Isolated):
    def test_compiled_prepare_job_exposes_failure_and_preserves_inputs(self):
        arguments = {}
        for name in ("ramdisk", "trustcache", "helper_engine", "helper_kernel", "support", "bios"):
            path = self.root / name
            if name in ("support", "bios"):
                path.mkdir()
            else:
                path.write_bytes(b"invalid fixture, not firmware")
            arguments[name] = str(path)
        arguments["output"] = str(self.root / "prepared")
        job = dispatch("ios.emulator.prepare_restore", arguments)
        done = jobs.wait(job["id"], 10)
        self.assertEqual(done["state"], "failed", jobs.logs(job["id"]))
        report = dispatch("ios.emulator.report", {"job_id": job["id"]})
        self.assertEqual(report["report"]["state"], "failed")
        self.assertIn("Missing local preparation support", report["report"]["error"])
        self.assertFalse(report["report"]["authentication_checks_modified"])
        self.assertEqual(Path(arguments["ramdisk"]).read_bytes(), b"invalid fixture, not firmware")
        self.assertEqual(report["report_directory"], arguments["output"])
        self.assertTrue(any(Path(a["path"]).name == "restore-runtime.json"
                            for a in done["result"]["artifacts"]))
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.prepare_restore", arguments)
        self.assertEqual(error.exception.code, "output_exists")


if __name__ == "__main__":
    unittest.main()
