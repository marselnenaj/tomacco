"""Bounded Linux PSI admission for new iOS storage/VM work.

PSI semantics: https://docs.kernel.org/accounting/psi.html
Limits below are Tomacco policy, not kernel or guest ABI requirements.
This checks a point in time; it never kills or pauses an existing workload.

I/O PSI is system wide: a saturated disk that holds none of the guest's
storage stalls the whole host's figure. When the caller names the storage it
will use, I/O is judged by how busy the block devices holding it are instead;
PSI remains the measure whenever any of those devices cannot be identified.
"""
import math
import os
from pathlib import Path
import time


FULL_PRESSURE_LIMITS = {"io": 20.0, "memory": 10.0}
PRESSURE_WINDOWS = ("avg10", "avg60")
# Share of wall time a storage device spent with requests in flight
# (/sys/block/*/stat io_ticks), sampled over a few seconds.
DEVICE_BUSY_LIMIT_PERCENT = 50.0
DEVICE_SAMPLE_SECONDS = 5.0


def pressure_directory():
    """The PSI directory: /proc/pressure, or a recorded one under the isolated
    test root. Tests that run synthetic engines must not depend on whatever
    the host is doing; outside TOMACCO_TEST_ROOT the override is ignored, so an
    inherited shell value cannot hide real pressure from product work."""
    override, root = os.environ.get("TOMACCO_IOS_PRESSURE_DIRECTORY"), os.environ.get("TOMACCO_TEST_ROOT")
    if override and root:
        candidate, base = Path(override).resolve(), Path(root).resolve()
        if candidate.is_relative_to(base):
            return candidate
    return Path("/proc/pressure")


def sysfs_root():
    """/sys, or a recorded tree under the isolated test root (see pressure_directory)."""
    override, root = os.environ.get("TOMACCO_IOS_SYSFS_DIRECTORY"), os.environ.get("TOMACCO_TEST_ROOT")
    if override and root and Path(override).resolve().is_relative_to(Path(root).resolve()):
        return Path(override).resolve()
    return Path("/sys")


def process_gpu_memory(pid, proc="/proc"):
    """(used, total) VRAM bytes of the largest card process `pid` has open, or
    None when none of its cards reports them. Vulkan opens every card of a
    machine, and an integrated one has no VRAM to report."""
    try:
        records = list(Path(proc, str(pid), "fdinfo").iterdir())
    except OSError:
        return None
    devices = set()
    for record in records:
        try:
            devices.update(line.split()[1] for line in record.read_text().splitlines()
                           if line.startswith("drm-pdev:") and len(line.split()) == 2)
        except OSError:
            continue
    cards = []
    for device in devices:
        card = sysfs_root() / "bus/pci/devices" / device
        try:
            cards.append((int((card / "mem_info_vram_total").read_text()), int((card / "mem_info_vram_used").read_text())))
        except (OSError, ValueError):
            continue
    return tuple(reversed(max(cards))) if cards else None


def gpu_memory_short(memory):
    """Whether a card's free VRAM is below an eighth of it. The card is shared
    with the desktop, and a compositor refused a command submission does not
    survive it, so a guest has to stop before that."""
    return memory is not None and memory[1] > 0 and memory[1] - memory[0] < memory[1] / 8


def mount_source(path):
    """The block device node a path's file system is mounted from, or None."""
    target = Path(path).resolve()
    best, source = None, None
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        left, _, right = line.partition(" - ")
        fields, tail = left.split(), right.split()
        if len(fields) < 5 or len(tail) < 2:
            continue
        point = Path(fields[4].encode().decode("unicode_escape"))
        if target.is_relative_to(point) and (best is None or len(point.parts) > len(best.parts)):
            best, source = point, tail[1]
    return source if source and source.startswith("/dev/") else None


def storage_device(path):
    """The sysfs directory of the block device that holds a path, or None."""
    source = mount_source(path)
    try:
        number = os.stat(source).st_rdev if source else None
    except OSError:
        return None
    if not number:
        return None
    directory = sysfs_root() / "dev/block" / f"{os.major(number)}:{os.minor(number)}"
    return directory if (directory / "stat").is_file() else None


def device_busy(devices, seconds=DEVICE_SAMPLE_SECONDS):
    """Percent of a sample each device had reads, writes or flushes in flight,
    by device name.

    io_ticks also counts time in which only discards were in flight. A file
    system freeing space in the background (btrfs `discard=async` after a large
    delete) keeps a device busy for many minutes with discards alone, which
    nothing a guest does waits for (69 % busy, no reads or writes). The busy time is therefore scaled by the share of request time
    that was not spent on discards (stat fields per
    Documentation/block/stat.rst)."""
    def fields():
        return {device: [int(value) for value in (device / "stat").read_text().split()] for device in devices}
    start, began = fields(), time.monotonic()
    time.sleep(seconds)
    end, elapsed = fields(), max(time.monotonic() - began, 1e-3)
    busy = {}
    for device in devices:
        delta = [after - before for before, after in zip(start[device], end[device])]
        ticks = delta[9]
        discard = delta[14] if len(delta) > 14 else 0
        if discard:
            work = delta[3] + delta[7] + (delta[16] if len(delta) > 16 else 0)
            ticks = ticks * work / (work + discard)
        busy[device.resolve().name] = min(100.0, ticks / (elapsed * 10))
    return busy


def device_admission(paths):
    """The I/O row judged by the devices holding paths, or None to use PSI."""
    devices = [storage_device(path) for path in paths]
    if not devices or None in devices:
        return None
    try:
        busy = device_busy(sorted(set(devices)))
    except (OSError, ValueError, IndexError):
        return None
    worst = max(busy.values())
    return {"observed": True, "measure": "storage-device-busy", "devices_busy_percent": busy,
            "busy_limit_percent": DEVICE_BUSY_LIMIT_PERCENT, "busy_percent": worst,
            "blocked_windows": ["sample"] if worst >= DEVICE_BUSY_LIMIT_PERCENT else []}


def host_pressure_admission(storage=()):
    """Admission for new work; storage names the files or directories it will
    read and write (I/O is then judged by their devices, see above)."""
    result = {"format": 1, "policy": "ios-host-pressure-v3", "admitted": True,
              "coverage_complete": True, "resources": {}, "blocked_resources": []}
    devices = device_admission(storage) if storage else None
    for resource, limit in FULL_PRESSURE_LIMITS.items():
        if resource == "io" and devices is not None:
            result["resources"][resource] = devices
            if devices["blocked_windows"]:
                result["blocked_resources"].append(resource)
            continue
        row = {"observed": False, "blocked_windows": []}
        for window in PRESSURE_WINDOWS:
            row[f"full_{window}_limit_percent"] = limit
        result["resources"][resource] = row
        try:
            with (pressure_directory() / resource).open("r", encoding="ascii") as stream:
                data = stream.read(4097)
            if len(data) > 4096:
                raise ValueError("PSI report exceeds the observation bound")
            full = [line.split() for line in data.splitlines() if line.startswith("full ")]
            if len(full) != 1:
                raise ValueError("PSI report has no unique full-pressure row")
            fields = dict(field.split("=", 1) for field in full[0][1:])
            # A brief quiet interval after a long I/O stall must not admit a
            # new disk copy or VM while the one-minute window is still high.
            # Parse independently: a missing window cannot erase an observed
            # refusal in the other one, or establish complete coverage.
            for window in PRESSURE_WINDOWS:
                try:
                    value = float(fields[window])
                    if not math.isfinite(value) or not 0 <= value <= 100:
                        raise ValueError("PSI full pressure is outside its percentage range")
                    row[f"full_{window}_percent"] = value
                    if value >= limit:
                        row["blocked_windows"].append(window)
                except (ValueError, KeyError) as error:
                    row.setdefault("window_errors", {})[window] = str(error)
                    result["coverage_complete"] = False
            row["observed"] = not row.get("window_errors")
        except (OSError, UnicodeError, ValueError, KeyError) as error:
            row["observation_error"] = str(error)
            result["coverage_complete"] = False
        if row["blocked_windows"]:
            result["blocked_resources"].append(resource)
    # Kernels without PSI retain their previous behavior, with explicit unknown
    # coverage. Missing data never establishes absence of host pressure.
    result["admitted"] = not result["blocked_resources"]
    return result


def wait_for_host_pressure(timeout=300.0, interval=10.0, storage=()):
    """Wait, bounded, for an admitted sample instead of refusing at the first
    busy moment. A desktop shares this host with builds and other guests whose
    I/O comes in bursts; a start clicked during one should wait it out, not
    fail. Returns the first admitting sample, or the last refusal once the
    bound has passed, which the caller still refuses with pressure_error.
    Every sample is a fresh point-in-time admission; nothing is cached.
    TOMACCO_IOS_PRESSURE_WAIT_SECONDS can only shorten the wait (0: one sample)."""
    try:
        limit = float(os.environ.get("TOMACCO_IOS_PRESSURE_WAIT_SECONDS", "inf"))
    except ValueError:
        limit = math.inf
    if math.isfinite(limit) and limit >= 0:
        timeout = min(timeout, limit)
    deadline = time.monotonic() + timeout
    while True:
        admission = host_pressure_admission(storage)
        remaining = deadline - time.monotonic()
        if admission["admitted"] or remaining <= 0:
            return admission
        time.sleep(min(interval, remaining))


def pressure_error(admission):
    resources = ", ".join(admission["blocked_resources"])
    return "Host resource pressure is too high for new iOS work: " + resources
