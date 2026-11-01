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
size of the new copy's files with the base's, so a reflinked copy shows as complete at once. These
fields have unit tests; they have not been watched during a live start yet.

Accepted is not the same as visible. After an accepted launch the start looks at the panel for up
to a minute, and `display_visible:false` in the status means every frame stayed black. That is a
display defect, not a working phone.

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

### Time and disk limits

**A session lasts at most 24 hours.** About ten minutes before the limit, the phone stops the careful
way by itself, and repeats that every twenty seconds while the guest is busy. Only if it is still
refused at the limit is the guest frozen. `ios.virtual.status` reports a guest that ended by itself as
`guest_ended`, with `guest_saved` saying whether it closed cleanly.

Disk copies are 32 GiB each, and at most two are kept. Only copies Tomacco made in the profile's `installs`
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
delete it yourself if it is yours. A real cleanup waits until no start or stop runs. The cleanup has unit tests and a
control-plane test; it has not been run on a real profile yet.

A start checks the space first. When the filesystem of `installs` lacks room for the copy (the base's disks
plus 1 GiB, counting the older copies the start would delete first), `ios.virtual.start` refuses at once with
`ios_virtual_disk_space`, before anything is deleted or copied. The refusal says how much to free. On a filesystem
that clones files (btrfs, or XFS with reflink) the copy shares blocks with its base and needs only the 1 GiB.
`start_disk` in `ios.virtual.status` gives the same reckoning before you start: `needed_bytes`, `free_bytes`,
`reclaimable_bytes` and `enough`. The status writes nothing to find out, so on btrfs or XFS short of room for a
full copy `enough` stays null until a start tries a clone. The refusal and `start_disk` have unit tests and a
control-plane test; neither has been met on a real full disk.

### Copy-free starts (experimental)

Every start copies the base run's 32 GiB root disk, which takes about 9 minutes on a filesystem that cannot clone files
(ext4). A profile can ask for an overlay instead:

```sh
tomaccoctl call ios.virtual.configure '{"start_disks":"overlay"}'
```

The next start then makes the run's root disk `system.qcow2`, an empty qcow2 file whose backing file is the base's root
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
- Without `qemu-img` the start copies, and its log says why. `start_disk` in `ios.virtual.status` has the `method`
  (`copy`, `overlay` or `flatten`), and `start_disks` the choice.

The plan, the qcow2 reading, the pruning and the launcher's block device have unit tests. None of it has run on a live
phone yet. What a first live run has to show: that the patched engine boots iOS from `system.qcow2` (`-blockdev`
driver `qcow2`, backing opened read-only by QEMU), that the helper installs the guest tools through it, that a stop
still ends with exit code 0 and complete hashes, that the next start layers on that run, and how long a start takes then.

## The graphics memory guard

A session stops the careful way when under an eighth of the graphics card stays free, so it cannot take
your desktop's memory. It checks every five seconds, and needs two short readings in a row so that a brief
peak does not end a session. Afterwards `ios.virtual.status` has `guest_gpu_memory_stop: true`, and the
desktop says why it stopped.

Why this exists: the renderer has no memory budget of its own. It holds what iOS has drawn, and it used at
most 1.7 GiB over 36 app launches on a 16 GiB card. A longer or heavier session can use more.

The guard's trip has unit tests only. It has not been seen to trip on a live guest.

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

## Screenshots and recordings

```sh
tomaccoctl call ios.virtual.screenshot '{"path":"/absolute/phone.png"}'
tomaccoctl call ios.virtual.screenshot '{"path":"/absolute/phone-framed.png","frame":"device"}'
```

`ios.virtual.screenshot` saves the running phone's current screen as a PNG, from the same frame a viewer gets. `frame`
`rounded` cuts the corners round with the radius of the model the phone runs as and makes outside them transparent;
the Home-button phone keeps square corners. `device` also draws a plain black bezel around it: a drawing, not a
picture of the phone, with circular corners rather than Apple's continuous curves. Over MCP the PNG comes back inline
when it is at most 4 MiB. The framing and the PNG writer have unit tests; a screenshot of a live phone has not been
taken with it yet.

```sh
tomaccoctl call ios.virtual.record '{"path":"/absolute/phone.mp4","seconds":20,"fps":10}'
tomaccoctl call job.wait '{"job_id":"RECORD_JOB","seconds":60}'
```

`ios.virtual.record` is a job that records the screen for `seconds` (up to 600) into an H.264 MP4, with ffmpeg, which must
be installed. It asks for a frame `fps` times a second (up to 30) and repeats the last picture when nothing changed or a
poll was slow, so the video lasts as long as the recording. The guest itself draws only about 10 changed frames a second,
so a higher `fps` mostly repeats pictures; the result's `frames_distinct` says how many differed. A phone that stops ends
the recording early with what it has. The pacing has unit tests, and an MP4 was made from synthetic frames with a real
ffmpeg and checked with ffprobe; a live phone has not been recorded with it yet.

`job.send` with `command` `stop` ends a recording early and keeps the video so far; the result then has
`stopped_early`. The desktop's **Record** button beside the phone's picture starts a recording of up to 10 minutes
under `Videos/Tomacco`, stops it on a second press and shows the file in the file manager. The stop was checked with a
synthetic frame source and a real ffmpeg; not on a live phone.

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
