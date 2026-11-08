"""Execute the real verifier shell wrapper with offline stdout/stderr fixtures."""
import copy
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import build_portable

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/portable-smoke"
COMPLETE = json.loads((FIXTURES / "complete13.json").read_text())
INCOMPLETE = json.loads((FIXTURES / "incomplete5-passedtrue.json").read_text())


def line(payload):
    return "TOMACCO_TEST " + json.dumps(payload) + "\n"


class StrictSmoke(unittest.TestCase):
    def execute(self, stdout, stderr="", code=0):
        start = build_portable.VERIFY_SCRIPT.index("check() {\n")
        end = build_portable.VERIFY_SCRIPT.index("missing() {\n", start)
        helpers = build_portable.VERIFY_SCRIPT[start:end]
        result_function = re.search(r"(?m)^result\(\) \{.*\}$", build_portable.VERIFY_SCRIPT).group(0)
        script = "set -u\nstatus=0\n" + result_function + "\n" + helpers + '\ncheck fixture smoke "$@"\nexit "$status"\n'
        fake = "import sys;sys.stdout.write(sys.argv[1]);sys.stderr.write(sys.argv[2]);sys.exit(int(sys.argv[3]))"
        return subprocess.run(["sh", "-c", script, "fixture", sys.executable, "-c", fake,
                               stdout, stderr, str(code)], capture_output=True, text=True, timeout=15)

    def test_fixture_names_independently_match_all_source_checks(self):
        qml = (ROOT / "qml/Main.qml").read_text()
        expected = set(re.findall(r'testResults\.push\(\{check:\s*"([^"]+)"\s*,', qml))
        expected.update("duo-" + x for x in re.findall(r'root\.checkDuoState\("([^"]+)"', qml))
        self.assertEqual(len(expected), 13)
        self.assertEqual({c["check"] for c in COMPLETE["checks"]}, expected)
        self.assertEqual(len(INCOMPLETE["checks"]), 5)
        self.assertIs(INCOMPLETE["passed"], True)

    def test_all_three_smoke_commands_use_the_strict_wrapper(self):
        for label in ("smoke-offscreen", "smoke-xcb-xvfb", "installed-smoke-offscreen"):
            self.assertRegex(build_portable.VERIFY_SCRIPT,
                             rf"(?m)^check {label} smoke env .* --smoke-test$")

    def test_wrapper_preserves_the_child_stdout_and_stderr_in_the_log(self):
        result = self.execute(line(COMPLETE), "fixture stderr warning\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("TOMACCO_TEST ", result.stdout)
        self.assertIn("fixture stderr warning", result.stdout)
        self.assertIn("RESULT fixture ok", result.stdout)


def altered(function):
    value = copy.deepcopy(COMPLETE)
    function(value)
    return line(value)


CASES = [
    ("complete13_stdout", line(COMPLETE), "", 0, True),
    ("complete13_stderr", "", line(COMPLETE), 0, True),
    ("asynchronous_order_changed", altered(lambda r: r["checks"].reverse()), "", 0, True),
    ("benign_warning", line(COMPLETE), "GPUInfo not initialized\nlibQt6Core.so resolved\n", 0, True),
    ("incomplete5_passedtrue", line(INCOMPLETE), "", 0, False),
    ("incomplete5_and_chromium_fatal", line(INCOMPLETE), "[FATAL:zygote_linux.cc:123] Check failed: renderer\n", 0, False),
    ("missing_one_callback", altered(lambda r: r["checks"].pop(0)), "", 0, False),
    ("duplicate_check_replaces_missing", altered(lambda r: r["checks"].__setitem__(0, copy.deepcopy(r["checks"][1]))), "", 0, False),
    ("unexpected_name", altered(lambda r: r["checks"][0].__setitem__("check", "unexpected")), "", 0, False),
    ("failed_check_with_overalltrue", altered(lambda r: r["checks"][0].__setitem__("pass", False)), "", 0, False),
    ("truthy_integer_check", altered(lambda r: r["checks"][0].__setitem__("pass", 1)), "", 0, False),
    ("truthy_string_check", altered(lambda r: r["checks"][0].__setitem__("pass", "true")), "", 0, False),
    ("overallfalse", altered(lambda r: r.__setitem__("passed", False)), "", 0, False),
    ("overalltruthyinteger", altered(lambda r: r.__setitem__("passed", 1)), "", 0, False),
    ("missing_overall_passed", altered(lambda r: r.pop("passed")), "", 0, False),
    ("missing_check_pass", altered(lambda r: r["checks"][0].pop("pass")), "", 0, False),
    ("null_check_name", altered(lambda r: r["checks"][0].__setitem__("check", None)), "", 0, False),
    ("unhashable_check_name", altered(lambda r: r["checks"][0].__setitem__("check", [])), "", 0, False),
    ("checks_not_an_array", altered(lambda r: r.__setitem__("checks", {})), "", 0, False),
    ("check_not_an_object", altered(lambda r: r["checks"].__setitem__(0, 1)), "", 0, False),
    ("missing_report_exit0", "No report\n", "", 0, False),
    ("malformed_report_exit0", "TOMACCO_TEST {broken\n", "", 0, False),
    ("nonobject_report", "TOMACCO_TEST []\n", "", 0, False),
    ("duplicate_reports", line(COMPLETE) + line(COMPLETE), "", 0, False),
    ("valid_report_nonzero_exit", line(COMPLETE), "", 7, False),
    ("valid_report_timeout_exit", line(COMPLETE), "", 124, False),
    ("valid_report_fatal_stderr", line(COMPLETE), "[FATAL:renderer.cc:1] renderer died\n", 0, False),
    ("valid_report_core_dump", line(COMPLETE), "Aborted (core dumped)\n", 0, False),
    ("valid_report_qemu_signal", line(COMPLETE), "qemu: uncaught target signal11 (Segmentation fault)\n", 0, False),
    ("valid_report_received_signal", line(COMPLETE), "Received signal11\n", 0, False),
    ("valid_report_check_failed", line(COMPLETE), "Check failed: process\n", 0, False),
]


def make_case(stdout, stderr, code, expected):
    def test(self):
        result = self.execute(stdout, stderr, code)
        self.assertEqual(result.returncode == 0, expected, result.stdout + result.stderr)
        self.assertIn("RESULT fixture " + ("ok" if expected else "fail"), result.stdout)
    return test


for name, stdout, stderr, code, expected in CASES:
    setattr(StrictSmoke, "test_" + name, make_case(stdout, stderr, code, expected))
