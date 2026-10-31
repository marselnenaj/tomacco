#!/usr/bin/env python3
"""Exercise the real VirtIO input monitor without firmware or guest boot claims."""
import argparse
import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from run_ios_paravirt import QMP, file_digest, stop_child


def check(engine):
    with tempfile.TemporaryDirectory(prefix="tomacco-input-check-") as temporary:
        root = Path(temporary)
        socket = root / "qmp.sock"
        command = [str(engine), "-M", "virt", "-cpu", "cortex-a57", "-m", "128M",
                   "-S", "-nodefaults", "-display", "none", "-monitor", "none",
                   "-device", "virtio-keyboard-pci,id=tomacco-keyboard",
                   "-device", "virtio-tablet-pci,id=tomacco-tablet",
                   "-device", "virtio-rng-pci,id=tomacco-entropy",
                   "-qmp", f"unix:{socket},server=on,wait=off"]
        def limits():
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        with (root / "host.log").open("wb") as log:
            child = subprocess.Popen(command, stdout=log, stderr=log,
                                     start_new_session=True, preexec_fn=limits)
            client = None
            try:
                deadline = time.monotonic() + 10
                while not socket.exists() and child.poll() is None:
                    if time.monotonic() > deadline:
                        raise TimeoutError("Emulator did not create its monitor")
                    time.sleep(0.02)
                client = QMP(socket)
                devices = client.call("x-query-virtio")["return"]
                assert len(devices) == 3, devices
                statuses = []
                for device in devices:
                    for _ in range(2):
                        status = client.call("x-query-virtio-status", {"path": device["path"]})["return"]
                        assert status["backend-features"] == {"transports": [], "dev-features": []}, status
                        assert not status["broken"], status
                    statuses.append({"path": device["path"], "device_id": status["device-id"],
                                     "backend_features": status["backend-features"]})
                rejected = client.call("x-query-virtio-status", {"path": "/machine/no-such-device"})
                assert "error" in rejected, rejected
                assert client.call("query-status")["return"]["status"] == "prelaunch"
                mice = client.call("query-mice")["return"]
                assert len(mice) == 1 and mice[0]["absolute"], mice
                client.call("quit")
                assert child.wait(timeout=5) == 0
                return {"engine_sha256": file_digest(engine), "devices": statuses,
                        "absolute_pointer": True, "guest_booted": False}
            except BaseException as error:
                raise RuntimeError(f"{error}\n{(root / 'host.log').read_text(errors='replace')}") from error
            finally:
                if client is not None:
                    client.close()
                stop_child(child)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.engine.resolve()), indent=2))
