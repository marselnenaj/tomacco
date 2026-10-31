"""Protocol fixtures only; native-client/live-firmware evidence is separate.

The fake USB peer deliberately leaves IN outstanding while waiting for OUT,
and echoes distinct streams. This exercises the duplex deadlock and routing
boundaries without representing a working iOS service or installed system.
"""
from concurrent.futures import ThreadPoolExecutor
from collections import deque
import errno
import json
from pathlib import Path
import plistlib
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from ios_paravirt_usb import USBConnection, USB_READY_MARKER, SYSTEM_USB_READY_MARKER, wait_for_usb_ready
from ios_paravirt_duplex import DuplexUSBConnection
from ios_paravirt_usbmuxd import Bridge, MuxHub, connect_guest, load_request, read_exact, reply
from test_ios_paravirt_mux import DEVICE, VERSION, mux_packet
from test_ios_paravirt_usb import FragmentedStream, response


class EchoUSB:
    def __init__(self):
        self.host, self.peer = socket.socketpair()
        self.peer.settimeout(5)
        self.pending_in = False
        self.queue, self.channels = deque(), {}
        self.errors = []
        self.stop = False
        self.out_with_pending_in = 0
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def response(self, endpoint, data=b""):
        frame = bytes([1, endpoint]) + data
        self.peer.sendall(struct.pack("<I", len(frame)) + frame)

    def deliver(self, data):
        self.queue.append(data)
        if self.pending_in:
            self.pending_in = False
            self.response(129, self.queue.popleft())

    def run(self):
        try:
            while not self.stop:
                length, = struct.unpack("<I", read_exact(self.peer, 4))
                frame = read_exact(self.peer, length)
                _, endpoint, kind = struct.unpack_from("<IBB", frame)
                assert kind == 1
                if endpoint == 129:
                    assert not self.pending_in
                    if self.queue:
                        self.response(129, self.queue.popleft())
                    else:
                        self.pending_in = True
                    continue
                assert endpoint == 1
                self.out_with_pending_in += self.pending_in
                self.response(1)
                data = frame[6:]
                protocol, size = struct.unpack_from("!II", data)
                assert size == len(data)
                if protocol == 0:
                    self.deliver(VERSION)
                elif protocol == 6:
                    src, dst, seq, ack, offset, flags, window, _, _ = struct.unpack_from("!HHIIBBHHH", data, 16)
                    payload = data[16 + (offset >> 4) * 4:]
                    if flags == 2:
                        self.channels[src] = 100
                        response_flags = 4 if dst == 9 else 18
                        packet = struct.pack("!HHIIBBHHH", dst, src, 100, seq + 1,
                                             0x50, response_flags, 1024, 0, 0)
                        self.channels[src] += 1
                        self.deliver(mux_packet(6, packet))
                    elif payload:
                        packet = struct.pack("!HHIIBBHHH", dst, src, self.channels[src],
                                             seq + len(payload), 0x50, 24, 1024, 0, 0)
                        self.channels[src] += len(payload)
                        self.deliver(mux_packet(6, packet + payload))
        except (EOFError, OSError) as error:
            if not self.stop:
                self.errors.append(error)
        except BaseException as error:
            self.errors.append(error)

    def close(self):
        self.stop = True
        for stream in (self.host, self.peer):
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.thread.join(2)
        self.host.close()
        self.peer.close()


def read_reply(stream):
    size, version, kind, tag = struct.unpack("<IIII", read_exact(stream, 16))
    assert version == 1 and kind == 8
    return tag, plistlib.loads(read_exact(stream, size - 16))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.peer = EchoUSB()
        self.usb = DuplexUSBConnection(USBConnection(self.peer.host, time.monotonic() + 10))
        self.device = dict(DEVICE, product_id=0x12ac, strings={"serial": "VM-USB-SERIAL\0\0"})
        self.hub = MuxHub(self.usb, self.device)
        self.bridge = Bridge(self.hub, self.device)
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.peer.stop = True
        self.bridge.close()
        self.peer.close()
        self.assertFalse(self.usb.reader.is_alive())
        self.assertFalse(self.hub.reader.is_alive())
        self.assertFalse(self.bridge.threads)
        self.assertFalse(self.peer.errors)

    def client(self):
        server, client = socket.socketpair()
        client.settimeout(3)
        self.bridge.accept(server)
        self.clients.append(client)
        return client

    def command(self, stream, name, **values):
        reply(stream, 73, dict(MessageType=name, **values))
        tag, response = read_reply(stream)
        self.assertEqual(tag, 73)
        return response

    def test_parallel_channels_and_idle_in_do_not_deadlock_out(self):
        first, second = self.hub.open(62078), self.hub.open(1082)
        try:
            first.send(b"first connection")
            second.send(b"second connection")
            self.assertEqual(second.read(17), b"second connection")
            self.assertEqual(first.read(16), b"first connection")
            self.assertGreater(self.peer.out_with_pending_in, 0)
        finally:
            first.close()
            second.close()

    def test_listen_and_list_use_actual_descriptor_serial(self):
        client = self.client()
        entry = self.command(client, "ListDevices")["DeviceList"][0]
        self.assertEqual(entry["Properties"]["SerialNumber"], "VM-USB-SERIAL")
        self.assertEqual(entry["Properties"]["ProductID"], 0x12ac)
        self.assertEqual(self.command(client, "Listen")["Number"], 0)
        self.assertEqual(read_reply(client), (0, entry))

    def test_clients_relay_concurrently_without_cross_stream_data(self):
        clients = [self.client(), self.client()]
        def exchange(index):
            client = clients[index]
            self.assertEqual(self.command(client, "Connect", DeviceID=1,
                                          PortNumber=socket.htons(62078 + index))["Number"], 0)
            payload = bytes([index + 1]) * 80000
            client.sendall(payload)
            self.assertEqual(read_exact(client, len(payload)), payload)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(exchange, (0, 1)))
        self.assertEqual(self.bridge.snapshot()["service_connections"], 2)

    def test_connect_and_first_payload_may_share_one_socket_write(self):
        client = self.client()
        payload = plistlib.dumps({"MessageType": "Connect", "DeviceID": 1,
                                  "PortNumber": socket.htons(62078)})
        client.sendall(struct.pack("<IIII", len(payload) + 16, 1, 8, 41) + payload + b"initial")
        self.assertEqual(read_reply(client), (41, {"MessageType": "Result", "Number": 0}))
        self.assertEqual(read_exact(client, 7), b"initial")

    def test_firmware_stream_can_exceed_plist_limit_with_bounded_packets(self):
        client = self.client()
        self.assertEqual(self.command(client, "Connect", DeviceID=1,
                                      PortNumber=socket.htons(12345))["Number"], 0)
        payload = bytes(range(256)) * 8192
        with ThreadPoolExecutor(max_workers=1) as writer:
            sent = writer.submit(client.sendall, payload)
            self.assertEqual(read_exact(client, len(payload)), payload)
            sent.result(timeout=3)

    def test_real_refusal_does_not_break_other_clients(self):
        refused = self.client()
        self.assertEqual(self.command(refused, "Connect", DeviceID=1,
                                      PortNumber=socket.htons(9))["Number"], 3)
        self.assertEqual(self.bridge.snapshot()["service_refusals"],
                         [{"port": 9, "error": "Guest reset the mux service connection"}])
        live = self.client()
        self.assertEqual(self.command(live, "Connect", DeviceID=1,
                                      PortNumber=socket.htons(62078))["Number"], 0)
        live.sendall(b"still live")
        self.assertEqual(read_exact(live, 10), b"still live")

    def test_pairing_and_invalid_targets_never_report_success(self):
        client = self.client()
        for name, code in (("ReadPairRecord", errno.ENOENT), ("SavePairRecord", errno.ENOTSUP),
                           ("DeletePairRecord", errno.ENOTSUP), ("ReadBUID", errno.ENOTSUP)):
            self.assertEqual(self.command(client, name, PairRecordID="../host")["Number"], code)
        self.assertEqual(self.command(client, "Connect", DeviceID=2, PortNumber=1)["Number"], 2)
        self.assertEqual(self.command(client, "Connect", DeviceID=1, PortNumber=True)["Number"], 1)
        self.assertEqual(self.command(client, "Connect", DeviceID=True, PortNumber=1)["Number"], 2)

    def test_bad_frames_only_disconnect_offending_client(self):
        for header in ((65537, 1, 8, 0), (16, 0, 8, 0), (16, 1, 2, 0)):
            client = self.client()
            client.sendall(struct.pack("<IIII", *header))
            self.assertEqual(client.recv(1), b"")
        self.assertEqual(len(self.command(self.client(), "ListDevices")["DeviceList"]), 1)

    def test_guest_failure_wakes_all_waiters(self):
        channel = self.hub.open(62078)
        try:
            self.peer.stop = True
            self.peer.peer.shutdown(socket.SHUT_RDWR)
            with self.assertRaises((EOFError, OSError)):
                channel.read(1)
        finally:
            try:
                channel.close()
            except (EOFError, OSError):
                pass


class ReadinessTests(unittest.TestCase):
    class ControllerStream(FragmentedStream):
        def __init__(self, data=b"", on_timeout=None):
            super().__init__(data)
            self.closed = False
            self.on_timeout = on_timeout

        def connect(self, _path):
            pass

        def close(self):
            self.closed = True

        def recv(self, size):
            if not self.data:
                if self.on_timeout:
                    self.on_timeout()
                raise TimeoutError("controller did not answer")
            return super().recv(size)

    def test_enumeration_preserves_real_responses(self):
        device = bytes([18, 1, 0, 2, 0, 0, 0, 64, 0xac, 5, 0xac, 0x12, 0, 1, 0, 0, 0, 1])
        config = bytes.fromhex("0902200001010080320904000002fffe02000705010200020007058102000200")
        frames = (response(4, 0x10) + response(1, 0, device) + response(1, 0) +
                  response(1, 0, config[:9]) + response(1, 0, config) +
                  response(1, 0) + response(1, 0, b"\x01"))
        working = self.ControllerStream(frames)
        events, deadline = [], time.monotonic() + 5
        with patch("ios_paravirt_usbmuxd.socket.socket", return_value=working):
            usb, descriptor = connect_guest({"usb": {"socket": "fixture"}}, deadline, events.append)
        self.assertFalse(working.closed)
        self.assertEqual(usb.deadline, deadline)
        self.assertEqual(descriptor["active_configuration"], 1)
        self.assertEqual(descriptor["responses_received"], 7)
        self.assertEqual(events[-1]["attempts"], 1)
        self.assertEqual(events[-1]["stage"], "enumerated")

    def test_unanswered_reset_requires_a_guest_reset_instead_of_reconnecting(self):
        stream = self.ControllerStream()
        events = []
        with patch("ios_paravirt_usbmuxd.socket.socket", return_value=stream) as factory:
            with self.assertRaises(TimeoutError):
                connect_guest({"usb": {"socket": "fixture"}}, time.monotonic() + 5, events.append)
        self.assertTrue(stream.closed)
        self.assertEqual(factory.call_count, 1)
        self.assertTrue(events[-1]["guest_reset_required"])
        self.assertEqual(events[-1]["requests_sent"], 1)
        self.assertEqual(events[-1]["responses_received"], 0)

    def test_timeout_after_guest_response_never_restarts_enumeration(self):
        stream = self.ControllerStream(response(4, 0x10))
        with patch("ios_paravirt_usbmuxd.socket.socket", return_value=stream) as factory:
            with self.assertRaises(TimeoutError):
                connect_guest({"usb": {"socket": "fixture"}}, time.monotonic() + 5)
        self.assertTrue(stream.closed)
        self.assertEqual(factory.call_count, 1)

    def test_partial_first_response_never_restarts_enumeration(self):
        for length in range(1, 6):
            with self.subTest(received_bytes=length):
                stream = self.ControllerStream(response(4, 0x10)[:length])
                with patch("ios_paravirt_usbmuxd.socket.socket", return_value=stream) as factory:
                    with self.assertRaises(TimeoutError):
                        connect_guest({"usb": {"socket": "fixture"}}, time.monotonic() + 5)
                self.assertTrue(stream.closed)
                self.assertEqual(factory.call_count, 1)

    def test_dropped_reset_cannot_extend_the_overall_deadline(self):
        with patch("ios_paravirt_usbmuxd.time.monotonic", return_value=100) as clock:
            stream = self.ControllerStream(on_timeout=lambda: setattr(clock, "return_value", 111))
            with patch("ios_paravirt_usbmuxd.socket.socket", return_value=stream) as factory:
                with self.assertRaisesRegex(TimeoutError, "controller did not answer"):
                    connect_guest({"usb": {"socket": "fixture"}}, 110)
        self.assertTrue(stream.closed)
        self.assertEqual(factory.call_count, 1)

    def test_marker_split_across_log_reads_is_observed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for marker, mode in ((USB_READY_MARKER, "restore"), (SYSTEM_USB_READY_MARKER, "system")):
                with self.subTest(mode=mode):
                    (path / "uart.log").write_bytes(b"x" * 65530 + marker + b"\n")
                    self.assertEqual(wait_for_usb_ready(path, time.monotonic() + 1), mode)

    def test_stopped_or_expired_guest_is_not_usb_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with self.assertRaises(TimeoutError):
                wait_for_usb_ready(path, time.monotonic())
            (path / "uart.log").write_bytes(b"Apple VirtIO driver initialized\n")
            (path / "result.json").write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "stopped"):
                wait_for_usb_ready(path, time.monotonic() + 1)


class RequestReadinessTests(unittest.TestCase):
    def test_waits_for_atomic_publication_and_respects_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "request.json"
            expected = {"engine_sha256": "f" * 64, "usb": {"protocol": "apple-vusb-v1", "socket": "/private/fixture"}}
            def publish():
                time.sleep(.05)
                pending = path.with_suffix(".tmp")
                pending.write_text(json.dumps(expected))
                pending.replace(path)
            with ThreadPoolExecutor(max_workers=1) as worker:
                future = worker.submit(publish)
                self.assertEqual(load_request(path, time.monotonic() + 1, wait=True), expected)
                future.result()
            path.unlink()
            with self.assertRaises(FileNotFoundError):
                load_request(path, time.monotonic(), wait=True)

    def test_invalid_oversized_or_indirect_requests_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "request.json"
            for value in (b"{}", b"null", b'{"usb":true}', b"x" * (1024 * 1024 + 1)):
                path.write_bytes(value)
                with self.assertRaises(ValueError):
                    load_request(path, time.monotonic() + 1)
            path.unlink()
            target = Path(temporary) / "target"
            target.write_text("{}")
            path.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "regular"):
                load_request(path, time.monotonic() + 1)


if __name__ == "__main__":
    unittest.main()
