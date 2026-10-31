"""Local USB/WDA wire fixture. This does not emulate iOS or qualify hardware."""
import json
import os
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

root = Path(os.environ['TOMACCO_TEST_ROOT'])
(root/'usb-process.json').write_text(json.dumps({'pid':os.getpid(),'argv':sys.argv[1:]}))
ports = [int(value.split(':')[0]) for value in sys.argv[1:] if ':' in value]

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def do_GET(self):
        if self.server.server_port == ports[1]:
            self.send_response(200)
            self.send_header('Content-Type','multipart/x-mixed-replace; boundary=BoundaryString')
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b'--BoundaryString\r\nContent-Type: image/jpeg\r\nContent-Length: 4\r\n\r\n\xff\xd8\xff\xd9\r\n')
                    self.wfile.flush();time.sleep(.05)
            except (BrokenPipeError, ConnectionResetError): pass
        else: self.reply()

    def do_POST(self): self.reply()
    def do_DELETE(self): self.reply()

    def reply(self):
        body = self.rfile.read(int(self.headers.get('Content-Length','0')))
        body = json.loads(body) if body else None
        with (root/'wda-calls.jsonl').open('a') as log:
            log.write(json.dumps({'method':self.command,'path':self.path,'body':body})+'\n')
        value = None
        if self.path.endswith('/window/size'): value = {'width':390,'height':844}
        elif self.path.endswith('/screenshot'):
            value = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jD1sAAAAASUVORK5CYII='
        response = {'value':value}
        if self.path == '/session': response['sessionId'] = 'fixture-session'
        data = json.dumps(response).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers();self.wfile.write(data)

for port in ports:
    server = ThreadingHTTPServer(('127.0.0.1',port),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
while True: time.sleep(1)
