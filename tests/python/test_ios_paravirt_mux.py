"""USB mux framing and stream-boundary tests, separate from live guest evidence."""
import plistlib
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from ios_paravirt_mux import MuxConnection, MUX_LIMIT, PLIST_LIMIT

DEVICE = {"active_configuration": 1, "configurations": [{"value": 1, "interfaces": [
    {"class": 255, "subclass": 254, "protocol": 2, "alternate": 0,
     "endpoints": [{"address": 1, "attributes": 2}, {"address": 129, "attributes": 2}]}]}]}
VERSION = bytes.fromhex("0000000000000014000000020000000000000000")


def mux_packet(protocol, payload, magic=0xfaceface):
    return struct.pack("!IIIHH", protocol, len(payload) + 16, magic, 0, 0) + payload


def tcp(sequence=100, ack=1, flags=18, payload=b"", window=1024):
    return mux_packet(6, struct.pack("!HHIIBBHHH", 62078, 1, sequence, ack,
                                   0x50, flags, window, 0, 0) + payload)


class USBWire:
    def __init__(self, incoming):
        self.incoming, self.outgoing = list(incoming), []

    def bulk_out(self, endpoint, data):
        assert endpoint == 1
        self.outgoing.append(data)

    def bulk_in(self, endpoint, length):
        assert endpoint == 129 and length == MUX_LIMIT
        if not self.incoming:
            raise EOFError("Test stream exhausted")
        return self.incoming.pop(0)


class MuxTests(unittest.TestCase):
    def make(self, packets=()):
        wire = USBWire([VERSION, *packets])
        mux = MuxConnection(wire, DEVICE)
        self.assertEqual(mux.negotiate(), {"major": 2, "minor": 0})
        self.assertEqual(wire.outgoing[0], VERSION)
        self.assertEqual(wire.outgoing[1], bytes.fromhex("0000000200000011feedface0000ffff07"))
        return mux, wire

    def test_fragmented_and_coalesced_mux_packets(self):
        first, second = mux_packet(1, b"hello"), mux_packet(1, b"world")
        mux, _ = self.make([first[:3], first[3:10], first[10:] + second])
        self.assertEqual(mux.receive_packet(), (1, b"hello"))
        self.assertEqual(mux.receive_packet(), (1, b"world"))

    def test_unknown_version_and_malformed_packets(self):
        mux = MuxConnection(USBWire([VERSION[:8] + struct.pack("!III", 3, 0, 0)]), DEVICE)
        with self.assertRaises(ValueError):
            mux.negotiate()
        for packet in (struct.pack("!II", 6, MUX_LIMIT + 1), struct.pack("!II", 6, 8),
                       mux_packet(1, b"x", magic=0)):
            mux, _ = self.make([packet])
            with self.subTest(packet=packet), self.assertRaises(ValueError):
                mux.receive_packet()

    def test_tcp_handshake_and_explicit_reset(self):
        mux, wire = self.make([tcp()])
        mux.connect()
        self.assertEqual((mux.sequence, mux.receive_sequence, mux.acknowledged), (1, 101, 1))
        self.assertTrue(mux.connected)
        syn = wire.outgoing[2][16:]
        self.assertEqual(syn, struct.pack("!HHIIBBHHH", 1, 62078, 0, 0, 0x50, 2, 1024, 0, 0))
        mux.close()
        self.assertEqual(wire.outgoing[-1][29], 4)
        self.assertFalse(mux.connected)

    def test_refused_or_invalid_ack_is_not_a_connection(self):
        for packet, error in ((tcp(flags=4), ConnectionError),
                              (tcp(ack=2), ValueError), (tcp(ack=0), ValueError)):
            mux, _ = self.make([packet])
            with self.subTest(packet=packet), self.assertRaises(error):
                mux.connect()
            self.assertFalse(mux.connected)

    def test_guest_zero_window_is_respected(self):
        mux, wire = self.make([tcp(window=0), tcp(sequence=101, flags=16, window=1)])
        mux.connect()
        mux.send(b"hello")
        self.assertEqual(wire.outgoing[-1][36:], b"hello")
        self.assertEqual(mux.sequence, 6)

    def test_property_list_split_across_tcp_segments(self):
        reply = {"Type": "com.apple.mobile.restored", "RestoreProtocolVersion": 15}
        data = plistlib.dumps(reply)
        framed = struct.pack("!I", len(data)) + data
        mux, _ = self.make([tcp(), tcp(101, flags=16, payload=framed[:2]),
                           tcp(103, flags=16, payload=framed[2:])])
        mux.connect()
        self.assertEqual(mux.query({"Request": "QueryType"}), reply)
        self.assertEqual(mux.receive_sequence, 101 + len(framed))

    def test_out_of_order_and_oversized_plist_are_rejected(self):
        for packet in (tcp(102, flags=16, payload=b"data"),
                       tcp(101, flags=16, payload=struct.pack("!I", PLIST_LIMIT + 1))):
            mux, _ = self.make([tcp(), packet])
            mux.connect()
            with self.subTest(packet=packet), self.assertRaises(ValueError):
                mux.query({"Request": "QueryType"})

    def test_fin_preserves_final_data_and_reports_end_of_stream(self):
        mux, wire = self.make([tcp(), tcp(101, flags=25, payload=b"last")])
        mux.connect()
        self.assertEqual(mux.read(4), b"last")
        self.assertTrue(mux.eof)
        self.assertEqual(mux.receive_sequence, 106)
        with self.assertRaises(EOFError):
            mux.read(1)

    def test_send_and_receive_plists_are_independent_for_restore_events(self):
        event = {"MsgType": "ProgressMsg", "Progress": 5}
        data = plistlib.dumps(event)
        mux, wire = self.make([tcp(), tcp(101, flags=24, payload=struct.pack("!I", len(data)) + data)])
        mux.connect()
        mux.send_plist({"Request": "StartRestore"})
        self.assertEqual(mux.receive_plist(), event)


if __name__ == "__main__":
    unittest.main()
