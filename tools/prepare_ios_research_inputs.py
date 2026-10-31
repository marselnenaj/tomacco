#!/usr/bin/env python3
"""Prepare copied, version-pinned inputs for disposable APFS boot research.

This enables Apple's unencrypted no-SEP test path. It does not implement a
Secure Enclave, persistent storage, a normal verified boot or graphical apps.
Normal Tomacco guest jobs do not call this recipe.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

SOURCES = Path(__file__).resolve().parents[1] / "integrations/ios-emulator"
MAX_INPUT_BYTES = 256 * 1024 * 1024


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_input(path):
    if not path.is_file() or not 1 <= path.stat().st_size <= MAX_INPUT_BYTES:
        raise ValueError("Research input must be a regular file of at most 256 MiB")
    with path.open("rb") as stream:
        data = stream.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError("Research input exceeds 256 MiB")
    return data


def apply_edits(data, recipe):
    if sha256(data) != recipe["input_sha256"]:
        raise ValueError("Input SHA-256 does not match this research profile")
    edits = sorted(recipe["edits"], key=lambda entry: entry["offset"])
    previous_end = -1
    checked = []
    for edit in edits:
        offset = edit["offset"]
        before, after = bytes.fromhex(edit["before"]), bytes.fromhex(edit["after"])
        if (type(offset) is not int or offset < 0 or offset <= previous_end or
                offset + len(before) > len(data) or not (before or after)):
            raise ValueError("Invalid or overlapping research edit")
        if data[offset:offset + len(before)] != before:
            raise ValueError("Expected research instruction/property bytes do not match")
        previous_end = offset + max(1, len(before)) - 1
        checked.append((offset, before, after))
    result = bytearray(data)
    for offset, before, after in reversed(checked):
        result[offset:offset + len(before)] = after
    if sha256(result) != recipe["output_sha256"]:
        raise ValueError("Patched SHA-256 does not match the research profile")
    return bytes(result)


def instruction_recipe(recipe):
    return {**recipe, "edits": [
        {"offset": entry["offset"],
         "before": struct.pack("<I", int(entry["before"], 16)).hex(),
         "after": struct.pack("<I", int(entry["after"], 16)).hex()}
        for entry in recipe["instructions"]]}


def prepare(kernel_path, tree_path, output, ramdisk, profile, data_layout="split-users",
            hardware_profile="none"):
    # Check everything before creating output. Never write to an input file.
    if data_layout not in ("split-users", "single-data"):
        raise ValueError("Unknown research Data layout")
    if hardware_profile not in ("none", "dcp-dart", "exclave-dart", "exclave-mcc"):
        raise ValueError("Unknown research hardware profile")
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; choose a new directory")
    kernel_path, tree_path = kernel_path.resolve(strict=True), tree_path.resolve(strict=True)
    kernel, tree = read_input(kernel_path), read_input(tree_path)
    kernel_input_hash = sha256(kernel)
    if kernel_input_hash == ramdisk["input_sha256"]:
        kernel = apply_edits(kernel, instruction_recipe(ramdisk))
    kernel = apply_edits(kernel, instruction_recipe(profile["kernel"]))
    tree = apply_edits(tree, profile["device_tree"])
    if data_layout == "single-data":
        tree = apply_edits(tree, profile["single_data_tree"])
    recipes = [] if hardware_profile == "none" else ["ios27-dcp-dart.json"]
    if hardware_profile in ("exclave-dart", "exclave-mcc"):
        recipes.append("ios27-exclave-dart.json")
    if hardware_profile == "exclave-mcc":
        recipes.append("ios27-exclave-mcc.json")
    for recipe in recipes:
        hardware = json.loads((SOURCES / recipe).read_text())
        if (data_layout != hardware["data_layout"] or
                any(profile[key] != hardware[key] for key in ("firmware", "build", "device"))):
            raise ValueError("DCP/DART requires its matching firmware and single-data layout")
        tree = apply_edits(tree, hardware["device_tree"])
    # A concurrent input change invalidates the record instead of being ignored.
    if (sha256(read_input(kernel_path)) != kernel_input_hash or
            sha256(read_input(tree_path)) != profile["device_tree"]["input_sha256"]):
        raise ValueError("Research input changed during preparation")
    record = {"format": 1, "scope": "disposable-apfs-ram-research",
              "firmware": profile["firmware"], "build": profile["build"], "device": profile["device"],
              "data_layout": data_layout,
              "hardware_profile": hardware_profile,
              "enabled_in_product_guest_jobs": False, "persistent_storage_qualified": False,
              "data_protection_qualified": False, "full_ios_boot_qualified": False,
              "graphical_apps_qualified": False, "root_authentication_changed": False,
              "inputs_preserved": True,
              "inputs": {"kernel": {"path": str(kernel_path), "sha256": kernel_input_hash},
                         "device_tree": {"path": str(tree_path),
                                         "sha256": profile["device_tree"]["input_sha256"]}},
              "outputs": {"kernel": {"path": "bootkc", "sha256": sha256(kernel), "bytes": len(kernel)},
                          "device_tree": {"path": "dtree", "sha256": sha256(tree), "bytes": len(tree)}}}
    output.mkdir(parents=True, exist_ok=False)
    for name, data in (("bootkc", kernel), ("dtree", tree),
                       ("research-inputs.json", (json.dumps(record, indent=2) + "\n").encode())):
        with (output / name).open("xb") as stream:
            stream.write(data)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--device-tree", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-layout", choices=("split-users", "single-data"), default="split-users",
                        help="single-data selects Apple's non-split Data layout for persona boot research")
    parser.add_argument("--hardware-profile", choices=("none", "dcp-dart", "exclave-dart", "exclave-mcc"), default="none",
                        help="Optional DART/ExclaveCore/MCC research; both exclave profiles require separately prepared Exclave firmware")
    args = parser.parse_args()
    ramdisk = json.loads((SOURCES / "ios27-ramdisk-research.json").read_text())
    profile = json.loads((SOURCES / "ios27-apfs-ram-research.json").read_text())
    record = prepare(args.kernel.expanduser(), args.device_tree.expanduser(),
                     args.output.expanduser().absolute(), ramdisk, profile, args.data_layout,
                     args.hardware_profile)
    print(json.dumps({"output": str(args.output), **record}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, struct.error) as error:
        raise SystemExit(str(error))
