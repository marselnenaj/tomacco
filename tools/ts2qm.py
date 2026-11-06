#!/usr/bin/env python3
"""Compile a Qt Linguist .ts file into a .qm file that QTranslator loads.

The build uses Qt's own lrelease (qt_add_translations) whenever Qt's
LinguistTools are installed. This is the fallback for hosts without them
(on Arch they ship in qt6-tools), so the translated interface also builds there.
It writes the same binary format as lrelease for what the .ts files here use:
contexts, optional disambiguation comments, plural forms, and the numerus
rules of the target language, which come from the registry (i18n/languages.json
through tools/tomacco_i18n.py: the same rule families as lrelease's numerus.cpp,
from one form for Japanese to six for Arabic). Unfinished, obsolete and
vanished messages are left out, as lrelease does by default.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tomacco_i18n as i18n  # noqa: E402

# Kept for callers that read them from this module.
MAGIC = i18n.MAGIC
elf_hash = i18n.elf_hash


def compile_ts(path: str) -> bytes:
    catalog = i18n.read_ts(Path(path))
    try:
        rule = i18n.plural_rule_for_ts_language(catalog.language)
    except KeyError:
        raise SystemExit(f"{path}: no numerus rules for language {catalog.language!r} (not in i18n/languages.json)")
    try:
        return i18n.compile_catalog(catalog, rule)
    except ValueError as error:
        raise SystemExit(f"{path}: {error}")


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: ts2qm.py input.ts output.qm")
    payload = compile_ts(sys.argv[1])
    with open(sys.argv[2], "wb") as handle:
        handle.write(payload)


if __name__ == "__main__":
    main()
