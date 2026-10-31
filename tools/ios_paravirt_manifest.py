"""Inspect declared Image4 restore metadata without authenticating its signature.

The guest takes the ASR system-image digest from IM4M's MANB/rosi/DGST, not
from the host's BuildManifest. Check both before starting a destructive restore.
Inspection leaves the supplied manifest unchanged. Matching fields do not
establish valid signatures or personalization.
"""
from ios_paravirt_firmware import der_fields, der_item, validate_im4m


def object_set(data):
    """Read bounded Image4 private-tagged key/value objects in a DER SET."""
    result, offset = {}, 0
    while offset < len(data):
        if len(result) >= 512 or data[offset] != 0xff:
            raise ValueError("Invalid Image4 manifest object set")
        offset += 1
        number = 0
        for index in range(5):
            if offset >= len(data):
                raise ValueError("Truncated Image4 private tag")
            byte = data[offset]
            offset += 1
            if index == 0 and byte == 0x80:
                raise ValueError("Noncanonical Image4 private tag")
            number = (number << 7) | (byte & 0x7f)
            if not byte & 0x80:
                break
        else:
            raise ValueError("Image4 private tag exceeds four characters")
        if not 31 <= number <= 0xffffffff:
            raise ValueError("Invalid Image4 private tag number")
        # Reuse the canonical DER length parser with a temporary ordinary tag.
        envelope = b"\x30" + data[offset:]
        _, start, end = der_item(envelope)
        value = envelope[start:end]
        offset += end - 1
        fields = der_fields(value)
        if (len(fields) != 2 or fields[0][0] != 0x16
                or fields[0][2] - fields[0][1] != 4):
            raise ValueError("Image4 manifest objects require a four-character key")
        key = value[fields[0][1]:fields[0][2]]
        if any(c < 32 or c > 126 for c in key) or int.from_bytes(key, "big") != number:
            raise ValueError("Image4 private tag does not match its key")
        name = key.decode("ascii")
        if name in result:
            raise ValueError(f"Duplicate Image4 manifest object: {name}")
        tag, start, end = fields[1]
        result[name] = (tag, value[start:end])
    return result


def required(objects, name, tag):
    value = objects.get(name)
    if value is None or value[0] != tag:
        raise ValueError(f"Missing or invalid Image4 manifest {name}")
    return value[1]


def integer(data):
    if (not 1 <= len(data) <= 9 or data[0] & 0x80
            or len(data) > 1 and data[0] == 0 and not data[1] & 0x80):
        raise ValueError("Invalid Image4 manifest unsigned integer")
    value = int.from_bytes(data, "big")
    if value > 0xffffffffffffffff:
        raise ValueError("Image4 manifest integer exceeds 64 bits")
    return value


def inspect_manifest(ticket):
    validate_im4m(ticket)
    _, start, end = der_fields(ticket)[2]
    outer = object_set(ticket[start:end])
    body = object_set(required(outer, "MANB", 0x31))
    properties = object_set(required(body, "MANP", 0x31))
    os_entry = object_set(required(body, "rosi", 0x31))
    digest = required(os_entry, "DGST", 4)
    if len(digest) != 48:
        raise ValueError("Restore IM4M must declare a SHA-384 OS digest")
    values = {key: integer(required(properties, key, 2)) for key in ("CHIP", "BORD")}
    if "ECID" in properties:
        values["ECID"] = integer(required(properties, "ECID", 2))
    return {"declared_hardware": values, "os_sha384": digest.hex(),
            "signature_verified": False, "device_binding_verified": False}


def check_build(info, os_digest, chip=0xfe01, board=0x91):
    if not isinstance(os_digest, bytes) or len(os_digest) != 48:
        raise ValueError("BuildManifest OS digest must be SHA-384")
    if info["os_sha384"] != os_digest.hex():
        raise ValueError("Restore IM4M OS digest differs from the selected BuildManifest; select a matching local manifest")
    declared = info["declared_hardware"]
    if declared["CHIP"] != chip or declared["BORD"] != board:
        raise ValueError("Restore IM4M hardware differs from the vphone600 build identity")
    return {**info, "build_metadata_matched": True}


def check_device(info, hardware):
    declared = info["declared_hardware"]
    for key, guest_key in (("CHIP", "ChipID"), ("BORD", "BoardID"), ("ECID", "UniqueChipID")):
        if key in declared and (type(hardware.get(guest_key)) is not int
                                or declared[key] != hardware[guest_key]):
            raise ValueError(f"Restore IM4M {key} differs from the connected virtual guest")
    # This checks declarations only. The original guest owns authentication.
    return {**info, "guest_metadata_matched": True}
