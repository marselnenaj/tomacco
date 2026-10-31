"""tools/make_release.py with fake commands: nothing is built, signed or published."""

import base64
import contextlib
import hashlib
import io
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import make_release

ROOT = Path(__file__).resolve().parents[2]
RAW = b"\x11" * 32
PUBLIC = base64.b64encode(RAW).decode()
METAINFO = '<component><releases><release version="1.2.3"/></releases></component>'


class Fake(make_release.Commands):
    """Answers the read-only questions, and plays the build for `run`."""

    def __init__(self, root, dry_run=False, status=b"", tagged=b"abc\n", tags=b"v1.2.3\n"):
        super().__init__(root, dry_run)
        self.status, self.tagged, self.tags = status, tagged, tags
        self.reads, self.runs = [], []

    def read(self, argv):
        self.reads.append(argv)
        if argv[:2] == ["git", "status"]:
            return self.status
        if argv[:2] == ["git", "tag"]:
            return self.tags
        if argv[:2] == ["git", "rev-parse"]:
            return b"abc\n" if argv[2] == "HEAD" else self.tagged
        if argv[:2] == ["openssl", "pkey"]:
            return b"prefix" + RAW
        return b""

    def run(self, argv):
        self.runs.append(argv)
        if self.dry_run:
            return
        if argv[:3] == ["cmake", "--build", make_release.BUILD] and argv[-1] == "package":
            build = self.root / make_release.BUILD
            build.mkdir()
            package = build / "tomacco-1.2.3-Linux-x86_64.tar.gz"
            package.write_bytes(b"package")
            package.with_name(package.name + ".sha256").write_text(hashlib.sha256(b"package").hexdigest() + "  " + package.name + "\n")
        if "tools/build_portable.py" in argv and "build" in argv:
            output = self.root / argv[argv.index("--output") + 1]
            output.mkdir(parents=True)
            (output / "work").mkdir()
            package = output / "tomacco-1.2.3-Linux-x86_64-portable.tar.gz"
            package.write_bytes(b"portable")
            package.with_name(package.name + ".sha256").write_text(hashlib.sha256(b"portable").hexdigest() + "  " + package.name + "\n")
        if "tools/build_source_release.py" in argv:
            dist = self.root / make_release.DIST
            dist.mkdir(exist_ok=True)
            source = dist / "tomacco-1.2.3-source.tar.gz"
            source.write_bytes(b"source")
            source.with_name(source.name + ".sha256").write_text(hashlib.sha256(b"source").hexdigest() + "  " + source.name + "\n")
        if argv[:3] == ["openssl", "pkeyutl", "-sign"]:
            Path(argv[argv.index("-out") + 1]).write_bytes(b"\x22" * 64)


class MakeRelease(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        self.root = base / "repository"
        (self.root / "native").mkdir(parents=True)
        (self.root / "assets").mkdir()
        (self.root / "CMakeLists.txt").write_text("project(Tomacco VERSION 1.2.3 LANGUAGES CXX)\n")
        (self.root / "Cargo.toml").write_text('[package]\nname = "x"\nversion = "1.2.3"\n')
        (self.root / "assets/x.metainfo.xml").write_text(METAINFO)
        self.product(PUBLIC)
        self.key = base / "outside.pem"
        self.key.write_text("private")
        self.key.chmod(0o600)

    def product(self, public):
        (self.root / "native/product.rs").write_text(
            'pub const PRODUCT_NAME: &str = "Tomacco";\npub const REPO_OWNER: &str = "owner";\npub const REPO_NAME: &str = "repo";\n'
            f'pub const PACKAGE_PREFIX: &str = "tomacco";\npub const RELEASE_SIGNING_PUBLIC_KEY_BASE64: &str = "{public}";\n')

    def make(self, *arguments, commands=None, environment=None):
        commands = commands or Fake(self.root)
        output, errors = io.StringIO(), io.StringIO()
        arguments = list(arguments)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = make_release.main(arguments, commands, self.root, environment if environment is not None else {})
        return code, commands, errors.getvalue(), output.getvalue()

    def assertRefused(self, reason, *arguments, **options):
        code, commands, errors, _ = self.make(*arguments, **options)
        self.assertEqual(code, 1, errors)
        self.assertIn(reason, errors)
        self.assertFalse(commands.runs, "Nothing runs after a refusal")

    def test_a_release_runs_the_checks_then_signs_then_makes_a_draft(self):
        code, commands, errors, _ = self.make("--key", str(self.key))
        self.assertEqual(code, 0, errors)
        words = [" ".join(argv) for argv in commands.runs]
        order = ["build_release_notes.py", "cargo fmt --check", "cargo clippy", "ctest", "--target package",
                 "build_portable.py build", "build_portable.py verify build-portable/tomacco-1.2.3-Linux-x86_64-portable.tar.gz",
                 "build_source_release.py", "pkeyutl -sign", "gh release create"]
        positions = [next(i for i, line in enumerate(words) if word in line) for word in order]
        self.assertEqual(positions, sorted(positions), words)
        release = commands.runs[-1]
        self.assertEqual(release[:4], ["gh", "release", "create", "v1.2.3"])
        for flag in ("--draft", "--verify-tag"):
            self.assertIn(flag, release)
        self.assertEqual(release[release.index("--repo") + 1], "owner/repo")
        self.assertEqual(release[release.index("--title") + 1], "Tomacco 1.2.3")
        names = [Path(part).name for part in release if part.startswith("dist/")]
        self.assertEqual(sorted(names), sorted(path.name for path in (self.root / "dist").iterdir()))
        signature = self.root / "dist/tomacco-1.2.3-Linux-x86_64.tar.gz.sig"
        self.assertEqual(base64.b64decode(signature.read_bytes()), b"\x22" * 64)
        self.assertIn(str(signature.relative_to(self.root)), release)
        # The portable package is released beside it: checksum, signature, and nothing of its build tree.
        self.assertEqual(sorted(names), sorted([
            "tomacco-1.2.3-Linux-x86_64.tar.gz", "tomacco-1.2.3-Linux-x86_64.tar.gz.sha256", "tomacco-1.2.3-Linux-x86_64.tar.gz.sig",
            "tomacco-1.2.3-Linux-x86_64-portable.tar.gz", "tomacco-1.2.3-Linux-x86_64-portable.tar.gz.sha256",
            "tomacco-1.2.3-Linux-x86_64-portable.tar.gz.sig", "tomacco-1.2.3-source.tar.gz", "tomacco-1.2.3-source.tar.gz.sha256"]))
        signed = [argv[argv.index("-in") + 1] for argv in commands.runs if argv[:3] == ["openssl", "pkeyutl", "-sign"]]
        self.assertEqual([Path(path).name for path in signed],
                         ["tomacco-1.2.3-Linux-x86_64.tar.gz", "tomacco-1.2.3-Linux-x86_64-portable.tar.gz"])

    def test_no_portable_leaves_the_portable_package_out(self):
        code, commands, errors, _ = self.make("--key", str(self.key), "--no-portable")
        self.assertEqual(code, 0, errors)
        self.assertFalse(any("tools/build_portable.py" in argv for argv in commands.runs))
        self.assertFalse(any("portable" in path.name for path in (self.root / "dist").iterdir()))

    def test_a_portable_package_that_does_not_match_its_checksum_is_refused(self):
        class Corrupting(Fake):
            def run(self, argv):
                super().run(argv)
                if "tools/build_portable.py" in argv and "build" in argv:
                    (self.root / make_release.PORTABLE / "tomacco-1.2.3-Linux-x86_64-portable.tar.gz").write_bytes(b"changed")
        code, commands, errors, _ = self.make("--key", str(self.key), commands=Corrupting(self.root))
        self.assertEqual(code, 1)
        self.assertIn("portable.tar.gz does not match its checksum", errors)
        self.assertFalse(any(argv[0] == "gh" for argv in commands.runs))

    def test_a_dry_run_only_prints(self):
        before = sorted(path.name for path in self.root.iterdir())
        fake = Fake(self.root, dry_run=True)
        code, commands, errors, output = self.make("--dry-run", "--key", str(self.key), commands=fake)
        self.assertEqual(code, 0, errors)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), before)
        self.assertIn("gh release create v1.2.3", " ".join(" ".join(argv) for argv in commands.runs))
        self.assertIn("Would sign", output)
        self.assertIn("build-portable/tomacco-1.2.3-Linux-x86_64-portable.tar.gz", output)
        self.assertIn("tools/build_portable.py verify", " ".join(" ".join(argv) for argv in commands.runs))
        self.assertNotIn(self.key.read_text(), output)

    def test_a_real_dry_run_starts_nothing(self):
        commands = make_release.Commands(self.root, dry_run=True)
        with unittest.mock.patch.object(make_release.subprocess, "run", side_effect=AssertionError("started")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                commands.run(["gh", "release", "create", "v1"])
        self.assertIn("Would run: gh release create v1", output.getvalue())

    def test_a_dirty_tree_is_refused(self):
        self.assertRefused("not clean", "--key", str(self.key), commands=Fake(self.root, status=b" M file\n"))

    def test_a_missing_tag_is_named(self):
        self.assertRefused("There is no tag v1.2.3 yet", "--key", str(self.key), commands=Fake(self.root, tags=b""))

    def test_a_checkout_that_is_not_the_tag_is_refused(self):
        self.assertRefused("HEAD is not", "--key", str(self.key), commands=Fake(self.root, tagged=b"other\n"))

    def test_a_version_mismatch_is_refused(self):
        self.assertRefused("does not match", "--tag", "v9.9.9", "--key", str(self.key))
        (self.root / "assets/x.metainfo.xml").write_text("<component/>")
        self.assertRefused("no <release>", "--key", str(self.key))
        (self.root / "assets/x.metainfo.xml").write_text(METAINFO)
        (self.root / "Cargo.toml").write_text('version = "1.2.4"\n')
        self.assertRefused("disagree", "--key", str(self.key))

    def test_the_key_comes_from_outside_the_repository_and_only_when_named(self):
        self.assertRefused("Name the signing key")
        inside = self.root / "key.pem"
        inside.write_text("private")
        inside.chmod(0o600)
        self.assertRefused("outside the repository", "--key", str(inside))
        self.assertRefused("No signing key", "--key", str(self.key) + ".missing")
        self.key.chmod(0o644)
        self.assertRefused("only by its owner", "--key", str(self.key))
        self.key.chmod(0o600)
        code, _, errors, _ = self.make(environment={make_release.KEY_ENVIRONMENT: str(self.key)})
        self.assertEqual(code, 0, errors)

    def test_a_key_the_release_could_not_verify_is_refused(self):
        self.product("")
        self.assertRefused("no public key", "--key", str(self.key))
        self.product(base64.b64encode(b"\x33" * 32).decode())
        self.assertRefused("does not belong", "--key", str(self.key))

    def test_leftovers_in_dist_are_refused(self):
        (self.root / "dist").mkdir()
        (self.root / "dist/old.tar.gz").write_text("old")
        self.assertRefused("not empty", "--key", str(self.key))

    def test_a_package_that_does_not_match_its_checksum_is_refused(self):
        class Corrupting(Fake):
            def run(self, argv):
                super().run(argv)
                if "tools/build_source_release.py" in argv:
                    (self.root / make_release.DIST / "tomacco-1.2.3-source.tar.gz").write_bytes(b"changed")
        code, commands, errors, _ = self.make("--key", str(self.key), commands=Corrupting(self.root))
        self.assertEqual(code, 1)
        self.assertIn("does not match its checksum", errors)
        self.assertFalse(any(argv[0] == "gh" for argv in commands.runs))

    @unittest.skipUnless(shutil.which("openssl"), "needs openssl")
    def test_real_openssl_makes_the_signature_format_the_program_reads(self):
        key = self.key.with_name("throwaway.pem")
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(key)], check=True, capture_output=True)
        package = self.root / "tomacco-1.2.3-Linux-x86_64.tar.gz"
        package.write_bytes(b"package")
        signature = make_release.sign(make_release.Commands(self.root), key, package)
        self.assertEqual(len(base64.b64decode(signature.read_bytes())), 64)


class ReleaseFlow(unittest.TestCase):
    def test_no_workflow_signs_or_holds_a_signing_secret(self):
        self.assertFalse((ROOT / ".github/workflows/release.yml").exists())
        for workflow in (ROOT / ".github/workflows").glob("*.yml"):
            self.assertNotIn("SIGNING", workflow.read_text(), workflow.name)


if __name__ == "__main__":
    unittest.main()
