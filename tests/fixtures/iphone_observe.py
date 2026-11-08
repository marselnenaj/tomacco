"""Loopback-only WDA double for observe contracts and roundtrip measurements.

Started instead of iproxy, inside agent_fixture.Isolated's private environment.
It does not forward USB, discover devices, or emulate iOS execution.
"""
import json
import os
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

root = Path(os.environ["TOMACCO_TEST_ROOT"])
ports = [int(value.split(":")[0]) for value in sys.argv[1:] if ":" in value]
lock = threading.Lock()
sequence = 0
CONTEXT = {"/wda/activeAppInfo", "/elements", "/element/active"}
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jD1sAAAAASUVORK5CYII="


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.server.server_port == ports[1]:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=BoundaryString")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b"--BoundaryString\r\nContent-Type: image/jpeg\r\nContent-Length: 4\r\n\r\n\xff\xd8\xff\xd9\r\n")
                    self.wfile.flush()
                    time.sleep(.05)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.reply()

    def do_POST(self):
        self.reply()

    def do_DELETE(self):
        self.reply()

    def reply(self):
        global sequence
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.loads(body) if body else None
        route = self.path.removeprefix("/session/fixture-session")
        plan = json.loads((root / "plan.json").read_text())
        with lock:
            sequence += 1
            ident = sequence
            self.record({"event": "start", "id": ident, "method": self.command,
                         "route": route, "body": body, "revision": plan.get("revision", 0)})
        if route in CONTEXT:
            time.sleep(plan.get("delay_ms", 0) / 1000)
        if route == "/window/size":
            value = {"width": 390, "height": 844}
        elif route == "/orientation":
            value = "PORTRAIT"
        elif route == "/wda/activeAppInfo":
            value = {"bundleId": "dev.example.app", "name": str(plan.get("revision", 0)), "pid": 123}
        elif route == "/elements":
            value = [{"ELEMENT": "keyboard"}] if plan.get("keyboard", False) else []
        elif route == "/element/active":
            value = {"ELEMENT": "field"}
        elif route == "/screenshot":
            value = PNG
        else:
            value = None
        fault = plan.get("faults", {}).get(route)
        status = 200
        if fault:
            status = fault.get("status", 500)
            value = {"error": fault["error"], "message": fault["error"]}
        response = {"value": value}
        if self.path == "/session":
            response["sessionId"] = "fixture-session"
        data = json.dumps(response).encode()
        with lock:
            self.record({"event": "end", "id": ident, "route": route})
        if fault and fault.get("disconnect"):
            self.close_connection = True
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def record(self, value):
        value["at_ns"] = time.monotonic_ns()
        with (root / "wda-events.jsonl").open("a") as log:
            log.write(json.dumps(value) + "\n")


for port in ports:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
while True:
    time.sleep(1)
