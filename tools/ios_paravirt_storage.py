#!/usr/bin/env python3
"""Create private vphone600 storage and recover its actual NVRAM boot state.

Creates new raw media only. Does not restore iOS, provision security keys,
request signing tickets or modify an existing VM's data.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import uuid
import zlib

from prepare_ios_paravirt import blank_nvram, decode, MAX_TREE_BYTES, validate_nvram

NVRAM_OFFSET = 0xa00000
NVRAM_BANK_BYTES = 0x80000
NVRAM_BANK_COUNT = 2


def initialize_root_gpt(stream, size):
    """Create a GPT APFS container partition; leave its filesystem unformatted.

    Layout matches the 512-byte-sector table written by the original vphone600
    restore: 128 entries and an aligned container from LBA 40 to N-41. No APFS
    superblock, volume, key or successful restore state is manufactured here.
    """
    if size < 1024 * 1024 or size % 4096:
        raise ValueError("GPT root storage must be at least 1 MiB and 4096-byte aligned")
    sectors = size // 512
    disk_guid, partition_guid = uuid.uuid4(), uuid.uuid4()
    first, last = 40, sectors - 41
    entries = bytearray(128 * 128)
    entries[:128] = struct.pack(
        "<16s16sQQQ72s", uuid.UUID("7C3457EF-0000-11AA-AA11-00306543ECAC").bytes_le,
        partition_guid.bytes_le, first, last, 0, "Container".encode("utf-16le"))
    entries_crc = zlib.crc32(entries)

    def header(current, alternate, table):
        data = bytearray(struct.pack(
            "<8sIIIIQQQQ16sQIII", b"EFI PART", 0x10000, 92, 0, 0,
            current, alternate, 34, sectors - 34, disk_guid.bytes_le,
            table, 128, 128, entries_crc))
        struct.pack_into("<I", data, 16, zlib.crc32(data))
        return data.ljust(512, b"\0")

    mbr = bytearray(512)
    mbr[446:462] = struct.pack("<B3sB3sII", 0, b"\0\x02\0", 0xee,
                             b"\xff\xff\xff", 1, min(sectors - 1, 0xffffffff))
    mbr[510:512] = b"\x55\xaa"
    stream.seek(0)
    stream.write(mbr)
    stream.write(header(1, sectors - 1, 2))
    stream.write(entries)
    stream.seek(size - 33 * 512)
    stream.write(entries)
    stream.write(header(sectors - 1, 1, sectors - 33))
    return {"scheme": "gpt", "sector_bytes": 512, "disk_guid": str(disk_guid),
            "partition_guid": str(partition_guid), "first_lba": first, "last_lba": last,
            "partition_type": "apfs-container", "filesystem_created": False}


def auxiliary_size(tree):
    if b"vphone600ap" not in tree.get("compatible").lower().split(b"\0"):
        raise ValueError("Storage layout requires a vphone600 device tree")
    regions = tree.child("product").child("util").get("regions")
    if not regions or len(regions) % 12 or len(regions) > 64 * 12:
        raise ValueError("Invalid auxiliary storage region table")
    spans, identifiers = [], set()
    nvram = None
    for kind, start, length in struct.iter_unpack("<III", regions):
        if (kind in identifiers or not length or (start | length) % 512 or
                start + length > 1024 * 1024 * 1024):
            raise ValueError("Invalid auxiliary storage region extent")
        identifiers.add(kind)
        spans.append((start, start + length))
        if kind == 2:
            nvram = (start, length)
    spans.sort()
    if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
        raise ValueError("Overlapping auxiliary storage regions")
    if nvram != (NVRAM_OFFSET, NVRAM_BANK_BYTES * NVRAM_BANK_COUNT):
        raise ValueError("Unsupported vphone600 NVRAM region")
    return spans[-1][1]


def nvram_handoff(tree, storage):
    """Read validated banks from locked media; never reuse stale boot variables."""
    required = auxiliary_size(tree)
    info = os.fstat(storage.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_size < required:
        raise ValueError(f"Auxiliary disk must cover all device-tree regions ({required} bytes)")
    banks = []
    for index in range(NVRAM_BANK_COUNT):
        data = os.pread(storage.fileno(), NVRAM_BANK_BYTES,
                        NVRAM_OFFSET + index * NVRAM_BANK_BYTES)
        try:
            validate_nvram(data)
        except ValueError:
            continue  # A torn inactive bank does not discard the valid one.
        banks.append((struct.unpack_from("<I", data, 20)[0], index, data))
    if not banks:
        raise ValueError("Auxiliary disk has no valid CHRP NVRAM bank; create new media with ios_paravirt_storage.py")
    generation, index, data = max(banks, key=lambda bank: bank[:2])
    chosen = tree.child("chosen")
    for name, value in (("nvram-bank-size", NVRAM_BANK_BYTES),
                        ("nvram-total-size", NVRAM_BANK_BYTES),
                        ("nvram-bank-count", NVRAM_BANK_COUNT),
                        ("nvram-current-bank", index)):
        chosen.set(name, struct.pack("<I", value))
    chosen.set("nvram-proxy-data", data)
    return {"bank": index, "generation": generation, "bank_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def create_storage(tree, output, root_bytes, root_layout="blank"):
    size = auxiliary_size(tree)
    if root_bytes < 512 or root_bytes % 512 or root_bytes > 1024 * 1024 * 1024 * 1024:
        raise ValueError("Root storage must be sector-aligned and at most 1 TiB")
    if root_layout not in ("blank", "restore-container"):
        raise ValueError("Unknown root storage layout")
    if root_layout == "restore-container" and (root_bytes < 1024 * 1024 or root_bytes % 4096):
        raise ValueError("GPT root storage must be at least 1 MiB and 4096-byte aligned")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    created = []
    root_partition = None
    try:
        for name, length in (("auxiliary.raw", size), ("system.raw", root_bytes), ("sep.raw", 512 * 1024)):
            path = output / name
            with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600), "w+b") as stream:
                created.append(path)
                stream.truncate(length)  # Sparse images, not large zero buffers.
                if name == "auxiliary.raw":
                    stream.seek(NVRAM_OFFSET)
                    for generation in (1, 0):
                        stream.write(blank_nvram(NVRAM_BANK_BYTES, generation))
                elif name == "system.raw" and root_layout == "restore-container":
                    root_partition = initialize_root_gpt(stream, length)
                stream.flush()
                os.fsync(stream.fileno())
        return {"format": 1, "auxiliary_bytes": size, "root_bytes": root_bytes,
                "sep_bytes": 512 * 1024, "nvram_bank_count": NVRAM_BANK_COUNT,
                "nvram_bank_bytes": NVRAM_BANK_BYTES, "root_layout": root_layout,
                "root_partition": root_partition, "restored": False}
    except BaseException:
        for path in reversed(created):
            path.unlink()
        output.rmdir()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-tree", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New private storage directory")
    parser.add_argument("--root-gib", type=int, default=32)
    parser.add_argument("--root-layout", choices=("blank", "restore-container"), default="blank",
                        help="Experimental restore-container creates a real GPT partition; iOS must format APFS itself")
    args = parser.parse_args()
    if not 1 <= args.root_gib <= 1024:
        parser.error("--root-gib must be between 1 and 1024")
    with args.device_tree.open("rb") as stream:
        data = stream.read(MAX_TREE_BYTES + 1)
    record = create_storage(decode(data), args.output.absolute(), args.root_gib << 30, args.root_layout)
    record["device_tree_sha256"] = hashlib.sha256(data).hexdigest()
    with (args.output / "storage.json").open("x") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"output": str(args.output.absolute()), **record}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
