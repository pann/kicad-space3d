"""
SpaceMouse KiCad Plugin (KiCad 9)
=================================
Reads 6DOF events from spacenavd's Unix socket and drives the PCB editor
canvas via synthesized wx events:

  - TX / TY  → continuous middle-mouse-drag (pan)
  - TZ       → mouse-wheel notches         (zoom)

KiCad 9 stripped the Python wrappers for PCB_EDIT_FRAME, so we can no
longer call view.SetCenter() / SetScale() from Python. Event synthesis
into the wxGLCanvas is the only path that works, but the events are
processed by KiCad's own C++ input handlers — pan/zoom behaviour is
exactly what real input would produce.

Threading model:
  - Reader thread: blocks on spacenavd socket, updates a single shared
    snapshot under a lock. Never touches wx.
  - wx.Timer on UI thread: every UI_TICK_MS, reads the snapshot and
    posts the appropriate events to the canvas. All wx work stays on
    the UI thread (which is required).
"""

import os
import socket
import struct
import threading
import time

import wx
import pcbnew


# === Tuning ==================================================================

SPNAV_SOCK = "/var/run/spnav.sock"

# Raw axis values are ~int32, typical strong push ≈ ±2000.
# Anything below this is treated as zero (drift filter).
DEADZONE = 600

# Pan scale: spacenavd unit → canvas pixels per UI tick.
# A strong push (~2000) should give a few tens of pixels per tick.
PAN_SCALE = 0.02

# Zoom scale: spacenavd unit → wheel notches per UI tick (before clamp).
# 1 notch ≈ KiCad's per-wheel zoom step (~30%).
ZOOM_SCALE = 0.0008

# Clamp how many wheel notches we synthesize per tick — prevents wild
# zoom jumps if the user slams the cap.
MAX_WHEEL_NOTCHES_PER_TICK = 3

# UI tick interval (milliseconds). 16 ≈ 60Hz.
UI_TICK_MS = 16

# If we haven't seen a spacenavd event in this long, treat the puck as
# centered (defensive against the daemon going quiet while the puck is
# still deflected).
EVENT_STALE_S = 0.2


# === ActionPlugin shell ======================================================

class SpaceMousePlugin(pcbnew.ActionPlugin):

    def defaults(self):
        self.name = "SpaceMouse"
        self.category = "Navigation"
        self.description = "Toggle SpaceMouse 6DOF navigation (via spacenavd)"
        self.show_toolbar_button = True
        self.icon_file_name = os.path.join(os.path.dirname(__file__), "icon.png")

    def Run(self):
        _manager.toggle()


# === Manager =================================================================

class _Manager:
    """
    Owns the reader thread, the wx UI timer, and the synthetic-drag state.
    """

    def __init__(self):
        # Reader thread state
        self._reader_thread = None
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._latest = (0, 0, 0, 0)           # tx, ty, tz, buttons
        self._latest_t = 0.0                  # monotonic time of last update

        # UI thread state (only touched on UI thread)
        self._timer = None
        self._canvas = None
        self._frame = None
        self._panning = False
        self._cursor_x = 0
        self._cursor_y = 0

        self._running = False

    # ── Public toggle ────────────────────────────────────────────────────────

    def toggle(self):
        if self._running:
            self.stop()
            _status("SpaceMouse stopped")
        else:
            ok, reason = self.start()
            _status("SpaceMouse active" if ok else f"SpaceMouse ERROR: {reason}")

    def start(self):
        if self._running:
            return True, "already running"
        if not os.path.exists(SPNAV_SOCK):
            return False, f"spacenavd socket not found at {SPNAV_SOCK}"

        canvas, frame = _find_pcb_canvas()
        if canvas is None:
            return False, "PCB Editor window not found — open it and try again"

        self._canvas = canvas
        self._frame = frame

        # Reader thread
        self._stop_evt.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="SpaceMouseReader", daemon=True
        )
        self._reader_thread.start()

        # UI timer — must be created on UI thread; Run()/auto_start() are
        # both invoked there.
        self._timer = wx.Timer()
        self._timer.Bind(wx.EVT_TIMER, self._on_tick)
        self._timer.Start(UI_TICK_MS)

        self._running = True
        return True, ""

    def stop(self):
        if not self._running:
            return
        self._running = False
        self._stop_evt.set()

        if self._timer is not None:
            self._timer.Stop()
            self._timer = None

        if self._panning:
            self._end_pan()

        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
            self._reader_thread = None

    # ── Reader thread ────────────────────────────────────────────────────────

    def _reader_loop(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(SPNAV_SOCK)
            sock.settimeout(0.5)
        except OSError as e:
            print(f"[SpaceMouse] reader connect failed: {e}")
            return

        print("[SpaceMouse] reader connected")
        buf = b""

        while not self._stop_evt.is_set():
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            except socket.timeout:
                continue
            except OSError:
                break

            while len(buf) >= 32:
                packet, buf = buf[:32], buf[32:]
                vals = struct.unpack("<8i", packet)
                with self._lock:
                    self._latest = (vals[0], vals[1], vals[2], vals[7])
                    self._latest_t = time.monotonic()

        sock.close()
        print("[SpaceMouse] reader disconnected")

    # ── UI tick (UI thread) ──────────────────────────────────────────────────

    def _on_tick(self, _evt):
        # Snapshot the latest puck state under the lock.
        with self._lock:
            tx, ty, tz, _buttons = self._latest
            age = time.monotonic() - self._latest_t

        # Stale → treat as centered.
        if age > EVENT_STALE_S:
            tx = ty = tz = 0

        # Deadzone.
        if abs(tx) < DEADZONE: tx = 0
        if abs(ty) < DEADZONE: ty = 0
        if abs(tz) < DEADZONE: tz = 0

        # If the canvas has been destroyed (e.g. PCB editor closed) bail.
        if self._canvas is None or not self._canvas:
            self.stop()
            return

        # ── Pan via middle-drag state machine ──
        if tx or ty:
            dx = int(tx * PAN_SCALE)
            dy = int(-ty * PAN_SCALE)   # invert: pushing puck forward pans up
            self._continue_pan(dx, dy)
        elif self._panning:
            self._end_pan()

        # ── Zoom via wheel notches ──
        if tz:
            # Pull cap up (tz < 0) → zoom in (positive wheel rotation).
            notches = int(-tz * ZOOM_SCALE)
            notches = max(-MAX_WHEEL_NOTCHES_PER_TICK,
                          min(MAX_WHEEL_NOTCHES_PER_TICK, notches))
            if notches:
                self._send_wheel(notches)

    # ── Synthetic events ─────────────────────────────────────────────────────

    def _continue_pan(self, dx, dy):
        if not self._panning:
            sz = self._canvas.GetSize()
            self._cursor_x = sz.x // 2
            self._cursor_y = sz.y // 2
            ev = wx.MouseEvent(wx.wxEVT_MIDDLE_DOWN)
            ev.m_x = self._cursor_x
            ev.m_y = self._cursor_y
            ev.m_middleDown = True
            ev.SetEventObject(self._canvas)
            self._canvas.GetEventHandler().ProcessEvent(ev)
            self._panning = True

        if dx == 0 and dy == 0:
            return
        self._cursor_x += dx
        self._cursor_y += dy

        ev = wx.MouseEvent(wx.wxEVT_MOTION)
        ev.m_x = self._cursor_x
        ev.m_y = self._cursor_y
        ev.m_middleDown = True
        ev.SetEventObject(self._canvas)
        self._canvas.GetEventHandler().ProcessEvent(ev)

    def _end_pan(self):
        ev = wx.MouseEvent(wx.wxEVT_MIDDLE_UP)
        ev.m_x = self._cursor_x
        ev.m_y = self._cursor_y
        ev.SetEventObject(self._canvas)
        self._canvas.GetEventHandler().ProcessEvent(ev)
        self._panning = False

    def _send_wheel(self, notches):
        sz = self._canvas.GetSize()
        cx = sz.x // 2
        cy = sz.y // 2
        sign = 1 if notches > 0 else -1
        for _ in range(abs(notches)):
            ev = wx.MouseEvent(wx.wxEVT_MOUSEWHEEL)
            ev.m_wheelRotation = sign * 120
            ev.m_wheelDelta = 120
            ev.m_x = cx
            ev.m_y = cy
            ev.SetEventObject(self._canvas)
            self._canvas.GetEventHandler().ProcessEvent(ev)


# === Helpers =================================================================

def _find_pcb_canvas():
    """Returns (canvas, frame) or (None, None) if PCB Editor not open."""
    frame = None
    for w in wx.GetTopLevelWindows():
        if "PCB Editor" in (w.GetTitle() or ""):
            frame = w
            break
    if frame is None:
        return None, None
    return _find_glcanvas(frame), frame


def _find_glcanvas(w):
    for child in w.GetChildren():
        if hasattr(child, "GetClassName") and child.GetClassName() == "wxGLCanvas":
            return child
        found = _find_glcanvas(child)
        if found is not None:
            return found
    return None


def _status(msg):
    print(f"[SpaceMouse] {msg}")


# === Module singleton ========================================================

_manager = _Manager()


def auto_start():
    """Called from ~/.local/share/kicad/9.0/scripting/startup.py."""
    ok, reason = _manager.start()
    _status("Auto-started" if ok else f"Auto-start deferred: {reason}")
