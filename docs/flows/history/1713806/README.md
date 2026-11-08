# Tomacco · Flows & Logik

The editable atlas lives in **Bogen → Tomacco · App → 04 · Flows & Logik** (file `u8uvnj33hx`, page `f5zgae58fy`). Start at **00**. **C1** links operation domains and all CLI routes. **S1** separates implemented source behavior, prerequisites, pending physical qualification and future work.

Current product provenance and installation evidence are recorded in `provenance-current.json`. The source-audio follow-up extends the `4584d6b` core; exact installed source is kept separate from unchanged core source anchors. The public release remains **1.4.1**. Atlas documentation commits are separate from the installed product commit. No physical device was changed to build this atlas.

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

Existing Bogen screen pages 01–03 are referenced, not changed by this atlas. There was no public publish, repository push, tag or release.

## Source-audio follow-up

D23 models saved `audio.mode` (`muted` by default, optional `source`), source-track/AAC admission, silent PNG previews and independently checked AAC exports aligned to the first decoded video timestamp. `demo-audio-delta-state.json` records the installation gate and subsequent canvas mutations. Current provenance changes only after the qualification receipt confirms `desktop_installed: true`; the helper never installs software or calls hardware.

Current audio-delta checks: 981 actual logical nodes, 382 verified links to 56 existing targets and 36 complete PNG exports. `demo-audio-delta-acceptance.json` separates completed checks from the final root review. Historical acceptance and PNGs remain in `history/4584d6b/`. Reconcile new live-tool observations with `tools/audio_acceptance.py`; the helper deliberately leaves final approval to the root reviewer.

Final root review passed: `final-acceptance.json` and `root-final-review.json` record the current accepted atlas. Historical4584 evidence remains under `history/4584d6b/`.
