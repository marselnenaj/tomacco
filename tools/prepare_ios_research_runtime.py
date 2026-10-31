#!/usr/bin/env python3
"""Package owned, checksummed full-system research inputs for the Rust runtime.

Does not download firmware or create missing hardware. Original inputs are never
modified. The output remains an explicitly unqualified iOS research environment.
"""
import argparse
import ctypes
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from extract_ios_platform import CLASSES, NODE_PATHS

PARTS = ("kernel", "device_tree", "ramdisk", "trustcache", "sptm", "txm")
NAMES = dict(zip(PARTS, ("bootkc", "dtree", "system.apfs", "bootstrap.tc", "sptm", "txm")))
EXCLAVE_PARTS = {"cl4": ("cl4", "cl4", 256 * 1024**2),
                 "exclave_integrity": ("integrity", "exclave.icat", 16 * 1024**2),
                 "exclave_trustcache": ("trustcache", "exclave.tc", 16 * 1024**2)}


def read_json(file):
    if not file.is_file() or file.stat().st_size > 2 * 1024 * 1024:
        raise ValueError(f"Missing or oversized manifest: {file}")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate manifest key")
            result[key] = value
        return result
    return json.loads(file.read_text(), object_pairs_hook=unique)


def confined(root, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("Invalid component path")
    file = (root / relative).resolve(strict=True)
    if not file.is_relative_to(root) or not file.is_file():
        raise ValueError("Component escapes its input directory")
    return file


def digest(file):
    with file.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def checked_copy(source, destination, expected):
    if not isinstance(expected, str) or len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError("Invalid component checksum")
    # Copy-on-write where supported; never hardlink mutable source images.
    subprocess.run(["cp", "--reflink=auto", "--sparse=always", "--", str(source), str(destination)], check=True)
    destination.chmod(0o400)
    if digest(destination) != expected:
        raise ValueError(f"Component checksum mismatch: {source.name}")
    return {"path": destination.name, "sha256": expected, "bytes": destination.stat().st_size}


def publish(stage, output):
    """Publish the entire bundle atomically, refusing any existing destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(stage), -100, os.fsencode(output), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(output))
    parent = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def prepare(firmware, inputs, bootstrap, image, output, staging=None, exclave=None):
    firmware, inputs, bootstrap = (p.resolve(strict=True) for p in (firmware, inputs, bootstrap))
    image = image.resolve(strict=True)
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; choose a new directory")
    manifests = {"firmware": firmware / "tomacco-ios.json",
                 "research_inputs": inputs / "research-inputs.json",
                 "bootstrap": bootstrap / "manifest.json",
                 "image": bootstrap / "image-manifest.json"}
    manifest_hashes = {name: digest(path) for name, path in manifests.items()}
    original = read_json(manifests["firmware"])
    edits = read_json(manifests["research_inputs"])
    boot = read_json(manifests["bootstrap"])
    assembled = read_json(manifests["image"])
    hardware_profile = edits.get("hardware_profile", "none")
    if hardware_profile not in ("none", "dcp-dart", "exclave-dart", "exclave-mcc"):
        raise ValueError("Unknown research hardware profile")
    if (hardware_profile in ("exclave-dart", "exclave-mcc")) != (exclave is not None):
        raise ValueError("An exclave-dart/exclave-mcc profile and a matching Exclave bundle must be supplied together")
    if (original.get("format") != 1 or original.get("backend") != "sptm" or
            original.get("environment") != "ios-restore-shell" or
            edits.get("format") != 1 or edits.get("scope") != "disposable-apfs-ram-research" or
            (edits.get("firmware"), edits.get("build")) != (original.get("version"), original.get("build")) or
            edits.get("inputs_preserved") is not True or edits.get("root_authentication_changed") is not False or
            boot.get("format") != 1 or boot.get("scope") != "full-system-boot-research" or
            boot.get("data_filesystem") != "apfs" or boot.get("restart_probe") is not False or
            boot.get("platform_diagnostics") is not True or boot.get("launchd_enabled") is not True or
            boot.get("platform_contract", {}).get("classes") != list(CLASSES) or
            boot.get("platform_contract", {}).get("device_tree_paths") != list(NODE_PATHS) or
            assembled.get("source_preserved") is not True or
            Path(assembled.get("target", "")).resolve() != image or
            Path(assembled.get("bootstrap", "")).resolve() != bootstrap / "manifest.json"):
        raise ValueError("Incompatible or incomplete iOS research inputs")
    if not image.is_file() or image.stat().st_size == 0 or image.stat().st_size % 16384:
        raise ValueError("The APFS image must be nonempty and page-aligned")
    if image.stat().st_size > 28 * 1024**3:
        raise ValueError("Research image exceeds the qualified 32 GiB guest layout")
    for field in ("board", "version", "build", "variant"):
        if not isinstance(original.get(field), str) or not original[field]:
            raise ValueError(f"Missing firmware identity: {field}")
    # Bind the combined cache and helpers to the bootstrap that produced the image.
    cache = confined(bootstrap, "bootstrap.tc")
    expected_cache = boot.get("trustcache_sha256")
    if not expected_cache:
        raise ValueError("Bootstrap manifest has no trust-cache checksum")
    sources = {
        "kernel": (confined(inputs, edits["outputs"]["kernel"]["path"]), edits["outputs"]["kernel"]["sha256"]),
        "device_tree": (confined(inputs, edits["outputs"]["device_tree"]["path"]), edits["outputs"]["device_tree"]["sha256"]),
        "ramdisk": (image, assembled["target_sha256"]),
        "trustcache": (cache, expected_cache),
    }
    for name in ("sptm", "txm"):
        entry = original["components"][name]
        sources[name] = (confined(firmware, entry["path"]), entry["sha256"])
    names = dict(NAMES)
    exclave_manifest = None
    if exclave is not None:
        exclave = exclave.resolve(strict=True)
        exclave_manifest = exclave / "exclave-research.json"
        manifests["exclave"] = exclave_manifest
        manifest_hashes["exclave"] = digest(exclave_manifest)
        secure = read_json(exclave_manifest)
        if (secure.get("format") != 1 or secure.get("scope") != "exclavecore-boot-research" or
                secure.get("chip") != "0x8140" or secure.get("manifest_digests_verified") is not True or
                secure.get("inputs_preserved") is not True or secure.get("firmware_modified") is not False or
                secure.get("secure_services_qualified") is not False or secure.get("graphical_apps_qualified") is not False or
                any(secure.get(key) != original[key] for key in ("board", "version", "build", "manifest_sha256")) or
                digest(confined(exclave, "BuildManifest.plist")) != original["manifest_sha256"]):
            raise ValueError("Exclave bundle does not match the prepared firmware identity")
        for name, (key, destination, limit) in EXCLAVE_PARTS.items():
            entry = secure["components"][key]
            source = confined(exclave, entry["path"])
            if not 0 < source.stat().st_size <= limit or source.stat().st_size != entry["bytes"]:
                raise ValueError("Invalid Exclave component size: " + name)
            sources[name] = (source, entry["sha256"])
            names[name] = destination
    output.parent.mkdir(parents=True, exist_ok=True)
    if staging is not None:
        if (staging.is_symlink() or not staging.is_dir() or any(staging.iterdir()) or
                staging.resolve().parent != output.parent.resolve()):
            raise ValueError("Staging must be an empty sibling directory")
    context = nullcontext(staging) if staging is not None else tempfile.TemporaryDirectory(prefix=".tomacco-ios-prepare-", dir=output.parent)
    with context as temporary:
        stage = Path(temporary)
        parts = {}
        checked_copy(firmware / "BuildManifest.plist", stage / "BuildManifest.plist", original["manifest_sha256"])
        for name in sources:
            print(f"Preparing {name}", flush=True)
            source, checksum = sources[name]
            parts[name] = checked_copy(source, stage / names[name], checksum)
        record = {"format": 1, "backend": "sptm", "environment": "ios-system-research",
                  **{key: original[key] for key in ("board", "version", "build", "variant", "manifest_sha256")},
                  "components": parts, "memory_mb": 32768,
                  "hardware_profile": hardware_profile,
                  "diagnostics": {"platform": True, "process": boot.get("process_diagnostics") is True,
                                  "application": boot.get("application_diagnostics") is True,
                                  "graphics": boot.get("graphics_diagnostics") is True},
                  "source_manifests": manifest_hashes,
                  "macos_required": False, "data_protection_qualified": False,
                  "graphical_apps_qualified": False, "full_ios_boot_qualified": False}
        if exclave_manifest is not None:
            record["secure_services_qualified"] = False
        if any(digest(path) != manifest_hashes[name] for name, path in manifests.items()):
            raise ValueError("Source manifest changed during preparation")
        (stage / "tomacco-ios.json").write_text(json.dumps(record, indent=2) + "\n")
        (stage / "tomacco-ios.json").chmod(0o400)
        # A cancelled preparation can leave a staging directory, never a partial bundle.
        publish(stage, output)
    return {"output": str(output), **record}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("firmware", "inputs", "bootstrap", "image", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--staging", type=Path)
    parser.add_argument("--exclave", type=Path, help="Matching directory from prepare_ios_exclave.py; requires exclave-dart or exclave-mcc inputs")
    args = parser.parse_args()
    print(json.dumps(prepare(**vars(args)), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error))
