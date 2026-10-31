#!/usr/bin/env python3
"""Package local vphone600 firmware for the original restore service.

Image4 assembly is not signing. Existing IM4M bytes are copied unchanged; the
original guest owns verification. No network, credentials or signing service.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import struct

MANIFEST_LIMIT = 1024 * 1024
COMPONENT_LIMIT = 256 * 1024 * 1024
NOR_LIMIT = 64 * 1024 * 1024
FILE_CHUNK_LIMIT = 128 * 1024
FILE_MESSAGE_LIMIT = 65536


def der_item(data, offset=0):
    if offset < 0 or offset + 2 > len(data):
        raise ValueError("Truncated Image4 DER field")
    tag, size = data[offset:offset + 2]
    if tag & 31 == 31:
        raise ValueError("Unexpected high-tag field in Image4 envelope")
    start = offset + 2
    if size & 128:
        count = size & 127
        if not 1 <= count <= 4 or start + count > len(data) or not data[start]:
            raise ValueError("Invalid Image4 DER length")
        size = int.from_bytes(data[start:start + count], "big")
        start += count
        if size < 128:
            raise ValueError("Noncanonical Image4 DER length")
    end = start + size
    if end > len(data):
        raise ValueError("Image4 field exceeds its envelope")
    return tag, start, end


def der_fields(data):
    tag, start, end = der_item(data)
    if tag != 0x30 or end != len(data):
        raise ValueError("Image4 requires exactly one DER sequence")
    fields = []
    while start < end:
        if len(fields) >= 16:
            raise ValueError("Too many fields in Image4 envelope")
        tag, value, next_offset = der_item(data, start)
        fields.append((tag, value, next_offset))
        start = next_offset
    return fields


def validate_im4m(data):
    if not 16 <= len(data) <= MANIFEST_LIMIT:
        raise ValueError("Boot manifest must be a bounded DER IM4M")
    fields = der_fields(data)
    if len(fields) != 5:
        raise ValueError("Invalid IM4M envelope")
    values = [(tag, data[start:end]) for tag, start, end in fields]
    if (values[0] != (0x16, b"IM4M") or values[1] != (2, b"\0")
            or [f[0] for f in values[2:]] != [0x31, 4, 0x30]
            or not all(f[1] for f in values[2:])):
        raise ValueError("Invalid IM4M identity, body, signature or certificate envelope")


def retag_im4p(data, payload_type=None):
    if not 16 <= len(data) <= COMPONENT_LIMIT:
        raise ValueError("Component must be a bounded IM4P")
    fields = der_fields(data)
    if (len(fields) < 4 or [f[0] for f in fields[:4]] != [0x16, 0x16, 0x16, 4]
            or data[fields[0][1]:fields[0][2]] != b"IM4P"
            or fields[1][2] - fields[1][1] != 4 or fields[3][1] == fields[3][2]):
        raise ValueError("Invalid IM4P identity, payload tag or data")
    start, end = fields[1][1:]
    original = data[start:end]
    if payload_type is not None and not isinstance(payload_type, str):
        raise ValueError("Image4 payload type must be a string")
    tag = original if payload_type is None else payload_type.encode("ascii")
    if len(tag) != 4 or any(c < 32 or c > 126 for c in tag):
        raise ValueError("Image4 payload type must be four printable ASCII bytes")
    return data[:start] + tag + data[end:], original.decode("ascii"), tag.decode("ascii")


def der_wrap(tag, data):
    size = len(data)
    if size >= 1 << 32:
        raise ValueError("Image4 object exceeds its size limit")
    length = bytes([size]) if size < 128 else size.to_bytes((size.bit_length() + 7) // 8, "big")
    if size >= 128:
        length = bytes([128 | len(length)]) + length
    return bytes([tag]) + length + data


def assemble_img4(im4p, im4m):
    validate_im4m(im4m)
    retag_im4p(im4p)  # Validate without changing any component field.
    return der_wrap(0x30, der_wrap(0x16, b"IMG4") + im4p + der_wrap(0xa0, im4m))


def read_component(root, relative, limit=COMPONENT_LIMIT):
    """Read a regular file beneath root without following package symlinks."""
    if not isinstance(relative, str) or "\\" in relative or ":" in relative or "\0" in relative:
        raise ValueError("Invalid firmware component path")
    parts = relative.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError("Firmware paths must stay inside their bundle")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
                raise ValueError("Firmware component must be a bounded nonempty regular file")
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or len(data) != before.st_size):
                raise ValueError("Firmware component changed while being read")
            return data
    finally:
        os.close(directory)


class FirmwareBundle:
    def __init__(self, root, identity_index, overrides=None):
        self.root = Path(root).resolve(strict=True)
        manifest_data = read_component(self.root, "BuildManifest.plist", 16 * MANIFEST_LIMIT)
        manifest = plistlib.loads(manifest_data)
        if not isinstance(manifest, dict):
            raise ValueError("BuildManifest must be a dictionary")
        identities = manifest.get("BuildIdentities")
        if (not isinstance(identities, list) or type(identity_index) is not int
                or not 0 <= identity_index < len(identities)):
            raise ValueError("Select an existing BuildManifest identity index")
        self.identity = identities[identity_index]
        if not isinstance(self.identity, dict) or not isinstance(self.identity.get("Info"), dict):
            raise ValueError("Build identity must contain an Info dictionary")
        info = self.identity.get("Info", {})
        def number(value):
            if type(value) is int:
                return value
            if isinstance(value, str):
                return int(value, 0)
            raise ValueError("Invalid numeric build identity property")
        if (info.get("DeviceClass") != "vphone600ap"
                or number(self.identity.get("ApChipID", "0")) != 0xfe01
                or number(self.identity.get("ApBoardID", "0")) != 0x91):
            raise ValueError("This firmware packager supports the vphone600 identity only")
        self.components = self.identity.get("Manifest", {})
        if (not isinstance(self.components, dict) or not self.components or
                any(not isinstance(entry, dict) or not isinstance(entry.get("Info"), dict)
                    for entry in self.components.values())):
            raise ValueError("Build identity must contain component dictionaries")
        self.overrides = dict(overrides or {})
        if set(self.overrides) - self.components.keys():
            raise ValueError("Component override is absent from the selected identity")
        self.version = {"product_version": manifest.get("ProductVersion"),
                        "build_version": manifest.get("ProductBuildVersion"),
                        "build_manifest_sha256": hashlib.sha256(manifest_data).hexdigest(),
                        "identity_index": identity_index, "variant": info.get("Variant")}

    def build_identity_request(self, arguments):
        """Return the selected local identity, with the original restored keys."""
        if not isinstance(arguments, dict):
            raise ValueError("BuildIdentityDict Arguments must be a dictionary")
        recovery = arguments.get("IsRecoveryOS", False)
        overrides = arguments.get("BuildIdentityApplyOverrides", False)
        variant = arguments.get("Variant", "Erase")
        if type(recovery) is not bool or recovery:
            raise ValueError("BuildIdentityDict recoveryOS identity is not configured")
        if type(overrides) is not bool or not isinstance(variant, str) or not 1 <= len(variant) <= 128:
            raise ValueError("Invalid BuildIdentityDict variant or override flag")
        # File overrides already match the original component digest; never
        # leak their host paths into the guest's firmware build identity.
        return {"BuildIdentityDict": plistlib.loads(plistlib.dumps(self.identity)), "Variant": variant}

    def source_component(self, name):
        entry = self.components.get(name)
        if not isinstance(entry, dict) or not isinstance(entry.get("Info"), dict):
            raise ValueError(f"Build identity has no component {name}")
        if name in getattr(self, "userland_sources", {}):
            root, relative = self.userland_sources[name]
            data = read_component(root, relative)
            path = root / relative
        elif name in self.overrides:
            path = Path(self.overrides[name]).absolute()
            # An explicit user override chooses its directory; a package path
            # itself never gets to escape the firmware bundle.
            data = read_component(path.parent, path.name)
        else:
            relative = entry["Info"].get("Path")
            data = read_component(self.root, relative)
            path = self.root / relative
        im4p, original_tag, tag = retag_im4p(data, entry["Info"].get("Img4PayloadType"))
        expected = entry.get("Digest")
        if not isinstance(expected, bytes) or len(expected) != 48:
            raise ValueError(f"{name} has no supported SHA-384 component digest")
        digest = hashlib.sha384(im4p).digest()
        if digest != expected:
            raise ValueError(f"{name} does not match the selected BuildManifest digest")
        return data, im4p, {"component": name, "source": str(path), "source_bytes": len(data),
                       "source_sha256": hashlib.sha256(data).hexdigest(),
                       "original_payload_type": original_tag, "payload_type": tag,
                       "manifest_digest_matched": True, "payload_sha384": digest.hex(),
                       "signature_verified": False, "device_binding_verified": False}

    def image(self, name, ticket):
        _source, im4p, record = self.source_component(name)
        image = assemble_img4(im4p, ticket)
        record.update(image_bytes=len(image), image_sha256=hashlib.sha256(image).hexdigest())
        return image, record

    def nor(self, ticket, flash_version_1=True):
        if type(flash_version_1) is not bool:
            raise ValueError("FlashVersion1 selection must be a boolean")
        names = [name for name, entry in self.components.items()
                 if entry.get("Info", {}).get("IsFirmwarePayload") is True or
                 (entry.get("Info", {}).get("IsSecondaryFirmwarePayload") is True and
                  entry.get("Info", {}).get("IsLoadedByiBoot") is True)]
        if "LLB" not in self.components or "iBoot" not in names:
            raise ValueError("Build identity has no complete NOR firmware list")
        images, records, total = {}, [], 0
        required = list(dict.fromkeys(["LLB", *names, *[name for name in
                         ("RestoreSEP", "SEP", "SepStage1") if name in self.components]]))
        for name in required:
            image, record = self.image(name, ticket)
            total += len(image)
            if total > NOR_LIMIT:
                raise ValueError("NOR data exceeds its aggregate size limit")
            images[name] = image
            records.append(record)
        nor_names = [name for name in names if name not in ("LLB", "RestoreSEP")]
        if flash_version_1:
            nor = {name: images[name] for name in nor_names}
        else:
            nor_names.sort(key=lambda name: not name.startswith("iBoot"))
            nor = [images[name] for name in nor_names]
        result = {"LlbImageData": images["LLB"], "NorImageData": nor}
        for name, key in (("RestoreSEP", "RestoreSEPImageData"), ("SEP", "SEPImageData"),
                          ("SepStage1", "SEPPatchImageData")):
            if name in images:
                result[key] = images[name]
        return result, records

    def image_request(self, arguments, ticket, data_type="PersonalizedData"):
        """Answer restore image requests using their manifest selection contract."""
        contracts = {"PersonalizedData": ("ImageList", None, "ImageData"),
                     "EANData": ("EANImageList", "IsEarlyAccessFirmware", "EANData"),
                     "FUDData": ("FUDImageList", "IsFUDFirmware", "FUDImageData")}
        if data_type not in contracts:
            raise ValueError("Unsupported firmware image request type")
        if not isinstance(arguments, dict):
            raise ValueError("Image request arguments must be a dictionary")
        list_key, fixed_kind, data_key = contracts[data_type]
        kind = fixed_kind or arguments.get("ImageType")
        want_list = arguments.get(list_key, False)
        name = arguments.get("ImageName")
        if (not isinstance(kind, str) or not kind.startswith("Is") or len(kind) > 128
                or type(want_list) is not bool or (name is not None and not isinstance(name, str))):
            raise ValueError("Invalid ImageType, ImageList or ImageName")
        flags = [entry["Info"].get(kind) for entry in self.components.values()]
        if (fixed_kind is None and not any(type(flag) is bool for flag in flags)) or any(
                flag is not None and type(flag) is not bool for flag in flags):
            raise ValueError("ImageType is not a boolean property in this build identity")
        names = [key for key, entry in self.components.items() if entry["Info"].get(kind) is True]
        if name is not None and (want_list or name not in names):
            raise ValueError("ImageName must select an actual component of the requested type")
        if want_list:
            return {list_key: names}, []
        images, records, total = {}, [], 0
        for key in ([name] if name is not None else names):
            data, record = self.image(key, ticket)
            total += len(data)
            if total > NOR_LIMIT:
                raise ValueError("Image response exceeds its aggregate size limit")
            images[key] = data
            records.append(record)
        if name is not None:
            return {"ImageName": name, data_key: images[name]}, records
        return {data_key: images}, records

    def metadata(self, name, arguments, source_request):
        optional = False
        if name in ("__RestoreVersion__", "__SystemVersion__"):
            relative = "RestoreVersion.plist" if name == "__RestoreVersion__" else "SystemVersion.plist"
        else:
            info = self.identity["Info"]
            variant = arguments.get("Variant") if source_request else info.get("MacOSVariant")
            prefix, suffix = arguments.get("GlobalManifestPrefix", "apticket"), arguments.get("GlobalManifestSuffix", "")
            for value, empty in ((variant, False), (prefix, False), (suffix, True)):
                if (not isinstance(value, str) or len(value) > 128 or value in (".", "..")
                        or not re.fullmatch(r"[A-Za-z0-9._ -]*" if empty else r"[A-Za-z0-9._ -]+", value)):
                    raise ValueError("Invalid global manifest filename arguments")
            optional = arguments.get("GlobalManifestOptional", False)
            if type(optional) is not bool:
                raise ValueError("GlobalManifestOptional must be boolean")
            optional = source_request and (optional or not info.get("VariantSupportsGlobalSigning", False))
            relative = f"Firmware/Manifests/restore/{variant}/{prefix}.{info['DeviceClass']}{suffix}.im4m"
        try:
            data = read_component(self.root, relative, 16 * MANIFEST_LIMIT)
        except FileNotFoundError:
            if not optional:
                raise
            data = b""
        if relative.endswith(".plist") and not isinstance(plistlib.loads(data), dict):
            raise ValueError("Restore version metadata must be a property-list dictionary")
        return data, {"component": name, "source": str(self.root / relative), "source_bytes": len(data),
                      "source_sha256": hashlib.sha256(data).hexdigest(), "optional_absent": not data and optional,
                      "signature_verified": False, "device_binding_verified": False}

    def boot_object_request(self, arguments, ticket, data_type="PersonalizedBootObjectV3"):
        """Stream a selected identity component with the original V3 framing.

        All source/digest validation finishes before the first message is made
        available. RecoveryOS requires its own identity and is not this profile.
        """
        if not isinstance(arguments, dict):
            raise ValueError("Boot object arguments must be a dictionary")
        name = arguments.get("ImageName")
        recovery = arguments.get("IsRecoveryOS", False)
        chunk_bytes = arguments.get("DataChunkSize", 8192)
        metadata_names = ("__GlobalManifest__", "__RestoreVersion__", "__SystemVersion__")
        if not isinstance(name, str) or name not in self.components and name not in metadata_names:
            raise ValueError("Boot object must name a component in the selected identity")
        if data_type not in ("PersonalizedBootObjectV3", "SourceBootObjectV4", "SourceBootObjectV5"):
            raise ValueError("Unsupported boot object protocol")
        if type(recovery) is not bool or recovery:
            raise ValueError("RecoveryOS needs a separate matching build identity")
        if type(chunk_bytes) is not int or not 1 <= chunk_bytes <= FILE_CHUNK_LIMIT:
            raise ValueError("Invalid boot object DataChunkSize")
        if name in metadata_names:
            data, record = self.metadata(name, arguments, data_type != "PersonalizedBootObjectV3")
        elif data_type == "PersonalizedBootObjectV3":
            data, record = self.image(name, ticket)
        else:
            data, _im4p, record = self.source_component(name)
        chunks = (len(data) + chunk_bytes - 1) // chunk_bytes
        if chunks + 1 > FILE_MESSAGE_LIMIT:
            raise ValueError("Boot object exceeds the file-message count limit")
        record.update(chunk_bytes=chunk_bytes, data_messages=chunks,
                      completion_message="FileDataDone", data_type=data_type)

        def messages():
            for offset in range(0, len(data), chunk_bytes):
                yield {"FileData": data[offset:offset + chunk_bytes]}
            yield {"FileDataDone": True}

        return messages(), record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware", type=Path, required=True, help="Local directory containing BuildManifest.plist")
    parser.add_argument("--identity", type=int, required=True, help="Explicit vphone600 BuildIdentity index")
    parser.add_argument("--manifest", type=Path, required=True, help="Existing local DER IM4M; never fetched or signed")
    parser.add_argument("--override", action="append", default=[], metavar="COMPONENT=PATH")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--component", action="append", help="Package selected components as individual IMG4 files")
    mode.add_argument("--nor", action="store_true", help="Require and package the complete NORData response")
    mode.add_argument("--image-request", type=Path,
                      help="Local plist containing restore image Arguments; write the corresponding response")
    mode.add_argument("--boot-object-request", type=Path,
                      help="PersonalizedBootObjectV3 Arguments for a manifest component; write framed FileData plists")
    parser.add_argument("--image-data-type", choices=("PersonalizedData", "EANData", "FUDData"),
                        help="Image request protocol (default: PersonalizedData); requires --image-request")
    parser.add_argument("--legacy-nor-array", action="store_true", help="NOR requests without FlashVersion1")
    parser.add_argument("--output", type=Path, required=True, help="New private output directory")
    args = parser.parse_args(argv)
    if args.legacy_nor_array and not args.nor:
        parser.error("--legacy-nor-array requires --nor")
    if args.image_data_type and not args.image_request:
        parser.error("--image-data-type requires --image-request")
    overrides = {}
    for value in args.override:
        name, separator, path = value.partition("=")
        if not separator or not name or not path or name in overrides:
            parser.error("Overrides must be unique COMPONENT=PATH entries")
        overrides[name] = path
    ticket = read_component(args.manifest.absolute().parent, args.manifest.name, MANIFEST_LIMIT)
    validate_im4m(ticket)
    bundle = FirmwareBundle(args.firmware, args.identity, overrides)
    files, records = {}, []
    if args.nor:
        value, records = bundle.nor(ticket, not args.legacy_nor_array)
        files["nor.plist"] = plistlib.dumps(value, fmt=plistlib.FMT_BINARY)
    elif args.image_request:
        request = read_component(args.image_request.absolute().parent, args.image_request.name, MANIFEST_LIMIT)
        arguments = plistlib.loads(request)
        value, records = bundle.image_request(arguments, ticket, args.image_data_type or "PersonalizedData")
        files["image-response.plist"] = plistlib.dumps(value, fmt=plistlib.FMT_BINARY)
    elif args.boot_object_request:
        request = read_component(args.boot_object_request.absolute().parent, args.boot_object_request.name, MANIFEST_LIMIT)
        arguments = plistlib.loads(request)
        messages, record = bundle.boot_object_request(arguments, ticket)
        records.append(record)
    else:
        if len(set(args.component)) != len(args.component):
            parser.error("Components must be unique")
        for name in args.component:
            if not name or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789,._-" for c in name):
                parser.error("Invalid component name")
            files[name + ".img4"], record = bundle.image(name, ticket)
            records.append(record)
    args.output.mkdir(mode=0o700)
    for name, data in files.items():
        with os.fdopen(os.open(args.output / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as out:
            out.write(data)
    file_receipts = {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                     for name, data in files.items()}
    if args.boot_object_request:
        # A property-list-service stream: each binary plist has its own
        # four-byte network-order length. Do not buffer a second full image.
        name = "boot-object.plist-stream"
        digest, size = hashlib.sha256(), 0
        with os.fdopen(os.open(args.output / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as out:
            for message in messages:
                encoded = plistlib.dumps(message, fmt=plistlib.FMT_BINARY)
                frame = struct.pack("!I", len(encoded)) + encoded
                out.write(frame)
                digest.update(frame)
                size += len(frame)
        file_receipts[name] = {"bytes": size, "sha256": digest.hexdigest()}
    data_type = ("NORData" if args.nor else "PersonalizedBootObjectV3" if args.boot_object_request else
                 (args.image_data_type or "PersonalizedData") if args.image_request else "components")
    report = {"format": 1, **bundle.version, "components": records,
              "data_type": data_type, "files": file_receipts,
              "manifest_sha256": hashlib.sha256(ticket).hexdigest(),
              "nor_response_complete": bool(args.nor), "restore_completed": False,
              "qualified_for_app_development": False}
    if args.image_request or args.boot_object_request:
        report["image_request"] = {"sha256": hashlib.sha256(request).hexdigest(),
                                   "arguments": {key: arguments[key] for key in
                                                 ("ImageType", "ImageList", "EANImageList", "FUDImageList", "ImageName", "DataChunkSize", "IsRecoveryOS")
                                                 if key in arguments and isinstance(arguments[key], (str, bool, int))}}
    (args.output / "firmware.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
