"""Original synthetic firmware fixtures; never evidence of an Apple OS boot."""
import copy
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from test_product import Isolated
from host_patience import PATIENCE
from native_client import ROOT, StudioError, dispatch, jobs

sys.path.insert(0, str(ROOT / "tools"))
import prepare_ios_pmp as pmp


def payload():
    image = bytearray(0x2040)
    struct.pack_into("<8I", image, 0, 0xfeedfacf, 0x100000c, 0, 5, 3, 432, 1, 0)
    for cursor, name, address, memory, offset, size, protection in (
            (32, b"__TEXT", 0x1000000, 4096, 4096, 4096, 5),
            (104, b"__DATA", 0x1001000, 8192, 8192, 64, 3)):
        struct.pack_into("<II16sQQQQiiII", image, cursor, 0x19, 72, name, address,
                         memory, offset, size, protection, protection, 0, 0)
    struct.pack_into("<IIII", image, 176, 5, 288, 6, 68)
    struct.pack_into("<Q", image, 176 + 16 + 32 * 8, 0x1000000)
    struct.pack_into("<I", image, 4096, 0x14000000)  # Original B-to-self instruction.
    image[8192:] = bytes(range(64))
    return bytes(image)


def der(tag, body):
    count = (len(body).bit_length() + 7) // 8
    length = bytes([len(body)]) if len(body) < 128 else bytes([0x80 | count]) + len(body).to_bytes(count, "big")
    return bytes([tag]) + length + body


def container(data):
    return der(0x30, der(0x16, b"IM4P") + der(0x16, b"pmpf") + der(0x16, b"0") + der(4, data))


def source(root):
    firmware = root / "firmware"
    firmware.mkdir()
    (firmware / "pmp.im4p").write_bytes(container(payload()))
    manifest = {"ProductVersion": "fixture", "ProductBuildVersion": "test", "BuildIdentities": [{
        "Info": {"DeviceClass": "d47ap", "Variant": "Customer Erase Install (IPSW)"},
        "ApChipID": "0x8140", "Manifest": {"PMP": {"Info": {"Path": "pmp.im4p"},
        "Digest": hashlib.sha384((firmware / "pmp.im4p").read_bytes()).digest()}}}]}
    file = root / "BuildManifest.plist"
    file.write_bytes(plistlib.dumps(manifest))
    return {"firmware": firmware, "manifest_path": file, "board": "d47ap", "output": root / "bundle, with spaces"}


class PmpImageTests(unittest.TestCase):
    def test_elf_preserves_code_entry_permissions_and_zero_fill(self):
        original = payload()
        image, layout = pmp.macho_elf(original)
        self.assertEqual(image[:7], b"\x7fELF\x02\x01\x01")
        self.assertEqual(struct.unpack_from("<Q", image, 24)[0], 0x1000000)
        self.assertEqual(layout["entry"], 0x1000000)
        for index, expected in enumerate(((5, 0x1000000, 4096, 4096), (6, 0x1001000, 64, 8192))):
            kind, flags, offset, vm, physical, size, memory, align = struct.unpack_from("<IIQQQQQQ", image, 64 + index * 56)
            self.assertEqual((flags, vm, size, memory), expected)
            self.assertEqual((kind, physical, align), (1, vm, 4096))
            source_offset = 4096 * (index + 1)
            self.assertEqual(image[offset:offset + size], original[source_offset:source_offset + size])

    def test_bad_macho_bounds_architecture_entry_and_load_commands(self):
        for offset, fmt, value in ((0,"I",0), (4,"I",12), (8,"I",2), (12,"I",2), (16,"I",65),
                (20,"I",0xffffffff), (28,"I",1), (36,"I",71), (56,"Q",0),
                (64,"Q",0x1000000000000000), (72,"Q",0), (80,"Q",0x10000), (92,"I",7),
                (104,"I",0x80000028), (128,"Q",0x1000000), (144,"Q",0x1000),
                (184,"I",7), (188,"I",67), (448,"Q",0x1001000), (448,"Q",0x1000002)):
            with self.subTest(offset=offset, value=value):
                data = bytearray(payload()); struct.pack_into("<" + fmt, data, offset, value)
                with self.assertRaises(ValueError): pmp.macho_elf(data)
        for data in (b"", payload()[:40], payload()[:-1], bytes(pmp.MAX_PAYLOAD + 1)):
            with self.assertRaises(ValueError): pmp.macho_elf(data)

    def test_der_requires_complete_canonical_uncompressed_pmp_container(self):
        good = container(payload())
        self.assertEqual(pmp.im4p_payload(good), payload())
        for data in (b"", good[:-1], good + b"x", good.replace(b"pmpf", b"sepi", 1),
                b"\x30\x80", b"\x30\x85\x01", b"\x30\x81\x04xxxx", b"\x30\x82\0\x80",
                container(b"bvx2" + bytes(16))):
            with self.assertRaises(ValueError): pmp.im4p_payload(data)


class PmpPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tomacco-pmp-prepare-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.args = source(self.root)

    def test_original_manifest_container_payload_and_elf_are_bound(self):
        before = {path: path.read_bytes() for path in (self.args["manifest_path"], self.args["firmware"] / "pmp.im4p")}
        result = pmp.prepare(**self.args)
        self.assertEqual(pmp.verify(self.args["output"]), result)
        self.assertFalse(result["firmware_executed"])
        self.assertFalse(result["power_services_qualified"])
        self.assertEqual((self.args["output"] / "pmp").read_bytes(), payload())
        for path in self.args["output"].iterdir(): self.assertEqual(path.stat().st_mode & 0o777, 0o400)
        for path, data in before.items(): self.assertEqual(path.read_bytes(), data)

    def test_digest_mismatch_preserves_sources_and_publishes_nothing(self):
        (self.args["firmware"] / "pmp.im4p").write_bytes(b"bad")
        with self.assertRaisesRegex(ValueError, "digest mismatch"): pmp.prepare(**self.args)
        self.assertFalse(self.args["output"].exists())
        self.assertFalse(list(self.root.glob(".tomacco-pmp-*")))

    def test_existing_output_is_never_replaced(self):
        output = self.args["output"]
        output.mkdir(); (output / "keep").write_text("mine")
        with self.assertRaisesRegex(ValueError, "Output exists"): pmp.prepare(**self.args)
        self.assertEqual((output / "keep").read_text(), "mine")
        alias = self.root / "alias"; alias.symlink_to(output)
        with self.assertRaisesRegex(ValueError, "Output exists"): pmp.prepare(**{**self.args, "output": alias})

    def test_source_escape_duplicate_identity_and_other_chips_are_rejected(self):
        file = self.args["manifest_path"]; original = plistlib.loads(file.read_bytes())
        identity = original["BuildIdentities"][0]
        for identities in ([], [identity, identity], [{**identity,"ApChipID":"0x8120"}]):
            file.write_bytes(plistlib.dumps({**original,"BuildIdentities":identities}))
            with self.assertRaisesRegex(ValueError, "matching T8140"): pmp.prepare(**self.args)
        modified = copy.deepcopy(original)
        for path in ("../outside", "/outside", "nested/../../outside"):
            modified["BuildIdentities"][0]["Manifest"]["PMP"]["Info"]["Path"] = path
            file.write_bytes(plistlib.dumps(modified))
            with self.assertRaises(ValueError): pmp.prepare(**self.args)
        file.write_bytes(plistlib.dumps(original))
        image = self.args["firmware"] / "pmp.im4p"
        outside = self.root / "outside"; image.rename(outside); image.symlink_to(outside)
        with self.assertRaises(ValueError): pmp.prepare(**self.args)

    def test_concurrent_source_change_leaves_no_partial_bundle(self):
        copy_file = shutil.copyfile
        def change(src, dst):
            result = copy_file(src, dst)
            Path(src).write_bytes(b"changed")
            return result
        with patch.object(pmp.shutil,"copyfile",side_effect=change):
            with self.assertRaisesRegex(ValueError,"changed during preparation"): pmp.prepare(**self.args)
        self.assertFalse(self.args["output"].exists())
        self.assertFalse(list(self.root.glob(".tomacco-pmp-*")))

    def test_modified_receipts_cannot_substitute_payload_or_executable(self):
        pmp.prepare(**self.args)
        root = self.args["output"]; file = root / "pmp-research.json"
        record = json.loads(file.read_text())
        for name, path in (("payload", root / "pmp"), ("executable", root / "pmp.elf")):
            original = path.read_bytes(); data = bytearray(original); data[-1] ^= 0xff
            path.chmod(0o600); file.chmod(0o600); path.write_bytes(data)
            modified = copy.deepcopy(record)
            modified["components"][name]["sha256"] = hashlib.sha256(data).hexdigest()
            file.write_text(json.dumps(modified))
            with self.assertRaisesRegex(ValueError,"payload does not match|ELF does not preserve"): pmp.verify(root)
            path.write_bytes(original)
        file.write_text(json.dumps(record))
        outside = self.root / "outside"; (root / "pmp").rename(outside); (root / "pmp").symlink_to(outside)
        with self.assertRaisesRegex(ValueError,"linked"): pmp.verify(root)

    def test_malformed_or_edited_receipts_do_not_qualify_firmware(self):
        pmp.prepare(**self.args)
        root = self.args["output"]; file = root / "pmp-research.json"
        original = json.loads(file.read_text()); file.chmod(0o600)
        for bad in ("[]", "null", '{"format":1,"format":1}', json.dumps({**original,"format":True})):
            file.write_text(bad)
            with self.assertRaises(ValueError): pmp.verify(root)
        file.write_text(json.dumps({**original,"manifest_digests_verified":False,"firmware_executed":True,
                                   "power_services_qualified":True,"graphical_apps_qualified":True}))
        record = pmp.verify(root)
        self.assertTrue(record["manifest_digests_verified"])
        for key in ("firmware_executed", "power_services_qualified", "graphical_apps_qualified"):
            self.assertFalse(record[key])


class PmpControlTests(Isolated):
    def fixture(self):
        self.inputs = source(self.root)
        pmp.prepare(**self.inputs)
        engine = self.root / "engine"
        engine.write_text("#!" + sys.executable + "\n" + (ROOT / "tests/fixtures/ios_research_engine.py").read_text().split("\n",1)[1]
                          .replace("darwin  Test-only machine", "tomacco-pmp-research  Test-only machine")
                          .replace("PC=0000000000001000", "PC=0000000001000000"))
        engine.chmod(0o700)
        return {"engine":str(engine),"firmware":str(self.inputs["output"]),"seconds":5}

    def report(self, job):
        return dispatch("ios.emulator.report", {"job_id":job["id"]})["report"]

    def assert_clean(self, job):
        for entry in job["plan"]["temporary_directories"]:
            self.assertFalse(Path(entry["path"]).exists())
        pid_file = self.root / "engine.pid"
        if pid_file.exists(): self.assertFalse(Path('/proc') .joinpath(pid_file.read_text()).exists())

    def test_preparation_is_a_durable_job_and_packaged_interface(self):
        inputs = source(self.root)
        args = {("manifest" if key == "manifest_path" else key): str(value) for key,value in inputs.items()}
        job = dispatch("ios.emulator.prepare_pmp", args)
        job = jobs.wait(job["id"], 10)
        self.assertEqual(job["state"],"succeeded",jobs.logs(job["id"]))
        self.assertEqual(pmp.verify(inputs["output"])["scope"], "isolated-pmp-firmware-research")
        self.assert_clean(job)
        with self.assertRaises(StudioError) as error: dispatch("ios.emulator.prepare_pmp", args)
        self.assertEqual(error.exception.code,"output_exists")

    def test_fault_report_preserves_exact_addresses_and_never_claims_boot(self):
        args = self.fixture()
        text = "tomacco_arm_bus_fault pc=0x1000000 physical=0x300e00040 virtual=0xffffffffcc801040 size=4 access=0 mmu=2 response=2\n"
        (self.root / "bus-fault-fixture.log").write_text(text.replace("virtual=","virt|SPLIT|ual="))
        job = dispatch("ios.emulator.pmp",{**args,"capture_ram":True}); final = jobs.wait(job["id"],10)
        self.assertEqual(final["state"],"failed")
        report = self.report(job)
        self.assertEqual(report["error"]["code"],"pmp_bus_fault")
        self.assertEqual(report["bus_faults"]["events"][0]["physical"],"0x300e00040")
        self.assertEqual(report["bus_faults"]["malformed_records"],0)
        self.assertTrue(report["firmware_instructions_observed"])
        self.assertTrue(report["source_executable_preserved"])
        self.assertTrue(report["engine_shutdown_verified"])
        self.assertEqual(dispatch("ios.emulator.memory", {"job_id":job["id"],"address":"0x1000000","length":4})["data"], "00010203")
        for field in ("pmp_boot_verified","power_services_verified","full_ios_boot_verified","graphical_apps_verified"):
            self.assertFalse(report[field])
        argv = json.loads((Path(job["report_directory"]) / "command.json").read_text())
        self.assertEqual(argv[argv.index('-machine')+1],"tomacco-pmp-research")
        self.assertEqual(argv[argv.index('-cpu')+1],"max,midr=0x610f0000,mp-affinity=0x0,tomacco-strict-sysregs=on")
        self.assertTrue(report["cpu_identity_verified"])
        self.assert_clean(final)

    def test_no_trace_no_machine_and_modified_input_fail_before_admission(self):
        args = self.fixture(); inspect = {k:args[k] for k in ("engine","firmware")}
        self.assertFalse(dispatch("ios.emulator.inspect_pmp",inspect)["capabilities"]["pmp_power_services"])
        (self.root / "no-bus-trace").touch()
        with self.assertRaises(StudioError) as error: dispatch("ios.emulator.pmp",args)
        self.assertEqual(error.exception.code,"pmp_trace_required")
        (self.root / "no-bus-trace").unlink()
        file = Path(args['engine']); text = file.read_text(); file.write_text(text.replace('tomacco-pmp-research  Test-only machine','other  Test-only machine'))
        with self.assertRaises(StudioError) as error: dispatch("ios.emulator.pmp",args)
        self.assertEqual(error.exception.code,"pmp_engine_required")
        file.write_text(text)
        image = Path(args['firmware']) / "pmp.elf"; image.chmod(0o600); image.write_bytes(b'changed')
        with self.assertRaises(StudioError) as error: dispatch("ios.emulator.pmp",args)
        self.assertEqual(error.exception.code,"invalid_pmp_bundle")
        self.assertEqual(jobs.listing(),[])

    def test_timeout_is_not_firmware_success(self):
        args = self.fixture()
        job = dispatch("ios.emulator.pmp",{**args,"seconds":1}); final = jobs.wait(job["id"],10)
        self.assertEqual(final["state"],"failed")
        report = self.report(job)
        self.assertEqual(report["error"]["code"],"pmp_observation_timeout")
        self.assertFalse(report["firmware_instructions_observed"])
        self.assertFalse(report["power_services_verified"])
        self.assert_clean(final)

    def test_asc_test_rig_does_not_qualify_a_controller_or_change_the_pmp_machine(self):
        args = self.fixture()
        inspect = {k: args[k] for k in ("engine", "firmware")}
        file = Path(args["engine"])
        original = file.read_text()
        for machine, trace, expected in ((False, False, False), (True, False, False),
                                         (False, True, False), (True, True, True)):
            with self.subTest(machine=machine, trace=trace):
                text = original
                if machine:
                    text = text.replace('tomacco-pmp-research  Test-only machine',
                                        'tomacco-pmp-research  Test-only machine\\ntomacco-asc-mailbox-test  Test-only machine')
                if trace:
                    text = text.replace('tomacco_arm_bus_fault', 'tomacco_arm_bus_fault\\ntomacco_asc_mailbox_mmio', 1)
                file.write_text(text)
                result = dispatch("ios.emulator.inspect_pmp", inspect)
                self.assertEqual(result["diagnostic_machines"]["asc_mailbox"], expected)
                self.assertEqual(result["machine"], "tomacco-pmp-research")
                for capability in ("asc_controller", "pmp_power_services", "graphical_apps"):
                    self.assertFalse(result["capabilities"][capability])

    def test_duplicate_jobs_reuse_and_conflicting_ios_runs_are_rejected(self):
        args = self.fixture(); args['seconds'] = 30
        job = dispatch("ios.emulator.pmp",args)
        duplicate = dispatch("ios.emulator.pmp",args)
        self.assertEqual(job["id"],duplicate["id"]);self.assertTrue(duplicate["reused"])
        for op, values in (("ios.emulator.pmp",{**args,"seconds":20}),
                ("ios.emulator.pmp",{**args,"controller":"asc-v6"}),
                ("ios.emulator.research",args),("ios.emulator.probe",args)):
            with self.assertRaises(StudioError) as error: dispatch(op,values)
            self.assertEqual(error.exception.code,"ios_emulator_busy")
        deadline = time.monotonic()+5*PATIENCE
        while not (self.root / 'engine.pid').exists():
            self.assertLess(time.monotonic(),deadline); time.sleep(.03)
        jobs.cancel(job["id"]); final = jobs.wait(job["id"],10)
        self.assertEqual(final["state"],"cancelled")
        self.assert_clean(final)

    def test_cpu_identity_is_canonical_verified_and_part_of_admission(self):
        args = {**self.fixture(), "cpu_midr":"0x611F0521", "cpu_affinity":"0x0000010000", "seconds":30}
        inspect = dispatch("ios.emulator.inspect_pmp",{k:v for k,v in args.items() if k != "seconds"})
        policy = inspect["cpu_policy"]
        self.assertEqual((policy["midr"],policy["affinity"],policy["implementer"],policy["part"],policy["variant"],policy["revision"]),
                         ("0x611f0521","0x10000","0x61","0x052",1,1))
        self.assertFalse(policy["measured_silicon_identity"])
        job = dispatch("ios.emulator.pmp",args)
        try:
            reused = dispatch("ios.emulator.pmp",{**args,"cpu_midr":"0x611f0521","cpu_affinity":"0x10000"})
            self.assertEqual(job["id"],reused["id"])
            self.assertTrue(reused["reused"])
            for field,value in (("cpu_midr","0x610f0000"),("cpu_affinity","0x0")):
                with self.assertRaises(StudioError) as error: dispatch("ios.emulator.pmp",{**args,field:value})
                self.assertEqual(error.exception.code,"ios_emulator_busy")
            # The report does not exist until the worker has published it, and
            # a loaded or containerised runner can need well over five seconds.
            deadline=time.monotonic()+60
            while not (self.report(job) or {}).get("cpu_identity_verified"):
                self.assertLess(time.monotonic(),deadline);time.sleep(.03)
            self.assertEqual(self.report(job)["cpu_policy"],policy)
        finally:
            jobs.cancel(job["id"])
            final=jobs.wait(job["id"],10)
        self.assert_clean(final)

    def test_invalid_cpu_identity_never_creates_a_job(self):
        args=self.fixture()
        for field,value in (("cpu_midr","0x100000000"),("cpu_midr","0x1,host=on"),
                ("cpu_midr","0x+1"),("cpu_midr",""),("cpu_affinity","0x1000000"),
                ("cpu_affinity","0x80000000"),("cpu_affinity","0x10000000000")):
            with self.subTest(field=field,value=value):
                with self.assertRaises(StudioError): dispatch("ios.emulator.pmp",{**args,field:value})
        self.assertEqual(jobs.listing(),[])
        self.assertFalse((self.root/'engine.pid').exists())

    def test_engine_identity_mismatch_stops_before_firmware_execution(self):
        args=self.fixture()
        (self.root/'wrong-cpu-identity').touch()
        (self.root/'bus-fault-fixture.log').write_text('tomacco_arm_bus_fault pc=0x1000000 physical=0x300e00040 virtual=0x300e00040 size=4 access=0 mmu=2 response=2\n')
        job=dispatch("ios.emulator.pmp",args);final=jobs.wait(job['id'],10)
        self.assertEqual(final['state'],'failed')
        report=self.report(job)
        self.assertEqual(report['error']['code'],'pmp_cpu_identity_mismatch')
        self.assertFalse(report.get('cpu_identity_verified',False))
        self.assertFalse(report['firmware_instructions_observed'])
        self.assertEqual(report['bus_faults']['events'],[])
        self.assert_clean(final)

    def test_controller_requires_machine_and_trace_and_explicit_selection(self):
        args = self.fixture()
        inspect = {k: args[k] for k in ("engine", "firmware")}
        file = Path(args["engine"]); original = file.read_text()
        for machine, trace in ((False, False), (True, False), (False, True), (True, True)):
            text = original
            if machine:
                text = text.replace('tomacco-pmp-research  Test-only machine',
                                    'tomacco-pmp-research  Test-only machine\\ntomacco-pmp-asc-research  Test-only machine')
            if trace:
                text = text.replace('tomacco_arm_bus_fault', 'tomacco_arm_bus_fault\\ntomacco_asc_controller_mmio', 1)
            file.write_text(text)
            result = dispatch("ios.emulator.inspect_pmp", inspect)
            self.assertEqual(result["experimental_asc_available"], machine and trace)
            self.assertEqual(result["controller"], "none")
            self.assertIsNone(result["controller_policy"])
            if machine and trace:
                result = dispatch("ios.emulator.inspect_pmp", {**inspect,"controller":"asc-v6"})
                self.assertEqual(result["machine"], "tomacco-pmp-asc-research")
                self.assertTrue(result["controller_policy"]["architectural_timers_connected"])
                self.assertFalse(result["capabilities"]["asc_controller"])
            else:
                with self.assertRaises(StudioError) as error:
                    dispatch("ios.emulator.pmp", {**args,"controller":"asc-v6"})
                self.assertEqual(error.exception.code, "pmp_controller_required")
        self.assertEqual(jobs.listing(), [])

    def test_controller_reports_snapshot_failures_without_inventing_state(self):
        args = self.fixture()
        file = Path(args["engine"])
        file.write_text(file.read_text().replace('tomacco-pmp-research  Test-only machine',
                        'tomacco-pmp-research  Test-only machine\\ntomacco-pmp-asc-research  Test-only machine')
                        .replace('tomacco_arm_bus_fault', 'tomacco_arm_bus_fault\\ntomacco_asc_controller_mmio', 1))
        (self.root / "bus-fault-fixture.log").write_text(
            'tomacco_arm_bus_fault pc=0x1000000 physical=0x300e10000 virtual=0x300e10000 size=4 access=0 mmu=2 response=2\n')
        for fixture in ({"revision":1,"queues":[{"count":2}],"reads":14}, {"revision":99}, "x"*16385):
            (self.root / "controller-snapshot.json").write_text(json.dumps(fixture))
            job = dispatch("ios.emulator.pmp", {**args,"controller":"asc-v6"})
            final = jobs.wait(job["id"],10)
            report = self.report(job)
            self.assertEqual(report["controller"], "asc-v6")
            self.assertFalse(report["power_services_verified"])
            self.assertEqual(report["error"]["code"], "pmp_bus_fault")
            for observation in report["observations"]:
                if isinstance(fixture, dict) and fixture.get("revision") == 1:
                    self.assertEqual(observation["controller_state"], fixture)
                    self.assertNotIn("controller_snapshot_error", observation)
                else:
                    self.assertNotIn("controller_state", observation)
                    self.assertEqual(observation["controller_snapshot_error"]["code"], "invalid_controller_snapshot")
            argv=json.loads((Path(job["report_directory"]) / "command.json").read_text())
            self.assertEqual(argv[argv.index("-machine")+1], "tomacco-pmp-asc-research")
            self.assertIn("enable=tomacco_asc_controller_mmio", argv)
            self.assert_clean(final)

    def test_breakpoint_and_capture_preserve_code_and_read_exact_64_bit_values(self):
        args = self.fixture()
        source_hash = pmp.checksum(Path(args["firmware"]) / "pmp.elf")
        job = dispatch("ios.emulator.pmp", {**args,"stop_at":"0x1000004","capture_ram":True})
        final = jobs.wait(job["id"],10)
        self.assertEqual(final["state"], "succeeded", jobs.logs(job["id"]))
        report = self.report(job)
        self.assertEqual(report["state"], "pmp_breakpoint_reached")
        self.assertTrue(report["breakpoint_hit"])
        self.assertTrue(report["firmware_instructions_observed"])
        self.assertTrue(report["engine_shutdown_verified"])
        self.assertFalse(report["debug_policy"]["guest_code_written"])
        self.assertEqual(report["observations"][-1]["pc"], 0x1000004)
        registers = {item["name"]:item["value"] for item in report["observations"][-1]["cpu_registers"]}
        self.assertEqual(registers["X00"], "0xffffffffcc810080")
        self.assertEqual(registers["SP"], "0x00000000011ffff0")
        self.assertEqual(pmp.checksum(Path(args["firmware"]) / "pmp.elf"), source_hash)
        capture = report["memory_capture"]
        self.assertEqual(capture["bytes"], 2097152)
        self.assertEqual(Path(capture["path"]).stat().st_mode & 0o777, 0o600)
        self.assertTrue(capture["guest_stopped"])
        self.assertIn(capture["path"], [item["path"] for item in final["result"]["artifacts"]])
        data = dispatch("ios.emulator.memory", {"job_id":job["id"],"address":"0x1000001","length":17})
        self.assertEqual(data["data"], bytes(range(1,18)).hex())
        self.assertEqual(data["u64_le"][0], "0x0807060504030201")
        self.assertEqual(data["u32_le"][0], "0x04030201")
        self.assertEqual(data["ascii"], "." * 17)
        edge = dispatch("ios.emulator.memory", {"job_id":job["id"],"address":"0x11ffffd","length":3})
        self.assertEqual(edge["data"], "fdfeff")
        commands = [json.loads(line) for line in (self.root / "debug-commands.jsonl").read_text().splitlines()]
        self.assertEqual(commands, ["qSupported:hwbreak+", "?", "Z1,1000004,4", "c"])
        self.assertNotIn("cont", [item.get("request",{}).get("execute") for item in report["qmp"]])
        for field in ("pmp_boot_verified","power_services_verified","full_ios_boot_verified","graphical_apps_verified"):
            self.assertFalse(report[field])
        self.assert_clean(final)

    def test_entry_breakpoint_does_not_claim_instruction_execution_or_implicit_ram(self):
        args = self.fixture()
        job = dispatch("ios.emulator.pmp", {**args,"stop_at":"0x1000000"})
        self.assertEqual(jobs.wait(job["id"],10)["state"], "succeeded", jobs.logs(job["id"]))
        report = self.report(job)
        self.assertFalse(report["firmware_instructions_observed"])
        self.assertNotIn("memory_capture", report)
        self.assertFalse((Path(job["report_directory"]) / "ram.bin").exists())
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.memory", {"job_id":job["id"],"address":"0x1000000"})
        self.assertEqual(error.exception.code, "pmp_memory_unavailable")

    def test_breakpoints_require_aligned_verified_file_backed_instructions(self):
        args = self.fixture()
        for address in ("0x1000002", "0xfffffc", "0x1001000", "0x1001040", "0x1200000", "0xfffffffffffffffc"):
            with self.subTest(address=address), self.assertRaises(StudioError) as error:
                dispatch("ios.emulator.pmp", {**args,"stop_at":address})
            self.assertEqual(error.exception.code, "invalid_pmp_breakpoint")
        self.assertEqual(jobs.listing(), [])
        self.assertFalse((self.root / "engine.pid").exists())

    def test_incorrect_stop_incomplete_capture_and_unsupported_breakpoint_fail(self):
        args = self.fixture()
        for marker, content, code in (("breakpoint-pc", "0x1000008", "pmp_debug_stop"),
                ("short-memory-capture", "", "pmp_memory_capture"),
                ("reject-breakpoint", "", "pmp_breakpoint_unsupported")):
            with self.subTest(marker=marker):
                file = self.root / marker; file.write_text(content)
                job = dispatch("ios.emulator.pmp", {**args,"stop_at":"0x1000004","capture_ram":True})
                final = jobs.wait(job["id"],10)
                self.assertEqual(final["state"], "failed")
                report = self.report(job)
                self.assertEqual(report["error"]["code"], code, report)
                self.assertFalse(report["pmp_boot_verified"])
                if marker == "short-memory-capture":
                    self.assertEqual(report["memory_capture_error"]["code"], code)
                    self.assertNotIn("memory_capture", report)
                self.assert_clean(final)
                file.unlink()

    def test_memory_bounds_hash_metadata_and_symlink_are_checked(self):
        args = self.fixture()
        job = dispatch("ios.emulator.pmp", {**args,"stop_at":"0x1000000","capture_ram":True})
        self.assertEqual(jobs.wait(job["id"],10)["state"], "succeeded")
        for address, length in (("0xffffff",1),("0x11fffff",2),("0x1200000",1),("0xffffffffffffffff",4096)):
            with self.subTest(address=address), self.assertRaises(StudioError) as error:
                dispatch("ios.emulator.memory", {"job_id":job["id"],"address":address,"length":length})
            self.assertEqual(error.exception.code, "invalid_memory_range")
        request = {"job_id":job["id"],"address":"0x1000000"}
        capture = Path(job["report_directory"]) / "ram.bin"
        original = capture.read_bytes()
        for data in (original[:-1], b"x" + original[1:]):
            capture.write_bytes(data)
            with self.assertRaises(StudioError) as error: dispatch("ios.emulator.memory", request)
            self.assertEqual(error.exception.code, "pmp_memory_modified")
        capture.write_bytes(original)
        outside = self.root / "outside.bin"; capture.rename(outside); capture.symlink_to(outside)
        with self.assertRaises(StudioError): dispatch("ios.emulator.memory", request)
        capture.unlink(); outside.rename(capture)
        file = Path(job["report_directory"]) / "report.json"
        report = json.loads(file.read_text()); report["memory_capture"]["path"] = str(outside)
        file.write_text(json.dumps(report))
        with self.assertRaises(StudioError) as error: dispatch("ios.emulator.memory", request)
        self.assertEqual(error.exception.code, "pmp_memory_modified")

    def test_breakpoint_identity_cancellation_and_running_capture_rejection(self):
        args = self.fixture(); (self.root / "hold-breakpoint").touch()
        args.update(seconds=30,stop_at="0x0100000C",capture_ram=True)
        job = dispatch("ios.emulator.pmp", args)
        duplicate = dispatch("ios.emulator.pmp", {**args,"stop_at":"0x100000c"})
        self.assertEqual(job["id"], duplicate["id"])
        self.assertTrue(duplicate["reused"])
        for difference in ({"stop_at":"0x1000004"},{"capture_ram":False}):
            with self.assertRaises(StudioError) as error: dispatch("ios.emulator.pmp", {**args,**difference})
            self.assertEqual(error.exception.code, "ios_emulator_busy")
        deadline = time.monotonic()+5*PATIENCE
        while not (self.root / "debug-commands.jsonl").exists() or '"c"' not in (self.root / "debug-commands.jsonl").read_text():
            self.assertLess(time.monotonic(),deadline); time.sleep(.03)
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.memory", {"job_id":job["id"],"address":"0x1000000"})
        self.assertEqual(error.exception.code, "pmp_capture_pending")
        jobs.cancel(job["id"]); final = jobs.wait(job["id"],10)
        self.assertEqual(final["state"], "cancelled")
        self.assert_clean(final)

    def test_strict_cpu_policy_is_required_and_observed_before_execution(self):
        args = self.fixture()
        (self.root / "no-sysreg-trace").touch()
        with self.assertRaises(StudioError) as error: dispatch("ios.emulator.pmp",args)
        self.assertEqual(error.exception.code,"pmp_cpu_policy_required")
        self.assertEqual(jobs.listing(),[])
        (self.root / "no-sysreg-trace").unlink()
        for name in ("wrong-cpu-policy", "wrong-pmp-tlbi"):
            marker=self.root / name; marker.touch()
            job = dispatch("ios.emulator.pmp",args); final = jobs.wait(job["id"],10)
            self.assertEqual(final["state"],"failed")
            report = self.report(job)
            self.assertEqual(report["error"]["code"],"pmp_cpu_policy_required")
            self.assertFalse(report["firmware_instructions_observed"])
            self.assert_clean(final)
            marker.unlink()

    def test_executed_system_register_fault_is_decoded_and_keeps_ram(self):
        args = self.fixture()
        (self.root / "bus-fault-fixture.log").write_text(
            "tomacco_arm_sysreg_fault pc=0x1000004 instruction=0xd53ff|SPLIT|4c0 el=1\n")
        job = dispatch("ios.emulator.pmp",{**args,"capture_ram":True}); final = jobs.wait(job["id"],10)
        self.assertEqual(final["state"],"failed")
        report = self.report(job)
        self.assertTrue(report["strict_sysregs_verified"])
        self.assertTrue(report["firmware_instructions_observed"])
        self.assertEqual(report["error"]["code"],"pmp_sysreg_fault")
        fault = report["sysreg_faults"]["events"][0]
        self.assertEqual(fault["register"],"S3_7_c15_c4_6")
        self.assertEqual(fault["pc"],"0x1000004")
        self.assertEqual(fault["access"],"read")
        self.assertEqual(report["sysreg_faults"]["malformed_records"],0)
        self.assertEqual(report["bus_faults"]["events"],[])
        self.assertTrue(report["engine_shutdown_verified"])
        self.assertEqual(dispatch("ios.emulator.memory",{"job_id":job["id"],"address":"0x1000000","length":4})["data"],"00010203")
        self.assert_clean(final)


if __name__ == "__main__":
    unittest.main()
