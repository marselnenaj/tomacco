#!/usr/bin/env python3
"""Prepare a private vphone600 device tree for Tomacco's research board.

The Apple inputs stay unchanged. This does not install iOS or qualify an app
runtime. GPU and SEP nodes are retained, including their original contracts.
"""
import argparse
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import secrets
import struct
import zlib

FIRMWARE = b"tomacco-vphone-research-v2\0"
DRAM_BASE = 0x70000000
MAX_TREE_BYTES = 16 * 1024 * 1024
TREE_AP_CPU_COUNT = 32
MAX_AP_CPU_COUNT = 16
DEFAULT_AP_CPU_COUNT = 6
REGIONS = (
    "TXM-ro TXM-rx TXM-bx TXM-rw TXM-le TXM-entry TXM-virt TrustCache "
    "BootKC-rx BootKC-bx BootKC-ro BootKC-rs BootKC-rw BootKC-le BootKC-virt BootKC-entry "
    "DeviceTree SPTM-ro SPTM-rm SPTM-rx SPTM-rw SPTM-le SPTM-entry SPTM-virt "
    "BootArgs CL4-rx CL4-ro CL4-rw CL4-le CL4-dummypage CL4-entry CL4-virt RAMDisk"
).split()


def chrp_header(signature, size, name):
    header = bytearray(struct.pack("<BBH12s", signature, 0, size // 16, name))
    checksum = signature + sum(header[2:])
    while checksum > 255:
        checksum = (checksum & 255) + (checksum >> 8)
    header[1] = checksum
    return header


def blank_nvram(size=8192, generation=1):
    """Fresh CHRP bank with real common/system partitions and checksums.

    The format follows XNU's IONVRAMCHRPHandler. This is initial boot state,
    not an implementation of a persistent NVRAM controller.
    """
    if not 4096 <= size <= 1024 * 1024 or size % 16 or not 0 <= generation <= 0xffffffff:
        raise ValueError("Invalid initial CHRP bank geometry or generation")
    image = bytearray(size)
    image[:16] = chrp_header(0x5a, 32, b"nvram")
    struct.pack_into("<I", image, 20, generation)
    image[32:48] = chrp_header(0x70, 2048, b"common")
    image[2080:2096] = chrp_header(0x70, len(image) - 2080, b"system")
    struct.pack_into("<I", image, 16, zlib.adler32(image[20:]))
    return bytes(image)


def validate_nvram(image):
    if not 4096 <= len(image) <= 1024 * 1024 or len(image) % 16:
        raise ValueError("NVRAM must be an aligned CHRP bank between 4 KiB and 1 MiB")
    if image[4:16].split(b"\0", 1)[0] not in (b"nvram", b"2nvram"):
        raise ValueError("Only CHRP v1/v2 NVRAM snapshots are supported")
    if struct.unpack_from("<I", image, 16)[0] != zlib.adler32(image[20:]):
        raise ValueError("NVRAM bank checksum mismatch")
    offset = 0
    partitions = []
    while offset < len(image):
        signature, checksum, blocks, name = struct.unpack_from("<BBH12s", image, offset)
        size = blocks * 16
        if size < 16 or size > len(image) - offset:
            raise ValueError("NVRAM partition exceeds its bank")
        if chrp_header(signature, size, name)[1] != checksum:
            raise ValueError("NVRAM partition checksum mismatch")
        partitions.append(name.rstrip(b"\0"))
        offset += size
    if not set(partitions).intersection((b"common", b"2common")):
        raise ValueError("NVRAM bank has no common partition")


@dataclass
class Property:
    data: bytes
    flags: int = 0
    padding: bytes = b""

    def encode(self, name):
        label = name.encode("ascii")
        if not label or len(label) > 31 or b"\0" in label:
            raise ValueError(f"Invalid property name: {name!r}")
        pad_length = -len(self.data) % 4
        padding = self.padding if len(self.padding) == pad_length else bytes(pad_length)
        return (label.ljust(32, b"\0") + struct.pack("<I", len(self.data) | self.flags)
                + self.data + padding)


@dataclass
class Node:
    props: dict[str, Property] = field(default_factory=dict)
    children: list = field(default_factory=list)

    def get(self, name):
        try:
            return self.props[name].data
        except KeyError:
            raise ValueError(f"Missing device-tree property {name}") from None

    def set(self, name, data):
        self.props[name] = Property(data)

    @property
    def name(self):
        return self.get("name").split(b"\0", 1)[0].decode("ascii")

    def child(self, name):
        matches = [node for node in self.children if node.name == name]
        if len(matches) != 1:
            raise ValueError(f"Expected exactly one {name} node, found {len(matches)}")
        return matches[0]

    def encode(self):
        return (struct.pack("<II", len(self.props), len(self.children))
                + b"".join(prop.encode(name) for name, prop in self.props.items())
                + b"".join(child.encode() for child in self.children))


def decode(data):
    """Bounds-checked ADT reader; retain lengths, flags and alignment bytes."""
    if len(data) > MAX_TREE_BYTES:
        raise ValueError("Device tree exceeds 16 MiB")
    offset = 0

    def take(size):
        nonlocal offset
        if size > len(data) - offset:
            raise ValueError(f"Truncated device tree at byte {offset}")
        result = data[offset:offset + size]
        offset += size
        return result

    def node(depth):
        if depth > 64:
            raise ValueError("Device tree nesting exceeds 64 levels")
        props, children = struct.unpack("<II", take(8))
        if props > (len(data) - offset) // 36 or children > len(data) // 8:
            raise ValueError("Invalid device-tree node counts")
        result = Node()
        for _ in range(props):
            label = take(32)
            if b"\0" not in label:
                raise ValueError("Unterminated device-tree property name")
            name = label.split(b"\0", 1)[0].decode("ascii")
            if not name or name in result.props:
                raise ValueError(f"Empty or duplicate property name: {name!r}")
            raw_length, = struct.unpack("<I", take(4))
            length = raw_length & 0x7fffffff
            result.props[name] = Property(take(length), raw_length & 0x80000000,
                                          take(-length % 4))
        result.children = [node(depth + 1) for _ in range(children)]
        names = [child.name for child in result.children]
        if len(set(names)) != len(names):
            raise ValueError("Duplicate child node names")
        return result

    root = node(0)
    if offset != len(data):
        raise ValueError(f"Unexpected {len(data) - offset} trailing device-tree bytes")
    return root


def firmware_references(root):
    result = {}
    def walk(node, path):
        for name, prop in node.props.items():
            if prop.flags:
                if not prop.data.startswith((b"syscfg/", b"macaddr/")):
                    raise ValueError(f"Unknown firmware property expression at {path}/{name}")
                result[f"{path}/{name}"] = prop.data
        for child in node.children:
            walk(child, f"{path}/{child.name}")
    walk(root, "")
    return result


def ap_cpu_count(root):
    """Return the contiguous AP topology from an already prepared device tree."""
    nodes = root.child("cpus").children
    if not 1 <= len(nodes) <= MAX_AP_CPU_COUNT:
        raise ValueError(f"Prepared vphone600 tree must contain 1–{MAX_AP_CPU_COUNT} AP CPUs")
    for index, cpu in enumerate(nodes):
        if (cpu.name != f"cpu{index}" or cpu.get("device_type") != b"cpu\0"
                or len(cpu.get("reg")) != 4
                or int.from_bytes(cpu.get("reg"), "little") != index):
            raise ValueError("Prepared vphone600 AP CPU nodes must be contiguous and affinity ordered")
    return len(nodes)


def display_handoff(root):
    """Do not advertise iBoot's unallocated VRAM slot as a real memory device.

    The paravirtual GPU maps ordinary guest-memory IOSurfaces. An empty /vram
    makes IOSurfaceRoot wait indefinitely for a PurpleGfxMem region that cannot
    register. Preserve actual carveouts and every GPU/mapper property.
    """
    regions = [child for child in root.children if child.name == "vram"]
    if len(regions) > 1:
        raise ValueError("Ambiguous VRAM device-tree nodes")
    removed = False
    if regions:
        region = regions[0]
        if (region.get("device_type") != b"vram\0" or len(region.get("reg")) != 16
                or region.children or region.props["reg"].flags):
            raise ValueError("Unsupported VRAM device-tree binding")
        if region.get("reg") == bytes(16):
            root.children.remove(region)
            removed = True
    return {"unallocated_vram_node_removed": removed,
            "dedicated_vram_advertised": bool(regions) and not removed,
            "gpu_and_mapper_preserved": True}


def prepare(data, *, memory_gib=8, ap_cpus=DEFAULT_AP_CPU_COUNT, seed=None,
            omit_optional_ctrr=False, properties=None, nvram=None,
            load_sep=False, sep_handoff="preloaded"):
    if not 2 <= memory_gib <= 32:
        raise ValueError("Memory must be between 2 and 32 GiB")
    if not 1 <= ap_cpus <= MAX_AP_CPU_COUNT:
        raise ValueError(f"AP CPU count must be between 1 and {MAX_AP_CPU_COUNT}")
    if sep_handoff not in ("preloaded", "userspace") or (not load_sep and sep_handoff != "preloaded"):
        raise ValueError("SEP handoff requires a supported mode and loaded SEP processor")
    root = decode(data)
    # Bit 31 marks iBoot expressions, e.g. syscfg/kmtp, not literal values.
    # Never hand these strings to XNU as integers. Explicit supplied values
    # replace them; missing optional properties let the guest use its defaults.
    references = firmware_references(root)
    properties = properties or {}
    if set(properties) - references.keys():
        raise ValueError("An override does not name a firmware-derived property")
    if any(not isinstance(value, bytes) or len(value) > 4096 for value in properties.values()):
        raise ValueError("Property values must be bytes of at most 4096 bytes")
    def consume_firmware_flags(node, path=""):
        for name, prop in list(node.props.items()):
            if not prop.flags:
                continue
            key = f"{path}/{name}"
            if key in properties:
                node.set(name, properties[key])
            else:
                del node.props[name]
        for child in node.children:
            consume_firmware_flags(child, f"{path}/{child.name}")
    consume_firmware_flags(root)
    armio = root.child("arm-io")
    if b"vphone600ap" not in root.get("compatible").lower().split(b"\0"):
        raise ValueError("This recipe accepts only a vphone600 device tree")
    if struct.unpack("<QQQ", armio.get("ranges")) != (0, 0x10000000, 0x70000000):
        raise ValueError("Unsupported virtual iPhone I/O aperture")
    display_handoff(root)
    if omit_optional_ctrr:
        ctrr = armio.child("avp-ctrr")
        if ctrr.get("compatible") != b"avp,ctrr\0":
            raise ValueError("Unsupported optional CTRR binding")
        # vresearch1 SPTM explicitly accepts an absent avp-ctrr node.
        # This declares that no such protection device exists on this board;
        # it does not emulate or acknowledge any protection operation.
        armio.children.remove(ctrr)
    # Fail instead of moving firmware devices to guessed addresses.
    for name, address, size, interrupt in (
        ("gfx", 0x20200000, 0x10000, 49),
        ("iosurface-mapper", 0x20210000, 0x10000, 48),
        ("uart0", 0x10010000, 0x10000, None),
        ("pl031-rtc", 0x10050000, 0x1000, None),
        ("avp-rtc", 0x20240000, 0x1000, 51),
        ("pl061-buttons", 0x10060000, 0x1000, 37),
        ("pvpanic", 0x10070000, 2, None),
    ):
        device = armio.child(name)
        if struct.unpack("<QQ", device.get("reg")) != (address, size):
            raise ValueError(f"Unsupported {name} register window")
        if interrupt is not None and struct.unpack_from("<I", device.get("interrupts"))[0] != interrupt:
            raise ValueError(f"Unsupported {name} interrupt")
    aes = armio.child("aes")
    if (aes.get("reg") != struct.pack("<QQQQ", 0x20220000, 0x4000, 0x20230000, 0x4000)
            or aes.get("interrupts") != struct.pack("<II", 50, 1)
            or aes.get("compatible") != b"aes,s8000\0"):
        raise ValueError("Unsupported AES device binding")
    sep = armio.child("sep")
    if (sep.get("compatible") != b"iop-sep,vpiop\0"
            or sep.get("reg") != struct.pack("<QQ", 0x20250000, 0x1000)
            or sep.get("interrupts") != struct.pack("<8I", 0, 0, 53, 0, 52, 0, 0, 0)):
        raise ValueError("Unsupported SEP mailbox binding")
    if armio.child("gic").get("reg") != struct.pack(
            "<6Q", 0, 0x10000, 0x10000, 0x400000, 0x0fff0000, 0x10000):
        raise ValueError("Unsupported GIC distributor/redistributor/MSI windows")
    pcie = armio.child("pcie")
    if (pcie.get("reg") != struct.pack("<QQ", 0x30000000, 0x10000000)
            or pcie.get("interrupt-base") != struct.pack("<I", 64)
            or pcie.get("msi-frame-index") != struct.pack("<I", 2)
            or pcie.get("ranges") != struct.pack("<IQQQ", 0x02000000,
                                                 0x50000000, 0x40000000, 0x2fff0000)):
        raise ValueError("Unsupported PCIe device binding")
    # Its parent translates 0x40000000 to physical 0x50000000. Apple's
    # original window extends into this board's RAM; never allocate BARs there.
    pcie.set("ranges", struct.pack("<IQQQ", 0x02000000,
                                   0x50000000, 0x40000000, 0x1fff0000))
    cpus = root.child("cpus")
    cpu0 = cpus.child("cpu0")
    if int.from_bytes(cpu0.get("timebase-frequency"), "little") != 24000000:
        raise ValueError("Unsupported timebase frequency")
    expected_cpu_names = [f"cpu{i}" for i in range(TREE_AP_CPU_COUNT)]
    if [cpu.name for cpu in cpus.children] != expected_cpu_names:
        raise ValueError("vphone600 must describe all 32 AP CPU cores in affinity order")
    expected_affinities = range(TREE_AP_CPU_COUNT)
    for cpu, affinity in zip(cpus.children, expected_affinities):
        if (cpu.get("device_type") != b"cpu\0"
                or len(cpu.get("reg")) != 4
                or int.from_bytes(cpu.get("reg"), "little") != affinity):
            raise ValueError(f"Unsupported vphone600 CPU affinity: {cpu.name}")
    # Keep the selected vCPU topology consistent in XNU's ADT, QEMU's GIC,
    # and the machine's virtual hardware report. All original CPU nodes are
    # validated above before any are omitted from this private prepared tree.
    cpus.children = cpus.children[:ap_cpus]
    chosen = root.child("chosen")
    # iBoot normally fills these zero-valued fields. These are the actual
    # vphone600 identity from Apple's 23E5207q BuildManifest, not a retail
    # iPhone identity. SEPManager requires a nonzero chip identifier.
    for name, value in (("chip-id", 0xfe01), ("board-id", 0x91)):
        old = chosen.get(name) if name in chosen.props else bytes(4)
        if old not in (bytes(4), struct.pack("<I", value)):
            raise ValueError(f"Unexpected vphone600 {name}")
        chosen.set(name, struct.pack("<I", value))
    chosen.set("firmware-version", FIRMWARE)
    chosen.set("dram-base", struct.pack("<Q", DRAM_BASE))
    chosen.set("dram-size", struct.pack("<Q", memory_gib << 30))
    entropy = seed if seed is not None else secrets.token_bytes(256)
    if len(entropy) != 256:
        raise ValueError("Boot entropy must be exactly 256 bytes")
    chosen.set("random-seed", entropy)
    chosen.set("unique-chip-id", entropy[:8])
    identity_handoff(root)
    nvram = blank_nvram() if nvram is None else nvram
    validate_nvram(nvram)
    chosen.set("nvram-proxy-data", nvram)
    chosen.set("nvram-total-size", struct.pack("<I", len(nvram)))
    chosen.set("nvram-bank-size", struct.pack("<I", len(nvram)))
    maps = [child for child in chosen.children if child.name == "memory-map"]
    if maps:
        memory = maps[0]
    else:
        memory = Node({"name": Property(b"memory-map\0")})
        chosen.children.append(memory)
    for region in REGIONS:
        memory.set(region, struct.pack("<QQ", 0xffffffffffffffff, 0xffffffffffffffff))
    memory.set("slide", bytes(16))
    # This means loaded bytes, never a successful SEP boot or service reply.
    # The launcher requires --sep and the engine fills the reserved extent.
    for name in ("sepfw-loaded", "sepfw-booted"):
        sep.props.pop(name, None)
    memory.props.pop("SEPFW", None)
    if load_sep:
        # seputil's automatic startup and credential consumers consult this
        # chosen property. Userspace restore must not wait for an automatic
        # load before the host has supplied RestoreSEPImageData.
        chosen.set("sepfw-load-at-boot", struct.pack("<I", sep_handoff == "preloaded"))
        if sep_handoff == "userspace":
            chosen.set("osenvironment", b"device-recovery".ljust(32, b"\0"))
        # Direct firmware entry replaces the ROM-loading step. AppleSEPBooter
        # uses this bootloader flag to send the configuration capsule directly;
        # it still waits for the firmware's real boot/service responses.
        sep.set("sepfw-booted", struct.pack("<I", 1))
        memory.set("SEPFW", bytes(16))
        if sep_handoff == "preloaded":
            sep.set("sepfw-loaded", struct.pack("<I", 1))
        # In userspace mode the AP has no firmware object at boot. The original
        # AppleSEPManager waits in state 2 for seputil's actual IMG4 submission.
        # SEPFW reserves zeroed memory for that submission; ELFour still runs
        # privately and waits for the AP configuration capsule.
    prepared = root.encode()
    if len(prepared) > MAX_TREE_BYTES:
        raise ValueError("Prepared device tree exceeds 16 MiB")
    return prepared


def identity_handoff(root):
    """Publish the VM's existing identity through XNU's serial-number property.

    VMAppleCfg already derives this diagnostic serial from the prepared seed.
    It is a local VM label, not an Apple-authenticated hardware identity.
    """
    chosen = root.child("chosen")
    seed, ecid = chosen.get("random-seed"), chosen.get("unique-chip-id")
    if len(seed) != 256 or len(ecid) != 8 or ecid != seed[:8]:
        raise ValueError("Prepared VM seed and chip identity must agree")
    added = "serial-number" not in root.props
    value = (f"PSV{int.from_bytes(ecid, 'little'):016x}".encode() + b"\0"
             if added else root.get("serial-number"))
    if (not 2 <= len(value) <= 32 or value[-1:] != b"\0" or
            any(c not in b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in value[:-1])):
        raise ValueError("VM serial-number must be 1–31 ASCII label characters and a terminal NUL")
    if added:
        root.set("serial-number", value)
    return {"serial": value[:-1].decode("ascii"), "added": added,
            "apple_authenticated": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device_tree", type=Path, help="Unwrapped vphone600 ADT")
    parser.add_argument("output", type=Path, help="New prepared ADT; never overwrites a file")
    parser.add_argument("--memory-gib", type=int, default=8)
    parser.add_argument("--ap-cpus", type=int, default=DEFAULT_AP_CPU_COUNT,
                        help=f"AP vCPUs exposed to iOS (1–{MAX_AP_CPU_COUNT}; default: {DEFAULT_AP_CPU_COUNT})")
    parser.add_argument("--load-sep", action="store_true",
                        help="Reserve a real SEPFW boot handoff; run with matching --sep firmware")
    parser.add_argument("--sep-handoff", choices=("preloaded", "userspace"), default="preloaded",
                        help="AP firmware source: bootloader preload or userspace IMG4 submission in device recovery")
    parser.add_argument("--omit-optional-ctrr", action="store_true",
                        help="Research profile without virtual CTRR protection; retains SEP and graphics")
    parser.add_argument("--properties", type=Path,
                        help="JSON object mapping firmware property paths to explicit hex-encoded values")
    parser.add_argument("--nvram", type=Path, help="CHRP bank snapshot; defaults to a fresh empty bank")
    parser.add_argument("--entropy-from", type=Path, metavar="PREPARED_TREE",
                        help="Take the boot entropy, and with it the ECID, from this prepared tree: one guest's "
                             "restore and normal-boot trees must describe the same device; default: new entropy")
    args = parser.parse_args()
    with args.device_tree.open("rb") as stream:
        data = stream.read(MAX_TREE_BYTES + 1)
    original = decode(data)
    references = firmware_references(original)
    values = {}
    if args.properties:
        with args.properties.open("rb") as stream:
            properties_data = stream.read(1024 * 1024 + 1)
        if len(properties_data) > 1024 * 1024:
            raise ValueError("Property overrides exceed 1 MiB")
        values = json.loads(properties_data)
    if not isinstance(values, dict) or any(not isinstance(v, str) for v in values.values()):
        raise ValueError("--properties requires a JSON object of path to hex strings")
    values = {name: bytes.fromhex(value) for name, value in values.items()}
    nvram = None
    if args.nvram:
        with args.nvram.open("rb") as stream:
            nvram = stream.read(1024 * 1024 + 1)
    seed = None
    if args.entropy_from:
        with args.entropy_from.open("rb") as stream:
            seed = decode(stream.read(MAX_TREE_BYTES + 1)).child("chosen").get("random-seed")
    prepared = prepare(data, memory_gib=args.memory_gib, ap_cpus=args.ap_cpus,
                       omit_optional_ctrr=args.omit_optional_ctrr, properties=values, nvram=nvram,
                       load_sep=args.load_sep, sep_handoff=args.sep_handoff, seed=seed)
    generated = ({"/serial-number"} if "/serial-number" not in values and
                 ("/serial-number" in references or "serial-number" not in original.props) else set())
    # A supplied NVRAM snapshot may contain private values.
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(prepared)
    print(json.dumps({
        "board": "tomacco-vphone", "device_tree": str(args.output.resolve()),
        "input_sha256": hashlib.sha256(data).hexdigest(),
        "output_sha256": hashlib.sha256(prepared).hexdigest(),
        "memory_gib": args.memory_gib, "cpus": args.ap_cpus,
        "omitted_devices": ["avp-ctrr"] if args.omit_optional_ctrr else [],
        "omitted_firmware_properties": sorted(set(references) - values.keys() - generated),
        "generated_vm_properties": sorted(generated),
        "virtual_identity": identity_handoff(decode(prepared)),
        "supplied_firmware_properties": sorted(values),
        "nvram_source": "supplied-chrp-snapshot" if args.nvram else "fresh-chrp-bank",
        "persistent_nvram_controller": False,
        "sep_firmware_handoff": args.load_sep,
        "sep_handoff": args.sep_handoff if args.load_sep else None,
        "pcie_mmio": {"physical_base": "0x50000000", "size": "0x1fff0000"},
        "virtual_hardware": {"chip_id": "0xfe01", "board_id": "0x91"},
        "qualified_for_app_development": False,
    }, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, struct.error) as error:
        raise SystemExit(str(error)) from None
