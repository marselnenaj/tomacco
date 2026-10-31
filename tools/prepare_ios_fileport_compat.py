#!/usr/bin/env python3
"""Build a private, version-pinned kernel with Biome fileport compatibility.

This explicitly changes one original MAC refusal for one exact platform
program in an owned virtual image. It never changes a running guest, native
fileport checks, SEP policy, other syscalls or other program identities.
Building it is not evidence that the service stopped crashing.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time

from ios_paravirt_firmware import read_component
from ios_paravirt_kernel import assemble, branch, kernel_profile, CSPROC_BLOB_GET

INPUT_SHA256 = "87b7e47e2ef2e6cd40f7f7b3c36157b8fbff8b14c5ecfe8746a7e03fee10e615"
# kernel_profile of what ios.emulator.prepare_cryptexes prepares with graphics,
# installer control and development tools.
INPUT_PROFILE_SHA256 = "349b6b8501c2a9307c5ec83adff56ee35af082cdc29d3a173d33703ec14cf2ae"
SERVICE_CDHASH = "35894cb7aa80ae1643476e44b7811456cdd952c1"
ENTRY, CAVE, CAVE_BYTES = 0x2339374, 0x1ca3800, 512
GATE_BYTES = bytes.fromhex("f90300aae0f9ff35c7ffff17")
GET_BLOB_BYTES = bytes.fromhex("7f2303d5f657bda9f44f01a9fd7b02a9fd830091")
SOURCE = Path(__file__).resolve().parents[1] / "integrations/ios-emulator/ios-biome-fileport.S"


def build(kernel, output):
    original = read_component(kernel.parent, kernel.name, 256 << 20)
    input_sha256 = hashlib.sha256(original).hexdigest()
    if input_sha256 != INPUT_SHA256 and kernel_profile(original) != INPUT_PROFILE_SHA256:
        raise ValueError("The fileport adapter requires the exact recorded input kernel or a kernel "
                         "prepared by ios.emulator.prepare_cryptexes with graphics, installer_control "
                         "and development_tools")
    if (original[ENTRY:ENTRY + len(GATE_BYTES)] != GATE_BYTES or
            original[CSPROC_BLOB_GET:CSPROC_BLOB_GET + len(GET_BLOB_BYTES)] != GET_BLOB_BYTES or
            original[CAVE:CAVE + CAVE_BYTES] != bytes(CAVE_BYTES)):
        raise ValueError("Original MAC gate, code identity getter or reserved RX cave differs")
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; choose a new private directory")
    output.mkdir(parents=True)
    source_bytes = SOURCE.read_bytes()
    source = source_bytes.decode()
    source += "\n.p2align 3\nbiome_code_hash:\n.byte "
    source += ",".join(str(value) for value in bytes.fromhex(SERVICE_CDHASH)) + "\n"
    code = assemble(output, "biome-fileport", source, CAVE, ENTRY + 4,
                    time.monotonic() + 120,
                    calls={"original_csproc_get_blob": CSPROC_BLOB_GET})
    if not 128 <= len(code) <= CAVE_BYTES or code[-20:] != bytes.fromhex(SERVICE_CDHASH):
        raise ValueError("Unexpected assembled adapter geometry or code identity")
    candidate = bytearray(original)
    candidate[CAVE:CAVE + len(code)] = code
    candidate[ENTRY:ENTRY + 4] = branch(ENTRY, CAVE)
    # Preserve every byte outside the two captured patch intervals.
    restored = bytearray(candidate)
    restored[CAVE:CAVE + len(code)] = original[CAVE:CAVE + len(code)]
    restored[ENTRY:ENTRY + 4] = original[ENTRY:ENTRY + 4]
    if restored != original:
        raise ValueError("Kernel bytes outside the patched intervals changed")
    destination = output / "kernelcache.research.vphone600"
    destination.write_bytes(candidate)
    receipt = {
        "format": 1, "scope": "private-exact-original-biome-fileport", "input": str(kernel),
        "input_sha256": input_sha256, "output": str(destination),
        "output_sha256": hashlib.sha256(candidate).hexdigest(),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "adapter_sha256": hashlib.sha256(code).hexdigest(), "adapter_bytes": len(code),
        "entry_file_offset": hex(ENTRY), "cave_file_offset": hex(CAVE),
        "allowed_cdhash": SERVICE_CDHASH, "required_flags": "0x24000001",
        "effective_uid": 501, "syscall": 430, "tolerated_native_error": 1,
        "original_mac_check_executed": True, "native_fileport_handler_retained": True,
        "other_kernel_bytes_unchanged": True, "offline_kernel_modified": True,
        "running_guest_modified": False,
        "guest_execution_verified": False, "crash_fix_qualified": False,
        "production_ready": False, "tests_added_or_run": False,
    }
    (output / "fileport-policy.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=lambda value: Path(value).expanduser().absolute(), required=True)
    parser.add_argument("--output", type=lambda value: Path(value).expanduser().absolute(), required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.kernel, args.output), indent=2))


if __name__ == "__main__":
    main()
