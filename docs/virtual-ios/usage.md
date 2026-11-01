# Use the virtual iOS device

Daily use, once [setup](setup.md) has given you a profile directory. The desktop, the
Omarchy panel and agents all use the same operations, and they see the same state.

## Start and stop

```sh
tomaccoctl call ios.virtual.configure '{"directory":"/absolute/profile"}'
tomaccoctl call ios.virtual.start '{}'
tomaccoctl call ios.virtual.status '{}'
tomaccoctl call ios.virtual.stop '{}'
```

Before a start, `tomaccoctl call ios.virtual.doctor '{}'` checks the profile, the machine, the disk space and the last
run in one report; [troubleshooting](troubleshooting.md) says what each item means.

You only configure the directory once. In the desktop, the devices tab (Apple) and the Omarchy
panel offer the same start, stop and a phone window.

`ios.virtual.start` is one durable job. It makes a private disk copy from the last verified run,
reinstalls the guest tools, boots, unlocks and opens the configured app. Expect about 9 minutes.
Follow it with `ios.virtual.status` and `job.logs`. A guest that is already running is adopted.

Phases in the status, in order: `preparing`, `booting`, `installing` (only when an app is new),
`unlocking`, `launch-accepted`. After a stop it is `stopped` or `unverified`, and after a refusal
`failed`, with `error` naming the reason.

`phase_elapsed_seconds` is how long the current phase has run, and `phases` lists each phase of the
current start or stop with its seconds. While `preparing`, `copy_progress` has the disk copy's
`bytes_done`, `bytes_total` and `percent`, with the preparation's `step` and `disk`. It compares the
size of the new copy's files with the base's, so a reflinked copy or an overlay shows as complete at once. Watched during
two live overlay starts: `step` went from `validate-payloads` to `validate-storage` and `copy-storage`, and the phases were
`preparing` 16 s, `booting` 155 to 163 s, `unlocking` 37 to 58 s.

**A start that failed halfway.** A start adopts a guest that is still running instead of booting another, so it
makes no new disk copy and goes on from the guest-service check. When a start fails after the boot, its error ends
with what to call, and `next` in the status says the same: `action` (`resume`, `unlock_then_resume`, `stop`,
`record_end`, `start_again` or `wait`), the `call` and its `arguments`, whether it `resumes` the running guest, and
`why`. An app launch whose outcome is unknown is never sent twice, so that case resumes with `{"open_app":false}`.
A start that failed before its guest ran has removed its copy and begins again from the last verified run. Unit-tested
over the start's phases, not tried on a live phone.

```json
"next": {"action": "resume", "call": "ios.virtual.start", "arguments": {}, "resumes": true,
         "why": "The last start ended while its guest kept running. Start again: ..."}
```

When a start or stop ends, `start_timing` and `stop_timing` in the status break it down into steps with their
seconds, name the `slowest` and give `total_seconds`; the run's own `timing.json`, beside its disks, keeps both. A
start's steps are `checks` (apps, a guest-tools rebuild, the kernel), `preparation`, `disk_copy` (with its `method`),
`guest_tools` (the helper that installs them), `boot_request`, `input_hashing` (with its `bytes`), `launcher`, `boot`
(until the guest service answers), `app_install`, `guest_settings`, `unlock_and_launch` and `display_check`; a failed
start has the steps it reached. A stop's are `guest_exit` (the flush and the emulator's exit), `output_hashing` and
`finalize`. A step whose times are unknown, for example from a launcher of an older version, is counted in the next
one, which lists it under `includes`. The breakdown has unit tests with synthetic step logs; it has not been read from
a live start yet.

Accepted is not the same as visible. After an accepted launch the start looks at the panel for up
to a minute, and `display_visible:false` in the status means every frame stayed black. That is a
display defect, not a working phone.

### Several profiles

Each profile directory is its own phone with its own runs. `ios.virtual.profiles` lists every directory configured so far,
the newest first, with its `run`, `phase`, whether it is `running`, `start_disks` and the number of `saved_states`; a
profile that can no longer be used is listed with its `error`.

```sh
tomaccoctl call ios.virtual.profiles '{}'
tomaccoctl call ios.virtual.configure '{"directory":"/absolute/other-profile"}'
```

Choosing another directory is refused with `ios_virtual_running` while the current profile's phone runs, and with
`ios_virtual_busy` while a start or stop is in progress; `can_switch` in the list says whether it would work now. Stop the
phone first. With a live phone running, the list showed it as `running` with `can_switch:false`, and choosing another
valid profile was refused with `ios_virtual_running`; the configured directory stayed the same.

### What a stop saves

`ios.virtual.stop` flushes the guest's storage and records whether this run may be the base of the
next start. **A run is verified only if** the guest confirmed the flush, the emulator then exited
with code 0, and the disk hashes are complete. After any other end, the next start goes back to the
last verified run and says so in its log. Whatever happened in between is lost.

- A stop works while a start is running. The start is cancelled, then its guest is stopped.
- A guest that answers but is working is never ended by force. Try again in a moment.
- A guest that does not answer is asked four times, twenty seconds apart, and only then is it ended
  without saving. The stop then fails with "ended without saving", so you know.
- A guest that never came up is forgotten with its disk copy, and the phone is simply off on its last
  verified run.
- In the desktop's dialog, **Stop** (*Beenden*) asks the guest to save first. When that is refused, it
  offers **Stop without saving** (*Ohne Sichern beenden*).

`last_run` in `ios.virtual.status` says how the last finished run ended, in words: `verified`, the launcher's
`outcome`, a `summary`, each of the `reasons` it was not verified and a `remedy`. A failed stop's message carries the
same reasons. [Troubleshooting](troubleshooting.md#stopping) has an example. Unit-tested only, not read off a live phone.

### When the phone stops responding

`liveness` in `ios.virtual.status` says whether a running phone still shows signs of life. Every status call looks at
the newest frame in the guest's frame mailbox and the size of its console log, and with `{"probe":true}` it also asks
the guest service for its status, for at most 10 seconds. `last_seen` has each sign with when it last changed.

```sh
tomaccoctl call ios.virtual.status '{"probe":true}'
tomaccoctl call ios.virtual.configure '{"stall_seconds":600}'
```

- `live`: a new frame or a guest answer within `stall_seconds` (300 unless configured, 30 to 86400).
- `quiet`: neither for `stall_seconds`. A still screen is normal, since only changed frames are published; probe
  to find out more. `console_only:true` says the console still printed: iOS keeps printing on a black or frozen
  display, so console output alone does not make a phone `live`.
- `stalled`: the guest service did not answer the last probe, and neither it nor the screen has shown life for
  `stall_seconds`.
- `booting`: during `preparing` and `booting` nothing is judged.

The status never acts on the phone. A stalled phone gets `recovery`, the calls to make in order: first
`ios.emulator.guest` with `restart_ui`, which restarts iOS's user interface without ending the guest, and then
`ios.virtual.stop`, which asks the guest to save four times before it ends it without saving. The signs are kept in
`liveness.json` in the guest job's run directory. Unit-tested only; no hung live phone has been judged with it yet.

### Time and disk limits

**A session lasts at most 24 hours.** About ten minutes before the limit, the phone stops the careful
way by itself, and repeats that every twenty seconds while the guest is busy. Only if it is still
refused at the limit is the guest frozen. `ios.virtual.status` reports a guest that ended by itself as
`guest_ended`, with `guest_saved` saying whether it closed cleanly.

Disk copies are 32 GiB disks, and at most two are kept. On a filesystem that cannot clone files the copy is streamed: it
keeps the base's holes and leaves out every 4 MiB chunk of zeros, so it takes the space of the disk's data, and the
hashes taken before and after a session read only that data. On a synthetic 4 GiB disk with 0.5 GiB of data this took
the copy from 13.6 s to 3.9 s and the launcher's hash from 32.4 s to 5.4 s; it has not been run on a live phone. Only copies Tomacco made in the profile's `installs`
directory are ever deleted.

To free that space yourself, `ios.virtual.cleanup` deletes the older copies. Without arguments it is a dry run:

```sh
tomaccoctl call ios.virtual.cleanup '{}'
tomaccoctl call ios.virtual.cleanup '{"dry_run":false}'
```

It lists each copy it would delete with its `bytes`, and `reclaimable_bytes` in all; on btrfs or XFS a copy shares blocks
with its base, so less is freed than that. It deletes only copies the start recorded in `state.json`, never the verified
base, the current run or a start's attempt, which it lists under `kept`. A copy whose disks a running guest holds is
refused, not deleted. A `guest-install*` directory the state does not know is listed under `unrecorded` and left alone;
delete it yourself if it is yours. A real cleanup waits until no start or stop runs. The dry run was run on a real profile
with a two-layer overlay chain: it listed nothing to delete and kept the base and both copies it reads (`overlay-backing`);
a real cleanup has not been run on a real profile yet.

A start checks the space first. When the filesystem of `installs` lacks room for the copy (the data the base's
disks hold, which is what a streamed copy writes, plus 1 GiB, counting the older copies the start would delete first), `ios.virtual.start` refuses at once with
`ios_virtual_disk_space`, before anything is deleted or copied. The refusal says how much to free. On a filesystem
that clones files (btrfs, or XFS with reflink) the copy shares blocks with its base and needs only the 1 GiB.
`start_disk` in `ios.virtual.status` gives the same reckoning before you start: `needed_bytes`, `free_bytes`,
`reclaimable_bytes` and `enough`. The status writes nothing to find out, so on btrfs or XFS short of room for a
full copy `enough` stays null until a start tries a clone. The refusal and `start_disk` have unit tests and a
control-plane test; neither has been met on a real full disk.

### Saved phone states

A verified run can be kept under a name and started from again later, for example a phone set up just before a test:

```sh
tomaccoctl call ios.virtual.state_save '{"name":"Clean phone"}'
tomaccoctl call ios.virtual.states '{}'
tomaccoctl call ios.virtual.state_use '{"name":"Clean phone"}'
tomaccoctl call ios.virtual.state_delete '{"name":"Clean phone"}'
```

`ios.virtual.state_save` names the last verified run, the base of the next start. It copies nothing: a start never changes
its base, and a verified run's disks do not change after its stop. The names are kept in `saved-states.json` in the profile
directory, and the start's own pruning and `ios.virtual.cleanup` keep every named run's copy (`kept` with the reason
`saved-state:NAME`). A running phone does not stop a save; it saves the last verified run, not the running one.

`ios.virtual.state_use` makes a saved run the base of the next start, which copies from it as from any verified run. The
phone must be stopped. When the current base has no name, a later start may delete its copy, so the call is refused with
`ios_virtual_state_unsaved` unless you save the current base first or pass `"discard_current":true`.

`ios.virtual.state_delete` removes the name, and the copy too when nothing else keeps it: not the base, the current run, a
start's attempt, another name or a copy an overlay reads. `kept_because` says why a copy stayed.

In the desktop, the iOS test system dialog has the same under **Saved states** (*Gesicherte Zustände*): a list, **Save
state**, **Start from it** while the phone is off, and **Delete**, which asks a second time.

Each named run keeps its own copy, up to 32 GiB, on a filesystem that cannot share blocks; a run made as an overlay keeps only its
overlay (0.4 GiB after a session). What ran on a live profile: a save while the phone ran (it named the last verified run),
`state_use` refused with `ios_virtual_state_unsaved` for an unsaved overlay run, then switched the base back and forth
between two saved runs, the next start began from the chosen one, and `state_delete` removed both names but kept both
copies, as `overlay-backing` of the later runs.

### Copy-free starts (experimental)

A copy of the base run's 32 GiB root disk takes minutes on a filesystem that cannot clone files (ext4), and a stop then
hashes the whole copy. So a start lays an overlay instead whenever it finds a `qemu-img`, and copies only without one.
A profile can choose either way:

```sh
tomaccoctl call ios.virtual.configure '{"start_disks":"overlay"}'
tomaccoctl call ios.virtual.configure '{"start_disks":"copy"}'
```

A start that lays an overlay makes the run's root disk `system.qcow2`, an empty qcow2 file whose backing file is the base's root
disk, with `qemu-img create`. The guest writes into the overlay; the base is only read. The auxiliary and SEP disks are
small and still copied. `qemu-img` is taken from beside the profile's helper engine (setup builds it there) or from
`PATH`. The engine and the Linux helper that installs the guest tools open the overlay as qcow2, and the host reads APFS
through the chain to check the Preboot volume as before.

- Each verified run adds one layer. After 8 overlays on one base the next start flattens the chain into a new raw disk
  (`qemu-img convert`), which writes the whole disk once, as a copy does. `"start_disks":"copy"` also flattens an
  overlay base.
- A copy an overlay reads is never deleted while the run on top of it is kept: the start's own pruning and
  `ios.virtual.cleanup` list it under `kept` with the reason `overlay-backing`.
- Backing files are named by absolute path, so moving the profile's `installs` breaks the runs made this way.
- Without `qemu-img` the start copies, and its log says why. With `"start_disks":"overlay"` and no `qemu-img`,
  `ios.virtual.host_check` and `ios.virtual.doctor` warn (`qemu_img`) and name where it must be. `start_disk` in
  `ios.virtual.status` has the `method` (`copy`, `overlay` or `flatten`), and `start_disks` the choice in effect.

**What ran on a live phone.** Two starts of the iPhone 18 Pro profile on the development host (ext4 on NVMe), the second
layered on the first, then a start with `"start_disks":"copy"`, which flattened that two-layer chain into a raw disk:

| | Copy (the run before) | Overlay, first | Overlay, layered | Flatten to raw |
| --- | --- | --- | --- | --- |
| Disk preparation and guest-tools install | 89 s | 16.3 s | 16.2 s | 66 s (`qemu-img convert` 54 s) |
| Start until the app's launch was accepted | 524 s | 217 s | 230 s | 569 s |
| Verified stop | 258 s | 4.9 s | 4.4 s | 199 s |
| New disk space for the run | 32 GiB | 0.4 GiB | 0.4 GiB | 24 GiB (sparse) |

Two later starts layered on a raw base, the second from a profile that names no `start_disks`, took 16 to 18 s of
preparation (5.1 s for the overlay, 0.3 to 0.6 s of input hashing), 307 and 342 s in all, and 4.3 and 4.8 s to stop
verified. The flattening that 8 overlays trigger has not run inside a start; run on a 9-overlay chain over that base
with the start's own plan and command, `qemu-img convert` took 47.9 s and wrote 24.1 GiB.

The engine opened `system.qcow2` with the `qcow2` block driver, the helper installed the guest tools through it, all stops
ended with exit code 0, a confirmed flush and complete hashes, and the second start's overlay named the first's as its
backing file, which named the raw base. Most of the difference is hashing: `start_timing` and `stop_timing` of the raw
start show 198 s to hash the 32 GiB disk before the boot and 198 s after it, which an overlay start does not spend on its
base. Not run live yet: the flattening that 8 layers trigger by themselves, and a start without `qemu-img`. A profile
whose helper was assembled by hand may lack `qemu-img` beside it; the start then copies and says so in its log.

## The graphics memory guard

A session stops the careful way when under an eighth of the graphics card stays free, so it cannot take
your desktop's memory. It checks every five seconds, and needs two short readings in a row so that a brief
peak does not end a session. Afterwards `ios.virtual.status` has `guest_gpu_memory_stop: true`, and the
desktop says why it stopped.

Why this exists: the renderer has no memory budget of its own. It holds what iOS has drawn, and it used at
most 1.7 GiB over 36 app launches on a 16 GiB card. A longer or heavier session can use more.

The guard's trip has unit tests only. It has not been seen to trip on a live guest.

**Watch it before it trips.** `host.gpu` in `ios.virtual.status` has the card's `used_bytes`, `total_bytes` and
`free_bytes` on every call, with `guard_limit_free_bytes` (the eighth the guard keeps) and a `level`: `ok`, `low` under a
quarter free, `short` under the guard's limit. While a phone runs, `source` is `guard` and the numbers are the guard's own
last check on the card that guest renders on, with `short_checks` (two in a row stop the session) and `guard_stopping`;
otherwise `source` is `sysfs`, the card with the most memory, as `ios.virtual.host_check` reads it. `host.pressure` has the
host's I/O and memory pressure (Linux PSI) against the limits a start waits for, and `blocked` names those over them.

A start that boots a guest returns `warnings` when the card is already `low` or `short`, or the host is under pressure.
They do not refuse the start. The numbers and warnings are unit-tested with recorded values; the status has not been
watched beside a live guest yet.

## Choose a model

```sh
tomaccoctl call ios.virtual.status '{}'
tomaccoctl call ios.virtual.configure '{"model":"16"}'
```

The status lists `models`, each with `id`, `name`, `panel` and `source`. The choice applies from the next
start; your data stays the same. The status also has `model` (for the next start), `running_model` and
`running_panel`. `running_panel` is the panel the current run's boot request gave the engine, so editing the
profile while a phone runs does not change it; it is null when that boot named no panel. Choose a model in the
desktop too.

| Model | Panel | Source | Result when booted once |
| --- | --- | --- | --- |
| iPhone 16 | 1179×2556 | Apple's variant | App launched, stopped and verified |
| iPhone 17, 18 Pro | 1206×2622 | derived | Home screen |
| iPhone 18 Pro Max | 1320×2868 | derived | Home screen |
| iPhone Air | 1260×2736 | derived | Home screen |
| iPhone 17e | 1170×2532 | derived | Home screen |
| iPhone Duo | 1398×2034 | derived | Home screen, one fixed panel, no folding |

"Derived" means the numbers come from Apple's variant with that model's panel; they are not Apple's own.
A model changes the screen and the interface style, not the hardware identity or the speed.

## Touch and gestures

**In the desktop.** A press, its moves and its release reach the guest as they happen, so a finger can hold,
drag and pause, and the picture keeps coming. A press near the screen's edge, or on the frame around it, starts
at the edge, which opens iOS's edge gestures by mouse. The wheel scrolls. Buttons and shortcuts do the rest:

| Shortcut | Action |
| --- | --- |
| Ctrl+Shift+H | Home |
| Ctrl+Shift+L | Side button (wake or lock) |
| Ctrl+Shift+A | App switcher |
| Ctrl+Shift+N | Notification Center |
| Ctrl+Shift+C | Control Center |
| Ctrl+Shift+B | Back (swipe right from the left edge) |
| Ctrl+V | Type the host clipboard's text into the focused field (**Paste**) |
| Ctrl+Shift+K | Give the keyboard back to the desktop, or to the phone again |

Ctrl+V and **Paste** type the host clipboard's text with the `text` action described below, in pieces of up to 1024
characters; a clipboard of more than 4096 characters is refused with a notice. Characters the guest's keyboard layout
cannot type make the guest refuse the piece. Tested in the desktop's offscreen tests; not tried on a live phone yet.

**Typing.** While the phone's view has the keyboard, **Keyboard to phone** shows highlighted under the picture
and the keys go to the phone: characters as `text`, so the host's layout decides what a key types (`@`, `ä`), and
Enter, Backspace, Tab, Escape, the arrows and keys with Ctrl, Alt or Meta as `key`. Characters typed while the
previous ones still wait are sent together. Ctrl+Shift+K, or the button, gives the keyboard back to the desktop, so
Tab and Escape work in the dialog again; a click on the screen or Ctrl+Shift+K takes it back for the phone. In the
phone's own window the rail's keyboard button does the same, and Escape still docks the window.

**Scrolling.** A wheel burst or a two-finger scroll becomes one drag that rests before it lifts, so the content stops
where the scroll stopped. The coasting a trackpad adds after the fingers lift is dropped, and lifting them sends the
drag at once.

**Speed.** **Speed** under the picture, or its button on the rail, shows how many new frames arrive per second, how
many frames were fetched, and the time from the last input to the first new frame fetched after it. That time is an
estimate: a frame that changed for another reason counts too, and an input that changes nothing within 5 seconds is
not measured.

**Screenshot with frame** saves and copies the screen through `ios.virtual.screenshot` with `frame` `device`
(described below), under `Pictures/Tomacco`.

All of this is tested in the desktop's offscreen tests only; not tried on a live phone.

The phone's window is described in the [desktop guide](../desktop.md#device-window).

**From an agent.** Use `ios.emulator.control` with the `guest_job` from `ios.virtual.status`. Coordinates run from
0 to 1 across the display.

```sh
tomaccoctl call ios.emulator.control '{"job_id":"GUEST_JOB","action":"click","x":0.5,"y":0.5}'
tomaccoctl call ios.emulator.control '{"job_id":"GUEST_JOB","action":"drag","x":0.5,"y":0.99,"to_x":0.5,"to_y":0.25,"duration_ms":1500}'
tomaccoctl call ios.emulator.control '{"job_id":"GUEST_JOB","action":"home"}'
tomaccoctl call ios.emulator.control '{"job_id":"GUEST_JOB","action":"frame","transport":"file"}'
```

- `frame` with `transport:"file"` returns a picture's path and `changed`. It is the way to watch the guest.
- An acknowledged input is not proof that iOS reacted. Look at a following frame.
- A `drag` lifts at its end point at once, which a scroll view reads as a fling. With `hold_ms` it rests there first,
  so the content stops where the finger did.
- `touch_down`, `touch_move` and `touch_up` stream one finger as it moves, for a client that stays connected
  (`tomaccoctl mcp`). A one-shot `tomaccoctl call` refuses `touch_down` with `ios_touch_lane_required`.
- There is one contact: no hover and no multitouch. A contact without an event for 5 seconds is lifted.
- An edge gesture starts at the edge: `y` 1.0 going up for Home, top-left or top-right going down for Notification
  Center or Control Center.
- `text` types on the USB keyboard, into the field that has the focus (tap it first and check a frame). iOS reads the
  keys with the layout of the guest's language; `layout` (`us` or `de`) overrides it, and a language that is neither
  English nor German must name one. `key` sends QEMU key names.

A phone without a Home button is unlocked with a slow drag up from the bottom edge, as in the second example. The start
does that by itself.

## The lock screen

The phone can lock itself again after the start, for example with guest tools that cannot turn Auto-Lock off.
`ios.virtual.unlock` leaves the lock screen the way the start does, as a short job: it observes the native screen
state, then wakes the display and presses Home (or swipes up on a model without a Home button) with the start's pacing,
until a fresh observation says the screen is unlocked. The job's `screen` is `unlocked`, or `not-checked` when the
guest tools cannot report the screen; then nothing is pressed.

```sh
tomaccoctl call ios.virtual.unlock '{}'
```

`ios.virtual.deploy` does the same before it opens the app, and `testing.run` on `virtual-iphone` before its first
step; its result has the `screen` it found. A passcode is never entered. A lock screen with one, or one that stays
locked for ten minutes, fails the job, and `testing.run` fails with `ios_virtual_locked` before it taps anything.
Tested with scripted guest replies, not yet on a live phone.

## Screenshots and recordings

```sh
tomaccoctl call ios.virtual.screenshot '{"path":"/absolute/phone.png"}'
tomaccoctl call ios.virtual.screenshot '{"path":"/absolute/phone-framed.png","frame":"device"}'
```

`ios.virtual.screenshot` saves the running phone's current screen as a PNG, from the same frame a viewer gets. `frame`
`rounded` cuts the corners round with the radius of the model the phone runs as and makes outside them transparent;
the Home-button phone keeps square corners. `device` also draws a plain black bezel around it: a drawing, not a
picture of the phone, with circular corners rather than Apple's continuous curves. Over MCP the PNG comes back inline
when it is at most 4 MiB. On a live iPhone 18 Pro a plain screenshot (1206×2622) took 0.2 s, `rounded` (corner radius
186) 0.35 s and `device` (1302×2718 with a 48-pixel bezel) 0.55 s.

```sh
tomaccoctl call ios.virtual.record '{"path":"/absolute/phone.mp4","seconds":20,"fps":10}'
tomaccoctl call job.wait '{"job_id":"RECORD_JOB","seconds":60}'
```

`ios.virtual.record` is a job that records the screen for `seconds` (up to 600) into an H.264 MP4, with ffmpeg, which must
be installed. It asks for a frame `fps` times a second (up to 30) and repeats the last picture when nothing changed or a
poll was slow, so the video lasts as long as the recording. The guest itself draws only about 10 changed frames a second,
so a higher `fps` mostly repeats pictures; the result's `frames_distinct` says how many differed. A phone that stops ends
the recording early with what it has. The pacing has unit tests, and an MP4 was made from synthetic frames with a real
ffmpeg and checked with ffprobe. A live phone recorded for 12 s at 10 fps, with Home and two swipes, gave a 1206×2622
H.264 MP4 of 12.0 s with 120 frames, 19 of them distinct (426 KiB).

`job.send` with `command` `stop` ends a recording early and keeps the video so far; the result then has
`stopped_early`. The desktop's **Record** button beside the phone's picture starts a recording of up to 10 minutes
under `Videos/Tomacco`, stops it on a second press and shows the file in the file manager. The stop was checked with a
synthetic frame source and a real ffmpeg, and on a live phone: a 60 s recording stopped after 5 s kept 4.9 s with
`stopped_early:true`.

## UI tests

`testing.run`, `testing.record` and `testing.crashes` take `virtual-iphone` as the device while the phone runs. Steps act at
pixels of the screenshot and compare whole screens with a baseline, because the guest reports no UI hierarchy to find
elements in. The [agent guide](../agents.md#ui-tests-on-the-virtual-iphone) has the steps and a table of what works on
Android, here and on an iPhone, and what ran on a live phone.

## Deploy your own app

To build a SwiftUI app for it on Linux, first import the SDK from an `Xcode.xip` you
downloaded from Apple. Tomacco does not ship that SDK or download it. Read Xcode's license first.

```sh
tomaccoctl call ios.sdk_install '{"path":"/absolute/Xcode.xip","slim":true}'
```

Then one job creates the project and builds it, and `project.app` names the result. The
project kind is `xtool`. This ran for real with xtool 1.20.1 and the Xcode 27.2 SDK; the
bundle passed the checks below, and it was not started on a phone in that run.

```sh
tomaccoctl call project.create '{"path":"/absolute/MyApp","kind":"xtool","name":"MyApp","organization":"dev.example","build":true}'
tomaccoctl call job.wait '{"job_id":"CREATE_JOB","seconds":60}'
tomaccoctl call project.app '{"path":"/absolute/MyApp"}'
```

`project.app` returns `app`, `bundle_id`, `virtual_device.accepted` with the refusal's
reason if not, and the calls below as `next`. After a change to the sources, rebuild with
`project.execute`:

```sh
tomaccoctl call project.execute '{"path":"/absolute/MyApp","platform":"ios"}'
```

By hand, this is `xtool new MyApp --skip-setup`, then `xtool dev build` in `MyApp`, which
writes `xtool/MyApp.app`. Use the path of that `.app` below. An Xcode project works the same
way after [`project.import_xcode`](../iphone.md#flutter-xcode-projects-crash-reports-and-the-app-store).

**Add an arm64 iOS `.app` directory and choose it as the app the start opens:**

```sh
tomaccoctl call ios.virtual.configure '{"add_application":"/absolute/MyApp.app"}'
tomaccoctl call ios.virtual.configure '{"application":"dev.example.MyApp"}'
tomaccoctl call ios.virtual.configure '{"clang":"/absolute/clang-22/bin/clang"}'
tomaccoctl call ios.virtual.start '{}'
```

`ios.virtual.configure {add_application}` inspects the bundle first (`Info.plist`, an arm64 executable, an allowlist of
entitlements) and refuses it with the reason otherwise. `add_entitlements` names a plist, and `remove_application` takes
the path or the bundle identifier. The desktop's iOS test system dialog (**Open test system**, *Testsystem öffnen*) does the
same with **Add app…** (*App hinzufügen…*).

**What the start does.** When the profile's guest tools do not carry the app, the next start rebuilds them with the profile's
apps, pins the new signing adapter in a derived kernel and installs each app (phase `installing`) before opening the chosen
one; `ios.emulator.build_guest_tools` with `applications` still builds a set by hand. The rebuild needs clang 22: name it with
`ios.virtual.configure {clang}` unless a `clang-22` is on `PATH`. The linker and `ldid` come from `profile.json` or the
settings. A later start installs only an app whose signed executable changed.

**What a bundle may hold.**

- Embedded frameworks and libraries (`Frameworks/*.framework`, `Frameworks/*.dylib`) are signed and trusted one by one before
  their app. An app with both has opened on the virtual device.
- App extensions in `PlugIns/` are accepted and signed; an app with a WidgetKit extension installed on the running phone
  and the extension registered with pluginkit, but a widget drawn from it was not confirmed.
- Not supported: ExtensionKit extensions (`Extensions/`), XPC services and other nested bundles, and bundles over 128 MiB.
- At most 16 MiB per executable or library, and 64 MiB per file. No symbolic links. Executables must be thin arm64, not fat
  and not arm64e.
- Entitlements must stay within `application-identifier`, `com.apple.developer.team-identifier`, `get-task-allow`,
  `keychain-access-groups` and `com.apple.security.application-groups`. Anything else is refused by name.

Every refusal names the file and the reason. The full sequence for an agent, with every status to read, is in the
[agent guide](../agents.md#deploying-an-app-to-the-virtual-ios-device).

To check a result, read `application`, `application_launch_accepted`, `display_visible` and `installed_applications` in
`ios.virtual.status`.

`ios.virtual.apps` puts the apps in one list, without asking the guest:

```sh
tomaccoctl call ios.virtual.apps '{}'
```

Each row has the `bundle_id`, the `version`, `build` and `minimum_os` that the bundle on the host has now, `installed`
(the current run's record of what a start or `ios.virtual.deploy` installed) and `opens_at_start`. A bundle that is gone
has `bundle_readable:false` and the reason. An app the run installed but the profile no longer lists appears with
`listed:false`. `installed` does not say whether you rebuilt the bundle since; the next start checks that itself. The
list has unit tests only. The bundle's own `crash_reports` are described in [troubleshooting](troubleshooting.md#an-app-does-not-open).

## Small things the start does for you

- **Launch allowance.** Under emulation iOS would end apps for being slow, so the start raises FrontBoard's launch watchdog
  (`launch_watchdog_scale` in `profile.json`, 2 to 30, default 8).
- **Time zone.** It offers the guest the host's zone. This never fails a start.
- **Auto-Lock off.** It sets Auto-Lock to Never, because an idle phone would turn off its display while the desktop keeps
  showing the last frame.

Each of these reports its outcome in the job log, and none of them fails a start. The phone also shows a full battery on
external power.
