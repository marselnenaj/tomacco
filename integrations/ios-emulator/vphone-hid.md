# USB Home and touchscreen

`tomacco-ios-hid` is a virtual USB device for the local research machine. It uses
the guest's original USB host and HID drivers. Its two interfaces describe
ordinary [USB-IF HID reports](https://www.usb.org/hid), with no Apple device
identity, firmware bytes or injected guest-memory responses.

Interface 0 has one one-byte Consumer Control input report: bit 0 is Consumer
Menu, page `0x0c`, usage `0x40`; the other bits are constant padding. This is the
iOS Home usage, distinct from keyboard Home or browser AC Home, as documented
in the upstream [vphone input investigation](https://github.com/Lakr233/vphone-cli/blob/main/research/keyboard_event_pipeline.md).

Interface 1 is a Digitizers Touch Screen application collection containing one
Finger collection. Its input report has seven bytes:

| Bytes | Meaning |
| --- | --- |
| 0 | Tip Switch, In Range, Confidence and Untouch in bits 0–3; remaining bits constant |
| 1 | Contact identifier, 1 for this single-contact device |
| 2–3 | Little-endian X, 0–32767 |
| 4–5 | Little-endian Y, 0–32767 |
| 6 | Number of contacts reported in this frame, 1 including the final lift sample |

The feature report is one byte, maximum contact count 1. Lift retains the last
contact coordinates, clears Tip Switch and In Range, and sets Untouch. Confidence
stays set through lift so the contact remains classified as a finger. Contact count
describes the report's valid contact records, as in the
[HID parallel reporting model](https://learn.microsoft.com/en-us/windows-hardware/design/component-guidelines/touchscreen-packet-reporting-modes).
Descriptors, input/feature GET_REPORT and per-interface GET/SET_IDLE are
implemented. Nonzero idle periods generate actual repeated input reports.
These are non-boot HID interfaces, so boot-keyboard protocol switching and
output reports are not supported; invalid control transfers stall.

The local control monitor addresses `/machine/peripheral/tomacco-hid`:

| QOM property | Contract |
| --- | --- |
| `home-press-ms` | Write 1–1000; queue a press, start its release timer on actual USB delivery, reject another press until release delivery |
| `touch-state` | Write `x \| (y << 16) \| (contact << 32)`; reject reserved bits, unconfigured devices or a full queue |
| `configured` | Read the configuration actually selected by the guest |
| `home-pending` | Read whether a Home sequence is outstanding |
| `touch-pending` | Read the number of queued contact reports |
| `touch-active` | Read the current contact state |
| `home-reports-delivered` / `touch-reports-delivered` | Read actual USB interrupt-IN delivery counts, including idle repeats |

The 128-report contact queue reserves its last slot for a release. It does not
evict reports to admit more motion. A five-second guest-time lease, renewed by
actual contact delivery, releases a contact if the client disappears. USB reset
clears pending input and timers; unplug cancels and frees all timers. Counters
are diagnostic totals for this device instance. Migration is explicitly
unsupported rather than dropping in-flight reports.

The dedicated `qemu-xhci` input controller uses `ports-non-removable=on`.
`vphone-usb-internal-ports.patch` implements this hardware topology through the
read-only xHCI `PORTSC.DR` bit (bit 30). It survives controller reset, port reset,
attach and detach. Other controllers retain the upstream removable-port default.
The USB host driver can therefore identify this touchscreen as an internal device;
HID usage alone does not associate an external touchscreen with the iOS display.

Tomacco exposes these operations through `ios.emulator.control` with
`input_transport=usb-hid`, without general QOM access. A separate standard QEMU
USB keyboard is attached on a removable USB port; QMP key presses use that
keyboard, while this device retains only the internal Home and touchscreen
interfaces. Both devices name root port `1` explicitly. QEMU otherwise inserts
an automatic hub when the keyboard consumes its controller's last free port;
this iOS guest repeatedly refuses to enumerate that hub's child. The explicit
port binds the keyboard directly to its controller. Coordinates describe the guest's absolute input space. A consumed
report does not prove a UI response; inspect subsequent captures.

The separate platform side button uses the machine's existing GPIO key, wired
to PL061 line 6. The pinned PCC26.4 `AppleVirtualPlatformButtons` driver enables
bit6 (`0x40`) in IEV/IE and constructs Consumer Power events (`0x0c/0x30`) from
that interrupt. The inherited macOS VMApple pin3 binding cannot signal this guest's
enabled interrupt. `ios.emulator.control` action `power_button` is restricted to
`tomacco-vphone`; it sends the powerdown notification whose machine handler
triggers that key. The GPIO key owns its 100 ms guest-time release timer. This
notification does not request emulator exit on this machine. It may wake or
lock the screen; observe the native screen state and actual display afterwards.
It is not an unlock acknowledgement or a replacement for orderly `stop`.
