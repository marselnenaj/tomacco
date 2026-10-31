"""NVRAM configuration delivery, without firmware or a fabricated guest boot."""
import hashlib
import json
import struct
import unittest
import zlib

from test_ios_paravirt import prep, source_tree
from ios_paravirt_pcc import hostname_config, pcc_config_handoff, pcc_guest_evidence


class PccConfigurationTests(unittest.TestCase):
    def tree(self, common=b"", system=b"", *, version=1):
        tree = prep.decode(prep.prepare(source_tree().encode()))
        image = bytearray(prep.blank_nvram())
        if version == 2:
            for offset, size, name in ((0, 32, b"2nvram"), (32, 2048, b"2common"),
                                      (2080, len(image) - 2080, b"2system")):
                image[offset:offset + 16] = prep.chrp_header(0x5a if offset == 0 else 0x70, size, name)
        image[48:48 + len(common)] = common
        image[2096:2096 + len(system)] = system
        struct.pack_into("<I", image, 16, zlib.adler32(image[20:]))
        tree.child("chosen").set("nvram-proxy-data", bytes(image))
        return tree

    def test_v1_and_v2_preserve_binary_variables_and_make_only_a_hostname_config(self):
        for version in (1, 2):
            with self.subTest(version=version):
                common = b"boot-args=-v\0binary=\xff\x83bytes\0"
                system = b"security-mode=unchanged\0"
                tree = self.tree(common, system, version=version)
                original = tree.child("chosen").get("nvram-proxy-data")
                report = pcc_config_handoff(tree, "tomacco")
                data = tree.child("chosen").get("nvram-proxy-data")
                prep.validate_nvram(data)
                self.assertEqual(data[48:48 + len(common)], common)
                self.assertEqual(data[2080:], original[2080:])
                self.assertEqual(data[:16], original[:16])
                self.assertEqual(data[20:48], original[20:48])
                config = data[48 + len(common):].split(b"\0", 1)[0].removeprefix(b"darwin-init=")
                self.assertEqual(json.loads(config), {"local-host-name": "tomacco"})
                self.assertFalse(report["host_wrote_storage"])
                self.assertFalse(report["security_controls_modified"])
                self.assertEqual(report["bank_sha256_before"], hashlib.sha256(original).hexdigest())
                self.assertEqual(report["bank_sha256_after"], hashlib.sha256(data).hexdigest())
                repeated = pcc_config_handoff(tree, "tomacco")
                self.assertFalse(repeated["changed"])
                self.assertEqual(tree.child("chosen").get("nvram-proxy-data"), data)

    def test_existing_or_ambiguous_configuration_is_never_overwritten(self):
        entries = ((b'darwin-init={"local-host-name":"existing"}\0', b""),
                   (b"darwin-init-system={}\0", b""),
                   (b"", b"darwin-init={}\0"),
                   (b'a=first\0a=second\0', b""),
                   (b"broken\0", b""),
                   (b"a=x\0\0nonzero-padding", b""),
                   (b"x=" + b"a" * 2028 + b"\0", b""))
        for common, system in entries:
            with self.subTest(common=common[:50], system=system):
                tree = self.tree(common, system)
                before = tree.encode()
                with self.assertRaises(ValueError):
                    pcc_config_handoff(tree, "tomacco")
                self.assertEqual(tree.encode(), before)

    def test_invalid_hostname_and_damaged_bank_fail_before_changes(self):
        for name in ("", "A", "a.b", "-a", "a-", "x" * 64, "a\n", "a\0", None, 42):
            with self.subTest(name=name), self.assertRaises(ValueError):
                hostname_config(name)
        self.assertEqual(hostname_config("a"), {"local-host-name": "a"})
        self.assertEqual(hostname_config("x" * 63), {"local-host-name": "x" * 63})
        tree = self.tree()
        damaged = bytearray(tree.child("chosen").get("nvram-proxy-data"))
        damaged[100] ^= 1
        tree.child("chosen").set("nvram-proxy-data", bytes(damaged))
        before = tree.encode()
        with self.assertRaisesRegex(ValueError, "checksum"):
            pcc_config_handoff(tree, "tomacco")
        self.assertEqual(tree.encode(), before)

    def test_receipt_distinguishes_loading_applying_and_failure(self):
        prefix = "2026-10-06 18:06:10+0000 Default darwin-init[46:1e4][com.apple.darwin-init:] "
        loaded = prefix + "Successfully loaded configuration from darwin-init NVRAM variable\r\r\n"
        requested = prefix + 'Applying configuration: {"identification":{"local_hostname":"requested"}}\n'
        applied = prefix + 'Applied configuration: {\r\r\n"identification": {"local_hostname":"tomacco"}\r\n}\r\n'
        self.assertEqual(pcc_guest_evidence(loaded + requested)["darwin_init_applied_local_hostnames"], [])
        evidence = pcc_guest_evidence(loaded + requested + applied + applied)
        self.assertEqual(evidence["darwin_init_config_sources"], ["darwin-init"])
        self.assertEqual(evidence["darwin_init_applied_local_hostnames"], ["tomacco"])
        self.assertEqual(evidence["darwin_init_apply_failures"], [])
        failed = pcc_guest_evidence(loaded + prefix + "darwin-init apply failed: example failure\n")
        self.assertEqual(failed["darwin_init_apply_failures"], ["example failure"])
        self.assertEqual(failed["darwin_init_applied_local_hostnames"], [])
        for data in ("[]", "null", '{"identification":42}', '{"identification":{"local_hostname":42}}', '{"truncated":'):
            self.assertEqual(pcc_guest_evidence(prefix + "Applied configuration: " + data)["darwin_init_applied_local_hostnames"], [])


if __name__ == "__main__":
    unittest.main()
