"""
KiCad 9 API probe v2 for the SpaceMouse plugin.

Run from KiCad's PCB editor scripting console:

    exec(open('/home/pa/work/git/kicad-space3d/diag/probe_kicad9.py').read())

Arrow-up to re-run after edits.

All verbose output is written to /tmp/kicad_probe.txt; only a short status
line is printed to the console (KiPython mangles long pasted blocks, and
collecting the output in a file keeps the console clean).
"""

import io
import sys
import traceback

import wx
import pcbnew


OUT_PATH = "/tmp/kicad_probe.txt"


def _run(out):
    """All probe logic — writes to the file handle `out`."""

    def p(*args):
        print(*args, file=out)

    def hr(t):
        p("\n" + "=" * 60)
        p(t)
        p("=" * 60)

    # --- Find PCB editor frame by title -------------------------------------

    pcb_frame = None
    for w in wx.GetTopLevelWindows():
        if "PCB Editor" in (w.GetTitle() or ""):
            pcb_frame = w
            break

    hr("0. Top-level windows")
    for w in wx.GetTopLevelWindows():
        p(f"  {type(w).__name__:20s}  title={w.GetTitle()!r}")

    if pcb_frame is None:
        p("\n!! No PCB Editor frame open. Open it and rerun.")
        return

    hr(f"1. PCB Editor frame: type={type(pcb_frame).__name__}  title={pcb_frame.GetTitle()!r}")

    attrs = sorted(a for a in dir(pcb_frame) if not a.startswith("_"))
    keys = ("canvas", "view", "zoom", "pan", "center", "redraw",
            "refresh", "tool", "board", "frame", "command", "action")
    interesting = [a for a in attrs if any(k in a.lower() for k in keys)]
    p(f"\n  Frame public attrs total: {len(attrs)}")
    p(f"  Interesting subset ({len(interesting)}):")
    for a in interesting:
        p(f"    {a}")

    # 2. Try methods that SWIG sometimes hides from hasattr ------------------

    p("\n  Direct call probes:")
    for call in ("GetCanvas", "GetToolManager", "GetBoard"):
        try:
            v = getattr(pcb_frame, call)()
            p(f"    {call}() -> {type(v).__name__}")
        except Exception as e:
            p(f"    {call}() FAILED: {type(e).__name__}: {e}")

    # 3. Walk child windows --------------------------------------------------

    hr("2. Child window tree (depth-first, max depth 4)")

    def walk(w, depth=0, maxdepth=4):
        if depth > maxdepth:
            return
        for child in w.GetChildren():
            cls = type(child).__name__
            cname = child.GetClassName() if hasattr(child, "GetClassName") else "?"
            name = child.GetName() if hasattr(child, "GetName") else "?"
            size = child.GetSize()
            p(f"    {'  ' * depth}{cls:24s} class={cname!r:26s} name={name!r:22s} size={size.x}x{size.y}")
            walk(child, depth + 1, maxdepth)

    walk(pcb_frame)

    # 4. Find candidate canvases --------------------------------------------

    hr("3. Candidate canvas children (GAL / GLCanvas / EDA_DRAW / CAIRO / DRAWPANEL)")

    def find_canvases(w, found):
        for child in w.GetChildren():
            cls = type(child).__name__
            cname = child.GetClassName() if hasattr(child, "GetClassName") else ""
            if any(s in (cls + cname).upper() for s in
                   ("GAL", "GLCANVAS", "EDA_DRAW", "CAIRO", "DRAWPANEL")):
                found.append(child)
            find_canvases(child, found)

    cands = []
    find_canvases(pcb_frame, cands)
    if not cands:
        p("    (none found — the canvas may be a plain wx.Window)")
    for c in cands:
        cls = type(c).__name__
        cname = c.GetClassName() if hasattr(c, "GetClassName") else ""
        p(f"    {cls}  class={cname!r}  size={c.GetSize().x}x{c.GetSize().y}")
        for call in ("GetView", "GetGAL", "Refresh", "GetParent"):
            ok = hasattr(c, call)
            p(f"      hasattr {call}: {ok}")
        # Try direct GetView even when hasattr says False
        try:
            v = c.GetView()
            p(f"      GetView() direct -> {type(v).__name__}")
        except Exception as e:
            p(f"      GetView() direct FAILED: {type(e).__name__}: {e}")

    # 5. pcbnew module surface ----------------------------------------------

    hr("4. pcbnew module — interesting top-level names")
    mod_names = sorted(n for n in dir(pcbnew) if not n.startswith("_"))
    pkeys = ("view", "canvas", "zoom", "frame", "pan", "center",
             "redraw", "window", "tool", "current", "active", "selection")
    hits = [n for n in mod_names if any(k in n.lower() for k in pkeys)]
    for n in hits:
        obj = getattr(pcbnew, n)
        kind = type(obj).__name__
        p(f"    pcbnew.{n}  ({kind})")
    p(f"\n  Total pcbnew public names: {len(mod_names)}")


# --- Main: redirect into a file, print only a status line --------------------

buf = io.StringIO()
err = None
try:
    _run(buf)
except Exception:
    err = traceback.format_exc()
    buf.write("\n\n!! Probe raised an exception:\n")
    buf.write(err)

try:
    with open(OUT_PATH, "w") as f:
        f.write(buf.getvalue())
    line_count = buf.getvalue().count("\n")
    status = f"[probe] wrote {line_count} lines to {OUT_PATH}"
    if err:
        status += "  (with exception — see end of file)"
    print(status)
except OSError as e:
    print(f"[probe] FAILED to write {OUT_PATH}: {e}")
    print(buf.getvalue())
