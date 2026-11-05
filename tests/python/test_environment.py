"""QA environments through the compiled CLI against a fake adb.

The fake adb is a stateful Android: it remembers settings, night mode, app
languages and accessibility services per serial and records every call, so
the tests see exactly what Tomacco would change on a device. No real adb,
emulator or device is reached: TOMACCO_HOME and the adb executable are
private to each test.
"""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_client import StudioError, binary, dispatch  # noqa: E402

FAKE_ADB = r'''
import json, os, shlex, sys
from pathlib import Path
root = Path(os.environ["TOMACCO_TEST_ROOT"])
a = sys.argv[1:]
with (root / "adb-calls.jsonl").open("a") as f:
    f.write(json.dumps(a) + "\n")
if a[:1] != ["-s"]:
    print("List of devices attached")
    sys.exit(0)
serial, rest = a[1], a[2:]
file = root / f"device-{serial}.json"
if not file.exists():
    print(f"adb: device '{serial}' not found", file=sys.stderr)
    sys.exit(1)
d = json.loads(file.read_text())
out = ""
if rest[:1] == ["emu"]:
    d.setdefault("console", []).append(rest[1:])
    out = "OK"
else:
    c = shlex.split(rest[1])
    s = d["settings"]
    if c[:1] == ["getprop"]:
        out = d["props"].get(c[1], "")
    elif c[:2] == ["settings", "get"]:
        out = s.get(c[2] + "/" + c[3], "null")
    elif c[:2] == ["settings", "put"]:
        s[c[2] + "/" + c[3]] = c[4]
    elif c[:2] == ["settings", "delete"]:
        s.pop(c[2] + "/" + c[3], None)
    elif c[:3] == ["cmd", "uimode", "night"]:
        if len(c) > 3:
            d["night"] = c[3]
        out = "Night mode: " + d["night"]
    elif c[:3] == ["cmd", "connectivity", "airplane-mode"]:
        s["global/airplane_mode_on"] = "1" if c[3] == "enable" else "0"
    elif c[:3] == ["cmd", "locale", "get-app-locales"]:
        out = "Locales for %s for user 0 are [%s]" % (c[3], d["locale"])
    elif c[:3] == ["cmd", "locale", "set-app-locales"]:
        d["locale"] = c[5] if len(c) > 5 else ""
    elif c[:3] == ["cmd", "package", "query-services"]:
        out = "".join("priority=0 preferredOrder=0\n  %s\n" % x for x in d["services"])
    else:
        print("unknown " + rest[1], file=sys.stderr)
        sys.exit(1)
file.write_text(json.dumps(d))
print(out)
'''

TALKBACK = "com.google.android.marvin.talkback/.TalkBackService"
SWITCH = "com.example.switch/.SwitchAccessService"


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tomacco-environment-test-")
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"TOMACCO_HOME": str(self.root / "state"), "TOMACCO_TEST_ROOT": str(self.root)})
        self.env.start()
        adb = self.root / "adb"
        adb.write_text("#!" + sys.executable + "\n" + FAKE_ADB)
        adb.chmod(0o755)
        dispatch("settings.configure", {"executables": {"adb": str(adb)}})

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def device(self, serial, sdk="35", services=(SWITCH, TALKBACK), enabled=SWITCH, fingerprint="google/sdk_gphone64/emu64:15/X/1:user/release-keys"):
        (self.root / f"device-{serial}.json").write_text(json.dumps({
            "props": {"ro.serialno": serial, "ro.product.model": "sdk_gphone64_x86_64", "ro.build.fingerprint": fingerprint,
                      "ro.build.version.sdk": sdk},
            "settings": {"global/airplane_mode_on": "0", "secure/enabled_accessibility_services": enabled,
                         "secure/accessibility_enabled": "1" if enabled else "0", "system/font_scale": "1.0"},
            "night": "no", "locale": "", "services": list(services)}))

    def state(self, serial):
        return json.loads((self.root / f"device-{serial}.json").read_text())

    def calls(self):
        path = self.root / "adb-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def writes(self, serial):
        found = []
        for call in self.calls():
            if call[:2] != ["-s", serial]:
                continue
            if call[2] == "emu":
                found.append(" ".join(call[2:]))
                continue
            command = shlex.split(call[3])
            if command[:2] in (["settings", "put"], ["settings", "delete"]) or "airplane-mode" in command \
                    or "set-app-locales" in command or command[:3] == ["cmd", "uimode", "night"] and len(command) > 3:
                found.append(" ".join(command))
        return found

    def save(self, **settings):
        return dispatch("android.environment.preset.save", {"id": "night-qa", "name": "Night QA", "settings": settings})["preset"]

    def test_schema_refuses_a_preset_without_target_and_unknown_settings(self):
        for operation, arguments in [("android.environment.plan", {"preset": "x"}), ("android.environment.apply", {"plan_id": "0" * 32}),
                                     ("android.environment.preset.save", {"id": "x", "name": "X", "settings": {"system_locale": "de"}})]:
            with self.assertRaises(StudioError, msg=operation):
                dispatch(operation, arguments)
        self.assertEqual(self.calls(), [], "nothing reached adb")

    def test_full_cycle_on_a_named_emulator_keeps_other_accessibility_services(self):
        self.device("emulator-5554")
        self.device("emulator-5556")
        self.save(dark_mode="on", font_scale=1.3, animations=False, airplane_mode=True, app_locale="de-DE",
                  location={"latitude": 48.2082, "longitude": 16.3738}, talkback=True)
        before = self.state("emulator-5554")
        plan = dispatch("android.environment.plan", {"preset": "night-qa", "device": "emulator-5554", "bundle_id": "dev.test.app"})
        self.assertEqual(self.writes("emulator-5554"), [], "planning only reads")
        self.assertEqual(plan["unsupported"], [])
        self.assertEqual(plan["not_restorable"], ["location"])
        talkback = next(e for e in plan["entries"] if e["field"] == "talkback")
        self.assertEqual(talkback["current"]["service"], "com.google.android.marvin.talkback/com.google.android.marvin.talkback.TalkBackService")
        self.assertEqual(talkback["current"]["other_enabled_services"], [SWITCH])
        # The plan names its device; another serial cannot use it.
        with self.assertRaises(StudioError) as other:
            dispatch("android.environment.apply", {"plan_id": plan["plan_id"], "device": "emulator-5556"})
        self.assertEqual(other.exception.code, "environment_target_changed")
        self.assertEqual(self.writes("emulator-5556"), [])
        applied = dispatch("android.environment.apply", {"plan_id": plan["plan_id"], "device": "emulator-5554"})
        status = {r["field"]: r["status"] for r in applied["results"]}
        self.assertEqual(status, {"dark_mode": "applied", "font_scale": "applied", "animations": "applied", "airplane_mode": "applied",
                                  "app_locale": "applied", "location": "applied_unverified", "talkback": "applied"})
        after = self.state("emulator-5554")
        self.assertEqual(after["settings"]["secure/enabled_accessibility_services"],
                         SWITCH + ":com.google.android.marvin.talkback/com.google.android.marvin.talkback.TalkBackService")
        self.assertEqual(after["console"], [["geo", "fix", "16.3738", "48.2082"]])
        self.assertEqual(self.state("emulator-5556")["settings"], before["settings"], "the other device is untouched")
        listed = dispatch("android.environment.restores", {"device": "emulator-5554"})["restores"]
        self.assertEqual([r["restore_id"] for r in listed], [applied["restore_id"]])
        restored = dispatch("android.environment.restore", {"restore_id": applied["restore_id"], "device": "emulator-5554"})
        self.assertEqual({r["field"]: r["status"] for r in restored["results"]},
                         {**{k: "restored" for k in status}, "location": "not_restorable"})
        final = self.state("emulator-5554")
        self.assertEqual(final["settings"], before["settings"])
        self.assertEqual((final["night"], final["locale"]), (before["night"], before["locale"]))

    def test_a_phone_without_talkback_reports_unsupported_and_changes_nothing_without_consent(self):
        self.device("R5CT123", sdk="29", services=(SWITCH,))
        self.save(airplane_mode=True, talkback=True, animations=False, location={"latitude": 1, "longitude": 2})
        inspected = dispatch("android.environment.inspect", {"device": "R5CT123"})
        self.assertFalse(inspected["fields"]["talkback"]["supported"])
        self.assertIn("not installed", inspected["fields"]["talkback"]["reason"])
        self.assertIn("screen reader", inspected["talkback_note"])
        plan = dispatch("android.environment.plan", {"preset": "night-qa", "device": "R5CT123"})
        self.assertEqual(plan["unsupported"], ["airplane_mode", "location", "talkback"])
        with self.assertRaises(StudioError) as refused:
            dispatch("android.environment.apply", {"plan_id": plan["plan_id"], "device": "R5CT123"})
        self.assertEqual(refused.exception.code, "environment_unsupported")
        self.assertEqual(self.writes("R5CT123"), [])
        applied = dispatch("android.environment.apply", {"plan_id": plan["plan_id"], "device": "R5CT123", "allow_partial": True})
        self.assertEqual(applied["applied"], ["animations"])
        self.assertTrue(all("animation" in w or "animator" in w for w in self.writes("R5CT123")), self.writes("R5CT123"))
        self.assertEqual(self.state("R5CT123")["settings"]["secure/enabled_accessibility_services"], SWITCH)

    def test_a_reflashed_device_behind_the_same_serial_is_refused(self):
        self.device("emulator-5554")
        self.save(dark_mode="on")
        plan = dispatch("android.environment.plan", {"preset": "night-qa", "device": "emulator-5554"})
        self.device("emulator-5554", fingerprint="google/sdk_gphone64/emu64:16/Y/2:user/release-keys")
        with self.assertRaises(StudioError) as changed:
            dispatch("android.environment.apply", {"plan_id": plan["plan_id"], "device": "emulator-5554"})
        self.assertEqual(changed.exception.code, "environment_target_changed")
        self.assertEqual(self.writes("emulator-5554"), [])

    def test_presets_round_trip_through_a_file_and_revisions_protect_edits(self):
        first = self.save(dark_mode="off", font_scale=1.15)
        with self.assertRaises(StudioError) as taken:
            self.save(dark_mode="on")
        self.assertEqual(taken.exception.code, "environment_conflict")
        target = self.root / "exports" / "night.json"
        target.parent.mkdir()
        exported = dispatch("android.environment.preset.export", {"id": "night-qa", "path": str(target)})
        self.assertEqual(json.loads(target.read_text()), exported["document"])
        self.assertEqual(exported["document"]["format"], "tomacco.environment-preset")
        with self.assertRaises(StudioError):
            dispatch("android.environment.preset.export", {"id": "night-qa", "path": str(target)})
        imported = dispatch("android.environment.preset.import", {"path": str(target), "id": "night-copy"})
        self.assertEqual(imported["preset"]["settings"], first["settings"])
        names = [p["id"] for p in dispatch("android.environment.preset.list", {})["presets"]]
        self.assertEqual(names, ["night-copy", "night-qa"])
        dispatch("android.environment.preset.delete", {"id": "night-copy", "if_revision": 1})
        self.assertEqual([p["id"] for p in dispatch("android.environment.preset.list", {})["presets"]], ["night-qa"])
        self.assertEqual(self.calls(), [], "presets never touch a device")

    def test_capabilities_say_what_is_not_verifiable(self):
        value = dispatch("android.environment.capabilities", {})
        self.assertFalse(value["fields"]["location"]["restorable"])
        self.assertTrue(value["fields"]["talkback"]["keeps_other_services"])
        self.assertEqual(value["fields"]["app_locale"]["minimum_sdk"], 33)
        self.assertIn("tomaccoctl", Path(binary()).name)


if __name__ == "__main__":
    unittest.main()
