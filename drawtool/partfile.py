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
        "datum": {"origin": "auto", "confirm": False},
        "holes": {},
        "notes": [],
    }


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

sheet: auto                    # auto | A4 | A3
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

datum:
  origin: auto                 # hole table origin: auto (centre lines where symmetric, else the
                               # lower/left edge) | centre | corner | [x, y, z] in model coordinates
  confirm: {datum_confirm}

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
        out += [f"  {json.dumps(t['key'], ensure_ascii=False)}:    # {t['letter']}: {t['count']} hole(s){note}",
                f"    thread: {json.dumps(spec.get('thread') or '')}",
                f"    thread_depth: {'' if depth is None else depth}",
                f"    tolerance: {json.dumps(spec.get('tolerance') or '')}",
                f"    finish: {json.dumps(spec.get('finish') or '')}",
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
