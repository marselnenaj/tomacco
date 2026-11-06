"""diagnostics.report_draft through the compiled CLI: a readable issue, attachments
with digests, a redaction receipt, secrets and device ids masked, nothing sent."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_fixture import Isolated  # noqa: E402


class ReportDraftTests(unittest.TestCase):
    def test_a_draft_is_readable_masked_and_sent_nowhere(self):
        box = Isolated()
        try:
            # A job that failed, so the draft has something to excerpt (the trapped adb fails loudly).
            job = box.result("device.app", {"platform": "android", "device": "R5CT30ABCDE", "bundle_id": "com.example.app", "action": "stop"})
            box.result("job.wait", {"job_id": job["id"], "seconds": 30})
            draft = box.result("diagnostics.report_draft", {
                "title": "Stop fails on Ana's phone",
                "steps": "adb -s R5CT30ABCDE shell am force-stop with token ghp_0123456789abcdefABCDEF0123 from %s/project" % box.home,
                "expected": "the app stops", "actual": "the job failed", "directory": str(box.home / "draft")}, timeout=120)
            self.assertFalse(draft["sent"])
            directory = Path(draft["directory"])
            issue = (directory / "issue.md").read_text()
            self.assertIn("# Stop fails on Ana's phone", issue)
            self.assertIn("[redacted]", issue)
            self.assertNotIn("ghp_0123456789abcdefABCDEF0123", issue)
            self.assertNotIn("R5CT30ABCDE", issue, "the serial is a placeholder")
            self.assertIn("[device-1]", issue)
            self.assertNotIn(str(box.home), issue, "the home directory is ~")
            self.assertIn("~/project", issue)
            self.assertIn("## Jobs that did not succeed", issue)
            receipt = json.loads((directory / "redaction-receipt.json").read_text())
            self.assertEqual(receipt["secrets_masked"], 1)
            self.assertEqual(receipt["device_ids_masked"], 1)
            self.assertFalse(receipt["sent"])
            names = [a["name"] for a in draft["attachments"]]
            self.assertIn("attachments/tomacco-report.tar.gz", names)
            self.assertTrue(any(n.startswith("attachments/job-") for n in names))
            for attachment in draft["attachments"]:
                self.assertTrue((directory / attachment["name"]).is_file(), attachment["name"])
                self.assertEqual(len(attachment["sha256"]), 64)
            self.assertTrue(draft["issue_url"].startswith("https://github.com/"))
            self.assertIn("title=Stop%20fails", draft["issue_url"])
            self.assertNotIn("redacted", draft["issue_url"])
            self.assertNotIn("body=", draft["issue_url"])
            self.assertEqual(box.error("diagnostics.report_draft", {"title": "again", "directory": str(box.home / "draft")})["code"], "path_exists")
        finally:
            box.close()


if __name__ == "__main__":
    unittest.main()
