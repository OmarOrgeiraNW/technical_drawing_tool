"""drawtool analyze part.step  |  drawtool draw part.step part.yaml"""

import argparse
import json
import sys
from pathlib import Path

from . import geometry, partfile, sheet


def analyze(step, out):
    shape = geometry.load_step(step)
    front, up = geometry.guess_orientation(shape)
    features = {
        "source": Path(step).name,
        "units": "mm",
        "envelope": geometry.envelope(shape),
        "volume_mm3": round(geometry.volume(shape), 1),
        "planar_faces": geometry.planar_faces(shape),
        "views": {"front": front, "up": up},
    }
    stem = Path(step).stem
    (out / f"{stem}.features.json").write_text(json.dumps(features, indent=2))
    yaml_path = out / f"{stem}.yaml"
    if yaml_path.exists():
        print(f"kept existing {yaml_path} (delete it to regenerate)")
    else:
        partfile.write_template(yaml_path, step, features)
    cfg = partfile.load(yaml_path, step)
    doc, info = sheet.build(shape, cfg)
    (out / f"{stem}.preview.png").write_bytes(sheet.render(doc, info["sheet"], "png", dpi=100))
    print(f"wrote {stem}.features.json, {yaml_path.name}, {stem}.preview.png to {out}")


def draw(step, yaml_path, out):
    cfg = partfile.load(yaml_path, step)
    if cfg["views"].get("confirm"):
        print("warning: views.confirm is still true - check the view orientation", file=sys.stderr)
    doc, info = sheet.build(geometry.load_step(step), cfg)
    stem = Path(step).stem
    doc.saveas(out / f"{stem}.dxf")
    (out / f"{stem}.pdf").write_bytes(sheet.render(doc, info["sheet"], "pdf"))
    (out / f"{stem}.png").write_bytes(sheet.render(doc, info["sheet"], "png"))
    print(f"wrote {stem}.dxf/.pdf/.png to {out}  ({info['sheet']}, scale {sheet.fmt_scale(info['scale'])}, "
          f"front {info['front']}, up {info['up']})")
    for p in info["problems"]:
        print(f"layout problem: {p}", file=sys.stderr)


def main(argv=None):
    p = argparse.ArgumentParser(prog="drawtool", description="STEP part -> ISO 2D drawing (PDF + DXF)")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="write features.json, part.yaml template and preview.png")
    a.add_argument("step")
    a.add_argument("-o", "--out", default=".")
    d = sub.add_parser("draw", help="write the drawing as DXF, PDF and PNG")
    d.add_argument("step")
    d.add_argument("yaml", nargs="?")
    d.add_argument("-o", "--out", default=".")
    args = p.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.cmd == "analyze":
        analyze(args.step, out)
    else:
        draw(args.step, args.yaml, out)


if __name__ == "__main__":
    main()
