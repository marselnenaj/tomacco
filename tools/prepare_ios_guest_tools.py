#!/usr/bin/env python3
"""Install owned guest tools into a private copy of a restored virtual iPhone.

Only unsealed Preboot is mounted writable, inside a networkless Linux helper.
The original monitor's virtual ERM boot option enables SupplementalPaths.
This does not activate iOS or certify a working graphical application runtime.
"""
import argparse
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import stat
import subprocess
import time

from ios_apfs_layout import inspect as inspect_apfs
import ios_disk_overlay
from build_ios_guest_tools import guest_directory_modes
from ios_guest_tools import payload, virtual_research_mode
from ios_host_pressure import pressure_error, wait_for_host_pressure
from ios_paravirt_graphics import BUNDLE_PATH, GRAPHICS_RELATIVE_PATH
from ios_paravirt_firmware import read_component
from prepare_ios_cryptex import preboot_volume
from prepare_ios_emulator import write_cpio
from prepare_ios_userland import open_source, snapshot


def verify_previous_script(inventory, hashes="/previous.sha256", *, directory_modes=False,
                           scratch_prefix="/previous"):
    """Check a closed, signed inventory before replacing our own offline files."""
    directories = set()
    for row in inventory:
        directories.update(str(parent) for parent in Path(row["path"]).parents if str(parent) != ".")
    lines = ["test -d .", "test ! -L ."]
    if directory_modes:
        lines.append('test "$(stat -c %a .)" = 755')
    for directory in sorted(directories):
        name = shlex.quote(directory)
        lines.extend([f"test -d {name}", f"test ! -L {name}"])
        if directory_modes:
            lines.append(f'test "$(stat -c %a {name})" = 755')
    for row in inventory:
        name = shlex.quote(row["path"])
        lines.extend([f"test -f {name}", f"test ! -L {name}",
                      f'test "$(stat -c %a {name})" = {row["mode"]:o}'])
    entries = sorted({"./" + name for name in directories} | {"./" + row["path"] for row in inventory})
    # The selected payload has already passed payload()'s fixed-path contract.
    actual, expected = [shlex.quote(scratch_prefix + suffix) for suffix in ("-actual.paths", "-expected.paths")]
    lines.extend(["find . -mindepth 1 -print | LC_ALL=C sort > " + actual,
                  "printf '%s\\n' " + " ".join(map(shlex.quote, entries)) + " > " + expected,
                  "cmp " + expected + " " + actual,
                  "sha256sum -c " + shlex.quote(hashes)])
    return "\n".join(lines) + "\n"


GRAPHICS_FILES = (("AppleParavirtGPUMetalIOGPUFamily", 0o755), ("Info.plist", 0o644),
                  ("_CodeSignature/CodeResources", 0o644),
                  ("libAppleParavirtCompilerPluginIOGPUFamily.dylib", 0o755), ("p", 0o755))


def graphics_payload(directory):
    """Read the fixed Preboot Metal driver inventory without following links."""
    expected = {name for name, _mode in GRAPHICS_FILES} | {"_CodeSignature"}
    actual = {str(path.relative_to(directory)) for path in directory.rglob("*")}
    if directory.is_symlink() or actual != expected or any((directory / name).is_symlink() for name in expected):
        # Name the difference: an analysis tool's cache beside the driver (as
        # ipsw writes .a2s files) is otherwise indistinguishable from damage.
        unexpected, missing = sorted(actual - expected), sorted(expected - actual)
        raise ValueError("Graphics payload must contain exactly the prepared driver bundle files"
                         + (f"; unexpected: {', '.join(unexpected)}" if unexpected else "")
                         + (f"; missing: {', '.join(missing)}" if missing else ""))
    files = {name: read_component(directory, name, 16 << 20) for name, _mode in GRAPHICS_FILES}
    return files, [{"path": name, "mode": mode, "bytes": len(files[name]),
                    "sha256": hashlib.sha256(files[name]).hexdigest()} for name, mode in GRAPHICS_FILES]


def graphics_script(inventory, previous=None):
    """Verify the driver bundle, or restore it when the guest removed it."""
    if inventory is None:
        return ""
    bundle = "/mnt/preboot/" + GRAPHICS_RELATIVE_PATH
    check = (f"cd {bundle}\n" +
             verify_previous_script(inventory, "/graphics.sha256", directory_modes=True,
                                    scratch_prefix="/graphics") + "cd /\n")
    if previous is not None and previous == inventory:
        # A tools-only update may retain the exact graphics inventory. Keep
        # the original file/type/mode/hash checks, but avoid copying, renaming
        # and removing an identical bundle on the private APFS volume.
        replacement = (f"cd {bundle}\n" + verify_previous_script(previous, "/previous-graphics.sha256", directory_modes=True) +
                       "cd /\necho TOMACCO_GRAPHICS_VERIFIED\n")
    elif previous is not None:
        replacement = (f"cd {bundle}\n" + verify_previous_script(previous, "/previous-graphics.sha256") +
            f"cd /\ntest ! -e {bundle}.tomacco-new\ntest ! -L {bundle}.tomacco-new\n"
            f"test ! -e {bundle}.tomacco-old\ntest ! -L {bundle}.tomacco-old\n"
            f"mkdir -m 755 {bundle}.tomacco-new\ncp -a /graphics/. {bundle}.tomacco-new/\n" +
            check.replace(f"cd {bundle}\n", f"cd {bundle}.tomacco-new\n") +
            f"mv {bundle} {bundle}.tomacco-old\nmv {bundle}.tomacco-new {bundle}\n"
            f"rm -rf {bundle}.tomacco-old\necho TOMACCO_GRAPHICS_REPLACED\n")
    else:
        replacement = check + "echo TOMACCO_GRAPHICS_VERIFIED\n"
    return f"""test -d /mnt/preboot/Cryptexes
test ! -L /mnt/preboot/Cryptexes
if [ -e {bundle} ] || [ -L {bundle} ]; then
  test -d {bundle}
  test ! -L {bundle}
{replacement}
else
  mkdir -m 755 {bundle}
  cp -a /graphics/. {bundle}/
{check}  echo TOMACCO_GRAPHICS_RESTORED
fi
"""


def installer_script(partition, volume, previous=None, graphics=None, previous_graphics=None, *, inventory):
    if type(partition) is not int or not 1 <= partition <= 128 or type(volume) is not int or not 0 <= volume <= 100:
        raise ValueError("Invalid Preboot device location")
    # Verify every retained graphics file before the first Supplemental write.
    # A wrong previous graphics inventory must not leave new tools beside the
    # old trust cache. The replacement step still repeats its own guards.
    graphics_preflight = ""
    if previous_graphics is not None:
        bundle = "/mnt/preboot/" + GRAPHICS_RELATIVE_PATH
        graphics_preflight = (f"if [ -e {bundle} ] || [ -L {bundle} ]; then\n"
            f"  test -d {bundle}\n  test ! -L {bundle}\n  cd {bundle}\n" +
            verify_previous_script(previous_graphics, "/previous-graphics.sha256", directory_modes=True) +
            "  cd /\nfi\necho TOMACCO_GRAPHICS_PREFLIGHT_COMPLETE\n")
    destination = "/mnt/preboot/Cryptexes/Supplemental"
    if previous is None:
        prepare_destination = f"""if [ -e {destination} ]; then
  test -d {destination}
  test -z "$(ls -A {destination})"
else
  mkdir -m 755 {destination}
fi
"""
        replacement = ""
    else:
        prepare_destination = f"""test -d {destination}
cd {destination}
{verify_previous_script(previous)}cd /
test ! -e {destination}.tomacco-new
test ! -L {destination}.tomacco-new
test ! -e {destination}.tomacco-old
test ! -L {destination}.tomacco-old
mkdir -m 755 {destination}.tomacco-new
"""
        replacement = f"""cd /
mv {destination} {destination}.tomacco-old
mv {destination}.tomacco-new {destination}
rm -rf {destination}.tomacco-old
"""
        destination += ".tomacco-new"
    return f"""#!/bin/sh
export PATH=/bin
/bin/busybox --install -s /bin
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
set -eu
trap 'echo TOMACCO_GUEST_TOOLS_FAILED; sync; poweroff -f' EXIT
insmod /apfs.ko
mount -t apfs -o readwrite,vol={volume} /dev/vda{partition} /mnt/preboot
test -d /mnt/preboot/Cryptexes
test ! -L /mnt/preboot/Cryptexes
if [ -L /mnt/preboot/Cryptexes/Supplemental ]; then exit 1; fi
{graphics_preflight}{prepare_destination}cp -a /Supplemental/. {destination}/
cd {destination}
{verify_previous_script(inventory, "/inventory.sha256", directory_modes=True)}
{replacement}cd /
{graphics_script(graphics, previous_graphics)}sync
umount /mnt/preboot
trap - EXIT
echo TOMACCO_GUEST_TOOLS_INSTALLED
poweroff -f
"""


def kernel_release(kernel):
    """The release string of a Linux bzImage, from its setup header."""
    with open(kernel, "rb") as stream:
        header = stream.read(0x10000)
    if len(header) < 0x210 or header[0x202:0x206] != b"HdrS":
        raise ValueError(f"Helper kernel is not a Linux bzImage: {kernel}")
    offset = int.from_bytes(header[0x20e:0x210], "little") + 0x200
    if not 0x200 <= offset < len(header):
        raise ValueError(f"Helper kernel has no version string: {kernel}")
    return header[offset:header.index(b"\0", offset)].split(b" ", 1)[0].decode("ascii", "replace")


def module_release(module):
    """The kernel release an out-of-tree module was built for (its vermagic)."""
    data = Path(module).read_bytes()
    start = data.find(b"vermagic=")
    if start < 0:
        raise ValueError(f"Helper module has no vermagic: {module}")
    end = data.index(b"\0", start)
    return data[start + 9:end].split(b" ", 1)[0].decode("ascii", "replace")


def check_helper_kernel(kernel, support):
    """Refuse a helper kernel the support APFS module cannot load into.

    The module is built for one kernel release; a host kernel update leaves
    the helper unable to mount Preboot, which otherwise surfaces only as a
    failed insmod in the helper log."""
    built, running = module_release(Path(support) / "apfs/apfs.ko"), kernel_release(kernel)
    if built != running:
        raise ValueError(f"Helper kernel {running} cannot load the support APFS module built for {built}; "
                         f"rebuild the module for {running} or keep a copy of the {built} kernel as "
                         f"{Path(support) / 'apfs/vmlinuz'}")


def helper(args, output, files, inventory, layout, volume, deadline, previous=None, graphics=None, previous_graphics=None,
           storage_fd=None, storage_name="system.raw", storage_format="raw"):
    check_helper_kernel(args.helper_kernel, args.support)
    root = output / "helper/root"
    for name in ("bin", "dev", "proc", "sys", "mnt/preboot"):
        (root / name).mkdir(parents=True, exist_ok=True)
    for relative, destination in (("busybox/busybox", "bin/busybox"), ("apfs/apfs.ko", "apfs.ko")):
        snapshot(args.support, relative, root / destination, deadline)
    (root / "bin/busybox").chmod(0o755)
    (root / "bin/sh").symlink_to("busybox")
    for row in inventory:
        target = root / "Supplemental" / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(files[row["path"]])
        target.chmod(row["mode"])
    guest_directory_modes(root / "Supplemental")
    (root / "inventory.sha256").write_text("".join(row["sha256"] + "  " + row["path"] + "\n" for row in inventory))
    if previous is not None:
        (root / "previous.sha256").write_text("".join(row["sha256"] + "  " + row["path"] + "\n" for row in previous))
    graphics_files, graphics_inventory = graphics or (None, None)
    if graphics is not None:
        for row in graphics_inventory:
            target = root / "graphics" / row["path"]
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            target.write_bytes(graphics_files[row["path"]])
            target.chmod(row["mode"])
        guest_directory_modes(root / "graphics")
        (root / "graphics.sha256").write_text(
            "".join(row["sha256"] + "  " + row["path"] + "\n" for row in graphics_inventory))
    if previous_graphics is not None:
        (root / "previous-graphics.sha256").write_text(
            "".join(row["sha256"] + "  " + row["path"] + "\n" for row in previous_graphics))
    (root / "init").write_text(installer_script(layout["partition"], volume["index"], previous,
                                               graphics_inventory, previous_graphics, inventory=inventory))
    (root / "init").chmod(0o755)
    archive = output / "helper/init.cpio"
    write_cpio(root, archive)
    command = list(map(str, [args.helper_engine, "-L", args.bios, "-machine", "q35", "-accel", args.accel,
                           "-cpu", "host" if args.accel == "kvm" else "max", "-m", "1G", "-smp", "1",
                           "-kernel", args.helper_kernel, "-initrd", archive,
                           "-append", "console=ttyS0 rdinit=/init panic=-1 mitigations=off",
                           "-drive", "file=" + ("/proc/self/fd/" + str(storage_fd) if storage_fd is not None
                                                else str(output / storage_name).replace(",", ",,")) +
                           f",format={storage_format},if=virtio,readonly=off",
                           "-display", "none", "-serial", "stdio", "-monitor", "none", "-nic", "none", "-no-reboot"]))
    (output / "helper/command.json").write_text(json.dumps(command, indent=2) + "\n")
    log_path = output / "helper/helper.log"
    with log_path.open("xb") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                timeout=max(0.01, deadline - time.monotonic()),
                                pass_fds=(storage_fd,) if storage_fd is not None else ())
    data = read_component(log_path.parent, log_path.name, 4 << 20).replace(b"\r", b"")
    if result.returncode or b"\nTOMACCO_GUEST_TOOLS_INSTALLED\n" not in data or b"TOMACCO_GUEST_TOOLS_FAILED" in data:
        raise RuntimeError("Guest tools installation failed; inspect " + str(log_path))
    if graphics is None:
        return None
    return ("restored" if b"\nTOMACCO_GRAPHICS_RESTORED\n" in data else
            "replaced-verified-previous" if b"\nTOMACCO_GRAPHICS_REPLACED\n" in data else "verified")


def storage_identity(info):
    return {"device": info.st_dev, "inode": info.st_ino, "bytes": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def closed_copy_digest(stream, deadline):
    digest = hashlib.sha256()
    position, size = 0, os.fstat(stream.fileno()).st_size
    while position < size:
        if time.monotonic() >= deadline:
            raise TimeoutError("Closed-copy verification deadline exceeded")
        data = os.pread(stream.fileno(), min(4 << 20, size - position), position)
        if not data:
            raise ValueError("Closed copy was truncated during verification")
        digest.update(data)
        position += len(data)
    return digest.hexdigest()


def make_root(root, deadline):
    """Make the root disk the plan names with qemu-img: an empty overlay on the
    base (seconds) or a flattened raw disk (a full write, as a copy). The
    target must not exist; qemu-img would replace it."""
    target = Path(root["target"])
    if target.exists() or target.is_symlink():
        raise ValueError(f"{target} already exists")
    try:
        subprocess.run(root["command"], check=True, capture_output=True, text=True,
                       timeout=max(1.0, deadline - time.monotonic()))
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"qemu-img could not make {target}: {(error.stderr or error.stdout).strip()[:400]}") from None
    if root["method"] == "overlay":
        header = ios_disk_overlay.qcow2_header(target)
        if header is None or header["backing"] != root["base"]:
            raise ValueError(f"qemu-img made {target} without the backing file {root['base']}")
    os.chmod(target, 0o600)
    info = os.stat(target)
    return {"source": root["base"], "copy_method": root["method"], "backing_chain": root["base_chain"]
            if root["method"] == "overlay" else [], "copy_identity": {
                "device": info.st_dev, "inode": info.st_ino, "bytes": info.st_size,
                "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}}


def prepare(args):
    output = args.output
    # The disks read and the copy written; host I/O admission is judged by their devices.
    storage_paths = [path for path in (args.disk, args.auxiliary_disk, args.sep_storage) if path] + [output]
    resume = getattr(args, "resume", False)
    prior = None
    if resume:
        if output.is_symlink():
            raise ValueError("Preparation output must not be a symlink")
        captured = read_component(output, "guest-tools-install.json", 1 << 20)
        prior = json.loads(captured)
        if (not isinstance(prior, dict) or prior.get("scope") != "ios-owned-guest-tools-install"
                or prior.get("state") != "failed" or prior.get("phase") != "host-pressure-admission"
                or prior.get("disk_copies_complete") is not True or prior.get("helper_started")
                or (output / "helper").exists() or (output / "helper").is_symlink()):
            raise ValueError("Resume requires a complete closed copy refused before its helper started")
        if prior.get("root_disk", {}).get("method", "copy") != "copy" or getattr(args, "root_overlay", False):
            raise ValueError("Resume applies to copied disks; an overlay or flattened root disk is made again")
        previous_receipt = "guest-tools-install.attempt-" + hashlib.sha256(captured).hexdigest()[:16] + ".json"
        archive = output / previous_receipt
        if archive.exists() or archive.is_symlink():
            if read_component(output, previous_receipt, 1 << 20) != captured:
                raise ValueError("Previous preparation receipt archive changed")
        else:
            with os.fdopen(os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as stream:
                stream.write(captured)
    else:
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
    deadline = time.monotonic() + args.seconds
    record = {"format": 1, "scope": "ios-owned-guest-tools-install", "state": "preparing",
              "phase": "validate-payloads", "payload_preflight_complete": False,
              "storage_preflight_complete": False, "disk_copies_started": False,
              "disk_copies_complete": False, "disk_copies": {},
              "helper_started": False, "resumed": resume,
              "external_requests": False, "guest_execution_verified": False,
              "application_scene_verified": False, "activation_changed": False}
    if resume:
        record.update(previous_attempt=previous_receipt, disk_copies_started=True,
                      disk_copies_complete=True, disk_copies=prior["disk_copies"])
        # A second pressure refusal may happen before payload validation. Keep
        # the original bindings available for the next explicit resume.
        for name in ("build", "source_layout", "graphics_payload", "previous_build",
                     "previous_trustcache_source_sha256", "virtual_research_mode", "development_device"):
            if name in prior:
                record[name] = prior[name]

    def save():
        temporary = output / ".guest-tools-install.json.tmp"
        with temporary.open("w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(record, indent=2) + "\n")
        os.replace(temporary, output / "guest-tools-install.json")

    copy_locks = ExitStack()
    save()
    try:
        record["host_pressure_preflight"] = wait_for_host_pressure(300, storage=storage_paths)
        if not record["host_pressure_preflight"]["admitted"]:
            record["phase"] = "host-pressure-admission"
            raise RuntimeError(pressure_error(record["host_pressure_preflight"]))
        source_cache = read_component(args.trustcache.parent, args.trustcache.name, 4 << 20)
        build, files, cache = payload(args.tools, source_cache)
        previous = None
        previous_source = getattr(args, "previous_trustcache", None)
        if getattr(args, "previous_graphics", None) is not None and getattr(args, "graphics", None) is None:
            raise ValueError("A previous graphics payload requires the new graphics payload")
        if previous_source is not None and args.previous_tools is None:
            raise ValueError("A previous trust cache requires the corresponding previous tools")
        if args.previous_tools is not None:
            previous_cache = source_cache if previous_source is None else read_component(
                previous_source.parent, previous_source.name, 4 << 20)
            previous, _previous_files, _previous_cache = payload(args.previous_tools, previous_cache)
            record["previous_build"] = previous
            record["previous_trustcache_source_sha256"] = hashlib.sha256(previous_cache).hexdigest()
        # Capture the validated bytes now. Analysis sidecars or invalid previous
        # bundles must fail before creating potentially multi-gigabyte copies.
        graphics = graphics_payload(args.graphics) if getattr(args, "graphics", None) is not None else None
        previous_graphics = (graphics_payload(args.previous_graphics)[1]
                             if getattr(args, "previous_graphics", None) is not None else None)
        tree = read_component(args.device_tree.parent, args.device_tree.name, 4 << 20)
        development_device = getattr(args, "development_device", False)
        prepared_tree = virtual_research_mode(tree, development_device=development_device)
        if resume:
            if (read_component(output, "device-tree", 4 << 20) != prepared_tree or
                    read_component(output, "system.tc", 4 << 20) != cache or
                    prior["build"]["files"] != build["files"] or
                    prior.get("graphics_payload", {}).get("files") != (graphics[1] if graphics else None) or
                    prior.get("graphics_payload", {}).get("previous_files") != previous_graphics):
                raise ValueError("Resume inputs differ from the captured preparation")
        else:
            (output / "device-tree").write_bytes(prepared_tree)
            (output / "system.tc").write_bytes(cache)
        record.update(build=build, virtual_research_mode={
            "source_device_tree_sha256": hashlib.sha256(tree).hexdigest(),
            "device_tree_sha256": hashlib.sha256(prepared_tree).hexdigest(),
            "property": "/chosen/sec-research-device-erm-enabled", "value": 1,
            "monitor_code_modified": False, "kernel_code_modified": False, "active_verified": False})
        if development_device:
            record["development_device"] = {
                "profile": "virtual-vphone600-development", "production_status": 0,
                "certificate_security_mode": 1, "effective_security_mode_ap": 1,
                "effective_security_mode_sep": 1, "activation_service_modified": False,
                "activation_response_generated": False, "activation_result_verified": False}
        record.update(payload_preflight_complete=True, phase="validate-storage")
        if graphics is not None:
            record["graphics_payload"] = {"path": BUNDLE_PATH, "state": "validated",
                                          "files": graphics[1], "previous_files": previous_graphics,
                                          "rendering_verified": False}
        save()
        # The root disk: a copy, an overlay on the base (root_overlay) or a
        # flattened overlay base (ios_disk_overlay.plan). The small disks are copied.
        root = ios_disk_overlay.plan(args.disk, output, "overlay" if getattr(args, "root_overlay", False) else "copy",
                                     getattr(args, "qemu_img", None))
        root_name = Path(root["target"]).name
        record["root_disk"] = {key: root[key] for key in ("method", "reason", "base", "base_chain", "target")}
        save()
        sources = [(args.disk, root_name), (args.auxiliary_disk, "auxiliary.raw"), (args.sep_storage, "sep.raw")]
        # Hold shared locks on the whole storage set while taking its snapshots.
        # A running Tomacco guest holds exclusive locks on these inputs.
        with ExitStack() as locks:
            identities, streams = [], []
            for source, _name in sources:
                stream = locks.enter_context(os.fdopen(open_source(source.parent, source.name), "rb"))
                fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 64 << 30
                        or info.st_size % 512):
                    raise ValueError("Guest storage must be a bounded nonempty regular file of whole sectors")
                if source == args.sep_storage and (info.st_size < 1024 or info.st_size % 1024):
                    raise ValueError("SEP storage must contain a nonzero, even number of 512-byte sectors")
                identities.append((info.st_dev, info.st_ino))
                streams.append(stream)
            if len(set(identities)) != len(sources):
                raise ValueError("Root, auxiliary and SEP disks must be distinct files")
            # Inspect only metadata on the pinned, closed root before copying.
            # Reject missing/invalid Preboot now, not after a fallback disk copy.
            # An overlay base is read through its chain, each layer held shared.
            base_reader = locks.enter_context(ios_disk_overlay.Qcow2Chain(args.disk, lock=True))
            source_layout = inspect_apfs(base_reader)
            preboot_volume(source_layout)
            record["host_pressure_before_copy"] = wait_for_host_pressure(120, storage=storage_paths)
            if not record["host_pressure_before_copy"]["admitted"]:
                record["phase"] = "host-pressure-admission"
                raise RuntimeError(pressure_error(record["host_pressure_before_copy"]))
            record.update(phase="copy-storage", storage_preflight_complete=True,
                          source_layout=source_layout)
            save()
            copies = {}
            for (source, name), stream in zip(sources, streams):
                if not resume:
                    record.update(disk_copies_started=True, copying_disk=name)
                    save()
                    if name == root_name and root["method"] != "copy":
                        record["disk_copies"][name] = make_root(root, deadline)
                    else:
                        record["disk_copies"][name] = snapshot(
                            source.parent, source.name, output / name, deadline, source_fd=stream.fileno())
                copy = copy_locks.enter_context(os.fdopen(
                    os.open(output / name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK), "r+b"))
                fcntl.flock(copy.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                info = os.fstat(copy.fileno())
                expected = (base_reader.size() if name == root_name and root["method"] == "flatten"
                            else os.fstat(stream.fileno()).st_size)
                if not stat.S_ISREG(info.st_mode) or (root["method"] != "overlay" or name != root_name) and info.st_size != expected:
                    raise ValueError("Private copy has an invalid type or size")
                copy_id = storage_identity(info)
                expected_id = record["disk_copies"][name].get("copy_identity")
                if expected_id is not None and copy_id != expected_id:
                    raise ValueError("Private copy changed since it was created")
                identities.append((info.st_dev, info.st_ino))
                if len(set(identities)) != len(identities):
                    raise ValueError("Private copies and source disks must be distinct")
                if resume:
                    record.update(phase="verify-closed-copies", verifying_disk=name)
                    save()
                    source_id = storage_identity(os.fstat(stream.fileno()))
                    source_digest = closed_copy_digest(stream, deadline)
                    copy_digest = closed_copy_digest(copy, deadline)
                    if (source_digest != copy_digest or source_id != storage_identity(os.fstat(stream.fileno()))
                            or copy_id != storage_identity(os.fstat(copy.fileno()))):
                        raise ValueError("Private copy differs from its stable source")
                    record.setdefault("verified_copy_hashes", {})[name] = {
                        "source_sha256": source_digest, "copy_sha256": copy_digest, "all_bytes_equal": True}
                copies[name] = copy
                save()
            record.pop("copying_disk", None)
            record.pop("verifying_disk", None)
            record["disk_copies_complete"] = True
            save()
        with ios_disk_overlay.Qcow2Chain(output / root_name) as root_view:
            before = inspect_apfs(root_view)
        if resume and source_layout != prior["source_layout"]:
            raise ValueError("Source APFS layout changed since the closed copy was created")
        if before != source_layout:
            raise ValueError("Copied APFS layout differs from the validated source")
        volume = preboot_volume(before)
        record.update(phase="install-preboot", layout_before=before)
        record["host_pressure_before_helper"] = wait_for_host_pressure(120, storage=storage_paths)
        if not record["host_pressure_before_helper"]["admitted"]:
            record["phase"] = "host-pressure-admission"
            raise RuntimeError(pressure_error(record["host_pressure_before_helper"]))
        save()
        record["helper_started"] = True
        save()
        graphics_state = helper(args, output, files, build["files"], before, volume, deadline,
                                previous["files"] if previous is not None else None, graphics, previous_graphics,
                                storage_fd=copies[root_name].fileno(), storage_name=root_name,
                                storage_format="qcow2" if root["method"] == "overlay" else "raw")
        if graphics is not None:
            # Presence and bytes only; rendering still needs live qualification.
            record["graphics_payload"] = {"path": BUNDLE_PATH, "state": graphics_state,
                                          "files": graphics[1], "rendering_verified": False}
            record["graphics_payload"]["previous_files"] = previous_graphics
        with ios_disk_overlay.Qcow2Chain(output / root_name) as root_view:
            after = inspect_apfs(root_view)
        if [v for v in before["volumes"] if v["role"] != 16] != [v for v in after["volumes"] if v["role"] != 16]:
            raise ValueError("A volume other than Preboot changed during guest tools installation")
        record.update(state="installed", phase="complete", layout_after=after,
                      sealed_system_modified=False, original_service_cache_modified=False,
                      previous_payload_verified=previous is not None,
                      all_payload_files_verified=True, boot_inputs={
                          "writable_root_disk": str(output / root_name),
                          "writable_auxiliary_disk": str(output / "auxiliary.raw"),
                          "sep_storage": str(output / "sep.raw"),
                          "device_tree": str(output / "device-tree"), "trustcache": str(output / "system.tc")})
        save()
        return record
    except BaseException as error:
        record.update(state="cancelled" if isinstance(error, KeyboardInterrupt) else "failed", error=str(error))
        save()
        raise
    finally:
        copy_locks.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("disk", "auxiliary-disk", "sep-storage", "tools", "trustcache", "device-tree", "support",
                 "helper-engine", "helper-kernel", "bios", "output"):
        parser.add_argument("--" + name, type=lambda value: Path(value).expanduser().absolute(), required=True)
    parser.add_argument("--previous-tools", type=lambda value: Path(value).expanduser().absolute(),
                        help="Verified old payload to replace in a new copy of a closed guest")
    parser.add_argument("--previous-trustcache", type=lambda value: Path(value).expanduser().absolute(),
                        help="Original cache bound to previous tools when the new graphics cache changed; requires previous-tools")
    parser.add_argument("--graphics", type=lambda value: Path(value).expanduser().absolute(),
                        help="Prepared Preboot Metal driver bundle to verify, or restore if the guest removed it")
    parser.add_argument("--previous-graphics", type=lambda value: Path(value).expanduser().absolute(),
                        help="Exact closed previous bundle to verify before replacing it in the private copy; requires graphics")
    parser.add_argument("--development-device", action="store_true",
                        help="Configure the original local development-device activation path for a non-production vphone600 VM")
    parser.add_argument("--resume", action="store_true",
                        help="Verify and continue a complete closed copy refused by host pressure before its helper started")
    parser.add_argument("--root-overlay", action="store_true",
                        help="Make the root disk a qcow2 overlay on --disk instead of a copy (needs --qemu-img)")
    parser.add_argument("--qemu-img", help="qemu-img for an overlay root disk or to flatten an overlay base")
    parser.add_argument("--accel", choices=("kvm", "tcg"), default="kvm")
    parser.add_argument("--seconds", type=int, default=300)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600:
        parser.error("seconds must be between 1 and 3600")

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        record = prepare(args)
        print(json.dumps({"state": record["state"], "boot_inputs": record["boot_inputs"]}))
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"state": "failed", "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
