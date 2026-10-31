"""Host protocol boundary tests; these do not stand in for a guest boot."""
import struct
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from ios_paravirt_usb import (MAX_FRAME, USBConnection, USBStall,
                             parse_configuration, request_frame)


class FragmentedStream:
    def __init__(self, data):
        self.data, self.sent = bytearray(data), bytearray()

    def settimeout(self, seconds):
        assert seconds > 0

    def recv(self, size):
        size = min(size, 3, len(self.data))
        result = self.data[:size]
        del self.data[:size]
        return bytes(result)

    def sendall(self, data):
        self.sent.extend(data)


def response(kind, endpoint, data=b""):
    return struct.pack("<IBB", len(data) + 2, kind, endpoint) + data


class USBProtocolTests(unittest.TestCase):
    def test_original_request_layout(self):
        setup = bytes.fromhex("8006000100001200")
        self.assertEqual(request_frame(1, payload=setup),
                         bytes.fromhex("0e0000000800000000018006000100001200"))
        self.assertEqual(request_frame(2), bytes.fromhex("06000000000000000002"))
        self.assertEqual(request_frame(1, 0x81, transfer_length=4096),
                         bytes.fromhex("06000000001000008101"))

    def test_bad_requests_never_reach_driver_assertions(self):
        for args in ((1, 0, b""), (1, 0x80, b""), (4,),
                     (1, 0x91, b""), (1, 0x81, b"bad"), (2, 1),
                     (3, 1, b"bad"), (1, 1, b"a", 2),
                     (1, 0x81, b"", MAX_FRAME),
                     (1, 0, bytes.fromhex("0009000000000100"))):
            with self.subTest(args=args), self.assertRaises(ValueError):
                request_frame(*args)

    def test_short_stream_reads_and_endpoint_events(self):
        stream = FragmentedStream(response(3, 0x81) + response(1, 0, b"abc"))
        usb = USBConnection(stream, time.monotonic() + 5)
        self.assertEqual(usb.control(0x80, 6, 0x100, length=18), b"abc")
        self.assertEqual(usb.events, [{"kind": 3, "endpoint": 0x81}])
        self.assertEqual(usb.sent, 1)
        self.assertEqual(usb.received, 2)

    def test_guest_stall_is_not_success(self):
        usb = USBConnection(FragmentedStream(response(2, 0)), time.monotonic() + 5)
        with self.assertRaises(USBStall):
            usb.control(0x80, 6, 0x100, length=18)

    def test_reset_acknowledgement_is_specific(self):
        usb = USBConnection(FragmentedStream(response(4, 0x10)), time.monotonic() + 5)
        self.assertEqual(usb.reset(), b"")
        usb = USBConnection(FragmentedStream(response(1, 0)), time.monotonic() + 5)
        with self.assertRaises(ValueError):
            usb.reset()

    def test_response_bounds_truncation_and_deadline(self):
        for data in (struct.pack("<I", MAX_FRAME + 1), struct.pack("<I", 1),
                     response(6, 0), response(2, 0, b"x")):
            usb = USBConnection(FragmentedStream(data), time.monotonic() + 5)
            with self.subTest(data=data), self.assertRaises(ValueError):
                usb.response()
        usb = USBConnection(FragmentedStream(b"\x02\x00"), time.monotonic() + 5)
        with self.assertRaises(EOFError):
            usb.response()
        usb = USBConnection(FragmentedStream(b""), time.monotonic() - 1)
        with self.assertRaises(TimeoutError):
            usb.reset()

    def test_bulk_length_and_direction(self):
        usb = USBConnection(FragmentedStream(response(1, 0x81, b"abc")), time.monotonic() + 5)
        with self.assertRaises(ValueError):
            usb.bulk_in(0x81, 2)
        with self.assertRaises(ValueError):
            usb.bulk_in(1, 3)
        with self.assertRaises(ValueError):
            usb.bulk_out(0, b"a")
        with self.assertRaises(ValueError):
            usb.control(0, 9, length=1)

    def test_initial_timeout_does_not_shorten_a_connected_session(self):
        stream = FragmentedStream(response(4, 0x10))
        with patch("ios_paravirt_usb.time.monotonic", return_value=100) as clock:
            usb = USBConnection(stream, 150, first_response_timeout=5)
            self.assertEqual(usb.reset(), b"")
            clock.return_value = 110
            usb._timeout()  # The first-response budget is no longer active.
            clock.return_value = 150
            with self.assertRaises(TimeoutError):
                usb._timeout()
            # The enclosing session limit always takes precedence.
            clock.return_value = 100
            usb = USBConnection(stream, 102, first_response_timeout=5)
            clock.return_value = 103
            with self.assertRaises(TimeoutError):
                usb.reset()

    def test_recovery_respects_session_deadline_without_sending_or_replaying(self):
        stream = FragmentedStream(response(4, 0x10))
        with patch("ios_paravirt_usb.time.monotonic", return_value=100) as clock, \
             patch("ios_paravirt_usb.time.sleep") as sleep:
            usb = USBConnection(stream, 101)
            usb.reset()
            before = bytes(stream.sent)
            usb.recover(0.050)
            sleep.assert_called_once_with(0.050)
            clock.return_value = 100.995
            with self.assertRaises(TimeoutError):
                usb.recover(0.010)
            self.assertEqual(bytes(stream.sent), before)
            self.assertEqual(sleep.call_count, 1)

    def test_partial_first_response_uses_the_session_deadline(self):
        # The first byte can arrive within the attach budget while the rest
        # follows later. Frame accounting and byte arrival are distinct.
        with patch("ios_paravirt_usb.time.monotonic", return_value=100) as clock:
            class DelayedStream(FragmentedStream):
                def recv(self, size):
                    part = super().recv(1)
                    clock.return_value = 110
                    return part
            stream = DelayedStream(response(4, 0x10))
            usb = USBConnection(stream, 150, first_response_timeout=5)
            self.assertEqual(usb.reset(), b"")
            self.assertEqual(usb.received, 1)
            self.assertEqual(usb.received_bytes, 6)
            clock.return_value = 150
            with self.assertRaises(TimeoutError):
                usb._timeout()

    def test_configuration_boundaries_and_endpoints(self):
        data = bytes.fromhex("0902190001010080320904000001fffe020007058102000200")
        result = parse_configuration(data)
        self.assertEqual(result["value"], 1)
        self.assertEqual(result["interfaces"][0]["endpoints"][0]["address"], 0x81)
        self.assertEqual(result["interfaces"][0]["endpoints"][0]["max_packet_size"], 512)
        for malformed in (data[:-1], data[:9] + b"\0" + data[10:],
                          data[:9] + b"\xff" + data[10:]):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                parse_configuration(malformed)


if __name__ == "__main__":
    unittest.main()
