# Experimental ASC/KIC controller

`tomacco-pmp-asc-research` runs the same unchanged PMP executable and CPU/RAM
policy as `tomacco-pmp-research`, with an explicit, opt-in controller mapping at
`0x300e00000`. The model provides configuration storage, interrupt arbitration,
two ARM timer inputs and the [channel-zero message transport](asc-mailbox.md).
There is no RTKit responder or invented PMP service acknowledgement. Unknown
registers and unsupported transactions cause architectural external aborts.

This is a functional research model, not a measured T8140 hardware replica.
The default PMP machine remains unchanged and unmapped. Neither machine is
integrated into the full-system Darwin guest or qualifies graphical iOS apps.

## Basis and model policies

The manifest-bound RTKit 3514.0.15 payload has SHA-256
`86b139c4d58ac573385d41bf6595c729b68b1cf075dea0165ff66a0253192210`.
Its original configuration routine at `0x102e964` reads/modifies offsets `0x40`,
`0x400` and `0x80c`. The examined object requests `0xa0401` in the known
configuration fields, sets firmware-control bit 10 and global bits 0, 29, 30.
These accesses establish fields used by this image, not cold-start values or
the physical timing consequences of those fields. They reset to zero in this
model; configuration timing is not emulated.

The original KIC dispatcher at `0x102bed0`, handler at `0x102c0dc` and rearm
routine at `0x102c4e0` establish event reads, mask banks and software-pending
writes. Initialization at `0x102c5c0` writes `1` to `0xc88`, restores external
masks and programs timer configuration `0xa`. The physical timer driver at
`0x102746c` writes ARM `CNTP_CTL_EL0` and `CNTP_TVAL_EL0`; these instruction
encodings select the EL1 physical timer, even where a disassembler prints an
EL2 alias. That real QEMU timer output is connected to KIC timer input zero.

The [Asahi ASC notes](https://asahilinux.org/docs/hw/soc/asc/) and
[m1n1 register definitions](https://github.com/AsahiLinux/m1n1/blob/main/proxyclient/m1n1/hw/asc.py)
describe the same programming model, with their own uncertainties.
Register facts are used; implementation code is original. No Apple firmware
or disassembly is distributed.

The following are explicit experimental policies, not silicon measurements:

- Global bit 0 gates all controller interrupts. Bits 29/30 and the accepted
  configuration fields retain writes; their timing/power effects are unknown.
- All IRQ/FIQ masks reset set. Reading an event automatically masks its source;
  writing the corresponding clear alias rearms it. A live level can reassert.
- Arbitration is timer before external before mailbox, lowest source first.
  External IRQ and FIQ masks are independent. AP outputs expose mailbox events
  for diagnostics; they are not a qualified SoC AIC route.
- Queue enable gates interrupt generation, not message storage. The two views
  share the same queues and enable state but have independent interrupt masks.
- `0xc88` enables timer arbitration. Timer configuration bit 1 routes the ARM
  timer input to IRQ when set, otherwise FIQ; bit 3 similarly routes software
  pending. State bit 0 is software pending, bits 1/2 mask IRQ/FIQ. The physical
  timer connection follows the examined driver; virtual timer input one and
  the complete routing/reset behavior remain unqualified.
- Interrupt reset clears controller/mask/software state while preserving
  configuration, queued/staged messages, queue enables and input wire levels.
  Full machine reset clears all device state.

The original mailbox objects contain events `0x40000` through `0x40003` for
empty/nonempty conditions. Vectors `0x100a5` through `0x100a8` belong to their
overflow/underflow callbacks, **not** queue readiness. They are not connected
to those raw conditions. Error transactions retain the transport's strict
abort policy, rather than pretending to implement unmeasured error hardware.

## Register contract

All controller accesses below are aligned 32-bit transactions. Mailbox paired
payload words retain the transport's aligned 64-bit contract. Rejecting an
access leaves functional state unchanged; diagnostic rejection counters grow.

| Offset | Behavior |
| --- | --- |
| `0x40` | Configuration storage; accepts mask `0x000fff01` |
| `0x400` | Firmware-control bit 10 storage |
| `0x808` | Interrupt reset, bit 0; rising edge resets interrupt state, held reset inhibits output |
| `0x80c` | Global configuration, accepts `0x60000001` |
| `0x818`, `0x820` | IRQ/FIQ event, zero when none; read acknowledges by masking |
| `0x900..0x93c`, `0x980..0x9bc` | W1S/W1C software-pending banks for 512 sources; not readable |
| `0xa00..0xa3c`, `0xa80..0xabc` | IRQ mask W1S/W1C; both aliases read current masks |
| `0xb00..0xb3c`, `0xb80..0xbbc` | FIQ mask W1S/W1C; both aliases read current masks |
| `0xc88` | Timer arbitration enable, bit 0 |
| `0x1000`, `0x1004` | Timer routing, accepts bits 1/3 |
| `0x1010`, `0x1014` | Timer state W1S; reads current state |
| `0x1018`, `0x101c` | Timer state W1C; reads current state |
| `0x4000`, `0x8000` | IOP/AP mailbox window bases |
| window + `0x100`, `0x104` | IRQ/FIQ mailbox mask W1S; reads current mask |
| window + `0x108`, `0x10c` | IRQ/FIQ mailbox mask W1C; reads current mask |
| window + `0x110`, `0x114` | Queue status plus enable bit; write accepts only bit 0 |
| window + `0x800..0x838` | Paired transport words, as documented in the transport contract |

Timer state writes apply bits 0..2; wider mask writes intentionally ignore
the other bits, including the firmware's all-ones shutdown write. Mailbox
mask storage retains other channel bits but absent channels generate no events.
No other unspecified bits are silently accepted. CPU control/status, independent
KIC timer banks, additional channels and power-management peripherals are absent.

## Agent and desktop workflow

Select `controller: "asc-v6"` in `ios.emulator.inspect_pmp` or
`ios.emulator.pmp`. Both the machine and its trace must exist; otherwise the
request fails before creating a job. The default `controller: "none"` retains
the reference machine without a controller mapping. The controller selection is part of duplicate-job
identity and shares the exclusive lock with all other iOS runs.

Inspection returns `experimental_asc_available` and `controller_policy`;
`capabilities.asc_controller` remains false because complete controller service
qualification is still absent. Reports carry the selected policy alongside
input hashes, command, QMP exchange and actual fault diagnostics.

The read-only QOM property `/machine/asc` → `controller-state` returns revisioned
JSON: configuration, 16 external interrupt banks, queues, timer inputs/masks,
four output levels/next events, transitions and MMIO counters. Reading this
property does not pop a queue, acknowledge an event or increment guest MMIO
counters. Managed reports save these observations initially, every second and
after stopping. Snapshot failures remain explicit in `controller_snapshot_error`.
MMIO tracing is limited to 2,048 accesses; counters continue after that budget.

The desktop iOS research panel exposes both system and PMP modes, remembers
their separate bundles/durations, follows agent-created jobs and displays
controller state and the first bus fault. Run cancellation and job/log links
use the same durable job interface as CLI/MCP.

Complete SoC wiring, independently qualified reset/timing behavior, the power
service protocol, Darwin integration and the missing CPU voltage/frequency
boot data remain required for a complete iOS runtime.

## Firmware behaviour

With laboratory MIDR `0x610f0000` and affinity `0x10000`, the firmware performs 10
controller reads and 49 writes under strict system-register handling, with no rejected
accesses. Configuration becomes `0xa0401`, firmware control `0x400`, global
configuration `0x60000001` and timer routing `0xa`. It then faults on system register
`S3_7_c15_c4_6` at PC `0x102adac`. No unknown register instruction is skipped. The
alternate identity is not a measurement of the PMP core, and the default identity faults
at EHID3 before ASC. See [the alternate identity](pmp-research.md#explicit-alternate-identity).

The firmware-free ARM guest in `tests/ios_asc_controller_integration.py` executes four IRQs,
two FIQs, both architectural timer paths, bidirectional message transfer, source rearming and
three external aborts. Trace on and off give identical guest results. QOM observations preserve
pending messages and controller counters, and reset clears the device. The native state core
checks all 512 external sources under undefined-behavior sanitization.
