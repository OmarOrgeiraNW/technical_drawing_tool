"""One test set per sample STEP file.

Expected envelopes were cross-checked against the raw STEP data (vertex
coordinates, and for the horn aperture a dense sampling of the spline faces),
and volumes against an independent mesh-based volume (within 0.1 %), not just
taken from the tool's own output.

Hole count/position checks against features.json come with hole detection.
"""

from pathlib import Path

import numpy as np
import pytest

from drawtool import geometry, partfile, sheet

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
EXPECTED = {
    "2026_09_16-Design-NW-HornQV-v01-REFERENCE.STEP": {
        "size": (58.6, 34.407, 110.33), "front": "-Y", "up": "+Z", "volume": 8034.1,
        "sheet": "A3", "scale": 1, "views": ["front", "top", "left"],
        "dims": ["(58.6)", "(110.33)", "(34.407)"],
    },
    "2026_03_26-Design-NW-Cassegrain-v03-REFERENCE.STEP": {
        "size": (291.06, 291.06, 148.9775), "front": "-Y", "up": "+Z", "volume": 103422.1,
        "sheet": "A3", "scale": 0.5, "views": ["front", "top"],  # side view repeats the front
        "dims": ["(148.9775)", "(Ø291.06)"],
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
