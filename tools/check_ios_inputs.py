#!/usr/bin/env python3
"""Check the person's own inputs for the virtual iOS device before any long step.

Looks at the unpacked iPhone and PCC restore images, the decrypted disk images,
the public IM4M template and a prepared IM4M, as far as each is given, and says
in plain words what is wrong and how to fix it: the wrong build, a swapped
directory, a missing component, an encrypted archive, an IM4M made for another
image or device. It reads manifests and file headers and hashes only small files
(manifests, template, IM4M); the large images' digests are checked later by the
import. Nothing is changed, fetched or sent, and nothing of Apple's is supplied.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import sys

from ios_paravirt_firmware import MANIFEST_LIMIT, read_component
from ios_paravirt_manifest import check_build, inspect_manifest
from ios_paravirt_userland import IPHONE_BUILD_SHA256
from prepare_ios_paravirt import MAX_TREE_BYTES, decode
from prepare_ios_research_manifest import PCC_BUILD_SHA256, TEMPLATE_SHA256

SETUP = "docs/virtual-ios/setup.md"
IPHONE = {"build": "24A446", "version": "27.0.1", "device": "iPhone17,3", "identity": 0}
PCC = {"build": "23E5207q", "version": "26.4 beta", "device": "vphone600", "identity": 5}
# The components the chain reads from each identity, and what the person decrypts.
IPHONE_IMAGES = ("OS", "Cryptex1,SystemOS")
PCC_BOOT = ("KernelCache", "DeviceTree", "SEP", "RestoreRamDisk", "RestoreTrustCache")
PCC_BOOT_FILES = ("sptm.vresearch1.release.im4p", "txm.iphoneos.research.im4p")
IM4M_LIMIT = 9216


def check(name, path, status, problem=None, fix=None, **details):
    return {"input": name, "path": str(path) if path else None, "status": status,
            "problem": problem, "fix": fix, **details}


def read_manifest(root):
    """(raw bytes, parsed dictionary) of a directory's BuildManifest.plist."""
    data = read_component(root, "BuildManifest.plist", 16 * MANIFEST_LIMIT)
    manifest = plistlib.loads(data)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("BuildIdentities"), list):
        raise ValueError("BuildManifest.plist has no build identities")
    return data, manifest


def describe(manifest):
    """The build a manifest names, in words."""
    devices = manifest.get("SupportedProductTypes") or []
    return (f"{', '.join(map(str, devices)) or 'an unknown device'} {manifest.get('ProductVersion', '?')} "
            f"build {manifest.get('ProductBuildVersion', '?')}")


def identity_manifest(manifest, index):
    identities = manifest["BuildIdentities"]
    if not 0 <= index < len(identities) or not isinstance(identities[index].get("Manifest"), dict):
        raise ValueError(f"the BuildManifest has no identity {index}")
    return identities[index]["Manifest"]


def component_path(components, name):
    path = ((components.get(name) or {}).get("Info") or {}).get("Path")
    return path if isinstance(path, str) and path else None


def firmware_check(name, root, expected, digest, other_digest, other_name):
    """One unpacked restore image: the exact manifest, then that the identity's files were unpacked."""
    if root is None:
        return check(name, None, "skipped", "not given"), None
    if not root.is_dir():
        return check(name, root, "fail", "not a directory",
                     f"Unpack the {expected['device']} {expected['build']} archive with unzip -n into it."), None
    if not (root / "BuildManifest.plist").is_file():
        return check(name, root, "fail", "has no BuildManifest.plist, so it is not an unpacked restore image",
                     "Unpack the whole .ipsw archive into this directory, keeping its structure."), None
    try:
        data, manifest = read_manifest(root)
    except (OSError, ValueError, plistlib.InvalidFileException) as error:
        return check(name, root, "fail", f"its BuildManifest.plist cannot be read: {error}",
                     "Unpack the archive again; the file is damaged or incomplete."), None
    found = hashlib.sha256(data).hexdigest()
    if found == other_digest:
        return check(name, root, "fail", f"this is the {other_name}, not the {name}",
                     f"The two directories are swapped. Pass this one as the {other_name}."), None
    if found != digest:
        return check(name, root, "fail",
                     f"it holds {describe(manifest)}, but the chain needs {expected['device']} "
                     f"{expected['version']} build {expected['build']} exactly (BuildManifest SHA-256 {digest})",
                     f"The kernel and adapters are pinned to that build; another one is refused. Download build "
                     f"{expected['build']} as {SETUP} says.", build=manifest.get("ProductBuildVersion")), None
    try:
        components = identity_manifest(manifest, expected["identity"])
    except ValueError as error:
        return check(name, root, "fail", str(error), "Unpack the archive again."), None
    return check(name, root, "pass", build=expected["build"], manifest_sha256=found), components


def missing_components(root, components, names):
    """The listed components whose file the unpacked directory lacks (an .aea counts as present)."""
    absent = []
    for name in names:
        path = component_path(components, name)
        if path is None:
            absent.append(f"{name} (not in the identity)")
        elif not ((root / path).is_file() or (root / (path + ".aea")).is_file()
                  or (path.endswith(".aea") and (root / path[:-4]).is_file())):
            absent.append(f"{name} ({path})")
    return absent


def image_check(name, path, expected_names, unpacked):
    """A decrypted disk image: present, not an AEA archive, an APFS container, and the component it should be."""
    if path is None:
        return check(name, None, "skipped", "not given")
    if not path.is_file():
        return check(name, path, "fail", "not a file",
                     f"Decrypt the component with the external ipsw tool as {SETUP} step 4 shows.")
    with path.open("rb") as stream:
        header = stream.read(48)
    if header[:4] == b"AEA1":
        return check(name, path, "fail", "it is still an encrypted AEA archive; Tomacco has no decryption",
                     f"Decrypt it first ({SETUP}, step 4) and pass the resulting .dmg.")
    if len(header) < 36 or header[32:36] != b"NXSB":
        return check(name, path, "fail", "it is not an APFS disk image (no NXSB container header)",
                     "Pass the decrypted .dmg of that component, not another file.")
    size = path.stat().st_size
    stem = path.name
    names = {Path(value).name.removesuffix(".aea") for value in expected_names if value}
    if names and stem not in names:
        return check(name, path, "warn", f"its name {stem} is not the {', '.join(sorted(names))} the {unpacked} "
                     "lists for it; it may be another component",
                     "Check that you decrypted the right file. The import compares its digest with the manifest.",
                     bytes=size)
    return check(name, path, "pass", bytes=size)


def template_check(path):
    if path is None:
        return check("IM4M template", None, "skipped", "not given")
    if not path.is_file():
        return check("IM4M template", path, "fail", "not a file", f"Download ticket.shsh2 as {SETUP} says.")
    data = read_component(path.parent, path.name, MANIFEST_LIMIT)
    if data.startswith((b"<?xml", b"bplist")):
        try:
            saved = plistlib.loads(data)
        except plistlib.InvalidFileException:
            saved = None
        data = saved.get("ApImg4Ticket") if isinstance(saved, dict) else None
        if not isinstance(data, bytes):
            return check("IM4M template", path, "fail", "the property list holds no ApImg4Ticket",
                         f"Use the ticket.shsh2 at the pinned revision {SETUP} names.")
    if hashlib.sha256(data).hexdigest() != TEMPLATE_SHA256:
        return check("IM4M template", path, "fail", "it is not the documented research template",
                     f"Download ticket.shsh2 from the exact revision {SETUP} names; another ticket does not work.")
    return check("IM4M template", path, "pass")


def tree_ecid(tree):
    data = decode(read_component(tree.parent, tree.name, MAX_TREE_BYTES))
    value = data.child("chosen").get("unique-chip-id")
    if len(value) != 8:
        raise ValueError("the device tree declares no eight-byte unique-chip-id")
    return int.from_bytes(value, "little")


def im4m_check(path, iphone_components, tree):
    if path is None:
        return check("IM4M", None, "skipped", "not given")
    make = ("Make it with tools/prepare_ios_research_manifest.py from the template, both firmware directories "
            f"and your restore device tree ({SETUP}, How do I get the IM4M?).")
    if not path.is_file():
        return check("IM4M", path, "fail", "not a file", make)
    if path.stat().st_size > IM4M_LIMIT:
        return check("IM4M", path, "fail", f"it is {path.stat().st_size} bytes, more than an IM4M's {IM4M_LIMIT}; "
                     "it is probably the template or another file", make)
    try:
        info = inspect_manifest(read_component(path.parent, path.name, IM4M_LIMIT))
    except ValueError as error:
        return check("IM4M", path, "fail", f"it is not a DER IM4M: {error}", make)
    hardware = info["declared_hardware"]
    if (hardware.get("CHIP"), hardware.get("BORD")) != (0xfe01, 0x91):
        return check("IM4M", path, "fail", "it declares another device than the virtual vphone600 "
                     f"(chip {hardware.get('CHIP'):#x}, board {hardware.get('BORD'):#x})", make)
    if iphone_components is not None:
        digest = (iphone_components.get("OS") or {}).get("Digest")
        try:
            check_build(info, digest)
        except ValueError:
            return check("IM4M", path, "fail", f"it was made for another iPhone image than build {IPHONE['build']}: "
                         "its OS digest differs from the iPhone BuildManifest's", make)
    if tree is not None:
        try:
            ecid = tree_ecid(tree)
        except (OSError, ValueError) as error:
            return check("IM4M", path, "fail", f"the device tree {tree} cannot be read: {error}",
                         "Pass the restore tree prepare_ios_paravirt.py wrote.")
        if "ECID" in hardware and hardware["ECID"] != ecid:
            return check("IM4M", path, "fail", f"it names device {hardware['ECID']:#x}, but the device tree is "
                         f"device {ecid:#x}; it was made for another prepared device", make
                         + " Do not regenerate it for a device that is already restored.")
    return check("IM4M", path, "pass", ecid=hex(hardware["ECID"]) if "ECID" in hardware else None)


def guarded(name, path, run):
    """Run one check; a file that cannot be read becomes a failed row instead of an exception."""
    try:
        return run()
    except (OSError, ValueError, KeyError, TypeError) as error:
        return check(name, path, "fail", f"it cannot be read: {error}", "Check the path and the file.")


def default_image(root, components, name):
    """Where setup.md puts the decrypted image of a component: ROOT/decrypted/NAME without .aea."""
    path = component_path(components or {}, name) if components else None
    return root / "decrypted" / Path(path).name.removesuffix(".aea") if root and path else None


def inspect(args):
    rows = []
    iphone_row, iphone = firmware_check("iPhone image", args.iphone_firmware, IPHONE, IPHONE_BUILD_SHA256,
                                        PCC_BUILD_SHA256, "PCC image")
    pcc_row, pcc = firmware_check("PCC image", args.pcc_firmware, PCC, PCC_BUILD_SHA256,
                                  IPHONE_BUILD_SHA256, "iPhone image")
    rows += [iphone_row, pcc_row]
    if iphone is not None:
        absent = missing_components(args.iphone_firmware, iphone, IPHONE_IMAGES)
        if absent:
            rows.append(check("iPhone image files", args.iphone_firmware, "fail", "lacks " + ", ".join(absent),
                              "Unpack the whole archive again with unzip -n."))
    if pcc is not None:
        absent = missing_components(args.pcc_firmware, pcc, PCC_BOOT)
        absent += [name for name in PCC_BOOT_FILES
                   if not any(args.pcc_firmware.rglob(name))]
        if absent:
            rows.append(check("PCC image files", args.pcc_firmware, "fail", "lacks " + ", ".join(absent),
                              "Unpack the whole archive again with unzip -n."))
    images = (("iPhone OS image", args.iphone_os_image, iphone, "OS", args.iphone_firmware, "iPhone image"),
              ("iPhone SystemOS image", args.iphone_systemos_image, iphone, "Cryptex1,SystemOS",
               args.iphone_firmware, "iPhone image"),
              ("PCC system image", args.pcc_system_image, pcc, "OS", args.pcc_firmware, "PCC image"))
    for name, given, components, component, root, unpacked in images:
        path = given or default_image(root, components, component)
        expected = [component_path(components, component)] if components else []
        rows.append(guarded(name, path, lambda: image_check(name, path, expected, unpacked)))
    rows.append(guarded("IM4M template", args.template, lambda: template_check(args.template)))
    rows.append(guarded("IM4M", args.manifest, lambda: im4m_check(args.manifest, iphone, args.device_tree)))
    failed = [row for row in rows if row["status"] == "fail"]
    return {"ok": not failed, "checks": rows,
            "summary": (f"{len(failed)} of the given inputs need attention before any long step"
                        if failed else "every given input is the one the chain needs")}


def parser():
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    path = lambda value: Path(value).expanduser().absolute()
    result.add_argument("--iphone-firmware", type=path, help="Unpacked iPhone17,3 24A446 restore image")
    result.add_argument("--pcc-firmware", type=path, help="Unpacked PCC 23E5207q restore image")
    result.add_argument("--iphone-os-image", type=path, help="Decrypted OS image; default IPHONE/decrypted/NAME")
    result.add_argument("--iphone-systemos-image", type=path,
                        help="Decrypted SystemOS Cryptex image; default IPHONE/decrypted/NAME")
    result.add_argument("--pcc-system-image", type=path, help="Decrypted PCC system image; default PCC/decrypted/NAME")
    result.add_argument("--template", type=path, help="The public ticket.shsh2 research template")
    result.add_argument("--manifest", type=path, help="Your prepared IM4M")
    result.add_argument("--device-tree", type=path, help="Your prepared restore device tree, to compare the ECID")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    report = inspect(args)
    for row in report["checks"]:
        if row["status"] in ("fail", "warn"):
            print(f"{row['input']} ({row['path']}): {row['problem']}\n  fix: {row['fix']}", file=sys.stderr)
    print(json.dumps(report, indent=1))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
