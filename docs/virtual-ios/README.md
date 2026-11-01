# Virtual iOS device

An experimental iOS 27 phone that runs on an x86_64 Linux PC, with no Mac. It boots to
the Home screen, runs Settings, Safari and arm64 apps you built on Linux, and takes live
touch in a desktop window or from an agent.

**Status: experimental.** It is a research runtime, not a qualified simulator. Do not put real accounts or personal
data into it.

Setup includes preparing a local IM4M research manifest from a public template,
your firmware manifests and your VM's device tree. The
[IM4M instructions](setup.md#how-do-i-get-the-im4m) give the download and helper command.

## What it is

Three parts work together:

- **The machine.** A patched QEMU emulator translates every ARM instruction of the guest
  in software. It has six application processors plus a SEP (Apple's separate security
  processor), and 8 GiB of guest RAM. It uses 16 KiB translated pages with BTI, a CPU
  protection feature, turned off, because that is what made it fast enough to use.
- **The renderer.** A Rust program implements Apple's paravirtual GPU on Vulkan, and Metal
  shaders are translated for it.
- **The guest tools.** Tomacco's own small programs run inside the guest. They let the host
  install and open apps, read logs and save the disk.

For contributors, [how it works](how-it-works.md) goes deeper.

## What it needs

| You need | Details |
| --- | --- |
| Your own restore images | Two exact Apple images: iPhone17,3 `24A446` and PCC `23E5207q`. **Nothing from Apple ships with Tomacco**. [Find and prepare the images](setup.md#obtain-the-apple-images). |
| A local Image4 boot manifest | Prepare the IM4M on Linux with `tools/prepare_ios_research_manifest.py` and the public template. No Mac or Apple signing request is needed for this step. See [the instructions](setup.md#how-do-i-get-the-im4m). |
| To build the engine | Tomacco ships no engine. Setup builds it on your machine from pinned sources, with the compilers [setup](setup.md) names. |
| A graphics card | Vulkan, with about 16 GiB of memory. Measured on a Radeon RX 6800-class card. |
| A fast CPU | Every guest processor is emulated. Measured on an i9-13900K. |
| Memory | 8 GiB for the guest, and up to 2 GiB for translated code. |
| Disk | About 100 GiB for setup. Each session's disk copy is a 32 GiB disk that takes the space of its data, and at most two are kept. |
| Time | Half an hour for the restore itself. After that, about 9 minutes from a start to your app. |

Other machines and graphics drivers have not been qualified. Read the
[legal notes](../legal.md) first.

## Measured

On an i9-13900K with a Radeon RX 6800-class 16 GiB card:

| What | Number |
| --- | --- |
| App window appears after launch | about 6.4 to 7.7 s |
| Touch to visible change | about 0.1 s |
| Home-screen swipe, first page | about 7.5 to 11 changed frames per second |
| Graphics memory over 36 app launches | at most 1.7 GiB |

This is far below hardware speed. These are baselines on one host, not a comparison with
a real phone. The idle first Home-screen page already changes about 10.5 times a second, so
the swipe figure shows that frames keep coming during a swipe, not a rate above idle.

## Honest limits

**Partly supported:**

- **Network.** A boot with the `guest_network` setting `nat` attaches a virtio network
  device behind [passt](https://passt.top) (install it with `pacman -S passt`): outbound
  only, no inbound port, and the host's own loopback kept away. The guest brought up `en0`
  with DHCP, and a SwiftUI app's `NWPathMonitor` read the path as satisfied, against a test
  stand-in for passt with DHCP and DNS only. Real traffic through passt has
  not been seen: the development host has no passt.
  Default off. The guest's protections are off, so this is real internet exposure of a
  guest you should not trust; see the [security note](../legal.md).
- **Hardware keyboard.** `text` types on the USB keyboard, with the layout of the guest's
  language. A German guest read `Hello`, mixed symbols and the field held them.
  `key` sends QEMU key names.
- **App install without a reboot.** `ios.virtual.deploy` puts a built `.app` on the
  running phone in about 25 seconds, no reboot. A boot reserves 512 spare trust-cache
  entries and the host exchanges them for the app's hashes while the guest is briefly
  stopped; the adapter reads a per-boot table for the new app. Shown with a SwiftUI app
  and an app carrying a widget extension.

**Not supported:**

- **Debugging.** The host-to-guest bridge and `ios.virtual.debug` are in place: lldb
  reaches a loopback port and the guest spawns `debugserver`, but iOS refuses it while
  Developer Mode is off, and arming that mode did not turn it on at the next boot. No
  breakpoint yet.
- **Audio.** No userland drives `AppleVirtIOSound`; adding `virtio-sound-pci` has
  not been tried.

**SwiftUI works, with the SDK from your own Xcode download.** An app made with
`xtool new`, built on Linux with `xtool dev build` and the Xcode 27.2 SDK, launched and
drew its text and SF Symbol on the device. A few small apps so far, one with a keychain
round-trip and one carrying a widget extension; see
[deploying your own app](usage.md#deploy-your-own-app).

**Known defects and gaps:**

- **Pointer authentication is bypassed**, not emulated. The guest is not a security
  boundary, so do not run apps in it that you do not trust.
- **XR multisampling is open**, so some rendering is incomplete. No Wi-Fi, cellular,
  flashlight or camera glyphs are drawn: `wifid` wants a device tree with `wlan`, and
  overriding the status bar has not been tried.
- **Widgets can stay empty.** Widget extensions miss chronod's deadline under emulation
  and are relaunched several times an hour. chronod is the iOS service that runs them.
- **Saved data is partly proven.** A protected (Class C) file survives a clean reboot. A
  SwiftUI app's `SecItemAdd` and `SecItemCopyMatching` round-tripped a keychain item,
  and a `.noFileProtection` (Class D) write read back as `NSFileProtectionNone`;
  neither was checked across a reboot.
- **A hung app is not ended for you**, because the watchdog that would do it also ended
  apps for being slow.
- **Models are descriptions, not hardware.** A model sets the screen and the interface
  style. Cameras, sensors and radios do not exist, and the Duo has one fixed panel with no
  folding.
- **The whole setup chain has never run on a fresh machine as one sequence.** The
  [setup guide](setup.md#what-has-been-checked) says what has been checked.

## Next

1. [Set it up](setup.md) from your restore images to the first start.
2. [Use it](usage.md): start, stop, apps, models, touch and gestures.
   [Your first SwiftUI app](../tutorial-virtual-iphone.md) goes from a starter project to a build, run and watch loop.
3. [Fix it](troubleshooting.md) when something goes wrong.
