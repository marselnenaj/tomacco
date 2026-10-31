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

- Build produces an APK or an AAB. Installing on a device takes APKs, split APKs
  included.
- For other Gradle apps, set `applicationId` with `project.configure`, or pass
  `bundle_id` to `project.run`.
- Release signing stays in the project's Gradle configuration and keystore.
- A Flutter run supports hot reload through `job.send`, and holds the project's
  workspace lock until it exits.

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
`android.permission`, `android.deep_link`, `android.performance`, `android.settings`
and `android.emulator`.

`android.input` types ASCII text with `adb shell input text`. Other text, such as
`Grüße` or emoji, cannot go that way, so Tomacco pastes it: it starts the configured
scrcpy 4.1 server briefly without video, sets the device clipboard and presses paste
into the focused field. This needs scrcpy configured and Android 7 (API 24) or newer,
replaces what was on the device clipboard, and answers `text_unsupported` with a
remedy when it cannot work.

`device.logs` with `bundle_id` keeps only that app's lines. On Android 12 (API 31)
and newer it filters by the app's user ID, so the capture can start before the app
and continues across restarts. On older Android it follows the process ID of the
running app, so launch the app first.

```sh
tomaccoctl call device.logs '{"platform":"android","device":"emulator-5554","bundle_id":"dev.example.newapp","duration":30}'
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

## Phones

Enable USB debugging and authorize the computer. For wireless debugging, use
`android.connect` with the device's address and pairing code. Everything else works
the same as on the emulator.

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
