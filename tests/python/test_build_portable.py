"""tools/build_portable.py without Docker: names, pins and the rules the build applies."""

import contextlib
import io
import re
import tempfile
import unittest
import unittest.mock
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


class Aarch64(unittest.TestCase):
    """The aarch64 portable package: its own builder, pins and limits, the same rules otherwise."""

    def test_names_say_the_architecture(self):
        self.assertEqual(build_portable.package_name("1.2.3", "tomacco", "aarch64"), "tomacco-1.2.3-Linux-aarch64-portable")
        for name, expected in (("tomacco-1.2.3-Linux-aarch64-portable.tar.gz", "aarch64"),
                               ("dist/tomacco-1.2.3-Linux-aarch64.tar.gz", "aarch64"),
                               ("tomacco-1.2.3-Linux-x86_64-portable.tar.gz", "x86_64"), ("other.tar.gz", "x86_64")):
            self.assertEqual(build_portable.architecture_of(name).name, expected, name)
        self.assertEqual(build_portable.ARCHITECTURES["aarch64"].output, "build-portable-aarch64")
        self.assertEqual(build_portable.ARCHITECTURES["x86_64"].output, "build-portable")

    def test_each_architecture_has_its_own_builder_image(self):
        x86 = build_portable.image_tag()
        arm = build_portable.image_tag(architecture="aarch64")
        self.assertRegex(arm, r"^tomacco-portable-builder-aarch64:[0-9a-f]{12}$")
        self.assertRegex(x86, r"^tomacco-portable-builder:[0-9a-f]{12}$")
        self.assertNotEqual(x86.split(":")[1], arm.split(":")[1])

    def test_every_download_of_the_aarch64_builder_is_pinned(self):
        dockerfile = (ROOT / "packaging/portable/Dockerfile.aarch64").read_text()
        self.assertRegex(dockerfile, r"(?m)^FROM ubuntu:24\.04@sha256:[0-9a-f]{64}$")
        downloads = re.findall(r"curl -fsSL[^\n]*-o (\S+)", dockerfile)
        self.assertGreaterEqual(len(downloads), 5)
        for target in downloads:
            if target == '"$file"':
                self.assertIn('echo "$sum  $file" | sha256sum -c -', dockerfile)
            else:
                self.assertIn(f"  {target}\" | sha256sum -c -", dockerfile, target)
        for name in ("RUSTUP_SHA256", "CMAKE_SHA256", "FFMPEG_SHA256", "ICU_LICENSE_SHA256"):
            self.assertRegex(dockerfile, rf"ARG {name}=[0-9a-f]{{64}}\n")
        self.assertIn("linux-aarch64.tar.gz", dockerfile)
        self.assertIn("aarch64-unknown-linux-gnu/rustup-init", dockerfile)
        self.assertIn("qtsdkrepository/linux_arm64/", dockerfile)
        instructions = "\n".join(line for line in dockerfile.splitlines() if not line.startswith("#"))
        self.assertNotIn("x86_64", instructions)

    def test_the_aarch64_builder_matches_the_x86_64_one_where_it_can(self):
        x86 = (ROOT / "packaging/portable/Dockerfile").read_text()
        arm = (ROOT / "packaging/portable/Dockerfile.aarch64").read_text()
        for name in ("RUST_VERSION", "RUSTUP_VERSION", "CMAKE_VERSION", "FFMPEG_VERSION", "FFMPEG_SHA256", "ICU_LICENSE_SHA256"):
            self.assertEqual(re.search(rf"ARG {name}=(\S+)", x86).group(1), re.search(rf"ARG {name}=(\S+)", arm).group(1), name)
        # build.rs compiles the iOS runtime protocol with protoc.
        for text in (x86, arm):
            self.assertIn("protobuf-compiler", text)
        configure = lambda text: text[text.index("./configure"):text.index("&& make", text.index("./configure"))]
        self.assertEqual(configure(x86), configure(arm), "the same LGPL-only FFmpeg")

    def test_the_aarch64_qt_archives_are_the_same_builds_as_the_x86_64_ones(self):
        def modules(name):
            text = (ROOT / "packaging/portable" / name).read_text()
            lines = [line for line in text.splitlines() if line and not line.startswith("#")]
            for line in lines:
                self.assertRegex(line, r"^[0-9a-f]{64}  (desktop|extensions)/\S+\.7z$")
            return {re.search(r"/(6\.12\.0-0-\d{12})(icu|qt[a-z]+)-", line).group(2):
                    re.search(r"/(6\.12\.0-0-\d{12})", line).group(1) for line in lines}
        x86, arm = modules("qt-archives.txt"), modules("qt-archives-aarch64.txt")
        self.assertEqual(x86, arm, "qt-sources.txt is the source of both")
        self.assertTrue(all("AARCH64" in line or "arm64" in line
                            for line in (ROOT / "packaging/portable/qt-archives-aarch64.txt").read_text().splitlines()
                            if line and not line.startswith("#")))

    def test_the_aarch64_package_is_checked_where_its_glibc_is(self):
        arm = build_portable.ARCHITECTURES["aarch64"]
        self.assertEqual(arm.glibc, (2, 39))
        self.assertEqual(arm.platform, "linux/arm64")
        self.assertEqual(arm.system_library_directory, Path("/usr/lib/aarch64-linux-gnu"))
        # Qt's aarch64 binaries need glibc 2.38: Debian 12 (2.36) and Ubuntu 22.04 (2.35) cannot run them.
        self.assertFalse({"debian:12-slim", "ubuntu:22.04"} & set(arm.images))
        self.assertIn("ubuntu:24.04", arm.images)
        self.assertTrue(build_portable.SYSTEM.match("ld-linux-aarch64.so.1"))

    def test_the_symbol_limits_are_the_architectures(self):
        with tempfile.TemporaryDirectory() as directory:
            top = Path(directory)
            (top / "lib").mkdir()
            (top / "lib/libQt6Qml.so.6").write_bytes(b"\x7fELF")
            found = {"GLIBC": {(2, 38)}, "GLIBCXX": {(3, 4, 32)}}
            with unittest.mock.patch.object(build_portable, "symbol_versions", lambda path, family: found[family]):
                with self.assertRaisesRegex(build_portable.Failed, "glibc 2.35:\n  lib/libQt6Qml.so.6 needs GLIBC_2.38"):
                    build_portable.check(top, [])
                self.assertEqual(build_portable.check(top, [], build_portable.ARCHITECTURES["aarch64"]),
                                 {"GLIBC": "2.38", "GLIBCXX": "3.4.32"})

    def test_the_notice_names_the_aarch64_builder_and_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            ffmpeg = Path(directory)
            (ffmpeg / "lib/pkgconfig").mkdir(parents=True)
            (ffmpeg / "share/licenses").mkdir(parents=True)
            (ffmpeg / "lib/pkgconfig/libavcodec.pc").write_text("Version: 62.1.100\n")
            (ffmpeg / "share/licenses/config.mak").write_text("FFMPEG_CONFIGURATION=--disable-everything\n")
            text = build_portable.notice("1.2.3", ["libc.so.6"], {"GLIBC": "2.38"}, ffmpeg=ffmpeg,
                                         architecture=build_portable.ARCHITECTURES["aarch64"])
        self.assertIn("for aarch64", text.splitlines()[0])
        self.assertIn("built on Ubuntu 24.04", text)
        self.assertIn("packaging/portable/Dockerfile.aarch64", text)
        self.assertIn("qtsdkrepository/linux_arm64/", text)
        self.assertIn("glibc 2.38 or newer", text)
        self.assertNotIn("Ubuntu 22.04", text)

    def test_build_and_verify_ask_docker_for_linux_arm64(self):
        calls = []

        def fake_run(argv, **options):
            calls.append([str(a) for a in argv])
            if "inside" in [str(a) for a in argv]:
                output = Path(next(a.split(":")[0] for a in map(str, argv) if a.endswith(":/out")))
                (output / "tomacco-1.2.3-Linux-aarch64-portable.tar.gz").write_bytes(b"x")

        class Done:
            returncode, stdout = 1, ""

        with tempfile.TemporaryDirectory() as directory, \
                unittest.mock.patch.object(build_portable, "run", fake_run), \
                unittest.mock.patch.object(build_portable, "export_source"), \
                unittest.mock.patch.object(build_portable, "source_commit", return_value="c"), \
                unittest.mock.patch.object(build_portable, "project_version", return_value="1.2.3"), \
                unittest.mock.patch.object(build_portable, "host_architecture", return_value="x86_64"), \
                unittest.mock.patch.object(build_portable.subprocess, "run", return_value=Done()), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            build_portable.main(["build", "--arch", "aarch64", "--output", directory])
        image = calls[0]
        self.assertEqual(image[:6], ["docker", "buildx", "build", "--platform", "linux/arm64", "--load"])
        self.assertEqual(Path(image[image.index("--file") + 1]).name, "Dockerfile.aarch64")
        self.assertRegex(image[image.index("--tag") + 1], r"^tomacco-portable-builder-aarch64:")
        container = calls[1]
        self.assertEqual(container[3:5], ["--platform", "linux/arm64"])
        self.assertEqual(container[-2:], ["--arch", "aarch64"])
        self.assertIn("under emulation", output.getvalue())

        seen = []

        def fake_subprocess(argv, stdout=None, **options):
            seen.append(argv)
            stdout.write("== Ubuntu 24.04\nRESULT packages ok\n")
            return type("Done", (), {"returncode": 0})()

        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory, "tomacco-1.2.3-Linux-aarch64-portable.tar.gz")
            package.write_bytes(b"x")
            with unittest.mock.patch.object(build_portable.subprocess, "run", fake_subprocess), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(build_portable.main(["verify", str(package)]), 0)
        self.assertEqual([argv[argv.index("--platform") + 1] for argv in seen], ["linux/arm64"] * 3)
        self.assertEqual([argv[argv.index("--platform") + 4] for argv in seen], list(build_portable.ARCHITECTURES["aarch64"].images))


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
