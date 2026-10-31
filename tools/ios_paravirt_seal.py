"""Explicit research policy for exactly two local, digest-checked seal objects.

This is a substitute authentication policy, NOT Apple signature verification.
It is disabled unless the restore preparer receives research-seal inputs.
"""
import hashlib
from pathlib import Path
import re
import struct

from ios_paravirt_firmware import FirmwareBundle, MANIFEST_LIMIT, der_fields, read_component, validate_im4m

BUILD_MANIFEST_SHA256 = "96b65481173fb8dbb87ba4a770cbf9ca26512ea3222ea38ca9e23532db29ccc4"
SEAL_CAVE = 0x2ce000
SEAL_CALLS = (0x60e00, 0x60e34)
ORIGINAL_VERIFIER = 0x60148
PIN_MARKER = b"seal-pins-v1\0\0\0\0"
COMPONENTS = {"Ap,SystemVolumeCanonicalMetadata": b"msys", "SystemVolume": b"isys"}
STUBS = {"CFDataGetBytePtr": 0x1ae3e0, "CFDataGetLength": 0x1ae400,
         "CC_SHA384": 0x1ae200, "malloc_type_malloc": 0x1b06f0,
         "memcpy": 0x1b07b0, "memcmp": 0x1b07a0, "puts": 0x1b10b0,
         "original": ORIGINAL_VERIFIER}


def image_pin(name, image):
    fields = der_fields(image)
    if len(fields) != 3 or image[fields[0][1]:fields[0][2]] != b"IMG4":
        raise ValueError("Seal pin requires a complete IMG4 container")
    # The second top-level field is IM4P; keep its DER header when decoding it.
    im4p_start = fields[0][2]
    im4p = image[im4p_start:fields[1][2]]
    payload_fields = der_fields(im4p)
    if (name not in COMPONENTS or len(payload_fields) < 4
            or im4p[payload_fields[0][1]:payload_fields[0][2]] != b"IM4P"
            or im4p[payload_fields[1][1]:payload_fields[1][2]] != COMPONENTS[name]
            or payload_fields[3][0] != 4):
        raise ValueError("Seal pin has an unsupported component or payload type")
    start, end = payload_fields[3][1:]
    offset, size = im4p_start + start, end - start
    if not 0 < size <= len(image) - offset or len(image) > 0xffffffff:
        raise ValueError("Seal payload bounds are invalid")
    return {"component": name, "tag": int.from_bytes(COMPONENTS[name], "big"),
            "bytes": len(image), "payload_offset": offset, "payload_bytes": size,
            "sha384": hashlib.sha384(image).hexdigest()}


def prepare_policy(firmware, identity, manifest, overrides, userland=None):
    if set(overrides) - COMPONENTS.keys():
        raise ValueError("Research seal overrides must name only the two sealing components")
    bundle = FirmwareBundle(firmware, identity, overrides)
    if bundle.version["build_manifest_sha256"] != BUILD_MANIFEST_SHA256:
        raise ValueError("Research sealing requires the exact original PCC 23E5207q BuildManifest")
    if userland is not None:
        from ios_paravirt_userland import IPHONE_BUILD_SHA256, attach_userland
        attach_userland(bundle, userland)
        if bundle.version["ios_userland"]["build_manifest_sha256"] != IPHONE_BUILD_SHA256:
            raise ValueError("iPhone research sealing requires the exact original 24A446 BuildManifest")
    path = Path(manifest).absolute()
    ticket = read_component(path.parent, path.name, MANIFEST_LIMIT)
    validate_im4m(ticket)
    pins, sources = [], []
    for name in COMPONENTS:
        image, source = bundle.image(name, ticket)
        pins.append(image_pin(name, image))
        sources.append(source)
    return {"mode": "exact-component-hash-pins-only", "authentication_checks_modified": True,
            "apple_signature_verified": False, "fallback": "original-Image4-verifier",
            "firmware": bundle.version, "manifest_sha256": hashlib.sha256(ticket).hexdigest(),
            "pins": pins, "sources": sources}


def assemble_adapter(output, pins, source, run):
    if [pin["component"] for pin in pins] != list(COMPONENTS):
        raise ValueError("Exactly the two supported seal pins are required, in canonical order")
    table = bytearray()
    for pin in pins:
        fields = [pin[key] for key in ("tag", "bytes", "payload_offset", "payload_bytes")]
        if (any(type(value) is not int or not 0 <= value <= 0xffffffff for value in fields)
                or fields[0] != int.from_bytes(COMPONENTS[pin["component"]], "big")
                or not 0 < fields[3] <= fields[1] - fields[2]
                or not re.fullmatch(r"[0-9a-f]{96}", pin["sha384"])):
            raise ValueError("Invalid bounded seal pin")
        table.extend(struct.pack("<IIII48s", *fields, bytes.fromhex(pin["sha384"])))
    obj, binary = output / "seal-adapter.o", output / "seal-adapter.bin"
    run(["llvm-mc", "-triple=aarch64", "-filetype=obj", source, "-o", obj])
    relocations = run(["llvm-objdump", "-r", obj]).decode()
    run(["llvm-objcopy", "-O", "binary", "--only-section=.text", obj, binary])
    adapter = bytearray(binary.read_bytes())
    references = re.findall(r"^([0-9a-fA-F]+)\s+(R_\S+)\s+(\S+)\s*$", relocations, re.MULTILINE)
    if not 128 <= len(adapter) <= 8192 or len(references) != 13 or adapter.count(PIN_MARKER) != 1:
        raise ValueError("Unexpected research seal adapter layout")
    seen = set()
    for location, kind, symbol in references:
        offset = int(location, 16)
        opcode = {"R_AARCH64_CALL26": 0x94000000, "R_AARCH64_JUMP26": 0x14000000}.get(kind)
        if (opcode is None or symbol not in STUBS or offset in seen or offset % 4
                or adapter[offset:offset + 4] != struct.pack("<I", opcode)):
            raise ValueError("Unexpected research seal adapter relocation")
        delta = STUBS[symbol] - SEAL_CAVE - offset
        if delta % 4 or not -(1 << 27) <= delta < 1 << 27:
            raise ValueError("Research seal adapter branch exceeds ARM64 range")
        seen.add(offset)
        adapter[offset:offset + 4] = struct.pack("<I", opcode | ((delta >> 2) & 0x3ffffff))
    start = adapter.index(PIN_MARKER) + len(PIN_MARKER)
    if adapter[start:start + 128] != bytes(128):
        raise ValueError("Research seal pin table is not empty")
    adapter[start:start + 128] = table
    binary.write_bytes(adapter)
    return bytes(adapter)
