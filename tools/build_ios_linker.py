#!/usr/bin/env python3
"""Build the pinned ARM64e ld64/TAPI port in the workspace, without root.

Uses libdispatch from an explicitly supplied Linux Swift toolchain. No Apple
SDK, firmware, system installation, guest patch or signing service is involved.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess

import build_ios_emulator as base

SOURCES = {
    "cctools": ("https://github.com/tpoechtrager/cctools-port.git", "904de2a71d4da6a9b30d2efaf912a10ddc7d9ddb"),
    "tapi": ("https://github.com/tpoechtrager/apple-libtapi.git", "fa9443738c1a18accef4244732ec6d6ee97a8133"),
}


def stage_file(source, target):
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"Expected a regular Swift runtime file: {source}")
    if target.is_symlink() or (target.exists() and base.sha256(target) != base.sha256(source)):
        raise ValueError(f"Preserved different dependency: {target}; use a new output directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with source.open("rb") as incoming, target.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
    return base.sha256(target)


def build(args):
    if platform.system() != "Linux":
        raise ValueError("This linker recipe requires Linux")
    if not 1 <= args.jobs <= 64:
        raise ValueError("jobs must be between 1 and 64")
    for program in ("git", "cmake", "ninja", "make", "clang", "clang++"):
        if shutil.which(program) is None:
            raise ValueError(f"Missing build tool: {program}")
    root = args.output.expanduser().resolve()
    swift = args.swift_lib.expanduser().resolve(strict=True)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "build.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        prefix = root / "prefix"
        base.SOURCES.update(SOURCES)
        for name in SOURCES:
            source = root / (name + "-source")
            base.checkout(name, source)
            if base.run(["git", "-C", source, "diff", "--name-only", "HEAD"], capture=True).stdout.strip():
                raise ValueError(f"Preserved modified linker source: {source}")
        runtime = {}
        for name in ("libdispatch.so", "libBlocksRuntime.so"):
            runtime[name] = stage_file(swift / "linux" / name, prefix / "lib" / name)
        stage_file(swift / "Block/Block.h", prefix / "include/Block.h")
        for name in ("dispatch", "os"):
            directory = swift / name
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError(f"Missing Swift dispatch headers: {directory}")
            for source in sorted(directory.rglob("*")):
                if source.is_symlink():
                    raise ValueError("Swift dispatch headers must not contain symlinks")
                if source.is_file():
                    stage_file(source, prefix / "include" / name / source.relative_to(directory))
        tapi = root / "tapi-source/build"
        base.run(["cmake", "-G", "Ninja", "-S", root / "tapi-source/src/llvm", "-B", tapi,
                  "-DCMAKE_C_COMPILER=clang", "-DCMAKE_CXX_COMPILER=clang++",
                  "-DLLVM_INCLUDE_TESTS=OFF", "-DCMAKE_BUILD_TYPE=RELEASE", "-DLLVM_ENABLE_PROJECTS=tapi;clang",
                  "-DCMAKE_INSTALL_PREFIX=" + str(prefix), "-DTAPI_REPOSITORY_STRING=1600.0.11.8",
                  "-DTAPI_FULL_VERSION=1600.0.11.8"])
        base.run(["ninja", "-C", tapi, "clangBasic", "vt_gen", "-j", args.jobs])
        base.run(["ninja", "-C", tapi, "libtapi", "install-libtapi", "install-tapi-headers", "-j", args.jobs])
        directory = root / "cctools-build"
        directory.mkdir(exist_ok=True)
        env = {**os.environ,
               "LDFLAGS": "-L" + shlex.quote(str(prefix / "lib")) + " -Wl,-rpath," + shlex.quote(str(prefix / "lib")),
               "CPPFLAGS": "-isystem " + shlex.quote(str(prefix / "include"))}
        base.run([root / "cctools-source/cctools/configure", "--prefix=" + str(prefix),
                  "--target=aarch64-apple-darwin", "--with-libtapi=" + str(prefix),
                  "--with-libdispatch=" + str(prefix), "--disable-lto-support"], cwd=directory, env=env)
        base.run(["make", "-j", args.jobs], cwd=directory, env=env)
        base.run(["make", "install"], cwd=directory, env=env)
        linker = prefix / "bin/aarch64-apple-darwin-ld"
        record = {"format": 1, "scope": "linux-arm64e-linker", "sources": SOURCES,
                  "linker": str(linker), "sha256": base.sha256(linker), "swift_lib": str(swift),
                  "runtime_libraries": runtime, "apple_sdk_used": False}
        (root / "linker.json").write_text(json.dumps(record, indent=2) + "\n")
        return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".tooling/ios-linker"))
    parser.add_argument("--swift-lib", type=Path, required=True, help="Linux Swift toolchain's usr/lib/swift directory")
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 2, 8))
    args = parser.parse_args()
    try:
        record = build(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
