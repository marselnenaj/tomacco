"""The provenance of a release: an in-toto Statement (v1) with a SLSA
provenance (v1) predicate that says what make_release.py built, from which
commit and inputs, on which host, and the SHA-256 of every file it produced.

make_release.py writes it as `<prefix>-<version>-provenance.intoto.json` and
signs it with the release key like a package, so `tomaccoctl verify` checks
its signature and the files it lists. It is a plain JSON file with a detached
signature, not a DSSE envelope, and the builder is the maintainer's computer,
which no third party attests."""
import datetime
import hashlib
import platform
import re
import sys
from pathlib import Path

import build_sbom

STATEMENT = "https://in-toto.io/Statement/v1"
PREDICATE = "https://slsa.dev/provenance/v1"
SUFFIX = "-provenance.intoto.json"
REPOSITORY = "https://github.com/marselnenaj/tomacco"
PORTABLE_DOCKERFILE = build_sbom.PORTABLE_DOCKERFILE
# The tools whose versions the provenance records, each with the command that
# prints its version. Docker only builds the portable package.
TOOLS = {
    "rustc": ["rustc", "--version"],
    "cargo": ["cargo", "--version"],
    "cmake": ["cmake", "--version"],
    "ninja": ["ninja", "--version"],
    "c++": ["c++", "--version"],
    "openssl": ["openssl", "version"],
    "git": ["git", "--version"],
}
PORTABLE_TOOLS = {"docker": ["docker", "--version"]}


def file_digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def name(prefix, version):
    return f"{prefix}-{version}{SUFFIX}"


def subjects(dist, own_name):
    """Every file the release offers except checksums, signatures and the
    provenance itself, which name or sign files listed here."""
    return [{"name": path.name, "digest": {"sha256": file_digest(path)}}
            for path in sorted(dist.iterdir())
            if path.is_file() and path.name != own_name and not path.name.endswith((".sha256", ".sig"))]


def os_name(release=Path("/etc/os-release")):
    try:
        found = re.search(r'^PRETTY_NAME="?([^"\n]*)"?$', release.read_text(), re.M)
    except OSError:
        return platform.system()
    return found.group(1) if found else platform.system()


def builder(read, portable):
    """{tool: first line of its version}; a tool that cannot say is left out."""
    versions = {"os": os_name(), "kernel": platform.release(), "machine": platform.machine(),
                "python": sys.version.split()[0]}
    for tool, argv in {**TOOLS, **(PORTABLE_TOOLS if portable else {})}.items():
        try:
            output = read(argv).decode(errors="replace").strip()
        except Exception:  # Refused, a missing program: the provenance says less, the release goes on.
            continue
        if output:
            versions[tool] = output.splitlines()[0]
    return versions


def dockerfile_inputs(root):
    """The downloads packaging/portable/Dockerfile pins: base image, CMake,
    rustup and FFmpeg, each with its digest."""
    text = (root / PORTABLE_DOCKERFILE).read_text()
    argument = lambda key: build_sbom.dockerfile_argument(root, key)
    found = []
    image = re.search(r"^FROM (\S+)@sha256:([0-9a-f]{64})$", text, re.M)
    if image:
        found.append({"uri": f"docker://{image.group(1)}", "digest": {"sha256": image.group(2)}})
    cmake = argument("CMAKE_VERSION")
    found.append({"uri": f"https://github.com/Kitware/CMake/releases/download/v{cmake}/cmake-{cmake}-linux-x86_64.tar.gz",
                  "digest": {"sha256": argument("CMAKE_SHA256")}})
    rustup = argument("RUSTUP_VERSION")
    found.append({"uri": f"https://static.rust-lang.org/rustup/archive/{rustup}/x86_64-unknown-linux-gnu/rustup-init",
                  "digest": {"sha256": argument("RUSTUP_SHA256")}, "annotations": {"rust": argument("RUST_VERSION")}})
    ffmpeg = argument("FFMPEG_VERSION")
    found.append({"uri": f"https://ffmpeg.org/releases/ffmpeg-{ffmpeg}.tar.xz", "digest": {"sha256": argument("FFMPEG_SHA256")}})
    return found


def inputs(root, commit, tag, portable):
    """The resolved dependencies: the commit, the lock file, and for the
    portable package its Dockerfile, the Qt archives and its other downloads."""
    found = [{"uri": f"git+{REPOSITORY}@refs/tags/{tag}", "digest": {"gitCommit": commit}},
             {"name": "Cargo.lock", "digest": {"sha256": file_digest(root / "Cargo.lock")}}]
    if portable:
        found.append({"name": PORTABLE_DOCKERFILE, "digest": {"sha256": file_digest(root / PORTABLE_DOCKERFILE)}})
        found += [{"uri": url, "digest": {"sha256": digest}} for _, _, digest, url in build_sbom.qt_archives(root)]
        found += dockerfile_inputs(root)
    return found


def stamp(moment):
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def statement(dist, own_name, commit, tag, epoch, resolved, versions, parameters, started, finished):
    return {
        "_type": STATEMENT,
        "subject": subjects(dist, own_name),
        "predicateType": PREDICATE,
        "predicate": {
            "buildDefinition": {
                "buildType": f"{REPOSITORY}/blob/{tag}/tools/make_release.py",
                "externalParameters": {"repository": REPOSITORY, "ref": f"refs/tags/{tag}", **parameters},
                "internalParameters": {"SOURCE_DATE_EPOCH": str(epoch)},
                "resolvedDependencies": resolved,
            },
            "runDetails": {
                "builder": {"id": f"{REPOSITORY}/blob/{tag}/docs/building.md#making-a-release", "version": versions},
                "metadata": {"invocationId": f"{tag}@{stamp(started)}", "startedOn": stamp(started),
                             "finishedOn": stamp(finished)},
            },
        },
    }
