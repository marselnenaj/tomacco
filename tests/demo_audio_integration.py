#!/usr/bin/env python3
"""Real Demo Studio source-audio exports, with generated local media only.

No audio device, phone, browser, host or existing Tomacco state is accessed.
The CLI and its real sibling renderer run in an isolated home with device/host
tool traps and unreachable D-Bus addresses. Decoded PCM and rendered frame
colours establish clip timing independently of the renderer's own manifest.

    python3 tests/demo_audio_integration.py build/tomaccoctl --report qa/demo-audio/result.json
"""
import argparse
import array
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
TRAPS = ("adb", "fastboot", "ssh", "scp", "uxplay", "bluetoothctl", "btmgmt",
         "ideviceinfo", "idevice_id", "idevicepair", "iproxy", "xtool", "xcrun",
         "pymobiledevice3", "tidevice", "go-ios", "hyprctl", "pactl", "wpctl",
         "systemctl", "notify-send", "tomacco")
SAMPLE_RATE = 48000
CLI = None
REPORT = {"fixtures": {}, "exports": {}, "physical_device_access": False}
ARTIFACT_ROOT = None
KEEP_ARTIFACTS = False


def command(args, env, timeout=90):
    result = subprocess.run(args, env=env, capture_output=True, timeout=timeout)
    if result.returncode:
        raise AssertionError(f"{Path(args[0]).name} failed ({result.returncode}): "
                             f"{result.stderr.decode(errors='replace')[-6000:]}")
    return result.stdout


def rms(pcm, start, end):
    values = pcm[round(start*SAMPLE_RATE):round(end*SAMPLE_RATE)]
    if not values:
        raise AssertionError(f"No decoded samples at {start}–{end} seconds")
    return math.sqrt(sum(value*value for value in values)/len(values))


class DemoAudioIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not CLI or not CLI.is_file():
            raise RuntimeError("Pass a source-built tomaccoctl with its real sibling renderer")
        renderer = CLI.with_name("tomacco-demo-render")
        if not renderer.is_file():
            raise RuntimeError(f"Real sibling renderer missing: {renderer}")
        cls.temp = None if KEEP_ARTIFACTS else tempfile.TemporaryDirectory(prefix="tomacco-demo-audio-", dir=ARTIFACT_ROOT)
        cls.root = Path(tempfile.mkdtemp(prefix="tomacco-demo-audio-", dir=ARTIFACT_ROOT)) if KEEP_ARTIFACTS else Path(cls.temp.name)
        if KEEP_ARTIFACTS:
            REPORT["artifacts"] = str(cls.root)
        for name in ("bin", "home", "tmp", "run", "state", "fixtures"):
            (cls.root/name).mkdir(mode=0o700)
        for source in (CLI, renderer):
            target = cls.root/"bin"/source.name
            try:
                os.link(source, target)
            except OSError:
                shutil.copy2(source, target)
        cls.binary = cls.root/"bin/tomaccoctl"
        cls.trap_log = cls.root/"FORBIDDEN-TOOLS"
        for name in TRAPS:
            trap = cls.root/"bin"/name
            trap.write_text("#!"+sys.executable+"\nimport pathlib,sys\np=pathlib.Path("+
                            repr(str(cls.trap_log))+")\np.write_text(sys.argv[0]);sys.exit(97)\n")
            trap.chmod(0o700)
        cls.env = {"HOME": str(cls.root/"home"), "TOMACCO_HOME": str(cls.root/"state"),
                   "PATH": str(cls.root/"bin")+":/usr/bin:/bin", "TMPDIR": str(cls.root/"tmp"),
                   "XDG_RUNTIME_DIR": str(cls.root/"run"), "QT_QPA_PLATFORM": "offscreen",
                   "XDG_CONFIG_HOME": str(cls.root/"home/.config"),
                   "XDG_DATA_HOME": str(cls.root/"home/.local/share"),
                   "XDG_STATE_HOME": str(cls.root/"home/.local/state"),
                   "XDG_CACHE_HOME": str(cls.root/"home/.cache"),
                   "DBUS_SYSTEM_BUS_ADDRESS": "unix:path="+str(cls.root/"absent-system-bus"),
                   "DBUS_SESSION_BUS_ADDRESS": "unix:path="+str(cls.root/"absent-session-bus"),
                   "LANG": "C.UTF-8"}
        cls.sources = {}
        video = "color=c=red:s=320x640:r=24:d=4,drawbox=c=green:t=fill:enable='gte(t,1)*lt(t,2)',drawbox=c=blue:t=fill:enable='gte(t,2)'"
        for name, audio, delay in (
            ("pulses", "aevalsrc='0.4*sin(2*PI*733*t)*(between(t,0.10,0.25)+between(t,1.35,1.60))':s=48000:d=4", 0),
            ("delayed", "sine=frequency=997:sample_rate=48000:duration=0.75", .6),
            ("short", "sine=frequency=557:sample_rate=48000:duration=1.2", 0),
            ("silent", None, 0),
        ):
            target = cls.root/"fixtures"/(name+".mp4")
            args = ["/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", video]
            if audio:
                args += ["-itsoffset", str(delay), "-f", "lavfi", "-i", audio,
                         "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"]
            else:
                args += ["-map", "0:v:0", "-an"]
            args += ["-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(target)]
            command(args, cls.env)
            cls.sources[name] = target
            metadata = json.loads(command(["/usr/bin/ffprobe", "-v", "error", "-show_streams",
                                           "-show_format", "-of", "json", str(target)], cls.env))
            REPORT["fixtures"][name] = {"sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                                         "audio": [s for s in metadata["streams"] if s["codec_type"] == "audio"]}
        # A recording can contain sound before its first video frame. The video
        # decoder rebases that first picture; sound must use the same origin.
        delayed_video = cls.root/"fixtures/video-delayed.mp4"
        command(["/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-i", str(cls.sources["pulses"]),
                 "-itsoffset", "0.5", "-i", str(cls.sources["pulses"]), "-map", "1:v:0",
                 "-map", "0:a:0", "-c", "copy", str(delayed_video)], cls.env)
        cls.sources["video-delayed"] = delayed_video
        metadata = json.loads(command(["/usr/bin/ffprobe", "-v", "error", "-show_streams",
                                       "-show_format", "-of", "json", str(delayed_video)], cls.env))
        REPORT["fixtures"]["video-delayed"] = {"sha256": hashlib.sha256(delayed_video.read_bytes()).hexdigest(),
                                                 "streams": metadata["streams"], "format_start": metadata["format"]["start_time"]}
        coarse = cls.root/"fixtures/coarse-timebase.mp4"
        command(["/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-i", str(cls.sources["pulses"]),
                 "-c", "copy", "-video_track_timescale", "24", str(coarse)], cls.env)
        cls.sources["coarse-timebase"] = coarse
        metadata = json.loads(command(["/usr/bin/ffprobe", "-v", "error", "-show_streams",
                                       "-of", "json", str(coarse)], cls.env))
        REPORT["fixtures"]["coarse-timebase"] = {"sha256": hashlib.sha256(coarse.read_bytes()).hexdigest(),
                                                   "streams": metadata["streams"]}
        REPORT["binaries"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (CLI, renderer)}

    @classmethod
    def tearDownClass(cls):
        try:
            if cls.trap_log.exists():
                raise AssertionError("Forbidden device/host tool invoked: "+cls.trap_log.read_text())
            REPORT["forbidden_tool_calls"] = 0
        finally:
            if cls.temp:
                cls.temp.cleanup()

    def setUp(self):
        self.case = self.root/self.id().rsplit(".", 1)[-1]
        self.case.mkdir()
        self.jobs = []

    def tearDown(self):
        for job in self.jobs:
            status = self.call("job.get", {"job_id": job["id"]})
            if status["state"] not in ("succeeded", "failed", "cancelled", "timed_out"):
                self.call("job.cancel", {"job_id": job["id"]})
                self.call("job.wait", {"job_id": job["id"], "seconds": 30})
        self.assertFalse(self.trap_log.exists(), "Device/host trap was called")

    def raw(self, operation, arguments=None):
        result = subprocess.run([str(self.binary), "call", operation, json.dumps(arguments or {})],
                                env=self.env, capture_output=True, text=True, timeout=60)
        self.assertTrue(result.stdout, (result.returncode, result.stderr))
        return json.loads(result.stdout)

    def call(self, operation, arguments=None):
        result = self.raw(operation, arguments)
        self.assertTrue(result["ok"], result)
        return result["result"]

    def scene(self, source="pulses", start=1.0, duration=1.5, audio="source", name="scene"):
        path = self.case/(name+".json")
        created = self.call("demo.scene.create", {"path": str(path), "source": str(self.sources[source]),
                                                  "preset": "portrait-still"})
        changes = {"clip": {"start": start}, "output": {"width": 320, "height": 320, "fps": 24,
                                                             "duration": duration},
                   "keyframes": [{"time": 0, "yaw": 0, "pitch": 0, "roll": 0, "scale": .72,
                                  "x": .5, "y": .5, "easing": "linear"}]}
        if audio is not None:
            changes["audio"] = {"mode": audio}
        scene = self.call("demo.scene.update", {"path": str(path), "if_revision": created["revision"],
                                                 "changes": changes})
        return path, scene

    def export(self, path, scene, name="output"):
        output = self.case/(name+".mp4")
        args = {"path": str(path), "if_revision": scene["revision"], "output": str(output),
                "request_id": self.case.name+"-"+name}
        job = self.call("demo.export", args)
        self.jobs.append(job)
        final = self.call("job.wait", {"job_id": job["id"], "seconds": 30})
        deadline = time.monotonic()+90
        while final["state"] in ("queued", "running") and time.monotonic() < deadline:
            final = self.call("job.wait", {"job_id": job["id"], "seconds": 30})
        self.assertEqual(final["state"], "succeeded", self.call("job.logs", {"job_id": job["id"]}))
        inspected = self.call("demo.export.inspect", {"path": str(output)})
        manifest = json.loads(Path(str(output)+".manifest.json").read_text())
        expected_frames = math.ceil(scene["scene"]["output"]["duration"]*24)
        self.assertEqual(inspected["frames"], expected_frames)
        self.assertAlmostEqual(inspected["duration"], expected_frames/24, delta=.003)
        self.assertTrue(inspected["decoded"])
        REPORT["exports"][self.case.name+"/"+name] = {"inspection": inspected,
                                                     "validation": manifest["validation"]}
        return output, inspected, manifest, args, job

    def pcm(self, output):
        raw = command(["/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(output),
                       "-map", "0:a:0", "-ac", "1", "-ar", str(SAMPLE_RATE),
                       "-f", "f32le", "pipe:1"], self.env)
        result = array.array("f")
        result.frombytes(raw)
        if sys.byteorder != "little":
            result.byteswap()
        return result

    def assert_quiet(self, pcm, start, end):
        value = rms(pcm, start, end)
        self.assertLess(value, .012, f"Unexpected audio {value:.5f} at {start}–{end}s")

    def assert_tone(self, pcm, start, end):
        value = rms(pcm, start, end)
        self.assertGreater(value, .05, f"Missing audio {value:.5f} at {start}–{end}s")

    def test_trimmed_source_audio_preserves_content_and_matches_video_range(self):
        path, scene = self.scene()
        before = hashlib.sha256(self.sources["pulses"].read_bytes()).hexdigest()
        output, inspected, manifest, args, job = self.export(path, scene)
        self.assertTrue(inspected["has_audio"])
        self.assertFalse(manifest["validation"]["silent"])
        pcm = self.pcm(output)
        self.assert_quiet(pcm, .05, .25)  # The earlier source pulse must be cut away.
        self.assert_tone(pcm, .40, .55)  # Source 1.35–1.60 now lies at .35–.60.
        self.assert_quiet(pcm, .70, 1.40)
        active = [i*.01 for i in range(145) if rms(pcm, i*.01, i*.01+.02) > .05]
        self.assertAlmostEqual(min(active), .35, delta=.04)
        self.assertAlmostEqual(max(active), .60, delta=.04)
        REPORT["exports"][self.case.name+"/output"]["decoded_pulse_bounds"] = [min(active), max(active)]
        for timestamp, expected in ((.1, "green"), (1.2, "blue")):
            pixel = command(["/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-ss", str(timestamp),
                             "-i", str(output), "-frames:v", "1", "-vf", "crop=2:2:160:160",
                             "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], self.env)
            self.assertGreaterEqual(len(pixel), 12)
            red, green, blue = pixel[:3]
            self.assertGreater(green if expected == "green" else blue, max(red, blue if expected == "green" else green)+40)
        self.assertEqual(hashlib.sha256(self.sources["pulses"].read_bytes()).hexdigest(), before)
        self.assertEqual(self.call("demo.export", args)["id"], job["id"])
        conflict = self.raw("demo.export", {**args, "request_id": "different-request"})
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["error"]["code"], "demo_conflict")

    def test_delayed_audio_keeps_initial_gap_and_pads_tail(self):
        path, scene = self.scene("delayed", .25, 1.5)
        output, _, _, _, _ = self.export(path, scene)
        pcm = self.pcm(output)
        self.assert_quiet(pcm, .05, .25)
        self.assert_tone(pcm, .45, .90)
        self.assert_quiet(pcm, 1.20, 1.45)

    def test_short_audio_does_not_shorten_video(self):
        path, scene = self.scene("short", .8, 1.25)
        output, _, _, _, _ = self.export(path, scene)
        pcm = self.pcm(output)
        self.assert_tone(pcm, .05, .30)
        self.assert_quiet(pcm, .55, 1.20)

    def test_clip_after_audio_end_exports_silence_with_audio_track(self):
        path, scene = self.scene("short", 1.5, .5)
        output, inspected, _, _, _ = self.export(path, scene)
        self.assertTrue(inspected["has_audio"])
        self.assert_quiet(self.pcm(output), .03, .45)

    def test_audio_matches_rebased_picture_when_video_starts_after_sound(self):
        for start, pulse_start in ((0, .85), (.25, .85), (1.0, .35)):
            with self.subTest(clip_start=start):
                name = "start-"+str(start)
                path, scene = self.scene("video-delayed", start, 1.5, name=name)
                output, _, _, _, _ = self.export(path, scene, name)
                pcm = self.pcm(output)
                # Container video starts at .5. Both clips requested before
                # that time display the same first frame, so source pulse1.35
                # belongs at .85. A clip starting1.0 places it at .35 instead.
                self.assert_quiet(pcm, .03, pulse_start-.10)
                self.assert_tone(pcm, pulse_start+.05, pulse_start+.15)
                self.assert_quiet(pcm, pulse_start+.40, 1.45)

    def test_non_frame_aligned_trim_uses_the_first_displayed_frame_origin(self):
        for source, start, duration, origin, pulse_start, pulse_end in (
            ("pulses", 1.01, 1.0, 25/24, 1.35, 1.60),
            ("coarse-timebase", 1.01, 1.0, 1.0, 1.35, 1.60),
            ("coarse-timebase", .03, .5, 1/24, .10, .25),
        ):
            with self.subTest(source=source, clip_start=start):
                name = source+"-"+str(start)
                path, scene = self.scene(source, start, duration, name=name)
                output, _, _, _, _ = self.export(path, scene, name)
                pcm = self.pcm(output)
                # Fine timestamps keep frame25 (1.0416667s); a1/24 stream's
                # input seek rounds to frame24 (1s). Sound must follow the
                # actual picture, not the requested1.01 or roundedPTS+1.01.
                active = [i*.005 for i in range(round(duration/.005)-1) if rms(pcm, i*.005, i*.005+.005) > .05]
                self.assertTrue(active)
                self.assertAlmostEqual(min(active), pulse_start-origin, delta=.008)
                self.assertAlmostEqual(max(active), pulse_end-origin, delta=.008)
                REPORT["exports"][self.case.name+"/"+name]["decoded_pulse_bounds"] = [min(active), max(active)]

    def test_default_explicit_muted_and_old_scene_remain_silent(self):
        for name, mode in (("default", None), ("muted", "muted"), ("legacy", None)):
            path, scene = self.scene(audio=mode, name=name, duration=.25)
            if name == "legacy":
                document = json.loads(path.read_text())
                document.pop("audio", None)
                path.write_text(json.dumps(document))
                scene = self.call("demo.scene.get", {"path": str(path)})
            output, inspected, manifest, _, _ = self.export(path, scene, name)
            self.assertFalse(inspected["has_audio"])
            self.assertTrue(manifest["validation"]["silent"])
            streams = json.loads(command(["/usr/bin/ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)], self.env))["streams"]
            self.assertEqual([s["codec_type"] for s in streams], ["video"])

    def test_source_audio_policy_is_explicit_and_revision_guarded(self):
        path, scene = self.scene("silent", 0, .25, audio=None)
        before = path.read_bytes()
        for mode, code in (("source", "demo_invalid"), ("unknown", "invalid_argument")):
            failure = self.raw("demo.scene.update", {"path": str(path), "if_revision": scene["revision"], "changes": {"audio": {"mode": mode}}})
            self.assertFalse(failure["ok"])
            self.assertEqual(failure["error"]["code"], code)
            self.assertEqual(path.read_bytes(), before)
        audio_path, audio_scene = self.scene(name="with-audio", duration=.25)
        stale = self.raw("demo.scene.update", {"path": str(audio_path), "if_revision": audio_scene["revision"]-1,
                                                "changes": {"audio": {"mode": "muted"}}})
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["error"]["code"], "demo_conflict")
        preview = self.case/"preview.png"
        job = self.call("demo.preview", {"path": str(audio_path), "if_revision": audio_scene["revision"],
                                        "time": .1, "output": str(preview)})
        self.jobs.append(job)
        self.assertEqual(self.call("job.wait", {"job_id": job["id"], "seconds": 30})["state"], "succeeded")
        self.assertTrue(preview.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))


def main():
    global CLI, ARTIFACT_ROOT, KEEP_ARTIFACTS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cli", nargs="?", default=os.environ.get("TOMACCOCTL", ROOT/"build/tomaccoctl"))
    parser.add_argument("--report", type=Path)
    parser.add_argument("--keep-artifacts", action="store_true", help="Keep synthetic source/export media for review")
    args = parser.parse_args()
    CLI = Path(args.cli).resolve()
    KEEP_ARTIFACTS = args.keep_artifacts
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACT_ROOT = args.report.parent
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DemoAudioIntegration)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    REPORT.update(tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                  skipped=len(result.skipped), passed=result.wasSuccessful(),
                  limitations="Synthetic local-media contract/timing qualification; no physical AirPlay/device latency proof")
    if args.report:
        args.report.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2)+"\n")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
