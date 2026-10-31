# Building and testing

For people changing Tomacco. To only use it, [getting started](getting-started.md) is enough. How the code is laid out is in
[architecture](architecture.md).

## Build

Requirements are in [getting started](getting-started.md#1-install).

```sh
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build
```

The control plane also builds without Qt:

```sh
cargo build --release --locked
target/release/tomaccoctl schema
```

All operations are implemented in `native/` and share the JSON schemas embedded from `assets/operations.json`. Cargo dependencies
are pinned in `Cargo.lock`.

## Test

```sh
ctest --test-dir build --output-on-failure
cargo test --locked
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
```

CTest runs five groups: `desktop-ui`, `backend`, `native-display`, `control-plane` (the Python suite) and `rust-core`.

Run the Python tests directly from `tests/python`:

```sh
cd tests/python
PYTHONPATH=../../tools:..:../.. python3 -m unittest discover -q
```

Desktop checks:

```sh
QT_QPA_PLATFORMTHEME=generic QT_QUICK_CONTROLS_STYLE=Basic build/tomacco --smoke-test
python3 tests/gui_integration.py --binary build/tomacco
```

The test environment also needs `unzip` for the iPhone runner tests, a font such as DejaVu Sans for the desktop tests, loopback
networking, and a graphical session or `QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software` for the desktop checks. Tests that need a toolchain, engine or device they do not find skip and say
what it is.

**Temporary files.** Tests follow `TMPDIR`. Unix sockets need a path of at most 107 bytes, so the tests, the runtime and the virtual
iPhone's launcher make their socket directories in `TMPDIR` only while it is short enough, and otherwise in a private `XDG_RUNTIME_DIR`
or in `/tmp`. `tests/update_e2e.rs` holds several copies of the debug `tomaccoctl`; without `TMPDIR` it works in `target/tmp`, not in
`/tmp`.

**Documentation tests.** `tests/python/test_docs_links.py` checks every relative link and anchor in tracked Markdown.
`tests/python/test_docs_virtual_iphone.py` checks the `tomaccoctl` commands and tools the guides show against the schema and the sources, and the
limits they must state.

**Live checks** need a device or an engine and are not part of CTest:

```sh
TOMACCOCTL="$PWD/build/tomaccoctl" python3 tests/runtime_integration.py --project /absolute/app
python3 tests/iphone_linux_integration.py --binary build/tomacco --project /absolute/project
TOMACCOCTL="$PWD/build/tomaccoctl" TOMACCO_HOME=/path/to/isolated-state python3 tests/android_native_debug_integration.py --device emulator-5556 --ndk /path/to/android-ndk --output qa/android-native-debug
```

- `runtime_integration.py` needs a configured SDK, scrcpy 4.1, a booted emulator, the app installed and an active desktop.
- `iphone_linux_integration.py` checks the real Linux setup and the runner download. It is not an iPhone test.
- `android_native_debug_integration.py` installs its own JNI fixture on the device you select. Configure the SDK, NDK, JDK, Gradle and
  LLDB in that isolated state first.

## Translations

The desktop speaks English, German, French, Spanish and Italian. The translations are in `i18n/tomacco_de.ts`,
`i18n/tomacco_fr.ts`, `i18n/tomacco_es.ts` and `i18n/tomacco_it.ts`; `i18n/tomacco_en.ts` holds only the English plural forms. With Qt's Linguist tools (`qt6-tools`), this refreshes
them after text changes; without those tools, the build compiles them with `tools/ts2qm.py`.

```sh
cmake --build build --target update_translations
```

## Packaging

```sh
cmake --build build --target package
```

This makes a TGZ and its SHA-256. A distribution package configures with `-DTOMACCO_DISABLE_UPDATES=ON`, which builds an `tomaccoctl` that never asks for or
installs an update. It holds `bin/`, `lib/`, `share/` and `install.py`, which installs for one user what it finds beside it, with the options below.
`tools/build_source_release.py` builds the corresponding-source tarball a release offers; see [SOURCE.md.in](SOURCE.md.in).

**Arch package.** `packaging/arch/PKGBUILD` builds a system package from the release's source tarball, with `-DTOMACCO_DISABLE_UPDATES=ON`
and the tarball's checksum pinned. For a new release, update `pkgver`, `sha256sums` (from the release's `.sha256`) and `_commit` (from the
tarball's `SOURCE_RELEASE.md`), then refresh `.SRCINFO` and build:

```sh
cd packaging/arch
makepkg --printsrcinfo > .SRCINFO
makepkg
```

For 1.0.0, `makepkg` built the package in about a minute and a half on the development host, and the packaged desktop passed
`--smoke-test` offscreen. For 1.1.0, `makepkg -d` built it in 1 min 54 s, and the packaged `SOURCE.md` names the tag's commit.
Installing it with pacman was not part of either check.

**Portable package.** The regular package links the system's Qt and FFmpeg, so it runs only where those match the build host's
(Arch). `tools/build_portable.py` builds `tomacco-VERSION-Linux-x86_64-portable.tar.gz`, which bundles them, in a container:

```sh
tools/build_portable.py build
tools/build_portable.py verify build-portable/tomacco-1.0.0-Linux-x86_64-portable.tar.gz
```

`build` needs Docker and network access. It builds the image `tomacco-portable-builder:<digest>` from
`packaging/portable/Dockerfile` on Ubuntu 22.04, whose glibc 2.35 is the oldest the package needs. The image pins every download by
SHA-256: Qt 6.12.0 and Qt WebEngine from the official Qt binaries (`packaging/portable/qt-archives.txt`), FFmpeg 9.0.2 built as an
LGPL-only library with only the H.264 and MJPEG decoders, CMake 4.4.5, and Rust 1.89.0 through rustup. The build copies the
checkout's tracked and unignored files into `build-portable/work/`, builds there without tests, and adds `lib/tomacco/runtime`: Qt's
libraries, plugins (xcb, Wayland, offscreen, TLS, image formats), QML modules, QtWebEngineProcess with its resources, FFmpeg, and
the xcb helper libraries Qt's xcb plugin needs. The desktop finds them through its RUNPATH and `qt.conf`. The build stops if any file
needs a newer glibc than 2.35 or a host library outside its list; that list is in the package's `share/doc/tomacco/PORTABLE.md`.

`verify` unpacks the package in clean containers of Debian 12, Ubuntu 22.04, Ubuntu 24.04 and Fedora 44 (`--image` picks others).
In each it installs the host libraries, runs `tomaccoctl --version` and `tomaccoctl doctor`, checks that every bundled library
resolves, runs `tomacco --smoke-test` offscreen and on Xvfb, then installs with `install.py` into a scratch home, runs the
installed launchers and uninstalls. `install.py` copies `lib/tomacco/runtime` to `~/.local/lib/tomacco/runtime` and writes
`qt.conf` beside the program; installing a regular package later removes them. An installation from a portable package updates to
the next release's portable package.

For the 1.0.0 sources the package was about 176 MB (436 MB unpacked, 2182 files), and all checks passed in all four containers;
`install.py` took 26 to 100 seconds there, most of it journalling each file. Since then `install.py` journals and flushes once per
batch of files: installing the 1.1.0 portable package (2184 files) into a scratch home took 0.7 s instead of 15 s on the development
host (ext4 on NVMe), and 4.2 to 13 s instead of 27 to 84 s in a `debian:12` container on the same host, with the host busy. Nobody has
run it on a desktop of those distributions yet.

`install.py` and `cmake --install` install the same files, and the same documents into `share/doc/tomacco`: the guides and legal texts, in one flat directory, with
links adjusted and `SOURCE.md` naming the version and commit. The list is `DOCUMENTS` in `install.py`. Links to anything else point at the release's copy on GitHub.

## Installing

`install.py` installs for the current user. Once installed, two commands cover the rest:

```sh
tomaccoctl update
tomaccoctl uninstall
```

`tomaccoctl update` checks GitHub for a newer release as your own request (`update.check` with `manual`) and, if there is one, installs it
with `update.apply` and waits for the job; `--check` only checks. `tomaccoctl uninstall` runs `install.py --uninstall` from the copy every
installation keeps in `~/.local/lib/tomacco/install.py`, and prints the command that undoes it. Installations made by 1.0.0 have no such copy:
run `python3 install.py --uninstall` from the release package instead. Both commands refuse a source checkout and a system package; pacman
updates and removes the Arch package.

| What | Where |
| --- | --- |
| Program | `~/.local/lib/tomacco/tomacco` |
| A copy of the installer, for `tomaccoctl uninstall` | `~/.local/lib/tomacco/install.py` |
| Launchers | `~/.local/bin/tomacco`, `~/.local/bin/tomaccoctl` |
| Menu entry and icon | `~/.local/share/applications/`, `~/.local/share/icons/` |
| Documents | `~/.local/share/doc/tomacco/` |
| Window rules, with `--omarchy` | `~/.config/hypr/tomacco.lua`, required from `hyprland.lua` |
| App preferences | `~/.config/marselnenaj/Tomacco.conf` |
| Data, profiles, job history | `~/.local/share/tomacco`, or an absolute `TOMACCO_HOME` |

| Option | What it does |
| --- | --- |
| `--binary` | Selects the desktop build (default: `build/tomacco`, or `bin/tomacco` in a release tarball) |
| `--cli` | Selects a separate `tomaccoctl` build (default: next to the desktop binary) |
| `--omarchy` | Adds the Omarchy panel and Hyprland rules |
| `--no-window-rule` | Leaves the Hyprland configuration alone |
| `--user-home` | Stages an installation into another home |
| `--update` | Keeps the integrations as they are: refreshes the Omarchy panel and window rules only where they are in use (what an update passes) |
| `--list-backups` | Lists recovery points |
| `--rollback ID` | Restores one, after verifying all current files; refuses to overwrite your later edits, and makes its own backup |
| `--uninstall` | Removes what the installs wrote, while each file is still as installed; files you changed are kept and listed |
| `--version` | Prints the version it installs |

Every replaced file is backed up under `~/.local/share/tomacco/install-backups/<timestamp>/`, and every change is journalled before it is made.
`install.py` stages up to 512 files beside their destinations, writes the journal, gets the staged files, the backups and the journal onto
the disk with one `syncfs` (one `fsync` per file where that is missing), and only then renames them into place. After a crash or a kill,
each file is either as before or as installed: `--rollback ID` accepts both, `--uninstall` finishes an interrupted uninstall, and the next
run deletes the `.tomacco-staged-*` files a killed run left. An uninstall
undoes its edits to `hyprland.lua` and `shell.json`, never touches `~/.local/share/tomacco`, and is itself journalled, so `--rollback ID` brings the
installation back. An uninstall also leaves your preferences (`~/.config/marselnenaj/Tomacco.conf`) and caches (`~/.cache/marselnenaj/Tomacco`); delete those and
`~/.local/share/tomacco` by hand to remove every trace.

## Releases and updates

A release holds the package and the portable package, each with its `.sha256` and its `.sig`. It starts as a draft ([making a release](#making-a-release)), and publishing it is a person's decision; drafts and pre-releases are
never offered as updates.

Releases are signed, and `update.apply` installs a package only when its detached Ed25519 signature verifies against the public key compiled into `tomaccoctl`. A build without
that key refuses to install updates and says so.

`native/product.rs` holds the product name, the repository and the installed paths the updater uses, so a rename changes them in one place. An update runs the package's own `install.py` with
`--update`, which keeps the integrations as they are, so the journal, the backups and `--rollback` work as for any installation. `tests/update_e2e.rs` runs the whole path against a local
fixture with a test key; the hooks it uses exist only in debug builds.

## Checks before a pull request

There is no hosted CI. Run the [test commands](#test) locally. In a disposable container that runs as root and has no GPU, set `QTWEBENGINE_DISABLE_SANDBOX=1` and
`QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu` for the tests only.

The iOS engine and guest tools are separate local builds: a guest-tools build needs a local linker and clang 22, and `--cpu-optimization native` fits only the CPU that built it. Tests that need an engine, the iOS
linker with `ldid`, `llvm-mc` or `llvm-objcopy`, or an ARM64 execution engine skip and say so.

## Making a release

A release is a draft until you publish it. Before tagging:

1. Set the version in `CMakeLists.txt` and `Cargo.toml`, the `<release>` in the metainfo, and `CHANGELOG.md`.
2. Update `docs/RELEASE_NOTES.md`. The tools refuse notes that still hold the comment `numbers: final measurement pending`.

Releases are made locally, not by a workflow. From a clean checkout of the tag, `tools/make_release.py` runs the checks, builds the package, the
[portable package](#packaging) (built and verified with `tools/build_portable.py`; `--no-portable` leaves it out) and the source tarball with their
checksums, signs both packages and creates a draft release whose body comes from `docs/RELEASE_NOTES.md`, with relative links pointing into the tag. It refuses a dirty tree, a checkout that is not
the tag, and versions that disagree. With `--dry-run` it only prints what it would do.

`--with-qt-sources` also puts the source tarballs of the Qt modules the portable package bundles into the release, each with its
`.sha256`, as the LGPL source offer for them. `packaging/portable/qt-sources.txt` pins their URLs and SHA-256 digests: qtbase,
qtdeclarative, qtsvg, qtwayland, qttranslations, qtpositioning and qtwebchannel from Qt's 6.12.0 submodules. Qt WebEngine is not among
those submodules; Qt ships it as an extension of its online installer, and the source it publishes beside the bundled build
(`6.12.0-0-202607081027`) is a 583 MB tarball named `qtwebengine-everywhere-src-6.12.0-beta2.tar.xz`. That tarball does not record
its commit, so it could not be matched against the commit the binary's SBOM names. Together the tarballs are about 680 MB.

**Set by hand on GitHub.** The repository's social preview is `assets/readme/social-preview.png` (1280x640, the banner on a 2:1 canvas). Upload it under Settings, General,
Social preview.

## Reproducible engine builds

The virtual iOS device's engine is built from pinned sources by `tools/build_ios_paravirt.py`; [setup](virtual-ios/setup.md#the-chain) has the normal build. Results go to
`.tooling/ios-paravirt/`.

To reproduce an engine digest, these options remove what would make two builds differ. The build's manifest records what each one did.

| Option | What it fixes |
| --- | --- |
| `--configure-pins PATH` | QEMU's host-detected features, from a configured build directory or a `configure-pins.json`. The project's pins are in `integrations/ios-emulator/qemu-configure-pins.json`. |
| `--file-prefix-map FROM=TO` | Source paths embedded in the binary, recorded under TO for both C and Rust. |
| `--link-translator` | Cargo's crate metadata for the shader translator. |
| `--expect-engine-sha256 HEX` | Checks the result: the manifest records whether the digest matches, and the build fails after publication if not. |
| `--offline` | Requires local pinned sources and disables Cargo and QEMU downloads. |

Other options:

- `--cpu-optimization native` is what the numbers were measured on, and fits only the CPU that built it. `portable` is the default.
- `--experimental-patch NAME` adds an optional patch that the default series leaves out.
- `--pgo generate`, then `--pgo use --pgo-data DIR`, makes a profile-guided build, trained on your own session. `--expect-engine-sha256` reproduces the default build, not
  one that depends on such a profile.

Measured with GCC 16.2.1, rustc 1.99.1 and Meson 1.10.2 on an i9-13900K: one build takes about 5.5 minutes after its clones. Two builds in different directories gave one
digest.
