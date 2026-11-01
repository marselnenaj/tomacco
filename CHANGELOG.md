# Changelog

Notable changes per release. The [release notes](docs/RELEASE_NOTES.md) summarise a
release. Versions follow [semantic versioning](https://semver.org) for the `tomaccoctl`
operations and their JSON results.

## Unreleased

`tomaccoctl` now has one typed API of 200 operations.

### Added

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

### Fixed

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
