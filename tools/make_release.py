#!/usr/bin/env python3
"""Make a draft release from a clean checkout of its tag: checks, package and
source tarball with checksums, signature, then `gh release create --draft`.
Publishing the draft stays a person's decision."""
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

ROOT = Path(__file__).resolve().parents[1]
KEY_ENVIRONMENT = "TOMACCO_RELEASE_SIGNING_KEY_FILE"
BUILD = "build-product"
DIST = "dist"
NOTES = "dist-notes/body.md"


class Refused(Exception):
    pass


class Commands:
    """Runs commands. `read` also runs in a dry run, so it must only ever look."""

    def __init__(self, root, dry_run=False):
        self.root = root
        self.dry_run = dry_run

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
        if subprocess.run(argv, cwd=self.root, check=False).returncode:
            raise Refused(f"{shlex.join(argv)} failed")


def constant(root, name):
    found = re.search(rf'pub const {name}: &str = "([^"]*)";', (root / "native/product.rs").read_text())
    if not found:
        raise Refused(f"native/product.rs has no {name}")
    return found.group(1)


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
    """A signature the released program would refuse is no use to anyone."""
    expected = constant(root, "RELEASE_SIGNING_PUBLIC_KEY_BASE64")
    if not expected:
        raise Refused("native/product.rs has no public key, so this release could not update anything")
    actual = base64.b64encode(public_key_der(commands, key)[-32:]).decode()
    if actual != expected:
        raise Refused("This signing key does not belong to the public key in native/product.rs")


def build(commands, root, tag):
    python = sys.executable
    for argv in (
        [python, "tools/build_release_notes.py", "--tag", tag, "--output", NOTES],
        ["cargo", "fmt", "--check"],
        ["cargo", "clippy", "--locked", "--all-targets", "--", "-D", "warnings"],
        ["cmake", "-S", ".", "-B", BUILD, "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release"],
        ["cmake", "--build", BUILD],
        ["ctest", "--test-dir", BUILD, "--output-on-failure"],
        ["cmake", "--build", BUILD, "--target", "package"],
        [python, "tools/build_source_release.py", "--output", DIST],
    ):
        commands.run(argv)


def collect_package(root, prefix, version):
    """Moves the package and its checksum into dist, and returns the package."""
    packages = sorted((root / BUILD).glob(f"{prefix}-{version}-Linux-*.tar.gz"))
    if len(packages) != 1:
        raise Refused(f"Expected one package in {BUILD}, found {len(packages)}")
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
    if dist.exists() and any(dist.iterdir()):
        raise Refused(f"{DIST}/ is not empty: everything in it would be published")
    build(commands, root, tag)
    prefix = constant(root, "PACKAGE_PREFIX")
    if commands.dry_run:
        print(f"Would move {BUILD}/{prefix}-{version}-Linux-*.tar.gz and its checksum to {DIST}/, verify the checksums")
        print(f"Would sign the package with {key} into {DIST}/{prefix}-{version}-Linux-*.tar.gz.sig, and verify the signature")
    else:
        package = collect_package(root, prefix, version)
        verify_checksums(root)
        sign(commands, key, package)
    draft(commands, root, tag, version)


def main(arguments=None, commands=None, root=ROOT, environment=os.environ):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="The release's tag (default: v and the version in CMakeLists.txt)")
    parser.add_argument("--key", help=f"The signing key's path, outside this repository (or {KEY_ENVIRONMENT})")
    parser.add_argument("--dry-run", action="store_true", help="Only print what it would do")
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
