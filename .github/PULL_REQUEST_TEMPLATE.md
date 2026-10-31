## What and why

<!-- What changes for a user or an agent, and why. Link the issue if there is one. -->

## How it was checked

<!-- The tests you added or ran. For anything that needs a device or an engine: what you ran on it and what you saw. -->

## Checklist

- [ ] `cargo fmt --check`, `cargo clippy --locked --all-targets -- -D warnings` and `ctest` pass
- [ ] New behaviour has a test
- [ ] Documentation and `CHANGELOG.md` say what changed; a limit is stated as a limit
- [ ] New user-visible strings are in English with a German translation
- [ ] Nothing from Apple firmware, an iOS image, a key or a ticket is included
