# Examples

Small projects and files an agent or a person can run with `tomaccoctl`. Each one
says what it needs. Long operations answer with a durable job: repeat `job.wait`
with its `result.id` until the state is `succeeded`, `failed`, `cancelled` or
`timed_out`.

| Example | What it is | What it needs |
| --- | --- | --- |
| [hello-android](hello-android/) | The native Android starter, with a UI test | Android SDK with platform 36, a JDK, Gradle; an emulator or phone to run it |
| [native-navigation.json](native-navigation.json) | The format of a `testing.run` scenario, for an app of your own | An Android device and an app with the named controls |
| [ios-restore-native](ios-restore-native/) | C and Embedded Swift programs for the iOS restore-shell guest | The virtual iOS research setup; see its README |

## hello-android

This is exactly what `project.create` writes for `kind` `android`, name
`HelloAndroid` and organization `dev.tomacco.examples`: a native Android app with
a counter, **Add one** and **Reset**. Its application ID is
`dev.tomacco.examples.helloandroid`.

Set up the Android tools first, as in
[getting started, route C](../docs/getting-started.md#route-c-a-new-android-app). The
starter compiles against `platforms;android-36`. Copy the example out of the checkout,
so its build output stays out of the repository:

```sh
cp -r examples/hello-android /absolute/HelloAndroid
```

Build it, then find a device and run it:

```sh
tomaccoctl call project.execute '{"path":"/absolute/HelloAndroid","platform":"android","action":"build","format":"apk"}'
tomaccoctl call job.wait '{"job_id":"JOB_ID","seconds":60}'
tomaccoctl call device.list '{"platform":"android"}'
tomaccoctl call project.run '{"path":"/absolute/HelloAndroid","platform":"android","device":"emulator-5554"}'
```

The build writes `app/build/outputs/apk/debug/app-debug.apk`. Check the screen,
then run the UI test. `ui-test.json` clears the app's data, opens it, taps **Add one**,
checks the counter, takes a screenshot and resets it. The report, JUnit results and
screenshots go into `directory`.

```sh
tomaccoctl call device.capture '{"platform":"android","device":"emulator-5554","path":"/absolute/hello.png"}'
tomaccoctl call testing.run '{"path":"/absolute/HelloAndroid/ui-test.json","directory":"/absolute/hello-ui","devices":["emulator-5554"]}'
```

**What was run.** The project was generated with `project.create` and built with
`project.execute` (Gradle 9.3.1, platform 36). `testing.run` accepted `ui-test.json`
as a valid scenario. Running the app and the scenario on a device was not part of
that check.

## native-navigation.json

A scenario for a different app (`dev.example.tomatoes`), kept as a reference for the
step format: `launch`, `assert`, `click`, `screenshot` and `key`. The other steps are
`wait`, `long_click`, `fill`, `stop` and `clear`. Change the application ID and
selectors to your app's, then pass it to `testing.run` as above.
