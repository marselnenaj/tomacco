#!/usr/bin/env python3
"""Restore a vphone600 guest through its private Tomacco USB bridge.

Writes the virtual disks attached to that session. Requires local firmware,
an existing IM4M and a digest-matched OS image. No downloads, signing requests,
physical devices or implicit connection to the system usbmuxd are supported.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import signal
import stat
import struct
import time
import uuid

from ios_paravirt_asr import ASRTransfer, VerifiedImage, connect_service, parse_plist
from ios_paravirt_firmware import FirmwareBundle, MANIFEST_LIMIT, read_component, validate_im4m
from ios_paravirt_manifest import check_build, check_device, inspect_manifest
from run_ios_paravirt import guest_evidence

INCOMING_LIMIT = 1024 * 1024
OUTGOING_LIMIT = 128 * 1024 * 1024
EVENT_LIMIT = 8192
JOURNAL_LIMIT = 32 * 1024 * 1024
SYSTEM_TRANSFER_LIMIT = 3


def read_json(path):
    path = Path(path).absolute()
    value = json.loads(read_component(path.parent, path.name, INCOMING_LIMIT))
    if not isinstance(value, dict):
        raise ValueError("Restore session receipt must be a JSON object")
    return value


def mapping(parent, key):
    value = parent.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"Invalid {key} in restore session receipt")
    return value


def inspect_bridge(path, ticket):
    bridge = read_json(path)
    if (not isinstance(bridge, dict) or bridge.get("format") != 1
            or bridge.get("state") != "ready" or bridge.get("local_only") is not True
            or bridge.get("physical_devices_exposed") is not False):
        raise ValueError("Select a ready, private Tomacco bridge.json")
    request = read_json(bridge["request"])
    if (mapping(request, "usb").get("protocol") != "apple-vusb-v1"
            or mapping(request, "usb").get("local_only") is not True
            or request.get("engine_sha256") != bridge.get("engine_sha256")):
        raise ValueError("Bridge does not match its virtual USB engine session")
    disks = mapping(request, "persistent_disks")
    if not all(mapping(disks, name).get("path") for name in ("tomacco-root", "tomacco-aux")):
        raise ValueError("Restore requires explicit persistent virtual root and auxiliary disks")
    if not mapping(request, "persistent_sep_storage").get("path"):
        raise ValueError("Restore requires persistent virtual SEP storage")
    if mapping(request, "boot_manifest_handoff").get("sha256") != hashlib.sha256(ticket).hexdigest():
        raise ValueError("Restore IM4M differs from the manifest supplied to the running guest")
    address = Path(bridge["socket"])
    info, parent = address.lstat(), address.parent.stat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
        raise ValueError("Bridge socket must belong to this user in a private directory")
    return bridge, request, address


@contextmanager
def session_lock(socket_path):
    # The socket directory identifies the live session even if an agent copies
    # bridge.json to another location. All clients contend for this same lock.
    path = Path(socket_path).parent / ".restore.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Invalid private restore lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("A restore already owns this virtual USB session") from error
        yield
    finally:
        os.close(fd)


def encode(value):
    # Receipts retain provenance without copying tickets or firmware into logs.
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, dict):
        return {key: encode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [encode(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


class Journal:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700)
        self.events = (self.directory / "events.jsonl").open("x", encoding="utf-8")
        os.chmod(self.directory / "events.jsonl", 0o600)
        self.bytes = self.count = 0
        self.started = time.monotonic()

    def event(self, kind, value):
        data = json.dumps({"elapsed_seconds": round(time.monotonic() - self.started, 3),
                           "kind": kind, "value": encode(value)}, ensure_ascii=True) + "\n"
        if self.count >= EVENT_LIMIT or self.bytes + len(data) > JOURNAL_LIMIT:
            raise ValueError("Restore event journal limit exceeded")
        self.events.write(data)
        self.events.flush()
        self.bytes += len(data)
        self.count += 1

    def save(self, record):
        temporary = self.directory / "restore.json.tmp"
        with temporary.open("w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(encode(record), stream, indent=2)
            stream.write("\n")
        temporary.replace(self.directory / "restore.json")

    def close(self):
        self.events.close()


def receive_plist(wire):
    size, = struct.unpack("!I", wire.exact(4))
    if not 1 <= size <= INCOMING_LIMIT:
        raise ValueError("Guest restore plist exceeds its size limit")
    value = parse_plist(wire.exact(size))
    if not isinstance(value, dict):
        raise ValueError("Guest restore message must be a dictionary")
    return value


def send_plist(wire, value):
    payload = plistlib.dumps(value, fmt=plistlib.FMT_XML)
    if len(payload) > OUTGOING_LIMIT:
        raise ValueError("Host restore plist exceeds its size limit")
    wire.send(struct.pack("!I", len(payload)) + payload)


class RestoreSession:
    def __init__(self, address, bundle, ticket, image, deadline, journal, record):
        self.address, self.bundle, self.ticket, self.image = address, bundle, ticket, image
        self.deadline, self.journal, self.record = deadline, journal, record
        self.control = None

    def system_image(self, request):
        transfers = self.record.setdefault("system_transfers", [])
        if len(transfers) >= SYSTEM_TRANSFER_LIMIT:
            raise ValueError("Guest system-image retry limit exceeded")
        # restored may explicitly request another ASR transfer after its own
        # verification or inversion failure. Never replay on a transport error.
        attempt = len(transfers) + 1
        transfers.append({"attempt": attempt, "state": "connecting", "payload_sent": False})
        self.record["system_transfer"] = transfers[-1]
        self.journal.save(self.record)
        port = request.get("DataPort", 12345)
        if port == 0 and type(port) is int:
            port = 12345
        # The ASR daemon may listen shortly after sending SystemImageData.
        # Retry connection refusal only, never an initiated transfer.
        connect_deadline = min(self.deadline, time.monotonic() + 60)
        wire = None
        try:
            while True:
                try:
                    wire = connect_service(self.address, port, self.deadline)
                    break
                except ConnectionRefusedError:
                    remaining = connect_deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    time.sleep(min(0.2, remaining))
            def progress(value):
                value["attempt"] = attempt
                transfers[-1] = value
                self.record["system_transfer"] = value
                self.journal.save(self.record)
            self.journal.event("system-transfer-start", {"attempt": attempt, "port": port, "image": self.image.record})
            result = ASRTransfer(wire, self.image, progress).run()
            self.journal.event("system-transfer-end", {**result, "attempt": attempt})
        except BaseException as error:
            transfers[-1].update(state="failed", error=str(error))
            self.journal.save(self.record)
            raise
        finally:
            if wire is not None:
                wire.stream.close()

    def data_request(self, request):
        kind = request.get("DataType")
        arguments = request.get("Arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError("Restore request Arguments must be a dictionary")
        if kind == "SystemImageData":
            self.system_image(request)
            return
        records = []
        if kind == "RootTicket":
            messages = [{"RootTicketData": self.ticket}]
        elif kind == "BuildIdentityDict":
            messages = [self.bundle.build_identity_request(arguments)]
        elif kind == "ReceiptManifest":
            messages = [{"ReceiptManifest": self.bundle.components}]
        elif kind in ("KernelCache", "DeviceTree", "SystemImageRootHash", "SystemImageCanonicalMetadata"):
            name = {"SystemImageRootHash": "SystemVolume",
                    "SystemImageCanonicalMetadata": "Ap,SystemVolumeCanonicalMetadata"}.get(kind, kind)
            image, entry = self.bundle.image(name, self.ticket)
            messages, records = [{kind + "File": image}], [entry]
        elif kind == "NORData":
            version = arguments.get("FlashVersion1", "0")
            if version not in ("0", "1"):
                raise ValueError("Unsupported NOR FlashVersion1")
            value, records = self.bundle.nor(self.ticket, version == "1")
            messages = [value]
        elif kind in ("PersonalizedData", "EANData", "FUDData"):
            value, records = self.bundle.image_request(arguments, self.ticket, kind)
            messages = [value]
        elif kind in ("PersonalizedBootObjectV3", "SourceBootObjectV4", "SourceBootObjectV5"):
            messages, entry = self.bundle.boot_object_request(arguments, self.ticket, kind)
            records = [entry]
        else:
            raise ValueError(f"Unsupported guest restore data request: {str(kind)[:128]}")
        wire = self.control
        if "DataPort" in request:
            wire = connect_service(self.address, request["DataPort"], self.deadline)
        try:
            count = 0
            for message in messages:
                send_plist(wire, message)
                count += 1
            self.journal.event("data-response", {"data_type": kind, "messages": count,
                                                "components": records})
        finally:
            if wire is not self.control:
                wire.stream.close()

    def run(self):
        self.control = connect_service(self.address, 62078, self.deadline)
        try:
            send_plist(self.control, {"Label": "Tomacco", "Request": "QueryType"})
            identity = receive_plist(self.control)
            if (identity.get("Type") != "com.apple.mobile.restored"
                    or identity.get("Result") != "Success"
                    or str(identity.get("HardwareModel", "")).lower() != "vphone600ap"
                    or type(identity.get("RestoreProtocolVersion")) is not int
                    or not 1 <= identity["RestoreProtocolVersion"] <= 32):
                raise ValueError("Guest is not the supported vphone600 restore service")
            self.record["guest_identity"] = identity
            for key in ("HardwareInfo", "SavedDebugInfo"):
                send_plist(self.control, {"Label": "Tomacco", "Request": "QueryValue", "QueryKey": key})
                value = receive_plist(self.control).get(key)
                if not isinstance(value, dict):
                    raise ValueError(f"Guest did not supply {key}")
                self.record[key] = encode(value)
            hardware = self.record["HardwareInfo"]
            if (type(hardware.get("ChipID")) is not int or hardware["ChipID"] != 0xfe01
                    or type(hardware.get("BoardID")) is not int or hardware["BoardID"] != 0x91
                    or hardware.get("SupportsImage4") is not True):
                raise ValueError("Guest hardware does not match the selected vphone600 build identity")
            self.record["manifest_validation"] = check_device(
                check_build(inspect_manifest(self.ticket), self.image.sha384), hardware)
            # These are requested options. The original guest has an allowlist
            # and owns whether they are applied; no host-side override exists.
            options = {"AuthInstallRestoreBehavior": "Erase", "AutoBootDelay": 0,
                       "CreateFilesystemPartitions": True, "SystemImage": True,
                       "FlashNOR": True, "UpdateBaseband": False, "UUID": str(uuid.uuid4()).upper(),
                       # restored also reads these during final cleanup. In
                       # 23E5207q a missing message dictionary reaches
                       # CFDictionaryContainsKey(NULL) in cleanup_send_crash_logs.
                       # Use the upstream baseline/extension flags, and omit
                       # unsupported keys entirely: some guest paths test key
                       # presence rather than the boolean value.
                       "SupportedMessageTypes": {
                           "DataRequestMsg": False, "PreviousRestoreLogMsg": False,
                           "ProgressMsg": False, "StatusMsg": False,
                           "ReceivedFinalStatusMsg": False, "CheckpointMsg": True,
                           "RestoreAttestation": True, "RestoreProtocol": True,
                           "RestoredCrash": True, "MsgType": False,
                           "BBUpdateStatusMsg": False, "ProvisioningInfo": False,
                           "ProvisioningAck": False, "ProvisioningStatusMsg": False},
                       # Protocol 15 permits legacy, request-driven data
                       # transfer without a SupportedDataTypes negotiation.
                       # Its global required-data list includes physical-device
                       # services this vphone client deliberately cannot supply.
                       # Do not advertise those as supported merely to pass it.
                       "SupportedAsyncDataTypes": {}}
            self.record.update(state="restoring", requested_options=options)
            self.journal.save(self.record)
            send_plist(self.control, {"Label": "Tomacco", "Request": "StartRestore",
                                      "RestoreOptions": options,
                                      "RestoreProtocolVersion": identity["RestoreProtocolVersion"]})
            for _ in range(EVENT_LIMIT):
                event = receive_plist(self.control)
                self.journal.event("guest", event)
                kind = event.get("MsgType")
                if kind == "DataRequestMsg":
                    self.data_request(event)
                elif kind == "StatusMsg":
                    status, error = event.get("Status"), event.get("AMRError", 0)
                    self.record["final_status"] = encode(event)
                    if type(status) is not int or type(error) is not int:
                        raise ValueError("Guest final status and AMRError must be integers")
                    # A failed restore still needs its final status ACK to
                    # finish cleanup. Acknowledgement never means success.
                    send_plist(self.control, {"MsgType": "ReceivedFinalStatusMsg"})
                    self.record["final_status_acknowledged"] = True
                    if status != 0 or error != 0 or event.get("Successful") is False:
                        raise RuntimeError(f"Original restore failed: Status={status}, AMRError={error}")
                    self.record["guest_restore_completed"] = True
                    if not self.record.get("system_transfer", {}).get("payload_sent"):
                        raise RuntimeError("Guest ended restore without requesting the system image")
                    self.record.update(state="restore-completed", restore_completed=True)
                    self.journal.save(self.record)
                    return
                elif kind == "ProgressMsg":
                    self.record["guest_progress"] = encode(event)
                elif kind == "CheckpointMsg":
                    self.record["last_checkpoint"] = encode(event)
                elif kind == "RestoredCrash":
                    self.record["restored_crash"] = encode(event)
                    raise RuntimeError("Original restore daemon reported a crash; inspect the guest event journal")
                elif kind == "BBUpdateStatusMsg":
                    self.record["baseband_status"] = encode(event)
                    if event.get("Accepted") is not True:
                        raise RuntimeError("Guest rejected the baseband update")
                elif kind in ("ProvisioningInfo", "ProvisioningAck", "ProvisioningStatusMsg", "RestoreProtocol"):
                    # Baseline notifications, also consumed without a response
                    # by upstream MobileRestore. Preserve their original data;
                    # they neither request signing nor establish provisioning.
                    self.record.setdefault("guest_notifications", {})[kind] = encode(event)
                elif kind == "RestoreAttestation":
                    if event.get("RestoreAttestationState") != "RestoreShouldAttest":
                        raise ValueError("Unsupported restore attestation state")
                    # Upstream restored asks whether the host requires a
                    # separate restore attestation. This offline client does
                    # not request one; this is not an attestation response or
                    # a successful firmware signature verification.
                    response = {"RestoreShouldAttest": False}
                    send_plist(self.control, response)
                    self.record["restore_attestation_requested"] = False
                    self.journal.event("host", response)
                elif kind != "PreviousRestoreLogMsg":
                    raise ValueError(f"Unsupported guest restore message: {str(kind)[:128]}")
                self.journal.save(self.record)
            raise ValueError("Restore guest message limit exceeded")
        finally:
            self.control.stream.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", type=Path, required=True, help="Live private bridge.json")
    parser.add_argument("--firmware", type=Path, required=True, help="Local BuildManifest.plist directory")
    parser.add_argument("--identity", type=int, required=True, help="Explicit vphone600 build identity")
    parser.add_argument("--manifest", type=Path, required=True, help="Existing DER IM4M used for guest boot")
    parser.add_argument("--system-image", type=Path, required=True, help="Local image matching the OS digest")
    parser.add_argument("--userland", type=Path, help="Completed original iPhone import; keeps PCC boot components")
    parser.add_argument("--override", action="append", default=[], metavar="COMPONENT=PATH")
    parser.add_argument("--output", type=Path, required=True, help="New private receipt directory")
    parser.add_argument("--seconds", type=int, default=600, help="Total deadline, including preflight")
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= 3600:
        parser.error("--seconds must be between 1 and 3600")
    overrides = {}
    for item in args.override:
        name, separator, path = item.partition("=")
        if not separator or not name or not path or name in overrides:
            parser.error("Overrides must be unique COMPONENT=PATH entries")
        overrides[name] = path
    deadline = time.monotonic() + args.seconds
    journal = Journal(args.output)
    record = {"format": 1, "state": "preflight", "local_only": True, "external_requests": False,
              "restore_completed": False, "guest_restore_completed": False,
              "qualified_for_app_development": False, "bridge": str(args.bridge.absolute())}
    def terminate(_signum, _frame):
        raise KeyboardInterrupt
    old_handler = signal.signal(signal.SIGTERM, terminate)
    try:
        journal.save(record)
        ticket = read_component(args.manifest.absolute().parent, args.manifest.name, MANIFEST_LIMIT)
        validate_im4m(ticket)
        bridge, request, address = inspect_bridge(args.bridge, ticket)
        with session_lock(address):
            record.update(engine_sha256=bridge["engine_sha256"], request=bridge["request"],
                          virtual_disks=request["persistent_disks"],
                          virtual_sep_storage=request["persistent_sep_storage"],
                          manifest={"sha256": hashlib.sha256(ticket).hexdigest(), "bytes": len(ticket),
                                    "signature_verified": False, "device_binding_verified": False})
            bundle = FirmwareBundle(args.firmware, args.identity, overrides)
            if args.userland is not None:
                from ios_paravirt_userland import attach_userland
                selected_image = attach_userland(bundle, args.userland)
                if args.system_image.absolute() != selected_image:
                    raise ValueError("System image must be the selected userland's images/system.dmg")
            record["firmware"] = bundle.version
            record["manifest_validation"] = inspect_manifest(ticket)
            record["manifest_validation"] = check_build(
                record["manifest_validation"], bundle.components.get("OS", {}).get("Digest"))
            # Check all NOR inputs before asking the original guest to erase.
            _nor, preflight = bundle.nor(ticket)
            del _nor
            record["nor_preflight"] = preflight
            boot_components = []
            for name in ("KernelCache", "DeviceTree", "SystemVolume", "Ap,SystemVolumeCanonicalMetadata"):
                if name in bundle.components:
                    _image, entry = bundle.image(name, ticket)
                    del _image
                    boot_components.append(entry)
            record["boot_components_preflight"] = boot_components
            for kind in ("EANData", "FUDData"):
                _images, entries = bundle.image_request({}, ticket, kind)
                del _images
                record[kind + "_preflight"] = entries
            with VerifiedImage(args.system_image, bundle.components.get("OS", {}).get("Digest"), deadline) as image:
                record["system_image"] = image.record
                RestoreSession(address, bundle, ticket, image, deadline, journal, record).run()
    except KeyboardInterrupt:
        record.update(state="cancelled", restore_completed=False, error="Restore interrupted")
    except (OSError, EOFError, ValueError, RuntimeError, KeyError, TypeError, RecursionError) as error:
        record.update(state="failed", restore_completed=False, error=str(error))
    finally:
        signal.signal(signal.SIGTERM, old_handler)
        if record.get("request"):
            try:
                evidence = guest_evidence(Path(record["request"]).parent)
                record["guest_evidence"] = evidence
                record["guest_evidence_scope"] = "vm-session-since-boot"
                if evidence["restore_failures"]:
                    record["first_guest_failure"] = evidence["restore_failures"][0]
            except OSError as error:
                record["guest_evidence_error"] = str(error)
        record["elapsed_seconds"] = round(time.monotonic() - journal.started, 3)
        journal.save(record)
        journal.close()
    print(json.dumps({key: record[key] for key in ("state", "restore_completed", "elapsed_seconds")}
                     | {"receipt": str(args.output.absolute() / "restore.json")}
                     | ({"error": record["error"]} if "error" in record else {})))
    return 0 if record["restore_completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
