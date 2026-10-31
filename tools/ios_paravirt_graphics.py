"""Prepare a private, hash-pinned PCC graphics bundle for the research guest.

Original driver instructions and data are preserved. The compiler bridge adds a
load dependency and a local ad-hoc signature to a separate driver copy. Local
platform trust grants are neither Apple authentication nor proof of working
shaders. Only the copy of the guest's unsealed Preboot is written.
"""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import time

from build_ios_gpu_plugin import (BRIDGE_NAME, NAME, SOURCE_SHA256, inspect_bridge, inspect_plugin)
from ios_paravirt_firmware import der_fields, read_component, retag_im4p
from prepare_ios_userland import DRIVER, manifest
from ios_graphics_location import BUNDLE_PATH, GRAPHICS_RELATIVE_PATH, PLUGIN_PERSONALITY

DRIVER_FILES = {
    DRIVER: "29ba36c7bc87d82c40fa1cecdf357e7a383fa70eb46df0d75869f5ac562aedbc",
    "Info.plist": "285a9fd4c67852450bf13631f1319b3b25c920d6a90f4b867b9b6edbcd8a73aa",
    "_CodeSignature/CodeResources": "3c727b0f043e463c0227db49cef54c5567715986d08a46c98923c9bb73585e5a",
}
# Keep the driver under the retained Cryptexes tree. An unrelated top-level
# directory of our own under Preboot was removed during an observed guest boot, leaving
# later cold boots without a Metal device. The compact name also fits the
# original fixed-size prelink XML slot without moving any kernel byte.

DESTINATION_SHELL = """prepare_graphics_destination() {
  if [ -e "$1/Cryptexes" ] || [ -L "$1/Cryptexes" ]; then
    test -d "$1/Cryptexes" && test ! -L "$1/Cryptexes" || return 1
  fi
  if [ -e "$1/Cryptexes/ps.bundle" ] || [ -L "$1/Cryptexes/ps.bundle" ]; then
    echo "Graphics destination already exists: $1/Cryptexes/ps.bundle" >&2
    return 1
  fi
}
"""


def cache_entries(data):
    if len(data) < 24:
        raise ValueError("Truncated graphics trust cache")
    version, _uuid, count = struct.unpack_from("<I16sI", data)
    if version != 2 or count > 100000 or len(data) != 24 + count * 24:
        raise ValueError("Expected a complete version-2 graphics trust cache")
    entries = {}
    for offset in range(24, len(data), 24):
        item = data[offset:offset + 24]
        if item[:20] in entries:
            raise ValueError("Duplicate graphics trust-cache hash")
        entries[item[:20]] = item
    return entries


def extend_cache(original, pcc, driver_hash, compiler_hash, *, adapted_driver=None, bridge=None):
    entries, source = cache_entries(original), cache_entries(pcc)
    if len(driver_hash) != 20 or len(compiler_hash) != 20 or driver_hash == compiler_hash:
        raise ValueError("Expected two distinct graphics CodeDirectory hashes")
    driver_entry = source.get(driver_hash)
    # Preserve the exact PCC SHA-256/platform policy flags. The compiler entry
    # is a local research grant and is always reported as such.
    if driver_entry is None or driver_entry[20:] != bytes.fromhex("02400200"):
        raise ValueError("Original PCC graphics trust policy does not match")
    compiler_entry = compiler_hash + driver_entry[20:]
    additions = [driver_entry, compiler_entry]
    if (adapted_driver is None) != (bridge is None):
        raise ValueError("Adapted graphics driver and bridge must be trusted together")
    if adapted_driver is not None:
        if (len(adapted_driver) != 20 or len(bridge) != 20
                or len({driver_hash, compiler_hash, adapted_driver, bridge}) != 4):
            raise ValueError("Expected distinct adapted graphics CodeDirectory hashes")
        additions = [adapted_driver + driver_entry[20:], compiler_entry, bridge + driver_entry[20:]]
    for entry in additions:
        if entry[:20] in entries:
            raise ValueError("Graphics trust-cache entry already exists or conflicts")
        entries[entry[:20]] = entry
    body = b"".join(sorted(entries.values()))
    combined = struct.pack("<I16sI", 2, hashlib.sha256(body).digest()[:16], len(entries)) + body
    report = {"entries": len(entries), "original_entry_bytes_preserved": True,
                      "original_pcc_driver_entry": driver_entry.hex(),
                      "original_pcc_driver_entry_installed": adapted_driver is None,
                      "local_compiler_entry": compiler_entry.hex(),
                      "compiler_platform_policy_grant": True, "compiler_apple_authenticated": False}
    if adapted_driver is not None:
        report.update(local_driver_entry=additions[0].hex(), local_bridge_entry=additions[2].hex(),
                      driver_apple_authenticated=False, bridge_apple_authenticated=False)
    return combined, report


def adapt_driver(original, destination, signer, deadline):
    """Append a bridge dependency without moving code or existing library ordinals."""
    if hashlib.sha256(original).hexdigest() != DRIVER_FILES[DRIVER]:
        raise ValueError("Driver adaptation requires the exact original PCC bundle")
    # The pinned Mach-O has 32 bytes of header padding before __text. Replacing
    # LC_SOURCE_VERSION (16 bytes) with a final LC_LOAD_DYLIB (40 bytes) fits.
    count, size = struct.unpack_from("<II", original, 16)
    if 32 + size != 0xda8 or original[0xda8:0xdc8] != bytes(32):
        raise ValueError("Original driver header padding differs from the profile")
    offset, commands, removed = 32, [], 0
    for _ in range(count):
        command, length = struct.unpack_from("<II", original, offset)
        if length < 8 or length % 8 or offset + length > 32 + size:
            raise ValueError("Invalid original driver load command")
        if command == 0x2a:
            if length != 16:
                raise ValueError("Unexpected source-version command")
            removed += 1
        else:
            commands.append(original[offset:offset + length])
        offset += length
    if removed != 1 or offset != 32 + size:
        raise ValueError("Original driver load-command profile changed")
    dependency = ("@loader_path/" + BRIDGE_NAME).encode() + b"\0"
    if len(dependency) > 16:
        raise ValueError("Bridge dependency exceeds reserved command space")
    commands.append(struct.pack("<6I", 0xc, 40, 24, 0, 0, 0) + dependency.ljust(16, b"\0"))
    header = b"".join(commands)
    changed = bytearray(original)
    struct.pack_into("<II", changed, 16, len(commands), len(header))
    changed[32:0xdc8] = header.ljust(0xdc8 - 32, b"\0")
    destination.write_bytes(changed)
    signer = signer.resolve(strict=True)
    process = subprocess.run([str(signer), "-Cadhoc", "-S", str(destination)], capture_output=True,
                             timeout=max(0.01, min(90, deadline - time.monotonic())))
    if process.returncode:
        raise RuntimeError("Local GPU driver signing failed: " + process.stderr[:4096].decode(errors="replace"))
    result = destination.read_bytes()
    integrity = inspect_plugin(result, bundle=True)
    if result[0xdc8:0x76d00] != original[0xdc8:0x76d00]:
        raise ValueError("Driver signing changed original executable or data bytes")
    return result, integrity, {
        "dependency": "@loader_path/" + BRIDGE_NAME, "source_version_command_removed": True,
        "original_library_ordinals_preserved": True, "original_payload_bytes_preserved": True,
        "original_payload_sha256": hashlib.sha256(original[0xdc8:0x76d00]).hexdigest(),
        "original_driver_sha256": DRIVER_FILES[DRIVER], "local_ad_hoc_signature": True,
        "apple_authenticated": False}


def prepare_graphics(output, root, receipt, trustcache, original_cache, *, signer=None, deadline=None):
    """Verify immutable inputs before staging the bounded guest payload."""
    pcc = receipt.get("pcc", {})
    if not isinstance(pcc, dict):
        raise ValueError("Missing imported PCC identity")
    _manifest, entries, identity = manifest(root, pcc.get("identity_index"), pcc=True,
                                          filename="pcc-BuildManifest.plist")
    if identity != pcc or identity["build_version"] != "23E5207q":
        raise ValueError("Graphics preparation requires the imported original vphone600 23E5207q identity")
    compiler = receipt.get("gpu_compiler", {})
    build = json.loads(read_component(root, "gpu-compiler/plugin.json", 1 << 20))
    if (not isinstance(compiler, dict) or not isinstance(build, dict)
            or not isinstance(build.get("plugin"), dict)
            or compiler.get("source_sha256") != SOURCE_SHA256 or build.get("source_sha256") != SOURCE_SHA256
            or build.get("state") != "built" or build.get("scope") != "ios-paravirtual-gpu-compiler-plugin"):
        raise ValueError("Graphics preparation requires the imported source-built compiler plugin")
    bundle = root / "gpu" / (DRIVER + ".bundle")
    files = {name: read_component(bundle, name, 16 << 20) for name in DRIVER_FILES}
    for name, digest in DRIVER_FILES.items():
        if hashlib.sha256(files[name]).hexdigest() != digest:
            raise ValueError("Original PCC graphics bundle differs from the pinned build: " + name)
    plugin = read_component(root, "gpu-compiler/" + NAME, 16 << 20)
    digest = hashlib.sha256(plugin).hexdigest()
    for record in (compiler, build.get("plugin", {})):
        if record.get("sha256") != digest or record.get("bytes") != len(plugin):
            raise ValueError("Graphics compiler changed after its build or import")
    driver_integrity = inspect_plugin(files[DRIVER], bundle=True)
    compiler_integrity = inspect_plugin(plugin)
    original_driver_hash = bytes.fromhex(driver_integrity["cdhash"])
    entry = entries.get("StaticTrustCache", {})
    source = read_component(trustcache.parent, trustcache.name, 4 << 20)
    normalized, _old, _new = retag_im4p(source, entry.get("Info", {}).get("Img4PayloadType"))
    if hashlib.sha384(normalized).digest() != entry.get("Digest"):
        raise ValueError("GPU trust cache differs from the original PCC manifest")
    fields = der_fields(normalized)
    pcc_cache = normalized[fields[3][1]:fields[3][2]]
    files[NAME] = plugin
    target = output / "graphics/payload"
    target.mkdir(mode=0o700, parents=True)
    bridge_record, adaptation = None, None
    if "bridge" in build or "bridge" in compiler:
        if signer is None or deadline is None or "bridge" not in compiler or "bridge" not in build:
            raise ValueError("GPU bridge requires its complete import and the support signing tool")
        bridge, _source, _header = inspect_bridge(root / "gpu-compiler", build["bridge"])
        # Recheck both receipts; their absolute artifact paths may differ after import.
        inspect_bridge(root / "gpu-compiler", compiler["bridge"])
        files[BRIDGE_NAME] = bridge
        files[DRIVER], driver_integrity, adaptation = adapt_driver(
            files[DRIVER], target / DRIVER, signer, deadline)
        bridge_record = {**build["bridge"], "path": BUNDLE_PATH + "/" + BRIDGE_NAME}
    combined, cache = extend_cache(original_cache, pcc_cache, original_driver_hash,
        bytes.fromhex(compiler_integrity["cdhash"]),
        adapted_driver=bytes.fromhex(driver_integrity["cdhash"]) if bridge_record else None,
        bridge=bytes.fromhex(bridge_record["integrity"]["cdhash"]) if bridge_record else None)
    inventory = []
    for name, data in files.items():
        path = target / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(data)
        inventory.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    report = {"bundle_path": BUNDLE_PATH, "files": inventory,
                      "driver": driver_integrity, "compiler": compiler_integrity,
                      "trustcache": cache, "pcc_trustcache_sha256": hashlib.sha256(source).hexdigest(),
                      "original_pcc_manifest_digest_matched": True,
                      "original_apple_code_modified": bridge_record is not None,
                      "original_apple_payload_bytes_preserved": True, "source_built_compiler_added": True,
                      "guest_execution_verified": False, "qualified_for_app_development": False}
    if bridge_record:
        report.update(bridge=bridge_record["integrity"], compiler_bridge=bridge_record,
                      driver_adaptation=adaptation)
    return combined, report


def helper_script(output, root, graphics):
    if graphics is None:
        return "", ""
    expected = set(DRIVER_FILES) | {NAME}
    if graphics.get("compiler_bridge") is not None:
        expected.add(BRIDGE_NAME)
    rows = graphics["files"]
    if len(rows) != len(expected) or {row["path"] for row in rows} != expected:
        raise ValueError("Unexpected graphics payload inventory")
    target = root / "gpu.bundle"
    target.mkdir(mode=0o755)
    checksums = []
    for row in rows:
        name = row["path"]
        data = read_component(output / "graphics/payload", name, 16 << 20)
        digest = hashlib.sha256(data).hexdigest()
        if digest != row["sha256"] or len(data) != row["bytes"]:
            raise ValueError("Graphics payload changed after preparation")
        path = target / name
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o755 if name in (DRIVER, NAME, BRIDGE_NAME) else 0o644)
        checksums.append(f"{digest}  {name}\n")
    (root / "gpu.sha256").write_text("".join(checksums))
    check = DESTINATION_SHELL + "\nprepare_graphics_destination /mnt/preboot\n"
    install = f"""phase install-graphics
mkdir -p -m 755 /mnt/preboot/Cryptexes
mkdir -m 755 /mnt/preboot/{GRAPHICS_RELATIVE_PATH}
cp -a /gpu.bundle/. /mnt/preboot/{GRAPHICS_RELATIVE_PATH}/
cd /mnt/preboot/{GRAPHICS_RELATIVE_PATH}
sha256sum -c /gpu.sha256
cd /
echo TOMACCO_GRAPHICS_VERIFIED
"""
    return check, install
