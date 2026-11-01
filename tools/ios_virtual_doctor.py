#!/usr/bin/env python3
"""The profile half of ios.virtual.doctor: one pass over a virtual iPhone profile.

Checks every input profile.json names the way a start would use it (with the
checks tools/ios_virtual_profile.py runs before it writes a profile), whether
the guest tools and the engine were built from the sources this checkout has
now, whether the recorded runs and saved states still have their disks, and
how the last run ended. Prints one JSON object whose `checks` rows have
check, status (pass, warn or fail), found, message and remedy, as
ios.virtual.host_check's do. It reads files only: no guest runs and nothing is
written or repaired.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import ios_virtual_profile as profiles
from ios_virtual_iphone import changed_guest_sources

REPO = Path(__file__).resolve().parents[1]


def row(check, status, found, message, remedy=None):
    return {"check": check, "status": status, "found": found, "message": message,
            "remedy": None if status == "pass" else remedy}


def inputs(profile, state):
    """(input name, path, check, command that makes it, fail or warn), from a written profile."""
    prepare, boot = profile.get("prepare") or {}, profile.get("boot") or {}
    path = lambda value: Path(value) if isinstance(value, str) and value else None
    tools, trustcache = path(profile.get("tools")) or path(state.get("tools")), path(prepare.get("trustcache"))
    base = (state.get("verified") or {}).get("install") or (state.get("install") if state.get("closed") else None)
    missing = Path("/nonexistent")
    return (
        ("engine", path(profile.get("engine")), profiles.engine_problem, profiles.MAKE_ENGINE, "fail"),
        ("guest control backend", path(boot.get("guest_control_backend")), profiles.executable_problem,
         profiles.MAKE_BACKEND, "fail"),
        ("helper engine", path(prepare.get("helper_engine")), profiles.executable_problem, profiles.MAKE_HELPER, "fail"),
        ("helper firmware", path(prepare.get("bios")), profiles.directory_problem, profiles.MAKE_HELPER, "fail"),
        ("helper support", path(prepare.get("support")),
         lambda value: profiles.directory_problem(value, "busybox/busybox", "apfs/apfs.ko"), profiles.MAKE_SUPPORT,
         "fail"),
        # A start falls back to the support's own vmlinuz, then the running kernel's.
        ("helper kernel", path(prepare.get("helper_kernel")), profiles.file_problem,
         profiles.MAKE_SUPPORT + " (or a kernel matching apfs.ko)", "warn"),
        ("SPTM", path(boot.get("sptm")), profiles.file_problem, profiles.MAKE_BOOT_INPUTS, "fail"),
        ("TXM", path(boot.get("txm")), profiles.file_problem, profiles.MAKE_BOOT_INPUTS, "fail"),
        ("SEP firmware", path(boot.get("sep")), profiles.file_problem, profiles.MAKE_BOOT_INPUTS, "fail"),
        ("RAM disk", path(boot.get("ramdisk")), profiles.file_problem, profiles.MAKE_BOOT_INPUTS, "fail"),
        ("system trust cache", trustcache, profiles.file_problem, profiles.MAKE_CRYPTEX, "fail"),
        ("device tree", path(prepare.get("device_tree")), profiles.tree_problem, profiles.MAKE_CRYPTEX, "fail"),
        ("system root hash", path(boot.get("system_volume_root_hash")), profiles.file_problem, profiles.MAKE_CRYPTEX,
         "fail"),
        ("boot manifest", path(boot.get("boot_manifest")), profiles.manifest_problem, profiles.MAKE_MANIFEST, "fail"),
        ("graphics payload", path(profile.get("graphics")), profiles.graphics_problem, profiles.MAKE_CRYPTEX, "fail"),
        ("guest tools", tools, lambda value: profiles.tools_problem(value, trustcache or missing), profiles.MAKE_TOOLS,
         "fail"),
        ("kernel", path(boot.get("kernel")), lambda value: profiles.kernel_problem(value, tools or missing),
         f"{profiles.MAKE_FILEPORT}; {profiles.MAKE_WORKLOOP}; only if the tools changed afterwards: "
         f"{profiles.MAKE_PIN}", "fail"),
        # The base a start copies from; its guest tools are replaced at every start.
        ("verified base", path(base), lambda value: profiles.install_problem(value, missing, missing),
         "Stop a run with ios.virtual.stop so it ends verified, or use a saved state with ios.virtual.state_use",
         "fail"),
    )


def profile_inputs(profile, state):
    problems, checked = [], 0
    for name, path, check, make, severity in inputs(profile, state):
        problem = "not named in profile.json" if path is None else profiles.problem_of(lambda: check(path))
        checked += 1
        if problem:
            problems.append({"input": name, "path": str(path) if path else None, "problem": problem,
                             "make": make, "severity": severity})
    status = ("fail" if any(item["severity"] == "fail" for item in problems) else
              "warn" if problems else "pass")
    if not problems:
        return row("profile_inputs", "pass", {"checked": checked},
                   f"All {checked} inputs profile.json names are present and fit together.")
    first = problems[0]
    return row("profile_inputs", status, {"checked": checked, "problems": problems},
               f"{len(problems)} of {checked} inputs have a problem; the first: {first['input']}"
               f" ({first['path']}): {first['problem']}.",
               "Make each one with the command under make in found.problems, or point profile.json at the right "
               "file. python3 tools/ios_virtual_profile.py --check runs the same checks on a new profile's inputs.")


def guest_tools_sources(profile, state):
    tools = profile.get("tools") or state.get("tools")
    if not tools or not Path(tools).is_dir():
        return row("guest_tools_sources", "pass", {"tools": tools},
                   "No guest-tools build to compare; profile_inputs reports a missing one.")
    changed = changed_guest_sources(tools)
    if not changed:
        return row("guest_tools_sources", "pass", {"tools": tools, "changed": []},
                   "The guest tools were built from the guest sources this checkout has (or the build records none).")
    return row("guest_tools_sources", "warn", {"tools": tools, "changed": changed},
               f"{len(changed)} guest sources changed since these tools were built: {', '.join(changed[:6])}"
               + (" and more." if len(changed) > 6 else "."),
               "The next start rebuilds the guest tools with the profile's apps first, which takes some minutes. "
               "To do it beforehand, call ios.emulator.build_guest_tools and name the result as tools.")


def engine_manifest(engine):
    """The engine's build-manifest.json, when it describes these very bytes."""
    for manifest in (engine.parent / "build-manifest.json", engine.parent.parent / "build-manifest.json"):
        if manifest.is_file():
            record = json.loads(manifest.read_text())
            if record.get("engine_sha256") == hashlib.sha256(engine.read_bytes()).hexdigest():
                return manifest, record
    return None, None


def engine_patches(profile, patches=None):
    engine = Path(profile.get("engine") or "/nonexistent")
    if not engine.is_file():
        return row("engine_patches", "pass", {"engine": str(engine)},
                   "No engine to compare; profile_inputs reports a missing one.")
    try:
        manifest, record = engine_manifest(engine)
    except (OSError, ValueError) as error:
        return row("engine_patches", "warn", {"engine": str(engine)}, f"The engine's build record cannot be read: {error}.",
                   "Rebuild the engine: " + profiles.MAKE_ENGINE)
    if record is None:
        return row("engine_patches", "warn", {"engine": str(engine)},
                   "No build-manifest.json beside the engine describes these bytes, so its patches are unknown.",
                   "Rebuild the engine with " + profiles.MAKE_ENGINE + ", which writes the record.")
    recorded = {entry.get("name"): entry.get("sha256")
                for entry in (record.get("ordered_patch_contract") or {}).get("engine", []) if isinstance(entry, dict)}
    try:
        if patches is None:
            import build_ios_paravirt
            patches = build_ios_paravirt.PATCHES
        current = {patch.name: hashlib.sha256(patch.read_bytes()).hexdigest() for patch in patches}
    except (ImportError, OSError) as error:
        return row("engine_patches", "pass", {"engine": str(engine), "manifest": str(manifest)},
                   f"The checkout's engine patches cannot be read here ({error}); nothing to compare.")
    words = [f"{name} changed" for name in sorted(set(recorded) & set(current)) if recorded[name] != current[name]]
    words += [f"{name} is new" for name in sorted(set(current) - set(recorded))]
    words += [f"{name} is no longer in the series" for name in sorted(set(recorded) - set(current))]
    found = {"engine": str(engine), "manifest": str(manifest), "differences": words}
    if not words:
        return row("engine_patches", "pass", found,
                   f"The engine was built from the {len(current)} engine patches this checkout has.")
    return row("engine_patches", "warn", found,
               f"The engine was built from other patches than this checkout's: {'; '.join(words[:4])}"
               + ("; and more." if len(words) > 4 else "."),
               "Rebuild the engine: " + profiles.MAKE_ENGINE + ". An engine built from the same inputs is reused.")


def saved_states(directory, state):
    path = directory / "saved-states.json"
    problems = []
    names = []
    if path.exists() or path.is_symlink():
        try:
            states = json.loads(path.read_text())["states"]
            if not isinstance(states, dict):
                raise TypeError("states is not an object")
        except (OSError, ValueError, KeyError, TypeError) as error:
            return row("saved_states", "fail", {"file": str(path)}, f"{path} cannot be read: {error}.",
                       "Fix or move the file aside; while it cannot be read, no disk copy is pruned.")
        for name, record in sorted(states.items()):
            names.append(name)
            install = record.get("install") if isinstance(record, dict) else None
            if not isinstance(install, str) or not Path(install).is_dir():
                problems.append(f"{name}: its disks {install} are gone")
    verified = (state.get("verified") or {}).get("install")
    if verified and not Path(verified).is_dir():
        problems.append(f"the verified base {verified} is gone")
    found = {"states": names, "problems": problems}
    if problems:
        message = "; ".join(problems)
        return row("saved_states", "fail", found, message[:1].upper() + message[1:] + ".",
                   "Delete a state whose disks are gone with ios.virtual.state_delete; without a verified base, use "
                   "another state with ios.virtual.state_use.")
    return row("saved_states", "pass", found,
               f"{len(names)} saved states and the verified base have their disks." if names else
               "No saved states; the verified base has its disks.")


def last_run(state):
    verdict = state.get("verdict")
    if not isinstance(verdict, dict) or not isinstance(verdict.get("summary"), str):
        outcome = (state.get("outcome") or {}).get("outcome") if isinstance(state.get("outcome"), dict) else None
        if state.get("phase") == "unverified":
            return row("last_run", "warn", {"outcome": outcome},
                       "The last run did not end verified; the next start goes back to the last verified run.",
                       "Stop runs with ios.virtual.stop while they answer.")
        return row("last_run", "pass", {"outcome": outcome}, "No finished run is described yet.")
    found = {key: verdict.get(key) for key in ("verified", "outcome", "reasons")}
    if verdict.get("verified") is True and not verdict.get("reasons") and verdict.get("outcome") in (
            "bounded-session", "emulator-exited", None):
        return row("last_run", "pass", found, "The last run ended verified: " + verdict["summary"])
    message = " ".join([verdict["summary"]] + [reason for reason in verdict.get("reasons") or []
                                               if reason != verdict["summary"]])
    return row("last_run", "warn", found, ("The last run ended verified, but: " if verdict.get("verified") else
                                           "The last run was not verified. ") + message,
               verdict.get("remedy") or "See ios.emulator.report for the last guest job.")


def doctor(directory, patches=None):
    profile = json.loads((directory / "profile.json").read_text())
    state = json.loads((directory / "state.json").read_text())
    return {"checks": [profile_inputs(profile, state), guest_tools_sources(profile, state),
                       engine_patches(profile, patches), saved_states(directory, state), last_run(state)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", required=True, type=lambda value: Path(value).absolute())
    args = parser.parse_args()
    try:
        print(json.dumps({"ok": True, **doctor(args.profile)}))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
