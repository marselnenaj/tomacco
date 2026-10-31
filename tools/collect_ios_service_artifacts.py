#!/usr/bin/env python3
"""Read fixed original iconservices artifacts in a networkless read-only helper.

This observes selected paths, not the complete service/sandbox configuration.
Source descriptors are shared-locked and passed read-only to QEMU. Native
source signatures and source-wide hashes are not established by this reader.
"""
import argparse
import base64
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time

from ios_apfs_layout import inspect
from ios_host_pressure import host_pressure_admission, pressure_error
from prepare_ios_emulator import write_cpio
from prepare_ios_userland import open_source, snapshot


CANDIDATES = (
    ("launchd", "System/Library/LaunchDaemons/com.apple.iconservices.iconservicesagent.plist"),
    ("launchd-alternate", "System/Library/LaunchDaemons/com.apple.iconservicesagent.plist"),
    ("sandbox", "System/Library/Sandbox/Profiles/com.apple.iconservices.iconservicesagent.sb"),
    ("sandbox-alternate", "System/Library/Sandbox/Profiles/iconservicesagent.sb"),
    ("executable", "System/Library/CoreServices/iconservicesagent"),
)
MAX_FILE_BYTES = 2 << 20
MAX_LOG_BYTES = 30 << 20


def identity(info):
    return {"device": info.st_dev, "inode": info.st_ino, "bytes": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def reader_script(partition, volume):
    if type(partition) is not int or not 1 <= partition <= 128 or type(volume) is not int or not 0 <= volume <= 100:
        raise ValueError("Unsupported source System volume")
    lines = ["#!/bin/sh", "export PATH=/bin", "/bin/busybox --install -s /bin", "set -eu",
             "trap 'echo TOMACCO_SERVICE_FAILED; poweroff -f' EXIT",
             "mount -t proc proc /proc", "mount -t sysfs sys /sys", "mount -t devtmpfs dev /dev",
             "insmod /apfs.ko",
             f"mount -t apfs -o ro,vol={volume} /dev/vda{partition} /mnt/system",
             "mount -t apfs -o ro /dev/vdb /mnt/cryptex"]
    for source in ("system", "cryptex"):
        for label, relative in CANDIDATES:
            path = "/mnt/" + source
            lines.append("valid=1")
            for part in relative.split("/")[:-1]:
                path += "/" + part
                lines.append(f"if [ ! -d {path} ] || [ -L {path} ]; then valid=0; fi")
            path += "/" + relative.split("/")[-1]
            name = source + ":" + label
            lines.extend([f"if [ \"$valid\" = 1 ] && [ -f {path} ] && [ ! -L {path} ]; then",
                          f"  bytes=$(stat -c %s {path})",
                          f"  if [ \"$bytes\" -le {MAX_FILE_BYTES} ]; then",
                          f"    digest=$(sha256sum {path}); digest=${{digest%% *}}",
                          f"    echo TOMACCO_SERVICE_FILE {name} $bytes $digest", f"    base64 {path}",
                          "    echo TOMACCO_SERVICE_END", "  else",
                          f"    echo TOMACCO_SERVICE_LIMIT {name}", "  fi", "else",
                          f"  echo TOMACCO_SERVICE_UNAVAILABLE {name}", "fi"])
    lines.extend(["umount /mnt/cryptex", "umount /mnt/system", "trap - EXIT",
                  "echo TOMACCO_SERVICE_COMPLETE", "poweroff -f"])
    return "\n".join(lines) + "\n"


def collect(args):
    output = args.output
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    deadline = time.monotonic() + args.seconds
    record = {"format": 1, "scope": "original-iconservices-artifacts", "state": "preparing",
              "network_enabled": False, "source_disks_writable": False,
              "full_source_hashes_verified": False, "original_signatures_verified": False,
              "complete_service_configuration_verified": False, "files": [], "unavailable": []}

    def save():
        temporary = output / ".service-artifacts.tmp"
        with temporary.open("w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(record, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, output / "service-artifacts.json")

    save()
    try:
        record["host_pressure"] = host_pressure_admission()
        if not record["host_pressure"]["admitted"]:
            raise RuntimeError(pressure_error(record["host_pressure"]))
        with ExitStack() as locks:
            sources = []
            for path in (args.disk, args.system_cryptex):
                stream = locks.enter_context(os.fdopen(open_source(path.parent, path.name), "rb"))
                fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 64 << 30:
                    raise ValueError("Source must be a bounded nonempty regular disk image")
                sources.append((stream, identity(info)))
            if (sources[0][1]["device"], sources[0][1]["inode"]) == (sources[1][1]["device"], sources[1][1]["inode"]):
                raise ValueError("System and Cryptex sources must be distinct")
            layout = inspect(sources[0][0].fileno())
            volumes = [v for v in layout["volumes"] if v["role"] == 1 and v["sealed"]]
            if len(volumes) != 1:
                raise ValueError("Select a closed restored disk with one sealed System volume")
            record["sources"] = [{"path": str(path), "identity_before": source[1]}
                                 for path, source in zip((args.disk, args.system_cryptex), sources)]
            root = output / "helper/root"
            for directory in ("bin", "dev", "proc", "sys", "mnt/system", "mnt/cryptex"):
                (root / directory).mkdir(parents=True, exist_ok=True)
            for relative, destination in (("busybox/busybox", "bin/busybox"), ("apfs/apfs.ko", "apfs.ko")):
                snapshot(args.support, relative, root / destination, deadline)
            (root / "bin/busybox").chmod(0o755)
            (root / "bin/sh").symlink_to("busybox")
            (root / "init").write_text(reader_script(layout["partition"], volumes[0]["index"]))
            (root / "init").chmod(0o755)
            archive = output / "helper/init.cpio"
            write_cpio(root, archive)
            command = list(map(str, [args.helper_engine, "-L", args.bios, "-machine", "q35", "-accel", "kvm",
                "-cpu", "host", "-m", "1G", "-smp", "1", "-kernel", args.helper_kernel, "-initrd", archive,
                "-append", "console=ttyS0 rdinit=/init panic=-1 mitigations=off", "-display", "none",
                "-serial", "stdio", "-monitor", "none", "-nic", "none", "-no-reboot"]))
            for stream, _info in sources:
                command.extend(["-drive", f"file=/proc/self/fd/{stream.fileno()},format=raw,if=virtio,readonly=on"])
            record.update(state="reading", command=command, host_pressure_before_helper=host_pressure_admission())
            save()
            if not record["host_pressure_before_helper"]["admitted"]:
                raise RuntimeError(pressure_error(record["host_pressure_before_helper"]))
            log_path = output / "helper/helper.log"
            with log_path.open("xb") as log:
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                    pass_fds=tuple(stream.fileno() for stream, _info in sources),
                    timeout=max(0.01, deadline - time.monotonic()))
            for row, (stream, before) in zip(record["sources"], sources):
                row["identity_after"] = identity(os.fstat(stream.fileno()))
                if row["identity_after"] != before:
                    raise ValueError("Source image identity changed during read-only collection")
            data = log_path.read_bytes() if log_path.stat().st_size <= MAX_LOG_BYTES else b""
            lines = data.replace(b"\r", b"").splitlines()
            if result.returncode or lines.count(b"TOMACCO_SERVICE_COMPLETE") != 1 or b"TOMACCO_SERVICE_FAILED" in lines:
                raise RuntimeError("Read-only artifact helper did not finish; inspect helper/helper.log")
            expected = {source + ":" + label: relative for source in ("system", "cryptex") for label, relative in CANDIDATES}
            observed = set()
            index = 0
            while index < len(lines):
                parts = lines[index].split()
                index += 1
                if not parts or not parts[0].startswith(b"TOMACCO_SERVICE_"):
                    continue
                if parts[0] in (b"TOMACCO_SERVICE_UNAVAILABLE", b"TOMACCO_SERVICE_LIMIT"):
                    name = parts[1].decode("ascii")
                    if len(parts) != 2 or name not in expected or name in observed:
                        raise ValueError("Unexpected artifact availability record")
                    observed.add(name)
                    record["unavailable"].append({"name": name, "reason": parts[0].decode("ascii")})
                elif parts[0] == b"TOMACCO_SERVICE_FILE":
                    if len(parts) != 4:
                        raise ValueError("Invalid artifact file record")
                    name, size, digest = parts[1].decode("ascii"), int(parts[2]), parts[3].decode("ascii")
                    if name not in expected or name in observed or not 0 <= size <= MAX_FILE_BYTES:
                        raise ValueError("Unexpected, duplicate or oversized artifact")
                    encoded = []
                    count = 0
                    while index < len(lines) and lines[index] != b"TOMACCO_SERVICE_END":
                        count += len(lines[index])
                        if count > ((MAX_FILE_BYTES + 2) // 3) * 4:
                            raise ValueError("Encoded artifact exceeds bound")
                        encoded.append(lines[index]); index += 1
                    if index == len(lines):
                        raise ValueError("Artifact contents incomplete")
                    index += 1
                    content = base64.b64decode(b"".join(encoded), validate=True)
                    if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
                        raise ValueError("Artifact bytes disagree with the read-only helper")
                    target = output / "files" / name.replace(":", "/")
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with target.open("xb") as stream:
                        os.fchmod(stream.fileno(), 0o600); stream.write(content)
                    observed.add(name)
                    record["files"].append({"source": name.split(":")[0], "relative_path": expected[name],
                                            "path": str(target), "bytes": size, "sha256": digest})
            if observed != set(expected):
                raise ValueError("Selected artifact path coverage is incomplete")
            record.update(state="collected", source_identities_unchanged=True, selected_path_coverage_complete=True)
            save()
            return record
    except BaseException as error:
        record.update(state="failed", error=str(error)); save(); raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("disk", "system-cryptex", "support", "helper-engine", "helper-kernel", "bios", "output"):
        parser.add_argument("--" + name, type=lambda s: Path(s).expanduser().absolute(), required=True)
    parser.add_argument("--seconds", type=int, default=180)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 600:
        parser.error("seconds must be 1–600")
    result = collect(args)
    print(json.dumps({"state": result["state"], "files": result["files"], "unavailable": result["unavailable"]}))


if __name__ == "__main__":
    main()
