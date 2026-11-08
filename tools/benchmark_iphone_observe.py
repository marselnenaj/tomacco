#!/usr/bin/env python3
"""Observe latency against a delayed loopback WDA double, never hardware.

TOMACCOCTL selects the release binary. Each call captures a new screenshot and
reads the three context fields; delay_ms applies only to the context endpoints.
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--delay-ms", type=int, default=40)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=ROOT,
                        help="Tomacco source checkout containing the offline test helpers")
    args = parser.parse_args()
    if not 1 <= args.trials <= 100 or not 0 <= args.delay_ms <= 1000:
        parser.error("trials must be 1..100 and delay-ms 0..1000")
    source = args.source_root.expanduser().resolve()
    helpers = ("tests/python/iphone_observe_fixture.py", "tests/python/agent_fixture.py",
               "tests/native_client.py", "tests/host_patience.py", "tests/fixtures/iphone_observe.py")
    if any(not (source / helper).is_file() for helper in helpers):
        parser.error("This benchmark requires a Tomacco source checkout with offline test helpers; "
                     "pass --source-root /path/to/tomacco.")
    sys.path[:0] = [str(source / "tests/python"), str(source / "tests")]
    from iphone_observe_fixture import CONTEXT, ObserveDouble
    from native_client import binary

    double = ObserveDouble()
    samples = []
    try:
        double.observe()  # Warm geometry and transport setup, excluded.
        first = len(double.events())
        for trial in range(args.trials):
            double.plan(delay_ms=args.delay_ms, revision=trial + 1, keyboard=bool(trial % 2))
            start = time.perf_counter_ns()
            seen = double.observe()
            samples.append((time.perf_counter_ns() - start) / 1e6)
            assert seen["app"]["name"] == str(trial + 1), seen
            assert seen["keyboard"]["visible"] == bool(trial % 2), seen
            assert seen["focus"]["focused"] is True, seen
            assert seen["frame"]["source"] == "screenshot", seen
        events = double.events()[first:]
        starts = [event for event in events if event["event"] == "start"]
        for route in CONTEXT | {"/screenshot"}:
            assert sum(e["route"] == route for e in starts) == args.trials, (route, starts)
        active = peak = 0
        for event in events:
            if event["route"] in CONTEXT:
                active += 1 if event["event"] == "start" else -1
                peak = max(peak, active)
        double.assert_safe()
        path = Path(binary()).resolve()
        result = {"scope": "loopback WDA double; no physical iPhone or USB",
                  "boundary": "fresh runtime.observe CLI request through result, including process startup, screenshot and context",
                  "binary": str(path), "binary_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                  "trials": args.trials, "context_endpoint_delay_ms": args.delay_ms,
                  "samples_ms": samples, "p50_ms": statistics.median(samples),
                  "p95_ms": sorted(samples)[max(0, int(len(samples) * .95) - 1)],
                  "peak_context_requests_in_flight": peak,
                  "screenshots": args.trials, "context_requests": args.trials * 3,
                  "fresh_revisions_verified": args.trials, "unexpected_helper_calls": double.trapped(),
                  "limitations": ["The double handles HTTP requests concurrently; actual WDA may serialize XCTest work.",
                                  "No hardware performance, cancellation latency, or competitor claim."]}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({k: v for k, v in result.items() if k != "samples_ms"}, indent=2))
    finally:
        double.close()


if __name__ == "__main__":
    main()
