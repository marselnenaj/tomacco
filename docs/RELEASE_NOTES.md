# Tomacco 1.3.0

Tomacco: Give your agent a phone. It builds, runs and tests Android and iOS apps from Linux.
This release adds 26 operations, for 250 in all. Most of it is about speed and about letting an
agent see the screen. The [changelog](../CHANGELOG.md) lists every change.

## Highlights

- **Faster.**
  - Virtual iPhone: the renderer copies and converts each frame on several threads. Live, against
    1.2.1 on the same script, the Store chain took 3.9 instead of 7.1 ms a present.
  - Android: stopping a Tomacco-managed emulator saves its Quick Boot snapshot, and the next start
    resumes from it: 8.6 s instead of a 21.4 s cold boot for a Pixel 11 Pro Fold emulator.
  - Agents: `TOMACCO_MCP_TOOLS=compact` or `grouped` shrinks the tool list an agent reads from
    316 KB to 156 KB or 33 KB.
  - Desktop: ready after 346 instead of 404 ms (median, offscreen).
  - Installing no longer needs Python; the package's own `tomaccoctl` installs it, in about a second.
- **Seeing the screen.** `screen.marks` draws numbered boxes over what can be tapped and
  `screen.tap` taps a number; `screen.tree` is a short text form of the UI; `screen.ocr` reads text
  where there is no UI hierarchy, such as on the virtual iPhone; `screen.diff` says what changed;
  `screen.wait` waits for text or a still screen.
- **Screen export.** `screen.export` writes a screen as a PNG and a versioned JSON file with the
  device, display, insets and every element with its bounds, for design tools and visual checks.
  The [format](screen-export.md) has a JSON Schema.
- **Android performance.** Run Macrobenchmark modules as jobs, generate and install Baseline
  Profiles, and compare cold starts with and without one in a single call
  (`profile.baseline_compare`). `profile.startup` also times app starts on the virtual iPhone and
  on an iPhone over USB.
- **Agents.** Guardrails with policies, approvals and an audit log; `workflow.run` for several
  steps in one call; MCP resource templates and elicitation. The agent skill is now a short
  router with generated references for every operation; it is also at
  [tomacco.dev/skill](https://tomacco.dev/skill).
- **Android emulators.** Camera images, simulated sensors, foldables, App Links checks and
  network capture.
- **Omarchy.** Menu entries, optional key bindings, job notifications, the theme's accent in the
  panel, screenshots in Omarchy's folder, and `tomaccoctl omarchy status` and `repair`.
- **Linux hosts and ARM64.** `doctor` describes the host; releases carry aarch64 packages; the
  [Linux distributions page](linux-hosts.md) says what ran where.
- **Documentation online.** The guides and the operation reference are at
  [tomacco.dev/docs](https://tomacco.dev/docs/).

## Changed behaviour

- Installing and updating run in `tomaccoctl`; `install.py` hands over to it.
- With `--omarchy`, desktop screenshots go to Omarchy's screenshot folder instead of
  `Pictures/Tomacco`.
- When an MCP client closes the server's input, calls still running get 5 seconds and are then
  cancelled, so the server exits.
- New emulators get less memory and fewer processors on small hosts, and an emulator nobody chose
  a renderer for starts with `swiftshader` on a host without a hardware graphics driver.
- `ios.virtual.start` refuses hosts that are not x86_64 or have less than 12 GiB of memory.
- An argument array whose schema asks for unique items now refuses repeated items.

## Limits

The [1.1.0 limits](https://github.com/marselnenaj/tomacco/blob/v1.1.0/docs/RELEASE_NOTES.md#limits)
still apply. In addition:

- Tested with stand-ins only, not on real devices or projects: the iPhone-over-USB features, Flutter,
  React Native, Expo and Kotlin Multiplatform runs, Wi-Fi pairing, and the Zed, VS Code and OpenCode
  agent setups.
- The screen operations and the screen export ran live on an Android emulator; on the virtual
  iPhone and an iPhone over USB they are tested with recorded screens only. OCR marks whole lines
  of text, and a screen with light and dark areas may need a `region`.
- Macrobenchmark and Baseline Profile numbers came from an emulator, whose variation is as large as
  some of the differences; measure on a phone.
- The Omarchy menu, key bindings and notifications were tested in a scratch home, not in a live
  Omarchy session.
- On the virtual iPhone, the Settings app's top-level list can show a placeholder symbol instead of
  each row's icon; [troubleshooting](virtual-ios/troubleshooting.md) has the call that brings them
  back after a restart.
- The Docker image is not published: it contains Google's Android SDK, whose license you accept
  when you build it.

## Install and upgrade

```sh
curl -fsSL https://tomacco.dev/install.sh | bash
```

An installed 1.2 shows a banner at the top of the window that offers the update; 1.1.0 shows it in
its status bar and installs it from **Settings**. Every replaced file is backed up, and
`update.rollback` undoes an update.
