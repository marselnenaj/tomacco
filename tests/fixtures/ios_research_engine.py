#!/usr/bin/env python3
"""QMP/serial control fixture. Contains no firmware and does not emulate iOS."""
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

if sys.argv[1:] == ["--version"]:
    print("Test-only research protocol fixture")
    sys.exit(0)
if sys.argv[1:] == ["-machine", "help"]:
    print("darwin  Test-only machine")
    sys.exit(0)
if sys.argv[1:] == ["-machine", "darwin,help"]:
    print("boot-display=<string>\nboot-media=<bool>\nboot-storage=<string>\nresearch-dart=<bool>\nresearch-mcc=<bool>\nresearch-ptd=<bool>\nresearch-cl4=<string>\nexclave-integrity=<string>\nexclave-trustcache=<string>")
    sys.exit(0)
if sys.argv[1:] == ["-trace", "help"]:
    if not (Path(os.environ["TOMACCO_TEST_ROOT"]) / "no-bus-trace").exists():
        print("tomacco_arm_bus_fault\ntomacco_mcc_mmio\ntomacco_ptd_mmio")
    if not (Path(os.environ["TOMACCO_TEST_ROOT"]) / "no-sysreg-trace").exists():
        print("tomacco_arm_sysreg_fault")
    sys.exit(0)

root = Path(os.environ["TOMACCO_TEST_ROOT"])
(root / "engine.pid").write_text(str(os.getpid()))
(root / "engine-argv.json").write_text(json.dumps(sys.argv))
listeners = []
def listen(path):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    listeners.append(server)
    return server

channels = []
debug_server = None
debug_state = {"pc": 0x1000000, "status": "prelaunch", "running": False}
for index, arg in enumerate(sys.argv):
    if arg == "-chardev":
        path = sys.argv[index + 1].split("path=", 1)[1].rsplit(",server=", 1)[0].replace(",,", ",")
        server = listen(path)
        if "id=pmp-debug" in sys.argv[index + 1]:
            debug_server = server
        else:
            channels.append(server)

def debugger_fixture():
    """RSP protocol fixture only: no ARM instructions are executed here."""
    client = debug_server.accept()[0]
    breakpoint = 0x1000000
    def reply(text):
        data = text.encode()
        packet = b"$" + data + f"#{sum(data) & 255:02x}".encode()
        # Force the native reader to handle arbitrary transport fragmentation.
        for offset in range(0, len(packet), 3):
            client.sendall(packet[offset:offset + 3])
            time.sleep(.001)
    while True:
        byte = client.recv(1)
        if not byte:
            return
        if byte == b"+":
            continue
        assert byte == b"$"
        data = b""
        while (byte := client.recv(1)) != b"#":
            if not byte or len(data) > 4096:
                return
            data += byte
        checksum = client.recv(2, socket.MSG_WAITALL)
        assert int(checksum, 16) == sum(data) & 255
        command = data.decode()
        with (root / "debug-commands.jsonl").open("a") as file:
            file.write(json.dumps(command) + "\n")
        client.sendall(b"+")
        if command.startswith("qSupported:"):
            reply("PacketSize=1000;hwbreak+")
        elif command == "?":
            reply("T05thread:1;")
        elif command.startswith("Z1,"):
            breakpoint = int(command.split(",")[1], 16)
            reply("E22" if (root / "reject-breakpoint").exists() else "OK")
        elif command == "c":
            debug_state.update(status="running", running=True)
            if not (root / "hold-breakpoint").exists():
                override = root / "breakpoint-pc"
                debug_state.update(pc=int(override.read_text(), 16) if override.exists() else breakpoint,
                                   status="debug", running=False)
                reply("T05thread:1;")
        else:
            reply("")

if debug_server:
    threading.Thread(target=debugger_fixture, daemon=True).start()
qmp = sys.argv[sys.argv.index("-qmp") + 1].removeprefix("unix:").split(",server=", 1)[0].replace(",,", ",")
peer = listen(qmp).accept()[0]
peer.sendall(b'{"QMP":{"version":{"qemu":{"major":1,"minor":0,"micro":0}},"capabilities":[]}}\n')
serials = []
for raw in peer.makefile("rb"):
    command = json.loads(raw)
    name = command["execute"]
    result = {}
    if name == "cont":
        debug_state.update(status="running", running=True)
        serials = [server.accept()[0] for server in channels]
        bus_faults = root / "bus-fault-fixture.log"
        if "enable=tomacco_arm_bus_fault" in sys.argv and bus_faults.exists():
            for part in bus_faults.read_bytes().split(b"|SPLIT|"):
                os.write(2, part)
                time.sleep(.01)
        if (root / "serial-burst").exists():
            # QEMU can hold its control lock while writing a guest UART. The
            # client must keep draining it even while awaiting a QMP reply.
            for channel in serials[:3]:
                channel.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
                channel.sendall(b"SERIAL_BURST\n" * 32768)
        if (root / "serial-overflow").exists():
            serials[0].setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            serials[0].sendall(b"overflow" * (1024 * 1024 + 128))
        console = root / "console-fixture.log"
        if console.exists():
            # Force fragmented lines across reader calls, including the panic prefix.
            for part in console.read_bytes().split(b"|SPLIT|"):
                serials[0].sendall(part)
                time.sleep(.02)
        data = root / "platform-fixture.log"
        if data.exists():
            serials[3].sendall(data.read_bytes())
    elif name == "query-status":
        result = {key: debug_state[key] for key in ("status", "running")}
    elif name == "query-cpus-fast":
        result = [{"cpu-index":0,"qom-path":"/machine/test-cpu"}]
    elif name == "stop":
        debug_state.update(status="paused", running=False)
    elif name == "human-monitor-command":
        result = (f"PC={debug_state['pc']:016x} X00=ffffffffcc810080 SP=00000000011ffff0 PSTATE=400003c5"
                  if debug_server else "PC=0000000000001000 X00=0000000000000000")
    elif name == "qom-get":
        if command["arguments"]["property"] == "pmp-tlbi-alias":
            assert command["arguments"]["path"] == "/machine"
            result = not (root / "wrong-pmp-tlbi").exists()
        elif command["arguments"]["property"] == "tomacco-strict-sysregs":
            assert command["arguments"]["path"] == "/machine/test-cpu"
            result = not (root / "wrong-cpu-policy").exists()
        elif command["arguments"]["property"] in ("midr", "mp-affinity"):
            assert command["arguments"]["path"] == "/machine/test-cpu"
            values = dict(item.split("=",1) for item in sys.argv[sys.argv.index("-cpu")+1].split(",")[1:])
            result = int(values[command["arguments"]["property"]],16)
            if (root / "wrong-cpu-identity").exists():
                result ^= 1
        else:
            assert command["arguments"] == {"path": "/machine/asc", "property": "controller-state"}
            fixture = root / "controller-snapshot.json"
            result = fixture.read_text() if fixture.exists() else "{}"
    elif name == "screendump":
        if (root / "fail-frame").exists():
            peer.sendall((json.dumps({"error": {"class": "GenericError", "desc": "fixture capture failure"}, "id": command["id"]}) + "\n").encode())
            continue
        # Explicit protocol fixture, never a real guest display.
        Path(command["arguments"]["filename"]).write_bytes(b"TEST FRAME")
    elif name == "pmemsave":
        assert not debug_state["running"]
        assert command["arguments"]["val"] == 0x1000000 and command["arguments"]["size"] == 2097152
        # Deterministic synthetic bytes, never firmware or a real RAM capture.
        data = bytes(range(256)) * 8192
        if (root / "short-memory-capture").exists():
            data = data[:-1]
        Path(command["arguments"]["filename"]).write_bytes(data)
    peer.sendall((json.dumps({"return": result, "id": command["id"]}) + "\n").encode())
    if name == "quit":
        if not (root / "omit-flush").exists():
            print("TOMACCO_BOOT_STORAGE_FLUSH bytes=16384 msync=0 fsync=0", flush=True)
        break
