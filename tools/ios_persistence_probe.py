#!/usr/bin/env python3
"""Run the virtual iPhone's persistence probe through tomaccoctl and print a table.

The guest's `persistence_probe` worker owns one fixed Class C challenge file
(`create:<64 hex>` writes it once, `read:<same>` compares it) and, from guest
tools 370, surveys one file per protection class A to D with the same
challenge (`classes:<64 hex>`: a missing file is created with its class, an
existing one is read back, so a survey in a later boot reads what an earlier
boot wrote). This tool sends one such request to the running guest and prints
one row per protection class the reply covers: the class, what was done, the
file's inode and the class the kernel reports for it, the bytes read back and
whether they match the challenge, or the step that failed with its errno.

`inode --inode N` (guest tools from 370) resolves one inode on the volume
that holds the mobile Library to its path and reads the file's protection
class without opening it, for a boot log's "retain_crypto_states failed on
ino N" line. `keybag` reads a run's uart.log without touching the guest and
lists the keybag and AppleKeyStore lines of the boot, decoding the IOKit codes
they carry (system, subsystem, code; `sub_iokit_keystore` is 31).

Each reply describes one file operation in one boot. A clean-reboot result
needs two boots with the same challenge and matched media, bound by whoever
runs them; this tool reports one boot and says so.
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time

# Darwin bsd/sys/content_protection.h. E, F and CX are the system's own.
CLASSES = {1: "A", 2: "B", 3: "C", 4: "D", 5: "E", 6: "F", 7: "CX"}
FOUNDATION_NAMES = {1: "NSFileProtectionComplete", 2: "NSFileProtectionCompleteUnlessOpen",
                    3: "NSFileProtectionCompleteUntilFirstUserAuthentication", 4: "NSFileProtectionNone"}
SURVEY_GUEST_TOOLS = 370
# Darwin bsd/sys/errno.h; Linux numbers differ from 35 on, so Python's errno
# module cannot name a guest errno.
DARWIN_ERRNO = {1: "EPERM", 2: "ENOENT", 4: "EINTR", 5: "EIO", 9: "EBADF", 11: "EDEADLK", 13: "EACCES",
                16: "EBUSY", 17: "EEXIST", 20: "ENOTDIR", 21: "EISDIR", 22: "EINVAL", 24: "EMFILE",
                28: "ENOSPC", 30: "EROFS", 35: "EAGAIN", 45: "ENOTSUP", 60: "ETIMEDOUT", 62: "ELOOP",
                63: "ENAMETOOLONG", 66: "ENOTEMPTY", 78: "ENOSYS", 80: "EAUTH", 81: "ENEEDAUTH",
                82: "EPWROFF", 83: "EDEVERR", 93: "ENOATTR", 100: "EPROTO"}
# iokit/IOKit/IOReturn.h: sys_iokit is err_system(0x38); the common
# subsystem's codes are named there, a family's codes are the family's own.
IOKIT_SYSTEM = 0x38
IOKIT_SUBSYSTEMS = {0: "common", 1: "usb", 2: "firewire", 4: "block_storage", 5: "graphics", 6: "networking",
                    8: "bluetooth", 9: "pmu", 10: "acpi", 11: "smbus", 12: "ahci", 13: "powermanagement",
                    14: "hidsystem", 16: "scsi", 17: "usbaudio", 18: "wirelesscharging", 28: "nvme",
                    29: "thunderbolt", 30: "graphics_acceleration", 31: "keystore", 32: "smc", 33: "apfs",
                    34: "acpiec", 35: "timesync_avb"}
IORETURN_COMMON = {
    0x1: "kIOReturnInvalid", 0x2bc: "kIOReturnError", 0x2bd: "kIOReturnNoMemory", 0x2be: "kIOReturnNoResources",
    0x2bf: "kIOReturnIPCError", 0x2c0: "kIOReturnNoDevice", 0x2c1: "kIOReturnNotPrivileged",
    0x2c2: "kIOReturnBadArgument", 0x2c3: "kIOReturnLockedRead", 0x2c4: "kIOReturnLockedWrite",
    0x2c5: "kIOReturnExclusiveAccess", 0x2c6: "kIOReturnBadMessageID", 0x2c7: "kIOReturnUnsupported",
    0x2c8: "kIOReturnVMError", 0x2c9: "kIOReturnInternalError", 0x2ca: "kIOReturnIOError",
    0x2cc: "kIOReturnCannotLock", 0x2cd: "kIOReturnNotOpen", 0x2ce: "kIOReturnNotReadable",
    0x2cf: "kIOReturnNotWritable", 0x2d0: "kIOReturnNotAligned", 0x2d1: "kIOReturnBadMedia",
    0x2d2: "kIOReturnStillOpen", 0x2d3: "kIOReturnRLDError", 0x2d4: "kIOReturnDMAError", 0x2d5: "kIOReturnBusy",
    0x2d6: "kIOReturnTimeout", 0x2d7: "kIOReturnOffline", 0x2d8: "kIOReturnNotReady",
    0x2d9: "kIOReturnNotAttached", 0x2da: "kIOReturnNoChannels", 0x2db: "kIOReturnNoSpace",
    0x2dd: "kIOReturnPortExists", 0x2de: "kIOReturnCannotWire", 0x2df: "kIOReturnNoInterrupt",
    0x2e0: "kIOReturnNoFrames", 0x2e1: "kIOReturnMessageTooLarge", 0x2e2: "kIOReturnNotPermitted",
    0x2e3: "kIOReturnNoPower", 0x2e4: "kIOReturnNoMedia", 0x2e5: "kIOReturnUnformattedMedia",
    0x2e6: "kIOReturnUnsupportedMode", 0x2e7: "kIOReturnUnderrun", 0x2e8: "kIOReturnOverrun",
    0x2e9: "kIOReturnDeviceError", 0x2ea: "kIOReturnNoCompletion", 0x2eb: "kIOReturnAborted",
    0x2ec: "kIOReturnNoBandwidth", 0x2ed: "kIOReturnNotResponding", 0x2ee: "kIOReturnIsoTooOld",
    0x2ef: "kIOReturnIsoTooNew", 0x2f0: "kIOReturnNotFound"}
COLUMNS = ("class", "action", "inode", "kernel class", "read", "match", "outcome")
# bsd/sys/vnode.h enum vtype.
OBJECT_TYPES = {0: "none", 1: "regular file", 2: "directory", 3: "block device", 4: "character device",
                5: "symbolic link", 6: "socket", 7: "fifo", 8: "bad", 9: "stream", 10: "complex"}


def errno_text(value):
    if value is None:
        return ""
    name = DARWIN_ERRNO.get(value)
    return f"errno {value} ({name})" if name else f"errno {value}"


def decode_ioreturn(value):
    """What an IOKit/AppleKeyStore return value says: its system, subsystem
    and code (mach/error.h: 6, 12 and 14 bits), with the common subsystem's
    name from IOReturn.h. A keystore-family code has no public name."""
    code = int(value, 16) if isinstance(value, str) else int(value)
    if code == 0:
        return {"value": "0", "system": "success", "subsystem": None, "code": 0, "name": "kIOReturnSuccess"}
    system, subsystem, number = (code >> 26) & 0x3f, (code >> 14) & 0xfff, code & 0x3fff
    if system != IOKIT_SYSTEM:
        return {"value": f"{code:x}", "system": f"err_system({system})", "subsystem": None, "code": number,
                "name": None}
    family = IOKIT_SUBSYSTEMS.get(subsystem)
    name = IORETURN_COMMON.get(number) if subsystem == 0 else None
    if name is None and family:
        name = f"{family} family code {number:#x}"
    return {"value": f"{code:x}", "system": "iokit", "subsystem": subsystem, "family": family,
            "code": number, "name": name}


def class_name(number):
    letter = CLASSES.get(number)
    return f"{letter} ({number})" if letter else f"? ({number})"


def rows(response):
    """One table row per protection class the native reply covers."""
    if not isinstance(response, dict):
        return []
    outcome = lambda error, errno: ("ok" if not error else " ".join(filter(None, [error, errno_text(errno)])))
    if response.get("probe_action") in ("create", "read"):
        return [{"class": class_name(3), "action": response.get("probe_action"),
                 "inode": response.get("file_inode"), "kernel class": response.get("protection_class"),
                 "read": response.get("read_bytes"), "match": yes_no(response.get("contents_match")),
                 "outcome": outcome(response.get("error"), response.get("errno"))}]
    result = []
    if response.get("probe_action") == "classes":
        for number in range(1, 8):
            prefix = f"class_{number}_"
            fields = {key[len(prefix):]: value for key, value in response.items() if key.startswith(prefix)}
            if not fields:
                continue
            note = " (file removed)" if fields.get("removed") is True else (
                " (removal failed)" if fields.get("removed") is False else "")
            result.append({"class": class_name(number), "action": fields.get("action"),
                           "inode": fields.get("inode"), "kernel class": fields.get("protection_class"),
                           "read": fields.get("read_bytes"), "match": yes_no(fields.get("contents_match")),
                           "outcome": outcome(fields.get("error"), fields.get("errno")) + note})
        if response.get("error"):
            result.append({"class": "survey", "action": "classes", "inode": None, "kernel class": None,
                           "read": None, "match": "", "outcome": outcome(response["error"], response.get("errno"))})
    return result


def yes_no(value):
    return "yes" if value is True else "no" if value is False else ""


def format_table(entries, columns=COLUMNS):
    cells = [[("" if row.get(column) is None else str(row.get(column))) for column in columns] for row in entries]
    widths = [max([len(column)] + [len(line[index]) for line in cells]) for index, column in enumerate(columns)]
    lines = ["  ".join(column.ljust(widths[index]) for index, column in enumerate(columns)),
             "  ".join("-" * width for width in widths)]
    lines += ["  ".join(cell.ljust(widths[index]) for index, cell in enumerate(line)).rstrip() for line in cells]
    return "\n".join(lines)


def default_cli():
    installed = Path.home() / ".local/lib/tomacco/tomaccoctl"
    return shutil.which("tomaccoctl") or (str(installed) if installed.exists() else "tomaccoctl")


class Probe:
    def __init__(self, cli, pause=5.0, patience=1.0):
        self.cli, self.pause, self.patience = cli, pause, patience

    def invoke(self, operation, arguments, timeout=180):
        done = subprocess.run([self.cli, "call", operation, json.dumps(arguments)],
                              capture_output=True, text=True, timeout=timeout)
        if not done.stdout.strip():
            raise RuntimeError(f"{operation} produced no reply: {done.stderr.strip()[:300]}")
        return json.loads(done.stdout)

    def guest_job(self):
        reply = self.invoke("ios.virtual.status", {})
        if not reply.get("ok"):
            raise RuntimeError(f"ios.virtual.status: {reply.get('error', {}).get('message', 'failed')}")
        status = reply["result"]
        if not status.get("guest_job") or status.get("guest_running") is False:
            raise RuntimeError("the virtual iPhone has no running guest (ios.virtual.status reports no guest_job)")
        return status["guest_job"]

    def probe(self, job, action, challenge, timeout=120):
        """The raw tomaccoctl reply for one persistence_probe request. A guest
        that is still ending an earlier worker refuses before any work, so
        that refusal is asked again; nothing else is ever resent."""
        arguments = {"job_id": job, "operation": "persistence_probe", "argument": f"{action}:{challenge}",
                     "timeout_seconds": timeout}
        deadline = time.monotonic() + 120 * self.patience
        while True:
            reply = self.invoke("ios.emulator.guest", arguments, timeout + 60)
            response = ((reply.get("result") or {}).get("response") or {}) if reply.get("ok") else {}
            if response.get("error") != "guest_operation_busy" or time.monotonic() >= deadline:
                return reply
            time.sleep(self.pause)


def summarize(reply, action):
    """(exit code, text) for one reply: 0 when the operation succeeded and
    every class read back, 1 for a native failure or mismatch, 2 when the
    request did not reach a reply."""
    if not reply.get("ok"):
        error = reply.get("error") or {}
        code, message = error.get("code"), error.get("message", "")
        if action in ("classes", "inode") and code in ("invalid_guest_request", "invalid_guest_response"):
            what = "the class survey" if action == "classes" else "the inode lookup"
            return 2, (f"{code}: {what} needs guest tools from {SURVEY_GUEST_TOOLS} (the first build after 369 that "
                       f"carries guest-persistence-probe.h with it) and a tomaccoctl that accepts {action}:<argument>; "
                       f"this guest or CLI does not carry it ({message})")
        details = error.get("details") or {}
        extra = f"; request {details['request_id']} may have executed" if details.get("request_may_have_executed") else ""
        return 2, f"{code}: {message}{extra}"
    result = reply.get("result") or {}
    response = result.get("response") or {}
    if response.get("probe_action") == "inode":
        return summarize_lookup(result, response)
    entries = rows(response)
    lines = [format_table(entries)] if entries else ["(no class rows in the reply)"]
    if response.get("probe_action") == "classes":
        lines.append(f"{response.get('classes_readable', 0)}/{response.get('classes_surveyed', 0)} classes read back"
                     f" (A {FOUNDATION_NAMES[1]}, B {FOUNDATION_NAMES[2]}, C {FOUNDATION_NAMES[3]}, D {FOUNDATION_NAMES[4]})")
    lines.append(f"request {result.get('request_id')}, guest pid {response.get('pid')}, uid {response.get('uid')},"
                 f" challenge {response.get('challenge')}")
    lines.append("one boot only: a clean-reboot result needs a second boot reading the same challenge")
    matched = bool(entries) and all(row["match"] == "yes" for row in entries if row["class"] != "survey")
    return (0 if response.get("ok") is True and matched else 1), "\n".join(lines)


def summarize_lookup(result, response):
    kind = response.get("object_type")
    lines = [f"inode {response.get('inode')} on volume fsid {response.get('volume_fsid')}:"]
    if response.get("path_known"):
        lines.append(f"  path {response.get('path')}")
    if kind is not None:
        lines.append(f"  object type {kind} ({OBJECT_TYPES.get(kind, 'unknown')})")
    if response.get("protection_class") is not None:
        number = response["protection_class"]
        lines.append(f"  protection class {class_name(number)}"
                     + (f" {FOUNDATION_NAMES[number]}" if number in FOUNDATION_NAMES else
                        " (the system's own: E keybag-only, F ephemeral key, CX)" if number in (5, 6, 7) else ""))
    if response.get("error"):
        lines.append("  " + " ".join(filter(None, [response["error"], errno_text(response.get("errno"))])))
    lines.append(f"request {result.get('request_id')}, guest pid {response.get('pid')}; the file was not opened")
    return (0 if response.get("ok") is True else 1), "\n".join(lines)


KEYBAG_PATTERNS = {
    "fake_effaceable_key": re.compile(r"disabling use of effaceable storage, using fake key, (0x[0-9a-f]+)"),
    "system_keybag_missing": re.compile(r"MKB_INIT: Could not open (/private/var/keybags/systembag\.kb)\b"),
    "system_keybag_loaded": re.compile(r"MKB_INIT: (No system keybag loaded|System keybag loaded)"),
    "identity_loaded": re.compile(r"Loaded identity ([0-9A-F-]+) as session (-?\d+)"),
    "first_unlock": re.compile(r"Sending First Unlock notification for handle (-?\d+)"),
    "lock_state": re.compile(r"Sending lock state change (\w+) \((\d+)\) for handle (-?\d+)"),
    "volume_bag": re.compile(r"mapped uuid ([0-9A-F-]+) to bag (-?\d+), persona (\w+)"),
    "media_key_failure": re.compile(r"failed to get wrapped media key \(uuid = ([0-9A-F-]+), cur_tag = (\d+)\), error = (\d+)"),
    "unwrap_failure": re.compile(r"AKS unwrap_key failed, error = ([0-9a-f]+)"),
    "keystore_failure": re.compile(r'"AppleSEPKeyStore":pid:(\d+),.*operation failed \(sel: (\d+) ret: ([0-9a-f]+)\)'),
}
RETAIN_PATTERN = re.compile(r"(disk\d+s\d+) retain_crypto_states failed on ino (\d+) with (\d+)")


def read_log(path):
    """A log's text with its line endings as written, so that line numbers
    count newlines only, as grep -n does (a serial log carries lone CRs)."""
    with open(path, encoding="utf-8", errors="replace", newline="") as log:
        return log.read()


def keybag_summary(text):
    """The keybag and AppleKeyStore facts of one boot, from its uart.log."""
    lines = text.split("\n")
    summary = {"fake_effaceable_key": None, "system_keybag_file": None, "system_keybag_loaded": None,
               "identities": [], "first_unlock": [], "lock_states": [], "volume_bags": [],
               "media_key_failures": [], "unwrap_failures": [], "keystore_failures": []}
    for number, line in enumerate(lines, 1):
        found = {name: pattern.search(line) for name, pattern in KEYBAG_PATTERNS.items()}
        if found["fake_effaceable_key"]:
            summary["fake_effaceable_key"] = {"line": number, "key": found["fake_effaceable_key"].group(1)}
        if found["system_keybag_missing"] and summary["system_keybag_file"] is None:
            summary["system_keybag_file"] = {"line": number, "path": found["system_keybag_missing"].group(1),
                                             "present": False}
        if found["system_keybag_loaded"]:
            summary["system_keybag_loaded"] = {"line": number, "text": found["system_keybag_loaded"].group(1)}
        if found["identity_loaded"]:
            summary["identities"].append({"line": number, "identity": found["identity_loaded"].group(1),
                                          "session": int(found["identity_loaded"].group(2))})
        if found["first_unlock"]:
            summary["first_unlock"].append({"line": number, "handle": int(found["first_unlock"].group(1))})
        if found["lock_state"]:
            summary["lock_states"].append({"line": number, "state": found["lock_state"].group(1),
                                           "value": int(found["lock_state"].group(2)),
                                           "handle": int(found["lock_state"].group(3))})
        if found["volume_bag"]:
            summary["volume_bags"].append({"line": number, "volume": found["volume_bag"].group(1),
                                           "bag": int(found["volume_bag"].group(2)),
                                           "persona": found["volume_bag"].group(3)})
        if found["media_key_failure"]:
            summary["media_key_failures"].append({"line": number, "volume": found["media_key_failure"].group(1),
                                                  "tag": int(found["media_key_failure"].group(2)),
                                                  "errno": int(found["media_key_failure"].group(3))})
        if found["unwrap_failure"]:
            entry = {"line": number, "error": decode_ioreturn(found["unwrap_failure"].group(1)),
                     "volume": None, "inode": None, "errno": None}
            for later in lines[number:number + 4]:
                retained = RETAIN_PATTERN.search(later)
                if retained:
                    entry.update(volume=retained.group(1), inode=int(retained.group(2)), errno=int(retained.group(3)))
                    break
            summary["unwrap_failures"].append(entry)
        if found["keystore_failure"]:
            summary["keystore_failures"].append({"line": number, "pid": int(found["keystore_failure"].group(1)),
                                                 "selector": int(found["keystore_failure"].group(2)),
                                                 "error": decode_ioreturn(found["keystore_failure"].group(3))})
    return summary


def format_keybag(summary):
    lines = []
    fake = summary["fake_effaceable_key"]
    lines.append("effaceable storage: " + (f"disabled, fake key {fake['key']} (line {fake['line']})" if fake
                                           else "no fake-key line"))
    keybag = summary["system_keybag_file"]
    lines.append("system keybag file: " + (f"{keybag['path']} absent (line {keybag['line']})" if keybag
                                           else "no MKB_INIT open failure"))
    loaded = summary["system_keybag_loaded"]
    lines.append("MKB_INIT: " + (f"{loaded['text']} (line {loaded['line']})" if loaded else "no keybag-loaded line"))
    for identity in summary["identities"]:
        lines.append(f"identity {identity['identity']} loaded as session {identity['session']} (line {identity['line']})")
    for unlock in summary["first_unlock"]:
        lines.append(f"first unlock notified for handle {unlock['handle']} (line {unlock['line']})")
    for state in summary["lock_states"]:
        lines.append(f"lock state {state['state']} ({state['value']}) for handle {state['handle']} (line {state['line']})")
    for volume in summary["volume_bags"]:
        lines.append(f"volume {volume['volume']} -> bag {volume['bag']}, persona {volume['persona']} (line {volume['line']})")
    for failure in summary["media_key_failures"]:
        lines.append(f"media key tag {failure['tag']} of volume {failure['volume']} not found: "
                     f"{errno_text(failure['errno'])} (line {failure['line']})")
    for failure in summary["unwrap_failures"]:
        error = failure["error"]
        where = (f"{failure['volume']} inode {failure['inode']}, opener gets {errno_text(failure['errno'])}"
                 if failure["volume"] else "no retain_crypto_states line follows")
        lines.append(f"AKS unwrap_key failed: {error['value']} = {error['name']} (system {error['system']}, "
                     f"subsystem {error.get('subsystem')}, code {error['code']:#x}); {where} (line {failure['line']})")
    for failure in summary["keystore_failures"]:
        error = failure["error"]
        lines.append(f"AppleSEPKeyStore operation failed: pid {failure['pid']} selector {failure['selector']} "
                     f"-> {error['value']} = {error['name']} (line {failure['line']})")
    if not summary["unwrap_failures"] and not summary["keystore_failures"]:
        lines.append("no AKS unwrap or AppleSEPKeyStore operation failure")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("create", "read", "classes", "inode", "keybag"),
                        help="create: write the Class C challenge once; read: compare it; classes: survey "
                             "classes A..D; inode: path and class of one inode; keybag: summarize a "
                             "uart.log without touching the guest")
    parser.add_argument("--cli", default=default_cli(), help="tomaccoctl executable")
    parser.add_argument("--job", help="the guest job (default: the running virtual iPhone's)")
    parser.add_argument("--challenge", help="64 lowercase hex digits; required for read, generated otherwise")
    parser.add_argument("--inode", help="the inode to look up (1 to 19 decimal digits), for inode")
    parser.add_argument("--timeout", type=int, default=120, help="guest seconds for the request")
    parser.add_argument("--pause", type=float, default=5.0)
    parser.add_argument("--patience", type=float, default=1.0)
    parser.add_argument("--save", help="write the raw tomaccoctl reply (or keybag summary) to this file")
    parser.add_argument("--json", action="store_true", help="print the raw reply instead of the table")
    parser.add_argument("--uart-log", help="also summarize this run's uart.log")
    args = parser.parse_args(argv)
    if args.action == "keybag":
        if not args.uart_log:
            parser.error("keybag needs --uart-log")
        summary = keybag_summary(read_log(args.uart_log))
        if args.save:
            Path(args.save).write_text(json.dumps(summary, indent=1) + "\n")
        print(json.dumps(summary, indent=1) if args.json else format_keybag(summary))
        return 0
    challenge = args.challenge
    if args.action == "read" and not challenge:
        parser.error("read needs --challenge, the 64 hex digits the file was created with")
    if challenge and not re.fullmatch(r"[0-9a-f]{64}", challenge):
        parser.error("--challenge must be 64 lowercase hex digits")
    if args.action == "inode":
        if not args.inode or not re.fullmatch(r"[0-9]{1,19}", args.inode):
            parser.error("inode needs --inode, 1 to 19 decimal digits")
        challenge = args.inode
    challenge = challenge or secrets.token_hex(32)
    probe = Probe(args.cli, pause=args.pause, patience=args.patience)
    try:
        job = args.job or probe.guest_job()
        reply = probe.probe(job, args.action, challenge, args.timeout)
    except (RuntimeError, subprocess.SubprocessError, ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.save:
        Path(args.save).write_text(json.dumps(reply, indent=1) + "\n")
    code, text = summarize(reply, args.action)
    print(json.dumps(reply, indent=1) if args.json else text)
    if args.uart_log:
        print(format_keybag(keybag_summary(read_log(args.uart_log))))
    return code


if __name__ == "__main__":
    sys.exit(main())
