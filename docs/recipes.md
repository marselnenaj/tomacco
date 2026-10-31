# Recipes

Short sequences to copy into a coding agent's session or a terminal. Every line is a
real `tomaccoctl call`, checked against the schema like the other guides. Over MCP, the
same operation names are the tool names.

Three rules hold for all of them:

- Paths are absolute. Replace `/absolute/...` with yours.
- A call that starts long work answers with a job; its id is `result.id`. Repeat
  `tomaccoctl call job.wait '{"job_id":"JOB_ID","seconds":60}'` until the state is
  `succeeded`, `failed`, `cancelled` or `timed_out`, and read `job.logs` when it fails.
  Queued or running is not success.
- On an error, read its `message` and `remedy` before trying again.

The [agent guide](agents.md) has the full contract, and [Examples](../examples/README.md)
has a project to try these on.

## Build and run an Android app

Needs the Android SDK, a JDK and Gradle configured, and an emulator or phone; set them up
with `tomaccoctl setup` or [route C of getting started](getting-started.md#route-c-a-new-android-app).

```sh
tomaccoctl call project.inspect '{"path":"/absolute/MyApp"}'
tomaccoctl call project.execute '{"path":"/absolute/MyApp","platform":"android","action":"build","format":"apk"}'
tomaccoctl call job.wait '{"job_id":"JOB_ID","seconds":60}'
tomaccoctl call device.list '{"platform":"android"}'
tomaccoctl call project.run '{"path":"/absolute/MyApp","platform":"android","device":"emulator-5554"}'
```

No emulator yet? `android.avds` lists them and `android.avd_start` starts one:

```sh
tomaccoctl call android.avds '{}'
tomaccoctl call android.avd_start '{"name":"Tomacco"}'
```

For a new app instead of an existing one, start with `project.create`:

```sh
tomaccoctl call project.create '{"path":"/absolute/MyApp","kind":"android","name":"MyApp","organization":"dev.example"}'
```

## Take a screenshot and test the UI

With the app running on `emulator-5554`. `device.capture` waits up to 30 seconds for the
picture, and over MCP returns the PNG inline. `android.hierarchy` gives the UI tree with
each element's text, id and bounds.

```sh
tomaccoctl call device.capture '{"platform":"android","device":"emulator-5554","path":"/absolute/screen.png"}'
tomaccoctl call android.hierarchy '{"device":"emulator-5554"}'
```

Find and press controls by what they say rather than by coordinates, then check the result:

```sh
tomaccoctl call testing.find '{"device":"emulator-5554","selector":{"text":"Add one"}}'
tomaccoctl call testing.action '{"device":"emulator-5554","selector":{"text":"Add one"},"action":"click"}'
tomaccoctl call testing.find '{"device":"emulator-5554","selector":{"text":"1"},"timeout":5}'
```

To repeat a whole flow, write it as a scenario and run it with `testing.run`. It writes a
JSON report, JUnit results and screenshots of failures into `directory`. The format is in
[examples/hello-android/ui-test.json](../examples/hello-android/ui-test.json).

```sh
tomaccoctl call testing.run '{"path":"/absolute/ui-test.json","directory":"/absolute/ui-results","devices":["emulator-5554"]}'
```

To catch visual changes, compare a fresh capture with a baseline you approved. The first
time, look at the picture and keep it with `update_baseline`; later calls report the
changed fraction and write a diff image with the changes in red:

```sh
tomaccoctl call testing.visual_diff '{"actual":"/absolute/screen.png","baseline":"/absolute/baselines/home.png","update_baseline":true}'
tomaccoctl call testing.visual_diff '{"actual":"/absolute/screen.png","baseline":"/absolute/baselines/home.png","diff":"/absolute/home-diff.png","threshold":0.001}'
```

Text through `android.input` is ASCII only. For Unicode, install the companion once with
`testing.setup`, then use `testing.action` with `fill`.

## Build an iOS app on Linux with xtool

Needs Swift and xtool, and Apple's SDK imported from an `Xcode.xip` you downloaded from
Apple yourself. Tomacco does not ship or download that SDK. `ios.doctor` says what is
missing. This builds an `.app` on Linux; it does not need a phone.

```sh
tomaccoctl call ios.sdk_install '{"path":"/absolute/Xcode.xip","slim":true}'
tomaccoctl call ios.doctor '{}'
tomaccoctl call project.create '{"path":"/absolute/MyApp","kind":"xtool","name":"MyApp","organization":"dev.example","build":true}'
tomaccoctl call job.wait '{"job_id":"JOB_ID","seconds":60}'
tomaccoctl call project.app '{"path":"/absolute/MyApp"}'
```

`project.app` names the built `.app`, its `bundle_id`, and whether the virtual iOS device
accepts it. After changing the sources, rebuild:

```sh
tomaccoctl call project.execute '{"path":"/absolute/MyApp","platform":"ios"}'
```

A signed IPA and running on a real iPhone need an Apple account and a paired phone; follow
the [iPhone guide](iphone.md). Execution on a physical iPhone has not been qualified.

## Put an app on the virtual iPhone

Needs a configured and set-up [virtual iOS device](virtual-ios/README.md), which is
experimental and takes a separate setup with your own Apple images. Start it if it is not
running. The start is one durable job of about 9 minutes; follow it with `ios.virtual.status`
and `job.logs` until the phase is `launch-accepted`.

```sh
tomaccoctl call ios.virtual.status '{}'
tomaccoctl call ios.virtual.start '{}'
tomaccoctl call job.logs '{"job_id":"JOB_ID","offset":0}'
```

Accepted is not the same as visible: `display_visible:false` in the status means every frame
stayed black. On the running phone, one call installs and opens an arm64 `.app` without a
reboot, in about 25 seconds:

```sh
tomaccoctl call ios.virtual.deploy '{"bundle":"/absolute/MyApp/xtool/MyApp.app"}'
tomaccoctl call job.logs '{"job_id":"JOB_ID","offset":0}'
```

The bundle rules (thin arm64, size limits, allowed entitlements) are in
[usage](virtual-ios/usage.md#deploy-your-own-app); a refusal names the file and the reason.
To keep the app across restarts and open it at each start:

```sh
tomaccoctl call ios.virtual.configure '{"add_application":"/absolute/MyApp/xtool/MyApp.app"}'
tomaccoctl call ios.virtual.configure '{"application":"dev.example.MyApp"}'
```
