"""Adapter identity pinning in the research kernel's executable-mapping list."""
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from ios_paravirt_kernel import kernel_profile
import prepare_ios_adapter_pin as pin_tool
from prepare_ios_adapter_pin import (ADAPTER_SLOT, ADR_OFFSET, COUNT_OFFSET, ENTRIES, FIXED_ENTRIES, INPUT_SHA256,
                                     LIST_OFFSET, accepted_input, adapter_slot, build, pin)

OLD = "0699aab5f6cbc3120b0c921dd2f2ad5353f72ea7"  # an earlier adapter, as the reference kernel pins it
NEW = "1474774d2867bad583f620047e7e277975083e0d"  # a table-bearing adapter


def synthetic_kernel(adapter=OLD, *, entries=ENTRIES, target=LIST_OFFSET, fixed=FIXED_ENTRIES, padding=b""):
    """A kernel-sized buffer holding the mapping adapter's real list shape."""
    data = bytearray(b"\x5a" * LIST_OFFSET + bytes(40 * 20))
    offset = target - ADR_OFFSET
    struct.pack_into("<I", data, ADR_OFFSET, 0x1000000b | ((offset & 3) << 29) | ((offset >> 2) << 5))
    struct.pack_into("<I", data, COUNT_OFFSET, 0x52800000 | (entries << 5) | 15)
    for index, value in enumerate((*fixed, adapter)):
        data[LIST_OFFSET + index * 20:LIST_OFFSET + (index + 1) * 20] = bytes.fromhex(value)
    data[LIST_OFFSET + 80:LIST_OFFSET + 80 + len(padding)] = padding
    return bytes(data)


class AdapterPinTests(unittest.TestCase):
    def test_only_the_adapter_slot_changes(self):
        original = synthetic_kernel()
        candidate, offset, previous = pin(original, NEW)
        self.assertEqual((offset, previous), (LIST_OFFSET + ADAPTER_SLOT * 20, OLD))
        self.assertIn(bytes.fromhex(NEW), candidate)
        self.assertNotIn(bytes.fromhex(OLD), candidate)
        changed = [i for i in range(len(original)) if original[i] != candidate[i]]
        self.assertTrue(changed and min(changed) >= offset and max(changed) < offset + 20)
        self.assertEqual(adapter_slot(candidate), (offset, NEW))
        # The graphics entries stay where the cave's linear search finds them.
        for index, value in enumerate(FIXED_ENTRIES):
            self.assertEqual(candidate[LIST_OFFSET + index * 20:LIST_OFFSET + (index + 1) * 20].hex(), value)

    def test_any_other_layout_is_refused(self):
        for kernel, reason in (
                (synthetic_kernel(target=LIST_OFFSET + 4), "hash list at the recorded place"),
                (synthetic_kernel(entries=5), "exactly 4 hashes"),
                (synthetic_kernel(fixed=(FIXED_ENTRIES[0], FIXED_ENTRIES[1], "11" * 20)), "graphics entries"),
                (synthetic_kernel(adapter=FIXED_ENTRIES[0]), "exactly one adapter hash"),
                (synthetic_kernel(adapter="00" * 20), "exactly one adapter hash"),
                (synthetic_kernel(padding=b"\x01"), "recorded padding"),
                (synthetic_kernel()[:LIST_OFFSET + 40], "too short")):
            with self.assertRaisesRegex(ValueError, reason):
                pin(kernel, NEW)

    def test_the_new_hash_is_checked(self):
        for value, reason in (("XYZ", "40 lowercase hex"), (NEW.upper(), "40 lowercase hex"),
                              (FIXED_ENTRIES[2], "graphics entry"), (OLD, "already pins")):
            with self.assertRaisesRegex(ValueError, reason):
                pin(synthetic_kernel(), value)

    def test_a_build_pins_a_guest_tools_adapter_from_a_recorded_kernel(self):
        with tempfile.TemporaryDirectory(prefix="tomacco-adapter-pin-") as directory:
            root = Path(directory)
            source = root / "kernel-in" / "kernelcache.research.vphone600"
            source.parent.mkdir()
            source.write_bytes(synthetic_kernel())
            # Only the reference kernel itself, or a kernel this tool produced, is accepted.
            with self.assertRaisesRegex(ValueError, "exact recorded reference kernel"):
                accepted_input(source, source.read_bytes())
            (source.parent / "adapter-pin.json").write_text(json.dumps({
                "scope": "owned-adapter-identity-only", "origin_sha256": INPUT_SHA256,
                "output_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}))
            tools = root / "tools"
            adapter = tools / "Supplemental/usr/libexec/libtomacco-development-signing.dylib"
            adapter.parent.mkdir(parents=True)
            adapter.write_bytes(b"adapter bytes")
            (tools / "guest-tools.json").write_text(json.dumps({
                "state": "built", "development_signing": {}, "programs": [
                    {"path": "usr/libexec/libtomacco-development-signing.dylib", "architecture": "arm64e",
                     "cdhash": NEW, "sha256": hashlib.sha256(b"adapter bytes").hexdigest()}]}))
            receipt = build(source, tools, root / "kernel-out")
            output = (root / "kernel-out/kernelcache.research.vphone600").read_bytes()
            self.assertEqual((receipt["previous_cdhash"], receipt["cdhash"], receipt["scope"]),
                             (OLD, NEW, "owned-adapter-identity-only"))
            self.assertEqual(receipt["output_sha256"], hashlib.sha256(output).hexdigest())
            self.assertFalse(receipt["guest_execution_verified"])
            self.assertIn(bytes.fromhex(NEW), output)
            # A pinned output can be re-pinned later; an existing output is never overwritten.
            self.assertEqual(accepted_input(root / "kernel-out/kernelcache.research.vphone600", output),
                             hashlib.sha256(output).hexdigest())
            with self.assertRaisesRegex(ValueError, "Output already exists"):
                build(source, tools, root / "kernel-out")
            # A build whose recorded adapter changed, or that has none, is refused.
            adapter.write_bytes(b"other bytes")
            with self.assertRaisesRegex(ValueError, "changed after its build"):
                build(source, tools, root / "kernel-other")
            (tools / "guest-tools.json").write_text(json.dumps({"state": "built", "programs": []}))
            with self.assertRaisesRegex(ValueError, "no development-signing adapter"):
                build(source, tools, root / "kernel-other")


    def test_a_users_own_kernel_is_accepted_by_its_profile_with_its_own_graphics_entries(self):
        own = ("11" * 20, "22" * 20, "33" * 20)
        with tempfile.TemporaryDirectory(prefix="tomacco-adapter-pin-") as directory:
            root = Path(directory)
            source = root / "kernel-in" / "kernelcache.research.vphone600"
            source.parent.mkdir()
            source.write_bytes(synthetic_kernel(fixed=own))
            tools = root / "tools"
            adapter = tools / "Supplemental/usr/libexec/libtomacco-development-signing.dylib"
            adapter.parent.mkdir(parents=True)
            adapter.write_bytes(b"adapter bytes")
            (tools / "guest-tools.json").write_text(json.dumps({
                "state": "built", "development_signing": {}, "programs": [
                    {"path": "usr/libexec/libtomacco-development-signing.dylib", "architecture": "arm64e",
                     "cdhash": NEW, "sha256": hashlib.sha256(b"adapter bytes").hexdigest()}]}))
            # Its graphics entries are its own builds', which the recorded list would refuse.
            with self.assertRaisesRegex(ValueError, "graphics entries"):
                pin(source.read_bytes(), NEW)
            with self.assertRaisesRegex(ValueError, "exact recorded reference kernel"):
                build(source, tools, root / "refused")
            with mock.patch.object(pin_tool, "INPUT_PROFILE_SHA256", kernel_profile(source.read_bytes())):
                receipt = build(source, tools, root / "kernel-out")
                output = (root / "kernel-out/kernelcache.research.vphone600").read_bytes()
                self.assertEqual((receipt["previous_cdhash"], receipt["cdhash"]), (OLD, NEW))
                self.assertEqual(receipt["origin_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
                self.assertEqual(adapter_slot(output, None), (LIST_OFFSET + ADAPTER_SLOT * 20, NEW))
                for index, value in enumerate(own):
                    self.assertEqual(output[LIST_OFFSET + index * 20:LIST_OFFSET + (index + 1) * 20].hex(), value)
                # The same list entry twice is still refused.
                with self.assertRaisesRegex(ValueError, "exactly one adapter hash"):
                    pin(source.read_bytes(), own[0], None)


if __name__ == "__main__":
    unittest.main()
