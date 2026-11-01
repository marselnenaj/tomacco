"""The profile half of ios.virtual.doctor over profile directories made here; no guest runs."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
import ios_run_verdict
import ios_virtual_doctor as doctor
from native_client import dispatch
from test_product import Isolated


def sha(data):
    return hashlib.sha256(data).hexdigest()


class VirtualDoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tomacco-doctor-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "profile"
        self.directory.mkdir()
        self.base = self.root / "installs/guest-install4"
        self.base.mkdir(parents=True)
        self.profile = {"format": 1, "prepare": {}, "boot": {}, "engine": str(self.root / "engine"),
                        "graphics": str(self.root / "graphics"), "tools": str(self.root / "tools"),
                        "installs": str(self.root / "installs")}
        self.state = {"run": 4, "closed": True, "install": str(self.base), "tools": str(self.root / "tools"),
                      "verified": {"run": 4, "install": str(self.base)}}
        self.write()

    def write(self):
        (self.directory / "profile.json").write_text(json.dumps(self.profile))
        (self.directory / "state.json").write_text(json.dumps(self.state))

    def check(self, name, patches=()):
        return next(row for row in doctor.doctor(self.directory, list(patches))["checks"] if row["check"] == name)

    def test_every_row_has_the_host_checks_shape(self):
        checks = doctor.doctor(self.directory, [])["checks"]
        self.assertEqual([row["check"] for row in checks],
                         ["profile_inputs", "guest_tools_sources", "engine_patches", "saved_states", "last_run"])
        for row in checks:
            self.assertEqual(set(row), {"check", "status", "found", "message", "remedy"})
            self.assertIn(row["status"], ("pass", "warn", "fail"))
            self.assertEqual(row["remedy"] is None, row["status"] == "pass", row)

    def test_missing_inputs_fail_with_the_command_that_makes_each(self):
        row = self.check("profile_inputs")
        self.assertEqual(row["status"], "fail")
        problems = {item["input"]: item for item in row["found"]["problems"]}
        self.assertEqual(problems["engine"]["problem"], "not a file")
        self.assertIn("build_ios_paravirt.py", problems["engine"]["make"])
        self.assertEqual(problems["SPTM"]["problem"], "not named in profile.json")
        # The helper kernel has a fallback, so alone it would only warn.
        self.assertEqual(problems["helper kernel"]["severity"], "warn")
        # The verified base lacks its disks.
        self.assertIn("lacks", problems["verified base"]["problem"])
        self.assertIn("first:", row["message"])

    def test_guest_tools_built_from_other_sources_are_named(self):
        tools = self.root / "tools"
        tools.mkdir()
        source = sorted(path.name for path in (REPO / "integrations/ios-emulator").iterdir() if path.is_file())[0]
        actual = sha((REPO / "integrations/ios-emulator" / source).read_bytes())
        (tools / "guest-tools.json").write_text(json.dumps({"sources": {source: actual}}))
        self.assertEqual(self.check("guest_tools_sources")["status"], "pass")
        (tools / "guest-tools.json").write_text(json.dumps({"sources": {source: "0" * 64}}))
        row = self.check("guest_tools_sources")
        self.assertEqual((row["status"], row["found"]["changed"]), ("warn", [source]))
        self.assertIn("ios.emulator.build_guest_tools", row["remedy"])

    def test_an_engine_built_from_other_patches_is_named(self):
        engine = self.root / "engine"
        engine.write_bytes(b"engine bytes")
        patch_a, patch_b = self.root / "a.patch", self.root / "b.patch"
        patch_a.write_bytes(b"a")
        patch_b.write_bytes(b"b")
        self.assertEqual(self.check("engine_patches", [patch_a])["status"], "warn")  # no record yet
        (self.root / "build-manifest.json").write_text(json.dumps({
            "engine_sha256": sha(b"engine bytes"),
            "ordered_patch_contract": {"engine": [{"name": "a.patch", "sha256": sha(b"a")}]}}))
        self.assertEqual(self.check("engine_patches", [patch_a])["status"], "pass")
        patch_a.write_bytes(b"a, changed")
        row = self.check("engine_patches", [patch_a, patch_b])
        self.assertEqual(row["status"], "warn")
        self.assertEqual(row["found"]["differences"], ["a.patch changed", "b.patch is new"])
        self.assertIn("build_ios_paravirt.py", row["remedy"])
        # A record of other bytes does not describe this engine.
        engine.write_bytes(b"rebuilt")
        self.assertIn("No build-manifest.json", self.check("engine_patches", [patch_a])["message"])

    def test_saved_states_whose_disks_are_gone_fail(self):
        self.assertEqual(self.check("saved_states")["status"], "pass")
        kept = self.root / "installs/guest-install3"
        kept.mkdir()
        (self.directory / "saved-states.json").write_text(json.dumps({"states": {
            "clean": {"install": str(kept)}, "old": {"install": str(self.root / "installs/guest-install1")}}}))
        row = self.check("saved_states")
        self.assertEqual((row["status"], row["found"]["states"]), ("fail", ["clean", "old"]))
        self.assertTrue(row["message"].startswith("Old: its disks"))
        self.assertIn("ios.virtual.state_delete", row["remedy"])
        (self.directory / "saved-states.json").write_text("not json")
        self.assertIn("cannot be read", self.check("saved_states")["message"])

    def test_the_last_run_is_read_from_its_verdict(self):
        self.assertEqual(self.check("last_run")["status"], "pass")
        self.state["verdict"] = ios_run_verdict.explain({"outcome": "emulator-exited", "exit_code": 0,
                                                         "storage_hashes_complete": True}, {})
        self.write()
        row = self.check("last_run")
        self.assertEqual(row["status"], "warn")
        self.assertIn("not verified", row["message"])
        self.assertIn("ios.virtual.stop", row["remedy"])
        self.state["verdict"] = ios_run_verdict.explain({"outcome": "bounded-session", "exit_code": 0,
                                                         "storage_hashes_complete": True},
                                                        {"guest_storage_flushed": True})
        self.write()
        self.assertEqual(self.check("last_run")["status"], "pass")

    def test_the_command_prints_one_json_answer(self):
        done = subprocess.run([sys.executable, str(REPO / "tools/ios_virtual_doctor.py"), "--profile",
                               str(self.directory)], capture_output=True, text=True, timeout=120)
        answer = json.loads(done.stdout)
        self.assertTrue(answer["ok"])
        self.assertEqual(len(answer["checks"]), 5)
        done = subprocess.run([sys.executable, str(REPO / "tools/ios_virtual_doctor.py"), "--profile",
                               str(self.root / "missing")], capture_output=True, text=True, timeout=120)
        self.assertEqual((done.returncode, json.loads(done.stdout)["ok"]), (1, False))


class VirtualDoctorOperationTests(Isolated):
    """ios.virtual.doctor through tomaccoctl: the machine's items always, the profile's once one is configured."""

    def test_the_report_joins_the_machine_the_profile_and_the_next_start(self):
        answer = dispatch("ios.virtual.doctor")
        self.assertEqual(answer["checks"][0]["check"], "profile")
        self.assertEqual(answer["checks"][0]["status"], "fail")
        self.assertFalse(answer["ready"])
        names = [row["check"] for row in answer["checks"]]
        for name in ("architecture", "memory", "graphics", "gpu_now", "pressure_now"):
            self.assertIn(name, names)
        directory = self.root / "profile"
        (directory / "installs").mkdir(parents=True)
        (directory / "profile.json").write_text(json.dumps({
            "format": 1, "prepare": {}, "boot": {}, "graphics": "/graphics", "engine": "/engine.py",
            "application": "app.example", "installs": str(directory / "installs")}))
        (directory / "state.json").write_text(json.dumps({
            "run": 3, "install": str(self.root / "missing-install"), "tools": str(self.root / "missing-tools"),
            "closed": True, "guest_job": None}))
        dispatch("ios.virtual.configure", {"directory": str(directory)})
        answer = dispatch("ios.virtual.doctor")
        rows = {row["check"]: row for row in answer["checks"]}
        self.assertEqual(rows["profile"]["status"], "pass")
        self.assertEqual(rows["profile_inputs"]["status"], "fail")
        self.assertIn("engine", {item["input"] for item in rows["profile_inputs"]["found"]["problems"]})
        for name in ("guest_tools_sources", "engine_patches", "saved_states", "last_run", "start_disk"):
            self.assertIn(name, rows)
        self.assertEqual(answer["directory"], str(directory))
        self.assertEqual(answer["status"], "fail")
        for row in answer["checks"]:
            self.assertIn(row["status"], ("pass", "warn", "fail"), row)


if __name__ == "__main__":
    unittest.main()
