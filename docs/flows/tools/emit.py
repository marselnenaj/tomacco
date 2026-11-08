#!/usr/bin/env python3
"""Print the Bogen batch steps (JSON) that replace one board's header,
canvas and footer. Target ids come from docs/flows/bogen-ids.json."""
import json, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import atlas
ROOT = pathlib.Path(__file__).resolve().parents[1]
ids = json.loads((ROOT / "bogen-ids.json").read_text())
bid = sys.argv[1]
parts = sys.argv[2:] or ["head", "canvas", "foot"]
b = next(x for x in atlas.load_boards() if x["id"] == bid)
rec = ids["boards"][bid]
html = {"head": atlas.header_html(b), "canvas": atlas.build(b)[0], "foot": atlas.footer_html(b)}
key = {"head": "header", "canvas": "canvas", "foot": "footer"}
steps = [{"tool": "html_write", "args": {"file_id": ids["file_id"], "page_id": ids["page_id"], "target_id": rec[key[p]], "mode": "replace", "html": html[p]}} for p in parts]
print(json.dumps(steps, ensure_ascii=False))
