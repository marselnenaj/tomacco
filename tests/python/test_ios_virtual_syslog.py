"""The virtual iPhone's console log as structured lines, through ios.virtual.syslog."""
import json
import plistlib
from pathlib import Path
import shutil
import unittest

from native_client import StudioError, dispatch, jobs
from test_ios_virtual_profiles import running_guest, write_profile
from test_product import Isolated

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/ios_guest_syslog.log"


class GuestSyslog(Isolated):
    def test_the_guest_jobs_console_is_read_filtered_and_followed(self):
        directory = write_profile(self.root / "profile")
        dispatch("ios.virtual.configure", {"directory": str(directory)})
        with self.assertRaises(StudioError) as error:
            dispatch("ios.virtual.syslog")
        self.assertEqual(error.exception.code, "ios_virtual_not_running")
        guest = running_guest(self)
        try:
            state = json.loads((directory / "state.json").read_text())
            (directory / "state.json").write_text(json.dumps({**state, "closed": False, "guest_job": guest}))
            log = Path(jobs.get(guest)["plan"]["cwd"]) / "paravirtual/uart.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(FIXTURE, log)
            reply = dispatch("ios.virtual.syslog", {"process": "SampleApp"})
            self.assertEqual((reply["job_id"], reply["running"], reply["reset"]), (guest, True, False))
            self.assertEqual([(line["process"], line["pid"], line["level"]) for line in reply["lines"]],
                             [("dev.tomacco.SampleApp", 412, "error"), ("SampleApp", 412, "warning"),
                              ("SampleApp", 412, "notice")])
            faults = dispatch("ios.virtual.syslog", {"levels": ["fault"]})["lines"]
            self.assertEqual([(line["time"], line["process"], line["message"]) for line in faults],
                             [("Oct 28 07:19:55", "backboardd", "display pipeline stalled")])
            # Following: from next_offset, only what was written since.
            with log.open("a") as stream:
                stream.write(" now\nSampleApp[412] <Info>: second batch\n")
            more = dispatch("ios.virtual.syslog", {"offset": reply["next_offset"]})
            self.assertEqual([line["message"] for line in more["lines"]],
                             ["partial line without its end now", "second batch"])
            with self.assertRaises(StudioError):
                dispatch("ios.virtual.syslog", {"levels": ["loud"]})
            # One listed app's lines, by its executable and its launchd service.
            with self.assertRaises(StudioError) as error:
                dispatch("ios.virtual.syslog", {"bundle_id": "dev.tomacco.SampleApp"})
            self.assertEqual(error.exception.code, "ios_virtual_application_unknown")
            app = self.root / "SampleApp.app"
            app.mkdir()
            (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "dev.tomacco.SampleApp",
                                                             "CFBundleExecutable": "SampleApp"}))
            profile = json.loads((directory / "profile.json").read_text())
            (directory / "profile.json").write_text(json.dumps({**profile, "applications": [{"bundle": str(app)}]}))
            owned = dispatch("ios.virtual.syslog", {"bundle_id": "dev.tomacco.SampleApp", "offset": 0})
            self.assertEqual(owned["app"], {"bundle_id": "dev.tomacco.SampleApp", "executable": "SampleApp"})
            self.assertEqual([line["message"] for line in owned["lines"]],
                             ["exited due to SIGABRT", "Caller wanted 1 of 2 rows", "Loaded 3 items", "second batch"])
        finally:
            jobs.cancel(guest)
            jobs.wait(guest, 30)
        self.assertFalse(dispatch("ios.virtual.syslog", {"max_lines": 1})["running"])


if __name__ == "__main__":
    unittest.main()
