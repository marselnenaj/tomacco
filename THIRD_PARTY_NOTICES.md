# Third-party notices

Tomacco 1.0.0 is free software under the GNU General Public License,
version 2 or (at your option) any later version (`GPL-2.0-or-later`, see
[LICENSE](LICENSE)). This file lists the third-party components that are part
of its source tree, compiled into its binaries, linked by them at run time, or
downloaded and built by its optional recipes, with their licenses and the
notices those licenses require. It is installed as
`share/doc/tomacco/THIRD_PARTY_NOTICES.md`. Trademarks are covered in
[docs/legal.md](docs/legal.md).

The table of Rust crates below was generated from `Cargo.lock` with
`cargo metadata` and the license files shipped in each crate. It covers the
Linux build and leaves out crates used only on Windows, macOS or WebAssembly.
Regenerate it when `Cargo.lock` changes.

## How the licenses combine

- **Source.** Tomacco's own code is `GPL-2.0-or-later`. Every file
  without its own SPDX line falls under that license. Files under
  `integrations/ios-emulator/` that change or extend other projects keep the
  license of the project they change (see [below](#integrationsios-emulator)).
- **`tomaccoctl` (Rust).** The binary statically links the crates listed in the
  table. Almost all are MIT, MIT-or-Apache-2.0 or similarly permissive. `prost`
  and `sync_wrapper` are **Apache-2.0 only**. The Free Software Foundation
  considers Apache-2.0 incompatible with GPL version 2 but compatible with GPL
  version 3. The "or any later version" option of Tomacco's license
  allows that combination: **a distributed `tomaccoctl` binary is conveyed under
  the terms of GPL version 3 (or later)**. The source code remains available
  under `GPL-2.0-or-later`. `tomaccoctl` links only `libc`, `libm` and `libgcc_s`
  dynamically, and runs `openssl`, `curl`, `ssh`, `rsync` and the platform tools
  as separate programs.
- **`tomacco` (Qt desktop).** It links Qt 6 and FFmpeg dynamically. The
  regular CMake package (`cpack`, TGZ) takes both from the system and ships
  neither; the portable package bundles them (see
  [below](#bundled-in-the-portable-package)).
  Qt 6 is offered under LGPL-3.0 and GPL (2.0 or 3.0, depending on the module).
  The FFmpeg libraries are LGPL-2.1-or-later in a default build. A build
  configured with `--enable-gpl` is GPL-2.0-or-later, and with
  `--enable-version3` it is (L)GPL-3.0-or-later; Arch Linux's `ffmpeg` uses
  both flags. Linking either is compatible with Tomacco's license through
  its "or later" option, and a binary linked against such a build is conveyed
  under GPL version 3. FFmpeg built with `--enable-nonfree` cannot be
  redistributed together with GPL software. Do not distribute Tomacco
  linked against such a build.
- **Bundles.** Anyone who ships Tomacco **together with** Qt, Qt
  WebEngine or FFmpeg (an AppImage, Flatpak, container image or similar) takes
  on those libraries' obligations: their license texts and notices,
  corresponding source code or a written offer, and for LGPL libraries the
  ability to replace them. This includes Chromium's notices for Qt WebEngine.
- **Separate programs.** Tools that the optional recipes download, build or run
  (QEMU engines, ldid, BusyBox, ipsw, scrcpy, WebDriverAgent, go-ios, xtool,
  Swift, the Android SDK, Gradle, and so on) are separate programs under their
  own licenses. Tomacco does not redistribute them. Anyone who
  redistributes a built engine must meet that engine's license, see
  [Downloaded and built on request](#downloaded-and-built-on-request).

## Bundled in the portable package

`tools/build_portable.py` makes `tomacco-VERSION-Linux-x86_64-portable.tar.gz`,
which ships these libraries in `lib/tomacco/runtime`, all as shared libraries
a user can replace. The package's `share/doc/tomacco/PORTABLE.md` names the
exact versions and FFmpeg's configuration, and installs with the LGPL texts
(`COPYING.LGPL-2.1`, `COPYING.LGPL-3.0`) beside `COPYING.GPL-3.0`.

| Component | Version and origin | License | Corresponding source |
| --- | --- | --- | --- |
| Qt 6 (the modules in the table below, plus Wayland client and the XCB platform plugin) and ICU 73.2 | 6.12.0, the official Qt binaries for Linux x86_64, unmodified; pinned in `packaging/portable/qt-archives.txt` | LGPL-3.0-only; ICU under the Unicode license (Unicode-DFS-2016) and the terms in its `LICENSE`, installed as `LICENSE.ICU` | <https://download.qt.io/official_releases/qt/6.12/6.12.0/submodules/> |
| Qt WebEngine 6 with Qt WebChannel and Qt Positioning | 6.12.0, as above | LGPL-3.0-only; Chromium and its components under their own licenses | The software bills of materials Qt ships, with each component's license, are copied to `lib/tomacco/runtime/sbom/`; source as above |
| FFmpeg: libavcodec, libavutil, libswscale | 9.0.2 from <https://ffmpeg.org/releases/>, built in `packaging/portable/Dockerfile` with only the H.264 and MJPEG decoders and parsers, without `--enable-gpl`, `--enable-version3`, `--enable-nonfree` or external libraries | LGPL-2.1-or-later | <https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz> (SHA-256 in the Dockerfile) |
| xcb-util libraries: libxcb-cursor, libxcb-icccm, libxcb-image, libxcb-keysyms, libxcb-render-util, libxcb-util | Ubuntu 22.04's packages | MIT/X11 | Ubuntu's source packages |

Everything else the portable desktop loads (glibc, libstdc++, OpenGL, X11,
Wayland, fontconfig, FreeType, GLib, D-Bus, NSS, ALSA, zlib) comes from the
system, as for the regular package.

## In the container image

`packaging/container/Dockerfile` builds an image that is not distributed yet; whoever builds it
gets the components below, and would have to meet their licenses to pass the image on. Tomacco's
own part is `tomaccoctl` from the signed release package, with this file and the license in
`/usr/share/doc/tomacco/`.

| Component | Version and origin | License |
| --- | --- | --- |
| Ubuntu 24.04 base image and its packages: OpenJDK 17, curl, unzip, OpenSSL, Git, tini; in the emulator variant the X11, OpenGL, NSS, PulseAudio, ALSA and libpng runtime libraries | Ubuntu's archive at build time | Each package's own license, in `/usr/share/doc/PACKAGE/copyright` in the image; OpenJDK is GPL-2.0-only with the Classpath Exception |
| Gradle | 9.3.1 from <https://services.gradle.org/distributions/> | Apache-2.0 |
| Android SDK command-line tools, Platform-Tools, Platform 36, Build-Tools 36.0.0; in the emulator variant the Android Emulator and the Google APIs x86_64 system image for API 36 | Google's repository, <https://dl.google.com/android/repository/> | The Android Software Development Kit License Agreement, <https://developer.android.com/studio/terms>, and the notices in each package's `NOTICE.txt`. The build requires `ACCEPT_ANDROID_SDK_LICENSE=1`: whoever builds the image accepts this license. The Google APIs system image contains Google software under the same agreement. |

The Android SDK license restricts redistribution of the SDK. Publishing an image that contains it
needs a check of that license first.

## Libraries linked by the desktop at run time (not bundled)

| Component | Used for | License | Notes |
| --- | --- | --- | --- |
| Qt 6 (Core, Gui, Qml, Quick, Quick Controls 2, Network, Svg, DBus, OpenGL; Test for the test binaries) | Desktop UI | LGPL-3.0-only, GPL-2.0-only or GPL-3.0-only (commercial licenses also exist) | © The Qt Company Ltd. and other contributors. <https://www.qt.io/licensing/> |
| Qt WebEngine 6 (with Qt WebChannel, Qt Positioning) | Layout lab / web preview | LGPL-3.0-only, GPL-2.0-only or GPL-3.0-only; includes Chromium (BSD-3-Clause) and many third-party components | Chromium's own notices: <https://doc.qt.io/qt-6/qtwebengine-licensing.html> |
| FFmpeg: libavcodec, libavutil, libswscale (and the FFmpeg libraries they load) | H.264 decoding for the native device display | LGPL-2.1-or-later; GPL-2.0-or-later or GPL-3.0-or-later, depending on the build | © the FFmpeg developers. <https://ffmpeg.org/legal.html> |
| OpenSSL 3 (indirectly, through Qt Network's TLS backend and FFmpeg) | TLS | Apache-2.0 | Compatible with GPL-3.0. `tomaccoctl` only runs the `openssl` command. |

Build tools that are not part of the result: CMake (BSD-3-Clause), Ninja
(Apache-2.0), protobuf's `protoc` (BSD-3-Clause, runs only to generate Rust
code from `native/protocol/idb.proto`), the Rust toolchain (MIT OR
Apache-2.0), and Meson (Apache-2.0, installed into the engine recipe's private
virtual environment).

## Embedded in the binaries

### SQLite

`rusqlite` is built with its `bundled` feature, so `tomaccoctl` embeds SQLite
3.50.2 from `libsqlite3-sys`. SQLite is in the public domain
(<https://sqlite.org/copyright.html>).

### idb protocol subset

`native/protocol/idb.proto` follows `facebook/idb/proto/idb.proto`. `build.rs`
compiles it into `tomaccoctl` with `tonic-prost-build`.

```
Copyright (c) Meta Platforms, Inc. and affiliates.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
IN THE SOFTWARE.
```

### Protocols implemented for interoperability (no code copied)

- scrcpy v4.1 protocol (Genymobile, Apache-2.0). The scrcpy server is
  installed by scrcpy itself and is not redistributed.
- Appium WebDriverAgent HTTP and MJPEG protocols (BSD-3-Clause).
- JDWP, the Debug Adapter Protocol, the Model Context Protocol, the Google
  Play Android Publisher API and Google OAuth 2.0 service-account tokens.
  These are public specifications.

### Assets

| Path | Origin | License |
| --- | --- | --- |
| `assets/icon.svg`, the line icons drawn in `qml/StudioIcon.qml` | Original Tomacco artwork | GPL-2.0-or-later |
| Device frames (`qml/Device.qml`, `qml/NativeDeviceFrame.qml`) | Drawn from rounded rectangles and cutout shapes at run time; no manufacturer artwork or images | GPL-2.0-or-later |
| `assets/models.json` | Display sizes, resolutions and colour names from the manufacturers' public specification pages, which are linked in each entry's `source` field. Safe areas, corner radii and the `virtual` values are Tomacco's own approximations or measurements. | Facts. The compilation is GPL-2.0-or-later. Product names are the manufacturers' trademarks (see docs/LEGAL.md). |
| `assets/demo.html`, `assets/layout-audit.js`, `assets/templates/*`, `examples/*` | Original | GPL-2.0-or-later |
| `examples/ios-restore-native/libSystem.tbd`, `integrations/ios-emulator/*-libSystem.tbd` | Hand-written linker stubs that list only the names of the public C symbols the examples call, so they can be linked without an SDK | GPL-2.0-or-later. No Apple code. |
| `tests/fixtures/*.h264` | Synthetic FFmpeg `testsrc` colour bars | GPL-2.0-or-later |
| `assets/fonts/archivo-var.ttf` | [Archivo](https://github.com/Omnibus-Type/Archivo) by The Archivo Project Authors, subset to Latin. Bundled in the desktop. Text in `assets/fonts/OFL-Archivo.txt`. | SIL OFL 1.1 |
| `assets/fonts/jetbrains-mono-var.ttf` | [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) by The JetBrains Mono Project Authors, subset to Latin. Bundled in the desktop. Text in `assets/fonts/OFL-JetBrainsMono.txt`. | SIL OFL 1.1 |

## integrations/ios-emulator

These are patches, device models and guest programs for the optional, experimental
iOS research runtime. They are installed as source text
(`share/tomacco/integrations/ios-emulator/`). No Apple firmware, Apple
binary, SDK, key or iOS image is included. Each group keeps the license of the
code it changes:

| Files | License | Upstream |
| --- | --- | --- |
| `vphone-*.patch`, `sptm-boot-display.patch`, `vhost-display-dirty-log.patch`, `qemu-*.patch` | GPL-2.0 (the QEMU files they change; QEMU as a whole is GPL-2.0-only, and many files are GPL-2.0-or-later) | QEMU through jprx/qemu-sptm and yaelliethy/orchard |
| `vphone-*.c/.h`, `asc-*`, `mcc-t8140.*`, `ptd-t8140.*`, `pmp-research.c`, `dart-t8110.c`, `restore-*.c/.S` | GPL-2.0-or-later (SPDX line in each file) | Tomacco |
| `dart-t8110.h`, `exclave-bundle.h` | MIT (SPDX line in each file) | Tomacco |
| `qemu-plugin.h` | GPL-2.0-or-later (SPDX line in the file), an unmodified copy of the pinned QEMU's public TCG plugin API header | QEMU (jprx/qemu-sptm, bad6336) |
| `tcg-profile.c` | GPL-2.0-or-later, a TCG plugin built against that header | Tomacco |
| `reims-*.patch` | LGPL-3.0, the license of steelbrain/reims-vgpu, for the changes to `reims-vgpu/`. One change to orchard's `qemu/hw` transport is GPL-2.0-or-later. | steelbrain/reims-vgpu, as included in yaelliethy/orchard |
| `metal2vulkan-*.patch` | LGPL-3.0-or-later | steelbrain/metal2vulkan |
| `bringup.patch` | GPL-3.0 and AGPL-3.0 parts, the license of ChefKiss Inferno (`LICENSE`, `COPYING`, `COPYING_GPL-3.0`, `COPYING_AGPL-3.0`). The patch removes Inferno's branding, as its branding notice requires. | ChefKissInc/Inferno |
| `vhost-vsock-scatter.patch`, `vsock-tx-packet.rs` | Apache-2.0 OR BSD-3-Clause | rust-vmm/vhost-device |
| `apfs-buffer-allocation.patch` | GPL-2.0 | linux-apfs/linux-apfs-rw |
| `guest-*.c/.h`, `research-*.c`, `ios-*.c/.h/.S`, `ios27-*.S`, `cryptex-seal-pins.S`, `virtio-used-ring.S`, `storage-probe.c`, `*.plist`, `*.md` | GPL-2.0-or-later (project default; these files carry no SPDX line because the build recipes pin their hashes) | Tomacco |
| `ios27-*.json`, `qemu-configure-pins.json` | GPL-2.0-or-later. Recipes made of hashes, file offsets, single instruction words and device-tree property values; no firmware content beyond these identifiers. | Tomacco |
| `COPYING_GPL-2.0` | QEMU's license text, from jprx/qemu-sptm | |
| `LICENSE.darwin-vm` | MIT, © 2026 jprx | jprx/darwin-vm |

Some of these combinations are allowed only under GPL version 3. LGPL-3.0,
GPL-3.0 and AGPL-3.0 code cannot be combined with GPL-2.0-only code. That is
why each patch stays under the license of the project it changes, and why
Tomacco's own files are "or later". The patch files are distributed side
by side with Tomacco as an aggregate. Tomacco does not distribute
an engine built from them, see the next section.

## Consulted, no code taken

The register definitions of [Asahi Linux's m1n1](https://github.com/AsahiLinux/m1n1)
(MIT) are the published reference for the DART and ASC models in
`integrations/ios-emulator`; those files cite them where they are used.

## Downloaded and built on request

The optional recipes in `tools/`, and the setup guide, fetch these at pinned
revisions or with pinned SHA-256 digests, only when you run them. Tomacco
redistributes none of them.

| Component | Pin | License | Fetched by |
| --- | --- | --- | --- |
| jprx/qemu-sptm | `bad6336fb242` | GPL-2.0 (QEMU; parts under compatible licenses) | `build_ios_emulator.py` |
| yaelliethy/orchard (QEMU + reims-vgpu) | `c73a2bda1fd4` | QEMU GPL-2.0, orchard's changes GPL-2.0-or-later, `reims-vgpu/` LGPL-3.0 | `build_ios_paravirt.py` |
| steelbrain/metal2vulkan | `9e0e99a41dc3` | LGPL-3.0-or-later | `build_ios_paravirt.py` |
| ChefKissInc/Inferno (optional, earlier backend) | `cc4302a99167` | GPL-3.0, with AGPL-3.0 parts; branding restricted | `build_ios_emulator.py --backend inferno` |
| lzfse | `e634ca58b482` | BSD-3-Clause, © 2015-2016 Apple Inc. | `build_ios_emulator.py` |
| jprx/darwin-vm | `e969c3adffd8` | MIT | `prepare_ios_emulator.py` |
| TrungNguyen1909/qemu-t8030-tools (`bootstrap_scripts/ticket.shsh2` research template) | `7a2017461ee7` | No stated license (no LICENSE file upstream). A research Image4 blob; Tomacco neither bundles nor redistributes it. | you, per [setup](docs/virtual-ios/setup.md#how-do-i-get-the-im4m); read by `prepare_ios_research_manifest.py` |
| ProcursusTeam/ldid | `af86971ae72e` | AGPL-3.0 | `prepare_ios_emulator.py` |
| linux-apfs/linux-apfs-rw | `923526a669d7` | GPL-2.0 | `prepare_ios_emulator.py` |
| blacktop/ipsw 3.1.730 (release binary) | SHA-256 `7a9bb4f0…` | MIT | `prepare_ios_emulator.py` |
| Alpine busybox-static 1.37.0-r31 | SHA-256 `b0d6cfc5…` | GPL-2.0 | `prepare_ios_emulator.py` |
| rust-vmm/vhost-device | `842abb0dff43` | Apache-2.0 OR BSD-3-Clause | `build_ios_guest_control.py` |
| tpoechtrager/cctools-port | `904de2a71d4d` | APSL-2.0 (Apple Public Source License), with GPL parts | `build_ios_linker.py` |
| tpoechtrager/apple-libtapi | `fa9443738c1a` | NCSA / Apache-2.0 with LLVM exception | `build_ios_linker.py` |
| Lakr233/vphone-cli (one source file, `GraphicLoader/main.mm`) | `3a819b0dd00a` | MIT | `build_ios_gpu_plugin.py` (you pass the checkout) |
| Meson 1.10.2 (PyPI) | version pin | Apache-2.0 | `build_ios_emulator.py` |
| Appium WebDriverAgent v16.13.6 runner | SHA-256 verified | BSD-3-Clause | `ios.wda_prepare` |
| joshuaswarren/omarchy-apple-dev, © 2026 Joshua Warren; Flutter pipeline by dl-alexandre | `79bda716bf20` | MIT (below) | `install_apple_dev.py`, for `ios.toolkit_install` |
| joshuaswarren/xtool (fork of xtool-org/xtool) | `f300489f3a3f` | MIT | its `install-toolchain.sh`, with `toolchain` only |
| joshuaswarren/OpenAppleMacros | `cb003a1763b0` | MIT | the same |
| indygreg/apple-platform-rs: rcodesign 0.29.0 (release binary) | SHA-256 `dbe85ced…` (x86_64) | MPL-2.0 | the same |
| blacktop/ipsw 3.1.731 (release binary; no operation calls it) | SHA-256 `98e5b127…` (x86_64) | MIT | the same |
| pymobiledevice3 and its Python dependencies (PyPI) | latest at install time, not pinned | GPL-3.0 and their own licenses | the same |
| go-ios, libusbmuxd/iproxy, xtool, Swift, scrcpy, Android SDK, Gradle, Flutter | your installation | their own licenses | found on `PATH` |

**If you redistribute a built engine.** orchard's QEMU contains GPL-2.0-only
files and links the LGPL-3.0 `reims-vgpu` library statically. These two
licenses are not compatible when the code is combined into one program. Inferno
is GPL-3.0/AGPL-3.0, and that cannot be combined with GPL-2.0-only QEMU files
either. Building and running an engine for yourself is not affected. Before you
distribute an engine binary, get the combination reviewed, or build it without
the GPL-2.0-only parts.

**omarchy-apple-dev.** Tomacco contains none of this project's code. On request it
fetches the commit above into its data directory and runs the project's scripts as
separate programs. Thanks to Joshua Warren for the project and to dl-alexandre
([@agrxculture](https://github.com/agrxculture)) for its Flutter pipeline. Its notice:

```text
MIT License

Copyright (c) 2026 Joshua Warren

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Apple software (iOS, cloudOS/PCC research images, the Xcode SDK, firmware) is
never downloaded by default and never redistributed. When you pass an official
Apple IPSW URL to `prepare_ios_emulator.py --ipsw-url`, the recipe fetches
components from that URL for you. Apple's license terms for that software
apply to you. See [docs/legal.md](docs/legal.md).

## Rust crates

Licenses are SPDX expressions. Where a crate offers a choice ("MIT OR
Apache-2.0"), Tomacco uses it under MIT, which is GPL-2.0 compatible.
The copyright notices were taken from each crate's license files, or from the
crate's `authors` field where the license file has none. The full license texts
follow the table.

### Linked into `tomaccoctl`

| Crate | Version | License (SPDX) | Copyright notice in the crate | Source |
| --- | --- | --- | --- | --- |
| adler2 | 2.0.1 | 0BSD OR MIT OR Apache-2.0 | Copyright (C) Jonas Schievink | https://github.com/oyvindln/adler2 |
| aho-corasick | 1.1.5 | Unlicense OR MIT | Copyright (c) 2015 Andrew Gallant | https://github.com/BurntSushi/aho-corasick |
| anyhow | 1.0.104 | MIT OR Apache-2.0 | Authors: David Tolnay | https://github.com/dtolnay/anyhow |
| async-trait | 0.1.92 | MIT OR Apache-2.0 | Authors: David Tolnay | https://github.com/dtolnay/async-trait |
| atomic-waker | 1.1.2 | Apache-2.0 OR MIT | Copyright (c) 2016 Alex Crichton; Copyright (c) 2017 The Tokio Authors | https://github.com/smol-rs/atomic-waker |
| axum | 0.8.9 | MIT | Copyright (c) 2019 axum Contributors | https://github.com/tokio-rs/axum |
| axum-core | 0.5.6 | MIT | Copyright (c) 2019–2025 axum Contributors | https://github.com/tokio-rs/axum |
| base64 | 0.22.1 | MIT OR Apache-2.0 | Copyright (c) 2015 Alice Maz | https://github.com/marshallpierce/rust-base64 |
| base64 | 0.23.1 | MIT OR Apache-2.0 | Copyright (c) 2015 Alice Maz | https://github.com/marshallpierce/rust-base64 |
| bitflags | 2.13.2 | MIT OR Apache-2.0 | Copyright (c) 2014 The Rust Project Developers | https://github.com/bitflags/bitflags |
| block-buffer | 0.12.1 | MIT OR Apache-2.0 | Copyright (c) 2018-2019 The RustCrypto Project Developers | https://github.com/RustCrypto/utils |
| bytes | 1.12.1 | MIT | Copyright (c) 2018 Carl Lerche | https://github.com/tokio-rs/bytes |
| cfg-if | 1.0.5 | MIT OR Apache-2.0 | Copyright (c) 2014 Alex Crichton | https://github.com/rust-lang/cfg-if |
| const-oid | 0.10.2 | Apache-2.0 OR MIT | Copyright (c) 2020-2026 The RustCrypto Project Developers | https://github.com/RustCrypto/formats |
| cpufeatures | 0.3.1 | MIT OR Apache-2.0 | Copyright (c) 2020-2025 The RustCrypto Project Developers | https://github.com/RustCrypto/utils |
| crc32fast | 1.5.3 | MIT OR Apache-2.0 | Copyright (c) 2018 Sam Rijs, Alex Crichton and contributors | https://github.com/srijs/rust-crc32fast |
| crypto-common | 0.2.2 | MIT OR Apache-2.0 | Copyright (c) 2021 RustCrypto Developers | https://github.com/RustCrypto/traits |
| curve25519-dalek | 5.0.0 | BSD-3-Clause | Copyright (c) 2016-2021 isis agora lovecruft; Copyright (c) 2016-2021 Henry de Valence | https://github.com/dalek-cryptography/curve25519-dalek/tree/main/curve25519-dalek |
| curve25519-dalek-derive | 0.1.1 | MIT/Apache-2.0 | The crate authors (no notice line in the license file) | https://github.com/dalek-cryptography/curve25519-dalek |
| digest | 0.11.3 | MIT OR Apache-2.0 | Copyright (c) 2017 Artyom Pavlov | https://github.com/RustCrypto/traits |
| ed25519 | 3.0.0 | Apache-2.0 OR MIT | Copyright (c) 2018-2023 RustCrypto Developers | https://github.com/RustCrypto/signatures |
| ed25519-dalek | 3.0.0 | BSD-3-Clause | Copyright (c) 2017-2019 isis agora lovecruft | https://github.com/dalek-cryptography/curve25519-dalek/tree/main/ed25519-dalek |
| either | 1.19.0 | MIT OR Apache-2.0 | Copyright (c) 2015 | https://github.com/rayon-rs/either |
| equivalent | 1.0.3 | Apache-2.0 OR MIT | Copyright (c) 2016--2023 | https://github.com/indexmap-rs/equivalent |
| fallible-iterator | 0.3.0 | MIT/Apache-2.0 | Copyright (c) 2015 The rust-openssl-verify Developers | https://github.com/sfackler/rust-fallible-iterator |
| fallible-streaming-iterator | 0.1.9 | MIT/Apache-2.0 | Copyright (c) 2016 The fallible-streaming-iterator Developers | https://github.com/sfackler/fallible-streaming-iterator |
| fastrand | 2.5.0 | Apache-2.0 OR MIT | Authors: Stjepan Glavina | https://github.com/smol-rs/fastrand |
| filetime | 0.2.29 | MIT/Apache-2.0 | Copyright (c) 2014 Alex Crichton | https://github.com/alexcrichton/filetime |
| flate2 | 1.1.10 | MIT OR Apache-2.0 | Copyright (c) 2014-2026 Alex Crichton | https://github.com/rust-lang/flate2-rs |
| fnv | 1.0.7 | Apache-2.0 / MIT | Copyright (c) 2017 Contributors | https://github.com/servo/rust-fnv |
| foldhash | 0.2.0 | Zlib | Copyright (c) 2024 Orson Peters | https://github.com/orlp/foldhash |
| futures-channel | 0.3.34 | MIT OR Apache-2.0 | Copyright (c) 2016 Alex Crichton; Copyright (c) 2017 The Tokio Authors | https://github.com/rust-lang/futures-rs |
| futures-core | 0.3.34 | MIT OR Apache-2.0 | Copyright (c) 2016 Alex Crichton; Copyright (c) 2017 The Tokio Authors | https://github.com/rust-lang/futures-rs |
| futures-sink | 0.3.34 | MIT OR Apache-2.0 | Copyright (c) 2016 Alex Crichton; Copyright (c) 2017 The Tokio Authors | https://github.com/rust-lang/futures-rs |
| futures-task | 0.3.34 | MIT OR Apache-2.0 | Copyright (c) 2016 Alex Crichton; Copyright (c) 2017 The Tokio Authors | https://github.com/rust-lang/futures-rs |
| futures-util | 0.3.34 | MIT OR Apache-2.0 | Copyright (c) 2016 Alex Crichton; Copyright (c) 2017 The Tokio Authors | https://github.com/rust-lang/futures-rs |
| getrandom | 0.4.3 | MIT OR Apache-2.0 | Copyright (c) 2018-2026 The rust-random Project Developers; Copyright (c) 2014 The Rust Project Developers | https://github.com/rust-random/getrandom |
| glob | 0.3.4 | MIT OR Apache-2.0 | Copyright (c) 2014 The Rust Project Developers | https://github.com/rust-lang/glob |
| h2 | 0.4.20 | MIT | Copyright (c) 2017 h2 authors | https://github.com/hyperium/h2 |
| hashbrown | 0.17.1 | MIT OR Apache-2.0 | Copyright (c) 2016 Amanieu d'Antras | https://github.com/rust-lang/hashbrown |
| hashlink | 0.12.2 | MIT OR Apache-2.0 | Authors: kyren | https://github.com/djc/hashlink |
| http | 1.5.0 | MIT OR Apache-2.0 | Copyright 2017 http-rs authors; Copyright (c) 2017 http-rs authors | https://github.com/hyperium/http |
| http-body | 1.1.0 | MIT | Copyright (c) 2019-2026 Sean McArthur & Hyper Contributors | https://github.com/hyperium/http-body |
| http-body-util | 0.1.5 | MIT | Copyright (c) 2019-2026 Sean McArthur & Hyper Contributors | https://github.com/hyperium/http-body |
| httparse | 1.10.1 | MIT OR Apache-2.0 | Copyright (c) 2015-2025 Sean McArthur | https://github.com/seanmonstar/httparse |
| httpdate | 1.0.3 | MIT OR Apache-2.0 | Copyright (c) 2016 Pyfisch | https://github.com/pyfisch/httpdate |
| hybrid-array | 0.4.15 | MIT OR Apache-2.0 | Copyright (c) 2022-2026 The RustCrypto Project Developers | https://github.com/RustCrypto/hybrid-array |
| hyper | 1.12.0 | MIT | Copyright (c) 2014-2026 Sean McArthur | https://github.com/hyperium/hyper |
| hyper-timeout | 0.5.2 | MIT OR Apache-2.0 | Copyright (c) 2016 The weldr Project Developers | https://github.com/hjr3/hyper-timeout |
| hyper-util | 0.1.21 | MIT | Copyright (c) 2023-2025 Sean McArthur | https://github.com/hyperium/hyper-util |
| indexmap | 2.14.2 | Apache-2.0 OR MIT | Copyright (c) 2016--2017 | https://github.com/indexmap-rs/indexmap |
| itertools | 0.14.0 | MIT OR Apache-2.0 | Copyright (c) 2015 | https://github.com/rust-itertools/itertools |
| itoa | 1.0.18 | MIT OR Apache-2.0 | Authors: David Tolnay | https://github.com/dtolnay/itoa |
| libc | 0.2.191 | MIT OR Apache-2.0 | Copyright (c) The Rust Project Developers | https://github.com/rust-lang/libc |
| libsqlite3-sys | 0.39.1 | MIT | Copyright (c) 2014 The rusqlite developers | https://github.com/rusqlite/rusqlite |
| linux-raw-sys | 0.12.1 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | Copyrights in the `linux-raw-sys` project are retained by their contributors | https://github.com/sunfishcode/linux-raw-sys |
| matchit | 0.8.4 | MIT AND BSD-3-Clause | Copyright (c) 2022 Ibraheem Ahmed; Copyright (c) 2013, Julien Schmidt | https://github.com/ibraheemdev/matchit |
| memchr | 2.8.3 | Unlicense OR MIT | Copyright (c) 2015 Andrew Gallant | https://github.com/BurntSushi/memchr |
| mime | 0.3.17 | MIT OR Apache-2.0 | Copyright (c) 2014 Sean McArthur | https://github.com/hyperium/mime |
| miniz_oxide | 0.9.1 | MIT OR Zlib OR Apache-2.0 | Copyright 2013-2014 RAD Game Tools and Valve Software; Copyright 2010-2014 Rich Geldreich and Tenacious Software LLC | https://github.com/Frommi/miniz_oxide/tree/master/miniz_oxide |
| mio | 1.2.4 | MIT | Copyright (c) 2014 Carl Lerche and other MIO contributors | https://github.com/tokio-rs/mio |
| once_cell | 1.21.4 | MIT OR Apache-2.0 | Authors: Aleksey Kladov | https://github.com/matklad/once_cell |
| percent-encoding | 2.3.2 | MIT OR Apache-2.0 | Copyright (c) 2013-2025 The rust-url developers | https://github.com/servo/rust-url/ |
| pin-project | 1.1.13 | Apache-2.0 OR MIT | The crate authors (no notice line in the license file) | https://github.com/taiki-e/pin-project |
| pin-project-internal | 1.1.13 | Apache-2.0 OR MIT | The crate authors (no notice line in the license file) | https://github.com/taiki-e/pin-project |
| pin-project-lite | 0.2.17 | Apache-2.0 OR MIT | The crate authors (no notice line in the license file) | https://github.com/taiki-e/pin-project-lite |
| proc-macro2 | 1.0.107 | MIT OR Apache-2.0 | Authors: David Tolnay, Alex Crichton | https://github.com/dtolnay/proc-macro2 |
| prost | 0.14.4 | Apache-2.0 | Authors: Dan Burkert, Lucio Franco, Casper Meijn, Tokio Contributors | https://github.com/tokio-rs/prost |
| prost-derive | 0.14.4 | Apache-2.0 | Authors: Dan Burkert, Lucio Franco, Casper Meijn, Tokio Contributors | https://github.com/tokio-rs/prost |
| quote | 1.0.47 | MIT OR Apache-2.0 | Authors: David Tolnay | https://github.com/dtolnay/quote |
| regex | 1.13.1 | MIT OR Apache-2.0 | Copyright (c) 2014 The Rust Project Developers | https://github.com/rust-lang/regex |
| regex-automata | 0.4.18 | MIT OR Apache-2.0 | Copyright (c) 2014 The Rust Project Developers | https://github.com/rust-lang/regex |
| regex-syntax | 0.8.11 | MIT OR Apache-2.0 | Copyright (c) 2014 The Rust Project Developers | https://github.com/rust-lang/regex |
| roxmltree | 0.21.2 | MIT OR Apache-2.0 | Copyright (c) 2018 Yevhenii Reizner | https://github.com/RazrFalcon/roxmltree |
| rusqlite | 0.41.1 | MIT | Copyright (c) 2014 The rusqlite developers | https://github.com/rusqlite/rusqlite |
| rustix | 1.1.6 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | Copyrights in the `rustix` project are retained by their contributors | https://github.com/bytecodealliance/rustix |
| semver | 1.0.28 | MIT OR Apache-2.0 | Authors: David Tolnay | https://github.com/dtolnay/semver |
| serde | 1.0.229 | MIT OR Apache-2.0 | Authors: Erick Tryzelaar, David Tolnay | https://github.com/serde-rs/serde |
| serde_core | 1.0.229 | MIT OR Apache-2.0 | Authors: Erick Tryzelaar, David Tolnay | https://github.com/serde-rs/serde |
| serde_derive | 1.0.229 | MIT OR Apache-2.0 | Authors: Erick Tryzelaar, David Tolnay | https://github.com/serde-rs/serde |
| serde_json | 1.0.154 | MIT OR Apache-2.0 | Authors: Erick Tryzelaar, David Tolnay | https://github.com/serde-rs/json |
| sha2 | 0.11.1 | MIT OR Apache-2.0 | Copyright (c) 2006-2009 Graydon Hoare; Copyright (c) 2009-2013 Mozilla Foundation; Copyright (c) 2016 Artyom Pavlov | https://github.com/RustCrypto/hashes |
| signature | 3.0.0 | Apache-2.0 OR MIT | Copyright (c) 2018-2023 RustCrypto Developers | https://github.com/RustCrypto/traits |
| simd-adler32 | 0.3.10 | MIT | Copyright (c) 2021 Marvin Countryman | https://github.com/mcountryman/simd-adler32 |
| slab | 0.4.12 | MIT | Copyright (c) 2019 Carl Lerche | https://github.com/tokio-rs/slab |
| smallvec | 1.16.4 | MIT OR Apache-2.0 | Copyright (c) 2018 The Servo Project Developers | https://github.com/servo/rust-smallvec |
| socket2 | 0.6.5 | MIT OR Apache-2.0 | Copyright (c) 2014 Alex Crichton | https://github.com/rust-lang/socket2 |
| subtle | 2.6.1 | BSD-3-Clause | Copyright (c) 2016-2024 Isis Agora Lovecruft; Copyright (c) 2016-2017 Henry de Valence | https://github.com/dalek-cryptography/subtle |
| syn | 2.0.119 | MIT OR Apache-2.0 | Authors: David Tolnay | https://github.com/dtolnay/syn |
| syn | 3.0.9 | MIT OR Apache-2.0 | Authors: David Tolnay | https://github.com/dtolnay/syn |
| sync_wrapper | 1.0.2 | Apache-2.0 | Authors: Actyx AG | https://github.com/Actyx/sync_wrapper |
| tar | 0.4.46 | MIT OR Apache-2.0 | Copyright (c) The tar-rs Project Contributors | https://github.com/composefs/tar-rs |
| tempfile | 3.27.0 | MIT OR Apache-2.0 | Copyright (c) 2015 Steven Allen | https://github.com/Stebalien/tempfile |
| tokio | 1.54.1 | MIT | Copyright (c) Tokio Contributors | https://github.com/tokio-rs/tokio |
| tokio-macros | 2.7.2 | MIT | Copyright (c) 2019 Yoshua Wuyts; Copyright (c) Tokio Contributors | https://github.com/tokio-rs/tokio |
| tokio-stream | 0.1.19 | MIT | Copyright (c) Tokio Contributors | https://github.com/tokio-rs/tokio |
| tokio-util | 0.7.20 | MIT | Copyright (c) Tokio Contributors | https://github.com/tokio-rs/tokio |
| tonic | 0.14.9 | MIT | Copyright (c) 2025 Lucio Franco | https://github.com/grpc/grpc-rust |
| tonic-prost | 0.14.9 | MIT | Authors: Lucio Franco | https://github.com/grpc/grpc-rust |
| tower | 0.5.3 | MIT | Copyright (c) 2019 Tower Contributors | https://github.com/tower-rs/tower |
| tower-layer | 0.3.3 | MIT | Copyright (c) 2019 Tower Contributors | https://github.com/tower-rs/tower |
| tower-service | 0.3.3 | MIT | Copyright (c) 2019 Tower Contributors | https://github.com/tower-rs/tower |
| tracing | 0.1.44 | MIT | Copyright (c) 2019 Tokio Contributors | https://github.com/tokio-rs/tracing |
| tracing-attributes | 0.1.31 | MIT | Copyright (c) 2019 Tokio Contributors | https://github.com/tokio-rs/tracing |
| tracing-core | 0.1.36 | MIT | Copyright (c) 2019 Tokio Contributors | https://github.com/tokio-rs/tracing |
| try-lock | 0.2.5 | MIT | Copyright (c) 2018-2023 Sean McArthur; Copyright (c) 2016 Alex Crichton | https://github.com/seanmonstar/try-lock |
| typenum | 1.20.1 | MIT OR Apache-2.0 | Copyright 2014 Paho Lurie-Gregg; Copyright (c) 2014 Paho Lurie-Gregg | https://github.com/paholg/typenum |
| unicode-ident | 1.0.27 | (MIT OR Apache-2.0) AND Unicode-3.0 | COPYRIGHT AND PERMISSION NOTICE; Copyright © 1991-2023 Unicode, Inc. | https://github.com/dtolnay/unicode-ident |
| uuid | 1.29.0 | Apache-2.0 OR MIT | Copyright (c) 2014 The Rust Project Developers; Copyright (c) 2018 Ashley Mannix, Christopher Armstrong, Dylan DPC, Hunar Roop Kahlon | https://github.com/uuid-rs/uuid |
| want | 0.3.2 | MIT | Copyright (c) 2018-2019 Sean McArthur | https://github.com/seanmonstar/want |
| xattr | 1.7.0 | MIT OR Apache-2.0 | Copyright (c) 2015 Steven Allen | https://github.com/Stebalien/xattr |
| zmij | 1.0.23 | MIT | Authors: David Tolnay | https://github.com/dtolnay/zmij |

### Build-time only (code generators, procedural macros, build scripts; not in the binary)

| Crate | Version | License (SPDX) | Copyright notice in the crate | Source |
| --- | --- | --- | --- | --- |
| cc | 1.10.0 | MIT OR Apache-2.0 | Copyright (c) 2014 Alex Crichton | https://github.com/rust-lang/cc-rs |
| find-msvc-tools | 0.1.15 | MIT OR Apache-2.0 | Copyright (c) 2014 Alex Crichton | https://github.com/rust-lang/cc-rs |
| fixedbitset | 0.5.7 | MIT OR Apache-2.0 | Copyright (c) 2015-2017 | https://github.com/petgraph/fixedbitset |
| foldhash | 0.1.5 | Zlib | Copyright (c) 2024 Orson Peters | https://github.com/orlp/foldhash |
| hashbrown | 0.15.5 | MIT OR Apache-2.0 | Copyright (c) 2016 Amanieu d'Antras | https://github.com/rust-lang/hashbrown |
| heck | 0.5.0 | MIT OR Apache-2.0 | Copyright (c) 2015 The Rust Project Developers | https://github.com/withoutboats/heck |
| log | 0.4.35 | MIT OR Apache-2.0 | Copyright (c) 2014 The Rust Project Developers | https://github.com/rust-lang/log |
| multimap | 0.10.1 | MIT OR Apache-2.0 | Copyright (c) 2016 multimap developers | https://github.com/havarnov/multimap |
| petgraph | 0.8.3 | MIT OR Apache-2.0 | Copyright (c) 2015 | https://github.com/petgraph/petgraph |
| pkg-config | 0.3.34 | MIT OR Apache-2.0 | Copyright (c) 2014 Alex Crichton | https://github.com/rust-lang/pkg-config-rs |
| prettyplease | 0.2.37 | MIT OR Apache-2.0 | Authors: David Tolnay | https://github.com/dtolnay/prettyplease |
| prost-build | 0.14.4 | Apache-2.0 | Authors: Dan Burkert, Lucio Franco, Casper Meijn, Tokio Contributors | https://github.com/tokio-rs/prost |
| prost-types | 0.14.4 | Apache-2.0 | Authors: Dan Burkert, Lucio Franco, Casper Meijn, Tokio Contributors | https://github.com/tokio-rs/prost |
| pulldown-cmark | 0.13.6 | MIT | Copyright 2015 Google Inc. All rights reserved. | https://github.com/raphlinus/pulldown-cmark |
| pulldown-cmark-to-cmark | 22.0.3 | Apache-2.0 | Copyright 2018 "Sebastian Thiel <byronimo@gmail.com>", "Dylan Owen <dyltotheo@gmail.com>", "Alessandro Ogier <alessandro.ogier@gmail.com>", "Zixian Cai <2891235+caizixian@users.noreply.github.com>" | https://github.com/Byron/pulldown-cmark-to-cmark |
| rustc_version | 0.4.1 | MIT OR Apache-2.0 | Copyright (c) 2016 The Rust Project Developers | https://github.com/djc/rustc-version-rs |
| shlex | 2.0.1 | MIT OR Apache-2.0 | Copyright 2015 Nicholas Allegra (comex).; Copyright (c) 2015 Nicholas Allegra (comex). | https://github.com/comex/rust-shlex |
| tonic-build | 0.14.9 | MIT | Copyright (c) 2025 Lucio Franco | https://github.com/grpc/grpc-rust |
| tonic-prost-build | 0.14.9 | MIT | Authors: Lucio Franco | https://github.com/grpc/grpc-rust |
| unicase | 2.10.0 | MIT OR Apache-2.0 | Copyright (c) 2014-2026 Sean McArthur | https://github.com/seanmonstar/unicase |
| vcpkg | 0.2.15 | MIT/Apache-2.0 | Copyright (c) 2017 Jim McGrath | https://github.com/mcgoo/vcpkg-rs |

`syn` and `quote` also depend on `unicode-ident`, whose Unicode data is under
Unicode-3.0. That crate runs only at compile time.

## License texts

### MIT License

This text applies to each MIT-licensed crate above, with that crate's
copyright notice from the table.

```
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### BSD-3-Clause (matchit, from julienschmidt/httprouter)

```
BSD 3-Clause License

Copyright (c) 2013, Julien Schmidt
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### BSD-3-Clause (curve25519-dalek)

```text
Copyright (c) 2016-2021 isis agora lovecruft. All rights reserved.
Copyright (c) 2016-2021 Henry de Valence. All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

1. Redistributions of source code must retain the above copyright
notice, this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright
notice, this list of conditions and the following disclaimer in the
documentation and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
contributors may be used to endorse or promote products derived from
this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS
IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED
TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE. 

========================================================================

Portions of curve25519-dalek were originally derived from Adam Langley's
Go ed25519 implementation, found at <https://github.com/agl/ed25519/>,
under the following licence:

========================================================================

Copyright (c) 2012 The Go Authors. All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

   * Redistributions of source code must retain the above copyright
notice, this list of conditions and the following disclaimer.
   * Redistributions in binary form must reproduce the above
copyright notice, this list of conditions and the following disclaimer
in the documentation and/or other materials provided with the
distribution.
   * Neither the name of Google Inc. nor the names of its
contributors may be used to endorse or promote products derived from
this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS
IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER
OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### BSD-3-Clause (ed25519-dalek)

```text
Copyright (c) 2017-2019 isis agora lovecruft. All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

1. Redistributions of source code must retain the above copyright
notice, this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright
notice, this list of conditions and the following disclaimer in the
documentation and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
contributors may be used to endorse or promote products derived from
this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS
IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED
TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### BSD-3-Clause (subtle)

```text
Copyright (c) 2016-2017 Isis Agora Lovecruft, Henry de Valence. All rights reserved.
Copyright (c) 2016-2024 Isis Agora Lovecruft. All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

1. Redistributions of source code must retain the above copyright
notice, this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright
notice, this list of conditions and the following disclaimer in the
documentation and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
contributors may be used to endorse or promote products derived from
this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS
IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED
TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### Zlib (foldhash)

```
Copyright (c) 2024 Orson Peters

This software is provided 'as-is', without any express or implied warranty. In
no event will the authors be held liable for any damages arising from the use of
this software.

Permission is granted to anyone to use this software for any purpose, including
commercial applications, and to alter it and redistribute it freely, subject to
the following restrictions:

1. The origin of this software must not be misrepresented; you must not claim
    that you wrote the original software. If you use this software in a product,
    an acknowledgment in the product documentation would be appreciated but is
    not required.

2. Altered source versions must be plainly marked as such, and must not be
    misrepresented as being the original software.

3. This notice may not be removed or altered from any source distribution.```

### Apache License 2.0 (prost, sync_wrapper, and the Apache-2.0 option of dual-licensed crates)

```
                              Apache License
                        Version 2.0, January 2004
                     http://www.apache.org/licenses/

TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

1. Definitions.

   "License" shall mean the terms and conditions for use, reproduction,
   and distribution as defined by Sections 1 through 9 of this document.

   "Licensor" shall mean the copyright owner or entity authorized by
   the copyright owner that is granting the License.

   "Legal Entity" shall mean the union of the acting entity and all
   other entities that control, are controlled by, or are under common
   control with that entity. For the purposes of this definition,
   "control" means (i) the power, direct or indirect, to cause the
   direction or management of such entity, whether by contract or
   otherwise, or (ii) ownership of fifty percent (50%) or more of the
   outstanding shares, or (iii) beneficial ownership of such entity.

   "You" (or "Your") shall mean an individual or Legal Entity
   exercising permissions granted by this License.

   "Source" form shall mean the preferred form for making modifications,
   including but not limited to software source code, documentation
   source, and configuration files.

   "Object" form shall mean any form resulting from mechanical
   transformation or translation of a Source form, including but
   not limited to compiled object code, generated documentation,
   and conversions to other media types.

   "Work" shall mean the work of authorship, whether in Source or
   Object form, made available under the License, as indicated by a
   copyright notice that is included in or attached to the work
   (an example is provided in the Appendix below).

   "Derivative Works" shall mean any work, whether in Source or Object
   form, that is based on (or derived from) the Work and for which the
   editorial revisions, annotations, elaborations, or other modifications
   represent, as a whole, an original work of authorship. For the purposes
   of this License, Derivative Works shall not include works that remain
   separable from, or merely link (or bind by name) to the interfaces of,
   the Work and Derivative Works thereof.

   "Contribution" shall mean any work of authorship, including
   the original version of the Work and any modifications or additions
   to that Work or Derivative Works thereof, that is intentionally
   submitted to Licensor for inclusion in the Work by the copyright owner
   or by an individual or Legal Entity authorized to submit on behalf of
   the copyright owner. For the purposes of this definition, "submitted"
   means any form of electronic, verbal, or written communication sent
   to the Licensor or its representatives, including but not limited to
   communication on electronic mailing lists, source code control systems,
   and issue tracking systems that are managed by, or on behalf of, the
   Licensor for the purpose of discussing and improving the Work, but
   excluding communication that is conspicuously marked or otherwise
   designated in writing by the copyright owner as "Not a Contribution."

   "Contributor" shall mean Licensor and any individual or Legal Entity
   on behalf of whom a Contribution has been received by Licensor and
   subsequently incorporated within the Work.

2. Grant of Copyright License. Subject to the terms and conditions of
   this License, each Contributor hereby grants to You a perpetual,
   worldwide, non-exclusive, no-charge, royalty-free, irrevocable
   copyright license to reproduce, prepare Derivative Works of,
   publicly display, publicly perform, sublicense, and distribute the
   Work and such Derivative Works in Source or Object form.

3. Grant of Patent License. Subject to the terms and conditions of
   this License, each Contributor hereby grants to You a perpetual,
   worldwide, non-exclusive, no-charge, royalty-free, irrevocable
   (except as stated in this section) patent license to make, have made,
   use, offer to sell, sell, import, and otherwise transfer the Work,
   where such license applies only to those patent claims licensable
   by such Contributor that are necessarily infringed by their
   Contribution(s) alone or by combination of their Contribution(s)
   with the Work to which such Contribution(s) was submitted. If You
   institute patent litigation against any entity (including a
   cross-claim or counterclaim in a lawsuit) alleging that the Work
   or a Contribution incorporated within the Work constitutes direct
   or contributory patent infringement, then any patent licenses
   granted to You under this License for that Work shall terminate
   as of the date such litigation is filed.

4. Redistribution. You may reproduce and distribute copies of the
   Work or Derivative Works thereof in any medium, with or without
   modifications, and in Source or Object form, provided that You
   meet the following conditions:

   (a) You must give any other recipients of the Work or
       Derivative Works a copy of this License; and

   (b) You must cause any modified files to carry prominent notices
       stating that You changed the files; and

   (c) You must retain, in the Source form of any Derivative Works
       that You distribute, all copyright, patent, trademark, and
       attribution notices from the Source form of the Work,
       excluding those notices that do not pertain to any part of
       the Derivative Works; and

   (d) If the Work includes a "NOTICE" text file as part of its
       distribution, then any Derivative Works that You distribute must
       include a readable copy of the attribution notices contained
       within such NOTICE file, excluding those notices that do not
       pertain to any part of the Derivative Works, in at least one
       of the following places: within a NOTICE text file distributed
       as part of the Derivative Works; within the Source form or
       documentation, if provided along with the Derivative Works; or,
       within a display generated by the Derivative Works, if and
       wherever such third-party notices normally appear. The contents
       of the NOTICE file are for informational purposes only and
       do not modify the License. You may add Your own attribution
       notices within Derivative Works that You distribute, alongside
       or as an addendum to the NOTICE text from the Work, provided
       that such additional attribution notices cannot be construed
       as modifying the License.

   You may add Your own copyright statement to Your modifications and
   may provide additional or different license terms and conditions
   for use, reproduction, or distribution of Your modifications, or
   for any such Derivative Works as a whole, provided Your use,
   reproduction, and distribution of the Work otherwise complies with
   the conditions stated in this License.

5. Submission of Contributions. Unless You explicitly state otherwise,
   any Contribution intentionally submitted for inclusion in the Work
   by You to the Licensor shall be under the terms and conditions of
   this License, without any additional terms or conditions.
   Notwithstanding the above, nothing herein shall supersede or modify
   the terms of any separate license agreement you may have executed
   with Licensor regarding such Contributions.

6. Trademarks. This License does not grant permission to use the trade
   names, trademarks, service marks, or product names of the Licensor,
   except as required for reasonable and customary use in describing the
   origin of the Work and reproducing the content of the NOTICE file.

7. Disclaimer of Warranty. Unless required by applicable law or
   agreed to in writing, Licensor provides the Work (and each
   Contributor provides its Contributions) on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
   implied, including, without limitation, any warranties or conditions
   of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A
   PARTICULAR PURPOSE. You are solely responsible for determining the
   appropriateness of using or redistributing the Work and assume any
   risks associated with Your exercise of permissions under this License.

8. Limitation of Liability. In no event and under no legal theory,
   whether in tort (including negligence), contract, or otherwise,
   unless required by applicable law (such as deliberate and grossly
   negligent acts) or agreed to in writing, shall any Contributor be
   liable to You for damages, including any direct, indirect, special,
   incidental, or consequential damages of any character arising as a
   result of this License or out of the use or inability to use the
   Work (including but not limited to damages for loss of goodwill,
   work stoppage, computer failure or malfunction, or any and all
   other commercial damages or losses), even if such Contributor
   has been advised of the possibility of such damages.

9. Accepting Warranty or Additional Liability. While redistributing
   the Work or Derivative Works thereof, You may choose to offer,
   and charge a fee for, acceptance of support, warranty, indemnity,
   or other liability obligations and/or rights consistent with this
   License. However, in accepting such obligations, You may act only
   on Your own behalf and on Your sole responsibility, not on behalf
   of any other Contributor, and only if You agree to indemnify,
   defend, and hold each Contributor harmless for any liability
   incurred by, or claims asserted against, such Contributor by reason
   of your accepting any such warranty or additional liability.

END OF TERMS AND CONDITIONS

APPENDIX: How to apply the Apache License to your work.

   To apply the Apache License to your work, attach the following
   boilerplate notice, with the fields enclosed by brackets "[]"
   replaced with your own identifying information. (Don't include
   the brackets!)  The text should be enclosed in the appropriate
   comment syntax for the file format. We also recommend that a
   file or class name and description of purpose be included on the
   same "printed page" as the copyright notice for easier
   identification within third-party archives.

Copyright [yyyy] [name of copyright owner]

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

	http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```
