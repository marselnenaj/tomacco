#!/usr/bin/env python3
"""Measure the agent observe/act/verify loop through Tomacco's MCP server, with the
same boundaries for every transport: one persistent `tomaccoctl mcp` process, one
tools/call per measured step, the time from request to reply.

Today only the offline fixture runtime (native/runtime/fixture.rs) can be measured
here, so every number this writes carries `evidence: "fixture"`: it is the cost of
Tomacco's own control plane (process, sockets, PNG, hierarchy, OCR) over a scripted
device, and never a device's performance. The same cases on a Simulator, a USB
iPhone, AirPlay, Android and the virtual iPhone are listed as pending in
`hardware_matrix` until they can be run on hardware with this same script
(`--device` and `--platform` select a real session; the measurement code is the same).

No number here compares Tomacco with another product: a head-to-head needs the
same device, app, build and settings on one host, measured by this script and the
other tool alike, which has not been done.

    python3 tools/benchmark_agent_loop.py --trials 100 --output qa/agent-loop.json
    python3 tools/benchmark_agent_loop.py --cases labeled_control,rotation_target --append

Failed trials are kept with their error under `failures`, never dropped."""
import argparse
import json
import os
import platform
import shutil
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests/python"))
from agent_fixture import Isolated, write_login_scenario, render  # noqa: E402
from robust_support import Session  # noqa: E402

# Cases whose trials are long (a bounded wait, a reconnect) run a share of --trials.
TRIAL_SHARE = {"animated_list": 0.25, "map_metal_sync": 0.5, "reconnect": 0.1}
CASES = ["labeled_control", "animated_list", "rotation_target", "deterministic_drag", "pinch",
         "map_metal_sync", "reconnect", "stale_frame_rejection"]
HARDWARE = {"ios_simulator": "pending: needs a Mac with idb reachable over SSH and a built app",
            "linux_virtual_iphone": "pending: needs the virtual iPhone running (ios.virtual.start) and the device argument virtual-iphone",
            "usb_iphone": "pending: needs a trusted iPhone with WebDriverAgent, blocked while hardware access is off",
            "airplay_hid": "pending: AirPlay captures no frame; only action acknowledgement can be timed",
            "android": "pending: needs an emulator or device with the scrcpy 4.1 server, blocked while hardware access is off",
            "desktop_runtime": "pending: the desktop's own frame timing is not read by this script"}


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int(round(p / 100 * len(ordered) + 0.5)))
    return ordered[min(rank, len(ordered)) - 1]


def summary(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
    return {"n": len(values), "p50_ms": round(statistics.median(values), 2), "p95_ms": round(percentile(values, 95), 2),
            "min_ms": round(min(values), 2), "max_ms": round(max(values), 2), "mean_ms": round(statistics.mean(values), 2)}


def proc_usage(pid):
    """Resident set and CPU seconds of a process from /proc, or None."""
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        rss = next(int(line.split()[1]) for line in status.splitlines() if line.startswith("VmRSS:"))
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        ticks = os.sysconf("SC_CLK_TCK")
        return {"rss_kib": rss, "cpu_seconds": round((int(fields[11]) + int(fields[12])) / ticks, 3)}
    except (OSError, StopIteration, IndexError, ValueError):
        return None


class Runner:
    def __init__(self, box, platform_name, device, trials):
        self.box = box
        self.platform = platform_name
        self.device = device
        self.requested = trials
        self.trials = trials
        self.mcp = Session(box.env)
        self.mcp.initialize()
        self.ident = 10
        self.session = None
        self.worker_pid = None

    def call(self, name, arguments):
        """One MCP tools/call: (ok, result or error, milliseconds, reply characters)."""
        self.ident += 1
        started = time.perf_counter_ns()
        reply = self.mcp.request(self.ident, "tools/call", {"name": name, "arguments": arguments}, timeout=120)
        elapsed = (time.perf_counter_ns() - started) / 1e6
        result = reply.get("result", {})
        text = next((c["text"] for c in result.get("content", []) if c.get("type") == "text"), "")
        value = json.loads(text) if text else {}
        ok = not result.get("isError", True)
        return ok, (value if ok else value.get("error", value)), elapsed, len(text)

    def start(self):
        ok, value, elapsed, _ = self.call("runtime.start", {"platform": self.platform, "device": self.device})
        assert ok, value
        self.session = value["session"]
        self.worker_pid = value.get("pid")
        return elapsed, value

    def stop(self):
        if self.session:
            self.call("runtime.stop", {"session": self.session})
            self.session = None

    def home(self):
        for _ in range(2):
            self.call("runtime.input", {"session": self.session, "action": "key", "key": "back"})
        ok, state, _, _ = self.call("runtime.state", {"session": self.session})
        if ok and state.get("width", 0) > state.get("height", 0):
            self.call("runtime.input", {"session": self.session, "action": "rotate", "orientation": "portrait"})

    # ---- cases: each answers (metrics dict of lists, failures list) over self.trials ----

    def case_labeled_control(self):
        m = {"read_request_to_result": [], "frame_age": [], "hierarchy_time": [], "ocr_time": [], "action_request_to_ack": [],
             "action_request_to_visible_postcondition": [], "full_observe_act_verify_loop": [], "observe_reply_chars": [],
             "observe_reply_utf8_bytes": [], "frame_image_bytes": [], "token_estimate_chars_div_4": []}
        failures = []
        have_ocr = bool(shutil.which("tesseract"))
        for trial in range(self.trials):
            self.home()
            loop_started = time.perf_counter_ns()
            ok, seen, read_ms, chars = self.call("runtime.observe", {"session": self.session})
            if not ok:
                failures.append({"trial": trial, "step": "observe", "error": seen})
                continue
            m["read_request_to_result"].append(read_ms)
            m["frame_age"].append(round((time.time() - seen["frame"]["captured_at"]) * 1000, 2))
            m["observe_reply_chars"].append(chars)
            m["observe_reply_utf8_bytes"].append(len(json.dumps(seen).encode()))
            m["frame_image_bytes"].append(seen["frame"]["bytes"])
            m["token_estimate_chars_div_4"].append(chars / 4)
            ok, marks, marks_ms, _ = self.call("screen.marks", {"device": self.session, "path": str(self.box.home / "bench-marks.png")})
            if not ok:
                failures.append({"trial": trial, "step": "marks", "error": marks})
                continue
            m["hierarchy_time"].append(marks_ms)
            if have_ocr and trial % 10 == 0:
                ok, text, ocr_ms, _ = self.call("screen.ocr", {"device": self.session, "text": "Sign in"})
                if ok:
                    m["ocr_time"].append(ocr_ms)
                else:
                    failures.append({"trial": trial, "step": "ocr", "error": text})
            button = next((x["mark"] for x in marks["marks"] if x.get("text") == "Sign in"), None)
            ok, ack, ack_ms, _ = self.call("runtime.input", {"session": self.session, "action": "key", "key": "back"})
            if ok:
                m["action_request_to_ack"].append(ack_ms)
            ok, batch, _, _ = self.call("runtime.actions", {"session": self.session, "steps": [
                {"action": "tap", "mark": button, "expect": {"text": "Loading", "timeout_ms": 5000, "interval_ms": 50}}],
                "frame": {"width": seen["frame"]["width"], "height": seen["frame"]["height"]}})
            if not ok or batch["steps"][0]["status"] != "verified":
                failures.append({"trial": trial, "step": "tap+expect", "error": batch})
                continue
            m["action_request_to_visible_postcondition"].append(batch["steps"][0]["verified_ms"])
            m["full_observe_act_verify_loop"].append((time.perf_counter_ns() - loop_started) / 1e6)
        return m, failures

    def case_animated_list(self):
        m = {"unstable_wait_bound": [], "stable_after_settle": []}
        failures = []
        for trial in range(self.trials):
            self.home()
            ok, _, _, _ = self.call("runtime.input", {"session": self.session, "action": "tap", "x": 0.5, "y": 0.64})
            started = time.perf_counter_ns()
            ok, value, ms, _ = self.call("screen.wait", {"device": self.session, "until": "stable", "timeout": 1, "interval_ms": 50, "stable_ms": 300})
            if ok or value.get("code") != "screen_unstable":
                failures.append({"trial": trial, "step": "unstable", "error": value})
                continue
            m["unstable_wait_bound"].append(ms)
            self.call("runtime.input", {"session": self.session, "action": "tap", "x": 0.5, "y": 0.5})
            ok, value, ms, _ = self.call("screen.wait", {"device": self.session, "until": "stable", "timeout": 5, "interval_ms": 50, "stable_ms": 200})
            if not ok:
                failures.append({"trial": trial, "step": "stable", "error": value})
                continue
            m["stable_after_settle"].append(ms)
        return m, failures

    def case_rotation_target(self):
        m = {"stale_rejection": [], "re_mark_after_rotation": [], "tap_after_re_mark": []}
        failures = []
        rejected = 0
        for trial in range(self.trials):
            self.home()
            ok, marks, _, _ = self.call("screen.marks", {"device": self.session, "path": str(self.box.home / "bench-marks.png")})
            self.call("runtime.input", {"session": self.session, "action": "rotate", "orientation": "landscape_left"})
            ok, value, ms, _ = self.call("screen.tap", {"device": self.session, "mark": 2})
            if ok or value.get("code") != "mark_stale":
                failures.append({"trial": trial, "step": "stale", "error": value})
            else:
                rejected += 1
                m["stale_rejection"].append(ms)
            ok, again, ms, _ = self.call("screen.marks", {"device": self.session, "path": str(self.box.home / "bench-marks.png")})
            if not ok:
                failures.append({"trial": trial, "step": "re-mark", "error": again})
                continue
            m["re_mark_after_rotation"].append(ms)
            ok, tapped, ms, _ = self.call("screen.tap", {"device": self.session, "mark": 1})
            if not ok or not tapped.get("verified"):
                failures.append({"trial": trial, "step": "tap", "error": tapped})
                continue
            m["tap_after_re_mark"].append(ms)
        m["_stale_taps_rejected"] = rejected
        return m, failures

    def case_deterministic_drag(self):
        m = {"trace_request_to_ack": [], "trace_timing_error_vs_requested": []}
        failures = []
        points = [{"x": 0.5, "y": 0.8 - i * 0.05, "t_ms": i * 25} for i in range(9)]
        for trial in range(self.trials):
            ok, value, ms, _ = self.call("runtime.gesture", {"session": self.session, "contacts": [{"points": points}]})
            if not ok:
                failures.append({"trial": trial, "step": "gesture", "error": value})
                continue
            m["trace_request_to_ack"].append(ms)
            if "elapsed_ms" in value:
                m["trace_timing_error_vs_requested"].append(abs(value["elapsed_ms"] - points[-1]["t_ms"]))
        return m, failures

    def case_pinch(self):
        m = {"pinch_request_to_ack": []}
        failures = []
        ok, state, _, _ = self.call("runtime.state", {"session": self.session})
        if not state.get("input_capabilities", {}).get("pinch"):
            return {"_unsupported": state.get("input_capabilities", {}).get("pinch")}, []
        for trial in range(self.trials):
            ok, value, ms, _ = self.call("runtime.input", {"session": self.session, "action": "pinch", "x": 0.5, "y": 0.5, "scale": 2, "duration": 0.1})
            if not ok:
                failures.append({"trial": trial, "step": "pinch", "error": value})
                continue
            m["pinch_request_to_ack"].append(ms)
        return m, failures

    def case_map_metal_sync(self):
        """The contract a map or Metal view must meet: after a pinch the next fresh frame
        differs from the one before, and a stable wait sees the camera settle. On the
        fixture this proves only the control plane; a real MapKit/Metal app is pending."""
        m = {"pinch_to_changed_frame": [], "settle_after_pinch": []}
        failures = []
        for trial in range(self.trials):
            ok, _, _, _ = self.call("runtime.input", {"session": self.session, "action": "key", "key": "back"})
            self.call("runtime.input", {"session": self.session, "action": "key", "key": "back"})
            self.call("runtime.input", {"session": self.session, "action": "tap", "x": 0.5, "y": 0.98})  # the map state
            ok, before, _, _ = self.call("runtime.observe", {"session": self.session, "context": False})
            started = time.perf_counter_ns()
            ok, value, _, _ = self.call("runtime.input", {"session": self.session, "action": "pinch", "x": 0.5, "y": 0.5, "scale": 2, "duration": 0.05})
            ok, after, _, _ = self.call("runtime.observe", {"session": self.session, "context": False})
            if not ok or after["frame"]["id"] == before["frame"]["id"]:
                failures.append({"trial": trial, "step": "pinch-frame", "error": {"before": before.get("frame", {}).get("id"), "after": after.get("frame", {}).get("id") if ok else after}})
                continue
            m["pinch_to_changed_frame"].append((time.perf_counter_ns() - started) / 1e6)
            ok, value, ms, _ = self.call("screen.wait", {"device": self.session, "until": "stable", "timeout": 5, "interval_ms": 50, "stable_ms": 150})
            if ok:
                m["settle_after_pinch"].append(ms)
            else:
                failures.append({"trial": trial, "step": "settle", "error": value})
        return m, failures

    def case_reconnect(self):
        m = {"stop_to_connected_again": [], "reuse_existing_session": []}
        failures = []
        for trial in range(self.trials):
            ok, value, ms, _ = self.call("runtime.start", {"platform": self.platform, "device": self.device})
            if not ok or not value.get("reused"):
                failures.append({"trial": trial, "step": "reuse", "error": value})
            else:
                m["reuse_existing_session"].append(ms)
            started = time.perf_counter_ns()
            self.stop()
            try:
                self.start()
                m["stop_to_connected_again"].append((time.perf_counter_ns() - started) / 1e6)
            except AssertionError as error:
                failures.append({"trial": trial, "step": "restart", "error": str(error)})
                self.start()
        return m, failures

    def case_stale_frame_rejection(self):
        m = {"refused_batch": []}
        failures = []
        refused = 0
        for trial in range(self.trials):
            ok, value, ms, _ = self.call("runtime.actions", {"session": self.session, "frame": {"width": 1, "height": 1},
                                                               "steps": [{"action": "tap", "x": 0.5, "y": 0.5}]})
            if ok or value.get("code") != "frame_stale":
                failures.append({"trial": trial, "step": "refuse", "error": value})
                continue
            refused += 1
            m["refused_batch"].append(ms)
        m["_batches_refused_without_input"] = refused
        return m, failures


def add_map_states(directory):
    """Adds a map screen to the login scenario: a tap near the bottom opens it, a pinch
    changes the frame (the camera moved) and the picture settles after a few frames."""
    render(directory / "map.png", (360, 640), "Map", shade=(200, 230, 200))
    render(directory / "zoomed1.png", (360, 640), "Map 2x", shade=(180, 220, 180))
    render(directory / "zoomed2.png", (360, 640), "Map 2x", shade=(170, 215, 170))
    scenario = json.loads((directory / "scenario.json").read_text())
    scenario["states"]["map"] = {"frame": "map.png", "keyboard": False, "focused": False, "pinch_to": "zooming", "key_to": {"back": "home"}}
    scenario["states"]["zooming"] = {"frame": "zoomed1.png", "keyboard": False, "focused": False, "key_to": {"back": "home"},
                                     "animated": {"frames": ["zoomed1.png"] + ["zoomed2.png"] * 7, "interval_ms": 0}}
    scenario["states"]["home"]["taps"].append({"left": 0, "top": 600, "right": 360, "bottom": 640, "to": "map", "after_ms": 0})
    (directory / "scenario.json").write_text(json.dumps(scenario))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument("--cases", default=",".join(CASES))
    parser.add_argument("--output", default=str(ROOT / "qa/agent-loop-benchmark.json"))
    parser.add_argument("--append", action="store_true", help="merge the cases into an existing output file")
    parser.add_argument("--platform", default="android")
    parser.add_argument("--device", default="fixture:login", help="fixture:NAME, or a real device once hardware runs are allowed")
    parser.add_argument("--latency-ms", type=int, default=0, help="simulated transport latency of the fixture")
    args = parser.parse_args()
    box = Isolated(prefix="bench-")
    if args.device.startswith("fixture:"):
        add_map_states(write_login_scenario(box.fixtures, focus_after_ms=100, latency_ms=args.latency_ms))
    evidence = "fixture" if args.device.startswith("fixture:") else "device"
    runner = Runner(box, args.platform, args.device, args.trials)
    output = Path(args.output)
    report = json.loads(output.read_text()) if args.append and output.exists() else {}
    report.update({
        "tool": "tools/benchmark_agent_loop.py",
        "evidence": evidence,
        "warning": "Fixture numbers are the control plane over a scripted device, not a device's performance. No head-to-head with another product was run." if evidence == "fixture" else "Device numbers; compare only with equal device, app, build and settings.",
        "boundaries": {"read_request_to_result": "MCP tools/call runtime.observe, request written to reply read, persistent server",
                       "frame_age": "now at reply minus frame.captured_at",
                       "hierarchy_time": "tools/call screen.marks on the session (capture + hierarchy + drawing)",
                       "ocr_time": "tools/call screen.ocr with text, every 10th trial, only with tesseract installed",
                       "action_request_to_ack": "tools/call runtime.input key, request to reply",
                       "action_request_to_visible_postcondition": "runtime.actions tap by mark with expect text: step verified_ms (hierarchy polled every 50 ms)",
                       "full_observe_act_verify_loop": "observe + marks + verified tap, wall time of the trial",
                       "cost": "reply characters and UTF-8 bytes of runtime.observe; token_estimate is chars/4, an estimate, not a tokenizer; frame_image_bytes is the PNG, reported apart"},
        "environment": {"platform": platform.platform(), "python": platform.python_version(), "cpus": os.cpu_count(),
                        "load_average_at_start": os.getloadavg(), "tomaccoctl": os.environ.get("TOMACCOCTL", ""),
                        "device": args.device, "transport_latency_ms": args.latency_ms, "trials_requested": args.trials,
                        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        "hardware_matrix": HARDWARE if evidence == "fixture" else report.get("hardware_matrix", {}),
        "reference_targets": {"note": "170 ms read and 70 ms tap are figures from a third-party screenshot of unknown boundaries, not Tomacco results and not a comparison"},
    })
    report.setdefault("cases", {})
    try:
        start_ms, state = runner.start()
        report["session"] = {"start_ms": round(start_ms, 2), "transport": state.get("transport"), "input_capabilities": state.get("input_capabilities")}
        for name in [c.strip() for c in args.cases.split(",") if c.strip()]:
            if name not in CASES:
                raise SystemExit(f"unknown case {name}; choose from {', '.join(CASES)}")
            before = proc_usage(runner.mcp.process.pid)
            started = time.perf_counter()
            runner.trials = max(1, int(runner.requested * TRIAL_SHARE.get(name, 1.0)))
            metrics, failures = getattr(runner, f"case_{name}")()
            after = proc_usage(runner.mcp.process.pid)
            summarised = {key: summary(values) for key, values in metrics.items() if isinstance(values, list)}
            extras = {key: values for key, values in metrics.items() if not isinstance(values, list)}
            trials = runner.trials
            report["cases"][name] = {"evidence": evidence, "trials": trials, "failed_trials": len(failures),
                                     "success_rate": round(1 - len(failures) / max(1, trials), 4),
                                     "wall_seconds": round(time.perf_counter() - started, 2), "metrics": summarised, **extras,
                                     "mcp_server_usage": {"before": before, "after": after}, "failures": failures[:50]}
            print(f"{name}: {trials} trials, {len(failures)} failed, {report['cases'][name]['wall_seconds']} s", flush=True)
    finally:
        runner.stop()
        runner.mcp.kill()
        trapped = box.trapped()
        box.close()
    # Every device helper was trapped; a non-empty log means a transport reached for one.
    report["trapped_helpers"] = trapped
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
