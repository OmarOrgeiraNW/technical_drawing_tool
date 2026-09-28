"""One test set per sample STEP file.

Expected envelopes were cross-checked against the raw STEP data (vertex
coordinates, and for the horn aperture a dense sampling of the spline faces),
not just taken from the tool's own output.

Hole count/position checks against features.json come with hole detection.
"""

from pathlib import Path

import numpy as np
import pytest

from drawtool import geometry, partfile, sheet

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
EXPECTED = {
    "2026_09_16-Design-NW-HornQV-v01-REFERENCE.STEP": {
        "size": (58.6, 34.407, 110.33), "front": "-Z", "up": "+Y",
        "sheet": "A4", "scale": 1, "dims": ["58.6", "34.407", "110.33"],
    },
    "2026_03_26-Design-NW-Cassegrain-v03-REFERENCE.STEP": {
        "size": (291.06, 291.06, 148.9775), "front": "+Z", "up": "+Y",
        "sheet": "A3", "scale": 0.5, "dims": ["Ø291.06", "148.9775"],
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
    doc, info = sheet.build(shape, partfile.load(None, path))
    assert (info["sheet"], info["scale"]) == (exp["sheet"], exp["scale"])
    assert [value for _, value in info["dims"]] == exp["dims"]
    assert info["problems"] == []
    doc.saveas(tmp_path / "part.dxf")
    assert sheet.render(doc, info["sheet"], "pdf")[:4] == b"%PDF"


def test_first_angle_frames_share_axes():
    frames = geometry.view_frames("-Y", "+Z")
    right = {name: np.cross(y, d) for name, (d, y) in frames.items()}
    assert np.allclose(right["top"], right["front"])  # top view sits below, same x
    assert np.allclose(frames["left"][1], frames["front"][1])  # left view sits right, same y
    assert np.allclose(right["left"], frames["front"][0])  # front face points right in the left view
