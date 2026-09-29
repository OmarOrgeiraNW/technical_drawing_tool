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
   **Drawing** tab, confirm thread guesses, tolerances, finishes and position
   tolerances per hole type on the **Holes** tab, then **Regenerate**.
3. On the **Surfaces & datums** tab, mark surfaces and pick datums on the
   preview (see [Surfaces and datums](#surfaces-and-datums)).
4. On the **Sections & callouts** tab, add your own sections, notes, hole
   callouts, dimensions and balloons, and move what the tool placed (see
   [Sections, callouts and moving items](#sections-callouts-and-moving-items)).
5. **Export DXF + PDF...** writes `<part>.dxf`, `<part>.pdf` and `<part>.yaml`
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
- A full section (ISO 128-44) through the middle of the front view, seen from
  the left, so the inside of the part shows (horn flare, waveguides, feed):
  cut faces hatched at 45° (ISO 128-50, closer lines on thin walls), the
  cutting plane on the front view with thick ends, arrows and letters, and the
  view labelled e.g. `W–W`. It takes the side view's place when that view
  repeats the front view (turned parts), otherwise any free space. It is left
  out when it would show nothing new (one solid cut face); `section: none`
  turns it off.
- Centre lines where a view is mirror-symmetric, centre marks on every visible
  full circle, axis lines on visible cylinders seen from the side.
- Overall envelope dimensions, each size once (one Ø on a round view), shown as
  reference dimensions `( )` unless `reference_envelope: false`.
- Sheet: `auto` (default) takes A4 if the part fits at 1:1 or larger,
  otherwise the largest ISO 5455 scale on A3; A2 only when A3 has no room for
  the section at the drawing scale. Or choose the sheet yourself (`sheet`, or
  *Sheet* on the Drawing tab: A4, A3, A2, A1, A0) and the drawing adapts to
  it: the largest scale at which everything fits, the views placed left when
  centring them splits the free space, hole tables continued in a second or
  third column (marked "(cont.)"), extra views and the section reduced, and
  the section left out (with a warning) only when keeping it would shrink the
  main views by more than one scale step. If even that does not fit, the
  message says so and suggests a larger sheet. `scale` fixes the scale too.
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
- Rectangular waveguide ports (openings in a flat face with the inside size of
  an EIA WR waveguide, WR-430 to WR-3, sharp or rounded corners) go into the
  hole table like holes: e.g. `2× WR-19, 2.388 (X) × 4.775 (Y)` with the centre
  in X/Y and centre lines in the view. Other rectangular cut-outs (lattice
  pockets, lettering) are ignored. A tolerance set for the port type follows
  the size.
- Each hole or port type gets a letter and each hole a tag (A1, A2, ...). One
  hole table per view; equal holes share one SIZE cell with their count, e.g.
  `4× Ø1.56 THRU`.
- Equal holes equally spaced on a full circle (3 or more; a square of 4 stays
  X/Y) are one row: the pattern centre in X/Y, `PCD Ø145.39 EQS, C1 at 90°`
  (angle from +X, counter-clockwise), and the pitch circle drawn as a chain line.
- Datums (ISO 5459): A is the flat face most openings lie on, B the largest
  opening type on A (a waveguide port before a hole; one feature, or the whole
  group as a pattern datum). Their indicators (filled triangle, leader, framed
  letter) are drawn where the face shows edge-on and unhidden (largest scale
  first, the section included) and on B's outline. X/Y in every hole table run
  from datum B where it meets A, so all tables share one origin; the origin is
  marked unless it is the centre lines. `datum.A` / `B` / `C` take a model
  point to choose other features (the app does this by clicking), or `none`;
  `datum.origin` (centre | corner | [x, y, z]) overrides the origin.
- Every group gets an ISO 1101 position tolerance frame in the POSITION column:
  `⌖ Ø0.1 | A | B` for holes, `⌖ 0.05 | A` for the datum features themselves
  (a datum feature never refers to itself). Change it per type with
  `position` (empty = none). A note states that table X/Y are theoretically
  exact dimensions.
- Thread guesses compare the drill diameter with the internal-thread minor
  diameter limits (ISO 965-1 6H: M1.6-M12; ASME B1.1 2B: #1-64 to 3/8-16).
  STEP face colours (AP214/AP242) add hints: red = threaded, blue = needs a
  tolerance, green = critical finish. Guesses go into the YAML / the Holes tab
  with `confirm: true` and never reach the drawing until confirmed; then the
  table shows e.g. `M4x0.7-6H depth 7.5, drill Ø3.3 depth 10` and the thread
  gets its ISO 6410 thin 3/4 circle.
- Where hole centres come closer than 5 mm on paper, an enlarged detail view
  (ISO 128-3, e.g. `Z (5:1)`) carries their tags.
- `analyze` writes the holes, ports, datums and section to `features.json`; the
  tests compare the holes with `tests/expected/*.holes.json` and the horn's
  WR-19 ports with their positions, all checked against the cylinder axes and
  vertices in the STEP text.

## Surfaces and datums

In the app, the **Surfaces & datums** tab:

- *Surfaces not marked*: the ISO 21920-1 symbol in the title block and a note,
  `any` (basic symbol), `machined` (material removal required) or `as built`
  (material removal not permitted, e.g. LPBF surfaces left as printed).
- *Pick surface...*: choose machined or as built and an Ra value, click the
  surface in a view (on its line when it is seen edge-on, or inside it), then
  click where the symbol should go. The symbol stands on a reference line with
  a leader to the surface: an arrow on an edge-on surface, a dot on one seen
  face-on; the process is written above and the Ra below. Once marks exist the
  title block shows `(√)`: other requirements are on the views. Marks are
  listed with a Remove button.
- *Datums*: **Pick...** A and click a flat face, or B / C and click a hole or
  port (in a view or a detail view); **Auto** / **Clear** undo a pick.

Picks are saved in the YAML (`surfaces.marks`, `datum.A/B/C`) as model points,
so they follow the part when views move or the scale changes.

## Sections, callouts and moving items

The tool first draws the drawing it thinks best; on the **Sections & callouts**
tab you then add to it and rearrange it, all by clicking on the preview (Esc
cancels a pick). Each entry is listed with a Remove / Reset button.

- *Section views* (ISO 128-44): choose which way the arrows point (right, left,
  up, down: the direction the section is seen in), **Add section...** and click
  the front, top or side view (or a whole extra view) where the cutting plane
  should pass. The plane is square to that view; the click snaps to hole and
  circle centres and to the view's centre lines. The section gets a letter not
  used on the drawing yet (so no other letter changes), hatched cut faces, the
  cutting line with thick ends, arrows and letters (plus the thin chain line
  across the view when it is not on a centre line), and a place of its own: it
  is never left out for lack of room, the scale or sheet adapts instead. Looking
  right on the front view it can take the side view's place when that view
  repeats the front view. *Automatic section* switches the tool's own section
  on or off.
- *Callouts* (layer `CALLOUTS`), leaders per ISO 128-22: an arrow when you
  click on a line (an outline or a face seen edge-on), a dot when you click
  inside a face.
  - *Leader note*: type the text, click the part, then where the text goes; it
    sits on a reference line running away from the leader.
  - *Hole callout*: click a hole or port, then where the text goes. With no text
    it reads like its hole table row (e.g. `8× Ø2.2 THRU`) and follows it when
    you confirm a thread; the leader ends on the hole's outline.
  - *Dimension*: click two points (hole and circle centres, corners and lines
    snap), then where the dimension line goes: above or below the points gives a
    horizontal dimension, beside them a vertical one (or choose horizontal,
    vertical or aligned). No text shows the measured value; `<>` stands for it,
    e.g. `<> ±0.05`.
  - *Balloon*: type a note, click the part, then where the balloon goes. The
    text is added to the NOTES and the balloon shows its number (balloons with
    the same text share one note); type a number instead to point at an existing
    note.
- *Move items*: **Move...**, click an item, then where it should go: a section,
  extra, detail or isometric view, or the hole table (their centre goes to the
  click), a hole tag, a datum letter (B and C: the letter goes to the click, the
  triangle on the outline in line with it; A: first click A's face where a view
  shows it as a line, then where the letter goes) or one of your callouts (its
  text or balloon). The front, top and side views stay in projection. Moved
  items keep their size and scale and stay inside the frame; everything placed
  automatically afterwards (the isometric view, tags, datums) finds room around
  them. Text that ends up on other text, or outside the frame, is reported in
  the status bar as a layout problem.

In the YAML these are `sections` (letter, view, a model point on the plane,
`look`), `callouts` (type, view, model point(s), `at` = text offset on the paper
in mm, text) and `moves` (views and the hole table by their centre on the sheet,
tags and datum letters by their offset from the feature), so they follow the
part when the layout changes. A section or callout whose view is no longer on
the drawing is left out with a warning.

DXF layers: `VISIBLE`, `HIDDEN`, `CENTER`, `DIMS`, `TEXT`, `FRAME`, `HOLES`, `HATCH`, `SURFACES`, `CALLOUTS`. The DXF is
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
