"""One test set per sample STEP file.

Expected envelopes were cross-checked against the raw STEP data (vertex
coordinates, and for the horn aperture a dense sampling of the spline faces),
volumes against an independent mesh-based volume (within 0.1 %), and every hole
in tests/expected/*.holes.json against the cylinder axes written in the STEP
text; they are not just the tool's own output.
"""

import json
from pathlib import Path

import numpy as np
import pytest
import yaml

from drawtool import cli, geometry, holes, partfile, sheet

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
EXPECTED_HOLES = Path(__file__).resolve().parent / "expected"
EXPECTED = {
    "2026_09_16-Design-NW-HornQV-v01-REFERENCE.STEP": {
        "size": (58.6, 34.407, 110.33), "front": "-Y", "up": "+Z", "volume": 8034.1,
        "sheet": "A3", "scale": 1, "views": ["front", "top", "left"],
        "dims": ["(58.6)", "(110.33)", "(34.407)"],
        # all flange holes are seen from below; from above the horn hides some of them
        "extra_views": {"Z from below"},
        "hole_sizes": {"4× Ø1.56 THRU", "4× Ø1.7 THRU", "8× Ø2.2 THRU"},
    },
    "2026_03_26-Design-NW-Cassegrain-v03-REFERENCE.STEP": {
        "size": (291.06, 291.06, 148.9775), "front": "-Y", "up": "+Z", "volume": 103422.1,
        "sheet": "A3", "scale": 0.5, "views": ["front", "top"],  # side view repeats the front
        "dims": ["(148.9775)", "(Ø291.06)"],
        # M5 holes under the dish seen from below; the feed block's back face from behind
        "extra_views": {"Z from below", "W from behind"},
        "hole_sizes": {"4× Ø1.25 depth 6.5", "Ø2 THRU", "8× Ø4.2 depth 8.68"},
    },
}


@pytest.fixture(scope="module", params=sorted(EXPECTED))
def sample(request):
    path = SAMPLES / request.param
    return path, geometry.load_step(path), EXPECTED[request.param]


def test_envelope_and_orientation(sample):
    _, shape, exp = sample
    assert geometry.envelope(shape)["size"] == pytest.approx(exp["size"], abs=1e-3)
    assert geometry.guess_orientation(shape) == (exp["front"], exp["up"])


def test_drawing(sample, tmp_path):
    path, shape, exp = sample
    cfg = partfile.load(None, path)
    cfg["title_block"]["material"] = "AlSi10Mg (LPBF)"
    doc, info = sheet.build(shape, cfg)
    assert (info["sheet"], info["scale"]) == (exp["sheet"], exp["scale"])
    assert info["views"] == exp["views"]
    assert [value for _, value in info["dims"]] == exp["dims"]
    assert info["mass_g"] == pytest.approx(exp["volume"] * 2.67 / 1000, rel=1e-4)
    assert set(info["extra_views"]) == exp["extra_views"]
    texts = {e.dxf.text for e in doc.modelspace() if e.dxftype() == "TEXT"}
    assert exp["hole_sizes"] <= texts  # equal holes grouped under one size with their count
    assert info["problems"] == []
    doc.saveas(tmp_path / "part.dxf")
    assert sheet.render(doc, info["sheet"], "pdf")[:4] == b"%PDF"


def test_first_angle_frames_share_axes():
    frames = geometry.view_frames("-Y", "+Z")
    right = {name: np.cross(y, d) for name, (d, y) in frames.items()}
    assert np.allclose(right["top"], right["front"])  # top view sits below, same x
    assert np.allclose(frames["left"][1], frames["front"][1])  # left view sits right, same y
    assert np.allclose(right["left"], frames["front"][0])  # front face points right in the left view


@pytest.mark.parametrize("size, fields", [("A4", (6, 4)), ("A3", (8, 6))])
def test_grid_reference_fields(size, fields):
    """ISO 5457: A4 landscape has 6 x 4 fields, A3 8 x 6."""
    w, h = sheet.SHEETS[size]
    cols = sheet._fields(w / 2, sheet.LEFT, w - sheet.RIGHT)
    rows = sheet._fields(h / 2, sheet.BOTTOM, h - sheet.TOP)
    assert (len(cols) - 1, len(rows) - 1) == fields


def test_density_from_material():
    cfg = partfile.defaults("x.step")
    cfg["title_block"]["material"] = "Ti-6Al-4V grade 23"
    assert partfile.density(cfg) == 4.43
    cfg["density"] = "2.7"
    assert partfile.density(cfg) == 2.7


def test_holes_match_features_json(sample, tmp_path):
    """analyze finds every hole: count, type and entry position as in the expected list."""
    path, _, _ = sample
    cli.analyze(path, tmp_path)
    found = json.loads((tmp_path / f"{path.stem}.features.json").read_text(encoding="utf-8"))["holes"]
    expected = json.loads((EXPECTED_HOLES / f"{path.stem}.holes.json").read_text(encoding="utf-8"))
    assert len(found) == len(expected)
    for want in expected:
        got = next(h for h in found if h["tag"] == want["tag"])
        assert got["type"] == want["type"]
        assert got["view"] == want["view"]  # a view in which the hole's opening is visible
        assert got["through"] == want["through"]
        assert got["point"] == pytest.approx(want["point"], abs=0.01)
        assert got["axis"] == pytest.approx(want["axis"], abs=1e-3)
    # the YAML template lists every hole type, with guesses waiting for confirmation
    cfg = yaml.safe_load((tmp_path / f"{path.stem}.yaml").read_text(encoding="utf-8"))
    assert set(cfg["holes"]) == {h["type"] for h in expected}


def _coloured_plate(path):
    """40 x 30 x 8 plate written as AP214: two blind Ø3.3 x 6 holes with red faces,
    a Ø3.4 through hole with a Ø6.5 x 3.4 counterbore and a plain Ø2.2 through hole."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.GeomAbs import GeomAbs_Cylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_AsIs
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopoDS import TopoDS
    from OCP.XCAFDoc import XCAFDoc_ColorSurf, XCAFDoc_DocumentTool

    shape = BRepPrimAPI_MakeBox(40, 30, 8).Shape()
    for x, y, z0, r, h in ((10, 10, 2, 1.65, 7), (30, 10, 2, 1.65, 7), (20, 20, -1, 1.7, 10),
                           (20, 20, 4.6, 3.25, 5), (5, 25, -1, 1.1, 10)):
        tool = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, z0), gp_Dir(0, 0, 1)), r, h).Shape()
        shape = BRepAlgoAPI_Cut(shape, tool).Shape()
    doc = TDocStd_Document(TCollection_ExtendedString("XCAF"))
    shapes = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    colours = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    label = shapes.AddShape(shape, False)
    red = Quantity_Color(1, 0, 0, Quantity_TOC_RGB)
    for face in geometry._explore(shape, TopAbs_FACE, TopoDS.Face):
        surf = BRepAdaptor_Surface(face)
        if surf.GetType() == GeomAbs_Cylinder and abs(surf.Cylinder().Radius() - 1.65) < 1e-6:
            colours.SetColor(shapes.AddSubShape(label, face), red, XCAFDoc_ColorSurf)
    writer = STEPCAFControl_Writer()
    writer.SetColorMode(True)
    writer.Transfer(doc, STEPControl_AsIs)
    writer.Write(str(path))


def test_coloured_plate(tmp_path):
    path = tmp_path / "plate.step"
    _coloured_plate(path)
    shape = geometry.load_step(path)
    doc, info = sheet.build(shape, partfile.load(None, path))
    types = {t["key"]: t for t in info["hole_types"]}
    assert set(types) == {"Ø2.20 THRU", "Ø3.30 depth 6.00", "Ø3.40 THRU cbore Ø6.50 depth 3.40"}
    red = types["Ø3.30 depth 6.00"]
    assert red["count"] == 2
    assert red["spec"]["thread"] == "M4x0.7-6H" and red["spec"]["confirm"]  # guessed, not accepted
    assert "red faces" in red["spec"]["note"]
    assert {h["type"]: h["colours"] for h in info["holes"]}["Ø3.30 depth 6.00"] == ["red"]
    assert all(h["view"] == "top" for h in info["holes"])
    assert info["extra_views"] == []
    assert info["problems"] == []


def test_thread_guess_needs_confirmation():
    hole = {"diameter": 3.3, "through": False, "pitch": None, "colours": []}
    entry = {"depth": 10.0, "cbore": None, "csk": None}
    spec = holes.guess(hole, entry)
    assert spec["thread"] == "M4x0.7-6H" and spec["confirm"]
    assert holes.size_text(hole, entry, spec) == "Ø3.3 depth 10"  # guess not shown
    spec["confirm"] = False
    assert holes.size_text(hole, entry, spec) == "M4x0.7-6H depth 7.5, drill Ø3.3 depth 10"
    assert holes.thread_candidates(4.2, 0.8) == ["M5x0.8-6H"]  # a modelled pitch pins it down
