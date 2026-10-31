"""Bounded USB multiplexing connection to a real guest service.

Wire format reference: libimobiledevice/usbmuxd src/device.c. This client uses
one TCP connection on the private virtual USB stream, with actual sequence and
window tracking. It does not impersonate restored or perform a restore.
"""
import plistlib
import struct

MUX_LIMIT = 65536
PLIST_LIMIT = 1024 * 1024


class MuxConnection:
    def __init__(self, usb, device):
        self.usb = usb
        configuration = next(c for c in device["configurations"]
                             if c["value"] == device["active_configuration"])
        candidates = [i for i in configuration["interfaces"] if
                      (i["class"], i["subclass"], i["protocol"], i["alternate"]) == (255, 254, 2, 0)]
        if len(candidates) != 1:
            raise ValueError("Guest has no unique active USB multiplexing interface")
        endpoints = [e["address"] for e in candidates[0]["endpoints"] if e["attributes"] & 3 == 2]
        inputs, outputs = [e for e in endpoints if e & 128], [e for e in endpoints if not e & 128]
        if len(inputs) != 1 or len(outputs) != 1:
            raise ValueError("Guest mux interface must have one bulk endpoint in each direction")
        self.input, self.output = inputs[0], outputs[0]
        self.version = self.mux_tx = 0
        self.mux_rx = 65535
        self.wire = bytearray()
        self.buffer = bytearray()
        self.sport, self.port = 1, None
        self.sequence = self.acknowledged = self.receive_sequence = 0
        self.window = 0
        self.connected = False
        self.eof = False

    def send_packet(self, protocol, data):
        header_size = 16 if self.version == 2 else 8
        if len(data) + header_size > MUX_LIMIT:
            raise ValueError("Mux packet exceeds its transport limit")
        header = struct.pack("!II", protocol, len(data) + header_size)
        if self.version == 2:
            header += struct.pack("!IHH", 0xfeedface, self.mux_tx, self.mux_rx)
        self.usb.bulk_out(self.output, header + data)
        if self.version == 2:
            self.mux_tx = (self.mux_tx + 1) & 65535

    def receive_packet(self):
        header_size = 16 if self.version == 2 else 8
        while True:
            if len(self.wire) >= 8:
                protocol, size = struct.unpack_from("!II", self.wire)
                if not header_size <= size <= MUX_LIMIT:
                    raise ValueError("Invalid mux packet length")
                if len(self.wire) >= size:
                    packet = bytes(self.wire[:size])
                    del self.wire[:size]
                    if self.version == 2:
                        magic, _tx, self.mux_rx = struct.unpack_from("!IHH", packet, 8)
                        # The USB mux magic identifies direction: FACEFACE
                        # from the device, FEEDFACE from the host.
                        if magic != 0xfaceface:
                            raise ValueError(f"Invalid mux protocol magic {magic:#010x}; header={packet[:16].hex()}")
                    return protocol, packet[header_size:]
            data = self.usb.bulk_in(self.input, MUX_LIMIT)
            if len(self.wire) + len(data) > 2 * MUX_LIMIT:
                raise ValueError("Mux reassembly buffer limit exceeded")
            self.wire.extend(data)

    def negotiate(self):
        if self.version or self.port is not None:
            raise ValueError("Mux version negotiation requires a fresh connection")
        self.send_packet(0, struct.pack("!III", 2, 0, 0))
        protocol, data = self.receive_packet()
        if protocol != 0 or len(data) != 12:
            raise ValueError("Invalid mux version reply")
        major, minor, _ = struct.unpack("!III", data)
        if major not in (1, 2):
            raise ValueError(f"Unsupported guest mux version {major}.{minor}")
        self.version = major
        if major == 2:
            self.send_packet(2, b"\x07")
        return {"major": major, "minor": minor}

    def send_tcp(self, flags, data=b""):
        if self.port is None:
            raise ValueError("No guest service selected")
        header = struct.pack("!HHIIBBHHH", self.sport, self.port, self.sequence,
                             self.receive_sequence, 5 << 4, flags, 1024, 0, 0)
        self.send_packet(6, header + data)
        self.sequence = (self.sequence + len(data) + bool(flags & 2)) & 0xffffffff

    def receive_tcp(self):
        for _ in range(64):
            protocol, packet = self.receive_packet()
            if protocol == 1:
                if packet[:1] == b"\x03":
                    raise RuntimeError("Guest mux error: " + packet[1:512].decode("utf-8", errors="replace"))
                continue
            if protocol != 6 or len(packet) < 20:
                raise ValueError("Invalid mux TCP packet")
            source, target, sequence, ack, offset, flags, window, _, _ = struct.unpack_from("!HHIIBBHHH", packet)
            size = (offset >> 4) * 4
            if source != self.port or target != self.sport or not 20 <= size <= len(packet):
                raise ValueError("Unexpected TCP ports or header extent")
            if flags & 4:
                self.connected = False
                raise ConnectionError("Guest reset the mux service connection")
            if not flags & 16 or (ack - self.acknowledged) & 0xffffffff > (self.sequence - self.acknowledged) & 0xffffffff:
                raise ValueError("Invalid guest TCP acknowledgement")
            self.acknowledged, self.window = ack, window << 8
            return sequence, flags, packet[size:]
        raise ValueError("Mux control event limit exceeded")

    def connect(self, port=62078):
        if self.version not in (1, 2) or self.port is not None or not 1 <= port <= 65535:
            raise ValueError("A fresh mux connection and valid service port are required")
        self.port = port
        self.send_tcp(2)
        sequence, flags, data = self.receive_tcp()
        if flags != 18 or self.acknowledged != self.sequence or data:
            raise ValueError("Invalid guest SYN/ACK handshake")
        self.receive_sequence = (sequence + 1) & 0xffffffff
        self.send_tcp(16)
        self.connected = True

    def pump(self):
        sequence, flags, data = self.receive_tcp()
        if flags not in (16, 24, 17, 25) or sequence != self.receive_sequence or self.eof:
            raise ValueError("Out-of-order or unsupported guest TCP segment")
        if len(self.buffer) + len(data) > PLIST_LIMIT + 4:
            raise ValueError("Guest service receive buffer limit exceeded")
        self.buffer.extend(data)
        self.eof = bool(flags & 1)
        self.receive_sequence = (self.receive_sequence + len(data) + self.eof) & 0xffffffff
        if data or self.eof:
            self.send_tcp(16)

    def send(self, data):
        if not self.connected or len(data) > PLIST_LIMIT + 4:
            raise ValueError("Closed connection or oversized service message")
        while data:
            outstanding = (self.sequence - self.acknowledged) & 0xffffffff
            capacity = max(0, self.window - outstanding)
            if not capacity:
                self.pump()
                continue
            chunk = min(len(data), capacity, 16384)
            self.send_tcp(16, data[:chunk])
            data = data[chunk:]

    def read(self, count):
        if not self.connected or not 0 <= count <= PLIST_LIMIT:
            raise ValueError("Closed connection or oversized service read")
        while len(self.buffer) < count:
            if self.eof:
                raise EOFError("Guest closed the mux service stream")
            self.pump()
        data = bytes(self.buffer[:count])
        del self.buffer[:count]
        return data

    def query(self, request):
        self.send_plist(request)
        return self.receive_plist()

    def send_plist(self, request):
        payload = plistlib.dumps(request, fmt=plistlib.FMT_XML)
        self.send(struct.pack("!I", len(payload)) + payload)

    def receive_plist(self):
        size, = struct.unpack("!I", self.read(4))
        if not 0 < size <= PLIST_LIMIT:
            raise ValueError("Invalid guest property-list length")
        response = plistlib.loads(self.read(size))
        if not isinstance(response, dict):
            raise ValueError("Guest service reply is not a dictionary")
        return response

    def close(self):
        if self.connected:
            try:
                self.send_tcp(4)
            finally:
                self.connected = False


def query_restore(usb, device):
    mux = MuxConnection(usb, device)
    version = mux.negotiate()
    mux.connect()
    try:
        reply = mux.query({"Label": "Tomacco", "Request": "QueryType"})
        if reply.get("Type") != "com.apple.mobile.restored" or type(reply.get("RestoreProtocolVersion")) is not int:
            raise ValueError("Guest did not identify a real restored service")
        result = {"mux_version": version, "port": mux.port,
                  "type": reply["Type"], "protocol_version": reply["RestoreProtocolVersion"],
                  "restore_started": False}
    except BaseException:
        try:
            mux.close()
        except (OSError, EOFError, RuntimeError, ValueError):
            pass  # Keep the original guest/protocol failure as the reported cause.
        raise
    else:
        mux.close()
        return result
