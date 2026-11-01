"""IPA inspection reads identity, signature and provisioning from synthetic files.

The fixtures are built here: a thin arm64 Mach-O header with a code signature
super blob, and DER certificates and CMS envelopes that carry the right shape
but no valid cryptography. Nothing from Apple is involved.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import ios_ipa_inspect as tool  # noqa: E402


def element(tag, *parts):
    body = b"".join(parts)
    if len(body) < 0x80:
        length = bytes([len(body)])
    else:
        raw = len(body).to_bytes((len(body).bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(raw)]) + raw
    return bytes([tag]) + length + body


def sequence(*parts):
    return element(0x30, *parts)


def name(common, unit=None):
    rows = [element(0x31, sequence(element(0x06, bytes.fromhex("550403")), element(0x0c, common.encode())))]
    if unit:
        rows.append(element(0x31, sequence(element(0x06, bytes.fromhex("55040b")), element(0x0c, unit.encode()))))
    return sequence(*rows)


def certificate(common, issuer, unit=None):
    tbs = sequence(element(0xa0, element(0x02, b"\x02")), element(0x02, b"\x01"),
                   sequence(element(0x06, b"\x2a\x86\x48")), name(issuer),
                   sequence(element(0x17, b"260101000000Z"), element(0x17, b"270101000000Z")),
                   name(common, unit), sequence())
    return sequence(tbs, sequence(element(0x06, b"\x2a\x86\x48")), element(0x03, b"\x00\x00"))


def cms(certificates, content=None):
    encapsulated = [element(0x06, b"\x2a\x86\x48\x86\xf7\x0d\x01\x07\x01")]
    if content is not None:
        encapsulated.append(element(0xa0, element(0x04, content)))
    signed = sequence(element(0x02, b"\x01"), element(0x31), sequence(*encapsulated),
                      element(0xa0, *certificates), element(0x31))
    return sequence(element(0x06, b"\x2a\x86\x48\x86\xf7\x0d\x01\x07\x02"), element(0xa0, signed))


def blob(magic, body):
    return struct.pack(">II", magic, 8 + len(body)) + body


def code_directory(identifier, team, flags=0):
    ident = identifier.encode() + b"\0"
    team_bytes = team.encode() + b"\0" if team else b""
    header = 52
    fields = struct.pack(">IIIIIIIBBBBIII", 0x20200, flags, 0, header, 0, 0, 0, 0, 0, 0, 0,
                         0, 0, header + len(ident) if team else 0)
    body = fields + ident + team_bytes
    return struct.pack(">II", 0xfade0c02, 8 + len(body)) + body


def super_blob(slots):
    count = len(slots)
    offset = 12 + 8 * count
    index, payload = b"", b""
    for slot, data in slots:
        index += struct.pack(">II", slot, offset + len(payload))
        payload += data
    return struct.pack(">III", 0xfade0cc0, 12 + len(index) + len(payload), count) + index + payload


def macho(signature, cpu=0x100000c, subtype=0):
    """A 64-bit Mach-O header, its one LC_CODE_SIGNATURE and the signature at offset 64."""
    if signature is None:
        return struct.pack("<IiiIIIII", 0xfeedfacf, cpu, subtype, 2, 0, 0, 0, 0) + b"\0" * 64
    command = struct.pack("<IIII", 0x1d, 16, 64, len(signature))
    code = struct.pack("<IiiIIIII", 0xfeedfacf, cpu, subtype, 2, 1, len(command), 0, 0) + command
    return code + b"\0" * (64 - len(code)) + signature


SIGNER = certificate("Apple Development: Jane Doe (ABCDE12345)", "Apple Worldwide Developer Relations", "TEAM123456")
INTERMEDIATE = certificate("Apple Worldwide Developer Relations", "Apple Root CA")
ENTITLEMENTS = {"application-identifier": "TEAM123456.dev.example.Demo", "get-task-allow": True}


def signed_executable(adhoc=False):
    slots = [(0, code_directory("dev.example.Demo", None if adhoc else "TEAM123456", 0x2 if adhoc else 0)),
             (5, blob(0xfade7171, plistlib.dumps(ENTITLEMENTS)))]
    if not adhoc:
        slots.append((0x10000, blob(0xfade0b01, cms([SIGNER, INTERMEDIATE]))))
    return macho(super_blob(slots))


def provisioning(expires, devices=("00008110-000A1B2C3D4E5F60",), certificates=(SIGNER,), identifier=None):
    profile = {"Name": "Demo Development", "UUID": "1D1C2B3A-0000-4000-8000-000000000001",
               "AppIDName": "Demo", "TeamIdentifier": ["TEAM123456"], "TeamName": "Jane Doe",
               "CreationDate": datetime.datetime(2026, 1, 1), "ExpirationDate": expires,
               "Platform": ["iOS"], "DeveloperCertificates": list(certificates),
               "Entitlements": {"application-identifier": identifier or "TEAM123456.dev.example.*",
                                "get-task-allow": True}}
    if devices is not None:
        profile["ProvisionedDevices"] = list(devices)
    return cms([], plistlib.dumps(profile))


INFO = {"CFBundleIdentifier": "dev.example.Demo", "CFBundleExecutable": "Demo", "CFBundleName": "Demo",
        "CFBundleShortVersionString": "1.4", "CFBundleVersion": "27", "MinimumOSVersion": "17.0",
        "UIDeviceFamily": [1, 2], "CFBundlePackageType": "APPL"}
NOW = datetime.datetime(2026, 10, 31, tzinfo=datetime.timezone.utc)


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def ipa(self, files, name="Demo.app"):
        path = self.root / "Demo.ipa"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for relative, data in files.items():
                archive.writestr(f"Payload/{name}/{relative}", data)
        return path

    def app(self, files, name="Demo.app"):
        root = self.root / name
        for relative, data in files.items():
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_bytes(data)
        return root


class InspectTests(Fixture):
    def test_a_development_ipa_reports_identity_signature_and_profile(self):
        files = {"Info.plist": plistlib.dumps(INFO), "Demo": signed_executable(),
                 "embedded.mobileprovision": provisioning(datetime.datetime(2027, 1, 30)),
                 "Frameworks/Lib.framework/Lib": b"x", "PlugIns/Widget.appex/Info.plist": b"x"}
        result = tool.inspect(self.ipa(files), NOW)
        self.assertEqual(result["kind"], "ipa")
        self.assertEqual((result["bundle_id"], result["version"], result["build"], result["minimum_os"]),
                         ("dev.example.Demo", "1.4", "27", "17.0"))
        self.assertEqual(result["device_family"], ["iphone", "ipad"])
        self.assertEqual(result["architectures"], ["arm64"])
        signature = result["signature"]
        self.assertTrue(signature["signed"])
        self.assertFalse(signature["ad_hoc"])
        self.assertEqual((signature["identifier"], signature["team_id"]), ("dev.example.Demo", "TEAM123456"))
        self.assertEqual(signature["certificates"], 2)
        self.assertEqual(signature["certificate"]["common_name"], "Apple Development: Jane Doe (ABCDE12345)")
        self.assertEqual(signature["certificate"]["organizational_unit"], "TEAM123456")
        self.assertEqual(signature["certificate"]["not_after"], "2027-01-01T00:00:00Z")
        self.assertEqual(signature["certificate"]["sha1"], hashlib.sha1(SIGNER).hexdigest())
        self.assertEqual(result["entitlements"], ENTITLEMENTS)
        profile = result["provisioning_profile"]
        self.assertEqual((profile["kind"], profile["devices"], profile["team_id"]), ("development", 1, "TEAM123456"))
        self.assertEqual(profile["expires"], "2027-01-30T00:00:00Z")
        self.assertFalse(profile["expired"])
        self.assertEqual(profile["days_left"], 91)
        self.assertTrue(profile["bundle_id_matches"])
        self.assertTrue(profile["signing_certificate_included"])
        self.assertEqual(profile["certificates"][0]["common_name"], "Apple Development: Jane Doe (ABCDE12345)")
        self.assertEqual(result["frameworks"], ["Lib.framework"])
        self.assertEqual(result["extensions"], ["Widget.appex"])
        self.assertEqual(result["warnings"], [])

    def test_an_expired_mismatched_profile_is_named_in_warnings(self):
        files = {"Info.plist": plistlib.dumps(INFO), "Demo": signed_executable(),
                 "embedded.mobileprovision": provisioning(datetime.datetime(2026, 3, 1), devices=None,
                                                         certificates=(INTERMEDIATE,),
                                                         identifier="OTHER12345.dev.other.App")}
        result = tool.inspect(self.app(files), NOW)
        profile = result["provisioning_profile"]
        self.assertTrue(profile["expired"])
        self.assertEqual(profile["kind"], "app-store")
        self.assertFalse(profile["bundle_id_matches"])
        self.assertFalse(profile["signing_certificate_included"])
        joined = " ".join(result["warnings"])
        self.assertIn("expired on 2026-03-01T00:00:00Z", joined)
        self.assertIn("not dev.example.Demo", joined)
        self.assertIn("not among the provisioning profile's certificates", joined)

    def test_an_ad_hoc_app_without_profile_and_an_unsigned_one(self):
        result = tool.inspect(self.app({"Info.plist": plistlib.dumps(INFO), "Demo": signed_executable(True)}), NOW)
        self.assertTrue(result["signature"]["ad_hoc"])
        self.assertIsNone(result["signature"]["certificate"])
        self.assertIsNone(result["provisioning_profile"])
        self.assertEqual(result["warnings"], [])
        unsigned = self.root / "unsigned"
        unsigned.mkdir()
        app = unsigned / "Demo.app"
        app.mkdir()
        (app / "Info.plist").write_bytes(plistlib.dumps(INFO))
        (app / "Demo").write_bytes(macho(None))
        result = tool.inspect(app, NOW)
        self.assertFalse(result["signature"]["signed"])
        self.assertIn("The executable is not signed", result["warnings"])

    def test_a_universal_binary_reads_its_arm64_slice(self):
        thin = signed_executable()
        offset = 4096
        fat = struct.pack(">II", 0xcafebabe, 2) + struct.pack(">iiIII", 0x1000007, 3, offset, 16, 12)
        fat += struct.pack(">iiIII", 0x100000c, 0, offset, len(thin), 14)
        fat += b"\0" * (offset - len(fat)) + thin
        result = tool.inspect(self.app({"Info.plist": plistlib.dumps(INFO), "Demo": fat}), NOW)
        self.assertEqual(result["architectures"], ["x86_64", "arm64"])
        self.assertEqual(result["signature"]["team_id"], "TEAM123456")

    def test_refusals_name_the_reason(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            tool.inspect(self.root / "missing.ipa")
        (self.root / "notzip.ipa").write_bytes(b"plain")
        with self.assertRaisesRegex(ValueError, "neither an .app directory nor an .ipa"):
            tool.inspect(self.root / "notzip.ipa")
        path = self.root / "two.ipa"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Payload/A.app/Info.plist", plistlib.dumps(INFO))
            archive.writestr("Payload/B.app/Info.plist", plistlib.dumps(INFO))
        with self.assertRaisesRegex(ValueError, "holds 2 apps"):
            tool.inspect(path)
        result = tool.inspect(self.ipa({"Info.plist": plistlib.dumps(INFO)}), NOW)
        self.assertIn("The executable that CFBundleExecutable names is missing", result["warnings"])
        link = self.root / "Link.app"
        link.symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            tool.inspect(link)

    def test_a_linked_executable_inside_an_app_is_not_followed(self):
        app = self.app({"Info.plist": plistlib.dumps(INFO), "real": signed_executable()})
        os.symlink(app / "real", app / "Demo")
        result = tool.inspect(app, NOW)
        self.assertIn("The executable that CFBundleExecutable names is missing", result["warnings"])

    def test_the_command_line_answers_json(self):
        app = self.app({"Info.plist": plistlib.dumps(INFO), "Demo": signed_executable()})
        run = lambda *args: json.loads(subprocess.run(
            [sys.executable, str(ROOT / "tools/ios_ipa_inspect.py"), *args],
            capture_output=True, text=True, check=False).stdout)
        self.assertEqual(run("inspect", str(app))["bundle_id"], "dev.example.Demo")
        refused = run("inspect", str(self.root / "missing.ipa"))
        self.assertFalse(refused["ok"])
        rows = run("info", str(app), str(self.root / "missing.app"))["applications"]
        self.assertEqual((rows[0]["ok"], rows[0]["version"], rows[0]["build"]), (True, "1.4", "27"))
        self.assertFalse(rows[1]["ok"])


class ProfileOverviewTests(Fixture):
    """ios.signing's reading of profiles and signers, on synthetic files."""

    def profile(self, name, created, expires, devices=("00008110-000A1B2C3D4E5F60",), certificates=(SIGNER,)):
        plist = {"Name": name, "UUID": name.upper(), "TeamIdentifier": ["TEAM123456"], "TeamName": "Jane Doe",
                 "CreationDate": created, "ExpirationDate": expires, "DeveloperCertificates": list(certificates),
                 "Entitlements": {"application-identifier": "TEAM123456.dev.example.*", "get-task-allow": True}}
        if devices is not None:
            plist["ProvisionedDevices"] = list(devices)
        path = self.root / f"{name}.mobileprovision"
        path.write_bytes(cms([], plistlib.dumps(plist)))
        return path

    def test_free_expiring_expired_and_uncovered_profiles_are_warned_about(self):
        free = self.profile("free", datetime.datetime(2026, 10, 29), datetime.datetime(2026, 11, 5))
        paid = self.profile("paid", datetime.datetime(2026, 1, 1), datetime.datetime(2027, 1, 1))
        soon = self.profile("soon", datetime.datetime(2025, 11, 2), datetime.datetime(2026, 11, 2),
                            devices=("00008110-FFFFFFFFFFFFFFFF",))
        old = self.profile("old", datetime.datetime(2025, 1, 1), datetime.datetime(2026, 1, 1))
        broken = self.root / "broken.mobileprovision"
        broken.write_bytes(b"not a profile")
        app = self.app({"Info.plist": plistlib.dumps(INFO), "Demo": signed_executable(),
                        "embedded.mobileprovision": paid.read_bytes()})
        result = tool.overview([free, paid, soon, old, broken], [app], "00008110-000a1b2c3d4e5f60", NOW)
        rows = {Path(row["path"]).stem: row for row in result["profiles"]}
        self.assertTrue(rows["free"]["free_account"])
        self.assertEqual(rows["free"]["days_left"], 5)
        self.assertTrue(any("free Apple account" in text for text in rows["free"]["warnings"]))
        self.assertTrue(any("Expires in 5 day" in text for text in rows["free"]["warnings"]))
        self.assertEqual((rows["paid"]["free_account"], rows["paid"]["warnings"]), (False, []))
        self.assertTrue(rows["paid"]["covers_device"])
        self.assertFalse(rows["soon"]["covers_device"])
        self.assertTrue(any("Does not list the device" in text for text in rows["soon"]["warnings"]))
        self.assertTrue(any("Expires in 2 day" in text for text in rows["soon"]["warnings"]))
        self.assertTrue(rows["old"]["expired"])
        self.assertFalse(rows["broken"]["ok"])
        self.assertEqual(len(result["identities"]), 1)
        identity = result["identities"][0]
        self.assertEqual(identity["name"], "Apple Development: Jane Doe (ABCDE12345)")
        self.assertEqual((identity["team_id"], identity["days_left"], identity["expired"]), ("TEAM123456", 62, False))
        self.assertIn(str(app), identity["seen_in"])
        self.assertEqual(result["apps"][0]["certificate_sha1"], hashlib.sha1(SIGNER).hexdigest())
        self.assertTrue(any(text.startswith("free.mobileprovision: ") for text in result["warnings"]))
        self.assertFalse(any(text.startswith("paid.mobileprovision") for text in result["warnings"]))

    def test_the_command_line_answers_in_json(self):
        paid = self.profile("paid", datetime.datetime(2026, 1, 1), datetime.datetime(2099, 1, 1))
        done = subprocess.run([sys.executable, "-I", str(Path(tool.__file__)), "profiles", "--device",
                               "00008110-000A1B2C3D4E5F60", str(paid)], capture_output=True, text=True, check=True)
        reply = json.loads(done.stdout)
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["profiles"][0]["covers_device"])


class HelperTests(unittest.TestCase):
    def test_application_identifiers_with_and_without_wildcards(self):
        self.assertTrue(tool.identifier_matches("TEAM123456.*", "TEAM123456", "a.b"))
        self.assertTrue(tool.identifier_matches("TEAM123456.a.b", "TEAM123456", "a.b"))
        self.assertFalse(tool.identifier_matches("TEAM123456.a.c", "TEAM123456", "a.b"))
        self.assertIsNone(tool.identifier_matches(None, "TEAM123456", "a.b"))

    def test_plist_values_become_json(self):
        value = tool.jsonable({"d": datetime.datetime(2026, 1, 2, 3, 4, 5), "b": b"abc", "l": [1]})
        self.assertEqual(value, {"d": "2026-01-02T03:04:05Z", "b": {"data_bytes": 3}, "l": [1]})

    def test_a_profile_in_an_indefinite_envelope_is_still_read(self):
        plist = plistlib.dumps({"Name": "X"}, fmt=plistlib.FMT_XML)
        self.assertEqual(tool.profile_plist(b"\x30\x80junk" + plist + b"\0\0")["Name"], "X")
        with self.assertRaisesRegex(ValueError, "no readable property list"):
            tool.profile_plist(b"\x30\x80junk")


if __name__ == "__main__":
    unittest.main()
