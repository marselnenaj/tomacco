#!/usr/bin/env python3
"""Pin a guest-tools build's development-signing adapter in a research kernel.

The research kernel's executable-mapping adapter (ios-gpu-mmap.S) lets a file
on /private/var be mapped executable only when its CodeDirectory hash is in a
short list compiled into the GPU cave: the Metal driver, its compiler, the
owned GPU bridge identity and the owned installd adapter. A guest-tools build
whose adapter hash is not in that list cannot have its adapter loaded into
installd, so its development signing stays silent.

This replaces only the adapter's 20-byte slot in that list with the given build's adapter hash. No
instruction and no other byte changes; the cave searches the list linearly
over a fixed count, so the order needs no maintenance. The input is the exact
recorded reference kernel, a kernel equal to it outside the user's own inputs (their
IM4M and mapping hashes: ios_paravirt_kernel.kernel_profile), or a kernel this
tool produced from either. Producing a kernel is not evidence that a guest maps
or runs the adapter.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct

from ios_paravirt_firmware import read_component
from ios_paravirt_kernel import kernel_profile

KERNEL_NAME = "kernelcache.research.vphone600"
RECEIPT_NAME = "adapter-pin.json"
SCOPE = "owned-adapter-identity-only"
# The reference kernel: bridge identity plus workloop adapter.
INPUT_SHA256 = "600c853e8f3127b9245e7f86c245dfaf6a4372d707d294a7245f7ea7a6e657da"
# kernel_profile of the reference kernel: a user's prepared kernel after the fileport and
# workloop tools. Their driver, compiler and bridge hashes are their own builds'.
INPUT_PROFILE_SHA256 = "f876fe3f00781e3c794cddc503b929ed2048c954da207a61a77a89ac4f4f3d91"
# The mapping adapter's `adr x11, allowed_code_hashes` and `mov w15, #count`,
# the list itself, and the entries other than the adapter's (file offsets).
ADR_OFFSET, COUNT_OFFSET, LIST_OFFSET = 0x1ca343c, 0x1ca3440, 0x1ca3490
ENTRIES, ADAPTER_SLOT, CDHASH_BYTES = 4, 3, 20
FIXED_ENTRIES = (
    "f45922e31ee414591b328c608d447fcf97cd9b86",  # Metal driver
    "a7483ef8ed860d6aa58497b8b89f4dd25827e0a4",  # Metal compiler plugin
    "ff72cd7ee8bb8707e691678ce68477a0e42c15bc",  # owned GPU bridge identity
)
ADAPTER_PATH = "usr/libexec/libtomacco-development-signing.dylib"


def adr_target(word, address):
    immlo, immhi = (word >> 29) & 3, (word >> 5) & 0x7ffff
    offset = (immhi << 2) | immlo
    if offset & (1 << 20):
        offset -= 1 << 21
    return address + offset


def adapter_slot(kernel, fixed=FIXED_ENTRIES):
    """The (offset, cdhash) of the adapter slot after checking the whole list's
    shape. `fixed`: the expected graphics entries; None takes the kernel's own."""
    if len(kernel) < LIST_OFFSET + (ENTRIES + 1) * CDHASH_BYTES:
        raise ValueError("Kernel is too short for the recorded mapping adapter layout")
    adr, count = struct.unpack_from("<II", kernel, ADR_OFFSET)
    if (adr & 0x9f00001f) != 0x1000000b or adr_target(adr, ADR_OFFSET) != LIST_OFFSET:
        raise ValueError("The mapping adapter does not address its hash list at the recorded place")
    if count != (0x52800000 | (ENTRIES << 5) | 15):
        raise ValueError(f"The mapping adapter does not search exactly {ENTRIES} hashes")
    entries = [kernel[LIST_OFFSET + index * CDHASH_BYTES:LIST_OFFSET + (index + 1) * CDHASH_BYTES].hex()
               for index in range(ENTRIES)]
    if fixed is not None and tuple(entries[:ADAPTER_SLOT]) != fixed:
        raise ValueError("The mapping list's graphics entries differ from the recorded driver, compiler and bridge")
    previous = entries[ADAPTER_SLOT]
    if not any(bytes.fromhex(previous)) or entries.count(previous) != 1:
        raise ValueError("The mapping list does not hold exactly one adapter hash")
    if any(kernel[LIST_OFFSET + ENTRIES * CDHASH_BYTES:LIST_OFFSET + (ENTRIES + 1) * CDHASH_BYTES]):
        raise ValueError("The mapping list is not followed by its recorded padding")
    return LIST_OFFSET + ADAPTER_SLOT * CDHASH_BYTES, previous


def pin(kernel, cdhash, fixed=FIXED_ENTRIES):
    """Return (kernel with the adapter slot set to `cdhash`, offset, previous cdhash)."""
    if not isinstance(cdhash, str) or re.fullmatch(r"[0-9a-f]{40}", cdhash) is None:
        raise ValueError("The adapter's CodeDirectory hash must be 40 lowercase hex digits")
    offset, previous = adapter_slot(kernel, fixed)
    if fixed is not None and cdhash in fixed:
        raise ValueError("The adapter's hash equals a graphics entry")
    if cdhash == previous:
        raise ValueError("The kernel already pins this adapter")
    candidate = bytearray(kernel)
    candidate[offset:offset + CDHASH_BYTES] = bytes.fromhex(cdhash)
    restored = bytearray(candidate)
    restored[offset:offset + CDHASH_BYTES] = kernel[offset:offset + CDHASH_BYTES]
    if restored != kernel or adapter_slot(bytes(candidate), fixed) != (offset, cdhash):
        raise ValueError("Kernel bytes outside the adapter slot changed")
    return bytes(candidate), offset, previous


def tools_adapter(tools):
    """(cdhash, sha256) of a guest-tools build's ARM64e development-signing adapter."""
    record = json.loads(read_component(tools, "guest-tools.json", 1 << 20))
    rows = [row for row in record.get("programs") or [] if isinstance(row, dict) and row.get("path") == ADAPTER_PATH]
    if record.get("state") != "built" or "development_signing" not in record or len(rows) != 1:
        raise ValueError("The guest-tools build carries no development-signing adapter")
    row = rows[0]
    if row.get("architecture") != "arm64e" or re.fullmatch(r"[0-9a-f]{40}", str(row.get("cdhash"))) is None:
        raise ValueError("The guest-tools build's adapter is not a recorded ARM64e program")
    data = read_component(tools, "Supplemental/" + ADAPTER_PATH, 4 << 20)
    if hashlib.sha256(data).hexdigest() != row.get("sha256"):
        raise ValueError("The guest-tools build's adapter changed after its build")
    return row["cdhash"], row["sha256"]


def recorded_origin(kernel_path, digest):
    """Whether this is the exact recorded reference kernel, or a kernel this tool produced from it."""
    if digest == INPUT_SHA256:
        return True
    try:
        receipt = json.loads((kernel_path.parent / RECEIPT_NAME).read_text())
    except (OSError, ValueError):
        return False
    return (isinstance(receipt, dict) and receipt.get("scope") == SCOPE and receipt.get("output_sha256") == digest
            and receipt.get("origin_sha256") == INPUT_SHA256)


def accepted_input(kernel_path, data):
    """The digest of a kernel `recorded_origin` accepts, or of one with the reference kernel's profile."""
    digest = hashlib.sha256(data).hexdigest()
    if recorded_origin(kernel_path, digest) or kernel_profile(data) == INPUT_PROFILE_SHA256:
        return digest
    raise ValueError("Adapter pinning requires the exact recorded reference kernel, a prepared kernel after the "
                     "fileport and workloop tools, or a kernel this tool produced from one of them")


def build(kernel, tools, output):
    original = read_component(kernel.parent, kernel.name, 256 << 20)
    input_sha256 = accepted_input(kernel, original)
    cdhash, adapter_sha256 = tools_adapter(tools)
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; choose a new kernel directory")
    own = not recorded_origin(kernel, input_sha256)
    candidate, offset, previous = pin(original, cdhash, None if own else FIXED_ENTRIES)
    output.mkdir(parents=True)
    destination = output / KERNEL_NAME
    destination.write_bytes(candidate)
    receipt = {
        "format": 1, "scope": SCOPE, "previous_cdhash": previous, "cdhash": cdhash, "offset": offset,
        "list_offset": LIST_OFFSET, "list_entries": ENTRIES, "adapter_slot": ADAPTER_SLOT,
        "input": str(kernel), "input_sha256": input_sha256,
        "origin_sha256": input_sha256 if own else INPUT_SHA256, "origin_profile_sha256": INPUT_PROFILE_SHA256,
        "output": str(destination), "output_sha256": hashlib.sha256(candidate).hexdigest(),
        "guest_tools": str(tools), "adapter_sha256": adapter_sha256,
        "kernel_instruction_bytes_changed": False, "all_other_kernel_bytes_equal": True,
        "guest_execution_verified": False,
    }
    (output / RECEIPT_NAME).write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    path = lambda value: Path(value).expanduser().resolve()
    parser.add_argument("--kernel", type=path, required=True, help="The reference kernelcache, a prepared kernel after the fileport and workloop tools, or one this tool produced")
    parser.add_argument("--tools", type=path, required=True, help="Guest-tools build whose adapter to pin")
    parser.add_argument("--output", type=path, required=True, help="New directory for the pinned kernelcache")
    args = parser.parse_args()
    print(json.dumps(build(args.kernel, args.tools, args.output), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error))
