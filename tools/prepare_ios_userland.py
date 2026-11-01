#!/usr/bin/env python3
"""Import original iPhone userland and its PCC GPU driver on Linux.

Copies and checks the complete OS/Cryptex image set against the selected source
BuildManifest. Extracts the original GPU bundle in a networkless, read-only
Linux helper. Does not change guest authentication, install firmware, or boot
iOS. Prepared assets are not a qualified graphical runtime.
"""
import argparse
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import signal
import stat
import struct
import subprocess
import tarfile
import time

from ios_paravirt_asr import VerifiedImage, parse_plist
from ios_paravirt_firmware import read_component, retag_im4p
from prepare_ios_emulator import write_cpio
from build_ios_gpu_plugin import NAME as PLUGIN_NAME, SOURCE_SHA256 as PLUGIN_SOURCE_SHA256, inspect_plugin

IMAGES = {"OS": "system.dmg", "Cryptex1,SystemOS": "system-cryptex.dmg",
          "Cryptex1,AppOS": "app-cryptex.dmg"}
METADATA = ("StaticTrustCache", "SystemVolume", "Ap,SystemVolumeCanonicalMetadata",
            "Cryptex1,SystemTrustCache", "Cryptex1,SystemVolume",
            "Cryptex1,AppTrustCache", "Cryptex1,AppVolume")
COMPONENTS = (*IMAGES, *METADATA)
BUNDLE = "System/Library/Extensions/AppleParavirtGPUMetalIOGPUFamily.bundle"
DRIVER = "AppleParavirtGPUMetalIOGPUFamily"
EXPORT_LIMIT = 64 << 20
MANIFEST_LIMIT = 16 << 20
# Space a streamed copy must leave free on its disk.
COPY_HEADROOM = 1 << 30
COPY_CHUNK = 4 << 20
_ZERO_CHUNK = bytes(COPY_CHUNK)


def data_extent(fd, position, size, state):
    """The next data range at or after `position` as (start, end): from the
    OS's SEEK_DATA/SEEK_HOLE, or all of the rest where the filesystem cannot
    tell (state["sparse"] False afterwards). A hole reads as zeros, so a copy
    that skips it is the same file."""
    if not state["sparse"]:
        return position, size
    try:
        start = os.lseek(fd, position, os.SEEK_DATA)
        end = os.lseek(fd, start, os.SEEK_HOLE)
    except OSError as error:
        if error.errno == errno.ENXIO:
            return size, size
        if error.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS):
            raise
        state["sparse"] = False
        return position, size
    if not position <= start < end:
        raise ValueError("The filesystem reported an invalid data range")
    return min(start, size), min(end, size)


def sparse_copy(source_fd, target_fd, size, deadline):
    """Copy `size` bytes so that the copy has holes where the source has
    holes or whole zero chunks. A disk copied this way takes the space of its
    data only, and the launcher's hash reads only that data (it hashes holes as
    zeros without reading them). The copy reads every data byte of the source;
    what it leaves out reads back as zeros."""
    state = {"sparse": getattr(os, "SEEK_DATA", None) is not None and getattr(os, "SEEK_HOLE", None) is not None}
    position = written = 0
    while position < size:
        if time.monotonic() >= deadline:
            raise TimeoutError("Userland import deadline exceeded")
        start, end = data_extent(source_fd, position, size, state)
        position = start
        while position < end:
            if time.monotonic() >= deadline:
                raise TimeoutError("Userland import deadline exceeded")
            data = os.pread(source_fd, min(COPY_CHUNK, end - position), position)
            if not data:
                raise ValueError("Userland input was truncated")
            if data != (_ZERO_CHUNK if len(data) == COPY_CHUNK else bytes(len(data))):
                view = memoryview(data)
                while view:
                    count = os.pwrite(target_fd, view, position + len(data) - len(view))
                    view = view[count:]
                written += len(data)
            position += len(data)
    os.ftruncate(target_fd, size)
    return {"data_bytes_written": written, "zero_bytes_skipped": size - written,
            "extent_queries": "used" if state["sparse"] else "unavailable"}


def save(output, record):
    temporary = output / ".userland.json.tmp"
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(record, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output / "userland.json")


def relative_path(value):
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ValueError("Invalid firmware component path")
    return Path(value)


def manifest(root, index, *, pcc=False, filename="BuildManifest.plist"):
    data = read_component(root, filename, MANIFEST_LIMIT)
    value = parse_plist(data)
    identities = value.get("BuildIdentities") if isinstance(value, dict) else None
    if (not isinstance(identities, list) or type(index) is not int
            or not 0 <= index < len(identities) or not isinstance(identities[index], dict)):
        raise ValueError("Select an existing firmware identity")
    identity = identities[index]
    info, entries = identity.get("Info"), identity.get("Manifest")
    variants = (("Darwin Cloud Customer Erase Install (IPSW)",
                 "Research Darwin Cloud Customer Erase Install (IPSW)") if pcc
                else ("Customer Erase Install (IPSW)",))
    if (not isinstance(info, dict) or not isinstance(entries, dict)
            or info.get("Variant") not in variants):
        raise ValueError("An explicit erase-install firmware identity is required")
    board = info.get("DeviceClass")
    products = value.get("SupportedProductTypes")
    if pcc:
        if board != "vphone600ap":
            raise ValueError("The GPU source must be a vphone600 PCC identity")
    elif (info["Variant"] != "Customer Erase Install (IPSW)"
          or not isinstance(products, list) or not products
          or any(not isinstance(p, str) or not p.startswith(("iPhone", "iPad")) for p in products)):
        raise ValueError("Userland requires a production iPhone/iPad erase identity")
    keys = ("OS",) if pcc else COMPONENTS
    for name in keys:
        entry = entries.get(name)
        if (not isinstance(entry, dict) or not isinstance(entry.get("Info"), dict)
                or not isinstance(entry.get("Digest"), bytes) or len(entry["Digest"]) != 48):
            raise ValueError(f"Missing SHA-384 manifest component: {name}")
        relative_path(entry["Info"].get("Path"))
    if not all(isinstance(value.get(k), str) and value[k] for k in ("ProductVersion", "ProductBuildVersion")):
        raise ValueError("Firmware has no version/build identity")
    return data, entries, {"product_version": value["ProductVersion"],
                           "build_version": value["ProductBuildVersion"],
                           "board": board, "supported_products": products,
                           "identity_index": index, "variant": info["Variant"],
                           "build_manifest_sha256": hashlib.sha256(data).hexdigest()}


def open_source(root, relative):
    """Pin every package directory and the source without following symlinks."""
    parts = relative_path(relative).parts
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)


def snapshot(root, relative, destination, deadline, *, source_fd=None):
    """Reflink or stream a source, optionally from an already locked descriptor.
    A streamed copy keeps the source's holes and leaves whole zero chunks out
    (sparse_copy)."""
    if time.monotonic() >= deadline:
        raise TimeoutError("Userland import deadline exceeded")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # A caller holding locks on a storage set must copy those exact files,
    # even if another process renames a path while the set is being copied.
    pinned = os.dup(source_fd) if source_fd is not None else open_source(root, relative)
    with os.fdopen(pinned, "rb") as source, destination.open("xb") as target:
        target_fd, source_fd = target.fileno(), source.fileno()
        os.fchmod(target_fd, 0o600)
        fcntl.flock(source_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 64 << 30:
            raise ValueError("Userland input must be a bounded nonempty regular file")
        cloned, sparse = False, None
        try:
            fcntl.ioctl(target_fd, 0x40049409, source_fd)  # FICLONE
            cloned = True
        except OSError as error:
            if error.errno not in (errno.EOPNOTSUPP, errno.ENOTTY, errno.EXDEV, errno.EINVAL):
                raise
            target.truncate(0)
            # A full copy that runs out of space part way leaves nothing usable
            # and a nearly full disk behind; refuse it before writing.
            space = os.statvfs(target_fd)
            available = space.f_bavail * space.f_frsize
            if available < before.st_size + COPY_HEADROOM:
                raise OSError(errno.ENOSPC, f"Copying {Path(root) / relative} needs "
                              f"{(before.st_size + COPY_HEADROOM) / 2**30:.1f} GiB in {destination.parent}; "
                              f"{available / 2**30:.1f} GiB are free")
            sparse = sparse_copy(source_fd, target_fd, before.st_size, deadline)
        target.flush()
        os.fsync(target_fd)
        if VerifiedImage._identity(before) != VerifiedImage._identity(os.fstat(source_fd)):
            raise ValueError("Userland input changed while copying")
        copied = os.fstat(target_fd)
    return {"source": str(Path(root) / relative), "copy_method": "reflink" if cloned else "stream",
            **({"sparse_copy": sparse} if sparse is not None else {}),
            "copy_identity": {"device": copied.st_dev, "inode": copied.st_ino, "bytes": copied.st_size,
                              "mtime_ns": copied.st_mtime_ns, "ctime_ns": copied.st_ctime_ns}}


def import_image(root, relative, destination, expected, deadline):
    copied = snapshot(root, relative, destination, deadline)
    with VerifiedImage(destination, expected, deadline) as verified:
        header = os.pread(verified.fd, 48, 0)
        if header[:4] == b"AEA1":
            raise ValueError("Supply the decrypted original disk image, not an AEA archive")
        if len(header) != 48 or header[32:36] != b"NXSB":
            raise ValueError("Expected an original raw APFS disk image")
        block_size, blocks = struct.unpack_from("<IQ", header, 36)
        if (block_size not in (4096, 8192, 16384, 32768, 65536)
                or not blocks or block_size * blocks != verified.size):
            raise ValueError("APFS container geometry does not match the image")
        return {**copied, **verified.record, "apfs_block_size": block_size, "apfs_blocks": blocks}


def unpack_gpu(archive, destination, expected_version):
    """Only ordinary files/directories of this one bundle may leave the helper."""
    rows, names, total = [], set(), 0
    with tarfile.open(archive, mode="r:") as source:
        for entry in source:
            name = entry.name.rstrip("/")
            relative_path(name)
            path = PurePosixPath(name)
            if not path.is_relative_to(BUNDLE) or name in names or len(names) >= 1024:
                raise ValueError("Unexpected or duplicate GPU export entry")
            names.add(name)
            relative = path.relative_to(BUNDLE)
            target = destination / relative
            if entry.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            if not entry.isfile() or entry.size < 0:
                raise ValueError("GPU export contains a link or special file")
            total += entry.size
            if total > EXPORT_LIMIT:
                raise ValueError("GPU export exceeds 64 MiB")
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            stream = source.extractfile(entry)
            if stream is None:
                raise ValueError("Missing GPU export data")
            checksum = hashlib.sha256()
            with stream, target.open("xb") as output:
                os.fchmod(output.fileno(), 0o700 if entry.mode & 0o111 else 0o600)
                remaining = entry.size
                while remaining:
                    data = stream.read(min(1 << 20, remaining))
                    if not data:
                        raise ValueError("Truncated GPU export")
                    remaining -= len(data)
                    output.write(data)
                    checksum.update(data)
            rows.append({"path": str(relative), "bytes": entry.size, "sha256": checksum.hexdigest()})
    required = {DRIVER, "Info.plist", "_CodeSignature/CodeResources"}
    if not required.issubset(row["path"] for row in rows):
        raise ValueError("PCC GPU bundle is incomplete")
    info = parse_plist(read_component(destination, "Info.plist", 1 << 20))
    if (not isinstance(info, dict) or info.get("CFBundleIdentifier") != "com.apple.driver." + DRIVER
            or info.get("DTPlatformVersion") != expected_version):
        raise ValueError("GPU bundle does not match the PCC platform version")
    with (destination / DRIVER).open("rb") as stream:
        if stream.read(8) != bytes.fromhex("cffaedfe0c000001"):
            raise ValueError("PCC GPU driver is not an ARM64 Mach-O")
    return {"path": str(destination), "bundle_id": info["CFBundleIdentifier"],
            "platform_version": info["DTPlatformVersion"], "files": rows,
            "signature_verified": False, "original_code_signature_preserved": True,
            "compiler_plugin_present": any(row["path"] == "libAppleParavirtCompilerPluginIOGPUFamily.dylib"
                                           for row in rows)}


def extract_gpu(args, image, output, expected_version, deadline):
    directory = output / "gpu-extract"
    directory.mkdir(mode=0o700)
    root = directory / "root"
    root.mkdir()
    for name in ("bin", "dev", "mnt", "proc", "sys"):
        (root / name).mkdir()
    support = Path(args.support)
    for source, target in [(support / "busybox/busybox", root / "bin/busybox"),
                           (support / "apfs/apfs.ko", root / "apfs.ko")]:
        snapshot(source.parent, source.name, target, deadline)
    (root / "bin/busybox").chmod(0o755)
    (root / "bin/sh").symlink_to("busybox")
    action = f"""#!/bin/sh
export PATH=/bin
/bin/busybox --install -s /bin
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
set -eu
trap 'echo TOMACCO_USERLAND_FAILED; poweroff -f' EXIT
insmod /apfs.ko
mount -t apfs -o ro /dev/vda /mnt
test -f /mnt/{BUNDLE}/{DRIVER}
tar -cf /dev/vdb -C /mnt {BUNDLE}
sync
umount /mnt
trap - EXIT
echo TOMACCO_USERLAND_EXPORTED
poweroff -f
"""
    (root / "init").write_text(action)
    (root / "init").chmod(0o755)
    initrd = directory / "helper.cpio"
    write_cpio(root, initrd)
    archive = directory / "gpu.tar"
    with archive.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.truncate(EXPORT_LIMIT)
    def drive(path, readonly):
        return f"file={str(path).replace(',', ',,')},format=raw,if=virtio,readonly={readonly}"
    command = [args.helper_engine, "-L", args.bios, "-machine", "q35", "-accel", args.accel,
               "-cpu", "host" if args.accel == "kvm" else "max", "-m", "1G", "-smp", "1",
               "-kernel", args.helper_kernel, "-initrd", initrd,
               "-append", "console=ttyS0 rdinit=/init panic=-1 mitigations=off",
               "-drive", drive(image, "on"), "-drive", drive(archive, "off"),
               "-display", "none", "-serial", "stdio", "-monitor", "none", "-nic", "none", "-no-reboot"]
    (directory / "command.json").write_text(json.dumps(list(map(str, command)), indent=2) + "\n")
    log_path = directory / "helper.log"
    with log_path.open("xb") as log:
        process = subprocess.run(list(map(str, command)), stdout=log, stderr=subprocess.STDOUT,
                                 timeout=max(0.01, min(180, deadline - time.monotonic())))
    with log_path.open("rb") as log:
        text = log.read(4 << 20)
    if (process.returncode or b"\nTOMACCO_USERLAND_EXPORTED\r\n" not in text
            or b"TOMACCO_USERLAND_FAILED" in text):
        raise RuntimeError(f"Read-only GPU extraction failed; inspect {log_path}")
    return unpack_gpu(archive, output / "gpu" / (DRIVER + ".bundle"), expected_version)


def import_plugin(source, output):
    """Keep the source-built plugin beside, not inside, Apple's original bundle."""
    source = Path(source).resolve(strict=True)
    record = json.loads(read_component(source, "plugin.json", 1 << 20))
    if (not isinstance(record, dict) or record.get("scope") != "ios-paravirtual-gpu-compiler-plugin"
            or record.get("state") != "built" or record.get("source_sha256") != PLUGIN_SOURCE_SHA256
            or not isinstance(record.get("plugin"), dict)):
        raise ValueError("Select a completed, pinned GPU plugin build")
    data = read_component(source, PLUGIN_NAME, 16 << 20)
    digest = hashlib.sha256(data).hexdigest()
    if record["plugin"].get("sha256") != digest or record["plugin"].get("bytes") != len(data):
        raise ValueError("GPU plugin changed after its build")
    integrity = inspect_plugin(data)
    from build_ios_gpu_plugin import (BRIDGE_NAME, BRIDGE_SOURCE, BRIDGE_LOG_HEADER,
                                      BRIDGE_SURFACE_HEADER_PROFILES, inspect_bridge)
    bridge_files = []
    if "bridge" in record:
        bridge, bridge_source, bridge_header = inspect_bridge(source, record["bridge"])
        bridge_files = [(BRIDGE_NAME, bridge), (BRIDGE_SOURCE, bridge_source),
                        ("ios-gpu-path.h", bridge_header),
                        (BRIDGE_LOG_HEADER, read_component(source, BRIDGE_LOG_HEADER, 8 << 10))]
        bridge_files.extend((name, read_component(source, name, 32 << 10))
                            for name in BRIDGE_SURFACE_HEADER_PROFILES.get(
                                record["bridge"]["source_sha256"], {}))
    directory = output / "gpu-compiler"
    directory.mkdir(mode=0o700)
    for name, content in [(PLUGIN_NAME, data), ("plugin.json", json.dumps(record, indent=2).encode()),
                          ("UPSTREAM-LICENSE", read_component(source, "UPSTREAM-LICENSE", 64 << 10)),
                          *bridge_files]:
        with (directory / name).open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
    result = {"path": str(directory / PLUGIN_NAME), "sha256": digest, "bytes": len(data),
            "source_sha256": PLUGIN_SOURCE_SHA256, "original_apple_bundle_modified": False,
            "signing": "ad-hoc", "integrity": integrity, "guest_execution_verified": False}
    if bridge_files:
        result["bridge"] = {**record["bridge"], "path": str(directory / BRIDGE_NAME)}
    return result


def prepare(args):
    output = Path(args.output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Choose a new userland output directory")
    deadline = time.monotonic() + args.seconds
    output.mkdir(mode=0o700, parents=True)
    record = {"format": 1, "scope": "original-ios-userland-assets", "state": "preparing",
              "phase": "manifests", "components": {},
              "input_files_modified": False, "authentication_policy_modified": False,
              "signing_requests_sent": False, "guest_boot_attempted": False,
              "qualified_for_app_development": False}
    save(output, record)
    try:
        if args.accel == "kvm" and not os.access("/dev/kvm", os.R_OK | os.W_OK):
            raise ValueError("KVM helper requires access to /dev/kvm; select accel=tcg if KVM is unavailable")
        phone_root, pcc_root = (Path(p).resolve(strict=True) for p in (args.firmware, args.pcc_firmware))
        phone_data, components, phone = manifest(phone_root, args.identity)
        pcc_data, pcc_components, pcc = manifest(pcc_root, args.pcc_identity, pcc=True)
        overrides = {}
        for spec in args.component:
            name, separator, source = spec.partition("=")
            if not separator or name not in COMPONENTS or name in overrides or not source:
                raise ValueError("Each component override must name one unique userland component")
            overrides[name] = Path(source).absolute()
        record.update(ios=phone, pcc=pcc)
        if getattr(args, "gpu_plugin_build", None) is not None:
            record["phase"] = "gpu-compiler"
            save(output, record)
            record["gpu_compiler"] = import_plugin(args.gpu_plugin_build, output)
        record["phase"] = "images"
        for name, data in (("ios-BuildManifest.plist", phone_data), ("pcc-BuildManifest.plist", pcc_data)):
            (output / name).write_bytes(data)
        for name in COMPONENTS:
            record["current_component"] = name
            save(output, record)
            entry = components[name]
            relative = relative_path(entry["Info"]["Path"])
            source = overrides.get(name)
            root, source_name = (source.parent, source.name) if source else (phone_root, str(relative))
            if name in IMAGES:
                destination = output / "images" / IMAGES[name]
                result = import_image(root, source_name, destination, entry["Digest"], deadline)
            else:
                data = read_component(root, source_name)
                normalized, source_tag, digest_tag = retag_im4p(data, entry["Info"].get("Img4PayloadType"))
                digest = hashlib.sha384(normalized).digest()
                if digest != entry["Digest"]:
                    raise ValueError(f"{name} does not match the selected firmware manifest")
                destination = output / "components" / relative
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with destination.open("xb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(data)
                result = {"path": str(destination), "source": str(root / source_name), "bytes": len(data),
                          "sha256": hashlib.sha256(data).hexdigest(), "manifest_sha384": digest.hex(),
                          "manifest_digest_matched": True, "source_tag": source_tag, "digest_tag": digest_tag,
                          "original_bytes_preserved": True, "signature_verified": False}
            record["components"][name] = result
            save(output, record)
        record.update(phase="pcc-image", current_component="PCC:OS")
        save(output, record)
        source = Path(args.pcc_system_image).absolute()
        pcc_image = output / "images/pcc-system.dmg"
        record["pcc_system_image"] = import_image(source.parent, source.name, pcc_image,
                                                  pcc_components["OS"]["Digest"], deadline)
        record["phase"] = "gpu-extraction"
        save(output, record)
        record["gpu"] = extract_gpu(args, pcc_image, output, pcc["product_version"], deadline)
        record.pop("current_component", None)
        record.update(state="prepared", phase="complete")
        save(output, record)
        return record
    except BaseException as error:
        record.update(state="cancelled" if isinstance(error, KeyboardInterrupt) else "failed", error=str(error))
        save(output, record)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("firmware", "pcc-firmware", "pcc-system-image", "support", "helper-engine", "helper-kernel", "bios", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("identity", "pcc-identity"):
        parser.add_argument("--" + name, type=int, required=True)
    parser.add_argument("--component", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--gpu-plugin-build", type=Path, help="Optional completed source-built compiler plugin directory")
    parser.add_argument("--accel", choices=("kvm", "tcg"), default="kvm")
    parser.add_argument("--seconds", type=int, default=900)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600:
        parser.error("seconds must be between 1 and 3600")
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt("Userland import cancelled")
    signal.signal(signal.SIGTERM, interrupted)
    try:
        record = prepare(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps({"state": record["state"], "report": str(args.output / "userland.json")}, indent=2))


if __name__ == "__main__":
    main()
