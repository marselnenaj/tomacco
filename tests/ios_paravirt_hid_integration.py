#!/usr/bin/env python3
"""Exercise USB HID lifecycle and refused input on the actual firmware-free engine."""
import argparse
import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time

from ios_dart_integration import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from run_ios_paravirt import QMP, file_digest, stop_child


def check_controller(engine, internal):
    with tempfile.TemporaryDirectory(prefix="tomacco-hid-check-") as temporary:
        root = Path(temporary)
        socket = root / "qmp.sock"
        controller = "qemu-xhci,id=usb,p2=2,p3=2,addr=1"
        if internal:
            controller += ",ports-non-removable=on"
        command = [str(engine), "-M", "virt,highmem=off", "-cpu", "cortex-a57", "-m", "128M",
                   "-S", "-nodefaults", "-display", "none", "-monitor", "none",
                   "-device", controller,
                   "-device", "tomacco-ios-hid,id=hid,bus=usb.0",
                   "-qtest", f"unix:{root}/qtest.sock,server=on,wait=off",
                   "-qtest-log", "/dev/null",
                   "-qmp", f"unix:{socket},server=on,wait=off"]
        def limits():
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        with (root / "host.log").open("wb") as log:
            child = subprocess.Popen(command, stdout=log, stderr=log,
                                     start_new_session=True, preexec_fn=limits)
            client = None
            test = None
            wire = None
            try:
                deadline = time.monotonic() + 10
                while not socket.exists() and child.poll() is None:
                    if time.monotonic() > deadline:
                        raise TimeoutError("Emulator did not create its monitor")
                    time.sleep(0.02)
                client = QMP(socket)
                test = connect(root / "qtest.sock", child)
                wire = test.makefile("rwb", buffering=0)
                def bus(command):
                    wire.write((command + "\n").encode())
                    reply = wire.readline().decode().strip().split()
                    assert reply and reply[0] == "OK", (command, reply)
                    return int(reply[1], 16) if len(reply) > 1 else None
                # The virt machine's low ECAM window, slot 1, function 0.
                # Assign the real PCI BAR; all checks below read guest MMIO.
                config = 0x3f000000 + (1 << 15)
                bar = 0x10000000
                assert bus(f"readl {config:#x}") not in (0, 0xffffffff)
                bus(f"writel {config + 0x10:#x} {bar:#x}")
                bus(f"writel {config + 0x14:#x} 0")
                bus(f"writew {config + 4:#x} 2")
                operational = bar + (bus(f"readl {bar:#x}") & 0xff)
                ports = (bus(f"readl {bar + 4:#x}") >> 24) & 0xff
                assert ports == 4, ports
                def check_ports():
                    for port in range(ports):
                        address = operational + 0x400 + port * 0x10
                        value = bus(f"readl {address:#x}")
                        assert bool(value & (1 << 30)) == internal, (port, hex(value))
                        # DR is read-only. Neither write can override the hardware topology.
                        for attempt in (0, 1 << 30):
                            bus(f"writel {address:#x} {attempt | (1 << 9):#x}")
                            assert bool(bus(f"readl {address:#x}") & (1 << 30)) == internal
                check_ports()
                bus(f"writel {operational:#x} 2")  # controller reset
                check_ports()
                path = "/machine/peripheral/hid"
                def get(name):
                    return client.call("qom-get", {"path": path, "property": name})
                for cycle in range(3):
                    for name in ("configured", "home-pending", "touch-pending", "touch-active",
                                 "home-reports-delivered", "touch-reports-delivered"):
                        assert get(name) == {"return": 0}, (cycle, name, get(name))
                    for name, value in (("home-press-ms", 0), ("home-press-ms", 1001),
                                        ("touch-state", 1 << 40), ("touch-state", 0x8000),
                                        ("configured", 1), ("home-press-ms", 100),
                                        ("touch-state", 1 << 32)):
                        rejected = client.call("qom-set", {"path": path, "property": name,
                                                           "value": value})
                        assert "error" in rejected, (name, value, rejected)
                    assert get("home-pending") == {"return": 0}
                    assert get("touch-pending") == {"return": 0}
                    assert client.call("device_del", {"id": "hid"}) == {"return": {}}
                    deadline = time.monotonic() + 3
                    while "error" not in get("configured"):
                        if time.monotonic() > deadline:
                            raise TimeoutError("USB device did not detach")
                        time.sleep(0.01)
                    check_ports()
                    assert client.call("device_add", {"driver": "tomacco-ios-hid",
                                                       "id": "hid", "bus": "usb.0"}) == {"return": {}}
                    check_ports()
                client.call("quit")
                assert child.wait(timeout=5) == 0
                return {"internal_ports": internal, "usb2_and_usb3_ports_checked": ports,
                        "removability_read_only": True, "reset_preserves_topology": True,
                        "hotplug_cycles": 3, "unconfigured_input_refused": True}
            except BaseException as error:
                raise RuntimeError(f"{error}\n{(root / 'host.log').read_text(errors='replace')}") from error
            finally:
                if client is not None:
                    client.close()
                if wire is not None:
                    wire.close()
                if test is not None:
                    test.close()
                stop_child(child)


def check(engine):
    return {"engine_sha256": file_digest(engine), "guest_booted": False,
            "controllers": [check_controller(engine, internal) for internal in (False, True)]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.engine.resolve()), indent=2))
