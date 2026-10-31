"""Bounded waiting for host pressure admission."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import ios_host_pressure


def sample(admitted):
    return {"admitted": admitted, "blocked_resources": [] if admitted else ["io"]}


class HostPressureWaitTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(ios_host_pressure.os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        ios_host_pressure.os.environ.pop("TOMACCO_IOS_PRESSURE_WAIT_SECONDS", None)
        self.clock = 100.0
        self.sleeps = []

        def sleep(seconds):
            self.sleeps.append(seconds)
            self.clock += seconds

        for name, value in (("sleep", sleep), ("monotonic", lambda: self.clock)):
            stub = patch.object(ios_host_pressure.time, name, value)
            stub.start()
            self.addCleanup(stub.stop)

    def test_a_burst_of_pressure_is_waited_out(self):
        samples = iter([sample(False), sample(False), sample(True)])
        with patch.object(ios_host_pressure, "host_pressure_admission", lambda *_storage: next(samples)):
            self.assertTrue(ios_host_pressure.wait_for_host_pressure(300, 10)["admitted"])
        self.assertEqual(self.sleeps, [10, 10])

    def test_lasting_pressure_is_refused_when_the_bound_passes(self):
        with patch.object(ios_host_pressure, "host_pressure_admission", lambda *_storage: sample(False)):
            admission = ios_host_pressure.wait_for_host_pressure(25, 10)
        self.assertFalse(admission["admitted"])
        self.assertIn("io", ios_host_pressure.pressure_error(admission))
        # The last wait is cut to the bound; nothing sleeps past it.
        self.assertEqual(self.sleeps, [10, 10, 5])

    def test_the_environment_can_only_shorten_the_wait(self):
        with patch.object(ios_host_pressure, "host_pressure_admission", lambda *_storage: sample(False)):
            for value, sleeps in (("0", []), ("15", [10, 5]), ("900", [10, 10, 5]), ("nan", [10, 10, 5])):
                self.sleeps.clear()
                with patch.dict(ios_host_pressure.os.environ, {"TOMACCO_IOS_PRESSURE_WAIT_SECONDS": value}):
                    self.assertFalse(ios_host_pressure.wait_for_host_pressure(25, 10)["admitted"])
                self.assertEqual(self.sleeps, sleeps, value)

    def test_an_admitted_host_is_not_kept_waiting(self):
        with patch.object(ios_host_pressure, "host_pressure_admission", lambda *_storage: sample(True)):
            self.assertTrue(ios_host_pressure.wait_for_host_pressure()["admitted"])
        self.assertEqual(self.sleeps, [])



class PressureSourceTests(unittest.TestCase):
    def test_a_recorded_directory_counts_only_inside_the_test_root(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            busy = Path(root) / "pressure"
            busy.mkdir()
            for resource in ("io", "memory"):
                (busy / resource).write_text("some avg10=0 avg60=0 avg300=0 total=0\n"
                                             "full avg10=90.00 avg60=90.00 avg300=0 total=0\n")
            with patch.dict(ios_host_pressure.os.environ, {"TOMACCO_IOS_PRESSURE_DIRECTORY": str(busy),
                                                           "TOMACCO_TEST_ROOT": root}):
                self.assertEqual(ios_host_pressure.host_pressure_admission()["blocked_resources"], ["io", "memory"])
            with patch.dict(ios_host_pressure.os.environ, {"TOMACCO_IOS_PRESSURE_DIRECTORY": str(busy)}):
                ios_host_pressure.os.environ.pop("TOMACCO_TEST_ROOT", None)
                self.assertEqual(ios_host_pressure.pressure_directory(), Path("/proc/pressure"))


class StorageDeviceTests(unittest.TestCase):
    """I/O is judged by the devices holding the guest's storage when known."""

    def setUp(self):
        import tempfile
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        busy = self.root / "pressure"
        busy.mkdir()
        for resource in ("io", "memory"):  # the whole host's I/O figure is high
            (busy / resource).write_text("some avg10=0 avg60=0 avg300=0 total=0\n"
                                         f"full avg10={90 if resource == 'io' else 0}.00 "
                                         f"avg60={90 if resource == 'io' else 0}.00 avg300=0 total=0\n")
        self.ticks = {}
        for name in ("quiet", "saturated", "discarding", "mixed"):
            (self.root / "sys/dev/block" / name).mkdir(parents=True)
        environment = patch.dict(ios_host_pressure.os.environ, {
            "TOMACCO_TEST_ROOT": str(self.root), "TOMACCO_IOS_PRESSURE_DIRECTORY": str(busy),
            "TOMACCO_IOS_SYSFS_DIRECTORY": str(self.root / "sys")})
        environment.start()
        self.addCleanup(environment.stop)

    def stat(self, name, ticks, read_ticks=0, discard_ticks=0):
        fields = [0] * 17
        fields[3], fields[9], fields[14] = read_ticks, ticks, discard_ticks
        (self.root / "sys/dev/block" / name / "stat").write_text(" ".join(map(str, fields)))

    def admission(self, devices, paths):
        def sleep(_seconds):
            self.stat("quiet", 100)          # 0.1 s busy of 5 s
            self.stat("saturated", 4900)     # 4.9 s busy of 5 s
            self.stat("discarding", 4900, discard_ticks=4900)              # discards only
            self.stat("mixed", 4000, read_ticks=3000, discard_ticks=1000)  # 3/4 reads
            clock[0] += 5
        clock = [0.0]
        for name in ("quiet", "saturated", "discarding", "mixed"):
            self.stat(name, 0)
        located = lambda path: (self.root / "sys/dev/block" / devices[path]) if devices.get(path) else None
        with patch.object(ios_host_pressure, "storage_device", located), \
                patch.object(ios_host_pressure.time, "sleep", sleep), \
                patch.object(ios_host_pressure.time, "monotonic", lambda: clock[0]):
            return ios_host_pressure.host_pressure_admission(paths)

    def test_a_quiet_storage_disk_is_admitted_while_another_disk_saturates_the_host(self):
        admission = self.admission({"/fast/root.raw": "quiet", "/fast/sep.raw": "quiet"},
                                   ["/fast/root.raw", "/fast/sep.raw"])
        self.assertTrue(admission["admitted"])
        self.assertEqual(admission["resources"]["io"]["devices_busy_percent"], {"quiet": 2.0})

    def test_a_busy_storage_disk_is_refused(self):
        admission = self.admission({"/fast/root.raw": "quiet", "/full/copy": "saturated"},
                                   ["/fast/root.raw", "/full/copy"])
        self.assertEqual((admission["admitted"], admission["blocked_resources"]), (False, ["io"]))
        self.assertEqual(admission["resources"]["io"]["busy_percent"], 98.0)

    def test_time_spent_only_on_discards_does_not_count_as_busy(self):
        admission = self.admission({"/fast/root.raw": "discarding"}, ["/fast/root.raw"])
        self.assertTrue(admission["admitted"])
        self.assertEqual(admission["resources"]["io"]["devices_busy_percent"], {"discarding": 0.0})
        # Reads beside the discards still count, in proportion to their share.
        admission = self.admission({"/fast/root.raw": "mixed"}, ["/fast/root.raw"])
        self.assertEqual((admission["admitted"], admission["resources"]["io"]["busy_percent"]), (False, 60.0))

    def test_storage_on_an_unknown_device_falls_back_to_host_pressure(self):
        admission = self.admission({"/fast/root.raw": "quiet"}, ["/fast/root.raw", "/tmp/copy"])
        self.assertEqual(admission["blocked_resources"], ["io"])
        self.assertNotIn("measure", admission["resources"]["io"])
        self.assertFalse(ios_host_pressure.host_pressure_admission()["admitted"])  # no storage named: PSI

    def test_a_mount_names_the_device_its_file_system_comes_from(self):
        mounts = ("22 1 0:30 / / rw - btrfs /dev/mapper/root rw\n"
                  "23 22 259:2 / /mnt/linux\\040data rw - ext4 /dev/nvme0n1p2 rw\n"
                  "24 22 0:40 / /tmp rw - tmpfs tmpfs rw\n")
        with patch.object(ios_host_pressure.Path, "read_text", lambda _self: mounts):
            self.assertEqual(ios_host_pressure.mount_source("/mnt/linux data/guest/system.raw"), "/dev/nvme0n1p2")
            self.assertEqual(ios_host_pressure.mount_source("/home/user/system.raw"), "/dev/mapper/root")
            self.assertIsNone(ios_host_pressure.mount_source("/tmp/system.raw"))


if __name__ == "__main__":
    unittest.main()
