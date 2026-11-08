# Tomacco · Flows & Logik

The editable atlas lives in **Bogen → Tomacco · App → 04 · Flows & Logik** (file `u8uvnj33hx`, page `f5zgae58fy`). Start at **00**. **C1** links operation domains and all CLI routes. **S1** separates implemented source behavior, prerequisites, pending physical qualification and future work.

Current product provenance and installation evidence are recorded in `provenance-current.json`. Public release **1.5.0** and the installed desktop use source **a2f3ad8** / runtime **07d5586**. The source-audio, OCR and performance flows retain explicit historical core anchors; the identity bridge binds their private measurement history to the identical published source trees. Atlas documentation commits are separate from the installed product and immutable release tag. No physical device was changed to build this atlas.

## Contents

- `provenance-current.json`: current product/source-key/installation state; historical core proof stays under `history/4584d6b/`.
- `specs/`: source-backed diagrams with reusable Logic / Node instances, editable SVG connectors, branch labels, source anchors and cross-links.
- `atlas-map.json`: current diagram records, logical geometry, node/edge data, source hashes, Bogen IDs, operation mappings and route aliases.
- `curated-node-map.json`: explicit operation groups for shared lifecycle nodes. Group coverage does not imply every argument is separately drawn.
- `bogen-node-ids.json`: versioned logical-node to Bogen-layer identities, retained for reproducible map generation outside the original build directory. Refresh it when replacing diagram canvases.
- `bogen-ids.json`: canonical artboard/section/component identities. `metadata-ids-overlay.json` preserves metadata-lane edits during concurrent diagram application.
- `reviews/`: original independent reviews, kept as historical evidence; `review-resolutions.json` records corrections.
- `renders/`: current Bogen PNG exports; rendering/geometry/prototype evidence is stored alongside the map after final QA.

The 296 registry operations in 33 domains each map to an existing logical node and its source contract. Exact operation references and explicit grouped lifecycle references are distinguished in `mapping_kind`. An operation inventory is not proof that all argument combinations or devices work. The registry description remains the detailed contract.

## Regeneration and verification

Run from the repository root:

```sh
python3 docs/flows/tools/atlas.py check
python3 docs/flows/tools/atlas.py json
python3 docs/flows/tools/label_qa.py
python3 docs/flows/tools/metadata.py
python3 docs/flows/tools/build_map.py
```

`atlas.py` measures the actual Archivo and JetBrains Mono fonts, grows nodes and places labels in free space. A spec edit is incomplete until its corresponding Bogen sections are replaced and inspected. Use Bogen `html_write` one section at a time, retain artboard IDs, record new section IDs, then render and lint. Reconnect cross-links after replacements. Final top-level layout uses actual computed CSS bounds rather than fixed row heights.

The Bogen data source **logic-operation-flow-map** mirrors the operation rows. `tomaccoctl setup agents [status|remove]`, `setup status`, GUI `Studio.serveFolder`, QSettings, native window behavior and chooser helpers are outside the typed registry. No `agent.setup`, `setup.status` or `update.plan` operation is invented.

Desktop and phone use the same control program. CLI `preview` differs from the phone's internal `web-preview` extra ID. `Main.showWorkspace` handles runtime and preview separately; `ios-research` opens the devices panel. The desktop rail has eleven entries; phone navigation has four primary destinations plus More.

## Qualification limits

The current source-audio ARM package from `578819b` is container verified and staged on the Omarchy Linux ARM MacBook. Its SHA-256 is `dbe4d4be0a88a64bd7150a732188551405a33efe22dda1315b2d0c3aec999ae5`; verification and staging receipts are linked from `provenance-current.json`. Pixel installation awaits readiness after other mobile/flash work. Neither a source classification nor an ARM container run is a physical Pixel test. That MacBook is not macOS/Xcode and cannot host the x86_64-only experimental virtual iPhone.

Physical iPhone AirPlay audio synchrony, mouse swipes and verified fill remain unqualified here. The virtual iPhone is an experimental research runtime; command acceptance, a process observation and a visible scene are different evidence. Demo Studio's implemented QPainter perspective renderer is distinct from future arbitrary 3D scene work.

Existing Bogen screen pages 01–03 are referenced, not changed by this atlas. Historical statements below describe their original measurement epochs; the publication record in the following section is current.

## Source-audio follow-up

D23 models saved `audio.mode` (`muted` by default, optional `source`), source-track/AAC admission, silent PNG previews and independently checked AAC exports aligned to the first decoded video timestamp. `demo-audio-delta-state.json` records the installation gate and subsequent canvas mutations. Current provenance changes only after the qualification receipt confirms `desktop_installed: true`; the helper never installs software or calls hardware.

Historical audio-era checks (product `1713806`): 981 actual logical nodes, 382 verified links to 56 existing targets and 36 complete PNG exports. `demo-audio-delta-acceptance.json` separates completed checks from the final root review. Historical acceptance and PNGs remain in `history/4584d6b/`. Reconcile new live-tool observations with `tools/audio_acceptance.py`; the helper deliberately leaves final approval to the root reviewer.

The previous adaptive-OCR root acceptance is preserved under `history/1caa172/`; older4584 evidence remains under `history/4584d6b/`. Its acceptance belongs to that historical epoch; the current publication follow-up has a separate root-review gate.

## Optional OCR follow-up

The original adaptive-OCR product `1caa172` / runtime `e552e6f` was accepted before this engine follow-up. D12 retains opt-in `invert:adaptive`, compact `detail:matches`, frozen-input polarity reuse, target/index confidence fallbacks and actual engine/timing accounting. `auto` remains the default and still governs OCR actions, finds, waits and absence checks. Adaptive observations cannot prove absence or uniqueness. Its original cold-CLI report stays historical at `../ocr-adaptive-qualification.json`; those timings are not substituted for the current installed binary.

Historical adaptive-OCR atlas checks were 33 diagrams, 36 artboards and1,000 logical nodes. Its accepted map, IDs, labels, render hashes, changed PNGs and final root review are preserved in `history/1caa172/`. The earlier audio-era acceptance stays in `history/1713806/`.

## Historical private1.5.0 performance follow-up

`perf-delta-plan.json` pins the installed CODEcandidate `c2e2326f66879a595d27f476bf7964b49faf8c33` / runtime `e0b79fe4207a50c7f7416af8392ee4a0a5648906`. Only the new release qualification receipt can open the live-write gate. `reviews/perf-install-gate.json`, `perf-desktop-update-receipt.json` and `perf-installed-smoke.json` bind the three verified binaries, protected desktop settings and the warm-worker/reap smoke check. Public release remains1.4.1; no tag, public publish, device session restart or Pixel access was performed by this atlas work.

D12 separates the OCR polarity policy from worker/CLI selection and bounded worker lifecycle. Persistent eligible MCP calls can reuse one isolated model; cold/custom/large/busy routes keep CLI behavior. Startup unavailability may fall back before recognition; a dispatched failure is not silently run again. D09 adds fresh parallel WDA app/keyboard/focus reads with per-result errors and ordered input. The original independent engine review is retained unchanged, with the exact formatting/type-alias/local-lint/poll-rounding source delta documented in `reviews/perf-reviewed-source-delta.json` and its patch.

`reviews/perf-final-qualification.json` records the exact installed cfad72c binary against the **private1caa172 baseline**, separately from a public-release comparison:417 matrix cases,540 corpus calls and360 matching complete non-timing output comparisons. Known OCR misses remain. Warm fresh-image recognition, cache hits and cold/custom CLI are distinct routes; keeping the warm model raises resident memory until cleanup. These offline fixtures do not establish physical-device latency, GPU presentation, native-speaker translation quality or competitor leadership.

A separate verified **publicv1.4.1** comparison is preserved at `reviews/public-v1.4.1-performance.json`:36 paired warm fresh-image reads in default `auto`, median matched new/old ratio0.31207 (about3.20× old/new). It uses three prerecorded app crops and the common API; the known query misses remain. This is separate from the private1caa corpus and from first recognition, custom CLI, hardware, GPU or competitor performance. The signed public baseline's source/tag is recorded separately from installedc2e and eventual1.5 publication.

At that private-candidate epoch the last qualified/staged ARM package remained578819b. The newly published1.5.0 ARM qualification is recorded separately below; Pixel installation remains pending.

Private-candidate atlas checks: **33 diagrams,36 artboards,1,023 actual logical nodes,296 operations,382 links and56 exact existing targets**. All33 spec hashes and the unchanged registry hash were checked. All36 computed artboard bounds were read back after reflow with no overlap;491 independent branch-label measurements have no collision. Only00/D09/D12/S1 PNGs were regenerated; all36 dimensions and hashes were verified. Four documentation design-verification reports are agent screenshot self-assessments, not human approval. Critical render lint and hard/soft brand violations are zero; fixed-canvas token/grid/copy and320px resizing suggestions remain advisory, with no critical finding waived.

The archived private acceptance records its original review state. `public-1.5-delta-acceptance.json` and the current `final-acceptance.json` leave **root final review pending** for the publication metadata. `tools/perf_acceptance.py` reconciles live observations, actual identities, source/spec hashes, links and all36 exports without granting that approval. The Bogen checkpoint is recorded in `reviews/perf-bogen-observations.json`.

## Published1.5.0 provenance follow-up

Release **v1.5.0 is published** at [GitHub](https://github.com/marselnenaj/tomacco/releases/tag/v1.5.0), public source `a2f3ad8bdf731fd7337f6fcfd6bfd4b40f3d2c59`, runtime `07d5586310f3f10f0fd1a6382e795f6b1ba19d8a`. The publication receipt verifies43 assets and latest status. The source identity bridge preserves every source tree from the private qualification history; unpublished author-email headers changed. Historical c2e/cfad reports retain their measured identities. The installed final CLI is e2ee6fa; root verified its instruction section identical to cfad, re-ran final benchmarks and installed-worker smoke. No recognition behavior changed.

The published experimental aarch64 package is qualified for a native Linux GUI with13 exact interactive checks and a mock control plane, plus the actual CLI in offline tests. The public archive retains all171 qualified ELF files byte for byte. This does not qualify three native distributions, the incomplete QEMU-browser run, or physical Pixel/AirPlay use. The earlier578819b staging receipt remains historical.

`history/private-1.5-c2e2326/` preserves the previously accepted private-candidate map, acceptance, provenance and render hashes. `reviews/public-1.5-*` and `public-1.5-delta-acceptance.json` describe only this publication/metadata epoch. All36 PNG exports,33 specs,1,023 actual nodes and382 prototype links are checked again; final root approval is separate. No additional page, brand, device session or live website deployment was changed by this atlas follow-up.
