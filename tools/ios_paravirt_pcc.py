"""Supply a local PCC hostname through the documented darwin-init NVRAM input.

Only the in-memory boot proxy is edited. Existing configurations are never
replaced, and no signing, security, SSH, network or cryptex policy is set.
Format references: apple/security-pcc ConfigLoader and XNU IONVRAMCHRPHandler.
"""
import hashlib
import json
import re
import struct
import zlib

from prepare_ios_paravirt import validate_nvram


def hostname_config(hostname):
    if not isinstance(hostname, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", hostname):
        raise ValueError("PCC hostname must be a lowercase DNS label of 1–63 characters")
    return {"local-host-name": hostname}


def pcc_config_handoff(tree, hostname):
    """Append one ASCII JSON value while preserving every other NVRAM byte.

    CHRP Data values escape only NUL/0xff bytes, neither of which can occur in
    this generated JSON. The guest remains responsible for applying the config
    and persisting its NVRAM state. A receipt proves delivery, not acceptance.
    """
    config = hostname_config(hostname)
    value = json.dumps(config, separators=(",", ":")).encode("ascii")
    chosen = tree.child("chosen")
    original = chosen.get("nvram-proxy-data")
    validate_nvram(original)
    common_name = b"2common" if original[4:16].startswith(b"2nvram\0") else b"common"
    system_name = b"2system" if common_name == b"2common" else b"system"
    offset, common, seen, existing = 0, None, set(), None
    while offset < len(original):
        _, _, blocks, raw_name = struct.unpack_from("<BBH12s", original, offset)
        size, name = blocks * 16, raw_name.rstrip(b"\0")
        if name in (b"common", b"2common", b"system", b"2system"):
            if name not in (common_name, system_name) or name in seen:
                raise ValueError("PCC configuration requires unambiguous CHRP partitions")
            seen.add(name)
            start, end = offset + 16, offset + size
            cursor, keys = start, set()
            while cursor < end and original[cursor]:
                terminator = original.find(b"\0", cursor, end)
                if terminator < 0:
                    raise ValueError("Unterminated NVRAM variable")
                entry = original[cursor:terminator]
                key, separator, data = entry.partition(b"=")
                if not separator or not key or key in keys:
                    raise ValueError("Malformed or duplicate NVRAM variable")
                keys.add(key)
                if key in (b"darwin-init", b"darwin-init-system"):
                    if name != common_name or key != b"darwin-init" or data != value:
                        raise ValueError("Existing darwin-init configuration must not be replaced")
                    existing = data
                cursor = terminator + 1
            if name == common_name:
                common = (cursor, end)
        offset += size
    if common is None:
        raise ValueError("Missing matching CHRP common partition")
    data = bytearray(original)
    if existing is None:
        start, end = common
        entry = b"darwin-init=" + value + b"\0"
        # Retain a final empty-name terminator; never discard unknown padding.
        if len(entry) >= end - start or any(original[start:end]):
            raise ValueError("No empty NVRAM space for PCC configuration")
        data[start:start + len(entry)] = entry
        struct.pack_into("<I", data, 16, zlib.adler32(data[20:]))
    validate_nvram(data)
    chosen.set("nvram-proxy-data", bytes(data))
    return {"variable": "darwin-init", "configuration": config,
            "delivery": "nvram-boot-proxy", "changed": existing is None,
            "bank_sha256_before": hashlib.sha256(original).hexdigest(),
            "bank_sha256_after": hashlib.sha256(data).hexdigest(),
            "configuration_sha256": hashlib.sha256(value).hexdigest(),
            "host_wrote_storage": False, "security_controls_modified": False}


def pcc_guest_evidence(serial):
    """Parse original darwin-init messages; delivery alone is not application."""
    log = serial.replace("\r", "")
    prefix = r"darwin-init\[\d+:[0-9a-f]+\]\[com\.apple\.darwin-init:\] "
    loaded = list(dict.fromkeys(re.findall(
        prefix + r"Successfully loaded configuration from (darwin-init(?:-system)?) NVRAM variable", log)))
    applied = []
    decoder = json.JSONDecoder()
    for match in re.finditer(prefix + "Applied configuration: ", log):
        try:
            config, _ = decoder.raw_decode(log[match.end():match.end() + 65536].lstrip())
        except (ValueError, RecursionError):
            continue
        if not isinstance(config, dict) or not isinstance(config.get("identification"), dict):
            continue
        name = config["identification"].get("local_hostname")
        if isinstance(name, str) and name not in applied and len(applied) < 16:
            applied.append(name[:255])
    failures = list(dict.fromkeys(re.findall(prefix + r"darwin-init apply failed: ([^\n]{1,1024})", log)))[:16]
    return {"darwin_init_config_sources": loaded,
            "darwin_init_applied_local_hostnames": applied,
            "darwin_init_apply_failures": failures}
