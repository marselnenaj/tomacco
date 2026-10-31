"""The desktop interface's translations stay complete and in step with the code.

English is the source language of qml/, the C++ desktop code and the
operation titles in assets/operations.json; i18n/tomacco_de.ts carries
the German interface and i18n/tomacco_en.ts the English plural forms.
"""

import glob
import importlib.util
import json
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LITERAL = r'"(?:[^"\\]|\\.)*"'
CPP_CONTEXTS = {"backend.cpp": "Backend", "control.cpp": "Control", "nativeview.cpp": "NativeView"}


def unquote(literal: str) -> str:
    return json.loads(literal.replace("\\'", "'"))


def marked_strings() -> dict:
    """(context, source, disambiguation) -> numerus for every marked string, as lupdate sees them."""
    found = {}

    def add(context, source, comment, numerus):
        found[(context, unquote(source), unquote(comment) if comment else "")] = bool(numerus)

    for path in sorted(glob.glob(str(ROOT / "qml/*.qml")) + glob.glob(str(ROOT / "qml/*.js"))):
        context = Path(path).stem
        text = Path(path).read_text(encoding="utf-8")
        for m in re.finditer(r"\bqsTr\(\s*(" + LITERAL + r")\s*(?:,\s*(" + LITERAL + r")\s*)?(,)?", text):
            add(context, m.group(1), m.group(2), m.group(3))
        for m in re.finditer(r"\bqsTranslate\(\s*(" + LITERAL + r")\s*,\s*(" + LITERAL + r")\s*(?:,\s*(" + LITERAL + r")\s*)?(,)?", text):
            add(unquote(m.group(1)), m.group(2), m.group(3), m.group(4))
        for m in re.finditer(r"\bQT_TRANSLATE_NOOP\(\s*(" + LITERAL + r")\s*,\s*(" + LITERAL + r")", text):
            add(unquote(m.group(1)), m.group(2), None, None)
    for path in sorted(glob.glob(str(ROOT / "src/*.cpp"))):
        text = Path(path).read_text(encoding="utf-8")
        context = CPP_CONTEXTS.get(Path(path).name)
        for m in re.finditer(r"(?<![\w])(?<!::)tr\(\s*(" + LITERAL + r")\s*(?:,\s*(" + LITERAL + r")\s*)?(,)?", text):
            add(context, m.group(1), m.group(2), m.group(3))
        for m in re.finditer(r"\btranslate\(\s*(" + LITERAL + r")\s*,\s*(" + LITERAL + r")\s*(?:,\s*(" + LITERAL + r")\s*)?(,)?", text):
            add(unquote(m.group(1)), m.group(2), m.group(3), m.group(4))
    return found


def ts_messages(path: Path) -> dict:
    messages = {}
    for context in ET.parse(path).getroot().iter("context"):
        name = context.findtext("name")
        for message in context.iter("message"):
            key = (name, message.findtext("source"), message.findtext("comment") or "")
            messages[key] = message
    return messages


class TranslationTests(unittest.TestCase):
    def test_every_marked_string_has_a_finished_german_translation(self):
        marked = marked_strings()
        self.assertGreater(len(marked), 500)
        german = ts_messages(ROOT / "i18n/tomacco_de.ts")
        missing = sorted(key for key in marked if key not in german)
        self.assertEqual(missing, [], "run lupdate (cmake --build <dir> --target update_translations) and translate")
        for key, numerus in marked.items():
            message = german[key]
            translation = message.find("translation")
            self.assertNotIn(translation.get("type"), ("unfinished", "obsolete", "vanished"), key)
            self.assertEqual(message.get("numerus") == "yes", numerus, key)
            forms = [f.text or "" for f in translation.findall("numerusform")] if numerus else [translation.text or ""]
            self.assertTrue(all(forms), key)
            # Placeholders survive translation.
            for form in forms:
                self.assertEqual(sorted(set(re.findall(r"%\d", key[1]))), sorted(set(re.findall(r"%\d", form))), key)
                self.assertEqual("%n" in key[1], "%n" in form, key)
        # Nothing stale lingers as a live translation.
        self.assertEqual(sorted(key for key in german if key not in marked
                                and german[key].find("translation").get("type") not in ("obsolete", "vanished")), [])

    def test_english_plural_forms_exist_for_every_plural(self):
        english = ts_messages(ROOT / "i18n/tomacco_en.ts")
        plurals = sorted(key for key, numerus in marked_strings().items() if numerus)
        self.assertTrue(plurals)
        for key in plurals:
            forms = [f.text or "" for f in english[key].find("translation").findall("numerusform")]
            self.assertEqual(len(forms), 2, key)
            self.assertTrue(all("%n" in form for form in forms), key)

    def test_operation_titles_are_english_and_translated_by_name(self):
        operations = json.loads((ROOT / "assets/operations.json").read_text(encoding="utf-8"))
        table = dict(re.findall(r'^\s*"([^"]+)": QT_TRANSLATE_NOOP\("Operations", (' + LITERAL + r")\),?$",
                                (ROOT / "qml/OperationTitles.js").read_text(encoding="utf-8"), re.M))
        # Every operation has a title and a description; the desktop lists them all.
        for op in operations:
            self.assertTrue(op.get("title", "").strip(), f"{op['name']} has no title")
            self.assertTrue(op.get("description", "").strip(), f"{op['name']} has no description")
        titled = {op["name"]: op["title"] for op in operations}
        self.assertEqual({name: unquote(source) for name, source in table.items()}, titled)
        for title in titled.values():
            self.assertIsNone(re.search(r"[äöüÄÖÜß]", title), title)
        german = ts_messages(ROOT / "i18n/tomacco_de.ts")
        for title in titled.values():
            self.assertIn(("Operations", title, ""), german)

    def test_color_names_are_english_and_translated(self):
        listed = {unquote(source) for source in re.findall(r'QT_TRANSLATE_NOOP\("Colors", (' + LITERAL + r")\)",
                                                         (ROOT / "qml/ColorNames.js").read_text(encoding="utf-8"))}
        catalog = {name for model in json.loads((ROOT / "assets/models.json").read_text(encoding="utf-8")) for name in model["colorNames"]}
        # A profile measured from a connected device has one color, "Device".
        self.assertIn('"colorNames":["Device"]', (ROOT / "native/devices.rs").read_text(encoding="utf-8"))
        self.assertEqual(listed, catalog | {"Device"})
        german = ts_messages(ROOT / "i18n/tomacco_de.ts")
        for name in listed:
            self.assertIsNone(re.search(r"[äöüÄÖÜß]", name), name)
            self.assertIn(("Colors", name, ""), german)
        # Every place the interface shows a color name goes through the table.
        for path in sorted(glob.glob(str(ROOT / "qml/*.qml"))):
            for match in re.finditer(r"(?:ColorNames[.]name\()?[\w.]*colorNames\[", Path(path).read_text(encoding="utf-8")):
                self.assertTrue(match.group(0).startswith("ColorNames.name("), f"{path}: {match.group(0)}")

    def test_no_german_literal_outside_the_translation_layer(self):
        # A German text in the interface code is a missed translation.
        for path in sorted(glob.glob(str(ROOT / "qml/*.qml")) + glob.glob(str(ROOT / "qml/*.js")) + glob.glob(str(ROOT / "src/*.cpp"))):
            for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
                code = re.sub(r"(qsTr|qsTranslate|QT_TRANSLATE_NOOP|tr|translate)\((\s*" + LITERAL + r"\s*,?)+", "", line)
                if code.lstrip().startswith("//"):
                    continue
                for literal in re.findall(LITERAL, code):
                    self.assertIsNone(re.search(r"[äöüÄÖÜß]", literal), f"{path}:{number}: {literal}")

    def test_fallback_compiler_writes_loadable_files(self):
        spec = importlib.util.spec_from_file_location("ts2qm", ROOT / "tools/ts2qm.py")
        ts2qm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ts2qm)
        for name in ("tomacco_de.ts", "tomacco_en.ts"):
            payload = ts2qm.compile_ts(str(ROOT / "i18n" / name))
            self.assertTrue(payload.startswith(ts2qm.MAGIC))
        self.assertEqual(ts2qm.elf_hash(b""), 1)
        # elfHash("Open"), worked by hand: ((79*16+112)*16+101)*16+110.
        self.assertEqual(ts2qm.elf_hash(b"Open"), 0x566BE)
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "bad.ts"
            bad.write_text('<TS version="2.1" language="fr"></TS>')
            with self.assertRaises(SystemExit):
                ts2qm.compile_ts(str(bad))


if __name__ == "__main__":
    unittest.main()
