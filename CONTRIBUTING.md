# Contributing

Issues and pull requests are welcome. Larger changes are best discussed in an
issue first.

[docs/architecture.md](docs/architecture.md) explains how the repository fits
together, how to add an operation, and what you can work on with which hardware.
Please follow the [code of conduct](CODE_OF_CONDUCT.md).

## Good places to start

- Issues labelled `good first issue`.
- A translation fix in `i18n/`, or a clearer error message or document.
- A test for something that only has a live check today.

No iOS image is needed for most of the project, including the unit tests of the
iOS parts.

## Build and test

Install the build requirements first: [getting started](docs/getting-started.md#1-install) lists the
packages for Arch, Debian, Ubuntu and Fedora. Then, from a fresh clone:

```sh
git clone https://github.com/marselnenaj/tomacco.git
cd tomacco
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build
QT_QPA_PLATFORM=offscreen ctest --test-dir build --output-on-failure
./tomaccoctl --version
./tomaccoctl call device.list '{}'
```

`./tomaccoctl` in the checkout runs the `tomaccoctl` it finds in `build-product/`, `build/` or
`target/release/`, so you can call operations without installing anything. `build/tomacco` is the
desktop. With `QT_QPA_PLATFORM=offscreen`, the desktop tests need no display.

On the development host (Arch Linux, i9-13900K, Rust crates already in Cargo's cache), a fresh clone
of 1.2.1 took 2 s to configure, 3 min 39 s to build (2.0 GB in `build/`) and 7 min 27 s for `ctest`,
which runs one test group at a time; all five passed. Most of that is the Python suite, about 5 min.
`ctest -j8` in another build of the same tree took 5 min.

Before a pull request, also run:

```sh
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
```

`ctest` includes the Python suite in `tests/python`, which also checks the documentation: every
relative link and anchor, every `tomaccoctl call` line against the schema, and that the operation
reference in `docs/reference/` matches `assets/operations.json`. After changing an operation, run
`python3 tools/build_reference.py`. The documentation checks alone take about a second, from the
repository root:

```sh
python3 -m unittest tests.python.test_docs_links tests.python.test_docs_virtual_iphone tests.python.test_docs_reference
```

Tests that need a toolchain, an engine or a device skip and say what they need.
[docs/building.md](docs/building.md) has the other build, test and packaging commands,
[docs/agents.md](docs/agents.md) describes the typed operations,
[docs/desktop.md](docs/desktop.md) the desktop and
[docs/virtual-ios/](docs/virtual-ios/README.md) the virtual iOS device.
[docs/README.md](docs/README.md) lists every guide.

## Pull requests

- One topic per pull request, on a branch from `main`.
- The subject line says what changes for the user, in the imperative or as a
  plain statement; the body says why and what was measured.
- Add a line to `CHANGELOG.md` for anything a user notices.
- Run the checks above locally before you open a pull request. There is no hosted
  CI, so say in the pull request what you ran.

## Rules for changes

- Never commit Apple firmware, iOS images, Apple binaries, keys, tickets or
  anything taken from them, nor links to third-party copies. Users supply their
  own restore image. See [docs/legal.md](docs/legal.md).
- A claim in the documentation states what was measured or run. An accepted
  request is not a result.
- A new user-visible string goes through `qsTr` in English with its translation in
  every `i18n/tomacco_*.ts` file (German, French, Spanish and Italian); `tests/python/test_i18n.py`
  checks that none is missing.
- Engine patches go in `integrations/ios-emulator` and are listed in order in
  `tools/build_ios_paravirt.py`.

Contributions are licensed under GPL-2.0-or-later, the license of the project.
