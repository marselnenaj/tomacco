# Set up Tomacco for your person

Instructions for a coding agent. A person gave you this page because they want Tomacco
installed and working. You do the work; they decide and sign in.

## Rules

- Say what you are about to do before anything that downloads gigabytes, accepts a license or
  needs `sudo`, and wait for a yes. Steps with `ask_first` are those.
- Only the person signs in to accounts, types passwords and supplies Apple's files.
- Never ask for a private key, password or token in the chat.
- A step that fails is reported with its error. Don't report it as done.

## 1. Install

```sh
curl -fsSL https://tomacco.dev/install.sh | bash
```

Read the script first if the person prefers that. It asks before installing system packages,
so when you have no terminal for it to ask in, install these yourself after asking the person.
On Arch: `sudo pacman -S --needed python ffmpeg openssl qt6-base qt6-declarative qt6-svg qt6-webengine`.

## 2. Ask what they want

Ask which of these they need, and skip the rest: Android apps, iOS builds on Linux for an
iPhone, the virtual iOS device (an experimental research feature).

## 3. Work through the plan

```sh
tomaccoctl doctor
```

The result has `next_steps`, in order. Each has a `goal`, `what`, `how` and `ask_first`. Do the
steps for the goals the person chose, then run `doctor` again until none are left.
The virtual iOS device's step also has `chain`: its whole setup chain in order, each stage with
`done` (`true`, `false`, or `null` when only the person's files can tell), and `next_stage`, the
first stage not done. Once a profile is set up, its steps name only what stops the next start,
such as too little disk space. Over MCP
the same plan comes from the `doctor` tool. To connect yourself over MCP, run `tomaccoctl setup`
with the person watching, or use the commands in [Agents](agents.md#connect-a-client).

## 4. Prove it works

Don't stop at installed tools. Run something real and show the person:

- **Android:** create a sample app, run it on the emulator and take a screenshot. See
  [getting started](getting-started.md#route-c-a-new-android-app).
- **iOS builds:** build a sample with xtool. See [iPhone](iphone.md).
- **Virtual iOS device:** start it, install an app and take a screenshot. See
  [usage](virtual-ios/usage.md).

Finish with what works, what you skipped, and what only the person can still do.
