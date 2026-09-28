"""Primitives for the GIP architecture diagrams.

Hand-laid-out SVG, official AWS Architecture Icons (Q3 2026, 07.31.2026) inlined
unmodified, orthogonal connectors snapped onto the visible icon glyph, and
build-failing layout checks: no connector through text, icons or other connectors;
no overlapping text; text clear of borders; no floating arrow ends; text readable
at README width.
"""
import json
import math
import os
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ICONS = os.path.join(HERE, "icons")
FONT = "Arial, Helvetica, sans-serif"
MONO = "Menlo, Consolas, 'Courier New', monospace"
INK, GREY, LINE = "#232F3E", "#545B64", "#7D8998"
# Font sizes (px). Readability rule: min font x 980 / canvas width >= 9 (README display width 980 px).
F_LABEL, F_SUB, F_EDGE, F_TITLE = 17, 15, 15, 17
LH_LABEL, LH_SUB = 20, 18
README_W, MIN_DISPLAY_PX = 980, 9
# Opaque-pixel runs per icon row/column (icons/glyph-extents.json, from measure_glyphs.py).
with open(os.path.join(ICONS, "glyph-extents.json"), encoding="utf-8") as _f:
    GLYPHS = json.load(_f)
GN = GLYPHS["n"]
SNAP_SEARCH = round(GN * 0.06)   # empty row/column: use the nearest non-empty one within 6 %
END_TOL = 3.0                    # max distance (px) from an edge end to an opaque glyph pixel
GROUPS = {  # AWS group styles: stroke colour, dashed, group icon
    "cloud": ("#232F3E", False, "AWS-Cloud-logo_32"),
    "account": ("#E7157B", False, "AWS-Account_32"),
    "region": ("#00A4A6", True, "Region_32"),
    "plain": ("#7D8998", False, None),
    "generic": ("#7D8998", True, None),
    "bucket": ("#7AA116", False, "Res_Amazon-Simple-Storage-Service_Bucket_48"),
}
LW = 1.6

# Helvetica/Arial advance widths (1/1000 em), regular and bold, ASCII 32-126.
_R = ("278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 556 556 556 556 556 556 556 556 556 556 "
      "278 278 584 584 584 556 1015 667 667 722 722 667 611 778 722 278 500 667 556 833 722 778 667 778 722 667 "
      "611 722 667 944 667 667 611 278 278 278 469 556 333 556 556 500 556 556 278 556 556 222 222 500 222 833 "
      "556 556 556 556 333 500 278 556 500 722 500 500 500 334 260 334 584")
_B = ("278 333 474 556 556 889 722 238 333 333 389 584 278 333 278 278 556 556 556 556 556 556 556 556 556 556 "
      "333 333 584 584 584 611 975 722 722 722 722 667 611 778 722 278 556 722 611 833 722 778 667 778 722 667 "
      "611 722 667 944 667 667 611 333 278 333 584 556 333 556 611 556 611 556 333 611 611 278 278 556 278 889 "
      "611 611 611 611 389 556 333 611 556 778 556 556 500 389 280 389 584")
W_REG = dict(zip((chr(c) for c in range(32, 127)), map(int, _R.split())))
W_BOLD = dict(zip((chr(c) for c in range(32, 127)), map(int, _B.split())))


def tw(text, size, bold=False, mono=False):
    """Text advance width in px, with a 4% safety margin for font substitution."""
    if mono:
        return len(text) * size * 0.602 * 1.04
    table = W_BOLD if bold else W_REG
    return sum(table.get(ch, 600) for ch in text) * size / 1000 * 1.04


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def glyph_run(key, axis, idx):
    """[lo, hi] opaque run of row (axis 'rows') or column idx in GN-px raster units, or None."""
    runs = GLYPHS["icons"][key][axis]
    idx = min(max(idx, 0), GN - 1)
    for d in range(SNAP_SEARCH + 1):
        for j in (idx - d, idx + d):
            if 0 <= j < GN and runs[j]:
                return runs[j]
    return None


class Box:
    def __init__(self, x0, y0, x1, y1, kind, owner=None, key=None):
        self.x0, self.y0, self.x1, self.y1 = min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
        self.kind, self.owner, self.key = kind, owner, key

    def glyph_dist(self, p):
        """Distance (px) from p to the nearest run-end opaque pixel of this icon (conservative)."""
        s = (self.x1 - self.x0) / GN
        u, v = (p[0] - self.x0) / s, (p[1] - self.y0) / s
        best = math.inf
        for axis in ("rows", "cols"):
            for i, run in enumerate(GLYPHS["icons"][self.key][axis]):
                if not run:
                    continue
                for j in run:
                    px, py = (j, i) if axis == "rows" else (i, j)
                    dx, dy = max(px - u, 0, u - px - 1), max(py - v, 0, v - py - 1)
                    best = min(best, math.hypot(dx, dy) * s)
        return best

    def overlaps(self, o, pad=0):
        return not (self.x1 + pad <= o.x0 or o.x1 + pad <= self.x0 or self.y1 + pad <= o.y0 or o.y1 + pad <= self.y0)

    def hit_segment(self, a, b, shrink=0.5):
        """True when the axis-aligned segment a-b enters this box's interior."""
        x0, y0, x1, y1 = self.x0 + shrink, self.y0 + shrink, self.x1 - shrink, self.y1 - shrink
        (ax, ay), (bx, by) = a, b
        if ay == by:
            return y0 < ay < y1 and max(ax, bx) > x0 and min(ax, bx) < x1
        return x0 < ax < x1 and max(ay, by) > y0 and min(ay, by) < y1


class Diagram:
    def __init__(self, name, w, h):
        self.name, self.w, self.h = name, w, h
        self.bg, self.fg, self.defs = [], [], {}
        self.texts, self.solids, self.edges, self.nodes, self.crossing_ok = [], [], [], {}, set()
        self.borders, self.attach = [], []

    # ---------- text ----------
    def text(self, x, y, lines, size=15, color=INK, bold=False, anchor="middle", mono=False, lh=None, owner=None):
        """Draw lines with the first baseline at y; returns the bounding Box and registers it."""
        lines = [lines] if isinstance(lines, str) else lines
        lh = lh or round(size * 1.22)
        widths = [tw(t, size, bold, mono) for t in lines]
        w = max(widths) if widths else 0
        x0 = {"middle": x - w / 2, "start": x, "end": x - w}[anchor]
        for i, t in enumerate(lines):
            fam = MONO if mono else FONT
            self.fg.append(f'<text x="{x:.1f}" y="{y + i * lh:.1f}" font-family="{fam}" font-size="{size}" '
                           f'fill="{color}" text-anchor="{anchor}"{" font-weight=\"bold\"" if bold else ""}>{esc(t)}</text>')
        box = Box(x0 - 1, y - size * 0.80, x0 + w + 1, y + (len(lines) - 1) * lh + size * 0.24, "text", owner)
        self.texts.append(box)
        return box

    # ---------- icons ----------
    def _symbol(self, key):
        if key not in self.defs:
            src = open(os.path.join(ICONS, key + ".svg"), encoding="utf-8").read()
            m = re.search(r"<svg[^>]*viewBox=\"([^\"]+)\"[^>]*>(.*)</svg>", src, re.S)
            inner = re.sub(r"<title>.*?</title>", "", m.group(2), flags=re.S)
            inner = re.sub(r'id="([^"]+)"', lambda g: f'id="{key}-{g.group(1)}"', inner)
            inner = re.sub(r"url\(#([^)]+)\)", lambda g: f"url(#{key}-{g.group(1)})", inner)
            self.defs[key] = f'<symbol id="ic-{key}" viewBox="{m.group(1)}">{inner}</symbol>'
        return f"ic-{key}"

    def icon(self, key, cx, cy, size):
        sid = self._symbol(key)
        self.fg.append(f'<use href="#{sid}" x="{cx - size / 2:.1f}" y="{cy - size / 2:.1f}" width="{size}" height="{size}"/>')
        return Box(cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2, "icon", key=key)

    def node(self, nid, key, cx, cy, label, sub=(), size=64, pos="below"):
        """Icon plus label (dark) and optional sub-caption (grey). pos: below | above | right | left."""
        box = self.icon(key, cx, cy, size)
        box.owner = nid
        self.solids.append(box)
        label, sub = ([label] if isinstance(label, str) else list(label)), list(sub)
        if pos == "below":
            y = cy + size / 2 + F_LABEL + 4
            self.text(cx, y, label, F_LABEL, lh=LH_LABEL, owner=nid)
            if sub:
                self.text(cx, y + (len(label) - 1) * LH_LABEL + LH_SUB + 1, sub, F_SUB, GREY, lh=LH_SUB, owner=nid)
        elif pos == "above":
            tail = ((len(label) - 1) * LH_LABEL + (LH_SUB + 1 + (len(sub) - 1) * LH_SUB if sub else 0)
                    + 0.24 * (F_SUB if sub else F_LABEL))
            y = cy - size / 2 - 8 - tail
            self.text(cx, y, label, F_LABEL, lh=LH_LABEL, owner=nid)
            if sub:
                self.text(cx, y + (len(label) - 1) * LH_LABEL + LH_SUB + 1, sub, F_SUB, GREY, lh=LH_SUB, owner=nid)
        else:
            anchor, x = ("start", cx + size / 2 + 10) if pos == "right" else ("end", cx - size / 2 - 10)
            hgt = (len(label) - 1) * LH_LABEL + (len(sub) * LH_SUB if sub else 0) + F_LABEL
            y = cy - hgt / 2 + F_LABEL * 0.8
            self.text(x, y, label, F_LABEL, anchor=anchor, lh=LH_LABEL, owner=nid)
            if sub:
                self.text(x, y + (len(label) - 1) * LH_LABEL + LH_SUB + 1, sub, F_SUB, GREY, anchor=anchor, lh=LH_SUB,
                          owner=nid)
        self.nodes[nid] = box
        return box

    # ---------- groups and cards ----------
    def _rect(self, x, y, w, h, stroke, width, dashed, rx=0):
        dash = ' stroke-dasharray="6 4"' if dashed else ""
        r = f' rx="{rx}"' if rx else ""
        fill = "none" if not rx else "#FFFFFF"
        self.bg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}"{r} fill="{fill}" stroke="{stroke}" '
                       f'stroke-width="{width}"{dash}/>')
        self.borders += [((x, y), (x + w, y)), ((x, y + h), (x + w, y + h)), ((x, y), (x, y + h)), ((x + w, y), (x + w, y + h))]

    def group(self, x, y, w, h, kind, label, bold=True):
        color, dashed, gicon = GROUPS[kind]
        self._rect(x, y, w, h, color, 1.5, dashed)
        tx = x + 12
        if gicon:
            gb = self.icon(gicon, x + 16, y + 16, 32)
            gb.kind, gb.owner = "group-icon", label
            self.solids.append(gb)
            tx = x + 42
        return self.text(tx, y + 23, label, F_TITLE, INK, bold, anchor="start")

    def card(self, cid, x, y, w, h, title, cmds=(), icons=(), dashed=False, icon_x0=None, isize=48, notes=(), slots=None):
        """Module card: title, commands and notes top-left; icons in a row, each (key, label[, grey sub])."""
        self._rect(x, y, w, h, LINE, 1.4, dashed, rx=4)
        self.text(x + 14, y + 27, title, F_TITLE, INK, True, anchor="start", owner=cid)
        yy = y + 27 + 22
        for c in cmds:
            self.text(x + 14, yy, c, F_SUB, GREY, anchor="start", mono=True, owner=cid)
            yy += 20
        for n in notes:
            self.text(x + 14, yy, n, F_SUB, GREY, anchor="start", owner=cid)
            yy += LH_SUB
        if icons:
            x0 = icon_x0 if icon_x0 is not None else x + 14
            step = (x + w - 14 - x0) / (slots or len(icons))
            cy = y + 10 + isize / 2 if icon_x0 is not None else yy + 10 + isize / 2
            for i, item in enumerate(icons):
                key, lab, sub = (item + ((),))[:3]
                lab = [lab] if isinstance(lab, str) else list(lab)
                cx = x0 + step * (i + 0.5)
                b = self.icon(key, cx, cy, isize)
                b.owner = cid
                self.solids.append(b)
                ty = cy + isize / 2 + F_SUB + 3
                self.text(cx, ty, lab, F_SUB, INK, lh=LH_SUB, owner=cid)
                if sub:
                    self.text(cx, ty + len(lab) * LH_SUB, list(sub), F_SUB, GREY, lh=LH_SUB, owner=cid)
        box = Box(x, y, x + w, y + h, "card", cid)
        self.nodes[cid] = box
        return box

    def component(self, cid, x, y, w, h, key, label, sub=()):
        """Tall box for a hub process: icon and label at the top, ports along the edges."""
        self._rect(x, y, w, h, LINE, 1.4, False, rx=4)
        b = self.icon(key, x + w / 2, y + 16 + 32, 64)
        b.owner = cid
        self.solids.append(b)
        label = [label] if isinstance(label, str) else list(label)
        ly = y + 16 + 64 + F_LABEL + 4
        self.text(x + w / 2, ly, label, F_LABEL, INK, lh=LH_LABEL, owner=cid)
        if sub:
            self.text(x + w / 2, ly + (len(label) - 1) * LH_LABEL + LH_SUB + 1, list(sub), F_SUB, GREY, lh=LH_SUB, owner=cid)
        box = Box(x, y, x + w, y + h, "card", cid)
        self.nodes[cid] = box
        return box

    def port(self, nid, side, off=0, gap=0):
        b = self.nodes[nid]
        cx, cy = (b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2
        return {"left": (b.x0 - gap, cy + off), "right": (b.x1 + gap, cy + off),
                "top": (cx + off, b.y0 - gap), "bottom": (cx + off, b.y1 + gap)}[side]

    # ---------- edges ----------
    def _snap(self, p, q):
        """Move endpoint p (neighbour q) from a node icon's nominal box onto its opaque glyph."""
        (px, py), (qx, qy) = p, q
        for b in self.nodes.values():
            if b.kind != "icon" or b.key is None:
                continue
            s = (b.x1 - b.x0) / GN
            if py == qy and b.y0 < py < b.y1:
                run = glyph_run(b.key, "rows", int((py - b.y0) / s))
                if abs(px - b.x0) < 0.6 and qx < px and run:
                    return (b.x0 + run[0] * s, py), b
                if abs(px - b.x1) < 0.6 and qx > px and run:
                    return (b.x0 + (run[1] + 1) * s, py), b
            if px == qx and b.x0 < px < b.x1:
                run = glyph_run(b.key, "cols", int((px - b.x0) / s))
                if abs(py - b.y0) < 0.6 and qy < py and run:
                    return (px, b.y0 + run[0] * s), b
                if abs(py - b.y1) < 0.6 and qy > py and run:
                    return (px, b.y0 + (run[1] + 1) * s), b
        return p, None

    def edge(self, pts, label=None, dashed=False, both=False, badge=None, seg=None, side="above", t=0.5, sub=(),
             at=None, badge_at=None):
        """Orthogonal polyline. Label on segment `seg` (default: longest), centred at fraction t or at coordinate
        `at` along the segment. badge=(n, seg) centred at coordinate badge_at along that segment."""
        pts = [(float(a), float(b)) for a, b in pts]
        att = {}
        for end, nb in ((0, 1), (len(pts) - 1, len(pts) - 2)):
            pts[end], box = self._snap(pts[end], pts[nb])
            if box is not None:
                att[end] = box
        self.edges.append((pts, label))
        self.attach.append(att)
        if seg is None:
            seg = max(range(len(pts) - 1), key=lambda i: abs(pts[i][0] - pts[i + 1][0]) + abs(pts[i][1] - pts[i + 1][1]))
        draw = list(pts)
        draw[-1] = self._pull(pts[-1], pts[-2], 7)
        if both:
            draw[0] = self._pull(pts[0], pts[1], 7)
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in draw)
        dash = ' stroke-dasharray="6 4"' if dashed else ""
        self.fg.insert(0, f'<path d="{d}" fill="none" stroke="{INK}" stroke-width="{LW}"{dash}/>')
        self._head(pts[-1], pts[-2])
        if both:
            self._head(pts[0], pts[1])
        if label:
            self._label(pts[seg], pts[seg + 1], label, side, self._frac(pts[seg], pts[seg + 1], t, at), sub)
        if badge:
            n, bseg = badge
            (ax, ay), (bx, by) = pts[bseg], pts[bseg + 1]
            bt = self._frac(pts[bseg], pts[bseg + 1], 0.18, badge_at)
            x, y = ax + (bx - ax) * bt, ay + (by - ay) * bt
            self.fg.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="13" fill="{INK}"/>')
            self.fg.append(f'<text x="{x:.1f}" y="{y + 5.5:.1f}" font-family="{FONT}" font-size="{F_SUB}" '
                           f'font-weight="bold" fill="#FFFFFF" text-anchor="middle">{n}</text>')
            self.solids.append(Box(x - 13, y - 13, x + 13, y + 13, "badge", ("badge", len(self.edges) - 1)))

    @staticmethod
    def _frac(a, b, t, at):
        if at is None:
            return t
        i = 0 if a[1] == b[1] else 1
        return (at - a[i]) / (b[i] - a[i])

    @staticmethod
    def _pull(p, q, dist):
        (px, py), (qx, qy) = p, q
        if px == qx:
            return (px, py - dist if qy < py else py + dist)
        return (px - dist if qx < px else px + dist, py)

    def _head(self, tip, prev):
        (x, y), (px, py) = tip, prev
        dx, dy = (0 if x == px else (1 if x > px else -1)), (0 if y == py else (1 if y > py else -1))
        bx, by = x - dx * 11, y - dy * 11
        nx, ny = -dy * 5, dx * 5
        self.fg.append(f'<path d="M{x:.1f},{y:.1f} L{bx + nx:.1f},{by + ny:.1f} L{bx - nx:.1f},{by - ny:.1f} Z" fill="{INK}"/>')

    def _label(self, a, b, label, side, t, sub):
        (ax, ay), (bx, by) = a, b
        mx, my = ax + (bx - ax) * t, ay + (by - ay) * t
        lines, subs = ([label] if isinstance(label, str) else list(label)), list(sub)
        n = len(lines) + len(subs)
        if ay == by:
            y = my - 8 - (n - 1) * LH_SUB if side == "above" else my + 19
            anchor, x = "middle", mx
        else:
            anchor, x = ("start", mx + 9) if side == "right" else ("end", mx - 9)
            y = my - ((n - 1) * LH_SUB + F_EDGE) / 2 + F_EDGE * 0.8
        self.text(x, y, lines, F_EDGE, INK, anchor=anchor, lh=LH_SUB, owner="edge")
        if subs:
            self.text(x, y + len(lines) * LH_SUB, subs, F_SUB, GREY, anchor=anchor, lh=LH_SUB, owner="edge")

    # ---------- captions and legend ----------
    def caption(self, x, y, lines):
        """Caption block (grey, one sentence per line) under the drawing."""
        return self.text(x, y, lines, F_SUB, GREY, anchor="start", lh=21)

    def legend(self, x, y, optional_box=True):
        items = [("line", False, "request or data flow"), ("line", True, "auth, async, or optional flow")]
        if optional_box:
            items.append(("box", True, "optional module"))
        for kind, dashed, text in items:
            dash = ' stroke-dasharray="6 4"' if dashed else ""
            if kind == "line":
                self.fg.append(f'<path d="M{x},{y} L{x + 34},{y}" stroke="{INK}" stroke-width="{LW}"{dash}/>')
            else:
                self.bg.append(f'<rect x="{x}" y="{y - 9}" width="34" height="18" rx="3" fill="none" stroke="{LINE}" '
                               f'stroke-width="1.4"{dash}/>')
            self.text(x + 42, y + 5, text, F_SUB, GREY, anchor="start")
            x += 42 + tw(text, F_SUB) + 28

    # ---------- checks and output ----------
    def check(self):
        errs = []
        for i, (pts, _lab) in enumerate(self.edges):
            for a, b in zip(pts, pts[1:]):
                if a[0] != b[0] and a[1] != b[1]:
                    errs.append(f"edge {i}: diagonal segment {a}->{b}")
                for t in self.texts:
                    if t.hit_segment(a, b):
                        errs.append(f"edge {i} {a}->{b} crosses text {t.x0:.0f},{t.y0:.0f}")
                for s in self.solids:
                    if s.owner == ("badge", i):
                        continue
                    if (a == pts[0] and self.attach[i].get(0) is s) or \
                            (b == pts[-1] and self.attach[i].get(len(pts) - 1) is s):
                        continue  # the end segment reaches into its own icon's box to touch the glyph
                    if s.hit_segment(a, b, shrink=1.0):
                        errs.append(f"edge {i} {a}->{b} crosses {s.kind} {s.owner}")
        for i in range(len(self.edges)):
            for j in range(i + 1, len(self.edges)):
                if (i, j) in self.crossing_ok:
                    continue
                for a, b in zip(self.edges[i][0], self.edges[i][0][1:]):
                    for c, d in zip(self.edges[j][0], self.edges[j][0][1:]):
                        if _cross(a, b, c, d):
                            errs.append(f"edges {i} and {j} cross near {a}->{b} / {c}->{d}")
        for t in self.texts:
            for a, b in self.borders:
                if t.hit_segment(a, b, shrink=-3):
                    errs.append(f"text @{t.x0:.0f},{t.y0:.0f} crosses a group border {a}->{b}")
        items = self.texts + self.solids
        for i, p in enumerate(items):
            for q in items[i + 1:]:
                if p.overlaps(q, pad=1) and not (p.kind == "icon" and q.kind == "icon"):
                    if {p.kind, q.kind} == {"badge", "text"} or p.kind == "text" or q.kind == "text":
                        errs.append(f"overlap {p.kind}@{p.x0:.0f},{p.y0:.0f} / {q.kind}@{q.x0:.0f},{q.y0:.0f}")
            if p.x0 < 0 or p.y0 < 0 or p.x1 > self.w or p.y1 > self.h:
                errs.append(f"out of canvas: {p.kind}@{p.x0:.0f},{p.y0:.0f}")
        errs += self._check_ends()
        return errs

    def _check_ends(self):
        """Every edge end touches an opaque glyph pixel (<= END_TOL px) or lies on a card/component/group border."""
        errs = []
        icons = [b for b in self.nodes.values() if b.kind == "icon" and b.key]
        for i, (pts, _lab) in enumerate(self.edges):
            for p in (pts[0], pts[-1]):
                if any(_on_segment(p, a, b) for a, b in self.borders):
                    continue
                near = [b.glyph_dist(p) for b in icons
                        if b.x0 - END_TOL <= p[0] <= b.x1 + END_TOL and b.y0 - END_TOL <= p[1] <= b.y1 + END_TOL]
                d = min(near) if near else math.inf
                if d > END_TOL:
                    why = f"nearest glyph pixel {d:.1f} px away" if near else "no icon or border there"
                    errs.append(f"edge {i} end {p} floats: {why} (limit {END_TOL:g} px)")
        return errs

    def render(self, svg_dir, png_dir=None, zoom=2):
        """Write <name>.svg to svg_dir and a zoom-x PNG to png_dir; returns self with .errors set."""
        errs = self.check()
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" viewBox="0 0 {self.w} {self.h}">'
               f'<defs>{"".join(self.defs[k] for k in sorted(self.defs))}</defs>'
               f'<rect width="{self.w}" height="{self.h}" fill="#FFFFFF"/>' + "".join(self.bg) + "".join(self.fg) + "</svg>\n")
        self.min_font = min(float(m) for m in re.findall(r'font-size="([0-9.]+)"', svg))
        self.display_px = self.min_font * README_W / self.w
        if self.display_px < MIN_DISPLAY_PX:
            errs.append(f"readability: min font {self.min_font} px x {README_W} / {self.w} = {self.display_px:.2f} px "
                        f"< {MIN_DISPLAY_PX}")
        png_dir = png_dir or svg_dir
        for dr in (svg_dir, png_dir):
            os.makedirs(dr, exist_ok=True)
        sp, pp = os.path.join(svg_dir, self.name + ".svg"), os.path.join(png_dir, self.name + ".png")
        with open(sp, "w", encoding="utf-8") as f:
            f.write(svg)
        subprocess.run(["rsvg-convert", "-z", str(zoom), "-b", "#FFFFFF", "-o", pp, sp], check=True)  # nosec B603 - fixed argv
        self.errors = errs
        return self


def _on_segment(p, a, b, tol=0.5):
    (px, py), (ax, ay), (bx, by) = p, a, b
    if ay == by:
        return abs(py - ay) <= tol and min(ax, bx) - tol <= px <= max(ax, bx) + tol
    return abs(px - ax) <= tol and min(ay, by) - tol <= py <= max(ay, by) + tol


def _cross(a, b, c, d):
    """Strict interior crossing of two axis-aligned segments (perpendicular only)."""
    h1, h2 = a[1] == b[1], c[1] == d[1]
    if h1 == h2:
        return False
    (hx0, hx1, hy), (vx, vy0, vy1) = ((min(a[0], b[0]), max(a[0], b[0]), a[1]), (c[0], min(c[1], d[1]), max(c[1], d[1]))) if h1 \
        else ((min(c[0], d[0]), max(c[0], d[0]), c[1]), (a[0], min(a[1], b[1]), max(a[1], b[1])))
    return hx0 < vx < hx1 and vy0 < hy < vy1
