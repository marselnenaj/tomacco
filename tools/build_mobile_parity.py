#!/usr/bin/env python3
"""The phone parity matrix: every operation of assets/operations.json, and
whether it runs on the phone itself (Omarchy Mobile, Linux aarch64), only on an
explicitly chosen remote host, conditionally, or not at all.

    python3 tools/build_mobile_parity.py            # writes docs/mobile-parity.json and .md
    python3 tools/build_mobile_parity.py --check    # exits 1 when the files are stale

The matrix is a claim about where work *can* run, with the requirement and the
next test for each row. It is not a test result: "local" means the operation's
code path has no x86_64, KVM, Apple-hardware or desktop-only dependency, not
that it was run on a Pixel. Rows marked unverified stay so until the device
qualification step is done.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPERATIONS = ROOT / "assets" / "operations.json"
JSON_OUT = ROOT / "docs" / "mobile-parity.json"
MD_OUT = ROOT / "docs" / "mobile-parity.md"

STATUSES = ("local", "remote", "conditional", "unavailable")

# Ordered rules: the first matching prefix (or exact name) wins. Each rule gives
# the phone status, what it needs, why, and what proves it next.
RULES: list[tuple[tuple[str, ...], dict]] = [
    (("android.avd_start", "android.emulator", "android.avd_usage"), {
        "phone": "remote", "requires": ["KVM", "host with Android emulator"],
        "evidence": "The Android emulator needs KVM; Omarchy Mobile on the Pixel exposes none (doctor reports kvm readiness). AVD files can still be created and managed locally.",
        "next_test": "doctor on the device: kvm=false; then remote.run android.avd_start against a chosen host."}),
    (("android.avd_", "android.avds", "android.sdk_packages", "android.sdk_install"), {
        "phone": "local", "requires": ["Android cmdline-tools for linux (arm64 JDK)", "explicit download consent"],
        "evidence": "sdkmanager and avdmanager are Java tools; the SDK's platform-tools ship linux-aarch64 builds. Downloads are large and ask first.",
        "next_test": "android.sdk_packages on the device with TOMACCO_HOME in a temp dir; verify arm64 platform-tools are picked."}),
    (("android.environment.", "android."), {
        "phone": "local", "requires": ["adb (arm64)", "USB OTG or Wi-Fi pairing to the target phone"],
        "evidence": "Every android.* operation talks to a device over adb; adb has linux-aarch64 builds. Mirroring (android.mirror) decodes H.264 in software through the LGPL FFmpeg build.",
        "next_test": "device.list and android.screen with a paired phone; measure mirror frame rate on the Pixel."}),
    (("ios.emulator.", "ios.virtual."), {
        "phone": "remote", "requires": ["x86_64 host with KVM", "the person's own iOS image and SDK"],
        "evidence": "The virtual iPhone is an x86_64 research runtime (tools/ios_*; packaging builds it for x86_64 only). Experimental, not shipped for aarch64.",
        "next_test": "remote.configure a host, then ios.virtual.status through remote.run; the phone shows the host's state read-only until connected."}),
    (("ios.sdk_install", "ios.toolkit_install", "ios.flutter_setup", "ios.flutter_build", "ios.store_ship", "ios.signing"), {
        "phone": "remote", "requires": ["xtool and an Apple SDK on a host", "Apple account for signing"],
        "evidence": "xtool's Swift-for-iOS toolchain is published for x86_64 Linux; no aarch64 Linux build is qualified here. Signing needs Apple credentials entered on the host.",
        "next_test": "swift.status on a host; remote.run ios.sdk_install with the person's Xcode.xip."}),
    (("ios.crash_symbolicate", "ios.ipa_inspect"), {
        "phone": "local", "requires": [],
        "evidence": "Pure file parsing in the Rust control program.",
        "next_test": "ios.ipa_inspect on a fixture IPA from tests/fixtures."}),
    (("ios.",), {
        "phone": "conditional", "requires": ["usbmuxd and libimobiledevice on the phone", "USB OTG cable"],
        "evidence": "USB iPhone operations use libimobiledevice, which builds for aarch64 Linux, but no Omarchy Mobile package set is qualified yet.",
        "next_test": "ios.doctor on the device with an iPhone on USB OTG; otherwise offer the host."}),
    (("apple.",), {
        "phone": "remote", "requires": ["host with xtool/Apple SDK or a Mac"],
        "evidence": "apple.* wraps xtool and Apple device tooling; see ios.sdk_install.",
        "next_test": "remote.run apple.devices on a configured host."}),
    (("swift.",), {
        "phone": "conditional", "requires": ["Swift toolchain for aarch64 Linux (swift.org ships one)", "Swift Android SDK"],
        "evidence": "swift.org publishes aarch64 Linux toolchains; the Android SDK for Swift is architecture-independent. Not yet installed or tested on Omarchy Mobile.",
        "next_test": "swift.status on the device after installing the aarch64 toolchain; swift.plan for a sample package."}),
    (("preview.",), {
        "phone": "conditional", "requires": ["Qt WebEngine for linux_arm64 (Qt ships it; packaging/portable/Dockerfile.aarch64 downloads it)", "GL or software rendering"],
        "evidence": "The web preview is a QtWebEngineQuick view; Qt provides aarch64 binaries, but Chromium on the Pixel's GPU/Wayland stack is unqualified and memory-heavy. The build must make WebEngine optional so the rest of the app runs without it.",
        "next_test": "Run the aarch64 build with and without the WebEngine plugin; the preview section shows an 'unavailable' state when absent."}),
    (("runtime.",), {
        "phone": "conditional", "requires": ["a running Android device or emulator session"],
        "evidence": "runtime.* drives a live device session (native/runtime.rs); with a USB/Wi-Fi phone it is local, with an emulator it follows the emulator's host.",
        "next_test": "runtime.start with a paired phone on the device."}),
    (("demo.",), {
        "phone": "local", "requires": ["software H.264 encoder (slow)", "storage for clips"],
        "evidence": "Demo Studio renders offscreen with Qt and encodes H.264 (MVP: one clip, silent MP4). No hardware encoder is assumed; the app shows the slower software path and the host option.",
        "next_test": "demo.export of a 5 s clip on the device; record wall time and thermal state."}),
    (("update.",), {
        "phone": "local", "requires": ["the aarch64 portable package"],
        "evidence": "tools/build_portable.py builds Linux-aarch64-portable.tar.gz; update.check must pick the aarch64 asset.",
        "next_test": "update.check with a fixture release index that lists both architectures."}),
    (("remote.",), {
        "phone": "local", "requires": ["ssh key", "explicit connect by the person"],
        "evidence": "remote.* is the host transport itself (native/remote.rs). Connections are explicit; no discovery, no auto-connect.",
        "next_test": "remote.check against a fake sshd fixture; no real host during the hardware block."}),
    (("play.",), {
        "phone": "local", "requires": ["network", "Play service account on the person's side"],
        "evidence": "HTTP API calls from the control program; each upload asks first.",
        "next_test": "play.prepare against the mocked endpoint in tests."}),
    (("testing.", "debug.", "profile.", "screen.", "device."), {
        "phone": "local", "requires": ["adb (arm64)", "a paired device"],
        "evidence": "All device-facing operations go through adb/JDWP/perfetto on the device; no x86_64 binaries are involved.",
        "next_test": "testing.find, debug.start and profile.capture against a paired phone from the device."}),
    (("project.import_xcode",), {
        "phone": "local", "requires": [],
        "evidence": "Parses an Xcode project into a SwiftPM layout; file work only.",
        "next_test": "project.import_xcode on the fixture project."}),
    (("project.", "files.", "workspace.", "job.", "release.", "catalog.", "settings.", "desktop.", "doctor", "capabilities", "agent.policy", "audit.list", "result.read", "workflow.run", "storage.prune", "diagnostics.bundle"), {
        "phone": "local", "requires": ["Gradle/JDK 21 arm64 for Android builds", "Flutter arm64 for Flutter projects"],
        "evidence": "Projects, files, jobs, release signing (keytool/apksigner are Java) and settings live in the control program; iOS build actions inside project.execute route to a host through the same explicit host choice.",
        "next_test": "project.inspect, project.execute build (android, debug) and release.sign on the device with a sample project."}),
]


def classify(name: str) -> dict:
    for keys, verdict in RULES:
        for key in keys:
            if name == key or (key.endswith(".") and name.startswith(key)) or (not key.endswith(".") and name.startswith(key) and key.count(".") >= 1 and name[len(key):len(key)+1] in ("", ".", "_")):
                return dict(verdict)
    raise KeyError(name)


def build() -> dict:
    operations = json.loads(OPERATIONS.read_text(encoding="utf-8"))
    rows = []
    for operation in operations:
        name = operation["name"]
        verdict = classify(name)
        rows.append({
            "operation": name,
            "title": operation.get("title", ""),
            "phone": verdict["phone"],
            "requires": verdict["requires"],
            "evidence": verdict["evidence"],
            "next_test": verdict["next_test"],
            "verified_on_pixel": False,
        })
    counts = {status: sum(1 for r in rows if r["phone"] == status) for status in STATUSES}
    return {
        "$schema": "tomacco-mobile-parity/1",
        "target": "Omarchy Mobile, Linux aarch64 (Pixel; hardware profile unknown until the device block is lifted)",
        "legend": {
            "local": "runs on the phone itself",
            "remote": "runs only on an explicitly chosen host (remote.*); the app offers the host choice, never a silent fallback",
            "conditional": "runs on the phone when the listed requirement is met; the app shows the requirement and the host option",
            "unavailable": "cannot run anywhere reachable from the phone",
        },
        "counts": counts,
        "operations": rows,
        "ui_routes": [
            {"route": "project", "phone": "local", "screen": "Bogen 04/05", "qml": "Workspace.qml projectOverview"},
            {"route": "devices", "phone": "local", "screen": "Bogen 09/11", "qml": "Workspace.qml devicesPage, AndroidDevices.qml"},
            {"route": "live device", "phone": "local", "screen": "Bogen 10", "qml": "NativeWorkspace.qml"},
            {"route": "jobs", "phone": "local", "screen": "Bogen 12/13", "qml": "Workspace.qml jobs"},
            {"route": "tools", "phone": "local", "screen": "Bogen 14/15", "qml": "Workspace.qml tools, ApprovalDialog.qml"},
            {"route": "debug", "phone": "local", "screen": "Bogen 18", "qml": "DebugWorkspace.qml"},
            {"route": "testing", "phone": "local", "screen": "Bogen 16", "qml": "TestWorkspace.qml, EnvironmentPanel.qml"},
            {"route": "profiling", "phone": "local", "screen": "Bogen 17", "qml": "ProfileWorkspace.qml"},
            {"route": "files", "phone": "local", "screen": "Bogen 06/07/08", "qml": "FileWorkspace.qml"},
            {"route": "release", "phone": "local", "screen": "Bogen 21", "qml": "ReleaseWorkspace.qml"},
            {"route": "demo", "phone": "local", "screen": "Bogen 20", "qml": "DemoWorkspace.qml"},
            {"route": "preview (web)", "phone": "conditional", "screen": "Bogen 23 (Mehr)", "qml": "Main.qml preview column; needs optional WebEngine"},
            {"route": "ios-research (virtual iPhone)", "phone": "remote", "screen": "Bogen 23 (Mehr)", "qml": "IosResearch.qml"},
            {"route": "settings", "phone": "local", "screen": "Bogen 22", "qml": "NativeWorkspace.qml settings sheets"},
        ],
    }


def render_md(data: dict) -> str:
    lines = ["# Phone parity matrix", "", f"Target: {data['target']}", "", "Generated by tools/build_mobile_parity.py from assets/operations.json. Not a test result: `verified_on_pixel` is false for every row until the device qualification step runs.", ""]
    lines.append("| Status | Count |")
    lines.append("|---|---|")
    for status in STATUSES:
        lines.append(f"| {status} | {data['counts'][status]} |")
    lines += ["", "| Operation | Phone | Requires | Next test |", "|---|---|---|---|"]
    for row in data["operations"]:
        lines.append(f"| `{row['operation']}` | {row['phone']} | {'; '.join(row['requires']) or '–'} | {row['next_test']} |")
    lines += ["", "## UI routes", "", "| Route | Phone | Bogen screen | QML |", "|---|---|---|---|"]
    for route in data["ui_routes"]:
        lines.append(f"| {route['route']} | {route['phone']} | {route['screen']} | {route['qml']} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when the generated files differ from the repository")
    args = parser.parse_args(argv)
    data = build()
    json_text = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
    md_text = render_md(data)
    if args.check:
        stale = [str(p) for p, text in ((JSON_OUT, json_text), (MD_OUT, md_text)) if not p.exists() or p.read_text(encoding="utf-8") != text]
        if stale:
            print("stale: " + ", ".join(stale), file=sys.stderr)
            return 1
        print("mobile parity up to date")
        return 0
    JSON_OUT.write_text(json_text, encoding="utf-8")
    MD_OUT.write_text(md_text, encoding="utf-8")
    print(f"{len(data['operations'])} operations: " + ", ".join(f"{k} {v}" for k, v in data["counts"].items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
