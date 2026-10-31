"""Preparation boundaries; actual iOS program execution has separate acceptance."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import prepare_ios_emulator as prepare


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.destination = self.root / "payload"

    def test_payload_snapshot_preserves_source_and_records_exact_bytes(self):
        folder = self.source / "Grüße 日本語"
        folder.mkdir()
        file = folder / "program"
        file.write_bytes(b"\x00\xff\x80\x0a")
        file.chmod(0o555)
        before = file.stat()
        result = prepare.copy_guest_payload(self.source, self.destination)
        self.assertEqual((self.destination / "Grüße 日本語/program").read_bytes(), file.read_bytes())
        self.assertEqual(result, [{"path": "/tomacco/Grüße 日本語/program", "source_bytes": 4,
                                   "source_sha256": hashlib.sha256(file.read_bytes()).hexdigest()}])
        self.assertEqual(before.st_mtime_ns, file.stat().st_mtime_ns)
        self.assertEqual(file.stat().st_mode & 0o777, 0o555)

    def test_links_and_special_files_are_rejected(self):
        outside = self.root / "outside"
        outside.write_bytes(b"preserve")
        for kind in ("file-link", "directory-link", "fifo"):
            with self.subTest(kind=kind):
                file = self.source / kind
                if kind == "fifo":
                    os.mkfifo(file)
                else:
                    file.symlink_to(outside if kind == "file-link" else self.root)
                with self.assertRaises(RuntimeError):
                    prepare.copy_guest_payload(self.source, self.root / kind)
                file.unlink()
        self.assertEqual(outside.read_bytes(), b"preserve")

    def test_output_inside_input_and_empty_payload_are_rejected(self):
        with self.assertRaises(RuntimeError):
            prepare.copy_guest_payload(self.source, self.source / "recursive")
        with self.assertRaises(RuntimeError):
            prepare.copy_guest_payload(self.source, self.destination)

    def test_limits_and_ambiguous_names_are_rejected(self):
        (self.source / "program").write_bytes(b"0123456789")
        with patch.object(prepare, "MAX_PAYLOAD_BYTES", 9), self.assertRaises(RuntimeError):
            prepare.copy_guest_payload(self.source, self.destination)
        with patch.object(prepare, "MAX_PAYLOAD_ENTRIES", 0), self.assertRaises(RuntimeError):
            prepare.copy_guest_payload(self.source, self.root / "entry-limit")
        (self.source / "bad\nname").write_bytes(b"x")
        with self.assertRaisesRegex(RuntimeError, "control characters"):
            prepare.copy_guest_payload(self.source, self.root / "bad-name")
