"""--guest-memory-import: how guest RAM is laid out for the renderer's GPU import."""
import fcntl
import os
from pathlib import Path
import tempfile
import unittest

import run_ios_paravirt as launcher
from ios_guest_control import GUEST_MEMORY_ALIGN, GuestControl, sealed_guest_memory


class GuestMemoryPlanTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.device = os.path.join(directory.name, "udmabuf")
        with open(self.device, "w"):
            pass
        os.chmod(self.device, 0o600)

    def test_shared_keeps_qemus_shared_ram(self):
        fd, receipt = launcher.guest_memory_plan("shared", True, 1 << 20, udmabuf=self.device)
        self.assertIsNone(fd)
        self.assertEqual(receipt, {"requested": "shared", "backing": "shared-ram"})

    def test_dma_buf_lays_a_sealed_memfd(self):
        made = []
        fd, receipt = launcher.guest_memory_plan(
            "dma-buf", True, 1 << 20, udmabuf=self.device, create=lambda size: made.append(size) or 42)
        self.assertEqual((fd, made), (42, [1 << 20]))
        self.assertEqual(receipt, {"requested": "dma-buf", "backing": "sealed-memfd"})

    def test_private_ram_needs_no_dma_buf(self):
        fd, receipt = launcher.guest_memory_plan(
            "dma-buf", False, 1 << 20, udmabuf=self.device, create=self.fail)
        self.assertIsNone(fd)
        self.assertEqual(receipt["backing"], "private-ram")
        self.assertIn("host-pointer import", receipt["fallback"])

    def test_an_unopenable_udmabuf_keeps_shared_ram_and_says_why(self):
        os.chmod(self.device, 0o000)
        if os.access(self.device, os.R_OK | os.W_OK):
            self.skipTest("permission bits do not bind this user")
        fd, receipt = launcher.guest_memory_plan(
            "dma-buf", True, 1 << 20, udmabuf=self.device, create=self.fail)
        self.assertIsNone(fd)
        self.assertEqual(receipt["backing"], "shared-ram")
        self.assertIn("not readable and writable", receipt["fallback"])
        fd, receipt = launcher.guest_memory_plan(
            "dma-buf", True, 1 << 20, udmabuf=self.device + ".missing", create=self.fail)
        self.assertEqual(receipt["backing"], "shared-ram")

    def test_a_memfd_failure_keeps_shared_ram_and_says_why(self):
        def refuse(size):
            raise OSError(38, "Function not implemented")
        fd, receipt = launcher.guest_memory_plan("dma-buf", True, 1 << 20, udmabuf=self.device, create=refuse)
        self.assertIsNone(fd)
        self.assertEqual(receipt["backing"], "shared-ram")
        self.assertIn("no sealed memfd", receipt["fallback"])

    def test_the_variable_is_the_renderers(self):
        self.assertEqual(launcher.GUEST_DMA_BUF_VARIABLE, "REIMS_VGPU_GUEST_IMPORT_DMA_BUF")


class SealedGuestMemoryTests(unittest.TestCase):
    def test_the_memfd_is_sealed_against_resizing_and_inheritable(self):
        fd = sealed_guest_memory(4 << 20)
        self.addCleanup(os.close, fd)
        self.assertEqual(os.fstat(fd).st_size, 4 << 20)
        seals = fcntl.fcntl(fd, fcntl.F_GET_SEALS)
        for seal in (fcntl.F_SEAL_SHRINK, fcntl.F_SEAL_GROW, fcntl.F_SEAL_SEAL):
            self.assertTrue(seals & seal)
        # udmabuf refuses a write seal, and guest RAM must stay writable.
        self.assertFalse(seals & fcntl.F_SEAL_WRITE)
        with self.assertRaises(OSError):
            os.ftruncate(fd, 2 << 20)


class QemuArgumentsTests(unittest.TestCase):
    def control(self):
        control = GuestControl.__new__(GuestControl)
        control.directory = Path("/run/x")
        return control

    def test_without_a_memfd_guest_ram_is_qemus_shared_ram(self):
        arguments = self.control().qemu_arguments(8 << 30)
        self.assertEqual(arguments[:2], ["-object",
                                         f"memory-backend-ram,id=tomacco-guest-memory,size={8 << 30},share=on"])

    def test_a_memfd_is_mapped_through_the_file_backend_at_qemus_alignment(self):
        arguments = self.control().qemu_arguments(8 << 30, 17)
        self.assertEqual(arguments[1], f"memory-backend-file,id=tomacco-guest-memory,size={8 << 30},"
                                       f"mem-path=/proc/self/fd/17,share=on,align={2 << 20}")
        self.assertEqual(GUEST_MEMORY_ALIGN, 2 << 20)
        self.assertIn("vhost-user-vsock-pci,chardev=tomacco-guest-control,disable-legacy=on", arguments)


if __name__ == "__main__":
    unittest.main()
