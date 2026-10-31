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

`logs development` shows the adapter's `active` line with `entries`, and an `entry` line per further identifier.
That tells an inactive signing library from a refused bundle.

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
| "The guest stopped without verified storage" | The flush, exit code or hashes did not check out. | The same. Your last verified run is unchanged. |
| The next start says it went back to an older run | The previous run was not verified. | This is on purpose. Stop runs with `ios.virtual.stop` to keep their changes. |
| `output_exists` | A leftover disk copy has this name. A leftover that a guest still has locked counts as in use. | Stop that guest. Only copies Tomacco made inside the profile's `installs` directory are removed. |

## Still stuck

`ios.emulator.report` with the guest job shows the emulator's outcome, and `userspace_failures` and `respawn_storms` list
what went wrong inside iOS. An entry of kind `guard-ended` is expected. If you are still stuck, please open an issue
with the report.
