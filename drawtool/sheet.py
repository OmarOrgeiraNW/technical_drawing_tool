"""Drawing sheet: ISO 5457 frame, first-angle views, dimensions, ISO 7200 title block.

Everything is drawn in modelspace in paper millimetres (views are scaled);
dimensions use DIMLFAC so they show true part size. The PDF and PNG are
rendered from the same DXF, so all three outputs always agree.
"""

import itertools
from collections import defaultdict
from pathlib import Path

import ezdxf
import ezdxf.bbox
import numpy as np
from ezdxf.addons.drawing import Frontend, RenderContext, config, layout
from ezdxf.addons.drawing import pymupdf as pdf_backend
from ezdxf.enums import TextEntityAlignment
from ezdxf.fonts import fonts
from ezdxf.math import BoundingBox2d

from . import geometry, holes, partfile

fonts.font_manager.scan_folder(Path(__file__).parent / "fonts")  # same font on every OS

SHEETS = {"A4": (297, 210), "A3": (420, 297)}  # landscape
SCALES = [10, 5, 2, 1, 1 / 2, 1 / 5, 1 / 10]  # ISO 5455
LEFT, BOTTOM, RIGHT, TOP = 20, 10, 10, 10  # ISO 5457 frame margins
FONT = "DejaVuSans.ttf"
H = 3.5  # ISO 3098 lettering height for dimensions and notes
TB_W = 180  # ISO 7200 maximum title block width
TB_ROWS = [0, 10, 19, 28, 37, 46]  # row boundaries from the bottom; row 0 holds the 5 mm ID number
TB_H = TB_ROWS[-1]
DIM_OFFSET = 10  # outline to dimension line
DIM_SPACE = DIM_OFFSET + 1 + H + 4  # room kept free beside a view for one dimension
GAP = 12  # clear space between views
PAD = 4  # clear space around blocks
LAYERS = {  # name: (linetype, lineweight in 1/100 mm)
    "VISIBLE": ("Continuous", 50),
    "HIDDEN": ("ISO_DASHED", 25),
    "CENTER": ("ISO_CENTER", 25),
    "DIMS": ("Continuous", 25),
    "TEXT": ("Continuous", 25),
    "FRAME": ("Continuous", 35),
    "HOLES": ("Continuous", 25),  # hole tags, leaders, table origin, detail letters
}
TAG_H = 2.5  # lettering height for hole tags and the hole table
ROW = 5.0  # hole table row height
CROWDED = 5.0  # hole centres closer than this on paper get a detail view
DETAIL_LETTERS = "ZWVUTSRQPNMLKJHGFE"  # from the end of the alphabet, apart from hole types


# ---------------------------------------------------------------- layout

def fmt_scale(s):
    return f"{s:g}:1" if s >= 1 else f"1:{1 / s:g}"


def parse_scale(text):
    a, b = str(text).split(":")
    return float(a) / float(b)


def _size(ext):
    return ext[2] - ext[0], ext[3] - ext[1]


def _overlap(a, b, pad=0.0):
    return a[0] < b[2] + pad and b[0] < a[2] + pad and a[1] < b[3] + pad and b[1] < a[3] + pad


def _inside(r, box):
    return r[0] >= box[0] and r[1] >= box[1] and r[2] <= box[2] and r[3] <= box[3]


def place(ext, sheet, s, notes_h):
    """Place front/top/left (first angle) at scale s, or None if they do not fit."""
    w, h = SHEETS[sheet]
    area = (LEFT, BOTTOM, w - RIGHT, h - TOP)
    inner = (area[0] + PAD, area[1] + PAD, area[2] - PAD, area[3] - PAD)
    tb = (area[2] - TB_W, area[1], area[2], area[1] + TB_H + notes_h)
    fw, fh = (v * s for v in _size(ext["front"]))
    lw = _size(ext["left"])[0] * s if "left" in ext else -GAP
    th = _size(ext["top"])[1] * s
    group_w = DIM_SPACE + fw + GAP + lw
    free_w = inner[2] - inner[0]
    for gx, gtop in ((inner[0] + (free_w - group_w) / 2, inner[3]), (inner[0], inner[3])):
        fx, ftop = gx + DIM_SPACE, gtop - DIM_SPACE
        rects = {
            "front": (fx, ftop - fh, fx + fw, ftop),
            "top": (fx, ftop - fh - GAP - th, fx + fw, ftop - fh - GAP),
        }
        # keep-out zones = views plus the dimension space above/left of them
        zones = [(rects["front"][0] - DIM_SPACE, *rects["front"][1:3], rects["front"][3] + DIM_SPACE),
                 (rects["top"][0] - DIM_SPACE, *rects["top"][1:])]
        if "left" in ext:
            rects["left"] = (fx + fw + GAP, ftop - fh, fx + fw + GAP + lw, ftop)
            zones.append((*rects["left"][:3], rects["left"][3] + DIM_SPACE))
        if all(_inside(z, inner) and not _overlap(z, tb, PAD) for z in zones):
            origins = {}
            for name, r in rects.items():
                e = ext[name]
                origins[name] = (r[0] - e[0] * s, r[1] - e[1] * s)
            return {"sheet": sheet, "size": (w, h), "scale": s, "area": area,
                    "title_block": tb, "rects": rects, "zones": zones, "origins": origins}
    return None


def choose_layout(ext, notes_h, sheet="auto", scale="auto", block=None):
    """A4 if the part fits at 1:1 or larger, otherwise the best scale on A3.

    `block` is the size of the hole tables, which must fit next to the views.
    """
    if sheet != "auto" and sheet not in SHEETS:
        raise ValueError(f"unknown sheet size '{sheet}': use auto, A4 or A3")
    sheets = ["A4", "A3"] if sheet == "auto" else [sheet]
    scales = SCALES if scale == "auto" else [parse_scale(scale)]
    for i, name in enumerate(sheets):
        for s in scales:
            lay = place(ext, name, s, notes_h)
            if lay is None or (block and not _reserve(lay, block)):
                continue
            if s >= 1 or i == len(sheets) - 1 or scale != "auto":
                return lay
            break  # only fits reduced on this sheet: try the next size first
    raise ValueError("views do not fit; set 'sheet' and/or 'scale' in the part YAML")


def _reserve(lay, block):
    """Put the hole tables in the top-right corner of the largest free area."""
    r, fit = free_rect(lay, block, label=0)
    if not r or fit < 1:
        return False
    x1, y1 = r[2] - PAD, r[3] - PAD
    lay["table"] = (x1 - block[0], y1 - block[1], x1, y1)
    lay["zones"].append(lay["table"])
    return True


def free_rect(lay, size, label=H + 4):
    """Largest free rectangle (scored by how well `size` fits) for the isometric view.

    `label` mm stay free under the view for its scale caption.
    """
    x0, y0, x1, y1 = lay["area"]
    blocks = lay["zones"] + [lay["title_block"]]
    xs = sorted({x0, x1, *(v for b in blocks for v in (b[0] - PAD, b[2] + PAD) if x0 <= v <= x1)})
    ys = sorted({y0, y1, *(v for b in blocks for v in (b[1] - PAD, b[3] + PAD) if y0 <= v <= y1)})
    best, best_fit = None, 0
    for (ax, bx), (ay, by) in itertools.product(itertools.combinations(xs, 2), itertools.combinations(ys, 2)):
        r = (ax, ay, bx, by)
        if any(_overlap(r, b) for b in blocks):
            continue
        fit = min((bx - ax - 2 * PAD) / size[0], (by - ay - 2 * PAD - label) / size[1])
        if fit > best_fit:
            best, best_fit = r, fit
    return best, best_fit


# ---------------------------------------------------------------- DXF helpers

def new_doc():
    doc = ezdxf.new("R2013", setup=True)
    doc.header["$INSUNITS"] = 4  # mm
    doc.header["$MEASUREMENT"] = 1
    doc.header["$LTSCALE"] = 1
    doc.linetypes.add("ISO_CENTER", pattern=[18, 12, -3, 0, -3], description="ISO 128 centre line")
    doc.linetypes.add("ISO_DASHED", pattern=[4, 3, -1], description="ISO 128 hidden line")
    for name, (lt, lw) in LAYERS.items():
        doc.layers.add(name, linetype=lt, lineweight=lw)
    doc.styles.add("ISO", font=FONT)
    ds = doc.dimstyles.new("ISO")
    ds.dxf.dimtxsty = "ISO"
    for k, v in dict(dimtxt=H, dimasz=3, dimexe=2, dimexo=1, dimgap=1, dimtad=1, dimtih=0,
                     dimtoh=0, dimdec=2, dimzin=8, dimdsep=ord("."), dimclrd=256, dimclre=256,
                     dimclrt=256, dimlwd=25, dimlwe=25).items():
        ds.set_dxf_attrib(k, v)
    return doc


def text_width(s, h):
    return fonts.make_font(FONT, h).text_width(s)


def text(msp, s, x, y, h=H, align="BOTTOM_LEFT", width=None, layer="TEXT"):
    """Single-line text; shrinks to `width` if given."""
    if width and s and text_width(s, h) > width:
        h *= width / text_width(s, h)
    msp.add_text(s, height=h, dxfattribs={"layer": layer, "style": "ISO"}).set_placement(
        (x, y), align=TextEntityAlignment[align])


def rect(msp, r, layer="FRAME", lw=None):
    attribs = {"layer": layer, **({"lineweight": lw} if lw else {})}
    msp.add_lwpolyline([(r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])], close=True,
                       dxfattribs=attribs)


# ---------------------------------------------------------------- drawing content

def draw_frame(msp, lay):
    w, h = lay["size"]
    x0, y0, x1, y1 = lay["area"]
    rect(msp, lay["area"], lw=70)
    for a, b in (((0, h / 2), (x0, h / 2)), ((x1, h / 2), (w, h / 2)),  # ISO 5457 centring marks
                 ((w / 2, 0), (w / 2, y0)), ((w / 2, y1), (w / 2, h))):
        msp.add_line(a, b, dxfattribs={"layer": "FRAME", "lineweight": 70})
    # ISO 5457 grid reference: 50 mm fields counted from the centring marks,
    # numbers left to right along the top and bottom, letters top to bottom at the sides
    xs = _fields(w / 2, x0, x1)
    ys = _fields(h / 2, y0, y1)[::-1]
    for x in xs[1:-1]:
        for ya, yb in ((y1, y1 + 5), (y0, y0 - 5)):
            msp.add_line((x, ya), (x, yb), dxfattribs={"layer": "FRAME"})
    for y in ys[1:-1]:
        for xa, xb in ((x0, x0 - 5), (x1, x1 + 5)):
            msp.add_line((xa, y), (xb, y), dxfattribs={"layer": "FRAME"})
    for i, (a, b) in enumerate(zip(xs[:-1], xs[1:]), 1):
        for y in (y1 + 5, y0 - 5):
            text(msp, str(i), (a + b) / 2, y, align="MIDDLE_CENTER", layer="FRAME")
    for letter, (a, b) in zip("ABCDEFGH", zip(ys[:-1], ys[1:])):
        for x in (x0 - 5, x1 + 5):
            text(msp, letter, x, (a + b) / 2, align="MIDDLE_CENTER", layer="FRAME")


def _fields(centre, lo, hi, size=50):
    """Field boundaries every `size` mm from the centring mark, clipped to the frame."""
    inner = [centre + k * size for k in range(-10, 11) if lo < centre + k * size < hi]
    return [lo, *inner, hi]


def to_paper(pts, origin, s):
    return np.asarray(pts) * s + origin


def draw_view(msp, polylines, origin, s):
    for pl in polylines:
        msp.add_lwpolyline(to_paper(pl, origin, s).tolist(), dxfattribs={"layer": "VISIBLE"})


def draw_centre_lines(msp, ext, origin, s, sym, overhang=3):
    x0, y0 = to_paper(ext[:2], origin, s)
    x1, y1 = to_paper(ext[2:], origin, s)
    attribs = {"layer": "CENTER", "ltscale": 0.5}
    if sym[0]:
        cx = (x0 + x1) / 2
        msp.add_line((cx, y0 - overhang), (cx, y1 + overhang), dxfattribs=attribs)
    if sym[1]:
        cy = (y0 + y1) / 2
        msp.add_line((x0 - overhang, cy), (x1 + overhang, cy), dxfattribs=attribs)


def centre_marks(msp, circles, frame, lines, ext, origin, s, sym, overhang=2, pixels=600):
    """Centre crosses on visible full circles and axis lines on visible cylinders.

    Hidden features get none (hidden lines are not drawn). Arms that would lie
    on the view's symmetry lines are left out; tiny circles get continuous lines.
    """
    d, y = frame
    x = np.cross(y, d)
    raster = geometry.Raster(lines, pixels=pixels)
    tol = 1e-3 * max(_size(ext))
    sym_x = (ext[0] + ext[2]) / 2 if sym[0] else None
    sym_y = (ext[1] + ext[3]) / 2 if sym[1] else None
    marks, axes = {}, {}
    ring = np.linspace(0, 2 * np.pi, 48, endpoint=False)
    for c, a, r in circles:
        p = np.array([c @ x, c @ y])
        if abs(a @ d) > 0.999:  # seen as a circle
            if raster.coverage(p + r * np.c_[np.cos(ring), np.sin(ring)]) >= 0.7:
                key = tuple(np.round(p, 2))
                marks[key] = max(marks.get(key, 0), r)
        elif abs(a @ d) < 1e-3:  # seen from the side: collect positions along the axis
            foot = c - (c @ a) * a
            key = (tuple(np.round(a, 4)), tuple(np.round(foot, 2)))
            axes.setdefault(key, []).append((c @ a, r, c))
    for (px, py), r in marks.items():
        small = 2 * r * s < 3  # under 3 mm on paper: short continuous lines
        arm = r * s + (overhang / 2 if small else overhang)
        cx, cy = to_paper((px, py), origin, s)
        attribs = {"layer": "CENTER", "ltscale": 0.25}
        if small:
            attribs["linetype"] = "Continuous"
        if sym_x is None or abs(px - sym_x) > tol:
            msp.add_line((cx, cy - arm), (cx, cy + arm), dxfattribs=attribs)
        if sym_y is None or abs(py - sym_y) > tol:
            msp.add_line((cx - arm, cy), (cx + arm, cy), dxfattribs=attribs)
    for (a, _), stations in axes.items():
        ts = [t for t, _, _ in stations]
        if max(ts) - min(ts) < tol:
            continue
        r = max(r for _, r, _ in stations)
        c0 = stations[0][2] - stations[0][0] * np.asarray(a)
        a2 = np.array([np.dot(a, x), np.dot(a, y)])
        n2 = np.array([-a2[1], a2[0]])
        base = np.array([c0 @ x, c0 @ y])
        t = np.linspace(min(ts), max(ts), 24)[:, None]
        sides = np.vstack([base + t * a2 + r * n2, base + t * a2 - r * n2])
        if raster.coverage(sides) < 0.6:
            continue  # hidden cylinder
        on_sym = ((sym_x is not None and abs(a2[0]) < 1e-6 and abs(base[0] - sym_x) < tol) or
                  (sym_y is not None and abs(a2[1]) < 1e-6 and abs(base[1] - sym_y) < tol))
        if on_sym:
            continue
        p1 = to_paper(base + min(ts) * a2, origin, s) - a2 * overhang
        p2 = to_paper(base + max(ts) * a2, origin, s) + a2 * overhang
        msp.add_line(tuple(p1), tuple(p2), dxfattribs={"layer": "CENTER", "ltscale": 0.25})


def plan_holes(found, frames, ortho, ext, sym, cfg):
    """Give every hole a view (where it is seen as a circle), a type letter and a tag.

    Returns rows (one per hole), types (one per hole type, with the spec in
    use) and warnings (unconfirmed guesses, holes that fit no view).
    """
    rows, warnings = [], []
    for h in found:
        best = None
        for rank, view in enumerate(ortho):
            d, y = frames[view]
            if abs(np.dot(h["axis"], d)) < 0.999:
                continue
            facing = [e for e in h["entries"] if np.dot(e["out"], d) > 0.999]
            shown = next((e for e in facing if e["exposed"]), None)
            option = (shown is None, rank, view, shown or (facing or h["entries"])[0])
            if best is None or option[:2] < best[:2]:
                best = option
        if best is None:
            warnings.append(f"Ø{holes.fmt(h['diameter'])} hole at {np.round(h['entries'][0]['point'], 2).tolist()}"
                            " is not square to any view and is left out of the hole table")
            continue
        hidden, _, view, entry = best
        d, y = frames[view]
        p = entry["point"]
        rows.append({"hole": h, "entry": entry, "view": view, "hidden": hidden,
                     "xy": np.array([p @ np.cross(y, d), p @ y]), "key": holes.type_key(h, entry)})
    first = {}
    for r in rows:
        first.setdefault(r["key"], r)
    keys = sorted(first, key=lambda k: (first[k]["hole"]["diameter"], k))
    user = cfg.get("holes") or {}
    types = []
    for letter, key in zip(_letters(), keys):
        spec = holes.guess(first[key]["hole"], first[key]["entry"])
        if key in user:
            spec = {"thread": "", "thread_depth": None, "tolerance": "", "finish": "", "confirm": False,
                    "note": spec["note"], **(user[key] or {})}
        mine = sorted((r for r in rows if r["key"] == key),
                      key=lambda r: (ortho.index(r["view"]), -round(r["xy"][1], 2), round(r["xy"][0], 2)))
        for i, r in enumerate(mine, 1):
            r["tag"] = f"{letter}{i}"
        h = first[key]["hole"]
        types.append({"letter": letter, "key": key, "count": len(mine), "spec": spec,
                      "candidates": holes.thread_candidates(h["diameter"], h["pitch"])})
        if holes.pending(spec):
            warnings.append(f"hole type {letter} ({key}): '{holes.pending(spec)}' not confirmed")
    return rows, types, warnings


def _letters():
    for n in itertools.count(1):
        for combo in itertools.product("ABCDEFGHJKLMNPQRSTUVWXYZ", repeat=n):
            yield "".join(combo)


def hole_origin(view, frame, ext, sym, datum):
    """Hole table origin in view coordinates.

    auto: the symmetry centre in a symmetric direction, else the lower/left edge.
    """
    origin = (datum or {}).get("origin", "auto")
    x0, y0, x1, y1 = ext
    if isinstance(origin, (list, tuple)):
        d, y = frame
        p = np.array(origin, float)
        return np.array([p @ np.cross(y, d), p @ y])
    if origin == "centre":
        return np.array([(x0 + x1) / 2, (y0 + y1) / 2])
    if origin == "corner":
        return np.array([x0, y0])
    return np.array([(x0 + x1) / 2 if sym[0] else x0, (y0 + y1) / 2 if sym[1] else y0])


def hole_tables(rows, types, ortho, origins, at_centre):
    """[(title, note, [(tag, X, Y, SIZE, REMARK)])] per view; SIZE on the first row of each type."""
    specs = {t["key"]: t["spec"] for t in types}
    tables = []
    for view in ortho:
        mine = sorted((r for r in rows if r["view"] == view), key=lambda r: (r["tag"][0], int(r["tag"][1:])))
        if not mine:
            continue
        lines = []
        for key in dict.fromkeys(r["key"] for r in mine):  # types in tag order
            group = [r for r in mine if r["key"] == key]
            first = group[0]
            size = _wrap(holes.size_text(first["hole"], first["entry"], specs[key]), SIZE_W)
            for i, r in enumerate(group):
                x, y = (v if abs(v) >= 0.005 else 0.0 for v in r["xy"] - origins[view])
                lines.append((r["tag"], f"{x:.2f}", f"{y:.2f}", size[i] if i < len(size) else "",
                              "hidden" if r["hidden"] else ""))
            lines += [("", "", "", more, "") for more in size[len(group):]]
        note = (f"X/Y from the centre lines of the {view} view" if at_centre[view]
                else f"X/Y from the origin marked in the {view} view")
        tables.append((f"HOLE TABLE - {view.upper()} VIEW", note, lines))
    return tables


HEADER = ("TAG", "X", "Y", "SIZE", "REMARK")
SIZE_W = 62  # mm; longer hole descriptions continue on the type's next rows


def _wrap(description, width):
    """Split a hole description at its commas into lines no wider than `width`."""
    lines = []
    for part in description.split(", "):
        if lines and text_width(f"{lines[-1]}, {part}", TAG_H) <= width:
            lines[-1] = f"{lines[-1]}, {part}"
        else:
            if lines:
                lines[-1] += ","
            lines.append(part)
    return lines


def table_size(tables):
    cells = [c for _, _, lines in tables for c in lines] + [HEADER]
    widths = [max(text_width(c[k], TAG_H) for c in cells) + 3 for k in range(5)]
    widths[1] = widths[2] = max(widths[1], widths[2], text_width("-00.00", TAG_H) + 3)
    title_w = max(max(text_width(t, TAG_H + 0.5), text_width(n, TAG_H)) for t, n, _ in tables) + 3
    widths[3] += max(0, title_w - sum(widths))
    height = sum(ROW * (len(lines) + 3) for _, _, lines in tables) + PAD * (len(tables) - 1)
    return widths, (sum(widths), height)


def draw_tables(msp, box, tables, widths):
    x0, top = box[0], box[3]
    xs = np.cumsum([x0] + widths)
    for title, note, lines in tables:
        bottom = top - ROW * (len(lines) + 2)
        rect(msp, (x0, bottom, xs[-1], top), lw=50)
        text(msp, title, x0 + 1.5, top - ROW + 1.3, h=TAG_H + 0.5, align="LEFT")
        for i, row in enumerate([HEADER] + lines):
            y = top - ROW * (i + 2)
            msp.add_line((x0, y + ROW), (xs[-1], y + ROW), dxfattribs={"layer": "FRAME"})
            for k, value in enumerate(row):
                right = k in (1, 2) and i > 0  # numbers right-aligned
                text(msp, value, xs[k + 1] - 1.5 if right else xs[k] + 1.5, y + 1.5, h=TAG_H,
                     align="RIGHT" if right else "LEFT")
        for xv in xs[1:-1]:
            msp.add_line((xv, bottom), (xv, top - ROW), dxfattribs={"layer": "FRAME"})
        text(msp, note, x0, bottom - ROW + 1.5, h=TAG_H, align="LEFT")
        top = bottom - ROW - PAD


def plan_details(rows, lay, ortho):
    """Enlarged detail views (ISO 128-3) where hole centres crowd on paper.

    Scale: the smallest ISO 5455 scale that puts the closest holes 10 mm apart,
    stepping down until the detail fits the free space.
    """
    s, details, letters = lay["scale"], [], iter(DETAIL_LETTERS)
    for view in ortho:
        spots = defaultdict(list)
        for r in rows:
            if r["view"] == view:
                spots[tuple(np.round(r["xy"], 3))].append(r)
        pts = np.array(list(spots))
        parent = list(range(len(pts)))

        def root(i):
            while parent[i] != i:
                i = parent[i]
            return i

        for i, j in itertools.combinations(range(len(pts)), 2):
            if np.hypot(*(pts[i] - pts[j])) * s < CROWDED:
                parent[root(i)] = root(j)
        clusters = defaultdict(list)
        for i in range(len(pts)):
            clusters[root(i)].append(i)
        for members in (m for m in clusters.values() if len(m) > 1):
            p = pts[members]
            radii = [max(r["hole"]["diameter"] / 2 for r in spots[tuple(q)]) for q in p]
            centre = (p.min(axis=0) + p.max(axis=0)) / 2
            R = max(np.hypot(*(q - centre)) + rh for q, rh in zip(p, radii)) * 1.3 + 1.0
            gap = min(np.hypot(*(a - b)) for a, b in itertools.combinations(p, 2))
            options = sorted(c for c in SCALES if c >= 2 * s)
            wanted = next((c for c in options if gap * c >= 10), options[-1] if options else None)
            for sc in [c for c in options if wanted and c <= wanted][::-1]:
                size = (2 * R * sc + 2, 2 * R * sc + 10)  # circle plus its label
                if size[0] > 110:
                    continue
                free, fit = free_rect(lay, size, label=0)
                if not free or fit < 1:
                    continue
                cx, cy = (free[0] + free[2]) / 2, (free[1] + free[3]) / 2 - 4
                box = (cx - size[0] / 2, cy - R * sc - 1, cx + size[0] / 2, cy + R * sc + 9)
                lay["zones"].append(box)
                detail = {"view": view, "letter": next(letters), "centre": centre, "R": R,
                          "scale": sc, "paper": np.array([cx, cy]), "box": box,
                          "origin": np.array([cx, cy]) - centre * sc}
                details.append(detail)
                for q in p:
                    for r in spots[tuple(q)]:
                        r["detail"] = detail
                break
    return details


def draw_details(msp, details, lines, frames, ext, sym, circles, lay):
    """Circle and letter on the main view; the enlarged, clipped view with its label."""
    s = lay["scale"]
    for d in details:
        view, c, R, sc, o = d["view"], d["centre"], d["R"], d["scale"], d["origin"]
        mx, my = to_paper(c, np.array(lay["origins"][view]), s)
        msp.add_circle((mx, my), R * s, dxfattribs={"layer": "DIMS"})
        text(msp, d["letter"], mx + 0.71 * R * s + 1, my + 0.71 * R * s + 1, h=5, layer="HOLES")
        clipped = geometry.clip_to_circle(lines[view], c, R)
        draw_view(msp, clipped, o, sc)
        msp.add_circle(tuple(d["paper"]), R * sc, dxfattribs={"layer": "DIMS"})
        text(msp, f"{d['letter']} ({fmt_scale(sc)})", d["paper"][0], d["paper"][1] + R * sc + 3, h=5,
             align="BOTTOM_CENTER")
        e = ext[view]
        for k, on in enumerate(sym[view]):  # symmetry lines crossing the detail
            at = (e[k] + e[k + 2]) / 2
            if on and abs(at - c[k]) < R:
                half = np.sqrt(R * R - (at - c[k]) ** 2)
                a, b = np.array(c, float), np.array(c, float)
                a[k] = b[k] = at
                a[1 - k] -= half
                b[1 - k] += half
                msp.add_line(tuple(to_paper(a, o, sc)), tuple(to_paper(b, o, sc)),
                             dxfattribs={"layer": "CENTER", "ltscale": 0.5})
        dv, y = frames[view]
        x = np.cross(y, dv)
        inside = [(cc, a, r) for cc, a, r in circles if np.hypot(cc @ x - c[0], cc @ y - c[1]) + r < R]
        if clipped:
            centre_marks(msp, inside, frames[view], clipped, e, o, sc, sym[view], pixels=150)


def origin_symbol(msp, x, y, size=7):
    """Hole table origin: arrows along the view's +X and +Y."""
    msp.add_circle((x, y), 0.6, dxfattribs={"layer": "HOLES"})
    for u, label, align in (((1, 0), "X", "MIDDLE_LEFT"), ((0, 1), "Y", "BOTTOM_CENTER")):
        u = np.array(u, float)
        n = np.array([-u[1], u[0]])
        tip = np.array([x, y]) + size * u
        msp.add_line((x, y), tuple(tip), dxfattribs={"layer": "HOLES"})
        msp.add_solid([tuple(tip), tuple(tip - 1.8 * u + 0.5 * n), tuple(tip - 1.8 * u - 0.5 * n)],
                      dxfattribs={"layer": "HOLES"})
        text(msp, label, *(tip + 0.8 * u), h=TAG_H, align=align, layer="HOLES")


class Occupancy:
    """What is already drawn, on a 0.25 mm grid, so hole tags go in free space."""

    RES = 0.25

    def __init__(self, lay):
        w, h = lay["size"]
        self.grid = np.ones((int(h / self.RES) + 2, int(w / self.RES) + 2), bool)
        x0, y0, x1, y1 = lay["area"]
        self.grid[self._i(y0 + 1):self._i(y1 - 1), self._i(x0 + 1):self._i(x1 - 1)] = False

    def _i(self, v):
        return int(np.clip(v / self.RES, 0, max(self.grid.shape) - 1))

    def points(self, pts):
        pts = np.asarray(pts, float)
        cols = np.clip((pts[:, 0] / self.RES).astype(int), 0, self.grid.shape[1] - 1)
        rows = np.clip((pts[:, 1] / self.RES).astype(int), 0, self.grid.shape[0] - 1)
        self.grid[rows, cols] = True

    def polyline(self, pts):
        pts = np.asarray(pts, float)[:, :2]
        for a, b in zip(pts[:-1], pts[1:]):
            n = int(np.hypot(*(b - a)) / self.RES) + 2
            self.points(a + np.linspace(0, 1, n)[:, None] * (b - a))

    def box(self, r):
        self.grid[self._i(r[1]):self._i(r[3]) + 1, self._i(r[0]):self._i(r[2]) + 1] = True

    def cost(self, r):
        return int(self.grid[self._i(r[1]):self._i(r[3]) + 1, self._i(r[0]):self._i(r[2]) + 1].sum())

    def entity(self, e):
        kind = e.dxftype()
        if kind == "LWPOLYLINE":
            self.polyline(list(e.get_points("xy")) + ([e[0][:2]] if e.closed else []))
        elif kind == "LINE":
            self.polyline([(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)])
        elif kind in ("CIRCLE", "ARC"):
            c, r = e.dxf.center, e.dxf.radius
            a = np.linspace(0, 2 * np.pi, max(12, int(2 * np.pi * r / self.RES)))
            self.points(np.c_[c[0] + r * np.cos(a), c[1] + r * np.sin(a)])
        elif kind in ("TEXT", "MTEXT", "SOLID"):
            b = ezdxf.bbox.extents([e])
            if b.has_data:
                self.box((b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y))
        elif kind == "DIMENSION":
            for v in e.virtual_entities():
                self.entity(v)


def place_tags(msp, occ, marks):
    """Tag text next to each hole in the freest spot; farther out with a leader if crowded."""
    for tag, cx, cy, rp in marks:
        w, h = text_width(tag, TAG_H), TAG_H * 1.29
        best = None
        for ring, gap in enumerate((0.8, 4.0, 8.0)):
            for k, angle in enumerate((45, 135, -45, -135, 0, 180, 90, -90)):
                u = np.array([np.cos(np.radians(angle)), np.sin(np.radians(angle))])
                dist = rp + gap + 0.5 * abs(w * u[0]) + 0.5 * abs(h * u[1])
                bx, by = cx + dist * u[0], cy + dist * u[1]
                box = (bx - w / 2, by - h / 2, bx + w / 2, by + h / 2)
                # a line through a tag costs far more than moving it out on a leader
                cost = 10 * occ.cost((box[0] - 0.3, box[1] - 0.3, box[2] + 0.3, box[3] + 0.3)) + 60 * ring + k
                if best is None or cost < best[0]:
                    best = (cost, box, ring, u)
        _, box, ring, u = best
        text(msp, tag, box[0], box[1] + 0.29 * TAG_H, h=TAG_H, align="LEFT", layer="HOLES")
        occ.box(box)
        if ring:
            start = (cx + rp * u[0], cy + rp * u[1])
            end = (np.clip(cx, box[0], box[2]), np.clip(cy, box[1], box[3]))
            msp.add_line(start, end, dxfattribs={"layer": "HOLES"})
            occ.polyline([start, end])


def _extreme_point(polylines, axis, low, pick_high):
    """Vertex on the outline at the min/max of `axis`, nearest the dimension line."""
    pts = np.vstack(polylines)
    v = pts[:, axis]
    target = v.min() if low else v.max()
    cand = pts[np.abs(v - target) < 1e-3 * max(1.0, abs(target)) + 1e-6]
    other = cand[:, 1 - axis]
    return cand[np.argmax(other) if pick_high else np.argmin(other)]


def dim_horizontal_above(msp, polylines, ext, origin, s, prefix=""):
    p1 = _extreme_point(polylines, 0, True, True)
    p2 = _extreme_point(polylines, 0, False, True)
    p1, p2 = (ext[0], p1[1]), (ext[2], p2[1])  # exact envelope in x
    y = ext[3] * s + origin[1] + DIM_OFFSET
    return _dim(msp, (0, y), to_paper(p1, origin, s), to_paper(p2, origin, s), 0, s, prefix)


def dim_vertical_left(msp, polylines, ext, origin, s, prefix=""):
    p1 = _extreme_point(polylines, 1, True, False)
    p2 = _extreme_point(polylines, 1, False, False)
    p1, p2 = (p1[0], ext[1]), (p2[0], ext[3])  # exact envelope in y
    x = ext[0] * s + origin[0] - DIM_OFFSET
    return _dim(msp, (x, 0), to_paper(p1, origin, s), to_paper(p2, origin, s), 90, s, prefix)


def _dim(msp, base, p1, p2, angle, s, prefix=""):
    """`prefix` wraps the measured value: "%%c" for Ø, "(" for a reference dimension."""
    label = prefix + "<>" + (")" if prefix.startswith("(") else "")
    dim = msp.add_linear_dim(base=base, p1=tuple(p1), p2=tuple(p2), angle=angle, dimstyle="ISO",
                             text=label, override={"dimlfac": 1 / s},
                             dxfattribs={"layer": "DIMS"})
    dim.render()
    return dim.dimension


def projection_symbol(msp, cx, cy, d=6.0):
    """ISO 5456-2 first-angle symbol: truncated cone, its end view to the right."""
    L, small, gap = d, d / 2, d / 2
    x0 = cx - (L + gap + d) / 2
    x1 = x0 + L
    msp.add_lwpolyline([(x0, cy - d / 2), (x1, cy - small / 2), (x1, cy + small / 2), (x0, cy + d / 2)],
                       close=True, dxfattribs={"layer": "FRAME"})
    ccx = x1 + gap + d / 2
    msp.add_circle((ccx, cy), d / 2, dxfattribs={"layer": "FRAME"})
    msp.add_circle((ccx, cy), small / 2, dxfattribs={"layer": "FRAME"})
    attribs = {"layer": "CENTER", "ltscale": 0.12}
    msp.add_line((x0 - 1, cy), (ccx + d / 2 + 1, cy), dxfattribs=attribs)
    msp.add_line((ccx, cy - d / 2 - 1), (ccx, cy + d / 2 + 1), dxfattribs=attribs)


def texture_symbol(msp, x, y, h1=2.5):
    """ISO 21920-1 basic graphical symbol (apex at x, y)."""
    t = np.tan(np.radians(60))
    msp.add_lwpolyline([(x - h1 / t, y + h1), (x, y), (x + 2 * h1 / t, y + 2 * h1)],
                       dxfattribs={"layer": "TEXT"})


def title_block(msp, lay, tb, mass=""):
    x0, y0 = lay["area"][2] - TB_W, lay["area"][1]

    def cell(c0, r0, c1, r1, label, value, h=H):
        r = (x0 + c0, y0 + TB_ROWS[r0], x0 + c1, y0 + TB_ROWS[r1])
        rect(msp, r)
        if label:
            text(msp, label, r[0] + 1, r[3] - 0.7, h=1.8, align="TOP_LEFT")
        if value:
            text(msp, str(value), r[0] + 1.5, r[1] + 1.6, h=h, align="LEFT", width=r[2] - r[0] - 3)
        return r

    t = tb["title_block"]
    cell(0, 4, 50, 5, "Material", t["material"])
    cell(50, 4, 72, 5, "Mass", mass)
    cell(72, 4, 107, 5, "General tolerances", tb["general_tolerance"])
    r = cell(107, 4, 137, 5, "Surface texture", "")
    if tb["default_finish"]:
        texture_symbol(msp, r[0] + 3, r[1] + 1.2)
        text(msp, tb["default_finish"], r[0] + 7.5, r[1] + 1.6, align="LEFT", width=r[2] - r[0] - 9)
    cell(137, 4, 155, 5, "Scale", fmt_scale(lay["scale"]))
    r = cell(155, 4, 180, 5, "", "")
    projection_symbol(msp, (r[0] + r[2]) / 2, (r[1] + r[3]) / 2)
    cell(0, 3, 35, 4, "Responsible dept.", t["department"])
    cell(35, 3, 70, 4, "Technical reference", t["technical_reference"])
    cell(70, 3, 110, 4, "Document type", t["document_type"])
    cell(110, 3, 145, 4, "Document status", t["status"])
    cell(145, 3, 180, 4, "Date of issue", t["date"])
    cell(0, 2, 35, 3, "Created by", t["drawn_by"])
    cell(35, 2, 70, 3, "Approved by", t["approved_by"])
    cell(70, 1, 180, 3, "Title", t["title"], h=5)
    cell(0, 0, 70, 2, "Legal owner", t["company"], h=5)
    cell(70, 0, 150, 1, "Identification number", t["part_number"], h=5)
    cell(150, 0, 162, 1, "Rev.", t["revision"])
    cell(162, 0, 180, 1, "Sheet", t["sheet"])
    rect(msp, (x0, y0, x0 + TB_W, y0 + TB_H), lw=70)


def note_lines(notes):
    lines = []
    for i, n in enumerate(notes, 1):
        words, line = str(n).split(), f"{i}."
        for word in words:
            if text_width(f"{line} {word}", H) > TB_W:
                lines.append(line)
                line = "    "
            line = f"{line} {word}" if line.strip() else f"{line}{word}"
        lines.append(line)
    return lines


def general_notes(cfg):
    """Standard notes that precede the part's own notes."""
    notes = ["Dimensions in mm." + (" Dimensions in ( ) are for reference only."
                                    if cfg["reference_envelope"] else "")]
    edges = cfg.get("edges") or {}
    parts = [f"{side} {edges[side]}" for side in ("external", "internal") if edges.get(side)]
    if parts:
        notes.append("Undefined edges ISO 13715: " + ", ".join(parts) + ".")
    return notes


NOTE_PITCH = 1.7 * H


def notes_height(notes):
    return (len(note_lines(notes)) + 1) * NOTE_PITCH + PAD if notes else 0


def draw_notes(msp, lay, notes):
    if not notes:
        return
    x = lay["area"][2] - TB_W
    y = lay["area"][1] + TB_H + PAD
    for line in reversed(note_lines(notes)):
        text(msp, line, x, y, width=TB_W)
        y += NOTE_PITCH
    text(msp, "NOTES", x, y)


# ---------------------------------------------------------------- build / render

def view_extents(env, frames):
    """Exact envelope of each orthographic view in view coordinates."""
    lo, hi = np.array(env["min"]), np.array(env["max"])
    corners = np.array([[(lo, hi)[i][0], (lo, hi)[j][1], (lo, hi)[k][2]]
                        for i in (0, 1) for j in (0, 1) for k in (0, 1)])
    out = {}
    for name, (d, y) in frames.items():
        x = np.cross(y, d)
        px, py = corners @ x, corners @ y
        out[name] = (px.min(), py.min(), px.max(), py.max())
    return out


def build(shape, cfg):
    """Return (ezdxf document, info dict) for a part and its merged YAML config."""
    env = geometry.envelope(shape)
    views = cfg["views"]
    front, up = views["front"], views["up"]
    if front == "auto" or up == "auto":
        g_front, g_up = geometry.guess_orientation(shape)
        front = g_front if front == "auto" else front
        up = g_up if up == "auto" else up
    frames = geometry.view_frames(front, up, None if views["iso"] == "auto" else views["iso"])
    geometry.prepare(shape)
    lines = {name: geometry.project(shape, d, y) for name, (d, y) in frames.items()}
    ext = view_extents(env, {k: v for k, v in frames.items() if k != "iso"})
    ext["iso"] = geometry.extents(lines["iso"])
    ortho = ["front", "top", "left"]
    if geometry.same_view(lines["left"], lines["front"], ext["left"], ext["front"]):
        ortho.remove("left")  # e.g. a turned part: the side view repeats the front view
        del ext["left"]

    sym = {name: geometry.symmetry(lines[name]) for name in ortho}
    rows, types, warnings = plan_holes(geometry.holes(shape), frames, ortho, ext, sym, cfg)
    origins = {v: hole_origin(v, frames[v], ext[v], sym[v], cfg.get("datum")) for v in ortho}
    at_centre = {v: bool(sym[v][0] and sym[v][1] and np.allclose(
        origins[v], ((ext[v][0] + ext[v][2]) / 2, (ext[v][1] + ext[v][3]) / 2), atol=1e-6)) for v in ortho}
    tables = hole_tables(rows, types, ortho, origins, at_centre)
    widths, block = table_size(tables) if tables else (None, None)
    if rows and (cfg.get("datum") or {}).get("confirm"):
        warnings.append("datum.confirm is still true - check the hole table origin")

    notes = general_notes(cfg) + list(cfg["notes"] or [])
    lay = choose_layout(ext, notes_height(notes), cfg["sheet"], cfg["scale"], block)
    s = lay["scale"]
    doc = new_doc()
    msp = doc.modelspace()
    draw_frame(msp, lay)

    info = {"sheet": lay["sheet"], "scale": s, "front": front, "up": up, "views": ortho,
            "dims": [], "view_rects": dict(lay["rects"]), "warnings": warnings}
    circles = geometry.circles(shape)
    for name in ortho:
        o = np.array(lay["origins"][name])
        draw_view(msp, lines[name], o, s)
        draw_centre_lines(msp, ext[name], o, s, sym[name])
        centre_marks(msp, circles, frames[name], lines[name], ext[name], o, s, sym[name])

    # overall envelope: width, height, depth once each; one Ø on a round view instead of two sizes
    round_view = next((v for v in ortho if geometry.is_round(lines[v], ext[v])), None)
    plan = {"front": [("front", dim_horizontal_above, "%%c"), ("top", dim_vertical_left, "")],
            "top": [("front", dim_vertical_left, ""), ("top", dim_vertical_left, "%%c")],
            "left": [("front", dim_horizontal_above, ""), ("left", dim_horizontal_above, "%%c")],
            None: [("front", dim_horizontal_above, ""), ("front", dim_vertical_left, ""),
                   ("top", dim_vertical_left, "")]}[round_view]
    ref = "(" if cfg["reference_envelope"] else ""
    for view, fn, prefix in plan:
        dim = fn(msp, lines[view], ext[view], np.array(lay["origins"][view]), s, ref + prefix)
        value = prefix.replace("%%c", "Ø") + str(round(dim.get_measurement() / s, 4))
        info["dims"].append((view, f"({value})" if ref else value))

    details = plan_details(rows, lay, ortho) if rows else []
    for d in details:  # the detail circle itself; its label sits above it
        cx, cy, rr = *d["paper"], d["R"] * d["scale"]
        info["view_rects"][f"detail {d['letter']}"] = (cx - rr, cy - rr, cx + rr, cy + rr)

    iso_w, iso_h = _size(ext["iso"])
    r, fit = free_rect(lay, (iso_w, iso_h))
    iso_scale = next((c for c in SCALES if c <= min(s, fit)), None) if r else None
    if iso_scale:
        cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3] + H + 4) / 2
        e = ext["iso"]
        o = np.array((cx - (e[0] + e[2]) / 2 * iso_scale, cy - (e[1] + e[3]) / 2 * iso_scale))
        draw_view(msp, lines["iso"], o, iso_scale)
        info["view_rects"]["iso"] = (*to_paper(e[:2], o, iso_scale), *to_paper(e[2:], o, iso_scale))
        if iso_scale != s:
            text(msp, f"ISOMETRIC  {fmt_scale(iso_scale)}", cx, info["view_rects"]["iso"][1] - 2,
                 align="TOP_CENTER")

    draw_details(msp, details, lines, frames, ext, sym, circles, lay)
    draw_holes(msp, lay, rows, types, origins, at_centre, tables, widths, details)
    info["holes"] = [{"tag": r["tag"], "type": r["key"], "view": r["view"], "hidden": bool(r["hidden"]),
                      "x": round(float(r["xy"][0] - origins[r["view"]][0]), 3),
                      "y": round(float(r["xy"][1] - origins[r["view"]][1]), 3),
                      "point": np.round(r["entry"]["point"], 3).tolist(),
                      "axis": np.round(r["hole"]["axis"], 4).tolist(),
                      "diameter": round(r["hole"]["diameter"], 3), "through": r["hole"]["through"],
                      "depth": None if r["entry"]["depth"] is None else round(r["entry"]["depth"], 3),
                      "pitch": r["hole"]["pitch"], "colours": r["hole"]["colours"]}
                     for r in sorted(rows, key=lambda r: (r["tag"][0], int(r["tag"][1:])))]
    info["hole_types"] = types
    info["details"] = [f"{d['letter']} ({fmt_scale(d['scale'])}) on the {d['view']} view" for d in details]

    grams = mass_grams(shape, cfg)
    info["mass_g"] = grams
    title_block(msp, lay, cfg, fmt_mass(grams))
    draw_notes(msp, lay, notes)
    info["problems"] = check(doc, info, lay)
    return doc, info


def draw_holes(msp, lay, rows, types, origins, at_centre, tables, widths, details):
    """Hidden holes dashed, ISO 6410 thread arcs, table origins, tags and the tables.

    Holes inside a detail view are annotated there, at the detail's scale.
    """
    if not rows:
        return
    specs = {t["key"]: t["spec"] for t in types}
    spots = {}  # holes drawn on the same spot (near and far side) share one tag
    for r in rows:
        d = r.get("detail")
        o, s = (d["origin"], d["scale"]) if d else (np.array(lay["origins"][r["view"]]), lay["scale"])
        cx, cy = to_paper(r["xy"], o, s)
        rp = r["hole"]["diameter"] / 2 * s
        spec = specs[r["key"]]
        if r["hidden"]:
            msp.add_circle((cx, cy), rp, dxfattribs={"layer": "HIDDEN", "ltscale": 0.25})
        elif spec.get("thread") and not spec.get("confirm"):
            major = holes.thread_major(str(spec["thread"]))
            if major and major > r["hole"]["diameter"]:  # thin 3/4 circle, open at the top right
                msp.add_arc((cx, cy), major / 2 * s, 100, 350, dxfattribs={"layer": "VISIBLE", "lineweight": 25})
        key = (r["view"], round(cx, 1), round(cy, 1))
        spots.setdefault(key, [[], cx, cy, rp])[0].append(r["tag"])
        spots[key][3] = max(spots[key][3], rp)
    for view in {r["view"] for r in rows}:
        if not at_centre[view]:
            inside = [d for d in details if d["view"] == view
                      and np.hypot(*(origins[view] - d["centre"])) < d["R"]]
            o, s = ((inside[0]["origin"], inside[0]["scale"]) if inside
                    else (np.array(lay["origins"][view]), lay["scale"]))
            origin_symbol(msp, *to_paper(origins[view], o, s))
    occ = Occupancy(lay)
    for e in msp:
        occ.entity(e)
    occ.box(lay["title_block"])
    occ.box(lay["table"])
    place_tags(msp, occ, [(", ".join(tags), cx, cy, rp) for tags, cx, cy, rp in spots.values()])
    draw_tables(msp, lay["table"], tables, widths)


def mass_grams(shape, cfg):
    density = partfile.density(cfg)  # g/cm3
    return geometry.volume(shape) * density / 1000 if density else None


def fmt_mass(grams):
    if grams is None:
        return ""
    return f"{grams:.1f} g" if grams < 1000 else f"{grams / 1000:.2f} kg"


def check(doc, info, lay):
    """Overlapping text, text on geometry, anything outside the frame."""
    boxes = []
    for e in doc.modelspace():
        if e.dxftype() == "TEXT" and e.dxf.layer != "FRAME":  # grid labels sit in the margin
            boxes.append((e.dxf.text, ezdxf.bbox.extents([e]), e.dxf.layer))
        elif e.dxftype() == "DIMENSION":
            for v in e.virtual_entities():
                if v.dxftype() in ("MTEXT", "TEXT"):
                    boxes.append((v.plain_text() if v.dxftype() == "MTEXT" else v.dxf.text,
                                  ezdxf.bbox.extents([v]), "DIMS"))
    boxes = [(t, (b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y), layer) for t, b, layer in boxes
             if b.has_data]
    problems = []
    for (ta, a, _), (tb, b, _) in itertools.combinations(boxes, 2):
        if _overlap(a, b, -0.05):
            problems.append(f"text '{ta}' overlaps text '{tb}'")
    for t, a, layer in boxes:
        for name, r in info["view_rects"].items():
            if layer != "HOLES" and _overlap(a, r):  # hole tags belong on their view
                problems.append(f"text '{t}' overlaps {name} view")
        if not _inside(a, lay["area"]):
            problems.append(f"text '{t}' outside frame")
    ext = ezdxf.bbox.extents(e for e in doc.modelspace() if e.dxf.layer in ("VISIBLE", "DIMS"))
    area = lay["area"]
    if ext.extmin.x < area[0] or ext.extmin.y < area[1] or ext.extmax.x > area[2] or ext.extmax.y > area[3]:
        problems.append("geometry or dimensions outside frame")
    return problems


def render(doc, sheet, fmt="pdf", dpi=150):
    w, h = SHEETS[sheet]
    backend = pdf_backend.PyMuPdfBackend()
    cfg = config.Configuration(background_policy=config.BackgroundPolicy.WHITE,
                               color_policy=config.ColorPolicy.BLACK,
                               lineweight_policy=config.LineweightPolicy.ABSOLUTE)
    Frontend(RenderContext(doc), backend, config=cfg).draw_layout(doc.modelspace())
    page = layout.Page(w, h, layout.Units.mm)
    box = BoundingBox2d([(0, 0), (w, h)])
    if fmt == "pdf":
        return backend.get_pdf_bytes(page, render_box=box)
    return backend.get_pixmap_bytes(page, fmt=fmt, dpi=dpi, render_box=box)
