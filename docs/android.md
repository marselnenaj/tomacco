# Android

Build Android apps on Linux and run them on the real Android Emulator or on a
phone. This covers native Kotlin and Java, Flutter, and Swift on Android.

**Tested on:** an Android 16 (API 36) x86_64 emulator. Physical ARM64 phones
and live Play submission have not been qualified.

## From zero to a running app

1. **Install the tools.** You need the
   [Android SDK command-line tools](https://developer.android.com/studio#command-line-tools-only),
   a JDK 17 or newer and Gradle. The starter was verified with Gradle 9.3.1 and JDK 17.
   Unpack the command-line tools to `SDK/cmdline-tools/latest`, because that is where Tomacco
   looks for `sdkmanager`.

2. **Tell Tomacco where they are.** The desktop has the same under *SDK & tool paths*.

   ```sh
   tomaccoctl call settings.configure '{"android_sdk":"/absolute/Android/Sdk","java_home":"/usr/lib/jvm/default","executables":{"gradle":"/usr/bin/gradle"}}'
   tomaccoctl doctor
   ```

3. **Install SDK packages.** Package installation accepts licenses only when you
   say so. `android.sdk_packages` lists what is available.

   ```sh
   tomaccoctl call android.sdk_install '{"packages":["platform-tools","emulator","platforms;android-36","build-tools;36.0.0","system-images;android-36;google_apis;x86_64"],"accept_licenses":true}'
   ```

4. **Get a device.** Create and start an emulator, or connect a phone with USB
   debugging authorized.

   ```sh
   tomaccoctl call android.avd_create '{"name":"First","system_image":"system-images;android-36;google_apis;x86_64","profile_id":"pixel-11"}'
   tomaccoctl call android.avd_start '{"name":"First"}'
   tomaccoctl call device.list '{"platform":"android"}'
   ```

5. **Create, build and run.** Wait for each returned job with `job.wait`; see
   [jobs](agents.md#jobs).

   ```sh
   tomaccoctl call project.create '{"path":"/absolute/NewApp","kind":"android","name":"NewApp","organization":"dev.example"}'
   tomaccoctl call project.execute '{"path":"/absolute/NewApp","platform":"android","action":"build","format":"apk"}'
   tomaccoctl call project.run '{"path":"/absolute/NewApp","platform":"android","device":"emulator-5554"}'
   ```

6. **Look at it.**

   ```sh
   tomacco --project /absolute/NewApp --workspace runtime
   ```

## Projects

`project.inspect` finds a Gradle project at the project root or in `android/`. A
project's own Gradle wrapper takes precedence over the configured Gradle, and its
own build configuration stays as it is. Flutter uses the installed Flutter CLI.

The starter has a native Android UI and a persistent counter. It uses the highest
installed SDK platform, and the Android Gradle Plugin 9.1.1.

`kind: "compose"` creates the same counter in Kotlin with Jetpack Compose and Material 3,
with a versions catalog in `gradle/libs.versions.toml` and one Compose UI test in
`app/src/androidTest`. It needs Android platform 36. Run the test with
`project.execute` and `"action":"instrumented-test"`. A new project built, passed its UI
test on an Android 16 emulator and ran with `project.run`.

- Build produces an APK or an AAB. Installing on a device takes APKs, split APKs
  included, or one AAB through bundletool; see below.
- `project.inspect` reads each app module's application ID, namespace, minimum,
  target and compile SDK, build types, product flavors and variants from the build
  files and `gradle/libs.versions.toml`, without running Gradle. It follows
  `rootProject.ext`, `extra` and gradle.properties values and catalog versions; what
  only Gradle can compute, such as Flutter's `flutter.minSdkVersion` or a module
  configured by a convention plugin, is listed under `unresolved` or in a `note`.
- `project.run` installs the app module's variant and launches the ID read from the
  build files. An app with product flavors needs `variant`, such as `freeDebug`. To
  override the ID, set `applicationId` with `project.configure`, or pass `bundle_id`.
- Release signing stays in the project's Gradle configuration and keystore.
- A Flutter run supports hot reload through `job.send`, and holds the project's
  workspace lock until it exits.
- `project.watch` runs the app again whenever its sources change, or sends a Flutter
  session Hot Reload; see [the agent guide](agents.md#android).

## React Native, Expo and Capacitor

`project.inspect` names the framework under `javascript`, with the package manager
its lockfile implies, whether `node_modules` has the framework, and how
`project.run` will run the Android app.

- **React Native.** `project.run` installs `android/` with Gradle, then starts Metro
  with `npx react-native start` on port 8081, reverses that port to the device and
  launches the app. The job keeps Metro running until you cancel it. A release run
  installs and launches without Metro.
- **Expo.** `project.run` runs `npx expo run:android --device SERIAL`, which writes
  `android/` with prebuild when it is missing and serves the JavaScript with Metro
  until you cancel the job. A release run passes `--variant release --no-bundler`.
- **Capacitor and Ionic.** `project.run` runs the package's `build` script when it
  has one, then `npx cap sync android`, installs `android/` with Gradle and launches
  the app ID from the Gradle files, or `appId` from `capacitor.config.json` or `.ts`.
  Without `android/` it answers `android_project_missing` and names
  `npx cap add android`.
- Install the dependencies first (`npm install`, or your package manager); without
  them the run answers `dependencies_missing` with the command. Without Node.js it
  answers `tool_missing`.

These runs are covered by tests with stand-in tools; no React Native, Expo or Capacitor
app was built with them yet.

## Flutter

`project.execute` with `"action":"test"` runs `flutter test --reporter expanded
--file-reporter=json:build/tomacco/flutter_test.json`: the log stays readable and the
JSON reporter's file is kept as an artifact, even when tests fail. `testing.results`
on the project reads it into the summary it gives for JUnit results, one suite per test
file. A failed golden-file comparison is listed under `golden_failures` with the
golden's name, flutter_test's message (such as `Pixel test failed, 1.20%, 480px diff
detected.`) and the paths of the diff images it wrote to `failures/`: `masterImage`,
`testImage`, `isolatedDiff` and `maskedDiff`.

```sh
tomaccoctl call project.execute '{"path":"/absolute/MyApp","action":"test"}'
tomaccoctl call testing.results '{"path":"/absolute/MyApp","job_id":"JOB_ID"}'
```

`"action":"instrumented-test"` with a `device` runs the project's `integration_test`
directory on that device: `flutter test integration_test -d SERIAL`, its JSON reporter
writing `build/tomacco/integration_test.json`, which `testing.results` reads the same
way. Without `device` it answers `device_required`, and without an `integration_test`
directory `integration_test_missing`. For a Gradle project, `device` sets
`ANDROID_SERIAL` for the connected tests.

```sh
tomaccoctl call project.execute '{"path":"/absolute/MyApp","action":"instrumented-test","device":"emulator-5554"}'
```

Tested with a stand-in `flutter` that writes the JSON reporter's events; Flutter is not
installed here.

A Flutter session from `project.run` (and the one `project.watch` keeps) runs `flutter
run --machine`. While it runs, `job.get` shows under `result.flutter` what Flutter
reported about it:

- `vm_service_uri`, the Dart VM service's WebSocket address from `app.debugPort`, and
  `vm_service_http_uri`, the same service for a browser or `dart devtools`.
- `devtools_uri`, from `app.devTools` when Flutter serves DevTools, and
  `devtools_app_uri`, that address with `?uri=` the VM service, which opens DevTools on
  this app.
- `dtd_uri`, the Dart Tooling Daemon, from `app.dtd`.

Flutter reports only what it serves, so a key can be missing. Without a DevTools URI,
`dart devtools` opens DevTools, which asks for the `vm_service_http_uri`. The addresses
are on this computer; for a device, Flutter forwards the port.

```sh
tomaccoctl call job.get '{"job_id":"RUN_JOB_ID"}'
```

Tested with a stand-in `flutter` that prints these events.

## Kotlin Multiplatform and Compose Multiplatform

`project.inspect` adds the kinds `kotlin-multiplatform` and, with the
`org.jetbrains.compose` plugin, `compose-multiplatform`. `multiplatform` lists each
module's targets (`androidTarget`, `iosArm64`, `jvm` and so on) and the Xcode project
of the iOS app, such as `iosApp/iosApp.xcodeproj`.

- The Android target builds and runs like any Gradle app: `project.run` installs the
  app module, for example `:composeApp:installDebug`, and launches its ID.
- The iOS target does not build on Linux. Kotlin/Native links iOS frameworks only on
  macOS with Xcode, so `project.plan` and `project.run` for `ios` answer
  `macos_required`. Build it on a Mac: configure one with `remote.configure`, then use
  `apple.build` for the Xcode project or `remote.run` for Gradle.

Tested on fixture projects only; no Kotlin Multiplatform app was built here.

## Dependency updates

`project.dependency_updates` compares the versions a Gradle build declares with Maven
metadata, without running Gradle or changing a file. It reads `gradle/libs.versions.toml`
and the build files, asks Google Maven, Maven Central and the Gradle Plugin Portal in
that order, and returns a job whose `report.json` lists every dependency and plugin with
its source line, latest stable and latest version and a status: `update_available`,
`prerelease_update_available`, `up_to_date`, `managed` (no version, such as libraries
under a BOM), `dynamic`, `unresolved` (`$variable`), `not_found` or `unreachable`.

```sh
tomaccoctl call project.dependency_updates '{"path":"/absolute/NewApp"}' --wait
```

Without a network it answers `offline` at once. `repositories` replaces the defaults
with your own `https://` or `file:///` repositories. It ran against the Compose starter
online.

The same job checks the other package managers a project has, each entry marked with
its `ecosystem`:

- **pub.** `flutter pub outdated --json` (`dart pub` for a plain Dart package), for direct
  and dev dependencies, with pub's `upgradable` and `resolvable` versions besides the
  latest. A package pub marks discontinued has `discontinued: true`.
- **npm.** Every dependency and dev dependency in `package.json`, through `npm outdated
  --json`, or `pnpm outdated --format json` or Yarn 1's `yarn outdated --json` when
  their lockfile is there. A package that is not installed is `not_installed`.
- **SwiftPM.** Each pin of `Package.resolved`, at the root or inside an `.xcodeproj` or
  `.xcworkspace`, against the version tags `git ls-remote --tags` lists for its
  repository. A pin to a branch or revision is `unversioned`. Nothing is resolved or
  fetched into the project.

`ecosystems` in the report names each one checked, with its `error` when its tool is
missing or failed; the job fails only when none could be checked. Online, `npm outdated`
(npm 11.19) and `git ls-remote` against GitHub gave the expected entries for a small
`package.json` and `Package.resolved`; the pub path ran only with a stand-in `flutter`.

## Static analysis

`project.analyze` runs the analysers a project already has and returns a job whose
`report.json` lists every finding as an issue with `tool`, `file` (relative to the
project), `line`, `column`, `severity` (`error`, `warning` or `info`), `code` and
`message`, with `totals` per severity and `passed` (no errors, and every analyser ran).

- A Dart or Flutter project runs `dart analyze --format=json`. Without a configured
  `dart`, it uses the `dart` in Flutter's `bin` directory.
- A JavaScript or TypeScript project runs `eslint --format json .` and, when it has a
  `tsconfig.json`, `tsc --noEmit`, both from its own `node_modules/.bin`. Nothing is
  installed or downloaded.

`tools` limits the run to `dart`, `eslint` or `tsc`. Findings do not fail the job; it
fails only when no analyser produced a result.

```sh
tomaccoctl call project.analyze '{"path":"/absolute/MyApp"}' --wait
```

Tested with stand-in analysers that print the formats above; no real Flutter or
JavaScript project was analysed here.

## Inspect an APK or AAB

`android.package_inspect` reads a package file without a device: package name, version
name and code, minimum, target and compile SDK, permissions, native ABIs and the
signing certificates with their SHA-256 digests.

```sh
tomaccoctl call android.package_inspect '{"path":"/absolute/NewApp/app/build/outputs/apk/debug/app-debug.apk"}'
```

- An APK is read with `aapt2 dump badging` and `apksigner verify --print-certs` from
  the newest installed build-tools. An unsigned APK answers `signing.signed:false` with
  apksigner's errors.
- An AAB's compiled manifest is read directly, so no extra tool is needed. Its JAR
  signature is read with `keytool` from the configured JDK.
- If apksigner or keytool is missing, the rest is still reported and `signing.checked`
  is `false` with the reason.
- Checked on APKs with the API 36 build-tools. AAB reading is covered by unit tests on
  generated bundles; no Gradle-built AAB has been read yet.

## Install an AAB

`device.install` with one `.aab` runs Google's
[bundletool](https://github.com/google/bundletool) as a job: `build-apks
--connected-device` builds the APK set for that device, and `install-apks` installs it.
The APK set is deleted when the job ends.

```sh
tomaccoctl call settings.configure '{"executables":{"bundletool":"/absolute/bundletool-all.jar"}}'
tomaccoctl call device.install '{"platform":"android","device":"emulator-5554","paths":["/absolute/app-release.aab"]}'
```

- Download `bundletool-all-VERSION.jar` from its releases page. A configured `.jar` runs
  with the configured Java; a `bundletool` command on `PATH` works too. Without either,
  the answer is `tool_missing` with that remedy.
- bundletool signs the APKs it builds with the debug keystore in
  `~/.android/debug.keystore`, which any earlier debug build creates; the bundle's own
  signature does not carry over. Signing the APK set with your own key is not offered
  yet.
- Only the commands are covered by tests with a stand-in bundletool. No AAB has been
  installed on a device through it yet.

## See and control the device

The desktop shows the device's own video stream, so the status bar, keyboard and
animations are the real ones. This needs **scrcpy 4.1**, configured through
`settings.configure`. Tomacco finds `scrcpy-server` beside the executable, in its
`../share/scrcpy` directory or in `/usr/share/scrcpy`, and `runtime.start` can name
a matching server JAR with `server`. Other scrcpy wire versions are not accepted
silently.

```sh
tomaccoctl call runtime.start '{"platform":"android","device":"emulator-5554"}'
tomacco --project /absolute/app --workspace runtime
```

In the desktop, tap and drag inside the picture, type with the keyboard, or use
Home, Back, Rotate and Screenshot. The picture keeps its real aspect ratio, and
the margins around it are not forwarded as touches. Unicode text goes through the
device clipboard. To replace a whole field, use `testing.setup` and
`testing.action` with `fill`.

For agents, the same device is reached by `runtime.input`, `runtime.capture` and
`runtime.hierarchy`; see [display sessions](agents.md#display-sessions). Without a
display session, use `device.capture`, `device.logs`, `android.hierarchy`,
`android.input`, `android.record`, `android.mirror`, `android.ports`,
`android.permission`, `android.deep_link`, `android.performance`, `android.settings`,
`android.display`, `android.notifications`, `android.app_files` and `android.emulator`.

`android.input` types ASCII text with `adb shell input text`. Other text, such as
`Grüße` or emoji, cannot go that way, so Tomacco pastes it: it starts the configured
scrcpy 4.1 server briefly without video, sets the device clipboard and presses paste
into the focused field. This needs scrcpy configured and Android 7 (API 24) or newer,
replaces what was on the device clipboard, and answers `text_unsupported` with a
remedy when it cannot work.

`android.screen` saves the screen as a PNG at once, without a job, for previews
that refresh; `device.capture` keeps evidence with a job. Checked on the API 36 x86_64
emulator.

`android.record` records the screen as a job, for up to 180 seconds; `job.send` with
`command` `stop` ends it early, and the video is still pulled to `path`. The stop
interrupts `screenrecord` on the device, which then finishes the file. On the API 36
x86_64 emulator a recording stopped after 6 seconds gave a playable H.264 MP4.

```sh
tomaccoctl call android.record '{"device":"emulator-5554","path":"/absolute/clip.mp4","seconds":180}'
tomaccoctl call job.send '{"job_id":"RECORD_JOB","command":"stop"}'
```

`device.logs` with `bundle_id` keeps only that app's lines. On Android 12 (API 31)
and newer it filters by the app's user ID, so the capture can start before the app
and continues across restarts. On older Android it follows the process ID of the
running app, so launch the app first.

```sh
tomaccoctl call device.logs '{"platform":"android","device":"emulator-5554","bundle_id":"dev.example.newapp","duration":30}'
```

`device.info` reads a device's properties and, under `health`, what matters at a
glance in one read-only call: Android version, SDK level and security patch, battery
level, state, health and temperature, free and total storage on `/data`, screen size
and density in pixels and dp (an override from `android.display` counts), and uptime in
seconds. A part the device does not report, such as the battery of a device without
one, is `null`. Read on the API 36 x86_64 emulator.

```sh
tomaccoctl call device.info '{"platform":"android","device":"emulator-5554"}'
```

`device.label` gives a device a nickname or pins it. `device.list` then shows the
nickname as `name`, keeps the earlier name as `original_name`, sets `pinned` and lists
pinned devices first. An emulator is named by its AVD, so the nickname stays when its
serial changes (checked on the API 36 x86_64 emulator); `avd:NAME` names one that is off. An empty nickname or `pinned:false`
removes the label, and a call with neither only reads.

```sh
tomaccoctl call device.label '{"device":"emulator-5554","nickname":"Small phone","pinned":true}'
tomaccoctl call device.label '{"device":"avd:Tomacco-pixel-11","nickname":""}'
```

## Device settings for tests

`android.settings` sets what changes an app's look and motion, so UI tests and
screenshots start from a known state. Leave out what should stay as it is; the result
reads every setting back. A value Android accepts but does not apply answers
`setting_not_applied` with what it reads instead.

```sh
tomaccoctl call android.settings '{"device":"emulator-5554","dark_mode":"on","font_scale":1.3,"animations":false}'
tomaccoctl call android.settings '{"device":"emulator-5554","bundle_id":"dev.example.newapp","app_locale":"de-DE"}'
tomaccoctl call android.settings '{"device":"emulator-5554"}'
```

- `dark_mode` is `on`, `off` or `auto`, through `cmd uimode night`.
- `font_scale` is the system font size from 0.5 to 2; 1 is the default.
- `animations:false` sets the window, transition and animator scales to 0. `true`
  sets them back to 1.
- `airplane_mode` needs Android 11 (API 30) or newer.
- `app_locale` sets the language of one app, given by `bundle_id`, through Android's
  per-app languages, which need Android 13 (API 33) or newer. An empty value returns
  the app to the device language. The language of the whole device is not changed:
  adb cannot do that without root.

### Orientation, display size and density

`android.display` locks the screen orientation and overrides the display size or
density, for layouts on other screens without another emulator. The result reads
all three back, and a value Android did not apply answers `setting_not_applied`.

```sh
tomaccoctl call android.display '{"device":"emulator-5554","orientation":"landscape"}'
tomaccoctl call android.display '{"device":"emulator-5554","width":720,"height":1600,"density":320}'
tomaccoctl call android.display '{"device":"emulator-5554","reset":true}'
```

- `orientation` is `portrait`, `landscape`, `reverse-portrait`, `reverse-landscape` or
  `auto`. A locked orientation turns auto-rotate off; `auto` turns it on again. An app
  that fixes its own orientation keeps it.
- `width` and `height` go together, through `wm size`; `density` goes through
  `wm density`. Both stay until `reset`, also across app restarts.
- `reset` returns size and density to the device's own and rotation to the sensor,
  before any other argument in the same call.
- Checked on the API 36 x86_64 emulator, where landscape turned the display by
  90 degrees and `reset` brought back its own size and density.

### Notifications

`android.notifications` lists what Android is showing, so a test can check that its
app posted the right notification. Each entry has the key, package, ID, tag, channel,
flags and, when set, title, text, sub-text and big text. `bundle_id` keeps one app's.

```sh
tomaccoctl call android.notifications '{"device":"emulator-5554","bundle_id":"dev.example.newapp"}'
tomaccoctl call android.notifications '{"device":"emulator-5554","action":"open"}'
tomaccoctl call android.notifications '{"device":"emulator-5554","action":"snooze","key":"0|dev.example.newapp|7|null|10200","seconds":600}'
```

- `open` and `close` expand and collapse the notification shade through
  `cmd statusbar`, for a screenshot or a tap on a notification with `android.input`.
- adb cannot cancel another app's notifications: Android refuses it to the shell. To
  get one out of the way, `snooze` hides it for `seconds` and fails with
  `notification_not_snoozed` when it is still listed.
- Every action returns the list afterwards. The list is read from
  `dumpsys notification --noredact`.
- Listing, open, close and snooze were checked on the API 36 x86_64 emulator with a
  notification posted by `cmd notification post`.

### App data files

`android.app_files` lists or pulls the private files of a debuggable app, such as
its databases and shared preferences, through `run-as`. Android allows that only for
debuggable builds, so a release build answers `app_not_debuggable`. Paths are relative
to the app's data directory.

```sh
tomaccoctl call android.app_files '{"device":"emulator-5554","bundle_id":"dev.example.newapp"}'
tomaccoctl call android.app_files '{"device":"emulator-5554","bundle_id":"dev.example.newapp","action":"pull","path":"databases","output":"/absolute/out/databases.tar"}'
```

- `list` returns each file's path, size and modification time.
- `pull` returns a job that writes the files as one `.tar` archive; unpack it with
  `tar -xf`.
- To force-stop the app or clear its data, use `device.app` with `stop` or
  `clear-data`.
- Listing, pulling and the refusal for a non-debuggable app were checked on the API 36
  x86_64 emulator with a debug build.

## Sound

On Android 11 and newer, output sound is forwarded to the computer by default when
a native session connects. The **Sound** button (*Ton*) switches this without
restarting the app, emulator or video. The desktop remembers the choice. Agents
pass `audio:false` to `runtime.start`, or control a session:

```sh
tomaccoctl call runtime.audio '{"session":"SESSION_ID","action":"status"}'
tomaccoctl call runtime.audio '{"session":"SESSION_ID","action":"start"}'
tomaccoctl call runtime.audio '{"session":"SESSION_ID","action":"stop"}'
```

- Playback uses the configured scrcpy client with a 60 ms buffer. No microphone is
  captured.
- One background job owns the audio of a device. A second start reuses it. Stopping,
  disconnecting or losing the runtime worker ends it.
- An audio failure leaves the video connected. It shows in `runtime.state` under `audio`, in
  the job log and beside the desktop's button.
- `enabled` means forwarding was asked for and its job is active. It does not say
  that an app is making sound.
- While capture is active, sound is redirected away from the device. Stopping restores
  it. Android 11 must be unlocked when capture starts, and apps may restrict capture.
  See [scrcpy's audio notes](https://github.com/Genymobile/scrcpy/blob/master/doc/audio.md).
- `android.avd_start` enables emulator audio by default, also with `headless:true`.
  Pass `audio:false` for a silent emulator. An emulator started with `-no-audio` can
  still use `runtime.audio` without a reboot.

## Emulators

`android.avd_create` takes an installed system image and a device profile from
`catalog.list`. A profile sets the display's resolution and density, not the
vendor's firmware. `android.avd_profile` refuses to change a running emulator.
Tomacco keeps its emulators in its own registry.

`android.avd_configure` stores `gpu`, `vulkan`, `memory_mb` and `cores` for a stopped
emulator. It keeps app data and backs up the old configuration. Changed hardware
cold-boots.

```sh
tomaccoctl call android.avd_configure '{"name":"First","gpu":"host","vulkan":"off","memory_mb":4096,"cores":4}'
tomaccoctl call android.avd_start '{"name":"First","headless":true}'
```

`android.avd_start` can override `gpu` and `vulkan` for one run. Choosing the software
GPU keeps CPU acceleration; the older `software` flag turns that off too. Use the host
renderer only after checking its picture and stability on your machine.

The desktop's Omarchy panel can boot an emulator without its own window and show just
the phone; see the [desktop guide](desktop.md#device-window).

### Snapshots, location, battery and network

`android.emulator` drives a running emulator through its console with `adb emu`.
Snapshots keep the whole running state, so a test can return to a logged-in screen
in seconds instead of booting.

```sh
tomaccoctl call android.emulator '{"device":"emulator-5554","action":"snapshot-save","name":"logged-in"}'
tomaccoctl call android.emulator '{"device":"emulator-5554","action":"snapshot-list"}'
tomaccoctl call android.emulator '{"device":"emulator-5554","action":"snapshot-load","name":"logged-in"}'
tomaccoctl call android.emulator '{"device":"emulator-5554","action":"snapshot-delete","name":"logged-in"}'
tomaccoctl call android.emulator '{"device":"emulator-5554","action":"location","latitude":48.2082,"longitude":16.3738}'
tomaccoctl call android.emulator '{"device":"emulator-5554","action":"battery","battery_level":15,"charging":false}'
tomaccoctl call android.emulator '{"device":"emulator-5554","action":"network","network_speed":"edge","network_delay":"gprs"}'
```

- `snapshot-list` returns the snapshot names and the console's own table.
- `battery` sets the charge in percent and whether the charger is connected, and
  returns what the emulator then reports.
- `network` takes the emulator's presets: speeds `gsm`, `hscsd`, `gprs`, `edge`,
  `umts`, `hsdpa`, `lte`, `evdo`, `5g` and `full`, and latencies `gprs` (150 to 550 ms),
  `edge` (80 to 400 ms), `umts` (35 to 200 ms) and `none`. `full` and `none` remove the
  limits. Latencies are sent as these ranges because the emulator's own `gprs` name
  applied the UMTS range on the tested emulator. For airplane mode, which also works on
  phones, use `android.settings`.
- The console answers some refusals with `KO` while adb still succeeds, for example a
  snapshot load in an emulator started with `-read-only`. Tomacco reports those as
  `emulator_refused` with the console's reason.

## Every device at once

`android.each` runs the same steps on every connected Android device, or on the
`devices` you list, as one job. Devices run at the same time; on each device the
steps run in order and stop at the first failure. A step that returns a job, such as
an install, is followed to its end.

```sh
tomaccoctl call android.each '{"steps":[{"operation":"device.install","arguments":{"paths":["/absolute/app-debug.apk"]}},{"operation":"android.launch","arguments":{"bundle_id":"dev.example.newapp"}}]}'
```

- Steps leave out `device` and `platform`; they are set per device. A step is checked
  against its operation's schema before anything runs.
- The steps can be `device.install`, `device.app`, `android.launch`,
  `android.permission`, `android.settings`, `android.display`, `android.deep_link`,
  `android.input` and `android.notifications`.
- The job's `report.json` artifact lists every device with each step's result, job
  state or error, and `failed` names the devices that failed. The job fails when any
  device failed.
- Without `devices`, every device that `device.list` shows as `device` takes part,
  including emulators that other tools started.
- Install, launch and a notification list ran as one job on one API 36 x86_64
  emulator. Several devices at once, and a failure on one of them, are covered by tests
  with a stand-in adb; no run on several real devices yet.

## Phones

Enable USB debugging and authorize the computer. Everything else works the same as on
the emulator.

### Wireless debugging

Android 11 and newer pair over Wi-Fi by QR code. `android.pair` makes a one-time code
for the phone's **Developer options › Wireless debugging › Pair device with QR code**;
`wait` then pairs and connects as soon as the phone offers it on the local network
(adb's mDNS discovery). Nothing is stored; the password lives only in the code.

```sh
tomaccoctl call android.pair '{"path":"/absolute/pair.png"}'
tomaccoctl call android.pair '{"action":"wait","service":"tomacco-…","password":"…","timeout":60}'
```

- `qr` returns `qr_text`, the modules as `qr.rows` and an SVG; with `path` it also writes
  a PNG or SVG file to show the person.
- `wait` answers `pairing_timeout` while the phone has not scanned the code; call it again.
  Its result names the `device` to use, or `connected:false` when the phone offered no
  connection service in time.
- With a pairing code instead, `services` lists what adb finds, such as the address
  the phone shows under **Pair device with pairing code**; pass it with the code to
  `android.connect`, then connect to the address under **Wireless debugging**.
- The code is checked by decoding it with zbar. Pairing and connecting were tested with
  a stand-in adb only; no phone was paired over Wi-Fi.

## Swift on Android

Swift compiles to native Android code. SwiftUI and UIKit do not come along: they are
Apple's UI frameworks. Tomacco's starter therefore has native Android widgets and a Swift
core linked through JNI.

The tested toolchain is Swift **6.4**, its matching Android SDK and **NDK 30**. Follow
the [official Swift setup](https://www.swift.org/documentation/articles/swift-sdk-for-android-getting-started.html)
for the downloads. `swift.sdk_install` requires a checksum for an HTTPS download, and
`swift.status` reports the selected SDK and whether its sysroot is ready.

```sh
tomaccoctl call settings.configure '{"android_ndk":"/absolute/Android/Sdk/ndk/30.0.16248370","swift_android_sdk":"swift-6.4.0-RELEASE_android"}'
tomaccoctl call swift.android_configure '{}'
tomaccoctl call project.create '{"path":"/absolute/SwiftCounter","kind":"swift-android","name":"SwiftCounter","organization":"dev.example"}'
tomaccoctl call project.execute '{"path":"/absolute/SwiftCounter","platform":"android","action":"build"}'
tomaccoctl call swift.execute '{"path":"/absolute/SwiftCounter/Swift","platform":"linux","action":"test"}'
tomaccoctl call project.run '{"path":"/absolute/SwiftCounter","platform":"android","device":"emulator-5554"}'
```

Wait for `swift.android_configure` before building. It runs the SDK's own script to link
the NDK; repeat it after moving either one.

- The starter builds ARM64 and x86_64 libraries for API 26 and newer. ARM64 is built
  but has not run on a physical phone.
- The generated app does not depend on Tomacco being installed.
- `tomacco.json` selects architectures, API level, package and SDK. `SWIFT_EXECUTABLE`,
  `SWIFT_ANDROID_SDK` and `ANDROID_NDK_HOME` override tool paths.
- Standalone `Package.swift` projects work too, through `swift.plan` and `swift.execute`.
  Android unit tests cannot run as Linux programs, so test portable logic on Linux and
  app behaviour on a device.
- For Java objects, callbacks and async interop, use upstream
  [swift-java](https://github.com/swiftlang/swift-java).

## Debugging, tests, profiling and release

These are the same for agents and the desktop. See the sections in the agent guide:

- [Debugging](agents.md#debugging): JDWP for Java and Kotlin, LLDB for C/C++ through
  the NDK. Native debugging is qualified on API 36 x86_64 only.
- [UI tests and profiling](agents.md#ui-tests-and-profiling): semantic selectors,
  parallel test matrices, Perfetto traces.
- [Release](agents.md#release): keystores, signing, and Play edits. Live Play submission
  needs your account and has not been qualified.
