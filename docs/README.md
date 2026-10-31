# Tomacco documentation

Tomacco is a mobile development workbench for
Linux. A Qt desktop and a command-line tool, `tomaccoctl`, share the same typed
operations, so a person and a coding agent can do the same things.

## What do you want to do?

| I want to... | Read |
| --- | --- |
| Install Tomacco and get a first success | [Getting started](getting-started.md) |
| Have my coding agent set everything up | [Agent setup](agent-setup.md) |
| Connect a coding agent over MCP or the JSON CLI | [Agents](agents.md) |
| Copy a short sequence: build and run, screenshot and UI test, iOS build, virtual iPhone deploy | [Recipes](recipes.md) |
| Look up one operation: its arguments, defaults, annotations and an example call | [Operation reference](reference/README.md) |
| Use the desktop app: device window, shortcuts, Omarchy panel, layout lab | [Desktop](desktop.md) |
| Build and run an Android app, Swift on Android included | [Android](android.md) |
| Build, run and test a ready-made example app | [Examples](../examples/README.md) |
| Run an app on an iPhone over USB, or use a Mac | [iPhone](iphone.md) |
| Know what the experimental virtual iOS device is and what it needs | [Virtual iOS device](virtual-ios/README.md) |
| Find the two Apple restore images and prepare their disk-image inputs | [Virtual iOS: image preparation](virtual-ios/setup.md#obtain-the-apple-images) |
| Build the virtual iOS device from my own restore image | [Virtual iOS: setup](virtual-ios/setup.md) |
| Start, stop and put my apps on the virtual iOS device | [Virtual iOS: usage](virtual-ios/usage.md) |
| Fix something on the virtual iOS device | [Virtual iOS: troubleshooting](virtual-ios/troubleshooting.md) |
| Fix something anywhere else | [Troubleshooting](troubleshooting.md) |
| Change the code | [Architecture](architecture.md) and [Building and testing](building.md) |
| Understand the virtual machine, renderer and guest tools | [Virtual iOS: how it works](virtual-ios/how-it-works.md) |
| Check what may be done with Apple software, and what is changed | [Legal notes](legal.md) |
| See what leaves my machine | [Privacy](privacy.md) |
| Get short answers: simulator or not, do I need a Mac, legality, hardware | [FAQ](faq.md) |
| See what this release is, and its limits | [Release notes](RELEASE_NOTES.md) |
| Run my own programs in a restore environment, or study PMP firmware | [Research engines](research-engines.md) |

Licenses are in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md), and
[SOURCE.md.in](SOURCE.md.in) is the template of the notice that says where the
source of a distributed binary is offered.

These guides describe what the current code does, and a test checks each
`tomaccoctl call` they show against the schema.
