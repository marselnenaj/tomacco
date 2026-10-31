# iPhone

Build a Swift app on Linux and run it on an iPhone or iPad over USB. No Mac and no
cloud build host are involved. An optional [Mac route](#the-mac-route) covers Xcode
projects and Apple's Simulator.

**Honest status: a physical iPhone has not been qualified.** The process, HTTP and
socket contracts, the decoding and the setup screens have tests, and the runner
download was really fetched and verified. But no Apple SDK, account login or
iPhone was available during qualification, so compiling, signing and running on a
real phone are all unproven. The same holds for the Mac route: no Mac was
available, so real Simulator use and its latency are unverified. Expect rough edges,
and please report what you find.

## Which route do I need?

| You have | Use |
| --- | --- |
| A new Swift app in xtool's project format | The [Linux route](#from-zero-to-a-running-app) below |
| An Xcode or Flutter iOS project, and no Mac | [Flutter, Xcode projects, crash reports and the App Store](#flutter-xcode-projects-crash-reports-and-the-app-store) |
| An Xcode or Flutter iOS project and a Mac | The [Mac route](#the-mac-route) |
| No iPhone and your own iOS restore image | The experimental [virtual iOS device](virtual-ios/README.md) |

The Linux route compiles [xtool](https://github.com/xtool-org/xtool)'s SwiftPM project
format. `project.import_xcode` writes that format for an `.xcodeproj`, and
`ios.flutter_build` builds a Flutter iOS project on Linux by other means; both need the
toolkit [described below](#flutter-xcode-projects-crash-reports-and-the-app-store).

## Related projects

[omarchy-apple-dev](https://github.com/joshuaswarren/omarchy-apple-dev) and
[omarchy-ios-kit](https://github.com/JonathanInTheClouds/omarchy-ios-kit) are what to
use for SwiftUI, Flutter iOS and Xcode projects on a real iPhone. The second adds hot
reload and VS Code debugging on top of the first. Tomacco ships no code from either, can
fetch a pinned commit of the first on request and call its scripts, and uses nothing
from the second.

## From zero to a running app

**What this needs:** Swift and xtool in matching versions, an Xcode archive from Apple
for the SDK (Xcode itself does not run on Linux), an Apple account, and an iPhone with
Developer Mode on that trusts this computer. Install libimobiledevice, libusbmuxd
(`iproxy`) and usbmuxd through your distribution, and get the
[go-ios](https://github.com/danielpaulus/go-ios) binary. The commands were checked
against go-ios 1.3.2.

The desktop has all of this in one dialog: **Apple → Set up iPhone** (*iPhone
einrichten*) shows what is missing and offers each step. Agents and scripts use the
same operations:

1. **Check what is there.** `ACTUAL_UDID` below is the real device id from `device.list`.

   ```sh
   tomaccoctl call settings.configure '{"executables":{"go-ios":"/absolute/go-ios"}}'
   tomaccoctl call ios.doctor '{"path":"/absolute/MyiOSApp"}'
   tomaccoctl call device.list '{"platform":"ios"}'
   ```

2. **Log in and import the SDK.** The login opens `xtool auth login` in a local
   terminal, and the credentials stay with xtool: nothing of it reaches a job or an
   MCP response. Get the archive from
   [Apple's download page](https://developer.apple.com/download/all/?q=Xcode). An
   extracted `Xcode.app` or a Darwin `.xtoolsdk` directory works too. Match the Swift
   version as [xtool's Linux instructions](https://github.com/xtool-org/xtool/blob/main/Documentation/xtool.docc/Installation-Linux.md)
   describe.

   Tomacco does not ship this SDK and never downloads it for you. Read Xcode's license
   first. See also the [legal notes](legal.md).

   ```sh
   tomaccoctl call ios.prepare '{"action":"login"}'
   tomaccoctl call ios.sdk_install '{"path":"/absolute/Xcode.xip","slim":true}'
   ```

3. **Prepare the phone.** These return durable jobs. Wait for the first two. Keep the
   tunnel job running while you use developer services on iOS 17 and newer;
   `job.cancel` stops it. Some services need a restart after pairing or after the image
   is mounted. Tomacco never runs sudo for you.

   ```sh
   tomaccoctl call ios.prepare '{"action":"pair","device":"ACTUAL_UDID"}'
   tomaccoctl call ios.prepare '{"action":"developer-image","device":"ACTUAL_UDID"}'
   tomaccoctl call ios.prepare '{"action":"tunnel","device":"ACTUAL_UDID"}'
   ```

4. **Install the automation runner.** `ios.wda_prepare` downloads Appium's official
   WebDriverAgent 16.13.6 device package, checks its pinned SHA-256 and signs and
   installs it with your account. WebDriverAgent is the program on the phone that
   provides video, input and accessibility. Signing can rename its bundle id, so read
   the real one from `ios.apps`.

   ```sh
   tomaccoctl call ios.wda_prepare '{"device":"ACTUAL_UDID"}'
   tomaccoctl call ios.apps '{"device":"ACTUAL_UDID"}'
   ```

5. **Create the app and connect the display.** Connect before starting the app, so that
   the runner does not take the foreground afterwards.

   ```sh
   tomaccoctl call project.create '{"path":"/absolute/MyiOSApp","kind":"xtool","name":"MyiOSApp","organization":"dev.example"}'
   tomaccoctl call runtime.start '{"platform":"ios","device":"ACTUAL_UDID","runner_bundle_id":"INSTALLED_RUNNER_ID"}'
   ```

6. **Build, install and launch.** `project.run` builds, signs and installs with xtool,
   reads the installed bundle id back from the device and launches it with go-ios. A
   failed build or install prevents the launch.

   ```sh
   tomaccoctl call project.run '{"path":"/absolute/MyiOSApp","platform":"ios","device":"ACTUAL_UDID"}'
   ```

To only make an IPA, use `project.execute`:

```sh
tomaccoctl call project.execute '{"path":"/absolute/MyiOSApp","platform":"ios","format":"ipa","sign":true}'
```

## Flutter, Xcode projects, crash reports and the App Store

These call a pinned commit of Joshua Warren's MIT-licensed
[omarchy-apple-dev](https://github.com/joshuaswarren/omarchy-apple-dev); the Flutter
pipeline in it is the work of [dl-alexandre](https://github.com/dl-alexandre). Tomacco does
not ship that code, and nothing is fetched until you ask. Its authors' own claims about
what it does are theirs; what follows is what happened here.

| Operation | State on the machine it was written on |
| --- | --- |
| `ios.toolkit_install` | **Run for real** on Arch with Swift 6.4: its recipe without an archive, stopping at the SDK step as described, then the operation with Xcode 27.2. Your own xtool and SDK were unchanged afterwards. |
| `project.import_xcode` | **Run for real** on a one-target SwiftUI fixture; `project.execute` built the adapter into an arm64 `.app` with stock xtool 1.20.1. Real projects were not tried. |
| `ios.crash_symbolicate` | **Run for real** on upstream's synthetic report and Mach-O fixtures. No crash from a device was read. |
| `ios.store_ship` | **Run for real** without a key on a new SwiftUI project with an icon: built, signed with the local TEST identity, and 41 of 41 offline checks passed. The key route and the upload are **tested with fakes only**: no App Store Connect account was used. |
| `ios.flutter_setup`, `ios.flutter_build` | **Untested in Tomacco.** Flutter was not installed, so only fakes ran. Upstream reports Flutter 3.47.6 working. |

**Set it up.** The first call fetches the checkout into `~/.local/share/tomacco/apple-dev`.
That is enough for `project.import_xcode` and `ios.crash_symbolicate`.

```sh
tomaccoctl call ios.toolkit_install '{}'
tomaccoctl call ios.doctor '{}'
```

`ios.doctor` has a `linux_toolkit` section: the commit, what is installed and, under
`missing`, each thing still needed with its remedy. It does not count towards `ready`.

Flutter and App Store builds also need upstream's toolchain, which takes a few minutes
to build and about 15 GB. Name the Xcode archive you downloaded yourself:

```sh
tomaccoctl call ios.toolkit_install '{"toolchain":true,"xcode_xip":"/absolute/Xcode.xip"}'
```

This runs `install-toolchain.sh --mode full --user-only` with `HOME` pointing into Tomacco's
data directory. What that means:

- **It runs no sudo or AUR helper.** Upstream's normal route installs packages with
  `sudo pacman` and `yay`; `--user-only` skips that, and a `sudo` that refuses is first on
  `PATH`. What it would have installed is yours to install, and a missing package stops
  the operation with the command to run. On Arch:
  `sudo pacman -S --needed zip base-devel git libimobiledevice openssl poppler libheif llvm rsync`.
- **It downloads nothing from Apple.** Without `xcode_xip` it stops at the SDK step and
  reports `sdk_ready:false`. It does fetch other things; [privacy](privacy.md) lists them.
- **Upstream's no-Xcode mode is left out on purpose.** That mode can pull Apple's iOS
  update from Apple's servers by itself. No operation selects it.
- **It leaves your xtool, SDK and Swift as they are.** Upstream's script would replace
  `~/.local/bin/xtool` with its fork and, finding an SDK with an older stamp, want it
  rebuilt, and it patches build defaults inside the Swift toolchain it finds. Here it gets
  a private xtool and SDK and a copy of your toolchain (a reflink where the filesystem
  allows). `xtool` and `ios.sdk_install` keep working as before.
- **So there are two SDKs**, and `ios.sdk_install` does not feed the private one: the fork
  refuses its stamp (`epoch=2` from xtool 1.20.1, `epoch=4` required). The same Apple
  license applies to both.

**Xcode project.** This writes `omarchy-xtool/` beside the project, with symlinks to your
sources, and returns what it could not map as `warnings`. Then build it as any xtool
project. CocoaPods, Objective-C sources and run-script phases are among what it warns about.

```sh
tomaccoctl call project.import_xcode '{"project":"/absolute/MyApp.xcodeproj"}'
tomaccoctl call project.execute '{"path":"/absolute/omarchy-xtool","platform":"ios"}'
```

**Crash report.** The result has the exception, the faulting thread's `frames` and
`last_exception_backtrace`. Pass the app's executable to resolve stripped frames; files
match by `LC_UUID`, not by name.

```sh
tomaccoctl call ios.crash_symbolicate '{"report":"/absolute/crash.ips","binaries":["/absolute/MyApp.app/MyApp"]}'
```

**Flutter (untested in Tomacco).** Install Flutter yourself first. `ios.flutter_setup` is needed
once per Flutter version: it downloads about 7 GB from Google's servers, runs prebuilt
tools from there, and replaces `gen_snapshot_arm64` in your Flutter install, keeping the
original beside it as `gen_snapshot_arm64.macos`. The job's `metadata` names the unsigned
`Runner.ipa` and `Runner.app`.

```sh
tomaccoctl call ios.flutter_setup '{}'
tomaccoctl call ios.flutter_build '{"path":"/absolute/flutter_app"}'
```

Upstream's limits: release mode, arm64 devices, plugins with a `Package.swift`. No debug
build, hot reload or simulator, and the result is for iPhones, not the virtual device.

**App Store.** Without a key this checks the whole path with a local TEST identity, which
Apple rejects. With `upload` it sends the IPA to App Store Connect and prints Apple's
processing result. It never submits for review. You create a team key and the app record
once in App Store Connect. A project needs an app icon, or the offline validation fails the
job and names it: an `AppIcon.appiconset` with a 1024x1024 image in `Sources/MyiOSApp/Assets.xcassets`.

```sh
tomaccoctl call ios.store_ship '{"path":"/absolute/MyiOSApp"}'
tomaccoctl call ios.store_ship '{"path":"/absolute/MyiOSApp","upload":true,"key_id":"ABCDE12345","issuer_id":"12345678-1234-1234-1234-123456789abc","key_path":"/absolute/AuthKey_ABCDE12345.p8"}'
```

The `.p8` must be readable only by you, and only its path is passed on; the key ID and
issuer ID appear in the job record, as they do for `apple.store`. See
[privacy](privacy.md#credentials). `flutter/ship.sh` and `ship.sh --device` are not wrapped.

## Reference

**What you get on the phone.** The stream carries the real status bar and app.
WebDriverAgent supports Unicode text and field replacement, tap, swipe, an explicit
orientation, PNG capture and XML accessibility.

**What you do not get.** A gesture runs when you release it; this is not continuous
finger streaming. Pinch, shake, hinge, and hardware or sensor emulation are not
implemented on this route. Video is MJPEG capped at 30 frames per second. There is no
iPhone test-matrix runner.

**How it connects.** The runtime owns a `go-ios runwda` process and `iproxy` forwards
bound to 127.0.0.1, which carry the runner's HTTP and video over USB. No public port is
opened. `runner_mode:external` attaches to a runner already running on that device; the
default is `xctest`, and a plain launch of the runner app is not enough. A per-device
lock prevents two Tomacco sessions from controlling one iPhone.

**Choosing the bundle id.** An ambiguous id at launch is an error. Pass the exact
`bundle_id` from `ios.apps`. `device.install` needs a signed IPA. Device screenshots
and syslog through `device.capture` and `device.logs` use libimobiledevice.

**The desktop.** `preview.configure` with `native:{platform:"ios",apple_target:"device",setup:true}`
shows the setup dialog without implying a connected device.

**Limits of a Linux build.** Device and iOS support and Apple's developer services
remain prerequisites. Swift debugging on Linux does not qualify remote debugging on iOS.

## The Mac route

Use your own or a hosted Mac with Xcode, an installed iOS Simulator runtime and SSH key
access. Tomacco supplies no Mac, SDK or signing account. Builds of general Xcode and
Flutter iOS projects run on the Mac.

**Connect the Mac.** Use an existing SSH host alias. Tomacco does not disable host
verification.

```sh
tomaccoctl call remote.configure '{"name":"mac","host":"my-ssh-mac","directory":"/Users/me/Projects/MyApp"}'
tomaccoctl call remote.check '{"name":"mac"}'
tomaccoctl call remote.sync '{"name":"mac","path":"/absolute/MyApp"}'
tomaccoctl call remote.run '{"name":"mac","argv":["xcodebuild","-list"]}'
```

- `remote.run` takes an argument array, quoted across SSH. It can call `xcodebuild`,
  `xcrun simctl`, tests and exports in the workspace.
- `remote.fetch` retrieves workspace-relative artifacts.
- Sync leaves out common credentials and caches and deletes nothing on the Mac. Check
  your project's own secret file names before choosing the root.

**Live Simulator.** On the Mac, install Meta's companion. Tomacco speaks its gRPC protocol
directly and does not need the Python client.

```sh
brew install facebook/fb/idb
```

Then, with a UUID from the first call:

```sh
tomaccoctl call apple.devices '{"remote":"mac"}'
tomaccoctl call runtime.start '{"platform":"ios","remote":"mac","device":"SIMULATOR_UUID"}'
```

Tomacco boots the Simulator, starts the companion and forwards a private Unix socket through
SSH. Startup can outlast the first 30-second call; a timeout returns a session id for
`runtime.state`. Newer input (orientation, pinch, hinge, accessibility value changes)
needs a companion and Simulator that support it, and an unsupported request returns the
companion's real error. Hinge input cannot make a non-foldable runtime fold.

**Build and test.**

```sh
tomaccoctl call apple.project '{"remote":"mac","project":"MyApp.xcodeproj"}'
tomaccoctl call apple.build '{"remote":"mac","path":"/absolute/MyApp","project":"MyApp.xcodeproj","scheme":"MyApp","destination":"platform=iOS Simulator,id=SIMULATOR_UUID"}'
```

Wait for the job. Its `metadata.products` names the products directory relative to the
remote workspace. Use the `Debug-iphonesimulator/MyApp.app` there with `apple.simulator`
`action=install`, then `action=launch` with the app's bundle id.

- `apple.test` runs XCTest and retrieves an xcresult archive even when tests fail.
- `apple.archive` and `apple.export` produce device distribution artifacts. A Simulator
  `.app` and a signed device IPA are different things.
- `apple.store` validates and uploads through the Mac. A live submission has not been
  qualified, and contract tests are not proof of an upload.

Apple's [Simulator is part of Xcode on macOS](https://developer.apple.com/xcode/system-requirements/).
Projects such as [touchHLE](https://github.com/touchHLE/touchHLE) target early iPhone
applications, not modern SwiftUI.
