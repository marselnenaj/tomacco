#!/usr/bin/env python3
"""Build native C and Embedded Swift iOS CLI examples on Linux, without an SDK.

This does not compile UIKit, SwiftUI, Foundation or an IPA. The small text-based
linker interface resolves actual libSystem symbols from the running iOS guest.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

SOURCE = Path(__file__).resolve().parents[1] / "examples/ios-restore-native"


def run(argv):
    subprocess.run(list(map(str, argv)), check=True)


def digest(file):
    return hashlib.sha256(file.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--clang", default="clang")
    parser.add_argument("--swiftc", default="swiftc")
    parser.add_argument("--linker", required=True, help="Path to an LLVM ld64.lld with iOS support")
    parser.add_argument("--minimum-ios", default="27.0")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]{1,2}\.[0-9]{1,2}(?:\.[0-9]{1,2})?", args.minimum_ios):
        parser.error("Invalid minimum iOS version")
    output = args.output.expanduser().resolve()
    if output.exists():
        parser.error("Output already exists; choose a new directory")
    info = json.loads(subprocess.check_output([args.swiftc, "-print-target-info"], text=True))
    linker = str(Path(args.linker).expanduser().resolve(strict=True))
    output.parent.mkdir(parents=True, exist_ok=True)
    target = "arm64-apple-ios" + args.minimum_ios
    with tempfile.TemporaryDirectory(prefix=".tomacco-ios-native-", dir=output.parent) as temporary:
        stage = Path(temporary)
        programs = stage / "programs"
        programs.mkdir()
        for name in ("hello", "swift_bridge"):
            run([args.clang, "--target=" + target, "-O2", "-c", SOURCE / (name + ".c"), "-o", stage / (name + ".o")])
        run([args.swiftc, "-target", target, "-module-cache-path", stage / "ModuleCache",
             "-enable-experimental-feature", "Embedded", "-wmo", "-O", "-parse-as-library",
             "-emit-object", SOURCE / "hello.swift", "-o", stage / "swift.o"])
        flags = [linker, "-arch", "arm64", "-platform_version", "ios", args.minimum_ios,
                 args.minimum_ios, "-dead_strip", "-L" + str(SOURCE), "-lSystem"]
        run([*flags, "-o", programs / "hello-c", stage / "hello.o"])
        run([*flags, "-o", programs / "hello-swift", stage / "swift.o", stage / "swift_bridge.o"])
        record = {
            "format": 1, "target": target, "scope": "native-command-line-programs",
            "apple_sdk_used": False, "macos_used": False, "graphical_apps_qualified": False,
            "swift": info["compilerVersion"],
            "clang": subprocess.check_output([args.clang, "--version"], text=True).splitlines()[0],
            "linker": subprocess.check_output([linker, "--version"], text=True).strip(),
            "sources": {file.name: digest(file) for file in sorted(SOURCE.iterdir()) if file.suffix in (".c", ".swift", ".tbd")},
            "programs": {file.name: digest(file) for file in sorted(programs.iterdir())},
        }
        (stage / "build-manifest.json").write_text(json.dumps(record, indent=2) + "\n")
        stage.rename(output)
    print(json.dumps({"output": str(output), "payload": str(output / "programs"), **record}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
