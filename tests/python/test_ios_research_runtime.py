"""Research orchestration and input integrity. Not real iOS qualification."""
import json
import os
from pathlib import Path
import shutil
import sys
import time
from unittest.mock import patch

import test_ios_emulator as emulator
from test_ios_platform import fixture as platform_fixture, encode
from test_product import Isolated
from native_client import ROOT, StudioError, dispatch, jobs
from host_patience import PATIENCE

sys.path.insert(0, str(ROOT / "tools"))
import prepare_ios_research_runtime as prepare


class ResearchTests(Isolated):
    fixture = emulator.IosEmulatorTests.fixture
    prepared_fixture = emulator.IosEmulatorTests.prepared_fixture

    def inputs(self):
        arguments = self.prepared_fixture()
        firmware = Path(arguments["firmware"])
        prepared = json.loads((firmware / "tomacco-ios.json").read_text())
        inputs, bootstrap = self.root / "inputs", self.root / "bootstrap"
        inputs.mkdir(); bootstrap.mkdir()
        outputs = {}
        for name in ("kernel", "device_tree"):
            source = firmware / prepared["components"][name]["path"]
            shutil.copyfile(source, inputs / source.name)
            outputs[name] = prepared["components"][name]
        (inputs / "research-inputs.json").write_text(json.dumps({
            "format": 1, "scope": "disposable-apfs-ram-research", "firmware": "27.0.1", "build": "24A446",
            "inputs_preserved": True, "root_authentication_changed": False, "outputs": outputs}))
        shutil.copyfile(firmware / "trust.im4p", bootstrap / "bootstrap.tc")
        (bootstrap / "manifest.json").write_text(json.dumps({
            "format": 1, "scope": "full-system-boot-research", "data_filesystem": "apfs", "restart_probe": False,
            "platform_diagnostics": True, "launchd_enabled": True,
            "trustcache_sha256": prepare.digest(bootstrap / "bootstrap.tc"),
            "platform_contract": {"classes": list(prepare.CLASSES), "device_tree_paths": list(prepare.NODE_PATHS)}}))
        image = firmware / "restore.dmg"
        (bootstrap / "image-manifest.json").write_text(json.dumps({
            "source_preserved": True, "target": str(image), "target_sha256": prepare.digest(image),
            "bootstrap": str(bootstrap / "manifest.json")}))
        return arguments, dict(firmware=firmware, inputs=inputs, bootstrap=bootstrap, image=image, output=self.root / "bundle")

    def runtime(self, raw=None, hardware_profile=None):
        args, inputs = self.inputs()
        if hardware_profile is not None:
            file = inputs["inputs"] / "research-inputs.json"
            data = json.loads(file.read_text())
            data["hardware_profile"] = hardware_profile
            file.write_text(json.dumps(data))
        if hardware_profile in ("exclave-dart", "exclave-mcc"):
            self.exclave_inputs(inputs, hardware_profile)
        prepare.prepare(**inputs)
        engine = Path(args["engine"])
        engine.write_text("#!" + sys.executable + "\n" + (ROOT / "tests/fixtures/ios_research_engine.py").read_text().split("\n", 1)[1])
        if raw is not None:
            (self.root / "platform-fixture.log").write_bytes(raw)
        return {**args, "firmware": str(inputs["output"]), "seconds": 5}, inputs

    def exclave_inputs(self, inputs, hardware_profile="exclave-dart"):
        """Protocol data only; the real BUND parser has separate C fixtures."""
        source = inputs["exclave"] = self.root / "exclave"
        source.mkdir()
        original = json.loads((inputs["firmware"] / "tomacco-ios.json").read_text())
        shutil.copyfile(inputs["firmware"] / "BuildManifest.plist", source / "BuildManifest.plist")
        record = {"format": 1, "scope": "exclavecore-boot-research", "chip": "0x8140",
                  "manifest_digests_verified": True, "inputs_preserved": True,
                  "firmware_modified": False, "secure_services_qualified": False,
                  "graphical_apps_qualified": False,
                  **{key: original[key] for key in ("board", "version", "build", "manifest_sha256")},
                  "components": {}}
        for name in ("cl4", "integrity", "trustcache"):
            file = source / name
            file.write_bytes(("Protocol fixture " + name).encode())
            record["components"][name] = {"path": name, "sha256": prepare.digest(file), "bytes": file.stat().st_size}
        (source / "exclave-research.json").write_text(json.dumps(record))
        profile = inputs["inputs"] / "research-inputs.json"
        value = json.loads(profile.read_text()); value["hardware_profile"] = hardware_profile
        profile.write_text(json.dumps(value))
        # Exercise QEMU option escaping as well as argv path handling.
        inputs["output"] = self.root / "bundle,with-comma"

    def test_exclave_preparation_job_and_native_loader_options(self):
        args, inputs = self.inputs()
        self.exclave_inputs(inputs)
        job = dispatch("ios.emulator.prepare_research", {key: str(value) for key, value in inputs.items()})
        self.assertEqual(jobs.wait(job["id"], 10)["state"], "succeeded", jobs.logs(job["id"]))
        engine = Path(args["engine"])
        script = "#!" + sys.executable + "\n" + (ROOT / "tests/fixtures/ios_research_engine.py").read_text().split("\n", 1)[1]
        engine.write_text(script.replace("\\nexclave-trustcache=<string>", ""))
        args.update(firmware=str(inputs["output"]), seconds=1)
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.research", args)
        self.assertEqual(error.exception.code, "research_hardware_missing")
        self.assertFalse((self.root / "engine.pid").exists())
        engine.write_text(script)
        (self.root / "platform-fixture.log").write_bytes(encode(platform_fixture()))
        job = dispatch("ios.emulator.research", args)
        self.assertEqual(jobs.wait(job["id"], 10)["state"], "succeeded", jobs.logs(job["id"]))
        report = dispatch("ios.emulator.report", {"job_id": job["id"]})["report"]
        self.assertFalse(report["graphical_apps_verified"])
        self.assertFalse(report["firmware"]["secure_services_qualified"])
        self.assertEqual(report["firmware"]["hardware_profile"], "exclave-dart")
        argv = json.loads((self.root / "engine-argv.json").read_text())
        machine = argv[argv.index("-M") + 1]
        self.assertIn(",research-dart=on", machine)
        for option, name in (("research-cl4", "cl4"), ("exclave-integrity", "exclave_integrity"),
                             ("exclave-trustcache", "exclave_trustcache")):
            file = Path(report["firmware"]["components"][name]["path"])
            self.assertIn("," + option + "=" + str(file).replace(",", ",,"), machine)
            self.assertEqual(file.stat().st_mode & 0o777, 0o400)
            self.assertEqual(prepare.digest(file), report["firmware"]["components"][name]["sha256"])

    def test_exclave_identity_profile_checksum_and_confinement_before_publication(self):
        _, inputs = self.inputs()
        self.exclave_inputs(inputs)
        file = inputs["exclave"] / "exclave-research.json"
        original = json.loads(file.read_text())
        for key, value in (("board", "different"), ("build", "different"), ("chip", "0x1"),
                           ("manifest_digests_verified", False), ("secure_services_qualified", True)):
            file.write_text(json.dumps({**original, key: value}))
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "does not match"):
                prepare.prepare(**inputs)
            self.assertFalse(inputs["output"].exists())
        file.write_text(json.dumps(original))
        with self.assertRaisesRegex(ValueError, "supplied together"):
            prepare.prepare(**{key: value for key, value in inputs.items() if key != "exclave"})
        cl4 = inputs["exclave"] / "cl4"
        before = cl4.read_bytes()
        cl4.write_bytes(b"x" * len(before))
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            prepare.prepare(**inputs)
        self.assertFalse(inputs["output"].exists())
        self.assertFalse(list(self.root.glob(".tomacco-ios-prepare-*")))
        external = self.root / "outside-cl4"
        external.write_bytes(before); cl4.unlink(); cl4.symlink_to(external)
        with self.assertRaisesRegex(ValueError, "escapes"):
            prepare.prepare(**inputs)
        self.assertFalse(inputs["output"].exists())

    def test_exclave_component_tampering_is_rejected_by_native_inspection(self):
        args, inputs = self.runtime(hardware_profile="exclave-dart")
        file = inputs["output"] / "exclave.tc"
        file.chmod(0o600); file.write_bytes(b"changed")
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.research", args)
        self.assertEqual(error.exception.code, "firmware_modified")
        self.assertFalse((self.root / "engine.pid").exists())

    def test_manifest_change_during_copy_prevents_publication(self):
        _, inputs = self.inputs()
        self.exclave_inputs(inputs)
        file = inputs["exclave"] / "exclave-research.json"
        original_copy = prepare.checked_copy

        def copy_and_change(source, destination, expected):
            result = original_copy(source, destination, expected)
            if destination.name == "exclave.tc":
                value = json.loads(file.read_text())
                value["build"] = "changed while copying"
                file.write_text(json.dumps(value))
            return result

        with patch.object(prepare, "checked_copy", side_effect=copy_and_change):
            with self.assertRaisesRegex(ValueError, "Source manifest changed"):
                prepare.prepare(**inputs)
        self.assertFalse(inputs["output"].exists())
        self.assertFalse(list(self.root.glob(".tomacco-ios-prepare-*")))

    def test_preparation_is_a_durable_job_and_preserves_sources(self):
        _, inputs = self.inputs()
        before = prepare.digest(inputs["image"])
        job = dispatch("ios.emulator.prepare_research", {key: str(value) for key, value in inputs.items()})
        result = jobs.wait(job["id"], 10)
        self.assertEqual(result["state"], "succeeded", jobs.logs(job["id"]))
        self.assertEqual(len(result["result"]["artifacts"]), 1)
        self.assertEqual(result["result"]["artifacts"][0]["path"], str(inputs["output"] / "tomacco-ios.json"))
        self.assertEqual(prepare.digest(inputs["image"]), before)
        manifest = json.loads((inputs["output"] / "tomacco-ios.json").read_text())
        self.assertEqual(manifest["environment"], "ios-system-research")
        self.assertFalse(manifest["graphical_apps_qualified"])
        self.assertFalse(manifest["full_ios_boot_qualified"])
        for entry in manifest["components"].values():
            file = inputs["output"] / entry["path"]
            self.assertEqual(prepare.digest(file), entry["sha256"])
            self.assertEqual(file.stat().st_mode & 0o777, 0o400)
            self.assertEqual(file.stat().st_nlink, 1)
        self.assertFalse(list(self.root.glob(".tomacco-ios-prepare-*")))
        with self.assertRaises(StudioError):
            dispatch("ios.emulator.prepare_research", {key: str(value) for key, value in inputs.items()})

    def test_hardware_profile_is_preserved_and_requires_engine_support(self):
        args, inputs = self.runtime(encode(platform_fixture(mode="uart")), "dcp-dart")
        data = json.loads((inputs["output"] / "tomacco-ios.json").read_text())
        self.assertEqual(data["hardware_profile"], "dcp-dart")
        engine = Path(args["engine"])
        with_dart = engine.read_text()
        engine.write_text(with_dart.replace("\\nresearch-dart=<bool>", ""))
        with self.assertRaises(StudioError) as caught:
            dispatch("ios.emulator.research", args)
        self.assertEqual(caught.exception.code, "research_hardware_missing")
        self.assertFalse((self.root / "engine.pid").exists())
        engine.write_text(with_dart)
        job = dispatch("ios.emulator.research", args)
        state = jobs.wait(job["id"], 10)
        self.assertEqual(state["state"], "succeeded", jobs.logs(job["id"]))
        report = dispatch("ios.emulator.report", {"job_id": job["id"]})["report"]
        self.assertEqual(report["firmware"]["hardware_profile"], "dcp-dart")
        argv = json.loads((self.root / "engine-argv.json").read_text())
        self.assertIn(",research-dart=on", argv[argv.index("-M") + 1])
        self.assertNotIn("mcc_cntr_cfg", argv[argv.index("-args") + 1])
        self.assertNotIn("research-ptd=", argv[argv.index("-M") + 1])
        self.assertNotIn("enable=tomacco_ptd_mmio", argv)
        self.assertFalse(report["graphical_apps_verified"])

    def test_unknown_hardware_profile_is_rejected_before_publication(self):
        _, inputs = self.inputs()
        file = inputs["inputs"] / "research-inputs.json"
        data = json.loads(file.read_text())
        for value in ("unknown", None, 1, ["dcp-dart"]):
            data["hardware_profile"] = value
            file.write_text(json.dumps(data))
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Unknown research hardware"):
                prepare.prepare(**inputs)
            self.assertFalse(inputs["output"].exists())

    def test_mcc_profile_requires_both_loader_and_model_and_records_options(self):
        args, inputs = self.runtime(encode(platform_fixture(mode="uart")), "exclave-mcc")
        engine = Path(args["engine"])
        original = engine.read_text()
        for absent in ("\\nresearch-mcc=<bool>", "\\nresearch-cl4=<string>", "\\nresearch-dart=<bool>"):
            engine.write_text(original.replace(absent, ""))
            with self.subTest(absent=absent), self.assertRaises(StudioError) as caught:
                dispatch("ios.emulator.research", args)
            self.assertEqual(caught.exception.code, "research_hardware_missing")
            self.assertFalse((self.root / "engine.pid").exists())
        engine.write_text(original)
        inspected = dispatch("ios.emulator.inspect", {k: args[k] for k in ("engine", "firmware", "variant")})
        self.assertTrue(inspected["capabilities"]["mcc_registers"])
        self.assertTrue(inspected["capabilities"]["mcc_mmio_trace"])
        self.assertTrue(inspected["capabilities"]["ptd_mailbox"])
        self.assertTrue(inspected["capabilities"]["ptd_mmio_trace"])
        self.assertTrue({"kernel", "device_tree", "ramdisk", "trustcache", "sptm", "txm",
                         "cl4", "exclave_integrity", "exclave_trustcache"}
                        <= set(inspected["firmware"]["components"]))
        job = dispatch("ios.emulator.research", args)
        self.assertEqual(jobs.wait(job["id"], 10)["state"], "succeeded", jobs.logs(job["id"]))
        argv = json.loads((self.root / "engine-argv.json").read_text())
        machine = argv[argv.index("-M") + 1]
        for option in (",research-mcc=on", ",research-ptd=on", ",research-dart=on", ",research-cl4=", ",exclave-integrity=", ",exclave-trustcache="):
            self.assertIn(option, machine)
        self.assertIn("enable=tomacco_mcc_mmio", argv)
        self.assertIn("enable=tomacco_ptd_mmio", argv)
        self.assertIn("mcc_cntr_cfg=0", argv[argv.index("-args") + 1].split())
        report = dispatch("ios.emulator.report", {"job_id": job["id"]})["report"]
        self.assertEqual(report["firmware"]["hardware_profile"], "exclave-mcc")
        self.assertFalse(report["graphical_apps_verified"])
        self.assertFalse(report["firmware"]["secure_services_qualified"])
        self.assertEqual(prepare.digest(inputs["image"]), report["firmware"]["components"]["ramdisk"]["sha256"])

    def test_mcc_legacy_engine_does_not_receive_unsupported_ptd_options(self):
        args, _ = self.runtime(encode(platform_fixture(mode="uart")), "exclave-mcc")
        engine = Path(args["engine"])
        engine.write_text(engine.read_text().replace("\\nresearch-ptd=<bool>", ""))
        inspected = dispatch("ios.emulator.inspect", {k: args[k] for k in ("engine", "firmware", "variant")})
        self.assertFalse(inspected["capabilities"]["ptd_mailbox"])
        job = dispatch("ios.emulator.research", args)
        self.assertEqual(jobs.wait(job["id"], 10)["state"], "succeeded", jobs.logs(job["id"]))
        argv = json.loads((self.root / "engine-argv.json").read_text())
        self.assertIn(",research-mcc=on", argv[argv.index("-M") + 1])
        self.assertNotIn("research-ptd=", argv[argv.index("-M") + 1])
        self.assertNotIn("enable=tomacco_ptd_mmio", argv)

    def test_changed_component_and_contract_fail_without_partial_publication(self):
        _, inputs = self.inputs()
        (inputs["bootstrap"] / "bootstrap.tc").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            prepare.prepare(**inputs)
        self.assertFalse(inputs["output"].exists())
        self.assertFalse(list(self.root.glob(".tomacco-ios-prepare-*")))
        file = inputs["bootstrap"] / "manifest.json"
        value = json.loads(file.read_text()); value["platform_contract"]["classes"].pop(); file.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "Incompatible"):
            prepare.prepare(**inputs)

    def test_cancelled_preparation_removes_staging_without_publishing_bundle(self):
        _, inputs = self.inputs()
        wrapper = self.root / "python3"
        wrapper.write_text("#!" + sys.executable + "\nimport sys,time\nfrom pathlib import Path\n"
                  "stage=Path(sys.argv[sys.argv.index('--staging')+1])\n"
                  "(stage/'partial.apfs').write_bytes(b'partial fixture copy')\n"
                  "print('staged',flush=True)\ntime.sleep(60)\n")
        wrapper.chmod(0o755)
        with patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ["PATH"]}):
            job = dispatch("ios.emulator.prepare_research", {key: str(value) for key, value in inputs.items()})
        self.wait_log(job, "staged")
        self.assertTrue(list(self.root.glob(".tomacco-ios-prepare-*")))
        jobs.cancel(job["id"])
        self.assertEqual(jobs.wait(job["id"], 10)["state"], "cancelled")
        self.assertFalse(list(self.root.glob(".tomacco-ios-prepare-*")))
        self.assertFalse(inputs["output"].exists())
        self.assertTrue(inputs["image"].is_file())

    def test_atomic_publication_never_replaces_concurrent_destination(self):
        _, inputs = self.inputs()
        publish = prepare.publish
        def competing(stage, destination):
            destination.mkdir()
            (destination / "keep").write_text("preserved")
            publish(stage, destination)
        with patch.object(prepare, "publish", competing), self.assertRaises(FileExistsError):
            prepare.prepare(**inputs)
        self.assertEqual((inputs["output"] / "keep").read_text(), "preserved")
        self.assertFalse((inputs["output"] / "tomacco-ios.json").exists())

    def test_confined_inputs_and_output_links_are_rejected(self):
        _, inputs = self.inputs()
        inputs["output"].symlink_to(self.root / "absent")
        with self.assertRaises(ValueError):
            prepare.prepare(**inputs)
        self.assertTrue(inputs["output"].is_symlink())
        inputs["output"].unlink()
        kernel = inputs["inputs"] / "kernel.im4p"
        kernel.unlink(); kernel.symlink_to(inputs["firmware"] / "kernel.im4p")
        with self.assertRaisesRegex(ValueError, "escapes"):
            prepare.prepare(**inputs)

    def test_research_report_validates_two_snapshots_and_cleans_disposable_image(self):
        args, inputs = self.runtime(encode(platform_fixture()))
        original = prepare.digest(inputs["image"])
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.probe", args)
        self.assertEqual(error.exception.code, "research_environment")
        job = dispatch("ios.emulator.research", args)
        result = jobs.wait(job["id"], 10)
        self.assertEqual(result["state"], "succeeded", jobs.logs(job["id"]))
        value = dispatch("ios.emulator.report", {"job_id": job["id"]})
        report = value["report"]
        self.assertTrue(report["guest_observations_verified"])
        self.assertTrue(report["working_image_flushed"])
        self.assertFalse(report["graphical_apps_verified"])
        self.assertFalse(report["full_ios_boot_verified"])
        self.assertEqual(len(report["platform"]["snapshots"]), 2)
        self.assertIn("AppleSEPManager", report["platform"]["missing_services"])
        folder = Path(value["report_directory"])
        request = json.loads((folder / "request.json").read_text())
        self.assertFalse(Path(request["machine_directory"]).exists())
        self.assertFalse(Path(request["socket_directory"]).exists())
        self.assertEqual(prepare.digest(inputs["image"]), original)
        argv = json.loads((folder / "command.json").read_text())
        self.assertEqual(argv[argv.index("-m") + 1], "32768M")
        self.assertEqual(argv.count("-serial"), 4)

    def test_partial_evidence_times_out_without_success_and_keeps_report(self):
        events = platform_fixture()
        second = next(i for i, e in enumerate(events) if e["event"] == "snapshot" and e["tick"] == 90)
        args, _ = self.runtime(encode(events[:second + 1]))
        job = dispatch("ios.emulator.research", {**args, "seconds": 1})
        self.assertEqual(jobs.wait(job["id"], 10)["state"], "failed")
        report = dispatch("ios.emulator.report", {"job_id": job["id"]})["report"]
        self.assertEqual(report["error"]["code"], "guest_observation_timeout")
        self.assertFalse(report["guest_observations_verified"])
        self.assertEqual(len(report["platform"]["snapshots"]), 1)

    def test_serial_backpressure_does_not_deadlock_qmp_or_lose_output(self):
        args, _ = self.runtime(encode(platform_fixture()))
        (self.root / "serial-burst").touch()
        job = dispatch("ios.emulator.research", args)
        result = jobs.wait(job["id"], 30)
        self.assertEqual(result["state"], "succeeded", jobs.logs(job["id"]))
        value = dispatch("ios.emulator.report", {"job_id": job["id"]})
        self.assertTrue(value["report"]["working_image_flushed"])
        folder = Path(value["report_directory"])
        for name in ("console", "process", "graphics"):
            self.assertEqual((folder / (name + ".log")).read_bytes(), b"SERIAL_BURST\n" * 32768)

    def test_fragmented_kernel_panic_stops_early_and_flushes_failed_guest(self):
        args, inputs = self.runtime()
        original = prepare.digest(inputs["image"])
        panic = b"panic(cpu 0 caller 0xfffffff02b430010): SK panic request: exception.c:35\r\n"
        raw = b"booting\r\npan|SPLIT|ic(cpu 0 caller 0xff|SPLIT|fffff02b430010): SK panic request: exception.c:35\r|SPLIT|\n"
        (self.root / "console-fixture.log").write_bytes(raw)
        (self.root / "bus-fault-fixture.log").write_bytes(
            b"tomacco_arm_bus_|SPLIT|fault pc=0xfffffff029822e98 physical=0x220401d48 "
            b"virtual=0xffffffe209669d48 size=4 access=0 mmu=8 response=2\n")
        before = time.monotonic()
        job = dispatch("ios.emulator.research", {**args, "seconds": 60})
        self.assertEqual(jobs.wait(job["id"], 10)["state"], "failed", jobs.logs(job["id"]))
        self.assertLess(time.monotonic() - before, 10)
        value = dispatch("ios.emulator.report", {"job_id": job["id"]})
        report = value["report"]
        self.assertEqual(report["error"]["code"], "guest_kernel_panic")
        self.assertEqual(report["error"]["message"], panic.decode().strip())
        self.assertTrue(report["bus_faults"]["trace_enabled"])
        self.assertEqual(report["bus_faults"]["events"][0]["physical"], "0x220401d48")
        self.assertFalse(report["guest_observations_verified"])
        self.assertTrue(report["working_image_flushed"])
        folder = Path(value["report_directory"])
        self.assertEqual((folder / "console.log").read_bytes(), b"booting\r\n" + panic)
        request = json.loads((folder / "request.json").read_text())
        self.assertFalse(Path(request["machine_directory"]).exists())
        self.assertFalse(Path(request["socket_directory"]).exists())
        self.assertEqual(prepare.digest(inputs["image"]), original)

    def test_recovered_bus_faults_are_structured_without_failing_observations(self):
        args, _ = self.runtime(encode(platform_fixture()))
        fault = (b"tomacco_arm_bus_fault pc=0xfffffff029822e98 physical=0x220401d48 "
                 b"virtual=0xffffffe209669d48 size=4 access=0 mmu=8 response=2\n")
        raw = fault + fault.replace(b"access=0", b"access=1") + fault.replace(b"size=4", b"size=bad")
        (self.root / "bus-fault-fixture.log").write_bytes(raw)
        inspected = dispatch("ios.emulator.inspect", {key: args[key] for key in ("engine", "firmware", "variant")})
        self.assertTrue(inspected["capabilities"]["arm_bus_fault_trace"])
        job = dispatch("ios.emulator.research", args)
        self.assertEqual(jobs.wait(job["id"], 10)["state"], "succeeded", jobs.logs(job["id"]))
        value = dispatch("ios.emulator.report", {"job_id": job["id"]})
        faults = value["report"]["bus_faults"]
        self.assertEqual([event["access"] for event in faults["events"]], ["read", "write"])
        self.assertEqual(faults["events"][0]["pc"], "0xfffffff029822e98")
        self.assertEqual(faults["events"][0]["bytes"], 4)
        self.assertEqual(faults["malformed_records"], 1)
        self.assertEqual(faults["omitted_events"], 0)
        log = Path(value["report_directory"]) / "engine.log"
        self.assertIn(raw, log.read_bytes())
        self.assertEqual(faults["source"]["sha256"], prepare.digest(log))
        self.assertFalse(value["report"]["graphical_apps_verified"])

    def test_engine_without_bus_trace_keeps_compatible_research_behavior(self):
        args, _ = self.runtime(encode(platform_fixture()))
        (self.root / "no-bus-trace").touch()
        inspected = dispatch("ios.emulator.inspect", {key: args[key] for key in ("engine", "firmware", "variant")})
        self.assertFalse(inspected["capabilities"]["arm_bus_fault_trace"])
        job = dispatch("ios.emulator.research", args)
        self.assertEqual(jobs.wait(job["id"], 10)["state"], "succeeded", jobs.logs(job["id"]))
        value = dispatch("ios.emulator.report", {"job_id": job["id"]})
        self.assertFalse(value["report"]["bus_faults"]["trace_enabled"])
        self.assertEqual(value["report"]["bus_faults"]["events"], [])
        self.assertNotIn("enable=tomacco_arm_bus_fault", json.loads((self.root / "engine-argv.json").read_text()))

    def test_diagnostic_panic_mentions_do_not_fail_valid_observations(self):
        args, _ = self.runtime(encode(platform_fixture()))
        (self.root / "console-fixture.log").write_bytes(
            b"log mentions panic(cpu 0 caller 0x123): ignored\n"
            b"panic(cpu x caller 0x123): malformed\n"
            b"panic(cpu 0 caller 0xz): malformed\n"
            b"panic(cpu 0 caller 0x123): \n" + b"x" * 2048 + b"\n")
        job = dispatch("ios.emulator.research", args)
        self.assertEqual(jobs.wait(job["id"], 10)["state"], "succeeded", jobs.logs(job["id"]))

    def test_serial_budget_failure_still_drains_for_clean_shutdown(self):
        args, _ = self.runtime(encode(platform_fixture()))
        (self.root / "serial-overflow").touch()
        job = dispatch("ios.emulator.research", args)
        self.assertEqual(jobs.wait(job["id"], 30)["state"], "failed")
        value = dispatch("ios.emulator.report", {"job_id": job["id"]})
        self.assertEqual(value["report"]["error"]["code"], "research_log_limit")
        folder = Path(value["report_directory"])
        self.assertEqual((folder / "console.log").stat().st_size, 8 * 1024 * 1024)
        self.assertIn("TOMACCO_BOOT_STORAGE_FLUSH", (folder / "engine.log").read_text())
        self.assertTrue(value["report"]["working_image_flushed"])
        self.assertTrue(any(row["request"]["execute"] == "quit" for row in value["report"]["qmp"]))
        request = json.loads((folder / "request.json").read_text())
        self.assertFalse(Path(request["machine_directory"]).exists())
        self.assertFalse(Path(request["socket_directory"]).exists())

    def test_interactive_single_snapshot_does_not_qualify_scheduled_research(self):
        args, _ = self.runtime(encode(platform_fixture("stdio")))
        job = dispatch("ios.emulator.research", args)
        self.assertEqual(jobs.wait(job["id"], 10)["state"], "failed")
        report = dispatch("ios.emulator.report", {"job_id": job["id"]})["report"]
        self.assertFalse(report["guest_observations_verified"])
        self.assertEqual(report["error"]["code"], "invalid_platform_observations")

    def test_failed_frame_capture_fails_job_after_clean_shutdown(self):
        args, _ = self.runtime(encode(platform_fixture()))
        (self.root / "fail-frame").touch()
        job = dispatch("ios.emulator.research", args)
        self.assertEqual(jobs.wait(job["id"], 10)["state"], "failed")
        value = dispatch("ios.emulator.report", {"job_id": job["id"]})
        self.assertEqual(value["report"]["error"]["code"], "qmp_error")
        self.assertTrue(value["report"]["guest_observations_verified"])
        folder = Path(value["report_directory"])
        request = json.loads((folder / "request.json").read_text())
        self.assertFalse(Path(request["machine_directory"]).exists())
        self.assertFalse(Path(request["socket_directory"]).exists())
        self.assertIn("TOMACCO_BOOT_STORAGE_FLUSH", (folder / "engine.log").read_text())

    def test_bad_evidence_and_missing_flush_are_failures(self):
        args, _ = self.runtime(encode(platform_fixture()))
        (self.root / "omit-flush").touch()
        job = dispatch("ios.emulator.research", args)
        self.assertEqual(jobs.wait(job["id"], 10)["state"], "failed")
        self.assertEqual(dispatch("ios.emulator.report", {"job_id": job["id"]})["report"]["error"]["code"], "storage_flush_unverified")
        (self.root / "platform-fixture.log").write_bytes(b'TOMACCO_PLATFORM {"seq":0,"seq":0}\n')
        job = dispatch("ios.emulator.research", args)
        self.assertEqual(jobs.wait(job["id"], 10)["state"], "failed")
        report = dispatch("ios.emulator.report", {"job_id": job["id"]})["report"]
        self.assertFalse(report["guest_observations_verified"])

    def test_repeated_start_reuses_job_conflict_is_rejected_and_cancel_cleans_process(self):
        args, _ = self.runtime()
        args["seconds"] = 60
        job = dispatch("ios.emulator.research", args)
        duplicate = dispatch("ios.emulator.research", args)
        self.assertEqual(duplicate["id"], job["id"])
        self.assertTrue(duplicate["reused"])
        for op, extra in (("ios.emulator.research", {"seconds": 30}), ("ios.emulator.probe", {})):
            with self.assertRaises(StudioError) as error:
                dispatch(op, {**args, **extra})
            self.assertEqual(error.exception.code, "ios_emulator_busy")
        pidfile = self.root / "engine.pid"
        deadline = time.monotonic() + 8 * PATIENCE
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(.03)
        self.assertTrue(pidfile.exists())
        jobs.cancel(job["id"])
        self.assertEqual(jobs.wait(job["id"], 10)["state"], "cancelled")
        process = Path("/proc") / pidfile.read_text() / "stat"
        self.assertTrue(not process.exists() or process.read_text().split(")", 1)[1].split()[0] == "Z")
        value = dispatch("ios.emulator.report", {"job_id": job["id"]})
        self.assertEqual(value["job_state"], "cancelled")
        request = json.loads((Path(value["report_directory"]) / "request.json").read_text())
        self.assertFalse(Path(request["machine_directory"]).exists())
        self.assertFalse(Path(request["socket_directory"]).exists())
