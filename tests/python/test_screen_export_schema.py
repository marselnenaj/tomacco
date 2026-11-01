"""screen.json of a screen.export bundle follows assets/schemas/screen-export-v1.schema.json.

The recorded bundles in tests/fixtures/screen_export are written by the unit tests of
native/screen_export.rs (an Android hierarchy, an OCR screen, a batch index) or taken from a
live emulator; each is checked against the schema here, and so is a live export when
TOMACCO_SCREEN_EXPORT names a bundle directory (or a batch directory) written by tomaccoctl.
The schema is checked with a small validator for the keywords it uses, since no JSON Schema
library is required to run the tests. Unknown properties are errors here, so a field added to
the output without a line in the schema and docs/screen-export.md fails."""
import json
import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/screen_export"
SCHEMAS = ROOT / "assets/schemas"


def load(path):
    return json.loads(Path(path).read_text())


def errors(value, schema, root, where="$"):
    """The problems of value against schema, for the keywords the schemas use."""
    if "$ref" in schema:
        return errors(value, root["$defs"][schema["$ref"].rsplit("/", 1)[1]], root, where)
    if "anyOf" in schema:
        found = [errors(value, option, root, where) for option in schema["anyOf"]]
        return [] if any(not f for f in found) else ["%s: matches none of anyOf (%s)" % (where, found[0][:1])]
    out = []
    if "const" in schema and value != schema["const"]:
        out.append("%s: %r is not %r" % (where, value, schema["const"]))
    if "enum" in schema and value not in schema["enum"]:
        out.append("%s: %r is not one of %r" % (where, value, schema["enum"]))
    kinds = schema.get("type")
    if kinds is not None:
        kinds = [kinds] if isinstance(kinds, str) else kinds
        checks = {
            "object": lambda v: isinstance(v, dict),
            "array": lambda v: isinstance(v, list),
            "string": lambda v: isinstance(v, str),
            "boolean": lambda v: isinstance(v, bool),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "null": lambda v: v is None,
        }
        if not any(checks[k](value) for k in kinds):
            return out + ["%s: %r is not %s" % (where, value, kinds)]
    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        out.append("%s: %r does not match %s" % (where, value, schema["pattern"]))
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                out.append("%s: %s is missing" % (where, key))
        known = schema.get("properties", {})
        for key, item in value.items():
            if key not in known:
                if "properties" in schema:
                    out.append("%s: %s is not in the schema" % (where, key))
                continue
            out += errors(item, known[key], root, "%s.%s" % (where, key))
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            out += errors(item, schema["items"], root, "%s[%d]" % (where, i))
    return out


def consistent(document):
    """What the schema cannot say."""
    problems = []
    elements = document["elements"]
    ids = [e["id"] for e in elements]
    if len(set(ids)) != len(ids):
        problems.append("element ids are not unique")
    seen = set()
    for e in elements:
        if e["parent"] is not None and e["parent"] not in seen:
            problems.append("%s: parent %s is not listed before it" % (e["id"], e["parent"]))
        seen.add(e["id"])
    marks = sorted(e["mark"] for e in elements if e["mark"] is not None)
    if marks != list(range(1, len(marks) + 1)):
        problems.append("mark numbers are not 1..N: %s" % marks)
    if document["marks"]["count"] != len(marks):
        problems.append("marks.count is not the number of marked elements")
    display = document["display"]
    if (display["width_px"], display["height_px"]) != (document["screenshot"]["width"], document["screenshot"]["height"]):
        problems.append("display and screenshot sizes differ")
    scale = display["scale"]
    for e in elements:
        dp = e["bounds"]["dp"]
        if (dp is None) != (scale is None):
            problems.append("%s: dp bounds and display.scale disagree" % e["id"])
        elif dp is not None and abs(dp["width"] - e["bounds"]["px"]["width"] / scale) > 0.01:
            problems.append("%s: dp width is not px / scale" % e["id"])
    if document["elements_source"] == "hierarchy":
        if document["hierarchy"] != "available" or any(e["source"] != "hierarchy" for e in elements):
            problems.append("hierarchy elements without an available hierarchy")
    if document["elements_source"] == "ocr":
        if document["hierarchy"] == "available" or any(e["source"] != "ocr" or e["clickable"] is not None for e in elements):
            problems.append("OCR elements must say source ocr and report no state")
    return problems


class ScreenExportSchemaTests(unittest.TestCase):
    longMessage = False
    schema = load(SCHEMAS / "screen-export-v1.schema.json")
    index = load(SCHEMAS / "screen-export-index-v1.schema.json")

    def check(self, path):
        document = load(path)
        self.assertEqual(errors(document, self.schema, self.schema), [], str(path))
        self.assertEqual(consistent(document), [], str(path))
        return document

    def test_the_recorded_bundles_follow_the_schema(self):
        names = ["screen_export_android.json", "screen_export_ocr.json", "screen_export_live_android.json"]
        documents = {name: self.check(FIXTURES / name) for name in names}
        self.assertEqual(documents["screen_export_android.json"]["hierarchy"], "available")
        ocr = documents["screen_export_ocr.json"]
        self.assertEqual((ocr["hierarchy"], ocr["elements_source"]), ("unavailable", "ocr"))
        self.assertTrue(all(e["source"] == "ocr" for e in ocr["elements"]))

    def test_the_recorded_index_follows_its_schema(self):
        index = load(FIXTURES / "screen_export_index.json")
        self.assertEqual(errors(index, self.index, self.index), [])
        self.assertEqual(index["count"], len(index["screens"]))
        self.assertEqual(index["failed"], sum(1 for s in index["screens"] if not s["ok"]))

    def test_the_validator_catches_a_wrong_document(self):
        document = load(FIXTURES / "screen_export_android.json")
        document["version"] = 2
        document["display"]["orientation"] = "diagonal"
        document["surprise"] = 1
        del document["elements"][0]["bounds"]["dp"]
        found = " ".join(errors(document, self.schema, self.schema))
        for needle in ["$.version", "orientation", "dp is missing", "$.elements[0].bounds"]:
            self.assertIn(needle, found)
        self.assertTrue(any("is missing" in e for e in errors({"x": 1}, self.schema, self.schema)))

    def test_the_schema_and_the_documentation_name_the_same_fields(self):
        docs = (ROOT / "docs/screen-export.md").read_text()
        for key in self.schema["properties"]:
            self.assertIn("`%s`" % key, docs, "docs/screen-export.md does not describe %s" % key)
        for key in self.schema["$defs"]["element"]["properties"]:
            self.assertIn("`%s`" % key, docs, "docs/screen-export.md does not describe element field %s" % key)

    @unittest.skipUnless(os.environ.get("TOMACCO_SCREEN_EXPORT"), "set TOMACCO_SCREEN_EXPORT to a bundle written by screen.export")
    def test_a_live_export_follows_the_schema(self):
        base = Path(os.environ["TOMACCO_SCREEN_EXPORT"])
        files = [base / "screen.json"] if (base / "screen.json").exists() else sorted(base.glob("*/screen.json"))
        self.assertTrue(files, "no screen.json under %s" % base)
        for path in files:
            self.check(path)
            self.assertTrue((path.parent / "screen.png").exists())
        if (base / "index.json").exists():
            self.assertEqual(errors(load(base / "index.json"), self.index, self.index), [])


if __name__ == "__main__":
    unittest.main()
