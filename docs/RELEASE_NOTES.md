# Tomacco 1.4.1

This maintenance release fixes blank AirPlay views when joining a running mirror and
reading large Android Baseline Profiles. It follows [Tomacco 1.4.0](https://github.com/marselnenaj/tomacco/releases/tag/v1.4.0).

## AirPlay

- Opening another preview preserves an unchanged video configuration and the full cached
  start picture, including multi-slice keyframes. Replaying a longer cached sequence no
  longer silently drops its tail when the live queue fills.
- The video cache stays bounded. If no usable start picture is available, the desktop
  explains that it is waiting and offers receiver controls. Reconnect Screen Mirroring
  on the iPhone to send a fresh start picture.
- The device picker includes the Wi-Fi receiver. Start and restart show their progress;
  restarting waits for the previous receiver to stop before opening the ports again.
- Auxiliary connection closes no longer imply that the video mirror stopped. Receiver
  failures preserve the actual UxPlay/GStreamer error, including mixed-case error messages.

AirPlay still needs UxPlay 1.73 or newer and the [documented network setup](iphone.md#over-wi-fi).
Bluetooth input needs pairing and AssistiveTouch. These fixes do not make a disconnected
phone reconnect by itself, and a stream without a new keyframe can still require a
Screen Mirroring reconnect on the phone.

## Baseline Profiles

A generated profile can be larger than a Gradle build script. Profile summary, discovery
and installation now share a separate bounded reader of up to 64 MiB. Gradle build files
retain their existing 1 MiB limit. Invalid text, read errors and oversized profiles are
reported directly instead of being described as a missing profile.

The corrected CLI read the retained real Gradle artifact of **1,449,946 bytes and 14,009
rules** (10,795 methods and 3,214 classes). This verifies the reader against that artifact;
it is not a new end-to-end Gradle generation run or a measured startup-speed improvement.

## Private USB tools

The virtual bridge now reports the same canonical device ID to libimobiledevice and go-ios,
while retaining compatible pair-record aliases. Tomacco adapts the private socket address
only in go-ios child processes, including jobs, WDA, syslog and startup measurement. Other
tools retain their existing environment and the guest's actual USB descriptor is unchanged.

Normal-boot enumeration and `ios.apps` were checked over a private socket with both tool
families agreeing on the device ID. Socket paths with literal spaces, `%`, `#` and `?` were
also checked with the installed go-ios parser. See [private USB usage](virtual-ios/usage.md#standard-tools-over-the-private-usb-bridge).

## Validation and limits

Regression tests cover repeated codec configuration, multi-slice keyframes, long video
replay, receiver errors, desktop restart sequencing and large/invalid profile files.
A synthetic H.264 stream with multi-slice keyframes reproduced the failure in the released
1.4.0 CLI: a late viewer decoded no frames. The corrected CLI replayed and decoded all
180 frames without decoder errors. This is a socket/decoder regression check, not a
recording of a physical iPhone.

The [1.4.0 requirements and remaining limits](https://github.com/marselnenaj/tomacco/blob/v1.4.0/docs/RELEASE_NOTES.md#requirements-and-limits)
still apply, with one additional USB qualification: the normal-boot private bridge was
checked with standard enumeration, existing pairing, a paired disk-information query and
installation of one owned HelloVirtual test app through xtool. The installer automatically
re-signed and provisioned it with the configured Apple development identity; this was not
an offline-signing test. Standard USB debugserver launch remains unqualified after
`DebugserverClient.Error.unknown`. This does not qualify arbitrary applications or live
store submission. A new physical iPhone recording remains a separate hardware check.

## Install and upgrade

Existing installations offer the update in Settings. You can also run `tomaccoctl update`.
Every replaced file is backed up; `update.rollback` restores the previous installation.
