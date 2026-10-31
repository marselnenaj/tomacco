# Agents

How a coding agent, or a script, drives Tomacco. The JSON CLI, the MCP server and the desktop all
call the same typed operations, so an agent can do everything the desktop can.

`tomaccoctl` is a compiled Rust program. Run it directly, not through `python3`. Neither the CLI,
MCP nor the build workers need Python; repository tests, the installer and the optional iOS
preparation recipes do.

## Connect a client

`tomaccoctl setup` connects Claude Code and Codex for you. By hand, register the absolute path of
`tomaccoctl`:

```sh
claude mcp add --transport stdio --scope user tomacco -- "$(command -v tomaccoctl)" mcp
codex mcp add tomacco -- "$(command -v tomaccoctl)" mcp
```

For another user or an uninstalled checkout, give that absolute path instead. This is a local
stdio server: no network port and no token. The installer never changes client settings; only `tomaccoctl setup` does, after asking. See the
[Claude Code](https://code.claude.com/docs/en/mcp) and
[Codex](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) documentation.

Any MCP stdio client gets the same tools. The resources `tomacco://catalog`, `tomacco://jobs`,
`tomacco://workspaces`, `tomacco://debug` and `tomacco://runtimes` expose profiles and recent work.

The server also offers three prompts, short recipes a client can show as commands:

| Prompt | Arguments | What it asks the agent to do |
| --- | --- | --- |
| `setup` | none | Call `doctor` and work through `next_steps` in order, asking before each step marked `ask_first`. |
| `run-android-app` | `path` | Inspect the project, find or start an Android device, `project.run` it, wait for the job and capture the screen. |
| `run-virtual-iphone` | `path` | Check the virtual iOS device, find or build the xtool app, deploy it to a running phone or configure and start one, then look at the screen. |

- MCP requests can run at the same time.
- `notifications/cancelled` stops that request. Cancelling `job.wait` leaves its job running; use
  `job.cancel` to cancel a build and its descendants.
- Native screenshots up to 4 MiB are embedded as MCP images. Larger ones keep a path and digest.

## The contract

**Do not guess operation names or arguments.** `tomaccoctl schema` prints every operation with its
JSON Schema: arguments, defaults, enums and bounds. `tomaccoctl schema OPERATION` prints only that
operation, as `result`, and `tomaccoctl schema --names` prints just the names, one per line, as plain
text. Unknown arguments are refused before dispatch.
A mistyped operation name or argument key gets a `remedy` naming the closest match, such as
`Did you mean project.run?` or `Did you mean arguments.path?`. With no close match, the remedy for an
unknown key lists the keys that are accepted.

```sh
tomaccoctl schema
tomaccoctl schema project.run
tomaccoctl schema --names
tomaccoctl doctor
tomaccoctl call device.list '{}'
tomaccoctl call project.inspect '{"path":"/absolute/project"}'
tomaccoctl call project.plan '{"path":"/absolute/project","platform":"android"}'
tomaccoctl call project.execute @/absolute/build-request.json
```

Arguments are inline, from a file (`@FILE`) or from standard input (`@-`). A response is one JSON
object on stdout, `{"ok":true,"result":...}` or
`{"ok":false,"error":{"code":...,"message":...,"remedy":...}}`. An error exits nonzero. In MCP mode,
standard output has protocol messages only. Opening or inspecting a project never runs its build
scripts.

When standard output is a terminal, the JSON is indented for reading. Through a pipe or into a file it
is one line, exactly as before. `--pretty` indents it anywhere, and `--compact` keeps one line at a
terminal.

For people typing commands, `tomaccoctl completions bash`, `zsh` or `fish` prints a completion script
for the commands, their flags and the operation names after `call` and `schema`. Load it with
`source <(tomaccoctl completions bash)`, the same for zsh, or `tomaccoctl completions fish | source`.
The script asks `tomaccoctl schema --names` for the names, so it stays current after an update. The
bash script is tested; the zsh and fish scripts have not been run in those shells yet.

Use `TOMACCO_HOME=/absolute/test-state` to isolate job, toolchain, profile and emulator state. The
desktop's **All tools** tab shows every operation and its schema.

## Jobs

Long operations return a job, and **queued or running is not success**. Keep the id, which is in
`result.id`.

```sh
tomaccoctl call job.wait '{"job_id":"JOB_ID","seconds":30}'
tomaccoctl call job.logs '{"job_id":"JOB_ID","offset":0}'
tomaccoctl call job.get '{"job_id":"JOB_ID"}'
```

`job.wait` waits at most 60 seconds per call, and answers `running` as a valid result: repeat it, or
read `job.logs` with `next_offset`, until the state is `succeeded`, `failed`, `cancelled` or
`timed_out`. Then check the exit status and artifacts.

From the CLI, `--wait` does that loop for you:

```sh
tomaccoctl call project.execute '{"path":"/absolute/project","platform":"android"}' --wait
```

The job's log goes to standard error as it grows, and standard output gets one envelope at the end. A
job that succeeded is `{"ok":true,"result":JOB}` with exit status 0. One that ended `failed`,
`cancelled` or `timed_out` is an error with code `job_failed`, `job_cancelled` or `job_timed_out`,
the final job in `error.details.job`, and exit status 1. A result that is not a job is printed as it
is. Interrupting `--wait` leaves the job running.

- **Artifacts** carry an absolute path, size, SHA-256 and `modified_in_job`. An old output is not
  evidence of a new build after a failed job. Incremental builds can legitimately report
  `modified_in_job:false`.
- **Durability.** Jobs persist in SQLite and survive a client disconnecting, but a sandbox or process
  supervisor can still kill their workers.
- **Cancelling.** `job.cancel` ends the process group.
- **Workspace locks.** Build, run and task jobs lock their project while they run. An identical
  active request reuses its job, and different work in the same project answers `workspace_busy` with
  the active job. A Flutter run holds the lock until it exits. Keep independent projects in separate
  directories. Commands you run outside Tomacco are not covered.

## Android

Full guide: [Android](android.md). Check prerequisites with `doctor`, `android.sdk_packages` and `android.avds`.

```sh
tomaccoctl call project.create '{"path":"/absolute/NewApp","kind":"android","name":"NewApp","organization":"dev.example"}'
tomaccoctl call project.execute '{"path":"/absolute/NewApp","platform":"android","action":"build","format":"apk"}'
tomaccoctl call device.list '{"platform":"android"}'
tomaccoctl call project.run '{"path":"/absolute/NewApp","platform":"android","device":"emulator-5554"}'
```

Then look: `device.capture` for evidence, `android.hierarchy` for the UI tree, and assert the app's state
after each action. Prefer `testing.find` and `testing.action` over coordinates when the app exposes
accessible controls.

Before UI tests or screenshots, `android.settings` fixes dark mode, font scale, animations, airplane
mode and an app's language, and reads them back:

```sh
tomaccoctl call android.settings '{"device":"emulator-5554","dark_mode":"off","font_scale":1,"animations":false}'
```

`android.display` locks the orientation or overrides display size and density, and `reset` undoes
both.

`android.notifications` lists the notifications shown, for asserting that the app posted one, and
`android.app_files` lists or pulls a debuggable app's private files.

`android.each` runs steps such as install and launch on every connected Android device as one job,
with each device's result in the job's `report.json`.

`android.package_inspect` reads what a built APK or AAB declares, without a device: package,
version, SDK levels, permissions, ABIs and signing certificate digests.
`device.install` also takes one `.aab` and installs it through bundletool.

For the emulator's settings, `android.avd_configure` changes a stopped emulator:

```sh
tomaccoctl call android.avd_configure '{"name":"TomaccoQA","gpu":"host","vulkan":"off","memory_mb":4096,"cores":4}'
tomaccoctl call android.avd_start '{"name":"TomaccoQA","headless":true}'
```

## Swift on Android

Full guide: [Swift on Android](android.md#swift-on-android). The tested toolchain is Swift 6.4 and NDK 30. Wait for
`swift.android_configure` before you build.

```sh
tomaccoctl call settings.configure '{"android_ndk":"/absolute/Android/Sdk/ndk/30.0.16248370","swift_android_sdk":"swift-6.4.0-RELEASE_android"}'
tomaccoctl call swift.android_configure '{}'
tomaccoctl call project.create '{"path":"/absolute/SwiftCounter","kind":"swift-android","name":"SwiftCounter","organization":"dev.example"}'
tomaccoctl call project.execute '{"path":"/absolute/SwiftCounter","platform":"android","action":"build"}'
tomaccoctl call swift.execute '{"path":"/absolute/SwiftCounter/Swift","platform":"linux","action":"test"}'
tomaccoctl call project.run '{"path":"/absolute/SwiftCounter","platform":"android","device":"emulator-5554"}'
```

## iPhone over USB

Full guide: [iPhone](iphone.md). **A physical iPhone has not been qualified.** Start with `ios.doctor`. Connect
`runtime.start` before `project.run`, so the runner does not take the foreground afterwards.

```sh
tomaccoctl call ios.prepare '{"action":"login"}'
tomaccoctl call ios.sdk_install '{"path":"/absolute/Xcode.xip"}'
tomaccoctl call project.create '{"path":"/absolute/MyiOSApp","kind":"xtool","name":"MyiOSApp","organization":"dev.example"}'
tomaccoctl call project.execute '{"path":"/absolute/MyiOSApp","platform":"ios","format":"ipa","sign":true}'
tomaccoctl call runtime.start '{"platform":"ios","device":"ACTUAL_UDID","runner_bundle_id":"INSTALLED_RUNNER_ID"}'
tomaccoctl call project.run '{"path":"/absolute/MyiOSApp","platform":"ios","device":"ACTUAL_UDID"}'
```

This route builds xtool's SwiftPM format; `project.import_xcode` writes it for an `.xcodeproj`. Tomacco neither ships nor downloads the SDK.
Read Xcode's license first.

### Xcode projects, Flutter, crash reports and the App Store

Full guide, with what was run for real and what was not:
[iPhone](iphone.md#flutter-xcode-projects-crash-reports-and-the-app-store). These call a pinned commit of Joshua Warren's
omarchy-apple-dev, which nothing fetches until `ios.toolkit_install` is called. `ios.doctor` reports its state under
`linux_toolkit`, and each thing in `missing` has a `remedy`. Nothing runs sudo or downloads from Apple.

```sh
tomaccoctl call ios.toolkit_install '{}'
tomaccoctl call project.import_xcode '{"project":"/absolute/MyApp.xcodeproj"}'
tomaccoctl call ios.crash_symbolicate '{"report":"/absolute/crash.ips","binaries":["/absolute/MyApp.app/MyApp"]}'
tomaccoctl call ios.toolkit_install '{"toolchain":true,"xcode_xip":"/absolute/Xcode.xip"}'
tomaccoctl call ios.store_ship '{"path":"/absolute/MyiOSApp"}'
tomaccoctl call ios.flutter_setup '{}'
tomaccoctl call ios.flutter_build '{"path":"/absolute/flutter_app"}'
```

- `project.import_xcode` returns at once with `adapter`, `warnings` and the `next` call. Tell the user about the warnings: each is
  something the Xcode project does that the adapter does not.
- `ios.toolkit_install` with `toolchain` takes minutes and about 15 GB, and without `xcode_xip` succeeds with `sdk_ready:false` in its
  report. Do not fetch Xcode for the user. An SDK from `ios.sdk_install` does not count for this private toolchain.
- **`ios.flutter_setup` and `ios.flutter_build` are untested in Tomacco:** they ran only against fakes. Setup downloads about 7 GB and changes
  one file in the Flutter install, so ask first.
- `ios.store_ship` with `upload` publishes to the user's App Store Connect account, so ask first. A live upload has not been qualified.

## A Mac over SSH

Full guide: [the Mac route](iphone.md#the-mac-route). For Xcode projects, Apple's Simulator, Flutter iOS and store
validation, with a Mac you supply. Typed operations are `apple.devices`, `apple.project`, `apple.build`, `apple.test`,
`apple.archive`, `apple.export`, `apple.simulator`, `apple.device`, `apple.profile` and `apple.store`.

```sh
tomaccoctl call remote.configure '{"name":"mac","host":"my-ssh-mac","directory":"/Users/me/Projects/MyApp"}'
tomaccoctl call remote.check '{"name":"mac"}'
tomaccoctl call remote.sync '{"name":"mac","path":"/absolute/MyApp"}'
tomaccoctl call remote.run '{"name":"mac","argv":["xcodebuild","-list"]}'
```

## The virtual iOS device

Experimental. Full guides: [what it is](virtual-ios/README.md), [setup](virtual-ios/setup.md),
[usage](virtual-ios/usage.md). It needs the user's own restore images and an Image4 boot ticket (IM4M).

**No profile yet?** `python3 tools/ios_virtual_profile.py --check` lists every input of a start that is missing or
wrong, in chain order, each with the command that makes it. The JSON on stdout has them under `missing` (`input`, `flag`,
`path`, `problem`, `make`). With everything present, `--directory /absolute/profile` plus `--cryptex`, `--boot-inputs`,
`--kernel`, `--tools` and `--install` writes `profile.json` and `state.json`, never over an existing profile, and returns
the `ios.virtual.configure` call to make next. **The user supplies the restore images, already decrypted, and one IM4M. No
tool makes those. Stop and tell the user when the IM4M is what is missing, rather than working around it.**

**Driving it.** `ios.virtual.status` reports the shared state; `ios.virtual.start` and `ios.virtual.stop` are durable jobs. See
[start and stop](virtual-ios/usage.md#start-and-stop).

- `display_visible:false` means iOS took the launch but every frame stayed black. That is a display defect, not a working phone.
- `ios.emulator.control` is input and frames, on the `guest_job` of `ios.virtual.status`. Poll it with `action:"frame"` and
  `transport:"file"` to watch the guest; see [touch and gestures](virtual-ios/usage.md#touch-and-gestures).
- `ios.virtual.screenshot {path, frame}` saves the screen as a PNG, with `frame` `rounded` or `device` for round corners or a
  plain bezel. `ios.virtual.record {path, seconds, fps}` is a job that records it to an MP4 with ffmpeg; see
  [screenshots and recordings](virtual-ios/usage.md#screenshots-and-recordings).
- `ios.virtual.configure {model}` selects the iPhone for the next start. `ios.virtual.status` has the ids, and also reports
  the current run's `running_panel` as `WIDTHxHEIGHT@HZ:WIDTH_MMxHEIGHT_MM`.
- `ios.virtual.status` reports the time-zone outcome as `time_zone`, and `installed_applications` (bundle identifier to the
  cdhash on the current run's volume) next to `applications`, the profile's own bundles.
- During a start, `phase_elapsed_seconds`, `phases` and, while `preparing`, `copy_progress` show where it is. `start_disk.enough`
  false means the next start will be refused with `ios_virtual_disk_space`; its remedy says how much to free.
- A window for the phone: `preview.configure` with `ios_guest:{detached:true}`. Asked while no guest is running, the request is
  kept for ten minutes. `workspace.ios_research.guest.detach_pending` in the returned state tells whether it is still waiting.

**Guest operations.** `ios.emulator.guest {job_id, operation, argument}`, with `job_id` being the `guest_job`. They are `inspect`,
`status`, `lookup`, `cancel`, `install`, `register`, `open`, `logs` (`development`, `quiet`, `buffer_texture` and the owned app's
logs), `setup`, `setup_finish`, `restart_ui`, `flush`, `effects`, `screen_state`, `display_state`, `foreground_state`,
`display_refresh`, `graphics_state`, `surface_state`, `crash_reports`, `persistence_probe`, `watchdog_scale`, `icon_cache`, `time_zone`,
`auto_lock`, `receive_bundle`, `policy_add`, `debug_server`, `network_state`, `hardware_probe`, `gestalt`, `developer_mode` and
`keyboard_layout`. `tomaccoctl schema` has each argument. Some that need more words:

- `crash_reports` takes any report-name prefix, so a developer's app's reports are read like iOS's, and tomaccoctl projects
  `crash_report_identities` from the report's own process name. A run's reports reach the next run's disk only when that run became
  the base, so read them during the run.
- `logs quiet` reads tomacco-quiet's record: the services it unloaded, its locationd guard's questions and the processes it ended.
- `time_zone Area/City` sets the guest's zone through iOS's own tzlink service, and answers `previous_link` and `link`.
- `auto_lock never` (or `original`) sets Settings' Auto-Lock and reads it back. The start sends it by itself to a guest that reports
  `auto_lock_supported`.
- `network_state` lists the interfaces' IPv4 addresses and tries one name lookup; `hardware_probe` counts an IOKit class's services;
  `gestalt` reads one MobileGestalt key; `developer_mode status`/`enable` reads or arms iOS's Developer Mode; `keyboard_layout` reports
  the guest's language. `receive_bundle`, `policy_add` and `debug_server` are the guest side of `ios.virtual.deploy` and
  `ios.virtual.debug`; use those operations, not these directly.

The run's evidence (`userspace_failures`, `respawn_storms`) lists a locationd the guard ended as kind `guard-ended`, an expected
entry. A widget extension ended by chronod's deadline is accounted only for iOS's own extensions, and Settings' or a developer's app's
SIGTRAP stays unaccounted, so a clean reading of a run is not mistaken for a clean session.

### Deploying an app to the virtual iOS device

Two things decide before the boot whether an executable runs in this guest: its code-directory hash must be in the trust cache the guest
booted with, and installd admits an ad-hoc signed app only through the development-signing adapter's policy. Both belong to the guest-tools
build, so a developer's app is staged into that build and installed by the start. The rules for a bundle are in
[usage](virtual-ios/usage.md#deploy-your-own-app); every refusal names the file and the reason.

A SwiftUI app to run there comes from one job. `project.app` then names the bundle, says whether the rules below accept it, and returns the
calls that follow:

```sh
tomaccoctl call project.create '{"path":"/absolute/MyApp","kind":"xtool","name":"MyApp","organization":"dev.example","build":true}'
tomaccoctl call project.app '{"path":"/absolute/MyApp"}'
```

**On a phone that is already running, one call installs and opens the app without a reboot:**

```sh
tomaccoctl call ios.virtual.deploy '{"bundle":"/absolute/MyApp.app"}'
tomaccoctl call job.logs '{"job_id":"DEPLOY_JOB","offset":0}'
```

It signs a copy, exchanges spare trust-cache entries of the boot for its hashes while the guest is briefly stopped, sends the bundle over
the guest channel, names it to the signing adapter, installs it through installd and opens it (about 25 s). The profile lists the app
afterwards, so the next start carries it too. `ios.virtual.configure` with `add_application` and `deploy` does the add and the deploy in one
call. A boot has 512 spare entries and the adapter's per-boot table holds 32 apps, frameworks and extensions. To debug the app, build it
with `get-task-allow` and run `ios.virtual.debug {target}` — but iOS refuses it with Developer Mode off and arming it did not hold across a boot, so
lldb cannot attach yet.

Otherwise, usually `add_application`, `application` and `ios.virtual.start` are enough, because the start builds what it needs. The sequence
with the optional build by hand:

```sh
tomaccoctl call ios.virtual.configure '{"add_application":"/absolute/MyApp.app"}'
tomaccoctl call ios.emulator.build_guest_tools '{"trustcache":"/absolute/system.tc","output":"/absolute/guest-tools-new","linker":"/absolute/aarch64-apple-darwin-ld","ldid":"/absolute/ldid","clang":"/absolute/clang-22.1.8/clang","minimum_ios":"27.0","agent":true,"application_probe":true,"development_signing":true,"quiet_services":true,"applications":[{"bundle":"/absolute/MyApp.app"}]}'
tomaccoctl call job.wait '{"job_id":"BUILD_JOB","seconds":60}'
tomaccoctl call ios.emulator.report '{"job_id":"BUILD_JOB"}'
tomaccoctl call ios.virtual.configure '{"application":"dev.example.MyApp"}'
tomaccoctl call ios.virtual.start '{}'
tomaccoctl call job.logs '{"job_id":"START_JOB","offset":0}'
tomaccoctl call ios.virtual.status '{}'
tomaccoctl call ios.emulator.guest '{"job_id":"GUEST_JOB","operation":"logs","argument":"development"}'
```

1. `ios.virtual.configure {add_application}` inspects the bundle and adds it to the profile. `add_entitlements` names a plist, and
   `remove_application` takes the path or the bundle identifier.
2. Optional, because the start does this by itself: `ios.emulator.build_guest_tools` with `applications` (requires `development_signing`,
   and with it `agent` and `application_probe`) signs each bundle ad hoc, adds its hash to the trust cache and its identity to the
   adapter's table. Repeat `job.wait` until terminal, then read `ios.emulator.report`. Name the finished build as `tools` in `profile.json`:
   the start installs the build the profile names.
3. `ios.virtual.configure {application}` chooses the app the start opens, by bundle identifier. Without it the built-in diagnostic app opens.
4. `ios.virtual.start` inspects each bundle again. When the profile's build does not carry one with the same content and entitlements, it
   builds a set that does, before any disk copy, into the profile's `guest-tools` directory, and reuses a set built from the same inputs. A
   kernel that does not pin the new adapter is replaced by a derived one, and `profile.json` names both, with the previous file kept beside
   it. The job log has `guest-tools`, `kernel` and `profile` lines. Clang 22, the linker and `ldid` come from `profile.json` `toolchain`, the
   settings' `executables`, or the build's record; a missing one fails the start and says where it looked. After the guest service answers,
   each app whose hash the base does not record is installed: phase `installing`, one `application` event per bundle, `installed` or
   `installed-before`. An install is never sent twice. installd's refusal ends the start with its reason, and the guest keeps running for
   diagnosis.
5. Read the outcome from `ios.virtual.status`: `phase`, `error`, `application`, `application_launch_accepted`, `application_process`,
   `display_visible` and `installed_applications`. A verified stop carries that record into the base, so a later start installs only an app
   whose signed executable changed.
6. `ios.emulator.guest logs development` shows the adapter's `active` line with `entries`, and an `entry` line per further identifier. This
   tells an inactive signing library from a refused bundle. `crash_reports <executable name>` reads the app's own reports.

Embedded frameworks and libraries have been installed and loaded on the virtual device. App extensions are verified offline only:
`ios.emulator.guest` `lookup` with the app's bundle identifier answers `plugins`, the extensions iOS registered. SwiftUI built by Tomacco's own
route is not supported here.

## Display sessions

`runtime.start`, `runtime.list`, `runtime.state`, `runtime.audio`, `runtime.input`, `runtime.capture`, `runtime.hierarchy` and `runtime.stop` work
on a live device, and `runtime.start` connects Android or a physical iPhone directly on Linux. Passing `remote` selects a Simulator on a configured Mac.
There is one session per device. Repeated calls return it with `reused:true`, and different encoder or runner settings answer
`runtime_configuration_conflict`.

Start with `workspace.context` for the selected project and `runtime.list` for what is connected. `desktop.open` with `tab:"runtime"` lets the
user watch the same app you are controlling.

**Input.** `runtime.input` coordinates run from 0 to 1. Android supports `tap`, `touch` (down, move, up, cancel), `swipe`, `key`, `text` and `rotate`.
The Simulator also supports `fill`, `pinch`, `shake` and `hinge` when its companion does. Apple `text` sends ASCII keys, and `fill` replaces an
accessible field at x/y with any Unicode. Apple rotation needs an explicit orientation. Pass the current decoded image size for mapping after a rotation.

**Evidence.** `runtime.capture` and `runtime.hierarchy` give real app and OS evidence. `device.capture` waits for a screenshot, and
`device.screenshot` is the asynchronous form.

**Sockets.** Most agents should use the operations; the desktop uses these to avoid a process per pointer move.

- `video_socket` emits repeated **u32 big-endian length + payload**. `codec` selects `h264` (Annex-B) or `mjpeg` (one JPEG per payload). It can have
  several consumers, and a slow one is disconnected rather than growing memory.
- `control_socket` takes one JSON line per connection and is owner-only:

  ```json
  {"operation":"input","arguments":{"session":"SESSION_ID","action":"tap","x":0.5,"y":0.5}}
  ```

**Stopping.** `runtime.stop` is idempotent. It releases the session's processes, sockets and forwarding, and leaves the app and device running.

## Windows

`desktop.open` with `device_only:true`, then `preview.configure` with `native:{device, connect:true, detached:true}`, shows a device as its own window.
`preview.state` reports the desktop's mode, native runtime and decoded-frame metadata.

## Files

`files.list`, `files.read`, `files.search`, `files.write` and `files.mkdir` stay within an explicit project root and never execute source text. When
overwriting, pass the digest from `files.read` as `if_match`; a conflict leaves the user's edit intact. The desktop's editor uses this same contract.

## Debugging

`debug.start` starts a durable session: JDWP for Android and Java, Android NDK LLDB, or local LLDB. Then use `debug.breakpoints`, `debug.state`,
`debug.events`, `debug.stack`, `debug.variables`, `debug.evaluate` and `debug.control`, and stop it explicitly. The desktop's Debugger view can take
over the same session.

JDWP evaluation supports field paths, not arbitrary Java. LLDB capabilities depend on the adapter. A Linux Swift debug run does not qualify iOS remote
debugging, and Swift expressions inside an Android process are not qualified.

For Android C/C++, configure `android_ndk` and `lldb-dap` through `settings.configure`, install a **debuggable** APK, then:

```sh
tomaccoctl call debug.start '{"backend":"android-lldb","device":"emulator-5554","bundle_id":"dev.example.app","program":"/work/app/native-symbols/libapp.so","launch":true}'
```

- `program` is the local **unstripped ELF** from the installed build, with the same ABI and build id.
- `symbol_paths` adds library directories, `source_map` relocates source paths, and `pid` picks a process.
- `launch:true` starts a missing app and attaches, and keeps a running one. It does not stop before early native initializers.
- It uses Android's `run-as` gate and needs neither `adb root` nor changed security settings.
- An identical request reuses the session. A different one on the device answers `device_debug_busy`, across JDWP and LLDB; mixed debugging is not
  supported. `debug.control` with `action:"disconnect"` releases it and keeps the app running.

Native debugging is qualified on API 36 x86_64. ARM64 is built but not run on a physical phone.

## UI tests and profiling

`testing.setup` installs a small instrumentation companion for Unicode, emoji and whole-field replacement, without changing the user's keyboard. `testing.run`
runs JSON scenarios on a bounded parallel device matrix, returning JUnit results and capturing failures; `examples/native-navigation.json` shows the format.

`profile.capture` records Android Perfetto traces. Configure the official `trace_processor`, then use `profile.analyze` and `profile.compare`. Metrics that are
not available are null, not zero. A count of frames with jank tags is not the same as visible drops.

## Release

`release.keystore`, `release.sign`, `release.verify` and `release.version` handle Android artifacts. Passwords come from owner-only files, never a string in a call.

`play.prepare` creates an edit, uploads the AAB, sets the track and validates, without committing. Inspect that before an explicitly authorized `play.commit`;
`play.abort` discards it. Live store submission has not been qualified, on Play or on the App Store.

## Updates

`update.check` reports `current`, `latest`, `notes_url`, `update_available` and a `state`: `up_to_date`, `update_available`, `no_release`, `no_package`, `offline`, `error` or `not_checked`.
Without arguments it follows `updates.mode`, using the network at most once a day and never when off. Pass `manual` only when the user asked for a check.

```sh
tomaccoctl call update.status '{}'
tomaccoctl call update.check '{"manual":true}'
tomaccoctl call update.apply '{}'
tomaccoctl call update.rollback '{}'
tomaccoctl call settings.configure '{"updates":{"mode":"off"}}'
```

`update.apply` starts a durable job (download, signature check, install, health check, automatic rollback on failure). It is refused, with the reason in the error, while a job, a device display
session or the virtual iOS device runs, for a source checkout, a system package or an unwritable installation, without Python, and with `updates.mode` off unless `manual` says the user asked.
`update.rollback` restores the version before the last update. `settings.configure` takes `updates.mode` (`off`, `notify`, `auto`) and `updates.token_file`, an owner-only file. `TOMACCO_UPDATES=off`
turns all of it off. After an update, restart `tomaccoctl mcp` to use the new version; the running server keeps working.
From a terminal, `tomaccoctl update` runs the manual check and, when a release applies, `update.apply` and its job to the end.

## Web layouts

The layout lab is a separate Chromium surface, not a device. Start a session with `tomacco --url URL --preview-session NAME`, and discover it with `preview.sessions`.

- `preview.configure` selects a profile, orientation, fold, chrome, safe-area metadata, guides and zoom.
- `preview.ready` waits for the document, fonts and Flutter's first frame. A `wait_for` expression can wait for your own readiness.
- `preview.inspect`, `preview.click`, `preview.fill`, `preview.evaluate`, `preview.console` and `preview.capture` interact with the page.
- `preview.matrix` captures several profiles and orientations and restores the session.
- `preview.audit` reports overflow, missing names and small targets. It is not an accessibility certificate or Safari emulation.
- `catalog.export`, `catalog.import` and `catalog.remove` manage custom profiles, which are data only. `android.calibrate` reads a device's real size and density.

The [desktop guide](desktop.md#visual-layout-lab) has the details.

## Research operations

These lower-level `ios.emulator.*` operations are for research into the guest and the boot chain, not for everyday use. `tomaccoctl schema` has their arguments.
They share an admission lock: identical active requests reuse their job, and conflicting ones answer `ios_emulator_busy`. Do not retry with another state
directory to get around that. A successful process is never a qualification of an app runtime; read the report's own flags.

| Operations | What they are for | Details |
| --- | --- | --- |
| `prepare_userland`, `build_gpu_plugin`, `prepare_restore`, `restore`, `usb_bridge`, `prepare_cryptexes`, `build_guest_tools`, `prepare_guest_tools`, `paravirtual` | The steps of the virtual device's [setup chain](virtual-ios/setup.md#the-chain) | [How it works](virtual-ios/how-it-works.md) |
| `control`, `guest`, `guest_result`, `report` | Input, frames and the guest channel on a running guest, and a job's receipt | [Usage](virtual-ios/usage.md) |
| `inspect`, `probe`, `exec` | The separate restore-shell engine: run own programs in a real iOS restore environment | [Research engines](research-engines.md) |
| `prepare_research`, `research` | Full-system research images on that engine | [Managed runs](research-engines.md#managed-full-system-runs) |
| `prepare_pmp`, `inspect_pmp`, `pmp`, `memory` | Run and inspect PMP controller firmware in an isolated machine | [PMP workflow](research-engines.md#isolated-pmp-firmware-execution) |

`tools/build_ios_native_example.py` builds C and Embedded Swift command-line examples for that engine. Use `desktop.open` with `tab:"ios-research"` to show
the user its dialog.
