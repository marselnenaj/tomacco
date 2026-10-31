# Frequently asked questions

Short answers, each taken from a guide that goes into detail. Where something has not
been tested, the answer says so.

## What is Tomacco?

A mobile development workbench for Linux. A Qt desktop and a command-line tool,
`tomaccoctl`, share the same typed operations, so a person and a coding agent can build,
run and test Android and iOS apps the same way. See the [README](../README.md).

## Is the virtual iPhone a simulator?

No. Tomacco's virtual iOS device is an emulated phone: a patched QEMU translates every ARM instruction
of the guest in software, and it boots Apple's iOS 27 from restore images you supply,
with arm64 apps you build on Linux. It is an experimental research runtime, not a
qualified simulator, and far slower than a real phone. See
[Virtual iOS device](virtual-ios/README.md).

Tomacco can also drive the real Simulator on a Mac over SSH; see
[the Mac route](iphone.md#the-mac-route). That route has not been qualified.

## Is Android emulated too?

Android apps run in the real Android Emulator, which `tomaccoctl setup` offers to install
with an emulator called `Tomacco`, or on a phone. See [Android](android.md).

## Do I need a Mac?

Not for Android, and not for the virtual iOS device: its Image4 boot manifest is
prepared on Linux, with no Mac or Apple signing request. Building iOS apps on Linux uses
xtool and Apple's SDK imported from an `Xcode.xip` that you download from Apple
yourself; Xcode itself does not run on Linux. A Mac is only needed for the optional Mac
route (Xcode builds, XCTest, the Simulator). See [iPhone](iphone.md).

## What do I need for a real iPhone?

Swift and xtool, Apple's SDK from an Xcode archive, an Apple account, and an iPhone with
Developer Mode on that trusts the computer. Building, signing, pairing and the runner
have contract tests, but running an app on a physical iPhone has not been verified. See
[iPhone](iphone.md).

## Is this legal?

The documentation does not give legal clearance; [the legal notes](legal.md) explain
what Tomacco is for and what it changes. In short:

- No Apple firmware, iOS image or Apple binary is part of the repository or its
  packages. You supply your own images and SDK.
- The research runtime changes or bypasses some Apple protections on local copies.
  Keeping copies private or calling a use research does not by itself establish
  permission.
- Public availability of Apple's images does not establish permission to decrypt,
  modify, run or redistribute them. Check the applicable Apple terms and the law where
  you are.

## Which hardware does it need?

Tomacco runs on x86_64 Linux. Arch Linux with Omarchy is the tested platform, on one
workstation; other distributions should work but have not been qualified.

Running an Android app on the emulator needs KVM, which `tomaccoctl doctor` reports, or
a phone instead.

The virtual iOS device needs much more: a Vulkan graphics card with about 16 GiB of
memory, a fast CPU, 8 GiB of RAM for the guest plus up to 2 GiB for translated code, and
about 100 GiB of disk for setup. It was measured on an i9-13900K with a Radeon RX
6800-class card. Other machines and graphics drivers have not been qualified.

## Why does the virtual iPhone need x86_64?

The runtime requires an x86_64 host. QEMU translates the guest's ARM code
to x86_64 in software, because KVM cannot run an ARM guest on Intel. On Linux for Apple
Silicon (M1, M2, Asahi), the tools and the agent API are experimental and the virtual iOS
device is not available. See [how it works](virtual-ios/how-it-works.md).

## How do coding agents connect?

`tomaccoctl mcp` is a local MCP server over standard input and output, with no network
port and no token. `tomaccoctl setup` registers it with Claude Code and Codex; by hand:

```sh
claude mcp add --transport stdio --scope user tomacco -- "$(command -v tomaccoctl)" mcp
codex mcp add tomacco -- "$(command -v tomaccoctl)" mcp
```

Any stdio MCP client can use the same tools. Without MCP, an agent runs
`tomaccoctl call OPERATION '{...}'`, which prints one JSON result, and `tomaccoctl schema`
lists every operation. Long work runs as durable jobs that survive a disconnect. See
[Agents](agents.md) and [Recipes](recipes.md).

## Does Tomacco send data anywhere?

It sends no telemetry. [Privacy](privacy.md) lists what does use the network, such as the
update check.

## How do I update or remove it?

`tomaccoctl update` installs a newer signed release, and `tomaccoctl uninstall` removes the
installation while keeping your data. See [installing](building.md#installing).
