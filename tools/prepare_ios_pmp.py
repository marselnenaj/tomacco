#!/usr/bin/env python3
"""Verify locally supplied T8140 PMP firmware and prepare an isolated ARM64 run.

The ELF envelope preserves all Mach-O loadable bytes, addresses, permissions
and the UNIXTHREAD entry. No instructions, controller replies or boot tables
are patched. Neither preparing nor executing this image qualifies PMP services.
"""
import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import struct
import tempfile

from prepare_ios_exclave import checksum, component_file
from prepare_ios_research_runtime import publish

MAX_PAYLOAD = 8 * 1024 * 1024
RAM_BASE, RAM_BYTES = 0x1000000, 0x200000


def im4p_payload(data):
    """Read the uncompressed pmpf IM4P form supplied with T8140 firmware."""
    def item(cursor, end, tag):
        if cursor + 2 > end or data[cursor] != tag:
            raise ValueError("Invalid PMP IM4P DER tag")
        size = data[cursor + 1]
        cursor += 2
        if size & 0x80:
            width = size & 0x7f
            if not 1 <= width <= 4 or cursor + width > end or data[cursor] == 0:
                raise ValueError("Invalid PMP IM4P DER length")
            size = int.from_bytes(data[cursor:cursor + width], "big")
            cursor += width
            if size < 128:
                raise ValueError("Noncanonical PMP IM4P DER length")
        if size > end - cursor:
            raise ValueError("Truncated PMP IM4P")
        return cursor, cursor + size
    begin, end = item(0, len(data), 0x30)
    if end != len(data):
        raise ValueError("Trailing PMP IM4P data")
    for expected in (b"IM4P", b"pmpf"):
        start, begin = item(begin, end, 0x16)
        if data[start:begin] != expected:
            raise ValueError("Expected a PMP pmpf IM4P container")
    _, begin = item(begin, end, 0x16)  # Version description.
    start, finish = item(begin, end, 4)
    if finish != end or data[start:start + 4] != b"\xcf\xfa\xed\xfe":
        raise ValueError("Only uncompressed, unencrypted PMP Mach-O payloads are supported")
    return data[start:finish]


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate PMP record key")
        result[key] = value
    return result


def bundle_file(root, name, limit):
    path = root / name
    if path.is_symlink() or not path.is_file() or not 1 <= path.stat().st_size <= limit:
        raise ValueError("Missing, linked or oversized PMP bundle file: " + name)
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("PMP bundle file exceeded its size limit: " + name)
    return data


def verify(root):
    root = root.resolve(strict=True)
    record = json.loads(bundle_file(root, "pmp-research.json", 1024 * 1024), object_pairs_hook=unique_object)
    if (not isinstance(record, dict) or type(record.get("format")) is not int or record["format"] != 1 or
            record.get("scope") != "isolated-pmp-firmware-research" or
            record.get("chip") != "0x8140" or record.get("instructions_modified") is not False):
        raise ValueError("Invalid PMP preparation record")
    manifest_bytes = bundle_file(root, "BuildManifest.plist", 16 * 1024**2)
    manifest = plistlib.loads(manifest_bytes)
    if (hashlib.sha256(manifest_bytes).hexdigest() != record["manifest_sha256"] or
            manifest["ProductVersion"] != record["version"] or manifest["ProductBuildVersion"] != record["build"]):
        raise ValueError("PMP manifest does not match the prepared bundle")
    identities = [i for i in manifest["BuildIdentities"] if i.get("Info", {}).get("DeviceClass") == record["board"]
                  and i["Info"].get("Variant") == "Customer Erase Install (IPSW)"]
    if len(identities) != 1 or identities[0].get("ApChipID") != "0x8140":
        raise ValueError("Select one matching T8140 customer erase identity")
    component = identities[0]["Manifest"]["PMP"]
    expected = component.get("Digest")
    if not isinstance(expected, bytes) or len(expected) != 48 or record["input"] != {
            "manifest_path": component["Info"]["Path"], "sha384": expected.hex()}:
        raise ValueError("PMP source identity does not match its manifest")
    container = bundle_file(root, "pmp.im4p", 64 * 1024**2)
    payload = bundle_file(root, "pmp", MAX_PAYLOAD)
    executable = bundle_file(root, "pmp.elf", MAX_PAYLOAD + 4096 * 65)
    if hashlib.sha384(container).digest() != expected:
        raise ValueError("PMP container manifest digest mismatch")
    if im4p_payload(container) != payload:
        raise ValueError("PMP payload does not match its verified container")
    for name, filename, data in (("container", "pmp.im4p", container), ("payload", "pmp", payload),
                                 ("executable", "pmp.elf", executable)):
        if record["components"][name] != {"path": filename, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}:
            raise ValueError("PMP component changed: " + name)
    elf, layout = macho_elf(payload)
    if elf != executable or layout != record["layout"]:
        raise ValueError("PMP ELF does not preserve the Mach-O load segments and entry")
    # Qualification is computed by a run, never accepted from an editable receipt.
    record.update(manifest_digests_verified=True, instructions_modified=False,
                  firmware_executed=False, power_services_qualified=False, graphical_apps_qualified=False)
    return record


def macho_elf(data):
    """Bounded MH_PRELOAD/ARM64 loader; input bytes are never modified."""
    if not 32 <= len(data) <= MAX_PAYLOAD:
        raise ValueError("PMP payload must be 32 bytes to 8 MiB")
    magic, cpu, subtype, kind, count, commands_size, flags, reserved = struct.unpack_from("<8I", data)
    if (magic != 0xfeedfacf or cpu != 0x100000c or subtype != 0 or kind != 5 or
            reserved or not 1 <= count <= 64 or not 8 * count <= commands_size <= len(data) - 32):
        raise ValueError("Expected an ARM64 Mach-O MH_PRELOAD image")
    cursor, end, entry, segments = 32, 32 + commands_size, None, []
    for _ in range(count):
        if cursor + 8 > end:
            raise ValueError("Truncated Mach-O command")
        command, size = struct.unpack_from("<II", data, cursor)
        if size < 8 or size % 8 or size > end - cursor:
            raise ValueError("Invalid Mach-O command size")
        if command == 0x19:  # LC_SEGMENT_64
            if size < 72:
                raise ValueError("Truncated Mach-O segment")
            name, address, memory, offset, length, maximum, protection, sections, _ = struct.unpack_from(
                "<16sQQQQiiII", data, cursor + 8)
            if (size != 72 + 80 * sections or length > memory or
                    offset > len(data) or length > len(data) - offset or
                    protection not in (1, 3, 5) or maximum & ~7 or protection & ~maximum):
                raise ValueError("Invalid Mach-O segment size or permissions")
            if memory:
                if (address < RAM_BASE or memory > RAM_BYTES or address - RAM_BASE > RAM_BYTES - memory or
                        address % 4096 or memory % 4096 or (length and offset < end)):
                    raise ValueError("PMP segment is outside the isolated RAM window or unaligned")
                for previous in segments:
                    if (address < previous["address"] + previous["memory_bytes"] and
                            previous["address"] < address + memory):
                        raise ValueError("Overlapping PMP memory segments")
                    if (length and previous["file_bytes"] and offset < previous["file_offset"] + previous["file_bytes"] and
                            previous["file_offset"] < offset + length):
                        raise ValueError("Overlapping PMP file segments")
                segments.append({"name": name.split(b"\0", 1)[0].decode("ascii"), "address": address,
                                 "memory_bytes": memory, "file_offset": offset, "file_bytes": length,
                                 "permissions": protection})
        elif command == 5:  # LC_UNIXTHREAD, ARM_THREAD_STATE64
            if size != 288 or entry is not None or struct.unpack_from("<II", data, cursor + 8) != (6, 68):
                raise ValueError("Expected exactly one ARM64 UNIXTHREAD entry")
            entry = struct.unpack_from("<Q", data, cursor + 16 + 32 * 8)[0]
        elif command not in (2, 0x1b):  # Symbol metadata and UUID only.
            raise ValueError(f"Unsupported PMP load command {command:#x}")
        cursor += size
    if (cursor != end or entry is None or entry % 4 or not segments or
            not any(s["permissions"] & 4 and s["address"] <= entry <= s["address"] + s["file_bytes"] - 4
                    for s in segments)):
        raise ValueError("Missing or invalid executable PMP entry")
    # QEMU's generic loader accepts ELF. File contents remain byte-for-byte
    # identical; zero-fill is expressed by p_memsz, not invented instructions.
    output = bytearray(4096)
    output[:64] = struct.pack("<16sHHIQQQIHHHHHH", b"\x7fELF\x02\x01\x01" + bytes(9),
                             2, 183, 1, entry, 64, 0, 0, 64, 56, len(segments), 0, 0, 0)
    for index, segment in enumerate(segments):
        permission = segment["permissions"]
        elf_flags = ((permission & 1) << 2) | (permission & 2) | ((permission & 4) >> 2)
        offset, length = segment["file_offset"], segment["file_bytes"]
        struct.pack_into("<IIQQQQQQ", output, 64 + index * 56, 1, elf_flags, len(output),
                         segment["address"], segment["address"], length, segment["memory_bytes"], 4096)
        output.extend(data[offset:offset + length])
        output.extend(bytes(-len(output) % 4096))
    return bytes(output), {"entry": entry, "segments": segments,
                           "ram_base": RAM_BASE, "ram_bytes": RAM_BYTES}


def prepare(firmware, manifest_path, board, output, staging=None):
    firmware, manifest_path = (p.resolve(strict=True) for p in (firmware, manifest_path))
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Output exists; choose a new PMP bundle directory")
    if (not firmware.is_dir() or not manifest_path.is_file() or
            not 1 <= manifest_path.stat().st_size <= 16 * 1024**2):
        raise ValueError("Missing or oversized firmware manifest")
    with manifest_path.open("rb") as stream:
        manifest_bytes = stream.read(16 * 1024**2 + 1)
    if len(manifest_bytes) > 16 * 1024**2:
        raise ValueError("Oversized firmware manifest")
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    manifest = plistlib.loads(manifest_bytes)
    identities = [i for i in manifest["BuildIdentities"] if i.get("Info", {}).get("DeviceClass") == board
                  and i["Info"].get("Variant") == "Customer Erase Install (IPSW)"]
    if len(identities) != 1 or identities[0].get("ApChipID") != "0x8140":
        raise ValueError("Select one matching T8140 customer erase identity")
    if not all(isinstance(value, str) and value for value in
               (board, manifest.get("ProductVersion"), manifest.get("ProductBuildVersion"))):
        raise ValueError("Missing firmware version, build or board")
    identity = identities[0]
    entry = identity["Manifest"]["PMP"]
    relative, digest = entry["Info"]["Path"], entry.get("Digest")
    if not isinstance(digest, bytes) or len(digest) != 48:
        raise ValueError("Missing PMP SHA-384 manifest digest")
    source = component_file(firmware, relative)
    if checksum(source, "sha384") != digest.hex():
        raise ValueError("PMP firmware manifest digest mismatch")
    output.parent.mkdir(parents=True, exist_ok=True)
    if staging is not None and (staging.is_symlink() or not staging.is_dir() or any(staging.iterdir()) or
                                staging.resolve().parent != output.parent.resolve()):
        raise ValueError("PMP staging must be an empty sibling of the output")
    context = nullcontext(staging) if staging is not None else tempfile.TemporaryDirectory(prefix=".tomacco-pmp-", dir=output.parent)
    with context as temporary:
        stage = Path(temporary) / "bundle"
        stage.mkdir(mode=0o700)
        copied = stage / "pmp.im4p"
        shutil.copyfile(source, copied)
        if checksum(copied, "sha384") != digest.hex():
            raise ValueError("PMP firmware changed while copying")
        payload = stage / "pmp"
        with copied.open("rb") as stream:
            container = stream.read(64 * 1024**2 + 1)
        if len(container) > 64 * 1024**2 or hashlib.sha384(container).digest() != digest:
            raise ValueError("PMP firmware changed while reading")
        data = im4p_payload(container)
        elf, layout = macho_elf(data)
        payload.write_bytes(data)
        (stage / "pmp.elf").write_bytes(elf)
        (stage / "BuildManifest.plist").write_bytes(manifest_bytes)
        if checksum(manifest_path) != manifest_hash or checksum(source, "sha384") != digest.hex():
            raise ValueError("PMP inputs changed during preparation")
        record = {"format": 1, "scope": "isolated-pmp-firmware-research", "chip": "0x8140", "board": board,
                  "version": manifest["ProductVersion"], "build": manifest["ProductBuildVersion"],
                  "manifest_sha256": manifest_hash, "manifest_digests_verified": True,
                  "input": {"manifest_path": relative, "sha384": digest.hex()},
                  "components": {name: {"path": filename, "sha256": checksum(stage / filename),
                                        "bytes": (stage / filename).stat().st_size}
                                 for name, filename in (("container", "pmp.im4p"), ("payload", "pmp"), ("executable", "pmp.elf"))},
                  "layout": layout, "instructions_modified": False, "inputs_preserved": True,
                  "firmware_executed": False, "power_services_qualified": False, "graphical_apps_qualified": False}
        (stage / "pmp-research.json").write_text(json.dumps(record, indent=2) + "\n")
        for file in stage.iterdir():
            file.chmod(0o400)
            with file.open("rb") as stream:
                os.fsync(stream.fileno())
        descriptor = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        publish(stage, output)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", type=Path, help="Revalidate an existing prepared bundle without executing firmware")
    for name in ("firmware", "manifest", "output"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--board", default="d47ap")
    parser.add_argument("--staging", type=Path)
    args = parser.parse_args()
    try:
        if args.verify:
            if any((args.firmware, args.manifest, args.output, args.staging)):
                parser.error("Use --verify by itself")
            record = verify(args.verify)
        else:
            if not all((args.firmware, args.manifest, args.output)):
                parser.error("Supply --firmware, --manifest and --output")
            record = prepare(args.firmware, args.manifest, args.board, args.output, args.staging)
        print(json.dumps(record, indent=2))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, struct.error) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
