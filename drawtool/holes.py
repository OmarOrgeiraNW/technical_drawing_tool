"""Hole types, thread guesses and hole-table text.

A thread is guessed when the drilled diameter lies within the internal-thread
minor-diameter limits (ISO 965-1 class 6H, ASME B1.1 class 2B); a modelled
thread must match the pitch as well. Guesses are only suggestions: the drawing
shows a thread, tolerance or finish only once its entry is confirmed.
"""

import math
import re

INCH = 25.4
# callout, nominal major diameter, pitch, minor diameter min, max (mm)
THREADS = [
    ("M1.6x0.35-6H", 1.6, 0.35, 1.221, 1.321),
    ("M2x0.4-6H", 2.0, 0.4, 1.567, 1.679),
    ("M2.5x0.45-6H", 2.5, 0.45, 2.013, 2.138),
    ("M3x0.5-6H", 3.0, 0.5, 2.459, 2.599),
    ("M3.5x0.6-6H", 3.5, 0.6, 2.850, 3.010),
    ("M4x0.7-6H", 4.0, 0.7, 3.242, 3.422),
    ("M5x0.8-6H", 5.0, 0.8, 4.134, 4.334),
    ("M6x1-6H", 6.0, 1.0, 4.917, 5.153),
    ("M8x1.25-6H", 8.0, 1.25, 6.647, 6.912),
    ("M10x1.5-6H", 10.0, 1.5, 8.376, 8.676),
    ("M12x1.75-6H", 12.0, 1.75, 10.106, 10.441),
    ("#1-64 UNC-2B", 0.073 * INCH, INCH / 64, 0.0561 * INCH, 0.0623 * INCH),
    ("#2-56 UNC-2B", 0.086 * INCH, INCH / 56, 0.0667 * INCH, 0.0737 * INCH),
    ("#3-48 UNC-2B", 0.099 * INCH, INCH / 48, 0.0764 * INCH, 0.0841 * INCH),
    ("#4-40 UNC-2B", 0.112 * INCH, INCH / 40, 0.0849 * INCH, 0.0939 * INCH),
    ("#5-40 UNC-2B", 0.125 * INCH, INCH / 40, 0.0979 * INCH, 0.1062 * INCH),
    ("#6-32 UNC-2B", 0.138 * INCH, INCH / 32, 0.1040 * INCH, 0.1140 * INCH),
    ("#8-32 UNC-2B", 0.164 * INCH, INCH / 32, 0.1300 * INCH, 0.1390 * INCH),
    ("#10-24 UNC-2B", 0.190 * INCH, INCH / 24, 0.1450 * INCH, 0.1560 * INCH),
    ("#12-24 UNC-2B", 0.216 * INCH, INCH / 24, 0.1709 * INCH, 0.1807 * INCH),
    ("1/4-20 UNC-2B", 0.250 * INCH, INCH / 20, 0.1960 * INCH, 0.2070 * INCH),
    ("5/16-18 UNC-2B", 0.3125 * INCH, INCH / 18, 0.2520 * INCH, 0.2650 * INCH),
    ("3/8-16 UNC-2B", 0.375 * INCH, INCH / 16, 0.3070 * INCH, 0.3210 * INCH),
]
# ISO 273 clearance holes (fine, medium, coarse), mentioned next to thread guesses
CLEARANCE = {"M1.6": (1.7, 1.8, 2.0), "M2": (2.2, 2.4, 2.6), "M2.5": (2.7, 2.9, 3.1),
             "M3": (3.2, 3.4, 3.6), "M4": (4.3, 4.5, 4.8), "M5": (5.3, 5.5, 5.8),
             "M6": (6.4, 6.6, 7.0), "M8": (8.4, 9.0, 10.0)}


def fmt(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def type_key(hole, entry):
    """Stable name of a hole type, used as its key in the part YAML."""
    parts = [f"Ø{hole['diameter']:.2f}", "THRU" if hole["through"] else f"depth {entry['depth']:.2f}"]
    if entry["cbore"]:
        parts.append(f"cbore Ø{entry['cbore'][0]:.2f} depth {entry['cbore'][1]:.2f}")
    if entry["csk"]:
        parts.append(f"csk Ø{entry['csk'][0]:.2f} x {entry['csk'][1]}°")
    return " ".join(parts)


def thread_candidates(diameter, pitch=None):
    return [t[0] for t in THREADS
            if t[3] - 0.01 <= diameter <= t[4] + 0.01 and (pitch is None or abs(t[2] - pitch) < 0.02)]


def thread_major(callout):
    """Nominal major diameter of a thread callout (for the ISO 6410 thin arc), or None."""
    base = callout.strip().split()[0].removesuffix("-6H") if callout.strip() else ""
    for name, major, *_ in THREADS:
        if base and base == name.split()[0].removesuffix("-6H"):
            return major
    m = re.match(r"M(\d+(?:\.\d+)?)", base)
    return float(m.group(1)) if m else None


def guess(hole, entry):
    """Pre-filled YAML entry for a hole type. Anything guessed needs confirming."""
    candidates = thread_candidates(hole["diameter"], hole["pitch"])
    thread = candidates[0] if candidates else ""
    notes = []
    if hole["pitch"]:
        notes.append(f"modelled thread, pitch {fmt(hole['pitch'])}")
    if "red" in hole["colours"]:
        notes.append("red faces: threaded" + ("" if thread else ", size not recognised"))
    if len(candidates) > 1:
        notes.append("also fits " + ", ".join(candidates[1:]))
    if hole["through"]:
        clear = [f"{m} ({('fine', 'medium', 'coarse')[i]})" for m, sizes in CLEARANCE.items()
                 for i, c in enumerate(sizes) if abs(c - hole["diameter"]) < 0.051]
        if clear and thread:
            notes.append("or ISO 273 clearance for " + ", ".join(clear))
    if "blue" in hole["colours"]:
        notes.append("blue faces: add a tolerance")
    if "green" in hole["colours"]:
        notes.append("green faces: add a surface finish")
    depth = None
    if thread and not hole["through"]:
        if hole["pitch"]:  # the modelled thread runs the modelled length
            depth = round(entry["depth"], 1)
        else:  # usable thread ends ~3 pitches short of the drill depth (tap chamfer)
            pitch = next(t[2] for t in THREADS if t[0] == thread)
            depth = max(math.floor((entry["depth"] - 3 * pitch) * 2) / 2, round(1.5 * pitch, 1))
    return {"thread": thread, "thread_depth": depth, "tolerance": "", "finish": "",
            "confirm": bool(thread or hole["colours"]), "note": "; ".join(notes)}


def size_text(hole, entry, spec):
    """Hole-table SIZE text. Unconfirmed guesses are left out."""
    use = spec if spec and not spec.get("confirm") else {}
    thread, tol, finish = (str(use.get(k) or "").strip() for k in ("thread", "tolerance", "finish"))
    through, depth = hole["through"], entry["depth"]
    if thread:
        td = use.get("thread_depth")
        text = f"{thread} THRU" if through else f"{thread} depth {fmt(float(td or depth))}"
        if not through:
            text += f", drill Ø{fmt(hole['diameter'])} depth {fmt(depth)}"
    else:
        text = f"Ø{fmt(hole['diameter'])}" + (f" {tol}" if tol else "")
        text += " THRU" if through else f" depth {fmt(depth)}"
    if entry["cbore"]:
        text += f", cbore Ø{fmt(entry['cbore'][0])} depth {fmt(entry['cbore'][1])}"
    if entry["csk"]:
        text += f", csk Ø{fmt(entry['csk'][0])} x {entry['csk'][1]}°"
    if finish:
        text += f", {finish}"
    return text


def pending(spec):
    """Human-readable guess awaiting confirmation, or '' when nothing is pending."""
    if not spec or not spec.get("confirm"):
        return ""
    bits = [str(spec[k]) for k in ("thread", "tolerance", "finish") if spec.get(k)]
    return ", ".join(bits) or spec.get("note", "") or "check this hole"
