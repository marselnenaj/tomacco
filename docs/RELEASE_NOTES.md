# Tomacco 1.1.0

Tomacco: Give your agent a phone. It builds, runs and tests Android and iOS apps from Linux.
This release adds 23 operations, for 193 in all, a portable package for other distributions,
and three more desktop languages. The [changelog](../CHANGELOG.md) lists every change.

## Highlights

- **Other distributions.** A new portable package brings its own Qt 6.12 and FFmpeg. It needs
  glibc 2.35 or newer and passed its checks in clean Debian 12, Ubuntu 22.04, Ubuntu 24.04 and
  Fedora 44 containers. The one-line installer picks it where the regular package cannot start.
- **Testing.** Compare screenshots with a baseline (`testing.visual_diff`), audit an Android
  screen for accessibility (`testing.accessibility`), collect crashes and ANRs as events
  (`testing.crashes`), read Gradle's JUnit results (`testing.results`), and run the same steps
  on every connected Android device (`android.each`).
- **Android.** Unicode text input, `android.settings` (dark mode, font scale, animations, app
  language), `android.display`, `android.notifications`, `android.app_files`, APK and AAB
  inspection, AAB installs through bundletool, and emulator snapshots, battery and network.
- **Projects.** React Native, Expo, Capacitor and Kotlin or Compose Multiplatform apps run on
  Android. A Jetpack Compose starter with a UI test, app IDs and variants read from Gradle files,
  and `project.dependency_updates`.
- **Virtual iPhone.** PNG screenshots, MP4 recordings, the installed apps, crash reports, safe
  cleanup of old disk copies, a disk check before a start, and start progress.
- **Agents.** `capabilities` in one call, MCP progress and log notifications, structured results,
  tool annotations on every operation, MCP prompts and guides, and `tomaccoctl setup` now also
  connects Gemini CLI, Cursor, VS Code and Zed.
- **Command line.** `call … --wait`, `help OPERATION`, shell completions, a manual page,
  `tomaccoctl update`, `uninstall` and `report`.
- **Jobs.** Builds queue (two at a time by default), some steps can retry, downloads resume,
  and every job keeps a timeline (`job.events`).
- **Desktop.** French, Spanish and Italian; drag and drop to install; screenshots to the
  clipboard; recent projects; job notifications; log search; tap markers; settings export.
- **Web previews.** Dark mode, reduced motion, slow networks and offline; page errors per load
  (`preview.errors`); a contact sheet for `preview.matrix`; more layout checks; Samsung foldables
  and tablets in the catalogue.

## Changed behaviour

- At most two builds run at once; a third waits as `queued`. Set `jobs.max_builds` in
  `settings.configure` (0 for no limit).
- Unknown argument keys are reported before missing required ones.
- `ios.virtual.start` refuses up front when the disk lacks room for its copy.

## Limits

The [1.0.0 limits](https://github.com/marselnenaj/tomacco/blob/v1.0.0/docs/RELEASE_NOTES.md#limits)
still apply. In addition:

- The new virtual iPhone features, React Native, Expo, Capacitor and Kotlin Multiplatform runs,
  bundletool installs and the Zed and VS Code agent setups were tested with stand-ins, not on
  real devices or real projects of those kinds.
- The portable package has not been run on a real desktop session of those distributions yet.
- The French, Spanish and Italian translations have not been reviewed by native speakers.
  Corrections are welcome.

## Install and upgrade

```sh
curl -fsSL https://tomacco.dev/install.sh | bash
```

An installed 1.0.0 updates itself through `update.apply`, or run `tomaccoctl update` once 1.1.0
is installed. Every replaced file is backed up; `install.py --rollback` undoes an update.
