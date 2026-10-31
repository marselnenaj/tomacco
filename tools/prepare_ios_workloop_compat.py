#!/usr/bin/env python3
"""Build a private, version-pinned kernel that keeps an owner discovery from
waking the discovered owner's own sync wait (see ios27-workloop-discover.S).

It changes one instruction of filt_wlupdate into a branch to an
adapter in live RX padding; every other kernel byte stays equal to the exact
recorded input. Building it is not evidence that a guest runs better.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time

from ios_paravirt_firmware import read_component
from ios_paravirt_kernel import assemble, branch, kernel_profile

INPUT_SHA256 = "9f9f4fe33b3de53ed0a1c7a4fa9b6977e1401893a1a0c8c08e79373863c57e81"
# kernel_profile of a kernel prepare_ios_fileport_compat.py derived
# from a user's own prepared kernel.
INPUT_PROFILE_SHA256 = "336a95e24ecf360d20a5c09f8579ea7255ca3fcdee9fb8f2fd59b9807902201c"
# filt_wlupdate: `ldrb w9, [x27, #24]; tbnz w9, #7, discover` after the debounce
# compare, and the compare's ESTALE path (`mov x22, #0`, ... `mov w25, #70`).
ENTRY, STALE, CAVE, CAVE_BYTES = 0x21a58bc, 0x21a587c, 0x1ca3900, 128
ENTRY_BYTES = bytes.fromhex("6963403969143837")
STALE_BYTES = bytes.fromhex("160080d21c0080520a008052")
STALE_ERROR = (0x21a589c, bytes.fromhex("d9088052"))  # mov w25, #70 (ESTALE)
SOURCE = Path(__file__).resolve().parents[1] / "integrations/ios-emulator/ios27-workloop-discover.S"


def patch(original, code):
    """Return `original` with the adapter installed; refuse any other layout."""
    if (original[ENTRY:ENTRY + len(ENTRY_BYTES)] != ENTRY_BYTES
            or original[STALE:STALE + len(STALE_BYTES)] != STALE_BYTES
            or original[STALE_ERROR[0]:STALE_ERROR[0] + 4] != STALE_ERROR[1]
            or original[CAVE:CAVE + CAVE_BYTES] != bytes(CAVE_BYTES)):
        raise ValueError("filt_wlupdate, its ESTALE path or the reserved RX cave differs")
    if not 32 <= len(code) <= CAVE_BYTES:
        raise ValueError("Unexpected assembled adapter geometry")
    candidate = bytearray(original)
    candidate[CAVE:CAVE + len(code)] = code
    candidate[ENTRY:ENTRY + 4] = branch(ENTRY, CAVE)
    restored = bytearray(candidate)
    restored[CAVE:CAVE + len(code)] = original[CAVE:CAVE + len(code)]
    restored[ENTRY:ENTRY + 4] = original[ENTRY:ENTRY + 4]
    if restored != original:
        raise ValueError("Kernel bytes outside the patched intervals changed")
    return bytes(candidate)


def build(kernel, output):
    original = read_component(kernel.parent, kernel.name, 256 << 20)
    input_sha256 = hashlib.sha256(original).hexdigest()
    if input_sha256 != INPUT_SHA256 and kernel_profile(original) != INPUT_PROFILE_SHA256:
        raise ValueError("The workloop adapter requires the exact recorded input kernel or a kernel "
                         "prepare_ios_fileport_compat.py derived from a prepared kernel")
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; choose a new private directory")
    output.mkdir(parents=True)
    source_bytes = SOURCE.read_bytes()
    code = assemble(output, "workloop-discover", source_bytes.decode(), CAVE, ENTRY + 4,
                    time.monotonic() + 120, success=STALE)
    candidate = patch(original, code)
    destination = output / "kernelcache.research.vphone600"
    destination.write_bytes(candidate)
    receipt = {
        "format": 1, "scope": "private-exact-workloop-discover", "input": str(kernel),
        "input_sha256": input_sha256, "output": str(destination),
        "output_sha256": hashlib.sha256(candidate).hexdigest(),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "adapter_sha256": hashlib.sha256(code).hexdigest(), "adapter_bytes": len(code),
        "entry_file_offset": hex(ENTRY), "cave_file_offset": hex(CAVE), "stale_path_file_offset": hex(STALE),
        "matched_fflags": "SYNC_WAKE|DISCOVER_OWNER|IGNORE_ESTALE", "operation": "FILT_WLTOUCH",
        "knote_state": "SYNC_WAIT without SYNC_WAKE", "result": "ESTALE (dropped by IGNORE_ESTALE)",
        "handoff_wakes_unchanged": True, "other_kernel_bytes_unchanged": True,
        "offline_kernel_modified": True, "running_guest_modified": False,
        "guest_execution_verified": False,
    }
    (output / "workloop-policy.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=lambda value: Path(value).expanduser().absolute(), required=True)
    parser.add_argument("--output", type=lambda value: Path(value).expanduser().absolute(), required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.kernel, args.output), indent=2))


if __name__ == "__main__":
    main()
