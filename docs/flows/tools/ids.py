#!/usr/bin/env python3
"""Record replaced Bogen ids: ids.py D01 header=x canvas=y footer=z artboard=w"""
import json, sys, pathlib
p = pathlib.Path(__file__).resolve().parents[1] / "bogen-ids.json"
d = json.loads(p.read_text())
b = d["boards"].setdefault(sys.argv[1], {})
for kv in sys.argv[2:]:
    k, v = kv.split("=", 1)
    b[k] = v
p.write_text(json.dumps(d, ensure_ascii=False, indent=1))
print(sys.argv[1], b)
