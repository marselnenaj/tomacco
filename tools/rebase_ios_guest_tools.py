#!/usr/bin/env python3
"""Carry a verified guest-tools build over to another source trust cache.

A build's programs do not depend on the trust cache it was built for; only its
system.tc (the source cache plus the programs' own hashes) and the record of
that source do. A new graphics bridge brings a new source cache, and this
writes a copy of the build for it without recompiling or re-signing anything:
the build is first verified completely against its own source, and the copy
against the new one.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from build_ios_guest_tools import extend_trustcache
from ios_guest_tools import payload
from ios_paravirt_firmware import read_component


def rebase(tools, source, trustcache, output):
    tools, output = tools.absolute(), output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; choose a new directory")
    read = lambda path: read_component(path.parent, path.name, 4 << 20)
    original, replacement = read(source), read(trustcache)
    record, _files, _cache = payload(tools, original)
    cache = extend_trustcache(replacement, [bytes.fromhex(row["cdhash"]) for row in record["programs"]])
    record["trustcache"] = {**record["trustcache"], "source_sha256": hashlib.sha256(replacement).hexdigest(),
                            "sha256": hashlib.sha256(cache).hexdigest()}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".tomacco-guest-tools-", dir=output.parent) as temporary:
        stage = Path(temporary) / "tools"
        shutil.copytree(tools, stage, symlinks=True)
        (stage / "system.tc").write_bytes(cache)
        (stage / "guest-tools.json").write_text(json.dumps(record, indent=2) + "\n")
        payload(stage, replacement)
        stage.rename(output)
    return {"tools": str(output), "rebased_from": str(tools),
            "previous_source_sha256": hashlib.sha256(original).hexdigest(),
            "source_sha256": record["trustcache"]["source_sha256"], "sha256": record["trustcache"]["sha256"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", type=Path, required=True, help="Verified guest-tools build")
    parser.add_argument("--source-trustcache", type=Path, required=True, help="Trust cache the build was made for")
    parser.add_argument("--trustcache", type=Path, required=True, help="New source trust cache")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(rebase(args.tools, args.source_trustcache, args.trustcache, args.output), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error))
