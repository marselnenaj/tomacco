"""Every relative link and anchor in every tracked Markdown document resolves.

Links in fenced code and inline code are examples, not links. Anchors follow
GitHub's rule for heading ids, and a heading inside a fence is no heading."""
import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIPPED_DIRECTORIES = {".git", "target", ".tooling", "references", "qa"}
LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
HTML_LINK = re.compile(r"<(?:a|img)\b[^>]*?\b(?:href|src)=\"([^\"]+)\"", re.I)
HTML_ANCHOR = re.compile(r"<[a-z]+\b[^>]*?\b(?:id|name)=\"([^\"]+)\"", re.I)
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
EXTERNAL = re.compile(r"[a-z][a-z0-9+.-]*:", re.I)


def documents():
    """The tracked .md and .md.in files; outside a Git checkout, a source tarball's."""
    try:
        listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", "*.md", "*.md.in"],
                                capture_output=True, text=True, check=True).stdout
        return sorted(ROOT / name for name in listed.split("\0") if name and (ROOT / name).is_file())
    except (OSError, subprocess.CalledProcessError):
        found = []
        for directory, names, files in os.walk(ROOT):
            names[:] = [name for name in names if name not in SKIPPED_DIRECTORIES and not name.startswith("build")]
            found += [Path(directory) / name for name in files if name.endswith((".md", ".md.in"))]
        return sorted(found)


def prose_lines(text):
    """(line number, line) outside fenced code, with inline code removed."""
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        match = FENCE.match(line)
        if fence is None and match:
            fence = match.group(1)
        elif fence is not None:
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) and not match.group(2).strip():
                fence = None
        else:
            yield number, re.sub(r"(`+).*?\1", "", line)


def github_slug(heading):
    """The id GitHub gives a heading, without the -1 of a repeated one."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"<[^>]+>", "", text).replace("`", "")
    text = re.sub(r"(\*\*|__)", "", text)
    text = re.sub(r"[^\w\- ]", "", text.strip().lower())
    return text.replace(" ", "-")


def anchors(path):
    """Every anchor the document offers, as GitHub generates them."""
    found, seen = set(), {}
    fence = None
    for line in path.read_text(encoding="utf-8").splitlines():
        match = FENCE.match(line)
        if fence is None and match:
            fence = match.group(1)
            continue
        if fence is not None:
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) and not match.group(2).strip():
                fence = None
            continue
        heading = HEADING.match(line)
        if heading:
            slug = github_slug(heading.group(2))
            count = seen.get(slug, 0)
            seen[slug] = count + 1
            found.add(slug if count == 0 else f"{slug}-{count}")
        found.update(HTML_ANCHOR.findall(line))
    return found


def problems(path):
    text = path.read_text(encoding="utf-8")
    base = path.parent
    for number, line in prose_lines(text):
        for target in [*LINK.findall(line), *HTML_LINK.findall(line)]:
            if EXTERNAL.match(target):
                continue
            location, _, anchor = target.partition("#")
            resolved = (base / location).resolve() if location else path
            where = f"{path.relative_to(ROOT)}:{number}: {target}"
            if not resolved.is_relative_to(ROOT):
                yield f"{where} leaves the repository"
            elif not resolved.exists():
                yield f"{where} names a file that does not exist"
            elif anchor and resolved.suffix == ".md" and anchor not in anchors(resolved):
                yield f"{where} names an anchor that does not exist"


class DocumentLinkTests(unittest.TestCase):
    longMessage = False

    def test_the_slugs_follow_github(self):
        self.assertEqual(github_slug("Guest speed, measured"), "guest-speed-measured")
        self.assertEqual(github_slug("What 1.0.0 ships without (draft)"), "what-100-ships-without-draft")
        self.assertEqual(github_slug("1. Check the requirements"), "1-check-the-requirements")
        self.assertEqual(github_slug("`job.wait` and [links](x.md)"), "jobwait-and-links")

    def test_every_relative_link_and_anchor_resolves(self):
        everything = documents()
        self.assertTrue(len(everything) > 20, "too few documents found")
        found = [problem for path in everything for problem in problems(path)]
        self.assertEqual(found, [], "\n".join(found))


if __name__ == "__main__":
    unittest.main()
