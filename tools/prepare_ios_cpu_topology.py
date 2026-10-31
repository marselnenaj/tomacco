#!/usr/bin/env python3
"""Create a new AP CPU topology from an original and prepared vphone600 ADT.

Preserve the prepared identity, NVRAM, GPU, SEP and all non-CPU nodes. Validate
existing CPU nodes against the original preparation contract before changing
count. Neither input is modified and the output must not already exist.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat

from prepare_ios_paravirt import MAX_AP_CPU_COUNT, MAX_TREE_BYTES, ap_cpu_count, decode, prepare


def read_tree(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_TREE_BYTES:
            raise ValueError("Device tree must be a regular file of at most 16 MiB")
        data = stream.read(MAX_TREE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
            (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or len(data) != before.st_size):
        raise ValueError("Device tree changed during its read")
    return data


def cpu_topology(original_data, prepared_data, requested):
    if type(requested) is not int or not 1 <= requested <= MAX_AP_CPU_COUNT:
        raise ValueError(f"AP CPU count must be an integer between 1 and {MAX_AP_CPU_COUNT}")
    current = decode(prepared_data)
    previous = ap_cpu_count(current)
    chosen = current.child("chosen")
    memory = chosen.get("dram-size")
    if len(memory) != 8 or int.from_bytes(memory, "little") % (1 << 30):
        raise ValueError("Prepared memory geometry must be a whole GiB")
    # The original preparer validates all 32 affinity-ordered CPU nodes and
    # handles firmware-derived properties. Only its CPU nodes are consumed;
    # none of its newly prepared identity or peripheral nodes are copied.
    reference = decode(prepare(
        original_data,
        memory_gib=int.from_bytes(memory, "little") >> 30,
        ap_cpus=max(previous, requested),
        seed=chosen.get("random-seed"),
        nvram=chosen.get("nvram-proxy-data"),
    ))
    cpus = current.child("cpus")
    expected = reference.child("cpus").children
    for index, node in enumerate(cpus.children):
        if node.encode() != expected[index].encode():
            raise ValueError(f"Prepared CPU{index} differs from the original preparation contract")
    # Encoding changes only this node's child count and its validated children.
    # A new output retains every other byte, including property alignment bytes.
    non_cpu = [node.encode() for node in current.children if node.name != "cpus"]
    root_properties = b"".join(prop.encode(name) for name, prop in current.props.items())
    parent_properties = b"".join(prop.encode(name) for name, prop in cpus.props.items())
    cpus.children = expected[:requested]
    data = current.encode()
    candidate = decode(data)
    if (ap_cpu_count(candidate) != requested
            or b"".join(prop.encode(name) for name, prop in candidate.props.items()) != root_properties
            or b"".join(prop.encode(name) for name, prop in candidate.child("cpus").props.items()) != parent_properties
            or non_cpu != [node.encode() for node in candidate.children if node.name != "cpus"]):
        raise ValueError("CPU topology did not preserve the prepared platform")
    return data, previous


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original_device_tree", type=Path)
    parser.add_argument("prepared_device_tree", type=Path)
    parser.add_argument("output", type=Path, help="New file; never overwrites an existing tree")
    parser.add_argument("--ap-cpus", type=int, required=True,
                        help=f"AP CPUs exposed to iOS, 1–{MAX_AP_CPU_COUNT}; SEP remains separate")
    args = parser.parse_args()
    original = read_tree(args.original_device_tree)
    prepared = read_tree(args.prepared_device_tree)
    data, previous = cpu_topology(original, prepared, args.ap_cpus)
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(data)
    print(json.dumps({
        "format": 1,
        "device_tree": str(args.output.resolve()),
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "prepared_input_sha256": hashlib.sha256(prepared).hexdigest(),
        "output_sha256": hashlib.sha256(data).hexdigest(),
        "ap_cpus_before": previous,
        "ap_cpus": args.ap_cpus,
        "prior_retained_cpu_nodes_identical": True,
        "non_cpu_nodes_identical": True,
        "identity_nvram_gpu_sep_preserved": True,
        "emulator_started": False,
        "native_runtime_qualified": False,
    }, indent=2))


if __name__ == "__main__":
    main()
