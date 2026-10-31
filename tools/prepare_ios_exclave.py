#!/usr/bin/env python3
"""Prepare matching ExclaveCore boot inputs from locally supplied Apple firmware.

Checks the SHA-384 digests in the selected BuildManifest identity, unwraps the
three components, and atomically publishes a new read-only research bundle.
No downloads, source modifications, kernel patches or service substitutes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import shutil
import struct
import subprocess
import tempfile

from prepare_ios_research_runtime import publish

COMPONENTS = {"cl4": "Ap,cL4", "integrity": "Ap,ExclaveOSIntegrityCatalog",
              "trustcache": "Ap,ExclaveOSTrustCache"}
MAX_CONTAINER = 64 * 1024**2
MAX_PAYLOAD = {"cl4": 256 * 1024**2, "integrity": 16 * 1024**2, "trustcache": 16 * 1024**2}


def checksum(path, algorithm="sha256"):
    with path.open("rb") as source:
        return hashlib.file_digest(source, algorithm).hexdigest()


def component_file(root, relative):
    if not isinstance(relative, str) or not relative or "\0" in relative:
        raise ValueError("Invalid Exclave component path")
    part = PurePosixPath(relative)
    if part.is_absolute() or ".." in part.parts or str(part) != relative:
        raise ValueError("Exclave component path escapes firmware")
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file() or not 1 <= path.stat().st_size <= MAX_CONTAINER:
        raise ValueError("Exclave component is missing, oversized or outside firmware")
    return path


def bundle_banks(path):
    """Inspect the bootstrap range table; the engine also validates Mach-O entry.

    Each entry is offset, length, permissions, name. The array begins at 0x270,
    and includes the secure kernel itself. Range names are little-endian FourCCs.
    """
    with path.open("rb") as source:
        header = source.read(0x4000)
    size = path.stat().st_size
    if len(header) != 0x4000 or size % 0x4000 or header[8:20] != b"DNUB\0\0\x04\0\x01\0\0\0":
        raise ValueError("Expected a page-aligned unwrapped ExclaveCore BUND v4 image")
    count, = struct.unpack_from("<I", header, 20)
    if not 4 <= count <= 16:
        raise ValueError("Unsupported ExclaveCore range count")
    ranges, cursor, rx, rw = [], 0, 0, 0
    kernel_text, kernel_data, metadata = 0, 0, 0
    for index in range(count):
        offset, length, flags, name = struct.unpack_from("<QQI4s", header, 0x270 + index * 24)
        if offset != cursor or not length or length % 0x4000 or length > size - cursor:
            raise ValueError("ExclaveCore ranges do not cover the input exactly")
        if flags in (10, 14) and not rw:
            rx += length
        elif flags in (17, 21):
            rw += length
        else:
            raise ValueError("Unsupported ExclaveCore memory permissions/order")
        name = name[::-1].decode("ascii")
        if name == "ktxt":
            if index != 0 or flags != 10 or kernel_text:
                raise ValueError("Invalid ExclaveCore kernel text range")
            kernel_text = length
        elif name == "kdat":
            if flags != 17 or kernel_data or rw != length:
                raise ValueError("Invalid ExclaveCore kernel data range")
            kernel_data = length
        elif name == "ubdl":
            if index != count - 1 or flags != 21 or metadata:
                raise ValueError("Invalid ExclaveCore metadata range")
            metadata = offset
        ranges.append({"name": name, "offset": offset, "bytes": length, "flags": flags})
        cursor += length
    if (cursor != size or not kernel_text or not kernel_data or not metadata or
            kernel_text != struct.unpack_from("<Q", header, 32)[0] or
            kernel_data != struct.unpack_from("<Q", header, 72)[0]):
        raise ValueError("Incomplete ExclaveCore banks")
    return {"rx_bytes": rx, "rw_bytes": rw, "ranges": ranges}


def prepare(firmware, manifest_path, board, ipsw, output):
    firmware = firmware.resolve(strict=True)
    manifest_path = manifest_path.resolve(strict=True)
    ipsw = ipsw.resolve(strict=True)
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Output exists; choose a new Exclave bundle directory")
    if not firmware.is_dir() or not manifest_path.is_file() or manifest_path.stat().st_size > 16 * 1024**2:
        raise ValueError("Missing or oversized firmware manifest")
    if not ipsw.is_file() or not os.access(ipsw, os.X_OK):
        raise ValueError("Supply an executable ipsw tool")
    manifest_bytes = manifest_path.read_bytes()
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    manifest = plistlib.loads(manifest_bytes)
    identities = [row for row in manifest["BuildIdentities"]
                  if row.get("Info", {}).get("DeviceClass") == board and
                  row["Info"].get("Variant") == "Customer Erase Install (IPSW)"]
    if len(identities) != 1 or identities[0].get("ApChipID") != "0x8140":
        raise ValueError("Select one matching T8140 customer erase identity")
    identity = identities[0]
    version, build = manifest.get("ProductVersion"), manifest.get("ProductBuildVersion")
    if not all(isinstance(value, str) and value for value in (board, version, build)):
        raise ValueError("Missing firmware version, build or board")
    sources, inputs = {}, {}
    for name, key in COMPONENTS.items():
        entry = identity["Manifest"][key]
        relative, digest = entry["Info"]["Path"], entry.get("Digest")
        if not isinstance(digest, bytes) or len(digest) != 48:
            raise ValueError("Missing SHA-384 manifest digest for " + key)
        path = component_file(firmware, relative)
        if checksum(path, "sha384") != digest.hex():
            raise ValueError("Firmware manifest digest mismatch: " + key)
        sources[name] = path
        inputs[name] = {"component": key, "manifest_path": relative,
                        "sha384": digest.hex(), "sha256": checksum(path), "bytes": path.stat().st_size}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".tomacco-cl4-", dir=output.parent) as temporary:
        stage = Path(temporary) / "bundle"
        stage.mkdir(mode=0o700)
        outputs = {}
        for name, source in sources.items():
            # Unwrap the checked copy so concurrent source writes cannot switch
            # the content between verification and extraction.
            copied = Path(temporary) / (name + ".im4p")
            shutil.copyfile(source, copied)
            if checksum(copied, "sha384") != inputs[name]["sha384"]:
                raise ValueError("Firmware changed while copying " + name)
            result = subprocess.run([str(ipsw), "img4", "im4p", "extract", str(copied),
                                     "--output", str(stage / name)], capture_output=True, timeout=120)
            if result.returncode:
                raise ValueError("Could not unwrap " + name + ": " + result.stderr.decode(errors="replace")[-1000:])
            target = stage / name
            if target.is_symlink() or not target.is_file() or not 1 <= target.stat().st_size <= MAX_PAYLOAD[name]:
                raise ValueError("Missing or oversized unwrapped " + name)
            target.chmod(0o400)
            outputs[name] = {"path": name, "sha256": checksum(target), "bytes": target.stat().st_size}
        layout = bundle_banks(stage / "cl4")
        cache = (stage / "trustcache").read_bytes()
        if len(cache) < 24 or struct.unpack_from("<I", cache)[0] != 1 or len(cache) != 24 + struct.unpack_from("<I", cache, 20)[0] * 22:
            raise ValueError("Invalid Exclave trust-cache v1 payload")
        with (stage / "integrity").open("rb") as source:
            if source.read(4) != b"iCat":
                raise ValueError("Invalid Exclave integrity catalog")
        if checksum(manifest_path) != manifest_hash or any(checksum(path, "sha384") != inputs[name]["sha384"] for name, path in sources.items()):
            raise ValueError("Firmware inputs changed during preparation")
        record = {"format": 1, "scope": "exclavecore-boot-research", "board": board,
                  "version": version, "build": build, "chip": identity["ApChipID"],
                  "manifest_sha256": manifest_hash, "manifest_digests_verified": True,
                  "inputs_preserved": True, "firmware_modified": False,
                  "secure_services_qualified": False, "graphical_apps_qualified": False,
                  "inputs": inputs, "components": outputs, "layout": layout}
        (stage / "BuildManifest.plist").write_bytes(manifest_bytes)
        (stage / "exclave-research.json").write_text(json.dumps(record, indent=2) + "\n")
        for path in stage.iterdir():
            path.chmod(0o400)
            with path.open("rb") as stream:
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
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--board", default="d47ap")
    parser.add_argument("--ipsw", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.firmware.expanduser(), args.manifest.expanduser(), args.board,
                             args.ipsw.expanduser(), args.output.expanduser()), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, struct.error, subprocess.TimeoutExpired) as error:
        raise SystemExit(str(error))
