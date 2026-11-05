"""AVD management and the image-to-emulator setup through the compiled CLI.

Everything runs against a private SDK directory with fake avdmanager and
sdkmanager scripts that record their calls and act on files, a private
TOMACCO_HOME and a private ANDROID_AVD_HOME with fixture AVDs. adb and the
emulator are stand-ins that refuse, so no device, emulator or adb server is
reached.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_client import Jobs, StudioError, binary, dispatch  # noqa: E402

FAKE_AVDMANAGER = r'''
import json, os, sys
from pathlib import Path
root = Path(os.environ["TOMACCO_TEST_ROOT"])
a = sys.argv[1:]
with (root / "avdmanager-calls.jsonl").open("a") as f:
    f.write(json.dumps({"argv": a, "avd_home": os.environ.get("ANDROID_AVD_HOME")}) + "\n")
home = Path(os.environ["ANDROID_AVD_HOME"])
if a[:2] == ["move", "avd"]:
    name, new = a[a.index("--name") + 1], a[a.index("--rename") + 1]
    old_ini = home / (name + ".ini")
    path = Path([l for l in old_ini.read_text().splitlines() if l.startswith("path=")][0][5:])
    target = path.parent / (new + ".avd")
    path.rename(target)
    old_ini.unlink()
    (home / (new + ".ini")).write_text("avd.ini.encoding=UTF-8\npath=%s\ntarget=android-36\n" % target)
    sys.exit(0)
if a[:2] == ["create", "avd"]:
    name, package, path = a[a.index("--name") + 1], a[a.index("--package") + 1], Path(a[a.index("--path") + 1])
    answer = sys.stdin.read()
    path.mkdir(parents=True)
    (path / "config.ini").write_text("AvdId=%s\navd.ini.displayname=%s\nimage.sysdir.1=%s/\nhw.lcd.width=1080\nhw.lcd.height=2400\nhw.lcd.density=420\n" % (name, name, package.replace(";", "/")))
    (home / (name + ".ini")).write_text("avd.ini.encoding=UTF-8\npath=%s\ntarget=android-36\n" % path)
    print("created with answer %r" % answer)
    sys.exit(0)
print("unsupported " + " ".join(a), file=sys.stderr)
sys.exit(2)
'''

FAKE_SDKMANAGER = r'''
import json, os, sys
from pathlib import Path
root = Path(os.environ["TOMACCO_TEST_ROOT"])
a = sys.argv[1:]
answer = sys.stdin.read() if not sys.stdin.isatty() else ""
with (root / "sdkmanager-calls.jsonl").open("a") as f:
    f.write(json.dumps({"argv": a, "answered_yes": answer.startswith("y")}) + "\n")
if "--install" in a:
    sdk = Path([x for x in a if x.startswith("--sdk_root=")][0].split("=", 1)[1])
    for package in a[a.index("--install") + 1:]:
        if not answer.startswith("y") and not (sdk / "licenses" / "android-sdk-license").exists():
            print("Accept? (y/N): Skipping following packages as the license is not accepted: " + package)
            continue
        image = sdk.joinpath(*package.split(";"))
        image.mkdir(parents=True, exist_ok=True)
        (image / "system.img").write_bytes(b"\0" * 64)
    sys.exit(0)
sys.exit(0)
'''

REFUSE = "#!/bin/sh\necho 'hardware transport blocked in test' >&2\nexit 97\n"
IMAGE = "system-images;android-36;google_apis;x86_64"


class AvdTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tomacco-avd-test-")
        self.root = Path(self.temp.name)
        self.sdk = self.root / "sdk"
        self.avd_home = self.root / "avd"
        self.avd_home.mkdir()
        tools = self.sdk / "cmdline-tools" / "latest" / "bin"
        tools.mkdir(parents=True)
        for name, body in [("avdmanager", FAKE_AVDMANAGER), ("sdkmanager", FAKE_SDKMANAGER)]:
            path = tools / name
            path.write_text("#!" + sys.executable + "\n" + body)
            path.chmod(0o755)
        for relative in ["platform-tools/adb", "emulator/emulator"]:
            path = self.sdk / relative
            path.parent.mkdir(parents=True)
            path.write_text(REFUSE)
            path.chmod(0o755)
        self.env = patch.dict(os.environ, {"TOMACCO_HOME": str(self.root / "state"), "TOMACCO_TEST_ROOT": str(self.root),
                                           "ANDROID_HOME": str(self.sdk), "ANDROID_SDK_ROOT": str(self.sdk),
                                           "ANDROID_AVD_HOME": str(self.avd_home), "ADB_SERVER_SOCKET": "tcp:127.0.0.1:1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def avd(self, name, locked=False, home=None):
        home = home or self.avd_home
        directory = home / (name + ".avd")
        (directory / "snapshots" / "default_boot").mkdir(parents=True)
        (directory / "snapshots" / "default_boot" / "ram.bin").write_bytes(b"\1" * 3000)
        (directory / "userdata-qemu.img").write_bytes(b"\2" * 2000)
        (directory / "cache.img").write_bytes(b"\3" * 500)
        (directory / "sdcard.img").write_bytes(b"\4" * 400)
        (directory / "config.ini").write_text("AvdId=%s\navd.ini.displayname=%s\nhw.lcd.width=1080\nhw.lcd.height=2400\nhw.lcd.density=420\n"
                                              "image.sysdir.1=system-images/android-36/google_apis/x86_64/\ntomacco.coldBoot=yes\n" % (name, name))
        if locked:
            (directory / "hardware-qemu.ini.lock").mkdir()
        (home / (name + ".ini")).write_text("avd.ini.encoding=UTF-8\npath=%s\ntarget=android-36\n" % directory)
        return directory

    def calls(self, tool):
        path = self.root / f"{tool}-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def install_image(self):
        image = self.sdk.joinpath(*IMAGE.split(";"))
        image.mkdir(parents=True)
        (image / "system.img").write_bytes(b"\0" * 64)

    def test_usage_reports_space_locks_and_what_a_wipe_frees(self):
        self.avd("Base")
        self.avd("Busy", locked=True)
        usage = dispatch("android.avd_usage")
        by_name = {a["name"]: a for a in usage["avds"]}
        self.assertEqual(sorted(by_name), ["Base", "Busy"])
        self.assertEqual(by_name["Base"]["wipe_bytes"], 3000 + 2000 + 500)
        self.assertGreater(by_name["Base"]["bytes"], by_name["Base"]["wipe_bytes"])
        self.assertEqual((by_name["Base"]["running"], by_name["Base"]["locks"], by_name["Base"]["managed"]), (False, [], False))
        self.assertEqual(by_name["Busy"]["locks"], ["hardware-qemu.ini.lock"])
        self.assertEqual(usage["wipe_removes"][0], "userdata-qemu.img")
        self.assertIsInstance(usage["free_bytes"], int)
        self.assertEqual(self.calls("avdmanager"), [], "reading usage runs no SDK tool")

    def test_wipe_needs_confirmation_refuses_a_running_avd_and_keeps_the_configuration(self):
        base = self.avd("Base")
        busy = self.avd("Busy", locked=True)
        with self.assertRaises(StudioError) as unconfirmed:
            dispatch("android.avd_manage", {"action": "wipe", "name": "Base"})
        self.assertEqual(unconfirmed.exception.code, "avd_confirmation_required")
        self.assertTrue((base / "userdata-qemu.img").exists())
        with self.assertRaises(StudioError) as running:
            dispatch("android.avd_manage", {"action": "wipe", "name": "Busy", "confirm": "Busy"})
        self.assertEqual(running.exception.code, "avd_in_use")
        self.assertEqual(running.exception.details["locks"], ["hardware-qemu.ini.lock"])
        self.assertTrue((busy / "userdata-qemu.img").exists())
        with self.assertRaises(StudioError) as unknown:
            dispatch("android.avd_manage", {"action": "wipe", "name": "Nope", "confirm": "Nope"})
        self.assertEqual(unknown.exception.code, "avd_not_found")
        wiped = dispatch("android.avd_manage", {"action": "wipe", "name": "Base", "confirm": "Base"})
        self.assertEqual(wiped["freed_bytes"], 3000 + 2000 + 500)
        self.assertEqual(sorted(r["file"] for r in wiped["removed"]), ["cache.img", "snapshots", "userdata-qemu.img"])
        self.assertEqual(sorted(p.name for p in base.iterdir()), ["config.ini", "sdcard.img"])
        self.assertEqual(self.calls("avdmanager"), [])

    def test_rename_goes_through_avdmanager_and_moves_the_nickname(self):
        self.avd("Base")
        dispatch("device.label", {"device": "avd:Base", "nickname": "Daily phone", "pinned": True})
        with self.assertRaises(StudioError) as unconfirmed:
            dispatch("android.avd_manage", {"action": "rename", "name": "Base", "new_name": "Daily"})
        self.assertEqual(unconfirmed.exception.code, "avd_confirmation_required")
        self.avd("Taken")
        with self.assertRaises(StudioError) as taken:
            dispatch("android.avd_manage", {"action": "rename", "name": "Base", "new_name": "Taken", "confirm": "Base"})
        self.assertEqual(taken.exception.code, "avd_exists")
        self.assertEqual(self.calls("avdmanager"), [], "a refused rename runs nothing")
        renamed = dispatch("android.avd_manage", {"action": "rename", "name": "Base", "new_name": "Daily", "confirm": "Base"})
        self.assertEqual((renamed["from"], renamed["to"], renamed["label_moved"]), ("Base", "Daily", True))
        call = self.calls("avdmanager")[0]
        self.assertEqual(call["argv"], ["move", "avd", "--name", "Base", "--rename", "Daily"])
        self.assertEqual(call["avd_home"], str(self.avd_home), "avdmanager looks where the AVD is")
        names = {a["name"]: a for a in dispatch("android.avds")["avds"]}
        self.assertEqual(sorted(names), ["Daily", "Taken"])
        self.assertEqual((names["Daily"]["nickname"], names["Daily"]["pinned"]), ("Daily phone", True))

    def test_duplicate_is_a_job_that_publishes_a_complete_copy_with_its_own_identity(self):
        self.avd("Base")
        self.avd("Busy", locked=True)
        with self.assertRaises(StudioError) as running:
            dispatch("android.avd_manage", {"action": "duplicate", "name": "Busy", "new_name": "Copy"})
        self.assertEqual(running.exception.code, "avd_in_use")
        job = dispatch("android.avd_manage", {"action": "duplicate", "name": "Base", "new_name": "Copy"})
        self.assertGreater(job["needed_bytes"], 0)
        done = Jobs().wait(job["id"], 60)
        self.assertEqual(done["state"], "succeeded", Jobs().logs(job["id"]))
        managed = self.root / "state" / "avds"
        copy = managed / "Copy.avd"
        self.assertTrue((managed / "Copy.ini").is_file())
        self.assertFalse((managed / "Copy.avd.partial").exists())
        self.assertTrue((copy / "userdata-qemu.img").is_file() and (copy / "sdcard.img").is_file())
        self.assertFalse((copy / "snapshots").exists(), "snapshots name the source's paths")
        config = dict(line.split("=", 1) for line in (copy / "config.ini").read_text().splitlines() if "=" in line)
        self.assertEqual((config["AvdId"], config["avd.ini.displayname"], config["hw.lcd.width"]), ("Copy", "Copy", "1080"))
        self.assertNotIn("tomacco.coldBoot", config)
        listed = {a["name"]: a for a in dispatch("android.avds")["avds"]}
        self.assertEqual(sorted(listed), ["Base", "Busy", "Copy"])
        self.assertTrue(listed["Copy"]["managed"])
        self.assertTrue((self.avd_home / "Base.avd" / "snapshots").exists(), "the source is untouched")
        with self.assertRaises(StudioError) as exists:
            dispatch("android.avd_manage", {"action": "duplicate", "name": "Base", "new_name": "Copy"})
        self.assertEqual(exists.exception.code, "avd_exists")

    def test_plan_tells_download_license_and_blockers_without_running_anything(self):
        plan = dispatch("android.avd_plan", {"name": "Fresh", "system_image": IMAGE, "profile_id": "pixel-11"})
        self.assertEqual((plan["image_installed"], plan["licenses_accepted"], plan["ready"]), (False, False, True))
        self.assertGreater(plan["estimate"]["download_bytes"], 0)
        self.assertIn("typical", plan["estimate"]["source"])
        install = plan["steps"][0]
        self.assertEqual((install["step"], install["needed"], install["needs_license"]), ("install_image", True, True))
        self.assertEqual([s["needed"] for s in plan["steps"]], [True, True, True, False])
        self.assertTrue(plan["form_factor"]["supported"])
        self.avd("Fresh")
        self.install_image()
        (self.sdk / "licenses").mkdir()
        (self.sdk / "licenses" / "android-sdk-license").write_text("hash\n")
        again = dispatch("android.avd_plan", {"name": "Fresh", "system_image": IMAGE, "profile_id": "pixel-11", "start": {"headless": True}})
        self.assertEqual((again["image_installed"], again["licenses_accepted"], again["ready"]), (True, True, False))
        self.assertEqual(again["blockers"], ["avd_exists"])
        self.assertEqual(again["estimate"]["download_bytes"], 0)
        self.assertTrue(again["steps"][3]["needed"])
        tv = dispatch("android.avd_plan", {"name": "Tv", "system_image": "system-images;android-36;google-tv;x86_64", "profile_id": "pixel-11"})
        self.assertEqual((tv["form_factor"]["kind"], tv["form_factor"]["supported"]), ("tv", False))
        self.assertIn("form_factor_unsupported", tv["blockers"])
        with self.assertRaises(StudioError) as blocked:
            dispatch("android.avd_setup", {"name": "Fresh", "system_image": IMAGE, "profile_id": "pixel-11"})
        self.assertEqual(blocked.exception.code, "avd_setup_blocked")
        self.assertEqual(self.calls("sdkmanager") + self.calls("avdmanager"), [], "planning runs no SDK tool")

    def test_setup_downloads_only_with_consent_then_creates_and_profiles_in_one_job(self):
        with self.assertRaises(StudioError) as license_needed:
            dispatch("android.avd_setup", {"name": "Fresh", "system_image": IMAGE, "profile_id": "pixel-11"})
        self.assertEqual(license_needed.exception.code, "sdk_license_required")
        self.assertEqual(self.calls("sdkmanager"), [], "nothing downloaded without consent")
        self.assertFalse((self.root / "state" / "avds" / "Fresh.avd").exists())
        job = dispatch("android.avd_setup", {"name": "Fresh", "system_image": IMAGE, "profile_id": "pixel-11", "accept_licenses": True})
        self.assertEqual([s["step"] for s in job["plan"]["steps"] if s["needed"]], ["install_image", "create", "profile"])
        done = Jobs().wait(job["id"], 90)
        self.assertEqual(done["state"], "succeeded", Jobs().logs(job["id"]))
        installs = self.calls("sdkmanager")
        self.assertEqual(len(installs), 1)
        self.assertIn(IMAGE, installs[0]["argv"])
        self.assertTrue(installs[0]["answered_yes"])
        self.assertTrue(self.sdk.joinpath(*IMAGE.split(";"), "system.img").is_file())
        create = self.calls("avdmanager")[0]
        self.assertEqual(create["argv"][:4], ["create", "avd", "--name", "Fresh"])
        self.assertEqual(create["avd_home"], str(self.root / "state" / "avds"))
        listed = {a["name"]: a for a in dispatch("android.avds")["avds"]}
        self.assertEqual(listed["Fresh"]["profile_id"], "pixel-11", "the profile step ran")
        self.assertTrue(listed["Fresh"]["managed"])
        self.assertEqual(listed["Fresh"]["system_image"], IMAGE)
        # The image is installed now: a second setup for another name needs no licence.
        second = dispatch("android.avd_setup", {"name": "Second", "system_image": IMAGE, "profile_id": "pixel-11"})
        self.assertEqual([s["step"] for s in second["plan"]["steps"] if s["needed"]], ["create", "profile"])
        self.assertEqual(Jobs().wait(second["id"], 90)["state"], "succeeded")
        self.assertEqual(len(self.calls("sdkmanager")), 1)

    def test_schema_refuses_bad_names_and_actions(self):
        for arguments in [{"action": "delete", "name": "Base"}, {"action": "wipe", "name": "../etc"}, {"action": "wipe"}]:
            with self.assertRaises(StudioError, msg=arguments):
                dispatch("android.avd_manage", arguments)
        with self.assertRaises(StudioError) as image:
            dispatch("android.avd_plan", {"name": "X", "system_image": "../x", "profile_id": "pixel-11"})
        self.assertIn(image.exception.code, ("invalid_argument", "invalid_identifier"))


if __name__ == "__main__":
    unittest.main()
