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
elif command=='fsync':
    flags=dict(arg[2:].split('=',1) for arg in sys.argv[2:] if arg.startswith('--') and '=' in arg)
    if flags.get('app')!='XTL-AB12.dev.example.counter': print('house_arrest refused',file=sys.stderr); sys.exit(1)
    if 'tree' in sys.argv: print('/Documents'); print('├── notes.txt'); print('└── db'); print('    └── app.sqlite')
    elif 'pull' in sys.argv:
        target=Path(flags['dstPath'])/'Documents'; (target/'db').mkdir(parents=True)
        (target/'notes.txt').write_text('hello'); (target/'db/app.sqlite').write_bytes(b'SQLite')
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


class IphoneAppTests(IphoneTools):
    def test_apps_are_launched_stopped_and_uninstalled_by_their_installed_id(self):
        self.go_ios()
        for action, verb in (("launch", "launch"), ("stop", "kill"), ("uninstall", "uninstall")):
            job = dispatch("device.app", {"platform": "ios", "device": UDID, "bundle_id": "dev.example.counter",
                                          "action": action})
            self.assertEqual(jobs.wait(job["id"], 10)["state"], "succeeded", jobs.logs(job["id"]))
            self.assertEqual(self.calls()[-1], [verb, "XTL-AB12.dev.example.counter", "--udid=" + UDID])
        with self.assertRaises(StudioError) as refused:
            dispatch("device.app", {"platform": "ios", "device": UDID, "bundle_id": "dev.example.counter",
                                    "action": "clear-data"})
        self.assertEqual(refused.exception.code, "action_unsupported")
        with self.assertRaises(StudioError) as missing:
            dispatch("device.app", {"platform": "ios", "device": UDID, "bundle_id": "dev.example.gone"})
        self.assertEqual(missing.exception.code, "app_not_installed")

    def test_the_inventory_has_one_plain_row_per_app(self):
        self.go_ios()
        result = dispatch("ios.apps", {"device": UDID})
        self.assertEqual(result["apps"], APPS)
        self.assertEqual(result["installed"][0], {"bundle_id": "XTL-AB12.dev.example.counter", "name": "Counter",
                                                  "version": "1.2", "build": "7", "executable": "Counter"})
        dispatch("ios.apps", {"device": UDID, "all": True})
        self.assertEqual(self.calls()[-1], ["apps", "--udid=" + UDID, "--all"])

    def test_container_files_are_listed_and_pulled_as_one_tar(self):
        import tarfile
        self.go_ios()
        listed = dispatch("ios.app_files", {"device": UDID, "bundle_id": "dev.example.counter", "path": "Documents"})
        self.assertEqual([row["path"] for row in listed["files"]], ["notes.txt", "db", "db/app.sqlite"])
        self.assertEqual(self.calls()[-1], ["fsync", "--udid=" + UDID, "--app=XTL-AB12.dev.example.counter", "tree",
                                            "--path=/Documents"])
        output = self.root / "out/counter.tar"
        job = dispatch("ios.app_files", {"device": UDID, "bundle_id": "dev.example.counter", "action": "pull",
                                         "path": "Documents", "output": str(output)})
        done = jobs.wait(job["id"], 15)
        self.assertEqual(done["state"], "succeeded", jobs.logs(job["id"]))
        with tarfile.open(output) as archive:
            names = sorted(name.lstrip("./") for name in archive.getnames())
            self.assertIn("Documents/db/app.sqlite", names)
            self.assertEqual(archive.extractfile("./Documents/notes.txt").read(), b"hello")
        self.assertFalse(any((self.root / "state/iphone-files").iterdir()))
        for bad in ("../x", "/etc"):
            with self.assertRaises(StudioError):
                dispatch("ios.app_files", {"device": UDID, "bundle_id": "dev.example.counter", "path": bad})
        with self.assertRaises(StudioError):
            dispatch("ios.app_files", {"device": UDID, "bundle_id": "dev.example.counter", "action": "pull",
                                       "output": str(self.root / "x.zip")})
        with self.assertRaises(StudioError) as refused:
            dispatch("ios.app_files", {"device": UDID, "bundle_id": "com.apple.Maps"})
        self.assertEqual(refused.exception.code, "app_container_unavailable")


SOURCE = """<?xml version="1.0" encoding="UTF-8"?><AppiumAUT><XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Counter" label="Counter" enabled="true" visible="true" x="0" y="0" width="390" height="844"><XCUIElementTypeStaticText type="XCUIElementTypeStaticText" name="count" label="Count: 0" enabled="true" visible="true" x="20" y="100" width="200" height="30"/><XCUIElementTypeButton type="XCUIElementTypeButton" name="add" label="Add" enabled="true" visible="true" x="20" y="200" width="100" height="44"/><XCUIElementTypeTextField type="XCUIElementTypeTextField" name="who" value="Name" enabled="true" visible="true" x="20" y="400" width="300" height="40"/></XCUIElementTypeApplication></AppiumAUT>"""


class IphoneScenarioTests(IphoneTools):
    """testing.run with platform ios against the WDA stand-in in tests/fixtures/iphone_usb.py."""

    def connect(self):
        from native_client import ROOT
        self.tool("iproxy", (ROOT / "tests/fixtures/iphone_usb.py").read_text())
        self.tool("go-ios", "import time\ntime.sleep(120)\n")
        (self.root / "wda-source.xml").write_text(SOURCE)
        live = dispatch("runtime.start", {"platform": "ios", "device": UDID, "runner_bundle_id": "dev.test.Runner.xctrunner"})
        self.sessions.append(live["session"])
        return live

    def setUp(self):
        super().setUp()
        self.sessions = []

    def tearDown(self):
        # Before Isolated's tearDown puts TOMACCO_HOME back.
        for session in self.sessions:
            dispatch("runtime.stop", {"session": session})
        super().tearDown()

    def run_scenario(self, steps, name="report"):
        scenario = self.root / f"{name}.json"
        scenario.write_text(json.dumps({"name": "Counter", "steps": steps}))
        directory = self.root / name
        job = dispatch("testing.run", {"path": str(scenario), "directory": str(directory), "devices": [UDID],
                                       "platform": "ios"})
        done = jobs.wait(job["id"], 60)
        return done, json.loads((directory / "report.json").read_text()), directory

    def wda(self, suffix):
        calls = [json.loads(line) for line in (self.root / "wda-calls.jsonl").read_text().splitlines()]
        return [call for call in calls if call["path"].endswith(suffix)]

    def test_a_scenario_finds_taps_and_fills_through_webdriveragent(self):
        self.connect()
        done, report, directory = self.run_scenario([
            {"action": "assert", "selector": {"text": "Count: 0"}},
            {"action": "click", "selector": {"resource_id": "add"}},
            {"action": "fill", "selector": {"resource_id": "who"}, "text": "Zoë"},
            {"action": "wait", "selector": {"text": "Gone"}, "state": "absent", "timeout": 0},
            {"action": "tap", "x": 195, "y": 422},
            {"action": "key", "key": "home"},
            {"action": "screenshot"},
        ])
        self.assertEqual(done["state"], "succeeded", report)
        self.assertTrue(report["passed"])
        taps = [call["body"] for call in self.wda("/wda/tap")]
        self.assertEqual(taps[0], {"x": 70, "y": 222})
        self.assertEqual(taps[1], {"x": 170, "y": 420})
        self.assertEqual(taps[2], {"x": 195, "y": 422})
        self.assertEqual(self.wda("/element/fixture-element/value")[0]["body"], {"value": ["Zoë"]})
        self.assertEqual(len(self.wda("/wda/homescreen")), 1)
        self.assertTrue(list(directory.glob("*-006.png")))
        self.assertEqual(report["results"][0]["crashes_error"]["code"], "not_checked")

    def test_a_failing_step_fails_the_run_with_a_capture(self):
        self.connect()
        done, report, directory = self.run_scenario([
            {"action": "click", "selector": {"text": "Missing"}, "timeout": 0}])
        self.assertEqual(done["state"], "failed")
        step = report["results"][0]["steps"][0]
        self.assertEqual(step["result"]["error"]["code"], "element_timeout")
        self.assertTrue(list(directory.glob("*-failure.png")))

    def test_without_a_session_or_with_android_only_steps_the_run_is_refused(self):
        scenario = self.root / "s.json"
        scenario.write_text(json.dumps({"name": "x", "steps": [{"action": "assert", "selector": {"text": "Go"}}]}))
        args = {"path": str(scenario), "directory": str(self.root / "out"), "devices": [UDID], "platform": "ios"}
        with self.assertRaises(StudioError) as missing:
            dispatch("testing.run", args)
        self.assertEqual(missing.exception.code, "runtime_required")
        scenario.write_text(json.dumps({"name": "x", "steps": [{"action": "clear", "bundle_id": "dev.example.app"}]}))
        with self.assertRaises(StudioError) as refused:
            dispatch("testing.run", args)
        self.assertEqual(refused.exception.code, "step_unsupported")


# go-ios screenshot --stream: an MJPEG server on --port with one picture, sent in pieces.
STREAMING_GO_IOS = """import os,sys,time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
Path(os.environ['TOMACCO_TEST_ROOT'],'stream-args.txt').write_text(' '.join(sys.argv[1:]))
picture=Path(os.environ['TOMACCO_TEST_ROOT'],'picture.jpg').read_bytes()
port=int(next(a for a in sys.argv if a.startswith('--port=')).split('=')[1])
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def do_GET(self):
        self.send_response(200); self.send_header('Content-Type','multipart/x-mixed-replace; boundary=--frame'); self.end_headers()
        try:
            while True:
                part=b'--frame\\r\\nContent-Type: image/jpeg\\r\\n\\r\\n'+picture+b'\\r\\n'
                self.wfile.write(part[:7]); self.wfile.flush(); self.wfile.write(part[7:]); self.wfile.flush(); time.sleep(.05)
        except (BrokenPipeError,ConnectionResetError): pass
HTTPServer(('127.0.0.1',port),Handler).serve_forever()
"""


class IphoneScreenTests(IphoneTools):
    def test_the_screen_is_recorded_from_the_go_ios_stream_into_an_mp4(self):
        import shutil
        import subprocess
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("needs ffmpeg")
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=64x48",
                        "-frames:v", "1", str(self.root / "picture.jpg")], check=True)
        self.tool("go-ios", STREAMING_GO_IOS)
        target = self.root / "out/screen.mp4"
        job = dispatch("ios.record", {"device": UDID, "path": str(target), "seconds": 1, "fps": 5})
        done = jobs.wait(job["id"], 60)
        self.assertEqual(done["state"], "succeeded", jobs.logs(job["id"]))
        self.assertEqual(target.read_bytes()[4:8], b"ftyp")
        summary = json.loads([line for line in jobs.logs(job["id"])["text"].splitlines() if line.startswith("{")][-1])
        self.assertEqual((summary["state"], summary["frames_written"]), ("recorded", 5))
        self.assertGreater(summary["pictures_received"], 0)
        args = (self.root / "stream-args.txt").read_text().split()
        self.assertEqual(args[:2], ["screenshot", "--stream"])
        self.assertIn("--udid=" + UDID, args)
        with self.assertRaises(StudioError):
            dispatch("ios.record", {"device": UDID, "path": str(self.root / "screen.mov")})

    def test_a_screenshot_falls_back_to_go_ios(self):
        self.tool("go-ios", "import sys\nfrom pathlib import Path\nPath(sys.argv[2].split('=',1)[1]).write_bytes(b'\\x89PNG fake')\n")
        self.without_path_tools()
        target = self.root / "shot.png"
        job = dispatch("device.screenshot", {"platform": "ios", "device": UDID, "path": str(target)})
        self.assertEqual(job["plan"]["steps"][0]["argv"][1:], ["screenshot", "--output=" + str(target), "--udid=" + UDID])
        self.assertEqual(jobs.wait(job["id"], 15)["state"], "succeeded", jobs.logs(job["id"]))
        self.assertTrue(target.read_bytes().startswith(b"\x89PNG"))


class MjpegTests(unittest.TestCase):
    def test_pictures_are_cut_from_a_stream_in_any_pieces(self):
        import ios_device_record as recorder
        stream = b"--frame\r\n\r\n\xff\xd8one\xff\xd9\r\n--frame\r\n\r\n\xff\xd8two\xff\xd9junk"
        for size in (1, 3, len(stream)):
            pictures, found = recorder.Pictures(), []
            for at in range(0, len(stream), size):
                found += pictures.feed(stream[at:at + size])
            self.assertEqual(found, [b"\xff\xd8one\xff\xd9", b"\xff\xd8two\xff\xd9"], size)


# go-ios for startup measurements: a phone whose state lives in phone.json.
# launch and kill change what is in front; screenshot --stream serves one
# picture every 50 ms over HTTP whose single byte is the screen's shade; the
# app draws 300 ms after its launch, and logs a line 100 ms after it.
STARTUP_GO_IOS = """import json,os,socket,sys,time
from pathlib import Path
root=Path(os.environ['TOMACCO_TEST_ROOT'])
with (root/'go-ios.log').open('a') as log: log.write(json.dumps(sys.argv[1:])+'\\n')
state=root/'phone.json'
SHADES={'home':40,'settings':120}
def load():
    return json.loads(state.read_text()) if state.exists() else {'front':'home','pid':0,'next':500,'since':0,'behind':40}
def save(s):
    temporary=root/'phone.tmp'; temporary.write_text(json.dumps(s)); os.replace(temporary,state)
def shade(s):
    if s['front']!='app': return SHADES[s['front']]
    return 220 if time.time()-s['since']>0.3 else s['behind']
command=sys.argv[1]
if command=='apps': print(json.dumps(%s))
elif command=='launch':
    s=load()
    if sys.argv[2]=='com.apple.Preferences': s['front']='settings'
    else:
        s['behind']=shade(s)
        if not s['pid']: s['pid']=s['next']; s['next']+=1
        s['front']='app'; s['since']=time.time()
        print(json.dumps({'level':'info','msg':'Process launched','pid':s['pid']}),file=sys.stderr)
    save(s)
elif command=='kill':
    s=load()
    if not s['pid']: print('process not found',file=sys.stderr); sys.exit(1)
    s['pid']=0; s['front']='home'; save(s)
elif command=='screenshot':
    port=int([a for a in sys.argv if a.startswith('--port=')][0][7:])
    server=socket.socket(); server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    server.bind(('127.0.0.1',port)); server.listen(1)
    connection,_=server.accept(); connection.recv(4096)
    connection.sendall(b'HTTP/1.0 200 OK\\r\\nContent-Type: multipart/x-mixed-replace;boundary=b\\r\\n\\r\\n')
    while True:
        connection.sendall(b'--b\\r\\nContent-Type: image/jpeg\\r\\n\\r\\n\\xff\\xd8'+bytes([shade(load())])+b'\\xff\\xd9\\r\\n')
        time.sleep(0.05)
elif command=='syslog':
    logged=0
    while True:
        s=load()
        if s['front']=='app' and s['since']!=logged and time.time()-s['since']>0.1:
            logged=s['since']
            print(json.dumps({'msg':'Nov  1 10:00:00 iPhone Counter(UIKitCore)[%%d] <Notice>: scene active' %% s['pid']}),flush=True)
        time.sleep(0.05)
""" % json.dumps(APPS)

# ffmpeg turning each stand-in picture into one 32 x 64 grey frame of its shade.
FFMPEG = """import json,os,sys
from pathlib import Path
with (Path(os.environ['TOMACCO_TEST_ROOT'])/'ffmpeg.log').open('a') as log: log.write(json.dumps(sys.argv[1:])+'\\n')
data=b''
while True:
    chunk=sys.stdin.buffer.read1(65536)
    if not chunk: break
    data+=chunk
    while True:
        start=data.find(b'\\xff\\xd8'); end=data.find(b'\\xff\\xd9',start+2) if start>=0 else -1
        if start<0 or end<0: break
        sys.stdout.buffer.write(bytes([data[start+2]])*2048); sys.stdout.buffer.flush(); data=data[end+2:]
"""


class IphoneStartupTests(IphoneTools):
    """profile.startup on a USB iPhone, against stand-ins for go-ios and ffmpeg:
    what is asked of them and how the screen times are read, not a phone."""

    def setUp(self):
        super().setUp()
        self.tool("go-ios", STARTUP_GO_IOS)
        self.without_path_tools()
        # ffmpeg is found on PATH, which is now only the stand-in.
        ffmpeg = self.root / "empty-path" / "ffmpeg"
        ffmpeg.write_text("#!" + sys.executable + "\n" + FFMPEG)
        ffmpeg.chmod(0o755)

    def measure(self, **extra):
        return dispatch("profile.startup", {"platform": "ios", "device": UDID, "bundle_id": "dev.example.counter",
                                            "runs": 2, "settle_ms": 300, **extra})

    def test_cold_launches_are_timed_on_the_screen_stream_after_go_ios_kill(self):
        target = self.root / "startup.json"
        value = self.measure(report=str(target))
        self.assertEqual((value["device_kind"], value["mode"], value["kind"]), ("iphone", "cold", "startup"))
        self.assertEqual(value["installed_bundle_id"], "XTL-AB12.dev.example.counter")
        self.assertEqual([run["launch_state"] for run in value["runs"]], ["cold", "cold"])
        self.assertEqual([run["pid"] for run in value["runs"]], [501, 502])
        self.assertEqual(value["mismatched_runs"], 0)
        for run in value["runs"]:
            # The stand-in app draws 300 ms after its launch, seen in 50 ms pictures.
            self.assertGreaterEqual(run["first_frame_ms"], 280)
            self.assertLess(run["first_frame_ms"], 2000)
            self.assertLessEqual(run["launched_ms"], run["first_frame_ms"])
            self.assertIsNotNone(run["settled_ms"])
            self.assertIsNotNone(run["first_log_ms"])
            self.assertTrue(run["baseline_still"])
        metrics = value["metrics"]
        for name in ("startup_cold_median_ms", "startup_cold_p90_ms", "startup_cold_launched_median_ms",
                     "startup_cold_settled_median_ms", "startup_cold_first_log_median_ms"):
            self.assertIsInstance(metrics[name], (int, float), name)
        self.assertEqual(json.loads(target.read_text())["metrics"], metrics)
        calls = self.calls()
        app = "XTL-AB12.dev.example.counter"
        self.assertEqual(sum(call == ["kill", app, "--udid=" + UDID] for call in calls), 2)
        self.assertEqual(sum(call == ["launch", app, "--udid=" + UDID] for call in calls), 3)
        stream = next(call for call in calls if call[0] == "screenshot")
        self.assertEqual((stream[1], stream[3]), ("--stream", "--udid=" + UDID))
        self.assertIn(["syslog", "--udid=" + UDID], calls)
        decoder = self.calls("ffmpeg.log")[0]
        self.assertIn("scale=32:64:flags=area,format=gray", decoder)
        # The saved report passes and fails budgets like an Android one.
        verdict = dispatch("profile.budget", {"reports": [str(target)],
                                              "budgets": {"startup_cold_median_ms": {"maximum": 60000},
                                                          "startup_cold_settled_p90_ms": {"maximum": 1}}})
        self.assertEqual([r["passed"] for r in verdict["results"]], [True, False])

    def test_hot_launches_reopen_the_running_app_from_settings(self):
        value = self.measure(mode="hot", runs=1)
        self.assertEqual(value["runs"][0]["launch_state"], "hot")
        self.assertEqual(value["runs"][0]["pid"], 500)
        self.assertIn(["launch", "com.apple.Preferences", "--udid=" + UDID], self.calls())
        self.assertFalse(any(call[0] == "kill" for call in self.calls()))
        self.assertIsInstance(value["metrics"]["startup_hot_median_ms"], (int, float))

    def test_android_only_modes_and_arguments_are_refused(self):
        for extra in ({"mode": "warm"}, {"activity": ".Main"}):
            with self.assertRaises(StudioError) as refused:
                self.measure(**extra)
            self.assertEqual(refused.exception.code, "invalid_argument")
        with self.assertRaises(StudioError) as refused:
            dispatch("profile.startup", {"device": "emulator-5554", "bundle_id": "dev.example.counter", "settle_ms": 500})
        self.assertEqual(refused.exception.code, "invalid_argument")
        self.assertFalse(any(call[0] in ("launch", "kill") for call in self.calls()))


if __name__ == "__main__":
    unittest.main()
