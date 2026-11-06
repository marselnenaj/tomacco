# Agent platform: desktop surfaces and the contract for the mobile lane

What the `feat/agent-platform` branch adds to the desktop, which shared components it uses, and what
the mobile lane (Bogen app design, adaptive native mobile UI) needs to mirror. Nothing here touches the
Bogen app file or the brand site; every string below is `qsTr` and listed for the i18n lane in
`docs/agent-platform-progress.json` under `i18n_strings_for_reconciliation`.

## Surfaces

| Surface | File | Backend operations | Primary action |
| --- | --- | --- | --- |
| Handoff banner (top band of the main window) | `qml/HandoffBanner.qml`, `qml/HandoffModel.qml` | `handoff.list` (poll every 3 s while a window is open), `handoff.complete`, `handoff.cancel` | **Done** |
| Session state word under the live device | `qml/NativeWorkspace.qml` (`deviceState`) | `runtime.list`, `runtime.start` errors (`device_busy`, `runtime_configuration_conflict`, `workspace_busy`), `handoffs.waiting` | none (status) |
| Saved screenshot notice | `qml/NativeWorkspace.qml` (`noteSavedFile`) | `runtime.capture` | **Show file** |
| Route playback (QA environments view) | `qml/EnvironmentPanel.qml` (route section) | `gpx.inspect`, `gpx.play`, `gpx.status`, `job.send` (`pause`, `resume`, `stop`), `job.get` | **Play route** |

Components used: `StudioButton` (`accent` for the one primary action, `danger` for Stop and Delete, `inverted` on
bands), `StudioPanel`, `StudioTextField`, `StudioCheckBox`, `Caption` text, `Theme` tokens only (tomato accent band,
`Theme.accentText` on it, flat, no new gradients or shadows). The handoff band reuses the update band's layout.

## States the mobile UI must show, not hide

- `handoff`: an agent waits for the person. Word **Your turn**, tone busy, the band shows `kind`, `reason`,
  `instructions`, **Done** and **Cancel**. **Done** calls `handoff.complete {id}`; an agent over MCP cannot.
  Never ask for or type a passcode or password; the note says so.
- `reconnecting`: a session ended and a new start runs. Word **Reconnecting**, tone busy.
- `busy_elsewhere`: another session or process holds the device (`runtime.start` refused). Word **In use
  elsewhere**, tone warn; choosing another device clears it.
- `connecting`, `live`, `failed`, `ended` (**Disconnected**), `idle` as before.
- Route playback: `queued` → **Starting playback…**, `running` → **Playing point N of M** or **Paused at point N
  of M**, `succeeded` → **Finished** or **Stopped** (the last position stays), other → **Playback ended: state**.
  The previous position is unknown and is never claimed restored.

## Responsive rules verified on the desktop

- Handoff banner: tested at 360, 560 and 1380 px wide and at 100, 175 and 200 % interface size; the title
  wraps, the reason is at most three lines, both buttons stay inside the band.
- Route section: part of the QA environments panel, whose layout test runs at 560, 960 and 1380 px and 100,
  175 and 200 %; the file button, speed field, loop switch and the three playback buttons flow.
- On a phone the same band belongs above the content with the two actions on one row at 360 px (wrap
  the title first), RTL mirrors the row, and with the screen keyboard open the band must stay reachable
  (it is not a dialog, so it never blocks the keyboard).

## Agent-facing contract the UI relies on

- `handoff.list {state:"waiting"}` answers `{handoffs:[{id, kind, reason, instructions, session, created,
  expires_at}], count}` newest first; an older `tomaccoctl` without the operation makes the poll fail quietly.
- `runtime.state` carries `input_capabilities`; `runtime.observe` is the fresh look an agent takes after
  **Done**. The UI never replays an agent's step.
- `gpx.status {job_id}` answers `{progress:{state, index, total, lap, lat, lon}}` written by the playback job
  with every fix.

## New strings (contexts)

`HandoffModel`: Face ID or Touch ID, Passcode, Sign-in, Purchase, Permission, Your turn on the device.
`HandoffBanner`: %1 · an agent is waiting for you, %1 · an agent is waiting for you (%2 more), Done, Done: I
did it on the device, Cancel, Do it on the device itself. Tomacco never asks for your passcode or password.
`NativeWorkspace`: An agent is waiting for you: %1, Your turn, Reconnecting, In use elsewhere, Show file,
Screenshot saved. `EnvironmentPanel`: the route playback strings. `Operations`: the titles of `runtime.observe`,
`runtime.gesture`, `runtime.actions`, `handoff.*`, `gpx.*`, `device.resolve`, `diagnostics.report_draft`.
The complete list with contexts is in `docs/agent-platform-progress.json`.
