#!/usr/bin/env python3
"""Expose one live vphone guest to standard Linux libusbmuxd clients.

Private Unix socket only; does not replace the system usbmuxd, pair devices,
contact a signing server or initiate a restore. Client traffic reaches the
original guest service, including simultaneous restore/data connections.
"""
import argparse
from collections import deque
import errno
import json
import os
from pathlib import Path
import plistlib
import select
import signal
import socket
import stat
import struct
import tempfile
import threading
import time

from ios_paravirt_duplex import DuplexUSBConnection
from ios_paravirt_mux import MuxConnection, PLIST_LIMIT
from ios_paravirt_usb import USB_ATTACH_TIMEOUT, USBConnection, enumerate_device, wait_for_usb_ready

CLIENT_LIMIT = 32
COMMAND_LIMIT = 65536
QUEUE_LIMIT = PLIST_LIMIT + 65536
ERRORS = (OSError, EOFError, ValueError, RuntimeError)


class SharedConnection(MuxConnection):
    """One TCP stream with its own sequence state on a shared USB mux link."""
    def __init__(self, hub, sport):
        super().__init__(hub.usb, hub.device)
        self.hub, self.sport, self.version = hub, sport, hub.wire.version
        self.packets, self.queued_bytes = deque(), 0
        self.inbox = threading.Condition()
        self.error = None
        self.signal_read, self.signal_write = socket.socketpair()
        self.signal_read.setblocking(False)
        self.signal_write.setblocking(False)

    def _signal(self):
        try:
            self.signal_write.send(b"\x01")
        except (BlockingIOError, OSError):
            pass

    def enqueue(self, packet):
        with self.inbox:
            if self.error:
                return
            if len(self.packets) >= 1024 or self.queued_bytes + len(packet) > QUEUE_LIMIT:
                self.error = ValueError("Mux client receive queue limit exceeded")
            else:
                self.packets.append(packet)
                self.queued_bytes += len(packet)
            self._signal()
            self.inbox.notify_all()

    def fail(self, error):
        with self.inbox:
            self.error = error
            self._signal()
            self.inbox.notify_all()

    def send_packet(self, protocol, data):
        with self.hub.writer:
            if self.hub.error:
                raise self.hub.error
            self.hub.wire.send_packet(protocol, data)

    def receive_packet(self):
        with self.inbox:
            while not self.packets and not self.error:
                remaining = self.usb.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Mux client deadline exceeded")
                self.inbox.wait(remaining)
            if self.error:
                raise self.error
            data = self.packets.popleft()
            self.queued_bytes -= len(data)
            try:
                self.signal_read.recv(1)
            except BlockingIOError:
                pass
            return 6, data

    def close(self):
        try:
            super().close()
        finally:
            with self.hub.lock:
                self.hub.channels.pop(self.sport, None)
            self.signal_read.close()
            self.signal_write.close()


class MuxHub:
    def __init__(self, usb, device):
        self.usb, self.device = usb, device
        self.lock, self.writer = threading.Lock(), threading.Lock()
        self.channels, self.next_port, self.error = {}, 1, None
        self.wire = MuxConnection(usb, device)
        self.version = self.wire.negotiate()
        self.reader = threading.Thread(target=self._receive, name="vusb-mux", daemon=True)
        self.reader.start()

    def _receive(self):
        try:
            while True:
                protocol, packet = self.wire.receive_packet()
                if protocol == 1:
                    if packet[:1] == b"\x03":
                        raise RuntimeError("Guest mux error: " + packet[1:512].decode("utf-8", errors="replace"))
                    continue
                if protocol != 6 or len(packet) < 20:
                    raise ValueError("Invalid shared mux TCP packet")
                _source, target = struct.unpack_from("!HH", packet)
                with self.lock:
                    channel = self.channels.get(target)
                    if channel is not None:
                        # TCP validation still belongs to the receiving channel.
                        channel.enqueue(packet)
                    # Late acknowledgements after local close cannot be routed
                    # to another client: source ports are never reused in a run.
        except ERRORS as error:
            self.fail(error)

    def fail(self, error):
        with self.lock:
            if self.error is None:
                self.error = error
            for channel in self.channels.values():
                channel.fail(self.error)

    def open(self, port):
        with self.lock:
            if self.error:
                raise self.error
            if len(self.channels) >= CLIENT_LIMIT or self.next_port > 65535:
                raise RuntimeError("Mux connection capacity exhausted")
            channel = SharedConnection(self, self.next_port)
            self.next_port += 1
            self.channels[channel.sport] = channel
        try:
            channel.connect(port)
            return channel
        except BaseException:
            try:
                channel.close()
            except ERRORS:
                pass
            raise

    def close(self):
        self.fail(EOFError("USB mux bridge closed"))
        self.usb.close()
        self.reader.join(timeout=2)


def read_exact(stream, length):
    data = bytearray()
    while len(data) < length:
        part = stream.recv(length - len(data))
        if not part:
            raise EOFError("Local usbmuxd client closed")
        data.extend(part)
    return bytes(data)


def read_command(stream):
    length, version, kind, tag = struct.unpack("<IIII", read_exact(stream, 16))
    if not 16 <= length <= COMMAND_LIMIT:
        raise ValueError("Invalid usbmuxd command size")
    if version != 1 or kind != 8:
        raise ValueError("Only usbmuxd plist protocol version 1 is supported")
    value = plistlib.loads(read_exact(stream, length - 16))
    if not isinstance(value, dict) or not isinstance(value.get("MessageType"), str):
        raise ValueError("Invalid usbmuxd command dictionary")
    return tag, value


def reply(stream, tag, value):
    payload = plistlib.dumps(value, fmt=plistlib.FMT_XML)
    if len(payload) + 16 > COMMAND_LIMIT:
        raise ValueError("Oversized usbmuxd reply")
    stream.sendall(struct.pack("<IIII", len(payload) + 16, 1, 8, tag) + payload)


def result(stream, tag, number):
    reply(stream, tag, {"MessageType": "Result", "Number": number})


def device_entry(device):
    # Preserve the actual USB descriptor's serial, stripping only its trailing
    # NUL padding. This is a transport identifier, not an authenticated UDID.
    serial = device["strings"].get("serial", "").rstrip("\0")
    if not serial or len(serial) > 256 or any(ord(c) < 32 for c in serial):
        raise ValueError("Guest has no valid USB serial descriptor")
    return {"MessageType": "Attached", "DeviceID": 1, "Properties": {
        "DeviceID": 1, "SerialNumber": serial, "ProductID": device["product_id"],
        "ConnectionType": "USB", "LocationID": 0}}


class Bridge:
    def __init__(self, hub, device):
        self.hub, self.entry = hub, device_entry(device)
        self.lock = threading.Lock()
        self.clients, self.threads = set(), set()
        self.slots = threading.BoundedSemaphore(CLIENT_LIMIT)
        self.commands, self.failures = {}, []
        self.refusals = []
        self.connections = self.to_guest = self.from_guest = 0
        self.stopping = False

    def _relay(self, stream, channel):
        # Exactly one consumer owns a channel's TCP state. USB receive and
        # response dispatch run independently, so an idle IN never blocks OUT.
        while not self.stopping:
            if channel.buffer:
                stream.sendall(channel.buffer)
                with self.lock:
                    self.from_guest += len(channel.buffer)
                channel.buffer.clear()
            if channel.eof:
                return
            if channel.error:
                raise channel.error
            ready, _, _ = select.select([stream, channel.signal_read], [], [], 0.25)
            if channel.signal_read in ready:
                channel.pump()
            if stream in ready:
                data = stream.recv(16384)
                if not data:
                    return
                channel.send(data)
                with self.lock:
                    self.to_guest += len(data)

    def _listen(self, stream, tag):
        result(stream, tag, 0)
        reply(stream, 0, self.entry)
        while not self.stopping and not self.hub.error:
            ready, _, _ = select.select([stream], [], [], 0.25)
            if ready:
                return  # EOF or an invalid command on an event-only stream.
        if not self.stopping:
            reply(stream, 0, {"MessageType": "Detached", "DeviceID": 1})

    def handle(self, stream):
        channel = None
        try:
            # One stalled local client cannot hold its slot beyond 15 seconds;
            # guest operations retain the enclosing session's own deadline.
            stream.settimeout(min(15, max(0.001, self.hub.usb.deadline - time.monotonic())))
            for _ in range(128):
                tag, command = read_command(stream)
                name = command["MessageType"]
                counter = name if name in ("ListDevices", "Listen", "Connect", "ReadPairRecord",
                                           "SavePairRecord", "DeletePairRecord", "ReadBUID") else "unknown"
                with self.lock:
                    self.commands[counter] = self.commands.get(counter, 0) + 1
                if name == "ListDevices":
                    reply(stream, tag, {"DeviceList": [] if self.hub.error else [self.entry]})
                elif name == "Listen":
                    self._listen(stream, tag)
                    return
                elif name == "Connect":
                    port, identifier = command.get("PortNumber"), command.get("DeviceID")
                    if type(identifier) is not int or identifier != 1 or self.hub.error:
                        result(stream, tag, 2)  # BadDevice
                    elif type(port) is not int or not 1 <= port <= 65535:
                        result(stream, tag, 1)  # BadCommand
                    else:
                        try:
                            channel = self.hub.open(socket.ntohs(port))
                        except ConnectionError as error:
                            with self.lock:
                                self.refusals.append({"port": socket.ntohs(port), "error": str(error)[:512]})
                                self.refusals = self.refusals[-32:]
                            result(stream, tag, 3)  # ConnectionRefused, from real SYN/RST
                            return
                        result(stream, tag, 0)  # Only after the real guest SYN/ACK
                        with self.lock:
                            self.connections += 1
                        self._relay(stream, channel)
                        return
                elif name == "ReadPairRecord":
                    result(stream, tag, errno.ENOENT)
                elif name in ("SavePairRecord", "DeletePairRecord", "ReadBUID"):
                    result(stream, tag, errno.ENOTSUP)
                else:
                    result(stream, tag, 1)
            raise ValueError("Local usbmuxd command limit exceeded")
        except EOFError:
            pass
        except ERRORS as error:
            if not self.stopping:
                with self.lock:
                    self.failures.append(str(error)[:512])
                    self.failures = self.failures[-32:]
        finally:
            if channel:
                try:
                    channel.close()
                except ERRORS:
                    pass
            stream.close()
            with self.lock:
                self.clients.discard(stream)
                self.threads.discard(threading.current_thread())
            self.slots.release()

    def accept(self, stream):
        if not self.slots.acquire(blocking=False):
            stream.close()
            return
        thread = threading.Thread(target=self.handle, args=(stream,), name="usbmuxd-client", daemon=True)
        with self.lock:
            self.clients.add(stream)
            self.threads.add(thread)
        thread.start()

    def close(self):
        self.stopping = True
        self.hub.close()
        with self.lock:
            clients, threads = list(self.clients), list(self.threads)
        for stream in clients:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for thread in threads:
            thread.join(timeout=2)

    def snapshot(self):
        with self.lock:
            return {"service_connections": self.connections, "bytes_to_guest": self.to_guest,
                    "bytes_from_guest": self.from_guest, "commands": dict(self.commands),
                    "client_errors": list(self.failures), "service_refusals": list(self.refusals)}


def write_record(directory, record):
    target = directory / "bridge.json"
    fd, temporary = tempfile.mkstemp(prefix=".bridge-", dir=directory)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(record, output, indent=2)
            output.write("\n")
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def connect_guest(request, deadline, progress=None):
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        stream = socket.socket(socket.AF_UNIX)
        # Retry socket availability only. Disconnecting after even the first
        # submitted reset marks this VirtIO device NEEDS_RESET; reconnecting
        # without a guest reset cannot recover it and only hides the failure.
        usb = USBConnection(stream, deadline, first_response_timeout=USB_ATTACH_TIMEOUT)
        try:
            usb._timeout()
            stream.connect(request["usb"]["socket"])
            if progress:
                progress({"attempts": attempts, "stage": "enumerating"})
            device = enumerate_device(usb)
            if progress:
                progress({"attempts": attempts, "stage": "enumerated",
                          "requests_sent": usb.sent, "responses_received": usb.received})
            return usb, device
        except (EOFError, ConnectionError, FileNotFoundError, TimeoutError) as error:
            stream.close()
            if usb.sent or usb.received_bytes:
                if progress:
                    progress({"attempts": attempts, "stage": "failed",
                              "last_error": str(error), "guest_reset_required": True,
                              "requests_sent": usb.sent, "responses_received": usb.received})
                raise
            if progress:
                progress({"attempts": attempts, "stage": "waiting-for-controller",
                          "last_error": str(error), "requests_sent": usb.sent,
                          "responses_received": usb.received})
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        except BaseException:
            stream.close()
            raise
    raise TimeoutError("Virtual USB controller never became available")


def load_request(path, deadline, wait=False):
    """Wait for the owning launcher's atomic request, with bounded local input."""
    while True:
        try:
            with path.open("rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or path.is_symlink():
                    raise ValueError("Guest request must be a regular, owned file")
                data = stream.read(1024 * 1024 + 1)
            break
        except FileNotFoundError:
            if not wait or time.monotonic() >= deadline:
                raise
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    if len(data) > 1024 * 1024:
        raise ValueError("Guest request exceeds 1 MiB")
    request = json.loads(data)
    if (not isinstance(request, dict) or not isinstance(request.get("usb"), dict)
            or request["usb"].get("protocol") != "apple-vusb-v1"
            or not isinstance(request["usb"].get("socket"), str)
            or not request["usb"]["socket"] or not isinstance(request.get("engine_sha256"), str)):
        raise ValueError("The session does not expose the Apple virtual USB transport")
    return request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True, help="Live run_ios_paravirt.py --usb request.json")
    parser.add_argument("--output", type=Path, required=True, help="New private directory for bridge.json")
    parser.add_argument("--seconds", type=int, default=180, help="Total lifetime, including USB enumeration")
    parser.add_argument("--wait-for-request", action="store_true",
                        help="Wait within the same deadline for an owned platform job to publish its request")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600:
        parser.error("--seconds must be between 1 and 3600")
    args.output.mkdir(mode=0o700)
    deadline = time.monotonic() + args.seconds
    record = {"format": 1, "request": str(args.request.resolve()),
              "state": "connecting",
              "local_only": True, "physical_devices_exposed": False,
              "pairing_supported": False, "qualified_for_app_development": False}
    write_record(args.output, record)
    usb = hub = bridge = None
    failed = False
    def terminate(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        def progress(state):
            record["connection"] = state
            write_record(args.output, record)
        progress({"stage": "waiting-for-guest-request"})
        request = load_request(args.request, deadline, args.wait_for_request)
        record["engine_sha256"] = request["engine_sha256"]
        progress({"stage": "waiting-for-guest-usb"})
        record["guest_usb_mode"] = wait_for_usb_ready(args.request.parent, deadline)
        record["guest_usb_ready_checkpoint"] = True
        enumerated, device = connect_guest(request, deadline, progress)
        usb = DuplexUSBConnection(enumerated)
        hub = MuxHub(usb, device)
        bridge = Bridge(hub, device)
        # Short path avoids sockaddr_un truncation for deeply nested workspaces.
        with tempfile.TemporaryDirectory(prefix="tomacco-usbmuxd-") as private:
            address = str(Path(private) / "usbmuxd.sock")
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(address)
                os.chmod(address, 0o600)
                listener.listen(CLIENT_LIMIT)
                listener.settimeout(0.25)
                record.update(state="ready", socket=address, device=device,
                              mux_version=hub.version, usbmuxd_protocol=1,
                              environment={"USBMUXD_SOCKET_ADDRESS": "UNIX:" + address})
                write_record(args.output, record)
                print(json.dumps({"state": "ready", "environment": record["environment"]}), flush=True)
                updated = time.monotonic()
                while time.monotonic() < deadline and not hub.error:
                    if time.monotonic() - updated >= 1:
                        record["traffic"] = bridge.snapshot()
                        record["transport"] = {"requests_sent": usb.sent, "responses_received": usb.received}
                        write_record(args.output, record)
                        updated = time.monotonic()
                    try:
                        stream, _ = listener.accept()
                    except TimeoutError:
                        continue
                    bridge.accept(stream)
        record["state"] = "stopped"
        record["reason"] = "guest-disconnected" if isinstance(hub.error, (EOFError, ConnectionError)) else "deadline"
        if hub.error and not isinstance(hub.error, (EOFError, ConnectionError, TimeoutError)):
            raise hub.error
    except KeyboardInterrupt:
        record.update(state="stopped", reason="cancelled")
    except ERRORS as error:
        failed = True
        record.update(state="failed", error=str(error))
    finally:
        if bridge:
            bridge.close()
            record["traffic"] = bridge.snapshot()
        elif hub:
            hub.close()
        elif usb:
            usb.close()
        if usb:
            record["transport"] = {"requests_sent": usb.sent, "responses_received": usb.received}
            usb.stream.close()
        write_record(args.output, record)
    print(json.dumps({k: v for k, v in record.items() if k != "device"}, indent=2))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
