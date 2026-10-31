# Isolated PMP firmware execution

`pmp-research.c` adds `tomacco-pmp-research` and `tomacco-pmp-asc-research`. They
execute locally supplied, manifest-checked T8140 PMP firmware under ARM64 TCG,
separately from Darwin. The default has no controller mapping; the opt-in
ASC/KIC model has a [documented register contract](asc-controller.md).
Neither implements power services. Their purpose is to expose the firmware's real next hardware dependency
to the Rust CLI/MCP, with original fault addresses and reproducible inputs.

No Apple firmware or disassembly is included in the product. The preparation
tool downloads nothing and does not modify source inputs or instructions.

## Preparation contract

`prepare_ios_pmp.py` selects exactly one T8140 customer-erase identity for the
requested board, confines its PMP path to the supplied extraction and checks
the container's SHA-384 against that local BuildManifest. This is digest
binding to the supplied manifest, not Apple signature or secure-boot verification.

Only the bounded, canonical DER `pmpf` IM4P containing an uncompressed ARM64
`MH_PRELOAD` is accepted. Encrypted/compressed payloads, unsupported commands,
overlapping/out-of-bounds ranges, invalid permissions and entries outside
file-backed executable segments fail explicitly. `LC_UNIXTHREAD` supplies the
entry. The ELF envelope preserves load addresses, segment bytes, permissions
and zero-fill sizes; it inserts no code. ELF permissions describe the image;
the CPU's own page tables control access after firmware initialization.

Publication is atomic and refuses an existing output. Files are read-only.
The bundle contains the original manifest/container, unwrapped Mach-O, ELF
and a receipt with hashes and load ranges. Inspection and execution revalidate
the manifest, container and payload, reconstruct the ELF and compare it byte
for byte. Changing an ELF hash in the receipt cannot authorize changed code.
Capability flags are computed, never accepted from an editable receipt.

## Explicit execution policy

The managed run uses one QEMU `max` CPU at EL1, with EL2/EL3 disabled, a 24 MHz
physical counter and 2 MiB RAM at `[0x1000000, 0x1200000)`. The default MIDR is
`0x610f0000`, with MPIDR affinity fields `0x0`. These are laboratory choices, not measurements of a physical
T8140 PMP core or its complete SRAM/DRAM map. The original firmware checks
the MIDR family mask `0xff0f0000` against `0x610f0000`; QEMU's default MIDR reaches
its original `!MIDR` panic. No instruction is patched to bypass that check.

`cpu_midr` and `cpu_affinity` expose those choices explicitly in inspection,
execution and the desktop. The former is a 32-bit hex value; the latter accepts
only affinity bits within `0xff00ffffff`. QEMU supplies MPIDR's architectural
flags separately. The ISA remains QEMU `max` regardless of the chosen MIDR.
Before resuming, the worker checks QOM `midr` and `mp-affinity` against the
request; any difference fails with `pmp_cpu_identity_mismatch`. Reports mark
`cpu_identity_verified` for this configuration check, while
`cpu_policy.measured_silicon_identity` remains false. Canonical numeric values
participate in duplicate-job admission; different identities cannot overlap.

PMP machines enable `tomacco-strict-sysregs`: an unregistered system-register
instruction raises architectural UNDEF and emits an executed-instruction trace.
The upstream fork instead silently skipped those accesses. Managed runs verify
the CPU property through QMP before resuming, require the trace capability and
report `strict_sysregs_verified`. This correction is PMP-specific; the Darwin
machine keeps the upstream policy.

The PMP bootstrap executes the encoding normally called `TLBI ALLE3`
(`S1_6_c8_c7_0`) after configuring EL1 page tables, while running at EL1.
`PMP_TLBI_ALL` implements that write-only EL1 operation by flushing all local
QEMU translations. A real ARM test changes a mapping and sees the new page
after the operation. This is an effective invalidation, not a skipped
instruction. Its exact silicon scope remains unmeasured; the laboratory
policy flushes every local translation. The read-only machine property
`pmp-tlbi-alias` advertises the implementation and is checked before execution.

In the default `controller: "none"` run, all hardware outside RAM remains
unmapped and produces normal ARM external aborts. Selecting `controller: "asc-v6"`
maps configuration, KIC, ARM physical/virtual timers and channel-zero transport.
That model declares its unmeasured reset/routing policies and leaves unsupported
peripherals faulting. No PTD reply or controller-ready resource is synthesized.

The engine also includes a separately tested [ASC message transport](asc-mailbox.md).
It is also exposed as a diagnostic machine, independent of the controller mode:
its queue tests do not establish controller configuration or KIC routing.
Inspection discovers it under `diagnostic_machines.asc_mailbox` when both the
machine and its trace exist. `experimental_asc_available` discovers the new
controller machine/trace pair; `capabilities.asc_controller` remains false until
complete controller services exist. Reports include read-only
controller snapshots when that experimental mode is selected.

## Instruction and memory capture

The managed workflow accepts a `stop_at` instruction address and independent
`capture_ram` flag. A bounded native RSP client installs one TCG hardware
breakpoint over a private Unix socket, resumes the CPU, verifies SIGTRAP and
the requested PC, then captures registers/controller state before shutdown.
Original code is never patched. Points outside verified executable bytes or
not aligned to four bytes are rejected before starting a job. Protocol frames,
checksums, decoded length and read deadlines are bounded and checked.

With RAM capture enabled, QMP `pmemsave` records the isolated 2 MiB after CPU
stop. Metadata includes its physical base and SHA-256. The agent operation
`ios.emulator.memory` checks the owned job, complete regular file and digest on
each read, and returns at most 4096 bytes as hex/ASCII/word strings. Registers
also use hex strings for exact 64-bit values in JavaScript clients. Failed runs
can retain captures; immediate cancellation may leave none. Captures contain
local firmware and are diagnostic artifacts, not bundled product files.

See the [CLI and desktop workflow](../../docs/research-engines.md#capture-an-instruction-before-it-faults).
Breakpoint success speaks for a diagnostic stop only. All firmware boot and
power-service flags remain false, and the image entry alone is not evidence
of executed instructions.

## Reference firmware and CPU behaviour

The iPhone17,3 / d47ap iOS 27.0.1 / 24A446 payload is RTKit `3514.0.15`:

| Artifact | SHA-256 |
| --- | --- |
| PMP IM4P | `16395c6c3f7b8b6222937d6e7d997d586317d7c89f56b812284e056d7f8906f5` |
| Mach-O payload | `86b139c4d58ac573385d41bf6595c729b68b1cf075dea0165ff66a0253192210` |
| Generated ELF | `422d8e92c2dfc90c22d69e08c6932c168e9e41fdf6087b96e6e606fae97b42f3` |

The entry is `0x1000000`. With the default identity, strict CPU handling and the
translation operation implemented, execution stops at PC `0x10280d4`. The instruction `0xd538f329` reads `S3_0_c15_c3_1` into X09 at EL1 and raises
UNDEF, ESR `0x02000000`, before any ASC accesses. This register is commonly
named EHID3 in [m1n1's register definitions](https://github.com/AsahiLinux/m1n1/blob/main/src/cpu_regs.h).
Those definitions do not establish the PMP's reset value or the behavior of
the bit used here. It remains unsupported; no guessed value or storage-only
substitute was added.

The managed breakpoint can stop immediately before that instruction, keeping exact
registers and RAM. All controller boot, power-service and graphical-iOS flags remain false.

### Explicit alternate identity

The original routine at `0x1028150` uses MPIDR bits 23:16 to select an internal
CPU type, together with generation fields derived from MIDR. Under the default
identity, it selects the path that reads EHID3. Explicit affinity `0x10000`
selects another original branch with the same MIDR; it does not require
patching instructions or suppressing unknown-register exceptions. The T8140 PMP
core's identity has not been measured, so this does not replace the default or establish
that the alternate path matches hardware.

With that affinity, execution reaches the initializer's return at `0x1028148` with no
system-register or bus fault before that point. Without a breakpoint it performs 10 ASC
reads and 49 writes, with zero rejected controller accesses, under strict CPU handling.
Configuration becomes `0xa0401`, firmware control
`0x400`, global configuration `0x60000001`; both ARM timer routing registers
become `0xa`. The queues remain disabled and no timer interrupt is observed.
The next unsupported instruction is `0xd53ff4c0` at PC
`0x102adac`, reading `S3_7_c15_c4_6` into X00 at EL1. It raises architectural
UNDEF and is retained as `first_sysreg_fault`.

That encoding is named `AON_CNT10` in the published
[M5 register fields](https://gist.github.com/justtryingthingsout/6c976d52d3f95887c8d51a679367fc26).
They describe event-selection controls, not a known fixed-rate clock, and do
not establish the PMP core's reset state or event mapping. The firmware also
has a paired write routine. No constant-zero or invented clock implementation
was added.

## Checks and open dependencies

```sh
python3 -m unittest discover -s tests/python -p test_ios_pmp.py -v
python3 tests/ios_pmp_integration.py \
  --engine .tooling/ios-sptm/build/qemu-system-aarch64 \
  --clang /absolute/clang --linker /absolute/ld.lld \
  --output /absolute/pmp-machine-results.json
```

The original firmware-free ARM64 guest checks EL1, MIDR, MPIDR, counter frequency and
advancement, both RAM edges, faults immediately outside RAM and ASC read/write
faults. It also verifies actual UNDEF exceptions for unknown reads/writes and
a changed page-table translation after PMP invalidation. Trace on/off gives
identical guest results. An alternate MIDR `0x611f0521` and affinity
`0xff00020103` verify all four affinity fields and guest-visible MPIDR flags;
use `--cpu-midr` and `--cpu-affinity` to reproduce it. These tests exercise actual
QEMU instructions and exceptions; protocol fixtures separately test Rust jobs,
strict input rejection, fragmented traces, timeout, duplicate admission and
cancellation cleanup.

The main Darwin guest stops at ApplePMGR's missing `voltage-states1`. Strict PMP
execution with the default identity first needs correct support for the Apple CPU setup
register described above. The alternate identity reaches `S3_7_c15_c4_6` after 59
controller accesses, and its relationship to physical PMP hardware is unmeasured. See also
[the controller's behaviour](asc-controller.md#firmware-behaviour). Analysis of the matching
iBoot/LLB shows that RCfg opcode 13 captures existing MMIO values into reconfiguration records,
so its embedded words are not cold-start voltage or frequency tables, and no such values are
inserted. Complete PMGR initialization needs correct boot preparation and a working controller.
