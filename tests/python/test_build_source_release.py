"""The corresponding-source tarball a release offers (tools/build_source_release.py)."""
import contextlib
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import build_source_release

# Stands in for cargo: a vendored crate with an executable script, and the
# configuration cargo vendor prints.
FAKE_CARGO = """#!/bin/sh
mkdir -p vendor/example-1.0.0 || exit 1
echo 'pub fn example() {}' > vendor/example-1.0.0/lib.rs
printf '#!/bin/sh\\n' > vendor/example-1.0.0/run.sh
chmod 755 vendor/example-1.0.0/run.sh
echo "$@" > vendor/arguments
printf '[source.crates-io]\\nreplace-with = "vendored-sources"\\n'
"""


class SourceReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tomacco-source-release-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root / "repository"
        (self.repository / "tools").mkdir(parents=True)
        self.write("CMakeLists.txt", "project(\n  Tomacco VERSION 9.8.7 LANGUAGES CXX)\n")
        self.write("Cargo.toml", "[package]\n")
        self.write(".gitignore", "qa/\n.tooling/\n")
        self.write("tools/recipe.py", 'ENGINE = ("https://example.org/engine.git",\n          "' + "a" * 40 + '")\n'
                                      'UPSTREAM = "https://example.org/plugin"\nREVISION = "' + "b" * 40 + '"\n')
        self.write("qa/record.json", "local\n")
        self.write(".tooling/tool", "local\n")
        self.git("init", "-q")
        self.commit()
        self.cargo = self.root / "cargo"
        self.cargo.write_text(FAKE_CARGO)
        self.cargo.chmod(0o755)

    def write(self, name, text):
        path = self.repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def git(self, *arguments):
        environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        subprocess.run(["git", "-C", str(self.repository), "-c", "user.name=Test", "-c", "user.email=test@example.org",
                        *arguments], check=True, capture_output=True, env=environment)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "state")

    def build(self, output="output", **options):
        return build_source_release.build(self.repository, self.root / output, cargo=str(self.cargo), **options)

    def members(self, tarball):
        with tarfile.open(tarball) as archive:
            return {member.name: member for member in archive.getmembers()}

    def test_the_tarball_holds_head_and_the_vendored_crates_and_says_what_it_lacks(self):
        tarball, checksum, value = self.build()
        self.assertEqual(tarball.name, "tomacco-9.8.7-source.tar.gz")
        self.assertEqual(checksum.read_text(), f"{value}  {tarball.name}\n")
        self.assertEqual(hashlib.sha256(tarball.read_bytes()).hexdigest(), value)
        members = self.members(tarball)
        prefix = "tomacco-9.8.7-source/"
        for name in ("CMakeLists.txt", "Cargo.toml", "tools/recipe.py", "vendor/example-1.0.0/lib.rs",
                     ".cargo/config.toml", "SOURCE_RELEASE.md"):
            self.assertIn(prefix + name, members)
        self.assertFalse([name for name in members if "/qa/" in name or "/.tooling/" in name])
        self.assertTrue(all(name.startswith(prefix) for name in members))
        self.assertEqual(members[prefix + "vendor/example-1.0.0/run.sh"].mode, 0o755)
        self.assertEqual(members[prefix + "Cargo.toml"].mode, 0o644)
        with tarfile.open(tarball) as archive:
            read = lambda name: archive.extractfile(prefix + name).read().decode()
            self.assertIn("vendored-sources", read(".cargo/config.toml"))
            self.assertEqual(read("vendor/arguments").split(), ["vendor", "--locked", "--versioned-dirs", "vendor"])
            release = read("SOURCE_RELEASE.md")
        commit = subprocess.run(["git", "-C", str(self.repository), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.assertIn(f"Commit `{commit}`", release)
        for pin in ("https://example.org/engine.git | `" + "a" * 40, "https://example.org/plugin | `" + "b" * 40):
            self.assertIn(pin, release)
        self.assertIn("Apple data", release)
        self.assertNotIn(str(self.root), release)

    def test_the_same_commit_gives_the_same_bytes(self):
        first, _, value = self.build("one")
        os.utime(self.repository / "Cargo.toml", (1, 1))
        second, _, again = self.build("two")
        self.assertEqual((first.name, value), (second.name, again))
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual({member.mtime for member in self.members(first).values()},
                         {int(subprocess.run(["git", "-C", str(self.repository), "log", "-1", "--format=%ct"],
                                             capture_output=True, text=True).stdout)})
        self.assertEqual({(member.uid, member.gid, member.uname, member.gname) for member in self.members(first).values()},
                         {(0, 0, "", "")})
        self.assertEqual(sorted(self.root.glob("one/.source-release-*")), [])

    def test_a_dirty_tree_is_refused_unless_allowed_and_the_tarball_then_holds_head(self):
        self.write("Cargo.toml", "[package]\nname = 'uncommitted'\n")
        with self.assertRaisesRegex(build_source_release.Refused, "uncommitted changes"):
            self.build()
        self.assertFalse((self.root / "output").exists())
        self.git("checkout", "--", "Cargo.toml")
        self.write("untracked.txt", "new\n")
        with self.assertRaisesRegex(build_source_release.Refused, "uncommitted changes"):
            self.build()
        self.write("Cargo.toml", "[package]\nname = 'uncommitted'\n")
        tarball, _, _ = self.build(allow_dirty=True)
        prefix = "tomacco-9.8.7-source/"
        self.assertNotIn(prefix + "untracked.txt", self.members(tarball))
        with tarfile.open(tarball) as archive:
            self.assertEqual(archive.extractfile(prefix + "Cargo.toml").read(), b"[package]\n")
            self.assertIn(b"--allow-dirty", archive.extractfile(prefix + "SOURCE_RELEASE.md").read())

    def test_what_must_not_be_published_is_refused(self):
        cases = (("qa/kept.txt", "text", "local state .*qa"),
                 (".tooling/kept.txt", "text", "local state .*\\.tooling"),
                 ("assets/iPhone.ipsw", "text", "Apple's data: assets/iPhone.ipsw"),
                 ("assets/program", None, "Apple's data: assets/program"),
                 ("vendor/mine.rs", "text", "vendor/ or .cargo/"))
        for name, text, reason in cases:
            with self.subTest(name=name):
                path = self.repository / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(bytes.fromhex("cffaedfe") + bytes(8) if text is None else text.encode())
                self.git("add", "-f", name)
                self.git("commit", "-qm", "carry")
                with self.assertRaisesRegex(build_source_release.Refused, reason):
                    self.build()
                self.git("rm", "-qf", "--cached", name)
                self.git("commit", "-qm", "drop")
                path.unlink()
        self.assertEqual(self.build()[0].name, "tomacco-9.8.7-source.tar.gz")

    def test_a_cargo_that_fails_names_the_registry_and_leaves_nothing(self):
        self.cargo.write_text("#!/bin/sh\necho 'no matching package' >&2\nexit 101\n")
        output = self.root / "output"
        with self.assertRaisesRegex(build_source_release.Refused, r"local registry alone \(--offline\): no matching package"):
            build_source_release.build(self.repository, output, offline=True, cargo=str(self.cargo))
        self.assertEqual(list(output.iterdir()), [])

    def test_the_command_prints_the_digest_and_fails_cleanly(self):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            self.assertEqual(build_source_release.main(["--repository", str(self.repository), "--output", str(self.root / "out"),
                                                        "--cargo", str(self.cargo)]), 0)
        value = hashlib.sha256((self.root / "out/tomacco-9.8.7-source.tar.gz").read_bytes()).hexdigest()
        self.assertIn(f"SHA-256 {value}", printed.getvalue())
        self.write("dirty.txt", "x")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(build_source_release.main(["--repository", str(self.repository), "--output", str(self.root / "out"),
                                                        "--cargo", str(self.cargo)]), 1)
        self.assertIn("Refused:", errors.getvalue())

    def test_the_real_recipes_pins_are_found_and_the_notice_names_the_tarball(self):
        pins = build_source_release.pinned_sources(ROOT / "tools")
        names = {pin[0]: pin[2] for pin in pins}
        for name, commit in (("qemu-sptm", "bad6336fb24211f8c4e8e946c259bd5b12417340"),
                             ("vphone-cli", "3a819b0dd00aea8906ddd8d33c9364736a869ca0"),
                             ("orchard", "c73a2bda1fd4afefd61204bb74afd74292ef2fe9")):
            self.assertEqual(names.get(name), commit)
        notice = (ROOT / "docs/SOURCE.md.in").read_text()
        self.assertIn("tomacco-@TOMACCO_VERSION@-source.tar.gz", notice)
        self.assertIn("SOURCE_RELEASE.md", notice)
        self.assertIn("`vendor/`", notice)


if __name__ == "__main__":
    unittest.main()
