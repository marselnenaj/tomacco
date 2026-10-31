#!/usr/bin/env python3
"""Inspect a developer's iOS app bundle before it is staged for the virtual iPhone.

A bundle is an `.app` directory built for arm64 iOS (SwiftPM/xtool, or any
other toolchain). This reads it without following links, names every reason
it cannot be deployed, and records what the guest-tools build and a later
start compare: the bundle's identity, its executable, its entitlement set and
a digest of its content. No Apple SDK or signature is required of the input;
the guest-tools build signs the staged copy ad hoc itself.

Embedded frameworks and libraries (Frameworks/<Name>.framework and
Frameworks/<name>.dylib, arm64 Mach-O dylibs) and app extensions
(PlugIns/<Name>.appex, each an arm64 executable under an identifier of its
own) are the bundle's only other code. The build signs each of them ad hoc
before the main executable, so the app's sealed manifest covers their signed
bytes, and trusts each by its own cdhash. ExtensionKit extensions
(Extensions/), XPC services and nested apps stay refused.
"""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import re
import stat
import struct
import sys

# The capabilities an app may ask for. A trust-cache entry makes the staged
# executable a platform binary whose entitlements are taken as signed, so the
# set is closed: nothing outside it reaches the guest, in particular nothing
# that would make the app a platform application or a debugger of others.
ALLOWED_ENTITLEMENTS = {
    "application-identifier": str,
    "com.apple.developer.team-identifier": str,
    "get-task-allow": bool,
    "keychain-access-groups": list,
    "com.apple.security.application-groups": list,
}
REFUSED_EXAMPLES = ("platform-application", "task_for_pid-allow", "com.apple.private.*", "com.apple.security.*")
BUNDLE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\.app")
# The same identifier vocabulary the signer accepts (build_ios_research_bootstrap.sign_program).
BUNDLE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,254}")
TEAM_IDENTIFIER = re.compile(r"[A-Z0-9]{10}")
ACCESS_GROUP = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
EXECUTABLE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
FRAMEWORK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\.framework")
EXTENSION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\.appex")
# NSExtensionPointIdentifier, NSExtensionPrincipalClass and storyboard names.
EXTENSION_POINT = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,254}")
EXTENSION_CLASS = re.compile(r"[A-Za-z_$][A-Za-z0-9_.$]{0,254}")
LIBRARY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}\.dylib")
# Bundles are carried by the guest-tools payload and by the installer helper's
# initial RAM disk (a 1 GiB helper machine), so their size is bounded.
MAX_FILE_BYTES = 64 << 20
MAX_BUNDLE_BYTES = 128 << 20
MAX_FILES = 4096
# The payload verifier reads one signed program of at most this size
# (inspect_signed_code); a larger executable or library cannot be staged.
MAX_CODE_BYTES = 16 << 20
# Where embedded frameworks and libraries live. Each is signed and trusted on
# its own (build_ios_guest_tools.stage_application).
FRAMEWORKS = "Frameworks"
# Where app extensions live: PlugIns/<Name>.appex, each signed with its own
# entitlements and admitted by its own identity.
PLUGINS = "PlugIns"
# Directories whose contents would be code of another kind: ExtensionKit
# extensions, XPC services, helpers and watch or clip apps are bundles with
# executables, identities and entitlements of their own, which installd and
# their hosts validate and launch separately; not supported.
NESTED_CODE = ("SharedFrameworks", "Plug-ins", "XPCServices", "Helpers",
               "Extensions", "Watch", "AppClips")
# Extension points whose installd catalog entry prohibits NSExtensionPrincipalClass
# and NSExtensionMainStoryboard (MIPluginKitBundle refuses an appex that
# defines either: "AppexBundleContainsClassOrStoryboard"). A WidgetKit
# extension's entry point is its widget bundle.
PRINCIPAL_CLASS_PROHIBITED = ("com.apple.widgetkit-extension",)
NESTED_BUNDLES = {".app": "a nested application", ".appex": "an app extension", ".xpc": "an XPC service"}
MACH_O_MAGICS = {0xfeedfacf: "64-bit Mach-O", 0xfeedface: "32-bit Mach-O", 0xcafebabe: "universal (fat) Mach-O",
                 0xcffaedfe: "64-bit Mach-O (byte-swapped)", 0xcefaedfe: "32-bit Mach-O (byte-swapped)",
                 0xbebafeca: "universal (fat) Mach-O (byte-swapped)"}
CPU_ARM64, MH_EXECUTE, MH_DYLIB, LC_CODE_SIGNATURE = 0x100000c, 2, 6, 0x1d
CPU_NAMES = {0x100000c: "arm64", 0x1000007: "x86_64", 0xc: "arm", 0x7: "i386"}
FILE_TYPES = {MH_EXECUTE: ("an", "executable"), MH_DYLIB: ("a", "dynamic library")}


def application_entitlements(bundle_id, requested):
    """The validated entitlement set of an app, from what it asked for.

    Every key is checked against the closed allowlist and its value against the
    type and vocabulary that key takes; a refused key is named so the developer
    can remove it. `application-identifier` defaults to the bundle identifier
    and may otherwise only be it, or it behind a ten-character team prefix."""
    if not isinstance(bundle_id, str) or not BUNDLE_IDENTIFIER.fullmatch(bundle_id):
        raise ValueError(f"Invalid bundle identifier {bundle_id!r}")
    if requested is None:
        requested = {}
    if not isinstance(requested, dict):
        raise ValueError("Entitlements must be a dictionary")
    granted = {}
    for key in sorted(requested, key=str):
        if not isinstance(key, str):
            raise ValueError("Entitlement keys must be strings")
        kind = ALLOWED_ENTITLEMENTS.get(key)
        if kind is None:
            raise ValueError(f"Entitlement {key!r} is not allowed on the virtual iPhone; allowed: "
                             + ", ".join(sorted(ALLOWED_ENTITLEMENTS)) + "; refused in particular: "
                             + ", ".join(REFUSED_EXAMPLES))
        value = requested[key]
        if type(value) is not kind:
            raise ValueError(f"Entitlement {key!r} must be a {kind.__name__}, not {type(value).__name__}")
        if kind is list:
            if not value or len(value) > 64 or any(not isinstance(item, str) or not ACCESS_GROUP.fullmatch(item)
                                                    for item in value):
                raise ValueError(f"Entitlement {key!r} must list 1 to 64 group names")
            if len(set(value)) != len(value):
                raise ValueError(f"Entitlement {key!r} lists a group twice")
            value = list(value)
        if key == "com.apple.developer.team-identifier" and not TEAM_IDENTIFIER.fullmatch(value):
            raise ValueError("Entitlement 'com.apple.developer.team-identifier' must be ten upper-case letters or digits")
        granted[key] = value
    team = granted.get("com.apple.developer.team-identifier")
    identity = granted.setdefault("application-identifier", bundle_id)
    prefix = identity[:-len(bundle_id) - 1] if identity.endswith("." + bundle_id) else None
    if identity != bundle_id and not (prefix and TEAM_IDENTIFIER.fullmatch(prefix) and team in (None, prefix)):
        raise ValueError(f"Entitlement 'application-identifier' {identity!r} must be the bundle identifier "
                         f"{bundle_id!r}, optionally behind the team identifier")
    return granted


def fat_architectures(data):
    """The CPU types a universal (fat) Mach-O names, as far as its header is
    complete. Its header is big-endian on disk (0xcafebabe), 20 bytes per slice."""
    order = ">" if data[:4] == b"\xca\xfe\xba\xbe" else "<"
    count = struct.unpack_from(order + "I", data, 4)[0]
    return [struct.unpack_from(order + "I", data, 8 + index * 20)[0]
            for index in range(min(count, 64)) if 8 + (index + 1) * 20 <= len(data)]


def macho_header(data, what, file_type=MH_EXECUTE):
    """Check that data is a thin arm64 Mach-O of the given file type (an
    executable, or a dynamic library for embedded code); name what it is otherwise."""
    article, noun = FILE_TYPES[file_type]
    if len(data) < 32:
        raise ValueError(f"{what} is not a Mach-O {noun} (only {len(data)} bytes)")
    magic = struct.unpack_from("<I", data)[0]
    if magic != 0xfeedfacf:
        found = MACH_O_MAGICS.get(magic)
        if found and "universal" in found:
            if CPU_ARM64 not in fat_architectures(data):
                raise ValueError(f"{what} is a {found} binary without an arm64 slice; build it for arm64")
            raise ValueError(f"{what} is a {found} binary; thin it to the arm64 slice (lipo -thin arm64)")
        raise ValueError(f"{what} is not a 64-bit Mach-O {noun}"
                         + (f" (it is a {found})" if found else f" (magic {magic:#010x})"))
    cpu, subtype, kind = struct.unpack_from("<III", data, 4)
    if cpu != CPU_ARM64:
        raise ValueError(f"{what} is built for {CPU_NAMES.get(cpu, f'CPU type {cpu:#x}')}, not arm64")
    if subtype & 0xffffff == 2:
        # The build verifies staged code as plain arm64 (subtype 0); arm64e
        # pointer authentication is the ABI of Apple's own processes here.
        raise ValueError(f"{what} is built for arm64e; build it for arm64")
    if kind != file_type:
        raise ValueError(f"{what} is a Mach-O file of type {kind}, not {article} {noun} (type {file_type})")
    if len(data) > MAX_CODE_BYTES:
        raise ValueError(f"{what} is larger than {MAX_CODE_BYTES >> 20} MiB, the most the payload verifier "
                         "reads of one program")


def embedded_entitlements(data):
    """The entitlements an input executable already carries in its signature,
    or none when it is unsigned. Only the signature's entitlement slot is read;
    the signature itself is replaced by the guest-tools build."""
    commands, command_bytes = struct.unpack_from("<II", data, 16)
    offset, signature = 32, None
    for _ in range(min(commands, 4096)):
        if offset + 8 > min(32 + command_bytes, len(data)):
            break
        command, size = struct.unpack_from("<II", data, offset)
        if size < 8:
            break
        if command == LC_CODE_SIGNATURE and size >= 16:
            signature = struct.unpack_from("<II", data, offset + 8)
        offset += size
    if signature is None:
        return {}
    start, length = signature
    if start + length > len(data) or length < 12:
        return {}
    magic, total, count = struct.unpack_from(">III", data, start)
    if magic != 0xfade0cc0 or count > 64 or total > length:
        return {}
    for index in range(count):
        if start + 12 + index * 8 + 8 > len(data):
            break
        slot, relative = struct.unpack_from(">II", data, start + 12 + index * 8)
        if slot == 5 and relative + 8 <= total:
            blob_magic, blob_size = struct.unpack_from(">II", data, start + relative)
            if blob_magic == 0xfade7171 and 8 <= blob_size <= total - relative:
                try:
                    value = plistlib.loads(data[start + relative + 8:start + relative + blob_size])
                except Exception as error:  # plistlib raises several kinds for damaged input
                    raise ValueError("The executable's embedded entitlements are not a property list") from error
                return value if isinstance(value, dict) else {}
    return {}


def read_entitlements_file(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Entitlements file {path} is not a regular file")
    data = path.read_bytes()
    if len(data) > 1 << 20:
        raise ValueError(f"Entitlements file {path} is larger than 1 MiB")
    try:
        value = plistlib.loads(data)
    except Exception as error:
        raise ValueError(f"Entitlements file {path} is not a property list") from error
    if not isinstance(value, dict):
        raise ValueError(f"Entitlements file {path} must hold a dictionary")
    return value


def bundle_files(root):
    """Every file of the bundle as relative paths, without following links;
    refuses links, special files and nested code other than embedded
    frameworks and libraries (Frameworks/<Name>.framework, Frameworks/<name>.dylib)
    and app extensions (PlugIns/<Name>.appex, with no nested code of their own)."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"{root} is not an .app directory")
    if not BUNDLE_NAME.fullmatch(root.name):
        raise ValueError(f"{root.name!r} is not a valid .app bundle name (letters, digits, '_', '.', '-')")
    files, count = [], 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        parts = relative.parts
        mode = path.lstat().st_mode
        if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
            # Also every link that would leave the bundle: the payload and the
            # installer helper carry regular files only.
            raise ValueError(f"{root.name} contains a symbolic link or special file: {relative}")
        if parts[0] in NESTED_CODE:
            raise ValueError(f"{root.name} contains nested code in {parts[0]}/; ExtensionKit extensions, XPC "
                             "services and other nested bundles are not supported (embedded frameworks and "
                             "libraries in Frameworks/ and app extensions in PlugIns/ are)")
        if parts[0] == PLUGINS and len(parts) > 1:
            if len(parts) == 2 and not (stat.S_ISDIR(mode) and EXTENSION_NAME.fullmatch(parts[1])):
                raise ValueError(f"{root.name}/PlugIns/ holds only <Name>.appex app extensions, not {parts[1]!r}")
            # An extension is one executable and its resources: installd
            # verifies it as a bundle of its own, and code it embedded would
            # need identities and trust of its own again.
            if len(parts) > 2 and stat.S_ISDIR(mode) and (
                    (len(parts) == 3 and parts[2] in (FRAMEWORKS, PLUGINS, *NESTED_CODE))
                    or Path(parts[-1]).suffix in (*NESTED_BUNDLES, ".framework")):
                raise ValueError(f"{root.name} contains nested code inside an app extension: {relative}; an "
                                 "extension carries only its executable and resources (link what it shares with "
                                 "the app from the app's Frameworks/)")
        elif stat.S_ISDIR(mode):
            kind = NESTED_BUNDLES.get(Path(parts[-1]).suffix)
            if kind:
                raise ValueError(f"{root.name} contains {kind}: {relative}; only frameworks and libraries in "
                                 "Frameworks/ and app extensions in PlugIns/ may be nested code")
            if parts[-1].endswith(".framework") and not (len(parts) == 2 and parts[0] == FRAMEWORKS):
                raise ValueError(f"{root.name} contains a framework outside Frameworks/: {relative}; embedded "
                                 "frameworks are Frameworks/<Name>.framework, none nested in another")
        if parts[0] == FRAMEWORKS and len(parts) == 2 and not (
                FRAMEWORK_NAME.fullmatch(parts[1]) if stat.S_ISDIR(mode) else LIBRARY_NAME.fullmatch(parts[1])):
            raise ValueError(f"{root.name}/Frameworks/ holds only <Name>.framework bundles and <name>.dylib "
                             f"libraries, not {parts[1]!r}")
        if (parts[0] == FRAMEWORKS and len(parts) == 3 and stat.S_ISDIR(mode)
                and parts[2] in (FRAMEWORKS, PLUGINS, *NESTED_CODE)):
            raise ValueError(f"{root.name} contains nested code inside a framework: {relative}")
        if stat.S_ISDIR(mode):
            continue
        count += 1
        if count > MAX_FILES:
            raise ValueError(f"{root.name} has more than {MAX_FILES} files")
        files.append(str(relative))
    return files


def previous_signature(name):
    """Whether a bundle path belongs to an input signature (the app's, an
    embedded framework's or an app extension's _CodeSignature/), which the
    build replaces."""
    parts = name.split("/")
    return parts[0] == "_CodeSignature" or (len(parts) > 3 and parts[0] in (FRAMEWORKS, PLUGINS)
                                            and parts[2] == "_CodeSignature")


def read_info(root, where, relative):
    """A nested bundle's Info.plist dictionary; where names the bundle in errors."""
    data = (Path(root) / relative).read_bytes()
    if len(data) > 1 << 20:
        raise ValueError(f"{where}/Info.plist is larger than 1 MiB")
    try:
        info = plistlib.loads(data)
    except Exception as error:
        raise ValueError(f"{where}/Info.plist is not a property list") from error
    if not isinstance(info, dict):
        raise ValueError(f"{where}/Info.plist must hold a dictionary")
    return info


def extension_identity(where, info, bundle_id):
    """Check an app extension's Info.plist the way installd's MIPluginKitBundle
    and MIAppExtensionBundle do, so a refusal is named before the boot; returns
    its NSExtensionPointIdentifier. Its identifier is the app's followed by
    one more component, and it declares an extension point. Whether the guest
    knows that point (installd refuses an unknown one) and accepts the
    extension's principal class is decided by the phone's own catalog."""
    identifier = info["CFBundleIdentifier"]
    if not identifier.startswith(bundle_id + "."):
        raise ValueError(f"{where} has the bundle identifier {identifier}, which is not prefixed by its app's "
                         f"identifier {bundle_id}. (installd refuses it: AppexBundleIDNotPrefixed)")
    if "." in identifier[len(bundle_id) + 1:]:
        raise ValueError(f"{where} has the bundle identifier {identifier}, whose part after {bundle_id}. contains "
                         "a '.' (installd refuses it: AppexBundleIDSuffixContainsPeriod)")
    package = info.get("CFBundlePackageType", "XPC!")
    if package != "XPC!":
        raise ValueError(f"{where} is a {package!r} package, not an app extension (XPC!)")
    for key in ("CFBundleVersion", "CFBundleName"):
        if not isinstance(info.get(key), str) or not info[key]:
            raise ValueError(f"{where}/Info.plist has no {key} (a non-empty string), which installd requires of an "
                             "app extension")
    for key in ("CFBundleShortVersionString", "CFBundleDisplayName"):
        if key in info and (not isinstance(info[key], str) or not info[key]):
            raise ValueError(f"{where}/Info.plist has a {key} that is not a non-empty string")
    extension = info.get("NSExtension")
    if not isinstance(extension, dict):
        raise ValueError(f"{where}/Info.plist has no NSExtension dictionary; ExtensionKit extensions "
                         "(EXAppExtensionAttributes) belong in Extensions/, which is not supported")
    point = extension.get("NSExtensionPointIdentifier")
    if not isinstance(point, str) or not EXTENSION_POINT.fullmatch(point):
        raise ValueError(f"{where}/Info.plist has no valid NSExtension NSExtensionPointIdentifier")
    entries = [key for key in ("NSExtensionPrincipalClass", "NSExtensionMainStoryboard") if key in extension]
    for key in entries:
        if not isinstance(extension[key], str) or not EXTENSION_CLASS.fullmatch(extension[key]):
            raise ValueError(f"{where}/Info.plist has an NSExtension {key} that is not a class or storyboard name")
    if len(entries) > 1:
        raise ValueError(f"{where}/Info.plist names both NSExtensionPrincipalClass and NSExtensionMainStoryboard; "
                         "an extension has one of them")
    if entries and point in PRINCIPAL_CLASS_PROHIBITED:
        raise ValueError(f"{where}/Info.plist names an {entries[0]}, which the extension point {point} does not "
                         "allow (installd refuses it: AppexBundleContainsClassOrStoryboard)")
    return point


def embedded_code(root, names, bundle_id):
    """The embedded frameworks, libraries and app extensions of a bundle, by
    path: each framework's and extension's binary is the one its Info.plist
    names, its signing identifier its bundle identifier (installd verifies
    both as bundles of their own and compares the two); a library's
    identifier is its file name without .dylib. Identifiers are distinct from
    the app's and each other's."""
    root = Path(root)
    code, identities = [], {bundle_id: root.name}
    bundles = [(FRAMEWORKS, name.split("/")[1]) for name in names
               if name.startswith(FRAMEWORKS + "/") and name.count("/") >= 2]
    bundles += [(PLUGINS, name.split("/")[1]) for name in names if name.startswith(PLUGINS + "/")]
    for directory, bundle in sorted(set(bundles)):
        where = f"{root.name}/{directory}/{bundle}"
        if f"{directory}/{bundle}/Info.plist" not in names:
            raise ValueError(f"{where} has no Info.plist at its top level")
        info = read_info(root, where, f"{directory}/{bundle}/Info.plist")
        identifier, executable = info.get("CFBundleIdentifier"), info.get("CFBundleExecutable")
        if not isinstance(identifier, str) or not BUNDLE_IDENTIFIER.fullmatch(identifier):
            raise ValueError(f"{where}/Info.plist has no valid CFBundleIdentifier (letters, digits, '.', '-')")
        if not isinstance(executable, str) or not EXECUTABLE_NAME.fullmatch(executable):
            raise ValueError(f"{where}/Info.plist has no valid CFBundleExecutable")
        if f"{directory}/{bundle}/{executable}" not in names:
            raise ValueError(f"{where} has no {'binary' if directory == FRAMEWORKS else 'executable'} "
                             f"{executable!r} (named by CFBundleExecutable)")
        if directory == FRAMEWORKS:
            package = info.get("CFBundlePackageType", "FMWK")
            if package != "FMWK":
                raise ValueError(f"{where} is a {package!r} package, not a framework (FMWK)")
        if identifier in identities:
            raise ValueError(f"{where} uses the bundle identifier {identifier} of {identities[identifier]}; "
                             f"every {'framework' if directory == FRAMEWORKS else 'extension'} needs an "
                             "identifier of its own")
        identities[identifier] = where
        row = {"path": f"{directory}/{bundle}/{executable}", "kind": "framework" if directory == FRAMEWORKS else "extension",
               "bundle": f"{directory}/{bundle}", "identifier": identifier}
        if directory == PLUGINS:
            row["extension_point"] = extension_identity(where, info, bundle_id)
        code.append(row)
    for name in names:
        if name.startswith(FRAMEWORKS + "/") and name.count("/") == 1:
            code.append({"path": name, "kind": "library", "identifier": name[len(FRAMEWORKS) + 1:-len(".dylib")]})
    return sorted(code, key=lambda row: row["path"])


def team_prefix(entitlements, bundle_id):
    """The team an entitlement set names: its application-identifier's prefix
    or its team identifier, None without either."""
    identity = entitlements["application-identifier"]
    return (identity[:-len(bundle_id) - 1] if identity != bundle_id else None) or entitlements.get(
        "com.apple.developer.team-identifier")


def inspect_bundle(path, entitlements=None):
    """Read and check a bundle; the record names what the build will stage.

    `entitlements` is an optional plist file; without it the executable's own
    embedded entitlements (if it is signed) are the request. Either way the
    set is validated against the allowlist."""
    root = Path(path)
    names = bundle_files(root)
    if "Info.plist" not in names:
        raise ValueError(f"{root.name} has no Info.plist at its top level")
    info_data = (root / "Info.plist").read_bytes()
    if len(info_data) > 1 << 20:
        raise ValueError(f"{root.name}/Info.plist is larger than 1 MiB")
    try:
        info = plistlib.loads(info_data)
    except Exception as error:
        raise ValueError(f"{root.name}/Info.plist is not a property list") from error
    if not isinstance(info, dict):
        raise ValueError(f"{root.name}/Info.plist must hold a dictionary")
    bundle_id = info.get("CFBundleIdentifier")
    if not isinstance(bundle_id, str) or not BUNDLE_IDENTIFIER.fullmatch(bundle_id):
        raise ValueError(f"{root.name}/Info.plist has no valid CFBundleIdentifier (letters, digits, '.', '-')")
    executable = info.get("CFBundleExecutable")
    if not isinstance(executable, str) or not EXECUTABLE_NAME.fullmatch(executable):
        raise ValueError(f"{root.name}/Info.plist has no valid CFBundleExecutable")
    if executable not in names:
        raise ValueError(f"{root.name} has no executable {executable!r} (named by CFBundleExecutable)")
    package = info.get("CFBundlePackageType", "APPL")
    if package != "APPL":
        raise ValueError(f"{root.name} is a {package!r} package, not an application (APPL)")
    nested = embedded_code(root, names, bundle_id)
    code = {executable, *(row["path"] for row in nested)}
    files, total, digest = [], 0, hashlib.sha256()
    for name in names:
        if previous_signature(name):
            # A previous signature is replaced by the build's own.
            continue
        size = (root / name).lstat().st_size
        if size > MAX_FILE_BYTES:
            raise ValueError(f"{root.name}/{name} is larger than {MAX_FILE_BYTES >> 20} MiB")
        if not size:
            # The payload verifier reads only non-empty regular files.
            raise ValueError(f"{root.name}/{name} is empty; remove it from the bundle")
        total += size
        if total > MAX_BUNDLE_BYTES:
            raise ValueError(f"{root.name} is larger than {MAX_BUNDLE_BYTES >> 20} MiB")
        data = (root / name).read_bytes()
        if len(data) != size:
            raise ValueError(f"{root.name}/{name} changed while it was read")
        if name not in code and len(data) >= 4 and struct.unpack_from("<I", data)[0] in MACH_O_MAGICS:
            raise ValueError(f"{root.name} contains a second Mach-O file: {name}; only the main executable, "
                             "the frameworks and libraries in Frameworks/ and the app extensions' executables "
                             "may be code")
        sha = hashlib.sha256(data).hexdigest()
        digest.update(sha.encode() + b"  " + name.encode() + b"\n")
        files.append({"path": name, "bytes": size, "sha256": sha})
    program = (root / executable).read_bytes()
    macho_header(program, f"{root.name}/{executable}")
    for row in nested:
        macho_header((root / row["path"]).read_bytes(), f"{root.name}/{row['path']}",
                     MH_EXECUTE if row["kind"] == "extension" else MH_DYLIB)
    requested = read_entitlements_file(entitlements) if entitlements is not None else embedded_entitlements(program)
    granted = application_entitlements(bundle_id, requested)
    team = team_prefix(granted, bundle_id)
    for row in nested:
        if row["kind"] != "extension":
            continue
        # An extension is signed with its own entitlements: those embedded in
        # its executable's input signature, else only its application-identifier.
        # The allowlist and the identifier rule are the app's; a team it names
        # is the app's team.
        where = f"{root.name}/{row['bundle']}"
        try:
            row["entitlements"] = application_entitlements(row["identifier"],
                                                           embedded_entitlements((root / row["path"]).read_bytes()))
        except ValueError as error:
            raise ValueError(f"{where}: {error}") from error
        other = team_prefix(row["entitlements"], row["identifier"])
        if team and other and other != team:
            raise ValueError(f"{where} is signed for the team {other}, its app {root.name} for {team}; an "
                             "extension belongs to its app's team")
    minimum = info.get("MinimumOSVersion")
    return {"name": root.name, "path": str(root), "bundle_id": bundle_id, "executable": executable,
            "display_name": info.get("CFBundleDisplayName") or info.get("CFBundleName") or root.name[:-4],
            "version": str(info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or ""),
            "minimum_os": minimum if isinstance(minimum, str) else None,
            "files": files, "bytes": total, "content_sha256": digest.hexdigest(), "nested_code": nested,
            "entitlements": granted, "entitlements_source": "file" if entitlements is not None else
            ("embedded" if requested else "default")}


def verify_bundle_resources(files, code_resources, executable):
    """Check a sealed CodeResources against the bundle's actual files.

    `files` maps every bundle file (relative path, the executable included,
    `_CodeSignature/CodeResources` excluded) to its bytes. The v1 inventory
    must name exactly the non-code files with their SHA-1; the v2 inventory the
    same files but Info.plist and PkgInfo (sealed otherwise, or omitted by the
    signer's rules) with SHA-1 and SHA-256. A manifest that names a file the
    bundle lacks, or lacks one it has, is refused."""
    try:
        resources = plistlib.loads(code_resources)
    except Exception as error:
        raise ValueError("CodeResources is not a property list") from error
    if not isinstance(resources, dict) or not isinstance(resources.get("files"), dict) \
            or not isinstance(resources.get("files2"), dict):
        raise ValueError("CodeResources lacks its file inventories")
    expected = {name: data for name, data in files.items() if name != executable}
    if set(resources["files"]) != set(expected):
        unexpected = sorted(set(resources["files"]) - set(expected))
        missing = sorted(set(expected) - set(resources["files"]))
        raise ValueError("The bundle's resource inventory differs from its signed manifest"
                         + (f"; manifest names missing files: {', '.join(unexpected[:5])}" if unexpected else "")
                         + (f"; unsealed files: {', '.join(missing[:5])}" if missing else ""))
    for name, entry in resources["files"].items():
        recorded = entry.get("hash") if isinstance(entry, dict) else entry
        if recorded != hashlib.sha1(expected[name]).digest():
            raise ValueError(f"The signed manifest's hash of {name} differs from the file")
    omitted = {"Info.plist", "PkgInfo"}
    if set(resources["files2"]) != set(expected) - omitted:
        raise ValueError("The bundle's version-2 resource inventory differs from its signed manifest")
    for name, entry in resources["files2"].items():
        if (not isinstance(entry, dict) or entry.get("hash") != hashlib.sha1(expected[name]).digest()
                or entry.get("hash2") != hashlib.sha256(expected[name]).digest()):
            raise ValueError(f"The signed manifest's hashes of {name} differ from the file")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect",))
    parser.add_argument("bundle", type=Path, help="An .app directory built for arm64 iOS")
    parser.add_argument("--entitlements", type=Path, help="Entitlements plist to request instead of the executable's embedded ones")
    args = parser.parse_args()
    try:
        record = inspect_bundle(args.bundle, args.entitlements)
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    print(json.dumps({"ok": True, **{key: value for key, value in record.items() if key != "files"},
                      "file_count": len(record["files"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
