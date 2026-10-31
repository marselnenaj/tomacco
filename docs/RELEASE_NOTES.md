# Tomacco 1.0.0

Tomacco: Give your agent a phone. It builds, runs and tests Android and iOS apps
from Linux. A Qt desktop and a command-line tool, `tomaccoctl`, share one typed set of
operations, so a person and a coding agent can do the same things. `tomaccoctl` is also
a local MCP server.

This is the first release. [The guides](README.md) describe what ships.

## Highlights

- **Agents.** Over 150 schema-validated operations with JSON results, durable jobs
  that survive a disconnect, and shared debug sessions.
- **Android.** Gradle and Flutter builds, the real emulator, USB and wireless phones,
  live view, input, debugging, UI tests, Perfetto, signing and staged Play edits.
- **Swift on Android.** A native Swift core with an Android UI starter, for ARM64 and
  x86_64.
- **iOS without a Mac.** xtool projects built and signed on Linux, with install and
  launch on a paired iPhone. This route is contract-tested only; see the limits.
- **Virtual iOS device (experimental).** Boots your own iOS 27 image on x86_64 Linux in
  a patched QEMU with a Rust Vulkan renderer. A SwiftUI app built on Linux runs on it,
  shown so far with a few small apps. The engine builds reproducibly: two builds in
  different directories gave one digest.
- **Desktop.** Live device view, detachable device window, layout lab, English and
  German, and an optional Omarchy panel.

Measured on one host, an i9-13900K with a Radeon RX 6800-class 16 GiB card:

| Virtual iOS device | |
| --- | --- |
| Touch to visible change | about 0.1 s |
| App window appears after launch | about 6.4 to 7.7 s |
| Home-screen swipe | about 7.5 to 11 changed frames per second |

## Limits

- **A physical iPhone is not qualified.** No physical iPhone, or physical ARM64 Android
  phone, was run, and neither were live Play or App Store submissions.
- **One tested machine.** Arch Linux with Omarchy, x86_64, Qt 6.11, Rust 1.99. Other
  distributions should build but were not run.
- **Linux on Apple Silicon (M1, M2, Asahi): experimental for the tools and agent API; the virtual iOS device needs x86_64.**
- **The virtual iOS device is a research runtime**, not a qualified simulator. It is
  far below hardware speed. It has no audio or working debugger, and pointer
  authentication is bypassed. See [its limits](virtual-ios/README.md#honest-limits).
- **It needs your own material.** You supply the restore images and an Image4 boot
  ticket. Nothing from Apple ships with Tomacco, and no engine binary ships either: you
  build it. See [the legal notes](legal.md).

## Install

```sh
curl -fsSL https://tomacco.dev/install.sh | bash
tomaccoctl setup
```

On Omarchy this adds the bar widget. To install by hand or build from source, see
[getting started](getting-started.md). The binaries are conveyed under GPL-3.0-or-later and the source is
GPL-2.0-or-later; `SOURCE.md` says where the corresponding source is offered.

## Upgrade

This is the first public release. Every file an install replaces is backed up:
`install.py --list-backups`, then `--rollback ID`. See
[installing](building.md#installing).

## Credits

Tomacco stands on the work of others. The iOS runtime builds on qemu-sptm and darwin-vm,
orchard, reims-vgpu and metal2vulkan, Inferno, vphone-cli, linux-apfs-rw, vhost-device
and m1n1's register definitions. Building for iOS on Linux uses xtool, go-ios, Appium
WebDriverAgent, cctools-port, apple-libtapi, ldid and ipsw. Android's device view uses
scrcpy. [The README](../README.md#built-on-the-work-of-others) links them, and
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) has their licenses.
