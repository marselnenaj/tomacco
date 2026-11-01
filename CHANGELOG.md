# Changelog

Notable changes per release. The [release notes](docs/RELEASE_NOTES.md) summarise a
release. Versions follow [semantic versioning](https://semver.org) for the `tomaccoctl`
operations and their JSON results.

## Unreleased

`tomaccoctl` now has one typed API of 228 operations.

### Added

- `workflow.run` runs up to 20 operations in order in one call, follows each job a step starts until it ends, stops at
  the first step that fails and returns every step's result. A destructive step needs its own `confirm`; a later step
  can take a value from an earlier step's result with `{"from_step": N, "pointer": "/result/..."}`.

- MCP resource templates, listed by `resources/templates/list`: `tomacco://jobs/{id}` (the job), `tomacco://jobs/{id}/log`
  (the end of its log) and `tomacco://reference/{operation}` (the operation's help and schema), and the resource
  `tomacco://devices`. `completion/complete` completes operation names and recent job IDs for them.
- MCP elicitation: a client that declares it is asked to put a yes-or-no question to the person before
  `android.sdk_install` accepts licences, before `play.commit`, an `ios.store_ship` or `apple.store` upload, and before
  an `ios.virtual.setup` stage marked `ask_first`, instead of the call failing or the agent having to ask. A no answers
  `consent_declined`. Other clients see no change.
- `TOMACCO_MCP_TOOLS` picks a shorter MCP tool list for agents with a small context: `compact` (first sentences, no
  argument descriptions; 136,247 bytes instead of 265,234) or `grouped` (one tool per area that takes an operation and
  its arguments; 29,261 bytes). Both add a `schema` tool for one operation's whole schema.
- **Quick Boot.** `android.emulator` `stop` saves a Tomacco-managed emulator's Quick Boot snapshot first
  (`save_quick_boot`, on by default), and `android.avd_start` resumes from it; its result's `boot` says `quick` or
  `cold` and why. After `android.avd_configure` or `android.avd_profile` the old snapshot is removed once. On the
  development host a Pixel 11 Pro Fold emulator (API 36 x86_64) booted cold in 21.4 s and quick in 8.6 s; the save
  took 3.9 to 4.9 s and 3.7 GB for a 4 GB emulator.
- `android.avd_start` takes `camera_image`: the emulator's back or front camera shows a PNG or JPEG, for QR code and
  document scanning. A QR code in such an image was read by zbar from the stock Camera app's preview and photo.
- `android.sensors` simulates an emulator's sensors: any sensor's values, the device turned by gravity with the
  rotation read back, a shake, proximity, light, a fingerprint touch, and a foldable's posture. On the API 36
  emulator, fingerprint touches enrolled a finger in Settings and unlocked the lock screen, and the postures gave
  Android's closed, half-open and open device states.
- A foldable profile such as `pixel-11-pro-fold` gives the AVD a hinge, with the open screen as its display and the
  folded one as a display region.
- `android.app_links` reads an app's App Links verification per domain, verifies again, names the app and activity a
  link opens or the chooser, and approves, denies or resets domains for tests. Run online against YouTube's domains on
  the API 36 emulator. `android.deep_link` names the `activity` and `package` that opened the link.
- `android.network_capture` captures an emulator's traffic, or what one app sends, after `opt_in:true`, with the
  image's tcpdump as root, and summarises hosts, TLS server names, HTTP requests without their query and DNS queries.
  The pcap file is readable only by you. HTTPS stays encrypted: no certificate is installed. Run on the API 36
  emulator for the whole emulator and for Chrome alone.

### Changed

- Every error has a `remedy`: where the place that failed gave none, the error carries its code's general one. The
  new [error reference](docs/reference/errors.md), generated from `assets/errors.json`, lists all 435 codes with what
  each means and what to do, and a unit test fails when code returns an error code the catalogue lacks.
- The Arch package builds 1.2.1: `packaging/arch/PKGBUILD` and `.SRCINFO` name its source tarball, whose SHA-256 matched
  the release's `.sha256`, and the `v1.2.1` commit. `makepkg -d` built it in 2 min 55 s on the development host.
- The README's social card and how-it-works diagrams said 170 operations; they now say 224, and the diagram fits the count
  in its column. A test checks the drawn count against `assets/operations.json`.
- README, getting started and the FAQ said `tomaccoctl setup` connects Claude Code and Codex; it also connects Gemini
  CLI, Cursor, VS Code, Zed and OpenCode, as [Agents](docs/agents.md#connect-a-client) says.
- CONTRIBUTING has one first-build sequence: clone, build, run the tests offscreen and call `tomaccoctl` from the
  checkout, with the times it took in a fresh clone.
- `android.avd_create` accepts system image tags with hyphens and extension levels, and refuses TV, Wear OS,
  Automotive and XR images with `form_factor_unsupported` and the avdmanager command that creates one: Tomacco's
  profiles describe phones, foldables and tablets.

### Tests

- An end-to-end MCP session test (`AgentConversationTests` in `tests/python/test_product.py`) plans, builds an
  Android project on a fake Gradle wrapper, follows the job with progress and log messages, reads the job and its log
  as resources and runs the same build through `workflow.run`, with no device or SDK.

## 1.2.1

The first published 1.2 release; `v1.2.0` was tagged but not released.

### Fixed

- The installer's uninstall and rollback ran only on Python 3.11 or newer; it now also runs on Python 3.10, as on
  Ubuntu 22.04. The release check found it in a clean Ubuntu 22.04 container.
- `ios.virtual.deploy`, and so `project.run`, installed an app and then failed with `ios_virtual_application_conflict`
  when the profile listed the same bundle identifier from another path, such as an earlier copy of the project. The
  deploy now replaces that listing (its log says `relisted`) and keeps the app the start opens; the same live
  `project.run` then opened the app.
- A start refused for a `.tomacco-engine-tmp` that others may enter said to free space; it now says `chmod 700` it or
  remove it.
- Settings showing a "?" square for every top-level row's icon is iOS serving icons it kept from an earlier session.
  Troubleshooting has the `icon_cache` `clear` call: on a live phone the icons came back in a Settings started after
  it, about 2 minutes after the next start, not in the Settings that had kept running.

### Changed

- Virtual iPhone renderer: less work per frame on the drain, under the lock the guest's processors also wait for. The
  render Store converts the frame for the present beside its copy into the guest's pages instead of before it; the frame
  mailbox's hand-off copy runs in up to four bands; and the sampled-texture cache's content digest, which only picks the
  candidate a byte compare then decides, is a four-lane multiply hash instead of two SipHash passes. A recorded live run
  had spent about 1.9 ms a frame on that conversion, 2.3 ms on that copy, 1.5 ms on the hand-off and up to 305 ms a
  second on digests. Offline on the development host: conversion plus copy 6.0-7.0 ms against 3.8-4.4 ms side by side,
  the hand-off 1.1-1.4 ms against 0.3-0.5 ms, a 2 MB digest 0.55 ms against 0.09 ms. On a live phone, one boot each with the old and the new engine and the same
  script: 5.9 instead of 3.6 distinct frames a second while swiping, 11.3 instead of 8.8 presents a second, and 7.3
  instead of 16.6 ms of Store work per present; Settings scrolled at 18.1 instead of 15.9 presents a second.
  Screenshots matched pixel for pixel, with no black or stale frames.
- The virtual iPhone example has an app icon of its own, a white T on tomato red, named with xtool's `iconPath`.
  On a live phone `project.run` built it in 3.3 s, installed and opened it, and the icon showed on the Home screen.

## 1.2.0

`tomaccoctl` now has one typed API of 224 operations.

### Added

- **Virtual iPhone app loop.**
  - `ios.virtual.deploy` skips the trust-cache exchange, the transfer and the install when this run already installed the
    same signed app, and only opens it: the deploy log says `unchanged`. `force` installs it again. On a live phone a
    second `project.run` of an unchanged app built in 2.9 s and was opened in 0.6 s.
  - `project.run` with `device` `virtual-iphone` builds an xtool project, deploys it on the running virtual iPhone and opens
    it in one job, then looks for a new crash report of the app for `crash_check_seconds` and fails the job as `crashed`
    when one appears. The run's `build/tomacco/virtual_iphone_run.json` has the build and deploy seconds, `unchanged`, the
    pid and the crash check. Refused at once when the phone is off. When iOS refuses the open right after the install
    as `InvalidRequest`, as it did once on a live phone, the deploy asks again every 5 s for up to 2 minutes and logs
    each refusal with iOS's reason; `deploy.open_refusals` counts them. On a live phone with a copy of
    examples/hello-virtual-iphone under a new bundle identifier: built in 9.5 s, installed and opened in 13.4 s with no
    refusal, no crash report in 10 s, and a `testing.run` `wait_screen` matched the app's screenshot exactly (0 changed
    pixels) 6.2 s after its `launch`. A changed build of the example itself deployed in 7.3 s and showed its new text.
  - `project.watch` with `device` `virtual-iphone` rebuilds and redeploys an xtool project on each save, leaves xtool's
    `xtool`, `.xtool` and `.bsp` output out, and logs one line per cycle with the build and deploy seconds, `unchanged`,
    the pid and the crash check. Unit-tested; not run on a live phone.
  - `ios.virtual.syslog` takes `bundle_id` and returns only that app's lines: its executable's own and launchd's about its
    service, matched by name, so following with `offset` keeps them across relaunches. Tested on synthetic console lines.
  - `examples/hello-virtual-iphone`, a SwiftUI starter in xtool's format for the virtual iPhone, and the walkthrough
    [Your first SwiftUI app on the virtual iPhone](docs/tutorial-virtual-iphone.md). Its build and `project.app` check ran;
    running it on a live phone did not.
- `tomaccoctl setup` also connects OpenCode (`~/.config/opencode/opencode.json`, `mcp`). Apps that run these
  agents, such as T3 Code, then reach Tomacco through them; docs/agents.md says so.
- **Release.**
  - `release.version` changes every version file of a project in one call: `pubspec.yaml`, Expo's `app.json`, a React
    Native or Expo `package.json`, the Android Gradle file and the versions catalog entry or `gradle.properties` key it
    reads, and an xtool project's `Info.plist`. `bump` raises the major, minor or patch part or only the build number,
    and `dry_run` lists the changes without writing.
  - `release.notes` writes Google Play "What's new" files per language from a section of a Markdown changelog, in
    fastlane's, Gradle Play Publisher's or a whatsnew directory's layout, and refuses a text over Play's 500 characters
    unless `truncate` cuts it at whole lines. Its result is `play.prepare`'s `release_notes`, which now also takes
    language codes with digits such as `es-419`.
  - `android.size_report` breaks an APK or AAB down by size per category, ABI, density and module with its largest
    files, estimates the download per ABI and density (from `bundletool get-size total` for an AAB when bundletool is
    configured), and compares with a `baseline` build.
  - `release.signing_health` reports each key of a keystore with its expiry, days left, key algorithm and size and
    fingerprint, warns about keys expiring within a year or before Play's 22 October 2033 minimum, weak keys and SHA-1
    signatures, matches the keys with the upload and app signing certificates Play Console shows, and checks that an
    APK or AAB is signed with one of them.
  - `release.checklist` checks an Android release build before an upload, from the build files and manifest: targetSdk
    against Play's required level, versionCode above the last release, not debuggable, minify on, a release signing
    config other than the debug key, and no cleartext HTTP unless allowed, each as a structured check with evidence.
  - `android.store_screenshots` captures an app's store screenshots as one job on each device, for each app
    language and for light and dark mode, each screen reached by its own `testing.run` steps, into fastlane supply's
    `phoneScreenshots`, `sevenInchScreenshots` and `tenInchScreenshots` folders, checks the sizes against Google Play's
    rules and puts the device settings back afterwards.
- **Testing.**
  - `testing.record` records a UI test on an Android device: taps, swipes, text and keys sent through
    `android.input`, `runtime.input` (the desktop's live display included) and `testing.action` become `testing.run`
    steps. A tap becomes a click on the element under it when the UI hierarchy names it, otherwise a tap at
    coordinates. Scenarios gain the steps `tap`, `swipe` and `text` for that.
  - `testing.lint` checks a scenario against the current screen or a saved hierarchy without acting: selectors that
    match nothing, several elements or a disabled one, points off the screen and malformed keys.
  - `testing.run` takes `retries`, or a scenario its own `retries`: a failed scenario runs again on that device.
    Each result lists its `attempts`, a pass after a failure is `flaky`, the report lists them under `flaky`, and
    `junit.xml` writes the earlier failures as `flakyFailure` or `rerunFailure`.
  - `testing.run` takes a directory of scenarios as `path`, and with `shard:true` splits them across the devices,
    each scenario once on the next free device, into one report and one `junit.xml`. Without it every device runs
    every scenario.
- **Projects.**
  - `project.watch` runs the app again with `project.run` whenever its sources change and have stayed
    unchanged for `debounce_ms`, as a job with one log line per cycle; build, dependency and tool directories are
    ignored. A Flutter project keeps one session and gets Hot Reload on each change.
  - `project.analyze` runs `dart analyze --format=json` for Dart and Flutter projects, and the project's own
    `eslint` and `tsc --noEmit` for JavaScript and TypeScript projects, as a job whose `report.json` lists each
    issue with file, line, column, severity and code.
  - Flutter tests: `project.execute` with `action` `test` also writes the JSON reporter's output to
    `build/tomacco/flutter_test.json`, and `testing.results` reads it, or any `flutter test --machine` output, into its
    summary. Failed golden-file comparisons are listed under `golden_failures` with flutter_test's diff images.
  - `project.dependency_updates` also checks pub (`pub outdated --json`), npm, pnpm and Yarn 1 (their `outdated`
    JSON) and SwiftPM (`Package.resolved` pins against the repository's tags from `git ls-remote`), in the same
    report, each entry marked with its `ecosystem`. `dart`, `git`, `pnpm` and `yarn` can be set with
    `settings.configure`.
  - A running Flutter session's job shows the app's Dart VM service URI and, when Flutter reports them, its DevTools
    and Dart Tooling Daemon URIs under `result.flutter` in `job.get`, with a `devtools_app_uri` that opens DevTools
    on the app.
  - `project.execute` with `action` `instrumented-test` and the new `device` argument runs a Flutter project's
    `integration_test` directory on that device (`flutter test integration_test -d SERIAL`), with results for
    `testing.results` in `build/tomacco/integration_test.json`. For Gradle, `device` sets `ANDROID_SERIAL`.
  - `job.send` `reload` or `restart` reloads a React Native or Expo debug run through Metro's `/reload` endpoint
    on port 8081, and answers with Metro's HTTP status. Before, the command went to the session's input, where
    Metro did not read it.
- **Session log and replay.** With `TOMACCO_SESSION_LOG` set to a file, every operation called through `tomaccoctl call`
  or `tomaccoctl mcp` is appended to it as a JSON line: arguments with recognised secrets masked, outcome, a short
  result summary and the duration. `tomaccoctl replay FILE [--dry-run]` calls the logged operations that change
  something again, in order, waiting for their jobs and stopping at the first failure.
- An operation reference in `docs/reference/`, one page per area, generated from `assets/operations.json` by
  `tools/build_reference.py`: each operation's title, description, annotations, arguments with types, choices,
  limits and defaults, and an example call. A test fails when it is out of date.
- A tutorial, [Your first Android app](docs/tutorial-android.md): from an installed Tomacco to the Compose starter on
  the emulator, a screenshot, a checked tap and a passing UI test, in 103 seconds on a host with the SDK and Gradle
  cache already present.
- A guide for agent authors, [Agent patterns](docs/agent-patterns.md): planning with `capabilities`, waiting on jobs
  with progress, acting on an error's `remedy`, safe and destructive operations by their annotations, and recovering
  from failures, with real results.
- The issue form links troubleshooting and asks for an optional `tomaccoctl report` bundle; the pull request
  checklist follows CONTRIBUTING, and CONTRIBUTING says how the documentation is tested.
- **Update banner.** A new version shows as a band at the top of the main window with **What's new**,
  **Install** and **Later**; Later hides it for that version. Found while no Tomacco window is active, a
  new version also sends one desktop notification. The word in the status line stays.
- The **Updates** settings explain Off, Notify and Automatic in one line each. After an installation,
  also one made by itself in Automatic mode, the banner says **Updated to X** and offers **Restart now**.
- `desktop.open` replaces a desktop on the session that is not the installed version, such as the one
  that ran while an update was installed, instead of raising its window; the result names it as
  `replaced_version`. The desktop's `state` and `show` answers carry its `version`.
- **Restart after an update.** **Restart now** starts the installed launcher with the same project, view and window
  size, and the new desktop waits for the old one to end so it gets the agent session. The first start of a new
  version shows a **What's new in X** sheet from the installed release notes, once.
- **First start.** A short sheet runs `doctor` and lists what is left to set up in plain words, with
  **Connect your coding agent** (copies the MCP configuration) and **Skip**. It shows once; **Get started**
  in the command palette opens it again.
- **Update channel.** `updates.channel` in `settings.configure` is `stable` (the default) or `prerelease`;
  on `prerelease`, `update.check` and `update.apply` take the newest release including GitHub
  pre-releases. The **Updates** settings offer the choice, and the settings export carries it.
- `make_release.py --with-qt-sources` releases the source tarballs of the Qt modules the portable package bundles, pinned
  by URL and SHA-256 in `packaging/portable/qt-sources.txt`; Qt WebEngine's comes from Qt's online repository, as it is not
  among the 6.12.0 submodules.
- `doctor` names the `apt-get` or `dnf` command that installs the system libraries the desktop is missing, on Debian, Ubuntu
  (with the t64 package names of 24.04), Fedora and their derivatives, as `desktop.install`; the `desktop` next step and
  `tomaccoctl setup` show it. For the portable package it also checks the X11 and Wayland plugins and WebEngine's helper,
  and `desktop.portable` says which package it is. Run in debian:12, ubuntu:22.04, ubuntu:24.04 and fedora:44 containers,
  the one command made the 1.1.0 portable desktop loadable.
- `tomaccoctl verify FILE [--sig FILE.sig] [--sha256 FILE.sha256] [--json]` checks a downloaded release file against its
  checksum and its Ed25519 signature with the release keys built into `tomaccoctl`, in plain text or as one JSON envelope, and
  exits 0 only when both match. It verified the published 1.1.0 package and refused a copy with one byte appended.
- `tools/check_dependencies.py` checks the Rust dependencies offline: licenses compatible with GPL-2.0-or-later, Apache-2.0-only
  crates named in the notices, no new crate in two incompatible versions, no yanked version in Cargo's index cache, and the
  notices' crate tables in step with `Cargo.lock`; with cargo-deny installed, also `cargo deny check` with the new `deny.toml`.
  `make_release.py` runs it.
- With cargo-audit installed, `tools/check_dependencies.py` runs `cargo audit --no-fetch` against the local RustSec database,
  and `make_release.py` refuses a release it reports a vulnerability for. Building says how to install it.
- **SBOM.** `make_release.py` writes a CycloneDX 1.6 SBOM beside each package, `PACKAGE.cdx.json`, with `tools/build_sbom.py`,
  and signs it: the Rust crates linked into `tomaccoctl` with versions, licenses and checksums, the embedded SQLite, the fonts, and
  for the portable package Qt, ICU, FFmpeg and the xcb-util libraries with the digests of their pinned archives.
- **Provenance.** `make_release.py` writes and signs `tomacco-VERSION-provenance.intoto.json`, an in-toto statement with a SLSA
  provenance v1 predicate: the tag and commit, `SOURCE_DATE_EPOCH`, the digests of `Cargo.lock` and of the portable package's
  pinned downloads, the builder's OS and tool versions, and the SHA-256 of every release file. `tomaccoctl verify` checks its
  signature and the listed files beside it.
- **Release key rotation.** `tomaccoctl` trusts the release key in `native/product.rs` and, during a rotation, the next key
  beside it, `NEXT_RELEASE_SIGNING_PUBLIC_KEY_BASE64`; a package signed by either installs, one signed by another key is refused.
  `make_release.py` and the container build accept both. [Building](docs/building.md#rotating-the-release-key) has the steps.

- **Virtual iPhone.**
  - Copy-free starts, experimental and the default while a `qemu-img` is found (beside the helper engine or on `PATH`;
    `ios.virtual.configure {"start_disks":"copy"}` keeps copies): a start makes the run's root
    disk an empty qcow2 overlay on the base's with `qemu-img` instead of copying 32 GiB, and copies only the small
    auxiliary and SEP disks. The engine and the guest-tools helper attach it as qcow2, the host reads APFS through the
    chain, pruning and `ios.virtual.cleanup` keep the copies an overlay reads, and after 8 layers a start flattens the
    chain. `ios.emulator.prepare_guest_tools` takes `root_overlay` and `qemu_img`, and `start_disk` in
    `ios.virtual.status` names the `method`. Run on a live phone twice, the second start layered on the first: the disk
    preparation took 16 s instead of 89 s for a copy, a verified stop 4.4 to 4.9 s instead of 258 s, and a run 0.4 GiB
    instead of 32 GiB. Switching back to `copy` flattened the chain live in 54 s. Two more live starts layered on a raw
    base, the second from a profile that names no `start_disks`: 16 to 18 s of preparation, 5.1 s for the overlay,
    0.3 to 0.6 s of input hashing instead of 198 s, starts of 307 and 342 s, verified stops of 4.3 and 4.8 s and
    0.4 GiB per run, both with a visible display. The flattening that 8 layers trigger ran on a 9-overlay chain over
    that live base with the start's own plan and command, not in a start: 47.9 s, 24.1 GiB written.
  - Saved phone states: `ios.virtual.state_save` keeps the last verified run under a name without copying it,
    `ios.virtual.states` lists the names, `ios.virtual.state_use` makes one the base of the next start and
    `ios.virtual.state_delete` removes a name and the copy only it kept. Pruning and `ios.virtual.cleanup` keep named runs.
    Run on a live profile: save, the refusal for an unsaved base, `state_use` back and forth, a start from the chosen
    state, and deletes that kept copies later overlays read. The iOS test system dialog lists them under **Saved states**
    with **Save state**, **Start from it** and a **Delete** that asks twice.
  - `start_timing` and `stop_timing` in `ios.virtual.status` break the last start and stop down into steps with their
    seconds (checks, preparation, disk copy, guest-tools install, input hashing, boot, app install, guest settings,
    unlock and launch; guest exit, output hashing), with the slowest step and the total, also kept in the run's
    `timing.json`. The disk preparation now logs when each of its steps began (`step_times`), and the launcher's
    `request.json` and `result.json` have `timing`: hashing seconds and bytes before and after the session and when
    the engine started and exited. Read on a live start that flattened an overlay chain: preparation 10 s, disk copy
    54 s, input hashing 198 s (the slowest), app install 69 s, guest settings 153 s, 614 s in all; its stop spent
    198 s of 199 s hashing the output.
  - `ios.virtual.host_check` adds `host` (logical processors, physical cores, memory) and `recommend`: what to change in
    the configured profile's `host_cpu_policy` and `translation_cache_mib` for this host, with the current and suggested
    value and why. It changes nothing. Unit-tested; the effect of a suggested setting has not been measured on a live
    phone. Its `qemu_img` item, also in `ios.virtual.doctor`, warns when the profile asks for overlay starts and no
    `qemu-img` is found, and names the path beside the helper engine where it must be. The warning is unit-tested; the
    live profile found its `qemu-img` there.
  - `ios.virtual.profiles` lists every profile directory configured so far with its run, phase and whether its phone runs.
    `ios.virtual.configure` refuses another directory while the current phone runs (`ios_virtual_running`) or a start or
    stop is in progress (`ios_virtual_busy`); before, it switched and left the running phone without its profile. The
    refusal was seen with a live phone.
  - `ios.virtual.syslog` reads the phone's console log (the guest job's `uart.log`) as structured lines with time, process,
    PID, level and message, filtered by process, level or text, and followed with `next_offset`. Read against a live
    phone's log: 9,283 lines, 8,238 of them launchd's records about services, 1,043 the kernel's and no app's own log
    records. launchd's domains (`user/501/…`, `pid/PID [NAME]`, `system`) are read as the service's process, not as
    processes named `501` or `system`.
  - `last_run` in `ios.virtual.status` says in plain words how the last finished run ended: whether it was verified,
    the launcher's outcome with a summary, each reason it was not verified (no exit code 0, incomplete storage hashes,
    no confirmed flush, the graphics memory guard) and a remedy. A failed stop's error and a boot that ended early say
    the same. Read off a live phone after a verified stop (`verified:true`, no reasons).
  - A start that fails after the boot ends with what to call next, and `next` in `ios.virtual.status` says the same:
    resume with `ios.virtual.start`, which adopts the still-running guest without a new disk copy, with
    `{"open_app":false}` when an app launch's outcome is unknown, after unlocking a passcode, or stop again. Unit-tested
    over the start's phases, not tried on a live phone.
  - `host` in `ios.virtual.status` shows the graphics card's memory against the graphics memory guard's limit on every
    call, from the running guest's own guard checks (which the launcher now publishes as `gpu-memory.json`) or from
    /sys, and the host's I/O and memory pressure. A start that boots returns `warnings` when the card is already low or
    the host under pressure. Beside a live guest it read the guard's figures (3.2 GiB of 16 GiB used, level `ok`).
  - `liveness` in `ios.virtual.status` says whether a running phone still shows signs of life (new frames, console
    output, and with `{"probe":true}` an answer from the guest service): `live`, `quiet`, `stalled` or `booting`, with
    what it last showed and when. A stalled phone gets `recovery`, the typed calls to make in order (`restart_ui`, then
    `ios.virtual.stop`); the status itself never acts on the phone. `ios.virtual.configure {"stall_seconds"}` sets the
    limit. Not tried on a hung live phone. Only a new frame or an answered probe makes a phone `live`; console output
    alone is `quiet` with `console_only:true`. Before, a live phone whose display stayed black said `live` from console
    output while its frame had not changed for 410 s; that run's recorded signs are now a test fixture and read `quiet`.
    A working live phone read `live` from its frames and an answered probe.
  - The engine writes its scratch files, the shader translator's included, to `.tomacco-engine-tmp` beside the engine
    instead of the shared /tmp, and a start refuses when that directory cannot take 16 MiB. A live start whose /tmp was
    over its quota had every shader translation refused (`Disk_quota_exceeded` in the renderer's `fail.log`) and a black
    display for the whole run; the two live starts after the change had a visible display. A black display's warning now
    names such a scratch failure.
  - A redeployed app the guest tools were built with is admitted by the hash its deploy wrote. Before, installd
    rejected it as an invalid signature (`0xe8008014`), seen live after the example app's text changed; after the
    change the same redeploy installed and opened.
  - `ios.virtual.unlock` leaves the running phone's lock screen the way the start does, as a short job, and never
    enters a passcode. `ios.virtual.deploy` unlocks first before it opens the app, and `testing.run` on
    `virtual-iphone` before its first step, failing with the new `ios_virtual_locked` when it cannot. Tested with
    scripted guest replies; on a live phone the deploy's unlock found the screen already unlocked.
  - `ios.virtual.doctor` checks the configured phone in one pass, warn or fail report with a remedy per item: every
    input `profile.json` names (with `tools/ios_virtual_profile.py`'s checks, run by the new
    `tools/ios_virtual_doctor.py`), guest tools and engine against this checkout's sources, the saved states' disks,
    the last run's verdict, the next start's disk copy, every `ios.virtual.host_check` item, and the graphics card and
    host pressure right now. Against the full live test profile it warned only about passt, which is not installed.
  - The desktop's phone view types the host clipboard's text into the focused field with Ctrl+V or **Paste**, through the
    `text` action in pieces of up to 1024 characters. Not tried on a live phone yet.
  - The desktop's phone view sends typed characters to the phone as text, so `@`, `ä` and the like arrive as the host's
    layout types them, and Enter, Backspace, the arrows and keys with Ctrl, Alt or Meta by name. **Keyboard to phone**
    shows when it has the keyboard; Ctrl+Shift+K gives the keyboard back to the desktop and takes it again. A modifier
    pressed alone is no longer sent.
  - **Speed** shows new and fetched frames per second and the time from an input to the first new frame after it, over
    the picture. **Screenshot with frame** saves a framed screenshot through `ios.virtual.screenshot`.
  - The phone's own window has App switcher, Notification Center, Control Center, the keyboard switch and the speed
    overlay on its rail, a pin that keeps it above other windows, and opens where it was last (not on Wayland).
  - A trackpad's coasting after the fingers lift no longer scrolls the phone, and lifting them sends the scroll at once.
  - All offscreen tests only; not tried on a live phone.

### Changed

- The README and the documentation index describe 1.1.0: the portable package, the five desktop languages, what
  is new, and the new tutorial, agent patterns and reference.
- docs/building.md says why there is no Flatpak or AppImage: Tomacco runs adb, the emulator, Gradle, xtool, usbmuxd and QEMU
  on the host, and agents call `tomaccoctl` by a fixed path.
- `install.py` journals and flushes once per batch of up to 512 files instead of once per file: the portable package
  (2184 files) installs in 0.7 s instead of 15 s on the development host, and uninstalls and rolls back as fast. Backups are
  hard links where possible. An uninstall killed halfway now finishes when run again; before, the files whose removal was
  journalled but not done were kept as if the user had changed them.
- `make_release.py` builds with `SOURCE_DATE_EPOCH` set to the release commit's time and the package's entries get UID and
  GID 0, so two builds of a commit in the same directory give the same package. Before, the tarball's time stamps and the
  time Qt's `rcc` stored for the translations differed per build.
- The Arch PKGBUILD and .SRCINFO build 1.1.0 from its source tarball.
- Virtual iPhone: a start's streamed disk copy keeps the base's holes and leaves out every 4 MiB chunk of zeros, so the copy
  takes the space of the disk's data, and the hashes the launcher takes before and after a session read only that data;
  it hashes holes as zeros without reading them, and its limit of extent queries rose from 4096 to 262144. Before, every
  copy was written out to its full 32 GiB, and both hashes read all of it at 128 MiB/s. On a synthetic 4 GiB disk with
  0.5 GiB of data, the copy took 3.9 s instead of 13.6 s and the launcher's hash 5.4 s instead of 32.4 s, with the same
  SHA-256. The start's space check and `start_disk.needed_bytes` count the data the base's disks hold instead of their
  full size, so a start fits on a fuller disk. Not run on a live phone.
- The virtual iOS device's troubleshooting guide opens with a table of every error code it returns, with what each
  means and what to do. A test fails when a code there or in a symptom does not exist in the code, or when a code the
  code returns is missing from the table.

### Fixed

- **Virtual iPhone, found on a live phone.**
  - `ios.virtual.host_check` asked for room for a 32 GiB copy when the profile starts with an overlay; it now asks for
    what the overlay start needs (1 GiB on the test profile).
  - `ios.virtual.setup` named the engine build as the next stage, and `run` asked to build it, for a profile that was
    written, selected and running but whose earlier outputs were elsewhere or older. The plan now says `ready` and names
    no next stage.
  - `ios.virtual.crash_reports` left pid, process and capture time empty for reports named with their kind first, such as
    `ExcUserFault_chronod-…`; `testing.crashes` counted those fault reports as crashes and showed every frame's image as
    `???`. They are now read, listed as `not-a-crash`, and frames show the image's UUID when the report names none.
  - `assert_screen` and `wait_screen` take `ignore` regions, as `testing.visual_diff` does: the status bar's clock
    otherwise fails an exact comparison a minute later.
  - `testing.record` on the virtual iPhone keeps a wait of a second or more between inputs as a `pause` step; a replay
    sent a recorded tap, swipe and Home within 0.4 s, before the app had opened.
- THIRD_PARTY_NOTICES.md named SQLite 3.50.2; the locked `libsqlite3-sys` 0.39.1 embeds 3.54.1. A test keeps them in step.
- The recipes and troubleshooting said `android.input` types ASCII only; since 1.1.0 it pastes other text through the
  device clipboard. Getting started and building showed 1.0.0 package names and said a release has no portable package.
- A test keeps the version, the operation count, the Rust and Qt build minimums, the desktop languages and the
  `tomaccoctl` flags the documentation names in step with their sources.

## 1.1.0

`tomaccoctl` now has one typed API of 193 operations.

### Added

- `doctor` checks with the dynamic loader whether the desktop can start on this system, lists missing libraries and symbol
  versions under `desktop`, and puts a step first in `next_steps` when it cannot.
- **Command line.**
  - `tomaccoctl completions bash|zsh|fish`. Only the bash script has been run.
  - `tomaccoctl schema OPERATION` and `schema --names`.
  - A manual page, `man tomaccoctl`, installed by CMake and the installer; a test keeps it in step with `--help`.
  - `tomaccoctl help OPERATION` and `call OPERATION --help` describe an operation in plain text: arguments with types,
    choices, limits and defaults, and an example call generated from the schema.
  - `call … --wait` follows a job to its end, with its log on stderr.
  - Indented JSON in a terminal or with `--pretty`; output to a pipe is unchanged.
  - "Did you mean" remedies for misspelt operations, argument keys and flags.
- **Update and uninstall.** `tomaccoctl update [--check]` and `tomaccoctl uninstall`.
  The installer now keeps a copy of itself for uninstall and rollback.
- **Portable package.** `tools/build_portable.py` builds a release package with Qt 6.12 and an
  LGPL-only FFmpeg bundled, for glibc 2.35 and newer; `make_release.py` signs it beside the
  regular package, and `install.py` and `tomaccoctl update` handle it.
- **MCP prompts.** `setup`, `run-android-app` and `run-virtual-iphone`.
- **MCP tool annotations.** Every operation now states `destructiveHint` and `idempotentHint` as well
  as `readOnlyHint` and `openWorldHint`; before, MCP clients read 104 operations as destructive by
  default. `testing.setup` and `testing.instrumentation` have titles.
- **MCP structured results.** A tool result also comes as `structuredContent`; a result that is not
  an object is wrapped as `{"result": ...}`, an error as `{"error": ...}`.
- **MCP progress.** A tool call with a progress token gets `notifications/progress` once a second;
  for `job.wait` with the job's state and latest log line.
- **MCP logging.** After `logging/setLevel` `info`, `job.wait` sends its job's new log lines as
  `notifications/message`.
- `capabilities` says in one call what this computer can do right now: Android builds, emulator and
  devices, iOS builds with xtool, iPhones and the virtual iPhone, each `ready` or with what it lacks.
  Also the MCP resource `tomacco://capabilities`, with an `outputSchema`.
- `tomaccoctl setup` also connects Gemini CLI, Cursor, VS Code and Zed, after asking, by adding
  `tomacco` to their JSON settings. A file with comments or a link is left for the person to edit.
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
  - `android.package_inspect` reads an APK or AAB without a device: package, version,
    SDK levels, permissions, ABIs and signing certificate digests.
  - `device.install` installs one `.aab` through bundletool, built for the selected
    device.
  - `android.display` locks the screen orientation and overrides display size and
    density, then reads them back; `reset` undoes them.
  - `android.notifications` lists the notifications shown, opens or closes the shade
    and snoozes one by its key.
  - `android.app_files` lists or pulls the private files of a debuggable app through
    `run-as`.
  - `android.each` runs the same steps, such as install and launch, on every connected
    Android device as one job with a result per device.
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
- **Projects.**
  - `project.inspect` reports each Android app module's application ID, build types,
    flavors, variants and minimum, target and compile SDK from the Gradle files and
    versions catalog without running Gradle, and lists what it could not resolve.
    `project.run` uses that ID and takes a `variant` for apps with product flavors.
  - `project.run` runs the Android app of a React Native project (Gradle, then Metro on
    port 8081) or an Expo project (`npx expo run:android`), and refuses with the install
    command when `node_modules` lacks the framework. Tested with stand-in tools only.
  - `project.run` runs a Capacitor or Ionic app on Android: the web build script,
    `npx cap sync android`, Gradle install and launch. Tested with stand-in tools only.
  - `project.inspect` recognises Kotlin Multiplatform and Compose Multiplatform builds, with
    each module's targets and the iOS app's Xcode project. `project.run` runs the Android
    target; the iOS target answers `macos_required` with what it needs.
  - `project.create` with `kind` `compose`: a Kotlin and Jetpack Compose starter with a versions
    catalog and one Compose UI test, which passed on an Android 16 emulator.
  - `project.dependency_updates` compares a Gradle build's catalog and build-file versions with
    Google Maven, Maven Central and Gradle Plugin Portal metadata, read-only, as a job with
    `report.json`; it answers `offline` at once without a network.
- **Bug reports.** `tomaccoctl report` and `diagnostics.bundle` write a local `.tar.gz` with the
  version, host, `doctor`, settings and recent failed jobs with their log ends, secrets masked.
  Nothing is sent.
- **Jobs.** `job.events` returns a job's state and phase changes and cancellation requests with
  their times, recorded by the job database for every job from now on.
- **Downloads.** The Android command-line tools download in `tomaccoctl setup` and the iPhone runner
  download of `ios.wda_prepare` continue where they stopped when run again, and the file is used
  only after its checksum matches; a mismatching file is deleted.
- **Jobs.** `android.sdk_install`, `ios.wda_prepare` and `android.connect` take an optional `retry`
  (count and backoff); a job lists its attempts in its result.
- **Jobs.** At most two builds run at once by default; more wait as `queued` with a `queued_reason`.
  `settings.configure` `jobs` sets the cap for builds and for Android emulators.
- **Jobs.** `job.list` filters by state, operation, project and creation time, and pages with
  `before`. Each new job records the operation that started it.
- **Housekeeping.** `storage.prune` lists, and with `apply` removes, old job records and
  logs and old test reports in the data directory. A dry run by default.
- **iPhone.**
  - `ios.ipa_inspect` reads an `.ipa` or `.app` without a device: bundle id, version,
    minimum iOS, entitlements, the signing certificate and a summary of the embedded
    provisioning profile, with warnings such as an expired profile. Unit-tested only, on
    synthetic files.
  - `ios.crash_reports` lists a USB-connected iPhone's crash reports with go-ios and copies
    them to a directory, returning paths for `ios.crash_symbolicate`. Unit-tested only; no
    phone was connected.
- **Virtual iPhone.**
  - `ios.virtual.screenshot` writes a PNG, with optional rounded corners or a bezel.
  - `ios.virtual.record` records the screen to MP4.
  - `ios.virtual.status` reports phase times, copy progress and whether the disk has
    room for the next start.
  - None of these four has been run on a live phone yet.
  - `ios.virtual.apps` lists the phone's apps in one place: each listed app's version from
    its bundle on the host, whether the current run has it installed, the one the start
    opens, and installed apps the profile no longer lists. Unit-tested only.
  - `ios.virtual.crash_reports` keeps an app's crash reports from the running phone as files
    on the host, with paths for `ios.crash_symbolicate`. Unit-tested only.
  - `ios.virtual.cleanup` deletes older disk copies in the profile's `installs`, with a dry run
    by default that reports the bytes it would free. It never touches the verified base, the
    current run or a copy a guest holds. Unit-tested only.
- **Desktop.**
  - An Italian interface, chosen under Language in Settings or followed from the desktop language.
  - A Spanish interface, chosen under Language in Settings or followed from the desktop language.
  - A French interface, chosen under Language in Settings or followed from the desktop language.
  - Drop an `.apk`, `.ipa` or `.app` on the device view to install it, or drop a
    project folder on the window to open it.
  - Screenshots are also copied to the clipboard.
  - Recent projects on the start screen and in the command palette.
  - The window reopens at its last size.
  - The Omarchy panel can open a job's log (V key, `log` command).
  - A job's log can be filtered by text and by level, and the lines shown copied.
  - Builds & jobs shows each job's duration and project, filters running or failed
    jobs, opens the log file and runs again a job the desktop started.
  - A desktop notification when a job ends while the window is in the background;
    it can be turned off in Settings.
  - The live device view can mark touches and show the pointer's device pixel and
    screen fractions, for writing UI tests.
  - Settings can be exported to a JSON file and imported on another computer, without
    paths, window sizes or history.
  - Keyboard and accessibility: named icon buttons and list rows, a wider focus ring,
    and keyboard navigation in the jobs, tools and file lists.
- **Web layout lab.**
  - `preview.matrix` also writes `contact-sheet.png`, all captures side by side at one
    scale and labelled with model and orientation.
  - `preview.configure` and the lab's View panel set the page's `prefers-color-scheme`
    and `prefers-reduced-motion`, per session, through the DevTools protocol.
  - `preview.audit` finds a page wider than the viewport, text under the camera cutout
    or system bars, and controls under 44 pt (iOS) or 48 dp (Android), each with a
    selector that matches the element alone. `window.__tomaccoViewport` gains `platform`.
  - Profiles for the Galaxy Z Fold8 Ultra, Z Fold8 and Z Flip8 and the Galaxy Tab S12+
    and Tab S12 Ultra.
  - `preview.errors` reports one page load's console errors, uncaught exceptions,
    unhandled rejections and failed requests as structured results.
  - Network throttling and offline for the lab's page: Fast 3G, Slow 3G, offline or
    your own latency and bandwidth, with `preview.configure` or the View panel.
- **Packaging and guides.**
  - An Arch `PKGBUILD` in `packaging/arch`.
  - Debian, Ubuntu and Fedora package lists; not tested on those systems.
  - An Android example in `examples/`.
  - [Recipes](docs/recipes.md) and an [FAQ](docs/faq.md).

### Changed

- `doctor`'s `next_steps` for the virtual iOS device name the next stage of its setup chain and
  carry the whole chain in order, with what is already built; with a profile they name what
  stops the next start (disk space, a failed start, apps not installed). Unit-tested only.
- Operations that write a file (screenshots, recordings, traces, reports, profile exports, signed releases) refuse a destination
  that is a symbolic link or a special file; `catalog.export` now checks its destination like the others.
- `doctor` checks all tools at once instead of eight at a time, and a `call` parses only its own operation's schema, not all of them.
- `ios.virtual.start` refuses up front, before deleting or copying anything, when the
  disk lacks room for the copy.
- The panel `ios.virtual.status` reports comes from the running guest's boot, not from
  the current profile.
- Unknown argument keys are reported before missing required ones.

### Fixed

- Errors, job records and job logs mask recognised secrets (authorization headers, tokens, signing passwords, private keys) as
  `[redacted]`, and a secret given where its file's path belongs is no longer repeated in the error.
- Job logs and worker logs are created 0600 and the data directories 0700 in one step, whatever the umask; older logs are
  tightened when they are rewritten. The installer creates `~/.local/share/tomacco` and its backups owner-only.
- Runtime and debugger sockets, the virtual iPhone's launcher and the tests no longer fail with a long `TMPDIR`
  ("path must be shorter than SUN_LEN"): socket directories move to a private `XDG_RUNTIME_DIR` or `/tmp` when `TMPDIR` is too long.
  `tests/update_e2e.rs` works in `target/tmp` without a `TMPDIR`, and the touch-lane tests no longer fail under parallel load.
- A command the emulator console refused (`KO`) no longer looks like success:
  `emulator_refused`.
- Every network latency preset sends its millisecond range; the emulator's own `gprs`
  name applied the UMTS range.
- `testing.setup` and `testing.instrumentation` have titles, shown translated in the desktop; a test now requires a title,
  a description and a German title for every operation.
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
