"""The factor by which the host tests stretch their stand-in process bounds on a loaded host."""
from pathlib import Path
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import host_patience
import native_client


class PatienceDerivation(unittest.TestCase):
    def test_a_host_whose_load_is_within_its_cpus_waits_as_before(self):
        for load, cpus in ((0.0, 1), (11.2, 32), (32.0, 32), (3.99, 4)):
            self.assertEqual(host_patience.derive(load, cpus), 1, (load, cpus))

    def test_a_loaded_host_waits_the_load_per_cpu_rounded_up_at_most_eight_times(self):
        self.assertEqual(host_patience.derive(32.5, 32), 2)
        self.assertEqual(host_patience.derive(70.0, 32), 3)
        self.assertEqual(host_patience.derive(9.0, 2), 5)
        self.assertEqual(host_patience.derive(900.0, 32), host_patience.LIMIT)
        # A CPU count that cannot be read counts as one CPU.
        self.assertEqual(host_patience.derive(5.0, 0), 5)
        self.assertEqual(host_patience.derive(5.0, None), 5)

    def test_the_factor_can_be_pinned_and_a_bad_pin_is_refused(self):
        self.assertEqual(host_patience.derive(900.0, 32, "1"), 1)
        self.assertEqual(host_patience.derive(0.0, 32, " 3 "), 3)
        # An empty variable is the same as an unset one.
        self.assertEqual(host_patience.derive(900.0, 32, ""), host_patience.LIMIT)
        for bad in ("0", "-2", "1.5", "slow"):
            with self.assertRaises(ValueError, msg=bad):
                host_patience.derive(0.0, 32, bad)

    def test_the_load_is_read_from_loadavg_once_and_an_unreadable_file_counts_as_quiet(self):
        with tempfile.TemporaryDirectory() as directory:
            loadavg = Path(directory) / "loadavg"
            loadavg.write_text("66.52 61.03 50.11 7/5160 3445686\n")
            self.assertEqual(host_patience.host_load(loadavg), 66.52)
            self.assertEqual(host_patience.patience({}, loadavg, 32), 3)
            self.assertEqual(host_patience.patience({"TOMACCO_TEST_PATIENCE": "2"}, loadavg, 32), 2)
            self.assertEqual(host_patience.patience({}, Path(directory) / "absent", 32), 1)
            loadavg.write_text("garbage\n")
            self.assertEqual(host_patience.patience({}, loadavg, 32), 1)
        # The suite reads one factor at import, a whole number of at least one.
        self.assertIsInstance(host_patience.PATIENCE, int)
        self.assertGreaterEqual(host_patience.PATIENCE, 1)


class PatienceApplication(unittest.TestCase):
    """The waits of the stand-in control plane client stretch by the factor
    within the product's own bound: job.wait takes at most 60 s in one call
    (assets/operations.json; a larger value is refused as out of range), so
    a longer wait repeats the call until the job ends or the stretched
    seconds are spent. The seconds stay whole numbers for the control plane."""

    def stand_in(self, calls, states=()):
        states = iter(states)

        def dispatch(operation, arguments=None):
            calls.append((operation, arguments))
            return {"state": next(states, "running")}
        return dispatch

    def test_a_stretched_wait_repeats_within_the_products_bound_until_its_seconds_are_spent(self):
        calls = []
        with patch.object(native_client, "dispatch", self.stand_in(calls)), patch.object(native_client, "PATIENCE", 3):
            self.assertEqual(native_client.jobs.wait("a" * 32, 5)["state"], "running")
            native_client.jobs.wait("b" * 32)
            native_client.jobs.wait("c" * 32, 60)
        self.assertEqual({operation for operation, _ in calls}, {"job.wait"})
        self.assertEqual([arguments["job_id"] for _, arguments in calls], ["a" * 32] + ["b" * 32] * 2 + ["c" * 32] * 3)
        self.assertEqual([arguments["seconds"] for _, arguments in calls], [15, 60, 30, 60, 60, 60])
        self.assertLessEqual(max(arguments["seconds"] for _, arguments in calls), native_client.Jobs.LONGEST)

    def test_a_job_that_ends_stops_the_repeated_wait(self):
        calls = []
        with patch.object(native_client, "dispatch", self.stand_in(calls, ["running", "failed", "unreachable"])), \
                patch.object(native_client, "PATIENCE", 4):
            self.assertEqual(native_client.jobs.wait("a" * 32, 60)["state"], "failed")
        self.assertEqual(calls, [("job.wait", {"job_id": "a" * 32, "seconds": 60})] * 2)

    def test_an_unloaded_host_waits_once_as_before(self):
        calls = []
        with patch.object(native_client, "dispatch", self.stand_in(calls)), patch.object(native_client, "PATIENCE", 1):
            native_client.jobs.wait("a" * 32, 60)
            native_client.jobs.wait("b" * 32, 5)
            native_client.jobs.wait("c" * 32, 0)
        self.assertEqual([arguments["seconds"] for _, arguments in calls], [60, 5, 0])

    def test_the_control_plane_accepts_every_call_of_a_stretched_wait(self):
        # Against the real control plane in a home of its own: job.wait takes
        # at most 60 s in one call (assets/operations.json), and a call beyond
        # that is refused before the job store is consulted, so an unknown
        # job answers job_not_found only when the stretched wait's call passed
        # validation.
        with tempfile.TemporaryDirectory(prefix="tomacco-patience-") as home, \
                patch.dict(os.environ, {"TOMACCO_HOME": home}), patch.object(native_client, "PATIENCE", 2):
            with self.assertRaises(native_client.StudioError) as refused:
                native_client.dispatch("job.wait", {"job_id": "0" * 32, "seconds": 120})
            self.assertEqual(refused.exception.code, "invalid_argument")
            self.assertIn("out of range", str(refused.exception))
            with self.assertRaises(native_client.StudioError) as unknown:
                native_client.jobs.wait("0" * 32, 60)
            self.assertEqual(unknown.exception.code, "job_not_found")

    def test_the_clients_own_process_bound_is_stretched_by_the_factor(self):
        seen = []

        def run(argv, **options):
            seen.append(options["timeout"])
            return types.SimpleNamespace(stdout='{"ok": true, "result": {"id": "x"}}', stderr="", returncode=0)

        with patch.object(native_client.subprocess, "run", run), patch.object(native_client, "PATIENCE", 3):
            self.assertEqual(native_client.dispatch("job.get", {"job_id": "a" * 32}), {"id": "x"})
        self.assertEqual(seen, [300])


if __name__ == "__main__":
    unittest.main()
