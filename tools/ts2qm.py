#!/usr/bin/env python3
"""Compile a Qt Linguist .ts file into a .qm file that QTranslator loads.

The build uses Qt's own lrelease (qt_add_translations) whenever Qt's
LinguistTools are installed. This is the fallback for hosts without them
(on Arch they ship in qt6-tools), so the translated interface also builds there.
It writes the same binary format as lrelease for what the .ts files here use:
contexts, optional disambiguation comments, plural forms, and the numerus
rules of the target language. Unfinished, obsolete and vanished messages
are left out, as lrelease does by default.
"""

import struct
import sys
import xml.etree.ElementTree as ET

MAGIC = bytes([0x3C, 0xB8, 0x64, 0x18, 0xCA, 0xEF, 0x9C, 0x95,
               0xCD, 0x21, 0x1C, 0xBF, 0x60, 0xA1, 0xBD, 0xDD])
TAG_END, TAG_TRANSLATION, TAG_SOURCE, TAG_CONTEXT, TAG_COMMENT = 1, 3, 6, 7, 8
SECTION_HASHES, SECTION_MESSAGES, SECTION_NUMERUS, SECTION_LANGUAGE = 0x42, 0x69, 0x88, 0xA7
Q_EQ, Q_LT, Q_LEQ, Q_BETWEEN = 0x01, 0x02, 0x03, 0x04
Q_NOT, Q_MOD_10, Q_MOD_100 = 0x08, 0x10, 0x20
Q_AND, Q_OR, Q_NEWRULE = 0xFD, 0xFE, 0xFF

# Numerus rules from Qt's translator (qtranslator.cpp/numerus.cpp): the
# form index is the first rule that holds, or the count of rules.
NUMERUS_RULES = {
    "en": bytes([Q_EQ, 1]),
    "de": bytes([Q_EQ, 1]),
    # French uses the singular for 0 and 1.
    "fr": bytes([Q_LEQ, 1]),
    "it": bytes([Q_EQ, 1]),
    "es": bytes([Q_EQ, 1]),
}


def elf_hash(data: bytes) -> int:
    h = 0
    for byte in data:
        h = ((h << 4) + byte) & 0xFFFFFFFF
        g = h & 0xF0000000
        if g:
            h ^= g >> 24
        h &= ~g & 0xFFFFFFFF
    return h or 1


def block(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def message_bytes(context: str, source: str, comment: str, translations: list[str]) -> bytes:
    out = bytearray()
    for text in translations:
        out += bytes([TAG_TRANSLATION]) + block(text.encode("utf-16-be"))
    out += bytes([TAG_SOURCE]) + block(source.encode("utf-8"))
    if comment:
        out += bytes([TAG_COMMENT]) + block(comment.encode("utf-8"))
    out += bytes([TAG_CONTEXT]) + block(context.encode("utf-8"))
    out += bytes([TAG_END])
    return bytes(out)


def compile_ts(path: str) -> bytes:
    root = ET.parse(path).getroot()
    if root.tag != "TS":
        raise SystemExit(f"{path}: not a Qt Linguist file")
    language = (root.get("language") or "").split("_")[0]
    if language not in NUMERUS_RULES:
        raise SystemExit(f"{path}: no numerus rules for language {language!r}")
    messages = []
    seen = set()
    for context in root.iter("context"):
        name = context.findtext("name") or ""
        for message in context.iter("message"):
            source = message.findtext("source")
            translation = message.find("translation")
            if source is None or translation is None:
                continue
            if translation.get("type") in ("unfinished", "obsolete", "vanished"):
                continue
            comment = message.findtext("comment") or ""
            if message.get("numerus") == "yes":
                forms = [form.text or "" for form in translation.findall("numerusform")]
            else:
                forms = [translation.text or ""]
            if not any(forms):
                continue
            key = (name, source, comment)
            if key in seen:
                raise SystemExit(f"{path}: duplicate message {key!r}")
            seen.add(key)
            messages.append((name, source, comment, forms))
    data = bytearray()
    hashes = []
    for name, source, comment, forms in messages:
        hashes.append((elf_hash((source + comment).encode("utf-8")), len(data)))
        data += message_bytes(name, source, comment, forms)
    hashes.sort()
    table = b"".join(struct.pack(">II", h, offset) for h, offset in hashes)
    out = bytearray(MAGIC)
    out += bytes([SECTION_LANGUAGE]) + block((root.get("language") or language).encode("utf-8"))
    out += bytes([SECTION_HASHES]) + block(table)
    out += bytes([SECTION_MESSAGES]) + block(bytes(data))
    out += bytes([SECTION_NUMERUS]) + block(NUMERUS_RULES[language])
    return bytes(out)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: ts2qm.py input.ts output.qm")
    payload = compile_ts(sys.argv[1])
    with open(sys.argv[2], "wb") as handle:
        handle.write(payload)


if __name__ == "__main__":
    main()
