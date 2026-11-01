"""Firmware-free tests of the experimental virtual iPhone's host contracts."""
import contextlib
import difflib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import zlib

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
import build_ios_paravirt as build
import prepare_ios_paravirt as prep
import run_ios_paravirt as run
import ios_paravirt_storage as storage
from ios_paravirt_firmware import der_wrap


def node(name, **properties):
    return prep.Node({"name": prep.Property(name.encode() + b"\0"),
                      **{key: prep.Property(value) for key, value in properties.items()}})


def source_tree():
    """Synthetic ADT, containing no Apple firmware or extracted binary data."""
    root = node("device-tree", compatible=b"VPHONE600AP\0")
    arm = node("arm-io", ranges=struct.pack("<QQQ", 0, 0x10000000, 0x70000000))
    for name, address, irq in (("gfx", 0x20200000, 49),
                               ("iosurface-mapper", 0x20210000, 48),
                               ("uart0", 0x10010000, 33)):
        arm.children.append(node(name, reg=struct.pack("<QQ", address, 0x10000),
                                 interrupts=struct.pack("<II", irq, 1)))
    arm.children += [node("avp-ctrr", compatible=b"avp,ctrr\0"),
                     node("sep", compatible=b"iop-sep,vpiop\0",
                          reg=struct.pack("<QQ", 0x20250000, 0x1000),
                          interrupts=struct.pack("<8I", 0, 0, 53, 0, 52, 0, 0, 0)),
                     node("aes", compatible=b"aes,s8000\0",
                          reg=struct.pack("<QQQQ", 0x20220000, 0x4000, 0x20230000, 0x4000),
                          interrupts=struct.pack("<II", 50, 1))]
    for name, address, size, irq in (("pl031-rtc", 0x10050000, 0x1000, 34),
                                     ("avp-rtc", 0x20240000, 0x1000, 51),
                                     ("pl061-buttons", 0x10060000, 0x1000, 37),
                                     ("pvpanic", 0x10070000, 2, 0)):
        arm.children.append(node(name, reg=struct.pack("<QQ", address, size),
                                 interrupts=struct.pack("<II", irq, 0)))
    arm.children += [node("gic", reg=struct.pack(
        "<6Q", 0, 0x10000, 0x10000, 0x400000, 0x0fff0000, 0x10000)),
        node("pcie", reg=struct.pack("<QQ", 0x30000000, 0x10000000), **{
            "interrupt-base": struct.pack("<I", 64),
            "msi-frame-index": struct.pack("<I", 2),
            "ranges": struct.pack("<IQQQ", 0x02000000, 0x50000000, 0x40000000, 0x2fff0000)})]
    cpus = node("cpus")
    for i, affinity in enumerate(range(32)):
        cpu = node(f"cpu{i}")
        cpu.set("device_type", b"cpu\0")
        cpu.set("reg", struct.pack("<I", affinity))
        cpu.set("timebase-frequency", struct.pack("<Q", 24000000))
        cpus.children.append(cpu)
    defaults = node("defaults")
    defaults.props["kern.max_task_pmem"] = prep.Property(b"syscfg/kmtp\0", 0x80000000)
    root.children = [arm, cpus, node("chosen"), defaults]
    return root


def storage_tree():
    tree = source_tree()
    product = node("product")
    product.children.append(node("util", regions=struct.pack(
        "<9I", 1, 0, 0xa00000, 2, 0xa00000, 0x100000, 6, 0x1a00000, 0x600000)))
    tree.children.append(product)
    return tree


class DisplayProfileTests(unittest.TestCase):
    def test_portrait_profile_preserves_pixel_and_physical_geometry(self):
        self.assertEqual(run.parse_display_profile("1170x2532@60:65x141"), {
            "width": 1170, "height": 2532, "refresh_hz": 60, "width_mm": 65, "height_mm": 141})
        self.assertEqual(run.parse_display_profile("64x64@1:10x10")["width"], 64)

    def test_cli_rejects_inconsistent_or_out_of_range_profiles(self):
        import argparse
        for raw in ("1170x2532", "1170x2532@60:65x141x1", "+1170x2532@60:65x141",
                    "8193x8193@60:100x100", "63x64@60:10x10", "1170x2532@121:65x141",
                    "1170x2532@0:65x141", "1170x2532@60:141x65", "1170x2532@60:0x0"):
            with self.subTest(raw=raw), self.assertRaises(argparse.ArgumentTypeError):
                run.parse_display_profile(raw)


def variant_tree(**tags):
    """A tree with one gestalt variant, shaped like the virtual iPhone's own."""
    tree = source_tree()
    product = node("product", **{"artwork-device-idiom": b"phone\0", "unique-model": b"VPHONE600AP\0"})
    values = {"ards": struct.pack("<I", 2556), "arsf": struct.pack("<I", 3), "dcrd": struct.pack("<II", 55, 1),
              "isnl": struct.pack("<I", 144), "oled": struct.pack("<I", 1), "home": struct.pack("<I", 2),
              "larg": struct.pack("<I", 0), "prde": b"Research Device\0", "vana": b"D47VM2025VRE\0", **tags}
    variant = node("d47vm2025vre-variant", **{"tag-" + tag: value for tag, value in values.items()})
    group = node("vphone600-gestalt-variants", compatible=b"gestalt-variant-list\0")
    group.children.append(variant)
    product.children.append(group)
    tree.children += [product, node("buttons")]
    return tree


class DeviceModelTests(unittest.TestCase):
    def test_a_model_is_a_variant_name_with_bounded_overrides(self):
        import argparse
        self.assertEqual(run.parse_device_model('{"variant":"d47vm2025vre"}'), {"variant": "d47vm2025vre"})
        full = {"variant": "d47vm2025vre", "artwork_subtype": 2622, "corner_radius": 62, "island_bottom": 153,
                "large_format": True}
        self.assertEqual(run.parse_device_model(json.dumps(full)), full)
        self.assertIsNone(run.parse_device_model('{"variant":"v1","island_bottom":null}')["island_bottom"])
        for raw in ("", "[]", "d47vm2025vre", "{}", '{"variant":"D47"}', '{"variant":"a/b"}', '{"variant":""}',
                    '{"variant":"v","extra":1}', '{"variant":"v","artwork_subtype":"2622"}',
                    '{"variant":"v","artwork_subtype":true}', '{"variant":"v","artwork_subtype":100}',
                    '{"variant":"v","corner_radius":-1}', '{"variant":"v","corner_radius":2.5}',
                    '{"variant":"v","island_bottom":5000}', '{"variant":"v","large_format":1}',
                    '{"variant":"v","artwork_subtype":null}'):
            with self.subTest(raw=raw), self.assertRaises(argparse.ArgumentTypeError):
                run.parse_device_model(raw)

    def test_the_selected_variant_becomes_the_product_description(self):
        tree = variant_tree()
        product, before = tree.child("product"), dict(tree.child("product").props)
        applied = run.device_model_handoff(tree, {"variant": "d47vm2025vre"})
        self.assertEqual(product.get("artwork-device-subtype"), struct.pack("<I", 2556))
        self.assertEqual(product.get("artwork-scale-factor"), struct.pack("<I", 3))
        self.assertEqual(product.get("display-corner-radius"), struct.pack("<II", 55, 1))
        self.assertEqual(product.get("island-notch-location"), struct.pack("<I", 144))
        self.assertEqual(product.get("product-description"), b"Research Device\0")
        self.assertEqual(tree.child("buttons").get("home-button-type"), struct.pack("<I", 2))
        self.assertEqual(applied["variant"], "d47vm2025vre")
        self.assertEqual(applied["overrides"], {})
        self.assertFalse(applied["home_button"])
        self.assertIn("artwork-device-subtype", applied["properties"])
        # Tags without a product property of their own (the variant's name) are not invented.
        self.assertNotIn("vana", " ".join(product.props))
        # Nothing else of the product changes, and the variant list stays for later boots.
        for name, value in before.items():
            self.assertEqual(product.props[name], value)
        self.assertEqual(product.child("vphone600-gestalt-variants").children[0].get("tag-ards"),
                         struct.pack("<I", 2556))
        # It survives the tree's own encoding.
        again = prep.decode(tree.encode())
        self.assertEqual(again.child("product").get("artwork-device-subtype"), struct.pack("<I", 2556))

    def test_overrides_describe_another_panel_of_the_same_family(self):
        tree = variant_tree()
        applied = run.device_model_handoff(tree, {"variant": "d47vm2025vre", "artwork_subtype": 2868,
                                                  "corner_radius": 62, "island_bottom": 153, "large_format": True})
        product = tree.child("product")
        self.assertEqual(product.get("artwork-device-subtype"), struct.pack("<I", 2868))
        self.assertEqual(product.get("display-corner-radius"), struct.pack("<II", 62, 1))
        self.assertEqual(product.get("island-notch-location"), struct.pack("<I", 153))
        self.assertEqual(product.get("large-format-phone"), struct.pack("<I", 1))
        self.assertEqual(applied["overrides"], {"artwork-device-subtype": 2868, "display-corner-radius": 62,
                                                "island-notch-location": 153, "large-format-phone": 1})
        # A device without an island or notch has no such property at all.
        tree = variant_tree()
        run.device_model_handoff(tree, {"variant": "d47vm2025vre", "island_bottom": None})
        self.assertNotIn("island-notch-location", tree.child("product").props)

    def test_a_variant_the_tree_does_not_define_is_refused(self):
        with self.assertRaises(ValueError):
            run.device_model_handoff(variant_tree(), {"variant": "d93vm2025vre"})
        plain = storage_tree()
        plain.children.append(node("buttons"))
        with self.assertRaises(ValueError):
            run.device_model_handoff(plain, {"variant": "d47vm2025vre"})
        # A node that merely has the name, outside a variant list, is not a variant.
        decoy = storage_tree()
        decoy.child("product").children.append(node("d47vm2025vre-variant", **{"tag-ards": struct.pack("<I", 1)}))
        decoy.children.append(node("buttons"))
        with self.assertRaises(ValueError):
            run.device_model_handoff(decoy, {"variant": "d47vm2025vre"})
        # A variant that keeps the Home button says so.
        with_button = variant_tree()
        del with_button.child("product").children[0].children[0].props["tag-home"]
        self.assertTrue(run.device_model_handoff(with_button, {"variant": "d47vm2025vre"})["home_button"])
        self.assertNotIn("home-button-type", with_button.child("buttons").props)


class DisplayScaleTests(unittest.TestCase):
    """/chosen/display-scale is iBoot's answer for the panel, and MobileGestalt's
    main-screen-scale reads it. The launcher boots the kernel directly, so it
    fills the value in from the profile's density; with the tree's placeholder
    0, Settings traps in IconServices and the window server runs a 2x virtual
    mode."""

    def test_the_panels_density_selects_the_scale_iboot_would(self):
        for raw, ppi, scale in (("1179x2556@60:65x141", 460.7, 3), ("1170x2532@60:65x141", 457.2, 3),
                                ("750x1334@60:58x104", 328.4, 2), ("828x1792@60:65x140", 323.6, 2),
                                ("1920x1080@60:480x270", 101.6, 1)):
            with self.subTest(raw=raw):
                profile = run.parse_display_profile(raw)
                self.assertEqual(round(run.pixels_per_inch(profile), 1), ppi)
                self.assertEqual(run.display_scale_for_profile(profile), scale)

    def test_zero_becomes_the_derived_scale_once(self):
        tree = source_tree()
        tree.child("chosen").set("display-scale", struct.pack("<I", 0))
        applied = run.display_scale_handoff(tree, 3)
        self.assertEqual(tree.child("chosen").get("display-scale"), struct.pack("<I", 3))
        self.assertEqual(applied, {"scale": 3, "previous": 0, "property_updated": True})
        self.assertEqual(prep.decode(tree.encode()).child("chosen").get("display-scale"), struct.pack("<I", 3))
        # The same scale again changes nothing.
        self.assertEqual(run.display_scale_handoff(tree, 3), {"scale": 3, "previous": 3, "property_updated": False})
        # A tree without the property gets it, as iBoot would have written it.
        bare = source_tree()
        self.assertEqual(run.display_scale_handoff(bare, 2), {"scale": 2, "previous": None, "property_updated": True})
        self.assertEqual(bare.child("chosen").get("display-scale"), struct.pack("<I", 2))

    def test_a_flagged_odd_sized_or_foreign_value_is_refused(self):
        flagged = source_tree()
        flagged.child("chosen").props["display-scale"] = prep.Property(b"syscfg/dsc\0", 0x80000000)
        short = source_tree()
        short.child("chosen").set("display-scale", b"\x03\0")
        foreign = source_tree()
        foreign.child("chosen").set("display-scale", struct.pack("<I", 2))
        for tree in (flagged, short, foreign):
            before = tree.child("chosen").props["display-scale"]
            with self.subTest(value=before.data), self.assertRaises(ValueError):
                run.display_scale_handoff(tree, 3)
            # A refusal leaves the tree as it was.
            self.assertEqual(tree.child("chosen").props["display-scale"], before)
        for scale in (0, 4, 2.0, "3"):
            with self.subTest(scale=scale), self.assertRaises(ValueError):
                run.display_scale_handoff(source_tree(), scale)


class SessionLimitTests(unittest.TestCase):
    def test_only_an_interactive_session_with_guest_control_saves_itself(self):
        cli, control = Path("/cli"), object()
        self.assertEqual(run.saving_margin(21600, cli, True, control, "a" * 32), 600)
        self.assertEqual(run.saving_margin(600, cli, True, control, "a" * 32), 150)
        for arguments in ((600, None, True, control, "a" * 32), (600, cli, False, control, "a" * 32),
                          (600, cli, True, None, "a" * 32), (600, cli, True, control, None),
                          (600, cli, True, control, "")):
            self.assertIsNone(run.saving_margin(*arguments))

    def test_the_desktop_keeps_an_eighth_of_the_card(self):
        self.assertFalse(run.gpu_memory_short(None))
        self.assertFalse(run.gpu_memory_short((0, 0)))
        self.assertFalse(run.gpu_memory_short((14 << 30, 16 << 30)))
        self.assertTrue(run.gpu_memory_short(((14 << 30) + 1, 16 << 30)))

    def test_each_guard_check_is_published_for_the_status(self):
        record = run.gpu_memory_record(((15 << 30), 16 << 30), 1, False, 1234.5678)
        self.assertEqual(record, {"format": 1, "checked": 1234.568, "used_bytes": 15 << 30, "total_bytes": 16 << 30,
                                  "free_bytes": 1 << 30, "limit_free_bytes": 2 << 30, "short_checks": 1,
                                  "stopping": False})
        # A card that reports nothing is published as unknown, not as empty.
        self.assertEqual(run.gpu_memory_record(None, 0, False, 1.0)["free_bytes"], None)
        self.assertEqual(run.gpu_memory_record((0, 0), 0, False, 1.0)["limit_free_bytes"], None)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            run.save_gpu_memory(output, record)
            self.assertEqual(json.loads((output / run.GPU_MEMORY_RECORD).read_text()), record)
            self.assertEqual((output / run.GPU_MEMORY_RECORD).stat().st_mode & 0o777, 0o600)
            self.assertEqual(sorted(path.name for path in output.iterdir()), [run.GPU_MEMORY_RECORD])
            # A directory that cannot take it costs the session nothing.
            run.save_gpu_memory(output / "missing", record)

    def test_the_card_a_process_renders_on_is_read_from_its_open_files(self):
        with tempfile.TemporaryDirectory() as directory:
            card = Path(directory) / "bus/pci/devices/0000:03:00.0"
            card.mkdir(parents=True)
            (card / "mem_info_vram_used").write_text("7\n")
            (card / "mem_info_vram_total").write_text("9\n")
            fdinfo = Path(directory) / "proc/42/fdinfo"
            fdinfo.mkdir(parents=True)
            (fdinfo / "1").write_text("pos:\t0\n")
            (fdinfo / "2").write_text("drm-driver:\ti915\ndrm-pdev:\t0000:00:02.0\n")
            (fdinfo / "3").write_text("drm-driver:\tamdgpu\ndrm-pdev:\t0000:03:00.0\n")
            with patch.dict(run.process_gpu_memory.__globals__, {"sysfs_root": lambda: Path(directory)}):
                self.assertEqual(run.process_gpu_memory(42, Path(directory) / "proc"), (7, 9))
            self.assertIsNone(run.process_gpu_memory(43, Path(directory) / "proc"))

    def test_the_saved_stop_reports_what_the_control_plane_answered(self):
        with tempfile.TemporaryDirectory() as directory:
            cli = Path(directory) / "cli"
            calls = Path(directory) / "calls"
            def answer(text):
                cli.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" >> " + str(calls) + "\ncat <<'EOF'\n" + text + "\nEOF\n")
                cli.chmod(0o755)
            answer(json.dumps({"ok": True, "result": {"result": {"guest_storage_flushed": True}}}))
            self.assertEqual(run.saved_stop(cli, "b" * 32),
                             {"requested": True, "accepted": True, "guest_storage_flushed": True})
            self.assertEqual(calls.read_text().splitlines(),
                             ["call", "ios.emulator.control", json.dumps({"job_id": "b" * 32, "action": "stop"})])
            answer(json.dumps({"ok": True, "result": {"result": {"guest_storage_flushed": False}}}))
            self.assertFalse(run.saved_stop(cli, "b" * 32)["guest_storage_flushed"])
            answer(json.dumps({"ok": False, "error": {"code": "ios_guest_not_idle"}}))
            self.assertEqual(run.saved_stop(cli, "b" * 32),
                             {"requested": True, "accepted": False, "error": "ios_guest_not_idle"})
            answer("not json")
            refused = run.saved_stop(cli, "b" * 32)
            self.assertEqual((refused["requested"], refused["accepted"]), (True, False))
            missing = run.saved_stop(Path(directory) / "missing", "b" * 32)
            self.assertEqual((missing["requested"], missing["accepted"]), (True, False))

    def longest_stop(self):
        """Seconds a careful stop of the control plane can take before it tells
        the emulator to exit, summed from the constants native/ builds it on."""
        def constant(name, pattern):
            found = re.search(pattern, (REPO / "native/ios_emulator" / name).read_text())
            self.assertIsNotNone(found, f"{name} no longer defines {pattern}")
            return found[1]
        lane = int(constant("control.rs", r"const LANE_WAIT: Duration = Duration::from_millis\((\d+)\);")) / 1000
        service = int(constant("guest_control.rs", r"const SERVICE_WAIT: Duration = Duration::from_secs\((\d+)\);"))
        status = int(constant("guest_control.rs", r"const STATUS_SECONDS: u64 = (\d+);"))
        flushes = constant("guest_control.rs", r"const FLUSH_SECONDS: \[u64; \d+\] = \[([\d, ]+)\];")
        return lane + service + status + sum(int(seconds) for seconds in flushes.split(","))

    def test_the_launcher_waits_longer_than_the_control_planes_longest_stop(self):
        longest = self.longest_stop()
        # The wait for the control lane (2.5 s), the status query (15 s) and
        # both flush deadlines (60 s and 120 s) are the least it can take.
        self.assertGreaterEqual(longest, 2.5 + 15 + 60 + 120)
        # The QMP quit (5 s) and the stop record come after them.
        self.assertGreater(run.SAVED_STOP_SECONDS, longest + 5 + 5)
        # A long session has room for a whole attempt, the pause before the
        # next one, and a whole second attempt.
        margin = run.saving_margin(21600, Path("/cli"), True, object(), "a" * 32)
        self.assertGreaterEqual(margin, 2 * run.SAVED_STOP_SECONDS + 20)

    def test_a_stop_the_launcher_cut_off_is_recorded_apart_from_a_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            cli = Path(directory) / "cli"
            cli.write_text("#!/bin/sh\nexec sleep 30\n")
            cli.chmod(0o755)
            with patch.object(run, "SAVED_STOP_SECONDS", 1):
                cut_off = run.saved_stop(cli, "b" * 32)
            self.assertEqual((cut_off["requested"], cut_off["accepted"], cut_off["timed_out"]), (True, False, True))
            self.assertIn("1 s", cut_off["error"])
            # What the launcher waits for is that constant, not a number of its own.
            with patch.object(run.subprocess, "run", side_effect=subprocess.TimeoutExpired("cli", 1)) as called:
                self.assertTrue(run.saved_stop(cli, "b" * 32)["timed_out"])
            self.assertEqual(called.call_args.kwargs["timeout"], run.SAVED_STOP_SECONDS)
            # A refusal, an unreadable answer and a missing executable are no cut-offs.
            cli.write_text("#!/bin/sh\necho '{\"ok\": false, \"error\": {\"code\": \"ios_guest_not_idle\"}}'\n")
            for receipt in (run.saved_stop(cli, "b" * 32), run.saved_stop(Path(directory) / "missing", "b" * 32)):
                self.assertNotIn("timed_out", receipt)
                self.assertFalse(receipt["accepted"])


class RendererLogTests(unittest.TestCase):
    def test_only_a_large_own_regular_file_is_cut_back_and_writers_continue(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "fail.log"
            self.assertFalse(run.trim_renderer_log(log, 10))  # missing
            with log.open("ab") as writer:
                writer.write(b"x" * 8)
                writer.flush()
                self.assertFalse(run.trim_renderer_log(log, 10))
                self.assertEqual(log.stat().st_size, 8)
                writer.write(b"y" * 8)
                writer.flush()
                self.assertTrue(run.trim_renderer_log(log, 10))
                self.assertEqual(log.stat().st_size, 0)
                # An appending writer continues at the new end, without a hole.
                writer.write(b"after")
                writer.flush()
            self.assertEqual(log.read_bytes(), b"after")
            # A link is never followed, whatever it points at.
            target = Path(directory) / "target"
            target.write_bytes(b"z" * 64)
            link = Path(directory) / "link.log"
            link.symlink_to(target)
            self.assertFalse(run.trim_renderer_log(link, 10))
            self.assertEqual(target.stat().st_size, 64)
            self.assertFalse(run.trim_renderer_log(Path(directory), 10))

    def test_a_fifo_planted_at_the_path_cannot_block_the_session_loop(self):
        # /tmp is shared: anyone can create a FIFO under that name, and opening
        # one for writing waits for a reader.
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "fail.log"
            os.mkfifo(log)
            outcome = []
            attempt = threading.Thread(target=lambda: outcome.append(run.trim_renderer_log(log, 0)), daemon=True)
            attempt.start()
            attempt.join(5)
            blocked = attempt.is_alive()
            if blocked:
                # Let the stuck open through, so that the thread ends.
                os.close(os.open(log, os.O_RDONLY | os.O_NONBLOCK))
                attempt.join(5)
            self.assertFalse(blocked, "a FIFO without a reader blocked the caller")
            self.assertEqual(outcome, [False])
            # With a reader attached the open succeeds; a FIFO is still no log to cut back.
            reader = os.open(log, os.O_RDONLY | os.O_NONBLOCK)
            try:
                self.assertFalse(run.trim_renderer_log(log, 0))
            finally:
                os.close(reader)


class EngineFileLimitTests(unittest.TestCase):
    def test_the_limit_covers_one_uncompressed_frame_of_the_panel(self):
        small = run.parse_display_profile("1179x2556@60:65x141")  # 9 MB of RGB
        self.assertEqual(run.engine_file_limit([], small), 32 << 20)
        self.assertEqual(run.engine_file_limit([], None), 32 << 20)
        large = run.parse_display_profile("4096x4096@60:140x140")  # 48 MiB of RGB
        frame = len(b"P6\n4096 4096\n255\n") + 4096 * 4096 * 3
        self.assertEqual(run.frame_file_bytes(large), frame)
        limit = run.engine_file_limit([], large)
        self.assertEqual(limit % (1 << 20), 0)
        self.assertGreaterEqual(limit, frame + (1 << 20))
        self.assertLess(limit, frame + (3 << 20))
        # Even the largest panel the launcher accepts fits its own frame.
        biggest = run.parse_display_profile("8192x8192@60:200x200")
        self.assertGreater(run.engine_file_limit([], biggest), 8192 * 8192 * 3)
        # A writable disk keeps its own length when that is larger.
        self.assertEqual(run.engine_file_limit([100 << 30, 1 << 20], large), 100 << 30)
        self.assertEqual(run.engine_file_limit([1 << 20], small), 32 << 20)

    def test_the_limit_covers_the_frame_mailbox_of_an_interactive_session(self):
        # Two BGRA8 frames and a 4 KiB header: the engine writes the next frame
        # while a reader may still copy the previous one.
        small = run.parse_display_profile("1179x2556@60:65x141")
        self.assertEqual(run.frame_mailbox_bytes(small), 4096 + 2 * 1179 * 2556 * 4)
        self.assertEqual(run.engine_file_limit([], small, mailbox=True), 32 << 20)  # 24.1 MB fits
        large = run.parse_display_profile("4096x4096@60:140x140")
        mailbox = 4096 + 2 * 4096 * 4096 * 4
        limit = run.engine_file_limit([], large, mailbox=True)
        self.assertEqual(limit % (1 << 20), 0)
        self.assertGreaterEqual(limit, mailbox + (1 << 20))
        self.assertLess(limit, mailbox + (3 << 20))
        # Without a viewer's mailbox the limit stays one PPM frame.
        self.assertLess(run.engine_file_limit([], large), mailbox)
        self.assertEqual(run.engine_file_limit([], None, mailbox=True), 32 << 20)


class EngineScratchTests(unittest.TestCase):
    """The engine's TMPDIR, where the renderer translates every shader."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = Path(self.temp.name) / "build" / "qemu-system-aarch64"
        self.engine.parent.mkdir()
        self.engine.write_text("")

    def tearDown(self):
        self.temp.cleanup()

    def test_it_is_a_private_directory_beside_the_engine_that_took_a_probe(self):
        directory, problem = run.engine_scratch(self.engine, probe_bytes=2 << 20)
        self.assertIsNone(problem)
        self.assertEqual(directory, self.engine.parent / ".tomacco-engine-tmp")
        self.assertTrue(run.private_directory(directory))
        # The probe is gone, and a second launch reuses the directory.
        self.assertEqual(list(directory.iterdir()), [])
        self.assertEqual(run.engine_scratch(self.engine, probe_bytes=1 << 20), (directory, None))

    def test_a_full_or_over_quota_filesystem_is_named_before_the_launch(self):
        import errno as errnos
        real_write = os.write
        def write(descriptor, data):
            if os.readlink(f"/proc/self/fd/{descriptor}").endswith(f".probe-{os.getpid()}"):
                raise OSError(errnos.EDQUOT, os.strerror(errnos.EDQUOT))
            return real_write(descriptor, data)
        with patch.object(run.os, "write", write):
            directory, problem = run.engine_scratch(self.engine, probe_bytes=1 << 20)
        self.assertIn("cannot take 1 MiB", problem)
        self.assertIn(os.strerror(errnos.EDQUOT), problem)
        self.assertEqual(list(directory.iterdir()), [])

    def test_a_directory_others_may_enter_is_refused(self):
        directory = self.engine.parent / ".tomacco-engine-tmp"
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
        self.assertEqual(run.engine_scratch(self.engine)[1], "it is not a private directory of this user")

    def test_the_launcher_hands_it_to_the_engine_as_tmpdir(self):
        source = Path(run.__file__).read_text()
        self.assertIn('"TMPDIR": str(scratch)', source)
        self.assertLess(source.index("engine_scratch(engine)"), source.index("subprocess.Popen(command"))


class FrameMailboxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name) / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.environ = {"XDG_RUNTIME_DIR": str(self.runtime)}

    def test_each_launcher_gets_a_private_locked_directory_it_removes(self):
        first = run.FrameMailbox.open(self.environ)
        second = run.FrameMailbox.open(self.environ)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        root = self.runtime / "tomacco/ios-mailbox"
        for mailbox in (first, second):
            self.assertEqual(mailbox.directory.parent, root)
            self.assertRegex(mailbox.directory.name, r"^[0-9a-f]{32}$")
            self.assertEqual(mailbox.path, mailbox.directory / "frames")
            self.assertFalse(mailbox.path.exists())  # the engine creates it
            self.assertTrue(run.private_directory(mailbox.directory))
            # Held for as long as the launcher lives.
            probe = os.open(mailbox.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)
        for path in (self.runtime / "tomacco", root):
            self.assertTrue(run.private_directory(path))
        self.assertNotEqual(first.directory, second.directory)
        receipt = first.receipt()
        self.assertEqual((receipt["path"], receipt["pixel_format"]), (str(first.path), "bgra8"))
        first.path.write_bytes(b"frames")
        first.close()
        first.close()  # closing twice is harmless
        self.assertFalse(first.directory.exists())
        self.assertTrue(second.directory.exists())

    def test_no_mailbox_without_a_private_runtime_directory(self):
        self.assertIsNone(run.FrameMailbox.open({}))
        self.assertIsNone(run.FrameMailbox.open({"XDG_RUNTIME_DIR": "relative"}))
        self.assertIsNone(run.FrameMailbox.open({"XDG_RUNTIME_DIR": str(self.runtime / "missing")}))
        self.runtime.chmod(0o755)
        self.assertIsNone(run.FrameMailbox.open(self.environ))
        self.runtime.chmod(0o700)
        # A component someone else may enter, or a link in its place.
        (self.runtime / "tomacco").mkdir(mode=0o700)
        (self.runtime / "tomacco").chmod(0o755)
        self.assertIsNone(run.FrameMailbox.open(self.environ))
        (self.runtime / "tomacco").chmod(0o700)
        elsewhere = self.runtime / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        (self.runtime / "tomacco/ios-mailbox").symlink_to(elsewhere)
        self.assertIsNone(run.FrameMailbox.open(self.environ))
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_only_an_ended_launchers_old_mailbox_is_swept(self):
        live = run.FrameMailbox.open(self.environ)
        self.addCleanup(live.close)
        root = live.directory.parent
        ended = root / ("a" * 32)
        ended.mkdir(mode=0o700)
        (ended / "frames").write_bytes(b"left behind by a killed launcher")
        recent = root / ("b" * 32)
        recent.mkdir(mode=0o700)
        other = root / "not-a-mailbox"
        other.mkdir()
        old = time.time() - 3600
        for path in (ended, live.directory, other):
            os.utime(path, (old, old))
        self.assertEqual(run.sweep_frame_mailboxes(root), ["a" * 32])
        self.assertFalse(ended.exists())
        self.assertTrue(live.directory.exists())  # locked: its launcher runs
        self.assertTrue(recent.exists())  # may be between mkdir and lock
        self.assertTrue(other.exists())
        self.assertEqual(run.sweep_frame_mailboxes(root, now=time.time() + 3600), ["b" * 32])


class SocketRootTests(unittest.TestCase):
    """Sockets need a short path whatever TMPDIR is."""

    def test_a_long_tmpdir_gives_way_to_a_short_private_directory(self):
        from ios_guest_control import SOCKET_PATH_LIMIT, socket_root
        with tempfile.TemporaryDirectory(dir="/tmp") as base:
            short = os.path.join(base, "t")
            long = os.path.join(base, "x" * 90)
            runtime = os.path.join(base, "run")
            for path in (short, long, runtime):
                os.mkdir(path, 0o700)
            os.chmod(runtime, 0o700)
            self.assertEqual(socket_root(48, {"TMPDIR": short, "XDG_RUNTIME_DIR": runtime}), short)
            self.assertEqual(socket_root(48, {"TMPDIR": long, "XDG_RUNTIME_DIR": runtime}), runtime)
            os.chmod(runtime, 0o755)
            self.assertEqual(socket_root(48, {"TMPDIR": long, "XDG_RUNTIME_DIR": runtime}), "/tmp")
            self.assertEqual(socket_root(48, {"TMPDIR": "relative"}), "/tmp")
            self.assertEqual(socket_root(48, {}), "/tmp")
            # What the launcher makes there fits a socket address.
            self.assertLessEqual(len(socket_root(48)) + 48, SOCKET_PATH_LIMIT)


class SessionLogBudgetTests(unittest.TestCase):
    def test_a_day_long_session_gets_a_log_budget_per_six_hours(self):
        # A serial log reaches about 11 MiB in six hours; a fixed 32 MiB
        # budget would end a longer session at about 17 hours.
        self.assertEqual(run.SESSION_SECONDS_MAX, 86400)
        self.assertEqual(run.log_budget(1), 32 << 20)
        self.assertEqual(run.log_budget(21600), 32 << 20)
        self.assertEqual(run.log_budget(21601), 64 << 20)
        self.assertEqual(run.log_budget(86400), 128 << 20)
        small = run.parse_display_profile("1179x2556@60:65x141")
        self.assertEqual(run.engine_file_limit([], small, run.log_budget(86400)), 128 << 20)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            log = output / "uart.log"
            with log.open("wb") as stream:
                stream.truncate((32 << 20) + 1)
            with self.assertRaisesRegex(ValueError, "exceeded the 32 MiB budget"):
                run.check_log_budget(output)
            run.check_log_budget(output, run.log_budget(43200))
            with log.open("r+b") as stream:
                stream.truncate((64 << 20) + 1)
            with self.assertRaisesRegex(ValueError, "exceeded the 64 MiB budget"):
                run.check_log_budget(output, run.log_budget(43200))

    def test_the_operation_schema_allows_the_launchers_whole_range(self):
        operations = json.loads((Path(__file__).resolve().parents[2] / "assets/operations.json").read_text())
        maxima = []

        def walk(value):
            if isinstance(value, dict):
                seconds = value.get("seconds")
                if isinstance(seconds, dict) and "Bounded host session duration" in str(seconds.get("description")):
                    maxima.append(seconds.get("maximum"))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(operations)
        self.assertEqual(maxima, [run.SESSION_SECONDS_MAX])


class DisplayHandoffTests(unittest.TestCase):
    def test_empty_vram_is_removed_without_changing_gpu_or_other_nodes(self):
        tree = source_tree()
        original = tree.encode()
        tree.children.append(node("vram", reg=bytes(16), device_type=b"vram\0"))
        record = prep.display_handoff(tree)
        self.assertTrue(record["unallocated_vram_node_removed"])
        self.assertFalse(record["dedicated_vram_advertised"])
        self.assertEqual(tree.encode(), original)
        self.assertFalse(prep.display_handoff(tree)["unallocated_vram_node_removed"])
        source = source_tree()
        source.children.append(node("vram", reg=bytes(16), device_type=b"vram\0"))
        prepared = prep.decode(prep.prepare(source.encode()))
        self.assertNotIn("vram", [child.name for child in prepared.children])

    def test_real_carveout_is_preserved_and_unknown_bindings_are_rejected(self):
        tree = source_tree()
        region = node("vram", reg=struct.pack("<QQ", 0x90000000, 0x800000), device_type=b"vram\0")
        tree.children.append(region)
        original = tree.encode()
        self.assertTrue(prep.display_handoff(tree)["dedicated_vram_advertised"])
        self.assertEqual(tree.encode(), original)
        for key, value in (("reg", b""), ("reg", bytes(8)), ("device_type", b"unknown\0")):
            region.set(key, value)
            before = tree.encode()
            with self.assertRaisesRegex(ValueError, "Unsupported VRAM"):
                prep.display_handoff(tree)
            self.assertEqual(tree.encode(), before)
            region.set(key, struct.pack("<QQ", 0x90000000, 0x800000) if key == "reg" else b"vram\0")


class QMPTests(unittest.TestCase):
    def client(self, responses):
        client = run.QMP.__new__(run.QMP)
        client.socket = Mock()
        client.socket.gettimeout.return_value = 5
        client.stream = io.BytesIO()
        client.receive = Mock(side_effect=responses)
        return client

    def test_debugger_event_backlog_does_not_hide_command_response(self):
        events = [{"event": kind} for _ in range(256) for kind in ("STOP", "RESUME")]
        response = {"return": {"status": "paused"}}
        client = self.client([*events, response])
        self.assertEqual(client.call("query-status"), response)
        self.assertEqual(json.loads(client.stream.getvalue()), {"execute": "query-status"})
        client.socket.settimeout.assert_called_with(5)

    def test_event_stream_cannot_extend_response_deadline(self):
        client = self.client([{"event": "STOP"}])
        with patch.object(run.time, "monotonic", side_effect=[100, 100, 105]):
            with self.assertRaisesRegex(TimeoutError, "response deadline"):
                client.call("stop")
        client.socket.settimeout.assert_called_with(5)

    def test_command_error_is_preserved_after_events(self):
        response = {"error": {"class": "GenericError", "desc": "unsupported"}}
        client = self.client([{"event": "RESUME"}, response])
        self.assertEqual(client.call("human-monitor-command", {"command-line": "unknown"}), response)


class BootManifestTests(unittest.TestCase):
    # Synthetic envelope only; the deliberately non-cryptographic payloads
    # must never be described as signature- or device-binding verification.
    manifest = b"\x30\x13\x16\x04IM4M\x02\x01\x00\x31\x02xx\x04\x01s\x30\x01c"

    def tree(self):
        root = source_tree()
        root.child("chosen").set("crypto-hash-method", b"sha2-384\0")
        root.child("chosen").set("boot-manifest-hash", bytes(48))
        return root

    def test_exact_supplied_object_hash_without_authentication_claim(self):
        root = self.tree()
        record = run.boot_manifest_handoff(root, self.manifest)
        self.assertEqual(root.child("chosen").get("boot-manifest-hash"), hashlib.sha384(self.manifest).digest())
        self.assertFalse(record["signature_verified_by_host"])
        self.assertFalse(record["device_binding_verified"])
        self.assertFalse(run.boot_manifest_handoff(root, self.manifest)["hash_updated"])
        changed = self.manifest[:-1] + b"d"
        with self.assertRaisesRegex(ValueError, "different boot manifest"):
            run.boot_manifest_handoff(root, changed)

    def test_bad_envelopes_cannot_change_boot_measurement(self):
        for data in (b"", self.manifest[:-1], self.manifest + b"x",
                     self.manifest.replace(b"IM4M", b"IM4P"), b"\x30\x80" + self.manifest,
                     b"\x30\x81\x13" + self.manifest[2:]):
            root = self.tree()
            with self.subTest(data=data), self.assertRaises(ValueError):
                run.boot_manifest_handoff(root, data)
            self.assertEqual(root.child("chosen").get("boot-manifest-hash"), bytes(48))

    def test_unknown_hash_profile_is_not_guessed(self):
        root = self.tree()
        root.child("chosen").set("crypto-hash-method", b"sha1\0")
        with self.assertRaisesRegex(ValueError, "SHA2-384"):
            run.boot_manifest_handoff(root, self.manifest)


class SystemVolumeHandoffTests(unittest.TestCase):
    # Synthetic SHA-256 digest slots, not firmware or a signed root hash.
    payload = struct.pack("<4I", 2, 0, 1, 32) + b"h" * 32 + bytes(32) + bytes(128)

    @classmethod
    def im4p(cls, payload=None, tag=b"isys"):
        return der_wrap(0x30, der_wrap(0x16, b"IM4P") + der_wrap(0x16, tag)
                        + der_wrap(0x16, b"synthetic test")
                        + der_wrap(4, cls.payload if payload is None else payload))

    def test_exact_payload_is_handed_off_without_trust_overrides(self):
        tree = source_tree()
        chosen = tree.child("chosen")
        chosen.set("secure-boot", bytes(4))
        chosen.set("system-trusted", bytes(4))
        original = dict(chosen.props)
        data = self.im4p()
        record = run.system_volume_handoff(tree, data)
        self.assertEqual(chosen.get("system-volume-auth-blob"), self.payload)
        for name, value in original.items():
            self.assertEqual(chosen.props[name], value)
        self.assertEqual(set(chosen.props) - set(original), {"system-volume-auth-blob", "root-snapshot-name"})
        snapshot = "com.apple.os.update-" + (b"h" * 32).hex().upper()
        self.assertEqual(chosen.get("root-snapshot-name"), snapshot.encode() + b"\0")
        self.assertEqual(record["root_snapshot_name"], snapshot)
        self.assertEqual(record["source_sha384"], hashlib.sha384(data).hexdigest())
        self.assertEqual(record["root_hash"], (b"h" * 32).hex())
        self.assertFalse(record["signature_verified_by_host"])
        self.assertFalse(record["volume_seal_verified_by_host"])
        self.assertFalse(record["bootloader_policy_evaluated"])
        self.assertFalse(run.system_volume_handoff(tree, data)["property_updated"])
        changed = self.payload[:16] + b"z" * 32 + self.payload[48:]
        with self.assertRaisesRegex(ValueError, "different SystemVolume"):
            run.system_volume_handoff(tree, self.im4p(changed))
        self.assertEqual(chosen.get("system-volume-auth-blob"), self.payload)

    def test_bad_or_unknown_root_hash_cannot_modify_tree(self):
        padded = bytearray(self.payload)
        padded[-1] = 1
        for data in (b"", self.im4p()[:-1], self.im4p() + b"x", b"x" * 65537,
                     self.im4p(tag=b"msys"), self.im4p(self.payload[:-1]),
                     self.im4p(b"\x03" + self.payload[1:]),
                     self.im4p(self.payload[:16] + bytes(192)), self.im4p(bytes(padded))):
            tree = source_tree()
            original = tree.encode()
            with self.subTest(data=data[:32]), self.assertRaises(ValueError):
                run.system_volume_handoff(tree, data)
            self.assertEqual(tree.encode(), original)

    def test_conflicting_snapshot_is_rejected_without_partial_tree_changes(self):
        tree = source_tree()
        tree.child("chosen").set("root-snapshot-name", b"com.apple.os.update-OTHER\0")
        original = tree.encode()
        with self.assertRaisesRegex(ValueError, "different root snapshot"):
            run.system_volume_handoff(tree, self.im4p())
        self.assertEqual(tree.encode(), original)


class PersistentStorageTests(unittest.TestCase):
    def test_restore_container_has_recoverable_gpt_but_no_apfs_filesystem(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "media"
            size = 64 << 20
            record = storage.create_storage(storage_tree(), output, size, "restore-container")
            self.assertFalse(record["restored"])
            partition = record["root_partition"]
            self.assertFalse(partition["filesystem_created"])
            with (output / "system.raw").open("rb") as stream:
                mbr = stream.read(512)
                self.assertEqual(mbr[510:], b"\x55\xaa")
                self.assertEqual(mbr[450], 0xee)
                self.assertEqual(struct.unpack_from("<II", mbr, 454), (1, size // 512 - 1))
                tables = []
                for lba, other in ((1, size // 512 - 1), (size // 512 - 1, 1)):
                    stream.seek(lba * 512)
                    header = bytearray(stream.read(512))
                    self.assertEqual(header[:8], b"EFI PART")
                    self.assertEqual(struct.unpack_from("<QQ", header, 24), (lba, other))
                    checksum, = struct.unpack_from("<I", header, 16)
                    struct.pack_into("<I", header, 16, 0)
                    self.assertEqual(zlib.crc32(header[:92]), checksum)
                    table_lba, count, entry_bytes, table_crc = struct.unpack_from("<QIII", header, 72)
                    self.assertEqual((count, entry_bytes), (128, 128))
                    stream.seek(table_lba * 512)
                    table = stream.read(count * entry_bytes)
                    self.assertEqual(zlib.crc32(table), table_crc)
                    tables.append(table)
                self.assertEqual(tables[0], tables[1])
                entry = tables[0][:128]
                self.assertEqual(entry[:16].hex(), "ef57347c0000aa11aa1100306543ecac")
                self.assertEqual(struct.unpack_from("<QQQ", entry, 32), (40, size // 512 - 41, 0))
                self.assertEqual(entry[56:].decode("utf-16le").rstrip("\0"), "Container")
                self.assertEqual(tables[0][128:], bytes(len(tables[0]) - 128))
                for offset in (40 * 512, size // 2, (size // 512 - 48) * 512):
                    stream.seek(offset)
                    self.assertEqual(stream.read(4096), bytes(4096))
            with self.assertRaises(FileExistsError):
                storage.create_storage(storage_tree(), output, size, "restore-container")

    def test_invalid_restore_layout_fails_before_creating_media(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "media"
            for size, layout in ((512, "restore-container"), ((64 << 20) + 512, "restore-container"),
                                 (64 << 20, "unknown")):
                with self.subTest(size=size, layout=layout), self.assertRaises(ValueError):
                    storage.create_storage(storage_tree(), output, size, layout)
                self.assertFalse(output.exists())

    def test_create_preserves_existing_paths_and_reads_latest_valid_bank(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "media"
            tree = storage_tree()
            record = storage.create_storage(tree, output, 64 << 20)
            self.assertFalse(record["restored"])
            for name, size in (("auxiliary.raw", 32 << 20), ("system.raw", 64 << 20), ("sep.raw", 512 << 10)):
                self.assertEqual((output / name).stat().st_size, size)
                self.assertEqual((output / name).stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                storage.create_storage(tree, output, 512)
            with (output / "auxiliary.raw").open("r+b") as stream:
                chosen = tree.child("chosen")
                first = storage.nvram_handoff(tree, stream)
                self.assertEqual((first["bank"], first["generation"]), (0, 1))
                self.assertEqual(chosen.get("nvram-bank-count"), struct.pack("<I", 2))
                newer = prep.blank_nvram(0x80000, 9)
                stream.seek(0xa80000)
                stream.write(newer)
                stream.flush()
                next_boot = storage.nvram_handoff(tree, stream)
                self.assertEqual((next_boot["bank"], next_boot["generation"]), (1, 9))
                self.assertEqual(chosen.get("nvram-proxy-data"), newer)
                stream.seek(0xa80000 + 100)
                stream.write(b"torn bank")
                stream.flush()
                fallback = storage.nvram_handoff(tree, stream)
                self.assertEqual((fallback["bank"], fallback["generation"]), (0, 1))
                stream.seek(0xa00000)
                stream.write(bytes(0x80000))
                stream.flush()
                with self.assertRaisesRegex(ValueError, "no valid CHRP"):
                    storage.nvram_handoff(tree, stream)

    def test_bad_layout_or_truncated_media_cannot_claim_persistent_nvram(self):
        tree = storage_tree()
        regions = tree.child("product").child("util")
        for table in (bytes(11), struct.pack("<6I", 2, 0xa00000, 0x100000, 2, 0xb00000, 512),
                      struct.pack("<6I", 1, 0, 0xb00000, 2, 0xa00000, 0x100000),
                      struct.pack("<3I", 2, 0, 0x100000)):
            regions.set("regions", table)
            with self.assertRaises(ValueError):
                storage.auxiliary_size(tree)
        with tempfile.TemporaryFile() as stream:
            stream.truncate(16 << 20)
            with self.assertRaisesRegex(ValueError, "cover all"):
                storage.nvram_handoff(storage_tree(), stream)


class FaultContextTests(unittest.TestCase):
    @staticmethod
    def header(cpu=0, pc="0x1000", kind="bus"):
        return (f"tomacco_arm_fault_context cpu={cpu} pc={pc} kind={kind} "
                "pstate=0x3c5 el=1 tcr_el1=0xabc ttbr0_el1=0x4000 ttbr1_el1=0x8000\n")

    @staticmethod
    def registers(cpu=0, pc="0x1000", first=0, seed=0):
        values = ",".join(hex(seed + i) for i in range(first, first + 4))
        return f"tomacco_arm_fault_registers cpu={cpu} pc={pc} first={first} values={values}\n"

    def test_interleaved_cpus_at_same_pc_keep_original_registers_and_stack(self):
        log = self.header() + self.header(cpu=1, kind="sysreg")
        for first in range(0, 32, 4):
            log += self.registers(first=first) + self.registers(cpu=1, first=first, seed=100)
        snapshots = run.fault_contexts(log)
        self.assertEqual(len(snapshots), 2)
        for cpu, snapshot in enumerate(snapshots):
            self.assertTrue(snapshot["complete"])
            self.assertEqual(snapshot["cpu"], cpu)
            self.assertEqual(snapshot["registers"]["x30"], hex(cpu * 100 + 30))
            self.assertEqual(snapshot["registers"]["sp"], hex(cpu * 100 + 31))
            self.assertNotIn("x31", snapshot["registers"])

    def test_repeated_pc_and_bad_or_missing_groups_cannot_complete_a_snapshot(self):
        log = self.registers() + self.header() + self.registers()
        log += self.registers(seed=100)  # duplicate must not replace original
        log += self.registers(pc="0x2000", first=4)
        log += self.registers(first=31) + self.registers(first=1)
        log += "tomacco_arm_fault_registers cpu=0 pc=0x1000 first=4 values=0x1\n"
        log += self.header() + self.registers(first=4)
        a, b = run.fault_contexts(log)
        self.assertFalse(a["complete"])
        self.assertFalse(b["complete"])
        self.assertEqual(a["registers"], {f"x{i}": hex(i) for i in range(4)})
        self.assertEqual(b["registers"], {f"x{i}": hex(i) for i in range(4, 8)})

    def test_snapshot_limit_does_not_modify_an_earlier_fault(self):
        log = "".join(self.header() + self.registers(seed=n * 100) for n in range(20))
        snapshots = run.fault_contexts(log)
        self.assertEqual(len(snapshots), 16)
        self.assertEqual(snapshots[-1]["registers"]["x0"], hex(1500))
        self.assertFalse(snapshots[-1]["complete"])


class DeviceTreeTests(unittest.TestCase):
    def test_vm_serial_is_stable_and_preserves_explicit_identity(self):
        tree = prep.decode(prep.prepare(source_tree().encode(), seed=bytes(range(256))))
        self.assertEqual(tree.get("serial-number"), b"PSV0706050403020100\0")
        self.assertFalse(prep.identity_handoff(tree)["added"])
        tree.set("serial-number", b"LOCAL-VM-1\0")
        self.assertEqual(prep.identity_handoff(tree)["serial"], "LOCAL-VM-1")
        del tree.props["serial-number"]
        self.assertTrue(prep.identity_handoff(tree)["added"])
        self.assertEqual(tree.get("serial-number"), b"PSV0706050403020100\0")
        for value in (b"", b"missing-nul", b"a\0b\0", b"\0", b"x" * 32 + b"\0", b"bad\nserial\0"):
            tree.set("serial-number", value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                prep.identity_handoff(tree)
        tree.set("serial-number", b"LOCAL-VM-1\0")
        tree.child("chosen").set("unique-chip-id", bytes(8))
        with self.assertRaises(ValueError):
            prep.identity_handoff(tree)

    def test_sep_handoff_is_explicit_and_requires_a_loader_extent(self):
        original = source_tree().encode()
        tree = prep.decode(prep.prepare(original, load_sep=True))
        sep = tree.child("arm-io").child("sep")
        self.assertEqual(sep.get("sepfw-loaded"), struct.pack("<I", 1))
        self.assertEqual(sep.get("sepfw-booted"), struct.pack("<I", 1))
        self.assertEqual(tree.child("chosen").child("memory-map").get("SEPFW"), bytes(16))
        self.assertNotIn("sep-booted", sep.props)  # service readiness belongs to the guest
        tree = prep.decode(prep.prepare(original))
        self.assertNotIn("sepfw-loaded", tree.child("arm-io").child("sep").props)
        self.assertNotIn("SEPFW", tree.child("chosen").child("memory-map").props)

    def test_roundtrip_preserves_flags_padding_and_nested_data(self):
        root = source_tree()
        root.props["odd"] = prep.Property(b"x", 0, b"\x11\x22\x33")
        encoded = root.encode()
        self.assertEqual(prep.decode(encoded).encode(), encoded)

    def test_userspace_sep_handoff_has_no_ap_preload_or_service_ready_flag(self):
        source = source_tree()
        source.child("arm-io").child("sep").set("sepfw-loaded", struct.pack("<I", 1))
        source.child("chosen").children.append(node("memory-map", SEPFW=bytes(16)))
        original = source.encode()
        tree = prep.decode(prep.prepare(original, load_sep=True, sep_handoff="userspace"))
        sep = tree.child("arm-io").child("sep")
        self.assertEqual(sep.get("sepfw-booted"), struct.pack("<I", 1))
        self.assertNotIn("sepfw-loaded", sep.props)
        self.assertEqual(tree.child("chosen").get("sepfw-load-at-boot"), bytes(4))
        self.assertNotIn("sep-booted", sep.props)
        self.assertEqual(tree.child("chosen").child("memory-map").get("SEPFW"), bytes(16))
        self.assertEqual(tree.child("chosen").get("osenvironment"), b"device-recovery".ljust(32, b"\0"))
        for options in ({"sep_handoff": "userspace"}, {"load_sep": True, "sep_handoff": "unknown"}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "SEP handoff"):
                prep.prepare(original, **options)

    def test_rejects_truncation_trailing_bytes_duplicates_and_unknown_expressions(self):
        encoded = source_tree().encode()
        for damaged in (encoded[:4], encoded[:-1], encoded + b"\0"):
            with self.subTest(size=len(damaged)), self.assertRaises(ValueError):
                prep.decode(damaged)
        root = source_tree()
        root.children.append(node("chosen"))
        with self.assertRaisesRegex(ValueError, "Duplicate child"):
            prep.decode(root.encode())
        root = source_tree()
        root.props["unknown"] = prep.Property(b"unsupported/expression", 0x80000000)
        with self.assertRaisesRegex(ValueError, "Unknown firmware"):
            prep.prepare(root.encode())

    def test_a_normal_boot_tree_can_take_the_restore_trees_device_identity(self):
        with tempfile.TemporaryDirectory(prefix="tomacco-entropy-") as directory:
            root = Path(directory)
            (root / "original").write_bytes(source_tree().encode())
            def command(name, *options):
                with patch.object(sys, "argv", ["prepare_ios_paravirt.py", str(root / "original"), str(root / name), *options]), \
                        patch("builtins.print"):
                    prep.main()
                return prep.decode((root / name).read_bytes()).child("chosen")
            first = command("restore", "--ap-cpus", "1")
            self.assertEqual(command("normal", "--ap-cpus", "2", "--entropy-from", str(root / "restore")).get("unique-chip-id"),
                             first.get("unique-chip-id"))
            self.assertNotEqual(command("other", "--ap-cpus", "1").get("unique-chip-id"), first.get("unique-chip-id"))

    def test_preparation_preserves_real_devices_and_resolves_firmware_expressions(self):
        original = source_tree().encode()
        prepared = prep.decode(prep.prepare(original, seed=bytes(range(256)), omit_optional_ctrr=True))
        self.assertEqual(prepared.child("chosen").get("chip-id"), struct.pack("<I", 0xfe01))
        self.assertEqual(prepared.child("chosen").get("board-id"), struct.pack("<I", 0x91))
        self.assertEqual(prepared.child("chosen").get("unique-chip-id"), bytes(range(8)))
        arm = prepared.child("arm-io")
        self.assertEqual(arm.child("sep").get("compatible"), b"iop-sep,vpiop\0")
        self.assertEqual(arm.child("gfx").encode(), source_tree().child("arm-io").child("gfx").encode())
        self.assertNotIn("avp-ctrr", [n.name for n in arm.children])
        self.assertEqual([n.name for n in prepared.child("cpus").children],
                         [f"cpu{i}" for i in range(6)])
        self.assertNotIn("kern.max_task_pmem", prepared.child("defaults").props)
        chosen = prepared.child("chosen")
        self.assertEqual(chosen.get("random-seed"), bytes(range(256)))
        self.assertEqual(chosen.get("dram-base"), struct.pack("<Q", 0x70000000))
        prep.validate_nvram(chosen.get("nvram-proxy-data"))
        self.assertEqual(original, source_tree().encode())
        _, bus, parent, length = struct.unpack("<IQQQ", arm.child("pcie").get("ranges"))
        self.assertEqual(bus, parent + 0x10000000)
        self.assertLessEqual(bus + length, prep.DRAM_BASE)
        value = struct.pack("<I", 4096)
        supplied = prep.decode(prep.prepare(original, properties={"/defaults/kern.max_task_pmem": value}))
        self.assertEqual(supplied.child("defaults").get("kern.max_task_pmem"), value)
        self.assertEqual(prep.firmware_references(supplied), {})

    def test_preparation_selects_a_valid_multicore_subset(self):
        original = source_tree().encode()
        self.assertEqual(prep.ap_cpu_count(prep.decode(prep.prepare(original))), 6)
        for count in (1, 2, 6, 8, 16):
            with self.subTest(count=count):
                prepared = prep.decode(prep.prepare(original, ap_cpus=count))
                self.assertEqual(prep.ap_cpu_count(prepared), count)
                self.assertEqual([cpu.name for cpu in prepared.child("cpus").children],
                                 [f"cpu{i}" for i in range(count)])
        for count in (0, 17, 32):
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, "AP CPU count"):
                prep.prepare(original, ap_cpus=count)

    def test_wrong_hardware_and_bad_overrides_fail_before_output(self):
        for option in ({"memory_gib": 1}, {"memory_gib": 33}, {"seed": bytes(64)},
                       {"properties": {"/arbitrary": b"x"}}):
            with self.subTest(option=option), self.assertRaises(ValueError):
                prep.prepare(source_tree().encode(), **option)
        for name in ("gfx", "aes", "gic", "pcie", "pl031-rtc", "avp-rtc",
                     "pl061-buttons", "pvpanic", "sep"):
            tree = source_tree()
            tree.child("arm-io").child(name).set("reg", bytes(32 if name == "aes" else 16))
            with self.assertRaises(ValueError):
                prep.prepare(tree.encode())
        for name in ("chip-id", "board-id"):
            tree = source_tree()
            tree.child("chosen").set(name, struct.pack("<I", 0x1234))
            with self.assertRaisesRegex(ValueError, name):
                prep.prepare(tree.encode())
        tree = source_tree()
        tree.set("compatible", b"not-vphone600ap\0")
        with self.assertRaisesRegex(ValueError, "only a vphone600"):
            prep.prepare(tree.encode())

    def test_nvram_rejects_corruption_and_invalid_partition_lengths(self):
        original = prep.blank_nvram()
        prep.validate_nvram(original)
        damaged = bytearray(original)
        damaged[-1] ^= 1
        with self.assertRaisesRegex(ValueError, "bank checksum"):
            prep.validate_nvram(damaged)
        damaged = bytearray(original)
        damaged[32:48] = prep.chrp_header(0x70, 0xffff * 16, b"common")
        struct.pack_into("<I", damaged, 16, zlib.adler32(damaged[20:]))
        with self.assertRaisesRegex(ValueError, "partition exceeds"):
            prep.validate_nvram(damaged)
        damaged = bytearray(original)
        damaged[1] ^= 1
        with self.assertRaisesRegex(ValueError, "partition checksum"):
            prep.validate_nvram(damaged)

    def test_cli_private_output_and_existing_files_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "original", root / "prepared"
            source.write_bytes(source_tree().encode())
            command = [sys.executable, REPO / "tools/prepare_ios_paravirt.py", source, target]
            completed = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertFalse(json.loads(completed.stdout)["qualified_for_app_development"])
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            saved = target.read_bytes()
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(target.read_bytes(), saved)
            self.assertEqual(source.read_bytes(), source_tree().encode())


class BuildStagingTests(unittest.TestCase):
    def test_qemu_patch_created_file_is_checked_with_unrelated_untracked_files(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "source"
            root.mkdir()
            def git(*args):
                return subprocess.run(["git", "-C", root, *args], check=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
            git("init")
            (root / "existing").write_text("base\n")
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "base")
            change = parent / "create.patch"
            change.write_text("diff --git a/generated/source.h b/generated/source.h\n"
                              "new file mode 100644\n--- /dev/null\n+++ b/generated/source.h\n"
                              "@@ -0,0 +1 @@\n+owned source\n")
            unrelated = root / "my-source"
            unrelated.write_text("preserve my work\n")
            original_index = (root / ".git/index").read_bytes()
            with contextlib.redirect_stdout(io.StringIO()):
                build.stage_patches(root, [change])
                build.stage_patches(root, [change], dry_run=True)
                build.stage_patches(root, [change])
                self.assertEqual((root / ".git/index").read_bytes(), original_index)
                self.assertEqual(unrelated.read_text(), "preserve my work\n")
                with self.assertRaisesRegex(RuntimeError, "Preserved untracked source"):
                    build.stage_patches(root, [change], reject_untracked=True)
                created = root / "generated/source.h"
                created.write_text("my edited source\n")
                with self.assertRaises(subprocess.CalledProcessError):
                    build.stage_patches(root, [change], dry_run=True)
                self.assertEqual(created.read_text(), "my edited source\n")
                self.assertEqual((root / ".git/index").read_bytes(), original_index)
                created.write_text("owned source\n")
                git("add", "my-source")
                staged_index = (root / ".git/index").read_bytes()
                with self.assertRaises(subprocess.CalledProcessError):
                    build.stage_patches(root, [change], dry_run=True)
                self.assertEqual((root / ".git/index").read_bytes(), staged_index)
                self.assertEqual(unrelated.read_text(), "preserve my work\n")

    def test_patch_created_source_is_repeatable_but_local_edits_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "source"
            root.mkdir()
            def git(*args):
                return subprocess.run(["git", "-C", root, *args], check=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
            git("init")
            (root / "existing").write_text("base\n")
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "base")
            change = parent / "create.patch"
            change.write_text("diff --git a/generated/texels.rs b/generated/texels.rs\n"
                              "new file mode 100644\n--- /dev/null\n+++ b/generated/texels.rs\n"
                              "@@ -0,0 +1 @@\n+owned source\n")
            with contextlib.redirect_stdout(io.StringIO()):
                build.stage_patches(root, [change], reject_untracked=True)
                original_index = (root / ".git/index").read_bytes()
                build.stage_patches(root, [change], reject_untracked=True)
                self.assertEqual((root / ".git/index").read_bytes(), original_index)
                git("add", "generated/texels.rs")
                staged_index = (root / ".git/index").read_bytes()
                build.stage_patches(root, [change], reject_untracked=True)
                self.assertEqual((root / ".git/index").read_bytes(), staged_index)
                (root / "generated/texels.rs").write_text("my edited source\n")
                with self.assertRaises(subprocess.CalledProcessError):
                    build.stage_patches(root, [change], reject_untracked=True)
                self.assertEqual((root / "generated/texels.rs").read_text(), "my edited source\n")
                self.assertEqual((root / ".git/index").read_bytes(), staged_index)
                (root / "unknown.rs").write_text("my staged addition\n")
                git("add", "unknown.rs")
                with self.assertRaisesRegex(RuntimeError, "Preserved untracked source"):
                    build.stage_patches(root, [change], reject_untracked=True)
                self.assertEqual((root / "unknown.rs").read_text(), "my staged addition\n")

    def test_graphics_patch_series_rejects_untracked_source_without_touching_it(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "source"
            root.mkdir()
            def git(*args):
                return subprocess.run(["git", "-C", root, *args], check=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
            git("init")
            (root / "mapper.rs").write_text("original\n")
            (root / ".gitignore").write_text("target/\n")
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "base")
            graphics = parent / "graphics.patch"
            graphics.write_text("".join(difflib.unified_diff(
                ["original\n"], ["ios mapper\n"], fromfile="a/mapper.rs", tofile="b/mapper.rs")))
            with contextlib.redirect_stdout(io.StringIO()):
                build.stage_patches(root, [graphics], reject_untracked=True)
                (root / "target").mkdir()
                (root / "target/library").write_bytes(b"ignored build output")
                build.stage_patches(root, [graphics], reject_untracked=True)
                index = (root / ".git/index").read_bytes()
                (root / "my_source.rs").write_text("preserve my work\n")
                with self.assertRaisesRegex(RuntimeError, "Preserved untracked source"):
                    build.stage_patches(root, [graphics], reject_untracked=True)
            self.assertEqual((root / ".git/index").read_bytes(), index)
            self.assertEqual((root / "my_source.rs").read_text(), "preserve my work\n")
            self.assertEqual((root / "mapper.rs").read_text(), "ios mapper\n")

    def test_exact_patch_stack_is_repeatable_and_preserves_unrelated_staged_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.run(["git", "-C", root, *args], check=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
            git("init")
            (root / "file").write_text("original\n")
            (root / "unrelated").write_text("keep\n")
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "base")
            patches = []
            for i, (before, after) in enumerate((("original\n", "middle\n"), ("middle\n", "final\n"))):
                file = root / f"change{i}.patch"
                file.write_text("".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                           fromfile="a/file", tofile="b/file")))
                patches.append(file)
            with patch.object(build, "PATCHES", patches), contextlib.redirect_stdout(io.StringIO()):
                build.stage_patches(root)
                build.stage_patches(root)
                self.assertEqual((root / "file").read_text(), "final\n")
                (root / "unrelated").write_text("user edit\n")
                git("add", "unrelated")
                index = (root / ".git/index").read_bytes()
                with self.assertRaises(subprocess.CalledProcessError):
                    build.stage_patches(root)
                self.assertEqual((root / ".git/index").read_bytes(), index)
                self.assertEqual((root / "unrelated").read_text(), "user edit\n")

    def test_staging_never_overwrites_files_or_follows_links(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "file"
            build.stage_file(target, b"local source")
            with self.assertRaises(RuntimeError):
                build.stage_file(target, b"different")
            link = Path(directory) / "link"
            link.symlink_to(target)
            with self.assertRaises(RuntimeError):
                build.stage_file(link, b"local source")
            self.assertEqual(target.read_bytes(), b"local source")


class LauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # A quiet host, sampled once: real I/O on this machine (a guest, a
        # build) must neither refuse nor stall these synthetic launches.
        (self.root / "pressure").mkdir()
        for resource in ("io", "memory"):
            (self.root / "pressure" / resource).write_text(
                "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
                "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        # No block devices either: with TMPDIR on a disk rather than tmpfs, the
        # launcher would otherwise sample that disk for seconds and judge the
        # host by whatever else is writing to it.
        (self.root / "sysfs").mkdir()
        environment = patch.dict(os.environ, {"TOMACCO_TEST_ROOT": str(self.root),
                                              "TOMACCO_IOS_PRESSURE_WAIT_SECONDS": "0",
                                              "TOMACCO_IOS_PRESSURE_DIRECTORY": str(self.root / "pressure"),
                                              "TOMACCO_IOS_SYSFS_DIRECTORY": str(self.root / "sysfs")})
        environment.start()
        self.addCleanup(environment.stop)
        self.engine = self.root / "engine"
        self.engine.write_text("#!/usr/bin/env python3\nimport os,time\n"
                               f"open({str(self.root / 'child.pid')!r},'w').write(str(os.getpid()))\n"
                               "time.sleep(60)\n")
        self.engine.chmod(0o700)
        self.tree = self.root / "tree"
        self.tree.write_bytes(prep.prepare(source_tree().encode()))
        self.payload = self.root / "synthetic-input"
        self.payload.write_bytes(b"not firmware; refused launch / process cleanup tests only")
        self.command = [sys.executable, REPO / "tools/run_ios_paravirt.py", "--engine", self.engine,
                        "--device-tree", self.tree, "--output", self.root / "output", "--seconds", "1"]
        for name in ("kernel", "sptm", "txm", "ramdisk", "trustcache"):
            self.command += ["--" + name, self.payload]

    def test_active_engine_lock_prevents_a_second_process(self):
        with (self.root / ".tomacco-vphone-session.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = subprocess.run(self.command, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already has an active session", result.stderr)
        self.assertFalse((self.root / "output").exists())
        self.assertFalse((self.root / "child.pid").exists())

    def test_an_engine_missing_host_libraries_is_refused_before_a_job_starts(self):
        tools = self.root / "fake-ldd"
        tools.mkdir()
        (tools / "ldd").write_text("#!/bin/sh\nprintf '\\tlinux-vdso.so.1 (0x0)\\n"
                                   "\\tlibnfs.so.16.2.0 => not found\\n'\n")
        (tools / "ldd").chmod(0o700)
        env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"]}
        result = subprocess.run(self.command, capture_output=True, text=True, timeout=5, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no longer has libnfs.so.16.2.0", result.stderr)
        self.assertFalse((self.root / "output").exists())
        self.assertFalse((self.root / "child.pid").exists())

    def test_guest_clock_selection_reaches_engine_and_receipt(self):
        self.engine.write_text("#!/bin/sh\nexit 0\n")
        for mode in ("wall", "instruction"):
            with self.subTest(mode=mode):
                output = self.root / ("clock-" + mode)
                result = subprocess.run(self.command + ["--output", output, "--clock-mode", mode],
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                receipt = json.loads((output / "request.json").read_text())
                self.assertEqual(receipt["clock_mode"], mode)
                command = receipt["command"]
                self.assertEqual("-icount" in command, mode == "instruction")
                self.assertEqual(command[command.index("-smp") + 1], "6")
                self.assertEqual(command[command.index("-accel") + 1],
                                 "tcg,thread=" + ("single" if mode == "instruction" else "multi"))
                self.assertEqual(receipt["ap_cpus"], 6)
                if mode == "instruction":
                    self.assertEqual(command[command.index("-icount") + 1], "shift=0,sleep=on")
                self.assertFalse(receipt["qualified_for_app_development"])

    def test_launcher_matches_qemu_topology_to_prepared_cpu_tree(self):
        self.engine.write_text("#!/bin/sh\nexit 0\n")
        selected_tree = self.root / "tree-6-cpus"
        selected_tree.write_bytes(prep.prepare(source_tree().encode(), ap_cpus=6))
        command = list(self.command)
        command[command.index("--device-tree") + 1] = str(selected_tree)
        output = self.root / "six-core-output"
        command[command.index("--output") + 1] = str(output)
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads((output / "request.json").read_text())
        self.assertEqual(receipt["ap_cpus"], 6)
        self.assertEqual(receipt["command"][receipt["command"].index("-smp") + 1], "6")

    def test_unknown_clock_is_rejected_before_launch(self):
        result = subprocess.run(self.command + ["--clock-mode", "fast"],
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid choice", result.stderr)
        self.assertFalse((self.root / "output").exists())
        self.assertFalse((self.root / "child.pid").exists())

    def test_translation_cache_selection_is_bounded_and_reaches_engine(self):
        self.engine.write_text("#!/bin/sh\nexit 0\n")
        for size in (None, 1, 2048):
            output = self.root / ("cache-" + str(size))
            args = [] if size is None else ["--translation-cache-mib", str(size)]
            result = subprocess.run(self.command + ["--output", output] + args,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads((output / "request.json").read_text())
            command = receipt["command"]
            expected = "tcg,thread=multi" + ("" if size is None else ",tb-size=" + str(size))
            self.assertEqual(command[command.index("-accel") + 1], expected)
            self.assertEqual(receipt["translation_cache_mib"], size)
            self.assertFalse(receipt["qualified_for_app_development"])
        for size in ("0", "-1", "4097", "1.5"):
            result = subprocess.run(self.command + ["--translation-cache-mib", size],
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--translation-cache-mib", result.stderr)
        self.assertFalse((self.root / "output").exists())

    def tcg_profile_engine(self):
        """A fake engine that writes the report the plugin would, at the
        path its -plugin option names (commas in that path doubled)."""
        report = {"format": "tomacco-tcg-profile", "version": 1, "kind": "final", "label": "",
                  "start_seconds": 0, "end_seconds": 1, "seconds": 1, "vcpus": 1, "blocks": 1,
                  "executed_blocks": 1, "page_bits": 12, "code_included": False, "estimate": "synthetic",
                  "totals": {"tb_execs": 10, "insns": 40, "kernel_insns": 40, "user_insns": 0,
                             "overflow_tb_execs": 0, "overflow_insns": 0},
                  "per_vcpu": [{"vcpu": 0, "tb_execs": 10, "insns": 40, "kernel_insns": 40, "user_insns": 0,
                                "idle_seconds": 0, "idle_entries": 0, "interrupts": 0, "exceptions": 0,
                                "hostcalls": 0, "vectors": {}}],
                  "classes": {"kernel": {"int": 40}, "user": {}}, "special": [], "exception_pcs": [],
                  "exception_pcs_overflow": 0, "pages_total": 1,
                  "pages": [{"page": "0xfffffe0007004000", "space": "kernel", "insns": 40, "share": 1,
                             "tb_execs": 10, "blocks": 1}],
                  "top_blocks": [{"rank": 1, "pc": "0xfffffe0007004000", "space": "kernel", "tb_execs": 10,
                                  "insns_per_tb": 4, "insns": 40, "share": 1, "cumulative": 1, "bytes": 16,
                                  "hash": "0x01234567", "translations": 1, "per_vcpu": [10],
                                  "mix": {"int": 4}, "special": []}]}
        self.engine.write_text(
            "#!/usr/bin/env python3\nimport json,re,sys\n"
            "value = sys.argv[sys.argv.index('-plugin') + 1]\n"
            "options = [part.replace('\\0', ',') for part in value.replace(',,', '\\0').split(',')]\n"
            "out = [o[4:] for o in options if o.startswith('out=')][0]\n"
            f"open(out, 'w').write(json.dumps({report!r}))\n")

    def test_tcg_profile_loads_the_plugin_and_renders_its_report(self):
        self.tcg_profile_engine()
        plugin = self.root / "prebuilt, plugin.so"
        plugin.write_bytes(b"not a plugin; the fake engine never loads it")
        output = self.root / "profiled"
        result = subprocess.run(self.command + ["--output", output, "--tcg-profile", "tcg-profile.json",
                                                "--tcg-profile-plugin", plugin, "--tcg-profile-interval", "5",
                                                "--tcg-profile-top", "50"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads((output / "result.json").read_text())
        command = record["command"]
        # A relative report path lands in the run's own directory.
        report = output / "tcg-profile.json"
        self.assertEqual(command[command.index("-plugin") + 1],
                         f"file={str(plugin).replace(',', ',,')},out={report},interval=5,top=50")
        profile = record["tcg_profile"]
        self.assertEqual(profile["report"], str(report))
        self.assertEqual(profile["mark"], str(output / "tcg-profile.mark"))
        self.assertEqual(profile["windows"], str(output / "tcg-profile.window-*.json"))
        self.assertEqual(profile["plugin"], str(plugin))
        self.assertEqual(profile["plugin_sha256"], hashlib.sha256(plugin.read_bytes()).hexdigest())
        self.assertFalse(profile["plugin_built"])
        self.assertEqual((profile["interval_seconds"], profile["top"], profile["code_included"]), (5, 50, False))
        self.assertIn("mark " + str(report), profile["window_request"])
        # The exit report is rendered next to itself and summarized; the
        # synthetic boot inputs are no Mach-O, which the summary says.
        self.assertTrue(profile["report_written"])
        self.assertNotIn("render_error", profile)
        self.assertEqual(profile["summary"]["totals"]["insns"], 40)
        self.assertEqual(profile["summary"]["top_blocks"][0]["where"], "kernel half (no image)")
        self.assertTrue(any(note.startswith("kernel: Not a raw 64-bit Mach-O")
                            for note in profile["summary"]["images"]["notes"]))
        text = (output / "tcg-profile.txt").read_text()
        self.assertIn("0xfffffe0007004000", text)
        self.assertEqual(record["outcome"], "emulator-exited")

    def test_tcg_profile_accepts_an_absolute_report_in_an_existing_directory(self):
        self.tcg_profile_engine()
        plugin = self.root / "plugin.so"
        plugin.write_bytes(b"x")
        reports = self.root / "reports"
        reports.mkdir()
        output = self.root / "absolute"
        result = subprocess.run(self.command + ["--output", output, "--tcg-profile", reports / "swipe.json",
                                                "--tcg-profile-plugin", plugin, "--tcg-profile-code"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads((output / "result.json").read_text())
        command = record["command"]
        self.assertEqual(command[command.index("-plugin") + 1],
                         f"file={plugin},out={reports / 'swipe.json'},code=on")
        self.assertTrue((reports / "swipe.txt").is_file())
        self.assertTrue(record["tcg_profile"]["code_included"])

    def test_without_tcg_profile_no_plugin_is_loaded(self):
        self.engine.write_text("#!/bin/sh\nexit 0\n")
        output = self.root / "unprofiled"
        result = subprocess.run(self.command + ["--output", output], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads((output / "request.json").read_text())
        self.assertNotIn("-plugin", record["command"])
        self.assertIsNone(record["tcg_profile"])

    def test_invalid_tcg_profile_requests_are_refused_before_launch(self):
        plugin = self.root / "plugin.so"
        plugin.write_bytes(b"x")
        existing = self.root / "existing.json"
        existing.write_text("{}")
        for extra, message in (
                (["--tcg-profile-interval", "5"], "require --tcg-profile"),
                (["--tcg-profile-code"], "require --tcg-profile"),
                (["--tcg-profile-plugin", plugin], "require --tcg-profile"),
                (["--tcg-profile", "profile.txt"], "must name a .json file"),
                (["--tcg-profile", "request.json"], "launcher's own files"),
                (["--tcg-profile", self.root / "missing" / "p.json"], "existing directory"),
                (["--tcg-profile", existing], "must name a new file"),
                (["--tcg-profile", "p.json", "--tcg-profile-interval", "0.5"], "between 1 and 3600"),
                (["--tcg-profile", "p.json", "--tcg-profile-top", "0"], "between 1 and 100000"),
                (["--tcg-profile", "p.json", "--tcg-profile-plugin", self.root / "absent.so"], "absent.so")):
            with self.subTest(extra=extra):
                result = subprocess.run(self.command + [str(item) for item in extra],
                                        capture_output=True, text=True, timeout=30)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse((self.root / "output").exists())
                self.assertFalse((self.root / "child.pid").exists())

    def test_control_plane_profile_key_names_the_launcher_report(self):
        # The control plane passes a relative --tcg-profile, which the
        # launcher places in its --output, and collects it from there.
        source = (REPO / "native/ios_emulator/paravirtual.rs").read_text()
        self.assertIn('const TCG_PROFILE: &str = "tcg-profile.json";', source)
        self.assertIn('argv.extend(["--tcg-profile".into(), TCG_PROFILE.into()]);', source)
        self.assertIn('"--tcg-profile-interval"', source)
        self.assertIn('output.join(TCG_PROFILE)', source)
        self.assertIn('output.join("tcg-profile.txt")', source)
        self.assertEqual(run.tcg_profile_report(Path("tcg-profile.json"), self.root / "out"),
                         self.root / "out" / "tcg-profile.json")

    def test_pointer_auth_and_console_paint_selection_reach_engine_and_receipt(self):
        seen = self.root / "paint-seen"
        self.engine.write_text("#!/usr/bin/env python3\nimport json,os\n"
                               f"open({str(seen)!r}, 'w').write(json.dumps("
                               "os.environ.get('REIMS_VGPU_EAGER_CONSOLE_PAINT')))\n")
        # A shell override never decides the paint; only the option does.
        inherited = {**os.environ, "REIMS_VGPU_EAGER_CONSOLE_PAINT": "1"}
        for index, (auth, paint) in enumerate(((None, None), ("helpers", "eager"), ("fast", "lazy"))):
            with self.subTest(auth=auth, paint=paint):
                output = self.root / f"auth-paint-{index}"
                args = ([] if auth is None else ["--pointer-auth", auth]) + \
                       ([] if paint is None else ["--console-paint", paint])
                result = subprocess.run(self.command + ["--output", output] + args,
                                        capture_output=True, text=True, timeout=5, env=inherited)
                self.assertEqual(result.returncode, 0, result.stderr)
                receipt = json.loads((output / "request.json").read_text())
                command = receipt["command"]
                self.assertEqual(receipt["pointer_auth"], auth or "fast")
                self.assertEqual(receipt["console_paint"], paint or "lazy")
                globals_ = [command[i + 1] for i, arg in enumerate(command) if arg == "-global"]
                self.assertEqual(globals_, ["max-arm-cpu.tomacco-pauth-fast=off"] if auth == "helpers" else [])
                self.assertEqual(json.loads(seen.read_text()), "1" if paint == "eager" else None)
        for option, value in (("--pointer-auth", "qarma5"), ("--console-paint", "never")):
            result = subprocess.run(self.command + [option, value], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid choice", result.stderr)
        self.assertFalse((self.root / "output").exists())

    def test_tcg_engine_options_reach_engine_and_receipt(self):
        self.engine.write_text("#!/bin/sh\nexit 0\n")
        cases = (([], [], ("keep", "engine", None)),
                 (["--bti", "off"], ["max-arm-cpu.tomacco-bti=off"], ("off", "engine", None)),
                 (["--jump-probe", "generic", "--wfe-wait-us", "0"],
                  ["max-arm-cpu.tomacco-jmp-probe=0", "max-arm-cpu.tomacco-wfe-wait-us=0"], ("keep", "generic", 0)),
                 (["--jump-probe", "inline", "--wfe-wait-us", "200", "--bti", "keep"],
                  ["max-arm-cpu.tomacco-jmp-probe=2", "max-arm-cpu.tomacco-wfe-wait-us=200"], ("keep", "inline", 200)),
                 (["--jump-probe", "lean"], ["max-arm-cpu.tomacco-jmp-probe=1"], ("keep", "lean", None)))
        for index, (args, expected, (bti, probe, wait)) in enumerate(cases):
            with self.subTest(args=args):
                output = self.root / f"tcg-options-{index}"
                result = subprocess.run(self.command + ["--output", output] + args,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                receipt = json.loads((output / "request.json").read_text())
                command = receipt["command"]
                globals_ = [command[i + 1] for i, arg in enumerate(command) if arg == "-global"]
                self.assertEqual(globals_, expected)
                self.assertEqual((receipt["bti"], receipt["jump_probe"], receipt["wfe_wait_us"]),
                                 (bti, probe, wait))
        for args in (["--bti", "on"], ["--jump-probe", "fast"], ["--wfe-wait-us", "-1"],
                     ["--wfe-wait-us", "100001"]):
            result = subprocess.run(self.command + args, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0, args)
        self.assertFalse((self.root / "output").exists())

    def test_boot_arguments_reject_truncation_and_controls_before_launch(self):
        for value in ("", "x" * 1024, "rd=md0\n", "rd=md0\t-v", "\u00e4"):
            result = subprocess.run(self.command + ["--boot-args=" + value],
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("1–1023 printable ASCII", result.stderr)
            self.assertFalse((self.root / "output").exists())
            self.assertFalse((self.root / "child.pid").exists())
        mismatch = subprocess.run(self.command + ["--boot-args", "debug=0x8 cpus=16 rd=md0"],
                                  capture_output=True, text=True, timeout=5)
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn("must match the prepared tree's 6 AP CPUs", mismatch.stderr)
        self.assertFalse((self.root / "output").exists())

    def test_system_volume_and_kernel_arguments_reach_recorded_private_handoff(self):
        source = self.root / "root_hash.im4p"
        source.write_bytes(SystemVolumeHandoffTests.im4p())
        original = self.tree.read_bytes()
        # Only exercise host argument construction: this executable is not QEMU.
        self.engine.write_text("#!/bin/sh\nexit 0\n")
        arguments = "rd=disk0 custom=a,b -v"
        result = subprocess.run(self.command + ["--system-volume-root-hash", source,
                                                "--boot-args", arguments],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads((self.root / "output/request.json").read_text())
        self.assertEqual(record["boot_args"], arguments)
        self.assertIn(",args=rd=disk0 custom=a,,b -v", record["command"][2])
        handoff = record["system_volume_handoff"]
        self.assertEqual(Path(handoff["path"]).read_bytes(), source.read_bytes())
        self.assertEqual(Path(handoff["path"]).stat().st_mode & 0o777, 0o600)
        tree = prep.decode(Path(record["inputs"]["dtree"]["path"]).read_bytes())
        self.assertEqual(tree.child("chosen").get("system-volume-auth-blob"), SystemVolumeHandoffTests.payload)
        self.assertEqual(self.tree.read_bytes(), original)
        self.assertFalse(record["qualified_for_app_development"])

    def test_the_display_profile_fills_in_the_trees_display_scale_and_records_it(self):
        placeholder = source_tree()
        placeholder.child("chosen").set("display-scale", struct.pack("<I", 0))
        self.tree.write_bytes(prep.prepare(placeholder.encode()))
        original = self.tree.read_bytes()
        # Only exercise host argument construction: this executable is not QEMU.
        self.engine.write_text("#!/bin/sh\nexit 0\n")
        result = subprocess.run(self.command + ["--display-profile", "1179x2556@60:65x141"],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads((self.root / "output/request.json").read_text())
        self.assertEqual(record["display_scale"], {"scale": 3, "pixels_per_inch": 460.7, "previous": 0,
                                                   "property_updated": True})
        handed = prep.decode(Path(record["inputs"]["dtree"]["path"]).read_bytes())
        self.assertEqual(handed.child("chosen").get("display-scale"), struct.pack("<I", 3))
        self.assertEqual(self.tree.read_bytes(), original)
        # Without a panel (the desktop mode list) the tree is left as it is.
        command = [str(part) if part != self.root / "output" else str(self.root / "output2") for part in self.command]
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads((self.root / "output2/request.json").read_text())
        self.assertIsNone(record["display_scale"])
        self.assertEqual(prep.decode(Path(record["inputs"]["dtree"]["path"]).read_bytes())
                         .child("chosen").get("display-scale"), struct.pack("<I", 0))
        # A tree that already names another scale is refused before the engine starts.
        foreign = source_tree()
        foreign.child("chosen").set("display-scale", struct.pack("<I", 2))
        self.tree.write_bytes(prep.prepare(foreign.encode()))
        command = [str(part) if part != self.root / "output" else str(self.root / "output3") for part in self.command]
        result = subprocess.run(command + ["--display-profile", "1179x2556@60:65x141"],
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already names display scale 2", result.stderr)
        self.assertFalse((self.root / "output3").exists())

    def test_invalid_system_volume_fails_before_engine_and_output_creation(self):
        result = subprocess.run(self.command + ["--system-volume-root-hash", self.payload],
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "output").exists())
        self.assertFalse((self.root / "child.pid").exists())

    def test_sep_firmware_and_handoff_tree_must_be_paired_before_launch(self):
        for load_sep in (False, True):
            self.tree.write_bytes(prep.prepare(source_tree().encode(), load_sep=load_sep))
            command = self.command + ([] if load_sep else ["--sep", self.payload])
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--load-sep", result.stderr)
            self.assertFalse((self.root / "output").exists())
            self.assertFalse((self.root / "child.pid").exists())

    def test_sep_manifest_without_processor_is_rejected_before_launch(self):
        result = subprocess.run(self.command + ["--sep-manifest", self.payload],
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--sep-manifest requires --sep", result.stderr)
        self.assertFalse((self.root / "output").exists())
        self.assertFalse((self.root / "child.pid").exists())

    def test_sep_measurement_requires_manifest_and_evidence_does_not_assert_trust(self):
        result = subprocess.run(self.command + ["--sep", self.payload, "--sep-measure-manifest"],
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--sep-measure-manifest requires --sep-manifest", result.stderr)
        self.assertFalse((self.root / "output").exists())
        self.assertFalse((self.root / "child.pid").exists())
        self.assertIsNone(run.guest_evidence(self.root)["sep_manifest_measurement"])
        digest = hashlib.sha384(BootManifestTests.manifest).hexdigest()
        (self.root / "host.log").write_text(
            f"TOMACCO_SEP_MANIFEST_MEASURED sha384={digest} "
            "signature_verified_by_host=false bootloader_policy_evaluated=false\n")
        observed = run.guest_evidence(self.root)["sep_manifest_measurement"]
        self.assertEqual(observed["sha384"], digest)
        self.assertFalse(observed["signature_verified_by_host"])
        self.assertFalse(observed["bootloader_policy_evaluated"])

    def test_sep_handoff_mode_must_match_before_launch(self):
        for prepared, requested in (("preloaded", "userspace"), ("userspace", "preloaded")):
            self.tree.write_bytes(prep.prepare(source_tree().encode(), load_sep=True, sep_handoff=prepared))
            result = subprocess.run(self.command + ["--sep", self.payload, "--sep-handoff", requested,
                                                    "--sep-boot-role", "restore"],
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("matching --sep-handoff", result.stderr)
            self.assertFalse((self.root / "child.pid").exists())
        result = subprocess.run(self.command + ["--sep-handoff", "userspace"],
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--sep-handoff requires --sep", result.stderr)
        self.assertFalse((self.root / "output").exists())

    def test_sep_storage_without_processor_is_rejected_before_launch(self):
        result = subprocess.run(self.command + ["--sep-storage", self.payload],
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--sep-storage requires --sep", result.stderr)
        self.assertFalse((self.root / "output").exists())

    def test_sep_boot_role_requires_processor_and_known_role(self):
        for role in ("normal", "restore", "invalid"):
            result = subprocess.run(self.command + ["--sep-boot-role", role],
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root / "output").exists())
            self.assertFalse((self.root / "child.pid").exists())

    def test_sep_storage_cannot_overwrite_inputs_even_through_hardlinks(self):
        self.payload.write_bytes(bytes(1024))
        alias = self.root / "storage"
        os.link(self.payload, alias)
        with self.assertRaisesRegex(ValueError, "separate from"):
            run.open_sep_storage(alias, (self.payload,))
        self.assertEqual(self.payload.read_bytes(), bytes(1024))

    def test_sep_storage_geometry_and_exclusive_lock(self):
        disk = self.root / "storage"
        for size in (0, 512, 1025, 1536):
            disk.write_bytes(bytes(size))
            with self.assertRaisesRegex(ValueError, "even number"):
                run.open_sep_storage(disk, ())
        disk.write_bytes(bytes(512 * 1024))
        with run.open_sep_storage(disk, ()):
            with self.assertRaisesRegex(ValueError, "active session"):
                run.open_sep_storage(disk, ())
        with run.open_sep_storage(disk, ()):
            pass  # A stopped session leaves no stale lock.

    def test_writable_disks_cannot_alias_inputs_each_other_or_sep(self):
        self.payload.write_bytes(bytes(1024))
        disk = self.root / "disk"
        os.link(self.payload, disk)
        result = subprocess.run(self.command + ["--writable-root-disk", disk],
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("separate from", result.stderr)
        disk.unlink()
        disk.write_bytes(bytes(1024))
        for extra in (["--writable-auxiliary-disk", disk], ["--auxiliary-disk", disk],
                      ["--sep", self.payload, "--sep-storage", disk]):
            self.tree.write_bytes(prep.prepare(source_tree().encode(), load_sep="--sep" in extra))
            result = subprocess.run(self.command + ["--writable-root-disk", disk] + extra,
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("separate from", result.stderr)
            self.assertFalse((self.root / "output").exists())
            self.assertFalse((self.root / "child.pid").exists())

    def test_writable_disk_mode_is_explicit_and_receipt_uses_locked_media(self):
        disk = self.root / "root,with-comma"
        disk.write_bytes(bytes(512))
        refused = subprocess.run(self.command + ["--root-disk", disk, "--writable-root-disk", disk],
                                 capture_output=True, text=True, timeout=5)
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse((self.root / "output").exists())
        result = subprocess.run(self.command + ["--writable-root-disk", disk],
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)  # No QMP in this process fixture.
        receipt = json.loads((self.root / "output/result.json").read_text())
        media = receipt["persistent_disks"]["tomacco-root"]
        self.assertEqual(media["bytes"], 512)
        self.assertEqual(media["sha256_before"], run.file_digest(disk))
        self.assertEqual(media["sha256_after"], media["sha256_before"])
        self.assertTrue(receipt["storage_hashes_complete"])
        self.assertEqual(receipt["read_only_disks"], {})
        # The launch's timing: hashing before the engine (every input and the
        # disk), when the engine started and exited, and the disk's hash after.
        timing = receipt["timing"]
        hashed = receipt["storage_hash_observations"]
        self.assertEqual(timing["output_hashing_bytes"], 512)
        self.assertEqual(timing["input_hashing_bytes"] + 512, sum(row["bytes"] for row in hashed))
        self.assertGreaterEqual(timing["input_hashing_seconds"], 0)
        self.assertLessEqual(timing["emulator_started"], timing["engine_exited"])
        command = receipt["command"]
        self.assertNotIn("enable=virtio_blk_*", command)
        self.assertFalse(receipt["trace_storage"])
        node = json.loads(command[command.index("-blockdev") + 1])
        self.assertFalse(node["read-only"])
        self.assertEqual(node["node-name"], "tomacco-root")
        self.assertEqual(node["file"]["locking"], "on")
        self.assertTrue(node["file"]["filename"].startswith("/proc/self/fd/"))
        self.assertIn("vmapple-virtio-blk-pci,variant=root,drive=tomacco-root", command)
        with run.open_writable_storage(disk, ()):
            pass

    def test_the_engine_may_write_one_uncompressed_frame_of_its_panel(self):
        # A frame the file limit cannot hold kills the emulator with SIGXFSZ when
        # a viewer asks for it; the engine reports the limit it was started with.
        seen = self.root / "file-limit"
        self.engine.write_text("#!/usr/bin/env python3\nimport resource\n"
                               f"open({str(seen)!r}, 'w').write(str(resource.getrlimit(resource.RLIMIT_FSIZE)[0]))\n")
        for index, (profile, frame) in enumerate((("1179x2556@60:65x141", 0),
                                                 ("4096x4096@60:140x140", 4096 * 4096 * 3 + 17))):
            seen.unlink(missing_ok=True)
            result = subprocess.run(self.command + ["--output", self.root / f"limit-{index}",
                                                    "--display-profile", profile],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            limit = int(seen.read_text())
            if frame:
                self.assertGreaterEqual(limit, frame)
            else:
                self.assertEqual(limit, 32 << 20)  # unchanged for a panel that fits

    def test_an_interactive_engine_is_handed_its_private_frame_mailbox(self):
        runtime = self.root / "runtime"
        runtime.mkdir(mode=0o700)
        seen = self.root / "mailbox-seen"
        self.engine.write_text("#!/usr/bin/env python3\nimport json,os\n"
                               "path = os.environ.get('REIMS_VGPU_FRAME_MAILBOX')\n"
                               "mode = os.stat(os.path.dirname(path)).st_mode & 0o777 if path else None\n"
                               f"open({str(seen)!r}, 'w').write(json.dumps([path, mode]))\n")
        inherited = {**os.environ, "XDG_RUNTIME_DIR": str(runtime),
                     "REIMS_VGPU_FRAME_MAILBOX": "/tmp/an-inherited-override"}
        for index, interactive in enumerate((True, False)):
            seen.unlink(missing_ok=True)
            output = self.root / f"mailbox-{index}"
            result = subprocess.run(self.command + ["--output", output]
                                    + (["--interactive"] if interactive else []),
                                    capture_output=True, text=True, timeout=10, env=inherited)
            self.assertEqual(result.returncode, 0, result.stderr)
            path, mode = json.loads(seen.read_text())
            request = json.loads((output / "request.json").read_text())
            if interactive:
                self.assertEqual(request["frame_mailbox"]["path"], path)
                self.assertEqual(request["frame_mailbox"]["pixel_format"], "bgra8")
                self.assertEqual(Path(path).parent.parent, runtime / "tomacco/ios-mailbox")
                self.assertEqual(mode, 0o700)
                # Removed with the session; the frames a viewer was handed live elsewhere.
                self.assertFalse(Path(path).parent.exists())
            else:
                # A session nobody watches gets none, and a shell override is dropped.
                self.assertIsNone(path)
                self.assertNotIn("frame_mailbox", request)

    def test_detailed_storage_trace_requires_explicit_diagnostic_option(self):
        disk = self.root / "disk"
        disk.write_bytes(bytes(512))
        self.engine.write_text("#!/bin/sh\nexit 0\n")
        subprocess.run(self.command + ["--root-disk", disk, "--trace-storage"],
                       capture_output=True, text=True, check=True, timeout=5)
        receipt = json.loads((self.root / "output/result.json").read_text())
        self.assertTrue(receipt["trace_storage"])
        self.assertIn("enable=virtio_blk_*", receipt["command"])

    def test_postprocessing_interruption_retains_guest_result_without_claiming_disk_hashes(self):
        disk = self.root / "disk"
        disk.write_bytes(bytes(512))
        self.engine.write_text("#!/bin/sh\nexit 0\n")
        actual_digest = run.file_digest
        media_reads = 0

        def digest(path, *, observation=None):
            nonlocal media_reads
            if str(path).startswith("/proc/self/fd/"):
                media_reads += 1
                if media_reads == 2:
                    raise KeyboardInterrupt("Interrupted after guest exit, during final disk hashing")
            return actual_digest(path, observation=observation)

        argv = list(map(str, self.command[1:] + ["--writable-root-disk", disk]))
        with patch.object(sys, "argv", argv), patch.object(run, "file_digest", side_effect=digest):
            with self.assertRaises(KeyboardInterrupt), contextlib.redirect_stdout(io.StringIO()):
                run.main()
        self.assertEqual(media_reads, 2)
        receipt = json.loads((self.root / "output/result.json").read_text())
        self.assertEqual(receipt["outcome"], "emulator-exited")
        self.assertFalse(receipt["storage_hashes_complete"])
        self.assertIsNone(receipt["persistent_disks"]["tomacco-root"]["sha256_after"])
        self.assertEqual(receipt["persistent_disks"]["tomacco-root"]["sha256_before"], actual_digest(disk))
        self.assertIn("guest_evidence", receipt)
        self.assertFalse(receipt["qualified_for_app_development"])
        self.assertEqual((self.root / "output/result.json").stat().st_mode & 0o777, 0o600)

    def test_launcher_refreshes_nvram_from_media_without_editing_prepared_tree(self):
        tree = storage_tree()
        media = self.root / "media"
        storage.create_storage(tree, media, 512)
        prepared = prep.prepare(tree.encode())
        self.tree.write_bytes(prepared)
        result = subprocess.run(self.command + ["--writable-auxiliary-disk", media / "auxiliary.raw"],
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)  # Synthetic process, no QMP.
        receipt = json.loads((self.root / "output/result.json").read_text())
        self.assertEqual(receipt["nvram_handoff"]["generation"], 1)
        self.assertEqual(receipt["nvram_handoff"]["prepared_tree"]["path"], str(self.tree))
        self.assertEqual(self.tree.read_bytes(), prepared)
        actual = prep.decode(Path(receipt["inputs"]["dtree"]["path"]).read_bytes())
        self.assertEqual(actual.child("chosen").get("nvram-bank-size"), struct.pack("<I", 0x80000))
        self.assertEqual(actual.child("chosen").get("nvram-bank-count"), struct.pack("<I", 2))
        self.assertEqual(actual.child("chosen").get("random-seed"), prep.decode(prepared).child("chosen").get("random-seed"))

    def test_sep_storage_receipt_records_mutable_media_separately(self):
        self.tree.write_bytes(prep.prepare(source_tree().encode(), load_sep=True))
        disk = self.root / "storage,with-comma"
        disk.write_bytes(bytes(1024))
        result = subprocess.run(self.command + ["--sep", self.payload, "--sep-storage", disk,
                                                "--sep-boot-role", "restore"],
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)  # Synthetic engine has no QMP.
        receipt = json.loads((self.root / "output/result.json").read_text())
        media = receipt["persistent_sep_storage"]
        self.assertEqual(media["sha256_before"], run.file_digest(disk))
        self.assertEqual(media["sha256_after"], media["sha256_before"])
        self.assertNotIn("sep-storage", receipt["inputs"])
        self.assertEqual(receipt["sep_boot_role"], "restore")
        self.assertTrue(any(",sep-role=restore" in arg for arg in receipt["command"]))
        self.assertTrue(any("file.locking=on,file=" in arg and "storage,,with-comma" in arg
                            for arg in receipt["command"]))
        with run.open_sep_storage(disk, ()):
            pass

    def test_sep_handoff_refuses_partial_or_stale_metadata_before_launch(self):
        for name, value in (("sepfw-loaded", b"\0" * 4), ("sepfw-booted", b""),
                            ("sepfw-booted", None), ("SEPFW", bytes(8)),
                            ("SEPFW", struct.pack("<QQ", 0x80000000, 0x4000))):
            tree = prep.decode(prep.prepare(source_tree().encode(), load_sep=True))
            target = tree.child("chosen").child("memory-map") if name == "SEPFW" else tree.child("arm-io").child("sep")
            if value is None:
                target.props.pop(name)
            else:
                target.set(name, value)
            self.tree.write_bytes(tree.encode())
            result = subprocess.run(self.command + ["--sep", self.payload], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root / "output").exists())
            self.assertFalse((self.root / "child.pid").exists())

    def test_startup_timeout_reaps_the_actual_child_and_records_failure(self):
        result = subprocess.run(self.command, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        receipt = json.loads((self.root / "output/result.json").read_text())
        self.assertEqual(receipt["outcome"], "observation-error")
        self.assertFalse(receipt["qualified_for_app_development"])
        child = int((self.root / "child.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)
        with (self.root / ".tomacco-vphone-session.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_guest_panic_and_first_bus_failure_are_explicit(self):
        (self.root / "uart.log").write_text("AMFI: Booted in a VM\r\ninterleaved log: panic(cpu 0): example\r\n")
        (self.root / "debug.log").write_text(
            "tomacco_arm_bus_fault pc=0x1234 physical=0x4006cc virtual=0xabc size=4 access=0 mmu=3 response=2\n")
        evidence = run.guest_evidence(self.root)
        self.assertEqual(evidence["kernel_panic"], "panic(cpu 0): example")
        self.assertEqual(evidence["bus_faults"][0]["physical"], "0x4006cc")
        self.assertTrue(evidence["amfi_identified_vm"])
        self.assertFalse(evidence["psci_driver_started"])
        self.assertNotIn("cpu", evidence["bus_faults"][0])  # older engine log
        self.assertEqual(evidence["fault_contexts"], [])

    def test_fault_cpu_identity_survives_the_guest_panic(self):
        (self.root / "uart.log").write_text("panic(cpu 0): secondary failure\n")
        (self.root / "debug.log").write_text(
            "tomacco_arm_bus_fault pc=0x1000 physical=0x600400 virtual=0xabc size=4 access=0 mmu=0 response=2 cpu=1\n"
            + FaultContextTests.header(cpu=1)
            + "".join(FaultContextTests.registers(cpu=1, first=i) for i in range(0, 32, 4))
            + "tomacco_arm_sysreg_fault pc=0x6810 instruction=0xd53bf208 el=1 cpu=1\n")
        evidence = run.guest_evidence(self.root)
        self.assertEqual(evidence["bus_faults"][0]["cpu"], "1")
        self.assertEqual(evidence["system_register_faults"][0]["cpu"], "1")
        self.assertTrue(evidence["fault_contexts"][0]["complete"])
        self.assertEqual(evidence["fault_contexts"][0]["registers"]["x8"], "0x8")

    def test_device_failure_remains_visible_without_a_kernel_panic(self):
        (self.root / "uart.log").write_text(
            "APV-IOSFC: success\nmount-complete volume ramdisk\nhello from launchd.1\n")
        (self.root / "debug.log").write_text(
            "tomacco-vphone SEP: start pc=0x6000000 args=0x4020000\n"
            "tomacco-vphone AES: hardware key selector 3 unavailable\n"
            "tomacco_arm_sysreg_fault pc=0x6810 instruction=0xd53bf208 el=1\n")
        evidence = run.guest_evidence(self.root)
        self.assertIsNone(evidence["kernel_panic"])
        self.assertEqual(evidence["system_register_faults"][0]["pc"], "0x6810")
        self.assertEqual(evidence["device_errors"], ["tomacco-vphone AES: hardware key selector 3 unavailable"])
        for key in ("iosurface_driver_initialized", "sep_firmware_started", "apfs_ramdisk_mounted", "launchd_started"):
            self.assertTrue(evidence[key])
        (self.root / "debug.log").write_text("cmd_data: No payload\ncmd_key: No payload\n")
        self.assertEqual(run.guest_evidence(self.root)["device_errors"], [])

    def test_system_volume_mount_does_not_claim_successful_root_authentication(self):
        (self.root / "uart.log").write_text(
            "BSD root: disk0, major 1, minor 0\n"
            "apfs_extract_root_hash_arm:14739: could not retrieve system-volume-auth-blob from device tree\n"
            "apfs_vfsop_mount:2875: disk1s1 failed to find named root snapshot: Need authenticator (81)\n"
            "apfs_log_op_with_proc:3227: disk1s1 mount-complete volume ExampleOS, requested by: kernel_task (pid 0)\n"
            "panic(cpu 0 caller 0xffff): rootvp not authenticated after mounting @bsd_init.c:979\n")
        evidence = run.guest_evidence(self.root)
        self.assertEqual(evidence["root_device"], "disk0")
        self.assertEqual(evidence["apfs_mounted_volumes"], [{"device": "disk1s1", "name": "ExampleOS"}])
        self.assertTrue(evidence["system_volume_root_hash_missing"])
        self.assertTrue(evidence["root_snapshot_lookup_failed"])
        self.assertTrue(evidence["root_volume_authentication_failed"])
        self.assertFalse(evidence["apfs_ramdisk_mounted"])
        self.assertFalse(evidence["launchd_started"])

    def test_original_iphone_loader_failure_is_not_a_completed_system_boot(self):
        (self.root / "uart.log").write_text(
            "authenticate_root_hash:699: disk1s1 successfully validated on-disk root hash\n"
            "libignition: 1:        hello: hello from launchd.1\n"
            "libignition: 1: cryptex1 sniff: ignition failed: 8\n"
            "dyld[1]: dyld cache '(null)' not loaded: syscall to map cache into shared region failed\n"
            "panic(cpu 0 caller 0xffff): initproc failed to start -- Library not loaded: /usr/lib/libSystem.B.dylib\n")
        evidence = run.guest_evidence(self.root)
        self.assertTrue(evidence["system_volume_hash_validated"])
        self.assertEqual(evidence["ignition_failure"], {"pid": 1, "stage": "cryptex1 sniff", "code": 8})
        self.assertEqual(evidence["dyld_cache_failure"]["pid"], 1)
        self.assertIn("syscall to map cache", evidence["dyld_cache_failure"]["detail"])
        self.assertTrue(evidence["launchd_started"])
        self.assertFalse(evidence["early_boot_completed"])
        self.assertIsNone(evidence["sealed_system_root"])
        self.assertIsNotNone(evidence["kernel_panic"])

    def test_grafts_and_launchd_tasks_do_not_hide_later_abort(self):
        (self.root / "uart.log").write_text(
            "authenticate_root_hash:699: os.dmgs1 successfully validated on-disk root hash\n"
            "libignition: 1: graft: os: grafted cryptex: dmg = /os.dmg, ignored error = 0\n"
            "libignition: 1: graft: app: grafted cryptex: dmg = /app.dmg, ignored error = 5\n"
            "dyld[1]: dyld cache mapped system-wide: customer, auth GOTs: unmapped\n"
            "com.apple.xpc.launchd|date <Notice>: hello, launchd UUID: example\n"
            "****** DONE EARLYBOOT SETUP ****\nCache sent to launchd successfully\n"
            "panic(cpu 0 caller 0xffff): initproc exited -- exit reason namespace 2 subcode 0x6\n")
        evidence = run.guest_evidence(self.root)
        self.assertEqual(evidence["cryptex_grafts"], [
            {"cryptex": "os", "content_hash_validated": True, "grafted": True},
            {"cryptex": "app", "content_hash_validated": False, "grafted": False}])
        for key in ("dyld_shared_cache_mapped", "launchd_service_manager_started",
                    "launchd_service_cache_validated", "early_boot_setup_completed"):
            self.assertTrue(evidence[key], key)
        self.assertFalse(evidence["early_boot_completed"])
        self.assertIsNotNone(evidence["kernel_panic"])
        self.assertFalse(evidence["system_volume_hash_validated"])

    def test_restore_progress_does_not_hide_guest_process_or_usb_failure(self):
        (self.root / "uart.log").write_text(
            "enabling USB finished: success (0)\nwaiting for host to trigger start of restore\n"
            "*** Terminating app due to uncaught exception 'NSInvalidArgumentException', reason: 'missing SerialNumber'\n"
            "Caught signal 6 (abrt), si_code=0\n")
        (self.root / "host.log").write_text("qemu-system-aarch64: vphone USB: host disconnected; reset required\n")
        evidence = run.guest_evidence(self.root)
        self.assertTrue(evidence["restore_usb_enabled"])
        self.assertTrue(evidence["restore_waiting_for_host"])
        self.assertEqual(len(evidence["userspace_crashes"]), 2)
        self.assertEqual(evidence["usb_host_disconnects"], 1)
        self.assertTrue(evidence["usb_reset_required_after_disconnect"])
        self.assertEqual(evidence["device_errors"], [])
        self.assertFalse(evidence["system_usb_configured"])
        (self.root / "host.log").write_text("vphone USB: invalid response frame\n")
        (self.root / "uart.log").write_text("USB configuration complete!\n")
        evidence = run.guest_evidence(self.root)
        self.assertEqual(evidence["device_errors"], ["vphone USB: invalid response frame"])
        self.assertTrue(evidence["system_usb_configured"])

    def test_original_sep_activity_and_storage_results_are_distinct_from_qualification(self):
        (self.root / "uart.log").write_text(
            "void AppleSEPManager::_notifyOSActiveGated(): SEP/OS is alive in 8 state\n"
            "void AppleSEPManager::endReseed()_block_invoke: FIPS reseed completed\n")
        (self.root / "debug.log").write_text(
            "tomacco-vphone VSTR: command=0 sector=1023 status=0\n"
            "tomacco-vphone VSTR: command=1 sector=0 status=0\n"
            "tomacco-vphone VSTR: command=0 sector=1024 status=1\n")
        evidence = run.guest_evidence(self.root)
        self.assertEqual(evidence["sep_os_active_state"], 8)
        self.assertTrue(evidence["sep_fips_reseed_completed"])
        self.assertEqual(evidence["sep_storage_reads"], 1)
        self.assertEqual(evidence["sep_storage_writes"], 1)
        self.assertEqual(evidence["sep_storage_failures"], 1)
        self.assertEqual(evidence["device_errors"], ["tomacco-vphone VSTR: command=0 sector=1024 status=1"])

    def test_launchd_reports_crashes_before_app_diagnostics_exist(self):
        prefix = "com.apple.xpc.launchd|2026-10-08 16:54:47.227158 (user/501/dev.tomacco.test [256]) <Notice>: "
        crash = prefix + "exited due to SIGTRAP | sent by exc handler[256], ran for 16564ms"
        (self.root / "uart.log").write_text(
            "****** DONE EARLYBOOT SETUP ****\n" + crash + "\r\r\n" + crash + "\n"
            + prefix + "service state: running\n")
        evidence = run.guest_evidence(self.root)
        self.assertTrue(evidence["early_boot_setup_completed"])
        self.assertEqual(evidence["userspace_crashes"], [crash])
        self.assertIsNone(evidence["kernel_panic"])
        for signal in ("SIGTERM", "SIGKILL", "SIGINT", "SIGSTOP", "SIGTRAP_UNKNOWN"):
            with self.subTest(signal=signal):
                (self.root / "uart.log").write_text(prefix + "exited due to " + signal + "\n")
                self.assertEqual(run.guest_evidence(self.root)["userspace_crashes"], [])

    def test_known_failures_do_not_fail_a_run_and_storms_are_named(self):
        def line(service, pid, end):
            return f"com.apple.xpc.launchd|2026-10-27 16:08:11.746702 ({service} [{pid}]) <Notice>: {end}"
        widget = "pid/137/com.apple.weather.widget.421E6A04-1FB2-44C1-97F7-C905FECD9762"
        rows = [line(widget, 200 + n, "exited with exit reason (namespace: 20 code: 0x3e9) - OS_REASON_WATCHDOG"
                     " | <RBSTerminateContext| domain:10 code:0x8BADF00D>") for n in range(6)]
        rows += [
            line("user/501/com.apple.intelligencetasksd", 634, "exited with exit reason (namespace: 18 code: 0x2)"
                 ' - OS_REASON_LIBSYSTEM | assertion failure: "matched == (match_result.error_code == AICMR_MATCH)"'),
            line("user/501/com.apple.locationd", 359, "exited due to SIGBUS | sent by exc handler[359], ran for 30221ms"),
            line("user/501/UIKitApplication:com.apple.chrono.WidgetRenderer-Default[2577][rb-legacy]", 640,
                 "exited with exit reason (namespace: 10 code: 0x8badf00d) - OS_REASON_SPRINGBOARD | <RBSTerminateContext|"),
            # Ordinary ends: RunningBoard's own lifecycle reason and a kill.
            line("user/501/com.apple.siri.metrics.AssetMetrics", 700,
                 "exited with exit reason (namespace: 15 code: 0x0) - OS_REASON_RUNNINGBOARD | <RBSTerminateContext|"),
            line("user/501/com.apple.locationd", 360, "exited due to SIGKILL | sent by launchd[1]"),
        ]
        (self.root / "uart.log").write_text("\n".join(rows) + "\n")
        evidence = run.guest_evidence(self.root)
        # locationd's dispatch crashes came from the engine's memory ordering,
        # which the engine series now fixes: another one is not accounted for.
        self.assertEqual((evidence["unaccounted_failures"], evidence["accounted_failures"]), (1, 8))
        self.assertEqual({(row["service"], row["kind"], row["count"], row["accounted"])
                          for row in evidence["userspace_failures"]}, {
            ("com.apple.weather.widget", "extension-watchdog", 6, "widget-render-deadline"),
            ("com.apple.intelligencetasksd", "assertion", 1, "libxpc-match-assertion"),
            ("com.apple.locationd", "SIGBUS", 1, None),
            ("com.apple.chrono.WidgetRenderer-Default", "frontboard-watchdog", 1, "frontboard-allowance")})
        self.assertEqual(evidence["respawn_storms"],
                         [{"service": "com.apple.weather.widget", "failures": 6, "accounted": True}])
        record = {"guest_evidence": evidence, "exit_code": 0, "registers": {}}
        self.assertEqual(run.run_outcome(record), "guest-process-crash")
        accounted_only = {**evidence, "unaccounted_failures": 0}
        self.assertEqual(run.run_outcome({**record, "guest_evidence": accounted_only}), "bounded-session")
        # Another service's page that fails its signature, an unexplained
        # abort and a developer's app out of scene time are not accounted for.
        rows += [
            line("user/501/com.apple.locationd", 81,
                 "exited with exit reason (namespace: 3 code: 0x2) - OS_REASON_CODESIGNING, ran for 195014ms"),
            line("user/501/com.apple.runningboardd", 82,
                 "exited with exit reason (namespace: 3 code: 0x2) - OS_REASON_CODESIGNING, ran for 5014ms"),
            line("user/501/com.apple.chronod", 118, "exited due to SIGABRT | sent by chronod[118], ran for 12392ms"),
            line("user/501/UIKitApplication:dev.example.app[1a2b][rb-legacy]", 900,
                 "exited with exit reason (namespace: 10 code: 0x8badf00d) - OS_REASON_FRONTBOARD | <RBSTerminateContext|"),
        ]
        (self.root / "uart.log").write_text("\n".join(rows) + "\n")
        evidence = run.guest_evidence(self.root)
        self.assertEqual(evidence["unaccounted_failures"], 5)
        self.assertEqual(sorted((row["service"], row["kind"]) for row in evidence["userspace_failures"]
                                if row["accounted"] is None),
                         [("com.apple.chronod", "SIGABRT"), ("com.apple.locationd", "SIGBUS"),
                          ("com.apple.locationd", "code-signing:0x2"),
                          ("com.apple.runningboardd", "code-signing:0x2"), ("dev.example.app", "frontboard-watchdog")])
        self.assertEqual(run.run_outcome({**record, "guest_evidence": evidence}), "guest-process-crash")

    def test_a_launchd_backlog_cut_off_by_the_end_of_a_run_is_counted(self):
        def line(time, end):
            return f"com.apple.xpc.launchd|2026-10-27 {time} (user/501/com.apple.example [9]) <Notice>: {end}"
        flush = lambda time, count: f"com.apple.xpc.launchd|2026-10-27 {time} <Notice>: Flushed {count} logs [limit: 20]"
        # A backlog written in full: three records under their earlier times.
        rows = [line("17:37:52.0", "service state: running"), flush("17:42:07.8", 3),
                line("17:37:52.2", "a"), line("17:37:52.3", "b"), line("17:37:52.4", "c")]
        (self.root / "uart.log").write_text("\n".join(rows) + "\n")
        self.assertEqual(run.guest_evidence(self.root)["launchd_serial_records_unwritten"], 0)
        # The run ended one record into a backlog of 1080.
        rows += [flush("17:47:09.4", 1080), line("17:43:48.2", "d"),
                 "tx_flush:1344: disk1 xid 17512 tx stats: # 400"]
        (self.root / "uart.log").write_text("\n".join(rows) + "\n")
        self.assertEqual(run.guest_evidence(self.root)["launchd_serial_records_unwritten"], 1079)
        # The span the records cover: the backlog's earlier times and the
        # flush's later one count, the clock's step at boot does not.
        # Nothing was recorded between the first backlog's last record and the
        # flush that announced it: the longest gap names what the search missed.
        coverage = run.guest_evidence(self.root)["launchd_serial_coverage"]
        self.assertEqual(coverage["longest_gap_seconds"], round(42 * 60 + 7.8 - (37 * 60 + 52.4), 3))
        rows = [line("01:00:00.0", "before the clock is set"), line("19:11:56.0", "init"),
                line("19:12:02.0", "a"), line("19:12:32.0", "last record")]
        (self.root / "uart.log").write_text("\n".join(rows) + "\n")
        self.assertEqual(run.guest_evidence(self.root)["launchd_serial_coverage"],
                         {"span_seconds": 36.0, "longest_gap_seconds": 30.0})
        (self.root / "uart.log").write_text("no launchd here\n")
        self.assertEqual(run.guest_evidence(self.root)["launchd_serial_records_unwritten"], 0)
        self.assertEqual(run.guest_evidence(self.root)["launchd_serial_coverage"],
                         {"span_seconds": 0.0, "longest_gap_seconds": 0.0})

    def test_original_restore_failure_survives_later_cleanup_crash(self):
        (self.root / "uart.log").write_text(
            "[09:40:04.0823-GMT]{3>6} CHECKPOINT END: RESTORED:[0x0609] load_sep_os\r\r\n"
            "CHECKPOINT BEGIN: RESTORED:[0x064E] clean_nand\n"
            "CHECKPOINT FAILURE:(FAILURE:35) RESTORED:[0x064E] clean_nand [0]D(failed to clean NAND)\r\n"
            "CHECKPOINT FAILURE:(FAILURE:35) RESTORED:[0x064E] clean_nand [0]D(failed to clean NAND)\n"
            "CHECKPOINT END: RESTORED:[0x067C] cleanup_boot_command\n"
            "Caught signal 11 (segv), si_code=2, si_addr=0x0\n")
        evidence = run.guest_evidence(self.root)
        self.assertEqual(evidence["restore_completed_steps"], ["load_sep_os", "cleanup_boot_command"])
        self.assertEqual(evidence["restore_failures"], [{"code": 35, "checkpoint": "0x064E",
                         "step": "clean_nand", "detail": "[0]D(failed to clean NAND)"}])
        self.assertEqual(len(evidence["userspace_crashes"]), 1)
        self.assertIsNone(evidence["kernel_panic"])
        (self.root / "uart.log").write_text(
            "CHECKPOINT END: (null):[0x061C] create_partitions\n"
            "CHECKPOINT FAILURE:(FAILURE:47) (null):[0x0658] format_media [0]D(failed to create APFS Filesystem)\n"
            "CHECKPOINT FAILURE:(FAILURE:47) RESTORED:[0x0677] perform_main_os_prepare\n")
        evidence = run.guest_evidence(self.root)
        self.assertEqual(evidence["restore_completed_steps"], ["create_partitions"])
        self.assertEqual([f["step"] for f in evidence["restore_failures"]],
                         ["format_media", "perform_main_os_prepare"])
        (self.root / "uart.log").write_text(
            "CHECKPOINT FAILURE:(FAILURE:-1) FIRMWARE:[0x1300] update_iBoot [0]D(Generic updater error: -1)\n")
        self.assertEqual(run.guest_evidence(self.root)["restore_failures"][0]["code"], -1)

    def test_asynchronous_image_failure_precedes_later_outer_checkpoint(self):
        (self.root / "uart.log").write_text(
            "[AsyncCP][End] Checkpoint restore_system_image finished with result: 106.\r\n"
            "[AsyncCP][End] Checkpoint ignored_success finished with result: 0.\n"
            "CHECKPOINT FAILURE:(FAILURE:14) RESTORED:[0x064E] outer_step [0]D(secondary failure)\n")
        evidence = run.guest_evidence(self.root)
        self.assertEqual([(f["step"], f["code"]) for f in evidence["restore_failures"]],
                         [("restore_system_image", 106), ("outer_step", 14)])

    def test_actual_sepos_panic_is_progress_evidence_and_still_a_failure(self):
        (self.root / "debug.log").write_text(
            "tomacco-vphone SEP mailbox 0 -> 1: 00088798001400fe 0000000000000000\n")
        (self.root / "uart.log").write_text(
            "panic(cpu 0 caller 0xffff): SEP Panic: :SEPO/BOOT: example\n"
            "Root task vers: AppleSEPOS-3151.100.111\n")
        evidence = run.guest_evidence(self.root)
        self.assertTrue(evidence["sep_boot_capsule_submitted"])
        self.assertEqual(evidence["sepos_version"], "AppleSEPOS-3151.100.111")
        self.assertIn("SEP Panic", evidence["kernel_panic"])
        self.assertFalse(evidence["launchd_started"])


class EngineExitTests(unittest.TestCase):
    def test_a_profile_generating_engine_gets_time_to_write_its_profile(self):
        # build_ios_paravirt.py --pgo generate engines write .gcda/.profraw at
        # exit; pgo-generate.json beside the engine says so. Others keep 5 s
        # (120 s with the TCG profile plugin).
        with tempfile.TemporaryDirectory() as directory:
            engine = Path(directory) / "qemu-system-aarch64"
            engine.write_bytes(b"")
            self.assertEqual(run.engine_exit_seconds(engine, None), 5)
            self.assertEqual(run.engine_exit_seconds(engine, object()), run.TCG_PROFILE_EXIT_SECONDS)
            (Path(directory) / run.PGO_MARKER).write_text("{}")
            self.assertEqual(run.engine_exit_seconds(engine, None), run.PGO_EXIT_SECONDS)
            self.assertGreaterEqual(run.PGO_EXIT_SECONDS, 60)


class ParavirtSchemaTests(unittest.TestCase):
    def test_every_key_paravirtual_rs_reads_is_in_the_operation_schema(self):
        # A start fails with "Unknown argument: arguments.tcg_profile" when
        # paravirtual.rs reads a key the schema does not admit.
        root = Path(__file__).resolve().parents[2]
        source = (root / "native/ios_emulator/paravirtual.rs").read_text()
        operations = json.loads((root / "assets/operations.json").read_text())
        operations = operations if isinstance(operations, list) else operations["operations"]
        schema = next(op for op in operations if op["name"] == "ios.emulator.paravirtual")["inputSchema"]["properties"]
        for key in ("tcg_profile", "tcg_profile_interval", "bti", "jump_probe", "wfe_wait_us", "target_page"):
            self.assertIn(f'"{key}"', source)
            self.assertIn(key, schema)

    def test_engine_options_admit_exactly_the_launchers_values(self):
        # A profile's boot.bti, boot.jump_probe and boot.wfe_wait_us reach the
        # launcher's --bti, --jump-probe and --wfe-wait-us through this schema;
        # the launcher's choices and range are the schema's. No default: an
        # absent key keeps the engine's own behaviour and the request key.
        root = Path(__file__).resolve().parents[2]
        operations = json.loads((root / "assets/operations.json").read_text())
        operations = operations if isinstance(operations, list) else operations["operations"]
        schema = next(op for op in operations if op["name"] == "ios.emulator.paravirtual")["inputSchema"]["properties"]
        self.assertEqual(schema["bti"]["type"], "string")
        self.assertEqual(schema["bti"]["enum"], ["keep", "off"])
        self.assertEqual(schema["jump_probe"]["type"], "string")
        self.assertEqual(schema["jump_probe"]["enum"], ["engine", *run.JUMP_PROBES])
        self.assertEqual((schema["wfe_wait_us"]["type"], schema["wfe_wait_us"]["minimum"],
                          schema["wfe_wait_us"]["maximum"]), ("integer", 0, 100000))
        self.assertEqual(schema["target_page"]["type"], "string")
        self.assertEqual(schema["target_page"]["enum"], ["engine", *run.TARGET_PAGES])
        for key in ("bti", "jump_probe", "wfe_wait_us", "target_page"):
            self.assertNotIn("default", schema[key])
        source = (root / "native/ios_emulator/paravirtual.rs").read_text()
        self.assertIn('argv.extend(["--wfe-wait-us".into(), wait.to_string()]);', source)

