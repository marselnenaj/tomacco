"""The virtual iPhone's screen recording (tools/ios_virtual_record.py): the
pacing of frames into the video, the frames read through the CLI, and with a
real ffmpeg, an MP4 made from synthetic frames. No guest is booted."""
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

for directory in ("tools", "tests"):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / directory))
import ios_virtual_record  # noqa: E402
from ios_virtual_record import RecordingError, record  # noqa: E402


class Clock:
    """A clock that a sleep or a slow poll moves forward."""

    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, seconds)


def ppm(width, height, shade):
    return f"P6\n{width} {height}\n255\n".encode() + bytes([shade]) * (width * height * 3)


class Pacing(unittest.TestCase):
    def test_every_moment_of_the_recording_gets_the_latest_frame(self):
        clock, written = Clock(), []
        frames = iter([b"a", b"a", b"b", None, b"c"] + [b"c"] * 100)

        def fetch():
            return next(frames)

        summary = record(fetch, written.append, 2, 5, clock, clock.sleep)
        self.assertEqual(len(written), 10)
        # A poll that brought nothing (None) keeps the picture before it.
        self.assertEqual(written[:5], [b"a", b"a", b"b", b"b", b"c"])
        self.assertEqual(summary["frames_written"], 10)
        self.assertIsNone(summary["ended_early"])
        # The video lasts as long as the recording did.
        self.assertAlmostEqual(clock.now - 100.0, 1.8, places=6)

    def test_a_slow_poll_repeats_the_last_frame_for_the_moments_it_missed(self):
        clock, written = Clock(), []
        pictures = iter([b"first", b"slow", b"next"] + [b"next"] * 50)

        def fetch():
            picture = next(pictures)
            if picture == b"slow":
                clock.sleep(0.75)  # three frames' time at 4 per second
            return picture

        record(fetch, written.append, 2, 4, clock, clock.sleep)
        self.assertEqual(len(written), 8)
        self.assertEqual(written[:5], [b"first", b"slow", b"slow", b"slow", b"slow"])

    def test_no_first_frame_fails_and_a_phone_that_stops_ends_the_recording(self):
        clock = Clock()
        with self.assertRaises(RecordingError):
            record(lambda: None, lambda picture: None, 5, 10, clock, clock.sleep)
        clock, written, calls = Clock(), [], []

        def fetch():
            calls.append(1)
            if len(calls) > 3:
                raise RecordingError("The phone stopped during the recording (ios_guest_stopped)")
            return b"x"

        summary = record(fetch, written.append, 10, 2, clock, clock.sleep)
        self.assertLess(len(written), 20)
        self.assertIn("ios_guest_stopped", summary["ended_early"])

    def test_a_stop_ends_the_recording_early_and_keeps_what_it_has(self):
        clock, written = Clock(), []
        summary = record(lambda: b"x", written.append, 10, 5, clock, clock.sleep, stopped=lambda: len(written) >= 7)
        self.assertEqual(len(written), 7)
        self.assertTrue(summary["stopped_early"])
        self.assertIsNone(summary["ended_early"])
        # A stop at once still keeps the first picture; before any frame there is nothing to keep.
        written = []
        self.assertEqual(record(lambda: b"x", written.append, 10, 5, clock, clock.sleep, stopped=lambda: True)["frames_written"], 1)
        with self.assertRaises(RecordingError):
            record(lambda: None, written.append, 10, 5, clock, clock.sleep, stopped=lambda: True)
        self.assertFalse(record(lambda: b"x", [].append, 1, 5, clock, clock.sleep)["stopped_early"])

    def test_ffmpeg_reads_ppm_from_stdin_and_writes_a_playable_mp4(self):
        command = ios_virtual_record.ffmpeg_command("/usr/bin/ffmpeg", 10, "/tmp/out.mp4")
        joined = " ".join(command)
        for part in ("-f image2pipe", "-framerate 10", "-c:v ppm", "-i pipe:0", "-c:v libx264",
                     "-pix_fmt yuv420p", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-f mp4"):
            self.assertIn(part, joined)
        self.assertEqual(command[-1], "/tmp/out.mp4")


FAKE_CLI = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ["FAKE_ROOT"])
count = root / "count"
calls = int(count.read_text()) if count.exists() else 0
count.write_text(str(calls + 1))
mode = os.environ.get("FAKE_MODE", "frames")
if mode == "ended":
    print(json.dumps({"ok": False, "error": {"code": "ios_guest_stopped", "message": "stopped"}}))
    sys.exit(1)
sequence = calls // 3 + 1
path = root / f"frame-{sequence}.ppm"
shade = (sequence * 40) % 256
path.write_bytes(f"P6\\n16 8\\n255\\n".encode() + bytes([shade]) * (16 * 8 * 3))
print(json.dumps({"ok": True, "result": {"job_id": "a" * 32, "action": "frame",
                  "result": {"path": str(path), "sequence": sequence, "changed": True}}}))
"""


class CliFrames(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tomacco-record-")
        self.root = Path(self.temp.name)
        self.cli = self.root / "tomaccoctl"
        self.cli.write_text(FAKE_CLI)
        self.cli.chmod(self.cli.stat().st_mode | stat.S_IXUSR)
        self.environment = dict(os.environ, FAKE_ROOT=str(self.root))
        self.saved = os.environ.copy()
        os.environ.update(self.environment)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)
        self.temp.cleanup()

    def test_a_frame_is_read_once_per_sequence_and_a_stopped_phone_ends_it(self):
        frames = ios_virtual_record.Frames(self.cli, "a" * 32)
        first = frames()
        self.assertTrue(first.startswith(b"P6\n16 8\n255\n"))
        # The same sequence again: the picture already read, not the file.
        (self.root / "frame-1.ppm").unlink()
        self.assertIs(frames(), first)
        os.environ["FAKE_MODE"] = "ended"
        with self.assertRaises(RecordingError):
            frames()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg and ffprobe")
    def test_a_recording_becomes_an_h264_mp4_of_its_length(self):
        output = self.root / "clips/phone.mp4"
        output.parent.mkdir()
        done = subprocess.run([sys.executable, str(Path(ios_virtual_record.__file__)), "--cli", str(self.cli),
                               "--job", "a" * 32, "--output", str(output), "--seconds", "2", "--fps", "5",
                               "--ffmpeg", shutil.which("ffmpeg")],
                              capture_output=True, text=True, timeout=120, env=os.environ)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        summary = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual((summary["state"], summary["frames_written"], summary["path"]),
                         ("recorded", 10, str(output)))
        self.assertGreater(summary["frames_distinct"], 1)
        probe = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
             "stream=codec_name,width,height,nb_read_frames", "-of", "json", str(output)],
            capture_output=True, text=True, timeout=60, check=True).stdout)["streams"][0]
        self.assertEqual((probe["codec_name"], probe["width"], probe["height"], probe["nb_read_frames"]),
                         ("h264", 16, 8, "10"))
        self.assertEqual([path.name for path in output.parent.iterdir()], ["phone.mp4"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg and ffprobe")
    def test_an_interrupt_stops_the_recording_and_keeps_the_video(self):
        output = self.root / "stopped.mp4"
        recorder = subprocess.Popen([sys.executable, str(Path(ios_virtual_record.__file__)), "--cli", str(self.cli),
                                     "--job", "a" * 32, "--output", str(output), "--seconds", "60", "--fps", "5",
                                     "--ffmpeg", shutil.which("ffmpeg")],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ)
        time.sleep(1.5)
        recorder.send_signal(signal.SIGINT)
        stdout, stderr = recorder.communicate(timeout=60)
        self.assertEqual(recorder.returncode, 0, stdout + stderr)
        summary = json.loads(stdout.strip().splitlines()[-1])
        self.assertEqual(summary["state"], "recorded")
        self.assertTrue(summary["stopped_early"])
        self.assertLess(summary["frames_written"], 60 * 5)
        self.assertTrue(output.is_file())

    def test_a_phone_that_is_off_leaves_no_file(self):
        os.environ["FAKE_MODE"] = "ended"
        output = self.root / "phone.mp4"
        done = subprocess.run([sys.executable, str(Path(ios_virtual_record.__file__)), "--cli", str(self.cli),
                               "--job", "a" * 32, "--output", str(output), "--seconds", "1",
                               "--ffmpeg", shutil.which("ffmpeg") or "ffmpeg"],
                              capture_output=True, text=True, timeout=120, env=os.environ)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(done.stdout.strip().splitlines()[-1])["state"], "failed")
        self.assertEqual(sorted(path.name for path in self.root.iterdir() if path.suffix == ".mp4"
                                or path.name.endswith(".partial")), [])


if __name__ == "__main__":
    unittest.main()
