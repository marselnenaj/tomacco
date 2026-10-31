#!/usr/bin/env python3
"""Reproduce the local, unsigned vphone600 research manifest used by Tomacco.

The input template's signature and certificates are retained as opaque bytes.
Changing its body invalidates that signature. This is not Apple signing or
personalization and does not produce a ticket for physical devices.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib

from ios_paravirt_firmware import (FirmwareBundle, MANIFEST_LIMIT, der_fields,
                                   der_wrap, read_component, validate_im4m)
from ios_paravirt_manifest import check_build, inspect_manifest, object_set, required
from ios_paravirt_userland import IPHONE_BUILD_SHA256, OS_COMPONENTS
from prepare_ios_paravirt import MAX_TREE_BYTES, decode

TEMPLATE_SHA256 = "59bd2dca36992d9f5be656397164880ab829bc0c65080c246d6f57bfbb710d26"
PCC_BUILD_SHA256 = "96b65481173fb8dbb87ba4a770cbf9ca26512ea3222ea38ca9e23532db29ccc4"
# Entries without an explicit Img4PayloadType in the pinned BuildManifest.
TAGS = {"Ap,SystemVolumeCanonicalMetadata": "msys", "SystemVolume": "isys",
        "OS": "rosi", "AppleLogo": "logo", "BatteryCharging0": "chg0",
        "BatteryCharging1": "chg1", "BatteryFull": "batF", "BatteryLow0": "bat0",
        "BatteryLow1": "bat1", "BatteryPlugin": "glyP", "DeviceTree": "dtre",
        "KernelCache": "krnl", "LLB": "illb", "RecoveryMode": "recm",
        "RestoreRamDisk": "rdsk", "SEP": "sepi", "StaticTrustCache": "trst",
        "iBEC": "ibec", "iBSS": "ibss", "iBoot": "ibot"}


def private_object(key, value):
    label = key.encode("ascii")
    if len(label) != 4 or any(c < 32 or c > 126 for c in label):
        raise ValueError("Image4 keys must be four printable ASCII bytes")
    number = int.from_bytes(label, "big")
    tag = bytes([number & 127])
    number >>= 7
    while number:
        tag = bytes([128 | (number & 127)]) + tag
        number >>= 7
    payload = der_wrap(0x30, der_wrap(0x16, label) + der_wrap(*value))
    return b"\xff" + tag + der_wrap(0x30, payload)[1:]


def objects(values):
    return b"".join(sorted(private_object(k, v) for k, v in values.items()))


def unsigned_integer(value):
    if type(value) is not int or not 0 <= value <= 0xffffffffffffffff:
        raise ValueError("Manifest integers must be unsigned 64-bit values")
    raw = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    return 2, b"\0" + raw if raw[0] & 128 else raw


def rebuild(template, pcc, phone, ecid):
    """Apply the historical PCC declarations and four iPhone OS replacements."""
    validate_im4m(template)
    fields = der_fields(template)
    body = object_set(required(object_set(template[fields[2][1]:fields[2][2]]), "MANB", 0x31))
    props = object_set(required(body, "MANP", 0x31))
    for key, value in (("CHIP", 0xfe01), ("BORD", 0x91), ("ECID", ecid)):
        props[key] = unsigned_integer(value)
    props.update(CPRO=(1, b"\0"), CSEC=(1, b"\0"))
    body["MANP"] = 0x31, objects(props)
    for name, component in pcc.items():
        tag = component["Info"].get("Img4PayloadType", TAGS.get(name))
        digest = component.get("Digest")
        if tag is None or not isinstance(digest, bytes):
            continue
        if len(digest) != 48:
            raise ValueError(f"Expected SHA-384 digest for {name}")
        values = object_set(body[tag][1]) if tag in body else {}
        values["DGST"] = 4, digest
        for flag in ("EKEY", "EPRO", "ESEC"):
            values[flag] = 1, b"\xff" if flag == "EKEY" and component.get("Trusted") else b"\0"
        body[tag] = 0x31, objects(values)
    for name in OS_COMPONENTS:
        tag = TAGS[name]
        values = object_set(required(body, tag, 0x31))
        digest = phone[name]["Digest"]
        if not isinstance(digest, bytes) or len(digest) != 48:
            raise ValueError(f"Expected iPhone SHA-384 digest for {name}")
        values["DGST"] = 4, digest
        body[tag] = 0x31, objects(values)
    result = der_wrap(0x30, b"".join(
        der_wrap(tag, private_object("MANB", (0x31, objects(body)))) if i == 2
        else der_wrap(tag, template[start:end])
        for i, (tag, start, end) in enumerate(fields)))
    check_build(inspect_manifest(result), phone["OS"]["Digest"])
    return result


def prepare(template_path, pcc_root, phone_root, tree_path):
    template_path, tree_path = Path(template_path).absolute(), Path(tree_path).absolute()
    data = read_component(template_path.parent, template_path.name, MANIFEST_LIMIT)
    if data.startswith((b"<?xml", b"bplist")):
        saved = plistlib.loads(data)
        data = saved.get("ApImg4Ticket") if isinstance(saved, dict) else None
    if not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != TEMPLATE_SHA256:
        raise ValueError("Template differs from the documented qemu-t8030/Inferno research input")
    pcc = FirmwareBundle(pcc_root, 5)
    if pcc.version["build_manifest_sha256"] != PCC_BUILD_SHA256:
        raise ValueError("PCC BuildManifest differs from 23E5207q")
    phone_data = read_component(phone_root, "BuildManifest.plist", 16 * MANIFEST_LIMIT)
    if hashlib.sha256(phone_data).hexdigest() != IPHONE_BUILD_SHA256:
        raise ValueError("iPhone BuildManifest differs from 24A446")
    phone = plistlib.loads(phone_data)["BuildIdentities"][0]["Manifest"]
    tree = decode(read_component(tree_path.parent, tree_path.name, MAX_TREE_BYTES))
    ecid_bytes = tree.child("chosen").get("unique-chip-id")
    if len(ecid_bytes) != 8:
        raise ValueError("Prepared device tree must declare an eight-byte unique-chip-id")
    ecid = int.from_bytes(ecid_bytes, "little")
    result = rebuild(data, pcc.components, phone, ecid)
    return result, {"scope": "local-vphone600-research-manifest", "bytes": len(result),
                    "sha256": hashlib.sha256(result).hexdigest(), "ecid": hex(ecid),
                    "template_sha256": TEMPLATE_SHA256, "external_requests": False,
                    "inherited_signature_invalidated": True, "signature_verified": False,
                    "apple_authorized": False, "input_files_modified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True, help="Local documented ticket.shsh2 or its DER ApImg4Ticket")
    parser.add_argument("--pcc-firmware", required=True, help="Unpacked PCC 23E5207q directory")
    parser.add_argument("--iphone-firmware", required=True, help="Unpacked iPhone17,3 24A446 directory")
    parser.add_argument("--device-tree", required=True, help="Prepared restore device tree; supplies this VM's ECID")
    parser.add_argument("--output", required=True, help="New local IM4M file; existing files are never replaced")
    args = parser.parse_args()
    try:
        data, record = prepare(args.template, args.pcc_firmware, args.iphone_firmware, args.device_tree)
        with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(data)
        print(json.dumps({**record, "output": str(Path(args.output).absolute())}, indent=2))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Research manifest preparation failed: {error}\n")


if __name__ == "__main__":
    main()
