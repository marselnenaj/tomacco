"""Verified inputs for the owned tools in a virtual iOS development guest."""
import hashlib
import json
import plistlib
import re

from build_ios_gpu_plugin import inspect_signed_code
from build_ios_guest_tools import APPLICATIONS_ROOT, EXPERIMENT_PROBE_LIBRARY, GUEST_IDENTIFIER, GUEST_ROOT, LOCAL_PLATFORM_POLICY, application_info, development_policy, development_policy_table, embedded_policy_table, extend_trustcache, launch_jobs, policy_entries, program_entitlements
from build_ios_research_bootstrap import verify_application_resources
from ios_application_bundle import BUNDLE_IDENTIFIER, BUNDLE_NAME, EXECUTABLE_NAME, EXTENSION_NAME, EXTENSION_POINT, FRAMEWORK_NAME, FRAMEWORKS, LIBRARY_NAME, MAX_FILE_BYTES, MAX_FILES, PLUGINS, application_entitlements, verify_bundle_resources
from ios_paravirt_firmware import read_component
from prepare_ios_paravirt import decode


def embedded_code_valid(app, code):
    """Whether one entry of an app record's nested_code names an embedded
    framework binary (Frameworks/<Name>.framework/<binary>, under a bundle
    identifier of its own), library (Frameworks/<name>.dylib, under its file
    name) or app extension executable (PlugIns/<Name>.appex/<executable>,
    under the app's identifier and one more component, with allowlisted
    entitlements of its own) of that app, with its signed hashes."""
    extension = isinstance(code, dict) and code.get("kind") == "extension"
    keys = {"path", "kind", "identifier", "cdhash", "sha256"} | ({"entitlements", "extension_point"} if extension else set())
    if not isinstance(code, dict) or set(code) != keys:
        return False
    path, kind, identifier = code["path"], code["kind"], code["identifier"]
    directory = PLUGINS if extension else FRAMEWORKS
    if (not isinstance(path, str) or not path.startswith(app["path"] + "/" + directory + "/")
            or not isinstance(identifier, str) or not isinstance(code["cdhash"], str)
            or not re.fullmatch(r"[0-9a-f]{40}", code["cdhash"]) or not isinstance(code["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", code["sha256"])):
        return False
    parts = path[len(app["path"]) + 1:].split("/")
    if extension:
        if (len(parts) != 3 or not EXTENSION_NAME.fullmatch(parts[1]) or not EXECUTABLE_NAME.fullmatch(parts[2])
                or not BUNDLE_IDENTIFIER.fullmatch(identifier) or not identifier.startswith(app["bundle_id"] + ".")
                or "." in identifier[len(app["bundle_id"]) + 1:] or not isinstance(code["extension_point"], str)
                or not EXTENSION_POINT.fullmatch(code["extension_point"])):
            return False
        try:
            # A record cannot grant an extension more than the allowlist.
            return application_entitlements(identifier, code["entitlements"]) == code["entitlements"]
        except ValueError:
            return False
    if kind == "framework":
        return (len(parts) == 3 and bool(FRAMEWORK_NAME.fullmatch(parts[1])) and bool(EXECUTABLE_NAME.fullmatch(parts[2]))
                and bool(BUNDLE_IDENTIFIER.fullmatch(identifier)) and identifier != app["bundle_id"]
                and identifier != application_info("27.0")["CFBundleIdentifier"])
    return (kind == "library" and len(parts) == 2 and bool(LIBRARY_NAME.fullmatch(parts[1]))
            and identifier == parts[1][:-len(".dylib")])


def staged_applications(record):
    """The developer apps a build record declares, keyed by executable path,
    after checking each declaration against the closed deployment contract.
    An app's embedded frameworks, libraries and app extensions are its
    nested_code (builds before them record none)."""
    applications = record.get("applications", [])
    if not isinstance(applications, list) or any(not isinstance(row, dict) for row in applications):
        raise ValueError("Invalid native guest application inventory")
    if applications and "development_signing" not in record:
        raise ValueError("Developer apps require a development-signing payload")
    staged = {}
    for app in applications:
        name, path, bundle_id, executable, cdhash = (app.get(key) for key in ("name", "path", "bundle_id", "executable", "cdhash"))
        if (not isinstance(name, str) or not BUNDLE_NAME.fullmatch(name) or name == "TomaccoProbe.app"
                or path != APPLICATIONS_ROOT + "/" + name
                or not isinstance(bundle_id, str) or not BUNDLE_IDENTIFIER.fullmatch(bundle_id)
                or bundle_id == application_info("27.0")["CFBundleIdentifier"]
                or not isinstance(executable, str) or not executable.startswith(path + "/")
                or "/" in executable[len(path) + 1:] or len(executable) == len(path) + 1
                or not isinstance(cdhash, str) or not re.fullmatch(r"[0-9a-f]{40}", cdhash)
                or app.get("guest_path") != GUEST_ROOT + "/" + path):
            raise ValueError("Invalid native guest application record")
        # A record cannot grant more than the allowlist; a refused key is named.
        if application_entitlements(bundle_id, app.get("entitlements")) != app.get("entitlements"):
            raise ValueError(f"Staged app {name} records entitlements outside the allowed set")
        if executable in staged or bundle_id in {other["bundle_id"] for other in staged.values()}:
            raise ValueError("Two staged apps share a bundle identifier or path")
        nested = app.get("nested_code", [])
        if (not isinstance(nested, list) or any(not embedded_code_valid(app, code) for code in nested)
                or len({code["path"] for code in nested}) != len(nested) or executable in {code["path"] for code in nested}):
            raise ValueError(f"Staged app {name} has an invalid embedded code record")
        staged[executable] = app
    return staged


def development_library(root, source_cache):
    """Pin mapping permission to the page-verified owned ARM64e adapter."""
    record, _files, _cache = payload(root, source_cache)
    if "development_signing" not in record:
        raise ValueError("Development mapping requires an opt-in development-signing payload")
    library = next(row for row in record["programs"]
                   if row["path"] == "usr/libexec/libtomacco-development-signing.dylib")
    if library.get("architecture") != "arm64e":
        raise ValueError("Development mapping requires the ARM64e installer adapter; rebuild guest tools")
    return {"cdhash": library["cdhash"], "sha256": library["sha256"],
            "bundle_id": record["development_signing"]["bundle_id"],
            "app_cdhash": record["development_signing"]["cdhash"]}


def virtual_research_mode(data, *, development_device=False):
    """Use TXM's original VM-only configuration, without changing monitor code.

    The vphone600 research monitor consumes a four-byte little-endian value
    from /chosen; it accepts ERM on a virtual device outside recovery. XNU
    publishes the actual monitor result to launchd through its commpage.
    """
    tree = decode(data)
    if tree.get("target-sub-type") != b"VPHONE600AP\0" or tree.child("defaults").get("vmm-present") != b"\1\0\0\0":
        raise ValueError("Guest tools require an explicitly virtual vphone600 device")
    chosen = tree.child("chosen")
    if chosen.get("osenvironment").rstrip(b"\0") not in (b"", b"normal"):
        raise ValueError("Guest tools require the normal virtual iOS boot environment")
    key = "sec-research-device-erm-enabled"
    if key in chosen.props and chosen.get(key) not in (b"\0" * 4, b"\1\0\0\0"):
        raise ValueError("Unsupported virtual research-mode property")
    chosen.set(key, b"\1\0\0\0")
    if development_device:
        # Original mobileactivationd recognizes a non-production, secure
        # development device from these virtual boot properties. It selects
        # its own local activation path; no daemon, response, or record is
        # patched. Refuse retail identities and inconsistent input profiles.
        for name in ("certificate-production-status", "effective-production-status-ap",
                     "effective-production-status-sep"):
            if chosen.get(name) != bytes(4):
                raise ValueError("Development-device setup requires a non-production virtual identity")
        modes = ("certificate-security-mode", "effective-security-mode-ap", "effective-security-mode-sep")
        values = {chosen.get(name) for name in modes}
        if len(values) != 1 or not values.issubset({bytes(4), b"\1\0\0\0"}):
            raise ValueError("Inconsistent virtual development-device security modes")
        for name in modes:
            chosen.set(name, b"\1\0\0\0")
    return tree.encode()


def payload(root, source_cache):
    """Revalidate signed bytes and a closed inventory before copying a payload."""
    record = json.loads(read_component(root, "guest-tools.json", 1 << 20))
    if not isinstance(record, dict) or (record.get("format"), record.get("scope"), record.get("state"), record.get("guest_root")) != (
            1, "ios-owned-guest-tools", "built", GUEST_ROOT):
        raise ValueError("Select a completed owned guest-tools build")
    target = record.get("target")
    if not isinstance(target, str) or not re.fullmatch(r"arm64-apple-ios[0-9]{1,2}\.[0-9]{1,2}(?:\.[0-9]{1,2})?", target):
        raise ValueError("Invalid native guest deployment target")
    programs = record.get("programs")
    development_signing = "development_signing" in record
    # Older builds have no such helper; a build that has it says so itself.
    quiet_services = record.get("quiet_services", False)
    if type(quiet_services) is not bool:
        raise ValueError("Invalid native guest service-quieting mode")
    # Builds before 382 publish no battery.
    virtual_battery = record.get("virtual_battery", False)
    if type(virtual_battery) is not bool:
        raise ValueError("Invalid native guest virtual battery mode")
    staged = staged_applications(record)
    # Each staged app's embedded frameworks, libraries and extensions: path -> (app, entry).
    nested = {code["path"]: (app, code) for app in staged.values() for code in app.get("nested_code", [])}
    # Scratch experiment builds (--probe-legacy-library) carry one extra signed
    # adapter for the setup helper's load self-test; ordinary builds never do.
    probe_library = record.get("experiment_probe_library", False)
    if type(probe_library) is not bool or (probe_library and not development_signing):
        raise ValueError("Invalid native guest experiment probe library")
    owned = (len(programs) - quiet_services - virtual_battery - len(staged) - len(nested) - probe_library
             if isinstance(programs, list) else None)
    if not isinstance(programs, list) or owned not in ((5,) if development_signing else (2, 3)) or any(not isinstance(row, dict) for row in programs):
        raise ValueError("Invalid native guest program inventory")
    expected = {"usr/libexec/tomacco-platform", "usr/libexec/tomacco-app-control"}
    application_probe = owned != 2
    if application_probe:
        expected.add("usr/libexec/TomaccoProbe.app/TomaccoProbe")
    if development_signing:
        expected.update(("usr/libexec/libtomacco-development-signing.dylib", "usr/libexec/tomacco-development-setup"))
    if quiet_services:
        expected.add("usr/libexec/tomacco-quiet")
    if virtual_battery:
        expected.add("usr/libexec/tomacco-battery")
    expected.update(staged)
    expected.update(nested)
    if probe_library:
        expected.add(EXPERIMENT_PROBE_LIBRARY)
    if {row.get("path") for row in programs} != expected:
        raise ValueError("Unexpected native guest programs")
    agent = record.get("agent", False)
    if type(agent) is not bool:
        raise ValueError("Invalid native guest agent mode")
    installer_connection_query = record.get("installer_connection_query", False)
    if type(installer_connection_query) is not bool or (installer_connection_query and not agent):
        raise ValueError("Invalid native installer connection capability")
    application_log_access = record.get("application_log_access", False)
    if type(application_log_access) is not bool or (application_log_access and not agent):
        raise ValueError("Invalid native application log capability")
    graphics_query_access = record.get("graphics_query_access", False)
    if type(graphics_query_access) is not bool or (graphics_query_access and not agent):
        raise ValueError("Invalid native graphics query capability")
    application_termination = record.get("application_termination", False)
    if type(application_termination) is not bool or (application_termination and not agent):
        raise ValueError("Invalid native application termination capability")
    time_zone_control = record.get("time_zone_control", False)
    if type(time_zone_control) is not bool or (time_zone_control and not agent):
        raise ValueError("Invalid native time zone capability")
    developer_mode_control = record.get("developer_mode_control", False)
    if type(developer_mode_control) is not bool or (developer_mode_control and not agent):
        raise ValueError("Guest build has an invalid Developer Mode grant")
    auto_lock_control = record.get("auto_lock_control", False)
    if type(auto_lock_control) is not bool or (auto_lock_control and not agent):
        raise ValueError("Invalid native auto-lock capability")
    # Builds before the policy table record one compiled-in application; such a
    # build is still read (it is the base a start upgrades from) but cannot
    # carry developer apps.
    legacy_policy = development_signing and "policy_table_sha256" not in record["development_signing"]
    if legacy_policy and staged:
        raise ValueError("A build without a policy table cannot carry developer apps")
    if development_signing and (not agent or record["development_signing"] != development_policy(programs, legacy=legacy_policy)):
        raise ValueError("Unexpected development-signing scope or application identity")
    files, hashes = {}, []
    bundle_files = None
    if application_probe:
        prefix = "usr/libexec/TomaccoProbe.app/"
        for name in ("Info.plist", "_CodeSignature/CodeResources"):
            files[prefix + name] = read_component(root, "Supplemental/" + prefix + name, 65536)
        if plistlib.loads(files[prefix + "Info.plist"]) != application_info(target.removeprefix("arm64-apple-ios")):
            raise ValueError("Unexpected native diagnostic bundle identity")
        verify_application_resources(files[prefix + "Info.plist"], files[prefix + "_CodeSignature/CodeResources"])
        bundle_files = {1: files[prefix + "Info.plist"], 3: files[prefix + "_CodeSignature/CodeResources"]}
    # Every file of a staged app, as the inventory names it: the bundle's
    # identity and its sealed resource manifest are checked against them.
    inventory = record.get("files")
    if not isinstance(inventory, list) or any(not isinstance(row, dict) for row in inventory):
        raise ValueError("Guest payload inventory is incomplete or contains extra files")
    application_seals = {}
    for executable, app in staged.items():
        prefix = app["path"] + "/"
        names = [row.get("path") for row in inventory if isinstance(row.get("path"), str) and row["path"].startswith(prefix)]
        if len(names) > MAX_FILES or len(set(names)) != len(names):
            raise ValueError(f"Staged app {app['name']} has an invalid file inventory")
        contents = {}
        for name in names:
            files[name] = contents[name[len(prefix):]] = read_component(root, "Supplemental/" + name, MAX_FILE_BYTES)
        if "Info.plist" not in contents or "_CodeSignature/CodeResources" not in contents or executable[len(prefix):] not in contents:
            raise ValueError(f"Staged app {app['name']} lacks its Info.plist, resource manifest or executable")
        try:
            info = plistlib.loads(contents["Info.plist"])
        except Exception as error:
            raise ValueError(f"Staged app {app['name']} has an unreadable Info.plist") from error
        if (not isinstance(info, dict) or info.get("CFBundleIdentifier") != app["bundle_id"]
                or info.get("CFBundleExecutable") != executable[len(prefix):]):
            raise ValueError(f"Staged app {app['name']} does not declare its recorded identity")
        resources = contents.pop("_CodeSignature/CodeResources")
        verify_bundle_resources(contents, resources, executable[len(prefix):])
        application_seals[executable] = {1: contents["Info.plist"], 3: resources}
        # An embedded framework or app extension is sealed twice: by the app's
        # manifest above (every file, its signed binary included) and by its own.
        for code in app.get("nested_code", []):
            if code["kind"] == "library":
                continue
            what = "Embedded framework" if code["kind"] == "framework" else "App extension"
            bundle, _, binary = code["path"][len(prefix):].rpartition("/")
            nested_bundle = {name[len(bundle) + 1:]: data for name, data in contents.items() if name.startswith(bundle + "/")}
            if ("Info.plist" not in nested_bundle or "_CodeSignature/CodeResources" not in nested_bundle
                    or binary not in nested_bundle):
                raise ValueError(f"{what} {code['path']} lacks its Info.plist, resource manifest or binary")
            try:
                info = plistlib.loads(nested_bundle["Info.plist"])
            except Exception as error:
                raise ValueError(f"{what} {code['path']} has an unreadable Info.plist") from error
            if (not isinstance(info, dict) or info.get("CFBundleIdentifier") != code["identifier"]
                    or info.get("CFBundleExecutable") != binary
                    or (code["kind"] == "extension" and (
                        info.get("CFBundlePackageType", "XPC!") != "XPC!" or not isinstance(info.get("NSExtension"), dict)
                        or info["NSExtension"].get("NSExtensionPointIdentifier") != code["extension_point"]))):
                raise ValueError(f"{what} {code['path']} does not declare its recorded identity")
            nested_resources = nested_bundle.pop("_CodeSignature/CodeResources")
            verify_bundle_resources(nested_bundle, nested_resources, binary)
            application_seals[code["path"]] = {1: nested_bundle["Info.plist"], 3: nested_resources}
    for row in programs:
        data = read_component(root, "Supplemental/" + row["path"], 16 << 20)
        app = staged.get(row["path"])
        owner, code = nested.get(row["path"], (None, None))
        if app is not None:
            if (row.get("application") != app["path"] or row.get("signing_identifier") != app["bundle_id"]
                    or row.get("cdhash") != app["cdhash"] or row.get("entitlements") != app["entitlements"]
                    or "nested_in" in row):
                raise ValueError("Staged app record differs from its program")
            entitlements, signing_identifier = app["entitlements"], app["bundle_id"]
        elif code is not None:
            # Embedded code is signed under its own identifier, an embedded
            # library or framework without entitlements, an extension with its own.
            entitlements = code.get("entitlements", {})
            if (row.get("nested_in") != owner["path"] or row.get("kind") != code["kind"] or "application" in row
                    or row.get("signing_identifier") != code["identifier"] or row.get("cdhash") != code["cdhash"]
                    or row.get("sha256") != code["sha256"] or row.get("entitlements") != entitlements):
                raise ValueError("Staged app record differs from its embedded code")
            signing_identifier = code["identifier"]
        else:
            if "application" in row or "nested_in" in row:
                raise ValueError("Unexpected native guest programs")
            entitlements = program_entitlements(row["path"], agent, development_signing,
                                                installer_connection_query=installer_connection_query,
                                                application_log_access=application_log_access,
                                                graphics_query_access=graphics_query_access,
                                                application_termination=application_termination,
                                                time_zone_control=time_zone_control,
                                                auto_lock_control=auto_lock_control,
                                                developer_mode_control=developer_mode_control)
            if row.get("entitlements", {}) != entitlements:
                raise ValueError("Unexpected native guest capability grant; rebuild guest tools")
            signing_identifier = row.get("signing_identifier")
            if (signing_identifier is not None and
                    (row["path"] != "usr/libexec/TomaccoProbe.app/TomaccoProbe" or
                     signing_identifier != application_info("27.0")["CFBundleIdentifier"])):
                raise ValueError("Unexpected native signing identifier")
            if development_signing and row["path"].endswith(".app/TomaccoProbe") and not signing_identifier:
                raise ValueError("Development signing requires a verified bundle identity")
        library = (row["path"] in ("usr/libexec/libtomacco-development-signing.dylib", EXPERIMENT_PROBE_LIBRARY)
                   or (code is not None and code["kind"] != "extension"))
        # The adapter runs in the ARM64e installd; the development-setup helper is
        # built ARM64e too so it can dlopen the adapter in its own process as a
        # load self-test. Every other owned tool stays ARM64.
        arm64e_allowed = (library and code is None) or row["path"] == "usr/libexec/tomacco-development-setup"
        architecture = row.get("architecture", "arm64")
        if architecture not in (("arm64", "arm64e") if arm64e_allowed else ("arm64",)):
            raise ValueError("Unexpected native guest program architecture")
        # Older ARM64 adapters remain readable for verified offline upgrades.
        # A manifest cannot relabel those bytes as an ARM64e replacement.
        integrity = inspect_signed_code(data, cpu_subtype=2 if architecture == "arm64e" else 0,
                                        file_type=6 if library else 2,
                                        expected_identifier=signing_identifier,
                                        expected_entitlements=entitlements,
                                        sealed_files=application_seals.get(row["path"]) if app is not None or code is not None
                                        else bundle_files if row["path"].endswith(".app/TomaccoProbe") else None)
        if (hashlib.sha256(data).hexdigest() != row.get("sha256") or integrity["cdhash"] != row.get("cdhash")
                or integrity != row.get("integrity")):
            raise ValueError("Guest program changed after its build")
        hashes.append(bytes.fromhex(integrity["cdhash"]))
        files[row["path"]] = data
    if development_signing and not legacy_policy:
        # The adapter admits what its compiled-in table names, not what the
        # record claims: the signed library's own bytes must hold that table.
        table = development_policy_table(policy_entries(programs))
        if embedded_policy_table(files["usr/libexec/libtomacco-development-signing.dylib"]) != table:
            raise ValueError("The signing library's compiled-in policy table differs from the recorded applications")
    jobs = record.get("jobs")
    if jobs != launch_jobs(application_probe, agent, development_signing, quiet_services, virtual_battery=virtual_battery):
        raise ValueError("Guest bootstrap permits only the owned bounded inventory/lifecycle jobs")
    for row in jobs:
        name = "System/Library/LaunchDaemons/" + row["Label"] + ".plist"
        files[name] = read_component(root, "Supplemental/" + name, 65536)
        if plistlib.loads(files[name]) != row:
            raise ValueError("Guest launchd job changed after its build")
    if len(inventory) != len(files) or {row.get("path") for row in inventory} != files.keys():
        raise ValueError("Guest payload inventory is incomplete or contains extra files")
    for row in inventory:
        data = files[row["path"]]
        expected_mode = 0o755 if row["path"] in expected else 0o644
        if (len(data), hashlib.sha256(data).hexdigest(), row.get("mode")) != (
                row.get("bytes"), row.get("sha256"), expected_mode):
            raise ValueError("Guest payload file integrity or mode changed")
    cache = extend_trustcache(source_cache, hashes)
    if (record.get("trustcache") != {"source_sha256": hashlib.sha256(source_cache).hexdigest(),
                                   "sha256": hashlib.sha256(cache).hexdigest(),
                                   "original_entries_preserved": True,
                                   "local_platform_policy": LOCAL_PLATFORM_POLICY.hex()} or
            read_component(root, "system.tc", 4 << 20) != cache):
        raise ValueError("Guest tools were built for a different trust cache")
    return record, files, cache
