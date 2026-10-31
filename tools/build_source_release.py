#!/usr/bin/env python3
"""Build the corresponding-source tarball a release offers (docs/SOURCE.md.in).

The tarball is `git archive` of HEAD plus `cargo vendor` of its Cargo.lock, so
only what the commit tracks goes in: not qa/, .tooling/ or a working tree's
other contents. What it does not hold is listed in its SOURCE_RELEASE.md. The
same commit and crates give the same bytes: names, order, owners and times do
not depend on the machine or the moment.
"""
import argparse
import gzip
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

REPOSITORY = Path(__file__).resolve().parents[1]
# Local state a commit must never carry into a public tarball.
FORBIDDEN = ("qa", ".tooling", "target", "references", "backups")
# Apple's containers, and Mach-O in either byte order (thin or universal).
APPLE_SUFFIXES = (".ipsw", ".dmg", ".im4p", ".img4", ".ipa", ".kernelcache")
MACHO_MAGIC = tuple(bytes.fromhex(value) for value in (
    "feedface", "cefaedfe", "feedfacf", "cffaedfe", "cafebabe", "bebafeca"))
# A recipe's pin: a source URL followed by its 40-hex commit, as a tuple
# ("url", "commit") or as UPSTREAM = "url" and REVISION = "commit".
PIN = re.compile(r'"(https://[^"\s]+)"[,\s]*(?:[A-Z_]+\s*=\s*)?"([0-9a-f]{40})"')


class Refused(RuntimeError):
    """The tarball cannot be built as asked; the message says why."""


def git(repository, *arguments):
    done = subprocess.run(["git", "-C", str(repository), *arguments], capture_output=True, text=True)
    if done.returncode:
        raise Refused(f"git {arguments[0]}: {done.stderr.strip()[:300]}")
    return done.stdout


def pinned_sources(tools):
    """The upstream sources the recipes in `tools` fetch: [(name, URL, commit,
    recipe file)], read from the recipes so that a new pin cannot be left out."""
    found = []
    for path in sorted(Path(tools).glob("*.py")):
        for url, commit in PIN.findall(path.read_text(errors="replace")):
            name = url.rstrip("/").rpartition("/")[2].removesuffix(".git")
            found.append((name, url, commit, path.name))
    return sorted(set(found))


def apple_data(tree):
    """Files of `tree` that are or may hold Apple's data, by name or by magic."""
    found = []
    for path in sorted(tree.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() in APPLE_SUFFIXES:
            found.append(path.relative_to(tree).as_posix())
            continue
        with path.open("rb") as stream:
            if stream.read(4) in MACHO_MAGIC:
                found.append(path.relative_to(tree).as_posix())
    return found


def notes(version, commit, dirty, pins):
    """SOURCE_RELEASE.md: what this tarball is and what it does not hold."""
    rows = "\n".join(f"| {name} | {url} | `{revision}` | `tools/{recipe}` |" for name, url, revision, recipe in pins)
    state = ("\nThe working tree this was made from had uncommitted changes (`--allow-dirty`). They are not in "
             "this tarball, which holds the commit; do not offer it for a release.\n" if dirty else "")
    return f"""# Tomacco {version}: corresponding source

Commit `{commit}`.
{state}
## What this holds

- Every file Tomacco tracks at this commit (`git archive`).
- `vendor/`: every crate `Cargo.lock` pins, from `cargo vendor`, for all
  platforms, and `.cargo/config.toml`, which makes Cargo build from them.

To build without a network, run `cmake -B build` and `cmake --build build`
inside this directory: Cargo finds `.cargo/config.toml` from a build directory
below it. `cargo build --release --locked --offline` builds `tomaccoctl` alone.

## What you need separately

No iOS engine, Apple software or other third-party program is distributed with
Tomacco, so none of these is part of the binaries this tarball
corresponds to.

The recipes in `tools/` fetch and build these upstream commits on your machine;
the patches they apply are in `integrations/ios-emulator`. To read one without
running a recipe:

    git clone <source> && git -C <directory> checkout <commit>

| Name | Source | Commit | Recipe |
| --- | --- | --- | --- |
{rows}

`tools/prepare_ios_emulator.py` also downloads helper programs it names with
their digests. Qt and FFmpeg come from your distribution, which provides their
source.

## Apple data

None is included. You supply your own iOS restore image; what the recipes
extract from it stays on your machine.

Local state is left out as well: `qa/` and `.tooling/` are not tracked.
"""


def pack(stage, name, output, modified):
    """A gzipped tar of `stage`/`name` whose bytes depend only on the content."""
    tree = stage / name
    entries = sorted((path for path in tree.rglob("*") if path.is_symlink() or path.is_file()),
                     key=lambda path: path.relative_to(tree).as_posix())
    with output.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", compresslevel=9, fileobj=raw, mtime=0) as packed:
        with tarfile.open(fileobj=packed, mode="w", format=tarfile.GNU_FORMAT) as archive:
            for path in entries:
                info = tarfile.TarInfo(f"{name}/{path.relative_to(tree).as_posix()}")
                info.mtime, info.uid, info.gid, info.uname, info.gname = modified, 0, 0, "", ""
                if path.is_symlink():
                    info.type, info.linkname, info.mode = tarfile.SYMTYPE, os.readlink(path), 0o777
                    archive.addfile(info)
                    continue
                info.size = path.stat().st_size
                info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                with path.open("rb") as stream:
                    archive.addfile(info, stream)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build(repository, output, allow_dirty=False, offline=False, cargo=None):
    """Write <name>.tar.gz and <name>.tar.gz.sha256 into `output`; their paths
    and the digest. Refused names what stands in the way."""
    repository, output = Path(repository).resolve(), Path(output).resolve()
    dirty = bool(git(repository, "status", "--porcelain").strip())
    if dirty and not allow_dirty:
        raise Refused("The working tree has uncommitted changes, which the tarball would not hold; "
                      "commit them, or pass --allow-dirty for a tarball of HEAD that is not for a release")
    commit = git(repository, "rev-parse", "HEAD").strip()
    modified = int(git(repository, "log", "-1", "--format=%ct", "HEAD").strip())
    version = re.search(r"project\(\s*Tomacco\s+VERSION\s+([0-9][0-9.]*)", git(repository, "show", "HEAD:CMakeLists.txt"))
    if version is None:
        raise Refused("CMakeLists.txt at HEAD names no project version")
    version = version.group(1)
    name = f"tomacco-{version}-source"
    cargo = cargo or shutil.which("cargo") or str(repository / ".tooling/cargo/bin/cargo")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".source-release-", dir=output) as directory:
        stage = Path(directory)
        archive = stage / "head.tar"
        with archive.open("wb") as stream:
            done = subprocess.run(["git", "-C", str(repository), "archive", "--format=tar", f"--prefix={name}/", "HEAD"],
                                  stdout=stream, stderr=subprocess.PIPE, text=True)
        if done.returncode:
            raise Refused(f"git archive: {done.stderr.strip()[:300]}")
        with tarfile.open(archive) as tar:
            tar.extractall(stage, filter="tar")
        archive.unlink()
        tree = stage / name
        carried = [part for part in FORBIDDEN if (tree / part).exists()]
        if carried:
            raise Refused("HEAD tracks local state that must not be published: " + ", ".join(carried))
        apple = apple_data(tree)
        if apple:
            raise Refused("HEAD tracks files that may hold Apple's data: " + ", ".join(apple[:20]))
        if (tree / "vendor").exists() or (tree / ".cargo").exists():
            raise Refused("HEAD tracks vendor/ or .cargo/, which this tool writes")
        # Run in the export, not the checkout: it is HEAD's Cargo.lock that counts.
        command = [cargo, "vendor", "--locked", "--versioned-dirs", *(["--offline"] if offline else []), "vendor"]
        try:
            done = subprocess.run(command, cwd=tree, capture_output=True, text=True)
        except OSError as error:
            raise Refused(f"cargo vendor cannot run: {error}") from None
        if done.returncode:
            raise Refused("cargo vendor failed"
                          + (" with the local registry alone (--offline)" if offline else "")
                          + ": " + done.stderr.strip()[-1500:])
        (tree / ".cargo").mkdir()
        (tree / ".cargo/config.toml").write_text(done.stdout)
        (tree / "SOURCE_RELEASE.md").write_text(notes(version, commit, dirty, pinned_sources(tree / "tools")))
        temporary = stage / f"{name}.tar.gz"
        pack(stage, name, temporary, modified)
        tarball = output / temporary.name
        os.replace(temporary, tarball)
    value = digest(tarball)
    checksum = output / f"{tarball.name}.sha256"
    checksum.write_text(f"{value}  {tarball.name}\n")
    return tarball, checksum, value


def main(arguments=None):
    parser = argparse.ArgumentParser(description="Build the corresponding-source tarball of HEAD for a release")
    parser.add_argument("--output", type=Path, required=True, help="Directory for the tarball and its .sha256")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="Accept uncommitted changes; the tarball still holds HEAD and is not for a release")
    parser.add_argument("--offline", action="store_true", help="Vendor from the local Cargo registry alone")
    parser.add_argument("--cargo", help="The cargo executable (default: cargo on PATH, then this checkout's)")
    parser.add_argument("--repository", type=Path, default=REPOSITORY, help=argparse.SUPPRESS)
    options = parser.parse_args(arguments)
    try:
        tarball, checksum, value = build(options.repository, options.output, options.allow_dirty, options.offline, options.cargo)
    except Refused as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    print(tarball)
    print(checksum)
    print(f"SHA-256 {value}")
    print(f"{tarball.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
