"""Actual Rust job lifecycle with synthetic firmware and a process-only helper."""
import json
import os
from pathlib import Path
import time

from test_product import Isolated
from native_client import StudioError, dispatch, jobs
from host_patience import PATIENCE
import test_ios_userland as fixtures


class UserlandJobTests(Isolated):
    def arguments(self):
        fixture = fixtures.UserlandTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        args = vars(fixture.arguments())
        args.pop("component")
        args["components"] = []
        args["seconds"] = 60
        for key in ("helper_kernel",):
            args[key].write_text("Process fixture; not a kernel")
        for key in ("busybox/busybox", "apfs/apfs.ko"):
            source = Path(args["support"]) / key
            source.parent.mkdir()
            source.write_text("Synthetic process fixture")
        engine = args["helper_engine"]
        engine.write_text("#!/bin/sh\necho $$ > \"$0.pid\"\nexec sleep 60\n")
        engine.chmod(0o755)
        return {name: str(value) if isinstance(value, Path) else value for name, value in args.items()}

    def test_exclusive_reuse_conflict_reporting_and_cancellation(self):
        args = self.arguments()
        job = dispatch("ios.emulator.prepare_userland", args)
        pid_file = Path(args["helper_engine"] + ".pid")
        until = time.monotonic() + 8 * PATIENCE
        while not pid_file.is_file() and time.monotonic() < until:
            time.sleep(0.02)
        self.assertTrue(pid_file.is_file(), jobs.logs(job["id"]))
        pid = int(pid_file.read_text())
        repeated = dispatch("ios.emulator.prepare_userland", args)
        self.assertEqual(repeated["id"], job["id"])
        self.assertTrue(repeated["reused"])
        with self.assertRaises(StudioError) as busy:
            dispatch("ios.emulator.prepare_userland", {**args, "seconds": 50})
        self.assertEqual(busy.exception.code, "ios_prepare_busy")
        live = dispatch("ios.emulator.report", {"job_id": job["id"]})
        self.assertEqual(live["report"]["phase"], "gpu-extraction")
        jobs.cancel(job["id"])
        self.assertEqual(jobs.wait(job["id"], 8)["state"], "cancelled")
        final = dispatch("ios.emulator.report", {"job_id": job["id"]})
        self.assertEqual(final["report"]["state"], "cancelled")
        self.assertFalse(final["report"]["qualified_for_app_development"])
        until = time.monotonic() + 2 * PATIENCE
        while Path(f"/proc/{pid}").exists() and time.monotonic() < until:
            time.sleep(0.02)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_manifest_failure_is_reported_and_existing_output_is_preserved(self):
        args = self.arguments()
        (Path(args["firmware"]) / "BuildManifest.plist").write_bytes(b"not a manifest")
        job = dispatch("ios.emulator.prepare_userland", args)
        self.assertEqual(jobs.wait(job["id"], 8)["state"], "failed")
        report = dispatch("ios.emulator.report", {"job_id": job["id"]})
        self.assertEqual(report["report"]["state"], "failed")
        self.assertIn("property list", report["report"]["error"])
        receipt = Path(job["userland"])
        before = receipt.read_bytes()
        with self.assertRaises(StudioError) as existing:
            dispatch("ios.emulator.prepare_userland", args)
        self.assertEqual(existing.exception.code, "output_exists")
        self.assertEqual(receipt.read_bytes(), before)
        receipt.unlink()
        outside = self.root / "foreign-report.json"
        outside.write_text(json.dumps({"state": "prepared"}))
        receipt.symlink_to(outside)
        with self.assertRaises(StudioError) as unsafe:
            dispatch("ios.emulator.report", {"job_id": job["id"]})
        self.assertEqual(unsafe.exception.code, "invalid_report_path")

    def test_duplicate_component_rejected_before_submission(self):
        args = self.arguments()
        entry = {"component": "OS", "path": str(Path(args["firmware"]) / "OS.bin")}
        with self.assertRaises(StudioError) as duplicate:
            dispatch("ios.emulator.prepare_userland", {**args, "components": [entry, entry]})
        self.assertEqual(duplicate.exception.code, "duplicate_component")
        self.assertFalse(Path(args["output"]).exists())
