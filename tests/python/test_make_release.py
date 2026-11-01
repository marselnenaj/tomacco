"""tools/make_release.py with fake commands: nothing is built, signed or published."""

import base64
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import make_release

ROOT = Path(__file__).resolve().parents[2]
RAW = b"\x11" * 32
PUBLIC = base64.b64encode(RAW).decode()
METAINFO = '<component><releases><release version="1.2.3"/></releases></component>'
CRATE = "registry+https://github.com/rust-lang/crates.io-index#serde@1.0.0"
METADATA = {"packages": [{"id": "path+file:///x#x@1.2.3", "name": "x", "version": "1.2.3", "license": "GPL-2.0-or-later",
                          "manifest_path": "/x/Cargo.toml"},
                         {"id": CRATE, "name": "serde", "version": "1.0.0", "license": "MIT OR Apache-2.0",
                          "manifest_path": "/nowhere/serde-1.0.0/Cargo.toml"}],
            "workspace_members": ["path+file:///x#x@1.2.3"],
            "resolve": {"nodes": [{"id": "path+file:///x#x@1.2.3", "deps": [{"pkg": CRATE, "dep_kinds": [{"kind": None}]}]},
                                  {"id": CRATE, "deps": []}]}}


class Fake(make_release.Commands):
    """Answers the read-only questions, and plays the build for `run`."""

    def __init__(self, root, dry_run=False, status=b"", tagged=b"abc\n", tags=b"v1.2.3\n"):
        super().__init__(root, dry_run)
        self.status, self.tagged, self.tags = status, tagged, tags
        self.reads, self.runs = [], []

    def read(self, argv):
        self.reads.append(argv)
        if argv[:2] == ["git", "status"]:
            return self.status
        if argv[:2] == ["git", "tag"]:
            return self.tags
        if argv[:2] == ["git", "rev-parse"]:
            return b"abc\n" if argv[2] == "HEAD" else self.tagged
        if argv[:2] == ["openssl", "pkey"]:
            return b"prefix" + RAW
        if argv[:2] == ["git", "log"]:
            return b"1700000000\n"
        if argv[:2] == ["cargo", "metadata"]:
            return json.dumps(METADATA).encode()
        if argv[:2] == ["cargo", "tree"]:
            return b"x v1.2.3 (/x)\nserde v1.0.0\n"
        if argv[0] == "ssh" and argv[-1].endswith("uname -m"):
            return b"aarch64\n"
        return b""

    def run(self, argv):
        self.runs.append(argv)
        if self.dry_run:
            return
        if argv[:3] == ["cmake", "--build", make_release.BUILD] and argv[-1] == "package":
            build = self.root / make_release.BUILD
            build.mkdir()
            package = build / "tomacco-1.2.3-Linux-x86_64.tar.gz"
            package.write_bytes(b"package")
            package.with_name(package.name + ".sha256").write_text(hashlib.sha256(b"package").hexdigest() + "  " + package.name + "\n")
        if "tools/build_portable.py" in argv and "build" in argv:
            output = self.root / argv[argv.index("--output") + 1]
            output.mkdir(parents=True)
            (output / "work").mkdir()
            architecture = argv[argv.index("--arch") + 1] if "--arch" in argv else "x86_64"
            package = output / f"tomacco-1.2.3-Linux-{architecture}-portable.tar.gz"
            package.write_bytes(b"portable " + architecture.encode())
            package.with_name(package.name + ".sha256").write_text(
                hashlib.sha256(package.read_bytes()).hexdigest() + "  " + package.name + "\n")
        if argv[0] == "scp" and argv[-1] == f"{self.root / make_release.ARM_BUILD}/":
            # Fetching from the ARM build host.
            package = self.root / make_release.ARM_BUILD / "tomacco-1.2.3-Linux-aarch64.tar.gz"
            package.write_bytes(b"package aarch64")
            package.with_name(package.name + ".sha256").write_text(hashlib.sha256(b"package aarch64").hexdigest() + "  " + package.name + "\n")
        if "tools/build_source_release.py" in argv:
            dist = self.root / make_release.DIST
            dist.mkdir(exist_ok=True)
            source = dist / "tomacco-1.2.3-source.tar.gz"
            source.write_bytes(b"source")
            source.with_name(source.name + ".sha256").write_text(hashlib.sha256(b"source").hexdigest() + "  " + source.name + "\n")
        if argv[:3] == ["openssl", "pkeyutl", "-sign"]:
            Path(argv[argv.index("-out") + 1]).write_bytes(b"\x22" * 64)
        if argv[0] == "curl":
            Path(argv[argv.index("-o") + 1]).write_bytes(b"source of " + argv[-1].rpartition("/")[2].encode())


class MakeRelease(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        self.root = base / "repository"
        (self.root / "native").mkdir(parents=True)
        (self.root / "assets").mkdir()
        (self.root / "CMakeLists.txt").write_text("project(Tomacco VERSION 1.2.3 LANGUAGES CXX)\n")
        (self.root / "Cargo.toml").write_text('[package]\nname = "x"\nversion = "1.2.3"\n')
        (self.root / "assets/x.metainfo.xml").write_text(METAINFO)
        (self.root / "Cargo.lock").write_text('[[package]]\nname = "serde"\nversion = "1.0.0"\nchecksum = "' + "ab" * 32 + '"\n')
        (self.root / "packaging/portable").mkdir(parents=True)
        for name in ("qt-archives.txt", "Dockerfile", "qt-archives-aarch64.txt", "Dockerfile.aarch64"):
            shutil.copy2(ROOT / "packaging/portable" / name, self.root / "packaging/portable" / name)
        self.product(PUBLIC)
        self.key = base / "outside.pem"
        self.key.write_text("private")
        self.key.chmod(0o600)

    def product(self, public, upcoming=None):
        (self.root / "native/product.rs").write_text(
            'pub const PRODUCT_NAME: &str = "Tomacco";\npub const REPO_OWNER: &str = "owner";\npub const REPO_NAME: &str = "repo";\n'
            f'pub const PACKAGE_PREFIX: &str = "tomacco";\npub const RELEASE_SIGNING_PUBLIC_KEY_BASE64: &str = "{public}";\n'
            + (f'pub const NEXT_RELEASE_SIGNING_PUBLIC_KEY_BASE64: &str = "{upcoming}";\n' if upcoming is not None else ""))

    def make(self, *arguments, commands=None, environment=None):
        commands = commands or Fake(self.root)
        output, errors = io.StringIO(), io.StringIO()
        arguments = list(arguments)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = make_release.main(arguments, commands, self.root, environment if environment is not None else {})
        return code, commands, errors.getvalue(), output.getvalue()

    def assertRefused(self, reason, *arguments, **options):
        code, commands, errors, _ = self.make(*arguments, **options)
        self.assertEqual(code, 1, errors)
        self.assertIn(reason, errors)
        self.assertFalse(commands.runs, "Nothing runs after a refusal")

    def test_a_release_runs_the_checks_then_signs_then_makes_a_draft(self):
        code, commands, errors, _ = self.make("--key", str(self.key))
        self.assertEqual(code, 0, errors)
        words = [" ".join(argv) for argv in commands.runs]
        order = ["build_release_notes.py", "cargo fmt --check", "cargo clippy", "check_dependencies.py", "ctest", "--target package",
                 "build_portable.py build", "build_portable.py verify build-portable/tomacco-1.2.3-Linux-x86_64-portable.tar.gz",
                 "build_source_release.py", "pkeyutl -sign", "gh release create"]
        positions = [next(i for i, line in enumerate(words) if word in line) for word in order]
        self.assertEqual(positions, sorted(positions), words)
        release = commands.runs[-1]
        self.assertEqual(release[:4], ["gh", "release", "create", "v1.2.3"])
        for flag in ("--draft", "--verify-tag"):
            self.assertIn(flag, release)
        self.assertEqual(release[release.index("--repo") + 1], "owner/repo")
        self.assertEqual(release[release.index("--title") + 1], "Tomacco 1.2.3")
        names = [Path(part).name for part in release if part.startswith("dist/")]
        self.assertEqual(sorted(names), sorted(path.name for path in (self.root / "dist").iterdir()))
        signature = self.root / "dist/tomacco-1.2.3-Linux-x86_64.tar.gz.sig"
        self.assertEqual(base64.b64decode(signature.read_bytes()), b"\x22" * 64)
        self.assertIn(str(signature.relative_to(self.root)), release)
        # The portable package is released beside it: checksum, signature, and nothing of its build tree.
        regular, portable = "tomacco-1.2.3-Linux-x86_64.tar.gz", "tomacco-1.2.3-Linux-x86_64-portable.tar.gz"
        self.assertEqual(sorted(names), sorted([
            regular, f"{regular}.sha256", f"{regular}.sig", f"{regular}.cdx.json", f"{regular}.cdx.json.sig",
            portable, f"{portable}.sha256", f"{portable}.sig", f"{portable}.cdx.json", f"{portable}.cdx.json.sig",
            "tomacco-1.2.3-source.tar.gz", "tomacco-1.2.3-source.tar.gz.sha256",
            "tomacco-1.2.3-provenance.intoto.json", "tomacco-1.2.3-provenance.intoto.json.sig"]))
        signed = [argv[argv.index("-in") + 1] for argv in commands.runs if argv[:3] == ["openssl", "pkeyutl", "-sign"]]
        self.assertEqual([Path(path).name for path in signed],
                         [regular, portable, f"{regular}.cdx.json", f"{portable}.cdx.json", "tomacco-1.2.3-provenance.intoto.json"])
        # The provenance lists every file the release offers but checksums and signatures, with its digest.
        statement = json.loads((self.root / "dist/tomacco-1.2.3-provenance.intoto.json").read_text())
        self.assertEqual(statement["_type"], "https://in-toto.io/Statement/v1")
        self.assertEqual(statement["predicateType"], "https://slsa.dev/provenance/v1")
        listed = {subject["name"]: subject["digest"]["sha256"] for subject in statement["subject"]}
        self.assertEqual(sorted(listed), sorted([regular, portable, f"{regular}.cdx.json", f"{portable}.cdx.json",
                                                 "tomacco-1.2.3-source.tar.gz"]))
        for name, digest in listed.items():
            self.assertEqual(digest, hashlib.sha256((self.root / "dist" / name).read_bytes()).hexdigest())
        definition = statement["predicate"]["buildDefinition"]
        self.assertEqual(definition["externalParameters"]["ref"], "refs/tags/v1.2.3")
        self.assertEqual(definition["externalParameters"]["portable"], True)
        self.assertEqual(definition["internalParameters"], {"SOURCE_DATE_EPOCH": "1700000000"})
        resolved = definition["resolvedDependencies"]
        self.assertEqual(resolved[0]["digest"], {"gitCommit": "abc"})
        self.assertEqual(resolved[1], {"name": "Cargo.lock", "digest": {"sha256": hashlib.sha256((self.root / "Cargo.lock").read_bytes()).hexdigest()}})
        uris = [entry.get("uri", "") for entry in resolved]
        self.assertTrue(any(uri.endswith("qtbase-Linux-RHEL_9_6-GCC-Linux-RHEL_9_6-X86_64.7z") for uri in uris), uris)
        self.assertTrue(any(uri.startswith("https://ffmpeg.org/releases/ffmpeg-") for uri in uris), uris)
        builder = statement["predicate"]["runDetails"]["builder"]["version"]
        self.assertIn("python", builder)
        # Each SBOM describes its own package; only the portable one lists Qt and FFmpeg.
        for name, bundles in ((regular, False), (portable, True)):
            sbom = json.loads((self.root / "dist" / f"{name}.cdx.json").read_text())
            self.assertEqual(sbom["bomFormat"], "CycloneDX")
            self.assertEqual(sbom["metadata"]["component"]["hashes"][0]["content"],
                             hashlib.sha256((self.root / "dist" / name).read_bytes()).hexdigest())
            self.assertEqual(sbom["metadata"]["timestamp"], "2023-11-14T22:13:20Z")
            names_in = {component["name"] for component in sbom["components"]}
            self.assertIn("serde", names_in)
            self.assertEqual({"qtbase", "FFmpeg"} <= names_in, bundles)

    def test_an_arm_host_builds_the_aarch64_package_from_the_source_tarball(self):
        code, commands, errors, _ = self.make("--key", str(self.key), "--arm-host", "builder@arm",
                                              "--arm-path", "~/.cache/tomacco-buildtools/bin")
        self.assertEqual(code, 0, errors)
        words = [" ".join(argv) for argv in commands.runs]
        source = hashlib.sha256(b"source").hexdigest()
        remote = [argv for argv in commands.runs if argv[0] in ("ssh", "scp")]
        self.assertTrue(all(argv[1:3] == ["-o", "BatchMode=yes"] for argv in remote), remote)
        scripts = [argv[-1] for argv in remote if argv[0] == "ssh"]
        for script in scripts:
            self.assertTrue(script.startswith('export PATH="$HOME"/.cache/tomacco-buildtools/bin:"$PATH" && '
                                              "export SOURCE_DATE_EPOCH=1700000000 && "), script)
        order = ["build_source_release.py", 'test "$(uname -m)" = aarch64', "rm -rf -- tomacco-build-release-1.2.3",
                 "scp -o BatchMode=yes -q " + str(self.root / "dist/tomacco-1.2.3-source.tar.gz") + " builder@arm:tomacco-build-release-1.2.3/",
                 f"printf '%s  %s\\n' {source} tomacco-1.2.3-source.tar.gz | sha256sum -c -",
                 "cmake -S . -B build-product -G Ninja -DCMAKE_BUILD_TYPE=Release", "cmake --build build-product",
                 "ctest --test-dir build-product --output-on-failure --no-tests=error -R '^(backend|desktop-ui|native-display)$'",
                 "cmake --build build-product --target package",
                 "builder@arm:tomacco-build-release-1.2.3/tomacco-1.2.3-source/build-product/tomacco-1.2.3-Linux-aarch64.tar.gz.sha256",
                 "pkeyutl -sign", "gh release create"]
        position = 0
        for word in order:  # each after the one before
            position = next((i for i in range(position, len(words)) if word in words[i]), None)
            self.assertIsNotNone(position, f"{word} in order: {words}")
        self.assertIn("cd -- tomacco-build-release-1.2.3/tomacco-1.2.3-source", scripts[2])
        arm = "tomacco-1.2.3-Linux-aarch64.tar.gz"
        dist = self.root / "dist"
        for name in (arm, f"{arm}.sha256", f"{arm}.sig", f"{arm}.cdx.json", f"{arm}.cdx.json.sig"):
            self.assertTrue((dist / name).is_file(), name)
            self.assertIn(f"dist/{name}", commands.runs[-1])
        self.assertFalse(any((self.root / make_release.ARM_BUILD).iterdir()), "the package moved to dist/")
        signed = [Path(argv[argv.index("-in") + 1]).name for argv in commands.runs if argv[:3] == ["openssl", "pkeyutl", "-sign"]]
        self.assertIn(arm, signed)
        self.assertIn(f"{arm}.cdx.json", signed)
        sbom = json.loads((dist / f"{arm}.cdx.json").read_text())
        self.assertEqual(sbom["metadata"]["component"]["hashes"][0]["content"], hashlib.sha256(b"package aarch64").hexdigest())
        self.assertNotIn("qtbase", {component["name"] for component in sbom["components"]})
        statement = json.loads((dist / "tomacco-1.2.3-provenance.intoto.json").read_text())
        self.assertIn(arm, {subject["name"] for subject in statement["subject"]})
        self.assertTrue(statement["predicate"]["buildDefinition"]["externalParameters"]["aarch64_package"])
        builder = statement["predicate"]["runDetails"]["builder"]["version"]
        self.assertEqual(builder["aarch64-host machine"], "aarch64")
        self.assertNotIn("builder@arm", json.dumps(statement), "the provenance does not name the host")

    def test_the_arm_host_can_come_from_the_environment(self):
        code, commands, errors, _ = self.make("--key", str(self.key), environment={
            make_release.ARM_HOST_ENVIRONMENT: "arm", make_release.ARM_PATH_ENVIRONMENT: "/opt/tools/bin"})
        self.assertEqual(code, 0, errors)
        scripts = [argv[-1] for argv in commands.runs if argv[0] == "ssh"]
        self.assertTrue(scripts)
        self.assertTrue(all(script.startswith('export PATH=/opt/tools/bin:"$PATH"') for script in scripts))
        self.assertTrue(all(argv[3] == "arm" for argv in commands.runs if argv[0] == "ssh"))
        # Without either, nothing goes to a host.
        shutil.rmtree(self.root / "dist")
        for directory in (make_release.BUILD, make_release.PORTABLE, make_release.ARM_BUILD):
            shutil.rmtree(self.root / directory, ignore_errors=True)
        code, commands, errors, _ = self.make("--key", str(self.key))
        self.assertEqual(code, 0, errors)
        self.assertFalse(any(argv[0] in ("ssh", "scp") for argv in commands.runs))
        self.assertFalse(any("aarch64" in path.name for path in (self.root / "dist").iterdir()))

    def test_the_arm_tests_and_directory_can_be_chosen(self):
        code, commands, errors, _ = self.make("--key", str(self.key), "--arm-host", "arm", "--arm-tests", "^backend$",
                                              "--arm-directory", "tomacco-build-x")
        self.assertEqual(code, 0, errors)
        words = " ".join(argv[-1] for argv in commands.runs if argv[0] == "ssh")
        self.assertIn("-R '^backend$'", words)
        self.assertIn("cd -- tomacco-build-x/tomacco-1.2.3-source", words)

    def test_unsafe_arm_hosts_and_directories_are_refused(self):
        self.assertRefused("Not an SSH host name", "--key", str(self.key), "--arm-host", "-oProxyCommand=touch x")
        self.assertRefused("Not an SSH host name", "--key", str(self.key), "--arm-host", "arm; rm -rf ~")
        for directory in ("../outside", "/tmp/x", "a b", "~/x", "a/../../b"):
            code, commands, errors, _ = self.make("--key", str(self.key), "--arm-host", "arm", "--arm-directory", directory)
            self.assertEqual(code, 1, directory)
            self.assertIn("plain path below the remote home", errors)
            self.assertFalse(any(argv[0] in ("ssh", "scp") for argv in commands.runs))
            for name in ("dist", make_release.BUILD, make_release.PORTABLE):
                shutil.rmtree(self.root / name, ignore_errors=True)

    def test_an_aarch64_package_that_does_not_match_its_checksum_is_refused(self):
        class Corrupting(Fake):
            def run(self, argv):
                super().run(argv)
                if argv[0] == "scp" and argv[-1].endswith(make_release.ARM_BUILD + "/"):
                    (self.root / make_release.ARM_BUILD / "tomacco-1.2.3-Linux-aarch64.tar.gz").write_bytes(b"changed")
        code, commands, errors, _ = self.make("--key", str(self.key), "--arm-host", "arm", commands=Corrupting(self.root))
        self.assertEqual(code, 1)
        self.assertIn("tomacco-1.2.3-Linux-aarch64.tar.gz does not match its checksum", errors)
        self.assertFalse(any(argv[0] == "gh" for argv in commands.runs))
        self.assertFalse(any(argv[:3] == ["openssl", "pkeyutl", "-sign"] for argv in commands.runs))

    def test_a_source_tarball_changed_before_the_copy_is_refused(self):
        class Corrupting(Fake):
            def run(self, argv):
                super().run(argv)
                if "tools/build_source_release.py" in argv:
                    (self.root / make_release.DIST / "tomacco-1.2.3-source.tar.gz").write_bytes(b"changed")
        code, commands, errors, _ = self.make("--key", str(self.key), "--arm-host", "arm", commands=Corrupting(self.root))
        self.assertEqual(code, 1)
        self.assertIn("tomacco-1.2.3-source.tar.gz does not match its checksum", errors)
        self.assertFalse(any(argv[0] in ("ssh", "scp") for argv in commands.runs))

    def test_a_dry_run_prints_the_arm_steps_and_touches_nothing(self):
        before = sorted(path.name for path in self.root.iterdir())
        code, commands, errors, output = self.make("--dry-run", "--key", str(self.key), "--arm-host", "arm", "--arm-portable",
                                                   commands=Fake(self.root, dry_run=True))
        self.assertEqual(code, 0, errors)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), before)
        words = [" ".join(argv) for argv in commands.runs]
        for step in ("sha256sum -c -", "cmake --build build-product --target package", "ctest --test-dir build-product",
                     "build_portable.py build --arch aarch64 --output build-portable-aarch64",
                     "build_portable.py verify build-portable-aarch64/tomacco-1.2.3-Linux-aarch64-portable.tar.gz"):
            self.assertTrue(any(step in line for line in words), step)
        self.assertTrue(any(argv[0] == "scp" for argv in commands.runs))
        self.assertIn("build-aarch64/tomacco-1.2.3-Linux-aarch64.tar.gz and its checksum", output)
        self.assertIn("build-portable-aarch64/tomacco-1.2.3-Linux-aarch64-portable.tar.gz", output)

    def test_the_arm_portable_package_is_built_checked_signed_and_described(self):
        code, commands, errors, _ = self.make("--key", str(self.key), "--arm-portable")
        self.assertEqual(code, 0, errors)
        words = [" ".join(argv) for argv in commands.runs]
        order = ["build_portable.py build --output build-portable", "build_portable.py verify build-portable/",
                 "build_portable.py build --arch aarch64 --output build-portable-aarch64",
                 "build_portable.py verify build-portable-aarch64/tomacco-1.2.3-Linux-aarch64-portable.tar.gz",
                 "build_source_release.py", "pkeyutl -sign", "gh release create"]
        positions = [next(i for i, line in enumerate(words) if word in line) for word in order]
        self.assertEqual(positions, sorted(positions), words)
        arm = "tomacco-1.2.3-Linux-aarch64-portable.tar.gz"
        for name in (arm, f"{arm}.sha256", f"{arm}.sig", f"{arm}.cdx.json", f"{arm}.cdx.json.sig"):
            self.assertTrue((self.root / "dist" / name).is_file(), name)
        sbom = json.loads((self.root / "dist" / f"{arm}.cdx.json").read_text())
        qtbase = next(component for component in sbom["components"] if component["name"] == "qtbase")
        self.assertIn("/linux_arm64/", qtbase["externalReferences"][0]["url"])
        self.assertIn("ubuntu-24.04:libxcb-cursor", {component["bom-ref"] for component in sbom["components"]})
        statement = json.loads((self.root / "dist/tomacco-1.2.3-provenance.intoto.json").read_text())
        definition = statement["predicate"]["buildDefinition"]
        self.assertTrue(definition["externalParameters"]["portable"])
        self.assertTrue(definition["externalParameters"]["aarch64_portable"])
        names = [entry.get("name", "") for entry in definition["resolvedDependencies"]]
        self.assertIn("packaging/portable/Dockerfile", names)
        self.assertIn("packaging/portable/Dockerfile.aarch64", names)
        uris = " ".join(entry.get("uri", "") for entry in definition["resolvedDependencies"])
        self.assertIn("AARCH64.7z", uris)
        self.assertIn("cmake-4.4.5-linux-aarch64.tar.gz", uris)
        self.assertIn("docker://ubuntu:24.04", uris)

    def test_qt_sources_go_with_the_arm_portable_package_alone(self):
        self.qt_sources("qtbase.tar.xz")
        code, commands, errors, _ = self.make("--key", str(self.key), "--no-portable", "--arm-portable", "--with-qt-sources")
        self.assertEqual(code, 0, errors)
        self.assertTrue((self.root / "dist/qtbase.tar.xz").is_file())
        self.assertFalse((self.root / "dist/tomacco-1.2.3-Linux-x86_64-portable.tar.gz").exists())

    def test_the_release_and_the_portable_build_agree_on_the_aarch64_name(self):
        import build_portable
        self.assertEqual(build_portable.package_name("1.2.3", "tomacco", "aarch64") + ".tar.gz",
                         make_release.portable_name("tomacco", "1.2.3", "aarch64"))
        self.assertEqual(make_release.PORTABLE_ARM, build_portable.ARCHITECTURES["aarch64"].output)

    def test_remote_paths_expand_the_remote_home_and_quote_the_rest(self):
        self.assertEqual(make_release.remote_path("~/a b/bin:/opt/x"), '"$HOME"/\'a b/bin\':/opt/x')
        self.assertEqual(make_release.remote_script(None, "1", "true"), "export SOURCE_DATE_EPOCH=1 && true")
        self.assertEqual(make_release.remote_script(None, None, "a", "b"), "a && b")

    @unittest.skipUnless(shutil.which("sh") and shutil.which("sha256sum") and shutil.which("tar"), "needs sh, sha256sum, tar")
    def test_the_remote_commands_run_in_a_real_shell(self):
        """The ssh and scp steps run here, in a scratch home, with stand-ins for uname, cmake and ctest on --arm-path."""
        home = self.root.parent / "home"
        tools = home / "tools"
        tools.mkdir(parents=True)
        log = home / "log"
        stand_ins = {
            "uname": "#!/bin/sh\necho aarch64\n",
            "ctest": f"#!/bin/sh\necho \"ctest $*\" >> {log}\n",
            "cmake": f"""#!/bin/sh
echo "cmake $* SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH" >> {log}
case "$*" in *"--target package"*)
    cd build-product && printf 'aarch64 package' > tomacco-1.2.3-Linux-aarch64.tar.gz &&
    sha256sum tomacco-1.2.3-Linux-aarch64.tar.gz > tomacco-1.2.3-Linux-aarch64.tar.gz.sha256 ;;
*"-B build-product"*) mkdir -p build-product ;;
esac
""",
        }
        for name, text in stand_ins.items():
            (tools / name).write_text(text)
            (tools / name).chmod(0o755)

        class Local(Fake):
            """ssh HOST SCRIPT runs SCRIPT in `home`; scp copies, HOST: meaning `home`."""

            def run(self, argv):
                self.runs.append(argv)
                if argv[0] == "ssh":
                    environment = {**os.environ, "HOME": str(home)}
                    if subprocess.run(["sh", "-c", argv[-1]], cwd=home, env=environment, capture_output=True).returncode:
                        raise make_release.Refused(f"{argv[-1]} failed")
                elif argv[0] == "scp":
                    *sources, target = [part.replace("arm:", f"{home}/") for part in argv[4:]]
                    for source in sources:
                        shutil.copy(source, target)
                else:
                    super().run(argv)

        dist = self.root / "dist"
        dist.mkdir()
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory, "tomacco-1.2.3-source")
            tree.mkdir()
            (tree / "CMakeLists.txt").write_text("project(x)\n")
            subprocess.run(["tar", "-czf", str(dist / "tomacco-1.2.3-source.tar.gz"), "-C", directory, tree.name], check=True)
        digest = hashlib.sha256((dist / "tomacco-1.2.3-source.tar.gz").read_bytes()).hexdigest()
        (dist / "tomacco-1.2.3-source.tar.gz.sha256").write_text(f"{digest}  tomacco-1.2.3-source.tar.gz\n")
        commands = Local(self.root)
        commands.environment["SOURCE_DATE_EPOCH"] = "1700000000"
        package = make_release.build_on_arm_host(commands, self.root, "arm", "tomacco", "1.2.3", f"{tools}:/usr/bin:/bin")
        fetched = self.root / make_release.ARM_BUILD / package
        self.assertEqual(fetched.read_bytes(), b"aarch64 package")
        self.assertTrue(fetched.with_name(package + ".sha256").is_file())
        lines = log.read_text().splitlines()
        self.assertEqual(lines[0], "cmake -S . -B build-product -G Ninja -DCMAKE_BUILD_TYPE=Release SOURCE_DATE_EPOCH=1700000000")
        self.assertIn("ctest --test-dir build-product --output-on-failure --no-tests=error -R ^(backend|desktop-ui|native-display)$", lines)
        self.assertEqual(lines[-1], "cmake --build build-product --target package SOURCE_DATE_EPOCH=1700000000")
        # A tarball that does not match on the host stops there.
        (dist / "tomacco-1.2.3-source.tar.gz.sha256").write_text("0" * 64 + "  tomacco-1.2.3-source.tar.gz\n")
        with self.assertRaises(make_release.Refused):
            make_release.build_on_arm_host(commands, self.root, "arm", "tomacco", "1.2.3", str(tools))
        (dist / "tomacco-1.2.3-source.tar.gz.sha256").write_text(f"{digest}  tomacco-1.2.3-source.tar.gz\n")
        (home / "tomacco-build-release-1.2.3").mkdir(exist_ok=True)
        with unittest.mock.patch.object(make_release, "source_digest", return_value="0" * 64):
            with self.assertRaisesRegex(make_release.Refused, "sha256sum -c"):
                make_release.build_on_arm_host(commands, self.root, "arm", "tomacco", "1.2.3", f"{tools}:/usr/bin:/bin")
        # A host that is not aarch64 stops before anything is copied.
        (tools / "uname").write_text("#!/bin/sh\necho x86_64\n")
        commands.runs.clear()
        with self.assertRaises(make_release.Refused):
            make_release.build_on_arm_host(commands, self.root, "arm", "tomacco", "1.2.3", f"{tools}:/usr/bin:/bin")
        self.assertEqual([argv[0] for argv in commands.runs], ["ssh"])

    def qt_sources(self, *names, corrupt=None):
        lines = ["# comment"]
        for name in names:
            content = b"source of " + name.encode()
            lines.append(f"{hashlib.sha256(b'other' if name == corrupt else content).hexdigest()}  https://download.example/{name}")
        (self.root / "packaging/portable").mkdir(parents=True, exist_ok=True)
        (self.root / make_release.QT_SOURCES).write_text("\n".join(lines) + "\n")

    def test_with_qt_sources_mirrors_each_tarball_with_its_checksum(self):
        self.qt_sources("qtbase-everywhere-src-6.12.0.tar.xz", "qtwebengine-src.tar.xz")
        code, commands, errors, _ = self.make("--key", str(self.key), "--with-qt-sources")
        self.assertEqual(code, 0, errors)
        release = commands.runs[-1]
        names = [Path(part).name for part in release if part.startswith("dist/")]
        for name in ("qtbase-everywhere-src-6.12.0.tar.xz", "qtwebengine-src.tar.xz"):
            self.assertIn(name, names)
            self.assertIn(name + ".sha256", names)
            self.assertNotIn(name + ".sig", names)
        downloads = [argv for argv in commands.runs if argv[0] == "curl"]
        self.assertEqual([argv[-1] for argv in downloads], ["https://download.example/qtbase-everywhere-src-6.12.0.tar.xz",
                                                           "https://download.example/qtwebengine-src.tar.xz"])
        # Without the flag, none.
        shutil.rmtree(self.root / "dist")
        shutil.rmtree(self.root / make_release.BUILD)
        shutil.rmtree(self.root / make_release.PORTABLE)
        code, commands, errors, _ = self.make("--key", str(self.key))
        self.assertEqual(code, 0, errors)
        self.assertFalse(any(argv[0] == "curl" for argv in commands.runs))

    def test_a_qt_source_that_does_not_match_its_pin_is_refused(self):
        self.qt_sources("qtbase.tar.xz", "qtsvg.tar.xz", corrupt="qtsvg.tar.xz")
        code, commands, errors, _ = self.make("--key", str(self.key), "--with-qt-sources")
        self.assertEqual(code, 1)
        self.assertIn("qtsvg.tar.xz from https://download.example/qtsvg.tar.xz does not match", errors)
        self.assertFalse((self.root / "dist/qtsvg.tar.xz").exists())
        self.assertFalse(any(argv[0] == "gh" for argv in commands.runs))

    def test_qt_sources_need_the_portable_package_and_the_list(self):
        self.assertRefused("leave out --no-portable", "--key", str(self.key), "--with-qt-sources", "--no-portable")
        self.assertRefused("There is no packaging/portable/qt-sources.txt", "--key", str(self.key), "--with-qt-sources")

    def test_the_qt_source_list_covers_every_bundled_module(self):
        # Each module of qt-archives.txt has its source pinned, by name.
        sources = make_release.qt_sources(ROOT)
        archives = (ROOT / "packaging/portable/qt-archives.txt").read_text()
        modules = sorted(set(__import__("re").findall(r"\d{12}(qt[a-z]+)-Linux", archives)))
        self.assertEqual(len(modules), 8, modules)
        for module in modules:
            self.assertTrue(any(f"{module}-everywhere-src-6.12.0" in url for _, url in sources), module)
        self.assertTrue(all(url.startswith("https://download.qt.io/") for _, url in sources))

    def test_the_builds_take_their_time_from_the_release_commit(self):
        code, commands, errors, _ = self.make("--key", str(self.key))
        self.assertEqual(code, 0, errors)
        self.assertEqual(commands.environment, {"SOURCE_DATE_EPOCH": "1700000000"})
        self.assertIn(["git", "log", "-1", "--format=%ct", "HEAD"], commands.reads)
        # A real run hands it to every command it starts.
        real = make_release.Commands(self.root)
        real.environment = {"SOURCE_DATE_EPOCH": "1700000000"}
        with unittest.mock.patch.object(make_release.subprocess, "run") as run:
            run.return_value.returncode = 0
            with contextlib.redirect_stdout(io.StringIO()):
                real.run(["cmake", "--build", "build-product"])
        self.assertEqual(run.call_args.kwargs["env"]["SOURCE_DATE_EPOCH"], "1700000000")

    def test_no_portable_leaves_the_portable_package_out(self):
        code, commands, errors, _ = self.make("--key", str(self.key), "--no-portable")
        self.assertEqual(code, 0, errors)
        self.assertFalse(any("tools/build_portable.py" in argv for argv in commands.runs))
        self.assertFalse(any("portable" in path.name for path in (self.root / "dist").iterdir()))

    def test_a_portable_package_that_does_not_match_its_checksum_is_refused(self):
        class Corrupting(Fake):
            def run(self, argv):
                super().run(argv)
                if "tools/build_portable.py" in argv and "build" in argv:
                    (self.root / make_release.PORTABLE / "tomacco-1.2.3-Linux-x86_64-portable.tar.gz").write_bytes(b"changed")
        code, commands, errors, _ = self.make("--key", str(self.key), commands=Corrupting(self.root))
        self.assertEqual(code, 1)
        self.assertIn("portable.tar.gz does not match its checksum", errors)
        self.assertFalse(any(argv[0] == "gh" for argv in commands.runs))

    def test_the_container_step_builds_and_checks_the_signed_package_and_pushes_nothing(self):
        code, commands, errors, output = self.make("--key", str(self.key), "--container", "--accept-android-sdk-license")
        self.assertEqual(code, 0, errors)
        words = [" ".join(argv) for argv in commands.runs]
        order = ["pkeyutl -sign", "build_container.py build", "build_container.py verify tomacco-container:1.2.3", "gh release create"]
        positions = [next(i for i, line in enumerate(words) if word in line) for word in order]
        self.assertEqual(positions, sorted(positions), words)
        building = next(argv for argv in commands.runs if "tools/build_container.py" in argv and "build" in argv)
        self.assertEqual(building[building.index("--packages") + 1], make_release.DIST)
        self.assertIn("--accept-android-sdk-license", building)
        self.assertFalse(any("docker" in argv[0] for argv in commands.runs), "make_release runs no docker command itself")
        self.assertFalse(any("push" in argv for argv in commands.runs))
        self.assertIn("docker push ghcr.io/marselnenaj/tomacco:1.2.3", output)
        self.assertIn("nothing was pushed", output)

    def test_the_container_step_needs_the_android_sdk_license_accepted(self):
        self.assertRefused("--accept-android-sdk-license", "--key", str(self.key), "--container")

    def test_without_container_no_image_is_built(self):
        code, commands, errors, _ = self.make("--key", str(self.key))
        self.assertEqual(code, 0, errors)
        self.assertFalse(any("tools/build_container.py" in argv for argv in commands.runs))

    def test_a_dry_run_only_prints(self):
        before = sorted(path.name for path in self.root.iterdir())
        fake = Fake(self.root, dry_run=True)
        code, commands, errors, output = self.make("--dry-run", "--key", str(self.key), commands=fake)
        self.assertEqual(code, 0, errors)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), before)
        self.assertIn("gh release create v1.2.3", " ".join(" ".join(argv) for argv in commands.runs))
        self.assertIn("Would sign", output)
        self.assertIn("build-portable/tomacco-1.2.3-Linux-x86_64-portable.tar.gz", output)
        self.assertIn("tools/build_portable.py verify", " ".join(" ".join(argv) for argv in commands.runs))
        self.assertNotIn(self.key.read_text(), output)

    def test_a_real_dry_run_starts_nothing(self):
        commands = make_release.Commands(self.root, dry_run=True)
        with unittest.mock.patch.object(make_release.subprocess, "run", side_effect=AssertionError("started")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                commands.run(["gh", "release", "create", "v1"])
        self.assertIn("Would run: gh release create v1", output.getvalue())

    def test_a_dirty_tree_is_refused(self):
        self.assertRefused("not clean", "--key", str(self.key), commands=Fake(self.root, status=b" M file\n"))

    def test_a_missing_tag_is_named(self):
        self.assertRefused("There is no tag v1.2.3 yet", "--key", str(self.key), commands=Fake(self.root, tags=b""))

    def test_a_checkout_that_is_not_the_tag_is_refused(self):
        self.assertRefused("HEAD is not", "--key", str(self.key), commands=Fake(self.root, tagged=b"other\n"))

    def test_a_version_mismatch_is_refused(self):
        self.assertRefused("does not match", "--tag", "v9.9.9", "--key", str(self.key))
        (self.root / "assets/x.metainfo.xml").write_text("<component/>")
        self.assertRefused("no <release>", "--key", str(self.key))
        (self.root / "assets/x.metainfo.xml").write_text(METAINFO)
        (self.root / "Cargo.toml").write_text('version = "1.2.4"\n')
        self.assertRefused("disagree", "--key", str(self.key))

    def test_the_key_comes_from_outside_the_repository_and_only_when_named(self):
        self.assertRefused("Name the signing key")
        inside = self.root / "key.pem"
        inside.write_text("private")
        inside.chmod(0o600)
        self.assertRefused("outside the repository", "--key", str(inside))
        self.assertRefused("No signing key", "--key", str(self.key) + ".missing")
        self.key.chmod(0o644)
        self.assertRefused("only by its owner", "--key", str(self.key))
        self.key.chmod(0o600)
        code, _, errors, _ = self.make(environment={make_release.KEY_ENVIRONMENT: str(self.key)})
        self.assertEqual(code, 0, errors)

    def test_a_key_the_release_could_not_verify_is_refused(self):
        self.product("")
        self.assertRefused("no public key", "--key", str(self.key))
        self.product(base64.b64encode(b"\x33" * 32).decode())
        self.assertRefused("does not belong", "--key", str(self.key))
        self.product(base64.b64encode(b"\x33" * 32).decode(), "")
        self.assertRefused("does not belong", "--key", str(self.key))

    def test_during_a_rotation_the_next_key_may_sign(self):
        # The key in hand is the next one: the released program trusts it.
        self.product(base64.b64encode(b"\x33" * 32).decode(), PUBLIC)
        code, _, errors, _ = self.make("--key", str(self.key))
        self.assertEqual(code, 0, errors)
        self.assertEqual(make_release.trusted_keys(self.root), [base64.b64encode(b"\x33" * 32).decode(), PUBLIC])
        # A next key alone does not make a release: the current one must be set.
        self.product("", PUBLIC)
        self.assertRefused("no public key", "--key", str(self.key))

    def test_the_product_names_the_current_and_the_next_key(self):
        product = (ROOT / "native/product.rs").read_text()
        self.assertRegex(product, r'pub const RELEASE_SIGNING_PUBLIC_KEY_BASE64: &str = "[A-Za-z0-9+/]{43}=";')
        self.assertRegex(product, r'pub const NEXT_RELEASE_SIGNING_PUBLIC_KEY_BASE64: &str = "(?:[A-Za-z0-9+/]{43}=)?";')
        keys = make_release.trusted_keys(ROOT)
        self.assertTrue(all(len(base64.b64decode(key)) == 32 for key in keys))

    def test_leftovers_in_dist_are_refused(self):
        (self.root / "dist").mkdir()
        (self.root / "dist/old.tar.gz").write_text("old")
        self.assertRefused("not empty", "--key", str(self.key))

    def test_a_package_that_does_not_match_its_checksum_is_refused(self):
        class Corrupting(Fake):
            def run(self, argv):
                super().run(argv)
                if "tools/build_source_release.py" in argv:
                    (self.root / make_release.DIST / "tomacco-1.2.3-source.tar.gz").write_bytes(b"changed")
        code, commands, errors, _ = self.make("--key", str(self.key), commands=Corrupting(self.root))
        self.assertEqual(code, 1)
        self.assertIn("does not match its checksum", errors)
        self.assertFalse(any(argv[0] == "gh" for argv in commands.runs))

    @unittest.skipUnless(shutil.which("openssl"), "needs openssl")
    def test_real_openssl_makes_the_signature_format_the_program_reads(self):
        key = self.key.with_name("throwaway.pem")
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(key)], check=True, capture_output=True)
        package = self.root / "tomacco-1.2.3-Linux-x86_64.tar.gz"
        package.write_bytes(b"package")
        signature = make_release.sign(make_release.Commands(self.root), key, package)
        self.assertEqual(len(base64.b64decode(signature.read_bytes())), 64)


class ReleaseFlow(unittest.TestCase):
    def test_no_workflow_signs_or_holds_a_signing_secret(self):
        self.assertFalse((ROOT / ".github/workflows/release.yml").exists())
        for workflow in (ROOT / ".github/workflows").glob("*.yml"):
            self.assertNotIn("SIGNING", workflow.read_text(), workflow.name)


if __name__ == "__main__":
    unittest.main()
