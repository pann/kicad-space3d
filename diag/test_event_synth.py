"""
KiCad 9 — test whether wx event synthesis can drive the PCB canvas.

KiCad 9 removed the Python wrappers for PCB_EDIT_FRAME, so we can no longer
call view.SetCenter() / view.SetScale() from Python. The canvas is still
reachable as a wxGLCanvas widget, though. If we can post the same wx
events that real mouse/keyboard input would generate, KiCad's internal
C++ handlers should respond and the view should move.

This test posts a small sequence of synthetic events and reports which
ones the canvas accepted (ProcessEvent returns True). Watch the PCB
canvas — you should see the view actually zoom/pan if it works.

Run from PCB editor scripting console:

    exec(open('/home/pa/work/git/kicad-space3d/diag/test_event_synth.py').read())

Arrow-up to re-run.
"""

import io
import sys
import time
import traceback

import wx
import pcbnew


OUT_PATH = "/tmp/kicad_event_test.txt"


def _run(out):
    def p(*args):
        print(*args, file=out)

    def hr(t):
        p("\n" + "=" * 60); p(t); p("=" * 60)

    # --- Find the PCB Editor frame and its wxGLCanvas ----------------------

    pcb_frame = None
    for w in wx.GetTopLevelWindows():
        if "PCB Editor" in (w.GetTitle() or ""):
            pcb_frame = w
            break
    if pcb_frame is None:
        p("!! No PCB Editor frame open.")
        return

    canvas = None
    def find_glcanvas(w):
        nonlocal canvas
        if canvas is not None:
            return
        for child in w.GetChildren():
            if hasattr(child, "GetClassName") and child.GetClassName() == "wxGLCanvas":
                canvas = child
                return
            find_glcanvas(child)
    find_glcanvas(pcb_frame)
    if canvas is None:
        p("!! No wxGLCanvas child found.")
        return

    hr("Canvas info")
    sz = canvas.GetSize()
    p(f"  canvas: {type(canvas).__name__}  class={canvas.GetClassName()!r}  size={sz.x}x{sz.y}")
    cx, cy = sz.x // 2, sz.y // 2
    p(f"  center pixel: ({cx}, {cy})")
    p(f"  hasattr SetFocus: {hasattr(canvas, 'SetFocus')}")
    p(f"  hasattr ProcessEvent: {hasattr(canvas, 'ProcessEvent')}")
    p(f"  hasattr GetEventHandler: {hasattr(canvas, 'GetEventHandler')}")

    canvas.SetFocus()
    wx.Yield()

    # --- Helper: send an event both sync and async, log both outcomes -------

    def send(label, evt):
        evt.SetEventObject(canvas)
        try:
            handled_sync = canvas.GetEventHandler().ProcessEvent(evt)
        except Exception as e:
            handled_sync = f"EXC:{type(e).__name__}:{e}"
        # Posting a *new* event of the same kind for async
        try:
            wx.PostEvent(canvas, evt)
            posted = True
        except Exception as e:
            posted = f"EXC:{type(e).__name__}:{e}"
        p(f"    [{label}] ProcessEvent={handled_sync!r}  PostEvent={posted!r}")

    # --- Test 1: mouse wheel zoom ------------------------------------------

    hr("Test 1: wxEVT_MOUSEWHEEL — expect view to zoom IN (5 notches up)")
    for i in range(5):
        ev = wx.MouseEvent(wx.wxEVT_MOUSEWHEEL)
        # Different wx versions expose these as attributes vs setters.
        # Try attribute assignment first; wx 4.x supports it for MouseEvent.
        try:
            ev.m_wheelRotation = 120
            ev.m_wheelDelta = 120
            ev.m_x = cx
            ev.m_y = cy
        except Exception as e:
            p(f"    attr-set failed: {e}; trying SetPosition/SetWheelRotation")
            if hasattr(ev, "SetPosition"):
                ev.SetPosition(wx.Point(cx, cy))
            if hasattr(ev, "SetWheelRotation"):
                ev.SetWheelRotation(120)
        send(f"wheel-up #{i+1}", ev)
        wx.Yield()
        time.sleep(0.05)

    time.sleep(0.5)

    hr("Test 1b: wxEVT_MOUSEWHEEL — expect view to zoom OUT (5 notches down)")
    for i in range(5):
        ev = wx.MouseEvent(wx.wxEVT_MOUSEWHEEL)
        try:
            ev.m_wheelRotation = -120
            ev.m_wheelDelta = 120
            ev.m_x = cx
            ev.m_y = cy
        except Exception:
            if hasattr(ev, "SetPosition"):
                ev.SetPosition(wx.Point(cx, cy))
            if hasattr(ev, "SetWheelRotation"):
                ev.SetWheelRotation(-120)
        send(f"wheel-down #{i+1}", ev)
        wx.Yield()
        time.sleep(0.05)

    time.sleep(0.5)

    # --- Test 2: arrow key for pan -----------------------------------------

    hr("Test 2: wxEVT_KEY_DOWN with arrow keys — expect view to pan")
    for code, label in [
        (wx.WXK_RIGHT, "right"),
        (wx.WXK_DOWN,  "down"),
        (wx.WXK_LEFT,  "left"),
        (wx.WXK_UP,    "up"),
    ]:
        for i in range(3):
            ev = wx.KeyEvent(wx.wxEVT_KEY_DOWN)
            try:
                ev.m_keyCode = code
            except Exception:
                pass
            send(f"key-{label} #{i+1}", ev)
            wx.Yield()
            time.sleep(0.05)
        time.sleep(0.2)

    # --- Test 3: middle-mouse-drag pan -------------------------------------

    hr("Test 3: middle-mouse-drag pan — expect view to pan +200px in X")
    # Down at center
    down = wx.MouseEvent(wx.wxEVT_MIDDLE_DOWN)
    try:
        down.m_x = cx; down.m_y = cy
        down.m_middleDown = True
    except Exception:
        if hasattr(down, "SetPosition"):
            down.SetPosition(wx.Point(cx, cy))
    send("middle-down", down)
    wx.Yield()
    time.sleep(0.05)

    # Drag rightward in 20 steps
    for step in range(1, 21):
        mv = wx.MouseEvent(wx.wxEVT_MOTION)
        try:
            mv.m_x = cx + step * 10
            mv.m_y = cy
            mv.m_middleDown = True
        except Exception:
            if hasattr(mv, "SetPosition"):
                mv.SetPosition(wx.Point(cx + step * 10, cy))
        send(f"motion step {step}", mv)
        wx.Yield()
        time.sleep(0.02)

    # Up at end
    up = wx.MouseEvent(wx.wxEVT_MIDDLE_UP)
    try:
        up.m_x = cx + 200; up.m_y = cy
    except Exception:
        if hasattr(up, "SetPosition"):
            up.SetPosition(wx.Point(cx + 200, cy))
    send("middle-up", up)
    wx.Yield()

    hr("Done")
    p("If the view visibly moved during any test, that approach is viable.")
    p("Report back which (if any) you actually saw move.")


# --- Main: redirect stdout to a buffer, write to file -----------------------

buf = io.StringIO()
err = None
try:
    _run(buf)
except Exception:
    err = traceback.format_exc()
    buf.write("\n!! Test raised:\n" + err)

try:
    with open(OUT_PATH, "w") as f:
        f.write(buf.getvalue())
    n = buf.getvalue().count("\n")
    print(f"[event-test] wrote {n} lines to {OUT_PATH}" + ("  (with exception)" if err else ""))
except OSError as e:
    print(f"[event-test] FAILED to write {OUT_PATH}: {e}")
    print(buf.getvalue())
