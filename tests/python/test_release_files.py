"""What a release ships: the tarball's own installer, the notes and the artwork."""

import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
APP_ID = "io.github.marselnenaj.tomacco"


def package_tree(top):
    """The tree CMake's install rules make, built by hand with stand-in binaries."""
    for name in ("tomacco", "tomaccoctl"):
        binary = top / "bin" / name
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"\x7fELF" + name.encode())
        binary.chmod(0o755)
    shutil.copy2(ROOT / "install.py", top / "install.py")
    share = top / "share/tomacco"
    share.mkdir(parents=True)
    (share / "VERSION").write_text("7.8.9\n")
    (share / "tools").mkdir()
    shutil.copy2(ROOT / "tools/build_source_release.py", share / "tools")
    shutil.copytree(ROOT / "integrations/omarchy", share / "omarchy")
    shutil.copytree(ROOT / "integrations/agent-skill", share / "agent-skill")
    (share / "examples/ios-restore-native").mkdir(parents=True)
    (share / "examples/ios-restore-native/example.c").write_text("int main(void){return 0;}\n")
    recipes = share / "integrations/ios-emulator"
    recipes.mkdir(parents=True)
    (recipes / "README.md").write_text("Recipes\n")
    (recipes / "one.patch").write_text("patch\n")
    (top / "lib/tomacco/assets").mkdir(parents=True)
    (top / "lib/tomacco/assets/models.json").write_text("[]\n")
    documents = top / "share/doc/tomacco"
    documents.mkdir(parents=True)
    (documents / "RELEASE_NOTES.md").write_text("Notes\n")
    (documents / "LICENSE").write_text("License\n")
    for source, target in (("assets/icon.svg", f"share/icons/hicolor/scalable/apps/{APP_ID}.svg"),
                           (f"assets/{APP_ID}.desktop", f"share/applications/{APP_ID}.desktop"),
                           (f"assets/{APP_ID}.metainfo.xml", f"share/metainfo/{APP_ID}.metainfo.xml"),
                           ("docs/tomaccoctl.1", "share/man/man1/tomaccoctl.1")):
        (top / target).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / source, top / target)


class TarballInstaller(unittest.TestCase):
    def test_the_tarball_installs_from_its_own_tree_and_uninstalls(self):
        with tempfile.TemporaryDirectory() as directory:
            top, home = Path(directory, "tomacco-7.8.9-Linux-x86_64"), Path(directory, "home")
            home.mkdir()
            package_tree(top)

            def install(*arguments):
                return subprocess.run([sys.executable, "-I", str(top / "install.py"), "--user-home", str(home), *arguments],
                                      capture_output=True, text=True, check=True)
            self.assertIn("7.8.9", install("--version").stdout)
            shell = home / ".config/omarchy/shell.json"
            shell.parent.mkdir(parents=True)
            shell.write_text('{"bar": {"layout": {"right": []}}}\n')
            (home / ".claude").mkdir()
            installed = json.loads(install("--no-window-rule", "--omarchy").stdout)
            plugin = home / ".config/omarchy/plugins" / APP_ID
            self.assertEqual(json.loads((plugin / "manifest.json").read_text())["license"], "GPL-2.0-or-later")
            self.assertEqual(json.loads(shell.read_text())["bar"]["layout"]["right"], [{"id": APP_ID}])
            library = home / ".local/lib/tomacco"
            self.assertEqual((library / "VERSION").read_text(), "7.8.9\n")
            self.assertEqual((library / "tomacco").read_bytes(), b"\x7fELFtomacco")
            self.assertTrue((library / "tools/build_source_release.py").is_file())
            self.assertTrue((library / "examples/ios-restore-native/example.c").is_file())
            self.assertEqual((library / "integrations/ios-emulator/one.patch").read_text(), "patch\n")
            self.assertTrue((library / "assets/models.json").is_file())
            self.assertEqual((home / ".local/share/doc/tomacco/RELEASE_NOTES.md").read_text(), "Notes\n")
            entry = (home / f".local/share/applications/{APP_ID}.desktop").read_text()
            self.assertIn(f'Exec="{home}/.local/bin/tomacco"', entry)
            self.assertTrue((home / f".local/share/icons/hicolor/scalable/apps/{APP_ID}.svg").is_file())
            self.assertTrue((home / f".local/share/metainfo/{APP_ID}.metainfo.xml").is_file())
            self.assertEqual((home / ".local/share/man/man1/tomaccoctl.1").read_bytes(), (ROOT / "docs/tomaccoctl.1").read_bytes())
            # The agent skill comes from the tarball's share/tomacco/agent-skill.
            skill = library / "skills/tomacco"
            self.assertEqual((skill / "SKILL.md").read_bytes(), (ROOT / "integrations/agent-skill/tomacco/SKILL.md").read_bytes())
            self.assertEqual(installed["agent_skill"]["linked"], [str(home / ".claude/skills/tomacco")])
            self.assertEqual(os.readlink(home / ".claude/skills/tomacco"), str(skill.resolve()))
            result = json.loads(install("--uninstall").stdout)
            self.assertEqual(result["kept"], [])
            self.assertFalse((home / ".local/bin/tomacco").exists())
            self.assertFalse(library.exists())
            self.assertFalse(os.path.lexists(home / ".claude/skills"))
            self.assertTrue((home / ".claude").is_dir())

    def test_cmake_packages_what_the_installer_reads(self):
        cmake = (ROOT / "CMakeLists.txt").read_text()
        self.assertIn("install(PROGRAMS install.py DESTINATION .)", cmake)
        self.assertRegex(cmake, r'install\(FILES "\$\{CMAKE_CURRENT_BINARY_DIR\}/VERSION" DESTINATION share/tomacco\)')
        self.assertIn("assets/operations.json DESTINATION lib/tomacco/assets", cmake)
        self.assertIn("install(FILES docs/tomaccoctl.1 DESTINATION share/man/man1)", cmake)
        # install.py reads the skill from share/tomacco/agent-skill in a package.
        sys.path.insert(0, str(ROOT))
        from install import PACKAGE_PATHS
        self.assertEqual(PACKAGE_PATHS["integrations/agent-skill"], "share/tomacco/agent-skill")
        self.assertIn("install(DIRECTORY integrations/agent-skill DESTINATION share/tomacco)", cmake)


class InstalledDocuments(unittest.TestCase):
    def test_html_links_of_the_readme_work_once_installed(self):
        sys.path.insert(0, str(ROOT))
        from install import VERSION, documents
        readme = documents()["README.md"].decode()
        self.assertIn('href="guides.md"', readme)
        self.assertIn(f'src="https://github.com/marselnenaj/tomacco/raw/v{VERSION}/assets/readme/hero.png"', readme)
        self.assertNotIn('href="docs/', readme)

    def test_the_short_notes_are_installed(self):
        sys.path.insert(0, str(ROOT))
        from install import DOCUMENTS
        self.assertEqual(DOCUMENTS["docs/RELEASE_NOTES.md"], "RELEASE_NOTES.md")
        self.assertIn('QStringLiteral("RELEASE_NOTES.md")', (ROOT / "src/backend.cpp").read_text())
        self.assertIn('Studio.openDocument("RELEASE_NOTES.md")', (ROOT / "qml/NativeWorkspace.qml").read_text())


class ReleaseNotes(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("build_release_notes", ROOT / "tools/build_release_notes.py")
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)

    def tree(self, directory, text):
        root = Path(directory)
        (root / "docs").mkdir()
        (root / "CMakeLists.txt").write_text("project(Tomacco VERSION 1.2.3 LANGUAGES CXX)\n")
        (root / "docs/RELEASE_NOTES.md").write_text(text)
        return root

    def test_links_point_into_the_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.tree(directory, "# Tomacco 1.2.3\n[a](guides/x.md#y) [b](../README.md) [c](https://e.example/) [d](#e)\n")
            body = self.tool.body(root, "v1.2.3")
        self.assertIn("[a](https://github.com/marselnenaj/tomacco/blob/v1.2.3/docs/guides/x.md#y)", body)
        self.assertIn("[b](https://github.com/marselnenaj/tomacco/blob/v1.2.3/README.md)", body)
        self.assertIn("[c](https://e.example/)", body)
        self.assertIn("[d](#e)", body)

    def test_a_wrong_tag_or_pending_numbers_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.tree(directory, "# Tomacco 1.2.3\n")
            with self.assertRaisesRegex(SystemExit, "not this source's version"):
                self.tool.body(root, "v1.2.4")
        with tempfile.TemporaryDirectory() as directory:
            root = self.tree(directory, f"# Tomacco 1.2.3\n{self.tool.PENDING}\n")
            with self.assertRaisesRegex(SystemExit, "final numbers"):
                self.tool.body(root, "v1.2.3")

    def test_the_notes_name_this_version_and_stay_short(self):
        text = (ROOT / "docs/RELEASE_NOTES.md").read_text()
        self.assertTrue(text.startswith("# Tomacco "))
        self.assertLess(text.count("\n"), 100)


class Workflows(unittest.TestCase):
    def test_release_output_does_not_dirty_the_source_tarball(self):
        # build_source_release.py refuses a dirty tree, and the notes come first.
        ignored = (ROOT / ".gitignore").read_text().splitlines()
        self.assertIn("/dist/", ignored)
        self.assertIn("/dist-notes/", ignored)


class Artwork(unittest.TestCase):
    def test_the_icon_is_square_and_needs_no_font(self):
        root = ET.parse(ROOT / "assets/icon.svg").getroot()
        self.assertEqual(root.get("viewBox"), "0 0 100 100")
        self.assertEqual(root.findall(".//{http://www.w3.org/2000/svg}text"), [])
        for colour in ("#F2361F", "#2FA84F"):
            self.assertIn(colour, (ROOT / "assets/icon.svg").read_text())

    def test_the_social_preview_has_githubs_size(self):
        data = (ROOT / "assets/readme/social-preview.png").read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", data[16:24]), (1280, 640))
        self.assertLess(len(data), 1024 * 1024)

    def test_the_readme_graphics_state_the_current_operation_count(self):
        # They are drawn by assets/readme/generate.py; rerun it after the count changes.
        count = len(json.loads((ROOT / "assets/operations.json").read_text()))
        for stem in ("social", "how-it-works-dark", "how-it-works-light"):
            svg = (ROOT / f"assets/readme/{stem}.svg").read_text()
            self.assertRegex(svg, rf">(iOS · Android · )?{count}( typed operations · \w+)?</text>", stem)


class Newcomer(unittest.TestCase):
    """Two things a stranger runs in a first minute, on the build under test."""

    def built(self, name):
        path = Path(os.environ.get("TOMACCOCTL", ROOT / "build-product/tomaccoctl")).with_name(name)
        if not path.is_file():
            self.skipTest(f"{path} has not been built")
        return str(path)

    def test_a_reader_that_stops_early_does_not_make_tomaccoctl_panic(self):
        with tempfile.TemporaryDirectory() as home:
            done = subprocess.run(f'"{self.built("tomaccoctl")}" schema | head -c 1 > /dev/null; exit ${{PIPESTATUS[0]}}',
                                  shell=True, executable="/bin/bash", capture_output=True, text=True,
                                  env={**os.environ, "TOMACCO_HOME": home})
        self.assertNotIn("panicked", done.stderr)
        self.assertEqual(done.returncode, 0)

    def test_the_desktop_without_a_display_says_so_and_answers_its_version(self):
        binary = self.built("tomacco")
        env = {key: value for key, value in os.environ.items() if key not in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM")}
        with tempfile.TemporaryDirectory() as home:
            env.update(HOME=home, TOMACCO_HOME=home)
            done = subprocess.run([binary], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 1, done.stderr)
            self.assertIn("graphical session", done.stderr)
            done = subprocess.run([binary, "--version"], env={**env, "QTWEBENGINE_DISABLE_SANDBOX": "1"}, capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("Tomacco ", done.stdout)


class Catalogue(unittest.TestCase):
    def test_the_catalogue_reaches_agents_in_english(self):
        for model in json.loads((ROOT / "assets/models.json").read_text()):
            self.assertIn(model.get("family"), ("Current", "Android", "Other"), model["id"])
            self.assertNotRegex(model.get("display", ""), r"\d,\d", model["id"])

    def test_the_smoke_test_takes_the_duo_from_the_catalogue(self):
        # It compared against numbers from before the catalogue changed, and failed.
        main = (ROOT / "qml/Main.qml").read_text()
        self.assertNotRegex(main, r'checkDuoState\("open-[a-z]+",\s*\d')
        self.assertIn("result.width===root.currentModel.openWidth", main)


if __name__ == "__main__":
    unittest.main()


class ArchPackageTests(unittest.TestCase):
    """packaging/arch: the PKGBUILD and the .SRCINFO the AUR reads from it."""

    @staticmethod
    def pkgbuild():
        text = (ROOT / "packaging/arch/PKGBUILD").read_text()
        return dict(re.findall(r"(?m)^(\w+)=\(?'?\"?([^'\"()\n]*)", text))

    def test_srcinfo_matches_the_pkgbuild(self):
        fields = self.pkgbuild()
        info = (ROOT / "packaging/arch/.SRCINFO").read_text()
        version = fields["pkgver"]
        self.assertIn(f"\tpkgver = {version}\n", info)
        self.assertIn(f"\tpkgrel = {fields['pkgrel']}\n", info)
        self.assertIn(f"\tsha256sums = {fields['sha256sums']}\n", info)
        self.assertIn(f"\tsource = https://github.com/marselnenaj/tomacco/releases/download/v{version}/"
                      f"tomacco-{version}-source.tar.gz\n", info)
        self.assertRegex(fields["sha256sums"], r"^[0-9a-f]{64}$")
        self.assertRegex(fields["_commit"], r"^[0-9a-f]{40}$")

    def test_the_package_is_not_newer_than_the_source(self):
        # A PKGBUILD names a released version: never one the tree has not reached.
        cmake = re.search(r"project\(\s*Tomacco\s+VERSION\s+([0-9.]+)", (ROOT / "CMakeLists.txt").read_text()).group(1)
        version = lambda text: tuple(int(part) for part in text.split("."))
        self.assertLessEqual(version(self.pkgbuild()["pkgver"]), version(cmake))

    @unittest.skipUnless(shutil.which("makepkg"), "makepkg is not installed")
    def test_srcinfo_is_what_makepkg_prints(self):
        done = subprocess.run(["makepkg", "--printsrcinfo"], cwd=ROOT / "packaging/arch", capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, (ROOT / "packaging/arch/.SRCINFO").read_text())


class ReproduciblePackageTests(unittest.TestCase):
    def test_the_package_records_no_builder_and_no_build_time(self):
        cmake = (ROOT / "CMakeLists.txt").read_text()
        self.assertIn("set(CPACK_ARCHIVE_UID 0)", cmake)
        self.assertIn("set(CPACK_ARCHIVE_GID 0)", cmake)
        release = (ROOT / "tools/make_release.py").read_text()
        self.assertIn('"SOURCE_DATE_EPOCH"', release)
