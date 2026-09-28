# drawtool

Turns a STEP file of a single machined / LPBF part into an ISO 2D manufacturing
drawing (DXF + PDF, plus a PNG for quick checks).

## Windows app

Download **DrawTool-windows** from the latest successful
[Windows exe](../../actions/workflows/windows-exe.yml) run, unzip it and start
`DrawTool.exe` (keep it next to its `_internal` folder).

1. **Open STEP...** loads the part and generates the drawing (the Cassegrain
   sample takes ~30 s, smaller parts a few seconds).
2. Adjust the view orientation, sheet, scale, title block and notes on the
   **Drawing** tab, and confirm thread guesses, tolerances and finishes per hole
   type on the **Holes** tab, then **Regenerate**.
3. **Export DXF + PDF...** writes `<part>.dxf`, `<part>.pdf` and `<part>.yaml`
   to a folder. The YAML holds your settings: keep it next to the STEP file and
   they are loaded automatically the next time you open that part.

Preview: mouse wheel zooms, drag pans, double-click fits the sheet.
The exe also runs the command line: `DrawTool.exe draw part.step part.yaml -o out`.

To build it yourself on Windows: `pip install . pyinstaller` then
`pyinstaller packaging/DrawTool.spec` (output in `dist/DrawTool`).

## Command line

```sh
pip install -e ".[test]"
drawtool analyze part.step -o out        # out/part.features.json, out/part.yaml, out/part.preview.png
# edit out/part.yaml (title block, orientation, notes)
drawtool draw part.step out/part.yaml -o out   # out/part.dxf, out/part.pdf, out/part.png
pytest
```

## What v1 draws

- Front, top and left views in ISO first-angle arrangement (ISO 5456-2) plus an
  isometric view. The largest flat face becomes the base when it covers a good
  part of the footprint (a flange); the front view is the most detailed of the
  side views (ISO 128-3). A side view that repeats the front view (turned
  parts) is left out. Override with `views.front` / `views.up` / `views.iso`.
- Visible edges and silhouettes only (tangent edges and hidden lines omitted).
- Centre lines where a view is mirror-symmetric, centre marks on every visible
  full circle, axis lines on visible cylinders seen from the side.
- Overall envelope dimensions, each size once (one Ø on a round view), shown as
  reference dimensions `( )` unless `reference_envelope: false`.
- Automatic sheet and scale: A4 if the part fits at 1:1 or larger, otherwise
  the largest ISO 5455 scale on A3. Override with `sheet` / `scale`.
- ISO 5457 frame with grid reference zones, ISO 7200 title block with mass
  (volume x density; density from `density` or the material name), general
  tolerance (ISO 2768-mK default), default surface texture, scale and
  projection symbol.
- Notes: "Dimensions in mm", the ISO 13715 edge condition (`edges`, default
  external -0.3 / internal +0.3), then the part's own notes.

## Holes

- Found in the solid: coaxial concave cylinders with their counterbores,
  countersinks and drill points; through or blind is decided by probing past
  each end, and a hole must open onto a flat face (so waveguide sections inside
  the part are not holes). Modelled threads are recognised by their pitch.
- Holes are marked only in a view where their opening is directly visible.
  Equal holes (same type) stay together in the first view that shows all of
  them. When none of the front, top and left views shows them, an extra view is
  added (ISO 128-3 reference-arrow method: a letter above the view and an arrow
  with the same letter on a main view): the whole view from below, the right or
  behind, or an enlarged partial view when the holes sit in a small area.
- Each hole type gets a letter and each hole a tag (A1, A2, ...). One hole
  table per view; equal holes share one SIZE cell with their count, e.g.
  `4× Ø1.56 THRU`. X/Y run from the centre lines when the view is symmetric,
  otherwise from an origin marked in the view (`datum.origin`:
  auto | centre | corner | [x, y, z]).
- Thread guesses compare the drill diameter with the internal-thread minor
  diameter limits (ISO 965-1 6H: M1.6-M12; ASME B1.1 2B: #1-64 to 3/8-16).
  STEP face colours (AP214/AP242) add hints: red = threaded, blue = needs a
  tolerance, green = critical finish. Guesses go into the YAML / the Holes tab
  with `confirm: true` and never reach the drawing until confirmed; then the
  table shows e.g. `M4x0.7-6H depth 7.5, drill Ø3.3 depth 10` and the thread
  gets its ISO 6410 thin 3/4 circle.
- Where hole centres come closer than 5 mm on paper, an enlarged detail view
  (ISO 128-3, e.g. `Z (5:1)`) carries their tags.
- `analyze` writes the holes to `features.json`; the tests compare them with
  `tests/expected/*.holes.json`, which were checked against the cylinder axes in
  the STEP text.

DXF layers: `VISIBLE`, `HIDDEN`, `CENTER`, `DIMS`, `TEXT`, `FRAME`, `HOLES`. The DXF is
drawn at sheet size in mm (views scaled); dimensions carry DIMLFAC so they read
true part size. The PDF and PNG are rendered from the DXF with ezdxf.

## Notes

- Geometry comes straight from OpenCascade (OCP); the bundled DejaVu Sans font
  keeps text metrics identical on every OS.
- Projection uses OpenCascade's polygonal HLR (`HLRBRep_PolyAlgo`): about 1 s per
  view on 2000+ face parts where the exact algorithm takes minutes.
- `draw` prints any layout problems it detects (overlapping text, text on a
  view, anything outside the frame); the tests require there are none.
- STEP face colours (red = threaded, blue = toleranced, green = critical finish)
  are only carried by AP214/AP242 exports; the sample files are AP203.
