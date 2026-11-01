#!/usr/bin/env python3
"""Unwrap the virtual iOS device's boot components from the person's PCC image.

Step 3 of docs/virtual-ios/setup.md, the table "From / To", as one command:
each component is found through the research identity of the unpacked PCC
23E5207q image (or by its file name in it) and unwrapped with the `ipsw` the
helper support carries, into a new directory. The unwrapped research kernel
must have the digest setup.md names. boot-inputs.json records where each came
from, the digests, and the PCC StaticTrustCache the Cryptex steps take as
gpu_trustcache. An existing output is never replaced; nothing is fetched.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile

from ios_paravirt_firmware import MANIFEST_LIMIT, read_component
from ios_paravirt_kernel import KERNEL_SHA256

IDENTITY = 5
# (output name, the file name in the image, or the identity's component).
COMPONENTS = (("sptm", "sptm.vresearch1.release.im4p", None),
              ("txm", "txm.iphoneos.research.im4p", None),
              ("sep", "sep-firmware.vphone600.RELEASE.im4p", None),
              ("dtree.original", "DeviceTree.vphone600ap.im4p", None),
              ("kernel", "kernelcache.research.vphone600", None),
              ("ramdisk.dmg", None, "RestoreRamDisk"),
              ("ramdisk.tc", None, "RestoreTrustCache"))


def identity_paths(root):
    """{component: relative path} of the research identity."""
    data = read_component(root, "BuildManifest.plist", 16 * MANIFEST_LIMIT)
    identities = plistlib.loads(data).get("BuildIdentities") or []
    if len(identities) <= IDENTITY:
        raise ValueError(f"{root} has no identity {IDENTITY}; is it the unpacked PCC 23E5207q image?")
    paths = {}
    for name, entry in (identities[IDENTITY].get("Manifest") or {}).items():
        path = ((entry or {}).get("Info") or {}).get("Path")
        if isinstance(path, str) and path:
            paths[name] = path
    return paths


def locate(root, paths, filename, component):
    """The source file of one component, relative to root."""
    if component is not None:
        if component not in paths:
            raise ValueError(f"the research identity names no {component}")
        relative = paths[component]
    else:
        named = sorted(path for path in paths.values() if Path(path).name == filename)
        found = named or sorted(str(path.relative_to(root)) for path in root.rglob(filename) if path.is_file())
        if not found:
            raise ValueError(f"{filename} is not in {root}; unpack the whole PCC archive")
        relative = found[0]
    if not (root / relative).is_file():
        raise ValueError(f"{relative} is listed but missing from {root}; unpack the whole PCC archive")
    return relative


def plan(root):
    """[(output name, source relative path)], and the StaticTrustCache's path."""
    paths = identity_paths(root)
    sources = [(name, locate(root, paths, filename, component)) for name, filename, component in COMPONENTS]
    static = paths.get("StaticTrustCache")
    return sources, (str(root / static) if static and (root / static).is_file() else None)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare(root, ipsw, output, run=subprocess.run):
    root, output = root.resolve(strict=True), output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError(f"{output} already exists; boot inputs are never replaced")
    sources, static = plan(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".boot-inputs-", dir=output.parent) as temporary:
        staging = Path(temporary) / "boot"
        staging.mkdir()
        record = {"format": 1, "firmware": str(root), "identity": IDENTITY, "components": {},
                  "gpu_trustcache": static}
        for name, relative in sources:
            target = staging / name
            run([str(ipsw), "img4", "im4p", "extract", str(root / relative), "-o", str(target)],
                check=True, stdout=subprocess.DEVNULL)
            if not target.is_file():
                raise ValueError(f"ipsw wrote no {name} from {relative}")
            record["components"][name] = {"source": relative, "sha256": digest(target)}
        kernel = record["components"]["kernel"]["sha256"]
        if kernel != KERNEL_SHA256:
            raise ValueError(f"the unwrapped research kernel has SHA-256 {kernel}, not {KERNEL_SHA256}; the "
                             "PCC image is not 23E5207q or its kernel was changed")
        (staging / "boot-inputs.json").write_text(json.dumps(record, indent=1) + "\n")
        os.rename(staging, output)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pcc-firmware", type=Path, required=True, help="Unpacked PCC 23E5207q image")
    parser.add_argument("--ipsw", type=Path, required=True, help="The ipsw tool, e.g. .tooling/ios-sptm/support/ipsw/ipsw")
    parser.add_argument("--output", type=Path, required=True, help="New directory for the unwrapped components")
    args = parser.parse_args(argv)
    try:
        record = prepare(args.pcc_firmware, args.ipsw, args.output)
    except (OSError, ValueError, plistlib.InvalidFileException, subprocess.CalledProcessError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        print(f"Boot input preparation failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "output": str(args.output.absolute()), **record}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
