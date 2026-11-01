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


class IphoneSigningTests(IphoneTools):
    def test_profiles_on_the_phone_and_in_the_project_are_summarised_and_the_copies_removed(self):
        import datetime
        import test_ios_ipa_inspect as fixtures
        today = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)
        plist = {"Name": "Free", "UUID": "FREE", "TeamIdentifier": ["TEAM123456"],
                 "CreationDate": today - datetime.timedelta(days=4), "ExpirationDate": today + datetime.timedelta(days=3),
                 "ProvisionedDevices": [UDID], "DeveloperCertificates": [fixtures.SIGNER],
                 "Entitlements": {"application-identifier": "TEAM123456.dev.example.counter"}}
        import plistlib
        profile = self.root / "free.mobileprovision"
        profile.write_bytes(fixtures.cms([], plistlib.dumps(plist)))
        self.tool("ideviceprovision", f"""import shutil,sys
from pathlib import Path
Path({str(self.root / 'provision-args.json')!r}).write_text(' '.join(sys.argv[1:]))
shutil.copy({str(profile)!r}, Path(sys.argv[4]) / 'FREE.mobileprovision')
""")
        project = self.root / "App"
        (project / "xtool/Counter.app").mkdir(parents=True)
        (project / "xtool/Counter.app/embedded.mobileprovision").write_bytes(profile.read_bytes())
        result = dispatch("ios.signing", {"device": UDID, "path": str(project)})
        args = (self.root / "provision-args.json").read_text().split()
        self.assertEqual(args[:3], ["-u", UDID, "copy"])
        self.assertFalse(Path(args[3]).exists())
        sources = [row["source"] for row in result["profiles"]]
        self.assertEqual(sources, ["project", "device"])
        device_row = result["profiles"][1]
        self.assertIsNone(device_row["path"])
        self.assertTrue(device_row["free_account"])
        self.assertTrue(device_row["covers_device"])
        self.assertTrue(any("free Apple account" in text for text in result["warnings"]))
        self.assertEqual(result["identities"][0]["team_id"], "TEAM123456")
        self.assertEqual(result["apps"][0]["ok"], False)
        with self.assertRaises(StudioError) as nothing:
            dispatch("ios.signing", {})
        self.assertEqual(nothing.exception.code, "invalid_argument")


class IphoneStatusTests(IphoneTools):
    """ios.device_status against stand-ins whose answers a test changes."""

    def phone(self, paired=True, validate="SUCCESS: Validated pairing with device", developer_mode="true",
              images='{"images":["abc"]}', version="18.1"):
        self.tool("idevice_id", f"print({UDID!r})\n")
        self.tool("idevicepair", f"""import json,os,sys
from pathlib import Path
with Path(os.environ['TOMACCO_TEST_ROOT'],'pair.log').open('a') as log: log.write(json.dumps(sys.argv[1:])+'\\n')
if sys.argv[1]=='list': print({UDID if paired else ''!r})
else:
    print({validate!r}); sys.exit(0 if {validate!r}.startswith('SUCCESS') else 1)
""")
        self.tool("ideviceinfo", f"""import json,os,sys
from pathlib import Path
with Path(os.environ['TOMACCO_TEST_ROOT'],'info.log').open('a') as log: log.write(json.dumps(sys.argv[1:])+'\\n')
key=sys.argv[sys.argv.index('-k')+1]
print({{'ProductVersion':{version!r},'DeviceName':'Test iPhone','DeveloperModeStatus':{developer_mode!r}}}[key])
""")
        self.tool("go-ios", f"print({images!r})\n")

    def test_a_ready_phone_has_no_steps_left(self):
        self.phone()
        result = dispatch("ios.device_status", {})
        self.assertEqual(result["device"], UDID)
        self.assertEqual((result["pairing"], result["trusted"], result["unlocked"]), ("paired", True, True))
        self.assertEqual((result["developer_mode"], result["developer_image"]), (True, "mounted"))
        self.assertEqual((result["ios_version"], result["name"]), ("18.1", "Test iPhone"))
        self.assertTrue(result["ready"])
        self.assertEqual([step["operation"]["arguments"]["action"] for step in result["next_steps"]], ["tunnel"])

    def test_an_unpaired_phone_is_only_asked_through_the_simple_connection(self):
        self.phone(paired=False)
        result = dispatch("ios.device_status", {"device": UDID})
        self.assertEqual(result["pairing"], "not_paired")
        self.assertFalse(result["ready"])
        self.assertEqual(result["next_steps"][0]["operation"]["arguments"], {"action": "pair", "device": UDID})
        info = [json.loads(line) for line in (self.root / "info.log").read_text().splitlines()]
        self.assertTrue(all("-s" in call for call in info), info)
        pair = [json.loads(line) for line in (self.root / "pair.log").read_text().splitlines()]
        self.assertEqual(pair, [["list"]])

    def test_locked_waiting_and_developer_mode_off_give_plain_steps(self):
        self.phone(validate="ERROR: Could not validate with device X because a passcode is set. Please enter the passcode on the device and retry.")
        result = dispatch("ios.device_status", {"device": UDID})
        self.assertEqual((result["pairing"], result["unlocked"]), ("locked", False))
        self.assertIn("Unlock", result["next_steps"][0]["step"])
        self.phone(validate="ERROR: Please accept the trust dialog on the screen of device X, then attempt to pair again.")
        self.assertIn("tap Trust", dispatch("ios.device_status", {"device": UDID})["next_steps"][0]["step"])
        self.phone(developer_mode="false", images='{"images":[]}')
        result = dispatch("ios.device_status", {"device": UDID})
        self.assertEqual((result["developer_mode"], result["developer_image"]), (False, "not_mounted"))
        self.assertIn("Developer Mode", result["next_steps"][0]["step"])
        self.assertEqual(result["next_steps"][1]["operation"]["arguments"]["action"], "developer-image")

    def test_a_missing_phone_says_to_connect_it(self):
        self.phone()
        result = dispatch("ios.device_status", {"device": "00008110-FFFFFFFFFFFFFFFF"})
        self.assertFalse(result["connected"])
        self.assertIn("USB cable", result["next_steps"][0]["step"])


if __name__ == "__main__":
    unittest.main()
