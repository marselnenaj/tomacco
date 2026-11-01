# Linux distributions and hardware

Tomacco is developed and tested on one Arch Linux (Omarchy) workstation with an AMD graphics
card. This page says what has run elsewhere, what `tomaccoctl doctor` reports about the computer
it runs on, and what is known about other kinds of hosts. Anything not listed as run here has
not been tried.

## What ran where

For 1.2.1, in clean containers on 2026-11-01:

| Image | Installer | `tomaccoctl doctor` and `capabilities` | JDK from the distribution | Android starter build |
| --- | --- | --- | --- | --- |
| `debian:12` | Installed the portable package | Ran | `openjdk-17-jdk-headless` (17.0.21) | Succeeded, APK built |
| `debian:13` | Installed the portable package | Ran | `openjdk-21-jdk-headless` (21.0.13) | Succeeded, APK built |
| `ubuntu:24.04` | Installed the portable package | Ran | `openjdk-21-jdk-headless` (21.0.12.1) | Succeeded, APK built |
| `fedora:44` | Installed the portable package | Ran | `java-25-openjdk-devel` (25.0.4.1) | Succeeded, APK built |
| `opensuse/tumbleweed` | Installed the portable package | Ran | `java-21-openjdk-devel` (21.0.13) | Succeeded, APK built |

How it ran:

- The installer was the one served at `https://tomacco.dev/install.sh`, run as an ordinary user
  (uid 1000). Its downloads of the v1.2.1 release came from a local copy of the GitHub release
  files, and it checked their checksum and signature as always.
- Before it, the container got `curl`, `ca-certificates`, `python3` and `openssl`, the tools the
  installer asks for. A bare `debian:12` lacks `python3`.
- The build was `project.create` with `kind` `android`, then `project.execute` `build` with
  format `apk`. Gradle 9.3.1 was unpacked from gradle.org: Debian's and Ubuntu's `gradle` package
  is 4.4.1, and Fedora 44 and Tumbleweed have none, while the starter's Android Gradle plugin 9
  needs Gradle 9. The Android SDK platform 36 and build tools 36.0.0 were copied in instead of
  downloaded with `tomaccoctl setup`. Gradle's dependency cache was shared between the runs, so
  only the first one downloaded the plugin.
- On Tumbleweed, `javac` on `PATH` is a libalternatives wrapper; `java_home` has to be the real
  JDK directory, here `/usr/lib64/jvm/java-21-openjdk-21`.
- The containers had no `/dev/kvm`, no display and none of the desktop's system libraries. The
  emulator and the desktop were not started; `doctor` listed the missing libraries with the
  `apt-get` or `dnf` command that installs them.

Earlier, `tools/build_portable.py verify` ran the portable desktop offscreen and on Xvfb in
Debian 12, Ubuntu 22.04, Ubuntu 24.04 and Fedora 44 containers; see
[Getting started](getting-started.md#debian-ubuntu-and-fedora). No real desktop of these
distributions has been tried.

## What doctor says about the computer

`tomaccoctl doctor` has a `host` entry:

| Key | What it holds |
| --- | --- |
| `distribution` | `id`, `name`, `version`, `family` (arch, debian, fedora, suse, nixos or other), `immutable` (`steamos`, `ostree`, `nixos`, `read-only-usr` or null) and `container` (toolbox, distrobox, podman, docker or null) |
| `kvm` | `status`, a `message` and the `commands` that fix it on this system; also copied into doctor's `kvm` |
| `emulator_gpu` | The graphics cards and drivers found, the session (wayland, x11 or none), whether the emulator's window can open (`window`), the `recommended` renderer with its `reason`, and `notes` |
| `resources` | Memory, processors and free space in the home directory, the smaller emulator settings a small computer gets, and whether the virtual iPhone fits |
| `install` | The command that installs a JDK here, and where Gradle 9 comes from |

`next_steps` and `tomaccoctl setup` use it: the JDK step names this distribution's package
instead of Arch's, and a KVM step with these commands appears while `/dev/kvm` is not usable.

The verdicts are tested with made-up machines in `native/host.rs`. On the development host
(Omarchy, Intel and AMD graphics, Wayland with XWayland) `doctor` reported KVM ready and the
`auto` renderer.

## KVM

The Android emulator needs `/dev/kvm` to run at a usable speed. `kvm.status` says why it is not
usable:

| `status` | Meaning | Commands it gives |
| --- | --- | --- |
| `ready` | `/dev/kvm` opens. In a virtual machine (`virtual_machine` true) the emulator runs on nested virtualization, slower. | None |
| `module_not_loaded` | The processor has VT-x or AMD-V but there is no `/dev/kvm`. | `sudo modprobe kvm_intel` or `kvm_amd` |
| `virtualization_off` | The processor does not report VT-x or AMD-V. | Turn it on in the firmware settings |
| `nested_virtualization_off` | A virtual machine whose processor offers no VT-x or AMD-V. | libvirt `host-passthrough`, VirtualBox `--nested-hw-virt on`, Hyper-V `ExposeVirtualizationExtensions` |
| `not_in_container` | A Docker or Podman container started without the device. | `docker run --device /dev/kvm ...` |
| `not_in_group` | `/dev/kvm` belongs to a group you are not in. | `sudo usermod -aG kvm USER`, then log in again; per system, below |
| `log_in_again` | You were added to the group after this session started. | Log out and in, or `newgrp kvm` for one terminal |
| `no_group_access` | `/dev/kvm` is owned by root only. | A udev rule giving the kvm group access, then the group |

For `not_in_group`, on Fedora's ostree systems (Silverblue, Kinoite, Bazzite) the kvm group is
only in `/usr/lib/group`, where `usermod` cannot change it, so the commands first copy its line
into `/etc/group`. On NixOS they name `users.users.USER.extraGroups` in `configuration.nix`
and `nixos-rebuild switch`. On SteamOS they say to set a password with `passwd` first, which
`sudo` needs. None of these commands was run on those systems.

## The emulator's window and renderer

The Android emulator's own Qt has only the X11 (xcb) platform plugin; emulator 37.2.12 ships
`libqxcbAndroidEmu.so` and no Wayland plugin. On the development host it aborted with "Could not
find the Qt platform plugin wayland" when `QT_QPA_PLATFORM` was `wayland`, and aborted when
neither `DISPLAY` nor `WAYLAND_DISPLAY` was set. Now:

- `android.avd_start` without `headless` is refused with `emulator_no_display` when `DISPLAY` is
  not set. On Wayland that means XWayland is off; elsewhere there is no graphical session. The
  remedy is `headless` `true`: `device.capture` and the live view work without the window.
- When `QT_QPA_PLATFORM` names only platforms other than `xcb`, the emulator is given
  `QT_QPA_PLATFORM=xcb`. On the development host, a start from a shell with
  `QT_QPA_PLATFORM=wayland` then booted.

The renderer: an AVD keeps the emulator's `auto` mode unless you chose one. `auto` lets the
emulator test the host's OpenGL and fall back to software rendering by itself. Where `doctor`
finds no hardware graphics driver (no card under `/sys/class/drm`, only a virtual machine's
display adapter, or no render node in `/dev/dri` without NVIDIA's driver), a start with `gpu`
`configured` passes `-gpu swiftshader`, and the start's result has `gpu` with `source` `host`
and the reason. The modes emulator 37.2.12 lists are `auto`, `host`, `software`, `lavapipe`,
`swiftshader` and `swangle`; older guides' `swiftshader_indirect` is not among them.

To choose yourself, pass `gpu` to `android.avd_start` for one start, or save it with
`android.avd_configure`; a saved choice is kept and never replaced by the host's. Only the AMD
development host has run the emulator; NVIDIA, Intel-only and virtual machine graphics have not.

## Small computers

| Host | New emulators get |
| --- | --- |
| Less than 6 GiB of memory | 1536 MB of memory |
| 6 to 10 GiB of memory | 2048 MB of memory |
| At most 4 processors | Half of them, at least one |

`android.avd_profile`, which `android.avd_create` runs, lowers the emulator's memory and
processors to these and lists what it changed in `adapted_to_host`. Values set with
`android.avd_configure` are kept. `doctor`'s `resources.emulator.notes` say so, and also warn
under 4 GiB of memory and with less than 16 GiB free in the home directory: the SDK download is
about 5 GB, and an emulator's data partition may grow to 10 GB.

`ios.virtual.start` is refused with `ios_virtual_host_too_small` on a host that is not x86_64 or
has less than 12 GiB of memory: the guest takes 8 GiB and its translated code up to 2 GiB more.
[`ios.virtual.host_check`](virtual-ios/README.md#what-it-needs) gives the full picture.

## Immutable and atomic systems

### Fedora Silverblue, Kinoite and Bazzite

There is no pacman, so the installer takes the portable package and installs it into
`~/.local`, as on any other distribution. It needs `python3` and `openssl` on the host; whether a
given image has them was not checked. Development tools belong in a toolbox or distrobox
container, and `doctor`'s JDK advice says so: `toolbox create`, `toolbox enter`, then
`sudo dnf install java-25-openjdk-devel` on Fedora 44. The container shares your home directory,
so `~/.local/bin/tomaccoctl` is there inside it too, beside the JDK. The `fedora:44` result
above is what such a container would do; a real toolbox has not been tried, and neither has the
emulator from inside one.

### NixOS

Tomacco's programs, like the Android SDK's own (the emulator, and `aapt2`, which Gradle
downloads), are linked for the standard loader `/lib64/ld-linux-x86-64.so.2`, which NixOS does
not have. In a `nixos/nix` container, which has no such loader either, the 1.2.1 installer
completed, but `tomaccoctl` could not start: "cannot execute: required file not found". A change
to the installer, not yet on tomacco.dev, makes it stop before downloading and say why. NixOS's `programs.nix-ld.enable`, or
running Tomacco in a distrobox container of another distribution, may work around it; neither has
been tried. `doctor` gives NixOS-style commands for the JDK (`nix-shell -p jdk21`) and the kvm
group.

### SteamOS and the Steam Deck

SteamOS is based on Arch and has pacman, but its root file system is read-only. The 1.2.1
installer takes it for Arch and asks to install Qt and FFmpeg with `sudo pacman`, which cannot
work there. Without a terminal to ask in, it stops. Until the installer on tomacco.dev changes, install the portable package:

```sh
curl -fsSL https://tomacco.dev/install.sh | bash -s -- --portable
```

In an Arch container with SteamOS's `/etc/os-release` and a read-only root, the 1.2.1 installer
stopped at the pacman step and `--portable` installed into the home directory. With the
installer change that is not yet on tomacco.dev, it installed there without the flag: it notices
SteamOS, or any read-only `/` or `/usr`, and takes the portable package. `tomaccoctl --version` and `doctor` ran afterwards. This was not run on a Steam
Deck: its desktop libraries, `/dev/kvm` and graphics are unchecked. A JDK has to go into the home
directory too: unpack one, such as Eclipse Temurin 21, and name it with `settings.configure`
`java_home`. `doctor` says the same. The installer prints the line that puts `~/.local/bin` on
`PATH` when it is missing.

## Containers

The [container image](container.md) is the supported way to run `tomaccoctl` in Docker. Inside
any container, `doctor` reports the container, and a missing `/dev/kvm` as `not_in_container`
with the `--device /dev/kvm` flag.
