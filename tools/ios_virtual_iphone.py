#!/usr/bin/env python3
"""Start or stop the configured virtual iPhone through the public agent API.

A start prepares a new private disk copy from the last verified run
(reinstalling guest tools and restoring the Preboot Metal driver the guest
removes), boots it, attempts to leave the passcode-free lock screen with Home presses
and submits the configured app launch. Rebuilt guest tools report the native
lock status before submission. Launch acceptance never verifies a visible scene; the start
only reports whether the panel then shows anything but black. Nothing here bypasses an
iOS decision; a refused launch is reported as refused.

The profile directory holds profile.json (static inputs) and state.json (the
current run). Only a run whose guest confirmed flushing its storage and that
then closed with exit 0 and complete storage hashes becomes the base of the
next start; after any other stop the next start goes back to the last verified
run. A guest whose service does not answer (still booting, or hung) can be
stopped as well; it is ended without saving and never becomes a base. A guest
that is stopped before iOS ever came up is simply forgotten, with its disk copy.
"""
import argparse
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zlib

from build_ios_guest_tools import policy_entries, stage_application
from ios_application_bundle import inspect_bundle
from ios_hot_install import (BUNDLE_PORT, connect_port, guest_socket, reserve, send_bundle, spare_entries,
                             with_hashes, write_private)
from ios_graphics_location import BUNDLE_PATH, COMPILER_LIBRARY_NAME, PLUGIN_PERSONALITY

TERMINAL = ("succeeded", "failed", "cancelled", "timed_out")
# The owned diagnostic app every development-signing build carries, under the
# identifier guests have it installed with (application_info keeps it).
RESEARCH_APPLICATION = "io.github.marselnenaj.tomacco.research-app"
# Refusals of a stop by a guest that does answer: a native request is in
# progress or its worker has not confirmed it is idle. Such a guest is never
# ended by force; an unknown mutation would be lost.
WORKING = ("ios_guest_busy", "ios_guest_not_idle")
# The control lane is taken for a moment (a viewer's gesture or frame, another
# stop). That says nothing about the guest: the stop is asked again like after
# any transient error, and refused, never forced, if the lane stays taken.
CONTENDED = ("ios_control_busy",)
# The emulator has exited already and the job is finishing by itself, or the
# job has ended: there is nothing left to stop.
ENDED = ("ios_guest_stopped", "ios_control_ended")
ACTIVE = ("queued", "running")


class GuestObservationError(RuntimeError):
    """A failed observation must not change the run's lifecycle state."""


class ToolsMismatch(RuntimeError):
    """The guest-tools build does not carry a profile app as it is now: a
    rebuild with the profile's apps (Phone.rebuild_tools) answers it."""


def frame_lit(path):
    """Whether a captured 8-bit RGB or grey PNG has any non-black pixel; None
    for a frame this cannot read. Filtering keeps an all-zero row all zero,
    so the filtered bytes answer this without reconstructing the pixels."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    offset, header, compressed = 8, None, bytearray()
    while offset + 8 <= len(data):
        length, kind = int.from_bytes(data[offset:offset + 4], "big"), data[offset + 4:offset + 8]
        body = data[offset + 8:offset + 8 + length]
        if kind == b"IHDR":
            header = body
        elif kind == b"IDAT":
            compressed += body
        offset += 12 + length
    if header is None or len(header) < 13 or header[8] != 8 or header[9] not in (0, 2):
        return None
    width, height = int.from_bytes(header[0:4], "big"), int.from_bytes(header[4:8], "big")
    stride = width * (3 if header[9] == 2 else 1) + 1
    try:
        rows = zlib.decompress(bytes(compressed))
    except zlib.error:
        return None
    if len(rows) < stride * height:
        return None
    return any(rows[row * stride + 1:(row + 1) * stride].strip(b"\0") for row in range(height))


def kernel_graphics_personalities(kernel):
    """The Metal plugin locations a kernelcache's prelinked GPU personality
    names (under /private/preboot), or None when the kernel cannot be read."""
    try:
        data = Path(kernel).read_bytes()
    except OSError:
        return None
    marker = PLUGIN_PERSONALITY[:PLUGIN_PERSONALITY.index("preboot/") + len("preboot/")].encode()
    found, offset = set(), data.find(marker)
    while offset >= 0:
        end = offset + len(marker)
        while end < len(data) and (chr(data[end]).isalnum() or data[end] in b"/._-"):
            end += 1
        found.add(data[offset:end].decode("ascii"))
        offset = data.find(marker, end)
    return found


def development_library_cdhash(tools):
    """The CodeDirectory hash of the development-signing adapter a guest-tools
    build carries, or None when the build has no such adapter. The kernel pins
    this hash in its mmap(PROT_EXEC) allow-list; a file on /private/var is only
    mapped executable when its hash is pinned, so the adapter the start inserts
    must be the one the boot kernel was prepared for."""
    try:
        record = json.loads((Path(tools) / "guest-tools.json").read_text())
    except (OSError, ValueError):
        return None
    for row in record.get("programs") or []:
        if isinstance(row, dict) and row.get("path") == "usr/libexec/libtomacco-development-signing.dylib":
            cdhash = row.get("cdhash")
            return cdhash if isinstance(cdhash, str) and re.fullmatch(r"[0-9a-f]{40}", cdhash) else None
    return None


def kernel_pins_cdhash(kernel, cdhash):
    """Whether the kernelcache carries `cdhash` (40 hex) verbatim in its pinned
    mmap-exec allow-list. None when the kernel cannot be read."""
    try:
        data = Path(kernel).read_bytes()
    except OSError:
        return None
    return bytes.fromhex(cdhash) in data


def bridge_compiler_paths(graphics):
    """The compiler plugin paths the graphics payload's bridge `p` opens
    (under /private/preboot), or None when the bridge cannot be read."""
    try:
        data = (Path(graphics) / "p").read_bytes()
    except OSError:
        return None
    marker, suffix = b"/private/preboot/", ("/" + COMPILER_LIBRARY_NAME).encode()
    found, offset = set(), data.find(marker)
    while offset >= 0:
        end = data.find(b"\0", offset)
        if end > offset and data[offset:end].endswith(suffix):
            found.add(data[offset:end].decode("ascii", "replace"))
        offset = data.find(marker, offset + len(marker))
    return found


# Host software the engine, the renderer and the guest-tools build depend on.
# Updates to libnfs, the kernel and clang each broke the one-step phone
# without a word; a start now names what changed since the last good one.
HOST_PACKAGES = ("linux", "mesa", "vulkan-radeon", "vulkan-icd-loader", "libdrm", "glibc",
                 "clang", "llvm-libs", "rust", "qt6-base", "libnfs")


def host_software():
    """The running kernel and the installed versions of HOST_PACKAGES that
    the host's package manager reports; a package it does not know is left out."""
    record = {"kernel": os.uname().release}
    pacman = shutil.which("pacman")
    if pacman:
        try:
            done = subprocess.run([pacman, "-Q", *HOST_PACKAGES], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return record
        for line in done.stdout.splitlines():
            name, _, version = line.partition(" ")
            if name in HOST_PACKAGES and version:
                record[name] = version
    return record


def emit(phase, **details):
    print("TOMACCO_VIRTUAL_IPHONE " + json.dumps({"phase": phase, **details}), flush=True)


def valid_time_zone(name):
    """Whether the guest's time_zone operation takes this name: one to three
    '/'-separated components of 1 to 32 ASCII letters, digits, '_', '-' or
    '+' each, the name of a file under the zoneinfo tree ("UTC",
    "Europe/Vienna", "Etc/GMT+1"); the guest applies the same rule
    (guest-time-zone.h), as does tomaccoctl."""
    if not isinstance(name, str) or not name:
        return False
    parts = name.split("/")
    return len(parts) <= 3 and all(
        1 <= len(part) <= 32 and all(c.isascii() and (c.isalnum() or c in "_-+") for c in part) for part in parts)


def host_time_zone():
    """The host's zone as a zoneinfo name, or None.

    The target of /etc/localtime below a zoneinfo directory (the link systemd
    and most distributions keep) comes first, then TZ (a leading ':' is
    glibc's optional prefix). A copied /etc/localtime, a POSIX rule in TZ or
    a path outside the zoneinfo tree names no zone file the guest could take."""
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        target = ""
    for candidate in (target, (os.environ.get("TZ") or "").lstrip(":")):
        name = candidate.rpartition("zoneinfo/")[2] if "zoneinfo/" in candidate else candidate
        if valid_time_zone(name):
            return name
    return None


def staged_applications(profile, tools):
    """The developer apps a start installs: the bundles `applications` in
    profile.json names, each of which the guest-tools build `tools` must
    carry with the content the bundle has now. Every reason it cannot be
    deployed is named before any disk copy is made; without `applications`
    there is nothing to install."""
    listed = profile.get("applications") or []
    if not isinstance(listed, list) or any(not isinstance(row, dict) or not isinstance(row.get("bundle"), str)
                                           or not row["bundle"] for row in listed):
        raise RuntimeError("profile.json: applications must be a list of {bundle: path to an .app directory}")
    if not listed:
        return []
    tools = Path(tools)
    try:
        record = json.loads((tools / "guest-tools.json").read_text())
    except (OSError, ValueError) as error:
        raise RuntimeError(f"The guest-tools build {tools} has no readable guest-tools.json: {error}") from error
    carried = {app["bundle_id"]: app for app in (record.get("applications") or []) if isinstance(app, dict)
               and isinstance(app.get("bundle_id"), str) and isinstance(app.get("guest_path"), str)
               and isinstance(app.get("cdhash"), str)}
    staged, seen = [], {}
    for row in listed:
        entitlements = row.get("entitlements")
        try:
            inspected = inspect_bundle(row["bundle"], entitlements if isinstance(entitlements, str) and entitlements else None)
        except (OSError, ValueError) as error:
            raise RuntimeError(f"App bundle {row['bundle']} cannot be deployed: {error}") from error
        bundle_id, name = inspected["bundle_id"], inspected["name"]
        if bundle_id == RESEARCH_APPLICATION:
            raise RuntimeError(f"{name} uses the owned diagnostic app's bundle identifier {bundle_id}; choose another")
        if bundle_id in seen:
            raise RuntimeError(f"Two app bundles use the bundle identifier {bundle_id}: {seen[bundle_id]} and {row['bundle']}")
        seen[bundle_id] = row["bundle"]
        app = carried.get(bundle_id)
        if app is None:
            raise ToolsMismatch(f"The guest-tools build {tools} does not carry {name} ({bundle_id}); build guest tools "
                                "with this bundle (ios.emulator.build_guest_tools applications, or "
                                "build_ios_guest_tools.py --application) and name that build as the profile's tools")
        if (app.get("source") or {}).get("content_sha256") != inspected["content_sha256"]:
            raise ToolsMismatch(f"{name} changed since the guest-tools build {tools} was made; "
                                "rebuild guest tools with the current bundle")
        if app.get("entitlements") != inspected["entitlements"]:
            raise ToolsMismatch(f"{name} asks for other entitlements than the guest-tools build {tools} signed; "
                                "rebuild guest tools")
        staged.append({"bundle_id": bundle_id, "name": name, "guest_path": app["guest_path"], "cdhash": app["cdhash"]})
    return staged


def owned_application(tools):
    """The owned diagnostic app as a staged app, or nothing for a build that
    does not carry it. Each build signs it anew and its trust cache admits
    only that signature, so a copy from an earlier build stays on the data
    volume yet iOS refuses to run it; a build it never came from has no copy
    at all. It is installed like a developer app, by its cdhash."""
    try:
        record = json.loads((Path(tools) / "guest-tools.json").read_text())
    except (OSError, ValueError):
        return []
    root, path = record.get("guest_root"), "usr/libexec/TomaccoProbe.app/TomaccoProbe"
    cdhash = next((row.get("cdhash") for row in record.get("programs") or []
                   if isinstance(row, dict) and row.get("path") == path), None)
    if not isinstance(root, str) or not isinstance(cdhash, str):
        return []
    return [{"bundle_id": RESEARCH_APPLICATION, "name": "TomaccoProbe.app",
             "guest_path": f"{root}/{path.rsplit('/', 1)[0]}", "cdhash": cdhash}]


# The control service's grants the builder ties to the owned agent; a record
# whose grants differ was made by another builder and is not rebuilt as it.
AGENT_GRANTS = ("installer_connection_query", "application_log_access", "application_termination", "time_zone_control")
# Grants this builder added later, also with the agent. A record from before
# one has no such key and is rebuilt with the grant; one recording it
# otherwise than granted was made by another builder.
LATER_AGENT_GRANTS = ("auto_lock_control",)
REPOSITORY = Path(__file__).resolve().parents[1]
# Where tools/build_ios_linker.py and the SPTM support build put them.
TOOLCHAIN_DEFAULTS = {"linker": ".tooling/ios-linker/prefix/bin/aarch64-apple-darwin-ld",
                      "ldid": ".tooling/ios-sptm/support/ldid/ldid"}


def rebuild_options(record):
    """The options a guest-tools record was built with, for a rebuild that
    changes only the staged apps. ValueError names why a record cannot be
    rebuilt so: not a finished build, no development signing with a policy
    table (staged apps need it), an experiment build, grants this builder no
    longer makes."""
    if not isinstance(record, dict) or record.get("state") != "built" or record.get("scope") != "ios-owned-guest-tools":
        raise ValueError("it is not a recorded guest-tools build")
    signing = record.get("development_signing")
    if not isinstance(signing, dict):
        raise ValueError("it was built without development signing, which staged apps need")
    if "policy_table_sha256" not in signing:
        raise ValueError("its development-signing adapter has no policy table (a legacy or substituted adapter)")
    if record.get("experiment_probe_library"):
        raise ValueError("it is an experiment build")
    if record.get("agent") is not True:
        raise ValueError("it has no owned agent, which development signing needs")
    for key in AGENT_GRANTS + LATER_AGENT_GRANTS:
        if record.get(key) is not True and (key in AGENT_GRANTS or key in record):
            raise ValueError(f"it records {key} as {record.get(key)!r}; this builder grants it with the agent")
    target = re.fullmatch(r"arm64-apple-ios([0-9]{1,2}\.[0-9]{1,2}(?:\.[0-9]{1,2})?)", str(record.get("target")))
    trustcache = (record.get("trustcache") or {}).get("source_sha256")
    sources = record.get("sources")
    if target is None or not isinstance(trustcache, str) or re.fullmatch(r"[0-9a-f]{64}", trustcache) is None:
        raise ValueError("it records no target or trust-cache source")
    if not isinstance(sources, dict) or not sources or any(not isinstance(name, str) for name in sources):
        raise ValueError("it records no sources")
    return {"agent": True, "application_probe": True, "development_signing": True,
            "graphics_query_access": record.get("graphics_query_access") is True,
            "quiet_services": record.get("quiet_services") is True,
            "virtual_battery": record.get("virtual_battery") is True, "minimum_ios": target.group(1),
            "trustcache_sha256": trustcache, "sources": dict(sources)}


def changed_guest_sources(tools):
    """The guest sources whose workspace content differs from what the
    guest-tools build `tools` records it was compiled from; a recorded source
    the workspace no longer has counts as changed. Nothing when the build
    records no sources, or when the workspace has none of them to compare
    with (a copy of the tools alone)."""
    try:
        recorded = json.loads((Path(tools) / "guest-tools.json").read_text()).get("sources")
    except (OSError, ValueError, AttributeError):
        return []
    if not isinstance(recorded, dict):
        return []
    import build_ios_guest_tools as builder
    changed, compared = [], False
    for name, value in recorded.items():
        try:
            data = (builder.SOURCES / name).read_bytes()
        except OSError:
            changed.append(name)
            continue
        compared = True
        if hashlib.sha256(data).hexdigest() != value:
            changed.append(name)
    return sorted(changed) if compared else []


def settings_executables():
    """The control plane's configured executables (settings.json `executables`,
    which ios.emulator.build_guest_tools resolves its tools through)."""
    home = Path(os.environ.get("TOMACCO_HOME") or Path.home() / ".local/share/tomacco")
    try:
        executables = json.loads((home / "settings.json").read_text()).get("executables")
    except (OSError, ValueError, AttributeError):
        return {}
    return executables if isinstance(executables, dict) else {}


def guest_toolchain(profile, record, tools=None):
    """The clang, linker and ldid a start rebuilds guest tools with. One named
    in profile.json (`toolchain`) or in settings.json (`executables`) must be
    usable as named; otherwise the one the build's record names, then the
    workspace's own linker and ldid (this checkout's, then that of any
    directory holding the build, since an installed copy has none) and a
    clang 22 on PATH. clang must be a
    release development signing is known to work with (a clang 23 adapter does
    not activate). ValueError names every tool that is missing and where it was
    looked for."""
    import build_ios_guest_tools as builder
    configured = profile.get("toolchain", {})
    if not isinstance(configured, dict):
        raise ValueError("profile.json: toolchain must be an object naming clang, linker and ldid")
    executables = settings_executables()
    recorded = record.get("toolchain") if isinstance(record.get("toolchain"), dict) else {}

    def usable(name, value):
        path = shutil.which(value) if isinstance(value, str) and value else None
        if path is None or name != "clang":
            return path
        try:
            qualified = builder.compiler_identity(path)["major"] in builder.QUALIFIED_DEVELOPMENT_COMPILERS
        except (OSError, ValueError, subprocess.SubprocessError):
            qualified = False
        return path if qualified else None

    found, missing = {}, []
    qualified = "clang " + "/".join(map(str, builder.QUALIFIED_DEVELOPMENT_COMPILERS))
    for name in ("clang", "linker", "ldid"):
        named = [(source, value) for source, value in ((f"profile.json toolchain.{name}", configured.get(name)),
                                                       (f"settings.json executables.{name}", executables.get(name)))
                 if value]
        if named:
            source, value = named[0]
            found[name] = usable(name, value)
            if found[name] is None:
                raise ValueError(f"{source} names {value}, which is not an executable"
                                 + (f" {qualified}" if name == "clang" else ""))
            continue
        workspaces = [REPOSITORY] + (list(Path(tools).resolve().parents) if tools else [])
        candidates = [recorded.get(name)] + ([str(root / TOOLCHAIN_DEFAULTS[name]) for root in workspaces
                                              if (root / TOOLCHAIN_DEFAULTS[name]).exists() or root == REPOSITORY]
                                             if name in TOOLCHAIN_DEFAULTS else ["clang-22", "clang"])
        found[name] = next((path for path in map(lambda value: usable(name, value), candidates) if path), None)
        if found[name] is None:
            missing.append(f"{qualified if name == 'clang' else name} (looked at: "
                           + ", ".join(str(value) for value in candidates if value) + ")")
    if missing:
        raise ValueError("no " + "; no ".join(missing) + ". Name them in profile.json as toolchain {clang: a "
                         f"{qualified} executable, linker: the ARM64e aarch64-apple-darwin-ld "
                         "(tools/build_ios_linker.py), ldid: an ldid executable}")
    return found


def pinned_kernel_valid(directory, cdhash):
    """Whether a directory holds a kernel prepare_ios_adapter_pin.py derived
    for this adapter cdhash, unchanged since."""
    import prepare_ios_adapter_pin as pinning
    try:
        receipt = json.loads((directory / pinning.RECEIPT_NAME).read_text())
        data = (directory / pinning.KERNEL_NAME).read_bytes()
    except (OSError, ValueError):
        return False
    return (isinstance(receipt, dict) and receipt.get("scope") == pinning.SCOPE and receipt.get("cdhash") == cdhash
            and receipt.get("output_sha256") == hashlib.sha256(data).hexdigest())


def development_activation_from_log(text):
    """installd's development-signing adapter state, read from the development
    log (logs development): the last boot's cycle is the text after the final
    library-integrity line, and its outcome is 'activated' (the adapter's own
    active event, or the setup's activated confirmation), 'failed' (the setup
    reported a terminal failure), 'pending' (a cycle began without either) or
    'absent' (no cycle). Mirrors guest-development-protocol.h's native scan, so
    it is the fallback for a guest-tools build whose status does not yet report
    the state directly."""
    cycle = text.rfind('"phase":"library-integrity"')
    if cycle < 0:
        return "absent"
    if (text.rfind('"event":"development-signing","state":"active"') > cycle or
            text.rfind('"event":"development-setup","state":"activated"') > cycle):
        return "activated"
    if text.rfind('"event":"development-setup","state":"failed"') > cycle:
        return "failed"
    return "pending"


def boot_request(profile, boot_inputs, model):
    """The guest request of a start: the profile's boot settings, the copy's
    inputs and the engine. A device model brings its own panel (the control
    plane takes it from the model), so the profile's display profile is left
    out: it describes the Home-button phone, and a profile written before
    models existed still carries that panel. Sent along, the control plane
    would refuse the request as conflicting; dropped here, it cannot."""
    boot = dict(profile["boot"])
    boot.update(boot_inputs, engine=profile["engine"])
    # The one-step phone needs USB Home to unlock iOS. Use the matching
    # USB keyboard by default too, while preserving an explicit choice.
    if boot.get("interactive") is True:
        boot.setdefault("input_transport", "usb-hid")
    if model:
        boot.pop("display_profile", None)
        boot["device_model"] = model
    return boot


def network_boot(boot, which=shutil.which):
    """The boot's guest network: passt, or off with the reason when there is no
    passt, because a missing tool must not cost the phone its boot."""
    if boot.get("guest_network", "off") != "nat":
        return {"state": "off"}
    backend = boot.get("guest_network_backend") or which("passt")
    if not backend:
        boot["guest_network"] = "off"
        return {"state": "off", "reason": "passt is not installed (the guest_network setting asks for nat)"}
    boot["guest_network_backend"] = str(backend)
    return {"state": "nat", "backend": str(backend)}


def reserved_trust_cache(boot, output):
    """Give the boot a trust cache with spare entries, for an app deployed while
    it runs. A module that cannot be extended costs that and not the boot: the
    boot keeps the one it has, and ios.virtual.deploy says so."""
    module = output / "system.boot.tc"
    try:
        write_private(module, reserve(Path(boot["trustcache"]).read_bytes()))
    except (OSError, ValueError, KeyError) as error:
        emit("trust-cache", state="no-spare-entries", reason=str(error))
        return None
    boot["trustcache"] = str(module)
    return str(module)


class Phone:
    def __init__(self, cli, directory, model=None, unlock="home"):
        self.cli, self.directory = cli, directory
        # The device model this start describes the phone as (None: the
        # profile's own description, with a Home button) and how such a
        # device leaves its lock screen.
        self.model, self.unlock_with = model, unlock
        self.profile = json.loads((directory / "profile.json").read_text())
        if self.profile.get("format") != 1:
            raise ValueError("Unsupported virtual iPhone profile")
        self.state_path = directory / "state.json"
        self.state = json.loads(self.state_path.read_text())
        if "verified" not in self.state and self.state.get("closed") is True:
            self.state["verified"] = self.base()

    def base(self):
        """The current run as the base of later starts. The trust cache and
        graphics payload its copy was prepared with are kept too (runs from
        before they were recorded have neither): a start with other ones
        verifies the base's own before replacing them."""
        return {key: self.state[key] for key in ("run", "install", "tools", "trustcache", "graphics", "applications")
                if key in self.state}

    def save(self, **changes):
        self.state.update(changes, updated=time.time())
        temporary = self.state_path.with_suffix(".json.tmp")
        # Launch intent must reach disk before the external mutation begins.
        # Atomic rename alone does not retain it across a host power loss.
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW
        with os.fdopen(os.open(temporary, flags, 0o600), "w") as stream:
            stream.write(json.dumps(self.state, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.state_path)
        directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        if "phase" in changes:
            emit(changes["phase"], run=self.state.get("run"))

    def invoke(self, operation, arguments, timeout=180, independent=False):
        environment = dict(os.environ)
        if independent:
            # A job submitted from inside a job is its child and ends with it.
            # The guest must outlive the start that created it.
            environment.pop("TOMACCO_PARENT_JOB", None)
        done = subprocess.run([self.cli, "call", operation, json.dumps(arguments)], env=environment,
                              capture_output=True, text=True, timeout=timeout)
        if not done.stdout.strip():
            raise RuntimeError(f"{operation} produced no reply: {done.stderr.strip()[:300]}")
        return json.loads(done.stdout)

    def call(self, operation, arguments, timeout=180, independent=False):
        value = self.invoke(operation, arguments, timeout, independent)
        if not value.get("ok"):
            raise RuntimeError(f"{operation}: {value.get('error', {}).get('message', 'failed')}")
        return value["result"]

    def wait(self, job):
        # Waiting only reads the job; a job database another writer holds past
        # its busy timeout (database_error "database is locked", with many
        # local clients) is asked again, not fatal.
        busy = 0
        while job["state"] not in TERMINAL:
            value = self.invoke("job.wait", {"job_id": job["id"], "seconds": 20}, 180)
            if value.get("ok"):
                job, busy = value["result"], 0
                continue
            if value.get("error", {}).get("code") != "database_error" or busy >= 6:
                raise RuntimeError(f"job.wait: {value.get('error', {}).get('message', 'failed')}")
            busy += 1
            time.sleep(2)
        return job

    def guest_running(self):
        job = self.state.get("guest_job")
        if not job:
            if self.state.get("closed") is not True:
                raise GuestObservationError("An open run has no recorded guest job; the existing run is preserved")
            return False
        result = self.observe("job.get", {"job_id": job}, missing=True)
        if result is None:
            # The control plane has no such job (another data directory or a
            # reset job store). It runs nothing here; its storage is unverified.
            return False
        if result.get("id") != job or result.get("state") not in ACTIVE + TERMINAL:
            raise GuestObservationError("Guest job state is unknown; the existing run is preserved")
        return result["state"] in ACTIVE

    def observe(self, operation, arguments, missing=False):
        """Only a successful observation may license lifecycle decisions.

        With missing, the control plane's definitive answer that the job does
        not exist returns None; every other failure stays an observation error."""
        try:
            reply = self.invoke(operation, arguments)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            raise GuestObservationError(f"{operation} could not observe the guest: {error}") from error
        if not isinstance(reply, dict) or reply.get("ok") is not True or not isinstance(reply.get("result"), dict):
            error = reply.get("error") if isinstance(reply, dict) else None
            if missing and isinstance(error, dict) and error.get("code") == "job_not_found":
                return None
            message = error.get("message", "invalid reply") if isinstance(error, dict) else "invalid reply"
            raise GuestObservationError(f"{operation}: {message}; the existing run is preserved")
        return reply["result"]

    def finalize(self):
        """Record whether the stopped guest left disks that may be reused.

        Only a report that proves exit 0, complete hashes and a confirmed flush
        makes the run a base. A job the control plane does not know, or a
        report whose evidence has the wrong shape, proves nothing: the run is
        unverified and the next start goes back to the last verified run."""
        result = self.observe("ios.emulator.report", {"job_id": self.state["guest_job"]}, missing=True)
        if result is None:
            result = {"report": {"outcome": "job-unknown"}}
        # Evidence of the wrong shape counts as absent, never as proof.
        summary = dict(result["report"]) if isinstance(result.get("report"), dict) else {}
        stop = dict(result["stop"]) if isinstance(result.get("stop"), dict) else {}
        for record, field, expected in ((summary, "exit_code", int),
                                        (summary, "storage_hashes_complete", bool),
                                        (stop, "guest_storage_flushed", bool)):
            if record.get(field) is not None and type(record[field]) is not expected:
                record[field] = None
        # Written by the stop itself; a guest that ended any other way has none.
        flushed = stop.get("guest_storage_flushed") is True
        closed = (type(summary.get("exit_code")) is int and summary["exit_code"] == 0
                  and summary.get("storage_hashes_complete") is True
                  and flushed)
        changes = {"closed": closed, "phase": "stopped" if closed else "unverified",
                   "outcome": {**{key: summary.get(key) for key in ("outcome", "exit_code", "storage_hashes_complete")},
                               "guest_storage_flushed": flushed}}
        if closed:
            changes["verified"] = self.base()
        self.save(**changes)
        if closed:
            self.prune()
        return closed

    def owned(self, path):
        """Whether a disk copy may be deleted here: only a real directory this
        tool created inside the profile's own installs directory, never a path
        taken from elsewhere."""
        installs = Path(self.profile["installs"]).resolve()
        return (path.parent.resolve() == installs and path.name.startswith("guest-install")
                and not path.is_symlink() and path.is_dir())

    def prune(self, keep=2):
        """Keep the verified run and the last `keep` copies (after a verified
        stop: the run and its predecessor); drop older disk copies."""
        created = [Path(path) for path in self.state.get("created", [])]
        protected = {Path(self.state["verified"]["install"])} | set(created[-keep:])
        remaining = []
        for path in created:
            if path in protected or not self.owned(path):
                if path in protected or path.exists():
                    remaining.append(str(path))
                continue
            try:
                self.remove_install(path)
            except (OSError, RuntimeError) as error:
                remaining.append(str(path))
                emit("cleanup-refused", install=str(path), reason=str(error))
        self.save(created=remaining)

    def remove_install(self, path):
        """Hold all root-file leases until an owned copy has been removed.

        The launcher locks its actual storage inodes for the whole session.
        Retaining these leases through rmtree prevents deleting a live copy
        or releasing an earlier disk while checking the later ones.
        """
        if not self.owned(path):
            raise RuntimeError(f"{path} is not an owned session copy")
        with ExitStack() as leases:
            directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
            leases.callback(os.close, directory)
            identity = os.fstat(directory)
            seen = set()
            for name in sorted(os.listdir(directory)):
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    continue
                stream = leases.enter_context(os.fdopen(os.open(
                    name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory), "rb"))
                actual = os.fstat(stream.fileno())
                if not stat.S_ISREG(actual.st_mode) or (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino):
                    raise RuntimeError(f"{path / name} changed during cleanup; the copy is preserved")
                key = (actual.st_dev, actual.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError(f"{path} is in use; stop its guest before removing the copy") from None
            current = path.lstat()
            if not self.owned(path) or (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                raise RuntimeError(f"{path} changed during cleanup; the replacement is preserved")
            shutil.rmtree(path)

    def discard_attempt(self):
        """Remove the disk copy of a start that never booted its guest.

        A guest locks its disks for as long as it runs. A copy that is locked
        belongs to a guest this state does not know about and is left alone."""
        attempt = self.state.get("attempt")
        if not attempt:
            return
        path = Path(attempt["install"])
        if self.owned(path):
            self.remove_install(path)
        self.save(attempt=None)

    def abandon(self):
        """Forget a run whose guest never came up: nothing in it is worth keeping.

        The state describes the last verified run again, as if this start had
        not happened; only its number stays used."""
        verified = self.state["verified"]
        path = Path(self.state["install"])
        if str(path) != verified["install"] and self.owned(path):
            self.remove_install(path)
        self.save(run=verified["run"], install=verified["install"], tools=verified["tools"], closed=True,
                  guest_job=None, phase="stopped", error=None, application_launch_accepted=None,
                  outcome={"outcome": "cancelled-before-boot"},
                  created=[item for item in self.state.get("created", []) if item != str(path)])

    def prepare_and_boot(self, tools):
        base = self.state.get("verified")
        if not base:
            raise RuntimeError("No verified run is recorded; the profile cannot start a guest")
        # The kernel's prelinked GPU personality decides where iOS looks for
        # the Metal driver. A kernel that names another place than the one the
        # guest tools install to boots without a GPU: every frame stays black.
        named = kernel_graphics_personalities(self.profile["boot"]["kernel"])
        if named and PLUGIN_PERSONALITY not in named:
            raise RuntimeError("The boot kernel looks for the Metal driver at " + ", ".join(sorted(named))
                               + f" but the guest tools install it at {PLUGIN_PERSONALITY}; "
                               "configure a kernel built for that location")
        # The bridge opens the compiler plugin by an absolute path. A bridge
        # built for another location loads the driver but aborts every
        # MTLCompilerService, and backboardd exits ("Metal failed to build
        # render pipeline"): the panel stays black as well.
        expected = BUNDLE_PATH + "/" + COMPILER_LIBRARY_NAME
        opened = bridge_compiler_paths(self.profile["graphics"])
        if opened and expected not in opened:
            raise RuntimeError("The graphics payload's bridge opens the compiler plugin at "
                               + ", ".join(sorted(opened)) + f" but the guest tools install it at {expected}; "
                               "configure a graphics payload built for that location")
        # The kernel pins the development-signing adapter's cdhash in its
        # mmap(PROT_EXEC) allow-list; installd maps the adapter from its cache
        # only when that hash is pinned. A kernel prepared for different guest
        # tools leaves the adapter silent and every owned install rejected.
        # Refuse now with the exact remedy rather than boot a
        # phone whose development signing cannot work.
        adapter = development_library_cdhash(self.tools_for_start(tools))
        if adapter is not None:
            pinned = kernel_pins_cdhash(self.profile["boot"]["kernel"], adapter)
            if pinned is False:
                raise RuntimeError(
                    f"The boot kernel does not pin the development-signing adapter's cdhash {adapter} "
                    "of these guest tools in its executable-mapping allow-list, so installd cannot map "
                    "the adapter and every owned app install is rejected. Derive a kernel that pins it "
                    "(tools/prepare_ios_adapter_pin.py --kernel <the boot kernel> --tools <this guest-tools "
                    "build> --output <a new directory>) and point the profile's boot kernel at the result")
        self.discard_attempt()
        # A start never modifies its base, so the predecessor kept for rollback
        # is not needed while it runs; dropping it first keeps two full copies
        # on the disk instead of three (32 GiB each where copies cannot share
        # blocks). A last run that did not close stays for inspection.
        self.prune(keep=1)
        # Every attempt gets a number of its own, also one that failed or was
        # cancelled before its guest existed.
        run = max(self.state["run"], base["run"], self.state.get("attempted", 0)) + 1
        install, previous = Path(base["install"]), Path(base["tools"])
        tools = self.tools_for_start(tools)
        output = Path(self.profile["installs"]) / f"guest-install{run}"
        while output.exists() or output.is_symlink():
            run += 1
            output = Path(self.profile["installs"]) / f"guest-install{run}"
        prepare = dict(self.profile["prepare"])
        if not Path(prepare.get("helper_kernel", "")).is_file():
            # A kernel kept beside the support APFS module survives host kernel
            # updates; the running kernel only works while the module matches it.
            pinned = Path(prepare.get("support", "")) / "apfs/vmlinuz"
            prepare["helper_kernel"] = str(pinned) if pinned.is_file() else f"/usr/lib/modules/{os.uname().release}/vmlinuz"
        prepare.update(disk=str(install / "system.raw"), auxiliary_disk=str(install / "auxiliary.raw"),
                       sep_storage=str(install / "sep.raw"), tools=str(tools), previous_tools=str(previous),
                       output=str(output), graphics=self.profile["graphics"])
        for key, previous_key in (("trustcache", "previous_trustcache"), ("graphics", "previous_graphics")):
            if base.get(key) and base[key] != prepare.get(key):
                prepare[previous_key] = base[key]
        # Recorded before the copy exists: a start that fails or is cancelled
        # must not leave a copy behind that blocks or outlives later starts.
        self.save(phase="preparing", error=None, attempted=run, attempt={"run": run, "install": str(output)})
        try:
            job = self.wait(self.call("ios.emulator.prepare_guest_tools", prepare))
            record_path = output / "guest-tools-install.json"
            record = json.loads(record_path.read_text()) if record_path.is_file() else {}
            if job["state"] != "succeeded" or record.get("state") != "installed":
                raise RuntimeError("Preparation failed: " + str(record.get("error") or job["state"]))
            # iOS's update cleanup once removed the Metal driver from Preboot
            # between boots; the copy's record says whether it was still there
            # (verified) or had to be put back (restored). The copy that holds
            # the record is deleted later, so the state keeps the answer.
            driver = (record.get("graphics_payload") or {}).get("state")
            self.save(graphics_driver=driver)
            emit("prepared", run=run, graphics_driver=driver)
            boot = boot_request(self.profile, record["boot_inputs"], self.model)
            network = network_boot(boot)
            emit("guest-network", **network)
            module = reserved_trust_cache(boot, output)
            guest = self.call("ios.emulator.paravirtual", boot, independent=True)
        except BaseException:
            # Also on cancellation, as far as this process still gets to run.
            try:
                self.discard_attempt()
            except (OSError, RuntimeError):
                pass
            raise
        # The apps installed on the base's data volume are this run's too,
        # until the start installs more (install_applications).
        self.save(run=run, install=str(output), tools=str(tools), trustcache=prepare.get("trustcache"),
                  graphics=prepare["graphics"], guest_job=guest["id"], closed=False,
                  phase="booting", graphics_payload=record.get("graphics_payload", {}).get("state"),
                  created=self.state.get("created", []) + [str(output)], application_launch_accepted=None,
                  attempt=None, model=self.model or "", applications=dict(base.get("applications") or {}),
                  guest_network=network, trust_cache_module=module)

    def tools_for_start(self, tools):
        """The guest-tools build the next start installs: the one asked for,
        else the one the profile names (a start then upgrades the base's
        tools to it), else the base's own tools again."""
        base = self.state.get("verified")
        if not base:
            raise RuntimeError("No verified run is recorded; the profile cannot start a guest")
        return tools or (Path(self.profile["tools"]) if self.profile.get("tools") else Path(base["tools"]))

    def deployable_applications(self, tools):
        """The staged apps of the next start (staged_applications). Profile
        tools that lack a listed app, or carry another copy of it, are rebuilt
        with the profile's apps (rebuild_tools) and the profile then names the
        rebuilt set, as are tools older than the workspace's guest sources
        (refreshed_applications); a build named for this start only is never
        replaced."""
        current = self.tools_for_start(tools)
        try:
            staged = staged_applications(self.profile, current)
        except ToolsMismatch as mismatch:
            if tools:
                raise
            return staged_applications(self.profile, self.rebuild_tools(Path(current), mismatch))
        return staged if tools else self.refreshed_applications(Path(current), staged)

    def refreshed_applications(self, current, staged):
        """The staged apps once the guest tools follow the workspace's guest
        sources. An upgraded product brings new sources while every app bundle
        stays as it was, so no ToolsMismatch would ever carry them to an
        existing profile. The set is rebuilt as for a changed app; its name
        follows from its inputs, so a record that never matches the workspace
        costs no second build. The tools still work, so nothing here ends the
        start: what stands in the way (no toolchain, a build that cannot be
        rebuilt, a compile that failed) keeps them and is reported. A failed
        compile is not tried again until an input changes."""
        try:
            changed = changed_guest_sources(current)
            if not changed:
                return staged
            rebuilt = self.rebuild_tools(current, f"The workspace's guest sources differ from {current} "
                                                  f"({', '.join(changed)})", retry_failed=False)
            staged = staged_applications(self.profile, rebuilt)
        except Exception as error:
            emit("guest-tools", state="kept", tools=str(current), reason=str(error))
            return staged
        remaining = changed_guest_sources(rebuilt)
        if remaining:
            emit("guest-tools", state="differs", tools=str(rebuilt), sources_changed=remaining)
        return staged

    def rebuild_tools(self, current, mismatch, retry_failed=True):
        """Build the guest tools the profile's apps need, the way `current`
        was built (its record's options, trust-cache source and the workspace
        sources) but staging exactly the profile's apps, into the phone's own
        guest-tools/<content-addressed name>; a set built from the same inputs
        before is reused. The rebuilt adapter's policy table changes its
        cdhash; a boot kernel that does not pin it is replaced by one derived
        from it (pinned_kernel). profile.json then names both, its previous
        content kept beside it. Nothing is changed when any step fails. A
        compile that failed is recorded; with retry_failed False the same
        inputs are refused instead of compiled again."""
        import build_ios_guest_tools as builder
        refuse = lambda reason: RuntimeError(f"{mismatch}. The start cannot rebuild it by itself: {reason}")
        try:
            record = json.loads((current / "guest-tools.json").read_text())
            options = rebuild_options(record)
            toolchain = guest_toolchain(self.profile, record, current)
        except (OSError, ValueError) as error:
            raise refuse(error) from None
        trustcache = self.profile["prepare"].get("trustcache")
        if not isinstance(trustcache, str) or not trustcache:
            raise refuse("profile.json names no prepare.trustcache")
        try:
            source = hashlib.sha256(Path(trustcache).read_bytes()).hexdigest()
            # A recorded source a later product dropped: the builder's own
            # digest below tells this set from the one before.
            sources = {name: hashlib.sha256((builder.SOURCES / name).read_bytes()).hexdigest()
                       for name in options["sources"] if (builder.SOURCES / name).is_file()}
            sources[Path(builder.__file__).name] = hashlib.sha256(Path(builder.__file__).read_bytes()).hexdigest()
        except OSError as error:
            raise refuse(error) from None
        if source != options["trustcache_sha256"]:
            raise refuse(f"the profile's trust cache {trustcache} is not the one {current} was built from")
        applications = []
        for row in self.profile.get("applications") or []:
            entitlements = row.get("entitlements") if isinstance(row.get("entitlements"), str) else ""
            try:
                inspected = inspect_bundle(row["bundle"], entitlements or None)
            except (OSError, ValueError) as error:
                raise RuntimeError(f"App bundle {row['bundle']} cannot be deployed: {error}") from error
            applications.append({"bundle": row["bundle"], "entitlements": entitlements,
                                 "bundle_id": inspected["bundle_id"], "name": inspected["name"],
                                 "content_sha256": inspected["content_sha256"],
                                 "granted": inspected["entitlements"]})
        inputs = {key: options[key] for key in options if key != "sources"}
        inputs.update(sources=sources, toolchain=toolchain,
                      applications=[{key: app[key] for key in ("bundle_id", "name", "content_sha256", "granted")}
                                    for app in applications])
        name = "guest-tools-" + hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:16]
        output = self.directory / "guest-tools" / name
        if output.exists() or output.is_symlink():
            try:
                staged_applications(self.profile, output)
            except RuntimeError as error:
                raise RuntimeError(f"The phone's guest-tools build {output} does not carry what its name says "
                                   f"({error}); remove it and start again") from None
            emit("guest-tools", state="reused", tools=str(output), reason=str(mismatch))
        else:
            failed = self.state.get("guest_tools_failed") or {}
            if not retry_failed and failed.get("name") == name:
                raise RuntimeError(f"Building {name} from these same inputs failed before: {failed.get('error')}")
            output.parent.mkdir(exist_ok=True)
            # A build ended by a cancelled start leaves its private staging behind.
            for stale in output.parent.glob(".tomacco-guest-tools-*"):
                if stale.is_dir() and not stale.is_symlink():
                    shutil.rmtree(stale)
            emit("guest-tools", state="building", tools=str(output), reason=str(mismatch), from_tools=str(current),
                 applications=[app["bundle_id"] for app in applications],
                 sources_changed=sorted(key for key, value in options["sources"].items() if sources.get(key) != value))
            arguments = argparse.Namespace(
                output=output, trustcache=Path(trustcache), clang=toolchain["clang"],
                linker=Path(toolchain["linker"]), ldid=Path(toolchain["ldid"]), allow_unqualified_compiler=False,
                minimum_ios=options["minimum_ios"], agent=True, application_probe=True, development_signing=True,
                graphics_query_access=options["graphics_query_access"], quiet_services=options["quiet_services"],
                virtual_battery=options["virtual_battery"],
                applications=[app["bundle"] for app in applications],
                application_entitlements=[app["entitlements"] for app in applications])
            try:
                builder.build(arguments)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                self.save(guest_tools_failed={"name": name, "error": str(error)[:500]})
                raise RuntimeError(f"The guest-tools rebuild into {output} failed: {error}") from error
            staged_applications(self.profile, output)
            emit("guest-tools", state="built", tools=str(output))
        kernel = Path(self.profile["boot"]["kernel"])
        adapter = development_library_cdhash(output)
        if adapter is not None and kernel_pins_cdhash(kernel, adapter) is False:
            kernel = self.pinned_kernel(kernel, output, adapter)
        self.update_profile(name, output, kernel)
        return output

    def pinned_kernel(self, kernel, tools, cdhash):
        """The boot kernel with the adapter slot of its executable-mapping
        allow-list set to `cdhash` (prepare_ios_adapter_pin.py), in the
        phone's kernels/adapter-pin-<cdhash>. Only that slot differs between
        the kernels it derives, so one per cdhash is reused."""
        import prepare_ios_adapter_pin as pinning
        output = self.directory / "kernels" / ("adapter-pin-" + cdhash)
        destination = output / pinning.KERNEL_NAME
        if pinned_kernel_valid(output, cdhash):
            emit("kernel", state="reused", kernel=str(destination), cdhash=cdhash)
            return destination
        if output.exists() or output.is_symlink():
            # What an interrupted derivation left; never the kernel booted from.
            if output.is_symlink() or not output.is_dir() or kernel.resolve().is_relative_to(output.resolve()):
                raise RuntimeError(f"{output} is not a pinned kernel this phone derived; remove it")
            shutil.rmtree(output)
        output.parent.mkdir(exist_ok=True)
        emit("kernel", state="pinning", kernel=str(destination), cdhash=cdhash, from_kernel=str(kernel))
        try:
            pinning.build(kernel, tools, output)
        except (OSError, ValueError) as error:
            raise RuntimeError(f"The boot kernel {kernel} does not pin the development-signing adapter's cdhash "
                               f"{cdhash} of the rebuilt guest tools {tools}, and no kernel pinning it can be "
                               f"derived from it: {error}") from error
        emit("kernel", state="pinned", kernel=str(destination), cdhash=cdhash)
        return destination

    def update_profile(self, label, tools, kernel):
        """Name these tools and boot kernel in profile.json, read again so a
        configuration made meanwhile is kept; the previous file is kept as
        profile.json.before-<label>."""
        path = self.directory / "profile.json"
        current = path.read_bytes()
        profile = json.loads(current)
        if profile.get("tools") == str(tools) and profile["boot"].get("kernel") == str(kernel):
            self.profile = profile
            return
        backup, number = self.directory / f"profile.json.before-{label}", 1
        while backup.exists() or backup.is_symlink():
            number += 1
            backup = self.directory / f"profile.json.before-{label}-{number}"
        with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600), "wb") as stream:
            stream.write(current)
        profile["tools"] = str(tools)
        profile["boot"]["kernel"] = str(kernel)
        temporary = path.with_suffix(".json.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW
        with os.fdopen(os.open(temporary, flags, 0o600), "w") as stream:
            stream.write(json.dumps(profile, indent=1) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        self.profile = profile
        emit("profile", tools=str(tools), kernel=str(kernel), backup=str(backup))

    def install(self, job, guest_path, bundle_id):
        """Install the app staged at guest_path through installd and wait for
        its outcome, the way tools/ios_lifecycle.py does: a reply that does
        not come in time is followed through the guest's receipt for that
        request, and an install is never sent twice."""
        deadline = time.monotonic() + 600
        while True:
            reply = self.invoke("ios.emulator.guest", {"job_id": job, "operation": "install",
                                                       "argument": guest_path, "timeout_seconds": 120}, 240)
            if reply.get("error", {}).get("code") == "ios_guest_stopped":
                self.finalize()
                raise RuntimeError(f"The guest stopped while {bundle_id} was being installed")
            response = (reply.get("result") or {}).get("response") or {}
            # Refused before any work while an earlier worker is still ending.
            if response.get("error") != "guest_operation_busy" or time.monotonic() >= deadline:
                break
            time.sleep(15)
        details = reply.get("error", {}).get("details") or {}
        request = self.launch_request_id(details.get("request_id"))
        if not reply.get("ok") and details.get("request_may_have_executed") is True and request:
            deadline = time.monotonic() + 1800
            response = {}
            while time.monotonic() < deadline:
                result = self.invoke("ios.emulator.guest_result", {"job_id": job, "request_id": request,
                                                                   "timeout_seconds": 30}, 120)
                if result.get("error", {}).get("code") == "ios_guest_stopped":
                    self.finalize()
                    raise RuntimeError(f"The guest stopped while {bundle_id} was being installed")
                value = result.get("result") or {}
                if value.get("request_id") != request or value.get("state") == "running":
                    time.sleep(15)
                    continue
                response = (value.get("response") or {}) if value.get("state") == "completed" else {}
                break
        if response.get("ok") is not True or response.get("request_accepted") is not True:
            native = response.get("native_error")
            raise RuntimeError(f"installd did not install {bundle_id} from {guest_path}: "
                               + str(response.get("error") or reply.get("error", {}).get("code") or "no outcome")
                               + (" " + json.dumps(native)[:400] if native else ""))
        return response

    def install_applications(self, job, staged):
        """Install every staged app whose signed executable is not on this
        run's data volume yet. The state records, per verified base, which
        cdhash of each app was installed; a rebuilt app is installed again,
        an unchanged one is left as it is."""
        installed = dict(self.state.get("applications") or {})
        pending = [app for app in staged if installed.get(app["bundle_id"]) != app["cdhash"]]
        for app in staged:
            if app not in pending:
                emit("application", bundle_id=app["bundle_id"], state="installed-before", cdhash=app["cdhash"])
        if not pending:
            return
        self.save(phase="installing")
        for app in pending:
            receipt = self.install(job, app["guest_path"], app["bundle_id"])
            installed[app["bundle_id"]] = app["cdhash"]
            self.save(applications=installed)
            emit("application", bundle_id=app["bundle_id"], state="installed", cdhash=app["cdhash"],
                 replaced_app_ended=receipt.get("running_application_ended"))

    def development_signing_state(self, job):
        """installd's development-signing adapter activation state: 'activated',
        'failed', 'pending', 'absent', 'unreadable' or 'unknown'. Guest tools
        from 370 report it in the control service's status; older builds are
        read from the development log (logs development) as the fallback."""
        reply = self.invoke("ios.emulator.guest", {"job_id": job, "operation": "status",
                                                   "timeout_seconds": 60}, 120)
        response = (reply.get("result") or {}).get("response") or {} if reply.get("ok") else {}
        reported = response.get("development_signing_activation")
        if isinstance(reported, str):
            return reported
        reply = self.invoke("ios.emulator.guest", {"job_id": job, "operation": "logs",
                                                   "argument": "development", "timeout_seconds": 60}, 120)
        response = (reply.get("result") or {}).get("response") or {} if reply.get("ok") else {}
        if response.get("ok") is not True:
            return "absent" if response.get("error") == "unknown_log" else "unknown"
        return development_activation_from_log(response.get("text") or "")

    def await_development_signing(self, job, staged):
        """Wait for installd's development-signing adapter to activate before a
        staged app is installed. installd admits an ad-hoc app only while the
        adapter is active; an install sent before it is rejected as an invalid
        signature after a long installer wait. A build with no staged apps carries no adapter and is not
        waited for."""
        if not staged:
            return
        deadline = time.monotonic() + 600
        state = "unknown"
        while time.monotonic() < deadline:
            state = self.development_signing_state(job)
            if state == "activated":
                emit("development-signing", activation="activated")
                return
            if state == "failed":
                break
            time.sleep(15)
        emit("development-signing", activation=state)
        raise RuntimeError(
            "installd's development-signing adapter did not activate"
            + (" (it reported a failed setup)" if state == "failed" else f" in time (last state {state!r})")
            + "; the staged apps cannot be installed, because installd admits an ad-hoc app only "
              "while the adapter is active. See the setup helper's last state in the development log "
              "(ios.emulator.guest logs development)")

    def deploy(self, bundle, entitlements, open_app):
        """Install an app on the running phone without a reboot: sign it, exchange
        spare trust-cache entries for its hashes while the guest is stopped, send
        it over, name it to the adapter, let installd install it and open it. The
        profile lists it afterwards, so the next start carries it as usual."""
        started = time.monotonic()
        stage = lambda name, **details: emit("deploy", step=name, seconds=round(time.monotonic() - started, 1), **details)
        if not self.guest_running():
            raise RuntimeError("The phone is not running. Add the app with ios.virtual.configure and start the phone")
        job = self.state["guest_job"]
        module_path = Path(self.state.get("trust_cache_module") or "")
        if not module_path.is_file():
            raise RuntimeError("This boot reserved no spare trust-cache entries (it started before they existed); "
                               "stop and start the phone")
        toolchain = guest_toolchain(self.profile, json.loads((Path(self.state["tools"]) / "guest-tools.json").read_text()),
                                    Path(self.state["tools"]))
        with tempfile.TemporaryDirectory(prefix=".tomacco-deploy-", dir=self.directory) as temporary:
            payload = Path(temporary)
            taken = {}
            rows, application = stage_application(bundle, entitlements, payload, str(Path(toolchain["ldid"]).resolve(strict=True)), taken)
            bundle_id = application["bundle_id"]
            signed = payload / application["path"]
            stage("signed", bundle_id=bundle_id, cdhash=application["cdhash"], programs=len(rows))
            module = with_hashes(module_path.read_bytes(), [bytes.fromhex(row["cdhash"]) for row in rows])
            write_private(module_path.with_name("system.boot.tc.next"), module)
            self.call("ios.emulator.control", {"job_id": job, "action": "trust_cache_update",
                                               "path": str(module_path.with_name("system.boot.tc.next"))})
            os.replace(module_path.with_name("system.boot.tc.next"), module_path)
            stage("trusted", spare_entries=spare_entries(module))
            guest_path = self.send_bundle(job, signed)
            stage("sent", guest_path=guest_path)
            for identity, cdhash in policy_entries(rows):
                response = self.deploy_request(job, "policy_add", f"{cdhash.hex()}:{identity}")
                if response.get("ok") is not True:
                    raise RuntimeError(f"The guest refused the signing policy of {identity}: "
                                       + str(response.get("error")))
            self.install(job, guest_path, bundle_id)
            stage("installed")
        installed = dict(self.state.get("applications") or {})
        installed[bundle_id] = application["cdhash"]
        self.save(applications=installed)
        # The next start stages it like any listed app.
        configured = {"add_application": str(bundle)}
        if entitlements:
            configured["add_entitlements"] = str(entitlements)
        self.call("ios.virtual.configure", configured)
        self.profile = json.loads((self.directory / "profile.json").read_text())
        if open_app:
            response = self.deploy_request(job, "open", bundle_id)
            if response.get("ok") is not True:
                raise RuntimeError(f"{bundle_id} installed, but it did not open: " + json.dumps(response)[:600])
            stage("opened", pid=response.get("pid"))
        stage("done", bundle_id=bundle_id)

    def deploy_request(self, job, operation, argument):
        """One guest request. The guest refuses a new one while an earlier worker
        is still ending; that comes before any work, so it is asked again."""
        deadline = time.monotonic() + 120
        while True:
            reply = self.call("ios.emulator.guest", {"job_id": job, "operation": operation, "argument": argument,
                                                     "timeout_seconds": 60}, 180)
            response = reply.get("response") or {}
            if response.get("error") != "guest_operation_busy" or time.monotonic() >= deadline:
                return response
            time.sleep(2)

    def guest_channel(self, job):
        """The host's end of the guest's vsock, from the run's request record."""
        record = self.call("job.get", {"job_id": job})
        return guest_socket(Path(record["plan"]["cwd"]) / "paravirtual" / "request.json")

    def send_bundle(self, job, bundle):
        """Ask the guest to listen, then send the bundle to it. Both sides of
        receive_bundle run at once: the guest's reply comes after the last byte."""
        path = self.guest_channel(job)
        result = {}

        def ask():
            result["reply"] = self.invoke("ios.emulator.guest", {"job_id": job, "operation": "receive_bundle",
                                                                 "timeout_seconds": 60}, 180)
        waiter = threading.Thread(target=ask, daemon=True)
        waiter.start()
        try:
            with connect_port(path, BUNDLE_PORT, 60) as stream:
                files, size = send_bundle(stream, bundle)
        except RuntimeError as error:
            waiter.join(10)
            raise RuntimeError(f"{error}; the guest said: " + json.dumps(result.get("reply"))[:400]) from None
        waiter.join(200)
        response = ((result.get("reply") or {}).get("result") or {}).get("response") or {}
        # A big bundle outlives the call's own minute; its receipt holds the answer.
        request = self.launch_request_id(((result.get("reply") or {}).get("error") or {}).get("details", {}).get("request_id"))
        deadline = time.monotonic() + 600
        while not response and request and time.monotonic() < deadline:
            value = self.invoke("ios.emulator.guest_result", {"job_id": job, "request_id": request,
                                                              "timeout_seconds": 30}, 120).get("result") or {}
            if value.get("state") != "running":
                response = value.get("response") or {}
                break
            time.sleep(5)
        if response.get("ok") is not True or (response.get("files"), response.get("bytes")) != (files, size):
            raise RuntimeError(f"The guest did not take the bundle ({files} files, {size} bytes): "
                               + json.dumps(result.get("reply"))[:600])
        return response["path"]

    def start(self, tools, open_app):
        host = host_software()
        staged = None
        if not self.guest_running():
            if self.state.get("closed") is not True and self.state.get("guest_job") and not self.finalize():
                emit("fallback", reason="The last run did not close with verified storage; "
                                        "starting from the last verified run")
            known = self.state.get("host_software")
            if isinstance(known, dict) and known != host:
                emit("host-changed", since_run=self.state.get("host_software_run"), changes={
                    key: [known.get(key), host.get(key)]
                    for key in sorted(set(known) | set(host)) if known.get(key) != host.get(key)})
            # Named before the disk copy: a bundle that cannot be deployed
            # costs no preparation. Tools that lack a listed app, or carry an
            # older copy of it, are rebuilt with the profile's apps first.
            staged = self.deployable_applications(tools)
            self.prepare_and_boot(tools)
        # A guest that is already running is adopted instead of starting a second one.
        job = self.state["guest_job"]
        if staged is None:
            staged = staged_applications(self.profile, self.state["tools"])
        staged = staged + owned_application(self.state["tools"])
        deadline = time.monotonic() + 3600
        while True:
            status = self.invoke("ios.emulator.guest", {"job_id": job, "operation": "status", "timeout_seconds": 60})
            if status.get("ok") and status["result"]["response"].get("ok"):
                break
            if status.get("error", {}).get("code") == "ios_guest_stopped":
                self.finalize()
                outcome = (self.state.get("outcome") or {}).get("outcome")
                if outcome == "host-pressure-blocked":
                    # The guest never ran; nothing about the phone failed.
                    raise RuntimeError("The host was too busy (I/O or memory pressure) to boot the guest; "
                                       "start again once it is quieter")
                raise RuntimeError("The guest stopped during boot"
                                   + (f" ({outcome})" if outcome else "") + "; see ios.emulator.report")
            if time.monotonic() > deadline:
                raise RuntimeError("The guest service did not answer within an hour")
            time.sleep(15)
        self.apply_watchdog_scale(job, status["result"]["response"])
        # The staged apps go in before the launch (the app to open may be one of
        # them) and before the time-zone step: installd admits an ad-hoc app only
        # while the development-signing adapter is active in installd, and that
        # adapter is the one the boot's setup helper configured. Waiting for the
        # adapter and installing promptly keeps the install on that installd;
        # letting the time-zone step run first delays it past that installd,
        # so the install would reach a fresh installd without the adapter
        # and iOS would reject the ad-hoc signature.
        # A staged app that cannot be installed must not cost the phone its
        # time zone or its launch: the failure is recorded under
        # applications_error (ios.virtual.status shows it) and the start goes on.
        try:
            self.await_development_signing(job, staged)
            self.install_applications(job, staged)
            if staged:
                self.save(applications_error=None)
        except RuntimeError as error:
            self.save(applications_error=str(error))
            emit("installing", outcome="failed", error=str(error))
        # After the install for the reason above, before the launch so that
        # the app opened next comes from a SpringBoard with the right scale.
        self.apply_display_scale(job, status["result"]["response"])
        self.apply_time_zone(job, status["result"]["response"])
        self.apply_auto_lock(job, status["result"]["response"])
        self.apply_effects(job, status["result"]["response"])
        self.save(phase="unlocking")
        if open_app:
            self.launch(job, deadline)
        self.save(phase="launch-accepted" if open_app else "service-ready",
                  application_launch_accepted=bool(open_app), application_scene_verified=False,
                  display_visible=None)
        if open_app:
            self.observe_display(job)
        # Only a start that ends with a picture vouches for the host it ran on.
        if self.state.get("display_visible") is True or not open_app:
            self.save(host_software=host, host_software_run=self.state.get("run"))

    def observe_display(self, job):
        """Look at the panel once the app is in front. iOS can accept launches
        while every captured frame stays black; such a start must not read as
        a working phone. A dark
        frame is reported, not treated as a failed start: the guest works."""
        visible = None
        for attempt in range(4):
            if attempt:
                time.sleep(15)
            reply = self.invoke("ios.emulator.control", {"job_id": job, "action": "screenshot"}, 120)
            path = ((reply.get("result") or {}).get("result") or {}).get("path") if reply.get("ok") else None
            visible = frame_lit(Path(path)) if isinstance(path, str) else None
            if visible:
                break
        self.save(display_visible=visible)
        if visible is False:
            emit("display", visible=False,
                 warning="iOS accepted the launch but the display shows only black; see ios.emulator.report")

    def apply_watchdog_scale(self, job, status):
        """Give iOS's launch watchdogs the time this emulated guest needs.

        FrontBoard multiplies its launch allowances by FBLaunchWatchdogScale
        and its resume and scene allowances by FBLaunchWatchdogResumeScale;
        the guest operation watchdog_scale sets both (guest tools older than
        359 set only the first and report no resume scale) and BKNoWatchdogs,
        since the move to the background has a fixed ten seconds no factor
        reaches (older guest tools do not report it). SpringBoard reads
        them when it starts; without them an app, a widget renderer or the
        lock screen's wallpaper poster is ended (0x8badf00d) before its scene
        is ready. The values live on the guest's data volume, so a run started
        from a base that already carries them needs no SpringBoard restart. A
        guest without the operation, or one that refuses it, keeps iOS's own
        allowances and the start goes on. Setting the same value again is
        harmless, so an unknown outcome is simply left to the next start."""
        scale = self.profile.get("launch_watchdog_scale", 8)
        if (status.get("launch_watchdog_scale_supported") is not True or type(scale) is not int
                or not 2 <= scale <= 30):
            return
        reply = self.invoke("ios.emulator.guest", {"job_id": job, "operation": "watchdog_scale",
                                                   "argument": str(scale), "timeout_seconds": 30}, 90)
        native = (reply.get("result") or {}).get("response") or {}
        if not reply.get("ok"):
            # Right after a boot the preference daemon can take longer than
            # the reply's 30 s; the guest keeps working on it. Its
            # receipt says how it ended, so the request is never sent twice.
            native = self.completed_receipt(job, reply, "watchdog_scale", time.monotonic() + 240) or native
        if native.get("ok") is not True or native.get("scale") != scale:
            emit("watchdog-scale", applied=False,
                 reason=native.get("error") or reply.get("error", {}).get("code"))
            return
        restarted = False
        resume_changed = "resume_scale" in native and native.get("previous_resume_scale") != scale
        declining_changed = "no_watchdogs" in native and native.get("previous_no_watchdogs") != native.get("no_watchdogs")
        if native.get("previous_scale") != scale or resume_changed or declining_changed:
            restart = self.invoke("ios.emulator.guest", {"job_id": job, "operation": "restart_ui",
                                                         "timeout_seconds": 55}, 120)
            restarted = (restart.get("ok") is True and
                         ((restart.get("result") or {}).get("response") or {}).get("ok") is True)
        self.save(launch_watchdog_scale=scale)
        emit("watchdog-scale", applied=True, scale=scale, springboard_restarted=restarted)

    def apply_display_scale(self, job, status):
        """Repair a media whose cached main-screen-scale is not the boot's.

        MobileGestalt keeps the answer in a persistent cache on the data
        volume, so media first booted without a panel, or with another one,
        answers a stale scale (such as 0) under a tree that names the right one:
        Settings then traps in its first commit and the scene draws at the wrong
        size. The repair is this sequence: display_refresh, a fresh display_state
        reading the boot's scale, restart_ui. It persists, so later starts only
        read. An app's scene configuration follows at a later boot. Anything that
        goes wrong is recorded and the start goes on with the phone as it was."""
        if any(status.get(key) is not True for key in (
                "display_state_supported", "display_cache_refresh_supported", "ui_restart_supported")):
            return
        try:
            self.repair_display_scale(job, time.monotonic() + 300)
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
            emit("display-scale", outcome="unknown", error=str(error))

    def repair_display_scale(self, job, deadline):
        chosen, main = self.display_scales(job, deadline)
        if chosen is None or main is None or not 1 <= chosen <= 4:
            # A boot scale of 0 is what a tree without display-scale reads;
            # the guest refuses to rebuild the cache from that, or from none.
            emit("display-scale", outcome="skipped", chosen=chosen, main_screen_scale=main)
            return
        if main == chosen:
            self.save(display_scale={"run": self.state.get("run"), "chosen": chosen, "main_screen_scale": main,
                                     "outcome": "current"})
            emit("display-scale", outcome="current", chosen=chosen)
            return
        # Once per media copy: the guest says a refresh may have changed the
        # cache even when it reports failure, and never permits a retry. An
        # attempt in this run, or in one this run's copy descends from, stops
        # another; one in a run that was given up did not reach this copy.
        run, last = self.state.get("run"), self.state.get("display_scale") or {}
        known = last.get("run")
        if (last.get("outcome") != "current" and last.get("chosen") == chosen and isinstance(known, int) and
                (known == run or known <= (self.state.get("verified") or {}).get("run", 0))):
            emit("display-scale", outcome="not-repeated", chosen=chosen, main_screen_scale=main,
                 attempted_in_run=known, attempt_outcome=last.get("outcome"))
            return
        attempt = {"run": run, "chosen": chosen, "main_screen_scale": main}
        # On disk before the guest's cache may change.
        self.save(display_scale={**attempt, "outcome": "requested"})
        while True:
            reply = self.invoke("ios.emulator.guest", {"job_id": job, "operation": "display_refresh",
                                                       "timeout_seconds": 55}, 120)
            native = (reply.get("result") or {}).get("response") or {}
            if native.get("error") != "guest_operation_busy" or time.monotonic() >= deadline:
                break
            time.sleep(5)
        details = reply.get("error", {}).get("details") or {}
        if native.get("error") == "guest_operation_busy" or (
                not reply.get("ok") and details.get("request_may_have_executed") is not True):
            # Refused before any work: no attempt, the next start asks again.
            self.save(display_scale=last or None)
            emit("display-scale", outcome="not-sent", chosen=chosen, main_screen_scale=main,
                 reason=native.get("error") or reply.get("error", {}).get("code"))
            return
        if not reply.get("ok"):
            native = self.completed_receipt(job, reply, "display_refresh", deadline) or native
        if native.get("ok") is not True:
            outcome = native.get("error") or reply.get("error", {}).get("code") or "unknown_outcome"
            self.save(display_scale={**attempt, "outcome": outcome})
            emit("display-scale", outcome=outcome, chosen=chosen, main_screen_scale=main)
            return
        # The helper's acknowledgement is not a verified rebuild; only a new
        # worker's read is, and only that may restart the interface.
        _, repaired = self.display_scales(job, deadline)
        if repaired != chosen:
            self.save(display_scale={**attempt, "outcome": "not_confirmed", "read_back": repaired})
            emit("display-scale", outcome="not_confirmed", chosen=chosen, main_screen_scale=repaired)
            return
        restart = self.invoke("ios.emulator.guest", {"job_id": job, "operation": "restart_ui",
                                                     "timeout_seconds": 55}, 120)
        restarted = (restart.get("ok") is True and
                     ((restart.get("result") or {}).get("response") or {}).get("ok") is True)
        self.save(display_scale={**attempt, "outcome": "repaired", "springboard_restarted": restarted})
        emit("display-scale", outcome="repaired", chosen=chosen, previous=main, springboard_restarted=restarted)

    def display_scales(self, job, deadline):
        """The boot's display scale and MobileGestalt's main-screen-scale as a
        new worker reads them; None for one that is not known."""
        while True:
            reply = self.query_reply(job, "display_state", deadline)
            native = (reply.get("result") or {}).get("response") or {}
            if native.get("error") != "guest_operation_busy" or time.monotonic() >= deadline:
                break
            time.sleep(5)
        display = (native.get("native_display") or {}) if native.get("ok") is True else {}
        return (display.get("chosen_display_scale") if display.get("chosen_display_scale_known") is True else None,
                display.get("main_screen_scale") if display.get("main_screen_scale_known") is True else None)

    def apply_time_zone(self, job, status):
        """Give the guest the host's time zone.

        A guest restored from the image keeps the image's zone (America/
        Los_Angeles on this phone, eight hours behind a host in Europe/
        Vienna): the development setup finishes Setup without the zone step.
        Guest tools from 368 set it through iOS's own link daemon (the
        operation time_zone; status.time_zone_supported) and read the link
        back; it lives on the data volume and running processes follow, so
        no restart. The operation is asked on every start: it is idempotent
        and cheap, a run may come from another base, and the reply says
        whether anything changed. A host whose zone is no zoneinfo name, a
        guest without the operation, or one that refuses it (a control
        service without the tzlink entitlement is expected to answer
        time_zone_not_permitted, the guest's name for tzlink's EPERM; not
        observed yet) leaves the guest's zone alone and the start goes on;
        the outcome is recorded under time_zone either way."""
        zone = host_time_zone()
        if status.get("time_zone_supported") is not True or not zone:
            reason = "unsupported" if status.get("time_zone_supported") is not True else "host_zone_unknown"
            self.save(time_zone={"zone": zone, "outcome": "skipped", "reason": reason})
            emit("time-zone", outcome="skipped", reason=reason, zone=zone)
            return
        # The worker may still be busy with the watchdog scale or an install;
        # a refusal before any work is safe to ask again, for a bounded time.
        deadline = time.monotonic() + 120
        while True:
            reply = self.invoke("ios.emulator.guest", {"job_id": job, "operation": "time_zone",
                                                       "argument": zone, "timeout_seconds": 30}, 90)
            native = (reply.get("result") or {}).get("response") or {}
            if native.get("error") != "guest_operation_busy" or time.monotonic() >= deadline:
                break
            time.sleep(5)
        if not reply.get("ok"):
            # The link daemon starts on demand and the guest is slow right
            # after a boot: a late reply is followed through its receipt,
            # never sent twice here (the next start asks again).
            native = self.completed_receipt(job, reply, "time_zone", time.monotonic() + 120) or native
        if native.get("ok") is not True or native.get("time_zone") != zone:
            error = native.get("error") or reply.get("error", {}).get("code") or "unknown_outcome"
            self.save(time_zone={"zone": zone, "outcome": error})
            emit("time-zone", outcome=error, zone=zone)
            return
        record = {"zone": zone, "outcome": "applied", "previous_link": native.get("previous_link"),
                  "link": native.get("link"), "changed": native.get("previous_link") != native.get("link")}
        self.save(time_zone=record)
        emit("time-zone", **record)

    def apply_auto_lock(self, job, status):
        """Keep the guest's display from ever locking by itself.

        After iOS's idle timeout the display goes off and the phone locks:
        the renderer goes idle, the desktop keeps showing the last frame as
        if it were live, and every tap first has to wake and unlock the
        phone. Guest tools that report status.auto_lock_supported set
        Settings' Auto-Lock to Never (the operation auto_lock never, through
        ManagedConfiguration as Settings does, SpringBoard's SBDontLockEver
        when profiled refuses it) and read it back; it lives on the data
        volume and SpringBoard follows it at once. It is asked on every start
        (idempotent, a run may come from another base); a guest without the
        operation or one that refuses it keeps its own timeout, and the start
        goes on. The outcome is recorded under auto_lock either way."""
        if status.get("auto_lock_supported") is not True:
            self.save(auto_lock={"outcome": "skipped", "reason": "unsupported"})
            emit("auto-lock", outcome="skipped", reason="unsupported")
            return
        # Right after boot profiled starts on demand and the read-back spins
        # up to ten seconds, so a first request can outlast its 30 guest
        # seconds and time out after it applied, with no receipt to follow. A late reply is followed through its receipt; when that
        # gives no outcome, Never is simply asked once more (harmless).
        for attempt in (1, 2):
            # A worker still busy with an earlier request refuses before any work.
            deadline = time.monotonic() + 120
            while True:
                reply = self.invoke("ios.emulator.guest", {"job_id": job, "operation": "auto_lock",
                                                           "argument": "never", "timeout_seconds": 90}, 150)
                native = (reply.get("result") or {}).get("response") or {}
                if native.get("error") != "guest_operation_busy" or time.monotonic() >= deadline:
                    break
                time.sleep(5)
            if reply.get("ok"):
                break
            recovered = self.completed_receipt(job, reply, "auto_lock", time.monotonic() + 120)
            if recovered is not None or reply.get("error", {}).get("code") != "ios_guest_timeout":
                native = recovered or native
                break
        if native.get("ok") is not True or native.get("auto_lock") != "never":
            error = native.get("error") or reply.get("error", {}).get("code") or "unknown_outcome"
            self.save(auto_lock={"outcome": error})
            emit("auto-lock", outcome=error)
            return
        record = {"outcome": "applied", "method": native.get("method"),
                  "previous_max_inactivity": native.get("previous_max_inactivity"),
                  "max_inactivity": native.get("max_inactivity"),
                  "changed": (native.get("previous_max_inactivity") != native.get("max_inactivity")
                              if native.get("method") == "managed_configuration"
                              else native.get("previous_springboard_dont_lock_ever") is not True)}
        self.save(auto_lock=record)
        emit("auto-lock", **record)

    def apply_effects(self, job, status):
        """Turn the system's glass materials and motion into plain surfaces.

        The blurred materials of the Home screen, Control Centre, folders and
        sheets cost most of the renderer and the emulated processors: with
        Reduce Transparency and Reduce Motion on, the guest drew about 6 new
        pictures a second where it drew 1.6. Guest tools
        that report status.display_effects_supported set both through the
        system's own setters and read them back (`effects reduced`); profile
        `effects: "original"` keeps iOS's look and restores it. Asked on
        every start (idempotent); the outcome is recorded under effects and
        never fails the start."""
        wanted = self.profile.get("effects", "reduced")
        if wanted not in ("reduced", "original"):
            wanted = "reduced"
        if status.get("display_effects_supported") is not True:
            self.save(effects={"outcome": "skipped", "reason": "unsupported"})
            emit("effects", outcome="skipped", reason="unsupported")
            return
        # A worker still busy with an earlier request refuses before any work.
        # Reduce Motion's setter takes effect a moment later than its read-back
        # (effects_not_applied with reduce_motion false, then both true when
        # asked again), so that refusal is asked once more.
        deadline, again = time.monotonic() + 120, True
        while True:
            reply = self.invoke("ios.emulator.guest", {"job_id": job, "operation": "effects",
                                                       "argument": wanted, "timeout_seconds": 60}, 120)
            native = (reply.get("result") or {}).get("response") or {}
            late = native.get("error") == "effects_not_applied" and again
            if not (native.get("error") == "guest_operation_busy" or late) or time.monotonic() >= deadline:
                break
            again = again and not late
            time.sleep(2 if late else 5)
        if not reply.get("ok"):
            native = self.completed_receipt(job, reply, "effects", time.monotonic() + 90) or native
        if native.get("ok") is not True:
            error = native.get("error") or reply.get("error", {}).get("code") or "unknown_outcome"
            self.save(effects={"outcome": error, "wanted": wanted})
            emit("effects", outcome=error, wanted=wanted)
            return
        record = {"outcome": "applied", "wanted": wanted,
                  "reduce_transparency": native.get("reduce_transparency"),
                  "reduce_motion": native.get("reduce_motion"),
                  "changed": (native.get("previous_reduce_transparency") != native.get("reduce_transparency")
                              or native.get("previous_reduce_motion") != native.get("reduce_motion"))}
        self.save(effects=record)
        emit("effects", **record)

    def completed_receipt(self, job, reply, operation, deadline):
        """The outcome of a request whose reply did not arrive in time.

        Follows the guest's receipt for that very request until it completes
        or the deadline passes. None when no completed outcome is known: the
        request may still run or have run, and is not sent again."""
        details = reply.get("error", {}).get("details") or {}
        request = self.launch_request_id(details.get("request_id"))
        if details.get("request_may_have_executed") is not True or not request:
            return None
        while (remaining := deadline - time.monotonic()) > 0:
            recovered = self.invoke("ios.emulator.guest_result", {
                "job_id": job, "request_id": request,
                "timeout_seconds": max(1, min(30, int(remaining)))}, min(90, remaining))
            if recovered.get("error", {}).get("code") == "ios_guest_stopped":
                return None
            result = recovered.get("result") or {}
            if not recovered.get("ok") or result.get("state") == "running":
                time.sleep(min(5, max(0, deadline - time.monotonic())))
                continue
            response = result.get("response") or {}
            if (result.get("job_id") != job or result.get("request_id") != request
                    or result.get("state") != "completed" or response.get("operation") != operation
                    or response.get("request_id") != request):
                return None
            return response
        return None

    def unlock(self, job):
        """One attempt to leave the lock screen.

        The HID Home usage wakes the display on every device, and a second
        press shortly after leaves a passcode-free lock screen (the follow-up
        in launch). A device without a Home button is also given a slow swipe
        up from the bottom edge right after the wake: its display stays awake
        for only a few seconds of real time, and a swipe over a dark display
        does nothing. The swipe is slow on purpose; the guest lives through a
        fraction of real time and a quick one is over before iOS has seen a
        gesture. The side button cannot stand in for Home here: on engines
        without the GPIO wake it is acknowledged and changes nothing."""
        reply = self.invoke("ios.emulator.control", {"job_id": job, "action": "home"})
        accepted = (reply.get("ok") is True and
                    (reply.get("result", {}).get("result") or {}).get("accepted_by_emulator") is True)
        if self.unlock_with == "swipe":
            self.invoke("ios.emulator.control", {"job_id": job, "action": "drag", "x": 0.5, "y": 0.99,
                                                 "to_x": 0.5, "to_y": 0.25, "duration_ms": 1500})
        return accepted

    def wake_for_unlock(self, job, native_screen):
        """A native side pulse for each independently observed wake cycle.

        A pulse can lock an awake display too. Its acknowledgement therefore
        never establishes unlock; the caller must observe screen_state again.
        An interrupted pulse is retained and never automatically replayed.
        An accepted pulse can begin a new cycle only after distinct successful
        native replies observed the display awake and then asleep again.
        """
        previous = self.state.get("unlock_wake_receipt") or {}
        identity = self.launch_request_id(native_screen.get("request_id"))
        observed = (native_screen.get("ok") is True and
                    native_screen.get("operation") == "screen_state" and
                    native_screen.get("screen_state_known") is True and
                    native_screen.get("screen_awake_known") is True and
                    native_screen.get("screen_locked") is True and
                    native_screen.get("passcode_enabled") is False and identity is not None)
        if (native_screen.get("screen_awake_known") is True and
                native_screen.get("screen_awake") is True):
            if observed and previous.get("job_id") == job and previous.get("state") == "accepted":
                self.save(unlock_wake_receipt={**previous, "awake_request_id": identity})
            emit("unlock", screen_already_awake=True, side_button_submitted=False)
            return False
        renewed = False
        if previous.get("job_id") == job and previous.get("state") in ("submitting", "unknown", "accepted"):
            awake_identity = self.launch_request_id(previous.get("awake_request_id"))
            renewed = (previous.get("state") == "accepted" and observed and
                       native_screen.get("screen_awake") is False and
                       awake_identity is not None and awake_identity != identity)
            if not renewed:
                emit("unlock", side_button_already_submitted=True,
                     side_button_outcome_unknown=previous.get("state") != "accepted")
                return False
        status = self.invoke("ios.emulator.control", {"job_id": job, "action": "status"})
        native = status.get("result", {}).get("result") or {}
        if not status.get("ok") or native.get("power_button_supported") is not True:
            return False
        history = self.state.get("unlock_wake_history") or []
        if not isinstance(history, list):
            raise RuntimeError("The retained wake history is invalid; no Side input was submitted")
        cycle = 1
        if renewed:
            previous_cycle = previous.get("cycle", 1)
            if type(previous_cycle) is not int or previous_cycle < 1:
                raise RuntimeError("The retained wake cycle is invalid; no Side input was submitted")
            cycle = previous_cycle + 1
            history = [*history, {**previous, "next_sleep_request_id": identity}]
        receipt = {"job_id": job, "state": "submitting", "cycle": cycle}
        self.save(unlock_wake_receipt=receipt, unlock_wake_history=history)
        reply = self.invoke("ios.emulator.control", {"job_id": job, "action": "power_button"})
        result = reply.get("result", {}).get("result") or {}
        accepted = reply.get("ok") is True and result.get("accepted_by_emulator") is True
        # Even an error can follow a submitted QMP notification. Preserve it;
        # only another observed lock state may license ordinary Home input.
        self.save(unlock_wake_receipt={**receipt, "state": "accepted" if accepted else "unknown"})
        emit("unlock", side_button_accepted=accepted, screen_state_verified=False)
        return True

    def rearm_lock_screen(self, job):
        """Put an awake lock screen to sleep once Home stops unlocking it.

        After SpringBoard restarts (an icon cache clear, restart_ui) an awake,
        passcode-free lock screen can keep taking delivered Home presses
        without unlocking, even over thirty of them, and one side press, which
        sleeps the display, followed by Home unlocks it at once. The wake that follows is the ordinary path;
        this only ends the stale awake state, and its acknowledgement
        establishes nothing about the screen."""
        status = self.invoke("ios.emulator.control", {"job_id": job, "action": "status"})
        native = status.get("result", {}).get("result") or {}
        if not status.get("ok") or native.get("power_button_supported") is not True:
            return False
        reply = self.invoke("ios.emulator.control", {"job_id": job, "action": "power_button"})
        accepted = (reply.get("ok") is True and
                    (reply.get("result", {}).get("result") or {}).get("accepted_by_emulator") is True)
        emit("unlock", lock_screen_rearmed=accepted)
        return True

    def remember_launch(self, job, state, request_id=None, process_observation=None,
                        native_response=None, worker_idle_verified=False):
        receipt = {"job_id": job, "application": self.profile["application"],
                   "state": state, "request_id": request_id}
        if state in ("pending", "unknown") and native_response is not None:
            receipt["native_response"] = native_response
        if state == "accepted" and process_observation is not None:
            receipt["process_observation"] = process_observation
        if state == "accepted" and worker_idle_verified:
            receipt["native_worker_idle_verified"] = True
        self.save(launch_receipt=receipt)

    def uncertain_launch(self, job, request_id, message):
        previous = self.state.get("launch_receipt") or {}
        native = (previous.get("native_response") if previous.get("job_id") == job and
                  previous.get("request_id") == request_id and
                  previous.get("application") == self.profile["application"] else None)
        self.remember_launch(job, "unknown", request_id, native_response=native)
        self.save(phase="launch-uncertain", application_launch_accepted=None)
        raise RuntimeError(message + "; the app launch will not be replayed automatically" +
                           (" (request_id=" + request_id + ")" if request_id else ""))

    @staticmethod
    def launch_request_id(value):
        return (value if isinstance(value, str) and len(value) == 32 and
                all(char in "0123456789abcdef" for char in value) else None)

    @staticmethod
    def launch_response_matches(response, request_id, application):
        return (isinstance(response, dict) and response.get("request_id") == request_id and
                response.get("operation") == "open" and response.get("argument") == application)

    def screen_reply(self, job, deadline):
        return self.query_reply(job, "screen_state", deadline)

    def query_reply(self, job, operation, deadline):
        """Follow one read through its retained result and native worker exit.

        A reply can precede child teardown. Both immediate and late results
        must retain their identity until the service confirms the lane is idle.
        This owner accepts only argument-free observations, never mutations.
        """
        if operation not in ("inspect", "setup", "screen_state", "display_state",
                             "foreground_state", "graphics_state"):
            raise GuestObservationError("This query owner accepts only argument-free native observations")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GuestObservationError("The native query exceeded its deadline")
        reply = self.invoke("ios.emulator.guest", {"job_id": job, "operation": operation,
                                                   "timeout_seconds": max(1, min(30, int(remaining)))},
                            min(90, remaining))
        direct = None
        details = reply.get("error", {}).get("details") or {}
        if reply.get("ok"):
            result = reply.get("result") or {}
            # Older hosts do not provide correlated read receipts. Preserve
            # their response without asserting readiness they cannot establish.
            if "request_id" not in result:
                return reply
            request_id = self.launch_request_id(result.get("request_id"))
            native = result.get("response") or {}
            if (not request_id or result.get("job_id") != job or
                    native.get("operation") != operation or native.get("request_id") != request_id):
                raise GuestObservationError("The native query reply identity does not match")
            # These are parent refusals before a worker starts, so there is no
            # terminal worker receipt to follow and no operation to replay.
            if native.get("ok") is False and native.get("error") in (
                    "guest_operation_busy", "worker_start_failed"):
                return reply
            # The worker ended at its own guest-time bound before the native
            # call answered (the screen query is bound to 20 s while SpringBoard
            # is busy). A read that never completed has no
            # outcome to follow; the caller observes again later. The host
            # files such a receipt as unknown, so following it would only
            # end the start.
            if native.get("ok") is False and native.get("error") == "guest_worker_timeout":
                return reply
            direct = native
        else:
            request_id = self.launch_request_id(details.get("request_id"))
            if details.get("request_may_have_executed") is not True or not request_id:
                return reply
        pause = 0 if direct is not None else 5
        while time.monotonic() < deadline:
            emit("launch", accepted=False, waiting_for_screen_state=operation == "screen_state",
                 following_screen_request=operation == "screen_state",
                 waiting_for_native_query=True, query_operation=operation, request_id=request_id)
            time.sleep(min(pause, max(0, deadline - time.monotonic())))
            pause = 0.2 if direct is not None else 5
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            recovered = self.invoke("ios.emulator.guest_result", {
                "job_id": job, "request_id": request_id,
                "timeout_seconds": max(1, min(30, int(remaining)))}, min(90, remaining))
            if not recovered.get("ok"):
                if recovered.get("error", {}).get("code") == "ios_guest_stopped":
                    return recovered
                continue
            result = recovered.get("result") or {}
            if result.get("job_id") != job or result.get("request_id") != request_id:
                raise GuestObservationError("The native query receipt identity does not match")
            if result.get("state") == "running":
                continue
            if result.get("state") != "completed":
                # A read-only query with no retained outcome stays unknown.
                # Its caller checks worker idle before another observation.
                if direct is not None:
                    raise GuestObservationError("The native query replied but its completed receipt is unavailable")
                return reply
            if result.get("ready_for_next_request") is not True:
                continue
            native = result.get("response") or {}
            if native.get("operation") != operation or native.get("request_id") != request_id:
                raise GuestObservationError("The receipt is not the original native query")
            if direct is not None and native != direct:
                raise GuestObservationError("The completed native query receipt differs from its direct reply")
            return {"ok": True, "result": {"job_id": job, "request_id": request_id,
                                             "response": native, "ready_for_next_request": True}}
        raise GuestObservationError("The native query exceeded its deadline")

    @staticmethod
    def process_observation(response, application):
        """Retain optional native metadata without promoting PID or start
        snapshots to process lifetime, report attribution or a visible scene."""
        if (not isinstance(response, dict) or response.get("request_accepted") is not True or
                response.get("operation") != "open" or response.get("argument") != application or
                response.get("launch_process_pid_known") is not True or
                response.get("launch_process_pid_source") != "FrontBoard-completion-BSProcessHandle.pid" or
                response.get("launch_process_lifetime_bound") is not False or
                response.get("launch_process_snapshot_atomic_with_handle") is not False):
            return None
        pid = response.get("launch_process_pid")
        if type(pid) is not int or not 1 < pid <= 2147483647:
            return None
        observation = {"pid": pid, "source": response["launch_process_pid_source"],
                       "process_lifetime_bound": False, "snapshot_atomic_with_handle": False,
                       "start_snapshot_known": False}
        worker = response.get("worker_pid")
        if (response.get("pid_role") == "request-worker" and type(worker) is int and
                1 < worker <= 2147483647 and response.get("pid") == worker):
            observation["worker_pid"] = worker
        name = response.get("launch_process_handle_class")
        if isinstance(name, str) and 0 < len(name) <= 128:
            observation["handle_class"] = name
        snapshot = response.get("launch_process_start_snapshot")
        if response.get("launch_process_start_snapshot_known") is True and isinstance(snapshot, dict):
            seconds, micros = snapshot.get("start_seconds"), snapshot.get("start_microseconds")
            if (snapshot.get("source") == "PROC_PIDTBSDINFO" and
                    type(snapshot.get("pid")) is int and snapshot["pid"] == pid and
                    snapshot.get("single_native_record") is True and
                    type(seconds) is int and 0 < seconds <= 18446744073709551615 and
                    type(micros) is int and 0 <= micros < 1000000):
                observation["start_snapshot"] = {"source": snapshot["source"], "pid": pid,
                                                 "start_seconds": seconds, "start_microseconds": micros}
                name = snapshot.get("process_name")
                if isinstance(name, str) and 0 < len(name) <= 32:
                    observation["start_snapshot"]["process_name"] = name
                observation["start_snapshot_known"] = True
        return observation

    def launch(self, job, deadline):
        """Follow the original request after interruption; retry only a known
        pre-submission failure or the guest's explicit Locked/Busy refusal."""
        previous = self.state.get("launch_receipt") or {}
        same_request = (previous.get("job_id") == job and
                        previous.get("application") == self.profile["application"])
        pending = self.launch_request_id(previous.get("request_id")) if same_request else None
        if same_request and previous.get("state") in ("submitting", "unknown") and not pending:
            self.uncertain_launch(job, pending, "The previous launch outcome is unknown")
        if not same_request or previous.get("state") not in ("pending", "submitting", "unknown"):
            pending = None
        elif not pending:
            self.uncertain_launch(job, None, "The pending launch has no valid receipt identity")
        direct = previous.get("native_response") if pending else None
        if direct is not None and not self.launch_response_matches(direct, pending, self.profile["application"]):
            self.uncertain_launch(job, pending, "The retained direct launch reply identity does not match")
        receipt_pause = 0 if direct is not None else 15
        worker_idle_verified = False
        unlock_needed, retry_wait = True, False
        next_unlock_at = 0
        unlock_followup = False
        short_unlock_poll = False
        # Home cycles (a press and its follow-up) that left an awake lock
        # screen locked; see rearm_lock_screen.
        stale_home_cycles = 0
        while True:
            if time.monotonic() >= deadline:
                # Keep a pending identity for a later start to recover. No
                # timeout is evidence that its operation failed to execute.
                raise RuntimeError("iOS did not confirm the app launch before its deadline")
            if pending:
                # A direct reply can precede child teardown. Follow that same
                # receipt immediately; another open or Home is never a probe.
                time.sleep(min(receipt_pause, max(0, deadline - time.monotonic())))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    continue
                receipt_pause = 0.2 if direct is not None else 15
                reply = self.invoke("ios.emulator.guest_result", {"job_id": job, "request_id": pending,
                                       "timeout_seconds": max(1, min(30, int(remaining)))}, min(90, remaining))
                if reply.get("error", {}).get("code") == "ios_guest_stopped":
                    self.finalize()
                    raise RuntimeError("The guest stopped before the app launched")
                if not reply.get("ok"):
                    # Reading a receipt may itself time out; keep following
                    # the same identity, without another Home or open request.
                    emit("launch", accepted=False, following=True,
                         reason=reply.get("error", {}).get("code"))
                    continue
                result = reply.get("result") or {}
                if result.get("request_id") != pending or result.get("job_id") != job:
                    self.uncertain_launch(job, pending, "The launch receipt identity does not match")
                if result.get("state") == "running":
                    continue
                if result.get("state") != "completed":
                    self.uncertain_launch(job, pending, "The guest cannot establish the launch outcome")
                response = result.get("response") or {}
                if not self.launch_response_matches(response, pending, self.profile["application"]):
                    self.uncertain_launch(job, pending, "The receipt does not describe this app launch")
                if direct is not None and json.dumps(response, sort_keys=True) != json.dumps(direct, sort_keys=True):
                    self.uncertain_launch(job, pending, "The completed launch receipt differs from its direct reply")
                if result.get("ready_for_next_request") is not True:
                    continue
                worker_idle_verified = True
            else:
                if retry_wait:
                    # Home wakes the display first, with the backlight
                    # reaching state 2 only afterwards.
                    # A 15-second poll lets the lock screen sleep again.
                    # One short follow-up still requires a fresh successful
                    # native locked/no-passcode reply before ordinary input.
                    time.sleep(max(0, next_unlock_at - time.monotonic()) if short_unlock_poll else 15)
                    short_unlock_poll = False
                status = self.invoke("ios.emulator.guest", {"job_id": job, "operation": "status",
                                                            "timeout_seconds": 30}, 90)
                if status.get("error", {}).get("code") == "ios_guest_stopped":
                    self.finalize()
                    raise RuntimeError("The guest stopped before the app launched")
                worker = status.get("result", {}).get("response") or {}
                if not status.get("ok") or worker.get("ok") is not True or worker.get("worker_running") is not False:
                    retry_wait = True
                    continue
                if worker.get("screen_state_supported") is True:
                    screen = self.screen_reply(job, deadline)
                    native = screen.get("result", {}).get("response") or {}
                    if native.get("error") in ("virtual_screen_query_required", "screen_query_version_unavailable",
                                               "screen_query_contract_unqualified", "screen_query_framework_unavailable",
                                               "screen_query_api_unavailable", "screen_query_invalid_reply"):
                        raise RuntimeError("The native screen query is unavailable: " + native["error"])
                    if (not screen.get("ok") or native.get("ok") is not True or
                            native.get("screen_state_known") is not True or
                            type(native.get("screen_locked")) is not bool or
                            type(native.get("passcode_enabled")) is not bool):
                        emit("launch", accepted=False, waiting_for_screen_state=True)
                        retry_wait = True
                        continue
                    if native["screen_locked"]:
                        if native["passcode_enabled"]:
                            self.save(phase="awaiting-unlock", application_launch_accepted=False)
                            raise RuntimeError("Unlock the virtual iPhone with its passcode before opening the app")
                        # Home may only wake the display. Observe the native
                        # lock status again before submitting an app launch.
                        if time.monotonic() >= next_unlock_at:
                            if self.wake_for_unlock(job, native):
                                # Side can wake or lock. Its acknowledgement
                                # does not license Home against this earlier
                                # screen snapshot, even on an unknown reply.
                                # Recheck promptly so a woken display does not
                                # sleep again during the ordinary 15 s poll.
                                next_unlock_at = time.monotonic() + 2.5
                                short_unlock_poll = True
                                retry_wait = True
                                continue
                            if (stale_home_cycles >= 2 and native.get("screen_awake_known") is True and
                                    native.get("screen_awake") is True and self.rearm_lock_screen(job)):
                                # The display is going to sleep; the next poll
                                # takes the ordinary wake path.
                                stale_home_cycles = 0
                                next_unlock_at = time.monotonic() + 2.5
                                short_unlock_poll = True
                                retry_wait = True
                                continue
                            accepted = self.unlock(job)
                            if unlock_followup:
                                unlock_followup = False
                                stale_home_cycles += 1
                                next_unlock_at = time.monotonic() + 60
                            elif accepted:
                                unlock_followup = True
                                short_unlock_poll = True
                                next_unlock_at = time.monotonic() + 2.5
                            else:
                                next_unlock_at = time.monotonic() + 60
                        retry_wait = True
                        continue
                elif unlock_needed:
                    self.unlock(job)
                    time.sleep(60)
                if time.monotonic() >= deadline:
                    continue
                # Save intent before invoking the CLI. If this process dies
                # before it receives a request ID, adopting the running guest
                # cannot silently submit the mutation a second time.
                self.remember_launch(job, "submitting")
                try:
                    reply = self.invoke("ios.emulator.guest", {"job_id": job, "operation": "open",
                                                               "argument": self.profile["application"],
                                                               "timeout_seconds": 120}, 200)
                except BaseException:
                    # The durable submitting state already protects recovery
                    # even if this save is interrupted or fails.
                    try:
                        self.remember_launch(job, "unknown")
                    except OSError:
                        pass
                    raise
                code = reply.get("error", {}).get("code")
                details = reply.get("error", {}).get("details") or {}
                response = reply.get("result", {}).get("response") or {}
                if code == "ios_guest_stopped":
                    self.finalize()
                    raise RuntimeError("The guest stopped before the app launched")
                if details.get("request_may_have_executed") is True:
                    pending = self.launch_request_id(details.get("request_id"))
                    if not pending:
                        self.uncertain_launch(job, None, "The interrupted launch has no receipt identity")
                    self.remember_launch(job, "pending", pending)
                    continue
                if code in ("ios_guest_starting", "ios_guest_busy") or response.get("error") == "guest_operation_busy":
                    self.remember_launch(job, "not-submitted")
                    unlock_needed, retry_wait = False, True
                    continue
                if response.get("error") == "worker_start_failed":
                    self.remember_launch(job, "not-submitted")
                    raise RuntimeError("The native app-launch worker could not start")
                pending = self.launch_request_id(reply.get("result", {}).get("request_id"))
                if not reply.get("ok") or response.get("request_may_have_executed") is True:
                    self.uncertain_launch(job, pending, "The app launch has no definitive response")
                result = reply.get("result") or {}
                if "request_id" in result:
                    if (not pending or result.get("job_id") != job or
                            not self.launch_response_matches(response, pending, self.profile["application"])):
                        self.uncertain_launch(job, None, "The direct launch reply identity does not match")
                    direct = response
                    self.remember_launch(job, "pending", pending, native_response=direct)
                    receipt_pause = 0
                    continue
            opened = response.get("request_accepted") is True
            reason = response.get("native_error", {}).get("underlying", {}).get("code_description")
            emit("launch", accepted=opened, reason=reason or response.get("error"), following=False)
            if opened:
                self.remember_launch(job, "accepted", pending,
                                     self.process_observation(response, self.profile["application"]),
                                     worker_idle_verified=worker_idle_verified)
                return
            if response.get("request_accepted") is not False:
                self.uncertain_launch(job, pending, "The guest did not establish acceptance or refusal")
            self.remember_launch(job, "refused", pending)
            pending = None
            direct = None
            worker_idle_verified = False
            receipt_pause = 15
            if reason not in ("Locked", "Busy"):
                raise RuntimeError("iOS refused the launch: " + json.dumps(response)[:600])
            unlock_needed, retry_wait = reason == "Locked", True

    def stop(self):
        if self.state.get("closed") is True or not self.state.get("guest_job"):
            emit("stopped", run=self.state.get("run"))
            return
        job, forced = self.state["guest_job"], False
        # Until the guest service has answered once, iOS has not come up and
        # nobody could have changed anything in it.
        phase = self.state.get("phase")
        never_up = phase in ("preparing", "booting")
        if self.guest_running():
            self.save(phase="stopping", error=None)
            # A guest that has been up may only be slow: under load its
            # service can miss one deadline. It is asked several times before
            # it is taken for gone; ending it by force loses what it has not saved.
            attempts = 1 if never_up else 4
            for attempt in range(attempts):
                reply = self.invoke("ios.emulator.control", {"job_id": job, "action": "stop"}, 240)
                error = {} if reply.get("ok") else reply.get("error", {})
                code = error.get("code")
                if not error or code in WORKING + ENDED + ("ios_control_starting",) or attempt + 1 == attempts:
                    break
                emit("retry", reason=code)
                time.sleep(20)
            if code in WORKING + CONTENDED:
                # The guest is as it was; a guest that never came up stays one.
                self.save(phase=phase)
                raise RuntimeError("Stop refused: " + error.get("message", "the guest is busy"))
            # A job that is finishing by itself is waited for, not ended. Its
            # run was stopped the careful way (or has ended on its own) and is
            # only hashing its disks; cancelling it now would turn a verified
            # end into an unverified one. A guest that never came up has
            # nothing to save, so it is not waited for.
            finishing = code == "ios_guest_stopped" or (code == "ios_control_ended" and not never_up)
            if error and not finishing:
                # No answer from the guest service: iOS is still booting or
                # has hung. End the emulator without saving.
                emit("forced", reason=code)
                cancelled = self.invoke("job.cancel", {"job_id": job})
                if not cancelled.get("ok") and self.guest_running():
                    raise RuntimeError("The guest could not be stopped: "
                                       + cancelled.get("error", {}).get("message", ""))
                forced = True
            self.wait({"id": job, "state": "running"})
        if forced and never_up:
            self.abandon()
            return
        if not self.finalize():
            raise RuntimeError(("The guest did not answer and was ended without saving" if forced else
                                "The guest stopped without verified storage")
                               + "; the next start uses the last verified run")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("start", "stop", "deploy"))
    parser.add_argument("--bundle", type=lambda value: Path(value).absolute(), help="deploy: the .app directory")
    parser.add_argument("--entitlements", type=lambda value: Path(value).absolute(),
                        help="deploy: the entitlements to request for it")
    parser.add_argument("--cli", required=True, help="tomaccoctl executable")
    parser.add_argument("--profile", required=True, type=lambda value: Path(value).absolute())
    parser.add_argument("--tools", type=lambda value: Path(value).absolute(),
                        help="Guest-tools build to install instead of the current one")
    parser.add_argument("--no-app", action="store_true", help="Boot and wait for the guest service only")
    parser.add_argument("--model", help="Device model to describe the phone as (see ios.virtual.status); default: the profile's own")
    parser.add_argument("--unlock", choices=("home", "swipe"), default="home",
                        help="How this device leaves its lock screen")
    args = parser.parse_args()
    with (args.profile / "lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"state": "failed", "error": "Another virtual iPhone start or stop is in progress"}))
            return 1
        try:
            phone = Phone(args.cli, args.profile, args.model, args.unlock)
            if args.action == "start":
                phone.start(args.tools, not args.no_app)
            elif args.action == "deploy":
                phone.deploy(args.bundle, args.entitlements, not args.no_app)
            else:
                phone.stop()
            print(json.dumps({"state": "ok", "phase": phone.state.get("phase"), "run": phone.state.get("run"),
                              "guest_job": phone.state.get("guest_job"),
                              "display_visible": phone.state.get("display_visible")}))
            return 0
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
            try:
                # A stop has already recorded where the guest ended up
                # (running, stopped or unverified); only a start can fail as such.
                phone = Phone(args.cli, args.profile)
                # A start that gives up on a guest that is still booting (its
                # service does not answer) leaves that guest running: it stays
                # one that never came up, which a stop then forgets with its
                # disk copy instead of ending it as a guest that was lost.
                changes = {"phase": "failed"} if not isinstance(error, GuestObservationError) and args.action == "start" and phone.state.get("phase") not in (
                    "booting", "launch-uncertain", "awaiting-unlock") else {}
                phone.save(error=str(error), **changes)
            except (OSError, ValueError, KeyError):
                pass
            print(json.dumps({"state": "failed", "error": str(error)}))
            return 1


if __name__ == "__main__":
    sys.exit(main())
