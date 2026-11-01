# Tomacco 1.2.0

Tomacco: Give your agent a phone. It builds, runs and tests Android and iOS apps from Linux.
This release adds 31 operations, for 224 in all. The [changelog](../CHANGELOG.md) lists every change.

## Highlights

- **Virtual iPhone.** A start no longer copies the 32 GiB system disk when `qemu-img` is beside the
  helper engine: it layers a qcow2 overlay on the last verified run. Measured on the test profile,
  disk preparation took 16 s instead of 89 s, hashing before the boot 0.6 s instead of 198 s, and a
  verified stop 5 s instead of 258 s. Also new: saved phone states, a structured guest log,
  `ios.virtual.unlock`, `ios.virtual.doctor`, hang detection (`liveness`), per-step start and stop
  timing, and a guided `ios.virtual.setup`.
- **SwiftUI on the virtual iPhone.** `project.run` with `device:"virtual-iphone"` builds an xtool
  project, deploys it, opens it and checks for a crash; an unchanged app only reopens. Run live
  with `examples/hello-virtual-iphone`: built in 9.5 s, installed and opened in 13.4 s.
  `project.watch` redeploys on every save.
- **Tests.** Record a UI test by tapping, lint it without acting, retry flaky scenarios and shard
  them across devices. Scenarios run on the virtual iPhone at screen coordinates and compare the
  screen with a baseline.
- **Performance.** `profile.startup`, `profile.frames`, `profile.memory`, `profile.usage` and
  budgets that fail a scenario; `profile.compare` reports deltas against a baseline.
- **Release work.** Store screenshots across devices, languages and themes, release notes per
  language, size reports, signing key health, a pre-release checklist and version bumps for every
  project type.
- **Flutter and cross-platform.** `project.analyze`, Flutter test and golden results, dependency
  updates for pub, npm and SwiftPM, DevTools links and Metro reloads.
- **iPhone over USB.** Device logs per app, `ios.signing`, `ios.device_status`, app files, UI tests
  through WebDriverAgent and `ios.record`.
- **Desktop.** An update banner with **Restart now** and a **What's new** sheet, first-start help,
  Wi-Fi pairing by QR code, every running device side by side, a live log beside the device,
  agent activity with durations, typing straight into the virtual iPhone, and a fuzzy command
  palette.
- **Install and trust.** The installer is about 20 times faster for the portable package. `doctor`
  names the apt or dnf command for missing desktop libraries. `tomaccoctl verify` checks a
  download; each release carries an SBOM and a signed provenance file. A Docker image can be
  built from the signed package. `tomaccoctl setup` also connects OpenCode, so apps that run these
  agents, such as T3 Code, reach Tomacco through them.

## Changed behaviour

- Virtual iPhone starts use an overlay when `qemu-img` is found; set `start_disks` to `copy` for
  the old behaviour.
- The engine gets its own scratch directory instead of the shared `/tmp`. A full `/tmp` had left a
  display black.
- `job.send`'s title is now "Send a command to a job"; it also stops recordings.

## Limits

The [1.1.0 limits](https://github.com/marselnenaj/tomacco/blob/v1.1.0/docs/RELEASE_NOTES.md#limits)
still apply. In addition:

- Tested with stand-ins only, not on real devices or projects: the iPhone-over-USB features, Flutter,
  React Native, Expo and Kotlin Multiplatform runs, Wi-Fi pairing, and the Zed, VS Code and OpenCode
  agent setups.
- On the virtual iPhone, the Settings app's top-level list can show a placeholder symbol instead of
  each row's icon; the pages behind it show their icons. UI tests there work at screen
  coordinates only, because the guest reports no UI hierarchy.
- The Docker image is not published: it contains Google's Android SDK, whose license you accept
  when you build it.

## Install and upgrade

```sh
curl -fsSL https://tomacco.dev/install.sh | bash
```

An installed 1.1.0 shows the update in its status bar and installs it from **Settings**; from 1.2.0
on, a banner at the top of the window offers it. Every replaced file is backed up, and
`update.rollback` undoes an update.
