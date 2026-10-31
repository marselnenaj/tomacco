#!/usr/bin/env python3
"""Write the body of a GitHub release from docs/RELEASE_NOTES.md.

Relative links do not work in a release body, so each becomes a link into the
tag on GitHub. It refuses a tag that is not the version CMake builds, and notes
still marked as waiting for a measurement.
"""
import argparse
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/marselnenaj/tomacco"
PENDING = "<!-- numbers: final measurement pending -->"
LINK = re.compile(r"(\]\()([^)\s]+)(\))")


def project_version(root):
    found = re.search(r"project\(\s*Tomacco\s+VERSION\s+([0-9][0-9.]*)", (root / "CMakeLists.txt").read_text())
    if not found:
        raise SystemExit("CMakeLists.txt names no project version")
    return found.group(1)


def body(root, tag):
    """The release body for `tag`; SystemExit names what stands in the way."""
    version = project_version(root)
    if tag != f"v{version}":
        raise SystemExit(f"The tag {tag} is not this source's version, v{version}")
    text = (root / "docs/RELEASE_NOTES.md").read_text(encoding="utf-8")
    if PENDING in text:
        raise SystemExit("docs/RELEASE_NOTES.md still says " + PENDING + ": put in the final numbers and remove the comment")
    if not text.startswith(f"# Tomacco {version}\n"):
        raise SystemExit(f"docs/RELEASE_NOTES.md does not start with the heading of Tomacco {version}")

    def link(match):
        target = match.group(2)
        if re.match(r"[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("#"):
            return match.group(0)
        path, _, anchor = target.partition("#")
        resolved = os.path.normpath(f"docs/{path}").replace(os.sep, "/")
        return f"{match.group(1)}{REPOSITORY}/blob/{tag}/{resolved}{'#' + anchor if anchor else ''}{match.group(3)}"
    return LINK.sub(link, text)


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="The release's tag, such as v1.0.0")
    parser.add_argument("--output", type=Path, required=True, help="The file for the release body")
    options = parser.parse_args(arguments)
    try:
        text = body(ROOT, options.tag)
    except SystemExit as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(text, encoding="utf-8")
    print(options.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
