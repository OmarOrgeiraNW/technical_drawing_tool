"""STEP loading, view orientation and hidden-line projection."""

import numpy as np
from build123d import Compound, GeomType, import_step
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.HLRBRep import HLRBRep_PolyAlgo, HLRBRep_PolyHLRToShape

AXES = {
    "+X": (1, 0, 0), "-X": (-1, 0, 0),
    "+Y": (0, 1, 0), "-Y": (0, -1, 0),
    "+Z": (0, 0, 1), "-Z": (0, 0, -1),
}


def load_step(path):
    return import_step(str(path))


def envelope(shape):
    bb = shape.bounding_box()
    lo, hi = np.array(tuple(bb.min)), np.array(tuple(bb.max))
    return {"min": lo.round(4).tolist(), "max": hi.round(4).tolist(),
            "size": (hi - lo).round(4).tolist()}


def planar_faces(shape, count=5):
    """Largest planar faces, biggest first (datum / front-view candidates)."""
    faces = sorted((f for f in shape.faces() if f.geom_type == GeomType.PLANE),
                   key=lambda f: -f.area)[:count]
    return [{"area": round(f.area, 2),
             "normal": np.round(tuple(f.normal_at()), 4).tolist(),
             "center": np.round(tuple(f.center()), 3).tolist()} for f in faces]


def nearest_axis(v):
    v = np.asarray(v, float)
    i = int(np.argmax(np.abs(v)))
    return ("+" if v[i] > 0 else "-") + "XYZ"[i]


def guess_orientation(shape):
    """Front view looks at the largest flat face.

    'up' is +Z (or +Y when looking along Z) unless that makes the front view
    clearly portrait, in which case the shorter side is turned vertical.
    """
    faces = planar_faces(shape, 1)
    front = nearest_axis(faces[0]["normal"]) if faces else "-Y"
    size = envelope(shape)["size"]
    f = "XYZ".index(front[1])
    up = 1 if f == 2 else 2
    across = 3 - f - up
    if size[up] > 1.1 * size[across]:
        up = across
    return front, "+" + "XYZ"[up]


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


def mesh(shape, deflection):
    BRepMesh_IncrementalMesh(shape.wrapped, deflection, False, 0.2, True)


def project(shape, direction, up):
    """Visible sharp edges and silhouettes as 2D polylines (model units).

    Uses the polygonal HLR algorithm: ~1 s per view even on 2000+ face LPBF parts,
    where the exact algorithm takes minutes. Call mesh() first.
    """
    ax = gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(*direction))
    ax.SetYDirection(gp_Dir(*up))
    algo = HLRBRep_PolyAlgo()
    algo.Load(shape.wrapped)
    algo.Projector(HLRAlgo_Projector(ax))
    algo.Update()
    hlr = HLRBRep_PolyHLRToShape()
    hlr.Update(algo)
    segments = []
    for comp in (hlr.VCompound(), hlr.OutLineVCompound()):
        if comp.IsNull():
            continue
        for e in Compound(comp).edges():
            a, b = e.start_point(), e.end_point()
            segments.append(((a.X, a.Y), (b.X, b.Y)))
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


def symmetry(polylines, pixels=600, threshold=0.85):
    """(mirror about vertical centre line, mirror about horizontal centre line).

    Rasterises the view and compares it with its mirror image, so small
    asymmetric details (engraved text, lattice nodes) are tolerated.
    """
    x0, y0, x1, y1 = extents(polylines)
    step = max(x1 - x0, y1 - y0) / pixels
    nx, ny = int((x1 - x0) / step) + 1, int((y1 - y0) / step) + 1
    img = np.zeros((ny + 2, nx + 2), bool)
    for pl in polylines:
        for a, b in zip(pl[:-1], pl[1:]):
            n = int(np.hypot(*(b - a)) / step) + 2
            t = np.linspace(0, 1, n)[:, None]
            p = a + t * (b - a)
            img[((p[:, 1] - y0) / step).astype(int) + 1, ((p[:, 0] - x0) / step).astype(int) + 1] = True
    fat = img.copy()  # dilate 1 px to absorb mesh differences
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            fat |= np.roll(np.roll(img, dy, 0), dx, 1)
    total = img.sum()
    return (bool((img[:, ::-1] & fat).sum() >= threshold * total),
            bool((img[::-1, :] & fat).sum() >= threshold * total))
