"""Installing on a running phone, and the guest channels it and the debugger use.
Fakes only: the trust-cache rewrite, the wire format and the argument checks."""
import hashlib
import json
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from build_ios_guest_tools import LOCAL_PLATFORM_POLICY  # noqa: E402
import ios_debug_bridge  # noqa: E402
import ios_hot_install as hot  # noqa: E402
from ios_paravirt_graphics import cache_entries  # noqa: E402
import ios_virtual_iphone as phone  # noqa: E402
import run_ios_paravirt as launcher  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def module(*hashes):
    body = b"".join(sorted(value + LOCAL_PLATFORM_POLICY for value in hashes))
    return struct.pack("<I16sI", 2, b"u" * 16, len(hashes)) + body


def hash_of(text):
    return hashlib.sha256(text.encode()).digest()[:20]


class TrustCacheTests(unittest.TestCase):
    def test_a_boot_reserves_spare_entries_above_every_real_hash(self):
        real = [hash_of("a"), hash_of("b")]
        reserved = hot.reserve(module(*real), 4)
        entries = sorted(cache_entries(reserved))
        self.assertEqual(entries[:2], sorted(real))
        self.assertTrue(all(value.startswith(hot.FILLER_PREFIX) for value in entries[2:]))
        self.assertEqual(hot.spare_entries(reserved), 4)

    def test_a_cache_with_spare_entries_is_not_reserved_twice(self):
        with self.assertRaises(ValueError):
            hot.reserve(hot.reserve(module(hash_of("a")), 2), 2)

    def test_hashes_take_the_place_of_fillers_and_the_module_keeps_its_shape(self):
        reserved = hot.reserve(module(hash_of("a")), 4)
        new = hash_of("new")
        updated = hot.with_hashes(reserved, [new])
        self.assertEqual(len(updated), len(reserved))
        self.assertEqual(updated[:24], reserved[:24])
        entries = cache_entries(updated)
        self.assertEqual(entries[new], new + LOCAL_PLATFORM_POLICY)
        self.assertEqual(hot.spare_entries(updated), 3)
        keys = [updated[offset:offset + 20] for offset in range(24, len(updated), 24)]
        self.assertEqual(keys, sorted(keys))

    def test_a_hash_it_has_takes_no_spare_entry(self):
        reserved = hot.reserve(module(hash_of("a")), 2)
        self.assertEqual(hot.with_hashes(reserved, [hash_of("a")]), reserved)

    def test_running_out_of_spare_entries_says_what_to_do(self):
        reserved = hot.reserve(module(hash_of("a")), 1)
        with self.assertRaisesRegex(ValueError, "stop and start"):
            hot.with_hashes(reserved, [hash_of("b"), hash_of("c")])

    def test_only_real_hashes_are_taken(self):
        reserved = hot.reserve(module(hash_of("a")), 2)
        for bad in (b"short", hot.FILLER_PREFIX + b"\0" * 8):
            with self.assertRaises(ValueError):
                hot.with_hashes(reserved, [bad])


class BundleStreamTests(unittest.TestCase):
    def test_the_wire_format_names_each_file_and_ends_with_an_empty_one(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "App.app"
            (bundle / "Frameworks").mkdir(parents=True)
            (bundle / "App").write_bytes(b"code")
            (bundle / "App").chmod(0o755)
            (bundle / "Frameworks/data").write_bytes(b"xy")
            (bundle / "Frameworks/data").chmod(0o640)
            left, right = socket.socketpair()
            with left, right:
                self.assertEqual(hot.send_bundle(left, bundle), (2, 6))
                left.close()
                received = b""
                while part := right.recv(65536):
                    received += part
        expected = (struct.pack("<IIQ", 11, 0o755, 4) + b"App.app/App" + b"code"
                    + struct.pack("<IIQ", 23, 0o644, 2) + b"App.app/Frameworks/data" + b"xy"
                    + struct.pack("<IIQ", 0, 0, 0))
        self.assertEqual(received, expected)

    def test_a_link_in_the_signed_copy_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "App.app"
            bundle.mkdir()
            (bundle / "link").symlink_to("/etc/passwd")
            with self.assertRaisesRegex(ValueError, "not an ordinary file"):
                hot.bundle_files(bundle)

    def test_an_empty_bundle_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "holds no files"):
                hot.bundle_files(directory)

    def fake_vsock(self, replies):
        """A connect function whose peer answers each CONNECT with the next reply."""
        requests = []

        def connect(_path):
            left, right = socket.socketpair()
            reply = replies.pop(0)

            def serve():
                requests.append(right.recv(64))
                right.sendall(reply)
                right.close()
            threading.Thread(target=serve, daemon=True).start()
            return left
        return connect, requests

    def test_a_port_that_is_not_listening_yet_is_asked_again(self):
        connect, requests = self.fake_vsock([b"", b"OK 1234\n"])
        with hot.connect_port("/fake", 1342, 10, connect, pause=0):
            pass
        self.assertEqual(requests, [b"CONNECT 1342\n", b"CONNECT 1342\n"])

    def test_a_port_that_never_answers_names_itself(self):
        connect, _ = self.fake_vsock([b"" for _ in range(100000)])
        with self.assertRaisesRegex(RuntimeError, "port 1343"):
            hot.connect_port("/fake", 1343, 1, connect, pause=0)

    def test_the_guest_socket_comes_from_the_runs_request(self):
        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory) / "request.json"
            request.write_text(json.dumps({"guest_control": {"socket": "/run/guest.sock"}}))
            self.assertEqual(hot.guest_socket(request), "/run/guest.sock")
            request.write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "no guest channel"):
                hot.guest_socket(request)


class DebugBridgeTests(unittest.TestCase):
    def test_bytes_go_both_ways_until_one_side_ends(self):
        debugger, debugger_end = socket.socketpair()
        guest, guest_end = socket.socketpair()
        counts = {}

        def run():
            counts["bytes"] = ios_debug_bridge.pump(debugger_end, guest_end)
        pump = threading.Thread(target=run, daemon=True)
        pump.start()
        debugger.sendall(b"$qSupported#00")
        self.assertEqual(guest.recv(64), b"$qSupported#00")
        guest.sendall(b"+$OK#9a")
        self.assertEqual(debugger.recv(64), b"+$OK#9a")
        debugger.close()
        pump.join(5)
        self.assertEqual(counts["bytes"], (14, 7))
        for end in (debugger_end, guest, guest_end):
            end.close()

    def test_a_refused_guest_request_is_reported_with_its_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            cli = Path(directory) / "cli"
            cli.write_text("#!/bin/sh\necho '{\"ok\":true,\"result\":{\"response\":{\"ok\":false,\"error\":\"debug_port_busy\"}}}'\n")
            cli.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, "debug_port_busy"):
                ios_debug_bridge.guest_call(str(cli), "job", "debug_server", "App")


class GuestNetworkTests(unittest.TestCase):
    def test_off_attaches_nothing(self):
        self.assertEqual(launcher.guest_network_arguments("off"), [])

    def test_nat_is_a_modern_virtio_device_on_passt_with_no_inbound_and_no_host_loopback(self):
        command = launcher.guest_network_arguments("nat", Path("/opt/pa,sst"))
        netdev = command[command.index("-netdev") + 1]
        device = command[command.index("-device") + 1]
        self.assertTrue(netdev.startswith("passt,"))
        self.assertIn("path=/opt/pa,,sst", netdev)
        self.assertIn("map-host-loopback=none", netdev)
        self.assertNotIn("tcp-ports", netdev)
        self.assertNotIn("udp-ports", netdev)
        self.assertIn("virtio-net-pci,disable-legacy=on,romfile=,netdev=tomacco-net", device)

    def test_a_missing_passt_costs_the_network_and_not_the_boot(self):
        boot = {"guest_network": "nat"}
        record = phone.network_boot(boot, which=lambda _: None)
        self.assertEqual(boot["guest_network"], "off")
        self.assertEqual(record["state"], "off")
        self.assertIn("passt", record["reason"])

    def test_passt_on_path_is_named_to_the_engine(self):
        boot = {"guest_network": "nat"}
        record = phone.network_boot(boot, which=lambda _: "/usr/bin/passt")
        self.assertEqual(boot["guest_network_backend"], "/usr/bin/passt")
        self.assertEqual(record, {"state": "nat", "backend": "/usr/bin/passt"})

    def test_no_setting_means_no_network(self):
        boot = {}
        self.assertEqual(phone.network_boot(boot, which=lambda _: "/usr/bin/passt"), {"state": "off"})
        self.assertNotIn("guest_network_backend", boot)


class GuestSourceTests(unittest.TestCase):
    """The guest's own checks, compiled for this host."""

    def run_harness(self, body):
        compiler = shutil.which("clang") or shutil.which("clang-22")
        if not compiler:
            self.skipTest("Clang is required to parse the guest source's Blocks ABI")
        source = REPO / "integrations/ios-emulator/guest-app-control.c"
        with tempfile.TemporaryDirectory(prefix="tomacco-host-channel-") as directory:
            harness = Path(directory) / "harness.c"
            harness.write_text("#define main guest_entry\n#include " + json.dumps(str(source)) + "\n#undef main\n" + body)
            build = subprocess.run([compiler, "-O2", "-fblocks", "-ffunction-sections", "-fdata-sections",
                                    str(harness), "-Wl,--gc-sections", "-o", str(Path(directory) / "harness")],
                                   capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stderr)
            subprocess.run([str(Path(directory) / "harness")], check=True, timeout=5)

    def test_a_bundle_path_stays_inside_the_bundle(self):
        self.run_harness(r'''
#define OK(text) bundle_path_valid(text, sizeof(text) - 1)
int main(void) {
    if (!OK("App.app/App") || !OK("App.app/Frameworks/F.framework/F") || !OK("App.app")) return 1;
    if (OK("") || OK("/App.app/App") || OK("App.app/../x") || OK("App.app/./x") || OK("App.app//x") ||
        OK("App.app/x/") || OK("App/x") || OK(".app/x") || OK("../App.app/x")) return 2;
    return 0;
}
''')

    def test_the_guest_takes_the_same_arguments_as_the_control_plane(self):
        self.run_harness(r'''
int main(void) {
    if (!valid_command("receive_bundle", 0) || valid_command("receive_bundle", "x")) return 1;
    if (!valid_command("network_state", 0) || valid_command("network_state", "x")) return 2;
    const char *hash = "0123456789abcdef0123456789abcdef01234567:dev.tomacco.hello";
    if (!valid_command("policy_add", hash) || valid_command("policy_add", "dev.tomacco.hello")) return 3;
    if (valid_command("policy_add", "0123456789abcdef0123456789abcdef01234567:.x")) return 4;
    if (!valid_command("debug_server", "551") || !valid_command("debug_server", "HelloTomacco")) return 5;
    if (valid_command("debug_server", "-x") || valid_command("debug_server", "a b") || valid_command("debug_server", 0)) return 6;
    if (!valid_command("hardware_probe", "AppleVirtIONetwork") || valid_command("hardware_probe", "a/b")) return 7;
    if (!valid_command("gestalt", "HasFlash") || valid_command("gestalt", "a b")) return 8;
    if (!worker_records_result("receive_bundle", 0) || !worker_records_result("policy_add", hash)) return 9;
    return 0;
}
''')

    def test_the_policy_table_stays_canonical_when_entries_are_added(self):
        self.run_harness(r'''
static unsigned char table[TOMACCO_POLICY_TABLE_BYTES];
int main(void) {
    unsigned char first[20] = {1}, second[20] = {2}, third[20] = {3};
    unsigned long size = tomacco_policy_insert(table, 0, first, "dev.b");
    if (tomacco_policy_count(table, size) != 1) return 1;
    size = tomacco_policy_insert(table, 1, second, "dev.a");
    if (tomacco_policy_count(table, size) != 2) return 2;
    if (tomacco_policy_find(table, 2, "dev.a") != 0 || tomacco_policy_find(table, 2, "dev.b") != 1) return 3;
    if (tomacco_policy_cdhash(table, 1)[0] != 1 || tomacco_policy_cdhash(table, 0)[0] != 2) return 4;
    /* A redeployed app has another hash and no second entry. */
    size = tomacco_policy_insert(table, 2, third, "dev.a");
    if (tomacco_policy_count(table, size) != 2 || tomacco_policy_cdhash(table, 0)[0] != 3) return 5;
    char name[16];
    unsigned count = 2;
    for (unsigned index = 0; index < 30; ++index) {
        snprintf(name, sizeof(name), "dev.c%02u", index);
        size = tomacco_policy_insert(table, count++, first, name);
        if (tomacco_policy_count(table, size) != count) return 6;
    }
    if (tomacco_policy_insert(table, count, first, "dev.z")) return 7;
    if (!tomacco_policy_insert(table, count, first, "dev.a")) return 8;
    return 0;
}
''')


class DeployTests(unittest.TestCase):
    """Phone.deploy's order of steps, with every guest and signing step faked."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tomacco-deploy-")
        self.root = Path(self.temp.name)
        self.directory = self.root / "phone"
        self.directory.mkdir()
        self.tools = self.root / "tools"
        self.tools.mkdir()
        (self.tools / "guest-tools.json").write_text("{}")
        self.bundle = self.root / "App.app"
        self.bundle.mkdir()
        self.module = self.directory / "system.boot.tc"
        self.module.write_bytes(hot.reserve(module(hash_of("a")), 3))
        (self.directory / "profile.json").write_text(json.dumps({"format": 1, "boot": {}, "installs": str(self.directory)}))
        (self.directory / "state.json").write_text(json.dumps({
            "run": 9, "closed": False, "install": str(self.directory), "tools": str(self.tools),
            "guest_job": "guest", "trust_cache_module": str(self.module), "applications": {"old.app": "11"}}))
        self.steps = []
        self.hash = hash_of("the app").hex()

    def tearDown(self):
        self.temp.cleanup()

    def phone(self, running=True):
        device = phone.Phone("/no/cli", self.directory)
        device.guest_running = lambda: running

        def call(operation, arguments, timeout=180, independent=False):
            if operation == "ios.emulator.control":
                self.steps.append(arguments["action"])
                self.assertEqual(hot.spare_entries(Path(arguments["path"]).read_bytes()), 2)
                return {"updated": True}
            if operation == "ios.emulator.guest":
                self.steps.append(arguments["operation"])
                return {"response": {"ok": True, "pid": 5}}
            self.steps.append(operation)
            return {}
        device.call = call
        device.send_bundle = lambda job, bundle: self.steps.append("send") or "/guest/App.app"
        device.install = lambda job, path, identity: self.steps.append("install")
        return device

    def staged(self, bundle, entitlements, payload, signer, taken):
        (payload / "Applications/App.app").mkdir(parents=True)
        return ([{"path": "Applications/App.app/App", "cdhash": self.hash, "application": "x",
                  "signing_identifier": "dev.app"}],
                {"bundle_id": "dev.app", "path": "Applications/App.app", "cdhash": self.hash})

    def deploy(self, device, open_app=True, force=False):
        toolchain = {"ldid": sys.executable}
        self.events = []
        with unittest.mock.patch.object(phone, "stage_application", self.staged), \
                unittest.mock.patch.object(phone, "guest_toolchain", lambda *_: toolchain), \
                unittest.mock.patch.object(phone, "emit", lambda *_a, **k: self.events.append(k)):
            device.deploy(self.bundle, None, open_app, force)

    def installed_before(self):
        state = json.loads((self.directory / "state.json").read_text())
        state["applications"]["dev.app"] = self.hash
        (self.directory / "state.json").write_text(json.dumps(state))

    def test_the_same_signed_app_is_not_sent_again_but_opened(self):
        self.installed_before()
        self.deploy(self.phone())
        self.assertEqual(self.steps, ["ios.virtual.configure", "open"])
        self.assertEqual(hot.spare_entries(self.module.read_bytes()), 3)
        self.assertEqual([event["step"] for event in self.events], ["signed", "unchanged", "opened", "done"])
        self.assertIs(self.events[-1]["unchanged"], True)

    def test_force_installs_the_same_signed_app_again(self):
        self.installed_before()
        self.deploy(self.phone(), force=True)
        self.assertEqual(self.steps, ["trust_cache_update", "send", "policy_add", "install",
                                      "ios.virtual.configure", "open"])
        self.assertIs(self.events[-1]["unchanged"], False)

    def test_trust_comes_first_and_the_app_is_listed_after_it_installed(self):
        device = self.phone()
        self.deploy(device)
        self.assertEqual(self.steps, ["trust_cache_update", "send", "policy_add", "install",
                                      "ios.virtual.configure", "open"])
        self.assertEqual(hot.spare_entries(self.module.read_bytes()), 2)
        self.assertEqual(json.loads((self.directory / "state.json").read_text())["applications"],
                         {"old.app": "11", "dev.app": self.hash})

    def test_install_only_does_not_open(self):
        self.deploy(self.phone(), open_app=False)
        self.assertNotIn("open", self.steps)

    def test_a_stopped_phone_is_told_to_start(self):
        with self.assertRaisesRegex(RuntimeError, "not running"):
            self.deploy(self.phone(running=False))
        self.assertEqual(self.steps, [])

    def test_a_boot_without_spare_entries_says_so_before_any_work(self):
        self.module.unlink()
        with self.assertRaisesRegex(RuntimeError, "spare trust-cache entries"):
            self.deploy(self.phone())
        self.assertEqual(self.steps, [])

    def test_a_guest_that_stays_busy_ends_the_request_with_its_answer(self):
        device = self.phone()
        answers = [{"response": {"error": "guest_operation_busy"}}, {"response": {"ok": True, "entries": 1}}]
        device.call = lambda *_a, **_k: answers.pop(0)
        with unittest.mock.patch.object(phone, "time", unittest.mock.Mock(monotonic=lambda: 0, sleep=lambda _: None)):
            self.assertEqual(device.deploy_request("guest", "policy_add", "x")["entries"], 1)


class EngineTests(unittest.TestCase):
    def test_the_engine_can_rewrite_the_module_only_while_stopped_and_at_its_size(self):
        source = (REPO / "integrations/ios-emulator/vphone-research.c").read_text()
        body = source[source.index("static void set_trust_cache_update"):source.index("static char *get_sep(")]
        self.assertIn("runstate_is_running()", body)
        self.assertIn("tc_module_bytes", body)
        self.assertIn('"trust-cache-update"', source)


if __name__ == "__main__":
    unittest.main()
