"""The agent skill in integrations/agent-skill/tomacco names only what exists: every
operation against assets/operations.json, the installed guides against what install.py
installs and the tomacco://docs resources against the MCP server's guides. Its
`tomaccoctl call` lines are held to tomaccoctl's argument validation with the other
guides, in test_docs_virtual_iphone."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "integrations/agent-skill/tomacco"
OPERATIONS = {entry["name"] for entry in json.loads((ROOT / "assets/operations.json").read_text(encoding="utf-8"))}
# Backticked words that are neither operations nor meant to be: keys, states, codes and commands.
NOT_OPERATIONS = re.compile(r".*[/ {:\"'~]|.*\.(md|json|png)$|--|^[A-Z]")
# Dotted words that are paths into a result, not operations.
RESULT_PATHS = {"result.id", "error.code"}


def files():
    return sorted(SKILL.glob("*.md"))


class AgentSkillTests(unittest.TestCase):
    def test_the_front_matter_names_the_skill_and_when_to_use_it(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        front = re.match(r"---\n(.*?)\n---\n", text, re.S)
        self.assertTrue(front, "SKILL.md starts with YAML front matter")
        self.assertRegex(front.group(1), r"(?m)^name: tomacco$")
        description = front.group(1).split("description:", 1)[1]
        for word in ("Android", "iOS", "virtual iPhone", "screenshots", "UI tests", "tomaccoctl"):
            self.assertIn(word, description)

    def test_every_backticked_operation_exists(self):
        checked = 0
        for path in files():
            for token in re.findall(r"`([^`\n]+)`", path.read_text(encoding="utf-8")):
                if NOT_OPERATIONS.match(token) or token in RESULT_PATHS:
                    continue
                if "." in token:
                    # Anything dotted that is not a file is meant as an operation.
                    self.assertTrue(token in OPERATIONS, f"{path.name} names {token}, which is no operation")
                    checked += 1
                elif token in OPERATIONS:
                    checked += 1
        self.assertGreater(checked, 40)

    def test_the_task_table_lists_only_operations(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        rows = [line for line in text.split("## Which operation", 1)[1].split("\n## ", 1)[0].splitlines()
                if line.startswith("| ") and "---" not in line][1:]
        self.assertGreater(len(rows), 8)
        for row in rows:
            for token in re.findall(r"`([^`]+)`", row.split("|")[2]):
                self.assertTrue(token in OPERATIONS, f"{token} is no operation: {row}")

    def test_the_guides_it_points_to_are_installed_and_served(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from install import DOCUMENTS
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        documentation = text.split("## Documentation", 1)[1]
        installed = set(DOCUMENTS.values())
        for name in re.findall(r"`([a-z-]+\.md)`", documentation):
            self.assertIn(name, installed)
        served = set(re.findall(r'name: "([a-z-]+)",\n\s+title: [^\n]+\n\s+description: [^\n]+\n\s+text: include_str!',
                                (ROOT / "native/mcp.rs").read_text(encoding="utf-8")))
        self.assertIn("agents", served)
        for name in re.findall(r"tomacco://docs/([a-z-]+)", text):
            self.assertIn(name, served)
        for link in re.findall(r"\]\(([^)]+)\)", text):
            self.assertTrue((SKILL / link).is_file(), link)

    def test_every_file_of_the_skill_is_text_the_package_carries(self):
        listed = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
        self.assertIn("install(DIRECTORY integrations/agent-skill DESTINATION share/tomacco)", listed)
        for path in SKILL.rglob("*"):
            if path.is_file():
                path.read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
