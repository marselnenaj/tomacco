# Troubleshooting

Symptom, cause, fix. Error codes are the ones Tomacco really returns. Every `tomaccoctl` error also has a
`message`, and often a `remedy`: read those first. For the virtual iOS device, see
[its own guide](virtual-ios/troubleshooting.md).

**Start here.** This shows which tools Tomacco can see, with a remedy for each missing one:

```sh
tomaccoctl doctor
```

## Install and start

| Symptom | Cause | Fix |
| --- | --- | --- |
| `tomaccoctl: command not found` | `~/.local/bin` is not on `PATH`, or Tomacco is not installed. | Add it to `PATH`, or [install](getting-started.md#1-install). |
| `desktop_missing` | `desktop.open` found no desktop. | Build with CMake and run `python3 install.py`. |
| The desktop does not open; `doctor` has a `desktop` next step, or its `desktop` entry has `"loadable": false` | The system's dynamic loader cannot load it: `missing_libraries` and `missing_versions` say what is missing (`doctor` runs `ldd` on the desktop). The release desktop needs Qt 6.12 or newer, FFmpeg 9 (`libavcodec.so.63`) and glibc 2.39; the portable package (`"portable": true`) needs glibc 2.35 and the system libraries in its `PORTABLE.md`, and for it `doctor` also checks the X11 and Wayland plugins and WebEngine's helper. On Debian, Ubuntu, Fedora and their derivatives, `install.command` is the `apt-get` or `dnf` command that installs the missing libraries, and the `desktop` next step and `tomaccoctl setup` show it. | Run that command. Otherwise update the system, install the portable package, or [build from a checkout](getting-started.md#debian-ubuntu-and-fedora) on it. `tomaccoctl` works without the desktop. |
| `preview_unavailable`: "Cannot reach preview session" | No desktop session has that name. | Start `tomacco --preview-session NAME`. |
| The device window has a border or blur on Omarchy | The Hyprland rule is missing. | Install with `--omarchy`. It is `~/.config/hypr/tomacco.lua`. |
| A changed installed file is left after an uninstall | Files you changed are kept and listed, on purpose. | Delete them if you do not need them. |

## Tools and SDKs

| Symptom | Cause | Fix |
| --- | --- | --- |
| `tool_missing`: "X is not installed/configured" | Tomacco cannot find that tool. | Install it, or name it with `settings.configure` `executables`. The remedy says which. |
| `tool_missing`: "Install rsync" | The Mac route uses rsync. | Install rsync. |
| `tool_missing`: "Install unzip to extract the verified iPhone runner" | `ios.wda_prepare` needs unzip. | Install unzip. |
| `sdk_missing`: "Configure the Android SDK first" | No SDK path is set. | `settings.configure` `android_sdk`. |
| `sdk_platform_missing` | The SDK has no platform for the starter. | `android.sdk_install` with a `platforms;android-NN` package. |
| `ndk_missing` | Swift on Android or native debugging needs an NDK. | `settings.configure` `android_ndk`. |
| A Swift Android build cannot find its sysroot | The SDK is not linked to the NDK, or one was moved. | Run `swift.android_configure` and wait for it. |

## Projects and jobs

| Symptom | Cause | Fix |
| --- | --- | --- |
| `project_unrecognized` | No Flutter, Gradle, Swift, xtool, web or Xcode project here. | Open the right folder, or add `tomacco.json` tasks. |
| `invalid_argument` | The arguments do not match the schema. Unknown keys are refused. | Check `tomaccoctl schema`. For instance, `job.wait` `seconds` is at most 60. |
| `operation_not_found` | No such operation. | Check `tomaccoctl schema`. Do not guess names. |
| A job says `running` | This is a valid result, not success. | Repeat `job.wait` until it is `succeeded`, `failed`, `cancelled` or `timed_out`. |
| `download_failed` while setting up the Android command-line tools or the iPhone runner | The connection broke or stalled. The part already downloaded is kept in `downloads/` in the data directory. | Run it again: the download continues with a range request, and the file is used only after its checksum matches. Parts untouched for a week are deleted. |
| A job stays `queued` with `queued_reason` | As many builds or emulators as the limit allows already run; the reason names their jobs. | Wait, cancel one of them, or raise the limit with `settings.configure` `{"jobs":{"max_builds":3}}`. `0` removes it. |
| `invalid_path`: "The destination is a symbolic link" | A screenshot, recording, trace, report or export would be written through a link. | Name the file itself, or remove the link. A folder in the path may be a link. |
| `workspace_busy` | Other work holds this project. The error has the active job. | Wait for it or `job.cancel` it. Use separate directories for independent work. |
| `job_not_found` | The id is wrong, or from another `TOMACCO_HOME`. | Use the same data directory. `job.list` shows what exists. |
| A job vanished with a sandboxed agent | A process supervisor can kill workers, even though jobs persist. | Run outside that sandbox, and read `job.get` for the recorded state. |
| An output file exists after a failed build | It is the old one. | Check `modified_in_job` and the job's state. |

## Android

| Symptom | Cause | Fix |
| --- | --- | --- |
| `avd_not_found`: "Unknown AVD" | No emulator has that name. | `android.avds` or `android.avd_create`. |
| "Only Tomacco managed AVDs can be calibrated" | `android.calibrate` works on Tomacco's own emulators. | Create one with `android.avd_create`. |
| `android.avd_profile` or `android.avd_configure` refuses | That emulator is running. | Stop it first with `android.emulator` `action:"stop"`. |
| `scrcpy_server_missing` | The live view needs the scrcpy 4.1 server. | Install scrcpy 4.1, or pass the server JAR to `runtime.start`. |
| `runtime_configuration_conflict` | A session exists with other settings. | Stop it first, then start again. |
| The picture works but there is no sound | Audio failures leave video connected. Android 11 must be unlocked when capture starts, and apps may restrict capture. | Read `runtime.state` `audio` and the job log. |
| `enabled` is true but it is silent | `enabled` only means that forwarding is active. | Check that the app is making sound, and the Linux output device. |
| Unicode text does not arrive | `android.input` text is ASCII only. | `testing.setup`, then `testing.action` with `fill`. |
| `app_not_debuggable` | Android refused `run-as`. | Install a debuggable build for the current primary user. |
| `lldb_server_missing` | The NDK has no `lldb-server` for this ABI. | Use an NDK that has it. |
| `device_debug_busy` | Another debug session owns the device. The error names it. | `debug.control` with `action:"disconnect"`. |
| Native symbols do not match | `program` must be the unstripped ELF with the same ABI and build id. | Point it at the matching build's library. |

## iPhone

**A physical iPhone has not been qualified**, so this list is short. Please report what you hit. See the [iPhone guide](iphone.md).

| Symptom | Cause | Fix |
| --- | --- | --- |
| `terminal_missing` | No terminal could be opened for the login. | Run `xtool auth login` in your own terminal. |
| An Xcode or Flutter iOS project does not build on Linux | `project.execute` builds xtool's format only. | `project.import_xcode` or `ios.flutter_build`, or [another route](iphone.md#which-route-do-i-need). |
| `toolkit_missing`, `toolkit_toolchain_missing`, `toolkit_sdk_missing` | The pinned omarchy-apple-dev checkout, its private toolchain or its SDK is not installed. An SDK from `ios.sdk_install` does not count. | `ios.toolkit_install`; `ios.doctor` lists what is missing under `linux_toolkit`. |
| `device_busy`: "This iPhone already has an active native session" | One session controls one iPhone. | `runtime.list`, then `runtime.stop`. |
| Launch fails with an ambiguous id | Signing can change the bundle id. | Pass the exact `bundle_id` from `ios.apps`. |
| The runner takes the foreground | It was connected after the app started. | Run `runtime.start` before `project.run`. |
| Developer services fail on iOS 17 or newer | They need the tunnel job, and some need a restart after pairing or image mounting. | Keep `ios.prepare` `action:"tunnel"` running, and read the job's real errors. |
| Device install refuses a file | `device.install` needs a signed IPA. | Build with `"format":"ipa","sign":true`. |

## The Mac route

| Symptom | Cause | Fix |
| --- | --- | --- |
| `remote_not_found` | No Mac with that name is configured. | `remote.configure`, then `remote.check`. |
| `runtime.start` times out at 30 seconds | Startup can outlast the first call. | The timeout returns a session id. Follow it with `runtime.state`. |
| A newer input fails with a companion error | The companion or Simulator does not support it. | Update them. That is the companion's real error. |

## Web layout lab

| Symptom | Cause | Fix |
| --- | --- | --- |
| `--capture` looks wrong under the offscreen renderer | It does not draw the WebEngine layer mask correctly. | Use a real Wayland session. |
| Safe-area values look wrong in `env()` | The lab does not emulate `env(safe-area-inset-*)`. | Read `window.__tomaccoViewport`; see [app layout metadata](desktop.md#app-layout-metadata). |
| A native app does not run in the lab | The lab is Chromium and runs web pages only. | Use the native workspace. |

## Changing settings by mistake, or tests that collide

To report a bug, `tomaccoctl report` (the `diagnostics.bundle` operation) writes one `.tar.gz` with the
version, a host summary, `doctor`, your settings and the records, timelines and log ends of recent jobs
that failed, timed out or were cancelled. Nothing is sent. Recognised secrets are masked, job environments
keep only variable names and your home directory is shown as `~`, but the masking knows only common
shapes of secrets: read the files before you attach them.

Set `TOMACCO_HOME=/absolute/test-state` to isolate job, toolchain, profile and emulator state. Your normal data is in `~/.local/share/tomacco`,
and desktop preferences are in Qt's normal settings path.

## Reporting a problem

Say what you ran, the JSON error, and what `tomaccoctl doctor` says. Report security problems privately: see [SECURITY.md](../SECURITY.md).
