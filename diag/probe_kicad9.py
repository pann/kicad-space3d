"""
KiCad 9 API probe for the SpaceMouse plugin.

Run from KiCad's PCB editor scripting console:

    exec(open('/home/pa/work/git/kicad-space3d/diag/probe_kicad9.py').read())

Reload after edits with the same line (arrow-up).

Goal: collect the three facts we need to fix _apply_view():
  1. The class name of the PCB editor frame (so we can find it via
     wx.GetTopLevelWindows() instead of the missing pcbnew.GetCurrentFrame()).
  2. The concrete VECTOR type returned by view.GetCenter()
     (VECTOR2D = float, VECTOR2I = int nanometres).
  3. Whether wx.CallAfter exists (it should — sanity check only).

It also does a tiny non-destructive pan test (saves the original center,
shifts it by +1mm, then restores) so we can see whether SetCenter +
Refresh actually moves the view.
"""

import wx
import pcbnew


def _hr(title):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


# --- 1. List every top-level window ------------------------------------------

_hr("1. Top-level wx windows")
tops = wx.GetTopLevelWindows()
for w in tops:
    print(f"  {type(w).__name__:30s} | title={w.GetTitle()!r}")


# --- 2. Find the PCB editor frame --------------------------------------------

_hr("2. Locate PCB editor frame")
pcb_frame = None
for w in tops:
    name = type(w).__name__
    # KiCad 9 names; we accept anything that quacks like one
    if name in ("PCB_EDIT_FRAME", "FOOTPRINT_EDIT_FRAME"):
        pcb_frame = w
        print(f"  Found: {name}  title={w.GetTitle()!r}")
        break

if pcb_frame is None:
    # Heuristic fallback: any frame that has GetCanvas
    for w in tops:
        if hasattr(w, "GetCanvas"):
            pcb_frame = w
            print(f"  Fallback by duck-typing: {type(w).__name__}  title={w.GetTitle()!r}")
            break

if pcb_frame is None:
    print("  !! No PCB editor frame found. Open the PCB editor and rerun.")
else:
    print(f"  Class name to hard-code: {type(pcb_frame).__name__!r}")


# --- 3. Inspect canvas / view ------------------------------------------------

if pcb_frame is not None:
    _hr("3. Canvas + view")
    print(f"  hasattr(frame, 'GetCanvas') = {hasattr(pcb_frame, 'GetCanvas')}")
    canvas = pcb_frame.GetCanvas()
    print(f"  canvas type             = {type(canvas).__name__}")
    print(f"  hasattr(canvas, 'GetView') = {hasattr(canvas, 'GetView')}")
    view = canvas.GetView()
    print(f"  view type               = {type(view).__name__}")

    center = view.GetCenter()
    scale  = view.GetScale()
    print(f"  view.GetCenter() type   = {type(center).__name__}")
    print(f"  view.GetCenter() value  = ({center.x}, {center.y})")
    print(f"  view.GetScale()         = {scale}")
    print(f"  has SetCenter           = {hasattr(view, 'SetCenter')}")
    print(f"  has SetScale            = {hasattr(view, 'SetScale')}")

    # Show which VECTOR types pcbnew exposes
    _hr("4. pcbnew VECTOR types available")
    for name in ("VECTOR2D", "VECTOR2I", "VECTOR2L"):
        print(f"  pcbnew.{name}: {'yes' if hasattr(pcbnew, name) else 'NO'}")


# --- 5. wx.CallAfter sanity --------------------------------------------------

_hr("5. wx.CallAfter sanity")
print(f"  callable(wx.CallAfter) = {callable(wx.CallAfter)}")


# --- 6. Non-destructive pan test --------------------------------------------
# Shifts the view by +1mm in X, schedules a refresh, then restores it 1s later.
# If you SEE the view jump then snap back, SetCenter + wx.CallAfter(Refresh) works.

if pcb_frame is not None:
    _hr("6. Pan test (+1mm X, snap back after 1s)")
    try:
        view = pcb_frame.GetCanvas().GetView()
        orig = view.GetCenter()
        VecType = type(orig)               # use the same type the view returned
        shifted = VecType(orig.x + 1_000_000, orig.y)   # 1 mm in nm
        view.SetCenter(shifted)
        wx.CallAfter(pcb_frame.GetCanvas().Refresh)
        print(f"  shifted center to ({shifted.x}, {shifted.y}) using {VecType.__name__}")

        def _restore():
            view.SetCenter(orig)
            pcb_frame.GetCanvas().Refresh()
            print("  [pan test] restored original center")

        wx.CallLater(1000, _restore)
        print("  -> watch the canvas: it should jump right by ~1mm then snap back")
    except Exception as e:
        print(f"  pan test FAILED: {type(e).__name__}: {e}")

print("\nDone. Copy the output back to Claude.")
