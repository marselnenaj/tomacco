# Tomacco 1.5.0

Tomacco 1.5.0 brings the desktop, phone layout and coding agents onto the same
296-operation workflow. It follows [1.4.1](RELEASE_NOTES_1.4.1.md).

## Desktop, phone and languages

- All 50 Omarchy translation catalogs plus English cover 1,898 marked interface texts.
  Placeholder, plural, markup and stale-entry checks pass; this is not native-speaker review.
- Narrow windows switch to phone navigation and layouts, respecting the saved 75–200%
  interface size. The same app and control plane are used; physical Pixel deployment is pending.
- File and folder choosers follow interface scale, language and phone layout, preserve full
  names and paths, confirm replacements, and start nothing on Cancel or Esc.

## Agents and device coordination

Ten setup adapters cover Claude Code, Codex, Gemini CLI, Cursor, VS Code, Zed,
OpenCode, Grok CLI, Hermes Agent and omp. Setup, status and removal preserve other
clients' settings and disabled entries. See [agent clients](agent-clients.md).

PhoneUse-informed improvements add cooperative device reservations, durable request
identities, verified readable-field input and compact query-aware screen trees.
Unknown action outcomes are reported instead of blindly replayed; physical execution
is not guaranteed exactly once. Raw adb, SSH and flashing tools do not participate in
Tomacco reservations. [Agent workflows](agents.md) describe transport support and limits.

Fresh observations carry frame geometry, age and available app/keyboard/focus context.
Ordered action batches, human handoffs, named QA environments and GPX emulator routes
share the desktop's contracts. Unsupported transports report their actual capabilities.

## OCR and local performance

`screen.ocr` adds opt-in `invert:adaptive` and compact `detail:matches`, with actual
engine-call counts and separate capture/read/total timings. `auto` stays the default
with both polarities, including OCR actions, finds, waits and absence checks. Adaptive
results can miss additional words or duplicate targets; OCR does not prove absence.

Persistent MCP sessions can reuse one bounded, isolated Tesseract 5 worker. Cold CLI,
custom binaries, large images and a busy worker keep the subprocess route. Explicit
OpenMP settings are preserved. Cancellation, time/output/memory limits, recycling and
55-second idle cleanup bound retention; a dispatched failure is not silently retried.
The warm model uses more resident RAM until cleanup.

The integrated pre-release benchmark measured a median matched new/old elapsed ratio
of **0.313** for warm fresh-image MCP reads (about 3.20× old/new) and **0.860** for the
explicit-system CLI route. Across 540 corpus calls, 360 non-timing output comparisons
matched. These are local PNG measurements, not device, GPU or competitor rankings.
[Exact benchmark boundaries](performance-final-qualification.json) retain the measured
20aba4 binary. The later source pin `2fea917` and catalogue-complete CLI `8614c90`
are separate pre-release identities; a version change is not a new timing measurement.

Fresh app, keyboard and focus reads now run together on one WDA session, preserving
individual errors and ordered input. Delayed loopback results do not establish latency
on a physical iPhone. The release build uses Release mode; CMake's default is unchanged.

## Demo Studio

Compose an existing local recording inside a perspective-rendered generic device frame.
The desktop, CLI and MCP share revisioned scenes, preview frames and durable export jobs.
Each job freezes its scene and source; stale edits and conflicting request identities
are rejected, and existing outputs are preserved. This is a procedural QPainter
renderer, not arbitrary 3D scene editing. See [Demo Studio](demo-studio.md).

Original sound is optional: source mode trims and aligns the first audio track, preserves
initial gaps and fills a short tail with silence, then exports independently checked AAC
and video. Old scenes and new defaults stay muted; PNG previews stay silent. Audio mixing,
multiple clips and arbitrary 3D scenes remain future work.

## AirPlay and qualification limits

AirPlay audio backlog is bounded and Bluetooth pointer state is reset after reconnects.
Local fixtures cover queue and input behavior; physical sound synchronization, mouse
swipes and verified iPhone input remain unqualified. AirPlay still needs the documented
UxPlay, network, Bluetooth pairing and AssistiveTouch prerequisites.

Pixel/Omarchy Mobile installation and physical parity are pending. Prior ARM/container
artifacts do not qualify the new 1.5.0 package. The experimental virtual iPhone still
needs an x86_64 Linux host, your own Apple images and the documented resources; it is
not an Apple Simulator replacement. [Runtime limits](virtual-ios/README.md#honest-limits)
and [host requirements](linux-hosts.md) continue to apply.

## Install and upgrade

Once 1.5.0 is published, use Settings → Updates or `tomaccoctl update`. Fresh installs
use the [installation guide](getting-started.md#1-install). Package checksums, signatures,
SBOM and provenance apply to the actual release artifacts, separately from source and
fixture qualification. Updates preserve rollback through `update.rollback`.
