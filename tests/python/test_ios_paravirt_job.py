"""Compiled agent job lifecycle; process fixtures are not iOS boot evidence."""
import json
from pathlib import Path
import time
import unittest

from test_product import Isolated
from test_ios_paravirt import node, prep, source_tree, storage_tree, variant_tree
from native_client import StudioError, dispatch, jobs
from host_patience import PATIENCE


class ParavirtualJobTests(Isolated):
    def arguments(self, *, wait=False):
        engine = self.root / "process-fixture"
        engine.write_text("#!/bin/sh\n" + ("exec sleep 60\n" if wait else "exit 0\n"))
        engine.chmod(0o755)
        tree = self.root / "tree"
        tree.write_bytes(prep.prepare(source_tree().encode()))
        payload = self.root / "synthetic-input"
        payload.write_bytes(b"Not firmware: process admission fixture")
        return {"engine": str(engine), "device_tree": str(tree), "seconds": 10,
                **{name: str(payload) for name in ("kernel", "sptm", "txm", "ramdisk", "trustcache")}}

    def test_agent_job_retains_report_but_never_qualifies_a_successful_process_as_an_app_runtime(self):
        job = dispatch("ios.emulator.paravirtual", self.arguments())
        done = jobs.wait(job["id"], 10)
        self.assertEqual(done["state"], "succeeded", jobs.logs(job["id"]))
        report = dispatch("ios.emulator.report", {"job_id": job["id"]})
        self.assertEqual(report["report"]["outcome"], "emulator-exited")
        self.assertFalse(report["report"]["qualified_for_app_development"])
        self.assertFalse(report["report"]["guest_evidence"]["launchd_started"])
        self.assertIsNone(report["report"]["guest_evidence"]["sealed_system_root"])
        directory = Path(report["report_directory"]) / "paravirtual"
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertTrue(any(Path(a["path"]).name == "result.json" for a in done["result"]["artifacts"]))
        # Reporting must reject an externally substituted receipt.
        target = directory / "result.json"
        target.unlink()
        foreign = self.root / "foreign.json"
        foreign.write_text("{}")
        target.symlink_to(foreign)
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.report", {"job_id": job["id"]})
        self.assertEqual(error.exception.code, "invalid_report_path")

    def test_device_model_brings_its_panel_and_reaches_the_guests_device_tree(self):
        arguments = self.arguments()
        tree = self.root / "variant-tree"
        tree.write_bytes(prep.prepare(variant_tree().encode()))
        arguments["device_tree"] = str(tree)
        job = dispatch("ios.emulator.paravirtual", {**arguments, "device_model": "16"})
        argv = job["plan"]["steps"][0]["argv"]
        self.assertEqual(json.loads(argv[argv.index("--device-model") + 1]), {"variant": "d47vm2025vre"})
        self.assertEqual(argv[argv.index("--display-profile") + 1], "1179x2556@60:65x141")
        # Not an interactive session with guest control: nothing saves itself.
        self.assertNotIn("--cli", argv)
        done = jobs.wait(job["id"], 10)
        self.assertEqual(done["state"], "succeeded", jobs.logs(job["id"]))
        report = dispatch("ios.emulator.report", {"job_id": job["id"]})["report"]
        self.assertEqual(report["device_model"]["variant"], "d47vm2025vre")
        self.assertFalse(report["device_model"]["home_button"])
        self.assertEqual(report["display_profile"]["height"], 2556)
        handed = prep.decode((Path(job["report_directory"]) / "paravirtual/device-tree").read_bytes())
        self.assertEqual(handed.child("product").get("artwork-device-subtype"), (2556).to_bytes(4, "little"))
        self.assertEqual(handed.child("buttons").get("home-button-type"), (2).to_bytes(4, "little"))
        # Another model of the family: the variant with that model's derived values.
        job = dispatch("ios.emulator.paravirtual", {**arguments, "device_model": "18promax"})
        argv = job["plan"]["steps"][0]["argv"]
        self.assertEqual(json.loads(argv[argv.index("--device-model") + 1]),
                         {"variant": "d47vm2025vre", "artwork_subtype": 2868, "corner_radius": 62,
                          "island_bottom": 153, "large_format": True})
        self.assertEqual(argv[argv.index("--display-profile") + 1], "1320x2868@60:72x156")
        self.assertEqual(jobs.wait(job["id"], 10)["state"], "succeeded")
        # The model's own panel may be named; any other describes a device that does not exist.
        same = dispatch("ios.emulator.paravirtual", {**arguments, "device_model": "16",
                                                     "display_profile": "1179x2556@60:65x141"})
        self.assertEqual(jobs.wait(same["id"], 10)["state"], "succeeded")
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.paravirtual", {**arguments, "device_model": "16",
                                                  "display_profile": "1170x2532@60:65x141"})
        self.assertEqual(error.exception.code, "conflicting_display_profile")
        for unknown in ("iphone-99", "pixel-11", "duo-open"):
            with self.assertRaises(StudioError) as error:
                dispatch("ios.emulator.paravirtual", {**arguments, "device_model": unknown})
            self.assertEqual(error.exception.code, "invalid_device_model")
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.paravirtual", {**arguments, "device_model": "Not A Model"})
        self.assertEqual(error.exception.code, "invalid_argument")
        # A device tree without that variant cannot be described as the model.
        without = storage_tree()
        without.children.append(node("buttons"))
        tree.write_bytes(prep.prepare(without.encode()))
        plain = dispatch("ios.emulator.paravirtual", {**arguments, "device_model": "16"})
        self.assertEqual(jobs.wait(plain["id"], 10)["state"], "failed")
        self.assertIn("no gestalt variant", jobs.logs(plain["id"])["text"])

    def test_instruction_clock_is_forwarded_and_reported_by_the_owned_job(self):
        job = dispatch("ios.emulator.paravirtual", {**self.arguments(), "clock_mode": "instruction"})
        argv = job["plan"]["steps"][0]["argv"]
        self.assertEqual(argv[argv.index("--clock-mode") + 1], "instruction")
        done = jobs.wait(job["id"], 10)
        self.assertEqual(done["state"], "succeeded", jobs.logs(job["id"]))
        report = dispatch("ios.emulator.report", {"job_id": job["id"]})["report"]
        self.assertEqual(report["clock_mode"], "instruction")
        self.assertFalse(report["qualified_for_app_development"])

    def test_engine_options_reach_the_launcher_and_its_receipt(self):
        # A profile's boot.bti, boot.jump_probe and boot.wfe_wait_us (passed on
        # by ios.virtual.start) become the launcher's flags and CPU globals.
        cases = (({}, [], [], ("keep", "engine", None)),
                 ({"bti": "off", "jump_probe": "inline", "wfe_wait_us": 200},
                  ["--bti", "off", "--jump-probe", "inline", "--wfe-wait-us", "200"],
                  ["max-arm-cpu.tomacco-bti=off", "max-arm-cpu.tomacco-jmp-probe=2",
                   "max-arm-cpu.tomacco-wfe-wait-us=200"], ("off", "inline", 200)),
                 ({"bti": "keep", "jump_probe": "generic", "wfe_wait_us": 0},
                  ["--bti", "keep", "--jump-probe", "generic", "--wfe-wait-us", "0"],
                  ["max-arm-cpu.tomacco-jmp-probe=0", "max-arm-cpu.tomacco-wfe-wait-us=0"],
                  ("keep", "generic", 0)))
        for options, flags, expected, (bti, probe, wait) in cases:
            with self.subTest(options=options):
                job = dispatch("ios.emulator.paravirtual", {**self.arguments(), **options})
                argv = job["plan"]["steps"][0]["argv"]
                for flag in ("--bti", "--jump-probe", "--wfe-wait-us"):
                    if flag in flags:
                        self.assertEqual(argv[argv.index(flag) + 1], flags[flags.index(flag) + 1])
                    else:
                        self.assertNotIn(flag, argv)
                # Absent options leave the request key as it was.
                self.assertFalse(({"bti", "jump_probe", "wfe_wait_us"} - set(options)) &
                                 set(job["plan"]["operation_key"]["arguments"]))
                done = jobs.wait(job["id"], 10)
                self.assertEqual(done["state"], "succeeded", jobs.logs(job["id"]))
                receipt = json.loads((Path(job["report_directory"]) / "paravirtual/request.json").read_text())
                command = receipt["command"]
                globals_ = [command[i + 1] for i, arg in enumerate(command) if arg == "-global"]
                self.assertEqual([g for g in globals_ if "tomacco-bti" in g or "tomacco-jmp-probe" in g
                                  or "tomacco-wfe-wait-us" in g], expected)
                self.assertEqual((receipt["bti"], receipt["jump_probe"], receipt["wfe_wait_us"]),
                                 (bti, probe, wait))
        for invalid in ({"bti": "on"}, {"bti": True}, {"jump_probe": "fast"}, {"jump_probe": 2},
                        {"wfe_wait_us": -1}, {"wfe_wait_us": 100001}, {"wfe_wait_us": 1.5},
                        {"wfe_wait_us": "200"}):
            with self.subTest(invalid=invalid), self.assertRaises(StudioError) as error:
                dispatch("ios.emulator.paravirtual", {**self.arguments(), **invalid})
            self.assertEqual(error.exception.code, "invalid_argument")

    def test_target_page_reaches_the_machine_and_the_16k_cpu(self):
        # A profile's boot.target_page becomes the launcher's --target-page:
        # 4k and 16k name the target page in the machine options (the engine
        # decides it before the board exists), 16k also selects the CPU that
        # reports only the 16 KiB granule. Omitted: neither, as before.
        cases = (({}, None, None, []), ({"target_page": "engine"}, "engine", None, []),
                 ({"target_page": "4k"}, "4k", "12", []),
                 ({"target_page": "16k"}, "16k", "14", ["max-arm-cpu.tomacco-granule-16k=on"]))
        for options, flag, bits, granule in cases:
            with self.subTest(options=options):
                job = dispatch("ios.emulator.paravirtual", {**self.arguments(), **options})
                argv = job["plan"]["steps"][0]["argv"]
                if flag is None:
                    self.assertNotIn("--target-page", argv)
                    self.assertNotIn("target_page", job["plan"]["operation_key"]["arguments"])
                else:
                    self.assertEqual(argv[argv.index("--target-page") + 1], flag)
                done = jobs.wait(job["id"], 10)
                self.assertEqual(done["state"], "succeeded", jobs.logs(job["id"]))
                receipt = json.loads((Path(job["report_directory"]) / "paravirtual/request.json").read_text())
                command = receipt["command"]
                machine = command[command.index("-M") + 1]
                self.assertEqual("tomacco-page-bits=" + bits in machine.split(",") if bits else
                                 "tomacco-page-bits" in machine, bool(bits))
                globals_ = [command[i + 1] for i, arg in enumerate(command) if arg == "-global"]
                self.assertEqual([g for g in globals_ if "tomacco-granule-16k" in g], granule)
                self.assertEqual(receipt["target_page"], flag or "engine")
        for invalid in ({"target_page": "64k"}, {"target_page": 14}, {"target_page": True}):
            with self.subTest(invalid=invalid), self.assertRaises(StudioError) as error:
                dispatch("ios.emulator.paravirtual", {**self.arguments(), **invalid})
            self.assertEqual(error.exception.code, "invalid_argument")

    def test_long_instruction_session_reaches_runner_and_still_cancels(self):
        args = {**self.arguments(wait=True), "clock_mode": "instruction", "seconds": 21600}
        job = dispatch("ios.emulator.paravirtual", args)
        self.assertEqual(job["plan"]["timeout"], 22500)
        argv = job["plan"]["steps"][0]["argv"]
        self.assertEqual(argv[argv.index("--seconds") + 1], "21600")
        output = Path(job["report_directory"]) / "paravirtual"
        end = time.monotonic() + 5 * PATIENCE
        while not (output / "request.json").is_file() and time.monotonic() < end:
            time.sleep(.02)
        self.assertTrue((output / "request.json").is_file(), jobs.logs(job["id"]))
        self.assertEqual(dispatch("ios.emulator.paravirtual", args)["id"], job["id"])
        for seconds in (0, 21601):
            with self.subTest(seconds=seconds), self.assertRaises(StudioError):
                dispatch("ios.emulator.paravirtual", {**args, "seconds": seconds})
        jobs.cancel(job["id"])
        self.assertEqual(jobs.wait(job["id"], 8)["state"], "cancelled")

    def test_identical_request_reuses_conflict_rejects_and_cancel_reaps_the_guest(self):
        args = self.arguments(wait=True)
        job = dispatch("ios.emulator.paravirtual", args)
        output = Path(job["report_directory"]) / "paravirtual"
        end = time.monotonic() + 5 * PATIENCE
        while not (output / "request.json").is_file() and time.monotonic() < end:
            time.sleep(0.02)
        self.assertTrue((output / "request.json").is_file(), jobs.logs(job["id"]))
        repeated = dispatch("ios.emulator.paravirtual", args)
        self.assertEqual(repeated["id"], job["id"])
        self.assertTrue(repeated["reused"])
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.paravirtual", {**args, "seconds": 20})
        self.assertEqual(error.exception.code, "ios_emulator_busy")
        self.assertEqual(dispatch("ios.emulator.report", {"job_id": job["id"]})["report"], None)
        jobs.cancel(job["id"])
        self.assertEqual(jobs.wait(job["id"], 8)["state"], "cancelled")
        if (output / "result.json").exists():
            self.assertFalse(json.loads((output / "result.json").read_text())["qualified_for_app_development"])

    def test_display_panel_reaches_child_and_receipt_without_inheriting_a_shell_override(self):
        import os
        from unittest.mock import patch
        for profile in ("1170x2532@60:65x141", None):
            args = self.arguments()
            Path(args["engine"]).write_text("#!/bin/sh\nprintf 'PANEL=%s\\n' \"$REIMS_VGPU_PANEL\"\n")
            if profile:
                args["display_profile"] = profile
            with patch.dict(os.environ, {"REIMS_VGPU_PANEL": "1920x1080@120:480x270"}):
                job = dispatch("ios.emulator.paravirtual", args)
                done = jobs.wait(job["id"], 10)
            self.assertEqual(done["state"], "succeeded", jobs.logs(job["id"]))
            directory = Path(job["report_directory"]) / "paravirtual"
            self.assertEqual((directory / "host.log").read_text().strip(), "PANEL=" + (profile or ""))
            receipt = json.loads((directory / "request.json").read_text())
            self.assertEqual(receipt["display_profile"], {"width": 1170, "height": 2532,
                "refresh_hz": 60, "width_mm": 65, "height_mm": 141} if profile else None)
            self.assertFalse(receipt["qualified_for_app_development"])

    def test_render_stores_reach_the_guest_eagerly_whatever_the_shell_says(self):
        import os
        from unittest.mock import patch
        args = self.arguments()
        Path(args["engine"]).write_text("#!/bin/sh\nprintf 'WRITEBACK=%s\\n' \"$REIMS_VGPU_LAZY_WRITEBACK\"\n")
        with patch.dict(os.environ, {"REIMS_VGPU_LAZY_WRITEBACK": "on"}):
            job = dispatch("ios.emulator.paravirtual", args)
            done = jobs.wait(job["id"], 10)
        self.assertEqual(done["state"], "succeeded", jobs.logs(job["id"]))
        directory = Path(job["report_directory"]) / "paravirtual"
        self.assertEqual((directory / "host.log").read_text().strip(), "WRITEBACK=off")
        receipt = json.loads((directory / "request.json").read_text())
        self.assertEqual(receipt["render_store_writeback"], "eager")

    def test_display_panel_rejects_invalid_geometry_before_starting_a_job(self):
        args = self.arguments()
        for profile in ("1170x2532@60:141x65", "8193x8193@60:100x100", "1170x2532@121:65x141",
                        "1170@2532x60:65x141", "1170x2532@60:0x0", "+1170x2532@60:65x141"):
            with self.subTest(profile=profile), self.assertRaises(StudioError) as error:
                dispatch("ios.emulator.paravirtual", {**args, "display_profile": profile})
            self.assertEqual(error.exception.code, "invalid_display_profile")

    def test_storage_access_modes_conflict_before_job_submission(self):
        args = self.arguments()
        disk = self.root / "disk"
        disk.write_bytes(bytes(512))
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.paravirtual", {**args, "root_disk": str(disk), "writable_root_disk": str(disk)})
        self.assertEqual(error.exception.code, "conflicting_storage")
        self.assertEqual(disk.read_bytes(), bytes(512))

    def test_pcc_configuration_requires_a_normal_system_boot_before_submission(self):
        args = self.arguments()
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.paravirtual", {**args, "pcc_hostname": "tomacco"})
        self.assertEqual(error.exception.code, "invalid_pcc_boot")

    def test_usb_bridge_reuses_waits_reports_and_stops_with_its_guest(self):
        args = {**self.arguments(wait=True), "usb": True, "seconds": 30}
        parent = dispatch("ios.emulator.paravirtual", args)
        child_args = {"job_id": parent["id"], "seconds": 20}
        bridge = dispatch("ios.emulator.usb_bridge", child_args)
        self.assertEqual(bridge["plan"]["parent_job"], parent["id"])
        self.assertEqual(dispatch("ios.emulator.usb_bridge", child_args)["id"], bridge["id"])
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.usb_bridge", {**child_args, "seconds": 21})
        self.assertEqual(error.exception.code, "ios_usb_bridge_busy")
        receipt = Path(bridge["bridge"])
        end = time.monotonic() + 5 * PATIENCE
        while not receipt.exists() and time.monotonic() < end:
            time.sleep(.02)
        self.assertTrue(receipt.is_file(), jobs.logs(bridge["id"]))
        report = dispatch("ios.emulator.report", {"job_id": bridge["id"]})["report"]
        self.assertEqual(report["state"], "connecting")
        self.assertFalse(report["qualified_for_app_development"])
        self.assertFalse(report["physical_devices_exposed"])
        jobs.cancel(parent["id"])
        self.assertEqual(jobs.wait(parent["id"], 8)["state"], "cancelled")
        self.assertEqual(jobs.wait(bridge["id"], 8)["state"], "cancelled")
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.usb_bridge", child_args)
        self.assertEqual(error.exception.code, "ios_guest_stopped")
        foreign = self.root / "foreign-bridge.json"
        foreign.write_text("{}")
        receipt.unlink()
        receipt.symlink_to(foreign)
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.report", {"job_id": bridge["id"]})
        self.assertEqual(error.exception.code, "invalid_report_path")

    def test_bridge_cannot_attach_to_a_job_without_virtual_usb(self):
        parent = dispatch("ios.emulator.paravirtual", self.arguments(wait=True))
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.usb_bridge", {"job_id": parent["id"]})
        self.assertEqual(error.exception.code, "invalid_usb_guest")


if __name__ == "__main__":
    unittest.main()
