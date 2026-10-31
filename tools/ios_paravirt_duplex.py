"""Concurrent, bounded endpoint transactions on the private virtual USB wire.

Enumeration remains sequential. Once configured, one pending bulk-IN request
must not prevent bulk-OUT traffic from a second restored/ASR/FDR connection.
Only the reader thread consumes frames; each endpoint has one outstanding call.
"""
import socket
import threading
import time

from ios_paravirt_usb import USBConnection, USBStall, request_frame


class DuplexUSBConnection(USBConnection):
    def __init__(self, enumerated):
        super().__init__(enumerated.stream, enumerated.deadline)
        self.sent, self.received = enumerated.sent, enumerated.received
        self.received_bytes = enumerated.received_bytes
        self.events = list(enumerated.events)
        self.lock, self.writer = threading.Lock(), threading.Lock()
        self.pending = {}
        self.error = None
        self.reader = threading.Thread(target=self._receive, name="vusb-responses", daemon=True)
        self.reader.start()

    def _fail(self, error):
        with self.lock:
            if self.error is None:
                self.error = error
            for call in self.pending.values():
                call["error"] = self.error
                call["ready"].set()
            self.pending.clear()
        try:
            self.stream.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _receive(self):
        try:
            while True:
                kind, endpoint, data = self.response()
                with self.lock:
                    if kind == 3:
                        self.events.append({"kind": kind, "endpoint": endpoint})
                        self.events = self.events[-64:]
                        continue
                    call = self.pending.pop(endpoint, None)
                    if call is None:
                        raise ValueError("Unsolicited virtual USB completion")
                    if kind == 2:
                        call["error"] = USBStall(f"Guest stalled endpoint {endpoint:#04x}")
                    elif kind != 1:
                        call["error"] = ValueError("Unexpected bulk USB completion")
                    else:
                        call["data"] = data
                    call["ready"].set()
        except (OSError, EOFError, ValueError) as error:
            self._fail(error)

    def exchange(self, kind, endpoint=0, payload=b"", transfer_length=None, expected=1):
        if kind != 1 or not endpoint & 15 or expected != 1:
            raise ValueError("Concurrent USB supports configured bulk endpoints only")
        frame = request_frame(kind, endpoint, payload, transfer_length)
        call = {"ready": threading.Event()}
        with self.lock:
            if self.error:
                raise self.error
            if endpoint in self.pending:
                raise ValueError("A USB transfer is already pending on this endpoint")
            self.pending[endpoint] = call
        try:
            with self.writer:
                self._timeout()
                self.stream.sendall(frame)
                self.sent += 1
            if not call["ready"].wait(max(0, self.deadline - time.monotonic())):
                raise TimeoutError("Virtual USB operation deadline exceeded")
            if "error" in call:
                raise call["error"]
            return call["data"]
        except (OSError, EOFError, ValueError) as error:
            self._fail(error)
            raise

    def close(self):
        self._fail(EOFError("Virtual USB session closed"))
        self.reader.join(timeout=2)
