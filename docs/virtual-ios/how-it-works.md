# How the virtual iOS device works

For contributors who want to change it. To use it, read [usage](usage.md).

Nothing from Apple is in this repository. Every Apple part comes from the user's own restore images, on
private copies. [Legal notes](../legal.md) lists each protection this changes and why.

## The whole picture

```text
restore images -> prepared disks and kernel -> one-step start
                                                  |
engine (patched QEMU, software translation on x86_64) <---+
  |-- tomacco-vphone machine: six application processors and a SEP
  |-- reims-vgpu: Apple's paravirtual GPU in Rust on Vulkan, with Metal
  |   shaders going through the metal2vulkan translator
  `-- guest tools: control service, setup helpers and diagnostics,
      reached over virtio-vsock
```

The engine's sources are not in this repository. `tools/build_ios_paravirt.py` fetches pinned upstream revisions
and applies the ordered patch series in `integrations/ios-emulator`.

| Upstream | What it gives | License of its patches |
| --- | --- | --- |
| [jprx/qemu-sptm](https://github.com/jprx/qemu-sptm) | QEMU that runs Apple's SPTM and TXM | GPL-2.0 |
| [yaelliethy/orchard](https://github.com/yaelliethy/orchard), [steelbrain/reims-vgpu](https://github.com/steelbrain/reims-vgpu) | The vphone machine's graphics device and renderer | LGPL-3.0 |
| [steelbrain/metal2vulkan](https://github.com/steelbrain/metal2vulkan) | The Metal shader translator | LGPL-3.0-or-later |
| [rust-vmm/vhost-device](https://github.com/rust-vmm/vhost-device) | The virtio-vsock backend for the guest channel | Apache-2.0 OR BSD-3-Clause |

## The machine

The guest is Apple's **vphone600**, the virtual device of Private Cloud Compute (PCC), running the iOS 27 userland of a
real iPhone image on top of the PCC kernel. `integrations/ios-emulator/vphone-research.c` is the board.

**Processors.** Six application processors (APs) plus a SEP. QEMU's TCG translates ARM to x86_64 in software, because
KVM cannot run an ARM guest on Intel. Each AP has its own host thread (MTTCG). With `host_cpu_policy: "performance"`, AP
threads go to separate physical cores, fastest first. Sixteen APs start but are not supported or measured, and the
source tree's maximum of 32 does not work.

**Memory.** 8 GiB of guest RAM. The translation cache takes up to 2 GiB of host memory
(`translation_cache_mib: 2048`, the most this host architecture allows).

**Boot.** The kernel starts directly, without Apple's boot chain. SPTM (Apple's page-table monitor) enters at the guarded level,
TXM (its trusted execution monitor) runs at EL0 and XNU (the kernel) at EL1. Unknown Apple hypercalls return the real
`NOT_SUPPORTED`, and unknown system registers still fault, rather than being faked.

**Two engine options carry most of the speed.** Both are boot options, inert until asked for, and a generated profile asks for
both:

- `target_page: "16k"`: 16 KiB translated pages.
- `bti: "off"`: the emulated CPU reports no FEAT_BTI, the ARM branch-target check.

With both on the development host, a Home-screen swipe went from 2-6 to 10-15 changed frames per second, with the
processors at 30-40 % instead of 60-73 %.

**Pointer authentication is bypassed.** The engine does not enforce ARM pointer authentication, and
`qemu-arm-pauth-bypass-inline.patch` keeps that and makes it faster. Invalid pointers are not caught, so the guest is not a
security boundary.

**The SEP** is Apple's security processor. Unmodified firmware runs on its own CPU with 64 MiB of private RAM. `vphone-sep.c`
is its mailbox, `vphone-asmf.c` its shared measurement registers, and `vphone-vstr.c` its persistent storage. There are no
synthetic replies, so SEP services work only as far as the firmware's own code works.

**Storage.** Apple VirtIO devices for the root and auxiliary disks. Writable disks must be asked for by name, and the launcher
locks the real files. A flush is a real flush with error reporting. The root disk is raw, or with `start_disks: "overlay"` a qcow2 overlay on the previous
run's root disk (`tools/ios_disk_overlay.py`); the launcher then attaches it with the `qcow2` block driver and lets the file grow to
the disk's virtual size. A live phone booted, saved and stopped this way twice, the second time on a two-layer chain.

**Input.** A USB touchscreen and keyboard (`vphone-usb` and `vphone-hid`), with Home and the side button as GPIO or HID. iOS
ignores the virtio keyboard, which is why `key` has no effect. Touch has one contact.

**Time.** The guest clock follows wall-clock time. An older instruction-counting clock showed that a faster clock does not make
iOS faster; it gives iOS less work per second of its own time, and its watchdogs then kill what draws the interface.

**Models.** The device tree carries one gestalt variant. `device_model` copies its tags into the product properties, and the
other models are derived from it with their own panel, artwork subtype and corner radius.

## The renderer

iOS's own Metal driver, from the PCC image, talks to a paravirtual GPU. **reims-vgpu** implements that device in Rust on Vulkan,
and C code adapts it to QEMU.

Metal shaders are compiled to Vulkan through **metal2vulkan**. The compiler plugin (`ios.emulator.build_gpu_plugin`) is
cross-built on Linux, from `vphone-cli`, and loads inside the guest.

What to know before changing it:

- **XR formats.** iOS's extended-range formats render through a linear RGBA16F attachment and encode their guest texels on
  store. XR multisampling is open.
- **Declined work is logged.** The renderer records every operation it declines in its report's `renderer/fail.log`, always on,
  cut back at 64 MiB. Start there for a display defect.
- **Memory has no budget.** The renderer holds what iOS has drawn. Images of deleted resources are given back, surfaces when their
  generation ends, and depth and hash-keyed targets only by age above half the card; sole copies stay. A budget that retired images while a draw was being prepared ended the engine, so
  the session [stops the careful way](usage.md#the-graphics-memory-guard) instead (`tools/ios_host_pressure.py`,
  `tools/run_ios_paravirt.py`).
- **Frames.** `ios.emulator.control` `frame` with `transport:"file"` writes an uncompressed PPM into a private, memory-backed directory, and reports whether the picture changed. A viewer can skip the reload then.

Device contracts: [reims-device-capabilities.md](../../integrations/ios-emulator/reims-device-capabilities.md),
[reims-display-panel.md](../../integrations/ios-emulator/reims-display-panel.md),
[reims-texel-copy-regions.md](../../integrations/ios-emulator/reims-texel-copy-regions.md).

## The guest tools

Tomacco's own programs, in C, built on Linux with clang, an ARM64e-capable linker and `ldid` (`ios.emulator.build_guest_tools`). No Apple
SDK is used. The sources are `integrations/ios-emulator/guest-*`.

**Getting them in.** `ios.emulator.prepare_guest_tools` installs the payload into **unsealed Preboot** through a networkless Linux APFS
helper, on an independent copy of the disks. A trust cache names their hashes, with local development signatures, not Apple's. Original
launchd then finds the jobs through its `SupplementalPaths` contract.

**The channel.** The host reaches the guest's control service over virtio-vsock, and `ios.emulator.guest` is the API. Requests have
receipts. A reply that outlives its wait is followed through `ios.emulator.guest_result`, and an install is never sent twice.

**Development identity.** `development_device: true` prepares a virtual, non-production identity, so iOS's own MobileActivation takes its
existing local path. Neither that daemon nor its answer is patched.

**App signing.** Two things decide before boot whether an executable runs: its code-directory hash must be in the trust cache the guest
booted with, and installd admits an ad-hoc signed app only through the development-signing adapter's policy.

- The build copies a bundle into the payload, signs it ad hoc, adds its hashes to the trust cache and compiles its identity into a policy
  table inside the adapter (`guest-development-protocol.h`).
- Inside installd, the adapter enables ad-hoc signing only when the original validator returns a table entry's hash and identifier. An app
  outside the table keeps the original policy.
- A new adapter needs a kernel that pins its hash, because the kernel maps files from `/private/var` as executable only for hashes on its
  own allow-list. `tools/prepare_ios_adapter_pin.py` swaps that one 20-byte slot, and a start does this by itself.
- `tools/ios_application_bundle.py` holds the bundle checks, the same in `configure`, the build and the start.

**Quiet services.** `guest-quiet.c` unloads services that only burn time under emulation, and guards locationd.

## A start and a stop

`tools/ios_virtual_iphone.py` runs them, and `tools/run_ios_paravirt.py` supervises the engine. `native/ios_emulator/virtual_phone.rs` is the
operations. They only call public operations.

1. **Checks.** Inspect each app, and refuse a kernel or payload that does not fit the tools, before any disk copy.
2. **`preparing`.** Make a private copy from the last verified run (or an overlay on it), and reinstall the guest tools.
3. **`booting`.** Run the engine as an independent job, and wait for the guest service.
4. **Watchdog and time zone**, then Auto-Lock.
5. **`installing`.** Install each app whose hash the base does not record.
6. **`unlocking`.** Home presses, or the swipe on a phone with no Home button.
7. **`launch-accepted`.** Then look at the panel for up to a minute, for `display_visible`.

Every attempt is recorded before its disk copy exists, so a failed start cannot leave a copy that blocks the next one.
`tools/ios_start_timing.py` puts each start's and stop's steps together from the start's own marks, the preparation's
`step_times` and the launcher's `timing`, for `start_timing` and `stop_timing` in the status.

A stop asks the guest to flush and write `stop.json`, lets the emulator exit, and then checks the flush, exit code 0 and complete disk
hashes. Only that makes a run a base. `tools/ios_run_verdict.py` puts that evidence and the launcher's outcome into
words, which the state keeps as `verdict` and `ios.virtual.status` shows as `last_run`. Near the session limit, or when the [guard](usage.md#the-graphics-memory-guard) trips, the supervisor
asks the control plane for this same stop.

## Where to read next

| To change | Read |
| --- | --- |
| The USB device and host client | [vphone-usb.md](../../integrations/ios-emulator/vphone-usb.md) |
| Touch and keyboard | [vphone-hid.md](../../integrations/ios-emulator/vphone-hid.md) |
| Engine builds, patches and their licenses | [integrations/ios-emulator/README.md](../../integrations/ios-emulator/README.md) |
| PMP firmware and controller research | [pmp-research.md](../../integrations/ios-emulator/pmp-research.md), [asc-mailbox.md](../../integrations/ios-emulator/asc-mailbox.md), [asc-controller.md](../../integrations/ios-emulator/asc-controller.md) |
| The T8140 MCC and PTD models | [mcc-t8140.md](../../integrations/ios-emulator/mcc-t8140.md), [ptd-t8140.md](../../integrations/ios-emulator/ptd-t8140.md) |
| Reproducible engine builds | [Building](../building.md#reproducible-engine-builds) |
| The earlier restore-shell engine | [Research engines](../research-engines.md) |

Tests that need neither an image nor a GPU cover the patches and guest programs, in `tests/python/`. Live checks are
`tests/ios_*_integration.py`.
