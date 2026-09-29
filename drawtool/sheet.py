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

SHEETS = {"A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594),
          "A0": (1189, 841)}  # ISO 216, landscape, smallest first
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
    "CALLOUTS": ("Continuous", 25),  # notes, hole callouts, dimensions and balloons added in the app
}
LOOKS = {"right": (1, 0), "left": (-1, 0), "up": (0, 1), "down": (0, -1)}  # section arrows on the paper
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


def place(ext, sheet, s, notes_h, align="centre"):
    """Place front/top/left (first angle) at scale s, or None if they do not fit.

    align "centre" centres the group across the sheet when it fits so; "left"
    puts it at the left edge, leaving one large free area on the right.
    """
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
    spots = [(inner[0] + (free_w - group_w) / 2, inner[3])] if align == "centre" else []
    for gx, gtop in spots + [(inner[0], inner[3])]:
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
                    "aux": {}, "sections": {}}
    return None


AUTO_SHEETS = ("A4", "A3", "A2")  # "auto" chooses among these; A1 and A0 on request


def choose_layout(ext, notes_h, sheet="auto", scale="auto", block=None, needs=(), sections=(), slot=False):
    """Sheet and scale, and room for everything around the main views.

    auto: A4 if the part fits at 1:1 or larger, otherwise the best scale on A3;
    A2 only when A3 has no room for the section views at the drawing scale.
    A chosen sheet adapts to its space instead: the largest scale that fits,
    hole tables side by side when one column is too tall, sections reduced,
    and the automatic section left out (lay["section_dropped"]) only when the
    main views would otherwise shrink more than one ISO scale step. Sections
    added in the app are never left out.

    `block` is the hole tables' {"width", "heights"}, `needs` the extra views for
    holes and `sections` the section views ({"key", "ext", "auto", "slot"}, the
    automatic one first). With `slot` the first section marked "slot" (seen from
    the left) first tries the side view's place, right of the front view.
    """
    if sheet != "auto" and sheet not in SHEETS:
        raise ValueError(f"unknown sheet size '{sheet}': use auto, {', '.join(SHEETS)}")
    sheets = list(AUTO_SHEETS) if sheet == "auto" else [sheet]
    scales = SCALES if scale == "auto" else [parse_scale(scale)]
    auto = bool(sections) and sections[0]["auto"]

    def attempt(name, s, keep_auto):
        wanted = [x for x in sections if keep_auto or not x["auto"]]
        first = next((x for x in wanted if x["slot"]), None) if slot else None
        for in_slot in ([first] if first else []) + [None]:
            views = {**ext, "section": in_slot["ext"]} if in_slot else ext
            for align in ("centre", "left"):
                for columns in (1, 2, 3) if block else (1,):
                    lay = place(views, name, s, notes_h, align)
                    if lay and (not block or _reserve(lay, block, columns)) and \
                            all(_reserve_section(lay, x["key"], _size(x["ext"])) for x in wanted if x is not in_slot) \
                            and _reserve_aux(lay, needs):
                        lay["section_dropped"] = auto and not keep_auto
                        lay["slot"] = in_slot["key"] if in_slot else None
                        return lay
        return None

    def best(name):
        natural = [s for s in scales if place(ext, name, s, notes_h)]  # the main views alone
        floor = natural[min(1, len(natural) - 1)] if natural and auto else 0
        for s in scales:
            if s >= floor and (lay := attempt(name, s, True)):
                return lay
        if auto:  # no room for the automatic section: leave it out rather than shrink everything further
            return next((lay for s in scales if (lay := attempt(name, s, False))), None)
        return None

    fallback = None
    for name in sheets:
        lay = best(name)
        if lay is None:
            continue
        full = not lay["section_dropped"] and all(v["scale"] == lay["scale"] for v in lay["sections"].values())
        if scale != "auto" or sheet != "auto" or (full and (name != "A4" or lay["scale"] >= 1)):
            return lay
        if name != "A4":
            fallback = fallback or lay  # section reduced: a larger sheet may hold it at full scale
    if fallback:
        return fallback
    where = sheet if sheet != "auto" else AUTO_SHEETS[-1]
    raise ValueError(f"the views, sections, hole tables and extra views do not fit on {where}"
                     + (f" at {scale}; choose a smaller scale or a larger sheet" if scale != "auto"
                        else " at any scale; choose a larger sheet"))


def _table_height(groups):
    return ROW * (3 + sum(max(len(r), len(ls)) for ls, r, _ in groups))  # title, header, rows, note


def split_tables(tables, n):
    """The tables, in order, in up to n columns of about equal height. A table longer
    than its share continues in the next column, between two groups of equal holes."""
    target = (sum(_table_height(g) for _, _, g in tables) + PAD * (len(tables) - 1)) / n
    cols, used = [[]], 0.0
    for title, note, groups in tables:
        rest, cont = list(groups), False
        while rest:
            last = len(cols) == n
            gap = PAD if cols[-1] else 0.0
            take = len(rest)
            if not last:
                take = 0
                while take < len(rest) and used + gap + _table_height(rest[:take + 1]) <= target + 1e-6:
                    take += 1
                if take == 0 and cols[-1]:
                    cols.append([])
                    used = 0.0
                    continue
                take = max(take, 1)
            piece, rest = rest[:take], rest[take:]
            cols[-1].append((title + (" (cont.)" if cont else ""), "" if rest else note, piece))
            used += gap + _table_height(piece)
            cont = True
            if rest and not last:
                cols.append([])
                used = 0.0
    return [c for c in cols if c]


def _reserve(lay, block, columns=1):
    """Put the hole tables (in `columns` side by side) in the top-right corner of the largest free area."""
    cols = split_tables(block["tables"], columns)
    size = (len(cols) * block["width"] + (len(cols) - 1) * PAD,
            max(sum(_table_height(g) for _, _, g in c) + PAD * (len(c) - 1) for c in cols))
    r, fit = free_rect(lay, size, label=0)
    if not r or fit < 1:
        return False
    x1, y1 = r[2] - PAD, r[3] - PAD
    lay["table"] = (x1 - size[0], y1 - size[1], x1, y1)
    lay["table_columns"] = cols
    lay["zones"].append(lay["table"])
    return True


def _reserve_section(lay, key, size):
    """Room for a section view: the drawing scale if it fits, else down to a fifth of it."""
    s = lay["scale"]
    for c in (c for c in sorted(SCALES, reverse=True) if s / 5 <= c <= s):
        w, h = size[0] * c + 2, size[1] * c + 2
        free, fit = free_rect(lay, (w, h + 10), label=0)
        if free and fit >= 1:
            x0, y1 = free[0] + PAD, free[3] - PAD
            box = (x0, y1 - h - 10, x0 + w, y1)  # view plus its label
            lay["zones"].append(box)
            lay["sections"][key] = {"scale": c, "paper": np.array([x0 + w / 2, y1 - 10 - h / 2]), "box": box}
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
                                        "paper": np.array([cx, cy]), "box": box,
                                        "origin": np.array([cx, cy]) - need["centre"] * c}
            break
        else:
            return False
    return True


def _shifted(lay, box, centre, at):
    """`box` moved so that `centre` lands on `at`, kept inside the frame; its keep-out
    zone moves with it. Returns the new box and the shift."""
    x0, y0, x1, y1 = lay["area"]
    delta = np.asarray(at, float) - np.asarray(centre, float)
    delta[0] = np.clip(delta[0], x0 + PAD - box[0], max(x0 + PAD - box[0], x1 - PAD - box[2]))
    delta[1] = np.clip(delta[1], y0 + PAD - box[1], max(y0 + PAD - box[1], y1 - PAD - box[3]))
    new = (box[0] + delta[0], box[1] + delta[1], box[2] + delta[0], box[3] + delta[1])
    if box in lay["zones"]:
        lay["zones"][lay["zones"].index(box)] = new
    return new, delta


def _xy(value):
    """A 2D point or offset from the YAML, or None."""
    return np.array(value, float) if isinstance(value, (list, tuple)) and len(value) == 2 else None


def move_blocks(lay, moves, sections):
    """Sections, extra views and the hole tables where they were moved in the app: each
    keeps its size and scale, its centre goes to the chosen point (inside the frame)."""
    for sec in sections:
        at = _xy(moves.get(sec["key"]))
        if at is None:
            continue
        if lay["slot"] == sec["key"]:  # out of the side view's place, which stays free
            r = lay["rects"].pop("section")
            lay["origins"].pop("section")
            lay["slot"] = None
            box = (r[0], r[1], r[2], r[3] + 10)
            lay["zones"].append(box)
            lay["sections"][sec["key"]] = {"scale": lay["scale"], "box": box,
                                           "paper": np.array([(r[0] + r[2]) / 2, (r[1] + r[3]) / 2])}
        m = lay["sections"][sec["key"]]
        m["box"], shift = _shifted(lay, m["box"], m["paper"], at)
        m["paper"] = m["paper"] + shift
    for view, a in lay["aux"].items():
        at = _xy(moves.get(view))
        if at is not None:
            a["box"], shift = _shifted(lay, a["box"], a["paper"], at)
            a["paper"], a["origin"] = a["paper"] + shift, a["origin"] + shift
    at = _xy(moves.get("hole table"))
    if at is not None and "table" in lay:
        t = lay["table"]
        lay["table"], _ = _shifted(lay, t, ((t[0] + t[2]) / 2, (t[1] + t[3]) / 2), at)


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
    for letter, (a, b) in zip("ABCDEFGHJKLMNPQRSTUVWXYZ", zip(ys[:-1], ys[1:])):  # no I or O
        for x in (x0 - 5, x1 + 5):
            text(msp, letter, x, (a + b) / 2, align="MIDDLE_CENTER", layer="FRAME")


def _fields(centre, lo, hi, size=50):
    """Field boundaries every `size` mm from the centring mark, clipped to the frame;
    a sliver (under 15 mm, as on A1 and A0) at an edge joins its neighbour."""
    edge = 0.3 * size
    inner = [centre + k * size for k in range(-12, 13) if lo + edge <= centre + k * size <= hi - edge]
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
    Returns the marked centres in view coordinates (the app snaps to them).
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
    return list(marks)


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


def aux_needs(rows, ortho, ext, letters=DETAIL_LETTERS):
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
        need = {"view": view, "letter": letters[len(needs)], "gap": max(gap, 1e-3)}
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


def _wrap(description, width, h=TAG_H):
    """Split a hole description at its commas into lines no wider than `width`."""
    lines = []
    for part in description.split(", "):
        if lines and text_width(f"{lines[-1]}, {part}", h) <= width:
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
    return widths, {"width": sum(widths), "tables": tables}


POSITION = "POSITION"


def draw_tables(msp, box, columns, widths):
    """Hole tables, in columns side by side (from split_tables); the SIZE (and POSITION)
    cell of a group of equal holes spans all its rows."""
    for k, col in enumerate(columns):
        _draw_table_column(msp, box[0] + k * (sum(widths) + PAD), box[3], col, widths)


def _draw_table_column(msp, x0, top, tables, widths):
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
        if note:
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
    """Extra views for holes (ISO 128-3 reference-arrow views): whole or partial, with letter.
    Returns their paper rectangles and centre marks."""
    rects, centres = {}, {}
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
            centres[view] = centre_marks(msp, inside, frames[view], shown, e, o, c, sym[view], pixels=pixels)
        label = a["letter"] if c == lay["scale"] else f"{a['letter']} ({fmt_scale(c)})"
        text(msp, label, a["paper"][0], rects[view][3] + 3, h=5, align="BOTTOM_CENTER")
    return rects, centres


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
    """Circle and letter on the source view; the enlarged, clipped view with its label.
    Returns the centre marks per detail."""
    centres = {}
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
            centres[f"detail {d['letter']}"] = centre_marks(msp, _circles_in(circles, frames[view], c, R),
                                                            frames[view], clipped, ext[view], o, sc, sym[view],
                                                            pixels=150)
    return centres


def section_letters(entries):
    """The sections added in the app, each with its letter (one is given where the YAML has
    none), and the letters left for the extra views, the automatic section and details."""
    sections = [dict(e) for e in entries or [] if isinstance(e, dict)]
    taken = {str(e["letter"]) for e in sections if e.get("letter")}
    for e in sections:
        if not e.get("letter"):
            e["letter"] = next(letter for letter in reversed(DETAIL_LETTERS) if letter not in taken)
            taken.add(e["letter"])
    return sections, [letter for letter in DETAIL_LETTERS if letter not in taken]


def _section(shape, frames, view, q, look):
    """A section whose cutting plane passes through point q of `view`, square to that view and
    across the arrows, which point `look` on the paper: the direction the section is seen in."""
    d, y = frames[view]
    u = LOOKS[look]
    k = 0 if u[0] else 1
    n = u[0] * np.cross(y, d) + u[1] * y
    # seen along n: from the left or right as a side view, from above or below as a top view
    frame = (-n, y) if k == 0 else (-n, (n @ y) * d)
    lines, loops = geometry.section_view(shape, frame, n, q[k] * u[k])
    return {"view": view, "look": u, "pos": float(q[k]), "frame": frame, "normal": n, "offset": float(q[k] * u[k]),
            "lines": lines, "loops": loops, "ext": geometry.extents(lines) if lines else None,
            "slot": view == "front" and look == "right"}  # seen from the left: may take the side view's place


def plan_sections(shape, frames, ext, cfg, entries, views, warnings):
    """Section views (ISO 128-44): the automatic one first, through the middle of the front
    view and seen from the left, then those added in the app (`entries`), each cut on one
    of `views`.

    The automatic one is only kept when it shows something the outside views do
    not: more than one cut face, or a cut face with holes in it.
    """
    out = []
    if str(cfg.get("section", "auto")).lower() not in ("none", "false", "off"):
        sec = _section(shape, frames, "front", ((ext["front"][0] + ext["front"][2]) / 2, 0.0), "right")
        loops = sec["loops"]
        if loops and not (len(loops) == 1 and len(loops[0]) == 1):
            out.append({**sec, "key": "section", "auto": True})
    for e in entries:
        name = f"{e['letter']}–{e['letter']}"
        point, look = _pick(e.get("point")), str(e.get("look", "right"))
        if e.get("view") not in views or point is None or look not in LOOKS:
            warnings.append(f"section {name} is left out: its view ({e.get('view')}) is not on the drawing")
            continue
        d, y = frames[e["view"]]
        sec = _section(shape, frames, e["view"], (point @ np.cross(y, d), point @ y), look)
        if not sec["loops"]:
            warnings.append(f"section {name} is left out: its cutting plane misses the part")
            continue
        out.append({**sec, "key": f"section {e['letter']}", "letter": e["letter"], "auto": False})
    return out


def _area(loop):
    x, y = np.asarray(loop).T
    return 0.5 * (x @ np.roll(y, -1) - y @ np.roll(x, -1))


def draw_section(msp, sec, lay, circles, axis=False):
    """The section view: outline, 45° hatching of the cut faces (ISO 128-50), centre lines, label.
    Returns its paper rectangle and centre marks.

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
    centres = centre_marks(msp, circles, sec["frame"], sec["lines"], e, o, c, sym)
    r = (*to_paper(e[:2], o, c), *to_paper(e[2:], o, c))
    label = f"{sec['letter']}–{sec['letter']}"
    text(msp, label if c == lay["scale"] else f"{label} ({fmt_scale(c)})", (r[0] + r[2]) / 2, r[3] + 3, h=5,
         align="BOTTOM_CENTER")
    return r, centres


def cutting_plane(msp, lay, sec, details, rects, ext, sym):
    """ISO 128-44 cutting plane on the view it cuts: thick ends beyond the outline (and
    beyond detail circles on the line, where no other view is in the way), arrows in
    the viewing direction and the section letter. Off the view's centre lines the thin
    chain line runs across the view as well, so the plane's position reads clearly."""
    view = sec["view"]
    o, s = view_place(lay, view)
    r = rects[view]
    u = np.array(sec["look"], float)
    k = 0 if u[0] else 1  # the arrows run along paper x (k = 0) or y
    j = 1 - k  # ... and the cutting line along the other axis
    pos = sec["pos"] * s + o[k]
    ends = [r[j], r[j + 2]]
    others = [b for name, b in rects.items() if name != view]

    def clear(e, sign):  # no other view between the outline and GAP beyond an end at e
        strip = [0.0] * 4
        strip[k], strip[k + 2] = pos - 0.5, pos + 0.5
        strip[j], strip[j + 2] = (r[j + 2], e + GAP) if sign > 0 else (e - GAP, r[j])
        return not any(_overlap(strip, b) for b in others)

    for d in details:
        if d["view"] == view:
            c, rr = to_paper(d["centre"], o, s), d["R"] * s
            if abs(c[k] - pos) < rr:
                half = np.sqrt(rr * rr - (c[k] - pos) ** 2)
                if c[j] + half > ends[1] and clear(c[j] + half, 1):
                    ends[1] = c[j] + half
                if c[j] - half < ends[0] and clear(c[j] - half, -1):
                    ends[0] = c[j] - half

    def at(along, sign=0.0, offset=0.0):  # paper point on the cutting line
        p = np.zeros(2)
        p[k], p[j] = pos, along + sign * offset
        return p

    e = ext[view]
    middle = (e[k] + e[k + 2]) / 2
    if not (sym[view][k] and abs(sec["pos"] - middle) < 1e-3 * max(_size(e))):
        msp.add_line(tuple(at(ends[0], -1, 1.0)), tuple(at(ends[1], 1, 1.0)),
                     dxfattribs={"layer": "CENTER", "ltscale": 0.5})
    for along, sign in ((ends[1], 1), (ends[0], -1)):
        a, b = at(along, sign, 1.0), at(along, sign, 6.0)
        msp.add_line(tuple(a), tuple(b), dxfattribs={"layer": "CENTER", "linetype": "Continuous", "lineweight": 50})
        tip = b + 6 * u
        side = np.array([-u[1], u[0]])
        msp.add_line(tuple(b), tuple(tip - 2.4 * u), dxfattribs={"layer": "DIMS"})
        msp.add_solid([tuple(tip), tuple(tip - 2.5 * u + 0.8 * side), tuple(tip - 2.5 * u - 0.8 * side)],
                      dxfattribs={"layer": "DIMS"})
        if k == 0:  # the letter beyond the arrow head
            text(msp, sec["letter"], tip[0] + 1.5 * u[0], tip[1] - 2.5 + sign * 0.5, h=5,
                 align="LEFT" if u[0] > 0 else "RIGHT")
        else:
            text(msp, sec["letter"], tip[0], tip[1] + 1.5 * u[1], h=5,
                 align="BOTTOM_CENTER" if u[1] > 0 else "TOP_CENTER")


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


def datum_marks(shape, datums, lay, frames, lines, views, sections):
    """(letter, [(paper point on the feature, outward unit direction)], opening) per datum.

    A goes on a view that shows its face edge-on where nothing stands in front
    of it; B and C on the outline of their (first) opening, in the view or
    detail where it is tagged. `opening` is (paper centre, half width, half
    height, round) for B and C, None for A.
    """
    places = {v: (frames[v], lines[v], *view_place(lay, v), None) for v in views}
    for sec in sections:  # material in front of the cutting plane is gone in a section
        places[sec["key"]] = (sec["frame"], sec["lines"], sec["origin"], sec["scale"], (sec["normal"], sec["offset"]))
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
    marks = [("A", spots, None)]
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
                               (c + (hx, 0), np.array([1.0, 0.0])), (c - (hx, 0), np.array([-1.0, 0.0]))],
                      (c, hx, hy, h.get("kind") != "port")))
    return marks


def datum_moves(marks, moves, places, face):
    """{letter: (paper point on the feature, outward direction, leader length)} for the datum
    indicators moved in the app. A: its triangle on the face where it was picked, the letter
    square to the face towards the chosen point; B and C: the letter at the chosen offset from
    the opening's centre, the triangle on the outline in line with it (ISO 5459)."""
    fixed = {}
    for letter, _, opening in marks:
        m = moves.get(f"datum {letter}")
        if letter == "A":
            if not isinstance(m, dict) or m.get("view") not in places or _pick(m.get("point")) is None \
                    or _xy(m.get("at")) is None:
                continue
            place = places[m["view"]]
            d, y = (np.asarray(v, float) for v in place["frame"])
            n = np.array([face["normal"] @ np.cross(y, d), face["normal"] @ y])
            if np.abs(n).max() < 0.999:
                continue  # that view does not show the face edge-on
            p, reach = on_paper(place, m["point"]), float(_xy(m["at"]) @ n)
        else:
            at = _xy(m)
            if at is None or opening is None or np.hypot(*at) < 1e-6:
                continue
            c, hx, hy, round_ = opening
            if round_:
                n, half = at / np.hypot(*at), hx
            else:  # a port: the side facing the chosen point
                k = int(np.argmax(np.abs(at)))
                n, half = np.eye(2)[k] * np.sign(at[k]), (hx, hy)[k]
            p, reach = c + half * n, float(at @ n) - half
        fixed[letter] = (p, n, max(reach - 2.6 - DATUM_BOX / 2, 2.0))  # the frame's centre at the chosen reach
    return fixed


def _datum_frame(p, n, length):
    """Apex of the datum triangle on p, end of its leader and the letter's frame."""
    apex = p + 2.6 * n
    end = apex + length * n
    c = end + n * DATUM_BOX / 2
    return apex, end, (c[0] - DATUM_BOX / 2, c[1] - DATUM_BOX / 2, c[0] + DATUM_BOX / 2, c[1] + DATUM_BOX / 2)


def place_datums(msp, occ, marks, fixed=None):
    """ISO 5459 datum feature indicators: filled triangle on the feature, leader, framed letter,
    each where its frame lands in the freest space, or as moved in the app (`fixed`, from
    datum_moves). Returns {"datum A": (frame box, opening centre or None), ...}."""
    placed = {}
    for letter, spots, opening in marks:
        best = None
        if letter in (fixed or {}):
            p, n, length = fixed[letter]
            best = (0, p, n, *_datum_frame(p, n, length))
        for k, (p, n) in enumerate(spots if best is None else []):
            for length in (2.0, 4.0, 7.0, 11.0):
                apex, end, box = _datum_frame(p, n, length)
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
        placed[f"datum {letter}"] = (box, opening[0] if opening else None)
    return placed


def place_tags(msp, occ, marks, moves=None):
    """Tag text next to each hole in the freest spot; farther out with a leader if crowded,
    or where it was moved in the app (`moves`: "tag A1" = offset of the tag's centre from the
    hole's). Returns {"tag A1": (box, hole centre), ...}, keyed by the spot's first tag."""
    placed = {}
    for tag, cx, cy, rp in marks:
        key = "tag " + tag.split(", ")[0]
        w, h = text_width(tag, TAG_H), TAG_H * 1.29
        best, at = None, _xy((moves or {}).get(key))
        if at is not None:
            box = (cx + at[0] - w / 2, cy + at[1] - h / 2, cx + at[0] + w / 2, cy + at[1] + h / 2)
            gap = np.hypot(np.clip(cx, box[0], box[2]) - cx, np.clip(cy, box[1], box[3]) - cy) - rp
            best = (0, box, int(gap > 1.0), at / max(np.hypot(*at), 1e-9))  # a leader unless next to the hole
        for ring, gap in enumerate((0.8, 4.0, 8.0) if best is None else ()):
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
        placed[key] = (box, (cx, cy))
    return placed


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


def on_paper(place, point):
    """Paper position of a model point in a view (a `places` entry)."""
    d, y = (np.asarray(v, float) for v in place["frame"])
    p = np.asarray(point, float)
    return np.asarray(place["origin"], float) + np.array([p @ np.cross(y, d), p @ y]) * place["scale"]


def leader(msp, tip, knee, edge, layer):
    """ISO 128-22 leader line from `knee` to `tip`: an arrowhead on an outline (`edge`),
    a dot inside one."""
    tip, knee = np.asarray(tip, float), np.asarray(knee, float)
    u = (tip - knee) / max(np.linalg.norm(tip - knee), 1e-9)
    attribs = {"layer": layer}
    if edge:
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
        tip = on_paper(place, m["point"])
        knee = tip + np.array(m["at"], float)
        attribs = {"layer": "SURFACES"}
        leader(msp, tip, knee, m.get("edge"), "SURFACES")
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


# ---------------------------------------------------------------- callouts added in the app

def reference_text(msp, knee, tip, lines, layer="CALLOUTS"):
    """Text lines above a reference line that starts at `knee` and runs away from the
    leader's tip (ISO 128-22). Returns the box of line and text."""
    width = max(text_width(t, H) for t in lines) + 2
    x0 = knee[0] if tip[0] <= knee[0] else knee[0] - width
    msp.add_line((x0, knee[1]), (x0 + width, knee[1]), dxfattribs={"layer": layer})
    for i, t in enumerate(reversed(lines)):
        text(msp, t, x0 + 1, knee[1] + 0.8 + i * NOTE_PITCH, layer=layer)
    return x0, knee[1], x0 + width, knee[1] + 0.8 + (len(lines) - 1) * NOTE_PITCH + H


BALLOON_R = 4.0
CALLOUT_TYPES = ("note", "hole", "dimension", "balloon")


def balloon(msp, tip, centre, edge, number):
    """A numbered circle at `centre` on a leader to `tip`. Returns its box."""
    r = max(BALLOON_R, text_width(number, H) / 2 + 1.5)
    u = (tip - centre) / max(np.linalg.norm(tip - centre), 1e-9)
    leader(msp, tip, centre + r * u, edge, "CALLOUTS")
    msp.add_circle(tuple(centre), r, dxfattribs={"layer": "CALLOUTS"})
    text(msp, number, *centre, align="MIDDLE_CENTER", layer="CALLOUTS")
    return centre[0] - r, centre[1] - r, centre[0] + r, centre[1] + r


def balloon_notes(callouts, notes, warnings):
    """Balloons refer to numbered notes: a balloon's text becomes a note after the others
    (balloons with the same text share it); a balloon whose text is a number refers to that
    note. Appends to `notes`; returns {callout index: note number}."""
    numbers = {}
    for i, c in enumerate(callouts):
        t = str(c.get("text") or "").strip() if isinstance(c, dict) and c.get("type") == "balloon" else ""
        if t and not t.isdigit():
            if t not in notes:
                notes.append(t)
            numbers[i] = notes.index(t) + 1
    for i, c in enumerate(callouts):
        t = str(c.get("text") or "").strip() if isinstance(c, dict) and c.get("type") == "balloon" else ""
        if t.isdigit():
            if 1 <= int(t) <= len(notes):
                numbers[i] = int(t)
            else:
                warnings.append(f"callout {i + 1}: there is no note {t} for its balloon; left out")
    return numbers


def user_dimension(msp, a, b, base, direction, scale, label):
    """A dimension between paper points a and b, its line through `base`: horizontal,
    vertical or aligned; `label` "<>" is the measured value."""
    override, attribs = {"dimlfac": 1 / scale}, {"layer": "CALLOUTS"}
    if direction == "aligned":
        v = b - a
        n = np.array([-v[1], v[0]]) / np.linalg.norm(v)
        dim = msp.add_aligned_dim(p1=tuple(a), p2=tuple(b), distance=float((base - a) @ n), text=label,
                                  dimstyle="ISO", override=override, dxfattribs=attribs)
    else:
        dim = msp.add_linear_dim(base=tuple(base), p1=tuple(a), p2=tuple(b),
                                 angle=0 if direction == "horizontal" else 90, text=label, dimstyle="ISO",
                                 override=override, dxfattribs=attribs)
    dim.render()
    return dim.dimension


def draw_callouts(msp, callouts, places, rows, types, numbers, warnings):
    """Leader notes, hole callouts, dimensions and balloons added in the app (layer CALLOUTS).

    Each sits in its view (`places`): a leader from a model point on the part (an
    arrow on an outline, a dot inside it), its text at an offset on the paper. A
    hole callout reads like its hole table row unless given a text. Returns
    {"callout i": {"box", "anchor", "label"}} for moving them in the app.
    """
    items = {}
    specs = {t["key"]: t["spec"] for t in types}
    for i, c in enumerate(callouts):
        c = c if isinstance(c, dict) else {}
        kind, place, at = c.get("type"), places.get(c.get("view")), _xy(c.get("at"))
        problem = ("its type is not note, hole, dimension or balloon" if kind not in CALLOUT_TYPES
                   else f"its view ({c.get('view')}) is not on the drawing" if place is None
                   else "it has no position (at)" if at is None else None)
        if problem:
            warnings.append(f"callout {i + 1} ({kind}) is left out: {problem}")
            continue
        own = [t for t in str(c.get("text") or "").splitlines() if t.strip()]
        if kind == "dimension":
            pts = [_pick(p) for p in c.get("points") or []]
            a, b = (on_paper(place, p) for p in pts) if len(pts) == 2 and all(p is not None for p in pts) else (0, 0)
            if np.hypot(*(np.asarray(b) - a)) < 1e-6:
                warnings.append(f"callout {i + 1} (dimension) is left out: it needs two different points")
                continue
            direction = c.get("direction") if c.get("direction") in ("vertical", "aligned") else "horizontal"
            dim = user_dimension(msp, a, b, a + at, direction, place["scale"], " ".join(own) or "<>")
            label = [v for v in dim.virtual_entities() if v.dxftype() in ("MTEXT", "TEXT")]
            box = ezdxf.bbox.extents(label)
            items[f"callout {i}"] = {"box": (box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y), "anchor": a,
                                     "label": f"dimension {round(dim.get_measurement() / place['scale'], 3):g}"}
            continue
        point = _pick(c.get("point"))
        if point is None:
            warnings.append(f"callout {i + 1} ({kind}) is left out: it has no point")
            continue
        if kind == "hole":
            row = min(rows, key=lambda r: np.linalg.norm(r["point"] - point), default=None)
            if row is None or np.linalg.norm(row["point"] - point) > 0.05:
                warnings.append(f"callout {i + 1} (hole) is left out: no hole or port at {point.tolist()}")
                continue
            h, e = row["hole"], row["entry"]
            centre, s = on_paper(place, row["point"]), place["scale"]
            knee = centre + at
            u = at / max(np.hypot(*at), 1e-9)
            if h.get("kind") == "port":  # the leader ends on the rectangle
                d, y = (np.asarray(v, float) for v in place["frame"])
                half = np.array(h["size"]) / 2 * s
                hx, hy = half if abs(h["long"] @ np.cross(y, d)) > 0.5 else half[::-1]
                reach = min(hx / max(abs(u[0]), 1e-9), hy / max(abs(u[1]), 1e-9))
            else:  # ... on the outermost circle
                reach = max(h["diameter"], (e["cbore"] or [0])[0], (e["csk"] or [0])[0]) / 2 * s
            if not own:
                count = sum(r["view"] == row["view"] and r["key"] == row["key"] for r in rows)
                size = holes.size_text(h, e, specs[row["key"]], row["x_dir"])
                own = _wrap(f"{count}× {size}" if count > 1 else size, 70, H)
            leader(msp, centre + reach * u, knee, True, "CALLOUTS")
            box = reference_text(msp, knee, centre, own)
            items[f"callout {i}"] = {"box": box, "anchor": centre, "label": f"{row['tag']}: {' '.join(own)}"}
            continue
        tip = on_paper(place, point)
        knee = tip + at
        if kind == "note":
            if not own:
                warnings.append(f"callout {i + 1} (note) is left out: it has no text")
                continue
            leader(msp, tip, knee, c.get("edge", True), "CALLOUTS")
            box = reference_text(msp, knee, tip, own)
            items[f"callout {i}"] = {"box": box, "anchor": tip, "label": "note: " + " ".join(own)}
        elif i in numbers:
            box = balloon(msp, tip, knee, c.get("edge", True), str(numbers[i]))
            items[f"callout {i}"] = {"box": box, "anchor": tip, "label": f"balloon {numbers[i]}"}
        elif not own:
            warnings.append(f"callout {i + 1} (balloon) is left out: it has no text")
    return items


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

    entries, letters = section_letters(cfg.get("sections"))  # sections added in the app keep their letters
    rows, types, warnings = plan_holes(geometry.holes(shape) + geometry.ports(shape), frames, ortho, cfg)
    needs = aux_needs(rows, ortho, ext, letters)
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
    callouts = cfg.get("callouts") or []
    numbers = balloon_notes(callouts, notes, warnings)
    sections = plan_sections(shape, frames, ext, cfg, entries,
                             ortho + [n["view"] for n in needs if not n["partial"]], warnings)
    lay = choose_layout({v: ext[v] for v in ortho}, notes_height(notes), cfg["sheet"], cfg["scale"], block,
                        needs, sections=sections,
                        slot="left" not in ortho)  # a section may take the place of a repeated side view
    if lay["section_dropped"]:
        sections = sections[1:]
        warnings.append(f"no room for the section view on {lay['sheet']}: left out (a larger sheet shows it)")
    lay["letters"] = iter(letters[len(needs):])
    moves = cfg.get("moves") or {}
    move_blocks(lay, moves, sections)
    s = lay["scale"]
    doc = new_doc()
    msp = doc.modelspace()
    draw_frame(msp, lay)

    info = {"sheet": lay["sheet"], "scale": s, "front": front, "up": up, "views": ortho,
            "dims": [], "view_rects": dict(lay["rects"]), "warnings": warnings, "extra_views": []}
    circles = geometry.circles(shape)
    centres = {}  # centre marks per view, for snapping in the app
    for name in ortho:
        o = np.array(lay["origins"][name])
        draw_view(msp, lines[name], o, s)
        draw_centre_lines(msp, ext[name], o, s, sym[name])
        centres[name] = centre_marks(msp, circles, frames[name], lines[name], ext[name], o, s, sym[name])

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

    for sec in sections:
        if sec["auto"]:
            sec["letter"] = next(lay["letters"])
        if lay["slot"] == sec["key"]:
            sec["scale"], sec["origin"] = s, np.array(lay["origins"]["section"])
        else:
            e, c = sec["ext"], lay["sections"][sec["key"]]["scale"]
            sec["scale"] = c
            sec["origin"] = lay["sections"][sec["key"]]["paper"] - np.array([(e[0] + e[2]) / 2, (e[1] + e[3]) / 2]) * c
        info["view_rects"][sec["key"]], centres[sec["key"]] = draw_section(
            msp, sec, lay, circles, axis=sec["auto"] and "left" not in ortho and sym["front"][0])
    info["section"] = next((f"{x['letter']}–{x['letter']}" for x in sections if x["auto"]), None)
    info["sections"] = [{"key": x["key"], "label": f"{x['letter']}–{x['letter']}", "view": x["view"],
                         "auto": x["auto"]} for x in sections]

    rects, aux_centres = draw_aux(msp, lay, lines, frames, ext, sym, circles)
    info["view_rects"].update(rects)
    centres.update(aux_centres)
    info["extra_views"] = [f"{a['letter']} from the {v}" if v in ("left", "right") else
                           f"{a['letter']} from {'below' if v == 'bottom' else 'behind'}"
                           for v, a in lay["aux"].items()]
    details = plan_details(rows, lay, shown) if rows else []
    for d in details:  # the detail circle itself; its label sits above it
        at = _xy(moves.get(f"detail {d['letter']}"))
        if at is not None:
            d["box"], shift = _shifted(lay, d["box"], d["paper"], at)
            d["paper"], d["origin"] = d["paper"] + shift, d["origin"] + shift
        cx, cy, rr = *d["paper"], d["R"] * d["scale"]
        info["view_rects"][f"detail {d['letter']}"] = (cx - rr, cy - rr, cx + rr, cy + rr)
    for sec in sections:
        cutting_plane(msp, lay, sec, details, info["view_rects"], ext, sym)

    iso_w, iso_h = _size(ext["iso"])
    r, fit = free_rect(lay, (iso_w, iso_h))
    iso_scale = next((c for c in SCALES if c <= min(s, fit)), None) if r else None
    if iso_scale:
        cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3] + H + 4) / 2
        at = _xy(moves.get("iso"))
        if at is not None:  # where it was moved, inside the frame
            half, a = np.array([iso_w, iso_h]) * iso_scale / 2 + PAD, lay["area"]
            cx = float(np.clip(at[0], a[0] + half[0], max(a[0] + half[0], a[2] - half[0])))
            cy = float(np.clip(at[1], a[1] + half[1], max(a[1] + half[1], a[3] - half[1])))
        e = ext["iso"]
        iso_origin = np.array((cx - (e[0] + e[2]) / 2 * iso_scale, cy - (e[1] + e[3]) / 2 * iso_scale))
        draw_view(msp, lines["iso"], iso_origin, iso_scale)
        info["view_rects"]["iso"] = (*to_paper(e[:2], iso_origin, iso_scale), *to_paper(e[2:], iso_origin, iso_scale))
        if iso_scale != s:
            text(msp, f"ISOMETRIC  {fmt_scale(iso_scale)}", cx, info["view_rects"]["iso"][1] - 2,
                 align="TOP_CENTER")

    centres.update(draw_details(msp, details, lines, frames, ext, sym, circles, lay))

    # where each view sits on the paper, with its lines and centre marks: for surface marks,
    # callouts and for picking in the app
    def place_of(key, origin, scale, frame, rect, lines, **extra):
        return {"origin": [float(v) for v in origin], "scale": float(scale),
                "frame": [np.asarray(frame[0], float).tolist(), np.asarray(frame[1], float).tolist()],
                "rect": [float(v) for v in rect], "lines": lines, "centres": centres.get(key, []), **extra}

    def label(v):  # as the holes list names views
        return v if v in ortho else f"view {lay['aux'][v]['letter']}"

    places = {v: place_of(v, *view_place(lay, v), frames[v], info["view_rects"][v], lines[v], label=label(v),
                          kind="main" if v in ortho else "extra", sym=list(sym[v]),
                          partial=bool(lay["aux"].get(v, {}).get("partial")))
              for v in ortho + list(lay["aux"])}
    for sec in sections:
        places[sec["key"]] = place_of(sec["key"], sec["origin"], sec["scale"], sec["frame"],
                                      info["view_rects"][sec["key"]], sec["lines"], label=sec["key"],
                                      kind="section", cut=[sec["normal"].tolist(), sec["offset"]])
    for d in details:
        key = f"detail {d['letter']}"
        places[key] = place_of(key, d["origin"], d["scale"], frames[d["view"]], info["view_rects"][key],
                               lines[d["view"]], label=label(d["view"]), kind="detail", parent=d["view"])
    if "iso" in info["view_rects"]:
        places["iso"] = place_of("iso", iso_origin, iso_scale, frames["iso"], info["view_rects"]["iso"],
                                 lines["iso"], label="iso", kind="iso")
    info["places"] = places
    for m in surface_marks(msp, (cfg.get("surfaces") or {}).get("marks"), places):
        warnings.append(f"a surface mark on the {m.get('view')} view is left out: that view is not on the drawing")

    # what can be moved in the app, and where it is now
    names = {v: f"view {a['letter']}" for v, a in lay["aux"].items()} | {"iso": "isometric view"}
    names |= {x["key"]: f"section {x['letter']}–{x['letter']}" for x in sections}
    names |= {f"detail {d['letter']}": f"detail {d['letter']}" for d in details}
    items = {key: {"box": tuple(info["view_rects"][key]), "kind": "view", "label": name}
             for key, name in names.items() if key in info["view_rects"]}
    if "table" in lay:
        items["hole table"] = {"box": lay["table"], "kind": "view", "label": "hole table"}
    for key, item in draw_callouts(msp, callouts, places, rows, types, numbers, warnings).items():
        items[key] = {**item, "kind": "callout", "index": int(key.split()[1])}
    full_aux = [v for v, a in lay["aux"].items() if not a["partial"]]
    spots = datum_marks(shape, datums, lay, frames, lines, ortho + full_aux, sections) if datums else []
    fixed = datum_moves(spots, moves, places, datums["A"]) if datums else {}
    for key, (box, anchor) in draw_holes(msp, lay, rows, types, origins, at_centre, tables, widths, details,
                                         frames, ortho, spots, moves, fixed).items():
        items[key] = {"box": box, "kind": "datum A" if key == "datum A" else "offset", "label": key,
                      "anchor": None if anchor is None else np.asarray(anchor, float)}
    info["items"] = items
    info["letters"] = ([a["letter"] for a in lay["aux"].values()] + [x["letter"] for x in sections] +
                       [d["letter"] for d in details])

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


def draw_holes(msp, lay, rows, types, origins, at_centre, tables, widths, details, frames, ortho, datum_spots=(),
               moves=None, fixed=None):
    """ISO 6410 thread arcs, port centre lines, table origins, datum indicators,
    reference arrows, tags and the tables.

    Every hole is annotated in the view (or detail) where its opening is visible.
    Tags and datum indicators go where they were moved in the app (`moves`, and
    `fixed` from datum_moves), else in the freest space. Returns where each tag and
    datum indicator went: {key: (box, centre of its hole or None)}.
    """
    if not rows:
        return {}
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
    placed = place_datums(msp, occ, datum_spots, fixed)
    arrows = [(a["letter"], frames[view][0], [r["point"] for r in rows if r["view"] == view])
              for view, a in lay["aux"].items()]
    place_arrows(msp, occ, lay, frames, ortho, arrows)
    placed |= place_tags(msp, occ, [(", ".join(tags), cx, cy, rp) for tags, cx, cy, rp in spots.values()], moves)
    draw_tables(msp, lay["table"], lay["table_columns"], widths)
    return placed


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
                                  ezdxf.bbox.extents([v]), e.dxf.layer))
    boxes = [(t, (b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y), layer) for t, b, layer in boxes
             if b.has_data]
    problems = []
    for (ta, a, _), (tb, b, _) in itertools.combinations(boxes, 2):
        if _overlap(a, b, -0.05):
            problems.append(f"text '{ta}' overlaps text '{tb}'")
    for t, a, layer in boxes:
        for name, r in info["view_rects"].items():
            if layer not in ("HOLES", "SURFACES", "CALLOUTS") and _overlap(a, r):  # annotations belong on their view
                problems.append(f"text '{t}' overlaps {name} view")
        if not _inside(a, lay["area"]):
            problems.append(f"text '{t}' outside frame")
    ext = ezdxf.bbox.extents(e for e in doc.modelspace() if e.dxf.layer in ("VISIBLE", "DIMS", "CALLOUTS"))
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
