#!/usr/bin/env python3
"""Real Qt/Chromium IPC checks. Needs loopback, and a graphical session or the offscreen Qt platform."""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from native_client import catalog, preview, StudioError, atomic_json

parser = argparse.ArgumentParser()
parser.add_argument("--binary", default="build-product/tomacco")
parser.add_argument("--folder")
parser.add_argument("--output", default="qa/gui")
args = parser.parse_args()
output = Path(args.output).resolve()
output.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TOMACCO_HOME", str(output / "state"))
session = "integration-" + str(os.getpid())
command = [str(Path(args.binary).resolve()), "--preview-session", session, "--device", "18pro"]
if args.folder:
    command += ["--folder", str(Path(args.folder).resolve())]
checks = []
with (output / "client.log").open("w") as log:
    process = subprocess.Popen(command, env={**os.environ, "QT_QPA_PLATFORMTHEME": "generic", "QT_QUICK_CONTROLS_STYLE": "Basic",
                               "XDG_CONFIG_HOME": str(output / "xdg/config"), "XDG_DATA_HOME": str(output / "xdg/data"),
                               "XDG_CACHE_HOME": str(output / "xdg/cache")}, stdout=log, stderr=log)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                preview.call("state", session=session)
                break
            except StudioError:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Qt preview failed to start: " + (output / "client.log").read_text())
                time.sleep(.15)
        preview.ready(session)
        if args.folder:
            time.sleep(3)
        else:
            preview.call("evaluate", {"script": "document.body.classList.remove('light');updateChromeColor();window.scrollTo(0,0)"}, session)
            preview.call("click", {"selector": "#start"}, session)
            value = preview.call("evaluate", {"script": "document.getElementById('start').textContent"}, session)
            assert "Pause" in value, value
            checks.append({"check": "agent-click", "pass": True})
        for model in catalog.profiles():
            for unfolded in ([False, True] if "openWidth" in model else [False]):
                for orientation in ("portrait", "landscape"):
                    params = {"device": model["id"], "orientation": orientation, "unfolded": unfolded, "safe_area": True}
                    state = preview.call("configure", params, session)
                    preview.ready(session)
                    value = preview.call("evaluate", {"script": "({width:innerWidth,height:innerHeight,dpr:devicePixelRatio,layout:window.__tomaccoViewport,body:document.body.getBoundingClientRect().toJSON(),flutter:!!document.querySelector('flutter-view,flt-glass-pane')})"}, session)
                    assert value["width"] == state["screen"]["width"], (params, value, state)
                    assert value["height"] == state["screen"]["height"], (params, value, state)
                    assert value["layout"]["width"] == value["width"], (params, value)
                    if not args.folder:
                        header = preview.call("evaluate", {"script": "({controls:['.brand','#theme'].map(s=>document.querySelector(s).getBoundingClientRect().toJSON()),width:document.documentElement.scrollWidth})"}, session)
                        inset = value["layout"]["safeArea"]
                        for box in header["controls"]:
                            assert box["top"] >= inset["top"], (params, header, inset)
                            assert box["left"] >= inset["left"], (params, header, inset)
                            assert box["right"] <= value["width"] - inset["right"], (params, header, inset)
                        assert header["width"] == value["width"], (params, header)
                    assert state["device_category"] == model["platform"], state
                    assert all(catalog.find(profile)["platform"] == model["platform"] for profile in state["visible_devices"]), state
                    if model["id"] == "17e":
                        assert state["cutout"]["notch"] and not state["cutout"]["island"], state
                    if model["id"] == "duo":
                        expected = "horizontal" if unfolded and orientation == "portrait" else "compact-ring"
                        assert state["status_style"] == expected, state
                    label = model["id"] + ("-open" if unfolded else "") + "-" + orientation
                    capture = preview.invoke("capture", session=session, path=str(output / (label + ".png")), scale=1)
                    assert Path(capture["path"]).stat().st_size > 1000
                    if model["id"] == "17e":
                        assert capture["width"] == state["screen"]["width"] + 42, capture
                        assert capture["height"] == state["screen"]["height"] + 42, capture
                    checks.append({"check": label, "pass": True, "state": state, "browser": value, "capture": capture})
        if not args.folder:
            preview.call("configure", {"device": "17e", "orientation": "portrait", "unfolded": False, "chrome_theme": "auto"}, session)
            preview.call("click", {"selector": "#theme"}, session)
            time.sleep(1)
            assert preview.call("state", session=session)["effective_chrome_theme"] == "dark"
            preview.invoke("capture", session=session, path=str(output / "17e-light-theme.png"), scale=1)
            preview.call("click", {"selector": "#theme"}, session)
            time.sleep(1)
            assert preview.call("state", session=session)["effective_chrome_theme"] == "light"
            checks.append({"check": "demo-light-dark-system-indicator-contrast", "pass": True})
        # Check the same import/reload route used by agents and the GUI.
        custom = {**catalog.builtin()[0], "id": "integration-custom", "name": "Custom integration display", "width": 500, "height": 900}
        atomic_json(output / "custom.json", custom)
        catalog.import_profiles(str(output / "custom.json"), replace=True)
        preview.call("catalog_reload", session=session)
        preview.call("configure", {"device": custom["id"], "orientation": "portrait"}, session)
        preview.ready(session)
        value = preview.call("evaluate", {"script": "innerWidth"}, session)
        assert value == 500, value
        catalog.remove(custom["id"])
        checks.append({"check": "custom-profile-live-reload", "pass": True})
        atomic_json(output / "results.json", {"passed": True, "checks": checks})
        print(json.dumps({"passed": True, "checks": len(checks), "output": str(output)}))
    finally:
        process.terminate()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()
