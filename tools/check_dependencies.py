#!/usr/bin/env python3
"""Check the Rust dependencies against the project's policy, offline, from
Cargo.lock and the crates' own metadata:

- every crate's license (its SPDX expression) allows distribution with
  Tomacco under GPL-2.0-or-later; a crate linked into `tomaccoctl` that is
  Apache-2.0 only must be one THIRD_PARTY_NOTICES.md names, since it makes a
  distributed binary GPL-3.0;
- no crate is in Cargo.lock in two semver-incompatible versions, except the
  ones listed in KNOWN_DUPLICATES with the reason;
- no locked version is yanked, as far as Cargo's local index cache knows;
- the crate tables of THIRD_PARTY_NOTICES.md list exactly the crates of the
  Linux build, with their versions and licenses.

When cargo-deny is installed it also runs `cargo deny --offline check` with
deny.toml (licenses, bans, sources, and advisories from its local database).

The metadata comes from `cargo metadata` and `cargo tree`, both --offline
--locked, so they need the crates in Cargo's cache or vendor/ (as in the
source tarball); --metadata reads a saved copy of `cargo metadata` instead.
Exits 1 when a check fails."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = "x86_64-unknown-linux-gnu"
NOTICES = "THIRD_PARTY_NOTICES.md"
LINKED = "### Linked into `tomaccoctl`"
BUILD_ONLY = "### Build-time only"

# Licenses whose code may be combined with GPL-2.0-or-later code and
# distributed. Apache-2.0 is compatible with GPL-3.0 only.
ALLOWED = {
    "0BSD", "Apache-2.0", "Apache-2.0 WITH LLVM-exception", "BSD-1-Clause", "BSD-2-Clause", "BSD-3-Clause",
    "CC0-1.0", "GPL-2.0-or-later", "GPL-3.0-or-later", "ISC", "LGPL-2.1-or-later", "MIT", "MPL-2.0",
    "Unicode-3.0", "Unicode-DFS-2016", "Unlicense", "Zlib",
}
GPL3_ONLY = {"Apache-2.0", "GPL-3.0-or-later"}
# Old crates write "MIT/Apache-2.0" for "MIT OR Apache-2.0".
SLASH = re.compile(r"\s*/\s*")

# Crates Cargo.lock holds in two incompatible versions because two of our
# dependencies need different ones; each needs an upstream release to go away.
KNOWN_DUPLICATES = {
    "base64": "tonic 0.14 uses 0.22; tomaccoctl uses 0.23",
    "foldhash": "hashbrown 0.15 (petgraph, build time only) uses 0.1; serde_json and hashbrown 0.17 use 0.2",
    "hashbrown": "petgraph 0.8 (prost-build, build time only) uses 0.15; indexmap uses 0.17",
    "syn": "prost, tonic-build and tracing's macros use 2; serde, tokio and async-trait use 3",
}


class Failed(Exception):
    pass


# ---------------------------------------------------------------------------
# Licenses
# ---------------------------------------------------------------------------

def tokens(expression):
    text = SLASH.sub(" OR ", expression.strip())
    return re.findall(r"\(|\)|[^\s()]+", text)


def alternatives(expression):
    """The license sets that satisfy an SPDX expression: a list of
    frozensets, any one of which is enough. `A WITH B` is one license."""
    words = tokens(expression)
    position = 0

    def atom():
        nonlocal position
        if position >= len(words):
            raise ValueError(f"incomplete license expression: {expression!r}")
        word = words[position]
        position += 1
        if word == "(":
            found = either()
            if position >= len(words) or words[position] != ")":
                raise ValueError(f"unbalanced license expression: {expression!r}")
            position += 1
            return found
        if word in ("AND", "OR", "WITH", ")"):
            raise ValueError(f"malformed license expression: {expression!r}")
        if position < len(words) and words[position] == "WITH":
            if position + 1 >= len(words):
                raise ValueError(f"incomplete license expression: {expression!r}")
            word = f"{word} WITH {words[position + 1]}"
            position += 2
        return [frozenset([word])]

    def both():
        nonlocal position
        found = atom()
        while position < len(words) and words[position] == "AND":
            position += 1
            right = atom()
            found = [left | other for left in found for other in right]
        return found

    def either():
        nonlocal position
        found = both()
        while position < len(words) and words[position] == "OR":
            position += 1
            found = found + both()
        return found

    found = either()
    if position != len(words):
        raise ValueError(f"malformed license expression: {expression!r}")
    return found


def allowed(expression, licenses=ALLOWED):
    return any(choice <= licenses for choice in alternatives(expression))


def license_problems(packages, shipped, notices_text):
    """Crates whose license is not allowed, and Apache-2.0-only crates linked
    into tomaccoctl that the notices do not name."""
    problems = []
    for package in packages:
        name, version, license = package["name"], package["version"], package.get("license")
        if not license:
            problems.append(f"{name} {version} states no SPDX license")
            continue
        try:
            if not allowed(license):
                problems.append(f"{name} {version} is {license}, which is not compatible with GPL-2.0-or-later")
            elif package["id"] in shipped and not is_macro(package) and not allowed(license, ALLOWED - GPL3_ONLY) \
                    and not re.search(rf"`{re.escape(name)}`", notices_text):
                problems.append(f"{name} {version} is {license}, linked into tomaccoctl, and not named in {NOTICES}'s "
                                "account of how the licenses combine")
        except ValueError as error:
            problems.append(f"{name} {version}: {error}")
    return problems


# ---------------------------------------------------------------------------
# The dependency graph
# ---------------------------------------------------------------------------

def reachable(metadata, kinds):
    """The packages reachable from the workspace through dependencies of the
    given kinds (None for normal, "build"); the workspace itself left out."""
    nodes = {node["id"]: node for node in metadata["resolve"]["nodes"]}
    roots = list(metadata["workspace_members"])
    seen = set(roots)
    while roots:
        node = nodes[roots.pop()]
        for dependency in node.get("deps", []):
            if any(kind.get("kind") in kinds for kind in dependency.get("dep_kinds", [])) and dependency["pkg"] not in seen:
                seen.add(dependency["pkg"])
                roots.append(dependency["pkg"])
    return seen - set(metadata["workspace_members"])


def shipped_ids(metadata):
    """The packages of the binary: reachable through normal dependencies
    only. Procedural macros among them run in the compiler; THIRD_PARTY_NOTICES.md
    lists them with the linked crates all the same."""
    return reachable(metadata, {None})


def is_macro(package):
    return any("proc-macro" in target.get("kind", []) for target in package.get("targets", []))


def series(version):
    """The semver-compatible series of a version: 1.2.3 -> 1, 0.2.3 -> 0.2."""
    parts = re.match(r"(\d+)\.(\d+)\.(\d+)", version).groups()
    if parts[0] != "0":
        return parts[0]
    if parts[1] != "0":
        return f"0.{parts[1]}"
    return f"0.0.{parts[2]}"


def duplicates(packages):
    """{name: [versions]} for crates locked in more than one series."""
    found = {}
    for package in packages:
        found.setdefault(package["name"], {}).setdefault(series(package["version"]), package["version"])
    return {name: sorted(versions.values()) for name, versions in found.items() if len(versions) > 1}


def duplicate_problems(packages, known=KNOWN_DUPLICATES):
    found = duplicates(packages)
    problems = [f"{name} is locked in incompatible versions {', '.join(versions)}" for name, versions in sorted(found.items())
                if name not in known]
    stale = sorted(set(known) - set(found))
    return problems, stale


# ---------------------------------------------------------------------------
# Yanked versions, from Cargo's local index cache
# ---------------------------------------------------------------------------

def index_path(name):
    name = name.lower()
    if len(name) <= 2:
        return Path(str(len(name)), name)
    if len(name) == 3:
        return Path("3", name[0], name)
    return Path(name[:2], name[2:4], name)


def cached_versions(caches, name):
    """{version: yanked} from the first index cache that has the crate."""
    for cache in caches:
        path = cache / index_path(name)
        if path.is_file():
            versions = {}
            for chunk in path.read_bytes().split(b"\0"):
                if chunk.startswith(b"{"):
                    try:
                        entry = json.loads(chunk)
                    except ValueError:
                        continue
                    versions[entry.get("vers")] = bool(entry.get("yanked"))
            return versions
    return None


def index_caches(cargo_home=None):
    home = Path(cargo_home or os.environ.get("CARGO_HOME") or Path.home() / ".cargo")
    return sorted(home.glob("registry/index/*/.cache"))


def yanked_problems(packages, caches):
    problems, unknown = [], []
    for package in packages:
        if not str(package.get("source") or "").startswith(("registry+", "sparse+")):
            continue
        versions = cached_versions(caches, package["name"])
        if versions is None or package["version"] not in versions:
            unknown.append(f"{package['name']} {package['version']}")
        elif versions[package["version"]]:
            problems.append(f"{package['name']} {package['version']} is yanked")
    return problems, unknown


# ---------------------------------------------------------------------------
# THIRD_PARTY_NOTICES.md's crate tables
# ---------------------------------------------------------------------------

def notice_table(text, heading):
    """{(name, version): license} from the table under `heading`."""
    start = text.find(heading)
    if start < 0:
        raise Failed(f"{NOTICES} has no section {heading!r}")
    rows = {}
    for line in text[start:].splitlines()[1:]:
        if line.startswith("#"):
            break
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0] not in ("Crate", "---") and not set(cells[0]) <= {"-"}:
            rows[(cells[0], cells[1])] = cells[2]
    return rows


def notice_problems(packages, shipped, text):
    problems = []
    for heading, wanted in ((LINKED, True), (BUILD_ONLY, False)):
        listed = notice_table(text, heading)
        expected = {(p["name"], p["version"]): p.get("license") or "" for p in packages if (p["id"] in shipped) == wanted}
        for key in sorted(set(expected) - set(listed)):
            problems.append(f"{NOTICES} does not list {key[0]} {key[1]} under {heading!r}")
        for key in sorted(set(listed) - set(expected)):
            problems.append(f"{NOTICES} lists {key[0]} {key[1]} under {heading!r}, which Cargo.lock does not have there")
        for key in sorted(set(listed) & set(expected)):
            if listed[key] != expected[key]:
                problems.append(f"{NOTICES} gives {key[0]} {key[1]} the license {listed[key]!r}, the crate says {expected[key]!r}")
    return problems


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

def cargo_metadata(root):
    done = subprocess.run(["cargo", "metadata", "--offline", "--locked", "--format-version", "1", "--filter-platform", TARGET],
                          cwd=root, capture_output=True, check=False)
    if done.returncode:
        raise Failed("cargo metadata --offline failed (are the crates in Cargo's cache or vendor/?): "
                     + done.stderr.decode(errors="replace").strip()[-400:])
    return json.loads(done.stdout)


def cargo_tree(root, edges):
    """{(name, version)} of the Linux build through the given edge kinds, the
    workspace left out. Unlike `cargo metadata`, `cargo tree` resolves
    features per target, so a crate only Windows switches on stays out."""
    done = subprocess.run(["cargo", "tree", "--offline", "--locked", "--target", TARGET, "-e", edges,
                           "--prefix", "none", "--format", "{p}"], cwd=root, capture_output=True, check=False)
    if done.returncode:
        raise Failed("cargo tree --offline failed: " + done.stderr.decode(errors="replace").strip()[-400:])
    found = set()
    for line in done.stdout.decode().splitlines():
        words = line.split()
        if len(words) >= 2 and words[1].startswith("v") and "(/" not in line:
            found.add((words[0], words[1][1:]))
    return found


def graph(metadata, root=None):
    """(linked, used): the ids of the packages linked into tomaccoctl, and of
    all the Linux build compiles. From `cargo tree` when `root` is given,
    otherwise from the metadata's graph, which may hold a few more."""
    if root is None:
        return shipped_ids(metadata), reachable(metadata, {None, "build"})
    ids = {(p["name"], p["version"]): p["id"] for p in metadata["packages"]}
    pick = lambda found: {ids[key] for key in found if key in ids}
    return pick(cargo_tree(root, "normal")), pick(cargo_tree(root, "normal,build"))


def cargo_deny(root, run=subprocess.run):
    """Runs cargo-deny when it is installed. Returns (ran, problems)."""
    if not shutil.which("cargo-deny"):
        return False, []
    done = run(["cargo", "deny", "--offline", "--config", str(root / "deny.toml"), "check"],
               cwd=root, capture_output=True, check=False)
    if done.returncode:
        output = (done.stderr or b"").decode(errors="replace")
        errors = [line for line in output.splitlines() if line.startswith("error")]
        return True, ["cargo deny check failed: " + ("; ".join(errors[:10]) or output.strip()[-400:])]
    return True, []


def check(metadata, root=ROOT, caches=None, deny=cargo_deny, edges=None):
    """Runs every check; returns (problems, notes). `edges` is (linked, used)
    as `graph` returns it; by default the metadata's own graph."""
    shipped, used = edges or graph(metadata)
    packages = [p for p in metadata["packages"] if p["id"] in used]
    notices_text = (root / NOTICES).read_text()
    problems = license_problems(packages, shipped, notices_text)
    duplicated, stale = duplicate_problems(packages)
    problems += duplicated
    yanked, unknown = yanked_problems(packages, index_caches() if caches is None else caches)
    problems += yanked
    problems += notice_problems(packages, shipped, notices_text)
    notes = [f"{len(packages)} crates, {len(shipped)} linked into tomaccoctl"]
    notes += [f"known duplicate: {name} ({reason})" for name, reason in sorted(KNOWN_DUPLICATES.items()) if name not in stale]
    notes += [f"{name} is no longer duplicated: remove it from KNOWN_DUPLICATES" for name in stale]
    if unknown:
        notes.append(f"Cargo's index cache does not know {len(unknown)} locked versions, so their yanked state was not checked: "
                     + ", ".join(unknown[:10]))
    if deny:
        ran, found = deny(root)
        problems += found
        notes.append("cargo deny check ran" if ran else "cargo-deny is not installed; its checks were skipped")
    return problems, notes


def main(arguments=None, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--metadata", type=Path, help="Read saved `cargo metadata` JSON instead of running cargo")
    parser.add_argument("--no-cargo-deny", action="store_true", help="Do not run cargo-deny even when it is installed")
    options = parser.parse_args(arguments)
    try:
        if options.metadata:
            metadata = json.loads(options.metadata.read_text())
            edges = graph(metadata)
        else:
            metadata = cargo_metadata(root)
            edges = graph(metadata, root)
        problems, notes = check(metadata, root, deny=None if options.no_cargo_deny else cargo_deny, edges=edges)
    except Failed as error:
        print(f"Failed: {error}", file=sys.stderr)
        return 1
    for note in notes:
        print(note)
    for problem in problems:
        print(f"Problem: {problem}", file=sys.stderr)
    print("Dependencies: " + ("the policy holds" if not problems else f"{len(problems)} problems"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
