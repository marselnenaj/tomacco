#!/usr/bin/env python3
"""Build a stand-in for a developer's own iOS app: an unsigned arm64 .app directory.

This host has no Darwin SDK, so a SwiftUI app cannot be built here. The owned
UIKit diagnostic (research-application.c) compiled under another bundle
identifier and name is the same kind of input a SwiftPM/xtool build produces:
an .app directory with an Info.plist and an arm64 Mach-O executable, which
the guest-tools build then stages, signs ad hoc and admits
(`build_ios_guest_tools.py --application`). The output is not signed.

--embedded-framework adds what an Xcode/SwiftPM app with dynamic
dependencies carries: Frameworks/<Name>Kit.framework (a framework bundle with
its own Info.plist and identifier) and Frameworks/lib<Name>Support.dylib, both
arm64 dylibs installed as @rpath/..., which the executable links (non-weak,
with an @executable_path/Frameworks run path) and calls from a constructor.
dyld refuses to start the app unless both load, so the app reaching its
window is the evidence that embedded code is signed and trusted.

--app-extension widget|share adds PlugIns/<Name>Widget.appex (or
<Name>Share.appex): an app extension bundle (package type XPC!, identifier
<bundle id>.widget or .share, an NSExtension dictionary for the system's
com.apple.widgetkit-extension or com.apple.share-services extension point)
whose arm64 executable is an owned stand-in: a WidgetKit or share extension
needs Swift and Apple's SDK, which this host lacks, so the executable only
hands control to Foundation's NSExtensionMain, the entry point of every
NSExtension. installd validates and registers the extension, and PlugInKit
lists it, without running it; what runs it (chronod asking a widget for its
descriptors, the share sheet) finds no widget or principal class and ends it.
"""
import argparse
import json
from pathlib import Path
import plistlib
import re
import subprocess

from build_ios_guest_tools import SOURCES, compiler_identity
from build_ios_research_bootstrap import application_info
from ios_application_bundle import BUNDLE_IDENTIFIER, EXECUTABLE_NAME, inspect_bundle
from ios_paravirt_firmware import read_component


# The embedded code of --embedded-framework: one function each, and a
# constructor in the app that binds and calls both before main.
KIT_SOURCE = "int tomacco_sample_kit_value(void) { return 42; }\n"
SUPPORT_SOURCE = "int tomacco_sample_support_value(void) { return 7; }\n"
CLIENT_SOURCE = """extern int tomacco_sample_kit_value(void);
extern int tomacco_sample_support_value(void);
int tomacco_sample_linked;
__attribute__((constructor)) static void tomacco_sample_link(void) {
    tomacco_sample_linked = tomacco_sample_kit_value() + tomacco_sample_support_value();
}
"""


# The stand-in executable of --app-extension: Foundation's NSExtensionMain,
# bound at run time (the build links only libSystem's stub), is what an
# extension's main runs; without it the process ends at once.
EXTENSION_SOURCE = """extern void *dlopen(const char *, int);
extern void *dlsym(void *, const char *);
extern int dprintf(int, const char *, ...);
int main(int argc, char **argv) {
    dprintf(2, "{\\"event\\":\\"sample-extension\\",\\"state\\":\\"started\\"}\\n");
    void *foundation = dlopen("/System/Library/Frameworks/Foundation.framework/Foundation", 2);
    int (*extension_main)(int, char **) = foundation ? (int (*)(int, char **))dlsym(foundation, "NSExtensionMain") : 0;
    if (!extension_main) return 1;
    return extension_main(argc, argv);
}
"""
# The extension points --app-extension builds for: the system's own hosts
# (chronod for widgets, the share sheet), so no host app is needed. A widget
# extension names no principal class (installd refuses one for its point); a
# share extension names one and the rule that offers it for every item.
APP_EXTENSIONS = {
    "widget": ("Widget", {"NSExtensionPointIdentifier": "com.apple.widgetkit-extension"}),
    "share": ("Share", {"NSExtensionPointIdentifier": "com.apple.share-services",
                        "NSExtensionPrincipalClass": "TomaccoSampleShareViewController",
                        "NSExtensionAttributes": {"NSExtensionActivationRule": "TRUEPREDICATE"}}),
}


def extension_info(minimum_ios, bundle_id, name, display_name, extension):
    """The Info.plist of an app extension, in the shape Xcode writes."""
    return {"CFBundleIdentifier": bundle_id, "CFBundleExecutable": name, "CFBundleName": name,
            "CFBundleDisplayName": display_name, "CFBundlePackageType": "XPC!", "CFBundleVersion": "1",
            "CFBundleShortVersionString": "1.0", "CFBundleInfoDictionaryVersion": "6.0",
            "CFBundleSupportedPlatforms": ["iPhoneOS"], "MinimumOSVersion": minimum_ios, "UIDeviceFamily": [1],
            "NSExtension": extension}


def framework_info(minimum_ios, bundle_id, name):
    """The Info.plist of an embedded framework, in the shape Xcode writes."""
    return {"CFBundleIdentifier": bundle_id, "CFBundleExecutable": name, "CFBundleName": name,
            "CFBundlePackageType": "FMWK", "CFBundleVersion": "1", "CFBundleShortVersionString": "1.0",
            "CFBundleSupportedPlatforms": ["iPhoneOS"], "MinimumOSVersion": minimum_ios}


def sample_info(minimum_ios, bundle_id, name, display_name):
    """The owned diagnostic's Info.plist under the sample's own identity."""
    info = application_info(minimum_ios)
    info.update(CFBundleIdentifier=bundle_id, CFBundleExecutable=name, CFBundleName=display_name)
    return info


def build(args):
    if not BUNDLE_IDENTIFIER.fullmatch(args.bundle_id) or args.bundle_id == application_info("27.0")["CFBundleIdentifier"]:
        raise ValueError("Choose a bundle identifier of the sample's own (letters, digits, '.', '-')")
    if not EXECUTABLE_NAME.fullmatch(args.name):
        raise ValueError("Invalid executable name")
    if not re.fullmatch(r"[0-9]{1,2}\.[0-9]{1,2}(?:\.[0-9]{1,2})?", args.minimum_ios):
        raise ValueError("Invalid minimum iOS version")
    output = args.output.expanduser().absolute()
    bundle = output / (args.name + ".app")
    if bundle.exists() or bundle.is_symlink():
        raise ValueError("Output bundle already exists; choose a new directory")
    compiler = compiler_identity(args.clang)
    linker = str(args.linker.expanduser().resolve(strict=True))
    bundle.mkdir(parents=True)
    (bundle / "Info.plist").write_bytes(plistlib.dumps(sample_info(args.minimum_ios, args.bundle_id, args.name,
                                                                   args.display_name or args.name)))
    compile_flags = ["--target=arm64-apple-ios" + args.minimum_ios, "-O2", "-Wall", "-Wextra", "-Werror"]
    libraries = []
    if getattr(args, "embedded_framework", False):
        kit, support = args.name + "Kit", "lib" + args.name + "Support.dylib"
        kit_id = args.bundle_id + ".kit"
        if not EXECUTABLE_NAME.fullmatch(kit) or not BUNDLE_IDENTIFIER.fullmatch(kit_id):
            raise ValueError("The sample's name or bundle identifier is too long for its framework")
        framework = bundle / "Frameworks" / (kit + ".framework")
        framework.mkdir(parents=True)
        (framework / "Info.plist").write_bytes(plistlib.dumps(framework_info(args.minimum_ios, kit_id, kit)))
        for source, text, target, install in (("sample-kit.c", KIT_SOURCE, framework / kit,
                                               f"@rpath/{kit}.framework/{kit}"),
                                              ("sample-support.c", SUPPORT_SOURCE, bundle / "Frameworks" / support,
                                               "@rpath/" + support)):
            (output / source).write_text(text)
            obj = output / (source + ".o")
            subprocess.run([str(args.clang), *compile_flags, "-c", str(output / source), "-o", str(obj)],
                           check=True, timeout=120)
            subprocess.run([linker, "-arch", "arm64", "-platform_version", "ios", args.minimum_ios, args.minimum_ios,
                            "-dylib", "-install_name", install, "-o", str(target),
                            str(SOURCES / "research-libSystem.tbd"), str(obj)], check=True, timeout=120)
            target.chmod(0o755)
            libraries.append(str(target))
    extension = getattr(args, "app_extension", None)
    if extension:
        kind, dictionary = APP_EXTENSIONS[extension]
        name, identifier = args.name + kind, args.bundle_id + "." + extension
        if not EXECUTABLE_NAME.fullmatch(name) or not BUNDLE_IDENTIFIER.fullmatch(identifier):
            raise ValueError("The sample's name or bundle identifier is too long for its extension")
        appex = bundle / "PlugIns" / (name + ".appex")
        appex.mkdir(parents=True)
        (appex / "Info.plist").write_bytes(plistlib.dumps(extension_info(
            args.minimum_ios, identifier, name, (args.display_name or args.name) + " " + kind, dictionary)))
        (output / "sample-extension.c").write_text(EXTENSION_SOURCE)
        subprocess.run([str(args.clang), *compile_flags, "-c", str(output / "sample-extension.c"),
                        "-o", str(output / "sample-extension.c.o")], check=True, timeout=120)
        subprocess.run([linker, "-arch", "arm64", "-platform_version", "ios", args.minimum_ios, args.minimum_ios,
                        "-dead_strip", "-o", str(appex / name), str(SOURCES / "research-libSystem.tbd"),
                        str(output / "sample-extension.c.o")], check=True, timeout=120)
        (appex / name).chmod(0o755)
    objects = []
    if libraries:
        (output / "sample-client.c").write_text(CLIENT_SOURCE)
        subprocess.run([str(args.clang), *compile_flags, "-c", str(output / "sample-client.c"),
                        "-o", str(output / "sample-client.c.o")], check=True, timeout=120)
        objects.append(str(output / "sample-client.c.o"))
    for source in ("research-application.c", "research-application-trace.c"):
        data = read_component(SOURCES, source, 1 << 20)
        (output / source).write_bytes(data)
        obj = output / (source + ".o")
        subprocess.run([str(args.clang), "--target=arm64-apple-ios" + args.minimum_ios, "-O2", "-Wall", "-Wextra",
                        "-Werror", "-DTOMACCO_APPLICATION_FILE_LOG=1",
                        "-DTOMACCO_APPLICATION_IDENTIFIER=" + json.dumps(args.bundle_id),
                        "-fblocks", "-c", str(output / source), "-o", str(obj)], check=True, timeout=120)
        objects.append(str(obj))
    subprocess.run([linker, "-arch", "arm64", "-platform_version", "ios", args.minimum_ios, args.minimum_ios,
                    "-dead_strip", *(["-rpath", "@executable_path/Frameworks"] if libraries else []),
                    "-o", str(bundle / args.name), str(SOURCES / "research-libSystem.tbd"),
                    *objects, *libraries], check=True, timeout=120)
    (bundle / args.name).chmod(0o755)
    inspected = inspect_bundle(bundle)
    return {"bundle": str(bundle), "bundle_id": inspected["bundle_id"], "executable": inspected["executable"],
            "content_sha256": inspected["content_sha256"], "bytes": inspected["bytes"],
            "entitlements": inspected["entitlements"], "signed": False, "compiler": compiler,
            "nested_code": inspected["nested_code"],
            "stand_in_for": "a developer's arm64 iOS app bundle; built from the owned diagnostic's sources"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Directory that receives <name>.app")
    parser.add_argument("--linker", type=Path, required=True)
    parser.add_argument("--clang", default="clang")
    parser.add_argument("--minimum-ios", default="27.0")
    parser.add_argument("--name", default="TomaccoSample", help="Executable and bundle name")
    parser.add_argument("--display-name", default="Tomacco Sample")
    parser.add_argument("--bundle-id", default="io.github.marselnenaj.tomacco.sample-app")
    parser.add_argument("--embedded-framework", action="store_true",
                        help="Also build Frameworks/<name>Kit.framework and Frameworks/lib<name>Support.dylib, "
                             "which the executable links and calls before main")
    parser.add_argument("--app-extension", choices=sorted(APP_EXTENSIONS),
                        help="Also build an app extension for a system extension point: PlugIns/<name>Widget.appex "
                             "(com.apple.widgetkit-extension) or PlugIns/<name>Share.appex (com.apple.share-services), "
                             "with an owned stand-in executable that runs NSExtensionMain")
    print(json.dumps(build(parser.parse_args()), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
