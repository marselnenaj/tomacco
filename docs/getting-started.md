# Getting started

This guide takes you from nothing to a running app and a first agent call. Plan
on about ten minutes of your own time, plus any compile and SDK downloads.

**Prefer not to do this yourself?** Give your coding agent [its setup guide](agent-setup.md).

**On a server or in CI, without a desktop?** Build the [container image](container.md): `tomaccoctl` with
a JDK, Gradle and the Android SDK, and optionally the emulator.

## 1. Install

```sh
curl -fsSL https://tomacco.dev/install.sh | bash
```

This finds the latest release, checks its checksum and signature, and installs it for your
user into `~/.local`, adding the bar widget on Omarchy. Use `bash -s -- --no-omarchy` at the
end to leave the widget out. On Arch it asks before installing the system packages with
`sudo pacman`. Elsewhere, install the system packages first; see
[Debian, Ubuntu and Fedora](#debian-ubuntu-and-fedora). On an ARM64 (aarch64) computer it takes the
release's aarch64 package: the regular one on Arch Linux ARM, the portable one elsewhere; see
[ARM64](#arm64).

To read it first: `curl -fsSL https://tomacco.dev/install.sh -o install.sh`.

Once Tomacco is installed, `tomaccoctl verify` makes the same check for a package you download
yourself: its SHA-256 against the `.sha256` beside it, and its Ed25519 signature in the `.sig`
against the release keys built into `tomaccoctl`. It exits 0 only when both match; `--json`
prints the result as JSON.

```sh
tomaccoctl verify tomacco-1.2.1-Linux-x86_64.tar.gz
```

Tomacco runs on x86_64 and aarch64 (ARM64) Linux. x86_64 Arch Linux and Omarchy are the tested
platform. Other distributions should work, but they have not been qualified. On ARM64 Linux, such
as Arch Linux ARM on Apple Silicon (M1, M2, Asahi), the tools and agent API are experimental and the
virtual iOS device is not available.

<details>
<summary>By hand, or from source</summary>

**From a release tarball.** Download the package, its `.sha256` and `.sig` from the
[releases page](https://github.com/marselnenaj/tomacco/releases), then check it (`sha256sum`
checks the checksum only; with an earlier Tomacco installed, `tomaccoctl verify` checks the
signature as well):

```sh
sha256sum -c tomacco-1.2.1-Linux-x86_64.tar.gz.sha256
tar -xzf tomacco-1.2.1-Linux-x86_64.tar.gz
python3 tomacco-1.2.1-Linux-x86_64/install.py --omarchy
```

Outside Arch, take the release's `-portable` package instead; see
[which package](#debian-ubuntu-and-fedora).

**From a checkout.** You also need CMake, Ninja, a C++20 compiler, Rust/Cargo 1.89 or newer,
protobuf and the FFmpeg development libraries. On Arch:

```sh
sudo pacman -S --needed git cmake ninja clang rust python openssl rsync unzip protobuf ffmpeg ttf-dejavu qt6-base qt6-declarative qt6-svg qt6-webengine qt6-tools
cmake -S . -B build-product -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build-product
python3 install.py --binary build-product/tomacco --cli build-product/tomaccoctl --omarchy
```

Leave out `--omarchy` on other desktops. `tomacco` and `tomaccoctl` go in `~/.local/bin`, which
must be on your `PATH`. To upgrade, roll back or uninstall, see [Building](building.md#installing).

</details>

### Debian, Ubuntu and Fedora

Only Arch Linux has been tested as a desktop. The package names below come from each distribution's
package index; installing them and building Tomacco there has not been tried.

**Which package.** A release has two packages:

| Package | Built on | Needs | For |
| --- | --- | --- | --- |
| `tomacco-VERSION-Linux-x86_64.tar.gz` | Arch | The system's Qt 6.12 or newer, FFmpeg 9 (`libavcodec.so.63`), glibc 2.39 | Arch and Omarchy |
| `tomacco-VERSION-Linux-x86_64-portable.tar.gz` | Ubuntu 22.04 | glibc 2.35 or newer; Qt 6.12 and FFmpeg are bundled | Other distributions, such as Debian 12, Ubuntu 22.04 and newer, Fedora |

Releases from 1.1.0 have both; 1.0.0 had only the first. The regular package's desktop will not start
where the distribution's Qt or FFmpeg is older; use the portable package there, or build from a checkout. `tomaccoctl doctor` checks whether the installed desktop loads here: its `desktop` entry lists missing
libraries and symbol versions, and `next_steps` then starts with what to do.

**The portable package** installs like the other one: check it, unpack it, run its `install.py`.
It was about 176 MB when measured on the 1.0.0 sources. It still uses the system's graphics, X11 or Wayland, font, D-Bus, NSS and ALSA
libraries, which desktop installations have; `share/doc/tomacco/PORTABLE.md` in the package lists
them. For the 1.0.0 sources, `tools/build_portable.py verify` ran it in clean containers of Debian 12,
Ubuntu 22.04, Ubuntu 24.04 and Fedora 44: `tomaccoctl --version`, `tomaccoctl doctor`, the desktop's
`--smoke-test` offscreen and on Xvfb, and an installation into a scratch home with `install.py`, all
passed. It has not been run on a real desktop of those distributions. [Building](building.md#packaging)
describes how it is made.

On a bare container image the checks first installed the libraries listed as `APT_PACKAGES` and
`DNF_PACKAGES` in `tools/build_portable.py`; a desktop installation normally has them already.

### ARM64

A release made with the aarch64 builds has two more packages:

| Package | Built on | Needs | For |
| --- | --- | --- | --- |
| `tomacco-VERSION-Linux-aarch64.tar.gz` | Arch Linux ARM | The system's Qt 6.12 or newer, FFmpeg 9 (`libavcodec.so.63`), glibc 2.39 | Arch Linux ARM |
| `tomacco-VERSION-Linux-aarch64-portable.tar.gz` | Ubuntu 24.04 | glibc 2.39 or newer; Qt 6.12 and FFmpeg are bundled | Other distributions, such as Ubuntu 24.04, Debian 13, Fedora 40 and newer |

The aarch64 portable package needs a newer distribution than the x86_64 one: Qt's own aarch64
binaries, which it bundles, are built on Ubuntu 24.04. It does not run on Debian 12 or Ubuntu 22.04;
there the installer stops before installing anything and says which C library version is missing,
and a [build from a checkout](#debian-ubuntu-and-fedora) is the way. Releases up to 1.2.1 have no
aarch64 portable package, and only 1.2.1 has the regular one.

The installer picks the package as on x86_64: on Arch Linux ARM the regular package, falling back to
the portable one when the release has no regular aarch64 package or its desktop's libraries do not
load; elsewhere the portable package. `tomaccoctl update` keeps the kind of package you installed,
and an installation from the regular package on an aarch64 distribution other than Arch Linux ARM
moves to the portable package.

**To build from a checkout** you need Qt 6.9 or newer with Quick, Quick Controls 2, WebEngine Quick,
Network and SVG, the FFmpeg development libraries, OpenSSL, Python 3.11 or newer, CMake, Ninja, a
C++20 compiler, protobuf and Rust/Cargo 1.89 or newer. Distributions with Qt 6.9 or newer:
Debian testing and unstable (Debian 13 has Qt 6.8, too old), Ubuntu 25.10 and newer, and Fedora 43
and newer. If the distribution's `cargo --version` is older than 1.89, install Rust with
[rustup](https://rustup.rs) instead.

On Debian or Ubuntu:

```sh
sudo apt install git cmake ninja-build g++ pkg-config cargo protobuf-compiler python3 openssl ffmpeg rsync unzip fonts-dejavu-core \
  qt6-base-dev qt6-declarative-dev qt6-svg-dev qt6-svg-plugins qt6-webengine-dev libgl-dev \
  libavcodec-dev libavutil-dev libswscale-dev \
  qml6-module-qtquick qml6-module-qtquick-controls qml6-module-qtquick-templates qml6-module-qtquick-layouts \
  qml6-module-qtquick-dialogs qml6-module-qtquick-shapes qml6-module-qtquick-window qml6-module-qtquick-effects \
  qml6-module-qtqml-workerscript qml6-module-qtcore qml6-module-qtwebengine
```

On Fedora:

```sh
sudo dnf install git cmake ninja-build gcc-c++ pkgconf-pkg-config rust cargo protobuf-compiler python3 openssl ffmpeg-free rsync unzip dejavu-sans-fonts \
  qt6-qtbase-devel qt6-qtdeclarative-devel qt6-qtsvg-devel qt6-qtwebengine-devel \
  libavcodec-free-devel libavutil-free-devel libswscale-free-devel
```

Fedora's `ffmpeg-free` is a reduced FFmpeg build. Whether its H.264 decoder is enough for the
Android live view has not been checked; RPM Fusion's `ffmpeg` is the full build. Then build and
install with the three `cmake` and `install.py` commands of the Arch block above.

## 2. Set up your tools

```sh
tomaccoctl setup
```

It lists what this computer can do and what is missing, in plain text. Then it offers to connect
Claude Code and Codex, and to install the Android SDK with an emulator called `Tomacco`. That
download is about 5 GB and accepts Google's Android SDK license, so it asks first. A missing tool
is fine until you need it. For the same check as JSON, use `tomaccoctl doctor`.

## 3. Pick a route

| Route | What it needs | What you get |
| --- | --- | --- |
| [A. Look around](#route-a-look-around) | Only Tomacco | The desktop and the web layout lab with its demo app |
| [B. An existing project](#route-b-an-existing-project) | Only Tomacco to open it; the project's own toolchain to build it | Your project in the workspace, and the exact build commands |
| [C. A new Android app](#route-c-a-new-android-app) | Android SDK command-line tools, a JDK 17 or newer, Gradle, and KVM (`tomaccoctl doctor` reports it) or a phone | A native app running in the real Android Emulator |

Other targets take more setup and are not first-day routes. The
[iPhone route](iphone.md) needs Apple's SDK, an Apple account and an iPhone. The
[virtual iOS device](virtual-ios/README.md) needs your own restore image, a boot
ticket Tomacco does not make for you, and a 16 GiB graphics card.

### Route A: look around

```sh
tomacco
tomacco --device 17
```

The first command opens the native workspace. The second opens the layout lab, a
Chromium view of a web page in a device frame, with an included demo app. The
device ids come from `tomaccoctl call catalog.list`.

### Route B: an existing project

Opening a project never runs its build scripts, so this is safe for any checkout.

```sh
tomaccoctl call project.inspect '{"path":"/absolute/project"}'
tomaccoctl call project.plan '{"path":"/absolute/project","platform":"android"}'
tomacco --project /absolute/project
```

`project.inspect` detects Flutter, Gradle, Swift, xtool, web and Xcode projects.
`project.plan` prints the exact commands a build would run, without running them.
A folder Tomacco does not recognize answers `project_unrecognized`; adding a
`tomacco.json` with tasks makes it a project.

To build, pick the platform your project uses. This one starts a durable job:

```sh
tomaccoctl call project.execute '{"path":"/absolute/project","platform":"android","action":"build"}'
```

Then wait for the job as in [step 3 of route C](#route-c-a-new-android-app).

### Route C: a new Android app

If you said yes to Android in `tomaccoctl setup`, skip to step 4. Its emulator is called `Tomacco`.

**1. Point Tomacco at your tools.** Install the
[Android SDK command-line tools](https://developer.android.com/studio#command-line-tools-only),
a JDK and Gradle (on Arch, `jdk-openjdk` and `gradle`). Unpack the command-line tools to `SDK/cmdline-tools/latest`, because that
is where Tomacco looks for `sdkmanager`. Then tell Tomacco where everything is:

```sh
tomaccoctl call settings.configure '{"android_sdk":"/absolute/Android/Sdk","java_home":"/usr/lib/jvm/default","executables":{"gradle":"/usr/bin/gradle"}}'
```

**2. Install the SDK packages and create an emulator.** Licenses are accepted only
because you say so here. `android.sdk_packages` lists what is available.

```sh
tomaccoctl call android.sdk_install '{"packages":["platform-tools","emulator","platforms;android-36","build-tools;36.0.0","system-images;android-36;google_apis;x86_64"],"accept_licenses":true}'
tomaccoctl call android.avd_create '{"name":"First","system_image":"system-images;android-36;google_apis;x86_64","profile_id":"pixel-11"}'
tomaccoctl call android.avd_start '{"name":"First"}'
```

These are durable jobs. Each call answers with a job whose id is in `result.id`.

**3. Wait for a job.** Queued or running is not success. Repeat `job.wait` until the
state is `succeeded`, `failed`, `cancelled` or `timed_out`; it waits at most 60
seconds per call. An emulator stays running as a job while it is up, so wait for
the install and create jobs, and for the emulator check `device.list`.

```sh
tomaccoctl call job.wait '{"job_id":"JOB_ID","seconds":60}'
tomaccoctl call job.logs '{"job_id":"JOB_ID","offset":0}'
```

**4. Create, build and run the app.** The emulator's name in `device.list` is
usually `emulator-5554`.

```sh
tomaccoctl call device.list '{"platform":"android"}'
tomaccoctl call project.create '{"path":"/absolute/FirstApp","kind":"android","name":"FirstApp","organization":"dev.example"}'
tomaccoctl call project.execute '{"path":"/absolute/FirstApp","platform":"android","action":"build","format":"apk"}'
tomaccoctl call project.run '{"path":"/absolute/FirstApp","platform":"android","device":"emulator-5554"}'
```

**5. See it.** Wait for the run job, then take a screenshot or open the live view.
The live view also needs [scrcpy 4.1](android.md#see-and-control-the-device).

```sh
tomaccoctl call device.capture '{"platform":"android","device":"emulator-5554","path":"/absolute/first.png"}'
tomacco --project /absolute/FirstApp --workspace runtime
```

You have a first success when the screenshot shows the starter's counter app.
[Your first Android app](tutorial-android.md) does the same with the Jetpack Compose starter and
adds a UI test.
The [Android guide](android.md) goes on from here. If a step fails, read the error's
`message` and `remedy`; [Troubleshooting](troubleshooting.md) lists the common ones.

## 4. Make your first agent call

`tomaccoctl` is also a local MCP server. It speaks over standard input and output, with
no network port and no token. `tomaccoctl setup` registers it for you; by hand:

```sh
claude mcp add --transport stdio --scope user tomacco -- "$(command -v tomaccoctl)" mcp
codex mcp add tomacco -- "$(command -v tomaccoctl)" mcp
```

Then ask your agent to call the `doctor` tool and tell you what is missing. It
sees the same operations you just used, with their schemas. The
[agent guide](agents.md) has the contract and a workflow per platform.

## Where next

- [Desktop](desktop.md): navigation, shortcuts, device windows.
- [Agents](agents.md): MCP, jobs and workflows.
- [Android](android.md), [iPhone](iphone.md), [virtual iOS device](virtual-ios/README.md).
