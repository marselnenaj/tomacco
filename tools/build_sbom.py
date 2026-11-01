#!/usr/bin/env python3
"""Write a CycloneDX 1.6 JSON software bill of materials for a release package.

It lists what the package ships besides Tomacco's own code: the Rust crates
linked into `tomaccoctl` (from Cargo.lock, with versions, SPDX licenses and
crates.io checksums), the SQLite they embed, the bundled fonts, and for the
portable package Qt 6 with ICU, Qt WebEngine, FFmpeg and the xcb-util
libraries, with the SHA-256 of the archives they came from. Python is not in
either package: `install.py` runs on the system's Python 3.

The output depends only on its inputs: the time stamp is SOURCE_DATE_EPOCH and
the serial number is derived from the package's digest.

    tools/build_sbom.py PACKAGE.tar.gz [--portable] [--output FILE]"""
import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
import uuid
from pathlib import Path

import build_portable
import check_dependencies

ROOT = Path(__file__).resolve().parents[1]
SPEC = "1.6"
PORTABLE_DOCKERFILE = "packaging/portable/Dockerfile"
# The ICU that comes with Qt's 6.12.0 binaries; its archive name does not say.
ICU_VERSION = "73.2"
SUFFIX = ".cdx.json"
FONTS = (
    ("Archivo", "assets/fonts/archivo-var.ttf", "https://github.com/Omnibus-Type/Archivo"),
    ("JetBrains Mono", "assets/fonts/jetbrains-mono-var.ttf", "https://github.com/JetBrains/JetBrainsMono"),
)


class Failed(Exception):
    pass


def file_digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sha256(value):
    return [{"alg": "SHA-256", "content": value}]


def license_of(expression):
    """CycloneDX's form: one SPDX id, or an expression."""
    if re.fullmatch(r"[A-Za-z0-9.+-]+", expression):
        return [{"license": {"id": expression}}]
    return [{"expression": check_dependencies.SLASH.sub(" OR ", expression)}]


def lock_checksums(root):
    """{(name, version): sha256} from Cargo.lock."""
    lock = tomllib.loads((root / "Cargo.lock").read_text())
    return {(p["name"], p["version"]): p["checksum"] for p in lock.get("package", []) if p.get("checksum")}


def crate_components(metadata, linked, checksums):
    components = []
    for package in sorted((p for p in metadata["packages"] if p["id"] in linked), key=lambda p: (p["name"], p["version"])):
        name, version = package["name"], package["version"]
        component = {
            "type": "library",
            "bom-ref": f"pkg:cargo/{name}@{version}",
            "name": name,
            "version": version,
            "purl": f"pkg:cargo/{name}@{version}",
            "licenses": license_of(package["license"]),
        }
        if (name, version) in checksums:
            component["hashes"] = sha256(checksums[(name, version)])
        if package.get("repository"):
            component["externalReferences"] = [{"type": "vcs", "url": package["repository"]}]
        components.append(component)
    return components


def sqlite_version(metadata):
    """The SQLite that rusqlite's `bundled` feature compiles in, from the
    locked libsqlite3-sys's own copy of sqlite3.h."""
    for package in metadata["packages"]:
        if package["name"] == "libsqlite3-sys":
            header = Path(package["manifest_path"]).parent / "sqlite3/sqlite3.h"
            if header.is_file():
                found = re.search(r'#define SQLITE_VERSION\s+"([^"]+)"', header.read_text(errors="replace"))
                if found:
                    return found.group(1), package["version"]
    return None, None


def sqlite_component(metadata):
    version, crate = sqlite_version(metadata)
    if not crate:
        return None
    component = {"type": "library", "bom-ref": "sqlite", "name": "SQLite",
                 "description": f"Compiled into tomaccoctl from libsqlite3-sys {crate} (rusqlite's bundled feature)",
                 "licenses": [{"license": {"name": "Public Domain", "url": "https://sqlite.org/copyright.html"}}],
                 "externalReferences": [{"type": "website", "url": "https://sqlite.org"}]}
    if version:
        component["version"] = version
        component["bom-ref"] = f"sqlite@{version}"
    return component


def font_components(root):
    return [{"type": "file", "bom-ref": f"font:{path}", "name": name, "description": f"{path}, subset to Latin",
             "licenses": [{"license": {"id": "OFL-1.1"}}], "hashes": sha256(file_digest(root / path)),
             "externalReferences": [{"type": "vcs", "url": url}]}
            for name, path, url in FONTS if (root / path).is_file()]


def archives_file(architecture="x86_64"):
    """packaging/portable/qt-archives.txt, or the list of another architecture."""
    return "packaging/portable/" + build_portable.ARCHITECTURES[architecture].archives


def dockerfile(architecture="x86_64"):
    return "packaging/portable/" + build_portable.ARCHITECTURES[architecture].dockerfile


def qt_archives(root, architecture="x86_64"):
    """[(module, version, sha256, url)] from packaging/portable/qt-archives.txt
    (qt-archives-aarch64.txt for aarch64)."""
    listed = archives_file(architecture)
    base = f"https://download.qt.io/online/qtsdkrepository/{build_portable.ARCHITECTURES[architecture].qt_repository}/"
    found = []
    for line in (root / listed).read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        digest, path = line.split()
        match = re.search(r"/(\d+\.\d+\.\d+)-\d+-\d+([a-z]+)-", path)
        if not match:
            raise Failed(f"{listed}: cannot read module and version from {path}")
        found.append((match.group(2), match.group(1), digest, base + path))
    return found


def dockerfile_argument(root, name, architecture="x86_64"):
    path = dockerfile(architecture)
    found = re.search(rf"^ARG {name}=(\S+)$", (root / path).read_text(), re.M)
    if not found:
        raise Failed(f"{path} has no ARG {name}")
    return found.group(1)


def portable_components(root, architecture="x86_64"):
    components = []
    base = build_portable.ARCHITECTURES[architecture].base
    for module, version, digest, url in qt_archives(root, architecture):
        if module == "icu":
            component = {"type": "library", "bom-ref": f"icu@{ICU_VERSION}", "name": "ICU", "version": ICU_VERSION,
                         "group": "unicode-org", "licenses": [{"license": {"id": "Unicode-DFS-2016"}}],
                         "description": "From Qt's binaries; license in share/doc/tomacco/LICENSE.ICU"}
        else:
            component = {"type": "library", "bom-ref": f"pkg:generic/qt/{module}@{version}", "name": module,
                         "version": version, "group": "qt", "purl": f"pkg:generic/qt/{module}@{version}",
                         "licenses": [{"license": {"id": "LGPL-3.0-only"}}]}
            if module == "qtwebengine":
                component["description"] = ("Includes Chromium and its third-party components; Qt's own bills of materials "
                                            "are in lib/tomacco/runtime/sbom/")
        component["externalReferences"] = [{"type": "distribution", "url": url, "hashes": sha256(digest)}]
        components.append(component)
    version, digest = dockerfile_argument(root, "FFMPEG_VERSION", architecture), dockerfile_argument(root, "FFMPEG_SHA256", architecture)
    url = f"https://ffmpeg.org/releases/ffmpeg-{version}.tar.xz"
    components.append({"type": "library", "bom-ref": f"pkg:generic/ffmpeg@{version}", "name": "FFmpeg", "version": version,
                       "purl": f"pkg:generic/ffmpeg@{version}", "licenses": [{"license": {"id": "LGPL-2.1-or-later"}}],
                       "description": "libavcodec, libavutil and libswscale, built with only the H.264 and MJPEG decoders, "
                                      "without GPL, version 3 or nonfree parts",
                       "externalReferences": [{"type": "source-distribution", "url": url, "hashes": sha256(digest)}]})
    for library in build_portable.BUNDLED_SYSTEM:
        name = library.split(".so")[0]
        components.append({"type": "library", "bom-ref": f"{base.lower().replace(' ', '-')}:{name}", "name": name,
                           "description": f"{library} from {base}'s package", "licenses": [{"license": {"id": "MIT"}}]})
    return components


def bom(root, package_name, package_digest, version, metadata, linked, portable, epoch):
    components = crate_components(metadata, linked, lock_checksums(root))
    sqlite = sqlite_component(metadata)
    components += [sqlite] if sqlite else []
    components += font_components(root)
    if portable:
        components += portable_components(root, build_portable.architecture_of(package_name).name)
    main = {"type": "application", "bom-ref": "tomacco", "name": "Tomacco", "version": version,
            "description": package_name, "licenses": [{"license": {"id": "GPL-2.0-or-later"}}],
            "purl": f"pkg:github/marselnenaj/tomacco@v{version}", "hashes": sha256(package_digest),
            "externalReferences": [{"type": "vcs", "url": "https://github.com/marselnenaj/tomacco"}]}
    stamp = datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "bomFormat": "CycloneDX",
        "specVersion": SPEC,
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, 'sha256:' + package_digest)}",
        "version": 1,
        "metadata": {"timestamp": stamp, "component": main,
                     "tools": {"components": [{"type": "application", "name": "tools/build_sbom.py"}]}},
        "components": components,
        "dependencies": [{"ref": "tomacco", "dependsOn": [component["bom-ref"] for component in components]}],
    }


def read_cargo(root, read=None):
    """(metadata, linked ids) through `read(argv)`, which returns stdout bytes."""
    read = read or (lambda argv: subprocess.run(argv, cwd=root, capture_output=True, check=True).stdout)
    metadata = json.loads(read(["cargo", "metadata", "--offline", "--locked", "--format-version", "1",
                                "--filter-platform", check_dependencies.TARGET]))
    tree = read(["cargo", "tree", "--offline", "--locked", "--target", check_dependencies.TARGET, "-e", "normal",
                 "--prefix", "none", "--format", "{p}"]).decode()
    ids = {(p["name"], p["version"]): p["id"] for p in metadata["packages"]}
    linked = set()
    for line in tree.splitlines():
        words = line.split()
        if len(words) >= 2 and words[1].startswith("v") and (words[0], words[1][1:]) in ids and "(/" not in line:
            linked.add(ids[(words[0], words[1][1:])])
    return metadata, linked


def write(root, package, version, metadata, linked, portable, epoch, output=None):
    output = output or package.with_name(package.name + SUFFIX)
    document = bom(root, package.name, file_digest(package), version, metadata, linked, portable, epoch)
    output.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    return output


def main(arguments=None, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("package", type=Path)
    parser.add_argument("--portable", action="store_true", help="The package bundles Qt and FFmpeg")
    parser.add_argument("--output", type=Path, help=f"Where to write it (default: the package's name and {SUFFIX})")
    options = parser.parse_args(arguments)
    try:
        version = build_portable.project_version(root)
        metadata, linked = read_cargo(root)
        epoch = os.environ.get("SOURCE_DATE_EPOCH") or str(int(options.package.stat().st_mtime))
        output = write(root, options.package, version, metadata, linked, options.portable, epoch, options.output)
    except (Failed, build_portable.Failed, subprocess.CalledProcessError, OSError) as error:
        print(f"Failed: {error}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
