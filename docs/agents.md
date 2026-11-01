# Agents

How a coding agent, or a script, drives Tomacco. The JSON CLI, the MCP server and the desktop all
call the same typed operations, so an agent can do everything the desktop can.
[Agent patterns](agent-patterns.md) shows how to plan, wait and recover with this contract.

`tomaccoctl` is a compiled Rust program. Run it directly, not through `python3`. Neither the CLI,
MCP nor the build workers need Python; repository tests, the installer and the optional iOS
preparation recipes do.

## Connect a client

`tomaccoctl setup` connects Claude Code and Codex through their `mcp add` commands, and Gemini CLI,
Cursor, VS Code, Zed and OpenCode by adding a `tomacco` entry to their JSON settings: `~/.gemini/settings.json`
(`mcpServers`), `~/.cursor/mcp.json` (`mcpServers`), `~/.config/Code/User/mcp.json` (`servers`),
`~/.config/zed/settings.json` (`context_servers`) and `~/.config/opencode/opencode.json` (`mcp`), with `$XDG_CONFIG_HOME` in place of `~/.config` when it
is set. It asks first for each agent it finds and keeps
everything else in the file. A file with comments, such as Zed's usual settings, or a file that is a
symbolic link is left alone; setup prints the entry to add by hand. Only the file handling is tested,
on a scratch home directory; whether each agent then starts the server has not been checked here.
[T3 Code](https://github.com/pingdotgg/t3code) and similar apps run these agents for you, so they use the
same connection: once setup has connected Claude Code, Codex, Cursor or OpenCode, a session T3 Code starts
with that agent should have Tomacco's tools. This follows from how T3 Code starts the agents and has not
been tried here.

By hand, register the absolute path of `tomaccoctl`:

```sh
claude mcp add --transport stdio --scope user tomacco -- "$(command -v tomaccoctl)" mcp
codex mcp add tomacco -- "$(command -v tomaccoctl)" mcp
```

For another user or an uninstalled checkout, give that absolute path instead. This is a local
stdio server: no network port and no token. The installer never changes client settings; only `tomaccoctl setup` does, after asking. See the
[Claude Code](https://code.claude.com/docs/en/mcp) and
[Codex](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) documentation.

Any MCP stdio client gets the same tools. The resources `tomacco://catalog`, `tomacco://jobs`,
`tomacco://workspaces`, `tomacco://debug` and `tomacco://runtimes` expose profiles and recent work.
`tomacco://capabilities` is the result of the `capabilities` operation and `tomacco://devices` that of
`device.list`.

`resources/templates/list` names three resource templates that mirror read-only operations:

| Template | What it reads |
| --- | --- |
| `tomacco://jobs/{id}` | The job as `job.get` answers it, as JSON. |
| `tomacco://jobs/{id}/log` | The last 64 KiB of the job's log as text, secrets masked. `job.logs` reads the rest. |
| `tomacco://reference/{operation}` | The operation as `tomaccoctl help` describes it, then its JSON Schema with its annotations. |

`completion/complete` completes `{operation}` with operation names and `{id}` with the IDs of the 100
most recent jobs.

The guides are resources too, as Markdown of the installed version: `tomacco://docs/agents` (this guide),
`tomacco://docs/recipes`, `tomacco://docs/getting-started`, `tomacco://docs/android`, `tomacco://docs/iphone`,
`tomacco://docs/virtual-ios` (its usage guide), `tomacco://docs/troubleshooting` and `tomacco://docs/faq`. Links
inside them are relative to the repository's `docs/` directory.

The server also offers three prompts, short recipes a client can show as commands:

| Prompt | Arguments | What it asks the agent to do |
| --- | --- | --- |
| `setup` | none | Call `doctor` and work through `next_steps` in order, asking before each step marked `ask_first`. |
| `run-android-app` | `path` | Inspect the project, find or start an Android device, `project.run` it, wait for the job and capture the screen. |
| `run-virtual-iphone` | `path` | Check the virtual iOS device, find or build the xtool app, deploy it to a running phone or configure and start one, then look at the screen. |

- Every tool states all four MCP annotations: `readOnlyHint`, `destructiveHint`, `idempotentHint`
  and `openWorldHint`. They come from each operation's `annotations` in `tomaccoctl schema`.
- MCP requests can run at the same time.
- `notifications/cancelled` stops that request. Cancelling `job.wait` leaves its job running; use
  `job.cancel` to cancel a build and its descendants.
- A `tools/call` with `_meta.progressToken` gets `notifications/progress` once a second while it runs.
  For `job.wait` the message is the job's state and its latest log line, and `total` is the wait in
  seconds. A call that finishes within a second gets none.
- After `logging/setLevel` with `info` or `debug`, a running `job.wait` sends each new line of its job's
  log as `notifications/message`, level `info`, logger `job ID`. Secrets are masked as in `job.logs`.
  Until the client sets a level, the server sends no log messages.
- A tool result also comes as `structuredContent`: an object result as it is, any other result
  as `{"result": ...}`, and an error as `{"error": {...}}`. The text content is unchanged.
- Native screenshots up to 4 MiB are embedded as MCP images. Larger ones keep a path and digest.
- A client that declares `elicitation` (form mode, protocol 2025-06-18 or later) is asked to put a yes-or-no
  question to the person, with `elicitation/create`, before these steps:
  `android.sdk_install` without `accept_licenses` (a yes sets it; a no installs without accepting, as before),
  `play.commit`, `ios.store_ship` with `upload` and `apple.store` with `upload` (a no stops the call), and an
  `ios.virtual.setup` stage marked `ask_first` (a yes repeats the call with `confirm:true`). A no answers
  `consent_declined`, with the question and the client's `action` under `details`. A question waits up to 10
  minutes. A client without elicitation is never asked, and these calls behave as from the CLI.

### A shorter tool list

The full tool list is every operation with its whole schema, 265,234 bytes of `tools/list` response in this
version. For an agent with a small context, set `TOMACCO_MCP_TOOLS` in the `env` of the client's server entry:

| Value | Tools | `tools/list` response |
| --- | --- | --- |
| `full` (the default) | One per operation, with every description. | 265,234 bytes |
| `compact` | One per operation; each description is its first sentence, arguments have no descriptions, and annotations equal to MCP's defaults are left out. | 136,247 bytes |
| `grouped` | One per area, such as `android` or `job`: 25 tools. Each takes `operation`, the operation's name, and `arguments`, its own arguments, and its description lists the area's operations with their titles and whether each is read-only, writes or is destructive. | 29,261 bytes |

The sizes were measured with this version's schema. In `compact` and `grouped` there is also a `schema` tool that
answers one operation's whole schema, as does the resource `tomacco://reference/{operation}`. A grouped tool's
annotations are its area's strictest: `destructiveHint` when any of its operations is destructive, so a client that
asks before destructive tools asks for the whole area. Calling an operation by its own name works in every mode.
An unknown value is the full list, with a warning on standard error.

## The contract

**Do not guess operation names or arguments.** `tomaccoctl schema` prints every operation with its
JSON Schema: arguments, defaults, enums and bounds. `tomaccoctl schema OPERATION` prints only that
operation, as `result`, and `tomaccoctl schema --names` prints just the names, one per line, as plain
text. Unknown arguments are refused before dispatch.
A mistyped operation name or argument key gets a `remedy` naming the closest match, such as
`Did you mean project.run?` or `Did you mean arguments.path?`. With no close match, the remedy for an
unknown key lists the keys that are accepted. The [operation reference](reference/README.md) is the same
schema as Markdown pages, generated for this version.

```sh
tomaccoctl schema
tomaccoctl schema project.run
tomaccoctl schema --names
tomaccoctl doctor
tomaccoctl call device.list '{}'
tomaccoctl call project.inspect '{"path":"/absolute/project"}'
tomaccoctl call project.plan '{"path":"/absolute/project","platform":"android"}'
tomaccoctl call project.execute @/absolute/build-request.json
```

Arguments are inline, from a file (`@FILE`) or from standard input (`@-`). A response is one JSON
object on stdout, `{"ok":true,"result":...}` or
`{"ok":false,"error":{"code":...,"message":...,"remedy":...}}`. An error exits nonzero. The
[error reference](reference/errors.md) lists every code with what it means and what to do. In MCP mode,
standard output has protocol messages only. Opening or inspecting a project never runs its build
scripts.

When standard output is a terminal, the JSON is indented for reading. Through a pipe or into a file it
is one line, exactly as before. `--pretty` indents it anywhere, and `--compact` keeps one line at a
terminal.

For people typing commands, `tomaccoctl help OPERATION` (or `tomaccoctl call OPERATION --help`)
describes one operation in plain text: what it does, each argument with its type, choices, limits and
default, whether it changes anything, and an example call built from the schema. `man tomaccoctl`
describes the commands and flags; the installer and `cmake --install` put the page in `share/man/man1`.

`tomaccoctl completions bash`, `zsh` or `fish` prints a completion script
for the commands, their flags and the operation names after `call`, `schema` and `help`. Load it with
`source <(tomaccoctl completions bash)`, the same for zsh, or `tomaccoctl completions fish | source`.
The script asks `tomaccoctl schema --names` for the names, so it stays current after an update. The
bash script is tested; the zsh and fish scripts have not been run in those shells yet.

To plan, `tomaccoctl call capabilities` says in one call what this computer can do right now:
under `android` its `build`, `emulator` (with its AVDs) and `devices`, and under `ios` its `build`
(xtool), `devices` and `virtual_iphone` (configured, running, phase). Each has `ready` and `missing`, the
names of what it lacks, such as `kvm` or `xtool`; `doctor`'s `next_steps` say how to set them up. It
runs `doctor`'s tool checks, so it takes a few seconds. Over MCP its schema has an `outputSchema`, and the same result is the
resource `tomacco://capabilities`.

Use `TOMACCO_HOME=/absolute/test-state` to isolate job, toolchain, profile and emulator state. The
desktop's **All tools** tab shows every operation and its schema.

## Jobs

Long operations return a job, and **queued or running is not success**. Keep the id, which is in
`result.id`.

```sh
tomaccoctl call job.wait '{"job_id":"JOB_ID","seconds":30}'
tomaccoctl call job.logs '{"job_id":"JOB_ID","offset":0}'
tomaccoctl call job.get '{"job_id":"JOB_ID"}'
```

`job.wait` waits at most 60 seconds per call, and answers `running` as a valid result: repeat it, or
read `job.logs` with `next_offset`, until the state is `succeeded`, `failed`, `cancelled` or
`timed_out`. Then check the exit status and artifacts.

From the CLI, `--wait` does that loop for you:

```sh
tomaccoctl call project.execute '{"path":"/absolute/project","platform":"android"}' --wait
```

The job's log goes to standard error as it grows, and standard output gets one envelope at the end. A
job that succeeded is `{"ok":true,"result":JOB}` with exit status 0. One that ended `failed`,
`cancelled` or `timed_out` is an error with code `job_failed`, `job_cancelled` or `job_timed_out`,
the final job in `error.details.job`, and exit status 1. A result that is not a job is printed as it
is. Interrupting `--wait` leaves the job running.

- **Artifacts** carry an absolute path, size, SHA-256 and `modified_in_job`. An old output is not
  evidence of a new build after a failed job. Incremental builds can legitimately report
  `modified_in_job:false`.
- **Durability.** Jobs persist in SQLite and survive a client disconnecting, but a sandbox or process
  supervisor can still kill their workers.
- **Cancelling.** `job.cancel` ends the process group.
- **Workspace locks.** Build, run and task jobs lock their project while they run. An identical
  active request reuses its job, and different work in the same project answers `workspace_busy` with
  the active job. A Flutter run holds the lock until it exits. Keep independent projects in separate
  directories. Commands you run outside Tomacco are not covered.
- **Retries.** `android.sdk_install`, `ios.wda_prepare` and `android.connect` take
  `retry: {"count":N,"backoff_seconds":S}`: after a failure they try again up to `N` more times (at
  most 5), waiting `S` seconds (5 by default), doubled each time up to 600. A job repeats all its
  steps, never after being cancelled or timing out, lists each attempt under `result.attempts` and
  has a `retry` event per new attempt. `android.connect` repeats only `adb connect`, never pairing,
  and returns `attempts`. Other operations do not take `retry`, because repeating them is not known
  to be safe.
- **Heavy jobs wait their turn.** At most two builds run at once: `project.execute`, `project.run`
  (except a Flutter session) and `ios.flutter_build`. Others stay `queued`, with `phase:"queued"`
  and a `queued_reason` naming the jobs that hold the slots, and start when one ends. Emulators that
  `android.avd_start` starts can be capped as well, for as long as they run; they have no cap by
  default. `settings.configure {"jobs":{"max_builds":N,"max_emulators":N}}` changes the caps, and `0`
  removes one. A queued job can be cancelled, and its timeout starts only when it runs.
- **Finding jobs.** `job.list` returns the newest jobs first and takes `state` (a list), `operation`
  (a prefix of the operation that started the job, such as `project.`), `project` (an absolute
  directory the job locks or runs in), `since` (seconds since 1970) and `limit`. For the next page,
  pass the id of the last job as `before`. Jobs started by earlier versions have no recorded
  operation, so an `operation` filter leaves them out.
- **Timeline.** `job.events` lists a job's state changes, phase changes and cancellation requests in
  order, each with a `time` in seconds since 1970; a state that ended carries `exit_code` and
  `error`. Pass the returned `next_after` as `after` to read only what is new. Jobs created by an
  earlier version have only their creation and end, marked `synthesized`.

### Several steps in one call

`workflow.run` runs up to 20 operations in order and answers with each step's result. A step that starts
a job is followed until the job ends, or until its app is up (phase `ready`, as a Flutter run that keeps
its job is), unless the step has `"wait":false`. The first step that fails, or whose job does not
succeed, stops the rest: the answer is `workflow_step_failed` with every step so far under
`details.steps` and the operations not run under `details.skipped`. Unknown operations and wrong
arguments are refused before the first step runs.

```sh
tomaccoctl call workflow.run '{"steps":[{"operation":"project.execute","arguments":{"path":"/absolute/project","platform":"android","action":"build"}},{"operation":"job.get","arguments":{"job_id":{"from_step":0,"pointer":"/result/id"}}}]}'
```

An argument value `{"from_step": N, "pointer": "/result/..."}` is replaced by that JSON pointer into
step `N`'s record; for a followed job, `/result` is the finished job, so `/result/artifacts/0/path` is
its first artifact. A destructive step, such as `device.install`, and one that publishes (`play.commit`, an
upload with `ios.store_ship` or `apple.store`) runs only with `"confirm":true` on that step; without it
nothing runs and the answer is `workflow_needs_confirmation`. Set it only after the
person agreed. `timeout` (1800 seconds by default) covers the whole workflow; when it runs out, the
current job keeps running. The call holds its MCP request or terminal until it ends, so for a single
long build `project.execute` with `job.wait` is just as good.

### Cleaning up

Job records and logs stay in the data directory (`TOMACCO_HOME`, by default `~/.local/share/tomacco`) until you remove them.
`storage.prune` lists what is older than `older_than_days` (30 by default): records and logs of jobs that ended, log files without a
job, and report directories under `reports/`. `max_job_log_mb` also lists the oldest finished jobs until their logs fit. It is a dry run
that changes nothing and reports the size of each entry of the data directory under `usage`. Show the user the candidates, and only then
repeat the call with `apply:true`. Queued and running jobs, emulators, SDKs, the virtual iOS device, settings and the artifacts a job built
in a project are never removed.

```sh
tomaccoctl call storage.prune '{"older_than_days":14}'
tomaccoctl call storage.prune '{"older_than_days":14,"apply":true}'
```

### Bug reports

For a bug report, `diagnostics.bundle` (or `tomaccoctl report [FILE]`) writes one `.tar.gz`: version, host,
`doctor`, settings, and the record, timeline and log end of the most recent jobs that did not succeed
(`jobs`, 10 by default; `log_kb`, 64 by default). It sends nothing. Secrets of known shapes are masked, job
environments keep only their variable names and the home directory becomes `~`. Tell the user where it is
and that they should read it before sharing it.

```sh
tomaccoctl report /absolute/tomacco-report.tar.gz
```

### Session log and replay

Set `TOMACCO_SESSION_LOG` to an absolute file path, in the shell or in the `env` of your MCP client's server entry, and every
operation called through `tomaccoctl call` or `tomaccoctl mcp` is appended to that file as one JSON line: `time`, `operation`,
`arguments` with recognised secrets masked, `mutating` (the operation is not read-only), `ok`, `duration_seconds`, and a short
`result` (a job's ID and state, an object's keys, a list's length) or the `error` code and message. The file is created owner-only.
Calls an operation makes itself, and the job polling of `call --wait`, are not logged.

`tomaccoctl replay FILE` calls the logged operations that change something again, in order, waits for each job one of them starts,
and stops at the first failure. It skips, and lists with the reason, calls that only read (`read_only`), that failed when they were
logged (`failed_originally`), `job.*` calls, which name jobs of the old session (`job_reference`), and calls whose arguments were
masked (`redacted`). `--dry-run` lists what it would call without calling anything.

```sh
TOMACCO_SESSION_LOG=/absolute/session.jsonl tomaccoctl call project.run '{"path":"/absolute/MyApp","platform":"android","device":"emulator-5554"}'
tomaccoctl replay /absolute/session.jsonl --dry-run
tomaccoctl replay /absolute/session.jsonl
```

## Android

Full guide: [Android](android.md). Check prerequisites with `doctor`, `android.sdk_packages` and `android.avds`.

```sh
tomaccoctl call project.create '{"path":"/absolute/NewApp","kind":"android","name":"NewApp","organization":"dev.example"}'
tomaccoctl call project.execute '{"path":"/absolute/NewApp","platform":"android","action":"build","format":"apk"}'
tomaccoctl call device.list '{"platform":"android"}'
tomaccoctl call project.run '{"path":"/absolute/NewApp","platform":"android","device":"emulator-5554"}'
```

`"kind":"compose"` creates a Kotlin and Jetpack Compose app with one Compose UI test instead; run it
with `"action":"instrumented-test"`. `project.inspect` reports a Gradle app's application ID, variants
and SDK levels under `android`, read from its files. `project.dependency_updates` reports which of a
Gradle build's dependencies and plugins, and of a project's pub, npm (or pnpm and Yarn) and SwiftPM
dependencies, have newer versions, as a read-only job. `project.analyze` runs `dart analyze`, ESLint and `tsc`
as the project has them and lists their findings with file, line, severity and code; see
[Android](android.md#static-analysis).

`project.watch` takes the arguments of `project.run` and keeps the app current while you edit: a job that runs the app once,
then polls the project's files and, after they have stayed unchanged for `debounce_ms` (800 by default), calls `project.run`
again and waits for it. Build output, dependencies and tool state (`build`, `.gradle`, `.dart_tool`, `node_modules`, `Pods`,
`.git` and similar) are not watched; `ignore` adds directory names. A Flutter project gets one Flutter session and Hot Reload on
each change instead, and a new session when that one has ended. Each cycle writes one line to the job's log, such as
`cycle 3: 1 file changed (app/src/main/java/Main.kt): built, installed and launched in 41.2 s (job ID)`. Cancel the job to stop;
the run it started is cancelled with it.

```sh
tomaccoctl call project.watch '{"path":"/absolute/NewApp","platform":"android","device":"emulator-5554"}'
tomaccoctl call job.logs '{"job_id":"WATCH_JOB_ID"}'
```

With `device:"virtual-iphone"` an xtool project is rebuilt and deployed on the running virtual iPhone on each save (see
[deploying an app](#deploying-an-app-to-the-virtual-ios-device)); `xtool`, `.xtool` and `.bsp` (xtool's output) are not watched, and a phone that is off is refused
before the job. Its line reads like `cycle 2: 1 file changed (Sources/MyApp/ContentView.swift): built in 38.0 s, deployed in 24.1 s,
pid 612, no new crash report in 10 s; succeeded after 74.9 s (job ID)`, or `unchanged, opened in 3.2 s` when the signed app did not change.
Unit-tested; not run on a live phone.

A Flutter session from `project.run` takes `job.send` with `reload` (Hot Reload), `restart` (Hot Restart) or `quit`, and while it
runs `job.get` shows its Dart VM service and, when Flutter serves them, DevTools URIs under `result.flutter`. A React Native or Expo
debug run takes `reload` through Metro's `/reload` endpoint. See [Android](android.md#flutter).

Then look: `device.capture` for evidence, `android.hierarchy` for the UI tree, and assert the app's state
after each action. Prefer `testing.find` and `testing.action` over coordinates when the app exposes
accessible controls.

Before UI tests or screenshots, `android.settings` fixes dark mode, font scale, animations, airplane
mode and an app's language, and reads them back:

```sh
tomaccoctl call android.settings '{"device":"emulator-5554","dark_mode":"off","font_scale":1,"animations":false}'
```

`android.store_screenshots` captures store screenshots as a job across devices, app languages and light and dark
mode, each screen reached by its own `testing.run` steps, into fastlane supply's folders
(`THEME/LOCALE/images/phoneScreenshots/01_home.png`), and puts each device's settings back afterwards.
`report.json` flags sizes Google Play refuses under `play_problems`.

`android.display` locks the orientation or overrides display size and density, and `reset` undoes
both.

`android.notifications` lists the notifications shown, for asserting that the app posted one, and
`android.app_files` lists or pulls a debuggable app's private files.

`device.info` sums up an Android device under `health`: Android version and security patch, battery,
free storage, screen size and density, and uptime. `device.label` gives a device a nickname or pins it;
`device.list` then shows the nickname as `name` and lists pinned devices first.

`android.pair` pairs a phone over Wi-Fi by QR code: show the person the code it returns, then call it with
`action` `wait` until it pairs.

`android.each` runs steps such as install and launch on every connected Android device as one job,
with each device's result in the job's `report.json`.

`android.package_inspect` reads what a built APK or AAB declares, without a device: package,
version, SDK levels, permissions, ABIs and signing certificate digests.
`android.size_report` breaks one down by size per category, ABI and density, estimates the download, and compares with a `baseline` build.
`device.install` also takes one `.aab` and installs it through bundletool.

For the emulator's settings, `android.avd_configure` changes a stopped emulator:

```sh
tomaccoctl call android.avd_configure '{"name":"TomaccoQA","gpu":"host","vulkan":"off","memory_mb":4096,"cores":4}'
tomaccoctl call android.avd_start '{"name":"TomaccoQA","headless":true}'
```

`android.emulator` with `action` `stop` saves a Tomacco-managed emulator's Quick Boot snapshot first, and the next
`android.avd_start` resumes from it; its result's `boot` says `quick` or `cold` and why. `cold_boot:true` boots fully.
`camera_image` makes the emulator's camera show a PNG or JPEG, for apps that scan QR codes or documents. A foldable
profile such as `pixel-11-pro-fold` gives the AVD a hinge. See [Android](android.md#quick-boot).

`android.sensors` drives an emulator's simulated sensors: turn it by gravity, shake it, cover the proximity sensor,
set the light, touch the fingerprint sensor for a biometric prompt, or fold a foldable:

```sh
tomaccoctl call android.sensors '{"device":"emulator-5554","action":"orientation","orientation":"landscape"}'
tomaccoctl call android.sensors '{"device":"emulator-5554","action":"fingerprint"}'
```

`android.app_links` reads an app's App Links verification per domain, asks Android to verify them again, says which
app a link opens (`resolve`), and approves or denies domains by hand for tests; `android.deep_link` opens the link and
names the activity that took it.

`android.network_capture` captures an emulator's traffic, or what one app sends, after `opt_in:true`, and summarises
hosts, TLS server names, HTTP requests without their query and DNS queries. See
[Android](android.md#network-capture) for what it records and what it needs.

## Swift on Android

Full guide: [Swift on Android](android.md#swift-on-android). The tested toolchain is Swift 6.4 and NDK 30. Wait for
`swift.android_configure` before you build.

```sh
tomaccoctl call settings.configure '{"android_ndk":"/absolute/Android/Sdk/ndk/30.0.16248370","swift_android_sdk":"swift-6.4.0-RELEASE_android"}'
tomaccoctl call swift.android_configure '{}'
tomaccoctl call project.create '{"path":"/absolute/SwiftCounter","kind":"swift-android","name":"SwiftCounter","organization":"dev.example"}'
tomaccoctl call project.execute '{"path":"/absolute/SwiftCounter","platform":"android","action":"build"}'
tomaccoctl call swift.execute '{"path":"/absolute/SwiftCounter/Swift","platform":"linux","action":"test"}'
tomaccoctl call project.run '{"path":"/absolute/SwiftCounter","platform":"android","device":"emulator-5554"}'
```

## iPhone over USB

Full guide: [iPhone](iphone.md). **A physical iPhone has not been qualified.** Start with `ios.doctor`. Connect
`runtime.start` before `project.run`, so the runner does not take the foreground afterwards.

```sh
tomaccoctl call ios.prepare '{"action":"login"}'
tomaccoctl call ios.sdk_install '{"path":"/absolute/Xcode.xip"}'
tomaccoctl call project.create '{"path":"/absolute/MyiOSApp","kind":"xtool","name":"MyiOSApp","organization":"dev.example"}'
tomaccoctl call project.execute '{"path":"/absolute/MyiOSApp","platform":"ios","format":"ipa","sign":true}'
tomaccoctl call runtime.start '{"platform":"ios","device":"ACTUAL_UDID","runner_bundle_id":"INSTALLED_RUNNER_ID"}'
tomaccoctl call project.run '{"path":"/absolute/MyiOSApp","platform":"ios","device":"ACTUAL_UDID"}'
tomaccoctl call ios.ipa_inspect '{"path":"/absolute/MyiOSApp.ipa"}'
```

`ios.ipa_inspect` needs no device: read its `warnings` before an install fails on an expired profile or the wrong
bundle id. It reads what the files claim and verifies no signature.

This route builds xtool's SwiftPM format; `project.import_xcode` writes it for an `.xcodeproj`. Tomacco neither ships nor downloads the SDK.
Read Xcode's license first.

### On the connected iPhone

[Guide](iphone.md#working-with-the-connected-iphone). Tested with stand-ins only; no iPhone was connected.

```sh
tomaccoctl call ios.device_status '{"device":"ACTUAL_UDID"}'
tomaccoctl call device.logs '{"platform":"ios","device":"ACTUAL_UDID","bundle_id":"dev.example.MyiOSApp","duration":60}'
tomaccoctl call ios.signing '{"device":"ACTUAL_UDID","path":"/absolute/MyiOSApp"}'
tomaccoctl call device.app '{"platform":"ios","device":"ACTUAL_UDID","bundle_id":"dev.example.MyiOSApp","action":"stop"}'
tomaccoctl call ios.app_files '{"device":"ACTUAL_UDID","bundle_id":"dev.example.MyiOSApp","action":"pull","path":"Documents","output":"/absolute/documents.tar"}'
tomaccoctl call testing.run '{"path":"/absolute/ui-tests","directory":"/absolute/ui-results","devices":["ACTUAL_UDID"],"platform":"ios"}'
tomaccoctl call ios.record '{"device":"ACTUAL_UDID","path":"/absolute/screen.mp4","seconds":20}'
```

- `ios.device_status` says whether the phone is paired, trusted, unlocked and in Developer Mode. Show the user its
  `next_steps`: the steps on the phone (tap Trust, unlock, turn on Developer Mode) only they can do.
- `device.logs` with `bundle_id` keeps that app's process; the job names it as `process`.
- `ios.signing` lists profiles on the phone and in the project with expiry, team and devices, and the signing
  certificates they name. Read `warnings`: a free account's profile lasts 7 days.
- `device.app` launches, stops and uninstalls with go-ios; `ios.apps` has one plain row per app under `installed`.
  `ios.app_files` lists or pulls a development build's container as `android.app_files` does.
- `testing.run` with `platform:"ios"` runs scenarios through the WebDriverAgent of each iPhone's `runtime.start` session;
  start that first. `resource_id` is the accessibility identifier, `tap` and `swipe` take points, and the
  [table in the guide](iphone.md#ui-tests) shows the rest.
- `ios.record` records the screen to an MP4 as a job from go-ios's screenshot stream; `job.send` `stop` keeps what was recorded.
  `device.screenshot` falls back to go-ios when `idevicescreenshot` is missing.

### Xcode projects, Flutter, crash reports and the App Store

Full guide, with what was run for real and what was not:
[iPhone](iphone.md#flutter-xcode-projects-crash-reports-and-the-app-store). These call a pinned commit of Joshua Warren's
omarchy-apple-dev, which nothing fetches until `ios.toolkit_install` is called. `ios.doctor` reports its state under
`linux_toolkit`, and each thing in `missing` has a `remedy`. Nothing runs sudo or downloads from Apple.

```sh
tomaccoctl call ios.toolkit_install '{}'
tomaccoctl call project.import_xcode '{"project":"/absolute/MyApp.xcodeproj"}'
tomaccoctl call ios.crash_reports '{"device":"ACTUAL_UDID","pattern":"MyApp*","directory":"/absolute/crashes"}'
tomaccoctl call ios.crash_symbolicate '{"report":"/absolute/crash.ips","binaries":["/absolute/MyApp.app/MyApp"]}'
tomaccoctl call ios.toolkit_install '{"toolchain":true,"xcode_xip":"/absolute/Xcode.xip"}'
tomaccoctl call ios.store_ship '{"path":"/absolute/MyiOSApp"}'
tomaccoctl call ios.flutter_setup '{}'
tomaccoctl call ios.flutter_build '{"path":"/absolute/flutter_app"}'
```

- `project.import_xcode` returns at once with `adapter`, `warnings` and the `next` call. Tell the user about the warnings: each is
  something the Xcode project does that the adapter does not.
- `ios.toolkit_install` with `toolchain` takes minutes and about 15 GB, and without `xcode_xip` succeeds with `sdk_ready:false` in its
  report. Do not fetch Xcode for the user. An SDK from `ios.sdk_install` does not count for this private toolchain.
- **`ios.flutter_setup` and `ios.flutter_build` are untested in Tomacco:** they ran only against fakes. Setup downloads about 7 GB and changes
  one file in the Flutter install, so ask first.
- `ios.store_ship` with `upload` publishes to the user's App Store Connect account, so ask first. A live upload has not been qualified.

## A Mac over SSH

Full guide: [the Mac route](iphone.md#the-mac-route). For Xcode projects, Apple's Simulator, Flutter iOS and store
validation, with a Mac you supply. Typed operations are `apple.devices`, `apple.project`, `apple.build`, `apple.test`,
`apple.archive`, `apple.export`, `apple.simulator`, `apple.device`, `apple.profile` and `apple.store`.

```sh
tomaccoctl call remote.configure '{"name":"mac","host":"my-ssh-mac","directory":"/Users/me/Projects/MyApp"}'
tomaccoctl call remote.check '{"name":"mac"}'
tomaccoctl call remote.sync '{"name":"mac","path":"/absolute/MyApp"}'
tomaccoctl call remote.run '{"name":"mac","argv":["xcodebuild","-list"]}'
```

## The virtual iOS device

Experimental. Full guides: [what it is](virtual-ios/README.md), [setup](virtual-ios/setup.md),
[usage](virtual-ios/usage.md). It needs the user's own restore images and an Image4 boot ticket (IM4M).

**Can this machine run it?** `ios.virtual.host_check` answers before anything is built: architecture, processors and
VT-x/AMD-V, `/dev/kvm`, memory, free disk on the filesystem of `directory` (or the profile's `installs`), Vulkan drivers
and graphics memory, the helper's APFS module against the running kernel, and `passt`. Each entry in `checks` has a
`status` of `pass`, `warn` or `fail` and a `remedy`; `ready` is false when one fails. It is read-only.

**Will the configured phone start?** `ios.virtual.doctor` adds the profile to those checks, in the same shape: every
input `profile.json` names, whether the guest tools and the engine were built from this checkout's sources, the saved
states' disks, how the last run ended, whether the next start's disk copy fits, and the graphics card and host pressure
right now. Also read-only.

**Setting it up.** `ios.virtual.setup {work}` with the person's files plans the whole chain from what is already done (`stages`, `next`,
the person's files under `inputs`, the machine under `host`); `step:"run"` starts the next stage as a job and skips done
ones. Its refusals are `ios_setup_*` codes with a remedy: `ios_setup_needs_input` names the person's files a stage lacks,
`ios_setup_confirm` means ask the person first, `ios_setup_manual` is the restore, run by hand from `commands`.

**No profile yet?** `python3 tools/ios_virtual_profile.py --check` lists every input of a start that is missing or
wrong, in chain order, each with the command that makes it. The JSON on stdout has them under `missing` (`input`, `flag`,
`path`, `problem`, `make`). With everything present, `--directory /absolute/profile` plus `--cryptex`, `--boot-inputs`,
`--kernel`, `--tools` and `--install` writes `profile.json` and `state.json`, never over an existing profile, and returns
the `ios.virtual.configure` call to make next. **The user supplies the restore images, already decrypted, and one IM4M. No
tool makes those. Stop and tell the user when the IM4M is what is missing, rather than working around it.**

**Driving it.** `ios.virtual.status` reports the shared state; `ios.virtual.start` and `ios.virtual.stop` are durable jobs. See
[start and stop](virtual-ios/usage.md#start-and-stop).

- `display_visible:false` means iOS took the launch but every frame stayed black. That is a display defect, not a working phone.
- `ios.emulator.control` is input and frames, on the `guest_job` of `ios.virtual.status`. Poll it with `action:"frame"` and
  `transport:"file"` to watch the guest; see [touch and gestures](virtual-ios/usage.md#touch-and-gestures).
- `ios.virtual.screenshot {path, frame}` saves the screen as a PNG, with `frame` `rounded` or `device` for round corners or a
  plain bezel. `ios.virtual.record {path, seconds, fps}` is a job that records it to an MP4 with ffmpeg; see
  [screenshots and recordings](virtual-ios/usage.md#screenshots-and-recordings).
- `ios.virtual.configure {model}` selects the iPhone for the next start. `ios.virtual.status` has the ids, and also reports
  the current run's `running_panel` as `WIDTHxHEIGHT@HZ:WIDTH_MMxHEIGHT_MM`.
- `ios.virtual.status` reports the time-zone outcome as `time_zone`, and `installed_applications` (bundle identifier to the
  cdhash on the current run's volume) next to `applications`, the profile's own bundles.
- `ios.virtual.apps` joins both in one list, with each bundle's `version`, `build` and `minimum_os` read on the host,
  `installed`, `opens_at_start`, and installed apps the profile no longer lists (`listed:false`). It asks no guest.
- During a start, `phase_elapsed_seconds`, `phases` and, while `preparing`, `copy_progress` show where it is. `start_disk.enough`
  false means the next start will be refused with `ios_virtual_disk_space`; its remedy says how much to free.
  `ios.virtual.cleanup` (a dry run unless `dry_run:false`) deletes older disk copies; ask the user before deleting.
- `ios.virtual.syslog {process, levels, contains, offset}` reads the phone's console log as lines with `process`, `pid`,
  `level` and `message`; pass `next_offset` back as `offset` to follow it. `bundle_id` keeps one listed app's lines: its
  executable's own (the name comes from the bundle on the host) and launchd's about its service, by name, so they continue
  across relaunches.
- `ios.virtual.profiles` lists the profile directories configured so far; `ios.virtual.configure {directory}` switches
  between them only while the current phone is off (`ios_virtual_running` otherwise).
- `ios.virtual.state_save {name}` keeps the last verified run under a name without copying it, `ios.virtual.states` lists
  the names, `ios.virtual.state_use {name}` makes one the base of the next start (the phone must be off) and
  `ios.virtual.state_delete {name}` removes a name and the copy only it kept; ask the user before deleting. See
  [saved phone states](virtual-ios/usage.md#saved-phone-states).
- A window for the phone: `preview.configure` with `ios_guest:{detached:true}`. Asked while no guest is running, the request is
  kept for ten minutes. `workspace.ios_research.guest.detach_pending` in the returned state tells whether it is still waiting.

**Guest operations.** `ios.emulator.guest {job_id, operation, argument}`, with `job_id` being the `guest_job`. They are `inspect`,
`status`, `lookup`, `cancel`, `install`, `register`, `open`, `logs` (`development`, `quiet`, `buffer_texture` and the owned app's
logs), `setup`, `setup_finish`, `restart_ui`, `flush`, `effects`, `screen_state`, `display_state`, `foreground_state`,
`display_refresh`, `graphics_state`, `surface_state`, `crash_reports`, `persistence_probe`, `watchdog_scale`, `icon_cache`, `time_zone`,
`auto_lock`, `receive_bundle`, `policy_add`, `debug_server`, `network_state`, `hardware_probe`, `gestalt`, `developer_mode` and
`keyboard_layout`. `tomaccoctl schema` has each argument. Some that need more words:

- `crash_reports` takes any report-name prefix, so a developer's app's reports are read like iOS's, and tomaccoctl projects
  `crash_report_identities` from the report's own process name. `ios.virtual.crash_reports {process, directory}` saves the same
  reports as files on the host and lists their paths for `ios.crash_symbolicate`. A run's reports reach the next run's disk only when that run became
  the base, so read them during the run.
- `logs quiet` reads tomacco-quiet's record: the services it unloaded, its locationd guard's questions and the processes it ended.
- `time_zone Area/City` sets the guest's zone through iOS's own tzlink service, and answers `previous_link` and `link`.
- `auto_lock never` (or `original`) sets Settings' Auto-Lock and reads it back. The start sends it by itself to a guest that reports
  `auto_lock_supported`.
- `network_state` lists the interfaces' IPv4 addresses and tries one name lookup; `hardware_probe` counts an IOKit class's services;
  `gestalt` reads one MobileGestalt key; `developer_mode status`/`enable` reads or arms iOS's Developer Mode; `keyboard_layout` reports
  the guest's language. `receive_bundle`, `policy_add` and `debug_server` are the guest side of `ios.virtual.deploy` and
  `ios.virtual.debug`; use those operations, not these directly.

The run's evidence (`userspace_failures`, `respawn_storms`) lists a locationd the guard ended as kind `guard-ended`, an expected
entry. A widget extension ended by chronod's deadline is accounted only for iOS's own extensions, and Settings' or a developer's app's
SIGTRAP stays unaccounted, so a clean reading of a run is not mistaken for a clean session.

### Deploying an app to the virtual iOS device

Two things decide before the boot whether an executable runs in this guest: its code-directory hash must be in the trust cache the guest
booted with, and installd admits an ad-hoc signed app only through the development-signing adapter's policy. Both belong to the guest-tools
build, so a developer's app is staged into that build and installed by the start. The rules for a bundle are in
[usage](virtual-ios/usage.md#deploy-your-own-app); every refusal names the file and the reason.

A SwiftUI app to run there comes from one job. `project.app` then names the bundle, says whether the rules below accept it, and returns the
calls that follow:

```sh
tomaccoctl call project.create '{"path":"/absolute/MyApp","kind":"xtool","name":"MyApp","organization":"dev.example","build":true}'
tomaccoctl call project.app '{"path":"/absolute/MyApp"}'
```

**On a phone that is already running, one call installs and opens the app without a reboot:**

```sh
tomaccoctl call ios.virtual.deploy '{"bundle":"/absolute/MyApp.app"}'
tomaccoctl call job.logs '{"job_id":"DEPLOY_JOB","offset":0}'
```

It signs a copy, exchanges spare trust-cache entries of the boot for its hashes while the guest is briefly stopped, sends the bundle over
the guest channel, names it to the signing adapter, installs it through installd and opens it (about 25 s). The profile lists the app
afterwards, so the next start carries it too. When this run already installed the same signed app (the same cdhash after signing, which
covers every file of the bundle), the deploy log has `unchanged` after `signed`, nothing is sent and the app is only opened; `force:true`
installs it again. This skip has unit tests only; it was not run on a live phone.

**From an xtool project in one job.** `project.run` with `device:"virtual-iphone"` builds with `xtool dev build`, deploys the one
`xtool/*.app` with `ios.virtual.deploy` (which opens it) and, after `crash_check_seconds` (default 10, `0` skips it), reads the app's
crash reports. A report that was not there before the deploy makes the job fail as `crashed`. It is refused before any job with
`ios_virtual_not_running` when the phone is off, and with `action_unsupported` for a project that is not an xtool project.

```sh
tomaccoctl call project.run '{"path":"/absolute/MyApp","platform":"ios","device":"virtual-iphone"}'
tomaccoctl call job.wait '{"job_id":"RUN_JOB","seconds":60}'
```

The job writes `build/tomacco/virtual_iphone_run.json` in the project and lists it in its artifacts: `state` (`launched`, `crashed`,
`failed`, or `building` when the build failed), `build_seconds`, `deploy` with its `job_id`, `seconds`, `unchanged` and `steps`, the
opened app's `pid`, and `crash_check` with the new reports as `testing.crashes` events. No crash report never proves the app did not
crash. The last log line says the same in one line. Unit-tested with recorded replies; not run on a live phone. `ios.virtual.configure` with `add_application` and `deploy` does the add and the deploy in one
call. A boot has 512 spare entries and the adapter's per-boot table holds 32 apps, frameworks and extensions. To debug the app, build it
with `get-task-allow` and run `ios.virtual.debug {target}` — but iOS refuses it with Developer Mode off and arming it did not hold across a boot, so
lldb cannot attach yet.

Otherwise, usually `add_application`, `application` and `ios.virtual.start` are enough, because the start builds what it needs. The sequence
with the optional build by hand:

```sh
tomaccoctl call ios.virtual.configure '{"add_application":"/absolute/MyApp.app"}'
tomaccoctl call ios.emulator.build_guest_tools '{"trustcache":"/absolute/system.tc","output":"/absolute/guest-tools-new","linker":"/absolute/aarch64-apple-darwin-ld","ldid":"/absolute/ldid","clang":"/absolute/clang-22.1.8/clang","minimum_ios":"27.0","agent":true,"application_probe":true,"development_signing":true,"quiet_services":true,"applications":[{"bundle":"/absolute/MyApp.app"}]}'
tomaccoctl call job.wait '{"job_id":"BUILD_JOB","seconds":60}'
tomaccoctl call ios.emulator.report '{"job_id":"BUILD_JOB"}'
tomaccoctl call ios.virtual.configure '{"application":"dev.example.MyApp"}'
tomaccoctl call ios.virtual.start '{}'
tomaccoctl call job.logs '{"job_id":"START_JOB","offset":0}'
tomaccoctl call ios.virtual.status '{}'
tomaccoctl call ios.emulator.guest '{"job_id":"GUEST_JOB","operation":"logs","argument":"development"}'
```

1. `ios.virtual.configure {add_application}` inspects the bundle and adds it to the profile. `add_entitlements` names a plist, and
   `remove_application` takes the path or the bundle identifier.
2. Optional, because the start does this by itself: `ios.emulator.build_guest_tools` with `applications` (requires `development_signing`,
   and with it `agent` and `application_probe`) signs each bundle ad hoc, adds its hash to the trust cache and its identity to the
   adapter's table. Repeat `job.wait` until terminal, then read `ios.emulator.report`. Name the finished build as `tools` in `profile.json`:
   the start installs the build the profile names.
3. `ios.virtual.configure {application}` chooses the app the start opens, by bundle identifier. Without it the built-in diagnostic app opens.
4. `ios.virtual.start` inspects each bundle again. When the profile's build does not carry one with the same content and entitlements, it
   builds a set that does, before any disk copy, into the profile's `guest-tools` directory, and reuses a set built from the same inputs. A
   kernel that does not pin the new adapter is replaced by a derived one, and `profile.json` names both, with the previous file kept beside
   it. The job log has `guest-tools`, `kernel` and `profile` lines. Clang 22, the linker and `ldid` come from `profile.json` `toolchain`, the
   settings' `executables`, or the build's record; a missing one fails the start and says where it looked. After the guest service answers,
   each app whose hash the base does not record is installed: phase `installing`, one `application` event per bundle, `installed` or
   `installed-before`. An install is never sent twice. installd's refusal ends the start with its reason, and the guest keeps running for
   diagnosis.
5. Read the outcome from `ios.virtual.status`: `phase`, `error`, `application`, `application_launch_accepted`, `application_process`,
   `display_visible` and `installed_applications`. A verified stop carries that record into the base, so a later start installs only an app
   whose signed executable changed.
6. `ios.emulator.guest logs development` shows the adapter's `active` line with `entries`, and an `entry` line per further identifier. This
   tells an inactive signing library from a refused bundle. `crash_reports <executable name>` reads the app's own reports.

Embedded frameworks and libraries have been installed and loaded on the virtual device. App extensions are verified offline only:
`ios.emulator.guest` `lookup` with the app's bundle identifier answers `plugins`, the extensions iOS registered. SwiftUI built by Tomacco's own
route is not supported here.

## Display sessions

`runtime.start`, `runtime.list`, `runtime.state`, `runtime.audio`, `runtime.input`, `runtime.capture`, `runtime.hierarchy` and `runtime.stop` work
on a live device, and `runtime.start` connects Android or a physical iPhone directly on Linux. Passing `remote` selects a Simulator on a configured Mac.
There is one session per device. Repeated calls return it with `reused:true`, and different encoder or runner settings answer
`runtime_configuration_conflict`.

Start with `workspace.context` for the selected project and `runtime.list` for what is connected. `desktop.open` with `tab:"runtime"` lets the
user watch the same app you are controlling.
When the desktop on the session is not the installed version, for example the one that ran while an update was installed,
`desktop.open` asks it to quit, starts the installed one and reports the old version as `replaced_version` (null for a desktop from
an earlier release, which does not say). A desktop with unsaved files stays, with `desktop_unsaved`; one that does not quit within 10 seconds
gives `desktop_stale`.

**Input.** `runtime.input` coordinates run from 0 to 1. Android supports `tap`, `touch` (down, move, up, cancel), `swipe`, `key`, `text` and `rotate`.
The Simulator also supports `fill`, `pinch`, `shake` and `hinge` when its companion does. Apple `text` sends ASCII keys, and `fill` replaces an
accessible field at x/y with any Unicode. Apple rotation needs an explicit orientation. Pass the current decoded image size for mapping after a rotation.

**Evidence.** `runtime.capture` and `runtime.hierarchy` give real app and OS evidence. `device.capture` waits for a screenshot, and
`device.screenshot` is the asynchronous form.

**Sockets.** Most agents should use the operations; the desktop uses these to avoid a process per pointer move.

- `video_socket` emits repeated **u32 big-endian length + payload**. `codec` selects `h264` (Annex-B) or `mjpeg` (one JPEG per payload). It can have
  several consumers, and a slow one is disconnected rather than growing memory.
- `control_socket` takes one JSON line per connection and is owner-only:

  ```json
  {"operation":"input","arguments":{"session":"SESSION_ID","action":"tap","x":0.5,"y":0.5}}
  ```

**Stopping.** `runtime.stop` is idempotent. It releases the session's processes, sockets and forwarding, and leaves the app and device running.

## Windows

`desktop.open` with `device_only:true`, then `preview.configure` with `native:{device, connect:true, detached:true}`, shows a device as its own window.
`preview.state` reports the desktop's mode, native runtime and decoded-frame metadata.

## Files

`files.list`, `files.read`, `files.search`, `files.write` and `files.mkdir` stay within an explicit project root and never execute source text. When
overwriting, pass the digest from `files.read` as `if_match`; a conflict leaves the user's edit intact. The desktop's editor uses this same contract.

## Debugging

`debug.start` starts a durable session: JDWP for Android and Java, Android NDK LLDB, or local LLDB. Then use `debug.breakpoints`, `debug.state`,
`debug.events`, `debug.stack`, `debug.variables`, `debug.evaluate` and `debug.control`, and stop it explicitly. The desktop's Debugger view can take
over the same session.

JDWP evaluation supports field paths, not arbitrary Java. LLDB capabilities depend on the adapter. A Linux Swift debug run does not qualify iOS remote
debugging, and Swift expressions inside an Android process are not qualified.

For Android C/C++, configure `android_ndk` and `lldb-dap` through `settings.configure`, install a **debuggable** APK, then:

```sh
tomaccoctl call debug.start '{"backend":"android-lldb","device":"emulator-5554","bundle_id":"dev.example.app","program":"/work/app/native-symbols/libapp.so","launch":true}'
```

- `program` is the local **unstripped ELF** from the installed build, with the same ABI and build id.
- `symbol_paths` adds library directories, `source_map` relocates source paths, and `pid` picks a process.
- `launch:true` starts a missing app and attaches, and keeps a running one. It does not stop before early native initializers.
- It uses Android's `run-as` gate and needs neither `adb root` nor changed security settings.
- An identical request reuses the session. A different one on the device answers `device_debug_busy`, across JDWP and LLDB; mixed debugging is not
  supported. `debug.control` with `action:"disconnect"` releases it and keeps the app running.

Native debugging is qualified on API 36 x86_64. ARM64 is built but not run on a physical phone.

## UI tests and profiling

`testing.setup` installs a small instrumentation companion for Unicode, emoji and whole-field replacement, without changing the user's keyboard. `testing.run`
runs JSON scenarios on a bounded parallel device matrix, returning JUnit results and capturing failures; `examples/native-navigation.json` shows the format.
Besides the selector steps, a scenario can `tap` or `swipe` at pixel coordinates (`x`, `y`, `end_x`, `end_y`, `duration` in
milliseconds) and type `text` into the focused field.
With `platform:"ios"` the same scenarios run on USB iPhones through WebDriverAgent; see
[UI tests on an iPhone](iphone.md#ui-tests).

`testing.record` writes such a scenario from what you do. Between `start` and `stop`, the taps, swipes, text and keys sent to that
Android device through `android.input`, `runtime.input` (also from the desktop's live display) and `testing.action` become steps. Before
each tap it reads the UI hierarchy and names the element under the finger by resource ID, description or text, adding `index` when the
name is shared; without a name there it records a `tap` at pixel coordinates. Reading the hierarchy makes a tap take longer while
recording. A rotation or another input a scenario cannot express is listed under `skipped`. `status` counts the steps, `discard` throws
the recording away.

```sh
tomaccoctl call testing.record '{"device":"emulator-5554","action":"start","name":"Add a counter"}'
# tap and type in the desktop or with android.input / runtime.input
tomaccoctl call testing.record '{"device":"emulator-5554","action":"stop","path":"/absolute/add-counter.json"}'
```

Read the scenario before you keep it: coordinate steps depend on the screen size, and a recorded step waits only when it is a click
on a named element, which waits for that element.

`testing.lint` checks a scenario without acting on the device: its format, then each step against the screen now, or against a saved
`android.hierarchy` XML given as `hierarchy`. A finding names the step, a `severity` and a `code`: `not_found` (nothing on this screen
matches; a warning, since an earlier step may open the screen it belongs to), `ambiguous` (several elements match: an error for
`click`, `long_click` and `fill`, which act on one, with up to five `candidates` in `steps`), `disabled`, `present` (an element
expected absent is there), `off_screen` and `invalid_key`. `passed` is false when a finding is an error; a scenario that does not
parse answers `valid:false` with the error.

```sh
tomaccoctl call testing.lint '{"path":"/absolute/add-counter.json","device":"emulator-5554"}'
```

`path` can also be a directory: each `.json` file directly in it is a scenario, run in name order, and a malformed one is refused
before anything runs. By default every device runs every scenario. `shard:true` splits them instead: each scenario runs once, on
whichever device is free next, and the results are merged into one `report.json` (each result names its `scenario` and `device`) and
one `junit.xml` (`classname` is the scenario). Only the first `parallelism` devices take part, so raise it to use more.

```sh
tomaccoctl call testing.run '{"path":"/absolute/ui-tests","directory":"/absolute/ui-results","devices":["emulator-5554","emulator-5556"],"shard":true,"parallelism":2,"retries":1}'
```

`retries` runs a failed scenario again on that device, up to five times; a scenario's own `retries` field takes precedence. Each
device's result in `report.json` then lists its `attempts` (whether each passed, its error and the index of the step that failed), and
`flaky:true` marks a pass after a failure; the report's `flaky` lists each such `scenario` and `device`. In `junit.xml` the earlier failures are
`flakyFailure` elements of a passed test case, or `rerunFailure` elements of a failed one, as Maven Surefire writes them. A retry hides
nothing: read the attempts of a flaky scenario.

`testing.results` reads JUnit XML into one summary: `totals` (`tests`, `passed`, `failed`, `errors`, `skipped`, `time`), each suite with
its device for instrumented tests, and each failure with its class, name, message and stack. Given a project it finds Gradle's unit test
results (`build/test-results`) and instrumented test results (`build/outputs/androidTest-results`); a directory or a single file works too,
such as a `testing.run` report directory. Pass the test job's `job_id` (or `since`) so that results left from an earlier run are not
mistaken for this one's; files older than that are counted in `older_files_skipped`, and when nothing newer is left it answers
`no_test_results`. `report` writes the summary as JSON as well.

```sh
tomaccoctl call project.execute '{"path":"/absolute/MyApp","platform":"android","action":"test"}'
tomaccoctl call testing.results '{"path":"/absolute/MyApp","job_id":"JOB_ID","report":"/absolute/test-summary.json"}'
```

Flutter tests read the same way. `project.execute` with `"action":"test"` runs `flutter test` with its JSON reporter writing
`build/tomacco/flutter_test.json` beside the readable log, and `testing.results` on the project reads it: one suite per test file,
each failure with its test's line, and `golden_failures`, every failed golden-file comparison with flutter_test's message and the
paths of the `masterImage`, `testImage`, `isolatedDiff` and `maskedDiff` images it wrote. Any `flutter test --machine` or
`dart test --reporter json` output file works as `path` too. See [Android](android.md#flutter).

`testing.crashes` reads the device's crash, system and main logs and returns Java crashes (`FATAL EXCEPTION`), ANRs and native crashes
(`Fatal signal`) as `events`: `kind` (`crash`, `anr` or `native_crash`), `time`, `package`, `process`, `pid`, and for a crash the
`exception`, `message`, `stack` and `caused_by`, for an ANR the `reason`, for a native crash the `signal`. `bundle_id` keeps one app's
events. `since` is a time in seconds since 1970 on the device's clock; `job_id` takes the window of a job, such as a `project.run`, on the
computer's clock, which an emulator shares. Without either it reads the last `lines` log lines.

```sh
tomaccoctl call testing.crashes '{"device":"emulator-5554","bundle_id":"dev.example.newapp","job_id":"JOB_ID"}'
```

`testing.run` does the same for each device after its scenario: the device's result in `report.json` lists `crashes`, and a crash or ANR
of an app the scenario launches, stops or clears fails it with `app_crashed`.

`testing.visual_diff` compares a screenshot with an approved baseline PNG, pixel by pixel. It answers `changed_fraction` of the compared pixels,
`changed_bounds` and `passed` (`changed_fraction` at most `threshold`), and with `diff` writes the baseline faded with changed pixels in red.
`tolerance` ignores small colour differences per channel, and `ignore` leaves out regions such as the status bar clock. A missing baseline
answers `baseline_missing`; look at the image, and only if it is right call again with `update_baseline:true` to keep it. Images of different
sizes fail with `size_mismatch:true`.

```sh
tomaccoctl call testing.visual_diff '{"actual":"/absolute/screen.png","baseline":"/absolute/baselines/home.png","diff":"/absolute/home-diff.png","threshold":0.001,"tolerance":8,"ignore":[{"x":0,"y":0,"width":1080,"height":64}]}'
```

`testing.accessibility` audits the Android screen's UI hierarchy, from a `device` or a saved `hierarchy` file with its `density`. Each finding
has a `rule`, `severity`, `message`, the element's class, id, text and description, its `bounds` in pixels and its size in dp:

- `missing_label` (error): a clickable, long-clickable or checkable element with no text or content description, itself or inside it.
- `small_target` (warning): an enabled touch target narrower or lower than `min_target_dp`, 48 by default.
- `small_text` (warning): a text element lower than `min_text_height_dp`, 14 by default. The hierarchy has no font sizes, so this is an
  estimate from the element's height, which includes padding: it finds tiny text, not all small text.

An element cut off at the edge of a scrolling list is not judged by size. `bundle_id` keeps only the app's elements, without the status bar. These are heuristics, not a certificate: contrast, font sizes and focus
order are not checked.

```sh
tomaccoctl call testing.accessibility '{"device":"emulator-5554","bundle_id":"dev.example.newapp"}'
```

### UI tests on the virtual iPhone

Pass `virtual-iphone` as the device to `testing.run`, `testing.record` and `testing.crashes` while the virtual iPhone runs. The
guest tools report no UI hierarchy, so nothing can find an element by its label there: `testing.find`, `testing.action`,
`testing.lint`, `testing.accessibility` and the selector steps (`wait`, `assert`, `click`, `long_click`, `fill`) answer
`ui_hierarchy_unavailable`, whose `details.testing` lists what works.

- **Steps.** `tap` and `swipe` take pixels of the screenshot (`ios.virtual.screenshot` with `frame:"none"`) and go through
  `ios.emulator.control` as `click` and `drag`; a contact lasts at most 2 seconds, and a swipe that stays put is a long press.
  `text` types on the USB keyboard into the focused field. `key` takes a QEMU key name such as `ret`, `home` for the Home button,
  or an Android key code that has a counterpart (`KEYCODE_ENTER`, `KEYCODE_DEL`, `KEYCODE_HOME`, the arrows). `launch` opens an app
  through the guest service's `open`; `stop` and `clear` are not available. `launch` returns once iOS accepted the request,
  and the app shows a few seconds later (about 7 s on the development host), so follow it with a `wait_screen` before
  tapping.
- **Screens instead of elements.** On both platforms, `screenshot` keeps a PNG in the report directory, `pause` waits `duration`
  milliseconds, `assert_screen` compares the screen with a `baseline` PNG as `testing.visual_diff` does (`threshold`, `tolerance`) and
  fails with `screen_mismatch`, and `wait_screen` takes screenshots until one matches or `timeout` passes (`screen_timeout`). A failure
  writes the difference image beside the screenshot. Make the baseline with `ios.virtual.screenshot`, or with
  `testing.visual_diff` and `update_baseline:true` after you have looked at it. `ignore` leaves out regions in pixels as in
  `testing.visual_diff`: on the phone, the status bar (the top 170 pixels of a 1206×2622 screen) and the Clock app's icon
  change with the time.
- **Crashes.** `testing.crashes` needs `process`, the app's executable name or another report name prefix, and reads the guest's crash
  reports through its `crash_reports` operation. Each report with an exception becomes an event like Android's: `kind` `crash`, `time`
  (the report's capture time), `package` (the bundle identifier), `process`, `pid`, `thread`, `exception` (such as `EXC_CRASH`),
  `signal`, `message` (the abort message, the exception's subtype or the termination), `termination` and `stack`, the crashed thread's
  frames. Reports that give no event are listed under `unparsed` with the reason. The guest returns at most three reports per directory,
  so `collection_complete:false` means some were left out, and an empty list never proves that nothing crashed. `testing.run` does not
  read crash reports after an iPhone scenario; its result says so in `crashes_error`.
- **Recording.** `testing.record` with `virtual-iphone` notes the screen's size at `start`, then turns the `ios.emulator.control`
  input sent to the running phone into steps: `click` becomes a `tap`, `drag` a `swipe`, a streamed touch from `touch_down` to
  `touch_up` (the desktop's phone window sends these) a tap, a long press or a swipe, and `text`, `key` and `home` their steps. A
  key combination and the side button are listed under `skipped`. Every tap is at coordinates. A gap of a second or more
  between two inputs becomes a `pause` step of that length, up to 10 s, so a replay waits where the person waited.

```sh
tomaccoctl call testing.record '{"device":"virtual-iphone","action":"start","name":"Open Settings"}'
# tap and type in the desktop's phone window, or with ios.emulator.control
tomaccoctl call testing.record '{"device":"virtual-iphone","action":"stop","path":"/absolute/open-settings.json"}'
tomaccoctl call testing.run '{"path":"/absolute/open-settings.json","directory":"/absolute/iphone-results","devices":["virtual-iphone"]}'
tomaccoctl call testing.crashes '{"device":"virtual-iphone","process":"MyApp","job_id":"JOB_ID"}'
```

What ran on a live virtual iPhone (iPhone 18 Pro, iOS 27): a scenario that presses Home, launches an app, waits for its screen,
taps a button and asserts the result against baselines taken live passed in 10 s, with `threshold` 0 and the status bar
ignored (changed fraction 0.0); the same assertion against the screen before the tap failed with `screen_mismatch` (0.41 % of
the pixels changed) and wrote its difference image. `testing.record` turned a tap on an icon, a 9 s wait and Home into `tap`,
`pause` and `key`, and the replay with a `wait_screen` for the Home screen passed. `testing.crashes` read the phone's three
fault reports and listed them as `not-a-crash`; no app had crashed, so no crash event was seen live. On Android the screen
steps are unit-tested only.

What works where, kept equal to the code by a unit test (`capabilities` returns the same as `testing`).

<!-- testing-matrix:begin -->
| Feature | Android | Virtual iPhone | iPhone (USB) |
| --- | --- | --- | --- |
| `testing.find`, `testing.action` (selectors) | Yes: UI hierarchy | No: the guest reports no UI hierarchy | No |
| `testing.run`: `tap`, `swipe`, `text`, `key` | Yes | Yes: through `ios.emulator.control` | No |
| `testing.run`: `wait`, `assert`, `click`, `long_click`, `fill` | Yes | No: no UI hierarchy | No |
| `testing.run`: `screenshot`, `assert_screen`, `wait_screen`, `pause` | Yes | Yes: from `ios.virtual.screenshot` | No |
| `testing.run`: `launch` | Yes | Yes: the guest's `open` | No |
| `testing.run`: `stop`, `clear` | Yes | No | No |
| Crash check after a `testing.run` scenario | Yes | No: call `testing.crashes` | No |
| `testing.visual_diff` | Yes | Yes: PNG from `ios.virtual.screenshot` | Yes: PNG from `device.capture` |
| `testing.crashes` | Yes: logcat | Yes: crash reports, by `process` | No: use `ios.crash_reports` |
| `testing.record` | Yes: taps named by element | Yes: taps at coordinates | No |
| `testing.lint` | Yes | No: no UI hierarchy | No |
| `testing.accessibility` | Yes | No: no UI hierarchy | No |
| `testing.setup` (Unicode text) | Yes | No: `text` types US or German keys | No |
<!-- testing-matrix:end -->

What has been run: `testing.visual_diff`, `testing.accessibility` (on the Settings app), `testing.crashes` (with a crash from
`am crash`) and the crash list of `testing.run` on an API 36 x86_64 emulator, and `testing.results` on a `testing.run` report. ANRs,
native crashes and Gradle's own result files have been read only from sample text in the unit tests.
On an API 36 x86_64 emulator with the Settings app: `testing.record` with `android.input`, `testing.action` and `runtime.input`
(touch, key, swipe and rotation sent through a display session from the command line, not by hand in the desktop), the recorded
scenario passing `testing.lint` and `testing.run`, and a directory of two scenarios run with `shard` and `retries`. A pass after a
failure (`flaky`) has been seen only with a stand-in adb in the tests.

`profile.capture` records Android Perfetto traces. Configure the official `trace_processor`, then use `profile.analyze` and `profile.compare`. Metrics that are
not available are null, not zero. A count of frames with jank tags is not the same as visible drops.

### App performance

These operations read Android's own counters, without Perfetto. Each returns a report with flat `metrics`; give `report` an
absolute path to save it as JSON.

`profile.startup` launches an app `runs` times (5 by default) with `am start -W` and reports TotalTime and WaitTime in
milliseconds: every run, and their count, min, max, mean, median and p90 (nearest rank). `mode` `cold` stops the app before each
launch, `warm` keeps the process and recreates the activity (`--activity-clear-task`), and `hot` brings the running activity back
from the home screen; warm and hot start the app once first, unmeasured. Each run names the launch state Android reported, and
`mismatched_runs` counts the runs whose state differs from `mode`. The metrics are `startup_<mode>_median_ms`, `_p90_ms`, `_min_ms`,
`_max_ms` and `_mean_ms`. `activity` picks another activity than the launcher's.

```sh
tomaccoctl call profile.startup '{"device":"emulator-5554","bundle_id":"com.android.settings","mode":"cold","runs":5}'
```

On an API 36 x86_64 emulator, five runs each of the Settings app reported COLD, WARM and HOT for every run of the matching mode.

`profile.frames` resets a running app's frame statistics (`dumpsys gfxinfo reset`), waits `seconds` (5 by default) or runs a
`testing.run` scenario given as `scenario`, and reads them again. The metrics are `frames_total`, `janky_frames`, `jank_percent`,
`slow_frames` (17 ms or more) and `frozen_frames` (700 ms or more), both counted from the frame time histogram, and
`frame_p50_ms`, `frame_p90_ms`, `frame_p95_ms` and `frame_p99_ms` as dumpsys gives them for the app's main process. `processes`
keeps each process's counters, such as `missed_vsync` and `slow_ui_thread`. An app that drew nothing in that time has null metrics,
not zero; an app that is not running answers `app_not_running`, and a failed scenario `scenario_failed` with its steps.

```sh
tomaccoctl call profile.frames '{"device":"emulator-5554","bundle_id":"com.android.settings","scenario":"/absolute/scroll.json"}'
```

On the same emulator, a scenario of three swipes in Settings gave 94 frames, 2 of them janky.

`profile.memory` reads a running app's `dumpsys meminfo`. Each process in `processes` has the App Summary in KB (PSS and RSS of the
Java heap, native heap, code, stack, graphics and the rest), `total_pss`, `total_rss` and `total_swap`, the size, allocated and free
parts of the native and Dalvik heaps, and object counts such as `activities` and `views`. The metrics add the processes up in MB:
`memory_pss_mb`, `memory_rss_mb`, `java_heap_pss_mb`, `native_heap_pss_mb`, `code_pss_mb`, `stack_pss_mb` and `graphics_pss_mb`,
and the `activities` and `views` counts. With `heap_dump`, it also writes an HPROF heap dump of the main process there, through a
file on the device that is removed afterwards. That needs a debuggable app or a debuggable system image, otherwise it answers
`heap_dump_failed`; Android Studio opens the file, other tools after `hprof-conv` from the platform tools.

```sh
tomaccoctl call profile.memory '{"device":"emulator-5554","bundle_id":"com.android.settings","heap_dump":"/absolute/settings.hprof"}'
```

On the same emulator, Settings used 54 MB PSS, and its heap dump was a 47 MB HPROF file.

`profile.usage` measures a running app over `seconds` (10 by default). CPU is the user and system time of the app's processes in
`/proc` before and after: `cpu_ms`, `cpu_percent` of one core (above 100 when it used more than one) and `cpu_device_percent` of all
cores, also per process in `processes`. Battery is the change of the app's UID in batterystats' estimated power use, in mAh, in total
(`power_mah`) and per part such as `cpu` or `sensors`. That estimate comes from the device's power profile, which on an emulator is not
a real phone's; `battery` also says which UID it is (system apps share one), whether a charger is connected, and the level. Nothing on
the device is reset.

```sh
tomaccoctl call profile.usage '{"device":"emulator-5554","bundle_id":"com.android.settings","seconds":5}'
```

On the same emulator, Settings scrolled for five seconds used 1790 ms of CPU, 35 % of one core.

**Budgets.** A budget gives metrics a `maximum`, a `minimum` or both:

```json
{"startup_cold_median_ms": {"maximum": 800}, "jank_percent": {"maximum": 5}, "memory_pss_mb": {"maximum": 200}}
```

`profile.budget` checks saved reports of one app against it, given as a file (`budget`) or inline (`budgets`). Each result has the
metric, its value and limits, `passed`, and the report it came from; a metric no report measured fails as `measurement_unavailable`.

```sh
tomaccoctl call profile.budget '{"reports":["/absolute/startup.json","/absolute/memory.json"],"budget":"/absolute/budget.json"}'
```

In a `testing.run` scenario, a `measure` step records metrics of the app named by `bundle_id`: `measure` is `startup` (with `mode`,
`runs` and `activity`), `frames_reset` and later `frames`, `memory`, or `usage` (with `seconds`). A step's own `budget` is checked
against that step's metrics, and a scenario's `budget`, or the run's `budget` file or inline `budgets`, against all the metrics of the
scenario. A metric outside fails the scenario as `budget_exceeded`; each result in `report.json` carries its `metrics` and `budget`.

```json
{"name": "Settings performance",
 "budget": {"startup_cold_median_ms": {"maximum": 2000}, "jank_percent": {"maximum": 20}},
 "steps": [
  {"action": "measure", "measure": "startup", "bundle_id": "com.android.settings", "runs": 3},
  {"action": "measure", "measure": "frames_reset", "bundle_id": "com.android.settings"},
  {"action": "swipe", "x": 500, "y": 1800, "end_x": 500, "end_y": 400},
  {"action": "measure", "measure": "frames", "bundle_id": "com.android.settings"}]}
```

On the same emulator, a `testing.run` of such a scenario with a memory step passed its budget, and `profile.budget` failed a saved
startup report of 303 ms against a maximum of 300.

**Baselines.** Save a report as the baseline, measure again later, and `profile.compare` lists under `changes` every metric of either
report with its `baseline` and `current` value, `delta` and `delta_percent`. Both reports must be of the same app and kind. `budgets`
are optional here: a `maximum` or an `increase_percent` over the baseline fails the comparison, and without budgets `passed` stays true.

```sh
tomaccoctl call profile.compare '{"baseline":"/absolute/startup-baseline.json","current":"/absolute/startup.json","budgets":{"startup_cold_median_ms":{"increase_percent":25}}}'
```

On the same emulator, two cold startup reports of three runs each differed by 226 ms in their median (303 and 529 ms), which failed an
`increase_percent` of 25: an emulator's launch times vary that much between runs, so compare medians of more runs on a quiet host.

## Release

`release.keystore`, `release.sign`, `release.verify` and `release.version` handle Android artifacts. Passwords come from owner-only files, never a string in a call.

`release.version` sets one version name and build number in every version file it finds in the project: `pubspec.yaml`,
Expo's `app.json` (`expo.version`, `expo.android.versionCode`, `expo.ios.buildNumber`), the `package.json` of a React
Native or Expo app, `app/build.gradle(.kts)` or `android/app/build.gradle(.kts)`, and the `Info.plist` that `infoPath` in
`xtool.yml` names. A Gradle `versionCode = libs.versions.appCode.get().toInt()` changes that entry of
`gradle/libs.versions.toml`, and `providers.gradleProperty("appVersion")` that key of `gradle.properties`; a Flutter
module's `flutter.versionCode` follows `pubspec.yaml` and is listed under `skipped`. Any other computed value is refused
with `version_ambiguous` before a file changes. Instead of `version` and `build_number`, `bump` (`major`, `minor`, `patch`
or `build`) raises the current version and sets the build number to the highest current one plus 1. `dry_run:true` returns
the same `changes` (file, field, from, to) without writing; otherwise each changed file gets a backup, listed in `files`.
JSON files are written back with two-space indentation.

```sh
tomaccoctl call release.version '{"path":"/absolute/app","bump":"patch","dry_run":true}'
```

`release.notes` turns one section of a Markdown changelog into Google Play's "What's new" text for each language and checks
it against Play's limit of 500 characters. `section` names the heading (`2.0.0` matches `## [2.0.0] - 2026-11-01`; empty takes
the first section), `changelog` is the file for `language` (default `en-US`), and `translations` adds other languages' changelogs
with the same section. Bullets become `•`, bold, code and link markup is removed. A text over the limit is a `too_long` error
and no file is written for any language; `truncate:true` drops whole lines from its end instead and reports `truncated`. The
files go under `directory` in the `layout` the upload tool reads: `fastlane` (`LANGUAGE/changelogs/default.txt`, or
`BUILD.txt` with `build_number`), `play-publisher` (`release-notes/LANGUAGE/default.txt`) or `whatsnew` (`whatsnew-LANGUAGE`).
The result's `release_notes` is the `release_notes` argument of `play.prepare`, which stages them with the bundle.

```sh
tomaccoctl call release.notes '{"changelog":"/absolute/app/CHANGELOG.md","section":"2.0.0","directory":"/absolute/app/fastlane/metadata/android","translations":[{"language":"de-DE","changelog":"/absolute/app/CHANGELOG.de.md"}]}'
```

`release.signing_health` reads a keystore with `keytool -list -v` and reports each key's owner, validity (`valid_until`,
`days_left`), key algorithm and size, signature algorithm and SHA-256 fingerprint. Findings: `expired` (an error),
`expires_soon` within `warn_days` (default 365), `play_minimum_validity` for a key that ends before 22 October 2033, which
Google Play requires of app signing keys, `weak_key` under 2048 bits (an error), `dsa_key` and `weak_signature` (MD5 or
SHA-1). Pass the upload and app signing certificate fingerprints from Play Console's app integrity page as
`upload_certificate_sha256` and `app_signing_certificate_sha256`: each key gets a `play_role` (`upload`, `app_signing` or
`none`), no match is the error `not_play_key`, and Play's app signing key in the same keystore as a different upload key is
the warning `app_signing_key_local`. `artifact` checks that an APK or AAB is signed with one of the keys. Nothing is sent
to Play; `passed` is false when a finding is an error.

```sh
tomaccoctl call release.signing_health '{"keystore":"/absolute/upload.p12","password_file":"/absolute/upload.password","upload_certificate_sha256":"AB:CD:..."}'
```

`release.checklist` checks an Android app's release build before an upload, from its build files and manifest without
running Gradle. Each entry of `checks` has a `status` (`pass`, `fail` or `unknown`), a `severity` and its `evidence`:
`target_sdk` against the level Google Play requires (`required_target_sdk`, by default 36, Play's requirement for new apps
and updates since 31 August 2026), `version_code` above `previous_version_code` or a `baseline` package's, `debuggable` off in
the release build type and the manifest, `minify` on (a warning), `signing_config` set and not the debug key, and `cleartext`:
`usesCleartextTraffic` or a network security config that permits cleartext HTTP fails unless `allow_cleartext`. `artifact`
reads targetSdk and versionCode from the built APK or AAB, for builds that compute them. `passed` is false when an
error-severity check fails.

```sh
tomaccoctl call release.checklist '{"path":"/absolute/app","previous_version_code":41}'
```

`play.prepare` creates an edit, uploads the AAB, sets the track and validates, without committing. Inspect that before an explicitly authorized `play.commit`;
`play.abort` discards it. Live store submission has not been qualified, on Play or on the App Store.

## Updates

`update.check` reports `current`, `latest`, `notes_url`, `update_available` and a `state`: `up_to_date`, `update_available`, `no_release`, `no_package`, `offline`, `error` or `not_checked`.
Without arguments it follows `updates.mode`, using the network at most once a day and never when off. Pass `manual` only when the user asked for a check.
It asks for GitHub's latest release, or, with `updates.channel` set to `prerelease`, for the newest release including pre-releases; `channel` in the reply says which. A check from the other channel
counts as `not_checked`.

```sh
tomaccoctl call update.status '{}'
tomaccoctl call update.check '{"manual":true}'
tomaccoctl call update.apply '{}'
tomaccoctl call update.rollback '{}'
tomaccoctl call settings.configure '{"updates":{"mode":"off"}}'
```

`update.apply` starts a durable job (download, signature check, install, health check, automatic rollback on failure). It is refused, with the reason in the error, while a job, a device display
session or the virtual iOS device runs, for a source checkout, a system package or an unwritable installation, without Python, and with `updates.mode` off unless `manual` says the user asked.
`update.rollback` restores the version before the last update. `settings.configure` takes `updates.mode` (`off`, `notify`, `auto`), `updates.channel` (`stable`, the default, or `prerelease`) and `updates.token_file`, an owner-only file. `TOMACCO_UPDATES=off`
turns all of it off. After an update, restart `tomaccoctl mcp` to use the new version; the running server keeps working.
From a terminal, `tomaccoctl update` runs the manual check and, when a release applies, `update.apply` and its job to the end.
`tomaccoctl verify FILE [--json]` checks a downloaded release file against its `.sha256` and its `.sig` with the same release keys, without installing anything.

## Web layouts

The layout lab is a separate Chromium surface, not a device. Start a session with `tomacco --url URL --preview-session NAME`, and discover it with `preview.sessions`.

- `preview.configure` selects a profile, orientation, fold, chrome, safe-area metadata, guides and zoom.
- `preview.configure` also sets what the page sees for `prefers-color-scheme` (`color_scheme`: `system`, `light`,
  `dark`) and `prefers-reduced-motion` (`reduced_motion`: `system`, `reduce`, `no-preference`), and throttles or cuts
  its network: `network:{preset:"slow-3g"}`, `{preset:"offline"}`, or `latency_ms`, `download_kbps` and `upload_kbps`;
  `{preset:"none"}` turns throttling off. The settings belong to the session, last across reloads and are reported by
  `preview.state`. They go through Chromium's DevTools protocol, so they cover the page's requests but not a service
  worker's, and not WebSocket traffic.
- `preview.ready` waits for the document, fonts and Flutter's first frame. A `wait_for` expression can wait for your own readiness.
- `preview.inspect`, `preview.click`, `preview.fill`, `preview.evaluate`, `preview.console` and `preview.capture` interact with the page.
- `preview.errors` reports the current page load as structured results: `console_errors`, `exceptions` (uncaught
  errors and unhandled rejections, with stack), `failed_requests` (fetch, XMLHttpRequest, elements, style sheet
  resources, fonts and the document; status 0 is a network error) and `clean`. `reload:true` reloads and waits
  first, so the report covers one load. `preview.console` keeps the raw console. Requests from workers and frames
  are not recorded.
- `preview.matrix` captures several profiles and orientations and restores the session. Besides one PNG per
  capture and `matrix.json`, it writes `contact-sheet.png`: every capture side by side at one scale, labelled with
  the model and its orientation. `contact_sheet:false` leaves it out.
- `preview.audit` reports overflow, missing names and small targets. It is not an accessibility certificate or Safari emulation.
  Each finding has a `rule` and a `selector` that matches that element alone. `document-overflow` is a page wider than
  the viewport and `horizontal-overflow` an element that sticks out; `safe-area-text` is text drawn where the profile's
  safe area ends (camera cutout, system bars), with its `edges`, while safe-area metadata is on;
  `target-size-platform` is a control under 44 points on iOS and generic profiles or 48 dp on Android, with links in
  running text exempt; `target-size-24` is WCAG 2.2's 24-pixel minimum.
- `catalog.export`, `catalog.import` and `catalog.remove` manage custom profiles, which are data only. `android.calibrate` reads a device's real size and density.

The [desktop guide](desktop.md#visual-layout-lab) has the details.

## Research operations

These lower-level `ios.emulator.*` operations are for research into the guest and the boot chain, not for everyday use. `tomaccoctl schema` has their arguments.
They share an admission lock: identical active requests reuse their job, and conflicting ones answer `ios_emulator_busy`. Do not retry with another state
directory to get around that. A successful process is never a qualification of an app runtime; read the report's own flags.

| Operations | What they are for | Details |
| --- | --- | --- |
| `prepare_userland`, `build_gpu_plugin`, `prepare_restore`, `restore`, `usb_bridge`, `prepare_cryptexes`, `build_guest_tools`, `prepare_guest_tools`, `paravirtual` | The steps of the virtual device's [setup chain](virtual-ios/setup.md#the-chain) | [How it works](virtual-ios/how-it-works.md) |
| `control`, `guest`, `guest_result`, `report` | Input, frames and the guest channel on a running guest, and a job's receipt | [Usage](virtual-ios/usage.md) |
| `inspect`, `probe`, `exec` | The separate restore-shell engine: run own programs in a real iOS restore environment | [Research engines](research-engines.md) |
| `prepare_research`, `research` | Full-system research images on that engine | [Managed runs](research-engines.md#managed-full-system-runs) |
| `prepare_pmp`, `inspect_pmp`, `pmp`, `memory` | Run and inspect PMP controller firmware in an isolated machine | [PMP workflow](research-engines.md#isolated-pmp-firmware-execution) |

`tools/build_ios_native_example.py` builds C and Embedded Swift command-line examples for that engine. Use `desktop.open` with `tab:"ios-research"` to show
the user its dialog.
