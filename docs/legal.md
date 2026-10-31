# Legal notes

This document describes Tomacco's intended purposes, included materials,
Apple software prerequisites and changes to software protections. **It is not
legal advice or a legal clearance of the project.** Applicable agreements,
copyright and anti-circumvention rules depend on the software, the activity and
the jurisdiction. A disclaimer cannot settle those questions or remove
obligations imposed by law.

Licenses of the components are in
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md). Network use and stored
data are covered in [privacy.md](privacy.md).

## Purpose

Tomacco is a development workbench. Its iOS parts exist for three things:

- building, signing and testing **your own** apps on Linux, for an iPhone you
  own or for the virtual iOS device;
- **interoperability**: letting independently written programs (the Linux
  control plane, its guest tools, the Vulkan renderer and your own apps) work
  with iOS;
- **research** into how iOS behaves on emulated hardware, using system images
  you obtained yourself.

It is not meant for running iOS for someone else as a service, for
distributing Apple software, for working around App Store purchases or DRM,
or for gaining access to devices, accounts or data that are not yours.

## What is included, and what is not

**Included** (source tree and the CMake/`install.py` packages):

- Tomacco's own source code, documentation and tests (GPL-2.0-or-later).
- Source patches and small device models for third-party emulators in
  `integrations/ios-emulator/`, each under the license of the project it
  changes.
- Build and preparation recipes (`tools/*.py`). They name upstream projects by
  URL and commit and check SHA-256 digests.
- Recipes for copied Apple components (`integrations/ios-emulator/*.json`).
  They contain hashes, file offsets, single 32-bit instruction words and
  device-tree property values, nothing more.
- Linker stubs (`*.tbd`) that list the names of the public C symbols the
  example programs call, and the names of Objective-C classes and selectors
  that the guest tools look up at run time. Names like these are needed for
  interoperability. They are not Apple code.

**Not included:** Apple firmware, iOS, cloudOS or macOS images, kernels, dyld
shared caches or extracts of them, Mach-O binaries from Apple, the Xcode SDK,
Apple headers, encryption keys, signing certificates or provisioning
profiles. There are no built emulator engines either. The package test
(`tests/python/test_install.py`) fails the build when a packaged file is not
text. The documentation describes what was observed in disassembly in prose,
with short offsets, symbol names and log lines. It does not reproduce Apple's
code. Binaries and full disassembly stay in ignored local directories
(`.tooling/`, `references/`), which are never committed or packaged.

## What you supply

Everything that comes from Apple, you have to provide yourself:

- **The virtual iOS device's restore image**: the vphone600 research firmware
  components (Apple's Private Cloud Compute research environment, cloudOS
  23E5207q in the measured setup) and the iPhone userland of an iOS 27 restore
  image (IPSW).
- **The Xcode SDK** for building iOS apps on Linux (`ios.sdk_install` and
  `ios.toolkit_install` take an `Xcode.xip` that you downloaded from Apple. No
  operation downloads it, or Apple's iOS updates, for you).
- **Apple ID or App Store Connect credentials** for signing and upload. They
  are entered into xtool, Xcode or Apple's tools, never into Tomacco.

Obtain these only from Apple or in another lawful way, and only for uses that
the license you got them under permits. Tomacco cannot give you any
right to Apple's software. Be aware of the following:

- Apple's [iOS 27 and iPadOS 27 software license agreement](https://www.apple.com/legal/sla/docs/iOS27_iPadOS27.pdf),
  sections 2(a), 2(b) and 2(d), allows iOS to be used on one
  Apple-branded device. It prohibits copying, decompiling, reverse engineering,
  decryption and modification, subject to its stated exceptions, including
  applicable law and licenses for open-source components.
- The Xcode and Apple SDKs agreement allows the SDK to be used on
  Apple-branded computers.
- Apple publishes the Private Cloud Compute research environment and its
  images for security research on Apple hardware, under its own terms.
  Its [VRE setup documentation](https://security.apple.com/documentation/private-cloud-compute/vresetup)
  says use is limited to research under the applicable agreement, and commercial
  use is not permitted. The agreement can be reviewed with `pccvre license` on
  Apple's supported host; the source-code repository has its own limited-use
  license, which must not be treated as a license for every PCC binary.
  Apple provides that Virtual Research Environment for inspecting and testing
  Private Cloud Compute on Apple silicon Macs. Running its components on
  Linux, or using them to boot an iPhone userland, is outside what it is
  offered for.
- Downloading an IPSW or a research image does not make you the owner of the
  software in it. Apple licenses that software; the copy you hold is
  governed by the license terms above.

Running these images in QEMU on a Linux PC, or using the Xcode SDK on Linux,
may therefore fall outside Apple's license terms. Whether such a term can be
enforced against you depends on where you live and how you use the software.
For example, Articles 6 and 8 of
[Directive 2009/24/EC](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32009L0024)
provide conditional protection for specified interoperability activities; they
do not establish that this runtime or every documented use meets those conditions.
Check the applicable terms and legal requirements before using the iOS features.

## Download sources and permission

The [image preparation guide](virtual-ios/setup.md#obtain-the-apple-images)
records how the measured builds were located. **ipsw.me** is the iPhone firmware
catalog; **Lakr233/vphone-cli** records the PCC `cloud264` URL in its source.
Both recorded image URLs point to Apple's `updates.cdn-apple.com` CDN. The
independent **blacktop/ipsw** tool can look up, download and prepare files; using
it does not grant any rights to those files. Tomacco provides no firmware mirror.

These facts establish download provenance and technical compatibility, not a
legal entitlement to download, decrypt, modify, run or redistribute Apple
software. A public URL, an Apple-signed image, owning an iPhone or Mac, or calling
an activity "research" does not by itself establish permission for this Linux
runtime. Any applicable statutory exception has its own conditions. Read the
terms that accompany your particular software before downloading or using it;
[Apple's license directory](https://www.apple.com/legal/sla/) notes that the
agreement supplied with a product may differ from its currently published copy.

The instructions do not claim Apple authorization for Tomacco or general iOS
app development under the PCC research terms. Images, decrypted components,
keys and boot tickets stay outside Tomacco's repository and release packages.

Source attribution, keeping Apple images out of packages and stating an
interoperability purpose do not by themselves resolve licensing, copyright or
anti-circumvention issues for users or maintainers. Before distributing or
relying on the iOS runtime, obtain qualified legal advice covering its
implementation, protection changes, documentation, packaging and intended use
in the relevant jurisdictions. This notice does not promise that those
activities are lawful or that any party is protected from a claim.

## Parts that change Apple's protections

The research runtime does not use any Apple signing or activation server and
does not create valid Apple signatures, certificates or provisioning profiles. To run your
own code in the guest, it changes or bypasses the following, **always on
private copies of images you supplied** and only with explicit options:

| Part | What it does | Where |
| --- | --- | --- |
| Secure boot chain | The engine starts the supplied kernelcache and SEP firmware directly. Apple's boot ROM, LLB and iBoot do not run, so the signature and personalization checks they would apply to those images are not applied. | jprx/qemu-sptm (`hw/arm/xnuboot_sptm.c`), `vphone-loader.patch`, `vphone-sep-loader.patch` |
| Research and development device identity | `ios.emulator.prepare_guest_tools` sets `/chosen/sec-research-device-erm-enabled` in the copied device tree. With `development_device: true` it also sets the certificate, AP and SEP security-mode properties to a non-production, development identity (it refuses a production identity). The unchanged MobileActivation daemon then takes its own local development-device path and reports the device as Activated. Apple's activation service is not contacted, and no daemon, response or activation record is patched. | `tools/ios_guest_tools.py` (`virtual_research_mode`), `tools/prepare_ios_guest_tools.py`, `guest-setup.h` |
| Development-signing adapter | Lets the guest's installer accept ad-hoc signatures, but only for the bundle identifiers and CodeDirectory hashes compiled into the guest tools for your own apps. | `guest-development-signing.c` |
| Entitlements | Guest tools and your apps are signed ad hoc, with entitlements taken from an allowlist. The guest tools' list includes Apple-private entitlements, such as `com.apple.private.security.disk-device-access`, `com.apple.private.mobileinstall.allowedSPI` and `com.apple.private.xpc.service-attach`. Your apps may only ask for the application and team identifier, `get-task-allow`, keychain access groups and app groups; Apple-private and platform entitlements are refused. | `storage-entitlements.plist`, `tools/build_ios_guest_tools.py`, `tools/ios_application_bundle.py` |
| Trust-cache extension | Adds the CDHashes of your own guest tools and apps to a trust cache on the private disk copy. | `build_ios_research_bootstrap.py`, `build_ios_guest_tools.py` |
| Kernel and daemon patches on copies | A few hash-pinned instruction words in a copied kernel or daemon. `ios-gpu-mmap.S` overrides Sandbox's `file_check_mmap` EPERM decision, and so allows an executable mapping, for a short list of pinned CDHashes (the Metal driver, its compiler and Tomacco's own adapters). Other patches pin the signing adapter and add workloop and fileport compatibility, RAM-disk byte counts and a restore format retry. The originals are never modified. | `ios-gpu-mmap.S`, `prepare_ios_adapter_pin.py`, `prepare_ios_*_compat.py`, `ios27-*.json`, `restore-*.S` |
| Seal metadata pins | A research RAM disk accepts two specific system-volume Image4 containers by their SHA-384 hash, without checking Apple's signature, and says so in its log. Everything else still goes to the original verifier. | `restore-seal-pins.*`, `cryptex-seal-pins.S`, `ios_paravirt_seal.py` |
| Device-tree and boot-manifest handling | Adjusts properties of a copied device tree, for example SEP loading and memory layout. Repackages existing Image4 payloads with a supplied local manifest and changes only the four-byte payload type. The packager copies that manifest unchanged. | `prepare_ios_paravirt.py`, `ios_paravirt_firmware.py` |
| Local research manifest preparation | Reads a separately obtained public qemu-t8030/Inferno ticket template, sets local hardware declarations and firmware digests, and writes a new IM4M. The inherited signature is invalidated. No Apple signing request is made and no valid Apple ticket is created. | `prepare_ios_research_manifest.py` |
| Pointer authentication | The SPTM engine does not enforce ARM pointer authentication; Tomacco's `qemu-arm-pauth-bypass-inline.patch` keeps that behaviour and makes it faster. PAC is a CPU exploit mitigation, not a copy or access control. | jprx/qemu-sptm, `qemu-arm-pauth-bypass-inline.patch` |
| Branch target identification | With the boot option `bti: off`, which `tools/ios_virtual_profile.py` sets in every new profile, the emulated CPU reports no FEAT_BTI, so no guest code gets this protection, for speed. Like PAC, BTI is a CPU exploit mitigation, not a copy or access control. | `qemu-arm-bti-property.patch`, `tools/run_ios_paravirt.py` (`--bti`) |

The measured private setup used a locally modified IM4M research manifest. Its
changed declarations invalidated the inherited Apple signature. A file accepted by
this research runtime is therefore not evidence of an Apple-signed authorization
for that VM. The [setup instructions](virtual-ios/setup.md#how-do-i-get-the-im4m)
reproduce this local preparation with a separately downloaded public template.
That template is third-party research material, not one of the Apple-hosted
archive downloads, and is not included in Tomacco's repository or packages.

Nothing is decrypted: no GID or UID keys, no FairPlay, no encrypted payloads.
Tomacco contains no decryption of Apple content.
`ios.emulator.prepare_userland` requires an already decrypted disk image and
refuses an encrypted (AEA) archive. The emulated AES engine advertises its
hardware keys as unavailable; iOS's own data protection inside the guest uses
the software keys iOS creates itself.

None of these parts touch a physical iPhone, remove protection from purchased
apps or content, or give access to anyone else's device or data.

**Guest networking.** A boot does not reach the network unless its `guest_network`
setting is `nat`, which is off by default. That setting attaches one virtio network
device behind [passt](https://passt.top), which gives the guest outbound access through
the host's own network, with no inbound port opened and the host's loopback services kept
away (`map-host-loopback=none`). Because the guest's own protections are off (pointer
authentication is not enforced, ad-hoc code runs), a networked guest is real internet
exposure of an untrusted guest: keep it off unless an app under test needs the network,
and do not run code in it that you do not trust.

## Legal background

**Emulation as such.** Writing an emulator, and the reverse engineering
needed to make independent programs work with a platform, has repeatedly
been held lawful: *Sega v. Accolade*, 977 F.2d 1510 (9th Cir. 1992);
*Sony v. Connectix*, 203 F.3d 596 (9th Cir. 2000); *Sony v. Bleem*, 214 F.3d
1022 (9th Cir. 2000). In the EU, a program's functionality, programming
language and data formats are not protected as such (CJEU, C-406/10 *SAS
Institute*, 2012), and Directive 2009/24/EC allows a lawful user to observe
and test a program (Article 5(3)) and to decompile it where that is
indispensable for interoperability (Article 6); in Germany §§ 69d(3) and 69e
UrhG, in Austria §§ 40d and 40e UrhG. These authorities may be relevant to
independently written emulator code; their application depends on the facts
and their specific conditions. They do not establish permission to use Apple's
software or settle whether the protection changes listed above are allowed.

Measures like those changes can fall under anti-circumvention rules even when
the copyright use itself is lawful:

- **United States: DMCA, 17 U.S.C. § 1201.** The law forbids circumventing
  technological measures that control access to copyrighted works, and it
  forbids "trafficking" in tools that are primarily designed for that. There
  are exceptions for reverse engineering for interoperability (§ 1201(f)), for
  security testing (§ 1201(j)), and the Copyright Office's triennial exemptions
  (37 C.F.R. § 201.40), for example for good-faith security research and for
  jailbreaking smartphones to run lawfully obtained apps. These exemptions are
  narrow, and most of them cover only the act itself, not distributing tools.
  § 1201(f)(3) allows making circumvention means available to others only
  "solely for the purpose" of enabling interoperability of an independently
  created program; a general development tool with other uses may not meet
  that test.
  - *Apple v. Corellium* (S.D. Fla. 2020, affirmed by the 11th Cir. 2023)
    held that a virtualised iOS for security research was fair use. Apple's
    § 1201 claim in that case was settled and not decided. A general
    development tool is in a less clear position than a pure security
    research tool.
  - In *Apple v. Psystar*, 658 F.3d 1150 (9th Cir. 2011), the court
    enforced Apple's license term limiting macOS to Apple hardware and
    rejected the copyright-misuse defence. The district court had already
    held that circumventing the encryption protecting macOS on non-Apple
    hardware violated § 1201 (*Apple v. Psystar*, 673 F. Supp. 2d 931
    (N.D. Cal. 2009)). *MDY v. Blizzard*, 629 F.3d 928 (9th Cir. 2010), held
    that a § 1201(a)(2) claim needs no link to copyright infringement.
  - *Lexmark v. Static Control*, 387 F.3d 522 (6th Cir. 2004),
    held that an authentication check which leaves the program itself freely
    readable is not an effective access control to that program.
- **European Union, Germany and Austria.** The information obtained by
  decompilation may not be passed on except where necessary for
  interoperability, and may not be used to build a substantially similar
  program. Article 7(1)(c) of Directive 2009/24/EC asks member states to act
  against putting into circulation means whose *sole intended purpose* is to
  remove or circumvent technical protection of programs. Directive 2001/29/EC
  Article 6 protects technical measures on other works, such as media and
  fonts in a system image. The CJEU held in C-355/12 *Nintendo v PC Box*
  (2014) that such protection must be proportionate, taking into account
  whether a device has commercially significant purposes other than
  circumvention.
  - Germany: § 69f(2) UrhG covers means intended solely to remove or
    circumvent the protection of programs; §§ 95a ff. UrhG cover other works
    and do not apply to programs (§ 69a(5) UrhG).
  - Austria: § 90b UrhG covers means for circumventing the protection of
    programs and § 90c UrhG technical measures on other works.
- **Apple's license terms** (see [What you supply](#what-you-supply)) are a
  matter of contract between you and Apple. They apply in addition to the
  rules above.

The project's position is that its iOS parts serve interoperability and
research, operate only on images the user supplies, and contain no Apple code.
This repository contains no links to third-party copies of Apple firmware.

## Your responsibilities

- Use Tomacco only with software, devices, accounts and data that you
  are entitled to use.
- Obtain Apple software lawfully and stay within the terms it was licensed
  under, as far as they are enforceable where you live.
- Do not distribute images, firmware, SDK files, keys or binaries extracted
  from Apple software. That includes prepared profiles, disk copies,
  screenshots of Apple's UI used beyond what the law allows, and engine
  builds that embed any of it.
- If you redistribute Tomacco or an engine built with its recipes, meet
  the licenses in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
- Keep your store and signing credentials safe (see [privacy.md](privacy.md)).

## No warranty

Tomacco is distributed in the hope that it will be useful, but **without
any warranty**, without even the implied warranty of merchantability or
fitness for a particular purpose (GNU GPL, version 2, sections 11 and 12, and
the corresponding sections of later versions). The virtual iOS device is
an experimental research runtime. It can lose data in its disk copies and is not
qualified for production use.

## Trademarks

Tomacco is an independent project. It is **not affiliated with, endorsed
by or sponsored by Apple Inc., Google LLC, Samsung Electronics, Meta
Platforms, Genymobile, Appium or any other company** whose products it works
with. Tomacco is independent of the Omarchy project.

Apple, iPhone, iPad, iOS, iPadOS, macOS, Xcode, Swift, Metal, SpringBoard,
Dynamic Island, App Store, App Store Connect, TestFlight and Apple silicon are
trademarks of
Apple Inc., registered in the U.S. and other countries. "IOS" is a trademark
of Cisco and is used by Apple under license. Android, Google Play, Pixel,
Android Studio and Flutter are trademarks of Google LLC. Galaxy is a
trademark of Samsung Electronics Co., Ltd. QEMU is a trademark of Fabrice
Bellard. Other names belong to their respective owners.

These names are used only to say which platforms, devices and tools Tomacco
 works with. Device profiles are named after the phones whose screen
sizes they reproduce. Their frames are simple drawn shapes, not
manufacturers' artwork. The Tomacco icon is original artwork. The
virtual iOS device shows the user interface of the iOS image the user
supplied.
Tomacco does not ship that interface.
