# Fixed panels for virtual iOS

The upstream virtual GPU advertises a desktop monitor with four timings,
including 3840×2160, and a 480×270 mm physical size. Virtual iOS selected the
4K timing. A phone-sized host window does not change that
guest-visible display.

`reims-display-panel.patch` adds one optional fixed physical panel. Tomacco
passes it through `ios.emulator.paravirtual` and its CLI/MCP schema:

```json
{"display_profile":"1170x2532@60:65x141"}
```

This example specifies pixels, refresh in Hz and physical millimetres. It is a
virtual panel configuration, not a claim to emulate a particular iPhone's SoC,
display calibration, safe areas or device identity. Omitting it retains the
desktop configuration. Rotation requires a new session with both dimension
pairs swapped.

## Ownership and bounds

- The agent API validates before creating a job. The standalone launcher also
  validates before touching firmware or storage.
- The launcher owns `REIMS_VGPU_PANEL`, removes an inherited value, and sets the
  canonical value only from its explicit request. The receipt records the
  requested geometry; control status exposes it as `requested_display_profile`.
- `reims-vgpu-config::Panel` parses this structured choice once into a type with
  private fields. Pixels must be within 64–8192 on each edge; refresh must be
  within 1–120 Hz; physical dimensions must be within 10–2000 mm. The renderer's
  own scanout and host capability checks still apply.
- Pixel and physical aspect must agree within rounding each physical dimension
  to the nearest millimetre. A profile with swapped physical dimensions fails.
- The GPU publishes exactly one preferred/native timing in the display shared
  page. Both the integer and float physical-size fields describe that panel.
- The locked and contended VBL paths use the same selected refresh and atomic,
  phase-locked timestamp. A 60 Hz panel is not signalled on the desktop 120 Hz
  grid. The VBL observation also reports the selected grid.
- The early EFI console remains the bootstrap mode. After the display driver
  registers, normal scanout follows the actual presented surface. The host does
  not stretch, crop or synthesize phone contents.

Apple's display protocol also accepts guest-installed detailed timings. This
patch selects the advertised panel; it does not rewrite guest requests or
pretend that a requested mode proves the OS selected it. Actual capture
dimensions and live display observations remain the evidence.

The build manifest declares `display_panel_configuration_available`. Older
engines without this patch do not implement the environment choice. A changed
profile is part of the owned job's identity, so an incompatible request cannot
silently reuse an active VM.

## Validation

The owner tests cover portrait/landscape geometry, bounded values and aspect
rejection. Shared-page tests inspect the single timing, 16.16 refresh and both
physical-size encodings. Cadence tests cover 1, 30, 60 and 120 Hz, phase retention
after a late poll and duplicate-delivery prevention. Compiled API/launcher tests
check admission, the child's environment and the retained receipt, including
removal of an inherited desktop override.
