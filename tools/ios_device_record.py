#!/usr/bin/env python3
"""Record a USB-connected iPhone's screen to an MP4 file.

go-ios `screenshot --stream` serves the phone's screenshots as an MJPEG stream
on a local port. This reads that stream and hands the latest picture to ffmpeg
at a fixed rate; a picture that has not changed, or a frame that came late,
repeats the last one, so the video lasts as long as the recording did.
ios.record runs this as a durable job; job.send stop ends it early and keeps
what was recorded.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ios_virtual_record import RecordingError, record  # noqa: E402

# A picture larger than this is not a screenshot.
MAX_PICTURE = 16 << 20
CONNECT_SECONDS = 30


def ffmpeg_command(ffmpeg, fps, output):
    """ffmpeg reading JPEG pictures from stdin at `fps` and writing an H.264
    MP4 that players and browsers open: even dimensions, 4:2:0."""
    return [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "image2pipe", "-framerate", str(fps), "-c:v", "mjpeg", "-i", "pipe:0",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-f", "mp4", str(output)]


class Pictures:
    """JPEG pictures cut out of an MJPEG byte stream by their start and end markers."""

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer += data
        found = []
        while True:
            start = self.buffer.find(b"\xff\xd8")
            if start < 0:
                # Keep a trailing 0xff: it may begin the next start marker.
                del self.buffer[:max(0, len(self.buffer) - 1)]
                break
            end = self.buffer.find(b"\xff\xd9", start + 2)
            if end < 0:
                del self.buffer[:start]
                if len(self.buffer) > MAX_PICTURE:
                    raise RecordingError("A picture in the stream is larger than 16 MiB")
                break
            found.append(bytes(self.buffer[start:end + 2]))
            del self.buffer[:end + 2]
        return found


class Stream:
    """The latest picture of go-ios's MJPEG stream, read on a thread."""

    def __init__(self, port):
        self.port, self.latest, self.error, self.count = port, None, None, 0
        self.lock = threading.Lock()
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        deadline = time.monotonic() + CONNECT_SECONDS
        while True:
            try:
                connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
                connection.request("GET", "/")
                response = connection.getresponse()
                break
            except OSError:
                if time.monotonic() > deadline:
                    self.error = f"go-ios served no stream on port {self.port} within {CONNECT_SECONDS} s"
                    return
                time.sleep(0.2)
        pictures = Pictures()
        try:
            while True:
                data = response.read1(65536)
                if not data:
                    self.error = "go-ios ended the stream"
                    return
                for picture in pictures.feed(data):
                    with self.lock:
                        self.latest = picture
                        self.count += 1
        except (OSError, RecordingError, http.client.HTTPException) as error:
            self.error = str(error)

    def __call__(self):
        with self.lock:
            latest = self.latest
        if self.error:
            # Before the first picture this fails the recording; later it ends it early.
            raise RecordingError(self.error)
        return latest


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go-ios", required=True)
    parser.add_argument("--udid", required=True)
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
    stop = []
    signal.signal(signal.SIGINT, lambda _signum, _frame: stop.append(True))
    port = free_port()
    streamer = encoder = None
    try:
        streamer = subprocess.Popen([args.go_ios, "screenshot", "--stream", f"--port={port}", f"--udid={args.udid}"],
                                    stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr)
        stream = Stream(port)
        encoder = subprocess.Popen(ffmpeg_command(args.ffmpeg, args.fps, partial), stdin=subprocess.PIPE,
                                   stderr=subprocess.PIPE)

        def write(picture):
            try:
                encoder.stdin.write(picture)
            except BrokenPipeError:
                raise RecordingError("ffmpeg stopped: " + encoder.stderr.read().decode(errors="replace")[-400:])

        summary = record(stream, write, args.seconds, args.fps, stopped=lambda: bool(stop))
        encoder.stdin.close()
        error = encoder.stderr.read().decode(errors="replace")
        if encoder.wait() != 0 or not partial.is_file():
            raise RecordingError("ffmpeg failed: " + error[-400:])
        os.replace(partial, output)
        summary.update(state="recorded", path=str(output), bytes=output.stat().st_size,
                       seconds=summary["frames_written"] / args.fps, fps=args.fps, pictures_received=stream.count)
        print(json.dumps(summary), flush=True)
        return 0
    except KeyboardInterrupt:
        return 130
    except (RecordingError, OSError) as error:
        print(json.dumps({"state": "failed", "error": str(error)}), flush=True)
        return 1
    finally:
        for child in (encoder, streamer):
            if child is not None and child.poll() is None:
                child.kill()
                child.wait()
        if partial.exists():
            partial.unlink()


if __name__ == "__main__":
    sys.exit(main())
