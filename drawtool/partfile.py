"""part.yaml: defaults, loading, and the pre-filled template written by `analyze`."""

import datetime
from pathlib import Path

import yaml


def defaults(step_path):
    stem = Path(step_path).stem
    return {
        "title_block": {
            "title": stem,
            "part_number": stem,
            "revision": "A",
            "material": "",
            "drawn_by": "",
            "approved_by": "",
            "date": datetime.date.today().isoformat(),
            "company": "",
            "department": "",
            "technical_reference": "",
            "document_type": "Detail drawing",
            "status": "Draft",
            "sheet": "1/1",
        },
        "sheet": "auto",
        "scale": "auto",
        "general_tolerance": "ISO 2768-mK",
        "default_finish": "Ra 3.2",
        "views": {"front": "auto", "up": "auto", "iso": "auto", "confirm": False},
        "notes": [],
    }


def load(yaml_path, step_path):
    cfg = defaults(step_path)
    if yaml_path:
        user = yaml.safe_load(Path(yaml_path).read_text()) or {}
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    for key in ("date", "revision", "part_number", "title"):
        cfg["title_block"][key] = str(cfg["title_block"][key] or "")
    return cfg


TEMPLATE = """\
# Drawing definition for {name}
# Written by `drawtool analyze`. Edit, then run: drawtool draw {step} {yaml_name}
# Entries marked `confirm: true` are guesses: check them and set confirm: false.

title_block:
  title: "{stem}"
  part_number: "{stem}"
  revision: "A"
  material: ""                 # e.g. "AlSi10Mg (LPBF), stress relieved"
  drawn_by: ""
  approved_by: ""
  date: "{today}"
  company: ""                  # legal owner
  department: ""
  technical_reference: ""
  document_type: "Detail drawing"
  status: "Draft"
  sheet: "1/1"

sheet: auto                    # auto | A4 | A3
scale: auto                    # auto | "1:1" | "1:2" | "2:1" ...
general_tolerance: "ISO 2768-mK"
default_finish: "Ra 3.2"

views:
  front: "{front}"                 # looks at the largest flat face ({area:.1f} mm2, normal {normal})
  up: "{up}"
  iso: auto                    # or a direction such as "+X-Y+Z"
  confirm: true

notes:
  - "Remove all burrs and sharp edges."
"""


def write_template(path, step_path, features):
    step_path = Path(step_path)
    face = features["planar_faces"][0] if features["planar_faces"] else {"area": 0, "normal": []}
    Path(path).write_text(TEMPLATE.format(
        name=step_path.name, step=step_path.name, yaml_name=Path(path).name, stem=step_path.stem,
        today=datetime.date.today().isoformat(), front=features["views"]["front"],
        up=features["views"]["up"], area=face["area"], normal=face["normal"]))
