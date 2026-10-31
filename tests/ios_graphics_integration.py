#!/usr/bin/env python3
"""Check actual iOS CPU-render evidence; requires a captured research UART log."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import extract_ios_graphics as graphics


def verify(console, output):
    result = graphics.extract(console, output)
    if not result["finished"]:
        raise ValueError("All requested real guest rendering stages must finish")
    samples = {
        "coregraphics": {(0, 0): (0, 0, 0, 255),
                         (64, 320): (0, 0, 255, 255),
                         (184, 320): (0, 255, 0, 255),
                         (128, 154): (255, 0, 0, 255)},
        "calayer": {(0, 0): (0, 0, 0, 255),
                    (255, 383): (0, 0, 0, 255),
                    (128, 192): (255, 0, 255, 255),
                    (16, 16): (255, 0, 255, 255)},
        "uikit-before": {(0, 0): (255, 255, 255, 255),
                         (255, 383): (255, 255, 255, 255),
                         (128, 190): (230, 89, 0, 255),
                         (60, 260): (199, 197, 197, 255)},
        "uikit-after": {(0, 0): (255, 255, 255, 255),
                        (255, 383): (255, 255, 255, 255),
                        (128, 190): (230, 89, 0, 255),
                        (30, 255): (89, 199, 52, 255),
                        (60, 260): (255, 255, 255, 255)},
    }
    checked = []
    for frame in result["frames"]:
        data = (output / frame["files"]["bgra"]["path"]).read_bytes()
        for (x, y), expected in samples[frame["renderer"]].items():
            offset = y * frame["stride"] + x * 4
            actual = tuple(data[offset:offset + 4])
            if actual != expected:
                raise ValueError(f"{frame['renderer']} pixel ({x},{y}): {actual} != {expected}")
        colors = {data[offset:offset + 4] for offset in range(0, len(data), 4)}
        if len(colors) < 8:
            raise ValueError("Missing antialiased shape edges")
        checked.append({"renderer": frame["renderer"], "sampled_pixels": len(samples[frame["renderer"]]),
                        "unique_colors": len(colors), "decoded_bytes": len(data),
                        "encoded_bytes": frame["encoded_bytes"], "pixel_checks_passed": True})
    interaction = None
    if result["profile"] == "uikit":
        before = (output / "uikit-before.bgra").read_bytes()
        after = (output / "uikit-after.bgra").read_bytes()
        text_changed = switch_changed = 0
        for offset in range(0, len(before), 4):
            if before[offset:offset + 4] == after[offset:offset + 4]:
                continue
            x, y = (offset // 4) % graphics.WIDTH, offset // graphics.STRIDE
            if 16 <= x < 240 and 72 <= y < 132:
                text_changed += 1
            elif 16 <= x < 80 and 240 <= y < 288:
                switch_changed += 1
            else:
                raise ValueError(f"Unexpected UIKit change outside the label/switch at ({x},{y})")
        if min(text_changed, switch_changed) < 50:
            raise ValueError("Control callback must visibly update both text and switch")
        interaction = {"dispatch": "UIControl.sendActionsForControlEvents/UIAction",
                       "control_states": result["control_states"],
                       "text_changed_pixels": text_changed, "switch_changed_pixels": switch_changed,
                       "unchanged_rest_of_frame": True, "touch_input_qualified": False}
    verification = {"source_sha256": result["source_sha256"], "profile": result["profile"],
                    "frames": checked, "interaction": interaction,
                    "full_ios_boot_qualified": False, "graphical_apps_qualified": False}
    (output / "verification.json").write_text(json.dumps(verification, indent=2) + "\n")
    return verification


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--console", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.console, args.output), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error))
