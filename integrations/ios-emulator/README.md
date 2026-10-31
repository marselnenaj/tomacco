# Engine recipes and guest sources

This directory holds the patches, device models and guest programs for Tomacco's two
direct Linux engines: the paravirtual vphone600 machine behind the
[virtual iOS device](../../docs/virtual-ios/how-it-works.md), and the separate
[restore-shell research engine](../../docs/research-engines.md). The engine sources
are not here. `tools/build_ios_paravirt.py` and `tools/build_ios_emulator.py` fetch
pinned upstream revisions and apply these files, recording every source and output
digest.

These are unofficial integrations, without endorsement by the upstream projects or
Apple. Tomacco does not claim authorship of their CPU or Apple hardware emulation. No
Apple firmware, SDK, encryption key, guest image, engine or guest-utility binary is
redistributed here.

## The paravirtual machine

- The [Apple VirtIO USB device and host client](vphone-usb.md) transport USB between
  the guest's original driver and a private host socket. The host answers the restore
  protocol over USB mux.
- `restore-format-retry.c` and `.S` supply the hash-guarded, bounded format and reopen
  adapter that `ios.emulator.prepare_restore` uses. `restore-seal-pins.*` and
  `cryptex-seal-pins.S` make a research copy accept two explicit metadata objects by
  hash, which changes authentication policy on that copy. They contain no Apple binary
  code.
- The native guest channel builds and installs owned ARM64 tools in unsealed Preboot,
  and lets the Rust CLI and MCP clients call original LaunchServices and read logs over
  local virtio-vsock. `vphone-shared-memory.patch` retains QEMU's GPL-2.0 license.
  `vhost-vsock-scatter.patch` and `vsock-tx-packet.rs` extend the pinned rust-vmm backend
  under its Apache-2.0 OR BSD-3-Clause license.
- [vphone-hid.md](vphone-hid.md) describes the USB touchscreen and Home button.
- [reims-device-capabilities.md](reims-device-capabilities.md),
  [reims-display-panel.md](reims-display-panel.md) and
  [reims-texel-copy-regions.md](reims-texel-copy-regions.md) describe renderer contracts.

`tcg-profile.c` is a QEMU TCG plugin, loaded only by `run_ios_paravirt.py --tcg-profile`
(or the control plane's `tcg_profile` key). It counts every translated block's entries
per vCPU with an inline add, classifies its A64 instructions once at translation, and
writes the hottest blocks, pages, system and exception instructions and exception origins
as JSON at exit and on request. `tools/ios_tcg_profile.py` builds it against the engine
checkout's `qemu-plugin.h` and places kernel-half PCs in the boot's kernelcache by matching
block hashes. Reports hold addresses, counts and hashes, not guest code, unless its `code=on`
option is given.

## The research engine

The default build uses [jprx/qemu-sptm](https://github.com/jprx/qemu-sptm), commit
`bad6336fb24211f8c4e8e946c259bd5b12417340`. Its original QEMU GPL-2.0 text is in
`COPYING_GPL-2.0`; individual source files retain their notices. It boots a modified iOS
restore environment, not SpringBoard or graphical apps.

Full-system research images are packaged with `ios.emulator.prepare_research`, run through
`ios.emulator.research` and read through `ios.emulator.report`. The Rust worker owns the
disposable image, four UARTs, the bounded platform parser, shutdown and cleanup, and the desktop
dialog uses the same operations. See [managed runs](../../docs/research-engines.md#managed-full-system-runs).

`sptm-boot-display.patch` retains QEMU's GPL-2.0 license. It adds the optional
`darwin,boot-display=WIDTHxHEIGHT` machine property, reserves guest video RAM outside XNU's
allocator, fills the boot video ABI and exposes that RAM as a QEMU display surface. It also fixes
PPM capture padding for widths not divisible by four. The output is XNU's console, not an emulated
Apple GPU, display controller or application compositor.

The same patch adds `darwin,boot-media=on`. A private device-tree copy advertises the guest RAM
image through XNU's existing `boot-ramdmg-*` descriptors, and Apple's DiskImages, IOMedia and APFS
drivers then expose real devices for inspection. This is a memory-backed image, not persistent NAND
emulation. The feature is off by default and does not bypass root authentication. `storage-probe.c`,
its small linker interface and entitlement file provide a read-only, SDK-free native diagnostic built
with `tools/build_ios_storage_probe.py`.

`darwin,boot-storage=/absolute/working-copy.apfs` maps an explicit owner-only copy into that reserved
extent with shared file-backed RAM. It requires `boot-media=on`; source aliases, symlinks, hardlinks,
incompatible sizes and concurrent use are refused. An exit notifier checks `msync` and `fsync`. The CLI
and MCP expose it as `working_image` and wait for a confirmed flush. Crash recovery, encryption and a
complete iOS boot are outside its scope. It is not a NAND-controller model.

### Controllers and memory

`dart-t8110.h` implements the T8110 DART address-translation core (MIT), with `dart-t8110.c` supplying
its QEMU MMIO, IOMMU and interrupt adapter (GPL-2.0-or-later). The register and page-table ABI follows the
[Asahi m1n1 definitions](https://github.com/AsahiLinux/m1n1/blob/main/proxyclient/m1n1/hw/dart8110.py) and
the [Linux DART driver](https://github.com/torvalds/linux/blob/master/drivers/iommu/apple-dart.c). The build
stages these files beside the pinned engine sources and records both hashes. `darwin,research-dart=on` maps the
model using the checked device-tree register and interrupt descriptors, and is off by default. The model has 256
DMA streams, 16 KiB page walks, per-page and subpage permissions, faults, interrupts and register protection. It
does not implement DCP, Exclave firmware or a display endpoint. SID remapping, DAPF, diagnostic TLB operations and
migration are unsupported.

`ios27-dcp-dart.json` pins an optional `--hardware-profile dcp-dart` input recipe. It restores six original display
and DART driver bindings and supplies the `dart-id` bootloader property on a copied device tree. It changes no
additional kernel instructions, requires the matching single-Data research layout and keeps all capability flags
false. Managed jobs detect the profile and refuse an engine without DART support before starting a guest.

The firmware-free `tomacco-dart-test` board exposes QEMU RAM and a DMA window for `tests/ios_dart_integration.py`,
which verifies translated reads and writes and blocked writes with IRQ delivery.

`asc-mailbox.c` and `.h` add the separate [ASC transport and ARM interrupt test machine](asc-mailbox.md), under
GPL-2.0-or-later. It transfers queued messages, exposes raw interrupt levels and refuses unsupported transactions with
bus errors. It is not mapped into Darwin or PMP and implements no controller firmware.

`exclave-bundle.h` (MIT) validates the supplied BUND v4 range table, secure-kernel Mach-O metadata and executable
entry. The same engine patch adds opt-in `research-cl4`, `exclave-integrity` and `exclave-trustcache` properties, CL4
memory banks and bootloader metadata. It also separates the CPU's host and guest SPRR user-permission banks; a
firmware-free ARM64 regression exercises EL0 and GL0 access and faults in both translation regimes. No secure service or
successful guest response is synthesized. The read-only Exclave timebase register uses the CPU's existing virtual counter.

The opt-in `tomacco_arm_bus_fault` trace reports the original instruction PC, physical and virtual addresses, width, access
kind and memory response immediately before a real ARM bus fault. It does not suppress exceptions or supply missing register
values. `tests/ios_bus_fault_integration.py` compares CPU read and write aborts with the trace on and off, without Apple
firmware.

`tools/prepare_ios_exclave.py` checks the three local components against the matching BuildManifest and publishes a
read-only bundle. The pinned `ios27-exclave-dart.json` recipe extends the DCP profile with original secure mailbox and AOP
and DCP mapper bindings. Managed preparation requires the matching `exclave` argument and rechecks all nine components before
boot.

`mcc-t8140.c` and `.h` (GPL-2.0-or-later) add the T8140 DRAM-configuration and cache-way register subset; see
[its register model](mcc-t8140.md). The [PTD transport](ptd-t8140.md) and [isolated PMP machines](pmp-research.md) follow. The
pinned `ios27-exclave-mcc.json` recipe restores only original PMGR and MCC driver bindings on top of the Exclave profile. iOS does
not finish booting on this profile.

### Preparation

Preparation uses [darwin-vm](https://github.com/jprx/darwin-vm), commit `e969c3adffd89153a642d0626f98c9e03f92db05`; its MIT notice
is in `LICENSE.darwin-vm`. The device-tree script, shell launch configuration and prebuilt guest utilities are fetched with that
source, and guest utilities retain their own licenses. The Linux APFS approach also references
[the Linux contribution](https://github.com/jprx/darwin-vm/pull/1); Tomacco runs the APFS module only inside a disposable Linux helper
VM. The separate kernel, linux-apfs-rw, ldid, ipsw, BusyBox and LZFSE dependencies retain their upstream licenses and are downloaded
and built locally.

`apfs-buffer-allocation.patch` retains linux-apfs-rw's GPL-2.0 license. It changes the modern-kernel buffer lookup to Linux's
`__getblk` wrapper, which supplies the block mapping's allocation flags and permits memory reclaim. Passing only `__GFP_MOVABLE`
directly to `bdev_getblk` produces intermittent read failures while copying large compressed system files. The preparation recipe
accepts only this exact patch over its pinned source. The
[Linux buffer API](https://www.kernel.org/doc/html/v7.2/filesystems/buffer.html) documents the allocation-flag contract.

`ios27-ramdisk-research.json` records four byte-count instruction changes in a specific copied kernel. They allow a full-system APFS
root mount and a launchd start attempt.

### Bootstrap diagnostics

`research-init.c` and `research-apfs.c` implement a bounded bootstrap. Its optional APFS mode discovers the real boot container,
validates the Data volume's role and BSD device, and stops on setup failure. Before launching Apple's launchd it verifies a binary
write, flush and exact readback; APFS mode also unmounts, remounts and compares the data again. The matching build recipe defaults to
temporary tmpfs data.

`research-observer.c` is enabled only by `--process-diagnostics`. It captures the kernel, launchd and four selected Apple daemons
twice, with a two-MiB lifetime export limit and the single `com.apple.private.stackshot` entitlement. It runs in a separate short-lived
process so crash-log output cannot postpone sampling, and each round also samples the nonblocking Exclave boot-stage sysctl. The guest
exports checksummed KCData through a separate raw `qemuport0` UART, which QEMU's second `-serial` output must capture apart from the boot
console. `tools/extract_ios_stackshots.py` checks complete records, framing, checksums and observation counts before creating owner-only
outputs.

`research-graphics.c` is enabled only by `--graphics-diagnostics`. A separate, time-limited guest process calls real CoreGraphics and
CALayer CPU rendering, then losslessly compresses the BGRA buffers with the guest's zlib. The third serial port (`qemuport1`) carries
bounded, checksummed frame records. `tools/extract_ios_graphics.py` validates the whole transfer and decompression limits before exporting
raw pixels, PNGs and hashes. It requires the full-system research image's frameworks; no SDK or framework implementation is bundled. The
builder signs a separate `programs/graphics-probe`, records its hash and adds its CDHash to `bootstrap.tc`; copy it to
`/tomacco/graphics-probe` in the owned research image. The bootstrap executes it freshly instead of loading Objective-C inside a fork of its
initialized IOKit runtime. These are offscreen test renders, not an iPhone display or UI.

With `--graphics-diagnostics --graphics-profile uikit`, `research-uikit.c` adds real guest UIView, UILabel, UIButton and UISwitch rendering
and a public UIAction callback dispatched by UIKit. It exports before and after frames and verifies the callback count and switch state. Clang
compiles its native block with `-fblocks`, and the guest's block runtime supplies `_NSConcreteGlobalBlock`. The host extractor validates the
four-frame profile and state transition before accepting completion. This does not provide UIApplication's lifecycle, scene and window services,
physical touch dispatch or DCP or GPU output.

`ios27-apfs-ram-research.json` pins the separate kernel and device-tree changes for disposable Data-volume research.
`tools/prepare_ios_research_inputs.py` applies them only to matching copies, keeps the originals and checks the result. This enables the RAM
descriptor write path and Apple's existing unencrypted `DoNotUseSEP` APFS test mode. It supplies neither SEP or data protection, persistent
storage nor a graphical boot, and is not enabled in normal guest jobs.

### The Inferno backend

The optional Inferno backend pins ChefKiss Inferno at `cc4302a99167abec69b714cfd00c38caece7e7de`. `bringup.patch` applies only to this backend.
`LICENSE`, `COPYING`, `COPYING_GPL-3.0` and `COPYING_AGPL-3.0` are its original notices; the patch retains the respective source licenses.
Protected upstream artwork and branding are removed from that derivative build, while original copyright attribution remains. It fails to boot
iOS 27.
