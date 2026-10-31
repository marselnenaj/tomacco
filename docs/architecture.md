# Architecture

How the repository fits together, for someone about to change it.

## The one idea

Every capability is a **typed operation**. A person at the desktop, an agent over
MCP and a script using the CLI all call the same operations, so nothing can do
more than the others, and long work runs as a durable job that survives its client.

```text
Qt desktop (qml/, src/)     tomaccoctl CLI     stdio MCP (native/mcp.rs)
          \                       |                  /
           `--------- native/api.rs dispatch --------'
                             |   arguments validated against
                             |   assets/operations.json
              ___________________|_________________
             |              |                      |
        native/*.rs    native/jobs.rs        tools/*.py
        (operations)   (durable jobs)        (recipes a job runs)
```

## Where things live

| Path | What it is |
| --- | --- |
| `assets/operations.json` | The schema of every operation: name, description, arguments. It is the contract. |
| `native/` | The Rust control plane, `tomaccoctl`. `api.rs` dispatches, `jobs.rs` runs durable jobs, `mcp.rs` serves MCP, one module per platform area. |
| `native/ios_emulator/` | Control of the virtual iOS device: the one-step phone, its guest channel, its frames. |
| `src/`, `qml/` | The desktop: C++/Qt backend and native display, QML interface. |
| `i18n/` | German, French and Spanish translations of the desktop. English is the source language. |
| `tools/` | Python recipes that long jobs run: engine and guest-tool builds, boot and stop of the virtual phone, measurements. |
| `integrations/ios-emulator/` | Guest programs in C, and the ordered patch series applied to the pinned QEMU fork, renderer and shader translator. |
| `integrations/omarchy/` | The Omarchy panel and desktop integration. |
| `tests/python/` | The control-plane suite. `tests/*.cpp`: desktop and display. `tests/*_integration.py`: live checks that need a device or an engine. |
| `docs/` | The guides. |

The engine sources are not in this repository. `tools/build_ios_paravirt.py`
fetches pinned upstream revisions and applies the series from
`integrations/ios-emulator`, in the order listed in that tool.

## Adding an operation

1. Describe it in `assets/operations.json`. Schemas are strict: unknown arguments are refused.
2. Implement it in the area's module in `native/` and add it to the match in `native/api.rs`.
3. Work that takes more than moments becomes a job, so that it reports progress, can be cancelled and outlives its client.
4. Test it in `tests/python/` or as a Rust unit test, and document it in [the agent guide](agents.md).
5. If people should reach it too, add it to the desktop through its backend.

## The virtual iOS device

The longest path in the project. Nothing from Apple is in this repository; the
user supplies their own restore image.

```text
restore image -> prepared disks and kernel -> one-step start
                                                 |
engine (patched QEMU, TCG on x86_64) <-----------+
  |-- tomacco-vphone machine: six application processors and a SEP
  |-- reims-vgpu: Apple's paravirtual GPU in Rust on Vulkan; Metal shaders go
  |   through the metal2vulkan translator
  `-- guest tools (integrations/ios-emulator/*.c): control service, setup
      helpers and diagnostics, reached over virtio-vsock
```

`tools/ios_virtual_iphone.py` runs a start and a stop; `tools/run_ios_paravirt.py`
supervises the engine. [How it works](virtual-ios/how-it-works.md) has the
details.

## What you can work on without what

| You have | You can work on |
| --- | --- |
| Linux, Rust, Python | The control plane, its operations and tests |
| Qt 6 as well | The desktop and its translations |
| An Android SDK and device or emulator | Android and Swift on Android |
| An iPhone and an Apple account | The Mac-free iPhone route |
| Your own iOS 27 restore image and a Vulkan GPU with 16 GiB | The virtual iOS device |

Patches and guest programs have unit tests that need neither an image nor a GPU,
and tests that need something they do not find skip and say what it is.
