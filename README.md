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
   left, then **Regenerate**.
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
  isometric view. The front view faces the largest flat face; override with
  `views.front` / `views.up` / `views.iso` in the YAML.
- Visible edges and silhouettes only (tangent edges and hidden lines omitted).
- Centre lines where a view is mirror-symmetric.
- Overall envelope dimensions (one Ø instead of width + height when the front
  view outline is a circle), ISO 129-1 style.
- Automatic sheet and scale: A4 if the part fits at 1:1 or larger, otherwise
  the largest ISO 5455 scale on A3. Override with `sheet` / `scale`.
- ISO 5457 frame, ISO 7200 title block with general tolerance
  (ISO 2768-mK default), default surface texture, scale and projection symbol,
  and a numbered notes block.

DXF layers: `VISIBLE`, `HIDDEN`, `CENTER`, `DIMS`, `TEXT`, `FRAME`. The DXF is
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
