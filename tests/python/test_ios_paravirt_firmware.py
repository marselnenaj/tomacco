"""Offline Image4/NOR contracts using synthetic, unsigned firmware fixtures."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import ios_paravirt_firmware as firmware


def im4p(tag=b"sepi", payload=b"synthetic firmware", extra=b""):
    wrap = firmware.der_wrap
    return wrap(0x30, wrap(0x16, b"IM4P") + wrap(0x16, tag) +
                wrap(0x16, b"test-only") + wrap(4, payload) + extra)


TICKET = b"\x30\x13\x16\x04IM4M\x02\x01\x00\x31\x02xx\x04\x01s\x30\x01c"


class Image4Tests(unittest.TestCase):
    def test_retag_and_wrap_preserve_payload_extensions_and_manifest(self):
        # Optional fields are opaque and must survive an rsep role change.
        source = im4p(extra=firmware.der_wrap(0x30, b"optional data"))
        changed, old, new = firmware.retag_im4p(source, "rsep")
        self.assertEqual((old, new), ("sepi", "rsep"))
        self.assertEqual(changed, source.replace(b"sepi", b"rsep", 1))
        image = firmware.assemble_img4(changed, TICKET)
        fields = firmware.der_fields(image)
        self.assertEqual([x[0] for x in fields], [0x16, 0x30, 0xa0])
        self.assertEqual(image[fields[0][1]:fields[0][2]], b"IMG4")
        self.assertEqual(image[fields[2][1]:fields[2][2]], TICKET)
        self.assertEqual(image[fields[0][2]:fields[1][2]], changed)

    def test_der_canonical_lengths_and_bounds(self):
        for size in (0, 1, 127, 128, 255, 256, 65536):
            value = b"x" * size
            encoded = firmware.der_wrap(4, value)
            tag, start, end = firmware.der_item(encoded)
            self.assertEqual((tag, encoded[start:end], end), (4, value, len(encoded)))
        for bad in (b"", b"\x30", b"\x30\x80", b"\x30\x81\x01x",
                    b"\x30\x82\x00\x80", b"\x30\x85\x01\x00\x00\x00\x00",
                    b"\x30\x03xx", b"\x3f\x00", b"\x30\x02\x04\x01",
                    b"\x30\x00trailing", b"\x31\x00",
                    firmware.der_wrap(0x30, b"\x04\x00" * 17)):
            with self.subTest(bad=bad[:20]), self.assertRaises(ValueError):
                firmware.der_fields(bad)

    def test_invalid_component_and_ticket_envelopes(self):
        for bad in (im4p(tag=b"abc"), im4p(payload=b""), im4p()[:-1],
                    im4p().replace(b"IM4P", b"IMG4"), im4p() + b"x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                firmware.assemble_img4(bad, TICKET)
        for tag in ("abc", "abcde", "ab\x00d", "äbcd", 7, b"rsep"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                firmware.retag_im4p(im4p(), tag)
        for bad in (TICKET[:-1], TICKET + b"x", TICKET.replace(b"IM4M", b"IM4P"),
                    TICKET.replace(b"\x02\x01\x00", b"\x02\x01\x01")):
            with self.subTest(ticket=bad), self.assertRaises(ValueError):
                firmware.assemble_img4(im4p(), bad)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.components = {}
        for name, tag, flags in (("LLB", b"illb", {"IsFirmwarePayload": True}),
                                 ("DeviceTree", b"dtre", {"IsFirmwarePayload": True}),
                                 ("iBoot", b"ibot", {"IsFirmwarePayload": True}),
                                 ("SEP", b"sepi", {"IsFirmwarePayload": True}),
                                 ("RestoreSEP", b"sepi", {}),
                                 ("LoadedAux", b"aux1", {"IsSecondaryFirmwarePayload": True, "IsLoadedByiBoot": True}),
                                 ("UnloadedAux", b"aux2", {"IsSecondaryFirmwarePayload": True})):
            data = im4p(tag)
            (self.root / (name + ".im4p")).write_bytes(data)
            info = {"Path": name + ".im4p", **flags}
            if name == "RestoreSEP":
                info["Img4PayloadType"] = "rsep"
                data = firmware.retag_im4p(data, "rsep")[0]
            self.components[name] = {"Info": info, "Digest": hashlib.sha384(data).digest()}
        self.manifest = {"ProductVersion": "test", "ProductBuildVersion": "unsigned-fixture",
                         "BuildIdentities": [{"ApChipID": "0xFE01", "ApBoardID": "0x91",
                             "Info": {"DeviceClass": "vphone600ap", "Variant": "test"},
                             "Manifest": self.components}]}
        self.save()
        (self.root / "ticket.im4m").write_bytes(TICKET)

    def save(self):
        (self.root / "BuildManifest.plist").write_bytes(plistlib.dumps(self.manifest, sort_keys=False))

    def bundle(self, **kwargs):
        return firmware.FirmwareBundle(self.root, 0, **kwargs)

    def test_digest_is_checked_after_role_tag_change(self):
        data, report = self.bundle().image("RestoreSEP", TICKET)
        self.assertIn(b"rsep", data)
        self.assertEqual(report["original_payload_type"], "sepi")
        self.assertTrue(report["manifest_digest_matched"])
        self.assertFalse(report["signature_verified"])
        self.assertFalse(report["device_binding_verified"])
        self.assertEqual(report["image_sha256"], hashlib.sha256(data).hexdigest())
        self.components["RestoreSEP"]["Digest"] = bytes(48)
        self.save()
        with self.assertRaisesRegex(ValueError, "digest"):
            self.bundle().image("RestoreSEP", TICKET)

    def test_build_identity_response_keeps_original_metadata_and_variant(self):
        bundle = self.bundle(overrides={"LLB": str(self.root / "LLB.im4p")})
        result = bundle.build_identity_request({"BuildIdentityApplyOverrides": True, "IsRecoveryOS": False})
        self.assertEqual(result, {"BuildIdentityDict": self.manifest["BuildIdentities"][0], "Variant": "Erase"})
        result["BuildIdentityDict"]["Manifest"]["LLB"]["Info"]["Path"] = "changed"
        self.assertEqual(bundle.identity["Manifest"]["LLB"]["Info"]["Path"], "LLB.im4p")
        self.assertEqual(bundle.build_identity_request({"Variant": "Update"})["Variant"], "Update")
        for invalid in ({"IsRecoveryOS": True}, {"IsRecoveryOS": 0}, {"Variant": 3},
                        {"BuildIdentityApplyOverrides": 1}, {"Variant": ""}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                bundle.build_identity_request(invalid)

    def test_nor_contains_all_required_components_and_real_bytes(self):
        bundle = self.bundle()
        value, records = bundle.nor(TICKET)
        self.assertEqual(set(value), {"LlbImageData", "NorImageData", "RestoreSEPImageData", "SEPImageData"})
        self.assertEqual(list(value["NorImageData"]), ["DeviceTree", "iBoot", "SEP", "LoadedAux"])
        self.assertEqual(value["RestoreSEPImageData"], bundle.image("RestoreSEP", TICKET)[0])
        self.assertEqual(value["SEPImageData"], value["NorImageData"]["SEP"])
        self.assertEqual(len(records), 6)
        legacy, _ = bundle.nor(TICKET, False)
        self.assertEqual(legacy["NorImageData"][0], bundle.image("iBoot", TICKET)[0])
        self.assertEqual(set(legacy["NorImageData"]), set(value["NorImageData"].values()))
        self.assertEqual(plistlib.loads(plistlib.dumps(value, fmt=plistlib.FMT_BINARY)), value)

    def test_incomplete_nor_fails_and_cannot_publish_success(self):
        (self.root / "LLB.im4p").unlink()
        output = self.root / "pack"
        with self.assertRaises(FileNotFoundError):
            firmware.main(self.cli(output) + ["--nor"])
        self.assertFalse(output.exists())
        del self.components["iBoot"]
        self.save()
        with self.assertRaisesRegex(ValueError, "complete NOR"):
            self.bundle().nor(TICKET)

    def test_override_remains_subject_to_identity_digest(self):
        moved = self.root / "alternate.im4p"
        (self.root / "SEP.im4p").rename(moved)
        bundle = self.bundle(overrides={"SEP": moved})
        self.assertIn(b"IMG4", bundle.image("SEP", TICKET)[0])
        moved.write_bytes(im4p(payload=b"wrong firmware"))
        with self.assertRaisesRegex(ValueError, "digest"):
            bundle.image("SEP", TICKET)
        with self.assertRaisesRegex(ValueError, "absent"):
            self.bundle(overrides={"unknown": moved})

    def test_personalized_image_list_and_data_follow_actual_manifest_flags(self):
        bundle = self.bundle()
        arguments = {"ImageType": "IsSecondaryFirmwarePayload", "ImageList": True}
        response, records = bundle.image_request(arguments, TICKET)
        self.assertEqual(response, {"ImageList": ["LoadedAux", "UnloadedAux"]})
        self.assertEqual(records, [])
        response, records = bundle.image_request({**arguments, "ImageList": False}, TICKET)
        self.assertEqual(set(response["ImageData"]), {"LoadedAux", "UnloadedAux"})
        single, _ = bundle.image_request({**arguments, "ImageList": False, "ImageName": "LoadedAux"}, TICKET)
        self.assertEqual(single, {"ImageName": "LoadedAux", "ImageData": response["ImageData"]["LoadedAux"]})
        self.assertEqual(len(records), 2)
        with patch.object(firmware, "NOR_LIMIT", 1), self.assertRaisesRegex(ValueError, "aggregate"):
            bundle.image_request({**arguments, "ImageList": False}, TICKET)
        (self.root / "LoadedAux.im4p").unlink()
        with self.assertRaises(FileNotFoundError):
            bundle.image_request({**arguments, "ImageList": False}, TICKET)

    def test_unknown_image_type_cannot_be_confused_with_known_empty_list(self):
        self.components["iBoot"]["Info"]["IsiBootEANFirmware"] = False
        self.save()
        bundle = self.bundle()
        arguments = {"ImageType": "IsiBootEANFirmware", "ImageList": True}
        self.assertEqual(bundle.image_request(arguments, TICKET), ({"ImageList": []}, []))
        for wrong in ({}, {**arguments, "ImageType": "Unknown"}, {**arguments, "ImageList": 1},
                      {**arguments, "ImageName": "iBoot"},
                      {"ImageType": "IsFirmwarePayload", "ImageName": "missing"}):
            with self.subTest(arguments=wrong), self.assertRaises(ValueError):
                bundle.image_request(wrong, TICKET)
        self.components["iBoot"]["Info"]["IsiBootEANFirmware"] = 1
        self.save()
        with self.assertRaisesRegex(ValueError, "boolean property"):
            self.bundle().image_request(arguments, TICKET)

    def test_image_request_cli_writes_a_real_response_and_preserves_input(self):
        request = self.root / "request.plist"
        arguments = {"ImageType": "IsFirmwarePayload", "ImageName": "iBoot"}
        original = plistlib.dumps(arguments)
        request.write_bytes(original)
        output = self.root / "image-response"
        with contextlib.redirect_stdout(io.StringIO()):
            firmware.main(self.cli(output) + ["--image-request", str(request)])
        response = plistlib.loads((output / "image-response.plist").read_bytes())
        self.assertEqual(response["ImageName"], "iBoot")
        self.assertEqual(response["ImageData"], self.bundle().image("iBoot", TICKET)[0])
        self.assertEqual(request.read_bytes(), original)
        receipt = json.loads((output / "firmware.json").read_text())
        self.assertEqual(receipt["data_type"], "PersonalizedData")
        self.assertFalse(receipt["nor_response_complete"])
        self.assertEqual(receipt["image_request"], {"arguments": arguments,
                         "sha256": hashlib.sha256(original).hexdigest()})
        self.assertEqual(receipt["files"]["image-response.plist"]["sha256"],
                         hashlib.sha256((output / "image-response.plist").read_bytes()).hexdigest())

    def test_fixed_ean_and_fud_contracts_select_only_their_manifest_properties(self):
        self.components["iBoot"]["Info"]["IsFUDFirmware"] = True
        self.save()
        bundle = self.bundle()
        self.assertEqual(bundle.image_request({"EANImageList": True}, TICKET, "EANData"),
                         ({"EANImageList": []}, []))
        self.assertEqual(bundle.image_request({"FUDImageList": True}, TICKET, "FUDData"),
                         ({"FUDImageList": ["iBoot"]}, []))
        data, _ = bundle.image_request({"ImageName": "iBoot"}, TICKET, "FUDData")
        self.assertEqual(data, {"ImageName": "iBoot", "FUDImageData": bundle.image("iBoot", TICKET)[0]})
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            bundle.image_request({}, TICKET, "UnknownData")

    def test_boot_object_chunks_reassemble_exactly_and_end_once(self):
        bundle = self.bundle()
        messages, record = bundle.boot_object_request(
            {"ImageName": "RestoreSEP", "DataChunkSize": 17, "IsRecoveryOS": False}, TICKET)
        messages = list(messages)
        self.assertEqual(messages[-1], {"FileDataDone": True})
        self.assertTrue(all(set(m) == {"FileData"} and 0 < len(m["FileData"]) <= 17
                            for m in messages[:-1]))
        self.assertEqual(b"".join(m["FileData"] for m in messages[:-1]), bundle.image("RestoreSEP", TICKET)[0])
        self.assertEqual(record["data_messages"], len(messages) - 1)
        self.assertFalse(record["signature_verified"])

    def test_boot_object_rejects_wrong_profile_and_invalid_data_before_streaming(self):
        bundle = self.bundle()
        for wrong in (None, {}, {"ImageName": []}, {"ImageName": "__GlobalManifest__"},
                      {"ImageName": "SEP", "IsRecoveryOS": True},
                      {"ImageName": "SEP", "IsRecoveryOS": 1},
                      *[{"ImageName": "SEP", "DataChunkSize": value} for value in
                        (0, -1, True, "8192", firmware.FILE_CHUNK_LIMIT + 1)]):
            with self.subTest(arguments=wrong), self.assertRaises(ValueError):
                bundle.boot_object_request(wrong, TICKET)
        with patch.object(firmware, "FILE_MESSAGE_LIMIT", 2), self.assertRaisesRegex(ValueError, "count limit"):
            bundle.boot_object_request({"ImageName": "SEP", "DataChunkSize": 1}, TICKET)
        (self.root / "SEP.im4p").write_bytes(im4p(payload=b"wrong firmware"))
        with self.assertRaisesRegex(ValueError, "digest"):
            bundle.boot_object_request({"ImageName": "SEP"}, TICKET)

    def test_source_boot_objects_preserve_original_bytes_including_role_tag(self):
        for protocol in ("SourceBootObjectV4", "SourceBootObjectV5"):
            messages, record = self.bundle().boot_object_request(
                {"ImageName": "RestoreSEP", "DataChunkSize": 13}, TICKET, protocol)
            messages = list(messages)
            self.assertEqual(messages[-1], {"FileDataDone": True})
            self.assertEqual(b"".join(m["FileData"] for m in messages[:-1]),
                             (self.root / "RestoreSEP.im4p").read_bytes())
            self.assertTrue(record["manifest_digest_matched"])
            self.assertEqual(record["original_payload_type"], "sepi")
            self.assertEqual(record["payload_type"], "rsep")

    def test_metadata_reads_local_files_and_optional_absence_without_synthesis(self):
        version = plistlib.dumps({"ProductBuildVersion": "unsigned-fixture"})
        (self.root / "SystemVersion.plist").write_bytes(version)
        messages, record = self.bundle().boot_object_request({"ImageName": "__SystemVersion__"}, TICKET)
        self.assertEqual(list(messages), [{"FileData": version}, {"FileDataDone": True}])
        self.assertFalse(record["signature_verified"])
        args = {"ImageName": "__GlobalManifest__", "Variant": "Erase", "GlobalManifestOptional": True}
        messages, record = self.bundle().boot_object_request(args, TICKET, "SourceBootObjectV4")
        self.assertEqual(list(messages), [{"FileDataDone": True}])
        self.assertTrue(record["optional_absent"])
        info = self.manifest["BuildIdentities"][0]["Info"]
        info.update(VariantSupportsGlobalSigning=True, MacOSVariant="Erase")
        self.save()
        with self.assertRaises(FileNotFoundError):
            self.bundle().boot_object_request({**args, "GlobalManifestOptional": False}, TICKET, "SourceBootObjectV4")
        directory = self.root / "Firmware/Manifests/restore/Erase"
        directory.mkdir(parents=True)
        (directory / "apticket.vphone600ap.im4m").write_bytes(TICKET)
        for protocol in ("SourceBootObjectV4", "PersonalizedBootObjectV3"):
            messages, record = self.bundle().boot_object_request(args, TICKET, protocol)
            self.assertEqual(list(messages), [{"FileData": TICKET}, {"FileDataDone": True}])
            self.assertFalse(record["optional_absent"])
        for key, value in (("Variant", "../outside"), ("GlobalManifestPrefix", "/tmp/x"),
                           ("GlobalManifestSuffix", "/other"), ("GlobalManifestOptional", 1)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.bundle().boot_object_request({**args, key: value}, TICKET, "SourceBootObjectV4")

    def test_boot_object_cli_produces_framed_plists_and_audited_receipt(self):
        request = self.root / "boot-object-request.plist"
        request.write_bytes(plistlib.dumps({"ImageName": "iBoot", "DataChunkSize": 23}))
        output = self.root / "boot-object"
        with contextlib.redirect_stdout(io.StringIO()):
            firmware.main(self.cli(output) + ["--boot-object-request", str(request)])
        path = output / "boot-object.plist-stream"
        wire, messages, offset = path.read_bytes(), [], 0
        while offset < len(wire):
            size, = struct.unpack_from("!I", wire, offset)
            self.assertLessEqual(offset + 4 + size, len(wire))
            messages.append(plistlib.loads(wire[offset + 4:offset + 4 + size]))
            offset += 4 + size
        self.assertEqual(messages[-1], {"FileDataDone": True})
        self.assertEqual(b"".join(m["FileData"] for m in messages[:-1]), self.bundle().image("iBoot", TICKET)[0])
        receipt = json.loads((output / "firmware.json").read_text())
        self.assertEqual(receipt["data_type"], "PersonalizedBootObjectV3")
        self.assertEqual(receipt["files"][path.name], {"bytes": len(wire), "sha256": hashlib.sha256(wire).hexdigest()})
        self.assertEqual(receipt["image_request"]["arguments"]["DataChunkSize"], 23)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_explicit_identity_is_required_and_board_must_match(self):
        for index in (-1, 1, True, "0"):
            with self.subTest(index=index), self.assertRaises(ValueError):
                firmware.FirmwareBundle(self.root, index)
        self.manifest["BuildIdentities"][0]["ApChipID"] = "0x8120"
        self.save()
        with self.assertRaisesRegex(ValueError, "vphone600"):
            self.bundle()
        self.manifest["BuildIdentities"] = [False]
        self.save()
        with self.assertRaisesRegex(ValueError, "identity"):
            self.bundle()

    def test_aggregate_limit_and_flash_version_are_enforced(self):
        with patch.object(firmware, "NOR_LIMIT", 64), self.assertRaisesRegex(ValueError, "aggregate"):
            self.bundle().nor(TICKET)
        for value in (1, "1", None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "boolean"):
                self.bundle().nor(TICKET, value)

    def cli(self, output):
        return ["--firmware", str(self.root), "--identity", "0", "--manifest",
                str(self.root / "ticket.im4m"), "--output", str(output)]

    def test_cli_private_complete_output_and_no_overwrite(self):
        output = self.root / "pack"
        with contextlib.redirect_stdout(io.StringIO()):
            firmware.main(self.cli(output) + ["--nor"])
        report = json.loads((output / "firmware.json").read_text())
        self.assertTrue(report["nor_response_complete"])
        self.assertFalse(report["restore_completed"])
        self.assertFalse(report["qualified_for_app_development"])
        self.assertEqual(report["build_manifest_sha256"], hashlib.sha256((self.root / "BuildManifest.plist").read_bytes()).hexdigest())
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        before = (output / "nor.plist").read_bytes()
        self.assertEqual((output / "nor.plist").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            firmware.main(self.cli(output) + ["--nor"])
        self.assertEqual((output / "nor.plist").read_bytes(), before)
        individual = self.root / "individual"
        with contextlib.redirect_stdout(io.StringIO()):
            firmware.main(self.cli(individual) + ["--component", "RestoreSEP"])
        self.assertFalse(json.loads((individual / "firmware.json").read_text())["nor_response_complete"])
        self.assertTrue((individual / "RestoreSEP.img4").is_file())


class FileBoundaryTests(unittest.TestCase):
    def test_paths_symlinks_and_special_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "good").write_bytes(b"firmware")
            (root / "link").symlink_to("good")
            (root / "directory-link").symlink_to(root, target_is_directory=True)
            os.mkfifo(root / "fifo")
            self.assertEqual(firmware.read_component(root, "good"), b"firmware")
            for name in ("../good", "/good", "./good", "a//b", "good/", "C:good", "a\\good", "a\0", None):
                with self.subTest(path=name), self.assertRaises(ValueError):
                    firmware.read_component(root, name)
            for name in ("link", "directory-link/good", "fifo"):
                with self.subTest(path=name), self.assertRaises((OSError, ValueError)):
                    firmware.read_component(root, name)
            with self.assertRaisesRegex(ValueError, "bounded"):
                firmware.read_component(root, "good", 3)
            (root / "empty").touch()
            with self.assertRaisesRegex(ValueError, "nonempty"):
                firmware.read_component(root, "empty")


if __name__ == "__main__":
    unittest.main()
