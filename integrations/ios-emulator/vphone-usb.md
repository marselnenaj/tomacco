# vphone600 USB transport

`vphone-usb.c` is a GPL-2.0-or-later QEMU device, built only with the research
board. It exposes Apple PCI `106b:1a01` and two modern VirtIO split queues.
It transports actual request/response bytes between the original guest driver
and a private Unix-socket host. It implements no USB descriptor or guest service.

## Basis and scope

The analyzed input is PCC/cloudOS 26.4 beta `23E5207q`,
`kernelcache.research.vphone600`, SHA-256
`86895a2eebeb7fcfb68bcd60f2d7e685687faea07ee31166fb58c872b38e0d92`.
`com.apple.driver.AppleVirtualPlatform` has UUID
`86480BAE-4539-3F4F-AC4A-A456E934A97E`.
Its original prelink personalities explicitly match
`AppleVirtIOUSBConfiguration` to `0x1a01106b` and
`AppleVirtIOUSBPCITransport` to `0x1a01106b 0x1a04106b`.
The transport class is the generic `AppleVirtIOPCITransport`.
Only the former PCI identity is implemented and exercised here.

The addresses below are disassembly observations, not recovered method names:

| Address | Observed contract |
| --- | --- |
| `0xfffffe00080f4620` | Configuration service copies properties from `IODeviceTree:/product/usb-device`. |
| `0xfffffe00080edac0` | Requires VirtIO version 1; creates queues 0/1 and posts four 128 KiB receive buffers. |
| `0xfffffe00080ef16c` | Queue 0 completion reads request type at byte 5 and endpoint at byte 4. Types 1/2/3 dispatch to transfer/reset/cancel handling. |
| `0xfffffe00080fecf0` | Requires the six-byte request header. |
| `0xfffffe00080fed8c` | Request payload begins at byte 6. |
| `0xfffffe00080ef46c` | Endpoint zero requires a setup packet of at least eight bytes. |
| `0xfffffe00080ef7c0` | Parses the actual USB setup packet and its OUT data stage. |
| `0xfffffe00080f3e28` | Builds a two-byte response header followed by optional scatter/gather data. |
| `0xfffffe00080ef62c` | Builds reset completion type 4, parameter `0x10`. |
| `0xfffffe00080ef72c` | Builds cancellation completion type 5 for the requested endpoint. |

The surrounding IOKit control flow informed the interpretation of reset and
endpoint handling; a live guest enumeration independently shows reset, setup
transfers, descriptors, SET_ADDRESS, SET_CONFIGURATION and GET_CONFIGURATION.

## Wire contract

The framing convention follows the preboot USB stream in
[Orchard's BDIF implementation](https://github.com/yaelliethy/orchard/blob/c73a2bda1fd4afefd61204bb74afd74292ef2fe9/qemu/hw/vmapple/bdif.c):
little-endian `u32` frame length followed by exactly that many bytes.
This device connects the **OS PCI driver**, not that preboot MMIO interface.

Host requests are `u32le transfer_length, u8 endpoint, u8 type, payload`:

- Type 1: a setup packet plus OUT data on endpoint 0, OUT bytes on a data
  endpoint, or an empty payload with the requested length on an IN endpoint.
- Type 2: USB reset, no payload, endpoint/length zero.
- Type 3: cancel an endpoint, no payload, transfer length zero.

Guest responses are `u8 type, u8 endpoint_or_parameter, payload`. Type 1 is a
transfer completion, 2 a stall, 3 an endpoint-abort notification, 4 a reset
completion and 5 a cancellation completion. Host support for control and bulk
transfers is implemented; isochronous scheduling is not.

Queues and stream buffers are bounded. A receive request is never split over
guest descriptors or truncated to fit. Wrong descriptor directions, invalid
request shapes and oversized frames fail the device with NEEDS_RESET and close
the stream. Guest responses complete only after the actual host stream accepts
all their bytes. A slow or absent peer cannot receive a fabricated completion.
Partial writes use a writable event source; the device never calls blocking
`qemu_chr_fe_write_all`. Reset discards pending DMA and disconnects the stream
so partial frames cannot leak into the next session. Migration is explicitly
unsupported.

`tools/ios_paravirt_usb.py` performs bounded enumeration and supplies control,
bulk-IN and bulk-OUT methods. `tools/ios_paravirt_mux.py` adds a single service
connection over the guest's advertised USB mux interface. Its wire-format
references are [usbmuxd's device protocol](https://github.com/libimobiledevice/usbmuxd/blob/master/src/device.c),
[DeMuxUSB's direction-specific headers](https://github.com/hack-different/demuxusb#usbmuxinterfaceexpert),
and [libimobiledevice's restored client](https://github.com/libimobiledevice/libimobiledevice/blob/master/src/restore.c).
It negotiates version 1/2, validates framing, tracks TCP sequence/window values,
reassembles property lists and sends a real QueryType request. Its separate
send/receive plist methods also support unsolicited restore events. FIN and
PSH preserve final stream bytes; EOF is reported after buffered data is read.

`ios_paravirt_usbmuxd.py` exposes the configured guest through a **private**
usbmuxd plist-v1 Unix socket. Standard libusbmuxd clients select it with
`USBMUXD_SOCKET_ADDRESS=UNIX:<socket>`. The
[upstream client protocol](https://github.com/libimobiledevice/usbmuxd/blob/master/src/client.c)
defines ListDevices, Listen and Connect; the bridge implements these and routes
the connected byte streams to the original guest. A successful Connect follows
the guest's actual SYN/ACK; a guest RST returns ConnectionRefused.

The shared link negotiates once, assigns distinct source ports and routes
responses to their respective clients. `ios_paravirt_duplex.py` dispatches
endpoint completions independently: an outstanding bulk-IN cannot prevent
bulk-OUT from another service. Each stream retains its own sequence, window,
deadline and bounded receive buffers. There are at most 32 simultaneous local
clients, ports are never reused within a session, and an invalid local client
does not disconnect other clients. Session termination wakes all waiters and
reaps the reader/client threads. No guest responses are synthesized.

The socket's directory is mode 0700 and the socket is mode 0600. It never
replaces `/var/run/usbmuxd`, exposes physical phones or accesses host pairing
records. ReadPairRecord reports ENOENT; pairing mutations and ReadBUID explicitly
report ENOTSUP. This transport bridge does not provide normal-mode pairing or
claim that the guest has completed restore. It forwards client commands,
including destructive restore commands when a client explicitly sends them.

Both command-line hosts wait for the original restore process's UART
`enabling USB finished: success (0)` or the normal ConfigureUSB service's
`USB configuration complete!` checkpoint before enumerating. The PCI queue becomes
usable earlier than controller power-on, and an enumeration attempted in that gap stalls.
These checkpoints prove USB configuration only, not installed
application-service readiness. The reader scans incrementally,
with a deadline and log bound; an already stopped guest fails immediately.

Enumeration waits at fixed points, all inside the session deadline and with no delay
added to bulk data:

- 50 ms after reset completion and 10 ms after SET_ADDRESS, following the recovery
  intervals in the [Linux USB hub driver](https://github.com/torvalds/linux/blob/master/drivers/usb/core/hub.c).
- One second after attachment before the first reset, because a reset sent immediately
  after power-on is dropped under emulation.
- 50 ms after each completed control transfer, for the endpoint to accept its next setup
  packet. This is a board compatibility interval, not a claim that USB generally requires
  that delay.

The private bridge only retries socket availability **before** submitting any request. After a
submitted reset times out, disconnect marks the VirtIO device NEEDS_RESET and reconnecting cannot
recover it, so that case keeps the error and reports `guest_reset_required:true` instead of looping
on reconnect attempts. An observed host disconnect is reported separately from system-boot failure,
including `usb_reset_required_after_disconnect`. A refused lockdownd connection stays visible as a
service refusal rather than being promoted to device readiness.

`ios.emulator.usb_bridge` exposes the private bridge through Rust CLI/MCP and
owns its lifetime as a child of the corresponding platform job. The bridge waits
for its parent's atomically published request, exposes live counters via
`ios.emulator.report`, and ends with the parent. It does not create a guest
service where none exists.

## Running

Add `--usb` to the documented `run_ios_paravirt.py` invocation. The launcher
records the private socket in `request.json` and removes it on exit. Then run:

```sh
python3 tools/ios_paravirt_usb.py \
  --request /absolute/session/request.json \
  --output /absolute/new-usb-report.json \
  --seconds 180 --hold --query-restore
```

The host deadline should extend beyond the bounded guest session when using
`--hold`. Closing the host while the guest remains active requires a USB device
reset. Reports keep enumeration apart from service success, and
`qualified_for_app_development` stays false. No Apple signing request is made.

## Verification

`tests/ios_paravirt_usb_integration.py` uses actual PCI capabilities, queue DMA,
interrupt completion and a Unix socket in the local engine. It checks fragmented
input, receive backpressure, scatter/gather output, failure without truncated
completion, and reset/reconnect. Firmware CPUs are paused for these checks.
The Python protocol tests cover malformed frames, stalled requests, fragmented
mux messages, refused connections, sequence errors, flow control and bounded
property-list reassembly.

In a live session, the original guest returns vendor and product `05ac:12ac` and four configurations.
The original `restored_external` logs USB enablement success and waits for a host to start a restore. The
host negotiates mux 2.0, connects to port 62078 and reads the original `com.apple.mobile.restored` service's
protocol version 15. The bootloader `serial-number` property must be present: XNU's
`IOPlatformExpertDevice::configureDefaults()` publishes it as `IOPlatformSerialNumber`, and the original
restored process aborts after connection when `SerialNumber` is missing. Its value is the existing virtual
configuration identity, with no change to the ECID or authentication state.

## Standard Linux tool access

Run this **instead of** the standalone USB enumeration client for a session:

```sh
python3 tools/ios_paravirt_usbmuxd.py \
  --request /absolute/session/request.json \
  --output /absolute/new-usbmuxd-receipt --seconds 180
```

The new private directory receives an atomically updated `bridge.json`.
Wait for `state: ready`, then use its exact `environment.USBMUXD_SOCKET_ADDRESS`
for each client process. For example, `idevice_id -l` lists this virtual device
when run with that environment. Other terminals keep their usual USB service.
The bridge does not run a restore client, request tickets or fetch firmware.
Its lifetime should extend beyond the guest's bounded session; the private
socket is removed on deadline, guest disconnect, Ctrl-C or SIGTERM.

`tests/ios_paravirt_restored_client.c` is a read-only native interoperability
probe, compiled against the installed libraries:

```sh
cc -Wall -Wextra -Werror tests/ios_paravirt_restored_client.c \
  -o /tmp/tomacco-restored-probe \
  $(pkg-config --cflags --libs libimobiledevice-1.0 libplist-2.0)
```

With the bridge environment, it queries the real restored service while holding a second TCP connection to FDR
port 1082, and it sends no FDR operation. It works with unmodified host libimobiledevice 1.4.0, libusbmuxd 2.1.1 and
libplist 2.7.0: `idevice_id` lists the USB descriptor's serial, and the C client reads protocol 15, VPHONE600AP hardware
chip and board `0xfe01` and `0x91`, and SavedDebugInfo, with both connections open at once.

## Restore and the boot manifest

The first real data request of an original restore on blank media is `RootTicket`. Closing without a reply produces
Ramrod error 8. A manifest whose hash the device tree does not carry produces Ramrod error 3 (`invalid ticket`). An explicit
boot-manifest handoff gives the guest that hash, and the original process then passes its check and requests `NORData`. No
verifier or security flag is patched. Matching this handoff hash does not authenticate a manifest or bind it to the virtual
device. [Setup](../../docs/virtual-ios/setup.md#what-no-tool-does-for-you) says what the manifest must declare.
