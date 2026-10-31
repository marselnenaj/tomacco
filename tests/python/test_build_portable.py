"""tools/build_portable.py without Docker: names, pins and the rules the build applies."""

import re
import tempfile
import unittest
from pathlib import Path

import build_portable
import make_release

ROOT = Path(__file__).resolve().parents[2]


class Names(unittest.TestCase):
    def test_the_release_the_installer_and_the_updater_agree_on_the_name(self):
        self.assertEqual(build_portable.package_name("1.2.3") + ".tar.gz", make_release.portable_name("tomacco", "1.2.3"))
        self.assertEqual(build_portable.package_name("1.2.3"), "tomacco-1.2.3-Linux-x86_64-portable")
        self.assertIn('if portable { "-portable" }', (ROOT / "native/update.rs").read_text())

    def test_the_builder_image_is_named_after_its_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            context = Path(directory)
            (context / "Dockerfile").write_text("FROM x\n")
            (context / "qt-archives.txt").write_text("")
            first = build_portable.image_tag(context)
            self.assertRegex(first, r"^tomacco-portable-builder:[0-9a-f]{12}$")
            (context / "qt-archives.txt").write_text("changed\n")
            self.assertNotEqual(build_portable.image_tag(context), first)


class Pins(unittest.TestCase):
    def test_every_download_of_the_builder_is_checked_against_a_pinned_checksum(self):
        dockerfile = (ROOT / "packaging/portable/Dockerfile").read_text()
        self.assertRegex(dockerfile, r"(?m)^FROM ubuntu:22\.04@sha256:[0-9a-f]{64}$")
        downloads = re.findall(r"curl -fsSL[^\n]*-o (\S+)", dockerfile)
        self.assertGreaterEqual(len(downloads), 5)
        for target in downloads:
            if target == '"$file"':
                self.assertIn('echo "$sum  $file" | sha256sum -c -', dockerfile)
            else:
                self.assertIn(f"  {target}\" | sha256sum -c -", dockerfile, target)
        for name in ("RUSTUP_SHA256", "CMAKE_SHA256", "FFMPEG_SHA256", "ICU_LICENSE_SHA256"):
            self.assertRegex(dockerfile, rf"ARG {name}=[0-9a-f]{{64}}\n")
        archives = [line for line in (ROOT / "packaging/portable/qt-archives.txt").read_text().splitlines()
                    if line and not line.startswith("#")]
        self.assertGreaterEqual(len(archives), 8)
        for line in archives:
            self.assertRegex(line, r"^[0-9a-f]{64}  (desktop|extensions)/\S+\.7z$")

    def test_ffmpeg_is_built_without_gpl_or_nonfree_parts(self):
        dockerfile = (ROOT / "packaging/portable/Dockerfile").read_text()
        configure = dockerfile[dockerfile.index("./configure"):dockerfile.index("&& grep -q", dockerfile.index("./configure"))]
        self.assertIn("--disable-autodetect", configure)
        for flag in ("--enable-gpl", "--enable-nonfree", "--enable-version3", "--enable-lib"):
            self.assertNotIn(flag, configure)
        for setting in ("GPL", "NONFREE", "VERSION3"):
            self.assertIn(f"grep -q '^#define CONFIG_{setting} 0' config.h", dockerfile)

    def test_rust_is_at_least_the_declared_minimum(self):
        declared = re.search(r'rust-version = "([0-9.]+)"', (ROOT / "Cargo.toml").read_text()).group(1)
        pinned = re.search(r"ARG RUST_VERSION=([0-9.]+)", (ROOT / "packaging/portable/Dockerfile").read_text()).group(1)
        self.assertGreaterEqual(tuple(map(int, pinned.split("."))), tuple(map(int, declared.split("."))))


class Rules(unittest.TestCase):
    def test_the_host_provides_system_libraries_and_never_qt_or_ffmpeg(self):
        for name in ("libc.so.6", "libstdc++.so.6", "libGL.so.1", "libEGL.so.1", "libX11.so.6", "libxcb-xkb.so.1", "libnss3.so",
                     "libwayland-client.so.0", "libfontconfig.so.1", "libglib-2.0.so.0", "libxkbcommon-x11.so.0"):
            self.assertTrue(build_portable.SYSTEM.match(name), name)
        for name in ("libQt6Core.so.6", "libavcodec.so.63", "libswscale.so.10", "libicuuc.so.73", "libssl.so.3", "libQt6XcbQpa.so.6"):
            self.assertFalse(build_portable.SYSTEM.match(name), name)
        # Bundled helpers must not also be expected from the host, and depend only on libxcb.
        for name in build_portable.BUNDLED_SYSTEM:
            self.assertTrue(name.startswith("libxcb-"), name)

    def test_copied_trees_leave_out_build_only_files_and_skipped_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            source, destination = Path(directory, "qml"), Path(directory, "out")
            for name in ("QtQuick/qmldir", "QtQuick/libqtquick2plugin.so", "QtTest/qmldir", "Qt/test/controls/qmldir",
                         "Qt/labs/qmldir", "QtQuick/objects-RelWithDebInfo/x/init.cpp.o", "QtQuick/libstatic.a"):
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
            build_portable.copy_tree(source, destination, skip=build_portable.QML_SKIP)
            copied = sorted(str(p.relative_to(destination)) for p in destination.rglob("*") if p.is_file())
            self.assertEqual(copied, ["Qt/labs/qmldir", "QtQuick/libqtquick2plugin.so", "QtQuick/qmldir"])

    def test_the_verification_covers_what_a_user_runs(self):
        script = build_portable.VERIFY_SCRIPT
        for check in ("tomaccoctl\" --version", "tomaccoctl\" doctor", "--smoke-test", "xvfb-run", "install.py\" --user-home",
                      "--uninstall"):
            self.assertIn(check, script)
        self.assertEqual(build_portable.VERIFY_IMAGES, ("debian:12-slim", "ubuntu:22.04", "ubuntu:24.04", "fedora:44"))


if __name__ == "__main__":
    unittest.main()


class HostPackageTests(unittest.TestCase):
    """doctor's table of system packages (native/desktop.rs) covers the libraries this build leaves to the host."""

    def test_every_library_in_the_table_is_one_the_package_may_need_from_the_host(self):
        import re as regex
        from pathlib import Path as P
        import build_portable
        table = regex.findall(r'\("([^"]+\.so[^"]*)", "([^"]+)", "([^"]+)"\)',
                              (P(build_portable.ROOT) / "native/desktop.rs").read_text())
        self.assertGreater(len(table), 50)
        for library, debian, fedora in table:
            self.assertRegex(library, build_portable.SYSTEM, library)
        # Each package doctor names is one verify installs too, or the base system or a dependency of one.
        base_apt = {"libc6", "libgcc-s1", "libstdc++6", "zlib1g", "libglx0", "libnspr4"}
        base_dnf = {"glibc", "libgcc", "libstdc++", "zlib-ng-compat", "nss-util", "libbrotli"}
        self.assertLessEqual({row[1] for row in table} - base_apt, set(build_portable.APT_PACKAGES))
        self.assertLessEqual({row[2] for row in table} - base_dnf, set(build_portable.DNF_PACKAGES))
