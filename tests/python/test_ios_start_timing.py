"""Where a start or stop spent its time, from synthetic step logs; no phone runs."""
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import ios_start_timing as timing
from ios_virtual_iphone import Phone
import prepare_ios_guest_tools


def steps(result):
    return [(row["step"], row["seconds"]) for row in result["steps"]]


class StartBreakdown(unittest.TestCase):
    # A copy start: 5 s of checks, 3 s until the copy, a 300 s copy, a 40 s
    # helper, 2 s to the boot request, a launcher that hashed for 120 s of
    # its 125 s, a 180 s boot, an app install, settings, unlock and launch.
    marks = {"begin": 1000.0, "preparing": 1005.0, "booting": 1350.0, "guest_ready": 1655.0,
             "applications_installed": 1675.0, "settings_applied": 1690.0, "launched": 1720.0,
             "finished": 1730.0}
    preparation = {"root_disk": {"method": "copy"}, "step_times": [
        {"step": "validate-payloads", "started": 1006.0}, {"step": "copy-storage", "started": 1008.0},
        {"step": "install-preboot", "started": 1308.0}, {"step": "complete", "started": 1348.0}]}
    request = {"timing": {"input_hashing_seconds": 120.0, "input_hashing_bytes": 34359738368,
                          "emulator_started": 1475.0}}

    def test_every_step_of_a_start_in_order_with_its_seconds(self):
        result = timing.start_breakdown(self.marks, self.preparation, self.request)
        self.assertEqual(steps(result), [
            ("checks", 5.0), ("preparation", 3.0), ("disk_copy", 300.0), ("guest_tools", 40.0),
            ("boot_request", 2.0), ("input_hashing", 120.0), ("launcher", 5.0), ("boot", 180.0),
            ("app_install", 20.0), ("guest_settings", 15.0), ("unlock_and_launch", 30.0),
            ("display_check", 10.0)])
        self.assertEqual(result["total_seconds"], 730.0)
        self.assertEqual(sum(row["seconds"] for row in result["steps"]), 730.0)
        self.assertEqual(result["slowest"], "disk_copy")
        copy = next(row for row in result["steps"] if row["step"] == "disk_copy")
        self.assertEqual(copy["method"], "copy")
        hashing = next(row for row in result["steps"] if row["step"] == "input_hashing")
        self.assertEqual(hashing["bytes"], 34359738368)

    def test_unknown_marks_fold_into_the_next_step_and_say_so(self):
        # No preparation record and an old launcher without timing.
        result = timing.start_breakdown(self.marks, None, {"command": []})
        self.assertEqual(steps(result)[:3], [("checks", 5.0), ("boot_request", 345.0), ("boot", 305.0)])
        self.assertEqual(result["steps"][1]["includes"], ["preparation", "disk_copy", "guest_tools"])
        self.assertEqual(result["steps"][2]["includes"], ["launcher"])
        self.assertEqual(result["total_seconds"], 730.0)

    def test_a_failed_start_has_the_steps_it_reached(self):
        marks = {"begin": 10.0, "preparing": 12.0, "finished": 20.0}
        record = {"step_times": [{"step": "copy-storage", "started": 13.0}]}
        result = timing.start_breakdown(marks, record, None)
        self.assertEqual(steps(result), [("checks", 2.0), ("preparation", 1.0), ("display_check", 7.0)])
        self.assertEqual(result["steps"][-1]["includes"], ["disk_copy", "guest_tools", "boot_request",
                                                           "launcher", "boot", "app_install", "guest_settings",
                                                           "unlock_and_launch"])

    def test_values_of_another_shape_are_ignored(self):
        marks = {"begin": 0.0, "preparing": "late", "booting": float("nan"), "finished": True, "launched": 4.0}
        record = {"step_times": [{"step": "copy-storage", "started": None}, "x", {"started": 1.0}]}
        request = {"timing": {"input_hashing_seconds": "120", "emulator_started": float("inf")}}
        result = timing.start_breakdown(marks, record, request)
        self.assertEqual(result["total_seconds"], 4.0)
        self.assertEqual([row["step"] for row in result["steps"]], ["unlock_and_launch"])
        self.assertEqual(timing.start_breakdown({}, "x", [])["steps"], [])

    def test_hashing_longer_than_its_interval_is_capped(self):
        request = {"timing": {"input_hashing_seconds": 999.0, "emulator_started": 1360.0}}
        result = timing.start_breakdown({"begin": 1340.0, "booting": 1350.0, "guest_ready": 1400.0}, None, request)
        self.assertEqual(steps(result), [("boot_request", 10.0), ("input_hashing", 10.0), ("launcher", 0.0),
                                         ("boot", 40.0)])


class StopBreakdown(unittest.TestCase):
    def test_guest_exit_output_hashing_and_the_rest(self):
        report = {"timing": {"engine_exited": 130.0, "output_hashing_seconds": 250.0,
                             "output_hashing_bytes": 1 << 35}}
        result = timing.stop_breakdown(100.0, report, 385.0)
        self.assertEqual(steps(result), [("guest_exit", 30.0), ("output_hashing", 250.0), ("finalize", 5.0)])
        self.assertEqual(result["slowest"], "output_hashing")
        self.assertEqual(result["total_seconds"], 285.0)
        self.assertEqual(result["steps"][1]["bytes"], 1 << 35)

    def test_a_guest_that_ended_before_the_stop_has_no_exit_step(self):
        report = {"timing": {"engine_exited": 50.0, "output_hashing_seconds": 20.0}}
        self.assertEqual(steps(timing.stop_breakdown(100.0, report, 101.0)), [("output_hashing", 20.0)])

    def test_a_report_without_timing_gives_only_the_total(self):
        result = timing.stop_breakdown(100.0, {"outcome": "x"}, 160.0)
        self.assertEqual((result["steps"], result["total_seconds"], result["slowest"]), ([], 60.0, None))


class PreparationStepTimes(unittest.TestCase):
    def test_each_step_of_the_preparation_is_logged_once_with_its_start(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            args = SimpleNamespace(output=output, disk=Path(directory) / "disk", auxiliary_disk=None,
                                   sep_storage=None, seconds=60)
            refused = {"admitted": False}
            before = time.time()
            with patch.object(prepare_ios_guest_tools, "wait_for_host_pressure", return_value=refused), \
                    patch.object(prepare_ios_guest_tools, "pressure_error", return_value="busy"), \
                    self.assertRaisesRegex(RuntimeError, "busy"):
                prepare_ios_guest_tools.prepare(args)
            record = json.loads((output / "guest-tools-install.json").read_text())
        self.assertEqual([row["step"] for row in record["step_times"]],
                         ["validate-payloads", "host-pressure-admission"])
        started = [row["started"] for row in record["step_times"]]
        self.assertTrue(before <= started[0] <= started[1] <= time.time())


class PhoneRecordsTiming(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tomacco-timing-")
        self.root = Path(self.temp.name)
        self.installs = self.root / "installs"
        (self.installs / "guest-install3").mkdir(parents=True)
        (self.root / "profile.json").write_text(json.dumps({"format": 1, "installs": str(self.installs)}))
        (self.root / "state.json").write_text(json.dumps({
            "run": 3, "closed": False, "guest_job": "job", "install": str(self.installs / "guest-install3")}))
        self.phone = Phone("/unused", self.root)
        self.addCleanup(self.temp.cleanup)
        patcher = patch("ios_virtual_iphone.emit", lambda *args, **kwargs: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_start_keeps_its_steps_in_the_state_and_beside_the_run(self):
        request = self.root / "report" / "paravirtual"
        request.mkdir(parents=True)
        (request / "request.json").write_text(json.dumps(StartBreakdown.request))
        calls = []
        def invoke(operation, arguments, *args, **kwargs):
            calls.append(operation)
            return {"ok": True, "result": {"report_directory": str(self.root / "report")}}
        self.phone.invoke = invoke
        self.phone.marks = dict(StartBreakdown.marks)
        self.phone.preparation = StartBreakdown.preparation
        self.phone.record_start_timing()
        self.assertEqual(calls, ["ios.emulator.report"])
        saved = json.loads((self.root / "state.json").read_text())["start_timing"]
        self.assertEqual((saved["run"], saved["total_seconds"], saved["slowest"]), (3, 730.0, "disk_copy"))
        beside = json.loads((self.installs / "guest-install3" / "timing.json").read_text())
        self.assertEqual(beside["start"], saved)
        self.assertEqual((self.installs / "guest-install3" / "timing.json").stat().st_mode & 0o777, 0o600)
        # A stop adds its own steps to the same file.
        self.phone.record_timing("stop", timing.stop_breakdown(1.0, {}, 2.0))
        beside = json.loads((self.installs / "guest-install3" / "timing.json").read_text())
        self.assertEqual(set(beside), {"start", "stop"})

    def test_an_unreadable_launcher_report_costs_only_its_steps(self):
        def invoke(*_args, **_kwargs):
            raise RuntimeError("no control plane")
        self.phone.invoke = invoke
        self.phone.marks = {"begin": 1.0, "booting": 2.0, "guest_ready": 5.0, "finished": 5.0}
        self.phone.record_start_timing()
        saved = json.loads((self.root / "state.json").read_text())["start_timing"]
        self.assertEqual(steps(saved), [("boot_request", 1.0), ("boot", 3.0), ("display_check", 0.0)])

    def test_nothing_is_written_outside_an_owned_install(self):
        outside = self.root / "elsewhere"
        outside.mkdir()
        self.phone.state["install"] = str(outside)
        self.phone.record_timing("stop", timing.stop_breakdown(1.0, {}, 2.0))
        self.assertFalse((outside / "timing.json").exists())
        self.assertIn("stop_timing", json.loads((self.root / "state.json").read_text()))

    def test_a_start_that_fails_still_records_where_it_was(self):
        self.phone.start_steps = lambda *_: (_ for _ in ()).throw(RuntimeError("refused"))
        self.phone.launcher_request = lambda: None
        with self.assertRaisesRegex(RuntimeError, "refused"):
            self.phone.start(None, True)
        saved = json.loads((self.root / "state.json").read_text())["start_timing"]
        self.assertIsNotNone(saved["total_seconds"])


if __name__ == "__main__":
    unittest.main()
