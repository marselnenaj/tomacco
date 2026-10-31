"""Version-pinned local Cryptex policy and iOS 27 VM compatibility.

Only explicit research preparation calls this module. It emits a private kernel
copy and a receipt; no proprietary bytes belong in the repository. The Cryptex
adapter substitutes exact local pins for Apple authentication on two metadata
objects. APFS content hashes and code signature checks remain active.
"""
import hashlib
import json
from pathlib import Path
import plistlib
import re
import struct
import subprocess
import time

from ios_paravirt_firmware import der_fields, read_component, retag_im4p, validate_im4m
from ios_paravirt_userland import IPHONE_BUILD_SHA256
from prepare_ios_paravirt import MAX_TREE_BYTES, ap_cpu_count, decode
from prepare_ios_userland import relative_path
from ios_paravirt_graphics import PLUGIN_PERSONALITY

KERNEL_SHA256 = "86895a2eebeb7fcfb68bcd60f2d7e685687faea07ee31166fb58c872b38e0d92"
PIN_ENTRY, PIN_CAVE, PIN_END = 0x1b19f7c, 0x250d8d0, 0x250ff50
REGION_ENTRY, REGION_CAVE = 0x1e0f4e8, 0x250ff50
LOCKDOWN_ENTRY, LOCKDOWN_CAVE = 0x21f1668, 0x250ff70
CONTAINER_ENTRY, CONTAINER_CAVE = 0x1ab0654, 0x250ffc0
SWAP_DISPATCH_SIZE, SWAP_HANDLER_SIZE = 0x9c7234, 0x16ae22c
IOS27_SWAP_BYTES = 0x6e0
GPU_ENTRY, GPU_CAVE, GPU_CONTINUE, GPU_BLOB_GET = 0x1aa2cc4, 0x1ca3400, 0x1aa2b8c, 0x223776c
VIRTIO_USED_ENTRY, VIRTIO_USED_CAVE = 0x108a7d0, 0x1ca3500
INSTALLER_PORTS_ENTRY, INSTALLER_PORTS_CAVE, CSPROC_BLOB_GET = 0x1cff650, 0x1ca3600, 0x2194334
INSTALLER_CDHASH = "ebb83da92cb5d45df03f12993dc7e96f10af8302"
# What differs between two prepared kernels of the pinned original: the seal
# pins' cave, which embeds the local IM4M, and the mapping adapter's four hashes
# (driver, compiler, bridge, development adapter), which are that user's builds.
OWN_INPUT_RANGES = ((PIN_CAVE, PIN_END), (GPU_CAVE + 0x90, GPU_CAVE + 0x90 + 4 * 20))
METADATA = {"Cryptex1,SystemVolume": "os.dmg.root_hash",
            "Cryptex1,SystemTrustCache": "os.dmg.trustcache",
            "Cryptex1,AppVolume": "app.dmg.root_hash",
            "Cryptex1,AppTrustCache": "app.dmg.trustcache"}


def kernel_profile(data):
    """SHA-256 of a kernel without OWN_INPUT_RANGES. Equal profiles mean equal
    bytes everywhere else, so a tool pinned to one recorded kernel can accept a
    user's kernel prepared from their own IM4M and builds, and nothing else."""
    masked = bytearray(data)
    for start, end in OWN_INPUT_RANGES:
        masked[start:end] = bytes(end - start)
    return hashlib.sha256(masked).hexdigest()


def original_component(root, entry):
    relative = str(Path("components") / relative_path(entry["Info"]["Path"]))
    source = read_component(root, relative, 1 << 20)
    normalized, _old, _new = retag_im4p(source, entry["Info"].get("Img4PayloadType"))
    if hashlib.sha384(normalized).digest() != entry["Digest"]:
        raise ValueError("Cryptex metadata differs from the original iPhone manifest")
    return normalized


def validate_seal(data, tag):
    fields = der_fields(data)
    if (len(data) != 229 or len(fields) != 4 or data[fields[0][1]:fields[0][2]] != b"IM4P"
            or data[fields[1][1]:fields[1][2]] != tag or fields[3] != (4, 21, 229)
            or struct.unpack_from("<4I", data, 21) != (2, 0, 1, 32)):
        raise ValueError("Unsupported original Cryptex seal; expected v2 SHA-256 cssy/casy")
    payload = data[21:]
    if not any(payload[16:48]) or any(payload[start + 32:start + 64] != bytes(32)
                                       for start in (16, 80, 144)):
        raise ValueError("Cryptex seal has an invalid root hash or padding")


def manifest_name(tree_data):
    tree = decode(tree_data)
    target = tree.get("target-sub-type")
    ecid = tree.child("chosen").get("unique-chip-id")
    if target != b"VPHONE600AP\0" or len(ecid) != 8 or not any(ecid):
        raise ValueError("Cryptex boot requires the prepared vphone600 device identity")
    return f"apticket.vphone600ap.{int.from_bytes(ecid, 'little'):X}.im4m"


def branch(source, target):
    delta = target - source
    if delta % 4 or not -(1 << 27) <= delta < 1 << 27:
        raise ValueError("Kernel adapter branch exceeds ARM64 range")
    return struct.pack("<I", 0x14000000 | ((delta // 4) & 0x3ffffff))


def assemble(output, name, source, address, continuation, deadline, *, success=None, calls=None):
    assembly, obj, binary = (output / (name + suffix) for suffix in (".S", ".o", ".bin"))
    assembly.write_text(source)
    def run(command):
        return subprocess.check_output(list(map(str, command)), stderr=subprocess.STDOUT,
                                       timeout=max(0.01, deadline - time.monotonic()))
    run(["llvm-mc", "-triple=aarch64", "-filetype=obj", assembly, "-o", obj])
    relocs = run(["llvm-objdump", "-r", obj]).decode()
    refs = re.findall(r"^([0-9a-fA-F]+)\s+(R_\S+)\s+(\S+)\s*$", relocs, re.MULTILINE)
    targets = {"original_continue": (continuation, "R_AARCH64_JUMP26", 0x14000000)}
    if success is not None:
        targets["original_success"] = (success, "R_AARCH64_JUMP26", 0x14000000)
    for symbol, target in (calls or {}).items():
        if symbol in targets:
            raise ValueError("Duplicate kernel adapter relocation target")
        targets[symbol] = (target, "R_AARCH64_CALL26", 0x94000000)
    if (len(refs) != len(targets) or {ref[2] for ref in refs} != set(targets)
            or any(kind != targets[symbol][1] for _offset, kind, symbol in refs)):
        raise ValueError("Unexpected kernel adapter relocations")
    run(["llvm-objcopy", "-O", "binary", "--only-section=.text", obj, binary])
    code = bytearray(binary.read_bytes())
    for raw_offset, _kind, symbol in refs:
        offset = int(raw_offset, 16)
        target, _relocation, opcode = targets[symbol]
        if offset % 4 or offset + 4 > len(code) or code[offset:offset + 4] != struct.pack("<I", opcode):
            raise ValueError("Invalid kernel adapter continuation")
        instruction = int.from_bytes(branch(address + offset, target), "little") | opcode
        code[offset:offset + 4] = struct.pack("<I", instruction)
    binary.write_bytes(code)
    return bytes(code)


def patch_kernel(original, pins, region, lockdown, container, *, virtio_used, graphics=None,
                 installer_ports=None):
    if hashlib.sha256(original).hexdigest() != KERNEL_SHA256:
        raise ValueError("Research Cryptex boot requires the exact original vphone600 23E5207q kernel")
    if (original[PIN_ENTRY:PIN_ENTRY + 8] != bytes.fromhex("7f2303d5ff0305d1")
            or original[REGION_ENTRY:REGION_ENTRY + 20]
            != bytes.fromhex("f60761b2c00e084ee01f803dfa0761b218000014")
            or original[LOCKDOWN_ENTRY:LOCKDOWN_ENTRY + 8] != bytes.fromhex("7f2303d5fc6fbaa9")
            or original[CONTAINER_ENTRY:CONTAINER_ENTRY + 12] != bytes.fromhex("80f8ff34885bff9008213b91")
            or original[SWAP_DISPATCH_SIZE - 4:SWAP_DISPATCH_SIZE + 12] != struct.pack("<4I", 0, 0x588, 0, 0)
            or original[SWAP_HANDLER_SIZE:SWAP_HANDLER_SIZE + 8] != bytes.fromhex("5f20167121030054")):
        raise ValueError("Kernel instruction profile does not match")
    if original[VIRTIO_USED_ENTRY:VIRTIO_USED_ENTRY + 16] != bytes.fromhex(
            "0900088b29050091080500512001288a"):
        raise ValueError("VirtIO used-ring accessor differs from the verified profile")
    if (not 184 < len(pins) <= PIN_END - PIN_CAVE or len(region) != 28
            or len(lockdown) != 80 or len(container) != 24 or len(virtio_used) != 20):
        raise ValueError("Kernel adapter exceeds its verified executable padding")
    data = bytearray(original)
    for entry, cave, code in ((PIN_ENTRY, PIN_CAVE, pins), (REGION_ENTRY, REGION_CAVE, region),
                              (LOCKDOWN_ENTRY, LOCKDOWN_CAVE, lockdown),
                              (CONTAINER_ENTRY, CONTAINER_CAVE, container),
                              (VIRTIO_USED_ENTRY, VIRTIO_USED_CAVE, virtio_used)):
        if data[cave:cave + len(code)] != bytes(len(code)):
            raise ValueError("Kernel adapter destination is not empty")
        data[cave:cave + len(code)] = code
        data[entry:entry + 4] = branch(entry, cave)
    # This profile is exclusively for the pinned iOS 27 userland. Keep exact
    # dispatch/handler length checks; accept its native record without truncation
    # or enabling the unrestricted variable-size IOUserClient dispatch sentinel.
    struct.pack_into("<I", data, SWAP_DISPATCH_SIZE, IOS27_SWAP_BYTES)
    struct.pack_into("<I", data, SWAP_HANDLER_SIZE, 0x7100005f | (IOS27_SWAP_BYTES << 10))
    if graphics is not None:
        # This cave is in tmpfs's final RX page, not bootstrap __KLD padding
        # which XNU later marks NX. Verified on the exact running kernel.
        # Two/three graphics pins, optionally followed by one owned installer
        # library pin. Even four entries stay below the next RX adapter.
        if (len(graphics) not in (184, 204, 224) or original[GPU_ENTRY:GPU_ENTRY + 4] != bytes.fromhex("b2ffff17")
                or original[GPU_BLOB_GET:GPU_BLOB_GET + 16] != bytes.fromhex("7f2303d5f85fbca9f65701a9f44f02a9")
                or original[GPU_CAVE:GPU_CAVE + len(graphics)] != bytes(len(graphics))):
            raise ValueError("GPU adapter does not match the verified executable kernel profile")
        data[GPU_CAVE:GPU_CAVE + len(graphics)] = graphics
        data[GPU_ENTRY:GPU_ENTRY + 4] = branch(GPU_ENTRY, GPU_CAVE)
        data = bytearray(graphics_personality(bytes(data)))
    if installer_ports is not None:
        # The original hash pins both this MIG dispatcher and the live RX cave.
        # Other callers, operations and errors keep the original policy result.
        if (len(installer_ports) != 148 or installer_ports[128:] != bytes.fromhex(INSTALLER_CDHASH)
                or original[INSTALLER_PORTS_ENTRY:INSTALLER_PORTS_ENTRY + 4] != bytes.fromhex("20010034")
                or original[CSPROC_BLOB_GET:CSPROC_BLOB_GET + 20]
                != bytes.fromhex("7f2303d5f657bda9f44f01a9fd7b02a9fd830091")
                or data[INSTALLER_PORTS_CAVE:INSTALLER_PORTS_CAVE + len(installer_ports)]
                != bytes(len(installer_ports))):
            raise ValueError("Installer port adapter does not match the verified kernel profile")
        data[INSTALLER_PORTS_CAVE:INSTALLER_PORTS_CAVE + len(installer_ports)] = installer_ports
        data[INSTALLER_PORTS_ENTRY:INSTALLER_PORTS_ENTRY + 4] = branch(INSTALLER_PORTS_ENTRY, INSTALLER_PORTS_CAVE)
    return bytes(data)


def graphics_personality(data):
    old = b'<key>MetalPluginName</key>\n\t\t\t\t\t<string>AppleParavirtGPUMetalIOGPUFamily</string>'
    return _graphics_personality_slot(data, old)


def _graphics_personality_slot(data, old):
    prefix, suffix = b'<key>MetalPluginName</key>\n', b'</string>'
    body = b'<string>' + PLUGIN_PERSONALITY.encode() + suffix
    padding = len(old) - len(prefix) - len(body)
    if padding < 0:
        raise ValueError("GPU personality does not fit the original prelink slot")
    new = prefix + b'\t' * padding + body
    if len(old) != len(new) or data.count(old) != 1:
        raise ValueError("GPU personality differs from the pinned kernel")
    at = data.index(old)
    start, end = data.rfind(b'<?xml', 0, at), data.find(b'</plist>', at) + 8
    if start < 0 or end < at:
        raise ValueError("GPU personality has no complete prelink plist")
    expected = plistlib.loads(data[start:end])
    matches = [item for item in expected["_PrelinkInfoDictionary"]
               if item.get("CFBundleIdentifier") == "com.apple.driver.AppleParavirtGPUIOGPUFamily"]
    if len(matches) != 1:
        raise ValueError("Ambiguous GPU personality")
    matches[0]["IOKitPersonalities"]["AppleParavirtGPUARM"]["MetalPluginName"] = PLUGIN_PERSONALITY
    patched = data.replace(old, new)
    if plistlib.loads(patched[start:end]) != expected:
        raise ValueError("Unexpected GPU prelink metadata change")
    return patched


def prepare_boot(output, kernel, boot_manifest, device_tree, root, entries, ios, deadline, *,
                 graphics=None, installer_control=False, development_library=None):
    if development_library is not None and (graphics is None or not installer_control):
        raise ValueError("Development mapping requires graphics preparation and installer_control")
    if ios["build_manifest_sha256"] != IPHONE_BUILD_SHA256:
        raise ValueError("Research Cryptex boot supports only the original iPhone 24A446 identity")
    original = read_component(kernel.parent, kernel.name, 64 << 20)
    if hashlib.sha256(original).hexdigest() != KERNEL_SHA256:
        raise ValueError("Research Cryptex boot requires the exact original vphone600 23E5207q kernel")
    ticket = read_component(boot_manifest.parent, boot_manifest.name, 9216)
    validate_im4m(ticket)
    tree_data = read_component(device_tree.parent, device_tree.name, MAX_TREE_BYTES)
    ap_cpus = ap_cpu_count(decode(tree_data))
    ticket_name = manifest_name(tree_data)
    directory, objects = output / "kernel", output / "graft-objects"
    directory.mkdir(mode=0o700)
    objects.mkdir(mode=0o700)
    records = {}
    for component, name in METADATA.items():
        data = original_component(root, entries[component])
        if name.endswith("root_hash"):
            validate_seal(data, b"cssy" if name.startswith("os.") else b"casy")
        (objects / name).write_bytes(data)
        records[name] = {"component": component, "sha256": hashlib.sha256(data).hexdigest(),
                         "bytes": len(data), "original_manifest_digest_matched": True}
    (objects / ticket_name).write_bytes(ticket)
    (output / "device-tree").write_bytes(tree_data)
    sources = Path(__file__).resolve().parent.parent / "integrations/ios-emulator"
    body = (sources / "cryptex-seal-pins.S").read_text()
    assembly = f".equ TOMACCO_MANIFEST_BYTES, {len(ticket)}\n" + body + "\n.p2align 3\n"
    for label, name in (("os_pin", "os.dmg.root_hash"), ("app_pin", "app.dmg.root_hash"),
                        ("manifest_pin", ticket_name)):
        assembly += label + ":\n.incbin " + json.dumps(str(objects / name)) + "\n"
    pins = assemble(directory, "cryptex-pins", assembly, PIN_CAVE, PIN_ENTRY + 4, deadline)
    region = assemble(directory, "shared-region", (sources / "ios27-shared-region.S").read_text(),
                      REGION_CAVE, 0x1e0f558, deadline)
    lockdown = assemble(directory, "lockdown-alias", (sources / "ios27-lockdown-alias.S").read_text(),
                        LOCKDOWN_CAVE, LOCKDOWN_ENTRY + 4, deadline)
    container = assemble(directory, "container-rpc", (sources / "ios27-container-rpc.S").read_text(),
                         CONTAINER_CAVE, CONTAINER_ENTRY + 4, deadline, success=0x1ab0564)
    # Use live RX padding, shared with but disjoint from the GPU mapping adapter.
    # The full original kernel hash pins both the owner layout and destination.
    virtio_used = assemble(directory, "virtio-used-ring", (sources / "virtio-used-ring.S").read_text(),
                           VIRTIO_USED_CAVE, VIRTIO_USED_ENTRY + 8, deadline)
    gpu, gpu_policy, development_policy = None, None, None
    if graphics is not None:
        hashes = [graphics[kind]["cdhash"] for kind in ("driver", "compiler")]
        if "bridge" in graphics:
            hashes.append(graphics["bridge"]["cdhash"])
        graphics_hashes = list(hashes)
        if development_library is not None:
            hashes.append(development_library["cdhash"])
        if any(re.fullmatch(r"[0-9a-f]{40}", value) is None for value in hashes) or len(set(hashes)) != len(hashes):
            raise ValueError("Expected distinct local mapping CodeDirectory hashes")
        source = f".set tomacco_gpu_hash_count, {len(hashes)}\n" + (sources / "ios-gpu-mmap.S").read_text()
        source += "\n.p2align 3\nallowed_code_hashes:\n"
        source += ".byte " + ",".join(str(value) for digest in hashes for value in bytes.fromhex(digest)) + "\n"
        gpu = assemble(directory, "gpu-mmap", source, GPU_CAVE, GPU_CONTINUE, deadline,
                       calls={"original_cs_blob_get": GPU_BLOB_GET})
        gpu_policy = {"entry_vmaddr": "0xfffffe0008aa6cc4", "scope": "pinned-graphics-code-directories",
                      "allowed_cdhashes": graphics_hashes, "required_code_signing_flags": "0x24000003",
                      "original_sandbox_decision_executed": True, "tolerated_error": 1,
                      "other_errors_preserved": True, "amfi_page_checks_modified": False,
                      "other_mac_policies_modified": False, "bundle_path": graphics["bundle_path"]}
        if development_library is not None:
            development_policy = {
                **development_library, "scope": "exact-owned-installer-library-code-directory",
                "entry_vmaddr": "0xfffffe0008aa6cc4", "required_code_signing_flags": "0x24000003",
                "original_sandbox_decision_executed": True, "tolerated_error": 1,
                "other_errors_preserved": True, "amfi_page_checks_modified": False,
                "other_mac_policies_modified": False, "guest_execution_verified": False,
                "installation_verified": False}
    installer_ports, installer_policy = None, None
    if installer_control:
        source = (sources / "ios-installer-ports.S").read_text()
        source += "\n.p2align 3\ninstaller_code_hash:\n.byte "
        source += ",".join(str(value) for value in bytes.fromhex(INSTALLER_CDHASH)) + "\n"
        installer_ports = assemble(directory, "installer-ports", source, INSTALLER_PORTS_CAVE,
                                   INSTALLER_PORTS_ENTRY + 4, deadline,
                                   success=INSTALLER_PORTS_ENTRY + 0x24,
                                   calls={"original_csproc_get_blob": CSPROC_BLOB_GET})
        installer_policy = {"scope": "exact-original-installer-registered-ports",
                            "allowed_cdhash": INSTALLER_CDHASH,
                            "required_code_signing_flags": "0x24000001", "mig_id": 3404,
                            "tolerated_error": 53, "original_sandbox_decision_executed": True,
                            "original_mig_handler_retained": True, "reply_fabricated": False,
                            "other_callers_operations_and_errors_preserved": True,
                            "entry_vmaddr": "0xfffffe0008d03650",
                            "installation_verified": False}
    patched = patch_kernel(original, pins, region, lockdown, container,
                           virtio_used=virtio_used, graphics=gpu, installer_ports=installer_ports)
    target = output / "kernelcache.research.vphone600"
    target.write_bytes(patched)
    return {"kernel": "kernelcache.research.vphone600", "kernel_build": "23E5207q",
            "original_kernel_sha256": KERNEL_SHA256, "kernel_sha256": hashlib.sha256(patched).hexdigest(),
            "policy": "exact-byte-pins-for-two-original-Cryptex-seals-and-local-manifest",
            "authentication_checks_modified": True, "apple_signature_verified": False,
            "fallback": "original-validate_payload_and_manifest", "graft_type": 1,
            "original_apfs_content_verification_retained": True,
            "code_signature_checks_modified": False, "cache_bytes_modified": False,
            "graphics_mapping_policy": gpu_policy,
            "installer_control_policy": installer_policy,
            "development_mapping_policy": development_policy,
            "virtio_queue_compatibility": {
                "entry_vmaddr": "0xfffffe000808e7d0", "layout": "split-ring",
                "available_entries_included": True, "used_event_included": True,
                "virtual_layout_preserved": True, "queue_capacity_preserved": True},
            "shared_region_base": 0x180000000, "shared_region_size": 0x1a0000000,
            "cache_aslr_preserved": True, "objects": records,
            "sysctl_alias": {"name": "security.mac.lockdown_mode_state_public",
                             "target": "security.mac.lockdown_mode_state",
                             "real_kernel_state_preserved": True, "access_checks_preserved": True,
                             "entry_vmaddr": "0xfffffe00091f5668"},
            "container_rpc_compatibility": {
                "entry_vmaddr": "0xfffffe0008ab4654", "scope": "legacy-platform-app-container-upcall",
                "tolerated_error": 72, "tolerated_error_name": "EBADRPC",
                "other_errors_preserved": True, "remaining_sandbox_setup_preserved": True,
                "amfi_checks_modified": False, "container_paths_fabricated": False},
            "framebuffer_swap_compatibility": {
                "selector": 5, "original_record_bytes": 0x588, "record_bytes": IOS27_SWAP_BYTES,
                "dispatch_size_vmaddr": "0xfffffe00079cb234",
                "handler_compare_vmaddr": "0xfffffe00086b222c",
                "exact_size_checks_retained": True, "userland_record_truncated": False,
                "userland_swap_routing_modified": False},
            "manifest": {"name": ticket_name, "bytes": len(ticket), "sha256": hashlib.sha256(ticket).hexdigest()},
            "boot_inputs": {"kernel": str(target), "device_tree": str(output / "device-tree"),
                            "boot_manifest": str(objects / ticket_name), "sep_manifest": str(objects / ticket_name),
                            "boot_args": f"debug=0x8 cpus={ap_cpus} rd=disk0s1 serial=3 -v -noprogress keepsyms=1 wdt=-1 ignition_level=4"}}
