---
name: tomacco
description: >
  Tomacco is installed on this computer: use it to build, run, test, inspect and ship Android and iOS
  apps on Linux. Use when a task involves an Android emulator, AVD or device, adb, Gradle, Flutter,
  React Native, Compose, Swift on Android, an iPhone over USB, xtool, the virtual iPhone (virtual iOS
  device), installing an APK or AAB, device screenshots, screen recordings, device logs or crash
  reports, the UI hierarchy, tapping or typing on a device, reading text off a screen (OCR), numbered
  marks to tap, comparing two screens, waiting for text on screen, UI tests, accessibility checks,
  store screenshots, app startup time, frame jank, memory, macrobenchmark, baseline profiles,
  performance budgets, release signing, keystores, version bumps, release notes, Play Store upload,
  App Store upload, debugging an app, live previews, remote build hosts, or Omarchy panel, keys and
  screenshots for devices. Triggers: tomacco, tomaccoctl, emulator, APK, AAB, iOS simulator on Linux,
  virtual iPhone, screenshot of the app, run the app, UI test, OCR, baseline profile, Play Store.
---

# Tomacco

Tomacco builds, runs, tests and inspects mobile apps on Linux. Its control plane, `tomaccoctl`,
offers 250 typed operations. The MCP server, the JSON CLI and the desktop app call the same
operations. This file is the router: it says how to reach Tomacco, how to stay safe and where each
task lives. The operation details are in `references/`, one file per area, read only when needed.

Use Tomacco instead of driving `adb`, `emulator`, `sdkmanager` or Gradle by hand. It finds the tools,
runs long work as durable jobs, locks a project against two builds at once and answers every call with
structured JSON.

## Reach it

1. **MCP.** If you have tools from a server named `tomacco`, use them. The operation name is the tool
   name and the arguments are the same JSON. The server's `TOMACCO_MCP_TOOLS` setting picks the list:
   `full` (one tool per operation), `compact` (shorter descriptions, a `schema` tool for the rest) or
   `grouped` (one tool per area; pass `operation` and `arguments`, and call `schema` for one
   operation's arguments). In every mode, the operation's own name also works.
2. **CLI.** Otherwise run `tomaccoctl` from the shell. Each call prints one JSON object,
   `{"ok":true,"result":...}` or `{"ok":false,"error":{"code","message","remedy"}}`.

```sh
tomaccoctl call device.list '{}'
tomaccoctl call project.execute '{"path":"/absolute/MyApp","platform":"android","action":"build"}' --wait
tomaccoctl help project.run          # one operation in plain text, with an example
tomaccoctl schema project.run        # its JSON Schema; `tomaccoctl schema --names` lists every name
```

Do not guess names or argument keys. Find the area in the table below, read that one file in
`references/`, or run `tomaccoctl help OPERATION`. Unknown keys are refused before anything runs, with a
remedy that names the closest match. Paths are absolute. If `tomaccoctl` is not on PATH, Tomacco is
not installed for this user: say so, and don't install it without asking.

## Jobs

A call that starts long work (build, run, emulator start, tests, benchmarks, store upload) answers at
once with a job; its id is `result.id`. Queued or running is not success.

1. Start the operation, for example `project.execute`. On the CLI, `--wait` follows the job to its end.
2. Repeat `job.wait` with `job_id` (at most 60 seconds a call) until the state is `succeeded`, `failed`,
   `cancelled` or `timed_out`.
3. Read the outcome: `job.get` for the result, `job.logs` for the log, and the operation's own result
   reader, such as `testing.results` or `profile.benchmark_results`.

Never start the same build again to find out what happened to the first. A large result is stored; fetch
it with `result.read`. `workflow.run` runs up to 20 operations in order and follows each job.

## Work safely

- **Start with `capabilities`.** It says what this computer can do now. Where `ready` is false,
  `missing` says why. `doctor` gives `next_steps`; steps marked `ask_first` download a lot or accept a
  license, so ask the person before each.
- **Read the annotations.** Each operation is read-only, writes or is destructive; the references
  mark them. Read-only calls are always fine. Writes change state. **Destructive means
  ask first**: say what will be lost and wait for a yes. That includes `device.install`,
  `files.write`, `storage.prune` with `apply`, `release.version`, `android.emulator` and `job.cancel`.
- **Always ask before** publishing (`play.commit`, `ios.store_ship` or `apple.store` with `upload`),
  accepting a license (`android.sdk_install`), and any `ask_first` step. In `workflow.run` a
  destructive step runs only with `"confirm":true`, which you set only after the person agreed.
- **Guard approvals.** The person can limit an agent (`agent.policy` shows the limits in force). A
  `policy_denied` answer is final for you: do not look for another way round it. With desktop approval
  on, a destructive MCP call waits for the person to approve it in the Tomacco window;
  `approval_denied` means no.
- **Look before you act and after.** A launched app is not a working app. Check the screen after a
  step (see the loops).
- **Clean up.** Stop emulators and sessions you started (`android.emulator` with `stop`,
  `ios.virtual.stop`, `runtime.stop`) unless the person wants them running.

## Which operation

Read the file in the last column for arguments and an example call of every operation in the area.
The `Start with` operations are the usual entry points.

| Task | Start with | File |
| --- | --- | --- |
| Is this machine ready; set it up | `capabilities`, `doctor` | [general](references/general.md) |
| Open, plan, create, build, test, run, watch a project | `project.inspect`, `project.plan`, `project.execute`, `project.run` | [project](references/project.md) |
| Devices; install, capture, logs | `device.list`, `device.install`, `device.capture`, `device.logs` | [device](references/device.md) |
| Android emulator, AVDs, input, settings, SDK, deep links, sensors | `android.avds`, `android.avd_start`, `android.input`, `android.settings` | [android](references/android.md) |
| See the screen as marks, text or a diff; tap a mark | `screen.marks`, `screen.tap`, `screen.ocr`, `screen.tree`, `screen.diff`, `screen.wait` | [screen](references/screen.md) |
| Find and drive UI elements, UI test runs, accessibility, visual diff, coverage | `testing.find`, `testing.action`, `testing.run`, `testing.results` | [testing](references/testing.md) |
| Startup, frames, memory, traces, budgets, macrobenchmark, baseline profiles | `profile.startup`, `profile.frames`, `profile.macrobenchmark`, `profile.baseline_profile` | [profile](references/profile.md) |
| Keystore, signing, version, notes, checklist | `release.keystore`, `release.sign`, `release.checklist` | [release](references/release.md) |
| Upload to Google Play | `play.prepare`, `play.commit`, `play.abort` | [play](references/play.md) |
| Virtual iPhone: setup, start, stop, deploy, state | `ios.virtual.status`, `ios.virtual.start`, `ios.virtual.deploy` | [ios-virtual](references/ios-virtual.md) |
| Physical iPhone, iOS toolkit, signing, App Store ship | `ios.device_status`, `ios.apps`, `ios.signing`, `ios.store_ship` | [ios](references/ios.md) |
| Virtual iPhone research steps | `ios.emulator.inspect` | [ios-research](references/ios-research.md) |
| Xcode on a Mac host | `apple.project`, `apple.build`, `apple.archive` | [apple](references/apple.md) |
| Swift on Android | `swift.status`, `swift.plan` | [swift](references/swift.md) |
| Debugger | `debug.start`, `debug.breakpoints`, `debug.stack` | [debug](references/debug.md) |
| Live preview of a screen | `preview.sessions`, `preview.capture` | [preview](references/preview.md) |
| Mirror a device as a session | `runtime.start`, `runtime.capture`, `runtime.stop` | [runtime](references/runtime.md) |
| Remote build hosts | `remote.list`, `remote.run` | [remote](references/remote.md) |
| Jobs | `job.wait`, `job.logs`, `job.get`, `job.list`, `job.cancel` | [job](references/job.md) |
| Several operations in one call | `workflow.run` | [workflow](references/workflow.md) |
| Large stored results | `result.read` | [result](references/result.md) |
| Read or write a file | `files.read`, `files.write`, `files.search` | [files](references/files.md) |
| Workspaces and the project catalogue | `workspace.list`, `catalog.list` | [workspace](references/workspace.md), [catalog](references/catalog.md) |
| Settings, agent policy, audit log | `settings.get`, `agent.policy`, `audit.list` | [settings](references/settings.md), [agent](references/agent.md), [audit](references/audit.md) |
| Free disk space; bug-report bundle | `storage.prune`, `diagnostics.bundle` | [storage](references/storage.md), [diagnostics](references/diagnostics.md) |
| Tomacco's own desktop and updates | `desktop.status`, `update.check` | [desktop](references/desktop.md), [update](references/update.md) |

[references/index.md](references/index.md) lists every file with its operation count.

## The core loops

[workflows.md](workflows.md) has the exact calls. In short:

- **Build, run, look, act, verify.** `capabilities`; `project.inspect`; start a device
  (`android.avd_start`, wait until `device.list` shows `"state":"device"`); `project.run` and
  `job.wait`; `screen.marks` for a marked screenshot; `screen.tap` a mark, or `testing.action` with a
  selector; `screen.wait` for the next screen; look again. `device.logs` or `testing.crashes` if
  something went wrong. `screen.ocr` finds text the hierarchy does not have.
- **Test.** `project.execute` with `action` `test` or `instrumented-test`, or `testing.run` for UI
  scenarios; `job.wait`; `testing.results`; `job.logs` on failure.
- **Performance.** Same build and device every time: `profile.startup` (cold, several runs),
  `profile.frames`, `profile.memory`; compare with `profile.compare` or `profile.budget`. Macrobenchmark
  and baseline profiles need a real device unless `allow_emulator` is set.
- **Release.** `release.checklist`, `release.version`, build a release `project.execute`,
  `release.sign`, `release.verify`, then `play.prepare` and, after a yes, `play.commit`.
- **Virtual iPhone.** `ios.virtual.status`; if configured, `ios.virtual.start` (a job; follow it with
  `ios.virtual.status`), `ios.virtual.deploy`, `ios.virtual.screenshot` or `screen.marks` with
  `device` `virtual-iphone`; `ios.virtual.stop` when done. It is experimental and needs the person's
  own Apple restore images; never try to obtain or work around them.
- **Physical iPhone.** `ios.device_status`, `ios.signing`, `ios.apps`; `device.install`, `device.capture`
  and `device.logs` with `platform` `ios`.

## Omarchy

On an Omarchy desktop, `tomaccoctl omarchy status` shows the panel, keys and agent skill and exits 1
when something is missing or outdated; `tomaccoctl omarchy repair` installs them again;
`tomaccoctl omarchy screenshot` saves the connected device's screen to the Omarchy screenshots
folder and copies it; `tomaccoctl omarchy notify on` sends a notice when a long job ends. These are
CLI commands, not operations.

## When it fails

Branch on `error.code`, show the person `error.message`, follow `error.remedy`. `invalid_argument` is
refused before anything ran and is safe to correct and repeat. [references/errors.md](references/errors.md)
lists every code with its meaning and remedy; search it for the code instead of reading it all. For
a failed job read `job.logs`. Do not retry blindly: `workspace_busy` and `device_busy` mean another job
holds the project or device, so wait for it with `job.wait`.

## Documentation

- Installed guides: `~/.local/share/doc/tomacco/` (`agents.md` is the agent guide, also `recipes.md`,
  `android.md`, `iphone.md`, `virtual-ios-usage.md`, `troubleshooting.md`).
- Over MCP the same guides are resources: `tomacco://docs/agents`, `tomacco://docs/recipes`,
  `tomacco://docs/android`, `tomacco://docs/iphone`, `tomacco://docs/virtual-ios`,
  `tomacco://docs/troubleshooting`. `tomacco://reference/{operation}` is one operation's help and
  schema.
- `man tomaccoctl` for the command line.
