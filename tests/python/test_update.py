"""The update operations as a client sees them, without any network request.

The whole path, with a real package and a local fixture, is tests/update_e2e.rs.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_release_files import NativeTree, package_tree

ROOT = Path(__file__).resolve().parents[2]
TOMACCOCTL = os.environ.get("TOMACCOCTL", str(ROOT / "build/tomaccoctl"))


@unittest.skipUnless(Path(TOMACCOCTL).is_file(), f"needs tomaccoctl at {TOMACCOCTL}")
class UpdateOperations(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)

    def call(self, operation, arguments=None, forced=True, path=None):
        environment = {key: value for key, value in os.environ.items() if key != "TOMACCO_UPDATES"}
        environment.update(HOME=str(self.home), TOMACCO_HOME=str(self.home / "data"))
        # Without the fixture a request would go to GitHub: every call that could
        # make one is forced off or asserted not to happen.
        if forced:
            environment["TOMACCO_UPDATES"] = "off"
        if path:
            environment["PATH"] = path
        done = subprocess.run([TOMACCOCTL, "call", operation, json.dumps(arguments or {})], capture_output=True, text=True, env=environment, timeout=60, stdin=subprocess.DEVNULL)
        return json.loads(done.stdout)

    def test_the_mode_is_notify_by_default_persists_and_is_validated(self):
        self.assertEqual(self.call("update.status", forced=False)["result"]["mode"], "notify")
        for mode in ("auto", "off", "notify"):
            self.assertTrue(self.call("settings.configure", {"updates": {"mode": mode}})["ok"])
            self.assertEqual(self.call("settings.get")["result"]["updates"]["mode"], mode)
            self.assertEqual(self.call("update.status", forced=False)["result"]["mode"], mode)
        self.assertFalse(self.call("settings.configure", {"updates": {"mode": "sometimes"}})["ok"])
        self.assertEqual(self.call("update.status", forced=False)["result"]["channel"], "stable")
        for channel in ("prerelease", "stable"):
            self.assertTrue(self.call("settings.configure", {"updates": {"channel": channel}})["ok"])
            self.assertEqual(self.call("settings.get")["result"]["updates"]["channel"], channel)
            self.assertEqual(self.call("update.status", forced=False)["result"]["channel"], channel)
        self.assertFalse(self.call("settings.configure", {"updates": {"channel": "nightly"}})["ok"])
        self.assertFalse(self.call("settings.configure", {"updates": {"unknown": True}})["ok"])
        self.assertFalse(self.call("update.check", {"unknown": True})["ok"])

    def test_off_makes_no_request_and_records_no_check(self):
        self.call("settings.configure", {"updates": {"mode": "off"}})
        reply = self.call("update.check", forced=False, path="/nonexistent")["result"]
        self.assertEqual(reply["state"], "not_checked")
        self.assertIsNone(reply["checked_at"])
        self.assertEqual(self.call("update.apply", forced=False)["error"]["code"], "update_off")

    def test_the_environment_forces_off_even_for_a_manual_check(self):
        self.call("settings.configure", {"updates": {"mode": "auto"}})
        reply = self.call("update.check", {"manual": True})["result"]
        self.assertEqual((reply["mode"], reply["forced"], reply["state"]), ("off", "environment", "not_checked"))
        self.assertFalse(reply["can_apply"])
        self.assertIn("TOMACCO_UPDATES", reply["blocked_reason"])
        self.assertEqual(self.call("update.apply", {"manual": True})["error"]["code"], "update_forced_off")

    def test_a_token_file_must_be_private_and_is_never_echoed(self):
        token = self.home / "token"
        token.write_text("ghp_secretvalue\n")
        token.chmod(0o644)
        self.assertEqual(self.call("settings.configure", {"updates": {"token_file": str(token)}})["error"]["code"], "secret_permissions")
        token.chmod(0o600)
        reply = self.call("settings.configure", {"updates": {"token_file": str(token)}})
        self.assertTrue(reply["ok"])
        self.assertNotIn("ghp_secretvalue", json.dumps(reply))
        self.assertNotIn("ghp_secretvalue", (self.home / "data/settings.json").read_text())

    def test_a_copy_that_is_not_an_installation_is_left_alone(self):
        reply = self.call("update.status", forced=False)["result"]
        self.assertIn(reply["install"]["kind"], ("source_checkout", "unmanaged", "system_package"))
        self.assertEqual(self.call("update.rollback")["ok"], False)

    def test_a_release_binary_has_the_product_key_and_no_test_hooks(self):
        if "/debug/" in TOMACCOCTL:
            self.skipTest("a debug build has the test hooks")
        data = Path(TOMACCOCTL).read_bytes()
        self.assertNotIn(b"TOMACCO_UPDATE_TEST_API", data)
        self.assertNotIn(b"Tomacco-TEST-VERSION", data)
        product = (Path(__file__).resolve().parents[2] / "native/product.rs").read_text()
        has_key = 'pub const RELEASE_SIGNING_PUBLIC_KEY_BASE64: &str = "";' not in product
        self.assertEqual(self.call("update.status", forced=False)["result"]["signing_key_configured"], has_key)


class InstallerUpdateMode(unittest.TestCase):
    def install(self, top, home, *arguments):
        done = subprocess.run([sys.executable, str(top / "install.py"), "--user-home", str(home), *arguments], capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_an_update_adds_no_integration_and_refreshes_the_ones_in_use(self):
        with tempfile.TemporaryDirectory() as directory:
            top, home = Path(directory, "tomacco-7.8.9-Linux-x86_64"), Path(directory, "home")
            package_tree(top)
            config = home / ".config/hypr/hyprland.lua"
            config.parent.mkdir(parents=True)
            config.write_text("-- mine\n")
            shell = home / ".config/omarchy/shell.json"
            shell.parent.mkdir(parents=True)
            shell.write_text(json.dumps({"bar": {"layout": {"right": []}}}))
            # Installed without either: an update leaves them out.
            self.install(top, home, "--update")
            self.assertEqual(config.read_text(), "-- mine\n")
            self.assertFalse((home / ".config/hypr/tomacco.lua").exists())
            self.assertEqual(json.loads(shell.read_text())["bar"]["layout"]["right"], [])
            # In use: they stay in use, and a widget disabled by the user stays disabled.
            self.install(top, home, "--omarchy")
            value = json.loads(shell.read_text())
            value["disabledPlugins"] = ["io.github.marselnenaj.tomacco"]
            shell.write_text(json.dumps(value))
            (home / ".config/hypr/tomacco.lua").unlink()
            plugin = home / ".config/omarchy/plugins/io.github.marselnenaj.tomacco/manifest.json"
            plugin.unlink()
            self.install(top, home, "--update")
            self.assertTrue(plugin.is_file())
            self.assertEqual(json.loads(shell.read_text())["disabledPlugins"], ["io.github.marselnenaj.tomacco"])
            self.assertFalse((home / ".config/hypr/tomacco.lua").exists())


class NativeInstallerUpdateMode(NativeTree, InstallerUpdateMode):
    """--update as an older updater passes it to install.py, which hands over to tomaccoctl."""


class PublicDocuments(unittest.TestCase):
    def test_the_signing_steps_are_not_in_the_repository(self):
        sources = [ROOT / "docs/building.md", ROOT / "docs/privacy.md", ROOT / "docs/agents.md", ROOT / "docs/desktop.md",
                   ROOT / "native/product.rs", ROOT / "native/update.rs", ROOT / "CHANGELOG.md"]
        for source in sources:
            text = source.read_text()
            for word in ("genpkey", "pkeyutl", "secret set", "gh release upload"):
                self.assertNotIn(word, text, source)

    def test_the_privacy_guide_says_what_a_check_sends_and_how_to_stop_it(self):
        text = (ROOT / "docs/privacy.md").read_text()
        self.assertNotIn("does not check for updates", text)
        for word in ("api.github.com", "objects.githubusercontent.com", "TOMACCO_UPDATES=off", "TOMACCO_DISABLE_UPDATES"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
