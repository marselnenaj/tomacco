# Apple Silicon KVM research

Work toward running the virtual iPhone on Linux on Apple Silicon (M-series) with KVM,
instead of software translation on x86_64. Discussion and status: the GitHub issue
"Virtual iPhone on Apple Silicon Linux (M-series): help wanted".

**Status: early research. The kernel patch here is an untested v0: it has never been built
or booted.** Use it on a machine where you can pick another boot entry if it fails.

| File | What it is | State |
| --- | --- | --- |
| `probe.c` | Runs single instructions at guest EL1 under KVM and reports what happens | Ran on an M1 MacBook Air, stock Asahi-based 7.1.x kernel |
| `apply-gxf-v0.py` | Kernel patch (as a script) that enables Apple GXF/SPRR for KVM guests, opt-in with `kvm-arm.apple_gxf=1` | Untested, never built |
| `install-test-kernel.sh` | Installs the patched kernel as an extra GRUB entry; your normal kernel stays the default | Untested |

## Probe results (M1, stock kernel, guest EL1)

Build and run on the host: `cc -O2 -o probe probe.c && ./probe` (needs access to `/dev/kvm`).

| In the guest | Result |
| --- | --- |
| `nop`, `ACTLR_EL1`, standard PAC (`pacia`/`autia`), `APIAKEYLO_EL1` | work |
| SPRR registers (`s3_6_c15_c1_0`, `s3_6_c15_c1_5`) | UNDEF (EC 0) inside the guest |
| GXF config (`s3_6_c15_c1_2`), `genter`, `gexit` | UNDEF inside the guest |
| Apple IPI registers (`s3_5_c15_c1_1`, `s3_5_c15_c0_0`) | UNDEF inside the guest |
| `HID0` (`s3_0_c15_c0_0`) | UNDEF inside the guest |

The guest takes these exceptions itself and KVM never sees them, so forwarding to
userspace is not enough: the features have to be enabled in hardware while the guest runs.

## What the v0 patch does

On cores that report GXF in `AIDR_EL1` bit 16, and only with `kvm-arm.apple_gxf=1`:
when a vcpu's registers load (`arch/arm64/kvm/hyp/vhe/sysreg-sr.c`) it saves the host's
`SPRR_CONFIG_EL1`, then enables SPRR and then GXF (GXF config is UNDEF while SPRR is off).
On put it disables GXF and restores the host's SPRR value.

Try it:

1. In a kernel tree matching your running kernel: `python3 apply-gxf-v0.py`, then build.
2. `sudo HOST_KERNEL=<your kernel package> KERNEL_SRC=<tree> ./install-test-kernel.sh`
3. Boot "Linux GXF test", run `./probe` again. Success looks like the SPRR/GXF rows no
   longer reporting UNDEF.

## Known gaps

- The guest's own GXF/SPRR state (permission registers, guarded-mode exception vectors and
  state, lateral exception levels) is not saved or restored per vcpu yet.
- Apple IPI and HID registers are not handled.
- The engine still runs its application processors on TCG; a KVM mode is not written.

## References and rules

Tomacco is clean-room. References: Asahi Linux's m1n1 (MIT), whose hypervisor runs macOS
with these extensions on the same hardware, and the Linux kernel sources. Please do not
bring in code from AGPL emulators.

Kernel code here is GPL-2.0-only like the kernel; the probe and scripts are
GPL-2.0-or-later like the rest of Tomacco.
