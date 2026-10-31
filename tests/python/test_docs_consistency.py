"""Facts the documentation repeats stay in step with their source: the version
(CMakeLists.txt), the operation count (assets/operations.json), the Rust and Qt
minimums for a build (Cargo.toml, CMakeLists.txt), the desktop languages
(i18n/) and the tomaccoctl flags (docs/tomaccoctl.1, kept in step with --help).
CHANGELOG.md is history and is not checked; the generated reference has its own test."""
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LANGUAGES = {"de": "German", "en": "English", "es": "Spanish", "fr": "French", "it": "Italian"}


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def flat(path):
    return " ".join(read(path).split())


def guides():
    """The tracked Markdown a reader follows: README, CONTRIBUTING, docs/ and examples/,
    without the changelog and the generated reference."""
    listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--", "*.md"], capture_output=True, text=True).stdout.split()
    if not listed:
        listed = [str(path.relative_to(ROOT)) for path in ROOT.glob("**/*.md") if "target" not in path.parts]
    return sorted(path for path in listed
                  if (path in ("README.md", "CONTRIBUTING.md") or path.startswith(("docs/", "examples/")))
                  and not path.startswith("docs/reference/") and (ROOT / path).is_file())


def version():
    return re.search(r"project\(\s*Tomacco\s+VERSION\s+([0-9.]+)", read("CMakeLists.txt")).group(1)


class ConsistencyTests(unittest.TestCase):
    longMessage = False

    def test_the_version_is_the_one_cmake_builds(self):
        current = version()
        self.assertEqual(re.search(r'^version = "([^"]+)"', read("Cargo.toml"), re.M).group(1), current, "Cargo.toml")
        readme = flat("README.md")
        self.assertTrue(f"badge/version-{current}-" in readme and f'alt="Version {current}"' in readme,
                        "README.md: the version badge is not " + current)
        tested = re.search(r"Tomacco (\d+\.\d+\.\d+) was developed and tested", readme)
        self.assertTrue(tested and tested.group(1) == current, "README.md: the tested version is not " + current)
        described = re.search(r"These guides describe version (\d+\.\d+\.\d+)", flat("docs/README.md"))
        self.assertTrue(described and described.group(1) == current, "docs/README.md: the version is not " + current)
        title = re.match(r"# Tomacco (\S+)", read("docs/RELEASE_NOTES.md"))
        self.assertTrue(title and title.group(1) == current, "docs/RELEASE_NOTES.md is not about " + current)
        for path in ("docs/getting-started.md", "docs/building.md"):
            for named in re.findall(r"tomacco-(\d+\.\d+\.\d+)-Linux", read(path)):
                self.assertEqual(named, current, f"{path} shows the package of {named}")

    def test_every_operation_count_is_the_schema_count(self):
        count = len(json.loads(read("assets/operations.json")))
        patterns = (r"\b(\d+) typed operations", r"[Oo]f the (\d+) operations", r"\b(\d+) in all\b",
                    r"\b(\d+) operations in all\b", r"API of (\d+) operations")
        found = 0
        for path in guides():
            document = flat(path)
            for pattern in patterns:
                for number in re.findall(pattern, document):
                    found += 1
                    self.assertEqual(int(number), count, f"{path} says {number} operations; the schema has {count}")
        self.assertTrue(found >= 3, "the operation counts this test checks are gone")

    def test_the_build_minimums_are_the_ones_the_build_enforces(self):
        rust = re.search(r'^rust-version = "([^"]+)"', read("Cargo.toml"), re.M).group(1)
        qt = re.search(r"find_package\(Qt6 (\S+) REQUIRED", read("CMakeLists.txt")).group(1)
        guide = flat("docs/getting-started.md")
        rusts = re.findall(r"Rust/Cargo (\d+\.\d+) or newer", guide) + re.findall(r"`cargo --version` is older than (\d+\.\d+)", guide)
        self.assertTrue(len(rusts) >= 2, "docs/getting-started.md no longer names the Rust minimum")
        self.assertEqual(set(rusts), {rust}, f"docs/getting-started.md names Rust {rusts}; Cargo.toml needs {rust}")
        qts = re.findall(r"you need Qt (\d+\.\d+) or newer", guide) + re.findall(r"Distributions with Qt (\d+\.\d+) or newer", guide)
        self.assertTrue(len(qts) >= 2, "docs/getting-started.md no longer names the Qt minimum")
        self.assertEqual(set(qts), {qt}, f"docs/getting-started.md names Qt {qts}; CMakeLists.txt needs {qt}")

    def test_every_language_list_names_every_translation(self):
        shipped = {LANGUAGES[path.stem.split("_", 1)[1]] for path in (ROOT / "i18n").glob("tomacco_*.ts")}
        self.assertTrue(len(shipped) >= 5)
        lists = 0
        for path in guides():
            for match in re.finditer(r"English(?:, [A-Z][a-z]+)*,? and [A-Z][a-z]+", flat(path)):
                context = flat(path)[max(0, match.start() - 80):match.end()]
                if "layout" in context or "keyboard" in context:
                    continue  # the virtual iPhone's keyboard layouts, not the desktop's languages
                lists += 1
                named = set(re.findall(r"[A-Z][a-z]+", match.group(0)))
                self.assertEqual(named, shipped, f"{path}: {match.group(0)!r} is not the set of desktop languages")
        self.assertTrue(lists >= 3, "the language lists this test checks are gone")

    def test_every_tomaccoctl_flag_a_guide_shows_exists(self):
        manual = read("docs/tomaccoctl.1").replace("\\-", "-")
        for path in guides():
            for line in read(path).splitlines():
                for command in re.findall(r"tomaccoctl (?:[a-z]+)(?: [^`'|]*?)?((?: --[a-z][a-z-]*)+)", line):
                    for flag in command.split():
                        self.assertIn(flag, manual, f"{path}: tomaccoctl has no flag {flag}")


if __name__ == "__main__":
    unittest.main()
