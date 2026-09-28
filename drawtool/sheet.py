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

SHEETS = {"A4": (297, 210), "A3": (420, 297), "A2": (594, 420)}  # landscape, smallest first
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
    "HATCH": ("Continuous", 18),  # section hatching
    "SURFACES": ("Continuous", 25),  # surface texture marks placed in the app
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


def _inside(r, box, tol=1e-3):
    return r[0] >= box[0] - tol and r[1] >= box[1] - tol and r[2] <= box[2] + tol and r[3] <= box[3] + tol


def place(ext, sheet, s, notes_h):
    """Place front/top/left (first angle) at scale s, or None if they do not fit."""
    w, h = SHEETS[sheet]
    area = (LEFT, BOTTOM, w - RIGHT, h - TOP)
    inner = (area[0] + PAD, area[1] + PAD, area[2] - PAD, area[3] - PAD)
    tb = (area[2] - TB_W, area[1], area[2], area[1] + TB_H + notes_h)
    fw, fh = (v * s for v in _size(ext["front"]))
    side = next((v for v in ("left", "section") if v in ext), None)  # right of the front view
    lw = _size(ext[side])[0] * s if side else -GAP
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
        origins = {}
        for name, r in rects.items():
            e = ext[name]
            origins[name] = (r[0] - e[0] * s, r[1] - e[1] * s)
        if side:  # level with the front view
            e, x0 = ext[side], fx + fw + GAP
            origins[side] = (x0 - e[0] * s, origins["front"][1])
            rects[side] = (x0, origins[side][1] + e[1] * s, x0 + lw, origins[side][1] + e[3] * s)
            zones.append((*rects[side][:3], rects[side][3] + DIM_SPACE))
        if all(_inside(z, inner) and not _overlap(z, tb, PAD) for z in zones):
            return {"sheet": sheet, "size": (w, h), "scale": s, "area": area,
                    "title_block": tb, "rects": rects, "zones": zones, "origins": origins,
                    "aux": {}, "letters": iter(DETAIL_LETTERS)}
    return None


def choose_layout(ext, notes_h, sheet="auto", scale="auto", block=None, needs=(), section=None, slot=False):
    """A4 if the part fits at 1:1 or larger, otherwise the best scale on A3;
    A2 only when A3 has no room for the section view at the drawing scale.

    `block` is the size of the hole tables, `needs` the extra views for holes
    and `section` the extents of the section view; all must fit next to the
    main views. With `slot` the section first tries the side view's place
    (right of the front view), else it goes wherever there is room.
    """
    if sheet != "auto" and sheet not in SHEETS:
        raise ValueError(f"unknown sheet size '{sheet}': use auto, {', '.join(SHEETS)}")
    sheets = list(SHEETS) if sheet == "auto" else [sheet]
    scales = SCALES if scale == "auto" else [parse_scale(scale)]
    variants = [({**ext, "section": section}, None)] if section and slot else []
    variants.append((ext, _size(section) if section else None))

    def first_fit(name):
        for s in scales:
            for views, free_section in variants:
                lay = place(views, name, s, notes_h)
                if lay and (not block or _reserve(lay, block)) and \
                        (not free_section or _reserve_section(lay, free_section)) and _reserve_aux(lay, needs):
                    return lay
        return None

    fallback = None
    for name in sheets:
        lay = first_fit(name)
        if lay is None:
            continue
        full = "section" in lay["rects"] or lay.get("section", {}).get("scale", lay["scale"]) == lay["scale"]
        if scale != "auto" or sheet != "auto" or (full and (name != "A4" or lay["scale"] >= 1)):
            return lay
        if name != "A4":
            fallback = fallback or lay  # section reduced: a larger sheet may hold it at full scale
    if fallback:
        return fallback
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


def _reserve_section(lay, size):
    """Room for the section view: the drawing scale if it fits, else down to a fifth of it."""
    s = lay["scale"]
    for c in (c for c in sorted(SCALES, reverse=True) if s / 5 <= c <= s):
        w, h = size[0] * c + 2, size[1] * c + 2
        free, fit = free_rect(lay, (w, h + 10), label=0)
        if free and fit >= 1:
            x0, y1 = free[0] + PAD, free[3] - PAD
            lay["zones"].append((x0, y1 - h - 10, x0 + w, y1))
            lay["section"] = {"scale": c, "paper": np.array([x0 + w / 2, y1 - 10 - h / 2])}
            return True
    return False


def _reserve_aux(lay, needs):
    """Room for each extra view: the largest ISO scale (up to the one that puts its
    holes 10 mm apart) that fits the free space, down to a fifth of the sheet scale."""
    s = lay["scale"]

    def wanted(need):
        return next((c for c in sorted(SCALES) if c >= s and need["gap"] * c >= 10), max(SCALES))

    for need in sorted(needs, key=lambda n: -max(n["size"]) * wanted(n)):  # biggest first
        top = wanted(need)
        for c in (c for c in sorted(SCALES, reverse=True) if s / 5 <= c <= top):
            w, h = need["size"][0] * c + 2, need["size"][1] * c + 2
            free, fit = free_rect(lay, (w, h + 10), label=0)
            if not free or fit < 1:
                continue
            x0, y1 = free[0] + PAD, free[3] - PAD  # top-left corner keeps the rest in one piece
            box = (x0, y1 - h - 10, x0 + w, y1)  # view plus its label
            cx, cy = x0 + w / 2, y1 - 10 - h / 2
            lay["zones"].append(box)
            lay["aux"][need["view"]] = {"letter": need["letter"], "scale": c, "partial": need["partial"],
                                        "paper": np.array([cx, cy]),
                                        "origin": np.array([cx, cy]) - need["centre"] * c}
            break
        else:
            return False
    lay["letters"] = iter(DETAIL_LETTERS[len(needs):])
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


AUX_VIEWS = ("left", "bottom", "right", "rear")  # used only for holes no main view shows


def view_place(lay, view):
    """(origin, scale) mapping a view's coordinates to paper: paper = xy * scale + origin."""
    if view in lay["aux"]:
        return lay["aux"][view]["origin"], lay["aux"][view]["scale"]
    return np.array(lay["origins"][view]), lay["scale"]


def _type_entry(h):
    """The entry that names a hole type: the counterbored or countersunk end if any."""
    return next((e for e in h["entries"] if e["cbore"] or e["csk"]), h["entries"][0])


def plan_holes(found, frames, ortho, cfg):
    """Give every hole a view in which its opening is directly visible, a type and a tag.

    Equal holes stay together in the first view that shows all of them; views
    from below, the right or the rear are only used for holes the main views
    do not show. Holes are never marked in a view where they are hidden.
    """
    views = list(ortho) + [v for v in AUX_VIEWS if v not in ortho]

    def seen(h, view):
        d = frames[view][0]
        return next((e for e in h["entries"] if np.dot(e["out"], d) > 0.999 and e["exposed"]), None)

    groups = defaultdict(list)
    for h in found:
        groups[holes.type_key(h, _type_entry(h))].append(h)
    rows, warnings = [], []
    for key, members in groups.items():
        together = next((v for v in views if all(seen(h, v) for h in members)), None)
        for h in members:
            view = together or next((v for v in views if seen(h, v)), None)
            if view is None:
                warnings.append(f"{key} hole at {np.round(_type_entry(h)['point'], 2).tolist()} is not "
                                "visible square-on from any side and is left out of the hole table")
                continue
            d, y = frames[view]
            e = seen(h, view)
            p = e["point"]
            rows.append({"hole": h, "entry": _type_entry(h), "view": view, "key": key, "point": p, "out": e["out"],
                         "x_dir": np.cross(y, d), "xy": np.array([p @ np.cross(y, d), p @ y])})
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
                    "note": spec["note"], "position": spec["position"], **(user[key] or {})}
        mine = sorted((r for r in rows if r["key"] == key),
                      key=lambda r: (views.index(r["view"]), -round(r["xy"][1], 2), round(r["xy"][0], 2)))
        for i, r in enumerate(mine, 1):
            r["tag"] = f"{letter}{i}"
        h = first[key]["hole"]
        types.append({"letter": letter, "key": key, "count": len(mine), "spec": spec, "kind": h.get("kind", "hole"),
                      "candidates": [] if h.get("kind") == "port" else holes.thread_candidates(h["diameter"], h["pitch"])})
        if holes.pending(spec):
            warnings.append(f"hole type {letter} ({key}): '{holes.pending(spec)}' not confirmed")
    return rows, types, warnings


def aux_needs(rows, ortho, ext):
    """Extra views the holes need: the whole view, or a partial circle for a small area."""
    needs = []
    for view in AUX_VIEWS:
        mine = [r for r in rows if r["view"] == view]
        if view in ortho or not mine:
            continue
        pts = np.array([r["xy"] for r in mine])
        rmax = max(r["hole"]["diameter"] for r in mine) / 2
        lo, hi = pts.min(axis=0) - rmax, pts.max(axis=0) + rmax
        e = ext[view]
        gap = min((np.hypot(*(a - b)) for a, b in itertools.combinations(pts, 2)), default=np.inf)
        need = {"view": view, "letter": DETAIL_LETTERS[len(needs)], "gap": max(gap, 1e-3)}
        if hi[0] - lo[0] > 0.5 * _size(e)[0] or hi[1] - lo[1] > 0.5 * _size(e)[1]:
            need.update(partial=None, size=_size(e), centre=np.array([(e[0] + e[2]) / 2, (e[1] + e[3]) / 2]))
        else:
            centre = (lo + hi) / 2
            R = max(np.hypot(*(p - centre)) for p in pts) * 1.3 + rmax + 1.5
            need.update(partial=(centre, R), size=(2 * R, 2 * R), centre=centre)
        needs.append(need)
    return needs


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


def _pick(value):
    """A datum picked as a model point [x, y, z], or None for auto."""
    return np.array(value, float) if isinstance(value, (list, tuple)) and len(value) == 3 else None


def plan_datums(rows, faces, cfg, ortho):
    """ISO 5459 datums the hole tables and position tolerances refer to.

    A: the flat face most openings lie on (those in the main views first, then the
    larger face). B: the largest opening type on A, a waveguide port before a
    hole; a single feature or the whole group (a pattern datum). C: only when
    picked. `datum.A`/`B`/`C` in the YAML may give a model point instead (a point
    on the face; a point at the opening), or "none".
    Returns None when there is nothing to refer to.
    """
    dcfg = cfg.get("datum") or {}
    if not rows or str(dcfg.get("A", "auto")).lower() == "none":
        return None
    at = _pick(dcfg.get("A"))
    if at is not None:
        near = [f for f in faces if geometry.on_face(f, at, tol=0.05)]
        face = max(near, key=lambda f: f["area"]) if near else None
    else:
        def score(f):
            mine = [r for r in rows if geometry.on_face(f, r["point"], r["out"])]
            return len(mine), sum(r["view"] in ortho for r in mine), f["area"]
        face = max(faces, key=score, default=None)
        if face is not None and score(face)[0] == 0:
            face = None
    if face is None:
        return None
    on_a = [r for r in rows if geometry.on_face(face, r["point"], r["out"])]

    def picked(key):
        p = _pick(dcfg.get(key))
        if p is None:
            return []
        return [min(rows, key=lambda r: np.linalg.norm(r["point"] - p))]

    b = picked("B")
    if not b and str(dcfg.get("B", "auto")).lower() != "none" and on_a:
        best = max(on_a, key=lambda r: (r["hole"].get("kind") == "port", r["hole"]["diameter"]))
        b = [r for r in on_a if r["key"] == best["key"]]
    c = picked("C")
    origin = np.mean([r["point"] for r in b], axis=0) if b else None
    return {"A": face, "B": b, "C": c, "origin": origin,
            "letters": ["A"] + (["B"] if b else []) + (["C"] if c else [])}


def hole_origins(rows, views, frames, ext, sym, datum, needs, datums=None):
    """Origin, origin-at-centre-lines flag and table note for each view with holes.

    With datums, X/Y run from datum B (where it meets datum A) in every view,
    unless `datum.origin` sets another origin.
    """
    titles = {n["view"]: f"view {n['letter']}" for n in needs}
    partial = {n["view"]: n["partial"] for n in needs if n["partial"]}
    use_datum = datums and datums["origin"] is not None and (datum or {}).get("origin", "auto") == "auto"
    origins, at_centre, notes = {}, {}, {}
    for v in views:
        mine = [r for r in rows if r["view"] == v]
        if not mine:
            continue
        d, y = frames[v]
        if use_datum:
            o = np.array([datums["origin"] @ np.cross(y, d), datums["origin"] @ y])
        else:
            o = hole_origin(v, frames[v], ext[v], sym[v], datum)
        title = titles.get(v, f"{v} view")
        centre = np.array([(ext[v][0] + ext[v][2]) / 2, (ext[v][1] + ext[v][3]) / 2])
        where = title if v in titles else f"the {title}"
        if v in partial and np.hypot(*(o - partial[v][0])) > 0.9 * partial[v][1]:
            first = min(mine, key=lambda r: (r["tag"][0], int(r["tag"][1:])))
            o, at_centre[v] = first["xy"], False  # the usual origin is outside the partial view
            notes[v] = f"X/Y from the centre of hole {first['tag']} in {title}"
        else:
            at_centre[v] = bool(sym[v][0] and sym[v][1] and np.allclose(o, centre, atol=1e-6))
            mark = "the centre lines" if at_centre[v] else "the origin marked"
            notes[v] = (f"X/Y from datum B ({mark[4:]} in {where})" if use_datum
                        else f"X/Y from {mark} in {where}")
        origins[v] = o
    return origins, at_centre, notes


HEADER = ("TAG", "X", "Y", "SIZE")
SIZE_W = 45  # mm; longer hole descriptions wrap inside their cell


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


def _datum_refs(group, datums):
    """Datum letters a group's position tolerance refers to, and its own letter if it is a datum."""
    if not datums:
        return [], None
    ids = {id(r["hole"]) for r in group}  # a through hole is the same datum seen from either end
    if datums["B"] and ids <= {id(r["hole"]) for r in datums["B"]}:
        return ["A"], "B"
    if datums["C"] and ids <= {id(r["hole"]) for r in datums["C"]}:
        return ["A", "B"], "C"
    return datums["letters"], None


def _tag_order(r):
    return r["tag"][0], int(r["tag"][1:])


def bolt_circle(group):
    """(centre, radius, angle of the first hole) when 3 or more equal holes sit equally
    spaced on a full circle, else None. A square of 4 on the axes stays X/Y."""
    if len(group) < 3 or group[0]["hole"].get("kind") == "port":
        return None
    p = np.array([r["xy"] for r in group])
    if len(group) == 4 and len(np.unique(p[:, 0].round(3))) <= 2 and len(np.unique(p[:, 1].round(3))) <= 2:
        return None
    sol = np.linalg.lstsq(np.c_[2 * p, np.ones(len(p))], (p ** 2).sum(axis=1), rcond=None)[0]
    c = sol[:2]
    radius = float(np.sqrt(max(sol[2] + c @ c, 0.0)))
    if radius < 1e-3 or np.abs(np.hypot(*(p - c).T) - radius).max() > max(0.01, 1e-4 * radius):
        return None
    angles = np.degrees(np.arctan2(p[:, 1] - c[1], p[:, 0] - c[0])) % 360
    ordered = np.sort(angles)
    steps = np.diff(np.append(ordered, ordered[0] + 360))
    if np.abs(steps - 360 / len(p)).max() > 0.05:
        return None
    return c, radius, float(angles[0])


def bolt_circles(rows):
    """{(view, hole type): (centre, radius, first angle)} for the equally spaced patterns."""
    out = {}
    for view, key in dict.fromkeys((r["view"], r["key"]) for r in rows):
        group = sorted((r for r in rows if r["view"] == view and r["key"] == key), key=_tag_order)
        found = bolt_circle(group)
        if found:
            out[view, key] = found
    return out


def hole_tables(rows, types, views, origins, notes, titles, datums=None):
    """[(title, note, [(size lines, [(tag, X, Y)], position frame)])] per view: equal holes
    grouped under one size; the frame is (tolerance, datum letters) or None.
    An equally spaced pattern on a circle is one row: its centre, pitch circle and first angle."""
    specs = {t["key"]: t["spec"] for t in types}
    circles = bolt_circles(rows)
    tables = []
    for view in views:
        mine = sorted((r for r in rows if r["view"] == view), key=_tag_order)
        if not mine:
            continue
        groups = []
        parts = []
        for key in dict.fromkeys(r["key"] for r in mine):  # types in tag order
            group = [r for r in mine if r["key"] == key]
            # a datum feature gets its own row group: its tolerance cannot refer to itself
            marked = {id(r["hole"]) for k in ("B", "C") for r in (datums or {}).get(k) or []}
            own_rows = [r for r in group if id(r["hole"]) in marked]
            if own_rows and len(own_rows) < len(group):
                parts += [(key, own_rows, None), (key, [r for r in group if id(r["hole"]) not in marked], None)]
            else:
                parts.append((key, group, circles.get((view, key))))
        for key, group, pattern in parts:
            size = holes.size_text(group[0]["hole"], group[0]["entry"], specs[key], group[0]["x_dir"])
            refs, own = _datum_refs(group, datums)
            if pattern:
                c, radius, first = pattern
                size += f", PCD Ø{holes.fmt(2 * radius)} EQS, {group[0]['tag']} at {holes.fmt(first)}°"
            if own:
                size += f", datum {own}"
            lines = _wrap(f"{len(group)}× {size}" if len(group) > 1 else size, SIZE_W)
            cells = []
            for tag, xy in ([(f"{group[0]['tag']}–{group[-1]['tag']}", pattern[0])] if pattern else
                            [(r["tag"], r["xy"]) for r in group]):
                x, y = (v if abs(v) >= 0.005 else 0.0 for v in xy - origins[view])
                cells.append((tag, f"{x:.2f}", f"{y:.2f}"))
            pos = str(specs[key].get("position") or "").strip()
            groups.append((lines, cells, (pos, refs) if pos else None))
        tables.append((f"HOLE TABLE - {titles[view]}", notes[view], groups))
    return tables


FCF_H = 4.5  # feature control frame height in the hole table


def fcf_width(frame):
    value, refs = frame
    return FCF_H + text_width(value, TAG_H) + 2 + FCF_H * len(refs)


def feature_frame(msp, x, y, frame):
    """ISO 1101 tolerance frame [position symbol | value | datums], lower-left at (x, y)."""
    value, refs = frame
    widths = [FCF_H, text_width(value, TAG_H) + 2] + [FCF_H] * len(refs)
    edges = np.cumsum([x] + widths)
    rect(msp, (x, y, edges[-1], y + FCF_H), layer="HOLES")
    for e in edges[1:-1]:
        msp.add_line((e, y), (e, y + FCF_H), dxfattribs={"layer": "HOLES"})
    cx, cy = x + FCF_H / 2, y + FCF_H / 2  # position symbol: circle and cross
    msp.add_circle((cx, cy), 1.1, dxfattribs={"layer": "HOLES"})
    msp.add_line((cx - 1.7, cy), (cx + 1.7, cy), dxfattribs={"layer": "HOLES"})
    msp.add_line((cx, cy - 1.7), (cx, cy + 1.7), dxfattribs={"layer": "HOLES"})
    for label, a, b in zip([value] + refs, edges[1:-1], edges[2:]):
        text(msp, label, (a + b) / 2, cy, h=TAG_H, align="MIDDLE_CENTER", layer="HOLES")


def table_size(tables):
    cells = [c for _, _, groups in tables for _, rows, _ in groups for c in rows] + [HEADER[:3]]
    lines = [t for _, _, groups in tables for ls, _, _ in groups for t in ls] + [HEADER[3]]
    frames = [f for _, _, groups in tables for _, _, f in groups if f]
    widths = [max(text_width(c[k], TAG_H) for c in cells) + 3 for k in range(3)]
    widths += [max(text_width(t, TAG_H) for t in lines) + 3]
    widths[1] = widths[2] = max(widths[1], widths[2], text_width("-00.00", TAG_H) + 3)
    if frames:
        widths.append(max(max(fcf_width(f) for f in frames), text_width(POSITION, TAG_H)) + 3)
    title_w = max(max(text_width(t, TAG_H + 0.5), text_width(n, TAG_H)) for t, n, _ in tables) + 3
    widths[3] += max(0, title_w - sum(widths))
    height = sum(ROW * (3 + sum(max(len(r), len(ls)) for ls, r, _ in groups)) for _, _, groups in tables)
    return widths, (sum(widths), height + PAD * (len(tables) - 1))


POSITION = "POSITION"


def draw_tables(msp, box, tables, widths):
    """Hole tables; the SIZE (and POSITION) cell of a group of equal holes spans all its rows."""
    x0, top = box[0], box[3]
    xs = np.cumsum([x0] + widths)
    header = HEADER + ((POSITION,) if len(widths) > 4 else ())
    for title, note, groups in tables:
        bottom = top - ROW * (2 + sum(max(len(r), len(ls)) for ls, r, _ in groups))
        rect(msp, (x0, bottom, xs[-1], top), lw=50)
        text(msp, title, x0 + 1.5, top - ROW + 1.3, h=TAG_H + 0.5, align="LEFT")
        y = top - ROW
        msp.add_line((x0, y), (xs[-1], y), dxfattribs={"layer": "FRAME"})
        for k, label in enumerate(header):
            text(msp, label, xs[k] + 1.5, y - ROW + 1.5, h=TAG_H, align="LEFT")
        y -= ROW
        for lines, cells, frame in groups:
            height = ROW * max(len(cells), len(lines))
            step = height / len(cells)
            msp.add_line((x0, y), (xs[-1], y), dxfattribs={"layer": "FRAME", "lineweight": 50})
            for i, (tag, xv, yv) in enumerate(cells):
                if i:
                    msp.add_line((x0, y - i * step), (xs[3], y - i * step), dxfattribs={"layer": "FRAME"})
                base = y - (i + 1) * step + (step - TAG_H) / 2
                text(msp, tag, xs[0] + 1.5, base, h=TAG_H, align="LEFT")
                text(msp, xv, xs[2] - 1.5, base, h=TAG_H, align="RIGHT")
                text(msp, yv, xs[3] - 1.5, base, h=TAG_H, align="RIGHT")
            base = y - (height - len(lines) * ROW) / 2 - ROW + (ROW - TAG_H) / 2
            for j, line in enumerate(lines):  # centred in the merged SIZE cell
                text(msp, line, xs[3] + 1.5, base - j * ROW, h=TAG_H, align="LEFT")
            if frame:
                feature_frame(msp, xs[4] + 1.5, y - height / 2 - FCF_H / 2, frame)
            y -= height
        for xv in xs[1:-1]:
            msp.add_line((xv, bottom), (xv, top - ROW), dxfattribs={"layer": "FRAME"})
        text(msp, note, x0, bottom - ROW + 1.5, h=TAG_H, align="LEFT")
        top = bottom - ROW - PAD


def _centre_lines_in_circle(msp, ext, sym, centre, R, o, s):
    """The view's symmetry lines, clipped to a circular (detail or partial) view."""
    for k, on in enumerate(sym):
        at = (ext[k] + ext[k + 2]) / 2
        if on and abs(at - centre[k]) < R:
            half = np.sqrt(R * R - (at - centre[k]) ** 2)
            a, b = np.array(centre, float), np.array(centre, float)
            a[k] = b[k] = at
            a[1 - k] -= half
            b[1 - k] += half
            msp.add_line(tuple(to_paper(a, o, s)), tuple(to_paper(b, o, s)),
                         dxfattribs={"layer": "CENTER", "ltscale": 0.5})


def _circles_in(circles, frame, centre, R):
    d, y = frame
    x = np.cross(y, d)
    return [(c, a, r) for c, a, r in circles if np.hypot(c @ x - centre[0], c @ y - centre[1]) + r < R]


def draw_aux(msp, lay, lines, frames, ext, sym, circles):
    """Extra views for holes (ISO 128-3 reference-arrow views): whole or partial, with letter."""
    rects = {}
    for view, a in lay["aux"].items():
        o, c, e = a["origin"], a["scale"], ext[view]
        if a["partial"]:
            centre, R = a["partial"]
            shown = geometry.clip_to_circle(lines[view], centre, R)
            cx, cy = to_paper(centre, o, c)
            msp.add_circle((cx, cy), R * c, dxfattribs={"layer": "DIMS"})
            _centre_lines_in_circle(msp, e, sym[view], centre, R, o, c)
            inside, pixels = _circles_in(circles, frames[view], centre, R), 150
            rects[view] = (cx - R * c, cy - R * c, cx + R * c, cy + R * c)
        else:
            shown, inside, pixels = lines[view], circles, 600
            draw_centre_lines(msp, e, o, c, sym[view])
            rects[view] = (*to_paper(e[:2], o, c), *to_paper(e[2:], o, c))
        draw_view(msp, shown, o, c)
        if shown:
            centre_marks(msp, inside, frames[view], shown, e, o, c, sym[view], pixels=pixels)
        label = a["letter"] if c == lay["scale"] else f"{a['letter']} ({fmt_scale(c)})"
        text(msp, label, a["paper"][0], rects[view][3] + 3, h=5, align="BOTTOM_CENTER")
    return rects


def plan_details(rows, lay, views):
    """Enlarged detail views (ISO 128-3) where hole centres crowd on paper.

    Scale: the smallest ISO 5455 scale that puts the closest holes 10 mm apart,
    stepping down until the detail fits the free space.
    """
    details = []
    for view in views:
        s = view_place(lay, view)[1]
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
                cx, cy = free[0] + PAD + size[0] / 2, free[3] - PAD - 10 - R * sc  # top-left corner
                box = (cx - size[0] / 2, cy - R * sc - 1, cx + size[0] / 2, cy + R * sc + 9)
                lay["zones"].append(box)
                detail = {"view": view, "letter": next(lay["letters"]), "centre": centre, "R": R,
                          "scale": sc, "paper": np.array([cx, cy]), "box": box,
                          "origin": np.array([cx, cy]) - centre * sc}
                details.append(detail)
                for q in p:
                    for r in spots[tuple(q)]:
                        r["detail"] = detail
                break
    return details


def draw_details(msp, details, lines, frames, ext, sym, circles, lay):
    """Circle and letter on the source view; the enlarged, clipped view with its label."""
    for d in details:
        view, c, R, sc, o = d["view"], d["centre"], d["R"], d["scale"], d["origin"]
        vo, vs = view_place(lay, view)
        mx, my = to_paper(c, vo, vs)
        msp.add_circle((mx, my), R * vs, dxfattribs={"layer": "DIMS"})
        text(msp, d["letter"], mx + 0.71 * R * vs + 1, my + 0.71 * R * vs + 1, h=5, layer="HOLES")
        clipped = geometry.clip_to_circle(lines[view], c, R)
        draw_view(msp, clipped, o, sc)
        msp.add_circle(tuple(d["paper"]), R * sc, dxfattribs={"layer": "DIMS"})
        text(msp, f"{d['letter']} ({fmt_scale(sc)})", d["paper"][0], d["paper"][1] + R * sc + 3, h=5,
             align="BOTTOM_CENTER")
        _centre_lines_in_circle(msp, ext[view], sym[view], c, R, o, sc)
        if clipped:
            centre_marks(msp, _circles_in(circles, frames[view], c, R), frames[view], clipped, ext[view],
                         o, sc, sym[view], pixels=150)


def plan_section(shape, frames, ext, cfg):
    """Section through the middle of the front view, seen from the left (ISO 128-44).

    Only kept when it shows something the outside views do not: more than one
    cut face, or a cut face with holes in it.
    """
    if str(cfg.get("section", "auto")).lower() in ("none", "false", "off"):
        return None
    x = np.cross(frames["front"][1], frames["front"][0])
    at = (ext["front"][0] + ext["front"][2]) / 2
    lines, loops = geometry.section_view(shape, frames["left"], x, at)
    if not loops or (len(loops) == 1 and len(loops[0]) == 1):
        return None
    return {"lines": lines, "loops": loops, "at": at, "ext": geometry.extents(lines)}


def _area(loop):
    x, y = np.asarray(loop).T
    return 0.5 * (x @ np.roll(y, -1) - y @ np.roll(x, -1))


def draw_section(msp, sec, lay, frames, circles, axis=False):
    """The section view: outline, 45° hatching of the cut faces (ISO 128-50), centre lines, label.

    `axis`: the part is symmetric about the vertical centre line of the front
    view and its side view repeats the front (a turned part): the section gets
    that axis even where a cut rib or strut breaks the picture's symmetry.
    """
    o, c, e = sec["origin"], sec["scale"], sec["ext"]
    draw_view(msp, sec["lines"], o, c)
    for face in sec["loops"]:
        paper = [to_paper(loop, o, c) for loop in face]
        area = abs(_area(paper[0])) - sum(abs(_area(p)) for p in paper[1:])
        length = sum(np.hypot(*np.diff(np.vstack([p, p[:1]]), axis=0).T).sum() for p in paper)
        spacing = 2.0 if 2 * area / length > 4 else 1.0  # closer lines on thin walls
        hatch = msp.add_hatch(dxfattribs={"layer": "HATCH"})
        hatch.set_pattern_fill("ANSI31", scale=spacing / 3.175)
        for i, loop in enumerate(paper):
            hatch.paths.add_polyline_path(loop.tolist(), is_closed=True, flags=1 if i == 0 else 0)
    sym = geometry.symmetry(sec["lines"])
    sym = (sym[0] or axis, sym[1])
    draw_centre_lines(msp, e, o, c, sym)
    centre_marks(msp, circles, frames["left"], sec["lines"], e, o, c, sym)
    r = (*to_paper(e[:2], o, c), *to_paper(e[2:], o, c))
    label = f"{sec['letter']}–{sec['letter']}"
    text(msp, label if c == lay["scale"] else f"{label} ({fmt_scale(c)})", (r[0] + r[2]) / 2, r[3] + 3, h=5,
         align="BOTTOM_CENTER")
    return r


def cutting_plane(msp, lay, sec, details):
    """ISO 128-44 cutting plane on the front view: thick ends beyond the outline
    (and beyond detail circles on the line), arrows in the viewing direction and
    the section letter."""
    o, s = view_place(lay, "front")
    r = lay["rects"]["front"]
    x = sec["at"] * s + o[0]
    top, bottom = r[3], r[1]
    for d in details:
        if d["view"] == "front":
            (cx, cy), rr = to_paper(d["centre"], o, s), d["R"] * s
            if abs(cx - x) < rr:
                half = np.sqrt(rr * rr - (cx - x) ** 2)
                top, bottom = max(top, cy + half), min(bottom, cy - half)
    u = np.array([1.0, 0.0])  # the section looks from the left, i.e. to the right on the front view
    for y0, sign in ((top, 1), (bottom, -1)):
        a, b = np.array([x, y0 + sign * 1.0]), np.array([x, y0 + sign * 6.0])
        msp.add_line(tuple(a), tuple(b), dxfattribs={"layer": "CENTER", "linetype": "Continuous", "lineweight": 50})
        tip = b + 6 * u
        msp.add_line(tuple(b), tuple(tip - 2.4 * u), dxfattribs={"layer": "DIMS"})
        msp.add_solid([tuple(tip), tuple(tip - 2.5 * u + (0, 0.8)), tuple(tip - 2.5 * u - (0, 0.8))],
                      dxfattribs={"layer": "DIMS"})
        text(msp, sec["letter"], *(tip + 1.5 * u), h=5, align="MIDDLE_LEFT")


def origin_symbol(msp, x, y, size=7.0):
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

    def fill(self, loops):
        """Mark the inside of polygons (even-odd, so islands stay free), row by row."""
        if not loops:
            return
        edges = np.vstack([np.c_[p, np.roll(p, -1, axis=0)] for p in loops])  # x0 y0 x1 y1
        lo, hi = np.vstack(loops).min(axis=0), np.vstack(loops).max(axis=0)
        for row in range(self._i(lo[1]), self._i(hi[1]) + 1):
            y = (row + 0.5) * self.RES
            e = edges[(edges[:, 1] <= y) != (edges[:, 3] <= y)]
            xs = np.sort(e[:, 0] + (y - e[:, 1]) * (e[:, 2] - e[:, 0]) / (e[:, 3] - e[:, 1]))
            for a, b in zip(xs[::2], xs[1::2]):
                self.grid[row, self._i(a):self._i(b) + 1] = True

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
        elif kind == "HATCH":
            self.fill([np.array([v[:2] for v in path.vertices]) for path in e.paths
                       if hasattr(path, "vertices") and len(path.vertices) > 2])
        elif kind in ("TEXT", "MTEXT", "SOLID"):
            b = ezdxf.bbox.extents([e])
            if b.has_data:
                self.box((b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y))
        elif kind == "DIMENSION":
            for v in e.virtual_entities():
                self.entity(v)


def place_arrows(msp, occ, lay, frames, ortho, arrows):
    """ISO 128-3 reference arrows on a main view, pointing the way an extra view looks."""
    for letter, d, points in arrows:
        best = None
        for view in ortho:
            dv, yv = frames[view]
            if abs(np.dot(d, dv)) > 1e-6:
                continue  # the extra view's direction must lie in this view's plane
            xv = np.cross(yv, dv)
            u = np.array([np.dot(d, xv), np.dot(d, yv)])  # towards the viewer of the extra view
            o, s = view_place(lay, view)
            r = lay["rects"][view]
            aim = to_paper(np.mean([[p @ xv, p @ yv] for p in points], axis=0), o, s)
            k = 0 if abs(u[0]) > 0.5 else 1  # the arrow runs along x (k = 0) or y (k = 1)
            edge = r[k + 2] if u[k] > 0 else r[k]
            lo, hi = r[1 - k], r[3 - k]
            for inset in (0, 6, 12, 18):
                for shift in (0, 6, -6, 12, -12, 20, -20, 30, -30, 45, -45):
                    head = np.array(aim, float)
                    head[k] = edge + u[k] * (2.5 - inset)
                    head[1 - k] = np.clip(aim[1 - k] + shift, lo, hi)
                    tail = head + u * 8
                    w = text_width(letter, 5)
                    far = tail + u * (1.5 + (w if k == 0 else 5))
                    box = (min(head[0], far[0]) - w / 2 - 1, min(head[1], far[1]) - 2.5,
                           max(head[0], far[0]) + w / 2 + 1, max(head[1], far[1]) + 2.5)
                    cost = 10 * occ.cost(box) + abs(shift) + 3 * inset
                    if best is None or cost < best[0]:
                        best = (cost, head, tail, u, box)
        if best is None:
            continue
        _, head, tail, u, box = best
        side = np.array([-u[1], u[0]])
        msp.add_line(tuple(tail), tuple(head + u * 2.4), dxfattribs={"layer": "HOLES"})
        msp.add_solid([tuple(head), tuple(head + u * 2.5 + side * 0.8), tuple(head + u * 2.5 - side * 0.8)],
                      dxfattribs={"layer": "HOLES"})
        align = {(0, 1): "BOTTOM_CENTER", (0, -1): "TOP_CENTER", (1, 0): "MIDDLE_LEFT",
                 (-1, 0): "MIDDLE_RIGHT"}[tuple(int(round(v)) for v in u)]
        text(msp, letter, *(tail + u * 1.5), h=5, align=align, layer="HOLES")
        occ.box(box)


DATUM_BOX = 2 * H  # ISO 5459 frame: twice the lettering height


def datum_summary(datums, where):
    """Datums for features.json and the app: A's face, the tags of B and C, the table origin."""
    face = datums["A"]
    out = {"A": {"normal": np.round(face["normal"], 4).tolist(), "centre": np.round(face["centre"], 3).tolist(),
                 "area": round(face["area"], 2)}}
    for letter in ("B", "C"):
        if datums[letter]:
            out[letter] = {"tags": [r["tag"] for r in datums[letter]], "type": datums[letter][0]["key"],
                           "view": where(datums[letter][0])}
    if datums["origin"] is not None:
        out["origin"] = np.round(datums["origin"], 3).tolist()
    return out


def datum_marks(shape, datums, lay, frames, lines, views, section):
    """(letter, [(paper point on the feature, outward unit direction)]) per datum.

    A goes on a view that shows its face edge-on where nothing stands in front
    of it; B and C on the outline of their (first) opening, in the view or
    detail where it is tagged.
    """
    places = {v: (frames[v], lines[v], *view_place(lay, v), None) for v in views}
    if section:  # material in front of the cutting plane is gone in the section
        x_front = np.cross(frames["front"][1], frames["front"][0])
        places["section"] = (frames["left"], section["lines"], section["origin"], section["scale"],
                             (x_front, section["at"]))
    face, spots = datums["A"], []
    for frame, polylines, o, s, cut in sorted(places.values(), key=lambda p: -p[3]):  # larger scale first
        d, y = frame
        x = np.cross(y, d)
        n = face["normal"]
        n2 = np.array([n @ x, n @ y])
        if abs(n @ d) > 1e-6 or not polylines or np.abs(n2).max() < 0.999:
            continue  # not seen edge-on (only axis-aligned faces are)
        t2 = np.array([-n2[1], n2[0]])
        pts = face["pts"]
        if cut is not None:
            pts = pts[pts @ cut[0] >= cut[1] - 1e-6]
            if len(pts) < 2:
                continue
        depth = (pts @ d).max() - 1e-3  # the face's edge nearest the viewer
        along = pts @ np.c_[x, y] @ t2
        c = float(np.mean(pts @ n))
        for f in (0.35, 0.65, 0.2, 0.8, 0.5):  # the middle often carries a centre line
            q = c * n2 + (along.min() + f * np.ptp(along)) * t2
            p3 = q[0] * x + q[1] * y + depth * d + 0.01 * n
            stop = 1e5 if cut is None else p3 @ cut[0] - cut[1]  # up to the cutting plane
            if stop <= 0.02 or geometry.clear_ray(shape, p3, d, stop=stop):
                spots.append((to_paper(q, o, s), n2))
    marks = [("A", spots)]
    for letter in ("B", "C"):
        members = datums[letter]
        if not members:
            continue
        r = min(members, key=lambda r: (r["tag"][0], int(r["tag"][1:])))
        dd = r.get("detail")
        o, s = (dd["origin"], dd["scale"]) if dd else view_place(lay, r["view"])
        c, h = to_paper(r["xy"], o, s), r["hole"]
        if h.get("kind") == "port":
            a, b = (v / 2 * s for v in h["size"])
            hx, hy = (a, b) if abs(h["long"] @ r["x_dir"]) > 0.5 else (b, a)
        else:  # on the outermost circle seen: the counterbore or countersink if there is one
            e = r["entry"]
            hx = hy = max(h["diameter"], (e["cbore"] or [0])[0], (e["csk"] or [0])[0]) / 2 * s
        marks.append((letter, [(c + (0, hy), np.array([0.0, 1.0])), (c - (0, hy), np.array([0.0, -1.0])),
                               (c + (hx, 0), np.array([1.0, 0.0])), (c - (hx, 0), np.array([-1.0, 0.0]))]))
    return marks


def place_datums(msp, occ, marks):
    """ISO 5459 datum feature indicators: filled triangle on the feature, leader, framed letter,
    each where its frame lands in the freest space."""
    for letter, spots in marks:
        best = None
        for k, (p, n) in enumerate(spots):
            for length in (2.0, 4.0, 7.0, 11.0):
                apex = p + 2.6 * n
                end = apex + length * n
                c = end + n * DATUM_BOX / 2
                box = (c[0] - DATUM_BOX / 2, c[1] - DATUM_BOX / 2, c[0] + DATUM_BOX / 2, c[1] + DATUM_BOX / 2)
                lead = (min(apex[0], end[0]) - 0.3, min(apex[1], end[1]) - 0.3,
                        max(apex[0], end[0]) + 0.3, max(apex[1], end[1]) + 0.3)
                cost = 10 * occ.cost((box[0] - 0.5, box[1] - 0.5, box[2] + 0.5, box[3] + 0.5)) + \
                    3 * occ.cost(lead) + length + 2 * k
                if best is None or cost < best[0]:
                    best = (cost, p, n, apex, end, box)
        if best is None:
            continue
        _, p, n, apex, end, box = best
        t = np.array([-n[1], n[0]])
        msp.add_solid([tuple(p + 1.5 * t), tuple(p - 1.5 * t), tuple(apex)], dxfattribs={"layer": "HOLES"})
        msp.add_line(tuple(apex), tuple(end), dxfattribs={"layer": "HOLES"})
        rect(msp, box, layer="HOLES")
        text(msp, letter, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, align="MIDDLE_CENTER", layer="HOLES")
        occ.box(box)
        occ.polyline([p, end])


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


def texture_symbol(msp, x, y, h1=2.5, kind="any", layer="TEXT"):
    """ISO 21920-1 graphical symbol, apex at (x, y): basic ("any" process), with a bar
    ("machined": material removal required) or a circle ("as built": no material removal).
    Returns the top of the long leg."""
    t = np.tan(np.radians(60))
    msp.add_lwpolyline([(x - h1 / t, y + h1), (x, y), (x + 2 * h1 / t, y + 2 * h1)], dxfattribs={"layer": layer})
    if kind == "machined":
        msp.add_line((x - h1 / t, y + h1), (x + h1 / t, y + h1), dxfattribs={"layer": layer})
    elif kind == "as built":
        msp.add_circle((x, y + 2 * h1 / 3), h1 / 3, dxfattribs={"layer": layer})
    return np.array([x + 2 * h1 / t, y + 2 * h1])


def surface_marks(msp, marks, places):
    """Surface texture marks placed in the app: a leader from the surface (arrow on an
    edge-on surface, dot on one seen face-on) to a short reference line carrying the
    symbol, its process ("machined" / "as built") above and the Ra value below.
    Returns the marks whose view is not on the drawing."""
    missing = []
    for m in marks or []:
        place = places.get(m.get("view"))
        if not place or not m.get("point") or not m.get("at"):
            missing.append(m)
            continue
        d, y = (np.array(v) for v in place["frame"])
        p3 = np.array(m["point"], float)
        tip = to_paper(np.array([p3 @ np.cross(y, d), p3 @ y]), np.array(place["origin"]), place["scale"])
        knee = tip + np.array(m["at"], float)
        u = (tip - knee) / max(np.linalg.norm(tip - knee), 1e-9)
        attribs = {"layer": "SURFACES"}
        if m.get("edge"):
            msp.add_line(tuple(knee), tuple(tip - 2.4 * u), dxfattribs=attribs)
            side = np.array([-u[1], u[0]])
            msp.add_solid([tuple(tip), tuple(tip - 2.5 * u + 0.8 * side), tuple(tip - 2.5 * u - 0.8 * side)],
                          dxfattribs=attribs)
        else:
            msp.add_line(tuple(knee), tuple(tip), dxfattribs=attribs)
            msp.add_circle(tuple(tip), 0.5, dxfattribs=attribs)
            msp.add_hatch(dxfattribs=attribs).paths.add_polyline_path(
                [tuple(tip + 0.5 * np.array([np.cos(a), np.sin(a)])) for a in np.linspace(0, 2 * np.pi, 12)],
                is_closed=True)
        kind = m.get("finish", "machined")
        words = [w for w in ("machined" if kind == "machined" else "as built", str(m.get("ra") or "").strip()) if w]
        width = max(text_width(w, H) for w in words) + 2
        right = tip[0] <= knee[0]  # the reference line runs away from the surface
        end = knee + np.array([(1 if right else -1) * (width + 12), 0])
        msp.add_line(tuple(knee), tuple(end), dxfattribs=attribs)
        apex_x = knee[0] + (3 if right else -(width + 9))
        top = texture_symbol(msp, apex_x, knee[1], h1=5, kind=kind, layer="SURFACES")
        msp.add_line(tuple(top), (top[0] + width, top[1]), dxfattribs=attribs)
        text(msp, words[0], top[0] + 1, top[1] + 0.8, layer="SURFACES")
        if len(words) > 1:
            text(msp, words[1], top[0] + 1, top[1] - 0.8 - H, layer="SURFACES")
    return missing


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
    surfaces = tb.get("surfaces") or {}
    kind = surfaces.get("default", "any")
    others = bool(surfaces.get("marks"))  # "(symbol)": other requirements are shown on the views
    if tb["default_finish"] or kind != "any":
        texture_symbol(msp, r[0] + 3, r[1] + 1.2, kind=kind)
        text(msp, tb["default_finish"], r[0] + 7.5, r[1] + 1.6, align="LEFT",
             width=r[2] - r[0] - (16 if others else 9))
    if others:
        x = r[2] - 7.5
        text(msp, "(", x, r[1] + 1.6, align="LEFT")
        texture_symbol(msp, x + 2.4, r[1] + 1.8, h1=1.6)
        text(msp, ")", x + 5.2, r[1] + 1.6, align="LEFT")
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
    kind = (cfg.get("surfaces") or {}).get("default", "any")
    if kind in ("machined", "as built"):
        notes.append(f"Surfaces not marked otherwise: {kind}" +
                     (f", {cfg['default_finish']}" if cfg.get("default_finish") else "") + ".")
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
    lines = {name: geometry.project(shape, *frames[name]) for name in ("front", "top", "left", "iso")}
    ext = view_extents(env, {k: v for k, v in frames.items() if k != "iso"})
    ext["iso"] = geometry.extents(lines["iso"])
    ortho = ["front", "top", "left"]
    if geometry.same_view(lines["left"], lines["front"], ext["left"], ext["front"]):
        ortho.remove("left")  # e.g. a turned part: the side view repeats the front view

    rows, types, warnings = plan_holes(geometry.holes(shape) + geometry.ports(shape), frames, ortho, cfg)
    needs = aux_needs(rows, ortho, ext)
    for need in needs:
        lines[need["view"]] = geometry.project(shape, *frames[need["view"]])
    shown = ortho + [n["view"] for n in needs]
    sym = {name: geometry.symmetry(lines[name]) for name in shown}
    datums = plan_datums(rows, geometry.flat_faces(shape), cfg, ortho)
    origins, at_centre, table_notes = hole_origins(rows, shown, frames, ext, sym, cfg.get("datum"), needs, datums)
    titles = {v: f"{v.upper()} VIEW" for v in ortho} | {n["view"]: f"VIEW {n['letter']}" for n in needs}
    tables = hole_tables(rows, types, shown, origins, table_notes, titles, datums)
    widths, block = table_size(tables) if tables else (None, None)
    if rows and (cfg.get("datum") or {}).get("confirm"):
        warnings.append("datum.confirm is still true - check datums A/B and the hole table origin")

    notes = general_notes(cfg) + list(cfg["notes"] or [])
    if any(frame for _, _, groups in tables for *_, frame in groups):
        notes.insert(len(general_notes(cfg)), "Hole table X/Y are theoretically exact dimensions (ISO 1101); "
                                              "they are toleranced by the position tolerance of their group.")
    section = plan_section(shape, frames, ext, cfg)
    lay = choose_layout({v: ext[v] for v in ortho}, notes_height(notes), cfg["sheet"], cfg["scale"], block,
                        needs, section=section and section["ext"],
                        slot="left" not in ortho)  # the section may take the place of a repeated side view
    in_slot = "section" in lay["rects"]
    s = lay["scale"]
    doc = new_doc()
    msp = doc.modelspace()
    draw_frame(msp, lay)

    info = {"sheet": lay["sheet"], "scale": s, "front": front, "up": up, "views": ortho,
            "dims": [], "view_rects": dict(lay["rects"]), "warnings": warnings, "extra_views": []}
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

    info["section"] = None
    if section:
        section["letter"] = next(lay["letters"])
        if in_slot:
            section["scale"], section["origin"] = s, np.array(lay["origins"]["section"])
        else:
            e, c = section["ext"], lay["section"]["scale"]
            section["scale"] = c
            section["origin"] = lay["section"]["paper"] - np.array([(e[0] + e[2]) / 2, (e[1] + e[3]) / 2]) * c
        info["view_rects"]["section"] = draw_section(msp, section, lay, frames, circles,
                                                     axis="left" not in ortho and sym["front"][0])
        info["section"] = f"{section['letter']}–{section['letter']}"

    info["view_rects"].update(draw_aux(msp, lay, lines, frames, ext, sym, circles))
    info["extra_views"] = [f"{a['letter']} from the {v}" if v in ("left", "right") else
                           f"{a['letter']} from {'below' if v == 'bottom' else 'behind'}"
                           for v, a in lay["aux"].items()]
    details = plan_details(rows, lay, shown) if rows else []
    for d in details:  # the detail circle itself; its label sits above it
        cx, cy, rr = *d["paper"], d["R"] * d["scale"]
        info["view_rects"][f"detail {d['letter']}"] = (cx - rr, cy - rr, cx + rr, cy + rr)
    if section:
        cutting_plane(msp, lay, section, details)

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

    # where each view sits on the paper: for surface marks and for picking in the app
    def place_of(origin, scale, frame, rect, **extra):
        return {"origin": [float(v) for v in origin], "scale": float(scale),
                "frame": [np.asarray(frame[0], float).tolist(), np.asarray(frame[1], float).tolist()],
                "rect": [float(v) for v in rect], **extra}

    def label(v):  # as the holes list names views
        return v if v in ortho else f"view {lay['aux'][v]['letter']}"

    places = {v: place_of(*view_place(lay, v), frames[v], info["view_rects"][v], label=label(v))
              for v in ortho + list(lay["aux"])}
    if section:
        places["section"] = place_of(section["origin"], section["scale"], frames["left"],
                                     info["view_rects"]["section"], label="section",
                                     cut=[np.cross(frames["front"][1], frames["front"][0]).tolist(),
                                          float(section["at"])])
    for d in details:
        places[f"detail {d['letter']}"] = place_of(d["origin"], d["scale"], frames[d["view"]],
                                                   info["view_rects"][f"detail {d['letter']}"],
                                                   label=label(d["view"]), parent=d["view"])
    info["places"] = places
    for m in surface_marks(msp, (cfg.get("surfaces") or {}).get("marks"), places):
        warnings.append(f"a surface mark on the {m.get('view')} view is left out: that view is not on the drawing")
    full_aux = [v for v, a in lay["aux"].items() if not a["partial"]]
    spots = datum_marks(shape, datums, lay, frames, lines, ortho + full_aux, section) if datums else []
    draw_holes(msp, lay, rows, types, origins, at_centre, tables, widths, details, frames, ortho, spots)

    def where(r):
        return r["view"] if r["view"] in ortho else f"view {lay['aux'][r['view']]['letter']}"

    ordered = sorted(rows, key=lambda r: (r["tag"][0], int(r["tag"][1:])))
    info["holes"] = [{"tag": r["tag"], "type": r["key"], "view": where(r),
                      "x": round(float(r["xy"][0] - origins[r["view"]][0]), 3),
                      "y": round(float(r["xy"][1] - origins[r["view"]][1]), 3),
                      "point": np.round(r["point"], 3).tolist(),
                      "axis": np.round(r["hole"]["axis"], 4).tolist(),
                      "diameter": round(r["hole"]["diameter"], 3), "through": r["hole"]["through"],
                      "depth": None if r["entry"]["depth"] is None else round(r["entry"]["depth"], 3),
                      "pitch": r["hole"]["pitch"], "colours": r["hole"]["colours"]}
                     for r in ordered if r["hole"].get("kind") != "port"]
    info["ports"] = [{"tag": r["tag"], "type": r["key"], "view": where(r), "waveguide": r["hole"]["name"],
                      "size": [round(v, 3) for v in r["hole"]["size"]],
                      "x": round(float(r["xy"][0] - origins[r["view"]][0]), 3),
                      "y": round(float(r["xy"][1] - origins[r["view"]][1]), 3),
                      "point": np.round(r["point"], 3).tolist(), "axis": np.round(r["hole"]["axis"], 4).tolist(),
                      "broad_wall": np.round(r["hole"]["long"], 4).tolist()}
                     for r in ordered if r["hole"].get("kind") == "port"]
    info["datums"] = datum_summary(datums, where) if datums else {}
    info["hole_types"] = types
    info["details"] = [f"{d['letter']} ({fmt_scale(d['scale'])}) on the {d['view']} view" for d in details]

    grams = mass_grams(shape, cfg)
    info["mass_g"] = grams
    title_block(msp, lay, cfg, fmt_mass(grams))
    draw_notes(msp, lay, notes)
    info["problems"] = check(doc, info, lay)
    return doc, info


def draw_holes(msp, lay, rows, types, origins, at_centre, tables, widths, details, frames, ortho, datum_spots=()):
    """ISO 6410 thread arcs, port centre lines, table origins, datum indicators,
    reference arrows, tags and the tables.

    Every hole is annotated in the view (or detail) where its opening is visible.
    """
    if not rows:
        return
    specs = {t["key"]: t["spec"] for t in types}
    spots = {}  # tags of holes drawn on the same spot are combined
    for r in rows:
        d = r.get("detail")
        o, s = (d["origin"], d["scale"]) if d else view_place(lay, r["view"])
        cx, cy = to_paper(r["xy"], o, s)
        rp = r["hole"]["diameter"] / 2 * s
        spec = specs[r["key"]]
        if r["hole"].get("kind") == "port":  # centre lines across the opening
            h = r["hole"]
            a, b = (v / 2 * s + 2 for v in h["size"])
            hx, hy = (a, b) if abs(h["long"] @ r["x_dir"]) > 0.5 else (b, a)
            attribs = {"layer": "CENTER", "ltscale": 0.25}
            msp.add_line((cx - hx, cy), (cx + hx, cy), dxfattribs=attribs)
            msp.add_line((cx, cy - hy), (cx, cy + hy), dxfattribs=attribs)
            rp = max(hx, hy) - 2
        if spec.get("thread") and not spec.get("confirm"):
            major = holes.thread_major(str(spec["thread"]))
            if major and major > r["hole"]["diameter"]:  # thin 3/4 circle, open at the top right
                msp.add_arc((cx, cy), major / 2 * s, 100, 350, dxfattribs={"layer": "VISIBLE", "lineweight": 25})
        key = (r["view"], round(cx, 1), round(cy, 1))
        spots.setdefault(key, [[], cx, cy, rp])[0].append(r["tag"])
        spots[key][3] = max(spots[key][3], rp)
    for (view, _), (c, radius, _) in bolt_circles(rows).items():  # pitch circles, thin chain line
        o, s = view_place(lay, view)
        msp.add_circle(tuple(to_paper(c, o, s)), radius * s, dxfattribs={"layer": "CENTER", "ltscale": 0.5})
    for view in origins:
        if not at_centre[view]:
            inside = [d for d in details if d["view"] == view
                      and np.hypot(*(origins[view] - d["centre"])) < d["R"]]
            o, s = (inside[0]["origin"], inside[0]["scale"]) if inside else view_place(lay, view)
            mine = [(np.hypot(*(r["xy"] - origins[view])) * s, r["hole"]["diameter"] / 2 * s)
                    for r in rows if r["view"] == view]
            clear = min((d - rp - 4.5 for d, rp in mine if d > 1e-6), default=7)  # arrow plus its letter
            size = max(np.clip(clear, 3, 7), max((rp + 3 for d, rp in mine if d <= 1e-6), default=0))
            origin_symbol(msp, *to_paper(origins[view], o, s), size=float(size))
    occ = Occupancy(lay)
    for e in msp:
        occ.entity(e)
    occ.box(lay["title_block"])
    occ.box(lay["table"])
    place_datums(msp, occ, datum_spots)
    arrows = [(a["letter"], frames[view][0], [r["point"] for r in rows if r["view"] == view])
              for view, a in lay["aux"].items()]
    place_arrows(msp, occ, lay, frames, ortho, arrows)
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
            if layer not in ("HOLES", "SURFACES") and _overlap(a, r):  # annotations belong on their view
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
