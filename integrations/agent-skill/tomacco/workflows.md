# Tomacco workflows

Each recipe is a sequence of calls. Each line is a `tomaccoctl` call; over MCP, call the tool of the
same name with the same JSON. Replace the absolute paths, `emulator-5554` (a serial from `device.list`),
`dev.example.myapp` (the app's package) and `JOB_ID` (`result.id` of the call that started the job).
[references/index.md](references/index.md) lists every operation's arguments, and
`tomaccoctl help OPERATION` prints them too.

A call that starts a job answers at once. Follow it with `job.wait`, repeated until the state is
`succeeded`, `failed`, `cancelled` or `timed_out`; on the CLI, `--wait` does that for you.

```sh
tomaccoctl call job.wait '{"job_id":"JOB_ID","seconds":60}'
tomaccoctl call job.logs '{"job_id":"JOB_ID","offset":0}'
```

## Plan

```sh
tomaccoctl call capabilities '{}'
tomaccoctl doctor
tomaccoctl call project.inspect '{"path":"/absolute/MyApp"}'
tomaccoctl call project.plan '{"path":"/absolute/MyApp","platform":"android","action":"build"}'
```

`capabilities` says what is ready; where `ready` is false, `missing` says why. `doctor` lists
`next_steps`; steps with `ask_first` need the person's yes. `project.plan` prints the commands a build
would run, without running them.

## Build, install, run, look, act, verify (Android)

```sh
tomaccoctl call android.avds '{}'
tomaccoctl call android.avd_start '{"name":"Tomacco"}'
tomaccoctl call device.list '{"platform":"android"}'
tomaccoctl call project.run '{"path":"/absolute/MyApp","platform":"android","device":"emulator-5554"}'
tomaccoctl call job.wait '{"job_id":"JOB_ID","seconds":60}'
tomaccoctl call screen.marks '{"device":"emulator-5554","path":"/absolute/marks.png"}'
tomaccoctl call screen.tap '{"device":"emulator-5554","mark":3}'
tomaccoctl call screen.wait '{"device":"emulator-5554","until":"text","text":"Welcome"}'
tomaccoctl call device.capture '{"platform":"android","device":"emulator-5554","path":"/absolute/after.png"}'
tomaccoctl call device.logs '{"platform":"android","device":"emulator-5554","bundle_id":"dev.example.myapp","duration":30}'
```

An emulator started by `android.avd_start` keeps its job running while it is up: wait until
`device.list` shows it with `"state":"device"`, not for the job to end. `project.run` builds,
installs and launches; use `project.execute` with `action` `build` for a build alone and
`device.install` (destructive: it replaces the installed app) to install a file you have.
`screen.marks` returns a PNG with numbers and a list of what each number is; `screen.tap` checks the
screen still matches before it taps and answers `mark_stale` if not, so call `screen.marks` again.

## Understand a screen

```sh
tomaccoctl call screen.tree '{"device":"emulator-5554","only_interactive":true}'
tomaccoctl call screen.ocr '{"device":"emulator-5554","text":"Sign in"}'
tomaccoctl call screen.marks '{"device":"virtual-iphone","path":"/absolute/marks.png","source":"ocr"}'
tomaccoctl call screen.diff '{"before":"/absolute/before.png","after":"/absolute/after.png"}'
```

`screen.tree` is the UI hierarchy as short text. `screen.ocr` reads text off the screenshot and
returns where it is, for content the hierarchy does not have. The virtual iPhone has no hierarchy, so
its marks come from OCR.

## Drive the UI by selector

```sh
tomaccoctl call testing.find '{"device":"emulator-5554","selector":{"text":"Settings"}}'
tomaccoctl call testing.action '{"device":"emulator-5554","selector":{"text":"Settings"},"action":"click"}'
tomaccoctl call testing.action '{"device":"emulator-5554","selector":{"resource_id":"dev.example.myapp:id/email"},"action":"fill","text":"a@example.com"}'
tomaccoctl call android.input '{"device":"emulator-5554","action":"keyevent","key":"BACK"}'
```

## Build and test

```sh
tomaccoctl call project.execute '{"path":"/absolute/MyApp","platform":"android","action":"test"}' --wait
tomaccoctl call project.execute '{"path":"/absolute/MyApp","platform":"android","action":"instrumented-test","device":"emulator-5554"}' --wait
tomaccoctl call testing.results '{"path":"/absolute/MyApp","job_id":"JOB_ID"}'
tomaccoctl call testing.crashes '{"device":"emulator-5554","bundle_id":"dev.example.myapp"}'
tomaccoctl call job.logs '{"job_id":"JOB_ID","offset":0}'
```

## UI scenarios and accessibility

```sh
tomaccoctl call testing.run '{"path":"/absolute/ui-tests","directory":"/absolute/ui-results","devices":["emulator-5554"]}'
tomaccoctl call testing.accessibility '{"device":"emulator-5554","bundle_id":"dev.example.myapp"}'
tomaccoctl call testing.visual_diff '{"actual":"/absolute/actual.png","baseline":"/absolute/baseline.png"}'
```

## Store screenshots

```sh
tomaccoctl call android.settings '{"device":"emulator-5554","dark_mode":"off","font_scale":1,"animations":false}'
tomaccoctl call android.store_screenshots '{"bundle_id":"dev.example.myapp","devices":["emulator-5554"],"locales":["en-US","de-DE"],"themes":["light","dark"],"screens":[{"name":"home"}],"directory":"/absolute/screenshots"}'
```

## Performance

Use the same build, device and settings for every measurement, and several runs.

```sh
tomaccoctl call profile.startup '{"device":"emulator-5554","bundle_id":"dev.example.myapp","mode":"cold","runs":5,"report":"/absolute/startup.json"}'
tomaccoctl call profile.frames '{"device":"emulator-5554","bundle_id":"dev.example.myapp","seconds":10,"report":"/absolute/frames.json"}'
tomaccoctl call profile.memory '{"device":"emulator-5554","bundle_id":"dev.example.myapp","report":"/absolute/memory.json"}'
tomaccoctl call profile.compare '{"baseline":"/absolute/startup-before.json","current":"/absolute/startup.json"}'
tomaccoctl call profile.budget '{"reports":["/absolute/startup.json","/absolute/frames.json"]}'
```

## Macrobenchmark and baseline profiles

```sh
tomaccoctl call project.inspect '{"path":"/absolute/MyApp"}'
tomaccoctl call profile.macrobenchmark '{"path":"/absolute/MyApp","device":"R5CT1234567","report":"/absolute/macrobenchmark.json"}'
tomaccoctl call profile.benchmark_results '{"path":"/absolute/MyApp","job_id":"JOB_ID"}'
tomaccoctl call profile.baseline_profile '{"path":"/absolute/MyApp","device":"R5CT1234567"}'
tomaccoctl call profile.baseline_compare '{"device":"R5CT1234567","bundle_id":"dev.example.myapp","runs":5}'
```

`project.inspect` lists the benchmark and baseline profile modules under `performance`. Both jobs
refuse an emulator unless `allow_emulator` is set, because emulator numbers mislead.

## Release

```sh
tomaccoctl call release.checklist '{"path":"/absolute/MyApp"}'
tomaccoctl call release.version '{"path":"/absolute/MyApp","bump":"patch","dry_run":true}'
tomaccoctl call project.execute '{"path":"/absolute/MyApp","platform":"android","action":"build","configuration":"release","format":"aab"}' --wait
tomaccoctl call release.sign '{"path":"/absolute/app-release-unsigned.aab","output":"/absolute/app-release.aab","keystore":"/absolute/upload.jks","password_file":"/absolute/store-password.txt","alias":"upload"}'
tomaccoctl call release.verify '{"path":"/absolute/MyApp/app/build/outputs/bundle/release/app-release.aab"}'
tomaccoctl call play.prepare '{"credentials":"/absolute/play-key.json","package":"dev.example.myapp","path":"/absolute/app-release.aab","release_name":"1.2.0","track":"internal"}'
```

`release.sign` reads the keystore password from a file, never from an argument; keep the file out of
the repository. `release.version` writes the version files unless `dry_run` is true: show the person the dry run, then
repeat it without `dry_run` after a yes. `play.prepare` uploads to an edit and publishes nothing;
`play.commit` publishes and always needs the person's yes. `play.abort` drops the edit.

## The virtual iPhone

```sh
tomaccoctl call ios.virtual.status '{}'
tomaccoctl call ios.virtual.start '{}'
tomaccoctl call project.create '{"path":"/absolute/MyApp","kind":"xtool","name":"MyApp","organization":"dev.example","build":true}'
tomaccoctl call project.app '{"path":"/absolute/MyApp"}'
tomaccoctl call ios.virtual.deploy '{"bundle":"/absolute/MyApp/xtool/MyApp.app"}'
tomaccoctl call ios.virtual.screenshot '{"path":"/absolute/iphone.png"}'
tomaccoctl call ios.virtual.syslog '{"bundle_id":"dev.example.myapp"}'
tomaccoctl call ios.virtual.stop '{}'
```

`ios.virtual.status` says whether a profile is configured and the phone is running. Without a
profile, `ios.virtual.setup` plans the setup from the person's own files; stages marked `ask_first`
need their yes. `ios.virtual.start` is a job that stays up while the phone runs: follow it with
`ios.virtual.status`. Stop the phone when you are done unless the person wants it running.

## A physical iPhone

```sh
tomaccoctl call ios.device_status '{}'
tomaccoctl call ios.doctor '{"path":"/absolute/MyApp"}'
tomaccoctl call ios.signing '{}'
tomaccoctl call ios.apps '{"device":"UDID"}'
tomaccoctl call device.capture '{"platform":"ios","device":"UDID","path":"/absolute/iphone.png"}'
tomaccoctl call device.logs '{"platform":"ios","device":"UDID","bundle_id":"dev.example.myapp","duration":30}'
```

`UDID` is the device's id from `device.list`. `ios.prepare` with `action` `pair` or `login` does the
steps that need the person at the phone.

## Several steps in one call

```sh
tomaccoctl call workflow.run '{"steps":[{"operation":"project.execute","arguments":{"path":"/absolute/MyApp","platform":"android","action":"build"}},{"operation":"job.get","arguments":{"job_id":{"from_step":0,"pointer":"/result/id"}}}]}'
```

## When a call fails

```sh
tomaccoctl schema screen.marks
tomaccoctl call job.list '{}'
tomaccoctl call agent.policy '{}'
```

Read `error.code` and `error.remedy` in the answer, then look the code up in
[references/errors.md](references/errors.md). `invalid_argument`: compare your call with
`tomaccoctl schema OPERATION`. `workspace_busy` or `device_busy`: another job holds it, so
`job.list` and wait for that job. `policy_denied`: `agent.policy` shows why; only the person can
change it.
