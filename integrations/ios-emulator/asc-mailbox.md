# ASC message transport

`asc-mailbox.c` implements a reusable QEMU SysBus device with two real message
queues, A2I and I2A, and four raw level interrupt outputs. It moves messages
written by one participant to the other participant; it implements no RTKit
endpoint, power-management reply, boot acknowledgement or synthetic firmware.

The `tomacco-asc-mailbox-test` machine exercises the device. The state core is
also used in the opt-in [ASC/KIC controller](asc-controller.md). It is not mapped
in Darwin or the default `tomacco-pmp-research` machine. In the default mode, the original
PMP's first access to controller configuration at `0x300e00040` still faults.
The public builder verifies the test machine, hashes both new sources and
records `asc_mailbox_test_available: true`, `asc_controller_available: false`.
The opt-in controller is separately recorded as `experimental_asc_controller_available`.
`ios.emulator.inspect_pmp` discovers the machine and trace together under
`diagnostic_machines.asc_mailbox`; this does not change its runtime capabilities.

## Basis and supported contract

Register offsets, message width and the two directions agree with the
[Linux Apple mailbox driver](https://github.com/torvalds/linux/blob/master/drivers/soc/apple/mailbox.c)
and [m1n1's ASC client](https://github.com/AsahiLinux/m1n1/blob/main/src/asc.c).
The four empty/nonempty interrupt conditions are described in the
[Asahi ASC hardware notes](https://asahilinux.org/docs/hw/soc/asc/).
The model is original code; none of those implementations is copied.

The matching PMP payload (`86b139c4d58ac573385d41bf6595c729b68b1cf075dea0165ff66a0253192210`)
corroborates 64-bit paired transfers. Its sender at `0x102cad0` checks FULL
before STP; its receiver at `0x102ccb4` checks EMPTY before LDP. The layout
selection at `0x102ced4` uses channel-zero status offsets `0x110`/`0x114`,
send offsets `0x800`/`0x820` and receive offsets `0x810`/`0x830`. The original
objects at `0x1050948` and `0x1050a50` select this layout at base `0x300e04000`.
This comes from static analysis; those routines have not booted a controller.
No Apple instructions, disassembly or firmware are distributed.

| Offset | Access | Effect |
| --- | --- | --- |
| `0x110`, `0x114` | read32 | Queue count, pointers, FULL and EMPTY |
| `0x800`, `0x820` | write64 | Stage the first word of A2I/I2A |
| `0x808`, `0x828` | write64 | Commit the staged word plus 32-bit message metadata |
| `0x810`, `0x830` | read64 | Peek at the oldest payload without removing it |
| `0x818`, `0x838` | read64 | Return its metadata and consume exactly one message |

Each queue is independent. Uncommitted data cannot become a received message.
Reading the first word repeatedly does not consume messages. A rejected
transaction preserves both queues and their staging latches, allowing a full
producer to retry after the consumer frees space. A machine reset clears all
queued/staged data and returns the interrupt outputs to their empty levels.

## Explicit model policies and remaining hardware

This is a bounded transport model, not a measured T8140 silicon replica:

- Each queue has eight entries, with four-bit wrapping pointer counters.
  That capacity is an explicit laboratory policy, not a measured FIFO depth.
- Metadata bits 31:0 carry the sender's second word. OUTCNT and pointer fields
  snapshot this model's state before consumption. Asynchronous clock-domain
  INCNT behavior is not modeled; that field remains zero.
- Empty reads, full commits, commits without a staged payload, nonzero upper
  send metadata bits and partial/spanning/unknown accesses raise normal ARM
  external aborts. These are strict diagnostics for unqualified transactions,
  not claimed measurements of physical overflow/underflow behavior.
- Queue control writes, interrupt mask/acknowledge banks, CPU control,
  configuration, KIC, timers and the different mailbox channel layouts are
  unsupported in this standalone transport. The separate controller wraps it
  with its documented mask/configuration/timer contract.
- Raw IRQ outputs 0..3 are A2I empty, A2I nonempty, I2A empty, I2A nonempty.
  The test machine's direct connection from output 3 to ARM IRQ is test wiring,
  not a T8140 interrupt route. Its second register view is a test-only alias.

These boundaries prevent a working queue test from being reported as PMP
boot, working power services, or a complete iOS runtime. The experimental
controller's configuration/reset policies and KIC routing still need independent
hardware qualification, as do the transport policies above.

## Executable checks

```sh
python3 -m unittest discover -s tests/python -p test_ios_asc_mailbox.py -v
python3 tests/ios_asc_mailbox_integration.py \
  --engine /absolute/qemu-system-aarch64 \
  --clang /absolute/clang --linker /absolute/ld.lld \
  --output /absolute/asc-results.json
```

The production state core runs under undefined-behavior sanitization in the
normal product test suite. It checks full queues, repeated wraparound, both
directions, all 32 metadata bits, reset, rejected-access atomicity and every
unsupported register offset in the device window.

The separate QEMU test exchanges messages between two MMIO views, observes all
four real interrupt outputs and checks reset with both queued and staged data.
An original ARM64 guest receives a host-written message, sends a reply, handles
one real EL1 interrupt and checks ten architectural aborts without losing queued
messages. Tracing on/off produces the same guest results. IRQ-output interception
and real CPU delivery run in separate processes because QTest interception
disconnects the original destination. Neither test uses Apple firmware.
