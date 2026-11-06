#!/usr/bin/env python3
"""Keep the desktop's translation catalogs in step with the code and measure them.

  i18n_catalog.py import-registry [--omarchy FILE]   refresh i18n/languages.json from Omarchy's registry
  i18n_catalog.py check-registry [--omarchy FILE]    compare it with the installed Omarchy registry (read-only)
  i18n_catalog.py sync [CODE...]                     add new texts as unfinished, mark removed ones vanished
  i18n_catalog.py sources [--language CODE] [--unfinished] [--context NAME] [--start N] [--count N]
                                                     list the texts to translate, numbered
  i18n_catalog.py apply CODE FILE                    merge numbered translations (index<TAB>JSON) into the catalog
  i18n_catalog.py report [--json] [--write FILE]     coverage per language: finished, unfinished, problems

English is the source language; i18n/tomacco_en.ts carries only its plural
forms. Every other language in the registry has i18n/tomacco_CODE.ts with
every marked text. A text's number in `sources` is its position in the
catalog order shared by every language, so a translator can work on one
slice at a time; `apply` checks the slice against the texts it was made for.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tomacco_i18n as i18n  # noqa: E402

ROOT = i18n.ROOT
TEMPLATE = ROOT / "i18n" / "tomacco_de.ts"
DESKTOP_ENTRY = ROOT / "assets" / "io.github.marselnenaj.tomacco.desktop"

# The plural-rule family lrelease applies to each language (numerus.cpp).
PLURAL_FAMILY = {
    "universal": ["ja", "ko", "zh-CN", "zh-TW", "th", "vi", "id", "ms", "hu", "tr", "fa"],
    "english": ["en", "da", "fi", "el", "hi", "es", "es-MX", "it", "pt", "de", "nl", "no", "sv", "bn", "ca", "si", "ta", "uz", "sw",
                "he", "bg", "et", "ur"],
    "french": ["fr", "pt-BR"],
    "icelandic": ["is"], "latvian": ["lv"], "irish": ["ga"], "slovak": ["cs", "sk"], "lithuanian": ["lt"],
    "russian": ["ru", "uk", "hr", "sr"], "polish": ["pl"], "romanian": ["ro"], "slovenian": ["sl"], "arabic": ["ar"], "tagalog": ["fil"],
}
PLURAL_OF = {code: family for family, codes in PLURAL_FAMILY.items() for code in codes}
# The .ts language attribute lrelease reads; Qt knows Norwegian as nb.
QT_LOCALE_OF = {"no": "nb_NO"}
# Interface languages Omarchy's registry does not list.
EXTRA_LANGUAGES = [{"code": "es", "name": "Español", "englishName": "Spanish", "formatLocale": "es-ES", "locale": "es_ES.UTF-8",
                    "decimal": ",", "zeroDigit": "0"}]
# Tags the desktop maps onto a registry language when the system or Omarchy names them differently.
ALIASES = {"nb": "no", "nn": "no", "nb-NO": "no", "nn-NO": "no", "tl": "fil", "zh": "zh-CN", "zh-Hans": "zh-CN", "zh-SG": "zh-CN",
           "zh-Hant": "zh-TW", "zh-HK": "zh-TW", "zh-MO": "zh-TW", "pt-PT": "pt", "es-ES": "es", "es-419": "es-MX", "es-US": "es-MX",
           "iw": "he", "in": "id", "sr-Cyrl": "sr", "sr-Latn": "sr", "uz-Latn": "uz"}


def omarchy_file(argument: str | None) -> Path:
    path = Path(argument) if argument else i18n.omarchy_registry_path(os.environ)
    if path is None or not path.is_file():
        raise SystemExit("no Omarchy registry found; pass --omarchy FILE (read only)")
    return path


def import_registry(args) -> int:
    source = omarchy_file(args.omarchy)
    theirs = json.loads(source.read_text(encoding="utf-8"))
    existing = i18n.load_registry() if i18n.REGISTRY_PATH.exists() else None
    known = existing.by_code() if existing else {}
    languages = []
    for code, entry in list(theirs.items()) + [(extra["code"], extra) for extra in EXTRA_LANGUAGES]:
        if code not in PLURAL_OF:
            raise SystemExit(f"{code}: add its plural-rule family to PLURAL_FAMILY first")
        old = known.get(code)
        languages.append(i18n.Language(
            code=code, name=entry["name"], english_name=entry["englishName"], format_locale=entry["formatLocale"], system_locale=entry["locale"],
            qt_locale=old.qt_locale if old else QT_LOCALE_OF.get(code, code.replace("-", "_")), direction=entry.get("direction", "ltr"),
            decimal=entry.get("decimal", "."), zero_digit=entry.get("zeroDigit", "0"), plural=PLURAL_OF[code], omarchy=code in theirs,
            complete=old.complete if old else False, extra=old.extra if old else {}))
    languages.sort(key=lambda language: (language.code != i18n.SOURCE_LANGUAGE, language.code))
    provenance = {"omarchy": {"file": str(source), "sha256": i18n.sha256_of(source), "languages": len(theirs), "imported": date.today().isoformat(),
                              "note": "Omarchy's default/i18n/locales.json; refresh with tools/i18n_catalog.py import-registry, compare with check-registry"}}
    registry = i18n.Registry(languages=languages, aliases=dict(existing.aliases if existing else ALIASES) or dict(ALIASES), source=provenance)
    i18n.save_registry(registry)
    print(f"{i18n.REGISTRY_PATH.relative_to(ROOT)}: {len(languages)} languages from {source} ({provenance['omarchy']['sha256'][:12]})")
    return 0


def check_registry(args) -> int:
    registry = i18n.load_registry()
    result = i18n.compare_with_omarchy(registry, omarchy_file(args.omarchy))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["in_step"] else 1


# ---------------------------------------------------------------------------
# Catalog order and synchronisation
# ---------------------------------------------------------------------------
def canonical_messages() -> list[i18n.Message]:
    """Every marked text in the shared order: the template catalog's order, then new texts by context."""
    marked = i18n.marked_strings()
    ordered: list[i18n.Message] = []
    seen = set()
    if TEMPLATE.exists():
        for message in i18n.read_ts(TEMPLATE).messages:
            if message.key in marked and message.key not in seen:
                seen.add(message.key)
                ordered.append(i18n.Message(context=message.context, source=message.source, comment=message.comment, numerus=marked[message.key]))
    for key in sorted(k for k in marked if k not in seen):
        ordered.append(i18n.Message(context=key[0], source=key[1], comment=key[2], numerus=marked[key]))
    return ordered


def sources_digest(messages: list[i18n.Message]) -> str:
    return hashlib.sha256("\n".join("\t".join(message.key) for message in messages).encode("utf-8")).hexdigest()[:12]


def synced_catalog(language: i18n.Language, canonical: list[i18n.Message]) -> i18n.Catalog:
    """The language's catalog with every marked text (new ones unfinished, appended to their context) and
    removed ones marked vanished where they are. A file keeps its order; a new file follows the shared order."""
    forms = i18n.plural_form_count(language.plural)
    existing = i18n.read_ts(language.ts_path) if language.ts_path.exists() else i18n.Catalog(language.qt_locale, i18n.SOURCE_LANGUAGE, [])
    wanted = {message.key: message for message in canonical}
    messages: list[i18n.Message] = []
    seen = set()
    for message in existing.messages:
        target = wanted.get(message.key)
        if target is None:
            if any(message.forms):
                message.state = "vanished"
                messages.append(message)
            continue
        seen.add(message.key)
        message.numerus = target.numerus
        needed = forms if target.numerus else 1
        if len(message.forms) != needed:
            message.forms = (message.forms + [""] * needed)[:needed]
        if not all(message.forms):
            message.state = "unfinished"
        elif message.state in ("vanished", "obsolete", "unfinished") and message.state != "unfinished":
            message.state = ""
        messages.append(message)
    for target in canonical:
        if target.key in seen:
            continue
        messages.append(i18n.Message(context=target.context, source=target.source, comment=target.comment, numerus=target.numerus,
                                     forms=[""] * (forms if target.numerus else 1), state="unfinished"))
    # Contexts stay together, in the order of their first appearance (the file's, then the shared order).
    order: dict[str, int] = {}
    for message in messages:
        order.setdefault(message.context, len(order))
    messages.sort(key=lambda message: order[message.context])
    return i18n.Catalog(language=language.qt_locale, source_language=i18n.SOURCE_LANGUAGE, messages=messages)


def sync(args) -> int:
    registry = i18n.load_registry()
    canonical = canonical_messages()
    codes = args.codes or [language.code for language in registry.translated()]
    for code in codes:
        language = registry.get(code)
        catalog = synced_catalog(language, canonical)
        # A file whose content is already right is left alone, so hand-kept files do not churn.
        if not language.ts_path.exists() or not i18n.same_catalog(catalog, i18n.read_ts(language.ts_path)):
            i18n.write_ts(catalog, language.ts_path)
        unfinished = sum(1 for message in catalog.messages if message.live and not message.finished)
        print(f"{language.ts_path.relative_to(ROOT)}: {len(canonical)} texts, {unfinished} unfinished")
    return 0


def sources(args) -> int:
    canonical = canonical_messages()
    digest = sources_digest(canonical)
    catalog = None
    if args.language:
        language = i18n.load_registry().get(args.language)
        catalog = synced_catalog(language, canonical).by_key()
    print(f"# sources {digest} {len(canonical)} texts")
    shown = 0
    for index, message in enumerate(canonical):
        if args.context and message.context != args.context:
            continue
        if index < args.start:
            continue
        if catalog is not None and args.unfinished and catalog[message.key].finished:
            continue
        fields = [str(index), message.context, json.dumps(message.source, ensure_ascii=False)]
        if message.comment:
            fields.append("comment=" + json.dumps(message.comment, ensure_ascii=False))
        if message.numerus:
            fields.append("plural")
        print("\t".join(fields))
        shown += 1
        if args.count and shown >= args.count:
            break
    return 0


def apply(args) -> int:
    registry = i18n.load_registry()
    language = registry.get(args.code)
    canonical = canonical_messages()
    digest = sources_digest(canonical)
    catalog = synced_catalog(language, canonical)
    by_key = catalog.by_key()
    forms_needed = i18n.plural_form_count(language.plural)
    applied, rejected = 0, []
    lines = Path(args.file).read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        if line.startswith("#"):
            if line.startswith("# sources") and line.split()[2] != digest and not args.force:
                raise SystemExit(f"{args.file}: made for sources {line.split()[2]}, the code now has {digest}; run `sources` again (or --force)")
            continue
        try:
            index_text, value_text = line.split("\t", 1)
            index = int(index_text)
            value = json.loads(value_text)
        except (ValueError, json.JSONDecodeError) as error:
            rejected.append((number, line[:60], f"unreadable line: {error}"))
            continue
        if not 0 <= index < len(canonical):
            rejected.append((number, line[:60], "no such text"))
            continue
        wanted = canonical[index]
        message = by_key[wanted.key]
        forms = value if isinstance(value, list) else [value]
        if not all(isinstance(form, str) and form.strip() for form in forms):
            rejected.append((number, wanted.source[:60], "empty translation"))
            continue
        if wanted.numerus and len(forms) != forms_needed:
            rejected.append((number, wanted.source[:60], f"{len(forms)} plural forms, {language.plural} needs {forms_needed}"))
            continue
        if not wanted.numerus and len(forms) != 1:
            rejected.append((number, wanted.source[:60], "a list for a text without plural forms"))
            continue
        candidate = i18n.Message(context=wanted.context, source=wanted.source, comment=wanted.comment, numerus=wanted.numerus, forms=forms)
        problems = i18n.translation_problems(candidate, language.plural)
        if problems:
            rejected.append((number, wanted.source[:60], "; ".join(problems)))
            continue
        message.forms = forms
        message.state = ""
        applied += 1
    i18n.write_ts(catalog, language.ts_path)
    unfinished = sum(1 for message in catalog.messages if message.live and not message.finished)
    print(f"{language.ts_path.relative_to(ROOT)}: {applied} applied, {len(rejected)} rejected, {unfinished} still unfinished")
    for number, text, reason in rejected:
        print(f"  line {number}: {reason}: {text}")
    return 1 if rejected else 0


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------
def desktop_locale(code: str, registry: i18n.Registry | None = None) -> str:
    """The LOCALE of Key[LOCALE]= in the menu entry: the Qt locale (pt_BR, zh_CN, nb_NO), as the Desktop Entry
    specification spells regions with an underscore."""
    registry = registry or i18n.load_registry()
    return registry.get(code).qt_locale


def desktop_entry_keys(code: str, registry: i18n.Registry | None = None) -> tuple[int, int]:
    """How many of the German-translated keys of the menu entry this language also has."""
    locale = desktop_locale(code, registry)
    text = DESKTOP_ENTRY.read_text(encoding="utf-8")
    wanted = have = 0
    for section in text.split("\n\n"):
        for key in set(line.split("[")[0] for line in section.splitlines() if "[de]=" in line):
            wanted += 1
            if any(line.startswith(f"{key}[{locale}]=") and line.split("=", 1)[1].strip() for line in section.splitlines()):
                have += 1
    return have, wanted


def qt_translations_dir() -> Path | None:
    try:
        output = subprocess.run(["qmake6", "-query", "QT_INSTALL_TRANSLATIONS"], capture_output=True, text=True, timeout=10).stdout.strip()
        if output and Path(output).is_dir():
            return Path(output)
    except (OSError, subprocess.SubprocessError):
        pass
    fallback = Path("/usr/share/qt6/translations")
    return fallback if fallback.is_dir() else None


def qt_catalog_for(language: i18n.Language, directory: Path | None) -> str:
    """Which of Qt's own catalogs (dialog buttons, Qt Quick texts) this host offers for the language."""
    if directory is None:
        return "unknown"
    for prefix in ("qt", "qtbase"):
        for name in (language.qt_locale, language.qt_locale.split("_")[0]):
            if (directory / f"{prefix}_{name}.qm").is_file():
                return f"{prefix}_{name}"
    return "none"


def coverage(registry: i18n.Registry) -> dict:
    canonical = canonical_messages()
    wanted = {message.key: message for message in canonical}
    translations_dir = qt_translations_dir()
    rows = []
    for language in registry.translated():
        row = {"code": language.code, "name": language.name, "englishName": language.english_name, "direction": language.direction,
               "plural": language.plural, "forms": i18n.plural_form_count(language.plural), "complete": language.complete, "omarchy": language.omarchy,
               "texts": len(canonical), "finished": 0, "unfinished": 0, "missing": 0, "stale": 0, "problems": [], "suspicious": [],
               "qt_catalog": qt_catalog_for(language, translations_dir), "file": str(language.ts_path.relative_to(ROOT))}
        if not language.ts_path.exists():
            row["missing"] = len(canonical)
            row["status"] = "no catalog"
            rows.append(row)
            continue
        catalog = i18n.read_ts(language.ts_path)
        if catalog.language.lower() != language.qt_locale.lower():
            row["problems"].append(f"language attribute {catalog.language!r}, registry says {language.qt_locale!r}")
        present = catalog.by_key()
        for key, message in present.items():
            if key not in wanted:
                if message.live and any(message.forms):
                    row["stale"] += 1
                continue
            if message.numerus != wanted[key].numerus:
                row["problems"].append(f"{key[0]}: {key[1][:50]!r}: plural marking differs from the code")
            if message.finished:
                problems = i18n.translation_problems(message, language.plural)
                if problems:
                    row["problems"].append(f"{key[0]}: {key[1][:50]!r}: " + "; ".join(problems))
                else:
                    row["finished"] += 1
                if any(i18n.looks_untranslated(language.code, message.source, form) for form in message.forms):
                    row["suspicious"].append(f"{key[0]}: {key[1][:60]!r}")
            else:
                row["unfinished"] += 1
        row["missing"] = sum(1 for key in wanted if key not in present)
        have, needed = desktop_entry_keys(language.code, registry)
        row["desktop_entry"] = f"{have}/{needed}"
        row["desktop_entry_complete"] = have == needed
        translated_all = row["finished"] == len(canonical) and not row["problems"] and not row["stale"]
        row["status"] = "complete" if translated_all and row["desktop_entry_complete"] else ("partial" if row["finished"] else "untranslated")
        row["ratio"] = round(row["finished"] / len(canonical), 4) if canonical else 1.0
        rows.append(row)
    return {"texts": len(canonical), "digest": sources_digest(canonical), "languages": rows,
            "complete": sorted(row["code"] for row in rows if row["status"] == "complete"),
            "incomplete": sorted(row["code"] for row in rows if row["status"] != "complete"),
            "qt_translations": str(translations_dir) if translations_dir else None}


def coverage_markdown(data: dict) -> str:
    lines = ["# Translation coverage", "",
             f"Generated by `python3 tools/i18n_catalog.py report --write i18n/COVERAGE.md` for {data['texts']} marked interface texts",
             f"(sources `{data['digest']}`). *Complete* means every text has a finished translation, the plural forms match",
             "Qt's rules for the language, placeholders and mark-up survive, nothing stale is live, and the menu entry is translated.",
             "Qt's own texts (standard dialog buttons) come from the host's Qt translations; `report --json` lists which catalog each",
             "language finds on the reporting machine. *Suspicious* counts finished translations of three or more words",
             "that repeat the English text or show none of the language's script; names and codes legitimately do, so it is a",
             "review list, not an error count.", "",
             "| Code | Language | Direction | Plural forms (Qt) | Finished | Unfinished | Suspicious | Problems | Menu entry | Status |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for row in data["languages"]:
        lines.append(f"| {row['code']} | {row['name']} ({row['englishName']}) | {row['direction']} | {row['forms']} ({row['plural']}) | "
                     f"{row['finished']}/{row['texts']} | {row['unfinished'] + row['missing']} | {len(row['suspicious'])} | {len(row['problems'])} | "
                     f"{row.get('desktop_entry', '0/0')} | {row['status']} |")
    lines += ["", f"Complete: {len(data['complete'])} of {len(data['languages'])} translated languages" + (": " + ", ".join(data["complete"]) if data["complete"] else ""),
              "", f"Not complete: " + (", ".join(data["incomplete"]) if data["incomplete"] else "none"), ""]
    problems = [(row["code"], problem) for row in data["languages"] for problem in row["problems"]]
    if problems:
        lines += ["## Problems", ""] + [f"- {code}: {problem}" for code, problem in problems[:200]] + [""]
    return "\n".join(lines)


def report(args) -> int:
    data = coverage(i18n.load_registry())
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(coverage_markdown(data))
    if args.write:
        Path(args.write).write_text(coverage_markdown(data), encoding="utf-8")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("import-registry"); p.add_argument("--omarchy"); p.set_defaults(run=import_registry)
    p = commands.add_parser("check-registry"); p.add_argument("--omarchy"); p.set_defaults(run=check_registry)
    p = commands.add_parser("sync"); p.add_argument("codes", nargs="*"); p.set_defaults(run=sync)
    p = commands.add_parser("sources"); p.add_argument("--language"); p.add_argument("--unfinished", action="store_true")
    p.add_argument("--context"); p.add_argument("--start", type=int, default=0); p.add_argument("--count", type=int, default=0); p.set_defaults(run=sources)
    p = commands.add_parser("apply"); p.add_argument("code"); p.add_argument("file"); p.add_argument("--force", action="store_true"); p.set_defaults(run=apply)
    p = commands.add_parser("report"); p.add_argument("--json", action="store_true"); p.add_argument("--write"); p.set_defaults(run=report)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
