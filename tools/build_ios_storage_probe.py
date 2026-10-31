#!/usr/bin/env python3
"""Build a read-only iOS guest APFS/IOMedia probe on Linux, without an Apple SDK."""
import argparse
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile

import build_ios_emulator as build


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--clang", default="clang")
    parser.add_argument("--linker", type=Path, required=True)
    parser.add_argument("--ldid", type=Path, required=True)
    args = parser.parse_args()
    source = build.REPO / "integrations/ios-emulator"
    output = args.output.expanduser().resolve()
    if output.exists():
        parser.error("Output already exists; choose a new directory")
    linker, ldid = [p.expanduser().resolve(strict=True) for p in (args.linker, args.ldid)]
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".tomacco-ios-storage-", dir=output.parent) as name:
        stage = Path(name)
        programs = stage / "programs"
        programs.mkdir()
        program = programs / "storage-probe"
        build.run([args.clang, "--target=arm64-apple-ios27.0", "-O2", "-Wall", "-Wextra", "-Werror",
                   "-c", source / "storage-probe.c", "-o", stage / "storage.o"])
        build.run([linker, "-arch", "arm64", "-platform_version", "ios", "27.0", "27.0",
                   "-o", program, stage / "storage.o", source / "storage-libSystem.tbd"])
        entitlement = source / "storage-entitlements.plist"
        build.run([ldid, "-Cadhoc", "-S" + str(entitlement), program])
        actual = plistlib.loads(build.run([ldid, "-e", program], capture=True).stdout.encode())
        if actual != plistlib.loads(entitlement.read_bytes()):
            raise RuntimeError("Storage probe entitlements did not survive signing")
        record = {"format": 1, "scope": "read-only-guest-storage-diagnostic",
                  "apple_sdk_used": False, "macos_used": False,
                  "graphical_apps_qualified": False, "entitlements": actual,
                  "sources": {name: build.sha256(source / name) for name in
                              ("storage-probe.c", "storage-libSystem.tbd", "storage-entitlements.plist")},
                  "program_sha256": build.sha256(program),
                  "clang": subprocess.check_output([args.clang, "--version"], text=True).splitlines()[0],
                  "linker": subprocess.check_output([str(linker), "--version"], text=True).strip()}
        (stage / "build-manifest.json").write_text(json.dumps(record, indent=2) + "\n")
        stage.rename(output)
    print(json.dumps({"output": str(output), **record}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
