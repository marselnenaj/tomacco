#!/usr/bin/env python3
"""Time pointer-authentication instructions on a built engine (tests/engine/pac-bench.S).

Runs the bare-metal benchmark on QEMU's virt machine (-cpu max, one CPU, TCG)
for each engine given, --runs times, and prints the best nanoseconds per
iteration of every phase. Needs clang and llvm-objcopy. --engine-arg adds an
engine argument (repeatable, e.g. --engine-arg=-global
--engine-arg=max-arm-cpu.tomacco-pauth-fast=off turns the engine's helper-free
bypass off for an A/B on one binary).
"""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tests/engine/pac-bench.S"
ITERATIONS = 2_000_000
PHASES = ("call", "pacret", "pacaut", "data", "pacga")


def build(directory):
    clang, objcopy = shutil.which("clang"), shutil.which("llvm-objcopy")
    if not clang or not objcopy:
        raise SystemExit("Needs clang and llvm-objcopy")
    obj, image = Path(directory) / "bench.o", Path(directory) / "bench.bin"
    subprocess.run([clang, "--target=aarch64-none-elf", "-march=armv8.3-a", "-c", SOURCE, "-o", obj],
                   check=True, timeout=60)
    subprocess.run([objcopy, "-O", "binary", "--only-section=.text", obj, image], check=True, timeout=60)
    return image


def run(engine, image, extra):
    done = subprocess.run(
        [str(engine), "-M", "virt", "-cpu", "max", "-smp", "1", "-accel", "tcg", "-m", "128M",
         "-nodefaults", "-nic", "none", "-display", "none", "-monitor", "none", "-serial", "stdio",
         "-device", f"loader,file={image},addr=0x40200000,force-raw=on",
         "-device", "loader,addr=0x40200000,cpu-num=0", *extra],
        capture_output=True, text=True, timeout=600)
    values = {name: int(value, 16) for name, value in re.findall(r"([a-z]+)=([0-9a-f]{16})", done.stdout)}
    if set(values) != {"cntfrq", "sum", *PHASES}:
        raise SystemExit(f"{engine}: benchmark printed {done.stdout!r} {done.stderr[-500:]!r}")
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engines", nargs="+", type=Path)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--engine-arg", action="append", default=[])
    args = parser.parse_args()
    extra = args.engine_arg
    report = {}
    with tempfile.TemporaryDirectory(prefix="tomacco-pac-bench-") as directory:
        image = build(directory)
        for engine in args.engines:
            best, sums = {}, set()
            for _ in range(args.runs):
                values = run(engine, image, extra)
                sums.add(values["sum"])
                for phase in PHASES:
                    ns = values[phase] * 1e9 / values["cntfrq"] / ITERATIONS
                    best[phase] = min(best.get(phase, ns), ns)
            report[str(engine)] = {"ns_per_iteration": {k: round(v, 2) for k, v in best.items()},
                                   "sum": sorted(f"{s:016x}" for s in sums)}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
