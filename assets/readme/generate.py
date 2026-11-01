#!/usr/bin/env python3
"""Every README graphic, from one NAME and one CLI.

To re-brand: edit the two constants below and run this file. The screenshot is
taken from the real desktop app, so after renaming the app rebuild it and run
with --capture BUILD_DIR as well.

    python3 assets/readme/generate.py                     # all graphics, in place
    python3 assets/readme/generate.py --capture build-x   # first retake the screenshot
    python3 assets/readme/generate.py --name AMPED --cli ampedctl --out /tmp/preview

Needs rsvg-convert, Pillow, and the fonts Adwaita Sans and JetBrainsMono Nerd Font.
The SVGs next to the PNGs are the sources; the README shows the PNGs, so that it
never depends on a reader's fonts.

Operation names and the operation count come from assets/operations.json, and an
operation that does not exist there stops the run.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image, ImageFont

NAME = "Tomacco"
CLI = "tomaccoctl"

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OPERATIONS = {entry["name"] for entry in json.loads((ROOT / "assets/operations.json").read_text())}

SANS = "Adwaita Sans"
MONO = "JetBrainsMono Nerd Font"
SANS_FILE = "/usr/share/fonts/Adwaita/AdwaitaSans-Regular.ttf"
MONO_ADVANCE = 0.6  # JetBrains Mono: 600 units per 1000 em

# The tomato of assets/logo.svg: its fruit and its stem, flat on both grounds.
ACCENT = "#F2361F"
GREEN = "#2FA84F"
LOGO = re.search(r"<svg[^>]*>(.*)</svg>", (ROOT / "assets/logo.svg").read_text(), re.S).group(1)


@dataclass(frozen=True)
class Theme:
    ground: str
    ink: str
    dim: str
    rule: str
    window: str  # a window on the desktop
    on_accent: str


DARK = Theme("#0E0E0C", "#F4F2EC", "#96948B", "#34332E", "#171714", "#0E0E0C")
LIGHT = Theme("#F4F2EC", "#0E0E0C", "#66645C", "#CFCCC2", "#FFFFFF", "#0E0E0C")


def op(name):
    """An operation name, checked against the schema."""
    if name not in OPERATIONS:
        raise SystemExit(f"{name} is not in assets/operations.json")
    return name


def sans_width(text, size, weight=400, tracking=0.0):
    font = ImageFont.truetype(SANS_FILE, 200)
    font.set_variation_by_axes([14, weight])
    return font.getlength(text) * size / 200 + tracking * size * len(text)


def mono_width(text, size):
    return len(text) * size * MONO_ADVANCE


class Canvas:
    def __init__(self, width, height, ground):
        self.width, self.height = width, height
        self.parts = [f'<rect width="{width}" height="{height}" fill="{ground}"/>'] if ground else []

    def rect(self, x, y, w, h, fill="none", stroke=None, stroke_width=0, radius=0, extra=""):
        line = f' stroke="{stroke}" stroke-width="{stroke_width}"' if stroke else ""
        round_ = f' rx="{radius}"' if radius else ""
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}"{line}{round_} {extra}/>')

    def line(self, *points, color, width=4, extra=""):
        path = " ".join(f"{x},{y}" for x, y in points)
        self.parts.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="{width}" '
                          f'stroke-linecap="butt" stroke-linejoin="miter" {extra}/>')

    def circle(self, x, y, r, fill="none", stroke=None, stroke_width=0):
        line = f' stroke="{stroke}" stroke-width="{stroke_width}"' if stroke else ""
        self.parts.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{fill}"{line}/>')

    def sans(self, x, y, text, size, fill, weight=400, anchor="start", tracking=0.0):
        self.parts.append(f'<text x="{x}" y="{y}" font-family="{SANS}" font-size="{size}" font-weight="{weight}" '
                          f'fill="{fill}" text-anchor="{anchor}" letter-spacing="{tracking * size:.2f}">{escape(text)}</text>')

    def mono(self, x, y, text, size, fill, weight=400, anchor="start"):
        self.parts.append(f'<text x="{x}" y="{y}" font-family="{MONO}" font-size="{size}" font-weight="{weight}" '
                          f'fill="{fill}" text-anchor="{anchor}" xml:space="preserve">{escape(text)}</text>')

    def spans(self, x, y, size, *runs):
        """One line of monospace in several colours, on the character grid."""
        for text, fill in runs:
            self.mono(x, y, text, size, fill)
            x += mono_width(text, size)
        return x

    def mark(self, x, y, size):
        """The logo, whole, at a square size."""
        self.parts.append(f'<svg x="{x}" y="{y}" width="{size}" height="{size}" viewBox="0 0 100 100">{LOGO}</svg>')

    def svg(self):
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" height="{self.height}" '
                f'viewBox="0 0 {self.width} {self.height}">\n' + "\n".join(self.parts) + "\n</svg>\n")


TRACK = -0.04  # wordmark tracking, in em
CAP_HEIGHT = 0.727  # of the em, Adwaita Sans
MARK_GAP = 0.08
MARK_SIZE = 0.92


def wordmark(c, x, baseline, width, fill, weight=800):
    """NAME and the tomato, set to fill a width whatever the name's length."""
    probe = sans_width(NAME, 100, weight, TRACK)
    size = width / (probe / 100 + MARK_GAP + MARK_SIZE)
    c.sans(x, baseline, NAME, round(size, 1), fill, weight, tracking=TRACK)
    word = sans_width(NAME, size, weight, TRACK)
    mark = MARK_SIZE * size
    # The fruit ends at 97 % of the logo's height: it stands on the baseline.
    c.mark(round(x + word + MARK_GAP * size, 1), round(baseline - mark * 0.97, 1), round(mark, 1))
    return size


def bar(c, t, height, size, active="1", label="made for Omarchy", pad=None):
    """The slim top bar of an Omarchy desktop: workspaces left, a word right."""
    pad = pad if pad is not None else size
    y = round(height / 2 + size * 0.36)
    x = pad
    for number in "1234":
        if number == active:
            cell = size * 1.5
            c.rect(round(x - (cell - size * MONO_ADVANCE) / 2), 0, round(cell), height, ACCENT)
            c.mono(x, y, number, size, t.on_accent, 700)
        else:
            c.mono(x, y, number, size, t.dim)
        x += round(size * 1.9)
    c.mono(c.width - pad, y, label, size, t.ink, anchor="end")


def terminal_lines(c, t, x, y, size, leading):
    prompt = ("❯ ", GREEN)
    call = f"{CLI} call "
    lines = [
        [prompt, (call, t.ink), (op("project.run"), t.ink), (" {…}", t.dim)],
        [('{"ok":true,"result":{…,"state":"queued",…}}', t.dim)],
        [prompt, (call, t.ink), (op("job.wait"), t.ink), (" {…}", t.dim)],
        [('{"ok":true,"result":{…,"state":"succeeded",…}}', t.dim)],
        [prompt, (call, t.ink), (op("runtime.input"), t.ink), (' {…,"x":0.5,"y":0.7}', t.dim)],
        [prompt],
    ]
    for number, runs in enumerate(lines):
        end = c.spans(x, y + number * leading, size, *runs)
    # The same cursor as the wordmark's, at the size of one character cell.
    c.rect(round(end), round(y + (len(lines) - 1) * leading - size * 0.78), round(size * MONO_ADVANCE), round(size * 1.0), ACCENT)


def phone(c, t, cx, cy, height, tile_width):
    """A neutral phone, and a tap at exactly the coordinates the terminal sent."""
    width = round(height * 0.485)
    x, y = cx - width // 2, cy - height // 2
    c.rect(x, y, width, height, fill=t.ground, stroke=t.ink, stroke_width=6, radius=round(height * 0.075))
    inset = round(height * 0.022)
    sx, sy, sw, sh = x + inset, y + inset, width - 2 * inset, height - 2 * inset
    c.rect(sx, sy, sw, sh, fill=t.ink, radius=round(height * 0.058))
    unit = sw / 12
    tone = "#D5D2C8"
    top = sy + sh * 0.13
    c.rect(round(sx + unit), round(top), round(unit * 6), round(unit * 0.8), fill=t.ground)
    for row in range(3):
        ry = top + unit * (2.2 + row * 1.9)
        c.rect(round(sx + unit), round(ry), round(unit * 10), round(unit * 1.2), fill=tone)
    tx, ty = round(sx + sw * 0.5), round(sy + sh * 0.7)
    c.rect(round(sx + unit), round(ty - unit * 0.95), round(unit * 10), round(unit * 1.9), fill=ACCENT)
    reach = (tile_width - sw) // 2 - 36
    c.line((sx - reach, ty), (sx + sw + reach, ty), color=ACCENT, width=3)
    c.line((tx, sy - 70), (tx, sy + sh + 70), color=ACCENT, width=3)
    c.mono(sx - reach, ty - 20, "y 0.7", 36, t.ink)
    c.mono(tx + 20, sy - 36, "x 0.5", 36, t.ink)
    c.circle(tx, ty, round(unit * 0.62), fill=t.ink, stroke=t.ground, stroke_width=8)


GAP = 20  # Omarchy's gaps_in 5 and gaps_out 10, at two pixels per point
BORDER = 4  # its border_size 2, square corners


def window(c, t, x, y, w, h, focused=False):
    c.rect(x + BORDER / 2, y + BORDER / 2, w - BORDER, h - BORDER, fill=t.window,
           stroke=GREEN if focused else t.rule, stroke_width=BORDER)


def hero():
    t = DARK
    c = Canvas(2400, 1200, t.ground)
    top = 72
    bar(c, t, top, 36, pad=44)
    x0, y0 = GAP, top + GAP
    right_w = 760
    left_w = c.width - 2 * GAP - GAP - right_w
    h = c.height - y0 - GAP
    upper_h = 620
    lower_h = h - upper_h - GAP
    # Master: the name.
    window(c, t, x0, y0, left_w, upper_h)
    inner = 76
    wordmark(c, x0 + inner - 8, y0 + 366, left_w - 2 * inner, t.ink)
    c.sans(x0 + inner, y0 + 510, "iOS and Android, from Linux. No Mac needed.", 70, t.ink, 500, tracking=-0.018)
    # Stack: the agent at work, focused.
    ty = y0 + upper_h + GAP
    window(c, t, x0, ty, left_w, lower_h, focused=True)
    terminal_lines(c, t, x0 + inner, ty + 84, 42, 58)
    # The phone it is driving.
    px = x0 + left_w + GAP
    window(c, t, px, y0, right_w, h)
    phone(c, t, px + right_w // 2, y0 + h // 2 + 16, 840, right_w)
    return c


def how_it_works(t):
    c = Canvas(2400, 1320, t.ground)
    m, top, bottom = 80, 80, 1240
    slab_x, slab_w = 660, 500
    height = bottom - top
    line = 4
    # The one API: everything plugs into this slab.
    c.rect(slab_x, top, slab_w, height, ACCENT)
    pad = 44
    c.sans(slab_x + pad, top + 84, NAME, 52, t.on_accent, 700, tracking=-0.015)
    # The count fills the slab's width at most: three wide digits do not fit at full size.
    count = str(len(OPERATIONS))
    size = min(276, int((slab_w - 2 * pad + 10) * 276 / sans_width(count, 276, 800, tracking=-0.03)))
    c.sans(slab_x + pad - 10, top + 372, count, size, t.on_accent, 800, tracking=-0.03)
    c.sans(slab_x + pad, top + 468, "typed", 84, t.on_accent, 700, tracking=-0.025)
    c.sans(slab_x + pad, top + 562, "operations", 84, t.on_accent, 700, tracking=-0.025)
    c.sans(slab_x + pad, bottom - 138, "One schema.", 50, t.on_accent, 500)
    c.sans(slab_x + pad, bottom - 76, "Durable jobs.", 50, t.on_accent, 500)
    # Left: who calls it.
    mid = top + height // 2
    for y0, head, notes in ((top, "Your agent", ("Claude Code, Codex,", "any MCP client.", "Over MCP or the", f"{CLI} JSON CLI.")),
                            (mid, "You", ("In the Qt desktop.", "Watch, and take over", "at any time."))):
        c.line((m, y0 + line // 2), (slab_x, y0 + line // 2), color=t.ink, width=line)
        c.sans(m - 4, y0 + 124, head, 104, t.ink, 700, tracking=-0.03)
        for number, note in enumerate(notes):
            c.sans(m, y0 + 204 + number * 64, note, 50, t.dim)
    # Right: what it does. Each rule leaves the slab.
    rx = slab_x + slab_w
    tx, end = rx + 64, 2400 - m
    rows = [("Build", 240, [op("project.execute"), op("job.wait")]),
            ("Run", 440, [op("project.run"), op("ios.virtual.start")]),
            ("Tap", 240, [op("runtime.input"), op("ios.emulator.control")]),
            ("Debug", 240, [op("debug.start"), op("device.logs")])]
    y = top
    for head, row_h, ops in rows:
        c.line((rx, y + line // 2), (end, y + line // 2), color=t.ink, width=line)
        c.sans(tx - 4, y + 118, head, 104, t.ink, 700, tracking=-0.03)
        if head == "Run":
            c.mono(tx, y + 200, ops[0], 42, t.dim)
            c.mono(tx, y + 258, ops[1], 42, t.dim)
            run_y = y
        else:
            c.mono(tx, y + 192, ops[0], 42, t.dim)
            c.mono(tx + 520, y + 192, ops[1], 42, t.dim)
        y += row_h
    # Run lands on three kinds of device.
    dx = 1820
    stem = 1760
    devices = [("Android", "emulator or phone"), ("iPhone", "over USB, not qualified"), ("Virtual iOS device", "experimental")]
    leaf_ys = [run_y + 52 + i * 134 for i in range(3)]
    for (head, note), ly in zip(devices, leaf_ys):
        c.line((stem, ly), (dx - 16, ly), color=GREEN, width=line + 2)
        c.sans(dx, ly + 18, head, 52, t.ink, 600, tracking=-0.015)
        c.sans(dx, ly + 72, note, 40, t.dim)
    c.line((stem, leaf_ys[0] - 3), (stem, leaf_ys[-1] + 3), color=GREEN, width=line + 2)
    c.line((tx + 205, run_y + 86), (stem, run_y + 86), color=GREEN, width=line + 2)
    return c


def loop(t):
    c = Canvas(2400, 640, t.ground)
    outer, left = 60, 124
    steps = [("write", op("files.write"), "saves the source"),
             ("build", op("project.execute"), "as a durable job"),
             ("run", op("project.run"), "on a chosen device"),
             ("tap", op("runtime.input"), "at x, y from 0 to 1"),
             ("see", op("device.capture"), "PNG inline over MCP"),
             ("fix", op("debug.stack"), "reads the call stack")]
    column = (c.width - outer - left) / len(steps)
    rail, node, back = 84, 28, 560
    xs = [round(left + i * column) for i in range(len(steps))]
    right = c.width - outer
    c.line((xs[0], rail), (xs[-1], rail), color=t.ink, width=4)
    c.line((xs[-1] + node, rail), (right, rail), (right, back), (outer, back), (outer, rail), (xs[0], rail), color=GREEN, width=6)
    for x in xs:
        c.rect(x, rail - node // 2, node, node, ACCENT if x in (xs[0], xs[-1]) else t.ink)
    for x, (verb, name, note) in zip(xs, steps):
        c.sans(x - 5, rail + 170, verb, 124, t.ink, 700, tracking=-0.035)
        c.mono(x, rail + 268, name, 36, t.ink)
        c.sans(x, rail + 326, note, 36, t.dim)
    label = "and again"
    width = sans_width(label, 40) + 56
    c.rect(round(c.width / 2 - width / 2), back - 30, round(width), 60, t.ground)
    c.sans(c.width / 2, back + 14, label, 40, t.dim, anchor="middle")
    return c


def social():
    t = DARK
    c = Canvas(1280, 640, t.ground)
    bar(c, t, 44, 22, pad=26)
    m = 56
    wordmark(c, m - 6, 330, c.width - 2 * m, t.ink)
    c.sans(m, 450, "Your agent builds, runs and taps", 54, t.ink, 500, tracking=-0.02)
    c.sans(m, 520, "through mobile apps. From Linux.", 54, t.ink, 500, tracking=-0.02)
    c.mono(m, 592, f"iOS · Android · {len(OPERATIONS)} typed operations · {CLI}", 26, t.dim)
    return c


def desktop(raw, t):
    """A real capture of the app, in a window on an Omarchy desktop."""
    with Image.open(raw) as image:
        shot_w, shot_h = image.size
    width = 2400
    top = 72
    inner = width - 2 * GAP - 2 * BORDER
    shown = round(shot_h * inner / shot_w)
    height = top + GAP + BORDER + shown + BORDER + GAP
    c = Canvas(width, height, t.ground)
    bar(c, t, top, 36, pad=44)
    c.rect(GAP, top + GAP, width - 2 * GAP, shown + 2 * BORDER, GREEN)
    c.parts.append(f'<image x="{GAP + BORDER}" y="{top + GAP + BORDER}" width="{inner}" height="{shown}" '
                   f'href="source/{raw.name}" preserveAspectRatio="none"/>')
    return c


def capture_app(build, raw):
    """Run the real app offscreen on throwaway state, with no device, and save its window."""
    build = Path(build).resolve()
    raw.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tomacco-capture-") as home:
        home = Path(home)
        env = {**os.environ, "TOMACCO_HOME": str(home / "state"), "XDG_CONFIG_HOME": str(home / "config"),
               "XDG_DATA_HOME": str(home / "data"), "XDG_CACHE_HOME": str(home / "cache"),
               "QT_QPA_PLATFORM": "offscreen", "QT_QPA_PLATFORMTHEME": "generic", "QT_QUICK_CONTROLS_STYLE": "Basic",
               "QT_SCALE_FACTOR": "2"}
        project = home / "Tomatoes"
        arguments = {"path": str(project), "kind": "xtool", "name": "Tomatoes", "organization": "dev.example"}
        created = subprocess.run([str(build / CLI), "call", "project.create", json.dumps(arguments)],
                                 env=env, capture_output=True, text=True, check=True)
        job = json.loads(created.stdout)["result"]["id"]
        subprocess.run([str(build / CLI), "call", "job.wait", json.dumps({"job_id": job, "seconds": 30})],
                       env=env, capture_output=True, text=True, check=True)
        # bwrap only tidies the path the window displays: no real home directory shows.
        shown = "/home/you/Tomatoes"
        wrap = ["bwrap", "--dev-bind", "/", "/", "--tmpfs", "/home", "--dir", "/home/you", "--bind", str(project), shown,
                "--ro-bind", str(build), str(build), "--bind", str(raw.parent), str(raw.parent), "--setenv", "HOME", "/home/you"]
        subprocess.run(wrap + [str(build / "tomacco"), "--workspace", "project", "--project", shown,
                               "-qwindowgeometry", "1320x950", "--capture", str(raw)], env=env, check=True, timeout=60)


def render(c, stem, out):
    svg = out / f"{stem}.svg"
    svg.write_text(c.svg())
    png = out / f"{stem}.png"
    subprocess.run(["rsvg-convert", str(svg), "-o", str(png)], check=True)
    with Image.open(png) as image:
        image = image.convert("RGB")
    image.save(png, optimize=True)
    if png.stat().st_size > 420_000:
        image.quantize(256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(png, optimize=True)
    return png


def main():
    global NAME, CLI
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", help="preview another name without editing this file")
    parser.add_argument("--cli", help="preview another command name")
    parser.add_argument("--out", type=Path, default=HERE)
    parser.add_argument("--capture", metavar="BUILD_DIR", help="retake the screenshot from this build first")
    args = parser.parse_args()
    NAME, CLI = args.name or NAME, args.cli or CLI
    args.out.mkdir(parents=True, exist_ok=True)
    raw = HERE / "source" / "project-workspace.png"
    if args.capture:
        capture_app(args.capture, raw)
    made = [render(hero(), "hero", args.out), render(social(), "social", args.out)]
    # What is uploaded by hand as the repository's social preview.
    shutil.copy(args.out / "social.png", args.out / "social-preview.png")
    for stem, build in (("how-it-works", how_it_works), ("loop", loop)):
        made.append(render(build(DARK), f"{stem}-dark", args.out))
        made.append(render(build(LIGHT), f"{stem}-light", args.out))
    if raw.exists():
        if args.out.resolve() != HERE:  # rsvg-convert reads images only beside the SVG
            (args.out / "source").mkdir(exist_ok=True)
            shutil.copy(raw, args.out / "source" / raw.name)
        made.append(render(desktop(raw, DARK), "desktop", args.out))
    for png in made:
        print(f"{png.stat().st_size // 1024:5d} KB  {png}")


if __name__ == "__main__":
    main()
