"""tools/build_provenance.py: what a release's provenance records."""

import datetime
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import build_provenance

ROOT = Path(__file__).resolve().parents[2]


class Provenance(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dist = Path(directory.name)
        for name, data in (("p.tar.gz", b"package"), ("p.tar.gz.sha256", b"x"), ("p.tar.gz.sig", b"y"),
                           ("p.tar.gz.cdx.json", b"{}"), ("t-1.0.0-provenance.intoto.json", b"old")):
            (self.dist / name).write_bytes(data)

    def test_the_subjects_are_the_offered_files_without_checksums_signatures_or_itself(self):
        found = build_provenance.subjects(self.dist, "t-1.0.0-provenance.intoto.json")
        self.assertEqual(found, [
            {"name": "p.tar.gz", "digest": {"sha256": hashlib.sha256(b"package").hexdigest()}},
            {"name": "p.tar.gz.cdx.json", "digest": {"sha256": hashlib.sha256(b"{}").hexdigest()}}])

    def test_a_tool_that_cannot_say_its_version_is_left_out(self):
        def read(argv):
            if argv[0] == "rustc":
                return b"rustc 1.99.1 (abc 2026-01-01)\nmore\n"
            raise FileNotFoundError(argv[0])
        versions = build_provenance.builder(read, portable=True)
        self.assertEqual(versions["rustc"], "rustc 1.99.1 (abc 2026-01-01)")
        self.assertNotIn("cargo", versions)
        self.assertNotIn("docker", versions)
        for key in ("os", "kernel", "machine", "python"):
            self.assertTrue(versions[key])

    def test_docker_is_asked_only_for_the_portable_package(self):
        asked = []
        build_provenance.builder(lambda argv: asked.append(argv[0]) or b"v", portable=False)
        self.assertNotIn("docker", asked)
        build_provenance.builder(lambda argv: asked.append(argv[0]) or b"v", portable=True)
        self.assertIn("docker", asked)

    def test_the_inputs_are_the_commit_the_lock_file_and_the_portable_downloads(self):
        regular = build_provenance.inputs(ROOT, "c0ffee", "v1.2.3", portable=False)
        self.assertEqual(regular[0], {"uri": "git+https://github.com/marselnenaj/tomacco@refs/tags/v1.2.3",
                                      "digest": {"gitCommit": "c0ffee"}})
        self.assertEqual(regular[1]["digest"]["sha256"], hashlib.sha256((ROOT / "Cargo.lock").read_bytes()).hexdigest())
        self.assertEqual(len(regular), 2)
        portable = build_provenance.inputs(ROOT, "c0ffee", "v1.2.3", portable=True)
        dockerfile = (ROOT / build_provenance.PORTABLE_DOCKERFILE).read_text()
        for entry in portable[2:]:
            digest = entry["digest"]["sha256"]
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            if entry.get("uri", "").startswith("https://download.qt.io/"):
                self.assertIn(digest, (ROOT / "packaging/portable/qt-archives.txt").read_text())
            elif "uri" in entry:
                self.assertIn(digest, dockerfile)
        uris = " ".join(entry.get("uri", "") for entry in portable)
        for part in ("docker://ubuntu:22.04", "cmake-", "rustup-init", "ffmpeg-", "qtwebengine"):
            self.assertIn(part, uris)

    def test_each_portable_architecture_adds_its_own_inputs(self):
        both = build_provenance.inputs(ROOT, "c0ffee", "v1.2.3", portable=True, architectures=["x86_64", "aarch64"])
        names = [entry.get("name") for entry in both]
        self.assertIn("packaging/portable/Dockerfile", names)
        self.assertIn("packaging/portable/Dockerfile.aarch64", names)
        uris = [entry.get("uri", "") for entry in both]
        arm = (ROOT / "packaging/portable/Dockerfile.aarch64").read_text()
        archives = (ROOT / "packaging/portable/qt-archives-aarch64.txt").read_text()
        for entry in both:
            uri = entry.get("uri", "")
            if "/linux_arm64/" in uri:
                self.assertIn(entry["digest"]["sha256"], archives)
            elif "aarch64" in uri:
                self.assertIn(entry["digest"]["sha256"], arm)
        for part in ("docker://ubuntu:24.04", "cmake-4.4.5-linux-aarch64.tar.gz", "aarch64-unknown-linux-gnu/rustup-init",
                     "docker://ubuntu:22.04", "cmake-4.4.5-linux-x86_64.tar.gz"):
            self.assertTrue(any(part in uri for uri in uris), part)
        only_arm = build_provenance.inputs(ROOT, "c0ffee", "v1.2.3", portable=True, architectures=["aarch64"])
        self.assertNotIn("packaging/portable/Dockerfile", [entry.get("name") for entry in only_arm])

    def test_another_build_host_reports_its_tools_through_the_wrapper(self):
        asked = []

        def read(argv):
            asked.append(argv)
            if argv[-1] == "uname -m":
                return b"aarch64\n"
            if argv[-1] == "cmake --version":
                return b"cmake version 4.1.0\n\nCMake suite\n"
            raise OSError("no such tool")
        versions = build_provenance.host_builder(read, lambda command: ["ssh", "host", command])
        self.assertEqual(versions, {"machine": "aarch64", "cmake": "cmake version 4.1.0"})
        self.assertTrue(all(argv[:2] == ["ssh", "host"] for argv in asked))
        self.assertIn("rustc --version", [argv[-1] for argv in asked])

    def test_the_statement_is_in_toto_with_a_slsa_predicate(self):
        moment = datetime.datetime(2026, 11, 1, 12, 0, tzinfo=datetime.timezone.utc)
        document = build_provenance.statement(self.dist, "t-1.0.0-provenance.intoto.json", "c0ffee", "v1.0.0", 1700000000,
                                              [{"name": "x", "digest": {"sha256": "0" * 64}}], {"os": "Test"},
                                              {"portable": False}, moment, moment + datetime.timedelta(minutes=5))
        json.dumps(document)
        self.assertEqual(document["_type"], build_provenance.STATEMENT)
        self.assertEqual(document["predicateType"], build_provenance.PREDICATE)
        definition = document["predicate"]["buildDefinition"]
        self.assertTrue(definition["buildType"].endswith("/blob/v1.0.0/tools/make_release.py"))
        self.assertEqual(definition["externalParameters"],
                         {"repository": build_provenance.REPOSITORY, "ref": "refs/tags/v1.0.0", "portable": False})
        self.assertEqual(definition["internalParameters"], {"SOURCE_DATE_EPOCH": "1700000000"})
        run = document["predicate"]["runDetails"]
        self.assertEqual(run["metadata"]["startedOn"], "2026-11-01T12:00:00Z")
        self.assertEqual(run["metadata"]["finishedOn"], "2026-11-01T12:05:00Z")
        self.assertEqual(run["builder"]["version"], {"os": "Test"})

    def test_the_os_name_comes_from_os_release(self):
        release = self.dist / "os-release"
        release.write_text('NAME="Arch Linux"\nPRETTY_NAME="Arch Linux"\n')
        self.assertEqual(build_provenance.os_name(release), "Arch Linux")
        self.assertTrue(build_provenance.os_name(self.dist / "missing"))


if __name__ == "__main__":
    unittest.main()
