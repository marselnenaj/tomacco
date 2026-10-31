#!/usr/bin/env python3
"""Build the headless container image and check it.

The image (packaging/container/Dockerfile) holds tomaccoctl from a signed
release package, a JDK, Gradle and the Android SDK; the emulator variant adds
the Android Emulator and one x86_64 system image. docs/container.md describes it.

    tools/build_container.py build --accept-android-sdk-license             # -> tomacco-container:VERSION
    tools/build_container.py build --accept-android-sdk-license --emulator  # -> tomacco-container:VERSION-emulator
    tools/build_container.py verify tomacco-container:VERSION
    tools/build_container.py verify tomacco-container:VERSION-emulator --emulator   # needs /dev/kvm
    tools/build_container.py mcp tomacco-container:VERSION

Building installs the Android SDK, so it needs --accept-android-sdk-license:
whoever builds the image accepts Google's Android SDK license
(https://developer.android.com/studio/terms). `build --packages DIR` uses the
signed package in DIR (as make_release.py does for a release being made)
instead of downloading the published one; it is checked the same way.

Nothing here pushes an image anywhere; `push-commands` only prints the
commands a person would run to publish one.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = ROOT / "packaging/container"
IMAGE = "tomacco-container"
OUTPUT = "build-container"
REGISTRY = "ghcr.io/marselnenaj/tomacco"
# The operations an MCP client must find in tools/list.
MCP_TOOLS = ("doctor", "capabilities", "project.create", "project.execute", "project.run", "device.capture", "job.wait")
SYSTEM_IMAGE = "system-images;android-36;google_apis;x86_64"
APPLICATION_ID = "dev.example.firstapp"


class Failed(Exception):
    pass


def run(argv, **options):
    print("+", shlex.join(str(a) for a in argv), flush=True)
    done = subprocess.run([str(a) for a in argv], check=False, **options)
    if done.returncode:
        raise Failed(f"{shlex.join(str(a) for a in argv)} failed with {done.returncode}")
    return done


def project_version(root=ROOT):
    found = re.search(r"project\(\s*\w+\s+VERSION\s+([0-9][0-9.]*)", (root / "CMakeLists.txt").read_text())
    if not found:
        raise Failed("CMakeLists.txt names no version")
    return found.group(1)


def image_name(version, emulator=False):
    return f"{IMAGE}:{version}" + ("-emulator" if emulator else "")


def package_files(version, prefix="tomacco"):
    """The regular package with its checksum and signature: what the Dockerfile reads from packages/."""
    name = f"{prefix}-{version}-Linux-x86_64.tar.gz"
    return [name, name + ".sha256", name + ".sig"]


def build_arguments(version, emulator=False):
    arguments = {"ACCEPT_ANDROID_SDK_LICENSE": "1", "TOMACCO_VERSION": version}
    if emulator:
        arguments["ANDROID_EMULATOR"] = "1"
    return arguments


def build_command(tag, context, arguments):
    argv = ["docker", "build", "--tag", tag]
    for key, value in arguments.items():
        argv += ["--build-arg", f"{key}={value}"]
    return argv + [str(context)]


def prepare_context(output, packages, version):
    """A build context with the Dockerfile and the local signed package in packages/."""
    context = output / "context"
    if context.exists():
        shutil.rmtree(context)
    (context / "packages").mkdir(parents=True)
    shutil.copy2(CONTEXT / "Dockerfile", context / "Dockerfile")
    for name in package_files(version):
        source = packages / name
        if not source.is_file():
            raise Failed(f"No {name} in {packages}: the image is built only from a package with its checksum and signature")
        shutil.copy2(source, context / "packages" / name)
    return context


def build(options):
    if not options.accept_android_sdk_license:
        raise Failed("The image contains the Android SDK. Building it means you accept Google's Android SDK license "
                     "(https://developer.android.com/studio/terms); say so with --accept-android-sdk-license.")
    version = options.version or project_version()
    tag = options.tag or image_name(version, options.emulator)
    context = CONTEXT
    if options.packages:
        context = prepare_context(Path(options.output).resolve(), Path(options.packages).resolve(), version)
    started = time.monotonic()
    run(build_command(tag, context, build_arguments(version, options.emulator)))
    print(f"Built {tag} in {time.monotonic() - started:.0f}s, {image_size(tag)}")
    return tag


def image_size(tag):
    done = subprocess.run(["docker", "image", "inspect", "--format", "{{.Size}}", tag], capture_output=True, text=True, check=False)
    try:
        return f"{int(done.stdout.strip()) / 1e9:.2f} GB"
    except ValueError:
        return "size unknown"


# ---------------------------------------------------------------------------
# MCP over standard input and output
# ---------------------------------------------------------------------------

def mcp_requests():
    """initialize, the initialized notification and tools/list: what a client sends first."""
    return [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "build_container", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]


def check_mcp_reply(text):
    """Reads the server's answers, one JSON object per line, and returns (server version, tool count)."""
    answers = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        if "id" in message:
            answers[message["id"]] = message
    initialize, listing = answers.get(1), answers.get(2)
    if not initialize or "result" not in initialize:
        raise Failed(f"No answer to initialize: {text[:300]!r}")
    server = initialize["result"].get("serverInfo", {})
    if server.get("name") != "tomacco":
        raise Failed(f"The server is not tomacco: {server}")
    if not listing or "result" not in listing:
        raise Failed(f"No answer to tools/list: {text[:300]!r}")
    names = {tool["name"] for tool in listing["result"].get("tools", [])}
    missing = [name for name in MCP_TOOLS if name not in names]
    if missing:
        raise Failed("tools/list lacks " + ", ".join(missing))
    return server.get("version", "?"), len(names)


def mcp(options, runner=subprocess.run):
    """Pipes the first requests into `docker run -i --rm IMAGE mcp`, as an MCP client does."""
    payload = "".join(json.dumps(request) + "\n" for request in mcp_requests())
    argv = ["docker", "run", "-i", "--rm", options.image, "mcp"]
    print("+", shlex.join(argv), "< initialize, tools/list", flush=True)
    started = time.monotonic()
    done = runner(argv, input=payload, capture_output=True, text=True, timeout=120, check=False)
    if done.returncode:
        raise Failed(f"{shlex.join(argv)} failed with {done.returncode}: {done.stderr.strip()[:300]}")
    version, count = check_mcp_reply(done.stdout)
    print(f"MCP: tomacco {version} answered initialize and listed {count} tools in {time.monotonic() - started:.1f}s")
    return version, count


# ---------------------------------------------------------------------------
# Checks in a running container
# ---------------------------------------------------------------------------

class Container:
    """A long-lived container for the checks: jobs run as workers beside the
    call that started them, so they need the container to stay up."""

    def __init__(self, image, data, name, kvm=False):
        self.image, self.data, self.name, self.kvm = image, data, name, kvm

    def start_command(self):
        # The host user owns the data directory, so the container runs as that user.
        argv = ["docker", "run", "-d", "--rm", "--name", self.name, "--user", f"{os.getuid()}:{os.getgid()}",
                "-e", "HOME=/data/home", "-v", f"{self.data}:/data"]
        if self.kvm:
            argv += ["--device", "/dev/kvm", "--group-add", str(os.stat("/dev/kvm").st_gid)]
        return argv + ["--entrypoint", "sleep", self.image, "infinity"]

    def start(self):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, check=False)
        run(self.start_command(), stdout=subprocess.DEVNULL)

    def stop(self):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, check=False)

    def tomaccoctl(self, *arguments, timeout=1800):
        argv = ["docker", "exec", self.name, "tomaccoctl", *arguments]
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        return done.returncode, done.stdout, done.stderr

    def call(self, operation, arguments, wait=False, timeout=1800):
        extra = ["--wait"] if wait else []
        code, out, err = self.tomaccoctl("call", operation, json.dumps(arguments), "--compact", *extra, timeout=timeout)
        try:
            envelope = json.loads(out)
        except json.JSONDecodeError:
            raise Failed(f"{operation} printed no JSON (exit {code}): {(out or err)[-400:]}")
        if code or not envelope.get("ok"):
            raise Failed(f"{operation} failed: {json.dumps(envelope.get('error') or envelope.get('result'))[:600]}\n{err[-800:]}")
        return envelope["result"]


class Checks:
    def __init__(self):
        self.results = []

    def check(self, label, action):
        started = time.monotonic()
        try:
            detail = action()
        except (Failed, subprocess.TimeoutExpired, OSError) as error:
            self.results.append((label, False, time.monotonic() - started, str(error)))
            print(f"  {label}: FAILED after {time.monotonic() - started:.0f}s: {error}", flush=True)
            return False
        self.results.append((label, True, time.monotonic() - started, detail or ""))
        print(f"  {label}: ok ({time.monotonic() - started:.0f}s) {detail or ''}", flush=True)
        return True

    @property
    def failed(self):
        return [label for label, ok, _, _ in self.results if not ok]


def wait_for_boot(container, device, seconds=600):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        devices = container.call("device.list", {"platform": "android"})["devices"]
        if any(entry["id"] == device and entry.get("booted") for entry in devices):
            return f"{device} booted"
        time.sleep(5)
    raise Failed(f"{device} did not finish booting in {seconds}s")


def verify(options):
    image = options.image
    output = Path(options.output).resolve()
    data = output / "verify-data"
    if data.exists() and not options.keep_data:
        shutil.rmtree(data)
    (data / "home").mkdir(parents=True, exist_ok=True)
    data.chmod(0o700)
    checks = Checks()
    print(f"Checking {image} ({image_size(image)})")

    def plain(*arguments):
        def action():
            done = subprocess.run(["docker", "run", "--rm", image, *arguments], capture_output=True, text=True, timeout=300, check=False)
            if done.returncode:
                raise Failed(f"exit {done.returncode}: {(done.stdout + done.stderr)[-400:]}")
            return done.stdout
        return action

    checks.check("version", lambda: plain("--version")().strip())

    def doctor():
        result = json.loads(plain("doctor", "--compact")())["result"]
        tools = {tool["name"] for tool in result["tools"] if tool["available"]}
        missing = [name for name in ("java", "gradle", "adb", "sdkmanager", "avdmanager", "apksigner") if name not in tools]
        if missing:
            raise Failed("doctor does not find " + ", ".join(missing))
        if options.emulator and "emulator" not in tools:
            raise Failed("doctor does not find a working emulator")
        return f"android_sdk={result['android_sdk']}"
    checks.check("doctor", doctor)

    def capabilities():
        result = json.loads(plain("call", "capabilities", "--compact")())["result"]
        if not result["android"]["build"]["ready"]:
            raise Failed(f"Android builds are not ready: {result['android']['build']}")
        return f"android.build ready, emulator ready={result['android']['emulator']['ready']}"
    checks.check("capabilities", capabilities)
    checks.check("mcp", lambda: "tomacco %s, %d tools" % mcp(argparse.Namespace(image=image)))

    container = Container(image, data, f"{IMAGE}-verify-{os.getpid()}", kvm=options.emulator)
    project = "/data/projects/FirstApp"
    try:
        container.start()
        checks.check("project.create", lambda: container.call(
            "project.create", {"path": project, "kind": "compose", "name": "FirstApp", "organization": "dev.example"})["created"])

        def build_apk():
            job = container.call("project.execute", {"path": project, "platform": "android", "action": "build", "format": "apk"}, wait=True)
            artifacts = [a["path"] for a in job.get("result", {}).get("artifacts", []) if a["path"].endswith(".apk")]
            if job["state"] != "succeeded" or not artifacts:
                raise Failed(f"the build ended {job['state']} without an APK")
            return artifacts[0]
        built = checks.check("project.execute build", build_apk)
        if options.emulator and built:
            device = "emulator-5554"
            started = (checks.check("android.avd_create", lambda: container.call(
                "android.avd_create", {"name": "Tomacco", "system_image": SYSTEM_IMAGE, "profile_id": "pixel-11"}, wait=True)["state"])
                and checks.check("android.avd_start headless", lambda: container.call(
                    "android.avd_start", {"name": "Tomacco", "headless": True, "gpu": "auto"})["state"])
                and checks.check("boot", lambda: wait_for_boot(container, device)))
            if started:
                checks.check("project.run --wait", lambda: container.call(
                    "project.run", {"path": project, "platform": "android", "device": device}, wait=True)["state"])

                def capture():
                    time.sleep(3)
                    shot = container.call("device.capture", {"platform": "android", "device": device, "path": "/data/capture.png"})
                    with open(data / "capture.png", "rb") as png:
                        if png.read(8) != b"\x89PNG\r\n\x1a\n":
                            raise Failed("the capture is not a PNG")
                    return f"{shot['bytes']} bytes in {data / 'capture.png'}"
                checks.check("device.capture", capture)
                checks.check("emulator stop", lambda: container.call("android.emulator", {"device": device, "action": "stop"})["action"])
    finally:
        container.stop()
    print(f"\n{image}:")
    for label, ok, seconds, detail in checks.results:
        print(f"  {'ok  ' if ok else 'FAIL'} {label} ({seconds:.0f}s)")
    if checks.failed:
        raise Failed("Failed checks: " + ", ".join(checks.failed))
    return checks.results


def push_commands(image, version, registry=REGISTRY, emulator=False):
    """The commands that would publish an image. Printed for a person; never run here."""
    target = f"{registry}:{version}" + ("-emulator" if emulator else "")
    return [["docker", "tag", image, target], ["docker", "push", target]]


def print_push(options):
    version = options.version or project_version()
    image = options.image or image_name(version, options.emulator)
    print("To publish, a person would run (nothing was pushed):")
    for argv in push_commands(image, version, options.registry, options.emulator):
        print("  " + shlex.join(argv))


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    making = commands.add_parser("build", help="Build the image")
    making.add_argument("--accept-android-sdk-license", action="store_true",
                        help="Accept Google's Android SDK license, which the image's SDK is under")
    making.add_argument("--emulator", action="store_true", help="The variant with the emulator and an x86_64 system image")
    making.add_argument("--version", help="The release whose package to install (default: the version in CMakeLists.txt)")
    making.add_argument("--packages", help="A directory with the signed package, its .sha256 and .sig, instead of the download")
    making.add_argument("--tag", help=f"The image's tag (default: {IMAGE}:VERSION or {IMAGE}:VERSION-emulator)")
    making.add_argument("--output", default=str(ROOT / OUTPUT), help=f"Scratch directory (default: {OUTPUT}/)")
    checking = commands.add_parser("verify", help="Run the checks against an image")
    checking.add_argument("image")
    checking.add_argument("--emulator", action="store_true", help="Also start the emulator with /dev/kvm, run the app and capture it")
    checking.add_argument("--output", default=str(ROOT / OUTPUT), help=f"Scratch directory for the data volume (default: {OUTPUT}/)")
    checking.add_argument("--keep-data", action="store_true", help="Reuse the data directory (Gradle's caches) of an earlier run")
    talking = commands.add_parser("mcp", help="Send initialize and tools/list to `docker run -i --rm IMAGE mcp`")
    talking.add_argument("image")
    pushing = commands.add_parser("push-commands", help="Print the commands that would publish the image; runs nothing")
    pushing.add_argument("--image")
    pushing.add_argument("--version")
    pushing.add_argument("--emulator", action="store_true")
    pushing.add_argument("--registry", default=REGISTRY)
    options = parser.parse_args(arguments)
    try:
        {"build": build, "verify": verify, "mcp": mcp, "push-commands": print_push}[options.command](options)
    except (Failed, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"build_container: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
