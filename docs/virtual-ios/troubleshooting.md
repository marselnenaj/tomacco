# Virtual iOS device: troubleshooting

Symptom, cause, fix. Error codes and messages are the ones Tomacco really prints. For the rest of
the product, see the [general troubleshooting guide](../troubleshooting.md).

**Start here.** These two say what state you are in:

```sh
python3 tools/ios_virtual_profile.py --check
tomaccoctl call ios.virtual.status '{}'
```

To read why a start failed, use the job from `busy` or from the start's own answer:

```sh
tomaccoctl call job.logs '{"job_id":"START_JOB","offset":0}'
```

## Error codes

Every error code of the virtual iOS device that `tomaccoctl` returns, in alphabetical order. The code is the
`error.code` of the JSON answer; its message and remedy say more. A test checks that each code here exists in
the code and that none is missing.

| Code | What it means | What to do |
| --- | --- | --- |
| `ios_boot_failed` | `ios.emulator.probe` saw the boot fail early; the message is the diagnosis. | Read the diagnosis and `ios.emulator.report`. |
| `ios_control_busy` | Another action holds the guest's control lane. | Ask again. |
| `ios_control_ended` | The emulator has exited while its job finishes hashing the disks. | Wait for the job. Do not cancel it. |
| `ios_control_starting` | The control monitor is not published yet. | Ask again in a moment. |
| `ios_emulator_busy` | Another job holds this guest. The error names it. | Wait for that job or `job.cancel` it. |
| `ios_frame_file_unavailable` | No private runtime directory exists, or the frame would exceed 64 MiB. | Use `frame` with `transport:"inline"`. |
| `ios_guest_busy` | The guest answers but is working, so it is not stopped. | Try again when it is done. See [stopping](#stopping). |
| `ios_guest_control_unavailable` | The run or job has no guest channel. | Start the phone with `ios.virtual.start`, which always has one. |
| `ios_guest_crash_reports_failed` | The guest could not read its crash reports. | The `response` in the details says why. Try again later in the run. |
| `ios_guest_disconnected` | The guest closed the control stream during a request. | Check `ios.virtual.status`: the guest may have stopped. |
| `ios_guest_not_idle` | The guest is still working, so it is not stopped. | Try again when it is done. |
| `ios_guest_paused` | The virtual CPU is not running. | Wait for the boot, or check `ios.virtual.status`. |
| `ios_guest_progress_unavailable` | Install progress could not be read. It comes in `progress_error`; the install goes on. | Wait for the install's result. |
| `ios_guest_receipt_unavailable` | The guest could not read its operation receipt. | Ask again. The details carry the guest's log. |
| `ios_guest_starting` | The guest service is not up yet. | Wait, and watch `ios.virtual.status`. |
| `ios_guest_stopped` | The guest job has stopped. | Start the phone again. |
| `ios_guest_timeout` | The guest did not reply before the request's deadline. | Ask again. Under load the guest answers slowly. |
| `ios_prepare_busy` | Another job is preparing into this output. | Wait for that job or `job.cancel` it. |
| `ios_setup_confirm` | `ios.virtual.setup` reached a stage that builds for long or needs the person's template. | Ask the person, then call again with `confirm:true`. |
| `ios_setup_inputs_invalid` | Some of the person's own files are not the ones the chain needs. | Read each failed entry's `problem` and `fix` in `inputs`. Nothing long has started. |
| `ios_setup_manual` | The next stage is the restore, which boots a guest and is run by hand. | Run the three `commands` in order, then pass `restored:true`. |
| `ios_setup_needs_checkout` | The engine is built in a source checkout's `.tooling`. | Use the `tomaccoctl` of a Tomacco checkout for this stage. |
| `ios_setup_needs_input` | A stage needs files only the person has. `needs` names the arguments. | Pass them. [Setup](setup.md#what-no-tool-does-for-you) says where each comes from. |
| `ios_setup_out_of_order` | An earlier stage of the chain is not done. | Run that stage first; the error names it. |
| `ios_setup_partial` | An earlier run stopped halfway and left its output. | Move the output aside (nothing is deleted for you) and run the stage again. |
| `ios_touch_has_no_hover` | A touchscreen has no pointer that moves without a contact. | Use `click` or `drag`. |
| `ios_touch_lane_required` | A one-shot `tomaccoctl call` cannot hold a finger down. | Use `drag`, or `touch_down` through `tomaccoctl mcp`. |
| `ios_touch_not_down` | No contact is held down, or it was lifted after 5 seconds without an event. | Press again with `touch_down`; `touch_move` keeps a contact alive. |
| `ios_virtual_application_conflict` | Two listed bundles use the same bundle identifier. | Remove one with `ios.virtual.configure {remove_application}`. |
| `ios_virtual_application_unknown` | No listed bundle has this path or bundle identifier. | `ios.virtual.apps` lists them. |
| `ios_virtual_busy` | A start or stop is in progress. | Wait for it. The details name its job. |
| `ios_virtual_disk_space` | The start's disk copy does not fit on the filesystem of the profile's `installs`. | Free the space the remedy names. See [starting](#starting). |
| `ios_virtual_guest_state_unknown` | The guest job could not be observed. | Read `job.get` for the job in the details. |
| `ios_virtual_log_unavailable` | The guest job has no console log yet. | Start the phone, or wait until its boot has begun, then read `ios.virtual.syslog` again. |
| `ios_virtual_no_verified_run` | There is no verified run to save as a state. | Stop a run with `ios.virtual.stop` so it ends verified, then save it. |
| `ios_virtual_not_running` | The phone is off. | `ios.virtual.start`. |
| `ios_virtual_profile_invalid` | `profile.json` or `state.json` is missing or wrong. | `python3 tools/ios_virtual_profile.py --check`. See [setup](#setup). |
| `ios_virtual_running` | The phone is running, or its last run was not stopped. | Stop it with `ios.virtual.stop` first. |
| `ios_virtual_state_exists` | A saved state already has this name. | Choose another name, or delete the old state with `ios.virtual.state_delete`. |
| `ios_virtual_state_missing` | The disks of the saved state are gone. | Delete the state with `ios.virtual.state_delete`. |
| `ios_virtual_state_unknown` | No saved state has this name. | List the states with `ios.virtual.states`. |
| `ios_virtual_state_unsaved` | The current verified run is not saved, so a later start may delete its disks. | Save it with `ios.virtual.state_save`, or pass `discard_current:true`. |
| `ios_virtual_unconfigured` | No profile is selected. | Write one and call `ios.virtual.configure`. See [setup](setup.md). |

## Setup

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ios_virtual_unconfigured`: "No virtual iOS device profile is configured" | No profile is selected yet. | Write one with `tools/ios_virtual_profile.py`, then call `ios.virtual.configure` with its directory. See [setup](setup.md). |
| `ios_virtual_profile_invalid` | `profile.json` or `state.json` is missing or wrong. Nothing is created or repaired for you. | Read the message. `tools/ios_virtual_profile.py --check` lists what is wrong and the command that makes it. |
| The chain stops at the restore runtime or asks for a manifest | The local IM4M research manifest is missing. | Download the public template and run `tools/prepare_ios_research_manifest.py` with your firmware directories and prepared restore tree. See [the command](setup.md#how-do-i-get-the-im4m). Reuse its output for all later stages. |
| An image or kernel is refused | Only the exact firmware builds are accepted, because the adapters are pinned to their bytes. | Use the [versions in setup](setup.md#what-no-tool-does-for-you). |
| An archive is refused as encrypted | Tomacco has no decryption and refuses AEA archives. | Supply raw APFS images. |
| Helper preparation fails after a kernel update | The APFS module is built for the running kernel. | Repeat `tools/prepare_ios_emulator.py`, or keep the `vmlinuz` beside the module as `apfs/vmlinuz`. |
| `ios_emulator_busy` or `ios_prepare_busy` | Another job holds this guest or output. The error names it. | Wait for that job or `job.cancel` it. Do not use another state directory to get around the lock. |

## Starting

| Symptom | Cause | Fix |
| --- | --- | --- |
| "No verified run is recorded; the profile cannot start a guest" | `state.json` names no clean base. | Generate the profile from a finished [first install](setup.md#the-chain). |
| `ios_virtual_disk_space`: "The start copies ... of disks into ..." | The filesystem of the profile's `installs` lacks room for the session's disk copy. | Free the space the remedy names on that filesystem, or point `installs` at one with room. btrfs, or XFS with reflink, shares blocks with the base. |
| "The host was too busy (I/O or memory pressure) to boot the guest" | The machine was short of I/O or memory during boot. | Start again once it is quieter. |
| "The guest service did not answer within an hour" | The boot never got as far as the guest service. The phase stays `booting`. | Stop, then read `ios.emulator.report` for the guest job. |
| "The guest stopped during boot" | The emulator ended early. | Read `ios.emulator.report` for the guest job. |
| `display_visible:false` in the status | iOS accepted the launch, but every frame stayed black. This is a display defect, not a working phone. | Stop and start again. Look for "The boot kernel looks for the Metal driver at" or "The graphics payload's bridge opens the compiler plugin at" in the log: they name a kernel or payload built for another location. |
| A start ends with "Unlock the virtual iPhone with its passcode before opening the app" | The guest's lock screen has a passcode. | Unlock it by hand in the window, then start again. |
| "iOS refused the launch" | iOS itself said no. The reply is in the message. Tomacco does not bypass this. | Read the reply and the app's crash reports, below. |

## An app does not open

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ios.virtual.configure {add_application}` refuses a bundle | The bundle breaks a [bundle rule](usage.md#deploy-your-own-app). The message names the file, for example `Sample.app/Sample is built for x86_64, not arm64`, or `thin it to the arm64 slice` for a universal binary. | Fix what it names. |
| The start fails saying no clang, linker or ldid was found while it rebuilds guest tools | The rebuild needs clang 22, the linker and `ldid`. | `ios.virtual.configure {clang}`, or a `clang-22` on `PATH`. The others come from `profile.json` under `toolchain` or from `settings.json` under `executables`. |
| "The guest-tools rebuild into ... failed" | The set could not be rebuilt: no development signing, an experiment build, or another trust cache. The profile stays as it was. | Read the reason. A build made by hand with `ios.emulator.build_guest_tools` and `applications` must be named as `tools` in `profile.json`. |
| "The boot kernel does not pin the development-signing adapter's cdhash" | The kernel was prepared for other guest tools, so installd cannot map the adapter and rejects every own app. | Run the `tools/prepare_ios_adapter_pin.py` command the message gives, and point the profile's boot kernel at the result. |
| "installd's development-signing adapter did not activate" | The adapter did not come up in time, or reported a failed setup. | Read `ios.emulator.guest` `logs` `development`, below. |
| "installd did not install ..." | installd refused the app. The guest keeps running so you can look. | Read the reason, then the logs below. |
| The app opens and then disappears | It crashed. | Read its crash reports, below. Reports reach the next run's disk only if this run became the base, so read them during the run. |
| An app with a widget or share extension shows nothing from it | Extensions are verified offline only. The guest has only the system's own extension hosts. | `ios.emulator.guest` `lookup` with the app's bundle id answers `plugins`, the extensions iOS registered. |

Reading the guest's own view, with `GUEST_JOB` being `guest_job` from `ios.virtual.status`:

```sh
tomaccoctl call ios.emulator.guest '{"job_id":"GUEST_JOB","operation":"logs","argument":"development"}'
tomaccoctl call ios.emulator.guest '{"job_id":"GUEST_JOB","operation":"crash_reports","argument":"MyApp"}'
```

To keep an app's reports as files on the host, for example for `ios.crash_symbolicate`:

```sh
tomaccoctl call ios.virtual.crash_reports '{"process":"MyApp","directory":"/absolute/crashes"}'
tomaccoctl call ios.crash_symbolicate '{"report":"/absolute/crashes/mobile/MyApp-2026-10-31-120000.ips","binaries":["/absolute/MyApp.app/MyApp"]}'
```

Each report lands in `mobile/` or `root/` under the directory, after the CrashReporter directory it came from, and the
reply lists its `path`, `sha256` and the `pid` and `capture_time` read from it. The guest hands over at most three reports
per directory and 512 KiB in all, so `collection_complete:false` means some were left out, and an empty list does not
prove that nothing crashed. The saving has unit tests; it has not been run against a live phone yet.

`logs development` shows the adapter's `active` line with `entries`, and an `entry` line per further identifier.
That tells an inactive signing library from a refused bundle.

The phone's console log, where the kernel prints and launchd forwards log records, comes as structured lines with
`ios.virtual.syslog`, filtered by process, level or text:

```sh
tomaccoctl call ios.virtual.syslog '{"process":"MyApp"}'
tomaccoctl call ios.virtual.syslog '{"levels":["error","fault"],"max_lines":100}'
tomaccoctl call ios.virtual.syslog '{"process":"MyApp","offset":123456}'
```

Each line has `time`, `process`, `pid`, `service`, `sender`, `level` and `message`; a line of no known form is the kernel's.
Without `offset` it reads the last 256 KiB; pass the reply's `next_offset` back to follow the log. It reads the guest job's
`uart.log` on the host and asks the guest nothing, so it also works after the phone stopped, for the last run. The parsing
is tested against synthetic console lines; which forms a live phone prints, and how many of its records reach the console,
has not been surveyed yet.

## While it runs

| Symptom | Cause | Fix |
| --- | --- | --- |
| `text` typed the wrong characters | iOS read the keys with another layout than you typed for. | Pass `layout` (`us` or `de`), or set the guest's language. |
| `text` went nowhere | No field had the keyboard focus, or the app took it after the keys were sent. | Tap the field and check a frame, then `text`. |
| `ios_virtual_not_running` from `deploy`/`debug` | The phone is off. | `ios.virtual.configure {add_application}` then `ios.virtual.start`. |
| `deploy` fails with `spare trust-cache entries` | The boot predates hot install, or its 512 entries are used up. | Stop and start the phone. |
| The picture stands still after a while | Older guest tools let an idle phone lock and turn off its display. | Press Home or the side button. A guest that reports `auto_lock_supported` gets Auto-Lock off at start. |
| A widget is empty | The widget's extension misses chronod's deadline under emulation. | Nothing yet. This is a [known limit](README.md#honest-limits). |
| `ios_touch_lane_required` | A one-shot `tomaccoctl call` cannot hold a finger down. | Use `drag`, or `touch_down` through a client that stays connected (`tomaccoctl mcp`). |
| `ios_touch_not_down`, or a lift you did not ask for | A contact with no event for 5 seconds is lifted, and so is one whose client ended. | Send `touch_move` with the same point to keep it alive. |
| `ios_control_busy` | Another action holds the guest's control lane. It waits 2.5 s first. | Ask again. |
| `ios_control_starting` | The control monitor is not published yet. | Ask again in a moment. |
| `ios_control_ended` | The emulator has exited while its job finishes hashing the disks. | Wait for the job. Do not cancel it. |
| `ios_guest_starting` | The guest service is not up yet. | Wait, and watch `ios.virtual.status`. |
| `ios_frame_file_unavailable` | No private runtime directory exists, or an uncompressed frame would exceed 64 MiB. | Use `frame` with `transport:"inline"`. |
| The phone ended by itself, `guest_gpu_memory_stop: true` | Under an eighth of the graphics card stayed free, so the [guard](usage.md#the-graphics-memory-guard) stopped the session. | Close other graphics work and start again. |
| The phone ended by itself, `guest_gpu_memory_stop: false` | The most likely reason is the [24-hour limit](usage.md#time-and-disk-limits). `guest_saved` says whether it closed cleanly. | Start again. An unsaved run is not used as a base. |

## Stopping

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ios_guest_busy` or `ios_guest_not_idle` | The guest answers but is working. It is never ended by force. | Try again when it is done. In the desktop, **Stop without saving** ends it at once, and what it has not saved is lost. |
| "The guest did not answer and was ended without saving" | Four attempts, twenty seconds apart, got no answer. | Start again. It goes back to the last verified run. |
| "The guest stopped without verified storage" | The flush, exit code or hashes did not check out. The message goes on to say which, in words. | The same. Your last verified run is unchanged. `last_run` in `ios.virtual.status` keeps the reasons and a remedy. |
| The next start says it went back to an older run | The previous run was not verified. | This is on purpose. Stop runs with `ios.virtual.stop` to keep their changes. |
| `output_exists` | A leftover disk copy has this name. A leftover that a guest still has locked counts as in use. | Stop that guest. Only copies Tomacco made inside the profile's `installs` directory are removed. |

**Why was my run not verified?** `last_run` in `ios.virtual.status` answers for the last run that finished:

```json
{"verified": false, "outcome": "emulator-exited",
 "summary": "The emulator exited on its own with exit code 0.",
 "reasons": ["No stop asked the guest to flush its storage first: it ended on its own or was ended by force."],
 "remedy": "Stop the phone with ios.virtual.stop while it answers; a stop that has to end an unanswering guest cannot save it. The next start uses the last verified run."}
```

`outcome` is the launcher's classification of the run (for example `kernel-panic`, `guest-process-crash` or
`host-pressure-blocked`), and `summary` says it in words. A run can be verified and still name a problem: a crash of an iOS
process the emulator is not known to cause, or the graphics memory guard, is listed while the saved disks stay usable.
The wording is unit-tested against recorded launcher records, not yet read off a live phone.

## Still stuck

`ios.emulator.report` with the guest job shows the emulator's outcome, and `userspace_failures` and `respawn_storms` list
what went wrong inside iOS. An entry of kind `guard-ended` is expected. If you are still stuck, please open an issue
with the report.

`tomaccoctl report` writes one archive for that issue, on this computer only. Its `virtual-ios.json` holds this machine's
readiness from `ios.virtual.host_check`, the profile's run settings, `ios.virtual.status`, which engine and guest-tools
builds the profile uses, and how the last guest run ended (its outcome, evidence counts, and the first userspace
failures and respawn storms); `virtual-ios/guest.log` is the end of that run's log. Recognised secrets are masked and
your home directory is shown as `~`, but read the files before you share them. The archive holds no images, disks or
other files of Apple's.
