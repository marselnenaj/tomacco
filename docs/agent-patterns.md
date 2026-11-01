# Patterns for agent authors

How to write an agent, a prompt or a script that drives Tomacco well. The [agent guide](agents.md)
is the contract; this page is about using it: plan before acting, wait for jobs properly, act on
errors, ask before destructive calls and recover from failures. The JSON shown is real output of
`tomaccoctl` 1.1.0, shortened where marked with `...` and with local paths replaced.

Over MCP the operation name is the tool name and the arguments are the same JSON object. The
[operation reference](reference/README.md) lists every operation with its arguments and
annotations.

## Plan with `capabilities`

Start a session with one call that says what this computer can do right now, instead of trying an
operation and reading its failure:

```sh
tomaccoctl call capabilities '{}'
```

On a computer with no Android tools configured it answered (iOS part shortened):

```json
{"ok":true,"result":{"android":{"build":{"ready":false,"missing":["java","android_sdk"],"gradle":false},
"emulator":{"ready":false,"missing":["emulator","adb"],"avds":[...],"kvm":true},
"devices":{"ready":false,"missing":["adb"],"connected":[]}},
"ios":{"virtual_iphone":{"ready":false,"missing":["profile"],"host_supported":true,"configured":false}, ...},
"desktop":{"ready":true},"next":"doctor has next_steps for what is missing"}}
```

- Branch on `ready`. When it is false, `missing` names what is lacking; do not start a build that
  needs it.
- `tomaccoctl doctor` (the `doctor` tool) has `next_steps`, in order, for setting up what is
  missing. Steps marked `ask_first` download a lot or accept a license: ask the user before each.
- `capabilities` runs the tool checks, so it takes a few seconds. Call it once at the start and
  again after setup, not before every step.
- For a project, `project.inspect` says what kind it is and `project.plan` prints the commands a
  build would run, without running them. Neither runs the project's build scripts.

## Wait on jobs

A call that starts long work answers at once with a job. Its id is `result.id`, and **queued or
running is not success**. Three ways to follow it:

**From a script or a terminal**, `--wait` follows the job to its end, with its log on standard
error, and prints the final job:

```sh
tomaccoctl call project.execute '{"path":"/absolute/MyApp","platform":"android","action":"build"}' --wait
```

The exit status is 0 only when the job succeeded.

**Over MCP or step by step**, repeat `job.wait` until the state is `succeeded`, `failed`,
`cancelled` or `timed_out`. Each call waits at most 60 seconds and answers a running job as a
valid result; that is not an error and not a reason to start the work again.

```sh
tomaccoctl call job.wait '{"job_id":"JOB_ID","seconds":60}'
```

**For progress**, an MCP client that sends `_meta.progressToken` with `job.wait` gets
`notifications/progress` once a second with the job's state and latest log line. After
`logging/setLevel` `info` it also gets each new log line as `notifications/message`. Without MCP,
read the log in pieces and pass `next_offset` back as `offset`:

```sh
tomaccoctl call job.logs '{"job_id":"JOB_ID","offset":0}'
```

which answers `state`, `offset`, `next_offset`, `text` and `eof`. `job.events` gives the job's
timeline: each state and phase change with its time.

```json
{"ok":true,"result":{"id":"4986f069a0f74e73b5fcb55b63de0d46","label":"Broken · build · android · debug",
"state":"failed","phase":null,"events":[{"id":1,"kind":"state","value":"queued", ...},
{"id":2,"kind":"state","value":"running", ...},{"id":3,"kind":"phase","value":"building", ...},
{"id":4,"kind":"state","value":"failed","exit_code":1, ...}],"next_after":4,"recorded":true}}
```

Some jobs do not end while their work is useful. An emulator started by `android.avd_start` runs
as a job for as long as it is up, and a Flutter run holds its job until it exits. For those, check
the result you need instead: `device.list` until the emulator is listed with `"state":"device"`.

Jobs persist in a database and survive the client disconnecting. Keep the id: after a restart,
`job.get` or `job.list` (filter by `state`, `operation` or `project`) finds the job again. Do not
start the same build twice to find out what happened to the first; an identical request to a busy
project reuses the running job anyway.

## Read the error and its remedy

Every failure is one JSON object with a `code`, a `message` and usually a `remedy`, and the exit
status is not 0. Branch on `code`; show the user `message`; follow `remedy`. Real examples:

```json
{"ok":false,"error":{"code":"invalid_argument","message":"Unknown argument: arguments.platfrom","remedy":"Did you mean arguments.platform?"}}
{"ok":false,"error":{"code":"invalid_argument","message":"arguments.seconds is out of range","remedy":""}}
{"ok":false,"error":{"code":"tool_missing","message":"adb is not installed/configured","remedy":"Install Android SDK platform-tools"}}
{"ok":false,"error":{"code":"project_unrecognized","message":"No supported project in /absolute/notes","remedy":"Open a Flutter, Gradle, Swift, xtool, web or Xcode project, or add tomacco.json tasks"}}
```

- `invalid_argument` is refused before anything runs, so the call is safe to correct and repeat.
  Read the schema (`tomaccoctl schema OPERATION`) instead of guessing other keys.
- An empty `remedy` means there is no generic fix; the `message` is the information.
- A job that ended badly under `--wait` answers `job_failed`, `job_cancelled` or `job_timed_out`,
  with the final job under `error.details.job`. A real one, after a Kotlin syntax error:

  ```json
  {"ok":false,"error":{"code":"job_failed","message":"Job 4986f069a0f74e73b5fcb55b63de0d46 (Broken · build · android · debug) ended failed",
  "remedy":"Read the log above, or tomaccoctl call job.logs '{\"job_id\":\"4986f069a0f74e73b5fcb55b63de0d46\"}'",
  "details":{"job":{"state":"failed","exit_code":1, ...}}}}
  ```

  The log had the compiler's own line, `e: file:///.../MainActivity.kt:71:1 Syntax error:
  Expecting a top level declaration.` Fix the source, then build again.

## Safe and destructive operations

Every operation carries the four MCP annotations, and `tomaccoctl schema` shows them under
`annotations`. Of the 203 operations, 67 are read-only and 22 are destructive.

| Annotation | What it means for an agent |
| --- | --- |
| `readOnlyHint: true` | Changes nothing. Call it freely: `capabilities`, `doctor`, `device.list`, `job.wait`, `project.inspect`. |
| `destructiveHint: true` | May delete or overwrite data. Ask the user first, and say what will be lost. |
| neither | Changes state without being marked as deleting data: builds, installs, emulator starts. Fine within the task the user gave. |
| `idempotentHint: true` | Repeating the call with the same arguments has no further effect, so a retry after a lost answer is safe. |
| `openWorldHint: true` | Reaches devices, tools, files or the network outside Tomacco's own state. |

The destructive ones are `catalog.remove`, `project.configure`, `job.cancel`, `storage.prune`,
`device.install`, `device.app`, `android.each`, `android.avd_profile`, `android.emulator`,
`remote.run`, `workspace.remove`, `files.write`, `release.version`, `apple.simulator`,
`apple.device`, `play.commit`, `play.abort`, `ios.emulator.restore`, `ios.virtual.cleanup`,
`ios.virtual.state_delete`, `update.apply` and `update.rollback`. The [reference](reference/README.md) marks each.

Two of them are dry runs unless told otherwise. Show the user what the dry run lists, and only
then repeat it:

```sh
tomaccoctl call storage.prune '{"older_than_days":14}'
tomaccoctl call storage.prune '{"older_than_days":14,"apply":true}'
```

`ios.virtual.cleanup` works the same way with `dry_run:false`. `files.write` takes the digest
from `files.read` as `if_match`, so it refuses to overwrite a file the user changed in between.
Some calls are not destructive by the annotation but still need the user's word, as the
[agent guide](agents.md) says: publishing with `play.commit` or `ios.store_ship` with `upload`, and
downloads that accept a license.

## Recover from failures

- **Find out before retrying.** A failed job's log and `job.events` say where it stopped. Change
  something, the source, an argument, a setting, before running it again; the same call usually
  fails the same way.
- **Do not trust an old artifact.** After a failed build, an `.apk` from an earlier build may still
  be there. Artifacts in a job's result carry `modified_in_job`; check it before installing.
- **Use the built-in retry where it exists.** `android.sdk_install`, `ios.wda_prepare` and
  `android.connect` take `retry` (`count` and `backoff_seconds`) for network failures. Other
  operations do not, because repeating them is not known to be safe.
- **Busy is not broken.** `workspace_busy` names the job that holds the project. Wait for it, or
  cancel it with `job.cancel` if the user agrees; do not work around it with a copy of the project.
  A third build waits as `queued` with a `queued_reason`.
- **Clean up what you start.** Stop an emulator you started (`android.emulator` with `stop`) and a
  display session (`runtime.stop`) when the task is done, unless the user wants them running.
- **Report what you could not fix.** `tomaccoctl report` writes a local bug-report bundle with the
  version, host, `doctor` and the recent failed jobs, secrets masked. It sends nothing; tell the
  user where it is and that they should read it before sharing it.

```sh
tomaccoctl call job.cancel '{"job_id":"JOB_ID"}'
tomaccoctl report /absolute/tomacco-report.tar.gz
```

## A loop that works

1. `capabilities`; stop and set up what is missing, with the user's consent for downloads.
2. `project.inspect`, then `project.plan` for what a build will run.
3. Build or run, and wait for the job to end.
4. On `failed`, read `job.logs`, fix the cause and go back to 3.
5. Look at the result: `device.capture`, `android.hierarchy`, `testing.find`. A launched app is not
   a working app until the screen shows it.
6. Test: `project.execute` with `instrumented-test`, then `testing.results` with the test job's
   id; or `testing.run` with a scenario.
7. Clean up, and tell the user what ran and what did not.

[Your first Android app](tutorial-android.md) is this loop with real calls.
