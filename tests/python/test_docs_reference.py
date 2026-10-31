"""docs/reference/ is the output of tools/build_reference.py for the checked-in
assets/operations.json: a schema change without a regenerated reference fails
here. Every operation has its entry, and every example call passes tomaccoctl's
argument validation but for the placeholders. With TOMACCOCTL naming a built
tomaccoctl, each example is the one `tomaccoctl help` prints."""
import importlib.util
import json
import os
import re
import subprocess
import unittest
from pathlib import Path

try:
    from test_docs_virtual_iphone import refusal
except ImportError:  # run as tests.python.test_docs_reference from the repository root
    from tests.python.test_docs_virtual_iphone import refusal

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("build_reference", ROOT / "tools/build_reference.py")
build_reference = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_reference)


def without_patterns(schema):
    # A placeholder such as "<path>" cannot match a pattern; the rest of the schema still holds.
    if isinstance(schema, dict):
        return {key: without_patterns(value) for key, value in schema.items() if key != "pattern"}
    if isinstance(schema, list):
        return [without_patterns(item) for item in schema]
    return schema


class ReferenceTests(unittest.TestCase):
    longMessage = False

    def test_the_checked_in_reference_is_current(self):
        found = build_reference.stale()
        self.assertEqual(found, [], "stale: %s; run python3 tools/build_reference.py" % ", ".join(found))

    def test_every_operation_has_an_entry_with_its_annotations_and_example(self):
        operations = build_reference.load()
        self.assertEqual(len(operations), len({operation["name"] for operation in operations}))
        index = (ROOT / "docs/reference/README.md").read_text(encoding="utf-8")
        self.assertIn(f"{len(operations)} in all", index)
        for operation in operations:
            name = operation["name"]
            page = (ROOT / "docs/reference" / f"{build_reference.area(name)}.md").read_text(encoding="utf-8")
            entry = page.split(f"\n### {name}\n", 1)
            self.assertEqual(len(entry), 2, f"no entry for {name}")
            entry = entry[1].split("\n### ", 1)[0]
            self.assertIn("Annotations: ", entry, f"{name} lacks its annotations")
            if operation["annotations"].get("destructiveHint") and not operation["annotations"].get("readOnlyHint"):
                self.assertIn("**destructive**", entry, f"{name} is destructive and not marked")
            self.assertIn("\n" + build_reference.example_call(operation) + "\n", entry, f"{name} lacks its example")
            for key in operation["inputSchema"].get("properties", {}):
                self.assertIn(f"| `{key}` |", entry, f"{name} lacks its argument {key}")
            self.assertIn(f"]({build_reference.area(name)}.md#{name.replace('.', '')})", index, f"index lacks {name}")

    def test_every_example_passes_argument_validation(self):
        for operation in build_reference.load():
            arguments = build_reference.example(operation["inputSchema"])
            problem = refusal(arguments, without_patterns(operation["inputSchema"]))
            self.assertIsNone(problem, f"{operation['name']}: {problem}")

    def test_a_destructive_operation_reads_as_one(self):
        storage = (ROOT / "docs/reference/storage.md").read_text(encoding="utf-8")
        self.assertIn("Annotations: **destructive**, idempotent, closed world.", storage)
        job = (ROOT / "docs/reference/job.md").read_text(encoding="utf-8")
        self.assertIn("tomaccoctl call job.wait '{\"job_id\":\"<job_id>\"}'", job)
        self.assertIn("| `seconds` | integer |  | `30` | 0 to 60", job)

    @unittest.skipUnless(os.environ.get("TOMACCOCTL"), "set TOMACCOCTL to a built tomaccoctl")
    def test_every_example_is_the_one_tomaccoctl_help_prints(self):
        binary = os.environ["TOMACCOCTL"]
        for operation in build_reference.load():
            text = subprocess.run([binary, "help", operation["name"]], capture_output=True, text=True,
                                  check=True).stdout
            printed = re.search(r"^Example:\n  (.*)$", text, re.M)
            self.assertIsNotNone(printed, f"{operation['name']}: no example in tomaccoctl help")
            self.assertEqual(printed.group(1), build_reference.example_call(operation), operation["name"])


if __name__ == "__main__":
    unittest.main()
