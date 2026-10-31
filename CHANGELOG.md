# Changelog

Notable changes per release. The [release notes](docs/RELEASE_NOTES.md) summarise a
release. Versions follow [semantic versioning](https://semver.org) for the `tomaccoctl`
operations and their JSON results.

## Unreleased

`tomaccoctl` now has one typed API of 177 operations.

### Added

- **Command line.**
  - `tomaccoctl completions bash|zsh|fish`. Only the bash script has been run.
  - `tomaccoctl schema OPERATION` and `schema --names`.
  - `call … --wait` follows a job to its end, with its log on stderr.
  - Indented JSON in a terminal or with `--pretty`; output to a pipe is unchanged.
  - "Did you mean" remedies for misspelt operations, argument keys and flags.
- **Update and uninstall.** `tomaccoctl update [--check]` and `tomaccoctl uninstall`.
  The installer now keeps a copy of itself for uninstall and rollback.
- **MCP prompts.** `setup`, `run-android-app` and `run-virtual-iphone`.
- **MCP guides.** The agent guide, recipes and six other guides are MCP resources,
  `tomacco://docs/NAME`, as Markdown of the installed version.
- **Android.**
  - `android.input` types any Unicode text through the device clipboard.
  - `android.settings` sets dark mode, font scale, animations, airplane mode and one
    app's language, then reads them back.
  - `device.logs` follows one app by its user ID on Android 12 and newer, across
    restarts.
  - `android.emulator` gains `snapshot-list`, `snapshot-delete`, `battery` and
    `network`.
- **Testing.**
  - `testing.visual_diff` compares a screenshot with a baseline PNG: changed fraction,
    threshold, tolerance, ignored regions and a diff image.
  - `testing.accessibility` audits an Android screen: unlabelled clickable elements,
    touch targets under 48 dp and tiny text, as structured findings.
  - `testing.crashes` reports Android crashes, ANRs and native crashes from logcat as
    structured events, for an app, a time or a job's run. `testing.run` lists them per
    device and fails on a crash of an app the scenario uses.
  - `testing.results` summarises JUnit XML from Gradle unit and instrumented tests or
    `testing.run`: totals, suites and failures with their stacks, leaving out results
    older than the test job.
- **Virtual iPhone.**
  - `ios.virtual.screenshot` writes a PNG, with optional rounded corners or a bezel.
  - `ios.virtual.record` records the screen to MP4.
  - `ios.virtual.status` reports phase times, copy progress and whether the disk has
    room for the next start.
  - None of these four has been run on a live phone yet.
- **Desktop.**
  - Drop an `.apk`, `.ipa` or `.app` on the device view to install it, or drop a
    project folder on the window to open it.
  - Screenshots are also copied to the clipboard.
  - Recent projects on the start screen and in the command palette.
  - The window reopens at its last size.
  - The Omarchy panel can open a job's log (V key, `log` command).
- **Packaging and guides.**
  - An Arch `PKGBUILD` in `packaging/arch`.
  - Debian, Ubuntu and Fedora package lists; not tested on those systems.
  - An Android example in `examples/`.
  - [Recipes](docs/recipes.md) and an [FAQ](docs/faq.md).

### Changed

- `ios.virtual.start` refuses up front, before deleting or copying anything, when the
  disk lacks room for the copy.
- The panel `ios.virtual.status` reports comes from the running guest's boot, not from
  the current profile.
- Unknown argument keys are reported before missing required ones.

### Fixed

- A command the emulator console refused (`KO`) no longer looks like success:
  `emulator_refused`.
- Every network latency preset sends its millisecond range; the emulator's own `gprs`
  name applied the UMTS range.
- The documentation said Qt 6.9 for running the release on other distributions. The
  release desktop needs Qt 6.12, FFmpeg 9 and glibc 2.39; Qt 6.9 is enough to build
  from source.

## 1.0.0

First release of the Rust control plane and the redesigned Qt desktop.

### Added

- One-line install from tomacco.dev that checks the signature, and `tomaccoctl setup`,
  which checks the machine, installs the Android SDK and connects coding agents.
- `tomaccoctl`: one typed API of 170 operations as a JSON CLI and a stdio MCP
  server, with durable jobs, artifacts and shared debug sessions.
- Qt desktop in English and German with live device display, debugging and a
  visual layout lab.
- Android, Swift on Android, and SwiftPM/xtool iOS apps for a paired iPhone. The
  iPhone route is not qualified on a physical phone.
- Experimental virtual iOS device: boots your own iOS 27 image on Linux in QEMU
  with a Rust Vulkan renderer. It ships with stated limits and is not a qualified
  simulator. A SwiftUI app built on Linux with the SDK from your own Xcode download
  runs on it.
- `project.create` with `kind` `xtool` and `build` creates and builds a SwiftUI app
  in one job, and `project.app` names the `.app` and says whether the virtual device
  will take it.
- iOS tools from Joshua Warren's MIT-licensed
  [omarchy-apple-dev](https://github.com/joshuaswarren/omarchy-apple-dev), fetched at a
  pinned commit on request and never shipped: `ios.toolkit_install`,
  `project.import_xcode`, `ios.crash_symbolicate`, `ios.store_ship`, and, untested
  because no Flutter was at hand, `ios.flutter_setup` and `ios.flutter_build`.
  `ios.doctor` reports their state. Nothing here runs sudo or downloads from Apple.
  See [iPhone](docs/iphone.md#flutter-xcode-projects-crash-reports-and-the-app-store).
- For web pages in the layout lab, the viewport API is `window.__tomaccoViewport` and
  the event `tomacco:viewport`. The MCP resources are `tomacco://catalog` and so on, the
  demo page is `tomacco://demo`, and the Android automation package is
  `io.github.marselnenaj.tomacco.automation`.
- The release tarball holds its own `install.py`.
- Updates from GitHub releases: `update.check`, `update.apply`, `update.status` and
  `update.rollback`, with `updates.mode` (`off`, `notify` by default, `auto`) in
  **Settings** and `settings.configure`. A package installs only with a valid
  Ed25519 signature, through the journalled installer, and rolls back by itself if
  the new version does not start.
- The virtual iPhone gains several missing features. `ios.virtual.deploy` installs
  a built `.app` on the running phone without a reboot (about 25 s): a boot reserves
  spare trust-cache entries, the engine rewrites the module in guest RAM while the
  guest is stopped (`trust_cache_update`, the new `vphone-trust-cache-update` engine
  patch), and the signing adapter stays resident and reads a per-boot table. A
  `guest_network: nat` setting attaches an outbound-only virtio network device behind
  passt (off by default; see the security note in [legal.md](docs/legal.md)). The
  `text` control action types on the USB keyboard with the guest's layout. New guest
  operations `network_state`, `hardware_probe`, `gestalt`, `developer_mode` and
  `keyboard_layout`, and `ios.virtual.debug` joins lldb to the guest's `debugserver`
  over vsock (the attach is refused until iOS Developer Mode can be enabled). A
  keychain round-trip and a Class D write were shown on a SwiftUI app.

### Changed since the pre-release builds

- The documentation is restructured. [docs/README.md](docs/README.md) is the
  landing page, and the guides are new and task first.
- The commands are `tomacco` and `tomaccoctl`, the app ID is
  `io.github.marselnenaj.tomacco`, the data directory `~/.local/share/tomacco` and the
  variables `TOMACCO_*`.
