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

**Dependency policy.** `python3 tools/check_dependencies.py` checks the Rust dependencies offline, from `Cargo.lock` and the crates in
Cargo's cache or `vendor/`. It fails on a crate whose license does not allow distribution with Tomacco under GPL-2.0-or-later, on an
Apache-2.0-only crate in `tomaccoctl` that [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) does not name, on a crate locked in two
semver-incompatible versions other than the four it lists with their reasons (`base64`, `foldhash`, `hashbrown` and `syn`, each needed in two
versions by different dependencies), on a version Cargo's local index cache marks as yanked, and when the crate tables of the notices and
`Cargo.lock` disagree. When [cargo-deny](https://github.com/EmbarkStudios/cargo-deny) is installed it also runs `cargo deny --offline check`
with `deny.toml`. `make_release.py` runs it after Clippy. On the development host with cargo-deny 0.20.2, it found 133 crates in the Linux
build, 112 of them in `tomaccoctl`, and no problem.

**Security advisories.** When [cargo-audit](https://github.com/rustsec/rustsec/tree/main/cargo-audit) is installed,
`tools/check_dependencies.py` also runs `cargo audit --no-fetch` against the local copy of the [RustSec](https://rustsec.org) database, so a
vulnerability it reports fails the check and `make_release.py` refuses the release. It fetches nothing; to install it and get or refresh
the database:

```sh
cargo install cargo-audit --locked
cargo audit
```

Without cargo-audit, cargo-deny's advisory check covers the same database: its `cargo deny check` reads the copy cargo-deny keeps in
`~/.cargo/advisory-dbs`. On 1 November 2026, with that copy at RustSec commit `2295466` (30 October 2026), it reported no advisory for
any locked version; 30 of the crates have advisories in the database, all for other versions. cargo-audit was not installed there, so
`cargo audit` itself has not run.

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

## The documentation on the website

`tools/build_site_docs.py` turns the Markdown in `docs/` into the static pages served at
`tomacco.dev/docs`: the guides, the virtual iOS device pages and the operation reference. It
uses only the Python standard library and writes no script. Regenerate the pages after any
change under `docs/`, with the website checkout's `docs/` directory as the output:

```sh
python3 tools/build_reference.py
python3 tools/build_site_docs.py ../tomacco-site/docs --sitemap ../tomacco-site/sitemap.xml
```

The command fails, and says which page, when a link or anchor does not resolve. Links to files
that are not documentation point at GitHub; `--ref` sets the branch or tag they use (default
`main`). The stylesheet for these pages is the "Documentation pages" section of the website's
`assets/site.css`. `python3 -m unittest tests.python.test_build_site_docs` checks that every
document renders, that every link and anchor in the output resolves and that no page has a script.

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
installs an update. It holds `bin/`, `lib/`, `share/` and `install.py`. `bin/tomaccoctl _install` installs for one user what it finds beside
it, with the [options below](#installing); `install.py` takes the same options and hands over to it.
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
`--smoke-test` offscreen. For 1.1.0, `makepkg -d` built it in 1 min 54 s, and the packaged `SOURCE.md` names the tag's commit. For 1.2.1, the
downloaded tarball matched the release's `.sha256`, `makepkg -d` built it in 2 min 55 s while another build ran, the packaged
`SOURCE.md` names the tag's commit and the packaged `tomaccoctl --version` printed 1.2.1.
Installing it with pacman was not part of these checks.

**Portable package.** The regular package links the system's Qt and FFmpeg, so it runs only where those match the build host's
(Arch). `tools/build_portable.py` builds `tomacco-VERSION-Linux-x86_64-portable.tar.gz`, which bundles them, in a container:

```sh
tools/build_portable.py build
tools/build_portable.py verify build-portable/tomacco-1.2.1-Linux-x86_64-portable.tar.gz
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
installed launchers and uninstalls. The installer copies `lib/tomacco/runtime` to `~/.local/lib/tomacco/runtime` and writes
`qt.conf` beside the program; installing a regular package later removes them. An installation from a portable package updates to
the next release's portable package.

For the 1.0.0 sources the package was about 176 MB (436 MB unpacked, 2182 files), and all checks passed in all four containers;
`install.py` took 26 to 100 seconds there, most of it journalling each file. Since then `install.py` journals and flushes once per
batch of files: installing the 1.1.0 portable package (2184 files) into a scratch home took 0.7 s instead of 15 s on the development
host (ext4 on NVMe), and 4.2 to 13 s instead of 27 to 84 s in a `debian:12` container on the same host, with the host busy. Nobody has
run it on a desktop of those distributions yet. With `tomaccoctl _install` and the 1.2.1 portable package (2201 files), a `debian:12`
container without Python installed it in 0.9 to 1.2 s, uninstalled it in 0.7 s and rolled the uninstall back in 1.2 to 1.8 s; the 1.2.1
`install.py` took 1.0 to 1.7 s, 0.9 to 1.1 s and 1.5 to 2.0 s in the same container with Python 3.11 added. On the development host
the install took 1.1 s against 1.7 s (medians of three); on a busy disk `syncfs` takes most of either.

**The aarch64 portable package.** `build --arch aarch64` builds `tomacco-VERSION-Linux-aarch64-portable.tar.gz` the same way
for `linux/arm64`, into `build-portable-aarch64/`:

```sh
tools/build_portable.py build --arch aarch64
tools/build_portable.py verify build-portable-aarch64/tomacco-1.2.1-Linux-aarch64-portable.tar.gz
```

Its image, `tomacco-portable-builder-aarch64:<digest>`, comes from `packaging/portable/Dockerfile.aarch64` and
`packaging/portable/qt-archives-aarch64.txt`: Qt's official aarch64 binaries of the same 6.12.0 builds as the x86_64 ones, Qt
WebEngine included, so `qt-sources.txt` is their source too, and the same FFmpeg, CMake and Rust. Qt builds those binaries on
Ubuntu 24.04 and they need glibc 2.38 and `GLIBCXX_3.4.32`, so this builder is Ubuntu 24.04 and the package needs glibc 2.39: it runs on
Ubuntu 24.04, Debian 13, Fedora 40 and newer, not on Debian 12 or Ubuntu 22.04. `verify` checks it in `linux/arm64` containers of
Debian 13, Ubuntu 24.04 and Fedora 44. On an x86_64 host Docker runs those containers under QEMU emulation, which needs
`binfmt_misc` with `qemu-aarch64` registered (`docker buildx ls` then lists `linux/arm64`). Unlike
the x86_64 build, Qt WebEngine's aarch64 build links Opus, Snappy, WebP, Minizip and Little CMS from Ubuntu 24.04 instead of
carrying Chromium's own copies, so the package bundles those from the builder, each with its Debian copyright file, and its xcb
plugin also expects libSM and libICE from the host.

For the 1.2.1 sources on a 32-thread x86_64 workstation, all under emulation: the builder image took about 12 minutes (2.9 GB),
compiling about half an hour, and `verify` 22 minutes for the three containers, all of whose checks passed. The package was
175 MB (449 MB unpacked, 2326 files) and needs glibc 2.39 and `GLIBCXX_3.4.32`. In a `debian:12-slim` container (glibc
2.36) every program check failed on the missing `GLIBC_2.39`, as expected. On the M1 MacBook Air with Arch Linux ARM, in a
scratch home, `tomaccoctl --version` and `doctor`, the library check, `--smoke-test` offscreen (21 s), `install.py`, the
installed launchers and the uninstall all passed. With Docker on an aarch64 host the same commands run natively.

**Why there is no Flatpak or AppImage.** Both were considered for the distributions the regular package does not fit; the
portable package serves them instead. Tomacco's work happens in programs on the host: `tomaccoctl` runs `adb`, the Android emulator
(which needs `/dev/kvm`), Gradle and a JDK, `xtool` and Swift, `usbmuxd` for iPhones, and QEMU-based engines it builds into the
checkout's `.tooling`. Coding agents run `tomaccoctl` from their own processes, by a path `tomaccoctl setup` writes into their
settings files in the home directory.

- *Flatpak.* Inside the sandbox none of those programs exists. Reaching them takes `flatpak-spawn --host` for every command, which needs
  the `org.freedesktop.Flatpak` bus name and removes the sandbox anyway, plus device access for KVM and USB and the whole home for
  projects and agent settings. Agents would also have to call `flatpak run io.github.marselnenaj.tomacco` instead of `tomaccoctl`,
  and Flatpak's own updates would replace the signed updates `tomaccoctl update` makes.
- *AppImage.* It has no sandbox, so the host tools work, but it is one file mounted at a new path on every start: the path agents keep
  for `tomaccoctl` would point into a mount that is gone, so the programs would still have to be copied out. It also needs FUSE on
  the host. Copying them out with a menu entry, uninstall and rollback is what the portable package and `install.py` already do.

So there is no prototype of either. These reasons come from what Tomacco runs; neither format was built or tried.

The installer and `cmake --install` install the same files, and the same documents into `share/doc/tomacco`: the guides and legal texts, in one flat directory, with
links adjusted and `SOURCE.md` naming the version and commit. The list is `DOCUMENTS` in `install.py`. Links to anything else point at the release's copy on GitHub.

## Installing

The package's own `tomaccoctl` installs it for the current user, without Python:

```sh
cd tomacco-VERSION-Linux-x86_64
bin/tomaccoctl _install --omarchy
```

`python3 install.py` from the package does the same: it hands over to `bin/tomaccoctl _install`, so older copies of the one-line installer
and updaters of 1.2.1 and earlier, which run `install.py`, install a new package with it. From a checkout, `install.py` installs with
its own Python code, because it generates the documents it installs from their sources (`--binary` and `--cli` point it at the build).
It also stands in where it finds no `tomaccoctl` that installs; `TOMACCO_INSTALL_PYTHON=1` makes it install with Python anyway. Both
installers write the same files, journal and backups, so either rolls back or uninstalls what the other installed.

Once installed, two commands cover the rest:

```sh
tomaccoctl update
tomaccoctl uninstall
```

`tomaccoctl update` checks GitHub for a newer release as your own request (`update.check` with `manual`) and, if there is one, installs it
with `update.apply` and waits for the job; `--check` only checks. `tomaccoctl uninstall` uninstalls in its own process, also an
installation made by 1.0.0 or by `install.py`, and prints the command that undoes it: the removed `tomaccoctl`, kept in the
uninstallation's backup, run as `_install --rollback ID`. Both commands refuse a source checkout and a system package; pacman updates and
removes the Arch package.

| What | Where |
| --- | --- |
| Program | `~/.local/lib/tomacco/tomacco` |
| A copy of `install.py`, for older updaters and scripts; it hands over to the `tomaccoctl` beside it | `~/.local/lib/tomacco/install.py` |
| Launchers | `~/.local/bin/tomacco`, `~/.local/bin/tomaccoctl` |
| Menu entry and icon | `~/.local/share/applications/`, `~/.local/share/icons/` |
| Documents | `~/.local/share/doc/tomacco/` |
| Window rules, with `--omarchy` | `~/.config/hypr/tomacco.lua`, required from `hyprland.lua` |
| Agent skill | `~/.local/lib/tomacco/skills/tomacco`, linked from `~/.claude/skills`, `~/.codex/skills` and `~/.agents/skills` where `~/.claude`, `~/.codex` or `~/.agents` exists ([agent skill](agents.md#agent-skill)) |
| App preferences | `~/.config/marselnenaj/Tomacco.conf` |
| Data, profiles, job history | `~/.local/share/tomacco`, or an absolute `TOMACCO_HOME` |

| Option | What it does |
| --- | --- |
| `--binary` | Selects the desktop build (default: `build/tomacco`, or `bin/tomacco` in a release tarball) |
| `--cli` | Selects a separate `tomaccoctl` build (default: next to the desktop binary) |
| `--omarchy` | Adds the Omarchy panel and Hyprland rules, and Tomacco's entries in Omarchy's menu |
| `--omarchy-bindings` | Adds Tomacco's keys as a Hyprland bindings file, `~/.config/hypr/tomacco-bindings.lua` |
| `--no-omarchy-menu` | With `--omarchy`, leaves Omarchy's menu as it is |
| `--no-window-rule` | Leaves the Hyprland configuration alone |
| `--no-agent-skill` | Links the agent skill into no agent's skills directory, and removes the links an earlier installation made |
| `--user-home` | Stages an installation into another home |
| `--update` | Keeps the integrations as they are: refreshes the Omarchy panel, window rules, keys and menu entries only where they are in use, and adds no agent skill link you removed (what an update passes) |
| `--list-backups` | Lists recovery points |
| `--rollback ID` | Restores one, after verifying all current files; refuses to overwrite your later edits, and makes its own backup |
| `--uninstall` | Removes what the installs wrote, while each file is still as installed; files you changed are kept and listed |
| `--version` | Prints the version it installs |
| `--package DIR` | `tomaccoctl _install` only: installs the unpacked package in `DIR` instead of the one this `tomaccoctl` is in |

Every replaced file is backed up under `~/.local/share/tomacco/install-backups/<timestamp>/`, and every change is journalled before it is made.
The installer stages up to 512 files beside their destinations, writes the journal, gets the staged files, the backups and the journal onto
the disk with one `syncfs` (one `fsync` per file where that is missing), and only then renames them into place. After a crash or a kill,
each file is either as before or as installed: `--rollback ID` accepts both, `--uninstall` finishes an interrupted uninstall, and the next
run deletes the `.tomacco-staged-*` files a killed run left. `native/install.rs` has unit tests for each of these, and in a debug build
`TOMACCO_INSTALL_TEST_BATCH` and `TOMACCO_INSTALL_TEST_KILL_AT=N` (exit at the Nth rename or unlink) let `tests/python/test_install.py`
kill a real run. An uninstall
undoes its edits to `hyprland.lua` and `shell.json`, removes the agent skill links, never touches `~/.local/share/tomacco`, and is itself journalled, so `--rollback ID` brings the
installation back. An uninstall also leaves your preferences (`~/.config/marselnenaj/Tomacco.conf`) and caches (`~/.cache/marselnenaj/Tomacco`); delete those and
`~/.local/share/tomacco` by hand to remove every trace.

## Releases and updates

A release holds the package and the portable package for x86_64, and when it is made with the [aarch64 builds](#making-a-release)
the same two for aarch64, each with its `.sha256`, its `.sig`, and a software bill of materials
(`.cdx.json`) with its own `.sig`, and a signed [provenance](#making-a-release) of the whole release. It starts as a draft ([making a release](#making-a-release)), and publishing it is a person's decision; drafts and pre-releases are
never offered as updates.

Releases are signed, and `update.apply` installs a package only when its detached Ed25519 signature verifies against a public key compiled into `tomaccoctl`: the current
release key and, while the key is being [rotated](#rotating-the-release-key), the next one. A build without a key refuses to install updates and says so.

The updater takes the package of its own architecture. An installation from a portable package takes only the next portable
package. One from a regular package takes the next regular package, or the portable one when the release has no regular package
for its architecture; on aarch64 away from Arch Linux ARM (no `pacman`) it prefers the portable package, as the installer does.

`native/product.rs` holds the product name, the repository and the installed paths the updater uses, so a rename changes them in one place. An update runs the package's own
`bin/tomaccoctl _install --update`, which keeps the integrations as they are, so the journal, the backups and rollback work as for any
installation; `update.rollback` and the repair of an interrupted update read the journal in the running `tomaccoctl`. None of this
needs Python. `tests/update_e2e.rs` runs the whole path, and `tomaccoctl uninstall` with its undo command, against a local fixture with
a test key; the hooks it uses exist only in debug builds.

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

**ARM64 packages.** With `--arm-host` the release also gets the regular `tomacco-VERSION-Linux-aarch64.tar.gz`, built on an ARM build host over
SSH; with `--arm-portable`, the [aarch64 portable package](#packaging) as well:

```sh
tools/make_release.py --key KEY --arm-host builder@arm-laptop --arm-path '~/.cache/tomacco-buildtools/bin' --arm-portable
```

The host is a `user@host` or an `ssh_config` name (or `TOMACCO_ARM_BUILD_HOST`), reached with `ssh -o BatchMode=yes`, so its key
must already be loaded in your SSH agent. It needs what [a build from a checkout](getting-started.md) needs: Qt 6.9 or newer,
FFmpeg, Rust, CMake, Ninja and a C++ compiler; no root. `--arm-path` (or `TOMACCO_ARM_BUILD_PATH`) puts directories in front of
its `PATH`, `~/` meaning its home, for a CMake and Ninja installed with `pip` into a virtual environment. After the source
tarball is made, `make_release.py`:

1. checks that the host is aarch64 and empties `~/tomacco-build-release-VERSION` there (`--arm-directory` picks another
   directory below the home);
2. copies the release's own source tarball there and checks its SHA-256 on the host before unpacking it;
3. configures and builds with `cmake -G Ninja -DCMAKE_BUILD_TYPE=Release` and `SOURCE_DATE_EPOCH` set to the release commit's
   time, inside the unpacked tree, where Cargo finds the vendored crates;
4. runs `ctest -R '^(backend|desktop-ui|native-display)$'` there (`--arm-tests` takes another expression), repeating a failed
   test once, then `--target package`;
5. fetches the package and its `.sha256` into `build-aarch64/`, and from there treats it like the x86_64 package: moves it into
   `dist/`, checks the checksum, signs it, writes and signs its SBOM, and lists it in the provenance and the draft.

The provenance records that the aarch64 package came from a build host and that host's system, kernel, rustc, Cargo, CMake,
Ninja and compiler versions, but not its name. `--dry-run` prints each `ssh` and `scp` command without connecting.
On the 1.2.1 source tarball and an M1 MacBook Air with Arch Linux ARM (8 cores, 8 GB, no root, CMake and Ninja from a virtual
environment), the steps from copying the tarball to fetching the package took 301 s, including 81 s of tests, and the
package was 8.2 MB. In the run before, three desktop input tests failed in the full suite; they passed alone and in the next
full run, which is why a failed test gets one retry. The package's digest differed from a build of the same tarball in
another directory, as the binaries hold the paths they were built in.

**Software bills of materials.** After signing, `make_release.py` writes `PACKAGE.cdx.json` beside each package with
`tools/build_sbom.py` and signs it the same way. It is CycloneDX 1.6 JSON: the package with its SHA-256; the crates linked into
`tomaccoctl`, from `cargo tree` and `Cargo.lock`, with version, SPDX license, package URL and crates.io checksum; the SQLite they
embed; the bundled fonts; and for the portable package each Qt module and ICU with the SHA-256 of its archive from
`packaging/portable/qt-archives.txt`, FFmpeg with its tarball's SHA-256 from the Dockerfile, and the xcb-util libraries. Python is not
listed, since neither package contains it. Qt's own SBOMs for its modules stay in the portable package's `lib/tomacco/runtime/sbom/`.
The time stamp is `SOURCE_DATE_EPOCH`, so the same package gives the same file. `tests/python/test_build_sbom.py` checks the shape
against the parts of the CycloneDX schema it uses; no CycloneDX validator was run on it.

**Provenance.** Last, `make_release.py` writes `tomacco-VERSION-provenance.intoto.json` and signs it like a package: an
[in-toto](https://in-toto.io) statement with a [SLSA](https://slsa.dev) provenance v1 predicate (`tools/build_provenance.py`). Its subjects are
every file of the release except checksums and signatures, with their SHA-256. It records the tag and commit, `SOURCE_DATE_EPOCH`,
the digest of `Cargo.lock`, for the portable package the digests of its Dockerfile, base image, Qt archives, CMake, rustup and FFmpeg
tarballs, the builder's operating system, kernel and the versions of Python, rustc, Cargo, CMake, Ninja, the C++ compiler, OpenSSL,
Git and Docker, and when the run started and ended. It is a JSON file with a detached signature, not a signed DSSE envelope, and it
states what the maintainer's computer reported; nobody else attests it. `tomaccoctl verify` checks its signature and then each listed
file in the same directory:

```sh
tomaccoctl verify tomacco-1.2.0-provenance.intoto.json
```

The builds run with `SOURCE_DATE_EPOCH` set to the release commit's time, which CPack and Qt's `rcc` use for their time stamps.
Two builds of the same commit from fresh clones in the same directory, by the same user, gave the same package digest; without it,
the tarball's times and the time `rcc` stored for the generated translations differed. The package still records the builder's
user name, and the binaries hold the paths they were built in, so builds in other directories or by other users differ. Two runs of
`tools/build_source_release.py` on the v1.1.0 tag gave the published source tarball's digest both times.

`--with-qt-sources` also puts the source tarballs of the Qt modules the portable package bundles into the release, each with its
`.sha256`, as the LGPL source offer for them. `packaging/portable/qt-sources.txt` pins their URLs and SHA-256 digests: qtbase,
qtdeclarative, qtsvg, qtwayland, qttranslations, qtpositioning and qtwebchannel from Qt's 6.12.0 submodules. Qt WebEngine is not among
those submodules; Qt ships it as an extension of its online installer, and the source it publishes beside the bundled build
(`6.12.0-0-202607081027`) is a 583 MB tarball named `qtwebengine-everywhere-src-6.12.0-beta2.tar.xz`. That tarball does not record
its commit, so it could not be matched against the commit the binary's SBOM names. Together the tarballs are about 680 MB.

**Set by hand on GitHub.** The repository's social preview is `assets/readme/social-preview.png` (1280x640, the banner on a 2:1 canvas). Upload it under Settings, General,
Social preview.

## Rotating the release key

`native/product.rs` holds two public keys: `RELEASE_SIGNING_PUBLIC_KEY_BASE64`, the key that signs releases now, and
`NEXT_RELEASE_SIGNING_PUBLIC_KEY_BASE64`, empty except during a rotation. `tomaccoctl` accepts a package signed by either, and so does
the container build, whose `public_keys` in `packaging/container/Dockerfile` a test keeps equal to them. `make_release.py` accepts a
signing key whose public half is one of the two. To replace the key:

1. Make a new Ed25519 key on the machine that signs releases, outside the repository and readable only by its owner, like the
   current one.
2. Put base64 of its 32-byte public half into `NEXT_RELEASE_SIGNING_PUBLIC_KEY_BASE64` and into `public_keys` in the container's
   Dockerfile, after the current key. Update `install.sh` on the website the same way; it is not in this repository. Make a release
   as usual, signed with the current key. Every copy that updates to it trusts both keys.
3. Once that release has been out for a while, move the new key into `RELEASE_SIGNING_PUBLIC_KEY_BASE64`, empty
   `NEXT_RELEASE_SIGNING_PUBLIC_KEY_BASE64`, drop the old key from the Dockerfile and `install.sh`, and sign the next release with the new
   key.

A copy older than the release of step 2 does not know the new key: its update refuses the package with `update_signature_invalid` and
installs nothing, and its user installs the new release with `install.sh` or by hand. If the old private key may have leaked, skip the
waiting in step 3, since every copy still trusts the old key until it updates.

The tests in `native/update_tests.rs` check that a package signed by the next key is accepted and one signed by an unknown key is
refused; `tests/python/test_make_release.py` checks that `make_release.py` accepts the next key and still requires the current one.

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
