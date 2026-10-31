#!/usr/bin/env python3
"""Build the pinned Rust host-only virtio-vsock backend on Linux.

Only open-source Rust code is fetched. No Apple SDK, firmware or host vsock
kernel module is required. Supply an existing pinned --source for offline use.
The resulting executable is the paravirtual job's guest_control_backend.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import tempfile

import build_ios_emulator as base
from build_ios_paravirt import stage_file, stage_patches

SOURCE = ("https://github.com/rust-vmm/vhost-device.git",
          "842abb0dff435bf2c0497f7a24e9f7e419c6098e")
INTEGRATION = base.REPO / "integrations/ios-emulator"
PATCH = INTEGRATION / "vhost-vsock-scatter.patch"
PACKET = INTEGRATION / "vsock-tx-packet.rs"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New build receipt directory")
    parser.add_argument("--source", type=Path, help="Existing unmodified checkout at the pinned commit")
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 2, 8))
    parser.add_argument("--offline", action="store_true", help="Use only existing source and cached Cargo dependencies")
    args = parser.parse_args()
    if platform.system() != "Linux" or not 1 <= args.jobs <= 64:
        parser.error("Requires Linux and --jobs between 1 and 64")
    for command in ("git", "cargo"):
        if not shutil.which(command):
            parser.error("Missing dependency: " + command)
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        parser.error("Output exists; choose a new directory")
    if args.offline and (not args.source or not args.source.is_dir()):
        parser.error("Offline builds require an existing --source checkout")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".tomacco-vsock-", dir=output.parent) as temporary:
        stage = Path(temporary)
        source = args.source.resolve() if args.source else stage / "source"
        base.SOURCES["guest-vsock"] = SOURCE
        base.checkout("guest-vsock", source)
        untracked = base.run(["git", "-C", source, "ls-files", "--others", "--exclude-standard"], capture=True).stdout.splitlines()
        if set(untracked) - {"vhost-device-vsock/src/tx_packet.rs"}:
            raise RuntimeError("Preserved untracked source; use a clean pinned checkout")
        stage_patches(source, [PATCH])
        stage_file(source / "vhost-device-vsock/src/tx_packet.rs", PACKET.read_bytes())
        command = ["cargo", "build", "--release", "--locked", "--package", "vhost-device-vsock",
                   "--no-default-features", "--jobs", str(args.jobs)]
        if args.offline:
            command.append("--offline")
        # An external CARGO_TARGET_DIR must not redirect the recorded binary.
        env = {**os.environ, "CARGO_TARGET_DIR": str(source / "target")}
        base.run(command, cwd=source, env=env)
        executable = stage / "vhost-device-vsock"
        shutil.copy2(source / "target/release/vhost-device-vsock", executable)
        executable.chmod(0o755)
        version = base.run([executable, "--version"], capture=True).stdout.strip()
        record = {"format": 1, "source": SOURCE[0], "commit": SOURCE[1],
                  "version": version, "features": [], "backend": "unix-domain-socket",
                  "patches": {PATCH.name: base.sha256(PATCH)},
                  "sources": {PACKET.name: base.sha256(PACKET)},
                  "host_vsock_kernel_module_required": False,
                  "shared_dirty_logging": True,
                  "executable": str(output / executable.name), "sha256": base.sha256(executable),
                  "cargo_lock_sha256": base.sha256(source / "Cargo.lock"),
                  "guest_execution_verified": False}
        (stage / "build-manifest.json").write_text(json.dumps(record, indent=2) + "\n")
        stage.rename(output)
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
