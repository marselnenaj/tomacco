#!/usr/bin/env python3
"""Prepare a private vphone restore RAMdisk with bounded format/reopen retries.

Supports only restored_external from PCC 23E5207q, guarded by its exact digest.
Extracts and injects through a networkless Linux helper VM. Originals remain
unchanged; the output daemon is explicitly modified and ad-hoc signed. No
manifest, kernel or SEP is generated or modified. An explicit research-seal
option substitutes SHA-384 pins for authentication of two sealing objects only;
the default preserves the original authentication policy.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import struct
import subprocess
import uuid

from prepare_ios_emulator import write_cpio
from ios_paravirt_seal import SEAL_CALLS, SEAL_CAVE, ORIGINAL_VERIFIER, assemble_adapter, prepare_policy

SOURCE_SHA256 = "8b8ea670756d1ddb23c6cfaee6c20eec3ec1739f4922a3c277f7f7b5b4620cb1"
CALLSITE, CAVE, TEXT_END = 0x29c50, 0x2cda10, 0x2d0000
STUBS = {"_ioctl": 0x1b04f0, "___error": 0x1af4b0, "_usleep": 0x1b17e0,
         "_printf": 0x1b0e90, "_fcntl": 0x1b0060, "_open": 0x1b0ca0, "_close": 0x1afc80}
INTEGRATION = Path(__file__).resolve().parents[1] / "integrations/ios-emulator"
INIT = """#!/bin/sh
export PATH=/bin
/bin/busybox --install -s /bin
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
set -eu
trap 'echo TOMACCO_RESTORE_PREPARE_FAILED; sync; poweroff -f' EXIT
insmod /apfs.ko
__ACTION__
umount /mnt
trap - EXIT
echo TOMACCO_RESTORE_PREPARE_COMPLETE
poweroff -f
"""


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def branch(source, target):
    delta = target - source
    if delta % 4 or not -(1 << 27) <= delta < (1 << 27):
        raise ValueError("Restore adapter branch exceeds ARM64 range")
    return struct.pack("<I", 0x94000000 | ((delta >> 2) & 0x3ffffff))


def patch_binary(original, adapter, seal_adapter=None):
    if hashlib.sha256(original).hexdigest() != SOURCE_SHA256:
        raise ValueError("Unsupported restored_external: requires unmodified PCC 23E5207q")
    if (not 128 <= len(adapter) <= 4096 or CAVE + len(adapter) > TEXT_END
            or original[CAVE:CAVE + len(adapter)] != bytes(len(adapter))
            or original[CALLSITE:CALLSITE + 4] != branch(CALLSITE, STUBS["_ioctl"])):
        raise ValueError("Restore adapter does not fit the guarded executable layout")
    result = bytearray(original)
    result[CALLSITE:CALLSITE + 4] = branch(CALLSITE, CAVE)
    result[CAVE:CAVE + len(adapter)] = adapter
    if seal_adapter is not None:
        if (not 128 <= len(seal_adapter) <= TEXT_END - SEAL_CAVE
                or CAVE + len(adapter) > SEAL_CAVE
                or original[SEAL_CAVE:SEAL_CAVE + len(seal_adapter)] != bytes(len(seal_adapter))):
            raise ValueError("Research seal adapter does not fit the guarded layout")
        for callsite in SEAL_CALLS:
            if original[callsite:callsite + 4] != branch(callsite, ORIGINAL_VERIFIER):
                raise ValueError("Unexpected original sealing verifier callsite")
            result[callsite:callsite + 4] = branch(callsite, SEAL_CAVE)
        result[SEAL_CAVE:SEAL_CAVE + len(seal_adapter)] = seal_adapter
    return bytes(result)


def extend_trustcache(data, cdhashes):
    if not 24 <= len(data) <= 16 * 1024 * 1024:
        raise ValueError("Invalid restore trust cache size")
    version, _identifier, count = struct.unpack("<I16sI", data[:24])
    if version != 1 or len(data) != 24 + count * 22:
        raise ValueError("Expected a complete version 1 restore trust cache")
    entries = {data[offset:offset + 22] for offset in range(24, len(data), 22)}
    if len(entries) != count or not cdhashes or any(not re.fullmatch(r"[0-9a-fA-F]{40}", h) for h in cdhashes):
        raise ValueError("Invalid trust cache entries or ad-hoc code-directory hashes")
    entries.update(bytes.fromhex(h) + bytes([2, 0]) for h in cdhashes)
    return struct.pack("<I16sI", 1, uuid.uuid4().bytes, len(entries)) + b"".join(sorted(entries))


def run(command):
    try:
        return subprocess.run(list(map(str, command)), check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=90).stdout
    except subprocess.CalledProcessError as error:
        detail = error.stderr[-4096:].decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"{Path(command[0]).name} exited {error.returncode}: {detail}") from error


def save(output, record, phase=None):
    if phase is not None:
        record["phase"] = phase
    temporary = output / ".restore-runtime.json.tmp"
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(record, stream, indent=2)
        stream.write("\n")
    os.replace(temporary, output / "restore-runtime.json")


def helper(args, directory, ramdisk, action, *, writable=False, payload=None):
    directory.mkdir(mode=0o700)
    image = directory / "root"
    image.mkdir()
    for name in ("bin", "dev", "mnt", "proc", "sys"):
        (image / name).mkdir()
    shutil.copyfile(args.support / "busybox/busybox", image / "bin/busybox")
    (image / "bin/busybox").chmod(0o755)
    (image / "bin/sh").symlink_to("busybox")
    shutil.copyfile(args.support / "apfs/apfs.ko", image / "apfs.ko")
    if payload is not None:
        shutil.copyfile(payload, image / "restored_external")
    (image / "init").write_text(INIT.replace("__ACTION__", action))
    (image / "init").chmod(0o755)
    archive = directory / "helper.cpio"
    write_cpio(image, archive)
    command = [args.helper_engine, "-L", args.bios, "-machine", "q35", "-accel", args.accel,
               "-cpu", "host" if args.accel == "kvm" else "max", "-m", "1G", "-smp", "2",
               "-kernel", args.helper_kernel, "-initrd", archive,
               "-append", "console=ttyS0 rdinit=/init panic=-1 mitigations=off",
               "-drive", f"file={str(ramdisk).replace(',', ',,')},format=raw,if=virtio,readonly={'off' if writable else 'on'}",
               "-display", "none", "-serial", "stdio", "-monitor", "none", "-nic", "none", "-no-reboot"]
    (directory / "command.json").write_text(json.dumps(list(map(str, command)), indent=2) + "\n")
    log = directory / "helper.log"
    with log.open("xb") as output:
        result = subprocess.run(list(map(str, command)), stdout=output, stderr=output, timeout=90)
    if log.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Restore helper exceeded its output bound")
    data = log.read_bytes().replace(b"\r", b"")
    if result.returncode or b"TOMACCO_RESTORE_PREPARE_COMPLETE\n" not in data or b"TOMACCO_RESTORE_PREPARE_FAILED\n" in data:
        raise RuntimeError(f"Restore helper failed; inspect {log}")
    return data


def prepare(args, record):
    output = args.output
    save(output, record, "validate-inputs")
    policy = None
    if args.research_seal_manifest:
        policy = prepare_policy(args.research_seal_firmware, args.research_seal_identity,
                                args.research_seal_manifest, args.research_seal_overrides,
                                args.research_seal_userland)
        record["research_seal_policy"] = policy
        save(output, record)
    for name in ("ramdisk", "trustcache", "helper_engine", "helper_kernel"):
        path = getattr(args, name)
        if not path.is_file() or not 0 < path.stat().st_size <= 2 << 30:
            raise ValueError(f"Missing or oversized preparation input: {name}")
        record[name + "_input"] = {"path": str(path), "sha256": digest(path)}
    for name in ("ldid/ldid", "busybox/busybox", "apfs/apfs.ko"):
        if not (args.support / name).is_file():
            raise ValueError(f"Missing local preparation support: {name}")
    action = ("mount -t apfs -o ro /dev/vda /mnt\n"
              f"echo '{SOURCE_SHA256}  /mnt/usr/local/bin/restored_external' | sha256sum -c -\n"
              "echo TOMACCO_RESTORE_BINARY_BEGIN\nbase64 /mnt/usr/local/bin/restored_external\n"
              "echo TOMACCO_RESTORE_BINARY_END")
    save(output, record, "extract-original")
    log = helper(args, output / "extract", args.ramdisk, action)
    match = re.search(rb"(?:^|\n)TOMACCO_RESTORE_BINARY_BEGIN\n([A-Za-z0-9+/=\n]+)TOMACCO_RESTORE_BINARY_END\n", log)
    if match is None:
        raise ValueError("Restore helper returned no binary")
    original = base64.b64decode(match[1].replace(b"\n", b""), validate=True)
    save(output, record, "assemble-adapter")
    source = INTEGRATION / "restore-format-retry.S"
    run(["llvm-mc", "-triple=aarch64", "-mattr=+pauth,+bti", "-filetype=obj", source, "-o", output / "adapter.o"])
    relocations = run(["llvm-objdump", "-r", output / "adapter.o"]).decode()
    run(["llvm-objcopy", "-O", "binary", "--only-section=.text", output / "adapter.o", output / "adapter.bin"])
    adapter = bytearray((output / "adapter.bin").read_bytes())
    references = re.findall(r"^([0-9a-fA-F]+)\s+(R_\S+)\s+(\S+)\s*$", relocations, re.MULTILINE)
    if len(references) != 11:
        raise ValueError("Unexpected restore adapter relocation count")
    seen = set()
    for location, kind, symbol in references:
        offset = int(location, 16)
        if (kind != "R_AARCH64_CALL26" or symbol not in STUBS or offset in seen
                or offset % 4 or adapter[offset:offset + 4] != bytes.fromhex("00000094")):
            raise ValueError("Unexpected restore adapter relocation")
        seen.add(offset)
        adapter[offset:offset + 4] = branch(CAVE + offset, STUBS[symbol])
    (output / "adapter.bin").write_bytes(adapter)
    seal_adapter = None
    if policy is not None:
        save(output, record, "assemble-research-seal-adapter")
        seal_source = INTEGRATION / "restore-seal-pins.S"
        seal_adapter = assemble_adapter(output, policy["pins"], seal_source, run)
        policy.update(adapter_sha256=hashlib.sha256(seal_adapter).hexdigest(),
                      adapter_source_sha256=digest(seal_source), adapter_bytes=len(seal_adapter))
    original_file = output / "original-restored_external"
    original_file.write_bytes(original)
    binary = output / "restored_external"
    binary.write_bytes(patch_binary(original, adapter, seal_adapter))
    binary.chmod(0o755)
    ldid = args.support / "ldid/ldid"
    before = plistlib.loads(run([ldid, "-e", original_file]))
    run([ldid, "-Cadhoc", "-M", "-S", binary])
    if plistlib.loads(run([ldid, "-e", binary])) != before:
        raise ValueError("Restore entitlements changed while signing")
    hashes = re.findall(r"CDHash=([0-9a-fA-F]{40})", run([ldid, "-h", binary]).decode())
    tc = output / "ramdisk.tc"
    tc.write_bytes(extend_trustcache(args.trustcache.read_bytes(), hashes))
    ramdisk = output / "ramdisk.dmg"
    run(["cp", "--reflink=auto", "--sparse=always", "--", args.ramdisk, ramdisk])
    binary_hash = digest(binary)
    action = ("mount -t apfs -o readwrite /dev/vda /mnt\n"
              f"echo '{SOURCE_SHA256}  /mnt/usr/local/bin/restored_external' | sha256sum -c -\n"
              "cp /restored_external /mnt/usr/local/bin/restored_external\n"
              "chown 0:0 /mnt/usr/local/bin/restored_external\nchmod 755 /mnt/usr/local/bin/restored_external\n"
              f"echo '{binary_hash}  /mnt/usr/local/bin/restored_external' | sha256sum -c -\nsync")
    save(output, record, "inject-private-copy")
    helper(args, output / "inject", ramdisk, action, writable=True, payload=binary)
    save(output, record, "verify-inputs-and-outputs")
    for name in ("ramdisk", "trustcache", "helper_engine", "helper_kernel"):
        if digest(getattr(args, name)) != record[name + "_input"]["sha256"]:
            raise ValueError(f"Preparation input changed: {name}")
    record.update(state="prepared", phase="complete", source_binary_sha256=SOURCE_SHA256,
                  patched_binary_sha256=binary_hash, adapter_sha256=digest(output / "adapter.bin"),
                  adapter_source_sha256=digest(source), adapter_bytes=len(adapter),
                  entitlements_preserved=True, cdhashes=hashes,
                  retry={"errno": 16, "maximum_attempts": 50, "delay_microseconds": 10000,
                         "reopens_device": True, "live_descriptor_register": "x19"},
                  components={"ramdisk": {"path": str(ramdisk), "sha256": digest(ramdisk)},
                              "trustcache": {"path": str(tc), "sha256": digest(tc)}})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("ramdisk", "trustcache", "support", "helper-engine", "helper-kernel", "bios", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--accel", choices=("kvm", "tcg"), default="kvm")
    parser.add_argument("--research-seal-manifest", type=Path,
                        help="OPT-IN: replace Apple authentication for TWO exact sealing objects with SHA-384 pins")
    parser.add_argument("--research-seal-firmware", type=Path, help="Matching original PCC 23E5207q firmware directory")
    parser.add_argument("--research-seal-identity", type=int, help="Explicit build identity for the research seal pins")
    parser.add_argument("--research-seal-override", action="append", default=[], metavar="COMPONENT=PATH")
    parser.add_argument("--research-seal-userland", type=Path,
                        help="OPT-IN: pin the imported original iPhone 24A446 sealing components instead of PCC")
    args = parser.parse_args(argv)
    research = (args.research_seal_manifest, args.research_seal_firmware, args.research_seal_identity)
    if any(value is not None for value in research) and not all(value is not None for value in research):
        parser.error("Research sealing requires manifest, firmware and identity together")
    if args.research_seal_userland and not args.research_seal_manifest:
        parser.error("iPhone sealing inputs require explicit research-seal opt-in")
    args.research_seal_overrides = {}
    for entry in args.research_seal_override:
        name, separator, path = entry.partition("=")
        if not args.research_seal_manifest or not separator or not name or not path or name in args.research_seal_overrides:
            parser.error("Research seal overrides require opt-in and unique COMPONENT=PATH entries")
        args.research_seal_overrides[name] = path
    for name in ("ramdisk", "trustcache", "support", "helper_engine", "helper_kernel", "bios", "output"):
        setattr(args, name, getattr(args, name).expanduser().absolute())
    args.output.mkdir(mode=0o700)
    record = {"format": 1, "state": "preparing", "profile": "vphone600-23E5207q-format-reopen",
              "daemon_modified": True, "kernel_modified": False, "authentication_checks_modified": False,
              "external_requests": False, "macos_vm_used": False, "qualified_for_app_development": False}
    if args.research_seal_manifest:
        record.update(profile="vphone600-23E5207q-local-seal-pins", authentication_checks_modified=True,
                      apple_signature_verified=False)
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        prepare(args, record)
    except KeyboardInterrupt:
        record.update(state="cancelled", error="Restore preparation interrupted")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        record.update(state="failed", error=str(error))
    finally:
        signal.signal(signal.SIGTERM, previous)
        receipt = args.output / "restore-runtime.json"
        save(args.output, record)
    print(json.dumps({"state": record["state"], "receipt": str(receipt)} |
                     ({"error": record["error"]} if "error" in record else {})))
    return 0 if record["state"] == "prepared" else 1


if __name__ == "__main__":
    raise SystemExit(main())
