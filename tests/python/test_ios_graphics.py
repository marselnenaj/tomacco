"""Checksummed guest pixel transport, PNG fidelity and incomplete-run semantics."""
import binascii
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import extract_ios_graphics as graphics


class GraphicsExtractionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.output = self.root / "graphics.log", self.root / "frames"
        self.pixels = bytes((17, 34, 51, 255)) * (graphics.WIDTH * graphics.HEIGHT)

    def log(self, renderer="coregraphics", data=None, compressed=None):
        data = self.pixels if data is None else data
        crc = f"{binascii.crc32(data):08x}"
        encoded = zlib.compress(data, 1) if compressed is None else compressed
        return (f"TOMACCO_GRAPHICS_BEGIN renderer={renderer} width=256 height=384 stride=1024 format=bgra8-premultiplied encoding=zlib bytes={len(data)} encoded_bytes={len(encoded)} crc32={crc}\n" +
                "".join(f"TOMACCO_GRAPHICS_DATA offset={offset} hex={encoded[offset:offset + 256].hex()}\n"
                        for offset in range(0, len(encoded), 256)) +
                f"TOMACCO_GRAPHICS_END renderer={renderer} bytes={len(data)} crc32={crc}\n").encode()

    def test_exact_bytes_png_channels_and_checksums(self):
        raw = self.log() + self.log("calayer") + b"TOMACCO_GRAPHICS finished=1 frames=2\n"
        self.source.write_bytes(raw)
        record = graphics.extract(self.source, self.output)
        self.assertTrue(record["finished"])
        self.assertEqual(record["profile"], "layers")
        self.assertEqual(record["control_states"], [])
        self.assertFalse(record["main_display_qualified"])
        self.assertFalse(record["graphical_apps_qualified"])
        self.assertEqual(record["source_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(json.loads((self.output / "manifest.json").read_text()), record)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        for frame in record["frames"]:
            for entry in frame["files"].values():
                file = self.output / entry["path"]
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(), entry["sha256"])
                self.assertEqual(file.stat().st_size, entry["bytes"])
            self.assertEqual((self.output / frame["files"]["bgra"]["path"]).read_bytes(), self.pixels)
            png = (self.output / frame["files"]["png"]["path"]).read_bytes()
            self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
            offset, image = 8, bytearray()
            kinds = []
            while offset < len(png):
                size, = struct.unpack_from(">I", png, offset)
                kind, payload = png[offset + 4:offset + 8], png[offset + 8:offset + 8 + size]
                crc, = struct.unpack_from(">I", png, offset + 8 + size)
                self.assertEqual(crc, binascii.crc32(kind + payload))
                if kind == b"IDAT":
                    image.extend(payload)
                kinds.append(kind)
                offset += size + 12
            self.assertEqual(kinds, [b"IHDR", b"IDAT", b"IEND"])
            self.assertEqual(zlib.decompress(image), (b"\0" + bytes((51, 34, 17)) * 256) * 384)

    def test_completed_first_frame_is_retained_without_claiming_finished(self):
        self.source.write_bytes(self.log() + b"TOMACCO_GRAPHICS stage=load-quartzcore\n")
        result = graphics.extract(self.source, self.output)
        self.assertFalse(result["finished"])
        self.assertEqual(len(result["frames"]), 1)

    def uikit_log(self):
        return (b"TOMACCO_GRAPHICS_PROFILE profile=uikit frames=4\n" + self.log() + self.log("calayer") +
                b"TOMACCO_GRAPHICS_STATE phase=before action_count=0 switch_on=0\n" + self.log("uikit-before") +
                b"TOMACCO_GRAPHICS_STATE phase=after action_count=1 switch_on=1\n" + self.log("uikit-after") +
                b"TOMACCO_GRAPHICS finished=1 frames=4\n")

    def test_uikit_profile_requires_real_control_state_transition(self):
        self.source.write_bytes(self.uikit_log())
        result = graphics.extract(self.source, self.output)
        self.assertTrue(result["finished"])
        self.assertEqual(result["profile"], "uikit")
        self.assertEqual(result["expected_renderers"], list(graphics.PROFILES["uikit"]))
        self.assertEqual([frame["renderer"] for frame in result["frames"]], result["expected_renderers"])
        self.assertEqual(result["control_states"], [
            {"phase": "before", "action_count": 0, "switch_on": False},
            {"phase": "after", "action_count": 1, "switch_on": True},
        ])
        self.assertFalse(result["graphical_apps_qualified"])

    def test_uikit_failure_retains_prior_frames_with_incomplete_profile(self):
        self.source.write_bytes(b"TOMACCO_GRAPHICS_PROFILE profile=uikit frames=4\n" + self.log() +
                                self.log("calayer") + b"TOMACCO_GRAPHICS stage=create-uiview\n")
        result = graphics.extract(self.source, self.output)
        self.assertFalse(result["finished"])
        self.assertEqual(result["profile"], "uikit")
        self.assertEqual(len(result["frames"]), 2)
        self.assertEqual(result["control_states"], [])

    def test_failed_action_preserves_observed_state_without_qualification(self):
        raw = self.uikit_log().split(self.log("uikit-after"))[0]
        raw = raw.replace(b"phase=after action_count=1 switch_on=1", b"phase=after action_count=0 switch_on=0")
        self.source.write_bytes(raw + b"TOMACCO_GRAPHICS uikit_error=27\n")
        result = graphics.extract(self.source, self.output)
        self.assertFalse(result["finished"])
        self.assertEqual(len(result["frames"]), 3)
        self.assertEqual(result["control_states"][-1], {"phase": "after", "action_count": 0, "switch_on": False})

    def test_uikit_metadata_cannot_overstate_success(self):
        raw = self.uikit_log()
        header = b"TOMACCO_GRAPHICS_PROFILE profile=uikit frames=4\n"
        before = b"TOMACCO_GRAPHICS_STATE phase=before action_count=0 switch_on=0\n"
        after = b"TOMACCO_GRAPHICS_STATE phase=after action_count=1 switch_on=1\n"
        cases = {
            "missing_profile": raw.replace(header, b""),
            "unknown_profile": raw.replace(b"profile=uikit", b"profile=unknown"),
            "duplicate_profile": header + raw,
            "late_profile": b"TOMACCO_GRAPHICS stage=load-coregraphics\n" + raw,
            "profile_count": raw.replace(b"profile=uikit frames=4", b"profile=uikit frames=2"),
            "missing_before": raw.replace(before, b""),
            "missing_after": raw.replace(after, b""),
            "state_twice": raw.replace(before, before + before),
            "unexpected_action": raw.replace(b"action_count=0", b"action_count=1"),
            "missing_action": raw.replace(b"action_count=1", b"action_count=0"),
            "wrong_switch": raw.replace(b"switch_on=1", b"switch_on=0"),
            "unexpected_phase": raw.replace(b"phase=after", b"phase=other"),
            "extra_state": raw.replace(after, after + after),
            "misplaced_state": before + raw.replace(before, b""),
            "premature_summary": header + self.log() + self.log("calayer") + b"TOMACCO_GRAPHICS finished=1 frames=2\n",
            "summary_count": raw.replace(b"finished=1 frames=4", b"finished=1 frames=2"),
            "summary_malformed": raw.replace(b"finished=1 frames=4", b"finished=1 frames=four"),
        }
        for name, damaged in cases.items():
            with self.subTest(name=name):
                self.source.write_bytes(damaged)
                with self.assertRaises(ValueError):
                    graphics.extract(self.source, self.output)
                self.assertFalse(self.output.exists())

    def test_transport_damage_is_rejected_before_creating_output(self):
        raw = self.log()
        cases = {
            "corrupt": raw.replace(f"{binascii.crc32(self.pixels):08x}".encode(), b"00000000"),
            "zlib_corrupt": self.log(compressed=b"bad zlib"),
            "expansion_budget": self.log(compressed=zlib.compress(b"x" * (graphics.SIZE * 2))),
            "trailing_compressed_data": self.log(compressed=zlib.compress(self.pixels) + b"extra"),
            "truncated_zlib": self.log(compressed=zlib.compress(self.pixels)[:-2]),
            "reordered": raw.replace(b"offset=256 ", b"offset=512 ", 1),
            "duplicate": raw + raw,
            "wrong_renderer": raw.replace(b"renderer=coregraphics", b"renderer=calayer"),
            "unknown_renderer": raw.replace(b"renderer=coregraphics", b"renderer=../../../x"),
            "geometry": raw.replace(b"width=256", b"width=999999999"),
            "size": raw.replace(b"bytes=393216", b"bytes=4294967295"),
            "compressed_budget": raw.replace(b"encoded_bytes=", b"encoded_bytes=999999"),
            "truncated": raw[:len(raw) // 2],
            "interleaved": raw.replace(b"TOMACCO_GRAPHICS_DATA offset=256", b"unrelated console log\nTOMACCO_GRAPHICS_DATA offset=256"),
            "bad_summary": raw + b"TOMACCO_GRAPHICS finished=1 frames=2\n",
            "empty": b"TOMACCO_GRAPHICS stage=load-coregraphics\n",
            "diagnostic_inside_frame": raw.replace(b"TOMACCO_GRAPHICS_DATA offset=256", b"TOMACCO_GRAPHICS failed=1\nTOMACCO_GRAPHICS_DATA offset=256"),
            "after_completion": raw + self.log("calayer") + b"TOMACCO_GRAPHICS finished=1 frames=2\n" + raw,
            "budget": b"x" * (graphics.MAX_LOG + 1),
        }
        for name, damaged in cases.items():
            with self.subTest(name=name):
                self.source.write_bytes(damaged)
                with self.assertRaises(ValueError):
                    graphics.extract(self.source, self.output)
                self.assertFalse(self.output.exists())

    def test_alpha_contract_and_existing_output_are_preserved(self):
        self.source.write_bytes(self.log(data=bytes((1, 2, 3, 0)) * (graphics.WIDTH * graphics.HEIGHT)))
        with self.assertRaisesRegex(ValueError, "opaque"):
            graphics.extract(self.source, self.output)
        self.assertFalse(self.output.exists())
        self.source.write_bytes(self.log())
        self.output.mkdir()
        marker = self.output / "preserve"
        marker.write_bytes(b"user data")
        with self.assertRaisesRegex(ValueError, "already exists"):
            graphics.extract(self.source, self.output)
        self.assertEqual(marker.read_bytes(), b"user data")
        self.output = self.root / "dangling"
        self.output.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "already exists"):
            graphics.extract(self.source, self.output)
        self.assertFalse((self.root / "missing").exists())


if __name__ == "__main__":
    unittest.main()
