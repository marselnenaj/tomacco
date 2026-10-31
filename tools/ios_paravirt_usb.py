#!/usr/bin/env python3
"""Talk to a running vphone600 guest's actual virtual USB controller.

This is the Apple VirtIO device protocol, not a USB/IP server. Enumeration
reads the original guest's descriptors; it does not install or restore iOS.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import time

MAX_FRAME = 128 * 1024
USB_READY_MARKER = b"enabling USB finished: success (0)"
SYSTEM_USB_READY_MARKER = b"USB configuration complete!"
USB_ATTACH_TIMEOUT = 5.0
USB_ATTACH_RECOVERY = 1.0
USB_RESET_RECOVERY = 0.050
USB_ADDRESS_RECOVERY = 0.010
USB_CONTROL_RECOVERY = 0.050


def wait_for_usb_ready(session, deadline):
    """Wait for original restored or ConfigureUSB to enable its controller.

    VirtIO DRIVER_OK precedes the guest controller's power-on. Reset requests
    sent during that gap can be dropped by the original driver. The bounded
    launcher's UART checkpoint is the measured profile's attach indication;
    it does not assert restored, pairing or installed-system readiness.
    """
    session = Path(session)
    offset, tail = 0, b""
    markers = {USB_READY_MARKER: "restore", SYSTEM_USB_READY_MARKER: "system"}
    keep = max(map(len, markers))
    while time.monotonic() < deadline:
        try:
            with (session / "uart.log").open("rb") as uart:
                uart.seek(offset)
                chunk = uart.read(65536)
            if chunk:
                offset += len(chunk)
                if offset > 64 * 1024 * 1024:
                    raise ValueError("USB readiness log exceeds its observation bound")
                for marker, mode in markers.items():
                    if marker in tail + chunk:
                        return mode
                tail = (tail + chunk)[-keep:]
                continue
        except FileNotFoundError:
            pass
        if (session / "result.json").exists():
            raise RuntimeError("Guest stopped before reporting USB enablement")
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    raise TimeoutError("Guest did not report USB enablement before the deadline")


def request_frame(kind, endpoint=0, payload=b"", transfer_length=None):
    if not 0 <= endpoint <= 255 or endpoint & 0x70:
        raise ValueError("Invalid USB endpoint")
    length = len(payload) if transfer_length is None else transfer_length
    if not 0 <= length <= MAX_FRAME - 6 or len(payload) > MAX_FRAME - 6:
        raise ValueError("USB transfer exceeds the transport limit")
    if kind in (2, 3):
        valid = not payload and not length and (kind == 3 or endpoint == 0)
    elif kind == 1 and endpoint == 0:
        valid = (len(payload) >= 8 and length == len(payload) and
                 len(payload) == 8 + (0 if payload[0] & 0x80 else struct.unpack_from("<H", payload, 6)[0]))
    elif kind == 1 and endpoint != 0x80:
        valid = not payload if endpoint & 0x80 else length == len(payload)
    else:
        valid = False
    if not valid:
        raise ValueError("Invalid Apple virtual USB request")
    frame = struct.pack("<IBB", length, endpoint, kind) + payload
    return struct.pack("<I", len(frame)) + frame


class USBStall(RuntimeError):
    pass


class USBConnection:
    """One outstanding request; stream framing and deadlines survive short I/O."""
    def __init__(self, stream, deadline, *, first_response_timeout=None):
        self.stream, self.deadline = stream, deadline
        self.first_response_deadline = (deadline if first_response_timeout is None else
                                        min(deadline, time.monotonic() + first_response_timeout))
        self.sent = self.received = 0
        self.received_bytes = 0
        self.events = []

    def _timeout(self):
        deadline = self.deadline if self.received_bytes else min(self.deadline, self.first_response_deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Virtual USB operation deadline exceeded")
        self.stream.settimeout(remaining)

    def _read(self, length):
        data = bytearray()
        while len(data) < length:
            self._timeout()
            part = self.stream.recv(length - len(data))
            if not part:
                raise EOFError("Virtual USB host stream closed")
            # Even a partial header proves the guest has started responding.
            # A later timeout/EOF must not cause another reset and enumeration.
            self.received_bytes += len(part)
            data.extend(part)
        return bytes(data)

    def response(self):
        length, = struct.unpack("<I", self._read(4))
        if not 2 <= length <= MAX_FRAME:
            raise ValueError("Invalid guest USB response length")
        data = self._read(length)
        self.received += 1
        kind, endpoint = data[:2]
        if kind not in (1, 2, 3, 4, 5) or (kind != 1 and len(data) != 2):
            raise ValueError("Invalid guest USB response")
        return kind, endpoint, data[2:]

    def exchange(self, kind, endpoint=0, payload=b"", transfer_length=None, expected=1):
        self._timeout()
        self.stream.sendall(request_frame(kind, endpoint, payload, transfer_length))
        self.sent += 1
        for _ in range(64):
            response, target, data = self.response()
            if response == 3:
                # Original driver reports an endpoint abort independently of
                # the current control request. Keep a bounded event record.
                self.events.append({"kind": response, "endpoint": target})
                self.events = self.events[-64:]
                continue
            if response == 2 and target == endpoint:
                raise USBStall(f"Guest stalled endpoint {endpoint:#04x}")
            if response != expected or target != (0x10 if kind == 2 else endpoint):
                raise ValueError(f"Unexpected USB response {response} on endpoint {target:#04x}")
            return data
        raise ValueError("Guest USB event limit exceeded")

    def reset(self):
        return self.exchange(2, expected=4)

    def recover(self, seconds):
        # USB reset/SET_ADDRESS completion precedes the recovery interval.
        # Like Linux's hub driver, leave time for the device's new state to
        # settle before sending another setup packet. Never extend the job.
        if self.deadline - time.monotonic() < seconds:
            raise TimeoutError("Virtual USB recovery exceeds the operation deadline")
        time.sleep(seconds)
        self._timeout()

    def control(self, request_type, request, value=0, index=0, length=0, payload=b""):
        if not (0 <= request_type <= 255 and 0 <= request <= 255 and
                all(0 <= n <= 65535 for n in (value, index, length))):
            raise ValueError("Invalid USB setup packet")
        if payload and request_type & 0x80 or (not request_type & 0x80 and len(payload) != length):
            raise ValueError("USB control data does not match its setup packet")
        setup = struct.pack("<BBHHH", request_type, request, value, index, length)
        data = self.exchange(1, payload=setup + payload)
        if len(data) > length or (not request_type & 0x80 and data):
            raise ValueError("Guest returned too much control data")
        # PCC under TCG can post the response before its control endpoint is
        # ready for the next setup packet (observed at SET_ADDRESS). Keep this
        # board's bounded recovery on control transfers only, never bulk I/O.
        self.recover(USB_CONTROL_RECOVERY)
        return data

    def descriptor(self, kind, index=0, length=255, language=0):
        data = self.control(0x80, 6, kind << 8 | index, language, length)
        if len(data) < 2 or data[1] != kind or data[0] < 2:
            raise ValueError("Guest returned an invalid USB descriptor")
        return data

    def bulk_in(self, endpoint, length):
        if not endpoint & 0x80 or not endpoint & 15:
            raise ValueError("An IN data endpoint is required")
        data = self.exchange(1, endpoint, transfer_length=length)
        if len(data) > length:
            raise ValueError("Guest exceeded the requested bulk transfer length")
        return data

    def bulk_out(self, endpoint, data):
        if endpoint & 0x80 or not endpoint & 15:
            raise ValueError("An OUT data endpoint is required")
        if self.exchange(1, endpoint, data):
            raise ValueError("Unexpected payload in bulk OUT acknowledgement")


def parse_configuration(data):
    if len(data) < 9 or data[:2] != b"\x09\x02" or struct.unpack_from("<H", data, 2)[0] != len(data):
        raise ValueError("Incomplete USB configuration descriptor")
    result = {"value": data[5], "interfaces": [], "raw_hex": data.hex()}
    offset, interface = 0, None
    while offset < len(data):
        if offset + 2 > len(data):
            raise ValueError("Truncated USB descriptor header")
        length, kind = data[offset:offset + 2]
        if length < 2 or offset + length > len(data):
            raise ValueError("Invalid USB descriptor extent")
        part = data[offset:offset + length]
        if kind == 4:
            if length < 9:
                raise ValueError("Short USB interface descriptor")
            interface = {"number": part[2], "alternate": part[3], "class": part[5],
                         "subclass": part[6], "protocol": part[7], "endpoints": []}
            result["interfaces"].append(interface)
        elif kind == 5:
            if length < 7 or interface is None:
                raise ValueError("Invalid USB endpoint descriptor")
            interface["endpoints"].append({"address": part[2], "attributes": part[3],
                                           "max_packet_size": struct.unpack_from("<H", part, 4)[0],
                                           "interval": part[6]})
        offset += length
    return result


def enumerate_device(usb):
    # A newly attached host must leave the controller time to finish its
    # power-on/attach transition before reset. The original PCC ConfigureUSB
    # checkpoint precedes completion under TCG; the USB minimum of 100 ms was
    # insufficient for immediate agent attachment on this platform.
    usb.recover(USB_ATTACH_RECOVERY)
    usb.reset()
    usb.recover(USB_RESET_RECOVERY)
    device = usb.descriptor(1, length=18)
    if len(device) != 18 or device[0] != 18 or not device[17]:
        raise ValueError("Incomplete USB device descriptor")
    # SET_ADDRESS is part of real USB enumeration, even though this transport
    # carries endpoint numbers instead of physical bus addresses.
    usb.control(0, 5, value=1)
    usb.recover(USB_ADDRESS_RECOVERY)
    result = {"device_descriptor_hex": device.hex(),
              "vendor_id": struct.unpack_from("<H", device, 8)[0],
              "product_id": struct.unpack_from("<H", device, 10)[0],
              "configurations": [], "strings": {}}
    for index in range(device[17]):
        header = usb.descriptor(2, index, length=9)
        if len(header) != 9:
            raise ValueError("Short USB configuration header")
        total, = struct.unpack_from("<H", header, 2)
        if total < 9:
            raise ValueError("Invalid USB configuration length")
        result["configurations"].append(parse_configuration(usb.descriptor(2, index, total)))
    if any(device[14:17]):
        languages = usb.descriptor(3, length=255)
        if len(languages) < 4 or languages[0] != len(languages) or len(languages) % 2:
            raise ValueError("Invalid USB language descriptor")
        language, = struct.unpack_from("<H", languages, 2)
        for name, index in zip(("manufacturer", "product", "serial"), device[14:17]):
            if index:
                descriptor = usb.descriptor(3, index, language=language)
                if len(descriptor) != descriptor[0] or len(descriptor) % 2:
                    raise ValueError("Invalid USB string descriptor")
                result["strings"][name] = descriptor[2:].decode("utf-16-le", errors="strict")
    configuration = result["configurations"][0]["value"]
    usb.control(0, 9, value=configuration)
    configured = usb.control(0x80, 8, length=1)
    if configured != bytes([configuration]):
        raise ValueError("Guest did not confirm the selected USB configuration")
    result["active_configuration"] = configuration
    result["descriptor_sha256"] = hashlib.sha256(device + b"".join(
        bytes.fromhex(c["raw_hex"]) for c in result["configurations"])).hexdigest()
    result["requests_sent"], result["responses_received"] = usb.sent, usb.received
    result["endpoint_events"] = usb.events
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True, help="Live run_ios_paravirt.py --usb request.json")
    parser.add_argument("--output", type=Path, required=True, help="New private enumeration receipt")
    parser.add_argument("--seconds", type=int, default=60, help="Total connection and enumeration deadline")
    parser.add_argument("--hold", action="store_true", help="Retain connection until guest stops or the deadline expires")
    parser.add_argument("--query-restore", action="store_true", help="Read the original restored service's type over USB mux; does not start a restore")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600:
        parser.error("--seconds must be between 1 and 3600")
    request = json.loads(args.request.read_text())
    if request.get("usb", {}).get("protocol") != "apple-vusb-v1":
        parser.error("The session does not expose the Apple virtual USB transport")
    deadline = time.monotonic() + args.seconds
    record = {"format": 1, "request": str(args.request.resolve()),
              "engine_sha256": request["engine_sha256"], "enumerated": False,
              "qualified_for_app_development": False}
    # Reserve the result before touching the guest; never overwrite old evidence.
    with os.fdopen(os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as output:
        usb = None
        try:
            record["guest_usb_mode"] = wait_for_usb_ready(args.request.parent, deadline)
            record["guest_usb_ready_checkpoint"] = True
            # Driver initialization can reset the device before DRIVER_OK.
            # Retry only socket availability; a sent request cannot be replayed
            # across a disconnect without resetting the VirtIO device.
            while True:
                try:
                    with socket.socket(socket.AF_UNIX) as stream:
                        usb = USBConnection(stream, deadline, first_response_timeout=USB_ATTACH_TIMEOUT)
                        usb._timeout()
                        stream.connect(request["usb"]["socket"])
                        record["device"] = enumerate_device(usb)
                        record["enumerated"] = True
                        if args.query_restore:
                            from ios_paravirt_mux import query_restore
                            record["restore_service"] = query_restore(usb, record["device"])
                        record["transport"] = {"requests_sent": usb.sent, "responses_received": usb.received}
                        output.write(json.dumps(record, indent=2) + "\n")
                        output.flush()
                        if args.hold:
                            while time.monotonic() < deadline:
                                usb._timeout()
                                try:
                                    if not stream.recv(4096):
                                        break
                                except TimeoutError:
                                    break
                        break
                except (EOFError, ConnectionError, FileNotFoundError, TimeoutError):
                    if usb.sent or usb.received_bytes:
                        record["guest_reset_required"] = True
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError("USB controller never became available") from None
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        except (OSError, EOFError, ValueError, RuntimeError) as error:
            record["error"] = str(error)
            record["transport"] = {"requests_sent": usb.sent if usb else 0,
                                   "responses_received": usb.received if usb else 0}
            output.seek(0)
            output.truncate()
            output.write(json.dumps(record, indent=2) + "\n")
            raise SystemExit(str(error)) from error
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
