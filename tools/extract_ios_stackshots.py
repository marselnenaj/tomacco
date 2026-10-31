#!/usr/bin/env python3
"""Recover exact, checksummed guest stackshots from a research console log.

The producer is research-observer.c. Output is Apple's original KCData, suitable
for XNU's tools/lldbmacros/kcdata.py; no process state or backtrace is invented.
Incomplete, duplicated, reordered or corrupted serial records are rejected.
"""
import argparse
import binascii
import hashlib
import json
from pathlib import Path
import re
import struct

MAX_LOG = 32 * 1024 * 1024
MAX_CAPTURE = 1024 * 1024
MAX_TOTAL = 2 * 1024 * 1024
BOUNDARY = re.compile(r"TOMACCO_STACKSHOT_(BEGIN|END) tick=([0-9]+) pid=([0-9]+) bytes=([0-9]+) crc32=([0-9a-f]{8})")
CHUNK = re.compile(r"TOMACCO_STACKSHOT_DATA tick=([0-9]+) pid=([0-9]+) offset=([0-9]+) hex=([0-9a-f]{2,512})")
SUMMARY = re.compile(r"TOMACCO_PROCESS sample_tick=([0-9]+) selected=([0-9]+) captured=([0-9]+) failed=([0-9]+) finished=1")
BOOT_STAGE = re.compile(r"TOMACCO_PROCESS tick=([0-9]{1,3}) exclaves_boot_stage=([0-9]{1,10})")
BOOT_ERROR = re.compile(r"TOMACCO_PROCESS tick=([0-9]{1,3}) exclaves_boot_stage_result=(-?[0-9]{1,10}) errno=([0-9]{1,10}) bytes=([0-9]{1,20})")
BOOT_UNAVAILABLE = re.compile(r"TOMACCO_PROCESS tick=([0-9]{1,3}) exclaves_boot_stage_symbol_unavailable")


def validate_kcdata(data):
    """Validate framing only; interpretation belongs to a versioned decoder."""
    offset, containers = 0, []
    if len(data) < 32 or struct.unpack_from("<I", data)[0] != 0x59A25807:
        raise ValueError("Capture is not an uncompressed Apple stackshot")
    while offset + 16 <= len(data):
        kind, size, flags = struct.unpack_from("<IIQ", data, offset)
        offset += 16
        if size > len(data) - offset:
            raise ValueError("Truncated KCData item")
        if kind == 0x59A25807 and (offset != 16 or size):
            raise ValueError("Invalid or nested KCData buffer")
        if kind == 0x13:
            if size < 4 or len(containers) >= 64:
                raise ValueError("Truncated KCData container")
            containers.append(flags)
        elif kind == 0x14:
            if not containers or containers.pop() != flags:
                raise ValueError("Mismatched KCData container")
        elif kind == 0xF19158ED:
            if containers or size or offset != len(data):
                raise ValueError("Incomplete or trailing KCData")
            return
        offset += size
    raise ValueError("Missing KCData end marker")


def parse_log(raw):
    if len(raw) > MAX_LOG:
        raise ValueError("Console log exceeds 32 MiB")
    records, current, seen, total, rounds = [], None, set(), 0, set()
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line.startswith(("TOMACCO_STACKSHOT_BEGIN", "TOMACCO_STACKSHOT_END")):
            match = BOUNDARY.fullmatch(line)
            if not match:
                raise ValueError("Malformed stackshot boundary")
            kind, tick, pid, size, crc = match.groups()
            key, size = (int(tick), int(pid)), int(size)
            if key[0] not in (60, 120) or key[1] > 0x7fffffff or not 32 <= size <= MAX_CAPTURE:
                raise ValueError("Stackshot identity or size exceeds the producer contract")
            if kind == "BEGIN":
                if (current is not None or key in seen or key[0] in rounds or
                        total + size > MAX_TOTAL or len(seen) >= 18):
                    raise ValueError("Duplicate, overlapping or oversized stackshots")
                current = {"tick": key[0], "pid": key[1], "bytes": size, "crc32": crc, "data": bytearray()}
                seen.add(key)
                total += size
            else:
                if current is None or (current["tick"], current["pid"]) != key:
                    raise ValueError("Unmatched stackshot end")
                if size != current["bytes"] or crc != current["crc32"] or len(current["data"]) != size:
                    raise ValueError("Incomplete stackshot or conflicting boundaries")
                data = bytes(current["data"])
                if f"{binascii.crc32(data):08x}" != crc:
                    raise ValueError("Stackshot CRC32 mismatch")
                validate_kcdata(data)
                current["data"] = data
                records.append(current)
                current = None
        elif line.startswith("TOMACCO_STACKSHOT_DATA"):
            match = CHUNK.fullmatch(line)
            if not match or current is None:
                raise ValueError("Malformed or orphaned stackshot chunk")
            tick, pid, offset, encoded = match.groups()
            if (int(tick), int(pid)) != (current["tick"], current["pid"]) or int(offset) != len(current["data"]):
                raise ValueError("Stackshot chunk identity or sequence mismatch")
            decoded = bytes.fromhex(encoded)
            if len(current["data"]) + len(decoded) > current["bytes"]:
                raise ValueError("Stackshot chunk exceeds its declared size")
            current["data"].extend(decoded)
        elif line.startswith("TOMACCO_STACKSHOT_REJECTED"):
            raise ValueError("Guest rejected a stackshot; the observation is incomplete")
        elif line.startswith("TOMACCO_PROCESS sample_tick=") and " selected=" in line:
            match = SUMMARY.fullmatch(line)
            if not match:
                raise ValueError("Malformed observation summary")
            tick, selected, captured, failed = map(int, match.groups())
            actual = sum(record["tick"] == tick for record in records)
            if (current is not None or tick not in (60, 120) or tick in rounds or failed or
                    not 1 <= selected <= 9 or selected != captured or captured != actual):
                raise ValueError("Incomplete or failed observation; stackshot counts do not match")
            rounds.add(tick)
    if current is not None:
        raise ValueError("Truncated stackshot; capture did not complete")
    if not records:
        raise ValueError("No complete guest stackshots; inspect TOMACCO_PROCESS diagnostics")
    if {record["tick"] for record in records} != rounds:
        raise ValueError("Missing observation summary; the serial capture may be truncated")
    return records


def boot_stages(raw, ticks):
    """Keep actual sysctl results; absence on older observers is not success."""
    if len(raw) > MAX_LOG:
        raise ValueError("Console log exceeds 32 MiB")
    observations = {}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.startswith("TOMACCO_PROCESS ") or "exclaves_boot_stage" not in line:
            continue
        match = BOOT_STAGE.fullmatch(line)
        error = BOOT_ERROR.fullmatch(line)
        unavailable = BOOT_UNAVAILABLE.fullmatch(line)
        if match:
            tick, value = map(int, match.groups())
            if value > 0xffffffff:
                raise ValueError("Exclave boot stage exceeds uint32")
            record = {"tick": tick, "value": value, "result": 0}
        elif error:
            tick, result, errno, size = map(int, error.groups())
            if result not in (-1, 0) or errno > 0x7fffffff or size > 0xffffffffffffffff or (result == 0 and (size == 4 or errno)):
                raise ValueError("Inconsistent Exclave boot-stage error")
            record = {"tick": tick, "result": result, "errno": errno, "bytes": size}
        elif unavailable:
            tick = int(unavailable[1])
            record = {"tick": tick, "error": "symbol_unavailable"}
        else:
            raise ValueError("Malformed Exclave boot-stage record")
        if tick not in ticks or tick in observations:
            raise ValueError("Duplicate or unmatched Exclave boot-stage observation")
        observations[tick] = record
    return [observations[tick] for tick in sorted(observations)]


def extract(source, output):
    if not source.is_file():
        raise ValueError("Console input must be a regular file")
    with source.open("rb") as handle:
        raw = handle.read(MAX_LOG + 1)
    records = parse_log(raw)
    ticks = sorted({record["tick"] for record in records})
    stages = boot_stages(raw, ticks)
    # Parse and validate the entire input before creating any output files.
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; choose a new directory")
    output.mkdir(mode=0o700, parents=True)
    manifest = {"format": 1, "scope": "guest-kernel-stackshots", "source": str(source.resolve()),
                "source_sha256": hashlib.sha256(raw).hexdigest(), "captures": [],
                "observation_ticks": ticks, "boot_stage_observations": stages,
                "both_observations_completed": ticks == [60, 120],
                "graphical_apps_qualified": False}
    for capture in records:
        data = capture.pop("data")
        name = f"tick-{capture['tick']}-pid-{capture['pid']}.kcdata"
        with (output / name).open("xb") as handle:
            handle.write(data)
        (output / name).chmod(0o600)
        manifest["captures"].append({**capture, "path": name, "sha256": hashlib.sha256(data).hexdigest()})
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--console", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(extract(args.console, args.output), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error))
