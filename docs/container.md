# Container image

A headless image of Tomacco for coding agents, servers and CI. It holds `tomaccoctl` (the JSON
command line and the MCP server) with everything an Android build needs, so an agent can create,
build and test an Android app without installing anything on the host. The image starts
`tomaccoctl`, so `docker run -i --rm IMAGE mcp` is an MCP server on standard input and output.

There is no published image yet. You build it yourself from
[packaging/container/Dockerfile](../packaging/container/Dockerfile). The default image took about
two minutes to build here, the emulator variant about nine more.

## What is in it, and what is not

| In the image | Version |
| --- | --- |
| Ubuntu 24.04, pinned by digest (glibc 2.39) | |
| `tomaccoctl`, from the signed release package | 1.2.1 by default (`TOMACCO_VERSION`) |
| OpenJDK 17 (headless), curl, unzip, OpenSSL, Git, tini | Ubuntu's packages |
| Gradle, checked against its published SHA-256 | 9.3.1 |
| Android SDK command-line tools, checked against Google's SHA-1 and a pinned SHA-256 | 22.0 |
| Android SDK Platform-Tools (`adb`) | the current one at build time (37.0.1 when this was written) |
| Android SDK Platform and Build-Tools | `platforms;android-36`, `build-tools;36.0.0` |
| Only in the `-emulator` variant: the Android Emulator and one system image | the current emulator (37.2.13 when this was written), `system-images;android-36;google_apis;x86_64` |

`/opt/android-sdk/installed-packages.txt` in the image lists the SDK packages with their exact
versions, and `/usr/share/doc/tomacco/` has Tomacco's license and notices.

Not in the image:

- **No desktop.** The Qt app, its live device view and the layout lab need a display; the image
  has only `tomaccoctl`.
- **No virtual iPhone.** The experimental [virtual iOS device](virtual-ios/README.md) needs a
  GPU, an engine build and Apple's files; it is not set up and cannot run here.
- **No iPhone over USB and no iOS builds.** libimobiledevice, usbmuxd, Swift and xtool are not
  installed.
- **No scrcpy, Flutter, NDK, Node.js or bundletool.** Install them in an image of your own built
  `FROM` this one if you need them; `android.sdk_install` can add SDK packages at run time, but
  they are lost with the container unless you keep it.

The image is x86_64 only.

**Why command-line tools 22.0 and not the newest (23.0)?** In 23.0, `sdkmanager` is a wrapper
around Google's new Android CLI, which it fetches when first run and which collects usage data by
default. 22.0 still has the self-contained `sdkmanager`.

## Build it

Building the image installs the Android SDK, which is under Google's
[Android Software Development Kit License Agreement](https://developer.android.com/studio/terms).
**By building it you accept that license.** The build stops unless you say so with
`ACCEPT_ANDROID_SDK_LICENSE=1`:

```sh
docker build --build-arg ACCEPT_ANDROID_SDK_LICENSE=1 -t tomacco-container:1.1.0 packaging/container
docker build --build-arg ACCEPT_ANDROID_SDK_LICENSE=1 --build-arg ANDROID_EMULATOR=1 \
  -t tomacco-container:1.1.0-emulator packaging/container
```

Or with the script, which also checks the result:

```sh
python3 tools/build_container.py build --accept-android-sdk-license
python3 tools/build_container.py build --accept-android-sdk-license --emulator
python3 tools/build_container.py verify tomacco-container:1.1.0
python3 tools/build_container.py verify tomacco-container:1.1.0-emulator --emulator
```

The build downloads the release package with its `.sha256` and `.sig` from GitHub, checks the
checksum and the Ed25519 signature against the release keys (the same keys as `install.sh` and
`tomaccoctl update`), and stops if either does not match. `--build-arg TOMACCO_PACKAGE=portable`
takes `tomaccoctl` from the portable package instead (a 177 MB download for one 13 MB file).
`build_container.py build --packages DIR` uses a package with its checksum and signature from
`DIR` instead of the download, checked the same way; `tools/make_release.py --container` uses
this for the release it is making.

## Run it

Everything Tomacco keeps (settings, the job database and logs, Gradle's caches, emulators and
your projects, if you put them there) goes in `/data`. Give it a named volume to keep it between
runs; without one, each run starts from nothing and Gradle downloads its dependencies again.

```sh
docker run --rm tomacco-container:1.1.0 --version
docker run --rm -v tomacco-data:/data tomacco-container:1.1.0 doctor
docker run --rm -v tomacco-data:/data tomacco-container:1.1.0 call capabilities
```

The image runs as the user `tomacco`, UID 1000. `/data/tomacco` must belong to the user
`tomaccoctl` runs as, so a directory from the host must belong to UID 1000, or run the container
as your own user with a data directory you own:
`--user "$(id -u):$(id -g)" -e HOME=/data/home -v "$PWD/data:/data"`.
Updates are off in the image (`TOMACCO_UPDATES=off`); you get a new version with a new image.

**Long work needs a container that stays up.** Builds, test runs and emulators are durable jobs,
run by worker processes beside the call that started them. When the container's main process
ends, they end too. A single call can follow its job to the end with `--wait`:

```sh
docker run --rm -v tomacco-data:/data tomacco-container:1.1.0 \
  call project.create '{"path":"/data/projects/FirstApp","kind":"compose","name":"FirstApp","organization":"dev.example"}'
docker run --rm -v tomacco-data:/data tomacco-container:1.1.0 \
  call project.execute '{"path":"/data/projects/FirstApp","platform":"android","action":"build","format":"apk"}' --wait
```

For several calls against running jobs, keep one container and use `docker exec`:

```sh
docker run -d --name tomacco -v tomacco-data:/data --entrypoint sleep tomacco-container:1.1.0 infinity
docker exec tomacco tomaccoctl call job.list
docker rm -f tomacco
```

To build a project from the host, mount it into the container and use its path in the container.
Mounting it at the same path keeps the paths in results the same as yours:
`-v "$PWD:$PWD"`.

## Connect a coding agent

The agent starts the container and talks MCP to it over standard input and output, with no
network port. The jobs it starts live as long as that container, which is as long as the agent's
session.

Claude Code:

```sh
claude mcp add --transport stdio --scope user tomacco -- \
  docker run -i --rm -v tomacco-data:/data tomacco-container:1.1.0 mcp
```

Codex:

```sh
codex mcp add tomacco -- docker run -i --rm -v tomacco-data:/data tomacco-container:1.1.0 mcp
```

or by hand in `~/.codex/config.toml`:

```toml
[mcp_servers.tomacco]
command = "docker"
args = ["run", "-i", "--rm", "-v", "tomacco-data:/data", "tomacco-container:1.1.0", "mcp"]
```

Add `"-v", "/home/you/code:/home/you/code"` (or `-v` with your project directory) before the image
name so the agent can build your projects. These are the commands to type; they have been written
from each tool's documented syntax and were not run against a real agent configuration.

`packaging/container/mcp-check.sh [IMAGE]` checks the server from the host: it pipes
`initialize` and `tools/list` into `docker run -i --rm IMAGE mcp` and checks the answer.
`tools/build_container.py mcp IMAGE` does the same in Python.

## The emulator variant

The `-emulator` image runs the Android Emulator headless. It needs KVM: pass `/dev/kvm`, and on a
host where `/dev/kvm` is not open to everyone, its group as well.

```sh
docker run -d --name tomacco --device /dev/kvm --group-add "$(stat -c %g /dev/kvm)" \
  -v tomacco-data:/data --entrypoint sleep tomacco-container:1.1.0-emulator infinity
docker exec tomacco tomaccoctl call android.avd_create '{"name":"Tomacco","system_image":"system-images;android-36;google_apis;x86_64","profile_id":"pixel-11"}' --wait
docker exec tomacco tomaccoctl call android.avd_start '{"name":"Tomacco","headless":true,"gpu":"auto"}'
docker exec tomacco tomaccoctl call device.list '{"platform":"android"}'
```

When `device.list` shows `emulator-5554` with `"booted": true`, run the app and look at it:

```sh
docker exec tomacco tomaccoctl call project.run '{"path":"/data/projects/FirstApp","platform":"android","device":"emulator-5554"}' --wait
docker exec tomacco tomaccoctl call device.capture '{"platform":"android","device":"emulator-5554","path":"/data/first.png"}'
docker exec tomacco tomaccoctl call android.emulator '{"device":"emulator-5554","action":"stop"}'
```

The emulator draws with its software renderer; there is no GPU in the container. Through MCP,
`device.capture` returns the PNG to the agent inline.

## Phones and emulators outside the container

The container has its own `adb` server, which sees only what the container sees. Two ways to
reach a device outside it; neither has been tried with a phone.

**A USB phone, through the container's own adb.** Give the container the USB bus and stop the
host's adb server first, because only one server can hold the phone:

```sh
adb kill-server   # on the host, if one runs
docker run -d --name tomacco --device /dev/bus/usb -v tomacco-data:/data \
  --entrypoint sleep tomacco-container:1.1.0 infinity
```

The user in the container needs permission to open the phone's device node: the host's udev rules
for Android phones (often a group such as `plugdev` or `adbusers`) apply to the node, so add that
group with `--group-add`. A phone plugged in after the container started may not appear; start the
container again. Accept the debugging prompt on the phone; the key it trusts is kept in
`/data/android`.

**The host's adb server, for a phone or an emulator the host already sees.** Point the container's
`adb` at it and share the host's network:

```sh
docker run --rm --network host -e ADB_SERVER_SOCKET=tcp:127.0.0.1:5037 \
  tomacco-container:1.1.0 call device.list '{"platform":"android"}'
```

The host's server must listen on 127.0.0.1:5037 (the default). The two `adb` programs must speak
the same protocol: an `adb` client that finds a server of another version stops it and starts its
own. The image's Platform-Tools 37.0.1 speak version 41. With this setup, `device.list` in the
container reached the host's server and listed what it had, which was no device at the time. Builds
and installs then go through the host's server; files such as screenshots are written in the
container.

## Checks that were run

On an x86_64 Arch Linux host with Docker 29.9, for the images built from 1.1.0's signed regular
package. Times include container start-up on a busy machine and a cold Gradle cache.

| Check | Default image | Emulator image |
| --- | --- | --- |
| Size on disk (compressed) | 1.76 GB (644 MB) | 9.88 GB (3.03 GB), sharing the default image's layers |
| Build without the license argument | stops at the first step | |
| `tomaccoctl --version` | 1.1.0 | 1.1.0 |
| `doctor` | JDK, Gradle, adb, sdkmanager, build-tools found | the same, and the emulator |
| `call capabilities` | Android build ready | Android build ready, KVM seen |
| MCP `initialize` and `tools/list` over `docker run -i` | 193 tools | 193 tools |
| `project.create` (compose) and `project.execute` build | APK in 51 s and 75 s (two runs) | APK in 111 s |
| `android.avd_create`, headless `android.avd_start`, boot | | 46 s to `booted` |
| `project.run --wait` | | succeeded in 10 to 18 s |
| `device.capture` | | the starter's counter screen |
| `android.emulator` stop | | the emulator is gone from `device.list` |

`python3 tools/build_container.py verify IMAGE [--emulator]` runs these again.
