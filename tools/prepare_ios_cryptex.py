#!/usr/bin/env python3
"""Install original Cryptex files into a private restored iPhone disk copy.

The sealed System volume is never mounted writable. A networkless Linux helper
copies both original Cryptex filesystems into the unsealed Preboot volume and
checks every regular file against its source. Original version-2 trust-cache
entries are combined for this virtual guest's explicit boot inputs. Optional
kernel, boot-manifest and device-tree inputs select the version-pinned research
graft policy. Apple files, source disks and host security settings stay intact.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import struct
import subprocess
import time

from ios_apfs_layout import inspect as inspect_apfs
from ios_paravirt_firmware import der_fields, read_component, retag_im4p
from ios_paravirt_kernel import prepare_boot
from ios_paravirt_graphics import prepare_graphics, helper_script as graphics_script
from ios_guest_tools import development_library
from prepare_ios_emulator import write_cpio
from prepare_ios_userland import import_image, manifest, relative_path, snapshot

CACHES = ("StaticTrustCache", "Cryptex1,SystemTrustCache", "Cryptex1,AppTrustCache")

# Original libignition checks this sibling of Cryptexes before opening the
# canonical System/Cryptexes/OS path. Installing the files alone is insufficient:
# a missing directory returns ENOEXEC before dyld even looks for its cache.
IGNITION_LAYOUT_SHELL = """prepare_ignition_layout() {
  ignition_path="$1/cryptex1"
  if [ -L "$ignition_path" ] || { [ -e "$ignition_path" ] && [ ! -d "$ignition_path" ]; }; then
    echo "Cryptex ignition path is not an ordinary directory: $ignition_path" >&2
    return 1
  fi
  mkdir -p -m 755 "$ignition_path"
}
"""

GRAFT_DESTINATION_SHELL = """prepare_graft_destination() {
  graft_path="$1/cryptex1/current"
  if [ -L "$graft_path" ] || { [ -e "$graft_path" ] && [ ! -d "$graft_path" ]; }; then
    echo "Cryptex graft destination is not an ordinary directory: $graft_path" >&2
    return 1
  fi
  if [ -d "$graft_path" ]; then
    first_entry=$(find "$graft_path" -mindepth 1 -maxdepth 1 -print -quit) || return 1
    if [ -n "$first_entry" ]; then
      echo "Cryptex graft destination is not empty: $graft_path" >&2
      return 1
    fi
  fi
  mkdir -p -m 755 "$graft_path"
}
"""

DESTINATION_SHELL = """prepare_destination() {
  cryptex_path="$1"
  for target_path in "$cryptex_path" "$cryptex_path/OS" "$cryptex_path/App"; do
    if [ -L "$target_path" ] || { [ -e "$target_path" ] && [ ! -d "$target_path" ]; }; then
      echo "Cryptex destination is not an ordinary directory: $target_path" >&2
      return 1
    fi
  done
  for target_path in "$cryptex_path/OS" "$cryptex_path/App"; do
    if [ -d "$target_path" ]; then
      first_entry=$(find "$target_path" -mindepth 1 -maxdepth 1 -print -quit) || return 1
      if [ -n "$first_entry" ]; then
        echo "Cryptex destination is not empty: $target_path" >&2
        return 1
      fi
    fi
  done
  mkdir -p -m 755 "$cryptex_path" "$cryptex_path/OS" "$cryptex_path/App"
}
"""


def combine_caches(root, entries):
    records, unique = [], {}
    for name in CACHES:
        entry = entries[name]
        relative = str(Path("components") / relative_path(entry["Info"]["Path"]))
        source = read_component(root, relative)
        normalized, _old, _new = retag_im4p(source, entry["Info"].get("Img4PayloadType"))
        if hashlib.sha384(normalized).digest() != entry["Digest"]:
            raise ValueError(f"Cryptex trust cache differs from original manifest: {name}")
        field = der_fields(normalized)[3]
        cache = normalized[field[1]:field[2]]
        if len(cache) < 24:
            raise ValueError("Truncated Cryptex trust cache")
        version, _uuid, count = struct.unpack_from("<I16sI", cache)
        if version != 2 or len(cache) != 24 + count * 24 or count > 100000:
            raise ValueError("Expected a complete original version-2 trust cache")
        seen = set()
        for offset in range(24, len(cache), 24):
            item = cache[offset:offset + 24]
            if item[:20] in seen or (item[:20] in unique and unique[item[:20]] != item):
                raise ValueError("Duplicate or conflicting original trust-cache entry")
            seen.add(item[:20])
            unique[item[:20]] = item
        records.append({"component": name, "source_sha256": hashlib.sha256(source).hexdigest(),
                        "entries": count, "manifest_digest_matched": True})
    data = b"".join(sorted(unique.values()))
    combined = struct.pack("<I16sI", 2, hashlib.sha256(data).digest()[:16], len(unique)) + data
    return combined, {"sources": records, "entries": len(unique),
                      "original_entry_bytes_preserved": True, "signature_verified": False}


def preboot_volume(layout, expected_root_hash=None):
    system = [v for v in layout["volumes"] if v["role"] == 1]
    preboot = [v for v in layout["volumes"] if v["role"] == 16]
    if (layout["partition"] is None or len(system) != 1 or not system[0]["sealed"]
            or not system[0]["snapshots"] or len(preboot) != 1 or preboot[0]["sealed"]):
        raise ValueError("Requires a restored GPT disk with one sealed System snapshot and one unsealed Preboot volume")
    if expected_root_hash is not None:
        integrity = system[0].get("integrity") or {}
        if (integrity.get("hash_type") != 1 or integrity.get("seal_broken") is not False
                or integrity.get("root_hash") != expected_root_hash):
            raise ValueError("Restored System seal does not match the selected original iPhone userland")
    return preboot[0]


def original_root_hash(root, entries):
    entry = entries["SystemVolume"]
    relative = str(Path("components") / relative_path(entry["Info"]["Path"]))
    source = read_component(root, relative, 65536)
    normalized, _old, tag = retag_im4p(source, entry["Info"].get("Img4PayloadType"))
    if tag != "isys" or hashlib.sha384(normalized).digest() != entry["Digest"]:
        raise ValueError("System root hash differs from the original iPhone manifest")
    field = der_fields(normalized)[3]
    payload = normalized[field[1]:field[2]]
    if len(payload) != 208 or struct.unpack_from("<4I", payload) != (2, 0, 1, 32):
        raise ValueError("Expected an original v2 SHA-256 SystemVolume root hash")
    return payload[16:48].hex(), source


def save(output, record):
    path = output / ".cryptex.json.tmp"
    with path.open("w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(record, stream, indent=2)
        stream.write("\n")
    os.replace(path, output / "cryptex.json")


def graft_script(output, root, policy, images, deadline):
    """Stage bounded metadata and check the complete original images in Preboot."""
    if policy is None:
        return "", ""
    objects = root / "objects"
    objects.mkdir(mode=0o700)
    checksums = []
    records = {**policy["objects"], policy["manifest"]["name"]: policy["manifest"]}
    for name, record in records.items():
        # The generated policy only contains fixed metadata names and an ECID
        # encoded as hex. Keep this boundary explicit before shell generation.
        if re.fullmatch(r"(?:os|app)\.dmg\.(?:root_hash|trustcache)|apticket\.vphone600ap\.[0-9A-F]+\.im4m", name) is None:
            raise ValueError("Invalid Cryptex graft object name")
        snapshot(output / "graft-objects", name, objects / name, deadline)
        if hashlib.sha256((objects / name).read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError("Cryptex metadata changed after kernel preparation")
        checksums.append(f"{record['sha256']}  {name}\n")
    for component, name in (("Cryptex1,SystemOS", "os.dmg"), ("Cryptex1,AppOS", "app.dmg")):
        digest = images[component]["sha256"]
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("Invalid verified Cryptex image checksum")
        checksums.append(f"{digest}  {name}\n")
    (root / "graft.sha256").write_text("".join(checksums))
    check = GRAFT_DESTINATION_SHELL + "\nprepare_graft_destination /mnt/preboot\n"
    install = """phase copy-graft-images
cp -a /objects/. /mnt/preboot/cryptex1/current/
dd if=/dev/vdb of=/mnt/preboot/cryptex1/current/os.dmg bs=4M
dd if=/dev/vdc of=/mnt/preboot/cryptex1/current/app.dmg bs=4M
phase verify-graft-images
cd /mnt/preboot/cryptex1/current
sha256sum -c /graft.sha256
cd /
echo TOMACCO_CRYPTEX_GRAFT_VERIFIED
"""
    return check, install


def helper(args, output, layout, preboot, deadline, policy=None, images=None, graphics=None):
    root = output / "helper/root"
    root.mkdir(mode=0o700, parents=True)
    for name in ("bin", "dev", "proc", "sys", "tmp", "mnt/preboot", "mnt/os", "mnt/app"):
        (root / name).mkdir(parents=True, exist_ok=True)
    for source, relative in [(args.support / "busybox/busybox", "bin/busybox"),
                              (args.support / "apfs/apfs.ko", "apfs.ko")]:
        snapshot(source.parent, source.name, root / relative, deadline)
    (root / "bin/busybox").chmod(0o755)
    (root / "bin/sh").symlink_to("busybox")
    graft_check, graft_install = graft_script(output, root, policy, images, deadline)
    graphics_check, graphics_install = graphics_script(output, root, graphics)
    script = f"""#!/bin/sh
export PATH=/bin
/bin/busybox --install -s /bin
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
set -eu
phase() {{ stage="$1"; echo "TOMACCO_CRYPTEX_PHASE:$stage"; }}
phase load-apfs
trap 'echo TOMACCO_CRYPTEX_FAILED:$stage; sync; poweroff -f' EXIT
insmod /apfs.ko
phase mount-preboot
mount -t apfs -o readwrite,vol={preboot['index']} /dev/vda{layout['partition']} /mnt/preboot
phase check-destination
{DESTINATION_SHELL}
prepare_destination /mnt/preboot/Cryptexes
{IGNITION_LAYOUT_SHELL}
prepare_ignition_layout /mnt/preboot
{graft_check}
{graphics_check}
phase mount-sources
mount -t apfs -o ro /dev/vdb /mnt/os
mount -t apfs -o ro /dev/vdc /mnt/app
phase copy-files
cp -a /mnt/os/. /mnt/preboot/Cryptexes/OS/
cp -a /mnt/app/. /mnt/preboot/Cryptexes/App/
test -f /mnt/preboot/Cryptexes/OS/System/Library/Caches/com.apple.dyld/dyld_shared_cache_arm64e
echo TOMACCO_CRYPTEX_FILES_COPIED
phase verify-files
cd /mnt/os
find . -type f -exec sha256sum '{{}}' + > /tmp/os.sha256
cd /mnt/preboot/Cryptexes/OS
sha256sum -c /tmp/os.sha256 > /tmp/os.verify
cd /mnt/app
find . -type f -exec sha256sum '{{}}' + > /tmp/app.sha256
cd /mnt/preboot/Cryptexes/App
sha256sum -c /tmp/app.sha256 > /tmp/app.verify
cd /
echo TOMACCO_OS_HASHES_BEGIN
base64 /tmp/os.sha256
echo TOMACCO_OS_HASHES_END
echo TOMACCO_APP_HASHES_BEGIN
base64 /tmp/app.sha256
echo TOMACCO_APP_HASHES_END
{graft_install}
{graphics_install}
sync
phase unmount
umount /mnt/app
umount /mnt/os
umount /mnt/preboot
trap - EXIT
echo TOMACCO_CRYPTEX_COMPLETE
poweroff -f
"""
    (root / "init").write_text(script)
    (root / "init").chmod(0o755)
    archive = output / "helper/init.cpio"
    write_cpio(root, archive)
    command = [args.helper_engine, "-L", args.bios, "-machine", "q35", "-accel", args.accel,
               "-cpu", "host" if args.accel == "kvm" else "max", "-m", "1G", "-smp", "1",
               "-kernel", args.helper_kernel, "-initrd", archive,
               "-append", "console=ttyS0 rdinit=/init panic=-1 mitigations=off"]
    for path, readonly in [(output / "system.raw", "off"),
                           (output / "images/system-cryptex.dmg", "on"),
                           (output / "images/app-cryptex.dmg", "on")]:
        command += ["-drive", f"file={str(path).replace(',', ',,')},format=raw,if=virtio,readonly={readonly}"]
    command += ["-display", "none", "-serial", "stdio", "-monitor", "none", "-nic", "none", "-no-reboot"]
    command = list(map(str, command))
    (output / "helper/command.json").write_text(json.dumps(command, indent=2) + "\n")
    log_path = output / "helper/helper.log"
    with log_path.open("xb") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                timeout=max(0.01, deadline - time.monotonic()))
    with log_path.open("rb") as log:
        data = log.read((4 << 20) + 1).replace(b"\r", b"")
    if (result.returncode or len(data) > 4 << 20 or b"\nTOMACCO_CRYPTEX_COMPLETE\n" not in data
            or b"TOMACCO_CRYPTEX_FAILED" in data
            or (policy is not None and b"\nTOMACCO_CRYPTEX_GRAFT_VERIFIED\n" not in data)
            or (graphics is not None and b"\nTOMACCO_GRAPHICS_VERIFIED\n" not in data)):
        failure = re.search(rb"TOMACCO_CRYPTEX_FAILED:([a-z-]+)", data)
        stage = failure[1].decode() if failure else "unknown"
        raise RuntimeError(f"Cryptex helper failed during {stage}; inspect {log_path}")
    result = {}
    for name in ("OS", "APP"):
        match = re.search(rb"\nTOMACCO_" + name.encode() + rb"_HASHES_BEGIN\n([A-Za-z0-9+/=\n]+)TOMACCO_"
                          + name.encode() + rb"_HASHES_END\n", data)
        if match is None:
            raise ValueError("Missing Cryptex file verification inventory")
        inventory = base64.b64decode(match[1].replace(b"\n", b""), validate=True)
        rows = inventory.splitlines()
        if not rows or any(re.fullmatch(rb"[0-9a-f]{64}  \./[^\x00]+", row) is None for row in rows):
            raise ValueError("Invalid Cryptex file verification inventory")
        path = output / (name.lower() + "-files.sha256")
        path.write_bytes(inventory)
        result[name] = {"regular_files_verified": len(rows), "inventory": str(path),
                        "inventory_sha256": hashlib.sha256(inventory).hexdigest()}
    return result


def prepare(args):
    output = args.output
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    deadline = time.monotonic() + args.seconds
    record = {"format": 1, "scope": "ios-preboot-cryptex-install", "state": "preparing",
              "phase": "inputs", "input_files_modified": False, "sealed_system_modified": False,
              "external_requests": False, "guest_boot_attempted": False,
              "qualified_for_app_development": False}
    save(output, record)
    try:
        research_inputs = [getattr(args, name, None) for name in ("kernel", "boot_manifest", "device_tree")]
        if any(research_inputs) and not all(research_inputs):
            raise ValueError("Research graft preparation requires kernel, boot_manifest and device_tree together")
        gpu_trustcache = getattr(args, "gpu_trustcache", None)
        if gpu_trustcache is not None and not all(research_inputs):
            raise ValueError("GPU preparation requires kernel, boot_manifest and device_tree together")
        installer_control = getattr(args, "installer_control", False)
        if installer_control and not all(research_inputs):
            raise ValueError("Installer control requires kernel, boot_manifest and device_tree together")
        development_tools = getattr(args, "development_tools", None)
        if development_tools is not None and (gpu_trustcache is None or not installer_control):
            raise ValueError("Development mapping requires gpu_trustcache and installer_control")
        if args.accel == "kvm" and not os.access("/dev/kvm", os.R_OK | os.W_OK):
            raise ValueError("KVM helper requires access to /dev/kvm")
        root = args.userland.resolve(strict=True)
        receipt = json.loads(read_component(root, "userland.json", 1 << 20))
        if (not isinstance(receipt, dict) or receipt.get("state") != "prepared"
                or receipt.get("scope") != "original-ios-userland-assets"
                or not isinstance(receipt.get("ios"), dict)):
            raise ValueError("Select a completed original iPhone userland import")
        _data, entries, ios = manifest(root, receipt["ios"].get("identity_index"), filename="ios-BuildManifest.plist")
        if ios != receipt["ios"]:
            raise ValueError("Original iPhone manifest changed after import")
        record["ios"] = ios
        tc, record["trustcache"] = combine_caches(root, entries)
        graphics = None
        if gpu_trustcache is not None:
            record["phase"] = "prepare-graphics"
            save(output, record)
            tc, graphics = prepare_graphics(output, root, receipt, gpu_trustcache, tc,
                                           signer=args.support / "ldid/ldid", deadline=deadline)
            record["graphics"] = graphics
            record["trustcache"].update(entries=graphics["trustcache"]["entries"],
                                       graphics=graphics["trustcache"])
        (output / "system.tc").write_bytes(tc)
        record["trustcache"].update(path=str(output / "system.tc"), sha256=hashlib.sha256(tc).hexdigest())
        development = development_library(development_tools, tc) if development_tools is not None else None
        policy = None
        if all(research_inputs):
            record["phase"] = "prepare-research-kernel"
            save(output, record)
            policy = prepare_boot(output, *research_inputs, root, entries, ios, deadline,
                                  graphics=graphics, installer_control=installer_control,
                                  development_library=development)
            record["research_boot_policy"] = policy
        record["phase"] = "copy-disk"
        save(output, record)
        record["disk_copy"] = snapshot(args.disk.parent, args.disk.name, output / "system.raw", deadline)
        with (output / "system.raw").open("rb") as stream:
            layout = inspect_apfs(stream.fileno())
        expected_root_hash, root_hash_source = original_root_hash(root, entries)
        volume = preboot_volume(layout, expected_root_hash)
        (output / "system.root_hash").write_bytes(root_hash_source)
        record["system_root_hash_matched"] = expected_root_hash
        record.update(layout_before=layout, preboot=volume, phase="verify-cryptexes", images={})
        save(output, record)
        for name, filename in (("Cryptex1,SystemOS", "system-cryptex.dmg"), ("Cryptex1,AppOS", "app-cryptex.dmg")):
            record["images"][name] = import_image(root, "images/" + filename, output / "images" / filename,
                                                 entries[name]["Digest"], deadline)
            save(output, record)
        record["phase"] = "install-preboot"
        save(output, record)
        record["files"] = helper(args, output, layout, volume, deadline, policy, record["images"], graphics)
        with (output / "system.raw").open("rb") as stream:
            after = inspect_apfs(stream.fileno())
        protected = lambda values: [v for v in values["volumes"] if v["role"] != 16]
        if protected(layout) != protected(after):
            record["sealed_system_modified"] = any(v["role"] == 1 and v not in after["volumes"]
                                                   for v in layout["volumes"])
            raise ValueError("A volume other than Preboot changed during Cryptex installation")
        record.update(state="installed", phase="complete", layout_after=after,
                      system_disk=str(output / "system.raw"), all_regular_files_verified=True,
                      original_graft_objects_installed=policy is not None,
                      graphics_payload_installed=graphics is not None,
                      boot_inputs={**(policy["boot_inputs"] if policy else {}),
                                   "writable_root_disk": str(output / "system.raw"),
                                   "trustcache": str(output / "system.tc"),
                                   "system_volume_root_hash": str(output / "system.root_hash")})
    except KeyboardInterrupt:
        record.update(state="cancelled", error="Cryptex installation interrupted")
        raise
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        record.update(state="failed", error=str(error))
        raise
    finally:
        save(output, record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("disk", "userland", "support", "helper-engine", "helper-kernel", "bios", "output"):
        parser.add_argument("--" + name, type=lambda value: Path(value).expanduser().absolute(), required=True)
    for name in ("kernel", "boot-manifest", "device-tree", "gpu-trustcache", "development-tools"):
        parser.add_argument("--" + name, type=lambda value: Path(value).expanduser().absolute())
    parser.add_argument("--installer-control", action="store_true",
                        help="Enable only the pinned original installd registered-port query for native service control")
    parser.add_argument("--accel", choices=("kvm", "tcg"), default="kvm")
    parser.add_argument("--seconds", type=int, default=900)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600:
        parser.error("seconds must be between 1 and 3600")
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        record = prepare(args)
        print(json.dumps({"state": record["state"], "receipt": str(args.output / "cryptex.json")}))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"state": "cancelled", "receipt": str(args.output / "cryptex.json")}))
        return 130
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"state": "failed", "error": str(error), "receipt": str(args.output / "cryptex.json")}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
