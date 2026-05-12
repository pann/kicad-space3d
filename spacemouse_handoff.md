# SpaceMouse KiCad Plugin — Handoff Notes

## System
- OS: Ubuntu 26.04, Wayland (`$WAYLAND_DISPLAY=wayland-0`, `$DISPLAY=:0`)
- KiCad: 9.0
- Device: 3Dconnexion SpaceExplorer (`046d:c627`)

## What's working
- `spacenavd` installed, enabled, running as systemd service
- SpaceExplorer detected on `/dev/hidraw0`
- Unix socket `/var/run/spnav.sock` exists and delivers live 6DOF events
- 6DOF motion confirmed with raw Python socket test (all axes TX/TY/TZ/RX/RY/RZ)
- Plugin installs and **registers** in KiCad 9.0 (visible in External Plugins menu)
- Background reader thread starts and connects to spacenavd socket

## Plugin location
```
~/.local/share/kicad/9.0/scripting/plugins/spacemouse_kicad/
├── __init__.py              # registers SpaceMousePlugin()
├── spacemouse_action.py     # main plugin — ActionPlugin + reader thread
├── icon.png                 # placeholder transparent PNG
```

Auto-start configured in:
```
~/.local/share/kicad/9.0/scripting/startup.py
```
Contents:
```python
import spacemouse_kicad
spacemouse_kicad.auto_start()
```

## The problem
`pcbnew.GetCurrentFrame()` **does not exist in KiCad 9**.

The `_apply_view()` method in `spacemouse_action.py` calls it on every event and
fails silently (bare `except Exception: pass` swallows the error). The thread runs
fine but never moves the view.

`pcbnew.CallAfter()` also does **not** exist in KiCad 9 — use `wx.CallAfter()`.

`VECTOR2D` may need to be `VECTOR2I` — needs verification.

## What needs fixing in spacemouse_action.py

### 1. Find the correct frame handle
Run this in KiCad's scripting console to find the PCB editor frame class name:
```python
import wx
for w in wx.GetTopLevelWindows():
    print(type(w).__name__, '|', w.GetTitle())
```
Then check it for `GetCanvas()` and `GetView()`:
```python
# once you know the frame:
frame = ...  # whichever window matched
print(hasattr(frame, 'GetCanvas'))
canvas = frame.GetCanvas()
print(hasattr(canvas, 'GetView'))
view = canvas.GetView()
print(type(view))
print(hasattr(view, 'SetCenter'), hasattr(view, 'SetScale'), hasattr(view, 'GetCenter'), hasattr(view, 'GetScale'))
```

### 2. Fix _apply_view() — replace GetCurrentFrame + CallAfter
Current broken code:
```python
frame = pcbnew.GetCurrentFrame()   # does not exist in KiCad 9
...
pcbnew.CallAfter(frame.GetCanvas().Refresh)  # also broken
```

Replacement pattern (fill in correct frame class name from step 1):
```python
import wx

def _get_pcb_frame():
    for w in wx.GetTopLevelWindows():
        if type(w).__name__ == 'PCB_EDIT_FRAME':   # verify class name first
            return w
    return None

def _apply_view(self, tx, ty, tz, buttons):
    frame = _get_pcb_frame()
    if frame is None:
        return
    view = frame.GetCanvas().GetView()
    # ... pan/zoom logic ...
    wx.CallAfter(frame.GetCanvas().Refresh)   # wx.CallAfter, not pcbnew
```

### 3. Verify VECTOR type for SetCenter
```python
# In scripting console:
view = frame.GetCanvas().GetView()
c = view.GetCenter()
print(type(c))   # VECTOR2D or VECTOR2I?
```
Then use the matching type in SetCenter. Currently code uses `pcbnew.VECTOR2D`.
`VECTOR2I` takes integer nanometres. `VECTOR2D` takes float.

### 4. Tuning constants (in spacemouse_action.py, top of file)
```python
DEADZONE   = 600       # increase if puck drifts at rest (~0-2000 range)
PAN_SCALE  = 0.00015   # increase for faster panning
ZOOM_SCALE = 0.000003  # increase for faster zooming
```
spacenavd axis values are signed int32, roughly ±32000 at full deflection.
Pan math: `dx = tx * PAN_SCALE * 1_000_000` (converts to nm for KiCad internals).

## Packet format from spacenavd
32 bytes, 8 × int32 little-endian:
```
[0] TX  [1] TY  [2] TZ  [3] pad
[4] RX  [5] RY  [6] RZ  [7] buttons bitmask
```
SpaceExplorer buttons: bit 0 = left button, bit 1 = right button.

## Useful scripting console snippets

Reload plugin without restarting KiCad:
```python
import importlib, sys
for k in list(sys.modules.keys()):
    if 'spacemouse' in k:
        del sys.modules[k]
import spacemouse_kicad
```

Check if reader thread is alive:
```python
from spacemouse_kicad.spacemouse_action import _manager
print(_manager._running)
print(_manager._thread)
print(_manager._thread.is_alive() if _manager._thread else "no thread")
```

Manually toggle:
```python
from spacemouse_kicad.spacemouse_action import _manager
_manager.toggle()
```

Test spacenavd socket directly:
```python
import socket, struct
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("/var/run/spnav.sock")
s.settimeout(2)
data = s.recv(32)
print(struct.unpack('<8i', data))
s.close()
```

## Files edited during this session
All edits were made directly to the installed plugin files. The canonical copy is
the installed location — there is no separate git repo yet.

Source also available (may be slightly out of date) in the chat download:
`spacemouse_kicad_plugin.zip`
