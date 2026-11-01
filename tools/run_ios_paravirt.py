#!/usr/bin/env python3
"""Run a bounded, exclusive vphone600 firmware session and save real QMP state.

This is a research launcher. A successful QEMU exit is not an iOS boot or an
application-runtime qualification. No firmware is modified by this command.
"""
import argparse
import datetime
import ctypes
from contextlib import ExitStack
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import secrets
import shutil
import signal
import socket
import stat
import struct
import subprocess
import tempfile
import time

from prepare_ios_paravirt import (ap_cpu_count, decode, display_handoff, identity_handoff,
                                  FIRMWARE, MAX_TREE_BYTES)
from ios_paravirt_storage import nvram_handoff
import ios_disk_overlay
from ios_paravirt_pcc import hostname_config, pcc_config_handoff, pcc_guest_evidence
from ios_paravirt_firmware import der_fields, retag_im4p, validate_im4m
from ios_host_pressure import gpu_memory_short, pressure_error, process_gpu_memory, wait_for_host_pressure


def open_writable_storage(path, protected, *, sep=False):
    """Lock actual writable media across engine versions; preserve boot inputs."""
    storage = os.fdopen(os.open(path, os.O_RDWR | os.O_NOFOLLOW, 0o600), "r+b")
    try:
        info = os.fstat(storage.fileno())
        if sep and (not stat.S_ISREG(info.st_mode) or info.st_size < 1024 or
                    info.st_size % 1024 or info.st_size // 512 > 0xffffffff):
            raise ValueError("SEP storage must contain a nonzero, even number of 512-byte sectors")
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0 or info.st_size % 512:
            raise ValueError("Storage must be a nonempty regular file of whole 512-byte sectors")
        identity = (info.st_dev, info.st_ino)
        if any(identity == (other.st_dev, other.st_ino)
               for other in (item.stat() for item in protected)):
            raise ValueError("Writable storage must be separate from boot inputs and other storage")
        try:
            fcntl.flock(storage, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("This storage already has an active session") from None
        return storage
    except BaseException:
        storage.close()
        raise


def open_sep_storage(path, protected):
    return open_writable_storage(path, protected, sep=True)


def place_host_cpu_threads(qmp, process_id, ap_cpus, policy, clock_mode):
    """Place AP threads on separate high-frequency physical host cores.

    Respect the launcher's allowed CPU set. Missing topology, shared TCG
    threads and affinity failures leave scheduling to the host; placement is
    an optimization and must never make an otherwise valid boot fail.
    """
    receipt = {"policy": policy, "applied": False}
    if policy != "performance" or clock_mode != "wall":
        return {**receipt, "reason": "host-scheduler" if policy != "performance" else "single-threaded-tcg"}
    if not hasattr(os, "sched_getaffinity") or not hasattr(os, "sched_setaffinity"):
        return {**receipt, "reason": "affinity-unavailable"}
    original = {}
    applied = []
    try:
        allowed = os.sched_getaffinity(0)
        cores = {}
        for cpu in sorted(allowed):
            root = Path(f"/sys/devices/system/cpu/cpu{cpu}")
            package = int((root / "topology/physical_package_id").read_text())
            core = int((root / "topology/core_id").read_text())
            frequency = int((root / "cpufreq/cpuinfo_max_freq").read_text())
            if frequency <= 0 or package < 0 or core < 0:
                return {**receipt, "reason": "topology-unavailable"}
            # Select one logical CPU per physical core, keeping SMT siblings
            # available to the scheduler instead of placing two APs together.
            cores.setdefault((package, core), (frequency, cpu))
        targets = sorted(cores.values(), key=lambda row: (-row[0], row[1]))[:ap_cpus]
        if len(targets) != ap_cpus:
            return {**receipt, "reason": "insufficient-physical-cores", "available": len(cores)}
        reply = qmp.call("query-cpus-fast")
        rows = reply.get("return")
        if not isinstance(rows, list):
            return {**receipt, "reason": "cpu-thread-query-unavailable"}
        aps = [row for row in rows if isinstance(row, dict)
               and type(row.get("cpu-index")) is int and 0 <= row["cpu-index"] < ap_cpus]
        aps.sort(key=lambda row: row["cpu-index"])
        if [row["cpu-index"] for row in aps] != list(range(ap_cpus)):
            return {**receipt, "reason": "incomplete-ap-thread-list"}
        tids = [row.get("thread-id") for row in aps]
        if any(type(tid) is not int or tid <= 0 for tid in tids) or len(set(tids)) != ap_cpus:
            return {**receipt, "reason": "shared-or-invalid-cpu-threads"}
        # QMP describes our child; also check Linux task membership before any
        # affinity mutation, so a stale/malformed ID cannot name another process.
        if any(not Path(f"/proc/{process_id}/task/{tid}").is_dir() for tid in tids):
            return {**receipt, "reason": "cpu-thread-membership-unavailable"}
        for tid in tids:
            original[tid] = os.sched_getaffinity(tid)
        assignments = []
        for row, (frequency, cpu) in zip(aps, targets):
            tid = row["thread-id"]
            if cpu not in original[tid]:
                raise ValueError("Host core is outside the CPU thread's allowed set")
            os.sched_setaffinity(tid, {cpu})
            applied.append(tid)
            actual = sorted(os.sched_getaffinity(tid))
            if actual != [cpu]:
                raise ValueError("Host did not retain the requested CPU placement")
            assignments.append({"ap_cpu": row["cpu-index"], "thread_id": tid,
                                "host_cpus": actual, "max_frequency_khz": frequency})
        return {**receipt, "applied": True, "assignments": assignments}
    except (OSError, ValueError, TypeError, TimeoutError) as error:
        restore_errors = []
        for tid in applied:
            try:
                os.sched_setaffinity(tid, original[tid])
            except OSError as restore_error:
                restore_errors.append({"thread_id": tid, "error": str(restore_error)})
        return {**receipt, "reason": "placement-unavailable", "error": str(error),
                "restore_errors": restore_errors}


class QMP:
    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX)
        self.stream = None
        self.socket.settimeout(5)
        try:
            self.socket.connect(str(path))
            self.stream = self.socket.makefile("rwb")
            if "QMP" not in self.receive():
                raise ValueError("Invalid QMP greeting")
            if "error" in self.call("qmp_capabilities"):
                raise ValueError("QMP capabilities were rejected")
        except BaseException:
            self.close()
            raise

    def receive(self):
        line = self.stream.readline(1024 * 1024 + 1)
        if not line or len(line) > 1024 * 1024:
            raise ValueError("Missing or oversized QMP response")
        return json.loads(line)

    def call(self, command, arguments=None):
        request = {"execute": command}
        if arguments is not None:
            request["arguments"] = arguments
        self.stream.write(json.dumps(request).encode() + b"\n")
        self.stream.flush()
        # Debugger stops/resumes generate asynchronous events. A healthy guest
        # can accumulate thousands between observations; they are not failed
        # command replies. Bound total response time, including event traffic.
        timeout = self.socket.gettimeout()
        deadline = time.monotonic() + 5
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("QMP command response deadline exceeded")
                self.socket.settimeout(remaining)
                reply = self.receive()
                if "return" in reply or "error" in reply:
                    return reply
        finally:
            self.socket.settimeout(timeout)

    def close(self):
        if self.stream is not None:
            self.stream.close()
        self.socket.close()


def admit_translation_cache(qmp, requested_mib, receipt):
    """Admit a large cache from this paused engine's actual compiled capability.

    The QOM fields belong to the TCG backend, and the mapped span includes
    guard pages. An old engine's silent cache clamp cannot satisfy this gate.
    Legacy requests keep their existing launch behavior.
    """
    receipt.update(requested_mib=requested_mib,
                   large_cache_requested=requested_mib is not None and requested_mib > 2048,
                   engine_capabilities_verified=False, admitted=False)
    if not receipt["large_cache_requested"]:
        receipt["admitted"] = True
        return
    receipt["initial_state"] = qmp.call("query-status")
    state = receipt["initial_state"].get("return", {})
    if state.get("running") is not False or state.get("status") not in ("paused", "prelaunch"):
        receipt["refusal"] = "translation_cache_not_paused"
        raise ValueError("translation_cache_not_paused: large-cache admission requires an unstarted guest")
    replies = receipt["property_replies"] = {}
    for name in ("max-tb-size", "wide-tb-chaining", "tb-buffer-size"):
        replies[name] = qmp.call("qom-get", {"path": "/machine/accel", "property": name})
        if "return" not in replies[name]:
            receipt["refusal"] = "translation_cache_capability_unavailable"
            raise ValueError("translation_cache_capability_unavailable: engine cannot prove wide TB chaining")
    maximum = replies["max-tb-size"]["return"]
    wide = replies["wide-tb-chaining"]["return"]
    mapped = replies["tb-buffer-size"]["return"]
    if (type(maximum) is not int or not requested_mib <= maximum <= 4096 or wide is not True or
            type(mapped) is not int or mapped != requested_mib * (1 << 20)):
        receipt["refusal"] = "translation_cache_capability_mismatch"
        raise ValueError("translation_cache_capability_mismatch: wide-chain support and exact requested mapping are required")
    receipt.update(engine_capabilities_verified=True, maximum_mib=maximum,
                   range_independent_chaining=True, mapped_bytes=mapped, admitted=True)


LARGE_HASH_BYTES = 256 << 20
HASH_READ_BYTES = 4 << 20
HASH_BYTES_PER_SECOND = 128 << 20
# Two queries per data extent. A disk copied sparsely (prepare_ios_userland's
# sparse_copy) can have thousands of extents; past the limit the rest is read
# as data, holes too. A query costs microseconds, so the bound stays small.
SPARSE_HASH_QUERY_LIMIT = 1 << 18


def hash_region(stream, position, size, sparse):
    """Return one OS-reported zero hole or data range; fallback reads bytes.

    SEEK_DATA/SEEK_HOLE describe logical zeros, not allocation guesses from
    st_blocks. An unsupported or exhausted query falls back at the already
    hashed offset. I/O errors and invalid ranges are never hidden.
    """
    if sparse["state"] != "supported":
        return False, size
    def seek(kind):
        if sparse["queries"] >= SPARSE_HASH_QUERY_LIMIT:
            sparse["state"] = "query-limit"
            return None
        sparse["queries"] += 1
        return os.lseek(stream.fileno(), position, kind)
    try:
        try:
            data = seek(os.SEEK_DATA)
        except OSError as error:
            if error.errno != errno.ENXIO:
                raise
            return True, size
        if data is None:
            return False, size
        if not position <= data <= size:
            raise ValueError("Invalid sparse data range during SHA256 observation")
        if data > position:
            return True, data
        hole = seek(os.SEEK_HOLE)
        if hole is None:
            return False, size
        if not position < hole <= size:
            raise ValueError("Invalid sparse hole range during SHA256 observation")
        return False, hole
    except OSError as error:
        if error.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS):
            raise
        sparse["state"] = "unsupported"
        return False, size


def file_digest(path, *, observation=None):
    """Hash every logical byte, pacing large data reads including cached ones.

    Integrity is never inferred from an earlier receipt or file metadata.
    OS-reported sparse holes feed exact zero bytes to the same SHA256 stream.
    NOREUSE is only a cache-replacement hint, not cache eviction or an I/O
    guarantee. Each read has its own pacing interval: a storage stall cannot
    accumulate credit for a later burst. Small boot files keep the fast path.
    """
    started = time.monotonic()
    waited = 0.0
    with path.open("rb", buffering=0) as stream:
        before = os.fstat(stream.fileno())
        paced = stat.S_ISREG(before.st_mode) and before.st_size >= LARGE_HASH_BYTES
        advice = "not-requested"
        if paced and hasattr(os, "posix_fadvise") and hasattr(os, "POSIX_FADV_NOREUSE"):
            try:
                os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_NOREUSE)
                advice = "requested"  # An accepted hint does not prove kernel effect.
            except OSError as error:
                advice = "unavailable: " + str(error)
        digest = hashlib.sha256()
        total = read_bytes = zero_bytes = 0
        sparse = {"state": "not-requested", "queries": 0}
        if paced:
            sparse["state"] = ("supported" if getattr(os, "SEEK_DATA", None) is not None
                               and getattr(os, "SEEK_HOLE", None) is not None else "api-unavailable")
        zeros = None
        while total < before.st_size or not paced:
            hole, end = hash_region(stream, total, before.st_size, sparse)
            if hole:
                if zeros is None:
                    zeros = bytes(HASH_READ_BYTES)
                while total < end:
                    count = min(HASH_READ_BYTES, end - total)
                    digest.update(zeros if count == HASH_READ_BYTES else zeros[:count])
                    total += count
                    zero_bytes += count
                continue
            if paced:
                # Extent queries move the descriptor; fallback resumes exactly
                # after the prefix already hashed, without duplication.
                stream.seek(total)
            while total < end or not paced:
                reading = time.monotonic()
                count = min(HASH_READ_BYTES, end - total) if paced else HASH_READ_BYTES
                data = stream.read(count)
                if not data:
                    if stat.S_ISREG(before.st_mode) and total != before.st_size:
                        raise ValueError("File ended during complete SHA256 observation: " + str(path))
                    break
                digest.update(data)
                total += len(data)
                read_bytes += len(data)
                if paced:
                    delay = len(data) / HASH_BYTES_PER_SECOND - (time.monotonic() - reading)
                    if delay > 0:
                        waiting = time.monotonic()
                        time.sleep(delay)
                        waited += time.monotonic() - waiting
            if not paced:
                break
        after = os.fstat(stream.fileno())
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size,
                                 info.st_mtime_ns, info.st_ctime_ns)
        if stat.S_ISREG(before.st_mode) and (total != before.st_size or identity(before) != identity(after)):
            raise ValueError("File changed during complete SHA256 observation: " + str(path))
    if observation is not None:
        observation.update(path=str(path), bytes=total, complete=True,
                           read_bytes=read_bytes, sparse_zero_bytes=zero_bytes,
                           sparse_extent_state=sparse["state"], sparse_extent_queries=sparse["queries"],
                           elapsed_seconds=round(time.monotonic() - started, 3),
                           pacing_wait_seconds=round(waited, 3),
                           read_bytes_per_second_limit=HASH_BYTES_PER_SECOND if paced else None,
                           cache_noreuse_hint=advice)
    return digest.hexdigest()


def timing_of(observations, kind):
    """The seconds and bytes of these hash observations, as KIND_hashing_*."""
    return {f"{kind}_hashing_seconds": round(sum(row.get("elapsed_seconds") or 0 for row in observations), 3),
            f"{kind}_hashing_bytes": sum(row.get("bytes") or 0 for row in observations)}


def save_result(output, record):
    temporary = output / ".result.json.tmp"
    with temporary.open("w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(record, stream, indent=2)
        stream.write("\n")
    os.replace(temporary, output / "result.json")


def boot_manifest_handoff(tree, data):
    """Supply the explicit boot object's digest, without authenticating it.

    XNU IOGetBootManifestHash reads /chosen/boot-manifest-hash. The original
    restored verifier first compares that digest with the host's RootTicket.
    Validate the DER envelope here; signatures/binding remain guest decisions.
    """
    validate_im4m(data)
    chosen = tree.child("chosen")
    if chosen.get("crypto-hash-method") != b"sha2-384\0":
        raise ValueError("Boot manifest handoff requires the guest's SHA2-384 profile")
    digest = hashlib.sha384(data).digest()
    previous = chosen.get("boot-manifest-hash") if "boot-manifest-hash" in chosen.props else bytes(48)
    if len(previous) != 48 or (any(previous) and previous != digest):
        raise ValueError("Prepared tree refers to a different boot manifest")
    chosen.set("boot-manifest-hash", digest)
    return {"sha256": hashlib.sha256(data).hexdigest(), "sha384": digest.hex(),
            "bytes": len(data), "hash_updated": previous != digest,
            "signature_verified_by_host": False, "device_binding_verified": False}


def system_volume_handoff(tree, data):
    """Pass an original v2 SHA-256 SystemVolume payload to APFS unchanged.

    This reproduces the data handoff, not iBoot's Image4 authentication. APFS
    still compares this root hash with the actual sealed volume. In particular,
    no secure-boot, system-trusted or root-authentication override is added.
    """
    if len(data) > 65536:
        raise ValueError("SystemVolume root hash exceeds 64 KiB")
    _, original, _ = retag_im4p(data)
    if original != "isys":
        raise ValueError("SystemVolume root hash must be an original isys IM4P")
    _, start, end = der_fields(data)[3]
    payload = data[start:end]
    # vphone600 23E5207q supplies three 64-byte digest slots. The first is
    # used by APFS to select the sealed snapshot. Accept only this observed
    # version/profile; guessing a future layout could corrupt the handoff.
    if len(payload) != 208 or struct.unpack_from("<4I", payload) != (2, 0, 1, 32):
        raise ValueError("Unsupported SystemVolume root-hash format (expected v2 SHA-256)")
    if not any(payload[16:48]) or any(payload[i] for slot in (16, 80, 144)
                                      for i in range(slot + 32, slot + 64)):
        raise ValueError("Invalid SystemVolume root hash or digest padding")
    chosen = tree.child("chosen")
    previous = chosen.props.get("system-volume-auth-blob")
    if previous is not None and previous.data != payload:
        raise ValueError("Prepared tree refers to a different SystemVolume root hash")
    # The original restored/apfs_sealvolume pair creates this snapshot from
    # the first SHA-256 slot. APFS refuses a sealed live volume if iBoot's
    # root-snapshot-name handoff is absent, even with the correct auth blob.
    snapshot = "com.apple.os.update-" + payload[16:48].hex().upper()
    snapshot_data = snapshot.encode("ascii") + b"\0"
    previous_snapshot = chosen.props.get("root-snapshot-name")
    if previous_snapshot is not None and previous_snapshot.data != snapshot_data:
        raise ValueError("Prepared tree refers to a different root snapshot")
    chosen.set("system-volume-auth-blob", payload)
    chosen.set("root-snapshot-name", snapshot_data)
    return {"source_sha256": hashlib.sha256(data).hexdigest(),
            "source_sha384": hashlib.sha384(data).hexdigest(), "source_bytes": len(data),
            "payload_sha256": hashlib.sha256(payload).hexdigest(), "payload_bytes": len(payload),
            "root_hash": payload[16:48].hex(), "algorithm": "sha256", "version": 2,
            "root_snapshot_name": snapshot,
            "property_updated": previous is None or previous_snapshot is None,
            "signature_verified_by_host": False, "bootloader_policy_evaluated": False,
            "volume_seal_verified_by_host": False}


def frame_file_bytes(profile):
    """Bytes of one uncompressed binary PPM of the panel, as a viewer's live
    frame is dumped by the emulator (control.rs, transport file)."""
    width, height = profile["width"], profile["height"]
    return len(f"P6\n{width} {height}\n255\n") + width * height * 3


# The engine's frame mailbox (reims-frame-mailbox.patch): a 4 KiB header and
# two slots of one BGRA8 frame each, so a reader keeps a whole frame while the
# next one is written. The control plane reads live frames from it instead of
# asking QEMU's main loop for a screendump (native/ios_emulator/control.rs).
FRAME_MAILBOX_HEADER_BYTES = 4096
FRAME_MAILBOX_SLOTS = 2
FRAME_MAILBOX_VARIABLE = "REIMS_VGPU_FRAME_MAILBOX"
# reims-lazy-console-paint: with a mailbox and no window the engine copies a
# present into its console surface only when a screendump or refresh asks;
# this variable restores the copy on every present.
EAGER_CONSOLE_PAINT_VARIABLE = "REIMS_VGPU_EAGER_CONSOLE_PAINT"
# qemu-arm-pauth-bypass-inline: the CPU property that restores the per-
# instruction pointer-authentication helpers (default: inline bypass).
POINTER_AUTH_HELPERS = "max-arm-cpu.tomacco-pauth-fast=off"
# The opt-in engine patches (their CPU properties; an engine without the
# patch refuses the -global): qemu-arm-bti-property removes FEAT_BTI,
# qemu-arm-jump-probe selects the TB-exit jump-cache probe,
# qemu-arm-wfe-monitor bounds WFE's wait on an exclusive monitor.
BTI_OFF = "max-arm-cpu.tomacco-bti=off"
# A locally administered address of the guest's one network interface.
GUEST_MAC = "52:54:00:a1:1a:01"
JUMP_PROBES = {"generic": 0, "lean": 1, "inline": 2}
JUMP_PROBE_PROPERTY = "max-arm-cpu.tomacco-jmp-probe"
WFE_WAIT_PROPERTY = "max-arm-cpu.tomacco-wfe-wait-us"
# qemu-arm-target-page-16k: the target page size, a machine option read before
# the board exists (without it tomacco-vphone runs with Arm's legacy 1 KiB
# pages), and for 16k also the CPU that reports only the 16 KiB granule.
TARGET_PAGES = {"4k": 12, "16k": 14}
GRANULE_16K = "max-arm-cpu.tomacco-granule-16k=on"
FRAME_MAILBOX_ROOT = ("tomacco", "ios-mailbox")
# A directory younger than this may belong to a launcher between creating and
# locking it; only an older, unlocked one is a dead launcher's.
FRAME_MAILBOX_STALE_SECONDS = 60


def frame_mailbox_bytes(profile):
    """Bytes of the engine's frame mailbox for frames of the panel."""
    return FRAME_MAILBOX_HEADER_BYTES + FRAME_MAILBOX_SLOTS * profile["width"] * profile["height"] * 4


def private_directory(path):
    """A real directory of this user that nobody else may enter."""
    try:
        status = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(status.st_mode) and status.st_uid == os.geteuid() and not status.st_mode & 0o077


# What the engine's scratch directory must take before a launch: more than
# the shader translator and the renderer's pipeline cache write at once.
ENGINE_SCRATCH_PROBE_BYTES = 16 << 20


def engine_scratch(engine, probe_bytes=ENGINE_SCRATCH_PROBE_BYTES):
    """The engine's TMPDIR: a private directory beside the engine, on its
    filesystem rather than a shared /tmp. The renderer translates every Metal
    shader through scratch files there and remembers a translation that failed
    for the rest of the session, so a full or over-quota /tmp (seen live:
    llvm-dis "Disk quota exceeded") left every pipeline refused and the
    display black for the whole run. The directory keeps the renderer's
    pipeline cache from boot to boot. Returns (path, None), or (path, reason)
    when it cannot take probe_bytes now."""
    directory = engine.parent / ".tomacco-engine-tmp"
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
    except OSError as error:
        return directory, f"cannot make it: {error.strerror or error}"
    if not private_directory(directory):
        return directory, ENGINE_SCRATCH_NOT_PRIVATE
    probe = directory / f".probe-{os.getpid()}"
    try:
        descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            chunk = bytes(1 << 20)
            for _ in range(max(1, probe_bytes >> 20)):
                os.write(descriptor, chunk)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        return directory, f"cannot take {probe_bytes >> 20} MiB: {error.strerror or error}"
    finally:
        try:
            probe.unlink()
        except FileNotFoundError:
            pass
    return directory, None


ENGINE_SCRATCH_NOT_PRIVATE = "it is not a private directory of this user"


def engine_scratch_message(directory, problem):
    """The launch refusal for a scratch directory engine_scratch refused, with
    the remedy that fits: a directory others may enter (seen live, made by hand
    with mode 755 beside another engine) needs its mode, not free space."""
    if problem == ENGINE_SCRATCH_NOT_PRIVATE:
        remedy = (f"Make it private with chmod 700 {directory}, or remove it so that the launcher makes it "
                  "again")
    else:
        remedy = "Free space there"
    return (f"The engine's scratch directory {directory} {problem}; the renderer translates every shader "
            f"through it and would show a black display. {remedy}")


def sweep_frame_mailboxes(root, now=None):
    """Remove the mailboxes of launchers that have ended: each live launcher
    holds an exclusive lock on its own directory for as long as it runs."""
    now = time.time() if now is None else now
    removed = []
    for entry in os.scandir(root):
        if not re.fullmatch(r"[0-9a-f]{32}", entry.name) or not entry.is_dir(follow_symlinks=False):
            continue
        try:
            if now - entry.stat(follow_symlinks=False).st_mtime < FRAME_MAILBOX_STALE_SECONDS:
                continue
            descriptor = os.open(entry.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            continue  # its launcher is still running
        else:
            shutil.rmtree(entry.path, ignore_errors=True)
            removed.append(entry.name)
        finally:
            os.close(descriptor)
    return removed


class FrameMailbox:
    """A private directory under the user's runtime directory where the engine
    publishes live frames, locked and owned by this launcher until close()."""

    def __init__(self, directory, descriptor):
        self.directory, self.descriptor = directory, descriptor
        self.path = directory / "frames"

    @classmethod
    def open(cls, environ=None):
        """The mailbox directory, or None when the user has no private,
        memory-backed runtime directory (the viewer then uses screendump)."""
        runtime = (os.environ if environ is None else environ).get("XDG_RUNTIME_DIR")
        if not runtime or not os.path.isabs(runtime) or not private_directory(runtime):
            return None
        root = Path(runtime)
        try:
            for part in FRAME_MAILBOX_ROOT:
                root = root / part
                try:
                    os.mkdir(root, 0o700)
                    os.chmod(root, 0o700)
                except FileExistsError:
                    pass
                if not private_directory(root):
                    return None
            sweep_frame_mailboxes(root)
            directory = root / secrets.token_hex(16)
            os.mkdir(directory, 0o700)
            os.chmod(directory, 0o700)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            return None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            shutil.rmtree(directory, ignore_errors=True)
            return None
        return cls(directory, descriptor)

    def receipt(self):
        return {"path": str(self.path), "format": 1, "pixel_format": "bgra8",
                "created_by": "engine", "reader": "native/ios_emulator/control.rs"}

    def close(self):
        if self.descriptor is not None:
            shutil.rmtree(self.directory, ignore_errors=True)
            os.close(self.descriptor)
            self.descriptor = None


def missing_host_libraries(engine):
    """Name the shared libraries a distribution upgrade removed under the engine.

    The loader otherwise reports them only on the engine's stderr, after the
    job has started and as exit 127. A non-dynamic or unreadable engine (or a
    host without ldd) yields no answer rather than a refusal."""
    try:
        listing = subprocess.run(["ldd", str(engine)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if listing.returncode:
        return []
    return sorted({line.split("=>")[0].strip() for line in listing.stdout.splitlines()
                   if line.rstrip().endswith("=> not found")})


# A session may last a day; its logs get 32 MiB per started six hours (a
# quiet guest writes about 11 MiB of serial log in six).
SESSION_SECONDS_MAX = 86400
LOG_BUDGET_SLICE_SECONDS = 21600


def log_budget(seconds):
    """Bytes each emulator log may reach in a session of `seconds`."""
    return (32 << 20) * max(1, -(-seconds // LOG_BUDGET_SLICE_SECONDS))


def engine_file_limit(writable_sizes, display_profile, budget=32 << 20, mailbox=False):
    """Largest file the emulator may write: the session's log budget, a writable
    disk at its existing length, one uncompressed frame of the panel or, with a
    frame mailbox, that mailbox, whichever is greatest. Rounded up with a MiB of
    margin: a frame the limit refuses kills the emulator with SIGXFSZ instead of
    failing the one capture (the engine itself never grows its mailbox past
    the limit; a larger frame is refused there and the viewer falls back)."""
    frame = frame_file_bytes(display_profile) if display_profile else 0
    if mailbox and display_profile:
        frame = max(frame, frame_mailbox_bytes(display_profile))
    frame = -(-(frame + (1 << 20)) // (1 << 20)) << 20
    return max(budget, frame, *writable_sizes)


def writable_disk_bytes(stream):
    """The most a writable disk's file may grow to: a raw disk's length, or a
    qcow2 overlay's virtual size plus its metadata (one L2 entry per cluster,
    refcounts and a margin)."""
    header = ios_disk_overlay.qcow2_header(stream.fileno())
    length = os.fstat(stream.fileno()).st_size
    if header is None:
        return length
    clusters = -(-header["size"] // (1 << header["cluster_bits"]))
    return max(length, header["size"] + clusters * 16 + (64 << 20))


def disk_node(name, filename, read_only, qcow2):
    """The -blockdev node of a guest disk. An overlay's backing chain is
    opened by QEMU from its header (absolute names), read-only."""
    node = {"driver": "qcow2" if qcow2 else "raw", "node-name": name, "read-only": read_only,
            "file": {"driver": "file", "filename": filename, "locking": "on"}}
    return json.dumps(node, separators=(",", ":"))


def child_limits(parent_pid, max_file_bytes=32 << 20):
    # A fault loop must not fill the workstation's disk or create a RAM-sized core.
    # Writable guest disks may be larger than the log budget. Their existing
    # lengths set the upper bound; logs are checked separately by the launcher.
    resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_bytes, max_file_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # A killed launcher must not leave a firmware CPU spinning in the background.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent_pid:
        os._exit(125)


def stop_child(child, grace=5):
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)


# The TCG profile plugin sums the whole session's counters and writes its
# report while the emulator exits (QEMU's plugin atexit); a phone that ran for
# hours has many blocks, so its exit gets more than the usual few seconds.
TCG_PROFILE_EXIT_SECONDS = 120
# A profile-generating engine (build_ios_paravirt.py --pgo generate, marked by
# pgo-generate.json beside it) writes and merges its .gcda/.profraw files at
# exit; only a normal exit (QMP quit, SIGTERM) writes them, SIGKILL loses the
# session's profile.
PGO_MARKER = "pgo-generate.json"
PGO_EXIT_SECONDS = 120


def engine_exit_seconds(engine, tcg_profile):
    """How long the engine may take to exit after quit before it is ended."""
    seconds = TCG_PROFILE_EXIT_SECONDS if tcg_profile else 5
    if (Path(engine).parent / PGO_MARKER).is_file():
        seconds = max(seconds, PGO_EXIT_SECONDS)
    return seconds
TCG_PROFILE_RESERVED = ("request.json", "result.json", ".request.json.tmp")


def tcg_profile_report(value, output):
    """The report path of --tcg-profile: a relative PATH is placed in the new
    --output directory, an absolute one in an existing directory."""
    output = Path(os.path.abspath(output))
    path = Path(os.path.abspath(value if value.is_absolute() else output / value))
    if path.suffix != ".json" or path.name == ".json":
        raise ValueError("--tcg-profile must name a .json file")
    if path.parent == output:
        if path.name in TCG_PROFILE_RESERVED:
            raise ValueError("--tcg-profile must not name one of the launcher's own files")
    elif not path.parent.is_dir():
        raise ValueError("--tcg-profile must be in --output or an existing directory")
    elif path.exists() or path.is_symlink():
        raise ValueError("--tcg-profile must name a new file")
    return path


def tcg_profile_plugin(prebuilt=None):
    """The plugin --tcg-profile loads: a prebuilt one, or the build of
    integrations/ios-emulator/tcg-profile.c against the pinned engine
    checkout's qemu-plugin.h (tools/ios_tcg_profile.py, cached by content)."""
    if prebuilt is not None:
        plugin = prebuilt.resolve(strict=True)
        if not plugin.is_file():
            raise ValueError("--tcg-profile-plugin must be a regular file")
        return {"plugin": str(plugin), "plugin_sha256": file_digest(plugin), "built": False}
    from ios_tcg_profile import build_plugin
    receipt = build_plugin(cache=os.environ.get("TOMACCO_TCG_PROFILE_CACHE") or None)
    return {**receipt, "built": True}


def tcg_profile_receipt(report, plugin, args):
    from ios_tcg_profile import profile_paths
    paths = profile_paths(report)
    return {"report": paths["report"], "text": paths["text"], "mark": paths["mark"],
            "windows": paths["windows"], "plugin": plugin["plugin"],
            "plugin_sha256": plugin["plugin_sha256"], "plugin_built": plugin["built"],
            "plugin_source_sha256": plugin.get("identity", {}).get("source_sha256"),
            "plugin_api_version": plugin.get("identity", {}).get("plugin_api_version"),
            "interval_seconds": args.tcg_profile_interval, "top": args.tcg_profile_top,
            "code_included": args.tcg_profile_code,
            "window_request": f"tools/ios_tcg_profile.py mark {paths['report']} --label LABEL",
            "instrumentation": "inline per-vCPU counter per translated-block entry; "
                               "interrupt/exception and idle callbacks"}


def tcg_profile_render(record, inputs):
    """Render the exit report to text next to it and summarize it in the
    receipt; kernel-half PCs are placed in the boot's own images."""
    profile = record["tcg_profile"]
    report = Path(profile["report"])
    profile["report_written"] = report.is_file()
    if not profile["report_written"]:
        return
    try:
        from ios_tcg_profile import render_files
        images = [(label, inputs[key]) for label, key in
                  (("kernel", "bootkc"), ("sptm", "sptm"), ("txm", "txm")) if key in inputs]
        profile["summary"] = render_files(report, images)
    except (OSError, ValueError, KeyError, TypeError, struct.error) as error:
        profile["render_error"] = str(error)[:500]


def check_log_budget(output, budget=32 << 20):
    for name in ("uart.log", "debug.log", "host.log", "guest-control.log"):
        path = output / name
        if path.exists() and path.stat().st_size > budget:
            raise ValueError(f"Emulator log exceeded the {budget >> 20} MiB budget")


# The renderer appends its statistics and every operation it declines to one
# fixed file. /tmp is memory on this kind of host and a phone runs for hours,
# so the file is cut back once it is large; it is a rolling diagnostic, and
# its writers append, so they simply continue at the new end.
RENDERER_LOG = Path("/tmp/reims-vgpu-fail.log")
LOG_GAP_MARKER = "\nTOMACCO_LOG_OBSERVATION_GAP\n"
# launchd's record of a process that ended abnormally: a crash signal or one
# of iOS's own exit reasons. SIGKILL/SIGTERM and RunningBoard's lifecycle
# reasons are ordinary ends and do not match, with one exception: a SIGKILL
# sent by the guest tools' own locationd guard (tomacco-quiet) is recorded
# under its sender, so a run shows how often the guard acted.
LAUNCHD_EXIT = re.compile(
    r"com\.apple\.xpc\.launchd\|[^\r\n(]{0,64}\((?P<service>[^\r\n()]{1,256}?) \[\d+\]\) <\w+>: "
    r"(?:exited due to (?P<signal>SIG(?:ABRT|BUS|EMT|FPE|ILL|SEGV|SYS|TRAP))\b|"
    r"exited due to SIGKILL \| sent by (?P<guard>tomacco-quiet)\[\d+\]|"
    r"exited with exit reason \(namespace: (?P<namespace>\d+) code: (?P<code>0x[0-9a-fA-F]+)\) - "
    r"(?P<reason>OS_REASON_[A-Z_]+)(?P<detail>[^\r\n]{0,160}))")
# Failures this emulator is known to cause: (key, service pattern or None for any, failure kinds, reason).
# They stay listed in the evidence; every other failure fails the run.
ACCOUNTED_GUEST_FAILURES = (
    # Only iOS's own apps, services and extensions: a watchdog that ends a
    # developer's app or widget is what the developer needs to see.
    ("widget-render-deadline", r"com\.apple\..+", ("extension-watchdog",),
     "chronod ends widget and control extensions that miss their deadline under emulation"),
    ("frontboard-allowance", r"com\.apple\..+", ("frontboard-watchdog",),
     "FrontBoard's launch and scene allowances run out under emulation; the guest tools scale them"),
    # The guard may end nothing but locationd; any other process it ends is
    # a guard defect and stays unaccounted.
    ("locationd-guard", r"com\.apple\.locationd", ("guard-ended",),
     "tomacco-quiet ended a locationd that had not answered CoreLocation for 90 s; launchd restarts it"),
    ("libxpc-match-assertion", r"com\.apple\.(?:intelligencetasksd|transparencyd)", ("assertion",),
     "libxpc asserts matched == (match_result.error_code == AICMR_MATCH) at start"),
    # locationd's libdispatch crashes are not here: they came from the
    # engine reordering a load-acquire before a store-release, which
    # qemu-arm-rcsc-load-acquire.patch fixes, so another one is news.
    ("mapkit-snapshot", r"com\.apple\.MapKit\.SnapshotService", ("SIGABRT",),
     "MapKit's snapshot service aborts when Maps opens"),
)
# A service that fails this often in one run is relaunched in a loop.
RESPAWN_STORM_FAILURES = 6


def run_outcome(record):
    """What a finished run amounts to, from its record and guest evidence."""
    evidence = record["guest_evidence"]
    return ("interrupted" if record.get("interrupted") else
            "observation-error" if record.get("observation_error") else
            "kernel-panic" if evidence["kernel_panic"] else
            "restore-failed" if evidence["restore_failures"] else
            "guest-control-startup-failed" if evidence["guest_control_exec_failures"] else
            # Known failures stay in the evidence without failing the run;
            # any other one does.
            "guest-process-crash" if evidence["unaccounted_failures"] else
            "hardware-fault" if evidence["bus_faults"] or evidence["system_register_faults"] else
            "device-error" if evidence["device_errors"] else
            "emulator-error" if record.get("exit_code") else
            "bounded-session" if "registers" in record else "emulator-exited")


def guest_network_arguments(mode, backend=None):
    """The virtio network device AppleVirtIONetwork binds to, on the engine's
    passt backend. passt forwards no inbound port unless it is asked to, and
    map-host-loopback=none keeps the guest away from this host's own loopback
    services, which passt would otherwise offer on the gateway's address. No
    option ROM: no firmware here boots from a network, and the engine ships none."""
    if mode != "nat":
        return []
    netdev = "passt,id=tomacco-net,quiet=on,map-host-loopback=none"
    if backend is not None:
        netdev += ",path=" + str(backend).replace(",", ",,")
    return ["-netdev", netdev,
            "-device", f"virtio-net-pci,disable-legacy=on,romfile=,netdev=tomacco-net,mac={GUEST_MAC}"]


def launchd_service(label):
    """The service a launchd record names, without domain, process and instance."""
    label = re.sub(r"^(?:user/\d+|gui/\d+|pid/\d+|system)/", "", label)
    label = re.sub(r"(?:\[[0-9a-f]+\])?\[rb-legacy\]$", "", label.removeprefix("UIKitApplication:"))
    return re.sub(r"\.[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}$", "", label)


def launchd_serial_backlog(serial):
    """Log records launchd still owed the serial console when the log ended.

    launchd queues its serial records and writes a backlog late, announced as
    "Flushed N logs" and followed by those N records under their original
    times. The console falls minutes behind under load, so a run that ends
    during a flush loses what follows: its failure search covers less of the
    run than the file suggests."""
    flushes = list(re.finditer(r"^com\.apple\.xpc\.launchd\|[^\r\n]{0,64}<\w+>: Flushed (\d+) logs \[limit: \d+\]",
                               serial, re.MULTILINE))
    if not flushes:
        return 0
    last = flushes[-1]
    written = len(re.findall(r"^com\.apple\.xpc\.launchd\|", serial[last.end():], re.MULTILINE))
    return max(0, int(last[1]) - written)


def launchd_serial_span(serial):
    """The guest time the final boot's launchd records cover, and the longest
    stretch of it without one. launchd records something every few seconds
    while the guest runs, so a long gap is a backlog the console never caught
    up with, whether or not launchd announced it."""
    times = []
    for stamp in re.findall(r"^com\.apple\.xpc\.launchd\|(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?) ",
                            serial, re.MULTILINE):
        try:
            times.append(datetime.datetime.fromisoformat(stamp).timestamp())
        except ValueError:
            continue
    # The guest clock jumps by hours when it is set from the RTC; a written
    # backlog steps back by minutes and stays in the span.
    start = max((index for index in range(1, len(times)) if abs(times[index] - times[index - 1]) > 3600),
                default=0)
    boot = sorted(times[start:])
    if not boot:
        return {"span_seconds": 0.0, "longest_gap_seconds": 0.0}
    return {"span_seconds": round(boot[-1] - boot[0], 3),
            "longest_gap_seconds": round(max((b - a for a, b in zip(boot, boot[1:])), default=0.0), 3)}


def userspace_failures(serial, restore_crashes):
    """Each service's abnormal ends, accounted for or not, and respawn storms."""
    rows = {}
    for match in LAUNCHD_EXIT.finditer(serial):
        namespace, code, reason = match["namespace"], (match["code"] or "").lower(), match["reason"]
        if match["signal"]:
            kind = match["signal"]
        elif match["guard"]:
            kind = "guard-ended"
        elif namespace == "10" and code == "0x8badf00d" and reason in (
                "OS_REASON_SPRINGBOARD", "OS_REASON_FRONTBOARD"):
            kind = "frontboard-watchdog"
        elif namespace == "20" and code == "0x3e9" and reason == "OS_REASON_WATCHDOG":
            kind = "extension-watchdog"
        elif namespace == "18" and reason == "OS_REASON_LIBSYSTEM" and "assertion failure" in match["detail"]:
            kind = "assertion"
        elif namespace == "3" and reason == "OS_REASON_CODESIGNING":
            # Code 2 is a page that no longer matches its signature.
            kind = "code-signing:" + code
        else:
            continue
        service = launchd_service(match["service"])
        row = rows.setdefault((service, kind), {"service": service, "kind": kind, "count": 0,
                                                "first": match[0][:400]})
        row["count"] += 1
    for line in restore_crashes:
        row = rows.setdefault(("unidentified", line[:120]), {"service": "unidentified", "kind": "crash",
                                                               "count": 0, "first": line[:400]})
        row["count"] += 1
    for row in rows.values():
        row["accounted"] = next((key for key, service, kinds, _reason in ACCOUNTED_GUEST_FAILURES
                                 if row["kind"] in kinds and
                                 (service is None or re.fullmatch(service, row["service"]))), None)
    per_service = {}
    for row in rows.values():
        entry = per_service.setdefault(row["service"], {"service": row["service"], "failures": 0,
                                                        "accounted": True})
        entry["failures"] += row["count"]
        entry["accounted"] = entry["accounted"] and row["accounted"] is not None
    ordered = sorted(rows.values(), key=lambda row: (row["accounted"] is not None, -row["count"]))
    return {"userspace_failures": ordered[:64],
            "unaccounted_failures": sum(row["count"] for row in rows.values() if row["accounted"] is None),
            "accounted_failures": sum(row["count"] for row in rows.values() if row["accounted"] is not None),
            "respawn_storms": sorted((entry for entry in per_service.values()
                                      if entry["failures"] >= RESPAWN_STORM_FAILURES),
                                     key=lambda entry: -entry["failures"])}


def trim_renderer_log(path=RENDERER_LOG, limit=64 << 20):
    try:
        # /tmp is shared: a FIFO planted at this name must fail the open
        # (no reader) instead of blocking the session loop on it.
        descriptor = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_size <= limit:
            return False
        os.ftruncate(descriptor, 0)
        return True
    except OSError:
        return False
    finally:
        os.close(descriptor)


def fault_contexts(debug, kinds=("bus", "sysreg")):
    """Keep separate, bounded snapshots even when AP and SEP logs interleave."""
    if LOG_GAP_MARKER in debug:
        # Register fragments on opposite sides of unread bytes cannot establish
        # one complete snapshot, even when the CPU and PC happen to agree.
        return [snapshot for section in debug.split(LOG_GAP_MARKER)
                for snapshot in fault_contexts(section, kinds)][:16]
    snapshots, active = [], {}
    hex_value = r"(0x[0-9a-f]+)"
    header = re.compile(
        rf"cpu=(\d+) pc={hex_value} kind=(bus|sysreg|user-trap) pstate={hex_value} el=([0-3]) "
        rf"tcr_el1={hex_value} ttbr0_el1={hex_value} ttbr1_el1={hex_value}")
    registers = re.compile(
        rf"cpu=(\d+) pc={hex_value} first=(\d+) values={hex_value},{hex_value},{hex_value},{hex_value}")
    for line in re.finditer(r"tomacco_arm_fault_(context|registers) ([^\r\n]*)", debug):
        if line[1] == "context":
            match = header.fullmatch(line[2])
            if not match:
                continue
            cpu, pc, kind, pstate, el, tcr, ttbr0, ttbr1 = match.groups()
            if kind not in kinds:
                continue
            active.pop(cpu, None)
            if len(snapshots) == 16:
                continue
            snapshot = {"cpu": int(cpu), "pc": pc, "kind": kind,
                        "pstate": pstate, "el": int(el), "tcr_el1": tcr,
                        "ttbr0_el1": ttbr0, "ttbr1_el1": ttbr1,
                        "registers": {}, "complete": False}
            snapshots.append(snapshot)
            active[cpu] = snapshot
        else:
            match = registers.fullmatch(line[2])
            if not match:
                continue
            cpu, pc, first, *values = match.groups()
            first = int(first)
            snapshot = active.get(cpu)
            if not snapshot or snapshot["pc"] != pc or first not in range(0, 32, 4):
                continue
            names = [f"x{i}" if i < 31 else "sp" for i in range(first, first + 4)]
            if any(name in snapshot["registers"] for name in names):
                continue
            snapshot["registers"].update(zip(names, values))
            snapshot["complete"] = len(snapshot["registers"]) == 32
    return snapshots


def guest_evidence(output):
    """Report observed failures without equating QEMU exit 0 with an iOS boot."""
    coverage = {}

    def log(name):
        path = output / name
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            coverage[name] = {"state": "missing", "complete": False, "read_bytes": 0, "ranges": []}
            return ""
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("Guest evidence logs must be regular files")
            limit = 32 << 20
            ranges = ([(0, before.st_size)] if before.st_size <= limit else
                      [(0, limit // 2), (before.st_size - limit // 2, before.st_size)])
            chunks = [os.pread(stream.fileno(), end - start, start) for start, end in ranges]
            after = os.fstat(stream.fileno())
        read_bytes = sum(map(len, chunks))
        unchanged = ((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                     (after.st_size, after.st_mtime_ns, after.st_ctime_ns))
        coverage[name] = {"state": "read", "file_bytes": before.st_size,
                          "read_bytes": read_bytes, "unchanged_during_read": unchanged,
                          "complete": unchanged and read_bytes == before.st_size,
                          "ranges": [{"start": start, "end": start + len(data)}
                                     for (start, _end), data in zip(ranges, chunks)]}
        if len(chunks) > 1:
            # Discard partial boundary lines. Do not manufacture a native log
            # line by joining the beginning and end of a large file.
            head_end = chunks[0].rfind(b"\n")
            tail_start = chunks[1].find(b"\n")
            chunks = [chunks[0][:head_end + 1] if head_end >= 0 else b"",
                      chunks[1][tail_start + 1:] if tail_start >= 0 else b""]
        return LOG_GAP_MARKER.join(chunk.decode("utf-8", errors="replace") for chunk in chunks)
    serial, debug, host = log("uart.log"), log("debug.log"), log("host.log")
    measurement = re.search(
        r"^TOMACCO_SEP_MANIFEST_MEASURED sha384=([0-9a-f]{96}) "
        r"signature_verified_by_host=false bootloader_policy_evaluated=false$", host, re.MULTILINE)
    # Parallel kernel logging can concatenate a panic with an unfinished line.
    match = re.search(r"panic\(cpu [^\r\n]+", serial)
    panic = match[0] if match else None
    faults = []
    for match in re.finditer(
            r"tomacco_arm_bus_fault pc=(0x[0-9a-f]+) physical=(0x[0-9a-f]+) "
            r"virtual=(0x[0-9a-f]+) size=(\d+) access=(\d+) mmu=(\d+) response=(\d+)(?: cpu=(\d+))?", debug):
        faults.append({key: value for key, value in zip(
            ("pc", "physical", "virtual", "size", "access", "mmu", "response", "cpu"), match.groups())
            if value is not None})
        if len(faults) == 16:
            break
    sysregs = [{key: value for key, value in zip(("pc", "instruction", "el", "cpu"), m.groups())
                if value is not None} for m in re.finditer(
        r"tomacco_arm_sysreg_fault pc=(0x[0-9a-f]+) instruction=(0x[0-9a-f]+) el=(\d+)(?: cpu=(\d+))?", debug)][:16]
    # The inherited FIFO logs "No payload" while assembling valid word-wise
    # writes too. Only explicit key failures establish a failed device request.
    device_errors = list(dict.fromkeys(re.findall(
        r"^(?:tomacco-vphone AES: [^\r\n]*unavailable|cmd_data: Invalid key length|"
        r"tomacco-vphone VSTR: command=\d+ sector=\d+ status=1)$",
        debug, re.MULTILINE)))[:16]
    usb_messages = re.findall(r"vphone USB: [^\r\n]+", host)
    disconnect = "vphone USB: host disconnected; reset required"
    # Detaching a private host bridge is a link event, not a guest device
    # failure. Keep it visible without failing an otherwise healthy system
    # boot; restore/service clients still report any interrupted operation.
    device_errors += list(dict.fromkeys(message for message in usb_messages if message != disconnect))[:16]
    storage = re.findall(r"tomacco-vphone VSTR: command=(\d+) sector=\d+ status=(\d+)", debug)
    active_states = re.findall(r"SEP/OS is alive in (\d+) state", serial)
    root_device = re.search(r"BSD root: ([A-Za-z0-9]+), major \d+, minor \d+", serial)
    mounted_volumes = list(dict.fromkeys(re.findall(
        r"apfs_log_op_with_proc:\d+: ([A-Za-z0-9]+) mount-complete volume "
        r"([^\r\n]{1,255}?), requested by:", serial)))[:16]
    # launchd can be the only source of a crash report: libxpc/dyld can trap
    # before an application installs its own signal or exception diagnostics.
    # Normal termination and memory-pressure SIGKILL are not crash evidence.
    user_crashes = list(dict.fromkeys(re.findall(
        r"(?:\*\*\* Terminating app due to uncaught exception [^\r\n]+|"
        r"Caught signal \d+ \([^\r\n]+|"
        r"com\.apple\.xpc\.launchd\|[^\r\n]{1,512}? "
        r"<\w+>: exited due to SIG(?:ABRT|BUS|EMT|FPE|ILL|SEGV|SYS|TRAP)\b[^\r\n]{0,1024})",
        serial)))[:16]
    # A watchdog termination uses SIGKILL, which alone is also a normal
    # lifecycle/memory-pressure event. Retain only launchd's explicit native
    # SpringBoard/FrontBoard or explicit watchdog-namespace reason, not
    # arbitrary SIGKILL lines. Chronod's observed widget-extension watchdog
    # uses namespace 20/code 0x3e9 with OS_REASON_WATCHDOG.
    watchdog_records = list(dict.fromkeys(re.findall(
        r"com\.apple\.xpc\.launchd\|[^\r\n]{1,512}? "
        r"<\w+>: exited with exit reason \(namespace: "
        r"(?:10 code: 0x8[bB][aA][dD][fF]00[dD]\) - "
        r"OS_REASON_(?:SPRINGBOARD|FRONTBOARD)|"
        r"20 code: 0x3[eE]9\) - OS_REASON_WATCHDOG)"
        r"[^\r\n]{0,2048}", serial)))
    user_watchdogs = watchdog_records[:16]
    # A rejected exec never produces a signal crash or a control socket.
    # Retain launchd's definitive rejection of our selected native service;
    # emulator exit zero must not report this start as a usable guest run.
    control_exec_failures = list(dict.fromkeys(re.findall(
        r"com\.apple\.xpc\.launchd\|[^\r\n]{1,512}?"
        r"io\.github\.marselnenaj\.tomacco\.application-inventory \[\d+\]\) "
        r"<\w+>: (?:removing service since it exited with consistent failure - OS_REASON_EXEC|"
        r"exited with exit reason \([^\r\n]{1,128}\) - OS_REASON_EXEC)[^\r\n]{0,512}",
        serial)))[:16]
    completed = list(dict.fromkeys(re.findall(
        r"CHECKPOINT END: (?:[A-Z][A-Z0-9_]{0,31}|\(null\)):\[0x[0-9a-fA-F]+\] ([A-Za-z0-9_]+)", serial)))[:256]
    sealed_root = re.search(
        r"(com\.apple\.os\.update-[0-9A-F]{64})@(/dev/disk\d+s\d+) on / \(apfs, sealed, [^\r\n]+\)", serial)
    ignition_failure = re.search(
        r"libignition: (\d+):\s+([^:\r\n]{1,64}): ignition failed: (\d+)", serial)
    cache_failure = re.search(r"dyld\[(\d+)\]: (dyld cache [^\r\n]{1,2048}not loaded:[^\r\n]{0,2048})", serial)
    restore_failures, ordered_failures = [], []
    for match in re.finditer(
            r"CHECKPOINT FAILURE:\(FAILURE:(-?\d+)\) (?:[A-Z][A-Z0-9_]{0,31}|\(null\)):\[(0x[0-9a-fA-F]+)\] "
            r"([A-Za-z0-9_]+)([^\r\n]*)", serial):
        failure = {"code": int(match[1]), "checkpoint": match[2],
                   "step": match[3], "detail": match[4].strip()[:1024]}
        ordered_failures.append((match.start(), failure))
        if len(ordered_failures) == 32:
            break
    # ASR runs asynchronously and can fail before the outer checkpoint emits
    # FAILURE. Retain that first cause even when restored then requests a retry.
    for match in re.finditer(
            r"\[AsyncCP\]\[End\] Checkpoint ([A-Za-z0-9_]{1,128}) finished with result: (-?\d+)\.", serial):
        if int(match[2]) != 0:
            ordered_failures.append((match.start(), {"code": int(match[2]), "checkpoint": "async",
                                    "step": match[1], "detail": "Original asynchronous checkpoint failed"}))
        if len(ordered_failures) >= 64:
            break
    for _, failure in sorted(ordered_failures, key=lambda item: item[0]):
        if failure not in restore_failures:
            restore_failures.append(failure)
        if len(restore_failures) == 16:
            break
    return {"log_coverage": coverage,
            "failure_search_complete": all(row["complete"] for row in coverage.values()),
            "kernel_panic": panic, "bus_faults": faults, "fault_contexts": fault_contexts(debug),
            # Original EL0 traps may be handled by the guest. These snapshots
            # are diagnostics, never evidence of a process crash on their own.
            "user_exception_contexts": fault_contexts(debug, ("user-trap",)),
            "user_exception_events": [
                {"cpu": int(m[1]), "pc": m[2], "syndrome": m[3],
                 "exception": int(m[4]), "target_el": int(m[5]), "sequence": int(m[6])}
                for m in re.finditer(
                    r"tomacco_arm_user_exception cpu=(\d+) pc=(0x[0-9a-f]+) syndrome=(0x[0-9a-f]+) "
                    r"exception=(\d+) target_el=(\d+) sequence=(\d+)", debug)
            ][:128],
            "userspace_crashes": user_crashes,
            # Failures in records launchd never wrote are not in this search.
            "launchd_serial_records_unwritten": launchd_serial_backlog(serial),
            "launchd_serial_coverage": launchd_serial_span(serial),
            **userspace_failures(serial, re.findall(
                r"\*\*\* Terminating app due to uncaught exception [^\r\n]+|Caught signal \d+ \([^\r\n]+", serial)),
            "userspace_watchdog_terminations": user_watchdogs,
            "userspace_watchdog_distinct_record_count": len(watchdog_records),
            "userspace_watchdog_samples_truncated": len(watchdog_records) > len(user_watchdogs),
            "guest_control_exec_failures": control_exec_failures,
            "restore_completed_steps": completed, "restore_failures": restore_failures,
            "system_register_faults": sysregs, "device_errors": device_errors,
            "amfi_identified_vm": "AMFI: Booted in a VM" in serial,
            "psci_driver_started": "ApplePSCI - started" in serial,
            "iosurface_driver_initialized": "APV-IOSFC: success" in serial,
            "sep_firmware_started": "tomacco-vphone SEP: start pc=" in debug,
            "sep_manifest_measurement": {
                "sha384": measurement[1], "signature_verified_by_host": False,
                "bootloader_policy_evaluated": False} if measurement else None,
            "sep_boot_capsule_submitted": bool(re.search(
                r"tomacco-vphone SEP mailbox 0 -> 1: [0-9a-f]{14}fe ", debug)),
            "sepos_version": next(iter(re.findall(r"Root task vers: (AppleSEPOS-[^\r\n]+)", serial)), None),
            "sep_os_active_state": int(active_states[-1]) if active_states else None,
            "sep_fips_reseed_completed": "AppleSEPManager::endReseed()_block_invoke: FIPS reseed completed" in serial,
            "sep_storage_reads": storage.count(("0", "0")),
            "sep_storage_writes": storage.count(("1", "0")),
            "sep_storage_failures": sum(status != "0" for _, status in storage),
            "nvram_access_available": "NVRAM access available on initial check" in serial,
            "restore_usb_enable_requested": "enabling USB (blocking) with description: standardRestore" in serial,
            "restore_usb_enabled": "enabling USB finished: success (0)" in serial,
            "system_usb_configured": "USB configuration complete!" in serial,
            "usb_host_disconnects": usb_messages.count(disconnect),
            "usb_reset_required_after_disconnect": disconnect in usb_messages,
            "restore_waiting_for_host": "waiting for host to trigger start of restore" in serial,
            "root_device": root_device[1] if root_device else None,
            "apfs_mounted_volumes": [{"device": device, "name": name}
                                     for device, name in mounted_volumes],
            "system_volume_root_hash_missing": "could not retrieve system-volume-auth-blob from device tree" in serial,
            "root_snapshot_lookup_failed": "failed to find named root snapshot:" in serial,
            "sealed_system_root": {"snapshot": sealed_root[1], "device": sealed_root[2]} if sealed_root else None,
            "system_volume_hash_validated": bool(re.search(
                r"authenticate_root_hash:\d+: disk\d+s\d+ successfully validated on-disk root hash", serial)),
            "cryptex_grafts": [{"cryptex": name,
                                "content_hash_validated": bool(re.search(
                                    rf"authenticate_root_hash:\d+: {name}\.dmgs\d+ successfully validated on-disk root hash", serial)),
                                "grafted": bool(re.search(
                                    rf"libignition: 1:\s+graft: {name}: grafted cryptex:[^\r\n]+ignored error = 0(?:\r|\n|$)", serial))}
                               for name in ("os", "app")],
            "ignition_failure": {"pid": int(ignition_failure[1]), "stage": ignition_failure[2].strip(),
                                 "code": int(ignition_failure[3])} if ignition_failure else None,
            "dyld_cache_failure": {"pid": int(cache_failure[1]), "detail": cache_failure[2]} if cache_failure else None,
            "dyld_shared_cache_mapped": "dyld[1]: dyld cache mapped system-wide:" in serial,
            "launchd_service_manager_started": bool(re.search(
                r"com\.apple\.xpc\.launchd\|[^\r\n]+hello, launchd UUID:", serial)),
            "launchd_service_cache_validated": "Cache sent to launchd successfully" in serial,
            "early_boot_setup_completed": "****** DONE EARLYBOOT SETUP ****" in serial,
            "early_boot_completed": "Early boot complete. Continuing system boot." in serial,
            "darwin_init_missing_configuration": "DarwinInit.ConfigLoader.Error.missingSystemConfiguration" in serial,
            **pcc_guest_evidence(serial),
            "root_volume_authentication_failed": bool(panic and "rootvp not authenticated after mounting" in panic),
            "apfs_ramdisk_mounted": "mount-complete volume ramdisk" in serial,
            "launchd_started": "hello from launchd.1" in serial}


def parse_display_profile(raw):
    """Physical panel contract shared with reims' configuration owner."""
    match = re.fullmatch(r"([0-9]{1,5})x([0-9]{1,5})@([0-9]{1,5}):([0-9]{1,5})x([0-9]{1,5})", raw)
    if not match:
        raise argparse.ArgumentTypeError("Display profile must be WIDTHxHEIGHT@HZ:WIDTH_MMxHEIGHT_MM")
    width, height, hz, width_mm, height_mm = map(int, match.groups())
    if not (64 <= width <= 8192 and 64 <= height <= 8192 and 1 <= hz <= 120
            and 10 <= width_mm <= 2000 and 10 <= height_mm <= 2000):
        raise argparse.ArgumentTypeError("Display pixels must be 64..8192, Hz 1..120, millimetres 10..2000")
    if abs(width * height_mm - height * width_mm) * 2 > width + height:
        raise argparse.ArgumentTypeError("Display pixel and physical aspect ratios disagree beyond millimetre rounding")
    return {"width": width, "height": height, "refresh_hz": hz,
            "width_mm": width_mm, "height_mm": height_mm}


# Apple's device tree for the virtual iPhone carries "gestalt variants": sets
# of four-character tags that describe it as a particular iPhone. The boot
# loader would copy the selected variant into the product description; this
# launcher boots the kernel directly, so it does that itself. Each tag below
# has, in the device tree of the real iPhone 16, the product property of the
# same value.
VARIANT_PROPERTIES = {
    "ards": "artwork-device-subtype", "arsf": "artwork-scale-factor", "dcrd": "display-corner-radius",
    "isnl": "island-notch-location", "oled": "oled-display", "thin": "thin-bezel", "crid": "chrome-identifier",
    "fbid": "framebuffer-identifier", "fcof": "front-cam-offset-from-center", "rcof": "rear-cam-offset-from-center",
    "sblo": "side-button-location", "vdbl": "volume-down-button-location", "vubl": "volume-up-button-location",
    "uiba": "ui-background-quality", "uiwe": "ui-weather-quality", "uipi": "ui-pip", "uire": "ui-reachability",
    "larg": "large-format-phone", "codf": "compatible-device-fallback", "prde": "product-description"}


def parse_device_model(raw):
    """A device description: {"variant": NAME} plus optional overrides of it."""
    try:
        model = json.loads(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("Device model must be a JSON object") from None
    allowed = {"variant", "artwork_subtype", "corner_radius", "island_bottom", "large_format"}
    if not isinstance(model, dict) or set(model) - allowed or "variant" not in model:
        raise argparse.ArgumentTypeError("Device model needs variant and only known overrides")
    if not isinstance(model["variant"], str) or not re.fullmatch(r"[a-z0-9]{1,24}", model["variant"]):
        raise argparse.ArgumentTypeError("Device variant must be a short lower-case name")
    limits = {"artwork_subtype": (480, 8192), "corner_radius": (0, 200), "island_bottom": (0, 1000)}
    for name, (low, high) in limits.items():
        value = model.get(name)
        if name == "island_bottom" and name in model and value is None:
            continue  # explicitly no island or notch
        if name in model and (type(value) is not int or not low <= value <= high):
            raise argparse.ArgumentTypeError(f"Device model {name} must be an integer from {low} to {high}")
    if "large_format" in model and type(model["large_format"]) is not bool:
        raise argparse.ArgumentTypeError("Device model large_format must be true or false")
    return model


def device_model_handoff(tree, model):
    """Describe the virtual iPhone as the selected variant, as the boot loader would.

    The variant itself is Apple's data from the tree. Overrides describe other
    panels of the same family and are derived, not Apple's."""
    product = tree.child("product")
    lists = [node for node in product.children if node.props.get("compatible")
             and node.get("compatible").rstrip(b"\0") == b"gestalt-variant-list"]
    variants = [node for group in lists for node in group.children if node.name == model["variant"] + "-variant"]
    if len(variants) != 1:
        raise ValueError(f"This device tree has no gestalt variant {model['variant']}")
    variant = variants[0]
    applied = {}
    for tag, name in VARIANT_PROPERTIES.items():
        if "tag-" + tag in variant.props:
            product.set(name, variant.get("tag-" + tag))
            applied[name] = tag
    if "tag-home" in variant.props:
        tree.child("buttons").set("home-button-type", variant.get("tag-home"))
        applied["home-button-type"] = "home"
    overrides = {}
    if "artwork_subtype" in model:
        product.set("artwork-device-subtype", struct.pack("<I", model["artwork_subtype"]))
        overrides["artwork-device-subtype"] = model["artwork_subtype"]
    if "corner_radius" in model:
        # The variant's own value has the same shape: radius, then 1.
        product.set("display-corner-radius", struct.pack("<II", model["corner_radius"], 1))
        overrides["display-corner-radius"] = model["corner_radius"]
    if "island_bottom" in model:
        if model["island_bottom"] is None:
            product.props.pop("island-notch-location", None)
        else:
            product.set("island-notch-location", struct.pack("<I", model["island_bottom"]))
        overrides["island-notch-location"] = model["island_bottom"]
    if "large_format" in model:
        product.set("large-format-phone", struct.pack("<I", int(model["large_format"])))
        overrides["large-format-phone"] = int(model["large_format"])
    return {"variant": model["variant"], "properties": sorted(applied), "overrides": overrides,
            "home_button": "tag-home" not in variant.props}


# The panel's density selects the scale iBoot writes to /chosen/display-scale,
# which MobileGestalt answers as main-screen-scale. This launcher boots the
# kernel directly, so without this handoff the tree keeps its placeholder 0:
# IconServices then sizes its bitmap contexts to zero and Settings traps on
# its first commit, and the window server runs a 2x virtual mode at 167
# points per inch instead of the native mode (428x926 points on the 1179x2556
# panel). Apple's 2x phones are
# 326 ppi, its 3x phones 401-460 ppi; a monitor's density (the desktop mode
# list, 102 ppi) is 1x.
DISPLAY_SCALE_DENSITIES = ((400, 3), (200, 2))


def pixels_per_inch(profile):
    """A display profile's density along its width (pixels over millimetres)."""
    return profile["width"] / (profile["width_mm"] / 25.4)


def display_scale_for_profile(profile):
    density = pixels_per_inch(profile)
    for minimum, scale in DISPLAY_SCALE_DENSITIES:
        if density >= minimum:
            return scale
    return 1


def display_scale_handoff(tree, scale):
    """Write the panel's scale where iBoot would, once: the prepared tree's
    placeholder 0 (or a missing property) becomes the scale and the same
    scale is left alone; another value, an iBoot expression or an odd size is
    refused, so a tree prepared for a different panel is never silently
    redescribed."""
    if type(scale) is not int or not 1 <= scale <= 3:
        raise ValueError("Display scale must be 1, 2 or 3")
    chosen = tree.child("chosen")
    prop = chosen.props.get("display-scale")
    previous = None
    if prop is not None:
        if prop.flags or len(prop.data) != 4:
            raise ValueError("Prepared tree has no literal four-byte /chosen/display-scale")
        previous = int.from_bytes(prop.data, "little")
        if previous not in (0, scale):
            raise ValueError(f"Prepared tree already names display scale {previous}, not the panel's {scale}")
    if previous != scale:
        chosen.set("display-scale", struct.pack("<I", scale))
    return {"scale": scale, "previous": previous, "property_updated": previous != scale}


# How long the launcher waits for one careful stop. The control plane needs up
# to about 210 s for it on a loaded guest: its wait for the control lane, the
# status query and both flush deadlines, which the tests read from native/.
# Cutting it off earlier loses the very stop this exists for.
SAVED_STOP_SECONDS = 240


def saving_margin(seconds, cli, interactive, guest_control, job):
    """Seconds before the session limit at which an interactive session with
    guest control saves and stops itself; None when it cannot. A long session
    gets room for a whole attempt, the pause and a second whole attempt."""
    if cli is None or not interactive or guest_control is None or not job:
        return None
    return min(600, seconds // 4)


# How often a session looks at the card's free memory (gpu_memory_short).
GPU_MEMORY_CHECK_SECONDS = 5


GPU_MEMORY_RECORD = "gpu-memory.json"


def gpu_memory_record(memory, short_checks, stopping, now):
    """What the graphics memory guard saw at its last check, for
    ios.virtual.status: the card's used and total bytes (None when it reports
    none), the free bytes below which a check counts as short, how many short
    checks in a row there were (two stop the session) and whether it did."""
    used, total = memory if memory is not None else (None, None)
    return {"format": 1, "checked": round(now, 3), "used_bytes": used, "total_bytes": total,
            "free_bytes": None if memory is None else max(0, total - used),
            "limit_free_bytes": None if not total else total // 8,
            "short_checks": short_checks, "stopping": stopping}


def save_gpu_memory(output, record):
    """Publish the guard's last check beside the result; a record that cannot
    be written costs the session nothing."""
    temporary = output / f".{GPU_MEMORY_RECORD}.tmp"
    try:
        with temporary.open("w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(record, stream)
        os.replace(temporary, output / GPU_MEMORY_RECORD)
    except OSError:
        pass


def saved_stop(cli, job):
    """Ask the control plane to stop this very guest the careful way: flush its
    storage, record that, then let the emulator exit. Used before the session
    limit, which would otherwise freeze the guest without saving."""
    try:
        done = subprocess.run([str(cli), "call", "ios.emulator.control",
                               json.dumps({"job_id": job, "action": "stop"})],
                              capture_output=True, text=True, timeout=SAVED_STOP_SECONDS)
        reply = json.loads(done.stdout)
    except subprocess.TimeoutExpired:
        # Not a refusal: the control plane did not finish and was cut off, so
        # the guest may be halfway through its flush.
        return {"requested": True, "accepted": False, "timed_out": True,
                "error": f"No answer from the control plane within {SAVED_STOP_SECONDS} s"}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return {"requested": True, "accepted": False, "error": str(error)[:200]}
    if reply.get("ok"):
        result = reply["result"]["result"]
        return {"requested": True, "accepted": True,
                "guest_storage_flushed": result.get("guest_storage_flushed") is True}
    return {"requested": True, "accepted": False, "error": reply.get("error", {}).get("code")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--guest-control-backend", type=Path,
                        help="Local vhost-device-vsock executable. Requires shared-memory engine support and owned guest tools built with --agent; no IP networking")
    parser.add_argument("--guest-network", choices=("off", "nat"), default="off",
                        help="nat: a virtio network device behind passt (outbound only, no inbound port)")
    parser.add_argument("--guest-network-backend", type=Path,
                        help="The passt executable --guest-network nat uses; default: passt on PATH")
    for name in ("kernel", "sptm", "txm", "device-tree", "ramdisk", "trustcache"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New directory for private logs and receipts")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--clock-mode", choices=("wall", "instruction"), default="wall",
                        help="Guest time: wall clock uses threaded TCG for all enabled AP cores (default); instruction mode serializes TCG for timing diagnostics")
    parser.add_argument("--translation-cache-mib", type=int,
                        help="Requested TCG translation buffer in MiB (1–4096; above 2048 requires verified wide chaining); omitted uses the engine default. Consumes host memory; the engine may apply its host-architecture limit")
    parser.add_argument("--pointer-auth", choices=("fast", "helpers"), default="fast",
                        help="Pointer-authentication bypass: fast (default; the engine's own choice, which "
                             "is helper-free with qemu-arm-pauth-bypass-inline), or helpers, the per-"
                             "instruction helper calls of earlier engines, for an A/B on one engine "
                             "(requires the patch's tomacco-pauth-fast CPU property)")
    parser.add_argument("--bti", choices=("keep", "off"), default="keep",
                        help="Branch Target Identification of the emulated CPU: keep (default) or off, a "
                             "CPU without FEAT_BTI (guest-visible ID register; TCG then tracks no BTYPE; "
                             "requires qemu-arm-bti-property)")
    parser.add_argument("--jump-probe", choices=("engine", *JUMP_PROBES), default="engine",
                        help="Jump-cache probe at TB exits (requires qemu-arm-jump-probe): engine (default: "
                             "the engine's own choice, lean with the patch), generic (the out-of-line "
                             "helper, as upstream), lean or inline")
    parser.add_argument("--wfe-wait-us", type=int, metavar="US",
                        help="Bound of a WFE's wait on its exclusive monitor in microseconds (0-100000; 0: "
                             "return at once as upstream); omitted uses the engine default (requires "
                             "qemu-arm-wfe-monitor)")
    parser.add_argument("--target-page", choices=("engine", *TARGET_PAGES), default="engine",
                        help="Target page size of the engine (requires qemu-arm-target-page-16k): engine "
                             "(default: the board's, 1 KiB for tomacco-vphone), 4k, or 16k, which also "
                             "reports only the 16 KiB translation granule (guest-visible ID register)")
    parser.add_argument("--console-paint", choices=("lazy", "eager"), default="lazy",
                        help="QEMU console surface: lazy (default; with reims-lazy-console-paint and a frame "
                             "mailbox, copied only when a screendump or display refresh asks), or eager, "
                             "copied on every present as before")
    parser.add_argument("--host-cpu-policy", choices=("scheduler", "performance"), default="scheduler",
                        help="Host CPU placement: scheduler (default), or separate highest-frequency physical cores for AP threads; unsupported hosts keep scheduler placement")
    parser.add_argument("--input-transport", choices=("virtio", "usb-hid"), default="virtio",
                        help="Interactive input: VirtIO keyboard/mouse, or USB keyboard/Home/single-contact touchscreen")
    parser.add_argument("--display-profile", type=parse_display_profile,
                        help="One fixed panel, e.g. 1170x2532@60:65x141 (pixels, Hz, physical mm); requires an engine with reims-display-panel support. Its density also fills in the tree's /chosen/display-scale (3 from 400 ppi, 2 from 200 ppi, else 1), as iBoot would")
    parser.add_argument("--device-model", type=parse_device_model,
                        help='Describe the device as a gestalt variant of its own device tree, e.g. {"variant":"d47vm2025vre"}; optional artwork_subtype, corner_radius, island_bottom, large_format override it for other panels')
    parser.add_argument("--cli", type=Path,
                        help="Control-plane executable. An interactive session with guest control then saves and stops itself shortly before --seconds instead of being frozen at the limit")
    parser.add_argument("--trace-exceptions", action="store_true")
    parser.add_argument("--trace-storage", action="store_true",
                        help="Diagnostic only: trace every VirtIO disk request within the session's log budget")
    parser.add_argument("--tcg-profile", type=Path, metavar="PATH",
                        help="Diagnostic: load the TCG profile plugin (integrations/ios-emulator/tcg-profile.c, "
                             "built on demand by tools/ios_tcg_profile.py) and write which guest code the vCPUs "
                             "executed to PATH (.json) at exit, rendered to PATH's .txt with kernel-half PCs "
                             "placed in the boot's kernelcache; a relative PATH is placed in --output. "
                             "tools/ios_tcg_profile.py mark PATH writes a window (the change since the previous "
                             "one) while the guest runs. Costs about 0.35 ns per translated-block entry")
    parser.add_argument("--tcg-profile-plugin", type=Path,
                        help="A prebuilt tcg-profile plugin instead of building one")
    parser.add_argument("--tcg-profile-interval", type=float, metavar="SECONDS",
                        help="With --tcg-profile, also write a window every SECONDS (1–3600)")
    parser.add_argument("--tcg-profile-top", type=int, metavar="N",
                        help="With --tcg-profile, hot blocks, pages and exception origins per report (1–100000; default 300)")
    parser.add_argument("--tcg-profile-code", action="store_true",
                        help="With --tcg-profile, include the hottest blocks' instruction words in the private reports")
    parser.add_argument("--window", action="store_true", help="Enable reims' native host display window")
    parser.add_argument("--interactive", action="store_true",
                        help="Attach the selected keyboard/pointer devices and expose an owned local control monitor")
    parser.add_argument("--boot-args",
                        help="Explicit research kernel command line, replacing the RAM-disk boot defaults")
    parser.add_argument("--usb", action="store_true", help="Expose Apple VirtIO USB through a private local socket")
    parser.add_argument("--boot-manifest", type=Path,
                        help="Explicit local DER IM4M whose SHA-384 is handed to the AP boot environment; does not authenticate it")
    parser.add_argument("--system-volume-root-hash", type=Path,
                        help="Original v2 isys IM4P for a sealed system disk; supplies APFS root-hash data without authenticating its signature")
    parser.add_argument("--pcc-hostname",
                        help="Deliver a local PCC hostname via darwin-init NVRAM; retains any existing configuration and security controls")
    parser.add_argument("--sep", type=Path, help="Experimental isolated processor for unwrapped vphone600 SEP firmware")
    parser.add_argument("--sep-manifest", type=Path,
                        help="Raw DER IM4M for SEP direct entry; signature and device binding remain unqualified")
    parser.add_argument("--sep-measure-manifest", action="store_true",
                        help="Research only: hand SEP the actual IM4M SHA-384 without evaluating the bootloader trust policy")
    parser.add_argument("--sep-storage", type=Path,
                        help="Existing writable raw SEP storage (typically 512 KiB); retained across boots")
    parser.add_argument("--sep-boot-role", choices=("normal", "restore"),
                        help="SEP payload role: sepi normal boot (default) or rsep restore initialization")
    parser.add_argument("--sep-handoff", choices=("preloaded", "userspace"),
                        help="Must match the prepared AP firmware handoff (default: preloaded)")
    for kind in ("auxiliary", "root"):
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--" + kind + "-disk", type=Path,
                           help="Read-only raw image shared by firmware and Apple VirtIO storage")
        group.add_argument("--writable-" + kind + "-disk", type=Path,
                           help="Existing persistent raw image; guest writes are retained across boots")
    args = parser.parse_args()
    if args.input_transport != "virtio" and not args.interactive:
        parser.error("--input-transport usb-hid requires --interactive")
    if not 1 <= args.seconds <= SESSION_SECONDS_MAX:
        parser.error(f"--seconds must be between 1 and {SESSION_SECONDS_MAX}")
    if args.translation_cache_mib is not None and not 1 <= args.translation_cache_mib <= 4096:
        parser.error("--translation-cache-mib must be between 1 and 4096")
    if args.wfe_wait_us is not None and not 0 <= args.wfe_wait_us <= 100000:
        parser.error("--wfe-wait-us must be between 0 and 100000")
    if args.tcg_profile is None and (args.tcg_profile_plugin is not None or args.tcg_profile_code or
                                     args.tcg_profile_interval is not None or args.tcg_profile_top is not None):
        parser.error("--tcg-profile-plugin, --tcg-profile-interval, --tcg-profile-top and --tcg-profile-code "
                     "require --tcg-profile")
    if args.tcg_profile_interval is not None and not 1 <= args.tcg_profile_interval <= 3600:
        parser.error("--tcg-profile-interval must be between 1 and 3600 seconds")
    if args.tcg_profile_top is not None and not 1 <= args.tcg_profile_top <= 100000:
        parser.error("--tcg-profile-top must be between 1 and 100000")
    tcg_profile = tcg_profile_plugin_receipt = None
    if args.tcg_profile is not None:
        try:
            tcg_profile = tcg_profile_report(args.tcg_profile, args.output.absolute())
            tcg_profile_plugin_receipt = tcg_profile_plugin(args.tcg_profile_plugin)
        except (OSError, ValueError) as error:
            parser.error(str(error))
    if args.boot_args is not None and (not 1 <= len(args.boot_args) <= 1023 or
                                      any(not 32 <= ord(c) <= 126 for c in args.boot_args)):
        parser.error("--boot-args must contain 1–1023 printable ASCII bytes")
    if args.sep_manifest is not None and args.sep is None:
        parser.error("--sep-manifest requires --sep")
    if args.sep_measure_manifest and args.sep_manifest is None:
        parser.error("--sep-measure-manifest requires --sep-manifest")
    if args.sep_storage is not None and args.sep is None:
        parser.error("--sep-storage requires --sep")
    if args.sep_boot_role is not None and args.sep is None:
        parser.error("--sep-boot-role requires --sep")
    if args.sep_handoff is not None and args.sep is None:
        parser.error("--sep-handoff requires --sep")
    if args.sep_handoff == "userspace" and args.sep_boot_role != "restore":
        parser.error("--sep-handoff userspace requires --sep-boot-role restore")
    if args.pcc_hostname is not None:
        hostname_config(args.pcc_hostname)
        if (args.system_volume_root_hash is None or not (args.root_disk or args.writable_root_disk)
                or args.sep_boot_role == "restore"):
            parser.error("--pcc-hostname requires a normal sealed-system boot with a root disk")
    engine = args.engine.resolve(strict=True)
    inputs = {"bootkc": args.kernel, "sptm": args.sptm, "txm": args.txm,
              "dtree": args.device_tree, "ramdisk": args.ramdisk, "tc": args.trustcache}
    if args.sep is not None:
        inputs["sep"] = args.sep
    if args.sep_manifest is not None:
        inputs["sep-manifest"] = args.sep_manifest
    inputs = {key: path.resolve(strict=True) for key, path in inputs.items()}
    disks = {name: path.resolve(strict=True) for name, path in
             (("tomacco-aux", args.auxiliary_disk), ("tomacco-root", args.root_disk)) if path is not None}
    writable = {name: path.resolve(strict=True) for name, path in
                (("tomacco-aux", args.writable_auxiliary_disk),
                 ("tomacco-root", args.writable_root_disk)) if path is not None}
    storage_path = args.sep_storage.resolve(strict=True) if args.sep_storage is not None else None
    if not engine.is_file() or not os.access(engine, os.X_OK):
        parser.error("--engine must be an executable regular file")
    missing = missing_host_libraries(engine)
    if missing:
        parser.error("The engine cannot start; this host no longer has " + ", ".join(missing) +
                     ". Rebuild it with tools/build_ios_paravirt.py")
    for path in (*inputs.values(), *disks.values()):
        if not path.is_file() or path.stat().st_size == 0:
            parser.error(f"Boot input must be a nonempty regular file: {path}")
    with inputs["dtree"].open("rb") as stream:
        tree = decode(stream.read(MAX_TREE_BYTES + 1))
    chosen = tree.child("chosen")
    if chosen.get("firmware-version") != FIRMWARE:
        parser.error("Prepare the device tree with tools/prepare_ios_paravirt.py")
    try:
        ap_cpus = ap_cpu_count(tree)
    except ValueError as error:
        parser.error(str(error))
    if args.boot_args is not None:
        cpu_args = re.findall(r"(?<!\S)cpus=(\d+)(?=\s|$)", args.boot_args)
        if cpu_args and (len(cpu_args) != 1 or int(cpu_args[0]) != ap_cpus):
            parser.error(f"--boot-args cpus= must match the prepared tree's {ap_cpus} AP CPUs")
    identity = identity_handoff(tree)
    display = display_handoff(tree)
    device_model = None
    if args.device_model is not None:
        try:
            device_model = device_model_handoff(tree, args.device_model)
        except ValueError as error:
            parser.error(str(error))
    display_scale = None
    if args.display_profile is not None:
        try:
            display_scale = display_scale_handoff(tree, display_scale_for_profile(args.display_profile))
        except ValueError as error:
            parser.error(str(error))
        display_scale["pixels_per_inch"] = round(pixels_per_inch(args.display_profile), 1)
    boot_manifest_path = args.boot_manifest.resolve(strict=True) if args.boot_manifest else None
    boot_manifest = boot_manifest_data = None
    if boot_manifest_path:
        if not boot_manifest_path.is_file():
            parser.error("--boot-manifest must be a regular DER IM4M file")
        with boot_manifest_path.open("rb") as stream:
            boot_manifest_data = stream.read(1024 * 1024 + 1)
        boot_manifest = boot_manifest_handoff(tree, boot_manifest_data)
    root_hash_path = args.system_volume_root_hash.resolve(strict=True) if args.system_volume_root_hash else None
    root_hash = root_hash_data = None
    if root_hash_path:
        if not root_hash_path.is_file():
            parser.error("--system-volume-root-hash must be a regular isys IM4P file")
        with root_hash_path.open("rb") as stream:
            root_hash_data = stream.read(65537)
        root_hash = system_volume_handoff(tree, root_hash_data)
    sep = tree.child("arm-io").child("sep")
    maps = chosen.child("memory-map")
    if args.sep is not None:
        sep_valid = ("sepfw-booted" in sep.props and sep.get("sepfw-booted") == b"\x01\0\0\0"
                     and "SEPFW" in maps.props and maps.get("SEPFW") == bytes(16))
        if args.sep_handoff == "userspace":
            sep_valid = (sep_valid and "sepfw-loaded" not in sep.props
                         and "sepfw-load-at-boot" in chosen.props and chosen.get("sepfw-load-at-boot") == bytes(4)
                         and "osenvironment" in chosen.props
                         and chosen.get("osenvironment") == b"device-recovery".ljust(32, b"\0"))
        else:
            sep_valid = sep_valid and "sepfw-loaded" in sep.props and sep.get("sepfw-loaded") == b"\x01\0\0\0"
    else:
        sep_valid = not (set(sep.props) & {"sepfw-loaded", "sepfw-booted"} or "SEPFW" in maps.props)
    if not sep_valid:
        parser.error("Use prepare_ios_paravirt.py --load-sep together with run_ios_paravirt.py --sep and matching --sep-handoff")
    memory = int.from_bytes(chosen.get("dram-size"), "little")
    if memory % (1 << 30) or not (2 << 30) <= memory <= (32 << 30):
        parser.error("Unsupported prepared RAM size")
    # One session for this engine, even when output directories differ. flock
    # is released by the kernel after a crash; no stale PID can block a restart.
    lock_path = engine.parent / ".tomacco-vphone-session.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with ExitStack() as session:
        lock = session.enter_context(os.fdopen(lock_fd, "w"))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("This virtual iPhone engine already has an active session")
        scratch, scratch_problem = engine_scratch(engine)
        if scratch_problem:
            parser.error(engine_scratch_message(scratch, scratch_problem))
        protected = [engine, *inputs.values(), *disks.values()]
        if boot_manifest_path:
            protected.append(boot_manifest_path)
        if root_hash_path:
            protected.append(root_hash_path)
        media = {}
        for name, path in writable.items():
            media[name] = session.enter_context(open_writable_storage(path, protected))
            protected.append(path)
        storage = session.enter_context(open_sep_storage(storage_path, protected)) if storage_path else None
        # An overlay's backing files are read only; none may be a disk or
        # input of this session, which QEMU would then write through.
        overlays = {}
        for name, stream in media.items():
            header = ios_disk_overlay.qcow2_header(stream.fileno())
            if header is None:
                continue
            try:
                chain = ios_disk_overlay.backing_chain(writable[name])[1:]
            except ValueError as error:
                parser.error(str(error))
            own = {(item.stat().st_dev, item.stat().st_ino)
                   for item in (*protected, *([storage_path] if storage_path else []))}
            if any((layer.stat().st_dev, layer.stat().st_ino) in own for layer in chain):
                parser.error("An overlay's backing file must not be a disk or input of this session")
            overlays[name] = [str(layer) for layer in chain]
        nvram = None
        if "tomacco-aux" in media or "tomacco-aux" in disks:
            auxiliary = media.get("tomacco-aux")
            if auxiliary is None:
                auxiliary = session.enter_context(disks["tomacco-aux"].open("rb"))
                fcntl.flock(auxiliary, fcntl.LOCK_SH | fcntl.LOCK_NB)
            nvram = nvram_handoff(tree, auxiliary)
        pcc_config = pcc_config_handoff(tree, args.pcc_hostname) if args.pcc_hostname is not None else None
        writable_sizes = [writable_disk_bytes(stream) for stream in media.values()]
        if storage is not None:
            writable_sizes.append(os.fstat(storage.fileno()).st_size)
        file_limit = engine_file_limit(writable_sizes, args.display_profile, log_budget(args.seconds),
                                       mailbox=args.interactive)
        output = args.output.absolute()
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
        def pressure_preflight(stage, base=None):
            # Judged by the disks the guest reads and writes (hashed first, then live).
            guest_storage = [*disks.values(), *writable.values(), *([storage_path] if storage_path else [])]
            admission = wait_for_host_pressure(300 if stage == "before-input-hashing" else 120,
                                               storage=guest_storage)
            if not admission["admitted"]:
                refused = dict(base or {})
                refused.update(format=1, machine="tomacco-vphone", outcome="host-pressure-blocked",
                               exit_code=None, emulator_started=False, storage_hashes_complete=False,
                               qualified_for_app_development=False,
                               host_pressure_stage=stage, host_pressure_admission=admission,
                               observation_error=pressure_error(admission))
                save_result(output, refused)
                raise SystemExit(pressure_error(admission))
            return admission
        host_pressure_initial = pressure_preflight("before-input-hashing")
        hash_observations = []
        def observed_digest(path):
            observation = {}
            result = file_digest(path, observation=observation)
            hash_observations.append(observation)
            return result
        original_tree = None
        if (nvram is not None or identity["added"] or display["unallocated_vram_node_removed"]
                or boot_manifest is not None or root_hash is not None or pcc_config is not None
                or device_model is not None or display_scale is not None):
            original_tree = {"path": str(inputs["dtree"]), "sha256": observed_digest(inputs["dtree"])}
            inputs["dtree"] = output / "device-tree"
            encoded = tree.encode()
            if len(encoded) > MAX_TREE_BYTES:
                raise ValueError("Boot handoff exceeds the device-tree size limit")
            with inputs["dtree"].open("xb") as stream:
                stream.write(encoded)
        if boot_manifest is not None:
            manifest_copy = output / "boot-manifest.im4m"
            with os.fdopen(os.open(manifest_copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
                stream.write(boot_manifest_data)
            boot_manifest.update(source=str(boot_manifest_path), path=str(manifest_copy))
        if root_hash is not None:
            root_hash_copy = output / "system-volume-root-hash.im4p"
            with os.fdopen(os.open(root_hash_copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
                stream.write(root_hash_data)
            root_hash.update(source=str(root_hash_path), path=str(root_hash_copy))
        from ios_guest_control import socket_root
        # Room for "/tomacco-vphone-XXXXXXXX/guest.sock_1339" and the like.
        with tempfile.TemporaryDirectory(prefix="tomacco-vphone-", dir=socket_root(48)) as temporary:
            from ios_guest_control import local_control_preflight
            try:
                control_preflight = local_control_preflight(temporary)
            except OSError as error:
                record = {"format": 1, "machine": "tomacco-vphone",
                          "outcome": "local-control-unavailable", "exit_code": None,
                          "emulator_started": False, "storage_hashes_complete": False,
                          "qualified_for_app_development": False,
                          "observation_error": str(error),
                          "local_control_error": {"code": "local_unix_socket_unavailable",
                                                  "errno": error.errno,
                                                  "message": str(error)},
                          "guest_evidence": guest_evidence(output)}
                save_result(output, record)
                raise SystemExit("Local Unix control sockets are unavailable: " + str(error)) from None
            guest_control = None
            if args.guest_control_backend is not None:
                from ios_guest_control import GuestControl
                guest_control = GuestControl(args.guest_control_backend, temporary, output,
                                             lambda parent=os.getpid(): child_limits(parent))
                session.callback(guest_control.close)
            qmp_path = Path(temporary) / "qmp.sock"
            control_path = Path(temporary) / "control.sock"
            usb_path = Path(temporary) / "usb.sock"
            machine = "tomacco-vphone," + ",".join(
                key + "=" + str(path).replace(",", ",,") for key, path in inputs.items())
            if args.sep is not None:
                machine += ",sep-role=" + (args.sep_boot_role or "normal")
                machine += ",sep-handoff=" + (args.sep_handoff or "preloaded")
            if args.sep_measure_manifest:
                machine += ",sep-measure-manifest=on"
            if args.boot_args is not None:
                machine += ",args=" + args.boot_args.replace(",", ",,")
            if args.target_page != "engine":
                machine += f",tomacco-page-bits={TARGET_PAGES[args.target_page]}"
            if guest_control is not None:
                machine += ",memory-backend=tomacco-guest-memory"
            # A real-time clock allows MTTCG to execute the 32 ARM guest
            # cores concurrently. icount is intentionally isolated to a
            # single TCG thread because it cannot run with MTTCG.
            accelerator = ("tcg,thread=single" if args.clock_mode == "instruction"
                           else "tcg,thread=multi")
            if args.translation_cache_mib is not None:
                accelerator += ",tb-size=" + str(args.translation_cache_mib)
            command = [str(engine), "-M", machine, "-accel", accelerator,
                       "-m", str(memory >> 30) + "G", "-smp",
                       f"{ap_cpus},maxcpus={ap_cpus + 1}" if args.sep else str(ap_cpus),
                       "-display", "none",
                       "-serial", "file:" + str(output / "uart.log"), "-monitor", "none",
                       "-no-reboot", "-action", "panic=pause",
                       "-qmp", "unix:" + str(qmp_path) + ",server=on,wait=off",
                       "-d", "int,guest_errors,unimp" if args.trace_exceptions else "guest_errors,unimp",
                       "-D", str(output / "debug.log"), "-trace", "enable=tomacco_arm_*"]
            if args.pointer_auth == "helpers":
                # The machine creates its CPUs from the default type, so a
                # global reaches every AP (and the SEP processor, if any).
                command += ["-global", POINTER_AUTH_HELPERS]
            if args.bti == "off":
                command += ["-global", BTI_OFF]
            if args.jump_probe != "engine":
                command += ["-global", f"{JUMP_PROBE_PROPERTY}={JUMP_PROBES[args.jump_probe]}"]
            if args.wfe_wait_us is not None:
                command += ["-global", f"{WFE_WAIT_PROPERTY}={args.wfe_wait_us}"]
            if args.target_page == "16k":
                command += ["-global", GRANULE_16K]
            if tcg_profile is not None:
                # Counts every translated block's entries per vCPU with an
                # inline add; nothing runs per executed instruction.
                from ios_tcg_profile import plugin_argument
                command += ["-plugin", plugin_argument(
                    tcg_profile_plugin_receipt["plugin"], tcg_profile, interval=args.tcg_profile_interval,
                    top=args.tcg_profile_top, code=args.tcg_profile_code)]
            if args.translation_cache_mib is not None and args.translation_cache_mib > 2048:
                # Validate the actual engine and mapping before guest execution.
                command += ["-S"]
            if args.clock_mode == "instruction":
                # TCG can execute far fewer instructions per host second than
                # the original CPU. Keep guest deadlines tied to executed work
                # when requested. Idle waits remain real-time: sleep=off would
                # jump timers past asynchronous host GPU/I/O completion. This
                # is an instruction clock, not cycle-accurate hardware timing.
                command += ["-icount", "shift=0,sleep=on"]
            command += guest_network_arguments(args.guest_network, args.guest_network_backend)
            if args.interactive:
                # The PCI root bus cannot hotplug these devices. A separate
                # monitor lets agent commands coexist with the lifecycle owner.
                if args.input_transport == "usb-hid":
                    # send-key reaches the first registered guest keyboard.
                    # Keep VirtIO out of this mode so iOS receives standard
                    # USB keyboard reports. Touch/Home remain internal HID.
                    # Explicit root ports avoid QEMU's automatic hub when a
                    # device would consume a bus's final free port. The guest
                    # cannot enumerate that extra hub's child and otherwise
                    # retries it continuously instead of seeing the keyboard.
                    command += ["-device", "qemu-xhci,id=tomacco-usb-keyboard,p2=1,p3=0",
                                "-device", "usb-kbd,id=tomacco-keyboard,bus=tomacco-usb-keyboard.0,port=1",
                                "-device", "qemu-xhci,id=tomacco-usb-input,p2=2,p3=0,ports-non-removable=on",
                                "-device", "tomacco-ios-hid,id=tomacco-hid,bus=tomacco-usb-input.0,port=1"]
                else:
                    command += ["-device", "virtio-keyboard-pci,id=tomacco-keyboard,disable-legacy=on",
                                "-device", "virtio-tablet-pci,id=tomacco-pointer,disable-legacy=on"]
                command += ["-qmp", "unix:" + str(control_path) + ",server=on,wait=off"]
            # A normal multi-gigabyte restore performs millions of reads.
            # Per-request tracing can exhaust the fault-log budget during a
            # healthy mtree/seal pass. QMP supplies aggregate block statistics.
            if args.trace_storage and (disks or writable):
                command += ["-trace", "enable=virtio_blk_*"]
            if args.usb:
                command += ["-chardev", f"socket,id=tomacco-usb,path={usb_path},server=on,wait=off",
                            "-device", "tomacco-vphone-usb-pci,chardev=tomacco-usb"]
            for name, path in {**disks, **writable}.items():
                # One block node serves both the preboot reader and the OS
                # device. The inherited descriptor pins exactly the locked
                # inode, even if its pathname is replaced during a session.
                filename = f"/proc/self/fd/{media[name].fileno()}" if name in media else str(path)
                command += ["-blockdev", disk_node(name, filename, name in disks, name in overlays),
                            "-device", "vmapple-virtio-blk-pci,variant="
                            + ("aux" if name == "tomacco-aux" else "root") + ",drive=" + name]
            if storage_path is not None:
                command += ["-drive", "if=none,id=tomacco-sep-storage,format=raw,file.locking=on,file="
                            + str(storage_path).replace(",", ",,")]
            record = {"format": 1, "machine": "tomacco-vphone", "command": command,
                      "engine_sha256": observed_digest(engine),
                      "inputs": {key: {"path": str(path), "sha256": observed_digest(path)}
                                 for key, path in inputs.items()},
                      "read_only_disks": {key: {"path": str(path), "sha256": observed_digest(path)}
                                          for key, path in disks.items()},
                      "qualified_for_app_development": False, "macos_vm_used": False,
                      "local_control_preflight": control_preflight,
                      "host_pressure_initial": host_pressure_initial,
                      "storage_hash_observations": hash_observations,
                      "clock_mode": args.clock_mode,
                      "ap_cpus": ap_cpus, "tcg_thread_mode": "single" if args.clock_mode == "instruction" else "multi",
                      "translation_cache_mib": args.translation_cache_mib,
                      "host_cpu_policy": args.host_cpu_policy,
                      "pointer_auth": args.pointer_auth, "console_paint": args.console_paint,
                      "bti": args.bti, "jump_probe": args.jump_probe, "wfe_wait_us": args.wfe_wait_us,
                      "target_page": args.target_page, "guest_network": args.guest_network,
                      "engine_exit_seconds": engine_exit_seconds(engine, tcg_profile),
                      "display_profile": args.display_profile,
                      "device_model": device_model,
                      "display_scale": display_scale,
                      "trace_storage": args.trace_storage,
                      "tcg_profile": (None if tcg_profile is None else
                                      tcg_profile_receipt(tcg_profile, tcg_profile_plugin_receipt, args)),
                      "ctrr_advertised": any(n.name == "avp-ctrr" for n in tree.child("arm-io").children)}
            if storage is not None:
                record["persistent_sep_storage"] = {
                    "path": str(storage_path), "bytes": os.fstat(storage.fileno()).st_size,
                    "sha256_before": observed_digest(storage_path)}
            if writable:
                record["persistent_disks"] = {
                    name: {"path": str(path), "bytes": os.fstat(media[name].fileno()).st_size,
                           "sha256_before": observed_digest(Path(f"/proc/self/fd/{media[name].fileno()}")),
                           "format": "qcow2" if name in overlays else "raw",
                           **({"backing_chain": overlays[name]} if name in overlays else {})}
                    for name, path in writable.items()}
            # Where the launch spends its time (ios_start_timing): the hashing
            # of every input and disk before the engine starts, and below when
            # the engine started and exited and how long the disks' hashing
            # after it took.
            inputs_hashed = len(hash_observations)
            record["timing"] = timing_of(hash_observations, "input")
            record["engine_temporary"] = str(scratch)
            if nvram is not None:
                record["nvram_handoff"] = {**nvram, "prepared_tree": original_tree}
            if pcc_config is not None:
                record["pcc_configuration_handoff"] = pcc_config
            record["identity_handoff"] = identity
            record["display_handoff"] = display
            if args.boot_args is not None:
                record["boot_args"] = args.boot_args
            if boot_manifest is not None:
                record["boot_manifest_handoff"] = boot_manifest
            if root_hash is not None:
                record["system_volume_handoff"] = root_hash
            if original_tree is not None:
                record["prepared_tree"] = original_tree
            if args.sep is not None:
                record["sep_boot_role"] = args.sep_boot_role or "normal"
                record["sep_handoff"] = args.sep_handoff or "preloaded"
                record["sep_manifest_measurement"] = {
                    "requested": args.sep_measure_manifest,
                    "algorithm": "sha384" if args.sep_measure_manifest else None,
                    "scope": "complete-der-im4m" if args.sep_measure_manifest else None,
                    "signature_verified_by_host": False, "bootloader_policy_evaluated": False}
            if args.usb:
                record["usb"] = {"protocol": "apple-vusb-v1", "socket": str(usb_path),
                                 "local_only": True, "restore_completed": False}
            if args.interactive:
                record["control"] = {
                    "protocol": "qmp", "socket": str(control_path), "local_only": True,
                    "keyboard": "tomacco-keyboard",
                    "keyboard_transport": "usb-hid" if args.input_transport == "usb-hid" else "virtio",
                    "input_transport": args.input_transport,
                    "pointer": "tomacco-hid" if args.input_transport == "usb-hid" else "tomacco-pointer",
                    "pointer_kind": "single-contact-touch" if args.input_transport == "usb-hid" else "absolute-mouse",
                    "guest_interaction_qualified": False}
            # A viewer's live frames come from the renderer's own copy of each
            # changed present, not from screendump on QEMU's main loop. An
            # engine without the mailbox ignores the variable and creates no
            # file; the control plane then falls back to screendump.
            mailbox = FrameMailbox.open() if args.interactive else None
            if mailbox is not None:
                session.callback(mailbox.close)
                record["frame_mailbox"] = mailbox.receipt()
            if guest_control is not None:
                try:
                    guest_control.start()
                except (OSError, ValueError, RuntimeError) as error:
                    record.update(outcome="guest-control-start-error", observation_error=str(error),
                                  exit_code=None, storage_hashes_complete=False,
                                  guest_evidence=guest_evidence(output))
                    save_result(output, record)
                    raise SystemExit(str(error)) from None
                command.extend(guest_control.qemu_arguments(memory))
                record["guest_control"] = guest_control.receipt()
                record["command"] = command
            graphics_log_directory = output / "renderer"
            graphics_log_directory.mkdir(mode=0o700)
            record["render_store_writeback"] = "eager"
            record["graphics_logs"] = {
                "directory": str(graphics_log_directory),
                "fail": str(graphics_log_directory / "fail.log"),
                "draw": str(graphics_log_directory / "draw.log"),
                "requires_engine_log_directory_support": True,
            }
            # Child agent jobs may already be waiting for this request.
            # Publish it atomically so no reader sees a partial JSON document.
            request_temporary = output / ".request.json.tmp"
            with request_temporary.open("x") as stream:
                os.chmod(request_temporary, 0o600)
                stream.write(json.dumps(record, indent=2) + "\n")
            os.replace(request_temporary, output / "request.json")
            # iOS reads GPU-rendered surfaces with its CPU after completion and
            # names no resource first: the Calendar/Clock icons are placeholders
            # with deferred render Stores and render without them. So every Store lands in guest memory at the Store,
            # and a shell override must not bring the deferral back.
            env = {**os.environ, "REIMS_VGPU_RAIL": "vulkan",
                   "REIMS_VGPU_WINDOW": "1" if args.window else "0",
                   "REIMS_VGPU_LOG_DIRECTORY": str(graphics_log_directory),
                   "REIMS_VGPU_LAZY_WRITEBACK": "off",
                   "TMPDIR": str(scratch)}
            # Profile selection is part of the owned launch request, so an
            # inherited shell override must not silently change the device.
            env.pop("REIMS_VGPU_PANEL", None)
            env.pop(FRAME_MAILBOX_VARIABLE, None)
            env.pop(EAGER_CONSOLE_PAINT_VARIABLE, None)
            if args.console_paint == "eager":
                env[EAGER_CONSOLE_PAINT_VARIABLE] = "1"
            if mailbox is not None:
                env[FRAME_MAILBOX_VARIABLE] = str(mailbox.path)
            if args.display_profile is not None:
                panel = args.display_profile
                env["REIMS_VGPU_PANEL"] = (f"{panel['width']}x{panel['height']}@{panel['refresh_hz']}:"
                                           f"{panel['width_mm']}x{panel['height_mm']}")
            qmp = None
            record["host_pressure_before_launch"] = pressure_preflight("before-emulator-launch", record)
            started = time.monotonic()
            record["timing"]["emulator_started"] = time.time()
            parent_pid = os.getpid()
            def interrupt(signum, frame):
                raise KeyboardInterrupt
            previous_handlers = {sig: signal.signal(sig, interrupt)
                                 for sig in (signal.SIGTERM, signal.SIGHUP)}
            with (output / "host.log").open("xb") as log:
                child = subprocess.Popen(command, stdout=log, stderr=log, env=env,
                                         start_new_session=True,
                                         pass_fds=(lock_fd, *(stream.fileno() for stream in media.values()),
                                                   *((storage.fileno(),) if storage else ())),
                                         preexec_fn=lambda: child_limits(parent_pid, file_limit))
                try:
                    while child.poll() is None and not qmp_path.exists():
                        check_log_budget(output, log_budget(args.seconds))
                        if time.monotonic() - started > min(args.seconds, 30):
                            raise TimeoutError("QEMU did not expose QMP before the startup deadline")
                        time.sleep(0.05)
                    if child.poll() is None:
                        qmp = QMP(qmp_path)
                        cache_admission = record["translation_cache_admission"] = {}
                        admit_translation_cache(qmp, args.translation_cache_mib, cache_admission)
                        if cache_admission["large_cache_requested"]:
                            continued = qmp.call("cont")
                            cache_admission["continue_reply"] = continued
                            if "return" not in continued:
                                raise ValueError("translation_cache_continue_failed: qualified cache guest was not resumed")
                            cache_admission["guest_execution_released"] = True
                        record["host_cpu_placement"] = place_host_cpu_threads(
                            qmp, child.pid, ap_cpus, args.host_cpu_policy, args.clock_mode)
                        request_temporary = output / ".request.json.tmp"
                        with request_temporary.open("x") as stream:
                            os.chmod(request_temporary, 0o600)
                            stream.write(json.dumps(record, indent=2) + "\n")
                        os.replace(request_temporary, output / "request.json")
                        # A session someone works in must not be frozen at its
                        # limit with everything since the last save lost.
                        own_job = os.environ.get("TOMACCO_PARENT_JOB")
                        saving = saving_margin(args.seconds, args.cli, args.interactive, guest_control, own_job)
                        save_at = None if saving is None else args.seconds - saving
                        trimmed = gpu_checked = time.monotonic()
                        gpu_short = 0
                        while child.poll() is None and time.monotonic() - started < args.seconds:
                            if guest_control is not None and guest_control.process.poll() is not None:
                                raise OSError("Guest control backend exited during the session")
                            check_log_budget(output, log_budget(args.seconds))
                            if time.monotonic() - trimmed > 60:
                                trimmed = time.monotonic()
                                trim_renderer_log(graphics_log_directory / "fail.log")
                            if time.monotonic() - gpu_checked > GPU_MEMORY_CHECK_SECONDS:
                                gpu_checked = time.monotonic()
                                memory = process_gpu_memory(child.pid)
                                gpu_short = gpu_short + 1 if gpu_memory_short(memory) else 0
                                # Two checks in a row, so that a short peak does not end a session.
                                if gpu_short == 2 and "vram_guard_stop" not in record:
                                    record["vram_guard_stop"] = {"used": memory[0], "total": memory[1],
                                                                 "elapsed_seconds": round(time.monotonic() - started)}
                                    if save_at is not None:
                                        save_at = 0
                                save_gpu_memory(output, gpu_memory_record(
                                    memory, gpu_short, "vram_guard_stop" in record, time.time()))
                            if save_at is not None and time.monotonic() - started >= save_at:
                                attempt = saved_stop(args.cli, own_job)
                                record.setdefault("session_limit_stop", []).append(attempt)
                                # A guest that is busy right now is asked again
                                # while there is time; one that was told to exit
                                # is not asked twice.
                                save_at = None if attempt["accepted"] else time.monotonic() - started + 20
                            time.sleep(0.1)
                        if child.poll() is None:
                            qmp.call("stop")
                            record["status"] = qmp.call("query-status")
                            for name, cmd in (("registers", "info registers"), ("memory", "info mtree"),
                                              ("stack", "x /64gx $sp")):
                                record[name] = qmp.call("human-monitor-command", {"command-line": cmd})
                            record["cpus"] = qmp.call("query-cpus-fast")
                            record["block_statistics"] = qmp.call("query-blockstats")
                            record["pci_devices"] = qmp.call("query-pci")
                            if args.sep is not None:
                                record["sep_registers"] = qmp.call(
                                    "human-monitor-command", {"command-line": "info registers", "cpu-index": ap_cpus})
                            qmp.call("quit")
                            child.wait(timeout=engine_exit_seconds(engine, tcg_profile))
                except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                    record["observation_error"] = str(error)
                except KeyboardInterrupt:
                    record["interrupted"] = True
                finally:
                    stop_child(child, grace=engine_exit_seconds(engine, tcg_profile))
                    if guest_control is not None:
                        guest_control.close()
                    if qmp is not None:
                        qmp.close()
                    record["exit_code"] = child.returncode
                    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
                    record["timing"]["engine_exited"] = time.time()
                    for sig, handler in previous_handlers.items():
                        signal.signal(sig, handler)
            record["guest_evidence"] = guest_evidence(output)
            if tcg_profile is not None and not record.get("interrupted"):
                tcg_profile_render(record, inputs)
            # Cancellation has a short process-group grace period. Hashing a
            # 32 GiB disk here can outlive it and lose the entire diagnostic
            # receipt. Keep the real guest evidence and explicitly leave final
            # media hashes unverified; normal completion still hashes all media.
            record["storage_hashes_complete"] = False
            if storage_path is not None:
                record["persistent_sep_storage"]["sha256_after"] = None
            for name in media:
                record["persistent_disks"][name]["sha256_after"] = None
            evidence = record["guest_evidence"]
            record["outcome"] = run_outcome(record)
            # Publish before hashing even on normal completion. Cancellation
            # during post-processing must also retain the guest's outcome.
            save_result(output, record)
            if not record.get("interrupted"):
                try:
                    if storage_path is not None:
                        record["persistent_sep_storage"]["sha256_after"] = observed_digest(storage_path)
                    for name, stream in media.items():
                        record["persistent_disks"][name]["sha256_after"] = observed_digest(
                            Path(f"/proc/self/fd/{stream.fileno()}"))
                    record["storage_hashes_complete"] = True
                    record["timing"].update(timing_of(hash_observations[inputs_hashed:], "output"))
                except (OSError, ValueError) as error:
                    record["storage_hash_error"] = str(error)
                    record["outcome"] = "observation-error"
                save_result(output, record)
            registers = record.get("registers", {}).get("return", "")
            match = re.search(r"PC=([0-9a-fA-F]+)", registers)
            print(json.dumps({"output": str(output), "exit_code": child.returncode,
                              "pc": "0x" + match[1] if match else None,
                              "observation_error": record.get("observation_error"),
                              "outcome": record["outcome"], "guest_evidence": evidence,
                              "qualified_for_app_development": False}, indent=2))
            if record["outcome"] not in ("bounded-session", "emulator-exited"):
                raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
