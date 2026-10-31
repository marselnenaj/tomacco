"""tools/build_container.py and packaging/container/Dockerfile without Docker: names, pins, the
license gate, the MCP exchange and the commands it would run."""

import argparse
import base64
import contextlib
import io
import json
import re
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import build_container

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "packaging/container/Dockerfile").read_text()


def reply(*messages):
    return "".join(json.dumps(message) + "\n" for message in messages)


GOOD_REPLY = reply(
    {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "tomacco", "version": "1.2.3"}}},
    {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": name} for name in build_container.MCP_TOOLS + ("job.get",)]}},
)


class Dockerfile(unittest.TestCase):
    def test_the_base_is_pinned_and_at_least_the_portable_glibc_floor(self):
        self.assertRegex(DOCKERFILE, r"(?m)^FROM ubuntu:24\.04@sha256:[0-9a-f]{64}$")

    def test_the_build_refuses_without_the_android_sdk_license_accepted(self):
        self.assertIn("ARG ACCEPT_ANDROID_SDK_LICENSE=0", DOCKERFILE)
        gate = DOCKERFILE.index('if [ "$ACCEPT_ANDROID_SDK_LICENSE" != 1 ]')
        self.assertLess(gate, DOCKERFILE.index("sdkmanager"))
        self.assertIn("exit 1", DOCKERFILE[gate:gate + 400])

    def test_tomaccoctl_comes_from_a_package_checked_against_checksum_and_release_key(self):
        public = re.search(r'RELEASE_SIGNING_PUBLIC_KEY_BASE64: &str = "([^"]+)"', (ROOT / "native/product.rs").read_text()).group(1)
        self.assertIn(f'public_key="{public}"', DOCKERFILE)
        self.assertEqual(len(base64.b64decode(public)), 32)
        step = DOCKERFILE[DOCKERFILE.index("public_key="):DOCKERFILE.index("tomaccoctl --version")]
        checksum, verify, unpack = (step.index("sha256sum --check"), step.index("openssl pkeyutl -verify -rawin"), step.index("tar -xzf"))
        self.assertLess(checksum, unpack)
        self.assertLess(verify, unpack)
        # The same DER prefix of an Ed25519 public key as install.sh.
        self.assertIn(r"\060\052\060\005\006\003\053\145\160\003\041\000", step)

    def test_every_other_download_is_checked_against_a_pinned_digest(self):
        self.assertRegex(DOCKERFILE, r"ARG GRADLE_SHA256=[0-9a-f]{64}\n")
        self.assertIn('echo "${GRADLE_SHA256}  /tmp/gradle.zip" | sha256sum -c -', DOCKERFILE)
        self.assertRegex(DOCKERFILE, r"ARG ANDROID_CMDLINE_TOOLS_SHA1=[0-9a-f]{40}\n")
        self.assertRegex(DOCKERFILE, r"ARG ANDROID_CMDLINE_TOOLS_SHA256=[0-9a-f]{64}\n")
        self.assertIn('| sha1sum -c -', DOCKERFILE)
        self.assertIn('echo "${ANDROID_CMDLINE_TOOLS_SHA256}  /tmp/cmdline-tools.zip" | sha256sum -c -', DOCKERFILE)

    def test_the_sdk_matches_what_setup_installs(self):
        setup = (ROOT / "native/setup.rs").read_text()
        for package in ("platforms;android-36", "build-tools;36.0.0", "system-images;android-36;google_apis;x86_64"):
            self.assertIn(f'"{package}"', setup)
            self.assertIn(package, DOCKERFILE)
        self.assertEqual(build_container.SYSTEM_IMAGE, "system-images;android-36;google_apis;x86_64")

    def test_the_image_runs_tomaccoctl_as_an_unprivileged_user_with_its_data_on_a_volume(self):
        self.assertEqual(re.findall(r"(?m)^USER (\S+)$", DOCKERFILE)[-1], "tomacco")
        self.assertIn('ENTRYPOINT ["/usr/bin/tini", "--", "tomaccoctl"]', DOCKERFILE)
        self.assertIn('VOLUME ["/data"]', DOCKERFILE)
        self.assertIn("TOMACCO_HOME=/data/tomacco", DOCKERFILE)
        self.assertIn("TOMACCO_UPDATES=off", DOCKERFILE)

    def test_the_emulator_is_optional(self):
        self.assertIn("ARG ANDROID_EMULATOR=0", DOCKERFILE)
        self.assertIn('sdkmanager --install emulator "$ANDROID_SYSTEM_IMAGE"', DOCKERFILE)

    def test_no_desktop_or_virtual_iphone_goes_in(self):
        extracted = re.search(r'tar -xzf "\$name\.tar\.gz" ([^;]+);', DOCKERFILE).group(1)
        self.assertNotIn("bin/tomacco\"", extracted)
        self.assertIn('"$name/bin/tomaccoctl"', extracted)
        for word in ("qt6", "ios-paravirt", "libimobiledevice", "usbmuxd"):
            self.assertNotIn(word, DOCKERFILE.lower())


class Building(unittest.TestCase):
    def test_names(self):
        self.assertEqual(build_container.image_name("1.2.3"), "tomacco-container:1.2.3")
        self.assertEqual(build_container.image_name("1.2.3", emulator=True), "tomacco-container:1.2.3-emulator")
        self.assertEqual(build_container.package_files("1.2.3"), [
            "tomacco-1.2.3-Linux-x86_64.tar.gz", "tomacco-1.2.3-Linux-x86_64.tar.gz.sha256", "tomacco-1.2.3-Linux-x86_64.tar.gz.sig"])

    def test_the_build_command_accepts_the_license_and_picks_the_variant(self):
        argv = build_container.build_command("t:1", Path("/c"), build_container.build_arguments("1.2.3"))
        self.assertEqual(argv[:4], ["docker", "build", "--tag", "t:1"])
        self.assertIn("ACCEPT_ANDROID_SDK_LICENSE=1", argv)
        self.assertIn("TOMACCO_VERSION=1.2.3", argv)
        self.assertNotIn("ANDROID_EMULATOR=1", argv)
        self.assertIn("ANDROID_EMULATOR=1", build_container.build_command("t", Path("/c"), build_container.build_arguments("1", True)))
        self.assertEqual(argv[-1], "/c")

    def test_building_without_accepting_the_license_runs_nothing(self):
        errors = io.StringIO()
        with unittest.mock.patch.object(build_container.subprocess, "run", side_effect=AssertionError("started")), \
                contextlib.redirect_stderr(errors):
            self.assertEqual(build_container.main(["build"]), 1)
        self.assertIn("Android SDK license", errors.getvalue())

    def test_a_local_package_needs_its_checksum_and_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            packages, output = Path(directory, "dist"), Path(directory, "out")
            packages.mkdir()
            names = build_container.package_files("1.2.3")
            for name in names[:2]:
                (packages / name).write_text(name)
            with self.assertRaisesRegex(build_container.Failed, r"\.sig"):
                build_container.prepare_context(output, packages, "1.2.3")
            (packages / names[2]).write_text("signature")
            context = build_container.prepare_context(output, packages, "1.2.3")
            self.assertEqual(sorted(p.name for p in (context / "packages").iterdir()), sorted(names))
            self.assertEqual((context / "Dockerfile").read_text(), DOCKERFILE)

    def test_push_commands_are_only_printed(self):
        commands = build_container.push_commands("tomacco-container:1.2.3", "1.2.3")
        self.assertEqual(commands, [["docker", "tag", "tomacco-container:1.2.3", "ghcr.io/marselnenaj/tomacco:1.2.3"],
                                    ["docker", "push", "ghcr.io/marselnenaj/tomacco:1.2.3"]])
        output = io.StringIO()
        with unittest.mock.patch.object(build_container.subprocess, "run", side_effect=AssertionError("started")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(build_container.main(["push-commands", "--version", "1.2.3", "--emulator"]), 0)
        self.assertIn("docker push ghcr.io/marselnenaj/tomacco:1.2.3-emulator", output.getvalue())
        self.assertIn("nothing was pushed", output.getvalue())

    def test_the_check_container_is_long_lived_and_gets_kvm_only_for_the_emulator(self):
        plain = build_container.Container("i", Path("/d"), "n").start_command()
        self.assertEqual(plain[-4:], ["--entrypoint", "sleep", "i", "infinity"])
        self.assertIn("/d:/data", plain)
        self.assertNotIn("/dev/kvm", plain)
        with unittest.mock.patch.object(build_container.os, "stat", return_value=unittest.mock.Mock(st_gid=992)):
            kvm = build_container.Container("i", Path("/d"), "n", kvm=True).start_command()
        self.assertEqual(kvm[kvm.index("--device") + 1], "/dev/kvm")
        self.assertEqual(kvm[kvm.index("--group-add") + 1], "992")


class Mcp(unittest.TestCase):
    def test_the_requests_are_initialize_initialized_and_tools_list(self):
        methods = [request["method"] for request in build_container.mcp_requests()]
        self.assertEqual(methods, ["initialize", "notifications/initialized", "tools/list"])

    def test_a_good_reply_names_the_server_and_counts_the_tools(self):
        self.assertEqual(build_container.check_mcp_reply(GOOD_REPLY), ("1.2.3", len(build_container.MCP_TOOLS) + 1))

    def test_bad_replies_fail(self):
        with self.assertRaisesRegex(build_container.Failed, "initialize"):
            build_container.check_mcp_reply("")
        with self.assertRaisesRegex(build_container.Failed, "not tomacco"):
            build_container.check_mcp_reply(reply({"id": 1, "result": {"serverInfo": {"name": "other"}}}))
        with self.assertRaisesRegex(build_container.Failed, "lacks project.run"):
            build_container.check_mcp_reply(reply(
                {"id": 1, "result": {"serverInfo": {"name": "tomacco"}}},
                {"id": 2, "result": {"tools": [{"name": n} for n in build_container.MCP_TOOLS if n != "project.run"]}}))

    def test_the_exchange_pipes_into_docker_run_interactive(self):
        seen = {}

        def runner(argv, **options):
            seen["argv"], seen["input"] = argv, options["input"]
            return subprocess.CompletedProcess(argv, 0, stdout=GOOD_REPLY, stderr="")
        with contextlib.redirect_stdout(io.StringIO()):
            build_container.mcp(argparse.Namespace(image="tomacco-container:1.2.3"), runner=runner)
        self.assertEqual(seen["argv"], ["docker", "run", "-i", "--rm", "tomacco-container:1.2.3", "mcp"])
        self.assertEqual([json.loads(line)["method"] for line in seen["input"].splitlines()],
                         ["initialize", "notifications/initialized", "tools/list"])

    def test_the_host_script_sends_the_same_exchange(self):
        script = (ROOT / "packaging/container/mcp-check.sh").read_text()
        self.assertIn('docker run -i --rm "$image" mcp', script)
        for method in ("initialize", "notifications/initialized", "tools/list"):
            self.assertIn(f'"method":"{method}"', script)


if __name__ == "__main__":
    unittest.main()
