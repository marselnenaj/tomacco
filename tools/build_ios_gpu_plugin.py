#!/usr/bin/env python3
"""Cross-build vphone's open-source GPU compiler plugin on Linux, without an SDK.

Uses the exact upstream C++ implementation with small declaration-only linker
interfaces. LLVM/GPUCompiler calls resolve against the actual guest libraries;
no compiler result is synthesized. Building is not guest execution or graphics
qualification. The output is separate from the original Apple driver bundle.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import struct
import subprocess

from ios_paravirt_firmware import read_component
from ios_graphics_location import (BUNDLE_PATH, COMPILER_LIBRARY_NAME,
                                   PLUGIN_PERSONALITY, bridge_path_header)

UPSTREAM = "https://github.com/Lakr233/vphone-cli"
REVISION = "3a819b0dd00aea8906ddd8d33c9364736a869ca0"
SOURCE = "VPhoneGuestComponents/GraphicLoader/main.mm"
SOURCE_SHA256 = "26d05511a39068e9e5c7be29f3d0c02d1d00104778746e2708693e6a53fad1c4"
NAME = COMPILER_LIBRARY_NAME
BRIDGE_NAME = "p"
BRIDGE_SOURCE = "ios-gpu-bridge.c"
BRIDGE_SOURCE_SHA256 = "68cb7f063985249e3741008cb391c5c97ed8d69d0731ae06d7b2f21390791e9b"
BRIDGE_LOG_HEADER = "guest-buffer-log.h"
BRIDGE_LOG_HEADER_SHA256 = "5ae30ce2814f94e33a5551a76a0a532d783ef1890be874f9a34894e4a5f48629"
BRIDGE_SURFACE_HEADERS = {
    "guest-surface-log.h": "fdc9b77c2fe1e6dc57838b631fddefc951a8de6e9c4f75b0b2081438f94d1f35",
    "ios-surface-observer.h": "d360d74aee9b6c78b4ddf128c1848cc4ae25f21b527a9dbaf2a65e7179c4388c",
}
BRIDGE_SURFACE_HEADER_PROFILES = {BRIDGE_SOURCE_SHA256: BRIDGE_SURFACE_HEADERS}
METAL_UUID = "15BB7854-D1F3-33DA-8121-C3CC4C472D1D"
GRAPHICS_DRIVER_UUID = "7E55CCEA-9621-3AF3-9396-E285AB7DEE23"
INSTALL_NAME = "/System/Library/Extensions/AppleParavirtGPUMetalIOGPUFamily.bundle/" + NAME
EXPORTS = {"_MTLCompilerCreate", "_MTLCompilerBuildRequestWithOptions", "_MTLCompilerDelete", "_MTLCompilerReleaseReply"}
LOOKUP = ("LLVMDisposeMemoryBuffer", "LLVMGetBufferStart", "LLVMGetBufferSize", "LLVMGetTarget",
          "LLVMExtraMakeSharedModule", "LLVMExtraDisposeSharedModule", "LLVMGetNamedMetadataNumOperands",
          "LLVMGetNamedMetadataOperands", "LLVMGetMDNodeNumOperands", "LLVMGetMDNodeOperands",
          "LLVMGetValueName2", "MTLGPUCompilerCreate", "MTLGPUCompilerDestroy", "MTLDowngradeAIRModule",
          "MTLMetalLibCreateExecutableWithTriple", "MTLMetalFunctionCreate", "MTLMetalLibInsertFunction",
          "MTLWriteMetalLibToMemoryBuffer")
HEADERS = {
    "cstdint": "typedef __UINT64_TYPE__ uint64_t;\n",
    "cstdlib": """typedef __SIZE_TYPE__ size_t;
extern "C" void *malloc(size_t);
extern "C" void free(void *);
#define alloca(size) __builtin_alloca(size)
""",
    "cstring": """typedef __SIZE_TYPE__ size_t;
extern "C" void *memcpy(void *, const void *, size_t);
""",
}


def tbd(install_name, symbols):
    return ("--- !tapi-tbd\ntbd-version: 4\ntargets: [ arm64-ios, arm64e-ios ]\n"
            f"install-name: '{install_name}'\nexports:\n"
            "  - targets: [ arm64-ios, arm64e-ios ]\n"
            "    symbols: [ " + ", ".join(symbols) + " ]\n...\n")


def save(output, record):
    temporary = output / ".plugin.json.tmp"
    with temporary.open("w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(record, stream, indent=2)
        stream.write("\n")
    os.replace(temporary, output / "plugin.json")


def run(command, output, record):
    command = list(map(str, command))
    record["commands"].append(command)
    save(output, record)
    with (output / "build.log").open("ab") as log:
        log.write(("\n" + json.dumps(command) + "\n").encode())
        log.flush()
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=90)
    if result.returncode:
        raise RuntimeError(f"{Path(command[0]).name} exited {result.returncode}; inspect {output / 'build.log'}")


def inspect_plugin(data, *, bundle=False):
    """Check the ARM64e ABI and the actual ad-hoc CodeDirectory/page hashes.

    A successful `ldid -S` alone is insufficient: its default flags are zero,
    which sends TXM through the CMS path despite there being no CMS signature.
    These checks establish local code integrity, not Apple authentication.
    """
    return inspect_signed_code(data, cpu_subtype=2, file_type=8 if bundle else 6)


def guest_entitlements_der(value):
    """Canonical ldid DER for the deliberately small owned capability vocabulary."""
    def encoded(tag, body):
        size = len(body)
        length = size.to_bytes(max(1, (size.bit_length() + 7) // 8), "big")
        return bytes([tag]) + (length if size < 128 else bytes([128 + len(length)]) + length) + body
    if type(value) is bool:
        return encoded(1, bytes([int(value)]))
    if isinstance(value, str):
        return encoded(12, value.encode("utf-8"))
    if isinstance(value, list):
        return encoded(48, b"".join(guest_entitlements_der(item) for item in value))
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return encoded(49, b"".join(sorted(encoded(48, guest_entitlements_der(key) +
            guest_entitlements_der(item)) for key, item in value.items())))
    raise ValueError("Unsupported owned guest capability type")


def inspect_signed_code(data, *, cpu_subtype, file_type, expected_entitlements=None, sealed_files=None, expected_identifier=None):
    """Verify native Mach-O structure, ad-hoc CodeDirectory and every code page."""
    if (cpu_subtype, file_type) not in ((0, 6), (2, 6), (2, 8), (0, 2), (2, 2)):
        raise ValueError("Unsupported native code ABI")
    if len(data) < 32 or len(data) > 16 << 20:
        raise ValueError("Invalid GPU plugin size")
    magic, cpu, subtype, kind, commands, command_bytes = struct.unpack_from("<6I", data)
    if (magic, cpu, subtype & 0xffffff, kind) != (0xfeedfacf, 0x100000c, cpu_subtype, file_type):
        raise ValueError("Native code has an unexpected CPU subtype or Mach-O file type: "
                         f"expected ARM64 subtype {cpu_subtype:#x}, type {file_type}; "
                         f"got magic {magic:#x}, CPU {cpu:#x}, subtype {subtype:#x}, type {kind}. "
                         "Use a linker that supports the requested ABI, including authenticated pointers for ARM64e")
    if not commands or commands > 4096 or command_bytes > len(data) - 32:
        raise ValueError("Invalid GPU plugin load commands")
    offset, signature = 32, None
    for _ in range(commands):
        if offset + 8 > 32 + command_bytes:
            raise ValueError("Truncated GPU plugin load command")
        command, size = struct.unpack_from("<II", data, offset)
        if size < 8 or size % 8 or offset + size > 32 + command_bytes:
            raise ValueError("Invalid GPU plugin load command size")
        if command == 0x1d:  # LC_CODE_SIGNATURE
            if size != 16 or signature is not None:
                raise ValueError("Ambiguous GPU plugin code signature")
            signature = struct.unpack_from("<II", data, offset + 8)
        offset += size
    if offset != 32 + command_bytes or signature is None:
        raise ValueError("GPU plugin has no complete code signature")
    start, length = signature
    if start < offset or length < 20 or start + length > len(data):
        raise ValueError("Invalid GPU plugin signature bounds")
    magic, total, count = struct.unpack_from(">III", data, start)
    if magic != 0xfade0cc0 or not 1 <= count <= 64 or not 12 + count * 8 <= total <= length:
        raise ValueError("Invalid GPU plugin signature container")
    primary = None
    entitlement_blobs = {}
    for index in range(count):
        slot, relative = struct.unpack_from(">II", data, start + 12 + index * 8)
        if relative < 12 + count * 8 or relative + 8 > total:
            raise ValueError("Invalid GPU plugin signature slot")
        blob_magic, blob_size = struct.unpack_from(">II", data, start + relative)
        if blob_size < 8 or relative + blob_size > total:
            raise ValueError("Invalid GPU plugin signature blob")
        if slot in (5, 7):
            if slot in entitlement_blobs or blob_magic != {5: 0xfade7171, 7: 0xfade7172}[slot]:
                raise ValueError("Invalid native entitlement signature slot")
            entitlement_blobs[slot] = data[start + relative:start + relative + blob_size]
        if slot == 0:
            if primary is not None or blob_magic != 0xfade0c02 or blob_size < 44:
                raise ValueError("Invalid GPU plugin CodeDirectory")
            primary = data[start + relative:start + relative + blob_size]
    if primary is None:
        raise ValueError("GPU plugin is missing its CodeDirectory")
    flags, hashes, identifier, special, slots, limit = struct.unpack_from(">6I", primary, 12)
    if expected_identifier is not None:
        if not isinstance(expected_identifier, str) or not 44 <= identifier < len(primary):
            raise ValueError("Invalid native signing identifier")
        end = primary.find(b"\0", identifier, min(len(primary), identifier + 256))
        if end < 0 or primary[identifier:end] != expected_identifier.encode("utf-8"):
            raise ValueError("Native signing identifier differs from bundle identity")
    hash_size, hash_type, _, page_shift = struct.unpack_from("4B", primary, 36)
    if not flags & 2:
        raise ValueError("GPU plugin CodeDirectory must set CS_ADHOC")
    if (hash_size != 32 or hash_type != 2 or page_shift not in (12, 14)
            or limit != start or not 44 <= identifier < hashes
            or b"\0" not in primary[identifier:hashes]
            or hashes < 44 + special * hash_size or hashes + slots * hash_size > len(primary)
            or slots != (limit + (1 << page_shift) - 1) >> page_shift):
        raise ValueError("Unsupported GPU plugin CodeDirectory layout")
    for index in range(slots):
        page = data[index << page_shift:min((index + 1) << page_shift, limit)]
        if hashlib.sha256(page).digest() != primary[hashes + index * 32:hashes + (index + 1) * 32]:
            raise ValueError("GPU plugin signed code page changed")
    if expected_entitlements is not None:
        for slot in (5, 7):
            blob = entitlement_blobs.get(slot)
            recorded = primary[hashes - slot * 32:hashes - (slot - 1) * 32] if special >= slot else bytes(32)
            if recorded != (hashlib.sha256(blob).digest() if blob else bytes(32)):
                raise ValueError("Native signed entitlements changed")
        xml = entitlement_blobs.get(5)
        actual = plistlib.loads(xml[8:]) if xml else {}
        der = entitlement_blobs.get(7)
        if (actual != expected_entitlements or (der and not xml) or
                (der and der[8:] != guest_entitlements_der(expected_entitlements))):
            raise ValueError("Native entitlements differ from the allowed guest capabilities")
    if sealed_files is not None:
        if set(sealed_files) != {1, 3}:
            raise ValueError("Expected the complete bundle Info.plist and CodeResources seals")
        for slot, content in sealed_files.items():
            if special < slot or primary[hashes - slot * 32:hashes - (slot - 1) * 32] != hashlib.sha256(content).digest():
                raise ValueError("Native bundle seal changed or is missing")
    digest = hashlib.sha256(primary).hexdigest()
    record = {"flags": flags, "cdhash": digest[:40], "code_directory_sha256": digest,
              "code_pages": slots, "code_pages_verified": True, "apple_authenticated": False}
    if sealed_files is not None:
        record["bundle_seals_verified"] = [1, 3]
    return record


def inspect_bridge(root, record):
    """Recheck the source and output of the locally built compatibility module."""
    if (not isinstance(record, dict) or record.get("source_sha256") != BRIDGE_SOURCE_SHA256
            or record.get("metal_uuid") != METAL_UUID or record.get("llvm_version") != 32023
            or record.get("graphics_driver_uuid") != GRAPHICS_DRIVER_UUID
            or record.get("memoryless_storage") != "private-backing-logical-memoryless"
            or record.get("graphics_bundle_path") != BUNDLE_PATH
            or record.get("graphics_plugin_personality") != PLUGIN_PERSONALITY):
        raise ValueError("GPU bridge requires the pinned iOS 27 Metal profile")
    source = read_component(root, BRIDGE_SOURCE, 64 << 10)
    header = read_component(root, "ios-gpu-path.h", 4 << 10)
    log_header = read_component(root, BRIDGE_LOG_HEADER, 8 << 10)
    if (hashlib.sha256(log_header).hexdigest() != BRIDGE_LOG_HEADER_SHA256 or
            record.get("log_header_sha256") != BRIDGE_LOG_HEADER_SHA256):
        raise ValueError("GPU bridge diagnostic header differs from its build")
    surface_headers = BRIDGE_SURFACE_HEADER_PROFILES.get(record["source_sha256"])
    if surface_headers is not None:
        if record.get("surface_header_sha256") != surface_headers:
            raise ValueError("GPU bridge IOSurface header inventory differs from its build")
        for name, digest in surface_headers.items():
            if hashlib.sha256(read_component(root, name, 32 << 10)).hexdigest() != digest:
                raise ValueError("GPU bridge IOSurface header differs from its build")
    if (header != bridge_path_header() or
            hashlib.sha256(header).hexdigest() != record.get("path_header_sha256")):
        raise ValueError("GPU bridge path contract differs from its build")
    data = read_component(root, BRIDGE_NAME, 1 << 20)
    if (hashlib.sha256(source).hexdigest() != record["source_sha256"]
            or hashlib.sha256(data).hexdigest() != record.get("sha256") or len(data) != record.get("bytes")):
        raise ValueError("GPU bridge changed after its build")
    integrity = inspect_plugin(data)
    if integrity != record.get("integrity"):
        raise ValueError("GPU bridge CodeDirectory differs from its build")
    return data, source, header


def build_bridge(args, output, record, include, target):
    root = Path(__file__).resolve().parent.parent / "integrations/ios-emulator"
    source = read_component(root, BRIDGE_SOURCE, 64 << 10)
    if hashlib.sha256(source).hexdigest() != BRIDGE_SOURCE_SHA256:
        raise ValueError("GPU compatibility source differs from the pinned implementation")
    (output / BRIDGE_SOURCE).write_bytes(source)
    log_header = read_component(root, BRIDGE_LOG_HEADER, 8 << 10)
    if hashlib.sha256(log_header).hexdigest() != BRIDGE_LOG_HEADER_SHA256:
        raise ValueError("GPU diagnostic header differs from the pinned implementation")
    (output / BRIDGE_LOG_HEADER).write_bytes(log_header)
    for name, digest in BRIDGE_SURFACE_HEADERS.items():
        contents = read_component(root, name, 32 << 10)
        if hashlib.sha256(contents).hexdigest() != digest:
            raise ValueError("GPU IOSurface observer header differs from the pinned implementation")
        (output / name).write_bytes(contents)
    header = bridge_path_header()
    (output / "ios-gpu-path.h").write_bytes(header)
    (include / "libobjc.tbd").write_text(tbd("/usr/lib/libobjc.A.dylib", (
        "_objc_getClass", "_sel_registerName", "_class_getInstanceMethod",
        "_method_getImplementation", "_method_setImplementation", "_objc_msgSend",
        "_class_addMethod", "_method_getTypeEncoding", "_objc_allocateClassPair",
        "_objc_registerClassPair", "_objc_disposeClassPair", "_object_getClass", "_object_setClass", "_object_getClassName")))
    # These are declarations, not an Apple SDK or replacement implementations.
    (include / "libSystem.tbd").write_text(tbd("/usr/lib/libSystem.B.dylib", (
        "___stack_chk_fail", "___stack_chk_guard", "dyld_stub_binder", "__dyld_image_count",
        "__dyld_get_image_header", "__dyld_get_image_name", "__dyld_get_image_vmaddr_slide",
        "_getprogname", "_getuid", "_geteuid", "_getpid", "_proc_pidpath", "_bzero", "_pthread_threadid_np", "_pthread_getspecific", "_open", "_openat", "_close", "_lseek",
        "_write", "_snprintf", "___error", "_fstat", "_fchmod", "_flock")))
    (include / "libIOSurface.tbd").write_text(tbd(
        "/System/Library/Frameworks/IOSurface.framework/IOSurface", (
            "_IOSurfaceGetPixelFormat", "_IOSurfaceGetID", "_IOSurfaceLookup", "_IOSurfaceGetPlaneCount", "_IOSurfaceGetWidth",
            "_IOSurfaceGetHeight", "_IOSurfaceGetAllocSize", "_IOSurfaceGetBytesPerRowOfPlane",
            "_IOSurfaceGetBytesPerElementOfPlane", "_IOSurfaceGetNumberOfComponentsOfPlane",
            "_IOSurfaceGetNameOfComponentOfPlane", "_IOSurfaceGetTypeOfComponentOfPlane",
            "_IOSurfaceGetRangeOfComponentOfPlane", "_IOSurfaceGetBitDepthOfComponentOfPlane",
            "_IOSurfaceGetBitOffsetOfComponentOfPlane")))
    (include / "libCoreFoundation.tbd").write_text(tbd(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation", ("_CFRelease",)))
    obj, library = output / "bridge.o", output / BRIDGE_NAME
    run([args.clang, "--target=" + target, "-x", "c", "-O2", "-nostdinc", "-fvisibility=hidden",
         "-Wall", "-Wextra", "-Werror", "-c", output / BRIDGE_SOURCE, "-o", obj], output, record)
    run([args.linker, "-arch", "arm64e", "-platform_version", "ios", "26.1", "26.1", "-dylib",
         "-install_name", "@loader_path/" + BRIDGE_NAME, "-L" + str(include), "-lSystem", "-lobjc", "-lIOSurface", "-lCoreFoundation",
         "-o", library, obj], output, record)
    run([args.ldid, "-Cadhoc", "-S", library], output, record)
    data = library.read_bytes()
    return {"path": str(library), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
            "source_sha256": BRIDGE_SOURCE_SHA256, "metal_uuid": METAL_UUID, "llvm_version": 32023,
            "graphics_driver_uuid": GRAPHICS_DRIVER_UUID, "memoryless_storage": "private-backing-logical-memoryless",
            "integrity": inspect_plugin(data), "original_metal_in_process_compiler": True,
            "graphics_bundle_path": BUNDLE_PATH, "graphics_plugin_personality": PLUGIN_PERSONALITY,
            "path_header_sha256": hashlib.sha256(header).hexdigest(),
            "log_header_sha256": BRIDGE_LOG_HEADER_SHA256,
            "surface_header_sha256": BRIDGE_SURFACE_HEADERS,
            "surface_texture_observer_revision": "native-surface-v4",
            "shared_texture_observer_api": "newSharedTextureWithHandle:allocator:",
            "shared_texture_observer_retained_lookup": True,
            "shared_texture_observer_lookup_limit_per_process": 32,
            "shared_reference_is_texture_object": False,
            "surface_texture_observer_uid_policy": "native-real-and-effective-uid-equal-excluding-invalid-uid",
            "surface_texture_observer_call_limit_per_process": 4,
            "surface_texture_observer_event_limit_per_process": 8,
            "surface_texture_observer_file_limit_bytes_per_uid": 131072,
            "surface_component_enum_values_decoded": False,
            "buffer_texture_observer_uid": 276, "buffer_texture_observer_identity": "/System/Library/CoreServices/iconservicesagent", "buffer_texture_log_candidate": "/private/var/tmp",
            "guest_execution_verified": False, "apple_authenticated": False}


def build(args):
    output = Path(args.output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Choose a new GPU plugin output directory")
    output.mkdir(mode=0o700, parents=True)
    record = {"format": 1, "scope": "ios-paravirtual-gpu-compiler-plugin", "state": "building",
              "upstream": UPSTREAM, "revision": REVISION, "source_sha256": SOURCE_SHA256,
              "apple_sdk_used": False, "guest_execution_verified": False,
              "qualified_for_app_development": False, "commands": []}
    save(output, record)
    try:
        source = read_component(args.source, SOURCE, 256 << 10)
        if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
            raise ValueError("GPU plugin source differs from the pinned upstream implementation")
        license_text = read_component(args.source, "LICENSE", 64 << 10)
        (output / "UPSTREAM-LICENSE").write_bytes(license_text)
        (output / "main.mm").write_bytes(source)
        include = output / "include"
        include.mkdir(mode=0o700)
        for name, contents in HEADERS.items():
            (include / name).write_text(contents)
        (include / "libSystem.tbd").write_text(tbd("/usr/lib/libSystem.B.dylib",
            ("_malloc", "_free", "_memcpy", "___stack_chk_fail", "___stack_chk_guard", "dyld_stub_binder")))
        (include / "libc++.tbd").write_text(tbd("/usr/lib/libc++.1.dylib", ("__Znwm", "__ZdlPv", "__ZdlPvm")))
        for name in ("clang", "linker", "nm", "ldid"):
            executable = shutil.which(str(getattr(args, name)))
            if executable is None:
                raise ValueError(f"Missing build tool: {name}")
            setattr(args, name, str(Path(executable).resolve(strict=True)))
        record["tools"] = {name: {"path": getattr(args, name)} for name in ("clang", "linker", "nm", "ldid")}
        target = "arm64e-apple-ios26.1"
        object_file, library = output / "plugin.o", output / NAME
        run([args.clang, "--target=" + target, "-x", "c++", "-std=c++17", "-O2", "-nostdinc",
             "-nostdinc++", "-I", include, "-fno-exceptions", "-fno-rtti", "-fvisibility=hidden",
             "-c", output / "main.mm", "-o", object_file], output, record)
        command = [args.linker, "-arch", "arm64e", "-platform_version", "ios", "26.1", "26.1",
                   "-dylib", "-install_name", INSTALL_NAME, "-L" + str(include), "-lSystem", "-lc++",
                   "-o", library, object_file]
        for symbol in LOOKUP:
            command.extend(["-U", "_" + symbol])
        run(command, output, record)
        run([args.ldid, "-Cadhoc", "-S", library], output, record)
        data = library.read_bytes()
        integrity = inspect_plugin(data)
        symbols = subprocess.check_output([args.nm, "--defined-only", "--extern-only", "--just-symbol-name", library],
                                          timeout=30, text=True).splitlines()
        if set(symbols) != EXPORTS:
            raise ValueError("GPU plugin has an unexpected exported ABI")
        imports = subprocess.check_output([args.nm, "--undefined-only", "--just-symbol-name", library],
                                          timeout=30, text=True).splitlines()
        record.update(state="built", target=target,
                      plugin={"path": str(library), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                              "architecture": "arm64e", "exports": sorted(symbols), "imports": sorted(imports),
                              "signing": "ad-hoc", "integrity": integrity, "apple_authenticated": False})
        record["state"] = "building"
        record["bridge"] = build_bridge(args, output, record, include, target)
        record["state"] = "built"
        save(output, record)
        return record
    except BaseException as error:
        record.update(state="cancelled" if isinstance(error, KeyboardInterrupt) else "failed", error=str(error))
        save(output, record)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Local vphone-cli checkout with the pinned GraphicLoader source")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--clang", default="clang++")
    parser.add_argument("--linker", required=True, help="ARM64e-capable ld64, including authenticated-pointer relocations")
    parser.add_argument("--nm", default="llvm-nm")
    parser.add_argument("--ldid", required=True)
    args = parser.parse_args()
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt("GPU plugin build cancelled")
    signal.signal(signal.SIGTERM, interrupted)
    try:
        result = build(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps({"state": result["state"], "plugin": result["plugin"]}, indent=2))


if __name__ == "__main__":
    main()
