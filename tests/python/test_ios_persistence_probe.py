"""The shipped bounded file owner; synthetic Darwin calls are not SEP evidence."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class PersistenceProbeOwnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("clang") or shutil.which("cc")
        if not compiler:
            raise unittest.SkipTest("A C compiler is required for the persistence owner")
        cls.directory = tempfile.TemporaryDirectory(prefix="tomacco-persistence-owner-")
        cls.addClassCleanup(cls.directory.cleanup)
        root = Path(__file__).resolve().parents[2]
        cls.binary = Path(cls.directory.name) / "owner"
        subprocess.run([compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        str(root / "tests/fixtures/ios_persistence_probe_harness.c"),
                        "-o", str(cls.binary)], check=True, capture_output=True, text=True, timeout=30)

    def check_case(self, case):
        subprocess.run([str(self.binary), case], check=True, capture_output=True, text=True, timeout=10)

    def test_only_exact_create_read_and_lowercase_hex_are_accepted(self):
        self.check_case("arguments")

    def test_exclusive_create_sets_class_before_write_flushes_and_reopens(self):
        self.check_case("create")

    def test_read_is_bounded_preserves_eintr_and_never_changes_file(self):
        self.check_case("read")

    def test_identity_virtual_mobile_and_api_guards_precede_io(self):
        self.check_case("guards")

    def test_file_metadata_must_be_regular_private_single_link_and_exact_size(self):
        self.check_case("metadata")

    def test_an_existing_challenge_is_never_overwritten(self):
        self.check_case("no-overwrite")

    def test_native_failures_close_descriptors_and_keep_uncertain_created_file(self):
        self.check_case("failures")

    def test_wrong_class_content_replacement_or_concurrent_change_refuses(self):
        self.check_case("mismatches")

    # The class survey (`classes:<challenge>`): one file per class A..D.
    def test_the_class_survey_argument_is_the_third_bounded_form(self):
        self.check_case("classes-arguments")

    def test_the_survey_creates_each_missing_class_file_with_its_class_and_reads_it_back(self):
        self.check_case("classes-create")

    def test_the_survey_reads_existing_class_files_without_writing(self):
        self.check_case("classes-read")

    def test_a_class_that_cannot_be_set_is_removed_and_the_other_classes_go_on(self):
        self.check_case("classes-partial")

    def test_a_file_the_keystore_cannot_unwrap_is_reported_with_its_errno(self):
        self.check_case("classes-unreadable")

    def test_survey_guards_and_directory_identity_precede_any_class_file(self):
        self.check_case("classes-guards")

    # The inode lookup (`inode:<decimal>`): a boot log's failing inode to its
    # path and protection class, without opening the file.
    def test_the_inode_lookup_argument_is_a_bounded_decimal(self):
        self.check_case("inode-arguments")

    def test_the_lookup_resolves_the_path_and_class_without_opening_the_file(self):
        self.check_case("inode-lookup")

    def test_lookup_failures_keep_their_errno_and_guards_precede_the_lookup(self):
        self.check_case("inode-failures")


if __name__ == "__main__":
    unittest.main()
