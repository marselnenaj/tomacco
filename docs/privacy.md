# Privacy

Tomacco runs on your machine and has no account, server or cloud
service. **It sends no telemetry, usage statistics or crash reports. It connects to the
network when you start an action that needs it, and, unless you turn it off, once
a day to ask GitHub for the latest release.** The sections below list
those actions, what is sent, what stays on your disk and how credentials are handled.

## What leaves your machine, and when

| Action | Destination | What is sent |
| --- | --- | --- |
| Update check (`update.check`), once a day with the desktop open in the default `notify` mode and in `auto`; in `off` only when you press **Check now** | `api.github.com` | An ordinary request for the latest release. It names no version, device or account of yours; GitHub sees your IP address and that Tomacco's updater asked. With a token you configured, that token. |
| Update download (`update.apply`), when you install or in `auto` mode | `github.com`, and the GitHub storage hosts it redirects to (`objects.githubusercontent.com`, `release-assets.githubusercontent.com`); `api.github.com` with a token | Ordinary download requests for the package and its signature. The token goes only to `api.github.com`. |
| Google Play edits (`play.prepare`, `play.commit` and related operations) | `oauth2.googleapis.com`, `androidpublisher.googleapis.com` (only these two; other endpoints are refused) | A token request signed with your service-account key, then the edit, track data and the AAB you chose |
| App Store build and upload from Linux (`ios.store_ship`) | App Store Connect (`api.appstoreconnect.apple.com`), only with a key you pass | A token signed with your `.p8`, the certificate and profile requests, and with `upload` the IPA. The `.p8` is not sent. |
| iOS toolkit (`ios.toolkit_install`) | `github.com` (the pinned checkout; with `toolchain`, xtool's fork, OpenAppleMacros, rcodesign, ipsw and xtool's toolset), PyPI (`pymobiledevice3`) | Git fetches and downloads. Nothing goes to Apple. |
| Flutter for iOS (`ios.flutter_setup`) | `chromium.googlesource.com`, `dart.googlesource.com`, Google storage and Flutter's artifact host | Git fetches and downloads of about 7 GB. Some downloaded tools then run on your machine. |
| App Store validation and upload over the Mac route (`apple.store`) | Your Mac over SSH, then Apple through `xcrun altool` on the Mac | The IPA, your App Store Connect key ID and issuer ID. The private key stays on the Mac. |
| Other Mac-route operations (`apple.*`) | Your Mac, over `ssh` and `rsync` | The project directory, without `.git`, `.env*`, `.tooling`, `.gradle`, `node_modules`, `build`, `.dart_tool` and `.tomacco`. Build, test and profiling results come back. |
| iPhone sign-in and signing (`ios.prepare action=login`, xtool builds) | Apple, through xtool | Your Apple ID goes from xtool's terminal prompt directly to Apple. Tomacco never sees it. |
| WebDriverAgent runner (`ios.wda_prepare`) | `github.com` (Appium release download) | An ordinary download request |
| Swift SDK install from a URL (`swift.sdk_install`) | The HTTPS URL you give | An ordinary download request |
| iOS engine and preparation recipes (`tools/build_ios_*.py`, `tools/prepare_ios_*.py`) | GitHub, the Alpine CDN, PyPI (Meson), and an Apple IPSW URL if you pass `--ipsw-url` | Git clones and downloads at pinned versions |
| Web preview / layout lab | The address you enter | Whatever the page requests, as in a browser |
| Help and download links in the desktop (Apple's Xcode download page on `developer.apple.com`, Swift's install page on `swift.org`, the About dialog's link to the source on `github.com`) | Opened in your default web browser, not in Tomacco | Whatever your browser sends when it opens a page |
| Toolchains you run through Tomacco (Gradle, Flutter, Android SDK manager, `adb`, the Android Emulator, Swift, xtool) | Their own services | Whatever those tools send on their own |

Local connections do not leave the machine: the desktop's preview server
listens only on `127.0.0.1`, the desktop and `tomaccoctl` talk over a Unix
socket, and the MCP server uses stdio. The virtual iOS device has no network
device unless its `guest_network` setting is `nat`.

**AI agents.** If you connect an MCP client (Claude Code, Codex or another),
everything an operation returns goes to that client: logs, file contents,
screenshots and device pictures. The client may send it on to its AI
provider. Tomacco does not filter it. Only connect clients you trust
with the project.

## What is stored locally

| Location | Contents |
| --- | --- |
| `~/.local/share/tomacco/` (or `$TOMACCO_HOME`) | Job database, job logs and artifacts, `settings.json`, custom device profiles, saved workspaces, `remotes.json` (SSH host and directory of your Mac), debug sessions, reports, `desktop.log`, Android emulator data, virtual iOS device state and its disk copies, install backups |
| `~/.local/share/tomacco/apple-dev/` | The iOS toolkit: the pinned checkout and a private home with its xtool, SDK, Swift copy, Python environment and caches. If you use `ios.store_ship` with a key, the Apple Distribution private key it creates is in `home/.config/omarchy-apple-dev/distribution/`. |
| `~/.local/share/tomacco/ios-emulator/…` and your profile directories | Disk copies of the iOS image you supplied, 32 GiB each, at most two kept. They contain whatever you did in the virtual iOS device. |
| `~/.local/share/tomacco/updates/` | The last update check and installation, and a copy of the installer that made the last update, for rolling it back |
| `~/.config/marselnenaj/Tomacco.conf` | Desktop preferences, such as the interface language |
| `~/.local/share/marselnenaj/Tomacco/QtWebEngine/Tomacco/` | Cookies, cache and local storage of the pages you opened in the web preview |
| `~/.cache/marselnenaj/Tomacco/` | The web preview's HTTP cache and Qt's compiled interface and shader caches |
| `~/Pictures/Tomacco/` | Screenshots you saved |

Updates can be turned off in **Settings** or with `settings.configure`; then Tomacco makes no
update request at all unless you ask. `TOMACCO_UPDATES=off` in the environment, or a build with
`-DTOMACCO_DISABLE_UPDATES=ON`, turns even that off.

Nothing is encrypted by Tomacco. Keep your home directory protected.
Delete the directories above to remove all Tomacco data.

## Credentials

Tomacco does not store passwords, tokens or private keys of its own.
They stay in files and tools that you manage:

- **Google Play service-account key.** You pass the path to the JSON file.
  Tomacco refuses the file unless only its owner can read it. For each
  job, the key is copied into a private (`0700`) scratch directory, used by
  `openssl` to sign the token request, and deleted when the job ends. An
  abandoned job's scratch directory is deleted when the job is recovered. The
  access token goes to `curl` in a header file, not on the command line.
  Neither the key nor the token is written to job records or logs.
- **Android keystore passwords.** You pass password files, which must also be
  readable only by their owner. `keytool` and `apksigner` receive them as
  file references, never as command-line text. They are not logged.
- **Apple ID.** You type it only into xtool's own prompt, in a terminal window
  that Tomacco opens. xtool keeps the session in its own storage.
  Tomacco reads only whether you are signed in. It discards the output
  of `xtool auth status`.
- **App Store Connect API key.** The key ID and issuer ID are command
  arguments, so they appear in the job record. The `.p8` private key stays in
  the directory on your Mac that you name.
- **App Store Connect API key on Linux (`ios.store_ship`).** You pass the path of
  the `.p8` file, which must be readable only by its owner. Tomacco reads none of
  its content; only its path, the key ID and the issuer ID go to the job, so those
  three are in the job record. Upstream's `asc.py` reads the file to sign a token,
  which it sends in a header and does not print. Both the certificate request it
  makes and the Apple Distribution key it creates stay on this machine.
- **GitHub token for updates (optional).** You pass the path of a file, which must be
  readable only by its owner, as `updates.token_file` in `settings.configure`. Only the
  path is stored. The token goes to `curl` on standard input, not on the command line,
  and is not written to job records or logs.
- **SSH.** The Mac route uses your SSH configuration and agent in batch mode.
  Tomacco never asks for or stores an SSH password.

Job logs contain the output of the tools a job runs, such as Gradle, xcodebuild
or the guest console. Tomacco does not filter that output. Job records,
receipts and reports also contain absolute paths on your machine, which
include your user name (for example `/home/<name>/…`), and engine build
receipts record the host's kernel version and CPU model. Check logs and
receipts before you share them.
