"""Bounded ASR v1 system-image transfer for the private vphone USB bridge.

The wire contract follows libimobiledevice/idevicerestore's ASR implementation.
This module transfers an existing image; it neither formats host devices nor
claims that sending the payload installs, seals or boots the guest filesystem.
"""
import hashlib
import os
from pathlib import Path
import plistlib
import socket
import stat
import struct
import time
from xml.parsers.expat import ExpatError

CHUNK_SIZE = 131072
XML_LIMIT = 65536
IMAGE_LIMIT = 64 << 30
COMMAND_LIMIT = 4096


def parse_plist(data, **kwargs):
    if data.lstrip().startswith(b"<") and b"<!ENTITY" in data.upper():
        raise ValueError("Restore XML entities are not supported")
    try:
        return plistlib.loads(data, **kwargs)
    except (plistlib.InvalidFileException, ExpatError, ValueError, OverflowError,
            RecursionError) as error:
        raise ValueError("Malformed restore property list") from error


class Wire:
    """A socket with a total deadline, including partial reads and writes."""
    def __init__(self, stream, deadline):
        self.stream, self.deadline = stream, deadline
        self.sent = self.received = 0

    def timeout(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Restore transport deadline exceeded")
        self.stream.settimeout(remaining)

    def read(self, length):
        self.timeout()
        data = self.stream.recv(length)
        if not data:
            raise EOFError("Guest closed the restore connection")
        self.received += len(data)
        return data

    def exact(self, length):
        data = bytearray()
        while len(data) < length:
            data.extend(self.read(length - len(data)))
        return bytes(data)

    def send(self, data):
        # Advance only by bytes accepted by the socket. Never replay a chunk
        # after an error: a prefix might already have reached the guest.
        data = memoryview(data)
        while data:
            self.timeout()
            written = self.stream.send(data)
            if written <= 0:
                raise EOFError("Guest closed while receiving restore data")
            self.sent += written
            data = data[written:]


def connect_service(address, port, deadline):
    """Connect to device 1 on an explicitly selected local usbmuxd socket."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid guest service port")
    stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    wire = Wire(stream, deadline)
    try:
        wire.timeout()
        stream.connect(str(address))
        payload = plistlib.dumps({"MessageType": "Connect", "DeviceID": 1,
                                  "PortNumber": socket.htons(port),
                                  "ClientVersionString": "Tomacco"})
        wire.send(struct.pack("<IIII", len(payload) + 16, 1, 8, 1) + payload)
        size, version, kind, tag = struct.unpack("<IIII", wire.exact(16))
        if not 16 < size <= XML_LIMIT or (version, kind, tag) != (1, 8, 1):
            raise ValueError("Invalid private usbmuxd response")
        response = parse_plist(wire.exact(size - 16))
        if (not isinstance(response, dict) or response.get("MessageType") != "Result"
                or type(response.get("Number")) is not int):
            raise ValueError("Missing private usbmuxd connection result")
        if response["Number"]:
            raise ConnectionRefusedError(f"Guest service {port} refused: {response['Number']}")
        return wire
    except BaseException:
        stream.close()
        raise


class VerifiedImage:
    """Keep the exact, digest-checked regular file open through the transfer."""
    def __init__(self, path, expected_sha384, deadline):
        if not isinstance(expected_sha384, bytes) or len(expected_sha384) != 48:
            raise ValueError("OS needs a SHA-384 digest from the selected BuildManifest")
        self.path = Path(path).absolute()
        self.fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= IMAGE_LIMIT:
                raise ValueError("System image must be a bounded, nonempty regular file")
            self.size = info.st_size
            self.identity = self._identity(info)
            self.deadline = deadline
            sha256, sha384 = hashlib.sha256(), hashlib.sha384()
            for data in self.range(0, self.size):
                sha256.update(data)
                sha384.update(data)
            self.assert_unchanged()
            if sha384.digest() != expected_sha384:
                raise ValueError("System image does not match the BuildManifest OS digest")
            self.sha384 = expected_sha384
            self.record = {"path": str(self.path), "bytes": self.size,
                           "sha256": sha256.hexdigest(), "sha384": sha384.hexdigest(),
                           "manifest_digest_matched": True, "signature_verified": False}
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def assert_unchanged(self):
        if self._identity(os.fstat(self.fd)) != self.identity:
            raise ValueError("System image changed during the restore")

    def range(self, offset, length):
        if (type(offset) is not int or type(length) is not int or offset < 0
                or length < 0 or offset > self.size or length > self.size - offset):
            raise ValueError("ASR requested bytes outside the system image")
        self.assert_unchanged()
        while length:
            if time.monotonic() >= self.deadline:
                raise TimeoutError("System image deadline exceeded")
            # A short filesystem read must not shift ASR's checksum boundaries.
            size = min(CHUNK_SIZE, length)
            data = bytearray()
            while len(data) < size:
                if time.monotonic() >= self.deadline:
                    raise TimeoutError("System image deadline exceeded")
                part = os.pread(self.fd, size - len(data), offset + len(data))
                if not part:
                    raise EOFError("System image was truncated")
                data.extend(part)
            offset += len(data)
            length -= len(data)
            yield bytes(data)
        self.assert_unchanged()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class ASRTransfer:
    def __init__(self, wire, image, progress=None):
        self.wire, self.image, self.progress = wire, image, progress
        self.buffer = bytearray()
        self.checksums = False
        self.record = {"state": "waiting-for-initiate", "payload_sent": False,
                       "restore_completed": False, "payload_bytes": 0,
                       "checksum_bytes": 0, "validation_bytes": 0,
                       "validation_requests": 0, "initiations": 0}

    def receive(self):
        # ASR uses raw XML, unlike restored's four-byte length-prefixed plists.
        # Retain coalesced documents and accept fragmentation at any byte.
        self.buffer = self.buffer.lstrip()
        while True:
            end = self.buffer.find(b"</plist>")
            if end >= 0:
                end += len(b"</plist>")
                if end > XML_LIMIT:
                    raise ValueError("ASR XML command exceeds its size limit")
                data = bytes(self.buffer[:end]).lstrip()
                del self.buffer[:end]
                value = parse_plist(data, fmt=plistlib.FMT_XML)
                if not isinstance(value, dict) or not isinstance(value.get("Command"), str):
                    raise ValueError("ASR command must be a dictionary with Command")
                return value
            if len(self.buffer) >= XML_LIMIT:
                raise ValueError("ASR XML command exceeds its size limit")
            self.buffer.extend(self.wire.read(min(8192, XML_LIMIT - len(self.buffer))))

    def initiate(self, request):
        checksums = request.get("Checksum Chunks", self.checksums)
        if request.get("Command") != "Initiate" or type(checksums) is not bool:
            raise ValueError("Expected ASR Initiate with boolean Checksum Chunks")
        self.checksums = checksums
        info = {"Version": 1, "Stream ID": 1, "Payload": {"Port": 1, "Size": self.image.size},
                "FEC Slice Stride": 40, "Packets Per FEC": 25, "Packet Payload Size": 1450}
        if checksums:
            info["Checksum Chunk Size"] = CHUNK_SIZE
        self.wire.send(plistlib.dumps(info, fmt=plistlib.FMT_XML))
        self.record.update(state="validating", checksum_chunks=checksums)
        self.record["initiations"] += 1

    def notify(self):
        if self.progress:
            self.progress(dict(self.record))

    def run(self):
        try:
            self.initiate(self.receive())
            self.notify()
            for _ in range(COMMAND_LIMIT):
                request = self.receive()
                command = request["Command"]
                if command == "Initiate":
                    self.initiate(request)
                elif command == "OOBData":
                    offset, length = request.get("OOB Offset"), request.get("OOB Length")
                    if (type(length) is not int or length <= 0
                            or self.record["validation_bytes"] + length > 2 * self.image.size):
                        raise ValueError("ASR validation byte budget exceeded or invalid length")
                    for data in self.image.range(offset, length):
                        self.wire.send(data)
                        self.record["validation_bytes"] += len(data)
                    self.record["validation_requests"] += 1
                elif command == "Payload":
                    self.send_payload()
                    return self.record
                else:
                    raise ValueError(f"Unsupported ASR command: {command[:128]}")
                self.notify()
            raise ValueError("ASR validation command limit exceeded")
        except BaseException as error:
            self.record.update(state="failed", error=str(error), wire_bytes_sent=self.wire.sent)
            self.notify()
            raise

    def send_payload(self):
        self.record["state"] = "sending-payload"
        self.notify()
        digest = hashlib.sha384()
        notified = time.monotonic()
        for data in self.image.range(0, self.image.size):
            digest.update(data)
            self.wire.send(data)
            self.record["payload_bytes"] += len(data)
            if self.checksums:
                # SHA-1 is mandated by ASR's chunk protocol, not used as a
                # firmware authenticity check. OS identity uses SHA-384.
                self.wire.send(hashlib.sha1(data, usedforsecurity=False).digest())
                self.record["checksum_bytes"] += 20
            if time.monotonic() - notified >= 0.5:
                self.notify()
                notified = time.monotonic()
        if digest.digest() != self.image.sha384:
            raise ValueError("Transferred system image digest changed")
        self.record.update(state="payload-sent", payload_sent=True, wire_bytes_sent=self.wire.sent,
                           payload_sha384=digest.hexdigest())
        self.notify()
