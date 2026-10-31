"""Select imported iPhone OS components while preserving the PCC boot identity.

This assembles restore inputs, not signatures. The original manifests remain
unchanged and the restore client still verifies the complete system image.
"""
import copy
import hashlib
import json
from pathlib import Path
import plistlib

from ios_paravirt_firmware import read_component
from prepare_ios_userland import relative_path

OS_COMPONENTS = ("OS", "StaticTrustCache", "SystemVolume", "Ap,SystemVolumeCanonicalMetadata")
IPHONE_BUILD_SHA256 = "1d3df8a49cf134cbf3b9f46c67be5cfc000c6bc2fda014846784fa0a8993b461"


def attach_userland(bundle, directory):
    root = Path(directory).resolve(strict=True)
    receipt_data = read_component(root, "userland.json", 1 << 20)
    record = json.loads(receipt_data)
    if (not isinstance(record, dict) or record.get("format") != 1
            or record.get("scope") != "original-ios-userland-assets"
            or record.get("state") != "prepared"
            or not isinstance(record.get("ios"), dict)
            or not isinstance(record.get("pcc"), dict)
            or not isinstance(record.get("components"), dict)):
        raise ValueError("Select a completed original iOS userland import")
    pcc_data = read_component(root, "pcc-BuildManifest.plist", 16 << 20)
    pcc_sha = hashlib.sha256(pcc_data).hexdigest()
    if (pcc_sha != bundle.version["build_manifest_sha256"]
            or record["pcc"].get("build_manifest_sha256") != pcc_sha
            or record["pcc"].get("identity_index") != bundle.version["identity_index"]):
        raise ValueError("Imported userland belongs to a different PCC boot identity")
    data = read_component(root, "ios-BuildManifest.plist", 16 << 20)
    phone_sha = hashlib.sha256(data).hexdigest()
    manifest = plistlib.loads(data)
    identities = manifest.get("BuildIdentities") if isinstance(manifest, dict) else None
    index = record["ios"].get("identity_index")
    if (phone_sha != record["ios"].get("build_manifest_sha256")
            or not isinstance(identities, list) or type(index) is not int
            or not 0 <= index < len(identities) or not isinstance(identities[index], dict)):
        raise ValueError("Imported iPhone BuildManifest no longer matches its receipt")
    phone = identities[index]
    info, components = phone.get("Info"), phone.get("Manifest")
    if (not isinstance(info, dict) or info.get("Variant") != "Customer Erase Install (IPSW)"
            or not isinstance(components, dict)
            or info.get("DeviceClass") != record["ios"].get("board")):
        raise ValueError("Imported iPhone erase identity does not match its receipt")
    if set(bundle.overrides).intersection(OS_COMPONENTS):
        raise ValueError("Imported iPhone OS components cannot be replaced by firmware overrides")
    sources = {}
    for name in OS_COMPONENTS:
        entry, imported = components.get(name), record["components"].get(name)
        if (not isinstance(entry, dict) or not isinstance(entry.get("Info"), dict)
                or not isinstance(entry.get("Digest"), bytes) or len(entry["Digest"]) != 48
                or not isinstance(imported, dict)):
            raise ValueError(f"Missing imported iPhone component: {name}")
        original_path = relative_path(entry["Info"].get("Path"))
        relative = "images/system.dmg" if name == "OS" else str(Path("components") / original_path)
        expected = entry["Digest"].hex()
        if imported.get("sha384" if name == "OS" else "manifest_sha384") != expected:
            raise ValueError(f"Imported {name} does not match its original manifest")
        sources[name] = (root, relative)
    # Commit the selection only after the entire metadata contract passes.
    identity = copy.deepcopy(bundle.identity)
    for name in OS_COMPONENTS:
        identity["Manifest"][name] = copy.deepcopy(components[name])
    bundle.identity = identity
    bundle.components = identity["Manifest"]
    bundle.userland_sources = sources
    # source_component rechecks actual metadata bytes, through no-follow reads.
    for name in OS_COMPONENTS[1:]:
        data, _normalized, source = bundle.source_component(name)
        imported = record["components"][name]
        if imported.get("sha256") != source["source_sha256"] or imported.get("bytes") != len(data):
            raise ValueError(f"Imported {name} changed after preparation")
    bundle.version["ios_userland"] = {
        "directory": str(root), "receipt_sha256": hashlib.sha256(receipt_data).hexdigest(),
        "build_manifest_sha256": phone_sha, "identity_index": index,
        "product_version": manifest.get("ProductVersion"),
        "build_version": manifest.get("ProductBuildVersion"), "board": info["DeviceClass"],
        "replaced_components": list(OS_COMPONENTS), "pcc_boot_components_preserved": True,
        "signature_verified": False, "cryptexes_installed": False,
    }
    return root / "images/system.dmg"
