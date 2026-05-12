"""
SpaceMouse KiCad Plugin
=======================
Reads 6DOF events from spacenavd's Unix socket and applies them
directly to the KiCad PCB editor view — no keypresses, no window
focus tricks.

Controls (SpaceExplorer axes):
  TX  push left/right  → pan X
  TY  push fwd/back    → pan Y
  TZ  pull/push down   → zoom in/out
  RZ  twist CW/CCW     → zoom (alternative, disabled by default)

  Button 0 (left)      → Zoom to fit board
  Button 1 (right)     → Zoom to fit selection (or board if nothing selected)

Install:
  cp -r spacemouse_kicad ~/.local/share/kicad/8.0/scripting/plugins/
  (or whatever your KiCad version/path is — check with:
   pcbnew scripting console → import pcbnew; print(pcbnew.PLUGIN_DIRECTORIES_SEARCH) )

Auto-start:
  Add this line to ~/.config/kicad/8.0/scripting/startup.py  (create if missing):
    import spacemouse_kicad

Usage:
  Tools → External Plugins → SpaceMouse  (toggles on/off)
  Or auto-starts via startup.py (see above)
"""

import threading
import struct
import socket
import time
import os

import pcbnew

# ── Tuning ──────────────────────────────────────────────────────────────────

SPNAV_SOCK     = "/var/run/spnav.sock"

# Axis deadzone — raw spacenavd units, ~32000 max travel
# Increase if you get drift when puck is released
DEADZONE       = 600

# How strongly TX/TY move the view per event (in KiCad internal units = nm)
# pcbnew works in nanometres internally; 1 mm = 1_000_000 nm
# ~2mm per strong push feels natural — tune to taste
PAN_SCALE      = 0.00015    # multiplied by raw axis value → nm offset

# Zoom scale factor per event. 1.0 = no change, >1 = zoom in
# Applied proportionally to axis deflection
ZOOM_SCALE     = 0.000003   # multiplied by raw axis value → zoom ratio delta

# Event loop rate — 60Hz is plenty, keeps CPU negligible
LOOP_HZ        = 60
LOOP_SLEEP     = 1.0 / LOOP_HZ

# ── Plugin class ─────────────────────────────────────────────────────────────

class SpaceMousePlugin(pcbnew.ActionPlugin):

    def defaults(self):
        self.name             = "SpaceMouse"
        self.category         = "Navigation"
        self.description      = "Toggle SpaceMouse 6DOF navigation (via spacenavd)"
        self.show_toolbar_button = True
        self.icon_file_name   = os.path.join(os.path.dirname(__file__), "icon.png")

    def Run(self):
        """Called when user clicks the plugin. Toggles the listener thread."""
        _manager.toggle()


# ── Background listener ───────────────────────────────────────────────────────

class SpaceMouseManager:
    """
    Singleton that owns the reader thread.
    The thread reads from the spacenavd Unix socket and applies
    view transforms directly via pcbnew's Python API.
    """

    def __init__(self):
        self._thread   = None
        self._stop_evt = threading.Event()
        self._running  = False

    def toggle(self):
        if self._running:
            self.stop()
            _status("SpaceMouse stopped")
        else:
            ok = self.start()
            if ok:
                _status("SpaceMouse active — puck ready")
            else:
                _status(f"SpaceMouse ERROR: cannot connect to {SPNAV_SOCK} — is spacenavd running?")

    def start(self):
        if not os.path.exists(SPNAV_SOCK):
            return False
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._reader_loop,
            name="SpaceMouseReader",
            daemon=True
        )
        self._thread.start()
        self._running = True
        return True

    def stop(self):
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        self._running = False

    # ── Reader thread ─────────────────────────────────────────────────────────

    def _reader_loop(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(SPNAV_SOCK)
            sock.settimeout(0.1)
        except OSError as e:
            print(f"[SpaceMouse] Failed to connect: {e}")
            self._running = False
            return

        print("[SpaceMouse] Connected to spacenavd")
        buf = b""

        while not self._stop_evt.is_set():
            try:
                chunk = sock.recv(64)
                if chunk:
                    buf += chunk
            except socket.timeout:
                pass
            except OSError:
                break

            # Process all complete 32-byte packets in the buffer
            while len(buf) >= 32:
                packet, buf = buf[:32], buf[32:]
                self._handle_packet(packet)

            time.sleep(LOOP_SLEEP)

        sock.close()
        print("[SpaceMouse] Disconnected from spacenavd")

    def _handle_packet(self, data):
        """
        spacenavd sends 32-byte packets.
        Layout: 8 × int32 (little-endian)
          [0] TX  [1] TY  [2] TZ  (translation)
          [3] pad
          [4] RX  [5] RY  [6] RZ  (rotation)
          [7] buttons bitmask
        Values are signed, roughly ±32000 at full deflection.
        """
        vals = struct.unpack('<8i', data)
        tx, ty, tz = vals[0], vals[1], vals[2]
        rx, ry, rz = vals[4], vals[5], vals[6]
        buttons    = vals[7]

        self._apply_view(tx, ty, tz, buttons)

    def _apply_view(self, tx, ty, tz, buttons):
        """
        Manipulate the KiCad PCB editor view.
        All operations run on the reader thread — pcbnew's Python
        bindings are generally thread-safe for view operations.
        """
        try:
            frame = pcbnew.GetCurrentFrame()
            if frame is None:
                return

            view = frame.GetCanvas().GetView()

            # ── Buttons ───────────────────────────────────────────────────────
            # Processed first so a button press doesn't also pan/zoom

            if buttons & 0x01:   # Left button → fit board
                wx_evt = None
                pcbnew.CallAfter(frame.ToTheTop)  # bring to front
                pcbnew.CallAfter(lambda: _zoom_fit(frame))
                return

            if buttons & 0x02:   # Right button → fit selection or board
                pcbnew.CallAfter(lambda: _zoom_fit_selection(frame))
                return

            # ── Translation: TX / TY → pan ────────────────────────────────────

            needs_update = False

            if abs(tx) > DEADZONE or abs(ty) > DEADZONE:
                center = view.GetCenter()

                # TX: push right → positive → move view right (board goes left)
                # TY: push away  → negative → move view up   (board goes down)
                # Invert TY because screen Y is flipped vs spacenavd Y
                dx =  tx * PAN_SCALE * 1_000_000   # → nm
                dy = -ty * PAN_SCALE * 1_000_000   # → nm

                new_center = pcbnew.VECTOR2D(
                    center.x + dx,
                    center.y + dy
                )
                view.SetCenter(new_center)
                needs_update = True

            # ── Translation: TZ → zoom ────────────────────────────────────────

            if abs(tz) > DEADZONE:
                # TZ positive = push cap down = zoom out
                # TZ negative = pull cap up   = zoom in
                scale  = view.GetScale()
                factor = 1.0 - tz * ZOOM_SCALE
                factor = max(0.5, min(2.0, factor))   # clamp per-event change
                view.SetScale(scale * factor)
                needs_update = True

            if needs_update:
                pcbnew.CallAfter(frame.GetCanvas().Refresh)

        except Exception as e:
            # Don't crash the thread on transient API errors
            pass


def _zoom_fit(frame):
    """Zoom to fit entire board."""
    try:
        frame.GetCanvas().GetView().SetScale(1.0)   # reset first
        frame.ZoomFitBoard()
    except Exception:
        pass


def _zoom_fit_selection(frame):
    """Zoom to fit selection, falling back to full board."""
    try:
        board = pcbnew.GetBoard()
        selected = [i for i in board.GetFootprints() if i.IsSelected()]
        if selected:
            frame.ZoomFitSelection()
        else:
            frame.ZoomFitBoard()
    except Exception:
        pass


# ── Module-level singleton ────────────────────────────────────────────────────

_manager = SpaceMouseManager()


def _status(msg):
    """Print status to KiCad scripting console and stdout."""
    print(f"[SpaceMouse] {msg}")
    try:
        # KiCad 9: set the frame status bar text
        frame = pcbnew.GetCurrentFrame()
        if frame:
            frame.SetStatusText(msg, 0)
    except Exception:
        pass


# ── Auto-start helper (called from startup.py) ────────────────────────────────

def auto_start():
    """
    Call this from ~/.config/kicad/9.0/scripting/startup.py to
    start the SpaceMouse listener automatically when KiCad opens.

    Example startup.py:
        import spacemouse_kicad
        spacemouse_kicad.auto_start()
    """
    ok = _manager.start()
    if ok:
        _status("Auto-started successfully")
    else:
        _status(f"Auto-start failed — spacenavd socket not found at {SPNAV_SOCK}")
