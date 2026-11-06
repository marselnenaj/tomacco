"""The offline fixture runtime for the agent control-plane tests and the benchmark
runner: a scenario directory with rendered frames and hierarchies, an isolated
TOMACCO_HOME, and transport traps so that no helper can reach a real device.

The fixture is a scripted stand-in (native/runtime/fixture.rs). Nothing measured on
it is a device's performance."""
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_client import binary  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
# Helpers a transport would run. Each is a script that records the call and fails,
# so a test that reaches hardware fails loudly instead of touching a device.
TRAPPED = ["adb", "iproxy", "go-ios", "uxplay", "scrcpy", "idevice_id", "ideviceinfo", "idevicescreenshot",
           "xtool", "emulator", "ssh", "usbmuxd", "bluetoothctl", "ios"]


def render(path, size, label=None, shade=(244, 242, 236)):
    """A PNG of one colour with an optional label drawn large and dark, so OCR can read it."""
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", size, shade)
    if label:
        draw = ImageDraw.Draw(image)
        font = None
        for candidate in ("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
                          "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
            if Path(candidate).exists():
                font = ImageFont.truetype(candidate, max(24, size[0] // 12))
                break
        draw.text((size[0] // 10, size[1] // 3), label, fill=(14, 14, 12), font=font)
    image.save(path)


def hierarchy(width, height, rotation, nodes):
    out = ['<?xml version=\'1.0\' encoding=\'UTF-8\' standalone=\'yes\' ?>', f'<hierarchy rotation="{rotation}">',
           f'<node index="0" class="android.widget.FrameLayout" package="com.example.app" bounds="[0,0][{width},{height}]" enabled="true">']
    for node in nodes:
        out.append('<node index="1" class="%s" package="com.example.app" resource-id="%s" text="%s" content-desc="" bounds="[%d,%d][%d,%d]" clickable="true" enabled="true" focusable="true" focused="%s"/>'
                   % (node["class"], node.get("id", ""), node.get("text", ""), *node["rect"], "true" if node.get("focused") else "false"))
    out.append("</node></hierarchy>")
    return "\n".join(out)


def write_login_scenario(root, name="login", size=(360, 640), focus_after_ms=120, latency_ms=0):
    """A sign-in screen: tapping the field gives it focus after focus_after_ms, text
    then lands; the button leads to a welcome screen after a visible delay; the
    device can turn to landscape. Returns the scenario directory."""
    directory = Path(root) / name
    directory.mkdir(parents=True, exist_ok=True)
    w, h = size
    field = (20, h // 2 - 40, w - 20, h // 2 + 20)
    button = (20, h // 2 + 60, w - 20, h // 2 + 120)
    render(directory / "home.png", size, "Sign in")
    render(directory / "field.png", size, "Email", shade=(230, 228, 222))
    render(directory / "typed.png", size, "ana", shade=(220, 218, 212))
    render(directory / "welcome.png", size, "Welcome")
    render(directory / "wide.png", (h, w), "Sign in")
    render(directory / "spin1.png", size, "Loading", shade=(200, 200, 200))
    render(directory / "spin2.png", size, "Loading", shade=(120, 120, 120))
    nodes = [{"class": "android.widget.EditText", "id": "com.example.app:id/email", "rect": field},
             {"class": "android.widget.Button", "id": "com.example.app:id/sign_in", "text": "Sign in", "rect": button}]
    (directory / "home.xml").write_text(hierarchy(w, h, 0, nodes))
    (directory / "field.xml").write_text(hierarchy(w, h, 0, [dict(nodes[0], focused=True), nodes[1]]))
    (directory / "wide.xml").write_text(hierarchy(h, w, 1, [{"class": "android.widget.Button", "id": "com.example.app:id/sign_in",
                                                            "text": "Sign in", "rect": (h // 2 - 100, w // 2, h // 2 + 100, w // 2 + 60)}]))
    (directory / "loading.xml").write_text(hierarchy(w, h, 0, [{"class": "android.widget.TextView", "text": "Loading", "rect": (20, 100, w - 20, 160)}]))
    (directory / "welcome.xml").write_text(hierarchy(w, h, 0, [{"class": "android.widget.TextView", "text": "Welcome", "rect": (20, 100, w - 20, 160)}]))
    app = {"bundle_id": "com.example.app", "name": "Example", "home_screen": False}
    tap_field = {"left": field[0], "top": field[1], "right": field[2], "bottom": field[3], "to": "field", "after_ms": focus_after_ms}
    tap_button = {"left": button[0], "top": button[1], "right": button[2], "bottom": button[3], "to": "loading", "after_ms": 0}
    scenario = {"initial": "home", "latency_ms": latency_ms, "states": {
        "home": {"frame": "home.png", "hierarchy": "home.xml", "app": app, "keyboard": False, "focused": False,
                 "taps": [tap_field, tap_button], "rotate_to": {"landscape_left": "wide"}, "swipe_to": "home", "pinch_to": "home"},
        "field": {"frame": "field.png", "hierarchy": "field.xml", "app": app, "keyboard": True, "focused": True,
                  "taps": [tap_field, tap_button], "text_to": "typed", "key_to": {"back": "home"}},
        "typed": {"frame": "typed.png", "hierarchy": "field.xml", "app": app, "keyboard": True, "focused": True,
                  "taps": [tap_button], "key_to": {"enter": "loading", "back": "home"}},
        "loading": {"frame": "spin1.png", "hierarchy": "loading.xml", "app": app, "keyboard": False, "focused": False,
                    "animated": {"frames": ["spin1.png", "spin2.png"], "interval_ms": 0},
                    "taps": [{"left": 0, "top": 0, "right": w, "bottom": h, "to": "welcome", "after_ms": 0}], "key_to": {"back": "home"}},
        "welcome": {"frame": "welcome.png", "hierarchy": "welcome.xml", "app": app, "keyboard": False, "focused": False, "key_to": {"back": "home"}},
        "wide": {"frame": "wide.png", "hierarchy": "wide.xml", "app": app, "orientation": "landscape", "keyboard": False,
                 "focused": False, "rotate_to": {"portrait": "home"}}}}
    (directory / "scenario.json").write_text(json.dumps(scenario, indent=1))
    return directory


class Isolated:
    """A scratch home with the fixture runtime on and every device helper trapped."""

    def __init__(self, prefix="agent-"):
        self.temp = tempfile.TemporaryDirectory(prefix=prefix)
        base = Path(self.temp.name)
        self.home = base / "home"
        self.home.mkdir()
        self.fixtures = base / "fixtures"
        self.fixtures.mkdir()
        self.traps = base / "traps"
        self.traps.mkdir()
        self.trap_log = base / "traps.log"
        for name in TRAPPED:
            script = self.traps / name
            script.write_text("#!/bin/sh\necho \"TRAP $0 $*\" >> \"%s\"\necho 'transport trap: this test must not reach a device' >&2\nexit 97\n" % self.trap_log)
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
        self.env = dict(os.environ)
        self.env.update(HOME=str(self.home), TOMACCO_HOME=str(self.home / "tomacco"), XDG_CONFIG_HOME=str(self.home / ".config"),
                        XDG_DATA_HOME=str(self.home / ".local/share"), XDG_CACHE_HOME=str(self.home / ".cache"),
                        TOMACCO_FIXTURE_RUNTIME=str(self.fixtures), TOMACCO_UPDATES="off",
                        PATH=str(self.traps) + os.pathsep + self.env.get("PATH", ""))
        self.env.pop("XDG_RUNTIME_DIR", None)
        self.env["TMPDIR"] = str(base / "tmp")
        (base / "tmp").mkdir()

    def call(self, operation, arguments=None, timeout=120):
        import subprocess
        run = subprocess.run([binary(), "call", operation, json.dumps(arguments or {})], env=self.env,
                             capture_output=True, text=True, timeout=timeout)
        try:
            return json.loads(run.stdout)
        except ValueError:
            raise AssertionError((run.returncode, run.stdout, run.stderr))

    def result(self, operation, arguments=None, timeout=120):
        envelope = self.call(operation, arguments, timeout)
        assert envelope["ok"], envelope
        return envelope["result"]

    def error(self, operation, arguments=None, timeout=120):
        envelope = self.call(operation, arguments, timeout)
        assert not envelope["ok"], envelope
        return envelope["error"]

    def trapped(self):
        return self.trap_log.read_text() if self.trap_log.exists() else ""

    def stop_all(self):
        try:
            for session in self.result("runtime.list", timeout=60):
                if session.get("status") in ("starting", "connected"):
                    self.call("runtime.stop", {"session": session["session"]}, timeout=60)
        finally:
            time.sleep(0.2)

    def close(self):
        self.stop_all()
        self.temp.cleanup()
