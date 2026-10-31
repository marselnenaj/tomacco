#!/usr/bin/env python3
"""Prepare an iOS restore-shell guest entirely on x86_64 Linux.

The APFS writer runs only in an isolated, networkless Linux helper VM. No
host mounts, root privileges, macOS, or host kernel-module loading are used.
This does not create a SpringBoard or graphical app-development runtime.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.request
import uuid

import build_ios_emulator as build

SUPPORT = {
    "darwin-vm": ("https://github.com/jprx/darwin-vm.git", "e969c3adffd89153a642d0626f98c9e03f92db05"),
    "ldid": ("https://github.com/ProcursusTeam/ldid.git", "af86971ae72ec3ed3d0a699107c4e882324c941b"),
    "apfs": ("https://github.com/linux-apfs/linux-apfs-rw.git", "923526a669d7c7d0d209c904436db3a3b8d14e97"),
}
DOWNLOADS = {
    "ipsw": ("https://github.com/blacktop/ipsw/releases/download/v3.1.730/ipsw_3.1.730_linux_x86_64.tar.gz",
             "7a9bb4f0b52046344e540a89f8623206bd2d7ec7a9fc0b9cd27ba41d7a4f9c28"),
    "busybox": ("https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/busybox-static-1.37.0-r31.apk",
                "b0d6cfc585d7fdd12df359cc5d38d5e45894e9e9e78c4059eb41577d022ec734"),
}
COMPONENTS = {"kernel": ("KernelCache", "bootkc"), "device_tree": ("DeviceTree", "dtree"),
              "sptm": ("Ap,SecurePageTableMonitor", "sptm"), "txm": ("Ap,TrustedExecutionMonitor", "txm"),
              "ramdisk": ("RestoreRamDisk", "ramdisk.dmg"), "trustcache": ("RestoreTrustCache", "ramdisk.tc")}
MAX_PAYLOAD_BYTES = 256 * 1024 * 1024
MAX_PAYLOAD_ENTRIES = 4096
APFS_PATCH = build.REPO / "integrations/ios-emulator/apfs-buffer-allocation.patch"


def download(name, root):
    url, expected = DOWNLOADS[name]
    archive = root / Path(urllib.parse.urlparse(url).path).name
    if not archive.exists():
        temporary = archive.with_suffix(archive.suffix + ".part")
        with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        if build.sha256(temporary) != expected:
            temporary.unlink()
            raise RuntimeError(f"{name} download checksum mismatch")
        temporary.rename(archive)
    if build.sha256(archive) != expected:
        raise RuntimeError(f"Preserved unexpected existing archive: {archive}")
    destination = root / name
    destination.mkdir(exist_ok=True)
    if name == "busybox":
        # APK contains concatenated tar streams. Extract one fixed regular file.
        data = subprocess.check_output(["tar", "-xOzf", str(archive), "bin/busybox.static"])
        (destination / "busybox").write_bytes(data)
        (destination / "busybox").chmod(0o755)
    else:
        with tarfile.open(archive) as archive_file:
            archive_file.extractall(destination, filter="data")
    return destination


def confined_file(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("Firmware component escapes its directory")
    for candidate in (root / relative, root / relative.name):
        if candidate.is_file():
            candidate = candidate.resolve()
            if not candidate.is_relative_to(root):
                raise RuntimeError("Firmware component symlink escapes its directory")
            return candidate
    raise RuntimeError(f"Missing firmware component: {relative}")


def remote_file(ipsw, url, directory, name):
    result = build.run([ipsw, "extract", "--remote", url, "--output", directory, "--flat",
                        "--pattern", "^" + re.escape(name) + "$", "-j"], capture=True)
    files = json.loads(result.stdout)
    if len(files) != 1:
        raise RuntimeError(f"Expected one firmware entry for {name}")
    file = Path(files[0]).resolve()
    if not file.is_relative_to(directory.resolve()) or not file.is_file():
        raise RuntimeError("Unexpected IPSW extraction path")
    return file


def write_cpio(directory, destination):
    """Write newc with numeric guest root ownership, without host chown."""
    with destination.open("wb") as output:
        for inode, item in enumerate([*sorted(directory.rglob("*")), None], 1):
            if item is None:
                name, mode, data, links = "TRAILER!!!", 0, b"", 1
            else:
                name, mode = str(item.relative_to(directory)), item.lstat().st_mode
                data = os.readlink(item).encode() if item.is_symlink() else item.read_bytes() if item.is_file() else b""
                links = 2 if item.is_dir() else 1
            encoded = name.encode() + b"\0"
            fields = [inode, mode, 0, 0, links, 0, len(data), 0, 0, 0, 0, len(encoded), 0]
            header = b"070701" + b"".join(f"{field:08x}".encode() for field in fields)
            output.write(header + encoded + b"\0" * ((-len(header) - len(encoded)) % 4))
            output.write(data + b"\0" * ((-len(data)) % 4))


def copy_guest_payload(source, destination):
    """Snapshot bounded, ordinary files into /tomacco without modifying sources."""
    source = source.expanduser().resolve(strict=True)
    if not source.is_dir() or destination.resolve().is_relative_to(source):
        raise RuntimeError("Payload must be a directory outside the preparation output")
    entries = []
    for entry in source.rglob("*"):
        entries.append(entry)
        if len(entries) > MAX_PAYLOAD_ENTRIES:
            raise RuntimeError("Payload exceeds 4096 filesystem entries")
    files, total = [], 0
    destination.mkdir()
    for entry in sorted(entries):
        relative = entry.relative_to(source)
        if any(ord(char) < 32 or ord(char) == 127 for char in relative.as_posix()):
            raise RuntimeError("Payload names must not contain control characters")
        mode = entry.lstat().st_mode
        if len(relative.parts) > 32 or not entry.resolve().is_relative_to(source):
            raise RuntimeError("Payload path escapes its directory or exceeds 32 levels")
        target = destination / relative
        if stat.S_ISDIR(mode):
            target.mkdir(mode=0o755)
        elif stat.S_ISREG(mode):
            with os.fdopen(os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as input_file, target.open("xb") as output:
                if not stat.S_ISREG(os.fstat(input_file.fileno()).st_mode):
                    raise RuntimeError("Payload contains a non-regular file")
                checksum, size = hashlib.sha256(), 0
                while chunk := input_file.read(1024 * 1024):
                    size += len(chunk)
                    total += len(chunk)
                    if total > MAX_PAYLOAD_BYTES:
                        raise RuntimeError("Payload exceeds 256 MiB")
                    checksum.update(chunk)
                    output.write(chunk)
            target.chmod(0o755 if mode & 0o111 else 0o644)
            files.append({"path": "/tomacco/" + relative.as_posix(), "source_sha256": checksum.hexdigest(),
                          "source_bytes": size})
        else:
            raise RuntimeError("Payload contains a symlink or special file")
    if not files:
        raise RuntimeError("Payload must contain at least one regular file")
    return files


def helper_accelerator(requested):
    if requested == "tcg":
        return "tcg"
    try:
        with open("/dev/kvm", "rb+", buffering=0) as device:
            if fcntl.ioctl(device, 0xAE00) == 12:  # KVM_GET_API_VERSION
                return "kvm"
    except OSError:
        pass
    if requested == "kvm":
        raise RuntimeError("KVM is unavailable to this user; use --helper-accel tcg")
    return "tcg"


INIT = """#!/bin/sh
export PATH=/bin
/bin/busybox --install -s /bin
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
set -eu
set -o pipefail
trap 'echo TOMACCO_APFS_FAILED; sync; poweroff -f' EXIT
insmod /apfs.ko
mount -t apfs -o readwrite /dev/vda /mnt
mv /mnt/System/Library/LaunchDaemons /mnt/System/Library/LaunchDaemons.original
mkdir /mnt/System/Library/LaunchDaemons
cp /shell.plist /mnt/System/Library/LaunchDaemons/com.tomacco.shell.plist
cd /payload
find . -type d | while IFS= read -r path; do mkdir -p "/mnt/$path"; done
find . ! -type d | while IFS= read -r path; do rm -f "/mnt/$path"; cp -Pp "$path" "/mnt/$path"; done
chown -R 0:0 /mnt/bin /mnt/sbin /mnt/libexec /mnt/System/Library/LaunchDaemons
if [ -d /mnt/tomacco ]; then chown -R 0:0 /mnt/tomacco; fi
chmod 755 /mnt/System/Library/LaunchDaemons
chmod 644 /mnt/System/Library/LaunchDaemons/com.tomacco.shell.plist
sync
umount /mnt
trap - EXIT
echo TOMACCO_APFS_PREPARED
poweroff -f
"""


def prepare(args, root, stage, log_directory):
    configuration = json.loads((root / "build-manifest.json").read_text())
    if configuration.get("backend") != "sptm":
        raise RuntimeError("Build the sptm backend first with tools/build_ios_emulator.py")
    kernel_root = Path("/usr/lib/modules") / args.kernel_version
    kernel = kernel_root / "vmlinuz"
    config = (kernel_root / "build/.config").read_text()
    for feature in ("BLK_DEV_INITRD", "VIRTIO_BLK", "VIRTIO_PCI", "DEVTMPFS"):
        if f"CONFIG_{feature}=y\n" not in config:
            raise RuntimeError(f"The helper kernel requires built-in CONFIG_{feature}")
    if "CONFIG_MODULE_SIG_FORCE=y" in config or not kernel.is_file():
        raise RuntimeError("The helper kernel must be readable and permit the locally built APFS module")
    support = root / "support"
    support.mkdir(exist_ok=True)
    build.SOURCES.update(SUPPORT)
    for name in SUPPORT:
        source = support / name
        build.checkout(name, source)
        current = build.run(["git", "-C", source, "diff", "--binary", "HEAD"], capture=True).stdout
        expected = APFS_PATCH.read_text() if name == "apfs" else ""
        if current and current != expected:
            raise RuntimeError(f"Preserved local edits in {source}")
        if expected and not current:
            build.run(["git", "-C", source, "apply", "--check", APFS_PATCH])
            build.run(["git", "-C", source, "apply", APFS_PATCH])
    darwin, ldid, apfs = [support / name for name in ("darwin-vm", "ldid", "apfs")]
    build.run(["make", "-j", args.jobs, "VERSION=2.1.5-procursus7"], cwd=ldid)
    build.run(["make", "-j", args.jobs, f"KERNEL_DIR={kernel_root / 'build'}"], cwd=apfs)
    ipsw = download("ipsw", support) / "ipsw"
    busybox = download("busybox", support) / "busybox"
    downloads = stage / "downloads"
    downloads.mkdir()
    if args.firmware:
        inputs = args.firmware.expanduser().resolve()
        manifest_path = confined_file(inputs, "BuildManifest.plist")
    else:
        inputs = downloads
        manifest_path = remote_file(ipsw, args.ipsw_url, inputs, "BuildManifest.plist")
    manifest_bytes = manifest_path.read_bytes()
    if len(manifest_bytes) > 16 * 1024 * 1024:
        raise RuntimeError("Firmware manifest exceeds 16 MiB")
    manifest = plistlib.loads(manifest_bytes)
    identity = next((value for value in manifest["BuildIdentities"] if
                     value["Info"]["DeviceClass"] == args.board and
                     value["Info"]["Variant"] == "Customer Erase Install (IPSW)"), None)
    if not identity:
        raise RuntimeError("No matching release erase identity in the supplied firmware")
    (stage / "BuildManifest.plist").write_bytes(manifest_bytes)
    source_hashes = {}
    for name, (key, filename) in COMPONENTS.items():
        relative = identity["Manifest"][key]["Info"]["Path"]
        source = confined_file(inputs, relative) if args.firmware else remote_file(ipsw, args.ipsw_url, inputs, relative)
        source_hashes[name] = {"manifest_path": relative, "sha256": build.sha256(source)}
        build.run([ipsw, "img4", "im4p", "extract", source, "-o", stage / filename])
    original_tree = stage / "dtree.original"
    (stage / "dtree").rename(original_tree)
    build.run(["python3", darwin / "dt_fixup.py", "-nvram", darwin / "nvram.bin", original_tree, stage / "dtree"])
    image_root = stage / "helper-initramfs"
    image_root.mkdir()
    for folder in ("bin", "dev", "proc", "sys", "mnt"):
        (image_root / folder).mkdir()
    with tarfile.open(darwin / "sysroot/ios_sysroot.tar.gz") as archive:
        archive.extractall(image_root, filter="data")
    payload = image_root / "payload"
    (image_root / "ios_sysroot").rename(payload)
    user_files = copy_guest_payload(args.payload, payload / "tomacco") if args.payload else []
    cache = (stage / "ramdisk.tc").read_bytes()
    version, _, count = struct.unpack_from("<I16sI", cache)
    if version != 1 or len(cache) != 24 + count * 22:
        raise RuntimeError("Unsupported restore trust-cache format")
    hashes = {cache[24 + index * 22:24 + (index + 1) * 22] for index in range(count)}
    signed = 0
    user_entries = {entry["path"].lstrip("/"): entry for entry in user_files}
    for file in sorted(payload.rglob("*")):
        if file.is_symlink() or not file.is_file():
            continue
        with file.open("rb") as stream:
            magic = stream.read(4)
        if magic not in (bytes.fromhex("cffaedfe"), bytes.fromhex("cafebabe")):
            continue
        user_entry = user_entries.get(file.relative_to(payload).as_posix())
        signing = [ldid / "ldid", "-Cadhoc"]
        if user_entry is not None:
            # Retain the user's declared guest capabilities. Bare -S silently
            # dropped them, so correctly signed native payloads could fail.
            before = build.run([ldid / "ldid", "-e", file], capture=True).stdout
            entitlements = plistlib.loads(before.encode()) if before.strip() else {}
            if not isinstance(entitlements, dict):
                raise RuntimeError(f"Payload entitlements must be a dictionary: {file.name}")
            signing.append("-M")
        build.run([*signing, "-S", file])
        if user_entry is not None:
            after = build.run([ldid / "ldid", "-e", file], capture=True).stdout
            actual = plistlib.loads(after.encode()) if after.strip() else {}
            if actual != entitlements:
                raise RuntimeError(f"Payload entitlements changed while signing: {file.name}")
            user_entry["entitlements"] = entitlements
        signature = build.run([ldid / "ldid", "-h", file], capture=True).stdout
        cdhashes = re.findall(r"CDHash=([0-9a-fA-F]{40})", signature)
        if not cdhashes:
            raise RuntimeError(f"No code-directory hash returned for {file.name}")
        hashes.update(bytes.fromhex(value) + bytes([2, 0]) for value in cdhashes)
        signed += 1
    for entry in user_files:
        file = payload / entry["path"].lstrip("/")
        entry.update(guest_sha256=build.sha256(file), guest_bytes=file.stat().st_size)
    (stage / "ramdisk.tc").write_bytes(struct.pack("<I16sI", 1, uuid.uuid4().bytes, len(hashes)) + b"".join(sorted(hashes)))
    shutil.copy2(busybox, image_root / "bin/busybox")
    (image_root / "bin/sh").symlink_to("busybox")
    shutil.copy2(apfs / "apfs.ko", image_root / "apfs.ko")
    shutil.copy2(darwin / "launchdaemons/com.jprx.bash.plist", image_root / "shell.plist")
    (image_root / "init").write_text(INIT)
    (image_root / "init").chmod(0o755)
    initrd = stage / "helper.cpio"
    write_cpio(image_root, initrd)
    helper = Path(configuration["helper_engine"])
    if not helper.is_file():
        raise RuntimeError("Missing qemu-system-x86_64 helper; rebuild the sptm backend")
    accelerator = helper_accelerator(args.helper_accel)
    argv = [helper, "-L", configuration["bios_directory"], "-machine", "q35", "-accel", accelerator,
            "-cpu", "host" if accelerator == "kvm" else "max",
            "-m", "1G", "-smp", "2", "-kernel", kernel, "-initrd", initrd,
            "-append", "console=ttyS0 rdinit=/init panic=-1 mitigations=off",
            "-drive", f"file={str(stage / 'ramdisk.dmg').replace(',', ',,')},format=raw,if=virtio",
            "-display", "none", "-serial", "stdio", "-monitor", "none", "-nic", "none", "-no-reboot"]
    log = log_directory / "apfs-helper.log"
    (log_directory / "command.json").write_text(json.dumps(list(map(str, argv)), indent=2) + "\n")
    with log.open("wb") as stream:
        subprocess.run(list(map(str, argv)), stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, timeout=90, check=True)
    text = log.read_text(errors="replace")
    if ("TOMACCO_APFS_PREPARED" not in text or "TOMACCO_APFS_FAILED" in text or
            re.search(r"APFS .*?(?:failed|corrupt|error)", text)):
        raise RuntimeError(f"APFS preparation failed; see {log}")
    record = {"format": 1, "backend": "sptm", "environment": "ios-restore-shell", "board": args.board,
              "version": manifest["ProductVersion"], "build": manifest["ProductBuildVersion"], "variant": "release",
              "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(), "macos_used": False,
              "helper_kernel": args.kernel_version, "helper_kernel_sha256": build.sha256(kernel),
              "helper_accelerator": accelerator,
              "apfs_patch_sha256": build.sha256(APFS_PATCH),
              "source_commits": {name: revision for name, (_, revision) in SUPPORT.items()},
              "source_components": source_hashes, "signed_cli_binaries": signed, "qualified_for_app_development": False,
              "payload": user_files,
              "components": {name: {"path": filename, "sha256": build.sha256(stage / filename)}
                             for name, (_, filename) in COMPONENTS.items()}}
    (stage / "tomacco-ios.json").write_text(json.dumps(record, indent=2) + "\n")
    # Delete only setup inputs inside this owned staging directory.
    shutil.rmtree(image_root)
    shutil.rmtree(downloads)
    initrd.unlink()
    original_tree.unlink()
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain", type=Path, default=Path.cwd() / ".tooling/ios-sptm")
    parser.add_argument("--output", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--firmware", type=Path, help="Extracted IPSW or flat boot components, including BuildManifest.plist")
    source.add_argument("--ipsw-url", help="Official HTTPS Apple IPSW URL; fetches only six boot components")
    parser.add_argument("--payload", type=Path, help="Copy files into /tomacco; sign Mach-O programs and add them to the guest trust cache")
    parser.add_argument("--board", default="d47ap", help="Manifest board, default iPhone 16")
    parser.add_argument("--kernel-version", default=platform.release())
    parser.add_argument("--helper-accel", choices=["auto", "kvm", "tcg"], default="auto",
                        help="Use KVM for the isolated Linux preparation guest when accessible; otherwise TCG")
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 2, 16))
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        parser.error("The virtual iOS device needs an x86_64 Linux host. On " + platform.machine() + ", the tools and the agent API work, experimentally, and this helper does not.")
    if not re.fullmatch(r"[A-Za-z0-9._+-]+", args.kernel_version) or not 1 <= args.jobs <= 64:
        parser.error("Invalid kernel version or job count")
    if args.ipsw_url:
        url = urllib.parse.urlparse(args.ipsw_url)
        if url.scheme != "https" or not (url.hostname or "").endswith(".apple.com"):
            parser.error("Use an official HTTPS Apple URL or --firmware for local components")
    output = args.output.expanduser().resolve()
    if output.exists():
        parser.error("Output already exists; preserved it. Choose a new directory.")
    root = args.toolchain.expanduser().resolve()
    logs = root / "preparation-logs" / uuid.uuid4().hex
    logs.mkdir(parents=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".tomacco-ios-prep-", dir=output.parent) as temporary:
        stage = Path(temporary)
        record = prepare(args, root, stage, logs)
        stage.rename(output)
    print(json.dumps({"firmware": str(output), "version": record["version"], "build": record["build"],
                      "environment": record["environment"], "macos_used": False, "logs": str(logs)}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
