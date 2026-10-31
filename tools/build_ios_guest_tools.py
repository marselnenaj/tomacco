#!/usr/bin/env python3
"""Build owned native tools for a private, virtual iOS development image.

The output contains no Apple binaries or SDK. It adds explicit local platform
trust for its own signed programs, retaining every input trust-cache entry.
Building this payload does not establish that launchd loaded it or that an
application scene became visible. Original firmware and sealed volumes are not
modified by this builder.
"""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import re
import stat
import struct
import subprocess
import tempfile

from build_ios_research_bootstrap import application_info, platform_header, sign_program, verify_application_resources
from build_ios_gpu_plugin import inspect_signed_code
from ios_application_bundle import BUNDLE_IDENTIFIER, MAX_FILE_BYTES, inspect_bundle, verify_bundle_resources
from ios_paravirt_graphics import cache_entries
from ios_paravirt_firmware import read_component

SOURCES = Path(__file__).resolve().parents[1] / "integrations/ios-emulator"
GUEST_ROOT = "/System/Cryptexes/Supplemental"
# The prefix of the owned launchd jobs and of the files they and the control
# service write in the guest (guest-app-control.c, guest-setup.h). Every start
# replaces the whole Supplemental payload.
GUEST_IDENTIFIER = "io.github.marselnenaj.tomacco"
# Developer app bundles are staged here; a start installs them from this path.
APPLICATIONS_ROOT = "Applications"
RESEARCH_APPLICATION = "usr/libexec/TomaccoProbe.app/TomaccoProbe"
# Scratch experiment builds only (--probe-legacy-library): a second signed adapter.
EXPERIMENT_PROBE_LIBRARY = "usr/libexec/libtomacco-legacy-probe.dylib"
# The development-signing policy table (guest-development-protocol.h): header
# magic and version, one 280-byte entry per admitted application.
POLICY_TABLE_MAGIC = b"PSPT\x01"
POLICY_HEADER_BYTES, POLICY_ENTRY_BYTES, POLICY_MAX_ENTRIES = 8, 280, 32
# SHA-256, platform flag, category 7 (trust-cache validation), reserved.
# Category 2 is for authenticated system volumes and rejects our executable on
# unsealed Preboot. This grant covers only these owned, page-verified programs;
# never copy a system-volume constraint onto a development payload.
LOCAL_PLATFORM_POLICY = bytes.fromhex("02400700")
# Clang major versions whose development-signing builds activate in installd.
# clang 22.1.8 produces working payloads bit for bit; a clang 23.1.3 build boots
# with the library never reporting itself active, so owned installs hang in the
# installer.
QUALIFIED_DEVELOPMENT_COMPILERS = (22,)


def guest_directory_modes(root):
    """Set the owned guest payload's traversable directories, keeping host privacy."""
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise ValueError("Guest payload root must be an ordinary directory")
    directories = []
    for path in (root, *sorted(root.rglob("*"))):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            directories.append(path)
        elif not stat.S_ISREG(mode):
            raise ValueError("Guest payload contains a symlink or special file")
    for path in directories:
        path.chmod(0o755)


def program_entitlements(path, agent, development_signing=False, *, installer_connection_query=False,
                         application_log_access=False, graphics_query_access=False,
                         application_termination=False, time_zone_control=False,
                         auto_lock_control=False, developer_mode_control=False):
    # These capabilities belong only to the local VM control service, never to
    # the app under development. MobileInstallation still validates the app.
    if agent and path == "usr/libexec/tomacco-app-control":
        operations = ["InstallForInstallCoordination"]
        if installer_connection_query:
            operations.append("GetSystemAppMigrationStatus")
        grants = {"com.apple.private.mobileinstall.allowedSPI": operations,
                  "com.apple.springboard.launchapplications": True}
        # Reading the owned app's event log needs access to its data container.
        if application_log_access:
            grants["com.apple.private.security.storage.AppDataContainers"] = True
        # installd leaves a running copy of the app it replaces alone; the
        # install ends that app's processes first, by bundle identifier.
        if application_termination:
            grants["com.apple.runningboard.terminateprocess"] = True
        # The time_zone operation sets the system zone through libutil's
        # tzlink(), whose daemon (com.apple.tzlink) answers EPERM to a client
        # without this entitlement. A separate recorded grant, like the two
        # above: payloads built before it still install.
        if time_zone_control:
            grants["com.apple.tzlink.allow"] = True
        # The auto_lock operation stores Settings' Auto-Lock through
        # ManagedConfiguration, which hands it to profiled over its private
        # service; that service's clients carry this entitlement. Recorded
        # like the zone grant: payloads built before it still install.
        if auto_lock_control:
            grants["com.apple.managedconfiguration.profiled-access"] = True
        # The developer_mode operation arms iOS's Developer Mode through amfid,
        # whose service com.apple.amfi.xpc takes clients with this entitlement.
        # debugserver is refused without that mode. Recorded like the grants above.
        if developer_mode_control:
            grants["com.apple.private.amfi.developer-mode-control"] = True
        # The original kernel refused this exact user-client class in the
        # owned graphics_state worker. Keep the opt-in grant on this program.
        if graphics_query_access:
            grants["com.apple.security.iokit-user-client-class"] = ["AppleParavirtDeviceUserClient"]
        return grants
    if development_signing and path == "usr/libexec/tomacco-development-setup":
        return {"com.apple.private.xpc.service-attach": True}
    if development_signing and path == "usr/libexec/TomaccoProbe.app/TomaccoProbe":
        return {"application-identifier": application_info("27.0")["CFBundleIdentifier"]}
    return {}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def development_policy_table(entries):
    """Encode the admitted applications as the adapter's policy table.

    entries: (bundle identifier, 20-byte cdhash) pairs. The encoding is
    canonical: entries ascending by identifier bytes, so one set has one
    table and the verifier can compare bytes."""
    rows = []
    for entry in entries:
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise ValueError("Policy entries are (bundle identifier, cdhash) pairs")
        identity, cdhash = entry
        if not isinstance(identity, str) or not BUNDLE_IDENTIFIER.fullmatch(identity):
            raise ValueError(f"Invalid policy bundle identifier {identity!r}")
        if not isinstance(cdhash, bytes) or len(cdhash) != 20:
            raise ValueError("A policy entry needs a 20-byte CodeDirectory hash")
        rows.append((identity.encode("ascii"), cdhash))
    if not rows or len(rows) > POLICY_MAX_ENTRIES:
        raise ValueError(f"A policy table holds 1 to {POLICY_MAX_ENTRIES} entries: the owned diagnostic, each "
                         "staged app and each framework and app extension embedded in one")
    if len({identity for identity, _ in rows}) != len(rows):
        raise ValueError("Two policy entries name the same bundle identifier")
    if len({cdhash for _, cdhash in rows}) != len(rows):
        raise ValueError("Two policy entries name the same CodeDirectory hash")
    rows.sort()
    body = b"".join(cdhash + bytes(4) + identity.ljust(256, b"\0") for identity, cdhash in rows)
    return POLICY_TABLE_MAGIC + bytes([len(rows)]) + bytes(2) + body


def decode_development_policy_table(data):
    """The (bundle identifier, cdhash) pairs of a table, refusing any encoding
    the adapter's decoder (tomacco_policy_count) would refuse."""
    if not isinstance(data, bytes) or len(data) < POLICY_HEADER_BYTES or data[:5] != POLICY_TABLE_MAGIC or data[6:8] != bytes(2):
        raise ValueError("Not a version-1 development-signing policy table")
    count = data[5]
    if not 1 <= count <= POLICY_MAX_ENTRIES or len(data) != POLICY_HEADER_BYTES + count * POLICY_ENTRY_BYTES:
        raise ValueError("Development-signing policy table has an invalid entry count or length")
    entries = []
    for index in range(count):
        offset = POLICY_HEADER_BYTES + index * POLICY_ENTRY_BYTES
        cdhash, flags, field = data[offset:offset + 20], data[offset + 20:offset + 24], data[offset + 24:offset + 280]
        end = field.find(b"\0")
        if flags != bytes(4) or end < 1 or field[end:] != bytes(256 - end):
            raise ValueError("Development-signing policy entry is malformed")
        identity = field[:end].decode("ascii", "replace")
        if not BUNDLE_IDENTIFIER.fullmatch(identity) or (entries and entries[-1][0].encode() >= field[:end]):
            raise ValueError("Development-signing policy entries must be valid identifiers in ascending order")
        entries.append((identity, cdhash))
    if len({cdhash for _, cdhash in entries}) != count:
        raise ValueError("Two policy entries name the same CodeDirectory hash")
    return entries


def embedded_policy_table(library):
    """The one policy table compiled into the signing library's bytes."""
    tables, position = [], library.find(POLICY_TABLE_MAGIC)
    while position >= 0:
        if position + POLICY_HEADER_BYTES <= len(library):
            end = position + POLICY_HEADER_BYTES + library[position + 5] * POLICY_ENTRY_BYTES
            candidate = library[position:end]
            try:
                decode_development_policy_table(candidate)
                tables.append(candidate)
            except ValueError:
                pass
        position = library.find(POLICY_TABLE_MAGIC, position + 1)
    if len(tables) != 1:
        raise ValueError("The signing library carries no single valid policy table")
    return tables[0]


# Nested code installd verifies as a bundle of its own (an MIExecutableBundle
# with its own MICodeSigningVerifier): each needs a policy-table entry.
ADMITTED_NESTED_KINDS = ("framework", "extension")


def policy_entries(programs):
    """(bundle identifier, cdhash) of every bundle the payload admits: the
    owned diagnostic, each staged developer app and each framework and app
    extension embedded in one. installd verifies an embedded framework and an
    extension (MIPluginKitBundle) as bundles of their own (MIExecutableBundle),
    so its verifier asks the adapter about their identifiers; the adapter
    compares the cdhash libmis reports, which covers an extension's
    entitlements too. A library in Frameworks/ is no bundle and is covered by
    its app's sealed manifest and its trust-cache entry."""
    entries = []
    for row in programs:
        if row.get("path") == RESEARCH_APPLICATION:
            identity = application_info("27.0")["CFBundleIdentifier"]
        elif "application" in row or ("nested_in" in row and row.get("kind") in ADMITTED_NESTED_KINDS):
            identity = row.get("signing_identifier")
        else:
            continue
        cdhash = row.get("cdhash")
        if not isinstance(identity, str) or not isinstance(cdhash, str) or not re.fullmatch(r"[0-9a-f]{40}", cdhash):
            raise ValueError("Missing or invalid development-signing application identity")
        entries.append((identity, bytes.fromhex(cdhash)))
    return entries


def policy_headers(entries):
    """The generated C headers: the table for the adapter, and for both the
    adapter and the setup helper the digest their activation receipt carries."""
    table = development_policy_table(entries)
    policy_digest = hashlib.sha256(table).digest()[:20]
    policy = (f"#define TOMACCO_POLICY_ENTRIES {len(entries)}u\n"
              "static const unsigned char tomacco_policy_digest[20] = {" + ",".join(map(str, policy_digest)) + "};\n")
    table_header = (f"static const unsigned char tomacco_policy_table[{len(table)}] = {{"
                    + ",".join(map(str, table)) + "};\n")
    return policy, table_header, table


def development_policy(programs, *, legacy=False):
    """The development-signing record of a payload, derived from its programs.

    legacy: the record of a build before the policy table (one compiled-in
    application); such a build cannot carry staged developer apps."""
    if not isinstance(programs, list) or any(not isinstance(row, dict) for row in programs):
        raise ValueError("Invalid development-signing program inventory")
    def identity(path, field, length):
        matches = [row for row in programs if row.get("path") == path]
        value = matches[0].get(field) if len(matches) == 1 else None
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{%d}" % length, value):
            raise ValueError("Missing or invalid development-signing " + field)
        return value
    app_hash = identity(RESEARCH_APPLICATION, "cdhash", 40)
    library_hash = identity("usr/libexec/libtomacco-development-signing.dylib", "sha256", 64)
    policy = {"scope": "owned-cdhash", "bundle_id": application_info("27.0")["CFBundleIdentifier"],
              "cdhash": app_hash, "resources_required": True,
              "service": "com.apple.mobile.installd", "service_configuration": "next-invocation",
              "library_sha256": library_hash, "guest_execution_verified": False}
    applications = [row for row in programs if "application" in row]
    if legacy:
        if applications:
            raise ValueError("A build without a policy table cannot carry developer apps")
        return policy
    table = development_policy_table(policy_entries(programs))
    policy.update(policy_table_sha256=digest(table), policy_entries=len(decode_development_policy_table(table)),
                  applications=[{"bundle_id": row["signing_identifier"], "cdhash": row["cdhash"],
                                 "path": row["application"]} for row in applications])
    for kind, key in (("framework", "frameworks"), ("extension", "extensions")):
        nested = [row for row in programs if "nested_in" in row and row.get("kind") == kind]
        if nested:
            # Builds before embedded frameworks (or app extensions) have no such key.
            policy[key] = [{"bundle_id": row["signing_identifier"], "cdhash": row["cdhash"],
                            "path": row["path"], "application": row["nested_in"]} for row in nested]
    return policy


def stage_application(bundle, entitlements, payload, signer, taken):
    """Copy a developer's app bundle into the payload, sign it ad hoc with its
    validated entitlements and verify the sealed result. Returns the program
    rows (its executable, then its embedded frameworks, libraries and app
    extensions) and the application record.

    Signing goes inside out: each embedded framework (under its bundle
    identifier, with its own Info.plist and CodeResources seals) and library
    (under its file name) first, without entitlements, then each app
    extension (under its bundle identifier, with its own seals and its own
    validated entitlements), then the app with ldid's shallow directory pass,
    which seals those signed bytes as resources instead of signing them again
    under the app's identity."""
    inspected = inspect_bundle(bundle, entitlements)
    name, bundle_id = inspected["name"], inspected["bundle_id"]
    if bundle_id == application_info("27.0")["CFBundleIdentifier"] or name == "TomaccoProbe.app":
        raise ValueError(f"{name} uses the owned diagnostic app's identity ({bundle_id}); choose another bundle identifier")
    if bundle_id in taken:
        raise ValueError(f"Two app bundles use the bundle identifier {bundle_id}: {taken[bundle_id]} and {name}")
    if name in taken.values():
        raise ValueError(f"Two app bundles are named {name}; rename one of them")
    # Frameworks and libraries first, extensions after them (they may link
    # the app's frameworks), each in path order.
    nested = sorted(inspected["nested_code"], key=lambda code: (code["kind"] == "extension", code["path"]))
    # A framework or extension is admitted by the signing policy table like
    # an app, and the table names each identifier once.
    for code in nested:
        if code["kind"] in ADMITTED_NESTED_KINDS and (code["identifier"] in taken or code["identifier"] in (
                bundle_id, application_info("27.0")["CFBundleIdentifier"])):
            raise ValueError(f"{name}/{code['bundle']} uses the bundle identifier {code['identifier']}, which "
                             f"{taken.get(code['identifier'], 'the owned diagnostic')} already uses; the signing "
                             f"policy table admits each identifier once (stage one of them, or give the {code['kind']} "
                             "an identifier of its own)")
    taken[bundle_id] = name
    for code in nested:
        if code["kind"] in ADMITTED_NESTED_KINDS:
            taken[code["identifier"]] = f"{name}/{code['bundle']}"
    code_paths = {inspected["executable"], *(code["path"] for code in nested)}
    destination = payload / APPLICATIONS_ROOT / name
    destination.mkdir(parents=True)
    for row in inspected["files"]:
        data = read_component(Path(bundle), row["path"], MAX_FILE_BYTES)
        if digest(data) != row["sha256"]:
            raise ValueError(f"{name}/{row['path']} changed while the bundle was staged")
        target = destination / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if row["path"] in code_paths else 0o644)
    relative = APPLICATIONS_ROOT + "/" + name
    nested_rows, nested_records = [], []
    for code in nested:
        target = destination / code["path"]
        sealed = None
        # Only an extension carries entitlements; embedded libraries run with their app's.
        granted = code.get("entitlements", {})
        if code["kind"] in ADMITTED_NESTED_KINDS:
            # A bundle of its own: signed under its identifier with its own
            # Info.plist and CodeResources seals (shallow: it holds no other code).
            nested_bundle = destination / code["bundle"]
            cdhash = sign_program(signer, target, granted, bundle=nested_bundle, shallow=True)
            resources = nested_bundle / "_CodeSignature/CodeResources"
            resources.chmod(0o644)
            contents = {str(path.relative_to(nested_bundle)): path.read_bytes()
                        for path in sorted(nested_bundle.rglob("*")) if path.is_file() and path != resources}
            verify_bundle_resources(contents, resources.read_bytes(), target.name)
            sealed = {1: contents["Info.plist"], 3: resources.read_bytes()}
        else:
            cdhash = sign_program(signer, target, {}, identifier=code["identifier"])
        data = target.read_bytes()
        integrity = inspect_signed_code(data, cpu_subtype=0, file_type=2 if code["kind"] == "extension" else 6,
                                        expected_identifier=code["identifier"], expected_entitlements=granted,
                                        sealed_files=sealed)
        if integrity["cdhash"] != cdhash.hex():
            raise ValueError("Native program signature differs from the signer result")
        target.chmod(0o755)
        nested_rows.append({"path": relative + "/" + code["path"], "cdhash": cdhash.hex(), "architecture": "arm64",
                            "sha256": digest(data), "entitlements": granted, "integrity": integrity,
                            "local_platform_trust_grant": True, "apple_authenticated": False,
                            "signing_identifier": code["identifier"], "nested_in": relative, "kind": code["kind"]})
        nested_records.append({"path": relative + "/" + code["path"], "kind": code["kind"],
                               "identifier": code["identifier"], "cdhash": cdhash.hex(), "sha256": digest(data),
                               **({"entitlements": granted, "extension_point": code["extension_point"]}
                                  if code["kind"] == "extension" else {})})
    executable = destination / inspected["executable"]
    cdhash = sign_program(signer, executable, inspected["entitlements"], bundle=destination, shallow=True)
    resources = destination / "_CodeSignature/CodeResources"
    resources.chmod(0o644)
    files = {str(path.relative_to(destination)): path.read_bytes() for path in sorted(destination.rglob("*"))
             if path.is_file() and path != resources}
    verify_bundle_resources(files, resources.read_bytes(), inspected["executable"])
    for row in nested_rows:
        if digest(files[row["path"][len(relative) + 1:]]) != row["sha256"]:
            raise ValueError(f"Signing {name} changed its embedded code {row['path'][len(relative) + 1:]}")
    integrity = inspect_signed_code(files[inspected["executable"]], cpu_subtype=0, file_type=2,
                                    expected_identifier=bundle_id, expected_entitlements=inspected["entitlements"],
                                    sealed_files={1: files["Info.plist"], 3: resources.read_bytes()})
    if integrity["cdhash"] != cdhash.hex():
        raise ValueError("Native program signature differs from the signer result")
    row = {"path": relative + "/" + inspected["executable"], "cdhash": cdhash.hex(), "architecture": "arm64",
           "sha256": digest(files[inspected["executable"]]), "entitlements": inspected["entitlements"],
           "integrity": integrity, "local_platform_trust_grant": True, "apple_authenticated": False,
           "signing_identifier": bundle_id, "application": relative}
    record = {"path": relative, "name": name, "bundle_id": bundle_id, "executable": row["path"],
              "cdhash": cdhash.hex(), "entitlements": inspected["entitlements"],
              "display_name": inspected["display_name"], "version": inspected["version"],
              "minimum_os": inspected["minimum_os"], "guest_path": GUEST_ROOT + "/" + relative,
              "nested_code": nested_records,
              "source": {"path": inspected["path"], "content_sha256": inspected["content_sha256"],
                         "bytes": inspected["bytes"], "files": len(inspected["files"]),
                         "entitlements_source": inspected["entitlements_source"]}}
    return [row, *nested_rows], record


def extend_trustcache(original, hashes):
    entries = cache_entries(original)
    if not hashes or any(len(value) != 20 for value in hashes) or len(set(hashes)) != len(hashes):
        raise ValueError("Expected distinct SHA-256 CodeDirectory hashes")
    for value in hashes:
        if value in entries:
            raise ValueError("Guest program is already present in the input trust cache")
        entries[value] = value + LOCAL_PLATFORM_POLICY
    body = b"".join(sorted(entries.values()))
    return struct.pack("<I16sI", 2, hashlib.sha256(body).digest()[:16], len(entries)) + body


def job(label, arguments, user):
    log = "/private/var/mobile/Library/Logs/" + label
    return {"Label": label, "ProgramArguments": arguments,
            "RunAtLoad": True, "LaunchOnlyOnce": True, "UserName": user,
            "ProcessType": "Background", "StandardOutPath": log + ".jsonl",
            "StandardErrorPath": log + ".stderr.log"}


def launch_jobs(application_probe=False, agent=False, development_signing=False, quiet_services=False,
                virtual_battery=False):
    """The owned launchd jobs, labelled with the project's identifier; the owned
    diagnostic's job is labelled with its bundle identifier."""
    jobs = [job(GUEST_IDENTIFIER + ".platform-inventory",
                [GUEST_ROOT + "/usr/libexec/tomacco-platform", "--stdio"], "mobile"),
            job(GUEST_IDENTIFIER + ".application-inventory",
                [GUEST_ROOT + "/usr/libexec/tomacco-app-control", "serve" if agent else "inspect"], "mobile")]
    if agent:
        jobs[1].update(POSIXSpawnType="App", ProcessType="Interactive")
        # The control service is how the host reaches the phone at all, so
        # launchd brings it back after any exit, for the whole boot.
        del jobs[1]["LaunchOnlyOnce"]
        jobs[1]["KeepAlive"] = True
    if application_probe and not agent:
        # Agent mode includes the bundle for explicit install/open. Starting
        # a second, unregistered copy through launchd obscures app lifecycle.
        application = job(application_info("27.0")["CFBundleIdentifier"],
                          [GUEST_ROOT + "/usr/libexec/TomaccoProbe.app/TomaccoProbe", "--stdio"], "mobile")
        application.update(POSIXSpawnType="App", ProcessType="Interactive", EnvironmentVariables={
            "HOME": "/private/var/mobile", "USER": "mobile", "LOGNAME": "mobile",
            "TMPDIR": "/private/var/mobile/tmp", "OS_ACTIVITY_DT_MODE": "YES", "NSUnbufferedIO": "YES"})
        jobs.append(application)
    if development_signing:
        setup = job(GUEST_IDENTIFIER + ".development-signing",
                    [GUEST_ROOT + "/usr/libexec/tomacco-development-setup"], "root")
        # Installation waits for this bounded startup dependency. Scheduling
        # it as background work lets the boot's foreground services starve it.
        setup["ProcessType"] = "Interactive"
        jobs.append(setup)
    if quiet_services:
        # Unloads, for this boot only, the original services that can only
        # fail on the emulated processor (see guest-quiet.c). It has to get
        # its turn early: the services it stops are what keeps the boot busy.
        quiet = job(GUEST_IDENTIFIER + ".quiet", [GUEST_ROOT + "/usr/libexec/tomacco-quiet"], "root")
        quiet["ProcessType"] = "Interactive"
        jobs.append(quiet)
    if virtual_battery:
        # Publishes the phone's battery to powerd (see guest-battery.c): root,
        # because only root may post the com.apple.system. notifications that
        # make BatteryCenter read it. powerd drops the battery when its
        # publisher exits, so launchd keeps the helper for the whole boot.
        battery = job(GUEST_IDENTIFIER + ".battery", [GUEST_ROOT + "/usr/libexec/tomacco-battery"], "root")
        del battery["LaunchOnlyOnce"]
        battery.update(KeepAlive=True, ProcessType="Interactive")
        jobs.append(battery)
    return jobs


def compiler_identity(clang):
    """The compiler's own version line, recorded with the build it compiled."""
    result = subprocess.run([str(clang), "--version"], capture_output=True, text=True, timeout=60, check=True)
    line = (result.stdout.splitlines() or [""])[0].strip()
    match = re.search(r"clang version ([0-9]+)\.([0-9]+)\.([0-9]+)", line)
    if not match:
        raise ValueError("Select a clang compiler; its --version names no clang release")
    return {"version": ".".join(match.groups()), "major": int(match.group(1)), "banner": line[:200]}


def build(args):
    development_signing = getattr(args, "development_signing", False)
    quiet_services = getattr(args, "quiet_services", False)
    virtual_battery = getattr(args, "virtual_battery", False)
    graphics_query_access = getattr(args, "graphics_query_access", False)
    if graphics_query_access and not args.agent:
        raise ValueError("Graphics query access requires the owned agent")
    if development_signing and not (args.agent and args.application_probe):
        raise ValueError("Development signing requires agent and application_probe; policy covers only the built application")
    substitute_library = getattr(args, "substitute_library", None)
    if substitute_library and not development_signing:
        raise ValueError("--substitute-library is a development-signing experiment switch")
    if getattr(args, "probe_legacy_library", None) and not development_signing:
        raise ValueError("--probe-legacy-library is a development-signing experiment switch")
    if getattr(args, "substitute_setup", None) and not development_signing:
        raise ValueError("--substitute-setup is a development-signing experiment switch")
    if substitute_library and (getattr(args, "applications", None) or []):
        raise ValueError("--substitute-library is for a legacy single-entry adapter; stage no developer apps with it")
    # Developer app bundles, each with an optional entitlements plist of its own.
    bundles = [Path(value).expanduser().absolute() for value in getattr(args, "applications", None) or []]
    entitlement_files = [Path(value).expanduser().absolute() if value else None
                         for value in getattr(args, "application_entitlements", None) or []]
    if len(entitlement_files) > len(bundles):
        raise ValueError("More entitlements files than app bundles were given")
    entitlement_files += [None] * (len(bundles) - len(entitlement_files))
    if bundles and not development_signing:
        raise ValueError("Deploying app bundles requires development signing: installd admits them only through its policy table")
    if len(bundles) + 1 > POLICY_MAX_ENTRIES:
        raise ValueError(f"At most {POLICY_MAX_ENTRIES - 1} app bundles can be staged beside the owned diagnostic")
    if not re.fullmatch(r"[0-9]{1,2}\.[0-9]{1,2}(?:\.[0-9]{1,2})?", args.minimum_ios):
        raise ValueError("Invalid minimum iOS version")
    compiler = compiler_identity(args.clang)
    compiler["development_signing_qualified"] = compiler["major"] in QUALIFIED_DEVELOPMENT_COMPILERS
    if development_signing and not compiler["development_signing_qualified"] and not getattr(
            args, "allow_unqualified_compiler", False):
        raise ValueError(f"Development signing is qualified only with clang "
                         f"{', '.join(map(str, QUALIFIED_DEVELOPMENT_COMPILERS))}; a clang {compiler['version']} "
                         "build's installd library never activated. Pass --clang to a qualified compiler")
    output = args.output.expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; choose a new directory")
    original = read_component(args.trustcache.parent, args.trustcache.name, 4 << 20)
    cache_entries(original)
    linker, signer = [str(value.expanduser().resolve(strict=True)) for value in (args.linker, args.ldid)]
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".tomacco-guest-tools-", dir=output.parent) as temporary:
        stage = Path(temporary)
        payload = stage / "Supplemental"
        programs = payload / "usr/libexec"
        programs.mkdir(parents=True)
        daemons = payload / "System/Library/LaunchDaemons"
        daemons.mkdir(parents=True)
        (stage / "research-platform-contract.h").write_text(platform_header())
        target = "arm64-apple-ios" + args.minimum_ios
        # guest-development-protocol.h is staged for every build: guest-app-control.c
        # includes it to report installd's development-signing activation state in
        # its status (its policy/receipt helpers stay unused in non-signing builds).
        source_names = ["research-platform.c", "guest-app-control.c", "guest-objc-contract.h", "guest-setup.h", "guest-screen-state.h", "guest-display-state.h", "guest-foreground-state.h", "guest-graphics-state.h", "guest-process-info.h", "guest-launch-process.h", "guest-pixel-format-state.h", "guest-metal-capabilities.h", "guest-surface-state.h", "guest-crash-reports.h", "guest-persistence-probe.h", "guest-icon-cache.h", "guest-time-zone.h", "guest-auto-lock.h", "guest-host-channel.h", "guest-buffer-log.h", "guest-surface-log.h", "guest-development-protocol.h", "research-libSystem.tbd"]
        definitions = [("tomacco-platform", ["research-platform.c"]),
                       ("tomacco-app-control", ["guest-app-control.c"])]
        if args.application_probe:
            definitions.append(("TomaccoProbe.app/TomaccoProbe", ["research-application.c", "research-application-trace.c"]))
            source_names += definitions[-1][1]
        if development_signing:
            definitions += [("libtomacco-development-signing.dylib", ["guest-development-signing.c"]),
                            ("tomacco-development-setup", ["guest-development-setup.c"])]
            source_names += ["guest-development-signing.c", "guest-development-setup.c",
                             "guest-service-state.h"]
        if quiet_services:
            definitions.append(("tomacco-quiet", ["guest-quiet.c"]))
            source_names.append("guest-quiet.c")
        if virtual_battery:
            definitions.append(("tomacco-battery", ["guest-battery.c"]))
            source_names.append("guest-battery.c")
        # Compile and hash the same captured bytes. An editor may change the
        # workspace while this durable build runs; later reads of that working
        # tree must not be reported as the sources of an earlier executable.
        source_root = stage / "sources"
        source_root.mkdir()
        source_hashes = {}
        for name in source_names:
            data = read_component(SOURCES, name, 1 << 20)
            (source_root / name).write_bytes(data)
            source_hashes[name] = digest(data)
        records, application_records, taken = [], [], {}
        # Scratch experiment (--probe-legacy-library): a second, prebuilt signed
        # adapter the setup helper load-tests beside this build's one. Ordinary
        # builds get an empty header and an unchanged helper.
        probe_legacy = getattr(args, "probe_legacy_library", None)
        cache_dir = "/private/var/installd/Library/Caches/Tomacco/"
        (stage / "guest-development-probe.h").write_text(
            "/* No experiment load probe in this build. */\n" if not probe_legacy else
            "#define TOMACCO_PROBE_LEGACY_SOURCE " + json.dumps(GUEST_ROOT + "/" + EXPERIMENT_PROBE_LIBRARY) + "\n" +
            "#define TOMACCO_PROBE_TABLE_COPY " + json.dumps(cache_dir + "probe-table.dylib") + "\n" +
            "#define TOMACCO_PROBE_LEGACY_COPY " + json.dumps(cache_dir + "probe-legacy.dylib") + "\n")
        for name, sources in definitions:
            is_library = name == "libtomacco-development-signing.dylib"
            if name == "tomacco-development-setup" and getattr(args, "substitute_setup", None):
                # Scratch experiment only: use a prebuilt, already-signed setup
                # helper verbatim (e.g. 367's) instead of compiling this build's,
                # to tell the helper binary apart from the rest of the plumbing.
                substitute = Path(args.substitute_setup).expanduser().resolve(strict=True)
                destination = programs / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(read_component(substitute.parent, substitute.name, 4 << 20))
                destination.chmod(0o755)
                data = destination.read_bytes()
                entitlements = program_entitlements("usr/libexec/" + name, args.agent, development_signing,
                                                    installer_connection_query=args.agent,
                                                    application_log_access=args.agent,
                                                    graphics_query_access=graphics_query_access,
                                                    application_termination=args.agent,
                                                    time_zone_control=args.agent,
                                                    auto_lock_control=args.agent,
                                                    developer_mode_control=args.agent)
                integrity = inspect_signed_code(data, cpu_subtype=0, file_type=2,
                                                expected_identifier=None, expected_entitlements=entitlements)
                records.append({"path": str(destination.relative_to(payload)), "cdhash": integrity["cdhash"],
                                "architecture": "arm64", "sha256": digest(data), "entitlements": entitlements,
                                "integrity": integrity, "local_platform_trust_grant": True,
                                "apple_authenticated": False})
                continue
            if is_library and getattr(args, "substitute_library", None):
                # Scratch experiment only (--substitute-library): use a prebuilt,
                # already-signed adapter verbatim instead of compiling this build's
                # one, to tell library CONTENT from insertion PLUMBING. No compile
                # and no re-sign: the substitute's own CodeDirectory hash is trusted
                # and the helper is pointed at its exact bytes. Legacy (single
                # compiled-in entry) libraries carry no policy table; the policy
                # record is recorded legacy and no developer apps may be staged.
                substitute = Path(args.substitute_library).expanduser().resolve(strict=True)
                # The setup helper and (unused here) adapter sources still include
                # the generated policy headers, so emit them from the owned
                # diagnostic's single entry; the substituted legacy adapter ignores
                # them and answers with its own (older) ack, judged unconfirmed.
                policy, table_header, _table = policy_headers(policy_entries(records))
                (stage / "guest-development-policy.h").write_text(policy)
                (stage / "guest-development-policy-table.h").write_text(table_header)
                destination = programs / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(read_component(substitute.parent, substitute.name, 4 << 20))
                destination.chmod(0o755)
                data = destination.read_bytes()
                integrity = inspect_signed_code(data, cpu_subtype=2, file_type=6,
                                                expected_identifier=None, expected_entitlements={})
                sha = digest(data)
                records.append({"path": str(destination.relative_to(payload)), "cdhash": integrity["cdhash"],
                                "architecture": "arm64e", "sha256": sha, "entitlements": {},
                                "integrity": integrity, "local_platform_trust_grant": True,
                                "apple_authenticated": False})
                copy_name = sha + (("-" + args.copy_name_salt) if getattr(args, "copy_name_salt", None) else "") + ".dylib"
                (stage / "guest-development-library.h").write_text(
                    "#define TOMACCO_LIBRARY_SOURCE " + json.dumps(GUEST_ROOT + "/usr/libexec/" + name) + "\n" +
                    "#define TOMACCO_LIBRARY_NAME " + json.dumps(copy_name) + "\n" +
                    "#define TOMACCO_LIBRARY_DESTINATION " + json.dumps("/private/var/installd/Library/Caches/Tomacco/" + copy_name) + "\n" +
                    "#define TOMACCO_LIBRARY_BYTES " + str(len(data)) + "ul\n" +
                    "static const unsigned char tomacco_library_sha256[] = {" + ",".join(map(str, bytes.fromhex(sha))) + "};\n")
                continue
            if is_library:
                # The adapter's policy table names the owned diagnostic (signed
                # above) and every developer app: stage and sign those first,
                # then generate the headers the adapter and helper compile in.
                for bundle_path, entitlement_file in zip(bundles, entitlement_files):
                    rows, application = stage_application(bundle_path, entitlement_file, payload, signer, taken)
                    for row in rows[1:]:
                        # Two apps may not embed one byte-identical library:
                        # the trust cache names each cdhash once.
                        twin = next((other for other in records if other["cdhash"] == row["cdhash"]), None)
                        if twin:
                            raise ValueError(f"{row['path']} is the same signed code as {twin['path']}; "
                                             "stage one of the apps that embed it")
                    records.extend(rows)
                    application_records.append(application)
                policy, table_header, _table = policy_headers(policy_entries(records))
                (stage / "guest-development-policy.h").write_text(policy)
                (stage / "guest-development-policy-table.h").write_text(table_header)
            # The adapter is loaded into the original ARM64e installd process,
            # whose pointer-authentication ABI differs from our ARM64 tools; every
            # owned tool (the setup helper included) stays ARM64.
            # The probe helper is ARM64e so its dlopen of the ARM64e adapters is
            # arch-compatible (experiment builds only).
            arm64e = is_library or (bool(probe_legacy) and name == "tomacco-development-setup")
            architecture = "arm64e" if arm64e else "arm64"
            program_target = architecture + "-apple-ios" + args.minimum_ios
            destination = programs / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            bundle = destination.parent if name == "TomaccoProbe.app/TomaccoProbe" else None
            if bundle:
                (bundle / "Info.plist").write_bytes(plistlib.dumps(application_info(args.minimum_ios)))
            objects = []
            for source in sources:
                obj = stage / (source + ".o")
                subprocess.run([args.clang, "--target=" + program_target, "-O2", "-Wall", "-Wextra", "-Werror",
                                "-DTOMACCO_APPLICATION_FILE_LOG=1",
                                "-fblocks", "-I" + str(stage), "-c", str(source_root / source), "-o", str(obj)],
                               check=True, timeout=120)
                objects.append(str(obj))
            subprocess.run([linker, "-arch", architecture, "-platform_version", "ios", args.minimum_ios,
                            args.minimum_ios, "-dead_strip", "-o", str(destination),
                            *(["-dylib", "-install_name", GUEST_ROOT + "/usr/libexec/" + name] if is_library else []),
                            str(source_root / "research-libSystem.tbd"), *objects], check=True, timeout=120)
            entitlements = program_entitlements("usr/libexec/" + name, args.agent, development_signing,
                                                installer_connection_query=args.agent,
                                                application_log_access=args.agent,
                                                graphics_query_access=graphics_query_access,
                                                application_termination=args.agent,
                                                time_zone_control=args.agent,
                                                auto_lock_control=args.agent,
                                                developer_mode_control=args.agent)
            cdhash = sign_program(signer, destination, entitlements, bundle=bundle)
            if bundle:
                verify_application_resources((bundle / "Info.plist").read_bytes(),
                                             (bundle / "_CodeSignature/CodeResources").read_bytes())
                # Payload modes belong to the guest package contract. A
                # restrictive host umask must not change its signed resources.
                (bundle / "Info.plist").chmod(0o644)
                (bundle / "_CodeSignature/CodeResources").chmod(0o644)
            integrity = inspect_signed_code(destination.read_bytes(), cpu_subtype=2 if arm64e else 0, file_type=6 if is_library else 2,
                                            expected_identifier=application_info(args.minimum_ios)["CFBundleIdentifier"] if bundle else None,
                                            expected_entitlements=entitlements, sealed_files={
                                                1: (bundle / "Info.plist").read_bytes(),
                                                3: (bundle / "_CodeSignature/CodeResources").read_bytes()
                                            } if bundle else None)
            if integrity["cdhash"] != cdhash.hex():
                raise ValueError("Native program signature differs from the signer result")
            destination.chmod(0o755)
            records.append({"path": str(destination.relative_to(payload)), "cdhash": cdhash.hex(),
                            "architecture": architecture,
                            "sha256": digest(destination.read_bytes()), "entitlements": entitlements,
                            "integrity": integrity,
                            "local_platform_trust_grant": True, "apple_authenticated": False})
            if bundle:
                records[-1]["signing_identifier"] = application_info(args.minimum_ios)["CFBundleIdentifier"]
            if is_library:
                data = destination.read_bytes()
                sha = digest(data)
                # --copy-name-salt (experiment only) gives the installd-cache copy a
                # name no earlier boot has written, so the helper creates it fresh.
                name = sha + (("-" + args.copy_name_salt) if getattr(args, "copy_name_salt", None) else "") + ".dylib"
                # installd's sandbox maps the adapter from installd's own cache but
                # not from the Cryptex volume, so the helper copies it there (NAME)
                # and inserts it from DESTINATION; SOURCE is where it reads it from.
                (stage / "guest-development-library.h").write_text(
                    "#define TOMACCO_LIBRARY_SOURCE " + json.dumps(GUEST_ROOT + "/usr/libexec/" + destination.name) + "\n" +
                    "#define TOMACCO_LIBRARY_NAME " + json.dumps(name) + "\n" +
                    "#define TOMACCO_LIBRARY_DESTINATION " + json.dumps("/private/var/installd/Library/Caches/Tomacco/" + name) + "\n" +
                    "#define TOMACCO_LIBRARY_BYTES " + str(len(data)) + "ul\n" +
                    "static const unsigned char tomacco_library_sha256[] = {" + ",".join(map(str, bytes.fromhex(sha))) + "};\n")
        if probe_legacy:
            source = Path(probe_legacy).expanduser().resolve(strict=True)
            destination = payload / EXPERIMENT_PROBE_LIBRARY
            destination.write_bytes(read_component(source.parent, source.name, 4 << 20))
            destination.chmod(0o755)
            data = destination.read_bytes()
            integrity = inspect_signed_code(data, cpu_subtype=2, file_type=6, expected_identifier=None,
                                            expected_entitlements={})
            records.append({"path": EXPERIMENT_PROBE_LIBRARY, "cdhash": integrity["cdhash"], "architecture": "arm64e",
                            "sha256": digest(data), "entitlements": {}, "integrity": integrity,
                            "local_platform_trust_grant": True, "apple_authenticated": False})
        jobs = launch_jobs(args.application_probe, args.agent, development_signing, quiet_services,
                           virtual_battery=virtual_battery)
        for entry in jobs:
            destination = daemons / (entry["Label"] + ".plist")
            destination.write_bytes(plistlib.dumps(entry))
            destination.chmod(0o644)
        # cp -a and the helper's newc archive preserve directory metadata too.
        # Mobile's launchd domain must traverse every guest parent even when
        # the private host build runs under umask 077.
        guest_directory_modes(payload)
        combined = extend_trustcache(original, [bytes.fromhex(row["cdhash"]) for row in records])
        (stage / "system.tc").write_bytes(combined)
        inventory = []
        for path in sorted(payload.rglob("*")):
            if path.is_file():
                inventory.append({"path": str(path.relative_to(payload)), "sha256": digest(path.read_bytes()),
                                  "bytes": path.stat().st_size, "mode": path.stat().st_mode & 0o777})
        report = {"format": 1, "scope": "ios-owned-guest-tools", "state": "built", "target": target,
                  "guest_root": GUEST_ROOT, "programs": records, "files": inventory,
                  "sources": source_hashes,
                  "trustcache": {"source_sha256": digest(original), "sha256": digest(combined),
                                 "original_entries_preserved": True, "local_platform_policy": LOCAL_PLATFORM_POLICY.hex()},
                  "jobs": jobs, "agent": args.agent, "installer_connection_query": args.agent,
                  "application_log_access": args.agent,
                  "application_termination": args.agent,
                  "time_zone_control": args.agent,
                  "auto_lock_control": args.agent,
                  "developer_mode_control": args.agent,
                  "graphics_query_access": graphics_query_access,
                  "quiet_services": quiet_services, "virtual_battery": virtual_battery, "compiler": compiler,
                  # What a later rebuild of this set (ios_virtual_iphone.py) compiles with.
                  "toolchain": {"clang": str(args.clang), "linker": linker, "ldid": signer},
                  "applications": application_records,
                  "apple_sdk_used": False, "macos_used": False,
                  "guest_execution_verified": False, "application_scene_verified": False}
        if probe_legacy:
            report["experiment_probe_library"] = True
        if development_signing:
            # A substituted adapter is a legacy single-entry library with no
            # policy table, so its record is the legacy shape.
            report["development_signing"] = development_policy(records, legacy=bool(substitute_library))
        (stage / "guest-tools.json").write_text(json.dumps(report, indent=2) + "\n")
        stage.rename(output)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trustcache", type=Path, required=True)
    parser.add_argument("--linker", type=Path, required=True,
                        help="Mach-O linker; --development-signing requires ARM64e authenticated-pointer support (see build_ios_linker.py)")
    parser.add_argument("--ldid", type=Path, required=True)
    parser.add_argument("--clang", default="clang")
    parser.add_argument("--allow-unqualified-compiler", action="store_true",
                        help="Build development signing with a clang release it was never seen working with; the report records the compiler as unqualified")
    parser.add_argument("--minimum-ios", default="27.0")
    parser.add_argument("--application-probe", action="store_true",
                        help="Include the bounded, real UIKit lifecycle diagnostic (not a simulator demo)")
    parser.add_argument("--agent", action="store_true",
                        help="Run the local host-only VSOCK control service instead of one-shot app inventory")
    parser.add_argument("--graphics-query-access", action="store_true",
                        help="Opt in to AppleParavirtDeviceUserClient access for the owned agent's graphics queries; requires --agent")
    parser.add_argument("--development-signing", action="store_true",
                        help="Configure only installd's next invocation to validate the exact owned diagnostic app with ad-hoc signing; requires --agent --application-probe")
    parser.add_argument("--quiet-services", action="store_true",
                        help="Include a root helper that unloads, for each boot, the original services that can only fail on the emulated processor (four daemons in an assertion loop, the watchdog and speech-extension respawn loops)")
    parser.add_argument("--virtual-battery", action=argparse.BooleanOptionalAction, default=True,
                        help="Include a resident root helper that publishes a charged internal battery on AC power to powerd, so the status bar shows a full battery and no low-battery alert or Low Power Mode offer comes (default: on)")
    parser.add_argument("--substitute-library", metavar="SIGNED_DYLIB",
                        help="Experiment only: insert this prebuilt, already-signed adapter verbatim instead of compiling this build's one (its CodeDirectory hash is trusted, the helper points at its bytes). For telling library content from insertion plumbing; requires --development-signing and no --application")
    parser.add_argument("--probe-legacy-library", metavar="SIGNED_DYLIB",
                        help="Experiment only: stage this prebuilt signed adapter beside the build's one (trusted by its own cdhash) and build an ARM64e setup helper that load-tests both from their Cryptex paths and from fresh installd-cache copies, logging every dlerror in full")
    parser.add_argument("--copy-name-salt", metavar="SALT",
                        help="Experiment only: append -SALT to the adapter copy's name in installd's cache, so the setup helper writes a fresh copy instead of reusing one an earlier boot left")
    parser.add_argument("--substitute-setup", metavar="SIGNED_SETUP",
                        help="Experiment only: use this prebuilt, already-signed development-setup helper verbatim instead of compiling this build's one (its CodeDirectory hash is trusted). For telling the helper binary apart from the rest of the plumbing; requires --development-signing")
    parser.add_argument("--application", dest="applications", action="append", default=[], metavar="BUNDLE",
                        help="Stage a developer's .app directory built for arm64 iOS: signed ad hoc with its allowlisted entitlements, trusted by cdhash and admitted by the signing policy table; its embedded frameworks and libraries (Frameworks/) are signed first, without entitlements, then its app extensions (PlugIns/*.appex) with their own allowlisted entitlements (embedded in each extension's executable), and trusted and admitted the same way; requires --development-signing. Repeatable")
    parser.add_argument("--application-entitlements", dest="application_entitlements", action="append", default=[],
                        metavar="PLIST", help="Entitlements plist for the --application given at the same position (an empty value keeps the executable's embedded ones)")
    print(json.dumps(build(parser.parse_args()), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
