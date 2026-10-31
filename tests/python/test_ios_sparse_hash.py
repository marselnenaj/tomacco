"""Full logical-byte digest contracts; fixtures are not native media evidence."""
import errno
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run_ios_paravirt as run


class SparseHashTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "media"

    def digest(self, payload, seek=None):
        self.path.write_bytes(payload)
        observation = {}
        with patch.object(run, "LARGE_HASH_BYTES", 1), \
             patch.object(run, "HASH_READ_BYTES", 4096), \
             patch.object(run, "HASH_BYTES_PER_SECOND", 1 << 40):
            if seek is None:
                actual = run.file_digest(self.path, observation=observation)
            else:
                with patch.object(run.os, "lseek", side_effect=seek):
                    actual = run.file_digest(self.path, observation=observation)
        self.assertEqual(actual, hashlib.sha256(payload).hexdigest())
        self.assertTrue(observation["complete"])
        self.assertEqual(observation["bytes"], len(payload))
        self.assertEqual(observation["read_bytes"] + observation["sparse_zero_bytes"], len(payload))
        return observation

    @staticmethod
    def middle_data(_fd, offset, kind):
        if kind == os.SEEK_DATA:
            if offset < 4096:
                return 4096
            if offset < 8192:
                return offset
            raise OSError(errno.ENXIO, "no further data")
        return 8192

    def test_leading_and_trailing_holes_hash_the_exact_logical_bytes(self):
        row = self.digest(bytes(4096) + b"A" * 4096 + bytes(4096), self.middle_data)
        self.assertEqual(row["read_bytes"], 4096)
        self.assertEqual(row["sparse_zero_bytes"], 8192)
        self.assertEqual(row["sparse_extent_queries"], 4)

    def test_all_holes_require_no_file_data_reads(self):
        def no_data(*_):
            raise OSError(errno.ENXIO, "no data")
        row = self.digest(bytes(16385), no_data)
        self.assertEqual(row["read_bytes"], 0)
        self.assertEqual(row["sparse_zero_bytes"], 16385)

    def test_filesystem_that_reports_every_byte_as_data_is_supported(self):
        payload = b"a" * 8192 + bytes(8192)
        row = self.digest(payload, lambda _fd, offset, kind:
                          offset if kind == os.SEEK_DATA else len(payload))
        self.assertEqual(row["read_bytes"], len(payload))
        self.assertEqual(row["sparse_zero_bytes"], 0)

    def test_unsupported_extent_queries_fall_back_to_complete_reads(self):
        for code in (errno.EINVAL, errno.ENOTSUP, errno.ENOSYS):
            with self.subTest(code=code):
                def unsupported(*_, value=code):
                    raise OSError(value, "unsupported")
                row = self.digest(b"abc" * 5000, unsupported)
                self.assertEqual(row["sparse_extent_state"], "unsupported")
                self.assertEqual(row["read_bytes"], 15000)

    def test_midstream_fallback_does_not_repeat_or_omit_the_hashed_prefix(self):
        calls = 0
        def unsupported_later(_fd, _offset, _kind):
            nonlocal calls
            calls += 1
            if calls == 1:
                return 4096
            raise OSError(errno.EOPNOTSUPP, "unsupported later")
        row = self.digest(bytes(4096) + b"B" * 8193, unsupported_later)
        self.assertEqual(row["sparse_zero_bytes"], 4096)
        self.assertEqual(row["read_bytes"], 8193)
        self.assertEqual(row["sparse_extent_state"], "unsupported")

    def test_extent_query_budget_falls_back_at_the_current_offset(self):
        with patch.object(run, "SPARSE_HASH_QUERY_LIMIT", 1):
            row = self.digest(bytes(4096) + b"C" * 4096, self.middle_data)
        self.assertEqual(row["sparse_extent_queries"], 1)
        self.assertEqual(row["sparse_extent_state"], "query-limit")
        self.assertEqual(row["sparse_zero_bytes"], 4096)
        self.assertEqual(row["read_bytes"], 4096)

    def test_missing_sparse_api_uses_complete_reads(self):
        with patch.object(run.os, "SEEK_DATA", None):
            row = self.digest(b"D" * 4097)
        self.assertEqual(row["sparse_extent_state"], "api-unavailable")
        self.assertEqual(row["read_bytes"], 4097)

    def test_invalid_ranges_are_refused(self):
        for data, hole in ((-1, 4096), (4097, 4097), (0, 0), (0, 4097)):
            with self.subTest(data=data, hole=hole):
                with self.assertRaisesRegex(ValueError, "Invalid sparse"):
                    self.digest(b"E" * 4096, lambda _fd, _offset, kind:
                                data if kind == os.SEEK_DATA else hole)

    def test_unexpected_io_error_is_not_treated_as_a_hole_or_fallback(self):
        def failed(*_):
            raise OSError(errno.EIO, "I/O failure")
        with self.assertRaises(OSError) as error:
            self.digest(b"F" * 4096, failed)
        self.assertEqual(error.exception.errno, errno.EIO)

    def test_pacing_applies_to_data_reads_and_does_not_charge_zero_holes(self):
        payload = bytes(4096) + b"G" * 4096 + bytes(4096)
        self.path.write_bytes(payload)
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        row = {}
        with patch.object(run, "LARGE_HASH_BYTES", 1), \
             patch.object(run, "HASH_READ_BYTES", 4096), \
             patch.object(run, "HASH_BYTES_PER_SECOND", 4096), \
             patch.object(run.os, "lseek", side_effect=self.middle_data), \
             patch.object(run.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(run.time, "sleep", side_effect=sleep):
            actual = run.file_digest(self.path, observation=row)
        self.assertEqual(actual, hashlib.sha256(payload).hexdigest())
        self.assertEqual(row["pacing_wait_seconds"], 1.0)
        self.assertEqual(row["read_bytes_per_second_limit"], 4096)

    def test_real_sparse_file_matches_ordinary_byte_hash(self):
        with self.path.open("wb") as stream:
            stream.seek(8192)
            stream.write(b"H" * 4096)
            stream.seek(45056)
            stream.write(b"I" * 1024)
            stream.truncate(65536)
        expected = hashlib.sha256(self.path.read_bytes()).hexdigest()
        row = {}
        with patch.object(run, "LARGE_HASH_BYTES", 1), \
             patch.object(run, "HASH_BYTES_PER_SECOND", 1 << 40):
            actual = run.file_digest(self.path, observation=row)
        self.assertEqual(actual, expected)
        self.assertEqual(row["bytes"], 65536)
        self.assertEqual(row["read_bytes"] + row["sparse_zero_bytes"], 65536)

    def test_changes_during_zero_hashing_are_refused(self):
        self.path.write_bytes(bytes(8192))
        initial = self.path.stat()
        def changed(*_):
            self.path.write_bytes(bytes(8192))
            os.utime(self.path, ns=(initial.st_atime_ns, initial.st_mtime_ns + 1000000000))
            raise OSError(errno.ENXIO, "no data")
        with patch.object(run, "LARGE_HASH_BYTES", 1), \
             patch.object(run.os, "lseek", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "File changed"):
                run.file_digest(self.path)


if __name__ == "__main__":
    unittest.main()
