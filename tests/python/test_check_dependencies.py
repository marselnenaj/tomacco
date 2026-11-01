"""tools/check_dependencies.py: the dependency policy, on made-up metadata."""

import json
import re
import tempfile
import tomllib
import unittest
from pathlib import Path

import check_dependencies as policy

ROOT = Path(__file__).resolve().parents[2]
NOTICES = """# Notices

- `tokio-thing` is Apache-2.0 only, which makes the binary GPL-3.0.

### Linked into `tomaccoctl`

| Crate | Version | License (SPDX) | Copyright | Source |
| --- | --- | --- | --- | --- |
{linked}

### Build-time only (not in the binary)

| Crate | Version | License (SPDX) | Copyright | Source |
| --- | --- | --- | --- | --- |
{build}

## License texts
"""


def package(name, version, license, macro=False):
    return {"id": f"registry+https://github.com/rust-lang/crates.io-index#{name}@{version}", "name": name,
            "version": version, "license": license, "source": "registry+https://github.com/rust-lang/crates.io-index",
            "targets": [{"kind": ["proc-macro" if macro else "lib"]}]}


def metadata(linked, build=(), edges=None):
    """The workspace depends on `linked` normally and on `build` at build time."""
    root = {"id": "path+file:///x#tomacco-core@1.0.0", "name": "tomacco-core", "version": "1.0.0", "license": "GPL-2.0-or-later"}
    nodes = [{"id": root["id"], "deps": [{"pkg": p["id"], "dep_kinds": [{"kind": None}]} for p in linked]
              + [{"pkg": p["id"], "dep_kinds": [{"kind": "build"}]} for p in build]}]
    nodes += [{"id": p["id"], "deps": (edges or {}).get(p["name"], [])} for p in [*linked, *build]]
    return {"packages": [root, *linked, *build], "workspace_members": [root["id"]], "resolve": {"nodes": nodes}}


def table(packages):
    return "\n".join(f"| {p['name']} | {p['version']} | {p['license']} | (c) | https://example |" for p in packages)


class Licenses(unittest.TestCase):
    def test_expressions_are_satisfied_by_any_allowed_choice(self):
        self.assertTrue(policy.allowed("MIT OR Apache-2.0"))
        self.assertTrue(policy.allowed("MIT/Apache-2.0"))
        self.assertTrue(policy.allowed("Apache-2.0 / MIT"))
        self.assertTrue(policy.allowed("(MIT OR Apache-2.0) AND Unicode-3.0"))
        self.assertTrue(policy.allowed("Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT"))
        self.assertTrue(policy.allowed("MIT OR Apache-2.0 OR LGPL-2.1-or-later"))
        self.assertFalse(policy.allowed("GPL-3.0-only"))
        self.assertFalse(policy.allowed("GPL-2.0-only"))
        self.assertFalse(policy.allowed("MIT AND SSPL-1.0"))
        self.assertTrue(policy.allowed("SSPL-1.0 OR MIT"))
        self.assertFalse(policy.allowed("Apache-2.0", policy.ALLOWED - policy.GPL3_ONLY))
        self.assertTrue(policy.allowed("Apache-2.0 WITH LLVM-exception", policy.ALLOWED - policy.GPL3_ONLY))
        for broken in ("MIT OR", "(MIT", "MIT Apache-2.0", "MIT WITH"):
            with self.assertRaises(ValueError, msg=broken):
                policy.alternatives(broken)

    def run_check(self, linked, build=(), notices=None, **options):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / policy.NOTICES).write_text(notices if notices is not None else NOTICES.format(linked=table(linked), build=table(build)))
            return policy.check(metadata(linked, build), root, caches=[], deny=None, **options)

    def test_an_incompatible_license_fails_and_a_compatible_one_passes(self):
        problems, _ = self.run_check([package("good", "1.0.0", "MIT OR Apache-2.0")])
        self.assertEqual(problems, [])
        problems, _ = self.run_check([package("bad", "1.0.0", "GPL-3.0-only"), package("none", "1.0.0", None)])
        self.assertTrue(any("bad 1.0.0 is GPL-3.0-only" in p for p in problems), problems)
        self.assertTrue(any("none 1.0.0 states no SPDX license" in p for p in problems), problems)

    def test_a_linked_apache_only_crate_must_be_named_in_the_notices(self):
        problems, _ = self.run_check([package("tokio-thing", "1.0.0", "Apache-2.0")])
        self.assertEqual(problems, [])
        problems, _ = self.run_check([package("other-thing", "1.0.0", "Apache-2.0")])
        self.assertTrue(any("other-thing 1.0.0 is Apache-2.0, linked into tomaccoctl" in p for p in problems), problems)
        # At build time only, or as a procedural macro, it is not in the binary.
        problems, _ = self.run_check([package("macro-thing", "1.0.0", "Apache-2.0", macro=True)],
                                     [package("generator", "1.0.0", "Apache-2.0")])
        self.assertEqual(problems, [])


class Duplicates(unittest.TestCase):
    def test_versions_count_as_one_series_while_semver_compatible(self):
        self.assertEqual(policy.series("1.2.3"), "1")
        self.assertEqual(policy.series("0.2.3"), "0.2")
        self.assertEqual(policy.series("0.0.3"), "0.0.3")
        found = policy.duplicates([package("a", "1.0.0", "MIT"), package("a", "1.4.0", "MIT"),
                                   package("b", "0.2.0", "MIT"), package("b", "0.3.1", "MIT")])
        self.assertEqual(found, {"b": ["0.2.0", "0.3.1"]})

    def test_an_unknown_duplicate_fails_and_a_known_one_is_noted(self):
        packages = [package("b", "0.2.0", "MIT"), package("b", "0.3.1", "MIT"), package("c", "1.0.0", "MIT"), package("c", "2.0.0", "MIT")]
        problems, stale = policy.duplicate_problems(packages, {"c": "reason", "gone": "reason"})
        self.assertEqual(problems, ["b is locked in incompatible versions 0.2.0, 0.3.1"])
        self.assertEqual(stale, ["gone"])

    def test_deny_toml_skips_the_same_duplicates_and_allows_no_other_license(self):
        deny = tomllib.loads((ROOT / "deny.toml").read_text())
        skipped = {entry["crate"].split("@")[0] for entry in deny["bans"]["skip"]}
        self.assertEqual(skipped, set(policy.KNOWN_DUPLICATES))
        self.assertEqual(deny["bans"]["multiple-versions"], "deny")
        self.assertLessEqual(set(deny["licenses"]["allow"]), policy.ALLOWED)
        self.assertEqual(deny["advisories"]["yanked"], "deny")


class Yanked(unittest.TestCase):
    def test_a_yanked_version_in_the_index_cache_fails_and_an_unknown_one_is_noted(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            entry = cache / policy.index_path("serde")
            entry.parent.mkdir(parents=True)
            # Cargo's cache format: a header, then version and JSON, each ending in a NUL byte.
            entry.write_bytes(b"\x03\x02\x00\x00\x00etag: x\x00" + b"".join(
                version.encode() + b"\x00" + json.dumps({"name": "serde", "vers": version, "yanked": yanked}).encode() + b"\x00"
                for version, yanked in (("1.0.0", False), ("1.0.1", True))))
            problems, unknown = policy.yanked_problems(
                [package("serde", "1.0.0", "MIT"), package("serde", "1.0.1", "MIT"), package("absent", "1.0.0", "MIT")], [cache])
        self.assertEqual(problems, ["serde 1.0.1 is yanked"])
        self.assertEqual(unknown, ["absent 1.0.0"])

    def test_the_index_layout_is_cargos(self):
        self.assertEqual(policy.index_path("a"), Path("1/a"))
        self.assertEqual(policy.index_path("ab"), Path("2/ab"))
        self.assertEqual(policy.index_path("abc"), Path("3/a/abc"))
        self.assertEqual(policy.index_path("Serde"), Path("se/rd/serde"))


class NoticeTables(unittest.TestCase):
    def test_the_tables_must_list_each_crate_where_it_belongs_with_its_license(self):
        linked, build = [package("a", "1.0.0", "MIT")], [package("gen", "2.0.0", "MIT")]
        checks = Licenses()
        problems, _ = checks.run_check(linked, build)
        self.assertEqual(problems, [])
        notices = NOTICES.format(linked=table([package("a", "0.9.0", "MIT"), package("gen", "2.0.0", "MIT")]),
                                 build=table([package("gen", "2.0.0", "Zlib")]))
        problems, _ = checks.run_check(linked, build, notices=notices)
        self.assertIn("THIRD_PARTY_NOTICES.md does not list a 1.0.0 under '### Linked into `tomaccoctl`'", problems)
        self.assertTrue(any("lists a 0.9.0" in p for p in problems), problems)
        self.assertTrue(any("lists gen 2.0.0 under '### Linked" in p for p in problems), problems)
        self.assertTrue(any("gives gen 2.0.0 the license 'Zlib'" in p for p in problems), problems)

    def test_the_real_notices_have_both_tables(self):
        text = (ROOT / policy.NOTICES).read_text()
        linked = policy.notice_table(text, policy.LINKED)
        self.assertGreater(len(linked), 50)
        self.assertIn(("ed25519-dalek", "3.0.0"), linked)
        self.assertGreater(len(policy.notice_table(text, policy.BUILD_ONLY)), 5)
        # The Apache-2.0-only crates the account names.
        for name in ("prost", "sync_wrapper"):
            self.assertRegex(text, rf"`{re.escape(name)}`")


class Graph(unittest.TestCase):
    def test_normal_dependencies_are_linked_and_build_dependencies_are_not(self):
        a, b, c = package("a", "1.0.0", "MIT"), package("b", "1.0.0", "MIT"), package("c", "1.0.0", "MIT")
        data = metadata([a], [b], edges={"a": [{"pkg": c["id"], "dep_kinds": [{"kind": None}]}]})
        data["packages"].append(c)
        data["resolve"]["nodes"].append({"id": c["id"], "deps": []})
        linked, used = policy.graph(data)
        self.assertEqual(linked, {a["id"], c["id"]})
        self.assertEqual(used, {a["id"], b["id"], c["id"]})

    def test_cargo_deny_is_optional_and_its_failure_counts(self):
        class Done:
            returncode = 1
            stderr = b"error[vulnerability]: something\nerror[banned]: other\n"
        ran, problems = policy.cargo_deny(ROOT, run=lambda *a, **k: Done())
        if not ran:
            self.skipTest("cargo-deny is not installed")
        self.assertEqual(problems, ["cargo deny check failed: error[vulnerability]: something; error[banned]: other"])


if __name__ == "__main__":
    unittest.main()
