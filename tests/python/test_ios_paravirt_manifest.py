"""Synthetic IM4M metadata; these fixtures have no valid signatures."""
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from ios_paravirt_firmware import der_wrap
import ios_paravirt_manifest as manifest


def private_object(key, tag, payload):
    number = int.from_bytes(key.encode(), "big")
    encoded = bytes([number & 127])
    number >>= 7
    while number:
        encoded = bytes([128 | (number & 127)]) + encoded
        number >>= 7
    value = der_wrap(0x30, der_wrap(0x16, key.encode()) + der_wrap(tag, payload))
    return b"\xff" + encoded + der_wrap(0x30, value)[1:]


def ticket_for_image(image, *, chip=0xfe01, board=0x91, ecid=17, digest=None):
    props = []
    for name, value in (("CHIP", chip), ("BORD", board), ("ECID", ecid)):
        raw = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
        if raw[0] & 128:
            raw = b"\0" + raw
        props.append(private_object(name, 2, raw))
    body = private_object("MANP", 0x31, b"".join(sorted(props)))
    body += private_object("rosi", 0x31, private_object("DGST", 4,
                hashlib.sha384(image).digest() if digest is None else digest))
    return der_wrap(0x30, der_wrap(0x16, b"IM4M") + der_wrap(2, b"\0") +
                    der_wrap(0x31, private_object("MANB", 0x31, body)) +
                    der_wrap(4, b"unsigned-fixture") + der_wrap(0x30, der_wrap(4, b"not-a-certificate")))


class ManifestTests(unittest.TestCase):
    def test_declared_digest_and_hardware_do_not_authenticate_a_ticket(self):
        image = b"test image"
        ticket = ticket_for_image(image)
        info = manifest.check_build(manifest.inspect_manifest(ticket), hashlib.sha384(image).digest())
        checked = manifest.check_device(info, {"ChipID": 0xfe01, "BoardID": 0x91, "UniqueChipID": 17})
        self.assertTrue(checked["build_metadata_matched"])
        self.assertTrue(checked["guest_metadata_matched"])
        self.assertFalse(checked["signature_verified"])
        self.assertFalse(checked["device_binding_verified"])
        self.assertEqual(ticket, ticket_for_image(image))

    def test_rejects_different_os_chip_board_and_ecid(self):
        for options, message in (({"digest": bytes(48)}, "OS digest"),
                                 ({"chip": 0x8030}, "hardware"), ({"board": 4}, "hardware")):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, message):
                manifest.check_build(manifest.inspect_manifest(ticket_for_image(b"x", **options)),
                                     hashlib.sha384(b"x").digest())
        info = manifest.inspect_manifest(ticket_for_image(b"x"))
        for wrong in (18, None, True):
            with self.subTest(ecid=wrong), self.assertRaisesRegex(ValueError, "ECID"):
                manifest.check_device(info, {"ChipID": 0xfe01, "BoardID": 0x91, "UniqueChipID": wrong})

    def test_rejects_noncanonical_tags_duplicate_objects_and_wrong_keys(self):
        good = private_object("DGST", 4, bytes(48))
        bad = [b"\xff", b"\xff\x80\x01\x00", b"\xff" + b"\x81" * 6,
               good.replace(b"DGST", b"ABCD"), good + good, good[:-1], b"\x30\0"]
        for value in bad:
            with self.subTest(value=value[:20]), self.assertRaises(ValueError):
                manifest.object_set(value)

    def test_rejects_missing_metadata_and_wrong_digest_width(self):
        ticket = ticket_for_image(b"x")
        for value in (ticket.replace(b"rosi", b"roxx"), ticket.replace(b"MANB", b"MANX"),
                      ticket_for_image(b"x", digest=bytes(32)), ticket + b"trailing"):
            with self.subTest(value=value[:30]), self.assertRaises(ValueError):
                manifest.inspect_manifest(value)

    def test_manifest_integer_bounds_and_canonical_encoding(self):
        for value in (b"", b"\x80", b"\0\x01", bytes(10), b"\x01" + bytes(8)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                manifest.integer(value)
        self.assertEqual(manifest.integer(b"\0\x80"), 128)
        self.assertEqual(manifest.integer(b"\0" + b"\xff" * 8), 0xffffffffffffffff)


if __name__ == "__main__":
    unittest.main()
