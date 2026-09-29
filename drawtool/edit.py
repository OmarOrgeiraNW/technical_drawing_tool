"""Edits made by clicking on the drawing in the app: sections, callouts and moved items.

Each function takes the info of the last build (where every view sits on the
paper and what is drawn where) and clicks in paper millimetres from the lower
left corner of the sheet, and returns what goes into the part YAML. Points on
the part are kept as model coordinates and text positions as offsets on the
paper, so edits follow their views when the layout changes. Nothing here needs
a window: the tests drive the same code as the app.
"""

import numpy as np

from . import geometry, sheet

SNAP = 2.5  # mm on paper: a click this close to a hole or circle centre takes the centre
ON_LINE = 1.2  # mm on paper: a click this close to a drawn line takes the point on the line
DIRECTIONS = ("auto", "horizontal", "vertical", "aligned")  # of a dimension


def _round(values, n=4):
    return [round(float(v), n) for v in values]


def place_at(info, x, y, details=True):
    """(name, place) of the view under a paper point; details first (they are enlargements)."""
    hits = [(name, p) for name, p in info["places"].items()
            if p["rect"][0] - 2 <= x <= p["rect"][2] + 2 and p["rect"][1] - 2 <= y <= p["rect"][3] + 2
            and (details or "parent" not in p)]
    hits.sort(key=lambda h: ("parent" not in h[1], (h[1]["rect"][2] - h[1]["rect"][0]) *
                             (h[1]["rect"][3] - h[1]["rect"][1])))
    return hits[0] if hits else (None, None)


def view_xy(place, x, y):
    """Paper point -> the view's own 2D coordinates (model units)."""
    return (np.array([x, y], float) - np.asarray(place["origin"], float)) / place["scale"]


paper_xy = sheet.on_paper  # model point -> paper point in a view


def model_point(place, q, depth=0.0):
    """A model point that the view shows at its coordinates q, `depth` towards the viewer."""
    d, y = (np.asarray(v, float) for v in place["frame"])
    return q[0] * np.cross(y, d) + q[1] * y + depth * d


def openings(info, place):
    """Holes and ports marked in a view (a detail counts as the view it enlarges)."""
    return [o for o in info["holes"] + info["ports"] if o["view"] == place["label"]]


def _centres(info, place):
    """Centres of the visible circles, holes and ports of a view, in its coordinates."""
    d, y = (np.asarray(v, float) for v in place["frame"])
    x = np.cross(y, d)
    pts = [np.asarray(c, float) for c in place.get("centres", [])]
    pts += [np.array([np.asarray(o["point"]) @ x, np.asarray(o["point"]) @ y]) for o in openings(info, place)]
    return np.array(pts).reshape(-1, 2)


def centre_near(info, place, x, y, tol=SNAP):
    """The hole, port or circle centre (view coordinates) within `tol` mm of the click, or None."""
    pts = _centres(info, place)
    if not len(pts):
        return None
    dist = np.hypot(*(pts - view_xy(place, x, y)).T)
    i = int(np.argmin(dist))
    return pts[i] if dist[i] * place["scale"] <= tol else None


def line_near(place, x, y, tol=ON_LINE):
    """The point on the view's drawn lines nearest the click, within `tol` mm (a corner when
    one is that close), in view coordinates, or None."""
    q = view_xy(place, x, y)
    r = tol / place["scale"]
    corner, point = (None, np.inf), (None, np.inf)
    for pl in place.get("lines", []):
        pl = np.asarray(pl, float)
        if len(pl) < 2 or np.any(q < pl.min(axis=0) - r) or np.any(q > pl.max(axis=0) + r):
            continue
        seg = np.diff(pl, axis=0)
        angle = np.arctan2(seg[:, 1], seg[:, 0])
        turn = np.abs((np.diff(angle) + np.pi) % (2 * np.pi) - np.pi)
        corners = np.vstack([pl[:1], pl[1:-1][turn > np.radians(20)], pl[-1:]])  # not points along curves
        dist = np.hypot(*(corners - q).T)
        i = int(np.argmin(dist))
        if dist[i] < corner[1]:
            corner = (corners[i], dist[i])
        t = np.clip(((q - pl[:-1]) * seg).sum(axis=1) / np.maximum((seg * seg).sum(axis=1), 1e-18), 0, 1)
        foot = pl[:-1] + t[:, None] * seg
        dist = np.hypot(*(foot - q).T)
        i = int(np.argmin(dist))
        if dist[i] < point[1]:
            point = (foot[i], dist[i])
    for found, dist in (corner, point):
        if dist <= r:
            return found
    return None


# ---------------------------------------------------------------- sections

def new_letter(info, cfg):
    """A section letter not used anywhere on the drawing yet (so no other letter changes)."""
    taken = set(info.get("letters", [])) | {str(s.get("letter")) for s in cfg.get("sections") or []}
    return next(letter for letter in sheet.DETAIL_LETTERS if letter not in taken)


def section(info, cfg, x, y, look):
    """A section whose cutting plane passes through the clicked point of a view.

    The plane is square to that view and across the arrows, which point `look`
    (right / left / up / down on the paper): the direction the section is seen
    in. The cutting line snaps to hole and circle centres and to the view's
    centre lines. Returns (entry, None) or (None, message).
    """
    name, place = place_at(info, x, y, details=False)
    if place is None or place.get("kind") not in ("main", "extra") or place.get("partial"):
        return None, "Click on the front, top or side view (or a whole extra view)."
    u = sheet.LOOKS[look]
    k = 0 if u[0] else 1  # the arrows run along x (k = 0: a vertical cutting line) or y
    q = view_xy(place, x, y)
    stops = list(_centres(info, place)[:, k])
    r = place["rect"]
    if place["sym"][k]:  # the view's centre line
        stops.append(view_xy(place, (r[0] + r[2]) / 2, (r[1] + r[3]) / 2)[k])
    if stops:
        best = min(stops, key=lambda v: abs(v - q[k]))
        if abs(best - q[k]) * place["scale"] <= SNAP:
            q[k] = best
    return {"letter": new_letter(info, cfg), "view": name, "point": _round(model_point(place, q)),
            "look": look}, None


# ---------------------------------------------------------------- callouts

def leader_tip(shape, info, x, y):
    """Where a leader starts for a click on a view: ((view name, model point, on an outline), None)
    or (None, message). On a drawn line the leader ends in an arrow, inside a face in a dot
    (ISO 128-22)."""
    name, place = place_at(info, x, y)
    if place is None:
        return None, "That is not on a view: click on the part."
    q = line_near(place, x, y)
    d = np.asarray(place["frame"][0], float)
    hit = geometry.pick(shape, place["frame"], q if q is not None else view_xy(place, x, y),
                        0.8 / place["scale"], cut=place.get("cut"))
    if q is not None:
        return (name, _round(model_point(place, q, hit[0] @ d if hit else 0.0)), True), None
    if hit is None:
        return None, "No part there: click on the part."
    return (name, _round(hit[0]), bool(hit[1])), None


def opening_at(info, x, y):
    """((view name, hole or port), None) for the opening nearest the click, or (None, message)."""
    name, place = place_at(info, x, y)
    if place is None:
        return None, "That is not on a view: click a hole or port."
    near = sorted(((np.hypot(*(np.array([x, y]) - paper_xy(place, o["point"]))), i) for i, o in
                   enumerate(openings(info, place))))
    if not near or near[0][0] > 8:
        return None, "No hole or port there: click closer to one."
    return (name, openings(info, place)[near[0][1]]), None


def callout(kind, view, point, anchor, x, y, text="", edge=True):
    """A leader note, hole callout or balloon; its text (or balloon) goes where the second click
    was, kept as an offset from `anchor` (the leader's start, or the hole centre)."""
    entry = {"type": kind, "view": view, "point": _round(point), "at": _round(np.array([x, y]) - anchor, 2),
             "text": text}
    if kind != "hole":
        entry["edge"] = bool(edge)
    return entry


def dimension_point(info, x, y, view=None):
    """One end of a dimension: ((view name, model point), None) at the hole or circle centre,
    corner or line nearest the click, or (None, message). With `view`, only in that view."""
    name, place = place_at(info, x, y)
    if place is None or (view and name != view):
        return None, "Click on the view of the first point." if view else "Click on a view."
    q = centre_near(info, place, x, y)
    if q is None:
        q = line_near(place, x, y)
    if q is None:
        return None, "Click on a line, a corner or a hole centre."
    return (name, _round(model_point(place, q))), None


def dimension(info, view, p1, p2, x, y, direction="auto", text=""):
    """A dimension between two points of a view, its dimension line through the click.

    auto: horizontal when the click is above or below the points, vertical when
    it is beside them, else along the longer side. Returns (entry, None) or
    (None, message).
    """
    place = info["places"][view]
    a, b = paper_xy(place, p1), paper_xy(place, p2)
    c = np.array([x, y], float)
    span = np.abs(b - a)
    if direction == "auto":
        lo, hi = np.minimum(a, b), np.maximum(a, b)
        beside = c[0] < lo[0] or c[0] > hi[0]
        above = c[1] < lo[1] or c[1] > hi[1]
        direction = ("horizontal" if above and not beside else "vertical" if beside and not above else
                     "horizontal" if span[0] >= span[1] else "vertical")
        if span[0 if direction == "horizontal" else 1] < 1e-6:  # nothing to measure that way
            direction = "vertical" if direction == "horizontal" else "horizontal"
    measured = {"horizontal": span[0], "vertical": span[1], "aligned": np.hypot(*span)}[direction]
    if measured < 1e-6:
        return None, "The two points are the same in that direction."
    return {"type": "dimension", "view": view, "points": [_round(p1), _round(p2)], "direction": direction,
            "at": _round(c - a, 2), "text": text}, None


# ---------------------------------------------------------------- moving items

def item_at(info, x, y):
    """The movable item under a click: the smallest one whose box holds it, or None."""
    hits = [(key, it["box"]) for key, it in info.get("items", {}).items()
            if it["box"][0] - 1 <= x <= it["box"][2] + 1 and it["box"][1] - 1 <= y <= it["box"][3] + 1]
    return min(hits, key=lambda h: (h[1][2] - h[1][0]) * (h[1][3] - h[1][1]))[0] if hits else None


def move(cfg, info, key, x, y):
    """Put an item where the user clicked: views and the hole table with their centre there;
    tags, datum letters B / C and callouts as an offset from what they point at.
    Datum A is moved with datum_a_spot() and move_datum_a()."""
    item = info["items"][key]
    at = np.array([x, y], float)
    if item["kind"] == "callout":
        cfg["callouts"][item["index"]]["at"] = _round(at - item["anchor"], 2)
    elif item["kind"] == "view":
        cfg.setdefault("moves", {})[key] = _round(at, 2)
    else:
        cfg.setdefault("moves", {})[key] = _round(at - item["anchor"], 2)


def datum_a_spot(shape, info, x, y):
    """Where datum A's triangle goes: a click on A's face where a view shows it edge-on.
    Returns ((view name, model point), None) or (None, message)."""
    tip, error = leader_tip(shape, info, x, y)
    if error:
        return None, error
    name, point, edge = tip
    a = info["datums"]["A"]
    n, d = np.array(a["normal"]), np.asarray(info["places"][name]["frame"][0], float)
    if not edge or abs(n @ d) > 1e-3 or abs((np.array(point) - a["centre"]) @ n) > 0.05:
        return None, "Click on datum A's face where a view shows it edge-on (as a line)."
    return (name, point), None


def move_datum_a(cfg, info, view, point, x, y):
    """Datum A's triangle on its face at `point` of `view`, its letter towards the click."""
    anchor = paper_xy(info["places"][view], point)
    cfg.setdefault("moves", {})["datum A"] = {"view": view, "point": _round(point),
                                              "at": _round(np.array([x, y]) - anchor, 2)}
