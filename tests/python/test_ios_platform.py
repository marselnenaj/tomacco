"""Host-side evidence integrity; genuine IOKit execution is qualified separately."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import extract_ios_platform as platform
import build_ios_research_bootstrap as bootstrap


def fixture(mode="uart", failed_query=None):
    events = [dict(event="begin", format=1, uid=0, pid=118, mode=mode)]
    for tick in ([30, 90] if mode == "uart" else [0]):
        events.append(dict(event="snapshot", tick=tick, busy=1, busy_error=0))
        count = 0
        for query in platform.CLASSES:
            present = query in ("IOPlatformExpertDevice", "AppleKeyStore") and query != failed_query
            if present:
                events.append(dict(event="service", tick=tick, query=query, name=query, **{"class": query},
                                   path="IOService:/" + query, registry_id=f"0x{count+1:016x}", busy=1,
                                   errors=dict(name=0, **{"class": 0}, path=0, id=0, busy=0)))
                count += 1
            events.append(dict(event="class", tick=tick, query=query, result=-1 if query == failed_query else 0,
                               count=int(present), truncated=False))
        for path in platform.NODE_PATHS:
            found = not path.endswith("/sgx")
            data = b"arm-io\0AppleARM\0" if path == platform.NODE_PATHS[0] else b""
            events.append(dict(event="node", tick=tick, path=path, found=found,
                               compatible_type="data" if data else "absent", compatible_hex=data.hex(), property_error=0))
        events.append(dict(event="snapshot-end", tick=tick, services=count))
    events.append(dict(event="end", snapshots=2 if mode == "uart" else 1))
    return events


def encode(events):
    return b"".join(("TOMACCO_PLATFORM " + json.dumps(dict(seq=i, **event)) + "\n").encode()
                    for i, event in enumerate(events))


class PlatformObservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.output = self.root / "uart.log", self.root / "evidence"

    def extract(self, events):
        self.source.write_bytes(encode(events))
        return platform.extract(self.source, self.output)

    def test_complete_inventory_preserves_identity_and_never_claims_working_hardware(self):
        record = self.extract(fixture())
        self.assertTrue(record["completed"])
        self.assertEqual(record["completed_snapshots"], 2)
        self.assertEqual(record["source_sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())
        for observation in record["observations"]:
            self.assertFalse(observation["mobile_framebuffer_present"])
            self.assertFalse(observation["apple_sep_manager_present"])
        self.assertTrue(all(record[key] is False for key in record if key.endswith("_qualified")))
        observed = json.loads((self.output / "observations.json").read_text())
        self.assertEqual(observed["snapshots"][0]["services"][1]["class"], "AppleKeyStore")
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.output / "manifest.json").stat().st_mode & 0o777, 0o600)

    def test_failed_query_is_unknown_not_absent_and_stdio_is_one_snapshot(self):
        record = self.extract(fixture("stdio", failed_query="IOMobileFramebuffer"))
        self.assertTrue(record["completed"])
        self.assertEqual(record["completed_snapshots"], 1)
        self.assertIsNone(record["observations"][0]["mobile_framebuffer_present"])

    def test_provider_identity_distinguishes_device_tree_from_service_plane(self):
        events = fixture("stdio")
        events[0]["format"] = 2
        for event in events:
            if event["event"] == "node":
                event.update({"class": "AppleARMIODevice" if event["found"] else "",
                              "class_error": 0 if event["found"] else -1,
                              "service_path": "", "service_path_error": -1})
        record = self.extract(events)
        self.assertTrue(record["completed"])
        report = json.loads((self.output / "observations.json").read_text())
        node = report["snapshots"][0]["nodes"][platform.NODE_PATHS[1]]
        self.assertEqual(node["class"], "AppleARMIODevice")
        self.assertEqual(node["service_path_error"], -1)
        invalid = copy.deepcopy(events)
        next(e for e in invalid if e["event"] == "node").pop("class")
        with self.assertRaises(ValueError):
            platform.parse_log(encode(invalid))
        absent = next(e for e in events if e["event"] == "node" and not e["found"])
        absent["service_path"] = "IOService:/fabricated"
        with self.assertRaisesRegex(ValueError, "provider"):
            platform.parse_log(encode(events))

    def test_interrupted_second_snapshot_retains_only_completed_observations(self):
        events = fixture()
        end = next(i for i, event in enumerate(events) if event["event"] == "snapshot" and event["tick"] == 90)
        record = self.extract(events[:end+2])
        self.assertFalse(record["completed"])
        self.assertEqual(record["completed_snapshots"], 1)
        report = json.loads((self.output / "observations.json").read_text())
        self.assertEqual(report["partial_snapshot"]["tick"], 90)
        self.assertEqual(len(record["observations"]), 1)

    def test_loader_failure_is_preserved_without_inventing_inventory(self):
        record = self.extract([fixture()[0], dict(event="error", stage="IOServiceMatching")])
        self.assertFalse(record["completed"])
        self.assertEqual(record["observations"], [])
        self.assertEqual(record["terminal_error"], "IOServiceMatching")

    def test_missing_duplicate_and_contradictory_records_are_rejected(self):
        base = fixture("stdio")
        first_class = next(i for i, event in enumerate(base) if event["event"] == "class")
        first_node = next(i for i, event in enumerate(base) if event["event"] == "node")
        cases = []
        missing = copy.deepcopy(base); del missing[first_class]; cases.append(missing)
        duplicate = copy.deepcopy(base); duplicate.insert(first_class, duplicate[first_class]); cases.append(duplicate)
        count = copy.deepcopy(base); count[first_class]["count"] = 2; cases.append(count)
        truncated = copy.deepcopy(base); truncated[first_class]["truncated"] = True; cases.append(truncated)
        failed = copy.deepcopy(base); failed[first_class]["result"] = -1; cases.append(failed)
        negative = copy.deepcopy(base); negative[1]["busy"] = -1; cases.append(negative)
        wrong_tick = copy.deepcopy(base); wrong_tick[first_class]["tick"] = 30; cases.append(wrong_tick)
        malformed = copy.deepcopy(base); malformed[first_node]["compatible_hex"] = "x1"; cases.append(malformed)
        absent = copy.deepcopy(base); absent[first_node]["found"] = False; cases.append(absent)
        premature = copy.deepcopy(base); premature.pop(-2); cases.append(premature)
        after_end = copy.deepcopy(base); after_end.append(dict(event="error", stage="late")); cases.append(after_end)
        for number, events in enumerate(cases):
            with self.subTest(case=number), self.assertRaises(ValueError):
                self.extract(events)
            self.assertFalse(self.output.exists())

    def test_duplicate_fields_truncation_invalid_types_and_budgets_are_rejected(self):
        valid = encode(fixture("stdio"))
        cases = [b"", valid[:-8], b"kernel noise\n" + valid,
                 valid.replace(b'"seq": 0', b'"seq": 1', 1),
                 valid.replace(b'"seq": 0', b'"seq": 0, "seq": 0', 1),
                 valid.replace(b'"uid": 0', b'"uid": false', 1),
                 valid.replace(b'"format": 1', b'"format": true', 1),
                 valid.replace(b'"busy": 1', b'"busy": NaN', 1), b"\n" * (platform.MAX_LOG + 1)]
        for number, raw in enumerate(cases):
            with self.subTest(case=number), self.assertRaises(ValueError):
                platform.parse_log(raw)

    def test_previous_evidence_and_dangling_output_link_are_preserved(self):
        self.extract(fixture())
        previous = (self.output / "manifest.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.extract(fixture("stdio"))
        self.assertEqual((self.output / "manifest.json").read_bytes(), previous)
        self.output = self.root / "link"
        self.output.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.extract(fixture())
        self.assertTrue(self.output.is_symlink())

    def test_platform_launch_is_once_root_and_independent_of_application_diagnostics(self):
        job = bootstrap.platform_job()
        self.assertEqual(job["ProgramArguments"], ["/tomacco/platform-probe"])
        self.assertEqual(job["UserName"], "root")
        self.assertTrue(job["LaunchOnlyOnce"])
        self.assertNotIn("KeepAlive", job)
        self.assertNotEqual(job["Label"], bootstrap.application_job()["Label"])
        header = bootstrap.platform_header()
        self.assertTrue(all(json.dumps(name) in header for name in platform.CLASSES + platform.NODE_PATHS))


if __name__ == "__main__":
    unittest.main()
