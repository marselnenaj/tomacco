# Desktop

The Qt desktop is Tomacco's visual workspace. It calls the same typed operations as
`tomaccoctl` and the MCP server, so everything shown here is also available to agents
(see the [agent guide](agents.md)).

Where this guide names a button, the German label follows in italics.

## Starting the app

```sh
tomacco
tomacco --device-only
tomacco --project /path/to/app --workspace runtime
tomacco --url http://localhost:5173
tomacco --folder /path/to/project/build/web
tomacco --device duo --fold open --orientation landscape
```

Open **Tomacco** in the application menu to enter the native project workspace. Explicit URL, folder or device arguments open the visual
layout lab instead (see [Visual layout lab](#visual-layout-lab)).

Tomacco remembers the last eight project folders you opened. The start screen
shows the three newest besides the open one under **Recent** (*Zuletzt*), and
the command palette (Ctrl+K) finds all of them as **Open recent project**
(*Zuletzt geöffnetes Projekt … öffnen*). Folders that no longer exist are left out.
A project folder dragged from the file manager onto the window opens as the
project and shows its overview; a single file or an `.app` bundle does not.

## Languages

The desktop speaks English, German, French, Spanish and Italian. It follows the desktop language: the
first of the system's UI languages that it has, English otherwise. Choose *System*,
*English*, *Deutsch*, *Français*, *Español* or *Italiano* under **Language** in **Settings** (Ctrl+,); the
choice is stored and applies at once. `TOMACCO_LANGUAGE=en|de|fr|es|it` overrides it for one
run. `tomaccoctl` and MCP output always stay English.

## Appearance

The desktop has a dark and a light theme. It follows the desktop's dark or light
preference, and is dark when the desktop states none. Choose *System*, *Dark* or
*Light* under **Appearance** in **Settings** (Ctrl+,); the choice is stored and
applies at once. `TOMACCO_THEME=dark|light` overrides it for one run. Animations
stop when the desktop asks for reduced motion (Qt 6.12 or later). The fonts,
Archivo and JetBrains Mono, ship with the app.

## Updates

The **Updates** part of **Settings** (Ctrl+,) chooses how Tomacco treats new releases on GitHub:

| Choice | What it does |
| --- | --- |
| **Off** | No request at all. **Check now** still asks GitHub, once, when you press it. |
| **Notify** (default) | Checks once a day while the desktop runs, and shows the new version in the status line. |
| **Automatic** | Also installs it by itself while no job, device session or virtual iOS device runs. A version that failed or that you rolled back is not installed by itself again. |

A check never delays the start and fails silently when you are offline; **Settings** shows how it ended. **What's new** opens the release's page, **Install** downloads, verifies and
installs it, and **Restart** starts the new version once it is in place: the running app keeps working until then. **Roll back** returns to the version before. If the new version does not
start after installing, the previous one is restored by itself.

An installation made by `install.py` can be updated. A source checkout, a system package or a directory you cannot write is left alone, and the section says so. Installing needs Python 3.9
or newer, for the installer. [Privacy](privacy.md) says what the checks send.

## Moving settings to another computer

**Export settings…** and **Import settings…** under **Another computer** in
**Settings** (*Einstellungen exportieren…*, *Einstellungen importieren…*) save
and read a JSON file with the desktop's preferences: appearance, language,
job notifications, the update choice, the device view's frame, activity panel,
touches and sound forwarding, the WebDriverAgent bundle ID and the web
preview's last device. Paths, window sizes, recent projects, job history and the
iOS research settings belong to this computer and are left out; Tomacco keeps
no passwords or tokens in its settings. An import checks every value, ignores
what it does not know and says how many settings it took. The update choice is
not exported or imported where a build or `TOMACCO_UPDATES` fixes it.

```json
{
  "format": "tomacco-desktop-settings",
  "version": 1,
  "application": "Tomacco",
  "settings": {"appearance": "dark", "language": "de", "Notifications/jobs": true}
}
```

## Navigation and shortcuts

A rail on the left holds every view, with names and keys in a wide window and
icons in a narrow one. Pages open under a block of colour with their name;
Files, Builds & jobs and Agents run edge to edge. Apple and Android devices have
separate categories. Narrow, tall windows are normal in a tiling window manager:
the rail stays icons, the device bar's actions become icons, and the activity
panel takes the whole stage while it is open. The smallest window is 560×480.
The window opens at the size it had when it was last closed, at most as large
as the screen, and maximised if it was; a tiling window manager still decides
the size itself. `--capture` and `--smoke-test` keep the default size.

| Action | Shortcut |
| --- | --- |
| Search views, commands and all API operations (a tool opens its form before it runs) | Ctrl+K |
| Keyboard shortcuts | F1 |
| Open a project | Ctrl+O |
| Show or hide activity | Ctrl+J |
| Settings | Ctrl+, |
| Live device / files / jobs / devices / overview | Ctrl+1 … Ctrl+5 |
| Debugger / UI tests / performance / release / agents | Ctrl+6 … Ctrl+9, Ctrl+0 |
| Save the current source file | Ctrl+S |
| Save a screenshot of the live device and copy it to the clipboard | Ctrl+Shift+S |

Tab moves through the title bar, the rail, the device bar and the page, in that
order; the control with keyboard focus has a two-pixel ring. The lists of jobs,
agent tools and project files take the keyboard too: Tab into the list, the
arrow keys move, Enter or Space opens the entry. Every control the keyboard
reaches on the main screens has an accessible name, icon buttons included,
whose tip (with its shortcut) becomes the description. The desktop UI test checks
these names with Qt's accessibility switched on; the desktop has not yet been
tried with a screen reader such as Orca.

Screenshots of the live device, from the camera button, the palette, Ctrl+Shift+S or
the device window's rail (Ctrl+Shift+S there too), are saved in `~/Pictures/Tomacco`
(or the configured Pictures directory) and put on the clipboard as an image.
The virtual iOS device's **Screenshot** button copies its capture the same way.

## Activity

The panel beside the device (Ctrl+J) shows the device's state in large type, the
jobs that are running with the newest one's log, the calls agents make to this
window through its preview socket, and recent jobs. Jobs come from every client.
Calls an agent makes directly to `tomaccoctl` leave no record unless they start a
job, so they do not appear. The panel closes by itself below 1120 px wide and
opens on request. The status line at the bottom always names the device's state
and how many jobs run.

Repeated app/build requests reuse the active job, and each device uses one
native display session. A running Flutter app exposes **Hot Reload** and a **Stop**
button (*Beenden*) instead of remaining labelled as a build. Other work in the
occupied project returns its existing job context; it does not silently queue
another app launch. The display uses immediate Android H.264 access units and
reuses image buffers.

The default workspace displays an actual native runtime and its project files.
Apple defaults to a physical iPhone over USB: Linux compiles supported Swift
apps and the iPhone executes them. Apple's Simulator remains an optional Mac
connection. The **Web preview** button (*Web-Vorschau*) opens the separate Chromium
layout lab.

## Builds and jobs

**Builds & jobs** (Ctrl+3, *Builds & Aufträge*) lists the jobs of every client,
newest first, with their state, how long they ran or have been running, and the
project they work in. **All**, **Running** and **Failed** (*Alle*, *Laufend*,
*Fehler*) above the list narrow it; Failed includes jobs that timed out. The
chosen job's log fills the rest. Beside its name, **Open log file**
(*Protokolldatei öffnen*) opens the log in the desktop's text viewer,
**Run again** (*Erneut ausführen*) starts the same request once more and
**Cancel** (*Abbrechen*) stops a job that runs. Run again works for jobs this
window started (builds, tests, lint, Run app, installs and tool forms); it
remembers the last 40 such requests across restarts. Jobs that agents or
`tomaccoctl` started carry no request the desktop could repeat.

Above the log, **Filter the log**
(*Protokoll filtern*) keeps the lines that contain a text, ignoring case, and
**Warnings** or **Errors** (*Warnungen*, *Fehler*) keep the lines at that level:
Warnings shows warnings and errors. The level is guessed from words such as
`error`, `failed`, `exception` and `warning`, and from Android's logcat letters
(`E/Tag`, ` W Tag:`). **Copy these lines** (*Diese Zeilen kopieren*) copies
exactly the lines shown; without a filter the button copies the whole log.

### Notifications

A job that ran for at least 10 seconds and ends while no Tomacco window is
active, such as a build, a test run or an app start, shows a desktop
notification: **Completed after**, **Failed after** or **Timed out after** and
the duration (*Fertig nach*, *Fehlgeschlagen nach*, *Zeitüberschreitung nach*).
Its **Open** action (*Öffnen*) brings the window back with that job's log.
Cancelled jobs, and jobs that end while you are in the window, stay quiet.
Turn it off under **Notifications** in **Settings** (Ctrl+,): **When a job ends
in the background** (*Wenn ein Auftrag im Hintergrund endet*). The desktop sends
the notification to the desktop's notification service over D-Bus
(`org.freedesktop.Notifications`); a build without Qt D-Bus sends none. On
Omarchy the panel can notify about builds too; turn one of them off if you get
both.

## Installing an app by dropping it

Drag an app from the file manager onto the device view to install it on the
device shown there, or on the chosen one when none is live. Android devices
take one or more `.apk` files (split APKs install together), an iPhone over USB
one signed `.ipa`; both go through `device.install` and show up as a job under
**Builds & jobs**. On the virtual iOS device's picture, docked or in its own
window, an `.app` bundle is added to the profile through
`ios.virtual.configure` and, while the phone runs, installed at once
(`deploy`). A file the device cannot take is refused with the reason; files that
are no app are ignored. The detached device window accepts drops too.

## Touches and coordinates

**Touches and coordinates** in the tool strip beside the live device
(*Berührungen und Koordinaten*, also in the command palette) helps when writing
UI tests. While it is on, a ring marks where the pointer presses the device and
follows the finger until it lifts, and a label beside the pointer names the
device pixel under it and the same point as fractions of the screen, for
example `250, 1500  ·  0.250, 0.750`. `runtime.input` takes the fractions as its
coordinates. Presses still reach the device as before. The choice is
stored. It applies to the Android and iPhone picture, docked or in its own
window; the virtual iOS device has its own touch mark and no coordinates.

## Device window

The **Own window** button in the tool strip beside the device (*Eigenes Fenster*) moves the
running device into a frameless, device-shaped window with its own
Home/Back/Rotate/Screenshot rail. It is the same display item moved to another
window, so no second stream is opened and pointer coordinates stay exact. Drag
it by the bezel or rail; Ctrl+wheel or the rail buttons change its size; the
close button or Esc docks it again. Agents use `preview.configure` with
`native.detached`.

Each device view, the native device and the virtual iOS device, can be detached
into its own window. The virtual iOS device's window opens pixel for pixel where
the monitor fits the panel, zooms with Ctrl+wheel up to one panel pixel per
screen pixel and keeps only a zoom the user chose; docked, the picture takes
the dialog's height and carries the model's island and rounded corners.

In **device-only mode** that window is the whole app: `tomacco --device-only`
or `desktop.open` with `device_only` starts without showing the main window.
Closing the device window then ends the session and the app; the device itself
keeps running. Losing the device ends the app after a short grace period, and
unsaved editor changes bring the main window back instead of being dropped.
Closing the main window while a device is detached switches to this mode, and
the rail's workspace button returns to the full app.

The virtual iOS device takes **live touch**: a press, its moves and its release
reach the guest as they happen, so a finger can hold, drag and pause, and the
picture keeps coming meanwhile. A press near the screen's edge, or on the frame
around it, starts at the edge, which is what opens iOS's edge gestures by mouse.
The same gestures have buttons and Simulator-style shortcuts, with Ctrl for
Command:

| Shortcut | Action |
| --- | --- |
| Ctrl+Shift+H | Home |
| Ctrl+Shift+L | Side button (wake or lock) |
| Ctrl+Shift+A | App switcher |
| Ctrl+Shift+N | Notification Center |
| Ctrl+Shift+C | Control Center |
| Ctrl+Shift+B | Back (swipe right from the left edge) |

The frames are drawn to scale per model from Apple's dimensional drawings and
the iOS 27 simulator: body, screen offset, island, notch or punch, continuous
corners and buttons, the Duo's folded and open states, and a back view.

`native.pending_avd` shows the frame of an Android emulator that is still
booting, with a startup screen, and attaches the real stream as soon as Android
is up. The Omarchy panel uses this: its start button boots the emulator without
the emulator's own window and shows only the phone.

On Omarchy the installer adds a Hyprland rule that keeps the device window
floating without border, shadow or blur. With GPU rendering the picture is cut
to the frame's corner radius; the software renderer keeps the square picture.

## Omarchy panel

The Omarchy panel is a user plugin, so system updates do not replace it. Left
click opens the panel, middle click opens the live device as its own window and
right click opens the native runtime workspace. The bar icon lights its screen
while a device is live and shows an orbiting arc while a build or task runs;
emulators and audio bridges are listed as services and do not count as activity.

The panel lists connected devices (connect, disconnect, open as window, stop an
emulator), stopped Android emulators with a start button, running and recent
jobs with their duration, and saved projects with build-and-run. Each job row
has a button that opens the job's log file in the desktop's text viewer
(`xdg-open`). Keys in the open panel: O workspace, F device window,
S screenshot, G devices, A jobs, V log of the newest job, R refresh. The same
actions are available over IPC for Hyprland bindings, for example
`omarchy-shell io.github.marselnenaj.tomacco window`, `screenshot`, `log`,
`show jobs`, `emulator <name>`, `iphone start|window|stop`, `status`, `version`
and `refresh`. Its native popup supports Escape and switching between panels.
Completion notifications can be turned off in the widget's settings, or with
`"notifyBuilds": false` in its bar entry. Filesystem state and jobs are local; the control plane
opens no network API server.

The shell compiles a plugin file once per URL and keeps it until it restarts,
so the installer writes the panel into a directory named after its content and
points the manifest there. The running shell then loads an upgrade within about
a second; no restart is needed. Earlier copies of the panel are removed.

## Toolchains and platform setup

Use the **Toolchain** view in the project overview or `tomaccoctl doctor`. Configure
existing SDK/JDK/Flutter/Gradle paths through **SDK & tool paths** (*SDK & Tool-Pfade*) or
`settings.configure`. Tomacco invokes the real upstream tools and returns
their status and logs. Android project wrappers take precedence over a
configured Gradle executable.

**Swift on Android.** Swift Android apps use `project.create` with
`kind:"swift-android"`. Install a matching Swift compiler/Android SDK and NDK,
run `swift.android_configure`, then build and deploy through the same Android
operations. The starter packages actual Swift machine code for ARM64 phones and
x86_64 emulators. Android widgets supply the native UI; SwiftUI/UIKit remain
Apple frameworks. See [Swift on Android](android.md#swift-on-android).

**Physical iPhone without a Mac.** Choose **Apple → Set up iPhone** (*iPhone einrichten*) for the
Linux route, or start with `ios.doctor`. Install matching Swift/xtool, import an
Apple-provided Xcode archive using `ios.sdk_install`, and authenticate in the
local terminal using `ios.prepare action=login`. Credentials stay with xtool.
Connect an unlocked iPhone, trust the computer and enable Developer Mode. The
setup dialog handles USB pairing, go-ios services and the signed automation
runner. The Xcode archive supplies the SDK; Xcode itself does not run on Linux.
General Xcode and Flutter iOS builds are not supported by this Linux build
route; they use the optional SSH Mac route. Protocol and setup paths are
contract-tested; execution on a physical iPhone is not yet qualified. See the
[iPhone guide](iphone.md).

**Virtual iOS device.** The devices tab (Apple) and the Omarchy panel show the
virtual iOS device's state and offer start, stop and a classic iPhone-shaped window
with a working Home button, wheel scrolling and an immediate touch mark. The
iOS test system dialog (**Open test system**, *Testsystem öffnen*) lists the profile's own apps with
their state, adds an `.app` bundle (**Add app…**, *App hinzufügen…*), removes one and chooses
the app the start opens, each through `ios.virtual.configure`. See
[using the virtual iOS device](virtual-ios/usage.md).

**iOS research engines.** **Apple → Open test system** also controls bounded
full-system iOS research on Linux. The same `ios.emulator.prepare_research`,
`ios.emulator.research` and `ios.emulator.report` operations are available to
CLI/MCP agents. Jobs use checksummed inputs and disposable APFS copies, reject
conflicting starts, retain real diagnostic logs and show the guest boot
framebuffer. This restore-shell engine boots iOS 27.0.1 without macOS and runs
own C and Embedded Swift CLI programs; graphical iOS apps and a working main
display remain unavailable there. See [setup and limits](research-engines.md#managed-full-system-runs).

For controller development, `ios.emulator.prepare_pmp`, `inspect_pmp` and `pmp`
verify and execute the original PMP firmware in an isolated ARM64 machine.
Reports preserve actual instruction/bus faults and clean up the VM. CPU identity
is configurable through CLI/MCP and the desktop, and verified before execution.
These are explicit laboratory settings, not measured silicon identities. Strict
handling still exposes unsupported Apple registers; controller services and
full iOS remain unavailable. See the [reproducible PMP workflow](research-engines.md#isolated-pmp-firmware-execution).
The separate [ASC transport model](../integrations/ios-emulator/asc-mailbox.md)
passes queue, reset and real ARM interrupt tests; it is not yet a bootable PMP
controller or integrated into the iOS machine. The opt-in
[ASC/KIC mode](../integrations/ios-emulator/asc-controller.md) connects
configuration, interrupt masks, queues and ARM timers to the PMP CPU. CLI/MCP
and the desktop panel expose the same exclusive jobs and read-only controller
reports. Reset/routing policies remain experimental; power services and
graphical iOS are not qualified. For exact fault context, the same workflow can
stop at a verified instruction, capture registers and RAM, and inspect memory
through CLI/MCP or the desktop hex view. See
[targeted firmware debugging](research-engines.md#capture-an-instruction-before-it-faults).

## Visual layout lab

The lab is a separate Chromium (Qt WebEngine) surface for responsive web
layouts, including Flutter web. Enter your dev-server URL or choose a built web
folder containing `index.html`. The folder picker also detects `build/web`,
`dist` and `public`. It serves that folder on an ephemeral **127.0.0.1** port
while the app is open. It does not run build scripts or modify the selected
project.

The included Fokus demo has a working timer, editable task, light/dark theme,
session history and navigation. It contains no external image, font or script
dependencies. Its content consumes the current profile's safe-area metadata,
including Duo's side reservations, while its background fills the full display.
Automatic system-indicator contrast follows the page's `theme-color` metadata
or body background; manual light/dark overrides are available.

### Lab controls

| Action | Shortcut |
| --- | --- |
| Address | Ctrl+L |
| Reload | Ctrl+R |
| Rotate | Ctrl+Shift+R |
| Save device PNG | Ctrl+Shift+S |
| Fullscreen / exit | F11 / Esc |
| Chromium developer tools | F12 |

Device changes and rotation preserve the current page. The Duo has four direct
presets: folded portrait, folded landscape, open portrait and open landscape.
Outside it has a round camera in the upper-right corner (rotated with the
hardware); inside it has no camera cutout. The folded shell has a straight hinge
edge and different corner radii. These details follow Apple's product-viewer
images on the [Duo design page](https://www.apple.com/iphone-duo/), rather than
the generic Dynamic Island text in the combined specifications table.

Fit scales the whole device without changing the page's CSS viewport. Chromium
and the rounded display render using each profile's declared density,
accounting for the desktop display scale. This keeps text and canvas content
sharp when the device is enlarged. Zoomed devices can be scrolled into view.
Screenshots include the device frame and are saved at 2× the preview's logical
size in `~/Pictures/Tomacco` (or the configured Pictures directory) and copied
to the clipboard.

Pages on every profile always fill the complete display: there are no blank
strips around the browser. The app can read the layout metadata below to
position its controls beside the camera and system indicators. In folded
portrait and open landscape, bars sit on the right; rotating the outer display
moves them with the hardware to the left. Open portrait uses horizontal bars.
This follows Apple's [bar layout guidance](https://developer.apple.com/videos/play/tech-talks/111462/).
Exact inset dimensions remain approximations.

The **Safe area metadata** switch (*Safe-Area-Metadaten*) supplies approximate
safe-area hints without shrinking the browser. The footer reports the complete
CSS viewport. The Safe Area overlay is a visual aid. Status indicators can be
hidden, use automatic contrast or be switched between light/dark manually.
Duo's side views use the compact ring indicator; open portrait uses a
horizontal status bar. Android profiles use their own indicator shapes.

### Color scheme, motion and network

Under **View**, *Page color scheme* (*Farbschema der Seite*) makes the page see
`prefers-color-scheme: light` or `dark`, *Reduce motion* (*Bewegung reduzieren*)
`prefers-reduced-motion: reduce`, and *Network* (*Netzwerk*) slows its requests to
Chromium DevTools' Fast 3G or Slow 3G or takes it offline. *System* and *No
throttling* leave the page as the desktop has it. Agents set the same with
`preview.configure` and can give their own latency and bandwidth.

Qt WebEngine has no setting for these, so Tomacco sends them through the Chrome
DevTools protocol of the web inspector's view. From the first change on, that
view stays attached to the page while its window is hidden, and the page's
`debugger` statements are skipped until you open the inspector. The settings last
across reloads and navigation. Requests of service workers and WebSocket traffic
are not throttled.

### App layout metadata

Tomacco injects `window.__tomaccoViewport` before page scripts run,
and dispatches `tomacco:viewport` after a device, fold or orientation
change. Reading this is optional: unmodified web apps still get the full display
viewport. The object contains only layout data, with no native commands or host
paths. It does not replace browser CSS `env()` values or native iOS
reserved-region APIs.

```js
function applyPreviewLayout(viewport) {
  // Keep backgrounds full bleed; inset interactive content as appropriate.
  console.log(viewport.barEdge, viewport.safeArea);
}
if (window.__tomaccoViewport) {
  applyPreviewLayout(window.__tomaccoViewport);
}
window.addEventListener('tomacco:viewport', event => {
  applyPreviewLayout(event.detail);
});
```

Example for folded portrait (all dimensions in CSS pixels):

```json
{
  "version": 1, "model": "duo", "platform": "ios", "display": "outer",
  "orientation": "portrait", "width": 466, "height": 678,
  "barEdge": "right", "controlsTop": 126,
  "safeArea": {"top": 0, "right": 72, "bottom": 0, "left": 0}
}
```

`platform` is `ios`, `android` or `generic`. `barEdge` is `left`, `right`, `bottom`, or `automatic` for ordinary profiles.
`controlsTop` is the suggested starting Y of vertical controls, below system UI.
A page reload is unnecessary when the metadata changes.

### Scope and device data

The Chromium layout lab does not execute SwiftUI/UIKit apps, IPA files, native
plugins or Apple services; use the native workspace and device operations for
those apps. The browser's user agent, touch environment and CSS
`env(safe-area-inset-*)` values are not emulated. Use the viewport to evaluate
responsive layouts; verify platform-specific behavior separately. The device
pixel ratio follows the profile's declared render density where the desktop
scale permits it; the UI and API report the actual viewport separately.

Model names and physical display resolutions were checked against manufacturer
specifications on 2026-10-03. iPhone logical sizes use the profiles' 3× density.
Android profiles describe reference layouts, not measured Android
configurations. Each profile records its source and confidence; real devices
can differ because of display zoom, font scale and system settings. Frames,
colors, cutouts, radii and safe-area guides are visual approximations. The
iPhone 17e uses an edge-connected TrueDepth notch with concave shoulders,
rounded lower corners and its camera on the left, following Apple's front-view
imagery. Its frame width follows the published body/display dimensions; it does
not reuse a floating Dynamic Island shape. Apple and Android models have
separate selectors, remembered selection and search text. Custom profiles
appear in their declared platform; platform-neutral imports get an additional
**Custom** category (*Eigene*).

- [iPhone 18 Pro / Pro Max](https://www.apple.com/iphone-18-pro/specs/)
- [iPhone Duo](https://www.apple.com/iphone-duo/specs/)
- [iPhone Air](https://www.apple.com/iphone-air/specs/)
- [iPhone 17](https://www.apple.com/iphone-17/specs/)
- [iPhone 17e](https://www.apple.com/iphone-17e/specs/)
- [iPhone 16](https://www.apple.com/iphone-16/specs/)
- [Pixel 11](https://store.google.com/product/pixel_11_specs)
- [Pixel 11 Pro](https://store.google.com/product/pixel_11_pro_specs)
- [Pixel 11 Pro Fold](https://store.google.com/product/pixel_11_pro_fold_specs)
- Galaxy S26 / S26 Ultra: source URLs are stored in their catalogue records.
- [Galaxy Z Fold8 Ultra](https://www.samsung.com/nz/smartphones/galaxy-z-fold8-ultra/specs/),
  [Galaxy Z Fold8](https://www.samsung.com/nz/smartphones/galaxy-z-fold8/specs/) and
  [Galaxy Z Flip8](https://www.samsung.com/nz/smartphones/galaxy-z-flip8/specs/), checked on 2026-10-31.
  Their logical sizes assume a density of 3, which Samsung does not publish; the Flip8 profile is its
  unfolded main display.
- [Galaxy Tab S12+ and Tab S12 Ultra](https://www.samsung.com/in/tablets/galaxy-tab-s12/buy/), checked on
  2026-10-31, at an assumed density of 2. The frame draws no camera notch.

`assets/models.json` holds all profiles. Qt renders the app and device frames;
Qt WebEngine renders the page. Page content has no bridge to native commands.
Requests for device permissions are denied. TLS errors are not bypassed. The
four Duo presets switch the page size, camera placement, hinge edge, corner
radii and approximate insets together. They are 2D layout states; intermediate
3D folding angles and native iOS Flex/StandBy behavior are not implemented.

### Custom devices

Export a built-in profile, give it a new `id`, edit its dimensions/density/cutout,
colors and portrait/landscape insets, then import the JSON. Built-ins cannot be
overwritten. Foldables can declare `openWidth`, `openHeight`, `openCutout` and
separate open insets. See [the profile schema](../assets/device-profile.schema.json).
Profiles contain data only. `android.calibrate` reads real `wm size/density`;
frame details and reserved insets still need visual calibration.

## Visual checks

`tomacco --capture /absolute/path.png` captures the window after 4.5
seconds and exits. Native Wayland rendering is used for visual checks: the
offscreen Qt software renderer does not render the WebEngine layer mask
correctly on the development machine.

The GUI integration check (`tests/gui_integration.py`) verifies real JavaScript
viewport dimensions, rotation, all four Duo states and their cutouts, demo
input, PNG capture and fullscreen. It needs a functioning graphical session.
Backend tests cover URL handling and the local server, including SPA fallback,
HEAD, missing assets, traversal, symlink escape, hidden files, invalid hosts and
unsupported methods; they need loopback networking.
