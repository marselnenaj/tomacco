"""Drive Apple VirtIO's actual PCI capabilities and split queue through QTest."""
import struct
import time


class VirtioPCI:
    def __init__(self, bus, read, write, slot, identity):
        self.bus, self.read, self.write = bus, read, write
        config = 0x40000000 + (slot << 15)
        assert self.get(config, 4) == identity
        # Assign every memory BAR, including the MSI-X and modern VirtIO BARs.
        # Legacy I/O ports are unnecessary: the real guest uses PCI MMIO.
        bars = {}
        index = 0
        while index < 6:
            address = config + 0x10 + index * 4
            original = self.get(address, 4)
            self.put(address, 0xffffffff, 4)
            mask = self.get(address, 4)
            self.put(address, original, 4)
            if mask and not mask & 1:
                base = 0x50000000 + slot * 0x100000 + index * 0x10000
                bars[index] = base
                self.put(address, base, 4)
                if original & 6 == 4:
                    self.put(address + 4, 0, 4)
                    index += 1
            index += 1
        self.put(config + 4, 6, 2)  # MEMORY | BUS_MASTER
        capabilities = {}
        cap, seen = self.get(config + 0x34, 1), set()
        while cap:
            assert cap not in seen and 0x40 <= cap <= 0xfc
            seen.add(cap)
            if self.get(config + cap, 1) == 9:
                kind = self.get(config + cap + 3, 1)
                bar = self.get(config + cap + 4, 1)
                if kind in (1, 2, 4):
                    capabilities[kind] = bars[bar] + self.get(config + cap + 8, 4)
                    if kind == 2:
                        notify_multiplier = self.get(config + cap + 16, 4)
            cap = self.get(config + cap + 1, 1)
        self.capabilities = capabilities
        self.common, self.notify = (capabilities[n] for n in (1, 2))
        self.notify_multiplier = notify_multiplier

    def negotiate(self):
        self.put(self.common + 20, 0, 1)
        self.put(self.common + 20, 3, 1)  # ACKNOWLEDGE | DRIVER
        self.put(self.common, 1, 4)
        assert self.get(self.common + 4, 4) & 1  # VIRTIO_F_VERSION_1
        self.put(self.common + 8, 0, 4)
        self.put(self.common + 12, 0, 4)
        self.put(self.common + 8, 1, 4)
        self.put(self.common + 12, 1, 4)
        self.put(self.common + 20, 11, 1)  # FEATURES_OK
        assert self.get(self.common + 20, 1) == 11

    def queue(self, index, arena):
        self.put(self.common + 22, index, 2)
        assert self.get(self.common + 24, 2) >= 8
        self.put(self.common + 24, 8, 2)
        self.write(arena, bytes(0x1000))
        for offset, value in ((32, arena), (40, arena + 0x400), (48, arena + 0x800)):
            self.put(self.common + offset, value, 8)
        notify = self.notify + self.get(self.common + 30, 2) * self.notify_multiplier
        self.put(self.common + 28, 1, 2)
        return notify

    def start(self):
        self.put(self.common + 20, 15, 1)  # DRIVER_OK

    def get(self, address, size):
        return int(self.bus(f"read{ {1:'b',2:'w',4:'l',8:'q'}[size]} {address:#x}"), 16)

    def put(self, address, value, size):
        self.bus(f"write{ {1:'b',2:'w',4:'l',8:'q'}[size]} {address:#x} {value:#x}")


class VirtioBlock(VirtioPCI):
    def __init__(self, bus, read, write, slot, arena, variant):
        super().__init__(bus, read, write, slot, 0x1a00106b)
        self.arena, self.available = arena, 0
        device = self.capabilities[4]
        assert self.get(device + 60, 4) == variant  # Apple's type field
        self.negotiate()
        self.notify = self.queue(0, arena)
        self.start()
        self.sectors = self.get(device, 8)

    def submit(self, requests):
        """Publish the entire batch before one notification (write + barrier)."""
        descriptor, results = 0, []
        for operation, sector, data in requests:
            head = descriptor
            header = self.arena + 0x1000 + head * 0x200
            payload, status = header + 0x20, header + 0x230
            self.write(header, struct.pack("<IIQ", operation, 0, sector))
            self.write(status, b"\xff")
            self.write(self.arena + descriptor * 16, struct.pack("<QIHH", header, 16, 1, descriptor + 1))
            descriptor += 1
            if data is not None:
                assert len(data) == 512
                self.write(payload, data)
                self.write(self.arena + descriptor * 16,
                           struct.pack("<QIHH", payload, 512, 3 if operation == 0 else 1, descriptor + 1))
                descriptor += 1
            self.write(self.arena + descriptor * 16, struct.pack("<QIHH", status, 1, 2, 0))
            descriptor += 1
            assert descriptor <= 8
            self.put(self.arena + 0x404 + (self.available % 8) * 2, head, 2)
            self.available += 1
            results.append((payload, status))
        self.put(self.arena + 0x402, self.available, 2)
        self.put(self.notify, 0, 2)
        deadline = time.monotonic() + 5
        while self.get(self.arena + 0x802, 2) != self.available:
            assert time.monotonic() < deadline, "VirtIO queue completion timed out"
            time.sleep(0.005)
        return [(self.read(payload, 512), self.get(status, 1)) for payload, status in results]


def check_virtio_storage(bus, read, write, qmp, arena, auxiliary, checks):
    device = VirtioBlock(bus, read, write, 7, arena + 0x8000, 2)
    assert device.sectors == auxiliary.stat().st_size // 512
    def flushes():
        reply = qmp.call("query-blockstats")
        assert "return" in reply, reply
        return sum(item["stats"]["flush_operations"] for item in reply["return"])
    before = flushes()
    data = bytes(range(256)) * 2
    results = device.submit([(1, device.sectors - 1, data), (0x10000, 0, None)])
    assert [status for _, status in results] == [0, 0]
    assert flushes() == before + 1  # A no-op barrier cannot pass this check.
    with auxiliary.open("rb") as stream:
        stream.seek(-512, 2)
        assert stream.read() == data
    assert device.submit([(0, device.sectors - 1, bytes(512))]) == [(data, 0)]
    assert device.submit([(1, device.sectors, data)])[0][1] == 1
    assert device.submit([(0x20000, 0, None)])[0][1] == 2
    # Firmware and OS devices share one raw block node: no duplicated or stale
    # snapshots, and the preboot interface still cannot write to that media.
    descriptor, header, output, status = (arena + n for n in (0x300, 0x400, 0x600, 0x900))
    write(header, struct.pack("<4I", 0, 0, device.sectors - 1, 0))
    for operation in (0x30001, 0x10001):
        write(status, b"\xff")
        write(descriptor, struct.pack("<QIIQIIQII", header, 16, 0, output, 512, operation, status, 1, 0))
        bus(f"writeq 0x30010408 {descriptor:#x}")
        assert read(status, 1) == (b"\0" if operation == 0x30001 else b"\1")
        assert read(output, 512) == data
    checks.append("Apple VirtIO AUX: real PCI negotiation, queued disk write plus durable barrier, readback, invalid sector/opcode rejection and coherent read-only BDIF view")
