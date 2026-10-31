# Changelog

Notable changes per release. The [release notes](docs/RELEASE_NOTES.md) summarise a
release. Versions follow [semantic versioning](https://semver.org) for the `tomaccoctl`
operations and their JSON results.

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
