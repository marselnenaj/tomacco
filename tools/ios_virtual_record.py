#!/usr/bin/env python3
"""Record the running virtual iPhone's screen to an MP4 file.

The frames are the ones a viewer gets: `ios.emulator.control` `frame` with the
file transport, asked for through the public CLI. Each is handed to ffmpeg as
it arrives, at a fixed rate; a frame that has not changed, or a poll that took
longer than one frame, repeats the last picture, so the video lasts as long as
the recording did. ios.virtual.record runs this as a durable job.
"""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

# How long the first frame may take before the recording gives up.
FIRST_FRAME_SECONDS = 30
# Control errors after which no further frame will come.
ENDED = ("ios_guest_stopped", "ios_control_ended", "job_not_found")


class RecordingError(RuntimeError):
    pass


def ffmpeg_command(ffmpeg, fps, output):
    """ffmpeg reading binary PPM pictures from stdin at `fps` and writing an
    H.264 MP4 that players and browsers open: even dimensions, 4:2:0."""
    return [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "image2pipe", "-framerate", str(fps), "-c:v", "ppm", "-i", "pipe:0",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-f", "mp4", str(output)]


class Frames:
    """The guest's current frame through the CLI. A frame whose sequence has
    not changed is not read again."""

    def __init__(self, cli, job):
        self.cli, self.job = cli, job
        self.sequence, self.picture = None, None

    def __call__(self):
        """The current picture, or None when this poll got none. Raises
        RecordingError once the guest has ended."""
        arguments = {"job_id": self.job, "action": "frame", "transport": "file"}
        try:
            done = subprocess.run([str(self.cli), "call", "ios.emulator.control", json.dumps(arguments)],
                                  capture_output=True, text=True, timeout=30)
            reply = json.loads(done.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        if not reply.get("ok"):
            code = (reply.get("error") or {}).get("code")
            if code in ENDED:
                raise RecordingError(f"The phone stopped during the recording ({code})")
            return None
        frame = (reply.get("result") or {}).get("result") or {}
        if frame.get("sequence") == self.sequence and self.picture is not None:
            return self.picture
        try:
            picture = Path(frame["path"]).read_bytes()
        except (KeyError, TypeError, OSError):
            # A later poll retired the file: this one has no picture.
            return None
        if not picture.startswith(b"P6"):
            return None
        self.sequence, self.picture = frame.get("sequence"), picture
        return picture


def record(fetch, write, seconds, fps, clock=time.monotonic, sleep=time.sleep, stopped=lambda: False):
    """Write `seconds * fps` pictures, each the latest frame at its moment.

    Returns how many pictures were written, how many polls brought one, and
    how many of those were a different picture. Raises RecordingError when no
    first frame comes; a guest that ends later ends the recording early, and
    so does `stopped` once it returns true (stopped_early)."""
    total = int(seconds * fps)
    first_deadline = clock() + FIRST_FRAME_SECONDS
    last = fetch()
    while last is None:
        if stopped():
            raise RecordingError("Stopped before the first frame came")
        if clock() >= first_deadline:
            raise RecordingError(f"No frame came within {FIRST_FRAME_SECONDS} s")
        sleep(0.1)
        last = fetch()
    start = clock()
    written, captured, distinct, ended = 0, 1, 1, None
    # A stop keeps at least the first picture, so there is a video.
    while written < total and not (written and stopped()):
        # Every moment that has passed gets the latest picture.
        # (A millionth of a frame absorbs the clock's rounding at a frame's moment.)
        due = min(total, math.floor((clock() - start) * fps + 1e-6) + 1)
        while written < due:
            write(last)
            written += 1
        if written >= total:
            break
        wait = start + written / fps - clock()
        if wait > 0:
            sleep(wait)
        try:
            picture = fetch()
        except RecordingError as error:
            ended = str(error)
            break
        if picture is not None:
            captured += 1
            if picture is not last:
                distinct += 1
            last = picture
    return {"frames_written": written, "frames_captured": captured, "frames_distinct": distinct,
            "ended_early": ended, "stopped_early": written < total and ended is None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, required=True)
    parser.add_argument("--fps", type=int, default=10)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= 600 or not 1 <= args.fps <= 30:
        parser.error("seconds must be 1 to 600 and fps 1 to 30")
    output = args.output.absolute()
    partial = output.with_name("." + output.name + ".partial")

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    # SIGINT, as job.send stop sends it, ends the recording early and keeps it.
    stop = []
    signal.signal(signal.SIGINT, lambda _signum, _frame: stop.append(True))
    encoder = None
    try:
        encoder = subprocess.Popen(ffmpeg_command(args.ffmpeg, args.fps, partial), stdin=subprocess.PIPE,
                                   stderr=subprocess.PIPE)

        def write(picture):
            try:
                encoder.stdin.write(picture)
            except BrokenPipeError:
                raise RecordingError("ffmpeg stopped: " + encoder.stderr.read().decode(errors="replace")[-400:])

        summary = record(Frames(args.cli, args.job), write, args.seconds, args.fps, stopped=lambda: bool(stop))
        encoder.stdin.close()
        error = encoder.stderr.read().decode(errors="replace")
        if encoder.wait() != 0 or not partial.is_file():
            raise RecordingError("ffmpeg failed: " + error[-400:])
        os.replace(partial, output)
        summary.update(state="recorded", path=str(output), bytes=output.stat().st_size,
                       seconds=summary["frames_written"] / args.fps, fps=args.fps)
        print(json.dumps(summary), flush=True)
        return 0
    except KeyboardInterrupt:
        return 130
    except (RecordingError, OSError) as error:
        print(json.dumps({"state": "failed", "error": str(error)}), flush=True)
        return 1
    finally:
        if encoder is not None and encoder.poll() is None:
            encoder.kill()
            encoder.wait()
        if partial.exists():
            partial.unlink()


if __name__ == "__main__":
    sys.exit(main())
