#!/usr/bin/env python3
"""Build the bounded, SDK-free bootstrap for a copied full-system research image.

This does not create a working graphical iPhone emulator. Seed data must come
from the user's matching firmware. Apple files are embedded only in the local
build output and must not be included in Tomacco distributions.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import struct
import subprocess
import tempfile
import uuid
from extract_ios_platform import CLASSES, NODE_PATHS, MAX_LOG, CLASS_LIMIT, COMPAT_BYTES

REPO = Path(__file__).resolve().parents[1]
SOURCES = REPO / "integrations/ios-emulator"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def literal(value):
    return '"' + ''.join(chr(byte) if 32 <= byte < 127 and byte not in (34, 92)
                         else f"\\{byte:03o}" for byte in value.encode()) + '"'


def seed_header(manifest_file):
    manifest = json.loads(manifest_file.read_text())
    entries = manifest["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 1024:
        raise ValueError("Seed must contain 1–1024 entries")
    known, directories, arrays, initializers, total = set(), set(), [], [], 0
    for index, entry in enumerate(sorted(entries, key=lambda row: (len(PurePosixPath(row["path"]).parts), row["path"]))):
        path = PurePosixPath(entry["path"])
        if (not path.is_relative_to("/private/var") or ".." in path.parts or
                str(path) != entry["path"] or str(path) in known or
                any(ord(char) < 32 or ord(char) == 127 for char in str(path))):
            raise ValueError("Invalid or duplicate data template path")
        if str(path) != "/private/var" and str(path.parent) not in directories:
            raise ValueError("Missing parent directory in data template")
        mode, uid, gid = (entry[key] for key in ("mode", "uid", "gid"))
        if any(type(value) is not int or value < 0 for value in (mode, uid, gid)) or mode > 0o7777 or max(uid, gid) > 65535:
            raise ValueError("Invalid data template permissions")
        kind, payload, size = entry["kind"], "0", 0
        if kind == "file":
            relative = Path(entry["file"])
            source = (manifest_file.parent / relative).resolve(strict=True)
            if relative.is_absolute() or ".." in relative.parts or not source.is_relative_to(manifest_file.parent):
                raise ValueError("Seed file escapes its directory")
            if not source.is_file() or source.stat().st_size > 8 * 1024 * 1024 - total:
                raise ValueError("Seed files exceed 8 MiB")
            data = source.read_bytes()
            total += len(data)
            if sha256(data) != entry["sha256"] or len(data) != entry["size"]:
                raise ValueError("Seed file checksum or size mismatch")
            payload, size = f"tomacco_seed_{index}", len(data)
            arrays.append(f"static const unsigned char {payload}[] = {{" +
                          ",".join(map(str, data or b"\0")) + "};")
        elif kind == "symlink":
            target = entry["target"]
            if not target.startswith("/") or ".." in PurePosixPath(target).parts or "\0" in target:
                raise ValueError("Invalid template symlink")
            payload = "(const unsigned char *)" + literal(target)
        elif kind != "directory":
            raise ValueError("Unsupported seed entry")
        if kind == "directory":
            directories.add(str(path))
        known.add(str(path))
        initializers.append("{" + f"{literal(str(path))},{dict(directory=1,file=2,symlink=3)[kind]},0{mode:o},{uid},{gid},{payload},{size}" + "}")
    header = "\n".join(arrays) + "\nstatic const struct tomacco_seed tomacco_seeds[] = {\n" + ",\n".join(initializers) + "\n};\n"
    return header, {"entries": len(entries), "file_bytes": total,
                    "manifest_sha256": sha256(manifest_file.read_bytes())}


def diagnostic_jobs(source):
    """Keep Apple's job definitions; move diagnostics to writable guest paths.

    No Apple plist is shipped with Tomacco. Inputs must be extracted from
    the user's matching research image. These jobs do not establish boot order.
    """
    jobs, records = {}, {}
    definitions = (
        ("com.apple.SpringBoard", "/System/Library/CoreServices/SpringBoard.app/SpringBoard",
         "mobile", "/private/var/mobile/tmp/tomacco-SpringBoard.log"),
        ("com.apple.backboardd", "/usr/libexec/backboardd",
         "mobile", "/private/var/mobile/tmp/tomacco-backboardd.log"),
        ("com.apple.mobile.usermanagerd", "/usr/libexec/usermanagerd",
         "root", "/private/var/logs/tomacco-usermanagerd.log"))
    for label, program, owner, log in definitions:
        path = source / (label + ".plist")
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ValueError("Missing or oversized research launch job: " + label)
        original = path.read_bytes()
        job = plistlib.loads(original)
        if not isinstance(job, dict):
            raise ValueError("Research launch job must be a dictionary: " + label)
        arguments = job.get("ProgramArguments")
        if (job.get("Label") != label or not isinstance(arguments, list) or not arguments or
                arguments[0] != program or job.get("Program", program) != program or
                job.get("UserName", "root") != owner):
            raise ValueError("Unexpected research launch job identity: " + label)
        environment = job.setdefault("EnvironmentVariables", {})
        if not isinstance(environment, dict):
            raise ValueError("Invalid launch job environment: " + label)
        environment.update(OS_ACTIVITY_DT_MODE="YES", NSUnbufferedIO="YES")
        job.update(StandardOutPath=log, StandardErrorPath=log)
        patched = plistlib.dumps(job)
        jobs[path.name] = patched
        records[path.name] = {"input_sha256": sha256(original), "output_sha256": sha256(patched),
                              "log": log, "user": owner}
    persona = {"Label": "io.github.marselnenaj.tomacco.persona-init",
               "ProgramArguments": ["/usr/libexec/usermanagerd", "--init"],
               "RunAtLoad": True, "LaunchOnlyOnce": True, "ProcessType": "Interactive",
               "UserName": "root", "StandardOutPath": "/dev/console", "StandardErrorPath": "/dev/console",
               "EnvironmentVariables": {"OS_ACTIVITY_DT_MODE": "YES", "NSUnbufferedIO": "YES"}}
    name = persona["Label"] + ".plist"
    jobs[name] = plistlib.dumps(persona)
    records[name] = {"output_sha256": sha256(jobs[name]), "research_added_job": True}
    return jobs, records


def sign_program(ldid, program, entitlements, *, bundle=None, shallow=False, identifier=None):
    # Signing inputs must live outside the bundle. A sidecar next to the
    # executable becomes a sealed resource; deleting it afterwards invalidates
    # an otherwise correctly signed application (native MIS error 0xe8008017).
    # shallow: ldid's directory pass leaves every other Mach-O file of the
    # bundle as it is (-w) and only hashes it as a resource. Without it, ldid
    # re-signs embedded frameworks and libraries with the app's identifier
    # and entitlements; with it, code signed before the bundle keeps its own
    # signature and the bundle's manifest seals exactly those bytes. A bundle
    # without such files signs to the same bytes either way.
    # identifier: the signing identifier of a program outside a bundle (ldid
    # otherwise takes its file name); a bundle's is its CFBundleIdentifier.
    with tempfile.TemporaryDirectory(prefix=".tomacco-sign-", dir=bundle.parent if bundle else program.parent) as temporary:
        flag = "-S"
        if entitlements:
            path = Path(temporary) / "entitlements.plist"
            path.write_bytes(plistlib.dumps(entitlements))
            flag += str(path)
        seals = []
        identity = []
        if identifier is not None and not bundle:
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,254}", identifier):
                raise ValueError("Native program requires a valid signing identifier")
            identity = ["-I" + identifier]
        if bundle:
            identifier = plistlib.loads((bundle / "Info.plist").read_bytes()).get("CFBundleIdentifier")
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,254}", identifier):
                raise ValueError("Native bundle requires a valid signing identifier")
            identity = ["-I" + identifier]
            # ldid's directory path builds CodeResources and the Info.plist seal,
            # but drops -C flags when recursing. Re-sign the executable with the
            # exact external special slots so CS_ADHOC and both seals coexist.
            subprocess.run([ldid, flag, *(["-w"] if shallow else []), str(bundle)], check=True)
            seals = ["-E1:" + str(bundle / "Info.plist"),
                     "-E3:" + str(bundle / "_CodeSignature/CodeResources")]
        subprocess.run([ldid, "-Cadhoc", "-Hsha256", flag, *identity, *seals, str(program)], check=True)
    signed = subprocess.check_output([ldid, "-e", str(program)])
    actual = plistlib.loads(signed) if signed.strip() else {}
    if actual != entitlements:
        raise ValueError("Research program entitlement verification failed")
    signing = subprocess.check_output([ldid, "-h", str(program)], text=True)
    match = re.search(r"^CDHash=([0-9a-fA-F]{40})$", signing, re.MULTILINE)
    if not match:
        raise ValueError("Signer did not return a SHA-256 CDHash")
    return bytes.fromhex(match[1])


def verify_application_resources(info_plist, code_resources):
    """Verify the resource inventory of our three-file diagnostic application.

    Checking the CodeResources special-slot hash alone only proves that the
    manifest was signed, not that its referenced resources are present. This
    probe has no assets: its metadata is the sole v1 resource and is separately
    sealed in CodeDirectory slot 1; the v2 resource inventory must be empty.
    This is deliberately not a validator for arbitrary third-party bundles.
    """
    resources = plistlib.loads(code_resources)
    if (not isinstance(resources, dict) or
            resources.get("files") != {"Info.plist": hashlib.sha1(info_plist).digest()} or
            resources.get("files2") != {}):
        raise ValueError("Native diagnostic bundle resource inventory differs from its signed manifest")


def application_info(minimum_ios):
    """Own research bundle, using UIKit's scene-based lifecycle declaration."""
    return {"CFBundleIdentifier": "io.github.marselnenaj.tomacco.research-app",
            "CFBundleExecutable": "TomaccoProbe", "CFBundleName": "Tomacco Probe",
            "CFBundlePackageType": "APPL", "CFBundleVersion": "1",
            "CFBundleShortVersionString": "1.0", "CFBundleSupportedPlatforms": ["iPhoneOS"],
            "MinimumOSVersion": minimum_ios, "LSRequiresIPhoneOS": True, "UIDeviceFamily": [1],
            "UISupportedInterfaceOrientations": ["UIInterfaceOrientationPortrait"],
            "UIApplicationSceneManifest": {
                "UIApplicationSupportsMultipleScenes": False,
                "UISceneConfigurations": {"UIWindowSceneSessionRoleApplication": [{
                    "UISceneConfigurationName": "Default Configuration",
                    "UISceneClassName": "UIWindowScene",
                    "UISceneDelegateClassName": "TomaccoResearchSceneDelegate"}]}}}


def application_job():
    """Let Apple's launchd supply the mobile bootstrap namespace and identity."""
    return {"Label": "io.github.marselnenaj.tomacco.research-app",
            "ProgramArguments": ["/tomacco/TomaccoProbe.app/TomaccoProbe"],
            "UserName": "mobile", "RunAtLoad": True, "LaunchOnlyOnce": True,
            "POSIXSpawnType": "App", "StandardOutPath": "/dev/console",
            "StandardErrorPath": "/dev/console",
            "EnvironmentVariables": {"HOME": "/private/var/mobile", "USER": "mobile", "LOGNAME": "mobile",
                                     "TMPDIR": "/private/var/mobile/tmp", "OS_ACTIVITY_DT_MODE": "YES",
                                     "NSUnbufferedIO": "YES"}}


def platform_job():
    return {"Label": "io.github.marselnenaj.tomacco.platform-probe",
            "ProgramArguments": ["/tomacco/platform-probe"], "UserName": "root",
            "RunAtLoad": True, "LaunchOnlyOnce": True, "POSIXSpawnType": "Background",
            "StandardOutPath": "/dev/console", "StandardErrorPath": "/dev/console"}


def platform_header():
    return (f"#define TOMACCO_PLATFORM_MAX_BYTES {MAX_LOG}u\n"
            f"#define TOMACCO_PLATFORM_CLASS_LIMIT {CLASS_LIMIT}u\n"
            f"#define TOMACCO_PLATFORM_COMPAT_BYTES {COMPAT_BYTES}\n" +
            "static const char *platform_classes[] = {" + ",".join(map(json.dumps, CLASSES)) + "};\n" +
            "static const char *platform_nodes[] = {" + ",".join(map(json.dumps, NODE_PATHS)) + "};\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-manifest", type=Path, required=True)
    parser.add_argument("--trustcache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--linker", type=Path, required=True)
    parser.add_argument("--ldid", type=Path, required=True)
    parser.add_argument("--clang", default="clang")
    parser.add_argument("--minimum-ios", default="27.0")
    parser.add_argument("--data-filesystem", choices=("tmpfs", "apfs"), default="tmpfs",
                        help="Research only: create a disposable Data volume in the RAM-backed APFS container")
    parser.add_argument("--launch-daemons", type=Path,
                        help="Optional directory containing the three matching Apple plists for research diagnostics")
    parser.add_argument("--restart-probe", action="store_true",
                        help="APFS storage check only: preserve/read a marker, unmount Data and stop before launchd")
    parser.add_argument("--process-diagnostics", action="store_true",
                        help="Opt-in bounded guest stackshots of the kernel, launchd, SpringBoard, backboardd, usermanagerd and runningboardd, plus the nonblocking Exclave boot stage")
    parser.add_argument("--graphics-diagnostics", action="store_true",
                        help="Opt-in real CoreGraphics/CALayer CPU render on the third serial port; no main display")
    parser.add_argument("--graphics-profile", choices=("layers", "uikit"), default="layers",
                        help="With --graphics-diagnostics: optionally attempt real offscreen UIKit views and control actions")
    parser.add_argument("--application-diagnostics", action="store_true",
                        help="Opt-in bounded UIApplicationMain/scene lifecycle attempt from an own .app bundle")
    parser.add_argument("--platform-diagnostics", action="store_true",
                        help="Opt-in read-only IOKit service/device-tree observations on the fourth serial port")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]{1,2}\.[0-9]{1,2}(?:\.[0-9]{1,2})?", args.minimum_ios):
        parser.error("Invalid minimum iOS version")
    if args.restart_probe and args.data_filesystem != "apfs":
        parser.error("--restart-probe requires --data-filesystem apfs")
    if args.restart_probe and args.process_diagnostics:
        parser.error("--process-diagnostics requires a launchd boot, not --restart-probe")
    if args.restart_probe and args.graphics_diagnostics:
        parser.error("--graphics-diagnostics requires a launchd boot, not --restart-probe")
    if args.graphics_profile != "layers" and not args.graphics_diagnostics:
        parser.error("--graphics-profile requires --graphics-diagnostics")
    if args.application_diagnostics and (args.restart_probe or args.graphics_diagnostics):
        parser.error("--application-diagnostics requires a launchd boot and exclusive use of the third UART")
    if args.platform_diagnostics and args.restart_probe:
        parser.error("--platform-diagnostics requires a launchd boot, not --restart-probe")
    output = args.output.expanduser().resolve()
    if output.exists():
        parser.error("Output already exists; choose a new directory")
    seed = args.seed_manifest.expanduser().resolve(strict=True)
    header, seed_record = seed_header(seed)
    jobs, job_records = diagnostic_jobs(args.launch_daemons.expanduser().resolve(strict=True)) if args.launch_daemons else ({}, {})
    if args.application_diagnostics:
        job = application_job()
        name = job["Label"] + ".plist"
        jobs[name] = plistlib.dumps(job)
        job_records[name] = {"output_sha256": sha256(jobs[name]), "research_added_job": True}
    if args.platform_diagnostics:
        platform_launch_job = platform_job()
        name = platform_launch_job["Label"] + ".plist"
        jobs[name] = plistlib.dumps(platform_launch_job)
        job_records[name] = {"output_sha256": sha256(jobs[name]), "research_added_job": True}
    trustcache = args.trustcache.read_bytes()
    if len(trustcache) < 24:
        raise ValueError("Truncated trustcache")
    version, _, count = struct.unpack_from("<I16sI", trustcache)
    if version != 2 or len(trustcache) != 24 + 24 * count:
        raise ValueError("Requires a valid version-2 trustcache")
    linker, ldid = (str(path.expanduser().resolve(strict=True)) for path in (args.linker, args.ldid))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".tomacco-ios-bootstrap-", dir=output.parent) as temporary:
        stage = Path(temporary)
        for name in ("research-init.c", "research-libSystem.tbd", "research-apfs.c", "research-observer.c", "research-graphics.c", "research-uikit.c", "research-application.c", "research-application-trace.c", "research-platform.c"):
            shutil.copy2(SOURCES / name, stage / name)
        (stage / "research-data-seed.h").write_text(header)
        (stage / "research-platform-contract.h").write_text(platform_header())
        if jobs:
            (stage / "launch-daemons").mkdir()
            for name, data in jobs.items():
                (stage / "launch-daemons" / name).write_bytes(data)
        subprocess.run([args.clang, "--target=arm64-apple-ios" + args.minimum_ios, "-O2", "-Wall", "-Wextra", "-Werror",
                        "-DTOMACCO_DATA_APFS=" + str(int(args.data_filesystem == "apfs")),
                        "-DTOMACCO_RESTART_PROBE=" + str(int(args.restart_probe)),
                        "-DTOMACCO_PROCESS_DIAGNOSTICS=" + str(int(args.process_diagnostics)),
                        "-DTOMACCO_GRAPHICS_DIAGNOSTICS=" + str(int(args.graphics_diagnostics)),
                        "-DTOMACCO_APPLICATION_DIAGNOSTICS=" + str(int(args.application_diagnostics)),
                        "-c", str(stage / "research-init.c"), "-o", str(stage / "init.o")], check=True)
        program = stage / "launchd"
        objects = [str(stage / "init.o")]
        if args.data_filesystem == "apfs":
            subprocess.run([args.clang, "--target=arm64-apple-ios" + args.minimum_ios, "-O2",
                            "-Wall", "-Wextra", "-Werror", "-c", str(stage / "research-apfs.c"),
                            "-o", str(stage / "apfs.o")], check=True)
            objects.append(str(stage / "apfs.o"))
        if args.process_diagnostics:
            subprocess.run([args.clang, "--target=arm64-apple-ios" + args.minimum_ios, "-O2",
                            "-Wall", "-Wextra", "-Werror",
                            "-DTOMACCO_APPLICATION_DIAGNOSTICS=" + str(int(args.application_diagnostics)),
                            "-c", str(stage / "research-observer.c"),
                            "-o", str(stage / "observer.o")], check=True)
            objects.append(str(stage / "observer.o"))
        if args.graphics_diagnostics:
            subprocess.run([args.clang, "--target=arm64-apple-ios" + args.minimum_ios, "-O2",
                            "-Wall", "-Wextra", "-Werror", "-DTOMACCO_UIKIT_DIAGNOSTICS=" + str(int(args.graphics_profile == "uikit")),
                            "-c", str(stage / "research-graphics.c"),
                            "-o", str(stage / "graphics.o")], check=True)
            graphics_objects = [str(stage / "graphics.o")]
            if args.graphics_profile == "uikit":
                subprocess.run([args.clang, "--target=arm64-apple-ios" + args.minimum_ios, "-O2",
                                "-Wall", "-Wextra", "-Werror", "-fblocks", "-c", str(stage / "research-uikit.c"),
                                "-o", str(stage / "uikit.o")], check=True)
                graphics_objects.append(str(stage / "uikit.o"))
            (stage / "programs").mkdir()
            graphics_program = stage / "programs/graphics-probe"
            subprocess.run([linker, "-arch", "arm64", "-platform_version", "ios", args.minimum_ios, args.minimum_ios,
                            "-dead_strip", "-o", str(graphics_program), str(stage / "research-libSystem.tbd"),
                            *graphics_objects], check=True)
        if args.application_diagnostics:
            subprocess.run([args.clang, "--target=arm64-apple-ios" + args.minimum_ios, "-O2",
                            "-Wall", "-Wextra", "-Werror", "-fblocks", "-c", str(stage / "research-application.c"),
                            "-o", str(stage / "application.o")], check=True)
            subprocess.run([args.clang, "--target=arm64-apple-ios" + args.minimum_ios, "-O2",
                            "-Wall", "-Wextra", "-Werror", "-c", str(stage / "research-application-trace.c"),
                            "-o", str(stage / "application-trace.o")], check=True)
            bundle = stage / "programs/TomaccoProbe.app"
            bundle.mkdir(parents=True)
            (bundle / "Info.plist").write_bytes(plistlib.dumps(application_info(args.minimum_ios)))
            application_program = bundle / "TomaccoProbe"
            subprocess.run([linker, "-arch", "arm64", "-platform_version", "ios", args.minimum_ios, args.minimum_ios,
                            "-dead_strip", "-o", str(application_program), str(stage / "research-libSystem.tbd"),
                            str(stage / "application.o"), str(stage / "application-trace.o")], check=True)
        if args.platform_diagnostics:
            subprocess.run([args.clang, "--target=arm64-apple-ios" + args.minimum_ios, "-O2",
                            "-Wall", "-Wextra", "-Werror", "-c", str(stage / "research-platform.c"),
                            "-o", str(stage / "platform.o")], check=True)
            (stage / "programs").mkdir(exist_ok=True)
            platform_program = stage / "programs/platform-probe"
            subprocess.run([linker, "-arch", "arm64", "-platform_version", "ios", args.minimum_ios, args.minimum_ios,
                            "-dead_strip", "-o", str(platform_program), str(stage / "research-libSystem.tbd"),
                            str(stage / "platform.o")], check=True)
        subprocess.run([linker, "-arch", "arm64", "-platform_version", "ios", args.minimum_ios, args.minimum_ios,
                        "-dead_strip", "-o", str(program), str(stage / "research-libSystem.tbd"), *objects], check=True)
        code_hash = sign_program(ldid, program, {"com.apple.private.stackshot": True} if args.process_diagnostics else {})
        entries = {trustcache[offset:offset + 24] for offset in range(24, len(trustcache), 24)}
        entries.add(code_hash + bytes([2, 0, 0, 0]))
        graphics_record = None
        if args.graphics_diagnostics:
            graphics_hash = sign_program(ldid, graphics_program, {})
            entries.add(graphics_hash + bytes([2, 0, 0, 0]))
            graphics_record = {"path": "programs/graphics-probe", "guest_path": "/tomacco/graphics-probe",
                               "sha256": sha256(graphics_program.read_bytes()), "cdhash": graphics_hash.hex(),
                               "entitlements": {}, "fresh_exec": True}
        application_record = None
        if args.application_diagnostics:
            application_hash = sign_program(ldid, application_program, {})
            entries.add(application_hash + bytes([2, 0, 0, 0]))
            application_record = {"path": "programs/TomaccoProbe.app/TomaccoProbe",
                                  "guest_path": "/tomacco/TomaccoProbe.app/TomaccoProbe",
                                  "sha256": sha256(application_program.read_bytes()), "cdhash": application_hash.hex(),
                                  "info_plist_sha256": sha256((bundle / "Info.plist").read_bytes()),
                                  "entitlements": {}, "fresh_exec": True, "guest_uid": 501, "guest_gid": 501,
                                  "launcher": "apple-launchd", "job": job["Label"] + ".plist"}
        platform_record = None
        if args.platform_diagnostics:
            platform_hash = sign_program(ldid, platform_program, {})
            entries.add(platform_hash + bytes([2, 0, 0, 0]))
            platform_record = {"path": "programs/platform-probe", "guest_path": "/tomacco/platform-probe",
                               "sha256": sha256(platform_program.read_bytes()), "cdhash": platform_hash.hex(),
                               "entitlements": {}, "fresh_exec": True, "launcher": "apple-launchd",
                               "job": platform_launch_job["Label"] + ".plist"}
        combined = struct.pack("<I16sI", 2, uuid.uuid4().bytes, len(entries)) + b"".join(sorted(entries))
        (stage / "bootstrap.tc").write_bytes(combined)
        record = {"format": 1, "scope": "apfs-restart-probe" if args.restart_probe else "full-system-boot-research", "apple_sdk_used": False,
                  "macos_used": False, "persistent_data": False, "graphical_apps_qualified": False,
                  "data_filesystem": args.data_filesystem,
                  "data_bytes": 2147483648 if args.data_filesystem == "tmpfs" else None,
                  "data_volume_role": 64 if args.data_filesystem == "apfs" else None,
                  "data_probe_bytes": 4096,
                  "data_probe_remount_required": args.data_filesystem == "apfs",
                  "restart_probe": args.restart_probe,
                  "launchd_enabled": not args.restart_probe,
                  "process_diagnostics": args.process_diagnostics,
                  "platform_diagnostics": args.platform_diagnostics,
                  "platform_program": platform_record,
                  "platform_contract": {"transport": "qemuport2-raw-uart", "format": "json-lines",
                                        "ticks": [30, 90], "deadline_guest_seconds": 120,
                                        "classes": CLASSES, "device_tree_paths": NODE_PATHS,
                                        "max_bytes": MAX_LOG, "max_entries_per_class": CLASS_LIMIT,
                                        "read_only": True} if args.platform_diagnostics else None,
                  "platform_source_sha256": sha256((stage / "research-platform.c").read_bytes()),
                  "platform_contract_sha256": sha256((stage / "research-platform-contract.h").read_bytes()),
                  "graphics_diagnostics": args.graphics_diagnostics,
                  "application_diagnostics": args.application_diagnostics,
                  "application_program": application_record,
                  "application_contract": {"transport": "qemuport1-raw-uart", "format": "json-lines",
                                           "delay_guest_seconds": 20, "deadline_guest_seconds": 120,
                                           "application_main": True, "scene_based": True,
                                           "main_display_qualified": False, "graphical_apps_qualified": False}
                                           if args.application_diagnostics else None,
                  "graphics_program": graphics_record,
                  "graphics_transport": "qemuport1-raw-uart" if args.graphics_diagnostics else None,
                  "graphics_contract": {"width": 256, "height": 384, "stride": 1024,
                                        "profile": args.graphics_profile,
                                        "renderers": ["coregraphics", "calayer"] + (["uikit-before", "uikit-after"] if args.graphics_profile == "uikit" else []),
                                        "encoding": "zlib", "pixel_format": "bgra8-premultiplied",
                                        "delay_guest_seconds": 20, "deadline_guest_seconds": 60,
                                        "guest_qos": "user-interactive",
                                        "main_display_qualified": False} if args.graphics_diagnostics else None,
                  "graphics_source_sha256": sha256((stage / "research-graphics.c").read_bytes()),
                  "uikit_source_sha256": sha256((stage / "research-uikit.c").read_bytes()),
                  "application_source_sha256": sha256((stage / "research-application.c").read_bytes()),
                  "application_trace_source_sha256": sha256((stage / "research-application-trace.c").read_bytes()),
                  "diagnostic_transport": "qemuport0-raw-uart" if args.process_diagnostics else None,
                  "diagnostic_sampling": "independent-process" if args.process_diagnostics else None,
                  "observer_source_sha256": sha256((stage / "research-observer.c").read_bytes()),
                  "diagnostic_jobs": job_records,
                  "seed": seed_record,
                  "source_sha256": sha256((stage / "research-init.c").read_bytes()),
                  "apfs_source_sha256": sha256((stage / "research-apfs.c").read_bytes()),
                  "init_sha256": sha256(program.read_bytes()), "cdhash": code_hash.hex(),
                  "input_trustcache_sha256": sha256(trustcache),
                  "trustcache_sha256": sha256(combined), "trustcache_entries": len(entries)}
        (stage / "manifest.json").write_text(json.dumps(record, indent=2) + "\n")
        stage.rename(output)
    print(json.dumps({"output": str(output), **record}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
