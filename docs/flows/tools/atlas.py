#!/usr/bin/env python3
"""Generator for the Tomacco flow/logic atlas on Bogen page 04.

A diagram spec (docs/flows/specs/*.py, one BOARDS list each) becomes:
  * compact HTML for Bogen html_write: Logic / Node instances placed absolutely,
    one editable SVG connector overlay with real arrow heads, and edge labels
    as editable text layers beside the lines;
  * a machine-readable record (nodes, edges, branch labels, label boxes,
    operations, sources) that build_map.py folds into docs/flows/atlas-map.json.

Text is measured with the real brand fonts (assets/fonts/archivo-var.ttf,
jetbrains-mono-var.ttf): every node grows until its wrapped text fits inside
its border (and inside the diamond for decisions); edge labels are placed in
free space, never on a node, an arrow head or another label.

Usage: atlas.py html D07 [head|canvas|foot]  -> canvas HTML of one board
       atlas.py json                         -> docs/flows/build/diagrams.json
       atlas.py check [D07 ...]              -> node overlap, edge-through-node,
                                                text fit, label collisions
No network, no device access. Pure function of the spec files.
"""
import html
import importlib.util
import json
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
SPECS = ROOT / "specs"
FONTS = REPO / "assets" / "fonts"

# Product source the anchors are written against (installed desktop).
SOURCE_BASELINE = "4584d6b"

def provenance():
    """Current qualified product; footer source anchors keep their own baseline."""
    path = ROOT / "provenance-current.json"
    return json.loads(path.read_text()) if path.exists() else {
        "product_source": "4584d6bca3a07840e81683d99cd0adb386aeff35",
        "core_source_baseline": "4584d6bca3a07840e81683d99cd0adb386aeff35",
        "desktop_installed": True, "source_keys": 1894,
    }


# Grid: 5 columns of 272 px with 56 px gaps fill the 1584 px canvas exactly.
COLW, GAPX, ROWH, GAPY = 272, 56, 120, 64
PX, PY = COLW + GAPX, ROWH + GAPY
CANVAS_W = 1584
DEC_H = 144
LABEL_H = 26          # 16 px Archivo 500, line-height 1.35, padding 2 px

STROKE = {"n": "var(--fg)", "err": "var(--app-danger)", "ok": "var(--app-success)",
          "warn": "var(--app-warning)", "dim": "var(--app-faint)"}
DASH = {"n": None, "err": "8 6", "ok": None, "warn": "2 5", "dim": "3 5"}

# ---------------------------------------------------------------- text metrics
_FONTS = {}


def _font(kind, size, wght):
    key = (kind, size, wght)
    if key not in _FONTS:
        from PIL import ImageFont
        f = ImageFont.truetype(str(FONTS / ("archivo-var.ttf" if kind == "sans" else "jetbrains-mono-var.ttf")), size)
        f.set_variation_by_axes([wght, 100] if kind == "sans" else [wght])
        _FONTS[key] = f
    return _FONTS[key]


def textw(s, kind="sans", size=16, wght=500, ls=0.0):
    return _font(kind, size, wght).getlength(s) + ls * size * len(s)


def wrap(text, maxw, kind, size, wght, anywhere=False):
    """Greedy CSS-like wrapping (pre-wrap). Returns (lines, overflow_px)."""
    lines, over = [], 0.0
    W = lambda t: textw(t, kind, size, wght)
    for para in str(text).split("\n"):
        cur = ""
        for word in para.split(" "):
            cand = word if not cur else cur + " " + word
            if W(cand) <= maxw:
                cur = cand
                continue
            if cur:
                lines.append(cur)
                cur = ""
            if W(word) <= maxw:
                cur = word
                continue
            # one word wider than the line: hyphen breaks, then (mono) anywhere
            pieces = []
            for i, part in enumerate(word.split("-")):
                pieces.append(part + ("-" if i < word.count("-") else ""))
            for part in pieces:
                cand = cur + part
                if W(cand) <= maxw:
                    cur = cand
                    continue
                if cur:
                    lines.append(cur)
                    cur = ""
                if W(part) <= maxw or not anywhere:
                    over = max(over, W(part) - maxw)
                    cur = part
                    continue
                for ch in part:
                    if W(cur + ch) <= maxw:
                        cur += ch
                    else:
                        lines.append(cur)
                        cur = ch
        lines.append(cur)
    return lines, over


T_LH, O_LH, TAG_LH = 18 * 1.3, 14 * 1.35, 14 * 1.2


def node_need(n, w, h=None):
    """Height the node's text needs at width w; for decisions the smallest
    diamond height (>= h) whose sides clear every line. Returns (need, problem)."""
    if n["kind"] != "decision":
        inner = w - 36                       # 2 px border + 16 px padding each side
        tl, tover = wrap(n["title"], inner - 2, "sans", 18, 600)
        need = 4 + 24 + TAG_LH + 4 + len(tl) * T_LH
        oover = 0
        if n.get("op"):
            ol, oover = wrap(n["op"], inner - 2, "mono", 14, 400, anywhere=True)
            need += 4 + len(ol) * O_LH
        tag = n.get("tag") or DEFAULT_TAG[n["kind"]]
        tagover = textw(tag, "mono", 14, 400, .06) - inner
        prob = None
        if max(tover, oover, tagover) > 0:
            prob = "text wider than node by %dpx" % math.ceil(max(tover, oover, tagover))
        return math.ceil(need + 2), prob
    inner = w - 112
    tl, tover = wrap(n["title"], inner - 2, "sans", 18, 600)
    ol = []
    broken = None
    if n.get("op"):
        ol, _ = wrap(n["op"], inner - 2, "mono", 14, 400, anywhere=True)
        if any(textw(wd, "mono", 14, 400) > inner - 2 for wd in n["op"].split()):
            broken = "decision op breaks inside a word"
    rows = [(T_LH, textw(t, "sans", 18, 600)) for t in tl] + [(O_LH, textw(t, "mono", 14, 400)) for t in ol]
    block = sum(r[0] for r in rows) + (4 if ol else 0)
    hh = h or DEC_H
    while hh <= 320:
        top, ok = (hh - block) / 2, block <= hh - 24
        y = top
        for i, (lh, lw) in enumerate(rows):
            if ol and i == len(tl):
                y += 4
            d = max(abs(y - hh / 2), abs(y + lh - hh / 2))
            if lw > w * (1 - 2 * d / hh) - 10:
                ok = False
            y += lh
        if ok:
            return hh, ("title wider than diamond by %dpx" % math.ceil(tover)) if tover > 0 else broken
        hh += 8
    return hh, "decision text does not fit a diamond up to 320px"


# ---------------------------------------------------------------- spec helpers
def load_boards():
    boards = []
    for path in sorted(SPECS.glob("[0-9]*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        mod = importlib.util.module_from_spec(spec)
        mod.N, mod.E = N, E
        spec.loader.exec_module(mod)
        for b in mod.BOARDS:
            b.setdefault("spec_file", str(path.relative_to(REPO)))
            boards.append(b)
    return boards


ROWPAD = 0


def N(id, kind, c, r, title, op=None, **kw):
    """A node in grid cell (c, r); fractional cells are allowed."""
    d = {"id": id, "kind": kind, "c": c, "r": r, "title": title}
    if op:
        d["op"] = op
    d.update(kw)
    return d


def E(f, t, label=None, **kw):
    d = {"f": f, "t": t}
    if label:
        d["label"] = label
    d.update(kw)
    return d


def node_box(n):
    """n = dict(id, kind, c, r, [w, h, x, y]) -> x, y, w, h in canvas px.
    The height grows (centred on the row) until the text fits."""
    w = n.get("w") or (COLW * n.get("span", 1) + GAPX * (n.get("span", 1) - 1))
    h0 = n.get("h") or (DEC_H if n["kind"] == "decision" else ROWH)
    need, prob = node_need(n, w, h0)
    h = max(h0, int(math.ceil(need / 4.0) * 4), n.get("_hrow", 0))
    n["_fit"] = prob
    n["_grown"] = h - h0
    x = n["x"] if "x" in n else round(n["c"] * PX)
    if "y" in n:
        y = n["y"] - (h - h0) // 2
    else:
        y = round(n["r"] * (PY + ROWPAD)) + ROWPAD + (ROWH - h) // 2
    return x, y, w, h


def port(box, side, frac=0.5):
    x, y, w, h = box
    return {"r": (x + w, y + h * frac), "l": (x, y + h * frac),
            "t": (x + w * frac, y), "b": (x + w * frac, y + h)}[side]


def auto_sides(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    acx, acy, bcx, bcy = ax + aw / 2, ay + ah / 2, bx + bw / 2, by + bh / 2
    if abs(acy - bcy) < 2:
        return ("r", "l") if bcx > acx else ("l", "r")
    if abs(acx - bcx) < 2:
        return ("b", "t") if bcy > acy else ("t", "b")
    if by >= ay + ah:  # target below
        return ("b", "t")
    if by + bh <= ay:  # target above
        return ("t", "b")
    return ("r", "l") if bcx > acx else ("l", "r")


def route(p, q, s1, s2, via):
    """Orthogonal polyline from p (leaving s1) to q (entering s2)."""
    pts = [p]
    for v in via or []:
        last = pts[-1]
        if last[0] != v[0] and last[1] != v[1]:
            pts.append((v[0], last[1]) if len(pts) == 1 and s1 in "rl" else (last[0], v[1]) if len(pts) == 1 else (v[0], last[1]))
        pts.append(v)
    last = pts[-1]
    if last[0] != q[0] and last[1] != q[1]:
        if via:
            pts.append((q[0], last[1]) if s2 in "tb" else (last[0], q[1]))
        elif s1 in "rl" and s2 in "rl":
            mx = round((last[0] + q[0]) / 2)
            pts += [(mx, last[1]), (mx, q[1])]
        elif s1 in "tb" and s2 in "tb":
            my = round((last[1] + q[1]) / 2)
            pts += [(last[0], my), (q[0], my)]
        elif s1 in "rl":
            pts.append((q[0], last[1]))
        else:
            pts.append((last[0], q[1]))
    pts.append(q)
    clean = []
    for pt in pts:
        pt = (round(pt[0]), round(pt[1]))
        if not clean or clean[-1] != pt:
            clean.append(pt)
    out = []
    for pt in clean:
        if len(out) >= 2 and ((out[-2][0] == out[-1][0] == pt[0]) or (out[-2][1] == out[-1][1] == pt[1])):
            out[-1] = pt
        else:
            out.append(pt)
    return out


def arrow(pts, color):
    (x1, y1), (x2, y2) = pts[-2], pts[-1]
    s = 9
    if x1 == x2:
        d = 1 if y2 > y1 else -1
        tri = [(x2, y2), (x2 - s * .62, y2 - d * s * 1.3), (x2 + s * .62, y2 - d * s * 1.3)]
    else:
        d = 1 if x2 > x1 else -1
        tri = [(x2, y2), (x2 - d * s * 1.3, y2 - s * .62), (x2 - d * s * 1.3, y2 + s * .62)]
    return '<polygon points="%s" fill="%s"></polygon>' % (" ".join("%g,%g" % (round(a, 1), round(b, 1)) for a, b in tri), color)


def trim(pts, n=10):
    """Shorten the last segment so the line ends under the arrow head."""
    (x1, y1), (x2, y2) = pts[-2], pts[-1]
    if x1 == x2:
        y2 = y2 - n if y2 > y1 else y2 + n
    else:
        x2 = x2 - n if x2 > x1 else x2 + n
    return pts[:-1] + [(x2, y2)]


def label_pos(pts, e):
    """Kept for external reviewers: build() stores the chosen spot in e['lp']."""
    if "lp" in e:
        return e["lp"]
    seg = e.get("lseg", 0)
    (x1, y1), (x2, y2) = pts[seg], pts[seg + 1]
    w = 9 * len(e.get("label", "")) + 14
    if y1 == y2:
        return (x1 + 8, y1 - 30) if x2 > x1 else (x1 - 8 - w, y1 - 30)
    if y2 > y1:
        return (x1 + 8, y1 + 6)
    return (x1 + 8, y1 - 34)


# ---------------------------------------------------------------- geometry
def rect_poly(x, y, w, h):
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]


def shape(kind, box, grow=0):
    x, y, w, h = box
    x, y, w, h = x - grow, y - grow, w + 2 * grow, h + 2 * grow
    if kind == "decision":
        return [(x + w / 2, y), (x + w, y + h / 2), (x + w / 2, y + h), (x, y + h / 2)]
    return rect_poly(x, y, w, h)


def poly_hit(a, b):
    for poly in (a, b):
        for p, q in zip(poly, poly[1:] + poly[:1]):
            ax_ = (p[1] - q[1], q[0] - p[0])
            pa = [x * ax_[0] + y * ax_[1] for x, y in a]
            pb = [x * ax_[0] + y * ax_[1] for x, y in b]
            if max(pa) <= min(pb) or max(pb) <= min(pa):
                return False
    return True


def seg_hits_rect(p, q, r):
    (x1, y1), (x2, y2) = p, q
    rx, ry, rw, rh = r
    if y1 == y2:
        return ry < y1 < ry + rh and min(x1, x2) < rx + rw and max(x1, x2) > rx
    return rx < x1 < rx + rw and min(y1, y2) < ry + rh and max(y1, y2) > ry


def collinear_overlap(a, b):
    (p1, p2), (q1, q2) = a, b
    if p1[0] == p2[0] == q1[0] == q2[0]:
        lo, hi = max(min(p1[1], p2[1]), min(q1[1], q2[1])), min(max(p1[1], p2[1]), max(q1[1], q2[1]))
        return hi - lo > 4
    if p1[1] == p2[1] == q1[1] == q2[1]:
        lo, hi = max(min(p1[0], p2[0]), min(q1[0], q2[0])), min(max(p1[0], p2[0]), max(q1[0], q2[0]))
        return hi - lo > 4
    return False


def label_candidates(pts, w, h, hint, others=()):
    segs = list(zip(pts, pts[1:]))
    order = [0] + ([len(segs) - 1] if len(segs) > 1 else []) + list(range(1, len(segs) - 1))
    own = [si for si in order if not any(collinear_overlap(segs[si], o) for o in others)]
    order = own + [si for si in order if si not in own]
    out = []
    if hint:
        out.append((hint[0], hint[1], 0, -1))
    for rank, si in enumerate(order):
        (x1, y1), (x2, y2) = segs[si]
        if y1 == y2:
            d = 1 if x2 > x1 else -1
            xs = [x1 + 10 if d > 0 else x1 - 10 - w, min(x1, x2) + (abs(x2 - x1) - w) / 2, x2 - 18 - w if d > 0 else x2 + 18]
            for i, x in enumerate(xs):
                for j, y in enumerate((y1 - h - 8, y1 + 8)):
                    out.append((x, y, rank * 3 + i + j * .5, si))
        else:
            d = 1 if y2 > y1 else -1
            ys = [y1 + 8 if d > 0 else y1 - 8 - h, (y1 + y2) / 2 - h / 2, y2 - 16 - h if d > 0 else y2 + 16,
                  y1 + 2 if d > 0 else y1 - 2 - h]
            for i, y in enumerate(ys):
                for j, x in enumerate((x1 + 10, x1 - 10 - w)):
                    out.append((x, y, rank * 3 + i + j * .5, si))
    return out


def place_labels(board, nodes, boxes, out_edges, edges, height, lane_boxes):
    obstacles = [(k, shape(nodes[k]["kind"], boxes[k], 3)) for k in boxes]
    def tip(pts):
        (x1, y1), (x2, y2) = pts[-2], pts[-1]
        if x1 == x2:
            return rect_poly(x2 - 7, min(y2, y2 - 13 if y2 > y1 else y2 + 13), 14, 13)
        return rect_poly(min(x2, x2 - 13 if x2 > x1 else x2 + 13), y2 - 7, 13, 14)
    tips = [tip(e["points"]) for e in out_edges]
    lanes = [rect_poly(*b) for b in lane_boxes]
    allsegs = [(i, p, q) for i, e in enumerate(out_edges) for p, q in zip(e["points"], e["points"][1:])]
    placed, problems = [], []
    for i, (e, rec) in enumerate(zip(edges, out_edges)):
        if not e.get("label"):
            continue
        w = math.ceil(textw(e["label"], "sans", 16, 500)) + 14
        h = LABEL_H
        hint = e.get("_lphint")
        hint = (hint[0], hint[1] + board["_oy"]) if hint else None
        best = None
        others = [(p, q) for j, p, q in allsegs if j != i]
        for x, y, rank, si in label_candidates(rec["points"], w, h, hint, others):
            x, y = round(x), round(y)
            box = rect_poly(x, y, w, h)
            hard = 0
            if x < 0 or y < 0 or x + w > CANVAS_W or y + h > height:
                hard += 1
            hard += sum(1 for _, o in obstacles if poly_hit(box, o))
            hard += sum(1 for t in tips if poly_hit(box, t))
            hard += sum(1 for pl in placed if poly_hit(rect_poly(x - 4, y - 4, w + 8, h + 8), pl[1]))
            hard += sum(1 for ln in lanes if poly_hit(box, ln))
            cross = sum(1 for j, p, q in allsegs if seg_hits_rect(p, q, (x, y, w, h)))
            score = hard * 1000 + cross * 6 + rank
            if best is None or score < best[0]:
                best = (score, x, y, hard)
        _, x, y, hard = best
        if hard:
            problems.append("label %s->%s '%s' collides" % (e["f"], e["t"], e["label"]))
        e["lp"] = (x, y - board["_oy"])
        rec["label_box"] = [x, y, w, h]
        placed.append((e["label"], rect_poly(x, y, w, h)))
    return problems


def esc(t):
    return html.escape(str(t), quote=True)


def build(board):
    global ROWPAD
    ROWPAD = board.get("rowpad", 0)
    nodes = {n["id"]: n for n in board["nodes"]}
    # nodes of one row share a height, so that straight edges stay straight
    rows = {}
    for n in board["nodes"]:
        n.pop("_hrow", None)
        if n["kind"] not in ("decision", "note") and "y" not in n and "h" not in n and not board.get("no_row_equalize"):
            rows.setdefault(n["r"], []).append(n)
    for group in rows.values():
        hs = [node_box(n)[3] for n in group]
        for n in group:
            n["_hrow"] = max(hs)
    boxes = {k: node_box(n) for k, n in nodes.items()}
    oy = board.get("oy", 0 if board.get("lanes") else 16)
    board["_oy"] = oy
    boxes = {k: (x, y + oy, w, h) for k, (x, y, w, h) in boxes.items()}
    height = board.get("height") or (max(b[1] + b[3] for b in boxes.values()) + 24)
    height = max(height, max(b[1] + b[3] for b in boxes.values()) + 24)
    edges = board.get("edges", [])
    out_edges, svg = [], []
    for e in edges:
        if "_lphint" not in e:
            e["_lphint"] = e.get("lp")
        e.pop("lp", None)
        a, b = boxes[e["f"]], boxes[e["t"]]
        s1, s2 = auto_sides(a, b)
        s1, s2 = e.get("s1", s1), e.get("s2", s2)
        fo = .5 if nodes[e["f"]]["kind"] == "decision" else e.get("fo", .5)
        to = .5 if nodes[e["t"]]["kind"] == "decision" else e.get("to", .5)
        p, q = port(a, s1, fo), port(b, s2, to)
        via = [(v[0], v[1] + oy) for v in e.get("via") or []]
        if via and nodes[e["f"]]["kind"] == "decision":
            # branches leave a decision at its tip and fan out as a bus
            via[0] = (p[0], via[0][1]) if s1 in "tb" else (via[0][0], p[1])
        pts = route(p, q, s1, s2, via)
        style = e.get("style", "n")
        color, dash = STROKE[style], DASH[style]
        line = trim(pts)
        if e.get("both"):
            line = list(reversed(trim(list(reversed(line)))))
        svg.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="2"%s></polyline>' % (
            " ".join("%g,%g" % pt for pt in line), color, ' stroke-dasharray="%s"' % dash if dash else ""))
        svg.append(arrow(pts, color))
        if e.get("both"):
            svg.append(arrow(list(reversed(pts)), color))
        out_edges.append({"from": e["f"], "to": e["t"], "label": e.get("label", ""),
                          "kind": {"n": "flow", "err": "error", "ok": "success", "warn": "human/approval", "dim": "unsupported/optional"}[style],
                          "points": pts})
    lane_html, lane_boxes = [], []
    vsegs = [(x1, min(y1, y2), max(y1, y2)) for e in out_edges for (x1, y1), (x2, y2) in zip(e["points"], e["points"][1:]) if x1 == x2]
    for lane in board.get("lanes", []):
        y = lane.get("y", round(lane.get("r", 0) * (PY + ROWPAD)))
        h = lane.get("h", round(lane.get("rows", 1) * (PY + ROWPAD)))
        w = math.ceil(textw(lane["label"].upper(), "mono", 14, 400, .06)) + 8
        lx = 0
        while lx + w < CANVAS_W and (any(lx - 6 <= vx <= lx + w + 6 and a0 < y + 36 and a1 > y for vx, a0, a1 in vsegs)
                                     or any(poly_hit(rect_poly(lx, y + 4, w, 24), shape(nodes[k]["kind"], boxes[k])) for k in boxes)):
            lx += 8
        lane_boxes.append((lx, y + 6, w, 22))
        lane_html.append('<div data-name="Bahn · %s" style="position:absolute;left:0;top:%dpx;width:%dpx;height:%dpx;border-top:2px solid var(--rule);box-sizing:border-box"><div style="position:absolute;left:%dpx;top:8px;font-family:var(--mono);font-size:14px;letter-spacing:.06em;color:var(--at);white-space:nowrap">%s</div></div>' % (esc(lane["label"]), y, CANVAS_W, h, lx, esc(lane["label"].upper())))
    for col in board.get("columns", []):
        x = col.get("x", round(col.get("c", 0) * PX))
        w = col.get("w", COLW)
        lane_boxes.append((x, 0, w, 24))
        lane_html.append('<div data-name="Spalte · %s" style="position:absolute;left:%dpx;top:0;width:%dpx;height:%dpx;border-left:2px solid var(--rule);box-sizing:border-box;padding-left:12px;font-family:var(--mono);font-size:14px;letter-spacing:.06em;color:var(--at)">%s</div>' % (esc(col["label"]), x, w, height, esc(col["label"].upper())))
    label_problems = place_labels(board, nodes, boxes, out_edges, edges, height, lane_boxes)
    board["_label_problems"] = label_problems
    labels = []
    for e, rec in zip(edges, out_edges):
        if not e.get("label"):
            continue
        x, y = rec["label_box"][:2]
        col = {"err": "var(--app-danger)", "ok": "var(--app-success)", "warn": "var(--app-warning)"}.get(e.get("style", "n"), "var(--fg)")
        labels.append('<div class="lb" data-name="Label %s→%s" style="left:%dpx;top:%dpx;color:%s">%s</div>' % (
            e["f"], e["t"], x, y, col, esc(e["label"])))
    parts = ['<style>.lb{position:absolute;padding:2px 6px;background:var(--g);font-family:var(--sans);font-size:16px;font-weight:500;line-height:1.35;white-space:nowrap}.nx{position:absolute;min-height:0}</style><div data-name="Diagramm · %s" style="position:relative;width:%dpx;height:%dpx;flex-shrink:0">' % (board["id"], CANVAS_W, height)]
    parts += lane_html
    parts.append('<svg data-name="Connectoren · %s" viewBox="0 0 %d %d" width="%d" height="%d" style="position:absolute;left:0;top:0;width:%dpx;height:%dpx;overflow:visible">%s</svg>' % (
        board["id"], CANVAS_W, height, CANVAS_W, height, CANVAS_W, height, "".join(svg)))
    for k, n in nodes.items():
        x, y, w, h = boxes[k]
        attrs = ['component="Logic / Node"', 'prop-kind="%s"' % n["kind"], 'prop-title="%s"' % esc(n["title"])]
        if n["kind"] != "decision":
            attrs.append('prop-tag="%s"' % esc(n.get("tag") or DEFAULT_TAG[n["kind"]]))
        if n.get("op"):
            attrs.append('prop-op="%s"' % esc(n["op"]))
        else:
            attrs.append('prop-show-op="false"')
        parts.append('<x-instance %s class="nx" data-name="%s · %s" style="left:%dpx;top:%dpx;width:%dpx;height:%dpx"></x-instance>' % (
            " ".join(attrs), board["id"], esc(k), x, y, w, h))
    parts += labels
    parts.append("</div>")
    record = {"id": board["id"], "title": board["title"], "type": board["type"], "lead": board.get("lead", ""),
              "nodes": [{"id": k, "kind": n["kind"], "title": n["title"], "op": n.get("op", ""), "tag": n.get("tag") or DEFAULT_TAG[n["kind"]],
                         "box": list(boxes[k])} for k, n in nodes.items()],
              "edges": out_edges, "operations": board.get("ops", []), "sources": board.get("sources", []),
              "notes": board.get("notes", []), "links": board.get("links", []), "screens": board.get("screens", []),
              "status": board.get("status", ""), "routes": board.get("routes", []), "height": height,
              "spec_file": board.get("spec_file", ""), "source_baseline_label": board.get("source_label", "Kern " + SOURCE_BASELINE)}
    return "".join(parts), record


DEFAULT_TAG = {"start": "START", "step": "SCHRITT", "state": "ZUSTAND", "human": "MENSCH", "ok": "ENDE · OK",
               "error": "FEHLER", "stop": "ABBRUCH", "unsupported": "NICHT UNTERSTÜTZT", "note": "HINWEIS", "decision": ""}


def header_html(b):
    kicker = "%s · %s" % (b["id"], b["type"].upper())
    return ('<header data-name="Kopf · %s" style="display:flex;align-items:flex-start;justify-content:space-between;gap:48px;font-family:var(--sans);color:var(--fg)">'
            '<div style="display:flex;flex-direction:column;gap:12px;max-width:1240px">'
            '<div data-name="Kennung" style="font-family:var(--mono);font-size:16px;letter-spacing:.06em;color:var(--at)">%s</div>'
            '<h2 data-name="Titel" style="margin:0;font-size:var(--text-docs-title);font-weight:900;line-height:.98;letter-spacing:-.04em;text-wrap:balance">%s</h2>'
            '<p data-name="Lead" style="margin:0;font-size:20px;line-height:1.45;color:var(--dim)">%s</p></div>'
            '<div data-name="Link · Übersicht" style="flex-shrink:0;display:flex;align-items:center;min-height:48px;padding:0 16px;border:2px solid var(--rule);font-family:var(--mono);font-size:16px;color:var(--fg);white-space:nowrap">← 00 Übersicht</div>'
            '</header>') % (b["id"], esc(kicker), esc(b["title"]), esc(b["lead"]))


def footer_html(b):
    src = "".join('<li class="sl">%s</li>' % esc(x) for x in b.get("sources", []))
    notes = "".join('<p class="sl">%s</p>' % esc(x) for x in b.get("notes", []))
    links = "".join('<div class="fl" data-name="Link → %s">→ %s</div>' % (esc(l[0]), esc(l[0] + " " + l[1])) for l in b.get("links", []))
    links += "".join('<div class="fl" data-name="Screen → %s">Screen · %s</div>' % (esc(s[1]), esc(s[2])) for s in b.get("screens", []))
    k = 'kk'
    return ('<style>.sl{margin:0}.fl{display:flex;align-items:center;min-height:44px;padding:0 14px;border:2px solid var(--rule);font-family:var(--mono);font-size:15px;color:var(--fg);white-space:nowrap}.kk{font-family:var(--mono);font-size:14px;letter-spacing:.06em;color:var(--at)}</style>'
            '<footer data-name="Fuß · %s" style="display:grid;grid-template-columns:1fr 1fr;gap:24px 56px;padding-top:24px;border-top:2px solid var(--rule);font-family:var(--sans);color:var(--dim)">'
            '<div data-name="Grenzen" style="display:flex;flex-direction:column;gap:12px;font-size:18px;line-height:1.45"><div class="%s">STATUS &amp; GRENZEN</div>%s</div>'
            '<div data-name="Quellen" style="display:flex;flex-direction:column;gap:12px"><div class="%s">QUELLANKER (%s)</div><ul style="margin:0;padding-left:20px;display:flex;flex-direction:column;gap:4px;font-family:var(--mono);font-size:14px;line-height:1.45;overflow-wrap:anywhere">%s</ul></div>'
            '<div data-name="Weiter" style="grid-column:1 / span 2;display:flex;flex-wrap:wrap;gap:12px">%s</div></footer>') % (b["id"], k, notes, k, b.get("source_label", "Kern " + SOURCE_BASELINE), src, links)


def check(board):
    """Node overlaps, edges through nodes, text that does not fit, label collisions."""
    problems = []
    _, rec = build(board)
    nodes = {n["id"]: n for n in rec["nodes"]}
    for n in board["nodes"]:
        if n.get("_fit"):
            problems.append("%s: %s" % (n["id"], n["_fit"]))
    ks = list(nodes)
    for i, a in enumerate(ks):
        ax, ay, aw, ah = nodes[a]["box"]
        if ax < 0 or ax + aw > CANVAS_W or ay < 0:
            problems.append("%s outside canvas" % a)
        for b in ks[i + 1:]:
            bx, by, bw, bh = nodes[b]["box"]
            if ax < bx + bw + 8 and bx < ax + aw + 8 and ay < by + bh + 8 and by < ay + ah + 8:
                problems.append("overlap/too close %s/%s" % (a, b))
    for e in rec["edges"]:
        pts = e["points"]
        for p, q in zip(pts, pts[1:]):
            for k, n in nodes.items():
                if k in (e["from"], e["to"]):
                    continue
                if seg_hits_rect(p, q, n["box"]):
                    problems.append("edge %s->%s crosses %s" % (e["from"], e["to"], k))
        # the last segment must be long enough to show an arrow head
        (x1, y1), (x2, y2) = pts[-2], pts[-1]
        if abs(x2 - x1) + abs(y2 - y1) < 16:
            problems.append("edge %s->%s: last segment too short for an arrow" % (e["from"], e["to"]))
    problems += board.get("_label_problems", [])
    return problems


if __name__ == "__main__":
    boards = load_boards()
    cmd = sys.argv[1]
    if cmd == "html":
        b = next(b for b in boards if b["id"] == sys.argv[2])
        part = sys.argv[3] if len(sys.argv) > 3 else "all"
        out = {"head": header_html(b), "canvas": build(b)[0], "foot": footer_html(b)}
        print(out[part] if part != "all" else out["head"] + out["canvas"] + out["foot"])
    elif cmd == "json":
        recs = [build(b)[1] for b in boards]
        (ROOT / "build").mkdir(exist_ok=True)
        (ROOT / "build" / "diagrams.json").write_text(json.dumps(recs, ensure_ascii=False, indent=1))
        print(len(recs), "boards")
    elif cmd == "check":
        bad = 0
        for b in boards:
            if len(sys.argv) > 2 and b["id"] not in sys.argv[2:]:
                continue
            p = check(b)
            bad += len(p)
            grown = ["%s+%d" % (n["id"], n["_grown"]) for n in b["nodes"] if n.get("_grown")]
            print(b["id"], "OK" if not p else p, ("grown: " + " ".join(grown)) if grown else "")
        sys.exit(1 if bad else 0)
