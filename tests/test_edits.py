"""Sections, callouts and moves made in the app.

The tests click where the app's user would (paper millimetres worked out from
the build info) through the same edit functions the app calls, then check the
drawing that the YAML entries produce.
"""

import copy
from pathlib import Path

import numpy as np
import pytest
import yaml

from drawtool import edit, geometry, partfile, sheet

HORN = Path(__file__).resolve().parent.parent / "samples" / "2026_09_16-Design-NW-HornQV-v01-REFERENCE.STEP"


@pytest.fixture(scope="module")
def horn():
    shape = geometry.load_step(HORN)
    cfg = partfile.load(None, HORN)
    _, info = sheet.build(shape, copy.deepcopy(cfg))
    return shape, cfg, info


def centre(box):
    return np.array([(box[0] + box[2]) / 2, (box[1] + box[3]) / 2])


def texts(doc, layer=None):
    return [e.dxf.text for e in doc.modelspace().query("TEXT") if layer is None or e.dxf.layer == layer]


def test_section_added_in_app(horn):
    """A horizontal cut through the horn's neck, arrows down: seen from above, letter unused so far."""
    shape, cfg, info = horn
    cfg = copy.deepcopy(cfg)
    r = info["places"]["front"]["rect"]
    x, y = (r[0] + r[2]) / 2, r[1] + 0.45 * (r[3] - r[1])
    entry, error = edit.section(info, cfg, x, y, "down")
    assert error is None and entry["view"] == "front" and entry["look"] == "down"
    letter = entry["letter"]
    assert letter not in info["letters"]
    # a vertical cut clicked 1.5 mm beside the centre line snaps onto it (front view x = model X)
    snapped, _ = edit.section(info, cfg, x + 1.5, y, "right")
    assert snapped["point"][0] == pytest.approx(0, abs=1e-6)

    cfg["sections"] = [entry]
    doc, got = sheet.build(shape, cfg)
    label = f"{letter}–{letter}"
    assert [s["label"] for s in got["sections"]] == [info["section"], label]
    assert got["extra_views"] == info["extra_views"]  # no other letter changed
    assert texts(doc).count(letter) == 2  # at both ends of the cutting line
    assert label in texts(doc)
    rect = got["view_rects"][f"section {letter}"]
    scale = got["places"][f"section {letter}"]["scale"]
    size = geometry.envelope(shape)["size"]
    # seen from above: the flange's full X, and less than the part's Y as the flare above the cut is gone
    assert (rect[2] - rect[0]) / scale == pytest.approx(size[0], abs=0.01)
    assert (rect[3] - rect[1]) / scale < size[1] - 1
    hatches = [e for e in doc.modelspace().query("HATCH[layer=='HATCH']")
               if sheet._inside(ezdxf_box(e), rect, tol=0.5)]
    assert hatches  # the cut faces are hatched
    assert got["problems"] == []

    # a view that is not on the drawing, or a plane that misses the part: left out, said so
    cfg["sections"] = [{**entry, "view": "right"}, {**entry, "letter": "T", "point": [0, 0, 500]}]
    _, got = sheet.build(shape, cfg)
    assert [s["label"] for s in got["sections"]] == [info["section"]]
    assert any("is not on the drawing" in w for w in got["warnings"])
    assert any("misses the part" in w for w in got["warnings"])


def ezdxf_box(entity):
    import ezdxf.bbox
    b = ezdxf.bbox.extents([entity])
    return b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y


def test_callouts(horn, tmp_path):
    """A leader note, a hole callout, a dimension and a balloon, as the app makes them."""
    shape, cfg, info = horn
    cfg = copy.deepcopy(cfg)
    r = info["places"]["front"]["rect"]
    (view, point, edge), _ = edit.leader_tip(shape, info, r[0] + 10, r[1] + 0.3)  # on the flange's underside
    assert (view, edge) == ("front", True) and point[2] == pytest.approx(-48.0825, abs=1e-3)
    tip = edit.paper_xy(info["places"][view], point)
    cfg["callouts"].append(edit.callout("note", view, point, tip, tip[0] - 20, tip[1] - 12, "Machined flat", edge))

    zname = next(k for k, p in info["places"].items() if p["label"] == "view Z" and p["kind"] == "extra")
    zp = info["places"][zname]
    c6 = next(o for o in info["holes"] if o["tag"] == "C6")  # right of view Z, free space beyond
    cx, cy = edit.paper_xy(zp, c6["point"])
    (view, opening), _ = edit.opening_at(info, cx + 1, cy + 1)
    assert opening["tag"] == "C6"
    cfg["callouts"].append(edit.callout("hole", view, opening["point"], np.array([cx, cy]), cx + 22, cy + 10))

    # the ports' centres, clicked 1 mm off: they snap
    d1, d2 = (edit.paper_xy(zp, next(o for o in info["ports"] if o["tag"] == t)["point"]) for t in ("D1", "D2"))
    (dim_view, p1), _ = edit.dimension_point(info, *(d1 + 1.0))
    (_, p2), _ = edit.dimension_point(info, *(d2 - 1.0), view=dim_view)
    entry, _ = edit.dimension(info, dim_view, p1, p2, (d1[0] + d2[0]) / 2, d1[1] - 14)
    assert entry["direction"] == "horizontal"  # clicked below the two points
    cfg["callouts"].append(entry)

    (view, point, edge), _ = edit.leader_tip(shape, info, (r[0] + r[2]) / 2 + 8, r[1] + 0.75 * (r[3] - r[1]))
    assert not edge  # inside the horn's face: a dot
    tip = edit.paper_xy(info["places"][view], point)
    cfg["callouts"].append(edit.callout("balloon", view, point, tip, tip[0] + 25, tip[1] + 5,
                                        "Remove support structures", edge))

    doc, got = sheet.build(shape, copy.deepcopy(cfg))
    notes = sheet.general_notes(cfg)
    number = str(len(notes) + 2)  # after the standard notes and the hole table note
    assert {"Machined flat", "8× Ø2.2 THRU", number} <= set(texts(doc, "CALLOUTS"))
    assert f"{number}. Remove support structures" in texts(doc, "TEXT")
    dims = doc.modelspace().query("DIMENSION[layer=='CALLOUTS']")
    assert len(dims) == 1 and dims[0].get_measurement() / zp["scale"] == pytest.approx(30.0, abs=1e-6)
    callouts = doc.modelspace().query("*[layer=='CALLOUTS']")
    assert len(callouts.query("SOLID")) == 2  # arrows: the note and the hole callout
    assert len(callouts.query("HATCH")) == 1  # the balloon's dot
    assert {got["items"][f"callout {i}"]["label"] for i in range(4)} == {
        "note: Machined flat", "C6: 8× Ø2.2 THRU", "dimension 30", f"balloon {number}"}
    assert got["warnings"] == [w for w in got["warnings"] if "not confirmed" in w]
    assert got["problems"] == []

    # the entries survive the YAML the app exports
    path = tmp_path / "part.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    assert partfile.load(path, HORN)["callouts"] == cfg["callouts"]

    # a balloon may refer to an existing note by its number; a missing one is said so
    cfg["callouts"] = [{**cfg["callouts"][3], "text": "2"}, {**cfg["callouts"][3], "text": "99"}]
    doc, got = sheet.build(shape, cfg)
    assert "2" in texts(doc, "CALLOUTS")
    assert any("there is no note 99" in w for w in got["warnings"])


def test_moves(horn):
    """The automatic section, a tag, datum B and the hole table go where they are put."""
    shape, cfg, info = horn
    cfg = copy.deepcopy(cfg)
    items = info["items"]
    assert edit.item_at(info, *centre(items["tag C1"]["box"])) == "tag C1"
    assert edit.item_at(info, *centre(info["places"]["front"]["rect"])) is None  # main views stay put
    section_to = centre(items["section"]["box"]) + (40, 0)
    edit.move(cfg, info, "section", *section_to)
    edit.move(cfg, info, "tag C1", *(items["tag C1"]["anchor"] + (-8, 8)))
    edit.move(cfg, info, "datum B", *(items["datum B"]["anchor"] + (0, -14)))
    table_to = centre(items["hole table"]["box"]) - (0, 5)
    edit.move(cfg, info, "hole table", *table_to)
    assert cfg["moves"]["tag C1"] == [-8, 8] and cfg["moves"]["datum B"] == [0, -14]

    _, got = sheet.build(shape, copy.deepcopy(cfg))
    moved = got["items"]
    assert centre(got["view_rects"]["section"]) == pytest.approx(section_to, abs=0.01)
    assert centre(moved["tag C1"]["box"]) - moved["tag C1"]["anchor"] == pytest.approx((-8, 8), abs=0.01)
    assert centre(moved["datum B"]["box"]) - moved["datum B"]["anchor"] == pytest.approx((0, -14), abs=0.01)
    assert centre(moved["hole table"]["box"]) == pytest.approx(table_to, abs=0.01)
    assert got["problems"] == []

    # datum A: its triangle on the flange's underside in the left view, the letter below it
    left = got["places"]["left"]
    (view, point), error = edit.datum_a_spot(shape, got, left["rect"][0] + 3, left["rect"][1] + 0.2)
    assert error is None and view == "left"
    assert edit.datum_a_spot(shape, got, *centre(left["rect"]))[0] is None  # not on datum A's face
    spot = edit.paper_xy(left, point)
    edit.move_datum_a(cfg, got, view, point, spot[0], spot[1] - 9)
    _, got = sheet.build(shape, copy.deepcopy(cfg))
    box = got["items"]["datum A"]["box"]
    assert centre(box) == pytest.approx(spot + (0, -9), abs=0.01)
    assert got["problems"] == []


def _tube():
    """A turned part: Ø40 x 30 tube with a Ø24 bore, so the side view repeats the front view."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    axis = gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))
    return BRepAlgoAPI_Cut(BRepPrimAPI_MakeCylinder(axis, 20, 30).Shape(),
                           BRepPrimAPI_MakeCylinder(axis, 12, 30).Shape()).Shape()


def test_move_section_out_of_the_side_views_place():
    shape = _tube()
    cfg = partfile.defaults("tube.step")
    _, info = sheet.build(shape, copy.deepcopy(cfg))
    assert info["views"] == ["front", "top"] and info["section"]
    front, sec = info["view_rects"]["front"], info["view_rects"]["section"]
    assert sec[1] == pytest.approx(front[1]) and sec[0] > front[2]  # level with the front view, right of it
    to = centre(front) - (60, 0)  # to its left, clear of the front view's dimension
    edit.move(cfg, info, "section", *to)
    _, got = sheet.build(shape, cfg)
    assert centre(got["view_rects"]["section"]) == pytest.approx(to, abs=0.01)
    assert got["problems"] == []


def test_dimension_direction():
    """auto: horizontal when the dimension line is above or below the points, vertical beside them."""
    info = {"places": {"front": {"origin": [0, 0], "scale": 1.0, "frame": [[0, -1, 0], [0, 0, 1]],
                                 "rect": [0, 0, 100, 100]}}}
    p1, p2 = [10, 0, 10], [40, 0, 30]  # paper (10, 10) and (40, 30)
    kind = lambda x, y, d="auto": edit.dimension(info, "front", p1, p2, x, y, d)[0]["direction"]
    assert kind(25, 50) == "horizontal" and kind(25, 0) == "horizontal"
    assert kind(60, 20) == "vertical" and kind(0, 20) == "vertical"
    assert kind(25, 20) == "horizontal"  # between them: along the longer side
    assert kind(25, 50, "aligned") == "aligned"
    level = edit.dimension(info, "front", [10, 0, 10], [10, 0, 30], 10, 50)[0]
    assert level["direction"] == "vertical"  # nothing to measure horizontally
    assert edit.dimension(info, "front", p1, p1, 25, 50)[1]  # the same point twice: refused


def test_balloon_notes_and_section_letters():
    notes = ["Dimensions in mm.", "Stress relieved."]
    callouts = [{"type": "balloon", "text": "Remove supports"}, {"type": "note", "text": "x"},
                {"type": "balloon", "text": "Remove supports"}, {"type": "balloon", "text": "2"}]
    warnings = []
    assert sheet.balloon_notes(callouts, notes, warnings) == {0: 3, 2: 3, 3: 2}  # one note, shared
    assert notes[-1] == "Remove supports" and warnings == []
    entries, free = sheet.section_letters([{"letter": "U"}, {"view": "front"}])
    assert [e["letter"] for e in entries] == ["U", "E"]  # a missing letter comes from the far end
    assert free[:3] == ["Z", "W", "V"] and "U" not in free and "E" not in free
