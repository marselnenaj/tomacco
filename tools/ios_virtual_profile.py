#!/usr/bin/env python3
"""Write the virtual iOS device's profile directory from a user's own artifacts.

Checks every input of `ios.virtual.start` the way the start itself would and,
for each one that is absent or wrong, names the command that makes it, in the
order of the chain (docs/virtual-ios/setup.md, "From a restore image to
the first start"). With everything present it writes profile.json and a
state.json whose verified base is the first guest-tools install; an existing
profile is never overwritten. `--check` only reports. No guest runs and no input
is changed: this neither supplies, fetches nor signs anything of Apple's.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys

from ios_graphics_location import BUNDLE_PATH, COMPILER_LIBRARY_NAME, PLUGIN_PERSONALITY
from ios_guest_tools import payload
from ios_paravirt_firmware import read_component, validate_im4m
from ios_paravirt_kernel import kernel_profile, manifest_name
from ios_virtual_iphone import (bridge_compiler_paths, development_library_cdhash, kernel_graphics_personalities,
                                kernel_pins_cdhash)
from prepare_ios_adapter_pin import INPUT_PROFILE_SHA256 as PINNABLE_PROFILE, KERNEL_NAME
from prepare_ios_fileport_compat import INPUT_PROFILE_SHA256 as PREPARED_PROFILE
from prepare_ios_guest_tools import graphics_payload
from prepare_ios_paravirt import MAX_TREE_BYTES, ap_cpu_count, decode
from prepare_ios_workloop_compat import INPUT_PROFILE_SHA256 as FILEPORT_PROFILE

REPO = Path(__file__).resolve().parents[1]
# The owned diagnostic every development-signing guest-tools build carries
# (virtual_phone.rs RESEARCH_APPLICATION); the start opens it until another is chosen.
RESEARCH_APPLICATION = "io.github.marselnenaj.tomacco.research-app"
# The engine options: changed frames per second during swipes went from 2-6 to
# 10-15 with both. Both need the default engine series.
ENGINE_OPTIONS = {"target_page": "16k", "bti": "off"}
ENGINE_PATCHES = ("qemu-arm-target-page-16k.patch", "qemu-arm-bti-property.patch")
BOOT_ARGS = "debug=0x8 cpus={cpus} rd=disk0s1 serial=3 -v -noprogress keepsyms=1 wdt=-1 ignition_level=4 launchd_log_serial=1"

MAKE_ENGINE = "python3 tools/build_ios_paravirt.py --jobs 8 --cpu-optimization native"
MAKE_BACKEND = "python3 tools/build_ios_guest_control.py --output .tooling/ios-guest-control"
MAKE_HELPER = "python3 tools/build_ios_emulator.py --backend sptm"
MAKE_SUPPORT = "python3 tools/prepare_ios_emulator.py --firmware IPHONE_IPSW_DIRECTORY --output NEW_DIRECTORY"
MAKE_BOOT_INPUTS = (".tooling/ios-sptm/support/ipsw/ipsw img4 im4p extract COMPONENT.im4p -o BOOT_INPUTS/NAME "
                    "(sptm, txm, sep from your PCC firmware; ramdisk.dmg is the identity's RestoreRamDisk)")
MAKE_CRYPTEX = ("tomaccoctl call ios.emulator.prepare_cryptexes with disk, userland, kernel, boot_manifest, device_tree, "
                "gpu_trustcache, installer_control:true and development_tools")
MAKE_MANIFEST = ("no tool here makes it: prepare_cryptexes copies the IM4M you supply, which must declare your image's "
                 "OS digest and the tree's identity (docs/virtual-ios/setup.md, \"What no tool does for you\")")
MAKE_FILEPORT = "python3 tools/prepare_ios_fileport_compat.py --kernel CRYPTEX/kernelcache.research.vphone600 --output NEW_DIRECTORY"
MAKE_WORKLOOP = "python3 tools/prepare_ios_workloop_compat.py --kernel FILEPORT_OUTPUT/kernelcache.research.vphone600 --output NEW_DIRECTORY"
MAKE_PIN = "python3 tools/prepare_ios_adapter_pin.py --kernel KERNEL --tools TOOLS --output NEW_DIRECTORY"
MAKE_TOOLS = ("tomaccoctl call ios.emulator.build_guest_tools with trustcache (CRYPTEX/system.tc), linker, ldid, clang, "
              "agent, application_probe, development_signing and quiet_services true")
MAKE_INSTALL = ("tomaccoctl call ios.emulator.prepare_guest_tools with disk (CRYPTEX/system.raw), auxiliary_disk and "
                "sep_storage (the restored guest's), tools, trustcache, device_tree, graphics, support, helper_engine, "
                "helper_kernel, bios, development_device:true and output")


def problem_of(check):
    """Run one input's check: its problem as text, or None."""
    try:
        return check()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        return str(error)


def file_problem(path):
    return None if path.is_file() else "not a file"


def executable_problem(path):
    if not path.is_file():
        return "not a file"
    return None if os.access(path, os.X_OK) else "not executable"


def directory_problem(path, *names):
    if not path.is_dir():
        return "not a directory"
    absent = [name for name in names if not (path / name).is_file()]
    return "lacks " + ", ".join(absent) if absent else None


def engine_problem(engine):
    """A missing file, or a build whose manifest lacks the options' patches."""
    problem = executable_problem(engine)
    if problem:
        return problem
    # <output>/build-manifest.json beside sptm-build/, or beside a copied engine.
    for manifest in (engine.parent / "build-manifest.json", engine.parent.parent / "build-manifest.json"):
        if manifest.is_file():
            record = json.loads(manifest.read_text())
            if record.get("engine_sha256") == hashlib.sha256(engine.read_bytes()).hexdigest():
                absent = [name for name in ENGINE_PATCHES if name not in record.get("patches", {})]
                return f"built without {', '.join(absent)}, which target_page=16k and bti=off need" if absent else None
    return None


def tree_problem(tree):
    if not tree.is_file():
        return "not a file"
    data = read_component(tree.parent, tree.name, MAX_TREE_BYTES)
    manifest_name(data)
    ap_cpu_count(decode(data))
    return None


def manifest_problem(path):
    if not path.is_file():
        return "not a file"
    validate_im4m(read_component(path.parent, path.name, 9216))
    return None


def graphics_problem(graphics):
    """The payload's inventory and where its bridge opens the compiler, as the start refuses them."""
    if not graphics.is_dir():
        return "not a directory"
    graphics_payload(graphics)
    expected = BUNDLE_PATH + "/" + COMPILER_LIBRARY_NAME
    opened = bridge_compiler_paths(graphics)
    if opened and expected not in opened:
        return f"its bridge opens the compiler at {', '.join(sorted(opened))}, not {expected}"
    return None


def tools_problem(tools, trustcache):
    """A completed development-signing build of exactly this trust cache."""
    if not tools.is_dir():
        return "not a directory"
    if not trustcache.is_file():
        return f"its trust cache {trustcache} is absent"
    payload(tools, read_component(trustcache.parent, trustcache.name, 4 << 20))
    if development_library_cdhash(tools) is None:
        return "built without development_signing; the start cannot install apps with it"
    return None


def kernel_problem(kernel, tools):
    """The last step of the kernel chain must have run, and pin these tools' adapter."""
    if not kernel.is_file():
        return "not a file"
    if kernel.name != KERNEL_NAME:
        return f"the kernel must be named {KERNEL_NAME}"
    profile = kernel_profile(read_component(kernel.parent, kernel.name, 256 << 20))
    if profile == PREPARED_PROFILE:
        return "this is the prepared kernel; it still needs the fileport and workloop steps"
    if profile == FILEPORT_PROFILE:
        return "this kernel still needs the workloop step"
    if profile != PINNABLE_PROFILE:
        return ("not derived from a prepared original 23E5207q kernel through the fileport and workloop "
                "steps; prepare_ios_adapter_pin.py and a start's own app rebuild would refuse it")
    named = kernel_graphics_personalities(kernel)
    if named and PLUGIN_PERSONALITY not in named:
        return f"it looks for the Metal driver at {', '.join(sorted(named))}, not {PLUGIN_PERSONALITY}"
    adapter = development_library_cdhash(tools) if tools.is_dir() else None
    if adapter is not None and kernel_pins_cdhash(kernel, adapter) is False:
        return f"it does not pin the development-signing adapter {adapter} of {tools}"
    return None


def install_problem(install, tools, trustcache):
    """A guest-tools install of these tools, which a later start replaces as its previous payload."""
    if not install.is_dir():
        return "not a directory"
    root = "system.qcow2" if (install / "system.qcow2").is_file() else "system.raw"
    absent = [name for name in (root, "auxiliary.raw", "sep.raw", "guest-tools-install.json")
              if not (install / name).is_file()]
    if absent:
        return "lacks " + ", ".join(absent)
    record = json.loads((install / "guest-tools-install.json").read_text())
    if record.get("state") != "installed":
        return f"its guest-tools install is {record.get('state')}, not installed"
    if tools.is_dir() and trustcache.is_file():
        build = payload(tools, read_component(trustcache.parent, trustcache.name, 4 << 20))[0]
        if record.get("build", {}).get("files") != build["files"]:
            return f"it holds another guest-tools build than {tools}"
    return None


def inputs(args):
    """Every input in chain order: (name, flag, path or None, check, command that makes it)."""
    tooling = REPO / ".tooling"
    cryptex = args.cryptex
    from_cryptex = lambda given, name: given or (cryptex / name if cryptex else None)
    boot = lambda given, name: given or (args.boot_inputs / name if args.boot_inputs else None)
    support = args.support or tooling / "ios-sptm/support"
    kernel_image = support / "apfs/vmlinuz"
    helper_kernel = args.helper_kernel or (
        kernel_image if kernel_image.is_file() else Path(f"/usr/lib/modules/{os.uname().release}/vmlinuz"))
    trustcache = from_cryptex(args.trustcache, "system.tc")
    tools, kernel, install = args.tools, args.kernel, args.install
    manifest = args.boot_manifest
    if manifest is None and cryptex and (cryptex / "cryptex.json").is_file():
        record = json.loads((cryptex / "cryptex.json").read_text())
        named = record.get("boot_inputs", {}).get("boot_manifest")
        manifest = Path(named) if named else None
    return (
        ("engine", "--engine", args.engine or tooling / "ios-paravirt/sptm-build/qemu-system-aarch64",
         engine_problem, MAKE_ENGINE),
        ("guest control backend", "--guest-control-backend",
         args.guest_control_backend or tooling / "ios-guest-control/vhost-device-vsock", executable_problem, MAKE_BACKEND),
        ("helper engine", "--helper-engine", args.helper_engine or tooling / "ios-sptm/build/qemu-system-x86_64",
         executable_problem, MAKE_HELPER),
        ("helper firmware", "--bios", args.bios or tooling / "ios-sptm/source/pc-bios", directory_problem, MAKE_HELPER),
        ("helper support", "--support", support,
         lambda path: directory_problem(path, "busybox/busybox", "apfs/apfs.ko"), MAKE_SUPPORT),
        ("helper kernel", "--helper-kernel", helper_kernel, file_problem, MAKE_SUPPORT + " (or a kernel matching apfs.ko)"),
        ("SPTM", "--boot-inputs", boot(args.sptm, "sptm"), file_problem, MAKE_BOOT_INPUTS),
        ("TXM", "--boot-inputs", boot(args.txm, "txm"), file_problem, MAKE_BOOT_INPUTS),
        ("SEP firmware", "--boot-inputs", boot(args.sep, "sep"), file_problem, MAKE_BOOT_INPUTS),
        ("RAM disk", "--boot-inputs", boot(args.ramdisk, "ramdisk.dmg"), file_problem, MAKE_BOOT_INPUTS),
        ("system trust cache", "--trustcache", trustcache, file_problem, MAKE_CRYPTEX),
        ("device tree", "--device-tree", from_cryptex(args.device_tree, "device-tree"), tree_problem, MAKE_CRYPTEX),
        ("system root hash", "--system-volume-root-hash", from_cryptex(args.system_volume_root_hash, "system.root_hash"),
         file_problem, MAKE_CRYPTEX),
        ("boot manifest", "--boot-manifest", manifest, manifest_problem, MAKE_MANIFEST),
        ("graphics payload", "--graphics", from_cryptex(args.graphics, "graphics/payload"), graphics_problem, MAKE_CRYPTEX),
        ("guest tools", "--tools", tools,
         lambda path: tools_problem(path, trustcache) if trustcache else "needs --cryptex or --trustcache", MAKE_TOOLS),
        ("kernel", "--kernel", kernel, lambda path: kernel_problem(path, tools or Path("/nonexistent")),
         f"{MAKE_FILEPORT}; {MAKE_WORKLOOP}; only if the tools changed afterwards: {MAKE_PIN}"),
        ("first guest-tools install", "--install", install,
         lambda path: install_problem(path, tools or Path("/nonexistent"), trustcache or Path("/nonexistent")), MAKE_INSTALL),
    )


def check_inputs(args):
    """(paths by input name, the rows of everything missing or wrong, in chain order)."""
    paths, missing = {}, []
    for name, flag, path, check, make in inputs(args):
        problem = "not given" if path is None else problem_of(lambda: check(path))
        if problem:
            missing.append({"input": name, "flag": flag, "path": str(path) if path else None,
                            "problem": problem, "make": make})
        paths[name] = path
    return paths, missing


def documents(args, paths):
    """(profile.json, state.json): the static inputs and a closed run 0 on the first install."""
    tree = paths["device tree"]
    cpus = ap_cpu_count(decode(read_component(tree.parent, tree.name, MAX_TREE_BYTES)))
    profile = {
        "format": 1,
        "prepare": {
            "trustcache": str(paths["system trust cache"]), "device_tree": str(tree),
            "support": str(paths["helper support"]), "helper_engine": str(paths["helper engine"]),
            "helper_kernel": str(paths["helper kernel"]), "bios": str(paths["helper firmware"]),
            "seconds": 900, "accel": "kvm" if os.access("/dev/kvm", os.R_OK | os.W_OK) else "tcg",
            "development_device": True},
        "boot": {
            "seconds": 86400, "sep_boot_role": "normal", "sep_measure_manifest": True,
            "boot_args": BOOT_ARGS.format(cpus=cpus), "boot_manifest": str(paths["boot manifest"]),
            "system_volume_root_hash": str(paths["system root hash"]), "kernel": str(paths["kernel"]),
            "sptm": str(paths["SPTM"]), "txm": str(paths["TXM"]), "ramdisk": str(paths["RAM disk"]),
            "sep": str(paths["SEP firmware"]), "sep_manifest": str(paths["boot manifest"]), "usb": False,
            "display_profile": "1170x2532@60:65x141", "interactive": True, "input_transport": "usb-hid",
            "clock_mode": "wall", "guest_control_backend": str(paths["guest control backend"]),
            "translation_cache_mib": 2048, "host_cpu_policy": "performance", **ENGINE_OPTIONS},
        "graphics": str(paths["graphics payload"]), "engine": str(paths["engine"]),
        "application": RESEARCH_APPLICATION, "installs": str(args.directory / "installs"),
        "tools": str(paths["guest tools"]), "launch_watchdog_scale": 8, "applications": []}
    if args.clang:
        profile["toolchain"] = {"clang": str(args.clang)}
    state = {"run": 0, "closed": True, "install": str(paths["first guest-tools install"]),
             "tools": str(paths["guest tools"]), "trustcache": str(paths["system trust cache"]),
             "graphics": str(paths["graphics payload"]), "guest_job": None, "phase": "configured", "error": None}
    return profile, state


def write_new(path, value):
    """Create a private file; one that exists already is never replaced."""
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                           stat.S_IRUSR | stat.S_IWUSR), "w") as stream:
        stream.write(json.dumps(value, indent=1) + "\n")


def write_profile(args, paths):
    directory = args.directory
    for name in ("profile.json", "state.json"):
        if (directory / name).exists() or (directory / name).is_symlink():
            raise ValueError(f"{directory / name} already exists; a profile is never overwritten")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "installs").mkdir(exist_ok=True)
    profile, state = documents(args, paths)
    write_new(directory / "state.json", state)
    write_new(directory / "profile.json", profile)
    return profile


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    path = lambda value: Path(value).expanduser().absolute()
    parser.add_argument("--directory", type=path, help="Profile directory to create (required without --check)")
    parser.add_argument("--check", action="store_true", help="Only report what is missing; write nothing")
    parser.add_argument("--cryptex", type=path, help="ios.emulator.prepare_cryptexes output: trust cache, device "
                                                     "tree, root hash, boot manifest and graphics payload")
    parser.add_argument("--boot-inputs", type=path, help="Directory of the unwrapped sptm, txm, sep and ramdisk.dmg")
    parser.add_argument("--kernel", type=path, help="prepare_ios_workloop_compat.py's kernelcache, or one derived from it")
    parser.add_argument("--tools", type=path, help="ios.emulator.build_guest_tools output")
    parser.add_argument("--install", type=path, help="ios.emulator.prepare_guest_tools output: the first start's base")
    for name, what in (("engine", "default .tooling/ios-paravirt/sptm-build/qemu-system-aarch64"),
                       ("guest-control-backend", "default .tooling/ios-guest-control/vhost-device-vsock"),
                       ("helper-engine", "default .tooling/ios-sptm/build/qemu-system-x86_64"),
                       ("bios", "default .tooling/ios-sptm/source/pc-bios"),
                       ("support", "default .tooling/ios-sptm/support"),
                       ("helper-kernel", "default the support's apfs/vmlinuz, else the running kernel's"),
                       ("sptm", "default in --boot-inputs"), ("txm", "default in --boot-inputs"),
                       ("sep", "default in --boot-inputs"), ("ramdisk", "default in --boot-inputs"),
                       ("trustcache", "default in --cryptex"),
                       ("device-tree", "default in --cryptex; prepare_ios_cpu_topology.py makes one with more cores"),
                       ("system-volume-root-hash", "default in --cryptex"),
                       ("boot-manifest", "default the one --cryptex's receipt names"),
                       ("graphics", "default in --cryptex")):
        parser.add_argument("--" + name, type=path, help=what)
    parser.add_argument("--clang", type=path, help="clang 22 a start rebuilds guest tools with; default found on PATH")
    args = parser.parse_args()
    if platform.machine() != "x86_64":
        parser.error("The virtual iOS device needs an x86_64 Linux host. On " + platform.machine() + ", the tools and the agent API work, experimentally, and this helper does not.")
    if not args.check and args.directory is None:
        parser.error("--directory is required without --check")
    paths, missing = check_inputs(args)
    if missing:
        for row in missing:
            print(f"{row['input']} ({row['flag']}"
                  + (f" {row['path']}" if row["path"] else "") + f"): {row['problem']}\n  make it: {row['make']}", file=sys.stderr)
        print(json.dumps({"ok": False, "missing": missing}))
        return 1
    if args.check:
        print(json.dumps({"ok": True, "missing": []}))
        return 0
    try:
        profile = write_profile(args, paths)
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    print(json.dumps({"ok": True, "directory": str(args.directory),
                      "engine_options": {key: profile["boot"][key] for key in ENGINE_OPTIONS},
                      "next": f"tomaccoctl call ios.virtual.configure '{{\"directory\":\"{args.directory}\"}}'"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
