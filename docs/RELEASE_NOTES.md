# Tomacco 1.4.0

Tomacco builds, runs and tests Android and iOS apps from Linux. This release adds live
screen mirroring and Bluetooth control for a physical iPhone, fixes its USB live view, and
reduces the virtual iPhone engine's CPU use. The [changelog](../CHANGELOG.md) lists every change.

## Highlights

- **An iPhone over Wi-Fi.** `runtime.start` with `device` set to `airplay` starts a UxPlay
  receiver named Tomacco. Start Screen Mirroring on the phone and its floating device window
  opens automatically; it closes when mirroring stops. The Omarchy panel shows the receiver
  as live while a phone is mirroring. An idle desktop also picks up a phone that was already
  mirroring when the app opened; stop/start changes update the device window.
- **Control over Bluetooth.** Pair the phone with Tomacco and turn on AssistiveTouch to use
  the desktop's mouse and keyboard. An absolute pointer follows the mouse; clicks tap, drags
  swipe, and text, Home and the side buttons work. The pointer was checked on an iPhone Air
  running iOS 27.2. See [the setup steps](iphone.md#over-wi-fi), including the firewall ports.
- **USB live view works again.** WebDriverAgent's MJPEG stream no longer stops after about a
  second with “Missing or oversized MJPEG frame”. The corrected stream ran at about 18–20
  frames per second at full resolution on an iPhone Air. Tomacco asks for up to 60 fps and
  removes WebDriverAgent's animation and idle waits before input.
- **The right frame and a pin.** Physical iPhones use their hardware identifier to choose the
  matching device frame. The floating device window has a pin button to keep it above other
  windows, including under Hyprland.
- **Desktop shutdown.** A window can close with preview requests still pending without a
  socket teardown crash. Its control-program children are cleaned up before the window exits.
- **Less CPU for the virtual iPhone.** Translated-code lookups survive TLB changes that do not
  affect them. During Home-screen swipes, misses fell from 6.9 to 3.8 million a second; engine
  CPU time over the measurement workload fell from 475 to 433 seconds. Launch, tap and swipe
  timings changed less than their spread.
- **The virtual iPhone and standard USB tools.** The USB bridge now finds the device during a
  normal boot and keeps its own pair records. libimobiledevice and xtool can see it through
  the bridge's private socket; `idevicepair pair` pairs after Trust is tapped on the phone.
- **Documentation.** The online guides have diagrams, a revised topic index, and tables and
  code blocks that fit smaller screens.

## Requirements and limits

- Wi-Fi mirroring needs **UxPlay 1.73 or newer** and the AirPlay firewall ports described in
  [the iPhone guide](iphone.md#over-wi-fi). Bluetooth input needs BlueZ and a Bluetooth adapter.
  These are optional dependencies; Tomacco does not install them automatically.
- After upgrading an earlier Bluetooth-control development build, forget Tomacco in the
  phone's Bluetooth settings and pair again so iOS reads the new pointer descriptor.
- The new Bluetooth stack uses the maintained `zbus` 6 libraries. The Rust build minimum
  remains 1.89.
- **60 fps over USB is a request, not a measured result.** The observed USB stream was 18–20 fps.
  The effect of the input changes on USB tap latency has not been measured on the phone.
- The pin button has automated coverage; keeping the window above others has not yet been
  confirmed interactively on the physical iPhone setup.
- Installing an app through the virtual iPhone's USB bridge was not tried: xtool asks for an
  Apple ID first. Use the bridge's private `USBMUXD_SOCKET_ADDRESS`, not the host's USB socket.
- The [previous release's limits](https://github.com/marselnenaj/tomacco/blob/v1.3.0/docs/RELEASE_NOTES.md#limits)
  still apply except where the physical-iPhone checks above supersede them.

## Install and upgrade

```sh
curl -fsSL https://tomacco.dev/install.sh | bash
```

Existing installations offer the update in Settings. You can also run `tomaccoctl update`.
Every replaced file is backed up; `update.rollback` restores the previous installation.
