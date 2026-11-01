"""Copy-free root disks: qcow2 overlay plans, backing chains and reading through them."""
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import tempfile
import unittest

import ios_disk_overlay as overlay
from ios_apfs_layout import inspect
from test_ios_cryptex import container


def write_qcow2(path, size, backing=None, clusters=None, zeros=(), cluster_bits=12, version=3,
                backing_format="raw", compressed=()):
    """A qcow2 image as the specification lays it out: header and backing name in
    cluster 0, the L1 table in cluster 1, then L2 tables and data clusters.
    clusters maps a guest cluster index to its bytes; zeros are clusters with the
    v3 zero flag; compressed get the compressed flag. No refcounts are written:
    only this reader reads these fixtures."""
    cluster = 1 << cluster_bits
    l2_entries = cluster // 8
    l1_size = max(1, -(-size // (cluster * l2_entries)))
    image = bytearray(2 * cluster)
    name = backing.encode() if backing else b""
    header_length = 104 if version == 3 else 72
    struct.pack_into(">4sIQIIQIIQQIIQ", image, 0, b"QFI\xfb", version, 0, 0, cluster_bits, size, 0,
                     l1_size, cluster, 0, 0, 0, 0)
    if version == 3:
        struct.pack_into(">QQQII", image, 72, 0, 0, 0, 4, header_length)
    offset = header_length
    if backing_format:
        ext = backing_format.encode()
        struct.pack_into(">II", image, offset, 0xE2792ACA, len(ext))
        image[offset + 8:offset + 8 + len(ext)] = ext
        offset += 8 + (len(ext) + 7) // 8 * 8
    struct.pack_into(">II", image, offset, 0, 0)
    offset += 8
    if name:
        image[offset:offset + len(name)] = name
        struct.pack_into(">QI", image, 8, offset, len(name))
    l1 = [0] * l1_size
    tables = {}

    def l2_for(index):
        slot = index // l2_entries
        if slot not in tables:
            tables[slot] = len(image)
            image.extend(bytes(cluster))
            l1[slot] = tables[slot] | (1 << 63)
        return tables[slot] + (index % l2_entries) * 8

    for index, data in sorted((clusters or {}).items()):
        entry = l2_for(index)
        host = len(image)
        image.extend(data.ljust(cluster, b"\0"))
        flags = (1 << 62) if index in compressed else (1 << 63)
        struct.pack_into(">Q", image, entry, host | flags)
    for index in zeros:
        struct.pack_into(">Q", image, l2_for(index), 1)
    struct.pack_into(f">{l1_size}Q", image, cluster, *l1)
    Path(path).write_bytes(bytes(image))


class Qcow2Reading(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tomacco-overlay-")
        self.root = Path(self.temp.name)
        self.raw = self.root / "guest-install1/system.raw"
        self.raw.parent.mkdir()
        self.data = bytes(range(256)) * 64  # 16 KiB, four 4 KiB clusters
        self.raw.write_bytes(self.data)

    def tearDown(self):
        self.temp.cleanup()

    def test_the_header_names_size_backing_and_its_format(self):
        top = self.root / "top.qcow2"
        write_qcow2(top, len(self.data), backing=str(self.raw))
        header = overlay.qcow2_header(top)
        self.assertEqual((header["size"], header["backing"], header["backing_format"], header["version"]),
                         (len(self.data), str(self.raw), "raw", 3))
        self.assertIsNone(overlay.qcow2_header(self.raw))
        write_qcow2(top, len(self.data), backing=str(self.raw), version=2, backing_format=None)
        self.assertEqual(overlay.qcow2_header(top)["backing_format"], None)
        # Features this reader does not know are refused, not read wrongly.
        write_qcow2(top, len(self.data))
        image = bytearray(top.read_bytes())
        struct.pack_into(">Q", image, 72, 1 << 4)  # extended L2 entries
        top.write_bytes(bytes(image))
        with self.assertRaisesRegex(ValueError, "incompatible features"):
            overlay.qcow2_header(top)

    def test_reads_fall_through_unallocated_clusters_to_the_backing_file(self):
        middle, top = self.root / "guest-install2/system.qcow2", self.root / "guest-install3/system.qcow2"
        middle.parent.mkdir()
        top.parent.mkdir()
        write_qcow2(middle, len(self.data), backing=str(self.raw), clusters={1: b"M" * 4096})
        write_qcow2(top, len(self.data), backing=str(middle), backing_format="qcow2",
                    clusters={2: b"T" * 4096}, zeros=(3,))
        expected = self.data[:4096] + b"M" * 4096 + b"T" * 4096 + bytes(4096)
        with overlay.Qcow2Chain(top) as chain:
            self.assertEqual(chain.size(), len(self.data))
            self.assertEqual(chain.pread(len(self.data), 0), expected)
            # Across cluster boundaries and clipped at the end.
            self.assertEqual(chain.pread(10, 4090), expected[4090:4100])
            self.assertEqual(chain.pread(100, len(self.data) - 4), expected[-4:])
        self.assertEqual(overlay.backing_chain(top), [top, middle, self.raw])
        self.assertEqual(overlay.installs_backing(top.parent), [middle.parent, self.raw.parent])
        self.assertEqual(overlay.installs_backing(self.raw.parent), [])
        # A raw disk reads as itself.
        with overlay.Qcow2Chain(self.raw) as chain:
            self.assertEqual(chain.pread(8, 4096), self.data[4096:4104])

    def test_a_shorter_backing_file_reads_as_zeros_beyond_its_end(self):
        top = self.root / "top.qcow2"
        write_qcow2(top, 2 * len(self.data), backing=str(self.raw))
        with overlay.Qcow2Chain(top) as chain:
            self.assertEqual(chain.pread(8, len(self.data) + 100), bytes(8))

    def test_bad_chains_are_refused(self):
        top = self.root / "top.qcow2"
        write_qcow2(top, len(self.data), backing="guest-install1/system.raw")
        with self.assertRaisesRegex(ValueError, "relative path"):
            overlay.backing_chain(top)
        write_qcow2(top, len(self.data), backing=str(self.root / "gone.raw"))
        with self.assertRaisesRegex(ValueError, "missing"):
            overlay.backing_chain(top)
        other = self.root / "other.qcow2"
        write_qcow2(top, len(self.data), backing=str(other), backing_format="qcow2")
        write_qcow2(other, len(self.data), backing=str(top), backing_format="qcow2")
        with self.assertRaisesRegex(ValueError, "loops"):
            overlay.backing_chain(top)
        write_qcow2(top, len(self.data), backing=str(self.raw), backing_format="vmdk")
        with self.assertRaisesRegex(ValueError, "format vmdk"):
            overlay.backing_chain(top)
        write_qcow2(top, len(self.data), backing=str(self.raw), clusters={0: b"z"}, compressed=(0,))
        with overlay.Qcow2Chain(top) as chain, self.assertRaisesRegex(ValueError, "Compressed"):
            chain.pread(16, 0)

    def test_a_chain_longer_than_the_limit_is_refused(self):
        previous = self.raw
        for index in range(overlay.MAX_CHAIN + 1):
            layer = self.root / f"layer{index}.qcow2"
            write_qcow2(layer, len(self.data), backing=str(previous),
                        backing_format="raw" if previous == self.raw else "qcow2")
            previous = layer
        self.assertEqual(len(overlay.backing_chain(previous)), overlay.MAX_CHAIN + 2)
        top = self.root / "one-more.qcow2"
        write_qcow2(top, len(self.data), backing=str(previous), backing_format="qcow2")
        with self.assertRaisesRegex(ValueError, "more than"):
            overlay.backing_chain(top)

    def test_a_layer_a_running_guest_holds_is_refused_when_locked(self):
        import fcntl
        top = self.root / "top.qcow2"
        write_qcow2(top, len(self.data), backing=str(self.raw))
        with open(self.raw, "rb") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            with self.assertRaisesRegex(ValueError, "in use"):
                overlay.Qcow2Chain(top, lock=True)
            with overlay.Qcow2Chain(top) as chain:
                self.assertEqual(chain.pread(4, 0), self.data[:4])

    def test_apfs_is_inspected_through_an_overlay_as_through_the_flat_disk(self):
        image = container()
        base = self.root / "apfs.raw"
        base.write_bytes(image)
        # The overlay rewrites the Preboot volume's name block in its own cluster.
        changed = bytearray(image[7 * 4096:8 * 4096])
        top = self.root / "apfs.qcow2"
        write_qcow2(top, len(image), backing=str(base), clusters={7: bytes(changed)})
        flat = self.root / "flat.raw"
        with overlay.Qcow2Chain(top) as chain:
            flat.write_bytes(chain.pread(len(image), 0))
            through = inspect(chain)
        with open(flat, "rb") as stream:
            self.assertEqual(through, inspect(stream.fileno()))


class StartPlans(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tomacco-overlay-plan-")
        self.root = Path(self.temp.name)
        self.base = self.root / "installs/guest-install7"
        self.base.mkdir(parents=True)
        (self.base / "system.raw").write_bytes(bytes(8192))
        self.output = self.root / "installs/guest-install8"

    def tearDown(self):
        self.temp.cleanup()

    def test_an_overlay_start_makes_an_empty_overlay_on_the_absolute_base(self):
        planned = overlay.plan(overlay.root_disk(self.base), self.output, "overlay", "/opt/qemu-img")
        self.assertEqual(planned["method"], "overlay")
        self.assertEqual(planned["target"], str(self.output / "system.qcow2"))
        self.assertEqual(planned["depth"], 2)
        self.assertEqual(planned["command"], [
            "/opt/qemu-img", "create", "-q", "-f", "qcow2", "-u", "-F", "raw", "-b", str(self.base / "system.raw"),
            "-o", "cluster_size=65536", str(self.output / "system.qcow2"), "8192"])
        with self.assertRaises(ValueError):
            overlay.overlay_command("/opt/qemu-img", Path("relative.raw"), "raw", 1, self.output / "x")

    def test_without_qemu_img_or_by_choice_a_raw_base_is_copied(self):
        for mode, tool, reason in (("overlay", None, "no qemu-img"), ("copy", "/opt/qemu-img", "asks for copies")):
            planned = overlay.plan(self.base / "system.raw", self.output, mode, tool)
            self.assertEqual((planned["method"], planned["command"]), ("copy", None))
            self.assertIn(reason, planned["reason"])
            self.assertEqual(planned["target"], str(self.output / "system.raw"))

    def test_an_overlay_base_gets_another_layer_or_is_flattened(self):
        layer = self.root / "installs/guest-install8/system.qcow2"
        layer.parent.mkdir()
        write_qcow2(layer, 8192, backing=str(self.base / "system.raw"))
        output = self.root / "installs/guest-install9"
        planned = overlay.plan(layer, output, "overlay", "/opt/qemu-img")
        self.assertEqual((planned["method"], planned["depth"]), ("overlay", 3))
        self.assertIn("-F", planned["command"])
        self.assertEqual(planned["command"][planned["command"].index("-F") + 1], "qcow2")
        self.assertEqual(planned["base_chain"], [str(layer), str(self.base / "system.raw")])
        flattened = overlay.plan(layer, output, "copy", "/opt/qemu-img")
        self.assertEqual(flattened["method"], "flatten")
        self.assertEqual(flattened["command"], ["/opt/qemu-img", "convert", "-q", "-f", "qcow2", "-O", "raw",
                                                str(layer), str(output / "system.raw")])
        with self.assertRaisesRegex(ValueError, "flattening it needs qemu-img"):
            overlay.plan(layer, output, "copy", None)
        deep = overlay.plan(layer, output, "overlay", "/opt/qemu-img", max_chain=1)
        self.assertEqual(deep["method"], "flatten")
        self.assertIn("2 layers, the most is 1", deep["reason"])

    def test_qemu_img_is_taken_from_beside_the_helper_engine_first(self):
        helper = self.root / "build/qemu-system-x86_64"
        helper.parent.mkdir()
        tool = helper.parent / "qemu-img"
        tool.write_text("#!/bin/sh\n")
        self.assertEqual(overlay.find_qemu_img(helper, which=lambda _name: "/usr/bin/qemu-img"), "/usr/bin/qemu-img")
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
        self.assertEqual(overlay.find_qemu_img(helper, which=lambda _name: "/usr/bin/qemu-img"), str(tool))
        self.assertIsNone(overlay.find_qemu_img(None, which=lambda _name: None))

    def test_starts_lay_overlays_by_default_while_a_qemu_img_is_found(self):
        self.assertEqual(overlay.start_disks({}, "/opt/qemu-img"), "overlay")
        self.assertEqual(overlay.start_disks({}, None), "copy")
        self.assertEqual(overlay.start_disks({"start_disks": "copy"}, "/opt/qemu-img"), "copy")
        self.assertEqual(overlay.start_disks({"start_disks": "overlay"}, None), "overlay")

    @unittest.skipUnless(shutil.which("qemu-img"), "needs qemu-img")
    def test_a_real_qemu_img_overlay_reads_as_its_base(self):
        planned = overlay.plan(self.base / "system.raw", self.output, "overlay", shutil.which("qemu-img"))
        self.output.mkdir()
        subprocess.run(planned["command"], check=True)
        header = overlay.qcow2_header(planned["target"])
        self.assertEqual((header["backing"], header["size"]), (str(self.base / "system.raw"), 8192))
        with overlay.Qcow2Chain(planned["target"]) as chain:
            self.assertEqual(chain.pread(8192, 0), bytes(8192))


FAKE_QEMU_IMG = """#!/usr/bin/env python3
import sys
sys.path[:0] = [{tests!r}, {tools!r}]
from test_ios_disk_overlay import write_qcow2
args = sys.argv[1:]
with open({log!r}, "a") as log:
    log.write(" ".join(args) + "\\n")
if args[0] == "create":
    write_qcow2(args[-2], int(args[-1]), backing=args[args.index("-b") + 1])
else:
    open(args[-1], "wb").write(bytes(8192))
"""


class MakingRootDisks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tomacco-overlay-make-")
        self.root = Path(self.temp.name)
        self.base = self.root / "guest-install7/system.raw"
        self.base.parent.mkdir()
        self.base.write_bytes(bytes(8192))
        self.output = self.root / "guest-install8"
        self.output.mkdir()
        self.tool = self.root / "qemu-img"
        self.log = self.root / "qemu-img.log"
        self.tool.write_text(FAKE_QEMU_IMG.format(tests=str(Path(__file__).parent), tools=str(Path(__file__).resolve().parents[2] / "tools"), log=str(self.log)))
        self.tool.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def test_an_overlay_is_made_and_checked_for_its_backing_file(self):
        from prepare_ios_guest_tools import make_root
        import time
        planned = overlay.plan(self.base, self.output, "overlay", str(self.tool))
        record = make_root(planned, time.monotonic() + 30)
        target = self.output / "system.qcow2"
        self.assertEqual(record["copy_method"], "overlay")
        self.assertEqual(record["backing_chain"], [str(self.base)])
        self.assertEqual(record["copy_identity"]["bytes"], target.stat().st_size)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertIn("-u", self.log.read_text().split())
        # The target is never replaced.
        with self.assertRaisesRegex(ValueError, "already exists"):
            make_root(planned, time.monotonic() + 30)
        # A tool that makes no backing reference is refused.
        target.unlink()
        self.tool.write_text(FAKE_QEMU_IMG.format(tests=str(Path(__file__).parent), tools=str(Path(__file__).resolve().parents[2] / "tools"), log=str(self.log))
                             .replace('backing=args[args.index("-b") + 1]', "backing=None"))
        with self.assertRaisesRegex(ValueError, "without the backing file"):
            make_root(planned, time.monotonic() + 30)

    def test_a_failing_qemu_img_names_its_error(self):
        from prepare_ios_guest_tools import make_root
        import time
        self.tool.write_text("#!/bin/sh\necho 'Could not open backing' >&2\nexit 1\n")
        planned = overlay.plan(self.base, self.output, "overlay", str(self.tool))
        with self.assertRaisesRegex(RuntimeError, "Could not open backing"):
            make_root(planned, time.monotonic() + 30)


class LaunchingOverlays(unittest.TestCase):
    def test_an_overlay_is_attached_as_qcow2_and_may_grow_to_its_virtual_size(self):
        import run_ios_paravirt as launcher
        with tempfile.TemporaryDirectory(prefix="tomacco-overlay-launch-") as name:
            root = Path(name)
            raw = root / "system.raw"
            raw.write_bytes(bytes(1 << 20))
            top = root / "system.qcow2"
            write_qcow2(top, 32 << 30, backing=str(raw), cluster_bits=16)
            with open(raw, "rb") as stream:
                self.assertEqual(launcher.writable_disk_bytes(stream), 1 << 20)
            with open(top, "rb") as stream:
                limit = launcher.writable_disk_bytes(stream)
            self.assertGreater(limit, 32 << 30)
            self.assertLess(limit, (32 << 30) + (128 << 20))
        node = json.loads(launcher.disk_node("tomacco-root", "/proc/self/fd/9", False, True))
        self.assertEqual(node, {"driver": "qcow2", "node-name": "tomacco-root", "read-only": False,
                                "file": {"driver": "file", "filename": "/proc/self/fd/9", "locking": "on"}})
        self.assertEqual(json.loads(launcher.disk_node("tomacco-aux", "/a", True, False))["driver"], "raw")

if __name__ == "__main__":
    unittest.main()
