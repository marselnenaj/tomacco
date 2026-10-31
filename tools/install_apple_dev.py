#!/usr/bin/env python3
"""Install the pinned omarchy-apple-dev checkout into Tomacco's data directory.

Runs as the job of `ios.toolkit_install`. Tomacco ships none of that project's
code: this fetches one commit and later operations call its scripts.

    install_apple_dev.py --root DIR                  the checkout only
    install_apple_dev.py --root DIR --toolchain      and its install-toolchain.sh
                         [--xcode-xip /abs/Xcode.xip]

The toolchain step runs upstream's installer in the mode that needs the Xcode
archive you supply, with --user-only and with HOME pointing at DIR/home, so:

- It never runs sudo, pacman or an AUR helper. A `sudo` that always refuses is
  first on PATH as well, so that no step can reach the real one.
- It never downloads anything from Apple. The installer's other mode pulls
  Apple's public iOS update, and neither this recipe nor any operation selects
  it. Without --xcode-xip the installer stops at its SDK step, and that is
  reported as `sdk_ready: false`, not as a failure.
- It leaves your own xtool, your imported SDK and your Swift toolchain alone.
  Upstream replaces ~/.local/bin/xtool with its fork, whose SDK stamp differs,
  and patches defaults inside the toolchain it finds. Here both happen to
  private copies under DIR/home.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

UPSTREAM = "https://github.com/joshuaswarren/omarchy-apple-dev"
REVISION = "79bda716bf207924efc25bce923bda261dc67837"


class Refused(RuntimeError):
    """The install cannot go on; the message says why and what to do."""


def run(argv, **options):
    print("+", " ".join(str(part) for part in argv), flush=True)
    return subprocess.run([str(part) for part in argv], **options)


def git(*arguments, cwd):
    done = run(["git", *arguments], cwd=cwd, capture_output=True, text=True)
    if done.returncode:
        raise Refused(f"git {arguments[0]} failed: {done.stderr.strip()[:400]}")
    return done.stdout.strip()


def fetch_checkout(root):
    """DIR/checkout/REVISION, verified. A finished checkout is kept; a partial
    fetch never takes that name."""
    target = root / "checkout" / REVISION
    if (target / ".git").is_dir():
        if git("rev-parse", "HEAD", cwd=target) == REVISION:
            print(f"checkout present: {target}", flush=True)
            return target
        raise Refused(f"{target} is not at the pinned commit; delete it and run this again")
    partial = target.with_name(f".partial-{os.getpid()}")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    try:
        git("init", "-q", cwd=partial)
        git("fetch", "-q", "--depth", "1", UPSTREAM, REVISION, cwd=partial)
        git("checkout", "-q", "--detach", "FETCH_HEAD", cwd=partial)
        got = git("rev-parse", "HEAD", cwd=partial)
        if got != REVISION:
            raise Refused(f"The fetched commit is {got}, not the pinned {REVISION}")
        partial.rename(target)
    finally:
        shutil.rmtree(partial, ignore_errors=True)
    print(f"checkout ready: {target}", flush=True)
    return target


def private_toolchain(home):
    """A copy of the Swift toolchain on PATH that upstream's installer may
    patch. Reflinked where the filesystem can; about 3.5 GiB where it cannot."""
    swift = shutil.which("swift")
    if not swift:
        raise Refused("No Swift on PATH. Install Swift from https://www.swift.org/install/linux/ "
                      "(Swift 6.4 goes with Xcode 27) and run this again")
    done = subprocess.run([swift, "-print-target-info"], capture_output=True, text=True)
    if done.returncode:
        raise Refused(f"{swift} -print-target-info failed: {done.stderr.strip()[:400]}")
    resource = Path(json.loads(done.stdout)["paths"]["runtimeResourcePath"]).resolve()
    source = resource.parents[2]
    if resource != source / "usr/lib/swift":
        raise Refused(f"Cannot find the toolchain root from {resource}")
    target = home / ".local/share/tomacco-swift" / source.name
    if not (target / "usr/bin/swift").exists():
        partial = target.with_name(f".partial-{os.getpid()}")
        shutil.rmtree(partial, ignore_errors=True)
        partial.parent.mkdir(parents=True, exist_ok=True)
        print(f"Copying the Swift toolchain {source} for private use", flush=True)
        if run(["cp", "-a", "--reflink=auto", source, partial]).returncode:
            shutil.rmtree(partial, ignore_errors=True)
            raise Refused("Could not copy the Swift toolchain")
        partial.rename(target)
    return target


def write_guard(root):
    """First on PATH of everything that runs from the checkout."""
    guard = root / "guard"
    guard.mkdir(exist_ok=True)
    sudo = guard / "sudo"
    sudo.write_text("#!/bin/sh\necho 'Tomacco does not run sudo; install this package yourself.' >&2\nexit 1\n")
    sudo.chmod(0o755)
    return guard


def environment(root, home, toolchain):
    guard = write_guard(root)
    env = dict(os.environ)
    # Choices that would change what the installer does. The saved mode file
    # lives in the private HOME, and --mode wins over it.
    for name in ("OMARCHY_APPLE_MODE", "XCODE_XIP", "SDK_SRC", "SDK_CACHE", "SDKFREE_IPSW", "SDKFREE_DEVICE"):
        env.pop(name, None)
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), XDG_CACHE_HOME=str(home / ".cache"),
               XDG_DATA_HOME=str(home / ".local/share"),
               PATH=os.pathsep.join([str(guard), str(toolchain / "usr/bin"), str(home / ".local/bin"), env["PATH"]]))
    return env


def sdk_bundle(home):
    return home / ".config/swiftpm/swift-sdks/darwin.artifactbundle"


def install_toolchain(root, checkout, xcode_xip):
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    toolchain = private_toolchain(home)
    env = environment(root, home, toolchain)
    script = checkout / "install-toolchain.sh"
    # Vendor toolchains link library names a rolling distribution lacks. This
    # links aliases into the private copy; what it leaves unresolved only
    # matters if the compiler below does not start.
    run(["bash", script, "--curses-compat", toolchain], env=env, cwd=home, stdin=subprocess.DEVNULL)
    for tool in ("swift", "swift-build"):
        if run([toolchain / "usr/bin" / tool, "--version"], env=env, stdout=subprocess.DEVNULL).returncode:
            raise Refused(f"The private copy's {tool} does not start. Its missing libraries are listed above; "
                          "install them with your package manager")
    if xcode_xip:
        env["XCODE_XIP"] = str(xcode_xip)
    code = run(["bash", script, "--mode", "full", "--user-only"], env=env, cwd=home,
               stdin=subprocess.DEVNULL).returncode
    xtool = home / ".local/bin/xtool"
    if code and not (xtool.exists() and not sdk_bundle(home).is_dir() and not xcode_xip):
        raise Refused(f"install-toolchain.sh stopped with exit code {code}; its output is above")
    return {"toolchain": True, "xtool": str(xtool), "swift": str(toolchain / "usr/bin/swift"),
            "sdk_ready": sdk_bundle(home).is_dir(), "installer_exit_code": code}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--toolchain", action="store_true")
    parser.add_argument("--xcode-xip", type=Path)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    root = args.root.resolve()
    report = {"repository": UPSTREAM, "commit": REVISION, "passed": False}
    try:
        write_guard(root)
        report["checkout"] = str(fetch_checkout(root))
        if args.toolchain:
            report.update(install_toolchain(root, Path(report["checkout"]), args.xcode_xip))
            if not report["sdk_ready"]:
                report["next"] = ("Download Xcode from https://developer.apple.com/download/all/?q=Xcode yourself, "
                                  "read Apple's terms, and call ios.toolkit_install again with toolchain and xcode_xip")
        report["passed"] = True
    except (Refused, OSError, subprocess.SubprocessError) as error:
        report["error"] = str(error)
    (root / "install-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)
    if report.get("next"):
        print(report["next"], flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
