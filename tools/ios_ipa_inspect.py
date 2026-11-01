#!/usr/bin/env python3
"""Read an iOS app's identity, signature and provisioning profile without a device.

`inspect PATH` takes an `.ipa` archive or an `.app` directory and reports what
it says about itself: its bundle identifier, version and minimum iOS from
Info.plist, the architectures and code signature of its executable (signing
identifier, team, whether it is ad hoc, the signing certificate's name and
SHA-1 fingerprint), the entitlements embedded in that signature, and a summary
of `embedded.mobileprovision` (name, team, expiry, device count, kind and its
certificates). Nothing is verified cryptographically: this reads what the files
say, so a tampered bundle reads as whatever it claims to be.

`info PATH...` reads only the Info.plist of each `.app` directory.

`profiles [--device UDID] [--app APP]... PROFILE...` summarises provisioning
profiles and the signing certificates they and the apps name, with warnings:
expired or expiring within seven days, a free account's seven-day profile, a
device the profile does not cover.

Only Python's standard library is used. Nothing is written, run or fetched.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import plistlib
import re
import stat
import struct
import sys
import zipfile

MAX_PLIST_BYTES = 1 << 20
MAX_PROFILE_BYTES = 1 << 20
MAX_HEADER_BYTES = 1 << 20
MAX_SIGNATURE_BYTES = 8 << 20
MAX_ARCHIVE_MEMBERS = 100000
LC_CODE_SIGNATURE = 0x1d
CPU_TYPES = {0x100000c: "arm64", 0x1000007: "x86_64", 0xc: "arm", 0x7: "i386"}
DEVICE_FAMILIES = {1: "iphone", 2: "ipad", 3: "tv", 4: "watch", 6: "mac", 7: "vision"}
APP_IN_ARCHIVE = re.compile(r"Payload/([^/]+\.app)/Info\.plist")
# X.509 attribute types of a certificate's subject: common name,
# organizational unit (Apple puts the team identifier there) and organization.
NAME_ATTRIBUTES = {bytes.fromhex("550403"): "common_name", bytes.fromhex("55040b"): "organizational_unit",
                   bytes.fromhex("55040a"): "organization"}


class Source:
    """The files of one app, from an archive or a directory, read without
    following links. Paths are relative to the `.app` directory."""

    def __init__(self, path):
        self.path = Path(path)
        self.archive = None
        if self.path.is_symlink():
            raise ValueError(f"{self.path} is a symbolic link; name the file itself")
        if self.path.is_dir():
            if not self.path.name.endswith(".app"):
                raise ValueError(f"{self.path} is a directory but not an .app bundle")
            self.kind, self.app, self.prefix = "app", self.path.name, None
        elif self.path.is_file():
            try:
                self.archive = zipfile.ZipFile(self.path)
            except zipfile.BadZipFile:
                raise ValueError(f"{self.path} is neither an .app directory nor an .ipa (zip) archive") from None
            names = self.archive.namelist()
            if len(names) > MAX_ARCHIVE_MEMBERS:
                raise ValueError(f"{self.path} has more than {MAX_ARCHIVE_MEMBERS} members")
            apps = sorted({match.group(1) for name in names if (match := APP_IN_ARCHIVE.fullmatch(name))})
            if len(apps) != 1:
                raise ValueError(f"{self.path} holds {len(apps)} apps under Payload/; an .ipa holds exactly one")
            self.kind, self.app = "ipa", apps[0]
            self.prefix = f"Payload/{self.app}/"
            self.members = {name[len(self.prefix):]: info for info in self.archive.infolist()
                            if (name := info.filename).startswith(self.prefix)}
        else:
            raise ValueError(f"{self.path} does not exist")

    def size(self, name):
        """The size of a regular file of the app, or None."""
        if self.archive is not None:
            info = self.members.get(name)
            if info is None or info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
                return None
            return info.file_size
        file = self.path / name
        try:
            meta = file.lstat()
        except OSError:
            return None
        linked = any((self.path / parent).is_symlink()
                     for parent in Path(name).parents if str(parent) != ".")
        return meta.st_size if stat.S_ISREG(meta.st_mode) and not linked else None

    def read(self, name, limit, offset=0, length=None):
        """Bytes of a regular file of the app; at most `limit` of them."""
        size = self.size(name)
        if size is None:
            raise ValueError(f"{self.app}/{name} is missing or not a regular file")
        length = size - offset if length is None else length
        if offset < 0 or length < 0 or offset + length > size:
            raise ValueError(f"{self.app}/{name} is shorter than its own header says")
        if length > limit:
            raise ValueError(f"{self.app}/{name}: {length} bytes is more than the {limit} read here")
        opened = self.archive.open(self.prefix + name) if self.archive is not None else open(self.path / name, "rb")
        with opened as stream:
            stream.seek(offset)
            data = stream.read(length)
        if len(data) != length:
            raise ValueError(f"{self.app}/{name} changed while it was read")
        return data

    def names(self, directory):
        """The entries directly inside a directory of the app."""
        if self.archive is not None:
            prefix = directory.rstrip("/") + "/"
            return sorted({name[len(prefix):].split("/")[0] for name in self.members
                           if name.startswith(prefix) and len(name) > len(prefix)})
        folder = self.path / directory
        if folder.is_symlink() or not folder.is_dir():
            return []
        return sorted(entry.name for entry in folder.iterdir())


def jsonable(value):
    """A property-list value as JSON: dates as ISO 8601 UTC, data as its length."""
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, datetime.datetime):
        return iso(value)
    if isinstance(value, (bytes, bytearray)):
        return {"data_bytes": len(value)}
    return value


def iso(moment):
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def property_list(data, what):
    try:
        value = plistlib.loads(data)
    except Exception as error:  # plistlib raises several kinds for damaged input
        raise ValueError(f"{what} is not a property list") from error
    if not isinstance(value, dict):
        raise ValueError(f"{what} must hold a dictionary")
    return value


def text(value):
    return value if isinstance(value, str) and value else None


def bundle_info(info):
    """What an app's Info.plist says about it."""
    families = info.get("UIDeviceFamily")
    families = [families] if isinstance(families, int) else families if isinstance(families, list) else []
    platforms = info.get("CFBundleSupportedPlatforms")
    return {
        "bundle_id": text(info.get("CFBundleIdentifier")),
        "name": text(info.get("CFBundleName")),
        "display_name": text(info.get("CFBundleDisplayName")) or text(info.get("CFBundleName")),
        "version": text(info.get("CFBundleShortVersionString")),
        "build": text(info.get("CFBundleVersion")),
        "minimum_os": text(info.get("MinimumOSVersion")),
        "executable": text(info.get("CFBundleExecutable")),
        "device_family": [DEVICE_FAMILIES.get(family, str(family)) for family in families
                          if isinstance(family, int) and not isinstance(family, bool)],
        "platforms": [item for item in platforms if isinstance(item, str)] if isinstance(platforms, list) else [],
    }


# --- DER, as far as certificates and CMS SignedData need it --------------------------------------

def der(data, offset=0):
    """(tag, start of contents, end of contents) of the DER element at offset."""
    if offset + 2 > len(data):
        raise ValueError("truncated DER")
    tag, length = data[offset], data[offset + 1]
    start = offset + 2
    if length & 0x80:
        count = length & 0x7f
        if count == 0 or count > 4 or start + count > len(data):
            raise ValueError("unsupported DER length")
        length = int.from_bytes(data[start:start + count], "big")
        start += count
    if start + length > len(data):
        raise ValueError("truncated DER")
    return tag, start, start + length


def children(data, start, end):
    """The (tag, start, end) of each element between start and end."""
    items = []
    while start < end:
        tag, inner, stop = der(data, start)
        items.append((tag, inner, stop, start))
        start = stop
    return items


def der_time(data, tag, start, end):
    raw = data[start:end].decode("ascii", "replace")
    try:
        if tag == 0x17:  # UTCTime, YYMMDDHHMMSSZ
            moment = datetime.datetime.strptime(raw, "%y%m%d%H%M%SZ")
        elif tag == 0x18:  # GeneralizedTime, YYYYMMDDHHMMSSZ
            moment = datetime.datetime.strptime(raw, "%Y%m%d%H%M%SZ")
        else:
            return None
    except ValueError:
        return None
    return iso(moment)


def names_of(data, start, end):
    """The subject or issuer attributes Apple's certificates carry."""
    found = {}
    for _, set_start, set_end, _ in children(data, start, end):
        for _, pair_start, pair_end, _ in children(data, set_start, set_end):
            parts = children(data, pair_start, pair_end)
            if len(parts) == 2 and parts[0][0] == 0x06:
                key = NAME_ATTRIBUTES.get(data[parts[0][1]:parts[0][2]])
                if key:
                    found[key] = data[parts[1][1]:parts[1][2]].decode("utf-8", "replace")
    return found


def certificate(blob):
    """Subject, issuer, validity and SHA-1 fingerprint of one DER certificate."""
    _, start, end = der(blob)
    tbs = children(blob, *der(blob, start)[1:])
    if tbs and tbs[0][0] == 0xa0:  # explicit version
        tbs = tbs[1:]
    if len(tbs) < 5:
        raise ValueError("not an X.509 certificate")
    issuer, validity, subject = tbs[2], tbs[3], tbs[4]
    times = children(blob, validity[1], validity[2])
    return {**{key: None for key in NAME_ATTRIBUTES.values()}, **names_of(blob, subject[1], subject[2]),
            "issuer_common_name": names_of(blob, issuer[1], issuer[2]).get("common_name"),
            "not_before": der_time(blob, *times[0][:3]) if len(times) > 0 else None,
            "not_after": der_time(blob, *times[1][:3]) if len(times) > 1 else None,
            "sha1": hashlib.sha1(blob[:end]).hexdigest()}


def signed_data(cms):
    """(certificates as DER, encapsulated content or None) of a CMS SignedData."""
    _, start, end = der(cms)
    outer = children(cms, start, end)
    if len(outer) < 2 or outer[0][0] != 0x06 or outer[1][0] != 0xa0:
        raise ValueError("not a CMS ContentInfo")
    _, sd_start, sd_end = der(cms, outer[1][1])
    fields = children(cms, sd_start, sd_end)
    if len(fields) < 3:
        raise ValueError("not a CMS SignedData")
    content = None
    encapsulated = children(cms, fields[2][1], fields[2][2])
    if len(encapsulated) == 2 and encapsulated[1][0] == 0xa0:
        tag, inner, stop = der(cms, encapsulated[1][1])
        if tag == 0x04:
            content = cms[inner:stop]
    certificates = []
    for tag, inner, stop, _ in fields[3:]:
        if tag == 0xa0:
            certificates = [cms[at:until] for _, _, until, at in children(cms, inner, stop)]
    return certificates, content


def leaf(certificates):
    """The certificate that issued none of the others: the signer's own."""
    issuers = {row["issuer_common_name"] for row in certificates}
    own = [row for row in certificates if row["common_name"] not in issuers]
    return own[0] if len(own) == 1 else (certificates[0] if len(certificates) == 1 else None)


# --- Mach-O code signature ------------------------------------------------------------------------

def slices(source, executable, size):
    """(architecture, offset, size) of each slice of a thin or universal Mach-O."""
    head = source.read(executable, MAX_HEADER_BYTES, 0, min(size, 4096))
    if len(head) < 8:
        raise ValueError(f"{source.app}/{executable} is not a Mach-O file")
    magic = struct.unpack_from(">I", head)[0]
    if magic in (0xcafebabe, 0xcafebabf):
        wide = magic == 0xcafebabf
        count = struct.unpack_from(">I", head, 4)[0]
        step = 32 if wide else 20
        if count > 64 or 8 + count * step > len(head):
            raise ValueError(f"{source.app}/{executable} has an unreadable universal header")
        found = []
        for index in range(count):
            at = 8 + index * step
            if wide:
                cpu, subtype, offset, length = struct.unpack_from(">iiQQ", head, at)
            else:
                cpu, subtype, offset, length = struct.unpack_from(">iiII", head, at)
            found.append((architecture(cpu, subtype), offset, length))
        return found
    if magic in (0xcffaedfe, 0xcefaedfe):
        cpu, subtype = struct.unpack_from("<ii", head, 4)
        return [(architecture(cpu, subtype), 0, size)]
    raise ValueError(f"{source.app}/{executable} is not a Mach-O file (magic {magic:#010x})")


def architecture(cpu, subtype):
    name = CPU_TYPES.get(cpu & 0xffffffff, f"cpu-{cpu & 0xffffffff:#x}")
    return "arm64e" if name == "arm64" and subtype & 0xffffff == 2 else name


def code_signature(source, executable, offset, size):
    """The signature blob of one 64-bit slice, or None when it is unsigned."""
    header = source.read(executable, MAX_HEADER_BYTES, offset, min(size, 32))
    if len(header) < 32 or struct.unpack_from("<I", header)[0] != 0xfeedfacf:
        return None
    commands, command_bytes = struct.unpack_from("<II", header, 16)
    if command_bytes > MAX_HEADER_BYTES or 32 + command_bytes > size:
        raise ValueError(f"{source.app}/{executable} has unreadable load commands")
    table = source.read(executable, MAX_HEADER_BYTES, offset + 32, command_bytes)
    at = 0
    for _ in range(min(commands, 4096)):
        if at + 8 > len(table):
            break
        command, length = struct.unpack_from("<II", table, at)
        if length < 8:
            break
        if command == LC_CODE_SIGNATURE and length >= 16:
            start, blob_size = struct.unpack_from("<II", table, at + 8)
            if start + blob_size > size:
                raise ValueError(f"{source.app}/{executable}'s code signature lies outside the file")
            return source.read(executable, MAX_SIGNATURE_BYTES, offset + start, blob_size)
        at += length
    return None


def c_string(data, start):
    if not 0 < start < len(data):
        return None
    stop = data.find(b"\0", start)
    return data[start:stop if stop >= 0 else len(data)].decode("utf-8", "replace") or None


def signature_summary(blob):
    """What a code signature super blob says: identifier, team, ad hoc or
    signed by a certificate, its entitlements and the signer's certificate."""
    result = {"signed": False, "ad_hoc": None, "identifier": None, "team_id": None, "certificate": None,
              "certificates": 0}
    entitlements = {}
    if blob is None or len(blob) < 12:
        return result, entitlements
    magic, length, count = struct.unpack_from(">III", blob)
    if magic != 0xfade0cc0 or count > 64 or length > len(blob):
        return result, entitlements
    result["signed"] = True
    flags = None
    for index in range(count):
        if 12 + index * 8 + 8 > len(blob):
            break
        slot, relative = struct.unpack_from(">II", blob, 12 + index * 8)
        if relative + 8 > len(blob):
            continue
        kind, size = struct.unpack_from(">II", blob, relative)
        body = blob[relative:relative + size]
        if slot == 0 and kind == 0xfade0c02 and len(body) >= 52:
            version, flags = struct.unpack_from(">II", body, 8)
            result["identifier"] = c_string(body, struct.unpack_from(">I", body, 20)[0])
            if version >= 0x20200 and len(body) >= 52:
                result["team_id"] = c_string(body, struct.unpack_from(">I", body, 48)[0])
        elif slot == 5 and kind == 0xfade7171 and size >= 8:
            entitlements = property_list(body[8:], "The executable's embedded entitlements")
        elif slot == 0x10000 and kind == 0xfade0b01 and size > 8:
            try:
                rows = [certificate(cert) for cert in signed_data(body[8:])[0]]
            except (ValueError, IndexError, struct.error):
                rows = []
            result["certificates"] = len(rows)
            result["certificate"] = leaf(rows)
    # CS_ADHOC: no certificate vouches for the signature.
    result["ad_hoc"] = bool(flags & 0x2) if flags is not None else result["certificate"] is None
    return result, entitlements


# --- Provisioning profile ------------------------------------------------------------------------

def profile_plist(data):
    """The property list inside a provisioning profile's CMS envelope."""
    try:
        content = signed_data(data)[1]
    except (ValueError, IndexError):
        content = None
    if content is None:
        # Some writers use indefinite lengths; the plist is still in the clear.
        start, stop = data.find(b"<?xml"), data.find(b"</plist>")
        if start < 0 or stop < start:
            raise ValueError("embedded.mobileprovision holds no readable property list")
        content = data[start:stop + len(b"</plist>")]
    return property_list(content, "embedded.mobileprovision")


def profile_summary(profile, now):
    """A provisioning profile's name, team, dates, devices, kind and certificates."""
    entitlements = profile.get("Entitlements") if isinstance(profile.get("Entitlements"), dict) else {}
    devices = profile.get("ProvisionedDevices")
    devices = devices if isinstance(devices, list) else None
    teams = profile.get("TeamIdentifier")
    team = teams[0] if isinstance(teams, list) and teams and isinstance(teams[0], str) else None
    expiration = profile.get("ExpirationDate")
    expiration = expiration.replace(tzinfo=datetime.timezone.utc) if isinstance(
        expiration, datetime.datetime) and expiration.tzinfo is None else expiration
    all_devices = profile.get("ProvisionsAllDevices") is True
    if all_devices:
        kind = "enterprise"
    elif devices is None:
        kind = "app-store"
    elif entitlements.get("get-task-allow") is True:
        kind = "development"
    else:
        kind = "ad-hoc"
    certificates = []
    for blob in profile.get("DeveloperCertificates") or []:
        if isinstance(blob, bytes):
            try:
                certificates.append(certificate(blob))
            except (ValueError, IndexError):
                certificates.append({"unreadable": True, "sha1": hashlib.sha1(blob).hexdigest()})
    return {
        "name": text(profile.get("Name")), "uuid": text(profile.get("UUID")),
        "app_id_name": text(profile.get("AppIDName")), "team_id": team, "team_name": text(profile.get("TeamName")),
        "application_identifier": text(entitlements.get("application-identifier")),
        "created": iso(profile["CreationDate"]) if isinstance(profile.get("CreationDate"), datetime.datetime) else None,
        "expires": iso(expiration) if isinstance(expiration, datetime.datetime) else None,
        "expired": expiration <= now if isinstance(expiration, datetime.datetime) else None,
        "days_left": (expiration - now).days if isinstance(expiration, datetime.datetime) else None,
        "kind": kind, "provisions_all_devices": all_devices,
        "devices": len(devices) if devices is not None else 0,
        "platforms": [item for item in profile.get("Platform") or [] if isinstance(item, str)],
        "entitlements": jsonable(entitlements), "certificates": certificates,
    }


def identifier_matches(pattern, team, bundle_id):
    """Whether a profile's application-identifier (TEAM.bundle, maybe ending
    in '*') covers a bundle identifier."""
    if not pattern or not bundle_id:
        return None
    if team and pattern.startswith(team + "."):
        pattern = pattern[len(team) + 1:]
    elif "." in pattern:
        pattern = pattern.split(".", 1)[1]
    if pattern.endswith("*"):
        return bundle_id.startswith(pattern[:-1])
    return pattern == bundle_id


def inspect(path, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    source = Source(path)
    info = property_list(source.read("Info.plist", MAX_PLIST_BYTES), f"{source.app}/Info.plist")
    result = {"path": str(Path(path)), "kind": source.kind, "app": source.app, **bundle_info(info)}
    warnings = []
    executable = result["executable"]
    size = source.size(executable) if executable and "/" not in executable else None
    signature, entitlements = signature_summary(None)
    architectures = []
    if size is None:
        warnings.append("The executable that CFBundleExecutable names is missing")
    else:
        found = slices(source, executable, size)
        architectures = [name for name, _, _ in found]
        chosen = next((row for row in found if row[0] == "arm64"), None) or next(
            (row for row in found if row[0] == "arm64e"), found[0])
        signature, entitlements = signature_summary(code_signature(source, executable, chosen[1], chosen[2]))
        if not {"arm64", "arm64e"} & set(architectures):
            warnings.append("The executable has no arm64 slice, so it cannot run on an iPhone")
    if not signature["signed"]:
        warnings.append("The executable is not signed")
    profile = None
    if source.size("embedded.mobileprovision") is not None:
        profile = profile_summary(profile_plist(source.read("embedded.mobileprovision", MAX_PROFILE_BYTES)), now)
        signer = signature["certificate"]
        profile["bundle_id_matches"] = identifier_matches(profile["application_identifier"], profile["team_id"],
                                                          result["bundle_id"])
        profile["signing_certificate_included"] = None if not signer else any(
            row.get("sha1") == signer["sha1"] for row in profile["certificates"])
        if profile["expired"]:
            warnings.append(f"The provisioning profile expired on {profile['expires']}")
        if profile["bundle_id_matches"] is False:
            warnings.append(f"The provisioning profile is for {profile['application_identifier']}, "
                            f"not {result['bundle_id']}")
        if profile["signing_certificate_included"] is False:
            warnings.append("The signing certificate is not among the provisioning profile's certificates")
        if signature["team_id"] and profile["team_id"] and signature["team_id"] != profile["team_id"]:
            warnings.append(f"The signature's team {signature['team_id']} is not the profile's {profile['team_id']}")
    elif signature["signed"] and not signature["ad_hoc"]:
        warnings.append("The app is signed by a certificate but has no embedded.mobileprovision")
    result.update(architectures=architectures, signature=signature, entitlements=jsonable(entitlements),
                  provisioning_profile=profile,
                  frameworks=[name for name in source.names("Frameworks")
                              if name.endswith((".framework", ".dylib"))],
                  extensions=[name for name in source.names("PlugIns") if name.endswith(".appex")],
                  warnings=warnings)
    return result


# How long a profile from a free Apple account lasts; a day more absorbs time zones.
FREE_PROFILE_DAYS = 7
SOON_DAYS = 7


def profile_rows(paths, device=None, now=None):
    """Each profile's summary with its own warnings, or why it could not be read."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    rows = []
    for path in paths:
        try:
            data = Path(path).read_bytes()
            if len(data) > MAX_PROFILE_BYTES:
                raise ValueError(f"{path} is larger than {MAX_PROFILE_BYTES} bytes")
            plist = profile_plist(data)
            summary = profile_summary(plist, now)
        except (OSError, ValueError, IndexError, struct.error) as error:
            rows.append({"path": str(path), "ok": False, "error": str(error), "warnings": []})
            continue
        created, expires = plist.get("CreationDate"), plist.get("ExpirationDate")
        lifetime = (expires - created).days if isinstance(created, datetime.datetime) and isinstance(
            expires, datetime.datetime) else None
        devices = [item for item in plist.get("ProvisionedDevices") or [] if isinstance(item, str)]
        summary.update(path=str(path), ok=True, device_ids=devices,
                       free_account=lifetime is not None and lifetime <= FREE_PROFILE_DAYS)
        summary["covers_device"] = None if not device else (
            summary["provisions_all_devices"] or device.lower() in (item.lower() for item in devices))
        warnings = []
        if summary["expired"]:
            warnings.append(f"Expired on {summary['expires']}")
        elif summary["days_left"] is not None and summary["days_left"] < SOON_DAYS:
            warnings.append(f"Expires in {summary['days_left']} day(s), on {summary['expires']}")
        if summary["free_account"]:
            warnings.append("Made with a free Apple account: it lasts 7 days, and apps signed with it stop "
                            "opening then. Build and install again with xtool to renew it")
        if summary["covers_device"] is False:
            warnings.append(f"Does not list the device {device}")
        summary["warnings"] = warnings
        rows.append(summary)
    return rows


def identity_rows(profiles, apps, now=None):
    """The signing certificates the profiles list and the apps are signed with,
    once each by SHA-1, with where they were seen and how long they last."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    found = {}

    def add(certificate_row, team, source):
        if not certificate_row or certificate_row.get("unreadable") or not certificate_row.get("sha1"):
            return
        row = found.setdefault(certificate_row["sha1"], {
            "name": certificate_row.get("common_name"), "sha1": certificate_row["sha1"],
            "team_id": certificate_row.get("organizational_unit") or team,
            "issuer": certificate_row.get("issuer_common_name"), "expires": certificate_row.get("not_after"),
            "expired": None, "days_left": None, "seen_in": [], "warnings": []})
        if source not in row["seen_in"]:
            row["seen_in"].append(source)

    for profile in profiles:
        if profile.get("ok"):
            for row in profile["certificates"]:
                add(row, profile["team_id"], profile["path"])
    app_rows = []
    for path in apps:
        try:
            record = inspect(path, now)
        except (OSError, ValueError, zipfile.BadZipFile, struct.error) as error:
            app_rows.append({"path": str(path), "ok": False, "error": str(error)})
            continue
        signature = record["signature"]
        add(signature["certificate"], signature["team_id"], str(path))
        app_rows.append({"path": str(path), "ok": True, "bundle_id": record["bundle_id"],
                         "signed": signature["signed"], "ad_hoc": signature["ad_hoc"],
                         "team_id": signature["team_id"],
                         "certificate_sha1": (signature["certificate"] or {}).get("sha1"),
                         "warnings": record["warnings"]})
    for row in found.values():
        try:
            moment = datetime.datetime.fromisoformat(row["expires"].replace("Z", "+00:00")) if row["expires"] else None
        except ValueError:
            moment = None
        if moment is not None:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=datetime.timezone.utc)
            row["expired"] = moment <= now
            row["days_left"] = (moment - now).days
            if row["expired"]:
                row["warnings"].append(f"The certificate expired on {row['expires']}")
            elif row["days_left"] < SOON_DAYS:
                row["warnings"].append(f"The certificate expires in {row['days_left']} day(s)")
    return sorted(found.values(), key=lambda row: (row["name"] or "", row["sha1"])), app_rows


def overview(paths, apps=(), device=None, now=None):
    profiles = profile_rows(paths, device, now)
    identities, app_rows = identity_rows(profiles, apps, now)
    warnings = [f"{Path(row['path']).name}: {text}" for row in profiles for text in row["warnings"]]
    warnings += [f"{row['name'] or row['sha1']}: {text}" for row in identities for text in row["warnings"]]
    return {"profiles": profiles, "identities": identities, "apps": app_rows, "warnings": warnings}


def info_rows(paths):
    """Info.plist of each .app directory, or the reason it cannot be read."""
    rows = []
    for path in paths:
        try:
            source = Source(path)
            if source.kind != "app":
                raise ValueError(f"{path} is not an .app directory")
            info = property_list(source.read("Info.plist", MAX_PLIST_BYTES), f"{source.app}/Info.plist")
            rows.append({"path": str(path), "ok": True, **bundle_info(info)})
        except (OSError, ValueError) as error:
            rows.append({"path": str(path), "ok": False, "error": str(error)})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("inspect").add_argument("path", type=Path, help="An .ipa archive or an .app directory")
    commands.add_parser("info").add_argument("paths", type=Path, nargs="+", help=".app directories")
    listing = commands.add_parser("profiles")
    listing.add_argument("--device", help="UDID the profiles should list")
    listing.add_argument("--app", type=Path, action="append", default=[], help=".app or .ipa whose signer to add")
    listing.add_argument("paths", type=Path, nargs="*", help=".mobileprovision files")
    args = parser.parse_args()
    if args.action == "profiles":
        print(json.dumps({"ok": True, **overview(args.paths, args.app, args.device)}))
        return 0
    if args.action == "info":
        print(json.dumps({"ok": True, "applications": info_rows(args.paths)}))
        return 0
    try:
        record = inspect(args.path)
    except (OSError, ValueError, zipfile.BadZipFile, struct.error) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    print(json.dumps({"ok": True, **record}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
