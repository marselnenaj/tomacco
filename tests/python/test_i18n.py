"""The desktop's languages: the registry, the plural rules, the catalogs and what the build and the interface do with them.

English is the source language of qml/, the C++ desktop code and the operation titles in
assets/operations.json. i18n/languages.json registers every interface language (Omarchy's 50
plus Spanish without a region) and i18n/tomacco_CODE.ts carries each language's texts;
i18n/tomacco_en.ts holds the English plural forms. A language marked complete in the registry
has every text translated and is offered in the settings; the registry and the catalogs must
agree about that in both directions, so a text added to the code without its translations fails
here for every complete language, and a language that becomes complete is marked so.
"""

import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import tomacco_i18n as i18n  # noqa: E402

LITERAL = i18n.LITERAL
REGISTRY = i18n.load_registry()
BY_CODE = REGISTRY.by_code()
# Every language with a catalog, and those whose catalog is complete.
LANGUAGES = [language.code for language in REGISTRY.translated()]
COMPLETE = [language.code for language in REGISTRY.translated() if language.complete]
# Omarchy's registry as checked on 2026-11-05 (default/i18n/locales.json,
# sha256 dbe622637c2ee6acb320c0ba6f02cc2eb6338642ae71aa2a8e10e93a5f3beace): 50 languages.
OMARCHY_LANGUAGES = sorted("""en da ar fi fr el hu hi is ja ko es-MX fil pt sv tr vi ur bn ca si ta th uz it zh-CN pl lt ga nl no ru id de sw fa ms uk
                            ro cs sr he bg sk hr sl zh-TW et lv pt-BR""".split())


def unquote(literal):
    return i18n.unquote(literal)


def ts_messages(path: Path) -> dict:
    return i18n.read_ts(path).by_key()


def catalog_tool(*arguments):
    return subprocess.run([sys.executable, str(ROOT / "tools/i18n_catalog.py"), *arguments], capture_output=True, text=True, cwd=ROOT)


class RegistryTests(unittest.TestCase):
    def test_registry_holds_every_omarchy_language_and_spanish(self):
        codes = [language.code for language in REGISTRY.languages]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(sorted(code for code in codes if BY_CODE[code].omarchy), OMARCHY_LANGUAGES)
        self.assertEqual(len(OMARCHY_LANGUAGES), 50)
        self.assertIn("es", codes)
        self.assertFalse(BY_CODE["es"].omarchy)
        self.assertEqual(codes[0], "en")
        for language in REGISTRY.languages:
            self.assertTrue(language.name and language.english_name, language.code)
            self.assertRegex(language.format_locale, r"^[a-z]{2,3}-[A-Z]{2}$", language.code)
            self.assertRegex(language.system_locale, r"^[a-z]{2,3}_[A-Z]{2}\.UTF-8$", language.code)
            self.assertRegex(language.qt_locale, r"^[a-z]{2,3}(_[A-Z]{2})?$", language.code)
            self.assertIn(language.direction, ("ltr", "rtl"), language.code)
            self.assertIn(language.plural, i18n.PLURAL_RULES, language.code)
            self.assertEqual(len(language.decimal), 1, language.code)
            self.assertEqual(len(language.zero_digit), 1, language.code)
        self.assertEqual(sorted(code for code in codes if BY_CODE[code].rtl), ["ar", "fa", "he", "ur"])
        # Regional variants keep their own catalogs; Norwegian is Bokmål for Qt.
        self.assertEqual(BY_CODE["pt-BR"].qt_locale, "pt_BR")
        self.assertEqual(BY_CODE["zh-TW"].qt_locale, "zh_TW")
        self.assertEqual(BY_CODE["es-MX"].qt_locale, "es_MX")
        self.assertEqual(BY_CODE["no"].qt_locale, "nb_NO")
        self.assertEqual(BY_CODE["no"].format_locale, "nb-NO")
        for alias, code in REGISTRY.aliases.items():
            self.assertIn(code, BY_CODE, alias)
        # Provenance of Omarchy's data.
        self.assertEqual(len(REGISTRY.source["omarchy"]["sha256"]), 64)
        self.assertEqual(REGISTRY.source["omarchy"]["languages"], 50)

    def test_registry_matches_the_installed_omarchy_registry(self):
        installed = i18n.omarchy_registry_path(os.environ)
        if installed is None:
            self.skipTest("no Omarchy registry on this machine")
        result = i18n.compare_with_omarchy(REGISTRY, installed)
        self.assertTrue(result["in_step"], f"run tools/i18n_catalog.py import-registry: {result}")

    def test_language_tags_match_the_right_catalog(self):
        expected = {
            "de": "de", "de-DE": "de", "de_AT.UTF-8": "de", "DE": "de", "en-GB": "en", "en_US": "en", "fr-CA": "fr",
            "pt-BR": "pt-BR", "pt_BR": "pt-BR", "pt_BR.utf8": "pt-BR", "pt-PT": "pt", "pt": "pt", "pt-AO": "pt",
            "es": "es", "es-ES": "es", "es-AR": "es", "es-MX": "es-MX", "es_MX": "es-MX", "es-419": "es-MX", "es-US": "es-MX",
            "zh": "zh-CN", "zh-CN": "zh-CN", "zh-Hans": "zh-CN", "zh-Hans-CN": "zh-CN", "zh-SG": "zh-CN",
            "zh-TW": "zh-TW", "zh-Hant": "zh-TW", "zh-Hant-TW": "zh-TW", "zh-HK": "zh-TW", "zh_TW": "zh-TW",
            "no": "no", "nb": "no", "nb-NO": "no", "nn": "no", "nn_NO": "no", "fil": "fil", "tl": "fil", "fil-PH": "fil",
            "sr-Latn-RS": "sr", "sr-Cyrl": "sr", "iw": "he", "he-IL": "he", "in": "id", "ar-EG": "ar", "fa-IR": "fa", "ur-IN": "ur",
            "C": None, "POSIX": None, "": None, "klingon": None, "xx-YY": None, "tlh": None, "../de": None,
        }
        for tag, code in expected.items():
            self.assertEqual(REGISTRY.resolve(tag), code, tag)

    def test_picker_order_is_english_first_then_by_english_name(self):
        offered = REGISTRY.offered()
        self.assertEqual(offered[0].code, "en")
        names = [language.english_name for language in offered[1:]]
        self.assertEqual(names, sorted(names))
        self.assertEqual(sorted(language.code for language in offered), sorted(["en"] + COMPLETE))


class PluralRuleTests(unittest.TestCase):
    # The form index Qt picks for a count, per rule family (numerus.cpp order of forms).
    EXPECTED = {
        "universal": {0: 0, 1: 0, 2: 0, 5: 0, 11: 0, 100: 0},
        "english": {0: 1, 1: 0, 2: 1, 11: 1, 21: 1, 101: 1},
        "french": {0: 0, 1: 0, 2: 1, 11: 1, 21: 1, 1000000: 1},
        "icelandic": {0: 1, 1: 0, 2: 1, 11: 1, 21: 0, 101: 0, 111: 1},
        "latvian": {0: 2, 1: 0, 2: 1, 10: 1, 11: 1, 21: 0, 111: 1},
        "irish": {0: 2, 1: 0, 2: 1, 3: 2, 7: 2, 11: 2},
        "slovak": {0: 2, 1: 0, 2: 1, 3: 1, 4: 1, 5: 2, 12: 2, 22: 2},
        "lithuanian": {0: 2, 1: 0, 2: 1, 9: 1, 10: 2, 11: 2, 19: 2, 20: 2, 21: 0, 22: 1, 111: 2, 121: 0},
        "russian": {0: 2, 1: 0, 2: 1, 4: 1, 5: 2, 11: 2, 12: 2, 21: 0, 22: 1, 25: 2, 101: 0, 111: 2, 112: 2},
        "polish": {0: 2, 1: 0, 2: 1, 4: 1, 5: 2, 12: 2, 21: 2, 22: 1, 25: 2, 101: 2, 112: 2, 122: 1},
        "romanian": {0: 1, 1: 0, 2: 1, 19: 1, 20: 2, 21: 2, 101: 1, 119: 1, 120: 2},
        "slovenian": {0: 3, 1: 0, 2: 1, 3: 2, 4: 2, 5: 3, 101: 0, 102: 1, 103: 2, 105: 3},
        "arabic": {0: 0, 1: 1, 2: 2, 3: 3, 10: 3, 11: 4, 26: 4, 99: 4, 100: 5, 101: 5, 102: 5, 103: 3, 111: 4, 200: 5},
        "tagalog": {0: 0, 1: 0, 2: 2, 4: 1, 6: 1, 9: 1, 10: 2, 14: 1, 15: 2},
    }

    def test_rules_are_valid_for_qt_and_pick_the_expected_form(self):
        self.assertEqual(set(self.EXPECTED), set(i18n.PLURAL_RULES))
        for rule, (program, forms) in i18n.PLURAL_RULES.items():
            self.assertTrue(i18n.valid_numerus_rules(program), rule)
            for n, index in self.EXPECTED[rule].items():
                self.assertEqual(i18n.plural_index(rule, n), index, f"{rule} n={n}")
            # Every form is reachable for some count, and no count reaches beyond the forms.
            reached = {i18n.plural_index(rule, n) for n in range(0, 1000)}
            self.assertEqual(reached, set(range(len(forms))), rule)

    def test_every_language_uses_lrelease_rules(self):
        families = {"ar": "arabic", "cs": "slovak", "sk": "slovak", "pl": "polish", "ru": "russian", "uk": "russian", "hr": "russian", "sr": "russian",
                    "lt": "lithuanian", "lv": "latvian", "ro": "romanian", "sl": "slovenian", "ga": "irish", "is": "icelandic", "fil": "tagalog",
                    "fr": "french", "pt-BR": "french", "ja": "universal", "ko": "universal", "zh-CN": "universal", "zh-TW": "universal",
                    "th": "universal", "vi": "universal", "id": "universal", "ms": "universal", "hu": "universal", "tr": "universal", "fa": "universal"}
        for code, family in families.items():
            self.assertEqual(BY_CODE[code].plural, family, code)
        for code in LANGUAGES:
            if code not in families:
                self.assertEqual(BY_CODE[code].plural, "english", code)

    def test_english_plural_forms_exist_for_every_plural(self):
        english = ts_messages(ROOT / "i18n/tomacco_en.ts")
        plurals = sorted(key for key, numerus in i18n.marked_strings().items() if numerus)
        self.assertTrue(plurals)
        for key in plurals:
            forms = english[key].forms
            self.assertEqual(len(forms), 2, key)
            self.assertTrue(all("%n" in form for form in forms), key)


class CatalogTests(unittest.TestCase):
    def test_every_catalog_lists_every_marked_string_with_the_right_plural_forms(self):
        marked = i18n.marked_strings()
        self.assertGreater(len(marked), 500)
        for code in LANGUAGES:
            language = BY_CODE[code]
            self.assertTrue(language.ts_path.is_file(), f"{code}: run tools/i18n_catalog.py sync")
            catalog = i18n.read_ts(language.ts_path)
            self.assertEqual(catalog.language, language.qt_locale, code)
            translated = catalog.by_key()
            missing = sorted(key for key in marked if key not in translated)
            self.assertEqual(missing, [], f"{code}: run tools/i18n_catalog.py sync")
            forms = i18n.plural_form_count(language.plural)
            for key, numerus in marked.items():
                message = translated[key]
                self.assertEqual(message.numerus, numerus, (code,) + key)
                self.assertEqual(len(message.forms), forms if numerus else 1, (code,) + key)
                self.assertTrue(message.live, (code,) + key)
                if message.finished:
                    self.assertEqual(i18n.translation_problems(message, language.plural), [], (code,) + key)
            # Nothing stale lingers as a live translation.
            stale = sorted(key for key, message in translated.items() if key not in marked and message.live and any(message.forms))
            self.assertEqual(stale, [], code)

    def test_complete_languages_translate_everything_and_the_registry_says_which(self):
        marked = i18n.marked_strings()
        self.assertIn("de", COMPLETE)
        actually_complete = []
        for code in LANGUAGES:
            language = BY_CODE[code]
            translated = ts_messages(language.ts_path)
            unfinished = sorted(key for key in marked if not translated[key].finished)
            suspicious = [key for key in marked if translated[key].finished
                          and any(i18n.looks_untranslated(code, key[1], form) for form in translated[key].forms)]
            if language.complete:
                self.assertEqual(unfinished[:20], [], f"{code} is marked complete but has {len(unfinished)} unfinished texts")
                # Names, codes and units may stay English; a complete catalog is not an English copy.
                self.assertLessEqual(len(suspicious), len(marked) // 25, f"{code}: {suspicious[:10]}")
            if not unfinished:
                actually_complete.append(code)
        # The registry tells the truth in both directions; the menu entry belongs to a complete language.
        for code in actually_complete:
            if not BY_CODE[code].complete:
                have, wanted = importlib.import_module("i18n_catalog").desktop_entry_keys(code)
                self.assertNotEqual(have, wanted, f"{code} is fully translated: mark it complete in i18n/languages.json")
        self.assertEqual(sorted(code for code in COMPLETE if code in actually_complete), sorted(COMPLETE))

    def test_coverage_report_is_current(self):
        tool = importlib.import_module("i18n_catalog")
        data = tool.coverage(REGISTRY)
        self.assertEqual(sorted(data["complete"]), sorted(COMPLETE), "the registry's complete flags and the catalogs disagree")
        self.assertEqual((ROOT / "i18n/COVERAGE.md").read_text(encoding="utf-8"), tool.coverage_markdown(data),
                         "run: python3 tools/i18n_catalog.py report --write i18n/COVERAGE.md")

    def test_build_offers_and_bundles_every_registry_language(self):
        cmake = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
        self.assertIn("i18n/languages.json", cmake)
        self.assertIsNone(re.search(r"set\(TOMACCO_TS_FILE i18n/", cmake), "the .ts list comes from the registry, not a literal")
        self.assertIn('string(JSON code GET "${TOMACCO_LANGUAGE_REGISTRY}" languages ${index} code)', cmake)
        backend = (ROOT / "src/backend.cpp").read_text(encoding="utf-8")
        self.assertIn(':/i18n/languages.json', backend)
        self.assertIsNone(re.search(r"interfaceLanguages\{", backend), "the backend reads the registry, not a literal list")
        picker = (ROOT / "qml/NativeWorkspace.qml").read_text(encoding="utf-8")
        self.assertIn('readonly property var codes:["system"].concat(Studio.languages.map(l=>l.code))', picker)
        self.assertIn('model:[qsTr("System")].concat(Studio.languages.map(l=>l.name))', picker)
        self.assertNotIn("Deutsch", picker)
        # The menu entry is translated wherever the catalog is complete.
        entry = (ROOT / "assets/io.github.marselnenaj.tomacco.desktop").read_text(encoding="utf-8")
        for section in entry.split("\n\n"):
            for key in re.findall(r"^(\w+)\[de\]=", section, re.M):
                for code in COMPLETE:
                    locale = BY_CODE[code].qt_locale  # Desktop Entry keys spell regions pt_BR, not pt-BR
                    self.assertRegex(section, rf"(?m)^{re.escape(key)}\[{re.escape(locale)}\]=\S", f"{key}[{locale}]")

    def test_operation_titles_are_english_and_translated_by_name(self):
        operations = json.loads((ROOT / "assets/operations.json").read_text(encoding="utf-8"))
        table = dict(re.findall(r'^\s*"([^"]+)": QT_TRANSLATE_NOOP\("Operations", (' + LITERAL + r")\),?$",
                                (ROOT / "qml/OperationTitles.js").read_text(encoding="utf-8"), re.M))
        for op in operations:
            self.assertTrue(op.get("title", "").strip(), f"{op['name']} has no title")
            self.assertTrue(op.get("description", "").strip(), f"{op['name']} has no description")
        titled = {op["name"]: op["title"] for op in operations}
        self.assertEqual({name: unquote(source) for name, source in table.items()}, titled)
        for title in titled.values():
            self.assertIsNone(re.search(r"[äöüÄÖÜß]", title), title)
        for code in LANGUAGES:
            translated = ts_messages(BY_CODE[code].ts_path)
            for title in titled.values():
                self.assertIn(("Operations", title, ""), translated, code)
                if code in COMPLETE:
                    self.assertTrue(translated[("Operations", title, "")].finished, (code, title))

    def test_color_names_are_english_and_translated(self):
        listed = {unquote(source) for source in re.findall(r'QT_TRANSLATE_NOOP\("Colors", (' + LITERAL + r")\)",
                                                         (ROOT / "qml/ColorNames.js").read_text(encoding="utf-8"))}
        catalog = {name for model in json.loads((ROOT / "assets/models.json").read_text(encoding="utf-8")) for name in model["colorNames"]}
        self.assertIn('"colorNames":["Device"]', (ROOT / "native/devices.rs").read_text(encoding="utf-8"))
        self.assertEqual(listed, catalog | {"Device"})
        translations = {code: ts_messages(BY_CODE[code].ts_path) for code in LANGUAGES}
        for name in listed:
            self.assertIsNone(re.search(r"[äöüÄÖÜß]", name), name)
            for code, translated in translations.items():
                self.assertIn(("Colors", name, ""), translated, code)
        for path in sorted(glob.glob(str(ROOT / "qml/*.qml"))):
            for match in re.finditer(r"(?:ColorNames[.]name\()?[\w.]*colorNames\[", Path(path).read_text(encoding="utf-8")):
                self.assertTrue(match.group(0).startswith("ColorNames.name("), f"{path}: {match.group(0)}")

    def test_no_german_literal_outside_the_translation_layer(self):
        for path in sorted(glob.glob(str(ROOT / "qml/*.qml")) + glob.glob(str(ROOT / "qml/*.js")) + glob.glob(str(ROOT / "src/*.cpp"))):
            for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
                code = re.sub(r"(qsTr|qsTranslate|QT_TRANSLATE_NOOP|tr|translate)\((\s*" + LITERAL + r"\s*,?)+", "", line)
                if code.lstrip().startswith("//"):
                    continue
                for literal in re.findall(LITERAL, code):
                    self.assertIsNone(re.search(r"[äöüÄÖÜß]", literal), f"{path}:{number}: {literal}")

    def test_catalog_tool_round_trips_a_translation(self):
        # sources numbers the texts; apply checks placeholders, plural forms and the sources digest.
        listing = catalog_tool("sources", "--count", "3").stdout.splitlines()
        self.assertRegex(listing[0], r"^# sources [0-9a-f]{12} \d+ texts$")
        self.assertEqual(len(listing), 4)
        language = BY_CODE["de"]
        catalog = i18n.read_ts(language.ts_path)
        plural = next(message for message in catalog.messages if message.numerus)
        plain = next(message for message in catalog.messages if not message.numerus and "%1" in message.source)
        canonical = importlib.import_module("i18n_catalog").canonical_messages()
        index_of = {message.key: index for index, message in enumerate(canonical)}
        with tempfile.TemporaryDirectory() as directory:
            chunk = Path(directory) / "chunk.txt"
            bad = [f"{listing[0]}", f"{index_of[plural.key]}\t{json.dumps(['x'])}", f"{index_of[plain.key]}\t\"ohne Platzhalter\""]
            chunk.write_text("\n".join(bad) + "\n", encoding="utf-8")
            original = language.ts_path.read_text(encoding="utf-8")
            try:
                result = catalog_tool("apply", "de", str(chunk))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("2 rejected", result.stdout)
                self.assertIn("plural forms", result.stdout)
                self.assertIn("placeholders", result.stdout)
                self.assertTrue(i18n.same_catalog(i18n.read_ts(language.ts_path), catalog))
                # A stale digest is refused before anything is written.
                chunk.write_text("# sources 000000000000 1 texts\n0\t\"x\"\n", encoding="utf-8")
                result = catalog_tool("apply", "de", str(chunk))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("run `sources` again", result.stderr)
            finally:
                language.ts_path.write_text(original, encoding="utf-8")


class CompilerTests(unittest.TestCase):
    def ts2qm(self):
        spec = importlib.util.spec_from_file_location("ts2qm", ROOT / "tools/ts2qm.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_fallback_compiler_writes_every_catalog_with_its_plural_rules(self):
        ts2qm = self.ts2qm()
        self.assertEqual(i18n.elf_hash(b""), 1)
        # elfHash("Open"), worked by hand: ((79*16+112)*16+101)*16+110.
        self.assertEqual(i18n.elf_hash(b"Open"), 0x566BE)
        for code in ["en"] + LANGUAGES:
            language = BY_CODE[code]
            payload = ts2qm.compile_ts(str(language.ts_path))
            self.assertTrue(payload.startswith(i18n.MAGIC))
            compiled = i18n.read_qm(payload)
            self.assertEqual(compiled["language"], language.qt_locale, code)
            self.assertEqual(compiled["numerus_rules"], i18n.PLURAL_RULES[language.plural][0], code)
            catalog = i18n.read_ts(language.ts_path)
            finished = [message for message in catalog.messages if message.finished]
            self.assertEqual(len(compiled["messages"]), len(finished), code)
            self.assertEqual(compiled["hash_count"], len(finished), code)
            for message in finished[:50]:
                self.assertEqual(compiled["messages"][message.key], message.forms, (code,) + message.key)

    def test_fallback_compiler_refuses_unknown_languages_and_duplicates(self):
        ts2qm = self.ts2qm()
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "bad.ts"
            bad.write_text('<TS version="2.1" language="tlh"></TS>')
            with self.assertRaises(SystemExit):
                ts2qm.compile_ts(str(bad))
            twice = Path(directory) / "twice.ts"
            twice.write_text('<TS version="2.1" language="de"><context><name>A</name><message><source>x</source><translation>y</translation></message>'
                             '<message><source>x</source><translation>z</translation></message></context></TS>')
            with self.assertRaises(SystemExit):
                ts2qm.compile_ts(str(twice))
            # Regional codes compile with their family's rules.
            brazil = Path(directory) / "br.ts"
            brazil.write_text('<TS version="2.1" language="pt_BR"><context><name>A</name><message numerus="yes"><source>%n x</source>'
                              '<translation><numerusform>%n a</numerusform><numerusform>%n b</numerusform></translation></message></context></TS>')
            compiled = i18n.read_qm(ts2qm.compile_ts(str(brazil)))
            self.assertEqual(compiled["numerus_rules"], i18n.PLURAL_RULES["french"][0])
            self.assertEqual(compiled["messages"][("A", "%n x", "")], ["%n a", "%n b"])


class NumbersTests(unittest.TestCase):
    """qml/Numbers.js parses what people type in their language and keeps the machine side canonical."""

    def run_js(self, expression):
        # A node binary that runs without a version manager's shim (mise needs a writable cache, sandboxes have none).
        candidates = sorted(glob.glob(os.path.expanduser("~/.local/share/mise/installs/node/*/bin/node")), reverse=True)
        candidates += [path for path in (shutil.which("node"),) if path]
        script = (ROOT / "qml/Numbers.js").read_text(encoding="utf-8").replace(".pragma library", "")
        for node in candidates:
            result = subprocess.run([node, "-e", script + f"\nconsole.log(JSON.stringify({expression}))"], capture_output=True, text=True)
            if result.returncode == 0:
                return json.loads(result.stdout.strip())
            if "mise" not in result.stderr:
                self.fail(result.stderr)
        self.skipTest("no usable node binary")

    def test_parse_accepts_decimal_marks_and_digits_of_the_languages(self):
        cases = {"1.5": 1.5, "1,5": 1.5, " 0,25 ": 0.25, "-2": -2, "1.234,5": 1234.5, "1,234.5": 1234.5, "1 234,5": 1234.5,
                 "٣٫٥": 3.5, "۱۲": 12, "১০": 10, "३.५": 3.5, "abc": None, "": None, "1.2.3": None, "1,2,3": None, ".5": 0.5}
        for text, value in cases.items():
            got = self.run_js(f"parse({json.dumps(text)})")
            self.assertEqual(got, value, text)
        self.assertEqual(self.run_js('show(0.5, ",")'), "0,5")
        self.assertEqual(self.run_js('show(0.5, ".")'), "0.5")
        self.assertEqual(self.run_js('show("", ",")'), "")


if __name__ == "__main__":
    unittest.main()
