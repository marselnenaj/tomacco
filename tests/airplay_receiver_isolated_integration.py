#!/usr/bin/env python3
"""The AirPlay receiver session of a source-built tomaccoctl, with no hardware.

Runs `runtime.start` for device `airplay` with a stand-in `uxplay` that records
its arguments, in a throwaway HOME, XDG directories, TOMACCO_HOME and TMPDIR.
The system and session D-Bus addresses point at nothing, so BlueZ is never
reached and no Bluetooth advertisement goes out; every device or host tool a
session could call (adb, ssh, bluetoothctl, idevice*, ...) is a trap that
records the call and fails. Checks:

- UxPlay gets the bounded audio queue as -as, keeps -vsync no and -vrtp;
- a UxPlay startup file that picks the sink keeps it (no -as);
- without Bluetooth the session advertises no input, and input answers why;
- no trap was called.

    python3 tests/airplay_receiver_isolated_integration.py [path/to/tomaccoctl]
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRAPS = ["adb", "fastboot", "ssh", "scp", "bluetoothctl", "btmgmt", "hcitool", "hciconfig",
         "idevice_id", "ideviceinfo", "idevicepair", "iproxy", "pymobiledevice3", "tidevice",
         "go-ios", "xcrun", "hyprctl", "tomacco", "notify-send", "systemctl", "pactl", "wpctl"]


def main() -> int:
    ctl = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "target" / "debug" / "tomaccoctl").resolve()
    if not ctl.is_file():
        sys.exit(f"{ctl} is missing: build it with cargo build --offline --locked")
    failures = []
    with tempfile.TemporaryDirectory(prefix="tomacco-airplay-") as tmp:
        tmp = Path(tmp)
        home, bin_ = tmp / "home", tmp / "bin"
        for d in (home, bin_, tmp / "tomacco", tmp / "tmp", tmp / "run"):
            d.mkdir(mode=0o700)
        trap_log = tmp / "traps.log"
        for name in TRAPS:
            (bin_ / name).write_text(f"#!/bin/sh\necho \"{name} $*\" >> '{trap_log}'\nexit 97\n")
            (bin_ / name).chmod(0o755)
        argv_file = tmp / "uxplay.argv"
        (bin_ / "uxplay").write_text(
            "#!/bin/sh\n"
            f"for a in \"$@\"; do printf '%s\\0' \"$a\"; done > '{argv_file}.part'\n"
            f"mv '{argv_file}.part' '{argv_file}'\n"
            "echo 'Initialized server socket(s)'\nexec sleep 60\n")
        (bin_ / "uxplay").chmod(0o755)
        env = {
            "HOME": str(home), "PATH": f"{bin_}:/usr/bin:/bin", "TMPDIR": str(tmp / "tmp"),
            "TOMACCO_HOME": str(tmp / "tomacco"), "XDG_RUNTIME_DIR": str(tmp / "run"),
            "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
            "XDG_STATE_HOME": str(home / ".local/state"), "XDG_CACHE_HOME": str(home / ".cache"),
            "DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={tmp}/no-system-bus",
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={tmp}/no-session-bus",
            "LANG": "C.UTF-8",
        }

        def call(op, args, rc=None):
            e = dict(env)
            if rc:
                e["UXPLAYRC"] = str(rc)
            out = subprocess.run([str(ctl), "call", op, json.dumps(args)], env=e, capture_output=True, text=True, timeout=60)
            try:
                return json.loads(out.stdout)
            except json.JSONDecodeError:
                return {"ok": False, "raw": out.stdout + out.stderr, "exit": out.returncode}

        def result(answer):
            return answer.get("result", answer)

        def session(rc=None):
            if argv_file.exists():
                argv_file.unlink()
            started = call("runtime.start", {"platform": "ios", "device": "airplay"}, rc)
            sid = result(started).get("session")
            if not sid:
                sys.exit(f"runtime.start failed: {json.dumps(started)[:2000]}")
            state = {}
            for _ in range(100):
                state = result(call("runtime.state", {"session": sid}))
                if state.get("status") in ("connected", "failed") and argv_file.exists():
                    break
                time.sleep(0.1)
            argv = argv_file.read_text().split("\0")[:-1] if argv_file.exists() else []
            return sid, state, argv

        report = {}
        sid, state, argv = session()
        option = lambda name: argv[argv.index(name) + 1] if name in argv else None  # noqa: E731
        hover = call("runtime.input", {"session": sid, "action": "touch", "phase": "hover", "x": 0.5, "y": 0.5})
        down = call("runtime.input", {"session": sid, "action": "touch", "phase": "down", "x": 0.5, "y": 0.5})
        report["default"] = {"status": state.get("status"), "bluetooth": state.get("bluetooth"),
                             "audio_backlog_ms": state.get("audio_backlog_ms"),
                             "input_capabilities": {k: state.get("input_capabilities", {}).get(k) for k in ("live_touch", "max_contacts", "keys", "text", "fill")},
                             "uxplay": {"-as": option("-as"), "-vsync": option("-vsync"), "-vrtp": option("-vrtp"), "-n": option("-n"), "-s": option("-s")},
                             "hover": result(hover), "down": down.get("error", down)}
        call("runtime.stop", {"session": sid})
        d = report["default"]
        if d["status"] != "connected":
            failures.append(f"session status {d['status']}")
        if not (d["uxplay"]["-as"] or "").endswith("leaky=downstream ! autoaudiosink") or "max-size-time=100000000" not in d["uxplay"]["-as"]:
            failures.append(f"uxplay -as is {d['uxplay']['-as']!r}")
        if d["uxplay"]["-vsync"] != "no" or not (d["uxplay"]["-vrtp"] or "").endswith("tcpclientsink host=127.0.0.1 port=" + d["uxplay"]["-vrtp"].rsplit("=", 1)[-1]):
            failures.append("uxplay video options changed")
        if d["audio_backlog_ms"] != 100:
            failures.append("audio_backlog_ms is not 100")
        if (d["bluetooth"] or {}).get("status") != "unavailable":
            failures.append(f"bluetooth is {d['bluetooth']}")
        if d["input_capabilities"]["live_touch"] is not False or d["input_capabilities"]["keys"] != []:
            failures.append("input is advertised without Bluetooth")
        if d["hover"].get("delivered") is not False:
            failures.append(f"hover answered {d['hover']}")
        if "Control needs Bluetooth" not in json.dumps(d["down"]):
            failures.append(f"down answered {d['down']}")

        rc = tmp / "uxplayrc"
        rc.write_text("# the person's own sink\nas pipewiresink\n")
        sid, state, argv = session(rc)
        report["uxplayrc_sink"] = {"status": state.get("status"), "audio_backlog_ms": state.get("audio_backlog_ms"), "has_-as": "-as" in argv}
        call("runtime.stop", {"session": sid})
        if report["uxplayrc_sink"]["has_-as"] or report["uxplayrc_sink"]["audio_backlog_ms"] is not None:
            failures.append("a UxPlay startup file's sink was overridden")

        report["traps"] = trap_log.read_text().splitlines() if trap_log.exists() else []
        if report["traps"]:
            failures.append("a hardware or host tool was called")
        report["failures"] = failures
        print(json.dumps(report, indent=2))
    for f in failures:
        print("FAIL", f, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
