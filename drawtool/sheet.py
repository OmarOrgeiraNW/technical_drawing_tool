"""Drawing sheet: ISO 5457 frame, first-angle views, dimensions, ISO 7200 title block.

Everything is drawn in modelspace in paper millimetres (views are scaled);
dimensions use DIMLFAC so they show true part size. The PDF and PNG are
rendered from the same DXF, so all three outputs always agree.
"""

import itertools
import textwrap
from pathlib import Path

import ezdxf
import ezdxf.bbox
import numpy as np
from ezdxf.addons.drawing import Frontend, RenderContext, config, layout
from ezdxf.addons.drawing import pymupdf as pdf_backend
from ezdxf.enums import TextEntityAlignment
from ezdxf.fonts import fonts
from ezdxf.math import BoundingBox2d

from . import geometry

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
}


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
    lw = _size(ext["left"])[0] * s
    th = _size(ext["top"])[1] * s
    group_w, group_h = DIM_SPACE + fw + GAP + lw, DIM_SPACE + fh + GAP + th
    free_w = inner[2] - inner[0]
    for gx, gtop in ((inner[0] + (free_w - group_w) / 2, inner[3]), (inner[0], inner[3])):
        fx, ftop = gx + DIM_SPACE, gtop - DIM_SPACE
        rects = {
            "front": (fx, ftop - fh, fx + fw, ftop),
            "top": (fx, ftop - fh - GAP - th, fx + fw, ftop - fh - GAP),
            "left": (fx + fw + GAP, ftop - fh, fx + fw + GAP + lw, ftop),
        }
        # keep-out zones = views plus the dimension space above/left of them
        zones = [(rects["front"][0] - DIM_SPACE, *rects["front"][1:3], rects["front"][3] + DIM_SPACE),
                 (rects["top"][0] - DIM_SPACE, *rects["top"][1:]),
                 rects["left"]]
        if all(_inside(z, inner) and not _overlap(z, tb, PAD) for z in zones):
            origins = {}
            for name, r in rects.items():
                e = ext[name]
                origins[name] = (r[0] - e[0] * s, r[1] - e[1] * s)
            return {"sheet": sheet, "size": (w, h), "scale": s, "area": area,
                    "title_block": tb, "rects": rects, "zones": zones, "origins": origins}
    return None


def choose_layout(ext, notes_h, sheet="auto", scale="auto"):
    """A4 if the part fits at 1:1 or larger, otherwise the best scale on A3."""
    if sheet != "auto" and sheet not in SHEETS:
        raise ValueError(f"unknown sheet size '{sheet}': use auto, A4 or A3")
    sheets = ["A4", "A3"] if sheet == "auto" else [sheet]
    scales = SCALES if scale == "auto" else [parse_scale(scale)]
    for i, name in enumerate(sheets):
        for s in scales:
            lay = place(ext, name, s, notes_h)
            if lay is None:
                continue
            if s >= 1 or i == len(sheets) - 1 or scale != "auto":
                return lay
            break  # only fits reduced on this sheet: try the next size first
    raise ValueError("views do not fit; set 'sheet' and/or 'scale' in the part YAML")


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
    dim = msp.add_linear_dim(base=base, p1=tuple(p1), p2=tuple(p2), angle=angle, dimstyle="ISO",
                             text=prefix + "<>", override={"dimlfac": 1 / s},
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


def title_block(msp, lay, tb):
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
    cell(0, 4, 60, 5, "Material", t["material"])
    cell(60, 4, 100, 5, "General tolerances", tb["general_tolerance"])
    r = cell(100, 4, 135, 5, "Surface texture", "")
    if tb["default_finish"]:
        texture_symbol(msp, r[0] + 3, r[1] + 1.2)
        text(msp, tb["default_finish"], r[0] + 7.5, r[1] + 1.6, align="LEFT", width=r[2] - r[0] - 9)
    cell(135, 4, 155, 5, "Scale", fmt_scale(lay["scale"]))
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
        wrapped = textwrap.wrap(str(n), 80) or [""]
        lines += [f"{i}. {wrapped[0]}"] + [f"    {w}" for w in wrapped[1:]]
    return lines


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
    geometry.mesh(shape, max(env["size"]) / 5000)
    lines = {name: geometry.project(shape, d, y) for name, (d, y) in frames.items()}
    ext = view_extents(env, {k: v for k, v in frames.items() if k != "iso"})
    ext["iso"] = geometry.extents(lines["iso"])

    notes = cfg["notes"] or []
    lay = choose_layout(ext, notes_height(notes), cfg["sheet"], cfg["scale"])
    s = lay["scale"]
    doc = new_doc()
    msp = doc.modelspace()
    draw_frame(msp, lay)

    info = {"sheet": lay["sheet"], "scale": s, "front": front, "up": up, "dims": [],
            "view_rects": dict(lay["rects"])}
    for name in ("front", "top", "left"):
        o = np.array(lay["origins"][name])
        draw_view(msp, lines[name], o, s)
        draw_centre_lines(msp, ext[name], o, s, geometry.symmetry(lines[name]))
    # overall envelope: width + height on the front view (one Ø if it is round), depth on the top view
    round_front = geometry.is_round(lines["front"], ext["front"])
    dims = [("front", dim_horizontal_above, "%%c" if round_front else "")]
    if not round_front:
        dims.append(("front", dim_vertical_left, ""))
    dims.append(("top", dim_vertical_left, ""))
    for view, fn, prefix in dims:
        dim = fn(msp, lines[view], ext[view], np.array(lay["origins"][view]), s, prefix)
        info["dims"].append((view, prefix.replace("%%c", "Ø") + str(round(dim.get_measurement() / s, 4))))

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

    title_block(msp, lay, cfg)
    draw_notes(msp, lay, notes)
    info["problems"] = check(doc, info, lay)
    return doc, info


def check(doc, info, lay):
    """Overlapping text, text on geometry, anything outside the frame."""
    boxes = []
    for e in doc.modelspace():
        if e.dxftype() == "TEXT":
            boxes.append((e.dxf.text, ezdxf.bbox.extents([e])))
        elif e.dxftype() == "DIMENSION":
            for v in e.virtual_entities():
                if v.dxftype() in ("MTEXT", "TEXT"):
                    boxes.append((v.plain_text() if v.dxftype() == "MTEXT" else v.dxf.text,
                                  ezdxf.bbox.extents([v])))
    boxes = [(t, (b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y)) for t, b in boxes if b.has_data]
    problems = []
    for (ta, a), (tb, b) in itertools.combinations(boxes, 2):
        if _overlap(a, b, -0.05):
            problems.append(f"text '{ta}' overlaps text '{tb}'")
    for t, a in boxes:
        for name, r in info["view_rects"].items():
            if _overlap(a, r):
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
