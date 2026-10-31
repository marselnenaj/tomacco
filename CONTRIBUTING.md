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

```sh
cmake -S . -B build-product -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build-product
ctest --test-dir build-product --output-on-failure
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
```

Tests that need a toolchain, an engine or a device skip and say what they need.
[docs/agents.md](docs/agents.md) describes the typed operations,
[docs/desktop.md](docs/desktop.md) the desktop and
[docs/virtual-ios/](docs/virtual-ios/README.md) the virtual iOS device, and
[docs/building.md](docs/building.md) the build, test and packaging commands.
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
  every `i18n/tomacco_*.ts` file (German and French); `tests/python/test_i18n.py`
  checks that none is missing.
- Engine patches go in `integrations/ios-emulator` and are listed in order in
  `tools/build_ios_paravirt.py`.

Contributions are licensed under GPL-2.0-or-later, the license of the project.
