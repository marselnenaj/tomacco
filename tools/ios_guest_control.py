"""Local virtio-vsock host transport. No IP interface or external connection."""
import hashlib
import os
from pathlib import Path
import stat
import socket
import subprocess
import time


def local_control_preflight(directory):
    """Check required Unix bind/listen operations before any emulator starts.

    The caller supplies its owned fresh temporary directory. No client
    connects, no backend protocol is consumed, and an existing socket is never
    unlinked to manufacture availability.
    """
    # Shorter than every real endpoint name, so the check does not introduce
    # an artificial Unix address-length refusal for an otherwise valid path.
    path = Path(directory) / ".probe"
    created = False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.bind(str(path))
            created = True
            stream.listen(1)
    finally:
        if created:
            path.unlink(missing_ok=True)
    return {"family": "AF_UNIX", "bind_verified": True, "listen_verified": True,
            "local_only": True, "backend_verified": False}


class GuestControl:
    def __init__(self, executable, directory, output, child_setup):
        self.executable = Path(executable).resolve(strict=True)
        if not self.executable.is_file() or not os.access(self.executable, os.X_OK):
            raise ValueError("Guest control backend must be an executable regular file")
        self.directory, self.output, self.child_setup = Path(directory), Path(output), child_setup
        self.process = self.log = None

    def start(self):
        self.log = (self.output / "guest-control.log").open("xb")
        socket = self.directory / "guest-vhost.sock"
        try:
            self.process = subprocess.Popen([
                str(self.executable), "--guest-cid", "3", "--socket", str(socket),
                "--uds-path", str(self.directory / "guest.sock")],
                stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
                start_new_session=True, preexec_fn=self.child_setup)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("Guest control backend exited during startup; inspect guest-control.log")
                if socket.exists():
                    metadata = socket.lstat()
                    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid():
                        raise ValueError("Guest control backend created an invalid socket")
                    return self
                time.sleep(0.025)
            raise TimeoutError("Guest control backend did not publish its socket")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.log is not None:
            self.log.close()

    def qemu_arguments(self, memory):
        # QEMU's shared RAM backend uses an fd with QEMU_VMALLOC_ALIGN.
        # Plain memory-backend-memfd only guarantees host-page alignment;
        # on 4 KiB Linux hosts it breaks the GPU's 16 KiB guest-page aliases.
        return ["-object", f"memory-backend-ram,id=tomacco-guest-memory,size={memory},share=on",
                "-chardev", "socket,id=tomacco-guest-control,path=" + str(self.directory / "guest-vhost.sock"),
                "-device", "vhost-user-vsock-pci,chardev=tomacco-guest-control,disable-legacy=on"]

    def receipt(self):
        with self.executable.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return {"protocol": "tomacco-vsock-json-v1", "socket": str(self.directory / "guest.sock"),
                "port": 1339, "local_only": True, "guest_cid": 3, "host_cid": 2,
                "backend": str(self.executable), "backend_sha256": digest,
                "guest_service_verified": False}
