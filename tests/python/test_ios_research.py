"""Research input integrity; real guest I/O is qualified separately."""
import copy
import binascii
import hashlib
import json
from pathlib import Path
import plistlib
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import prepare_ios_research_inputs as research
import build_ios_research_bootstrap as bootstrap
import extract_ios_stackshots as stackshots


def digest(data):
    return hashlib.sha256(data).hexdigest()


def instruction(input_bytes, output_bytes, offset):
    return {"input_sha256": digest(input_bytes), "output_sha256": digest(output_bytes),
            "instructions": [{"offset": offset,
                              "before": hex(struct.unpack_from("<I", input_bytes, offset)[0]),
                              "after": hex(struct.unpack_from("<I", output_bytes, offset)[0])}]}


class ResearchInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.kernel, self.tree = self.root / "kernel", self.root / "tree"
        self.kernel.write_bytes(b"000011112222")
        self.tree.write_bytes(b"tree-before")
        self.ramdisk = instruction(self.kernel.read_bytes(), b"333311112222", 0)
        self.profile = {"firmware": "fixture", "build": "fixture", "device": "fixture",
                        "kernel": instruction(b"333311112222", b"333344442222", 4),
                        "device_tree": {"input_sha256": digest(self.tree.read_bytes()),
                                        "output_sha256": digest(b"tree-after!"),
                                        "edits": [{"offset": 5, "before": b"before".hex(),
                                                   "after": b"after!".hex()}]}}
        self.output = self.root / "output"

    def prepare(self):
        return research.prepare(self.kernel, self.tree, self.output, self.ramdisk, self.profile)

    def test_exact_outputs_preserved_inputs_and_no_unearned_qualification(self):
        originals = {file: file.read_bytes() for file in (self.kernel, self.tree)}
        before = {file: file.stat().st_mtime_ns for file in originals}
        record = self.prepare()
        self.assertEqual((self.output / "bootkc").read_bytes(), b"333344442222")
        self.assertEqual((self.output / "dtree").read_bytes(), b"tree-after!")
        self.assertEqual(json.loads((self.output / "research-inputs.json").read_text()), record)
        for file, data in originals.items():
            self.assertEqual(file.read_bytes(), data)
            self.assertEqual(file.stat().st_mtime_ns, before[file])
        for name in ("persistent_storage_qualified", "data_protection_qualified",
                     "full_ios_boot_qualified", "graphical_apps_qualified", "root_authentication_changed",
                     "enabled_in_product_guest_jobs"):
            self.assertIs(record[name], False)

    def test_tampering_expected_bytes_and_output_hash_fail_before_output(self):
        baseline = copy.deepcopy(self.profile)
        cases = (("input_sha256", "0" * 64), ("output_sha256", "0" * 64), ("bytes", "ffffffff"))
        for field, value in cases:
            with self.subTest(field=field):
                self.profile = copy.deepcopy(baseline)
                if field == "bytes":
                    self.profile["kernel"]["instructions"][0]["before"] = "0x" + value
                else:
                    self.profile["kernel"][field] = value
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.output.exists())

    def test_existing_output_and_dangling_link_are_never_replaced(self):
        self.output.mkdir()
        marker = self.output / "keep"
        marker.write_bytes(b"user content")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.prepare()
        self.assertEqual(marker.read_bytes(), b"user content")
        self.output = self.root / "linked-output"
        self.output.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.prepare()
        self.assertTrue(self.output.is_symlink())
        self.assertFalse((self.root / "missing").exists())

    def test_insertions_keep_original_offsets_and_invalid_ranges_are_rejected(self):
        data, expected = b"abcdefgh", b"aXYbcd12gh"
        recipe = {"input_sha256": digest(data), "output_sha256": digest(expected), "edits": [
            {"offset": 1, "before": "", "after": b"XY".hex()},
            {"offset": 4, "before": b"ef".hex(), "after": b"12".hex()}]}
        self.assertEqual(research.apply_edits(data, recipe), expected)
        for offset in (-1, True, 20, 1):
            with self.subTest(offset=offset):
                invalid = copy.deepcopy(recipe)
                invalid["edits"][1]["offset"] = offset
                with self.assertRaises(ValueError):
                    research.apply_edits(data, invalid)

    def test_already_widened_kernel_and_in_place_output(self):
        self.kernel.write_bytes(b"333311112222")
        self.assertEqual(self.prepare()["outputs"]["kernel"]["sha256"], digest(b"333344442222"))
        self.output = self.kernel
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.prepare()
        self.assertEqual(self.kernel.read_bytes(), b"333311112222")

    def test_single_data_layout_is_opt_in_and_checked_before_output(self):
        self.profile["single_data_tree"] = {
            "input_sha256": digest(b"tree-after!"), "output_sha256": digest(b"tree!"),
            "edits": [{"offset": 4, "before": b"-after".hex(), "after": ""}]}
        record = research.prepare(self.kernel, self.tree, self.output, self.ramdisk,
                                  self.profile, "single-data")
        self.assertEqual((self.output / "dtree").read_bytes(), b"tree!")
        self.assertEqual(record["data_layout"], "single-data")
        self.assertEqual(self.tree.read_bytes(), b"tree-before")
        self.output = self.root / "default-layout"
        self.assertEqual(self.prepare()["data_layout"], "split-users")
        self.assertEqual((self.output / "dtree").read_bytes(), b"tree-after!")
        self.output = self.root / "bad-layout"
        for layout in ("unknown", "single-data"):
            with self.subTest(layout=layout):
                self.profile["single_data_tree"]["input_sha256"] = "0" * 64
                with self.assertRaises(ValueError):
                    research.prepare(self.kernel, self.tree, self.output, self.ramdisk,
                                     self.profile, layout)
                self.assertFalse(self.output.exists())

    def test_hardware_profile_requires_matching_firmware_layout_and_tree(self):
        self.profile["single_data_tree"] = {
            "input_sha256": digest(b"tree-after!"), "output_sha256": digest(b"tree!"),
            "edits": [{"offset": 4, "before": b"-after".hex(), "after": ""}]}
        hardware = {"firmware": "fixture", "build": "fixture", "device": "fixture",
                    "data_layout": "single-data", "device_tree": {
                        "input_sha256": digest(b"tree!"), "output_sha256": digest(b"tree!DART"),
                        "edits": [{"offset": 5, "before": "", "after": b"DART".hex()}]}}
        file = self.root / "ios27-dcp-dart.json"
        file.write_text(json.dumps(hardware))
        with patch.object(research, "SOURCES", self.root):
            for layout, name in (("split-users", "dcp-dart"), ("single-data", "unknown")):
                with self.subTest(layout=layout, name=name), self.assertRaises(ValueError):
                    research.prepare(self.kernel, self.tree, self.output, self.ramdisk,
                                     self.profile, layout, name)
                self.assertFalse(self.output.exists())
            for key in ("firmware", "build", "device"):
                invalid = {**hardware, key: "different"}
                file.write_text(json.dumps(invalid))
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, "matching firmware"):
                    research.prepare(self.kernel, self.tree, self.output, self.ramdisk,
                                     self.profile, "single-data", "dcp-dart")
                self.assertFalse(self.output.exists())
            invalid = copy.deepcopy(hardware)
            invalid["device_tree"]["input_sha256"] = "0" * 64
            file.write_text(json.dumps(invalid))
            with self.assertRaisesRegex(ValueError, "Input SHA-256"):
                research.prepare(self.kernel, self.tree, self.output, self.ramdisk,
                                 self.profile, "single-data", "dcp-dart")
            self.assertFalse(self.output.exists())
            file.write_text(json.dumps(hardware))
            record = research.prepare(self.kernel, self.tree, self.output, self.ramdisk,
                                      self.profile, "single-data", "dcp-dart")
        self.assertEqual(record["hardware_profile"], "dcp-dart")
        self.assertEqual((self.output / "dtree").read_bytes(), b"tree!DART")
        self.assertEqual((self.output / "bootkc").read_bytes(), b"333344442222")
        self.assertEqual(self.tree.read_bytes(), b"tree-before")
        self.assertFalse(record["graphical_apps_qualified"])

    def test_exclave_hardware_recipe_is_chained_and_hash_checked(self):
        self.profile["single_data_tree"] = {"input_sha256": digest(b"tree-after!"),
            "output_sha256": digest(b"tree!"), "edits": [{"offset": 4, "before": b"-after".hex(), "after": ""}]}
        for name, before, after in (("dcp-dart", b"tree!", b"tree!DART"),
                                    ("exclave-dart", b"tree!DART", b"tree!DARTCL4"),
                                    ("exclave-mcc", b"tree!DARTCL4", b"tree!DARTCL4MCC")):
            record = {key: self.profile[key] for key in ("firmware", "build", "device")}
            record.update(data_layout="single-data", device_tree={
                "input_sha256": digest(before), "output_sha256": digest(after),
                "edits": [{"offset": len(before), "before": "", "after": after[len(before):].hex()}]})
            (self.root / ("ios27-" + name + ".json")).write_text(json.dumps(record))
        with patch.object(research, "SOURCES", self.root):
            record = research.prepare(self.kernel, self.tree, self.output, self.ramdisk,
                                      self.profile, "single-data", "exclave-dart")
            self.assertEqual((self.output / "dtree").read_bytes(), b"tree!DARTCL4")
            self.assertEqual(record["hardware_profile"], "exclave-dart")
            record = research.prepare(self.kernel, self.tree, self.root / "mcc", self.ramdisk,
                                      self.profile, "single-data", "exclave-mcc")
            self.assertEqual((self.root / "mcc/dtree").read_bytes(), b"tree!DARTCL4MCC")
            self.assertEqual(record["hardware_profile"], "exclave-mcc")
            mcc = self.root / "ios27-exclave-mcc.json"
            value = json.loads(mcc.read_text()); value["device_tree"]["output_sha256"] = "0" * 64
            mcc.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "Patched SHA-256"):
                research.prepare(self.kernel, self.tree, self.root / "bad-mcc", self.ramdisk,
                                 self.profile, "single-data", "exclave-mcc")
            self.assertFalse((self.root / "bad-mcc").exists())
            self.output = self.root / "bad-chain"
            file = self.root / "ios27-exclave-dart.json"
            value = json.loads(file.read_text()); value["device_tree"]["input_sha256"] = "0" * 64
            file.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "Input SHA-256"):
                research.prepare(self.kernel, self.tree, self.output, self.ramdisk,
                                 self.profile, "single-data", "exclave-dart")
            self.assertFalse(self.output.exists())
        self.assertEqual(self.tree.read_bytes(), b"tree-before")
        self.assertFalse(record["graphical_apps_qualified"])


class ResearchLaunchJobsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.jobs = {
            "com.apple.SpringBoard": ("/System/Library/CoreServices/SpringBoard.app/SpringBoard", "mobile"),
            "com.apple.backboardd": ("/usr/libexec/backboardd", "mobile"),
            "com.apple.mobile.usermanagerd": ("/usr/libexec/usermanagerd", "root")}
        for label, (program, user) in self.jobs.items():
            job = {"Label": label, "UserName": user, "ProgramArguments": [program],
                   "MachServices": {label: True}, "KeepAlive": True,
                   "EnvironmentVariables": {"PRESERVE": "fixture"}}
            (self.root / (label + ".plist")).write_bytes(plistlib.dumps(job))

    def test_diagnostics_preserve_service_contract_and_do_not_modify_inputs(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        jobs, record = bootstrap.diagnostic_jobs(self.root)
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)
        for name, original in before.items():
            changed = plistlib.loads(jobs[name])
            expected = plistlib.loads(original)
            self.assertEqual(changed["StandardOutPath"], record[name]["log"])
            self.assertEqual(changed["StandardOutPath"], changed["StandardErrorPath"])
            if expected["UserName"] == "mobile":
                self.assertTrue(changed["StandardOutPath"].startswith("/private/var/mobile/tmp/"))
            for key in ("Label", "UserName", "ProgramArguments", "MachServices", "KeepAlive"):
                self.assertEqual(changed[key], expected[key])
            self.assertEqual(changed["EnvironmentVariables"]["PRESERVE"], "fixture")
            self.assertEqual(record[name]["input_sha256"], digest(original))
            self.assertEqual(record[name]["output_sha256"], digest(jobs[name]))
        init = plistlib.loads(jobs["io.github.marselnenaj.tomacco.persona-init.plist"])
        self.assertTrue(init["LaunchOnlyOnce"])
        self.assertEqual(init["ProgramArguments"], ["/usr/libexec/usermanagerd", "--init"])

    def test_unexpected_program_owner_and_environment_are_rejected(self):
        path = self.root / "com.apple.backboardd.plist"
        original = path.read_bytes()
        for key, value in (("Label", "another.job"), ("ProgramArguments", ["/unrelated"]),
                           ("Program", "/unrelated"), ("UserName", "root"),
                           ("EnvironmentVariables", [])):
            with self.subTest(key=key):
                job = plistlib.loads(original)
                job[key] = value
                path.write_bytes(plistlib.dumps(job))
                with self.assertRaises(ValueError):
                    bootstrap.diagnostic_jobs(self.root)


class StackshotExtractionTests(unittest.TestCase):
    @staticmethod
    def item(kind, payload=b"", flags=0):
        return struct.pack("<IIQ", kind, len(payload), flags) + payload

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.output = self.root / "console.log", self.root / "captures"
        self.data = (self.item(0x59A25807) + self.item(0x32, struct.pack("<II", 1, 1) + b"\0" * 8)
                     + self.item(0xF19158ED))

    def log(self, data=None, pid=49, tick=60):
        data = self.data if data is None else data
        crc = f"{binascii.crc32(data):08x}"
        identity = f"tick={tick} pid={pid}"
        boundary = f"{identity} bytes={len(data)} crc32={crc}"
        chunks = [f"TOMACCO_STACKSHOT_DATA {identity} offset={start} hex={data[start:start + 16].hex()}"
                  for start in range(0, len(data), 16)]
        return "\r\r\n".join(["guest boot output", "TOMACCO_STACKSHOT_BEGIN " + boundary,
                                  *chunks, "TOMACCO_STACKSHOT_END " + boundary,
                                  f"TOMACCO_PROCESS sample_tick={tick} selected=1 captured=1 failed=0 finished=1",
                                  "guest continued"]).encode()

    def test_exact_binary_recovery_with_interleaved_guest_logs_and_multiple_targets(self):
        raw = self.log() + b"\n" + self.log(pid=0, tick=120)
        raw = raw.replace(b"TOMACCO_STACKSHOT_DATA tick=60 pid=49 offset=32",
                          b"unrelated kernel log\r\nTOMACCO_STACKSHOT_DATA tick=60 pid=49 offset=32")
        self.source.write_bytes(raw)
        record = stackshots.extract(self.source, self.output)
        self.assertEqual(len(record["captures"]), 2)
        self.assertEqual(record["source_sha256"], digest(raw))
        for entry in record["captures"]:
            path = self.output / entry["path"]
            self.assertEqual(path.read_bytes(), self.data)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(entry["sha256"], digest(self.data))
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        self.assertFalse(record["graphical_apps_qualified"])
        self.assertEqual(record["observation_ticks"], [60, 120])
        self.assertTrue(record["both_observations_completed"])
        self.assertEqual(record["boot_stage_observations"], [])

    def test_real_boot_stage_values_and_errors_are_preserved_without_claiming_readiness(self):
        raw = (b"TOMACCO_PROCESS tick=60 exclaves_boot_stage=4294967295\n" + self.log(pid=1) +
               b"\nTOMACCO_PROCESS tick=120 exclaves_boot_stage_result=-1 errno=2 bytes=4\n" +
               self.log(pid=1, tick=120))
        self.source.write_bytes(raw)
        record = stackshots.extract(self.source, self.output)
        self.assertEqual(record["boot_stage_observations"], [
            {"tick": 60, "value": 4294967295, "result": 0},
            {"tick": 120, "result": -1, "errno": 2, "bytes": 4}])
        self.assertTrue(record["both_observations_completed"])
        self.assertFalse(record["graphical_apps_qualified"])

    def test_unavailable_boot_stage_symbol_preserves_stackshots(self):
        self.source.write_bytes(b"TOMACCO_PROCESS tick=60 exclaves_boot_stage_symbol_unavailable\n" + self.log())
        record = stackshots.extract(self.source, self.output)
        self.assertEqual(record["boot_stage_observations"], [{"tick": 60, "error": "symbol_unavailable"}])
        self.assertEqual((self.output / record["captures"][0]["path"]).read_bytes(), self.data)

    def test_conflicting_or_malformed_boot_stage_evidence_leaves_no_output(self):
        valid = b"TOMACCO_PROCESS tick=60 exclaves_boot_stage=0\n"
        for invalid in (valid + valid, valid.replace(b"tick=60", b"tick=120"),
                        valid.replace(b"stage=0", b"stage=4294967296"), valid.replace(b"stage=0", b"stage=-1"),
                        valid.replace(b"stage=0", b"stage="),
                        b"TOMACCO_PROCESS tick=60 exclaves_boot_stage_result=0 errno=0 bytes=4\n",
                        b"TOMACCO_PROCESS tick=60 exclaves_boot_stage_result=-1 errno=2 bytes=18446744073709551616\n"):
            with self.subTest(invalid=invalid):
                self.source.write_bytes(invalid + self.log())
                with self.assertRaises(ValueError):
                    stackshots.extract(self.source, self.output)
                self.assertFalse(self.output.exists())

    def test_missing_whole_captures_failed_samples_and_truncated_summaries_are_rejected(self):
        raw = self.log()
        summary = next(line for line in raw.splitlines() if line.startswith(b"TOMACCO_PROCESS sample_tick="))
        for invalid in (raw.replace(summary, b""), raw.replace(b"selected=1 captured=1", b"selected=2 captured=2"),
                        raw.replace(b"selected=1 captured=1 failed=0", b"selected=2 captured=1 failed=1"),
                        raw.replace(summary, summary + b"\n" + summary),
                        raw.replace(b"finished=1", b"finished=")):
            self.source.write_bytes(invalid)
            with self.assertRaises(ValueError):
                stackshots.extract(self.source, self.output)
            self.assertFalse(self.output.exists())
        self.source.write_bytes(raw)
        record = stackshots.extract(self.source, self.output)
        self.assertEqual(record["observation_ticks"], [60])
        self.assertFalse(record["both_observations_completed"])

    def test_truncated_corrupted_reordered_and_duplicated_chunks_leave_no_output(self):
        raw = self.log()
        lines = raw.splitlines()
        chunks = [line for line in lines if line.startswith(b"TOMACCO_STACKSHOT_DATA")]
        cases = [raw[:raw.index(b"TOMACCO_STACKSHOT_END")],
                 raw.replace(chunks[0], chunks[0][:-2] + b"01"),
                 raw.replace(chunks[1], b""),
                 raw.replace(chunks[1], chunks[1] + b"\n" + chunks[1]),
                 raw.replace(b"offset=16", b"offset=32"),
                 raw.replace(chunks[1], chunks[1].replace(b"pid=49", b"pid=50"))]
        for invalid in cases:
            with self.subTest(invalid=invalid[-70:]):
                self.source.write_bytes(invalid)
                with self.assertRaises(ValueError):
                    stackshots.extract(self.source, self.output)
                self.assertFalse(self.output.exists())

    def test_duplicate_or_overlapping_captures_and_conflicting_end_are_rejected(self):
        raw = self.log()
        begin = next(line for line in raw.splitlines() if line.startswith(b"TOMACCO_STACKSHOT_BEGIN"))
        end = next(line for line in raw.splitlines() if line.startswith(b"TOMACCO_STACKSHOT_END"))
        for invalid in (raw + b"\n" + raw, raw.replace(begin, begin + b"\n" + begin),
                        raw.replace(end, end.replace(b"pid=49", b"pid=50")),
                        raw.replace(end, end[:-8] + b"00000000")):
            with self.assertRaises(ValueError):
                stackshots.parse_log(invalid)

    def test_valid_checksum_does_not_hide_malformed_kcdata(self):
        begin, end = self.item(0x59A25807), self.item(0xF19158ED)
        for data in (b"x" * 64, self.data[:-16], self.data + b"\0", begin + begin + end,
                     begin + self.item(0x14, flags=2) + end,
                     begin + self.item(0x13, b"\0" * 16, flags=1) + self.item(0x14, flags=2) + end):
            with self.assertRaises(ValueError):
                stackshots.parse_log(self.log(data))

    def test_existing_output_and_dangling_symlink_are_preserved(self):
        self.source.write_bytes(self.log())
        self.output.mkdir()
        marker = self.output / "user-file"
        marker.write_text("keep")
        with self.assertRaises(ValueError):
            stackshots.extract(self.source, self.output)
        self.assertEqual(marker.read_text(), "keep")
        link = self.root / "link"
        link.symlink_to(self.root / "missing")
        with self.assertRaises(ValueError):
            stackshots.extract(self.source, link)
        self.assertFalse((self.root / "missing").exists())

    def test_missing_invalid_and_oversized_records_fail(self):
        for raw in (b"TOMACCO_PROCESS stackshot_result=1", self.log(tick=30), self.log(pid=0x80000000),
                    self.log().replace(b"bytes=64", b"bytes=1048577"),
                    self.log().replace(b"offset=16", b"offset=-1"),
                    b"x" * (stackshots.MAX_LOG + 1)):
            with self.assertRaises(ValueError):
                stackshots.parse_log(raw)
