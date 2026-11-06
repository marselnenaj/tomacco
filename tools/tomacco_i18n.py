"""What the desktop knows about its languages, shared by the build, the tests and the tools.

i18n/languages.json is the registry: every interface language with its native
name, Omarchy's registry data (format locale, direction, digits), the Qt
locale its .ts file is written for and the plural-rule family Qt's lrelease
uses for it. tools/ts2qm.py compiles with these rules, tools/i18n_catalog.py
keeps the .ts files in step with the code and measures their coverage, and
src/backend.cpp reads the same file at run time. tests/python/test_i18n.py
checks that everything agrees.
"""

from __future__ import annotations

import glob
import hashlib
import json
import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "i18n" / "languages.json"
SOURCE_LANGUAGE = "en"

# ---------------------------------------------------------------------------
# Qt's plural rules (qttools/src/linguist/shared/numerus.cpp, evaluated by
# qtbase's QTranslator::numerusHelper). A rule set is the byte program lrelease
# writes into the .qm, and the form index for a count is the first rule that
# holds, or the number of rules when none does. The names are lrelease's.
# ---------------------------------------------------------------------------
Q_EQ, Q_LT, Q_LEQ, Q_BETWEEN = 0x01, 0x02, 0x03, 0x04
Q_NOT, Q_MOD_10, Q_MOD_100, Q_LEAD_1000 = 0x08, 0x10, 0x20, 0x40
Q_AND, Q_OR, Q_NEWRULE = 0xFD, 0xFE, 0xFF
Q_OP_MASK = 0x07
Q_NEQ, Q_GT, Q_GEQ, Q_NOT_BETWEEN = Q_NOT | Q_EQ, Q_NOT | Q_LEQ, Q_NOT | Q_LT, Q_NOT | Q_BETWEEN

PLURAL_RULES: dict[str, tuple[bytes, list[str]]] = {
    # Chinese, Japanese, Korean, Thai, Vietnamese, Indonesian, Malay, Hungarian,
    # Turkish and Persian: one form for every count (the noun does not change after a numeral).
    "universal": (bytes(), ["Universal Form"]),
    "english": (bytes([Q_EQ, 1]), ["Singular", "Plural"]),
    # French and Brazilian Portuguese use the singular for 0 and 1.
    "french": (bytes([Q_LEQ, 1]), ["Singular", "Plural"]),
    "icelandic": (bytes([Q_MOD_10 | Q_EQ, 1, Q_AND, Q_MOD_100 | Q_NEQ, 11]), ["Singular", "Plural"]),
    "latvian": (bytes([Q_MOD_10 | Q_EQ, 1, Q_AND, Q_MOD_100 | Q_NEQ, 11, Q_NEWRULE, Q_NEQ, 0]), ["Singular", "Plural", "Nullar"]),
    "irish": (bytes([Q_EQ, 1, Q_NEWRULE, Q_EQ, 2]), ["Singular", "Dual", "Plural"]),
    "slovak": (bytes([Q_EQ, 1, Q_NEWRULE, Q_BETWEEN, 2, 4]), ["Singular", "Paucal", "Plural"]),
    "lithuanian": (bytes([Q_MOD_10 | Q_EQ, 1, Q_AND, Q_MOD_100 | Q_NEQ, 11, Q_NEWRULE,
                          Q_MOD_10 | Q_NEQ, 0, Q_AND, Q_MOD_100 | Q_NOT_BETWEEN, 10, 19]), ["Singular", "Paucal", "Plural"]),
    "russian": (bytes([Q_MOD_10 | Q_EQ, 1, Q_AND, Q_MOD_100 | Q_NEQ, 11, Q_NEWRULE,
                       Q_MOD_10 | Q_BETWEEN, 2, 4, Q_AND, Q_MOD_100 | Q_NOT_BETWEEN, 10, 19]), ["Singular", "Dual", "Plural"]),
    "polish": (bytes([Q_EQ, 1, Q_NEWRULE, Q_MOD_10 | Q_BETWEEN, 2, 4, Q_AND, Q_MOD_100 | Q_NOT_BETWEEN, 10, 19]), ["Singular", "Paucal", "Plural"]),
    "romanian": (bytes([Q_EQ, 1, Q_NEWRULE, Q_EQ, 0, Q_OR, Q_MOD_100 | Q_BETWEEN, 1, 19]), ["Singular", "Paucal", "Plural"]),
    "slovenian": (bytes([Q_MOD_100 | Q_EQ, 1, Q_NEWRULE, Q_MOD_100 | Q_EQ, 2, Q_NEWRULE, Q_MOD_100 | Q_BETWEEN, 3, 4]),
                  ["Singular", "Dual", "Trial", "Plural"]),
    "arabic": (bytes([Q_EQ, 0, Q_NEWRULE, Q_EQ, 1, Q_NEWRULE, Q_EQ, 2, Q_NEWRULE,
                      Q_MOD_100 | Q_BETWEEN, 3, 10, Q_NEWRULE, Q_MOD_100 | Q_GEQ, 11]),
               ["Nullar", "Singular", "Dual", "Minority Plural", "Plural", "Plural (100-102, ...)"]),
    "tagalog": (bytes([Q_LEQ, 1, Q_NEWRULE, Q_MOD_10 | Q_EQ, 4, Q_OR, Q_MOD_10 | Q_EQ, 6, Q_OR, Q_MOD_10 | Q_EQ, 9]),
                ["Singular", "Plural (consonant-ended)", "Plural (vowel-ended)"]),
}


def plural_form_count(rule: str) -> int:
    return len(PLURAL_RULES[rule][1])


def plural_index(rule: str, n: int) -> int:
    """The form Qt picks for n: a port of QTranslator's numerusHelper over the rule bytes."""
    rules = PLURAL_RULES[rule][0]
    if not rules:
        return 0
    result, i, size = 0, 0, len(rules)
    while True:
        or_value = False
        while True:
            and_value = True
            while True:
                opcode = rules[i]
                i += 1
                left = n
                if opcode & Q_MOD_10:
                    left %= 10
                elif opcode & Q_MOD_100:
                    left %= 100
                elif opcode & Q_LEAD_1000:
                    while left >= 1000:
                        left //= 1000
                op = opcode & Q_OP_MASK
                right = rules[i]
                i += 1
                if op == Q_EQ:
                    truth = left == right
                elif op == Q_LT:
                    truth = left < right
                elif op == Q_LEQ:
                    truth = left <= right
                elif op == Q_BETWEEN:
                    top = rules[i]
                    i += 1
                    truth = right <= left <= top
                else:
                    raise ValueError(f"bad numerus opcode {opcode:#x}")
                if opcode & Q_NOT:
                    truth = not truth
                and_value = and_value and truth
                if i == size or rules[i] != Q_AND:
                    break
                i += 1
            or_value = or_value or and_value
            if i == size or rules[i] != Q_OR:
                break
            i += 1
        if or_value:
            return result
        result += 1
        if i == size:
            return result
        i += 1  # Q_NEWRULE


def valid_numerus_rules(rules: bytes) -> bool:
    """QTranslator's isValidNumerusRules: what the .qm reader accepts."""
    if not rules:
        return True
    offset, size = 0, len(rules)
    while True:
        opcode = rules[offset]
        if opcode & 0x80:
            return False
        offset += 1
        if offset == size:
            return False
        offset += 1
        op = opcode & Q_OP_MASK
        if op == Q_BETWEEN:
            if offset == size:
                return False
            offset += 1
        elif op not in (Q_EQ, Q_LT, Q_LEQ):
            return False
        if offset == size:
            return True
        if rules[offset] not in (Q_AND, Q_OR, Q_NEWRULE):
            return False
        offset += 1
        if offset == size:
            return False


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
@dataclass
class Language:
    code: str            # Omarchy's registry code; the settings value, TOMACCO_LANGUAGE and the file suffix
    name: str            # the language's own name
    english_name: str
    format_locale: str   # BCP 47, for QLocale: numbers, dates
    system_locale: str   # the POSIX locale Omarchy generates
    qt_locale: str       # the .ts file's language attribute, what lrelease reads
    direction: str       # ltr or rtl
    decimal: str
    zero_digit: str
    plural: str          # a PLURAL_RULES key
    omarchy: bool        # listed in Omarchy's registry
    complete: bool       # every interface text translated; offered in the settings
    extra: dict = field(default_factory=dict)

    @property
    def ts_path(self) -> Path:
        return ROOT / "i18n" / f"tomacco_{self.code}.ts"

    @property
    def rtl(self) -> bool:
        return self.direction == "rtl"

    def to_json(self) -> dict:
        data = {"code": self.code, "name": self.name, "englishName": self.english_name, "formatLocale": self.format_locale,
                "systemLocale": self.system_locale, "qtLocale": self.qt_locale, "direction": self.direction, "decimal": self.decimal,
                "zeroDigit": self.zero_digit, "plural": self.plural, "omarchy": self.omarchy, "complete": self.complete}
        data.update(self.extra)
        return data


@dataclass
class Registry:
    languages: list[Language]
    aliases: dict[str, str]
    source: dict
    schema_version: int = 1

    def by_code(self) -> dict[str, Language]:
        return {language.code: language for language in self.languages}

    def get(self, code: str) -> Language:
        return self.by_code()[code]

    def translated(self) -> list[Language]:
        """Every language with a catalog: all but the source language."""
        return [language for language in self.languages if language.code != SOURCE_LANGUAGE]

    def offered(self) -> list[Language]:
        """The settings picker: English first, then by English name, as Omarchy's own language list."""
        complete = [language for language in self.languages if language.complete]
        return sorted(complete, key=lambda language: "" if language.code == SOURCE_LANGUAGE else language.english_name)

    def resolve(self, tag: str) -> str | None:
        """The interface language a language tag names, or None.

        Accepts BCP 47 (pt-BR, zh-Hant-TW), POSIX (pt_BR.UTF-8) and bare codes, in
        any case. The whole tag is tried first, then language and region, language
        and script, and the language alone, each directly and through the aliases.
        """
        codes = {language.code.lower(): language.code for language in self.languages}
        aliases = {key.lower(): value for key, value in self.aliases.items()}
        for candidate in tag_candidates(tag):
            lowered = candidate.lower()
            if lowered in codes:
                return codes[lowered]
            if lowered in aliases and aliases[lowered].lower() in codes:
                return codes[aliases[lowered].lower()]
        return None

    def to_json(self) -> dict:
        return {"schemaVersion": self.schema_version, "source": self.source,
                "languages": [language.to_json() for language in self.languages], "aliases": self.aliases}


def tag_candidates(tag: str) -> list[str]:
    cleaned = re.sub(r"[.@].*$", "", str(tag).strip()).replace("_", "-")
    parts = [part for part in cleaned.split("-") if part]
    if not parts or not re.fullmatch(r"[A-Za-z]{2,3}", parts[0]):
        return []
    language = parts[0].lower()
    scripts = [part.title() for part in parts[1:] if re.fullmatch(r"[A-Za-z]{4}", part)]
    regions = [part.upper() for part in parts[1:] if re.fullmatch(r"[A-Za-z]{2}|[0-9]{3}", part)]
    candidates = ["-".join([language] + [part for part in parts[1:]])]
    for region in regions:
        candidates.append(f"{language}-{region}")
    for script in scripts:
        candidates.append(f"{language}-{script}")
    candidates.append(language)
    seen, unique = set(), []
    for candidate in candidates:
        if candidate.lower() not in seen:
            seen.add(candidate.lower())
            unique.append(candidate)
    return unique


KNOWN_KEYS = {"code", "name", "englishName", "formatLocale", "systemLocale", "qtLocale", "direction", "decimal", "zeroDigit",
              "plural", "omarchy", "complete"}


def load_registry(path: Path = REGISTRY_PATH) -> Registry:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    languages = []
    for entry in data["languages"]:
        if entry["plural"] not in PLURAL_RULES:
            raise ValueError(f"{entry['code']}: unknown plural rule {entry['plural']!r}")
        languages.append(Language(code=entry["code"], name=entry["name"], english_name=entry["englishName"],
                                  format_locale=entry["formatLocale"], system_locale=entry["systemLocale"], qt_locale=entry["qtLocale"],
                                  direction=entry.get("direction", "ltr"), decimal=entry.get("decimal", "."), zero_digit=entry.get("zeroDigit", "0"),
                                  plural=entry["plural"], omarchy=bool(entry.get("omarchy", False)), complete=bool(entry.get("complete", False)),
                                  extra={key: value for key, value in entry.items() if key not in KNOWN_KEYS}))
    return Registry(languages=languages, aliases=dict(data.get("aliases", {})), source=dict(data.get("source", {})),
                    schema_version=int(data.get("schemaVersion", 1)))


def save_registry(registry: Registry, path: Path = REGISTRY_PATH) -> None:
    Path(path).write_text(json.dumps(registry.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# Where an Omarchy installation keeps its registry; OMARCHY_LOCALES_JSON overrides it as it does for omarchy-language.
def omarchy_registry_path(environ: dict) -> Path | None:
    for candidate in (environ.get("OMARCHY_LOCALES_JSON"),
                      (environ["OMARCHY_PATH"] + "/default/i18n/locales.json") if environ.get("OMARCHY_PATH") else None,
                      "/usr/share/omarchy/default/i18n/locales.json"):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def compare_with_omarchy(registry: Registry, omarchy_file: Path) -> dict:
    """How the project's registry differs from an Omarchy registry file: languages Omarchy
    added or removed, and fields that changed. Only Omarchy's fields are compared."""
    theirs = json.loads(Path(omarchy_file).read_text(encoding="utf-8"))
    ours = {language.code: language for language in registry.languages if language.omarchy}
    added = sorted(code for code in theirs if code not in ours)
    removed = sorted(code for code in ours if code not in theirs)
    changed = {}
    for code, entry in theirs.items():
        if code not in ours:
            continue
        mine = ours[code]
        expected = {"name": mine.name, "englishName": mine.english_name, "formatLocale": mine.format_locale, "locale": mine.system_locale,
                    "direction": mine.direction, "decimal": mine.decimal, "zeroDigit": mine.zero_digit}
        actual = {"name": entry.get("name"), "englishName": entry.get("englishName"), "formatLocale": entry.get("formatLocale"),
                  "locale": entry.get("locale"), "direction": entry.get("direction", "ltr"), "decimal": entry.get("decimal", "."),
                  "zeroDigit": entry.get("zeroDigit", "0")}
        differences = {key: {"ours": expected[key], "omarchy": actual[key]} for key in expected if expected[key] != actual[key]}
        if differences:
            changed[code] = differences
    return {"file": str(omarchy_file), "sha256": sha256_of(omarchy_file), "recorded_sha256": registry.source.get("omarchy", {}).get("sha256"),
            "count": len(theirs), "added": added, "removed": removed, "changed": changed,
            "in_step": not added and not removed and not changed}


# ---------------------------------------------------------------------------
# The texts the code marks for translation, as lupdate sees them
# ---------------------------------------------------------------------------
LITERAL = r'"(?:[^"\\]|\\.)*"'
CPP_CONTEXTS = {"backend.cpp": "Backend", "control.cpp": "Control", "nativeview.cpp": "NativeView"}
Key = tuple[str, str, str]  # (context, source, disambiguation comment)


def unquote(literal: str) -> str:
    return json.loads(literal.replace("\\'", "'"))


def marked_strings(root: Path = ROOT) -> dict[Key, bool]:
    """(context, source, disambiguation) -> numerus for every marked string in qml/ and src/."""
    found: dict[Key, bool] = {}

    def add(context, source, comment, numerus):
        found[(context, unquote(source), unquote(comment) if comment else "")] = bool(numerus)

    for path in sorted(glob.glob(str(root / "qml/*.qml")) + glob.glob(str(root / "qml/*.js"))):
        context = Path(path).stem
        text = Path(path).read_text(encoding="utf-8")
        for m in re.finditer(r"\bqsTr\(\s*(" + LITERAL + r")\s*(?:,\s*(" + LITERAL + r")\s*)?(,)?", text):
            add(context, m.group(1), m.group(2), m.group(3))
        for m in re.finditer(r"\bqsTranslate\(\s*(" + LITERAL + r")\s*,\s*(" + LITERAL + r")\s*(?:,\s*(" + LITERAL + r")\s*)?(,)?", text):
            add(unquote(m.group(1)), m.group(2), m.group(3), m.group(4))
        for m in re.finditer(r"\bQT_TRANSLATE_NOOP\(\s*(" + LITERAL + r")\s*,\s*(" + LITERAL + r")", text):
            add(unquote(m.group(1)), m.group(2), None, None)
    for path in sorted(glob.glob(str(root / "src/*.cpp"))):
        text = Path(path).read_text(encoding="utf-8")
        context = CPP_CONTEXTS.get(Path(path).name)
        for m in re.finditer(r"(?<![\w])(?<!::)tr\(\s*(" + LITERAL + r")\s*(?:,\s*(" + LITERAL + r")\s*)?(,)?", text):
            add(context, m.group(1), m.group(2), m.group(3))
        for m in re.finditer(r"\btranslate\(\s*(" + LITERAL + r")\s*,\s*(" + LITERAL + r")\s*(?:,\s*(" + LITERAL + r")\s*)?(,)?", text):
            add(unquote(m.group(1)), m.group(2), m.group(3), m.group(4))
    return found


# ---------------------------------------------------------------------------
# .ts files
# ---------------------------------------------------------------------------
@dataclass
class Message:
    context: str
    source: str
    comment: str = ""
    numerus: bool = False
    forms: list[str] = field(default_factory=list)  # one entry, or one per plural form
    state: str = ""                                 # "", unfinished, vanished or obsolete

    @property
    def key(self) -> Key:
        return (self.context, self.source, self.comment)

    @property
    def finished(self) -> bool:
        return self.state == "" and bool(self.forms) and all(self.forms)

    @property
    def live(self) -> bool:
        return self.state not in ("vanished", "obsolete")


@dataclass
class Catalog:
    language: str          # the TS language attribute (qtLocale)
    source_language: str
    messages: list[Message]

    def by_key(self) -> dict[Key, Message]:
        return {message.key: message for message in self.messages}


def read_ts(path: Path) -> Catalog:
    root = ET.parse(path).getroot()
    if root.tag != "TS":
        raise ValueError(f"{path}: not a Qt Linguist file")
    messages = []
    for context in root.iter("context"):
        name = context.findtext("name") or ""
        for element in context.iter("message"):
            source = element.findtext("source")
            translation = element.find("translation")
            if source is None or translation is None:
                continue
            numerus = element.get("numerus") == "yes"
            forms = [form.text or "" for form in translation.findall("numerusform")] if numerus else [translation.text or ""]
            messages.append(Message(context=name, source=source, comment=element.findtext("comment") or "", numerus=numerus,
                                    forms=forms, state=translation.get("type") or ""))
    return Catalog(language=root.get("language") or "", source_language=root.get("sourcelanguage") or SOURCE_LANGUAGE, messages=messages)


def escape(text: str) -> str:
    """lupdate's escaping of text content."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")


def same_catalog(a: Catalog, b: Catalog) -> bool:
    return a.language == b.language and [(m.key, m.numerus, m.forms, m.state) for m in a.messages] == [(m.key, m.numerus, m.forms, m.state) for m in b.messages]


def write_ts(catalog: Catalog, path: Path) -> None:
    """Writes the catalog the way lupdate -locations none does, so the files diff cleanly."""
    lines = ['<?xml version="1.0" encoding="utf-8"?>', "<!DOCTYPE TS>",
             f'<TS version="2.1" language="{catalog.language}" sourcelanguage="{catalog.source_language}">']
    contexts: dict[str, list[Message]] = {}
    for message in catalog.messages:
        contexts.setdefault(message.context, []).append(message)
    for name, messages in contexts.items():
        lines += ["<context>", f"    <name>{escape(name)}</name>"]
        for message in messages:
            lines.append('    <message numerus="yes">' if message.numerus else "    <message>")
            lines.append(f"        <source>{escape(message.source)}</source>")
            if message.comment:
                lines.append(f"        <comment>{escape(message.comment)}</comment>")
            attribute = f' type="{message.state}"' if message.state else ""
            if message.numerus:
                lines.append(f"        <translation{attribute}>")
                for form in message.forms:
                    lines.append(f"            <numerusform>{escape(form)}</numerusform>")
                lines.append("        </translation>")
            else:
                lines.append(f"        <translation{attribute}>{escape(message.forms[0] if message.forms else '')}</translation>")
            lines.append("    </message>")
        lines.append("</context>")
    lines.append("</TS>")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Checks on one translation
# ---------------------------------------------------------------------------
PLACEHOLDER = re.compile(r"%L?\d|%n")
TAG = re.compile(r"</?[A-Za-z][^<>]*>")


def placeholders(text: str) -> list[str]:
    return sorted({match.group(0).replace("%L", "%") for match in PLACEHOLDER.finditer(text)})


def translation_problems(message: Message, rule: str) -> list[str]:
    """What is wrong with a finished translation: placeholders, plural form count, mark-up, line breaks."""
    problems = []
    if message.numerus and len(message.forms) != plural_form_count(rule):
        problems.append(f"{len(message.forms)} plural forms, {rule} needs {plural_form_count(rule)}")
    wanted = placeholders(message.source)
    for form in message.forms:
        if placeholders(form) != wanted:
            problems.append(f"placeholders {placeholders(form)} differ from {wanted}")
        if sorted(TAG.findall(message.source)) != sorted(TAG.findall(form)):
            problems.append("mark-up tags differ from the source")
        if ("\n" in message.source) != ("\n" in form):
            problems.append("line breaks differ from the source")
        if form != form.strip() and message.source == message.source.strip():
            problems.append("leading or trailing white space the source has not")
    return problems


# Scripts a translation must show at least once for a text of several words.
SCRIPT_RANGES = {
    "ar": r"[؀-ۿ]", "fa": r"[؀-ۿ]", "ur": r"[؀-ۿ]", "he": r"[֐-׿]",
    "el": r"[Ͱ-Ͽ]", "ru": r"[Ѐ-ӿ]", "uk": r"[Ѐ-ӿ]", "bg": r"[Ѐ-ӿ]", "sr": r"[Ѐ-ӿ]",
    "hi": r"[ऀ-ॿ]", "bn": r"[ঀ-৿]", "ta": r"[஀-௿]", "si": r"[඀-෿]", "th": r"[฀-๿]",
    "ja": r"[぀-ヿ一-鿿]", "ko": r"[가-힯ᄀ-ᇿ]", "zh-CN": r"[一-鿿]", "zh-TW": r"[一-鿿]",
}


def looks_untranslated(code: str, source: str, form: str) -> bool:
    """A finished translation that is the English text again, or shows none of the language's script
    for a sentence of several words. Names, codes and units may be the same in every language."""
    words = re.findall(r"[A-Za-z]{2,}", source)
    if len(words) < 3:
        return False
    if form.strip() == source.strip():
        return True
    script = SCRIPT_RANGES.get(code)
    return bool(script) and not re.search(script, form)


# ---------------------------------------------------------------------------
# .qm compilation (what tools/ts2qm.py writes; the same format as lrelease)
# ---------------------------------------------------------------------------
MAGIC = bytes([0x3C, 0xB8, 0x64, 0x18, 0xCA, 0xEF, 0x9C, 0x95, 0xCD, 0x21, 0x1C, 0xBF, 0x60, 0xA1, 0xBD, 0xDD])
TAG_END, TAG_TRANSLATION, TAG_SOURCE, TAG_CONTEXT, TAG_COMMENT = 1, 3, 6, 7, 8
SECTION_HASHES, SECTION_MESSAGES, SECTION_NUMERUS, SECTION_LANGUAGE = 0x42, 0x69, 0x88, 0xA7


def elf_hash(data: bytes) -> int:
    h = 0
    for byte in data:
        h = ((h << 4) + byte) & 0xFFFFFFFF
        g = h & 0xF0000000
        if g:
            h ^= g >> 24
        h &= ~g & 0xFFFFFFFF
    return h or 1


def _block(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def _message_bytes(context: str, source: str, comment: str, translations: list[str]) -> bytes:
    out = bytearray()
    for text in translations:
        out += bytes([TAG_TRANSLATION]) + _block(text.encode("utf-16-be"))
    out += bytes([TAG_SOURCE]) + _block(source.encode("utf-8"))
    if comment:
        out += bytes([TAG_COMMENT]) + _block(comment.encode("utf-8"))
    out += bytes([TAG_CONTEXT]) + _block(context.encode("utf-8"))
    out += bytes([TAG_END])
    return bytes(out)


def plural_rule_for_ts_language(ts_language: str, registry: Registry | None = None) -> str:
    """The plural-rule family of a .ts file's language attribute (de, pt_BR, nb_NO, ...)."""
    registry = registry or load_registry()
    for language in registry.languages:
        if language.qt_locale.lower() == ts_language.lower().replace("-", "_"):
            return language.plural
    code = registry.resolve(ts_language)
    if code is None:
        raise KeyError(ts_language)
    return registry.get(code).plural


def compile_catalog(catalog: Catalog, rule: str) -> bytes:
    """The .qm bytes for a catalog: finished messages only, as lrelease writes by default."""
    data = bytearray()
    hashes = []
    seen = set()
    for message in catalog.messages:
        if message.state in ("unfinished", "obsolete", "vanished") or not any(message.forms):
            continue
        if message.key in seen:
            raise ValueError(f"duplicate message {message.key!r}")
        seen.add(message.key)
        hashes.append((elf_hash((message.source + message.comment).encode("utf-8")), len(data)))
        data += _message_bytes(message.context, message.source, message.comment, message.forms)
    hashes.sort()
    table = b"".join(struct.pack(">II", h, offset) for h, offset in hashes)
    rules = PLURAL_RULES[rule][0]
    out = bytearray(MAGIC)
    out += bytes([SECTION_LANGUAGE]) + _block(catalog.language.encode("utf-8"))
    out += bytes([SECTION_HASHES]) + _block(table)
    out += bytes([SECTION_MESSAGES]) + _block(bytes(data))
    out += bytes([SECTION_NUMERUS]) + _block(rules)
    return bytes(out)


def read_qm(payload: bytes) -> dict:
    """The sections of a .qm: language, numerus rules and (context, source, comment) -> forms."""
    if not payload.startswith(MAGIC):
        raise ValueError("not a .qm file")
    offset = len(MAGIC)
    sections = {}
    while offset < len(payload):
        tag = payload[offset]
        length = struct.unpack(">I", payload[offset + 1:offset + 5])[0]
        sections[tag] = payload[offset + 5:offset + 5 + length]
        offset += 5 + length
    messages = {}
    data = sections.get(SECTION_MESSAGES, b"")
    position = 0
    while position < len(data):
        forms, source, comment, context = [], "", "", ""
        while True:
            tag = data[position]
            position += 1
            if tag == TAG_END:
                break
            length = struct.unpack(">I", data[position:position + 4])[0]
            value = data[position + 4:position + 4 + length]
            position += 4 + length
            if tag == TAG_TRANSLATION:
                forms.append(value.decode("utf-16-be"))
            elif tag == TAG_SOURCE:
                source = value.decode("utf-8")
            elif tag == TAG_COMMENT:
                comment = value.decode("utf-8")
            elif tag == TAG_CONTEXT:
                context = value.decode("utf-8")
        messages[(context, source, comment)] = forms
    return {"language": sections.get(SECTION_LANGUAGE, b"").decode("utf-8"), "numerus_rules": sections.get(SECTION_NUMERUS, b""),
            "messages": messages, "hash_count": len(sections.get(SECTION_HASHES, b"")) // 8}
