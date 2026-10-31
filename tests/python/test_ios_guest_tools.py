"""Guest preparation boundaries; synthetic signatures are never guest execution."""
import copy
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import unittest

from build_ios_gpu_plugin import guest_entitlements_der, inspect_signed_code
from build_ios_guest_tools import GUEST_IDENTIFIER, GUEST_ROOT, LOCAL_PLATFORM_POLICY, POLICY_TABLE_MAGIC, QUALIFIED_DEVELOPMENT_COMPILERS, build, compiler_identity, decode_development_policy_table, development_policy, development_policy_table, embedded_policy_table, extend_trustcache, guest_directory_modes, launch_jobs, policy_entries, program_entitlements
from build_ios_research_bootstrap import application_info, sign_program, verify_application_resources
from ios_application_bundle import application_entitlements
from ios_guest_tools import development_library, payload, virtual_research_mode
from prepare_ios_paravirt import Node, Property, decode
from prepare_ios_guest_tools import GRAPHICS_FILES, graphics_payload, graphics_script, verify_previous_script
from rebase_ios_guest_tools import rebase
from ios_graphics_location import GRAPHICS_RELATIVE_PATH
from test_product import Isolated
from native_client import dispatch, jobs



def helper_kernel_image(release):
    """A bzImage setup header whose version string names release."""
    image = bytearray(0x400)
    image[0x202:0x206] = b"HdrS"
    image[0x20e:0x210] = (0x300 - 0x200).to_bytes(2, "little")
    text = release.encode() + b" (builder@host) #1 SMP\0"
    image[0x300:0x300 + len(text)] = text
    return bytes(image)


def helper_module(release):
    return b"\x7fELF" + bytes(32) + b"vermagic=" + release.encode() + b" SMP preempt mod_unload \0" + bytes(8)

class GuestBundleSigningTests(unittest.TestCase):
    def test_development_mapping_pins_actual_signed_library_and_rejects_tampering(self):
        from types import SimpleNamespace
        repo = Path(__file__).resolve().parents[2]
        linker = repo / ".tooling/ios-linker/prefix/bin/aarch64-apple-darwin-ld"
        signer = repo / ".tooling/ios-sptm/support/ldid/ldid"
        compiler = shutil.which("clang-22") or shutil.which("clang")
        if not compiler or not linker.is_file() or not signer.is_file():
            self.skipTest("The local Linux iOS linker and ldid are required for actual payload signing")
        # The mapping is checked with any clang; only a qualified one may ship.
        qualified = compiler_identity(compiler)["major"] in QUALIFIED_DEVELOPMENT_COMPILERS
        with tempfile.TemporaryDirectory(prefix="tomacco-development-mapping-") as directory:
            root = Path(directory)
            trustcache = root / "original.tc"
            original = cache()
            trustcache.write_bytes(original)
            output = root / "tools"
            # The build's payload modes must survive an ordinary private
            # caller's restrictive umask without changing the process setting.
            previous_umask = os.umask(0o077)
            try:
                record = build(SimpleNamespace(output=output, trustcache=trustcache, linker=linker,
                                               ldid=signer, clang=compiler, minimum_ios="27.0",
                                               agent=True, application_probe=True, development_signing=True,
                                               allow_unqualified_compiler=not qualified))
                self.assertEqual(os.umask(0o077), 0o077)
            finally:
                os.umask(previous_umask)
            self.assertEqual(record["compiler"]["development_signing_qualified"], qualified)
            self.assertEqual(record, json.loads((output / "guest-tools.json").read_text()))
            program_paths = {row["path"] for row in record["programs"]}
            guest_root = output / "Supplemental"
            for directory in (guest_root, *(p for p in guest_root.rglob("*") if p.is_dir())):
                self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            self.assertEqual((output / "sources").stat().st_mode & 0o777, 0o700)
            for row in record["files"]:
                expected_mode = 0o755 if row["path"] in program_paths else 0o644
                self.assertEqual(row["mode"], expected_mode)
                self.assertEqual((output / "Supplemental" / row["path"]).stat().st_mode & 0o777,
                                 expected_mode)
            pin = development_library(output, original)
            relative = "usr/libexec/libtomacco-development-signing.dylib"
            library = next(row for row in record["programs"] if row["path"] == relative)
            self.assertEqual(pin["cdhash"], library["cdhash"])
            self.assertEqual(pin["sha256"], library["sha256"])
            self.assertEqual(pin["app_cdhash"], record["development_signing"]["cdhash"])
            self.assertTrue(record["installer_connection_query"])
            control = next(row for row in record["programs"] if row["path"].endswith("/tomacco-app-control"))
            self.assertEqual(control["entitlements"]["com.apple.private.mobileinstall.allowedSPI"],
                             ["InstallForInstallCoordination", "GetSystemAppMigrationStatus"])
            mislabelled = {**record, "installer_connection_query": False}
            (output / "guest-tools.json").write_text(json.dumps(mislabelled))
            with self.assertRaisesRegex(ValueError, "capability grant"):
                payload(output, original)
            self.assertTrue(record["application_termination"])
            self.assertIs(control["entitlements"]["com.apple.runningboard.terminateprocess"], True)
            (output / "guest-tools.json").write_text(json.dumps({**record, "application_termination": False}))
            with self.assertRaisesRegex(ValueError, "capability grant"):
                payload(output, original)
            (output / "guest-tools.json").write_text(json.dumps(record))
            path = output / "Supplemental" / relative
            damaged = bytearray(path.read_bytes())
            damaged[0x4000] ^= 1  # Inside a sealed code page, not its signature.
            path.write_bytes(damaged)
            # Updating the outer manifest cannot authorize modified code.
            library["sha256"] = digest(damaged)
            record["development_signing"]["library_sha256"] = digest(damaged)
            next(row for row in record["files"] if row["path"] == relative)["sha256"] = digest(damaged)
            (output / "guest-tools.json").write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "page"):
                development_library(output, original)

    def test_real_signer_keeps_entitlement_input_outside_sealed_bundle(self):
        repo = Path(__file__).resolve().parents[2]
        linker = repo / ".tooling/ios-linker/prefix/bin/aarch64-apple-darwin-ld"
        signer = repo / ".tooling/ios-sptm/support/ldid/ldid"
        compiler = shutil.which("clang") or shutil.which("clang-22")
        if not compiler or not linker.is_file() or not signer.is_file():
            self.skipTest("The local Linux iOS linker and ldid are required for actual bundle signing")
        with tempfile.TemporaryDirectory(prefix="tomacco-resource-seal-") as directory:
            root = Path(directory)
            bundle = root / "TomaccoProbe.app"
            bundle.mkdir()
            info = plistlib.dumps(application_info("27.0"))
            (bundle / "Info.plist").write_bytes(info)
            source = root / "main.c"
            source.write_text("int main(void) { return 0; }\n")
            obj = root / "main.o"
            program = bundle / "TomaccoProbe"
            subprocess.run([compiler, "--target=arm64-apple-ios27.0", "-c", str(source), "-o", str(obj)],
                           check=True, capture_output=True, timeout=30)
            subprocess.run([str(linker), "-arch", "arm64", "-platform_version", "ios", "27.0", "27.0",
                            "-o", str(program), str(repo / "integrations/ios-emulator/research-libSystem.tbd"),
                            str(obj)], check=True, capture_output=True, timeout=30)
            identifier = application_info("27.0")["CFBundleIdentifier"]
            entitlements = {"application-identifier": identifier}
            code_hash = sign_program(str(signer), program, entitlements, bundle=bundle)
            resources = (bundle / "_CodeSignature/CodeResources").read_bytes()
            verify_application_resources(info, resources)
            self.assertEqual({str(path.relative_to(bundle)) for path in bundle.rglob("*") if path.is_file()},
                             {"Info.plist", "TomaccoProbe", "_CodeSignature/CodeResources"})
            self.assertFalse(list(root.glob(".tomacco-sign-*")))
            integrity = inspect_signed_code(program.read_bytes(), cpu_subtype=0, file_type=2,
                                            expected_identifier=identifier, expected_entitlements=entitlements,
                                            sealed_files={1: info, 3: resources})
            self.assertEqual(integrity["cdhash"], code_hash.hex())

    def test_signed_manifest_cannot_reference_a_deleted_resource(self):
        info = plistlib.dumps(application_info("27.0"))
        resources = {"files": {"Info.plist": hashlib.sha1(info).digest()}, "files2": {}}
        verify_application_resources(info, plistlib.dumps(resources))
        for version in ("files", "files2"):
            bad = copy.deepcopy(resources)
            bad[version]["TomaccoProbe.entitlements.plist"] = hashlib.sha1(b"deleted input").digest()
            with self.assertRaisesRegex(ValueError, "resource inventory"):
                verify_application_resources(info, plistlib.dumps(bad))
        with self.assertRaisesRegex(ValueError, "resource inventory"):
            verify_application_resources(info + b"\n", plistlib.dumps(resources))


class GuestWorkerArgumentTests(unittest.TestCase):
    def test_controller_state_requires_complete_native_service_identity(self):
        compiler = shutil.which("clang") or shutil.which("cc")
        if not compiler:
            self.skipTest("A C compiler is required to exercise the native state reader")
        header = Path(__file__).resolve().parents[2] / "integrations/ios-emulator/guest-service-state.h"
        with tempfile.TemporaryDirectory(prefix="tomacco-service-state-") as directory:
            root = Path(directory)
            source = root / "state.c"
            source.write_text('#include <string.h>\n#include ' + json.dumps(str(header)) + '\n' + r'''
#define SPEC "user/501/com.apple.mobile.installd"
#define START SPEC " = {\n\tstate = running\n\tprogram = /usr/libexec/installd\n\tpid = 258\n"
static int parse(const char *text, int controller) {
    struct tomacco_service_state state = {-1, -1};
    return tomacco_service_state_read(text, strlen(text), SPEC, &state) &&
           state.pid == 258 && state.controller_pid == controller;
}
int main(void) {
    const char *attached = START "\tcontroller = tomacco-development-setup.63\n}\n";
    const char *detached = START "\tenvironment = {\n\t\tcontroller = misleading.63\n\t}\n}\n";
    if (!parse(attached, 63) || !parse(detached, 0)) return 1;
    const char *reports[] = {attached, detached};
    for (unsigned i = 0; i < 2; ++i) {
        unsigned long size = strlen(reports[i]);
        for (unsigned long n = 0; n < size; ++n) {
            struct tomacco_service_state state = {-1, -1};
            if (tomacco_service_state_read(reports[i], n, SPEC, &state)) return 2;
            if (state.pid != -1 || state.controller_pid != -1) return 3;
        }
    }
    const char *bad[] = {
        START "\tcontroller = .63\n}\n",
        START "\tcontroller = setup.0\n}\n",
        START "\tcontroller = setup.-63\n}\n",
        START "\tcontroller = setup.2147483648\n}\n",
        START "\tcontroller = setup.63\n\tcontroller = setup.63\n}\n",
        START "\tcontroller =\n}\n", START "\tcontroller_pid = 63\n}\n",
        START "\tpid = 258\n}\n", START "\tprogram = /other\n}\n",
        START "\tstate = waiting\n}\n", START "}\ntrailing\n",
        SPEC " = {\n\tstate = running\n\tprogram = /usr/libexec/installd\n}\n",
        SPEC " = {\n\tstate = running\n\tprogram = /other\n\tpid = 258\n}\n",
        SPEC " = {\n\tstate = stopped\n\tprogram = /usr/libexec/installd\n\tpid = 258\n}\n",
        "user/502/com.apple.mobile.installd = {\n\tstate = running\n\tprogram = /usr/libexec/installd\n\tpid = 258\n}\n"
    };
    for (unsigned i = 0; i < sizeof(bad) / sizeof(bad[0]); ++i) {
        struct tomacco_service_state state = {-1, -1};
        if (tomacco_service_state_read(bad[i], strlen(bad[i]), SPEC, &state)) return 4;
    }
    char corrupt[sizeof(START "}\n")];
    memcpy(corrupt, START "}\n", sizeof(corrupt));
    corrupt[sizeof(SPEC) + 10] = 0;
    struct tomacco_service_state state;
    if (tomacco_service_state_read(corrupt, sizeof(corrupt) - 1, SPEC, &state)) return 5;
    return 0;
}
''')
            binary = root / "state"
            subprocess.run([compiler, "-Wall", "-Wextra", "-Werror", str(source), "-o", str(binary)],
                           check=True, capture_output=True, text=True, timeout=30)
            subprocess.run([str(binary)], check=True, timeout=10)

    def test_activation_receipt_rejects_other_payloads_and_malformed_packets(self):
        compiler = shutil.which("clang") or shutil.which("cc")
        if not compiler:
            self.skipTest("A C compiler is required to exercise the actual receipt parser")
        header = Path(__file__).resolve().parents[2] / "integrations/ios-emulator/guest-development-protocol.h"
        with tempfile.TemporaryDirectory(prefix="tomacco-activation-receipt-") as directory:
            root = Path(directory)
            harness = root / "receipt.c"
            harness.write_text('#include ' + json.dumps(str(header)) + '\n' + r'''
int main(void) {
    unsigned char hash[20], packet[26];
    for (unsigned i = 0; i < 20; ++i) hash[i] = (unsigned char)(i * 7);
    for (unsigned char state = 1; state <= 2; ++state) {
        tomacco_development_ack_encode(packet, state, hash);
        if (tomacco_development_ack_decode(packet, 25, hash) != state) return 1;
        for (unsigned length = 0; length <= 26; ++length)
            if (length != 25 && tomacco_development_ack_decode(packet, length, hash)) return 2;
        for (unsigned index = 0; index < 25; ++index) {
            packet[index] ^= 0x80;
            if (tomacco_development_ack_decode(packet, 25, hash)) return 3;
            packet[index] ^= 0x80;
        }
    }
    tomacco_development_ack_encode(packet, 0, hash);
    if (tomacco_development_ack_decode(packet, 25, hash)) return 4;
    return 0;
}
''')
            executable = root / "receipt"
            subprocess.run([compiler, "-Wall", "-Wextra", "-Werror", str(harness), "-o", str(executable)],
                           check=True, capture_output=True, text=True, timeout=30)
            subprocess.run([str(executable)], check=True, timeout=10)

    def test_activation_scan_reports_the_last_cycle_of_the_development_log(self):
        compiler = shutil.which("clang") or shutil.which("cc")
        if not compiler:
            self.skipTest("A C compiler is required to exercise the actual activation scanner")
        header = Path(__file__).resolve().parents[2] / "integrations/ios-emulator/guest-development-protocol.h"
        with tempfile.TemporaryDirectory(prefix="tomacco-activation-scan-") as directory:
            root = Path(directory)
            harness = root / "scan.c"
            harness.write_text('#include <string.h>\n#include ' + json.dumps(str(header)) + '\n' + r'''
#define CYCLE "{\"event\":\"development-setup\",\"state\":\"starting\",\"phase\":\"library-integrity\"}\n"
#define SUBMIT "{\"event\":\"development-setup\",\"state\":\"configuration-submitted\",\"pid\":232}\n"
#define ACTIVE "{\"event\":\"development-signing\",\"state\":\"active\",\"policy\":\"owned-cdhash\",\"entries\":2,\"bundle_id\":\"io.x\"}\n"
#define ACTIVATED "{\"event\":\"development-setup\",\"state\":\"activated\",\"controller_released\":true}\n"
#define FAILED "{\"event\":\"development-setup\",\"state\":\"failed\",\"phase\":\"library-activation-unconfirmed\",\"error\":60}\n"
#define LOADED "{\"event\":\"development-signing\",\"state\":\"loaded\"}\n"
#define DETAIL "{\"event\":\"development-setup\",\"state\":\"configuration-detail\",\"pid\":268,\"insert_library\":\"/x.dylib\",\"adapter_stderr_fd\":2,\"ack_fd\":198}\n"
#define PROBE "{\"event\":\"development-setup\",\"state\":\"copy-probe\",\"which\":\"copy\",\"path\":\"/x.dylib\",\"size\":50864,\"expected_size\":50864,\"dlopen\":false,\"error\":\"dlopen(/x.dylib): code signature invalid\"}\n"
static int scan(const char *s) { return tomacco_development_activation_scan(s, strlen(s)); }
int main(void) {
    if (scan("") != TOMACCO_DEVELOPMENT_ACTIVATION_ABSENT) return 1;
    if (scan("nothing here\n") != TOMACCO_DEVELOPMENT_ACTIVATION_ABSENT) return 2;
    if (scan(CYCLE SUBMIT ACTIVE) != TOMACCO_DEVELOPMENT_ACTIVATION_ACTIVATED) return 3;
    if (scan(CYCLE SUBMIT ACTIVATED) != TOMACCO_DEVELOPMENT_ACTIVATION_ACTIVATED) return 4;
    if (scan(CYCLE SUBMIT FAILED) != TOMACCO_DEVELOPMENT_ACTIVATION_FAILED) return 5;
    if (scan(CYCLE SUBMIT) != TOMACCO_DEVELOPMENT_ACTIVATION_PENDING) return 6;
    /* Only the last boot's cycle decides. */
    if (scan(CYCLE SUBMIT ACTIVE ACTIVATED CYCLE SUBMIT FAILED) != TOMACCO_DEVELOPMENT_ACTIVATION_FAILED) return 7;
    if (scan(CYCLE SUBMIT FAILED CYCLE SUBMIT ACTIVE) != TOMACCO_DEVELOPMENT_ACTIVATION_ACTIVATED) return 8;
    /* The adapter's active event wins over a later setup failure in the cycle. */
    if (scan(CYCLE SUBMIT ACTIVE FAILED) != TOMACCO_DEVELOPMENT_ACTIVATION_ACTIVATED) return 9;
    /* A truncated leading line cannot forge a marker past the last cycle. */
    if (scan("tate\":\"active\"}\n" CYCLE SUBMIT FAILED) != TOMACCO_DEVELOPMENT_ACTIVATION_FAILED) return 10;
    /* find_last on a needle longer than the text, and an empty match target. */
    if (tomacco_development_find_last("ab", 2, "abc") != -1) return 11;
    if (tomacco_development_find_last(CYCLE, strlen(CYCLE), "\"phase\":\"library-integrity\"") < 0) return 12;
    /* The loaded liveness marker is not activation: the constructor ran but has
     * not hooked installd yet, so the cycle is still pending. */
    if (scan(CYCLE SUBMIT LOADED) != TOMACCO_DEVELOPMENT_ACTIVATION_PENDING) return 13;
    if (scan(CYCLE SUBMIT LOADED ACTIVE) != TOMACCO_DEVELOPMENT_ACTIVATION_ACTIVATED) return 14;
    if (scan(CYCLE SUBMIT LOADED FAILED) != TOMACCO_DEVELOPMENT_ACTIVATION_FAILED) return 15;
    /* The configuration-detail and copy-probe diagnostics are not activation. */
    if (scan(CYCLE DETAIL PROBE SUBMIT) != TOMACCO_DEVELOPMENT_ACTIVATION_PENDING) return 16;
    if (scan(CYCLE DETAIL PROBE SUBMIT FAILED) != TOMACCO_DEVELOPMENT_ACTIVATION_FAILED) return 17;
    if (scan(CYCLE DETAIL PROBE SUBMIT LOADED ACTIVE) != TOMACCO_DEVELOPMENT_ACTIVATION_ACTIVATED) return 18;
    return 0;
}
''')
            executable = root / "scan"
            subprocess.run([compiler, "-Wall", "-Wextra", "-Werror", str(harness), "-o", str(executable)],
                           check=True, capture_output=True, text=True, timeout=30)
            subprocess.run([str(executable)], check=True, timeout=10)

    def test_native_worker_preserves_identity_without_an_operation_argument(self):
        compiler = shutil.which("clang") or shutil.which("clang-22")
        if not compiler:
            self.skipTest("Clang is required to parse the guest source's Blocks ABI")
        source = Path(__file__).resolve().parents[2] / "integrations/ios-emulator/guest-app-control.c"
        with tempfile.TemporaryDirectory(prefix="tomacco-worker-argv-") as directory:
            root = Path(directory)
            harness = root / "argv.c"
            harness.write_text('#define main guest_entry\n#include ' + json.dumps(str(source)) + '\n#undef main\n' + r'''
int main(void) {
    const char *id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    char *args[6];
    worker_arguments(args, "/guest/control", "inspect", 0, id, 0);
    unsigned argc = 0;
    while (args[argc]) ++argc;
    if (argc != 4 || strcmp(args[3], id) || worker_argument(argc, args)) return 1;
    if (!valid_command(args[1], worker_argument(argc, args))) return 2;
    if (!valid_request_id(args[3])) return 3;
    worker_arguments(args, "/guest/control", "install", "/staging/App.ipa", id, "dev.app");
    argc = 0;
    while (args[argc]) ++argc;
    if (argc != 5 || strcmp(args[3], id) || strcmp(args[4], "dev.app")) return 4;
    if (strcmp(worker_argument(argc, args), "/staging/App.ipa")) return 5;
    if (!valid_installation_identifier(args[1], args[4])) return 6;
    worker_arguments(args, "/guest/control", "lookup", "dev.app", id, 0);
    if (strcmp(worker_argument(4, args), "dev.app") || strcmp(args[3], id)) return 7;
    worker_arguments(args, "/guest/control", "inspect", 0, 0, 0);
    if (!args[2] || !args[3] || args[3][0] || args[4]) return 8;
    return 0;
}
''')
            # Link only the actual argv/validation functions. No Foundation,
            # socket, firmware, signature or native iOS execution is simulated.
            build = subprocess.run([compiler, "-O2", "-fblocks", "-ffunction-sections", "-fdata-sections",
                                    str(harness), "-Wl,--gc-sections", "-o", str(root / "argv")],
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stderr)
            subprocess.run([str(root / "argv")], check=True, timeout=5)

    def test_all_framework_workers_keep_a_correlated_result_receipt(self):
        compiler = shutil.which("clang") or shutil.which("clang-22")
        if not compiler:
            self.skipTest("Clang is required to parse the actual guest worker")
        source = Path(__file__).resolve().parents[2] / "integrations/ios-emulator/guest-app-control.c"
        with tempfile.TemporaryDirectory(prefix="tomacco-worker-receipt-") as directory:
            root = Path(directory)
            harness = root / "receipt.c"
            harness.write_text('#define main guest_entry\n#include ' + json.dumps(str(source)) + '\n#undef main\n' + r'''
int main(void) {
    if (!worker_records_result("foreground_state", 0)) return 1;
    const char *queries[] = {"inspect", "lookup", "setup", "screen_state", "display_state",
        "foreground_state", "graphics_state", "surface_state", "crash_reports", "logs", "persistence_probe"};
    const char *arguments[] = {0, "dev.app", 0, 0, 0, 0, 0, "1", "Preferences", "application",
        "read:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"};
    for (unsigned i = 0; i < sizeof(queries)/sizeof(queries[0]); ++i)
        if (!valid_command(queries[i], arguments[i]) || !worker_records_result(queries[i], arguments[i])) return 2;
    const char *mutations[] = {"install", "register", "open", "setup_finish", "restart_ui", "effects", "display_refresh", "persistence_probe", "watchdog_scale", "icon_cache"};
    const char *values[] = {"/staging/App.app", "/staging/App.app", "dev.app", 0, 0, "reduced", 0,
        "create:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "8", "clear"};
    for (unsigned i = 0; i < sizeof(mutations)/sizeof(mutations[0]); ++i)
        if (!worker_records_result(mutations[i], values[i])) return 3;
    if (worker_records_result("status", 0) || worker_records_result("cancel", 0) ||
        worker_records_result("flush", 0) || worker_records_result("logs", "platform") ||
        worker_records_result("logs", "control") || worker_records_result("logs", "operation")) return 4;
    if (worker_records_result("unknown", 0) || worker_records_result("foreground_state", "unexpected") ||
        worker_records_result("open", 0)) return 5;
    const char *scales[] = {"original", "2", "8", "10", "30"};
    for (unsigned i = 0; i < sizeof(scales)/sizeof(scales[0]); ++i)
        if (!valid_command("watchdog_scale", scales[i])) return 6;
    const char *refused[] = {"", "0", "1", "02", "31", "100", "-4", "8.5", "+8", " 8", "8 ", "Original"};
    for (unsigned i = 0; i < sizeof(refused)/sizeof(refused[0]); ++i)
        if (valid_command("watchdog_scale", refused[i])) return 7;
    if (valid_command("watchdog_scale", 0) || watchdog_scale_argument("original") != 1 ||
        watchdog_scale_argument("12") != 12) return 8;
    const char *icon_refused[] = {"", "Clear", "clear ", "reset", "clear:all"};
    for (unsigned i = 0; i < sizeof(icon_refused)/sizeof(icon_refused[0]); ++i)
        if (valid_command("icon_cache", icon_refused[i])) return 9;
    if (valid_command("icon_cache", 0) || !valid_command("icon_cache", "clear")) return 10;
    return 0;
}
''')
            result = subprocess.run([compiler, "-O2", "-fblocks", "-ffunction-sections", "-fdata-sections",
                                     str(harness), "-Wl,--gc-sections", "-o", str(root / "receipt")],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(root / "receipt")], check=True, timeout=5)

    def test_crash_report_filters_the_quiet_log_and_time_zone_names_are_validated(self):
        """crash_reports takes any report name prefix (a developer's app, not
        only seven fixed iOS names), logs reads tomacco-quiet's record, and
        time_zone takes a zone file name, never a path."""
        compiler = shutil.which("clang") or shutil.which("clang-22")
        if not compiler:
            self.skipTest("Clang is required to parse the guest source's Blocks ABI")
        source = Path(__file__).resolve().parents[2] / "integrations/ios-emulator/guest-app-control.c"
        with tempfile.TemporaryDirectory(prefix="tomacco-worker-filters-") as directory:
            root = Path(directory)
            harness = root / "filters.c"
            harness.write_text('#define main guest_entry\n#include ' + json.dumps(str(source)) + '\n#undef main\n' + r'''
int main(void) {
    const char *reports[] = {"Preferences", "locationd", "com.apple.MapKit.SnapshotService", "chronod",
        "dev.example.app", "TomaccoProbe", "a", "My_App-2"};
    for (unsigned i = 0; i < sizeof(reports)/sizeof(reports[0]); ++i)
        if (!valid_command("crash_reports", reports[i]) || !crash_process_allowed(reports[i])) return 1;
    char longest[256], too_long[257];
    for (unsigned i = 0; i < 256; ++i) { longest[i] = i < 255 ? 'a' : 0; too_long[i] = 'a'; }
    too_long[256] = 0;
    if (!crash_process_allowed(longest) || crash_process_allowed(too_long)) return 2;
    const char *refused[] = {"", ".", "..", "../locationd", "-r", ".hidden", "a/b", "a b", "a\tb", "a*", "\xc3\xa4"};
    for (unsigned i = 0; i < sizeof(refused)/sizeof(refused[0]); ++i)
        if (crash_process_allowed(refused[i]) || valid_command("crash_reports", refused[i])) return 3;
    if (crash_process_allowed(0) || valid_command("crash_reports", 0)) return 4;
    if (strcmp(log_label("quiet"), "quiet") || strcmp(log_label("application"), "research-app") ||
        strcmp(log_label("development"), "development-signing") || log_label("") || log_label("Quiet")) return 5;
    const char *zones[] = {"UTC", "Europe/Vienna", "America/Argentina/Buenos_Aires", "Etc/GMT+1", "Etc/GMT-14"};
    for (unsigned i = 0; i < sizeof(zones)/sizeof(zones[0]); ++i)
        if (!time_zone_argument(zones[i]) || !valid_command("time_zone", zones[i])) return 6;
    const char *bad_zones[] = {"", "/", "/Europe/Vienna", "Europe/", "Europe//Vienna", "../etc", "Europe/../etc",
        "a/b/c/d", "Europe/Vienna.ics", "Europe/Vi enna", "Europe\\Vienna", "Europe/Vienna\n",
        "Aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"};
    for (unsigned i = 0; i < sizeof(bad_zones)/sizeof(bad_zones[0]); ++i)
        if (time_zone_argument(bad_zones[i]) || valid_command("time_zone", bad_zones[i])) return 7;
    if (time_zone_argument(0) || valid_command("time_zone", 0) || !worker_records_result("time_zone", "UTC")) return 8;
    return 0;
}
''')
            build = subprocess.run([compiler, "-O2", "-fblocks", "-ffunction-sections", "-fdata-sections",
                                    str(harness), "-Wl,--gc-sections", "-o", str(root / "filters")],
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stderr)
            subprocess.run([str(root / "filters")], check=True, timeout=5)

    def test_auto_lock_takes_never_or_original_and_a_lost_reply_is_retry_safe(self):
        """auto_lock sets one fixed setting to Never or back to iOS's default;
        asking again is harmless, so even the worker's timeout receipt says a
        retry is safe, unlike the other settings' receipts."""
        compiler = shutil.which("clang") or shutil.which("clang-22")
        if not compiler:
            self.skipTest("Clang is required to parse the guest source's Blocks ABI")
        source = Path(__file__).resolve().parents[2] / "integrations/ios-emulator/guest-app-control.c"
        with tempfile.TemporaryDirectory(prefix="tomacco-worker-auto-lock-") as directory:
            root = Path(directory)
            harness = root / "auto-lock.c"
            harness.write_text(r'''
#define main guest_entry
#define alarm test_alarm
#define signal test_signal
''' + '#include ' + json.dumps(str(source)) + r'''
#undef main
unsigned test_alarm(unsigned seconds) { (void)seconds; return 0; }
void (*test_signal(int number, void (*handler)(int)))(int) { (void)number; return handler; }
extern char *strstr(const char *, const char *);
int main(void) {
    if (!valid_command("auto_lock", "never") || !valid_command("auto_lock", "original")) return 1;
    const char *refused[] = {"", "Never", "never ", "off", "0", "30", "default", "original\n"};
    for (unsigned i = 0; i < sizeof(refused)/sizeof(refused[0]); ++i)
        if (auto_lock_argument(refused[i]) || valid_command("auto_lock", refused[i])) return 2;
    if (auto_lock_argument(0) || valid_command("auto_lock", 0)) return 3;
    if (!worker_records_result("auto_lock", "never")) return 4;
    record_mutation = 1;
    prepare_worker_deadlines("auto_lock");
    if (!strstr(timeout_receipt, "\"automatic_retry_safe\":true") ||
        !strstr(cancel_receipt, "\"automatic_retry_safe\":true")) return 5;
    prepare_worker_deadlines("time_zone");
    if (!strstr(timeout_receipt, "\"automatic_retry_safe\":false")) return 6;
    return 0;
}
''')
            build = subprocess.run([compiler, "-O2", "-fblocks", "-ffunction-sections", "-fdata-sections",
                                    str(harness), "-Wl,--gc-sections", "-o", str(root / "auto-lock")],
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stderr)
            subprocess.run([str(root / "auto-lock")], check=True, timeout=5)

    def test_the_screen_query_ends_before_a_host_wait(self):
        """A screen query SpringBoard leaves unanswered must free the worker.

        SpringBoard answers it on its main thread; while that thread is held
        the query would keep the only framework worker for the generic bound,
        and every later request would be refused as busy."""
        compiler = shutil.which("clang") or shutil.which("clang-22")
        if not compiler:
            self.skipTest("Clang is required to parse the guest source's Blocks ABI")
        source = Path(__file__).resolve().parents[2] / "integrations/ios-emulator/guest-app-control.c"
        with tempfile.TemporaryDirectory(prefix="tomacco-worker-deadline-") as directory:
            root = Path(directory)
            harness = root / "deadline.c"
            harness.write_text(r'''
#define main guest_entry
#define alarm test_alarm
#define signal test_signal
''' + '#include ' + json.dumps(str(source)) + r'''
#undef main
static unsigned armed;
unsigned test_alarm(unsigned seconds) { armed = seconds; return 0; }
void (*test_signal(int number, void (*handler)(int)))(int) { (void)number; return handler; }
static int bound(const char *operation) { armed = 0; prepare_worker_deadlines(operation); return (int)armed; }
int main(void) {
    if (bound("screen_state") != 20) return 1;
    if (bound("install") != 600) return 2;
    if (bound("open") != 120 || bound("icon_cache") != 120) return 3;
    return 0;
}
''')
            build = subprocess.run([compiler, "-O2", "-fblocks", "-ffunction-sections", "-fdata-sections",
                                    str(harness), "-Wl,--gc-sections", "-o", str(root / "deadline")],
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stderr)
            subprocess.run([str(root / "deadline")], check=True, timeout=5)

    def test_an_install_request_never_goes_through_a_closed_installer_client(self):
        """MIInstallerClient clears its connection when installd's ends.

        A request sent through such a client is a message to nil: it never
        reaches installd and never completes, so the worker would wait out its
        whole deadline. Each request gets a client made for it, and
        a client without a connection is refused."""
        compiler = shutil.which("clang") or shutil.which("clang-22")
        if not compiler:
            self.skipTest("Clang is required to parse the guest source's Blocks ABI")
        source = Path(__file__).resolve().parents[2] / "integrations/ios-emulator/guest-app-control.c"
        with tempfile.TemporaryDirectory(prefix="tomacco-installer-client-") as directory:
            root = Path(directory)
            harness = root / "installer.c"
            harness.write_text('#define main guest_entry\n#include ' + json.dumps(str(source)) + '\n#undef main\n' + r'''
struct fake { const char *kind; struct fake *connection; };
static struct fake installer_class = {"class", 0}, live = {"connection", 0}, clients[4];
static int connected[4];
static unsigned made;
static object fake_selector(const char *name) { return (object)name; }
static object fake_class(object receiver) { return receiver; }
static object fake_method(object receiver, object name) {
    const struct fake *value = receiver;
    if (!strcmp(value->kind, "class") && !strcmp(name, "installerWithProgressBlock:")) return name;
    if (!strcmp(value->kind, "client") && !strcmp(name, "connection")) return name;
    return 0;
}
static unsigned fake_count(object method) { return strcmp(method, "connection") ? 3 : 2; }
static void fake_return(object method, char *type, unsigned long size) { (void)method; snprintf(type, size, "@"); }
static void fake_argument(object method, unsigned index, char *type, unsigned long size) {
    (void)method; snprintf(type, size, "%s", index == 0 ? "@" : index == 1 ? ":" : "@?");
}
static object fake_send(object receiver, object name, object argument) {
    (void)argument;
    if (!strcmp(name, "installerWithProgressBlock:")) {
        struct fake *client = &clients[made];
        client->kind = "client";
        client->connection = connected[made] ? &live : 0;
        ++made;
        return client;
    }
    if (!strcmp(name, "connection")) return ((struct fake *)receiver)->connection;
    return 0;
}
int main(void) {
    selector = fake_selector; object_class = fake_class; instance_method = fake_method;
    argument_count = fake_count; return_type = fake_return; argument_type = fake_argument;
    message = (void (*)(void))fake_send;
    const char *factory = "installerWithProgressBlock:";
    connected[0] = 1; connected[1] = 0; connected[2] = 1;
    object first = connected_installer(&installer_class, factory, 0);
    if (first != &clients[0]) return 1;
    /* A client whose connection installd ended is refused, not awaited. */
    if (connected_installer(&installer_class, factory, 0)) return 2;
    /* The request never reuses an earlier client, even a connected one. */
    object request = connected_installer(&installer_class, factory, 0);
    if (request != &clients[2] || request == first || made != 3) return 3;
    return 0;
}
''')
            build = subprocess.run([compiler, "-O2", "-fblocks", "-ffunction-sections", "-fdata-sections",
                                    str(harness), "-Wl,--gc-sections", "-o", str(root / "installer")],
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stderr)
            subprocess.run([str(root / "installer")], check=True, timeout=5)

    def test_an_install_ends_only_the_running_copy_of_the_app_it_replaces(self):
        """installd leaves a running copy of the replaced app alone.

        Once installd has finished, the install ends that bundle's processes
        through RunningBoard, records the outcome, and never sends anything to
        an API of another shape or fails the install over it."""
        compiler = shutil.which("clang") or shutil.which("clang-22")
        if not compiler:
            self.skipTest("Clang is required to parse the guest source's Blocks ABI")
        source = Path(__file__).resolve().parents[2] / "integrations/ios-emulator/guest-app-control.c"
        with tempfile.TemporaryDirectory(prefix="tomacco-install-termination-") as directory:
            root = Path(directory)
            harness = root / "terminate.c"
            harness.write_text('#define main guest_entry\n#define dlopen test_dlopen\n#include ' +
                               json.dumps(str(source)) + '\n#undef main\n#undef dlopen\n' + r'''
struct fake { const char *kind; const char *text; };
static struct fake predicate_class = {"RBSProcessPredicate", 0}, context_class = {"RBSTerminateContext", 0},
    request_class = {"RBSTerminateRequest", 0}, string_class = {"NSString", 0}, number_class = {"NSNumber", 0},
    null_class = {"NSNull", 0}, row = {"row", 0}, predicate = {"predicate", 0}, context = {"context", 0},
    request = {"request", 0}, yes = {"yes", 0}, no = {"no", 0}, null_value = {"null", 0}, strings[64];
static unsigned string_count, stored;
static int framework, api, executes, released;
static const char *predicate_identifier, *keys[16];
static struct fake *values[16];
void *test_dlopen(const char *path, int mode) { (void)path; (void)mode; return framework ? &row : 0; }
static object fake_class_named(const char *name) {
    struct fake *all[] = {&predicate_class, &context_class, &request_class, &string_class, &number_class, &null_class};
    for (unsigned i = 0; i < sizeof(all) / sizeof(all[0]); ++i) if (!strcmp(all[i]->kind, name)) return all[i];
    return 0;
}
static object fake_selector(const char *name) { return (object)name; }
static object fake_object_class(object receiver) { return receiver; }
static object fake_method(object receiver, object name) {
    if (!api) return 0;
    if (receiver == &predicate_class && !strcmp(name, "predicateMatchingBundleIdentifier:")) return name;
    if (receiver == &context_class && !strcmp(name, "defaultContextWithExplanation:")) return name;
    if (receiver == &request && (!strcmp(name, "initWithPredicate:context:") || !strcmp(name, "execute:"))) return name;
    return 0;
}
static unsigned fake_count(object method) { return strcmp(method, "initWithPredicate:context:") ? 3 : 4; }
static void fake_return(object method, char *type, unsigned long size) {
    snprintf(type, size, "%s", strcmp(method, "execute:") ? "@" : "B");
}
static void fake_argument(object method, unsigned index, char *type, unsigned long size) {
    /* What the runtime reports for -execute:(out NSError **): the qualifier is
     * part of the type. */
    snprintf(type, size, "%s", index == 0 ? "@" : index == 1 ? ":" : strcmp(method, "execute:") ? "@" : "o^@");
}
static object fake_send(object receiver, object name, object first, object second) {
    if (!strcmp(name, "stringWithUTF8String:")) {
        struct fake *value = &strings[string_count++ % 64];
        value->kind = "string"; value->text = first;
        return value;
    }
    if (!strcmp(name, "numberWithBool:")) return ((unsigned long)first & 0xff) ? &yes : &no;
    if (!strcmp(name, "null")) return &null_value;
    if (!strcmp(name, "setObject:forKey:")) {
        if (receiver != &row || stored == 16) return 0;
        keys[stored] = ((struct fake *)second)->text; values[stored++] = first;
        return 0;
    }
    if (!strcmp(name, "predicateMatchingBundleIdentifier:")) {
        predicate_identifier = ((struct fake *)first)->text;
        return &predicate;
    }
    if (!strcmp(name, "defaultContextWithExplanation:")) return &context;
    if (!strcmp(name, "alloc")) return receiver == &request_class ? &request : 0;
    if (!strcmp(name, "initWithPredicate:context:")) return first == &predicate && second == &context ? &request : 0;
    if (!strcmp(name, "execute:")) { ++executes; return (object)1; }
    if (!strcmp(name, "release")) { ++released; return 0; }
    return 0;
}
static struct fake *recorded(const char *key) {
    for (unsigned i = 0; i < stored; ++i) if (!strcmp(keys[i], key)) return values[i];
    return 0;
}
int main(void) {
    get_class = fake_class_named; selector = fake_selector; object_class = fake_object_class;
    instance_method = fake_method; argument_count = fake_count;
    return_type = fake_return; argument_type = fake_argument;
    message = (void (*)(void))fake_send;
    struct fake identifier = {"string", "io.example.app"};
    end_running_application(&row, &identifier);
    struct fake *reason = recorded("termination_error");
    if (recorded("running_application_ended") != &no || executes ||
        !reason || strcmp(reason->text, "termination_framework_unavailable")) return 1;
    stored = 0; framework = 1;
    end_running_application(&row, &identifier);
    reason = recorded("termination_error");
    if (recorded("running_application_ended") != &no || executes ||
        !reason || strcmp(reason->text, "incompatible_termination_api")) return 2;
    stored = 0; api = 1;
    end_running_application(&row, &identifier);
    if (recorded("running_application_ended") != &yes || executes != 1 || released != 1 ||
        recorded("termination_error")) return 3;
    if (!predicate_identifier || strcmp(predicate_identifier, "io.example.app")) return 4;
    return 0;
}
''')
            build = subprocess.run([compiler, "-O2", "-fblocks", "-ffunction-sections", "-fdata-sections",
                                    str(harness), "-Wl,--gc-sections", "-o", str(root / "terminate")],
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stderr)
            subprocess.run([str(root / "terminate")], check=True, timeout=5)

    def test_late_signals_cannot_replace_a_definitive_worker_receipt(self):
        compiler = shutil.which("clang") or shutil.which("clang-22")
        if not compiler:
            self.skipTest("Clang is required to parse the guest source's Blocks ABI")
        source = Path(__file__).resolve().parents[2] / "integrations/ios-emulator/guest-app-control.c"
        with tempfile.TemporaryDirectory(prefix="tomacco-worker-signal-") as directory:
            root = Path(directory)
            harness = root / "signal.c"
            harness.write_text(r'''
#define main guest_entry
#define _exit test_exit
#define open test_open
#define write test_write
#define signal test_signal
#define alarm test_alarm
''' + '#include ' + json.dumps(str(source)) + r'''
#undef main
#include <setjmp.h>
static jmp_buf finished;
static int exit_code, opened, written, alarms;
static char output[2048];
void test_exit(int code) { exit_code = code; longjmp(finished, 1); }
int test_open(const char *path, int flags, ...) { (void)path; (void)flags; ++opened; return -1; }
long test_write(int fd, const void *bytes, unsigned long length) {
    (void)fd;
    for (unsigned long i = 0; i < length; ++i) output[written++] = ((const char *)bytes)[i];
    output[written] = 0;
    return (long)length;
}
int *__error(void) { static int error; return &error; }
unsigned test_alarm(unsigned seconds) { ++alarms; return seconds; }
void (*test_signal(int number, void (*handler)(int)))(int) { (void)number; return handler; }
int main(void) {
    record_result = 1;
    request_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    timeout_length = (unsigned long)snprintf(timeout_receipt, sizeof(timeout_receipt), "timeout");
    cancel_length = (unsigned long)snprintf(cancel_receipt, sizeof(cancel_receipt), "cancel");
    for (int committed = 0; committed < 2; ++committed) {
        for (int signum = 14; signum <= 15; ++signum) {
            terminal_response_started = committed;
            opened = written = alarms = 0;
            if (!setjmp(finished)) interrupted_worker(signum);
            if (exit_code != (signum == 14 ? 124 : 143)) return 1;
            if (committed) {
                if (opened || written || alarms) return 2;
            } else {
                if (opened != 1 || alarms != 1) return 3;
                if (strcmp(output, signum == 14 ? "TOMACCO_APP_CONTROL timeout\n"
                                               : "TOMACCO_APP_CONTROL cancel\n")) return 4;
            }
        }
    }
    return 0;
}
''')
            build = subprocess.run([compiler, "-O2", "-fblocks", "-ffunction-sections", "-fdata-sections",
                                    str(harness), "-Wl,--gc-sections", "-o", str(root / "signal")],
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stderr)
            subprocess.run([str(root / "signal")], check=True, timeout=5)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def cache(entries=()):
    return struct.pack("<I16sI", 2, bytes(16), len(entries)) + b"".join(entries)


def signed_fixture(identifier, entitlements=None, der=None, sealed_files=None, *, cpu_subtype=0, file_type=2):
    # Minimal CodeDirectory for integrity testing, not an executable program.
    identifier = identifier.encode() + b"\0"
    special = 7 if der is not None else 5 if entitlements is not None else 0
    seals = sealed_files or {}
    special = max([special, *seals])
    xml = plistlib.dumps(entitlements) if entitlements is not None else b""
    blob = struct.pack(">II", 0xfade7171, 8 + len(xml)) + xml if xml else b""
    cd_size = 44 + len(identifier) + special * 32 + 32
    blobs = {5: blob} if blob else {}
    if der is not None:
        blobs[7] = struct.pack(">II", 0xfade7172, 8 + len(der)) + der
    table_size = 12 + 8 * (1 + len(blobs))
    total = table_size + cd_size + sum(map(len, blobs.values()))
    data = struct.pack("<8I", 0xfeedfacf, 0x100000c, cpu_subtype, file_type, 1, 16, 0, 0)
    data += struct.pack("<4I", 0x1d, 16, 48, total)
    cd = struct.pack(">9I4BI", 0xfade0c02, cd_size, 0x20001, 2, 44 + len(identifier) + special * 32,
                     44, special, 1, 48, 32, 2, 0, 12, 0)
    cd += identifier
    for slot in range(special, 0, -1):
        cd += hashlib.sha256(blobs[slot] if slot in blobs else seals[slot]).digest() if slot in blobs or slot in seals else bytes(32)
    cd += hashlib.sha256(data).digest()
    table = struct.pack(">5I", 0xfade0cc0, total, 1 + len(blobs), 0, table_size)
    position = table_size + cd_size
    for slot, value in blobs.items():
        table += struct.pack(">II", slot, position)
        position += len(value)
    return data + table + cd + b"".join(blobs.values())


class GuestToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def fixture(self, quiet=False, battery=False):
        programs, files = [], {}
        for name in (("tomacco-platform", "tomacco-app-control") + (("tomacco-quiet",) if quiet else ())
                     + (("tomacco-battery",) if battery else ())):
            path = "usr/libexec/" + name
            data = signed_fixture(name)
            integrity = inspect_signed_code(data, cpu_subtype=0, file_type=2)
            programs.append({"path": path, "sha256": digest(data), "cdhash": integrity["cdhash"],
                             "integrity": integrity})
            files[path] = data
        for job in launch_jobs(quiet_services=quiet, virtual_battery=battery):
            files["System/Library/LaunchDaemons/" + job["Label"] + ".plist"] = plistlib.dumps(job)
        original = cache([b"A" * 20 + bytes.fromhex("02000200")])
        combined = extend_trustcache(original, [bytes.fromhex(row["cdhash"]) for row in programs])
        record = {"format": 1, "scope": "ios-owned-guest-tools", "state": "built",
                  "guest_root": GUEST_ROOT, "target": "arm64-apple-ios27.0", "programs": programs,
                  "jobs": launch_jobs(quiet_services=quiet, virtual_battery=battery),
                  "files": [{"path": name, "bytes": len(data), "sha256": digest(data),
                  "mode": 0o755 if name.startswith("usr/") else 0o644} for name, data in files.items()],
                  "trustcache": {"source_sha256": digest(original), "sha256": digest(combined),
                                 "original_entries_preserved": True, "local_platform_policy": LOCAL_PLATFORM_POLICY.hex()}}
        if quiet:
            record["quiet_services"] = True
        if battery:
            record["virtual_battery"] = True
        for name, data in files.items():
            path = self.root / "Supplemental" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (self.root / "system.tc").write_bytes(combined)
        self.save(record)
        return original, record

    def save(self, record):
        (self.root / "guest-tools.json").write_text(json.dumps(record))

    def test_agent_install_capability_is_signed_and_app_launch_is_explicit(self):
        grant = program_entitlements("usr/libexec/tomacco-app-control", True)
        self.assertEqual(grant["com.apple.private.mobileinstall.allowedSPI"], ["InstallForInstallCoordination"])
        connected = program_entitlements("usr/libexec/tomacco-app-control", True, installer_connection_query=True)
        self.assertEqual(connected["com.apple.private.mobileinstall.allowedSPI"],
                         ["InstallForInstallCoordination", "GetSystemAppMigrationStatus"])
        # Ending the replaced app is a separate, recorded grant: older payloads
        # without it still install, and no other program ever receives it.
        self.assertNotIn("com.apple.runningboard.terminateprocess", grant)
        ending = program_entitlements("usr/libexec/tomacco-app-control", True, application_termination=True)
        self.assertIs(ending["com.apple.runningboard.terminateprocess"], True)
        self.assertEqual(program_entitlements("usr/libexec/tomacco-app-control", False,
                                              application_termination=True), {})
        # Setting the guest's time zone through tzlinkd is the third recorded
        # grant, again for the control service only.
        self.assertNotIn("com.apple.tzlink.allow", grant)
        self.assertNotIn("com.apple.tzlink.allow", ending)
        zoned = program_entitlements("usr/libexec/tomacco-app-control", True, time_zone_control=True)
        self.assertIs(zoned["com.apple.tzlink.allow"], True)
        self.assertEqual({key: value for key, value in zoned.items() if key != "com.apple.tzlink.allow"}, grant)
        everything = program_entitlements("usr/libexec/tomacco-app-control", True, True,
                                          installer_connection_query=True, application_log_access=True,
                                          graphics_query_access=True, application_termination=True,
                                          time_zone_control=True)
        self.assertEqual(sorted(everything), ["com.apple.private.mobileinstall.allowedSPI",
                                              "com.apple.private.security.storage.AppDataContainers",
                                              "com.apple.runningboard.terminateprocess",
                                              "com.apple.security.iokit-user-client-class",
                                              "com.apple.springboard.launchapplications",
                                              "com.apple.tzlink.allow"])
        for other in ("usr/libexec/tomacco-platform", "usr/libexec/tomacco-quiet", "usr/libexec/tomacco-development-setup",
                      "usr/libexec/TomaccoProbe.app/TomaccoProbe"):
            self.assertNotIn("com.apple.tzlink.allow", program_entitlements(other, True, True, time_zone_control=True))
        self.assertEqual(program_entitlements("usr/libexec/tomacco-app-control", False, time_zone_control=True), {})
        # Storing Settings' Auto-Lock through profiled is the fourth recorded
        # grant, again for the control service only and only with the agent.
        self.assertNotIn("com.apple.managedconfiguration.profiled-access", everything)
        locked = program_entitlements("usr/libexec/tomacco-app-control", True, auto_lock_control=True)
        self.assertIs(locked["com.apple.managedconfiguration.profiled-access"], True)
        self.assertEqual({key: value for key, value in locked.items()
                          if key != "com.apple.managedconfiguration.profiled-access"}, grant)
        for other in ("usr/libexec/tomacco-platform", "usr/libexec/tomacco-quiet", "usr/libexec/tomacco-development-setup",
                      "usr/libexec/TomaccoProbe.app/TomaccoProbe", "usr/libexec/tomacco-battery"):
            self.assertNotIn("com.apple.managedconfiguration.profiled-access",
                             program_entitlements(other, True, True, auto_lock_control=True))
        self.assertEqual(program_entitlements("usr/libexec/tomacco-app-control", False, auto_lock_control=True), {})
        self.assertEqual(program_entitlements("usr/libexec/TomaccoProbe.app/TomaccoProbe", True,
                                              application_termination=True), {})
        self.assertEqual(program_entitlements("usr/libexec/TomaccoProbe.app/TomaccoProbe", True,
                                             installer_connection_query=True), {})
        self.assertEqual(program_entitlements("usr/libexec/TomaccoProbe.app/TomaccoProbe", True), {})
        self.assertEqual(program_entitlements("usr/libexec/tomacco-app-control", False), {})
        self.assertEqual(len(launch_jobs(application_probe=True, agent=True)), 2)
        data = signed_fixture("control", grant)
        inspect_signed_code(data, cpu_subtype=0, file_type=2, expected_entitlements=grant)
        with self.assertRaisesRegex(ValueError, "allowed guest capabilities"):
            inspect_signed_code(data, cpu_subtype=0, file_type=2, expected_entitlements={})
        tampered = data.replace(b"Install", b"Uninstl")
        with self.assertRaisesRegex(ValueError, "signed entitlements changed"):
            inspect_signed_code(tampered, cpu_subtype=0, file_type=2, expected_entitlements=grant)
        dual = signed_fixture("control", grant, guest_entitlements_der(grant))
        inspect_signed_code(dual, cpu_subtype=0, file_type=2, expected_entitlements=grant)
        # Both blobs and CodeDirectory are individually intact, but DER must
        # not silently grant capabilities absent from the XML dictionary.
        conflict = signed_fixture("control", grant, guest_entitlements_der({**grant, "extra": True}))
        with self.assertRaisesRegex(ValueError, "allowed guest capabilities"):
            inspect_signed_code(conflict, cpu_subtype=0, file_type=2, expected_entitlements=grant)

    def test_development_signing_refuses_a_compiler_it_was_never_seen_working_with(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory(prefix="tomacco-compiler-") as directory:
            root = Path(directory)
            (root / "original.tc").write_bytes(cache())
            compilers = {}
            for version in ("22.1.8", "23.1.3"):
                path = root / f"clang-{version}"
                # Answers --version only: a build that got past the check
                # would fail on its first compile.
                path.write_text(f"#!/bin/sh\n[ \"$1\" = --version ] || exit 3\n"
                                f"echo 'clang version {version}'\necho 'Target: x86_64-pc-linux-gnu'\n")
                path.chmod(0o755)
                compilers[version] = path
            self.assertEqual(compiler_identity(compilers["23.1.3"]),
                             {"version": "23.1.3", "major": 23, "banner": "clang version 23.1.3"})
            def attempt(version, name, **extra):
                return build(SimpleNamespace(
                    output=root / name, trustcache=root / "original.tc", linker=compilers[version],
                    ldid=compilers[version], clang=compilers[version], minimum_ios="27.0", agent=True,
                    application_probe=True, development_signing=True, **extra))
            with self.assertRaisesRegex(ValueError, "qualified only with clang 22; a clang 23.1.3 build"):
                attempt("23.1.3", "refused")
            self.assertFalse((root / "refused").exists())
            # Past the check both reach the compiler, which these stand-ins refuse.
            for version, name, extra in (("22.1.8", "qualified", {}),
                                         ("23.1.3", "override", {"allow_unqualified_compiler": True})):
                with self.assertRaises(subprocess.CalledProcessError):
                    attempt(version, name, **extra)
            not_clang = root / "cc"
            not_clang.write_text("#!/bin/sh\necho 'gcc (GCC) 15.2.1'\n")
            not_clang.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "names no clang release"):
                compiler_identity(not_clang)

    def test_development_policy_is_opt_in_and_keeps_app_privileges_separate(self):
        from types import SimpleNamespace
        for agent, application in ((False, False), (True, False), (False, True)):
            with self.assertRaisesRegex(ValueError, "requires agent and application_probe"):
                build(SimpleNamespace(development_signing=True, agent=agent, application_probe=application))
        jobs = launch_jobs(True, True, True)
        self.assertEqual(len(jobs), 3)
        setup = jobs[-1]
        self.assertEqual(setup["UserName"], "root")
        self.assertEqual(setup["ProcessType"], "Interactive")
        self.assertTrue(setup["LaunchOnlyOnce"])
        self.assertNotIn("EnvironmentVariables", setup)
        self.assertEqual(setup["ProgramArguments"], [GUEST_ROOT + "/usr/libexec/tomacco-development-setup"])
        self.assertEqual(program_entitlements("usr/libexec/tomacco-development-setup", True, True),
                         {"com.apple.private.xpc.service-attach": True})
        self.assertEqual(program_entitlements("usr/libexec/tomacco-development-setup", True, False), {})
        app = program_entitlements("usr/libexec/TomaccoProbe.app/TomaccoProbe", True, True)
        self.assertEqual(app, {"application-identifier": "io.github.marselnenaj.tomacco.research-app"})
        self.assertEqual(program_entitlements("usr/libexec/tomacco-platform", True, True), {})

    def test_the_control_service_stays_resident_for_the_whole_boot(self):
        # An idle alarm would end the service an hour into a session, and
        # launchd, told to run it once, would never bring it back, so every
        # save-and-stop after that would be refused.
        control = launch_jobs(True, True)[1]
        self.assertEqual(control["Label"], "io.github.marselnenaj.tomacco.application-inventory")
        self.assertIs(control["KeepAlive"], True)
        self.assertNotIn("LaunchOnlyOnce", control)
        # Only the control service changes; the inspection-only service and
        # every other job keep running once.
        self.assertTrue(launch_jobs(True, False)[1]["LaunchOnlyOnce"])
        self.assertNotIn("KeepAlive", launch_jobs(True, False)[1])
        for other in launch_jobs(True, True, True, True):
            if other["Label"] != control["Label"]:
                self.assertTrue(other["LaunchOnlyOnce"])
                self.assertNotIn("KeepAlive", other)
        source = (Path(__file__).resolve().parents[2] / "integrations/ios-emulator/guest-app-control.c").read_text()
        serve = source[source.index("for (;;) {", source.index('response("serve")')):]
        idle = serve[:serve.index("accept_fn(server")]
        self.assertIn("alarm(0);", idle)
        self.assertNotIn("alarm(3600)", source)

    def test_service_quieting_is_opt_in_and_one_root_job_without_privileges(self):
        self.assertEqual(launch_jobs(True, True, True, False), launch_jobs(True, True, True))
        jobs = launch_jobs(True, True, True, True)
        self.assertEqual(jobs[:-1], launch_jobs(True, True, True))
        quiet = jobs[-1]
        self.assertEqual(quiet["Label"], "io.github.marselnenaj.tomacco.quiet")
        self.assertEqual(quiet["ProgramArguments"], [GUEST_ROOT + "/usr/libexec/tomacco-quiet"])
        self.assertEqual((quiet["UserName"], quiet["ProcessType"]), ("root", "Interactive"))
        self.assertTrue(quiet["RunAtLoad"] and quiet["LaunchOnlyOnce"])
        self.assertNotIn("KeepAlive", quiet)
        self.assertEqual(program_entitlements("usr/libexec/tomacco-quiet", True, True), {})
        # The helper's service list is the whole of what it may touch: each
        # entry is a service that can only fail on this machine, and nothing
        # is written to the guest's disk.
        source = (Path(__file__).resolve().parents[2] / "integrations/ios-emulator/guest-quiet.c").read_text()
        listed = source[source.index("services[] = {"):source.index("};", source.index("services[] = {"))]
        # chronod is not among them: the widgets it renders are drawn here.
        self.assertEqual(re.findall(r'"([^"]+)"', listed), [
            "com.apple.duetexpertd", "com.apple.intelligencetasksd",
            "com.apple.icloud.searchpartyd", "com.apple.transparencyd", "com.apple.watchdogd",
            "com.apple.accessibility.axassetsd"])
        code = source.split("*/", 1)[1]
        for forbidden in ("fopen", "open", "creat", "unlink", "rename", "system", "posix_spawn", "execv", "fork"):
            self.assertIsNone(re.search(r"\b%s\w*\s*\(" % forbidden, code), forbidden)
        # The locationd guard may end one process, by its exact name, and
        # nothing else: a single kill call, fed from that one name.
        self.assertEqual(re.findall(r'guarded_process\[\] = "([^"]+)"', code), ["locationd"])
        self.assertEqual(len(re.findall(r"\bkill\s*\(", code)), 2)  # declaration and one call
        self.assertIn("kill(asked[index], 9)", code)

    def test_the_locationd_guard_ends_only_a_silent_locationd_and_spaces_its_restarts(self):
        """A locationd that answers is left alone; one that stops answering
        is ended after the deadline, only it (not a process whose name merely
        starts the same), and never twice within the spacing."""
        compiler = shutil.which("clang") or shutil.which("cc")
        if not compiler:
            self.skipTest("A C compiler is required")
        source = Path(__file__).resolve().parents[2] / "integrations/ios-emulator/guest-quiet.c"
        with tempfile.TemporaryDirectory(prefix="tomacco-quiet-guard-") as directory:
            root = Path(directory)
            harness = root / "guard.c"
            harness.write_text('#include <pthread.h>\n#include <stdarg.h>\n#include <stdio.h>\n#include <stdlib.h>\n'
                               '#include <string.h>\n#include <unistd.h>\n'
                               '#define main guest_entry\n#define dlopen test_dlopen\n#define dlsym test_dlsym\n'
                               '#define sleep test_sleep\n#define alarm test_alarm\n#define getuid test_getuid\n'
                               '#define kill test_kill\n#define dprintf test_dprintf\n#include ' +
                               json.dumps(str(source)) + '\n#undef main\n#undef dlopen\n#undef dlsym\n#undef sleep\n'
                               '#undef alarm\n#undef getuid\n#undef kill\n#undef dprintf\n' + r'''
static pthread_t main_thread;
static unsigned long clock_now;   /* guest seconds, advanced by the guard's sleeps */
static int hang;                  /* 1: locationd stops answering */
static unsigned kills, killed_pids[8], killed_signals[8];
static unsigned long kill_times[8];
static pthread_mutex_t output = PTHREAD_MUTEX_INITIALIZER;
int test_dprintf(int fd, const char *format, ...) {
    char text[512];
    va_list list;
    va_start(list, format);
    vsnprintf(text, sizeof(text), format, list);
    va_end(list);
    text[strcspn(text, "\n")] = 0;
    pthread_mutex_lock(&output);
    printf("{\"clock\":%lu,\"line\":%s}\n", __atomic_load_n(&clock_now, __ATOMIC_SEQ_CST), text);
    fflush(stdout);
    pthread_mutex_unlock(&output);
    (void)fd;
    return 0;
}
unsigned test_getuid(void) { return 0; }
unsigned test_alarm(unsigned seconds) { (void)seconds; return 0; }
unsigned test_sleep(unsigned seconds) {
    if (pthread_equal(pthread_self(), main_thread)) { usleep(200); return 0; }
    /* Each guest sleep lets the probe threads run before the clock moves on. */
    usleep(500);
    unsigned long now = __atomic_add_fetch(&clock_now, seconds, __ATOMIC_SEQ_CST);
    if (now >= 200) __atomic_store_n(&hang, 1, __ATOMIC_SEQ_CST);
    if (now >= 3000) {
        pthread_mutex_lock(&output);
        printf("{\"kills\":[");
        for (unsigned i = 0; i < kills && i < 8; ++i)
            printf("%s[%u,%u,%lu]", i ? "," : "", killed_pids[i], killed_signals[i], kill_times[i]);
        printf("]}\n");
        fflush(stdout);
        _exit(0);
    }
    return 0;
}
int test_kill(int pid, int signal) {
    unsigned index = __atomic_load_n(&kills, __ATOMIC_SEQ_CST);
    if (index < 8) {
        killed_pids[index] = (unsigned)pid;
        killed_signals[index] = (unsigned)signal;
        kill_times[index] = __atomic_load_n(&clock_now, __ATOMIC_SEQ_CST);
    }
    __atomic_add_fetch(&kills, 1, __ATOMIC_SEQ_CST);
    return 0;
}
static int system_token, runtime_token, location_token, xpc_token;
static int fake_class = 1, dictionary_value = 1, dictionary_type_value = 1;
void *test_dlopen(const char *path, int mode) {
    (void)mode;
    if (strstr(path, "libSystem")) return &system_token;
    if (strstr(path, "libobjc")) return &runtime_token;
    if (strstr(path, "CoreLocation")) return &location_token;
    return &xpc_token;
}
static object get_class_stub(const char *name) { return strcmp(name, "CLLocationManager") ? 0 : &fake_class; }
static void *selector_stub(const char *name) { return (void *)name; }
static unsigned char ask_stub(object receiver, void *name) {
    if (receiver != &fake_class || strcmp(name, "locationServicesEnabled")) abort();
    unsigned seen = __atomic_load_n(&kills, __ATOMIC_SEQ_CST);
    /* A hung locationd answers nothing until it is ended; the question then fails. */
    while (__atomic_load_n(&hang, __ATOMIC_SEQ_CST) && __atomic_load_n(&kills, __ATOMIC_SEQ_CST) == seen) usleep(50);
    return 1;
}
static void *pool_push_stub(void) { return &fake_class; }
static void pool_pop_stub(void *pool) { (void)pool; }
static int list_stub(int *pids, int size) {
    if (size < (int)(3 * sizeof(int))) return -1;
    pids[0] = 1; pids[1] = 81; pids[2] = 200;
    return 3;
}
static int name_stub(int pid, char *name, unsigned size) {
    const char *value = pid == 1 ? "launchd" : pid == 81 ? "locationd" : pid == 200 ? "locationdx" : "";
    snprintf(name, size, "%s", value);
    return (int)strlen(value);
}
static int create_stub(thread_handle *thread, const void *attributes, void *(*entry)(void *), void *argument) {
    (void)attributes;
    return pthread_create((pthread_t *)thread, 0, entry, argument);
}
static int detach_stub(thread_handle thread) { return pthread_detach((pthread_t)thread); }
static object dictionary_stub(const char *const *keys, const object *values, unsigned long count) {
    (void)keys; (void)values; (void)count;
    return &dictionary_value;
}
static void u64_stub(object o, const char *k, unsigned long long v) { (void)o; (void)k; (void)v; }
static void string_stub(object o, const char *k, const char *v) { (void)o; (void)k; (void)v; }
static int routine_stub(unsigned long long r, object request, object *reply) {
    (void)r; (void)request; (void)reply;
    return 113;
}
static long long integer_stub(object o, const char *k) { (void)o; (void)k; return 0; }
static object get_stub(object o, const char *k) { (void)o; (void)k; return 0; }
static object type_stub(object o) { (void)o; return &dictionary_type_value; }
static void release_stub(object o) { (void)o; }
void *test_dlsym(void *library, const char *name) {
    struct { const char *name; void *value; } table[] = {
        {"objc_getClass", (void *)get_class_stub}, {"sel_registerName", (void *)selector_stub},
        {"objc_msgSend", (void *)ask_stub}, {"objc_autoreleasePoolPush", (void *)pool_push_stub},
        {"objc_autoreleasePoolPop", (void *)pool_pop_stub}, {"proc_listallpids", (void *)list_stub},
        {"proc_name", (void *)name_stub}, {"pthread_create", (void *)create_stub},
        {"pthread_detach", (void *)detach_stub}, {"xpc_dictionary_create", (void *)dictionary_stub},
        {"xpc_dictionary_set_uint64", (void *)u64_stub}, {"xpc_dictionary_set_string", (void *)string_stub},
        {"_xpc_service_routine", (void *)routine_stub}, {"xpc_dictionary_get_int64", (void *)integer_stub},
        {"xpc_dictionary_get_value", (void *)get_stub}, {"xpc_get_type", (void *)type_stub},
        {"_xpc_type_dictionary", (void *)&dictionary_type_value}, {"xpc_release", (void *)release_stub},
    };
    (void)library;
    for (unsigned i = 0; i < sizeof(table) / sizeof(table[0]); ++i)
        if (!strcmp(table[i].name, name)) return table[i].value;
    return 0;
}
int main(void) {
    main_thread = pthread_self();
    return guest_entry();
}
''')
            build = subprocess.run([compiler, "-O2", "-Wall", "-Wno-unused-function", str(harness), "-lpthread",
                                    "-o", str(root / "guard")], capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(root / "guard")], capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr)
            rows = [json.loads(line) for line in run.stdout.splitlines()]
            guard = [row for row in rows if isinstance(row.get("line"), dict) and row["line"].get("event") == "guard"]
            self.assertEqual(guard[0]["line"], {"event": "guard", "service": "locationd", "state": "watching",
                                                "start": 120, "interval": 30, "deadline": 90})
            kills = rows[-1]["kills"]
            # Only pid 81 is locationd; pid 200 only shares a prefix.
            self.assertTrue(kills and all(pid == 81 and signal == 9 for pid, signal, _ in kills), kills)
            # Answers before the hang (from guest second 200) end nothing; the
            # first silence is acted on after the 90-second deadline.
            self.assertGreaterEqual(kills[0][2], 200 + 90)
            self.assertLessEqual(kills[0][2], 200 + 30 + 90 + 2)
            for earlier, later in zip(kills, kills[1:]):
                self.assertGreaterEqual(later[2] - earlier[2], 300)
            self.assertGreaterEqual(len(kills), 2)
            states = [row["line"]["state"] for row in guard]
            self.assertIn("unanswered", states)
            self.assertIn("ended", states)
            self.assertIn("answered", states)

    def test_the_locationd_guard_does_not_end_a_locationd_launchd_started_for_a_predecessors_silence(self):
        """A locationd can stop answering and crash on its own; its launchd-started
        successor must not be ended for a question the dead one left open. A question counts only
        against the processes it was asked of; a successor gets the boot's
        allowance (120 s) before it is asked, and is ended only for its
        own silence."""
        compiler = shutil.which("clang") or shutil.which("cc")
        if not compiler:
            self.skipTest("A C compiler is required")
        source = Path(__file__).resolve().parents[2] / "integrations/ios-emulator/guest-quiet.c"
        with tempfile.TemporaryDirectory(prefix="tomacco-quiet-successor-") as directory:
            root = Path(directory)
            harness = root / "successor.c"
            harness.write_text('#include <pthread.h>\n#include <stdarg.h>\n#include <stdio.h>\n#include <stdlib.h>\n'
                               '#include <string.h>\n#include <unistd.h>\n'
                               '#define main guest_entry\n#define dlopen test_dlopen\n#define dlsym test_dlsym\n'
                               '#define sleep test_sleep\n#define alarm test_alarm\n#define getuid test_getuid\n'
                               '#define kill test_kill\n#define dprintf test_dprintf\n#include ' +
                               json.dumps(str(source)) + '\n#undef main\n#undef dlopen\n#undef dlsym\n#undef sleep\n'
                               '#undef alarm\n#undef getuid\n#undef kill\n#undef dprintf\n' + r'''
static pthread_t main_thread;
static unsigned long clock_now;   /* guest seconds, advanced by the guard's sleeps */
static unsigned kills, killed_pids[8];
static unsigned long kill_times[8];
static pthread_mutex_t output = PTHREAD_MUTEX_INITIALIZER;
/* locationd 81 answers until second 200, then hangs; at 250 it is gone and
 * 82 runs in its place (launchd), answering until 600, then hanging. */
static unsigned locationd_pid(unsigned long now) { return now < 250 ? 81 : 82; }
int test_dprintf(int fd, const char *format, ...) {
    char text[512];
    va_list list;
    va_start(list, format);
    vsnprintf(text, sizeof(text), format, list);
    va_end(list);
    text[strcspn(text, "\n")] = 0;
    pthread_mutex_lock(&output);
    printf("{\"clock\":%lu,\"line\":%s}\n", __atomic_load_n(&clock_now, __ATOMIC_SEQ_CST), text);
    fflush(stdout);
    pthread_mutex_unlock(&output);
    (void)fd;
    return 0;
}
unsigned test_getuid(void) { return 0; }
unsigned test_alarm(unsigned seconds) { (void)seconds; return 0; }
unsigned test_sleep(unsigned seconds) {
    if (pthread_equal(pthread_self(), main_thread)) { usleep(200); return 0; }
    usleep(500);
    unsigned long now = __atomic_add_fetch(&clock_now, seconds, __ATOMIC_SEQ_CST);
    if (now >= 1100) {
        pthread_mutex_lock(&output);
        printf("{\"kills\":[");
        for (unsigned i = 0; i < kills && i < 8; ++i)
            printf("%s[%u,%lu]", i ? "," : "", killed_pids[i], kill_times[i]);
        printf("]}\n");
        fflush(stdout);
        _exit(0);
    }
    return 0;
}
int test_kill(int pid, int signal) {
    unsigned index = __atomic_load_n(&kills, __ATOMIC_SEQ_CST);
    if (index < 8) {
        killed_pids[index] = (unsigned)pid;
        kill_times[index] = __atomic_load_n(&clock_now, __ATOMIC_SEQ_CST);
    }
    __atomic_add_fetch(&kills, 1, __ATOMIC_SEQ_CST);
    return signal == 9 ? 0 : -1;
}
static int system_token, runtime_token, location_token, xpc_token;
static int fake_class = 1, dictionary_value = 1, dictionary_type_value = 1;
void *test_dlopen(const char *path, int mode) {
    (void)mode;
    if (strstr(path, "libSystem")) return &system_token;
    if (strstr(path, "libobjc")) return &runtime_token;
    if (strstr(path, "CoreLocation")) return &location_token;
    return &xpc_token;
}
static object get_class_stub(const char *name) { return strcmp(name, "CLLocationManager") ? 0 : &fake_class; }
static void *selector_stub(const char *name) { return (void *)name; }
static unsigned char ask_stub(object receiver, void *name) {
    if (receiver != &fake_class || strcmp(name, "locationServicesEnabled")) abort();
    unsigned long asked_at = __atomic_load_n(&clock_now, __ATOMIC_SEQ_CST);
    /* Asked of the hung 81: never answered, not even once 81 is gone. */
    if (asked_at >= 200 && asked_at < 250) for (;;) usleep(1000);
    /* Asked of the hung 82: answered (as a failure) once it is ended. */
    if (asked_at >= 600) {
        unsigned seen = __atomic_load_n(&kills, __ATOMIC_SEQ_CST);
        while (__atomic_load_n(&kills, __ATOMIC_SEQ_CST) == seen) usleep(50);
    }
    return 1;
}
static void *pool_push_stub(void) { return &fake_class; }
static void pool_pop_stub(void *pool) { (void)pool; }
static int list_stub(int *pids, int size) {
    if (size < (int)(2 * sizeof(int))) return -1;
    pids[0] = 1; pids[1] = (int)locationd_pid(__atomic_load_n(&clock_now, __ATOMIC_SEQ_CST));
    return 2;
}
static int name_stub(int pid, char *name, unsigned size) {
    const char *value = pid == 1 ? "launchd" : (pid == 81 || pid == 82) ? "locationd" : "";
    snprintf(name, size, "%s", value);
    return (int)strlen(value);
}
static int create_stub(thread_handle *thread, const void *attributes, void *(*entry)(void *), void *argument) {
    (void)attributes;
    return pthread_create((pthread_t *)thread, 0, entry, argument);
}
static int detach_stub(thread_handle thread) { return pthread_detach((pthread_t)thread); }
static object dictionary_stub(const char *const *keys, const object *values, unsigned long count) {
    (void)keys; (void)values; (void)count;
    return &dictionary_value;
}
static void u64_stub(object o, const char *k, unsigned long long v) { (void)o; (void)k; (void)v; }
static void string_stub(object o, const char *k, const char *v) { (void)o; (void)k; (void)v; }
static int routine_stub(unsigned long long r, object request, object *reply) {
    (void)r; (void)request; (void)reply;
    return 113;
}
static long long integer_stub(object o, const char *k) { (void)o; (void)k; return 0; }
static object get_stub(object o, const char *k) { (void)o; (void)k; return 0; }
static object type_stub(object o) { (void)o; return &dictionary_type_value; }
static void release_stub(object o) { (void)o; }
void *test_dlsym(void *library, const char *name) {
    struct { const char *name; void *value; } table[] = {
        {"objc_getClass", (void *)get_class_stub}, {"sel_registerName", (void *)selector_stub},
        {"objc_msgSend", (void *)ask_stub}, {"objc_autoreleasePoolPush", (void *)pool_push_stub},
        {"objc_autoreleasePoolPop", (void *)pool_pop_stub}, {"proc_listallpids", (void *)list_stub},
        {"proc_name", (void *)name_stub}, {"pthread_create", (void *)create_stub},
        {"pthread_detach", (void *)detach_stub}, {"xpc_dictionary_create", (void *)dictionary_stub},
        {"xpc_dictionary_set_uint64", (void *)u64_stub}, {"xpc_dictionary_set_string", (void *)string_stub},
        {"_xpc_service_routine", (void *)routine_stub}, {"xpc_dictionary_get_int64", (void *)integer_stub},
        {"xpc_dictionary_get_value", (void *)get_stub}, {"xpc_get_type", (void *)type_stub},
        {"_xpc_type_dictionary", (void *)&dictionary_type_value}, {"xpc_release", (void *)release_stub},
    };
    (void)library;
    for (unsigned i = 0; i < sizeof(table) / sizeof(table[0]); ++i)
        if (!strcmp(table[i].name, name)) return table[i].value;
    return 0;
}
int main(void) {
    main_thread = pthread_self();
    return guest_entry();
}
''')
            build = subprocess.run([compiler, "-O2", "-Wall", "-Wno-unused-function", str(harness), "-lpthread",
                                    "-o", str(root / "successor")], capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(root / "successor")], capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr)
            rows = [json.loads(line) for line in run.stdout.splitlines()]
            states = [row["line"]["state"] for row in rows
                      if isinstance(row.get("line"), dict) and row["line"].get("event") == "guard"]
            kills = rows[-1]["kills"]
            # 81's silence from second 200 is noticed, but by then 81 is gone:
            # nothing is ended, the successor is given its allowance.
            self.assertIn("replaced", states)
            self.assertTrue(kills, states)
            self.assertTrue(all(pid == 82 for pid, _ in kills), kills)
            # 82 is ended for its own silence (from second 600), after the
            # deadline, and never within the 120 s allowance after 250.
            self.assertGreaterEqual(kills[0][1], 600 + 90)
            self.assertLessEqual(kills[0][1], 600 + 30 + 90 + 2)

    def test_the_jobs_carry_the_project_identifier(self):
        self.assertEqual(GUEST_IDENTIFIER, "io.github.marselnenaj.tomacco")
        for job in launch_jobs(True, True, True, True, virtual_battery=True):
            self.assertTrue(job["Label"].startswith(GUEST_IDENTIFIER + "."), job["Label"])
        # The owned diagnostic's job is labelled with its bundle identifier.
        self.assertEqual(launch_jobs(True, False)[-1]["Label"], application_info("27.0")["CFBundleIdentifier"])
        original, record = self.fixture(quiet=True)
        actual, files, _combined = payload(self.root, original)
        self.assertEqual(actual, record)
        self.assertIn(f"System/Library/LaunchDaemons/{GUEST_IDENTIFIER}.quiet.plist", files)
        # A build's jobs share one prefix: one renamed job alone is refused.
        mixed = json.loads(json.dumps(record))
        mixed["jobs"][0]["Label"] = "io.example.platform-inventory"
        self.save(mixed)
        with self.assertRaisesRegex(ValueError, "owned bounded inventory"):
            payload(self.root, original)

    def test_quieting_helper_is_accepted_only_in_a_build_that_declares_it(self):
        original, record = self.fixture(quiet=True)
        actual, files, _combined = payload(self.root, original)
        self.assertEqual(actual, record)
        self.assertEqual(sorted(name for name in files if name.startswith("usr/")),
                         ["usr/libexec/tomacco-app-control", "usr/libexec/tomacco-platform", "usr/libexec/tomacco-quiet"])
        self.assertEqual(len(files), 6)
        undeclared = {key: value for key, value in record.items() if key != "quiet_services"}
        without_program = {**record, "programs": record["programs"][:2]}
        without_job = {**record, "jobs": launch_jobs()}
        for value, message in ((undeclared, "Unexpected native guest programs"),
                               ({**record, "quiet_services": False}, "Unexpected native guest programs"),
                               ({**record, "quiet_services": 1}, "service-quieting mode"),
                               (without_program, "program inventory"),
                               (without_job, "")):
            with self.subTest(value=sorted(value)), self.assertRaisesRegex(ValueError, message):
                self.save(value)
                payload(self.root, original)

    def test_the_virtual_battery_is_one_resident_root_job_without_privileges(self):
        # Opt-in where jobs are derived: every earlier build shape is unchanged.
        for shape in ((), (True, True), (True, True, True, True)):
            self.assertEqual(launch_jobs(*shape, virtual_battery=False), launch_jobs(*shape))
        jobs = launch_jobs(True, True, True, True, virtual_battery=True)
        self.assertEqual(jobs[:-1], launch_jobs(True, True, True, True))
        battery = jobs[-1]
        self.assertEqual(battery["Label"], "io.github.marselnenaj.tomacco.battery")
        self.assertEqual(battery["ProgramArguments"], [GUEST_ROOT + "/usr/libexec/tomacco-battery"])
        self.assertEqual((battery["UserName"], battery["ProcessType"]), ("root", "Interactive"))
        # powerd drops a published power source when its publisher exits.
        self.assertIs(battery["RunAtLoad"], True)
        self.assertIs(battery["KeepAlive"], True)
        self.assertNotIn("LaunchOnlyOnce", battery)
        # powerd checks no entitlement for a power source, so none is granted.
        self.assertEqual(program_entitlements("usr/libexec/tomacco-battery", True, True,
                                              installer_connection_query=True, application_log_access=True,
                                              graphics_query_access=True, application_termination=True,
                                              time_zone_control=True), {})
        source = (Path(__file__).resolve().parents[2] / "integrations/ios-emulator/guest-battery.c").read_text()
        code = source.split("*/", 1)[1]
        # It talks to powerd and notifyd only: no file, process or kernel access.
        for forbidden in ("fopen", "open", "openat", "creat", "unlink", "rename", "system", "posix_spawn", "execve",
                          "execv", "fork", "kill"):
            self.assertIsNone(re.search(r"\b%s\s*\(" % forbidden, code), forbidden)
        for forbidden in ("IOService", "IORegistry", "notify_set_state", "_xpc_"):
            self.assertNotIn(forbidden, code)
        self.assertEqual(sorted(re.findall(r'dlsym\(\w+, "([^"]+)"\)', code)), sorted([
            "CFDictionaryCreateMutable", "CFDictionarySetValue", "CFNumberCreate", "CFRelease",
            "CFStringCreateWithCString", "IOPSCreatePowerSource", "IOPSReleasePowerSource",
            "IOPSSetPowerSourceDetails", "kCFBooleanFalse", "kCFBooleanTrue", "kCFTypeDictionaryKeyCallBacks",
            "kCFTypeDictionaryValueCallBacks", "notify_post",
            # Read only, to log what powerd lists after a publish.
            "CFArrayGetCount", "CFArrayGetValueAtIndex", "CFDictionaryGetValue", "CFNumberGetValue",
            "CFStringGetCString", "IOPSCopyPowerSourcesInfo", "IOPSCopyPowerSourcesList",
            "IOPSGetPowerSourceDescription"]))
        listed = code[code.index("announcements[] = {"):code.index("};", code.index("announcements[] = {"))]
        self.assertEqual(re.findall(r'"([^"]+)"', listed), ["com.apple.system.powersources.timeremaining",
                                                            "com.apple.system.powersources.percent"])

    def test_new_builds_include_the_virtual_battery_unless_declined(self):
        import build_ios_guest_tools as builder
        from unittest import mock
        seen = []
        base = ["build_ios_guest_tools.py", "--output", "o", "--trustcache", "t", "--linker", "l", "--ldid", "d"]
        with mock.patch.object(builder, "build", lambda args: seen.append(args.virtual_battery) or {}), \
                mock.patch("builtins.print"):
            for extra in ([], ["--virtual-battery"], ["--no-virtual-battery"]):
                with mock.patch("sys.argv", base + extra):
                    builder.main()
        self.assertEqual(seen, [True, True, False])

    def test_the_virtual_battery_helper_is_accepted_only_in_a_build_that_declares_it(self):
        original, record = self.fixture(quiet=True, battery=True)
        actual, files, _combined = payload(self.root, original)
        self.assertEqual(actual, record)
        self.assertEqual(sorted(name for name in files if name.startswith("usr/")),
                         ["usr/libexec/tomacco-app-control", "usr/libexec/tomacco-battery", "usr/libexec/tomacco-platform",
                          "usr/libexec/tomacco-quiet"])
        self.assertEqual(len(files), 8)
        undeclared = {key: value for key, value in record.items() if key != "virtual_battery"}
        without_program = {**record, "programs": record["programs"][:3]}
        without_job = {**record, "jobs": launch_jobs(quiet_services=True)}
        for value, message in ((undeclared, "Unexpected native guest programs"),
                               ({**record, "virtual_battery": False}, "Unexpected native guest programs"),
                               ({**record, "virtual_battery": 1}, "virtual battery mode"),
                               (without_program, "program inventory"),
                               (without_job, "")):
            with self.subTest(value=sorted(value)), self.assertRaisesRegex(ValueError, message):
                self.save(value)
                payload(self.root, original)
        # A build that declares the battery cannot hide it from the inventory.
        plain_original, plain = self.fixture(quiet=True)
        with self.assertRaisesRegex(ValueError, "program inventory"):
            self.save({**plain, "virtual_battery": True})
            payload(self.root, plain_original)

    def test_the_virtual_battery_publishes_a_charged_internal_battery_and_keeps_it(self):
        """The helper retries until powerd answers, files a new source as a
        UPS and then gives it one charged internal battery on AC power,
        announces it, sets it again on its schedule and publishes anew when
        powerd has lost it."""
        compiler = shutil.which("clang") or shutil.which("cc")
        if not compiler:
            self.skipTest("A C compiler is required")
        source = Path(__file__).resolve().parents[2] / "integrations/ios-emulator/guest-battery.c"
        with tempfile.TemporaryDirectory(prefix="tomacco-battery-") as directory:
            root = Path(directory)
            harness = root / "battery.c"
            harness.write_text('#include <stdarg.h>\n#include <stdio.h>\n#include <stdlib.h>\n#include <string.h>\n'
                               '#include <unistd.h>\n'
                               '#define main guest_entry\n#define dlopen test_dlopen\n#define dlsym test_dlsym\n'
                               '#define sleep test_sleep\n#define getuid test_getuid\n#define dprintf test_dprintf\n'
                               '#include ' + json.dumps(str(source)) + '\n#undef main\n#undef dlopen\n#undef dlsym\n'
                               '#undef sleep\n#undef getuid\n#undef dprintf\n' + r'''
static unsigned long clock_now;   /* guest seconds, advanced by the helper's sleeps */
static unsigned creates, sources, released, lose_at = 1000, lost;
int test_dprintf(int fd, const char *format, ...) {
    char text[512];
    va_list list;
    va_start(list, format);
    vsnprintf(text, sizeof(text), format, list);
    va_end(list);
    text[strcspn(text, "\n")] = 0;
    printf("{\"clock\":%lu,\"line\":%s}\n", clock_now, text);
    (void)fd;
    return 0;
}
unsigned test_getuid(void) { return 0; }
unsigned test_sleep(unsigned seconds) {
    clock_now += seconds;
    if (clock_now >= 4000) {
        printf("{\"creates\":%u,\"released\":%u}\n", creates, released);
        exit(0);
    }
    return 0;
}
struct value { int kind; char text[64]; int number; };   /* 1 string, 2 number, 3 boolean */
struct table { int kind; unsigned count; struct value *keys[32], *values[32]; };
static struct value yes = {3, "", 1}, no = {3, "", 0};
static object yes_ref = &yes, no_ref = &no;
static int key_callbacks, value_callbacks, foundation_token, iokit_token, system_token;
void *test_dlopen(const char *path, int mode) {
    (void)mode;
    if (!strcmp(path, "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")) return &foundation_token;
    if (!strcmp(path, "/System/Library/Frameworks/IOKit.framework/Versions/A/IOKit")) return &iokit_token;
    if (!strcmp(path, "/usr/lib/libSystem.B.dylib")) return &system_token;
    return 0;
}
static object create_table(object allocator, long capacity, const void *keys, const void *values) {
    if (allocator || capacity || keys != &key_callbacks || values != &value_callbacks) abort();
    struct table *table = calloc(1, sizeof(*table));
    table->kind = 4;
    return table;
}
static void set_value(object dictionary, const void *key, const void *value) {
    struct table *table = dictionary;
    if (table->kind != 4 || ((const struct value *)key)->kind != 1) abort();
    for (unsigned i = 0; i < table->count; ++i)   /* CFDictionarySetValue replaces an equal key */
        if (!strcmp(table->keys[i]->text, ((const struct value *)key)->text)) {
            table->values[i] = (struct value *)value;
            return;
        }
    if (table->count == 32) abort();
    table->keys[table->count] = (struct value *)key;
    table->values[table->count++] = (struct value *)value;
}
static object create_string(object allocator, const char *text, unsigned encoding) {
    if (allocator || encoding != 0x08000100) abort();
    struct value *value = calloc(1, sizeof(*value));
    value->kind = 1;
    snprintf(value->text, sizeof(value->text), "%s", text);
    return value;
}
static object create_number(object allocator, long type, const void *number) {
    if (allocator || type != 3) abort();
    struct value *value = calloc(1, sizeof(*value));
    value->kind = 2;
    value->number = *(const int *)number;
    return value;
}
static void release_value(object value) {
    if (value == &yes || value == &no) abort();
}
static const struct table *published;
static int create_power_source(object *source) {
    ++creates;
    if (creates <= 2) return (int)0xe00002d8;   /* powerd not answering yet */
    *source = (object)(unsigned long)(0x1000 + ++sources);
    return 0;
}
static void print_value(const struct value *value) {
    if (value->kind == 1) printf("\"%s\"", value->text);
    else if (value->kind == 2) printf("%d", value->number);
    else printf(value->number ? "true" : "false");
}
static int set_power_source_details(object source, object dictionary) {
    const struct table *table = dictionary;
    if (!lost && clock_now >= lose_at) {
        lost = 1;
        return (int)0xe00002c2;
    }
    printf("{\"clock\":%lu,\"source\":%lu,\"details\":{", clock_now, (unsigned long)source - 0x1000);
    for (unsigned i = 0; i < table->count; ++i) {
        printf("%s\"%s\":", i ? "," : "", table->keys[i]->text);
        print_value(table->values[i]);
    }
    printf("}}\n");
    published = table;
    return 0;
}
static int release_power_source(object source) {
    (void)source;
    ++released;
    return 0;
}
/* powerd's listing: the last description it was given (published). */
static int stub_info_token, stub_list_token;
static object stub_copy_info(void) { return published ? &stub_info_token : 0; }
static object stub_copy_list(object info) { return info == &stub_info_token ? &stub_list_token : 0; }
static long stub_array_count(object list) { return list == &stub_list_token ? 1 : -1; }
static object stub_array_at(object list, long index) { return list == &stub_list_token && !index ? (object)1 : 0; }
static object stub_description_of(object info, object source) {
    return info == &stub_info_token && source == (object)1 ? (object)published : 0;
}
static object stub_get_value(object dictionary, const void *key) {
    const struct table *table = dictionary;
    for (unsigned i = 0; i < table->count; ++i)
        if (!strcmp(table->keys[i]->text, ((const struct value *)key)->text)) return table->values[i];
    return 0;
}
static unsigned char stub_get_string(object value, char *text, long size, unsigned encoding) {
    if (((struct value *)value)->kind != 1 || encoding != 0x08000100) abort();
    snprintf(text, (unsigned long)size, "%s", ((struct value *)value)->text);
    return 1;
}
static unsigned char stub_get_number(object value, long type, void *number) {
    if (((struct value *)value)->kind != 2 || type != 3) abort();
    *(int *)number = ((struct value *)value)->number;
    return 1;
}
static unsigned post_name(const char *name) {
    printf("{\"clock\":%lu,\"posted\":\"%s\"}\n", clock_now, name);
    return 0;
}
void *test_dlsym(void *library, const char *name) {
    struct { void *library; const char *name; void *value; } table[] = {
        {&foundation_token, "kCFTypeDictionaryKeyCallBacks", &key_callbacks},
        {&foundation_token, "kCFTypeDictionaryValueCallBacks", &value_callbacks},
        {&foundation_token, "kCFBooleanTrue", &yes_ref}, {&foundation_token, "kCFBooleanFalse", &no_ref},
        {&foundation_token, "CFDictionaryCreateMutable", (void *)create_table},
        {&foundation_token, "CFDictionarySetValue", (void *)set_value},
        {&foundation_token, "CFStringCreateWithCString", (void *)create_string},
        {&foundation_token, "CFNumberCreate", (void *)create_number},
        {&foundation_token, "CFRelease", (void *)release_value},
        {&iokit_token, "IOPSCreatePowerSource", (void *)create_power_source},
        {&iokit_token, "IOPSSetPowerSourceDetails", (void *)set_power_source_details},
        {&iokit_token, "IOPSReleasePowerSource", (void *)release_power_source},
        {&system_token, "notify_post", (void *)post_name},
        {&foundation_token, "CFArrayGetCount", (void *)stub_array_count},
        {&foundation_token, "CFArrayGetValueAtIndex", (void *)stub_array_at},
        {&foundation_token, "CFDictionaryGetValue", (void *)stub_get_value},
        {&foundation_token, "CFStringGetCString", (void *)stub_get_string},
        {&foundation_token, "CFNumberGetValue", (void *)stub_get_number},
        {&iokit_token, "IOPSCopyPowerSourcesInfo", (void *)stub_copy_info},
        {&iokit_token, "IOPSCopyPowerSourcesList", (void *)stub_copy_list},
        {&iokit_token, "IOPSGetPowerSourceDescription", (void *)stub_description_of},
    };
    for (unsigned i = 0; i < sizeof(table) / sizeof(table[0]); ++i)
        if (table[i].library == library && !strcmp(table[i].name, name)) return table[i].value;
    return 0;
}
int main(void) { return guest_entry(); }
''')
            build = subprocess.run([compiler, "-O2", "-Wall", "-Wno-unused-function", str(harness),
                                    "-o", str(root / "battery")], capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(root / "battery")], capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr)
            rows = [json.loads(line) for line in run.stdout.splitlines()]
            self.assertEqual(rows[-1], {"creates": 4, "released": 1})
            details = [row for row in rows if "details" in row]
            battery = {"Type": "InternalBattery", "Transport Type": "Internal", "Name": "InternalBattery-0",
                       "Power Source State": "AC Power", "Current Capacity": 100, "Max Capacity": 100,
                       "Is Present": True, "Is Charging": False, "Is Charged": True, "Show Charging UI": True,
                       "Raw External Connected": True, "Battery Provides Time Remaining": True}
            # A new source's first description is the same one as a UPS: powerd
            # files the source by its first "Type" and never again, and only a
            # kind-1 ("InternalBattery") source sends its precise copy through
            # the kernel battery's missing data. The internal battery follows
            # at once, before any announcement.
            first = {row["source"]: row for row in reversed(details)}
            self.assertEqual(sorted(first), [1, 2])
            for row in first.values():
                self.assertEqual(row["details"], {**battery, "Type": "UPS"})
                self.assertEqual(list(row["details"]), list(battery))
            filed = [row for row in details if row not in first.values()]
            self.assertTrue(filed and all(row["details"] == battery for row in filed), filed)
            self.assertEqual([(row["clock"], row["source"]) for row in details[:2]], [(20, 1), (20, 1)])
            self.assertEqual(details.index(first[2]) + 1, details.index(next(row for row in filed if row["source"] == 2)))
            # Two refused attempts ten seconds apart, then the source; after
            # 30 s (after the 2 s pause before the listing) it is set again,
            # the interval doubling to ten minutes.
            self.assertEqual([row["clock"] for row in filed[:6]], [20, 52, 112, 232, 472, 952])
            log = [row["line"] for row in rows if "line" in row]
            self.assertEqual([entry["state"] for entry in log],
                             ["retrying", "retrying", "published", "listed", "listed", "lost", "published"])
            # What powerd lists is logged after the publish and after the first refresh.
            self.assertEqual(log[3], {"event": "battery", "state": "listed", "sources": 1,
                                      "source_0": {"type": "InternalBattery", "state": "AC Power", "capacity": 100}})
            self.assertEqual(log[2], {"event": "battery", "state": "published", "type": "InternalBattery",
                                      "filed_as": "UPS", "percent": 100, "power": "AC Power", "charged": True,
                                      "attempts": 3, "announced": 2})
            # powerd lost it at second 1552 (952 + 600): released, created anew.
            lost = next(row for row in rows if row.get("line", {}).get("state") == "lost")
            self.assertEqual(lost["clock"], 1552)
            self.assertEqual({row["source"] for row in details if row["clock"] < 1552}, {1})
            self.assertEqual({row["source"] for row in details if row["clock"] >= 1552}, {2})
            self.assertEqual(log[-1]["attempts"], 1)
            # Each internal battery set is announced, with both names, and
            # nothing else is posted: never the source while it is a UPS.
            posted = [row for row in rows if "posted" in row]
            self.assertEqual(len(posted), 2 * len(filed))
            self.assertEqual({row["posted"] for row in posted}, {"com.apple.system.powersources.timeremaining",
                                                                "com.apple.system.powersources.percent"})
            self.assertEqual(sorted(row["clock"] for row in posted), sorted(2 * [row["clock"] for row in filed]))
            order = [("set", row["details"]["Type"]) if "details" in row else ("post",)
                     for row in rows if "details" in row or "posted" in row]
            self.assertEqual(order[:4], [("set", "UPS"), ("set", "InternalBattery"), ("post",), ("post",)])

    def test_signed_filename_cannot_substitute_for_bundle_identity(self):
        identifier = "io.github.marselnenaj.tomacco.research-app"
        args = dict(cpu_subtype=0, file_type=2, expected_identifier=identifier)
        inspect_signed_code(signed_fixture(identifier), **args)
        # Both have intact CodeDirectories and page hashes. Installation still
        # requires the signing identifier to match CFBundleIdentifier.
        with self.assertRaisesRegex(ValueError, "signing identifier differs"):
            inspect_signed_code(signed_fixture("TomaccoProbe"), **args)
        with self.assertRaisesRegex(ValueError, "signing identifier differs"):
            inspect_signed_code(signed_fixture(identifier + ".other"), **args)

    def test_an_experiment_probe_library_must_be_declared_by_a_signing_build(self):
        original, record = self.fixture()
        self.save(record)
        payload(self.root, original)
        # The flag is a strict boolean, and an extra adapter is never accepted by
        # a build that does not declare it (or one without development signing).
        for value in ("yes", 1, True):
            record["experiment_probe_library"] = value
            self.save(record)
            with self.assertRaisesRegex(ValueError, "experiment probe library|program inventory"):
                payload(self.root, original)

    def test_adapter_abi_is_verified_from_signed_bytes(self):
        for subtype, expected in ((0, 0), (0x80000002, 2)):
            data = signed_fixture("adapter", cpu_subtype=subtype, file_type=6)
            inspect_signed_code(data, cpu_subtype=expected, file_type=6)
            with self.assertRaisesRegex(ValueError, "unexpected CPU subtype"):
                inspect_signed_code(data, cpu_subtype=2 if expected == 0 else 0, file_type=6)
        original, record = self.fixture()
        record["programs"][0]["architecture"] = "arm64"
        self.save(record)
        payload(self.root, original)
        for architecture in ("arm64e", "x86_64", None):
            record["programs"][0]["architecture"] = architecture
            self.save(record)
            with self.assertRaisesRegex(ValueError, "program architecture"):
                payload(self.root, original)

    def test_development_policy_requires_unambiguous_signed_identities(self):
        app = {"path": "usr/libexec/TomaccoProbe.app/TomaccoProbe", "cdhash": "ab" * 20}
        library = {"path": "usr/libexec/libtomacco-development-signing.dylib", "sha256": "cd" * 32}
        self.assertEqual(development_policy([app, library])["cdhash"], app["cdhash"])
        for records in (None, [None], [library], [app], [app, app, library],
                        [{"path": app["path"]}, library], [app, {"path": library["path"]}],
                        [{**app, "cdhash": 10}, library], [{**app, "cdhash": "ab" * 19}, library],
                        [app, {**library, "sha256": "cg" * 32}]):
            with self.subTest(records=records), self.assertRaises(ValueError):
                development_policy(records)

    def test_complete_payload_and_original_trust_policy_are_preserved(self):
        original, record = self.fixture()
        actual, files, combined = payload(self.root, original)
        self.assertEqual(actual, record)
        self.assertEqual(len(files), 4)
        self.assertIn(original[24:], combined[24:])
        self.assertEqual(LOCAL_PLATFORM_POLICY, bytes.fromhex("02400700"))
        with self.assertRaisesRegex(ValueError, "already present"):
            extend_trustcache(combined, [bytes.fromhex(record["programs"][0]["cdhash"])])

    def test_a_build_is_rebased_onto_another_source_trust_cache_without_new_programs(self):
        original, record = self.fixture()
        source, replacement = self.root.parent / "source.tc", self.root.parent / "replacement.tc"
        self.addCleanup(source.unlink)
        self.addCleanup(replacement.unlink)
        source.write_bytes(original)
        replacement.write_bytes(cache([b"B" * 20 + bytes.fromhex("02400200")]))
        output = Path(self.temporary.name + "-rebased")
        self.addCleanup(shutil.rmtree, output, True)
        report = rebase(self.root, source, replacement, output)
        rebased, files, combined = payload(output, replacement.read_bytes())
        self.assertEqual(rebased["programs"], record["programs"])
        self.assertEqual(files, payload(self.root, original)[1])
        self.assertEqual(report["source_sha256"], digest(replacement.read_bytes()))
        self.assertIn(b"B" * 20, combined)
        with self.assertRaisesRegex(ValueError, "different trust cache"):
            payload(output, original)
        # Only a build that verifies against the source it names is carried over.
        with self.assertRaisesRegex(ValueError, "different trust cache"):
            rebase(self.root, replacement, source, Path(self.temporary.name + "-other"))
        with self.assertRaisesRegex(ValueError, "already exists"):
            rebase(self.root, source, replacement, output)

    def test_removed_graphics_bundle_is_restored_and_a_changed_one_is_refused(self):
        source = self.root / "graphics"
        (source / "_CodeSignature").mkdir(parents=True)
        for name, mode in GRAPHICS_FILES:
            (source / name).write_bytes(name.encode())
            (source / name).chmod(mode)
        files, inventory = graphics_payload(source)
        self.assertEqual([row["path"] for row in inventory], [name for name, _mode in GRAPHICS_FILES])
        self.assertEqual(files["p"], b"p")
        # An analysis cache beside the driver is named in the refusal.
        (source / "AppleParavirtGPUMetalIOGPUFamily.a2s").write_text("unexpected")
        with self.assertRaisesRegex(ValueError, "unexpected: AppleParavirtGPUMetalIOGPUFamily.a2s"):
            graphics_payload(source)
        (source / "AppleParavirtGPUMetalIOGPUFamily.a2s").unlink()
        (source / "p").unlink()
        with self.assertRaisesRegex(ValueError, "missing: p$"):
            graphics_payload(source)
        (source / "p").write_bytes(b"p")
        (source / "p").chmod(0o755)
        # Run the actual helper shell against a private stand-in for Preboot.
        preboot = self.root / "preboot"
        preboot.mkdir()
        (preboot / Path(GRAPHICS_RELATIVE_PATH).parent).mkdir()
        (self.root / "graphics.sha256").write_text("".join(
            row["sha256"] + "  " + row["path"] + "\n" for row in inventory))
        guest_directory_modes(self.root / "graphics")
        script = "set -eu\n" + graphics_script(inventory)
        for old, replacement in (("/mnt/preboot", str(preboot)), ("/graphics", str(self.root / "graphics"))):
            script = script.replace(old, replacement)

        def run():
            return subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, timeout=5)

        restored = run()
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertIn("TOMACCO_GRAPHICS_RESTORED", restored.stdout)
        bundle = preboot / GRAPHICS_RELATIVE_PATH
        self.assertEqual((bundle / "p").read_bytes(), b"p")
        self.assertIn("TOMACCO_GRAPHICS_VERIFIED", run().stdout)
        (bundle / "p").write_bytes(b"changed")
        self.assertNotEqual(run().returncode, 0)
        (bundle / "p").write_bytes(b"p")
        (bundle / "unexpected").write_text("kept")
        self.assertNotEqual(run().returncode, 0)
        self.assertEqual((bundle / "unexpected").read_text(), "kept")
        self.assertEqual(graphics_script(None), "")

    def test_a_helper_kernel_the_support_module_was_not_built_for_is_refused(self):
        from prepare_ios_guest_tools import check_helper_kernel
        support = self.root / "support"
        (support / "apfs").mkdir(parents=True)
        (support / "apfs/apfs.ko").write_bytes(helper_module("6.1.0-old"))
        (self.root / "kernel").write_bytes(helper_kernel_image("6.1.0-old"))
        check_helper_kernel(self.root / "kernel", support)
        (self.root / "kernel").write_bytes(helper_kernel_image("6.2.0-new"))
        with self.assertRaisesRegex(ValueError, "6.2.0-new cannot load the support APFS module built for 6.1.0-old"):
            check_helper_kernel(self.root / "kernel", support)
        (self.root / "kernel").write_bytes(b"not a kernel")
        with self.assertRaisesRegex(ValueError, "not a Linux bzImage"):
            check_helper_kernel(self.root / "kernel", support)

    def test_graphics_payload_reaches_the_helper_and_its_answer_is_the_recorded_state(self):
        import os
        from types import SimpleNamespace
        import time
        from prepare_ios_guest_tools import helper
        source = self.root / "graphics-source"
        (source / "_CodeSignature").mkdir(parents=True)
        for name, mode in GRAPHICS_FILES:
            (source / name).write_bytes(b"driver:" + name.encode())
            (source / name).chmod(mode)
        graphics = graphics_payload(source)
        support = self.root / "support"
        for relative in ("busybox/busybox", "apfs/apfs.ko"):
            (support / relative).parent.mkdir(parents=True)
            (support / relative).write_bytes(b"support")
        (support / "apfs/apfs.ko").write_bytes(helper_module("6.1.0-test"))
        (self.root / "kernel").write_bytes(helper_kernel_image("6.1.0-test"))
        # A stand-in for the support VM: it answers like the installer would.
        engine = self.root / "helper-engine"
        engine.write_text("#!/bin/sh\nprintf '\\nTOMACCO_GUEST_TOOLS_INSTALLED\\n'\n"
                          "[ -z \"$TOMACCO_TEST_ANSWER\" ] || printf '%s\\n' \"$TOMACCO_TEST_ANSWER\"\n")
        engine.chmod(0o755)
        arguments = SimpleNamespace(support=support, helper_engine=engine, bios="/bios", accel="tcg",
                                    helper_kernel=self.root / "kernel")
        tool = b"tool"
        inventory = [{"path": "bin/tool", "mode": 0o755, "sha256": hashlib.sha256(tool).hexdigest()}]
        layout, volume = {"partition": 2}, {"index": 3}

        def run(name, answer, payload):
            output = self.root / name
            output.mkdir()
            previous = os.environ.get("TOMACCO_TEST_ANSWER")
            os.environ["TOMACCO_TEST_ANSWER"] = answer
            previous_umask = os.umask(0o077)
            try:
                state = helper(arguments, output, {"bin/tool": tool}, inventory, layout, volume,
                               time.monotonic() + 30, None, payload)
                self.assertEqual(os.umask(0o077), 0o077)
            finally:
                os.umask(previous_umask)
                if previous is None:
                    del os.environ["TOMACCO_TEST_ANSWER"]
                else:
                    os.environ["TOMACCO_TEST_ANSWER"] = previous
            return state, output / "helper/root"

        state, root = run("restored", "TOMACCO_GRAPHICS_RESTORED", graphics)
        self.assertEqual(state, "restored")
        for directory in (root / "Supplemental", root / "graphics"):
            for path in (directory, *(p for p in directory.rglob("*") if p.is_dir())):
                self.assertEqual(path.stat().st_mode & 0o777, 0o755)
        # Check the actual newc archive's directory metadata as well: cp -a
        # inside the helper carries it onto APFS, including the payload root.
        archive = (root.parent / "init.cpio").read_bytes()
        offset, guest_directories = 0, set()
        while True:
            header = archive[offset:offset + 110]
            self.assertEqual(header[:6], b"070701")
            mode, size, length = [int(header[a:b], 16) for a, b in ((14, 22), (54, 62), (94, 102))]
            name = archive[offset + 110:offset + 110 + length - 1].decode()
            offset = (offset + 110 + length + 3) & ~3
            if name == "TRAILER!!!":
                break
            if (name == "Supplemental" or name.startswith("Supplemental/") or
                    name == "graphics" or name.startswith("graphics/")) and stat.S_ISDIR(mode):
                self.assertEqual(mode & 0o777, 0o755, name)
                guest_directories.add(name)
            offset = (offset + size + 3) & ~3
        self.assertTrue({"Supplemental", "Supplemental/bin", "graphics", "graphics/_CodeSignature"}.issubset(guest_directories))
        # The exact driver files travel into the helper with their modes and
        # digests, and the installer is told to check or restore them.
        for name, mode in GRAPHICS_FILES:
            self.assertEqual((root / "graphics" / name).read_bytes(), b"driver:" + name.encode())
            self.assertEqual((root / "graphics" / name).stat().st_mode & 0o777, mode)
        self.assertEqual((root / "graphics.sha256").read_text(), "".join(
            row["sha256"] + "  " + row["path"] + "\n" for row in graphics[1]))
        script = (root / "init").read_text()
        self.assertIn(graphics_script(graphics[1]), script)
        self.assertLess(script.index("TOMACCO_GRAPHICS_RESTORED"), script.rindex("TOMACCO_GUEST_TOOLS_INSTALLED"))
        # A bundle that was still in place is reported as verified, not restored.
        self.assertEqual(run("verified", "TOMACCO_GRAPHICS_VERIFIED", graphics)[0], "verified")
        # Without the option nothing about graphics enters the helper or the record.
        state, root = run("plain", "", None)
        self.assertIsNone(state)
        self.assertFalse((root / "graphics").exists() or (root / "graphics.sha256").exists())
        self.assertNotIn("TOMACCO_GRAPHICS", (root / "init").read_text())
        # A failure the installer reports is never read as an installation.
        with self.assertRaises(RuntimeError):
            run("failed", "TOMACCO_GUEST_TOOLS_FAILED", graphics)

    def test_guest_directory_modes_preserve_private_host_modes_and_umask(self):
        private = self.root / "private"
        old_umask = os.umask(0o077)
        try:
            private.mkdir()
            root = private / "Supplemental"
            (root / "usr/libexec/Probe.app/_CodeSignature").mkdir(parents=True)
            resource = root / "usr/libexec/Probe.app/_CodeSignature/CodeResources"
            resource.write_bytes(b"sealed resource bytes")
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            guest_directory_modes(root)
            self.assertEqual(os.umask(0o077), 0o077)
        finally:
            os.umask(old_umask)
        for path in (root, *(p for p in root.rglob("*") if p.is_dir())):
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)
        self.assertEqual(private.stat().st_mode & 0o777, 0o700)
        self.assertEqual(resource.stat().st_mode & 0o777, 0o600)
        self.assertEqual(resource.read_bytes(), b"sealed resource bytes")

    def test_guest_directory_modes_refuse_links_before_changing_directories(self):
        root, outside = self.root / "payload", self.root / "outside"
        root.mkdir(mode=0o700)
        outside.mkdir(mode=0o700)
        (root / "child").mkdir(mode=0o700)
        (root / "link").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            guest_directory_modes(root)
        for path in (root, root / "child", outside):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        with self.assertRaisesRegex(ValueError, "ordinary directory"):
            guest_directory_modes(root / "link")

    def test_installed_directory_modes_are_checked_including_payload_root(self):
        _original, record = self.fixture()
        root, inventory = self.root / "Supplemental", record["files"]
        for row in inventory:
            (root / row["path"]).chmod(row["mode"])
        guest_directory_modes(root)
        hashes = self.root / "directory-check.sha256"
        hashes.write_text("".join(row["sha256"] + "  " + row["path"] + "\n" for row in inventory))
        script = "set -eu\n" + verify_previous_script(
            inventory, str(hashes), directory_modes=True, scratch_prefix=str(self.root / "directory-check"))

        def check():
            return subprocess.run(["/bin/sh", "-c", script], cwd=root,
                                  capture_output=True, text=True, timeout=5)

        self.assertEqual(check().returncode, 0)
        root.chmod(0o700)
        self.assertNotEqual(check().returncode, 0)
        root.chmod(0o755)
        parent = (root / inventory[0]["path"]).parent
        parent.chmod(0o700)
        self.assertNotEqual(check().returncode, 0)
        # A legacy replacement may still verify its exact files before
        # installing a corrected directory tree; no native readiness follows.
        legacy = "set -eu\n" + verify_previous_script(
            inventory, str(hashes), scratch_prefix=str(self.root / "legacy-check"))
        self.assertEqual(subprocess.run(["/bin/sh", "-c", legacy], cwd=root,
                                       capture_output=True, timeout=5).returncode, 0)
        parent.chmod(0o755)
        self.assertEqual(check().returncode, 0)

    def test_upgrade_refuses_changed_or_extra_files_links_and_modes(self):
        _original, record = self.fixture()
        inventory = record["files"]
        root = self.root / "Supplemental"
        # Exercise the actual helper's shell checks on a private filesystem.
        # Hashes alone would miss links, extra files and executable mode changes.
        for row in inventory:
            (root / row["path"]).chmod(row["mode"])
        (self.root / "previous.sha256").write_text("".join(
            row["sha256"] + "  " + row["path"] + "\n" for row in inventory))
        script = "set -eu\n" + verify_previous_script(inventory).replace("/previous", str(self.root / "previous"))

        def check():
            return subprocess.run(["/bin/sh", "-c", script], cwd=root,
                                  capture_output=True, text=True, timeout=5)

        good = check()
        self.assertEqual(good.returncode, 0, good.stderr)
        executable = root / inventory[0]["path"]
        data = executable.read_bytes()
        executable.write_bytes(data + b"changed")
        self.assertNotEqual(check().returncode, 0)
        executable.write_bytes(data)
        executable.chmod(0o644)
        self.assertNotEqual(check().returncode, 0)
        executable.chmod(inventory[0]["mode"])
        executable.unlink()
        external = self.root / "external"
        external.write_bytes(data)
        external.chmod(inventory[0]["mode"])
        executable.symlink_to(external)
        self.assertNotEqual(check().returncode, 0)
        executable.unlink()
        executable.write_bytes(data)
        executable.chmod(inventory[0]["mode"])
        extra = root / "unexpected"
        extra.write_text("must not be removed")
        self.assertNotEqual(check().returncode, 0)
        extra.unlink()
        extra.mkdir()
        self.assertNotEqual(check().returncode, 0)
        extra.rmdir()
        good = check()
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(external.read_bytes(), data)

    def test_bundle_metadata_and_resource_manifest_must_match_signed_seals(self):
        seals = {1: b"owned bundle Info.plist", 3: b"owned resource manifest"}
        data = signed_fixture("bundle", sealed_files=seals)
        result = inspect_signed_code(data, cpu_subtype=0, file_type=2, sealed_files=seals)
        self.assertEqual(result["bundle_seals_verified"], [1, 3])
        for slot in seals:
            with self.subTest(slot=slot), self.assertRaisesRegex(ValueError, "bundle seal"):
                inspect_signed_code(data, cpu_subtype=0, file_type=2,
                                    sealed_files={**seals, slot: b"modified"})
        with self.assertRaisesRegex(ValueError, "bundle seal"):
            inspect_signed_code(signed_fixture("old-unsealed-bundle"), cpu_subtype=0,
                                file_type=2, sealed_files=seals)

    def test_changing_signed_code_is_rejected_even_after_manifest_rehash(self):
        original, record = self.fixture()
        row = record["programs"][0]
        path = self.root / "Supplemental" / row["path"]
        data = bytearray(path.read_bytes())
        data[24] ^= 1
        path.write_bytes(data)
        row["sha256"] = digest(data)
        record["files"][0]["sha256"] = digest(data)
        self.save(record)
        with self.assertRaisesRegex(ValueError, "signed code page changed"):
            payload(self.root, original)

    def test_daemon_behavior_cannot_be_extended_by_rehashing_the_manifest(self):
        original, record = self.fixture()
        job = record["jobs"][0]
        job["KeepAlive"] = True
        name = "System/Library/LaunchDaemons/" + job["Label"] + ".plist"
        data = plistlib.dumps(job)
        (self.root / "Supplemental" / name).write_bytes(data)
        row = next(row for row in record["files"] if row["path"] == name)
        row.update(sha256=digest(data), bytes=len(data))
        self.save(record)
        with self.assertRaisesRegex(ValueError, "bounded inventory"):
            payload(self.root, original)

    def test_symlink_and_different_input_cache_are_refused(self):
        original, record = self.fixture()
        with self.assertRaisesRegex(ValueError, "different trust cache"):
            payload(self.root, cache())
        name = record["programs"][0]["path"]
        path = self.root / "Supplemental" / name
        path.rename(self.root / "external")
        path.symlink_to(self.root / "external")
        with self.assertRaises(OSError):
            payload(self.root, original)

    def test_malformed_top_level_and_program_records_are_refused(self):
        original, record = self.fixture()
        for value in ([], {**record, "programs": [None, {}]}, {**record, "files": [[], {}, {}, {}]}):
            with self.subTest(value=value):
                self.save(value)
                with self.assertRaises(ValueError):
                    payload(self.root, original)

    def test_research_mode_is_virtual_normal_boot_only_and_preserves_other_properties(self):
        tree = Node({"name": Property(b"device-tree\0"), "target-sub-type": Property(b"VPHONE600AP\0")}, [
            Node({"name": Property(b"defaults\0"), "vmm-present": Property(b"\1\0\0\0")}),
            Node({"name": Property(b"chosen\0"), "osenvironment": Property(bytes(32)),
                  "unrelated": Property(b"abc", 0x80000000, b"x")})])
        encoded = tree.encode()
        prepared = decode(virtual_research_mode(encoded))
        self.assertEqual(prepared.child("chosen").get("sec-research-device-erm-enabled"), b"\1\0\0\0")
        del prepared.child("chosen").props["sec-research-device-erm-enabled"]
        self.assertEqual(prepared.encode(), encoded)
        for node, key, value in [("defaults", "vmm-present", bytes(4)),
                                 ("chosen", "osenvironment", b"device-recovery\0"),
                                 ("chosen", "sec-research-device-erm-enabled", b"\1")]:
            with self.subTest(key=key):
                changed = copy.deepcopy(tree)
                changed.child(node).set(key, value)
                with self.assertRaises(ValueError):
                    virtual_research_mode(changed.encode())


    def test_development_device_profile_is_virtual_nonproduction_and_preserves_unrelated_bytes(self):
        modes = ("certificate-security-mode", "effective-security-mode-ap", "effective-security-mode-sep")
        production = ("certificate-production-status", "effective-production-status-ap", "effective-production-status-sep")
        tree = Node({"name": Property(b"device-tree\0"), "target-sub-type": Property(b"VPHONE600AP\0")}, [
            Node({"name": Property(b"defaults\0"), "vmm-present": Property(b"\1\0\0\0")}),
            Node({"name": Property(b"chosen\0"), "osenvironment": Property(bytes(32)),
                  **{name: Property(bytes(4)) for name in modes + production},
                  "unrelated": Property(b"abc", 0x80000000, b"x")})])
        encoded = tree.encode()
        prepared = virtual_research_mode(encoded, development_device=True)
        self.assertEqual(virtual_research_mode(prepared, development_device=True), prepared)
        decoded = decode(prepared)
        for name in modes:
            self.assertEqual(decoded.child("chosen").get(name), b"\1\0\0\0")
            decoded.child("chosen").set(name, bytes(4))
        del decoded.child("chosen").props["sec-research-device-erm-enabled"]
        self.assertEqual(decoded.encode(), encoded)
        for name, value in [(name, b"\1\0\0\0") for name in production + modes] + [
                ("certificate-security-mode", b"\1"), ("certificate-production-status", b"")]:
            with self.subTest(name=name, value=value):
                changed = copy.deepcopy(tree)
                changed.child("chosen").set(name, value)
                with self.assertRaises(ValueError):
                    virtual_research_mode(changed.encode(), development_device=True)
        changed = copy.deepcopy(tree)
        changed.set("target-sub-type", b"PHONE600AP\0")
        with self.assertRaises(ValueError):
            virtual_research_mode(changed.encode(), development_device=True)


def signer_manifest(files):
    """CodeResources in the signer's shape for a bundle's non-code files
    (v1: every one; v2: the same without the top-level Info.plist and PkgInfo)."""
    return plistlib.dumps({"files": {path: hashlib.sha1(data).digest() for path, data in files.items()},
                           "files2": {path: {"hash": hashlib.sha1(data).digest(), "hash2": hashlib.sha256(data).digest()}
                                      for path, data in files.items() if path not in ("Info.plist", "PkgInfo")}})


def embedded_fixture(frameworks=(), libraries=(), extensions=()):
    """Signed embedded code of a staged app: frameworks as (name, identifier)
    with their own Info.plist and manifest, libraries as file names, app
    extensions as (name, identifier, extension point, entitlements) with their
    own Info.plist, manifest and entitlements. Returns the files by path in
    the app and the nested_code entries (path in the app)."""
    files, code = {}, []
    for name, identifier in frameworks:
        root = f"Frameworks/{name}.framework/"
        info = plistlib.dumps({"CFBundleIdentifier": identifier, "CFBundleExecutable": name, "CFBundlePackageType": "FMWK"})
        resources = signer_manifest({"Info.plist": info})
        binary = signed_fixture(identifier, sealed_files={1: info, 3: resources}, file_type=6)
        files.update({root + "Info.plist": info, root + "_CodeSignature/CodeResources": resources, root + name: binary})
        code.append({"path": root + name, "kind": "framework", "identifier": identifier,
                     "seals": {1: info, 3: resources}})
    for name in libraries:
        files["Frameworks/" + name] = signed_fixture(name[:-len(".dylib")], file_type=6)
        code.append({"path": "Frameworks/" + name, "kind": "library", "identifier": name[:-len(".dylib")], "seals": None})
    for name, identifier, point, entitlements in extensions:
        root = f"PlugIns/{name}.appex/"
        info = plistlib.dumps({"CFBundleIdentifier": identifier, "CFBundleExecutable": name, "CFBundlePackageType": "XPC!",
                               "CFBundleName": name, "CFBundleVersion": "1",
                               "NSExtension": {"NSExtensionPointIdentifier": point}})
        resources = signer_manifest({"Info.plist": info})
        binary = signed_fixture(identifier, entitlements, sealed_files={1: info, 3: resources})
        files.update({root + "Info.plist": info, root + "_CodeSignature/CodeResources": resources, root + name: binary})
        code.append({"path": root + name, "kind": "extension", "identifier": identifier, "seals": {1: info, 3: resources},
                     "entitlements": entitlements, "extension_point": point})
    return files, code


def application_fixture(name, bundle_id, entitlements, resources=("Assets.car",), embedded=None):
    """A synthetic staged app bundle: Info.plist, resources, signed embedded
    code (files of embedded_fixture), the executable signed with the bundle
    seals, and the manifest in the signer's shape, which seals the embedded
    code's signed bytes like any other file."""
    executable = name[:-4]
    info = plistlib.dumps({"CFBundleIdentifier": bundle_id, "CFBundleExecutable": executable,
                           "CFBundlePackageType": "APPL", "CFBundleName": executable})
    files = {"Info.plist": info, **{resource: (resource + " of " + bundle_id).encode() for resource in resources},
             **(embedded or {})}
    code_resources = signer_manifest(files)
    program = signed_fixture(bundle_id, entitlements, sealed_files={1: info, 3: code_resources})
    return {**files, "_CodeSignature/CodeResources": code_resources, executable: program}


class DeveloperApplicationTests(unittest.TestCase):
    """Developer app bundles beside the owned diagnostic: the policy table the
    adapter reads, the entitlement allowlist, and a payload that carries two
    apps. Fixtures are synthetic signatures, never guest execution."""
    RESEARCH = application_info("27.0")["CFBundleIdentifier"]
    HEADER = Path(__file__).resolve().parents[2] / "integrations/ios-emulator/guest-development-protocol.h"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tomacco-developer-apps-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def save(self, record):
        (self.root / "guest-tools.json").write_text(json.dumps(record))

    def fixture(self, apps, *, table=True, legacy=False, auto_lock=False):
        """A development-signing payload (agent, diagnostic, adapter, helper)
        with the given apps: (bundle name, bundle id, entitlements, resources)
        and optionally embedded code, (frameworks, libraries) of embedded_fixture."""
        programs, files, applications = [], {}, []

        def program(path, data, sealed=None, **extra):
            library = path.endswith(".dylib") or ("nested_in" in extra and extra.get("kind") != "extension")
            integrity = inspect_signed_code(data, cpu_subtype=2 if path.endswith(".dylib") and "nested_in" not in extra else 0,
                                            file_type=6 if library else 2,
                                            expected_identifier=extra.get("signing_identifier"),
                                            expected_entitlements=extra.get("entitlements", {}), sealed_files=sealed)
            programs.append({"path": path, "sha256": digest(data), "cdhash": integrity["cdhash"],
                             "integrity": integrity, "entitlements": {}, **extra})
            files[path] = data
        agent = program_entitlements("usr/libexec/tomacco-app-control", True, True, installer_connection_query=True,
                                     application_log_access=True, application_termination=True,
                                     time_zone_control=True, auto_lock_control=auto_lock)
        program("usr/libexec/tomacco-platform", signed_fixture("tomacco-platform"))
        program("usr/libexec/tomacco-app-control", signed_fixture("tomacco-app-control", agent), entitlements=agent)
        info = plistlib.dumps(application_info("27.0"))
        resources = plistlib.dumps({"files": {"Info.plist": hashlib.sha1(info).digest()}, "files2": {}})
        files["usr/libexec/TomaccoProbe.app/Info.plist"] = info
        files["usr/libexec/TomaccoProbe.app/_CodeSignature/CodeResources"] = resources
        grant = {"application-identifier": self.RESEARCH}
        program("usr/libexec/TomaccoProbe.app/TomaccoProbe",
                signed_fixture(self.RESEARCH, grant, sealed_files={1: info, 3: resources}),
                sealed={1: info, 3: resources}, entitlements=grant, signing_identifier=self.RESEARCH)
        for name, bundle_id, entitlements, resources, *nested in apps:
            embedded, code = embedded_fixture(*nested[0]) if nested else ({}, [])
            bundle = application_fixture(name, bundle_id, entitlements, resources, embedded)
            executable = name[:-4]
            for relative, data in bundle.items():
                files[f"Applications/{name}/{relative}"] = data
            seals = {1: bundle["Info.plist"], 3: bundle["_CodeSignature/CodeResources"]}
            program(f"Applications/{name}/{executable}", bundle[executable], sealed=seals, entitlements=entitlements,
                    signing_identifier=bundle_id, application=f"Applications/{name}")
            applications.append({"path": f"Applications/{name}", "name": name, "bundle_id": bundle_id,
                                 "executable": f"Applications/{name}/{executable}", "cdhash": programs[-1]["cdhash"],
                                 "entitlements": entitlements, "guest_path": GUEST_ROOT + f"/Applications/{name}",
                                 "source": {"path": "/host/" + name, "content_sha256": digest(name.encode())}})
            if nested:
                applications[-1]["nested_code"] = []
            for entry in code:
                path = f"Applications/{name}/{entry['path']}"
                extension = {"entitlements": entry["entitlements"]} if entry["kind"] == "extension" else {}
                program(path, bundle[entry["path"]], sealed=entry["seals"], architecture="arm64",
                        signing_identifier=entry["identifier"], nested_in=f"Applications/{name}", kind=entry["kind"],
                        **extension)
                applications[-1]["nested_code"].append({"path": path, "kind": entry["kind"], "identifier": entry["identifier"],
                                                        "cdhash": programs[-1]["cdhash"], "sha256": programs[-1]["sha256"],
                                                        **({**extension, "extension_point": entry["extension_point"]}
                                                           if extension else {})})
        # The adapter carries the table of the applications signed before it.
        library = signed_fixture("adapter", cpu_subtype=0x80000002, file_type=6)
        if table:
            library += development_policy_table(policy_entries(programs))
        program("usr/libexec/libtomacco-development-signing.dylib", library, architecture="arm64e")
        setup = program_entitlements("usr/libexec/tomacco-development-setup", True, True)
        program("usr/libexec/tomacco-development-setup", signed_fixture("setup", setup), entitlements=setup)
        jobs = launch_jobs(True, True, True)
        for job in jobs:
            files["System/Library/LaunchDaemons/" + job["Label"] + ".plist"] = plistlib.dumps(job)
        original = cache([b"A" * 20 + bytes.fromhex("02000200")])
        combined = extend_trustcache(original, [bytes.fromhex(row["cdhash"]) for row in programs])
        executables = {row["path"] for row in programs}
        record = {"format": 1, "scope": "ios-owned-guest-tools", "state": "built", "guest_root": GUEST_ROOT,
                  "target": "arm64-apple-ios27.0", "programs": programs, "jobs": jobs, "agent": True,
                  "installer_connection_query": True, "application_log_access": True,
                  "application_termination": True, "time_zone_control": True, "applications": applications,
                  "development_signing": development_policy(programs, legacy=legacy),
                  "files": [{"path": path, "bytes": len(data), "sha256": digest(data),
                             "mode": 0o755 if path in executables else 0o644} for path, data in files.items()],
                  "trustcache": {"source_sha256": digest(original), "sha256": digest(combined),
                                 "original_entries_preserved": True, "local_platform_policy": LOCAL_PLATFORM_POLICY.hex()}}
        if auto_lock:
            record["auto_lock_control"] = True
        shutil.rmtree(self.root / "Supplemental", ignore_errors=True)
        for path, data in files.items():
            target = self.root / "Supplemental" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (self.root / "system.tc").write_bytes(combined)
        self.save(record)
        return original, record

    def test_policy_table_round_trip_is_canonical_and_refuses_ambiguity(self):
        entries = [("io.example.second", b"\x02" * 20), ("io.example.first", b"\x01" * 20), (self.RESEARCH, b"\x03" * 20)]
        table = development_policy_table(entries)
        self.assertEqual(len(table), 8 + 3 * 280)
        self.assertTrue(table.startswith(POLICY_TABLE_MAGIC + b"\x03\0\0"))
        decoded = decode_development_policy_table(table)
        # Ascending by identifier bytes, whatever the input order.
        self.assertEqual(decoded, sorted(entries))
        self.assertEqual(development_policy_table(reversed(entries)), table)
        self.assertEqual(table[8:28], b"\x01" * 20)  # io.example.first sorts before io.github.marselnenaj...
        for bad in ([], entries * 11, entries + [("io.example.first", b"\x09" * 20)],
                    entries + [("io.example.third", b"\x01" * 20)], [("bad id!", b"\x01" * 20)],
                    [("io.example.first", b"\x01" * 19)], [("io.example.first", "01" * 20)], [None]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                development_policy_table(bad)
        one = development_policy_table([(self.RESEARCH, b"\x03" * 20)])
        self.assertEqual(decode_development_policy_table(one), [(self.RESEARCH, b"\x03" * 20)])
        first, second = table[8:288], table[288:568]
        damaged = {
            "magic": b"PSPU" + table[4:], "version": table[:4] + b"\x02" + table[5:],
            "count-zero": table[:5] + b"\0" + table[6:], "reserved": table[:6] + b"\1\0" + table[8:],
            "truncated": table[:-1], "trailing": table + b"\0",
            "order": table[:8] + second + first + table[568:],
            "flags": table[:28] + b"\1" + table[29:],
            "identifier-character": table[:32] + b"!" + table[33:],
            "identifier-empty": table[:32] + b"\0" + table[33:],
            "identifier-unterminated": table[:32] + b"a" * 256 + table[288:],
            "identifier-padding": table[:287] + b"x" + table[288:],
            "duplicate-hash": table[8:28].join([table[:8], table[28:288], table[308:]]) if False else
            table[:288] + table[8:28] + table[308:],
        }
        for name, data in damaged.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                decode_development_policy_table(data)

    def test_the_adapters_decoder_agrees_with_the_builders_encoding(self):
        compiler = shutil.which("clang") or shutil.which("cc")
        if not compiler:
            self.skipTest("A C compiler is required to exercise the adapter's actual table decoder")
        harness = self.root / "table.c"
        harness.write_text('#include ' + json.dumps(str(self.HEADER)) + '\n' + r'''
#include <stdio.h>
int main(int argc, char **argv) {
    static unsigned char buffer[16384];
    FILE *file = fopen(argv[1], "rb");
    unsigned long length = file ? fread(buffer, 1, sizeof(buffer), file) : 0;
    if (file) fclose(file);
    unsigned count = tomacco_policy_count(buffer, length);
    int found = count ? tomacco_policy_find(buffer, count, argv[2]) : -2;
    printf("%u %d", count, found);
    if (found >= 0) {
        printf(" %s ", tomacco_policy_identifier(buffer, (unsigned)found));
        const unsigned char *hash = tomacco_policy_cdhash(buffer, (unsigned)found);
        for (unsigned i = 0; i < 20; ++i) printf("%02x", hash[i]);
    }
    (void)argc;
    return 0;
}
''')
        executable = self.root / "table"
        subprocess.run([compiler, "-Wall", "-Wextra", "-Werror", str(harness), "-o", str(executable)],
                       check=True, capture_output=True, text=True, timeout=60)

        def decode(data, identifier):
            path = self.root / "candidate.bin"
            path.write_bytes(data)
            return subprocess.run([str(executable), str(path), identifier], check=True, capture_output=True,
                                  text=True, timeout=10).stdout.split()
        entries = [("io.example.second", bytes(range(20))), ("io.example.first", bytes(range(20, 40))),
                   (self.RESEARCH, b"\x03" * 20)]
        table = development_policy_table(entries)
        for identifier, cdhash in entries:
            index = decode_development_policy_table(table).index((identifier, cdhash))
            self.assertEqual(decode(table, identifier), ["3", str(index), identifier, cdhash.hex()])
        self.assertEqual(decode(table, "io.example.third"), ["3", "-1"])
        self.assertEqual(decode(table, "io.example.firs"), ["3", "-1"])
        self.assertEqual(decode(development_policy_table(entries[:1]), "io.example.second"),
                         ["1", "0", "io.example.second", bytes(range(20)).hex()])
        first, second = table[8:288], table[288:568]
        for name, data in {"magic": b"PSPU" + table[4:], "version": table[:4] + b"\x02" + table[5:],
                           "count": table[:5] + b"\x21" + table[6:] + b"\0" * 280 * 30,
                           "reserved": table[:7] + b"\1" + table[8:], "truncated": table[:-1],
                           "trailing": table + b"\0", "order": table[:8] + second + first + table[568:],
                           "flags": table[:28] + b"\1" + table[29:],
                           "identifier-character": table[:32] + b"!" + table[33:],
                           "identifier-leading-dot": table[:32] + b"." + table[33:],
                           "identifier-padding": table[:287] + b"x" + table[288:],
                           "empty": b""}.items():
            with self.subTest(name=name):
                # Both decoders refuse the same encodings.
                self.assertEqual(decode(data, "io.example.first"), ["0", "-2"])
                with self.assertRaises(ValueError):
                    decode_development_policy_table(data)

    def test_entitlement_allowlist_refuses_privileged_and_malformed_grants(self):
        bundle_id = "io.example.app"
        self.assertEqual(application_entitlements(bundle_id, None), {"application-identifier": bundle_id})
        self.assertEqual(application_entitlements(bundle_id, {}), {"application-identifier": bundle_id})
        full = {"application-identifier": "ABCDE12345." + bundle_id, "com.apple.developer.team-identifier": "ABCDE12345",
                "get-task-allow": True, "keychain-access-groups": ["ABCDE12345." + bundle_id],
                "com.apple.security.application-groups": ["group.io.example"]}
        self.assertEqual(application_entitlements(bundle_id, full), full)
        self.assertEqual(application_entitlements(bundle_id, {"application-identifier": "ZZZZZ99999." + bundle_id})
                         ["application-identifier"], "ZZZZZ99999." + bundle_id)
        for key in ("platform-application", "task_for_pid-allow", "com.apple.private.security.no-sandbox",
                    "com.apple.security.exception.files.absolute-path.read-write", "com.apple.springboard.launchapplications",
                    "com.apple.private.mobileinstall.allowedSPI", "seatbelt-profiles", "dynamic-codesigning"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, re.escape(key) + ".*not allowed"):
                application_entitlements(bundle_id, {key: True})
        for name, bad in {"identifier-type": {"application-identifier": True},
                          "identifier-other": {"application-identifier": "io.example.other"},
                          "identifier-team-mismatch": {"application-identifier": "ABCDE12345." + bundle_id,
                                                       "com.apple.developer.team-identifier": "FGHIJ67890"},
                          "identifier-short-prefix": {"application-identifier": "ABC." + bundle_id},
                          "team-format": {"com.apple.developer.team-identifier": "abcde12345"},
                          "task-allow-type": {"get-task-allow": 1},
                          "groups-type": {"keychain-access-groups": "io.example"},
                          "groups-empty": {"keychain-access-groups": []},
                          "groups-item": {"com.apple.security.application-groups": ["bad group!"]},
                          "groups-duplicate": {"keychain-access-groups": ["a.b", "a.b"]},
                          "not-a-dictionary": ["get-task-allow"]}.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                application_entitlements(bundle_id, bad)
        with self.assertRaises(ValueError):
            application_entitlements("not valid!", {})

    def test_two_staged_apps_are_trusted_admitted_and_verified(self):
        first = ("First.app", "io.example.first", {"application-identifier": "io.example.first"}, ("Assets.car", "en.lproj/Localizable.strings"))
        second = ("Second.app", "io.example.second", {"application-identifier": "io.example.second", "get-task-allow": True}, ("PkgInfo",))
        original, record = self.fixture([first, second])
        actual, files, combined = payload(self.root, original)
        self.assertEqual(actual, record)
        self.assertEqual(len(record["programs"]), 7)
        entries = cache_rows = {combined[offset:offset + 20]: combined[offset + 20:offset + 24]
                                for offset in range(24, len(combined), 24)}
        for app in record["applications"]:
            self.assertEqual(entries[bytes.fromhex(app["cdhash"])], LOCAL_PLATFORM_POLICY)
            self.assertIn(app["executable"], files)
        self.assertIn("Applications/First.app/en.lproj/Localizable.strings", files)
        self.assertEqual(len(cache_rows), 1 + 7)
        policy = record["development_signing"]
        self.assertEqual((policy["policy_entries"], policy["bundle_id"], policy["scope"]), (3, self.RESEARCH, "owned-cdhash"))
        self.assertEqual([app["bundle_id"] for app in policy["applications"]], ["io.example.first", "io.example.second"])
        table = embedded_policy_table(files["usr/libexec/libtomacco-development-signing.dylib"])
        self.assertEqual(digest(table), policy["policy_table_sha256"])
        self.assertEqual([identity for identity, _ in decode_development_policy_table(table)],
                         ["io.example.first", "io.example.second", self.RESEARCH])
        self.assertEqual(development_library(self.root, original)["app_cdhash"], policy["cdhash"])

        def refused(message, change):
            original, record = self.fixture([first, second])
            change(record)
            self.save(record)
            with self.assertRaisesRegex(ValueError, message):
                payload(self.root, original)
        # The record cannot grant more than the allowlist, nor relabel an app.
        def privileged(record):
            record["applications"][0]["entitlements"] = {"application-identifier": "io.example.first", "platform-application": True}
        refused("not allowed", privileged)
        def relabel(record):
            record["applications"][1]["bundle_id"] = "io.example.first"
        refused("share a bundle identifier|must be the bundle identifier", relabel)
        def unlisted(record):
            record["applications"].pop()
        refused("Unexpected native guest programs|Invalid native guest program inventory", unlisted)
        def wrong_hash(record):
            record["applications"][0]["cdhash"] = "ab" * 20
        refused("differs from its program|Unexpected development-signing", wrong_hash)
        def moved(record):
            record["applications"][0]["guest_path"] = "/private/var/First.app"
        refused("Invalid native guest application record", moved)
        # A legacy record (one compiled-in app) cannot carry staged apps.
        def legacy(record):
            record["development_signing"] = {key: value for key, value in record["development_signing"].items()
                                             if key not in ("policy_table_sha256", "policy_entries", "applications")}
        refused("cannot carry developer apps", legacy)
        # The bundle's own files are checked against the sealed manifest.
        original, record = self.fixture([first, second])
        (self.root / "Supplemental/Applications/First.app/Assets.car").write_bytes(b"replaced")
        for row in record["files"]:
            if row["path"] == "Applications/First.app/Assets.car":
                row.update(sha256=digest(b"replaced"), bytes=8)
        self.save(record)
        with self.assertRaisesRegex(ValueError, "differs from the file"):
            payload(self.root, original)
        original, record = self.fixture([first, second])
        extra = self.root / "Supplemental/Applications/Second.app/Extra.plist"
        extra.write_bytes(b"<plist/>")
        record["files"].append({"path": "Applications/Second.app/Extra.plist", "sha256": digest(b"<plist/>"),
                                "bytes": 8, "mode": 0o644})
        self.save(record)
        with self.assertRaisesRegex(ValueError, "resource inventory differs"):
            payload(self.root, original)
        # The adapter admits what its compiled-in table says; a library
        # without that table, or with another, is not the recorded policy.
        original, record = self.fixture([first, second], table=False)
        with self.assertRaisesRegex(ValueError, "no single valid policy table"):
            payload(self.root, original)
        original, record = self.fixture([first, second])
        library = self.root / "Supplemental/usr/libexec/libtomacco-development-signing.dylib"
        signed = signed_fixture("adapter", cpu_subtype=0x80000002, file_type=6)
        other = signed + development_policy_table(policy_entries(record["programs"])[:2])
        library.write_bytes(other)
        for row in record["programs"] + record["files"]:
            if row["path"] == "usr/libexec/libtomacco-development-signing.dylib":
                row.update(sha256=digest(other), **({"bytes": len(other)} if "bytes" in row else {}))
        record["development_signing"]["library_sha256"] = digest(other)
        self.save(record)
        with self.assertRaisesRegex(ValueError, "compiled-in policy table differs"):
            payload(self.root, original)
        # Two apps with one identity never reach a table.
        with self.assertRaisesRegex(ValueError, "same bundle identifier"):
            self.fixture([first, ("Other.app", "io.example.first", first[2], ("Assets.car",))])

    def test_embedded_frameworks_and_libraries_are_trusted_admitted_and_verified(self):
        embedded = ([("Kit", "io.example.kit"), ("Charts", "io.example.charts")], ["libSupport.dylib"])
        first = ("First.app", "io.example.first", {"application-identifier": "io.example.first"}, ("Assets.car",), embedded)
        second = ("Second.app", "io.example.second", {"application-identifier": "io.example.second"}, ("PkgInfo",))
        original, record = self.fixture([first, second])
        actual, files, combined = payload(self.root, original)
        self.assertEqual(actual, record)
        app = record["applications"][0]
        self.assertNotIn("nested_code", record["applications"][1])
        self.assertEqual([(code["path"], code["kind"], code["identifier"]) for code in app["nested_code"]], [
            ("Applications/First.app/Frameworks/Kit.framework/Kit", "framework", "io.example.kit"),
            ("Applications/First.app/Frameworks/Charts.framework/Charts", "framework", "io.example.charts"),
            ("Applications/First.app/Frameworks/libSupport.dylib", "library", "libSupport")])
        # Every embedded binary is trusted by its own cdhash, as the app is.
        entries = {combined[offset:offset + 20]: combined[offset + 20:offset + 24] for offset in range(24, len(combined), 24)}
        self.assertEqual(len(entries), 1 + len(record["programs"]))
        for code in app["nested_code"]:
            self.assertEqual(entries[bytes.fromhex(code["cdhash"])], LOCAL_PLATFORM_POLICY)
            self.assertEqual(digest(files[code["path"]]), code["sha256"])
        # installd verifies a framework as a bundle of its own, so the adapter's
        # table admits each framework's identifier with its cdhash; a library
        # is no bundle and has no entry.
        table = decode_development_policy_table(embedded_policy_table(files["usr/libexec/libtomacco-development-signing.dylib"]))
        self.assertEqual(table, sorted([(self.RESEARCH, bytes.fromhex(record["development_signing"]["cdhash"]))]
                                       + [(row["bundle_id"], bytes.fromhex(row["cdhash"])) for row in record["applications"]]
                                       + [(code["identifier"], bytes.fromhex(code["cdhash"])) for code in app["nested_code"][:2]]))
        policy = record["development_signing"]
        self.assertEqual(policy["policy_entries"], 5)
        self.assertEqual([row["bundle_id"] for row in policy["frameworks"]], ["io.example.kit", "io.example.charts"])
        modes = {row["path"]: row["mode"] for row in record["files"]}
        self.assertEqual((modes["Applications/First.app/Frameworks/libSupport.dylib"],
                          modes["Applications/First.app/Frameworks/Kit.framework/Kit"],
                          modes["Applications/First.app/Frameworks/Kit.framework/Info.plist"]), (0o755, 0o755, 0o644))

        def refused(message, change):
            original, record = self.fixture([first, second])
            change(record)
            self.save(record)
            with self.assertRaisesRegex(ValueError, message):
                payload(self.root, original)
        # A changed embedded binary is refused: the app's sealed manifest names
        # its signed bytes, and a manifest rewritten for it breaks the app's seal.
        def changed(name, replacement, reseal=False):
            def change(record):
                app = self.root / "Supplemental/Applications/First.app"
                (app / "Frameworks" / name).write_bytes(replacement)
                rewritten = {"Applications/First.app/Frameworks/" + name: replacement}
                if reseal:
                    contents = {str(path.relative_to(app)): path.read_bytes() for path in sorted(app.rglob("*"))
                                if path.is_file() and path.name != "First" and path != app / "_CodeSignature/CodeResources"}
                    rewritten["Applications/First.app/_CodeSignature/CodeResources"] = signer_manifest(contents)
                    (app / "_CodeSignature/CodeResources").write_bytes(signer_manifest(contents))
                for row in record["files"] + record["programs"] + record["applications"][0]["nested_code"]:
                    if row["path"] in rewritten:
                        data = rewritten[row["path"]]
                        row.update(sha256=digest(data), **({"bytes": len(data)} if "bytes" in row else {}))
            return change
        library = files["Applications/First.app/Frameworks/libSupport.dylib"]
        tampered = library[:40] + bytes([library[40] ^ 1]) + library[41:]
        refused("signed manifest's hash of Frameworks/libSupport.dylib", changed("libSupport.dylib", tampered))
        resigned = signed_fixture("libSupport", {"get-task-allow": True}, file_type=6)
        refused("signed manifest's hash of Frameworks/libSupport.dylib", changed("libSupport.dylib", resigned))
        refused("Native bundle seal changed", changed("libSupport.dylib", tampered, reseal=True))
        kit = files["Applications/First.app/Frameworks/Kit.framework/Kit"]
        refused("signed manifest's hash of Frameworks/Kit.framework/Kit",
                changed("Kit.framework/Kit", kit[:40] + bytes([kit[40] ^ 1]) + kit[41:]))
        refused("Native bundle seal changed", changed("Kit.framework/Kit", kit[:40] + bytes([kit[40] ^ 1]) + kit[41:], True))
        # The record cannot relabel, reroute, grant or drop embedded code.
        def wrong_hash(record):
            record["applications"][0]["nested_code"][2]["cdhash"] = "ab" * 20
        refused("differs from its embedded code", wrong_hash)
        def granted(record):
            row = next(row for row in record["programs"] if row["path"].endswith("libSupport.dylib"))
            row["entitlements"] = {"get-task-allow": True}
        refused("differs from its embedded code", granted)
        def relabeled(record):
            record["applications"][0]["nested_code"][0]["identifier"] = "io.example.other"
        refused("differs from its embedded code|does not declare its recorded identity", relabeled)
        def outside(record):
            record["applications"][0]["nested_code"][2]["path"] = "Applications/First.app/Plugins/libSupport.dylib"
        refused("invalid embedded code record", outside)
        def unlisted(record):
            record["applications"][0]["nested_code"].pop()
        refused("Unexpected native guest programs|Invalid native guest program inventory", unlisted)
        def as_app(record):
            record["applications"][0]["nested_code"][0]["identifier"] = "io.example.first"
        refused("invalid embedded code record", as_app)
        # A framework identifier cannot reach the table twice.
        with self.assertRaisesRegex(ValueError, "same bundle identifier"):
            self.fixture([first, ("Other.app", "io.example.other", {"application-identifier": "io.example.other"},
                                  ("Assets.car",), ([("Kit", "io.example.kit")], []))])
        # A rebase carries the embedded code's trust over too.
        original, record = self.fixture([first, second])
        source, replacement = self.root.parent / "nested-source.tc", self.root.parent / "nested-replacement.tc"
        self.addCleanup(source.unlink)
        self.addCleanup(replacement.unlink)
        source.write_bytes(original)
        replacement.write_bytes(cache([b"B" * 20 + bytes.fromhex("02400200")]))
        output = Path(self.temporary.name + "-nested-rebased")
        self.addCleanup(shutil.rmtree, output, True)
        rebase(self.root, source, replacement, output)
        _record, _files, combined = payload(output, replacement.read_bytes())
        for code in record["applications"][0]["nested_code"]:
            self.assertIn(bytes.fromhex(code["cdhash"]) + LOCAL_PLATFORM_POLICY, combined)

    def test_app_extensions_are_trusted_admitted_with_their_entitlements_and_verified(self):
        share = {"application-identifier": "io.example.first.share",
                 "com.apple.security.application-groups": ["group.io.example.first"]}
        embedded = ([("Kit", "io.example.kit")], [], [("Share", "io.example.first.share", "com.apple.share-services", share),
                                                      ("Widget", "io.example.first.widget", "com.apple.widgetkit-extension",
                                                       {"application-identifier": "io.example.first.widget"})])
        first = ("First.app", "io.example.first", {"application-identifier": "io.example.first"}, ("Assets.car",), embedded)
        second = ("Second.app", "io.example.second", {"application-identifier": "io.example.second"}, ("PkgInfo",))
        original, record = self.fixture([first, second])
        actual, files, combined = payload(self.root, original)
        self.assertEqual(actual, record)
        app = record["applications"][0]
        extensions = [code for code in app["nested_code"] if code["kind"] == "extension"]
        self.assertEqual([(code["path"], code["identifier"], code["extension_point"], code["entitlements"]) for code in extensions], [
            ("Applications/First.app/PlugIns/Share.appex/Share", "io.example.first.share", "com.apple.share-services", share),
            ("Applications/First.app/PlugIns/Widget.appex/Widget", "io.example.first.widget", "com.apple.widgetkit-extension",
             {"application-identifier": "io.example.first.widget"})])
        # Each extension executable is trusted by its own cdhash and admitted
        # by its own identity: installd verifies it as a bundle of its own.
        for code in extensions:
            self.assertIn(bytes.fromhex(code["cdhash"]) + LOCAL_PLATFORM_POLICY, combined)
        table = decode_development_policy_table(embedded_policy_table(files["usr/libexec/libtomacco-development-signing.dylib"]))
        self.assertEqual(table, sorted([(self.RESEARCH, bytes.fromhex(record["development_signing"]["cdhash"]))]
                                       + [(row["bundle_id"], bytes.fromhex(row["cdhash"])) for row in record["applications"]]
                                       + [(code["identifier"], bytes.fromhex(code["cdhash"]))
                                          for code in app["nested_code"] if code["kind"] != "library"]))
        policy = record["development_signing"]
        self.assertEqual(policy["policy_entries"], 6)
        self.assertEqual([row["bundle_id"] for row in policy["extensions"]], ["io.example.first.share", "io.example.first.widget"])
        self.assertEqual([row["bundle_id"] for row in policy["frameworks"]], ["io.example.kit"])
        self.assertNotIn("extensions", self.fixture([second])[1]["development_signing"])
        modes = {row["path"]: row["mode"] for row in record["files"]}
        self.assertEqual((modes["Applications/First.app/PlugIns/Share.appex/Share"],
                          modes["Applications/First.app/PlugIns/Share.appex/Info.plist"]), (0o755, 0o644))

        def refused(message, change):
            original, record = self.fixture([first, second])
            change(record)
            self.save(record)
            with self.assertRaisesRegex(ValueError, message):
                payload(self.root, original)

        def rewrite(record, relative, data, reseal=None):
            """Replace a file of First.app on disk and in the inventory; reseal
            rebuilds the named bundle's manifest (the app's or the extension's)."""
            app = self.root / "Supplemental/Applications/First.app"
            (app / relative).write_bytes(data)
            rewritten = {"Applications/First.app/" + relative: data}
            if reseal is not None:
                bundle = app / reseal if reseal else app
                binary = bundle.name[:-len(".appex")] if reseal else "First"
                contents = {str(path.relative_to(bundle)): path.read_bytes() for path in sorted(bundle.rglob("*"))
                            if path.is_file() and path.relative_to(bundle).parts[0] != "_CodeSignature" and path.name != binary}
                manifest = signer_manifest(contents)
                (bundle / "_CodeSignature/CodeResources").write_bytes(manifest)
                rewritten[str((bundle / "_CodeSignature/CodeResources").relative_to(self.root / "Supplemental"))] = manifest
            for row in record["files"] + record["programs"] + record["applications"][0]["nested_code"]:
                if row["path"] in rewritten:
                    row.update(sha256=digest(rewritten[row["path"]]),
                               **({"bytes": len(rewritten[row["path"]])} if "bytes" in row else {}))

        executable = files["Applications/First.app/PlugIns/Share.appex/Share"]
        tampered = executable[:40] + bytes([executable[40] ^ 1]) + executable[41:]
        refused("signed manifest's hash of PlugIns/Share.appex/Share",
                lambda record: rewrite(record, "PlugIns/Share.appex/Share", tampered))
        refused("Native bundle seal changed",
                lambda record: rewrite(record, "PlugIns/Share.appex/Share", tampered, reseal=""))
        # An extension re-signed with more entitlements than recorded.
        granted = signed_fixture("io.example.first.share", {**share, "get-task-allow": True},
                                 sealed_files={1: files["Applications/First.app/PlugIns/Share.appex/Info.plist"],
                                               3: files["Applications/First.app/PlugIns/Share.appex/_CodeSignature/CodeResources"]})
        refused("signed manifest's hash of PlugIns/Share.appex/Share",
                lambda record: rewrite(record, "PlugIns/Share.appex/Share", granted))
        refused("Native bundle seal changed", lambda record: rewrite(record, "PlugIns/Share.appex/Share", granted, reseal=""))
        # Its Info.plist is sealed by its own manifest and names the recorded identity and point.
        info = plistlib.loads(files["Applications/First.app/PlugIns/Share.appex/Info.plist"])
        moved = plistlib.dumps({**info, "NSExtension": {"NSExtensionPointIdentifier": "com.apple.widgetkit-extension"}})
        refused("does not declare its recorded identity",
                lambda record: rewrite(record, "PlugIns/Share.appex/Info.plist", moved, reseal=""))
        def resealed_extension(record):
            rewrite(record, "PlugIns/Share.appex/Info.plist", plistlib.dumps({**info, "CFBundleVersion": "2"}), reseal="PlugIns/Share.appex")
            rewrite(record, "Info.plist", files["Applications/First.app/Info.plist"], reseal="")
        refused("Native bundle seal changed", resealed_extension)
        # The record cannot grant, relabel, reroute or drop an extension.
        def entry(record, name="Share"):
            return next(code for code in record["applications"][0]["nested_code"] if code["path"].endswith("/" + name))
        def more_entitlements(record):
            code = entry(record)
            code["entitlements"] = {**code["entitlements"], "get-task-allow": True}
            next(row for row in record["programs"] if row["path"] == code["path"])["entitlements"] = code["entitlements"]
        refused("Native .*entitlement|Guest program changed", more_entitlements)
        def privileged(record):
            entry(record)["entitlements"] = {**share, "platform-application": True}
        refused("invalid embedded code record", privileged)
        def foreign(record):
            entry(record)["identifier"] = "io.example.second.share"
        refused("invalid embedded code record", foreign)
        def dotted(record):
            entry(record)["identifier"] = "io.example.first.share.more"
        refused("invalid embedded code record", dotted)
        def as_framework(record):
            entry(record)["kind"] = "framework"
        refused("invalid embedded code record", as_framework)
        def outside(record):
            entry(record)["path"] = "Applications/First.app/Frameworks/Share.appex/Share"
        refused("invalid embedded code record", outside)
        def no_point(record):
            del entry(record)["extension_point"]
        refused("invalid embedded code record", no_point)
        def other_point(record):
            entry(record)["extension_point"] = "com.apple.widgetkit-extension"
        refused("does not declare its recorded identity", other_point)
        def wrong_hash(record):
            entry(record)["cdhash"] = "ab" * 20
        refused("differs from its embedded code", wrong_hash)
        def unlisted(record):
            record["applications"][0]["nested_code"].remove(entry(record))
        refused("Unexpected native guest programs|Invalid native guest program inventory", unlisted)
        def unadmitted(record):
            record["development_signing"]["extensions"].pop()
        refused("Unexpected development-signing scope", unadmitted)
        # An extension identifier cannot reach the table twice.
        with self.assertRaisesRegex(ValueError, "same bundle identifier"):
            self.fixture([first, ("Other.app", "io.example.first.share", {"application-identifier": "io.example.first.share"},
                                  ("Assets.car",))])

    def test_staged_apps_are_carried_through_a_rebase_onto_another_trust_cache(self):
        apps = [("First.app", "io.example.first", {"application-identifier": "io.example.first"}, ("Assets.car",))]
        original, record = self.fixture(apps)
        source, replacement = self.root.parent / "apps-source.tc", self.root.parent / "apps-replacement.tc"
        self.addCleanup(source.unlink)
        self.addCleanup(replacement.unlink)
        source.write_bytes(original)
        replacement.write_bytes(cache([b"B" * 20 + bytes.fromhex("02400200")]))
        output = Path(self.temporary.name + "-rebased")
        self.addCleanup(shutil.rmtree, output, True)
        rebase(self.root, source, replacement, output)
        rebased, files, combined = payload(output, replacement.read_bytes())
        self.assertEqual(rebased["applications"], record["applications"])
        self.assertEqual(rebased["development_signing"], record["development_signing"])
        self.assertIn(bytes.fromhex(record["applications"][0]["cdhash"]) + LOCAL_PLATFORM_POLICY, combined)
        self.assertIn(b"B" * 20, combined)

    def test_the_auto_lock_grant_is_recorded_and_older_records_without_it_still_install(self):
        first = ("First.app", "io.example.first", {"application-identifier": "io.example.first"}, ("PkgInfo",))
        original, record = self.fixture([first], auto_lock=True)
        actual, _files, _combined = payload(self.root, original)
        self.assertIs(actual["auto_lock_control"], True)
        control = next(row for row in record["programs"] if row["path"] == "usr/libexec/tomacco-app-control")
        self.assertIs(control["entitlements"]["com.apple.managedconfiguration.profiled-access"], True)
        # A control service signed with the grant must be recorded with it.
        self.save({key: value for key, value in record.items() if key != "auto_lock_control"})
        with self.assertRaisesRegex(ValueError, "Unexpected native guest capability grant"):
            payload(self.root, original)
        # Only with the agent, and only as a Boolean.
        for claimed in ("yes", None):
            self.save({**record, "auto_lock_control": claimed})
            with self.assertRaisesRegex(ValueError, "Invalid native auto-lock capability"):
                payload(self.root, original)
        # A build from before the grant (no key) still installs.
        original, record = self.fixture([first])
        self.assertNotIn("auto_lock_control", record)
        self.assertNotIn("auto_lock_control", payload(self.root, original)[0])

    def test_a_record_from_before_the_policy_table_is_still_read_and_carries_no_apps(self):
        # The earlier shape: one compiled-in app, no table in the adapter.
        original, record = self.fixture([], table=False, legacy=True)
        self.assertNotIn("policy_table_sha256", record["development_signing"])
        actual, _files, _cache = payload(self.root, original)
        self.assertEqual(actual["applications"], [])
        # The same programs described by a current record need the table.
        current = development_policy(record["programs"])
        self.assertEqual((current["policy_entries"], current["applications"], current["cdhash"]),
                         (1, [], record["development_signing"]["cdhash"]))
        self.assertEqual({key: value for key, value in current.items()
                          if key not in ("policy_table_sha256", "policy_entries", "applications")},
                         record["development_signing"])
        record["development_signing"] = current
        self.save(record)
        with self.assertRaisesRegex(ValueError, "no single valid policy table"):
            payload(self.root, original)
        with self.assertRaisesRegex(ValueError, "cannot carry developer apps"):
            development_policy(record["programs"] + [{"path": "Applications/X.app/X", "cdhash": "ab" * 20,
                                                      "signing_identifier": "io.example.x", "application": "Applications/X.app"}],
                               legacy=True)

    def test_the_adapter_is_inserted_from_installd_cache_not_the_cryptex_path(self):
        # installd's file-system sandbox maps an executable from installd's own
        # cache but not from the Cryptex supplemental volume,
        # so the helper copies the adapter into that cache and inserts it from
        # there (DESTINATION), reading it from the Cryptex (SOURCE). An earlier
        # build that inserted straight from the Cryptex path never loaded.
        from build_ios_guest_tools import SOURCES
        source = (SOURCES / "guest-development-setup.c").read_text()
        insert = [line for line in source.splitlines()
                  if "DYLD_INSERT_LIBRARIES" in line and "string(environment" in line]
        self.assertEqual(len(insert), 1, insert)
        self.assertIn("TOMACCO_LIBRARY_DESTINATION", insert[0])
        self.assertNotIn("TOMACCO_LIBRARY_SOURCE", insert[0])
        self.assertIn("copy_library", source)      # the adapter is copied into the cache
        self.assertIn("Tomacco", source)      # ...under installd's cache directory
        self.assertIn("insert_library", source)    # configuration-detail logs the inserted path
        # The build emits the cache destination macro the helper inserts from.
        builder = (SOURCES.parents[1] / "tools/build_ios_guest_tools.py").read_text()
        self.assertIn('"#define TOMACCO_LIBRARY_DESTINATION " + json.dumps("/private/var/installd/Library/Caches/Tomacco/"', builder)

    def test_substitute_library_experiment_switch_is_guarded(self):
        from types import SimpleNamespace
        from build_ios_guest_tools import build
        common = dict(agent=True, application_probe=True, substitute_library="/tmp/x.dylib")
        with self.assertRaisesRegex(ValueError, "development-signing experiment"):
            build(SimpleNamespace(development_signing=False, applications=[], **common))
        with self.assertRaisesRegex(ValueError, "stage no developer apps"):
            build(SimpleNamespace(development_signing=True, applications=["/a.app"], **common))

    def test_an_installed_app_has_no_lifetime_bound(self):
        source = (Path(__file__).resolve().parents[2]
                  / "integrations/ios-emulator/research-application.c").read_text()
        # SIGALRM would end every installed sample app after 120 s.
        self.assertIn("#ifdef TOMACCO_APPLICATION_FILE_LOG\n    if (stdio) alarm(120);\n#else\n    alarm(120);\n#endif", source)
        self.assertEqual(source.count("alarm(120)"), 2)

    def test_a_real_build_stages_signs_and_admits_a_second_app(self):
        from types import SimpleNamespace
        import build_ios_sample_app
        repo = Path(__file__).resolve().parents[2]
        linker = repo / ".tooling/ios-linker/prefix/bin/aarch64-apple-darwin-ld"
        signer = repo / ".tooling/ios-sptm/support/ldid/ldid"
        compiler = shutil.which("clang-22") or shutil.which("clang")
        if not compiler or not linker.is_file() or not signer.is_file():
            self.skipTest("The local Linux iOS linker and ldid are required for actual payload signing")
        qualified = compiler_identity(compiler)["major"] in QUALIFIED_DEVELOPMENT_COMPILERS
        sample = build_ios_sample_app.build(SimpleNamespace(
            output=self.root / "sample", linker=linker, clang=compiler, minimum_ios="27.0", name="TomaccoSample",
            display_name="Tomacco Sample", bundle_id="io.github.marselnenaj.tomacco.sample-app"))
        self.assertFalse(sample["signed"])
        bundle = Path(sample["bundle"])
        # A resource beside the executable is sealed too.
        (bundle / "en.lproj").mkdir()
        (bundle / "en.lproj/Localizable.strings").write_text('"hello" = "Hello";\n')
        entitlements = self.root / "sample.entitlements"
        entitlements.write_bytes(plistlib.dumps({"get-task-allow": True}))
        trustcache = self.root / "original.tc"
        original = cache()
        trustcache.write_bytes(original)
        output = self.root / "tools"
        record = build(SimpleNamespace(output=output, trustcache=trustcache, linker=linker, ldid=signer, clang=compiler,
                                       minimum_ios="27.0", agent=True, application_probe=True, development_signing=True,
                                       quiet_services=False, allow_unqualified_compiler=not qualified,
                                       applications=[bundle], application_entitlements=[entitlements]))
        app, = record["applications"]
        self.assertEqual((app["bundle_id"], app["name"], app["executable"]),
                         ("io.github.marselnenaj.tomacco.sample-app", "TomaccoSample.app", "Applications/TomaccoSample.app/TomaccoSample"))
        self.assertEqual(app["entitlements"], {"get-task-allow": True, "application-identifier": app["bundle_id"]})
        self.assertEqual(app["source"]["entitlements_source"], "file")
        self.assertEqual(record["development_signing"]["policy_entries"], 2)
        self.assertEqual(record["development_signing"]["applications"], [{"bundle_id": app["bundle_id"], "cdhash": app["cdhash"], "path": app["path"]}])
        actual, files, combined = payload(output, original)
        self.assertEqual(actual, record)
        self.assertIn("Applications/TomaccoSample.app/en.lproj/Localizable.strings", files)
        self.assertIn(bytes.fromhex(app["cdhash"]) + LOCAL_PLATFORM_POLICY, combined)
        table = decode_development_policy_table(embedded_policy_table(files["usr/libexec/libtomacco-development-signing.dylib"]))
        self.assertEqual(table, sorted([(self.RESEARCH, bytes.fromhex(record["development_signing"]["cdhash"])),
                                        (app["bundle_id"], bytes.fromhex(app["cdhash"]))]))
        # The adapter is copied into installd's cache and inserted from there
        # (installd's sandbox maps its own cache but not the Cryptex volume).
        # The arm64 setup names both the Cryptex read source
        # and the installd-cache destination.
        setup = files["usr/libexec/tomacco-development-setup"]
        self.assertIn(b"/System/Cryptexes/Supplemental/usr/libexec/libtomacco-development-signing.dylib", setup)
        self.assertIn(b"/private/var/installd/Library/Caches/Tomacco", setup)
        self.assertEqual(next(p["architecture"] for p in record["programs"]
                              if p["path"] == "usr/libexec/tomacco-development-setup"), "arm64")
        # The staged copy is the signed one; the input bundle is untouched.
        self.assertFalse((bundle / "_CodeSignature").exists())
        self.assertEqual((output / "Supplemental/Applications/TomaccoSample.app/TomaccoSample").stat().st_mode & 0o777, 0o755)
        self.assertEqual((output / "Supplemental/Applications/TomaccoSample.app/Info.plist").stat().st_mode & 0o777, 0o644)
        # Apps need the policy table; a second bundle with the same identity is refused.
        with self.assertRaisesRegex(ValueError, "requires development signing"):
            build(SimpleNamespace(output=self.root / "plain", trustcache=trustcache, linker=linker, ldid=signer,
                                  clang=compiler, minimum_ios="27.0", agent=True, application_probe=True,
                                  development_signing=False, applications=[bundle]))
        twin = self.root / "twin/TomaccoSample.app"
        shutil.copytree(bundle, twin)
        with self.assertRaisesRegex(ValueError, "Two app bundles"):
            build(SimpleNamespace(output=self.root / "twins", trustcache=trustcache, linker=linker, ldid=signer,
                                  clang=compiler, minimum_ios="27.0", agent=True, application_probe=True,
                                  development_signing=True, allow_unqualified_compiler=not qualified,
                                  applications=[bundle, twin]))
        self.assertFalse((self.root / "twins").exists())

    def test_a_real_build_signs_embedded_frameworks_and_libraries_before_their_app(self):
        from types import SimpleNamespace
        import build_ios_sample_app
        repo = Path(__file__).resolve().parents[2]
        linker = repo / ".tooling/ios-linker/prefix/bin/aarch64-apple-darwin-ld"
        signer = repo / ".tooling/ios-sptm/support/ldid/ldid"
        compiler = shutil.which("clang-22") or shutil.which("clang")
        if not compiler or not linker.is_file() or not signer.is_file():
            self.skipTest("The local Linux iOS linker and ldid are required for actual payload signing")
        qualified = compiler_identity(compiler)["major"] in QUALIFIED_DEVELOPMENT_COMPILERS
        sample = build_ios_sample_app.build(SimpleNamespace(
            output=self.root / "sample", linker=linker, clang=compiler, minimum_ios="27.0", name="TomaccoKitSample",
            display_name="Tomacco Kit Sample", bundle_id="io.example.kit-sample", embedded_framework=True))
        bundle = Path(sample["bundle"])
        kit_path = "Frameworks/TomaccoKitSampleKit.framework/TomaccoKitSampleKit"
        self.assertEqual([(row["path"], row["kind"], row["identifier"]) for row in sample["nested_code"]], [
            (kit_path, "framework", "io.example.kit-sample.kit"),
            ("Frameworks/libTomaccoKitSampleSupport.dylib", "library", "libTomaccoKitSampleSupport")])
        # The executable loads both from its own bundle.
        executable = (bundle / "TomaccoKitSample").read_bytes()
        for name in (b"@rpath/TomaccoKitSampleKit.framework/TomaccoKitSampleKit", b"@rpath/libTomaccoKitSampleSupport.dylib",
                     b"@executable_path/Frameworks"):
            self.assertIn(name, executable)
        trustcache = self.root / "original.tc"
        original = cache()
        trustcache.write_bytes(original)
        output = self.root / "tools"
        record = build(SimpleNamespace(output=output, trustcache=trustcache, linker=linker, ldid=signer, clang=compiler,
                                       minimum_ios="27.0", agent=True, application_probe=True, development_signing=True,
                                       quiet_services=False, allow_unqualified_compiler=not qualified,
                                       applications=[bundle], application_entitlements=[]))
        app, = record["applications"]
        kit, support = app["nested_code"]
        self.assertEqual((kit["path"], kit["kind"], kit["identifier"]), (app["path"] + "/" + kit_path, "framework", "io.example.kit-sample.kit"))
        self.assertEqual((support["kind"], support["identifier"]), ("library", "libTomaccoKitSampleSupport"))
        actual, files, combined = payload(output, original)
        self.assertEqual(actual, record)
        for code in (app, kit, support):
            self.assertIn(bytes.fromhex(code["cdhash"]) + LOCAL_PLATFORM_POLICY, combined)
        table = decode_development_policy_table(embedded_policy_table(files["usr/libexec/libtomacco-development-signing.dylib"]))
        self.assertEqual(sorted(identity for identity, _ in table), [app["bundle_id"], kit["identifier"], self.RESEARCH])
        self.assertIn((kit["identifier"], bytes.fromhex(kit["cdhash"])), table)
        # Inside out: the app's manifest seals the signed embedded bytes, which
        # carry their own identifiers and no entitlements; the framework has
        # its own manifest. The input bundle is not touched.
        staged = output / "Supplemental" / app["path"]
        manifest = plistlib.loads((staged / "_CodeSignature/CodeResources").read_bytes())
        for code in (kit, support):
            relative = code["path"][len(app["path"]) + 1:]
            self.assertEqual(manifest["files2"][relative]["hash2"], hashlib.sha256((staged / relative).read_bytes()).digest())
            row = next(row for row in record["programs"] if row["path"] == code["path"])
            self.assertEqual((row["entitlements"], row["signing_identifier"], row["nested_in"]), ({}, code["identifier"], app["path"]))
            self.assertEqual(subprocess.check_output([str(signer), "-e", str(staged / relative)]).strip(), b"")
        self.assertIn("Frameworks/TomaccoKitSampleKit.framework/_CodeSignature/CodeResources", manifest["files"])
        self.assertEqual(plistlib.loads((staged / "Frameworks/TomaccoKitSampleKit.framework/_CodeSignature/CodeResources").read_bytes())["files"].keys(),
                         {"Info.plist"})
        self.assertEqual((staged / kit_path).stat().st_mode & 0o777, 0o755)
        self.assertFalse((bundle / "_CodeSignature").exists())
        self.assertFalse((bundle / "Frameworks/TomaccoKitSampleKit.framework/_CodeSignature").exists())
        # A changed embedded binary is refused even with a rehashed inventory.
        target = staged / "Frameworks/libTomaccoKitSampleSupport.dylib"
        data = bytearray(target.read_bytes())
        data[64] ^= 1
        target.write_bytes(bytes(data))
        for row in record["files"]:
            if row["path"] == support["path"]:
                row["sha256"] = hashlib.sha256(bytes(data)).hexdigest()
        (output / "guest-tools.json").write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "signed manifest's hash of Frameworks/libTomaccoKitSampleSupport.dylib"):
            payload(output, original)

    def test_a_real_build_signs_app_extensions_with_their_entitlements_before_their_app(self):
        from types import SimpleNamespace
        import build_ios_sample_app
        repo = Path(__file__).resolve().parents[2]
        linker = repo / ".tooling/ios-linker/prefix/bin/aarch64-apple-darwin-ld"
        signer = repo / ".tooling/ios-sptm/support/ldid/ldid"
        compiler = shutil.which("clang-22") or shutil.which("clang")
        if not compiler or not linker.is_file() or not signer.is_file():
            self.skipTest("The local Linux iOS linker and ldid are required for actual payload signing")
        qualified = compiler_identity(compiler)["major"] in QUALIFIED_DEVELOPMENT_COMPILERS
        sample = build_ios_sample_app.build(SimpleNamespace(
            output=self.root / "sample", linker=linker, clang=compiler, minimum_ios="27.0", name="TomaccoWidgets",
            display_name="Tomacco Widgets", bundle_id="io.example.widgets", embedded_framework=True,
            app_extension="widget"))
        bundle = Path(sample["bundle"])
        widget_path = "PlugIns/TomaccoWidgetsWidget.appex/TomaccoWidgetsWidget"
        self.assertEqual([(row["path"], row["kind"], row["identifier"]) for row in sample["nested_code"]], [
            ("Frameworks/TomaccoWidgetsKit.framework/TomaccoWidgetsKit", "framework", "io.example.widgets.kit"),
            ("Frameworks/libTomaccoWidgetsSupport.dylib", "library", "libTomaccoWidgetsSupport"),
            (widget_path, "extension", "io.example.widgets.widget")])
        info = plistlib.loads((bundle / "PlugIns/TomaccoWidgetsWidget.appex/Info.plist").read_bytes())
        self.assertEqual((info["CFBundlePackageType"], info["NSExtension"]), ("XPC!", {"NSExtensionPointIdentifier": "com.apple.widgetkit-extension"}))
        self.assertIn(b"NSExtensionMain", (bundle / widget_path).read_bytes())
        trustcache = self.root / "original.tc"
        original = cache()
        trustcache.write_bytes(original)
        output = self.root / "tools"
        record = build(SimpleNamespace(output=output, trustcache=trustcache, linker=linker, ldid=signer, clang=compiler,
                                       minimum_ios="27.0", agent=True, application_probe=True, development_signing=True,
                                       quiet_services=False, allow_unqualified_compiler=not qualified,
                                       applications=[bundle], application_entitlements=[]))
        app, = record["applications"]
        kit, support, widget = app["nested_code"]
        self.assertEqual((widget["path"], widget["kind"], widget["identifier"], widget["extension_point"], widget["entitlements"]),
                         (app["path"] + "/" + widget_path, "extension", "io.example.widgets.widget",
                          "com.apple.widgetkit-extension", {"application-identifier": "io.example.widgets.widget"}))
        actual, files, combined = payload(output, original)
        self.assertEqual(actual, record)
        for code in (app, kit, support, widget):
            self.assertIn(bytes.fromhex(code["cdhash"]) + LOCAL_PLATFORM_POLICY, combined)
        table = decode_development_policy_table(embedded_policy_table(files["usr/libexec/libtomacco-development-signing.dylib"]))
        self.assertEqual(sorted(table), sorted([(self.RESEARCH, bytes.fromhex(record["development_signing"]["cdhash"]))]
                                               + [(code["identifier"] if "identifier" in code else code["bundle_id"],
                                                   bytes.fromhex(code["cdhash"])) for code in (app, kit, widget)]))
        self.assertEqual(record["development_signing"]["extensions"],
                         [{"bundle_id": widget["identifier"], "cdhash": widget["cdhash"], "path": widget["path"],
                           "application": app["path"]}])
        # Inside out: the extension is signed under its identifier, with its
        # entitlements and its own seals, before the app's manifest seals it.
        staged = output / "Supplemental" / app["path"]
        manifest = plistlib.loads((staged / "_CodeSignature/CodeResources").read_bytes())
        self.assertEqual(manifest["files2"][widget_path]["hash2"], hashlib.sha256((staged / widget_path).read_bytes()).digest())
        self.assertIn("PlugIns/TomaccoWidgetsWidget.appex/_CodeSignature/CodeResources", manifest["files"])
        own = plistlib.loads((staged / "PlugIns/TomaccoWidgetsWidget.appex/_CodeSignature/CodeResources").read_bytes())
        self.assertEqual(own["files"].keys(), {"Info.plist"})
        self.assertEqual(plistlib.loads(subprocess.check_output([str(signer), "-e", str(staged / widget_path)])),
                         widget["entitlements"])
        self.assertIn("Identifier=io.example.widgets.widget",
                      subprocess.check_output([str(signer), "-h", str(staged / widget_path)], text=True))
        self.assertEqual((staged / widget_path).stat().st_mode & 0o777, 0o755)
        self.assertFalse((bundle / "PlugIns/TomaccoWidgetsWidget.appex/_CodeSignature").exists())
        # A changed extension executable is refused even with a rehashed inventory.
        target = staged / widget_path
        data = bytearray(target.read_bytes())
        data[64] ^= 1
        target.write_bytes(bytes(data))
        for row in record["files"]:
            if row["path"] == widget["path"]:
                row["sha256"] = hashlib.sha256(bytes(data)).hexdigest()
        (output / "guest-tools.json").write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "signed manifest's hash of " + widget_path):
            payload(output, original)


class GuestToolsJobs(Isolated):
    def test_build_passes_service_quieting_only_when_asked(self):
        (self.root / "trustcache").write_bytes(cache())
        tools = {}
        for name in ("clang", "linker", "ldid"):
            path = self.root / name
            path.write_text("#!/bin/sh\nexit 3\n")
            path.chmod(0o755)
            tools[name] = str(path)
        for flag, output in ((True, "quiet"), (False, "plain")):
            job = dispatch("ios.emulator.build_guest_tools", {
                **tools, "output": str(self.root / output), "trustcache": str(self.root / "trustcache"),
                "minimum_ios": "27.0", "application_probe": True, "agent": True, "quiet_services": flag})
            argv = job["plan"]["steps"][0]["argv"]
            self.assertEqual("--quiet-services" in argv, flag)
            self.assertEqual(argv[-1], "--quiet-services" if flag else "--agent")
            self.assertEqual(jobs.wait(job["id"], 20)["state"], "failed")

    def test_invalid_payload_fails_as_durable_job_before_any_disk_copy(self):
        args = {"seconds": 20, "accel": "tcg"}
        for name in ("tools", "support", "bios"):
            path = self.root / name
            path.mkdir()
            args[name] = str(path)
        for name in ("disk", "auxiliary_disk", "sep_storage", "device_tree", "helper_engine", "helper_kernel"):
            path = self.root / name
            path.write_text("synthetic source, not firmware")
            args[name] = str(path)
        (self.root / "trustcache").write_bytes(cache())
        args.update(trustcache=str(self.root / "trustcache"), output=str(self.root / "output"))
        (self.root / "tools/guest-tools.json").write_text('{"state":"failed"}')
        job = dispatch("ios.emulator.prepare_guest_tools", args)
        done = jobs.wait(job["id"], 10)
        self.assertEqual(done["state"], "failed", jobs.logs(job["id"]))
        receipt = dispatch("ios.emulator.report", {"job_id": job["id"]})["report"]
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("completed owned", receipt["error"])
        self.assertFalse((self.root / "output/system.raw").exists())
        self.assertFalse((self.root / "output/helper").exists())
        self.assertFalse(receipt["guest_execution_verified"])
        self.assertEqual(Path(args["disk"]).read_text(), "synthetic source, not firmware")
