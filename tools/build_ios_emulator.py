#!/usr/bin/env python3
"""Build a separate experimental Apple ARM engine on Linux, without root.

No Apple firmware or device images are downloaded or redistributed. The
result is a research engine, not a qualified graphical iOS application runtime.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import venv

REPO = Path(__file__).resolve().parents[1]
PATCHES = {name: REPO / "integrations/ios-emulator" / filename for name, filename in {
    "inferno": "bringup.patch", "sptm": "sptm-boot-display.patch"}.items()}
ARTWORK = "ui/icons/CKQEMUBootSplash_512x512@2x.png"
SOURCES = {
    "engine": ("https://github.com/ChefKissInc/Inferno.git", "cc4302a99167abec69b714cfd00c38caece7e7de"),
    "lzfse": ("https://github.com/lzfse/lzfse.git", "e634ca58b4821d9f3d560cdc6df5dec02ffc93fd"),
}
ENGINES = {
    "inferno": SOURCES["engine"],
    "sptm": ("https://github.com/jprx/qemu-sptm.git", "bad6336fb24211f8c4e8e946c259bd5b12417340"),
}
MESON = "1.10.2"
# Remote block backends no engine here opens (both QEMU sources have these
# options; the SPTM tree has no GlusterFS option). Meson enables each one it finds
# on the build host, and a later distribution upgrade of that library (libnfs
# 16 -> 17) then stops every engine and helper from starting at all.
UNUSED_HOST_LIBRARIES = ("--disable-libnfs", "--disable-libiscsi", "--disable-curl", "--disable-libssh",
                         "--disable-rbd")
HARDWARE_SOURCES = ("dart-t8110.c", "dart-t8110.h", "exclave-bundle.h",
                    "mcc-t8140.c", "mcc-t8140.h", "ptd-t8140.c", "ptd-t8140.h", "pmp-research.c",
                    "asc-mailbox.c", "asc-mailbox.h", "asc-controller.c", "asc-controller.h")


def run(argv, *, cwd=None, env=None, capture=False):
    print("+", " ".join(map(str, argv)), flush=True)
    return subprocess.run(list(map(str, argv)), cwd=cwd, env=env, check=True,
                          stdout=subprocess.PIPE if capture else None, text=capture)


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checkout(name, directory):
    url, revision = SOURCES[name]
    if not directory.exists():
        directory.mkdir(parents=True)
        run(["git", "init", directory])
        run(["git", "-C", directory, "remote", "add", "origin", url])
        run(["git", "-C", directory, "fetch", "--depth", "1", "origin", revision])
        run(["git", "-C", directory, "checkout", "--detach", "FETCH_HEAD"])
    actual = run(["git", "-C", directory, "rev-parse", "HEAD"], capture=True).stdout.strip()
    if actual != revision:
        raise RuntimeError(f"Preserved existing {directory}: expected commit {revision}, found {actual}")
    return revision


def stage_hardware_sources(source, backend, *, dry_run=False):
    """Install audited companion sources without overwriting local engine edits."""
    if backend != "sptm":
        return {}
    pending, record = [], {}
    for name in HARDWARE_SOURCES:
        data = (REPO / "integrations/ios-emulator" / name).read_bytes()
        target = source / "hw/arm" / name
        if target.is_symlink() or (target.exists() and (
                not target.is_file() or target.read_bytes() != data)):
            raise RuntimeError(f"Preserved different emulator hardware source: {target}; use a new --output directory")
        if not target.exists():
            pending.append((target, data))
        record[str(target.relative_to(source))] = hashlib.sha256(data).hexdigest()
    for target, data in ([] if dry_run else pending):
        with target.open("xb") as stream:
            stream.write(data)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=ENGINES, default="sptm")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 2, 16))
    args = parser.parse_args()
    if platform.system() != "Linux":
        parser.error("This recipe targets a Linux host")
    if platform.machine() != "x86_64":
        parser.error("The virtual iOS device needs an x86_64 Linux host. On " + platform.machine() + ", the tools and the agent API work, experimentally, and this helper does not.")
    if not 1 <= args.jobs <= 64:
        parser.error("--jobs must be between 1 and 64")
    for name in ("git", "cmake", "ninja", "pkg-config", "cc", "c++", "make"):
        if not shutil.which(name):
            parser.error(f"Required build tool is missing: {name}")
    packages = ["glib-2.0", "pixman-1", "zlib", "libpng"]
    if args.backend == "inferno":
        packages += ["nettle >= 3.10", "hogweed", "gnutls", "libtasn1", "sdl2", "gtk+-3.0"]
    run(["pkg-config", "--exists", *packages])
    root = (args.output or Path.cwd() / f".tooling/ios-{args.backend}").expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    source, build, deps = root / "source", root / "build", root / "dependencies"
    lzfse, lzfse_build = root / "lzfse", root / "lzfse-build"
    SOURCES["engine"] = ENGINES[args.backend]
    checkout("engine", source)
    excluded = [f":(exclude){ARTWORK}"] if args.backend == "inferno" else []
    current = run(["git", "-C", source, "diff", "--binary", "HEAD", "--", ".", *excluded], capture=True).stdout
    patch = PATCHES[args.backend]
    patch_text = patch.read_text()
    if current and current != patch_text:
        raise RuntimeError("Emulator source contains different local edits; preserved them. Use a new --output directory.")
    if not current:
        run(["git", "-C", source, "apply", "--check", patch])
        run(["git", "-C", source, "apply", patch])
    hardware = stage_hardware_sources(source, args.backend)
    if args.backend == "inferno":
        artwork = source / ARTWORK
        if artwork.is_symlink() or (artwork.exists() and run(
                ["git", "-C", source, "diff", "HEAD", "--", ARTWORK], capture=True).stdout):
            raise RuntimeError("Upstream artwork has local edits; preserved them. Use a new --output directory.")
        artwork.unlink(missing_ok=True)
        run(["git", "-C", source, "submodule", "update", "--init", "--depth", "1", "util/mlib"])
    checkout("lzfse", lzfse)
    if run(["git", "-C", lzfse, "status", "--porcelain"], capture=True).stdout:
        raise RuntimeError("LZFSE source has local edits; preserved them")
    run(["cmake", "-S", lzfse, "-B", lzfse_build, "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
         "-DCMAKE_POSITION_INDEPENDENT_CODE=ON", "-DBUILD_SHARED_LIBS=OFF", f"-DCMAKE_INSTALL_PREFIX={deps}"])
    run(["cmake", "--build", lzfse_build, "--parallel", args.jobs])
    run(["cmake", "--install", lzfse_build])
    python = root / "python/bin/python"
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(root / "python")
    run([python, "-m", "pip", "install", "--disable-pip-version-check", f"meson=={MESON}"])
    env = {**os.environ, "PATH": str(python.parent) + os.pathsep + os.environ.get("PATH", "")}
    build.mkdir(exist_ok=True)
    flags = [f"--prefix={root / 'install'}", "--enable-lzfse", "--disable-guest-agent",
             "--disable-werror", "--disable-debug-info", "--disable-qom-cast-debug",
             f"--extra-ldflags=-L{deps / 'lib'}", *UNUSED_HOST_LIBRARIES]
    if args.backend == "inferno":
        flags += ["--target-list=aarch64-softmmu", "--enable-slirp", "--enable-nettle", "--enable-gnutls", "--enable-sdl", "--enable-gtk",
                  f"--extra-cflags=-I{deps / 'include'} -DTOMACCO_IOS_BOOT_PROBE"]
    else:
        flags += ["--target-list=aarch64-softmmu,x86_64-softmmu", "--disable-pvg", "--disable-rust", "--disable-docs",
                  "--disable-sdl", "--disable-gtk", f"--extra-cflags=-I{deps / 'include'}"]
    run([source / "configure", *flags], cwd=build, env=env)
    targets = ["qemu-system-aarch64", "qemu-img"] + (["qemu-system-x86_64"] if args.backend == "sptm" else [])
    run(["ninja", "-C", build, "-j", args.jobs, *targets], env=env)
    engine = build / "qemu-system-aarch64"
    version = run([engine, "--version"], capture=True).stdout.strip()
    machines = run([engine, "-machine", "help"], capture=True).stdout
    required = ("t8030",) if args.backend == "inferno" else (
        "darwin", "tomacco-pmp-research", "tomacco-asc-mailbox-test", "tomacco-pmp-asc-research")
    for machine in required:
        if not any(line.split()[:1] == [machine] for line in machines.splitlines()):
            raise RuntimeError(f"Built engine does not expose {machine}")
    if args.backend == "sptm":
        traces = run([engine, "-trace", "help"], capture=True).stdout
        if "tomacco_arm_sysreg_fault" not in traces.splitlines():
            raise RuntimeError("Built engine lacks executed system-register fault tracing")
    record = {"backend": args.backend, "engine": str(engine), "engine_sha256": sha256(engine), "version": version,
              "source_commits": {key: rev for key, (_, rev) in SOURCES.items()},
              "patch_sha256": sha256(patch),
              "hardware_sources": hardware,
              "dart_iommu_available": args.backend == "sptm",
              "mcc_registers_available": args.backend == "sptm",
              "ptd_mailbox_available": args.backend == "sptm",
              "isolated_pmp_execution_available": args.backend == "sptm",
              "strict_pmp_system_registers_available": args.backend == "sptm",
              "asc_mailbox_test_available": args.backend == "sptm",
              "experimental_asc_controller_available": args.backend == "sptm",
              "asc_controller_available": False,
              "pmp_power_services_available": False,
              "exclave_core_loader_available": args.backend == "sptm",
              "boot_framebuffer_available": args.backend == "sptm",
              "boot_media_available": args.backend == "sptm",
              "file_backed_boot_media_available": args.backend == "sptm",
              "helper_engine": str(build / "qemu-system-x86_64") if args.backend == "sptm" else None,
              "bios_directory": str(source / "pc-bios"),
              "meson": MESON, "configure": flags, "host": platform.machine(),
              "qualified_for_app_development": False, "macos_vm_used": False}
    (root / "build-manifest.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
