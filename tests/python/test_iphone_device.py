"""A USB-connected iPhone from Linux, with stand-ins for go-ios, libimobiledevice
and WebDriverAgent. No iPhone is connected: these tests check what Tomacco asks
of the tools and how it reads their answers, not the tools or a phone."""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_client import StudioError, dispatch, jobs
from test_product import Isolated

UDID = "00008110-0011223344556677"
APPS = [{"CFBundleIdentifier": "XTL-AB12.dev.example.counter", "CFBundleExecutable": "Counter",
         "CFBundleName": "Counter", "CFBundleShortVersionString": "1.2", "CFBundleVersion": "7"},
        {"CFBundleIdentifier": "com.apple.Maps", "CFBundleExecutable": "Maps", "CFBundleName": "Maps"}]

# go-ios: logs its arguments, lists APPS and prints three syslog lines.
GO_IOS = """import json,os,sys
from pathlib import Path
root=Path(os.environ['TOMACCO_TEST_ROOT'])
with (root/'go-ios.log').open('a') as log: log.write(json.dumps(sys.argv[1:])+'\\n')
command=sys.argv[1]
if command=='apps': print('time="x" level=info msg="listing"'); print(json.dumps(%s))
elif command=='syslog':
    for line in ['Oct 31 12:00:00 iPhone Counter(UIKitCore)[123] <Notice>: tapped Save',
                 'Oct 31 12:00:01 iPhone SpringBoard[55] <Notice>: tapped Save',
                 'Oct 31 12:00:02 iPhone Counter[123] <Error>: load failed']:
        print(json.dumps({'msg':line}),flush=True)
""" % json.dumps(APPS)


class IphoneTools(Isolated):
    def go_ios(self, extra=""):
        return self.tool("go-ios", GO_IOS + extra)

    def calls(self, name="go-ios.log"):
        path = self.root / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def without_path_tools(self):
        """Search an empty PATH, so that this host's own libimobiledevice is not found;
        the stand-ins are configured by absolute path."""
        empty = self.root / "empty-path"
        empty.mkdir(exist_ok=True)
        # Isolated's patch of os.environ restores PATH after the test.
        os.environ["PATH"] = str(empty)


class IphoneLogTests(IphoneTools):
    def test_go_ios_syslog_is_filtered_to_the_apps_process_and_text(self):
        self.go_ios()
        self.without_path_tools()
        job = dispatch("device.logs", {"platform": "ios", "device": UDID, "bundle_id": "dev.example.counter",
                                       "duration": 5})
        self.assertEqual(job["process"], "Counter")
        done = jobs.wait(job["id"], 15)
        self.assertEqual(done["state"], "succeeded", jobs.logs(job["id"]))
        text = jobs.logs(job["id"])["text"]
        self.assertIn("Counter(UIKitCore)[123] <Notice>: tapped Save", text)
        self.assertIn("Counter[123] <Error>: load failed", text)
        self.assertNotIn("SpringBoard", text)
        self.assertEqual(self.calls()[-1], ["syslog", "--udid=" + UDID])
        job = dispatch("device.logs", {"platform": "ios", "device": UDID, "bundle_id": "dev.example.counter",
                                       "filter": "failed", "duration": 5})
        jobs.wait(job["id"], 15)
        text = jobs.logs(job["id"])["text"]
        self.assertIn("load failed", text)
        self.assertNotIn("tapped Save", text)

    def test_idevicesyslog_gets_the_process_and_match(self):
        self.go_ios()
        self.tool("idevicesyslog")
        job = dispatch("device.logs", {"platform": "ios", "device": UDID, "bundle_id": "dev.example.counter",
                                       "filter": "Save", "duration": 5})
        self.assertEqual(job["plan"]["steps"][0]["argv"][1:], ["-u", UDID, "-p", "Counter", "-m", "Save"])
        self.assertEqual(jobs.wait(job["id"], 15)["state"], "succeeded")
        job = dispatch("device.logs", {"platform": "ios", "device": UDID, "duration": 5})
        self.assertEqual(job["plan"]["steps"][0]["argv"][1:], ["-u", UDID])
        self.assertNotIn("process", job)

    def test_an_app_that_is_not_installed_is_refused_before_a_job(self):
        self.go_ios()
        with self.assertRaises(StudioError) as refused:
            dispatch("device.logs", {"platform": "ios", "device": UDID, "bundle_id": "dev.example.other"})
        self.assertEqual(refused.exception.code, "app_not_installed")
        with self.assertRaises(StudioError):
            dispatch("device.logs", {"platform": "ios", "device": "--udid", "duration": 5})
        self.assertEqual(jobs.listing(), [])


if __name__ == "__main__":
    unittest.main()
