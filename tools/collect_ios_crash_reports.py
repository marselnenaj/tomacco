#!/usr/bin/env python3
"""Collect original reports from a stopped private iOS disk without writes.

The offline, networkless Linux helper mounts only the identified Data volume,
read-only. Missing reports do not establish that the guest did not crash.
"""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import time

from ios_apfs_layout import inspect as inspect_apfs
from prepare_ios_emulator import write_cpio
from prepare_ios_userland import open_source, snapshot

# chronod aborts at boot and MapKit's snapshot service when Maps opens.
PROCESSES = ("iconservicesagent", "locationd", "Preferences", "SpringBoard", "MTLCompilerService",
             "chronod", "com.apple.MapKit.SnapshotService")
MAX_FILES = 5
MAX_BYTES = 4 << 20
MAX_LOG_BYTES = 30 << 20


def script(partition, volume, processes):
    if type(partition) is not int or not 1 <= partition <= 128 or type(volume) is not int or not 0 <= volume < 100:
        raise ValueError("Unsupported Data volume location")
    if not processes or any(name not in PROCESSES for name in processes):
        raise ValueError("Select a supported native process")
    patterns = " | ".join(name + "*.ips | " + name + "*.crash" for name in processes)
    return f"""#!/bin/sh
export PATH=/bin
/bin/busybox --install -s /bin
set -eu
trap 'dmesg | tail -n 40; echo TOMACCO_CRASH_FAILED; poweroff -f' EXIT
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
insmod /apfs.ko
mount -t apfs -o ro,vol={volume} /dev/vda{partition} /mnt/data
: > /candidates
scanned=0
for owner in mobile root; do
  valid=1
  current=/mnt/data
  for part in "$owner" Library Logs CrashReporter; do
    current="$current/$part"
    if [ ! -d "$current" ] || [ -L "$current" ]; then valid=0; break; fi
  done
  if [ "$valid" = 0 ]; then echo "TOMACCO_CRASH_DIRECTORY_UNAVAILABLE $owner"; continue; fi
  echo "TOMACCO_CRASH_DIRECTORY_READ $owner"
  for file in "$current"/*; do
    scanned=$((scanned + 1))
    if [ "$scanned" -gt 4096 ]; then echo TOMACCO_CRASH_SCAN_LIMIT; break; fi
    [ -f "$file" ] && [ ! -L "$file" ] || continue
    name=${{file##*/}}
    case "$name" in {patterns}) ;; *) continue ;; esac
    case "$name" in *[!a-zA-Z0-9._-]*) echo TOMACCO_CRASH_UNSUPPORTED_NAME; continue ;; esac
    printf '%s/%s\\n' "$owner" "$name" >> /candidates
  done
done
LC_ALL=C sort -r /candidates > /sorted
n=0
while IFS= read -r entry; do
  if [ "$n" -ge {MAX_FILES} ]; then echo TOMACCO_CRASH_FILE_LIMIT; break; fi
  owner=${{entry%%/*}}
  name=${{entry#*/}}
  file=/mnt/data/$owner/Library/Logs/CrashReporter/$name
  bytes=$(stat -c %s "$file")
  if [ "$bytes" -gt {MAX_BYTES} ]; then echo "TOMACCO_CRASH_OVERSIZE $entry $bytes"; continue; fi
  digest=$(sha256sum "$file"); digest=${{digest%% *}}
  echo "TOMACCO_CRASH_FILE $entry $bytes $digest"
  base64 "$file"
  echo TOMACCO_CRASH_END
  n=$((n + 1))
done < /sorted
umount /mnt/data
trap - EXIT
echo TOMACCO_CRASH_COMPLETE
poweroff -f
"""


def decode(log, output, processes):
    if len(log) > MAX_LOG_BYTES:
        raise ValueError("Helper log exceeds the collection bound")
    lines = log.replace(b"\r", b"").splitlines()
    if lines.count(b"TOMACCO_CRASH_COMPLETE") != 1 or b"TOMACCO_CRASH_FAILED" in lines:
        raise RuntimeError("Read-only collection did not complete; inspect helper/helper.log")
    records, directories, limitations = [], [], []
    seen = set()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.startswith(b"TOMACCO_CRASH_DIRECTORY_"):
            parts = line.decode("ascii").split()
            if len(parts) != 2 or parts[1] not in ("mobile", "root"):
                raise ValueError("Malformed directory receipt")
            directories.append({"owner": parts[1], "read": parts[0] == "TOMACCO_CRASH_DIRECTORY_READ"})
        elif line.startswith(b"TOMACCO_CRASH_FILE "):
            match = re.fullmatch(rb"TOMACCO_CRASH_FILE (mobile|root)/([A-Za-z0-9._-]+) ([0-9]+) ([0-9a-f]{64})", line)
            if match is None or len(records) >= MAX_FILES:
                raise ValueError("Malformed or excessive crash report receipt")
            owner, name, size, digest = (value.decode("ascii") for value in match.groups())
            if not any(name.startswith(p) for p in processes) or not name.endswith((".ips", ".crash")):
                raise ValueError("Unexpected report name")
            key = owner + "/" + name
            if key in seen or int(size) > MAX_BYTES:
                raise ValueError("Duplicate or oversized report")
            seen.add(key)
            chunks = []
            encoded_bytes = 0
            while index < len(lines) and lines[index] != b"TOMACCO_CRASH_END":
                encoded_bytes += len(lines[index])
                if encoded_bytes > ((MAX_BYTES + 2) // 3) * 4:
                    raise ValueError("Encoded report exceeds bounds")
                chunks.append(lines[index])
                index += 1
            if index == len(lines):
                raise ValueError("Incomplete crash report")
            index += 1
            data = base64.b64decode(b"".join(chunks), validate=True)
            if len(data) != int(size) or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("Original report bytes do not match the helper receipt")
            target = output / "reports" / owner / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open("xb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
            records.append({"path": str(target), "guest_path": "/private/var/" + owner +
                            "/Library/Logs/CrashReporter/" + name, "bytes": len(data), "sha256": digest})
        elif line.startswith((b"TOMACCO_CRASH_OVERSIZE ", b"TOMACCO_CRASH_FILE_LIMIT", b"TOMACCO_CRASH_SCAN_LIMIT", b"TOMACCO_CRASH_UNSUPPORTED_NAME")):
            limitations.append(line.decode("ascii"))
    if sorted(row["owner"] for row in directories) != ["mobile", "root"]:
        raise ValueError("Incomplete directory coverage receipt")
    return {"reports": records, "directories": directories, "limitations": limitations,
            "search_complete_within_selected_directories": all(row["read"] for row in directories) and not limitations,
            "absence_proves_no_crash": False, "selection_order": "descending owner/filename; not native crash time"}


def identity(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def digest_fd(fd, size, deadline):
    digest = hashlib.sha256()
    position = 0
    while position < size:
        if time.monotonic() >= deadline:
            raise TimeoutError("Crash collection deadline exceeded")
        data = os.pread(fd, min(4 << 20, size - position), position)
        if not data:
            raise ValueError("Input disk was truncated")
        digest.update(data)
        position += len(data)
    return digest.hexdigest()


def collect(args):
    output = args.output.absolute()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    deadline = time.monotonic() + args.seconds
    record = {"format": 1, "scope": "original-ios-crash-reports", "state": "collecting",
              "processes": args.process, "disk_writable": False, "guest_boot_attempted": False,
              "network_enabled": False, "max_files": MAX_FILES, "max_bytes_per_file": MAX_BYTES,
              "input_bytes_unchanged_verified": False}

    def save():
        (output / "crash-reports.json").write_text(json.dumps(record, indent=2) + "\n")

    save()
    try:
        with os.fdopen(open_source(args.disk.parent, args.disk.name), "rb") as disk:
            fd = disk.fileno()
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 64 << 30:
                raise ValueError("Select a bounded regular raw disk")
            layout = inspect_apfs(fd)
            volumes = [row for row in layout["volumes"] if row["role"] == 0x40 and not row["sealed"]]
            if len(volumes) != 1:
                raise ValueError("Expected exactly one unsealed APFS Data volume")
            record.update(disk=str(args.disk.absolute()), disk_bytes=before.st_size, layout=layout,
                          data_volume=volumes[0], disk_sha256_before=digest_fd(fd, before.st_size, deadline))
            save()
            root = output / "helper/root"
            for name in ("bin", "dev", "proc", "sys", "mnt/data"):
                (root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
            for relative, destination in (("busybox/busybox", "bin/busybox"), ("apfs/apfs.ko", "apfs.ko")):
                snapshot(args.support, relative, root / destination, deadline)
            (root / "bin/busybox").chmod(0o755)
            (root / "bin/sh").symlink_to("busybox")
            (root / "init").write_text(script(layout["partition"], volumes[0]["index"], args.process))
            (root / "init").chmod(0o755)
            archive = output / "helper/init.cpio"
            write_cpio(root, archive)
            command = list(map(str, [args.helper_engine, "-L", args.bios, "-machine", "q35", "-accel", args.accel,
                "-cpu", "host" if args.accel == "kvm" else "max", "-m", "1G", "-smp", "1",
                "-kernel", args.helper_kernel, "-initrd", archive,
                "-append", "console=ttyS0 rdinit=/init panic=-1 mitigations=off",
                "-drive", f"file=/proc/self/fd/{fd},format=raw,if=virtio,readonly=on",
                "-display", "none", "-serial", "stdio", "-monitor", "none", "-nic", "none", "-no-reboot"]))
            (output / "helper/command.json").write_text(json.dumps(command, indent=2) + "\n")
            (output / "helper/command.sh").write_text(shlex.join(command) + "\n")
            log_path = output / "helper/helper.log"
            with log_path.open("xb") as log:
                result = subprocess.run(command, pass_fds=(fd,), stdout=log, stderr=subprocess.STDOUT,
                                        timeout=max(0.01, deadline - time.monotonic()))
            record["helper_exit_code"] = result.returncode
            record["disk_sha256_after"] = digest_fd(fd, before.st_size, deadline)
            if identity(before) != identity(os.fstat(fd)) or record["disk_sha256_before"] != record["disk_sha256_after"]:
                raise ValueError("Input disk changed during collection")
            record["input_bytes_unchanged_verified"] = True
            save()
            if result.returncode:
                raise RuntimeError("Read-only helper failed; inspect helper/helper.log")
            with log_path.open("rb") as log:
                record.update(decode(log.read(MAX_LOG_BYTES + 1), output, args.process))
            record["state"] = "collected"
            save()
    except BaseException as error:
        record.update(state="cancelled" if isinstance(error, KeyboardInterrupt) else "failed", error=str(error))
        save()
        raise
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("disk", "support", "helper-engine", "helper-kernel", "bios", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--process", action="append", choices=PROCESSES, required=True)
    parser.add_argument("--seconds", type=int, choices=range(30, 1801), metavar="30..1800", default=300)
    parser.add_argument("--accel", choices=("kvm", "tcg"), default="kvm")
    args = parser.parse_args()
    collect(args)


if __name__ == "__main__":
    main()
