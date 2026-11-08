# Demo Studio

Demo Studio turns an existing local screen recording into a video inside a
perspective-rendered device. The desktop workspace, JSON CLI and MCP tools use
the same scene documents, revisions, renderer and durable jobs.

This first version works entirely with local recordings. It does not connect to
phones, start an emulator, change device settings or require a running mirror.

## Make a demo

Open **Demo Studio**, choose a recording and create a scene. The initial presets
are portrait orbit, landscape orbit, square float and portrait still. Edit the
canvas, trim start, duration, background, device and poses; save the scene, then
render a preview frame. Export the saved revision when the composition is ready.

The scene file is reusable. An agent can edit a scene, render representative
frames for visual review, and export exactly the same saved composition that
the desktop opens. A stale revision is rejected instead of replacing another
client's edits. Loading a scene must not discard an unsaved desktop draft.

## Control it from an agent

Discover the current capabilities and argument schemas first:

```sh
tomaccoctl call demo.capabilities '{}'
tomaccoctl help demo.scene.create
tomaccoctl schema demo.scene.update
```

Inspect a recording and create a new scene at an unused absolute path:

```sh
tomaccoctl call demo.media.inspect '{"path":"/home/me/Videos/app.mp4"}'
tomaccoctl call demo.scene.create '{"path":"/home/me/Videos/app-demo.json","source":"/home/me/Videos/app.mp4","preset":"portrait-orbit"}'
tomaccoctl call demo.scene.get '{"path":"/home/me/Videos/app-demo.json"}'
```

Use the returned revision when updating or rendering. For example, change the
background of revision 1:

```sh
tomaccoctl call demo.scene.update '{"path":"/home/me/Videos/app-demo.json","if_revision":1,"changes":{"background":{"style":"solid","color":"#f4f2ec","color2":"#f4f2ec","image":""}}}'
```

The update returns the complete new scene and revision. Preview and export are
ordinary Tomacco jobs; `--wait` follows them to their terminal state:

```sh
tomaccoctl call demo.preview '{"path":"/home/me/Videos/app-demo.json","if_revision":2,"time":2,"output":"/home/me/Videos/demo-preview.png"}' --wait
tomaccoctl call demo.export '{"path":"/home/me/Videos/app-demo.json","if_revision":2,"output":"/home/me/Videos/demo.mp4","request_id":"app-demo-take-1"}' --wait
tomaccoctl call demo.export.inspect '{"path":"/home/me/Videos/demo.mp4"}'
```

Each submitted render freezes its scene revision and verified local media.
Exporting does not follow later scene edits. Reusing a request ID for a different
render is an error; replaying the same request returns its existing job.
Existing output files are preserved. Choose another output name for another take.
Under an agent policy restricted to write roots, scene creation and updates
check `path`; previews and exports check `output`, including the export's poster
and manifest destinations.

An export produces the MP4, a poster at `demo.mp4.poster.png` and a render manifest
at `demo.mp4.manifest.json`. Job success includes an independent decode and media
metadata check. A queued or running job is not a completed export. Jobs and logs
stay under **Builds & jobs** when the desktop closes.

## Scene format

Scene documents use `format: "tomacco.demo"`, `version: 1` and a monotonically
increasing revision. The source records its path, SHA-256 digest, display
dimensions, duration, frame rate and rotation. A changed source must be inspected
again rather than silently being substituted for a previous take.

`output` defines the dimensions, frame rate and duration. `clip.start` selects
the first source timestamp. `device` defines the procedural phone or tablet
frame, its finish, bezel, thickness and corner radius. These are original generic
models; no claim is made that their geometry reproduces a particular handset.

`background` selects a solid colour, two-colour gradient or local image.
`shadow` controls the projected shadow. These are media composition settings;
the desktop continues to use the existing Tomacco UI palette and type scale.

`camera` contains distance and field of view. Keyframes contain time in seconds,
yaw, pitch and roll in degrees, scale, normalized centre position `x`/`y`, and
easing. Supported easing functions are reported by `demo.capabilities`. Poses
interpolate at explicit frame timestamps, so a render does not depend on the
desktop's animation frame rate or window size.

## Original sound

The scene's optional `audio` object selects `mode: "muted"` or `mode: "source"`.
New scenes start muted. Older version-1 scenes without `audio` also remain
muted, so opening them never turns on sound unexpectedly.

The desktop switch and agent edits use the same scene revision. For a recording
with an audio track, save source sound explicitly:

```sh
tomaccoctl call demo.scene.update '{"path":"/home/me/Videos/app-demo.json","if_revision":2,"changes":{"audio":{"mode":"source"}}}'
```

Use the returned revision for preview and export. Source mode selects the first
audio track, trims it to the chosen source clip and encodes AAC. Initial gaps
and a shorter track are filled with silence to match the rendered video's
frame-aligned duration. A recording without audio cannot enable source mode.
The recording itself stays unchanged. This is original-sound inclusion;
volume automation, background music and mixing are separate follow-up work.

The export manifest records the requested audio mode and actual media streams.
Success requires an independent audio/video decode and audio-policy check.
`demo.export.inspect` reports whether the finished file actually has audio.

## Current bounds

The renderer exports H.264 MP4 with yuv420p pixels and PNG preview
frames. Exports are muted by default. Enable **Include source audio** to keep
the recording's original sound; PNG preview frames remain silent. Its capabilities report the installed FFmpeg/renderer readiness, size,
duration and geometry limits. A higher output frame rate can repeat source
frames; a larger canvas does not create source detail. The manifest distinguishes
source and output metadata.

This version composes one recording per scene. Live capture, audio mixing,
multi-clip editing and timeline transitions are separate follow-up work.
