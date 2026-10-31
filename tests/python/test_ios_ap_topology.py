"""Execute the production AP topology owner with synthetic ADT accessors.

These checks do not qualify QEMU integration, AP execution or SEP persistence.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
BOARD = ROOT / "integrations/ios-emulator/vphone-research.c"


class APTopologyOwnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("clang") or shutil.which("cc")
        if not compiler:
            raise unittest.SkipTest("A C compiler is required for the AP topology owner")
        cls.directory = tempfile.TemporaryDirectory(prefix="tomacco-ap-topology-")
        cls.addClassCleanup(cls.directory.cleanup)
        directory = Path(cls.directory.name)
        source = BOARD.read_text()
        start = source.index("static bool vphone_validate_ap_topology(")
        end = source.index("\nstatic void vphone_init(", start)
        limit = next(line for line in source.splitlines()
                     if line.startswith("#define VPHONE_MAX_AP_CPUS "))
        # Compile the exact production owner; the fixture contains no copy.
        (directory / "vphone-ap-topology-owner.h").write_text(
            limit + '\n#line ' + str(source[:start].count("\n") + 1)
            + ' "vphone-research.c"\n' + source[start:end])
        cls.binary = directory / "owner"
        fixture = ROOT / "tests/fixtures/ios_ap_topology_harness.c"
        subprocess.run([compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(directory), str(fixture), "-o", str(cls.binary)],
                       check=True, capture_output=True, text=True, timeout=30)

    def check_case(self, case):
        subprocess.run([str(self.binary), case], check=True, capture_output=True,
                       text=True, timeout=10)

    def test_one_eight_and_sixteen_aps_validate_every_affinity_without_mutation(self):
        for count in (1, 8, 16):
            with self.subTest(count=count):
                self.check_case(f"valid-{count}")

    def test_missing_parent_and_invalid_or_mismatched_child_counts_are_rejected(self):
        self.check_case("counts")

    def test_skipped_extra_duplicate_and_noncanonical_names_are_rejected(self):
        self.check_case("names")

    def test_missing_reg_never_reaches_the_unsafe_length_accessor(self):
        self.check_case("missing-reg")

    def test_wrong_reg_lengths_never_reach_the_u32_load(self):
        self.check_case("reg-length")

    def test_wrong_and_swapped_reg_affinities_are_rejected(self):
        self.check_case("reg-affinity")

    def test_board_calls_the_single_owner_before_creating_any_ap(self):
        source = BOARD.read_text()
        start = source.index("static void vphone_init(")
        end = source.index("\nstatic void vphone_class_init(", start)
        initializer = source[start:end]
        call = "vphone_validate_ap_topology(root, ap_cpus)"
        self.assertEqual(initializer.count(call), 1)
        self.assertLess(initializer.index(call),
                        initializer.index("ARM_CPU(object_new(ms->cpu_type))"))
        self.assertNotIn("adt_get_prop_len(cpu_node", initializer)
        self.assertNotIn("extra_cpu_path", initializer)


if __name__ == "__main__":
    unittest.main()
