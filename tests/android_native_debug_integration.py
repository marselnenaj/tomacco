#!/usr/bin/env python3
"""Actual Android JNI/LLDB acceptance through the public CLI, not fake adapters.

Run on a dedicated emulator/device: installs and launches an own debug fixture.
The caller supplies TOMACCOCTL, an isolated TOMACCO_HOME and tool settings.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from native_client import dispatch, jobs, atomic_json, StudioError


def finished(job):
    while job["state"] not in jobs.TERMINAL:
        job = jobs.wait(job["id"], 5)
    assert job["state"] == "succeeded", {"job": job, "log": jobs.logs(job["id"], limit=20000)}
    return job


def fixture(root, ndk, device):
    project = root / "Native Debug Fixture"
    if not project.exists():
        dispatch("project.create", {"path": str(project), "name": "NativeDebugFixture", "organization": "io.github.marselnenaj.tomacco", "kind": "android"})
    fixtures = Path(__file__).parent / "fixtures"
    for relative, text in (("app/src/main/java/io/github/marselnenaj/tomacco/nativedebugfixture/MainActivity.java", (fixtures / "NativeDebugActivity.java").read_text()),
                           ("native.c", (fixtures / "NativeDebugLibrary.c").read_text())):
        existing = dispatch("files.read", {"path": str(project), "file": relative}) if (project / relative).exists() else {"sha256": ""}
        if not (project / relative).exists() or (project / relative).read_text() != text:
            dispatch("files.write", {"path": str(project), "file": relative, "if_match": existing["sha256"], "text": text})
    tools = ndk / "toolchains/llvm/prebuilt/linux-x86_64/bin"
    symbols = root / "native symbols"
    symbols.mkdir(exist_ok=True)
    # A supplied symbol file need not share the packaged library's filename.
    library = symbols / "unstripped symbols.so"
    for arch, target in (("x86_64", "x86_64-linux-android26-clang"), ("arm64", "aarch64-linux-android26-clang")):
        output = library if arch == "x86_64" else symbols / "libtomacconative-arm64.so"
        subprocess.run([str(tools / target), "-g", "-O0", "-fno-omit-frame-pointer", "-fPIC", "-shared", "-Wl,--build-id=sha1", "-Wl,-soname,libtomacconative.so", str(project / "native.c"), "-o", str(output)], check=True)
    stale = symbols / "libtomacconative.so"
    if stale.exists(): stale.unlink()
    packaged = project / "app/src/main/jniLibs/x86_64/libtomacconative.so"
    packaged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(library, packaged)
    finished(dispatch("project.execute", {"path": str(project), "platform": "android", "action": "build"}))
    finished(dispatch("device.install", {"platform": "android", "device": device, "paths": [str(project / "app/build/outputs/apk/debug/app-debug.apk")]}))
    finished(dispatch("device.app", {"platform": "android", "device": device, "bundle_id": "io.github.marselnenaj.tomacco.nativedebugfixture", "action": "launch"}))
    return project, library


def stopped(session, after=-1):
    end = time.monotonic() + 20
    while time.monotonic() < end:
        state = dispatch("debug.state", {"session": session})
        assert not state["terminated"], state
        if state["stopped"] and state.get("event_cursor", 0) > after:
            return state
        time.sleep(.08)
    raise AssertionError(dispatch("debug.events", {"session": session}))


def closed(session):
    end = time.monotonic() + 20
    while time.monotonic() < end:
        state = dispatch("debug.state", {"session": session})
        if state["terminated"] and state.get("cleanup", {}).get("complete"):
            return state
        time.sleep(.1)
    raise AssertionError(state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--ndk", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    project, library = fixture(root, args.ndk.resolve(), args.device)
    if args.prepare_only:
        print(json.dumps({"prepared": True, "project": str(project), "symbols": str(library)}))
        return
    request = {"backend": "android-lldb", "device": args.device, "bundle_id": "io.github.marselnenaj.tomacco.nativedebugfixture", "program": str(library), "launch": False}
    session = None
    records = []
    try:
        # Two independent clients race with differently ordered JSON objects.
        # Admission must create one worker and reuse it for the second client.
        with ThreadPoolExecutor(max_workers=2) as pool:
            starts = list(pool.map(lambda value: dispatch("debug.start", value), [request, dict(reversed(list(request.items())))]))
        assert starts[0]["session"] == starts[1]["session"], starts
        assert sum(bool(value.get("reused")) for value in starts) == 1, starts
        start = next(value for value in starts if not value.get("reused"))
        session = start["session"]
        stopped(session)
        reused = dispatch("debug.start", request)
        assert reused["session"] == session and reused["reused"], reused
        for conflict in ({**request, "stop_on_entry": False}, {"backend": "jdwp", "device": args.device, "bundle_id": request["bundle_id"]}):
            try:
                dispatch("debug.start", conflict)
                raise AssertionError("Conflicting device debugger was admitted")
            except StudioError as error:
                assert error.code == "device_debug_busy", error
        source = str(project / "native.c")
        bp = dispatch("debug.breakpoints", {"session": session, "source": source, "lines": [5]})
        assert bp["breakpoints"][0]["verified"], bp
        cursor = dispatch("debug.state", {"session": session})["event_cursor"]
        dispatch("debug.control", {"session": session, "action": "continue"})
        state = stopped(session, cursor)
        stack = dispatch("debug.stack", {"session": session})
        frame = stack["frames"][0]
        assert frame["line"] == 5 and "tomacco_calculate" in frame["name"], stack
        value = dispatch("debug.evaluate", {"session": session, "frame_id": str(frame["id"]), "expression": "count"})
        assert value["result"] == "40", value
        dispatch("debug.control", {"session": session, "action": "next"})
        stopped(session, state["event_cursor"])
        stack = dispatch("debug.stack", {"session": session})
        frame = str(stack["frames"][0]["id"])
        value = dispatch("debug.evaluate", {"session": session, "frame_id": frame, "expression": "count"})
        assert value["result"] == "42", value
        scopes = dispatch("debug.variables", {"session": session, "frame_id": frame})
        variables = dispatch("debug.variables", {"session": session, "reference": str(scopes["scopes"][0]["variablesReference"])})
        assert any(v["name"] == "count" and v["value"] == "42" for v in variables["variables"]), variables
        records.append({"start": start, "concurrent_start": starts, "reused": reused, "breakpoints": bp, "stack": stack, "variables": variables, "events": dispatch("debug.events", {"session": session})})
        dispatch("debug.control", {"session": session, "action": "disconnect"})
        records.append({"disconnect": closed(session)})
        session = None
        # A dead client/worker must not leave a foreign forward or lock behind.
        crash = dispatch("debug.start", request)
        session = crash["session"]
        stopped(session)
        os.kill(crash["pid"], signal.SIGKILL)
        records.append({"worker_recovery": closed(session)})
        session = None
        resumed = dispatch("debug.start", request)
        session = resumed["session"]
        assert resumed["android_native"]["target_pid"] == start["android_native"]["target_pid"], resumed
        dispatch("debug.control", {"session": session, "action": "disconnect"})
        records.append({"reattach": closed(session)})
        session = None
        # A real adapter process that exits during initialization exercises
        # cleanup after the remote server and ADB mapping already exist.
        try:
            dispatch("debug.start", {**request, "adapter": "/usr/bin/false"})
            raise AssertionError("Exited adapter accepted")
        except StudioError as error:
            assert error.code == "debug_disconnected", error
        failed = dispatch("debug.list", {})["sessions"][0]
        assert failed["terminated"] and failed["cleanup"]["complete"], failed
        records.append({"adapter_failure": failed})
        recovered = dispatch("debug.start", request)
        session = recovered["session"]
        assert recovered["android_native"]["target_pid"] == start["android_native"]["target_pid"], recovered
        dispatch("debug.control", {"session": session, "action": "disconnect"})
        records.append({"adapter_failure_reattach": closed(session)})
        session = None
        try:
            dispatch("debug.start", {**request, "program": str(library.with_name("libtomacconative-arm64.so"))})
            raise AssertionError("Wrong-architecture symbols accepted")
        except StudioError as error:
            assert error.code == "native_arch_mismatch", error
        # The actual app must still respond after all detach/recovery paths.
        found = dispatch("testing.find", {"device": args.device, "selector": {"text": "Native value: 42"}})
        assert len(found["matches"]) == 1, found
        atomic_json(root / "results.json", {"passed": True, "device": args.device, "checks": records, "app_alive": found})
        print(json.dumps({"passed": True, "report": str(root / "results.json")}))
    finally:
        if session:
            try: dispatch("debug.control", {"session": session, "action": "disconnect"})
            except StudioError: pass


if __name__ == "__main__":
    main()
