#!/usr/bin/env python3
"""Profile which guest code the paravirtual iPhone's TCG vCPUs execute.

  build   compile integrations/ios-emulator/tcg-profile.c, a QEMU TCG plugin,
          against the engine's include/plugins/qemu-plugin.h (cached by
          content under .tooling/tcg-profile)
  report  render a tomacco-tcg-profile JSON report as text; kernel-half PCs
          are placed in the kernelcache's fileset entries (and SPTM/TXM
          when given) at a slide found by matching block hashes
  mark    ask a running profile for a window: the change since the previous
          one is written next to the report

The launcher (tools/run_ios_paravirt.py --tcg-profile) builds, loads and
renders it. Only addresses, counts and hashes leave the guest unless the
plugin's code=on option is given; images are read locally and nothing from
them is copied into a report.
"""
import argparse
import bisect
from collections import Counter
import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "integrations/ios-emulator/tcg-profile.c"
# The pinned engine checkout tools/build_ios_paravirt.py builds from.
# The pinned engine checkout's plugin API, else the copy of the same header
# shipped with the integrations (an installed copy has no .tooling).
PINNED_INCLUDE = REPO / ".tooling/ios-paravirt/sptm-source/include/plugins"
SHIPPED_INCLUDE = REPO / "integrations/ios-emulator"
DEFAULT_INCLUDE = PINNED_INCLUDE if (PINNED_INCLUDE / "qemu-plugin.h").is_file() else SHIPPED_INCLUDE
CACHE = REPO / ".tooling/tcg-profile"
PLUGIN_NAME = "libtomacco-tcg-profile.so"
CFLAGS = ("-O2", "-fPIC", "-shared", "-std=gnu11", "-Wall", "-Wextra", "-Wno-unused-parameter")
FORMAT = "tomacco-tcg-profile"
M64 = (1 << 64) - 1
# The shared region the kernel adapter creates (tools/ios_paravirt_kernel.py).
SHARED_REGION = (0x180000000, 0x180000000 + 0x1a0000000)
SLIDE_ALIGN = 0x4000


# ---- building -------------------------------------------------------------

def include_directory(include=None):
    directory = Path(include or os.environ.get("TOMACCO_QEMU_PLUGIN_INCLUDE") or DEFAULT_INCLUDE)
    if not (directory / "qemu-plugin.h").is_file():
        raise ValueError(f"No qemu-plugin.h in {directory}; name the engine's QEMU include/plugins "
                         "directory (--include or TOMACCO_QEMU_PLUGIN_INCLUDE)")
    return directory


def plugin_api_version(header):
    match = re.search(rb"#define\s+QEMU_PLUGIN_VERSION\s+(\d+)", header)
    if not match:
        raise ValueError("qemu-plugin.h names no QEMU_PLUGIN_VERSION")
    return int(match[1])


def glib_flags():
    try:
        done = subprocess.run(["pkg-config", "--cflags", "--libs", "glib-2.0"],
                              capture_output=True, text=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("Building the profile plugin needs pkg-config and glib-2.0") from error
    return done.stdout.split()


def build_plugin(include=None, cache=None, compiler=None):
    """The plugin for this source and header, built once per content."""
    include = include_directory(include)
    header = (include / "qemu-plugin.h").read_bytes()
    source = SOURCE.read_bytes()
    if plugin_api_version(header) < 7:
        raise ValueError("The profile plugin needs QEMU plugin API version 7 or later")
    cc = compiler or os.environ.get("CC") or shutil.which("cc") or shutil.which("gcc")
    if not cc:
        raise ValueError("Building the profile plugin needs a C compiler (cc)")
    flags = glib_flags()
    try:
        version = subprocess.run([cc, "--version"], capture_output=True, text=True,
                                 timeout=30).stdout.splitlines()[:1]
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"The C compiler {cc} does not run") from error
    identity = {"source_sha256": hashlib.sha256(source).hexdigest(),
                "header_sha256": hashlib.sha256(header).hexdigest(),
                "plugin_api_version": plugin_api_version(header),
                "compiler": cc, "compiler_version": version, "flags": [*CFLAGS, *flags]}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    directory = Path(cache or CACHE) / key
    plugin, receipt_path = directory / PLUGIN_NAME, directory / "receipt.json"
    try:
        receipt = json.loads(receipt_path.read_text())
        if (receipt.get("identity") == identity and
                receipt.get("plugin_sha256") == hashlib.sha256(plugin.read_bytes()).hexdigest()):
            return receipt
    except (OSError, ValueError):
        pass
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = directory / f".{PLUGIN_NAME}.{os.getpid()}.tmp"
    try:
        done = subprocess.run([cc, *CFLAGS, "-I" + str(include), str(SOURCE), "-o", str(temporary), *flags],
                              capture_output=True, text=True, timeout=300)
        if done.returncode:
            raise ValueError("The profile plugin did not compile:\n" + done.stderr[-2000:])
        os.replace(temporary, plugin)
    finally:
        temporary.unlink(missing_ok=True)
    receipt = {"plugin": str(plugin), "plugin_sha256": hashlib.sha256(plugin.read_bytes()).hexdigest(),
               "include": str(include), "identity": identity}
    staged = directory / f".receipt.{os.getpid()}.tmp"
    staged.write_text(json.dumps(receipt, indent=2) + "\n")
    os.replace(staged, receipt_path)
    return receipt


def profile_paths(report):
    """Where the plugin writes for a report path (as tcg-profile.c names them)."""
    report = str(report)
    stem = report[:-5] if report.endswith(".json") else report
    return {"report": report, "mark": stem + ".mark", "windows": stem + ".window-*.json",
            "text": stem + ".txt"}


def plugin_argument(plugin, report, *, interval=None, top=None, code=False):
    """The value of QEMU's -plugin option (commas in paths doubled)."""
    escape = lambda value: str(value).replace(",", ",,")
    parts = ["file=" + escape(plugin), "out=" + escape(report)]
    if interval:
        parts.append(f"interval={interval:g}")
    if top:
        parts.append(f"top={int(top)}")
    if code:
        parts.append("code=on")
    return ",".join(parts)


def mark(report, label=""):
    """Request a window from a running profile; the plugin removes the file."""
    label = "".join(c for c in label if 32 <= ord(c) < 127)[:120]
    target = Path(profile_paths(report)["mark"])
    staged = target.with_name(target.name + f".{os.getpid()}.tmp")
    staged.write_text(label + "\n")
    os.replace(staged, target)
    return target


# ---- images ---------------------------------------------------------------

LC_SEGMENT_64, LC_FILESET_ENTRY = 0x19, 0x80000035
MH_MAGIC_64 = 0xfeedfacf
VM_PROT_EXECUTE = 4


def macho_commands(data, base=0):
    """(segments, fileset entries) of the 64-bit little-endian Mach-O at `base`."""
    if len(data) < base + 32:
        raise ValueError("Truncated Mach-O header")
    magic, _, _, _, ncmds, sizeofcmds = struct.unpack_from("<IiiIII", data, base)
    if magic != MH_MAGIC_64:
        raise ValueError("Not a raw 64-bit Mach-O (an IM4P or compressed kernel must be unwrapped first)")
    segments, entries = [], []
    offset, end = base + 32, base + 32 + sizeofcmds
    if end > len(data):
        raise ValueError("Mach-O load commands run past the file")
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<II", data, offset)
        if size < 8 or offset + size > end:
            raise ValueError("Malformed Mach-O load command")
        if cmd == LC_SEGMENT_64:
            name = data[offset + 8:offset + 24].split(b"\0", 1)[0].decode("ascii", "replace")
            vmaddr, vmsize, fileoff, filesize, _, initprot = struct.unpack_from("<QQQQii", data, offset + 24)
            segments.append({"name": name, "vmaddr": vmaddr, "vmsize": vmsize, "fileoff": fileoff,
                             "filesize": filesize, "initprot": initprot})
        elif cmd == LC_FILESET_ENTRY:
            vmaddr, fileoff, name_offset = struct.unpack_from("<QQI", data, offset + 8)
            raw = data[offset + name_offset:offset + size].split(b"\0", 1)[0]
            entries.append({"name": raw.decode("utf-8", "replace"), "vmaddr": vmaddr, "fileoff": fileoff})
        offset += size
    return segments, entries


class Image:
    """A guest image's address ranges: fileset entries' segments for names,
    the file's own segments for reading code at an address."""

    def __init__(self, label, path):
        self.label, self.path = label, str(path)
        with open(path, "rb") as stream:
            self.data = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
        segments, entries = macho_commands(self.data)
        self.segments = [s for s in segments if s["filesize"] and s["fileoff"] + s["filesize"] <= len(self.data)]
        self.ranges = []
        known = {(s["vmaddr"], s["fileoff"]) for s in self.segments}
        for entry in entries:
            try:
                for segment in macho_commands(self.data, entry["fileoff"])[0]:
                    if segment["vmsize"]:
                        self.ranges.append((segment["vmaddr"], segment["vmaddr"] + segment["vmsize"],
                                            entry["name"], segment["name"]))
                    # A fileset entry's segments use offsets in the whole
                    # file; they also serve for reading code when the file's
                    # own header has no covering segment.
                    if (segment["filesize"] and (segment["vmaddr"], segment["fileoff"]) not in known
                            and segment["fileoff"] + segment["filesize"] <= len(self.data)):
                        known.add((segment["vmaddr"], segment["fileoff"]))
                        self.segments.append(segment)
            except (ValueError, struct.error):
                continue
        if not self.ranges:
            self.ranges = [(s["vmaddr"], s["vmaddr"] + s["vmsize"], label, s["name"])
                           for s in segments if s["vmsize"]]
        self.ranges.sort()
        self.starts = [r[0] for r in self.ranges]
        self.slide = None

    def code(self, address, length):
        for s in self.segments:
            if s["initprot"] & VM_PROT_EXECUTE and s["vmaddr"] <= address and address + length <= s["vmaddr"] + s["filesize"]:
                offset = s["fileoff"] + address - s["vmaddr"]
                return self.data[offset:offset + length]
        return None

    def locate(self, unslid):
        index = bisect.bisect_right(self.starts, unslid) - 1
        if index >= 0:
            start, end, name, segment = self.ranges[index]
            if unslid < end:
                return name, segment, unslid - start
        return None


def fnv1a(data):
    value = 2166136261
    for byte in data:
        value = ((value ^ byte) * 16777619) & 0xffffffff
    return value


def detect_slide(image, blocks, sample=12):
    """The slide at which this image's code matches the hottest blocks'
    instruction hashes; None when fewer than two (or the only) block match."""
    votes, tested = Counter(), 0
    for block in blocks[:sample]:
        pc, length, wanted = int(block["pc"], 16), int(block["bytes"]), int(block["hash"], 16)
        if not length:
            continue
        tested += 1
        hits = set()
        for s in image.segments:
            if not s["initprot"] & VM_PROT_EXECUTE or s["filesize"] < length:
                continue
            low, high = s["vmaddr"], s["vmaddr"] + s["filesize"] - length
            address = low + ((pc - low) % SLIDE_ALIGN)
            base = s["fileoff"] - low
            data = image.data
            while address <= high:
                offset = base + address
                if fnv1a(data[offset:offset + length]) == wanted:
                    hits.add((pc - address) & M64)
                address += SLIDE_ALIGN
        votes.update(hits)
    if not votes:
        return None
    slide, matched = votes.most_common(1)[0]
    if matched < min(2, tested):
        return None
    return {"slide": slide, "matched": matched, "tested": tested}


def user_region(pc):
    if SHARED_REGION[0] <= pc < SHARED_REGION[1]:
        return "dyld shared region"
    if pc < SHARED_REGION[0]:
        return "user image below the shared region"
    return "user above the shared region"


class Locator:
    def __init__(self, images=(), kernel_blocks=(), slides=None):
        self.images = []
        self.notes = []
        for label, path in images:
            try:
                image = Image(label, path)
            except (OSError, ValueError, struct.error) as error:
                self.notes.append(f"{label}: {error}")
                continue
            if slides and label in slides:
                image.slide = {"slide": slides[label] & M64, "matched": None, "tested": 0, "given": True}
            else:
                image.slide = detect_slide(image, list(kernel_blocks))
            if image.slide is None:
                self.notes.append(f"{label}: no slide matched the hottest kernel-half blocks")
            self.images.append(image)

    def where(self, pc):
        if not pc >> 63:
            return user_region(pc)
        for image in self.images:
            if image.slide is None:
                continue
            unslid = (pc - image.slide["slide"]) & M64
            found = image.locate(unslid)
            if found:
                name, segment, offset = found
                return f"{name} {segment}+0x{offset:x} (unslid 0x{unslid:x})"
        return "kernel half (no image)"

    def image_of(self, pc):
        if not pc >> 63:
            return user_region(pc)
        for image in self.images:
            if image.slide is None:
                continue
            found = image.locate((pc - image.slide["slide"]) & M64)
            if found:
                return found[0]
        return "kernel half (no image)"

    def receipt(self):
        return {"images": [{"label": i.label, "path": i.path,
                            "slide": None if i.slide is None else "0x%x" % i.slide["slide"],
                            "matched_blocks": None if i.slide is None else i.slide["matched"],
                            "tested_blocks": None if i.slide is None else i.slide["tested"]}
                           for i in self.images],
                "notes": self.notes}


# ---- reports --------------------------------------------------------------

def load(path):
    data = json.loads(Path(path).read_text())
    if data.get("format") != FORMAT:
        raise ValueError(f"{path} is not a {FORMAT} report")
    return data


def share(part, whole):
    return f"{100 * part / whole:5.1f}%" if whole else "    -"


def big(value):
    for unit, size in (("G", 1e9), ("M", 1e6), ("k", 1e3)):
        if value >= size:
            return f"{value / size:.2f}{unit}"
    return str(value)


def summarize(profile, locator, top=20):
    """A short machine-readable summary for the launcher's receipt."""
    totals = profile["totals"]
    by_image = Counter()
    for block in profile["top_blocks"]:
        by_image[locator.image_of(int(block["pc"], 16))] += block["insns"]
    return {"seconds": profile["seconds"], "totals": totals,
            "special": profile["special"][:10],
            "top_blocks": [{"pc": b["pc"], "where": locator.where(int(b["pc"], 16)),
                            "share": b["share"], "tb_execs": b["tb_execs"], "insns_per_tb": b["insns_per_tb"],
                            "mix": b["mix"], "special": b["special"]}
                           for b in profile["top_blocks"][:top]],
            "top_block_insns_by_image": by_image.most_common(10),
            "images": locator.receipt()}


def render(profile, locator, top=60, pages=40):
    t = profile["totals"]
    total = t["insns"]
    lines = [f"tomacco TCG profile: {profile['kind']}"
             + (f" window {profile['window']}" if "window" in profile else "")
             + (f" '{profile['label']}'" if profile.get("label") else "")
             + f", {profile['start_seconds']:.1f}-{profile['end_seconds']:.1f} s ({profile['seconds']:.1f} s),"
             f" {profile['vcpus']} vCPUs, {profile['executed_blocks']} of {profile['blocks']} blocks executed",
             f"Executed instructions (estimate): {big(total)}  kernel half {share(t['kernel_insns'], total)}"
             f"  user half {share(t['user_insns'], total)}  TB entries {big(t['tb_execs'])}"
             f"  ({total / t['tb_execs']:.1f} instructions per TB)" if t["tb_execs"] else "No TB executed",
             "Note: " + profile["estimate"]]
    for image in locator.receipt()["images"]:
        lines.append(f"Image {image['label']}: {image['path']} slide {image['slide']}"
                     f" ({image['matched_blocks']} of {image['tested_blocks']} hot blocks matched)")
    lines += [f"Note: {note}" for note in locator.notes]
    if t.get("overflow_tb_execs"):
        lines.append(f"Overflow (blocks past max_blocks): {big(t['overflow_insns'])} instructions")
    seconds = profile["seconds"] or 1
    lines += ["", "Per vCPU          TB entries   instructions  kernel  idle   interrupts  exceptions  hostcalls"]
    for v in profile["per_vcpu"]:
        lines.append(f"  vcpu {v['vcpu']:<3}  {big(v['tb_execs']):>14} {big(v['insns']):>14}  {share(v['kernel_insns'], v['insns'])}"
                     f" {100 * v['idle_seconds'] / seconds:5.1f}%  {v['interrupts']:>10}  {v['exceptions']:>10}  {v['hostcalls']:>9}")
    lines += ["", "Instruction classes (share of executed instructions)   kernel half   user half"]
    classes = profile["classes"]
    names = sorted(set(classes["kernel"]) | set(classes["user"]),
                   key=lambda n: -(classes["kernel"].get(n, 0) + classes["user"].get(n, 0)))
    for name in names:
        k, u = classes["kernel"].get(name, 0), classes["user"].get(name, 0)
        lines.append(f"  {name:<12} {share(k + u, total)}   {big(k):>10} {share(k, t['kernel_insns'])}"
                     f"   {big(u):>10} {share(u, t['user_insns'])}")
    lines += ["", "System, exception and helper-heavy instructions (executions)"]
    for s in profile["special"][:40]:
        where = ", ".join(f"{x['pc']} {locator.where(int(x['pc'], 16))}" for x in s["top"][:2])
        lines.append(f"  {s['name']:<34} {big(s['count']):>9}  kernel {share(s['kernel'], s['count'])}  {where}")
    by_image = Counter()
    for page in profile["pages"]:
        by_image[locator.image_of(int(page["page"], 16))] += page["insns"]
    lines += ["", f"By image or region (top {len(profile['pages'])} of {profile['pages_total']} pages)"]
    for name, insns in by_image.most_common(25):
        lines.append(f"  {share(insns, total)}  {name}")
    lines += ["", f"Hottest {1 << (profile['page_bits'] - 10)} KiB pages"]
    for page in profile["pages"][:pages]:
        lines.append(f"  {page['page']}  {share(page['insns'], total)}  {page['blocks']:>5} blocks  "
                     f"{locator.where(int(page['page'], 16))}")
    lines += ["", "Hottest blocks (by estimated executed instructions)",
              "  rank  pc                  share   cum   TB entries  insns  where / mix / special"]
    for b in profile["top_blocks"][:top]:
        mix = " ".join(f"{k}={v}" for k, v in b["mix"].items())
        special = ("  [" + ", ".join(b["special"]) + "]") if b["special"] else ""
        lines.append(f"  {b['rank']:>4}  {b['pc']}  {share(b['insns'], total)} {100 * b['cumulative']:5.1f}%"
                     f"  {big(b['tb_execs']):>10}  {b['insns_per_tb']:>5}  {locator.where(int(b['pc'], 16))}")
        lines.append(f"{'':>34}{mix}{special}" + (f"  code {b['code']}" if b.get("code") else ""))
    if profile.get("exception_pcs"):
        lines += ["", "Synchronous exceptions by origin (system calls, faults, traps)"]
        for e in profile["exception_pcs"][:30]:
            lines.append(f"  {e['from_pc']}  {e['count']:>10}  {e['vector']:<18} {locator.where(int(e['from_pc'], 16))}")
    return "\n".join(lines) + "\n"


def kernel_blocks(profile):
    return [b for b in profile["top_blocks"] if int(b["pc"], 16) >> 63]


def locator_for(profile, images=(), slides=None):
    return Locator(images, kernel_blocks(profile), slides)


def render_files(report, images=(), text=None, slides=None):
    """Render `report` (and any window reports) to text; the summary of the report."""
    profile = load(report)
    locator = locator_for(profile, images, slides)
    paths = profile_paths(report)
    Path(text or paths["text"]).write_text(render(profile, locator))
    for window in sorted(Path(paths["report"]).parent.glob(Path(paths["windows"]).name)):
        try:
            data = load(window)
        except (OSError, ValueError):
            continue
        window.with_suffix(".txt").write_text(render(data, locator))
    return summarize(profile, locator)


def parse_image(value):
    label, _, path = value.partition("=")
    if not label or not path:
        raise argparse.ArgumentTypeError("use LABEL=PATH, e.g. kernel=kernelcache.research.vphone600")
    return label, path


def parse_slide(value):
    label, _, slide = value.partition("=")
    try:
        return label, int(slide, 0)
    except ValueError:
        raise argparse.ArgumentTypeError("use LABEL=0xSLIDE") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Compile the plugin; prints its receipt")
    build.add_argument("--include", type=Path, help="The engine's QEMU include/plugins directory")
    build.add_argument("--cache", type=Path, help="Directory for built plugins (default .tooling/tcg-profile)")
    report = commands.add_parser("report", help="Render a JSON report as text")
    report.add_argument("profile", type=Path)
    report.add_argument("--request", type=Path,
                        help="The run's request.json: take kernel, SPTM and TXM paths from its inputs")
    report.add_argument("--image", type=parse_image, action="append", default=[],
                        help="LABEL=PATH of a raw Mach-O (kernelcache, SPTM, TXM); repeatable")
    report.add_argument("--slide", type=parse_slide, action="append", default=[],
                        help="LABEL=0xSLIDE instead of detecting it")
    report.add_argument("--top", type=int, default=60)
    report.add_argument("--output", type=Path, help="Write here instead of standard output")
    marker = commands.add_parser("mark", help="Ask a running profile for a window")
    marker.add_argument("profile", type=Path, help="The report path the profile writes at exit")
    marker.add_argument("--label", default="")
    args = parser.parse_args(argv)
    if args.command == "build":
        print(json.dumps(build_plugin(args.include, args.cache), indent=2))
    elif args.command == "mark":
        print(mark(args.profile, args.label))
    else:
        images = list(args.image)
        if args.request:
            inputs = json.loads(args.request.read_text()).get("inputs", {})
            images += [(label, inputs[key]["path"]) for label, key in
                       (("kernel", "bootkc"), ("sptm", "sptm"), ("txm", "txm")) if key in inputs]
        profile = load(args.profile)
        text = render(profile, locator_for(profile, images, dict(args.slide)), top=args.top)
        if args.output:
            args.output.write_text(text)
        else:
            sys.stdout.write(text)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
