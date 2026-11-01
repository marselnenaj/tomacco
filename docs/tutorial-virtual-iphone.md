# Your first SwiftUI app on the virtual iPhone

From the SwiftUI starter in [`examples/hello-virtual-iphone`](../examples/hello-virtual-iphone/) to the app on
Tomacco's experimental [virtual iPhone](virtual-ios/README.md), then a loop that rebuilds and reinstalls it on every
save. Every step is one `tomaccoctl` call; an agent makes the same calls over MCP.

**What was run and what was not.** Steps 1 to 3 ran on the development host with xtool 1.20.1 and the slim Darwin SDK
xtool had installed from an Xcode download: the first build took 10.7 seconds, a rebuild after an edit 6.3 seconds, and
`project.app` accepted the bundle for the virtual iPhone. That run used its own state directory
(`TOMACCO_HOME=/absolute/tutorial-home`) and no phone. Steps 4 to 6 (run, watch, logs and crashes on the phone) have
unit tests and tests with stand-in tools only; **they have not been run on a live phone yet.**

## 1. What you need

- A virtual iPhone that is set up and starts: [setup](virtual-ios/setup.md) from your own restore images. It needs a
  separate setup and is experimental.
- xtool and the iOS SDK imported from an `Xcode.xip` that you downloaded from Apple. Tomacco does not ship that SDK or
  download it; read Xcode's license first. If xtool does not have the SDK yet:

```sh
tomaccoctl call ios.sdk_install '{"path":"/absolute/Xcode.xip","slim":true}'
```

The [iPhone guide](iphone.md#from-zero-to-a-running-app) has the full route to xtool and the SDK on Linux.

## 2. Copy the starter and build it

Copy the example out of the checkout, so that its build output stays out of the repository:

```sh
cp -r examples/hello-virtual-iphone /absolute/HelloVirtual
```

```sh
tomaccoctl call project.inspect '{"path":"/absolute/HelloVirtual"}'
tomaccoctl call project.execute '{"path":"/absolute/HelloVirtual","platform":"ios"}'
tomaccoctl call job.wait '{"job_id":"BUILD_JOB","seconds":60}'
```

`project.inspect` names the kinds `xtool` and `swift`. The build runs `xtool dev build` and writes
`xtool/HelloVirtual.app`, a thin arm64 executable of about 130 KB with no entitlements and no resources.

The starter is a counter with large **Add one** and **Reset** buttons: the virtual iPhone is driven with taps at screen
coordinates, so big targets are easy to hit, and it has no animations. It writes `HelloVirtual launched` at launch and
`HelloVirtual count N` on each tap with `NSLog`, so its lines are easy to find in step 5.

## 3. Check the bundle

```sh
tomaccoctl call project.app '{"path":"/absolute/HelloVirtual"}'
```

`virtual_device.accepted` true means the bundle passes the virtual iPhone's rules (thin arm64, sizes, allowed
entitlements); `bundle_id` is `dev.tomacco.examples.HelloVirtual`. Change `bundleID` in `xtool.yml` for an app of your own.

## 4. Run it on the phone

Start the phone if it is off, and wait until `ios.virtual.status` says `launch-accepted`:

```sh
tomaccoctl call ios.virtual.start '{}'
tomaccoctl call ios.virtual.status '{}'
```

Then one job builds, installs and opens the app, and looks for a crash report of it for 10 seconds:

```sh
tomaccoctl call project.run '{"path":"/absolute/HelloVirtual","platform":"ios","device":"virtual-iphone"}'
tomaccoctl call job.wait '{"job_id":"RUN_JOB","seconds":60}'
tomaccoctl call job.logs '{"job_id":"RUN_JOB","offset":0}'
```

The install is `ios.virtual.deploy`, about 25 seconds on the phone without a reboot. The last log line sums the run up,
for example `built in 6.3 s, deployed in 24.1 s, pid 612, no new crash report in 10 s`, and
`build/tomacco/virtual_iphone_run.json` in the project has the details. When the signed app has not changed since the
last install, the deploy only opens it and the line says `unchanged`. With the phone off, the call is refused at once
with `ios_virtual_not_running`.

## 5. Keep it current while you edit

```sh
tomaccoctl call project.watch '{"path":"/absolute/HelloVirtual","platform":"ios","device":"virtual-iphone"}'
tomaccoctl call job.logs '{"job_id":"WATCH_JOB"}'
```

Edit `Sources/HelloVirtual/ContentView.swift` and save. Each save that settles starts one cycle and one log line, such as
`cycle 1: 1 file changed (Sources/HelloVirtual/ContentView.swift): built in 6.3 s, deployed in 24.1 s, pid 640, ...`.
xtool's output (`xtool`, `.xtool`, `.bsp`) and `build` are not watched. Cancel the job with `job.cancel` to stop.

The app's own lines, across relaunches, from the phone's console log; pass `next_offset` back as `offset` to follow it:

```sh
tomaccoctl call ios.virtual.syslog '{"bundle_id":"dev.tomacco.examples.HelloVirtual"}'
```

Which of an app's lines reach the console the phone writes, its `NSLog` lines or launchd's lines about it, has not been
checked on a live phone yet.

## 6. When it crashes

A crash report that appears within `crash_check_seconds` of the launch fails the run as `crashed`, and the report's
exception, message and stack are in `crash_check.crashes`. For a crash later on:

```sh
tomaccoctl call testing.crashes '{"device":"virtual-iphone","process":"HelloVirtual","bundle_id":"dev.tomacco.examples.HelloVirtual"}'
```

No crash report never proves the app did not crash. The [troubleshooting guide](virtual-ios/troubleshooting.md) covers
an app that does not open.
