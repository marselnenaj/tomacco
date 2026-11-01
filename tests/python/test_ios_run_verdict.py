"""Plain-word verdicts of finished virtual iPhone runs, over recorded classifier inputs."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import ios_run_verdict
import run_ios_paravirt

GIB = 1 << 30


def evidence(**changes):
    """guest_evidence as guest_evidence() writes it for a quiet run."""
    record = {"kernel_panic": None, "restore_failures": [], "guest_control_exec_failures": [],
              "unaccounted_failures": 0, "accounted_failures": 0, "userspace_failures": [],
              "bus_faults": [], "system_register_faults": [], "device_errors": []}
    record.update(changes)
    return record


# Launcher records as run_ios_paravirt fills them before it classifies a run,
# one per outcome run_outcome can return.
RECORDS = {
    "bounded-session": {"guest_evidence": evidence(), "exit_code": 0, "registers": {}},
    "emulator-exited": {"guest_evidence": evidence(), "exit_code": 0},
    "interrupted": {"guest_evidence": evidence(), "exit_code": -15, "interrupted": True},
    "observation-error": {"guest_evidence": evidence(), "exit_code": 0,
                          "observation_error": "Guest control backend exited during the session"},
    "kernel-panic": {"guest_evidence": evidence(kernel_panic="panic(cpu 2 caller 0xfffffff0): watchdog"),
                     "exit_code": 0},
    "restore-failed": {"guest_evidence": evidence(restore_failures=["mount"]), "exit_code": 0},
    "guest-control-startup-failed": {"guest_evidence": evidence(guest_control_exec_failures=["exec"]),
                                     "exit_code": 0},
    "guest-process-crash": {"guest_evidence": evidence(unaccounted_failures=2, userspace_failures=[
        {"service": "com.apple.Preferences", "kind": "SIGTRAP", "count": 1, "accounted": None},
        {"service": "com.apple.weather.widget", "kind": "extension-watchdog", "count": 6,
         "accounted": "widget-render-deadline"},
        {"service": "dev.example.app", "kind": "SIGSEGV", "count": 1, "accounted": None}]),
        "exit_code": 0, "registers": {}},
    "hardware-fault": {"guest_evidence": evidence(bus_faults=["0x1"]), "exit_code": 0},
    "device-error": {"guest_evidence": evidence(device_errors=["usb: reset"]), "exit_code": 0},
    "emulator-error": {"guest_evidence": evidence(), "exit_code": 134},
}

FLUSHED = {"format": 1, "guest_storage_flushed": True}


def report(outcome, **changes):
    """The report ios.emulator.report returns after run_outcome classified the record."""
    record = dict(RECORDS[outcome])
    record["outcome"] = run_ios_paravirt.run_outcome(record)
    record.setdefault("storage_hashes_complete", True)
    record.update(changes)
    return record


class RunVerdictTests(unittest.TestCase):
    def test_every_classifier_outcome_has_words(self):
        for outcome in RECORDS:
            with self.subTest(outcome=outcome):
                self.assertEqual(run_ios_paravirt.run_outcome(RECORDS[outcome]), outcome)
                self.assertIn(outcome, ios_run_verdict.OUTCOMES)
        # The launcher's refusal under host pressure and the two outcomes the
        # phone records itself have words too.
        for outcome in ("host-pressure-blocked", "job-unknown", "cancelled-before-boot"):
            self.assertIn(outcome, ios_run_verdict.OUTCOMES)
        # Every outcome but a clean one has a remedy.
        for outcome, (summary, remedy) in ios_run_verdict.OUTCOMES.items():
            self.assertTrue(summary.endswith("."), outcome)
            if outcome not in ios_run_verdict.GOOD_OUTCOMES + ("cancelled-before-boot",):
                self.assertTrue(remedy, outcome)

    def test_a_clean_stop_is_verified_with_nothing_to_do(self):
        verdict = ios_run_verdict.explain(report("bounded-session"), FLUSHED)
        self.assertEqual(verdict, {"verified": True, "outcome": "bounded-session",
                                   "summary": "The session ran until it was stopped or reached its time limit.",
                                   "reasons": [], "remedy": None})

    def test_a_crash_the_emulator_does_not_cause_is_named_even_when_the_disks_were_saved(self):
        verdict = ios_run_verdict.explain(report("guest-process-crash"), FLUSHED)
        self.assertTrue(verdict["verified"])
        self.assertIn("Unaccounted: com.apple.Preferences, dev.example.app.", verdict["summary"])
        self.assertNotIn("weather", verdict["summary"])
        self.assertIn("ios.virtual.crash_reports", verdict["remedy"])

    def test_each_missing_proof_is_a_reason_of_its_own(self):
        # The guest ended on its own: no stop record at all.
        verdict = ios_run_verdict.explain(report("emulator-exited"), {})
        self.assertFalse(verdict["verified"])
        self.assertEqual(len(verdict["reasons"]), 1)
        self.assertIn("No stop asked the guest to flush", verdict["reasons"][0])
        self.assertIn("ios.virtual.stop", verdict["remedy"])
        self.assertTrue(verdict["remedy"].endswith("The next start uses the last verified run."))
        # A stop that was taken back, an exit code and incomplete hashes.
        verdict = ios_run_verdict.explain(
            report("emulator-error", storage_hashes_complete=False, storage_hash_error="EIO reading system.raw"),
            {"guest_storage_flushed": False, "error": "ios_control_ended"})
        self.assertEqual(verdict["summary"], "The emulator exited with exit code 134.")
        self.assertEqual(verdict["reasons"], [
            "The emulator exited with exit code 134, not 0.",
            "The disks' hashes after the run are incomplete (EIO reading system.raw), so the saved state "
            "cannot be checked.",
            "The stop could not confirm that the guest flushed its storage (ios_control_ended)."])
        # Fields of the wrong type prove nothing.
        verdict = ios_run_verdict.explain({"outcome": "bounded-session", "exit_code": "0",
                                           "storage_hashes_complete": "yes"}, {"guest_storage_flushed": 1})
        self.assertFalse(verdict["verified"])
        self.assertEqual(len(verdict["reasons"]), 3)

    def test_the_graphics_memory_guard_says_how_full_the_card_was(self):
        guarded = report("bounded-session", vram_guard_stop={"used": 15 * GIB, "total": 16 * GIB,
                                                             "elapsed_seconds": 900})
        verdict = ios_run_verdict.explain(guarded, FLUSHED)
        self.assertTrue(verdict["verified"])
        self.assertIn("(15.0 GiB of 16.0 GiB in use)", verdict["reasons"][0])
        self.assertIn("host.gpu", verdict["remedy"])
        verdict = ios_run_verdict.explain({**guarded, "exit_code": None}, {})
        self.assertFalse(verdict["verified"])
        self.assertEqual(len(verdict["reasons"]), 3)

    def test_runs_without_a_report_or_without_a_job(self):
        verdict = ios_run_verdict.explain({}, {})
        self.assertEqual(verdict["summary"], "The run left no final report.")
        self.assertFalse(verdict["verified"])
        for outcome in ("job-unknown", "cancelled-before-boot"):
            verdict = ios_run_verdict.explain({"outcome": outcome}, {})
            self.assertEqual(verdict["reasons"], [ios_run_verdict.OUTCOMES[outcome][0]])
            self.assertEqual(ios_run_verdict.sentence(verdict), ios_run_verdict.OUTCOMES[outcome][0])
        verdict = ios_run_verdict.explain({"outcome": "something-new", "exit_code": 0}, {})
        self.assertIn("something-new", verdict["summary"])
        verdict = ios_run_verdict.explain(report("observation-error"), FLUSHED)
        self.assertIn("Guest control backend exited", verdict["summary"])
        self.assertIn("ios.emulator.report", verdict["remedy"])


if __name__ == "__main__":
    unittest.main()
