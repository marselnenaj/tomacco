#!/usr/bin/env python3
"""lldb to a process of the virtual iPhone: a TCP port on this host's loopback,
joined to the guest's debugserver over vsock.

The guest's control service listens on vsock port 1343 and hands the connection
to /usr/libexec/debugserver with --attach (guest-host-channel.h). This process
holds the loopback listener and copies bytes both ways for one debugger session.
"""
import argparse
import json
from pathlib import Path
import selectors
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ios_hot_install import DEBUG_PORT, connect_port  # noqa: E402

BUFFER_BYTES = 1 << 16


def pump(left, right, deadline=None):
    """Copy between two connected sockets until either side ends. Returns the
    bytes each way."""
    selector = selectors.DefaultSelector()
    left.setblocking(False)
    right.setblocking(False)
    selector.register(left, selectors.EVENT_READ, right)
    selector.register(right, selectors.EVENT_READ, left)
    counts = {left: 0, right: 0}
    try:
        while deadline is None or time.monotonic() < deadline:
            for key, _ in selector.select(timeout=1):
                try:
                    data = key.fileobj.recv(BUFFER_BYTES)
                except BlockingIOError:
                    continue
                except OSError:
                    return counts[left], counts[right]
                if not data:
                    return counts[left], counts[right]
                target = key.data
                target.setblocking(True)
                target.sendall(data)
                target.setblocking(False)
                counts[key.fileobj] += len(data)
    finally:
        selector.close()
    return counts[left], counts[right]


def guest_call(cli, job, operation, argument):
    done = subprocess.run([cli, "call", "ios.emulator.guest",
                           json.dumps({"job_id": job, "operation": operation, "argument": argument,
                                       "timeout_seconds": 60})],
                          capture_output=True, text=True, timeout=120)
    reply = json.loads(done.stdout) if done.stdout.strip() else {}
    response = (reply.get("result") or {}).get("response") or {}
    if response.get("ok") is not True:
        raise RuntimeError(f"debug_server {argument}: "
                           + str(response.get("error") or reply.get("error") or done.stderr[:300]))
    return response


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--job", required=True, help="The running guest's job")
    parser.add_argument("--socket", required=True, help="The guest's vsock Unix socket")
    parser.add_argument("--target", required=True, help="PID or process name in the guest")
    parser.add_argument("--port", type=int, default=0, help="Loopback TCP port; 0 picks a free one")
    parser.add_argument("--seconds", type=int, default=3600, help="Longest a session lasts")
    args = parser.parse_args()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Loopback only: a debugger is arbitrary code in the guest.
    listener.bind(("127.0.0.1", args.port))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        response = guest_call(args.cli, args.job, "debug_server", args.target)
        print(json.dumps({"event": "listening", "host": "127.0.0.1", "port": port, "target": args.target,
                          "helper_pid": response.get("helper_pid"),
                          "lldb": f"process connect connect://127.0.0.1:{port}"}), flush=True)
        # The guest helper waits two minutes for this connection.
        listener.settimeout(110)
        client, _ = listener.accept()
        guest = connect_port(args.socket, DEBUG_PORT, 15)
        to_guest, to_debugger = pump(client, guest, time.monotonic() + args.seconds)
        print(json.dumps({"event": "ended", "bytes_to_guest": to_guest, "bytes_to_debugger": to_debugger}), flush=True)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"event": "failed", "error": str(error)}), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
