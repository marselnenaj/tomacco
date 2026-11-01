# Set up the virtual iOS device

From your own restore images to the first start. Read [what it needs](README.md#what-it-needs)
first.

**Before you begin.** This chain has not been run as one sequence on a truly fresh machine.
[What has been checked](#what-has-been-checked) is at the end. With all inputs already
available, budget about 100 GiB of disk and half an hour for the restore itself.

**Can this machine run it?** This checks the processor, `/dev/kvm`, memory, free disk, the
Vulkan driver and graphics memory, the helper kernel and `passt`, and with a configured profile
whether its overlay starts find `qemu-img` (beside the helper engine or on `PATH`), each as pass,
warn or fail with what to do. Pass the directory your chain's outputs will go to; it reads and writes nothing else.

```sh
tomaccoctl call ios.virtual.host_check '{"directory":"/phone"}'
```

On the development host it passed everything but `passt`, which was not installed. Its
thresholds come from [what it needs](README.md#what-it-needs); a pass is not a qualification.

Once a profile is configured, its `recommend` list compares the profile's boot settings with this host (`host` has its
logical processors, physical cores and memory): `host_cpu_policy` `performance` when there are at least two physical
cores more than the six application processors, `scheduler` when there are fewer cores than processors, and
`translation_cache_mib` 1024 under 16 GiB of memory or 2048 from 32 GiB. Each entry has the `setting`, its `current`
and `suggested` value and the `reason`. It changes nothing; edit `profile.json` yourself. The rules have unit tests;
whether a changed setting makes a live phone faster has not been measured.

**See where you stand at any time.** This lists inputs that are missing or wrong, in
chain order. The JSON has the missing inputs under `missing`. Prepare the local
IM4M with the command in step 4 before proceeding to the restore runtime.

```sh
python3 tools/ios_virtual_profile.py --check
```

**Check your own files first.** Before any long step, this looks at the images, the template and
the IM4M you have so far and says in plain words what is wrong: another build than the one the
chain needs, the two directories swapped, a component the unpacking left out, an image still
encrypted or not APFS, a template other than the documented one, or an IM4M made for another
iPhone image or another prepared device. Leave out what you do not have yet. It reads manifests
and file headers only, so it takes moments; the import checks the large images' digests later.
It exits with 1 when something needs attention, and its JSON lists each input under `checks`
with `status` `pass`, `warn`, `fail` or `skipped`, the `problem` and the `fix`.

```sh
python3 tools/check_ios_inputs.py --iphone-firmware /phone/iphone-ipsw --pcc-firmware /phone/pcc \
  --template /phone/ticket.shsh2 --manifest /phone/your.im4m --device-tree /phone/dtree.restore
```

The decrypted images are looked for where step 4 below puts them, under `decrypted/` in each
image directory; `--iphone-os-image`, `--iphone-systemos-image` and `--pcc-system-image` name
others. This check has unit tests with synthetic files only.

## Guided setup

`ios.virtual.setup` walks the chain below for you, one stage at a time. Give it a `work`
directory for the outputs and, as you get them, your own files. `plan` (the default) lists the
17 stages in order, each `done`, `pending`, `partial` or `unknown`, with what it still needs
from you and the exact commands it would run, and checks your files and this machine first:

```sh
tomaccoctl call ios.virtual.setup '{"work":"/phone","iphone_firmware":"/phone/iphone-ipsw","pcc_firmware":"/phone/pcc","template":"/phone/ticket.shsh2","vphone_cli":"/phone/vphone-cli","swift_lib":"/absolute/swift/usr/lib/swift","clang":"/absolute/clang-22/clang"}'
```

`step:"run"` with the same arguments starts the next stage that is not done as a durable job;
`job.wait` it, then call `run` again. A stage whose output is complete is skipped, so calling
it again after an interruption carries on where it stopped. A stage that stopped halfway is
reported as `partial`, and you move its output aside: nothing is deleted for you. The engine
build and the IM4M stage ask first (`confirm:true`). The restore boots a guest and stays a
step you run by hand from the three commands the plan shows; pass `restored:true` once it has
reported `restore_completed:true`. The [error codes](troubleshooting.md#error-codes) list what
`run` refuses with and why.

Once the work directory's profile is written and selected, the plan says `ready:true` and names no
next stage, and `run` without a `stage` starts nothing: the phone starts from the profile, whatever
state the earlier outputs are in.

What was run: `plan` and the refusals of `run` on the development host with nothing prepared
under `work`, and `plan` and `run` on a work directory whose profile runs a live phone but whose
earlier outputs were made by hand or by an older Tomacco (three of them `partial`): it answered
`ready:true`, and `run` started nothing. No stage has been started through it yet; its stage logic
has unit tests.

`tomaccoctl doctor` gives a coarser view without your files: its `virtual_ios` step names the
next stage of the chain (`next_stage`) and lists every stage under `chain`, with `done` true for
the engine and helper builds it finds in the checkout's `.tooling`. Stages that depend on your
images say `null`, because only the check above can tell. With a profile configured, it names
what stops the next start instead, such as too little disk space. With that live profile, `doctor`
named no virtual iOS step, and `ios.virtual.host_check` passed everything but `passt`, which is not
installed there; with `start_disks:"overlay"` its disk check asks for the 1 GiB an overlay start
needs, not a 32 GiB copy.

## What no tool does for you

Tomacco distributes no Apple images. You obtain and prepare these inputs yourself:

1. **The two images, in exactly these versions.** Other builds are refused, because the
   kernel and restore adapters are pinned to these bytes.
   - The PCC `23E5207q` vphone600 firmware: cloudOS 26.4 beta, the identity *Research Darwin
     Cloud Customer Erase Install (IPSW)*, index 5 in the measured manifest. PCC is Apple's
     Private Cloud Compute, and vphone600 is its virtual device. Its research kernel must be
     SHA-256 `86895a2eebeb7fcfb68bcd60f2d7e685687faea07ee31166fb58c872b38e0d92` once unwrapped.
   - The iPhone17,3 iOS 27.0.1 `24A446` image: BuildManifest SHA-256
     `1d3df8a49cf134cbf3b9f46c67be5cfc000c6bc2fda014846784fa0a8993b461`, the *Customer Erase
     Install (IPSW)* identity.
2. **Decrypted images.** `pcc_system_image` and the iPhone image components must already be
   raw APFS. Tomacco contains no decryption and refuses an encrypted (AEA) archive.
3. **A local Image4 boot manifest (IM4M).** Download the public research template and
   run `tools/prepare_ios_research_manifest.py` after preparing your device tree.
   [The instructions below](#how-do-i-get-the-im4m) reproduce the measured setup.
   Restore, Cryptex grafts and the normal boot all use the resulting DER IM4M.
   - The host checks, none of it cryptographically, that `MANB/rosi/DGST` equals the iPhone
     identity's `OS` digest, chip `0xfe01` and board `0x91`, and that an ECID, when it names
     one, equals the prepared tree's. The guest's restore and sealing read its other entries.
   - The helper sets these declarations from your firmware manifests and prepared tree.
     Its output is a local research input, not a valid Apple-signed restore ticket.
4. **Unwrapping the boot components** in step 3 uses the `ipsw` that step 2 downloads, by hand or with
   `tools/prepare_ios_boot_inputs.py`.
5. **The Linux Swift toolchain, clang 22 and the `vphone-cli` checkout** are your installs,
   named by path below.

See the [legal notes](../legal.md#download-sources-and-permission) before you start.

## How do I get the IM4M?

**Create it locally on Linux. No Mac or Apple signing request is needed for this
research setup.** This is the procedure used for the measured Tomacco VM:

1. **Download the public template.** We originally used `ticket.shsh2` from Inferno's
   documentation. The original qemu-t8030 tools repository contains the identical
   file. This template comes from that third-party project, separately from the
   two Apple-hosted firmware archives:

   ```sh
   curl --fail --location 'https://raw.githubusercontent.com/TrungNguyen1909/qemu-t8030-tools/7a2017461ee7c1864330083dc6a781b75d1d0279/bootstrap_scripts/ticket.shsh2' --output /phone/ticket.shsh2
   ```

2. **Prepare the device trees in step 4 below**, then run this from your Tomacco
   checkout. Both firmware directories are the unpacked archives from the next
   section; no Python packages beyond the standard library are needed:

   ```sh
   python3 tools/prepare_ios_research_manifest.py \
     --template /phone/ticket.shsh2 \
     --pcc-firmware /phone/pcc \
     --iphone-firmware /phone/iphone-ipsw \
     --device-tree /phone/dtree.restore \
     --output /phone/your.im4m
   ```

   The helper extracts `ApImg4Ticket`, checks the exact template and firmware
   manifests, sets the PCC hardware declarations and your VM's ECID, and combines
   the PCC component digests with the four iPhone OS component digests.
   The source files stay unchanged. An existing output file is never overwritten.

3. **Keep `/phone/your.im4m` for every remaining step.** Pass the same file as
   `manifest`, `boot_manifest` and `sep_manifest` where shown below. Keep both
   prepared device trees: the normal tree inherits the restore tree's ECID.
   Do not regenerate the manifest for an already restored VM; it is part of that
   VM's persistent boot state.

The output deliberately has an invalid inherited Apple signature. It works in this
research runtime because parts of boot and sealing verification are bypassed;
it is not an Apple authorization or a ticket for a physical iPhone. The helper
does not contact Apple. An Apple-signed ticket from a normal restore is a different
thing, and is not needed by this procedure. The [legal notes](../legal.md) apply.

**Verification:** with the measured inputs and ECID, this helper produces exactly
the same 9,206 bytes as the manifest used by the working VM. This checks manifest
reproduction, not the whole setup chain on a fresh machine.

## Obtain the Apple images

These are technical acquisition and preparation steps, not a grant of rights to
Apple software. Review the applicable terms **before downloading**. Apple's PCC
VRE is offered for research on Apple silicon Macs and excludes commercial use;
its public availability does not authorize running an iPhone userland on Linux.
See [download sources and permission](../legal.md#download-sources-and-permission).

Use blacktop's **`ipsw`** command-line tool, plus `curl`, `unzip`, Git and Python 3.
The measured tool version was `3.1.730`; its Linux x86_64 release and installation
instructions are in the **blacktop/ipsw** project. `ipsw` and **ipsw.me** are
separate projects. The commands below assume `ipsw` is on your `PATH`.
`/phone` stands for an absolute directory you own; substitute your path throughout.
Keep the downloads and extracted files local.

### 1. Find and download the iPhone archive

On **ipsw.me**, select **iPhone 16**, identifier **`iPhone17,3`**, and find
**iOS 27.0.1 / `24A446`**. The filename is
`iPhone17,3_27.0.1_24A446_Restore.ipsw`. Check the build rather than choosing the
latest version. A catalog's "signed" label is about Apple's restore signing,
not permission or Tomacco compatibility.

The command-line lookup prints the download URL without saving the archive:

```sh
ipsw download ipsw --device iPhone17,3 --build 24A446 --urls
```

For this recorded build, the URL's hostname is `updates.cdn-apple.com`.
Download the archive from Apple:

```sh
mkdir -p /phone/downloads
ipsw download ipsw --device iPhone17,3 --build 24A446 --output /phone/downloads
```

### 2. Find and download the PCC archive

The PCC URL was recorded in **Lakr233/vphone-cli's firmware catalog**. Use the
same source revision as the runtime's GPU compiler:

```sh
git clone https://github.com/Lakr233/vphone-cli /phone/vphone-cli
git -C /phone/vphone-cli checkout 3a819b0dd00aea8906ddd8d33c9364736a869ca0
git -C /phone/vphone-cli grep -n 'static let cloud264' -- VPhoneKit/VPhoneCoreKit/Firmware/VPhoneFirmwareCatalog.swift
```

If that checkout already exists, reuse it at the stated revision. The output
contains the `cloud264` assignment. Copy **only its quoted URL** into `PCC_URL`
below. Its hostname is `updates.cdn-apple.com` and its path starts with
`/private-cloud-compute/`. The URL has no filename extension; the archive is
still an IPSW ZIP. vphone-cli supplies the catalog entry; Apple serves the file.

```sh
PCC_URL='paste the Apple URL assigned to cloud264 here'
curl --fail --location --continue-at - "$PCC_URL" --output /phone/downloads/PCC_26.4_23E5207q_Restore.ipsw
```

The catalog's comment identifies this as cloudOS **26.4 beta / `23E5207q`**, with
`vphone600ap`. A newer PCC release is not a substitute. If Apple removes either
archive, these instructions cannot restore its availability; do not substitute
an unverified mirror or a different build.

### 3. Unpack and check both manifests

```sh
unzip -n /phone/downloads/iPhone17,3_27.0.1_24A446_Restore.ipsw -d /phone/iphone-ipsw
unzip -n /phone/downloads/PCC_26.4_23E5207q_Restore.ipsw -d /phone/pcc
python3 - <<'PY'
import hashlib
from pathlib import Path

expected = {
    "/phone/iphone-ipsw/BuildManifest.plist": "1d3df8a49cf134cbf3b9f46c67be5cfc000c6bc2fda014846784fa0a8993b461",
    "/phone/pcc/BuildManifest.plist": "96b65481173fb8dbb87ba4a770cbf9ca26512ea3222ea38ca9e23532db29ccc4",
}
for filename, digest in expected.items():
    if hashlib.sha256(Path(filename).read_bytes()).hexdigest() != digest:
        raise SystemExit(f"Unexpected manifest: {filename}")
    print(f"Recorded manifest matched: {filename}")
PY
```

This checks the manifests against the measured setup, not Apple's signatures or
your rights to use the software. Keep the extracted directory structure: later
steps read component paths from each `BuildManifest.plist`. The iPhone erase
identity is **0**; the PCC research erase identity is **5**.

### 4. Prepare the disk-image inputs

Unzipping leaves the `.aea` components encrypted. Tomacco accepts only decrypted
original APFS images. Where you have the necessary rights to decrypt them, the
external `ipsw` tool provides the following operation; this is separate from
Tomacco, and Tomacco supplies no decryption keys:

```sh
mkdir -p /phone/iphone-ipsw/decrypted /phone/pcc/decrypted
ipsw fw aea /phone/iphone-ipsw/043-69462-662.dmg.aea --output /phone/iphone-ipsw/decrypted
ipsw fw aea /phone/iphone-ipsw/043-70113-711.dmg.aea --output /phone/iphone-ipsw/decrypted
ipsw fw aea /phone/pcc/094-39278-029.dmg.aea --output /phone/pcc/decrypted
```

The iPhone results are `043-69462-662.dmg` (OS) and `043-70113-711.dmg`
(SystemOS cryptex). Its AppOS cryptex, `043-69297-793.dmg`, is already
unwrapped in the archive. The PCC result is
`/phone/pcc/decrypted/094-39278-029.dmg`, used as `pcc_system_image` below.
The import checks all image digests against the manifests.

Next, [prepare your local IM4M](#how-do-i-get-the-im4m) using the public template,
these manifests and the device tree from step 4.
Do not share decrypted images,
extracted Apple binaries, tickets or prepared VM disks with a bug report.

## The chain

`tomaccoctl call` operations here are all durable jobs: repeat `job.wait`, at most 60 seconds per
call, until the job ends, then read `ios.emulator.report`. `/phone` stands for any absolute
directory of yours, and `/absolute` for your checkout.

1. **Engine and helper.** This also builds the x86_64 helper engine and its firmware directory
   under `.tooling/ios-sptm` when they are missing.

   ```sh
   python3 tools/build_ios_paravirt.py --jobs 8 --cpu-optimization native
   python3 tools/build_ios_guest_control.py --output .tooling/ios-guest-control
   ```

   `native` is what the numbers were measured on, and it fits only the CPU that built it. Leave
   it out for a portable binary, whose speed has not been measured.

   Run again, the engine build compares its inputs with the `build-manifest.json` of the engine
   already in `.tooling/ios-paravirt`: the pinned revisions, the ordered patch series, the hardware
   sources, the options, the compiler versions, the CPU for `native`, and the checkouts' contents.
   When they all match and the engine's digest is the recorded one, it builds nothing and says so.
   Otherwise it names each difference before it builds. `--plan` only prints that decision as JSON
   and writes nothing; `--rebuild` builds anyway. This comparison has unit tests only.

2. **Helper support and toolchain.** The first command makes `.tooling/ios-sptm/support`
   (`busybox/busybox`, `apfs/apfs.ko`, `ldid/ldid`, `ipsw/ipsw`) before it reads the firmware;
   `--firmware` is your unpacked iPhone image. The APFS module is built for the running kernel,
   so repeat this after a kernel update, or keep the `vmlinuz` beside it as `apfs/vmlinuz`.
   Reuse the pinned `/phone/vphone-cli` checkout from the image lookup above; if
   you obtained the images separately, clone that same revision before building
   the GPU plugin.

   ```sh
   python3 tools/prepare_ios_emulator.py --firmware /phone/iphone-ipsw --output /phone/restore-shell
   python3 tools/build_ios_linker.py --swift-lib /absolute/swift/usr/lib/swift --jobs 8
   tomaccoctl call ios.emulator.build_gpu_plugin '{"source":"/phone/vphone-cli","output":"/phone/gpu-compiler","linker":"/absolute/.tooling/ios-linker/prefix/bin/aarch64-apple-darwin-ld","ldid":"/absolute/.tooling/ios-sptm/support/ldid/ldid"}'
   ```

3. **Boot components.** Unwrap from your PCC firmware directory into `/phone/boot`, with
   `.tooling/ios-sptm/support/ipsw/ipsw img4 im4p extract IN -o OUT`:

   | From | To |
   | --- | --- |
   | `sptm.vresearch1.release.im4p` | `sptm` |
   | `txm.iphoneos.research.im4p` | `txm` |
   | `sep-firmware.vphone600.RELEASE.im4p` | `sep` |
   | `DeviceTree.vphone600ap.im4p` | `dtree.original` |
   | `kernelcache.research.vphone600` | `kernel` |
   | the identity's `RestoreRamDisk` | `ramdisk.dmg` |
   | the identity's `RestoreTrustCache` | `ramdisk.tc` |

   `tools/prepare_ios_boot_inputs.py` does the same in one command: it finds each file through
   identity 5 of your PCC image, unwraps it into a new directory, refuses a research kernel
   whose digest is not the one named above, and writes `boot-inputs.json` with each source and
   digest and the PCC `StaticTrustCache` that step 8 takes as `gpu_trustcache`. It has unit tests
   with synthetic files only.

   ```sh
   python3 tools/prepare_ios_boot_inputs.py --pcc-firmware /phone/pcc \
     --ipsw .tooling/ios-sptm/support/ipsw/ipsw --output /phone/boot
   ```

4. **One device, two trees, and its disks.** `--entropy-from` keeps one device identity (ECID)
   across both.

   ```sh
   python3 tools/prepare_ios_paravirt.py /phone/boot/dtree.original /phone/dtree.restore \
     --memory-gib 8 --omit-optional-ctrr --load-sep --sep-handoff userspace --ap-cpus 1
   python3 tools/prepare_ios_paravirt.py /phone/boot/dtree.original /phone/dtree.normal \
     --memory-gib 8 --omit-optional-ctrr --load-sep --ap-cpus 6 --entropy-from /phone/dtree.restore
   python3 tools/ios_paravirt_storage.py --device-tree /phone/dtree.restore --output /phone/storage --root-gib 32
   ```

   Now create `/phone/your.im4m` with the helper command in
   [How do I get the IM4M?](#how-do-i-get-the-im4m). Do this before step 6 and
   reuse that file for restore, Cryptex preparation and normal boot.

5. **Import the userland.** `RELEASE` in `helper_kernel` is your running kernel's version.
   The `components` entries select the two decrypted iPhone images from above;
   the AppOS image and metadata are read at their original archive paths.

   ```sh
   tomaccoctl call ios.emulator.prepare_userland '{"firmware":"/phone/iphone-ipsw","identity":0,"pcc_firmware":"/phone/pcc","pcc_identity":5,"pcc_system_image":"/phone/pcc/decrypted/094-39278-029.dmg","components":[{"component":"OS","path":"/phone/iphone-ipsw/decrypted/043-69462-662.dmg"},{"component":"Cryptex1,SystemOS","path":"/phone/iphone-ipsw/decrypted/043-70113-711.dmg"}],"gpu_plugin_build":"/phone/gpu-compiler","output":"/phone/userland","support":"/absolute/.tooling/ios-sptm/support","helper_engine":"/absolute/.tooling/ios-sptm/build/qemu-system-x86_64","helper_kernel":"/usr/lib/modules/RELEASE/vmlinuz","bios":"/absolute/.tooling/ios-sptm/source/pc-bios"}'
   ```

6. **Restore runtime**, with the research seal policy for the iPhone image. This is the first step
   that needs your IM4M.

   ```sh
   tomaccoctl call ios.emulator.prepare_restore '{"ramdisk":"/phone/boot/ramdisk.dmg","trustcache":"/phone/boot/ramdisk.tc","output":"/phone/restore-runtime","accel":"kvm","research_seal":{"firmware":"/phone/pcc","identity":5,"manifest":"/phone/your.im4m","overrides":[],"userland":"/phone/userland"},"support":"/absolute/.tooling/ios-sptm/support","helper_engine":"/absolute/.tooling/ios-sptm/build/qemu-system-x86_64","helper_kernel":"/usr/lib/modules/RELEASE/vmlinuz","bios":"/absolute/.tooling/ios-sptm/source/pc-bios"}'
   ```

7. **Restore.** Boot the restore profile, connect its private USB bridge and restore. `GUEST_JOB`
   is the first call's job, and `BRIDGE` is the `bridge` path the second call's job returns. Wait for
   `restore_completed:true`, then `job.cancel` the bridge and the guest. It took 1,810 seconds when
   measured. Add `overrides` to the restore only for components your firmware directory lacks; each
   must still match its manifest digest.

   ```sh
   tomaccoctl call ios.emulator.paravirtual '{"engine":"/absolute/.tooling/ios-paravirt/sptm-build/qemu-system-aarch64","seconds":3600,"sep_boot_role":"restore","sep_handoff":"userspace","sep_measure_manifest":true,"usb":true,"boot_manifest":"/phone/your.im4m","sep_manifest":"/phone/your.im4m","kernel":"/phone/boot/kernel","sptm":"/phone/boot/sptm","txm":"/phone/boot/txm","sep":"/phone/boot/sep","device_tree":"/phone/dtree.restore","ramdisk":"/phone/restore-runtime/ramdisk.dmg","trustcache":"/phone/restore-runtime/ramdisk.tc","writable_root_disk":"/phone/storage/system.raw","writable_auxiliary_disk":"/phone/storage/auxiliary.raw","sep_storage":"/phone/storage/sep.raw"}'
   tomaccoctl call ios.emulator.usb_bridge '{"job_id":"GUEST_JOB","seconds":3600}'
   tomaccoctl call ios.emulator.restore '{"bridge":"BRIDGE","firmware":"/phone/pcc","identity":5,"manifest":"/phone/your.im4m","system_image":"/phone/userland/images/system.dmg","userland":"/phone/userland","seconds":3300}'
   ```

8. **Cryptexes, guest tools, Cryptexes again.** The guest tools are built from the trust cache the
   first pass makes, and the kernel pins the adapter those tools carry, so the second pass, into a new
   directory, is the one you keep. Your PCC firmware's `StaticTrustCache` is `gpu_trustcache`.

   ```sh
   tomaccoctl call ios.emulator.prepare_cryptexes '{"disk":"/phone/storage/system.raw","userland":"/phone/userland","kernel":"/phone/boot/kernel","boot_manifest":"/phone/your.im4m","device_tree":"/phone/dtree.normal","gpu_trustcache":"/phone/pcc/Firmware/STATIC.trustcache","installer_control":true,"output":"/phone/cryptex-first","support":"/absolute/.tooling/ios-sptm/support","helper_engine":"/absolute/.tooling/ios-sptm/build/qemu-system-x86_64","helper_kernel":"/usr/lib/modules/RELEASE/vmlinuz","bios":"/absolute/.tooling/ios-sptm/source/pc-bios"}'
   tomaccoctl call ios.emulator.build_guest_tools '{"trustcache":"/phone/cryptex-first/system.tc","output":"/phone/guest-tools","linker":"/absolute/.tooling/ios-linker/prefix/bin/aarch64-apple-darwin-ld","ldid":"/absolute/.tooling/ios-sptm/support/ldid/ldid","clang":"/absolute/clang-22/clang","minimum_ios":"27.0","agent":true,"application_probe":true,"development_signing":true,"quiet_services":true}'
   tomaccoctl call ios.emulator.prepare_cryptexes '{"disk":"/phone/storage/system.raw","userland":"/phone/userland","kernel":"/phone/boot/kernel","boot_manifest":"/phone/your.im4m","device_tree":"/phone/dtree.normal","gpu_trustcache":"/phone/pcc/Firmware/STATIC.trustcache","installer_control":true,"development_tools":"/phone/guest-tools","output":"/phone/cryptex","support":"/absolute/.tooling/ios-sptm/support","helper_engine":"/absolute/.tooling/ios-sptm/build/qemu-system-x86_64","helper_kernel":"/usr/lib/modules/RELEASE/vmlinuz","bios":"/absolute/.tooling/ios-sptm/source/pc-bios"}'
   ```

9. **Kernel.** Two adapters follow the prepared kernel, each refusing any other input.
   `tools/prepare_ios_adapter_pin.py` is not a step here: the second pass already pinned this adapter,
   and a start runs it by itself when it rebuilds the tools for your apps.

   ```sh
   python3 tools/prepare_ios_fileport_compat.py --kernel /phone/cryptex/kernelcache.research.vphone600 --output /phone/kernel-fileport
   python3 tools/prepare_ios_workloop_compat.py --kernel /phone/kernel-fileport/kernelcache.research.vphone600 --output /phone/kernel
   ```

10. **First guest-tools install.** This makes the disk copy every later start copies from.

    ```sh
    tomaccoctl call ios.emulator.prepare_guest_tools '{"disk":"/phone/cryptex/system.raw","auxiliary_disk":"/phone/storage/auxiliary.raw","sep_storage":"/phone/storage/sep.raw","tools":"/phone/guest-tools","trustcache":"/phone/cryptex/system.tc","device_tree":"/phone/cryptex/device-tree","graphics":"/phone/cryptex/graphics/payload","development_device":true,"output":"/phone/first-install","support":"/absolute/.tooling/ios-sptm/support","helper_engine":"/absolute/.tooling/ios-sptm/build/qemu-system-x86_64","helper_kernel":"/usr/lib/modules/RELEASE/vmlinuz","bios":"/absolute/.tooling/ios-sptm/source/pc-bios"}'
    ```

11. **Profile and first start.** The generator checks all of the above the way a start would, refuses
    to overwrite a profile, and writes `profile.json` and a `state.json` whose verified base is the
    first install.

    ```sh
    python3 tools/ios_virtual_profile.py --directory /phone/profile --cryptex /phone/cryptex \
      --boot-inputs /phone/boot --kernel /phone/kernel/kernelcache.research.vphone600 \
      --tools /phone/guest-tools --install /phone/first-install --clang /absolute/clang-22/clang
    tomaccoctl call ios.virtual.configure '{"directory":"/phone/profile"}'
    tomaccoctl call ios.virtual.start '{}'
    tomaccoctl call ios.virtual.status '{}'
    ```

## What the profile sets

A generated profile sets `target_page: "16k"` and `bti: "off"`, which took swipes from 2-6 to 10-15
changed frames per second on the development host. It also sets the six processors of the tree above
in `boot_args`, `translation_cache_mib: 2048` (host memory; edit it on a small host), and the built-in
diagnostic app as the app a start opens.

From here on, [usage](usage.md) applies.

## What has been checked

Checked offline, without a guest:

- Step 3's commands reproduce the recorded boot inputs and kernel digest.
- Step 4's two trees carry one ECID.
- The chain's tools turn the original kernel, a manifest and its hashes into a known-good kernel,
  byte for byte.
- The generator, run on a working phone's artifacts, reproduces its `profile.json` except for its own
  apps and paths.
- Each default patch series applies to its pinned source in a disposable index.

Not checked:

- Any of steps 1-11 on a machine without this project's earlier state.
- A restore or a first start from a generated profile.
- A user's own compiler, bridge and adapter hashes in a booted kernel.

If you try it, please report what happens.
