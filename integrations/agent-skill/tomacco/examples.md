# Tomacco calls for common tasks

Each line is a `tomaccoctl` call; over MCP, call the tool of the same name with the same JSON.
Replace the absolute paths, `emulator-5554` (a serial from `device.list`) and `JOB_ID` (`result.id`
of the call that started the job). `tomaccoctl help OPERATION` lists every argument.

## Plan

```sh
tomaccoctl call capabilities '{}'
tomaccoctl doctor
tomaccoctl call project.inspect '{"path":"/absolute/MyApp"}'
tomaccoctl call project.plan '{"path":"/absolute/MyApp","platform":"android","action":"build"}'
```

## Run an Android app and look at it

```sh
tomaccoctl call android.avds '{}'
tomaccoctl call android.avd_start '{"name":"Tomacco"}'
tomaccoctl call device.list '{"platform":"android"}'
tomaccoctl call project.run '{"path":"/absolute/MyApp","platform":"android","device":"emulator-5554"}'
tomaccoctl call job.wait '{"job_id":"JOB_ID","seconds":60}'
tomaccoctl call device.capture '{"platform":"android","device":"emulator-5554","path":"/absolute/screen.png"}'
tomaccoctl call android.hierarchy '{"device":"emulator-5554"}'
tomaccoctl call device.logs '{"platform":"android","device":"emulator-5554","bundle_id":"dev.example.myapp","duration":30}'
```

An emulator started by `android.avd_start` keeps its job running while it is up: wait until
`device.list` shows it with `"state":"device"`, not for the job to end.

## Build and test

```sh
tomaccoctl call project.execute '{"path":"/absolute/MyApp","platform":"android","action":"build"}' --wait
tomaccoctl call project.execute '{"path":"/absolute/MyApp","platform":"android","action":"instrumented-test","device":"emulator-5554"}' --wait
tomaccoctl call testing.results '{"path":"/absolute/MyApp","job_id":"JOB_ID"}'
tomaccoctl call job.logs '{"job_id":"JOB_ID","offset":0}'
```

## Drive the UI and run UI scenarios

```sh
tomaccoctl call testing.find '{"device":"emulator-5554","selector":{"text":"Settings"}}'
tomaccoctl call testing.action '{"device":"emulator-5554","selector":{"text":"Settings"},"action":"click"}'
tomaccoctl call testing.run '{"path":"/absolute/ui-tests","directory":"/absolute/ui-results","devices":["emulator-5554"]}'
```

## Store screenshots

```sh
tomaccoctl call android.store_screenshots '{"bundle_id":"dev.example.myapp","devices":["emulator-5554"],"locales":["en-US","de-DE"],"themes":["light","dark"],"screens":[{"name":"home"}],"directory":"/absolute/screenshots"}'
```

## Performance

```sh
tomaccoctl call profile.startup '{"device":"emulator-5554","bundle_id":"dev.example.myapp","mode":"cold","runs":5}'
tomaccoctl call profile.frames '{"device":"emulator-5554","bundle_id":"dev.example.myapp","seconds":10}'
tomaccoctl call profile.memory '{"device":"emulator-5554","bundle_id":"dev.example.myapp"}'
```

## The virtual iPhone

```sh
tomaccoctl call ios.virtual.status '{}'
tomaccoctl call ios.virtual.start '{}'
tomaccoctl call project.create '{"path":"/absolute/MyApp","kind":"xtool","name":"MyApp","organization":"dev.example","build":true}'
tomaccoctl call project.app '{"path":"/absolute/MyApp"}'
tomaccoctl call ios.virtual.deploy '{"bundle":"/absolute/MyApp/xtool/MyApp.app"}'
tomaccoctl call ios.virtual.screenshot '{"path":"/absolute/iphone.png"}'
```

`ios.virtual.status` says whether a profile is configured and the phone is running. Without a
profile, `ios.virtual.setup` plans the setup from the person's own files; stages marked `ask_first`
need their yes.

## Several steps in one call

```sh
tomaccoctl call workflow.run '{"steps":[{"operation":"project.execute","arguments":{"path":"/absolute/MyApp","platform":"android","action":"build"}},{"operation":"job.get","arguments":{"job_id":{"from_step":0,"pointer":"/result/id"}}}]}'
```
