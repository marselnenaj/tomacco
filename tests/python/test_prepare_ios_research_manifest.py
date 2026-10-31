"""Research manifests are reproducible metadata, never valid Apple signatures."""
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from ios_paravirt_firmware import der_fields
from ios_paravirt_manifest import inspect_manifest, object_set, required
import prepare_ios_research_manifest as research
from test_ios_paravirt_manifest import ticket_for_image


class ResearchManifestTests(unittest.TestCase):
    def components(self, prefix):
        return {name: {"Info": {}, "Digest": hashlib.sha384((prefix + name).encode()).digest()}
                for name in research.OS_COMPONENTS}

    def test_combines_pcc_boot_metadata_with_iphone_os_and_vm_identity(self):
        pcc, phone = self.components("pcc"), self.components("phone")
        pcc["RestoreSEP"] = {"Info": {"Img4PayloadType": "rsep"},
                             "Digest": bytes(range(48)), "Trusted": True}
        original = ticket_for_image(b"template")
        result = research.rebuild(original, pcc, phone, 0xfedcba9876543210)
        info = inspect_manifest(result)
        self.assertEqual(info["declared_hardware"], {"CHIP": 0xfe01, "BORD": 0x91,
                                                   "ECID": 0xfedcba9876543210})
        self.assertEqual(info["os_sha384"], phone["OS"]["Digest"].hex())
        self.assertFalse(info["signature_verified"])
        fields = der_fields(result)
        body = object_set(required(object_set(result[fields[2][1]:fields[2][2]]), "MANB", 0x31))
        sep = object_set(required(body, "rsep", 0x31))
        self.assertEqual(required(sep, "DGST", 4), pcc["RestoreSEP"]["Digest"])
        self.assertEqual(required(sep, "EKEY", 1), b"\xff")
        for name in research.OS_COMPONENTS:
            entry = object_set(required(body, research.TAGS[name], 0x31))
            self.assertEqual(required(entry, "DGST", 4), phone[name]["Digest"])
        old_fields = der_fields(original)
        for i in (3, 4):
            self.assertEqual(result[fields[i][1]:fields[i][2]],
                             original[old_fields[i][1]:old_fields[i][2]])
        self.assertEqual(result, research.rebuild(original, pcc, phone, 0xfedcba9876543210))
        self.assertNotEqual(result, research.rebuild(original, pcc, phone, 17))

    def test_refuses_invalid_identity_and_malformed_inputs(self):
        pcc, phone = self.components("pcc"), self.components("phone")
        for ecid in (-1, True, 1 << 64):
            with self.subTest(ecid=ecid), self.assertRaises(ValueError):
                research.rebuild(ticket_for_image(b"x"), pcc, phone, ecid)
        with self.assertRaises(ValueError):
            research.rebuild(b"not a manifest", pcc, phone, 17)
        phone["OS"]["Digest"] = bytes(32)
        with self.assertRaises(ValueError):
            research.rebuild(ticket_for_image(b"x"), pcc, phone, 17)


if __name__ == "__main__":
    unittest.main()
