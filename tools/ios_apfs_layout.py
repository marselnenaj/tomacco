"""Read APFS volume roles from a closed raw/GPT image without mounting it.

Resolves volume objects through the latest checkpoint's container object map.
Every metadata block and the GPT tables are checksummed; no filesystem writes.
Only ordinary, fixed-key container object maps are supported.
"""
import os
import hashlib
import struct
import uuid
import zlib

APFS_GUID = uuid.UUID("7c3457ef-0000-11aa-aa11-00306543ecac").bytes_le


def checked_block(data):
    if len(data) < 4096 or len(data) % 4:
        raise ValueError("Incomplete APFS metadata block")
    first = second = 0
    for (word,) in struct.iter_unpack("<I", data[8:]):
        first += word
        second += first
    mask = 0xffffffff
    low = mask - (first + second) % mask
    high = mask - (first + low) % mask
    if struct.unpack_from("<Q", data)[0] != (high << 32) | low:
        raise ValueError("APFS metadata checksum mismatch")
    return data


def _pread(source, length, offset):
    """A descriptor, or a reader with pread(length, offset) such as an overlay chain."""
    return source.pread(length, offset) if hasattr(source, "pread") else os.pread(source, length, offset)


def _size(source):
    return source.size() if hasattr(source, "size") else os.fstat(source).st_size


def container_extent(fd):
    size = _size(fd)
    prefix = _pread(fd, 1024, 0)
    if prefix[32:36] == b"NXSB":
        return {"offset": 0, "bytes": size, "partition": None}
    header = bytearray(prefix[512:1024])
    if header[:8] != b"EFI PART" or prefix[510:512] != b"\x55\xaa":
        raise ValueError("Expected a raw APFS container or a GPT disk")
    revision, length, checksum, reserved = struct.unpack_from("<IIII", header, 8)
    if revision != 0x10000 or not 92 <= length <= 512 or reserved:
        raise ValueError("Unsupported GPT header")
    struct.pack_into("<I", header, 16, 0)
    if zlib.crc32(header[:length]) != checksum:
        raise ValueError("GPT header checksum mismatch")
    current, alternate, first_usable, last_usable = struct.unpack_from("<QQQQ", header, 24)
    table, count, entry_size, table_crc = struct.unpack_from("<QIII", header, 72)
    if (current != 1 or alternate != size // 512 - 1 or size % 512
            or not 2 <= first_usable <= last_usable < alternate
            or not 1 <= count <= 4096 or not 128 <= entry_size <= 4096 or entry_size % 8
            or not 2 <= table < first_usable or table * 512 + count * entry_size > first_usable * 512):
        raise ValueError("GPT extents are out of bounds")
    entries = _pread(fd, count * entry_size, table * 512)
    if len(entries) != count * entry_size or zlib.crc32(entries) != table_crc:
        raise ValueError("GPT partition table checksum mismatch")
    candidates = []
    for index in range(count):
        entry = entries[index * entry_size:(index + 1) * entry_size]
        if entry[:16] != APFS_GUID:
            continue
        first, last = struct.unpack_from("<QQ", entry, 32)
        if not first_usable <= first <= last <= last_usable:
            raise ValueError("APFS partition exceeds GPT bounds")
        candidates.append({"offset": first * 512, "bytes": (last - first + 1) * 512,
                           "partition": index + 1})
    if len(candidates) != 1:
        raise ValueError("Select a disk with exactly one APFS partition")
    return candidates[0]


def inspect(fd):
    extent = container_extent(fd)
    prefix = _pread(fd, 48, extent["offset"])
    if prefix[32:36] != b"NXSB":
        raise ValueError("APFS container superblock is missing")
    block_size, blocks = struct.unpack_from("<IQ", prefix, 36)
    if block_size not in (4096, 8192, 16384, 32768, 65536) or not 1 <= blocks <= extent["bytes"] // block_size:
        raise ValueError("APFS container geometry exceeds its image")
    cache = {}

    def block(address):
        if type(address) is not int or not 0 <= address < blocks or len(cache) >= 32768:
            raise ValueError("APFS metadata address or read count exceeds bounds")
        if address not in cache:
            data = _pread(fd, block_size, extent["offset"] + address * block_size)
            if len(data) != block_size:
                raise ValueError("Truncated APFS metadata")
            cache[address] = checked_block(data)
        return cache[address]

    nx = block(0)
    count, = struct.unpack_from("<I", nx, 0x68)
    start, = struct.unpack_from("<Q", nx, 0x70)
    if not 1 <= count <= 16384 or not 0 < start <= blocks - count:
        raise ValueError("Unsupported APFS checkpoint descriptor area")
    checkpoints = [(struct.unpack_from("<Q", nx, 16)[0], 0, nx)]
    for address in range(start, start + count):
        raw = _pread(fd, block_size, extent["offset"] + address * block_size)
        if raw[32:36] != b"NXSB":
            continue
        try:
            candidate = block(address)
        except ValueError:
            continue  # Torn inactive checkpoints do not invalidate a closed image.
        if candidate[36:48] == prefix[36:48]:
            checkpoints.append((struct.unpack_from("<Q", candidate, 16)[0], address, candidate))
    xid, checkpoint, nx = max(checkpoints, key=lambda row: row[:2])
    omap = block(struct.unpack_from("<Q", nx, 0xa0)[0])
    if struct.unpack_from("<I", omap, 24)[0] & 0xffff != 11:
        raise ValueError("APFS container object map has the wrong type")
    tree = struct.unpack_from("<Q", omap, 48)[0]

    def resolve(oid, root=tree, transaction=xid):
        address, visited = root, set()
        for _ in range(16):
            if address in visited:
                raise ValueError("APFS object map contains a cycle")
            visited.add(address)
            node = block(address)
            flags, level, keys = struct.unpack_from("<HHI", node, 32)
            table_offset, table_length = struct.unpack_from("<HH", node, 40)
            table_start = 56 + table_offset
            key_base, value_end = table_start + table_length, block_size - (40 if flags & 1 else 0)
            if (not flags & 4 or not 1 <= keys <= block_size // 4
                    or keys * 4 > table_length or key_base > value_end
                    or bool(flags & 2) != (level == 0)):
                raise ValueError("Unsupported APFS object-map node")
            selected, previous = None, None
            for index in range(keys):
                key_offset, value_offset = struct.unpack_from("<HH", node, table_start + index * 4)
                key_start, value_start = key_base + key_offset, value_end - value_offset
                value_size = 16 if level == 0 else 8
                if not key_base <= key_start <= value_end - 16 or not key_base <= value_start <= value_end - value_size:
                    raise ValueError("APFS object-map entry exceeds its node")
                key = struct.unpack_from("<QQ", node, key_start)
                if previous is not None and key <= previous:
                    raise ValueError("APFS object-map keys are not strictly ordered")
                previous = key
                if key <= (oid, transaction):
                    selected = (key, value_start)
            if selected is None:
                raise ValueError("APFS object was not found in its map")
            key, offset = selected
            if level:
                address, = struct.unpack_from("<Q", node, offset)
                continue
            flags, size, address = struct.unpack_from("<IIQ", node, offset)
            if key[0] != oid or flags & 1 or size != block_size:
                raise ValueError("Missing, deleted or unsupported APFS volume object")
            return address
        raise ValueError("APFS object-map depth exceeds bounds")

    volumes = []
    for index in range(100):
        oid = struct.unpack_from("<Q", nx, 0xb8 + index * 8)[0]
        if not oid:
            continue
        address = resolve(oid)
        volume = block(address)
        if volume[32:36] != b"APSB" or struct.unpack_from("<Q", volume, 8)[0] != oid:
            raise ValueError("APFS volume does not match its object-map entry")
        features, = struct.unpack_from("<Q", volume, 56)
        integrity = None
        integrity_oid = struct.unpack_from("<Q", volume, 0x400)[0]
        if integrity_oid:
            volume_omap = block(struct.unpack_from("<Q", volume, 0x80)[0])
            if struct.unpack_from("<I", volume_omap, 24)[0] & 0xffff != 11:
                raise ValueError("APFS volume object map has the wrong type")
            volume_tree = struct.unpack_from("<Q", volume_omap, 48)[0]
            metadata = block(resolve(integrity_oid, volume_tree, struct.unpack_from("<Q", volume, 16)[0]))
            if struct.unpack_from("<I", metadata, 24)[0] & 0xffff != 0x1e:
                raise ValueError("APFS integrity metadata has the wrong type")
            version, flags, algorithm, offset = struct.unpack_from("<4I", metadata, 32)
            hash_bytes = {1: 32, 2: 32, 3: 48, 4: 64}.get(algorithm)
            if version not in (1, 2) or hash_bytes is None or not 128 <= offset <= block_size - hash_bytes:
                raise ValueError("Unsupported APFS integrity metadata layout")
            integrity = {"version": version, "seal_broken": bool(flags & 1),
                         "hash_type": algorithm, "root_hash": metadata[offset:offset + hash_bytes].hex()}
        volumes.append({"index": index, "oid": oid, "block": address, "integrity": integrity,
                        "superblock_sha256": hashlib.sha256(volume).hexdigest(),
                        "name": volume[0x2c0:0x3c0].split(b"\0", 1)[0].decode("utf-8", errors="strict"),
                        "uuid": str(uuid.UUID(bytes=volume[0xf0:0x100])),
                        "role": struct.unpack_from("<H", volume, 0x3c4)[0],
                        "sealed": bool(features & 0x20),
                        "snapshots": struct.unpack_from("<Q", volume, 0xd8)[0]})
    return {**extent, "block_size": block_size, "blocks": blocks,
            "checkpoint": checkpoint, "xid": xid, "volumes": volumes,
            "metadata_checksums_verified": True}
