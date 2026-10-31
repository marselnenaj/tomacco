#!/usr/bin/env python3
"""What installing an app on a running virtual iPhone needs from the host.

Two decisions keep an executable from running: its cdhash must be in the trust
cache, and installd must admit its ad-hoc signature. A boot's trust cache lies in
guest RAM as one sorted module, so a boot reserves spare entries in it (filler
hashes above every real one) and the host later exchanges fillers for real
hashes while the guest is stopped (`trust_cache_update`). The module keeps its
size and its entry count.

The bundle itself goes over a second vsock connection (`receive_bundle`,
guest-host-channel.h): the JSON channel takes 8 KiB per request and starts a
process each time.
"""
import json
import os
from pathlib import Path
import socket
import stat
import struct
import time

from build_ios_guest_tools import LOCAL_PLATFORM_POLICY, cache_entries, extend_trustcache

# Spare entries of a boot. Each deployment takes one per executable.
RESERVED_ENTRIES = 512
FILLER_PREFIX = b"\xff" * 12
# guest-host-channel.h TOMACCO_BUNDLE_PORT and TOMACCO_DEBUG_PORT.
BUNDLE_PORT = 1342
DEBUG_PORT = 1343
CHUNK_BYTES = 1 << 20
BUNDLE_MODES = (0o644, 0o755)


def filler(index):
    return FILLER_PREFIX + struct.pack(">Q", index)


def reserve(module, count=RESERVED_ENTRIES):
    """The module with `count` spare entries. A real cdhash above every filler
    would be a 1 in 2^96 accident; it is refused rather than misread."""
    entries = cache_entries(module)
    if any(value.startswith(FILLER_PREFIX) for value in entries):
        raise ValueError("This trust cache already has spare entries, or a hash that looks like one")
    return extend_trustcache(module, [filler(index) for index in range(count)])


def spare_entries(module):
    return sum(value.startswith(FILLER_PREFIX) for value in cache_entries(module))


def with_hashes(module, hashes):
    """The module with these hashes in the place of as many fillers: same size,
    same count, sorted, its header as it was. Hashes it has already take none."""
    entries = cache_entries(module)
    if any(len(value) != 20 or value.startswith(FILLER_PREFIX) for value in hashes):
        raise ValueError("Expected SHA-256 CodeDirectory hashes")
    added = sorted({value for value in hashes if value not in entries})
    spare = sorted(value for value in entries if value.startswith(FILLER_PREFIX))
    if len(added) > len(spare):
        raise ValueError(f"This boot has {len(spare)} spare trust-cache entries and {len(added)} are needed; "
                         "stop and start the phone")
    for value in spare[-len(added):] if added else []:
        del entries[value]
    for value in added:
        entries[value] = value + LOCAL_PLATFORM_POLICY
    body = b"".join(sorted(entries.values()))
    header = module[:len(module) - len(body)]
    if len(header) != 24:
        raise ValueError("Unexpected trust-cache header")
    return header + body


def bundle_files(bundle):
    """(relative path in the guest, mode, host path) of an .app directory, in
    path order. The name of the bundle is the first component. Only ordinary
    files and directories: the signed copy has nothing else."""
    bundle = Path(bundle)
    rows = []
    for path in sorted(bundle.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{path} is not an ordinary file")
        mode = 0o755 if metadata.st_mode & 0o100 else 0o644
        rows.append((bundle.name + "/" + str(path.relative_to(bundle)), mode, path))
    if not rows:
        raise ValueError(f"{bundle} holds no files")
    return rows


def frame_header(name, mode, size):
    encoded = name.encode()
    if mode not in BUNDLE_MODES:
        raise ValueError(f"Unexpected mode {mode:o} of {name}")
    return struct.pack("<IIQ", len(encoded), mode, size) + encoded


END_OF_BUNDLE = struct.pack("<IIQ", 0, 0, 0)


def send_bundle(stream, bundle):
    """The wire format of receive_bundle. Returns (files, bytes)."""
    files = total = 0
    for name, mode, path in bundle_files(bundle):
        with path.open("rb") as source:
            size = os.fstat(source.fileno()).st_size
            stream.sendall(frame_header(name, mode, size))
            left = size
            while left:
                part = source.read(min(left, CHUNK_BYTES))
                if not part:
                    raise ValueError(f"{path} changed while it was sent")
                stream.sendall(part)
                left -= len(part)
        files += 1
        total += size
    stream.sendall(END_OF_BUNDLE)
    return files, total


def guest_socket(request_path):
    """The host's end of the guest's vsock, from the run's request record."""
    record = json.loads(Path(request_path).read_text())
    path = (record.get("guest_control") or {}).get("socket")
    if not isinstance(path, str) or not path:
        raise RuntimeError("This run has no guest channel")
    return path


def connect_port(path, port, seconds=30, connect=None, pause=2.0):
    """A stream to the guest's vsock port through vhost-device-vsock's Unix
    socket: "CONNECT <port>" answered by "OK <local port>". A listener that is
    not up yet is asked again until the deadline, with a pause: the backend
    answers a port nobody listens on by closing, and starting the guest's worker
    takes it a moment."""
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        stream = (connect or _unix)(path)
        try:
            stream.settimeout(10)
            stream.sendall(f"CONNECT {port}\n".encode())
            reply = b""
            while not reply.endswith(b"\n") and len(reply) < 64:
                part = stream.recv(1)
                if not part:
                    break
                reply += part
            if reply.startswith(b"OK "):
                stream.settimeout(None)
                return stream
            last = reply.decode(errors="replace").strip() or "closed"
        except OSError as error:
            last = str(error)
        stream.close()
        time.sleep(pause)
    raise RuntimeError(f"The guest's port {port} did not answer: {last}")


def _unix(path):
    stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stream.connect(path)
    return stream


def write_private(path, data):
    """A file only this user reads, replaced whole."""
    temporary = Path(str(path) + ".tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600), "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
