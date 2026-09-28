"""STEP loading, view orientation, hidden-line projection and hole detection."""

import colorsys
from collections import defaultdict
from pathlib import Path

import numpy as np
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepGProp import BRepGProp
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Plane
from OCP.gp import gp_Ax2, gp_Dir, gp_Lin, gp_Pnt, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.HLRBRep import HLRBRep_PolyAlgo, HLRBRep_PolyHLRToShape
from OCP.IFSelect import IFSelect_RetDone
from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
from OCP.Quantity import Quantity_Color
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.STEPControl import STEPControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDocStd import TDocStd_Document
from OCP.XCAFDoc import XCAFDoc_ColorGen, XCAFDoc_ColorSurf, XCAFDoc_DocumentTool
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher as ShapeMap
from OCP.collections import Sequence_TDF_Label

AXES = {
    "+X": (1, 0, 0), "-X": (-1, 0, 0),
    "+Y": (0, 1, 0), "-Y": (0, -1, 0),
    "+Z": (0, 0, 1), "-Z": (0, 0, -1),
}


def load_step(path):
    """The STEP file's geometry as one OpenCascade shape.

    Face colours (AP214/AP242 files) are read too and kept for face_colour().
    Files without colour data (e.g. AP203) take the faster plain reader.
    """
    _colours.update(shape=None, maps={})
    if b"STYLED_ITEM" not in Path(path).read_bytes():  # no presentation styles, so no colours
        reader = STEPControl_Reader()
        if reader.ReadFile(str(path)) != IFSelect_RetDone:
            raise ValueError(f"cannot read STEP file {path}")
        reader.TransferRoots()
        return reader.OneShape()
    doc = TDocStd_Document(TCollection_ExtendedString("XCAF"))
    reader = STEPCAFControl_Reader()
    reader.SetColorMode(True)
    if reader.ReadFile(str(path)) != IFSelect_RetDone or not reader.Transfer(doc):
        raise ValueError(f"cannot read STEP file {path}")
    shapes = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    colours = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    labels = Sequence_TDF_Label()
    shapes.GetFreeShapes(labels)
    shape = shapes.GetShape_s(labels.Value(1))
    maps = defaultdict(ShapeMap)
    for face in _explore(shape, TopAbs_FACE, TopoDS.Face):
        c = Quantity_Color()
        if colours.GetColor(face, XCAFDoc_ColorSurf, c) or colours.GetColor(face, XCAFDoc_ColorGen, c):
            name = colour_name(c.Red(), c.Green(), c.Blue())
            if name:
                maps[name].Add(face)
    _colours.update(shape=shape, maps=dict(maps))
    return shape


_colours = {"shape": None, "maps": {}}


def colour_name(r, g, b):
    """'red' / 'green' / 'blue' for clearly coloured faces; None for greys and other hues."""
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if s < 0.35 or v < 0.2:
        return None
    hue = h * 360
    return "red" if hue < 30 or hue > 330 else "green" if 90 < hue < 150 else \
        "blue" if 210 < hue < 270 else None


def face_colour(shape, face):
    """Colour name of a face of the shape last returned by load_step()."""
    if _colours["shape"] is not shape:
        return None
    return next((name for name, faces in _colours["maps"].items() if faces.Contains(face)), None)


def _explore(shape, kind, cast):
    exp = TopExp_Explorer(shape, kind)
    while exp.More():
        yield cast(exp.Current())
        exp.Next()


def envelope(shape):
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)  # exact geometry, not the mesh
    lo, hi = np.array(box.CornerMin().Coord()), np.array(box.CornerMax().Coord())
    return {"min": lo.round(4).tolist(), "max": hi.round(4).tolist(),
            "size": (hi - lo).round(4).tolist()}


def planar_faces(shape, count=5):
    """Largest planar faces, biggest first (datum / front-view candidates)."""
    faces = []
    for face in _explore(shape, TopAbs_FACE, TopoDS.Face):
        surface = BRepAdaptor_Surface(face)
        if surface.GetType() != GeomAbs_Plane:
            continue
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        normal = np.array(surface.Plane().Axis().Direction().Coord())
        if face.Orientation() == TopAbs_REVERSED:
            normal = -normal
        faces.append({"area": round(props.Mass(), 2), "normal": np.round(normal, 4).tolist(),
                      "center": np.round(props.CentreOfMass().Coord(), 3).tolist()})
    return sorted(faces, key=lambda f: -f["area"])[:count]


def nearest_axis(v):
    v = np.asarray(v, float)
    i = int(np.argmax(np.abs(v)))
    return ("+" if v[i] > 0 else "-") + "XYZ"[i]


FRONT_PREFERENCE = ["-Y", "+X", "+Y", "-X", "+Z", "-Z"]  # tie-break: conventional front first


def guess_orientation(shape):
    """(front, up) for the principal views.

    The largest flat face becomes the base (drawn at the bottom) when it covers
    a good part of the footprint, as a flange does; otherwise +Z stays up.
    The front view is the most informative of the four horizontal views
    (ISO 128-3), measured as total visible edge length.
    """
    env = envelope(shape)
    up = np.array([0.0, 0.0, 1.0])
    faces = planar_faces(shape, 1)
    if faces:
        n = np.array(faces[0]["normal"])
        i = int(np.argmax(np.abs(n)))
        across = [env["size"][k] for k in range(3) if k != i]
        if faces[0]["area"] >= 0.3 * across[0] * across[1]:
            up = -np.sign(n[i]) * np.eye(3)[i]
    prepare(shape)
    candidates = [a for a in FRONT_PREFERENCE if abs(np.dot(AXES[a], up)) < 0.5]
    detail = {a: sum(np.hypot(*np.diff(pl, axis=0).T).sum()
                     for pl in project(shape, np.array(AXES[a], float), up)) for a in candidates}
    best = max(detail.values())
    front = next(a for a in candidates if detail[a] >= 0.95 * best)
    return front, nearest_axis(up)


def view_frames(front, up, iso=None):
    """Viewing direction (towards the viewer) and screen-up vector per view.

    Views are the same in first and third angle; only their placement differs.
    """
    d, y = np.array(AXES[front], float), np.array(AXES[up], float)
    if abs(d @ y) > 1e-9:
        raise ValueError(f"up {up} must be perpendicular to front {front}")
    x = np.cross(y, d)  # screen-right of the front view
    iso_d = _parse_direction(iso) if iso else d + y + x
    iso_d = iso_d / np.linalg.norm(iso_d)
    iso_y = y - (y @ iso_d) * iso_d
    if np.linalg.norm(iso_y) < 1e-6:
        iso_y = -d
    return {
        "front": (d, y),
        "top": (y, -d),     # seen from above
        "left": (-x, y),    # seen from the left
        "iso": (iso_d, iso_y / np.linalg.norm(iso_y)),
    }


def _parse_direction(text):
    """'+X-Y+Z' -> (1, -1, 1)."""
    v = np.zeros(3)
    for sign, axis in zip(text[::2], text[1::2]):
        v += np.array(AXES[sign + axis])
    return v


def prepare(shape):
    """Mesh the shape for projection (fine enough for any drawing scale)."""
    BRepMesh_IncrementalMesh(shape, max(envelope(shape)["size"]) / 5000, False, 0.2, True)


def volume(shape):
    """mm3. Adaptive integration: the default Gauss rule is ~2 % off on large spline parts."""
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props, 1e-4, True)
    return props.Mass()


_projections = {"shape": None, "views": {}}


def project(shape, direction, up):
    """Visible sharp edges and silhouettes as 2D polylines (model units).

    Uses the polygonal HLR algorithm: ~1 s per view even on 2000+ face LPBF parts,
    where the exact algorithm takes minutes. Call prepare() first. Results are
    cached for the current shape, so orientation search and drawing share them.
    """
    if _projections["shape"] is not shape:
        _projections.update(shape=shape, views={})
    key = (tuple(np.round(direction, 6)), tuple(np.round(up, 6)))
    if key not in _projections["views"]:
        _projections["views"][key] = _project(shape, direction, up)
    return _projections["views"][key]


def _project(shape, direction, up):
    ax = gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(*direction))
    ax.SetYDirection(gp_Dir(*up))
    algo = HLRBRep_PolyAlgo()
    algo.Load(shape)
    algo.Projector(HLRAlgo_Projector(ax))
    algo.Update()
    hlr = HLRBRep_PolyHLRToShape()
    hlr.Update(algo)
    segments = []
    for comp in (hlr.VCompound(), hlr.OutLineVCompound()):
        if comp.IsNull():
            continue
        for e in _explore(comp, TopAbs_EDGE, TopoDS.Edge):
            a = BRep_Tool.Pnt_s(TopExp.FirstVertex_s(e))
            b = BRep_Tool.Pnt_s(TopExp.LastVertex_s(e))
            segments.append(((a.X(), a.Y()), (b.X(), b.Y())))
    return chain(segments)


def chain(segments, tol=1e-4):
    """Join line segments sharing end points into polylines."""
    key = lambda p: (round(p[0] / tol), round(p[1] / tol))
    ends = {}
    for i, (a, b) in enumerate(segments):
        ends.setdefault(key(a), []).append(i)
        ends.setdefault(key(b), []).append(i)
    used = [False] * len(segments)

    def extend(line):
        while True:
            nxt = [i for i in ends.get(key(line[-1]), []) if not used[i]]
            if not nxt:
                return line
            used[nxt[0]] = True
            a, b = segments[nxt[0]]
            line.append(b if key(a) == key(line[-1]) else a)

    polylines = []
    for i, (a, b) in enumerate(segments):
        if used[i]:
            continue
        used[i] = True
        line = extend([a, b])[::-1]
        polylines.append(np.array(extend(line)))
    return polylines


def clip_to_circle(polylines, centre, radius):
    """The parts of the polylines inside a circle (for detail views)."""
    c = np.asarray(centre, float)
    out = []
    for pl in polylines:
        if np.hypot(*np.clip(c, pl.min(axis=0), pl.max(axis=0)) - c) > radius:
            continue  # bounding box misses the circle
        run = []
        for a, b in zip(pl[:-1], pl[1:]):
            span = _inside_span(a - c, b - c, radius)
            if span is None:
                if len(run) > 1:
                    out.append(np.array(run))
                run = []
                continue
            p, q = a + span[0] * (b - a), a + span[1] * (b - a)
            if run and np.allclose(run[-1], p):
                run.append(q)
            else:
                if len(run) > 1:
                    out.append(np.array(run))
                run = [p, q]
        if len(run) > 1:
            out.append(np.array(run))
    return out


def _inside_span(a, b, r):
    """Parameter range of segment a-b (circle-centred coordinates) inside the circle."""
    d = b - a
    qa, qb, qc = d @ d, 2 * (a @ d), a @ a - r * r
    if qa < 1e-18:
        return (0.0, 1.0) if qc <= 0 else None
    disc = qb * qb - 4 * qa * qc
    if disc <= 0:
        return None
    root = np.sqrt(disc)
    t0, t1 = max(0.0, (-qb - root) / (2 * qa)), min(1.0, (-qb + root) / (2 * qa))
    return (t0, t1) if t1 > t0 else None


def extents(polylines):
    pts = np.vstack(polylines)
    return (*pts.min(axis=0), *pts.max(axis=0))


def is_round(polylines, ext, tol=0.005):
    """True when the view outline is a full circle (dimension it with one Ø)."""
    w, h = ext[2] - ext[0], ext[3] - ext[1]
    if abs(w - h) > tol * w:
        return False
    pts = np.vstack(polylines) - ((ext[0] + ext[2]) / 2, (ext[1] + ext[3]) / 2)
    r = np.hypot(pts[:, 0], pts[:, 1])
    if r.max() > w / 2 * (1 + tol):  # corners stick out: square-ish, not round
        return False
    rim = pts[r > w / 2 * (1 - tol)]
    sectors = np.unique(((np.arctan2(rim[:, 1], rim[:, 0]) + np.pi) / (2 * np.pi) * 36).astype(int))
    return len(sectors) >= 34


class Raster:
    """A view's visible lines drawn into a boolean pixel grid (plus a 1 px dilated copy)."""

    def __init__(self, polylines, ext=None, pixels=600):
        self.x0, self.y0, x1, y1 = ext or extents(polylines)
        self.step = max(x1 - self.x0, y1 - self.y0) / pixels
        nx, ny = int((x1 - self.x0) / self.step) + 1, int((y1 - self.y0) / self.step) + 1
        self.img = np.zeros((ny + 2, nx + 2), bool)
        for pl in polylines:
            for a, b in zip(pl[:-1], pl[1:]):
                n = int(np.hypot(*(b - a)) / self.step) + 2
                p = a + np.linspace(0, 1, n)[:, None] * (b - a)
                self.img[self._rows(p[:, 1]), self._cols(p[:, 0])] = True
        self.fat = self.img.copy()  # absorbs mesh differences
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                self.fat |= np.roll(np.roll(self.img, dy, 0), dx, 1)

    def _rows(self, y):
        return np.clip(((y - self.y0) / self.step).astype(int) + 1, 0, self.img.shape[0] - 1)

    def _cols(self, x):
        return np.clip(((x - self.x0) / self.step).astype(int) + 1, 0, self.img.shape[1] - 1)

    def coverage(self, points):
        """Fraction of 2D points lying on drawn lines."""
        points = np.asarray(points)
        return self.fat[self._rows(points[:, 1]), self._cols(points[:, 0])].mean()


def _match(a, b):
    """Fraction of a's line pixels that fall on b's lines (grids cropped to a common size)."""
    ny, nx = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
    a = a[:ny, :nx]
    return (a & b[:ny, :nx]).sum() / max(a.sum(), 1)


def symmetry(polylines, threshold=0.85):
    """(mirror about vertical centre line, mirror about horizontal centre line).

    Compares the rasterised view with its mirror image, so small asymmetric
    details (engraved text, lattice nodes) are tolerated.
    """
    r = Raster(polylines)
    return (bool(_match(r.img[:, ::-1], r.fat) >= threshold),
            bool(_match(r.img[::-1, :], r.fat) >= threshold))


def same_view(a, b, ext_a, ext_b, threshold=0.85):
    """True when two views show (nearly) the same picture, directly or mirrored."""
    wa, ha = ext_a[2] - ext_a[0], ext_a[3] - ext_a[1]
    wb, hb = ext_b[2] - ext_b[0], ext_b[3] - ext_b[1]
    if abs(wa - wb) > 0.02 * max(wa, wb) or abs(ha - hb) > 0.02 * max(ha, hb):
        return False
    ra, rb = Raster(a, ext_a), Raster(b, ext_b)
    for img_b, fat_b in ((rb.img, rb.fat), (rb.img[:, ::-1], rb.fat[:, ::-1])):
        if min(_match(ra.img, fat_b), _match(img_b, ra.fat)) >= threshold:
            return True
    return False


def circles(shape):
    """Full circles on the part as (centre, unit axis, radius); split arcs are joined."""
    edges = ShapeMap()
    TopExp.MapShapes_s(shape, TopAbs_EDGE, edges)  # each edge once
    spans = {}
    for i in range(1, edges.Extent() + 1):
        curve = BRepAdaptor_Curve(TopoDS.Edge(edges.FindKey(i)))
        if curve.GetType() != GeomAbs_Circle:
            continue
        circ = curve.Circle()
        centre = np.array(circ.Axis().Location().Coord())
        axis = np.array(circ.Axis().Direction().Coord())
        if axis[np.argmax(np.abs(axis))] < 0:
            axis = -axis
        key = (tuple(np.round(centre, 3)), tuple(np.round(axis, 4)), round(circ.Radius(), 4))
        spans[key] = spans.get(key, 0.0) + abs(curve.LastParameter() - curve.FirstParameter())
    return [(np.array(c), np.array(a), r) for (c, a, r), span in spans.items()
            if span > 2 * np.pi - 0.05]


# ---------------------------------------------------------------- holes

def _revolved(shape, face):
    """Axis, radius, axial extent and concavity of a cylindrical or conical face."""
    surf = BRepAdaptor_Surface(face)
    kind = surf.GetType()
    if kind not in (GeomAbs_Cylinder, GeomAbs_Cone):
        return None
    prim = surf.Cylinder() if kind == GeomAbs_Cylinder else surf.Cone()
    loc = np.array(prim.Axis().Location().Coord())
    d = np.array(prim.Axis().Direction().Coord())
    if d[np.argmax(np.abs(d))] < 0:
        d = -d
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    pts = np.array([surf.Value(u, v).Coord() for u in np.linspace(u0, u1, 5) for v in np.linspace(v0, v1, 3)])
    radial = pts - (loc + np.outer((pts - loc) @ d, d))
    p, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
    surf.D1((u0 + u1) / 2, (v0 + v1) / 2, p, du, dv)
    normal = np.array(du.Crossed(dv).Coord())
    if face.Orientation() == TopAbs_REVERSED:
        normal = -normal
    mid = np.array(p.Coord())
    mid_radial = mid - (loc + ((mid - loc) @ d) * d)
    base = loc - (loc @ d) * d
    along = pts @ d
    return {"cone": kind == GeomAbs_Cone, "d": d, "base": base,
            "key": (tuple(np.round(d, 3)), tuple(np.round(base, 2))),
            "r": prim.Radius() if kind == GeomAbs_Cylinder else None,
            "rmin": np.linalg.norm(radial, axis=1).min(), "rmax": np.linalg.norm(radial, axis=1).max(),
            "span": u1 - u0, "angle": round(((u0 + u1) / 2) % (2 * np.pi), 1),
            "lo": along.min(), "hi": along.max(), "concave": bool(normal @ mid_radial < 0),
            "semi_angle": prim.SemiAngle() if kind == GeomAbs_Cone else None,
            "colour": face_colour(shape, face)}


def holes(shape):
    """Drilled holes: coaxial concave full cylinders (plus cones) opening onto a flat face.

    Each hole: axis, entries (point + outward direction per open flat end),
    drill diameter, depth (blind) or through, counterbore / countersink at the
    entry, thread pitch when the thread is modelled, and face colour names.
    Short line/face intersection probes decide what is open, closed or solid;
    they cost ~2 ms each, unlike point classification (~100 ms on large parts).
    """
    lines = defaultdict(list)
    for face in _explore(shape, TopAbs_FACE, TopoDS.Face):
        info = _revolved(shape, face)
        if info and info["concave"]:
            lines[info["key"]].append(info)
    probe = IntCurvesFace_ShapeIntersector()
    probe.Load(shape, 1e-6)

    def crosses(point, d, a, b):
        probe.Perform(gp_Lin(gp_Pnt(*point), gp_Dir(*d)), a, b)
        return probe.NbPnt() > 0

    found = []
    for faces in lines.values():
        d, base = faces[0]["d"], faces[0]["base"]
        by_radius = defaultdict(list)
        for f in faces:
            if not f["cone"]:
                by_radius[round(f["r"], 3)].append(f)
        full = {r for r, fs in by_radius.items() if sum(f["span"] for f in fs) > 2 * np.pi - 0.05}
        if not full:
            continue
        segs = sorted([f for r in full for f in by_radius[r]] + [f for f in faces if f["cone"]],
                      key=lambda f: f["lo"])
        e1 = np.cross(d, [1, 0, 0] if abs(d[0]) < 0.9 else [0, 1, 0])
        e1 /= np.linalg.norm(e1)
        e2 = np.cross(d, e1)
        r_min = min(full)
        off_axis = base + 0.3 * r_min * e1  # avoids cone apexes on the axis
        groups, current = [], [segs[0]]
        for f in segs[1:]:
            top = max(x["hi"] for x in current)
            if f["lo"] <= top + 1e-3 or not crosses(off_axis + d * top, d, 0, f["lo"] - top):
                current.append(f)  # touching, or only air in between (e.g. thread flanks, a cavity)
            else:
                groups.append(current)
                current = [f]
        groups.append(current)
        for group in groups:
            hole = _hole(group, d, base, e1, e2, full, crosses)
            if hole:
                found.append(hole)
    return sorted(found, key=lambda h: (tuple(np.round(h["axis"], 3)), round(h["diameter"], 3),
                                        tuple(np.round(h["entries"][0]["point"], 2))))


def _hole(group, d, base, e1, e2, full, crosses):
    cylinders = [f for f in group if not f["cone"] and round(f["r"], 3) in full]
    if not cylinders:
        return None
    r_drill = min(f["r"] for f in cylinders)
    lo, hi = min(f["lo"] for f in group), max(f["hi"] for f in group)
    ring = [np.cos(a) * e1 + np.sin(a) * e2 for a in np.linspace(0, 2 * np.pi, 8, endpoint=False)]
    ends = []
    for at, out in ((lo, -1), (hi, 1)):
        closed = crosses(base + 0.3 * r_drill * e1 + d * at, d * out, -0.5 * r_drill, 0.05)
        r_end = max(f["rmax"] for f in group if abs((f["lo"] if out < 0 else f["hi"]) - at) < 1e-3)
        flat = sum(crosses(base + (r_end + 0.3) * v + d * at, d, -0.1, 0.1) for v in ring) >= 6
        ends.append({"at": at, "out": out, "open": not closed, "entry": not closed and flat})
    if not any(e["entry"] for e in ends):
        return None  # e.g. a waveguide section inside the part, not a drilled hole
    drill = [f for f in cylinders if abs(f["r"] - r_drill) < 1e-3]
    through = ends[0]["open"] and ends[1]["open"]
    entries = []
    for e in ends:
        if not e["entry"]:
            continue
        at_end = [f for f in group if abs((f["lo"] if e["out"] < 0 else f["hi"]) - e["at"]) < 1e-3]
        r_end = max(f["rmax"] for f in at_end)
        # seen from outside along the axis unless other material blocks the view (one ray may graze)
        blocked = sum(crosses(base + (r_end + 0.3) * v + d * e["at"], d * e["out"], 0.05, 1e5)
                      for v in ring[::2])
        entry = {"point": base + d * e["at"], "out": d * e["out"], "cbore": None, "csk": None,
                 "exposed": blocked <= 1, "depth": None}
        wide = [f for f in at_end if not f["cone"] and f["r"] > r_drill + 1e-3]
        cone = [f for f in at_end if f["cone"] and f["rmax"] > r_drill + 1e-3]
        if wide:
            f = max(wide, key=lambda f: f["r"])
            entry["cbore"] = (2 * f["r"], f["hi"] - f["lo"])
        elif cone:
            f = cone[0]
            entry["csk"] = (2 * f["rmax"], round(2 * np.degrees(f["semi_angle"])))
        if not through:
            far = max(f["hi"] for f in drill) if e["out"] < 0 else min(f["lo"] for f in drill)
            entry["depth"] = abs(far - e["at"])
        entries.append(entry)
    return {"axis": d, "diameter": 2 * r_drill, "through": through, "length": hi - lo,
            "entries": entries,
            "pitch": _pitch(drill), "colours": sorted({f["colour"] for f in group if f["colour"]})}


def _pitch(drill_faces):
    """Thread pitch when the thread is modelled: the drill cylinder is cut into
    many equal pieces by the helical flanks; pieces at the same angle repeat every pitch."""
    if len(drill_faces) < 6:
        return None
    by_angle = defaultdict(list)
    for f in drill_faces:
        by_angle[f["angle"]].append(f["lo"])
    steps = [s for los in by_angle.values() for s in np.diff(sorted(los)) if s > 1e-3]
    return round(float(np.median(steps)), 3) if len(steps) >= 3 else None
