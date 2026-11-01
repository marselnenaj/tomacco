"""tools/build_sbom.py: the CycloneDX SBOM of a release package."""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import build_sbom
import check_dependencies

ROOT = Path(__file__).resolve().parents[2]
TYPES = {"application", "framework", "library", "container", "platform", "operating-system", "device", "device-driver",
         "firmware", "file", "machine-learning-model", "data", "cryptographic-asset"}
REFERENCE_TYPES = {"vcs", "website", "distribution", "source-distribution"}
SPDX_IDS = check_dependencies.ALLOWED | {"LGPL-3.0-only", "OFL-1.1", "Unicode-DFS-2016"}
CRATE = "registry+https://github.com/rust-lang/crates.io-index#serde@1.0.0"


def validate(test, document):
    """The parts of the CycloneDX 1.6 schema this SBOM uses, and its own rules."""
    test.assertEqual(document["bomFormat"], "CycloneDX")
    test.assertEqual(document["specVersion"], "1.6")
    test.assertRegex(document["serialNumber"], r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    test.assertIsInstance(document["version"], int)
    test.assertRegex(document["metadata"]["timestamp"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
    main = document["metadata"]["component"]
    components = document["components"]
    references = [main["bom-ref"]] + [component["bom-ref"] for component in components]
    test.assertEqual(len(references), len(set(references)), "bom-refs are unique")
    for component in [main, *components]:
        test.assertIn(component["type"], TYPES)
        test.assertTrue(component["name"])
        if "purl" in component:
            test.assertRegex(component["purl"], r"^pkg:[a-z]+/[^@\s]+@[^@\s]+$")
        test.assertTrue(component.get("licenses"), component["name"])
        for choice in component["licenses"]:
            test.assertEqual(len(choice), 1, choice)
            if "expression" in choice:
                check_dependencies.alternatives(choice["expression"])
                test.assertNotIn("/", choice["expression"])
            else:
                license = choice["license"]
                test.assertTrue(license.get("id") in SPDX_IDS or ("name" in license and "id" not in license), license)
        hashes = list(component.get("hashes", []))
        for reference in component.get("externalReferences", []):
            test.assertIn(reference["type"], REFERENCE_TYPES)
            test.assertRegex(reference["url"], r"^https://")
            hashes += reference.get("hashes", [])
        for digest in hashes:
            test.assertEqual(digest["alg"], "SHA-256")
            test.assertRegex(digest["content"], r"^[0-9a-f]{64}$")
    for dependency in document["dependencies"]:
        test.assertIn(dependency["ref"], references)
        for reference in dependency["dependsOn"]:
            test.assertIn(reference, references)


def fixture(root):
    """A checkout with one crate, linked."""
    metadata = {"packages": [{"id": CRATE, "name": "serde", "version": "1.0.0", "license": "MIT/Apache-2.0",
                              "repository": "https://github.com/serde-rs/serde", "manifest_path": "/nowhere/Cargo.toml"}]}
    (root / "Cargo.lock").write_text('[[package]]\nname = "serde"\nversion = "1.0.0"\nchecksum = "' + "cd" * 32 + '"\n')
    (root / "packaging/portable").mkdir(parents=True)
    for name in ("qt-archives.txt", "Dockerfile"):
        shutil.copy2(ROOT / "packaging/portable" / name, root / "packaging/portable" / name)
    (root / "assets/fonts").mkdir(parents=True)
    shutil.copy2(ROOT / "assets/fonts/archivo-var.ttf", root / "assets/fonts/archivo-var.ttf")
    package = root / "tomacco-1.2.3-Linux-x86_64-portable.tar.gz"
    package.write_bytes(b"package")
    return metadata, {CRATE}, package


class Shape(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.metadata, self.linked, self.package = fixture(self.root)

    def write(self, portable, output=None):
        path = build_sbom.write(self.root, self.package, "1.2.3", self.metadata, self.linked, portable, "1700000000", output)
        return json.loads(path.read_text()), path

    def test_the_portable_sbom_is_valid_and_lists_what_is_bundled(self):
        document, path = self.write(True)
        self.assertEqual(path.name, self.package.name + ".cdx.json")
        validate(self, document)
        main = document["metadata"]["component"]
        self.assertEqual((main["name"], main["version"]), ("Tomacco", "1.2.3"))
        self.assertEqual(main["hashes"][0]["content"], build_sbom.file_digest(self.package))
        self.assertEqual(main["licenses"], [{"license": {"id": "GPL-2.0-or-later"}}])
        self.assertEqual(document["metadata"]["timestamp"], "2023-11-14T22:13:20Z")
        by_name = {component["name"]: component for component in document["components"]}
        crate = by_name["serde"]
        self.assertEqual(crate["purl"], "pkg:cargo/serde@1.0.0")
        self.assertEqual(crate["licenses"], [{"expression": "MIT OR Apache-2.0"}])
        self.assertEqual(crate["hashes"][0]["content"], "cd" * 32)
        # Every pinned Qt archive, with its digest; ICU by its own version.
        for module, version, digest, url in build_sbom.qt_archives(ROOT):
            component = by_name["ICU" if module == "icu" else module]
            self.assertEqual(component["externalReferences"][0]["hashes"][0]["content"], digest)
            self.assertEqual(component["externalReferences"][0]["url"], url)
            self.assertEqual(component["version"], build_sbom.ICU_VERSION if module == "icu" else version)
        self.assertEqual(by_name["qtbase"]["licenses"], [{"license": {"id": "LGPL-3.0-only"}}])
        ffmpeg = by_name["FFmpeg"]
        self.assertEqual(ffmpeg["version"], build_sbom.dockerfile_argument(ROOT, "FFMPEG_VERSION"))
        self.assertEqual(ffmpeg["externalReferences"][0]["hashes"][0]["content"], build_sbom.dockerfile_argument(ROOT, "FFMPEG_SHA256"))
        self.assertEqual(ffmpeg["licenses"], [{"license": {"id": "LGPL-2.1-or-later"}}])
        self.assertIn("libxcb-cursor", by_name)
        self.assertEqual(by_name["Archivo"]["licenses"], [{"license": {"id": "OFL-1.1"}}])
        self.assertEqual(document["dependencies"][0]["ref"], "tomacco")

    def test_the_regular_sbom_leaves_qt_and_ffmpeg_out(self):
        document, _ = self.write(False)
        validate(self, document)
        names = {component["name"] for component in document["components"]}
        self.assertIn("serde", names)
        self.assertFalse(names & {"qtbase", "qtwebengine", "ICU", "FFmpeg", "libxcb-cursor"})

    def test_the_same_inputs_give_the_same_bytes(self):
        _, first = self.write(True, self.root / "a.json")
        _, second = self.write(True, self.root / "b.json")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.package.write_bytes(b"other")
        third, _ = self.write(True, self.root / "c.json")
        self.assertNotEqual(json.loads(first.read_text())["serialNumber"], third["serialNumber"])

    def test_the_icu_version_is_the_one_the_portable_build_names(self):
        self.assertIn(f"ICU {build_sbom.ICU_VERSION}", (ROOT / "tools/build_portable.py").read_text())
        self.assertIn(f"ICU {build_sbom.ICU_VERSION}", (ROOT / build_sbom.PORTABLE_DOCKERFILE).read_text())
        self.assertIn(f"ICU {build_sbom.ICU_VERSION}", (ROOT / "THIRD_PARTY_NOTICES.md").read_text())


class FromCargo(unittest.TestCase):
    """The real Cargo.lock, when the crates are in Cargo's cache."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("cargo"):
            raise unittest.SkipTest("cargo is not installed")
        try:
            cls.metadata, cls.linked = build_sbom.read_cargo(ROOT)
        except subprocess.CalledProcessError:
            raise unittest.SkipTest("cargo metadata --offline cannot run without the crates")

    def test_the_sbom_of_this_checkout_lists_the_linked_crates_with_their_checksums(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory, "tomacco-1.1.0-Linux-x86_64.tar.gz")
            package.write_bytes(b"package")
            document = json.loads(build_sbom.write(ROOT, package, "1.1.0", self.metadata, self.linked, True, "0").read_text())
        validate(self, document)
        crates = {c["name"]: c for c in document["components"] if c.get("purl", "").startswith("pkg:cargo/")}
        self.assertGreater(len(crates), 50)
        checksums = build_sbom.lock_checksums(ROOT)
        dalek = crates["ed25519-dalek"]
        self.assertEqual(dalek["hashes"][0]["content"], checksums[("ed25519-dalek", dalek["version"])])
        # Build-time only crates are not in the package.
        self.assertNotIn("prost-build", crates)

    def test_the_notices_name_the_sqlite_the_locked_crate_bundles(self):
        version, crate = build_sbom.sqlite_version(self.metadata)
        if not version:
            self.skipTest("libsqlite3-sys's source is not in Cargo's cache")
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
        self.assertIn(f"SQLite\n{version} from `libsqlite3-sys` {crate}", notices)


if __name__ == "__main__":
    unittest.main()
