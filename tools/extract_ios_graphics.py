#!/usr/bin/env python3
"""Validate and export real iOS research CPU renders from the third serial port.

Exports the guest's original BGRA bytes and a lossless PNG. These are offscreen
CoreGraphics/CALayer/UIKit renders, not screenshots of a functioning iPhone display.
Only complete, checksummed frames are accepted; input is validated before output.
"""
import argparse
import binascii
import hashlib
import json
from pathlib import Path
import re
import struct
import zlib

MAX_LOG = 4 * 1024 * 1024
WIDTH, HEIGHT, STRIDE = 256, 384, 1024
SIZE = STRIDE * HEIGHT
RENDERERS = ("coregraphics", "calayer")
PROFILES = {"layers": RENDERERS, "uikit": RENDERERS + ("uikit-before", "uikit-after")}
PROFILE = re.compile(r"TOMACCO_GRAPHICS_PROFILE profile=(layers|uikit) frames=([0-9]+)")
STATE = re.compile(r"TOMACCO_GRAPHICS_STATE phase=(before|after) action_count=([0-9]+) switch_on=([01])")
FINISHED = re.compile(r"TOMACCO_GRAPHICS finished=1 frames=([0-9]+)")
BEGIN = re.compile(r"TOMACCO_GRAPHICS_BEGIN renderer=(coregraphics|calayer|uikit-before|uikit-after) width=([0-9]+) height=([0-9]+) stride=([0-9]+) format=bgra8-premultiplied encoding=zlib bytes=([0-9]+) encoded_bytes=([0-9]+) crc32=([0-9a-f]{8})")
CHUNK = re.compile(r"TOMACCO_GRAPHICS_DATA offset=([0-9]+) hex=([0-9a-f]{2,512})")
END = re.compile(r"TOMACCO_GRAPHICS_END renderer=(coregraphics|calayer|uikit-before|uikit-after) bytes=([0-9]+) crc32=([0-9a-f]{8})")


def parse_log(raw):
    if len(raw) > MAX_LOG:
        raise ValueError("Graphics log exceeds 4 MiB")
    frames, current, finished = [], None, False
    profile, states, started = "layers", [], False
    for line in raw.decode("ascii", errors="strict").splitlines():
        if not line.startswith("TOMACCO_GRAPHICS"):
            if line.strip():
                raise ValueError("Unexpected data on the dedicated graphics UART")
            continue
        if finished:
            raise ValueError("Data after graphics completion")
        if line.startswith("TOMACCO_GRAPHICS_PROFILE"):
            match = PROFILE.fullmatch(line)
            if not match or started or int(match[2]) != len(PROFILES[match[1]]):
                raise ValueError("Malformed, misplaced or duplicate graphics profile")
            profile = match[1]
        elif line.startswith("TOMACCO_GRAPHICS_STATE"):
            match = STATE.fullmatch(line)
            index = len(states)
            if (not match or profile != "uikit" or current is not None or index >= 2 or
                    len(frames) != index + 2 or match[1] != ("before", "after")[index] or
                    int(match[2]) > 0xffffffff):
                raise ValueError("Malformed, misplaced or unexpected UIKit control state")
            states.append({"phase": match[1], "action_count": int(match[2]), "switch_on": bool(int(match[3]))})
        elif line.startswith("TOMACCO_GRAPHICS_BEGIN"):
            match = BEGIN.fullmatch(line)
            renderers = PROFILES[profile]
            if not match or current is not None or len(frames) >= len(renderers):
                raise ValueError("Malformed, overlapping or duplicate graphics frame")
            renderer, width, height, stride, size, encoded_size, crc = match.groups()
            if renderer != renderers[len(frames)] or tuple(map(int, (width, height, stride, size))) != (WIDTH, HEIGHT, STRIDE, SIZE):
                raise ValueError("Unexpected renderer, frame order or geometry")
            if renderer.startswith("uikit-") and len(states) != len(frames) - 1:
                raise ValueError("UIKit frame without its matching control state")
            if not 1 <= int(encoded_size) <= SIZE + 4096:
                raise ValueError("Compressed frame exceeds producer budget")
            current = {"renderer": renderer, "crc32": crc, "encoded_bytes": int(encoded_size), "data": bytearray()}
        elif line.startswith("TOMACCO_GRAPHICS_DATA"):
            match = CHUNK.fullmatch(line)
            if not match or current is None:
                raise ValueError("Malformed or orphaned graphics chunk")
            offset, hex_data = match.groups()
            chunk = bytes.fromhex(hex_data)
            remaining = current["encoded_bytes"] - len(current["data"])
            if int(offset) != len(current["data"]) or len(chunk) != min(256, remaining) or remaining <= 0:
                raise ValueError("Out-of-order or oversized graphics chunk")
            current["data"].extend(chunk)
        elif line.startswith("TOMACCO_GRAPHICS_END"):
            match = END.fullmatch(line)
            if not match or current is None:
                raise ValueError("Malformed or orphaned graphics boundary")
            renderer, size, crc = match.groups()
            if len(current["data"]) != current["encoded_bytes"]:
                raise ValueError("Truncated compressed frame")
            decoder = zlib.decompressobj()
            try:
                data = decoder.decompress(current["data"], SIZE + 1)
            except zlib.error as error:
                raise ValueError("Invalid compressed graphics data") from error
            if len(data) != SIZE or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                raise ValueError("Oversized, truncated or trailing compressed data")
            if (renderer != current["renderer"] or int(size) != SIZE or len(data) != SIZE or
                    crc != current["crc32"] or crc != f"{binascii.crc32(data):08x}"):
                raise ValueError("Truncated graphics frame or CRC32 mismatch")
            current["data"] = data
            frames.append(current)
            current = None
        elif line.startswith("TOMACCO_GRAPHICS finished="):
            match = FINISHED.fullmatch(line)
            expected = len(PROFILES[profile])
            if not match or current is not None or len(frames) != expected or int(match[1]) != expected:
                raise ValueError("Graphics completion without all expected frames")
            if profile == "uikit" and any(state["action_count"] != index or state["switch_on"] != bool(index)
                                          for index, state in enumerate(states)):
                raise ValueError("UIKit completion without the expected control state transition")
            finished = True
        elif line.startswith("TOMACCO_GRAPHICS "):
            if current is not None:
                raise ValueError("Diagnostic interrupted a graphics frame")
        else:
            raise ValueError("Unrecognized graphics record")
        started = True
    if current is not None:
        raise ValueError("Truncated graphics transfer")
    if not frames:
        raise ValueError("No complete graphics frames; inspect guest diagnostics")
    return frames, finished, profile, states


def png_bytes(data):
    """The probe paints an opaque background; reject unsupported alpha semantics.

    Keeping row order preserves CGBitmapContext's original pixel layout. BGRA
    is converted to RGB only after checking every alpha byte is opaque.
    """
    if len(data) != SIZE or any(alpha != 255 for alpha in data[3::4]):
        raise ValueError("Expected a complete opaque BGRA frame")

    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload +
                struct.pack(">I", binascii.crc32(kind + payload)))

    rows = bytearray()
    for y in range(HEIGHT):
        rows.append(0)  # PNG filter: None
        for offset in range(y * STRIDE, (y + 1) * STRIDE, 4):
            rows.extend((data[offset + 2], data[offset + 1], data[offset]))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(bytes(rows))) + chunk(b"IEND", b""))


def extract(source, output):
    if not source.is_file():
        raise ValueError("Graphics input must be a regular file")
    with source.open("rb") as handle:
        raw = handle.read(MAX_LOG + 1)
    frames, finished, profile, states = parse_log(raw)
    images = [png_bytes(frame["data"]) for frame in frames]
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; choose a new directory")
    output.mkdir(mode=0o700, parents=True)
    result = {"format": 1, "scope": "guest-offscreen-cpu-graphics", "source": str(source.resolve()),
              "source_sha256": hashlib.sha256(raw).hexdigest(), "finished": finished,
              "profile": profile, "expected_renderers": list(PROFILES[profile]), "control_states": states,
              "main_display_qualified": False, "graphical_apps_qualified": False, "frames": []}
    for frame, png in zip(frames, images):
        name, data = frame["renderer"], frame["data"]
        files = {}
        for suffix, payload in (("bgra", data), ("png", png)):
            file = output / (name + "." + suffix)
            with file.open("xb") as handle:
                handle.write(payload)
            file.chmod(0o600)
            files[suffix] = {"path": file.name, "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
        result["frames"].append({"renderer": name, "width": WIDTH, "height": HEIGHT, "stride": STRIDE,
                                 "format": "bgra8-premultiplied", "crc32": frame["crc32"],
                                 "encoded_bytes": frame["encoded_bytes"], "files": files})
    (output / "manifest.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--console", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(extract(args.console, args.output), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error))
