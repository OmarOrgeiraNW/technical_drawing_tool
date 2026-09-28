"""Desktop window: open a STEP file, preview the drawing, export DXF + PDF.

The packaged DrawTool.exe starts this window. Given `analyze`/`draw` arguments
it behaves like the command-line tool instead.
"""

import copy
import queue
import sys
import threading
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import pymupdf
import yaml

from . import cli, geometry, partfile, sheet

DIRECTIONS = ["auto", "+X", "-X", "+Y", "-Y", "+Z", "-Z"]
SHEETS = ["auto", "A4", "A3", "A2", "A1", "A0"]
SCALES = ["auto", "5:1", "2:1", "1:1", "1:2", "1:5", "1:10"]
ORIGINS = ["auto", "centre", "corner"]
TITLE_FIELDS = [("title", "Title"), ("part_number", "Part number"), ("revision", "Revision"),
                ("material", "Material"), ("drawn_by", "Created by"), ("approved_by", "Approved by"),
                ("company", "Legal owner"), ("date", "Date of issue"), ("status", "Status")]


class Preview(tk.Canvas):
    """PDF page viewer: mouse wheel zooms at the cursor, drag pans, double-click fits."""

    def __init__(self, master):
        super().__init__(master, background="#80858c", highlightthickness=0)
        self.page = None
        self.zoom, self.ox, self.oy = 1.0, 0.0, 0.0
        self._image = None
        self._last = self._press = (0, 0)
        self.on_click = None  # set while picking: called with the page position (0..1 from top left)
        self.bind("<Configure>", lambda e: self.fit())
        self.bind("<MouseWheel>", lambda e: self.zoom_at(e.x, e.y, 1.25 if e.delta > 0 else 0.8))
        self.bind("<Button-4>", lambda e: self.zoom_at(e.x, e.y, 1.25))  # Linux wheel
        self.bind("<Button-5>", lambda e: self.zoom_at(e.x, e.y, 0.8))
        self.bind("<ButtonPress-1>", self._grab)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Double-Button-1>", lambda e: self.fit())

    def _release(self, event):
        """A click (press and release without dragging) while picking."""
        if self.on_click and self.page and abs(event.x - self._press[0]) + abs(event.y - self._press[1]) < 4:
            r = self.page.rect
            self.on_click((event.x - self.ox) / self.zoom / r.width, (event.y - self.oy) / self.zoom / r.height)

    def show(self, pdf_bytes):
        self.page = pymupdf.open("pdf", pdf_bytes)[0]
        self.fit()

    def fit(self):
        if not self.page:
            return
        w, h, r = self.winfo_width(), self.winfo_height(), self.page.rect
        self.zoom = max(0.05, min((w - 24) / r.width, (h - 24) / r.height))
        self.ox, self.oy = (w - r.width * self.zoom) / 2, (h - r.height * self.zoom) / 2
        self.redraw()

    def zoom_at(self, x, y, factor):
        if not self.page:
            return
        factor = max(0.05, min(30.0, self.zoom * factor)) / self.zoom
        self.zoom *= factor
        self.ox, self.oy = x - (x - self.ox) * factor, y - (y - self.oy) * factor
        self.redraw()

    def _grab(self, event):
        self._last = self._press = (event.x, event.y)

    def _drag(self, event):
        self.ox += event.x - self._last[0]
        self.oy += event.y - self._last[1]
        self._last = (event.x, event.y)
        self.redraw()

    def redraw(self):
        """Rasterise only the visible part of the page at the current zoom."""
        self.delete("all")
        if not self.page:
            return
        z, w, h = self.zoom, self.winfo_width(), self.winfo_height()
        visible = pymupdf.Rect(-self.ox / z, -self.oy / z, (w - self.ox) / z, (h - self.oy) / z)
        visible &= self.page.rect
        if visible.is_empty:
            return
        pix = self.page.get_pixmap(matrix=pymupdf.Matrix(z, z), clip=visible)
        self._image = tk.PhotoImage(data=pix.tobytes("ppm"))
        self.create_image(self.ox + visible.x0 * z, self.oy + visible.y0 * z, image=self._image,
                          anchor="nw")


class App:
    def __init__(self, root):
        self.root = root
        root.title("drawtool - STEP to ISO drawing")
        root.geometry("1400x900")
        self.step = self.shape = self.doc = self.info = self.pdf = self.built_cfg = None
        self.cfg = None
        self.jobs = queue.Queue()

        bar = ttk.Frame(root, padding=6)
        bar.pack(fill="x")
        self.buttons = [
            ttk.Button(bar, text="Open STEP...", command=self.open_step),
            ttk.Button(bar, text="Regenerate", command=self.generate, state="disabled"),
            ttk.Button(bar, text="Export DXF + PDF...", command=self.export, state="disabled"),
        ]
        for b in self.buttons:
            b.pack(side="left", padx=(0, 6))
        self.file_label = ttk.Label(bar, text="No STEP file loaded")
        self.file_label.pack(side="left", padx=12)

        body = ttk.Panedwindow(root, orient="horizontal")
        body.pack(fill="both", expand=True)
        self.tabs = tabs = ttk.Notebook(body)
        form = ttk.Frame(tabs, padding=10)
        holes_tab = ttk.Frame(tabs, padding=(10, 10, 0, 10))
        marks_tab = ttk.Frame(tabs, padding=10)
        tabs.add(form, text="Drawing")
        tabs.add(holes_tab, text="Holes")
        tabs.add(marks_tab, text="Surfaces & datums")
        self.preview = Preview(body)
        body.add(tabs, weight=0)
        body.add(self.preview, weight=1)
        self._build_form(form)
        self.hole_list = _scrollable(holes_tab)
        self.hole_vars = {}
        self.picking = None
        self._build_marks(marks_tab)

        self.status = ttk.Label(root, text="Open a STEP file to start.", anchor="w", padding=(8, 4))
        self.status.pack(fill="x")
        ttk.Label(root, text="Preview: mouse wheel = zoom, drag = pan, double-click = fit",
                  anchor="w", padding=(8, 0, 8, 4), foreground="#555").pack(fill="x")
        root.after(100, self._poll)

    # ------------------------------------------------------------ form

    def _build_form(self, form):
        self.vars = {}
        row = 0

        def heading(text):
            nonlocal row
            ttk.Label(form, text=text, font=("TkDefaultFont", 10, "bold")).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(10 if row else 0, 4))
            row += 1

        def field(key, label, values=None):
            nonlocal row
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
            var = tk.StringVar()
            if values:
                w = ttk.Combobox(form, textvariable=var, values=values, state="readonly", width=10)
            else:
                w = ttk.Entry(form, textvariable=var, width=30)
            w.grid(row=row, column=1, sticky="we", pady=2)
            self.vars[key] = var
            row += 1

        heading("Views")
        field("front", "Front view looks at", DIRECTIONS)
        field("up", "Up direction", DIRECTIONS)
        field("sheet", "Sheet", SHEETS)
        field("scale", "Scale", SCALES)
        field("datum_origin", "Hole table origin", ORIGINS)
        heading("Title block")
        for key, label in TITLE_FIELDS:
            field(key, label)
        field("general_tolerance", "General tolerances")
        field("default_finish", "Surface texture")
        field("density", "Density g/cm3 (blank = auto)")
        field("edge_external", "Edges ISO 13715 external")
        field("edge_internal", "Edges ISO 13715 internal")
        self.reference = tk.BooleanVar()
        ttk.Checkbutton(form, text="Overall dimensions as (reference)", variable=self.reference).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=2)
        row += 1
        heading("Notes (one per line)")
        self.notes = tk.Text(form, width=40, height=6, wrap="word")
        self.notes.grid(row=row, column=0, columnspan=2, sticky="we")
        form.columnconfigure(1, weight=1)

    def _build_marks(self, tab):
        """Surface texture marks and datums, both picked on the preview."""
        bold = ("TkDefaultFont", 10, "bold")
        ttk.Label(tab, text="Surface texture (ISO 21920-1)", font=bold).grid(row=0, column=0, columnspan=4,
                                                                             sticky="w")
        ttk.Label(tab, text="Surfaces not marked").grid(row=1, column=0, sticky="w", pady=2)
        self.surface_default = tk.StringVar(value="any")
        ttk.Combobox(tab, textvariable=self.surface_default, values=partfile.FINISHES, state="readonly",
                     width=10).grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(tab, text="New mark").grid(row=2, column=0, sticky="w", pady=(8, 2))
        self.mark_finish = tk.StringVar(value="machined")
        ttk.Combobox(tab, textvariable=self.mark_finish, values=partfile.FINISHES[1:], state="readonly",
                     width=10).grid(row=2, column=1, sticky="w", padx=4, pady=(8, 2))
        self.mark_ra = tk.StringVar(value="Ra 1.6")
        ttk.Entry(tab, textvariable=self.mark_ra, width=10).grid(row=2, column=2, sticky="w", padx=4, pady=(8, 2))
        ttk.Button(tab, text="Pick surface...", command=lambda: self.start_pick("mark")).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Label(tab, text="Click the surface in a view (its line, or inside it), then click\n"
                            "where the symbol and its text should go.", foreground="#555").grid(
            row=4, column=0, columnspan=4, sticky="w")
        self.mark_list = ttk.Frame(tab)
        self.mark_list.grid(row=5, column=0, columnspan=4, sticky="we", pady=(6, 0))
        ttk.Label(tab, text="Datums (ISO 5459)", font=bold).grid(row=6, column=0, columnspan=4, sticky="w",
                                                                pady=(16, 2))
        ttk.Label(tab, text="A: a flat face.  B, C: a hole or waveguide port (click it).\n"
                            "Hole tables measure from B; position tolerances refer to A|B(|C).",
                  foreground="#555").grid(row=7, column=0, columnspan=4, sticky="w")
        self.datum_labels = {}
        for i, letter in enumerate("ABC"):
            ttk.Label(tab, text=letter, font=bold).grid(row=8 + i, column=0, sticky="w", pady=2)
            self.datum_labels[letter] = ttk.Label(tab, text="auto", width=14)
            self.datum_labels[letter].grid(row=8 + i, column=1, sticky="w")
            ttk.Button(tab, text="Pick...", command=lambda k=letter: self.start_pick(k)).grid(
                row=8 + i, column=2, sticky="w", padx=4)
            ttk.Button(tab, text="Auto" if letter != "C" else "Clear",
                       command=lambda k=letter: self.set_datum(k, "auto" if k != "C" else None)).grid(
                row=8 + i, column=3, sticky="w")

    def fill_marks(self):
        for child in self.mark_list.winfo_children():
            child.destroy()
        surfaces = self.cfg.setdefault("surfaces", {"default": "any", "marks": []})
        for i, m in enumerate(surfaces.get("marks") or []):
            text = f"{i + 1}. {m.get('finish')} {m.get('ra') or ''} - {m.get('view')} view"
            ttk.Label(self.mark_list, text=text).grid(row=i, column=0, sticky="w")
            ttk.Button(self.mark_list, text="Remove", command=lambda k=i: self.remove_mark(k)).grid(
                row=i, column=1, sticky="w", padx=6)
        datum = self.cfg.get("datum") or {}
        tags = (self.info or {}).get("datums", {})
        for letter, label in self.datum_labels.items():
            value = datum.get(letter, "auto" if letter != "C" else None)
            if isinstance(value, (list, tuple)):
                text = "picked"
            else:
                text = "none" if value in (None, "none") else "auto"
            if letter in tags and letter != "A":
                text += f" ({', '.join(tags[letter]['tags'])})"
            label.config(text=text)

    def remove_mark(self, index):
        del self.cfg["surfaces"]["marks"][index]
        self.fill_marks()
        self.generate()

    def set_datum(self, letter, value):
        self.cfg.setdefault("datum", {})[letter] = value
        self.fill_marks()
        self.generate()

    # ------------------------------------------------------------ picking on the preview

    def start_pick(self, what):
        if not self.info or self.shape is None:
            self.set_status("Generate the drawing first.")
            return
        self.picking = {"what": what}
        self.preview.on_click = self.on_pick
        self.preview.config(cursor="crosshair")
        self.set_status({"mark": "Click the surface in a view...",
                         "A": "Click datum A's flat face in a view (its line, or inside it)...",
                         "B": "Click the hole or port that is datum B...",
                         "C": "Click the hole or port that is datum C..."}[what] + "   (Esc cancels)")
        self.root.bind("<Escape>", lambda e: self.end_pick("Picking cancelled."))

    def end_pick(self, message=None):
        self.picking = None
        self.preview.on_click = None
        self.preview.config(cursor="")
        self.root.unbind("<Escape>")
        if message:
            self.set_status(message)

    def place_at(self, x, y, details=True):
        """(name, place) of the view under a paper point; details first (they are enlargements)."""
        hits = [(name, p) for name, p in self.info["places"].items()
                if p["rect"][0] - 2 <= x <= p["rect"][2] + 2 and p["rect"][1] - 2 <= y <= p["rect"][3] + 2
                and (details or "parent" not in p)]
        hits.sort(key=lambda h: ("parent" not in h[1], (h[1]["rect"][2] - h[1]["rect"][0]) *
                                 (h[1]["rect"][3] - h[1]["rect"][1])))
        return hits[0] if hits else (None, None)

    @staticmethod
    def _to_view(place, x, y):
        return (x - place["origin"][0]) / place["scale"], (y - place["origin"][1]) / place["scale"]

    @staticmethod
    def _to_paper(place, point):
        d, up = place["frame"]
        right = [up[1] * d[2] - up[2] * d[1], up[2] * d[0] - up[0] * d[2], up[0] * d[1] - up[1] * d[0]]
        vx = sum(a * b for a, b in zip(point, right))
        vy = sum(a * b for a, b in zip(point, up))
        return place["origin"][0] + vx * place["scale"], place["origin"][1] + vy * place["scale"]

    def on_pick(self, fx, fy):
        w, h = sheet.SHEETS[self.info["sheet"]]
        x, y = fx * w, (1 - fy) * h  # paper mm
        p = self.picking
        if p["what"] == "mark" and "point" in p:  # second click: where the symbol goes
            tx, ty = p["tip"]
            self.cfg.setdefault("surfaces", {}).setdefault("marks", []).append(
                {"finish": self.mark_finish.get(), "ra": self.mark_ra.get().strip(), "view": p["view"],
                 "point": p["point"], "edge": p["edge"], "at": [round(x - tx, 2), round(y - ty, 2)]})
            self.end_pick()
            self.fill_marks()
            self.generate()
            return
        name, place = self.place_at(x, y, details=p["what"] in ("B", "C"))
        if place is None:
            self.set_status("That is not on a view: click inside a view (Esc cancels).")
            return
        if p["what"] in ("B", "C"):
            openings = [o for o in self.info["holes"] + self.info["ports"] if o["view"] == place["label"]]
            near = sorted(((x - px) ** 2 + (y - py) ** 2, o) for o in openings
                          for px, py in [self._to_paper(place, o["point"])])
            if not near or near[0][0] > 8 ** 2:
                self.set_status("No hole or port there: click closer to one (Esc cancels).")
                return
            opening = near[0][1]
            self.cfg.setdefault("datum", {})[p["what"]] = [float(v) for v in opening["point"]]
            self.end_pick(f"Datum {p['what']}: {opening['tag']}")
            self.fill_marks()
            self.generate()
            return
        hit = geometry.pick(self.shape, place["frame"], self._to_view(place, x, y), 0.8 / place["scale"],
                            cut=place.get("cut"))
        if hit is None:
            self.set_status("No surface there: click on the part (Esc cancels).")
            return
        point = [round(float(v), 4) for v in hit[0]]
        if p["what"] == "A":
            self.cfg.setdefault("datum", {})["A"] = point
            self.end_pick("Datum A picked.")
            self.fill_marks()
            self.generate()
            return
        p.update(point=point, edge=bool(hit[1]), view=name, tip=self._to_paper(place, point))
        self.set_status("Now click where the symbol should go (Esc cancels).")

    def fill_form(self):
        c = self.cfg
        values = {key: c["title_block"][key] for key, _ in TITLE_FIELDS}
        edges = c["edges"] or {}
        values.update(front=c["views"]["front"], up=c["views"]["up"], sheet=c["sheet"],
                      scale=c["scale"], general_tolerance=c["general_tolerance"],
                      default_finish=c["default_finish"], density=c["density"],
                      edge_external=edges.get("external"), edge_internal=edges.get("internal"),
                      datum_origin=(c.get("datum") or {}).get("origin", "auto"))
        for key, var in self.vars.items():
            var.set(str(values.get(key, "") or ""))
        self.reference.set(bool(c["reference_envelope"]))
        self.surface_default.set((c.get("surfaces") or {}).get("default", "any"))
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", "\n".join(str(n) for n in c["notes"] or []))

    def read_form(self):
        c = self.cfg
        v = {k: var.get().strip() for k, var in self.vars.items()}
        c["views"].update(front=v["front"] or "auto", up=v["up"] or "auto", confirm=False)
        c["sheet"], c["scale"] = v["sheet"] or "auto", v["scale"] or "auto"
        c["general_tolerance"], c["default_finish"] = v["general_tolerance"], v["default_finish"]
        c["density"] = v["density"] or None
        c["edges"] = {"external": v["edge_external"], "internal": v["edge_internal"]}
        c["reference_envelope"] = self.reference.get()
        c.setdefault("datum", {}).update(origin=v["datum_origin"] or "auto", confirm=False)  # keeps picked A/B/C
        c.setdefault("surfaces", {"marks": []})["default"] = self.surface_default.get() or "any"
        for key, _ in TITLE_FIELDS:
            c["title_block"][key] = v[key]
        c["notes"] = [n.strip() for n in self.notes.get("1.0", "end").splitlines() if n.strip()]
        for key, hv in self.hole_vars.items():
            depth = hv["thread_depth"].get().strip()
            try:
                depth = float(depth) if depth else None
            except ValueError:
                depth = None
            c.setdefault("holes", {})[key] = {
                "thread": hv["thread"].get().strip(), "thread_depth": depth,
                "tolerance": hv["tolerance"].get().strip(), "finish": hv["finish"].get().strip(),
                "position": hv["position"].get().strip(), "confirm": not hv["confirmed"].get()}

    def fill_holes(self, types):
        """One block per hole type: thread (suggestions in the list), depth, tolerance, finish."""
        for child in self.hole_list.winfo_children():
            child.destroy()
        self.hole_vars = {}
        if not types:
            ttk.Label(self.hole_list, text="No drilled holes found.").grid(row=0, column=0, sticky="w")
            return
        ttk.Label(self.hole_list, text="Thread, tolerance and finish show on the drawing\n"
                                       "only when the hole type is ticked as confirmed.",
                  foreground="#555").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        row = 1
        for t in types:
            spec = t["spec"]
            hv = {k: tk.StringVar(value="" if spec.get(k) is None else str(spec.get(k)))
                  for k in ("thread", "thread_depth", "tolerance", "finish", "position")}
            hv["confirmed"] = tk.BooleanVar(value=not spec.get("confirm"))
            self.hole_vars[t["key"]] = hv
            ttk.Label(self.hole_list, text=f"{t['letter']}   {t['count']} x {t['key']}",
                      font=("TkDefaultFont", 10, "bold")).grid(row=row, column=0, columnspan=4,
                                                               sticky="w", pady=(8, 2))
            ttk.Label(self.hole_list, text="Thread").grid(row=row + 1, column=0, sticky="w")
            ttk.Combobox(self.hole_list, textvariable=hv["thread"], values=[""] + t["candidates"],
                         width=16).grid(row=row + 1, column=1, sticky="w", padx=(4, 8))
            ttk.Label(self.hole_list, text="Depth").grid(row=row + 1, column=2, sticky="w")
            ttk.Entry(self.hole_list, textvariable=hv["thread_depth"], width=6).grid(
                row=row + 1, column=3, sticky="w", padx=4)
            ttk.Label(self.hole_list, text="Tolerance").grid(row=row + 2, column=0, sticky="w")
            ttk.Entry(self.hole_list, textvariable=hv["tolerance"], width=8).grid(
                row=row + 2, column=1, sticky="w", padx=(4, 8))
            ttk.Label(self.hole_list, text="Finish").grid(row=row + 2, column=2, sticky="w")
            ttk.Entry(self.hole_list, textvariable=hv["finish"], width=8).grid(
                row=row + 2, column=3, sticky="w", padx=4)
            ttk.Checkbutton(self.hole_list, text="confirmed", variable=hv["confirmed"]).grid(
                row=row + 3, column=0, columnspan=2, sticky="w")
            ttk.Label(self.hole_list, text="Position").grid(row=row + 3, column=2, sticky="w")
            ttk.Entry(self.hole_list, textvariable=hv["position"], width=8).grid(
                row=row + 3, column=3, sticky="w", padx=4)
            if spec.get("note"):
                ttk.Label(self.hole_list, text=spec["note"], foreground="#555", wraplength=400).grid(
                    row=row + 4, column=0, columnspan=4, sticky="w")
            row += 5

    # ------------------------------------------------------------ actions

    def open_step(self):
        path = filedialog.askopenfilename(
            title="Open STEP file", filetypes=[("STEP files", "*.step *.stp *.STEP *.STP"),
                                               ("All files", "*.*")])
        if path:
            self.load(Path(path))

    def load(self, path):
        """Open a STEP file; settings come from <name>.yaml next to it when present."""
        yaml_path = path.with_suffix(".yaml")
        self.cfg = partfile.load(yaml_path if yaml_path.exists() else None, path)
        self.end_pick()
        self.info = None
        self.fill_form()
        self.fill_holes([])
        self.fill_marks()
        self.step, self.shape, self.doc = path, None, None
        note = f"   (settings from {yaml_path.name})" if yaml_path.exists() else ""
        self.file_label.config(text=path.name + note)
        self.generate()

    def generate(self):
        if not self.step:
            return
        self.read_form()
        cfg, step, shape = copy.deepcopy(self.cfg), self.step, self.shape

        def work():
            s = shape if shape is not None else geometry.load_step(step)
            doc, info = sheet.build(s, cfg)
            return s, doc, info, sheet.render(doc, info["sheet"], "pdf"), cfg

        self.run("Loading STEP file and generating drawing..." if shape is None
                 else "Generating drawing...", work, self.show)

    def show(self, result):
        self.shape, self.doc, self.info, self.pdf, self.built_cfg = result
        self.preview.show(self.pdf)
        i = self.info
        self.built_cfg["holes"] = {t["key"]: {k: v for k, v in t["spec"].items() if k != "note"}
                                   for t in i["hole_types"]}
        self.cfg["holes"] = copy.deepcopy(self.built_cfg["holes"])
        self.fill_holes(i["hole_types"])
        self.fill_marks()
        msg = (f"{i['sheet']}, scale {sheet.fmt_scale(i['scale'])}, front view {i['front']}, "
               f"up {i['up']}, {len(i['holes'])} holes")
        if i["ports"]:
            msg += f", {len(i['ports'])} waveguide port(s)"
        if i["section"]:
            msg += f", section {i['section']}"
        if i["extra_views"]:
            msg += ", extra views " + ", ".join(i["extra_views"])
        if i["warnings"]:
            msg += "   |   " + "; ".join(w for w in i["warnings"] if "surface mark" in w)
        pending = sum(1 for t in i["hole_types"] if t["spec"].get("confirm"))
        if pending:
            msg += f"   |   {pending} hole type(s) to confirm in the Holes tab"
        if i["problems"]:
            msg += "   |   layout problems: " + "; ".join(i["problems"])
        self.set_status(msg)

    def export(self):
        if not self.doc:
            return
        folder = filedialog.askdirectory(title="Export DXF + PDF to folder",
                                         initialdir=str(self.step.parent))
        if not folder:
            return
        stem = self.step.stem
        files = [Path(folder) / f"{stem}{ext}" for ext in (".dxf", ".pdf", ".yaml")]
        existing = [f.name for f in files if f.exists()]
        if existing and not messagebox.askyesno(
                "Overwrite?", "Replace existing files?\n\n" + "\n".join(existing)):
            return
        self.doc.saveas(files[0])
        files[1].write_bytes(self.pdf)
        files[2].write_text(yaml.safe_dump(self.built_cfg, sort_keys=False, allow_unicode=True),
                            encoding="utf-8")
        self.set_status(f"Exported {', '.join(f.name for f in files)} to {folder}")

    # ------------------------------------------------------------ background work

    def run(self, message, work, done):
        for b in self.buttons:
            b.config(state="disabled")
        self.root.config(cursor="watch")
        self.set_status(message)

        def target():
            try:
                self.jobs.put((done, work(), None))
            except Exception as e:  # shown to the user in the window
                self.jobs.put((done, None, e))

        threading.Thread(target=target, daemon=True).start()

    def _poll(self):
        try:
            done, result, error = self.jobs.get_nowait()
        except queue.Empty:
            pass
        else:
            self.root.config(cursor="")
            self.buttons[0].config(state="normal")
            if error:
                self.set_status(f"Error: {error}")
                messagebox.showerror("drawtool", str(error))
            else:
                done(result)
            if self.step:
                self.buttons[1].config(state="normal")
            if self.doc:
                self.buttons[2].config(state="normal")
        self.root.after(100, self._poll)

    def set_status(self, text):
        self.status.config(text=text)


def _scrollable(parent):
    """A frame inside a canvas with a vertical scrollbar."""
    canvas = tk.Canvas(parent, highlightthickness=0, width=450)
    bar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    inner = ttk.Frame(canvas)
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=bar.set)
    canvas.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")
    return inner


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("analyze", "draw", "-h", "--help"):
        try:
            return cli.main(argv)
        except Exception:
            if sys.stderr is not None:
                raise
            # windowed exe: no console, so leave the traceback where it can be read
            Path("drawtool-error.log").write_text(traceback.format_exc())
            sys.exit(1)
    if sys.platform == "win32":
        try:  # crisp rendering on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    app = App(root)
    if argv:  # e.g. a STEP file dropped on DrawTool.exe
        root.after(200, lambda: app.load(Path(argv[0])))
    root.mainloop()


if __name__ == "__main__":
    main()
