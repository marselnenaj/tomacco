#!/usr/bin/env python3
"""Make a draft release from a clean checkout of its tag: checks, package,
portable package and source tarball with checksums, signatures, a signed
CycloneDX SBOM per package, then `gh release create --draft`. Publishing the
draft stays a person's decision.

With --container it also builds the headless image from the signed package
(tools/build_container.py), checks it, and prints the `docker push` commands
that would publish it. It never pushes."""
import argparse
import base64
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import build_container
import build_sbom

ROOT = Path(__file__).resolve().parents[1]
KEY_ENVIRONMENT = "TOMACCO_RELEASE_SIGNING_KEY_FILE"
BUILD = "build-product"
# tools/build_portable.py's output: the package with Qt and FFmpeg bundled.
PORTABLE = "build-portable"
DIST = "dist"
NOTES = "dist-notes/body.md"
# The source of the Qt modules the portable package bundles, which
# --with-qt-sources mirrors into the release (LGPL-3.0's source offer).
QT_SOURCES = "packaging/portable/qt-sources.txt"


class Refused(Exception):
    pass


class Commands:
    """Runs commands. `read` also runs in a dry run, so it must only ever look."""

    def __init__(self, root, dry_run=False):
        self.root = root
        self.dry_run = dry_run
        # Variables added to the environment of `run`.
        self.environment = {}

    def read(self, argv):
        done = subprocess.run(argv, cwd=self.root, capture_output=True, check=False)
        if done.returncode:
            raise Refused(f"{shlex.join(argv)} failed: {done.stderr.decode(errors='replace').strip()}")
        return done.stdout

    def run(self, argv):
        if self.dry_run:
            print("Would run:", shlex.join(argv))
            return
        print("+", shlex.join(argv), flush=True)
        if subprocess.run(argv, cwd=self.root, check=False, env={**os.environ, **self.environment}).returncode:
            raise Refused(f"{shlex.join(argv)} failed")


def constant(root, name, required=True):
    found = re.search(rf'pub const {name}: &str = "([^"]*)";', (root / "native/product.rs").read_text())
    if not found:
        if not required:
            return ""
        raise Refused(f"native/product.rs has no {name}")
    return found.group(1)


def trusted_keys(root):
    """The release keys the released program accepts: the current one and,
    during a rotation, the next one (base64 of 32 bytes each)."""
    current = constant(root, "RELEASE_SIGNING_PUBLIC_KEY_BASE64")
    if not current:
        raise Refused("native/product.rs has no public key, so this release could not update anything")
    upcoming = constant(root, "NEXT_RELEASE_SIGNING_PUBLIC_KEY_BASE64", required=False)
    return [current] + ([upcoming] if upcoming else [])


def versions(root):
    """The version each file states; a release needs them to agree."""
    cmake = re.search(r"project\(\s*\w+\s+VERSION\s+([0-9][0-9.]*)", (root / "CMakeLists.txt").read_text())
    cargo = re.search(r'(?m)^version = "([^"]+)"', (root / "Cargo.toml").read_text())
    if not cmake or not cargo:
        raise Refused("CMakeLists.txt or Cargo.toml names no version")
    return {"CMakeLists.txt": cmake.group(1), "Cargo.toml": cargo.group(1)}


def check_version(root, tag):
    stated = versions(root)
    if len(set(stated.values())) != 1:
        raise Refused(f"The versions disagree: {stated}")
    version = next(iter(stated.values()))
    if tag and tag != f"v{version}":
        raise Refused(f"The tag {tag} does not match version {version}")
    for metainfo in sorted((root / "assets").glob("*.metainfo.xml")):
        if f'version="{version}"' not in metainfo.read_text():
            raise Refused(f"{metainfo.name} has no <release> for version {version}")
    return version


def check_tree(commands, tag):
    dirty = commands.read(["git", "status", "--porcelain"]).decode().strip()
    if dirty:
        raise Refused(f"The tree is not clean:\n{dirty}")
    if not commands.read(["git", "tag", "--list", tag]).strip():
        raise Refused(f"There is no tag {tag} yet: make it on the commit to release with `git tag {tag}`")
    head = commands.read(["git", "rev-parse", "HEAD"]).strip()
    tagged = commands.read(["git", "rev-parse", f"{tag}^{{commit}}"]).strip()
    if head != tagged:
        raise Refused(f"HEAD is not the commit of {tag}")


def key_path(given, environment, root):
    value = given or environment.get(KEY_ENVIRONMENT)
    if not value:
        raise Refused(f"Name the signing key with --key or {KEY_ENVIRONMENT}")
    path = Path(value).expanduser().resolve()
    if path.is_relative_to(root.resolve()):
        raise Refused("The signing key must stay outside the repository")
    if not path.is_file():
        raise Refused(f"No signing key at {path}")
    if path.stat().st_mode & 0o077:
        raise Refused(f"The signing key must be readable only by its owner: {path}")
    return path


def public_key_der(commands, key):
    return commands.read(["openssl", "pkey", "-in", str(key), "-pubout", "-outform", "DER"])


def check_key(commands, root, key):
    """A signature the released program would refuse is no use to anyone.
    During a rotation the next key may sign, too: the program trusts both."""
    expected = trusted_keys(root)
    actual = base64.b64encode(public_key_der(commands, key)[-32:]).decode()
    if actual not in expected:
        raise Refused("This signing key does not belong to a public key in native/product.rs")


def portable_name(prefix, version):
    return f"{prefix}-{version}-Linux-x86_64-portable.tar.gz"


def build(commands, root, tag, portable=None):
    """`portable` is the portable package's file name, or None to make none."""
    python = sys.executable
    steps = [
        [python, "tools/build_release_notes.py", "--tag", tag, "--output", NOTES],
        ["cargo", "fmt", "--check"],
        ["cargo", "clippy", "--locked", "--all-targets", "--", "-D", "warnings"],
        # Licenses, duplicate and yanked crates, the notices' crate tables, and cargo-deny when installed.
        [python, "tools/check_dependencies.py"],
        ["cmake", "-S", ".", "-B", BUILD, "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release"],
        ["cmake", "--build", BUILD],
        ["ctest", "--test-dir", BUILD, "--output-on-failure"],
        ["cmake", "--build", BUILD, "--target", "package"],
    ]
    if portable:
        # Built in its container, then started in clean containers of the
        # distributions it is for.
        steps += [[python, "tools/build_portable.py", "build", "--output", PORTABLE],
                  [python, "tools/build_portable.py", "verify", f"{PORTABLE}/{portable}"]]
    steps.append([python, "tools/build_source_release.py", "--output", DIST])
    for argv in steps:
        commands.run(argv)


def collect_package(root, prefix, version, directory=BUILD, pattern=None):
    """Moves the package and its checksum into dist, and returns the package."""
    packages = sorted((root / directory).glob(pattern or f"{prefix}-{version}-Linux-*.tar.gz"))
    if len(packages) != 1:
        raise Refused(f"Expected one package in {directory}, found {len(packages)}")
    package = packages[0]
    checksum = package.with_name(package.name + ".sha256")
    if not checksum.is_file():
        raise Refused(f"{package.name} has no checksum")
    for file in (package, checksum):
        shutil.move(file, root / DIST / file.name)
    return root / DIST / package.name


def verify_checksums(root):
    checksums = sorted((root / DIST).glob("*.sha256"))
    if not checksums:
        raise Refused("There are no checksums to verify")
    for checksum in checksums:
        expected = checksum.read_text().split()[0]
        target = checksum.with_suffix("")
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise Refused(f"{target.name} does not match its checksum")


def qt_sources(root):
    """[(SHA-256, URL)] from packaging/portable/qt-sources.txt."""
    path = root / QT_SOURCES
    if not path.is_file():
        raise Refused(f"There is no {QT_SOURCES}")
    found = []
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        digest, url = line.split()
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not url.startswith("https://"):
            raise Refused(f"{QT_SOURCES}: not a SHA-256 and an https URL: {line}")
        found.append((digest, url))
    return found


def mirror_qt_sources(commands, root):
    """Downloads the Qt source tarballs into dist with a .sha256 each, and
    refuses one that does not match its pinned digest."""
    for digest, url in qt_sources(root):
        name = url.rpartition("/")[2]
        target = root / DIST / name
        commands.run(["curl", "-fL", "--retry", "3", "-o", str(target), url])
        if commands.dry_run:
            continue
        with target.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest:
            target.unlink()
            raise Refused(f"{name} from {url} does not match {QT_SOURCES}: {actual}")
        target.with_name(name + ".sha256").write_text(f"{digest}  {name}\n")


def sign(commands, key, package):
    """Writes <package>.sig, base64 of the signature, and checks it."""
    with tempfile.TemporaryDirectory() as directory:
        raw = Path(directory, "signature")
        public = Path(directory, "public.der")
        commands.run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(key), "-in", str(package), "-out", str(raw)])
        public.write_bytes(public_key_der(commands, key))
        commands.read(["openssl", "pkeyutl", "-verify", "-rawin", "-pubin", "-keyform", "DER", "-inkey", str(public),
                       "-in", str(package), "-sigfile", str(raw)])
        signature = package.with_name(package.name + ".sig")
        signature.write_bytes(base64.b64encode(raw.read_bytes()))
    return signature


def bills_of_materials(commands, root, version, packages, portable, key):
    """Writes <package>.cdx.json, a CycloneDX SBOM, for each package, and
    signs it like the package."""
    metadata, linked = build_sbom.read_cargo(root, commands.read)
    epoch = commands.environment["SOURCE_DATE_EPOCH"]
    for package in packages:
        sbom = build_sbom.write(root, package, version, metadata, linked, package.name == portable, epoch)
        sign(commands, key, sbom)


def container(commands, version):
    """Builds and checks the image from the signed package in dist/, then
    prints how a person would publish it. Runs no push."""
    python = sys.executable
    image = build_container.image_name(version)
    commands.run([python, "tools/build_container.py", "build", "--accept-android-sdk-license",
                  "--version", version, "--packages", DIST, "--tag", image])
    commands.run([python, "tools/build_container.py", "verify", image])
    print("The image is built and checked locally. To publish it, a person would run (nothing was pushed):")
    for argv in build_container.push_commands(image, version):
        print("  " + shlex.join(argv))


def draft(commands, root, tag, version):
    repository = f"{constant(root, 'REPO_OWNER')}/{constant(root, 'REPO_NAME')}"
    files = [str(path.relative_to(root)) for path in sorted((root / DIST).iterdir())] if not commands.dry_run else [f"{DIST}/*"]
    commands.run(["gh", "release", "create", tag, "--repo", repository, "--draft", "--verify-tag",
                  "--title", f"{constant(root, 'PRODUCT_NAME')} {version}", "--notes-file", NOTES, *files])


def make(options, commands, root, environment):
    version = check_version(root, options.tag)
    tag = options.tag or f"v{version}"
    check_tree(commands, tag)
    key = key_path(options.key, environment, root)
    check_key(commands, root, key)
    dist = root / DIST
    if options.container and not options.accept_android_sdk_license:
        raise Refused("--container builds an image with the Android SDK: add --accept-android-sdk-license "
                      "to accept Google's Android SDK license (https://developer.android.com/studio/terms)")
    if dist.exists() and any(dist.iterdir()):
        raise Refused(f"{DIST}/ is not empty: everything in it would be published")
    prefix = constant(root, "PACKAGE_PREFIX")
    portable = None if options.no_portable else portable_name(prefix, version)
    # Reproducible packages: CPack and Qt's rcc take their time stamps from
    # the release commit instead of the moment of the build.
    commands.environment.setdefault(
        "SOURCE_DATE_EPOCH", commands.read(["git", "log", "-1", "--format=%ct", "HEAD"]).decode().strip())
    if options.with_qt_sources:
        if not portable:
            raise Refused("--with-qt-sources mirrors the source of the Qt in the portable package; leave out --no-portable")
        qt_sources(root)
    build(commands, root, tag, portable)
    if options.with_qt_sources:
        mirror_qt_sources(commands, root)
    if commands.dry_run:
        print(f"Would move {BUILD}/{prefix}-{version}-Linux-*.tar.gz and its checksum to {DIST}/, verify the checksums")
        print(f"Would sign the package with {key} into {DIST}/{prefix}-{version}-Linux-*.tar.gz.sig, and verify the signature")
        if portable:
            print(f"Would move {PORTABLE}/{portable} and its checksum to {DIST}/, and sign it into {DIST}/{portable}.sig the same way")
        print(f"Would write a CycloneDX SBOM beside each package ({build_sbom.SUFFIX}) and sign it the same way")
    else:
        packages = [collect_package(root, prefix, version)]
        if portable:
            packages.append(collect_package(root, prefix, version, PORTABLE, portable))
        verify_checksums(root)
        for package in packages:
            sign(commands, key, package)
        bills_of_materials(commands, root, version, packages, portable, key)
    if options.container:
        container(commands, version)
    draft(commands, root, tag, version)


def main(arguments=None, commands=None, root=ROOT, environment=os.environ):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="The release's tag (default: v and the version in CMakeLists.txt)")
    parser.add_argument("--key", help=f"The signing key's path, outside this repository (or {KEY_ENVIRONMENT})")
    parser.add_argument("--dry-run", action="store_true", help="Only print what it would do")
    parser.add_argument("--no-portable", action="store_true",
                        help="Leave out the portable package (tools/build_portable.py, which needs Docker)")
    parser.add_argument("--with-qt-sources", action="store_true",
                        help=f"Also release the source tarballs of the Qt modules the portable package bundles ({QT_SOURCES})")
    parser.add_argument("--container", action="store_true",
                        help="Also build and check the headless image locally (tools/build_container.py) and print "
                             "the docker push commands; it pushes nothing")
    parser.add_argument("--accept-android-sdk-license", action="store_true",
                        help="Accept Google's Android SDK license, which --container's image needs")
    options = parser.parse_args(arguments)
    commands = commands or Commands(root, options.dry_run)
    try:
        make(options, commands, root, environment)
    except Refused as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
