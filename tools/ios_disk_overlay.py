#!/usr/bin/env python3
"""Copy-free root disks for the virtual iPhone: a qcow2 overlay on the base.

A start used to copy the base run's 32 GiB root disk. With overlays, the new
run's root disk is an empty qcow2 file whose backing file is the base's root
disk (raw, or itself an overlay); the guest's writes land in the overlay and
the base is only read. The auxiliary and SEP disks are small and stay copies.

This module decides how the next root disk is made (plan), builds the
qemu-img command for it, follows a backing chain without opening anything for
writing, and reads a chain for the host's APFS inspection (Qcow2Chain). It
reads qcow2 headers itself and needs no QEMU to do so.

A chain grows by one overlay per start. At MAX_CHAIN layers the next start
flattens it into a new raw disk instead (qemu-img convert), which writes the
whole disk once, as the copy did. Backing names are absolute paths: the
engine and the helper open the overlay through /proc/self/fd, against which a
relative name would not resolve. Moving a profile breaks its overlays.
"""
import fcntl
import os
from pathlib import Path
import shutil
import struct

QCOW2_MAGIC = b"QFI\xfb"
# Overlays on the base before the next start flattens the chain into raw.
MAX_CHAIN = 8
ROOT_RAW, ROOT_OVERLAY = "system.raw", "system.qcow2"
COPIED_DISKS = ("auxiliary.raw", "sep.raw")
# Incompatible features this reader understands: dirty (bit 0) and the
# compression type field (bit 3, refused per cluster if a cluster is
# compressed). Corrupt (1), an external data file (2) and extended L2
# entries (4) are refused.
_KNOWN_INCOMPATIBLE = 0b1001
_OFFSET_MASK = 0x00FF_FFFF_FFFF_FE00


def qcow2_header(source):
    """The qcow2 header of a path or descriptor, or None when it is not qcow2.

    Raises ValueError for a qcow2 file this module cannot follow."""
    if isinstance(source, (str, Path)):
        with open(source, "rb") as stream:
            return qcow2_header(stream.fileno())
    head = os.pread(source, 104, 0)
    if len(head) < 4 or head[:4] != QCOW2_MAGIC:
        return None
    if len(head) < 72:
        raise ValueError("Truncated qcow2 header")
    (version, backing_offset, backing_size, cluster_bits, size, crypt,
     l1_size, l1_offset) = struct.unpack_from(">IQIIQIIQ", head, 4)
    if version not in (2, 3):
        raise ValueError(f"Unsupported qcow2 version {version}")
    incompatible, header_length = 0, 72
    if version == 3:
        if len(head) < 104:
            raise ValueError("Truncated qcow2 v3 header")
        incompatible = struct.unpack_from(">Q", head, 72)[0]
        header_length = struct.unpack_from(">I", head, 100)[0]
    if crypt:
        raise ValueError("Encrypted qcow2 images are not supported")
    if incompatible & ~_KNOWN_INCOMPATIBLE:
        raise ValueError(f"qcow2 incompatible features {incompatible:#x} are not supported")
    if not 9 <= cluster_bits <= 21:
        raise ValueError("Invalid qcow2 cluster size")
    backing = None
    if backing_offset:
        if not 0 < backing_size <= 1023:
            raise ValueError("Invalid qcow2 backing file name")
        name = os.pread(source, backing_size, backing_offset)
        if len(name) != backing_size:
            raise ValueError("Truncated qcow2 backing file name")
        backing = name.decode("utf-8")
    return {"version": version, "size": size, "cluster_bits": cluster_bits, "l1_size": l1_size,
            "l1_offset": l1_offset, "backing": backing, "backing_format": _backing_format(source, version, header_length),
            "dirty": bool(incompatible & 1)}


def _backing_format(source, version, header_length):
    """The backing format named in the header extensions (raw, qcow2) or None."""
    offset = header_length if version == 3 else 72
    for _ in range(64):
        head = os.pread(source, 8, offset)
        if len(head) < 8:
            return None
        kind, length = struct.unpack(">II", head)
        if kind == 0:
            return None
        if kind == 0xE2792ACA:
            return os.pread(source, length, offset + 8).decode("ascii", "replace")
        offset += 8 + (length + 7) // 8 * 8
    return None


def root_disk(install):
    """The root disk of an install: its overlay when it has one, else raw."""
    install = Path(install)
    return install / ROOT_OVERLAY if (install / ROOT_OVERLAY).is_file() else install / ROOT_RAW


def backing_chain(path, limit=MAX_CHAIN + 1):
    """The files a root disk reads, itself first and the raw disk last.

    Each backing name must be absolute and name a regular file; a loop or a
    chain longer than limit is refused, as is a backing format other than raw
    or qcow2."""
    chain, current = [], Path(path)
    while True:
        if current in chain:
            raise ValueError(f"The backing chain of {path} loops at {current}")
        if not current.is_file() or current.is_symlink():
            raise ValueError(f"Backing file {current} of {path} is missing or not a regular file")
        chain.append(current)
        header = qcow2_header(current)
        if header is None or header["backing"] is None:
            return chain
        if len(chain) > limit:
            raise ValueError(f"The backing chain of {path} has more than {limit} layers")
        backing = Path(header["backing"])
        if not backing.is_absolute():
            raise ValueError(f"{current} names its backing file {backing} by a relative path")
        if header["backing_format"] not in (None, "raw", "qcow2"):
            raise ValueError(f"{current} has a backing file of format {header['backing_format']}")
        current = backing


def installs_backing(install):
    """The install directories whose files the root disk of `install` reads
    (not including itself). Empty for a raw disk or an unreadable chain."""
    try:
        chain = backing_chain(root_disk(install))
    except (OSError, ValueError):
        return []
    return [layer.parent for layer in chain[1:]]


def find_qemu_img(helper_engine=None, which=None):
    """qemu-img beside the helper engine (built with it), else on PATH."""
    if helper_engine:
        candidate = Path(helper_engine).parent / "qemu-img"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return (which or shutil.which)("qemu-img")


def start_disks(profile, qemu_img):
    """How starts make their root disk: profile.json's start_disks when it
    names copy or overlay, else overlay when a qemu-img was found and copy
    without one."""
    named = profile.get("start_disks")
    if named in ("copy", "overlay"):
        return named
    return "overlay" if qemu_img else "copy"


def plan(base, output, mode="copy", qemu_img=None, max_chain=MAX_CHAIN):
    """How the next start makes its root disk from the base's root disk `base`.

    mode "overlay" asks for an overlay; "copy" for an independent disk. The
    result's method is overlay, copy (the existing reflink or stream copy of
    a raw base) or flatten (qemu-img convert of a qcow2 base into raw), with
    the reason, the base's chain, the target and the command when one is run."""
    base, output = Path(base), Path(output)
    chain = backing_chain(base)
    header = qcow2_header(base)
    result = {"base": str(base), "base_chain": [str(layer) for layer in chain]}
    if mode == "overlay" and qemu_img and len(chain) <= max_chain:
        size = header["size"] if header else base.stat().st_size
        target = output / ROOT_OVERLAY
        return {**result, "method": "overlay", "target": str(target), "depth": len(chain) + 1,
                "reason": "an overlay on the base's root disk",
                "command": overlay_command(qemu_img, base, "qcow2" if header else "raw", size, target)}
    if mode == "overlay" and not qemu_img:
        reason = "no qemu-img beside the helper engine or on PATH"
    elif mode == "overlay":
        reason = f"the base's chain has {len(chain)} layers, the most is {max_chain}"
    else:
        reason = "the profile asks for copies"
    if header is not None:
        if not qemu_img:
            raise ValueError(f"The base's root disk {base} is an overlay; flattening it needs qemu-img, "
                             "which is neither beside the helper engine nor on PATH")
        target = output / ROOT_RAW
        return {**result, "method": "flatten", "target": str(target), "reason": reason,
                "command": [str(qemu_img), "convert", "-q", "-f", "qcow2", "-O", "raw", str(base), str(target)]}
    return {**result, "method": "copy", "target": str(output / ROOT_RAW), "reason": reason, "command": None}


def overlay_command(qemu_img, backing, backing_format, size, target):
    """An empty qcow2 overlay of `size` bytes on an absolute backing file.
    -u: qemu-img does not open the backing file, which the start holds locked
    and which must never be written."""
    backing = Path(backing)
    if not backing.is_absolute():
        raise ValueError("An overlay's backing file must be an absolute path")
    return [str(qemu_img), "create", "-q", "-f", "qcow2", "-u", "-F", backing_format, "-b", str(backing),
            "-o", "cluster_size=65536", str(target), str(size)]


class Qcow2Chain:
    """Read-only view of a root disk through its backing chain.

    pread and size, as ios_apfs_layout.inspect uses them. A compressed
    cluster is refused; zero clusters read as zeros; reads past a shorter
    backing file read as zeros."""

    def __init__(self, path, lock=False):
        """With lock, each layer is held with a shared flock, so a layer a
        running guest holds is refused and none can be taken while read."""
        self.files = []
        try:
            for layer in backing_chain(path):
                stream = open(layer, "rb")
                self.files.append((stream, None))
                if lock:
                    try:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise ValueError(f"{layer} is in use by a running guest") from None
                self.files[-1] = (stream, qcow2_header(stream.fileno()))
        except BaseException:
            self.close()
            raise
        top = self.files[0][1]
        self._size = top["size"] if top else os.fstat(self.files[0][0].fileno()).st_size
        self._l1 = {}

    def close(self):
        for stream, _header in self.files:
            stream.close()
        self.files = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def fileno(self):
        return self.files[0][0].fileno()

    def size(self):
        return self._size

    def pread(self, length, offset):
        length = max(0, min(length, self._size - offset))
        out = bytearray()
        while len(out) < length:
            out += self._read(0, offset + len(out), length - len(out))
        return bytes(out)

    def _l1_table(self, index):
        if index not in self._l1:
            stream, header = self.files[index]
            data = os.pread(stream.fileno(), header["l1_size"] * 8, header["l1_offset"])
            if len(data) != header["l1_size"] * 8:
                raise ValueError("Truncated qcow2 L1 table")
            self._l1[index] = struct.unpack(f">{header['l1_size']}Q", data)
        return self._l1[index]

    def _read(self, index, offset, length):
        """Up to `length` bytes at `offset` from layer `index`, within one cluster."""
        if index >= len(self.files):
            return bytes(length)
        stream, header = self.files[index]
        if header is None:
            size = os.fstat(stream.fileno()).st_size
            if offset >= size:
                return bytes(length)
            data = os.pread(stream.fileno(), min(length, size - offset), offset)
            return data if data else bytes(min(length, size - offset))
        bits = header["cluster_bits"]
        cluster = 1 << bits
        within = offset & (cluster - 1)
        length = min(length, cluster - within)
        if offset >= header["size"]:
            return bytes(length)
        l2_bits = bits - 3
        l1_index = offset >> (bits + l2_bits)
        l1 = self._l1_table(index)
        table = l1[l1_index] & _OFFSET_MASK if l1_index < len(l1) else 0
        if not table:
            return self._read(index + 1, offset, length)
        l2_index = (offset >> bits) & ((1 << l2_bits) - 1)
        entry = struct.unpack(">Q", os.pread(stream.fileno(), 8, table + l2_index * 8))[0]
        if entry & (1 << 62):
            raise ValueError("Compressed qcow2 clusters are not supported")
        if header["version"] == 3 and entry & 1:
            return bytes(length)
        host = entry & _OFFSET_MASK
        if not host:
            return self._read(index + 1, offset, length)
        data = os.pread(stream.fileno(), length, host + within)
        return data + bytes(length - len(data))
