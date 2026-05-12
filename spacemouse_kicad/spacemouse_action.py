"""
SpaceMouse KiCad Plugin (KiCad 9)
=================================
Reads 6DOF events from spacenavd's Unix socket and drives the PCB editor
canvas via synthesized wx events:

  - TX / TY  → continuous middle-mouse-drag (pan)
  - TZ       → mouse-wheel notches         (zoom)

KiCad 9 stripped the Python wrappers for PCB_EDIT_FRAME, so we synthesize
input into the wxGLCanvas. KiCad's own C++ handlers respond as if the
events came from a real mouse.

Threading model:
  - Reader thread: blocks on spacenavd socket, updates a shared snapshot
    under a lock. Never touches wx.
  - wx.Timer (owned by the PCB editor frame) fires on the UI thread,
    reads the snapshot, posts events to the canvas. All wx work stays
    on the UI thread.

Debug:
  Set `BISECT_MODE` below to isolate pan or zoom while diagnosing
  crashes. Per-tick activity is appended to LOG_PATH.
"""

import os
import socket
import struct
import threading
import time
import traceback

import wx
import pcbnew


# === Tuning ==================================================================

SPNAV_SOCK = "/var/run/spnav.sock"

# Bisect / debug switches. Set to one of: "both", "pan_only", "zoom_only", "off".
# When something feels broken, narrow this to isolate the offending path.
BISECT_MODE = "both"

# Verbose per-tick logging to LOG_PATH (only when there's activity to log).
LOG_PATH = "/tmp/spacemouse.log"
LOG_ENABLED = True

# Raw axis values are ~int32; typical strong push ≈ ±2000. Anything below
# this is treated as zero (drift filter).
DEADZONE = 600

# Pan scale: spacenavd unit → canvas pixels per UI tick.
PAN_SCALE = 0.02

# Zoom scale: spacenavd unit → wheel notches per UI tick (before clamp).
ZOOM_SCALE = 0.0008

# Cap wheel notches per tick so a hard push doesn't fire a huge burst.
MAX_WHEEL_NOTCHES_PER_TICK = 3

# UI tick interval. 33 ≈ 30Hz (deliberately conservative during bring-up).
UI_TICK_MS = 33

# Stale check — if no spacenavd event in this long, treat the puck as
# centered (defensive against the daemon going quiet).
EVENT_STALE_S = 0.2


# === Logger (file-based; KiCad console scrolls and we already prefer files) ==

def _log(msg):
    if not LOG_ENABLED:
        return
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{time.time():.3f} {msg}\n")
    except OSError:
        pass


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

    def __init__(self):
        # Reader thread state
        self._reader_thread = None
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._latest = (0, 0, 0, 0)           # tx, ty, tz, buttons
        self._latest_t = 0.0

        # UI thread state
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
        _log(f"START canvas_size={canvas.GetSize().x}x{canvas.GetSize().y} mode={BISECT_MODE}")

        # Reader thread
        self._stop_evt.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="SpaceMouseReader", daemon=True
        )
        self._reader_thread.start()

        # Owned timer — Bind on the OWNER so EVT_TIMER actually dispatches.
        # An unowned wx.Timer() never delivers events; this was the
        # "no movement" bug in v2.
        self._timer = wx.Timer(self._frame)
        self._frame.Bind(wx.EVT_TIMER, self._on_tick, self._timer)
        self._timer.Start(UI_TICK_MS)

        self._running = True
        return True, ""

    def stop(self):
        if not self._running:
            return
        self._running = False
        self._stop_evt.set()
        _log("STOP")

        try:
            if self._timer is not None:
                self._timer.Stop()
                if self._frame is not None:
                    self._frame.Unbind(wx.EVT_TIMER, source=self._timer)
                self._timer = None
        except Exception as e:
            _log(f"stop: timer cleanup raised {type(e).__name__}: {e}")

        try:
            if self._panning:
                self._end_pan()
        except Exception as e:
            _log(f"stop: end_pan raised {type(e).__name__}: {e}")

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
            _log(f"reader connect failed: {e}")
            return

        _log("reader connected")
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
        _log("reader disconnected")

    # ── UI tick (UI thread) ──────────────────────────────────────────────────

    def _on_tick(self, _evt):
        try:
            self._tick_inner()
        except Exception:
            _log("TICK EXCEPTION:\n" + traceback.format_exc())
            # Don't let a bad event tear down the timer — but if this keeps
            # happening, BISECT_MODE will help isolate.

    def _tick_inner(self):
        with self._lock:
            tx, ty, tz, _buttons = self._latest
            age = time.monotonic() - self._latest_t

        if age > EVENT_STALE_S:
            tx = ty = tz = 0

        if abs(tx) < DEADZONE: tx = 0
        if abs(ty) < DEADZONE: ty = 0
        if abs(tz) < DEADZONE: tz = 0

        # Canvas might have been destroyed (PCB editor closed).
        if self._canvas is None or not self._canvas:
            _log("tick: canvas gone, stopping")
            self.stop()
            return

        # Pan
        if BISECT_MODE in ("both", "pan_only"):
            if tx or ty:
                dx = int(tx * PAN_SCALE)
                dy = int(-ty * PAN_SCALE)
                self._continue_pan(dx, dy)
            elif self._panning:
                self._end_pan()

        # Zoom — but never while a pan drag is open, to avoid confusing
        # KiCad's tool dispatcher with overlapping inputs. End pan first.
        if BISECT_MODE in ("both", "zoom_only") and tz:
            notches = int(-tz * ZOOM_SCALE)
            notches = max(-MAX_WHEEL_NOTCHES_PER_TICK,
                          min(MAX_WHEEL_NOTCHES_PER_TICK, notches))
            if notches:
                if self._panning:
                    self._end_pan()
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
            ok = self._canvas.GetEventHandler().ProcessEvent(ev)
            _log(f"MIDDLE_DOWN at ({self._cursor_x},{self._cursor_y}) handled={ok}")
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
        ok = self._canvas.GetEventHandler().ProcessEvent(ev)
        _log(f"MIDDLE_UP at ({self._cursor_x},{self._cursor_y}) handled={ok}")
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
        _log(f"WHEEL notches={notches}")


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
    _log(f"STATUS {msg}")


# === Module singleton ========================================================

_manager = _Manager()


def auto_start():
    ok, reason = _manager.start()
    _status("Auto-started" if ok else f"Auto-start deferred: {reason}")
