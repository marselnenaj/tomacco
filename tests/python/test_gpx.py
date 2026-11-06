"""GPX routes through the compiled CLI: inspection, refusals, and playback as a job
on a fake emulator console (an adb stand-in that records every geo fix), with
pause, resume, status and stop. No device, USB or network is reached."""
import stat
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_fixture import Isolated  # noqa: E402

TRACK = """<?xml version="1.0"?>
<gpx version="1.1" creator="test"><metadata><name>Ring</name></metadata><trk><trkseg>
<trkpt lat="48.2082" lon="16.3738"><ele>171</ele><time>2026-05-01T10:00:00Z</time></trkpt>
<trkpt lat="48.2092" lon="16.3748"><time>2026-05-01T10:00:00.2Z</time></trkpt>
<trkpt lat="48.2102" lon="16.3758"><time>2026-05-01T10:00:00.4Z</time></trkpt>
<trkpt lat="48.2112" lon="16.3768"><time>2026-05-01T10:00:00.6Z</time></trkpt>
</trkseg></trk></gpx>
"""


class GpxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.box = Isolated()
        cls.route = cls.box.home / "ride.gpx"
        cls.route.write_text(TRACK)
        # An adb stand-in: emulator console commands are recorded and answered OK.
        cls.console_log = cls.box.home / "console.log"
        fake = cls.box.traps / "adb"
        fake.write_text("#!/bin/sh\necho \"$*\" >> \"%s\"\ncase \"$*\" in *emu*) echo OK;; *devices*) echo 'List of devices attached'; echo 'emulator-5554\tdevice product:sdk model:sdk_gphone64';; esac\n" % cls.console_log)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    @classmethod
    def tearDownClass(cls):
        cls.box.close()

    def fixes(self):
        if not self.console_log.exists():
            return []
        return [line for line in self.console_log.read_text().splitlines() if "geo fix" in line]

    def test_inspect_says_what_the_route_is_and_who_can_play_it(self):
        seen = self.box.result("gpx.inspect", {"path": str(self.route), "device": "emulator-5554"})
        self.assertEqual((seen["name"], seen["points"], seen["source"]), ("Ring", 4, "track"))
        self.assertEqual(seen["timing"]["ordering"], "time")
        self.assertTrue(seen["device"]["supported"])
        self.assertIn("not wired", seen["playback"]["unsupported"]["iphone"])
        phone = self.box.result("gpx.inspect", {"path": str(self.route), "device": "R5CT123"})
        self.assertFalse(phone["device"]["supported"])
        self.assertEqual(phone["device"]["error"]["code"], "gpx_unsupported")
        bad = self.box.home / "bad.gpx"
        bad.write_text('<!DOCTYPE gpx [<!ENTITY x SYSTEM "file:///etc/passwd">]><gpx><wpt lat="1" lon="2"/><wpt lat="1" lon="3"/></gpx>')
        self.assertEqual(self.box.error("gpx.inspect", {"path": str(bad)})["code"], "gpx_invalid")
        self.assertEqual(self.box.error("gpx.play", {"device": "R5CT123", "path": str(self.route)})["code"], "gpx_unsupported")
        self.assertEqual(self.fixes(), [], "inspection sends nothing")

    def test_playback_sends_fixes_pauses_resumes_and_stops(self):
        started = self.box.result("gpx.play", {"device": "emulator-5554", "path": str(self.route), "speed": 0.5, "loop": True})
        self.assertEqual(started["baseline"], {"known": False, "restorable": False, "note": started["baseline"]["note"]})
        job = started["job"]["id"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and len(self.fixes()) < 2:
            time.sleep(0.1)
        self.assertGreaterEqual(len(self.fixes()), 2, self.console_log.read_text() if self.console_log.exists() else "no console log")
        self.assertIn("emu geo fix 16.3738 48.2082 171", self.fixes()[0])
        self.assertEqual(self.box.result("job.send", {"job_id": job, "command": "pause"})["sent"], "pause")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.box.result("gpx.status", {"job_id": job})["progress"]["state"] != "paused":
            time.sleep(0.1)
        paused = self.box.result("gpx.status", {"job_id": job})
        self.assertEqual(paused["progress"]["state"], "paused")
        count = len(self.fixes())
        time.sleep(1.0)
        self.assertEqual(len(self.fixes()), count, "a paused playback sends no fix")
        self.assertEqual(self.box.result("job.send", {"job_id": job, "command": "resume"})["sent"], "resume")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(self.fixes()) <= count:
            time.sleep(0.1)
        self.assertGreater(len(self.fixes()), count)
        self.assertEqual(self.box.result("job.send", {"job_id": job, "command": "stop"})["sent"], "stop")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self.box.result("job.get", {"job_id": job})["state"] == "running":
            time.sleep(0.2)
        final = self.box.result("job.get", {"job_id": job})
        self.assertEqual(final["state"], "succeeded", final)
        self.assertEqual(self.box.result("gpx.status", {"job_id": job})["progress"]["state"], "stopped")
        self.assertEqual(self.box.error("job.send", {"job_id": job, "command": "pause"})["code"], "not_pausable")
        other = self.box.result("job.list", {"limit": 1})
        self.assertTrue(other)


if __name__ == "__main__":
    unittest.main()
