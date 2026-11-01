---
name: tomacco
description: >
  Tomacco is installed on this computer: use it to build, run, test and inspect Android and iOS apps
  on Linux. Use when a task involves an Android emulator or device, adb, Gradle, Flutter, React Native,
  Compose, an iPhone over USB, xtool, the virtual iPhone (virtual iOS device), device screenshots,
  screen recordings, device logs, UI hierarchy, UI tests, store screenshots, app startup time, frame
  jank, memory or other app performance. Triggers: tomacco, tomaccoctl, emulator, AVD, APK, AAB,
  iOS simulator on Linux, virtual iPhone, screenshot of the app, run the app, UI test, Play Store
  screenshots.
---

# Tomacco

Tomacco builds, runs and tests mobile apps on Linux. Its control plane, `tomaccoctl`, offers 228
typed operations. The MCP server, the JSON CLI and the desktop app all call the same operations, so
anything the desktop does, you can do.

Use it instead of driving `adb`, `emulator`, `sdkmanager` or Gradle by hand. It finds the tools,
runs long work as durable jobs, locks a project against two builds at once and answers every call
with structured JSON.

## Reach it

1. **MCP.** If you have tools from a server named `tomacco`, use them. Each operation is a tool with
   the same name and arguments, such as `device.list` or `project.run`.
2. **CLI.** Otherwise run `tomaccoctl` from the shell. It prints one JSON object per call:
   `{"ok":true,"result":...}` or `{"ok":false,"error":{"code":...,"message":...,"remedy":...}}`.

```sh
tomaccoctl schema --names              # every operation name, one per line
tomaccoctl help project.run            # one operation in plain text, with an example
tomaccoctl schema project.run          # its JSON Schema
tomaccoctl call device.list '{}'
tomaccoctl call project.execute '{"path":"/absolute/MyApp","platform":"android","action":"build"}' --wait
```

Do not guess operation names or argument keys. Look them up with `tomaccoctl schema --names` and
`tomaccoctl help OPERATION`. Unknown keys are refused before anything runs, with a remedy that names
the closest match. Paths are absolute.

If `tomaccoctl` is not on PATH, Tomacco is not installed for this user. Say so and point the person to
`https://github.com/marselnenaj/tomacco`; don't install it without asking.

## Work safely

- **Start with `capabilities`.** One call says what this computer can do now: Android build,
  emulator, devices, the virtual iPhone. Where `ready` is false, `missing` says why. `doctor` gives
  `next_steps` to set up what is missing; steps with `ask_first` download a lot or accept a license,
  so ask the person before each.
- **Wait for jobs.** Builds, runs, emulator starts and tests answer at once with a job (`result.id`).
  Queued or running is not success. Repeat `job.wait` (at most 60 seconds a call) until the state is
  `succeeded`, `failed`, `cancelled` or `timed_out`, or use `--wait` on the CLI. Read `job.logs` when
  it fails. Never start the same build again to find out what happened to the first.
- **Read the error.** Branch on `error.code`, show the person `message`, follow `remedy`.
  `invalid_argument` is refused before anything runs and is safe to correct and repeat.
- **Ask before destructive calls.** Operations annotated `destructiveHint` (see
  `tomaccoctl schema OPERATION`) delete or overwrite data, for example `device.install`,
  `files.write`, `storage.prune` with `apply`, `ios.virtual.state_delete` and `job.cancel`. Say what
  will be lost and wait for a yes. Publishing (`play.commit`, uploads with `ios.store_ship` or
  `apple.store`) and license acceptance always need the person's yes.
- **Several steps in one call:** `workflow.run` runs up to 20 operations in order and follows each
  job. A destructive step runs only with `"confirm":true`, which you set only after the person agreed.
- **Check the screen.** A launched app is not a working app. Look with `device.capture` (Android or a
  USB iPhone) or `ios.virtual.screenshot` (the virtual iPhone).
- **Clean up.** Stop emulators and sessions you started (`android.emulator` with `stop`,
  `ios.virtual.stop`, `runtime.stop`) unless the person wants them running.

## Which operation

| Task | Operations |
| --- | --- |
| Open or plan a project | `project.inspect`, `project.plan`, `project.create` |
| Build, test, run | `project.execute`, `project.run`, `project.watch` |
| Devices and emulators | `device.list`, `android.avds`, `android.avd_start`, `android.emulator` |
| Look at the app | `device.capture`, `device.screenshot`, `android.hierarchy`, `device.logs` |
| Drive the UI | `testing.find`, `testing.action`, `android.input` |
| UI tests | `testing.run`, `testing.results`, `testing.record`, `testing.accessibility` |
| Store screenshots | `android.settings`, `android.store_screenshots` |
| Performance | `profile.startup`, `profile.frames`, `profile.memory`, `android.performance` |
| Virtual iPhone | `ios.virtual.status`, `ios.virtual.start`, `ios.virtual.deploy`, `ios.virtual.screenshot` |
| iPhone over USB | `ios.device_status`, `ios.apps`, `ios.signing` |
| Jobs | `job.wait`, `job.logs`, `job.get`, `job.list`, `job.cancel` |

[examples.md](examples.md) has a call for each common task.

## Documentation

- Installed guides: `~/.local/share/doc/tomacco/` (`agents.md` is the agent guide, also `recipes.md`,
  `android.md`, `iphone.md`, `virtual-ios-usage.md`, `troubleshooting.md`).
- Over MCP the same guides are resources: `tomacco://docs/agents`, `tomacco://docs/recipes`,
  `tomacco://docs/android`, `tomacco://docs/iphone`, `tomacco://docs/virtual-ios`,
  `tomacco://docs/troubleshooting`. `tomacco://reference/{operation}` is one operation's help and
  schema.
- `man tomaccoctl` for the command line.

The virtual iPhone is an experimental research feature. It needs the person's own Apple restore
images; never try to obtain or work around them.
