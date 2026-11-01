#!/usr/bin/env python3
"""Build the direct Linux vphone600 research engine and Rust Vulkan device.

Requires the SPTM recipe's local toolchain (built automatically when absent).
Apple firmware is neither downloaded nor redistributed by this recipe.
"""
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import shlex
import subprocess
import tempfile

import build_ios_emulator as base
import ios_paravirt_build as audit

ORCHARD = ("https://github.com/yaelliethy/orchard.git",
           "c73a2bda1fd4afefd61204bb74afd74292ef2fe9")
METAL2VULKAN = ("https://github.com/steelbrain/metal2vulkan.git",
               "9e0e99a41dc3cb8bb7e288b531f1698a79fd4b1c")
INTEGRATION = base.REPO / "integrations/ios-emulator"
SHIMS = ("reims-vgpu-mmio.c", "reims-vgpu-dirty.c", "reims-vgpu-dirty.h",
         "reims-vgpu-shim.c", "reims-vgpu-shim.h")
PATCHES = (base.PATCHES["sptm"], INTEGRATION / "vphone-loader.patch",
           INTEGRATION / "vphone-ap-fp-reset.patch",
           INTEGRATION / "vphone-peripherals.patch", INTEGRATION / "vphone-aes.patch",
           INTEGRATION / "vphone-sep-loader.patch", INTEGRATION / "vphone-tlb-domains.patch",
           INTEGRATION / "vphone-fault-context.patch", INTEGRATION / "vphone-asmf.patch",
           INTEGRATION / "vphone-vstr.patch", INTEGRATION / "vphone-virtio-storage.patch",
           INTEGRATION / "vphone-usb.patch", INTEGRATION / "vphone-virtio-query.patch",
           INTEGRATION / "vphone-hid.patch", INTEGRATION / "vphone-usb-internal-ports.patch",
           INTEGRATION / "vphone-shared-memory.patch",
           INTEGRATION / "vhost-display-dirty-log.patch",
           INTEGRATION / "vphone-user-exception-context.patch",
           INTEGRATION / "qemu-x86-wide-tb-chaining.patch",
           INTEGRATION / "vphone-iosurface-source-observation.patch",
           INTEGRATION / "qemu-arm-rcsc-load-acquire.patch",
           INTEGRATION / "qemu-arm-pauth-bypass-inline.patch",
           INTEGRATION / "qemu-tcg-jump-cache.patch",
           INTEGRATION / "qemu-arm-jump-probe.patch",
           INTEGRATION / "qemu-arm-bti-property.patch",
           INTEGRATION / "qemu-arm-wfe-monitor.patch",
           INTEGRATION / "qemu-arm-target-page-16k.patch",
           INTEGRATION / "qemu-tcg-jump-cache-page-scale.patch",
           INTEGRATION / "vphone-trust-cache-update.patch")
# TCG throughput at the end of that series (tests/engine/branch-bench.S and
# wfe-monitor.S): a larger jump cache with cheap flushes, jump-cache probes
# at TB exits whose next state is known, the opt-in CPU without FEAT_BTI, WFE
# waiting on the location an exclusive load monitors. qemu-arm-target-page-16k:
# the machine option tomacco-page-bits (tomacco-vphone otherwise runs with Arm's
# legacy 1 KiB target pages) and a 16 KiB-granule-only CPU;
# qemu-tcg-jump-cache-page-scale keeps one jump-cache entry per two
# instructions of a larger target page. BTI off and 16 KiB pages stay boot
# options (`bti`, `target_page`); without them the engine behaves as before.
#
# Optional engine (QEMU) patches outside the default series, applied in this
# order after PATCHES only when named with --experimental-patch (none now).
EXPERIMENTAL_PATCHES = {}
# An experimental engine patch that is written on top of another one.
EXPERIMENTAL_PATCH_REQUIRES = {}
GRAPHICS_PATCHES = (INTEGRATION / "reims-iosurface-mapper.patch",
                    INTEGRATION / "reims-metal2vulkan-source.patch",
                    INTEGRATION / "reims-native-color-load.patch",
                    INTEGRATION / "reims-framebuffer-fetch.patch",
                    INTEGRATION / "reims-device-capabilities.patch",
                    INTEGRATION / "reims-display-panel.patch",
                    INTEGRATION / "reims-texel-copy-regions.patch",
                    INTEGRATION / "reims-compute-srgb.patch",
                    INTEGRATION / "reims-compute-prefix-regression.patch",
                    INTEGRATION / "reims-guest-clock.patch",
                    INTEGRATION / "reims-xr-scanout.patch",
                    INTEGRATION / "reims-xr-sampling.patch",
                    INTEGRATION / "reims-xr-render-targets.patch",
                    INTEGRATION / "reims-retired-hazards.patch",
                    INTEGRATION / "reims-compute-copy-fallback.patch",
                    INTEGRATION / "reims-xr-draw-sampling.patch",
                    INTEGRATION / "reims-native-buffer-textures.patch",
                    INTEGRATION / "reims-native-render-store.patch",
                    INTEGRATION / "reims-dual-xr-storage.patch",
                    INTEGRATION / "reims-dual-xr-sampling.patch",
                    INTEGRATION / "reims-dual-xr-compute.patch",
                    INTEGRATION / "reims-runtime-log-directory.patch",
                    INTEGRATION / "reims-xr-mapping-observe.patch",
                    INTEGRATION / "reims-dual-xr-memo.patch",
                    INTEGRATION / "reims-native-xr-surface.patch",
                    INTEGRATION / "reims-dual-xr-scratch.patch",
                    INTEGRATION / "reims-native-xr-transfer.patch",
                    INTEGRATION / "reims-xr-fragment-quantization.patch",
                    INTEGRATION / "reims-native-xr-rendering.patch",
                    INTEGRATION / "reims-native-xr-readback.patch",
                    INTEGRATION / "reims-r16f-linear-upload.patch",
                    INTEGRATION / "reims-r16f-color-load.patch",
                    INTEGRATION / "reims-xr-repeated-encode.patch",
                    INTEGRATION / "reims-encoder-lifetime.patch",
                    INTEGRATION / "reims-pass-attachment-evidence.patch",
                    INTEGRATION / "reims-apv-xr-render-targets.patch",
                    INTEGRATION / "reims-strip-framebuffer-fetch.patch",
                    INTEGRATION / "reims-xr-replacement-blend.patch",
                    INTEGRATION / "reims-native-float-store.patch",
                    INTEGRATION / "reims-native-float-lease.patch",
                    INTEGRATION / "reims-native-float-live-witness.patch",
                    INTEGRATION / "reims-native-float-cached-mapper.patch",
                    INTEGRATION / "reims-interleaved-xr-load-store.patch",
                    INTEGRATION / "reims-slot-population-once.patch",
                    INTEGRATION / "reims-interleaved-xr-secondary-store.patch",
                    INTEGRATION / "reims-rung-window.patch",
                    INTEGRATION / "reims-interleaved-xr-encode-lease.patch",
                    INTEGRATION / "reims-colour-gva-verbatim.patch",
                    INTEGRATION / "reims-native-gva-copy-contract.patch",
                    INTEGRATION / "reims-readback-import-hardening.patch",
                    INTEGRATION / "reims-streamed-write-xr-upload.patch",
                    INTEGRATION / "reims-streamed-page-runs.patch",
                    INTEGRATION / "reims-prepared-gva-transfer.patch",
                    INTEGRATION / "reims-absent-fragment-observe.patch",
                    INTEGRATION / "reims-descriptor-use-cache.patch",
                    INTEGRATION / "reims-fragment-binding-observe.patch",
                    INTEGRATION / "reims-buffer-texture-views.patch",
                    INTEGRATION / "reims-buffer-texture-read-span.patch",
                    INTEGRATION / "reims-optional-fragment-stage.patch",
                    INTEGRATION / "reims-optional-fragment-conformance.patch",
                    INTEGRATION / "reims-buffer-texture-render-targets.patch",
                    INTEGRATION / "reims-current-test-contracts.patch",
                    INTEGRATION / "reims-rgb10a2-render-targets.patch",
                    INTEGRATION / "reims-retired-pipeline-translation-gate.patch",
                    INTEGRATION / "reims-native-float-color-load.patch",
                    INTEGRATION / "reims-native-half-load-store.patch",
                    INTEGRATION / "reims-shared-texture-coverage.patch",
                    INTEGRATION / "reims-iosurface-component-bytes.patch",
                    INTEGRATION / "reims-rgb10a2-compute-storage.patch",
                    INTEGRATION / "reims-buffer-texture-packed-read.patch",
                    INTEGRATION / "reims-color-input-raster-order.patch",
                    INTEGRATION / "reims-xr-shader-blend.patch",
                    INTEGRATION / "reims-rgb8-444-planar-surface.patch",
                    INTEGRATION / "reims-eager-writeback-gva.patch",
                    INTEGRATION / "reims-strip-restart.patch",
                    INTEGRATION / "reims-texture-capture.patch",
                    INTEGRATION / "reims-depth-stencil-capture.patch",
                    INTEGRATION / "reims-texture-capture-tasks.patch",
                    INTEGRATION / "reims-mapper-ring-slots.patch",
                    INTEGRATION / "reims-release-guard-rails.patch",
                    INTEGRATION / "reims-device-info-after-bring-up.patch",
                    INTEGRATION / "reims-srgb-sibling-resolve.patch",
                    INTEGRATION / "reims-multisample-continuation.patch",
                    INTEGRATION / "reims-multisample-encoder-start-undefined.patch",
                    INTEGRATION / "reims-dual-plane-depth-stencil-note.patch",
                    INTEGRATION / "reims-null-texture-specialization.patch",
                    INTEGRATION / "reims-xr-binary16-partition.patch",
                    INTEGRATION / "reims-xr-blend-every-format.patch",
                    INTEGRATION / "reims-rgb8-444-destination-alpha.patch",
                    INTEGRATION / "reims-present-capture-in-place.patch",
                    INTEGRATION / "reims-sampled-resolve-split.patch",
                    INTEGRATION / "reims-linear-memo-witness.patch",
                    INTEGRATION / "reims-frame-mailbox.patch",
                    INTEGRATION / "reims-lazy-console-paint.patch",
                    INTEGRATION / "reims-mrt-batching.patch",
                    INTEGRATION / "reims-store-capture-reuse.patch",
                    INTEGRATION / "reims-memo-arm-in-exec.patch",
                    INTEGRATION / "reims-rung-blank-verdict.patch",
                    INTEGRATION / "reims-gva-resident-lifetime.patch",
                    INTEGRATION / "reims-resident-lifetime-rest.patch",
                    INTEGRATION / "reims-store-convert-beside-publish.patch",
                    INTEGRATION / "reims-mailbox-banded-offer.patch",
                    INTEGRATION / "reims-sampled-digest-lanes.patch")
# Optional graphics patches outside the default series, applied in this
# order after the series above only when named with --experimental-patch.
EXPERIMENTAL_GRAPHICS_PATCHES = {path.stem: path for path in (
    INTEGRATION / "reims-async-readback.patch",)}
TRANSLATOR_PATCHES = (INTEGRATION / "metal2vulkan-record-layout.patch",
                       INTEGRATION / "metal2vulkan-aggregate-prefix.patch",
                       INTEGRATION / "metal2vulkan-budget-compat.patch",
                       INTEGRATION / "metal2vulkan-fragment-color.patch",
                       INTEGRATION / "metal2vulkan-unused-fragment-outputs.patch",
                       INTEGRATION / "metal2vulkan-unused-resource-loads.patch",
                       INTEGRATION / "metal2vulkan-resource-wrapper-ownership.patch",
                       INTEGRATION / "metal2vulkan-composite-path-resolution.patch",
                       INTEGRATION / "metal2vulkan-nested-aggregate-origins.patch",
                       INTEGRATION / "metal2vulkan-fragment-blend.patch",
                       INTEGRATION / "metal2vulkan-nan-minmax.patch",
                       INTEGRATION / "metal2vulkan-constant-helper-cursor.patch",
                       INTEGRATION / "metal2vulkan-special-float-words.patch",
                       INTEGRATION / "metal2vulkan-null-texture-specialization.patch",
                       INTEGRATION / "metal2vulkan-output-precision.patch")


def hardening_overrides(mode):
    """C flags that undo QEMU's own hardening, appended after it (the last flag wins).

    QEMU's meson.build compiles every file with -ftrivial-auto-var-init=zero,
    -fzero-call-used-regs=used-gpr (clear every used register on each return,
    every TCG helper included) and, when the compiler supports it,
    -fstack-protector-strong. `qemu` keeps them; `off` (experimental) drops
    the three for a faster, less defended engine.
    """
    if mode == "qemu":
        return []
    if mode == "off":
        return ["-fno-stack-protector", "-ftrivial-auto-var-init=uninitialized",
                "-fzero-call-used-regs=skip"]
    raise ValueError(f"Unknown hardening mode: {mode}")


def selected_engine_patches(names):
    """PATCHES, then the named experimental engine patches in EXPERIMENTAL_PATCHES order."""
    for name in names:
        missing = [need for need in EXPERIMENTAL_PATCH_REQUIRES.get(name, ()) if need not in names]
        if missing:
            raise ValueError(f"--experimental-patch {name} also needs: " + ", ".join(missing))
    return PATCHES + tuple(path for name, path in EXPERIMENTAL_PATCHES.items() if name in names)


def stage_patches(source, patches=None, *, reject_untracked=False, dry_run=False):
    """Accept exactly our patch series; preserve any other tracked edit."""
    patches = PATCHES if patches is None else tuple(patches)
    untracked = set(base.run(
        ["git", "-C", source, "ls-files", "--others", "--exclude-standard", "-z"],
        capture=True).stdout.split("\0")[:-1])
    staged_additions = set(base.run(
        ["git", "-C", source, "diff", "--cached", "HEAD", "--name-only", "--diff-filter=A", "-z"],
        capture=True).stdout.split("\0")[:-1])
    additions = untracked | staged_additions
    if additions:
        # Patch-created files must also participate in the QEMU reverse check.
        # Derive ownership from the exact series in a disposable index. Other
        # untracked files remain untouched unless strict source checking is on.
        with tempfile.TemporaryDirectory(prefix="tomacco-vphone-expected-") as directory:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(directory) / "index")}
            base.run(["git", "-C", source, "read-tree", "HEAD"], env=env)
            for patch in patches:
                base.run(["git", "-C", source, "apply", "--cached", patch], env=env)
            added = set(base.run(["git", "-C", source, "diff-index", "--cached",
                                  "--name-only", "--diff-filter=A", "-z", "HEAD"],
                                 capture=True, env=env).stdout.split("\0")[:-1])
        if reject_untracked and additions - added:
            raise RuntimeError(f"Preserved untracked source in {source}; use a clean pinned checkout")
        # Include all staged additions so unrelated index edits still fail the
        # final comparison. Never write to the user's real index.
        additions = (untracked & added) | staged_additions
    diff = base.run(["git", "-C", source, "diff", "HEAD", "--binary"], capture=True).stdout
    if not diff and not additions:
        # Apply the entire series to an isolated index before touching sources.
        with tempfile.TemporaryDirectory(prefix="tomacco-vphone-preflight-") as directory:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(directory) / "index")}
            base.run(["git", "-C", source, "read-tree", "HEAD"], env=env)
            for patch in patches:
                base.run(["git", "-C", source, "apply", "--cached", patch], env=env)
        if dry_run:
            return
        for patch in patches:
            base.run(["git", "-C", source, "apply", "--check", patch])
            base.run(["git", "-C", source, "apply", patch])
        return
    # Reverse the series in a disposable index, never in the user's worktree.
    # A successful reverse alone is insufficient: no unrelated changes may remain.
    with tempfile.TemporaryDirectory(prefix="tomacco-vphone-index-") as directory:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(directory) / "index")}
        base.run(["git", "-C", source, "read-tree", "HEAD"], env=env)
        base.run(["git", "-C", source, "add", "-u"], env=env)
        if additions:
            base.run(["git", "-C", source, "add", "--", *sorted(additions)], env=env)
        for patch in reversed(patches):
            base.run(["git", "-C", source, "apply", "--cached", "--reverse", patch], env=env)
        base.run(["git", "-C", source, "diff-index", "--cached", "--quiet", "HEAD"], env=env)


def stage_file(target, data, *, dry_run=False):
    if target.is_symlink() or (target.exists() and (
            not target.is_file() or target.read_bytes() != data)):
        raise RuntimeError(f"Preserved different source: {target}; use a new --output directory")
    if not dry_run and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
    return hashlib.sha256(data).hexdigest()


def prefix_map_flags(maps):
    """Flags that record every source path under FROM as if it were under TO.

    The engine embeds its checkout location: `__FILE__` in QEMU's assertions
    and Rust panic locations (hundreds of absolute paths). With the
    same map on both compilers a build in another directory keeps the
    recorded tree's paths. What no flag maps is the absolute path of the
    translator checkout: it lies outside the orchard workspace, so Cargo mixes
    that path into the crate's metadata hash and so into its symbol names.
    """
    cflags, rustflags = [], []
    for item in maps:
        source, separator, target = item.partition("=")
        if not separator or not source or not target or any(c.isspace() for c in item):
            raise RuntimeError(f"--file-prefix-map takes FROM=TO without whitespace: {item!r}")
        cflags.append(f"-ffile-prefix-map={source}={target}")
        rustflags.append(f"--remap-path-prefix={source}={target}")
    return cflags, rustflags


PGO_MODES = ("off", "generate", "use")
# Beside the published stage-1 engine (and in its profile directory): tells
# run_ios_paravirt.py to give the engine time to write its profile at exit.
PGO_MARKER = "pgo-generate.json"


def pgo_tree_digest(directory, suffix):
    """SHA-256 over the sorted (relative path, content digest) pairs of the profile files."""
    entries = []
    if directory.is_dir():
        for path in sorted(directory.rglob("*" + suffix)):
            if path.is_file():
                entries.append([str(path.relative_to(directory)), base.sha256(path), path.stat().st_size])
    digest = hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()
    return {"files": len(entries), "bytes": sum(entry[2] for entry in entries), "sha256": digest}


def llvm_major(text):
    match = re.search(r"LLVM version:? (\d+)\.", text)
    return int(match[1]) if match else None


def pgo_plan(mode, data, build, root, run, env):
    """C and Rust flags of one profile-guided stage, plus what the receipt says about them.

    generate (stage 1): GCC instruments every object (`-fprofile-generate`;
    not `-fprofile-update=atomic`, whose shared counters kept a real boot from
    unlocking within 40 minutes on seven MTTCG threads: a lost count
    only blurs the profile, and stage 2 corrects it) and writes `<data>/c/<object path>.gcda` when the engine
    exits normally (QMP quit or SIGTERM; never after SIGKILL), merging into
    files a previous session left. `-fprofile-prefix-path=<build>` names the
    files after the object path inside the build directory, so a stage 2 in
    another directory finds them. rustc instruments the renderer
    (`-Cprofile-generate`), which writes `<data>/rust/default_<hash>_<pid>.profraw`.
    The directory is baked into the engine (absolute); at run time GCOV_PREFIX
    and LLVM_PROFILE_FILE move it.

    use (stage 2): the same sources and flags plus `-fprofile-use` with
    `-fprofile-partial-training` (code the training never ran keeps its normal
    optimisation instead of being treated as cold), `-Wno-missing-profile` and
    `-Wno-error=coverage-mismatch` (a function changed since the training
    engine, e.g. by another patch, drops its profile with a warning);
    the renderer's raw profiles are merged with llvm-profdata of rustc's LLVM
    major into `<root>/pgo-rust.profdata` for `-Cprofile-use`. No raw Rust
    profile: the renderer is built without one (recorded). The receipt
    records digests of the profile data the build consumed.
    """
    if mode == "off":
        return [], [], None
    c_dir, rust_dir = data / "c", data / "rust"
    common = [f"-fprofile-prefix-path={build}"]
    record = {"mode": mode, "data": str(data)}
    if mode == "generate":
        left = [path for suffix in (".gcda", ".profraw") for path in data.rglob("*" + suffix)] \
            if data.is_dir() else []
        if left:
            raise RuntimeError(f"--pgo generate needs an empty profile directory; {data} already holds "
                               f"{len(left)} profile files (another stage 1's training?)")
        data.mkdir(parents=True, exist_ok=True)
        c_flags = [f"-fprofile-generate={c_dir}", *common]
        rust_flags = [f"-Cprofile-generate={rust_dir}"]
        record.update(c_flags=c_flags, rust_flags=rust_flags, exit_note=(
            "profiles are written when the engine exits normally (QMP quit or SIGTERM); "
            "a SIGKILLed session adds nothing"))
        return c_flags, rust_flags, record
    gcda = pgo_tree_digest(c_dir, ".gcda")
    if not gcda["files"]:
        raise RuntimeError(f"--pgo use found no .gcda profile under {c_dir}; run the stage-1 engine first")
    # A function whose source differs from the training engine's (another
    # patch set) loses its profile with a warning instead of failing the build.
    c_flags = [f"-fprofile-use={c_dir}", "-fprofile-correction", "-fprofile-partial-training", "-Wno-missing-profile",
               "-Wno-error=coverage-mismatch", *common]
    raw = sorted(rust_dir.glob("*.profraw")) if rust_dir.is_dir() else []
    record.update(c_profile=gcda, rust_raw_profile=pgo_tree_digest(rust_dir, ".profraw"))
    rust_flags = []
    if raw:
        profdata = shutil.which("llvm-profdata", path=env["PATH"])
        if not profdata:
            raise RuntimeError("--pgo use with Rust raw profiles needs llvm-profdata")
        rustc_llvm = llvm_major(run(["rustc", "-vV"], env=env, capture=True).stdout)
        tool_llvm = llvm_major(run([profdata, "--version"], env=env, capture=True).stdout)
        if rustc_llvm is None or rustc_llvm != tool_llvm:
            raise RuntimeError(f"llvm-profdata is LLVM {tool_llvm}, rustc uses LLVM {rustc_llvm}: "
                               "raw profiles do not cross LLVM majors")
        merged = root / "pgo-rust.profdata"
        run([profdata, "merge", "-o", merged, *raw], env=env)
        rust_flags = [f"-Cprofile-use={merged}"]
        record["rust_profile"] = {**audit.stable_file(merged), "llvm_major": rustc_llvm}
    else:
        record["rust_profile"] = None
        record["rust_profile_note"] = "no raw Rust profile: the renderer is built without profile guidance"
    record.update(c_flags=c_flags, rust_flags=rust_flags)
    stage1 = data / PGO_MARKER
    if stage1.is_file():
        record["stage1"] = json.loads(stage1.read_text())
    record["profile_sha256"] = hashlib.sha256(json.dumps(
        [gcda["sha256"], record["rust_profile"] and record["rust_profile"]["sha256"]]).encode()).hexdigest()
    return c_flags, rust_flags, record


def llvm_profile_runtime(root, run, env):
    """The LLVM profile runtime an instrumented renderer needs, as an archive QEMU's link can use.

    rustc -Cprofile-generate instruments the renderer but leaves its runtime
    (`__llvm_profile_instrument_*`, the atexit writer) out of a staticlib: only
    rustc's own executable links add profiler_builtins. Its objects come from
    the toolchain's rlib; GCDAProfiling.o stays out because it defines
    `__gcov_dump` and `__gcov_reset`, which libgcov (the C side) owns.
    """
    sysroot = run(["rustc", "--print", "sysroot"], env=env, capture=True).stdout.strip()
    host = next(line.split(": ", 1)[1] for line in
                run(["rustc", "-vV"], env=env, capture=True).stdout.splitlines() if line.startswith("host: "))
    rlibs = sorted(Path(sysroot, "lib/rustlib", host, "lib").glob("libprofiler_builtins-*.rlib"))
    if len(rlibs) != 1:
        raise RuntimeError(f"Expected one profiler_builtins rlib in {sysroot}, found {len(rlibs)}")
    archive = root / "pgo-llvm-profile-runtime.a"
    with tempfile.TemporaryDirectory(prefix="tomacco-pgo-runtime-") as directory:
        run(["ar", "x", rlibs[0]], cwd=directory, env=env)
        members = sorted(path.name for path in Path(directory).glob("*.o")
                         if not path.name.endswith(".rcgu.o") and not path.name.endswith("GCDAProfiling.o"))
        if not any(name.endswith("InstrProfilingRuntime.o") for name in members):
            raise RuntimeError(f"{rlibs[0]} holds no InstrProfilingRuntime.o")
        archive.unlink(missing_ok=True)
        run(["ar", "rcs", archive, *members], cwd=directory, env=env)
    return archive, {**audit.stable_file(rlibs[0]), "path": str(rlibs[0]), "members": members}


def clear_build_time_profile(data):
    """Remove what configure's test programs and `-machine help` wrote into a fresh profile directory."""
    removed = 0
    for suffix in (".gcda", ".profraw"):
        for path in data.rglob("*" + suffix):
            path.unlink()
            removed += 1
    return removed


def expected_engine_digest(value):
    """Normalise --expect-engine-sha256 before any work starts."""
    if value is None:
        return None
    digest = value.strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise RuntimeError("--expect-engine-sha256 takes the 64 hexadecimal digits of a SHA-256")
    return digest


def reproducibility_check(record, expected):
    """Say in the manifest whether the built engine is the expected one, byte for byte."""
    if expected is None:
        return None
    return {"expected_engine_sha256": expected, "engine_sha256": record["engine_sha256"],
            "reproduced": record["engine_sha256"] == expected}


# Feature options whose config-host.h macro is not CONFIG_<OPTION>. Every other
# feature with a macro follows that rule (numa: CONFIG_NUMA, zstd: CONFIG_ZSTD).
FEATURE_MACROS = {"alsa": "CONFIG_AUDIO_ALSA", "coreaudio": "CONFIG_AUDIO_COREAUDIO",
                  "dsound": "CONFIG_AUDIO_DSOUND", "jack": "CONFIG_AUDIO_JACK",
                  "oss": "CONFIG_AUDIO_OSS", "pa": "CONFIG_AUDIO_PA",
                  "pipewire": "CONFIG_AUDIO_PIPEWIRE", "sndio": "CONFIG_AUDIO_SNDIO",
                  "bpf": "CONFIG_EBPF", "cap_ng": "CONFIG_LIBCAP_NG", "crypto_afalg": "CONFIG_AF_ALG",
                  "keyring": "CONFIG_SECRET_KEYRING", "libkeyutils": "CONFIG_KEYUTILS",
                  "libusb": "CONFIG_USB_LIBUSB", "valgrind": "CONFIG_VALGRIND_H",
                  "xen": "CONFIG_XEN_BACKEND"}
# The configured headers a pinned build must reproduce: the host feature set,
# the devices Kconfig selected from it, and the target's own configuration.
CONFIGURE_HEADERS = ("config-host.h", "aarch64-softmmu-config-devices.h",
                     "aarch64-softmmu-config-target.h")
CONFIGURE_FLAG = re.compile(r"--(enable|disable)-([a-z0-9-]+)")
LOOKUP_LINE = re.compile(r'^(?:Run-time dependency|Dependency|Library|Program|Has header) '
                         r'"?([^" ]+)"? ?(?:found: |: )(YES|NO)\b')


def configured_macros(text):
    """`#define NAME` and `#undef NAME` lines of a Meson-generated config header."""
    macros = {}
    for line in text.splitlines():
        match = re.match(r"#\s*(define|undef)\s+([A-Za-z_][A-Za-z0-9_]*)", line)
        if match:
            macros[match.group(2)] = match.group(1) == "define"
    return macros


def feature_lookups(meson_build):
    """Host lookups QEMU's meson.build ties to a feature option: option -> names looked up."""
    lookups = {}
    for call in re.finditer(r"\b(?:dependency|find_library|find_program|has_header)\s*\(", meson_build):
        depth, position = 1, call.end()
        while depth and position < len(meson_build):
            depth += {"(": 1, ")": -1}.get(meson_build[position], 0)
            position += 1
        body = meson_build[call.end():position - 1]
        option = re.search(r"required:\s*get_option\('([a-z0-9_]+)'\)", body)
        name = re.match(r"\s*'([^']+)'", body)
        if option and name:
            lookups.setdefault(option.group(1), []).append(name.group(1))
    return lookups


def lookup_results(meson_log):
    """What Meson's log says each named lookup found; a later lookup of a name replaces an earlier one."""
    results = {}
    for line in meson_log.splitlines():
        match = LOOKUP_LINE.match(line)
        if match:
            results[match.group(1)] = match.group(2) == "YES"
    return results


def accepted_configure_flags(buildoptions_script):
    """Every --enable-X/--disable-X that scripts/meson-buildoptions.sh translates for Meson."""
    return set(re.findall(r"(--(?:enable|disable)-[a-z0-9-]+)\)", buildoptions_script))


def configure_pins(reference, source):
    """--enable/--disable flags that fix every host-detected feature of a configured QEMU build.

    A Meson feature left at `auto` follows the build host: a host that gains
    libnuma between two builds makes a clean configure define CONFIG_NUMA. The
    reference build directory's `meson-info/intro-buildoptions.json` holds the
    requested value of every option, including those changed later with
    `meson configure` (the five remote block libraries), which its
    `config.status` does not record; `config-host.h` holds what the auto
    features resolved to; for features without a macro the Meson log says what
    their library lookup found. `config-host.mak` carries only paths in this
    QEMU. Features neither explicit, nor in the header, nor looked up (block
    drivers, accelerators such as kvm, mshv and nitro, `tools`) are resolved
    from the host OS and target list alone and stay unpinned, as does a
    feature whose lookup the Meson log never ran on this host (whpx, nvmm);
    each is listed with the reason. The record is complete only with all five
    keys (reference, flags, evidence, unpinned, headers): the manifest records
    the first four and the parity check reads the fifth.
    """
    reference, source = Path(reference), Path(source)
    files = {"meson-info/intro-buildoptions.json": None, "config-host.h": None,
             "meson-logs/meson-log.txt": None}
    for name in files:
        if not (reference / name).is_file():
            raise RuntimeError(f"Not a configured QEMU build directory (missing {name}): {reference}")
        files[name] = audit.stable_file(reference / name)["sha256"]
    options = json.loads((reference / "meson-info/intro-buildoptions.json").read_text())
    macros = configured_macros((reference / "config-host.h").read_text())
    lookups = feature_lookups((source / "meson.build").read_text())
    results = lookup_results((reference / "meson-logs/meson-log.txt").read_text(errors="replace"))
    accepted = accepted_configure_flags((source / "scripts/meson-buildoptions.sh").read_text())
    flags, evidence, unpinned = [], {}, {}
    for option in options:
        if (option.get("section") != "user" or option.get("type") != "combo"
                or set(option.get("choices", ())) != {"enabled", "disabled", "auto"}
                or ":" in option["name"]):
            continue
        name, state = option["name"], None
        if option["value"] != "auto":
            state, why = option["value"], f"requested {option['value']}"
        else:
            macro = FEATURE_MACROS.get(name, "CONFIG_" + name.upper())
            missing = [item for item in lookups.get(name, ()) if results.get(item) is False]
            if macro in macros and macros[macro] and missing:
                # Auto-detection tolerated the missing lookup (gio without
                # gdbus-codegen); --enable would demand it and stop configure.
                unpinned[name] = (f"config-host.h defines {macro}, yet --enable-{name.replace('_', '-')} "
                                  f"would demand {', '.join(missing)}, which the reference did not find")
            elif macro in macros:
                state = "enabled" if macros[macro] else "disabled"
                why = f"config-host.h {'defines' if macros[macro] else 'undefines'} {macro}"
            elif name in lookups:
                found = {item: results.get(item) for item in lookups[name]}
                if all(value is not None for value in found.values()) or False in found.values():
                    state = "enabled" if all(found.values()) else "disabled"
                    why = "meson-log: " + ", ".join(
                        f"{item} {'found' if value else 'not found'}" for item, value in found.items()
                        if value is not None)
                else:
                    unpinned[name] = "the Meson log records no result for " + ", ".join(lookups[name])
            else:
                unpinned[name] = "resolved from the host OS and the target list, not from a host library"
        if state is None:
            continue
        flag = f"--{'enable' if state == 'enabled' else 'disable'}-{name.replace('_', '-')}"
        if flag not in accepted:
            raise RuntimeError(f"QEMU's configure does not accept the derived pin {flag}")
        flags.append(flag)
        evidence[name] = why
    headers = {}
    for name in CONFIGURE_HEADERS:
        path = reference / name
        if not path.is_file():
            raise RuntimeError(f"Reference build lacks the configured header {name}: {reference}")
        files[name] = audit.stable_file(path)["sha256"]
        headers[name] = path.read_text()
    return {"reference": {"build": str(reference), "files": files}, "flags": flags,
            "evidence": evidence, "unpinned": unpinned, "headers": headers}


def load_configure_pins(path, source):
    """A pins record: derived from a configured QEMU build directory, or read back from a file."""
    path = Path(path)
    if path.is_dir():
        return configure_pins(path, source)
    record = json.loads(path.read_text())
    if not (isinstance(record, dict) and isinstance(record.get("flags"), list)
            and isinstance(record.get("headers"), dict)
            and all(isinstance(record.get(key), dict) for key in ("reference", "evidence", "unpinned"))
            and all(isinstance(flag, str) and CONFIGURE_FLAG.fullmatch(flag) for flag in record["flags"])
            and all(name in record["headers"] for name in CONFIGURE_HEADERS)):
        raise RuntimeError(f"Not a configure-pins record (needs reference, flags, evidence, "
                           f"unpinned and the configured headers): {path}")
    return record


def pinned_configure_flags(flags, pins):
    """The recipe's configure flags plus the pins; a pin contradicting the recipe is an error."""
    chosen = {}
    for flag in flags:
        match = CONFIGURE_FLAG.fullmatch(flag)
        if match:
            chosen[match.group(2)] = match.group(1)
    merged = list(flags)
    for flag in pins:
        match = CONFIGURE_FLAG.fullmatch(flag)
        if not match:
            raise RuntimeError(f"Not a configure pin: {flag!r}")
        state, name = match.groups()
        if name in chosen:
            if chosen[name] != state:
                raise RuntimeError(f"Configure pin {flag} contradicts the recipe's --{chosen[name]}-{name}")
            continue
        chosen[name] = state
        merged.append(flag)
    return merged


def configure_parity(build, headers):
    """Whether the configured headers equal the pinned reference's, with the differing lines."""
    report, drifted = {}, []
    for name, expected in headers.items():
        actual = (Path(build) / name).read_text()
        if actual == expected:
            report[name] = {"identical": True}
            continue
        differences = [line.rstrip("\n") for line in difflib.unified_diff(
            expected.splitlines(True), actual.splitlines(True), f"reference/{name}", f"build/{name}", n=0)]
        report[name] = {"identical": False, "differences": differences[:200]}
        drifted.append(name)
    return report, drifted


CACHE_PARAMETERS = ("l1-cache-size", "l1-cache-line-size", "l2-cache-size")
CACHE_PARAMETER = re.compile(r"--param=(" + "|".join(CACHE_PARAMETERS) + r")=\s*(\d+)")


def cache_parameter_flags(observations):
    """Pin GCC's cache parameters to the largest values any CPU of the host reports.

    `-march=native` asks the CPU the compiler happens to run on for its cache
    geometry. On a hybrid Intel part the performance cores answer 48 KiB of L1
    data cache and the efficiency cores 32 KiB, so each object is compiled with
    whichever core the scheduler chose (788 of 1925 QEMU objects at 48 in one
    build, 663 in the next); the parameter
    shapes the code and GCC's LTO section seed, and no two builds link the same
    engine. The flags give every object and the LTO link the same geometry, the
    performance cores'.
    """
    pinned, observed = {}, {}
    for name in CACHE_PARAMETERS:
        values = [found[name] for found in observations.values() if name in found]
        if values:
            pinned[name] = max(values)
            observed[name] = {str(value): values.count(value) for value in sorted(set(values))}
    flags = [f"--param={name}={value}" for name, value in pinned.items()]
    return flags, {"pinned": pinned, "observed": observed, "cpus": len(observations)}


def native_expansion(driver_output):
    """What the GCC driver turned `-march=native -mtune=native` into: ISA flags and cache parameters.

    `cc -march=native -mtune=native -E -v` prints the cc1 command line, where
    `-march=native` has become `-march=alderlake -mmmx ... --param l1-cache-size=48
    ... -mtune=alderlake`. The ISA flags are the same on every core; the
    parameters are the running core's.
    """
    for line in driver_output.splitlines():
        tokens = line.split()
        if tokens and tokens[0].endswith("cc1") and any(t.startswith("-march=") for t in tokens):
            flags, params = [], {}
            for index, token in enumerate(tokens):
                if token == "--param" and index + 1 < len(tokens) and "=" in tokens[index + 1]:
                    name, value = tokens[index + 1].split("=", 1)
                    if name in CACHE_PARAMETERS and value.isdigit():
                        params[name] = int(value)
                elif token.startswith("-m"):
                    flags.append(token)
            return flags, params
    return None, {}


def observe_native_target(cc, env):
    """The native target `cc` resolves on each CPU this build may run on."""
    observations = {}
    original = os.sched_getaffinity(0)
    try:
        for cpu in sorted(original):
            os.sched_setaffinity(0, {cpu})
            report = subprocess.run([cc, "-march=native", "-mtune=native", "-E", "-v", "-x", "c",
                                     os.devnull, "-o", os.devnull], env=env, capture_output=True, text=True)
            observations[cpu] = native_expansion(report.stderr)
    finally:
        os.sched_setaffinity(0, original)
    return observations


def native_target_flags(observations):
    """Explicit flags for the native target, the same whichever core compiles.

    `-march=native` itself stays out of the command line: the driver's expansion
    carries the running core's cache parameters, so with it two objects compiled
    on different core types differ even when explicit parameters follow and win.
    """
    expansions = [flags for flags, _ in observations.values() if flags is not None]
    if not expansions:
        return ["-march=native", "-mtune=native"], {"expanded": False, "cpus": len(observations)}
    if any(flags != expansions[0] for flags in expansions):
        raise RuntimeError("The CPUs of this host resolve -march=native to different instruction sets")
    params, record = cache_parameter_flags({cpu: found for cpu, (_, found) in observations.items()})
    return [*expansions[0], *params], {"expanded": True, "isa_flags": expansions[0], **record}


SEEDED_COMPILER = """#!/bin/sh
# Written by tools/build_ios_paravirt.py. GCC draws a random seed for every
# compilation unless it is given one, and names the LTO sections of the object
# after it; this wrapper derives the seed from the output path, so the same
# source compiled into the same object gets the same seed in every build.
output=
previous=
for argument; do
  if [ "$previous" = "-o" ]; then output=$argument; fi
  previous=$argument
done
if [ -n "$output" ]; then
  exec {compiler} "$@" -frandom-seed="$(printf %s "$output" | sha256sum | cut -c1-16)"
fi
exec {compiler} "$@"
"""


def seeded_compilers(root, env):
    """Wrappers for cc and c++ that give GCC a seed derived from each output path."""
    wrappers = {}
    for name, variable in (("cc", "CC"), ("c++", "CXX")):
        compiler = shutil.which(name, path=env["PATH"])
        if not compiler:
            raise RuntimeError(f"Missing tool: {name}")
        if any(c.isspace() or c in "'\"$`\\" for c in compiler):
            raise RuntimeError(f"Compiler path cannot be quoted for the seed wrapper: {compiler}")
        wrapper = Path(root) / f"{name}-seeded"
        wrapper.write_text(SEEDED_COMPILER.format(compiler=compiler))
        wrapper.chmod(0o755)
        wrappers[variable] = str(wrapper)
    return wrappers


def deterministic_environment(env):
    """The build environment with the build scripts' own sources of variation removed.

    QEMU generates its ARM decoders with `scripts/decodetree.py`, whose output
    follows Python's per-process hash seed: of two builds in the same directory
    with the same compilers and seeds, the eight objects built from the decoders
    differed and so did the privatization of one VFP helper in the link.
    `PYTHONHASHSEED=0` fixes the seed. The C locale keeps generated files that
    pass through `sort` (`config-poison.h`) in one order.

    The orchard release profile asks for `incremental = true` beside
    `lto = "fat"`: rustc still links every workspace crate with fat LTO, but
    names the objects it writes after the incremental session, so two
    otherwise identical renderer archives differ in eleven member names and
    their digests never agree. `CARGO_INCREMENTAL=0` overrides the profile
    (Cargo's documented precedence) and the names become functions of the
    crate alone; the engine, which reads the members and not their names, was
    already reproducible without it.
    """
    return {**env, "PYTHONHASHSEED": "0", "LC_ALL": "C.UTF-8", "CARGO_INCREMENTAL": "0"}


def link_translator(orchard, translator, maps):
    """Reach the translator through a link inside the renderer workspace.

    Cargo hashes a path dependency's location into every dependent crate's
    metadata, and so into its symbol names: the absolute path when the crate
    lies outside the workspace (`<root>/metal2vulkan`), the workspace-relative
    path when it lies inside. Through `reims-vgpu/target/metal2vulkan`, a path
    the orchard checkout ignores, the hash covers `target/metal2vulkan` in every
    checkout location. The returned rustc map, applied after the others, keeps
    the recorded source paths those of a build that reaches the translator
    directly, `<mapped root>/metal2vulkan`.
    """
    link = Path(orchard) / "reims-vgpu/target/metal2vulkan"
    link.parent.mkdir(parents=True, exist_ok=True)
    relative = os.path.relpath(translator, link.parent)
    if link.is_symlink():
        if os.readlink(link) != relative:
            link.unlink()
            os.symlink(relative, link)
    elif link.exists():
        raise RuntimeError(f"Preserved existing file at the translator link: {link}")
    else:
        os.symlink(relative, link)
    recorded = str(translator)
    for item in maps:
        source, _, target = item.partition("=")
        if recorded == source or recorded.startswith(source.rstrip("/") + "/"):
            recorded = target.rstrip("/") + recorded[len(source.rstrip("/")):]
    return link, f"--remap-path-prefix={link}={recorded}"


def stage_hardware(source, orchard, *, dry_run=False, gpu_link_args=()):
    if not dry_run:
        stage_hardware(source, orchard, dry_run=True, gpu_link_args=gpu_link_args)
    record = base.stage_hardware_sources(source, "sptm", dry_run=dry_run)
    stage = lambda target, data: stage_file(target, data, dry_run=dry_run)
    hid = Path("hw/usb/vphone-hid.c")
    record[str(hid)] = stage(source / hid, (INTEGRATION / "vphone-hid.c").read_bytes())
    for name in ("vphone-research.c", "vphone-sep.c", "vphone-sep.h",
                 "vphone-asmf.c", "vphone-asmf.h", "vphone-vstr.c", "vphone-vstr.h", "vphone-usb.c"):
        board = Path("hw/arm") / name
        record[str(board)] = stage(source / board, (INTEGRATION / name).read_bytes())
    fault_header = Path("target/arm/tcg/vphone-fault.h")
    record[str(fault_header)] = stage(source / fault_header,
                                          (INTEGRATION / "vphone-fault.h").read_bytes())
    destination = source / "hw/display/tomacco-reims"
    for name in SHIMS:
        target = destination / name
        record[str(target.relative_to(source))] = stage(
            target, (orchard / "qemu/hw/display" / name).read_bytes())
    record["hw/display/tomacco-reims/trace.h"] = stage(
        destination / "trace.h", b'#include "trace/trace-hw_display.h"\n')
    gpu = orchard / "reims-vgpu"
    # Meson string literals: paths are data, never shell commands.
    quote = lambda value: "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"
    meson = (
        "tomacco_gpu = declare_dependency(\n"
        f"  link_args: [{quote(gpu / 'target/release/libreims_vgpu.a')}, "
        + "".join(quote(arg) + ", " for arg in gpu_link_args) + "'-lm', '-ldl', '-lpthread'],\n"
        f"  compile_args: [{quote('-I' + str(gpu / 'crates/reims-vgpu/include'))}])\n"
        "system_ss.add(files('reims-vgpu-dirty.c', 'reims-vgpu-shim.c'), tomacco_gpu)\n"
        "specific_ss.add(files('reims-vgpu-mmio.c'), tomacco_gpu)\n"
    )
    record["hw/display/tomacco-reims/meson.build"] = stage(destination / "meson.build", meson.encode())
    return record


REUSE_TOOLS = ("rustc", "cargo", "cc", "c++")


def quiet_run(argv, *, cwd=None, env=None, capture=False):
    """base.run without echoing the command, so a plan's JSON stays alone on stdout."""
    return subprocess.run(list(map(str, argv)), cwd=cwd, env=env, check=True,
                          stdout=subprocess.PIPE if capture else subprocess.DEVNULL, text=capture)


def toolchain_versions(run=quiet_run):
    """The first --version lines of the compilers that shape the engine's bytes, as the manifest records them."""
    versions = {}
    for name in REUSE_TOOLS:
        resolved = shutil.which(name)
        versions[name] = (run([Path(resolved).resolve(), "--version"], capture=True).stdout.splitlines()[:3]
                          if resolved else None)
    return versions


def changed_names(recorded, wanted):
    """What differs between two {name: digest} maps, in words."""
    recorded, wanted = recorded or {}, wanted or {}
    words = [f"{name} changed" for name in sorted(set(recorded) & set(wanted)) if recorded[name] != wanted[name]]
    words += [f"{name} is new" for name in sorted(set(wanted) - set(recorded))]
    words += [f"{name} is no longer used" for name in sorted(set(recorded) - set(wanted))]
    return words


def reuse_reasons(record, wanted, engine):
    """Why an earlier build's manifest `record` does not stand for the build `wanted` describes: [] when the
    engine at `engine` can be reused as it is. `wanted` holds what this run would put into the manifest; a key
    it lacks (no checkout to fingerprint, say) is not compared."""
    if record is None:
        return ["there is no complete earlier build (build-manifest.json) in this output directory"]
    reasons = []
    if not engine.is_file():
        reasons.append(f"the engine {engine} is missing")
    elif hashlib.sha256(engine.read_bytes()).hexdigest() != record.get("engine_sha256"):
        reasons.append(f"the engine {engine} is not the one its manifest records")
    recorded_commits, wanted_commits = record.get("source_commits") or {}, wanted["source_commits"]
    for role in sorted(wanted_commits):
        if recorded_commits.get(role) != wanted_commits[role]:
            reasons.append(f"the pinned {role} revision changed from {recorded_commits.get(role)} "
                           f"to {wanted_commits[role]}")
    recorded_contract = record.get("ordered_patch_contract") or {}
    for family, entries in wanted["ordered_patch_contract"].items():
        before = [(entry["name"], entry["sha256"]) for entry in recorded_contract.get(family, [])]
        after = [(entry["name"], entry["sha256"]) for entry in entries]
        if before != after:
            words = changed_names(dict(before), dict(after))
            reasons.append(f"the {family} patch series changed: "
                           + ("; ".join(words) if words else "same patches, another order"))
    if wanted.get("hardware_error"):
        reasons.append(f"the engine checkout's hardware sources differ: {wanted['hardware_error']}")
    elif "hardware_sources" in wanted and record.get("hardware_sources") != wanted["hardware_sources"]:
        reasons.append("hardware sources changed: " + "; ".join(
            changed_names(record.get("hardware_sources"), wanted["hardware_sources"])))
    for option in ("cpu_optimization", "hardening", "file_prefix_map"):
        if record.get(option) != wanted[option]:
            reasons.append(f"{option} was {record.get(option)!r}, now {wanted[option]!r}")
    if bool(record.get("translator_link")) != wanted["link_translator"]:
        reasons.append("--link-translator differs from the earlier build")
    if record.get("pgo"):
        reasons.append(f"the earlier build was a profile-guided {record['pgo'].get('mode')} stage")
    if record.get("configure_pins"):
        reasons.append("the earlier build pinned its configure features")
    if wanted["cpu_optimization"] == "native" and "host_cpu" in wanted:
        if (record.get("host_compatibility") or {}).get("cpu") != wanted["host_cpu"]:
            reasons.append("the native build was made for another CPU")
    for name, version in (wanted.get("toolchain") or {}).items():
        recorded = ((record.get("toolchain") or {}).get(name) or {}).get("version")
        if recorded != version:
            reasons.append(f"{name} changed from {' '.join(recorded or ['none'])} to {' '.join(version or ['none'])}")
    for role, tree in (wanted.get("source_trees") or {}).items():
        if ((record.get("source_trees") or {}).get(role) or {}).get("sha256") != tree["sha256"]:
            reasons.append(f"the {role} checkout differs from the one the engine was built from")
    return reasons


def forced_rebuild(args):
    """Options that always build: --rebuild, a reproducibility check, a profile stage, configure pins."""
    if args.rebuild:
        return "--rebuild"
    if args.expect_engine_sha256:
        return "--expect-engine-sha256 checks a fresh build"
    if args.pgo != "off":
        return f"--pgo {args.pgo} builds a profile stage"
    if args.configure_pins:
        return "--configure-pins builds against a reference"
    return None


# What the base tooling must hold for the build to be skipped: the build makes them first when absent.
BASE_TOOLING = ("python/bin/python", "dependencies/lib/liblzfse.a", "build/qemu-system-x86_64")


def reuse_plan(args, root, sources, tooling):
    """Whether this run would rebuild the engine, and why, without building, fetching or writing anything."""
    engine = root / "sptm-build/qemu-system-aarch64"
    forced = forced_rebuild(args)
    if forced:
        return {"rebuild": True, "reasons": [forced], "engine": str(engine), "unchecked_checkouts": [],
                "engine_sha256": None, "manifest": None}
    manifest = root / "build-manifest.json"
    try:
        record = json.loads(manifest.read_text()) if manifest.is_file() else None
    except (OSError, ValueError):
        record = None
    engine_patches = selected_engine_patches(args.experimental_patch)
    graphics_patches = GRAPHICS_PATCHES + tuple(path for name, path in EXPERIMENTAL_GRAPHICS_PATCHES.items()
                                                if name in args.experimental_patch)
    wanted = {
        "source_commits": {"sptm": base.ENGINES["sptm"][1], "orchard": ORCHARD[1], "metal2vulkan": METAL2VULKAN[1]},
        "ordered_patch_contract": audit.patch_descriptors(
            {"engine": engine_patches, "graphics": graphics_patches, "translator": TRANSLATOR_PATCHES}),
        "cpu_optimization": args.cpu_optimization, "hardening": args.hardening,
        "file_prefix_map": list(args.file_prefix_map), "link_translator": bool(args.link_translator),
    }
    if record is not None:
        if args.cpu_optimization == "native":
            wanted["host_cpu"] = audit.host_receipt(True)["cpu"]
        wanted["toolchain"] = toolchain_versions()
        # Without the checkouts there is nothing local to compare; the pinned revisions and patches still are.
        if all(path.is_dir() for path in sources.values()):
            try:
                wanted["hardware_sources"] = stage_hardware(sources["sptm"], sources["orchard"], dry_run=True)
            except (OSError, RuntimeError) as error:
                wanted["hardware_error"] = str(error)
            wanted["source_trees"] = {role: audit.source_fingerprint(path, quiet_run)
                                      for role, path in sources.items()}
    reasons = reuse_reasons(record, wanted, engine)
    reasons += [f"the base tooling lacks {tooling / name}" for name in BASE_TOOLING if not (tooling / name).exists()]
    unchecked = [role for role, path in sources.items() if not path.is_dir()]
    return {"rebuild": bool(reasons), "reasons": reasons, "engine": str(engine),
            "unchecked_checkouts": unchecked if record is not None else [],
            "engine_sha256": record["engine_sha256"] if record is not None and not reasons else None,
            "manifest": str(manifest) if record is not None else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=base.REPO / ".tooling/ios-paravirt")
    parser.add_argument("--base-tooling", type=Path, default=base.REPO / ".tooling/ios-sptm")
    parser.add_argument("--orchard-source", type=Path)
    parser.add_argument("--metal2vulkan-source", type=Path)
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 2, 8))
    parser.add_argument("--cpu-optimization", choices=("portable", "native"), default="portable",
                        help="experimental native profile enables host-specific CPU code and QEMU link-time optimization")
    parser.add_argument("--hardening", choices=("qemu", "off"), default="qemu",
                        help="qemu keeps QEMU's compiler hardening (default); off (experimental) "
                             "drops its stack protector, zeroed locals and register clearing on return")
    parser.add_argument("--offline", action="store_true",
                        help="require local pinned sources and tooling; disable Cargo and QEMU downloads")
    parser.add_argument("--file-prefix-map", action="append", default=[], metavar="FROM=TO",
                        help="record source paths under FROM as if under TO (C: -ffile-prefix-map, "
                             "Rust: --remap-path-prefix), so a build elsewhere embeds the recorded tree's paths")
    parser.add_argument("--expect-engine-sha256", metavar="HEX",
                        help="reproducibility check: the manifest records whether the built engine has "
                             "this digest, and the build fails after publication when it does not")
    parser.add_argument("--configure-pins", metavar="PATH",
                        help="pin QEMU's host-detected features to a reference: a configured QEMU build "
                             "directory (its meson-info, config-host.h and Meson log) or the "
                             "configure-pins.json an earlier build wrote; the configured headers must "
                             "then equal the reference's, or the build stops before compiling")
    parser.add_argument("--experimental-patch", action="append", default=[],
                        choices=sorted({*EXPERIMENTAL_PATCHES, *EXPERIMENTAL_GRAPHICS_PATCHES}),
                        metavar="NAME",
                        help="Also apply this optional engine or graphics patch (repeatable; "
                             + ", ".join([*EXPERIMENTAL_PATCHES, *EXPERIMENTAL_GRAPHICS_PATCHES]) + ")")
    parser.add_argument("--pgo", choices=PGO_MODES, default="off",
                        help="profile-guided build: generate (stage 1) builds an instrumented engine "
                             "that writes its profile into --pgo-data when it exits normally; use "
                             "(stage 2) builds the same sources with that profile")
    parser.add_argument("--pgo-data", type=Path, metavar="DIR",
                        help="profile directory (absolute; baked into a stage-1 engine): c/ holds GCC's "
                             ".gcda files, rust/ the renderer's .profraw files (default for generate: "
                             "<output>/pgo-data; required for use)")
    parser.add_argument("--link-translator", action="store_true",
                        help="reach the translator through orchard/reims-vgpu/target/metal2vulkan, so "
                             "Cargo hashes that workspace-relative path instead of this checkout's "
                             "location into every crate's metadata and symbol names")
    parser.add_argument("--plan", action="store_true",
                        help="only say whether the engine would be rebuilt and why, as JSON; build, fetch and "
                             "write nothing")
    parser.add_argument("--rebuild", action="store_true",
                        help="build even when --output holds an engine built from the same pinned inputs")
    args = parser.parse_args()
    if platform.system() != "Linux":
        parser.error("The vphone Vulkan recipe targets Linux")
    if not 1 <= args.jobs <= 64:
        parser.error("--jobs must be between 1 and 64")
    try:
        selected_engine_patches(args.experimental_patch)
    except ValueError as error:
        parser.error(str(error))
    if args.pgo == "use" and args.pgo_data is None:
        parser.error("--pgo use needs --pgo-data, the stage-1 engine's profile directory")
    if args.pgo == "off" and args.pgo_data is not None:
        parser.error("--pgo-data needs --pgo generate or use")
    if args.pgo_data is not None and not args.pgo_data.is_absolute():
        parser.error("--pgo-data must be an absolute path (a stage-1 engine writes there)")
    root, tooling = args.output.resolve(), args.base_tooling.resolve()
    orchard = args.orchard_source.resolve() if args.orchard_source else (root / "orchard").resolve()
    translator = (args.metal2vulkan_source.resolve() if args.metal2vulkan_source
                  else (root / "metal2vulkan").resolve())
    source = (root / "sptm-source").resolve()
    sources = {"sptm": source, "orchard": orchard, "metal2vulkan": translator}
    plan = reuse_plan(args, root, sources, tooling)
    if args.plan:
        print(json.dumps(plan, indent=2))
        return
    if not plan["rebuild"]:
        print(json.dumps({**plan, "reused": True,
                          "note": "The engine in --output was built from the same pinned inputs; "
                                  "pass --rebuild to build it again."}, indent=2))
        return
    print("Building the engine: " + "; ".join(plan["reasons"]), flush=True)
    for command in ("git", "cargo", "ninja", "pkg-config", "cc", "c++"):
        if not shutil.which(command):
            parser.error(f"Missing build dependency: {command}")
    audit.validate_roles(root, tooling, sources)
    with audit.BuildSession(root, [tooling, *sources.values()], base.run) as session:
        build_candidate(args, root, tooling, orchard, translator, sources, session)


def build_candidate(args, root, tooling, orchard, translator, sources, session):
    session.phase("preflight")
    expected = expected_engine_digest(args.expect_engine_sha256)
    prefix_cflags, prefix_rustflags = prefix_map_flags(args.file_prefix_map)
    pins_path = Path(args.configure_pins).resolve() if args.configure_pins else None
    if pins_path and not pins_path.exists():
        raise RuntimeError(f"--configure-pins names nothing: {pins_path}")
    # A recorded file is complete now; a build directory needs the checked-out meson.build.
    pins = load_configure_pins(pins_path, None) if pins_path and pins_path.is_file() else None
    revisions = {"sptm": base.ENGINES["sptm"][1], "orchard": ORCHARD[1], "metal2vulkan": METAL2VULKAN[1]}
    audit.preflight_pins(sources, revisions, session.run, offline=args.offline)
    engine_patches = selected_engine_patches(args.experimental_patch)
    graphics_patches = GRAPHICS_PATCHES + tuple(path for name, path in EXPERIMENTAL_GRAPHICS_PATCHES.items()
                                                if name in args.experimental_patch)
    descriptors = audit.patch_descriptors({"engine": engine_patches, "graphics": graphics_patches,
                                            "translator": TRANSLATOR_PATCHES})
    deps, python = tooling / "dependencies", tooling / "python/bin/python"
    if not python.exists() or not (deps / "lib/liblzfse.a").exists():
        if args.offline:
            raise RuntimeError("--offline requires existing base tooling with Python and liblzfse")
        session.run([shutil.which("python3"), base.REPO / "tools/build_ios_emulator.py",
                  "--backend", "sptm", "--output", tooling, "--jobs", args.jobs])
    root.mkdir(parents=True, exist_ok=True)
    base.SOURCES["vphone-engine"] = base.ENGINES["sptm"]
    base.SOURCES["orchard"] = ORCHARD
    base.SOURCES["metal2vulkan"] = METAL2VULKAN
    source, build = sources["sptm"], root / "sptm-build"
    if args.offline:
        missing = [str(directory) for directory in (source, orchard, translator)
                   if not directory.is_dir()]
        if missing:
            raise RuntimeError("--offline requires existing pinned source checkouts: " + ", ".join(missing))
    base.checkout("vphone-engine", source)
    base.checkout("orchard", orchard)
    base.checkout("metal2vulkan", translator)
    session.phase("dependency-preflight")
    dependencies = audit.offline_dependencies(source, session.run) if args.offline else {}
    session.phase("patch-preflight")
    stage_patches(translator, TRANSLATOR_PATCHES, reject_untracked=True, dry_run=True)
    stage_patches(orchard, graphics_patches, reject_untracked=True, dry_run=True)
    stage_patches(source, engine_patches, dry_run=True)
    # Hardware shims can be modified by the graphics patch series, so this
    # preflight occurs after all patches are admitted but before hardware writes.
    session.phase("patch-staging")
    stage_patches(translator, TRANSLATOR_PATCHES, reject_untracked=True)
    stage_patches(orchard, graphics_patches, reject_untracked=True)
    stage_patches(source, engine_patches)
    gpu_link_args, runtime_record = (), None
    if args.pgo == "generate":
        runtime, runtime_record = llvm_profile_runtime(root, session.run, dict(os.environ))
        gpu_link_args = ("-Wl,--whole-archive", str(runtime), "-Wl,--no-whole-archive")
    session.phase("hardware-preflight")
    stage_hardware(source, orchard, dry_run=True, gpu_link_args=gpu_link_args)
    session.phase("hardware-staging")
    hardware = stage_hardware(source, orchard, gpu_link_args=gpu_link_args)
    if pins_path and pins is None:
        session.phase("configure-pins")
        pins = load_configure_pins(pins_path, source)
    if pins:
        audit.atomic_json(root / "configure-pins.json", pins)
    translator_link = None
    if args.link_translator:
        translator_link, link_map = link_translator(orchard, translator, args.file_prefix_map)
        prefix_rustflags = [*prefix_rustflags, link_map]
    session.phase("source-provenance")
    source_trees = {role: audit.source_fingerprint(path, session.run) for role, path in sources.items()}
    cargo_lock = audit.stable_file(orchard / "reims-vgpu/Cargo.lock")
    contract = hashlib.sha256(json.dumps({"patches": descriptors, "hardware": hardware},
                                        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    env = deterministic_environment({**os.environ, "PYTHONNOUSERSITE": "1", "CARGO_BUILD_JOBS": str(args.jobs),
                                     "PATH": str(python.parent) + os.pathsep + os.environ.get("PATH", "")})
    if args.offline:
        env["CARGO_NET_OFFLINE"] = "true"
    pgo_data = args.pgo_data or (root / "pgo-data")
    pgo_cflags, pgo_rustflags, pgo = pgo_plan(args.pgo, pgo_data, build, root, session.run, env)
    if runtime_record:
        pgo["rust_runtime"] = runtime_record
    rust_flags = [*audit.effective_rust_flags(env, args.cpu_optimization == "native"), *prefix_rustflags,
                  *pgo_rustflags]
    env["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(rust_flags)
    session.phase("toolchain-provenance")
    tools = audit.toolchain_receipt(env, session.run)
    host_compatibility = audit.host_receipt(args.cpu_optimization == "native")
    native_flags, native_target = [], None
    if args.cpu_optimization == "native":
        native_flags, native_target = native_target_flags(
            observe_native_target(shutil.which("cc", path=env["PATH"]), env))
    # This recipe owns the target path recorded in Meson's link dependency.
    env["CARGO_TARGET_DIR"] = str(orchard / "reims-vgpu/target")
    # Keep the upstream Cargo cache untouched. The separately pinned checkout and patch are the
    # translation source; its path dependency is already represented in the patched lockfile.
    translator_config = (f'patch."{METAL2VULKAN[0]}".metal2vulkan.path='
                         + json.dumps(str(translator_link or translator)))
    session.phase("graphics-build")
    session.run(["cargo", "--config", translator_config, "build", "--locked", "--release", "--manifest-path",
              orchard / "reims-vgpu/Cargo.toml", "-p", "reims-vgpu", "--no-default-features",
              "--features", "backend-vulkan,host-window"], env=env)
    build.mkdir(exist_ok=True)
    extra_cflags = [f"-I{deps / 'include'}"]
    if args.cpu_optimization == "native":
        extra_cflags += ["-O3", *native_flags]
    extra_cflags += hardening_overrides(args.hardening)
    extra_cflags += prefix_cflags
    extra_cflags += pgo_cflags
    # The LTO link generates code too, so it gets the same overrides (and the
    # profile flags: the instrumented link needs libgcov, the LTO link the profile).
    extra_ldflags = [f"-L{deps / 'lib'}", *hardening_overrides(args.hardening), *pgo_cflags]
    flags = ["--target-list=aarch64-softmmu", "--enable-lzfse", "--disable-pvg",
             "--disable-rust", "--disable-docs", "--disable-debug-info", "--disable-werror",
             "--disable-guest-agent", "--disable-sdl", "--disable-gtk", *base.UNUSED_HOST_LIBRARIES,
             "--extra-cflags=" + " ".join(extra_cflags), "--extra-ldflags=" + " ".join(extra_ldflags)]
    if args.offline:
        # QEMU's auto-FDT policy disables its internal copy in nodownload
        # mode even when cached. Select that existing dependency explicitly;
        # Meson still refuses a missing copy without fetching it.
        flags += ["--disable-download", "--enable-fdt=internal"]
    if args.cpu_optimization == "native":
        flags += ["--enable-lto"]
    if pins:
        flags = pinned_configure_flags(flags, pins["flags"])
    compilers = seeded_compilers(root, env)
    env = {**env, **compilers}
    session.phase("engine-configure")
    session.run([source / "configure", *flags], cwd=build, env=env)
    parity = None
    if pins:
        parity, drifted = configure_parity(build, pins["headers"])
        if drifted:
            raise RuntimeError("Configured headers drifted from the pinned reference ("
                               + ", ".join(drifted) + "):\n" + "\n".join(
                                   line for name in drifted for line in parity[name]["differences"]))
    session.phase("engine-build")
    session.run(["ninja", "-C", build, "-j", args.jobs, "qemu-system-aarch64"], env=env)
    session.phase("artifact-publication")
    engine = build / "qemu-system-aarch64"
    machines = session.run([engine, "-machine", "help"], capture=True).stdout
    if not any(line.split()[:1] == ["tomacco-vphone"] for line in machines.splitlines()):
        raise RuntimeError("Built engine does not expose tomacco-vphone")
    engine_artifact = audit.stable_file(engine, executable=True)
    graphics_artifact = audit.stable_file(orchard / "reims-vgpu/target/release/libreims_vgpu.a")
    profile = audit.compile_profile(build)
    # Detect source edits during the compile instead of signing stale provenance.
    final_trees = {role: audit.source_fingerprint(path, session.run) for role, path in sources.items()}
    if final_trees != source_trees:
        raise RuntimeError("Source changed during build; manifest was not published")
    if audit.patch_descriptors({"engine": engine_patches, "graphics": graphics_patches,
                                "translator": TRANSLATOR_PATCHES}) != descriptors:
        raise RuntimeError("Patch inputs changed during build; manifest was not published")
    record = {
        "backend": "vphone-research", "engine": str(engine), "engine_sha256": engine_artifact["sha256"],
        "machine": "tomacco-vphone", "graphics_backend": "reims-vgpu-vulkan",
        "source_commits": {"sptm": base.ENGINES["sptm"][1], "orchard": ORCHARD[1],
                           "metal2vulkan": METAL2VULKAN[1]},
        "patches": {p.name: base.sha256(p) for p in engine_patches}, "hardware_sources": hardware,
        "experimental_patches": [p.name for p in engine_patches if p not in PATCHES],
        "graphics_patches": {p.name: base.sha256(p) for p in graphics_patches},
        "experimental_graphics_patches": [p.name for p in graphics_patches if p not in GRAPHICS_PATCHES],
        "translator_patches": {p.name: base.sha256(p) for p in TRANSLATOR_PATCHES},
        "graphics_library_sha256": graphics_artifact["sha256"],
        "artifacts": {"engine": engine_artifact, "graphics": graphics_artifact},
        "source_trees": source_trees, "offline_dependencies": dependencies,
        "ordered_patch_contract": descriptors, "source_contract_sha256": contract,
        "cargo_lock": cargo_lock, "toolchain": tools,
        "host_compatibility": host_compatibility, "effective_rust_flags": rust_flags,
        "observed_compile_profile": profile,
        "configure": flags, "host": platform.machine(), "macos_vm_used": False,
        "cpu_optimization": args.cpu_optimization, "hardening": args.hardening,
        "host_specific_binary": args.cpu_optimization == "native",
        "offline_build": args.offline, "file_prefix_map": list(args.file_prefix_map),
        "configure_pins": pins and {key: pins[key] for key in ("reference", "flags", "evidence", "unpinned")},
        "configure_parity": parity,
        "translator_link": str(translator_link) if translator_link else None,
        "native_target": native_target, "seeded_compilers": compilers, "pgo": pgo,
        "rust_compiler_flags": env.get("CARGO_ENCODED_RUSTFLAGS", env.get("RUSTFLAGS", "")),
        "qualified_for_app_development": False, "sep_emulation_available": False,
        "sep_firmware_processor_available": True, "sep_services_qualified": False,
        "sep_manifest_loader_available": True, "sep_signature_verified_by_host": False,
        "sep_manifest_measurement_available": True, "sep_bootloader_policy_qualified": False,
        "sep_storage_transport_available": True, "sep_restore_handoff_available": True,
        "persistent_apple_virtio_storage_available": True,
        "apple_virtio_usb_transport_available": True,
        "display_panel_configuration_available": True,
    }
    record["reproducibility"] = reproducibility_check(record, expected)
    if pgo and pgo["mode"] == "generate":
        # The directory was empty before this build (pgo_plan), so every file
        # in it now came from configure's test programs or `-machine help`.
        pgo["build_time_profile_files_removed"] = clear_build_time_profile(pgo_data)
        marker = {"engine_sha256": record["engine_sha256"], "engine": str(engine), "data": str(pgo_data),
                  "c_flags": pgo["c_flags"], "rust_flags": pgo["rust_flags"],
                  "experimental_patches": record["experimental_patches"],
                  "experimental_graphics_patches": record["experimental_graphics_patches"],
                  "note": "instrumented stage-1 engine: train it, then build stage 2 with "
                          "--pgo use --pgo-data " + str(pgo_data) + " and the same patch options"}
        audit.atomic_json(root / PGO_MARKER, marker)
        audit.atomic_json(pgo_data / PGO_MARKER, marker)
    session.complete(record)
    print(json.dumps(record, indent=2))
    if record["reproducibility"] and not record["reproducibility"]["reproduced"]:
        raise RuntimeError(f"Engine {record['engine_sha256']} does not reproduce the expected {expected}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None
