#!/usr/bin/env python3
"""Qualify native framing against an existing stream without restarting it."""
import argparse
import json
import os
import subprocess
import time
from pathlib import Path

from native_client import dispatch, atomic_json, StudioError

parser = argparse.ArgumentParser()
parser.add_argument("--binary", default="build-product/tomacco")
parser.add_argument("--device", required=True)
parser.add_argument("--profile", required=True)
parser.add_argument("--output", default="qa/device-frames")
args = parser.parse_args()
binary = Path(args.binary).resolve()
os.environ["TOMACCOCTL"] = str(binary.with_name("tomaccoctl"))
output = Path(args.output).resolve()
output.mkdir(parents=True, exist_ok=True)
session = "native-frame-" + str(os.getpid())
before = [s for s in dispatch("runtime.list") if s["status"] == "connected"]
existing = next(s for s in before if s["device"] == args.device)


def preview(op, **values):
    return dispatch(op, {"session": session, **values})


env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software",
       "QT_QPA_PLATFORMTHEME": "generic", "QT_QUICK_CONTROLS_STYLE": "Basic",
       "XDG_CONFIG_HOME": str(output / "xdg/config"), "XDG_CACHE_HOME": str(output / "xdg/cache"),
       "XDG_DATA_HOME": str(output / "xdg/data")}
with (output / "real-desktop.log").open("w") as log:
    process = subprocess.Popen([str(binary), "--preview-session", session, "--workspace", "runtime"],
                               env=env, stdout=log, stderr=log)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                state = preview("preview.state")
                if state["native"]["status"] == "live":
                    break
            except StudioError:
                pass
            assert process.poll() is None and time.monotonic() < deadline, "No native picture"
            time.sleep(.2)
        preview("preview.configure", native={"platform": existing["platform"], "device": args.device, "frame": True})
        while True:
            state = preview("preview.state")
            if state["native"]["session"] == existing["session"] and state["native"]["status"] == "live":
                break
            assert time.monotonic() < deadline
            time.sleep(.1)
        assert state["device"] == args.profile and state["platform"] == existing["platform"], state
        assert state["native"]["frame_visible"], state
        assert state["native"]["screen_rect"]["width"] > 0, state
        time.sleep(.2)
        for name, options in [("window", {"window": True, "scale": 1}),
                              ("device", {"frame": True, "scale": 2}),
                              ("display", {"frame": False, "scale": 2})]:
            path = output / ("real-native-" + name + ".png")
            preview("preview.capture", path=str(path), **options)
            assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        atomic_json(output / "real-state.json", state)
        unframed = preview("preview.configure", native={"frame": False})
        assert not unframed["native"]["frame_visible"]
        assert unframed["native"]["session"] == existing["session"]
        assert unframed["screen"] == state["screen"]
        preview("preview.configure", native={"frame": True})
        preview("desktop.open", tab="devices")
        time.sleep(.6)
        preview("preview.capture", path=str(output / "real-device-library.png"), window=True, scale=1)
        after = [s for s in dispatch("runtime.list") if s["status"] == "connected"]
        assert {s["session"] for s in before} == {s["session"] for s in after}
        assert next(s for s in after if s["session"] == existing["session"])["pid"] == existing["pid"]
        atomic_json(output / "real-result.json", {"passed": True, "existing_session": existing["session"],
                    "profile": args.profile, "framed_capture": True, "display_capture": True,
                    "same_stream_after_toggle": True, "no_additional_runtime": True})
        print(json.dumps({"passed": True, "output": str(output)}))
    finally:
        # This process is the only owned resource. Never stop the existing guest,
        # runtime worker, build job or visible desktop on success or failure.
        process.terminate()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
