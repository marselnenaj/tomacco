# Research engines

Tomacco has a second, older runtime beside the [virtual iOS device](virtual-ios/README.md): a
**restore-shell engine** that boots a modified iOS 27.0.1 restore environment on x86_64
Linux and runs your own command-line programs in it, and a set of isolated laboratory
machines for firmware and controller research. None of this is a runtime for graphical
apps. Use the virtual iOS device for those.

Nothing from Apple ships with Tomacco. You supply the firmware, under its license. See the
[legal notes](legal.md).

## What it is

The engine is [jprx/qemu-sptm](https://github.com/jprx/qemu-sptm) at a pinned commit, whose
SPTM, TXM and Apple-specific CPU emulation supply the boot support.
[darwin-vm](https://github.com/jprx/darwin-vm), also pinned, supplies device-tree preparation
and prebuilt open-source guest utilities. Tomacco adds the Linux preparation pipeline, the Rust
process and console control, the typed operations, the evidence, timeouts, cancellation and
cleanup.

The tested device is the iPhone 16: `iPhone17,3`, board `d47ap`, A18/T8140, iOS 27.0.1
`24A446`. The guest is a restore environment with added command-line tools. It is not the
installed iPhone OS filesystem, and it has no graphical app runtime.

`sptm-boot-display.patch` adds an optional real guest boot framebuffer. It is not an Apple
display controller, Metal or UIKit.

## Build and prepare

You need x86_64 Linux, Python with venv and pip, Git, a C/C++ compiler, Make, CMake, Ninja,
pkg-config, the GLib, Pixman, zlib and libpng development files, libplist, OpenSSL, and a
readable Linux kernel image with matching build headers. That helper kernel must have
initramfs, VirtIO PCI and block, and devtmpfs built in, and must permit locally compiled
modules.

```sh
python3 tools/build_ios_emulator.py --backend sptm --jobs 16
python3 tools/prepare_ios_emulator.py --toolchain .tooling/ios-sptm --firmware /absolute/extracted-IPSW --output .tooling/ios-sptm/firmware-27.0.1
```

The input is an extracted IPSW, or a flat set holding `BuildManifest.plist` and its kernel,
device tree, SPTM, TXM, restore RAM disk and restore trust cache. `--board` selects another
manifest identity; only the device above has been tested. An output folder that exists is
refused rather than overwritten.

The build records pinned sources, flags and hashes in `build-manifest.json`, and preparation
records source and output hashes in `tomacco-ios.json`. Preparation builds `ldid` and
`linux-apfs-rw`, signs the guest utilities, merges their trust-cache entries, and starts a
**temporary, networkless Linux helper VM** that mounts only the copied APFS image. No host
root, host mount or host APFS module is used. The helper uses KVM when `/dev/kvm` is usable
and TCG otherwise; `--helper-accel kvm|tcg` makes that explicit. KVM accelerates this helper,
not the ARM iOS guest. Any APFS failure in the helper's log rejects the preparation.

## Your own native programs

[`examples/ios-restore-native`](../examples/ios-restore-native/README.md) holds a C and
Embedded Swift example with its Linux build recipe. Supply an LLVM `ld64.lld` with iOS support.

```sh
python3 tools/build_ios_native_example.py --linker /absolute/path/to/ld64.lld --output .tooling/my-ios-programs
python3 tools/prepare_ios_emulator.py --firmware /absolute/extracted-IPSW --payload .tooling/my-ios-programs/programs --output .tooling/ios-sptm/my-native-guest
```

The payload appears under `/tomacco` in the new guest. Preparation copies the files, signs the
copied Mach-O binaries, adds their hashes to the trust cache and records both the original and
the signed hashes. The limits are 4096 entries, 256 MiB and 32 directory levels. Symbolic
links, special files and control characters in names are refused. Entitlements a program
already has are kept, verified and recorded.

Run them with `ios.emulator.exec` and a script such as
`set -e\n/tomacco/hello-c\n/tomacco/hello-swift 100\n`. This shows native command-line execution,
not SwiftUI, Foundation or IPA compatibility.

## Operations

`ios.emulator.inspect` detects the engine, checks the firmware identity, confines component
paths and verifies every prepared hash before a job is submitted.

```sh
tomaccoctl call ios.emulator.inspect '{"engine":"/absolute/.tooling/ios-sptm/build/qemu-system-aarch64","firmware":"/absolute/.tooling/ios-sptm/firmware-27.0.1"}'
tomaccoctl call ios.emulator.probe '{"engine":"/absolute/.tooling/ios-sptm/build/qemu-system-aarch64","firmware":"/absolute/.tooling/ios-sptm/firmware-27.0.1","seconds":30}'
tomaccoctl call ios.emulator.exec '{"engine":"/absolute/.tooling/ios-sptm/build/qemu-system-aarch64","firmware":"/absolute/.tooling/ios-sptm/firmware-27.0.1","script":"set -e\nuname -a\nid\n","seconds":60}'
```

Use the returned job with `job.wait`, `job.logs` and `job.cancel`. Each call boots a fresh
guest, runs the diagnostic or your script, captures the exit status and output, and stops the
engine. Scripts of up to 32768 UTF-8 bytes go in short acknowledged base64 packets over a
private serial socket, and they execute inside iOS, not in a host shell. A nonzero guest exit
fails the job and keeps its report. A timeout is never reported as guest success.

Reports hold the engine and input hashes, CPU observations, QMP messages, `engine.log`,
`console.log` and the guest's output; `guest-output.bin` keeps the exact bytes. Cancelling kills
the engine and removes its temporary directories. Runs are serialized to bound memory use, and
the engine and console logs are limited to 8 MiB each.

`guest_boot_verified: true` means the **restore-shell environment** produced a verified command
response. `full_ios_boot_verified: false` and `qualified_for_app_development: false` exclude a
full iPhone runtime.

## Managed full-system runs

The desktop's **Open test system** dialog and the CLI manage the same full-system research
jobs. Build the engine and prepare the firmware, research inputs, a bootstrap with
`--platform-diagnostics` and an assembled APFS image with the recipes above. The tested layout
is a 32 GiB guest with a 24 GiB APFS image; you need the host memory for that and the space for
an independent image copy.

```sh
tomaccoctl call ios.emulator.prepare_research '{"firmware":"/absolute/restore-firmware","inputs":"/absolute/research-inputs","bootstrap":"/absolute/bootstrap","image":"/absolute/system.apfs","output":"/absolute/research-bundle"}'
tomaccoctl call job.wait '{"job_id":"JOB_ID","seconds":60}'
tomaccoctl call ios.emulator.research '{"engine":"/absolute/qemu-system-aarch64","firmware":"/absolute/research-bundle","seconds":360}'
tomaccoctl call ios.emulator.report '{"job_id":"JOB_ID"}'
tomaccoctl call job.cancel '{"job_id":"JOB_ID"}'
```

Keep polling the preparation until it has `succeeded`. It verifies all six input hashes, the
bootstrap's diagnostics and the firmware identity, makes private read-only copies and publishes
the bundle atomically, so a mismatch leaves no partial bundle.

Each runtime job rechecks the engine and inputs, makes a separate writable APFS copy, opens four
private serial channels and boots with bounded log budgets. A channel over its budget stops
recording and fails the job, while control stays responsive. Identical active requests reuse one
job, and a conflicting one is refused. The report and its artifacts remain after success, failure
and cancellation; read `job_state` as well, since a forced cancel may interrupt a report.

The parser requires ordered, unique records with the expected IOKit queries and both scheduled
snapshots. A partial observation, malformed evidence or a missing clean image flush fails the job.
`guest_observations_verified` speaks only for those observations, and `full_ios_boot_verified`,
`graphical_apps_verified` and `sep_functionality_verified` stay false. The desktop shows progress,
missing services, logs, the report folder and the boot framebuffer, which is not a working iOS app
display. `tests/ios_research_integration.py` is the live check.

## Isolated PMP firmware execution

This runs the original T8140 PMP firmware apart from the full-system guest, with the SPTM engine
from `tools/build_ios_emulator.py`. Supply the matching extracted firmware root and its
BuildManifest. The tool checks the manifest's PMP SHA-384, unwraps the uncompressed IM4P and makes
an ELF with the original Mach-O code, load addresses and entry. It changes nothing in your
extraction, downloads nothing from Apple, and never replaces an existing directory.

```sh
tomaccoctl call ios.emulator.prepare_pmp '{"firmware":"/absolute/extracted-firmware","manifest":"/absolute/BuildManifest.plist","board":"d47ap","output":"/absolute/new-pmp-bundle"}'
tomaccoctl call job.wait '{"job_id":"JOB_ID","seconds":30}'
tomaccoctl call ios.emulator.inspect_pmp '{"engine":"/absolute/qemu-system-aarch64","firmware":"/absolute/new-pmp-bundle"}'
tomaccoctl call ios.emulator.pmp '{"engine":"/absolute/qemu-system-aarch64","firmware":"/absolute/new-pmp-bundle","seconds":10}'
tomaccoctl call job.wait '{"job_id":"JOB_ID","seconds":30}'
tomaccoctl call ios.emulator.report '{"job_id":"JOB_ID"}'
```

Inspection revalidates the bundle and discovers the machine and the bus and system-register fault
traces. Execution rechecks the inputs, copies the ELF into a private directory and starts one EL1
ARM64 CPU with an explicit laboratory MIDR, RAM and counter policy. These settings are in the report,
and they are not a measured silicon identity.

The run shares the iOS admission lock: the same request returns the active job, and a conflicting
start fails with `ios_emulator_busy`. Observation is bounded to 1-60 seconds, logs to 8 MiB and decoded
faults to 64.

With the default CPU identity the firmware stops during CPU setup on an unsupported Apple register,
reported as `pmp_sysreg_fault`. The default `controller: "none"` has no ASC mapping, so reaching it
raises a real external abort, `pmp_bus_fault`. Those jobs end as `failed`, and expiry without a fault
reports `pmp_observation_timeout`. None of these means PMP boot or iOS success: `pmp_boot_verified`,
`power_services_verified`, `full_ios_boot_verified` and `graphical_apps_verified` stay false.

An experimental controller mode adds ASC configuration, KIC IRQ/FIQ arbitration, 512 external inputs,
mailbox masks and the real QEMU ARM timer outputs. The engine must expose `tomacco-pmp-asc-research`.
Reports save non-consuming `controller_state` observations.

```sh
tomaccoctl call ios.emulator.inspect_pmp '{"engine":"/absolute/qemu-system-aarch64","firmware":"/absolute/new-pmp-bundle","controller":"asc-v6"}'
tomaccoctl call ios.emulator.pmp '{"engine":"/absolute/qemu-system-aarch64","firmware":"/absolute/new-pmp-bundle","controller":"asc-v6","seconds":15}'
```

[pmp-research.md](../integrations/ios-emulator/pmp-research.md) has the image contract and CPU policy,
[asc-mailbox.md](../integrations/ios-emulator/asc-mailbox.md) the transport and
[asc-controller.md](../integrations/ios-emulator/asc-controller.md) the controller registers.

### Configure the laboratory CPU identity

`ios.emulator.inspect_pmp` and `ios.emulator.pmp` take `cpu_midr` and `cpu_affinity`. The defaults are
MIDR `0x610f0000` and affinity `0x0`. MIDR must fit 32 bits, and affinity permits only the MPIDR affinity
fields within mask `0xff00ffffff`. A different identifier selects firmware branches; the instruction set
still comes from QEMU `max`, so this does not implement the named Apple core.

The values and decoded MIDR fields appear under `cpu_policy`. Before an instruction runs, Tomacco reads both
properties back through QMP and requires an exact match. `cpu_identity_verified` means that check passed,
and `measured_silicon_identity` stays false. The desktop's processor settings have the same inputs.

### Capture an instruction before it faults

A PMP job can stop before one selected instruction, which keeps the registers and objects as they were
before the firmware's panic handler changes them.

```sh
tomaccoctl call ios.emulator.pmp '{"engine":"/absolute/qemu-system-aarch64","firmware":"/absolute/new-pmp-bundle","controller":"asc-v6","seconds":15,"stop_at":"0x10280d4","capture_ram":true}'
tomaccoctl call job.wait '{"job_id":"JOB_ID","seconds":30}'
tomaccoctl call ios.emulator.report '{"job_id":"JOB_ID"}'
tomaccoctl call ios.emulator.memory '{"job_id":"JOB_ID","address":"0x1000000","length":256}'
```

`stop_at` must lie on a four-byte boundary inside the verified file-backed executable image. The address
above belongs to the 27.0.1 firmware; inspect another image's layout before choosing a point. The client
uses QEMU's hardware instruction breakpoint through a private Unix socket, writes no guest instructions and
opens no TCP port. The job verifies the debugger stop and the CPU's exact PC before reporting
`pmp_breakpoint_reached`, and success means only that this diagnostic completed.

`capture_ram` saves the 2 MiB physical RAM to an owner-only `ram.bin` after the CPU stops, and the report
records its base, size and SHA-256. An incomplete capture is an explicit error. `ios.emulator.memory` takes
only a completed PMP job and a range of 1-4096 bytes inside `[0x1000000, 0x1200000)`, rechecks the RAM hash,
and returns hex, ASCII and little-endian words. Registers are under `observations[].cpu_registers`. The
desktop's research panel has the same controls and a hex view.

## The Inferno backend

`tools/build_ios_emulator.py --backend inferno` pins ChefKiss Inferno and carries
`integrations/ios-emulator/bringup.patch`. It fails to boot iOS 27, because the legacy loader stops at XNU's
SPTM startup. The patch stays for reference and is not a supported route.
