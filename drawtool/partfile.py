"""part.yaml: defaults, loading, and the pre-filled template written by `analyze`."""

import datetime
import json
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
        "reference_envelope": True,
        "edges": {"external": "-0.3", "internal": "+0.3"},
        "density": None,
        "views": {"front": "auto", "up": "auto", "iso": "auto", "confirm": False},
        "datum": {"A": "auto", "B": "auto", "C": None, "origin": "auto", "confirm": False},
        "section": "auto",
        "sections": [],
        "surfaces": {"default": "any", "marks": []},
        "callouts": [],
        "moves": {},
        "holes": {},
        "notes": [],
    }


FINISHES = ["any", "machined", "as built"]  # ISO 21920-1: basic symbol, material removal required / not permitted


DENSITIES = {  # g/cm3, matched against the material text (case and spaces ignored)
    "alsi10mg": 2.67, "alsi7mg": 2.67, "scalmalloy": 2.67, "6061": 2.70, "6082": 2.70,
    "7075": 2.81, "ti6al4v": 4.43, "ti-6al-4v": 4.43, "316l": 7.99, "17-4": 7.80,
    "in718": 8.19, "inconel718": 8.19, "in625": 8.44, "inconel625": 8.44, "invar": 8.05,
    "cucrzr": 8.89, "copper": 8.96, "brass": 8.47,
}


def density(cfg):
    """g/cm3 from the YAML `density`, else looked up from the material name, else None."""
    if cfg.get("density"):
        return float(cfg["density"])
    material = str(cfg["title_block"]["material"]).lower().replace(" ", "")
    return next((v for k, v in DENSITIES.items() if k in material), None)


def load(yaml_path, step_path):
    cfg = defaults(step_path)
    if yaml_path:
        user = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
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

sheet: auto                    # auto (A4, A3 or A2) | A4 | A3 | A2 | A1 | A0: the drawing adapts to it
scale: auto                    # auto | "1:1" | "1:2" | "2:1" ...
general_tolerance: "ISO 2768-mK"
default_finish: "Ra 3.2"
reference_envelope: true       # overall dimensions shown as (reference); false = toleranced
edges:                         # ISO 13715 undefined edges; empty to omit the note
  external: "-0.3"
  internal: "+0.3"
density:                       # g/cm3 for the mass; empty = from the material name

views:
  front: "{front}"                 # most detailed side view; base = largest flat face ({area:.1f} mm2)
  up: "{up}"
  iso: auto                    # or a direction such as "+X-Y+Z"
  confirm: true

datum:                         # ISO 5459 datums for the hole tables and position tolerances
  A: auto                      # auto (the flat face most holes open onto) | none | [x, y, z] on the face
  B: auto                      # auto (the largest opening on A, a waveguide port first) | none | [x, y, z]
  C:                           # optional: [x, y, z] at a second opening
  origin: auto                 # hole table origin: auto (datum B, else the centre lines where symmetric,
                               # else the lower/left edge) | centre | corner | [x, y, z] in model coordinates
  confirm: {datum_confirm}

section: auto                  # auto (through the middle, seen from the left) | none
sections: []                   # sections added in the app, e.g.
#  - {{letter: U, view: front, point: [0, 0, 20], look: down}}   # plane through point, arrows point look

surfaces:                      # ISO 21920-1 surface texture
  default: any                 # symbol in the title block: any | machined | as built
  marks: []                    # surfaces marked in the app: finish, ra, view, point, at

callouts: []                   # added in the app; point(s) on the part, at = text offset on paper (mm)
#  - {{type: note, view: front, point: [x, y, z], edge: true, at: [-15, 10], text: "Engrave P/N here"}}
#  - {{type: hole, view: top, point: [x, y, z], at: [12, 8], text: ""}}          # empty = hole table size
#  - {{type: dimension, view: front, points: [[x, y, z], [x, y, z]], direction: horizontal, at: [0, 12],
#     text: ""}}                                                                 # "<> ±0.1": <> = value
#  - {{type: balloon, view: front, point: [x, y, z], edge: false, at: [10, 10], text: "Remove supports"}}
moves: {{}}                      # items moved in the app: views and the hole table by centre on the sheet,
                               # tags and datum letters by offset from their feature

{holes}
notes: []                      # your own notes, after the standard ones, e.g.
#  - "Stress relieved after printing."
"""


def _holes_yaml(types):
    """One entry per hole type, keyed by its size so it survives geometry changes."""
    if not types:
        return "holes: {}                      # no drilled holes found\n"
    out = ["holes:                         # per hole type; thread, tolerance and finish appear on",
           "                               # the drawing only once confirm is set to false"]
    for t in types:
        spec = t["spec"]
        note = f"; {spec['note']}" if spec.get("note") else ""
        depth = spec.get("thread_depth")
        out += [f"  {json.dumps(t['key'], ensure_ascii=False)}:    # {t['letter']}: {t['count']} {t.get('kind', 'hole')}(s){note}",
                f"    thread: {json.dumps(spec.get('thread') or '', ensure_ascii=False)}",
                f"    thread_depth: {'' if depth is None else depth}",
                f"    tolerance: {json.dumps(spec.get('tolerance') or '', ensure_ascii=False)}",
                f"    finish: {json.dumps(spec.get('finish') or '', ensure_ascii=False)}",
                f"    position: {json.dumps(spec.get('position') or '', ensure_ascii=False)}    # position tolerance; empty = none",
                f"    confirm: {'true' if spec.get('confirm') else 'false'}"]
    return "\n".join(out) + "\n"


def write_template(path, step_path, features, hole_types=()):
    step_path = Path(step_path)
    face = features["planar_faces"][0] if features["planar_faces"] else {"area": 0, "normal": []}
    Path(path).write_text(TEMPLATE.format(
        name=step_path.name, step=step_path.name, yaml_name=Path(path).name, stem=step_path.stem,
        today=datetime.date.today().isoformat(), front=features["views"]["front"],
        up=features["views"]["up"], area=face["area"],
        datum_confirm="true" if hole_types else "false", holes=_holes_yaml(hole_types)),
        encoding="utf-8")
