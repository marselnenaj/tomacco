"""Actual QEMU PCI/queue/Unix-socket checks; no Apple firmware executes here."""
import socket
import struct
import time

from ios_paravirt_virtio import VirtioPCI


def check_virtio_usb(bus, read, write, qmp, arena, path, checks):
    device = VirtioPCI(bus, read, write, 8, 0x1a01106b)
    device.negotiate()
    rx, tx = arena + 0x10000, arena + 0x11000
    rx_data, tx_data = arena + 0x20000, arena + 0x40000
    rx_notify, tx_notify = device.queue(0, rx), device.queue(1, tx)
    device.start()

    def wait_used(queue, expected):
        deadline = time.monotonic() + 5
        while device.get(queue + 0x802, 2) != expected:
            assert time.monotonic() < deadline, "USB queue completion timed out"
            time.sleep(.005)

    def post_rx(index, capacity=128 * 1024):
        write(rx, struct.pack("<QIHH", rx_data, capacity, 2, 0))
        device.put(rx + 0x404 + ((index - 1) % 8) * 2, 0, 2)
        device.put(rx + 0x402, index, 2)
        device.put(rx_notify, 0, 2)

    def post_tx(index, data):
        write(tx_data, data)
        # Exercise scatter/gather header and payload, as used by the real driver.
        write(tx, struct.pack("<QIHHQIHH", tx_data, 2, 1, 1,
                              tx_data + 2, len(data) - 2, 0, 0))
        device.put(tx + 0x404 + ((index - 1) % 8) * 2, 0, 2)
        device.put(tx + 0x402, index, 2)
        device.put(tx_notify, 1, 2)

    response = b"\x01\x00" + bytes(range(256)) * 400
    post_tx(1, response)
    # Publishing a guest response without a connected host must not complete it.
    assert device.get(tx + 0x802, 2) == 0
    with socket.socket(socket.AF_UNIX) as peer:
        peer.settimeout(5)
        peer.connect(str(path))
        packet = bytes.fromhex("0800000000018006000100001200")
        framed = struct.pack("<I", len(packet)) + packet
        # Host data can arrive before the guest posts a receive descriptor.
        peer.sendall(framed[:2])
        peer.sendall(framed[2:7])
        peer.sendall(framed[7:])
        assert device.get(rx + 0x802, 2) == 0
        post_rx(1)
        wait_used(rx, 1)
        assert read(rx_data, len(packet)) == packet
        assert device.get(rx + 0x808, 4) == len(packet)
        wire = peer.makefile("rb", buffering=0)
        def exact(count):
            data = bytearray()
            while len(data) < count:
                part = wire.read(count - len(data))
                assert part, "Host stream unexpectedly closed"
                data += part
            return bytes(data)
        assert struct.unpack("<I", exact(4))[0] == len(response)
        assert exact(len(response)) == response
        wait_used(tx, 1)
        assert device.get(tx + 0x808, 4) == 0
        checks.append("Apple VirtIO USB: real PCI negotiation, fragmented host frame, receive backpressure, no completion without a host, scatter/gather response and exact bidirectional bytes")
        # A short receive descriptor cannot truncate a request and report success.
        post_rx(2, capacity=4)
        peer.sendall(framed)
        deadline = time.monotonic() + 5
        while not device.get(device.common + 20, 1) & 64:
            assert time.monotonic() < deadline
            time.sleep(.005)
        assert device.get(rx + 0x802, 2) == 1
        wire.close()
        assert peer.recv(1) == b""
        checks.append("USB undersized DMA buffer: NEEDS_RESET, closed peer, no truncated completion")
    # Device reset clears the failure and all partial data. Reconnect and check
    # that an invalid host length fails without publishing a used descriptor.
    device.negotiate()
    device.queue(0, rx)
    device.queue(1, tx)
    device.start()
    with socket.socket(socket.AF_UNIX) as peer:
        peer.settimeout(5)
        peer.connect(str(path))
        peer.sendall(struct.pack("<I", 128 * 1024 + 1))
        assert peer.recv(1) == b""
        assert device.get(device.common + 20, 1) & 64
        assert device.get(rx + 0x802, 2) == 0
    checks.append("USB reset/reconnect: old buffers cleared and oversized host frame rejected before guest DMA")
