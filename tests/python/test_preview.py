"""preview.* operations against a stand-in desktop on the preview socket."""
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_client import StudioError, dispatch


def png(width, height, rgba):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    rows = b"".join(b"\x00" + bytes(rgba) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def png_size(path):
    data = Path(path).read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n"), path
    return struct.unpack(">II", data[16:24])


class FakePreview:
    """Answers the desktop's preview socket protocol: one JSON line in, one out."""

    def __init__(self, directory, session="main"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / f"preview-{session}.sock"
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(self.path))
        self.server.listen(16)
        self.requests = []
        self.state = {"device": "18pro", "orientation": "portrait", "unfolded": False, "url": "http://127.0.0.1:1/", "loading": False, "error": ""}
        self.answers = {}
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            with connection:
                request = json.loads(connection.makefile("rb").readline())
                method, params = request["method"], request["params"]
                self.requests.append((method, params))
                try:
                    result = self.answer(method, params)
                    reply = {"id": request["id"], "result": result}
                except Exception as error:  # reported to the caller as the desktop would
                    reply = {"id": request["id"], "error": {"code": "preview_error", "message": str(error)}}
                connection.sendall(json.dumps(reply).encode() + b"\n")

    def answer(self, method, params):
        if method in self.answers:
            return self.answers[method](params)
        if method == "state":
            return dict(self.state)
        if method == "configure":
            self.state.update({k: v for k, v in params.items() if k in ("device", "orientation", "unfolded")})
            return dict(self.state)
        if method == "navigate":
            self.state["url"] = params["url"]
            return dict(self.state)
        if method == "capture":
            portrait = self.state["orientation"] == "portrait"
            size = (40, 80) if portrait else (80, 40)
            if self.state["device"].startswith("pixel"):
                size = (size[0] * 2, size[1] * 2)
            Path(params["path"]).write_bytes(png(*size, (200, 30, 30, 255)))
            return {"path": params["path"], "width": size[0], "height": size[1]}
        if method == "reload":
            return {"reloading": True}
        if method == "errors":
            return {"clean": True, "failed_requests": []}
        if method == "evaluate":
            if "issues" in params["script"]:
                return {"issues": [], "url": self.state["url"]}
            return True
        raise ValueError("unexpected " + method)

    def close(self):
        self.server.close()
        self.path.unlink(missing_ok=True)


class PreviewCase(unittest.TestCase):
    def setUp(self):
        # A short data directory keeps the socket path under the 104-byte limit.
        self.temp = tempfile.TemporaryDirectory(prefix="tp-", dir="/tmp")
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"TOMACCO_HOME": str(self.root / "s"), "TOMACCO_TEST_ROOT": str(self.root)})
        self.env.start()
        self.preview = FakePreview(self.root / "s" / "runtime")

    def tearDown(self):
        self.preview.close()
        self.env.stop()
        self.temp.cleanup()


class Matrix(PreviewCase):
    def test_matrix_writes_a_labelled_contact_sheet_of_every_capture(self):
        out = self.root / "matrix"
        result = dispatch("preview.matrix", {"device_ids": ["18pro", "pixel-11"], "directory": str(out), "folded_views": False})
        self.assertEqual(len(result["results"]), 4)
        sheet = result["contact_sheet"]
        self.assertEqual(Path(sheet["path"]), out / "contact-sheet.png")
        width, height = png_size(sheet["path"])
        self.assertEqual((width, height), (sheet["width"], sheet["height"]))
        # Four captures in one row (40, 80, 80 and 160 wide; cells at least 200), each with two label lines.
        self.assertEqual(width, 32 * 2 + 4 * 200 + 3 * 32)
        self.assertEqual(height, 32 * 2 + 160 + 12 + 2 * 20)
        manifest = json.loads((out / "matrix.json").read_text())
        self.assertEqual(manifest["contact_sheet"]["path"], sheet["path"])
        # The session is put back as it was.
        self.assertEqual(self.preview.requests[-1][0], "configure")
        self.assertEqual(self.preview.requests[-1][1]["device"], "18pro")

    def test_the_contact_sheet_can_be_left_out(self):
        out = self.root / "plain"
        result = dispatch("preview.matrix", {"device_ids": ["18pro"], "orientations": ["portrait"], "directory": str(out), "contact_sheet": False})
        self.assertNotIn("contact_sheet", result)
        self.assertFalse((out / "contact-sheet.png").exists())
        self.assertTrue((out / "18pro-portrait.png").is_file())


class Emulation(PreviewCase):
    def test_media_and_network_settings_reach_the_desktop_as_given(self):
        dispatch("preview.configure", {"color_scheme": "dark", "reduced_motion": "reduce", "network": {"preset": "slow-3g", "latency_ms": 100}})
        method, params = self.preview.requests[-1]
        self.assertEqual(method, "configure")
        self.assertEqual(params, {"color_scheme": "dark", "reduced_motion": "reduce", "network": {"preset": "slow-3g", "latency_ms": 100}})

    def test_bad_media_and_network_settings_are_refused_before_the_desktop(self):
        for arguments in ({"color_scheme": "blue"}, {"reduced_motion": "less"}, {"network": {"preset": "5g"}},
                          {"network": {"latency_ms": -1}}, {"network": {"jitter": 1}}):
            with self.assertRaises(StudioError) as caught:
                dispatch("preview.configure", arguments)
            self.assertEqual(caught.exception.code, "invalid_argument", arguments)
        self.assertEqual(self.preview.requests, [])


class Errors(PreviewCase):
    def test_errors_report_the_current_load_or_reload_first(self):
        self.assertEqual(dispatch("preview.errors", {}), {"clean": True, "failed_requests": []})
        self.assertEqual([m for m, _ in self.preview.requests], ["errors"])
        self.preview.requests.clear()
        dispatch("preview.errors", {"reload": True, "wait_for": "window.ready", "timeout": 5})
        methods = [m for m, _ in self.preview.requests]
        self.assertEqual(methods[0], "reload")
        self.assertEqual(methods[-1], "errors")
        self.assertIn("Boolean(window.ready)", [p.get("script") for _, p in self.preview.requests])

    def test_a_page_that_failed_to_load_is_reported_after_a_reload(self):
        self.preview.state["error"] = "Page not reachable"
        self.assertEqual(dispatch("preview.errors", {"reload": True})["clean"], True)
        self.assertEqual(self.preview.requests[-1][0], "errors")


if __name__ == "__main__":
    unittest.main()
