"""ASR and restored wire fixtures; these are not evidence of an iOS restore.

Exercise actual bytes, fragmented/coalesced frames, bounded streaming and
failure propagation independently of the original-firmware integration runs.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import ios_paravirt_asr as asr
import ios_paravirt_restore as restore
from ios_paravirt_firmware import FirmwareBundle
from test_ios_paravirt_firmware import TICKET, im4p
from test_ios_paravirt_manifest import ticket_for_image
from test_product import Isolated
from native_client import StudioError, dispatch, jobs


class FragmentedSocket:
    def __init__(self, data=b"", read_size=13, write_size=17, fail_after=None):
        self.data, self.output = bytearray(data), bytearray()
        self.read_size, self.write_size, self.fail_after = read_size, write_size, fail_after

    def settimeout(self, timeout):
        if timeout <= 0:
            raise AssertionError("Invalid timeout")

    def recv(self, size):
        size = min(size, self.read_size)
        value = bytes(self.data[:size])
        del self.data[:size]
        return value

    def send(self, value):
        if self.fail_after is not None and len(self.output) >= self.fail_after:
            raise ConnectionResetError("Fixture disconnect after partial write")
        size = min(len(value), self.write_size)
        self.output.extend(value[:size])
        return size


def commands(*values):
    return b"".join(plistlib.dumps(value) for value in values)


class ASRTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "system.dmg"
        self.data = bytes(range(251)) * 1201
        self.path.write_bytes(self.data)

    def image(self):
        image = asr.VerifiedImage(self.path, hashlib.sha384(self.data).digest(), time.monotonic() + 10)
        self.addCleanup(image.close)
        return image

    def transfer(self, values, **options):
        stream = FragmentedSocket(commands(*values), **options)
        return asr.ASRTransfer(asr.Wire(stream, time.monotonic() + 10), self.image()), stream

    def test_fragmented_and_coalesced_commands_oob_reinit_and_chunk_checksums(self):
        values = [{"Command": "Initiate"}, {"Command": "OOBData", "OOB Offset": 290001, "OOB Length": 321},
                  {"Command": "Initiate", "Checksum Chunks": True}, {"Command": "Payload"}]
        for read_size in (1, 8192):
            with self.subTest(read_size=read_size):
                transfer, stream = self.transfer(values, read_size=read_size)
                result = transfer.run()
                output = bytes(stream.output)
                end = output.index(b"</plist>\n") + len(b"</plist>\n")
                info = plistlib.loads(output[:end])
                self.assertEqual(info, {"Version": 1, "Stream ID": 1, "Payload": {"Port": 1, "Size": len(self.data)},
                                        "FEC Slice Stride": 40, "Packets Per FEC": 25, "Packet Payload Size": 1450})
                self.assertEqual(output[end:end + 321], self.data[290001:290322])
                output = output[end + 321:]
                end = output.index(b"</plist>\n") + len(b"</plist>\n")
                self.assertEqual(plistlib.loads(output[:end])["Checksum Chunk Size"], 131072)
                expected = bytearray()
                for offset in range(0, len(self.data), 131072):
                    chunk = self.data[offset:offset + 131072]
                    expected.extend(chunk)
                    expected.extend(hashlib.sha1(chunk, usedforsecurity=False).digest())
                self.assertEqual(output[end:], expected)
                self.assertEqual(result["payload_bytes"], len(self.data))
                self.assertEqual(result["checksum_bytes"], 60)
                self.assertTrue(result["payload_sent"])
                self.assertFalse(result["restore_completed"])

    def test_short_file_reads_preserve_checksum_boundaries(self):
        original = os.pread
        with patch.object(asr.os, "pread", side_effect=lambda fd, count, offset: original(fd, min(count, 1001), offset)):
            transfer, stream = self.transfer([{"Command": "Initiate", "Checksum Chunks": True}, {"Command": "Payload"}])
            transfer.run()
        wire = bytes(stream.output).split(b"</plist>\n", 1)[1]
        self.assertEqual(len(wire), len(self.data) + 60)
        self.assertEqual(wire[131072:131092], hashlib.sha1(self.data[:131072], usedforsecurity=False).digest())

    def test_rejects_invalid_validation_without_sending_payload(self):
        for request in ({"Command": "OOBData", "OOB Offset": -1, "OOB Length": 2},
                        {"Command": "OOBData", "OOB Offset": len(self.data) - 1, "OOB Length": 2},
                        {"Command": "OOBData", "OOB Offset": True, "OOB Length": 2},
                        {"Command": "OOBData", "OOB Offset": 0, "OOB Length": 0},
                        {"Command": "OOBData", "OOB Offset": 0, "OOB Length": len(self.data) * 3},
                        {"Command": "Initiate", "Checksum Chunks": 1}, {"Command": "Unknown"}):
            with self.subTest(request=request):
                transfer, _ = self.transfer([{"Command": "Initiate"}, request])
                with self.assertRaises(ValueError):
                    transfer.run()
                self.assertEqual(transfer.record["state"], "failed")
                self.assertFalse(transfer.record["payload_sent"])
        transfer, _ = self.transfer([{"Command": "Payload"}])
        with self.assertRaisesRegex(ValueError, "Initiate"):
            transfer.run()

    def test_digest_mismatch_and_changed_image_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "digest"):
            asr.VerifiedImage(self.path, bytes(48), time.monotonic() + 10)
        image = self.image()
        self.path.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            list(image.range(0, 1))
        link = self.root / "link"
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            asr.VerifiedImage(link, bytes(48), time.monotonic() + 10)

    def test_xml_bounds_malformed_and_entities(self):
        for value in (b"x" * asr.XML_LIMIT, b"<plist><dict></plist>",
                      b'<!DOCTYPE plist [<!ENTITY x "x">]><plist><dict/></plist>'):
            transfer = asr.ASRTransfer(asr.Wire(FragmentedSocket(value, read_size=8192), time.monotonic() + 2), self.image())
            with self.assertRaises(ValueError):
                transfer.run()

    def test_partial_write_is_not_replayed_and_deadline_is_total(self):
        stream = FragmentedSocket(write_size=7, fail_after=14)
        wire = asr.Wire(stream, time.monotonic() + 2)
        source = bytes(range(50))
        with self.assertRaises(ConnectionResetError):
            wire.send(source)
        self.assertEqual(bytes(stream.output), source[:14])
        self.assertEqual(wire.sent, 14)
        wire.deadline = time.monotonic() - 1
        with self.assertRaises(TimeoutError):
            wire.exact(1)


class RestorePeer:
    """Local protocol peer with a separate ASR connection and original framing."""
    def __init__(self, root, image, ending="success"):
        self.address = root / "usbmuxd.sock"
        self.image, self.ending = image, ending
        self.errors, self.messages, self.payload = [], [], b""
        self.entered = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(self.address))
        os.chmod(self.address, 0o600)
        self.listener.listen(4)
        self.listener.settimeout(5)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def accept(self, port):
        stream, _ = self.listener.accept()
        wire = asr.Wire(stream, time.monotonic() + 5)
        size, version, kind, tag = struct.unpack("<IIII", wire.exact(16))
        request = plistlib.loads(wire.exact(size - 16))
        assert (version, kind, tag) == (1, 8, 1)
        assert request["DeviceID"] == 1 and socket.ntohs(request["PortNumber"]) == port
        result = plistlib.dumps({"MessageType": "Result", "Number": 0})
        wire.send(struct.pack("<IIII", len(result) + 16, 1, 8, tag) + result)
        return wire

    def run(self):
        control = data = None
        try:
            control = self.accept(62078)
            self.messages.append(restore.receive_plist(control))
            self.entered.set()
            if self.ending == "wait":
                try:
                    control.exact(1)
                except EOFError:
                    return
                raise AssertionError("Expected cancellation while waiting for QueryType")
            restore.send_plist(control, {"Type": "com.apple.mobile.restored", "Result": "Success",
                                         "HardwareModel": "VPHONE600AP", "RestoreProtocolVersion": 15})
            for key, value in (("HardwareInfo", {"ChipID": 0xfe01, "BoardID": 0x91,
                                                 "UniqueChipID": 17, "SupportsImage4": True}),
                               ("SavedDebugInfo", {})):
                request = restore.receive_plist(control)
                assert request["Request"] == "QueryValue" and request["QueryKey"] == key
                restore.send_plist(control, {key: value})
            if self.ending == "expect-no-restore":
                try:
                    control.exact(1)
                except EOFError:
                    return
                raise AssertionError("Mismatched manifest must not start a restore")
            self.messages.append(restore.receive_plist(control))
            options = self.messages[-1]["RestoreOptions"]
            # Original restored assumes this dictionary exists during cleanup,
            # even when optional CrashLog delivery has not been negotiated.
            assert isinstance(options["SupportedMessageTypes"], dict)
            assert "CrashLog" not in options["SupportedMessageTypes"]
            assert "AsyncDataRequestMsg" not in options["SupportedMessageTypes"]
            assert options["SupportedAsyncDataTypes"] == {}
            assert "SupportedDataTypes" not in options
            for name in ("ProvisioningInfo", "ProvisioningAck", "ProvisioningStatusMsg", "BBUpdateStatusMsg", "MsgType"):
                assert options["SupportedMessageTypes"][name] is False
            restore.send_plist(control, {"MsgType": "DataRequestMsg", "DataType": "RootTicket"})
            self.messages.append(restore.receive_plist(control))
            attempts = 0 if self.ending == "no-image" else 4 if self.ending == "retry-limit" else 2 if self.ending == "retry" else 1
            for attempt in range(attempts):
                restore.send_plist(control, {"MsgType": "DataRequestMsg", "DataType": "SystemImageData", "DataPort": 32123})
                if attempt == 3:
                    try:
                        control.exact(1)
                    except EOFError:
                        return
                    raise AssertionError("Guest-requested retry budget was not enforced")
                data = self.accept(32123)
                data.send(plistlib.dumps({"Command": "Initiate", "Checksum Chunks": True}))
                response = bytearray()
                while b"</plist>" not in response:
                    response.extend(data.read(4096))
                assert plistlib.loads(response)["Payload"]["Size"] == len(self.image)
                data.send(plistlib.dumps({"Command": "Payload"}))
                for offset in range(0, len(self.image), 131072):
                    chunk = self.image[offset:offset + 131072]
                    received = data.exact(len(chunk))
                    assert data.exact(20) == hashlib.sha1(received, usedforsecurity=False).digest()
                    self.payload += received
                assert self.payload == self.image * (attempt + 1)
                data.stream.close()
                data = None
            restore.send_plist(control, {"MsgType": "ProgressMsg", "Progress": 100, "Operation": 66})
            for kind in ("ProvisioningInfo", "ProvisioningAck", "ProvisioningStatusMsg", "RestoreProtocol"):
                restore.send_plist(control, {"MsgType": kind, "FixtureNotification": True})
            restore.send_plist(control, {"MsgType": "BBUpdateStatusMsg", "Accepted": True})
            if self.ending == "disconnect":
                return
            restore.send_plist(control, {"MsgType": "RestoreAttestation",
                                         "RestoreAttestationState": "RestoreShouldAttest"})
            response = restore.receive_plist(control)
            assert response == {"RestoreShouldAttest": False}
            restore.send_plist(control, {"MsgType": "StatusMsg", "Status": 0 if self.ending != "error" else 14})
            self.messages.append(restore.receive_plist(control))
        except BaseException as error:
            self.errors.append(error)
        finally:
            for wire in (control, data):
                if wire:
                    wire.stream.close()
            self.listener.close()

    def join(self):
        self.thread.join(6)
        if self.thread.is_alive():
            raise AssertionError("Protocol peer did not exit")
        if self.errors:
            raise self.errors[0]


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = bytes(range(251)) * 601
        self.path, self.bundle = firmware_fixture(self.root, self.data)
        self.ticket = (self.root / "ticket.im4m").read_bytes()

    def test_socket_restore_requires_payload_and_original_final_success(self):
        for ending in ("success", "retry", "retry-limit", "error", "disconnect", "no-image"):
            with self.subTest(ending=ending):
                directory = self.root / ending
                directory.mkdir(mode=0o700)
                peer = RestorePeer(directory, self.data, ending)
                journal = restore.Journal(directory / "receipt")
                record = {"restore_completed": False}
                try:
                    with asr.VerifiedImage(self.path, hashlib.sha384(self.data).digest(), time.monotonic() + 5) as image:
                        session = restore.RestoreSession(peer.address, self.bundle, self.ticket, image,
                                                         time.monotonic() + 5, journal, record)
                        if ending in ("success", "retry"):
                            session.run()
                            self.assertTrue(record["restore_completed"])
                            self.assertTrue(record["guest_restore_completed"])
                            self.assertEqual(len(record["system_transfers"]), 2 if ending == "retry" else 1)
                        else:
                            with self.assertRaises((RuntimeError, EOFError, ValueError)):
                                session.run()
                            self.assertFalse(record["restore_completed"])
                finally:
                    journal.close()
                    peer.join()
                self.assertEqual(peer.messages[1]["Request"], "StartRestore")
                self.assertEqual(peer.messages[2], {"RootTicketData": self.ticket})
                if ending in ("success", "retry", "no-image", "error"):
                    self.assertEqual(peer.messages[-1], {"MsgType": "ReceivedFinalStatusMsg"})
                    self.assertTrue(record["final_status_acknowledged"])
                self.assertNotIn(self.ticket.hex(), (directory / "receipt/events.jsonl").read_text())

    def test_device_manifest_mismatch_closes_before_start_restore(self):
        peer = RestorePeer(self.root, self.data, "expect-no-restore")
        journal = restore.Journal(self.root / "receipt")
        record = {"restore_completed": False}
        try:
            with asr.VerifiedImage(self.path, hashlib.sha384(self.data).digest(), time.monotonic() + 5) as image:
                session = restore.RestoreSession(peer.address, self.bundle,
                                                 ticket_for_image(self.data, ecid=18), image,
                                                 time.monotonic() + 5, journal, record)
                with self.assertRaisesRegex(ValueError, "ECID"):
                    session.run()
        finally:
            journal.close()
            peer.join()
        self.assertEqual([m.get("Request") for m in peer.messages], ["QueryType"])
        self.assertFalse(record["restore_completed"])

    def test_boot_component_responses_use_real_image4_and_original_wire_keys(self):
        stream = FragmentedSocket()
        journal = restore.Journal(self.root / "components")
        self.addCleanup(journal.close)
        session = restore.RestoreSession(None, self.bundle, self.ticket, None,
                                         time.monotonic() + 5, journal, {})
        session.control = asr.Wire(stream, time.monotonic() + 5)
        expected = []
        for kind, name in (("KernelCache", "KernelCache"), ("DeviceTree", "DeviceTree"),
                           ("SystemImageRootHash", "SystemVolume"),
                           ("SystemImageCanonicalMetadata", "Ap,SystemVolumeCanonicalMetadata")):
            source = im4p(payload=name.encode())
            path = self.root / (name + ".im4p")
            path.write_bytes(source)
            self.bundle.components[name] = {"Info": {"Path": path.name}, "Digest": hashlib.sha384(source).digest()}
            expected.append({kind + "File": self.bundle.image(name, self.ticket)[0]})
            session.data_request({"DataType": kind})
        session.data_request({"DataType": "BuildIdentityDict", "Arguments": {"Variant": "Erase"}})
        session.data_request({"DataType": "ReceiptManifest"})
        wire = asr.Wire(FragmentedSocket(bytes(stream.output)), time.monotonic() + 5)
        for response in expected:
            self.assertEqual(restore.receive_plist(wire), response)
        self.assertEqual(restore.receive_plist(wire), self.bundle.build_identity_request({"Variant": "Erase"}))
        self.assertEqual(restore.receive_plist(wire), {"ReceiptManifest": self.bundle.components})
        self.assertEqual(wire.stream.data, b"")

    def test_lock_prevents_concurrent_restore_and_releases_on_error(self):
        address = self.root / "usbmuxd.sock"
        with restore.session_lock(address):
            with self.assertRaisesRegex(RuntimeError, "already owns"):
                with restore.session_lock(address):
                    self.fail("Second restore acquired the session")
        with restore.session_lock(address):
            pass

    def test_bridge_rejects_physical_devices_and_mismatched_manifest(self):
        bridge_path = self.root / "bridge.json"
        bridge_path.write_text(json.dumps({"format": 1, "state": "ready", "local_only": True,
                                           "physical_devices_exposed": True}))
        with self.assertRaisesRegex(ValueError, "private"):
            restore.inspect_bridge(bridge_path, TICKET)
        request = {"usb": {"protocol": "apple-vusb-v1", "local_only": True}, "engine_sha256": "test",
                   "persistent_disks": {name: {"path": name} for name in ("tomacco-root", "tomacco-aux")},
                   "persistent_sep_storage": {"path": "sep"}, "boot_manifest_handoff": {"sha256": "wrong"}}
        request_path = self.root / "request.json"
        request_path.write_text(json.dumps(request))
        bridge_path.write_text(json.dumps({"format": 1, "state": "ready", "local_only": True,
                                           "physical_devices_exposed": False, "request": str(request_path),
                                           "engine_sha256": "test"}))
        with self.assertRaisesRegex(ValueError, "manifest"):
            restore.inspect_bridge(bridge_path, TICKET)

    def test_cli_preflight_failure_is_machine_readable_and_nonzero(self):
        bridge_path = self.root / "bridge.json"
        bridge_path.write_text('{}')
        output = self.root / "failed"
        with contextlib.redirect_stdout(io.StringIO()) as text:
            result = restore.main(["--bridge", str(bridge_path), "--firmware", str(self.root), "--identity", "0",
                                   "--manifest", str(self.root / "ticket.im4m"), "--system-image", str(self.path),
                                   "--output", str(output)])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(text.getvalue())["state"], "failed")
        receipt = json.loads((output / "restore.json").read_text())
        self.assertFalse(receipt["restore_completed"])
        self.assertFalse(receipt["external_requests"])
        self.assertEqual((output / "restore.json").stat().st_mode & 0o777, 0o600)


def firmware_fixture(root, image):
    path = root / "system.dmg"
    path.write_bytes(image)
    components = {"OS": {"Info": {"Path": "system.dmg"}, "Digest": hashlib.sha384(image).digest()}}
    for name, tag in (("LLB", b"illb"), ("iBoot", b"ibot")):
        data = im4p(tag)
        (root / (name + ".im4p")).write_bytes(data)
        components[name] = {"Info": {"Path": name + ".im4p", "IsFirmwarePayload": True},
                            "Digest": hashlib.sha384(data).digest()}
    manifest = {"BuildIdentities": [{"ApChipID": "0xfe01", "ApBoardID": "0x91",
                                     "Info": {"DeviceClass": "vphone600ap"}, "Manifest": components}]}
    (root / "BuildManifest.plist").write_bytes(plistlib.dumps(manifest))
    (root / "ticket.im4m").write_bytes(ticket_for_image(image))
    return path, FirmwareBundle(root, 0)


class RestoreJobTests(Isolated):
    def fixture(self, ending="success"):
        self.data = bytes(range(251)) * 1101
        image, _ = firmware_fixture(self.root, self.data)
        self.peer = RestorePeer(self.root, self.data, ending)
        request = {"usb": {"protocol": "apple-vusb-v1", "local_only": True}, "engine_sha256": "fixture",
                   "persistent_disks": {name: {"path": str(self.root / name)} for name in ("tomacco-root", "tomacco-aux")},
                   "persistent_sep_storage": {"path": str(self.root / "sep")},
                   "boot_manifest_handoff": {"sha256": hashlib.sha256((self.root / "ticket.im4m").read_bytes()).hexdigest()}}
        request_path = self.root / "request.json"
        request_path.write_text(json.dumps(request))
        bridge_path = self.root / "bridge.json"
        bridge_path.write_text(json.dumps({"format": 1, "state": "ready", "local_only": True,
                                           "physical_devices_exposed": False, "request": str(request_path),
                                           "engine_sha256": "fixture", "socket": str(self.peer.address)}))
        return {"bridge": str(bridge_path), "firmware": str(self.root), "identity": 0,
                "manifest": str(self.root / "ticket.im4m"), "system_image": str(image), "seconds": 15}

    def tearDown(self):
        try:
            super().tearDown()
        finally:
            if hasattr(self, "peer"):
                self.peer.join()

    def test_compiled_agent_job_streams_and_reports_original_final_status(self):
        job = dispatch("ios.emulator.restore", self.fixture())
        done = jobs.wait(job["id"], 10)
        self.assertEqual(done["state"], "succeeded", jobs.logs(job["id"]))
        report = dispatch("ios.emulator.report", {"job_id": job["id"]})
        self.assertTrue(report["report"]["restore_completed"])
        self.assertEqual(report["report"]["system_transfer"]["payload_bytes"], len(self.data))
        self.assertFalse(report["report"]["qualified_for_app_development"])
        self.assertTrue((Path(report["report_directory"]) / "restore/events.jsonl").is_file())

    def test_duplicate_reuses_job_conflict_rejects_and_cancel_closes_transport(self):
        args = self.fixture("wait")
        job = dispatch("ios.emulator.restore", args)
        self.assertTrue(self.peer.entered.wait(5))
        repeated = dispatch("ios.emulator.restore", args)
        self.assertEqual(repeated["id"], job["id"])
        self.assertTrue(repeated["reused"])
        with self.assertRaises(StudioError) as error:
            dispatch("ios.emulator.restore", {**args, "identity": 1})
        self.assertEqual(error.exception.code, "ios_restore_busy")
        jobs.cancel(job["id"])
        self.assertEqual(jobs.wait(job["id"], 5)["state"], "cancelled")
        report = dispatch("ios.emulator.report", {"job_id": job["id"]})["report"]
        self.assertFalse(report["restore_completed"])
        self.assertEqual(report["state"], "cancelled")

    def test_guest_failure_is_retained_ahead_of_transport_disconnect(self):
        args = self.fixture("disconnect")
        (self.root / "uart.log").write_text(
            "CHECKPOINT FAILURE:(FAILURE:35) RESTORED:[0x064E] clean_nand [0]D(failed to clean NAND)\n")
        job = dispatch("ios.emulator.restore", args)
        self.assertEqual(jobs.wait(job["id"], 10)["state"], "failed")
        report = dispatch("ios.emulator.report", {"job_id": job["id"]})["report"]
        self.assertEqual(report["first_guest_failure"]["step"], "clean_nand")
        self.assertIn("closed", report["error"])
        self.assertFalse(report["restore_completed"])


if __name__ == "__main__":
    unittest.main()
