#!/usr/bin/env python3
"""Offline fixture: does AirPlay mirror audio keep a backlog after a delivery stall?

Tomacco runs UxPlay with `-vsync no`: mirror audio (AAC) reaches the sink without
timestamps and with `sync=false`, so the sink plays whatever arrives as fast as
its ring buffer takes it. Audio that arrives late in a burst (a Wi-Fi stall, or
UxPlay's wait for a resent packet) is then played in full, later, and the delay
never shrinks again for the rest of the mirror.

This builds UxPlay 1.73.7's own audio pipeline string (renderers/audio_renderer.c,
format 3, AAC-LC; mirror AAC-ELD uses the same avdec_aac path) with the `-as`
value Tomacco passes, and replaces only the final audio device element by a
model of one: an appsink that plays in real time with a 200 ms ring buffer and
blocks when it is full, as pulsesink's default buffer-time does. Nothing here
opens an audio device, a network socket or any hardware.

    python3 tests/airplay_audio_backlog_integration.py            # Tomacco's -as
    python3 tests/airplay_audio_backlog_integration.py --legacy   # 1.4.x: plain sink

The second run is expected to fail: it reproduces the kept backlog.
"""
import argparse
import json
import random
import re
import statistics
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GstApp  # noqa: E402,F401

RATE = 44100
CHANNELS = 2
FRAME = 1024  # AAC-LC samples per frame
RING = 0.200  # pulsesink buffer-time default (gst-inspect-1.0 pulsesink)
NETWORK = 0.005  # one-way delay of every packet
# UxPlay 1.73.7 aac_lc_caps (renderers/audio_renderer.c)
UXPLAY_AAC_LC_CAPS = "audio/mpeg,mpegversion=(int)4,channels=(int)2,rate=(int)44100,stream-format=raw,codec_data=(buffer)1210"
SOURCE = Path(__file__).resolve().parent.parent / "native" / "runtime" / "airplay.rs"


def production_queue() -> str:
    """The queue Tomacco puts before UxPlay's audio sink, from the source."""
    text = SOURCE.read_text(encoding="utf-8")
    match = re.search(r'const AUDIO_QUEUE: &str =\s*"([^"]+)";', text)
    if not match:
        sys.exit(f"AUDIO_QUEUE not found in {SOURCE}")
    return match.group(1)


def uxplay_audio_pipeline(audiosink: str) -> str:
    # audio_renderer_init, case 2 (AAC-LC); " sync=false" is what -vsync no appends.
    return (
        "appsrc name=audio_source ! queue ! avdec_aac ! audioconvert ! "
        "audioresample quality=10 ! volume name=volume ! level ! "
        + audiosink
        + " sync=false"
    )


def encode(frames: int) -> list:
    """Raw AAC-LC frames of a 440 Hz tone, as the phone would send them."""
    pipeline = Gst.parse_launch(
        f"audiotestsrc wave=sine freq=440 num-buffers={frames + 4} samplesperbuffer={FRAME} ! "
        f"audio/x-raw,format=S16LE,rate={RATE},channels={CHANNELS} ! audioconvert ! avenc_aac ! "
        "audio/mpeg,stream-format=raw ! appsink name=out sync=false"
    )
    out = pipeline.get_by_name("out")
    pipeline.set_state(Gst.State.PLAYING)
    data, codec = [], None
    while True:
        sample = out.emit("pull-sample")
        if sample is None:
            break
        if codec is None:
            value = sample.get_caps().get_structure(0).get_value("codec_data")
            codec = value.extract_dup(0, value.get_size())
        buffer = sample.get_buffer()
        data.append(buffer.extract_dup(0, buffer.get_size()))
    pipeline.set_state(Gst.State.NULL)
    # 1210 is AAC-LC, 44.1 kHz, stereo; avenc_aac may append an explicit no-SBR extension.
    if codec is None or not codec.startswith(b"\x12\x10"):
        sys.exit("avenc_aac did not produce 44.1 kHz stereo AAC-LC (codec_data 1210)")
    if len(data) < frames:
        sys.exit(f"avenc_aac produced {len(data)} of {frames} frames")
    return data[:frames]


def run(audiosink: str, frames: list, deliver) -> dict:
    """Pushes `frames` into the pipeline at the times `deliver(i)` gives, and
    measures when each decoded sample leaves the modelled device."""
    pipeline = Gst.parse_launch(uxplay_audio_pipeline(audiosink))
    source = pipeline.get_by_name("audio_source")
    source.set_property("caps", Gst.Caps.from_string(UXPLAY_AAC_LC_CAPS))
    source.set_property("stream-type", 0)
    source.set_property("is-live", True)
    source.set_property("format", Gst.Format.TIME)
    device = pipeline.get_by_name("device")
    device.set_property("emit-signals", True)
    device.set_property("max-buffers", 0)
    device.set_property("drop", False)
    played = []
    clock = {"end": 0.0}

    def on_sample(sink):
        sample = sink.emit("pull-sample")
        buffer = sample.get_buffer()
        info = sample.get_caps().get_structure(0)
        rate = info.get_value("rate")
        width = 4 if info.get_value("format") in ("F32LE", "S32LE") else 2
        count = buffer.get_size() // (width * info.get_value("channels"))
        # A full ring buffer blocks the writer until there is room again.
        ahead = clock["end"] - time.monotonic()
        if ahead > RING:
            time.sleep(ahead - RING)
        start = max(time.monotonic(), clock["end"])
        clock["end"] = start + count / rate
        if buffer.pts == Gst.CLOCK_TIME_NONE:
            raise SystemExit("avdec_aac gave no interpolated timestamps")
        played.append((buffer.pts / Gst.SECOND, start, count / rate))
        return Gst.FlowReturn.OK

    device.connect("new-sample", on_sample)
    pipeline.set_state(Gst.State.PLAYING)
    origin = time.monotonic() + 0.3
    schedule = sorted((deliver(i), i) for i in range(len(frames)))
    for at, i in schedule:
        wait = origin + at - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        # UxPlay pushes without a timestamp when sync is off.
        source.emit("push-buffer", Gst.Buffer.new_wrapped(frames[i]))
    source.emit("end-of-stream")
    bus = pipeline.get_bus()
    bus.timed_pop_filtered(30 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pipeline.set_state(Gst.State.NULL)
    duration = len(frames) * FRAME / RATE
    # Latency of a decoded buffer: when it plays minus when the phone captured it.
    lat = [(pts, start - (origin + pts)) for pts, start, _ in played]
    window = lambda lo, hi: [l for p, l in lat if lo <= p < hi]  # noqa: E731
    return {
        "played_seconds": round(sum(d for _, _, d in played), 3),
        "sent_seconds": round(duration, 3),
        "latency_start_ms": round(1000 * statistics.median(window(0.5, 1.4)), 1),
        "latency_end_ms": round(1000 * statistics.median(window(duration - 1.0, duration)), 1),
        "latency_max_ms": round(1000 * max(l for _, l in lat), 1),
    }


def parses(audiosink: str) -> dict:
    """UxPlay's real AAC-ELD pipeline with this -as value, only parsed: it
    stays in the NULL state, so no audio device is opened."""
    text = uxplay_audio_pipeline(audiosink)
    pipeline = Gst.parse_launch(text)
    children = []
    it = pipeline.iterate_elements()
    while True:
        result, element = it.next()
        if result != Gst.IteratorResult.OK:
            break
        children.append(element)
    elements = {e.get_factory().get_name(): e for e in children}
    sink = elements["autoaudiosink"]
    found = {"pipeline": text, "sink_sync": sink.get_property("sync")}
    queues = [e for e in children if e.get_factory().get_name() == "queue"]
    leaky = [q for q in queues if int(q.get_property("leaky")) == 2]
    if leaky:
        q = leaky[0]
        found["queue"] = {"max_size_time_ns": q.get_property("max-size-time"), "max_size_buffers": q.get_property("max-size-buffers"), "max_size_bytes": q.get_property("max-size-bytes")}
    pipeline.set_state(Gst.State.NULL)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--legacy", action="store_true", help="the plain sink of Tomacco 1.4.x")
    parser.add_argument("--json", type=Path, help="also write the results here")
    args = parser.parse_args()
    Gst.init(None)
    for element in ("appsrc", "appsink", "queue", "avdec_aac", "avenc_aac", "level", "audioresample"):
        if Gst.ElementFactory.find(element) is None:
            sys.exit(f"GStreamer element {element} is missing")
    queue = None if args.legacy else production_queue()
    audiosink = "appsink name=device" if queue is None else f"{queue} ! appsink name=device"
    fd = FRAME / RATE
    stall_at = 1.5
    frames = encode(int(5.0 / fd))
    rng = random.Random(7)
    # Ordinary Wi-Fi jitter, up to 60 ms; UxPlay hands frames on in order.
    jittered = []
    for i in range(len(frames)):
        jittered.append(max(i * fd + NETWORK + rng.uniform(0.0, 0.060), jittered[-1] if jittered else 0.0))
    def stalled(stall):
        # Everything captured during the stall arrives at once at its end.
        return lambda i: (stall_at + stall if stall_at <= i * fd < stall_at + stall else i * fd) + NETWORK

    scenarios = {
        "stall": stalled(1.2),
        "long_stall": stalled(2.4),
        "jitter": lambda i: jittered[i],
    }
    backlog = 0.0 if queue is None else int(re.search(r"max-size-time=(\d+)", queue).group(1)) / 1e9
    # A burst fills the device's own ring buffer and then the queue; one more
    # frame of margin for decoding and scheduling.
    bound = 1000 * (RING + backlog + 3 * fd)
    production = "autoaudiosink" if queue is None else f"{queue} ! autoaudiosink"
    results = {"production": parses(production), "audiosink": audiosink, "ring_ms": RING * 1000, "bound_ms": round(bound, 1), "stall_ms": {"stall": 1200, "long_stall": 2400}}
    for name, deliver in scenarios.items():
        results[name] = run(audiosink, frames, deliver)
    failures = []
    j = results["jitter"]
    if results["production"]["sink_sync"] is not False:
        failures.append("the production sink does not keep sync=false")
    if queue is not None and results["production"].get("queue", {}).get("max_size_time_ns") != int(backlog * 1e9):
        failures.append("the production queue does not have the bound")
    # After a stall the added delay is bounded by the ring buffer and the
    # queue, however long the stall was.
    for name in ("stall", "long_stall"):
        s = results[name]
        added = s["latency_end_ms"] - s["latency_start_ms"]
        s["added_ms"] = round(added, 1)
        if added > bound:
            failures.append(f"{name}: latency stays {added:.0f} ms higher after the stall (bound {bound:.0f} ms)")
    # Ordinary jitter loses nothing and adds no lasting delay.
    if j["played_seconds"] < j["sent_seconds"] - 0.03:
        failures.append(f"jitter: only {j['played_seconds']} s of {j['sent_seconds']} s played")
    if j["latency_end_ms"] > j["latency_start_ms"] + 60:
        failures.append("jitter: latency grew")
    results["failures"] = failures
    print(json.dumps(results, indent=2))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    for failure in failures:
        print("FAIL", failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
